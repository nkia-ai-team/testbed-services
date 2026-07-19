#!/usr/bin/env python3
"""Shared fail-closed boundary for live-capable scenario profile executors."""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
COMPILER_SPEC = importlib.util.spec_from_file_location("scenario_compile_plan", ROOT / "compile-plan.py")
assert COMPILER_SPEC and COMPILER_SPEC.loader
compiler = importlib.util.module_from_spec(COMPILER_SPEC)
COMPILER_SPEC.loader.exec_module(compiler)


class ExecutorError(ValueError):
    pass


def profile_instance(plan: dict[str, Any], profile_id: str) -> dict[str, Any]:
    matches = [row for row in plan["profile_instances"] if row["profile_id"] == profile_id]
    if len(matches) != 1:
        raise ExecutorError(f"plan must contain exactly one {profile_id} instance")
    return matches[0]


def confirmation_for(plan: dict[str, Any]) -> str:
    return f"LIVE:{plan['scenario']['id']}:{plan['plan_digest']}"


def authorize_live(plan: dict[str, Any], plan_digest: str | None, confirmation: str | None) -> None:
    if not plan["live_allowed"]:
        raise ExecutorError("normalized plan does not allow live execution")
    if plan_digest != plan["plan_digest"]:
        raise ExecutorError("--plan-digest must exactly match the normalized plan")
    if confirmation != confirmation_for(plan):
        raise ExecutorError("--confirm must exactly match the normalized live confirmation")


def bind_level_parameters(
    plan: dict[str, Any], profile_id: str, level_index: int | None, raw: str | None
) -> tuple[dict[str, Any], str | None]:
    """Return a copied plan whose profile parameters exactly bind an approved level."""
    if (level_index is None) != (raw is None):
        raise ExecutorError("--level-index and --parameters-json must be supplied together")
    bound = copy.deepcopy(plan)
    instance = profile_instance(bound, profile_id)
    levels = instance.get("approved_levels", instance.get("scenario_levels", []))
    if level_index is None:
        return bound, instance.get("selected_level_id")
    if not levels:
        raise ExecutorError("fixed profile rejects adaptive level overrides")
    if level_index < 0 or level_index >= len(levels):
        raise ExecutorError("level index is outside the predeclared adaptive ladder")
    try:
        supplied = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ExecutorError("--parameters-json is invalid") from exc
    canonical = json.dumps(supplied, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if raw != canonical:
        raise ExecutorError("--parameters-json must use canonical JSON encoding")
    level = levels[level_index]
    expected = level["parameters"]
    if supplied != expected:
        raise ExecutorError("parameters do not exactly match the predeclared level")
    instance["parameters"] = expected
    instance["selected_level_id"] = level["level_id"]
    return bound, level["level_id"]


def kubectl_bash_argv(arguments: Sequence[str]) -> list[str]:
    return ["/usr/bin/bash", "-s", "--", *arguments]


def execute(argv: Sequence[str], stdin: bytes) -> int:
    return subprocess.run(argv, input=stdin, timeout=900, check=False).returncode


Builder = Callable[[dict[str, Any], str], tuple[list[str], bytes]]
Validator = Callable[[str, dict[str, Any], dict[str, Any]], None]


def main_for(profile_id: str, builder: Builder, validator: Validator) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", nargs="?", default="dry-run", choices=["preflight", "run", "cleanup", "recovery", "dry-run"])
    parser.add_argument("--scenario")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--plan-digest")
    parser.add_argument("--confirm")
    parser.add_argument("--level-index", type=int)
    parser.add_argument("--parameters-json")
    args = parser.parse_args()
    if not args.scenario:
        if args.live:
            raise ExecutorError("--scenario is required for live execution")
        registry = json.loads((ROOT / "registry" / "profiles.json").read_text())
        supported = bool(registry["profiles"][profile_id]["live_supported"])
        print(json.dumps({"schema_version": "1.0", "side_effects": False, "profile_id": profile_id, "action": args.action, "live_requested": False, "live_supported": supported}, sort_keys=True))
        return 0

    plan = compiler.compile_plan(args.scenario)
    selected_plan, selected_level_id = bind_level_parameters(
        plan, profile_id, args.level_index, args.parameters_json
    )
    instance = profile_instance(selected_plan, profile_id)
    profiles = json.loads((ROOT / "registry" / "profiles.json").read_text())
    profile = profiles["profiles"][profile_id]
    validator(plan["scenario"]["id"], instance["parameters"], profile)
    remote_action = "preflight" if args.action == "dry-run" else args.action
    argv, stdin = builder(selected_plan, remote_action)
    output = {
        "schema_version": "1.0", "side_effects": False, "profile_id": profile_id,
        "action": args.action, "scenario_id": plan["scenario"]["id"],
        "plan_digest": plan["plan_digest"], "confirmation": confirmation_for(plan),
        "parameters": instance["parameters"],
        "selected_level_id": selected_level_id,
        "invocation": {"argv": argv, "stdin_sha256": hashlib.sha256(stdin).hexdigest()},
        "live_allowed": plan["live_allowed"],
    }
    if not args.live:
        print(json.dumps(output, sort_keys=True))
        return 0
    authorize_live(plan, args.plan_digest, args.confirm)
    return execute(argv, stdin)


def cli(profile_id: str, builder: Builder, validator: Validator) -> None:
    try:
        raise SystemExit(main_for(profile_id, builder, validator))
    except ExecutorError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        raise SystemExit(3)
