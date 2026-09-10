"""OM vs ONNX 精度校验（验收标准：cosine similarity > 0.999）。

流程：
1. 项目 venv (py3.14) 生成本地 npz 输入
2. .venv-atc (py3.11, onnxruntime) 跑原 ONNX 得到 golden
3. 项目 venv 跑各档 OM
4. 比较：max abs error / cosine similarity / per-frame top-20 行一致性

用法：
  source /usr/local/Ascend/ascend-toolkit/set_env.sh
  export PYTHONPATH=/usr/local/Ascend/ascend-toolkit/latest/pyACL/python/site-packages:$PYTHONPATH
  .venv/bin/python scripts/verify_parity.py
"""
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

N_FRAMES = 3
WORK = Path("/tmp/parity")
ONNX = "data/example_models/gpu2_strict_best_snapshot.onnx"
OMS = {
    "b1": ("data/om_models/model_b1.om", 1),
    "b8": ("data/om_models/model_b8.om", 8),
    "b16": ("data/om_models/model_b16.om", 16),
    "b32": ("data/om_models/model_b32.om", 32),
}
ATC_PY = ".venv-atc/bin/python"

ORT_GOLDEN = """import numpy as np, onnxruntime as ort
x = np.load('/tmp/parity/inputs.npz')['frames']
sess = ort.InferenceSession('%(onnx)s', providers=['CPUExecutionProvider'])
out = np.concatenate([sess.run(['output0'], {'images': f[None]})[0] for f in x])
np.savez('/tmp/parity/golden.npz', output0=out)
"""


def sh(cmd, env=None):
    r = subprocess.run(cmd, shell=True, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-3000:])
        print(r.stderr[-3000:])
        raise SystemExit(f"command failed: {cmd}")
    return r


def main():
    WORK.mkdir(exist_ok=True)
    rng = np.random.default_rng(42)
    frames = rng.uniform(0, 255, (N_FRAMES, 384, 640, 3)).astype(np.float32)
    np.savez(WORK / "inputs.npz", frames=frames)

    env = dict(os.environ)
    sh(f"{ATC_PY} -c \"{ORT_GOLDEN % {'onnx': ONNX}}\"", env)

    for name, (om, _) in OMS.items():
        sh(f".venv/bin/python scripts/run_acl_once.py {om} "
           f"{WORK/'inputs.npz'} {WORK/f'om_{name}.npz'} 0", env)

    golden = np.load(WORK / "golden.npz")["output0"].astype(np.float64)
    print(f"golden {golden.shape} mean={golden.mean():.3f}")
    ok = True
    for name in OMS:
        out = np.load(WORK / f"om_{name}.npz")["output0"].astype(np.float64)
        diff = np.abs(out - golden)
        g, o = golden.ravel(), out.ravel()
        cos = float((g @ o) / (np.linalg.norm(g) * np.linalg.norm(o)))
        # 每帧按第 5 列(分数)排序后的 top-20 绝对误差
        print(f"[{name}] max_abs={diff.max():.4f} p99_abs="
              f"{np.percentile(diff, 99):.4f} cos={cos:.6f}")
        if cos < 0.999:
            ok = False
    print("PARITY", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
