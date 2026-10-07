#!/usr/bin/env bash
# Restore a trainer-reset "golden" onto the live AI stack before a scenario
# (spec-trainer-reset.md §5/§6, 2026-07-21). Light in-place reset:
#
#   1. select the golden whose capture time-of-day is nearest the run time
#   2. freeze the trainer (docker stop; restart-policy=no keeps it down)
#   3. restore the golden PG AI-state tables (reset the learned brain)
#   4. restart the observer so it reloads the golden models
#
# Continuity/warm-up is provided by the always-on baseline load plus the R6
# ~30-minute clean gap between scenarios (spec-scenario-load.md R6): the observer
# re-warms its short recent windows (stream 60m/log 1m/trace 5m; cold-start ~5m)
# from live-normal data before injection. The full data volumes are NOT swapped.
#
# The trainer stays frozen for the evaluation; thaw it afterwards with --thaw.
#
# Run this ON the AI-stack host (119): it uses the local docker daemon to
# freeze/restart containers and a postgres:16 sidecar for pg_restore.

set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage:
  restore-golden-state.sh (--golden-dir <dir> | --golden-root <root> [--for-time <ISO8601|now>]) \
    [--observer-container <name>] [--trainer-container <name>] \
    [--no-freeze] [--dry-run] [--ready-timeout-sec <n>]
  restore-golden-state.sh --thaw [--trainer-container <name>]
  restore-golden-state.sh --freeze [--trainer-container <name>]

Selection:
  --golden-dir   use this exact golden directory.
  --golden-root  pick the golden under here whose meta.tod_phase_kst is nearest
                 (circular, minutes) to --for-time (default: now).
Environment (same conventions as capture-golden-state.sh):
  PG_HOST 192.168.230.119  PG_PORT 15432  PG_USER lucida  PG_PASSWORD (required)
    RUNTIME=k8s 이고 PG_HOST 를 안 주면 pg-rw ClusterIP 로 자동 해석한다
    (PG_SERVICE 로 서비스명 변경 가능). 위 기본값은 docker 평면 전용이다.
  PG_DATABASE lucida        PG_DUMP_IMAGE postgres:16-alpine
  OBSERVER_HEALTH_URL http://192.168.230.119:18087/api/v1/health
  OBSERVER_CONTAINER  default: lucida-ai-observer
  TRAINER_CONTAINER   default: lucida-ai-trainer

  RUNTIME  auto (default) | docker | k8s   — 2026-08-13, 119 가 compose 에서 k3s 로 옮겨갔다.
    auto 는 trainer Deployment 가 실재하면 k8s, 아니면 docker 컨테이너를 본다.
    둘 다 없으면 죽는다 — 호출부(러너)가 RUNTIME 을 안 넘기는데 기본값이 docker 면
    Exited 로 남은 옛 compose 컨테이너 때문에 freeze 가 조용히 성공하기 때문이다.
    k8s 에서는 trainer/observer 가 Deployment 라 docker stop 으로 못 멈춘다(죽여도
    되살아난다). replicas 0/1 로 동결·해동한다.
    K8S_NAMESPACE   default: polestar
    TRAINER_WORKLOAD  default: deployment/ai-trainer
    OBSERVER_WORKLOAD default: deployment/ai-observer
    KUBECTL         비우면 자동 판별 — 'kubectl' 을 먼저, 안 되면 'sudo -n kubectl'.
                    (119 의 ydkim 계정은 sudo -n kubectl 만 된다)
    PG_SIDECAR_NET  default: host     — psql/pg_restore 사이드카가 ClusterIP 로 붙으려면
      호스트 네트워크를 써야 한다. 119 노드에서는 ClusterIP 가 호스트 라우팅으로 닿는다
      (2026-08-13 실측: pg-rw 10.43.100.31:5432 로 targets 1,581행 조회 성공).
EOF
}

