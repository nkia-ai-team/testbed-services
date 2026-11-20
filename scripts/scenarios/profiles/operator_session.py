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
  operator_session.py --node <VM name> --decoy-change <kind>   a U7 decoy: a vm.service change with harmless values
  operator_session.py --node <VM name> --decoy-revert <kind>   its undo (nothing to undo is 0)

The session takes the per-VM lock <CIVIL_PETITION_SSH_CONTROL_DIR>/operator-<VM>.lock that the executors take too, so
a normal-time session and an injection never share a VM at once. The session text goes as the remote command
(`bash -c '<text>'`, stdin /dev/null), never on the pty's input (review rv-t4o H1). ssh runs in its own process group
and the whole group ends after SESSION_TIMEOUT_SECONDS (the runner allows this plus 60 seconds, review rv-t4o M2).
ssh is in its own session, so a stop of this CLI (TERM, INT, HUP: the runner ends its group TERM first) ends ssh's
group before the CLI goes (rv-t4o2 N3). Exit code:
the ssh session's, 75 when the lock is held (the runner retries that VM later), 124 when the limit ended it, 3 on
refusal.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import random
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vm_executor_common as vm  # noqa: E402

OPERATOR = "sysop"
# Exit code when an executor's operator session holds the VM's lock (EX_TEMPFAIL): the runner retries that VM later.
BUSY_EXIT = 75
# The runner calls this CLI with this limit plus 60 seconds (150), room for the group to end (review rv-t4o M2).
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


# U7 decoys on this domain (T4U, command session 11-20; review rv-t4u R1 (가)): a decoy IS a vm.service change and its
# undo with harmless values, run through the vm.service executor itself, so every line the VM and 119 see (the
# operator's login, the sudo COMMAND lines with the same backup files and the same word order, the restart and its
# journal lines, the guard the executor arms) is an injection's, character for character. Only the values differ:
#   war_redeploy    the WAR swapped for one of the releases the was step leaves (the deployed bytes, or the same
#                   entries with a build mark only, drawn each time, rv-t4u3 D3; vm.service release_path names them)
#                   and tomcat10 restarted, as a war injection does (autoDeploy is off, rv-t4u2 N1): the same
#                   application starts again.
#   config_reapply  one line of the site file changed to a value with no effect on the traffic (the proxy timeout
#                   from 30 to one of PROXY_TIMEOUTS, drawn each time, rv-t4u2 N6; no request takes that long) and
#                   apache2 restarted, as a config injection does. The undo copies the backup back (30 again).
# The change and its undo are two calls (--decoy-change, --decoy-revert); the runner draws the gap between them from
# the decoy policy's vm_hold_sec (the approved injections' durations, contract with the runner). The executor's guard
# undoes the change by itself when the revert never comes: vm_hold_sec max goes in as max_injection_duration and the
# guard adds its own 600 seconds (2478 with the recorded range, rv-t4u2 N2). After each call the application must answer its health check
# (review R4). Exit codes: 0, 1 not healthy after the change (taken back on the spot), 3 refused, 4 the executor
# failed (it took back what it could as root), 75 busy, 124 the phase passed DECOY_PHASE_SECONDS. The health wait is
# the was step's 120 seconds (the same restart, steps/was.sh settle; command session 11-21). A decoy call has its own
# limits, apart from the normal-time check's 90: the executor and the health wait end within DECOY_PHASE_SECONDS,
# the whole call, a take-back included, within DECOY_LIMIT_SECONDS; the runner allows that plus 60 (300, rv-t4u2 N4).
WAR = "/var/lib/tomcat10/webapps/petition.war"
WAR_SHA256 = "/var/lib/petition-deploy/war.sha256"
DECOY_RELEASES = "/var/lib/petition-deploy/decoy-releases.sha256"
# A revert does not read the release list: the undo uses the backup only, any well-formed release passes the contract.
REVERT_PLACEHOLDER_SHA256 = "0" * 64
WEB_CONF = "/etc/apache2/sites-available/petition.conf"
PROXY_LINE = '\tProxyPass        "/petition/" "balancer://wascluster/petition/" timeout=30'
PROXY_TIMEOUTS = (25, 35, 40, 45, 60)
DECOYS: dict[str, dict[str, Any]] = {
    "war_redeploy": {"role": "was", "unit": "tomcat10", "health": "http://127.0.0.1:8080/petition/api/health"},
    "config_reapply": {"role": "web", "unit": "apache2", "health": "http://127.0.0.1:80/petition/api/health"},
}
DECOY_POLICY = Path(__file__).resolve().parents[1] / "registry" / "decoy-deployments.json"
HEALTH_WAIT_SECONDS = 120
DECOY_PHASE_SECONDS = 170
DECOY_LIMIT_SECONDS = 240
DECOY_EXECUTOR_FAILED = 4


