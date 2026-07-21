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
  PG_DUMP_IMAGE  default: postgres:16-alpine
Freshness gate:
  OBSERVER_HEALTH_URL   default: http://192.168.230.119:18087/api/v1/health
  FRESHNESS_MAX_LAG_SEC default: 600
Container/host path mapping (when this script runs inside a container and the
pg_dump sidecar needs the host path of the staging dir):
  CAPTURE_HOST_OUTPUT_ROOT   default: '' (unset = same path)
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

VM_URL="${VM_URL:-http://192.168.230.119:18428}"
CH_URL="${CH_URL:-http://192.168.230.119:18123}"
CH_USER="${CH_USER:-lucida}"
CH_PASSWORD="${CH_PASSWORD:-}"
PG_HOST="${PG_HOST:-192.168.230.119}"
PG_PORT="${PG_PORT:-15432}"
PG_USER="${PG_USER:-lucida}"
PG_PASSWORD="${PG_PASSWORD:-}"
PG_DATABASE="${PG_DATABASE:-lucida}"
PG_DUMP_IMAGE="${PG_DUMP_IMAGE:-postgres:16-alpine}"
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
  PGPASSWORD="$PG_PASSWORD" docker run --rm --env PGPASSWORD "$PG_DUMP_IMAGE" \
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
pg_dump_data_dir="$staging_dir/data"
if [[ -n "$CAPTURE_HOST_OUTPUT_ROOT" ]]; then
  pg_dump_data_dir="${CAPTURE_HOST_OUTPUT_ROOT%/}/${staging_dir##*/}/data"
fi
pg_dump_table_args=()
for t in "${AI_STATE_TABLES[@]}"; do
  pg_dump_table_args+=(--table "public.$t")
done
PGPASSWORD="$PG_PASSWORD" docker run --rm \
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
if counts_tsv=$(PGPASSWORD="$PG_PASSWORD" docker run --rm --env PGPASSWORD "$PG_DUMP_IMAGE" \
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
  --arg label "$label" \
  --arg dump_sha "$dump_sha" \
  --argjson ai_state_tables "$(printf '%s\n' "${AI_STATE_TABLES[@]}" | jq -R . | jq -s .)" \
  --argjson row_counts "$row_counts_json" \
  '{schema_version:$schema_version, kind:"golden", time_basis:"UTC",
    captured_at:$captured_at, captured_at_kst:$captured_at_kst,
    tod_phase_kst:$tod_phase,
    window_minutes:$window_minutes,
    window_start:$window_start, window_end:$window_end,
    window_start_kst:$window_start_kst, window_end_kst:$window_end_kst,
    label:(if $label == "" then null else $label end),
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
