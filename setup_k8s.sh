#!/usr/bin/env bash
set -euo pipefail

# One-time setup for a normal user to use the cluster already installed on
# this server. It copies the administrator kubeconfig without exposing it in
# the repository or changing the cluster.
if [[ "$(id -u)" == "0" ]]; then
  SUDO=()
else
  command -v sudo >/dev/null 2>&1 || {
    echo "sudo is required" >&2
    exit 1
  }
  sudo -v
  SUDO=(sudo)
fi

ADMIN_CONFIG=/etc/kubernetes/admin.conf
[[ -f "$ADMIN_CONFIG" ]] || {
  echo "Kubernetes admin config not found at $ADMIN_CONFIG" >&2
  exit 1
}

mkdir -p "$HOME/.kube"
chmod 700 "$HOME/.kube"
"${SUDO[@]}" install -m 600 \
  -o "$(id -u)" -g "$(id -g)" \
  "$ADMIN_CONFIG" "$HOME/.kube/config"

echo "Kubernetes access is ready for $USER"
echo "Run ./rebuild.sh, then ./start.sh"
