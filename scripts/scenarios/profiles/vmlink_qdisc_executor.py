#!/usr/bin/env python3
"""vmlink.qdisc: delay, loss or a rate limit on the hypervisor side of a VM NIC (§6-1).

What it changes: on 66, the transmit queue of one tap device (the VM NIC's host end, so the
direction toward the guest) becomes one netem qdisc with delay, jitter, random loss and an optional
rate. Only the taps behind the router's web, WAS and DB side NICs and the servers' business NICs
(vm_executor_common.HV_LINKS). The queue sits outside both the guest and the router, so neither shows
it in an interface counter, and the router's queue drop report (decision 4) never sees it.

Same path as vmlink.state: one request to the forced-command helper; the root helper checks the table
again, arms the U8 guard on 66 before the qdisc goes in, and deletes exactly its own root qdisc when
the deadline passes. A tap is created again when its VM restarts, so 66 needs no boot rule.

The executor is the means only; until a scenario adds an exact contract every scenario is refused.
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance, with_guard_values
import vm_executor_common as vm

PROFILE_ID = "vmlink.qdisc"
CONTRACTS: dict[str, dict[str, Any]] = {}
KEYS = {"host", "node", "vmid", "net", "delay_ms", "jitter_ms", "loss_percent", "rate_kbit"}


def check_parameters(p: dict[str, Any]) -> list[str]:
    vm.exact_keys(p, KEYS, PROFILE_ID)
    if p["host"] != vm.HYPERVISOR["host"] or p["node"] != vm.HYPERVISOR["node"]:
        raise ExecutorError("vmlink.qdisc runs on the hypervisor")
    tap = vm.hv_link(p["vmid"], p["net"])
    delay = vm.integer(p["delay_ms"], "delay_ms", 0, 2000)
    jitter = vm.integer(p["jitter_ms"], "jitter_ms", 0, delay)
    loss = vm.percent(p["loss_percent"], "loss_percent")
    rate = vm.integer(p["rate_kbit"], "rate_kbit", 0, 1_000_000)
    if delay == 0 and loss == "0" and rate == 0:
        raise ExecutorError("a link change needs delay, loss or rate")
    return [tap, str(delay), str(jitter), loss, str(rate)]


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    vm.contract_lookup(CONTRACTS, scenario_id, params, profile, "link queue")
    check_parameters(params)


DISPATCH = br'''tap="${node_args[0]}"; spec="${node_args[*]:1}"
case "$action" in
  preflight) hv_call "qdisc-check $tap" ;;
  run) need_hv_master; hv_call "qdisc-apply $tap $spec" ;;
  cleanup) hv_call "qdisc-cleanup $tap" ;;
  recovery) s=$(hv_call "qdisc-state $tap"); [[ "$s" == default ]] ;;
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
