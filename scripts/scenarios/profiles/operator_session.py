#!/usr/bin/env python3
"""An operator ssh session on one civil-petition VM (docs/spec-testbed-vm-3tier.md §6-2 운영자 경로, Q35 answer).

The change-cause executors (router.config, vm.service, and the U7 decoy deployment through vm.service) type the
operator's commands in an operator ssh login (vm_executor_common.RUNNER_HEAD op_session). If such logins happened only
at injection times, "operator login" would mark the cause time (design principle 8). So the runner also opens operator
sessions in normal time: same account, same source address, same ssh form, read-only commands. This module is that
normal-time session and the one place that fixes its form; the bash op_session builds the same ssh command (a test
holds the two equal).

  operator_session.py --node <VM name> --check          one read-only operator session (the runner's schedule calls it)
  operator_session.py --node <VM name> --print          the ssh command and the session text as JSON, nothing run
  operator_session.py --list-nodes                      the VMs that take normal-time sessions, one JSON line
  operator_session.py --node <VM name> --decoy <kind>   a harmless operator change and its undo (U7 decoy, T4U)

The session takes the per-VM lock <CIVIL_PETITION_SSH_CONTROL_DIR>/operator-<VM>.lock that the executors take too, so
a normal-time session and an injection never share a VM at once. The session text goes as the remote command
(`bash -c '<text>'`, stdin /dev/null), never on the pty's input (review rv-t4o H1). ssh runs in its own process group
and the whole group ends after SESSION_TIMEOUT_SECONDS (under the runner's 120 seconds, review rv-t4o M2). Exit code:
the ssh session's, 75 when the lock is held (the runner retries that VM later), 124 when the limit ended it, 3 on
refusal.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vm_executor_common as vm  # noqa: E402

OPERATOR = "sysop"
# Exit code when an executor's operator session holds the VM's lock (EX_TEMPFAIL): the runner retries that VM later.
BUSY_EXIT = 75
# Under the runner's limit for this CLI (120 seconds) with room for the group to end (review rv-t4o M2).
SESSION_TIMEOUT_SECONDS = 90
# What an operator looks at on a routine check of each role. Read-only: nothing here changes the VM. Each sudo line
# reaches 119 as the injection sessions' do (TTY=pts/N); "|| true" keeps a missing log from ending the session.
ROLE_CHECKS: dict[str, list[str]] = {
    "router": ["uptime", "ip -br addr", "sudo nft list ruleset | head -n 40",
               "sudo tail -n 20 /var/log/syslog || true"],
    "web": ["uptime", "df -h /", "sudo systemctl status apache2 --no-pager | head -n 15 || true",
            "sudo tail -n 20 /var/log/apache2/error.log || true"],
    "was": ["uptime", "df -h /", "sudo systemctl status tomcat10 --no-pager | head -n 15 || true",
            "sudo journalctl -u tomcat10 -n 20 --no-pager || true"],
    "db": ["uptime", "df -h / /pgdata", "sudo systemctl status postgresql@16-main --no-pager | head -n 15 || true",
           "sudo tail -n 20 /var/log/postgresql/postgresql-16-main.log || true"],
}


# U7 decoys on this domain (T4U, command session 11-20): an operator change that leaves the VM as it was, typed the way
# vm.service types a change and its undo (run session, then cleanup session, each a login). The copies go to the
# operator's home, not the executor's backup path, so a decoy never touches an injection's state.
WAR = "/var/lib/tomcat10/webapps/petition.war"
WEB_CONF = "/etc/apache2/sites-available/petition.conf"
DECOYS: dict[str, dict[str, Any]] = {
    # Tomcat redeploys the same bytes (the WAR's mtime changes); nothing else moves.
    "war_redeploy": {"role": "was",
                     "run": [f"sudo cp -p {WAR} ~/petition.war.prev", f"sudo cp ~/petition.war.prev {WAR}"],
                     "cleanup": ["sudo rm -f ~/petition.war.prev"]},
    # The site file copied back over itself and a graceful reload (open connections finish first).
    "config_reapply": {"role": "web",
                       "run": [f"sudo cp -p {WEB_CONF} ~/petition.conf.prev", f"sudo cp ~/petition.conf.prev {WEB_CONF}",
                               "sudo systemctl reload apache2"],
                       "cleanup": ["sudo rm -f ~/petition.conf.prev"]},
}


class Refused(ValueError):
    pass


def ssh_argv(host: str) -> list[str]:
    """The operator login up to the destination (the same words as op_ssh in vm_executor_common.RUNNER_HEAD)."""
    keys = os.environ.get(vm.KEY_DIR_ENV, vm.KEY_DIR)
    return ["ssh", "-tt", "-n", "-F", "none", "-i", f"{keys}/t4-operator", "-o", "IdentitiesOnly=yes",
            "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={keys}/known_hosts_civil-petition", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-o", "ControlPath=none",
            "-b", vm.OPERATOR_BIND_ADDR, f"{OPERATOR}@{host}"]


def session_text(lines: list[str]) -> str:
    """What op_session runs: set -e, the operator's lines, exit 0 (op_session builds the same text)."""
    return "set -e\n" + "\n".join(lines) + "\nexit 0"


