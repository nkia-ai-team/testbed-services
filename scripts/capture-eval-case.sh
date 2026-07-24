#!/usr/bin/env bash
# Capture one immutable evaluation case after a scenario injection.
# Contract: docs/spec-eval-data-capture.md §2.1/§4 (v2, daily-cycle) and
# §2.2 (v3, continuous-cycle — schema 2.0, triggered by --phases-json).

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
    [--phases-json <file>] [--topology-bundle <dir>] \
    [--output-root <dir>] [--dry-run]

Case labels:
  calibration       tuning/reproducibility capture; not used for scoring (default)
  evaluation        immutable capture eligible for scoring
  failed            diagnostic capture from a failed scenario run; not used for scoring

Capture contract version:
  Default (no --phases-json)  v2, daily-cycle contract (spec §2.1, schema 1.3):
                               window [t1-10m, t2+20m], assembled/ from
                               --normal-segment, segments[]/rebase/normal_provenance.
  --phases-json <file>         v3, continuous-cycle contract (spec §2.2, schema 2.0):
                               window [normal.start, t2+30m], phases[]/
                               clickhouse_tables[] (12-table CH catalog), no
                               assembled/. Mutually exclusive with --normal-segment.
                               phases JSON is the phases[] array the runner
                               produced: trainer_reset{at,golden_id,golden_sha256}
                               and normal|buffer|injection|cooldown{start,end}.
                               injection.start/.end must equal --t1/--t2.
  --topology-bundle <dir>      v3-only. Runner-collected periodic topology/
                               service-tree bundle (dir with graph/,
                               service-tree/, manifest.json — spec §2.2). Verified
                               (sha256 + chronological + ≥1 snapshot inside
                               [t1,t2]) and re-homed byte-for-byte into the
                               case's topology/ (only manifest.json's case_id
                               is patched). Omitted: capture proceeds fail-open
                               with a warning; meta.topology_bundle is null.

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

# Human-readable KST companion for a UTC epoch (meta keeps UTC as canonical).
kst_from_epoch() {
  TZ='Asia/Seoul' date -d "@$1" +'%Y-%m-%dT%H:%M:%S+09:00'
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
# Capture contract v2 (spec §2.1) optional inputs, supplied by the runner:
#   --preflight-json     runner's 1st-layer preflight verdict for [t1-10m, t1]
#   --normal-segment     path to the day's normal segment dir (normal-segments/
#                        <domain>/<date>/) used for provenance + assembled/ merge
preflight_json=''
normal_segment=''
# Capture contract v3 (spec §2.2) input, supplied by the runner: the phases[]
# array covering trainer_reset/normal/buffer/injection/cooldown. Presence of
# this flag switches the whole script into v3 (continuous-cycle) mode.
phases_json=''
# Periodic topology/service-tree bundle (spec §2.2, EventCluster contract,
# v3-only). Runner-collected dir with graph/, service-tree/, manifest.json.
topology_bundle=''
dry_run=false
self_check=false

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
    --preflight-json) preflight_json="${2:-}"; shift 2 ;;
    --normal-segment) normal_segment="${2:-}"; shift 2 ;;
    --phases-json) phases_json="${2:-}"; shift 2 ;;
    --topology-bundle) topology_bundle="${2:-}"; shift 2 ;;
    --output-root) output_root="${2:-}"; shift 2 ;;
    --dry-run) dry_run=true; shift ;;
    --self-check) self_check=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

