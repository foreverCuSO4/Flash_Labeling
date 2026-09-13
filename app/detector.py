"""Detector client + detection-result cache for auto-labeling.

Talks to the standalone inference service (see inference/service.py) over
plain HTTP — the web app never imports pyACL/CANN. If the service is down,
`DetectorUnavailable` is raised and the caller surfaces a clear error.

Cache layout: one compact file per extracted image,
    DATA_DIR/cache/det/<project_id>/<image_stored_name>.npz
holding {"rows": float32 (k,21), "conf_floor": float32 scalar,
"model": np.unicode_}. Rows are the service-side filtered hits
(0.05 / top-100); anything stricter is a numpy filter on top.
"""
from __future__ import annotations

import io
import json
import os
import struct
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from .config import DATA_DIR, DEFAULT_MODEL_TAG

INFER_URL = os.environ.get("INFER_URL", "http://127.0.0.1:8787")
CACHE_DIR = DATA_DIR / "cache" / "det"
MODEL_TAG = DEFAULT_MODEL_TAG
CONF_FLOOR = 0.05
MODEL_W, MODEL_H = 640, 384
_BATCH = 32
_TIMEOUT = 30


class DetectorUnavailable(RuntimeError):
    pass


def _unpack_rows_blob(blob: bytes) -> list[np.ndarray]:
    """Mirror of inference.pool._unpack_blob (kept dependency-free)."""
    out = []
    off = 0
    while off < len(blob):
        (cnt,) = struct.unpack_from("<H", blob, off)
        off += 2
        if cnt == 0:
            out.append(np.empty((0, 21), np.float32))
            continue
        rows = np.frombuffer(blob, np.float32, cnt * 21, off
                             ).reshape(cnt, 21).copy()
        off += cnt * 21 * 4
        out.append(rows)
    return out


def _model_tag(model_id: int | None) -> str:
    return MODEL_TAG if model_id is None else f"project-model:{int(model_id)}"


def _http_detect(frames_u8: np.ndarray, model_id: int | None = None) -> list[np.ndarray]:
    frames_u8 = np.ascontiguousarray(frames_u8, dtype=np.uint8)
    n = len(frames_u8)
    results: list[np.ndarray] = []
    for start in range(0, n, _BATCH):
        chunk = frames_u8[start:start + _BATCH]
        buf = io.BytesIO()
        np.savez(buf, frames=chunk)
        query = "" if model_id is None else f"?model_id={int(model_id)}"
        req = urllib.request.Request(
            f"{INFER_URL}/infer{query}", data=buf.getvalue(),
            headers={"Content-Type": "application/octet-stream"})
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                payload = json.loads(resp.read())
        except urllib.error.URLError as e:
            raise DetectorUnavailable(
                f"inference service not reachable at {INFER_URL}: {e}") from e
        dets = payload["detections"]
        if len(dets) != len(chunk):
            raise DetectorUnavailable(
                f"inference service returned {len(dets)} frames for "
                f"{len(chunk)} inputs")
        results.extend(_det_dicts_to_rows(dets))
    return results


def _det_dicts_to_rows(dets_per_frame: list[list[dict]]) -> list[np.ndarray]:
    """Service JSON detections -> (k, 21) rows (same column semantics as
    inference/postprocess.py: corners 4x2 | cls4 | aux9)."""
    out = []
    for dets in dets_per_frame:
        rows = []
        for d in dets:
            row = np.zeros(21, np.float32)
            for i, (xn, yn) in enumerate(d["corners"]):
                row[i * 2] = xn * MODEL_W
                row[i * 2 + 1] = yn * MODEL_H
            row[8:12] = d["cls_scores"]
            row[12:21] = d["aux_scores"]
            rows.append(row)
        out.append(np.array(rows, np.float32) if rows
                   else np.empty((0, 21), np.float32))
    return out


# ---------------------------------------------------------------------------
# Injectable detector handle (tests substitute a fake via set_detector_for_tests)

DetectFn = Callable[[np.ndarray], list[np.ndarray]]
_detect_impl: DetectFn = _http_detect


def detect_frames(frames_u8: np.ndarray, model_id: int | None = None) -> list[np.ndarray]:
    if model_id is None:
        return _detect_impl(frames_u8)
    return _http_detect(frames_u8, model_id)


def set_detector_for_tests(fn: Optional[DetectFn]) -> None:
    global _detect_impl
    _detect_impl = fn if fn is not None else _http_detect


# ---------------------------------------------------------------------------
# Cache

def cache_path(project_id: int, stored_name: str) -> Path:
    return CACHE_DIR / str(project_id) / f"{Path(stored_name).stem}.npz"


def write_cache(project_id: int, stored_name: str,
                rows: np.ndarray, model_id: int | None = None) -> Path:
    path = cache_path(project_id, stored_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = np.asarray(rows, dtype=np.float32).reshape(-1, 21)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as fh:
        np.savez(fh, rows=rows, conf_floor=np.float32(CONF_FLOOR),
                 model=np.array(_model_tag(model_id)))
    os.replace(tmp, path)
    return path


def read_cache(project_id: int, stored_name: str,
               model_id: int | None = None) -> Optional[np.ndarray]:
    path = cache_path(project_id, stored_name)
    if not path.exists():
        return None
    try:
        with np.load(path) as z:
            cached_model = str(z["model"].item()) if "model" in z else MODEL_TAG
            if cached_model != _model_tag(model_id):
                return None
            return np.asarray(z["rows"], dtype=np.float32).reshape(-1, 21)
    except Exception:
        return None


def delete_cache(project_id: int, stored_name: str) -> None:
    cache_path(project_id, stored_name).unlink(missing_ok=True)
