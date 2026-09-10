"""推理池吞吐基准：合成帧压测 8 卡聚合 fps、批次档位对比、延迟分位数。

用法（需先 source set_env.sh + 设 PYTHONPATH，见 scripts/run_infer_server.sh）：
  .venv/bin/python scripts/bench_inference.py [--devices 0,1,2,3,4,5,6,7]
                                               [--frames 20000]
                                               [--clients 16]
"""
import argparse
import threading
import time

import numpy as np

from inference.pool import InferPool


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--devices", default="0")
    ap.add_argument("--frames", type=int, default=20000)
    ap.add_argument("--clients", type=int, default=16)
    ap.add_argument("--chunk", type=int, default=256,
                    help="每次 submit 的帧数")
    ap.add_argument("--window-ms", type=float, default=5.0)
    ap.add_argument("--replicas", type=int, default=1,
                    help="每张卡开几个 worker 进程")
    args = ap.parse_args()

    devices = [int(x) for x in args.devices.split(",")]
    rng = np.random.default_rng(0)
    proto = rng.integers(0, 255, (args.chunk, 384, 640, 3), np.uint8)

    lat = []
    lock = threading.Lock()
    errors = []

    with InferPool(devices, "data/om_models/model_b16.om",
                   "data/om_models/model_b8.om",
                   max_batch=16, window_ms=args.window_ms,
                   workers_per_device=args.replicas) as pool:
        # 预热
        pool.infer_rows(proto)
        t_start = time.perf_counter()

        def worker(seed):
            buf = proto.copy()
            iters = args.frames // (args.clients * args.chunk)
            for it in range(iters):
                t0 = time.perf_counter()
                try:
                    out = pool.infer_rows(buf)
                    with lock:
                        lat.append(time.perf_counter() - t0)
                except Exception as e:  # noqa: BLE001
                    errors.append(e)
                    return
                if seed == 0 and it % 10 == 0:
                    done = it * args.chunk * args.clients
                    el = time.perf_counter() - t_start
                    print(f"[progress] {done} frames, "
                          f"{done / el:.0f} fps", flush=True)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(args.clients)]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.perf_counter() - t0

    total = (args.frames // (args.clients * args.chunk)
             * args.chunk * args.clients)
    lat = np.array(lat) * 1e3
    print(f"\n==== benchmark ====")
    print(f"devices={len(devices)} clients={args.clients} "
          f"window={args.window_ms}ms errors={len(errors)}")
    print(f"total frames : {total}")
    print(f"wall time    : {elapsed:.2f}s")
    print(f"throughput   : {total / elapsed:.0f} fps "
          f"({total / elapsed / len(devices):.0f} fps/device)")
    if len(lat):
        print(f"latency/chunk: p50={np.percentile(lat, 50):.1f}ms "
              f"p95={np.percentile(lat, 95):.1f}ms "
              f"p99={np.percentile(lat, 99):.1f}ms")
    if errors:
        raise SystemExit(f"bench had {len(errors)} errors: {errors[0]!r}")


if __name__ == "__main__":
    main()
