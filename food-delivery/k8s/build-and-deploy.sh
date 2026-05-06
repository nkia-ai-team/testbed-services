#!/bin/bash
# ============================================================
# food-delivery K3s build + deploy script
# ============================================================
# Used on the target server (ARM64 / aarch64) by testbed-build orchestrator.
# Usage: cd food-delivery && bash k8s/build-and-deploy.sh

set -euo pipefail
export DOCKER_BUILDKIT=0

SERVICES=("order" "restaurant" "dispatch" "payment")
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
echo "  Phase 2: K3s image import"
echo "========================================="
for svc in "${SERVICES[@]}"; do
  echo ">>> [import] food-delivery-${svc}..."
  docker save "food-delivery-${svc}:latest" | sudo k3s ctr images import -
done

echo ""
echo "========================================="
echo "  Phase 3: kubectl apply"
echo "========================================="
kubectl apply -f "${PROJECT_ROOT}/k8s/"

echo ""
echo "========================================="
echo "  Phase 4: rollout status"
echo "========================================="
kubectl -n rca-testbed-food rollout status statefulset/testbed-postgres --timeout=180s || true
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
