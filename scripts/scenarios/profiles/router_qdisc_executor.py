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

Mode operator-rate (added for F82-R, the parameters above stay as they were): an operator's rate limit on one downlink
through the operator path (design 6-1 change cause, 6-2 Q35). The operator logs in to the router and puts a tbf root
qdisc on the NIC with `sudo tc qdisc add dev <nic> root handle 1: tbf rate <r>kbit burst <b>kb latency <l>ms`; the sudo
COMMAND= line (TTY=pts/N) in the router's syslog is the change's own trace, and the queue's overflow drops reach the
NIC's ifOutDiscards through the router's helper (decision 4). Preflight measures what the NIC sends now (its transmit
byte counter over a few seconds) and refuses unless that is between the contract's percents of the limit (principle 9):
below, the limit holds every packet and nobody notices; far above, the queue stalls the link the way a cut cable does
and the portal's requests wait out the load generator instead of failing in the application. The guard is armed
before the operator types (its undo deletes exactly a tbf root with handle 1:); cleanup has the operator delete the
root, and the executor deletes whatever is left as root.

F82-R (Google Compute Engine 2015-03-07 rebuilt on the router): a configuration change that capped the traffic volume
let through for each VM dropped egress packets and made responses slow or time out. Here the operator caps the
router's WAS side NIC (ens20, what the router sends to was01 and was02: the web server's requests and the DB server's
answers) at a rate a little below what that NIC carries in the busy hours, so the queue overflows, packets are dropped
and retransmitted, the DB answers come late, the connection pools fill and the portal answers slowly (3 s and more) or
with 503 after the pool's 3-second wait.
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, profile_instance
import vm_executor_common as vm

PROFILE_ID = "router.qdisc"
CONTRACTS: dict[str, dict[str, Any]] = {
    "F82-R": {
        "host": "192.168.127.2",
        "node": "rt-core01",
        "mode": "operator-rate",
        "device": "ens20",
        "rate_kbit": 400,
        "burst_kb": 32,
        "latency_ms": 400,
        "min_load_pct": 102,
        "max_load_pct": 112,
    },
}
KEYS = {"host", "node", "device", "kind", "delay_ms", "jitter_ms", "loss_percent", "rate_kbit", "burst_kb", "latency_ms"}
ROOT_HANDLE = "1a7e"
OPERATOR_KEYS = {"host", "node", "mode", "device", "rate_kbit", "burst_kb", "latency_ms", "min_load_pct", "max_load_pct"}
MODES = ("operator-rate",)
# Seconds the preflight reads the NIC's transmit counter (a remote call stays within the 10 s of design 6-2).
PROBE_SECONDS = 6


def check_operator_rate(p: dict[str, Any]) -> list[str]:
    vm.exact_keys(p, OPERATOR_KEYS, PROFILE_ID)
    if vm.vm_named(p, ("router",)) != vm.ROUTER:
        raise ExecutorError("router.qdisc runs on the router")
    dev = vm.interface_allowed(vm.ROUTER, p["device"])
    rate = vm.integer(p["rate_kbit"], "rate_kbit", 64, 1_000_000)
    burst = vm.integer(p["burst_kb"], "burst_kb", 2, 4096)
    latency = vm.integer(p["latency_ms"], "latency_ms", 1, 5000)
    low = vm.integer(p["min_load_pct"], "min_load_pct", 50, 400)
    high = vm.integer(p["max_load_pct"], "max_load_pct", low, 400)
    return [dev, str(rate), str(burst), str(latency), str(low), str(high), str(PROBE_SECONDS)]


def check_parameters(p: dict[str, Any]) -> list[str]:
    if isinstance(p, dict) and "mode" in p:
        if p["mode"] not in MODES:
            raise ExecutorError(f"mode must be one of {MODES}")
        return check_operator_rate(p)
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

