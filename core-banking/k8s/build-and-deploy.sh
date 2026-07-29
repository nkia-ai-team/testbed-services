#!/bin/bash
# ============================================================
# core-banking kubeadm build + deploy script
# ============================================================
# Used on the target server by testbed-build orchestrator.
# Usage: cd core-banking && bash k8s/build-and-deploy.sh
# commerce/k8s/build-and-deploy.sh 최신 패턴(kubeadm ctr -n k8s.io import,
# Phase 2.5 ConfigMap --from-file, rollout fail-fast) 을 그대로 정합.

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
echo "  Phase 2: cluster image import (kubeadm/containerd, k3d fallback)"
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
  echo "[detect] kubeadm/containerd cluster (cluster=$CTX_CLUSTER)"
  # IMPORT_SSH_NODES: 빌드 호스트≠클러스터 노드 토폴로지용(commerce 스크립트와 동일 규약).
  SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=10)
  [[ -n "${IMPORT_SSH_KEY:-}" ]] && SSH_OPTS+=(-i "$IMPORT_SSH_KEY")
  for svc in "${SERVICES[@]}"; do
    if [[ -n "${IMPORT_SSH_NODES:-}" ]]; then
      for node in ${IMPORT_SSH_NODES}; do
        echo ">>> [ssh ctr import → ${node}] core-banking-${svc}..."
        docker save "core-banking-${svc}:latest" | ssh "${SSH_OPTS[@]}" "$node" "sudo ctr -n k8s.io images import -"
      done
    else
      echo ">>> [ctr import] core-banking-${svc}..."
      docker save "core-banking-${svc}:latest" | sudo ctr -n k8s.io images import -
    fi
  done
fi

echo ""
echo "========================================="
echo "  Phase 2.5: namespace + DB init/seed ConfigMap (원본 파일에서 직접 생성)"
echo "========================================="
kubectl apply -f "${PROJECT_ROOT}/k8s/00-namespace.yaml"

# DPM 모니터링 계정은 시드에 있어야 한다. 2026-07-28 Oracle 이사로 PV 가 새로
# 생기며 손으로 만들어 둔 lucida_mon 이 사라졌고 DPM 이 ORA-01017 로 멈췄다.
# 자격증명 정본은 01-secrets.yaml 이므로 여기서 읽어 치환한다 — 템플릿에 값을
# 복제해 두면 다음 변경 때 둘이 어긋난다. 환경변수로 덮어쓸 수 있다.
mon_user=${MON_USER:-$(awk '$1=="MON_USER:"{print $2}' "${PROJECT_ROOT}/k8s/01-secrets.yaml")}
mon_password=${MON_PASSWORD:-$(awk '$1=="MON_PASSWORD:"{print $2}' "${PROJECT_ROOT}/k8s/01-secrets.yaml")}
if [[ -z "$mon_user" || -z "$mon_password" ]]; then
  echo "ERROR: 01-secrets.yaml 에서 MON_USER/MON_PASSWORD 를 읽지 못했다" >&2
  exit 1
fi
monitoring_sql=$(mktemp)
trap 'rm -f "$monitoring_sql"' EXIT
sed -e "s/__MON_USER__/${mon_user}/g" -e "s/__MON_PASSWORD__/${mon_password}/g" \
  "${PROJECT_ROOT}/db/monitoring.sql.tmpl" > "$monitoring_sql"

kubectl create configmap oracle-init-scripts \
  --from-file=00-monitoring.sql="$monitoring_sql" \
  --from-file=01-init.sql="${PROJECT_ROOT}/db/init.sql" \
  --from-file=02-seed-all.sql="${PROJECT_ROOT}/db/seed-all.sql" \
  -n rca-testbed-banking --dry-run=client -o yaml | kubectl apply -f -
# loadgen(§8)은 클러스터 밖 tb-runner(192.168.122.206)의 systemd 서비스로 이전했다 —
# 측정 오염 방지 + 장애 중에도 baseline 유지. 배치 절차는 docs/runbook-testbed-deploy.md 참조.

echo ""
echo "========================================="
echo "  Phase 3: kubectl apply"
echo "========================================="
for f in "${PROJECT_ROOT}/k8s/"*.yaml; do
  kubectl apply -f "$f"
done

echo ""
echo "========================================="
echo "  Phase 4: rollout status (fail-fast — 실패 시 스크립트 즉시 종료)"
echo "========================================="
# :latest + imagePullPolicy:Never 조합은 재배포 시 Pod template 이 안 바뀌어 구이미지 Pod 가
# 그대로 남는다 — 명시적 restart 로 앱 Deployment 교체 강제 (StatefulSet 은 외부 고정 이미지라 제외).
kubectl -n rca-testbed-banking rollout restart deployment
kubectl -n rca-testbed-banking rollout status statefulset/testbed-oracle --timeout=600s
kubectl -n rca-testbed-banking rollout status statefulset/testbed-kafka --timeout=180s
for svc in "${SERVICES[@]}"; do
  kubectl -n rca-testbed-banking rollout status deployment/testbed-${svc} --timeout=180s
done
kubectl -n rca-testbed-banking rollout status deployment/testbed-nginx --timeout=120s

echo ""
echo "========================================="
echo "  Final state"
echo "========================================="
kubectl -n rca-testbed-banking get pods
echo ""
kubectl -n rca-testbed-banking get svc
