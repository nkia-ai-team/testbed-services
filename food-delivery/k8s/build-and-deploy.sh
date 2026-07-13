#!/bin/bash
# ============================================================
# food-delivery kubeadm 빌드 + 배포 스크립트
# ============================================================
# 109서버(ARM/aarch64)에서 실행한다.
# 사용법: cd food-delivery && bash k8s/build-and-deploy.sh
#
# 하는 일:
#   1) 5개 서비스 Docker 이미지를 ARM 네이티브로 빌드
#   2) 빌드된 이미지를 kubeadm containerd에 임포트
#   3) K8s 매니페스트를 적용 (kubectl apply)
#   4) 모든 Pod가 Running 상태인지 확인
#
# (7번 이식) commerce/k8s/build-and-deploy.sh 최신 패턴으로 정렬: kubeadm containerd import,
# Phase 2.5 동적 ConfigMap 생성(db/*.sql·loadgen/* 이 정본), rollout fail-fast.

set -euo pipefail
export DOCKER_BUILDKIT=0

SERVICES=("order" "restaurant" "dispatch" "payment" "notify")
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo ""
echo "========================================="
echo "  Phase 1: Docker 이미지 빌드 (ARM)"
echo "========================================="
# 각 서비스의 Dockerfile은 프로젝트 루트를 빌드 컨텍스트로 사용한다.
# 이유: 멀티모듈 Maven 프로젝트라서 루트의 pom.xml과 shop-common이 필요.
for svc in "${SERVICES[@]}"; do
  echo ""
  echo ">>> [빌드] food-delivery-${svc}..."
  docker build --network=host -f "${PROJECT_ROOT}/${svc}-service/Dockerfile" \
    -t "food-delivery-${svc}:latest" "${PROJECT_ROOT}"
  echo "<<< [완료] food-delivery-${svc}"
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
    echo ">>> [k3d import] food-delivery-${svc}..."
    k3d image import "food-delivery-${svc}:latest" -c "$K3D_NAME"
  done
else
  echo "[detect] kubeadm (cluster=$CTX_CLUSTER)"
  # IMPORT_SSH_NODES: 빌드 호스트≠클러스터 노드 토폴로지용(commerce 스크립트와 동일 규약).
  SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=10)
  [[ -n "${IMPORT_SSH_KEY:-}" ]] && SSH_OPTS+=(-i "$IMPORT_SSH_KEY")
  for svc in "${SERVICES[@]}"; do
    if [[ -n "${IMPORT_SSH_NODES:-}" ]]; then
      for node in ${IMPORT_SSH_NODES}; do
        echo ">>> [ssh ctr import → ${node}] food-delivery-${svc}..."
        docker save "food-delivery-${svc}:latest" | ssh "${SSH_OPTS[@]}" "$node" "sudo ctr -n k8s.io images import -"
      done
    else
      echo ">>> [ctr import] food-delivery-${svc}..."
      docker save "food-delivery-${svc}:latest" | sudo ctr -n k8s.io images import -
    fi
  done
fi

echo ""
echo "========================================="
echo "  Phase 2.5: DB 초기화 / loadgen ConfigMap 생성 (db·loadgen/* 이 정본)"
echo "========================================="
# mysql-init-scripts ConfigMap을 YAML에 SQL을 베껴 넣는 대신 food-delivery/db/init.sql에서
# 직접 생성한다 — docker-compose.dev.yml이 마운트하는 파일과 동일한 정본을 쓰게 해, k8s와
# 로컬 dev 시드가 어긋나는 일(commerce 4번 증분에서 실제로 겪은 문제)을 구조적으로 막는다.
# 네임스페이스가 먼저 있어야 하므로 00-namespace.yaml을 선적용한다.
kubectl apply -f "${PROJECT_ROOT}/k8s/00-namespace.yaml"
kubectl create configmap mysql-init-scripts \
  --from-file=01-init.sql="${PROJECT_ROOT}/db/init.sql" \
  -n rca-testbed-food --dry-run=client -o yaml | kubectl apply -f -

