#!/usr/bin/env python3
"""Namespace ResourceQuota executor (a quota below what the namespace already uses).

F50-R reconstructs the mybinder.org incident report "pod limit reached" (2022): the
cluster's ResourceQuota object said the namespace was full although it was not
("exceeded quota: gke-resource-quotas, requested: pods=1, used: pods=15k, limited:
pods=15k"), so the API server refused every new pod the hub had to create, and pod
creation resumed the moment that quota object was deleted. Here the quota is a
namespace ResourceQuota whose hard requests.memory sits below what the running pods
already request. Nothing running is touched: admission only refuses new pods. The
next pod the namespace needs is the one a routine rollout of a Deployment with
maxSurge 0 / maxUnavailable 1 has to create after it has already removed the old
one, so that Deployment is left with no pod. This executor refuses a Deployment
without that strategy, because under the default strategy the surge pod is refused
while the old pod keeps serving.

The scenario id never reaches kubectl: the remote script gets the namespace, the
quota name, the resource, the hard value and the Deployment, and its state file is
keyed by namespace and quota name.
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance

PROFILE_ID = "k8s.quota"

# 2026-10-09. F50-R: banking 네임스페이스에 requests.memory 4Gi 할당량을 건다. 실행 중 파드의 요청 합은
# 4928Mi(api, account, transfer, ledger 각 512Mi, Kafka 768Mi, Oracle 2Gi, nginx 64Mi)라 새 파드는 모두 거절된다.
# transfer 는 정본 전략이 maxSurge 0 / maxUnavailable 1 이라 일상 재배포가 옛 파드를 먼저 내리고 새 파드를 만들지 못한다.
CONTRACTS = {
    "F50-R": {
        "namespace": "rca-testbed-banking",
        "quota": "compute-resources",
        "resource": "requests.memory",
        "hard": "4Gi",
        "deployment": "testbed-transfer",
    },
}
RESOURCES = {"requests.memory"}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    contract = CONTRACTS.get(scenario_id)
    if contract is None:
        raise ExecutorError("scenario has no verified resource quota contract")
    allowed = profile.get("parameter_contract", {}).get("allowed_scenarios")
    if allowed is not None and scenario_id not in allowed:
        raise ExecutorError("scenario is not enabled by the profile registry")
    if params != contract:
        raise ExecutorError("parameters must exactly match the approved resource quota contract")
    if params["resource"] not in RESOURCES:
        raise ExecutorError("quota resource is outside the approved set")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    validate(plan["scenario"]["id"], p, {})
    location = instance["location"]
    if location.get("transport") != "kubectl" or location.get("namespace") != p["namespace"]:
        raise ExecutorError("resource quota executor requires its canonical Kubernetes namespace")
    return kubectl_bash_argv([
        action, p["namespace"], p["quota"], p["resource"], p["hard"], p["deployment"],
    ]), SCRIPT


SCRIPT = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; ns="$2"; quota="$3"; resource="$4"; hard="$5"; deploy="$6"
state_root="${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}"
state="$state_root/${ns}-${quota}-quota"
k=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$ns")
# Memory quantities in bytes (binary and decimal suffixes, as Kubernetes writes them).
bytes_jq='def b: tostring | capture("^(?<n>[0-9.]+)(?<u>[A-Za-z]*)$") as $q
  | ($q.n | tonumber) * ({"":1,"k":1e3,"M":1e6,"G":1e9,"Ki":1024,"Mi":1048576,"Gi":1073741824}[$q.u]);'
# What the running pods already request: the figure the quota admission compares against.
used() { "${k[@]}" get pods -o json | jq "$bytes_jq"' [.items[]
    | select(.status.phase != "Succeeded" and .status.phase != "Failed")
    | ([.spec.containers[].resources.requests.memory // "0" | b] | add) as $c
    | ([.spec.initContainers[]?.resources.requests.memory // "0" | b] | max // 0) as $i
    | [$c, $i] | max] | add // 0'; }
limit() { jq -n "$bytes_jq"' "'"$hard"'" | b'; }
any_quota() { [[ -n "$("${k[@]}" get resourcequota -o name)" ]]; }
quota_counted() { "${k[@]}" get resourcequota "$quota" -o json | jq -e --arg r "$resource" '.status.used[$r] != null' >/dev/null; }
replace_first() { "${k[@]}" get deploy "$deploy" -o json | jq -e '.spec.replicas == 1 and .spec.strategy.type == "RollingUpdate"
    and (.spec.strategy.rollingUpdate.maxSurge | tostring) == "0"' >/dev/null; }
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
  "${k[@]}" auth can-i create resourcequotas | grep -qx yes
  "${k[@]}" auth can-i patch deployments | grep -qx yes
  ! any_quota; replace_first; healthy 1s
  # The quota must bite: the namespace already requests more than the hard value.
  jq -ne --argjson u "$(used)" --argjson h "$(limit)" '$u > $h' >/dev/null
}
case "$action" in
  preflight) check; [[ ! -e "$state" ]] ;;
  # The quota is applied first and the routine restart follows once the quota controller
  # has counted the namespace. run does not wait for the rollout itself: waiting while the
  # coordinator lock is held expires the runner lease.
  run)
    check; mkdir -p "$state_root"; date -u +%FT%TZ >"$state.tmp"; mv -T "$state.tmp" "$state"
    "${k[@]}" create quota "$quota" --hard="$resource=$hard" >/dev/null
    for _ in $(seq 30); do quota_counted && break; sleep 1; done
    quota_counted
    "${k[@]}" rollout restart deploy "$deploy" >/dev/null ;;
  # Deleting the quota lets pods be created again, but the ReplicaSet controller retries a
  # refused create with exponential backoff (minutes after a long refusal), so a redeploy
  # follows when the pod does not come back on its own.
  cleanup)
    [[ -e "$state" ]] || exit 0
    "${k[@]}" delete resourcequota "$quota" --ignore-not-found >/dev/null
    healthy 30s || { "${k[@]}" rollout restart deploy "$deploy" >/dev/null; healthy 180s; }
    rm -f "$state" ;;
  recovery) [[ ! -e "$state" ]]; ! any_quota; replace_first; healthy 1s ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
