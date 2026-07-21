#!/usr/bin/env bash
# Opportunistic time-of-day golden library builder (cron on the AI-stack host, 119).
#
# A golden must reflect a FAULT-FREE trainer brain for its time-of-day. During a
# running scenario queue the trainer is reset+frozen before every scenario
# (restore-golden-state.sh), so a golden captured mid-queue would be a contaminated
# hybrid. We therefore capture ONLY during a genuinely clean learning stretch,
# detected cheaply via the trainer container's uptime:
#
#   - thaw un-freezes the trainer with `docker start`, resetting its StartedAt, so
#     during the queue the trainer's uptime stays below the R6 inter-scenario gap.
#   - when the queue is idle / paused / in the nightly protection window / done,
#     the trainer runs continuously on clean baseline and its uptime grows.
#
# So "trainer uptime >= MIN_TRAINER_UPTIME_SEC" means "no scenario reset recently →
# genuinely clean" and is the guard for capturing. The set is pruned to one golden
# per time-of-day bucket (newest wins) so it stays bounded and self-refreshing;
# restore-golden-state.sh then picks the nearest-tod golden per scenario.
#
# Install (119 crontab, every 2h):
#   0 */2 * * * . /opt/lucida/golden.env && /opt/lucida/capture-golden-cron.sh >> /var/log/capture-golden-cron.log 2>&1
set -Eeuo pipefail

GOLDEN_ROOT="${GOLDEN_ROOT:-/data/eval-cases/goldens}"
CAPTURE_SCRIPT="${CAPTURE_SCRIPT:-/opt/lucida/capture-golden-state.sh}"
TRAINER_CONTAINER="${TRAINER_CONTAINER:-lucida-ai-trainer}"
MIN_TRAINER_UPTIME_SEC="${MIN_TRAINER_UPTIME_SEC:-5400}"  # 90m of clean learning
WINDOW_MINUTES="${WINDOW_MINUTES:-20}"                    # normal window (unused by
                                                         # light restore; keep small)
BUCKET_MINUTES="${BUCKET_MINUTES:-120}"                  # one golden per 2h tod slot

log() { printf '[golden-cron] %s %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"; }

# --- Guard: capture only during a genuinely clean learning stretch. ---
running=$(docker inspect -f '{{.State.Running}}' "$TRAINER_CONTAINER" 2>/dev/null || echo missing)
if [[ "$running" != true ]]; then
  log "skip: trainer not running (frozen/absent: $running) — scenario likely active"
  exit 0
fi
started=$(docker inspect -f '{{.State.StartedAt}}' "$TRAINER_CONTAINER")
uptime=$(( $(date +%s) - $(date -d "$started" +%s) ))
if (( uptime < MIN_TRAINER_UPTIME_SEC )); then
  log "skip: trainer uptime ${uptime}s < ${MIN_TRAINER_UPTIME_SEC}s (recent reset — not clean)"
  exit 0
fi

# --- Capture this tod's golden (freshness gate inside the capture script). ---
log "capturing golden (trainer uptime ${uptime}s, window ${WINDOW_MINUTES}m)"
"$CAPTURE_SCRIPT" --window-minutes "$WINDOW_MINUTES" --label cron-tod

# --- Prune: keep only the newest golden per BUCKET_MINUTES time-of-day slot. ---
log "pruning golden library to newest-per-${BUCKET_MINUTES}m-tod-bucket"
python3 - "$GOLDEN_ROOT" "$BUCKET_MINUTES" <<'PY'
import json, os, shutil, sys
root, bucket_min = sys.argv[1], int(sys.argv[2])
best = {}  # bucket -> (captured_at, dir)
for name in os.listdir(root):
    d = os.path.join(root, name)
    meta = os.path.join(d, "meta.json")
    if name.startswith(".") or not os.path.isfile(meta):
        continue
    try:
        m = json.load(open(meta))
        hh, mm = (int(x) for x in m["tod_phase_kst"].split(":"))
        bucket = (hh * 60 + mm) // bucket_min
        cap = m["captured_at"]
    except Exception:
        continue
    if bucket not in best or cap > best[bucket][0]:
        best[bucket] = (cap, d)
keep = {d for _, d in best.values()}
for name in os.listdir(root):
    d = os.path.join(root, name)
    if name.startswith(".") or not os.path.isfile(os.path.join(d, "meta.json")):
        continue
    if d not in keep:
        print(f"[golden-cron] prune superseded golden: {d}")
        shutil.rmtree(d)
PY
log "done"
