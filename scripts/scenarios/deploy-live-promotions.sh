#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
scenario_root="$repo_root/scripts/scenarios"
remote=${SCENARIO_REMOTE:-root@192.168.200.109}
remote_root=${SCENARIO_REMOTE_ROOT:-/root/testbed-services/scripts/scenarios}
runner_url=${SCENARIO_RUNNER_URL:-http://localhost:8091}
runner_repo=${RCA_SCENARIO_RUNNER_REPO:-"$repo_root/../rca-scenario-runner"}

cd "$repo_root"
python3 -m unittest discover -s scripts/scenarios/tests -q
bash scripts/scenarios/test-scenarios.sh
python3 scripts/scenarios/generate-manifests.py --check

# Validate the exact external manifests against the production runner schema
# before publishing a registry entry that could block the active queue.
(
  cd "$runner_repo/backend"
  RCA_TRACE_DIR=/tmp/rca-traces uv run pytest tests -q
  # Derived from the catalog, not pinned: the pinned 64 outlived the catalog's
  # move to 60 and this assertion had been failing ever since, blocking the
  # deploy path it was meant to guard.
  uv run python -c \
    'import json;from pathlib import Path;from app.manifests import load_manifests;root=Path("../../testbed-services/scripts/scenarios");expected=len(json.loads((root/"catalog.json").read_text())["scenarios"]);manifests=load_manifests(root/"manifests");assert len({item.id for item in manifests.values()}) == expected, f"manifests {len(manifests)} != catalog {expected}"'
)

# Publish the load scripts the live profiles reference. 2026-07-28: this step did
# not exist, and tb-runner still held only what was hand-copied on 07-14/07-15 —
# commerce/surge.js and the three baseline script.js. Every scenario authored
# since (banking surge, frozen-bypass, transfer-heavy-surge, the slowquery trio,
# the food surge family) named a path that was not on the machine that runs it.
# Seven already-ready scenarios would have failed the batch smoke on "no such
# file". The registry is the source of truth for which paths must exist, so the
# list is derived from it rather than restated here.
loadgen_paths=$(python3 - "$scenario_root/registry/profiles.json" <<'PY'
import json, sys
profiles = json.load(open(sys.argv[1]))["profiles"]
paths = set()
for profile in profiles.values():
    contract = profile.get("parameter_contract", {})
    paths.update(contract.get("allowed_script_paths", []))
print("\n".join(sorted(paths)))
PY
)
# /opt/loadgen/<domain>/<file> mirrors <domain>/loadgen/<file> in this repo.
loadgen_repo_files=""
while read -r remote_path; do
  [[ -n "$remote_path" ]] || continue
  domain=$(basename "$(dirname "$remote_path")")
  file=$(basename "$remote_path")
  source_file="$repo_root/$domain/loadgen/$file"
  [[ -f "$source_file" ]] || { echo "load script missing from repo: $source_file" >&2; exit 1; }
  loadgen_repo_files+="$domain/loadgen/$file"$'\n'
done <<<"$loadgen_paths"

# Observation plane separation (docs/spec-scenario-observation-plane.md): the
# resident baseline units publish a per-domain live document, so tb-runner needs
# the shared monitor, the sh helper the three entrypoints source, and the
# entrypoints themselves. Without these the units keep running the old script and
# no baseline document ever appears — observations then fail closed, silently.
baseline_shared_files=(
  "scripts/scenarios/profiles/loadgen_monitor.py"
  "scripts/loadgen/baseline-publish.sh"
)
baseline_domains=(commerce core-banking food-delivery)
for domain in "${baseline_domains[@]}"; do
  loadgen_repo_files+="$domain/loadgen/entrypoint.sh"$'\n'
done
for shared in "${baseline_shared_files[@]}"; do
  [[ -f "$repo_root/$shared" ]] || { echo "baseline file missing from repo: $shared" >&2; exit 1; }
  loadgen_repo_files+="$shared"$'\n'
done

# The baseline script.js files are deliberately not shipped here — they belong to
# the resident loadgen-* units and replacing one would need a unit restart.
tar czf - -C "$repo_root" $(echo "$loadgen_repo_files" | tr '\n' ' ') \
  | ssh "$remote" "cat > /tmp/loadgen-publish.tgz && \
      scp -q -i ~/.ssh/tb_key /tmp/loadgen-publish.tgz nkia@192.168.122.206:/tmp/ && \
      ssh -i ~/.ssh/tb_key nkia@192.168.122.206 '
        set -e
        rm -rf /tmp/loadgen-stage && mkdir -p /tmp/loadgen-stage
        tar xzf /tmp/loadgen-publish.tgz -C /tmp/loadgen-stage
        for src in /tmp/loadgen-stage/*/loadgen/*.js /tmp/loadgen-stage/*/loadgen/entrypoint.sh; do
          [ -f \$src ] || continue
          domain=\$(basename \$(dirname \$(dirname \$src)))
          sudo install -m 644 -o root -g root \$src /opt/loadgen/\$domain/\$(basename \$src)
        done
        sudo install -m 644 -o root -g root \
          /tmp/loadgen-stage/scripts/scenarios/profiles/loadgen_monitor.py /opt/loadgen/loadgen_monitor.py
        sudo install -m 644 -o root -g root \
          /tmp/loadgen-stage/scripts/loadgen/baseline-publish.sh /opt/loadgen/baseline-publish.sh
        rm -rf /tmp/loadgen-stage /tmp/loadgen-publish.tgz'
      rm -f /tmp/loadgen-publish.tgz"

# Verify by content, not by exit status: a silently truncated copy still exits 0.
# Every published file is checked, not just the k6 scripts — the baseline monitor
# and the entrypoints are just as load-bearing now.
verify_pairs=""
while read -r remote_path; do
  [[ -n "$remote_path" ]] || continue
  domain=$(basename "$(dirname "$remote_path")")
  file=$(basename "$remote_path")
  verify_pairs+="$remote_path|$repo_root/$domain/loadgen/$file"$'\n'
done <<<"$loadgen_paths"
for domain in "${baseline_domains[@]}"; do
  verify_pairs+="/opt/loadgen/$domain/entrypoint.sh|$repo_root/$domain/loadgen/entrypoint.sh"$'\n'
done
verify_pairs+="/opt/loadgen/loadgen_monitor.py|$repo_root/scripts/scenarios/profiles/loadgen_monitor.py"$'\n'
verify_pairs+="/opt/loadgen/baseline-publish.sh|$repo_root/scripts/loadgen/baseline-publish.sh"$'\n'

while IFS='|' read -r remote_path local_path; do
  [[ -n "$remote_path" ]] || continue
  local_sum=$(md5sum "$local_path" | cut -d' ' -f1)
  remote_sum=$(ssh "$remote" "ssh -i ~/.ssh/tb_key nkia@192.168.122.206 'md5sum $remote_path'" | cut -d' ' -f1)
  [[ "$local_sum" == "$remote_sum" ]] \
    || { echo "load script mismatch on tb-runner: $remote_path" >&2; exit 1; }
done <<<"$verify_pairs"

# An entrypoint on disk is not an entrypoint in effect: the resident unit keeps
# running whatever it started with. Publishing without restarting would leave a
# green md5 next to a baseline that publishes nothing. Report it rather than
# restarting here — dropping baseline traffic mid-deploy is the caller's call.
# The unit each domain's entrypoint belongs to. test_loadgen_monitor.py locks
# this same pairing against profiles.json, so it cannot drift silently.
declare -A baseline_units=(
  [commerce]=loadgen-commerce
  [core-banking]=loadgen-banking
  [food-delivery]=loadgen-food
)
for domain in "${baseline_domains[@]}"; do
  unit=${baseline_units[$domain]}
  echo "[deploy] $unit: entrypoint published — restart for it to take effect:"
  echo "[deploy]   ssh nkia@192.168.122.206 'sudo systemctl restart $unit'"
done

# 2026-07-30: service error rate moved off the APM rollup onto the trace table,
# which needs a credential the runner did not previously hold. Without it every
# clickhouse observation returns quality=error, and an unusable signal in
# must_rule_out blocks success for the 13 scenarios that read it — a whole batch
# would fail for a reason no tick record explains. Fail here instead, loudly.
ssh "$remote" bash -s << 'PREFLIGHT'
set -Eeuo pipefail
container=$(docker ps --format '{{.Names}}' | grep -E 'rca-scenario-runner|scenario-runner' | head -1)
[[ -n "$container" ]] || { echo "runner container not found" >&2; exit 1; }
docker exec "$container" printenv CLICKHOUSE_PASSWORD > /dev/null 2>&1 || {
  echo "runner is missing CLICKHOUSE_PASSWORD — clickhouse.service_error_rate cannot authenticate" >&2
  echo "set CLICKHOUSE_USER/CLICKHOUSE_PASSWORD on the runner and recreate it" >&2
  exit 1
}
# Reachability and grant are separate failures; check the query the probe runs.
docker exec "$container" python3 - << 'PROBE'
import json, os, urllib.request
url = os.environ.get("CLICKHOUSE_URL", "http://192.168.230.119:18123/")
sql = (
    "SELECT count() AS n FROM lucida.otel_traces_local "
    "WHERE timestamp > now() - INTERVAL 60 SECOND FORMAT JSON"
)
request = urllib.request.Request(
    url,
    data=sql.encode(),
    headers={
        "X-ClickHouse-User": os.environ.get("CLICKHOUSE_USER", "lucida"),
        "X-ClickHouse-Key": os.environ["CLICKHOUSE_PASSWORD"],
    },
    method="POST",
)
with urllib.request.urlopen(request, timeout=10) as response:
    json.loads(response.read())["data"][0]["n"]
print("[deploy] clickhouse trace query reachable")
PROBE
PREFLIGHT

# Publish implementations and manifests before the registry exposes new live IDs.
rsync -az --exclude registry/controllers.json \
  "$scenario_root/" "$remote:$remote_root/"
rsync -az "$scenario_root/registry/controllers.json" \
  "$remote:$remote_root/registry/controllers.json"

ssh "$remote" \
  "curl -fsS '$runner_url/api/live-queue/readiness' && echo && curl -fsS '$runner_url/api/live-queue'"
