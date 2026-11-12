#!/usr/bin/env bash
# Capture one trainer-reset "golden" — a clean, frozen AI-layer state plus a
# normal-data window for continuity (spec-trainer-reset.md §2/§3/§8, 2026-07-21).
#
# A golden is restored before each scenario so every case is evaluated against an
# identical, fault-free AI brain (reproducibility) and the previous scenario's
# fault cannot contaminate the next (anti-contamination). One golden = the AI
# learned state (PostgreSQL trainer/observer tables) + a bounded normal window
# (VictoriaMetrics + ClickHouse) captured around the same time-of-day, so restore
# can splice the normal segment continuously in front of the injection (§7.2).
#
# Capture MULTIPLE goldens across the day (§8, 시간대별 golden 다장): the restore
# step picks the golden nearest the run's time-of-day, so scenarios run all day.
# Capture only during a CLEAN baseline period — never during a scenario injection
# (a lightweight freshness gate confirms the stack is alive; the operator is
# responsible for choosing a fault-free window).
#
# The operative stream-anomaly model.json is a static human-seeded default
# (unchanged since 2026-07-13) and is NOT part of the golden; the trainer-produced
# models live in the PostgreSQL tables dumped here.

set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage:
  capture-golden-state.sh [--window-minutes <n, default 120>] \
    [--label <text>] [--output-root <goldens dir>] [--dry-run] [--skip-freshness]

Captures a golden as of "now": PostgreSQL AI-state tables + a normal window
[now-<window>, now] from VictoriaMetrics and ClickHouse. Stored under
<output-root>/<capture UTC compact>/ with meta.json recording the time-of-day
(tod_phase) used by the restore step to select the nearest golden.

Required environment for a live capture (same conventions as capture-eval-case.sh):
  VM_URL         default: http://192.168.230.119:18428
  CH_URL         default: http://192.168.230.119:18123
  CH_USER        default: lucida
  CH_PASSWORD    required unless the ClickHouse endpoint allows anonymous access
  PG_HOST        default: 192.168.230.119
  PG_PORT        default: 15432
  PG_USER        default: lucida
  PG_PASSWORD    required
  PG_DATABASE    default: lucida
  PG_DUMP_IMAGE  default: postgres:17-alpine (서버 메이저 이상이어야 한다. 덤프 전에 확인)
Freshness gate:
  OBSERVER_HEALTH_URL   default: http://192.168.230.119:18087/api/v1/health
  FRESHNESS_MAX_LAG_SEC default: 600
Container/host path mapping (when this script runs inside a container and the
pg_dump sidecar needs the host path of the staging dir):
  CAPTURE_HOST_OUTPUT_ROOT   default: '' (unset = same path)
Runtime (2026-11-12, v4 E11). 119 는 2026-08-13 부터 k3s 다. 위 주소 기본값은 compose 평면의
호스트 포트라 119 자신에서는 닿지 않는다(평가용 LB 는 104, 66, 109 만 받는다).
  RUNTIME  auto (default) | docker | k8s
    auto 는 kubectl 이 되고 trainer Deployment 가 있으면 k8s, 아니면 docker(위 기본값).
    k8s 에서는 따로 주지 않은 값만 클러스터에서 채운다(준 값은 언제나 그대로 쓴다):
      VM_URL  <victoriametrics ClusterIP>:8428     CH_URL <clickhouse ClusterIP>:8123
      OBSERVER_HEALTH_URL <ai-observer ClusterIP>:8087/api/v1/health
      PG_HOST <pg-rw ClusterIP>, PG_PORT 5432
      PG_USER, PG_PASSWORD  PG_PASSWORD 를 안 주면 시크릿 pg-superuser 의 username, password
      CH_PASSWORD           안 주면 시크릿 polestar-secret 의 CLICKHOUSE_PASSWORD
    PG 사이드카는 ClusterIP 에 닿도록 --network host 로 돈다(restore-golden-state.sh 와 같다).
    K8S_NAMESPACE polestar, TRAINER_WORKLOAD deployment/ai-trainer,
    VM_SERVICE victoriametrics, CH_SERVICE clickhouse, OBSERVER_SERVICE ai-observer,
    PG_SERVICE pg-rw, PG_SUPERUSER_SECRET pg-superuser, CH_SECRET polestar-secret,
    KUBECTL 비우면 'kubectl' 다음 'sudo -n kubectl'.
    /opt/lucida/golden.env 의 비밀번호는 compose 시절 값이라 k8s 에서는 맞지 않는다(2026-11-12 실측
    PG, CH 모두 인증 실패). k8s 캡처는 그 파일을 읽지 않고 돌린다.
