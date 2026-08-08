#!/usr/bin/env python3
"""Trusted executor for the allowlisted commerce north-south k6 profile."""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from executor_common import ExecutorError, bind_level_parameters

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
COMPILER_SPEC = importlib.util.spec_from_file_location("scenario_compile_plan", ROOT / "compile-plan.py")
assert COMPILER_SPEC and COMPILER_SPEC.loader
compiler = importlib.util.module_from_spec(COMPILER_SPEC)
COMPILER_SPEC.loader.exec_module(compiler)


def validate_parameters(scenario_id: str, parameters: dict[str, Any], profile: dict[str, Any]) -> None:
    contract = profile["parameter_contract"]
    required = {
        "target_rps", "ramp_up", "hold", "ramp_down", "entry_url",
        "script_path", "scenario_tag", "seed", "baseline_unit",
    }
    if set(parameters) != required:
        raise ExecutorError(f"parameter keys must be exactly {sorted(required)}")
    if scenario_id not in contract["allowed_scenarios"]:
        raise ExecutorError(f"scenario is not allowlisted: {scenario_id}")
    levels = profile.get("scenario_levels", {}).get(scenario_id)
    if levels is not None and parameters not in [level["parameters"] for level in levels]:
        raise ExecutorError("parameters must exactly match a predeclared scenario level")
    target_rps = parameters["target_rps"]
    if isinstance(target_rps, bool) or not isinstance(target_rps, int):
        raise ExecutorError("target_rps must be an integer")
    bounds = contract["target_rps"]
    if not bounds["minimum"] <= target_rps <= bounds["maximum"]:
        raise ExecutorError("target_rps is outside the approved range")
    for name in ("ramp_up", "hold", "ramp_down"):
        if not re.fullmatch(contract["duration_pattern"], parameters[name]):
            raise ExecutorError(f"invalid duration: {name}")
    if parameters["entry_url"] not in contract["allowed_entry_urls"]:
        raise ExecutorError("entry_url is not allowlisted")
    if parameters["script_path"] not in contract["allowed_script_paths"]:
        raise ExecutorError("script_path is not allowlisted")
    if not re.fullmatch(contract["tag_pattern"], parameters["scenario_tag"]):
        raise ExecutorError("scenario_tag is not allowlisted")
    if parameters["scenario_tag"] != f"scenario_id={scenario_id}":
        raise ExecutorError("scenario_tag must exactly bind the scenario id")
    if parameters["baseline_unit"] not in contract["allowed_baseline_units"]:
        raise ExecutorError("baseline unit is not allowlisted")
    domain_profile = contract["domain_profiles"].get(parameters["entry_url"])
    if domain_profile is None or parameters["baseline_unit"] != domain_profile["baseline_unit"]:
        raise ExecutorError("baseline_unit does not match the entry_url domain profile")
    if isinstance(parameters["seed"], bool) or not isinstance(parameters["seed"], int):
        raise ExecutorError("seed must be an integer")


def confirmation_for(plan: dict[str, Any]) -> str:
    return f"LIVE:{plan['scenario']['id']}:{plan['plan_digest']}"


def load_instance(plan: dict[str, Any]) -> dict[str, Any]:
    matches = [row for row in plan["profile_instances"] if row["profile_id"] == "load.north_south"]
    if len(matches) != 1:
        raise ExecutorError("plan must contain exactly one load.north_south profile")
    return matches[0]


def build_ssh_argv(location: dict[str, Any]) -> list[str]:
    if location.get("transport") != "ssh" or location.get("host") != "192.168.122.206":
        raise ExecutorError("north-south executor requires canonical tb-runner SSH location")
    destination = f"{location.get('user', 'nkia')}@{location['host']}"
    return [
        "/usr/bin/ssh",
        "-i", "/root/.ssh/tb_key",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=10",
        destination,
        "bash", "-s", "--",
    ]


def remote_script() -> bytes:
    """원격 bash 스크립트를 조립한다.

    monitor 본문은 `loadgen_monitor.py`(정본)에서 읽어 주입한다. 예전에는 이 함수 안에
    heredoc으로 박혀 있었는데, baseline 경로가 같은 파서를 필요로 하면서 사본이 둘이 될
    참이었다 — 손으로 관리하는 두 번째 사본이 정본과 갈라져 시나리오의 유일한 성공 조건을
    죽인 게 2026-07-29 F06-P다. 읽어서 주입하면 ssh stdin 한 번으로 보내는 기존 구조를
    유지하면서도 정본이 하나로 남는다.
    """
    monitor_source = (HERE / "loadgen_monitor.py").read_bytes()
    if b"\nPY\n" in monitor_source or monitor_source.startswith(b"PY\n"):
        raise ExecutorError("monitor source collides with the heredoc terminator")
    return _REMOTE_PREFIX + monitor_source + _REMOTE_SUFFIX


