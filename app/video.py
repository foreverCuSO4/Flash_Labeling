"""Video frame extraction.

Pure processing logic — no FastAPI/SQLModel imports — so it stays unit-testable.

The regular extractor samples a deterministic frame every ``interval`` seconds.
The optional auto mode scans with the detector and samples only hit windows;
these are separate pipelines and auto mode never changes the regular sampling
behavior.
"""
from __future__ import annotations

import math
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np

class ParamsError(ValueError):
    """Invalid extraction parameters."""


class Cancelled(Exception):
    """Raised inside extract_frames when should_cancel() becomes true."""


@dataclass
class ExtractParams:
    interval: float = 0.2  # seconds between sampled frames
    max_frames: int = 5000  # safety cap on extracted frames per video
    jpeg_quality: int = 90

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "ExtractParams":
        if d is None:
            return cls()
        if not isinstance(d, dict):
            raise ParamsError("params must be an object")
        p = cls()
        if "interval" in d:
            p.interval = _num(d, "interval", 0.01, 3600)
        if "max_frames" in d:
            p.max_frames = int(_num(d, "max_frames", 1, 100000))
        if "jpeg_quality" in d:
            p.jpeg_quality = int(_num(d, "jpeg_quality", 1, 100))
        return p

    def to_dict(self) -> dict:
        return {
            "interval": self.interval,
            "max_frames": self.max_frames,
            "jpeg_quality": self.jpeg_quality,
        }


def _num(d: dict, key: str, lo: float, hi: float) -> float:
    v = d[key]
    if not isinstance(v, (int, float)) or not (lo <= v <= hi):
        raise ParamsError(f"{key} must be a number in [{lo}, {hi}]")
    return float(v)


_DEDUP_IMAGE_SIZE = (64, 36)
_DEDUP_IMAGE_DIFF = 0.025
_DEDUP_MODEL_IOU = 0.98
_DEDUP_MODEL_CENTER = 0.015
_DEDUP_MODEL_SCORE = 0.20
_DEDUP_MAX_GAP_S = 3.0


def _dedup_model_rows(rows: Optional[np.ndarray], conf: float) -> list[tuple]:
    """Return stable, significant detection signatures for duplicate checks."""
    if rows is None or len(rows) == 0:
        return []
    scores = rows[:, 8:12].max(axis=1)
    selected = rows[scores >= conf]
    signatures = []
    for row in selected:
        points = row[:8].reshape(4, 2).astype(np.float64)
        x1, y1 = points.min(axis=0)
        x2, y2 = points.max(axis=0)
        cx, cy = points.mean(axis=0)
        signatures.append((
            int(np.argmax(row[8:12])),
            float(x1), float(y1), float(x2), float(y2),
            float(cx), float(cy), float(row[8:12].max()),
        ))
    signatures.sort(key=lambda s: (s[5], s[6], s[0]))
    return signatures


def _boxes_similar(a: tuple, b: tuple) -> bool:
    ax1, ay1, ax2, ay2 = a[1:5]
    bx1, by1, bx2, by2 = b[1:5]
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    area_a = max(1.0, ax2 - ax1) * max(1.0, ay2 - ay1)
    area_b = max(1.0, bx2 - bx1) * max(1.0, by2 - by1)
    iou = inter / (area_a + area_b - inter)
    diagonal = math.hypot(640.0, 384.0)
    center_delta = math.hypot(a[5] - b[5], a[6] - b[6]) / diagonal
    return iou >= _DEDUP_MODEL_IOU and center_delta <= _DEDUP_MODEL_CENTER


def _models_similar(a: Optional[np.ndarray], b: Optional[np.ndarray], conf: float) -> bool:
    left = _dedup_model_rows(a, conf)
    right = _dedup_model_rows(b, conf)
    if len(left) != len(right):
        return False
    return all(
        x[0] == y[0]
        and abs(x[7] - y[7]) <= _DEDUP_MODEL_SCORE
        and _boxes_similar(x, y)
        for x, y in zip(left, right)
    )


def _dedup_image_view(path: Path) -> Optional[np.ndarray]:
    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        return None
    return cv2.resize(image, _DEDUP_IMAGE_SIZE, interpolation=cv2.INTER_AREA)


def _images_similar(a: Optional[np.ndarray], b: Optional[np.ndarray]) -> bool:
    if a is None or b is None or a.shape != b.shape:
        return False
    return float(cv2.absdiff(a, b).mean()) / 255.0 <= _DEDUP_IMAGE_DIFF


def _deduplicate_auto_frames(
    frames: list[dict],
    detections: dict[int, np.ndarray],
    out_dir: Path,
    fps: float,
    conf: float,
) -> tuple[list[dict], dict[int, np.ndarray]]:
    """Drop adjacent candidates only when model and appearance both match."""
    kept: list[dict] = []
    kept_detections: dict[int, np.ndarray] = {}
    previous_image: Optional[np.ndarray] = None
    previous_frame_idx: Optional[int] = None
    for frame in sorted(frames, key=lambda f: f["frame_idx"]):
        frame_idx = frame["frame_idx"]
        image = _dedup_image_view(out_dir / frame["stored_name"])
        previous_rows = (kept_detections.get(previous_frame_idx)
                         if previous_frame_idx is not None else None)
        close_in_time = (
            previous_frame_idx is not None
            and fps > 0
            and (frame_idx - previous_frame_idx) / fps <= _DEDUP_MAX_GAP_S
        )
        if close_in_time and _models_similar(previous_rows, detections.get(frame_idx), conf):
            if _images_similar(previous_image, image):
                (out_dir / frame["stored_name"]).unlink(missing_ok=True)
                continue
        kept.append(frame)
        kept_detections[frame_idx] = detections.get(frame_idx, np.empty((0, 21), np.float32))
        previous_frame_idx = frame_idx
        previous_image = image
    return kept, kept_detections


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
        sample_every = max(1, round(params.interval * fps))
        frame_idx = start - 1

        while frame_idx + 1 < end:
            ok, frame = cap.read()
            if not ok:
                break
            frame_idx += 1

            if not shared["tick"](frame_idx):
                return  # cancelled (or another segment cancelled)

            if frame_idx == start or (frame_idx - start) % sample_every == 0:
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
    Segment starts are always sampled, so results may differ from single-thread
    output by up to `workers - 1` extra frames at the boundaries.

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
    frames = sorted(shared["frames"], key=lambda f: f["frame_idx"])
    frames, detections = _deduplicate_auto_frames(
        frames, detections, out_dir, fps, params.conf,
    )
    if on_progress is not None:
        with lock:
            on_progress(total_decoded_pass1 + shared["decoded"], total,
                        len(frames))
    return {
        "fps": fps,
        "total_frames": total if total else total_decoded_pass1,
        "capped": capped,
        "frames": frames,
        "detections": detections,
    }
