#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "$script_dir/../run-scenario.sh" --scenario "f08-g-distractor-rollout-oracle-lock" "$@"

