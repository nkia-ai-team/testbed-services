#!/usr/bin/env python3
"""vmload.north_south: companion user load from the civil-petition load generator VM (§6-1).

A second k6 run of the domain's own script (/opt/loadgen/civil-petition/script.js, the same journeys
as the resident load) at the contract's rate for a bounded time, in a named transient unit as the k6
account, writing its summary under /var/lib/k6/. The load generator sends nothing to 119 (design §2-4),
so nothing of this reaches the recording except its effect on the servers.

No guard is armed (design §6-1: no node change): k6 ends by itself at its duration and the unit then
unloads; cleanup stops the unit earlier when it is called.

The executor is the means only; until a scenario adds an exact contract every scenario is refused.
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, profile_instance
import vm_executor_common as vm

PROFILE_ID = "vmload.north_south"
CONTRACTS: dict[str, dict[str, Any]] = {}
KEYS = {"host", "node", "unit", "target_rps", "duration_seconds", "seed"}
ENTRY_URL = "http://192.168.124.11/petition"
SCRIPT = "/opt/loadgen/civil-petition/script.js"


def check_parameters(p: dict[str, Any]) -> list[str]:
    vm.exact_keys(p, KEYS, PROFILE_ID)
    if vm.vm_named(p, ("loadgen",)) != "loadgen":
        raise ExecutorError("vmload.north_south runs on the load generator")
    return [vm.neutral_name(p["unit"], "unit"), str(vm.integer(p["target_rps"], "target_rps", 1, 120)),
            str(vm.integer(p["duration_seconds"], "duration_seconds", 60, 3600)),
            str(vm.integer(p["seed"], "seed", 1, 2**31 - 1))]


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    vm.contract_lookup(CONTRACTS, scenario_id, params, profile, "companion load")
    check_parameters(params)


# set -- <act> <unit> <rps> <duration> <seed>
NODE_PROGRAM = b"set -euo pipefail\n" + vm.UNIT_FUNCTIONS + br'''act="$1"; unit="$2"; rps="$3"; duration="$4"; seed="$5"
description="k6 load driver"
summary="/var/lib/k6/$unit-summary.json"
case "$act" in
  check) command -v k6 >/dev/null; [[ -r /opt/loadgen/civil-petition/script.js ]]
    if unit_active; then echo "$unit is already running" >&2; exit 1; fi ;;
  state) if unit_active; then echo ours; else echo default; fi ;;
  run) if unit_active; then echo "$unit is already running" >&2; exit 1; fi
    rm -f "$summary"
    unit_start --uid=k6 --gid=k6 --property=RuntimeMaxSec=$(( duration + 120 )) \
      /usr/bin/env ENTRY_URL=http://192.168.124.11/petition TARGET_RPS="$rps" DURATION="${duration}s" LOADGEN_SEED="$seed" \
      k6 run --quiet --summary-export "$summary" /opt/loadgen/civil-petition/script.js ;;
  cleanup) unit_stop ;;
  *) exit 2 ;;
esac
'''


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    validate(plan["scenario"]["id"], p, {})
    args = check_parameters(p)
    vm.check_vm_location(instance, p)
    from executor_common import kubectl_bash_argv, with_guard_values
    script = vm.runner_script(NODE_PROGRAM, vm.STANDARD_DISPATCH)
    return kubectl_bash_argv([action, p["host"], p["node"], *args]), with_guard_values(script, plan)


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
