#!/usr/bin/env bash
# Capture one immutable evaluation case after a scenario injection.
# Contract: docs/spec-eval-data-capture.md §2.1/§4.

set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage:
  capture-eval-case.sh --case-id <case-slug> --scenario-id <domain/id> \
    --scenario-title <title> --scenario-description <description> \
    --scenario-cause <cause> --scenario-injection-summary <summary> \
    --scenario-user-impact <impact> --scenario-distinguishing-evidence <evidence> \
    --scenario-metadata-sha256 <sha256> \
    --t1 <YYYY-MM-DDTHH:MM:SSZ> --t2 <YYYY-MM-DDTHH:MM:SSZ> \
    [--case-label <calibration|evaluation|failed>] [--run-result <result.json>] \
    [--output-root <dir>] [--dry-run]

Case labels:
  calibration       tuning/reproducibility capture; not used for scoring (default)
  evaluation        immutable capture eligible for scoring
  failed            diagnostic capture from a failed scenario run; not used for scoring

Required environment for a live capture:
  VM_URL             default: http://192.168.230.119:18428
  CH_URL             default: http://192.168.230.119:18123
  CH_USER            default: lucida
  CH_PASSWORD        required unless the ClickHouse endpoint allows anonymous access
  PG_HOST            default: 192.168.230.119
  PG_PORT            default: 15432
  PG_USER            default: lucida
  PG_PASSWORD        required
  PG_DATABASE        default: lucida
  CAPTURE_HOST_OUTPUT_ROOT
                      host path backing output_root when using host Docker socket

Model snapshot source (one must be accessible):
  MODEL_SOURCE       default: /var/lib/lucida/ai-models/stream-anomaly/global/v1/model.json
  MODEL_CONTAINER    default: lucida-ai-observer; used when MODEL_SOURCE is not readable locally

The live command waits until t2+20m, captures [t1-10m,t2+20m], validates every
artifact, then atomically renames the staging directory into the final case path.
EOF
}

die() {
  printf '[ERROR] %s\n' "$*" >&2
  exit 1
}

log() {
  printf '[INFO] %s %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

iso_utc_from_epoch() {
  date -u -d "@$1" +'%Y-%m-%dT%H:%M:%SZ'
}

parse_utc_epoch() {
  local raw=$1 name=$2 epoch normalized
  [[ "$raw" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] ||
    die "$name must be UTC in YYYY-MM-DDTHH:MM:SSZ form: $raw"
  epoch=$(date -u -d "$raw" +%s 2>/dev/null) || die "invalid $name timestamp: $raw"
  normalized=$(iso_utc_from_epoch "$epoch")
  [[ "$normalized" == "$raw" ]] || die "invalid $name timestamp: $raw"
  printf '%s\n' "$epoch"
}

case_id=''
scenario_id=''
scenario_title=''
scenario_description=''
scenario_cause=''
scenario_injection_summary=''
scenario_user_impact=''
scenario_distinguishing_evidence=''
scenario_metadata_sha256=''
t1_raw=''
t2_raw=''
output_root="${EVAL_CASE_ROOT:-/data/eval-cases}"
case_label='calibration'
run_result=''
dry_run=false

while (( $# > 0 )); do
  case "$1" in
    --case-id) case_id="${2:-}"; shift 2 ;;
    --scenario-id) scenario_id="${2:-}"; shift 2 ;;
    --scenario-title) scenario_title="${2:-}"; shift 2 ;;
    --scenario-description) scenario_description="${2:-}"; shift 2 ;;
    --scenario-cause) scenario_cause="${2:-}"; shift 2 ;;
    --scenario-injection-summary) scenario_injection_summary="${2:-}"; shift 2 ;;
    --scenario-user-impact) scenario_user_impact="${2:-}"; shift 2 ;;
    --scenario-distinguishing-evidence) scenario_distinguishing_evidence="${2:-}"; shift 2 ;;
    --scenario-metadata-sha256) scenario_metadata_sha256="${2:-}"; shift 2 ;;
    --t1) t1_raw="${2:-}"; shift 2 ;;
    --t2) t2_raw="${2:-}"; shift 2 ;;
    --case-label) case_label="${2:-}"; shift 2 ;;
    --run-result) run_result="${2:-}"; shift 2 ;;
    --output-root) output_root="${2:-}"; shift 2 ;;
    --dry-run) dry_run=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ -n "$case_id" ]] || die '--case-id is required'
