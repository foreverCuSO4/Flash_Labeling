#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAMESPACE="flash-labeling"

command -v kubectl >/dev/null 2>&1 || {
  echo "kubectl is required" >&2
  exit 1
}

if [[ -z "${KUBECONFIG:-}" && ! -f "$HOME/.kube/config" ]]; then
  echo "a usable kubeconfig is required (set KUBECONFIG or create ~/.kube/config)" >&2
  exit 1
fi

kubectl apply -f "$SCRIPT_DIR/deploy/k8s/namespace.yaml"

if ! kubectl -n "$NAMESPACE" get secret flash-labeling-secrets >/dev/null 2>&1; then
  command -v openssl >/dev/null 2>&1 || {
    echo "openssl is required to create the application SECRET_KEY" >&2
    exit 1
  }
  kubectl -n "$NAMESPACE" create secret generic flash-labeling-secrets \
    --from-literal=SECRET_KEY="$(openssl rand -hex 32)"
fi

# clusterIP is immutable. Replace the old virtual-IP Service once so the
# headless Service in inference.yaml can be applied on this cluster.
infer_cluster_ip="$(kubectl -n "$NAMESPACE" get svc flash-labeling-infer \
  -o jsonpath='{.spec.clusterIP}' 2>/dev/null || true)"
if [[ -n "$infer_cluster_ip" && "$infer_cluster_ip" != "None" ]]; then
  kubectl -n "$NAMESPACE" delete svc flash-labeling-infer
fi

kubectl apply -k "$SCRIPT_DIR/deploy/k8s"

# This is a bare-metal single-node cluster. The bundled ingress-nginx
# service has no cloud LoadBalancer, so make its controller listen directly
# on the node's network and advertise the public address on port 80/443.
if kubectl -n ingress-nginx get deployment ingress-nginx-controller >/dev/null 2>&1; then
  kubectl -n ingress-nginx patch deployment ingress-nginx-controller \
    --type=merge \
    -p '{"spec":{"template":{"spec":{"hostNetwork":true,"dnsPolicy":"ClusterFirstWithHostNet"}}}}' >/dev/null
  kubectl -n ingress-nginx patch service ingress-nginx-controller \
    --type=merge \
    -p '{"spec":{"externalIPs":["60.165.239.63"]}}' >/dev/null
  kubectl -n ingress-nginx rollout status deployment/ingress-nginx-controller --timeout=5m
fi

# The image tag is intentionally stable and imagePullPolicy is Never; restart
# makes every invocation pick up the image imported by rebuild.sh.
kubectl -n "$NAMESPACE" rollout restart deployment/flash-labeling-infer
kubectl -n "$NAMESPACE" rollout restart deployment/flash-labeling-app

kubectl -n "$NAMESPACE" rollout status deployment/flash-labeling-infer --timeout=10m
kubectl -n "$NAMESPACE" rollout status deployment/flash-labeling-app --timeout=5m

echo
echo "Flash Labeling: http://60.165.239.63/flash_labeling/"
kubectl -n "$NAMESPACE" get pods,svc,ingress -o wide
