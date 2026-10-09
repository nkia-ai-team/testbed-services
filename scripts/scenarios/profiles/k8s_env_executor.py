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
    # 2026-10-08. F30-R: food payment 의 JSON 명명 규칙을 snake_case 로 바꾸는 설정 배포.
    # 호출자 order 는 camelCase 그대로라 결제 요청의 orderId 를 못 읽는다(L 데이터 형식 불일치).
    "F30-R": ("rca-testbed-food", "testbed-payment", "payment-service"),
    # 2026-10-08. F32-R: food dispatch 의 배차 동시 한도(DISPATCH_MAX_CAPACITY)를 실제 배차 수
    # 아래로 내리는 설정 배포. order 의 용량 확인이 available=0 을 받아 주문을 503 으로 거절한다.
    "F32-R": ("rca-testbed-food", "testbed-dispatch", "dispatch-service"),
    # 2026-10-08. F39-R: banking account 의 이체 하류 주소(TRANSFER_SERVICE_URL)를 다른 내부 호스트
    # (testbed-ledger:8082)로 덮어쓰는 설정 배포. 이름은 해석되지만 그 포트에 아무도 없어 연결이 시간 초과된다.
    "F39-R": ("rca-testbed-banking", "testbed-account", "account-service"),
    # 2026-10-09. F32-H: food dispatch 의 JSON 직렬화 설정(숫자를 문자열로 쓰기)을 켜는 설정 배포.
    # 용량 응답의 정수가 "500" 처럼 문자열로 나가고 order 의 Integer 캐스트가 실패해 주문이 503 이 된다.
    "F32-H": ("rca-testbed-food", "testbed-dispatch", "dispatch-service"),
}
APPROVED_KEYS = {
    "F03-P": {"SPRING_DATASOURCE_HIKARI_MAXIMUM_POOL_SIZE"},
    "F09-H": {"JAVA_TOOL_OPTIONS"},
    "F08-P": {"SPRING_APPLICATION_JSON"},
    "F18-P": {"OUTBOX_RELAY_ENABLED"},
    "F23-R": {"SPRING_APPLICATION_JSON"},
    "F04-H": {"OUTBOX_RELAY_ENABLED"},
    "F05-R": {"JAVA_TOOL_OPTIONS"},
    "F30-R": {"SPRING_APPLICATION_JSON"},
    "F32-R": {"DISPATCH_MAX_CAPACITY"},
    "F39-R": {"TRANSFER_SERVICE_URL"},
    "F32-H": {"SPRING_JACKSON_GENERATOR_WRITE_NUMBERS_AS_STRINGS"},
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
  # Deliberately does NOT wait for the rollout it starts. profile-control holds
  # the coordinator lock for the whole apply, so the runner heartbeat cannot
  # renew the 30s lease while this runs: a 58s wait here expired the lease and
  # every later call died on "runner lease is expired" (2026-08-04, measured).
  # Waiting for a peer's rollout belongs in preflight, which runs lock-free --
  # see the settle budget in k8s_resource_executor.
  run) check; mkdir -p "$state_root"; current >"$state.tmp"; mv -T "$state.tmp" "$state"; patch "$fault" ;;
  cleanup) [[ -e "$state" ]] || exit 0; original=$(cat "$state"); [[ "$original" == "$baseline" ]]; patch "$original"; healthy 180s; rm -f "$state" ;;
  recovery) [[ ! -e "$state" ]]; [[ "$(current)" == "$baseline" ]]; healthy 1s ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