def decoy_hold_max() -> int:
    """The longest time from a change to its revert: vm_hold_sec max, or vm_revert_by_sec when the runner reverts
    later (after the recording, rv-t4u3 D1); the executor's guard adds its 600 seconds to it."""
    policy = json.loads(DECOY_POLICY.read_text(encoding="utf-8"))
    return max(int(policy["vm_hold_sec"]["max"]), int(policy.get("vm_revert_by_sec", 0)))


def proxy_line(timeout: int) -> str:
    return PROXY_LINE.replace("timeout=30", f"timeout={timeout}")


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


def decoy_row(node: str, kind: str) -> dict[str, Any]:
    if kind not in DECOYS:
        raise Refused(f"{kind!r} is not a decoy kind ({', '.join(sorted(DECOYS))})")
    row = vm.VMS.get(node)
    if row is None or row["role"] != DECOYS[kind]["role"]:
        raise Refused(f"{node!r} does not take {kind} (only {', '.join(decoy_nodes(kind))})")
    return row


def decoy_release(war_sha256: str) -> str:
    """The same-bytes release: vm.service's one naming rule (the was step leaves the deployed WAR under it)."""
    import vm_service_executor as svc
    return svc.release_path(war_sha256)


def decoy_params(node: str, kind: str, war_sha256: str | None = None, timeout: int | None = None) -> dict[str, Any]:
    """The vm.service contract of a decoy: an injection's parameters with harmless values. config_reapply draws the
    proxy timeout unless one is given (the revert ignores it: the undo copies the backup back)."""
    row = decoy_row(node, kind)
    base = {"host": row["mgmt"], "node": node, "unit": DECOYS[kind]["unit"], "file": "-", "match": "-", "replace": "-",
            "release": "-", "release_sha256": "-"}
    if kind == "war_redeploy":
        if not war_sha256 or len(war_sha256) != 64:
            raise Refused("the deployed WAR's sha256 is unknown")
        return {**base, "action": "war", "release": decoy_release(war_sha256), "release_sha256": war_sha256}
    if timeout is None:
        timeout = random.SystemRandom().choice(PROXY_TIMEOUTS)
    if timeout not in PROXY_TIMEOUTS:
        raise Refused(f"proxy timeout {timeout} is not one of {PROXY_TIMEOUTS}")
    return {**base, "action": "config", "file": WEB_CONF, "match": PROXY_LINE, "replace": proxy_line(timeout)}


def decoy_plan(params: dict[str, Any], hold_seconds: int) -> dict[str, Any]:
    """The plan shape the executor reads; the hold is max_injection_duration (the guard adds its own margin)."""
    return {"scenario": {"id": "operator-decoy"}, "controller": {"runtime": {"max_injection_duration": f"{hold_seconds}s"}},
            "profile_instances": [{"profile_id": "vm.service", "parameters": params, "location": {
                "transport": "ssh", "host": params["host"], "node": params["node"], "resolved": True}}]}


def decoy_invocation(params: dict[str, Any], hold_seconds: int, action: str) -> tuple[list[str], bytes]:
    import vm_service_executor as svc
    svc.CONTRACTS["operator-decoy"] = params
    return svc.build_invocation(decoy_plan(params, hold_seconds), action)


