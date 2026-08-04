#!/usr/bin/env python3
"""Exact-snapshot Kubernetes deployment environment/config executor."""
from __future__ import annotations

import json
from typing import Any

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance

PROFILE_ID = "k8s.env"
APPROVED_TARGETS = {
    "F03-P": ("rca-testbed-commerce", "testbed-payment", "payment-service"),
    "F09-H": ("rca-testbed-commerce", "testbed-order", "order-service"),
    "F08-P": ("rca-testbed-commerce", "testbed-order", "order-service"),
    "F18-P": ("rca-testbed-banking", "testbed-transfer", "transfer-service"),
    "F23-R": ("rca-testbed-commerce", "testbed-inventory", "inventory-service"),
    # 2026-07-29 승격 때 profiles.json 의 allowed_scenarios 에만 들어가고 이 표에는
    # 빠져 있었다. 정리도 같은 검증을 지나므로 런이 스스로 못 씻고 전역 DIRTY 가 된다.
    "F04-H": ("rca-testbed-commerce", "testbed-order", "order-service"),
    # 2026-08-04. F05-R의 limit 사다리만으로는 JVM이 OOMKill되지 않는다 — limit을
    # 내리면 힙 상한도 같이 내려가기 때문이다(배치 #2). 109 실측: payment의 anon은
    # 413MiB로 사다리 바닥 576Mi보다 163MiB 낮다. 힙을 pretouch로 못 박아야 anon이
    # 한도를 넘는다. k8s.resource와 함께 걸리는 companion 주입이다.
    "F05-R": ("rca-testbed-commerce", "testbed-payment", "payment-service"),
}
APPROVED_KEYS = {
    "F03-P": {"SPRING_DATASOURCE_HIKARI_MAXIMUM_POOL_SIZE"},
    "F09-H": {"JAVA_TOOL_OPTIONS"},
    "F08-P": {"SPRING_APPLICATION_JSON"},
    "F18-P": {"OUTBOX_RELAY_ENABLED"},
    "F23-R": {"SPRING_APPLICATION_JSON"},
    "F04-H": {"OUTBOX_RELAY_ENABLED"},
    "F05-R": {"JAVA_TOOL_OPTIONS"},
}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    required = {"namespace", "deployment", "container", "baseline", "fault"}
    if set(params) != required:
        raise ExecutorError("parameters do not match the approved environment schema")
    target = (params["namespace"], params["deployment"], params["container"])
    if APPROVED_TARGETS.get(scenario_id) != target:
        raise ExecutorError("scenario environment target is not allowlisted")
    allowed = profile.get("parameter_contract", {}).get("allowed_scenarios")
    if allowed is not None and scenario_id not in allowed:
        raise ExecutorError("scenario is not enabled by the profile registry")
    for name in ("baseline", "fault"):
        value = params[name]
        if not isinstance(value, list) or not all(isinstance(row, dict) and set(row) >= {"name"} for row in value):
            raise ExecutorError(f"{name} environment must be a Kubernetes env array")
    baseline = {row["name"]: row for row in params["baseline"]}
    fault = {row["name"]: row for row in params["fault"]}
    changed = {key for key in baseline | fault if baseline.get(key) != fault.get(key)}
    if not changed or not changed <= APPROVED_KEYS[scenario_id]:
        raise ExecutorError("environment change touches a non-allowlisted key")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    validate(plan["scenario"]["id"], p, {})
    return kubectl_bash_argv([
        action, plan["scenario"]["id"], p["namespace"], p["deployment"], p["container"],
        json.dumps(p["baseline"], sort_keys=True, separators=(",", ":")),
        json.dumps(p["fault"], sort_keys=True, separators=(",", ":")),
    ]), SCRIPT


SCRIPT = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; scenario_id="$2"; ns="$3"; deploy="$4"; container="$5"; baseline="$6"; fault="$7"
state_root="${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}"
state="$state_root/${scenario_id}-container-env.json"
k=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$ns")
current() { "${k[@]}" get deploy "$deploy" -o json | jq -Sc --arg c "$container" '.spec.template.spec.containers[] | select(.name==$c) | (.env // [])'; }
patch() { idx=$("${k[@]}" get deploy "$deploy" -o json | jq -r --arg c "$container" '.spec.template.spec.containers | to_entries[] | select(.value.name==$c) | .key'); jq -cn --arg idx "$idx" --argjson e "$1" '[{op:"replace",path:("/spec/template/spec/containers/"+$idx+"/env"),value:$e}]' | "${k[@]}" patch deploy "$deploy" --type=json --patch-file=/dev/stdin >/dev/null; }
# Not `kubectl rollout-status` -- ProgressDeadlineExceeded freezes onto the
# Deployment when an injection (e.g. F18-P's full stop under maxSurge=0)
# holds pods unready past progressDeadlineSeconds, and rollout status then
# re-reads that stale verdict after a successful restore (batch #17 class).
healthy() {
  local deadline=$((SECONDS + ${1%s}))
  while :; do
    if "${k[@]}" get deploy "$deploy" -o json | jq -e '
        .status.observedGeneration >= .metadata.generation
        and ((.status.updatedReplicas // 0) == .spec.replicas)
        and ((.status.availableReplicas // 0) == .spec.replicas)
        and ((.status.replicas // 0) == .spec.replicas)' >/dev/null; then return 0; fi
    (( SECONDS < deadline )) || return 1
    sleep 2
  done
}
check() { command -v kubectl >/dev/null; command -v jq >/dev/null; "${k[@]}" auth can-i patch deployments | grep -qx yes; [[ "$(current)" == "$baseline" ]]; healthy 1s; }
case "$action" in
  preflight) check; [[ ! -e "$state" ]] ;;
  # The patch starts a rollout; returning before it settles leaves the Deployment
  # mid-generation for whatever runs next. F05-R is the first scenario whose
  # primary profile (k8s.resource) touches the same Deployment this companion
  # does, and its preflight asserts a steady baseline: 0.9s after this returned,
  # it saw a rolling Deployment and refused, which cost the run and paused the
  # batch (2026-08-04). Cleanup already waited; apply did not. Best effort, since
  # an injection may legitimately leave pods unready -- a wait that times out
  # must not fail the apply. The controller, not this executor, judges results.
  run) check; mkdir -p "$state_root"; current >"$state.tmp"; mv -T "$state.tmp" "$state"; patch "$fault"; healthy 120s || true ;;
  cleanup) [[ -e "$state" ]] || exit 0; original=$(cat "$state"); [[ "$original" == "$baseline" ]]; patch "$original"; healthy 180s; rm -f "$state" ;;
  recovery) [[ ! -e "$state" ]]; [[ "$(current)" == "$baseline" ]]; healthy 1s ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
