#!/usr/bin/env python3
"""Exact-image rollout/rollback foundation for application release scenarios."""
from __future__ import annotations

import re
from typing import Any

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance

PROFILE_ID = "app.release"

TARGETS = {
    "F08-R": ("rca-testbed-commerce", "testbed-pricing", "pricing-service", "commerce-pricing:latest"),
    "F14-H": ("rca-testbed-commerce", "testbed-pricing", "pricing-service", "commerce-pricing:latest"),
}

BLOCKED = {
    "F08-R": "pricing fault image digest is not present or verified on every eligible node",
    "F14-H": "pricing correctness fault image and contaminated-data cleanup are not available",
}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    if scenario_id not in TARGETS:
        raise ExecutorError("application release scenario is not allowlisted")
    approved = profile.get("scenario_parameters", {}).get(scenario_id)
    if approved is None:
        raise ExecutorError(f"application release blocked: {BLOCKED[scenario_id]}")
    if params != approved:
        raise ExecutorError("parameters must exactly match the approved release contract")
    required = {"namespace", "deployment", "container", "baseline_image", "fault_image"}
    if set(params) != required:
        raise ExecutorError("release parameters have an invalid shape")
    if tuple(params[key] for key in ("namespace", "deployment", "container", "baseline_image")) != TARGETS[scenario_id]:
        raise ExecutorError("release target differs from the observed canonical deployment")
    if not re.fullmatch(r"commerce-pricing@sha256:[0-9a-f]{64}", params["fault_image"]):
        raise ExecutorError("fault image must be pinned by an approved immutable digest")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    location = instance["location"]
    if location.get("transport") != "kubectl" or location.get("namespace") != p["namespace"]:
        raise ExecutorError("release executor requires its canonical Kubernetes namespace")
    return kubectl_bash_argv([
        action, plan["scenario"]["id"], p["namespace"], p["deployment"],
        p["container"], p["baseline_image"], p["fault_image"],
    ]), SCRIPT


SCRIPT = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; scenario_id="$2"; ns="$3"; deployment="$4"; container="$5"; baseline="$6"; fault="$7"
state_root="${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}"
state="$state_root/${scenario_id}-app-release-image"
k=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$ns")
current() { "${k[@]}" get deploy "$deployment" -o "jsonpath={.spec.template.spec.containers[?(@.name=='$container')].image}"; }
ready() { "${k[@]}" rollout status deploy/"$deployment" --timeout=180s >/dev/null; }
case "$action" in
  preflight)
    command -v kubectl >/dev/null
    "${k[@]}" auth can-i patch deployments | grep -qx yes
    [[ "$(current)" == "$baseline" && ! -e "$state" ]]
    ready
    ;;
  run)
    [[ ! -e "$state" && "$(current)" == "$baseline" ]]
    mkdir -p "$state_root"; current >"$state.tmp"; mv -T "$state.tmp" "$state"
    "${k[@]}" set image deploy/"$deployment" "$container=$fault" >/dev/null
    ready
    [[ "$(current)" == "$fault" ]]
    ;;
  cleanup)
    [[ -e "$state" ]] || exit 0
    original="$(cat "$state")"; [[ "$original" == "$baseline" ]]
    "${k[@]}" set image deploy/"$deployment" "$container=$original" >/dev/null
    ready; [[ "$(current)" == "$original" ]]; rm -f "$state"
    ;;
  recovery)
    [[ ! -e "$state" && "$(current)" == "$baseline" ]]; ready
    ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
