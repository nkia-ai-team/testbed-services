#!/usr/bin/env python3
"""vmdb.lock: one session on db01 holds a table lock for a bounded time (§6-1, the VM counterpart of
db.lock). Writers and readers that need a conflicting lock wait behind it; the application's pool
fills and its requests time out. Shared body and what reaches 119: vmdb_executor_common.

Self-recovery (U8): armed before the unit starts, with an undo that stops exactly this unit and ends
exactly the backends that carry the contract's application_name.

The executor is the means only; until a scenario adds an exact contract every scenario is refused.
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, profile_instance
import vm_executor_common as vm
import vmdb_executor_common as db

PROFILE_ID = "vmdb.lock"
CONTRACTS: dict[str, dict[str, Any]] = {}
KEYS = {"host", "node", "unit", "description", "app_name", "table", "lock_mode", "runtime_seconds"}


def check_parameters(p: dict[str, Any]) -> list[str]:
    vm.exact_keys(p, KEYS, PROFILE_ID)
    head = db.common_args(p)
    if p["table"] not in db.TABLES:
        raise ExecutorError(f"table must be one of {db.TABLES}")
    if p["lock_mode"] not in db.LOCK_MODES:
        raise ExecutorError(f"lock_mode must be one of {db.LOCK_MODES}")
    runtime = vm.integer(p["runtime_seconds"], "runtime_seconds", 30, 1800)
    sql = f"LOCK TABLE {p['table']} IN {p['lock_mode']} MODE"
    return [*head, "lock", sql, "1", str(runtime)]


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    vm.contract_lookup(CONTRACTS, scenario_id, params, profile, "database lock")
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
