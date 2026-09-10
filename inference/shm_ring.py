"""共享内存槽位池：跨进程大 buffer 传输用。

slot 固定大小 max_batch * FRAME_BYTES（11.8MB @b16），生产者写入帧后把
slot 下标经控制消息发给 worker；worker 用完（含将原始结果写回同一 slot）
后把下标随结果消息带回，主进程回收字典序复用。

owner 进程创建并持有 all slots；worker 首次用时按名字 attach。
"""
import multiprocessing.shared_memory as shm
import threading

import numpy as np

H, W, C = 384, 640, 3


class ShmRing:
    def __init__(self, name_prefix: str, n_slots: int, max_batch: int):
        self.slot_bytes = max_batch * H * W * C
        self.max_batch = max_batch
        self.slots = [
            shm.SharedMemory(create=True, size=self.slot_bytes,
                             name=f"{name_prefix}_{i}")
            for i in range(n_slots)
        ]
        self.names = [s.name for s in self.slots]
        self._free = list(range(n_slots))
        self._lock = threading.Lock()

    def buffer(self, idx: int) -> np.ndarray:
        """主进程视图：uint8 [max_batch, H, W, C]"""
        return np.frombuffer(self.slots[idx].buf, dtype=np.uint8
                             ).reshape(self.max_batch, H, W, C)

    def acquire(self, timeout=None) -> int:
        with self._lock:
            assert self._free, "shm ring exhausted (背压应在调用方处理)"
            return self._free.pop()

    def release(self, idx: int):
        with self._lock:
            self._free.append(idx)

    def close(self):
        for s in self.slots:
            s.close()
            s.unlink()


class ShmRingClient:
    """worker 侧：按名字 attach，只读/写不 unlink。"""

    def __init__(self, names: list[str], max_batch: int):
        self._open: dict[int, shm.SharedMemory] = {}
        self.names = names
        self.max_batch = max_batch

    def buffer(self, idx: int) -> np.ndarray:
        sm = self._open.get(idx)
        if sm is None:
            sm = shm.SharedMemory(name=self.names[idx], create=False)
            self._open[idx] = sm
        return np.frombuffer(sm.buf, dtype=np.uint8).reshape(
            self.max_batch, H, W, C)

    def close(self):
        for sm in self._open.values():
            sm.close()
        self._open.clear()