EOF
}

die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }
log() { printf '[INFO] %s %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"; }
require_command() { command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"; }

iso_utc_from_epoch() { date -u -d "@$1" +'%Y-%m-%dT%H:%M:%SZ'; }
kst_from_epoch() { TZ='Asia/Seoul' date -d "@$1" +'%Y-%m-%dT%H:%M:%S+09:00'; }

# The golden AI-state contract: trainer-produced models + observer runtime state +
# coverage/registry. Explicit list (not a pattern) so the golden is reproducible
# and reviewable. Row counts are recorded into meta.json for verification.
AI_STATE_TABLES=(
  trainer_forecast_conformal
  trainer_forecast_model_route
  trainer_forecast_season
  trainer_masking_rules
  trainer_periodic_templates
  trainer_stream_distribution_profile
  trainer_stream_periodic_profile
  trainer_stream_snapshot_history
  trainer_stream_threshold
  trainer_template_merges
  trainer_template_profiles
  trainer_thresholds
  trainer_trace_threshold
  ai_model_state_snapshots
  detector_seen_signatures
  log_drain_templates
  ai_coverages
  coverage_events
  coverage_signal_state
  ai_model_registry
  ai_model_bindings
  ai_model_versions
  ai_model_evaluations
  # 2026-11-12(v4 E11): 119 develop-ai 판의 세대 기록과 그 세대에 묶인 산출물.
  # stream 은 trainer_stream_generations 의 활성 세대 한 행이 periodic, threshold,
  # distribution, drift 네 표의 그 세대 행 수를 정하고, observer 는 넷이 그 수와 맞을 때만
  # 적재한다(trainer_load.go). 세대 기록 없이 네 표만 되돌리면 활성 세대 행이 0 이라 적재를
  # 거절하고, 세대 기록만 되돌리고 drift 를 두면 다음 세대 번호(MAX+1)가 남은 drift 행과
  # 겹친다. 그래서 세대 기록과 drift 를 함께 담는다. log 도 같은 꼴(trainer_log_generations).
  # seed_verdicts, prompts 는 log trainer 산출물이다. trainer_trace_window_threshold 는
  # 15분마다 24시간 원천으로 새 판을 만들고 3GB 넘는 옛 판을 쌓아 두는 표라 담지 않는다.
  trainer_stream_generations
  trainer_stream_drift
  trainer_log_generations
  trainer_seed_verdicts
  trainer_prompts
)

window_minutes=120
label=''
output_root="${GOLDEN_ROOT:-/data/eval-cases/goldens}"
dry_run=false
skip_freshness=false

while (( $# > 0 )); do
  case "$1" in
    --window-minutes) window_minutes="${2:-}"; shift 2 ;;
    --label) label="${2:-}"; shift 2 ;;
    --output-root) output_root="${2:-}"; shift 2 ;;
    --dry-run) dry_run=true; shift ;;
    --skip-freshness) skip_freshness=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

require_command date
require_command jq

[[ "$window_minutes" =~ ^[0-9]+$ ]] && (( window_minutes > 0 )) ||
  die "--window-minutes must be a positive integer: $window_minutes"

now_epoch=$(date -u +%s)
start_epoch=$(( now_epoch - window_minutes * 60 ))
window_start=$(iso_utc_from_epoch "$start_epoch")
window_end=$(iso_utc_from_epoch "$now_epoch")
window_start_kst=$(kst_from_epoch "$start_epoch")
window_end_kst=$(kst_from_epoch "$now_epoch")
# tod_phase = capture time-of-day (KST HH:MM), used by restore for nearest match.
tod_phase=$(TZ='Asia/Seoul' date -d "@$now_epoch" +'%H:%M')
capture_compact=$(date -u -d "@$now_epoch" +'%Y-%m-%dT%H%M%SZ')
final_dir="${output_root%/}/$capture_compact"