die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }
log() { printf '[INFO] %s %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; }
require_command() { command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"; }

golden_dir=''
golden_root=''
for_time='now'
observer_container="${OBSERVER_CONTAINER:-lucida-ai-observer}"
trainer_container="${TRAINER_CONTAINER:-lucida-ai-trainer}"
no_freeze=false
dry_run=false
thaw_only=false
freeze_only=false
ready_timeout_sec=180

while (( $# > 0 )); do
  case "$1" in
    --golden-dir) golden_dir="${2:-}"; shift 2 ;;
    --golden-root) golden_root="${2:-}"; shift 2 ;;
    --for-time) for_time="${2:-}"; shift 2 ;;
    --observer-container) observer_container="${2:-}"; shift 2 ;;
    --trainer-container) trainer_container="${2:-}"; shift 2 ;;
    --no-freeze) no_freeze=true; shift ;;
    --dry-run) dry_run=true; shift ;;
    --thaw) thaw_only=true; shift ;;
    --freeze) freeze_only=true; shift ;;
    --ready-timeout-sec) ready_timeout_sec="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

# ------------------------------------------------------------
# 런타임 추상화 (2026-08-13). 119 가 compose → k3s 로 옮겨가면서 trainer/observer 가
# 컨테이너가 아니라 Deployment 가 됐다. docker stop 으로는 못 멈춘다 — 죽여도 ReplicaSet
# 이 되살린다. 아래 세 함수만 런타임을 안다; 호출부는 그대로다.
#
# 동결 = replicas 0. compose 의 restart-policy=no 와 같은 의미다(--thaw 전까지 안 뜬다).
# ------------------------------------------------------------
RUNTIME="${RUNTIME:-auto}"
K8S_NAMESPACE="${K8S_NAMESPACE:-polestar}"
TRAINER_WORKLOAD="${TRAINER_WORKLOAD:-deployment/ai-trainer}"
OBSERVER_WORKLOAD="${OBSERVER_WORKLOAD:-deployment/ai-observer}"
KUBECTL="${KUBECTL:-}"

# kubectl 호출 형태를 정한다. 119 는 ydkim 계정에 sudo -n kubectl 이 필요하다
# (평 kubectl 은 권한이 없다). 호출부가 KUBECTL 을 주면 그대로 쓴다.
detect_kubectl() {
  [[ -n "$KUBECTL" ]] && { printf '%s' "$KUBECTL"; return; }
  if command -v kubectl >/dev/null 2>&1 && kubectl version >/dev/null 2>&1; then
    printf 'kubectl'; return
  fi
  if command -v sudo >/dev/null 2>&1 && sudo -n kubectl version >/dev/null 2>&1; then
    printf 'sudo -n kubectl'; return
  fi
  printf ''
}
KUBECTL="$(detect_kubectl)"

# ------------------------------------------------------------
# 런타임 자동 판별 (2026-08-14).
#
# 왜 필요한가 — 호출부(러너 live_queue._run_restore)는 원격 실행에
# PG_PASSWORD·PG_USER·CH_PASSWORD·LUCIDA_LOGIN_* 만 실어 보내고 RUNTIME 을
# 넘기지 않는다. 기본값이 docker 면 k3s 이관 후에도 docker 경로로 떨어지는데,
# 119 에는 옛 compose 컨테이너(lucida-ai-trainer 등)가 Exited 로 남아 있어
# "이미 멈춰 있음"으로 조용히 통과하고 exit 0 을 낸다. 그동안 실제 k8s
# trainer 는 계속 학습한다 — 골든 복원이 거짓 성공하는 것이다.
#
# 그래서 (a) 실재하는 평면을 보고 판별하고, (b) 어느 평면에서도 대상을 못
# 찾으면 조용히 넘어가지 않고 죽는다.
# ------------------------------------------------------------
k8s_workload_exists() {
  [[ -n "$KUBECTL" ]] || return 1
  $KUBECTL -n "$K8S_NAMESPACE" get "$1" >/dev/null 2>&1
}
docker_container_exists() {
  command -v docker >/dev/null 2>&1 || return 1
  docker inspect "$1" >/dev/null 2>&1
}

detect_runtime() {
  # k8s 를 먼저 본다. 이관 후에는 옛 docker 컨테이너가 껍데기로 남아 있어
  # docker 를 먼저 보면 죽은 평면을 고르게 된다.
  if k8s_workload_exists "$TRAINER_WORKLOAD"; then printf 'k8s'; return; fi
  if docker_container_exists "$trainer_container"; then printf 'docker'; return; fi
  die "cannot detect runtime: neither k8s workload ($K8S_NAMESPACE/$TRAINER_WORKLOAD) nor docker container ($trainer_container) exists"
}

