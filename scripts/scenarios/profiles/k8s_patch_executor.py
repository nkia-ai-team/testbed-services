#!/usr/bin/env python3
"""Original-value Kubernetes resource patch executor."""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance

PROFILE_ID = "k8s.patch"


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    if scenario_id not in profile["parameter_contract"]["allowed_scenarios"]:
        raise ExecutorError("scenario is not allowlisted")
    required = {"deployment", "container", "baseline_cpu_limit", "fault_cpu_limit"}
    if set(params) != required:
        raise ExecutorError("parameters do not match the approved schema")
    levels = profile.get("scenario_levels", {}).get(scenario_id, [])
    if params not in [level["parameters"] for level in levels]:
        raise ExecutorError("parameters must exactly match a predeclared scenario level")
    if params["baseline_cpu_limit"] != "500m":
        raise ExecutorError("CPU baseline is outside the approved profile")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    location = instance["location"]
    if location.get("namespace") != "rca-testbed-commerce":
        raise ExecutorError("k8s.patch requires the canonical commerce namespace")
    return kubectl_bash_argv([
        action, plan["scenario"]["id"], location["namespace"], p["deployment"],
        p["container"], p["baseline_cpu_limit"], p["fault_cpu_limit"],
    ]), SCRIPT


SCRIPT = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; scenario_id="$2"; ns="$3"; deploy="$4"; container="$5"; baseline="$6"; fault="$7"
state_root="${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}"
state="$state_root/${scenario_id}-cpu-limit"
k=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$ns")
current() { "${k[@]}" get deploy "$deploy" -o "jsonpath={.spec.template.spec.containers[?(@.name=='$container')].resources.limits.cpu}"; }
check() { command -v kubectl >/dev/null; "${k[@]}" auth can-i patch deployments | grep -qx yes; [[ "$(current)" == "$baseline" ]]; "${k[@]}" rollout status deploy/"$deploy" --timeout=1s >/dev/null; }
case "$action" in
  preflight) check; [[ ! -e "$state" ]] ;;
  run) check; mkdir -p "$state_root"; current >"$state.tmp"; mv -T "$state.tmp" "$state"
    # kubectl set resources is client-side get-modify-update; right after a rollout it can
    # race the controller's status writes and die on a conflict (seen: F09-P run 2333e298,
    # 3s after level transition). Retry absorbs the transient conflict.
    ok=""; for _ in 1 2 3; do
      if "${k[@]}" set resources deploy "$deploy" -c "$container" --limits="cpu=$fault" >/dev/null; then ok=1; break; fi
      sleep 2
    done; [[ -n "$ok" ]] ;;
  cleanup) [[ -e "$state" ]] || exit 0; original=$(cat "$state"); [[ "$original" == "$baseline" ]]
    ok=""; for _ in 1 2 3; do
      if "${k[@]}" set resources deploy "$deploy" -c "$container" --limits="cpu=$original" >/dev/null; then ok=1; break; fi
      sleep 2
    done; [[ -n "$ok" ]]
    "${k[@]}" rollout status deploy/"$deploy" --timeout=180s >/dev/null; rm -f "$state" ;;
  recovery) [[ ! -e "$state" ]]; [[ "$(current)" == "$baseline" ]]; "${k[@]}" rollout status deploy/"$deploy" --timeout=1s >/dev/null ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
