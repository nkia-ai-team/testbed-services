#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "$script_dir/../run-scenario.sh" --scenario "f05-g-invalid-image-no-impact" "$@"

