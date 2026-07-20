#!/usr/bin/env bash
# Dump one day's shared normal segment (spec-eval-data-capture §2.1/§4, 2026-07-20).
#
# The protection window 00:00~02:00 KST is dumped once per day as the shared
# normal prefix for that day's scenario captures. Only the time-series stores
# that the assembled/ merge consumes are dumped (VM export + ClickHouse
# parquet); PostgreSQL, model artifacts and topology belong to the scenario
# segment and are not part of the normal segment.
#
# Before dumping, a *lightweight* gate confirms the collection stack is alive
# and fresh (agents/collector/119 observer up + recent data in each store).
# It deliberately does NOT assert "no alarm / no incident" over the window.

set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage:
  dump-normal-segment.sh --domain <commerce|food-delivery|core-banking> \
    [--date <YYYY-MM-DD KST, default today>] \
    [--baseline-rps <n>] [--loadgen-seed <n>] [--testbed-commit <sha>] \
    [--output-root <normal-segments dir>] [--dry-run] [--skip-freshness]

The KST protection window is [<date> 00:00, <date> 02:00) Asia/Seoul, stored as
UTC in meta.json (segment_start/segment_end) with a KST companion.

Required environment for a live dump (same conventions as capture-eval-case.sh):
  VM_URL         default: http://192.168.230.119:18428
  CH_URL         default: http://192.168.230.119:18123
  CH_USER        default: lucida
  CH_PASSWORD    required unless the ClickHouse endpoint allows anonymous access
Freshness gate:
  OBSERVER_HEALTH_URL   default: http://192.168.230.119:18087/api/v1/health
  FRESHNESS_MAX_LAG_SEC default: 600 (each store's newest datapoint must be
                        within this many seconds of now)
EOF
}

die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }
log() { printf '[INFO] %s %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"; }
require_command() { command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"; }

iso_utc_from_epoch() { date -u -d "@$1" +'%Y-%m-%dT%H:%M:%SZ'; }
kst_from_epoch() { TZ='Asia/Seoul' date -d "@$1" +'%Y-%m-%dT%H:%M:%S+09:00'; }

domain=''
date_kst=''
baseline_rps=''
loadgen_seed=''
testbed_commit=''
output_root="${NORMAL_SEGMENT_ROOT:-/data/eval-cases/normal-segments}"
dry_run=false
skip_freshness=false

while (( $# > 0 )); do
  case "$1" in
    --domain) domain="${2:-}"; shift 2 ;;
    --date) date_kst="${2:-}"; shift 2 ;;
    --baseline-rps) baseline_rps="${2:-}"; shift 2 ;;
    --loadgen-seed) loadgen_seed="${2:-}"; shift 2 ;;
    --testbed-commit) testbed_commit="${2:-}"; shift 2 ;;
    --output-root) output_root="${2:-}"; shift 2 ;;
    --dry-run) dry_run=true; shift ;;
    --skip-freshness) skip_freshness=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

require_command date
require_command jq

[[ "$domain" =~ ^(commerce|food-delivery|core-banking)$ ]] ||
  die '--domain must be one of: commerce, food-delivery, core-banking'