def remote_command(text: str) -> str:
    """The remote command: bash -c with the text single-quoted (op_quote in RUNNER_HEAD quotes the same way)."""
    return "bash -c " + shlex.quote(text)


def routine_nodes() -> list[str]:
    """The VMs that take normal-time operator sessions: the router and the servers (the runner's list, review L3)."""
    return [name for name, row in vm.VMS.items() if row["role"] in ROLE_CHECKS]


def check_session(node: str) -> tuple[list[str], str]:
    row = vm.VMS.get(node)
    if row is None or row["role"] not in ROLE_CHECKS:
        raise Refused(f"{node!r} is not a router or server VM of the domain")
    text = session_text(ROLE_CHECKS[row["role"]])
    return ssh_argv(row["mgmt"]) + [remote_command(text)], text


def decoy_nodes(kind: str) -> list[str]:
    return [name for name, row in vm.VMS.items() if row["role"] == DECOYS[kind]["role"]]


def decoy_sessions(node: str, kind: str) -> list[list[str]]:
    """The run session and the cleanup session of a decoy, each a full operator login command."""
    if kind not in DECOYS:
        raise Refused(f"{kind!r} is not a decoy kind ({', '.join(sorted(DECOYS))})")
    row = vm.VMS.get(node)
    if row is None or row["role"] != DECOYS[kind]["role"]:
        raise Refused(f"{node!r} does not take {kind} (only {', '.join(decoy_nodes(kind))})")
    return [ssh_argv(row["mgmt"]) + [remote_command(session_text(DECOYS[kind][part]))] for part in ("run", "cleanup")]


def lock_path(node: str) -> Path:
    return Path(os.environ.get(vm.CONTROL_DIR_ENV, vm.CONTROL_DIR_DEFAULT)) / f"operator-{node}.lock"


def run(node: str, argv: list[str], text: str) -> int:
    return run_sessions(node, [argv])


def run_sessions(node: str, sessions: list[list[str]]) -> int:
    """The sessions one after another under the VM's lock and one deadline. Every session runs even when one before it
    failed (a decoy's cleanup still removes its copy); the first failing code is the result."""
    path = lock_path(node)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as lock:
        # A normal-time session never waits: an executor's operator session holds the VM, the runner tries again later.
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"operator session on {node} is busy", file=sys.stderr)
            return BUSY_EXIT
        deadline = time.monotonic() + SESSION_TIMEOUT_SECONDS
        result = 0
        for argv in sessions:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, start_new_session=True)
            try:
                rc = proc.wait(timeout=max(deadline - time.monotonic(), 0.1))
            except subprocess.TimeoutExpired:
                end_group(proc)
                print(f"operator session on {node} passed {SESSION_TIMEOUT_SECONDS}s and was ended", file=sys.stderr)
                return 124
            if rc != 0 and result == 0:
                print(f"operator session on {node} ended with {rc}", file=sys.stderr)
                result = rc
        return result


def end_group(proc: subprocess.Popen) -> None:
    """End ssh and anything it started (its own session), TERM first then KILL."""
    for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--node")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--print", action="store_true")
    mode.add_argument("--list-nodes", action="store_true")
    mode.add_argument("--decoy")  # an unknown kind is a refusal (3), not a usage error
    args = parser.parse_args(argv)
    if args.list_nodes:
        print(json.dumps(routine_nodes()))
        return 0
    if not args.node:
        parser.error("--node is needed with --check, --print and --decoy")
    if args.decoy:
        try:
            sessions = decoy_sessions(args.node, args.decoy)
        except Refused as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 3
        return run_sessions(args.node, sessions)
    try:
        command, text = check_session(args.node)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    if args.print:
        print(json.dumps({"node": args.node, "argv": command, "text": text, "lock": str(lock_path(args.node))},
                         ensure_ascii=False))
        return 0
    return run(args.node, command, text)


if __name__ == "__main__":
    raise SystemExit(main())
