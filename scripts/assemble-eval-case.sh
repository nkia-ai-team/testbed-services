#!/usr/bin/env bash
# Assemble a continuous-timeline case (spec-eval-data-capture §2.1/§4, 2026-07-20).
#
# Copies the day's normal prefix (VM export + ClickHouse parquet only), shifts
# its event-time forward by delta = (t1-10m) - normal_end so the prefix ends at
# the scenario window's start (the seam at t1-10m), and concatenates it with the
# scenario segment into <case-dir>/assembled/. The scenario segment keeps real
# wall-clock time; PG dump, model artifact and topology are NOT assembled.
# Originals under data/ and normal-segments/ are never mutated.

set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage:
  assemble-eval-case.sh --case-dir <staging-or-case dir> \
    --normal-segment <normal-segments/<domain>/<date> dir> \
    --delta-sec <int, (t1-10m) - normal_end in seconds> [--dry-run]

Requires (live mode only) a ClickHouse-local capable runner for the parquet
time-shift; by default the clickhouse-server image is used via docker:
  CH_LOCAL_IMAGE   default: clickhouse/clickhouse-server:latest
EOF
}

die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }
log() { printf '[INFO] %s %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"; }
require_command() { command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"; }

case_dir=''
normal_segment=''
delta_sec=''
dry_run=false
vm_only=false   # assemble only the (deterministic) VM export; skip parquet

while (( $# > 0 )); do
  case "$1" in
    --case-dir) case_dir="${2:-}"; shift 2 ;;
    --normal-segment) normal_segment="${2:-}"; shift 2 ;;
    --delta-sec) delta_sec="${2:-}"; shift 2 ;;
    --dry-run) dry_run=true; shift ;;
    --vm-only) vm_only=true; shift ;;
    --self-check) self_check=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

# --self-check: prove the clickhouse-local parquet path end-to-end — image
# runnable AND the host-vs-container work-dir mount translation actually
# delivers files (the 2026-07-21 live failure mode). A tiny parquet is written
# and read back through the exact ch_local/work-mount code path used live.
if [[ "${self_check:-false}" == true ]]; then
  require_command docker
  root="${EVAL_CASE_ROOT:-/data/eval-cases}"
  [[ -d "$root" && -w "$root" ]] || die "self-check: eval case root is not writable: $root"
  work=$(mktemp -d "${root%/}/.assemble-selfcheck.XXXXXX")
  trap '[[ -n "${work:-}" ]] && rm -rf "$work"' EXIT
  work_mount="$work"
  if [[ -n "${CAPTURE_HOST_OUTPUT_ROOT:-}" ]]; then
    work_mount="${CAPTURE_HOST_OUTPUT_ROOT%/}/$(basename "$work")"
  fi
  CH_LOCAL_IMAGE="${CH_LOCAL_IMAGE:-clickhouse/clickhouse-server:latest}"
  docker run --rm --user "$(id -u):$(id -g)" --volume "$work_mount:/work" \
    --entrypoint clickhouse "$CH_LOCAL_IMAGE" local --query \
    "INSERT INTO FUNCTION file('/work/probe.parquet', Parquet) SELECT 1 AS x" ||
    die 'self-check: clickhouse-local parquet write failed'
  count=$(docker run --rm --user "$(id -u):$(id -g)" --volume "$work_mount:/work" \
    --entrypoint clickhouse "$CH_LOCAL_IMAGE" local --query \
    "SELECT count() FROM file('/work/probe.parquet', Parquet)") ||
    die 'self-check: clickhouse-local parquet read failed'
  [[ "$count" == "1" ]] || die "self-check: parquet roundtrip mismatch: $count"
  log 'self-check passed: clickhouse-local parquet shift path is provisioned'
  exit 0
fi

require_command python3
[[ -n "$case_dir" ]] || die '--case-dir is required'
[[ -d "$case_dir" ]] || die "case dir does not exist: $case_dir"
[[ -n "$normal_segment" ]] || die '--normal-segment is required'
[[ -d "$normal_segment" ]] || die "normal segment dir does not exist: $normal_segment"
[[ "$delta_sec" =~ ^-?[0-9]+$ ]] || die '--delta-sec must be an integer'

scenario_vm="$case_dir/data/victoriametrics.export"
normal_vm="${normal_segment%/}/data/victoriametrics.export"
[[ -r "$scenario_vm" ]] || die "scenario VM export missing: $scenario_vm"
[[ -r "$normal_vm" ]] || die "normal VM export missing: $normal_vm"

CH_TABLES=(otel_traces_local lucida_logs_local lucida_events_local)
declare -A CH_TIME_COL=(
  [otel_traces_local]=timestamp
  [lucida_logs_local]=timestamp
  [lucida_events_local]=occurred_at
)

if [[ "$dry_run" == true ]]; then
  plan=$(python3 - "$delta_sec" <<'PY'
import json, sys
print(json.dumps({
  "mode": "dry-run",
  "delta_sec": int(sys.argv[1]),
  "victoriametrics": "shift normal timestamps by delta_sec*1000 ms, then concat scenario",
  "clickhouse": {
    "otel_traces_local": "timestamp + delta_sec s",
    "lucida_logs_local": "timestamp + delta_sec s",
    "lucida_events_local": "occurred_at + delta_sec s",
  },
}))
PY
)
  printf '%s\n' "$plan"
  exit 0