# Mode operator-rate. Runs as root on the router. Line 1 of stdin: set -- <act> <device> <rate kbit> <burst kb>
# <latency ms> <min load %> <max load %> <probe seconds>; line 2: the guard's deadline and hold.
OPERATOR_BODY = vm.OPERATOR_FUNCTIONS + br'''act="$1"; dev="$2"; rate="$3"; burst="$4"; latency="$5"; min_pct="$6"; max_pct="$7"; probe="$8"
job="queue-${dev}"
root_handle=1
module_dir="${LINK_MODULE_DIR:-/sys/module}"
case "$dev" in ens19|ens20|ens21) ;; *) echo "$dev is not a downlink" >&2; exit 3 ;; esac
state() {
  local r
  if ! ip link show dev "$dev" >/dev/null 2>&1; then echo absent; return 0; fi
  r=$(tc qdisc show dev "$dev" root)
  case "$r" in
    "qdisc tbf $root_handle: root"*) echo ours ;;
    "qdisc "*" 0: root"*|"") echo default ;;
    *) echo foreign ;;
  esac
}
# What the NIC sends, in bytes, from this network namespace's own counters (/proc/net/dev, field 10 is tx bytes).
tx_bytes() { sed 's/:/ /' /proc/net/dev | awk -v d="$dev" '$1 == d { print $10 }'; }
check() {
  local b0 b1 t0 t1 kbit pct
  command -v tc >/dev/null
  ip link show dev "$dev" >/dev/null 2>&1 || { echo "no $dev" >&2; exit 1; }
  [[ -d "$module_dir/sch_tbf" ]] || { echo "sch_tbf is not loaded; load it outside a recording window" >&2; exit 1; }
  if [[ "$(state)" != default ]]; then return 0; fi
  if [[ -z "$op_home" || ! -d "$op_home" ]]; then echo "no home for $operator" >&2; exit 1; fi
  t0=$(date +%s%N); b0=$(tx_bytes); sleep "$probe"; b1=$(tx_bytes); t1=$(date +%s%N)
  [[ -n "$b0" && -n "$b1" ]] || { echo "cannot read the transmit counter of $dev" >&2; exit 1; }
  # kbit/s the NIC sends now, and that as a percent of the limit the operator will type.
  kbit=$(( (b1 - b0) * 8 * 1000000 / (t1 - t0) ))
  pct=$(( kbit * 100 / rate ))
  echo "$dev sends ${kbit} kbit/s now, ${pct} % of the ${rate} kbit/s limit (window ${min_pct} to ${max_pct} %)" >&2
  if (( pct < min_pct )); then echo "the limit would not fill its queue now; try at a busier hour" >&2; exit 1; fi
  if (( pct > max_pct )); then echo "the limit would stall the link now; try at a quieter hour" >&2; exit 1; fi
}
undo_body() {
  printf 'r=$(tc qdisc show dev %q root 2>/dev/null || true); if [[ "$r" == "qdisc tbf %s: root"* ]]; then tc qdisc del dev %q root; fi\n' \
    "$dev" "$root_handle" "$dev"
}
apply() { echo "an operator change goes through the operator session" >&2; exit 2; }
# What is left after the operator's undo (or all of it when the operator session could not run): root takes it back.
remove() { if [[ "$(state)" == ours ]]; then tc qdisc del dev "$dev" root; fi; }
# The operator's commands.
op_apply_script() {
  op_line sudo tc qdisc add dev "$dev" root handle "$root_handle": tbf rate "${rate}kbit" burst "${burst}kb" latency "${latency}ms"
}
op_remove_script() { if [[ "$(state)" == ours ]]; then op_line sudo tc qdisc del dev "$dev" root; fi; }
'''


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    validate(plan["scenario"]["id"], p, {})
    args = check_parameters(p)
    vm.check_vm_location(instance, p)
    if p.get("mode") == "operator-rate":
        return vm.vm_invocation(plan, action, p, OPERATOR_BODY, args, vm.operator_dispatch())
    return vm.vm_invocation(plan, action, p, NODE_BODY, args)


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
