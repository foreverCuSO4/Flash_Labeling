"""worker 进程：绑定一张 NPU，攒批推理，帧数据走共享内存、结果走队列。

输入消息  (sid, slot_idx, n, decode)
  slot 内含 n 帧 uint8 [n,H,W,3]（槽容量 = max_batch）
输出消息  (sid, slot_idx, payload)
  slot_idx 恒由本进程带回，主进程据此回收 slot；
  decode=True : payload = 每帧 b"<u16 count>" + count 行 float32(21,) 的拼接
  decode=False: 原始结果 float32 [n,5040,21] 写回该 slot，payload=None
控制消息  ("STOP",)
攒批策略：取到首个请求后，在 window_ms 窗口内继续取，凑满 max_batch 或
窗口耗尽即执行；不足部分用最后一帧 pad，结果按各请求原样切回。
"""
import queue as q_mod
import struct
import time

import numpy as np

from inference.acl_engine import AclRuntime, OmModel
from inference.postprocess import filter_rows
from inference.shm_ring import H, W, C, ShmRingClient

FRAME_BYTES = H * W * C
OUT_DIMS = (5040, 21)


class DeviceWorker:
    def __init__(self, device_id: int, om_b16: str, om_b8: str | None,
                 max_batch: int, window_ms: float,
                 slot_names: list[str], conf_thresh: float, topk: int):
        self.device_id = device_id
        self.om_b16 = om_b16
        self.om_b8 = om_b8
        self.max_batch = max_batch
        self.window = window_ms / 1e3
        self.slot_names = slot_names
        self.conf_thresh = conf_thresh
        self.topk = topk
        self.models = {}
        self.ring = None

    def setup(self):
        self.rt = AclRuntime(self.device_id).init()
        self.models[16] = OmModel(self.om_b16)
        if self.om_b8:
            self.models[8] = OmModel(self.om_b8)
        self.stage = {
            b: np.empty((b, H, W, C), dtype=np.float32) for b in self.models
        }
        self.ring = ShmRingClient(self.slot_names, self.max_batch)

    def run(self, in_q, out_q):
        self.setup()
        print(f"[worker dev{self.device_id}] ready", flush=True)
        try:
            stopping = False
            while not stopping:
                first = in_q.get()
                if isinstance(first, str):
                    break
                batch = [first]
                n_frames = first[2]
                deadline = time.perf_counter() + self.window
                while n_frames < self.max_batch:
                    left = deadline - time.perf_counter()
                    if left <= 0:
                        break
                    try:
                        item = in_q.get(timeout=left)
                    except q_mod.Empty:
                        break
                    if isinstance(item, str):
                        stopping = True
                        break
                    batch.append(item)
                    n_frames += item[2]
                self._execute(batch, out_q)
        finally:
            for m in self.models.values():
                m.close()
            self.rt.close()
            if self.ring:
                self.ring.close()

    def _execute(self, batch, out_q):
        n = sum(it[2] for it in batch)
        size = 8 if (n <= 8 and 8 in self.models) else 16
        stage = self.stage[size]
        model = self.models[size]

        pos = 0
        for sid, slot_idx, k, decode in batch:
            slot_view = self.ring.buffer(slot_idx)
            stage[pos:pos + k] = slot_view[:k]
            pos += k
        if pos < size:
            stage[pos:] = stage[:1]  # pad

        out = model.execute(stage)[0].reshape(size, *OUT_DIMS)

        pos = 0
        for sid, slot_idx, k, decode in batch:
            frame_outs = out[pos:pos + k]
            pos += k
            if decode:
                parts = []
                for f in frame_outs:
                    rows = filter_rows(f, self.conf_thresh, self.topk)
                    parts.append(struct.pack("<H", len(rows)) + rows.tobytes())
                out_q.put((sid, slot_idx, b"".join(parts)))
            else:
                raw = np.ascontiguousarray(frame_outs)
                view = np.frombuffer(
                    self.ring.buffer(slot_idx).data, dtype=np.uint8)
                view[:raw.nbytes] = raw.view(np.uint8).ravel()
                out_q.put((sid, slot_idx, None))


def worker_main(device_id, om_b16, om_b8, max_batch, window_ms,
                slot_names, conf_thresh, topk, in_q, out_q):
    w = DeviceWorker(device_id, om_b16, om_b8, max_batch, window_ms,
                     slot_names, conf_thresh, topk)
    w.run(in_q, out_q)
