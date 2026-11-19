#!/usr/bin/env python3
"""router.qdisc: the router's transmit queue on one downlink (docs/spec-testbed-vm-3tier.md §6-1).

What it changes: the root qdisc of one of the router's web, WAS or DB side NICs (ens19, ens20, ens21),
only while that root is the kernel's default. Two kinds, kept apart because they leave different
traces (decision 4):

- rate: a tbf queue that holds the link to a rate. When the queue overflows it drops, and the router's
  ifOutDiscards helper (civil-petition/deploy/router/ifdiscards.py) adds those drops to the interface
  counter, as an outgoing queue on real equipment would.
- netem: delay, jitter and random loss. The helper does not count netem drops (random loss shows on
  real equipment as receive errors on the far side, not as transmit discards).

The upstream NIC (ens18, the router's own monitoring path) and the management NIC (ens22) are never
changed (§3-5 rule 2); the executor refuses them before connecting and the router refuses them again.

Self-recovery (U8): the router arms the deadline guard with an undo that deletes exactly this root
qdisc before the qdisc goes in. A qdisc ends with a reboot, so the boot rule removes only the guard's
own files.

The executor is the means only. A scenario enables it by adding an exact contract below and the same
parameters to the registry; until then every scenario is refused (scenarios are designed with the
user, design §7).
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, profile_instance
import vm_executor_common as vm

PROFILE_ID = "router.qdisc"
CONTRACTS: dict[str, dict[str, Any]] = {}
KEYS = {"host", "node", "device", "kind", "delay_ms", "jitter_ms", "loss_percent", "rate_kbit", "burst_kb", "latency_ms"}
ROOT_HANDLE = "1a7e"


def check_parameters(p: dict[str, Any]) -> list[str]:
    vm.exact_keys(p, KEYS, PROFILE_ID)
    if vm.vm_named(p, ("router",)) != vm.ROUTER:
        raise ExecutorError("router.qdisc runs on the router")
    dev = vm.interface_allowed(vm.ROUTER, p["device"])
    delay = vm.integer(p["delay_ms"], "delay_ms", 0, 2000)
    jitter = vm.integer(p["jitter_ms"], "jitter_ms", 0, delay)
    loss = vm.percent(p["loss_percent"], "loss_percent")
    rate = vm.integer(p["rate_kbit"], "rate_kbit", 0, 1_000_000)
    burst = vm.integer(p["burst_kb"], "burst_kb", 0, 4096)
    latency = vm.integer(p["latency_ms"], "latency_ms", 0, 5000)
    if p["kind"] == "rate":
        if rate == 0 or burst == 0 or latency == 0 or delay or jitter or loss != "0":
            raise ExecutorError("a rate queue takes rate_kbit, burst_kb and latency_ms only")
    elif p["kind"] == "netem":
        if rate or burst or latency:
            raise ExecutorError("netem takes delay, jitter and loss only")
        if delay == 0 and loss == "0":
            raise ExecutorError("netem needs delay or loss")
    else:
        raise ExecutorError("kind must be rate or netem")
    return [dev, p["kind"], str(delay), str(jitter), loss, str(rate), str(burst), str(latency)]


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    vm.contract_lookup(CONTRACTS, scenario_id, params, profile, "router queue")
    check_parameters(params)


# Runs as root on the router. Line 1 of stdin: set -- <act> <device> <kind> <delay> <jitter> <loss>
# <rate> <burst> <latency>; line 2: the guard's deadline and hold.
NODE_BODY = br'''act="$1"; dev="$2"; kind="$3"; delay="$4"; jitter="$5"; loss="$6"; rate="$7"; burst="$8"; latency="$9"
job="queue-${dev}"
root_handle=1a7e
module_dir="${LINK_MODULE_DIR:-/sys/module}"
case "$dev" in ens19|ens20|ens21) ;; *) echo "$dev is not a downlink" >&2; exit 3 ;; esac
state() {
  local r
  if ! ip link show dev "$dev" >/dev/null 2>&1; then echo absent; return 0; fi
  r=$(tc qdisc show dev "$dev" root)
  case "$r" in
    "qdisc tbf $root_handle: root"*|"qdisc netem $root_handle: root"*) echo ours ;;
    "qdisc "*" 0: root"*|"") echo default ;;
    *) echo foreign ;;
  esac
}
check() {
  command -v tc >/dev/null
  ip link show dev "$dev" >/dev/null 2>&1 || { echo "no $dev" >&2; exit 1; }
  # Both modules print a line to the kernel log when they load (forwarded to 119); T2 loads them.
  local m=sch_netem
  if [[ "$kind" == rate ]]; then m=sch_tbf; fi
  [[ -d "$module_dir/$m" ]] || { echo "$m is not loaded; load it outside a recording window" >&2; exit 1; }
}
undo_body() {
  printf 'r=$(tc qdisc show dev %q root 2>/dev/null || true); if [[ "$r" == "qdisc tbf %s: root"* || "$r" == "qdisc netem %s: root"* ]]; then tc qdisc del dev %q root; fi\n' \
    "$dev" "$root_handle" "$root_handle" "$dev"
}
apply() {
  if [[ "$kind" == rate ]]; then
    tc qdisc add dev "$dev" root handle "$root_handle": tbf rate "${rate}kbit" burst "${burst}kb" latency "${latency}ms"
  else
    local spec=(limit 100000)
    if (( delay > 0 )); then spec+=(delay "${delay}ms"); if (( jitter > 0 )); then spec+=("${jitter}ms"); fi; fi
    if [[ "$loss" != 0 ]]; then spec+=(loss "${loss}%"); fi
    tc qdisc add dev "$dev" root handle "$root_handle": netem "${spec[@]}"
  fi
}
remove() { if [[ "$(state)" == ours ]]; then tc qdisc del dev "$dev" root; fi; }
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
