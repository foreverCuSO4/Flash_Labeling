"""Video frame extraction.

Pure processing logic — no FastAPI/SQLModel imports — so it stays unit-testable.

Motion mode (`extract_frames`): score motion on a small blurred grayscale view
(fraction of pixels whose abs-diff from the previous frame exceeds a threshold),
smooth the score with an EMA, and map it to a sampling interval via configurable
tiers: the calmer the footage, the sparser the sampling. Scene cuts (histogram
correlation drop) always force a sample.

Auto mode (`extract_frames_auto`, docs/auto_labeling.md): scan every frame
with the detector at a low confidence floor, dilate hits ±dilate_s into
"annotation-worthy" windows, re-decode just the windows and sample at
sample_fps; each extracted frame's detector rows are returned alongside so
the caller can cache them for brush annotation.
"""
from __future__ import annotations

import math
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np

# (motion score ceiling, sample interval in seconds). The first tier whose
# ceiling the smoothed score falls under applies; the last tier must have an
# infinite ceiling (catch-all). JSON serialization uses null for infinity.
DEFAULT_TIERS: list[tuple[float, float]] = [
    (0.005, 10.0),  # nearly static -> 1 frame / 10s
    (0.02, 5.0),    # slight motion -> 1 frame / 5s
    (0.08, 1.0),    # moderate motion -> 1 frame / 1s
    (math.inf, 0.2),  # fast motion -> 5 fps
]


class ParamsError(ValueError):
    """Invalid extraction parameters."""


class Cancelled(Exception):
    """Raised inside extract_frames when should_cancel() becomes true."""


@dataclass
class ExtractParams:
    tiers: list[tuple[float, float]] = field(default_factory=lambda: list(DEFAULT_TIERS))
    min_interval: float = 0.1  # never sample faster than this (seconds)
    max_interval: float = 30.0  # always sample at least this often (seconds)
    max_frames: int = 5000  # safety cap on extracted frames per video
    jpeg_quality: int = 90
    analyze_width: int = 256  # downscale width for the motion-analysis pass
    ema_alpha: float = 0.3  # EMA weight of the newest motion score
    diff_thresh: int = 25  # per-pixel abs-diff threshold (0..255)
    scene_cut_corr: float = 0.5  # histogram correlation below this = scene cut

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "ExtractParams":
        if d is None:
            return cls()
        if not isinstance(d, dict):
            raise ParamsError("params must be an object")
        p = cls()
        if "tiers" in d:
            raw = d["tiers"]
            if not isinstance(raw, list) or not raw:
                raise ParamsError("tiers must be a non-empty list")
            tiers = []
            for t in raw:
                if not isinstance(t, (list, tuple)) or len(t) != 2:
                    raise ParamsError("each tier must be [ceiling, interval_seconds]")
                ceiling, interval = t
                if ceiling is None:
                    ceiling = math.inf
                if not isinstance(ceiling, (int, float)) or ceiling <= 0:
                    raise ParamsError("tier ceiling must be a positive number or null")
                if not isinstance(interval, (int, float)) or interval <= 0:
                    raise ParamsError("tier interval must be a positive number")
                tiers.append((float(ceiling), float(interval)))
            tiers.sort(key=lambda t: t[0])
            if not math.isinf(tiers[-1][0]):
                raise ParamsError("the last tier must have a null ceiling (catch-all)")
            p.tiers = tiers
        if "min_interval" in d:
            p.min_interval = _num(d, "min_interval", 0.01, 60)
        if "max_interval" in d:
            p.max_interval = _num(d, "max_interval", 1, 3600)
        if p.min_interval >= p.max_interval:
            raise ParamsError("min_interval must be smaller than max_interval")
        if "max_frames" in d:
            p.max_frames = int(_num(d, "max_frames", 1, 100000))
        if "jpeg_quality" in d:
            p.jpeg_quality = int(_num(d, "jpeg_quality", 1, 100))
        return p

    def to_dict(self) -> dict:
        return {
            "tiers": [[None if math.isinf(c) else c, i] for c, i in self.tiers],
            "min_interval": self.min_interval,
            "max_interval": self.max_interval,
            "max_frames": self.max_frames,
            "jpeg_quality": self.jpeg_quality,
        }


