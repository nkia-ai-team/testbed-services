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

# DPM 모니터링 계정은 시드에 있어야 한다. banking Oracle 은 07-28 이사로 PV 가
# 새로 생기며 손으로 만든 lucida_mon 이 사라져 DPM 이 36시간 멈췄다. 자격증명
# 정본은 01-secrets.yaml 이므로 여기서 읽어 치환한다 — 템플릿에 값을 복제해 두면
# 다음 변경 때 둘이 어긋난다. 환경변수로 덮어쓸 수 있다.
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

kubectl create configmap mysql-init-scripts \
  --from-file=00-monitoring.sql="$monitoring_sql" \
  --from-file=01-init.sql="${PROJECT_ROOT}/db/init.sql" \
  -n rca-testbed-food --dry-run=client -o yaml | kubectl apply -f -

# loadgen(§8)은 클러스터 밖 tb-runner(192.168.122.206)의 systemd 서비스로 이전했다 —
# 측정 오염 방지 + 장애 중에도 baseline 유지. 배치 절차는 docs/runbook-testbed-deploy.md 참조.

echo ""
echo "========================================="
echo "  Phase 3: K8s 매니페스트 적용"
echo "========================================="
# 파일 이름 앞 00-, 01-, 02-, 10-, 11-, 20- 번호 → kubectl apply 가 알파벳순 적용:
# Namespace(00) → Secret(01) → ConfigMap(02) → MySQL(10) → Kafka(11) → 서비스(20-24)
for f in "${PROJECT_ROOT}/k8s/"*.yaml; do
  kubectl apply -f "$f"
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
echo "  Phase 4.1: 제어 테이블 + dispatches 인덱스 멱등 적용(기존 PVC 대응)"
echo "========================================="
# db/init.sql 은 데이터 디렉터리가 비어 있을 때만 MySQL 엔트리포인트가 실행한다. PVC 를
# 그대로 물려받는 테스트베드에서는 나중에 추가된 제어 테이블이 영영 생기지 않고, 앱은
# 없는 테이블을 폴링하며 실패만 반복한다(주입 표면이 조용히 죽어 있다). 여기서 같은
# 정의를 멱등으로 다시 넣는다 — 상한 10000 은 init.sql·앱·실행기와 함께 가드 테스트가
# 한 값으로 묶는다. 비밀번호는 파드 자기 env(mysql-secret)에서만 온다.
control_ddl=$(cat <<'SQL'
CREATE TABLE IF NOT EXISTS response_delay_control (
    service_id  VARCHAR(32) PRIMARY KEY,
    delay_ms    INT NOT NULL DEFAULT 0,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT chk_response_delay_ms CHECK (delay_ms BETWEEN 0 AND 10000)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
INSERT IGNORE INTO response_delay_control (service_id, delay_ms) VALUES ('restaurant', 0);

-- dispatches(status, assigned_at) — 2026-08-07 실측. countByStatus("ASSIGNED") 가 주문
-- 생성마다 두 번 도는데 status 인덱스가 없어 표가 커질수록 풀스캔이 된다. 137만 행에서
-- /api/deliveries/capacity 가 500 을 내기 시작했고, OrderService 가 그것을 payment 호출
-- **이전에** 503 으로 바꿔(:108-111) 하류 주입을 통째로 가렸다. init.sql 은 빈 데이터
-- 디렉터리에서만 도니 기존 PVC 에는 여기서 넣어야 한다.
--
-- MySQL 에는 CREATE INDEX IF NOT EXISTS 가 없다. 이 블록은 실패 시 30회 재시도로 통째로
-- 다시 실행되므로 그냥 ALTER 를 쓰면 두 번째 시도가 "Duplicate key name" 으로 죽고,
-- 그러면 재시도 루프가 끝내 성공하지 못해 배포가 중단된다. information_schema 로 존재를
-- 먼저 확인하고 동적 SQL 로 분기해 멱등하게 만든다.
SET @idx_exists := (
    SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE table_schema = DATABASE()
      AND table_name = 'dispatches'
      AND index_name = 'idx_dispatches_status_assigned'
);
SET @idx_ddl := IF(@idx_exists = 0,
    'ALTER TABLE dispatches ADD INDEX idx_dispatches_status_assigned (status, assigned_at)',
    'DO 0');
PREPARE add_idx FROM @idx_ddl;
EXECUTE add_idx;
DEALLOCATE PREPARE add_idx;
SQL
)
control_applied=no
for attempt in $(seq 1 30); do
  if printf '%s\n' "$control_ddl" | kubectl -n rca-testbed-food exec -i testbed-mysql-0 -- \
      sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot fooddelivery'; then
    control_applied=yes
    break
  fi
  echo "[retry ${attempt}/30] MySQL 이 아직 DDL 을 받지 않는다. 10초 후 재시도..."
  sleep 10
done
if [[ "$control_applied" != "yes" ]]; then
  echo "ERROR: 제어 테이블 DDL 적용 실패 — app.control 주입 표면 없이 배포를 끝내지 않는다" >&2
  exit 1
fi

echo ""
echo "========================================="
echo "  최종 상태"
echo "========================================="
kubectl -n rca-testbed-food get pods
echo ""
kubectl -n rca-testbed-food get svc
