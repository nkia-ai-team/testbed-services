"""Fakes for the civil-petition VM executors (profiles/vm_executor_common.py and its executors).

Not a test module. The executors' node programs run as root on a VM and their runner side runs ssh in
the runner container. The tests run the real programs: tc, nft and ip against a real kernel inside an
unprivileged user and network namespace (`unshare -rn`), everything else against small fakes that keep
state as files and log every call:

- ssh: `-O check` answers from the file ``master-<host>`` (present = the runner's master is up); any
  other call runs the remote command locally (sudo -n dropped) and logs the destination. A file
  ``down-<host>`` makes every call fail like an unreachable host.
- ssh as the operator (sysop): a login on a real pty (PTY_REMOTE, `pty.fork`), the remote command run by
  `bash -c` as sshd runs it with the login shell, and whatever came on ssh's stdin typed into that pty. The
  operator's sudo reads the terminal (as sudo with use_pty relays it), so lines typed ahead are eaten exactly
  where a real session would lose them (review rv-t4o H1). A held operator-*.lock is noted on the login line.
- setpriv, sudo: setpriv (the database executors' account switch) logs and runs; sudo logs its syslog line.
- systemd-run, systemctl (UNIT_SYSTEMD_RUN, UNIT_SYSTEMCTL): named units and services; a unit's
  command runs in the background and the unit is active while it lives.
- psql, conntrack, sysctl: just enough to answer the executors' queries.
The U8 guard keeps its own fake systemd (node_guard_fakes).
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

import node_guard_fakes

SSH = r'''#!/usr/bin/env bash
d="$VM_SANDBOX"
dest=""; check=no; bind=""; tty=no; nostdin=no; opts=()
while (( $# )); do
  case "$1" in
    -O) [[ "$2" == check ]] && check=yes; shift 2 ;;
    -b) bind="$2"; shift 2 ;;
    -o) opts+=("$2"); shift 2 ;;
    -F|-i|-p|-l) shift 2 ;;
    -tt|-t) tty=yes; shift ;;
    -n) nostdin=yes; shift ;;
    -*) shift ;;
    *) dest="$1"; shift; break ;;
  esac
done
host="${dest#*@}"
if [[ -e "$d/down-$host" ]]; then echo "ssh: connect to host $host port 22: No route to host" >&2; exit 255; fi
if [[ "$check" == yes ]]; then
  printf 'check %s\n' "$dest" >>"$d/ssh-log"
  [[ -e "$d/master-$host" ]] && exit 0
  echo "Control socket connect: No such file or directory" >&2; exit 255
fi
args="$*"
printf 'call %s %s\n' "$dest" "${args//$'\n'/\\n}" >>"$d/ssh-log"
if [[ "${dest%@*}" == hvjob ]]; then exec "$VM_SANDBOX_HV_FRONT"; fi
# An operator login: a new connection (never the master), from the bind address, with a pty. The commands run as the
# operator in its home; sudo then logs TTY=pts/0 like a real login's.
if [[ "${dest%@*}" == sysop ]]; then
  lock=free
  for f in "${CIVIL_PETITION_SSH_CONTROL_DIR:-/nonexistent}"/operator-*.lock; do
    [[ -e "$f" ]] && ! flock -n "$f" true && lock=held
  done
  printf 'login %s bind=%s tty=%s opts=%s stdin=%s lock=%s\n' "$dest" "$bind" "$tty" "${opts[*]}" \
    "$([[ $nostdin == yes ]] && echo null || echo open)" "$lock" >>"$d/ssh-log"
  [[ -e "$d/operator-down" ]] && { echo "ssh: connect to host $host port 22: Connection refused" >&2; exit 255; }
  export FAKE_SUDO_USER=sysop FAKE_TTY=pts/0 FAKE_SUDO_READS_TTY=1 PATH="$d/op-bin:$PATH"
  cd "$OPS_HOME"
  [[ $nostdin == yes ]] && exec </dev/null
  exec python3 "$d/vm-bin/pty-remote" "$*"
fi
if [[ "$1" == sudo ]]; then shift; [[ "$1" == -n ]] && shift; fi
# A file fail-state makes the read of the change's state fail (the management connection lost right after a run).
if [[ -e "$d/fail-state" ]]; then
  IFS= read -r first || true
  if [[ "$first" == "set -- state"* ]]; then echo "ssh: connection lost" >&2; exit 255; fi
  { printf '%s\n' "$first"; cat; } | "$@"
  exit "${PIPESTATUS[1]}"
fi
exec "$@"
'''

SETPRIV = r'''#!/usr/bin/env bash
user=""
while [[ "$1" != -- ]]; do case "$1" in --reuid=*) user="${1#--reuid=}" ;; esac; shift; done
shift
printf '%s: %s\n' "$user" "$*" >>"$VM_SANDBOX/operator-log"
printf '%s %s\n' "$(pwd)" "$*" >>"$VM_SANDBOX/operator-cwd"
export FAKE_SUDO_USER="$user"
exec "$@"
'''

SUDO = r'''#!/usr/bin/env bash
[[ "$1" == -n ]] && shift
# The operator's sudo on a pty reads the terminal (use_pty relays it): it eats whatever was typed ahead.
if [[ -n "${FAKE_SUDO_READS_TTY:-}" ]]; then while IFS= read -r -t 0.2 _ </dev/tty 2>/dev/null; do :; done; fi
printf 'sudo %s\n' "$*" >>"$VM_SANDBOX/sudo-log"
# The line sudo itself sends to syslog (auth facility), with the working directory it would log.
printf '%s : TTY=%s ; PWD=%s ; USER=root ; COMMAND=%s\n' "${FAKE_SUDO_USER:-root}" "${FAKE_TTY:-unknown}" "$(pwd)" "$*" >>"$VM_SANDBOX/sudo-syslog"
exec "$@"
'''

UNIT_RUN = r'''#!/usr/bin/env bash
d="$VM_SANDBOX/units"; mkdir -p "$d"
unit=""; desc=""; props=()
while [[ "${1:-}" == --* ]]; do
  case "$1" in
    --unit=*) unit="${1#--unit=}" ;;
    --description=*) desc="${1#--description=}" ;;
    --property=*) props+=("${1#--property=}") ;;
    --uid=*|--gid=*) props+=("$1") ;;
  esac
  shift
done
[[ -n "$unit" && $# -gt 0 ]] || { echo "fake systemd-run: bad call" >&2; exit 1; }
if [[ -e "$d/$unit.pid" ]] && kill -0 "$(cat "$d/$unit.pid")" 2>/dev/null; then echo "Unit $unit.service was already loaded" >&2; exit 1; fi
printf 'start %s|%s|%s|%s\n' "$unit" "$desc" "${props[*]}" "$*" >>"$VM_SANDBOX/unit-log"
setsid "$@" >"$d/$unit.out" 2>&1 </dev/null &
echo $! >"$d/$unit.pid"
'''

UNIT_CTL = r'''#!/usr/bin/env bash
d="$VM_SANDBOX/units"; mkdir -p "$d"
q=no; args=()
for a in "$@"; do if [[ "$a" == --quiet ]]; then q=yes; else args+=("$a"); fi; done
name="${args[1]:-}"; name="${name%.service}"
alive() { [[ -e "$d/$name.pid" ]] && kill -0 "$(cat "$d/$name.pid")" 2>/dev/null; }
printf 'ctl %s\n' "${args[*]}" >>"$VM_SANDBOX/unit-log"
case "${args[0]}" in
  is-active)
    if [[ -e "$d/$name.service-state" ]]; then [[ "$(cat "$d/$name.service-state")" == active ]]; exit; fi
    alive ;;
  cat) [[ -e "$d/$name.pid" || -e "$d/$name.service-state" ]] ;;
  stop)
    if [[ -e "$d/$name.service-state" ]]; then echo inactive >"$d/$name.service-state"; exit 0; fi
    if alive; then kill -TERM -- -"$(cat "$d/$name.pid")" 2>/dev/null || kill "$(cat "$d/$name.pid")"; for _ in $(seq 1 50); do alive || break; sleep 0.1; done; fi
    rm -f "$d/$name.pid" ;;
  start|restart) echo active >"$d/$name.service-state"; printf '%s %s\n' "${args[0]}" "$name" >>"$VM_SANDBOX/service-log" ;;
  *) exit 9 ;;
esac
'''

PSQL = r'''#!/usr/bin/env bash
sql=""
while (( $# )); do case "$1" in -c) sql="$2"; shift 2 ;; -d) shift 2 ;; *) shift ;; esac; done
printf '%s|%s\n' "${PGAPPNAME:-}" "$sql" >>"$VM_SANDBOX/psql-log"
case "$sql" in
  "SELECT 1") echo 1 ;;
  *"count(*) FROM pg_stat_activity"*) cat "$VM_SANDBOX/backends" 2>/dev/null || echo 0 ;;
  *pg_terminate_backend*) echo 0 >"$VM_SANDBOX/backends"; echo 0 ;;
  *pg_sleep*) echo 1 >"$VM_SANDBOX/backends"; sleep 60 ;;
  *) sleep 0.2 ;;
esac
'''

CONNTRACK = r'''#!/usr/bin/env bash
f="$VM_SANDBOX/conntrack"; touch "$f"
case "$1" in
  -I) echo entry >>"$f" ;;
  -L) cat "$f" ;;
  -D) : >"$f" ;;
esac
'''

SYSCTL = r'''#!/usr/bin/env bash
d="$VM_SANDBOX/sysctl"; mkdir -p "$d"
case "$1" in
  -n) cat "$d/$2" 2>/dev/null || echo 65536 ;;
  -w|-q)
    [[ "$1" == -q ]] && shift
    shift; kv="$1"; printf '%s\n' "${kv#*=}" >"$d/${kv%%=*}"; echo "$kv" ;;
esac
'''


# The operator's end of an ssh -tt login: argv[1] is the remote command, run as sshd runs it (`bash -c`, standing in
# for the login shell) on a new pty; what arrives on stdin is typed into the pty while the session runs. Output comes
# back with the pty's line ends; the exit code is the session's, 124 when it does not end within FAKE_PTY_LIMIT.
PTY_REMOTE = r'''#!/usr/bin/env python3
import os, pty, select, shlex, signal, sys, time
remote = sys.argv[1]
box = os.environ["VM_SANDBOX"]
try:
    words = shlex.split(remote)
except ValueError:
    words = []
typed = words[2] + "\n" if words[:2] == ["bash", "-c"] and len(words) == 3 else ""
pending = sys.stdin.buffer.read()
with open(os.path.join(box, "operator-typed"), "a") as log:
    log.write(typed + pending.decode(errors="replace"))
pid, fd = pty.fork()
if pid == 0:
    os.execvp("bash", ["bash", "-c", remote])
deadline = time.monotonic() + float(os.environ.get("FAKE_PTY_LIMIT", "20"))
out = bytearray()
status = None
while True:
    if time.monotonic() > deadline:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        sys.stdout.buffer.write(bytes(out).replace(b"\r\n", b"\n"))
        print("pty session did not end", file=sys.stderr)
        sys.exit(124)
    want_w = [fd] if pending else []
    r, w, _ = select.select([fd], want_w, [], 0.1)
    if w:
        n = os.write(fd, pending[:256])
        pending = pending[n:]
    if r:
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            chunk = b""
        if not chunk:
            break
        out += chunk
done, status = os.waitpid(pid, 0)
sys.stdout.buffer.write(bytes(out).replace(b"\r\n", b"\n"))
sys.exit(os.waitstatus_to_exitcode(status) if os.waitstatus_to_exitcode(status) >= 0 else 128 - os.waitstatus_to_exitcode(status))
'''


def install(root: Path) -> dict[str, str]:
    """The fakes under ``root``; returns the environment a node program or runner script reads."""
    env = node_guard_fakes.install(root)
    bindir = root / "vm-bin"
    bindir.mkdir(parents=True, exist_ok=True)
    for name, body in (("ssh", SSH), ("setpriv", SETPRIV), ("sudo", SUDO), ("unit-run", UNIT_RUN),
                       ("unit-ctl", UNIT_CTL), ("psql", PSQL), ("conntrack", CONNTRACK), ("sysctl", SYSCTL),
                       ("pty-remote", PTY_REMOTE)):
        path = bindir / name
        path.write_text(body)
        path.chmod(0o755)
    home = root / "home"
    home.mkdir(exist_ok=True)
    # The operator's own tools: systemctl answers from the same fake units and services.
    op_bin = root / "op-bin"
    op_bin.mkdir(exist_ok=True)
    (op_bin / "systemctl").symlink_to(bindir / "unit-ctl")
    env.update({
        "VM_SANDBOX": str(root),
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "OPS_SETPRIV": str(bindir / "setpriv"),
        "OPS_SUDO": str(bindir / "sudo"),
        "OPS_HOME": str(home),
        "PG_SETPRIV": str(bindir / "setpriv"),
        "UNIT_SYSTEMD_RUN": str(bindir / "unit-run"),
        "UNIT_SYSTEMCTL": str(bindir / "unit-ctl"),
        "LINK_MODULE_DIR": "/sys/module",
        "CIVIL_PETITION_SSH_CONTROL_DIR": str(root / "ctl"),
    })
    return env


def master(root: Path, host: str, up: bool = True) -> None:
    path = root / f"master-{host}"
    if up:
        path.touch()
    elif path.exists():
        path.unlink()


def node_call(program: bytes, args: list[str], env: dict[str, str], *, expire_after: int = 1800,
              guard_hold: int = 960, timeout: int = 120) -> subprocess.CompletedProcess:
    """Runs a node program the way node_call sends it: `set --` line, guard values, program on stdin."""
    stdin = ("set -- " + " ".join(shlex.quote(a) for a in args) + "\n"
             + f"expire_after={expire_after} guard_hold={guard_hold}\n").encode() + program
    return subprocess.run(["bash", "-s"], input=stdin, env=env, capture_output=True, timeout=timeout)


class NetnsShell:
    """One long-lived `unshare -rn` shell; every call runs inside the same network namespace."""

    def __init__(self, root: Path, env: dict[str, str], setup: str) -> None:
        self.root = root
        self.env = env
        self.n = 0
        script = root / "netns-shell.sh"
        script.write_text(setup + "\nwhile IFS= read -r cmd; do bash \"$cmd\"; echo \"$?\" >\"$cmd.rc\"; done\n")
        self.proc = subprocess.Popen(["unshare", "-rn", "bash", str(script)], stdin=subprocess.PIPE, env=env,
                                     text=True)

    def run(self, body: str, timeout: int = 120) -> tuple[int, str, str]:
        self.n += 1
        cmd = self.root / f"cmd{self.n}.sh"
        out, err = Path(f"{cmd}.out"), Path(f"{cmd}.err")
        cmd.write_text(f"exec >{shlex.quote(str(out))} 2>{shlex.quote(str(err))}\n" + body)
        rc = Path(f"{cmd}.rc")
        assert self.proc.stdin
        self.proc.stdin.write(f"{cmd}\n")
        self.proc.stdin.flush()
        import time
        deadline = time.time() + timeout
        while not rc.exists():
            if time.time() > deadline:
                raise TimeoutError(body[:200])
            time.sleep(0.02)
        return int(rc.read_text().strip() or "1"), out.read_text(), err.read_text()

    def node(self, program: bytes, args: list[str], expire_after: int = 1800, guard_hold: int = 960) -> tuple[int, str, str]:
        prog = self.root / f"prog{self.n + 1}.sh"
        prog.write_bytes(program)
        line = "set -- " + " ".join(shlex.quote(a) for a in args)
        return self.run(f"{{ printf '%s\\n' {shlex.quote(line)}; echo 'expire_after={expire_after} guard_hold={guard_hold}'; "
                        f"cat {shlex.quote(str(prog))}; }} | bash -s\n")

    def close(self) -> None:
        if self.proc.stdin:
            self.proc.stdin.close()
        self.proc.wait(timeout=30)


def logged(root: Path, name: str) -> list[str]:
    path = root / name
    return path.read_text().splitlines() if path.exists() else []


def plan(profile_id: str, scenario_id: str, params: dict) -> dict:
    """The shape build_invocation reads (the U8 list test builds the same)."""
    return {"scenario": {"id": scenario_id}, "controller": {"runtime": {"max_injection_duration": "10m"}},
            "profile_instances": [{"profile_id": profile_id, "parameters": params, "location": {
                "transport": "ssh", "host": params["host"], "node": params["node"], "resolved": True}}]}


def runner_call(module, profile_id: str, params: dict, action: str, env: dict[str, str], timeout: int = 120,
                shell: NetnsShell | None = None) -> tuple[int, str, str]:
    """build_invocation then the runner script, exactly as executor_common.execute would run it."""
    import unittest.mock
    with unittest.mock.patch.dict(module.CONTRACTS, {"T4-CASE": params}):
        argv, stdin = module.build_invocation(plan(profile_id, "T4-CASE", params), action)
    if shell is None:
        done = subprocess.run(argv, input=stdin, env=env, capture_output=True, timeout=timeout)
        return done.returncode, done.stdout.decode(), done.stderr.decode()
    script = shell.root / f"runner{shell.n + 1}.sh"
    script.write_bytes(stdin)
    return shell.run(" ".join(shlex.quote(a) for a in [argv[0], str(script), *argv[3:]]) + "\n", timeout=timeout)


def run_rsyslogd(rules: str, lines: list[tuple[str, str, str]], workdir: Path) -> tuple[str, str]:
    """A real rsyslogd (-n, user, own socket) with ``rules`` (a first rule that writes diverted lines to
    {diverted} and stops) in front of a catch-all file. ``lines`` are (facility.priority, tag, message), sent with
    logger. Returns (forwarded text, diverted text)."""
    import tempfile
    import time
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="rs") as short:
        sock = Path(short)
        conf_d = workdir / "rsyslog.d"
        conf_d.mkdir(parents=True, exist_ok=True)
        (conf_d / "00-first.conf").write_text(rules.replace("{diverted}", str(sock / "diverted.log")))
        (workdir / "work").mkdir(exist_ok=True)
        conf = workdir / "rsyslog.conf"
        conf.write_text(f'global(workDirectory="{workdir / "work"}")\nmodule(load="imuxsock" SysSock.Use="off")\n'
                        f'input(type="imuxsock" Socket="{sock / "l.sock"}")\ninclude(file="{conf_d}/*.conf")\n'
                        f'*.* action(type="omfile" file="{sock / "forward.log"}")\n')
        check = subprocess.run(["rsyslogd", "-N1", "-f", str(conf)], capture_output=True, text=True, timeout=30)
        if check.returncode != 0:
            raise AssertionError(check.stderr)
        proc = subprocess.Popen(["rsyslogd", "-n", "-f", str(conf), "-i", str(sock / "pid")],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(50):
                if (sock / "l.sock").exists():
                    break
                time.sleep(0.1)
            for prio, tag, message in lines:
                subprocess.run(["logger", "-u", str(sock / "l.sock"), "-p", prio, "-t", tag, message], check=True, timeout=10)
            expected = len(lines)
            for _ in range(60):
                got = sum((sock / f).read_text().count("\n") for f in ("forward.log", "diverted.log") if (sock / f).exists())
                if got >= expected:
                    break
                time.sleep(0.1)
        finally:
            proc.terminate()
            proc.wait(timeout=10)
        read = lambda name: (sock / name).read_text() if (sock / name).exists() else ""
        return read("forward.log"), read("diverted.log")


def dump(obj) -> str:
    return json.dumps(obj, sort_keys=True)