fi

assembled="$case_dir/assembled"
[[ ! -e "$assembled" ]] || die "assembled/ already exists (case is immutable): $assembled"
mkdir -p "$assembled/clickhouse"

# ------------------------------------------------------------
# VictoriaMetrics export: JSON lines {metric, values[], timestamps[ms]}.
# Shift the normal prefix forward by delta_sec, then concatenate the scenario
# window (real wall clock). Deterministic and pure — this is the tested core.
# ------------------------------------------------------------
log "assembling VictoriaMetrics export (delta=${delta_sec}s)"
python3 - "$normal_vm" "$scenario_vm" "$assembled/victoriametrics.export" "$delta_sec" <<'PY'
import json, sys
normal_path, scenario_path, out_path, delta_sec = sys.argv[1:5]
delta_ms = int(delta_sec) * 1000
with open(out_path, "w", encoding="utf-8") as out:
    with open(normal_path, encoding="utf-8") as normal:
        for line in normal:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            row["timestamps"] = [t + delta_ms for t in row.get("timestamps", [])]
            out.write(json.dumps(row, separators=(",", ":")) + "\n")
    with open(scenario_path, encoding="utf-8") as scenario:
        for line in scenario:
            line = line.strip()
            if line:
                out.write(line + "\n")
PY
[[ -s "$assembled/victoriametrics.export" ]] || die 'assembled VM export is empty'

if [[ "$vm_only" == true ]]; then
  log "assembled VM export written (vm-only): $assembled/victoriametrics.export"
  exit 0
fi

# ------------------------------------------------------------
# ClickHouse parquet: shift the normal prefix's event-time column forward by
# delta_sec and union with the scenario parquet. Uses `clickhouse local`.
# NOTE: the parquet time-shift path requires a ClickHouse-local runtime and has
# not been validated end-to-end here (no clickhouse CLI on the dev host); see
# report "결정 필요: assembled parquet 시프트 라이브 검증".
# ------------------------------------------------------------
CH_LOCAL_IMAGE="${CH_LOCAL_IMAGE:-clickhouse/clickhouse-server:latest}"
ch_local() {
  local query=$1 mount=$2
  if command -v clickhouse-local >/dev/null 2>&1; then
    clickhouse-local --query "$query"
  elif command -v clickhouse >/dev/null 2>&1; then
    clickhouse local --query "$query"
  else
    require_command docker
    docker run --rm --user "$(id -u):$(id -g)" --volume "$mount:/work" \
      --entrypoint clickhouse "$CH_LOCAL_IMAGE" local --query "$query"
  fi
}

for table in "${CH_TABLES[@]}"; do
  col="${CH_TIME_COL[$table]}"
  normal_pq="${normal_segment%/}/data/clickhouse/${table}.parquet"
  scenario_pq="$case_dir/data/clickhouse/${table}.parquet"
  out_pq="$assembled/clickhouse/${table}.parquet"
  [[ -r "$scenario_pq" ]] || die "scenario parquet missing: $scenario_pq"
  [[ -r "$normal_pq" ]] || die "normal parquet missing: $normal_pq"
  log "assembling ClickHouse table=$table (shift $col by ${delta_sec}s)"
  # Work dir MUST live under the case dir, not /tmp: this script runs inside
  # the runner container but `docker run -v` resolves paths on the HOST daemon
  # (host-mounted docker.sock). /tmp differs between the two; the eval-cases
  # tree is the shared root. CAPTURE_HOST_OUTPUT_ROOT translates when the two
  # roots differ (pg_dump precedent in capture-eval-case.sh). 2026-07-21 라이브
  # 실패(CANNOT_STAT /work/normal.parquet) 실측 수리.
  work=$(mktemp -d "${case_dir%/}/.ch-shift.XXXXXX")
  work_mount="$work"
  if [[ -n "${CAPTURE_HOST_OUTPUT_ROOT:-}" ]]; then
    work_mount="${CAPTURE_HOST_OUTPUT_ROOT%/}/$(basename "$case_dir")/$(basename "$work")"
  fi
  cp "$normal_pq" "$work/normal.parquet"
  cp "$scenario_pq" "$work/scenario.parquet"
  ch_local "
    INSERT INTO FUNCTION file('/work/out.parquet', Parquet)
    SELECT * REPLACE ($col + INTERVAL $delta_sec SECOND AS $col)
      FROM file('/work/normal.parquet', Parquet)
    UNION ALL
    SELECT * FROM file('/work/scenario.parquet', Parquet)
  " "$work_mount"
  [[ -s "$work/out.parquet" ]] || { rm -rf "$work"; die "assembled parquet is empty: $table"; }
  mv "$work/out.parquet" "$out_pq"
  rm -rf "$work"
done

log "assembled continuous timeline written: $assembled"