[[ "$case_id" =~ ^case-[a-z0-9][a-z0-9-]*$ ]] || die '--case-id must match case-[a-z0-9-]+'
[[ -n "$scenario_id" ]] || die '--scenario-id is required'
[[ -n "$scenario_title" ]] || die '--scenario-title is required'
[[ -n "$scenario_description" ]] || die '--scenario-description is required'
[[ -n "$scenario_cause" ]] || die '--scenario-cause is required'
[[ -n "$scenario_injection_summary" ]] || die '--scenario-injection-summary is required'
[[ -n "$scenario_user_impact" ]] || die '--scenario-user-impact is required'
[[ -n "$scenario_distinguishing_evidence" ]] || die '--scenario-distinguishing-evidence is required'
[[ "$scenario_metadata_sha256" =~ ^[0-9a-f]{64}$ ]] || die '--scenario-metadata-sha256 must be SHA-256'
[[ -n "$t1_raw" ]] || die '--t1 is required'
[[ -n "$t2_raw" ]] || die '--t2 is required'
[[ "$case_label" =~ ^(calibration|evaluation|failed)$ ]] ||
  die '--case-label must be one of: calibration, evaluation, failed'

require_command date
require_command jq
require_command realpath
require_command stat
require_command sha256sum

scenario_metadata=$(jq -cn \
  --arg title "$scenario_title" \
  --arg description "$scenario_description" \
  --arg cause "$scenario_cause" \
  --arg injection_summary "$scenario_injection_summary" \
  --arg user_impact "$scenario_user_impact" \
  --arg distinguishing_evidence "$scenario_distinguishing_evidence" \
  '{title:$title,description:$description,cause:$cause,
    injection_summary:$injection_summary,user_impact:$user_impact,
    distinguishing_evidence:$distinguishing_evidence}')
actual_scenario_metadata_sha256=$(printf '%s' "$scenario_metadata" | sha256sum | awk '{print $1}')
[[ "$actual_scenario_metadata_sha256" == "$scenario_metadata_sha256" ]] ||
  die 'scenario metadata hash mismatch'

