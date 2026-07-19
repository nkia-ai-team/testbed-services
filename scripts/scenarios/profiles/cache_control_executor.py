#!/usr/bin/env python3
"""Snapshot-and-restore Redis availability executor for F11-G."""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance

PROFILE_ID = "cache.control"


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    if scenario_id not in profile["parameter_contract"]["allowed_scenarios"]:
        raise ExecutorError("scenario is not allowlisted")
    if params != profile["scenario_parameters"][scenario_id]:
        raise ExecutorError("parameters do not match the approved scenario profile")
    if params["fault_replicas"] != 0 or params["baseline_replicas"] != 1:
        raise ExecutorError("cache replica transition is not approved")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    p = profile_instance(plan, PROFILE_ID)["parameters"]
    return kubectl_bash_argv([
        action, plan["scenario"]["id"], p["namespace"], p["deployment"],
        str(p["baseline_replicas"]), str(p["fault_replicas"]), p["dependent_deployment"],
    ]), SCRIPT


SCRIPT = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; scenario_id="$2"; ns="$3"; deploy="$4"; baseline="$5"; fault="$6"; dependent="$7"
state_root="${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}"
state="$state_root/${scenario_id}-cache-replicas"
k=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$ns")
current() { "${k[@]}" get deploy "$deploy" -o jsonpath='{.spec.replicas}'; }
check() { command -v kubectl >/dev/null; "${k[@]}" auth can-i update deployments/scale | grep -qx yes; [[ "$(current)" == "$baseline" ]]; "${k[@]}" rollout status deploy/"$deploy" --timeout=1s >/dev/null; "${k[@]}" rollout status deploy/"$dependent" --timeout=1s >/dev/null; }
case "$action" in
  preflight) check; [[ ! -e "$state" ]] ;;
  run) check; mkdir -p "$state_root"; current >"$state.tmp"; mv -T "$state.tmp" "$state"; "${k[@]}" scale deploy "$deploy" --replicas="$fault" >/dev/null ;;
  cleanup) [[ -e "$state" ]] || exit 0; original=$(cat "$state"); [[ "$original" == "$baseline" ]]; "${k[@]}" scale deploy "$deploy" --replicas="$original" >/dev/null; "${k[@]}" rollout status deploy/"$deploy" --timeout=180s >/dev/null; "${k[@]}" rollout status deploy/"$dependent" --timeout=180s >/dev/null; rm -f "$state" ;;
  recovery) [[ ! -e "$state" ]]; [[ "$(current)" == "$baseline" ]]; "${k[@]}" rollout status deploy/"$deploy" --timeout=1s >/dev/null; "${k[@]}" rollout status deploy/"$dependent" --timeout=1s >/dev/null ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
