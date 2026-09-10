"""HTTP 服务冒烟：并发 npz 请求打 /infer，测聚合吞吐。

  .venv/bin/python scripts/http_smoke.py [--clients 4] [--frames 1024]
"""
import argparse
import io
import json
import threading
import time
import urllib.request

import numpy as np

URL = "http://127.0.0.1:8787/infer"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clients", type=int, default=4)
    ap.add_argument("--frames", type=int, default=1024)
    ap.add_argument("--chunk", type=int, default=64)
    args = ap.parse_args()

    rng = np.random.default_rng(3)
    proto = rng.integers(0, 255, (args.chunk, 384, 640, 3), np.uint8)
    errors = []
    iters = max(1, args.frames // (args.clients * args.chunk))

    def client(seed):
        buf = io.BytesIO()
        np.savez(buf, frames=proto)
        body = buf.getvalue()
        for _ in range(iters):
            req = urllib.request.Request(URL, data=body,
                headers={"Content-Type": "application/octet-stream"})
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = json.loads(resp.read())
                assert len(data["detections"]) == args.chunk
            except Exception as e:  # noqa: BLE001
                errors.append(e)
                return

    threads = [threading.Thread(target=client, args=(i,))
               for i in range(args.clients)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    dt = time.perf_counter() - t0
    total = iters * args.chunk * args.clients
    print(f"HTTP clients={args.clients} total={total} in {dt:.2f}s "
          f"-> {total / dt:.0f} fps errors={len(errors)}")
    if errors:
        raise SystemExit(errors[0])


if __name__ == "__main__":
    main()