if [[ "$dry_run" == true ]]; then
  jq -n \
    --arg window_start "$window_start" --arg window_end "$window_end" \
    --arg tod_phase "$tod_phase" --arg final_dir "$final_dir" \
    --argjson window_minutes "$window_minutes" \
    --argjson n_tables "${#AI_STATE_TABLES[@]}" \
    '{mode:"dry-run", kind:"golden", time_basis:"UTC",
      window_minutes:$window_minutes, window_start:$window_start,
      window_end:$window_end, tod_phase_kst:$tod_phase, final_dir:$final_dir,
      pg_ai_state_tables:$n_tables,
      stores:["postgres.ai-state.dump","victoriametrics.export","clickhouse/*.parquet"]}'
  exit 0
fi

require_command curl
require_command docker

# ------------------------------------------------------------
# 런타임 (2026-11-12, v4 E11). restore-golden-state.sh 의 k8s 대응(2026-08-13)과 같은 길이다.
# 아래 기본값(119 호스트 포트)은 compose 평면의 것이라 k8s 인 119 자신에서는 닿지 않는다
# (2026-11-12 실측: 119 에서 18428, 18123, 15432, 18087 모두 접속 불가, ClusterIP 는 모두 응답).
# k8s 에서는 주지 않은 주소와 비밀번호만 클러스터에서 채운다. 준 값은 언제나 그대로 쓴다.
# ------------------------------------------------------------
RUNTIME="${RUNTIME:-auto}"
K8S_NAMESPACE="${K8S_NAMESPACE:-polestar}"
TRAINER_WORKLOAD="${TRAINER_WORKLOAD:-deployment/ai-trainer}"
KUBECTL="${KUBECTL:-}"

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

if [[ "$RUNTIME" == auto ]]; then
  # 캡처는 워크로드를 멈추지 않으므로 docker 컨테이너 유무는 보지 않는다. k8s 가 아니면
  # 예전 평면(호스트 포트 기본값)으로 간다.
  if [[ -n "$KUBECTL" ]] && $KUBECTL -n "$K8S_NAMESPACE" get "$TRAINER_WORKLOAD" >/dev/null 2>&1; then
    RUNTIME=k8s
  else
    RUNTIME=docker
  fi
fi
case "$RUNTIME" in
  docker) ;;
  k8s) [[ -n "$KUBECTL" ]] || die "RUNTIME=k8s but no usable kubectl (tried 'kubectl' and 'sudo -n kubectl')" ;;
  *) die "unknown RUNTIME: $RUNTIME (docker|k8s|auto)" ;;
esac
log "runtime=$RUNTIME${KUBECTL:+ kubectl='$KUBECTL'} namespace=$K8S_NAMESPACE"

