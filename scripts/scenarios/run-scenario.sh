#!/usr/bin/env bash
# Safe entry point for the 64-scenario catalog. Defaults to a side-effect-free plan.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
catalog="$script_dir/catalog.json"
manifest_dir="$script_dir/manifests"
scenario=""
action="plan"
live=false
plan_digest=""
confirmation=""

usage() {
  cat <<'EOF'
usage: run-scenario.sh --scenario <slug> [--plan|--run|--cleanup] [--dry-run]
       run-scenario.sh --scenario <slug> --run --live --plan-digest <sha256> \
         --confirm LIVE:<SCENARIO_ID>:<sha256>

Dry-run plan output is the default. Live run/cleanup requires the exact current
compiled plan digest and confirmation and is serialized by the trusted dispatcher.
EOF
}

while (( $# )); do
  case "$1" in
    --scenario) scenario="${2:-}"; shift 2 ;;
    --plan|--dry-run) action="plan"; shift ;;
    --run) action="run"; shift ;;
    --cleanup) action="cleanup"; shift ;;
    --live) live=true; shift ;;
    --plan-digest) plan_digest="${2:-}"; shift 2 ;;
    --confirm) confirmation="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -r "$catalog" ]] || { echo "catalog not readable: $catalog" >&2; exit 2; }
[[ -n "$scenario" ]] || { echo "--scenario is required" >&2; exit 2; }
command -v jq >/dev/null || { echo "jq is required" >&2; exit 2; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 2; }

row="$(jq -ce --arg slug "$scenario" '.scenarios[] | select(.slug == $slug)' "$catalog")" ||
  { echo "unknown scenario: $scenario" >&2; exit 2; }
manifest="$manifest_dir/$scenario.yaml"
[[ -r "$manifest" ]] || { echo "manifest not readable: $manifest" >&2; exit 2; }
manifest_json="$(jq -ce . "$manifest")" || { echo "invalid manifest: $manifest" >&2; exit 2; }
if ! jq -e --argjson row "$row" '
  ($row.profiles | map("injector-profiles/" + .)) as $profile_refs |
  .id == $row.id and .slug == $row.slug and .readiness == $row.readiness and
  .injection.location == $row.injection_location and
  .injection.profile_refs == $profile_refs and
  .actions.plan.adapter_refs == $row.profiles and
  .actions.run.adapter_refs == $row.profiles and
  .actions.cleanup.adapter_refs == ($row.profiles | reverse) and
  (if .execution.controller.live_enabled
   then .execution.controller.dispatcher_mode == "trusted" and
        .execution.controller.binding.primary_ref == $row.profiles[0] and
        .execution.controller.binding.companion_refs == $row.profiles[1:] and
        (if $row.load_mode == "adaptive"
         then .execution.controller.runtime.profile.kind == "adaptive_ladder"
         else .execution.controller.runtime.profile.kind == "fixed" end)
   else .execution.controller.profile.kind == $row.load_mode end)
' <<<"$manifest_json" >/dev/null; then
  echo "manifest/catalog mismatch: $scenario" >&2
  exit 2
fi
if ! normalized_plan="$(python3 "$script_dir/compile-plan.py" --scenario "$scenario")"; then
  echo "catalog/manifest/registry compilation failed: $scenario" >&2
  exit 2
fi
if ! jq -e --argjson row "$row" '
  .scenario == $row and
  (.profile_instances | map(.profile_id)) == $row.profiles and
  .cleanup_order == ($row.profiles | reverse) and
  (.plan_digest | test("^[0-9a-f]{64}$")) and
  (.scenario_digest | test("^[0-9a-f]{64}$")) and
  (.manifest_digest | test("^[0-9a-f]{64}$")) and
  (.registry_digest | test("^[0-9a-f]{64}$"))
' <<<"$normalized_plan" >/dev/null; then
  echo "compiled plan mismatch: $scenario" >&2
  exit 2
fi
if [[ "$live" == true ]]; then
  [[ "$action" == "run" || "$action" == "cleanup" ]] || {
    echo "live refused: --run or --cleanup is required" >&2; exit 3;
  }
  [[ -n "$plan_digest" && -n "$confirmation" ]] || {
    echo "live refused: --plan-digest and --confirm are required" >&2; exit 3;
  }
  for override in RUNNER_SCRIPT SCENARIO_CATALOG SCENARIO_MANIFEST_DIR \
    SCENARIO_REGISTRY_DIR SCENARIO_DISPATCHER_PATH SCENARIO_STATE_DIR; do
    [[ -z "${!override:-}" ]] || {
      echo "live refused: caller-controlled $override is forbidden" >&2; exit 3;
    }
  done
  exec python3 "$script_dir/trusted_dispatcher.py" \
    --scenario "$scenario" --action "$action" \
    --plan-digest "$plan_digest" --confirm "$confirmation"
fi

jq -n --argjson scenario "$row" --argjson manifest "$manifest_json" \
  --argjson normalized_plan "$normalized_plan" --arg action "$action" '{
    side_effects: false,
    action: $action,
    scenario: $scenario,
    manifest: $manifest,
    normalized_plan: $normalized_plan,
    digests: {
      scenario: $normalized_plan.scenario_digest,
      manifest: $normalized_plan.manifest_digest,
      registry: $normalized_plan.registry_digest,
      plan: $normalized_plan.plan_digest
    },
    profile_instances: $normalized_plan.profile_instances,
    profile_executor_hashes: ($normalized_plan.profile_instances | map({
      profile_id, executor, executor_sha256
    })),
    observation_query_ids: $normalized_plan.observation_query_ids,
    location_ids: ($normalized_plan.profile_instances | map(.location_id)),
    selected_action: $manifest.actions[$action],
    preflight: $manifest.execution.preflight_ids,
    controller: $manifest.execution.controller,
    cleanup: $manifest.actions.cleanup,
    prerequisite_gate: $manifest.prerequisite_gate,
    live_gate: (if $normalized_plan.live_allowed
      then "exact digest, confirmation, preflight, lease, cleanup, and recovery required"
      else "compiled normalized plan is not live-capable" end),
    capture: $manifest.capture_policy
  }'
