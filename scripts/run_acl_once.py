"""一次性 ACL 推理 runner：读 npz 输入 -> 按 OM 的 batch 分块执行 -> 存 npz 输出。

供 verify_parity.py 以子进程方式调用（环境变量见 scripts/run_infer_server.sh）。
"""
import sys

import numpy as np

sys.path.insert(0, ".")
from inference.acl_engine import AclRuntime, OmModel


def main():
    om_path, in_npz, out_npz = sys.argv[1:4]
    frames = np.load(in_npz)["frames"].astype(np.float32)
    n = frames.shape[0]

    with AclRuntime(int(sys.argv[4]) if len(sys.argv) > 4 else 0):
        model = OmModel(om_path)
        batch = model.in_size // (384 * 640 * 3 * 4)

        outs = []
        for start in range(0, n, batch):
            chunk = frames[start:start + batch]
            if len(chunk) < batch:
                pad = np.repeat(chunk[-1:], batch - len(chunk), axis=0)
                chunk = np.concatenate([chunk, pad])
            out = model.execute(np.ascontiguousarray(chunk))[0]
            outs.append(out.reshape(batch, 5040, 21))
        model.close()

    out0 = np.concatenate(outs)[:n]
    np.savez(out_npz, output0=out0)
    print(f"ran {om_path}: {n} frames (batch={batch}) -> {out0.shape}")


if __name__ == "__main__":
    main()
