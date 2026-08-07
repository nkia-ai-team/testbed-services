#!/usr/bin/env python3
"""Exact-snapshot Kubernetes container resource fault executor."""
from __future__ import annotations

import json
from typing import Any

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance

PROFILE_ID = "k8s.resource"

# Only targets named by the scenario catalog/execution matrix are admitted.
APPROVED_TARGETS = {
    "F05-R": ("rca-testbed-commerce", "testbed-payment", "payment-service", "memory"),
    "F09-P": ("rca-testbed-commerce", "testbed-inventory", "inventory-service", "cpu"),
    "F25-H": ("rca-testbed-commerce", "testbed-postgres", "postgres", "memory"),
}
APPROVED_KINDS = {
    "F05-R": "deploy",
    "F09-P": "deploy",
    "F25-H": "statefulset",
}
F05_R_BASELINE = {
    "limits": {"cpu": "500m", "memory": "1Gi"},
    "requests": {"cpu": "200m", "memory": "512Mi"},
}
# 2026-08-07(run 95b07798): 768Mi 단을 뺀다. fault companion이 힙을 -Xms384m -Xmx384m
# -XX:+AlwaysPreTouch로 고정·선점하므로 컨테이너 바닥은 384MiB(선점된 힙) + 비힙이고,
# 평시 cgroup 실측 415MiB(최대 힙 256MiB)에서 역산한 비힙이 ~160MiB라 바닥은 ~544MiB다.
# 768Mi는 그 위로 220MiB, 640Mi는 100MiB가 남아 OOM이 구조적으로 불가능하다 — 실제로
# 두 단 모두 restart_count 0으로 승급만 하고 6.5분을 태웠다. 640Mi는 "OOM이 안 나는 단"
# 대조군으로 남기고, 아래로는 더 못 내린다: 576Mi 밑은 바닥 아래라 부하 중 OOM이 아니라
# 기동 중 OOM이 되어 트래픽을 한 건도 못 받고 crashloop한다(피해가 커지는 게 아니라 사라진다).
F05_R_LEVELS = tuple(
    {
        "namespace": "rca-testbed-commerce",
        "deployment": "testbed-payment",
        "container": "payment-service",
        "resource": "memory",
        "baseline": F05_R_BASELINE,
        "fault": {
            "limits": {"cpu": "500m", "memory": limit},
            "requests": {"cpu": "200m", "memory": "512Mi"},
        },
    }
    for limit in ("640Mi", "576Mi")
)
F25_H_BASELINE = {
    "limits": {"cpu": "500m", "memory": "512Mi"},
    "requests": {"cpu": "200m", "memory": "256Mi"},
}
# 2026-08-06 사다리 전환(0804 #19): 고정 320Mi는 실사용 124MiB의 2.6배라 OOM이
# 불가능했다. requests(256Mi)가 limit보다 크면 쿠버네티스가 패치를 거부하므로
# (765e99f에서 k8s.patch가 먼저 겪은 문제) 단마다 requests를 limit에 맞춰 내린다.
#
# 2026-08-07(run af660719): 첫 단 256Mi는 min_hold+timeout 내내 pg_restart 0으로
# 무효과였다 — 실사용의 2배라 여전히 넉넉했다. 무효과 단은 사다리 시간만 먹고 레벨
# 전환 recovery 위험만 늘리므로 제거하고 192Mi에서 시작한다. 바닥은 96Mi로 한 단
# 더 내린다. 이 아래는 postgres가 OOM이 아니라 기동 자체를 실패할 수 있는데, 그때는
# 성공 조건(termination_reason == OOMKilled)이 서지 않아 조용히 통과하는 대신
# 정직하게 실패한다.
F25_H_LEVELS = tuple(
    {
        "namespace": "rca-testbed-commerce",
        "deployment": "testbed-postgres",
        "container": "postgres",
        "resource": "memory",
        "baseline": F25_H_BASELINE,
        "fault": {
            "limits": {"cpu": "500m", "memory": limit},
            "requests": {"cpu": "200m", "memory": limit},
        },
    }
    for limit in ("192Mi", "128Mi", "96Mi")
)

_MEM_UNITS = {"Ki": 1, "Mi": 2, "Gi": 3}


