#!/usr/bin/env python3
"""router.resource: the router itself runs short of CPU or of connection-tracking room, or restarts
(docs/spec-testbed-vm-3tier.md §6-1, §3-5 rule 3).

- cpu: N busy loops (`yes`) in one process group on the router for a bounded time. Packets the
  router forwards and the SNMP answers it gives share those CPUs.
- conntrack: the connection-tracking table fills with entries the executor inserts through the
  conntrack tool (UDP entries between two unused business addresses, marked 0x1a7e, timeout
  runtime + 60 s). New flows through the router then find no room and the kernel logs
  "nf_conntrack: table full, dropping packet"; the router's own monitoring flows are untracked
  (router nft-monitor-notrack.nft, §3-3) so that line and the SNMP answers still reach 119.
- reboot: the router restarts. Everything behind it vanishes from 119 together with it (design §6-5:
  normal, the tunnel watch on 66 stays healthy). Nothing to undo: no guard is armed, and the runner
  reopens its management connection and waits 30 minutes before the next recording (design §6-4).

The upstream NIC's configuration is never touched (§3-5 rule 3). Self-recovery (U8) for cpu and
conntrack: armed before the change, with an undo that kills exactly this process group and deletes
exactly the marked entries. Both end with a reboot, so the boot rule removes only the guard's files.

The executor is the means only; until a scenario adds an exact contract every scenario is refused.
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, profile_instance
import vm_executor_common as vm

PROFILE_ID = "router.resource"
CONTRACTS: dict[str, dict[str, Any]] = {}
KEYS = {"host", "node", "mode", "cpu_workers", "entries", "runtime_seconds"}


def check_parameters(p: dict[str, Any]) -> list[str]:
    vm.exact_keys(p, KEYS, PROFILE_ID)
    if vm.vm_named(p, ("router",)) != vm.ROUTER:
        raise ExecutorError("router.resource runs on the router")
    mode = p["mode"]
    workers = vm.integer(p["cpu_workers"], "cpu_workers", 0, 2)
    entries = vm.integer(p["entries"], "entries", 0, 1_048_576)
    runtime = vm.integer(p["runtime_seconds"], "runtime_seconds", 0, 1800)
    if mode == "cpu":
        if workers == 0 or entries or runtime < 60:
            raise ExecutorError("cpu takes cpu_workers (1 or 2) and runtime_seconds (60 to 1800) only")
    elif mode == "conntrack":
        if workers or entries < 1024 or runtime < 60:
            raise ExecutorError("conntrack takes entries (1024 or more) and runtime_seconds (60 to 1800) only")
    elif mode == "reboot":
        if workers or entries or runtime:
            raise ExecutorError("reboot takes no other parameter")
    else:
        raise ExecutorError("mode must be cpu, conntrack or reboot")
    return [mode, str(workers), str(entries), str(runtime)]


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    vm.contract_lookup(CONTRACTS, scenario_id, params, profile, "router resource")
    check_parameters(params)


# set -- <act> <mode> <workers> <entries> <runtime>
NODE_BODY = vm.GROUP_FUNCTIONS + br'''act="$1"; mode="$2"; workers="$3"; entries="$4"; runtime="$5"
case "$mode" in cpu) job=burnin ;; conntrack) job=tablefill ;; reboot) job=restart ;; *) exit 2 ;; esac
group_setup "$job"
mark=0x1a7e
ct_ours() { [[ "$(conntrack -L -m "$mark" 2>/dev/null | wc -l)" -gt 0 ]]; }
state() {
  if [[ "$mode" == reboot ]]; then echo default; return 0; fi
  if group_live || [[ -e "$pgidfile" ]]; then echo ours; return 0; fi
  if [[ "$mode" == conntrack ]] && ct_ours; then echo ours; return 0; fi
  echo default
}
check() {
  command -v setsid >/dev/null
  case "$mode" in
    cpu) command -v yes >/dev/null ;;
    conntrack) command -v conntrack >/dev/null; [[ -r /proc/sys/net/netfilter/nf_conntrack_max ]] ;;
    reboot) command -v systemctl >/dev/null ;;
  esac
}
undo_body() {
  group_undo
  if [[ "$mode" == conntrack ]]; then printf 'conntrack -D -m %s >/dev/null 2>&1 || true\n' "$mark"; fi
}
apply() {
  case "$mode" in
    cpu) group_start "for _ in \$(seq 1 $workers); do yes >/dev/null 2>&1 & done; sleep $runtime" ;;
    # Entries between two addresses no host uses; their timeout outlives the run by a minute.
    conntrack) group_start "t=\$(( $runtime + 60 )); for i in \$(seq 1 $entries); do conntrack -I -p udp -s 192.168.124.250 -d 192.168.125.250 --sport \$(( 1024 + i % 64000 )) --dport \$(( 1024 + i / 64000 )) -t \$t -m $mark >/dev/null 2>&1 || true; done; sleep $runtime" ;;
  esac
}
remove() {
  group_stop
  if [[ "$mode" == conntrack ]]; then conntrack -D -m "$mark" >/dev/null 2>&1 || true; fi
}
if [[ "$mode" == reboot ]]; then
  # The restart is the whole change; there is nothing to undo, so nothing is armed.
  case "$act" in
    check) check ;;
    state) echo default ;;
    run) check; setsid -f bash -c 'sleep 2; systemctl reboot' </dev/null >/dev/null 2>&1 ;;
    cleanup) : ;;
    *) exit 2 ;;
  esac
  exit 0
fi
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