[[ "$RUNTIME" == auto ]] && RUNTIME="$(detect_runtime)"

case "$RUNTIME" in
  docker) require_command docker ;;
  k8s)    [[ -n "$KUBECTL" ]] || die "RUNTIME=k8s but no usable kubectl (tried 'kubectl' and 'sudo -n kubectl')" ;;
  *)      die "unknown RUNTIME: $RUNTIME (docker|k8s|auto)" ;;
esac

# 대상이 그 평면에 실재하는지 확인한다. 없는데 계속 가면 freeze/thaw 가 아무것도
# 안 하고 성공으로 끝난다 — 위 주석의 거짓 성공이 바로 이 경로다.
require_workload_exists() {
  if [[ "$RUNTIME" == docker ]]; then
    docker_container_exists "$1" || die "docker container not found: $1 (RUNTIME=$RUNTIME)"
  else
    k8s_workload_exists "$(workload_for "$1")" ||
      die "k8s workload not found: $K8S_NAMESPACE/$(workload_for "$1") (RUNTIME=$RUNTIME)"
  fi
}

log "runtime=$RUNTIME${KUBECTL:+ kubectl='$KUBECTL'} namespace=$K8S_NAMESPACE"

# 호출부가 쓰는 이름(lucida-ai-trainer)을 런타임 핸들로 옮긴다.
workload_for() {
  case "$1" in
    "$trainer_container")  printf '%s' "$TRAINER_WORKLOAD" ;;
    "$observer_container") printf '%s' "$OBSERVER_WORKLOAD" ;;
    *) die "no k8s workload mapped for container: $1" ;;
  esac
}

