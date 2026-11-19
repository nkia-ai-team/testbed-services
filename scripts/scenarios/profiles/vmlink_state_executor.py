#!/usr/bin/env python3
"""vmlink.state: a VM NIC's link goes down on the hypervisor side (docs/spec-testbed-vm-3tier.md §6-1).

What it changes: on 66, QMP set_link turns one NIC of one VM off (the guest sees carrier loss, as a
pulled cable or a failed switch port would) and back on. The QMP socket is written directly instead
of going through qm, so Proxmox keeps no task record and logs no task line (66 forwards its syslog to
119). Only the router's web, WAS and DB side NICs and the servers' business NICs may go down
(vm_executor_common.HV_LINKS); the router's upstream NIC, every management NIC and the load generator
never do (§3-5 rule 2).

The runner reaches 66 only through the forced-command account (decision 2): this executor sends one
request line to t4-hv-exec, and the root helper (civil-petition/deploy/hypervisor/t4-hv-exec-root)
checks the table again, arms the U8 guard on 66 before it changes the link, and undoes the change by
itself if cleanup never comes. A link set through QMP comes back up when the VM restarts, so 66 needs
no boot rule (§3-5 rule 5).

The executor is the means only; until a scenario adds an exact contract every scenario is refused.
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance, with_guard_values
import vm_executor_common as vm

PROFILE_ID = "vmlink.state"
CONTRACTS: dict[str, dict[str, Any]] = {}
KEYS = {"host", "node", "vmid", "net"}


def check_parameters(p: dict[str, Any]) -> list[str]:
    vm.exact_keys(p, KEYS, PROFILE_ID)
    if p["host"] != vm.HYPERVISOR["host"] or p["node"] != vm.HYPERVISOR["node"]:
        raise ExecutorError("vmlink.state runs on the hypervisor")
    vm.hv_link(p["vmid"], p["net"])
    return [str(p["vmid"]), p["net"]]


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    vm.contract_lookup(CONTRACTS, scenario_id, params, profile, "link state")
    check_parameters(params)


DISPATCH = br'''vmid="${node_args[0]}"; net="${node_args[1]}"
case "$action" in
  preflight) hv_call "link-check $vmid $net" ;;
  run) need_hv_master; hv_call "link-down $vmid $net" ;;
  cleanup) hv_call "link-cleanup $vmid $net" ;;
  recovery) s=$(hv_call "link-state $vmid $net"); [[ "$s" == up ]] ;;
  *) exit 2 ;;
esac
'''


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    validate(plan["scenario"]["id"], p, {})
    args = check_parameters(p)
    vm.check_hv_location(instance)
    return (kubectl_bash_argv([action, p["host"], p["node"], *args]),
            with_guard_values(vm.hv_runner_script(DISPATCH), plan))


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
