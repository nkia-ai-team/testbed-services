#!/usr/bin/env python3
"""scenario_id dispatch for the shared `timeline.compose` profile.

Three scenarios share one profile id but need different orchestration scripts:
F08-H (two-fault compose), F15-R (bounded mock flapping), F15-T1 (exact-
simultaneous dual root). compile-plan binds exactly one executor file per
profile, so this dispatcher is that file — it routes validate/build_invocation
to the scenario's dedicated module and fails closed for every other timeline.
The profile id stays `timeline.compose`; the known_profiles count is unchanged.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from executor_common import ExecutorError, cli

PROFILE_ID = "timeline.compose"
HERE = Path(__file__).resolve().parent

# Composite timelines whose contracts remain unresolved. Aligned with
# timeline_dual_fault_executor.BLOCKED_TIMELINES minus the routed scenarios.
BLOCKED_TIMELINES = {
    "F08-G": "Oracle lock row, credential, and inverse transaction are unresolved",
    "F14-R": "response-loss proxy and duplicate-row cleanup do not exist",
    "F15-G": "Oracle and PostgreSQL lock rows and acquisition order are unresolved",
    # 2026-07-29 re-audit (parked-reaudit-0729.md): both reasons below were
    # restated. F15-T3's placement was pinned on 07-28 (ledger runs on tb-w2) and
    # its stall SLA is a calibration value, not a missing contract — the real
    # blocker is that it is a single injection wearing a compose profile, so it
    # leaves this dispatcher entirely for host.stress. F15-T4 keeps a real gap,
    # but a narrower one than "handoff interval unresolved": F15-T2 proved the
    # offset schedule live, and what is missing is releasing arm 1 before arm 2
    # starts so the two windows do not overlap.
    "F15-T3": "single-injection timeline mis-assigned to compose; belongs to host.stress",
    "F15-T4": "sequential offset exists (F15-T2) but non-overlapping handoff release does not",
}

ROUTES = {
    "F08-H": "timeline_compose_executor",
    "F15-R": "timeline_flap_executor",
    "F15-T1": "timeline_dual_fault_executor",
    # 2026-07-29: F15-H/F15-T2 were blocked on "food dispatch baseline/recovery",
    # a leftover of the discarded dispatch-503 reading of their food arm. food has
    # no 429 of its own, but the external PG mock's 429 propagates through
    # PgApiClient.java:50 to the food entry point unchanged — the F06-P surface.
    # Both arms are therefore already-proven injections; only the composition and
    # its observation plane were missing.
    "F15-H": "timeline_lock_mock_executor",
    "F15-T2": "timeline_lock_mock_executor",
}


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _route(scenario_id: str):
    module_name = ROUTES.get(scenario_id)
    if module_name is None:
        raise ExecutorError(BLOCKED_TIMELINES.get(scenario_id, "timeline is not allowlisted"))
    return _load(module_name)


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    _route(scenario_id).validate(scenario_id, params, profile)


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    return _route(plan["scenario"]["id"]).build_invocation(plan, action)


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
