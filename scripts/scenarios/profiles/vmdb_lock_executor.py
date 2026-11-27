#!/usr/bin/env python3
"""vmdb.lock: one session on db01 holds a table lock for a bounded time (§6-1, the VM counterpart of
db.lock). Writers and readers that need a conflicting lock wait behind it; the application's pool
fills and its requests time out. Shared body and what reaches 119: vmdb_executor_common.

Self-recovery (U8): armed before the unit starts, with an undo that stops exactly this unit and ends
exactly the backends that carry the contract's application_name.

Mode drop-index (added for F81-R, the parameters above stay as they were): an operator's schema change through the
operator path (design 6-1 change cause, 6-2 Q35). The operator logs in to the database VM and drops one index of an
application table with `sudo -u postgres psql` (ALTER TABLE ... DROP CONSTRAINT when a constraint owns it); a table
lock is taken for that statement only. Preflight refuses unless the reads through that index, at their rate now, would
need more than the contract's percent of the server's cores once each becomes a full scan (vmdb_executor_common
DDL_BODY). The guard is armed first with the same definition read from the catalog; cleanup has the operator build it
again, and the executor builds whatever is left as root.

The executor is the means only; until a scenario adds an exact contract every scenario is refused.

F81-R (Chargebee 2018-03-02 rebuilt on the SMS testbed): the operator removes the unique constraint on
petition.receipt_no, whose index was the only path of the portal's receipt lookup (30 % of the journeys). Each lookup
then reads the whole table (about 2 million rows, about 0.8 s of one core); at the resident load of the busy hours
(21 rps and up, about 6.3 lookups a second) the four cores fill, the connection pools fill behind the scans and the
portal answers 503 after the pool's 3-second wait.
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, profile_instance
import vm_executor_common as vm
import vmdb_executor_common as db

PROFILE_ID = "vmdb.lock"
CONTRACTS: dict[str, dict[str, Any]] = {
    "F81-R": {
        "host": "192.168.133.31",
        "node": "dbsvr01",
        "mode": "drop-index",
        "table": "petition",
        "index": "petition_receipt_no_key",
        "load_pct": 120,
    },
}
KEYS = {"host", "node", "unit", "description", "app_name", "table", "lock_mode", "runtime_seconds"}
DROP_INDEX_KEYS = {"host", "node", "mode", "table", "index", "load_pct"}
MODES = ("drop-index",)


def check_drop_index(p: dict[str, Any]) -> list[str]:
    vm.exact_keys(p, DROP_INDEX_KEYS, PROFILE_ID)
    node = vm.vm_named(p, ("db",))
    if node != vm.testbed_of(p).role_vm("db"):
        raise ExecutorError("vmdb executors run on the testbed's database VM")
    if p["table"] not in db.TABLES:
        raise ExecutorError(f"table must be one of {db.TABLES}")
    index = db.index_name(p["index"])
    load = vm.integer(p["load_pct"], "load_pct", 100, 400)
    return [p["table"], index, str(load), str(db.PROBE_SECONDS)]


def check_parameters(p: dict[str, Any]) -> list[str]:
    if isinstance(p, dict) and "mode" in p:
        if p["mode"] not in MODES:
            raise ExecutorError(f"mode must be one of {MODES}")
        return check_drop_index(p)
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
    if p.get("mode") == "drop-index":
        return vm.vm_invocation(plan, action, p, db.DDL_BODY, args, vm.operator_dispatch())
    return vm.vm_invocation(plan, action, p, db.NODE_BODY, args)


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