# loadgen(§8)의 k6 스크립트도 같은 이유(정본-사본 드리프트 방지)로 food-delivery/loadgen/에서
# 직접 ConfigMap을 생성한다. 40-loadgen.yaml은 이 ConfigMap을 이름으로만 참조한다.
kubectl create configmap loadgen-scripts \
  --from-file=script.js="${PROJECT_ROOT}/loadgen/script.js" \
  --from-file=entrypoint.sh="${PROJECT_ROOT}/loadgen/entrypoint.sh" \
  -n rca-testbed-food --dry-run=client -o yaml | kubectl apply -f -

echo ""
echo "========================================="
echo "  Phase 3: K8s 매니페스트 적용 (envsubst 치환 포함)"
echo "========================================="
# OTLP endpoint 는 manifest 가 downward API(status.hostIP:4317, 노드 로컬 OTel 에이전트)로 직접 구성한다
# — OTLP_ENDPOINT env 는 더 이상 치환 대상이 아니다 (에이전트 설치 전 임시 직접전송 시절의 잔재).
export OTLP_ENDPOINT="${OTLP_ENDPOINT:-}"
: "${POLESTAR_ORG_ID:?POLESTAR_ORG_ID 미설정 — Polestar10 web 의 24자리 hex 조직 ID. ansible 또는 수동 export 필요.}"

# lucida.target_id placeholder 는 application target 등록(§7) 후 UUID 를 export 해 주입한다.
# 미등록 상태 배포도 허용하므로 fail-fast 대상은 아니며, 미설정 시 envsubst 가 빈 문자열로 치환한다.
export ORDER_TARGET_ID="${ORDER_TARGET_ID:-}"
export RESTAURANT_TARGET_ID="${RESTAURANT_TARGET_ID:-}"
export DISPATCH_TARGET_ID="${DISPATCH_TARGET_ID:-}"
export PAYMENT_TARGET_ID="${PAYMENT_TARGET_ID:-}"
export NOTIFY_TARGET_ID="${NOTIFY_TARGET_ID:-}"

# fail-open 이되 조용히 빠지지는 않게 — 비어 있는 TARGET_ID 는 배포 로그에 경고를 남긴다.
for v in ORDER RESTAURANT DISPATCH PAYMENT NOTIFY; do
  eval tid="\${${v}_TARGET_ID}"
  [[ -z "$tid" ]] && echo "[WARN] ${v}_TARGET_ID 미설정 — lucida.target_id 빈 값으로 배포됨"
done

# envsubst 화이트리스트로 아래 placeholder 만 치환. 그 외 ${...} (예: K8s downward API 의 $(POD_NAME)) 와 충돌 회피.
# 파일 이름 앞 00-, 01-, 02-, 10-, 11-, 20-, 40- 번호 → kubectl apply 가 알파벳순 적용:
# Namespace(00) → Secret(01) → ConfigMap(02) → MySQL(10) → Kafka(11) → 서비스(20-24) → loadgen(40)
WHITELIST='${OTLP_ENDPOINT} ${POLESTAR_ORG_ID} ${ORDER_TARGET_ID} ${RESTAURANT_TARGET_ID} ${DISPATCH_TARGET_ID} ${PAYMENT_TARGET_ID} ${NOTIFY_TARGET_ID}'
for f in "${PROJECT_ROOT}/k8s/"*.yaml; do
  envsubst "$WHITELIST" < "$f" | kubectl apply -f -
done

echo ""
echo "========================================="
echo "  Phase 4: 배포 상태 확인"
echo "========================================="
# :latest + imagePullPolicy:Never 조합은 재배포 시 Pod template 이 안 바뀌어 구이미지 Pod 가
# 그대로 남는다 — 명시적 restart 로 교체 강제 (commerce 스크립트와 동일 이유).
echo "Deployment 재시작(새 이미지 반영) + 롤아웃 대기 중..."
kubectl -n rca-testbed-food rollout restart deployment
kubectl -n rca-testbed-food rollout status deployment --timeout=180s

# StatefulSet은 별도로 확인
echo "StatefulSet 롤아웃 대기 중..."
kubectl -n rca-testbed-food rollout status statefulset --timeout=180s

echo ""
echo "========================================="
echo "  최종 상태"
echo "========================================="
kubectl -n rca-testbed-food get pods
echo ""
kubectl -n rca-testbed-food get svc