def _mem_bytes(value: str) -> int:
    for suffix, power in _MEM_UNITS.items():
        if value.endswith(suffix):
            return int(value[: -len(suffix)]) * 1024 ** power
    return int(value)


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    required = {"namespace", "deployment", "container", "resource", "baseline", "fault"}
    if set(params) != required:
        raise ExecutorError("parameters do not match the approved resource schema")
    target = (params["namespace"], params["deployment"], params["container"], params["resource"])
    if APPROVED_TARGETS.get(scenario_id) != target:
        raise ExecutorError("scenario resource target is not allowlisted")
    allowed = profile.get("parameter_contract", {}).get("allowed_scenarios")
    if allowed is not None and scenario_id not in allowed:
        raise ExecutorError("scenario is not enabled by the profile registry")
    if not isinstance(params["baseline"], dict) or not isinstance(params["fault"], dict):
        raise ExecutorError("baseline and fault resources must be JSON objects")
    key = params["resource"]
    for name in ("baseline", "fault"):
        value = params[name]
        if set(value) - {"requests", "limits"}:
            raise ExecutorError(f"{name} contains unsupported resource fields")
        if key not in value.get("limits", {}):
            raise ExecutorError(f"{name} must declare the selected resource limit")
    if params["baseline"] == params["fault"]:
        raise ExecutorError("fault resources must differ from baseline")
    # Kubernetes rejects any pod spec whose request exceeds its limit, so a fault
    # that lowers only the limit dies at apply time and the run goes global-DIRTY
    # instead of failing here (k8s.patch hit this first, 765e99f).
    if key == "memory":
        fault_limit = _mem_bytes(params["fault"]["limits"][key])
        fault_request = params["fault"].get("requests", {}).get(key)
        if fault_request is not None and _mem_bytes(fault_request) > fault_limit:
            raise ExecutorError("fault memory request exceeds the fault limit")
    if scenario_id == "F05-R" and params not in F05_R_LEVELS:
        raise ExecutorError("parameters do not match the measured F05-R memory ladder")
    if scenario_id == "F25-H" and params not in F25_H_LEVELS:
        raise ExecutorError("parameters do not match the measured F25-H memory ladder")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    validate(plan["scenario"]["id"], p, {})
    kind = APPROVED_KINDS[plan["scenario"]["id"]]
    return kubectl_bash_argv([
        action, plan["scenario"]["id"], kind, p["namespace"], p["deployment"], p["container"],
        json.dumps(p["baseline"], sort_keys=True, separators=(",", ":")),
        json.dumps(p["fault"], sort_keys=True, separators=(",", ":")),
    ]), SCRIPT


SCRIPT = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; scenario_id="$2"; kind="$3"; ns="$4"; deploy="$5"; container="$6"; baseline="$7"; fault="$8"
state_root="${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}"
state="$state_root/${scenario_id}-container-resources.json"
k=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$ns")
current() { "${k[@]}" get "$kind" "$deploy" -o json | jq -Sc --arg c "$container" '.spec.template.spec.containers[] | select(.name==$c) | (.resources // {})'; }
patch() { jq -cn --arg c "$container" --argjson r "$1" '{spec:{template:{spec:{containers:[{name:$c,resources:$r}]}}}}' | "${k[@]}" patch "$kind" "$deploy" --type=strategic --patch-file=/dev/stdin >/dev/null; }
# Not `kubectl rollout-status` -- ProgressDeadlineExceeded freezes onto a Deployment
# whose pods an injection held unready past progressDeadlineSeconds, and
# the rollout verifier then re-reads that stale verdict after a successful restore
# (batch #17 class). Compute completion from live status; readyReplicas is
# the statefulset spelling of availableReplicas.
healthy() {
  local deadline=$((SECONDS + ${1%s}))
  while :; do
    if "${k[@]}" get "$kind" "$deploy" -o json | jq -e '
        .status.observedGeneration >= .metadata.generation
        and ((.status.updatedReplicas // 0) == .spec.replicas)
        and ((.status.availableReplicas // .status.readyReplicas // 0) == .spec.replicas)
        and ((.status.replicas // 0) == .spec.replicas)' >/dev/null; then return 0; fi
    (( SECONDS < deadline )) || return 1
    sleep 2
  done
}
# `${kind}s` built "deploys" from the "deploy" kind. That is not a resource
# type, so kubectl warned on stderr while still answering yes. Harmless to the
# check itself, but profile-control reports a failed preflight's stderr as the
# reason, so every real failure here arrived labelled with a warning about the
# wrong thing (2026-08-04, F05-R). auth can-i takes the spelling get/patch use.
check() { command -v kubectl >/dev/null; command -v jq >/dev/null; "${k[@]}" auth can-i patch "$kind" | grep -qx yes; [[ "$(current)" == "$baseline" ]]; healthy "${settle:-1s}"; }
case "$action" in
  # A companion may have just patched this same Deployment: F05-R's k8s.env adds
  # the heap pretouch to testbed-payment ~1s before this runs, and payment then
  # takes ~58s to roll (measured 2026-08-04). A 1s budget refused every time.
  # The wait lives here, not in the companion's apply, because profile-control
  # releases the coordinator lock around preflight and holds it through apply --
  # a long apply cannot be heartbeat-renewed and expires the 30s lease. `run`
  # keeps the 1s budget: preflight has already settled the Deployment by then.
  preflight) settle=90s check; [[ ! -e "$state" ]] ;;
  run)
    mkdir -p "$state_root"; umask 077
    if [[ -e "$state" ]]; then
      original=$(jq -Sc '.original' "$state"); applied=$(jq -Sc '.applied' "$state")
      [[ "$original" == "$baseline" ]]; [[ "$(current)" == "$applied" ]]
    else
      check; original=$(current)
    fi
    patch "$fault"
    jq -cn --argjson original "$original" --argjson applied "$fault" \
      '{original:$original,applied:$applied}' >"$state.tmp"
    mv -T "$state.tmp" "$state"
    ;;
  cleanup)
    [[ -e "$state" ]] || exit 0
    original=$(jq -Sc '.original' "$state"); [[ "$original" == "$baseline" ]]
    patch "$original"; healthy 180s; rm -f "$state"
    ;;
  recovery) [[ ! -e "$state" ]]; [[ "$(current)" == "$baseline" ]]; healthy 1s ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
