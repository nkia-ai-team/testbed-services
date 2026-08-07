#!/usr/bin/env python3
"""Runtime application-control executor: flip a server-side flag, no restart.

F18-P(릴레이 정지)·F21-P(응답 지연)의 주입 표면. 세 가지를 배제하고 남은 설계다.

  · k8s.env 토글 — 값 변경이 롤아웃을 유발하고 testbed-transfer 는
    maxSurge=0(F17-R용, c30924a)이라 유일한 파드가 먼저 내려간다. 그 unready 를
    F18-P 자신의 감별자 transfer-pod-failure 가 실격시켰다(0804 #13).
  · HTTP 관리 엔드포인트 — otel javaagent 가 서버 스팬을 남겨 정지 시각과
    수단이 트레이스에 그대로 찍힌다(G6 자백, F03-H delayMs 계보).
  · 호출자 파라미터형 제어 — 접근 로그가 정답을 자백한다(같은 계보).

DB 행(outbox_relay_control·response_delay_control)이면 셋 다 없다: 앱은 어차피
2초마다 DB 를 폴링하므로 컨트롤 조회는 평시 노이즈와 구별되지 않고, UPDATE 는 DB
파드 안에서 sysdba 로 이뤄져 앱 트레이스·접근 로그 어디에도 남지 않는다.

제어 행은 두 종류다. 플래그형(F18-P)은 enabled 를 1<->0 으로 넘기고, 값형(F21
계열, 0804 #27·#28 병합 재설계)은 delay_ms 에 "얼마나 느리게"를 쓴다. 값형은
계약에 정수 delay_ms(0~10000, 앱·DDL 의 CHECK 와 같은 상한)를 싣는다 — 사다리가
단마다 다른 값을 물고 오므로 이 키만은 동일성이 아니라 범위로 검증한다(단 하나만
검증되던 실행기들이 사다리 단을 통째로 막았던 배치 #12 의 재발 방지).

값은 원격에 `env` 로 넘긴다 — 로컬 변수명이 원격 heredoc 에서 빈 문자열로
퍼지던 것이 F01-P 를 일주일 죽였다(4398722). 식별자는 절대 따옴표로 감싸지
않는다(따옴표 친 테이블명은 테이블이 아니라 문자열을 읽는다).
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance

PROFILE_ID = "app.control"

# 계약은 실행기와 registry(profiles.json scenario_parameters)에 같은 값으로
# 존재해야 한다 — test_db_lock_contracts_do_not_drift 와 같은 가드가
# test_app_control_contract_matches_the_approved_profile 로 이 표를 고정한다.
CONTRACTS: dict[str, dict[str, Any]] = {
    "F18-P": {
        "engine": "oracle",
        "namespace": "rca-testbed-banking",
        "db_pod": "testbed-oracle-0",
        "schema": "BANKING",
        "control_table": "outbox_relay_control",
        "service_id": "transfer",
    },
    # 사다리 실측 전의 잠정 단이다(readiness=parked). 판정 계약은 실측 후에 붙는다.
    "F21-P": {
        "engine": "oracle",
        "namespace": "rca-testbed-banking",
        "db_pod": "testbed-oracle-0",
        "schema": "BANKING",
        "control_table": "response_delay_control",
        "service_id": "transfer",
        "delay_ms": 1000,
    },
}

# 앱(ResponseDelayFilter)과 DDL 의 CHECK 가 같은 상한을 조인다. 셋이 어긋나면
# 주입은 성공했다고 보고하는데 앱은 다른 값을 쓴다.
MAX_DELAY_MS = 10000


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    contract = CONTRACTS.get(scenario_id)
    if contract is None:
        raise ExecutorError("scenario is not allowlisted for app control")
    if "delay_ms" in contract:
        delay = params.get("delay_ms")
        if isinstance(delay, bool) or not isinstance(delay, int) or not 0 <= delay <= MAX_DELAY_MS:
            raise ExecutorError(f"delay_ms must be an integer within 0..{MAX_DELAY_MS}")
        if _without_value(params) != _without_value(contract):
            raise ExecutorError("parameters do not exactly match the verified control contract")
    elif params != contract:
        raise ExecutorError("parameters do not exactly match the verified control contract")
    allowed = profile.get("parameter_contract", {}).get("allowed_scenarios")
    if allowed is not None and scenario_id not in allowed:
        raise ExecutorError("scenario is not enabled by the profile registry")


def _without_value(params: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in params.items() if k != "delay_ms"}


def control_values(params: dict[str, Any]) -> tuple[str, str, str]:
    """(제어 컬럼, 주입 상태 값, 평시 값). 평시 값이 preflight/recovery 의 기대치다."""
    if "delay_ms" in params:
        return "delay_ms", str(int(params["delay_ms"])), "0"
    return "enabled", "0", "1"


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    validate(plan["scenario"]["id"], p, {})
    column, injected, clean = control_values(p)
    return kubectl_bash_argv([
        action, plan["scenario"]["id"], p["namespace"], p["db_pod"],
        p["schema"], p["control_table"], p["service_id"], column, injected, clean,
    ]), SCRIPT


SCRIPT = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; scenario="$2"; ns="$3"; pod="$4"; schema="$5"; table="$6"; sid="$7"
col="$8"; injected="$9"; clean="${10}"
k=(kubectl --kubeconfig /root/tb-kubeconfig -n "$ns")
# `set feedback off` must precede everything else: sqlplus echoes command tags
# while feedback is on and tr folds them into the value (F01-P, 2026-08-03).
sql() { printf 'set pages 0 feedback off heading off\nalter session set container=FREEPDB1;\nalter session set current_schema=%s;\n%s\nexit;\n' "$schema" "$1" | "${k[@]}" exec -i "$pod" -- sqlplus -s / as sysdba; }
value_is() { sql "select $col from $table where service_id='$sid';" | tr -d '[:space:]' | grep -qx "$1"; }
set_value() { sql "update $table set $col=$1, updated_at=systimestamp where service_id='$sid';
commit;" >/dev/null; }
case "$action" in
  # The control row is deployment-provisioned (db/init.sql). Its absence means
  # the app build on this testbed predates the switch -- refuse to inject,
  # because the app would keep running at full speed and success could never hold.
  preflight) value_is "$clean" ;;
  run) value_is "$clean"; set_value "$injected"; value_is "$injected" ;;
  # The row itself is the injection state: restore is one idempotent UPDATE,
  # so cleanup needs no runner-local state file to decide what to undo.
  cleanup) set_value "$clean"; value_is "$clean" ;;
  recovery) value_is "$clean" ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
