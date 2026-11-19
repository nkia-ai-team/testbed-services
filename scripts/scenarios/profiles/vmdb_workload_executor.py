#!/usr/bin/env python3
"""vmdb.workload: N sessions on db01 repeat one heavy read statement for a bounded time (§6-1, the VM
counterpart of db.workload). The statement comes from a fixed list (vmdb_executor_common.STATEMENTS),
never from the contract text. Shared body and what reaches 119: vmdb_executor_common.

Self-recovery (U8): armed before the unit starts, with an undo that stops exactly this unit and ends
exactly the backends that carry the contract's application_name.

The executor is the means only; until a scenario adds an exact contract every scenario is refused.
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, profile_instance
import vm_executor_common as vm
import vmdb_executor_common as db

PROFILE_ID = "vmdb.workload"
CONTRACTS: dict[str, dict[str, Any]] = {}
KEYS = {"host", "node", "unit", "description", "app_name", "statement", "sessions", "runtime_seconds"}


def check_parameters(p: dict[str, Any]) -> list[str]:
    vm.exact_keys(p, KEYS, PROFILE_ID)
    head = db.common_args(p)
    if p["statement"] not in db.STATEMENTS:
        raise ExecutorError(f"statement must be one of {sorted(db.STATEMENTS)}")
    sessions = vm.integer(p["sessions"], "sessions", 1, 16)
    runtime = vm.integer(p["runtime_seconds"], "runtime_seconds", 60, 1800)
    return [*head, "workload", db.STATEMENTS[p["statement"]], str(sessions), str(runtime)]


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    vm.contract_lookup(CONTRACTS, scenario_id, params, profile, "database workload")
    check_parameters(params)


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    validate(plan["scenario"]["id"], p, {})
    args = check_parameters(p)
    vm.check_vm_location(instance, p)
    return vm.vm_invocation(plan, action, p, db.NODE_BODY, args)


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
