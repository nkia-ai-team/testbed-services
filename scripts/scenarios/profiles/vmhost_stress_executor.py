#!/usr/bin/env python3
"""vmhost.stress: CPU, memory, disk IO or disk space pressure on one server VM (§6-1).

The VM counterpart of host.stress, with the tools a server VM has (no stress-ng or fio there):

- cpu: N `yes` busy loops in one process group for a bounded time.
- memhog: one python3 process allocates and touches N MiB, then holds it.
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

The executor is the means only; until a scenario adds an exact contract every scenario is refused.
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, profile_instance
import vm_executor_common as vm

PROFILE_ID = "vmhost.stress"
CONTRACTS: dict[str, dict[str, Any]] = {}
KEYS = {"host", "node", "mode", "cpu_workers", "mib", "target_dir", "watermark_percent", "reserve_mib", "runtime_seconds"}
JOBS = {"cpu": "burnin", "memhog": "memhold", "io": "iobench", "fill": "bulkcopy"}


def check_parameters(p: dict[str, Any]) -> list[str]:
    vm.exact_keys(p, KEYS, PROFILE_ID)
    vm.vm_named(p, ("web", "was", "db"))
    mode = p["mode"]
    if mode not in JOBS:
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


# set -- <act> <mode> <workers> <mib> <target dir> <watermark> <reserve> <runtime>
NODE_BODY = vm.GROUP_FUNCTIONS + br'''act="$1"; mode="$2"; workers="$3"; mib="$4"; target="$5"; watermark="$6"; reserve="$7"; runtime="$8"
case "$mode" in cpu) job=burnin ;; memhog) job=memhold ;; io) job=iobench ;; fill) job=bulkcopy ;; *) exit 2 ;; esac
group_setup "$job"
artifact=""
if [[ "$mode" == io ]]; then artifact="$target/.$job.dat"; fi
if [[ "$mode" == fill ]]; then artifact="$target/.$job.part"; fi
if [[ -n "$artifact" ]]; then boot_files+=("$artifact"); fi
state() {
  if group_live || [[ -e "$pgidfile" ]]; then echo ours; return 0; fi
  if [[ -n "$artifact" && -e "$artifact" ]]; then echo ours; return 0; fi
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
    io) command -v dd >/dev/null; [[ -d "$target" && ! -L "$target" ]]
      (( $(df -Pk "$target" | awk 'NR==2{print $4}') > (mib + 1024) * 1024 )) || { echo "not enough room under $target" >&2; exit 1; } ;;
    fill) command -v fallocate >/dev/null; [[ -d "$target" && ! -L "$target" ]]; calculate ;;
  esac
}
undo_body() {
  group_undo
  if [[ -n "$artifact" ]]; then printf 'rm -f %q\n' "$artifact"; fi
}
apply() {
  case "$mode" in
    cpu) group_start "for _ in \$(seq 1 $workers); do yes >/dev/null 2>&1 & done; sleep $runtime" ;;
    memhog) group_start "python3 -c 'import sys,time
buf=bytearray(int(sys.argv[1])*1048576)
for off in range(0,len(buf),1048576): buf[off]=1
time.sleep(int(sys.argv[2]))' $mib $runtime" ;;
    io) group_start "end=\$(( SECONDS + $runtime )); while (( SECONDS < end )); do dd if=/dev/zero of=$(printf %q "$artifact") bs=1M count=$mib oflag=direct conv=notrunc status=none || break; done" ;;
    fill) calculate; fallocate -l "${desired}K" "$artifact"; sync -f "$artifact" ;;
  esac
}
remove() {
  group_stop
  if [[ -n "$artifact" ]]; then rm -f -- "$artifact"; sync -f "$target" 2>/dev/null || true; fi
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
