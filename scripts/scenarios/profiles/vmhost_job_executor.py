#!/usr/bin/env python3
"""vmhost.job: an operator's heavy job on one server VM (docs/spec-testbed-vm-3tier.md §6-1).

The VM counterpart of host.job: a named transient unit (the contract's unit name and description, an
operator's plain words such as a nightly archive) compresses a directory into an archive file with
tar and gzip at a nice level, bounded by a run time. Its start and stop lines and its processes reach
119 as a real job's would; the executor's own connection, sudo and guard lines do not (§6-2).

Self-recovery (U8): armed before the unit starts, with an undo that stops exactly this unit and
removes its archive; the archive is listed for removal at the next boot.

Mode memload (its own keys; the archive job above keeps its keys and arguments exactly): the named unit is a python3
job that loads data into memory on a server without swap. It grows at ramp_mib_s and keeps the server's file cache
(Active(file) + Inactive(file)) between file_floor_mib and file_ceiling_mib + 16 MiB: it takes more while there is more
and hands a 16 MiB piece back when the cache falls under the floor (other processes growing). What the cache can no
longer hold, the server reads back from disk again and again (page cache thrashing): program code, logs and data
files are evicted and faulted in over and over, so the server's processes (the database, sshd, the SMS agent) stall on
disk reads while the kernel's OOM killer does not fire, because evicting file pages keeps counting as progress. The
job locks its own pages (mlockall, as root) so it keeps its band while everything else stalls. The server stops
answering, and no executor call reaches it. So the cleanup does not go through the server: it is what a person does
when a server neither answers nor dies, a reset from the hypervisor. Before the job starts, 66 holds the VM (vm-hold,
66's own U8 guard: if no cleanup comes by the deadline, 66 resets the VM itself). Cleanup has 66 reset the VM (QMP
system_reset, no Proxmox task line), drops the WAS servers' connections to the reset server's database port (they
wait on reads the reset server will never answer, with no read timeout and no keepalive in the pool, and would
otherwise stay until Tomcat restarts), waits for the server to come back over the management network, and checks that
the job is gone and the role's service is up again (PostgreSQL: active and pg_isready, after its crash recovery). Only
the T testbed has the 66 helper, and only a database server is reset this way (the peers are its clients).

The executor is the means only; a scenario runs only with its exact contract below.

Contracts:
- F83-R (2026-11-28): db01 (8 GB, no swap, PostgreSQL shared_buffers 2 GB) runs a one-off data job (unit
  petition-export, "Yearly petition statistics export") that ramps at 24 MiB/s and keeps the file cache at 32 to 64
  MiB; the server thrashes and stops answering (SMS reports, ssh, PostgreSQL logins past the application's 3 s limit)
  until the cleanup resets VM 205 from 66.
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance, with_guard_values
import vm_executor_common as vm

PROFILE_ID = "vmhost.job"
CONTRACTS: dict[str, dict[str, Any]] = {
    "F83-R": {"host": "192.168.127.41", "node": "db01", "mode": "memload", "unit": "petition-export",
              "description": "Yearly petition statistics export", "file_floor_mib": 32, "file_ceiling_mib": 48,
              "ramp_mib_s": 24, "runtime_seconds": 1500, "vmid": 205},
}
KEYS = {"host", "node", "unit", "description", "source_dir", "output_dir", "level", "nice", "runtime_seconds", "min_free_mib"}
# memload takes its own keys (the archive job keeps its own exactly): the file cache band, the ramp, the VM 66 resets.
MEM_KEYS = {"host", "node", "mode", "unit", "description", "file_floor_mib", "file_ceiling_mib", "ramp_mib_s",
            "runtime_seconds", "vmid"}
# The role's service that must be back after the reset (memload resets only a database server).
MEM_SERVICES = {"db": "postgresql@16-main"}


def _path(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.startswith("/") or ".." in value or any(c.isspace() for c in value):
        raise ExecutorError(f"{field} must be an absolute path without spaces")
    return value


def check_mem_parameters(p: dict[str, Any]) -> list[str]:
    vm.exact_keys(p, MEM_KEYS, PROFILE_ID)
    node = vm.vm_named(p, tuple(MEM_SERVICES))
    tb = vm.testbed_of(p)
    if tb is not vm.T_TESTBED:
        raise ExecutorError("memload is reset from the 66 helper, which only the T testbed has")
    if p["vmid"] != tb.vms[node]["vmid"]:
        raise ExecutorError(f"vmid must be {node}'s VM ({tb.vms[node]['vmid']})")
    unit = vm.neutral_name(p["unit"], "unit")
    description = vm.description(p["description"])
    # The floor may go down to 8 MiB: a server whose hot file pages are few needs a low floor to thrash at all, and the
    # kernel's OOM killer comes only when the file pages are nearly gone (design sheet F83-R 9-6). Below 8 MiB the job
    # would race the kernel's own reserve.
    floor = vm.integer(p["file_floor_mib"], "file_floor_mib", 8, 1024)
    ceiling = vm.integer(p["file_ceiling_mib"], "file_ceiling_mib", floor + 16, 2048)
    ramp = vm.integer(p["ramp_mib_s"], "ramp_mib_s", 4, 256)
    runtime = vm.integer(p["runtime_seconds"], "runtime_seconds", 300, 3600)
    peers = ",".join(tb.vms[name]["mgmt"] for name in tb.servers if tb.vms[name]["role"] == "was")
    port = str(vm.inventory()["app"]["db_port"])
    role = tb.vms[node]["role"]
    # Node words first ($2 .. $8), then what only the runner side reads (the VM id, the WAS servers' management
    # addresses, the address and port their connections go to).
    return [unit, description, str(floor), str(ceiling), str(ramp), str(runtime), MEM_SERVICES[role],
            str(p["vmid"]), peers, tb.business[node], port]


def check_parameters(p: dict[str, Any]) -> list[str]:
    if isinstance(p, dict) and p.get("mode") == "memload":
        return check_mem_parameters(p)
    vm.exact_keys(p, KEYS, PROFILE_ID)
    vm.vm_named(p, ("web", "was", "db"))
    unit = vm.neutral_name(p["unit"], "unit")
    description = vm.description(p["description"])
    return [unit, description, _path(p["source_dir"], "source_dir"), _path(p["output_dir"], "output_dir"),
            str(vm.integer(p["level"], "level", 1, 9)), str(vm.integer(p["nice"], "nice", 0, 19)),
            str(vm.integer(p["runtime_seconds"], "runtime_seconds", 60, 1800)),
            str(vm.integer(p["min_free_mib"], "min_free_mib", 1024, 1_048_576))]


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    vm.contract_lookup(CONTRACTS, scenario_id, params, profile, "server job")
    check_parameters(params)


# set -- <act> <unit> <description> <source dir> <output dir> <level> <nice> <runtime> <min free MiB>
NODE_BODY = vm.UNIT_FUNCTIONS + br'''act="$1"; unit="$2"; description="$3"; src="$4"; out="$5"; level="$6"; nice="$7"; runtime="$8"; min_free="$9"
job="$unit"
archive="$out/$unit.tar.gz"
boot_files=("$archive")
state() {
  if unit_active || [[ -e "$archive" ]]; then echo ours; return 0; fi
  echo default
}
check() {
  command -v tar >/dev/null; command -v gzip >/dev/null
  [[ -d "$src" && ! -L "$src" && -d "$out" && ! -L "$out" ]] || { echo "source or output directory missing" >&2; exit 1; }
  (( $(df -Pk "$out" | awk 'NR==2{print int($4/1024)}') > min_free )) || { echo "less than $min_free MiB free under $out" >&2; exit 1; }
  if unit_active; then echo "$unit is already running" >&2; exit 1; fi
}
undo_body() { unit_undo; printf 'rm -f %q\n' "$archive"; }
apply() {
  unit_start --property=Nice="$nice" --property=IOSchedulingClass=best-effort \
    /usr/bin/timeout "$runtime" /bin/sh -c 'tar -C "$1" -cf - . | gzip -"$2" >"$3"' archive "$src" "$level" "$archive"
  # A small source can be archived before the first look; the archive then stands for the job.
  local _
  for _ in $(seq 1 25); do if unit_active || [[ -s "$archive" ]]; then return 0; fi; sleep 0.2; done
  unit_active || [[ -s "$archive" ]]
}
remove() { unit_stop; rm -f -- "$archive"; }
'''

# The memload job itself (python3, root, in the named unit). systemd-run passes no caller environment, so on the server
# it reads /proc/meminfo; OPSJOB_MEMINFO is the test sandbox's stand-in (its fake systemd-run keeps the environment).
MEM_JOB = br'''import ctypes, os, sys, time
lo, hi, ramp, runtime = (int(v) for v in sys.argv[1:5])
ctypes.CDLL(None, use_errno=True).mlockall(3)
src = os.environ.get("OPSJOB_MEMINFO", "/proc/meminfo")
def cache_mib():
    total = 0
    for line in open(src):
        if line.startswith(("Active(file):", "Inactive(file):")):
            total += int(line.split()[1])
    return total >> 10
held = []
step = 16
end = time.monotonic() + runtime
while time.monotonic() < end:
    f = cache_mib()
    if f > hi + step:
        b = bytearray(step << 20)
        for off in range(0, len(b), 4096):
            b[off] = 1
        held.append(b)
        del b
        time.sleep(step / ramp)
    elif f < lo and held:
        held.pop()
        time.sleep(0.25)
    else:
        time.sleep(0.25)
'''

# set -- <act> <unit> <description> <floor MiB> <ceiling MiB> <ramp MiB/s> <runtime s> <service> [<runner-only words>]
MEM_NODE_BODY = vm.UNIT_FUNCTIONS + br'''act="$1"; unit="$2"; description="$3"; floor="$4"; ceiling="$5"; ramp="$6"; runtime="$7"; service="$8"
job="$unit"
meminfo="${OPSJOB_MEMINFO:-/proc/meminfo}"
read -r -d '' job_code <<'MEM_JOB' || true
''' + MEM_JOB + br'''MEM_JOB
cache_mib() { awk '/^(Active|Inactive)\(file\):/{s+=$2} END{print int(s/1024)}' "$meminfo"; }
state() {
  if unit_active; then echo ours; return 0; fi
  echo default
}
check() {
  command -v python3 >/dev/null
  if unit_active; then echo "$unit is already running" >&2; exit 1; fi
  # Sized for a server without swap: with swap the kernel would page the job's memory out instead.
  (( $(awk '/^SwapTotal:/{print $2}' "$meminfo") == 0 )) || { echo "the server has swap" >&2; exit 1; }
  (( $(cache_mib) > ceiling + 1024 )) || { echo "less than $(( ceiling + 1024 )) MiB of file cache to take" >&2; exit 1; }
  # MGLRU's thrashing guard (min_ttl_ms above 0) makes the kernel call its OOM killer instead of thrashing: then the
  # job would end like an OOM kill, not a hang. Refuse unless it is off or absent; say the kernel's reserve too.
  local ttl=0 ttl_file="${OPSJOB_LRU_GEN_DIR:-/sys/kernel/mm/lru_gen}/min_ttl_ms"
  if [[ -r "$ttl_file" ]]; then ttl=$(<"$ttl_file"); fi
  [[ "$ttl" == 0 ]] || { echo "lru_gen min_ttl_ms is $ttl (the kernel would OOM-kill instead of thrashing)" >&2; exit 1; }
  echo "lru_gen min_ttl_ms ${ttl}, min_free_kbytes $(cat "${OPSJOB_PROC_SYS:-/proc/sys}/vm/min_free_kbytes" 2>/dev/null || echo unknown)" >&2
  "$unit_ctl" is-active --quiet "$service.service" </dev/null || { echo "$service is not active" >&2; exit 1; }
}
undo_body() { unit_undo; }
apply() {
  unit_start --property=RuntimeMaxSec="$(( runtime + 60 ))" /usr/bin/python3 -c "$job_code" "$floor" "$ceiling" "$ramp" "$runtime"
  local _
  for _ in $(seq 1 25); do if unit_active; then return 0; fi; sleep 0.2; done
  unit_active
}
ready() {
  if [[ "$service" == postgresql@* ]]; then "${OPSJOB_RUNUSER:-runuser}" -u postgres -- pg_isready -q </dev/null; fi
}
# After the reset the job went with the old boot; the service comes up by itself (PostgreSQL after its crash recovery).
remove() {
  local t0=$SECONDS
  unit_stop
  while ! { "$unit_ctl" is-active --quiet "$service.service" </dev/null && ready; }; do
    if (( SECONDS - t0 >= ${OPSJOB_SERVICE_WAIT:-240} )); then echo "$service is not ready after $(( SECONDS - t0 ))s" >&2; return 1; fi
    sleep 2
  done
  echo "$service ready at +$(( SECONDS - t0 ))s" >&2
}
'''

# memload's runner side: 66 holds the VM before the job starts and resets it in the cleanup (module docstring).
MEM_DISPATCH = br'''vmid="${node_args[7]}"; peers="${node_args[8]}"; addr="${node_args[9]}"; port="${node_args[10]}"
# The WAS servers wait on reads the reset server will never answer (no read timeout, no keepalive): drop exactly their
# connections to its database port. A mark on the runner side ($owed, holding the run's start in epoch seconds) is
# written before 66 holds the VM and stays until the drop has worked, so a reset whose answer was lost on the way back,
# or a reset 66's guard did by itself at its deadline, still gets its drop at the next cleanup.
# strict: right after the reset the server is still booting, so no established connection to it may be left (new
# attempts get no answer or a reset). loose: the server may be back with the pool's new connections; they are dropped
# too (the pool opens new ones) and only the drop itself is checked. Each WAS also says whether its kernel can drop
# sockets at all (INET_DIAG_DESTROY); a kernel that cannot is said in the output.
owed="$ctl/memload-drop-$vmid"
drop_stale() {
  local strict=$1 p out destroy left bad=0
  for p in ${peers//,/ }; do
    if ! out=$(printf 'set -euo pipefail\nc="${OPSJOB_KERNEL_CONFIG:-/boot/config-$(uname -r)}"\nif grep -qx CONFIG_INET_DIAG_DESTROY=y "$c" 2>/dev/null; then echo destroy=yes; elif [[ -r "$c" ]]; then echo destroy=no; else echo destroy=unknown; fi\nss -K -tn dst %s dport = :%s >/dev/null\nss -Htn state established dst %s dport = :%s | wc -l\n' \
        "$addr" "$port" "$addr" "$port" \
        | timeout --foreground -k 5 40 "${ssh_vm[@]}" -o ControlMaster=no "opsjob@$p" sudo -n bash -s); then
      echo "could not drop $p's connections to $addr:$port" >&2; bad=1; continue
    fi
    destroy=$(sed -n 's/^destroy=//p' <<<"$out"); left=$(tail -n 1 <<<"$out"); left=${left//[[:space:]]/}
    if [[ "$destroy" != yes ]]; then echo "$p: socket destroy (INET_DIAG_DESTROY) is $destroy; its connections to $addr:$port may stay" >&2; fi
    if [[ "$strict" == strict && "$left" != 0 ]]; then
      echo "$p still has $left connection(s) to $addr:$port after the drop" >&2; bad=1
    fi
  done
  return "$bad"
}
# The server's boot time (epoch seconds), over the executor connection.
server_boot() {
  printf 'set -euo pipefail\nawk %q "${OPSJOB_PROC_STAT:-/proc/stat}"\n' '/^btime/{print $2}' \
    | timeout --foreground -k 5 40 "${ssh_vm[@]}" -o ControlMaster=no "opsjob@$host" sudo -n bash -s
}
# The server back after the reset: the executor connection answers and the job is not running.
wait_back() {
  local t0=$SECONDS s
  while (( SECONDS - t0 < ${MEM_BACK_WAIT:-420} )); do
    if s=$(node_call state 2>/dev/null) && [[ "$s" == default ]]; then echo "back at +$(( SECONDS - t0 ))s" >&2; return 0; fi
    sleep "${MEM_BACK_POLL:-5}"
  done
  echo "the server did not come back within $(( SECONDS - t0 ))s" >&2
  return 1
}
case "$action" in
  preflight) node_call check; hv_call "vm-check $vmid" >/dev/null ;;
  run) need_master; need_hv_master
    mkdir -p "$ctl"; date +%s >"$owed"
    hv_call "vm-hold $vmid"
    if ! node_call run; then
      echo "the job did not start; releasing the hypervisor hold" >&2
      node_call cleanup || true
      hv_call "vm-release $vmid" || true
      rm -f "$owed"
      exit 1
    fi ;;
  # reset: drop at once (strict) and wait for the server. none with the mark still there: the reset happened earlier
  # (its answer was lost, or 66's guard did it) or never (the run did not get far). Wait for the server, then drop only
  # when it booted after the run started (loose); a server that never rebooted keeps its connections, they are live.
  cleanup) r=$(hv_call "vm-reset $vmid")
    echo "hypervisor: $r" >&2
    if [[ "$r" == reset ]]; then
      if ! drop_stale strict; then echo "the WAS connections to $addr:$port are not dropped; cleanup again" >&2; exit 1; fi
      rm -f "$owed"
      wait_back
    elif [[ -e "$owed" ]]; then
      wait_back
      since=$(cat "$owed"); boot=$(server_boot)
      if [[ "$since" =~ ^[0-9]+$ && "$boot" =~ ^[0-9]+$ ]] && (( boot > since )); then
        echo "the server booted at $boot, after the run at $since: dropping the WAS connections left from before" >&2
        if ! drop_stale loose; then echo "the WAS connections to $addr:$port are not dropped; cleanup again" >&2; exit 1; fi
      else
        echo "the server did not reboot since the run (boot ${boot:-unknown}, run ${since:-unknown}): its connections are live, none dropped" >&2
      fi
      rm -f "$owed"
    fi
    node_call cleanup ;;
  recovery) s=$(node_call state) && [[ "$s" == default ]] && v=$(hv_call "vm-state $vmid") && [[ "$v" == free ]] ;;
  *) exit 2 ;;
esac
'''


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    validate(plan["scenario"]["id"], p, {})
    args = check_parameters(p)
    vm.check_vm_location(instance, p)
    if p.get("mode") == "memload":
        script = vm.runner_script(vm.node_program(MEM_NODE_BODY), MEM_DISPATCH, vm.testbed_of(p))
        return (kubectl_bash_argv([action, p["host"], p["node"], *args]), with_guard_values(script, plan))
    return vm.vm_invocation(plan, action, p, NODE_BODY, args)


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
