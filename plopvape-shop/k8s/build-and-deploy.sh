#!/bin/bash
# ============================================================
# plopvape-shop K3s 빌드 + 배포 스크립트
# ============================================================
# 109서버(ARM/aarch64)에서 실행한다.
# 사용법: cd plopvape-shop && bash k8s/build-and-deploy.sh
#
# 하는 일:
#   1) 5개 서비스 Docker 이미지를 ARM 네이티브로 빌드
#   2) 빌드된 이미지를 K3s containerd에 임포트
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
# 이유: 멀티모듈 Maven 프로젝트라서 루트의 pom.xml과 shop-common이 필요.
for svc in "${SERVICES[@]}"; do
  echo ""
  echo ">>> [빌드] plopvape-${svc}..."
  docker build --network=host -f "${PROJECT_ROOT}/${svc}-service/Dockerfile" \
    -t "plopvape-${svc}:latest" "${PROJECT_ROOT}"
  echo "<<< [완료] plopvape-${svc}"
done

echo ""
echo "========================================="
echo "  Phase 2: K3s 이미지 임포트"
echo "========================================="
# Docker 이미지를 K3s의 containerd로 옮긴다.
# docker save: 이미지를 tar 스트림으로 내보냄
# k3s ctr images import: tar를 containerd에 넣음
# 파이프(|)로 연결하면 중간 파일 없이 바로 전달.
for svc in "${SERVICES[@]}"; do
  echo ">>> [임포트] plopvape-${svc}..."
  docker save "plopvape-${svc}:latest" | sudo k3s ctr images import -
done

echo ""
echo "========================================="
echo "  Phase 3: K8s 매니페스트 적용 (envsubst 치환 포함)"
echo "========================================="
# 매니페스트의 ${OTLP_ENDPOINT} placeholder 를 deploy-time 에 치환.
# 사내 NAT/방화벽 환경에서는 collector 가 환경마다 달라서 매니페스트에 hardcode 불가.
# 누락 시 fail-fast — broken endpoint 로 Pod 가 뜨고 silent fail 하는 사고 차단.
: "${OTLP_ENDPOINT:?OTLP_ENDPOINT 미설정 — ansible 또는 수동 export 필요. 예: export OTLP_ENDPOINT=http://192.168.200.57:6565}"

# envsubst 화이트리스트로 '${OTLP_ENDPOINT}' 만 치환. 그 외 ${...} (예: K8s downward API 의 $(POD_NAME)) 와 충돌 회피.
# 파일 이름 앞 00-, 01-, 10-, 20-, 30- 번호 → kubectl apply 가 알파벳순 적용:
# Namespace(00) → Secret(01) → ConfigMap(02) → PostgreSQL(10) → ... → Nginx(30)
for f in "${PROJECT_ROOT}/k8s/"*.yaml; do
  envsubst '${OTLP_ENDPOINT}' < "$f" | kubectl apply -f -
done

echo ""
echo "========================================="
echo "  Phase 4: 배포 상태 확인"
echo "========================================="
# Deployment가 완전히 뜰 때까지 최대 3분 대기.
echo "Deployment 롤아웃 대기 중..."
kubectl -n rca-testbed rollout status deployment --timeout=180s || true

# StatefulSet은 별도로 확인
echo "StatefulSet 롤아웃 대기 중..."
kubectl -n rca-testbed rollout status statefulset --timeout=120s || true

echo ""
echo "========================================="
echo "  최종 상태"
echo "========================================="
kubectl -n rca-testbed get pods
echo ""
kubectl -n rca-testbed get svc
