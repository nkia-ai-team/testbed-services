#!/usr/bin/env python3
"""Failing-image rollout executor that preserves the original Deployment image."""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance

PROFILE_ID = "k8s.lifecycle"


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    if scenario_id not in profile["parameter_contract"]["allowed_scenarios"]:
        raise ExecutorError("scenario is not allowlisted")
    if params != profile["scenario_parameters"][scenario_id]:
        raise ExecutorError("parameters do not match the approved scenario profile")
    if not params["invalid_image"].endswith(":scenario-invalid-f05-g"):
        raise ExecutorError("invalid image is not scenario-scoped")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    p = profile_instance(plan, PROFILE_ID)["parameters"]
    return kubectl_bash_argv([
        action, plan["scenario"]["id"], p["namespace"], p["deployment"],
        p["container"], p["baseline_image"], p["invalid_image"],
    ]), SCRIPT


SCRIPT = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; scenario_id="$2"; ns="$3"; deploy="$4"; container="$5"; baseline="$6"; invalid="$7"
state_root="${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}"
state="$state_root/${scenario_id}-image"
k=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$ns")
current() { "${k[@]}" get deploy "$deploy" -o "jsonpath={.spec.template.spec.containers[?(@.name=='$container')].image}"; }
ready() { "${k[@]}" get deploy "$deploy" -o jsonpath='{.status.readyReplicas}'; }
check() { command -v kubectl >/dev/null; "${k[@]}" auth can-i patch deployments | grep -qx yes; [[ "$(current)" == "$baseline" ]]; [[ "$(ready)" =~ ^[1-9][0-9]*$ ]]; }
case "$action" in
  preflight) check; [[ ! -e "$state" ]] ;;
  run) check; mkdir -p "$state_root"; current >"$state.tmp"; mv -T "$state.tmp" "$state"; "${k[@]}" set image deploy/"$deploy" "$container=$invalid" >/dev/null ;;
  cleanup) [[ -e "$state" ]] || exit 0; original=$(cat "$state"); [[ "$original" == "$baseline" ]]; "${k[@]}" set image deploy/"$deploy" "$container=$original" >/dev/null; "${k[@]}" rollout status deploy/"$deploy" --timeout=180s >/dev/null; rm -f "$state" ;;
  recovery) [[ ! -e "$state" ]]; [[ "$(current)" == "$baseline" ]]; "${k[@]}" rollout status deploy/"$deploy" --timeout=1s >/dev/null ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
