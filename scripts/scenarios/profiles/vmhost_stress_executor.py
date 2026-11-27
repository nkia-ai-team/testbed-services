#!/usr/bin/env python3
"""vmhost.stress: CPU, memory, disk IO or disk space pressure on one server VM (§6-1).

The VM counterpart of host.stress, with the tools a server VM has (no stress-ng or fio there):

- cpu: N `yes` busy loops in one process group for a bounded time.
- memhog: one python3 process allocates and touches N MiB, then holds it.
- memprocs: a job of several python3 worker processes that together allocate and touch N MiB (each N/P MiB) and
  hold it, meant to be more than the server has free. The kernel's OOM killer picks the process with the largest
  resident set, and a database's processes count the shared buffers they have touched in theirs, so a job of
  workers each smaller than the database's largest process makes the kernel kill the database, not the job
  (one big job process would be the bigger one and die itself). Its undo also starts the service unit the
  contract names when that unit is no longer active (systemd's default OOMPolicy=stop takes a unit down when the
  kernel kills one of its processes, and the PostgreSQL unit has no Restart=). Bringing it back is bounded (v4 REVIVE,
  F80-P third recording): when the kernel kills only the postmaster's children, the postmaster can hang in its
  shutdown and the unit stays deactivating for its TimeoutStopSec (1 hour for postgresql@), with a plain start queued
  behind that stop job. So the undo waits for the stop at most REVIVE_STOP_WAIT seconds (60: a clean PostgreSQL stop
  writes at most its 2 GB of shared buffers in the shutdown checkpoint, about 37 s at 55 MB/s), then sends SIGKILL to
  that unit's control group only (systemctl kill, the unit from the fixed role table), resets a failed state, starts
  it without blocking and waits until it is active. The whole bring-back ends within REVIVE_TOTAL seconds (180: the stop
  wait, the kill and a start whose crash recovery took 0.13 s on dbsvr01, with room); with the job's own stop (at most
  about a minute) it stays inside the manifest's cleanup timeout (5 minutes for F80-P), the executor's 900 s and the
  dispatcher's 1200 s. Past it the undo fails (DIRTY), as before. Each step goes to the cleanup output with its second.
- io: `dd` writes a file under the target directory with direct IO, over and over, for a bounded time.
- fill: one fallocate'd file under the target directory brings the file system to a watermark, never
  past a reserve, never more than a maximum.

Job and file names are an operator's plain words (burnin, memhold, iobench, bulkcopy); nothing on the
VM names the testbed or the scenario (design principle 8). The connection, sudo and guard lines are
diverted by the VM's first rsyslog rule (§6-2); what reaches 119 is the change itself (the processes in
the process sample, the CPU, memory, disk and file system series, a kernel OOM message if it comes to
that).

Self-recovery (U8): armed before the job starts, with an undo that kills exactly this process group and
removes the job's files; the files are listed for removal at the next boot.

The executor is the means only; a scenario without an exact contract is refused.
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, profile_instance
import vm_executor_common as vm

PROFILE_ID = "vmhost.stress"
CONTRACTS: dict[str, dict[str, Any]] = {
    # F80-P: a job of twelve 512 MiB workers on the SMS testbed's database server dbsvr01 (8 GB, no swap, PostgreSQL
    # shared_buffers 2 GB whose processes show about 2.1 GiB resident each); the kernel kills a PostgreSQL process,
    # systemd stops the unit (OOMPolicy=stop, no Restart=), cleanup stops the job and starts PostgreSQL again.
    "F80-P": {"host": "192.168.133.31", "node": "dbsvr01", "mode": "memprocs", "processes": 12, "mib": 6144,
              "runtime_seconds": 1500, "revive_unit": "postgresql@16-main"},
}
KEYS = {"host", "node", "mode", "cpu_workers", "mib", "target_dir", "watermark_percent", "reserve_mib", "runtime_seconds"}
JOBS = {"cpu": "burnin", "memhog": "memhold", "io": "iobench", "fill": "bulkcopy", "memprocs": "extract"}
# memprocs takes its own keys (the other modes keep theirs exactly): the worker count and the unit the undo brings
# back. The unit is the server role's own service, nothing else.
PROC_KEYS = {"host", "node", "mode", "processes", "mib", "runtime_seconds", "revive_unit"}
REVIVE_UNITS = {"db": "postgresql@16-main", "was": "tomcat10", "web": "apache2"}
MIN_WORKER_MIB = 16


def check_proc_parameters(p: dict[str, Any]) -> list[str]:
    vm.exact_keys(p, PROC_KEYS, PROFILE_ID)
    node = vm.vm_named(p, ("web", "was", "db"))
    role = vm.testbed_of(p).vms[node]["role"]
    processes = vm.integer(p["processes"], "processes", 2, 16)
    mib = vm.integer(p["mib"], "mib", 1, 7168)
    runtime = vm.integer(p["runtime_seconds"], "runtime_seconds", 60, 1800)
    if mib % processes or mib // processes < MIN_WORKER_MIB:
        raise ExecutorError(f"mib must split evenly into workers of at least {MIN_WORKER_MIB} MiB")
    if p["revive_unit"] != REVIVE_UNITS[role]:
        raise ExecutorError(f"revive_unit must be the {role} server's service {REVIVE_UNITS[role]}")
    return ["memprocs", str(processes), str(mib), "-", "0", "0", str(runtime), p["revive_unit"]]


def check_parameters(p: dict[str, Any]) -> list[str]:
    if isinstance(p, dict) and p.get("mode") == "memprocs":
        return check_proc_parameters(p)
    vm.exact_keys(p, KEYS, PROFILE_ID)
    vm.vm_named(p, ("web", "was", "db"))
    mode = p["mode"]
    if mode not in JOBS or mode == "memprocs":
        raise ExecutorError(f"mode must be one of {sorted(JOBS)}")
    workers = vm.integer(p["cpu_workers"], "cpu_workers", 0, 8)
    mib = vm.integer(p["mib"], "mib", 0, 7168)
    watermark = vm.integer(p["watermark_percent"], "watermark_percent", 0, 99)
    reserve = vm.integer(p["reserve_mib"], "reserve_mib", 0, 1_048_576)
    runtime = vm.integer(p["runtime_seconds"], "runtime_seconds", 0, 1800)
    target = p["target_dir"]
    if target != "-" and (not isinstance(target, str) or not target.startswith("/") or ".." in target
                          or any(c.isspace() for c in target)):
        raise ExecutorError("target_dir must be - or an absolute path")
    expect = {
        "cpu": (workers > 0 and runtime >= 60 and not mib and not watermark and not reserve and target == "-"),
        "memhog": (mib > 0 and runtime >= 60 and not workers and not watermark and not reserve and target == "-"),
        "io": (mib > 0 and runtime >= 60 and target != "-" and not workers and not watermark and not reserve),
        "fill": (watermark > 0 and reserve > 0 and mib > 0 and target != "-" and not workers and not runtime),
    }[mode]
    if not expect:
        raise ExecutorError(f"{mode} parameters do not fit the mode")
    return [mode, str(workers), str(mib), target, str(watermark), str(reserve), str(runtime)]


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    vm.contract_lookup(CONTRACTS, scenario_id, params, profile, "server stress")
    check_parameters(params)


# set -- <act> <mode> <workers> <mib> <target dir> <watermark> <reserve> <runtime> [<revive unit>]
# memprocs: workers is the process count and mib their total; the ninth word is the unit its undo brings back.
NODE_BODY = vm.GROUP_FUNCTIONS + br'''act="$1"; mode="$2"; workers="$3"; mib="$4"; target="$5"; watermark="$6"; reserve="$7"; runtime="$8"; revive="${9:--}"
case "$mode" in cpu) job=burnin ;; memhog) job=memhold ;; io) job=iobench ;; fill) job=bulkcopy ;; memprocs) job=extract ;; *) exit 2 ;; esac
group_setup "$job"
unit_ctl="${UNIT_SYSTEMCTL:-systemctl}"
artifact=""
if [[ "$mode" == io ]]; then artifact="$target/.$job.dat"; fi
if [[ "$mode" == fill ]]; then artifact="$target/.$job.part"; fi
if [[ -n "$artifact" ]]; then boot_files+=("$artifact"); fi
revive_active() { [[ "$revive" == - ]] || "$unit_ctl" is-active --quiet "$revive.service" </dev/null; }
# The largest resident set (MiB) among the revive unit's processes, read from its control group.
revive_rss() {
  local cg pid r m=0
  cg=$("$unit_ctl" show -P ControlGroup "$revive.service" </dev/null) || return 1
  [[ -n "$cg" && -r "${UNIT_CGROUP_ROOT:-/sys/fs/cgroup}$cg/cgroup.procs" ]] || return 1
  while read -r pid; do
    r=$(awk '/^VmRSS:/{print int($2/1024)}' "/proc/$pid/status" 2>/dev/null || true)
    if [[ -n "$r" ]] && (( r > m )); then m=$r; fi
  done <"${UNIT_CGROUP_ROOT:-/sys/fs/cgroup}$cg/cgroup.procs"
  echo "$m"
}
# Bring the unit back within bounds (module docstring): revive_up <systemctl> <unit> <stop wait> <total>. Prints each
# step with its second; returns 1 when the unit is not active within <total> seconds or its start fails.
revive_up() {
  local ctl=$1 svc=$2.service stop_wait=$3 total=$4 t0=$SECONDS since="" killed=0 started=0 st
  while :; do
    st=$("$ctl" is-active "$svc" </dev/null 2>/dev/null || true)
    case "$st" in
      active) echo "revive $svc: active at +$(( SECONDS - t0 ))s"; return 0 ;;
      deactivating)
        [[ -n "$since" ]] || since=$SECONDS
        if (( ! killed && SECONDS - since >= stop_wait )); then
          echo "revive $svc: still deactivating after $(( SECONDS - since ))s, systemctl kill -s SIGKILL at +$(( SECONDS - t0 ))s"
          "$ctl" kill -s SIGKILL "$svc" </dev/null || true
          killed=1
        fi ;;
      inactive|failed)
        if (( started )); then echo "revive $svc: start failed ($st) at +$(( SECONDS - t0 ))s" >&2; return 1; fi
        echo "revive $svc: $st, reset-failed and start at +$(( SECONDS - t0 ))s"
        "$ctl" reset-failed "$svc" </dev/null 2>/dev/null || true
        "$ctl" start --no-block "$svc" </dev/null || { echo "revive $svc: start refused at +$(( SECONDS - t0 ))s" >&2; return 1; }
        started=1 ;;
    esac
    if (( SECONDS - t0 >= total )); then echo "revive $svc: not active after ${total}s (last $st)" >&2; return 1; fi
    sleep 1
  done
}
revive_stop_wait="${REVIVE_STOP_WAIT:-60}"
revive_total="${REVIVE_TOTAL:-180}"
revive() { if [[ "$revive" != - ]]; then revive_up "$unit_ctl" "$revive" "$revive_stop_wait" "$revive_total"; fi; }
state() {
  if group_live || [[ -e "$pgidfile" ]]; then echo ours; return 0; fi
  if [[ -n "$artifact" && -e "$artifact" ]]; then echo ours; return 0; fi
  if ! revive_active; then echo "$revive-down"; return 0; fi
  echo default
}
calculate() {
  local blocks used avail
  read -r blocks used avail < <(df -Pk "$target" | awk 'NR==2{print $2,$3,$4}')
  desired=$(( blocks * watermark / 100 - used ))
  if (( desired > avail - reserve * 1024 )); then desired=$(( avail - reserve * 1024 )); fi
  if (( desired > mib * 1024 )); then desired=$(( mib * 1024 )); fi
  (( desired > 0 )) || { echo "the file system is already at or past the watermark" >&2; return 1; }
}
check() {
  command -v setsid >/dev/null
  case "$mode" in
    cpu) command -v yes >/dev/null ;;
    memhog) command -v python3 >/dev/null
      (( $(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo) > mib )) || { echo "not enough available memory" >&2; exit 1; } ;;
    # More than is free is the point; what must hold is that the job alone fits (1 GiB left for the system once
    # the service is down) and that each worker is at most half the service's largest process, so the kernel
    # picks the service and not the job.
    memprocs) command -v python3 >/dev/null
      (( mib + 1024 <= $(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo) )) || { echo "the job does not fit in memory" >&2; exit 1; }
      revive_active || { echo "$revive is not active" >&2; exit 1; }
      local largest; largest=$(revive_rss) || { echo "cannot read the processes of $revive" >&2; exit 1; }
      (( largest >= 2 * mib / workers )) || { echo "$revive's largest process ($largest MiB) is not twice a worker" >&2; exit 1; } ;;
    io) command -v dd >/dev/null; [[ -d "$target" && ! -L "$target" ]]
      (( $(df -Pk "$target" | awk 'NR==2{print $4}') > (mib + 1024) * 1024 )) || { echo "not enough room under $target" >&2; exit 1; } ;;
    fill) command -v fallocate >/dev/null; [[ -d "$target" && ! -L "$target" ]]; calculate ;;
  esac
}
undo_body() {
  group_undo
  if [[ -n "$artifact" ]]; then printf 'rm -f %q\n' "$artifact"; fi
  if [[ "$revive" != - ]]; then
    declare -f revive_up
    printf 'revive_up %q %q %q %q\n' "$unit_ctl" "$revive" "$revive_stop_wait" "$revive_total"
  fi
}
apply() {
  case "$mode" in
    cpu) group_start "for _ in \$(seq 1 $workers); do yes >/dev/null 2>&1 & done; sleep $runtime" ;;
    memhog) group_start "python3 -c 'import sys,time
buf=bytearray(int(sys.argv[1])*1048576)
for off in range(0,len(buf),1048576): buf[off]=1
time.sleep(int(sys.argv[2]))' $mib $runtime" ;;
    memprocs) group_start "for _ in \$(seq 1 $workers); do python3 -c 'import sys,time
n=int(sys.argv[1]); held=[bytearray(min(64,n-i)*1048576) for i in range(0,n,64)]
time.sleep(int(sys.argv[2]))' $(( mib / workers )) $runtime & done; wait" ;;
    io) group_start "end=\$(( SECONDS + $runtime )); while (( SECONDS < end )); do dd if=/dev/zero of=$(printf %q "$artifact") bs=1M count=$mib oflag=direct conv=notrunc status=none || break; done" ;;
    fill) calculate; fallocate -l "${desired}K" "$artifact"; sync -f "$artifact" ;;
  esac
}
remove() {
  group_stop
  if [[ -n "$artifact" ]]; then rm -f -- "$artifact"; sync -f "$target" 2>/dev/null || true; fi
  revive
}
'''


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    validate(plan["scenario"]["id"], p, {})
    args = check_parameters(p)
    vm.check_vm_location(instance, p)
    return vm.vm_invocation(plan, action, p, NODE_BODY, args)


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