def _num(d: dict, key: str, lo: float, hi: float) -> float:
    v = d[key]
    if not isinstance(v, (int, float)) or not (lo <= v <= hi):
        raise ParamsError(f"{key} must be a number in [{lo}, {hi}]")
    return float(v)


def interval_for(score: float, params: ExtractParams) -> float:
    """Map a smoothed motion score to a sampling interval, clamped to bounds."""
    for ceiling, interval in params.tiers:
        if score < ceiling:
            return min(max(interval, params.min_interval), params.max_interval)
    return min(max(params.tiers[-1][1], params.min_interval), params.max_interval)


def _analysis_view(frame: np.ndarray, width: int) -> np.ndarray:
    h, w = frame.shape[:2]
    if w > width:
        frame = cv2.resize(frame, (width, max(1, round(h * width / w))),
                           interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return cv2.GaussianBlur(gray, (5, 5), 0)


def motion_score(prev_gray: np.ndarray, gray: np.ndarray, thresh: int) -> float:
    """Fraction of pixels whose abs-diff between frames exceeds `thresh`."""
    diff = cv2.absdiff(prev_gray, gray)
    _, mask = cv2.threshold(diff, thresh, 255, cv2.THRESH_BINARY)
    return cv2.countNonZero(mask) / mask.size


def _hist(gray: np.ndarray) -> np.ndarray:
    h = cv2.calcHist([gray], [0], None, [64], [0, 256])
    return cv2.normalize(h, h).flatten()


def _extract_segment(
    video_path: Path,
    out_dir: Path,
    params: ExtractParams,
    fps: float,
    start: int,
    end: float,
    shared: dict,
) -> None:
    """Decode frames [start, end) of the video and sample them.

    All cross-segment state (frame list, decoded count, cap, cancel, progress)
    lives in `shared`; see extract_frames. The first frame of a segment is
    always sampled, which may add up to `workers - 1` extra frames at segment
    boundaries — harmless over-sampling.
    """
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open video: {video_path}")
    try:
        if start:
            cap.set(cv2.CAP_PROP_POS_FRAMES, start)
        ema = 0.0
        prev_gray: Optional[np.ndarray] = None
        prev_hist: Optional[np.ndarray] = None
        last_sample = start
        frame_idx = start - 1

        while frame_idx + 1 < end:
            ok, frame = cap.read()
            if not ok:
                break
            frame_idx += 1

            if not shared["tick"](frame_idx):
                return  # cancelled (or another segment cancelled)

            gray = _analysis_view(frame, params.analyze_width)
            cut = False
            if prev_gray is not None:
                raw = motion_score(prev_gray, gray, params.diff_thresh)
                ema = params.ema_alpha * raw + (1 - params.ema_alpha) * ema
                h = _hist(gray)
                cut = bool(cv2.compareHist(prev_hist, h, cv2.HISTCMP_CORREL) < params.scene_cut_corr)
                prev_hist = h
            else:
                prev_hist = _hist(gray)
            prev_gray = gray

            # The due interval is re-evaluated every frame from the current
            # smoothed score, so a sudden burst of motion starts sampling
            # promptly instead of waiting out a stale "static" deadline.
            due = (frame_idx - last_sample) >= max(1, round(interval_for(ema, params) * fps))
            if frame_idx == start or cut or due:
                stored = f"{uuid.uuid4().hex}.jpg"
                fh, fw = frame.shape[:2]
                info = {
                    "frame_idx": frame_idx,
                    "timestamp": frame_idx / fps,
                    "stored_name": stored,
                    "width": fw,
                    "height": fh,
                }
                if not shared["reserve"](info):
                    return  # max_frames cap reached
                cv2.imwrite(str(out_dir / stored), frame,
                            [cv2.IMWRITE_JPEG_QUALITY, params.jpeg_quality])
                last_sample = frame_idx
    finally:
        cap.release()


def extract_frames(
    video_path: Path,
    out_dir: Path,
    params: ExtractParams,
    *,
    workers: int = 1,
    on_progress: Optional[Callable[[int, int, int], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> dict:
    """Extract frames from `video_path` into `out_dir` (full-resolution JPEGs).

    With `workers > 1` and a known frame count, the video is split into
    contiguous ranges decoded in parallel threads (OpenCV releases the GIL).
    Each segment scores motion independently; segment starts are always
    sampled, so results may differ from single-thread output by up to
    `workers - 1` extra frames at the boundaries.

    `on_progress(decoded, total, extracted)` fires every ~30 decoded frames;
    `total` is the container's frame count (0 when unknown). `should_cancel()`
    is polled at the same cadence; returning True raises `Cancelled`.

    Returns {"fps", "total_frames", "capped", "frames"} where each frame is
    {"frame_idx", "timestamp", "stored_name", "width", "height"} sorted by
    frame index.
    """
    probe = cv2.VideoCapture(str(video_path))
    if not probe.isOpened():
        raise RuntimeError(f"cannot open video: {video_path}")
    fps = probe.get(cv2.CAP_PROP_FPS)
    if not fps or math.isnan(fps) or fps <= 0:
        fps = 30.0
    total = int(probe.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    probe.release()
    # Containers without a frame index report garbage here (e.g. int64 min,
    # ffmpeg's AV_NOPTS_VALUE) — treat anything insane as "unknown" (0).
    if not (0 < total < 2 ** 40):
        total = 0

    out_dir.mkdir(parents=True, exist_ok=True)
    if workers > 1 and total >= workers * 900:
        step = math.ceil(total / workers)
        ranges = [(s, min(s + step, total)) for s in range(0, total, step)]
    else:
        ranges = [(0, math.inf)]

    lock = threading.Lock()
    shared: dict = {"decoded": 0, "frames": [], "capped": False,
                    "cancelled": False, "errors": []}

    def tick(frame_idx: int) -> bool:
        """Count one decoded frame; report progress; honour cancellation."""
        with lock:
            shared["decoded"] += 1
            decoded = shared["decoded"]
            extracted = len(shared["frames"])
        if decoded % 30 == 0:
            if should_cancel is not None and should_cancel():
                return False
            if on_progress is not None:
                on_progress(decoded, total, extracted)
        return True

    def reserve(info: dict) -> bool:
        """Take a max_frames slot under the lock (JPEG write stays outside)."""
        with lock:
            if len(shared["frames"]) >= params.max_frames:
                shared["capped"] = True
                return False
            shared["frames"].append(info)
            return True

    shared["tick"] = tick
    shared["reserve"] = reserve

    def run_segment(start: int, end: float) -> None:
        try:
            _extract_segment(video_path, out_dir, params, fps, start, end, shared)
        except Cancelled:
            shared["cancelled"] = True
        except Exception as e:  # noqa: BLE001 — surfaced after the join
            shared["errors"].append(e)

    if len(ranges) == 1:
        run_segment(*ranges[0])
    else:
        threads = [threading.Thread(target=run_segment, args=r, daemon=True) for r in ranges]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    if shared["errors"]:
        raise shared["errors"][0]
    if should_cancel is not None and should_cancel():
        raise Cancelled()
    if on_progress is not None:
        with lock:
            on_progress(shared["decoded"], total, len(shared["frames"]))
    frames = sorted(shared["frames"], key=lambda f: f["frame_idx"])
    return {
        "fps": fps,
        "total_frames": shared["decoded"],
        "capped": shared["capped"],
        "frames": frames,
    }


# ---------------------------------------------------------------------------
# Auto mode: model-driven scan -> windows -> sampled extraction


def _auto_scan_segment(
    video_path: Path,
    fps: float,
    start: int,
    end: float,
    stride: int,
    conf: float,
    detector,              # Callable[[np.ndarray], list[np.ndarray rows]]
    shared: dict,
    detect_batch: int = 32,
) -> None:
    """Pass 1 for one segment: detect every `stride`-th frame.

    Appends to shared["scan"]: (frame_idx, has_hit, rows) for every scanned
    frame (rows kept — they are the brush cache payload for sampled frames).
    """
    from .autolabel import frame_has_hit
    import cv2  # local import: mirrors _extract_segment style

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open video: {video_path}")
    try:
        if start:
            cap.set(cv2.CAP_PROP_POS_FRAMES, start)
        frame_idx = start - 1
        batch: list[tuple[int, "np.ndarray"]] = []

        def flush():
            if not batch:
                return
            frames = np.stack([f for _, f in batch])
            rows_list = detector(frames)
            for (idx, _f), rows in zip(batch, rows_list):
                shared["scan"].append(
                    (idx, frame_has_hit(rows, conf), rows))
            batch.clear()

        while frame_idx + 1 < end:
            ok, frame = cap.read()
            if not ok:
                break
            frame_idx += 1
            if not shared["tick"](frame_idx):
                flush()
                return  # cancelled
            if (frame_idx - start) % stride:
                continue
            fh, fw = frame.shape[:2]
            if (fw, fh) != (640, 384):
                frame = cv2.resize(frame, (640, 384))
            batch.append((frame_idx, frame))
            if len(batch) >= detect_batch:
                flush()
        flush()
    finally:
        cap.release()


def _auto_extract_segment(
    video_path: Path,
    out_dir: Path,
    jpeg_quality: int,
    fps: float,
    targets: dict[int, str],   # frame_idx -> stored_name (uuid reserved by caller)
    shared: dict,
) -> dict[int, dict]:
    """Pass 2 for one segment: re-decode and write the sample grid."""
    import cv2

    out: dict[int, dict] = {}
    if not targets:
        return out
    lo, hi = min(targets), max(targets) + 1
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open video: {video_path}")
    try:
        if lo:
            cap.set(cv2.CAP_PROP_POS_FRAMES, lo)
        frame_idx = lo - 1
        while frame_idx + 1 < hi:
            ok, frame = cap.read()
            if not ok:
                break
            frame_idx += 1
            if frame_idx not in targets:
                continue
            if not shared["tick"](frame_idx):
                return out  # cancelled
            fh, fw = frame.shape[:2]
            stored = targets[frame_idx]
            cv2.imwrite(str(out_dir / stored), frame,
                        [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
            out[frame_idx] = {
                "frame_idx": frame_idx,
                "timestamp": frame_idx / fps,
                "stored_name": stored,
                "width": fw,
                "height": fh,
            }
    finally:
        cap.release()
    return out


def extract_frames_auto(
    video_path: Path,
    out_dir: Path,
    params,                    # app.autolabel.AutoScanParams (typed loosely: no import cycle)
    detector,
    *,
    workers: int = 1,
    on_progress: Optional[Callable[[int, int, int], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> dict:
    """Model-driven extraction (docs/auto_labeling.md item 1).

    Returns {"fps", "total_frames", "capped", "frames", "detections"} where
    `frames` matches extract_frames and `detections` maps frame_idx ->
    detector rows (k, 21) float32 for every extracted frame.
    """
    from .autolabel import compute_windows, sample_frames

    probe = cv2.VideoCapture(str(video_path))
    if not probe.isOpened():
        raise RuntimeError(f"cannot open video: {video_path}")
    fps = probe.get(cv2.CAP_PROP_FPS)
    if not fps or math.isnan(fps) or fps <= 0:
        fps = 30.0
    total = int(probe.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    probe.release()
    if not (0 < total < 2 ** 40):
        total = 0

    out_dir.mkdir(parents=True, exist_ok=True)
    if workers > 1 and total >= workers * 900:
        step = math.ceil(total / workers)
        ranges = [(s, min(s + step, total)) for s in range(0, total, step)]
    else:
        ranges = [(0, math.inf)]

    lock = threading.Lock()
    shared: dict = {"decoded": 0, "scan": [], "cancelled": False,
                    "errors": []}

    def tick(frame_idx: int) -> bool:
        with lock:
            shared["decoded"] += 1
            decoded = shared["decoded"]
        if decoded % 30 == 0:
            if should_cancel is not None and should_cancel():
                return False
            if on_progress is not None:
                on_progress(decoded, total, 0)
        return True

    shared["tick"] = tick

    def run_scan(start: int, end: float) -> None:
        try:
            _auto_scan_segment(video_path, fps, start, end,
                               params.scan_stride, params.conf,
                               detector, shared)
        except Cancelled:
            shared["cancelled"] = True
        except Exception as e:  # noqa: BLE001
            shared["errors"].append(e)

    if len(ranges) == 1:
        run_scan(*ranges[0])
    else:
        threads = [threading.Thread(target=run_scan, args=r, daemon=True)
                   for r in ranges]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    if shared["errors"]:
        raise shared["errors"][0]
    if should_cancel is not None and should_cancel():
        raise Cancelled()

    scan = sorted(shared["scan"], key=lambda t: t[0])
    scan_total = scan[-1][0] + 1 if scan else 0
    hits = [False] * scan_total
    rows_by_idx: dict[int, "np.ndarray"] = {}
    for idx, hit, rows in scan:
        hits[idx] = hit
        rows_by_idx[idx] = rows
    with lock:
        total_decoded_pass1 = shared["decoded"]
        shared["decoded"] = 0
        shared["frames"] = []

    # The scan timeline follows the video frame grid (stride applies to which
    # frames get detected). Windows are dilated on a stride-adjusted fps and
    # mapped back onto real frame indices.
    stride = params.scan_stride
    if stride > 1:
        hits_strided = [hits[f] for f in range(0, scan_total, stride)]
        windows = compute_windows(hits_strided, fps / stride, params.dilate_s)
        windows = [(s * stride, e * stride) for s, e in windows]
    else:
        windows = compute_windows(hits, fps, params.dilate_s)
    picks = sample_frames(windows, fps, params.sample_fps, params.max_frames)
    # sample_frames stops at the cap; we recompute the uncapped count to know
    # whether truncation happened.
    full = sample_frames(windows, fps, params.sample_fps, max_frames=2 ** 30)
    capped = len(full) > len(picks)

    # Nearest-scanned-row lookup for picks that fell between scan grid points.
    scanned = sorted(rows_by_idx)

    def rows_for(idx: int):
        if idx in rows_by_idx:
            return rows_by_idx[idx]
        for alt in range(idx + 1, idx + stride + 1):
            if alt in rows_by_idx:
                return rows_by_idx[alt]
        return np.empty((0, 21), np.float32)

    # Pass 2: re-decode target ranges and write JPEGs.
    targets = {f: f"{uuid.uuid4().hex}.jpg" for f in picks}
    detections: dict[int, "np.ndarray"] = {f: rows_for(f) for f in picks}

    per_range: list[dict[int, str]] = [dict() for _ in ranges]
    for f, stored in targets.items():
        for i, (s, e) in enumerate(ranges):
            if s <= f < e:
                per_range[i][f] = stored
                break

    def tick_extract(frame_idx: int) -> bool:
        with lock:
            shared["decoded"] += 1
            decoded = shared["decoded"]
            extracted = len(shared["frames"])
        if decoded % 30 == 0:
            if should_cancel is not None and should_cancel():
                return False
            if on_progress is not None:
                on_progress(total_decoded_pass1 + decoded, total, extracted)
        return True

    shared["tick"] = tick_extract

    def run_extract(i: int) -> None:
        try:
            res = _auto_extract_segment(video_path, out_dir,
                                        params.jpeg_quality, fps,
                                        per_range[i], shared)
            with lock:
                shared["frames"].extend(res.values())
        except Cancelled:
            shared["cancelled"] = True
        except Exception as e:  # noqa: BLE001
            shared["errors"].append(e)

    if len(ranges) == 1:
        run_extract(0)
    else:
        threads = [threading.Thread(target=run_extract, args=(i,), daemon=True)
                   for i in range(len(ranges))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    if shared["errors"]:
        raise shared["errors"][0]
    if shared["cancelled"] or (should_cancel is not None and should_cancel()):
        raise Cancelled()
    if on_progress is not None:
        with lock:
            on_progress(total_decoded_pass1 + shared["decoded"], total,
                        len(shared["frames"]))

    frames = sorted(shared["frames"], key=lambda f: f["frame_idx"])
    return {
        "fps": fps,
        "total_frames": total if total else total_decoded_pass1,
        "capped": capped,
        "frames": frames,
        "detections": detections,
    }