pg_net_args=()
if [[ "$RUNTIME" == k8s ]]; then
  k8s_svc_ip() {
    local ip
    ip=$($KUBECTL -n "$K8S_NAMESPACE" get svc "$1" -o jsonpath='{.spec.clusterIP}' 2>/dev/null) || ip=''
    [[ -n "$ip" && "$ip" != None ]] || die "cannot resolve service ClusterIP: $K8S_NAMESPACE/$1"
    printf '%s' "$ip"
  }
  k8s_secret_value() {  # 값은 변수로만 옮긴다. 화면, 로그에 내지 않는다.
    # 못 읽으면 빈 값이다(pipefail 로 대입이 실패해 set -e 가 말없이 끝내지 않게). 빈 값은 호출부가 죽인다.
    $KUBECTL -n "$K8S_NAMESPACE" get secret "$1" -o "jsonpath={.data.$2}" 2>/dev/null | base64 -d 2>/dev/null || true
  }
  if [[ -z "${VM_URL:-}" ]]; then
    VM_URL="http://$(k8s_svc_ip "${VM_SERVICE:-victoriametrics}"):8428"
    log "resolved VictoriaMetrics -> $VM_URL"
  fi
  if [[ -z "${CH_URL:-}" ]]; then
    CH_URL="http://$(k8s_svc_ip "${CH_SERVICE:-clickhouse}"):8123"
    log "resolved ClickHouse -> $CH_URL"
  fi
  if [[ -z "${OBSERVER_HEALTH_URL:-}" ]]; then
    OBSERVER_HEALTH_URL="http://$(k8s_svc_ip "${OBSERVER_SERVICE:-ai-observer}"):8087/api/v1/health"
    log "resolved observer health -> $OBSERVER_HEALTH_URL"
  fi
  if [[ -z "${PG_HOST:-}" ]]; then
    PG_HOST="$(k8s_svc_ip "${PG_SERVICE:-pg-rw}")"
    PG_PORT="${PG_PORT:-5432}"
    log "resolved PG -> $PG_HOST:$PG_PORT"
  fi
  if [[ -z "${PG_PASSWORD:-}" ]]; then
    PG_SUPERUSER_SECRET="${PG_SUPERUSER_SECRET:-pg-superuser}"
    _pg_u=$(k8s_secret_value "$PG_SUPERUSER_SECRET" username)
    _pg_p=$(k8s_secret_value "$PG_SUPERUSER_SECRET" password)
    [[ -n "$_pg_u" && -n "$_pg_p" ]] ||
      die "PG_PASSWORD not given and k8s secret $K8S_NAMESPACE/$PG_SUPERUSER_SECRET is not readable"
    PG_USER="$_pg_u"
    PG_PASSWORD="$_pg_p"
    unset _pg_u _pg_p
    log "resolved PG credentials from k8s secret $PG_SUPERUSER_SECRET (user=$PG_USER)"
  fi
  if [[ -z "${CH_PASSWORD:-}" ]]; then
    CH_SECRET="${CH_SECRET:-polestar-secret}"
    CH_PASSWORD=$(k8s_secret_value "$CH_SECRET" CLICKHOUSE_PASSWORD)
    [[ -n "$CH_PASSWORD" ]] ||
      die "CH_PASSWORD not given and k8s secret $K8S_NAMESPACE/$CH_SECRET has no CLICKHOUSE_PASSWORD"
    log "resolved ClickHouse password from k8s secret $CH_SECRET"
  fi
  # ClusterIP 는 노드의 호스트 라우팅으로만 닿는다. 기본 bridge 의 사이드카는 못 붙는다.
  pg_net_args=(--network host)
fi

VM_URL="${VM_URL:-http://192.168.230.119:18428}"
CH_URL="${CH_URL:-http://192.168.230.119:18123}"
CH_USER="${CH_USER:-lucida}"
CH_PASSWORD="${CH_PASSWORD:-}"
PG_HOST="${PG_HOST:-192.168.230.119}"
PG_PORT="${PG_PORT:-15432}"
PG_USER="${PG_USER:-lucida}"
PG_PASSWORD="${PG_PASSWORD:-}"
PG_DATABASE="${PG_DATABASE:-lucida}"
PG_DUMP_IMAGE="${PG_DUMP_IMAGE:-postgres:17-alpine}"
OBSERVER_HEALTH_URL="${OBSERVER_HEALTH_URL:-http://192.168.230.119:18087/api/v1/health}"
FRESHNESS_MAX_LAG_SEC="${FRESHNESS_MAX_LAG_SEC:-600}"
CAPTURE_HOST_OUTPUT_ROOT="${CAPTURE_HOST_OUTPUT_ROOT:-}"

[[ -n "$PG_PASSWORD" ]] || die 'PG_PASSWORD is required for a live golden capture'
[[ ! -e "$final_dir" ]] || die "golden already exists and is immutable: $final_dir"

