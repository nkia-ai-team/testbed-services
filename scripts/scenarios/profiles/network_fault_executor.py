#!/usr/bin/env python3
"""Fail-closed boundary for unresolved network fault locations.

No current network scenario has both a trusted injection transport and a verified
out-of-band recovery path.  Keeping the refusal executable prevents a generic
``tc`` or interface-down implementation from being accidentally promoted.
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli

PROFILE_ID = "network.fault"

BLOCKED = {
    "F07-R": "cross-domain delay injection point is unresolved",
    "F12-P": "CNI/NET_ADMIN network namespace is unresolved",
    "F12-R": "physical interface and out-of-band recovery are unresolved",
    "F12-G": "non-service NMS source interface is unresolved",
    "F13-H": "WPM probe source host and interface are unresolved",
}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    del params, profile
    reason = BLOCKED.get(scenario_id, "scenario is not allowlisted")
    raise ExecutorError(f"unsafe network fault blocked: {reason}")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    del action
    scenario_id = plan.get("scenario", {}).get("id", "unknown")
    reason = BLOCKED.get(scenario_id, "scenario is not allowlisted")
    raise ExecutorError(f"unsafe network fault blocked: {reason}")


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
