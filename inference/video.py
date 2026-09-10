"""视频解码 → 批量喂推理池的最小路径（opencv 软解，预留抽帧）。

先解码攒批，批满即 submit（不阻塞等待），in-flight submit 数封顶，
实现解码与推理全流水。

用法：scripts/bench_video.py
"""
import time

import numpy as np

from inference.pool import InferPool, _unpack_blob


def scan_video(video_path: str, pool: InferPool,
               fps_sample: float | None = None,
               resize: tuple[int, int] = (640, 384),
               chunk: int = 64,
               max_inflight: int = 8) -> tuple[list[np.ndarray], dict]:
    """逐帧/抽帧 decode → rows。返回 (每采样帧 rows 列表, 统计)。"""
    import cv2

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"cannot open video: {video_path}")
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 0
    n_total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    step = 1
    if fps_sample and src_fps and fps_sample < src_fps:
        step = max(1, round(src_fps / fps_sample))

    inflight: list[tuple[int, object]] = []
    results: dict[int, list[np.ndarray]] = {}
    chunk_no = 0
    decoded = 0
    frame_idx = 0
    t0 = time.perf_counter()

    def drain():
        idx, fut = inflight.pop(0)
        blob_parts = fut.result()
        results[idx] = [r for b in blob_parts for r in _unpack_blob(b)]

    buf = []
    while True:
        ok, img = cap.read()
        if not ok:
            break
        if frame_idx % step:
            frame_idx += 1
            continue
        if img.shape[:2] != (resize[1], resize[0]):
            img = cv2.resize(img, resize)
        buf.append(img)
        decoded += 1
        frame_idx += 1
        if len(buf) == chunk:
            inflight.append((chunk_no, pool.submit_async(np.stack(buf))))
            chunk_no += 1
            buf = []
            while len(inflight) >= max_inflight:
                drain()
    if buf:
        inflight.append((chunk_no, pool.submit_async(np.stack(buf))))
    while inflight:
        drain()
    cap.release()
    dt = time.perf_counter() - t0

    ordered = [rows for i in sorted(results) for rows in results[i]]
    info = {
        "src_fps": src_fps, "total_frames": n_total,
        "sampled_step": step, "frames_feed": decoded,
        "seconds": dt, "feed_fps": decoded / dt,
    }
    return ordered, info
