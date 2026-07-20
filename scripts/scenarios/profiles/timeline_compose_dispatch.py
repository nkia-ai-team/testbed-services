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
    "F15-H": "food dispatch baseline is not healthy enough for a simultaneous fault",
    "F15-G": "Oracle and PostgreSQL lock rows and acquisition order are unresolved",
    "F15-T2": "food dispatch recovery is a prerequisite",
    "F15-T3": "worker placement and consumer stall SLA are unresolved",
    "F15-T4": "handoff close interval and consumer drain SLA are unresolved",
}

ROUTES = {
    "F08-H": "timeline_compose_executor",
    "F15-R": "timeline_flap_executor",
    "F15-T1": "timeline_dual_fault_executor",
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
