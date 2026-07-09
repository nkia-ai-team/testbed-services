#!/bin/bash
# ============================================================
# food-delivery K3s build + deploy script
# ============================================================
# Used on the target server (ARM64 / aarch64) by testbed-build orchestrator.
# Usage: cd food-delivery && bash k8s/build-and-deploy.sh

set -euo pipefail
export DOCKER_BUILDKIT=0

SERVICES=("order" "restaurant" "dispatch" "payment" "notify")
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo ""
echo "========================================="
echo "  Phase 1: Docker image build"
echo "========================================="
for svc in "${SERVICES[@]}"; do
  echo ""
  echo ">>> [build] food-delivery-${svc}..."
  docker build --network=host -f "${PROJECT_ROOT}/${svc}-service/Dockerfile" \
    -t "food-delivery-${svc}:latest" "${PROJECT_ROOT}"
  echo "<<< [done] food-delivery-${svc}"
done

echo ""
echo "========================================="
echo "  Phase 2: cluster image import (k3d / native K3s 자동 감지)"
echo "========================================="
# kubectl 의 현재 context cluster 이름이 'k3d-<name>' 으로 시작하면 k3d.
# k3d 노드의 containerd 는 호스트 docker 와 분리되어 있어 `k3d image import` 로 명시 주입 필요.
# 그 외 (native K3s / 기타) 는 k3s containerd 로 import.
CTX_CLUSTER=$(kubectl config view --minify -o jsonpath='{.clusters[0].name}' 2>/dev/null || echo "")
if [[ "$CTX_CLUSTER" == k3d-* ]]; then
  K3D_NAME="${CTX_CLUSTER#k3d-}"
  echo "[detect] k3d cluster: $K3D_NAME"
  for svc in "${SERVICES[@]}"; do
    echo ">>> [k3d import] food-delivery-${svc}..."
    k3d image import "food-delivery-${svc}:latest" -c "$K3D_NAME"
  done
else
  echo "[detect] native K3s (cluster=$CTX_CLUSTER)"
  for svc in "${SERVICES[@]}"; do
    echo ">>> [k3s ctr import] food-delivery-${svc}..."
    docker save "food-delivery-${svc}:latest" | sudo k3s ctr images import -
  done
fi

echo ""
echo "========================================="
echo "  Phase 3: kubectl apply (envsubst 치환 포함)"
echo "========================================="
# 매니페스트의 ${OTLP_ENDPOINT} placeholder 를 deploy-time 에 치환.
# Polestar10 collector 주소가 환경마다 다르므로 매니페스트 hardcode 불가 — testbed-build orchestrator
# 가 ansible service-k8s role 의 OTLP_ENDPOINT env var 로 주입 (NKIAAI-542 패턴, plopvape-shop 동일).
: "${OTLP_ENDPOINT:?OTLP_ENDPOINT 미설정 — ansible 또는 수동 export 필요. 예: export OTLP_ENDPOINT=http://192.168.230.104:6565}"
: "${POLESTAR_ORG_ID:?POLESTAR_ORG_ID 미설정 — ansible 또는 수동 export 필요. Polestar10 web 의 24자리 hex 조직 ID}"

# application target UUID placeholder. 미등록 단계(§7 4단계 전)에서는 빈 값 허용 → 기본 빈 문자열로 치환.
# 등록 후에는 ansible / 수동 export 로 실제 UUID 주입 (lucida.target_id 바인딩, spec §3.3).
export ORDER_TARGET_ID="${ORDER_TARGET_ID:-}"
export RESTAURANT_TARGET_ID="${RESTAURANT_TARGET_ID:-}"
export DISPATCH_TARGET_ID="${DISPATCH_TARGET_ID:-}"
export PAYMENT_TARGET_ID="${PAYMENT_TARGET_ID:-}"
export NOTIFY_TARGET_ID="${NOTIFY_TARGET_ID:-}"

# envsubst 화이트리스트로 명시 변수만 치환. 그 외 ${...} (예: K8s downward API 의 $(POD_NAME)) 와 충돌 회피.
for f in "${PROJECT_ROOT}/k8s/"*.yaml; do
  envsubst '${OTLP_ENDPOINT} ${POLESTAR_ORG_ID} ${ORDER_TARGET_ID} ${RESTAURANT_TARGET_ID} ${DISPATCH_TARGET_ID} ${PAYMENT_TARGET_ID} ${NOTIFY_TARGET_ID}' < "$f" | kubectl apply -f -
done

echo ""
echo "========================================="
echo "  Phase 4: rollout status"
echo "========================================="
kubectl -n rca-testbed-food rollout status statefulset/testbed-mysql --timeout=180s || true
kubectl -n rca-testbed-food rollout status deployment/testbed-redis --timeout=120s || true
for svc in "${SERVICES[@]}"; do
  kubectl -n rca-testbed-food rollout status deployment/testbed-${svc} --timeout=180s || true
done
kubectl -n rca-testbed-food rollout status deployment/testbed-external-pg-mock --timeout=120s || true

echo ""
echo "========================================="
echo "  Final state"
echo "========================================="
kubectl -n rca-testbed-food get pods
echo ""
kubectl -n rca-testbed-food get svc
