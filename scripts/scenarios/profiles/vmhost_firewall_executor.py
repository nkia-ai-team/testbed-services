#!/usr/bin/env python3
"""vmhost.firewall: a host firewall rule on one server VM (docs/spec-testbed-vm-3tier.md §6-1).

The VM counterpart of host.firewall: an nftables table on the server drops (or rejects) a flow
between business networks, optionally with a rate-limited log line whose prefix the contract fixes
(the kernel line is the change's trace, as "[FW BLOCK]" is for host.firewall). It is applied by root
through the executor's connection (§6-2 diverts that connection's own lines), so no operator command
line is left; the operator path belongs to router.config and vm.service.

Content guard (§3-5 rule 6), the same as router.config: addresses inside the business networks only,
no monitoring, management or tunnel port, no management NIC; every chain starts with fixed lines that
accept the tunnel and management flows (the server's reports and syslog to 10.250.0.1, ICMP from it,
management ssh). Right after the change 66 pings the server's business address through a tunnel
source and the executor reads the server over the management connection; if either fails the change
is undone and the run fails.

Self-recovery (U8): armed before the change with an undo that deletes exactly this table. A table
ends with a reboot, so the boot rule removes only the guard's own files.

On the S testbed (spec-testbed-sms-only.md §3-6 rule 3) the protected values are S's: its management network,
and its 66 gateways (the servers' default route) as addresses a rule may not name; 66 pings the server's S business
address the same way.

The executor is the means only; until a scenario adds an exact contract every scenario is refused.
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, profile_instance
import vm_executor_common as vm

PROFILE_ID = "vmhost.firewall"
CONTRACTS: dict[str, dict[str, Any]] = {}
KEYS = {"host", "node", "table", "rules", "log_prefix"}


def check_parameters(p: dict[str, Any]) -> list[str]:
    vm.exact_keys(p, KEYS, PROFILE_ID)
    vm.vm_named(p, ("web", "was", "db"))
    tb = vm.testbed_of(p)
    table = vm.neutral_name(p["table"], "table")
    prefix = vm.log_prefix(p["log_prefix"])
    tokens = vm.filter_rules(p["rules"], "server", (vm.SERVER_MGMT_IF,), tb)
    text = vm.nft_table(table, "server", vm.SERVER_MGMT_IF, tokens, prefix, tb)
    return [table, text, "-", "-"]


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    vm.contract_lookup(CONTRACTS, scenario_id, params, profile, "host firewall")
    check_parameters(params)


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    validate(plan["scenario"]["id"], p, {})
    args = check_parameters(p)
    vm.check_vm_location(instance, p)
    dispatch = vm.CHECKED_DISPATCH.replace(b"@PROBE@", vm.testbed_of(p).business[p["node"]].encode())
    return vm.vm_invocation(plan, action, p, vm.FIREWALL_BODY, ["root", *args], dispatch)


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
