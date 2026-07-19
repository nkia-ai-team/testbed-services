#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "$script_dir/../run-scenario.sh" --scenario "f15-t1-pg-lock-food-oom-exact" "$@"

