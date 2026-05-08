#!/bin/bash
# ============================================================
# social-feed K3s 빌드 + 배포 스크립트
# ============================================================
# ARM64 (aarch64) 호환. mysql:8.0 multiarch.
# 사용법: cd social-feed && bash k8s/build-and-deploy.sh

set -euo pipefail
export DOCKER_BUILDKIT=0

SERVICES=("post" "feed" "comment" "notification")
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo ""
echo "========================================="
echo "  Phase 1: Docker 이미지 빌드 (ARM)"
echo "========================================="
for svc in "${SERVICES[@]}"; do
  echo ""
  echo ">>> [빌드] social-feed-${svc}..."
  docker build --network=host -f "${PROJECT_ROOT}/${svc}-service/Dockerfile" \
    -t "social-feed-${svc}:latest" "${PROJECT_ROOT}"
  echo "<<< [완료] social-feed-${svc}"
done

echo ""
echo "========================================="
echo "  Phase 2: cluster 이미지 임포트 (k3d / native K3s 자동 감지)"
echo "========================================="
# kubectl 의 현재 context cluster 이름이 'k3d-<name>' 으로 시작하면 k3d.
# k3d 노드의 containerd 는 호스트 docker 와 분리되어 있어 `k3d image import` 로 명시 주입 필요.
# 그 외 (native K3s / 기타) 는 k3s containerd 로 import.
CTX_CLUSTER=$(kubectl config view --minify -o jsonpath='{.clusters[0].name}' 2>/dev/null || echo "")
if [[ "$CTX_CLUSTER" == k3d-* ]]; then
  K3D_NAME="${CTX_CLUSTER#k3d-}"
  echo "[detect] k3d cluster: $K3D_NAME"
  for svc in "${SERVICES[@]}"; do
    echo ">>> [k3d import] social-feed-${svc}..."
    k3d image import "social-feed-${svc}:latest" -c "$K3D_NAME"
  done
else
  echo "[detect] native K3s (cluster=$CTX_CLUSTER)"
  for svc in "${SERVICES[@]}"; do
    echo ">>> [k3s ctr import] social-feed-${svc}..."
    docker save "social-feed-${svc}:latest" | sudo k3s ctr images import -
  done
fi

echo ""
echo "========================================="
echo "  Phase 3: K8s 매니페스트 적용"
echo "========================================="
kubectl apply -f "${PROJECT_ROOT}/k8s/"

echo ""
echo "========================================="
echo "  Phase 4: 배포 상태 확인"
echo "========================================="
echo "StatefulSet 롤아웃 대기 중..."
kubectl -n rca-testbed-social rollout status statefulset/testbed-mysql --timeout=180s || true

echo "Deployment 롤아웃 대기 중..."
for svc in "${SERVICES[@]}"; do
  kubectl -n rca-testbed-social rollout status deployment/testbed-${svc} --timeout=180s || true
done
kubectl -n rca-testbed-social rollout status deployment/testbed-mock-push-gateway --timeout=120s || true
kubectl -n rca-testbed-social rollout status deployment/testbed-nginx --timeout=120s || true

echo ""
echo "========================================="
echo "  최종 상태"
echo "========================================="
kubectl -n rca-testbed-social get pods
echo ""
kubectl -n rca-testbed-social get svc
