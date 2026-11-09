#!/usr/bin/env python3
"""Shared fail-closed boundary for live-capable scenario profile executors."""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import re
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


def approved_parameter_sets(profile: dict[str, Any], scenario_id: str) -> list[dict[str, Any]]:
    """Every parameter set the registry approves: the fixed default plus each ladder rung.

    사다리 시나리오의 검증이 `scenario_parameters` 한 벌만 보면, 기본값과 다른 단은
    전부 거부된다 — 승인 레지스트리에는 멀쩡히 등재돼 있는데도 그렇다. F15-T1이
    768Mi 단에서 그렇게 막혔고(배치 #12), F15-R의 `episode-fault-240`도 같은 상태였다.
    `bind_level_parameters`가 이미 단을 정확히 묶어 주므로, 검증은 승인된 집합 어디에
    들어 있는지만 물으면 된다.
    """
    approved: list[dict[str, Any]] = []
    default = profile.get("scenario_parameters", {}).get(scenario_id)
    if default is not None:
        approved.append(default)
    for level in profile.get("scenario_levels", {}).get(scenario_id, []):
        if level["parameters"] not in approved:
            approved.append(level["parameters"])
    return approved


def kubectl_bash_argv(arguments: Sequence[str]) -> list[str]:
    return ["/usr/bin/bash", "-s", "--", *arguments]


# Remote bash bodies check their preconditions with bare `[[ ... ]]` under
# `set -e`. A check that fails exits 1 having said nothing, and profile-control
# could only report "no detail": 2026-10-22 F35-P lost a cycle to a full Oracle
# tablespace that way, found only by hand. This line, placed right after the
# shebang, makes the first failing command say which line and which command it
# was. `set -E` carries the trap into functions, where most checks live; the
# trap prints and does nothing else, so exit codes and branches stay as they
# were. One thing does change: inside a command substitution a non-fatal
# failure also prints, so a value captured with `$(... 2>&1)` can carry the
# trace line. No body captures stderr that way outside an && or || list,
# where the trap does not fire. LINENO is shifted back by this line so it
# matches the source.
# BASH_COMMAND is the unexpanded source text, so no variable value is printed.
FAILURE_TRACE = (
    b"set -E; trap 'printf \"%s\\n\" \"failed at line $((LINENO-1)): "
    b"${BASH_COMMAND:0:300}\" >&2' ERR\n"
)
_BASH_SHEBANG = re.compile(rb"^#!\s*(/usr/bin/env\s+bash|/(usr/)?bin/bash)\b")
_ERREXIT = re.compile(rb"(?m)^\s*set\s+-[A-Za-z]*e")
_OWN_ERR_TRAP = re.compile(rb"\btrap\b[^\n]*\bERR\b")


def with_failure_trace(stdin: bytes) -> bytes:
    """Add FAILURE_TRACE to a `set -e` bash body sent on stdin.

    Left alone: anything that is not a bash script, a script without errexit
    (a failing command there does not end it, so a trace would only be noise),
    and a script that sets its own ERR trap (`set -E` would change where that
    trap fires)."""
    first, newline, rest = stdin.partition(b"\n")
    if not newline or not _BASH_SHEBANG.match(first):
        return stdin
    if not _ERREXIT.search(rest) or _OWN_ERR_TRAP.search(rest):
        return stdin
    return first + newline + FAILURE_TRACE + rest


# Node self-recovery (deadline guard).
#
# 2026-10-17 F80-R-run-bad63a76: the job pushed tb-w2 into NotReady, cleanup could not reach the
# node over ssh, and the runner stopped DIRTY with the job's output still on the node. Any change an
# executor makes on a node outlives a runner that cannot reach it. So every executor that changes a
# node arms, before the change, a timer on that node that undoes the change by itself:
#
# - The timer lives in PID 1 (a transient systemd timer), not in a process the injection can starve
#   or the OOM killer can pick. It unloads once it has fired (RemainAfterElapse=no), so the next arm
#   can take the same name; an undo that failed stays visible as a failed <job>-expire.service.
# - It fires only after the runner itself would have ended the injection (max_injection_duration,
#   counted from t1, is never later than the arm) plus a margin for cleanup to arrive. So it never
#   cuts an injection the runner still holds.
# - cleanup postpones it as soon as it reaches the node (for as long as one executor call may last)
#   and cancels it once it has undone the change itself, so it fires only when cleanup never came or
#   failed half way.
# - The undo is idempotent: it checks before it changes, and a second run finds nothing to do.
# - A transient timer does not survive a reboot. A reboot ends processes and iptables rules by itself;
#   files survive it, so every file the change leaves is listed in a tmpfiles.d rule that removes it at
#   the next boot (systemd-tmpfiles-setup runs --remove --boot), and the rule removes itself with them.
# - Nothing the node logs names the change: see the note above GUARD_FUNCTIONS.
GUARD_MARGIN_SECONDS = 600
# cleanup's postponement: one executor call is bounded at 900s (execute below).
GUARD_CLEANUP_HOLD_SECONDS = 960
# The runner's default when a plan states no max_injection_duration (rca-scenario-runner controller).
DEFAULT_MAX_INJECTION_SECONDS = 1200
_DURATION = re.compile(r"^(\d+)(s|m|h)?$")


