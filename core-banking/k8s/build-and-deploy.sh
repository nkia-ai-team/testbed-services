#!/bin/bash
# ============================================================
# core-banking K3s build + deploy script
# ============================================================
# Used on the target server by testbed-build orchestrator.
# Usage: cd core-banking && bash k8s/build-and-deploy.sh

set -euo pipefail
export DOCKER_BUILDKIT=0

SERVICES=("api" "account" "transfer" "ledger")
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo ""
echo "========================================="
echo "  Phase 1: Docker image build"
echo "========================================="
for svc in "${SERVICES[@]}"; do
  echo ""
  echo ">>> [build] core-banking-${svc}..."
  docker build --network=host -f "${PROJECT_ROOT}/${svc}-service/Dockerfile" \
    -t "core-banking-${svc}:latest" "${PROJECT_ROOT}"
  echo "<<< [done] core-banking-${svc}"
done

echo ""
echo "========================================="
echo "  Phase 2: cluster image import (k3d / native K3s 자동 감지)"
echo "========================================="
CTX_CLUSTER=$(kubectl config view --minify -o jsonpath='{.clusters[0].name}' 2>/dev/null || echo "")
if [[ "$CTX_CLUSTER" == k3d-* ]]; then
  K3D_NAME="${CTX_CLUSTER#k3d-}"
  echo "[detect] k3d cluster: $K3D_NAME"
  for svc in "${SERVICES[@]}"; do
    echo ">>> [k3d import] core-banking-${svc}..."
    k3d image import "core-banking-${svc}:latest" -c "$K3D_NAME"
  done
else
  echo "[detect] native K3s (cluster=$CTX_CLUSTER)"
  for svc in "${SERVICES[@]}"; do
    echo ">>> [k3s ctr import] core-banking-${svc}..."
    docker save "core-banking-${svc}:latest" | sudo k3s ctr images import -
  done
fi

echo ""
echo "========================================="
echo "  Phase 3: kubectl apply (envsubst 치환 포함)"
echo "========================================="
# 매니페스트의 placeholder 를 deploy-time 에 치환.
# OTLP_ENDPOINT / POLESTAR_ORG_ID 는 필수. *_TARGET_ID 는 등록 루프(§7)가 주입하며 미설정 시 빈 값으로 치환된다.
: "${OTLP_ENDPOINT:?OTLP_ENDPOINT 미설정 — ansible 또는 수동 export 필요. 예: export OTLP_ENDPOINT=http://192.168.230.104:6565}"
: "${POLESTAR_ORG_ID:?POLESTAR_ORG_ID 미설정 — ansible 또는 수동 export 필요. Polestar10 web 의 24자리 hex 조직 ID}"
export API_TARGET_ID="${API_TARGET_ID:-}"
export ACCOUNT_TARGET_ID="${ACCOUNT_TARGET_ID:-}"
export TRANSFER_TARGET_ID="${TRANSFER_TARGET_ID:-}"
export LEDGER_TARGET_ID="${LEDGER_TARGET_ID:-}"

# envsubst 화이트리스트로 명시 변수만 치환. K8s downward API 의 $(POD_NAME) 등과 충돌 회피.
for f in "${PROJECT_ROOT}/k8s/"*.yaml; do
  envsubst '${OTLP_ENDPOINT} ${POLESTAR_ORG_ID} ${API_TARGET_ID} ${ACCOUNT_TARGET_ID} ${TRANSFER_TARGET_ID} ${LEDGER_TARGET_ID}' < "$f" | kubectl apply -f -
done

echo ""
echo "========================================="
echo "  Phase 4: rollout status"
echo "========================================="
kubectl -n rca-testbed-banking rollout status statefulset/testbed-mariadb --timeout=180s || true
kubectl -n rca-testbed-banking rollout status deployment/testbed-redis --timeout=120s || true
for svc in "${SERVICES[@]}"; do
  kubectl -n rca-testbed-banking rollout status deployment/testbed-${svc} --timeout=180s || true
done
kubectl -n rca-testbed-banking rollout status deployment/testbed-nginx --timeout=120s || true

echo ""
echo "========================================="
echo "  Final state"
echo "========================================="
kubectl -n rca-testbed-banking get pods
echo ""
kubectl -n rca-testbed-banking get svc
