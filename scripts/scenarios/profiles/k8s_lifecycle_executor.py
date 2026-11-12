#!/usr/bin/env python3
"""Failing-image rollout executor that preserves the original Deployment image."""
from __future__ import annotations

from typing import Any

import json
from pathlib import Path

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance

PROFILE_ID = "k8s.lifecycle"
FAULT_IMAGES = Path(__file__).resolve().parents[1] / "fault-images"


def built_release_tags() -> set[str]:
    """Every tag the fault-image builder can put on a node (fault-images/*/image.json)."""
    return {json.loads(path.read_text(encoding="utf-8"))["fault"] for path in FAULT_IMAGES.glob("*/image.json")}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    if scenario_id not in profile["parameter_contract"]["allowed_scenarios"]:
        raise ExecutorError("scenario is not allowlisted")
    if params != profile["scenario_parameters"][scenario_id]:
        raise ExecutorError("parameters do not match the approved scenario profile")
    # The rollout must reference an image no node has, the way a release tag that never reached the
    # nodes does. The tag is an ordinary version (it is recorded in the pod spec and the
    # ErrImageNeverPull event, so it may not name the scenario, design principle 8); what keeps it
    # absent is that it is neither the baseline nor any tag the fault-image builder produces.
    invalid, baseline = params["invalid_image"], params["baseline_image"]
    if invalid.rpartition(":")[0] != baseline.rpartition(":")[0] or invalid == baseline:
        raise ExecutorError("invalid image must be another tag of the baseline repository")
    if invalid in built_release_tags():
        raise ExecutorError("invalid image is a tag the fault-image builder produces")


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
# How long cleanup waits for Ready after restoring. Measured on 66: app pods become Ready in
# 55-145s at idle. The startupProbe allows 5s x 60 = 300s (at 5s x 30 = 150s, loaded startups
# restarted and took about 5 minutes). 420s covers the full 300s with margin. Waits in run are
# left alone (they set the scenario strength and the judging time).
cleanup_wait=420s
state="$state_root/${scenario_id}-image"
k=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$ns")
current() { "${k[@]}" get deploy "$deploy" -o "jsonpath={.spec.template.spec.containers[?(@.name=='$container')].image}"; }
ready() { "${k[@]}" get deploy "$deploy" -o jsonpath='{.status.readyReplicas}'; }
check() { command -v kubectl >/dev/null; "${k[@]}" auth can-i patch deployments | grep -qx yes; [[ "$(current)" == "$baseline" ]]; [[ "$(ready)" =~ ^[1-9][0-9]*$ ]]; }
case "$action" in
  preflight) check; [[ ! -e "$state" ]] ;;
  run) check; mkdir -p "$state_root"; current >"$state.tmp"; mv -T "$state.tmp" "$state"; "${k[@]}" set image deploy/"$deploy" "$container=$invalid" >/dev/null ;;
  cleanup) [[ -e "$state" ]] || exit 0; original=$(cat "$state"); [[ "$original" == "$baseline" ]]; "${k[@]}" set image deploy/"$deploy" "$container=$original" >/dev/null; deadline=$((SECONDS + ${cleanup_wait%s})); until "${k[@]}" rollout status deploy/"$deploy" --timeout=30s >/dev/null; do (( SECONDS < deadline )) || exit 1; sleep 5; done; rm -f "$state" ;;
  recovery) [[ ! -e "$state" ]]; [[ "$(current)" == "$baseline" ]]; "${k[@]}" rollout status deploy/"$deploy" --timeout=1s >/dev/null ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
