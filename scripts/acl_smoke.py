"""最小 pyACL 冒烟：加载 OM 跑一次推理。

用项目 venv (python3.14) + pyACL site-packages 直接跑，验证 py3.14 全流程可用。
用法见 scripts/run_infer_server.sh 的环境变量设置。
"""
import sys
import time

import numpy as np

import acl

OM = sys.argv[1] if len(sys.argv) > 1 else "data/om_models/model_b1.om"
BATCH = int(sys.argv[2]) if len(sys.argv) > 2 else 1


def check(ret, what):
    if ret != 0:
        raise RuntimeError(f"{what} failed: {ret}")


def main():
    check(acl.init(), "acl.init")
    check(acl.rt.set_device(0), "set_device")
    ctx, ret = acl.rt.create_context(0)
    check(ret, "create_context")

    model_id, ret = acl.mdl.load_from_file(OM)
    check(ret, "load_from_file")
    desc = acl.mdl.create_desc()
    check(acl.mdl.get_desc(desc, model_id), "get_desc")

    n_in = acl.mdl.get_num_inputs(desc)
    n_out = acl.mdl.get_num_outputs(desc)
    in_size = acl.mdl.get_input_size_by_index(desc, 0)
    print(f"inputs={n_in} outputs={n_out} input_bytes={in_size}")

    x = np.random.rand(BATCH, 384, 640, 3).astype(np.float32)
    assert x.nbytes == in_size, (x.nbytes, in_size)

    dev_in, ret = acl.rt.malloc(in_size, 2)  # ACL_MEM_MALLOC_NORMAL_ONLY
    check(ret, "malloc in")
    check(acl.rt.memcpy(dev_in, in_size, x.ctypes.data, x.nbytes, 1), "memcpy h2d")

    in_ds = acl.mdl.create_dataset()
    buf = acl.create_data_buffer(dev_in, in_size)
    _, ret = acl.mdl.add_dataset_buffer(in_ds, buf)
    check(ret, "add in buffer")

    out_ds = acl.mdl.create_dataset()
    out_meta = []
    for i in range(n_out):
        sz = acl.mdl.get_output_size_by_index(desc, i)
        dev, ret = acl.rt.malloc(sz, 2)
        check(ret, f"malloc out {i}")
        b = acl.create_data_buffer(dev, sz)
        _, ret = acl.mdl.add_dataset_buffer(out_ds, b)
        check(ret, f"add out buffer {i}")
        out_meta.append((dev, sz))

    t0 = time.perf_counter()
    check(acl.mdl.execute(model_id, in_ds, out_ds), "execute")
    t1 = time.perf_counter()
    print(f"first execute: {(t1 - t0) * 1e3:.1f} ms")

    n_loop = 50
    t0 = time.perf_counter()
    for _ in range(n_loop):
        check(acl.mdl.execute(model_id, in_ds, out_ds), "execute loop")
    t1 = time.perf_counter()
    print(f"steady-state: {(t1 - t0) / n_loop * 1e3:.2f} ms/iter "
          f"({BATCH * n_loop / (t1 - t0):.0f} fps @batch{BATCH})")

    dev, sz = out_meta[0]
    host = np.empty(sz // 4, dtype=np.float32)
    check(acl.rt.memcpy(host.ctypes.data, sz, dev, sz, 2), "memcpy d2h")
    host = host.reshape(BATCH, 5040, 21)
    print("output stats: mean=%.4f std=%.4f min=%.4f max=%.4f" %
          (host.mean(), host.std(), host.min(), host.max()))

    check(acl.mdl.unload(model_id), "unload")
    check(acl.rt.destroy_context(ctx), "destroy_context")
    check(acl.rt.reset_device(0), "reset_device")
    check(acl.finalize(), "finalize")
    print("SMOKE OK")


if __name__ == "__main__":
    main()
