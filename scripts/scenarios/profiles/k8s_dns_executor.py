#!/usr/bin/env python3
"""Pod DNS policy rollout executor (resolver configuration change on one Deployment).

F37-R reconstructs Intercom 2024-11-05: a resolver change rolled out to app hosts
resolved public names but not the private zone, so the workers could not resolve
their internal endpoints. Here the same change is one field of a Deployment's pod
template: dnsPolicy ClusterFirst (cluster DNS, which knows Service names) becomes
Default (the node's resolver, which answers public names only). Nothing else in
the pod spec moves, so the new ReplicaSet captured by KCM differs from the old one
in that single field.

The scenario id never reaches kubectl: the remote script gets the namespace, the
Deployment and the two policy values, and its state file is keyed by Deployment.
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance

PROFILE_ID = "k8s.dns"

# 2026-10-08. F37-R: banking api-service 의 파드 DNS 정책을 노드 resolver(Default)로 바꾸는 배포.
# api 는 DB 를 쓰지 않고 /actuator/health 가 이름 해석과 무관해 새 파드가 Ready 가 되고 옛 파드를 대체한다.
CONTRACTS = {
    "F37-R": {
        "namespace": "rca-testbed-banking",
        "deployment": "testbed-api",
        "baseline_dns_policy": "ClusterFirst",
        "fault_dns_policy": "Default",
    },
}
POLICIES = {"ClusterFirst", "Default"}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    contract = CONTRACTS.get(scenario_id)
    if contract is None:
        raise ExecutorError("scenario has no verified DNS policy contract")
    allowed = profile.get("parameter_contract", {}).get("allowed_scenarios")
    if allowed is not None and scenario_id not in allowed:
        raise ExecutorError("scenario is not enabled by the profile registry")
    if params != contract:
        raise ExecutorError("parameters must exactly match the approved DNS policy contract")
    if {params["baseline_dns_policy"], params["fault_dns_policy"]} - POLICIES:
        raise ExecutorError("DNS policy is outside the approved set")
    if params["baseline_dns_policy"] == params["fault_dns_policy"]:
        raise ExecutorError("fault DNS policy must differ from the baseline")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    validate(plan["scenario"]["id"], p, {})
    location = instance["location"]
    if location.get("transport") != "kubectl" or location.get("namespace") != p["namespace"]:
        raise ExecutorError("DNS policy executor requires its canonical Kubernetes namespace")
    return kubectl_bash_argv([
        action, p["namespace"], p["deployment"], p["baseline_dns_policy"], p["fault_dns_policy"],
    ]), SCRIPT


SCRIPT = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; ns="$2"; deploy="$3"; baseline="$4"; fault="$5"
state_root="${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}"
state="$state_root/${ns}-${deploy}-dns-policy"
k=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$ns")
policy() { "${k[@]}" get deploy "$deploy" -o jsonpath='{.spec.template.spec.dnsPolicy}'; }
# With a dnsConfig attached, restoring the policy alone would not restore the resolver path.
# The canonical Deployment has none; refuse to touch one that does.
custom_config() { [[ -n "$("${k[@]}" get deploy "$deploy" -o jsonpath='{.spec.template.spec.dnsConfig}')" ]]; }
patch() { jq -cn --arg p "$1" '[{op:"replace",path:"/spec/template/spec/dnsPolicy",value:$p}]' | "${k[@]}" patch deploy "$deploy" --type=json --patch-file=/dev/stdin >/dev/null; }
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
check() { command -v kubectl >/dev/null; command -v jq >/dev/null; "${k[@]}" auth can-i patch deployments | grep -qx yes; [[ "$(policy)" == "$baseline" ]]; ! custom_config; healthy 1s; }
case "$action" in
  preflight) check; [[ ! -e "$state" ]] ;;
  # Like k8s.env, run does not wait for the rollout it starts: waiting while the
  # coordinator lock is held expires the runner lease.
  run) check; mkdir -p "$state_root"; policy >"$state.tmp"; mv -T "$state.tmp" "$state"; patch "$fault" ;;
  cleanup) [[ -e "$state" ]] || exit 0; original=$(cat "$state"); [[ "$original" == "$baseline" ]]; patch "$original"; healthy 180s; rm -f "$state" ;;
  recovery) [[ ! -e "$state" ]]; [[ "$(policy)" == "$baseline" ]]; ! custom_config; healthy 1s ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
