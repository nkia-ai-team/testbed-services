#!/usr/bin/env python3
"""Deployment scale executor (an operator command that removes a serving subsystem's capacity).

F51-R reconstructs the AWS S3 us-east-1 summary of 2017-02-28: an authorized operator
following an established playbook ran a command meant to remove a small number of
servers from one subsystem, one of its inputs was entered incorrectly, and the servers
removed supported two other subsystems on the request path (index, placement). With
their capacity gone those subsystems could not serve until they were brought back. Here
the command is `kubectl scale` with replicas 0 on a Deployment that serves the request
path: the Deployment controller scales its current ReplicaSet to 0, the pod terminates
and the Service is left without endpoints. Nothing about the Deployment's template
changes (image, probes, env, resources stay), nothing crashes, and nothing brings the
capacity back on its own because there is no autoscaler.

The scenario id never reaches kubectl: the remote script gets the namespace, the
Deployment and the replica counts, and its state file is keyed by namespace and
Deployment.
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance

PROFILE_ID = "k8s.scale"

# 2026-10-09. F51-R: banking account-service 를 replicas 1 → 0. 잔액 조회와 계좌 목록(nginx → account)과
# 이체(nginx → api → account → transfer)가 account 를 지나고, 거래 내역(api → transfer)과 commerce 정산(→ transfer)은
# 지나지 않는다. HPA 가 없어 cleanup 이 되돌리기 전까지 파드가 다시 생기지 않는다.
CONTRACTS = {
    "F51-R": {
        "namespace": "rca-testbed-banking",
        "deployment": "testbed-account",
        "from_replicas": 1,
        "replicas": 0,
    },
}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    contract = CONTRACTS.get(scenario_id)
    if contract is None:
        raise ExecutorError("scenario has no verified deployment scale contract")
    allowed = profile.get("parameter_contract", {}).get("allowed_scenarios")
    if allowed is not None and scenario_id not in allowed:
        raise ExecutorError("scenario is not enabled by the profile registry")
    if params != contract:
        raise ExecutorError("parameters must exactly match the approved deployment scale contract")
    if not 0 <= params["replicas"] < params["from_replicas"]:
        raise ExecutorError("scale contract must remove capacity")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    validate(plan["scenario"]["id"], p, {})
    location = instance["location"]
    if location.get("transport") != "kubectl" or location.get("namespace") != p["namespace"]:
        raise ExecutorError("deployment scale executor requires its canonical Kubernetes namespace")
    return kubectl_bash_argv([
        action, p["namespace"], p["deployment"], str(p["from_replicas"]), str(p["replicas"]),
    ]), SCRIPT


SCRIPT = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; ns="$2"; deploy="$3"; from="$4"; to="$5"
state_root="${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}"
state="$state_root/${ns}-${deploy}-scale"
k=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$ns")
replicas() { "${k[@]}" get deploy "$deploy" -o jsonpath='{.spec.replicas}'; }
# No autoscaler may own the Deployment, or it would put the capacity back by itself.
no_autoscaler() { [[ -z "$("${k[@]}" get hpa -o name)" ]]; }
# Status fields rather than `kubectl rollout status`, for the same reason as k8s.env
# (a stale ProgressDeadlineExceeded verdict survives a successful restore).
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
check() {
  command -v kubectl >/dev/null; command -v jq >/dev/null
  "${k[@]}" auth can-i patch deployments/scale | grep -qx yes
  no_autoscaler; [[ "$(replicas)" == "$from" ]]; healthy 1s
}
case "$action" in
  preflight) check; [[ ! -e "$state" ]] ;;
  # run records the replica count it found and does not wait for the pod to terminate:
  # waiting while the coordinator lock is held expires the runner lease.
  run)
    check; mkdir -p "$state_root"; replicas >"$state.tmp"; mv -T "$state.tmp" "$state"
    "${k[@]}" scale deploy "$deploy" --replicas="$to" >/dev/null ;;
  cleanup)
    [[ -e "$state" ]] || exit 0
    original=$(cat "$state"); [[ "$original" == "$from" ]]
    "${k[@]}" scale deploy "$deploy" --replicas="$original" >/dev/null
    healthy 180s
    rm -f "$state" ;;
  recovery) [[ ! -e "$state" ]]; [[ "$(replicas)" == "$from" ]]; healthy 1s ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