evaluation_eligible=false
run_script_sha256=''
run_catalog_sha256=''
run_plan_sha256=''
if [[ "$case_label" == evaluation ]]; then
  [[ -r "$run_result" ]] || die '--run-result is required and must be readable for evaluation'
  trusted_runs_root=/var/lib/lucida/scenario-runs
  if [[ "$dry_run" == true && -n "${CAPTURE_DRY_RUN_TRUSTED_RUNS_ROOT:-}" ]]; then
    trusted_runs_root=$CAPTURE_DRY_RUN_TRUSTED_RUNS_ROOT
  fi
  trusted_runs_root=$(realpath -e "$trusted_runs_root") || die 'trusted runs root does not exist'
  run_result=$(realpath -e "$run_result") || die 'run result path does not exist'
  [[ "$(basename "$run_result")" == result.json && \
     "$(dirname "$(dirname "$run_result")")" == "$trusted_runs_root" ]] ||
    die 'run result must be a controller-owned <trusted-root>/<run-id>/result.json'
  [[ "$(stat -c %u "$run_result")" == "$(id -u)" ]] ||
    die 'run result owner does not match capture process owner'
  run_result_mode=$(stat -c %a "$run_result")
  (( (8#$run_result_mode & 8#022) == 0 )) ||
    die 'run result must not be group/world writable'
  jq -e \
    --arg case_id "$case_id" \
    --arg scenario_id "$scenario_id" \
    --arg t1 "$t1_raw" \
    --argjson scenario_metadata "$scenario_metadata" \
    --arg scenario_metadata_sha256 "$scenario_metadata_sha256" \
    --arg t2 "$t2_raw" '
    .mode == "evaluation" and .outcome == "succeeded" and .dirty == false and
    .case_id == $case_id and .scenario_id == $scenario_id and
    .t1 == $t1 and .t2 == $t2 and
    .scenario_metadata == $scenario_metadata and
    .scenario_metadata_sha256 == $scenario_metadata_sha256 and
    .profile.kind == "fixed" and (.profile.id | type == "string" and length > 0) and
    .approved_profile_id == .profile.id and
    .cleanup.status == "succeeded" and
    .recovery.status == "succeeded" and
    (.plan_sha256 | type == "string" and test("^[0-9a-f]{64}$")) and
    (.script_sha256 | type == "string" and test("^[0-9a-f]{64}$")) and
    (.catalog_sha256 | type == "string" and test("^[0-9a-f]{64}$"))
  ' "$run_result" >/dev/null || die 'run result is not eligible for evaluation capture'
  require_command sha256sum
  run_dir=$(dirname "$run_result")
  run_plan=$(realpath -e "$run_dir/plan.json") || die 'controller plan.json is missing'
  run_script=$(realpath -e "$(jq -r '.script_path' "$run_result")") ||
    die 'run result script_path is missing'
  run_catalog=$(realpath -e "$(jq -r '.catalog_path' "$run_result")") ||
    die 'run result catalog_path is missing'
  [[ "$(sha256sum "$run_plan" | awk '{print $1}')" == "$(jq -r '.plan_sha256' "$run_result")" ]] ||
    die 'controller plan hash mismatch'
  [[ "$(sha256sum "$run_script" | awk '{print $1}')" == "$(jq -r '.script_sha256' "$run_result")" ]] ||
    die 'scenario script hash mismatch'
  [[ "$(sha256sum "$run_catalog" | awk '{print $1}')" == "$(jq -r '.catalog_sha256' "$run_result")" ]] ||
    die 'scenario catalog hash mismatch'
  evaluation_eligible=true
  run_script_sha256=$(jq -r '.script_sha256' "$run_result")
  run_catalog_sha256=$(jq -r '.catalog_sha256' "$run_result")
  run_plan_sha256=$(jq -r '.plan_sha256' "$run_result")
fi

t1_epoch=$(parse_utc_epoch "$t1_raw" t1)
t2_epoch=$(parse_utc_epoch "$t2_raw" t2)
(( t2_epoch >= t1_epoch )) || die 't2 must be greater than or equal to t1'

# Capture window contract v2 (spec-eval-data-capture §2.1, 2026-07-20):
# scenario segment is [t1-10m, t2+20m]; the 2h normal lead-in moved to the
# shared daily normal segment (dump-normal-segment.sh).
capture_start_epoch=$(( t1_epoch - 10 * 60 ))
capture_end_epoch=$(( t2_epoch + 20 * 60 ))
t1=$(iso_utc_from_epoch "$t1_epoch")
t2=$(iso_utc_from_epoch "$t2_epoch")
capture_start=$(iso_utc_from_epoch "$capture_start_epoch")
capture_end=$(iso_utc_from_epoch "$capture_end_epoch")
final_dir="${output_root%/}/$case_id"

if [[ "$dry_run" == true ]]; then
  jq -n \
    --arg case_id "$case_id" \
    --arg scenario_id "$scenario_id" \
    --argjson scenario_metadata "$scenario_metadata" \
    --arg scenario_metadata_sha256 "$scenario_metadata_sha256" \
    --arg case_label "$case_label" \
    --arg t1 "$t1" \
    --arg t2 "$t2" \
    --arg capture_start "$capture_start" \
    --arg capture_end "$capture_end" \
    --arg final_dir "$final_dir" \
    --arg run_script_sha256 "$run_script_sha256" \
    --arg run_catalog_sha256 "$run_catalog_sha256" \
    --arg run_plan_sha256 "$run_plan_sha256" \
    --argjson evaluation_eligible "$evaluation_eligible" \
    '{mode:"dry-run", case_id:$case_id, scenario_id:$scenario_id,
      scenario_metadata:$scenario_metadata,
      scenario_metadata_sha256:$scenario_metadata_sha256,
      case_label:$case_label, evaluation_eligible:$evaluation_eligible,
      time_basis:"UTC", t1:$t1, t2:$t2, capture_start:$capture_start,
      capture_end:$capture_end, model_snapshot_not_before:$capture_end,
      run_script_sha256:$run_script_sha256, run_catalog_sha256:$run_catalog_sha256,
      run_plan_sha256:$run_plan_sha256,
      golden_anomaly_file:false, final_dir:$final_dir}'
  exit 0
fi

require_command curl
require_command docker
require_command sha256sum

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
CAPTURE_HOST_OUTPUT_ROOT="${CAPTURE_HOST_OUTPUT_ROOT:-}"
MODEL_SOURCE="${MODEL_SOURCE:-/var/lib/lucida/ai-models/stream-anomaly/global/v1/model.json}"
MODEL_CONTAINER="${MODEL_CONTAINER:-lucida-ai-observer}"

[[ -n "$PG_PASSWORD" ]] || die 'PG_PASSWORD is required for live capture'
[[ ! -e "$final_dir" ]] || die "case already exists and is immutable: $final_dir"
mkdir -p "$output_root"
[[ -w "$output_root" ]] || die "output root is not writable: $output_root"
mkdir -p "$output_root/.locks"
lock_dir="$output_root/.locks/$case_id.lock"
mkdir "$lock_dir" 2>/dev/null || die "capture already active for case: $case_id"

staging_dir=''
cleanup_capture() {
  if [[ -n "${staging_dir:-}" && -d "$staging_dir" ]]; then
    rm -rf "$staging_dir"
  fi
  if [[ -n "${lock_dir:-}" && -d "$lock_dir" ]]; then
    if ! rmdir "$lock_dir" 2>/dev/null; then
      printf '[WARN] capture lock was not empty and was preserved: %s\n' "$lock_dir" >&2
    fi
  fi
}
trap cleanup_capture EXIT

now_epoch=$(date -u +%s)
if (( now_epoch < capture_end_epoch )); then
  wait_sec=$(( capture_end_epoch - now_epoch ))
  log "waiting ${wait_sec}s for capture_end=$capture_end"
  sleep "$wait_sec"
fi

staging_dir=$(mktemp -d "${output_root%/}/.${case_id}.staging.XXXXXX")
mkdir -p \
  "$staging_dir/data/clickhouse" \
  "$staging_dir/models/stream-anomaly/global/v1"

dump_started_at=$(date -u +'%Y-%m-%dT%H:%M:%SZ')

model_dir="$staging_dir/models/stream-anomaly/global/v1"
model_file="$model_dir/model.json"
log "snapshotting stream-anomaly model from $MODEL_SOURCE"
if [[ -r "$MODEL_SOURCE" ]]; then
  cp "$MODEL_SOURCE" "$model_file"
else
  docker cp "${MODEL_CONTAINER}:${MODEL_SOURCE}" "$model_file"
fi

jq -e . "$model_file" >/dev/null
(
  cd "$model_dir"
  sha256sum model.json > model.json.sha256
)
model_sha256=$(sha256sum "$model_file" | awk '{print $1}')
model_snapshot_at=$(date -u +'%Y-%m-%dT%H:%M:%SZ')
model_snapshot_epoch=$(parse_utc_epoch "$model_snapshot_at" model_snapshot_at)
(( model_snapshot_epoch >= capture_end_epoch )) ||
  die "model snapshot occurred before capture_end: $model_snapshot_at < $capture_end"
model_snapshot_lag_sec=$(( model_snapshot_epoch - capture_end_epoch ))

log 'exporting VictoriaMetrics time window'
curl --fail --silent --show-error --get "${VM_URL%/}/api/v1/export" \
  --data-urlencode 'match[]={__name__!=""}' \
  --data-urlencode "start=$capture_start" \
  --data-urlencode "end=$capture_end" \
  --output "$staging_dir/data/victoriametrics.export"

ch_export() {
  local table=$1 query=$2 output=$3 credential
  log "exporting ClickHouse table=$table"
  if [[ -n "$CH_USER" ]]; then
    # Feed credentials through curl's stdin config so they never appear in argv.
    credential="${CH_USER}:${CH_PASSWORD}"
    credential=${credential//\\/\\\\}
    credential=${credential//\"/\\\"}
    credential=${credential//$'\n'/\\n}
    credential=${credential//$'\r'/\\r}
    printf 'user = "%s"\n' "$credential" |
      curl --config - --fail --silent --show-error \
        --data-binary "$query" "${CH_URL%/}/" \
        --output "$output"
    return
  fi
  curl --fail --silent --show-error \
    --data-binary "$query" "${CH_URL%/}/" \
    --output "$output"
}

ch_start="parseDateTime64BestEffort('$capture_start')"
ch_end="parseDateTime64BestEffort('$capture_end')"
ch_export otel_traces_local \
  "SELECT * FROM lucida.otel_traces_local WHERE timestamp >= $ch_start AND timestamp <= $ch_end FORMAT Parquet" \
  "$staging_dir/data/clickhouse/otel_traces_local.parquet"
ch_export lucida_logs_local \
  "SELECT * FROM lucida.lucida_logs_local WHERE timestamp >= $ch_start AND timestamp <= $ch_end FORMAT Parquet" \
  "$staging_dir/data/clickhouse/lucida_logs_local.parquet"
ch_export lucida_events_local \
  "SELECT * REPLACE(toString(event_id) AS event_id, toString(episode_id) AS episode_id) FROM lucida.lucida_events_local WHERE occurred_at >= $ch_start AND occurred_at <= $ch_end FORMAT Parquet" \
  "$staging_dir/data/clickhouse/lucida_events_local.parquet"
ch_export host_connections \
  'SELECT * FROM lucida.host_connections FORMAT Parquet' \
  "$staging_dir/data/clickhouse/host_connections.parquet"

log 'dumping PostgreSQL full control/inventory snapshot'
pg_dump_data_dir="$staging_dir/data"
if [[ -n "$CAPTURE_HOST_OUTPUT_ROOT" ]]; then
  pg_dump_data_dir="${CAPTURE_HOST_OUTPUT_ROOT%/}/${staging_dir##*/}/data"
fi
PGPASSWORD="$PG_PASSWORD" docker run --rm \
  --user "$(id -u):$(id -g)" \
  --env PGPASSWORD \
  --volume "$pg_dump_data_dir:/out" \
  "$PG_DUMP_IMAGE" \
  pg_dump -Fc --host "$PG_HOST" --port "$PG_PORT" --username "$PG_USER" \
  --dbname "$PG_DATABASE" --file /out/postgres.dump

required_files=(
  data/victoriametrics.export
  data/clickhouse/otel_traces_local.parquet
  data/clickhouse/lucida_logs_local.parquet
  data/clickhouse/lucida_events_local.parquet
  data/clickhouse/host_connections.parquet
  data/postgres.dump
  models/stream-anomaly/global/v1/model.json
  models/stream-anomaly/global/v1/model.json.sha256
)
for relative in "${required_files[@]}"; do
  [[ -s "$staging_dir/$relative" ]] || die "capture artifact is missing or empty: $relative"
done
(
  cd "$model_dir"
  sha256sum -c model.json.sha256 >/dev/null
)

[[ ! -e "$staging_dir/golden.anomaly.json" ]] ||
  die 'golden.anomaly.json is forbidden; expected anomalies belong in the scenario YAML'

dump_completed_at=$(date -u +'%Y-%m-%dT%H:%M:%SZ')
jq -n \
  --arg schema_version '1.2' \
  --arg case_id "$case_id" \
  --arg scenario_id "$scenario_id" \
  --argjson scenario_metadata "$scenario_metadata" \
  --arg scenario_metadata_sha256 "$scenario_metadata_sha256" \
  --arg case_label "$case_label" \
  --arg t1 "$t1" \
  --arg t2 "$t2" \
  --arg capture_start "$capture_start" \
  --arg capture_end "$capture_end" \
  --arg dump_started_at "$dump_started_at" \
  --arg dump_completed_at "$dump_completed_at" \
  --arg model_snapshot_at "$model_snapshot_at" \
  --argjson model_snapshot_lag_sec "$model_snapshot_lag_sec" \
  --arg model_source_path "$MODEL_SOURCE" \
  --arg model_sha256 "$model_sha256" \
  --arg run_script_sha256 "$run_script_sha256" \
  --arg run_catalog_sha256 "$run_catalog_sha256" \
  --arg run_plan_sha256 "$run_plan_sha256" \
  --argjson evaluation_eligible "$evaluation_eligible" \
  '{schema_version:$schema_version, case_id:$case_id, scenario_id:$scenario_id,
    scenario_metadata:$scenario_metadata,
    scenario_metadata_sha256:$scenario_metadata_sha256,
    case_label:$case_label, evaluation_eligible:$evaluation_eligible,
    time_basis:"UTC", t1:$t1, t2:$t2, capture_start:$capture_start,
    capture_end:$capture_end, dump_started_at:$dump_started_at,
    dump_completed_at:$dump_completed_at, model_snapshot_at:$model_snapshot_at,
    model_snapshot_lag_sec:$model_snapshot_lag_sec,
    model_source_path:$model_source_path, model_sha256:$model_sha256,
    run_script_sha256:$run_script_sha256, run_catalog_sha256:$run_catalog_sha256,
    run_plan_sha256:$run_plan_sha256,
    golden_anomaly_file:false}' > "$staging_dir/meta.json"

jq -e \
  --arg capture_end "$capture_end" \
  --arg case_label "$case_label" \
  --arg scenario_metadata_sha256 "$scenario_metadata_sha256" \
  '.time_basis == "UTC" and .capture_end == $capture_end and
   .scenario_metadata_sha256 == $scenario_metadata_sha256 and
   (.scenario_metadata.title | length > 0) and
   (.scenario_metadata.description | length > 0) and
   (.scenario_metadata.cause | length > 0) and
   (.scenario_metadata.injection_summary | length > 0) and
   (.scenario_metadata.user_impact | length > 0) and
   (.scenario_metadata.distinguishing_evidence | length > 0) and
   .model_snapshot_at >= $capture_end and .case_label == $case_label and
   .golden_anomaly_file == false and
   (if $case_label == "evaluation" then
      .evaluation_eligible == true and
      (.run_plan_sha256 | test("^[0-9a-f]{64}$")) and
      (.run_script_sha256 | test("^[0-9a-f]{64}$")) and
      (.run_catalog_sha256 | test("^[0-9a-f]{64}$"))
    else .evaluation_eligible == false end)' \
  "$staging_dir/meta.json" >/dev/null || die 'meta.json violates capture policy'

[[ ! -e "$final_dir" ]] || die "case appeared during capture: $final_dir"
mv -T "$staging_dir" "$final_dir"
staging_dir=''
cleanup_capture
trap - EXIT
log "capture complete: $final_dir"

# ------------------------------------------------------------
# 아카이브 동기화(best-effort): 완성 케이스를 아카이브 정본(.104 /data/eval-cases)으로 복사.
# 실패해도 캡처는 성공으로 남긴다(케이스는 로컬 보존, 수동 재동기화 가능).
# 대역폭 제한 필수 — 무제한 전송이 사무실 라우터 인터페이스를 75%까지 점유해
# NMS warning 인시던트를 만든 실증 있음(2026-07-20, N2024-R3-01).
# ------------------------------------------------------------
ARCHIVE_SSH_TARGET="${ARCHIVE_SSH_TARGET:-}"
ARCHIVE_ROOT="${ARCHIVE_ROOT:-/data/eval-cases}"
ARCHIVE_BWLIMIT_KBPS="${ARCHIVE_BWLIMIT_KBPS:-4096}"
ARCHIVE_SSH_KEY="${ARCHIVE_SSH_KEY:-/root/.ssh/tb_key}"
if [[ -n "$ARCHIVE_SSH_TARGET" ]]; then
  log "archiving case to ${ARCHIVE_SSH_TARGET}:${ARCHIVE_ROOT} (limit ${ARCHIVE_BWLIMIT_KBPS}KB/s)"
  if tar -C "$output_root" -cf - "${final_dir##*/}" \
    | python3 -c '
import sys, time
limit = int(sys.argv[1]) * 1024
start = time.monotonic(); sent = 0
while True:
    chunk = sys.stdin.buffer.read(65536)
    if not chunk:
        break
    sys.stdout.buffer.write(chunk)
    sent += len(chunk)
    ahead = sent / limit - (time.monotonic() - start)
    if ahead > 0:
        time.sleep(ahead)
sys.stdout.buffer.flush()
' "$ARCHIVE_BWLIMIT_KBPS" \
    | ssh -i "$ARCHIVE_SSH_KEY" -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 \
        "$ARCHIVE_SSH_TARGET" "tar -C '${ARCHIVE_ROOT}' -xf -"; then
    log "archive sync complete: ${final_dir##*/}"
  else
    printf '[WARN] archive sync failed (case remains local only): %s\n' "${final_dir##*/}" >&2
  fi
fi