def duration_seconds(value: Any) -> int:
    """Seconds from an integer or a compact ``10s|2m|1h`` duration (the runner's own syntax)."""
    if isinstance(value, bool):
        raise ExecutorError("duration must be seconds or a compact duration")
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str):
        match = _DURATION.fullmatch(value.strip())
        if match:
            return int(match.group(1)) * {None: 1, "s": 1, "m": 60, "h": 3600}[match.group(2)]
    raise ExecutorError("duration must be seconds or a compact duration")


def guard_expiry_seconds(plan: dict[str, Any]) -> int:
    """When an armed node undoes the change by itself, counted from the arm."""
    runtime = plan.get("controller", {}).get("runtime", {})
    limit = duration_seconds(runtime.get("max_injection_duration", DEFAULT_MAX_INJECTION_SECONDS))
    if limit <= 0:
        raise ExecutorError("max_injection_duration must be positive")
    return limit + GUARD_MARGIN_SECONDS


def guard_stdin_line(plan: dict[str, Any]) -> bytes:
    """The deadline and the cleanup hold ride on stdin, never on a node's sudo command line."""
    return f"expire_after={guard_expiry_seconds(plan)} guard_hold={GUARD_CLEANUP_HOLD_SECONDS}\n".encode()


# Shell functions that run as root on the node. host.stress includes them in its own root script;
# executors that drive the node one command at a time send GUARD_PROGRAM through node_guard (below).
# guard_arm <job> <seconds> <undo body> [<file removed at boot>...]
# guard_postpone <job> <seconds>   (only while an undo is armed and fewer seconds are left)
# guard_disarm <job>
#
# What reaches the node's journal (forwarded to 119) is a tool trace no real incident leaves, so it
# says nothing about the change (design principle 8): the units take the name systemd-run gives an
# unnamed command (run-r<32 hex>, fresh each time) and an empty description, which systemd shows as
# the unit name. A normal run adds one line at arm (Started run-r....timer.) and the stop lines at
# disarm; postpone restarts the timer only when less than the hold is left, so a cleanup on time adds
# nothing. The job name stays in files under the state root (0700) and is never logged; the boot rule
# is named after the first unit.
GUARD_FUNCTIONS = br'''guard_root="${OPSJOB_STATE_ROOT:-/var/lib/opsjobs}"
guard_boot_dir="${OPSJOB_TMPFILES_DIR:-/etc/tmpfiles.d}"
guard_ctl="${OPSJOB_SYSTEMCTL:-systemctl}"
guard_run="${OPSJOB_SYSTEMD_RUN:-systemd-run}"
guard_uptime="${OPSJOB_UPTIME_FILE:-/proc/uptime}"
# <job>.unit holds "<unit> <due, seconds of uptime> <boot rule>" while an undo is armed.
guard_names() {
  g_undo="$guard_root/$1.undo"; g_unitfile="$guard_root/$1.unit"; g_mark="$guard_root/$1.expired"
  g_unit=""; g_due=0; g_boot=""
  if [[ -s "$g_unitfile" ]]; then read -r g_unit g_due g_boot <"$g_unitfile" || true; fi
}
guard_now() { local up; read -r up _ <"$guard_uptime"; echo "${up%.*}"; }
guard_newname() { printf 'run-r%s' "$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"; }
# An undo already running finishes before anything replaces or cancels its timer, so the timer's
# undo and cleanup's own never run side by side.
guard_settle() {
  local _
  if [[ -z "$g_unit" ]]; then return 0; fi
  for _ in $(seq 1 120); do
    if ! "$guard_ctl" is-active --quiet "$g_unit.service"; then return 0; fi
    sleep 1
  done
  echo "$g_unit.service still running after 120s" >&2
  return 1
}
guard_stop() {
  if [[ -z "$g_unit" ]]; then return 0; fi
  "$guard_ctl" stop "$g_unit.timer" >/dev/null 2>&1 || true
  "$guard_ctl" reset-failed "$g_unit.timer" "$g_unit.service" >/dev/null 2>&1 || true
}
# A fresh name each start: a stopped transient unit may still be on its way out of PID 1.
guard_start() {
  g_unit=$(guard_newname)
  g_due=$(( $(guard_now) + $1 ))
  printf '%s %s %s\n' "$g_unit" "$g_due" "$g_boot" >"$g_unitfile.tmp"
  mv -T "$g_unitfile.tmp" "$g_unitfile"
  "$guard_run" --quiet --unit="$g_unit" --description= --on-active="$1" \
    --timer-property=AccuracySec=1s --timer-property=RemainAfterElapse=no /bin/bash "$g_undo"
}
guard_arm() {
  local job=$1 secs=$2 body=$3 p
  shift 3
  if [[ ! "$secs" =~ ^[1-9][0-9]*$ ]]; then echo "guard: bad deadline $secs" >&2; return 1; fi
  for p in "$@"; do
    if [[ "$p" != /* || "$p" == *[[:space:]]* ]]; then echo "guard: bad boot path $p" >&2; return 1; fi
  done
  guard_names "$job"
  guard_settle
  guard_stop
  if [[ -n "$g_boot" ]]; then rm -f "$g_boot"; fi
  mkdir -p "$guard_root" "$guard_boot_dir"
  chmod 700 "$guard_root"
  rm -f "$g_mark"
  g_boot="$guard_boot_dir/$(guard_newname).conf"
  {
    printf '%s\n' '#!/bin/bash' 'set -euo pipefail'
    printf '%s\n' "$body"
    printf 'date -u +%%Y-%%m-%%dT%%H:%%M:%%SZ >%q\n' "$g_mark"
    printf 'rm -f %q %q %q\n' "$g_boot" "$g_unitfile" "$g_undo"
  } >"$g_undo.tmp"
  chmod 700 "$g_undo.tmp"
  mv -T "$g_undo.tmp" "$g_undo"
  for p in "$@" "$g_undo" "$g_unitfile" "$g_boot"; do printf 'r %s\n' "$p"; done >"$g_boot.tmp"
  mv -T "$g_boot.tmp" "$g_boot"
  guard_start "$secs"
}
guard_postpone() {
  guard_names "$1"
  guard_settle
  if [[ ! -e "$g_undo" || -z "$g_unit" ]]; then return 0; fi
  if (( g_due - $(guard_now) >= $2 )); then return 0; fi
  guard_stop
  guard_start "$2"
}
guard_disarm() {
  guard_names "$1"
  guard_settle
  if [[ -n "$g_unit" ]] && "$guard_ctl" is-failed --quiet "$g_unit.service" 2>/dev/null; then
    echo "$g_unit.service failed; cleanup undoes the change itself" >&2
  fi
  guard_stop
  if [[ -s "$g_mark" ]]; then echo "the node undid the change by itself at $(cat "$g_mark")" >&2; fi
  rm -f "$g_undo" "$g_undo.tmp" "$g_unitfile" "$g_unitfile.tmp" "$g_mark"
  if [[ -n "$g_boot" ]]; then rm -f "$g_boot" "$g_boot.tmp"; fi
}
'''

GUARD_PROGRAM = b"set -euo pipefail\n" + GUARD_FUNCTIONS + br'''case "$1" in
 arm) shift; guard_arm "$@" ;;
 postpone) shift; guard_postpone "$@" ;;
 disarm) shift; guard_disarm "$@" ;;
 *) exit 2 ;;
esac
'''


def node_guard_function(node_command: str) -> bytes:
    """A bash function ``node_guard <arm|postpone|disarm> ...`` for scripts that reach the node
    through ``<node_command> <remote command>``. The arguments travel on stdin as a `set --` line,
    so the node's auth log records only ``bash -s``."""
    return (
        b"node_guard() {\n"
        b"  { printf 'set --'; printf ' %q' \"$@\"; printf '\\n'; cat <<'OPSJOB_GUARD'\n"
        + GUARD_PROGRAM
        + b"OPSJOB_GUARD\n"
        + b"  } | " + node_command.encode() + b" sudo -n bash -s\n}\n"
    )


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
    stdin = with_failure_trace(stdin)
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