# ------------------------------------------------------------
# --self-check: prove every external dependency the capture will need,
# BEFORE any scenario is run. The runner's queue readiness invokes this so a
# missing credential or unreachable store refuses queue start instead of
# burning a 30-minute scenario and failing at capture time (2026-07-21,
# preflight-json / QUERY_API_PASSWORD 실전 실패 계보의 근본 수리).
# Uses the exact same env defaults as live capture — no separate config.
# ------------------------------------------------------------
if [[ "$self_check" == true ]]; then
  require_command date; require_command jq; require_command realpath
  require_command stat; require_command sha256sum; require_command curl
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
  QUERY_API_URL="${QUERY_API_URL:-http://192.168.230.119:18080}"
  QUERY_API_USER="${QUERY_API_USER:-manager}"
  QUERY_API_PASSWORD="${QUERY_API_PASSWORD:-}"

  sc_status=0
  sc_fail() { printf '[ERROR] self-check FAILED: %s\n' "$*" >&2; sc_status=1; }
  sc_run() { local name=$1; shift; if "$@" >/dev/null 2>&1; then log "self-check ok: $name"; else sc_fail "$name"; fi; }

  [[ -n "$PG_PASSWORD" ]] || sc_fail 'env PG_PASSWORD is empty'
  [[ -n "$CH_PASSWORD" ]] || sc_fail 'env CH_PASSWORD is empty'
  [[ -n "$QUERY_API_PASSWORD" ]] || sc_fail 'env QUERY_API_PASSWORD is empty'

  sc_run "victoriametrics reachable ($VM_URL)" \
    curl --fail --silent --max-time 10 --get "${VM_URL%/}/api/v1/query" --data-urlencode 'query=vm_rows'
  if [[ -n "$CH_PASSWORD" ]]; then
    sc_ch() { printf 'user = "%s:%s"\n' "$CH_USER" "$CH_PASSWORD" | curl --config - --fail --silent --max-time 10 --data-binary 'SELECT 1' "${CH_URL%/}/"; }
    sc_run "clickhouse auth ($CH_URL)" sc_ch
    # v3 (spec §2.2) 12-table catalog must exist before a continuous-cycle
    # queue is allowed to start; a missing table means either a schema
    # drift or a v2-era ClickHouse that hasn't caught up.
    sc_ch_v3_tables() {
      local expected="otel_traces_local lucida_logs_local lucida_events_local dpm_session_local dpm_topsql_local kcm_events_local process_snapshot trace_error_chains_local trace_path_signatures_local syslog_local host_connections process_meta"
      local found missing=() t
      found=$(printf 'user = "%s:%s"\n' "$CH_USER" "$CH_PASSWORD" | curl --config - --fail --silent --max-time 10 --data-binary "SELECT name FROM system.tables WHERE database='lucida' FORMAT TSV" "${CH_URL%/}/") || return 1
      for t in $expected; do
        grep -qx "$t" <<<"$found" || missing+=("$t")
      done
      if (( ${#missing[@]} > 0 )); then
        printf 'missing v3 tables: %s\n' "${missing[*]}" >&2
        return 1
      fi
    }
    sc_run "clickhouse v3 12-table catalog present ($CH_URL)" sc_ch_v3_tables
  fi
  if [[ -n "$QUERY_API_PASSWORD" ]]; then
    sc_login() { printf '{"username":"%s","password":"%s"}' "$QUERY_API_USER" "$QUERY_API_PASSWORD" | curl --fail --silent --max-time 10 -X POST -H 'Content-Type: application/json' --data-binary @- "${QUERY_API_URL%/}/api/v1/login"; }
    sc_run "query api login for topology ($QUERY_API_URL)" sc_login
  fi
  if [[ -n "$PG_PASSWORD" ]]; then
    sc_run "postgres auth ($PG_HOST:$PG_PORT/$PG_DATABASE)" \
      docker run --rm --env PGPASSWORD="$PG_PASSWORD" "$PG_DUMP_IMAGE" \
      psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DATABASE" -tAc 'SELECT 1'
  fi
  sc_assemble="${ASSEMBLE_SCRIPT:-$(dirname "$(realpath "$0")")/assemble-eval-case.sh}"
  sc_run "assemble script executable ($sc_assemble)" test -x "$sc_assemble"
  if [[ -x "$sc_assemble" ]]; then
    sc_run "assemble clickhouse-local roundtrip" env EVAL_CASE_ROOT="$output_root" "$sc_assemble" --self-check
  fi
  sc_run "output root writable ($output_root)" bash -c "mkdir -p '$output_root' && touch '$output_root/.self-check' && rm -f '$output_root/.self-check'"
  if [[ -n "${ARCHIVE_SSH_TARGET:-}" ]]; then
    sc_run "archive ssh (${ARCHIVE_SSH_TARGET})" \
      ssh -i "${ARCHIVE_SSH_KEY:-/root/.ssh/tb_key}" -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 "$ARCHIVE_SSH_TARGET" true
  fi
  (( sc_status == 0 )) && log 'self-check passed: capture chain is fully provisioned'
  exit "$sc_status"
fi

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
[[ -z "$phases_json" || -z "$normal_segment" ]] ||
  die '--phases-json and --normal-segment are mutually exclusive (v3 phases[] replaces v2 assembled/ provenance)'
v3_mode=false
[[ -n "$phases_json" ]] && v3_mode=true
[[ -z "$topology_bundle" || "$v3_mode" == true ]] ||
  die '--topology-bundle requires --phases-json (v3-only)'

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

phases_enriched='[]'
if [[ "$v3_mode" == true ]]; then
  # Capture window contract v3 (spec-eval-data-capture §2.2, 2026-07-24):
  # capture_start = normal.start (=cycle_start), capture_end = cooldown.end
  # (= t2+30m in production; the runner's phases.json is the single source of
  # truth so shortened smoke cycles capture without a hardcoded 30m wait).
  # injection.start/.end must equal --t1/--t2 (runner/capture consistency).
  [[ -r "$phases_json" ]] || die "--phases-json is not readable: $phases_json"
  # The runner emits an envelope object {schema_version, run_id, scenario_id,
  # phases: [...]}; golden_sha256 may be null until restore emits a digest
  # (known TODO). Validate the envelope, then work off .phases below.
  jq -e '
    (type == "object") and (.phases | type == "array") and
    ([.phases[].phase] as $p |
      ($p | index("trainer_reset")) != null and
      ($p | index("normal")) != null and
      ($p | index("buffer")) != null and
      ($p | index("injection")) != null and
      ($p | index("cooldown")) != null) and
    (.phases[] | select(.phase == "trainer_reset") |
      (.at | type == "string") and (.golden_id | type == "string") and
      ((.golden_sha256 | type == "string") or .golden_sha256 == null)) and
    (.phases[] | select(.phase != "trainer_reset") |
      (.start | type == "string") and (.end | type == "string"))
  ' "$phases_json" >/dev/null ||
    die 'phases-json is missing required phases (trainer_reset/normal/buffer/injection/cooldown) or fields'

  normal_start_raw=$(jq -r '.phases[] | select(.phase == "normal") | .start' "$phases_json")
  injection_start_raw=$(jq -r '.phases[] | select(.phase == "injection") | .start' "$phases_json")
  injection_end_raw=$(jq -r '.phases[] | select(.phase == "injection") | .end' "$phases_json")
  injection_start_epoch=$(parse_utc_epoch "$injection_start_raw" 'phases-json injection.start')
  injection_end_epoch=$(parse_utc_epoch "$injection_end_raw" 'phases-json injection.end')
  (( injection_start_epoch == t1_epoch )) ||
    die "phases-json injection.start ($injection_start_raw) must equal --t1 ($t1_raw)"
  (( injection_end_epoch == t2_epoch )) ||
    die "phases-json injection.end ($injection_end_raw) must equal --t2 ($t2_raw)"

  capture_start_epoch=$(parse_utc_epoch "$normal_start_raw" 'phases-json normal.start')
  cooldown_end_raw=$(jq -r '.phases[] | select(.phase == "cooldown") | .end' "$phases_json")
  capture_end_epoch=$(parse_utc_epoch "$cooldown_end_raw" 'phases-json cooldown.end')
  (( capture_end_epoch >= t2_epoch )) ||
    die "phases-json cooldown.end ($cooldown_end_raw) must not precede --t2 ($t2_raw)"

  # Carry phases[] into meta.json verbatim, plus *_kst companions per field.
  while IFS= read -r phase_obj; do
    phase_name=$(jq -r '.phase' <<<"$phase_obj")
    if [[ "$phase_name" == trainer_reset ]]; then
      at_epoch=$(parse_utc_epoch "$(jq -r '.at' <<<"$phase_obj")" 'phases-json trainer_reset.at')
      enriched=$(jq -c --arg at_kst "$(kst_from_epoch "$at_epoch")" '. + {at_kst:$at_kst}' <<<"$phase_obj")
    else
      start_epoch=$(parse_utc_epoch "$(jq -r '.start' <<<"$phase_obj")" "phases-json $phase_name.start")
      end_epoch=$(parse_utc_epoch "$(jq -r '.end' <<<"$phase_obj")" "phases-json $phase_name.end")
      enriched=$(jq -c \
        --arg start_kst "$(kst_from_epoch "$start_epoch")" \
        --arg end_kst "$(kst_from_epoch "$end_epoch")" \
        '. + {start_kst:$start_kst, end_kst:$end_kst}' <<<"$phase_obj")
    fi
    phases_enriched=$(jq -c --argjson obj "$enriched" '. + [$obj]' <<<"$phases_enriched")
  done < <(jq -c '.phases[]' "$phases_json")
else
  # Capture window contract v2 (spec-eval-data-capture §2.1, 2026-07-20):
  # scenario segment is [t1-10m, t2+20m]; the 2h normal lead-in moved to the
  # shared daily normal segment (dump-normal-segment.sh).
  capture_start_epoch=$(( t1_epoch - 10 * 60 ))
  capture_end_epoch=$(( t2_epoch + 20 * 60 ))
fi
t1=$(iso_utc_from_epoch "$t1_epoch")
t2=$(iso_utc_from_epoch "$t2_epoch")
capture_start=$(iso_utc_from_epoch "$capture_start_epoch")
capture_end=$(iso_utc_from_epoch "$capture_end_epoch")
t1_kst=$(kst_from_epoch "$t1_epoch")
t2_kst=$(kst_from_epoch "$t2_epoch")
capture_start_kst=$(kst_from_epoch "$capture_start_epoch")
capture_end_kst=$(kst_from_epoch "$capture_end_epoch")
final_dir="${output_root%/}/$case_id"

# ------------------------------------------------------------
# Topology periodic bundle (spec §2.2, EventCluster contract, 2026-07-24,
# v3-only): the runner polls topology/service-tree throughout the cycle and
# hands the raw bundle off via --topology-bundle. Verified here (sha256 +
# chronological + >=1 snapshot inside [t1,t2]) so a bad/incomplete bundle
# dies before any live capture work starts; a missing bundle is fail-open —
# a collector outage must not kill the whole capture (spec: "수집기 장애는
# 큐를 멈추지 않고... 캡처는 번들 부재 시 경고 후 진행").
# ------------------------------------------------------------
topology_bundle_meta='null'
if [[ -n "$topology_bundle" ]]; then
  [[ -d "$topology_bundle" ]] || die "--topology-bundle is not a directory: $topology_bundle"
  tb_manifest="${topology_bundle%/}/manifest.json"
  [[ -r "$tb_manifest" ]] || die "topology bundle has no readable manifest.json: $tb_manifest"
  # A snapshot is either a full pair or a blank failure tick (graph=[] and
  # service_tree=null) — the collector appends blanks for failed ticks and
  # records the failure in capture_failures[] (수집 공백도 기록 원칙).
  jq -e '
    ((.schema_version | tostring) == "1") and
    (.capture_interval_seconds | type == "number") and
    (.capture_failures | type == "array") and
    (.snapshots | type == "array" and length > 0) and
    (.snapshots[] |
      (.captured_at | type == "string") and
      (((.graph | type == "array" and length == 0) and .service_tree == null) or
       ((.graph | type == "array" and length > 0) and
        (.graph[] |
          (.path | type == "string") and (.request_url | type == "string") and
          (.http_status | type == "number") and (.sha256 | type == "string")) and
        (.service_tree |
          (.path | type == "string") and (.request_url | type == "string") and
          (.http_status | type == "number") and (.sha256 | type == "string")))))
  ' "$tb_manifest" >/dev/null ||
    die 'topology bundle manifest.json is missing required fields (spec §2.2: schema_version/capture_interval_seconds/capture_failures[]/snapshots[].graph[]/service_tree)'

  # Raw-bytes re-verification: every referenced file must exist and hash to
  # the value manifest.json recorded (원본 무가공 원칙 — no re-derivation).
  while IFS=$'\t' read -r tb_rel_path tb_sha256; do
    tb_file="${topology_bundle%/}/$tb_rel_path"
    [[ -r "$tb_file" ]] || die "topology bundle references a missing file: $tb_rel_path"
    [[ "$(sha256sum "$tb_file" | awk '{print $1}')" == "$tb_sha256" ]] ||
      die "topology bundle sha256 mismatch: $tb_rel_path"
  done < <(jq -r '.snapshots[] | select((.graph | length) > 0) | (.graph[] | [.path, .sha256]), ([.service_tree.path, .service_tree.sha256]) | @tsv' "$tb_manifest")

  # Chronological order + minimum coverage: >=1 snapshot inside [t1, t2].
  tb_prev_epoch=''
  tb_covered=0
  while IFS= read -r tb_captured_at; do
    tb_cur_epoch=$(parse_utc_epoch "$tb_captured_at" 'topology bundle snapshots[].captured_at')
    if [[ -n "$tb_prev_epoch" ]]; then
      (( tb_cur_epoch >= tb_prev_epoch )) ||
        die "topology bundle snapshots[].captured_at is not chronological at $tb_captured_at"
    fi
    tb_prev_epoch=$tb_cur_epoch
    if (( tb_cur_epoch >= t1_epoch && tb_cur_epoch <= t2_epoch )); then
      tb_covered=$(( tb_covered + 1 ))
    fi
  done < <(jq -r '.snapshots[] | select((.graph | length) > 0) | .captured_at' "$tb_manifest")
  (( tb_covered >= 1 )) ||
    die 'topology bundle has no successful snapshots[] inside the injection window [t1, t2]'

  tb_snapshot_count=$(jq -r '.snapshots | length' "$tb_manifest")
  tb_failure_count=$(jq -r '.capture_failures | length' "$tb_manifest")
  tb_interval_seconds=$(jq -r '.capture_interval_seconds' "$tb_manifest")
  topology_bundle_meta=$(jq -cn \
    --argjson snapshot_count "$tb_snapshot_count" \
    --argjson capture_failure_count "$tb_failure_count" \
    --argjson interval_seconds "$tb_interval_seconds" \
    '{path:"topology/", snapshot_count:$snapshot_count,
      capture_failure_count:$capture_failure_count, interval_seconds:$interval_seconds}')
elif [[ "$v3_mode" == true ]]; then
  printf '[WARN] --topology-bundle not supplied; case will have no periodic topology snapshots (fail-open)\n' >&2
fi

# ------------------------------------------------------------
# schema 1.3 companion metadata (spec-eval-data-capture §2.1):
# preflight verdict, normal-segment provenance, segments[], rebase{}.
# All are optional inputs supplied by the runner; evaluation additionally
# requires a clean preflight verdict.
# ------------------------------------------------------------
CLEAN_VERDICTS='clean clean_after_wait ai_judged_clean'
preflight='null'
preflight_verdict=''
preflight_clean=false
if [[ -n "$preflight_json" ]]; then
  [[ -r "$preflight_json" ]] || die "--preflight-json is not readable: $preflight_json"
  jq -e '
    (.verdict | type == "string") and (.checked_at | type == "string") and
    (.window | type == "array" and length == 2) and (.checks | type == "array")
  ' "$preflight_json" >/dev/null || die 'preflight json is missing required fields'
  preflight=$(jq -c '.' "$preflight_json")
  preflight_verdict=$(jq -r '.verdict' "$preflight_json")
  case " $CLEAN_VERDICTS " in
    *" $preflight_verdict "*) preflight_clean=true ;;
    *) preflight_clean=false ;;
  esac
fi

if [[ "$case_label" == evaluation ]]; then
  [[ -n "$preflight_json" ]] ||
    die 'evaluation capture requires --preflight-json (a clean preflight verdict)'
  [[ "$preflight_clean" == true ]] ||
    die "evaluation capture requires a clean preflight verdict, got: ${preflight_verdict:-none}"
fi

normal_provenance='null'
segments='null'
rebase='null'
if [[ "$v3_mode" == false ]]; then
  segments=$(jq -cn \
    --arg start "$capture_start" --arg finish "$capture_end" \
    '[{role:"scenario", ref:null, original_start:$start, original_end:$finish, tod_phase:null}]')
  if [[ -n "$normal_segment" ]]; then
    normal_meta="${normal_segment%/}/meta.json"
    [[ -r "$normal_meta" ]] || die "--normal-segment has no readable meta.json: $normal_meta"
    jq -e '
      (.segment_start | type == "string") and (.segment_end | type == "string")
    ' "$normal_meta" >/dev/null || die 'normal-segment meta.json is missing segment bounds'
    normal_start=$(jq -r '.segment_start' "$normal_meta")
    normal_end=$(jq -r '.segment_end' "$normal_meta")
    normal_tod_phase=$(jq -r '.tod_phase // empty' "$normal_meta")
    normal_end_epoch=$(parse_utc_epoch "$normal_end" 'normal-segment segment_end')
    rebase_delta_sec=$(( capture_start_epoch - normal_end_epoch ))
    normal_ref=$(realpath -m "$normal_segment")
    segments=$(jq -c \
      --arg ref "$normal_ref" --arg start "$normal_start" --arg finish "$normal_end" \
      --arg phase "$normal_tod_phase" \
      '. + [{role:"normal", ref:$ref, original_start:$start, original_end:$finish,
             tod_phase:(if $phase == "" then null else $phase end)}]' \
      <<<"$segments")
    rebase=$(jq -cn \
      --argjson delta "$rebase_delta_sec" --arg seam "$capture_start" \
      '{policy:"shift_normal_forward", delta_sec:$delta, seam_at:$seam}')
    normal_provenance=$(jq -c '{
      captured_at: (.captured_at // null), loadgen_seed: (.loadgen_seed // null),
      baseline_rps: (.baseline_rps // null), testbed_commit: (.testbed_commit // null)
    }' "$normal_meta")
  fi
fi

if [[ "$dry_run" == true ]]; then
  if [[ "$v3_mode" == true ]]; then
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
      --arg t1_kst "$t1_kst" \
      --arg t2_kst "$t2_kst" \
      --arg capture_start_kst "$capture_start_kst" \
      --arg capture_end_kst "$capture_end_kst" \
      --argjson phases "$phases_enriched" \
      --argjson preflight "$preflight" \
      --argjson topology_bundle "$topology_bundle_meta" \
      --argjson evaluation_eligible "$evaluation_eligible" \
      '{mode:"dry-run", schema_version:"2.0", timeline:"continuous",
        case_id:$case_id, scenario_id:$scenario_id,
        scenario_metadata:$scenario_metadata,
        scenario_metadata_sha256:$scenario_metadata_sha256,
        case_label:$case_label, evaluation_eligible:$evaluation_eligible,
        time_basis:"UTC", t1:$t1, t2:$t2, capture_start:$capture_start,
        capture_end:$capture_end, model_snapshot_not_before:$capture_end,
        t1_kst:$t1_kst, t2_kst:$t2_kst, capture_start_kst:$capture_start_kst,
        capture_end_kst:$capture_end_kst,
        phases:$phases, clickhouse_tables:[], topology_bundle:$topology_bundle,
        preflight:$preflight, topology_snapshot:null,
        run_script_sha256:$run_script_sha256, run_catalog_sha256:$run_catalog_sha256,
        run_plan_sha256:$run_plan_sha256,
        golden_anomaly_file:false, final_dir:$final_dir}'
  else
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
      --arg t1_kst "$t1_kst" \
      --arg t2_kst "$t2_kst" \
      --arg capture_start_kst "$capture_start_kst" \
      --arg capture_end_kst "$capture_end_kst" \
      --argjson segments "$segments" \
      --argjson rebase "$rebase" \
      --argjson normal_provenance "$normal_provenance" \
      --argjson preflight "$preflight" \
      --argjson evaluation_eligible "$evaluation_eligible" \
      '{mode:"dry-run", schema_version:"1.3", case_id:$case_id, scenario_id:$scenario_id,
        scenario_metadata:$scenario_metadata,
        scenario_metadata_sha256:$scenario_metadata_sha256,
        case_label:$case_label, evaluation_eligible:$evaluation_eligible,
        time_basis:"UTC", t1:$t1, t2:$t2, capture_start:$capture_start,
        capture_end:$capture_end, model_snapshot_not_before:$capture_end,
        t1_kst:$t1_kst, t2_kst:$t2_kst, capture_start_kst:$capture_start_kst,
        capture_end_kst:$capture_end_kst,
        segments:$segments, rebase:$rebase, normal_provenance:$normal_provenance,
        preflight:$preflight, topology_snapshot:null,
        run_script_sha256:$run_script_sha256, run_catalog_sha256:$run_catalog_sha256,
        run_plan_sha256:$run_plan_sha256,
        golden_anomaly_file:false, final_dir:$final_dir}'
  fi
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
# Topology snapshot (spec §4, 2026-07-20) — query API on 119:18080, cookie login.
QUERY_API_URL="${QUERY_API_URL:-http://192.168.230.119:18080}"
QUERY_API_USER="${QUERY_API_USER:-manager}"
QUERY_API_PASSWORD="${QUERY_API_PASSWORD:-}"

