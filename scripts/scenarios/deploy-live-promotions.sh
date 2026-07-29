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
  uv run python -c \
    'from pathlib import Path; from app.manifests import load_manifests; manifests=load_manifests(Path("../../testbed-services/scripts/scenarios/manifests")); assert len({item.id for item in manifests.values()}) == 64'
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

# The baseline script.js files are deliberately not shipped here — they belong to
# the resident loadgen-* units and replacing one would need a unit restart.
tar czf - -C "$repo_root" $(echo "$loadgen_repo_files" | tr '\n' ' ') \
  | ssh "$remote" "cat > /tmp/loadgen-publish.tgz && \
      scp -q -i ~/.ssh/tb_key /tmp/loadgen-publish.tgz nkia@192.168.122.206:/tmp/ && \
      ssh -i ~/.ssh/tb_key nkia@192.168.122.206 '
        set -e
        rm -rf /tmp/loadgen-stage && mkdir -p /tmp/loadgen-stage
        tar xzf /tmp/loadgen-publish.tgz -C /tmp/loadgen-stage
        for src in /tmp/loadgen-stage/*/loadgen/*.js; do
          domain=\$(basename \$(dirname \$(dirname \$src)))
          sudo install -m 644 -o root -g root \$src /opt/loadgen/\$domain/\$(basename \$src)
        done
        rm -rf /tmp/loadgen-stage /tmp/loadgen-publish.tgz'
      rm -f /tmp/loadgen-publish.tgz"

# Verify by content, not by exit status: a silently truncated copy still exits 0.
while read -r remote_path; do
  [[ -n "$remote_path" ]] || continue
  domain=$(basename "$(dirname "$remote_path")")
  file=$(basename "$remote_path")
  local_sum=$(md5sum "$repo_root/$domain/loadgen/$file" | cut -d' ' -f1)
  remote_sum=$(ssh "$remote" "ssh -i ~/.ssh/tb_key nkia@192.168.122.206 'md5sum $remote_path'" | cut -d' ' -f1)
  [[ "$local_sum" == "$remote_sum" ]] \
    || { echo "load script mismatch on tb-runner: $remote_path" >&2; exit 1; }
done <<<"$loadgen_paths"

# Publish implementations and manifests before the registry exposes new live IDs.
rsync -az --exclude registry/controllers.json \
  "$scenario_root/" "$remote:$remote_root/"
rsync -az "$scenario_root/registry/controllers.json" \
  "$remote:$remote_root/registry/controllers.json"

ssh "$remote" \
  "curl -fsS '$runner_url/api/live-queue/readiness' && echo && curl -fsS '$runner_url/api/live-queue'"
