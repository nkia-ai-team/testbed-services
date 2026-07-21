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

Selection:
  --golden-dir   use this exact golden directory.
  --golden-root  pick the golden under here whose meta.tod_phase_kst is nearest
                 (circular, minutes) to --for-time (default: now).
Environment (same conventions as capture-golden-state.sh):
  PG_HOST 192.168.230.119  PG_PORT 15432  PG_USER lucida  PG_PASSWORD (required)
  PG_DATABASE lucida        PG_DUMP_IMAGE postgres:16-alpine
  OBSERVER_HEALTH_URL http://192.168.230.119:18087/api/v1/health
  OBSERVER_CONTAINER  default: lucida-ai-observer
  TRAINER_CONTAINER   default: lucida-ai-trainer
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
    --ready-timeout-sec) ready_timeout_sec="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

require_command docker

# --- thaw mode: just restart the trainer (post-evaluation) ---
if [[ "$thaw_only" == true ]]; then
  log "thawing trainer: docker start $trainer_container"
  docker start "$trainer_container" >/dev/null
  log 'trainer thawed'
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

PG_HOST="${PG_HOST:-192.168.230.119}"
PG_PORT="${PG_PORT:-15432}"
PG_USER="${PG_USER:-lucida}"
PG_PASSWORD="${PG_PASSWORD:-}"
PG_DATABASE="${PG_DATABASE:-lucida}"
PG_DUMP_IMAGE="${PG_DUMP_IMAGE:-postgres:16-alpine}"
OBSERVER_HEALTH_URL="${OBSERVER_HEALTH_URL:-http://192.168.230.119:18087/api/v1/health}"
[[ -n "$PG_PASSWORD" ]] || die 'PG_PASSWORD is required'

golden_tod=$(jq -r '.tod_phase_kst' "$meta")
if [[ "$dry_run" == true ]]; then
  jq -n \
    --arg golden_dir "$golden_dir" --arg tod "$golden_tod" \
    --arg observer "$observer_container" --arg trainer "$trainer_container" \
    --argjson freeze "$([[ "$no_freeze" == true ]] && echo false || echo true)" \
    --argjson n_tables "${#golden_tables[@]}" \
    '{mode:"dry-run", action:"restore-golden",
      golden_dir:$golden_dir, golden_tod_kst:$tod, pg_tables:$n_tables,
      freeze_trainer:$freeze, observer_container:$observer, trainer_container:$trainer,
      steps:["freeze trainer","pg_restore --clean AI-state","restart observer","await health"]}'
  exit 0
fi

# ------------------------------------------------------------
# 1. Freeze the trainer (restart-policy=no keeps it down until --thaw).
# ------------------------------------------------------------
if [[ "$no_freeze" != true ]]; then
  if [[ "$(docker inspect -f '{{.State.Running}}' "$trainer_container" 2>/dev/null)" == true ]]; then
    log "freezing trainer: docker stop $trainer_container"
    docker stop "$trainer_container" >/dev/null
  else
    log "trainer already stopped: $trainer_container"
  fi
else
  log 'skipping trainer freeze (--no-freeze)'
fi

# ------------------------------------------------------------
# 2. Restore golden PG AI-state (the 23 tables in the dump). --clean --if-exists
#    drops+recreates only the dumped tables, resetting them to the golden.
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
# session_replication_role=replica (requires superuser — the lucida role is).
# Under replica role the DELETE/COPY order is FK-independent.
delete_sql='SET session_replication_role = replica;'
for t in "${golden_tables[@]}"; do delete_sql+=" DELETE FROM public.\"$t\";"; done
PGPASSWORD="$PG_PASSWORD" docker run --rm --env PGPASSWORD "$PG_DUMP_IMAGE" \
  psql -v ON_ERROR_STOP=1 --host "$PG_HOST" --port "$PG_PORT" --username "$PG_USER" \
  --dbname "$PG_DATABASE" -c "$delete_sql" >/dev/null ||
  die 'emptying AI-state tables failed — trainer is frozen; investigate before injecting'
PGPASSWORD="$PG_PASSWORD" docker run --rm \
  --env PGPASSWORD \
  --volume "$restore_dir:/in:ro" \
  "$PG_DUMP_IMAGE" \
  pg_restore --data-only --disable-triggers --no-owner --no-privileges --exit-on-error \
  --host "$PG_HOST" --port "$PG_PORT" --username "$PG_USER" --dbname "$PG_DATABASE" \
  /in/golden-pg-state.dump ||
  die 'pg_restore failed — trainer is frozen; investigate before injecting'
log 'PostgreSQL AI-state restored (data-only, FK-safe)'

# ------------------------------------------------------------
# 3. Restart the observer so it reloads the golden models.
# ------------------------------------------------------------
log "restarting observer: docker restart $observer_container"
docker restart "$observer_container" >/dev/null

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
