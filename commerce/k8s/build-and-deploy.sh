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

SERVICES=("order" "product" "inventory" "payment" "notification" "user" "cart" "pricing" "shipping" "gateway")
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# 모듈 디렉토리 이름 규칙: 대부분 "<svc>-service"이지만 api-gateway만 예외("api-gateway").
module_dir() {
  if [[ "$1" == "gateway" ]]; then
    echo "api-gateway"
  else
    echo "$1-service"
  fi
}

echo ""
echo "========================================="
echo "  Phase 1: Docker 이미지 빌드 (ARM)"
echo "========================================="
# 각 서비스의 Dockerfile은 프로젝트 루트를 빌드 컨텍스트로 사용한다.
# 이유: 멀티모듈 Maven 프로젝트라서 루트의 pom.xml과 commerce-common이 필요.
for svc in "${SERVICES[@]}"; do
  echo ""
  echo ">>> [빌드] commerce-${svc}..."
  docker build --network=host -f "${PROJECT_ROOT}/$(module_dir "$svc")/Dockerfile" \
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
  # IMPORT_SSH_NODES: 빌드 호스트가 클러스터 노드가 아닌 토폴로지(예: 109 호스트에서 빌드,
  # 클러스터는 게스트 VM)용. 공백 구분 ssh 대상 목록(user@ip). 지정 시 각 노드의 containerd 로
  # docker save 스트림을 ssh 로 흘려 임포트한다. 미지정 시 기존처럼 로컬 containerd 임포트.
  # IMPORT_SSH_KEY: (옵션) ssh 개인키 경로.
  SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=10)
  [[ -n "${IMPORT_SSH_KEY:-}" ]] && SSH_OPTS+=(-i "$IMPORT_SSH_KEY")
  for svc in "${SERVICES[@]}"; do
    if [[ -n "${IMPORT_SSH_NODES:-}" ]]; then
      for node in ${IMPORT_SSH_NODES}; do
        echo ">>> [ssh ctr import → ${node}] commerce-${svc}..."
        docker save "commerce-${svc}:latest" | ssh "${SSH_OPTS[@]}" "$node" "sudo ctr -n k8s.io images import -"
      done
    else
      echo ">>> [ctr import] commerce-${svc}..."
      docker save "commerce-${svc}:latest" | sudo ctr -n k8s.io images import -
    fi
  done
fi

echo ""
echo "========================================="
echo "  Phase 2.5: DB 초기화 ConfigMap 생성 (db/*.sql이 정본)"
echo "========================================="
# postgres-init-scripts ConfigMap을 YAML에 데이터를 베껴 넣는 대신 commerce/db/init-schemas.sql·
# commerce/db/seed-all.sql에서 직접 생성한다 — docker-compose.dev.yml이 마운트하는 파일과
# 동일한 정본을 쓰게 해, k8s와 로컬 dev 시드가 다시 어긋나는 일(3a~4번 증분 사이 실제로 있었던
# 문제)을 구조적으로 막는다. 네임스페이스가 먼저 있어야 하므로 00-namespace.yaml을 선적용한다.
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

kubectl create configmap postgres-init-scripts \
  --from-file=00-monitoring.sql="$monitoring_sql" \
  --from-file=01-init-schemas.sql="${PROJECT_ROOT}/db/init-schemas.sql" \
  --from-file=02-seed-all.sql="${PROJECT_ROOT}/db/seed-all.sql" \
  -n rca-testbed-commerce --dry-run=client -o yaml | kubectl apply -f -

# loadgen(§8)은 클러스터 밖 tb-runner(192.168.122.206)의 systemd 서비스로 이전했다 —
# 측정 오염 방지 + 장애 중에도 baseline 유지. 배치 절차는 docs/runbook-testbed-deploy.md 참조.

echo ""
echo "========================================="
echo "  Phase 3: K8s 매니페스트 적용"
echo "========================================="
# 파일 이름 앞 00-, 01-, 10-, 20-, 30- 번호 → kubectl apply 가 알파벳순 적용:
# Namespace(00) → Secret(01) → ConfigMap(02) → PostgreSQL(10) → ... → Nginx(30)
for f in "${PROJECT_ROOT}/k8s/"*.yaml; do
  kubectl apply -f "$f"
done

