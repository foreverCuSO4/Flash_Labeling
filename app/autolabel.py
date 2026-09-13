"""Model-driven auto-labeling primitives — pure logic, no FastAPI/SQLModel
imports, so it stays unit-testable (mirrors app/video.py's structure).

Two features from docs/auto_labeling.md:
1. auto frame selection: scan the whole video with the detector at a low
   confidence floor, dilate hit frames ±dilate_s, take the union of the
   resulting windows, then sample at sample_fps inside those windows;
2. brush hit-test: given the detector rows of an extracted frame, pick the
   detection whose box center falls inside the brush circle.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np


class ParamsError(ValueError):
    """Invalid auto-scan parameters."""


@dataclass
class AutoScanParams:
    conf: float = 0.2          # hit threshold for window selection
    dilate_s: float = 3.0      # expand each hit frame ±seconds
    sample_fps: float = 10.0   # sampling rate inside windows
    scan_stride: int = 1       # detect every N-th frame during the scan pass
    jpeg_quality: int = 90
    max_frames: int = 5000

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "AutoScanParams":
        if d is None:
            return cls()
        if not isinstance(d, dict):
            raise ParamsError("auto params must be an object")
        p = cls()
        if "conf" in d:
            p.conf = _num(d, "conf", 0.001, 0.99)
        if "dilate_s" in d:
            p.dilate_s = _num(d, "dilate_s", 0.0, 60.0)
        if "sample_fps" in d:
            p.sample_fps = _num(d, "sample_fps", 0.1, 60.0)
        if "scan_stride" in d:
            v = d["scan_stride"]
            if not isinstance(v, int) or not (1 <= v <= 30):
                raise ParamsError("scan_stride must be an int in [1, 30]")
            p.scan_stride = v
        if "jpeg_quality" in d:
            p.jpeg_quality = int(_num(d, "jpeg_quality", 1, 100))
        if "max_frames" in d:
            p.max_frames = int(_num(d, "max_frames", 1, 100000))
        return p

    def to_dict(self) -> dict:
        return {
            "conf": self.conf,
            "dilate_s": self.dilate_s,
            "sample_fps": self.sample_fps,
            "scan_stride": self.scan_stride,
            "jpeg_quality": self.jpeg_quality,
            "max_frames": self.max_frames,
        }


def _num(d: dict, key: str, lo: float, hi: float) -> float:
    v = d[key]
    if not isinstance(v, (int, float)) or not (lo <= v <= hi):
        raise ParamsError(f"{key} must be a number in [{lo}, {hi}]")
    return float(v)


def frame_has_hit(rows: np.ndarray, conf: float) -> bool:
    """Whether any detector row (k, 21) scores >= conf. Score = max(cols 8:12)."""
    if rows is None or len(rows) == 0:
        return False
    return bool(rows[:, 8:12].max() >= conf)


def compute_windows(hits: list[bool] | np.ndarray,
                    fps: float, dilate_s: float) -> list[tuple[int, int]]:
    """Dilate hit frames ±dilate and merge overlaps.

    hits: per-frame boolean (index = frame_idx in the *scan* timeline — with
    scan_stride > 1 pass the downsampled timeline plus fps/stride via caller).
    Returns half-open [start, end) frame ranges in the same timeline.
    """
    pad = max(0, round(dilate_s * fps))
    windows: list[list[int]] = []
    for i, hit in enumerate(hits):
        if not hit:
            continue
        s, e = max(0, i - pad), i + pad + 1
        if windows and s <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], e)
        else:
            windows.append([s, e])
    return [(s, e) for s, e in windows]


def sample_frames(windows: list[tuple[int, int]], fps: float,
                  sample_fps: float, max_frames: int) -> list[int]:
    """Uniform grid inside each window: start at window edge, step round(fps/sample_fps)."""
    step = max(1, round(fps / sample_fps))
    out: list[int] = []
    for s, e in windows:
        f = s
        while f < e and len(out) < max_frames:
            out.append(f)
            f += step
        if len(out) >= max_frames:
            break
    return out


# ---------------------------------------------------------------------------
# Brush hit-test

# Detector row layout (verified on sample images 2026-09-04): cols 0-7 are
# four corner points (x,y) in 640x384 pixels ordered TL→BL→BR→TR; cols 8-11
# sigmoid class scores; cols 12-20 a 9-way secondary label head.
MODEL_W, MODEL_H = 640.0, 384.0
MODEL_COLOR_LABELS = ("B", "R", "N", "P")
MODEL_BOARD_TYPE_LABELS = ("G", "1", "2", "3", "4", "5", "O", "Bs", "Bb")


def _corner_xy(rows: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """-> (xs, ys) 每行 4 角点的 x/y 数组（model 像素空间）。"""
    return rows[:, [0, 2, 4, 6]], rows[:, [1, 3, 5, 7]]


def row_score(row: np.ndarray) -> float:
    return float(np.max(row[8:12]))


def row_class_index(row: np.ndarray) -> int:
    """Map the 4-way prefix and 9-way board-type heads to one class."""
    prefix = int(np.argmax(row[8:12]))
    board_type = int(np.argmax(row[12:21]))
    return prefix * len(MODEL_BOARD_TYPE_LABELS) + board_type


def row_class_name(row: np.ndarray) -> str:
    """Return the semantic class name encoded by both detector heads."""
    color = MODEL_COLOR_LABELS[int(np.argmax(row[8:12]))]
    board_type = MODEL_BOARD_TYPE_LABELS[int(np.argmax(row[12:21]))]
    return f"{color}-{board_type}"


def row_box_norm(row: np.ndarray) -> tuple[float, float, float, float]:
    """4 角点的轴对齐外接框，归一化 center x/y + w/h。"""
    xs = row[[0, 2, 4, 6]]
    ys = row[[1, 3, 5, 7]]
    x1, x2 = float(xs.min()), float(xs.max())
    y1, y2 = float(ys.min()), float(ys.max())
    w = max(x2 - x1, 1e-6)
    h = max(y2 - y1, 1e-6)
    return ((x1 + w / 2) / MODEL_W,
            (y1 + h / 2) / MODEL_H,
            w / MODEL_W, h / MODEL_H)


def row_corners_norm(row: np.ndarray) -> list[list[float]]:
    """Return the detector's four corners in normalized image coordinates.

    Detector rows use model-pixel coordinates in TL → BL → BR → TR order.
    Keep that order so the canvas can draw the same quadrilateral that the
    model predicted instead of replacing it with its axis-aligned bbox.
    """
    pts = row[:8].reshape(4, 2).astype(np.float64)
    pts[:, 0] = np.clip(pts[:, 0] / MODEL_W, 0.0, 1.0)
    pts[:, 1] = np.clip(pts[:, 1] / MODEL_H, 0.0, 1.0)
    return pts.tolist()


def brush_hit(rows: np.ndarray,
              nx: float, ny: float, r_px: float,
              img_w: int, img_h: int) -> Optional[np.ndarray]:
    """Best-scoring row whose box_a center is inside the brush circle.

    nx, ny: normalized click position; r_px: radius in **image pixels**;
    img_w/img_h: the displayed image's pixel size. Box centers are compared
    in image pixel space so the circle stays round on any aspect ratio:
        dist_px = hypot((bx - nx) * img_w, (by - ny) * img_h) <= r_px
    The model's 640x384 pixel space cancels out (both sides normalized
    first), so no resize bookkeeping is needed here.
    """
    if rows is None or len(rows) == 0:
        return None
    xs, ys = _corner_xy(rows)
    bx = xs.mean(axis=1) / MODEL_W   # 4 角点质心 = 实例中心
    by = ys.mean(axis=1) / MODEL_H
    dx = (bx - nx) * img_w
    dy = (by - ny) * img_h
    inside = (dx * dx + dy * dy) <= r_px ** 2
    if not inside.any():
        return None
    idx = np.flatnonzero(inside)
    scores = rows[idx, 8:12].max(axis=1)
    return rows[idx[int(np.argmax(scores))]]