ch_query() {
  local query=$1 credential
  if [[ -n "$CH_USER" ]]; then
    credential="${CH_USER}:${CH_PASSWORD}"
    credential=${credential//\\/\\\\}
    credential=${credential//\"/\\\"}
    printf 'user = "%s"\n' "$credential" |
      curl --config - --fail --silent --show-error --data-binary "$query" "${CH_URL%/}/"
    return
  fi
  curl --fail --silent --show-error --data-binary "$query" "${CH_URL%/}/"
}

ch_export() {
  local table=$1 query=$2 output=$3 credential
  log "exporting ClickHouse table=$table"
  if [[ -n "$CH_USER" ]]; then
    credential="${CH_USER}:${CH_PASSWORD}"
    credential=${credential//\\/\\\\}
    credential=${credential//\"/\\\"}
    printf 'user = "%s"\n' "$credential" |
      curl --config - --fail --silent --show-error --data-binary "$query" "${CH_URL%/}/" --output "$output"
    return
  fi
  curl --fail --silent --show-error --data-binary "$query" "${CH_URL%/}/" --output "$output"
}

# ------------------------------------------------------------
# Lightweight freshness gate (spec §2.1 convention): the collection stack is
# alive and ingesting. This confirms the golden is being taken against a live
# stack; it does NOT assert the window is fault-free (operator's responsibility).
# ------------------------------------------------------------
freshness_gate() {
  local newest lag
  log 'freshness gate: 119 observer (AI layer) health'
  curl --fail --silent --show-error --max-time 10 --output /dev/null "$OBSERVER_HEALTH_URL" ||
    die 'freshness gate failed: 119 observer health endpoint is not OK'

  log 'freshness gate: VictoriaMetrics recent ingest'
  newest=$(curl --fail --silent --show-error --get "${VM_URL%/}/api/v1/query" \
    --data-urlencode 'query=timestamp(vm_rows)' 2>/dev/null \
    | jq -r '[.data.result[].value[1] | tonumber] | max // 0')
  [[ -n "$newest" ]] || die 'freshness gate failed: VictoriaMetrics returned no samples'
  lag=$(( now_epoch - ${newest%.*} ))
  (( lag <= FRESHNESS_MAX_LAG_SEC )) ||
    die "freshness gate failed: VictoriaMetrics newest sample lag ${lag}s > ${FRESHNESS_MAX_LAG_SEC}s"

  log 'freshness gate: ClickHouse recent ingest'
  newest=$(ch_query 'SELECT toUnixTimestamp(max(timestamp)) FROM lucida.otel_traces_local') || newest=''
  [[ "$newest" =~ ^[0-9]+$ ]] || die 'freshness gate failed: ClickHouse returned no recent traces'
  lag=$(( now_epoch - newest ))
  (( lag <= FRESHNESS_MAX_LAG_SEC )) ||
    die "freshness gate failed: ClickHouse newest trace lag ${lag}s > ${FRESHNESS_MAX_LAG_SEC}s"

  log 'freshness gate: PostgreSQL auth'
  PGPASSWORD="$PG_PASSWORD" docker run --rm "${pg_net_args[@]}" --env PGPASSWORD "$PG_DUMP_IMAGE" \
    psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DATABASE" -tAc 'SELECT 1' >/dev/null ||
    die 'freshness gate failed: PostgreSQL auth/connect'

  log 'freshness gate: passed'
}

if [[ "$skip_freshness" != true ]]; then
  freshness_gate
else
  log 'freshness gate: skipped (--skip-freshness)'
fi

mkdir -p "$output_root"
staging_dir=$(mktemp -d "${output_root%/}/.${capture_compact}.staging.XXXXXX")
mkdir -p "$staging_dir/data/clickhouse"
cleanup() { [[ -n "${staging_dir:-}" && -d "$staging_dir" ]] && rm -rf "$staging_dir"; }
trap cleanup EXIT

captured_at=$(iso_utc_from_epoch "$now_epoch")

# --- normal window: VictoriaMetrics + ClickHouse (for continuity splice) ---
log "exporting VictoriaMetrics normal window [$window_start, $window_end]"
curl --fail --silent --show-error --get "${VM_URL%/}/api/v1/export" \
  --data-urlencode 'match[]={__name__!=""}' \
  --data-urlencode "start=$window_start" \
  --data-urlencode "end=$window_end" \
  --output "$staging_dir/data/victoriametrics.export"

ch_start="parseDateTime64BestEffort('$window_start')"
ch_end="parseDateTime64BestEffort('$window_end')"
ch_export otel_traces_local \
  "SELECT * FROM lucida.otel_traces_local WHERE timestamp >= $ch_start AND timestamp <= $ch_end FORMAT Parquet" \
  "$staging_dir/data/clickhouse/otel_traces_local.parquet"
ch_export lucida_logs_local \
  "SELECT * FROM lucida.lucida_logs_local WHERE timestamp >= $ch_start AND timestamp <= $ch_end FORMAT Parquet" \
  "$staging_dir/data/clickhouse/lucida_logs_local.parquet"
ch_export lucida_events_local \
  "SELECT * REPLACE(toString(event_id) AS event_id, toString(episode_id) AS episode_id) FROM lucida.lucida_events_local WHERE occurred_at >= $ch_start AND occurred_at <= $ch_end FORMAT Parquet" \
  "$staging_dir/data/clickhouse/lucida_events_local.parquet"

# --- AI learned state: PostgreSQL selective dump (trainer/observer/coverage) ---
log "dumping PostgreSQL AI-state (${#AI_STATE_TABLES[@]} tables)"

# pg_dump 는 자기보다 새 메이저 서버를 "server version mismatch" 로 거절한다(2026-11-10 실측: 16 클라이언트 →
# 17 서버 거절, 17 클라이언트 → 16, 17 서버 성공). pg_restore 도 자기보다 새 pg_dump 가 만든 덤프를 읽지 못한다.
# 119 PG 가 17 로 올라간 뒤 16 도구로 붙던 일을 덤프 전에 막는다. 인자는 docker run 에 더할 인자(네트워크 등).
pg_tool_major_check() {
  local client server
  # 다른 PG 단계처럼 시간 상한 안에서 돈다. 주소가 막혀도 TCP 재시도만큼 멈추지 않게 접속 상한도 건다.
  local limit="${PG_TOOL_CHECK_TIMEOUT_SEC:-60}"
  client=$(timeout --kill-after=10 "$limit" docker run --rm "$@" "$PG_DUMP_IMAGE" pg_dump --version 2>/dev/null |
    grep -oE '[0-9]+' | head -1)
  server=$(PGPASSWORD="$PG_PASSWORD" timeout --kill-after=10 "$limit" docker run --rm "$@" --env PGPASSWORD \
    --env PGCONNECT_TIMEOUT="${PG_TOOL_CHECK_CONNECT_TIMEOUT_SEC:-10}" "$PG_DUMP_IMAGE" \
    psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DATABASE" -tAc 'SHOW server_version_num' 2>/dev/null |
    tr -d '[:space:]')
  if [[ ! "$client" =~ ^[0-9]+$ || ! "$server" =~ ^[0-9]+$ ]]; then
    printf 'PG 도구, 서버 메이저를 읽지 못했다(PG_DUMP_IMAGE=%s client=%s server_version_num=%s)\n' \
      "$PG_DUMP_IMAGE" "${client:-?}" "${server:-?}"
    return 1
  fi
  if (( client < server / 10000 )); then
    printf 'PG_DUMP_IMAGE=%s 의 PG 도구 %s 은 서버 %s 보다 오래돼 덤프, 복원을 못 한다. postgres:%s-alpine 이상으로\n' \
      "$PG_DUMP_IMAGE" "$client" "$(( server / 10000 ))" "$(( server / 10000 ))"
    return 1
  fi
}
pg_major_msg=$(pg_tool_major_check "${pg_net_args[@]}") || die "$pg_major_msg"

pg_dump_data_dir="$staging_dir/data"
if [[ -n "$CAPTURE_HOST_OUTPUT_ROOT" ]]; then
  pg_dump_data_dir="${CAPTURE_HOST_OUTPUT_ROOT%/}/${staging_dir##*/}/data"
fi
pg_dump_table_args=()
for t in "${AI_STATE_TABLES[@]}"; do
  pg_dump_table_args+=(--table "public.$t")
done
PGPASSWORD="$PG_PASSWORD" docker run --rm "${pg_net_args[@]}" \
  --user "$(id -u):$(id -g)" \
  --env PGPASSWORD \
  --volume "$pg_dump_data_dir:/out" \
  "$PG_DUMP_IMAGE" \
  pg_dump -Fc --host "$PG_HOST" --port "$PG_PORT" --username "$PG_USER" \
  --dbname "$PG_DATABASE" "${pg_dump_table_args[@]}" --file /out/golden-pg-state.dump

# --- verify artifacts present + non-empty ---
required_files=(
  data/victoriametrics.export
  data/clickhouse/otel_traces_local.parquet
  data/clickhouse/lucida_logs_local.parquet
  data/clickhouse/lucida_events_local.parquet
  data/golden-pg-state.dump
)
for relative in "${required_files[@]}"; do
  [[ -s "$staging_dir/$relative" ]] || die "golden artifact is missing or empty: $relative"
done

# --- row counts of AI-state tables (verification aid in meta) ---
log 'collecting AI-state row counts'
row_counts_json='{}'
counts_sql="SELECT relname, n_live_tup FROM pg_stat_user_tables WHERE relname IN ("
first=true
for t in "${AI_STATE_TABLES[@]}"; do
  if $first; then counts_sql+="'$t'"; first=false; else counts_sql+=",'$t'"; fi
done
counts_sql+=") ORDER BY relname"
if counts_tsv=$(PGPASSWORD="$PG_PASSWORD" docker run --rm "${pg_net_args[@]}" --env PGPASSWORD "$PG_DUMP_IMAGE" \
    psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DATABASE" -F $'\t' -tAc "$counts_sql" 2>/dev/null); then
  row_counts_json=$(printf '%s\n' "$counts_tsv" | jq -R -s 'split("\n") | map(select(length>0) | split("\t")) | map({(.[0]): (.[1]|tonumber)}) | add // {}')
fi

dump_sha=$(sha256sum "$staging_dir/data/golden-pg-state.dump" | cut -d' ' -f1)

jq -n \
  --arg schema_version '1.0' \
  --arg captured_at "$captured_at" --arg captured_at_kst "$(kst_from_epoch "$now_epoch")" \
  --arg tod_phase "$tod_phase" \
  --arg window_start "$window_start" --arg window_end "$window_end" \
  --arg window_start_kst "$window_start_kst" --arg window_end_kst "$window_end_kst" \
  --argjson window_minutes "$window_minutes" \
  --arg label_text "$label" \
  --arg dump_sha "$dump_sha" \
  --argjson ai_state_tables "$(printf '%s\n' "${AI_STATE_TABLES[@]}" | jq -R . | jq -s .)" \
  --argjson row_counts "$row_counts_json" \
  '{schema_version:$schema_version, kind:"golden", time_basis:"UTC",
    captured_at:$captured_at, captured_at_kst:$captured_at_kst,
    tod_phase_kst:$tod_phase,
    window_minutes:$window_minutes,
    window_start:$window_start, window_end:$window_end,
    window_start_kst:$window_start_kst, window_end_kst:$window_end_kst,
    label:(if $label_text == "" then null else $label_text end),
    pg_ai_state:{dump:"data/golden-pg-state.dump", sha256:$dump_sha,
                 tables:$ai_state_tables, row_counts:$row_counts},
    stores:["postgres.ai-state.dump","victoriametrics.export","clickhouse/*.parquet"],
    model_note:"stream-anomaly model.json is a static human-seeded default (not part of golden); trainer models are in the PG dump"}' \
  > "$staging_dir/meta.json"

mkdir -p "$(dirname "$final_dir")"
[[ ! -e "$final_dir" ]] || die "golden appeared during capture: $final_dir"
mv -T "$staging_dir" "$final_dir"
staging_dir=''
trap - EXIT
log "golden complete: $final_dir (tod=$tod_phase KST, window=${window_minutes}m)"
