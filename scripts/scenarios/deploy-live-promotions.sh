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

# Publish implementations and manifests before the registry exposes new live IDs.
rsync -az --exclude registry/controllers.json \
  "$scenario_root/" "$remote:$remote_root/"
rsync -az "$scenario_root/registry/controllers.json" \
  "$remote:$remote_root/registry/controllers.json"

ssh "$remote" \
  "curl -fsS '$runner_url/api/live-queue/readiness' && echo && curl -fsS '$runner_url/api/live-queue'"
