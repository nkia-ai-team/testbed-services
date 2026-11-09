"""Fake systemd for the node self-recovery guard (executor_common GUARD_FUNCTIONS).

Not a test module. The guard runs as root on a worker and talks to PID 1; the tests run the real
guard program in a sandbox, so systemd-run and systemctl are replaced by scripts that keep the
timers as files and log every call. ``fire`` plays the timer elapsing: it runs exactly the command
systemd-run was given, the way PID 1 would, and unloads the timer (RemainAfterElapse=no).
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

SYSTEMD_RUN = r'''#!/usr/bin/env bash
set -euo pipefail
d="$FAKE_SYSTEMD"; unit=""; desc=""; secs=""; props=()
while [[ "${1:-}" == --* ]]; do
  case "$1" in
    --unit=*) unit="${1#--unit=}" ;;
    --description=*) desc="${1#--description=}" ;;
    --on-active=*) secs="${1#--on-active=}" ;;
    --timer-property=*) props+=("${1#--timer-property=}") ;;
  esac
  shift
done
[[ -n "$unit" && -n "$secs" && $# -gt 0 ]] || { echo "fake systemd-run: bad call" >&2; exit 1; }
# Like PID 1: a unit of the same name that is still loaded refuses a second one.
if [[ -e "$d/$unit.timer" ]]; then echo "Unit $unit.timer was already loaded" >&2; exit 1; fi
printf '%s\n' "$@" >"$d/$unit.timer"
printf 'run %s %s %s|%s|%s\n' "$unit" "$secs" "${props[*]}" "$desc" "$*" >>"$d/log"
'''

SYSTEMCTL = r'''#!/usr/bin/env bash
d="$FAKE_SYSTEMD"
args=()
for a in "$@"; do [[ "$a" == --quiet ]] || args+=("$a"); done
printf 'ctl %s\n' "${args[*]}" >>"$d/log"
case "${args[0]}" in
  stop)
    for u in "${args[@]:1}"; do
      if [[ "$u" == *.timer ]]; then
        [[ -e "$d/$u" ]] || exit 5
        rm -f "$d/$u"
      else
        rm -f "$d/$u.active"
        if [[ -n "${FAKE_STOP_HOOK:-}" ]]; then "$FAKE_STOP_HOOK" "$u"; fi
      fi
    done ;;
  is-active) [[ -e "$d/${args[1]}.active" ]] ;;
  is-failed) [[ -e "$d/${args[1]}.failed" ]] ;;
  reset-failed) for u in "${args[@]:1}"; do rm -f "$d/$u.failed"; done ;;
  *) exit 9 ;;
esac
'''


def install(root: Path) -> dict[str, str]:
    """Fake systemd under ``root``; returns the environment the guard reads in a sandbox."""
    bindir = root / "guard-bin"
    state = root / "systemd"
    bindir.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    (state / "log").touch()
    for name, body in (("systemd-run", SYSTEMD_RUN), ("systemctl", SYSTEMCTL)):
        path = bindir / name
        path.write_text(body)
        path.chmod(0o755)
    return {
        "FAKE_SYSTEMD": str(state),
        "OPSJOB_SYSTEMD_RUN": str(bindir / "systemd-run"),
        "OPSJOB_SYSTEMCTL": str(bindir / "systemctl"),
        "OPSJOB_STATE_ROOT": str(root / "state"),
        "OPSJOB_TMPFILES_DIR": str(root / "tmpfiles.d"),
    }


def timers(env: dict[str, str]) -> list[str]:
    return sorted(p.name for p in Path(env["FAKE_SYSTEMD"]).glob("*.timer"))


def calls(env: dict[str, str]) -> list[str]:
    return (Path(env["FAKE_SYSTEMD"]) / "log").read_text().splitlines()


def starts(env: dict[str, str], job: str) -> list[dict[str, str]]:
    """Every systemd-run for ``job``'s undo, in order: unit, seconds, timer properties, description."""
    out = []
    for line in calls(env):
        if not line.startswith("run "):
            continue
        _, unit, secs, rest = line.split(" ", 3)
        props, desc, cmd = rest.split("|", 2)
        if cmd.endswith(f"/{job}.undo"):
            out.append({"unit": unit, "seconds": secs, "props": props, "description": desc, "command": cmd})
    return out


def armed_seconds(env: dict[str, str], job: str) -> list[int]:
    return [int(row["seconds"]) for row in starts(env, job)]


def unit_record(env: dict[str, str], job: str) -> list[str]:
    """``<unit> <due> <boot rule>`` from the node's state file, or [] when nothing is armed."""
    path = Path(env["OPSJOB_STATE_ROOT"]) / f"{job}.unit"
    return path.read_text().split() if path.exists() else []


def command(env: dict[str, str], job: str) -> list[str]:
    unit = unit_record(env, job)[0]
    return (Path(env["FAKE_SYSTEMD"]) / f"{unit}.timer").read_text().splitlines()


def fire(env: dict[str, str], job: str, *, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """The timer elapses: PID 1 runs its command once and unloads the timer."""
    unit = unit_record(env, job)[0]
    argv = (Path(env["FAKE_SYSTEMD"]) / f"{unit}.timer").read_text().splitlines()
    os.unlink(Path(env["FAKE_SYSTEMD"]) / f"{unit}.timer")
    run_env = {**os.environ, **env, **(extra_env or {})}
    return subprocess.run(argv, env=run_env, capture_output=True, timeout=120, check=False)


NEUTRAL_UNIT = re.compile(r"^run-r[0-9a-f]{32}$")


def journal_words(env: dict[str, str], job: str) -> list[str]:
    """What the node's journal would show for ``job``'s timers that is not the generic form: a unit
    name other than run-r<32 hex>, or any description. Empty when the trace says nothing."""
    out = []
    for row in starts(env, job):
        if not NEUTRAL_UNIT.fullmatch(row["unit"]):
            out.append(row["unit"])
        if row["description"]:
            out.append(row["description"])
    return out
