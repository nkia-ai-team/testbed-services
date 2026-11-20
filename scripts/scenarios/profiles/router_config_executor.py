#!/usr/bin/env python3
"""router.config: an operator's configuration change on the router (docs/spec-testbed-vm-3tier.md §6-1).

What it changes, in the operator's own way (design §6-1 "변경 원인 주입 위치", §6-2 운영자 경로, Q35): the
operator logs in with ssh from the operator terminal address (key, pty, a session that opens and closes),
writes an nftables file in its home and loads it with `sudo nft -f`, adds or blackholes a route with
`sudo ip route add`, or sets a conntrack sysctl with `sudo sysctl -w`. The login and those sudo lines
(TTY=pts/N) are the change's own trace and reach 119 as a real operator's would. Nothing of the executor
does: its connection, sudo and the guard are diverted by the router's first rsyslog rule (§6-2); its new
connections stay at zero (Q7c is judged by source address, the operator's address is a cause trace).

Content guard (§3-5 rule 6): the change is not tied to an interface, so every item is checked before
anything connects. A rule may name only addresses inside the business networks (never the router's
upstream address), no monitoring, management or tunnel port, and no protected interface; a route may
not touch the tunnel, upstream, default or management routes; only conntrack sysctls may change. The
table the executor builds starts every chain with fixed lines that accept the protected flows
(tunnel, management, the router's upstream address), so a broad rule below them cannot catch those.
Right after the change, 66 pings the router's upstream address through a tunnel source and the
executor reads the router over the management connection; if either fails the change is undone on the
spot and the run fails.

Self-recovery (U8): armed before the change, with an undo that deletes exactly this table, these
routes and puts the sysctls back. The file in the operator's home is removed at the next boot.

The executor is the means only; until a scenario adds an exact contract every scenario is refused.
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, profile_instance
import vm_executor_common as vm

PROFILE_ID = "router.config"
CONTRACTS: dict[str, dict[str, Any]] = {}
KEYS = {"host", "node", "table", "rules", "routes", "sysctls", "log_prefix"}
SYSCTLS = {
    "net.netfilter.nf_conntrack_max": (1024, 1_048_576),
    "net.netfilter.nf_conntrack_tcp_timeout_established": (30, 432_000),
    "net.netfilter.nf_conntrack_udp_timeout": (5, 3600),
}
ROUTE_TYPES = ("blackhole", "unreachable", "via")


def check_routes(routes: Any) -> str:
    if not isinstance(routes, list) or len(routes) > 4:
        raise ExecutorError("routes must be a list of at most 4 routes")
    tokens = []
    for route in routes:
        if not isinstance(route, dict) or set(route) - {"dst", "type", "via"} or route.get("type") not in ROUTE_TYPES:
            raise ExecutorError("a route names dst, type (blackhole, unreachable or via) and via for type via")
        dst = vm.routable_destination(route.get("dst"), "route dst")
        via = "-"
        if route["type"] == "via":
            via = str(vm._network(route.get("via"), "route via").network_address)
            vm.business_network(via + "/32", "route via")
        elif "via" in route:
            raise ExecutorError("only a via route names via")
        tokens.append(f"{dst}={route['type']}={via}")
    return ",".join(tokens) or "-"


def check_sysctls(values: Any) -> str:
    if not isinstance(values, dict) or len(values) > 3:
        raise ExecutorError("sysctls must be an object of at most 3 keys")
    tokens = []
    for key, value in sorted(values.items()):
        if key not in SYSCTLS:
            raise ExecutorError(f"sysctl {key} is not one a router change may set")
        low, high = SYSCTLS[key]
        tokens.append(f"{key}={vm.integer(value, key, low, high)}")
    return ",".join(tokens) or "-"


def check_parameters(p: dict[str, Any]) -> list[str]:
    vm.exact_keys(p, KEYS, PROFILE_ID)
    if vm.vm_named(p, ("router",)) != vm.ROUTER:
        raise ExecutorError("router.config runs on the router")
    table = vm.neutral_name(p["table"], "table")
    prefix = vm.log_prefix(p["log_prefix"])
    protected_ifs = (vm.ROUTER_UPLINK_IF, vm.ROUTER_MGMT_IF)
    text = "-"
    if p["rules"]:
        tokens = vm.filter_rules(p["rules"], "router", protected_ifs)
        text = vm.nft_table(table, "router", vm.ROUTER_MGMT_IF, tokens, prefix)
    routes = check_routes(p["routes"])
    sysctls = check_sysctls(p["sysctls"])
    if text == "-" and routes == "-" and sysctls == "-":
        raise ExecutorError("a router change needs rules, routes or sysctls")
    return [table, text, routes, sysctls]


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    vm.contract_lookup(CONTRACTS, scenario_id, params, profile, "router change")
    check_parameters(params)


# Right after the operator's change: 66 pings the router's upstream address through a tunnel source and the change
# reads as landed over the management connection (design 3-5 rule 6).
DISPATCH = vm.operator_dispatch(b'hv_call "probe ' + vm.ROUTER_UPLINK_ADDR.encode() + b'" && ' + vm.LANDED)


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    validate(plan["scenario"]["id"], p, {})
    args = check_parameters(p)
    vm.check_vm_location(instance, p)
    return vm.vm_invocation(plan, action, p, vm.FIREWALL_BODY, ["operator", *args], DISPATCH)


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