def executor_read(host: str, command: str, timeout: float = 20) -> tuple[int, str]:
    """One read over the runner's always-open executor connection (never a new login: no master, no read)."""
    keys = os.environ.get(vm.KEY_DIR_ENV, vm.KEY_DIR)
    ctl = os.environ.get(vm.CONTROL_DIR_ENV, vm.CONTROL_DIR_DEFAULT)
    base = ["ssh", "-F", "none", "-i", f"{keys}/t4-executor", "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={keys}/known_hosts_civil-petition",
            "-o", "ConnectTimeout=10", "-o", f"ControlPath={ctl}/%r@%h:%p"]
    check = subprocess.run(base + ["-O", "check", f"opsjob@{host}"], stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=timeout)
    if check.returncode != 0:
        return 255, f"control connection to {host} is down"
    # The command on stdin to `bash -s`, as the runner's own reads go (no sudo: both reads need none).
    done = subprocess.run(base + ["-o", "ControlMaster=no", f"opsjob@{host}", "bash", "-s"],
                          input=command + "\n", capture_output=True, text=True, timeout=timeout)
    return done.returncode, done.stdout


def decoy_war_sha256(host: str) -> str:
    """One of the releases the was step listed for decoys, drawn each time (rv-t4u3 D3)."""
    rc, out = executor_read(host, f'cat "${{PETITION_DECOY_RELEASES:-{DECOY_RELEASES}}}"')
    sums = [line.split()[0] for line in out.splitlines() if line.strip() and len(line.split()[0]) == 64]
    if rc != 0 or not sums:
        raise Refused(f"cannot read the decoy releases on {host}")
    return random.SystemRandom().choice(sums)