[[ -z "$date_kst" ]] && date_kst=$(TZ='Asia/Seoul' date +'%Y-%m-%d')
[[ "$date_kst" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || die "--date must be YYYY-MM-DD: $date_kst"

# Protection window [00:00, 02:00) KST -> UTC epochs.
start_epoch=$(TZ='Asia/Seoul' date -d "$date_kst 00:00:00" +%s 2>/dev/null) ||
  die "invalid --date: $date_kst"
end_epoch=$(( start_epoch + 2 * 60 * 60 ))
segment_start=$(iso_utc_from_epoch "$start_epoch")
segment_end=$(iso_utc_from_epoch "$end_epoch")
segment_start_kst=$(kst_from_epoch "$start_epoch")
segment_end_kst=$(kst_from_epoch "$end_epoch")
final_dir="${output_root%/}/$domain/$date_kst"

if [[ "$dry_run" == true ]]; then
  jq -n \
    --arg domain "$domain" --arg date "$date_kst" \
    --arg segment_start "$segment_start" --arg segment_end "$segment_end" \
    --arg segment_start_kst "$segment_start_kst" --arg segment_end_kst "$segment_end_kst" \
    --arg final_dir "$final_dir" \
    '{mode:"dry-run", domain:$domain, date:$date, time_basis:"UTC",
      tod_phase:"00:00-02:00 KST", segment_start:$segment_start,
      segment_end:$segment_end, segment_start_kst:$segment_start_kst,
      segment_end_kst:$segment_end_kst, final_dir:$final_dir,
      stores:["victoriametrics.export","clickhouse/*.parquet"]}'
  exit 0
fi

require_command curl

VM_URL="${VM_URL:-http://192.168.230.119:18428}"
CH_URL="${CH_URL:-http://192.168.230.119:18123}"
CH_USER="${CH_USER:-lucida}"
CH_PASSWORD="${CH_PASSWORD:-}"
OBSERVER_HEALTH_URL="${OBSERVER_HEALTH_URL:-http://192.168.230.119:18087/api/v1/health}"
FRESHNESS_MAX_LAG_SEC="${FRESHNESS_MAX_LAG_SEC:-600}"

[[ ! -e "$final_dir" ]] || die "normal segment already exists and is immutable: $final_dir"

ch_query() {
  # Run a scalar ClickHouse query, credentials fed via curl stdin config.
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

# ------------------------------------------------------------
# Lightweight freshness gate (spec §2.1): collection stack alive + recent data.
# Gate failure means the day's normal segment is not dumped (caller retries or
# skips the day); it is NOT an alarm/incident audit.
# ------------------------------------------------------------
freshness_gate() {
  local now_epoch newest lag
  now_epoch=$(date -u +%s)

  log 'freshness gate: 119 observer (AI layer) health'
  curl --fail --silent --show-error --max-time 10 --output /dev/null "$OBSERVER_HEALTH_URL" ||
    die 'freshness gate failed: 119 observer health endpoint is not OK'

  log 'freshness gate: VictoriaMetrics recent ingest'
  # newest sample timestamp (seconds) across all series.
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

  log 'freshness gate: passed'
}

if [[ "$skip_freshness" != true ]]; then
  freshness_gate
else
  log 'freshness gate: skipped (--skip-freshness)'
fi

mkdir -p "$output_root"
staging_dir=$(mktemp -d "${output_root%/}/.${domain}-${date_kst}.staging.XXXXXX")
mkdir -p "$staging_dir/data/clickhouse"
cleanup() { [[ -n "${staging_dir:-}" && -d "$staging_dir" ]] && rm -rf "$staging_dir"; }
trap cleanup EXIT

captured_at=$(date -u +'%Y-%m-%dT%H:%M:%SZ')

log "exporting VictoriaMetrics normal window [$segment_start, $segment_end]"
curl --fail --silent --show-error --get "${VM_URL%/}/api/v1/export" \
  --data-urlencode 'match[]={__name__!=""}' \
  --data-urlencode "start=$segment_start" \
  --data-urlencode "end=$segment_end" \
  --output "$staging_dir/data/victoriametrics.export"

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

ch_start="parseDateTime64BestEffort('$segment_start')"
ch_end="parseDateTime64BestEffort('$segment_end')"
ch_export otel_traces_local \
  "SELECT * FROM lucida.otel_traces_local WHERE timestamp >= $ch_start AND timestamp <= $ch_end FORMAT Parquet" \
  "$staging_dir/data/clickhouse/otel_traces_local.parquet"
ch_export lucida_logs_local \
  "SELECT * FROM lucida.lucida_logs_local WHERE timestamp >= $ch_start AND timestamp <= $ch_end FORMAT Parquet" \
  "$staging_dir/data/clickhouse/lucida_logs_local.parquet"
ch_export lucida_events_local \
  "SELECT * REPLACE(toString(event_id) AS event_id, toString(episode_id) AS episode_id) FROM lucida.lucida_events_local WHERE occurred_at >= $ch_start AND occurred_at <= $ch_end FORMAT Parquet" \
  "$staging_dir/data/clickhouse/lucida_events_local.parquet"

required_files=(
  data/victoriametrics.export
  data/clickhouse/otel_traces_local.parquet
  data/clickhouse/lucida_logs_local.parquet
  data/clickhouse/lucida_events_local.parquet
)
for relative in "${required_files[@]}"; do
  [[ -s "$staging_dir/$relative" ]] || die "normal segment artifact is missing or empty: $relative"
done

jq -n \
  --arg schema_version '1.0' \
  --arg domain "$domain" --arg date "$date_kst" \
  --arg segment_start "$segment_start" --arg segment_end "$segment_end" \
  --arg segment_start_kst "$segment_start_kst" --arg segment_end_kst "$segment_end_kst" \
  --arg captured_at "$captured_at" --arg captured_at_kst "$(kst_from_epoch "$(date -d "$captured_at" +%s)")" \
  --arg tod_phase '00:00-02:00 KST' \
  --arg baseline_rps "$baseline_rps" --arg loadgen_seed "$loadgen_seed" \
  --arg testbed_commit "$testbed_commit" \
  '{schema_version:$schema_version, kind:"normal-segment", domain:$domain, date:$date,
    time_basis:"UTC", tod_phase:$tod_phase,
    segment_start:$segment_start, segment_end:$segment_end,
    segment_start_kst:$segment_start_kst, segment_end_kst:$segment_end_kst,
    captured_at:$captured_at, captured_at_kst:$captured_at_kst,
    baseline_rps:(if $baseline_rps == "" then null else ($baseline_rps|tonumber) end),
    loadgen_seed:(if $loadgen_seed == "" then null else ($loadgen_seed|tonumber) end),
    testbed_commit:(if $testbed_commit == "" then null else $testbed_commit end)}' \
  > "$staging_dir/meta.json"

mkdir -p "$(dirname "$final_dir")"
[[ ! -e "$final_dir" ]] || die "normal segment appeared during dump: $final_dir"
mv -T "$staging_dir" "$final_dir"
staging_dir=''
trap - EXIT
log "normal segment complete: $final_dir"
