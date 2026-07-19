#!/usr/bin/env python3
"""Fail-closed foundation for business-semantic fault scenarios."""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli

PROFILE_ID = "business.fault"

BLOCKED = {
    "F11-P": "pricing exposes no stale-cache version or fault-control API",
    "F13-P": "banking exposes no bounded large-history response or tagged seed contract",
    "F14-P": "ledger exposes no selective event-drop control or replay contract",
}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    del params, profile
    reason = BLOCKED.get(scenario_id, "scenario is not allowlisted")
    raise ExecutorError(f"business fault blocked: {reason}")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    del action
    scenario_id = plan.get("scenario", {}).get("id", "unknown")
    reason = BLOCKED.get(scenario_id, "scenario is not allowlisted")
    raise ExecutorError(f"business fault blocked: {reason}")


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