echo ""
echo "========================================="
echo "  Phase 4: 배포 상태 확인"
echo "========================================="
# :latest 태그 + imagePullPolicy:Never 조합은 재배포 시 Pod template 이 안 바뀌어
# 기존 Pod 가 구이미지로 계속 돈다 — 이미지 임포트 후 명시적 restart 로 교체를 강제한다.
# StatefulSet(DB·Kafka)은 이미지가 고정 태그(외부 이미지)라 재시작 대상이 아니다.
echo "Deployment 재시작(새 이미지 반영) + 롤아웃 대기 중..."
kubectl -n rca-testbed-commerce rollout restart deployment
kubectl -n rca-testbed-commerce rollout status deployment --timeout=180s

# StatefulSet은 별도로 확인
echo "StatefulSet 롤아웃 대기 중..."
kubectl -n rca-testbed-commerce rollout status statefulset --timeout=120s

echo ""
echo "========================================="
echo "  Phase 4.1: 관측 인덱스 멱등 적용(기존 PVC 대응)"
echo "========================================="
# db/init-schemas.sql 은 데이터 디렉터리가 비어 있을 때만 postgres 엔트리포인트가
# 실행한다. PVC 를 물려받는 테스트베드에서는 나중에 추가된 인덱스가 영영 생기지
# 않는다 — banking Phase 4.1 이 같은 함정으로 신설된 단계이고, 이번에도 F23-R
# 인덱스가 init 파일에만 있으면 실환경에는 도달하지 못한다.
#
# CONCURRENTLY 인 이유: 이 표는 2.5M 행이고 상주 baseline 부하가 계속 쓰고 있다.
# 일반 CREATE INDEX 는 ACCESS EXCLUSIVE 로 잠가 그동안의 재고 이동을 전부 막는다.
#
# ⚠ CONCURRENTLY + IF NOT EXISTS 의 함정 — 이 순서가 이유다.
#   1) CONCURRENTLY 는 트랜잭션 블록 안에서 못 돈다. psql 에 여러 문장을 한 번의
#      -c 로 주면 하나의 트랜잭션으로 묶여 실패하므로, 문장마다 -c 를 따로 준다.
#   2) CONCURRENTLY 가 중간에 실패하면 indisvalid=false 인 **무효 인덱스가 남는다**.
#      그 상태에서 IF NOT EXISTS 로 재시도하면 "이미 있다"고 판단해 건너뛰므로,
#      쓰기 비용만 물리고 읽기에는 안 쓰이는 인덱스가 조용히 영구화된다.
#      그래서 만들기 전에 무효 잔재를 먼저 떨어뜨린다.
#   3) CONCURRENTLY 는 병렬 빌드를 쓰지 못한다(max_parallel_maintenance_workers 무시).
#      힙 164MB 를 두 번 훑고 RESTOCK 11,949건만 정렬하므로 maintenance_work_mem
#      64MB 안에서 끝난다 — 실측 기준 수 초 규모이나, 동시 트랜잭션이 빠지길
#      기다리는 시간이 지배적이라 여유를 크게 준다.
PGX=(kubectl -n rca-testbed-commerce exec -i testbed-postgres-0 -- psql -U commerce -d commerce -v ON_ERROR_STOP=1)

# postgres 가 연결을 받을 때까지만 재시도한다. 인덱스 생성 자체는 재시도하지 않는다 —
# 빌드가 겹치면 서로를 기다리며 잠금 대기를 만든다.
pg_ready=no
for attempt in $(seq 1 30); do
  if "${PGX[@]}" -qtAc 'SELECT 1' >/dev/null 2>&1; then pg_ready=yes; break; fi
  echo "[retry ${attempt}/30] postgres 가 아직 연결을 받지 않는다. 5초 후 재시도..."
  sleep 5
done
if [[ "$pg_ready" != "yes" ]]; then
  echo "ERROR: postgres 연결 실패 — 관측 인덱스 없이 배포를 끝내지 않는다" >&2
  exit 1
fi

