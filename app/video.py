"""Motion-adaptive frame extraction from video.

Pure processing logic — no FastAPI/SQLModel imports — so it stays unit-testable.

Strategy: decode every frame, score motion on a small blurred grayscale view
(fraction of pixels whose abs-diff from the previous frame exceeds a threshold),
smooth the score with an EMA, and map it to a sampling interval via configurable
tiers: the calmer the footage, the sparser the sampling. Scene cuts (histogram
correlation drop) always force a sample.
"""
from __future__ import annotations

import math
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


def extract_frames(
    video_path: Path,
    out_dir: Path,
    params: ExtractParams,
    *,
    on_progress: Optional[Callable[[int, int, int], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> dict:
    """Extract frames from `video_path` into `out_dir` (full-resolution JPEGs).

    `on_progress(decoded, total, extracted)` fires every 30 decoded frames;
    `total` is the container's frame count (0 when unknown). `should_cancel()`
    is polled at the same cadence; returning True raises `Cancelled`.

    Returns {"fps", "total_frames", "capped", "frames"} where each frame is
    {"frame_idx", "timestamp", "stored_name", "width", "height"}.
    """
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open video: {video_path}")
    try:
        fps = cap.get(cv2.CAP_PROP_FPS)
        if not fps or math.isnan(fps) or fps <= 0:
            fps = 30.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)

        out_dir.mkdir(parents=True, exist_ok=True)
        frames: list[dict] = []
        ema = 0.0
        prev_gray: Optional[np.ndarray] = None
        prev_hist: Optional[np.ndarray] = None
        last_sample = 0  # frame index of the most recent sample
        frame_idx = -1
        capped = False

        while True:
            ok, frame = cap.read()
            if not ok:
                break
            frame_idx += 1

            if frame_idx % 30 == 0:
                if should_cancel is not None and should_cancel():
                    raise Cancelled()
                if on_progress is not None:
                    on_progress(frame_idx, total, len(frames))

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
            if frame_idx == 0 or cut or due:
                if len(frames) >= params.max_frames:
                    capped = True
                    break
                stored = f"{uuid.uuid4().hex}.jpg"
                cv2.imwrite(str(out_dir / stored), frame,
                            [cv2.IMWRITE_JPEG_QUALITY, params.jpeg_quality])
                fh, fw = frame.shape[:2]
                frames.append({
                    "frame_idx": frame_idx,
                    "timestamp": frame_idx / fps,
                    "stored_name": stored,
                    "width": fw,
                    "height": fh,
                })
                last_sample = frame_idx

        if on_progress is not None:
            on_progress(frame_idx + 1, total, len(frames))
        return {
            "fps": fps,
            "total_frames": frame_idx + 1,
            "capped": capped,
            "frames": frames,
        }
    finally:
        cap.release()