workload_running() {  # echo true/false
  if [[ "$RUNTIME" == docker ]]; then
    [[ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" == true ]] && echo true || echo false
    return
  fi
  local w n
  w=$(workload_for "$1")
  n=$($KUBECTL -n "$K8S_NAMESPACE" get "$w" -o jsonpath='{.spec.replicas}' 2>/dev/null)
  [[ -n "$n" && "$n" -gt 0 ]] && echo true || echo false
}

workload_stop() {
  if [[ "$RUNTIME" == docker ]]; then docker stop "$1" >/dev/null; return; fi
  local w; w=$(workload_for "$1")
  $KUBECTL -n "$K8S_NAMESPACE" scale "$w" --replicas=0 >/dev/null
  # scale 은 즉시 반환한다 — 파드가 실제로 사라질 때까지 기다려야 이어지는 DELETE/COPY
  # 가 살아있는 writer 와 경합하지 않는다(step 1b 의 이유와 같다).
  $KUBECTL -n "$K8S_NAMESPACE" rollout status "$w" --timeout=120s >/dev/null 2>&1 || true
}

workload_start() {
  if [[ "$RUNTIME" == docker ]]; then docker start "$1" >/dev/null; return; fi
  local w; w=$(workload_for "$1")
  $KUBECTL -n "$K8S_NAMESPACE" scale "$w" --replicas=1 >/dev/null
}

# --- thaw mode: just restart the trainer (post-evaluation) ---
if [[ "$thaw_only" == true ]]; then
  require_workload_exists "$trainer_container"
  log "thawing trainer ($RUNTIME): $trainer_container"
  workload_start "$trainer_container"
  log 'trainer thawed'
  exit 0
fi

# --- freeze mode: just stop the trainer (v3 cycle buffer start = t1-10m). The
#     cycle restores golden + thaws at cycle start so the trainer learns on the
#     2h normal lead-in, then freezes here for the evaluation window. --thaw
#     reverses it after capture. restart-policy=no keeps it down once stopped.
if [[ "$freeze_only" == true ]]; then
  require_workload_exists "$trainer_container"
  if [[ "$(workload_running "$trainer_container")" == true ]]; then
    log "freezing trainer ($RUNTIME): $trainer_container"
    workload_stop "$trainer_container"
  else
    log "trainer already stopped: $trainer_container"
  fi
  log 'trainer frozen'
  exit 0
fi

require_command jq
require_command python3

# --- resolve golden dir ---
if [[ -z "$golden_dir" ]]; then
  [[ -n "$golden_root" ]] || die 'either --golden-dir or --golden-root is required'
  [[ -d "$golden_root" ]] || die "golden root not found: $golden_root"
  # for_time -> KST minutes-of-day
  if [[ "$for_time" == now ]]; then
    target_min=$(TZ='Asia/Seoul' date +'%H %M' | awk '{print $1*60+$2}')
  else
    target_min=$(TZ='Asia/Seoul' date -d "$for_time" +'%H %M' 2>/dev/null | awk '{print $1*60+$2}') ||
      die "invalid --for-time: $for_time"
  fi
  # pick nearest circular tod_phase across goldens
  best='' ; best_dist=100000
  while IFS= read -r meta; do
    [[ -s "$meta" ]] || continue
    tod=$(jq -r '.tod_phase_kst // empty' "$meta" 2>/dev/null) || continue
    [[ "$tod" =~ ^[0-9]{2}:[0-9]{2}$ ]] || continue
    gmin=$(( 10#${tod%:*} * 60 + 10#${tod#*:} ))
    d=$(( gmin - target_min )); (( d < 0 )) && d=$(( -d ))
    (( d > 720 )) && d=$(( 1440 - d ))   # circular over 24h
    if (( d < best_dist )); then best_dist=$d; best="$(dirname "$meta")"; fi
  done < <(find "$golden_root" -mindepth 2 -maxdepth 2 -name meta.json 2>/dev/null)
  [[ -n "$best" ]] || die "no usable golden (meta.tod_phase_kst) under $golden_root"
  golden_dir="$best"
  log "selected nearest golden: $golden_dir (tod distance ${best_dist}m from target ${target_min}m KST)"
fi

meta="$golden_dir/meta.json"
dump="$golden_dir/data/golden-pg-state.dump"
[[ -s "$meta" ]] || die "golden meta.json missing: $meta"
[[ -s "$dump" ]] || die "golden pg dump missing: $dump"

golden_kind=$(jq -r '.kind // empty' "$meta")
[[ "$golden_kind" == golden ]] || die "not a golden (kind=$golden_kind): $meta"
expected_sha=$(jq -r '.pg_ai_state.sha256 // empty' "$meta")
actual_sha=$(sha256sum "$dump" | cut -d' ' -f1)
[[ -z "$expected_sha" || "$expected_sha" == "$actual_sha" ]] ||
  die "golden dump sha256 mismatch: meta=$expected_sha actual=$actual_sha"
mapfile -t golden_tables < <(jq -r '.pg_ai_state.tables[]' "$meta")
(( ${#golden_tables[@]} > 0 )) || die 'golden meta lists no AI-state tables'

# ------------------------------------------------------------
# k8s 평면에서는 호스트 포트 매핑(PG 15432 · observer 18087)이 없다. 그 포트는
# .104/.109 에만 열려 있어 **정작 이 스크립트가 도는 119 에서는 안 닿는다**
# (2026-08-14 실측: 15432 unreachable, 18087 실패 / ClusterIP 5432·8087 정상).
# 호출부(러너 _run_restore)는 PG_PASSWORD·PG_USER 만 보내고 주소는 안 넘기므로
# 여기서 ClusterIP 로 해석한다. 119 노드에서는 ClusterIP 가 그대로 라우팅된다.
# 명시적으로 준 값은 언제나 그대로 존중한다.
# ------------------------------------------------------------
k8s_svc_ip() { $KUBECTL -n "$K8S_NAMESPACE" get svc "$1" -o jsonpath='{.spec.clusterIP}' 2>/dev/null; }

if [[ "$RUNTIME" == k8s ]]; then
  PG_SERVICE="${PG_SERVICE:-pg-rw}"
  OBSERVER_SERVICE="${OBSERVER_SERVICE:-ai-observer}"
  if [[ -z "${PG_HOST:-}" ]]; then
    _ip=$(k8s_svc_ip "$PG_SERVICE")
    [[ -n "$_ip" ]] || die "cannot resolve PG service ClusterIP: $K8S_NAMESPACE/$PG_SERVICE"
    PG_HOST="$_ip"
    PG_PORT="${PG_PORT:-5432}"
    log "resolved PG via k8s svc $PG_SERVICE -> $PG_HOST:$PG_PORT"
  fi
  if [[ -z "${OBSERVER_HEALTH_URL:-}" ]]; then
    _ip=$(k8s_svc_ip "$OBSERVER_SERVICE")
    [[ -n "$_ip" ]] || die "cannot resolve observer service ClusterIP: $K8S_NAMESPACE/$OBSERVER_SERVICE"
    OBSERVER_HEALTH_URL="http://${_ip}:8087/api/v1/health"
    log "resolved observer health -> $OBSERVER_HEALTH_URL"
  fi
fi

PG_HOST="${PG_HOST:-192.168.230.119}"
PG_PORT="${PG_PORT:-15432}"
PG_USER="${PG_USER:-lucida}"
PG_PASSWORD="${PG_PASSWORD:-}"
PG_DATABASE="${PG_DATABASE:-lucida}"
PG_DUMP_IMAGE="${PG_DUMP_IMAGE:-postgres:16-alpine}"
OBSERVER_HEALTH_URL="${OBSERVER_HEALTH_URL:-http://192.168.230.119:18087/api/v1/health}"
[[ -n "$PG_PASSWORD" ]] || die 'PG_PASSWORD is required'

# ------------------------------------------------------------
# 관리 자격증명 (2026-08-14).
#
# 골든 복원의 두 단계 — 트리거를 끈 DELETE(session_replication_role=replica)와
# pg_restore --disable-triggers — 는 superuser 를 요구한다. compose 평면에서는
# lucida 가 superuser 였으나 k8s(CloudNativePG)는 앱 유저와 superuser 를 분리한다
# (2026-08-14 실측: lucida rolsuper=f → "permission denied to set parameter").
#
# 평상시 조회·질의는 그대로 PG_USER 로 돌고, 위 두 단계에서만 관리자를 쓴다.
# 자격증명은 클러스터 시크릿에서 읽는다 — 이 스크립트는 이미 같은 노드에서
# kubectl 로 워크로드를 조작하므로 새로운 권한 상승이 아니다.
# 명시적으로 준 PG_ADMIN_* 이 언제나 우선한다.
# ------------------------------------------------------------
PG_ADMIN_USER="${PG_ADMIN_USER:-}"
PG_ADMIN_PASSWORD="${PG_ADMIN_PASSWORD:-}"
if [[ "$RUNTIME" == k8s && -z "$PG_ADMIN_USER" ]]; then
  PG_SUPERUSER_SECRET="${PG_SUPERUSER_SECRET:-pg-superuser}"
  _admin_u=$($KUBECTL -n "$K8S_NAMESPACE" get secret "$PG_SUPERUSER_SECRET" \
    -o jsonpath='{.data.username}' 2>/dev/null | base64 -d 2>/dev/null)
  _admin_p=$($KUBECTL -n "$K8S_NAMESPACE" get secret "$PG_SUPERUSER_SECRET" \
    -o jsonpath='{.data.password}' 2>/dev/null | base64 -d 2>/dev/null)
  if [[ -n "$_admin_u" && -n "$_admin_p" ]]; then
    PG_ADMIN_USER="$_admin_u"
    PG_ADMIN_PASSWORD="$_admin_p"
    log "resolved PG admin from k8s secret $PG_SUPERUSER_SECRET (user=$PG_ADMIN_USER)"
  else
    log "WARN: k8s secret $K8S_NAMESPACE/$PG_SUPERUSER_SECRET not readable — falling back to $PG_USER"
  fi
  unset _admin_u _admin_p
fi
PG_ADMIN_USER="${PG_ADMIN_USER:-$PG_USER}"
PG_ADMIN_PASSWORD="${PG_ADMIN_PASSWORD:-$PG_PASSWORD}"

# k8s 에서 PG 는 ClusterIP(pg-rw)다. ClusterIP 는 노드의 호스트 라우팅으로만 닿으므로
# 사이드카가 기본 bridge 네트워크에 있으면 연결이 안 된다 → --network host.
# 2026-08-13 119 실측: --network host 로 pg-rw 10.43.100.31:5432 조회 성공.
pg_net_args=()
[[ "${PG_SIDECAR_NET:-$([[ "$RUNTIME" == k8s ]] && echo host || echo '')}" == host ]] &&
  pg_net_args=(--network host)

# 권한 사전 검사 — 아무것도 멈추기 전에 한다.
# 2026-08-14: 권한 부족이 "observer 정지" 다음에 터져서 observer 가 내려간 채로 남았다
# (복원은 step 3 에서 되살리는데 거기까지 못 갔다). 검사를 앞으로 당기면 실패해도
# 환경이 그대로다. dry-run 은 어차피 아무것도 안 만지므로 건너뛴다.
if [[ "$dry_run" != true ]]; then
  PGPASSWORD="$PG_ADMIN_PASSWORD" docker run --rm "${pg_net_args[@]}" --env PGPASSWORD \
    "$PG_DUMP_IMAGE" psql -v ON_ERROR_STOP=1 --host "$PG_HOST" --port "$PG_PORT" \
    --username "$PG_ADMIN_USER" --dbname "$PG_DATABASE" \
    -c 'SET session_replication_role = replica;' >/dev/null 2>&1 ||
    die "PG admin '$PG_ADMIN_USER' cannot SET session_replication_role — golden restore needs superuser. Nothing was stopped."
  log "PG admin privilege check passed (user=$PG_ADMIN_USER)"
fi

golden_tod=$(jq -r '.tod_phase_kst' "$meta")
if [[ "$dry_run" == true ]]; then
  jq -n \
    --arg golden_dir "$golden_dir" --arg tod "$golden_tod" \
    --arg observer "$observer_container" --arg trainer "$trainer_container" \
    --argjson freeze "$([[ "$no_freeze" == true ]] && echo false || echo true)" \
    --argjson n_tables "${#golden_tables[@]}" \
    --arg runtime "$RUNTIME" \
    --arg trainer_workload "$([[ "$RUNTIME" == k8s ]] && echo "$K8S_NAMESPACE/$TRAINER_WORKLOAD" || echo "$trainer_container")" \
    --arg observer_workload "$([[ "$RUNTIME" == k8s ]] && echo "$K8S_NAMESPACE/$OBSERVER_WORKLOAD" || echo "$observer_container")" \
    '{mode:"dry-run", action:"restore-golden",
      golden_dir:$golden_dir, golden_tod_kst:$tod, pg_tables:$n_tables,
      freeze_trainer:$freeze, observer_container:$observer, trainer_container:$trainer,
      runtime:$runtime, trainer_workload:$trainer_workload, observer_workload:$observer_workload,
      steps:["freeze trainer","pg_restore --clean AI-state","restart observer","await health"]}'
  exit 0
fi

# 두 대상이 판별된 평면에 실재하는지 먼저 확인한다. 없는 채로 내려가면 freeze 도
# observer 정지도 "이미 멈춰 있음"으로 조용히 통과해 복원이 거짓 성공한다.
require_workload_exists "$trainer_container"
require_workload_exists "$observer_container"

# ------------------------------------------------------------
# 1. Freeze the trainer (restart-policy=no keeps it down until --thaw).
# ------------------------------------------------------------
if [[ "$no_freeze" != true ]]; then
  if [[ "$(workload_running "$trainer_container")" == true ]]; then
    log "freezing trainer ($RUNTIME): $trainer_container"
    workload_stop "$trainer_container"
  else
    log "trainer already stopped: $trainer_container"
  fi
else
  log 'skipping trainer freeze (--no-freeze)'
fi

# ------------------------------------------------------------
# 1b. Stop the observer BEFORE resetting the tables. The observer live-writes the
#     AI-state tables (coverage_signal_state, detector_seen_signatures, ...); if it
#     keeps running, its inserts race the DELETE+COPY reset and the COPY fails with
#     a duplicate-key error. It is restarted in step 3. Unconditional (not gated on
#     --no-freeze): the observer is restarted regardless, so this only extends its
#     existing restart window by the ~15s of the restore.
# ------------------------------------------------------------
if [[ "$(workload_running "$observer_container")" == true ]]; then
  log "stopping observer for reset ($RUNTIME): $observer_container"
  workload_stop "$observer_container"
else
  log "observer already stopped: $observer_container"
fi

# ------------------------------------------------------------
# 2. Restore golden PG AI-state (the 23 tables in the dump). Empty the tables then
#    COPY golden data back (data-only, FK-safe); no live writer during the reset.
# ------------------------------------------------------------
log "restoring PostgreSQL AI-state from golden (${#golden_tables[@]} tables)"
restore_dir="$golden_dir/data"
if [[ -n "${RESTORE_HOST_INPUT_ROOT:-}" ]]; then
  restore_dir="${RESTORE_HOST_INPUT_ROOT%/}/$(basename "$golden_dir")/data"
fi
# Reset target tables to golden WITHOUT pg_restore --clean: --clean drops the
# tables' constraints, which fails when an out-of-golden table holds a FK into
# one (e.g. ai_training_runs -> ai_model_registry). Instead empty the tables and
# COPY golden data back, both with FK triggers disabled via
# session_replication_role=replica. Under replica role the DELETE/COPY order is
# FK-independent.
#
# ⚠ 이 두 단계는 superuser 를 요구한다. 2026-08-13 k3s 이관 전까지 lucida 가
# superuser 였고 이 주석도 "the lucida role is" 라고 단언했는데, CloudNativePG 는
# 앱 유저와 superuser 를 분리해서 그 단언이 조용히 거짓이 됐다
# (2026-08-14 실측 lucida rolsuper=f). 그래서 PG_ADMIN_* 를 따로 쓴다 — 위 참조.
delete_sql='SET session_replication_role = replica;'
for t in "${golden_tables[@]}"; do delete_sql+=" DELETE FROM public.\"$t\";"; done
PGPASSWORD="$PG_ADMIN_PASSWORD" docker run --rm "${pg_net_args[@]}" --env PGPASSWORD "$PG_DUMP_IMAGE" \
  psql -v ON_ERROR_STOP=1 --host "$PG_HOST" --port "$PG_PORT" --username "$PG_ADMIN_USER" \
  --dbname "$PG_DATABASE" -c "$delete_sql" >/dev/null ||
  die 'emptying AI-state tables failed — trainer is frozen; investigate before injecting'
PGPASSWORD="$PG_ADMIN_PASSWORD" docker run --rm "${pg_net_args[@]}" \
  --env PGPASSWORD \
  --volume "$restore_dir:/in:ro" \
  "$PG_DUMP_IMAGE" \
  pg_restore --data-only --disable-triggers --no-owner --no-privileges \
  --single-transaction \
  --host "$PG_HOST" --port "$PG_PORT" --username "$PG_ADMIN_USER" --dbname "$PG_DATABASE" \
  /in/golden-pg-state.dump ||
  die 'pg_restore failed — trainer is frozen; investigate before injecting'
log 'PostgreSQL AI-state restored (data-only, FK-safe)'

# ------------------------------------------------------------
# 3. Start the observer back up (stopped in step 1b) so it reloads the golden
#    models from the freshly-restored tables.
# ------------------------------------------------------------
log "starting observer ($RUNTIME): $observer_container"
workload_start "$observer_container"

# ------------------------------------------------------------
# 4. Await observer readiness (health endpoint).
# ------------------------------------------------------------
log "awaiting observer health (timeout ${ready_timeout_sec}s): $OBSERVER_HEALTH_URL"
require_command curl
deadline=$(( $(date +%s) + ready_timeout_sec ))
until curl --fail --silent --show-error --max-time 5 --output /dev/null "$OBSERVER_HEALTH_URL"; do
  (( $(date +%s) < deadline )) || die "observer did not become healthy within ${ready_timeout_sec}s"
  sleep 3
done
log 'observer healthy'

jq -n \
  --arg golden_dir "$golden_dir" --arg tod "$golden_tod" \
  --arg observer "$observer_container" --arg trainer "$trainer_container" \
  --argjson frozen "$([[ "$no_freeze" == true ]] && echo false || echo true)" \
  --argjson n_tables "${#golden_tables[@]}" \
  --arg restored_at "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" \
  '{action:"restore-golden", status:"ok", golden_dir:$golden_dir, golden_tod_kst:$tod,
    pg_tables_restored:$n_tables, trainer_frozen:$frozen, observer_container:$observer,
    trainer_container:$trainer, restored_at:$restored_at,
    note:"trainer stays frozen for evaluation; run with --thaw afterwards"}'
log 'golden restore complete'