_REMOTE_PREFIX = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; scenario_id="$2"; target_rps="$3"; ramp_up="$4"; hold="$5"; ramp_down="$6"
entry_url="$7"; script_path="$8"; scenario_tag="$9"; seed="${10}"; baseline_unit="${11}"
health_path="${12}"; gateway_env="${13}"; business_step="${14}"; read_step="${15:-}"
safe_id="${scenario_id//[^A-Za-z0-9_-]/_}"
summary="/tmp/rca-scenario-${safe_id}-summary.json"
samples="/tmp/rca-scenario-${safe_id}-samples.json"
live="/tmp/rca-scenario-${safe_id}-live.json"
monitor="/tmp/rca-scenario-${safe_id}-monitor.py"
monitor_pid="/tmp/rca-scenario-${safe_id}-monitor.pid"
log_file="/tmp/rca-scenario-${safe_id}.log"

tagged_pids() {
  local proc arg prev has_tag has_script is_k6 first
  for proc in /proc/[0-9]*; do
    [[ -r "$proc/cmdline" ]] || continue
    has_tag=false; has_script=false; is_k6=false; prev=""; first=true
    while IFS= read -r -d '' arg; do
      if [[ "$first" == true ]]; then [[ "${arg##*/}" == "k6" ]] && is_k6=true; first=false; fi
      [[ "$prev" == "--tag" && "$arg" == "$scenario_tag" ]] && has_tag=true
      [[ "$arg" == "$script_path" ]] && has_script=true
      prev="$arg"
    done < "$proc/cmdline" || true
    [[ "$is_k6" == true && "$has_tag" == true && "$has_script" == true ]] && printf '%s\n' "${proc##*/}"
  done
}

entry_health() {
  curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "$entry_url$health_path" 2>/dev/null || true
}

check_tools() {
  command -v k6 >/dev/null
  command -v curl >/dev/null
  systemctl is-active --quiet "$baseline_unit"
  [[ -r "$script_path" ]]
}

check_read_only() {
  check_tools
  [[ "$(entry_health)" == "200" ]]
}

case "$action" in
  preflight)
    check_read_only
    [[ -z "$(tagged_pids)" ]]
    ;;
  run)
    check_read_only
    [[ -z "$(tagged_pids)" ]] || { echo "tagged k6 already running" >&2; exit 4; }
    cat >"$monitor" <<'PY'
