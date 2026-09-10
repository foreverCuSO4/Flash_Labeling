"""pyACL 推理引擎封装。

用法约定：
- 一个进程只调用一次 `AclRuntime.init()`，退出前 `close()`（worker.py 里做）
- 每个 OmModel 绑定当前进程的 device/context，负责自己的 device buffer 生命周期
- execute() 为同步调用；批量输入按 OM 的固定 batch 整批喂入，不足时在调用方 pad
"""
import os
import threading
import time

import numpy as np

import acl

ACL_MEM_MALLOC_NORMAL_ONLY = 2
ACL_MEMCPY_HOST_TO_DEVICE = 1
ACL_MEMCPY_DEVICE_TO_HOST = 2


class AclError(RuntimeError):
    pass


def check(ret, what):
    if ret != 0:
        raise AclError(f"{what} failed, ret={ret}")


class AclRuntime:
    """进程级 ACL 运行时（init / set_device / context / finalize）。"""

    _lock = threading.Lock()
    _initialized = 0

    def __init__(self, device_id: int = 0):
        self.device_id = device_id
        self.ctx = None

    def init(self):
        with AclRuntime._lock:
            if AclRuntime._initialized == 0:
                config = os.environ.get("ACL_CONFIG_PATH")
                check(acl.init(config) if config else acl.init(), "acl.init")
            AclRuntime._initialized += 1
        check(acl.rt.set_device(self.device_id), "set_device")
        self.ctx, ret = acl.rt.create_context(self.device_id)
        check(ret, "create_context")
        return self

    def close(self):
        if self.ctx is not None:
            check(acl.rt.destroy_context(self.ctx), "destroy_context")
            self.ctx = None
        check(acl.rt.reset_device(self.device_id), "reset_device")
        with AclRuntime._lock:
            AclRuntime._initialized -= 1
            if AclRuntime._initialized == 0:
                check(acl.finalize(), "acl.finalize")

    def __enter__(self):
        return self.init()

    def __exit__(self, *exc):
        self.close()


class OmModel:
    """一个加载好的 OM：固定 batch 输入 NHWC float32，输出原样拷回主机。

    in_shape: 逻辑输入形状 (B, H, W, 3)；model 内部自带 /255、BGR 调整等预处理。
    """

    def __init__(self, om_path: str):
        self.om_path = om_path
        self.model_id, ret = acl.mdl.load_from_file(om_path)
        check(ret, f"load_from_file {om_path}")
        self.desc = acl.mdl.create_desc()
        check(acl.mdl.get_desc(self.desc, self.model_id), "get_desc")

        self.in_size = acl.mdl.get_input_size_by_index(self.desc, 0)
        self.out_sizes = [
            acl.mdl.get_output_size_by_index(self.desc, i)
            for i in range(acl.mdl.get_num_outputs(self.desc))
        ]

        self.dev_in, ret = acl.rt.malloc(self.in_size, ACL_MEM_MALLOC_NORMAL_ONLY)
        check(ret, "malloc input")
        self.in_ds = acl.mdl.create_dataset()
        buf = acl.create_data_buffer(self.dev_in, self.in_size)
        _, ret = acl.mdl.add_dataset_buffer(self.in_ds, buf)
        check(ret, "add input buffer")

        self.out_ds = acl.mdl.create_dataset()
        self.dev_outs = []
        for sz in self.out_sizes:
            dev, ret = acl.rt.malloc(sz, ACL_MEM_MALLOC_NORMAL_ONLY)
            check(ret, "malloc output")
            b = acl.create_data_buffer(dev, sz)
            _, ret = acl.mdl.add_dataset_buffer(self.out_ds, b)
            check(ret, "add output buffer")
            self.dev_outs.append(dev)

        self.stats = {"execs": 0, "frames": 0, "exec_sec": 0.0}

    @property
    def input_nbytes(self):
        return self.in_size

    def execute(self, frames: np.ndarray) -> list[np.ndarray]:
        """frames: 连续 float32，大小等于 in_size（pad 由调用方负责）。"""
        frames = np.ascontiguousarray(frames, dtype=np.float32)
        assert frames.nbytes == self.in_size, (frames.nbytes, self.in_size)
        check(acl.rt.memcpy(self.dev_in, self.in_size, frames.ctypes.data,
                            frames.nbytes, ACL_MEMCPY_HOST_TO_DEVICE),
              "memcpy h2d")
        t0 = time.perf_counter()
        check(acl.mdl.execute(self.model_id, self.in_ds, self.out_ds),
              "mdl.execute")
        dt = time.perf_counter() - t0

        outs = []
        for dev, sz in zip(self.dev_outs, self.out_sizes):
            host = np.empty(sz // 4, dtype=np.float32)
            check(acl.rt.memcpy(host.ctypes.data, sz, dev, sz,
                                ACL_MEMCPY_DEVICE_TO_HOST),
                  "memcpy d2h")
            outs.append(host)
        self.stats["execs"] += 1
        self.stats["exec_sec"] += dt
        return outs

    def close(self):
        for dev in self.dev_outs:
            acl.rt.free(dev)
        self.dev_outs = []
        if self.dev_in:
            acl.rt.free(self.dev_in)
            self.dev_in = None
        acl.mdl.unload(self.model_id)
