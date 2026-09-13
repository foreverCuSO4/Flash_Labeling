"""多卡推理池：帧数据过共享内存，控制/结果消息过 pipe。

- submit 把帧拷贝进 shm slot，只把 (sid, slot, n, decode) 控制消息入队
- worker 攒批执行后返回 (sid, slot, payload)
    decode=True  : payload = 每帧 b"<u16 count>" + count 行 float32(21,) 拼接
    decode=False : payload = None，原始输出 float32 已写回该 slot，由
                   collector 线程读出（numpy 拷贝）后才回收 slot
- 背压：slot 有限，acquire 阻塞即限流
"""
import itertools
import math
import multiprocessing as mp
import os
import struct
import threading
from concurrent.futures import Future
from multiprocessing.connection import wait as wait_connections

import numpy as np

from inference.shm_ring import ShmRing
from inference.worker import H, W, C, OUT_DIMS, worker_main

STOP = "STOP"


class InferPool:
    def __init__(self, devices, om_b16, om_b8=None, max_batch=16,
                 window_ms=5.0, conf_thresh=0.05, topk=100,
                 slots_per_device=4, workers_per_device=1):
        ctx = mp.get_context("spawn")
        self.max_batch = max_batch
        n_workers = len(devices) * workers_per_device
        if n_workers <= 0:
            raise ValueError("at least one inference worker is required")
        self.ring = ShmRing(f"flash_infer_{os.getpid()}",
                            n_workers * slots_per_device, max_batch)
        self._pending = {}
        self._pending_raw_k = {}
        self._pending_lock = threading.Lock()
        self._dispatch_lock = threading.Lock()
        self._slot_sem = threading.BoundedSemaphore(len(self.ring.names))
        self._req_ids = itertools.count()
        self._dispatch = itertools.cycle(range(n_workers))
        self._closed = False
        self.stats = {"submitted_frames": 0}

        # Queue/Lock relies on POSIX semaphores. A dedicated pipe per worker
        # avoids that kernel resource entirely while preserving batching.
        self._in_senders = []
        self._out_receivers = []
        self.workers = []
        for d in devices:
            for r in range(workers_per_device):
                in_receiver, in_sender = ctx.Pipe(duplex=False)
                out_receiver, out_sender = ctx.Pipe(duplex=False)
                p = ctx.Process(
                    target=worker_main,
                    args=(d, om_b16, om_b8, max_batch, window_ms,
                          self.ring.names, conf_thresh, topk,
                          in_receiver, out_sender),
                    daemon=True, name=f"acl-worker-dev{d}-{r}")
                p.start()
                # The child owns these ends after spawn.
                in_receiver.close()
                out_sender.close()
                self._in_senders.append(in_sender)
                self._out_receivers.append(out_receiver)
                self.workers.append(p)

        self._collector = threading.Thread(target=self._collect,
                                           daemon=True, name="acl-collector")
        self._collector.start()

    # ---- 内部 ----

    def _collect(self):
        active = list(self._out_receivers)
        while active:
            try:
                ready = wait_connections(active, timeout=1.0)
            except (EOFError, OSError):
                return
            for conn in ready:
                try:
                    message = conn.recv()
                except (EOFError, OSError):
                    active.remove(conn)
                    conn.close()
                    continue
                # Workers send a sentinel after releasing their model and
                # shared-memory handles during graceful shutdown.
                if message is None:
                    active.remove(conn)
                    conn.close()
                    continue
                sid, slot, payload = message
                with self._pending_lock:
                    fut = self._pending.pop(sid, None)
                    k = self._pending_raw_k.pop(sid, None)
                if payload is None:
                    # raw 模式：先从 slot 拷贝，再回收
                    nbytes = k * int(np.prod(OUT_DIMS)) * 4
                    view = np.frombuffer(self.ring.buffer(slot).data,
                                         dtype=np.uint8)[:nbytes]
                    result = view.view(np.float32).reshape(k, *OUT_DIMS).copy()
                else:
                    result = payload
                self.ring.release(slot)
                self._slot_sem.release()
                if fut is not None:
                    fut.set_result(result)

    def _acquire_slot(self) -> int:
        self._slot_sem.acquire()
        return self.ring.acquire()

    # ---- 对外 ----

    def submit_async(self, frames_u8: np.ndarray, decode=True) -> Future:
        """frames_u8: uint8 [n,H,W,3]。
        decode=True  : result() = list[bytes]，每 parts 一个 blob
        decode=False : result() = np.ndarray [n,5040,21] float32
        """
        frames_u8 = np.ascontiguousarray(frames_u8, dtype=np.uint8)
        assert frames_u8.ndim == 4 and frames_u8.shape[1:] == (H, W, C)
        n = len(frames_u8)
        top = Future()
        if n == 0:
            top.set_result([] if decode
                           else np.empty((0, *OUT_DIMS), np.float32))
            return top

        req_id = next(self._req_ids)
        n_parts = math.ceil(n / self.max_batch)
        state = {"left": n_parts, "bufs": [None] * n_parts,
                 "lock": threading.Lock()}

        def make_cb(i, decode):
            def cb(f):
                with state["lock"]:
                    state["bufs"][i] = f.result()
                    state["left"] -= 1
                    if state["left"] == 0:
                        if decode:
                            top.set_result(state["bufs"])
                        else:
                            top.set_result(np.concatenate(state["bufs"]))
            return cb

        for i, start in enumerate(range(0, n, self.max_batch)):
            chunk = frames_u8[start:start + self.max_batch]
            k = len(chunk)
            sid = (req_id, i)
            slot = self._acquire_slot()
            self.ring.buffer(slot)[:k] = chunk
            pf = Future()
            pf.add_done_callback(make_cb(i, decode))
            with self._pending_lock:
                self._pending[sid] = pf
                if not decode:
                    self._pending_raw_k[sid] = k
            with self._dispatch_lock:
                worker_idx = next(self._dispatch)
                self._in_senders[worker_idx].send((sid, slot, k, decode))

        self.stats["submitted_frames"] += n
        return top

    # ---- 便捷接口 ----

    def infer_raw(self, frames_u8: np.ndarray, timeout=None) -> np.ndarray:
        """原始输出 [n, 5040, 21] float32。"""
        return self.submit_async(frames_u8, decode=False).result(timeout)

    def infer_rows(self, frames_u8: np.ndarray,
                   timeout=None) -> list[np.ndarray]:
        """decode 模式：每帧 (k, 21) float32 行（score 过滤后）。"""
        parts = self.submit_async(frames_u8, decode=True).result(timeout)
        return [rows for blob in parts for rows in _unpack_blob(blob)]

    def stats_snapshot(self):
        with self._pending_lock:
            pending = len(self._pending)
        return {
            "pending_requests": pending,
            "submitted_frames": self.stats["submitted_frames"],
            "workers_alive": sum(p.is_alive() for p in self.workers),
            "workers_expected": len(self.workers),
        }

    def close(self):
        if self._closed:
            return
        self._closed = True
        for conn in self._in_senders:
            try:
                conn.send(STOP)
            except (BrokenPipeError, EOFError, OSError):
                pass
        for p in self.workers:
            p.join(timeout=5)
            if p.is_alive():
                p.terminate()
                p.join(timeout=2)
        for conn in self._in_senders + self._out_receivers:
            conn.close()
        self.ring.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _unpack_blob(blob: bytes) -> list[np.ndarray]:
    out = []
    off = 0
    while off < len(blob):
        (cnt,) = struct.unpack_from("<H", blob, off)
        off += 2
        if cnt == 0:
            out.append(np.empty((0, 21), np.float32))
            continue
        rows = np.frombuffer(blob, np.float32, cnt * 21, off
                             ).reshape(cnt, 21).copy()
        off += cnt * 21 * 4
        out.append(rows)
    return out
