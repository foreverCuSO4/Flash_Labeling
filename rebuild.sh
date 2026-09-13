#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

APP_IMAGE="flash-labeling:local"
INFER_IMAGE="flash-labeling-infer:local"

command -v docker >/dev/null 2>&1 || {
  echo "docker is required" >&2
  exit 1
}

if [[ "$(id -u)" == "0" ]]; then
  CTR=(ctr)
else
  command -v sudo >/dev/null 2>&1 || {
    echo "sudo is required to import images into Kubernetes containerd" >&2
    exit 1
  }
  sudo -v || {
    echo "sudo permission is required for: sudo ctr -n k8s.io images import" >&2
    exit 1
  }
  CTR=(sudo ctr)
fi

"${CTR[@]}" version >/dev/null 2>&1 || {
  echo "ctr is required and must be able to access containerd" >&2
  exit 1
}

echo "[rebuild] building $APP_IMAGE"
docker build --no-cache --platform linux/arm64 -t "$APP_IMAGE" .

echo "[rebuild] building $INFER_IMAGE"
docker build --no-cache --platform linux/arm64 \
  -f Dockerfile.inference -t "$INFER_IMAGE" .

for image in "$APP_IMAGE" "$INFER_IMAGE"; do
  echo "[rebuild] importing $image into containerd k8s.io"
  docker save "$image" | "${CTR[@]}" -n k8s.io images import -
done

echo "[rebuild] images are ready for Kubernetes"
