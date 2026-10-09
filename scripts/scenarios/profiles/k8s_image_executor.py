#!/usr/bin/env python3
"""Release image rollout executor (a Deployment rolled to an image the node does not hold).

F17-H reconstructs Harness 2025-10-28 (with Pipefy 2024-05-22): a routine deployment
needed a container image that was no longer in the registry the nodes pull from, so
the new pods never started and the endpoint behind them answered 502. Here the node's
own containerd store is that registry (every app image is imagePullPolicy Never and
loaded per node), and the change is one field of a Deployment's pod template: the
container image moves from the loaded tag to a release tag that was never loaded.
The Deployment's own strategy (maxSurge 0, maxUnavailable 1) removes the old pod
before the new one has to start; this executor refuses a Deployment without it,
because under the default strategy the old pod keeps serving (cut F05-G).

The scenario id never reaches kubectl: the remote script gets the namespace, the
Deployment, the container and the two image references, and its state file is keyed
by Deployment.
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance

PROFILE_ID = "k8s.image"

# 2026-10-09. F17-H: banking transfer-service 를 노드에 적재된 적 없는 릴리스 태그로 롤아웃한다.
# transfer 는 매니페스트 정본이 maxSurge 0 / maxUnavailable 1 이라 옛 파드가 먼저 내려가고,
# 새 파드는 ErrImageNeverPull 로 컨테이너를 띄우지 못한다.
CONTRACTS = {
    "F17-H": {
        "namespace": "rca-testbed-banking",
        "deployment": "testbed-transfer",
        "container": "transfer-service",
        "baseline_image": "core-banking-transfer:latest",
        "fault_image": "core-banking-transfer:2.1.0",
    },
}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    contract = CONTRACTS.get(scenario_id)
    if contract is None:
        raise ExecutorError("scenario has no verified release image contract")
    allowed = profile.get("parameter_contract", {}).get("allowed_scenarios")
    if allowed is not None and scenario_id not in allowed:
        raise ExecutorError("scenario is not enabled by the profile registry")
    if params != contract:
        raise ExecutorError("parameters must exactly match the approved release image contract")
    if params["baseline_image"] == params["fault_image"]:
        raise ExecutorError("fault image must differ from the baseline")
    if params["baseline_image"].split(":")[0] != params["fault_image"].split(":")[0]:
        raise ExecutorError("fault image must be another tag of the same repository")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    validate(plan["scenario"]["id"], p, {})
    location = instance["location"]
    if location.get("transport") != "kubectl" or location.get("namespace") != p["namespace"]:
        raise ExecutorError("release image executor requires its canonical Kubernetes namespace")
    return kubectl_bash_argv([
        action, p["namespace"], p["deployment"], p["container"], p["baseline_image"], p["fault_image"],
    ]), SCRIPT


SCRIPT = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; ns="$2"; deploy="$3"; container="$4"; baseline="$5"; fault="$6"
state_root="${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}"
state="$state_root/${ns}-${deploy}-image"
k=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$ns")
spec() { "${k[@]}" get deploy "$deploy" -o json; }
image() { spec | jq -r --arg c "$container" '.spec.template.spec.containers[] | select(.name==$c) | .image'; }
# The failure needs the image to be absent from the node and the old pod to go first.
# Check both on the live objects instead of trusting the contract.
never_pull() { [[ "$(spec | jq -r --arg c "$container" '.spec.template.spec.containers[] | select(.name==$c) | .imagePullPolicy')" == "Never" ]]; }
replace_first() { spec | jq -e '.spec.replicas == 1 and .spec.strategy.type == "RollingUpdate"
    and (.spec.strategy.rollingUpdate.maxSurge | tostring) == "0"' >/dev/null; }
node() { spec | jq -r '.spec.template.spec.nodeSelector["kubernetes.io/hostname"] // empty'; }
on_node() {
  local n; n=$(node); [[ -n "$n" ]]
  kubectl --kubeconfig=/root/tb-kubeconfig get node "$n" -o json \
    | jq -e --arg i "$1" --arg d "docker.io/library/$1" '[.status.images[] | (.names // [])[]] | any(. == $i or . == $d)' >/dev/null
}
# While the fault holds, no container uses the baseline image, and with no registry behind
# imagePullPolicy Never the node copy is the only one. kubelet image GC (tb-w2: high 85%,
# low 80%, minimum age 2m) would reclaim it once the image filesystem passes 85%, and cleanup
# could not bring transfer back. Start only below the GC low mark, which leaves about 2 GB of
# writes before GC can begin (tb-w2 was 79.7% on 2026-10-09).
disk_ok() {
  local n; n=$(node); [[ -n "$n" ]]
  kubectl --kubeconfig=/root/tb-kubeconfig get --raw "/api/v1/nodes/$n/proxy/stats/summary" \
    | jq -e '.node.runtime.imageFs | (1 - .availableBytes / .capacityBytes) < 0.80' >/dev/null
}
set_image() { "${k[@]}" set image deploy "$deploy" "$container=$1" >/dev/null; }
# Status fields rather than `kubectl rollout status`, for the same reason as k8s.env
# (a stale ProgressDeadlineExceeded verdict survives a successful restore).
healthy() {
  local deadline=$((SECONDS + ${1%s}))
  while :; do
    if spec | jq -e '
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
  "${k[@]}" auth can-i patch deployments | grep -qx yes
  [[ "$(image)" == "$baseline" ]]; never_pull; replace_first
  # `! on_node` would not stop the script under set -e (a negated status never does).
  on_node "$baseline"; if on_node "$fault"; then return 1; fi
  disk_ok
  healthy 1s
}
case "$action" in
  preflight) check; [[ ! -e "$state" ]] ;;
  # Like k8s.env, run does not wait for the rollout it starts: waiting while the
  # coordinator lock is held expires the runner lease. This rollout never completes.
  run) check; mkdir -p "$state_root"; image >"$state.tmp"; mv -T "$state.tmp" "$state"; set_image "$fault" ;;
  cleanup) [[ -e "$state" ]] || exit 0; original=$(cat "$state"); [[ "$original" == "$baseline" ]]
    # If image GC removed the baseline anyway, rolling back cannot start transfer. Say how to
    # reload it (109 docker holds the same image) instead of timing out silently.
    if ! on_node "$original"; then
      echo "baseline image $original is gone from node $(node): reload it on 109 with" \
        "docker save $original | ssh -i /root/.ssh/tb_key nkia@<node ip> 'sudo ctr -n k8s.io images import -'" >&2
      exit 1
    fi
    set_image "$original"; healthy 180s; rm -f "$state" ;;
  recovery) [[ ! -e "$state" ]]; [[ "$(image)" == "$baseline" ]]; healthy 1s ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