def wait_healthy(host: str, kind: str, deadline: float) -> bool:
    url = DECOYS[kind]["health"]
    command = f'curl -fsS -m 3 -o /dev/null "${{PETITION_HEALTH_URL:-{url}}}"'
    while time.monotonic() < deadline - 1:
        try:
            rc, _ = executor_read(host, command, timeout=min(20, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            return False
        if rc == 0:
            return True
        time.sleep(min(2, max(deadline - time.monotonic(), 0)))
    return False


def run_decoy(node: str, kind: str, phase: str) -> int:
    """change: the executor's run (the operator's change), then the application's health; 0 only when healthy.
    revert: cleanup and recovery (the operator's undo, root takes back what is left; nothing to undo is 0), then
    health. The executor's operator session takes the VM's lock itself; this only refuses early (75) when a session
    already holds it."""
    from executor_common import execute
    started = time.monotonic()
    row = decoy_row(node, kind)
    hold_seconds = decoy_hold_max()
    path = lock_path(node)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"operator session on {node} is busy", file=sys.stderr)
            return BUSY_EXIT
    deadline = started + DECOY_PHASE_SECONDS
    if kind == "war_redeploy":
        war_sha256 = decoy_war_sha256(row["mgmt"]) if phase == "change" else REVERT_PLACEHOLDER_SHA256
    else:
        war_sha256 = None
    params = decoy_params(node, kind, war_sha256, None if phase == "change" else PROXY_TIMEOUTS[0])
    actions = ["run"] if phase == "change" else ["cleanup", "recovery"]
    for action in actions:
        argv, stdin = decoy_invocation(params, hold_seconds, action)
        try:
            rc = execute(argv, stdin, timeout=max(deadline - time.monotonic(), 1))
        except subprocess.TimeoutExpired:
            print(f"decoy {kind} {phase} on {node} passed {DECOY_PHASE_SECONDS}s and was ended", file=sys.stderr)
            return 124
        if rc != 0:
            # Not the application's answer but the executor's own failure (it took back what it could): its own code.
            print(f"decoy {kind} {phase} on {node}: the executor's {action} ended with {rc}", file=sys.stderr)
            return DECOY_EXECUTOR_FAILED
    if not wait_healthy(row["mgmt"], kind, min(deadline, time.monotonic() + HEALTH_WAIT_SECONDS)):
        if phase == "change":
            # The change hurt: take it back now (the operator's undo, root for the rest) within what is left of the
            # whole call's budget, then report.
            end = started + DECOY_LIMIT_SECONDS
            for action in ("cleanup", "recovery"):
                argv, stdin = decoy_invocation(params, hold_seconds, action)
                left = end - time.monotonic()
                if left < 5:
                    break
                try:
                    execute(argv, stdin, timeout=left)
                except subprocess.TimeoutExpired:
                    break
        print(f"decoy {kind} {phase} on {node}: the application did not answer its health check", file=sys.stderr)
        return 1
    return 0


def lock_path(node: str) -> Path:
    return Path(os.environ.get(vm.CONTROL_DIR_ENV, vm.CONTROL_DIR_DEFAULT)) / f"operator-{node}.lock"


def run(node: str, argv: list[str], text: str) -> int:
    return run_sessions(node, [argv])


def run_sessions(node: str, sessions: list[list[str]]) -> int:
    """The sessions one after another under the VM's lock and one deadline. A session runs even when one before it
    failed; the first failing code is the result. At the deadline the running session's group ends and the call
    returns 124 (rv-t4u L3)."""
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
            # ssh writes to a file of this CLI, never to the runner's pipes: an ssh that outlived the CLI would hold
            # them open (rv-t4o2 N3). The last line goes to stderr when the session fails.
            with tempfile.TemporaryFile() as errors:
                proc, previous = start_forwarding(argv, errors)
                try:
                    rc = proc.wait(timeout=max(deadline - time.monotonic(), 0.1))
                except subprocess.TimeoutExpired:
                    end_group(proc)
                    print(f"operator session on {node} passed {SESSION_TIMEOUT_SECONDS}s and was ended", file=sys.stderr)
                    return 124  # the deadline is the whole call's: later sessions do not run
                finally:
                    for sig, handler in previous.items():
                        signal.signal(sig, handler)
                if rc != 0 and result == 0:
                    errors.seek(0)
                    last = [line for line in errors.read().decode(errors="replace").splitlines() if line.strip()][-1:]
                    print(f"operator session on {node} ended with {rc}" + (f": {last[0].strip()}" if last else ""),
                          file=sys.stderr)
                    result = rc
        return result


STOPS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


def start_forwarding(argv: list[str], errors) -> tuple[subprocess.Popen, dict]:
    """While ssh runs, a TERM, INT or HUP to this CLI ends ssh's group (within 2 x STOP_GRACE_SECONDS, under the
    runner's 5 seconds before KILL), then the CLI goes with the same signal (rv-t4o2 N3).

    Start ssh in its own session with the forwarding handler already set, so a stop that comes while ssh starts is
    not lost and cannot leave ssh behind (rv-t4u L2). A stop before Popen returned is noted and handled right after."""
    holder: dict[str, Any] = {}
    early: list[int] = []

    def stop(signum, _frame):
        if "proc" not in holder:
            early.append(signum)
            return
        end_group(holder["proc"], STOP_GRACE_SECONDS)
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    previous = {sig: signal.signal(sig, stop) for sig in STOPS}
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors,
                            start_new_session=True)
    holder["proc"] = proc
    if early:
        stop(early[0], None)
    return proc, previous


STOP_GRACE_SECONDS = 2


def end_group(proc: subprocess.Popen, grace: float = 5) -> None:
    """End ssh and anything it started (its own session), TERM first then KILL."""
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, grace)):
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
    # An unknown kind is a refusal (3), not a usage error.
    mode.add_argument("--decoy-change", metavar="KIND")
    mode.add_argument("--decoy-revert", metavar="KIND")
    args = parser.parse_args(argv)
    if args.list_nodes:
        print(json.dumps(routine_nodes()))
        return 0
    if not args.node:
        parser.error("--node is needed with --check, --print, --decoy-change and --decoy-revert")
    if args.decoy_change or args.decoy_revert:
        try:
            if args.decoy_change:
                return run_decoy(args.node, args.decoy_change, "change")
            return run_decoy(args.node, args.decoy_revert, "revert")
        except Refused as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 3
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