# (2)의 잔재 청소 + 빌드 + 유효성 확인을 인덱스마다 같은 순서로 돌린다.
# 함수로 묶은 것은 관측 인덱스가 셋이 됐기 때문이다(2026-08-07 프로브 전수 점검).
# 세 벌을 복사하면 위 세 함정 중 하나를 한 곳에서만 고치는 사고가 난다.
#   $1=스키마  $2=인덱스명  $3=CREATE 문  $4=적용 후 확인용 EXPLAIN 문
ensure_observation_index() {
  local schema="$1" idx="$2" create_sql="$3" explain_sql="$4"
  local invalid_left index_valid

  # DO 블록 안에서 하지 않는 이유: DROP INDEX CONCURRENTLY 도 트랜잭션 블록 안에서
  # 돌 수 없고 DO 블록은 트랜잭션이다. 판정과 실행을 나눠 최상위 문장으로 보낸다.
  invalid_left=$("${PGX[@]}" -qtAc "SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
    WHERE c.relname = '${idx}' AND NOT i.indisvalid" | tr -d '[:space:]')
  if [[ "$invalid_left" != "0" ]]; then
    echo "무효 인덱스 잔재를 제거한다(직전 CONCURRENTLY 실패): ${idx}"
    "${PGX[@]}" -qtAc "DROP INDEX CONCURRENTLY ${schema}.${idx}"
  fi

  echo "인덱스 빌드 중(CONCURRENTLY, 잠금 없음): ${idx}"
  "${PGX[@]}" -qtAc "$create_sql"

  # 유효성 확인. CONCURRENTLY 는 실패해도 종료코드가 0 일 수 있으므로 상태를 직접 읽는다.
  index_valid=$("${PGX[@]}" -qtAc "SELECT coalesce(bool_and(i.indisvalid), false)
    FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
    WHERE c.relname = '${idx}'" | tr -d '[:space:]')
  if [[ "$index_valid" != "t" ]]; then
    echo "ERROR: ${idx} 이 유효하지 않다(valid=${index_valid})." >&2
    echo "       DROP INDEX CONCURRENTLY ${schema}.${idx} 후 재실행하라." >&2
    exit 1
  fi

  echo "인덱스 유효 확인 완료: ${idx}. 적용 후 계획:"
  "${PGX[@]}" -c "$explain_sql"
  echo ""
}

# 1) F23-R database.restock_movement_rate (tick 15s) — 2.5M 행 / 353MB.
#    RESTOCK 은 전체의 0.48%(11,949/2,501,668)라 부분 인덱스가 1MB 미만이다.
ensure_observation_index inventory_schema idx_inventory_movements_restock \
  "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_inventory_movements_restock
     ON inventory_schema.inventory_movements (created_at) WHERE movement_type = 'RESTOCK'" \
  "EXPLAIN (ANALYZE, BUFFERS) SELECT count(*) AS restock_count
     FROM inventory_schema.inventory_movements
     WHERE movement_type = 'RESTOCK' AND created_at >= now() - interval '5 minutes'"

# 2) F15-R business.order_duplicate_count_since_t1 (tick 15s) — 1.19M 행 / 252MB.
#    질의는 created_at 창을 자른 뒤 order_id 로 묶는다. 두 컬럼을 다 실어 힙을 안 가는
#    Index Only Scan 이 되게 한다. 기존 idx_payments_unsettled 는 부분 술어가
#    settled_at IS NULL 이라 이 질의(그 술어가 없다)에는 쓸 수 없었다.
#    부분 인덱스로 만들지 않은 이유: 이 질의에는 상수 술어가 없다(시간 범위뿐).
ensure_observation_index payment_schema idx_payments_created_order \
  "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_payments_created_order
     ON payment_schema.payments (created_at, order_id)" \
  "EXPLAIN (ANALYZE, BUFFERS) SELECT count(*) FROM (
     SELECT order_id FROM payment_schema.payments
     WHERE created_at >= now() - interval '10 minutes'
     GROUP BY order_id HAVING count(*) > 1) AS duplicates"

# 3) preflight baseline-business-success — 1.19M 행 / 149MB. 런당 1회지만 ready 41종
#    전부가 지나므로 배치 전체에 얇게 깔린다.
#    부분 인덱스인 이유가 F23-R 과 다르다: status='PAID' 는 전체의 99.84%(pg_stats)라
#    **크기는 안 줄어든다.** 술어를 정확히 일치시켜 status 재검사를 없애고 count(*) 가
#    Index Only Scan 이 되게 하는 것이 목적이다 — 오늘 outbox_events 가 같은 1.1M 행에서
#    cost 4.31 인 이유가 정확히 이 술어 일치였다.
ensure_observation_index order_schema idx_orders_paid_created \
  "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_orders_paid_created
     ON order_schema.orders (created_at) WHERE status = 'PAID'" \
  "EXPLAIN (ANALYZE, BUFFERS) SELECT count(*) AS paid_count FROM order_schema.orders
     WHERE status = 'PAID' AND created_at >= now() - interval '5 minutes'"

echo ""
echo "========================================="
echo "  최종 상태"
echo "========================================="
kubectl -n rca-testbed-commerce get pods
echo ""
kubectl -n rca-testbed-commerce get svc
