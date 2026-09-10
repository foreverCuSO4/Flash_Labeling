#!/usr/bin/env bash
# 启动高吞吐推理后端（单卡默认，占用由 INFER_REPLICAS 控制）
set -eo pipefail
cd "$(dirname "$0")/.."

export PYTHONPATH=${PYTHONPATH:-}
source /usr/local/Ascend/ascend-toolkit/set_env.sh
export PYTHONPATH=/usr/local/Ascend/ascend-toolkit/latest/pyACL/python/site-packages:$PYTHONPATH

: "${INFER_DEVICES:=0}"           # 默认只占 0 号卡
: "${INFER_REPLICAS:=4}"          # 每卡 worker 进程数（DMA/后处理与计算重叠）
: "${INFER_PORT:=8787}"
export INFER_DEVICES INFER_REPLICAS

echo "[run_infer_server] devices=$INFER_DEVICES replicas=$INFER_REPLICAS port=$INFER_PORT"
exec .venv/bin/python -m uvicorn inference.service:app \
    --host 0.0.0.0 --port "$INFER_PORT" --workers 1