[[ -n "$PG_PASSWORD" ]] || die 'PG_PASSWORD is required for live capture'
[[ -n "$QUERY_API_PASSWORD" ]] || die 'QUERY_API_PASSWORD is required for the topology snapshot'
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
  "$staging_dir/data/topology" \
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

ch_query() {
  local query=$1 credential
  if [[ -n "$CH_USER" ]]; then
    credential="${CH_USER}:${CH_PASSWORD}"
    credential=${credential//\\/\\\\}
    credential=${credential//\"/\\\"}
    credential=${credential//$'\n'/\\n}
    credential=${credential//$'\r'/\\r}
    printf 'user = "%s"\n' "$credential" |
      curl --config - --fail --silent --show-error --data-binary "$query" "${CH_URL%/}/"
    return
  fi
  curl --fail --silent --show-error --data-binary "$query" "${CH_URL%/}/"
}

ch_start="parseDateTime64BestEffort('$capture_start')"
ch_end="parseDateTime64BestEffort('$capture_end')"
clickhouse_tables_json='[]'
if [[ "$v3_mode" == true ]]; then
  # ClickHouse capture — v3 12-table catalog (spec §2.2). 10 window-sliced +
  # 2 full-snapshot tables. Each entry records rows/time_min/time_max in
  # meta.clickhouse_tables[] so "did data land" is answerable without
  # opening a file (2026-07-24 "CH 데이터 없음" 논쟁 재발 방지).
  ch_v3_tables=(
    'otel_traces_local:timestamp:slice'
    'lucida_logs_local:timestamp:slice'
    'lucida_events_local:occurred_at:slice'
    'dpm_session_local:timestamp:slice'
    'dpm_topsql_local:timestamp:slice'
    'kcm_events_local:timestamp:slice'
    'process_snapshot:ts:slice'
    'trace_error_chains_local:window_start:slice'
    'trace_path_signatures_local:window_start:slice'
    'syslog_local:received_at:slice'
    'host_connections::full'
    'process_meta::full'
  )
  for entry in "${ch_v3_tables[@]}"; do
    IFS=':' read -r tbl time_col mode <<<"$entry"
    file="$staging_dir/data/clickhouse/${tbl}.parquet"
    select_expr='*'
    [[ "$tbl" == lucida_events_local ]] &&
      select_expr='* REPLACE(toString(event_id) AS event_id, toString(episode_id) AS episode_id)'
    if [[ "$mode" == full ]]; then
      ch_export "$tbl" "SELECT $select_expr FROM lucida.$tbl FORMAT Parquet" "$file"
      rows=$(ch_query "SELECT count() FROM lucida.$tbl FORMAT TSV")
      entry_json=$(jq -cn --arg table "$tbl" --arg file "data/clickhouse/${tbl}.parquet" \
        --argjson rows "$rows" \
        '{table:$table, file:$file, rows:$rows, time_column:null, time_min:null, time_max:null}')
    else
      where_clause=" WHERE $time_col >= $ch_start AND $time_col <= $ch_end"
      ch_export "$tbl" "SELECT $select_expr FROM lucida.$tbl${where_clause} FORMAT Parquet" "$file"
      stats_line=$(ch_query "SELECT count(), toString(min($time_col)), toString(max($time_col)) FROM lucida.$tbl${where_clause} FORMAT TSV")
      rows=$(cut -f1 <<<"$stats_line")
      if [[ "$rows" == 0 ]]; then
        entry_json=$(jq -cn --arg table "$tbl" --arg file "data/clickhouse/${tbl}.parquet" \
          --arg time_column "$time_col" --argjson rows "$rows" \
          '{table:$table, file:$file, rows:$rows, time_column:$time_column, time_min:null, time_max:null}')
      else
        time_min_raw=$(cut -f2 <<<"$stats_line")
        time_max_raw=$(cut -f3 <<<"$stats_line")
        entry_json=$(jq -cn --arg table "$tbl" --arg file "data/clickhouse/${tbl}.parquet" \
          --arg time_column "$time_col" --argjson rows "$rows" \
          --arg time_min "$time_min_raw" --arg time_max "$time_max_raw" \
          '{table:$table, file:$file, rows:$rows, time_column:$time_column, time_min:$time_min, time_max:$time_max}')
      fi
    fi
    clickhouse_tables_json=$(jq -c --argjson e "$entry_json" '. + [$e]' <<<"$clickhouse_tables_json")
  done

  # Drift warning (spec §2.2): any base table outside the 12-table list that
  # currently holds data is a schema-drift signal, not a capture failure.
  known_tables=$(printf '%s\n' "${ch_v3_tables[@]}" | cut -d: -f1)
  # Join against system.tables so MV backing tables (.inner_id.*) and
  # AggregatingMergeTree MV targets do not raise false drift warnings — both
  # hold physical parts (2026-07-24 실측: .inner_id.* 16M행) and are rebuilt
  # from base-table re-insert on restore, so they are intentionally uncaptured.
  drift_tables=$(ch_query "SELECT p.table FROM system.parts AS p INNER JOIN system.tables AS t ON t.database = p.database AND t.name = p.table WHERE p.database='lucida' AND p.active AND t.engine IN ('MergeTree','ReplacingMergeTree','SummingMergeTree') AND p.table NOT LIKE '.inner%' AND p.table != 'schema_migrations' GROUP BY p.table HAVING sum(p.rows) > 0 FORMAT TSV") || drift_tables=''
  while IFS= read -r drift_table; do
    [[ -z "$drift_table" ]] && continue
    grep -qx "$drift_table" <<<"$known_tables" ||
      printf '[WARN] unlisted table with data: %s\n' "$drift_table" >&2
  done <<<"$drift_tables"
else
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
fi

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

# ------------------------------------------------------------
# Topology snapshot (spec §4, 2026-07-20): the query API's cross-domain graph
# and operator service tree, saved as raw API JSON under data/topology/ so a
# consumer can score RCA propagation / topology context without restoring PG.
# Login mirrors the ai-coverages registration flow (POST /api/v1/login ->
# lucida_session cookie). graph range is the scenario window [t1-10m, t2+20m].
# NOTE: the exact query-param contract for /api/v1/topology/graph is not pinned
# in the specs; `from`/`to` (UTC) are used and echoed into graph_params so a
# live run can confirm/adjust. See report "결정 필요: topology graph params".
# ------------------------------------------------------------
topology_dir="$staging_dir/data/topology"
topology_snapshot_at=''
graph_params='{}'
topology_endpoints='[]'
snapshot_topology() {
  local cookie_jar
  cookie_jar=$(mktemp "${TMPDIR:-/tmp}/.topo-cookie.XXXXXX")
  log 'logging into query API for topology snapshot'
  printf '{"username":"%s","password":"%s"}' "$QUERY_API_USER" "$QUERY_API_PASSWORD" |
    curl --fail --silent --show-error --cookie-jar "$cookie_jar" \
      -H 'Content-Type: application/json' --data-binary @- \
      "${QUERY_API_URL%/}/api/v1/login" >/dev/null
  local graph_url="${QUERY_API_URL%/}/api/v1/topology/graph"
  log 'fetching topology graph'
  curl --fail --silent --show-error --cookie "$cookie_jar" --get "$graph_url" \
    --data-urlencode "from=$capture_start" --data-urlencode "to=$capture_end" \
    --output "$topology_dir/topology-graph.json"
  local tree_url="${QUERY_API_URL%/}/api/v1/asset-tree/service/unified"
  log 'fetching unified service asset tree'
  curl --fail --silent --show-error --cookie "$cookie_jar" \
    --output "$topology_dir/asset-tree-service-unified.json" "$tree_url"
  rm -f "$cookie_jar"
  jq -e . "$topology_dir/topology-graph.json" >/dev/null ||
    die 'topology graph response is not valid JSON'
  jq -e . "$topology_dir/asset-tree-service-unified.json" >/dev/null ||
    die 'asset-tree response is not valid JSON'
  topology_snapshot_at=$(date -u +'%Y-%m-%dT%H:%M:%SZ')
  graph_params=$(jq -cn --arg from "$capture_start" --arg to "$capture_end" \
    '{from:$from, to:$to, range:("["+$from+","+$to+"]")}')
  topology_endpoints=$(jq -cn --arg graph "$graph_url" --arg tree "$tree_url" \
    '[{name:"topology.graph", url:$graph, file:"data/topology/topology-graph.json"},
      {name:"asset-tree.service.unified", url:$tree, file:"data/topology/asset-tree-service-unified.json"}]')
}
snapshot_topology

# ------------------------------------------------------------
# Topology periodic bundle re-home (spec §2.2, v3-only): byte-identical copy
# of the runner's verified bundle into the case's topology/ — only
# manifest.json is re-emitted, and only to inject case_id (원본 무가공
# 원칙: graph/*.json, service-tree/*.json are never re-serialized).
# ------------------------------------------------------------
if [[ -n "$topology_bundle" ]]; then
  log "re-homing topology bundle from $topology_bundle"
  mkdir -p "$staging_dir/topology"
  cp -a "$topology_bundle/." "$staging_dir/topology/"
  tb_manifest_out=$(mktemp)
  jq --arg case_id "$case_id" '.case_id = $case_id' "$staging_dir/topology/manifest.json" \
    > "$tb_manifest_out"
  mv "$tb_manifest_out" "$staging_dir/topology/manifest.json"
fi

required_files=(
  data/victoriametrics.export
  data/postgres.dump
  data/topology/topology-graph.json
  data/topology/asset-tree-service-unified.json
  models/stream-anomaly/global/v1/model.json
  models/stream-anomaly/global/v1/model.json.sha256
)
[[ -z "$topology_bundle" ]] || required_files+=(topology/manifest.json)
if [[ "$v3_mode" == true ]]; then
  for entry in "${ch_v3_tables[@]}"; do
    tbl="${entry%%:*}"
    required_files+=("data/clickhouse/${tbl}.parquet")
  done
else
  required_files+=(
    data/clickhouse/otel_traces_local.parquet
    data/clickhouse/lucida_logs_local.parquet
    data/clickhouse/lucida_events_local.parquet
    data/clickhouse/host_connections.parquet
  )
fi
for relative in "${required_files[@]}"; do
  [[ -s "$staging_dir/$relative" ]] || die "capture artifact is missing or empty: $relative"
done
(
  cd "$model_dir"
  sha256sum -c model.json.sha256 >/dev/null
)

[[ ! -e "$staging_dir/golden.anomaly.json" ]] ||
  die 'golden.anomaly.json is forbidden; expected anomalies belong in the scenario YAML'

# ------------------------------------------------------------
# assembled/ (spec §2.1/§4, v2 only): when a normal segment is supplied,
# build the continuous-timeline merge (shift normal prefix forward by
# rebase.delta_sec, concat scenario window). Scenario data/ stays real wall
# clock; originals are not mutated. v3's capture window is already a real
# continuous timeline (§2.2), so assembled/ never applies there.
# ------------------------------------------------------------
if [[ "$v3_mode" == false && -n "$normal_segment" ]]; then
  assemble_script="${ASSEMBLE_SCRIPT:-$(dirname "$(realpath "$0")")/assemble-eval-case.sh}"
  [[ -x "$assemble_script" ]] || die "assemble script is not executable: $assemble_script"
  log "assembling continuous timeline (delta=${rebase_delta_sec}s)"
  "$assemble_script" --case-dir "$staging_dir" --normal-segment "$normal_segment" \
    --delta-sec "$rebase_delta_sec"
  for relative in \
    assembled/victoriametrics.export \
    assembled/clickhouse/otel_traces_local.parquet \
    assembled/clickhouse/lucida_logs_local.parquet \
    assembled/clickhouse/lucida_events_local.parquet; do
    [[ -s "$staging_dir/$relative" ]] || die "assembled artifact is missing or empty: $relative"
  done
fi

dump_completed_at=$(date -u +'%Y-%m-%dT%H:%M:%SZ')
dump_started_at_kst=$(kst_from_epoch "$(date -d "$dump_started_at" +%s)")
dump_completed_at_kst=$(kst_from_epoch "$(date -d "$dump_completed_at" +%s)")
model_snapshot_at_kst=$(kst_from_epoch "$model_snapshot_epoch")

topology_snapshot=$(jq -cn \
  --arg snapshot_at "$topology_snapshot_at" \
  --argjson endpoints "$topology_endpoints" \
  --argjson graph_params "$graph_params" \
  '{snapshot_at:$snapshot_at, endpoints:$endpoints, graph_params:$graph_params}')

if [[ "$v3_mode" == true ]]; then
  jq -n \
    --arg schema_version '2.0' \
    --arg case_id "$case_id" \
    --arg scenario_id "$scenario_id" \
    --argjson scenario_metadata "$scenario_metadata" \
    --arg scenario_metadata_sha256 "$scenario_metadata_sha256" \
    --arg case_label "$case_label" \
    --arg t1 "$t1" \
    --arg t2 "$t2" \
    --arg capture_start "$capture_start" \
    --arg capture_end "$capture_end" \
    --arg t1_kst "$t1_kst" \
    --arg t2_kst "$t2_kst" \
    --arg capture_start_kst "$capture_start_kst" \
    --arg capture_end_kst "$capture_end_kst" \
    --arg dump_started_at "$dump_started_at" \
    --arg dump_completed_at "$dump_completed_at" \
    --arg dump_started_at_kst "$dump_started_at_kst" \
    --arg dump_completed_at_kst "$dump_completed_at_kst" \
    --arg model_snapshot_at "$model_snapshot_at" \
    --arg model_snapshot_at_kst "$model_snapshot_at_kst" \
    --argjson model_snapshot_lag_sec "$model_snapshot_lag_sec" \
    --arg model_source_path "$MODEL_SOURCE" \
    --arg model_sha256 "$model_sha256" \
    --arg run_script_sha256 "$run_script_sha256" \
    --arg run_catalog_sha256 "$run_catalog_sha256" \
    --arg run_plan_sha256 "$run_plan_sha256" \
    --argjson phases "$phases_enriched" \
    --argjson clickhouse_tables "$clickhouse_tables_json" \
    --argjson preflight "$preflight" \
    --argjson topology_snapshot "$topology_snapshot" \
    --argjson topology_bundle "$topology_bundle_meta" \
    --argjson evaluation_eligible "$evaluation_eligible" \
    '{schema_version:$schema_version, timeline:"continuous",
      case_id:$case_id, scenario_id:$scenario_id,
      scenario_metadata:$scenario_metadata,
      scenario_metadata_sha256:$scenario_metadata_sha256,
      case_label:$case_label, evaluation_eligible:$evaluation_eligible,
      time_basis:"UTC", t1:$t1, t2:$t2, capture_start:$capture_start,
      capture_end:$capture_end, t1_kst:$t1_kst, t2_kst:$t2_kst,
      capture_start_kst:$capture_start_kst, capture_end_kst:$capture_end_kst,
      dump_started_at:$dump_started_at, dump_completed_at:$dump_completed_at,
      dump_started_at_kst:$dump_started_at_kst, dump_completed_at_kst:$dump_completed_at_kst,
      model_snapshot_at:$model_snapshot_at, model_snapshot_at_kst:$model_snapshot_at_kst,
      model_snapshot_lag_sec:$model_snapshot_lag_sec,
      model_source_path:$model_source_path, model_sha256:$model_sha256,
      run_script_sha256:$run_script_sha256, run_catalog_sha256:$run_catalog_sha256,
      run_plan_sha256:$run_plan_sha256,
      phases:$phases, clickhouse_tables:$clickhouse_tables,
      preflight:$preflight, topology_snapshot:$topology_snapshot,
      topology_bundle:$topology_bundle,
      golden_anomaly_file:false}' > "$staging_dir/meta.json"

  jq -e \
    --arg schema_version '2.0' \
    --arg capture_end "$capture_end" \
    --arg case_label "$case_label" \
    --arg scenario_metadata_sha256 "$scenario_metadata_sha256" \
    '.schema_version == $schema_version and .timeline == "continuous" and
     .time_basis == "UTC" and .capture_end == $capture_end and
     .scenario_metadata_sha256 == $scenario_metadata_sha256 and
     (.scenario_metadata.title | length > 0) and
     (.scenario_metadata.description | length > 0) and
     (.scenario_metadata.cause | length > 0) and
     (.scenario_metadata.injection_summary | length > 0) and
     (.scenario_metadata.user_impact | length > 0) and
     (.scenario_metadata.distinguishing_evidence | length > 0) and
     .model_snapshot_at >= $capture_end and .case_label == $case_label and
     .golden_anomaly_file == false and
     (.phases | type == "array" and length == 5) and
     (.clickhouse_tables | type == "array" and length == 12) and
     (.topology_bundle == null or
       (.topology_bundle.path == "topology/" and
        (.topology_bundle.snapshot_count | type == "number") and
        (.topology_bundle.capture_failure_count | type == "number") and
        (.topology_bundle.interval_seconds | type == "number"))) and
     (.topology_snapshot.snapshot_at | type == "string" and length > 0) and
     (if $case_label == "evaluation" then
        .evaluation_eligible == true and
        (.preflight.verdict | IN("clean", "clean_after_wait", "ai_judged_clean")) and
        (.run_plan_sha256 | test("^[0-9a-f]{64}$")) and
        (.run_script_sha256 | test("^[0-9a-f]{64}$")) and
        (.run_catalog_sha256 | test("^[0-9a-f]{64}$"))
      else .evaluation_eligible == false end)' \
    "$staging_dir/meta.json" >/dev/null || die 'meta.json violates capture policy'
else
  jq -n \
    --arg schema_version '1.3' \
    --arg case_id "$case_id" \
    --arg scenario_id "$scenario_id" \
    --argjson scenario_metadata "$scenario_metadata" \
    --arg scenario_metadata_sha256 "$scenario_metadata_sha256" \
    --arg case_label "$case_label" \
    --arg t1 "$t1" \
    --arg t2 "$t2" \
    --arg capture_start "$capture_start" \
    --arg capture_end "$capture_end" \
    --arg t1_kst "$t1_kst" \
    --arg t2_kst "$t2_kst" \
    --arg capture_start_kst "$capture_start_kst" \
    --arg capture_end_kst "$capture_end_kst" \
    --arg dump_started_at "$dump_started_at" \
    --arg dump_completed_at "$dump_completed_at" \
    --arg dump_started_at_kst "$dump_started_at_kst" \
    --arg dump_completed_at_kst "$dump_completed_at_kst" \
    --arg model_snapshot_at "$model_snapshot_at" \
    --arg model_snapshot_at_kst "$model_snapshot_at_kst" \
    --argjson model_snapshot_lag_sec "$model_snapshot_lag_sec" \
    --arg model_source_path "$MODEL_SOURCE" \
    --arg model_sha256 "$model_sha256" \
    --arg run_script_sha256 "$run_script_sha256" \
    --arg run_catalog_sha256 "$run_catalog_sha256" \
    --arg run_plan_sha256 "$run_plan_sha256" \
    --argjson segments "$segments" \
    --argjson rebase "$rebase" \
    --argjson normal_provenance "$normal_provenance" \
    --argjson preflight "$preflight" \
    --argjson topology_snapshot "$topology_snapshot" \
    --argjson evaluation_eligible "$evaluation_eligible" \
    '{schema_version:$schema_version, case_id:$case_id, scenario_id:$scenario_id,
      scenario_metadata:$scenario_metadata,
      scenario_metadata_sha256:$scenario_metadata_sha256,
      case_label:$case_label, evaluation_eligible:$evaluation_eligible,
      time_basis:"UTC", t1:$t1, t2:$t2, capture_start:$capture_start,
      capture_end:$capture_end, t1_kst:$t1_kst, t2_kst:$t2_kst,
      capture_start_kst:$capture_start_kst, capture_end_kst:$capture_end_kst,
      dump_started_at:$dump_started_at, dump_completed_at:$dump_completed_at,
      dump_started_at_kst:$dump_started_at_kst, dump_completed_at_kst:$dump_completed_at_kst,
      model_snapshot_at:$model_snapshot_at, model_snapshot_at_kst:$model_snapshot_at_kst,
      model_snapshot_lag_sec:$model_snapshot_lag_sec,
      model_source_path:$model_source_path, model_sha256:$model_sha256,
      run_script_sha256:$run_script_sha256, run_catalog_sha256:$run_catalog_sha256,
      run_plan_sha256:$run_plan_sha256,
      segments:$segments, rebase:$rebase, normal_provenance:$normal_provenance,
      preflight:$preflight, topology_snapshot:$topology_snapshot,
      golden_anomaly_file:false}' > "$staging_dir/meta.json"

  jq -e \
    --arg schema_version '1.3' \
    --arg capture_end "$capture_end" \
    --arg case_label "$case_label" \
    --arg scenario_metadata_sha256 "$scenario_metadata_sha256" \
    '.schema_version == $schema_version and
     .time_basis == "UTC" and .capture_end == $capture_end and
     .scenario_metadata_sha256 == $scenario_metadata_sha256 and
     (.scenario_metadata.title | length > 0) and
     (.scenario_metadata.description | length > 0) and
     (.scenario_metadata.cause | length > 0) and
     (.scenario_metadata.injection_summary | length > 0) and
     (.scenario_metadata.user_impact | length > 0) and
     (.scenario_metadata.distinguishing_evidence | length > 0) and
     .model_snapshot_at >= $capture_end and .case_label == $case_label and
     .golden_anomaly_file == false and
     (.segments | type == "array" and length >= 1) and
     (.topology_snapshot.snapshot_at | type == "string" and length > 0) and
     (if $case_label == "evaluation" then
        .evaluation_eligible == true and
        (.preflight.verdict | IN("clean", "clean_after_wait", "ai_judged_clean")) and
        (.run_plan_sha256 | test("^[0-9a-f]{64}$")) and
        (.run_script_sha256 | test("^[0-9a-f]{64}$")) and
        (.run_catalog_sha256 | test("^[0-9a-f]{64}$"))
      else .evaluation_eligible == false end)' \
    "$staging_dir/meta.json" >/dev/null || die 'meta.json violates capture policy'
fi

# Group-readable promotion (spec §2.2, 2026-07-24): a case staged under a
# restrictive umask must not read as "no data" to consumers on the storage
# host's shared group (07-24 root-700 사고 재발 방지). Fixed up on staging_dir
# right before the atomic rename so final_dir never exists with stale perms.
chmod -R g+rX "$staging_dir"
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
