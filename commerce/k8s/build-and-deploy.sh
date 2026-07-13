#!/bin/bash
# ============================================================
# commerce kubeadm 빌드 + 배포 스크립트
# ============================================================
# 109서버(ARM/aarch64)에서 실행한다.
# 사용법: cd commerce && bash k8s/build-and-deploy.sh
#
# 하는 일:
#   1) 5개 서비스 Docker 이미지를 ARM 네이티브로 빌드
#   2) 빌드된 이미지를 kubeadm containerd에 임포트
#   3) K8s 매니페스트를 적용 (kubectl apply)
#   4) 모든 Pod가 Running 상태인지 확인

# 에러 발생 시 즉시 중단 (-e), 미정의 변수 사용 시 에러 (-u), 파이프 에러 전파 (-o pipefail)
set -euo pipefail
export DOCKER_BUILDKIT=0

SERVICES=("order" "product" "inventory" "payment" "notification")
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo ""
echo "========================================="
echo "  Phase 1: Docker 이미지 빌드 (ARM)"
echo "========================================="
# 각 서비스의 Dockerfile은 프로젝트 루트를 빌드 컨텍스트로 사용한다.
# 이유: 멀티모듈 Maven 프로젝트라서 루트의 pom.xml과 commerce-common이 필요.
for svc in "${SERVICES[@]}"; do
  echo ""
  echo ">>> [빌드] commerce-${svc}..."
  docker build --network=host -f "${PROJECT_ROOT}/${svc}-service/Dockerfile" \
    -t "commerce-${svc}:latest" "${PROJECT_ROOT}"
  echo "<<< [완료] commerce-${svc}"
done

echo ""
echo "========================================="
echo "  Phase 2: cluster 이미지 임포트 (k3d / kubeadm 자동 감지)"
echo "========================================="
# kubectl 의 현재 context cluster 이름이 'k3d-<name>' 으로 시작하면 k3d.
# k3d 노드의 containerd 는 호스트 docker 와 분리되어 있어 `k3d image import` 로 명시 주입 필요.
# 그 외 (kubeadm / 기타) 는 kubeadm 의 containerd 로 import.
CTX_CLUSTER=$(kubectl config view --minify -o jsonpath='{.clusters[0].name}' 2>/dev/null || echo "")
if [[ "$CTX_CLUSTER" == k3d-* ]]; then
  K3D_NAME="${CTX_CLUSTER#k3d-}"
  echo "[detect] k3d cluster: $K3D_NAME"
  for svc in "${SERVICES[@]}"; do
    echo ">>> [k3d import] commerce-${svc}..."
    k3d image import "commerce-${svc}:latest" -c "$K3D_NAME"
  done
else
  echo "[detect] kubeadm (cluster=$CTX_CLUSTER)"
  for svc in "${SERVICES[@]}"; do
    echo ">>> [ctr import] commerce-${svc}..."
    docker save "commerce-${svc}:latest" | sudo ctr -n k8s.io images import -
  done
fi

echo ""
echo "========================================="
echo "  Phase 3: K8s 매니페스트 적용 (envsubst 치환 포함)"
echo "========================================="
# 매니페스트의 ${OTLP_ENDPOINT} placeholder 를 deploy-time 에 치환.
# 사내 NAT/방화벽 환경에서는 collector 가 환경마다 달라서 매니페스트에 hardcode 불가.
# 누락 시 fail-fast — broken endpoint 로 Pod 가 뜨고 silent fail 하는 사고 차단.
: "${OTLP_ENDPOINT:?OTLP_ENDPOINT 미설정 — ansible 또는 수동 export 필요. 예: export OTLP_ENDPOINT=http://192.168.200.57:6565}"
: "${POLESTAR_ORG_ID:?POLESTAR_ORG_ID 미설정 — lucida org id. ansible 또는 수동 export 필요.}"

# lucida.target_id placeholder 는 application target 등록(§7) 후 UUID 를 export 해 주입한다.
# 미등록 상태 배포도 허용하므로 fail-fast 대상은 아니며, 미설정 시 envsubst 가 빈 문자열로 치환한다.
export ORDER_TARGET_ID="${ORDER_TARGET_ID:-}"
export PRODUCT_TARGET_ID="${PRODUCT_TARGET_ID:-}"
export INVENTORY_TARGET_ID="${INVENTORY_TARGET_ID:-}"
export PAYMENT_TARGET_ID="${PAYMENT_TARGET_ID:-}"
export NOTIFICATION_TARGET_ID="${NOTIFICATION_TARGET_ID:-}"

# envsubst 화이트리스트로 아래 placeholder 만 치환. 그 외 ${...} (예: K8s downward API 의 $(POD_NAME)) 와 충돌 회피.
# 파일 이름 앞 00-, 01-, 10-, 20-, 30- 번호 → kubectl apply 가 알파벳순 적용:
# Namespace(00) → Secret(01) → ConfigMap(02) → PostgreSQL(10) → ... → Nginx(30)
WHITELIST='${OTLP_ENDPOINT} ${POLESTAR_ORG_ID} ${ORDER_TARGET_ID} ${PRODUCT_TARGET_ID} ${INVENTORY_TARGET_ID} ${PAYMENT_TARGET_ID} ${NOTIFICATION_TARGET_ID}'
for f in "${PROJECT_ROOT}/k8s/"*.yaml; do
  envsubst "$WHITELIST" < "$f" | kubectl apply -f -
done

echo ""
echo "========================================="
echo "  Phase 4: 배포 상태 확인"
echo "========================================="
# Deployment가 완전히 뜰 때까지 최대 3분 대기.
echo "Deployment 롤아웃 대기 중..."
kubectl -n rca-testbed-commerce rollout status deployment --timeout=180s

# StatefulSet은 별도로 확인
echo "StatefulSet 롤아웃 대기 중..."
kubectl -n rca-testbed-commerce rollout status statefulset --timeout=120s

echo ""
echo "========================================="
echo "  최종 상태"
echo "========================================="
kubectl -n rca-testbed-commerce get pods
echo ""
kubectl -n rca-testbed-commerce get svc