'''

_REMOTE_SUFFIX = br'''PY
    rm -f -- "$samples" "$live"
    nohup k6 run --tag "$scenario_tag" \
      --env "$gateway_env=$entry_url" --env "TARGET_RPS=$target_rps" \
      --env "RAMP_UP=$ramp_up" --env "HOLD=$hold" --env "RAMP_DOWN=$ramp_down" \
      --env "SURGE_SEED=$seed" --out "json=$samples" --summary-export "$summary" "$script_path" \
      >"$log_file" 2>&1 &
    nohup python3 "$monitor" --source "$samples" --output "$live" \
      --business-step "$business_step" --read-step "$read_step" \
      --mode tail --scenario-id "$scenario_id" \
      >>"$log_file" 2>&1 & echo $! >"$monitor_pid"
    ;;
  cleanup)
    mapfile -t pids < <(tagged_pids)
    ((${#pids[@]} == 0)) || kill -TERM "${pids[@]}" 2>/dev/null || true
    for _ in {1..20}; do [[ -z "$(tagged_pids)" ]] && break; sleep 0.25; done
    mapfile -t pids < <(tagged_pids)
    ((${#pids[@]} == 0)) || kill -KILL "${pids[@]}" 2>/dev/null || true
    if [[ -r "$monitor_pid" ]]; then kill -TERM "$(cat "$monitor_pid")" 2>/dev/null || true; fi
    rm -f -- "$summary" "$samples" "$live" "$monitor" "$monitor_pid" "$log_file"
    ;;
  recovery)
    # Recovery's whole job is to wait for the system to come back, so nothing here
    # may assert on a not-yet-recovered state. check_read_only() was doing exactly
    # that: its entry-health probe demanded 200 at that instant, and it ran before
    # everything else. Scenarios that deliberately break the entry (F25-H squeezes
    # commerce postgres until it OOMs) therefore failed recovery while the entry was
    # still coming back -- run 7bcd31eb died that way with
    # "load.north_south:recovery failed" even though its judgement had already
    # succeeded. Tool/unit checks stay immediate (they are environment invariants,
    # not recovery state); only the health probe waits.
    check_tools
    deadline=$((SECONDS + 120))
    while [[ "$(entry_health)" != "200" ]]; do
      if (( SECONDS >= deadline )); then
        echo "entry $entry_url$health_path not healthy after 120s (last=$(entry_health))" >&2
        exit 1
      fi
      sleep 2
    done
    # Do not assert immediately. k6 termination is asynchronous: even right after
    # cleanup SIGKILLs, the zombie stays in /proc until the parent reaps it, so
    # tagged_pids still sees it. F25-H run 4771bc5b died that way -- its
    # transition-cleanup ran 20:45:44->20:45:52 (8s) and recovery failed straight
    # after. Across every recorded run (n=88) this cleanup took p50 1.66s /
    # p90 4.42s / max 7.57s, and that max IS this run; the other 87 sat near p50
    # and slipped through, which is why the failure looked intermittent.
    #
    # 30s is ~4x the measured max. The cost is asymmetric, so take the
    # conservative side: overshooting costs a few seconds on a rare tail (k8s
    # level transitions already take 60-80s, so the transition profile is
    # unaffected), while undershooting costs a dirty run plus a babysitter action.
    deadline=$((SECONDS + 30))
    while [[ -n "$(tagged_pids)" ]]; do
      if (( SECONDS >= deadline )); then
        echo "tagged k6 still present after 30s: $(tagged_pids | tr '\n' ' ')" >&2
        exit 1
      fi
      sleep 1
    done
    ;;
  *) echo "unsupported remote action: $action" >&2; exit 2 ;;
esac
'''


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = load_instance(plan)
    parameters = instance["parameters"]
    profiles = json.loads((ROOT / "registry" / "profiles.json").read_text())
    contract = profiles["profiles"]["load.north_south"]["parameter_contract"]
    domain_profile = contract["domain_profiles"].get(parameters["entry_url"])
    if domain_profile is None:
        raise ExecutorError("entry_url has no domain profile")
    # ssh joins argv with spaces into one remote command line, so every remote
    # argument must be shell-quoted (db_ddl_executor does the same). The banking
    # health_path is `/api/accounts?status=ACTIVE&size=1`: unquoted, that `&` cut
    # the command in two and the rest ran as a new one, which is where
    # "GATEWAY_URL: command not found" came from. Every banking north-south
    # scenario — F10-P, F14-P, F18-P, F20-P, F21-P — died on contact. The `?` in
    # all three health paths was surviving only because pathname expansion found
    # no match; quoting removes that coin flip too.
    remote_args = [
        action,
        plan["scenario"]["id"],
        str(parameters["target_rps"]),
        parameters["ramp_up"],
        parameters["hold"],
        parameters["ramp_down"],
        parameters["entry_url"],
        parameters["script_path"],
        parameters["scenario_tag"],
        str(parameters["seed"]),
        parameters["baseline_unit"],
        domain_profile["health_path"],
        domain_profile["gateway_env"],
        domain_profile["business_step"],
        domain_profile.get("read_step", ""),
    ]
    argv = build_ssh_argv(instance["location"])
    argv.extend(shlex.quote(arg) for arg in remote_args)
    return argv, remote_script()


def execute(argv: Sequence[str], stdin: bytes) -> int:
    completed = subprocess.run(argv, input=stdin, timeout=900, check=False)
    return completed.returncode


def authorize_live(plan: dict[str, Any], plan_digest: str | None, confirmation: str | None) -> None:
    if not plan["live_allowed"]:
        raise ExecutorError("normalized plan does not allow live execution")
    if plan_digest != plan["plan_digest"]:
        raise ExecutorError("--plan-digest must exactly match the normalized plan")
    if confirmation != confirmation_for(plan):
        raise ExecutorError("--confirm must exactly match the normalized live confirmation")


def main() -> int:
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
        print(json.dumps({"schema_version": "1.0", "side_effects": False, "profile_id": "load.north_south", "action": args.action, "live_requested": False, "live_supported": True}, sort_keys=True))
        return 0

    plan = compiler.compile_plan(args.scenario)
    selected_plan, selected_level_id = bind_level_parameters(
        plan, "load.north_south", args.level_index, args.parameters_json
    )
    instance = load_instance(selected_plan)
    profiles = json.loads((ROOT / "registry" / "profiles.json").read_text())
    profile = profiles["profiles"]["load.north_south"]
    validate_parameters(plan["scenario"]["id"], instance["parameters"], profile)
    argv, stdin = build_invocation(selected_plan, "preflight" if args.action == "dry-run" else args.action)
    output = {
        "schema_version": "1.0",
        "side_effects": False,
        "profile_id": "load.north_south",
        "action": args.action,
        "scenario_id": plan["scenario"]["id"],
        "plan_digest": plan["plan_digest"],
        "confirmation": confirmation_for(plan),
        "parameters": instance["parameters"],
        "selected_level_id": selected_level_id,
        "invocation": {"argv": argv, "stdin_sha256": __import__("hashlib").sha256(stdin).hexdigest()},
        "live_allowed": plan["live_allowed"],
    }
    if not args.live:
        print(json.dumps(output, sort_keys=True))
        return 0
    authorize_live(plan, args.plan_digest, args.confirm)
    return execute(argv, stdin)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ExecutorError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        raise SystemExit(3)
