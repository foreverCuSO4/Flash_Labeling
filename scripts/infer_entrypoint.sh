#!/bin/sh
set -e

# Older images used multiprocessing semaphores. Remove only those stale
# files inside this container before starting a new worker pool.
if [ -d /dev/shm ]; then
    find /dev/shm -maxdepth 1 -type f -name 'sem.mp-*' -delete 2>/dev/null || true
fi

. /usr/local/Ascend/ascend-toolkit/set_env.sh
exec uvicorn inference.service:app \
    --host 0.0.0.0 \
    --port "${INFER_PORT:-8787}" \
    --workers 1
