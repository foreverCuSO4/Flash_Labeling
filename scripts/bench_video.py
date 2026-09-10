"""视频端到端基准：合成一段 mp4 → scan_video 全链路计时。

  .venv/bin/python scripts/bench_video.py [--video /path/x.mp4] [--seconds 60]
"""
import argparse
import os
import tempfile

import numpy as np

from inference.pool import InferPool
from inference.video import scan_video


def make_synthetic_video(path: str, seconds: int = 60, fps: int = 30):
    import cv2
    w, h = 1280, 720
    out = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    rng = np.random.default_rng(7)
    n = seconds * fps
    for i in range(n):
        frame = rng.integers(0, 255, (h, w, 3), np.uint8)
        # 画几个移动的方块，模拟有内容
        for k in range(5):
            x = (i * 3 + k * 200) % (w - 60)
            y = (k * 137) % (h - 60)
            frame[y:y + 60, x:x + 60] = (k * 50 % 255,)
        out.write(frame)
    out.release()
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video")
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--replicas", type=int, default=4)
    ap.add_argument("--fps-sample", type=float, default=None)
    args = ap.parse_args()

    path = args.video
    tmp = None
    if not path:
        tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        path = tmp.name
        tmp.close()
        n = make_synthetic_video(path, args.seconds)
        print(f"synthetic video: {path} ({args.seconds}s × 30fps = {n} frames)")

    with InferPool([0], "data/om_models/model_b16.om",
                   "data/om_models/model_b8.om",
                   workers_per_device=args.replicas) as pool:
        pool.infer_rows(np.zeros((16, 384, 640, 3), np.uint8))  # 预热
        rows_all, info = scan_video(path, pool, fps_sample=args.fps_sample)

    print(f"video: {info['total_frames']} frames @ {info['src_fps']:.1f}fps, "
          f"step={info['sampled_step']}")
    print(f"feed {info['frames_feed']} frames in {info['seconds']:.2f}s "
          f"-> {info['feed_fps']:.0f} fps end-to-end "
          f"(decode+scale+inference+filter)")
    nonempty = sum(1 for r in rows_all if len(r))
    print(f"frames with detections: {nonempty}/{len(rows_all)}")

    if tmp is not None:
        os.unlink(path)


if __name__ == "__main__":
    main()
