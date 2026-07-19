#!/usr/bin/env bash
# Trusted profile boundary. All actions are plan-only until live_supported is enabled in registry.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd -- "$script_dir/.." && pwd)"
profile_id=""
action="dry-run"
live=false

while (( $# )); do
  case "$1" in
    --profile) profile_id="${2:-}"; shift 2 ;;
    preflight|run|cleanup|recovery|dry-run) action="$1"; shift ;;
    --live) live=true; shift ;;
    *) echo "unsupported argument: $1" >&2; exit 2 ;;
  esac
done

[[ -n "$profile_id" ]] || { echo "--profile is required" >&2; exit 2; }

PROFILE_ID="$profile_id" PROFILE_ACTION="$action" PROFILE_LIVE="$live" \
PROFILE_REGISTRY="$root/registry/profiles.json" python3 - <<'PY'
import json, os, sys
from pathlib import Path

registry = json.loads(Path(os.environ["PROFILE_REGISTRY"]).read_text())
profile_id = os.environ["PROFILE_ID"]
action = os.environ["PROFILE_ACTION"]
profile = registry.get("profiles", {}).get(profile_id)
if profile is None:
    raise SystemExit(f"unknown profile: {profile_id}")
if action not in registry.get("required_actions", []):
    raise SystemExit(f"unsupported action: {action}")
live = os.environ["PROFILE_LIVE"] == "true"
if live and not profile.get("live_supported", False):
    print(f"live refused: profile {profile_id} is not registered as live-supported", file=sys.stderr)
    raise SystemExit(3)
print(json.dumps({
    "schema_version": "1.0",
    "side_effects": False,
    "profile_id": profile_id,
    "action": action,
    "live_requested": live,
    "live_supported": bool(profile.get("live_supported", False)),
    "location_strategy": profile["location_strategy"],
    "allowed_locations": profile["allowed_locations"],
    "contract": {
        "preflight": "read-only",
        "cleanup": "idempotent and scoped",
        "recovery": "read-only verification"
    }
}, sort_keys=True))
PY
