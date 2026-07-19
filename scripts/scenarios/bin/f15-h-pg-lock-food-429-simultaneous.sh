#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "$script_dir/../run-scenario.sh" --scenario "f15-h-pg-lock-food-429-simultaneous" "$@"

