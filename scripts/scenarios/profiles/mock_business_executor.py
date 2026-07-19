#!/usr/bin/env python3
"""MockServer foundations for food partial-429 and commerce compensation cases."""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance
from mock_expectation_executor import SCRIPT as BASE_SCRIPT

PROFILE_ID = "mock.expectation"

CONTRACTS = {
    "F06-P": {
        "path": "/pay", "mode": "transient_status", "status_code": 429,
        "remaining_times": 5, "ttl_seconds": 4,
        "pulse_interval_seconds": 5, "max_pulses": 96,
    },
    "F14-G": {
        "path": "/v1/payments", "mode": "status", "status_code": 429,
        "delay_seconds": 0,
    },
}

BLOCKED = {
    "F07-G": (
        "verified downstreams do not provide a successful fallback: food returns "
        "BAD_GATEWAY and commerce has no circuit breaker"
    ),
}

TARGETS = {
    "F06-P": ("rca-testbed-food", "deployment/testbed-external-pg-mock"),
    "F14-G": ("rca-testbed-commerce", "deployment/testbed-external-pg-mock"),
}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    del profile
    if scenario_id in BLOCKED:
        raise ExecutorError(BLOCKED[scenario_id])
    expected = CONTRACTS.get(scenario_id)
    if expected is None or params != expected:
        raise ExecutorError("parameters must exactly match an approved mock business contract")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    scenario_id = plan["scenario"]["id"]
    location = instance["location"]
    namespace, resource = TARGETS.get(scenario_id, (None, None))
    if location.get("namespace") != namespace or location.get("resource") != resource:
        raise ExecutorError("mock business executor target differs from its canonical domain")
    argv = kubectl_bash_argv([
        action, scenario_id, namespace, resource, p["path"], p["mode"],
        str(p["status_code"]), str(p.get("delay_seconds", 0)),
        str(p.get("remaining_times", 0)), str(p.get("ttl_seconds", 0)),
        str(p.get("pulse_interval_seconds", 0)), str(p.get("max_pulses", 0)),
    ])
    return argv, SCRIPT


_CAPACITY_FUNCTION = br'''
food_capacity_ready() {
  local capacity_port=19306 capacity_pid="" payload
  [[ "$scenario_id" != F06-P ]] && return 0
  "${k[@]}" port-forward service/testbed-dispatch "$capacity_port:8082" >"/tmp/${scenario_id}-dispatch-capacity-pf.log" 2>&1 & capacity_pid=$!
  for _ in {1..20}; do
    payload="$(curl -fsS --max-time 1 "http://127.0.0.1:$capacity_port/api/dispatch/capacity" 2>/dev/null || true)"
    if [[ -n "$payload" ]]; then
      kill "$capacity_pid" 2>/dev/null || true; wait "$capacity_pid" 2>/dev/null || true
      [[ "$(jq -r '.available // 0' <<<"$payload")" -gt 0 ]]
      return
    fi
    sleep .25
  done
  kill "$capacity_pid" 2>/dev/null || true; wait "$capacity_pid" 2>/dev/null || true
  return 1
}
'''

SCRIPT = BASE_SCRIPT.replace(
    b'case "$action" in\n',
    _CAPACITY_FUNCTION + b'case "$action" in\n',
).replace(
    b'  preflight)\n    command -v kubectl',
    b'  preflight)\n    command -v jq >/dev/null; command -v curl >/dev/null; command -v kubectl >/dev/null\n    food_capacity_ready\n    command -v kubectl',
).replace(
    b'  run)\n    [[ ! -e "$state"',
    b'  run)\n    food_capacity_ready\n    [[ ! -e "$state"',
)


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
