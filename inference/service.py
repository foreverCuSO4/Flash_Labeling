"""推理后端 HTTP 服务（独立进程，不依赖现有标注 app）。

接口：
  GET  /health  → {"status": "ok", "workers_alive": n}
  GET  /stats   → 池内状态快照
  POST /infer   → content-type: application/octet-stream，body 为 npz（含
                  'frames' uint8 [n,384,640,3]）；query:
                    raw=1   返回原始 output0 的 npz
                    conf=   score 阈值（默认 0.05；worker 侧下限 0.05，
                            更低的值按 0.05 算）
                    topk=   每帧保留条数（默认 100，上限 100）
                  默认返回 JSON {"detections": [[{...}], ...]}
  POST /infer_jpeg → multipart 图片列表（需 opencv），自动 resize 到
                  640×384，返回同上 JSON

启动：
  scripts/run_infer_server.sh
环境变量：
  INFER_DEVICES 逗号分隔卡号（默认 "0"）
  INFER_REPLICAS 每卡 worker 数（默认 4）
  INFER_OM_B16 / INFER_OM_B8 OM 路径
"""
import io
import os
import threading
from pathlib import Path

import anyio
import numpy as np
from fastapi import FastAPI, File, Request, Response
from fastapi.responses import JSONResponse

from inference.pool import InferPool
from inference.onnx_engine import OnnxModel, OnnxModelError
from inference.postprocess import rows_to_dicts

app = FastAPI(title="flash-infer")
_pool: InferPool | None = None
_onnx_models: dict[int, OnnxModel] = {}
_onnx_lock = threading.Lock()
_MODEL_DIR = Path(os.environ.get("INFER_MODEL_DIR", "/app/data/models"))


def get_pool() -> InferPool:
    global _pool
    if _pool is None:
        devices = [int(x) for x in
                   os.environ.get("INFER_DEVICES", "0").split(",")]
        _pool = InferPool(
            devices,
            os.environ.get("INFER_OM_B16", "data/om_models/model_b16.om"),
            os.environ.get("INFER_OM_B8", "data/om_models/model_b8.om"),
            window_ms=float(os.environ.get("INFER_WINDOW_MS", "5")),
            workers_per_device=int(os.environ.get("INFER_REPLICAS", "4")),
        )
    return _pool


def get_onnx_model(model_id: int) -> OnnxModel:
    if model_id <= 0:
        raise OnnxModelError("invalid model id")
    with _onnx_lock:
        model = _onnx_models.get(model_id)
        if model is None:
            path = _MODEL_DIR / f"{model_id}.onnx"
            if not path.is_file():
                raise OnnxModelError(f"model {model_id} is not available")
            model = OnnxModel(path)
            _onnx_models[model_id] = model
        return model


@app.on_event("startup")
def _startup():
    get_pool()


@app.on_event("shutdown")
def _shutdown():
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
    with _onnx_lock:
        _onnx_models.clear()


@app.get("/health")
def health():
    p = get_pool()
    snapshot = p.stats_snapshot()
    alive = snapshot["workers_alive"]
    expected = snapshot["workers_expected"]
    payload = {"status": "ok" if alive == expected else "degraded",
               "workers_alive": alive,
               "workers_expected": expected}
    if alive != expected:
        return JSONResponse(payload, status_code=503)
    return payload


@app.get("/stats")
def stats():
    return get_pool().stats_snapshot()


@app.post("/infer")
async def infer(request: Request, raw: int = 0,
                conf: float = 0.05, topk: int = 100, model_id: int = 0):
    blob = await request.body()
    return await anyio.to_thread.run_sync(_do_infer, blob, bool(raw),
                                          conf, topk, model_id)


def _do_infer(blob: bytes, raw: bool, conf: float, topk: int, model_id: int):
    """线程池里执行的阻塞部分：解 npz + 池调用 + JSON。"""
    frames = np.load(io.BytesIO(blob))["frames"]
    try:
        engine = get_onnx_model(model_id) if model_id else get_pool()
        if raw:
            out = engine.infer_raw(frames)
            buf = io.BytesIO()
            np.savez(buf, output0=out)
            return Response(buf.getvalue(), media_type="application/octet-stream")
        rows_list = engine.infer_rows(frames)
    except OnnxModelError as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    return JSONResponse({
        "detections": [rows_to_dicts(_refilter(rows, conf, topk))
                       for rows in rows_list]
    })


def _refilter(rows: np.ndarray, conf: float, topk: int) -> np.ndarray:
    """在 worker 侧 0.05/topk100 的结果集上按请求参数进一步过滤。"""
    if len(rows) == 0:
        return rows
    scores = rows[:, 8:12].max(axis=1)
    keep = scores >= conf
    return rows[keep][:topk]


@app.post("/infer_jpeg")
async def infer_jpeg(files: list[bytes] = File(...),
                     conf: float = 0.05, topk: int = 100, model_id: int = 0):
    return await anyio.to_thread.run_sync(_do_infer_jpeg, files, conf, topk,
                                          model_id)


def _do_infer_jpeg(files: list[bytes], conf: float, topk: int, model_id: int):
    try:
        import cv2
    except ImportError:
        return JSONResponse({"error": "opencv 未安装"}, status_code=501)
    frames = []
    for blob in files:
        arr = np.frombuffer(blob, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return JSONResponse({"error": "无法解码图片"}, status_code=400)
        img = cv2.resize(img, (640, 384))
        frames.append(img)
    try:
        engine = get_onnx_model(model_id) if model_id else get_pool()
        rows_list = engine.infer_rows(np.stack(frames))
    except OnnxModelError as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    return JSONResponse({
        "detections": [rows_to_dicts(_refilter(rows, conf, topk))
                       for rows in rows_list]
    })
