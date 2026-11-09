#!/usr/bin/env python3
"""Node-to-node link delay and loss on the pod overlay (tc netem on a node's flannel.1).

What it changes: on one cluster node, the egress queue of the overlay device (flannel.1, the
VXLAN device that carries pod traffic to the other nodes) becomes a prio qdisc whose last band is a
netem qdisc. Only packets that match a rule of the contract (a destination inside another node's pod
network, one protocol and one port) go to that band; everything else leaves as before. The rest of
the node's traffic never meets the change, because it does not use the overlay device:

- 66 measured routes (2026-11-09, tb-w1): the agents' reports and syslog to 119 leave through ens19
  (192.168.230.0/24 via 192.168.213.1), OTel spans to tb-ops (192.168.122.20) and ssh from 66 use
  eth0, the API server and kubelet are host addresses on eth0. flannel.1 carries only pod networks
  (10.244.x.0/24 via 10.244.x.0).
- What does ride the overlay and is monitoring: the KCM node agents talk to their master agents on
  tb-w3 (NATS 4222, agent port 18080/18081), and DPM reaches the databases through a NodePort that may
  forward to another node. So rule ports never include those ports or any port a KCM container
  declares, packets to or from the protected ports pass ahead of the rules, and so do packets from
  this node's KCM pods and packets to any KCM pod in the cluster.
- Name lookups: the kcm-monitoring node agent runs on the host network (ClusterFirstWithHostNet) and
  looks up its headless master service through CoreDNS on tb-cp. kube-proxy masquerades those
  lookups to the node's own overlay address, so they look like no pod. Lookups leaving from this
  node's overlay address and answers going to another node's overlay address pass too (DNS port
  only: NodePort traffic is masqueraded to the same addresses and stays subject to the rules).
  Payload names cannot be matched (flower does not see them, and u32 announces itself in the kernel
  log), so the exclusion is by address and port.
- A rule port stays below the node's ephemeral range, so a rule never matches the reply of some other
  connection that happened to pick that port.

Self-recovery (U8): before the qdisc goes in, the node arms the deadline guard of executor_common with
an undo that deletes exactly this root qdisc (and nothing when the root is not ours). A qdisc ends
with a reboot, so the boot rule only removes the guard's own files.

Traces on the node (design principle 8): every node call is one `bash -s` (with sudo for run and
cleanup) and its arguments ride on stdin, so the auth log never sees tc, the device, a port or a
number. The classifier is flower: cls_u32 prints "u32 classifier" to the kernel log when it loads,
which the node forwards to 119. sch_netem prints "netem: version 1.3" when it loads, so the run
refuses unless sch_netem is already loaded (load it in a maintenance window outside any recording).

The executor is the means only. A scenario enables it by adding an exact contract below and the same
parameters to the registry; until then every scenario is refused.
"""
from __future__ import annotations

import ipaddress
from typing import Any

from executor_common import (
    ExecutorError, GUARD_FUNCTIONS, cli, kubectl_bash_argv, profile_instance, with_guard_values,
)

PROFILE_ID = "network.fault"

# Scenarios written before this executor existed. Their injection points are not this one (the
# catalog keeps them at unresolved locations); N3 re-examines them.
BLOCKED = {
    "F07-R": "cross-domain delay injection point is unresolved",
    "F12-P": "CNI/NET_ADMIN network namespace is unresolved",
    "F12-R": "physical interface and out-of-band recovery are unresolved",
    "F12-G": "non-service NMS source interface is unresolved",
    "F13-H": "WPM probe source host and interface are unresolved",
}

# Verified contracts, one per scenario (the same parameters as registry scenario_parameters). Empty
# until a scenario is designed and measured.
CONTRACTS: dict[str, dict[str, Any]] = {}

# Cluster nodes on 66 (z4 survey 2026-11-09) and the only device a rule may change.
NODES = {
    "tb-cp": "192.168.122.77",
    "tb-w1": "192.168.122.184",
    "tb-w2": "192.168.122.11",
    "tb-w3": "192.168.122.14",
}
DEVICE = "flannel.1"
POD_NETWORK = ipaddress.ip_network("10.244.0.0/16")
# Monitoring, management and control-plane ports. A rule may not name one, and packets to or from one
# pass ahead of the rules.
PROTECTED_PORTS = (
    22,      # ssh (runner and 66 management)
    514,     # syslog to 119
    1521,    # Oracle: DPM through a NodePort that may forward across the overlay
    2379,    # etcd
    2380,    # etcd peers
    3306,    # MySQL: DPM, as above
    4222,    # KCM master agent (NATS), reached from every node over the overlay
    4317,    # OTLP gRPC
    4318,    # OTLP HTTP
    5432,    # PostgreSQL: DPM, as above
    6443,    # API server
    8472,    # flannel VXLAN itself
    10250,   # kubelet
    14317,   # OTLP gRPC to tb-ops
    14318,   # OTLP HTTP to tb-ops
    17575,   # agent reports to 119
    18080,   # KCM agent
    18081,   # KCM agent
    18090,   # agent reports to 119
)
MAX_RULES = 8
MAX_DELAY_MS = 2000
# A port at or above this would sit in the node's ephemeral range (32768-60999 measured on 66).
PORT_CEILING = 32768
PARAMETER_KEYS = {"host", "node", "device", "delay_ms", "jitter_ms", "loss_percent", "rules"}


def _integer(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ExecutorError(f"{name} must be an integer between {low} and {high}")
    return value


def _rule_token(rule: Any) -> str:
    """proto:side:port:cidr:probe, the form the remote side reads."""
    if not isinstance(rule, dict):
        raise ExecutorError("every rule must be an object")
    sides = [side for side in ("dport", "sport") if side in rule]
    if set(rule) != {"dst", "protocol", *sides} or len(sides) != 1:
        raise ExecutorError("a rule names dst, protocol and exactly one of dport or sport")
    try:
        dst = ipaddress.ip_network(rule["dst"], strict=True)
    except (TypeError, ValueError) as exc:
        raise ExecutorError("rule dst must be an exact IPv4 network") from exc
    if dst.version != 4 or not dst.subnet_of(POD_NETWORK) or dst.prefixlen < 24:
        raise ExecutorError("rule dst must lie inside one node's pod network (10.244.0.0/16, /24 or narrower)")
    if rule["protocol"] not in ("tcp", "udp"):
        raise ExecutorError("rule protocol must be tcp or udp")
    side = sides[0]
    port = _integer(rule[side], side, 1, PORT_CEILING - 1)
    if port in PROTECTED_PORTS:
        raise ExecutorError(f"rule port {port} carries monitoring or management traffic")
    probe = dst.network_address + 1 if dst.prefixlen < 31 else dst.network_address
    return f"{rule['protocol']}:{side}:{port}:{dst}:{probe}"


def check_parameters(params: dict[str, Any]) -> list[str]:
    """Structural bounds every contract must meet; returns the rule tokens."""
    if not isinstance(params, dict) or set(params) != PARAMETER_KEYS:
        raise ExecutorError(f"parameters must be exactly {sorted(PARAMETER_KEYS)}")
    if NODES.get(params["node"]) != params["host"]:
        raise ExecutorError("node and host must name the same cluster node")
    if params["device"] != DEVICE:
        raise ExecutorError("only the pod overlay device may be changed")
    delay = _integer(params["delay_ms"], "delay_ms", 0, MAX_DELAY_MS)
    _integer(params["jitter_ms"], "jitter_ms", 0, delay)
    loss = params["loss_percent"]
    if isinstance(loss, bool) or not isinstance(loss, (int, float)) or not 0 <= loss <= 100 or round(loss, 2) != loss:
        raise ExecutorError("loss_percent must be a number between 0 and 100 with at most two decimals")
    if delay == 0 and loss == 0:
        raise ExecutorError("a link change needs delay or loss")
    rules = params["rules"]
    if not isinstance(rules, list) or not 1 <= len(rules) <= MAX_RULES:
        raise ExecutorError(f"rules must be a list of 1 to {MAX_RULES} rules")
    tokens = [_rule_token(rule) for rule in rules]
    if len(set(tokens)) != len(tokens):
        raise ExecutorError("rules must not repeat")
    return tokens


def _refuse_blocked(scenario_id: str) -> None:
    if scenario_id in BLOCKED:
        raise ExecutorError(f"unsafe network fault blocked: {BLOCKED[scenario_id]}")


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    _refuse_blocked(scenario_id)
    contract = CONTRACTS.get(scenario_id)
    if contract is None:
        raise ExecutorError("scenario has no verified network link contract")
    allowed = profile.get("parameter_contract", {}).get("allowed_scenarios")
    if allowed is not None and scenario_id not in allowed:
        raise ExecutorError("scenario is not enabled by the profile registry")
    if params != contract:
        raise ExecutorError("parameters must exactly match the approved network link contract")
    check_parameters(params)


def _loss_text(loss: float | int) -> str:
    return f"{loss:g}" if isinstance(loss, float) else str(loss)


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    _refuse_blocked(plan.get("scenario", {}).get("id", ""))
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    validate(plan["scenario"]["id"], p, {})
    tokens = check_parameters(p)
    location = instance["location"]
    if location.get("transport") != "ssh" or location.get("host") != p["host"] or location.get("node") != p["node"]:
        raise ExecutorError("network link location must be the contract's node")
    return kubectl_bash_argv([
        action, p["host"], p["node"], p["device"], str(p["delay_ms"]), str(p["jitter_ms"]),
        _loss_text(p["loss_percent"]), ",".join(tokens), ",".join(str(port) for port in PROTECTED_PORTS),
    ]), with_guard_values(SCRIPT, plan)


# Runs as root (run, cleanup) or as the login user (check, state) on the node. Line 1 of its stdin is
# `set -- <action> <device> <job> <protected ports> <protected sources> <delay> <jitter> <loss> <rules>
# <protected destinations> <other nodes' overlay addresses>` (lists comma separated, "-" when empty),
# line 2 the guard's deadline and hold.
NODE_PROGRAM = b"set -euo pipefail\n" + GUARD_FUNCTIONS + br'''act="$1"; dev="$2"; job="$3"; ports="$4"; sources="$5"; delay="$6"; jitter="$7"; loss="$8"; rules="$9"
monitors="${10:--}"; peers="${11:--}"
root_handle=1a7e
dns_port=53
band_handle=1a7f
# Modules are global, so a sandbox may point at its own list.
module_dir="${LINK_MODULE_DIR:-/sys/module}"
# ip(8) asks the kernel of this network namespace; /sys/class/net shows the namespace sysfs was mounted in.
link_up() { ip link show dev "$dev" >/dev/null 2>&1; }
link_state() {
  if ! link_up; then echo absent; return 0; fi
  local r
  r=$(tc qdisc show dev "$dev" root)
  case "$r" in
    "qdisc noqueue 0: root"*) echo default ;;
    "qdisc prio $root_handle: root"*) echo ours ;;
    *) echo foreign ;;
  esac
}
# The undo the node runs by itself: delete this root qdisc, nothing else and nothing when it is gone.
undo_body() {
  printf 'if ip link show dev %q >/dev/null 2>&1 && [[ "$(tc qdisc show dev %q root)" == "qdisc prio %s: root"* ]]; then tc qdisc del dev %q root; fi\n' \
    "$dev" "$dev" "$root_handle" "$dev"
}
arm() { guard_arm "$job" "$expire_after" "$@"; }
check() {
  command -v tc >/dev/null
  if ! link_up; then echo "no $dev on this node" >&2; exit 1; fi
  # sch_netem prints its version to the kernel log when it loads (the node forwards it to 119).
  if [[ ! -d "$module_dir/sch_netem" ]]; then echo "sch_netem is not loaded; load it outside a recording window" >&2; exit 1; fi
  local lo rule proto side port dst probe route
  read -r lo _ </proc/sys/net/ipv4/ip_local_port_range
  for rule in ${rules//,/ }; do
    IFS=: read -r proto side port dst probe <<<"$rule"
    if (( port >= lo )); then echo "rule port $port is inside the ephemeral range" >&2; exit 1; fi
    if ! route=$(ip -o route get "$probe" 2>/dev/null) || [[ "$route" != *" dev $dev "* ]]; then
      echo "$dst does not leave through $dev" >&2; exit 1
    fi
  done
}
apply() {
  local b proto port src rule side dst probe key own
  local -a spec=() protos=()
  tc qdisc add dev "$dev" root handle "$root_handle": prio bands 4 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1
  # The overlay device has no transmit queue length, so a default band would hold one packet.
  for b in 1 2 3; do tc qdisc add dev "$dev" parent "$root_handle:$b" pfifo limit 1000; done
  if (( delay > 0 )); then
    spec+=(delay "${delay}ms")
    if (( jitter > 0 )); then spec+=("${jitter}ms"); fi
  fi
  if [[ "$loss" != 0 ]]; then spec+=(loss "${loss}%"); fi
  tc qdisc add dev "$dev" parent "$root_handle:4" handle "$band_handle": netem limit 100000 "${spec[@]}"
  for rule in ${rules//,/ }; do
    IFS=: read -r proto _ <<<"$rule"
    if [[ " ${protos[*]} " != *" $proto "* ]]; then protos+=("$proto"); fi
  done
  # Monitoring and management first: they pass in the ordinary band whatever the rules say.
  for proto in "${protos[@]}"; do
    for port in ${ports//,/ }; do
      tc filter add dev "$dev" parent "$root_handle": protocol ip pref 1 flower ip_proto "$proto" dst_port "$port" classid "$root_handle:2"
      tc filter add dev "$dev" parent "$root_handle": protocol ip pref 2 flower ip_proto "$proto" src_port "$port" classid "$root_handle:2"
    done
  done
  if [[ "$sources" != - ]]; then
    for src in ${sources//,/ }; do
      tc filter add dev "$dev" parent "$root_handle": protocol ip pref 3 flower src_ip "$src" classid "$root_handle:2"
    done
  fi
  if [[ "$monitors" != - ]]; then
    for dst in ${monitors//,/ }; do
      tc filter add dev "$dev" parent "$root_handle": protocol ip pref 4 flower dst_ip "$dst" classid "$root_handle:2"
    done
  fi
  # Host-network agents' name lookups and the answers to them (see the module docstring). The node's
  # overlay address is read here, never taken from the contract.
  own=$(ip -4 -o addr show dev "$dev" | awk '{print $4}' | cut -d/ -f1 | head -n1)
  for proto in "${protos[@]}"; do
    if [[ -n "$own" ]]; then
      tc filter add dev "$dev" parent "$root_handle": protocol ip pref 5 flower ip_proto "$proto" src_ip "$own" dst_port "$dns_port" classid "$root_handle:2"
    fi
    if [[ "$peers" != - ]]; then
      for dst in ${peers//,/ }; do
        tc filter add dev "$dev" parent "$root_handle": protocol ip pref 6 flower ip_proto "$proto" dst_ip "$dst" src_port "$dns_port" classid "$root_handle:2"
      done
    fi
  done
  for rule in ${rules//,/ }; do
    IFS=: read -r proto side port dst probe <<<"$rule"
    key=dst_port
    if [[ "$side" == sport ]]; then key=src_port; fi
    tc filter add dev "$dev" parent "$root_handle": protocol ip pref 10 flower ip_proto "$proto" dst_ip "$dst" "$key" "$port" classid "$root_handle:4"
  done
}
remove() { if [[ "$(link_state)" == ours ]]; then tc qdisc del dev "$dev" root; fi; }
case "$act" in
  check) check; s=$(link_state); if [[ "$s" != default ]]; then echo "$dev root qdisc is $s" >&2; exit 1; fi ;;
  state) link_state ;;
  # Checked first, armed before the qdisc goes in; a run that fails half way leaves a root that is
  # still ours, which cleanup (or the node's own undo) deletes.
  run) check; s=$(link_state); if [[ "$s" != default ]]; then echo "$dev root qdisc is $s" >&2; exit 1; fi
    arm "$(undo_body)"
    apply
    s=$(link_state); [[ "$s" == ours ]] ;;
  # Postponed before anything is read, cancelled only once the root is no longer ours. When the
  # node already undid the change, there is nothing to delete and disarm says when it happened.
  cleanup) guard_postpone "$job" "$guard_hold"
    remove
    s=$(link_state); if [[ "$s" == ours ]]; then exit 1; fi
    guard_disarm "$job" ;;
  *) exit 2 ;;
esac
'''

SCRIPT = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; host="$2"; node="$3"; dev="$4"; delay="$5"; jitter="$6"; loss="$7"; rules="$8"; ports="$9"
state_root="${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}"
state="$state_root/${node}-${dev}-link"
# The guard's name for this change; it lives only in files under the node's 0700 state root.
job="link-${dev//./-}"
k=(kubectl --kubeconfig=/root/tb-kubeconfig)
node_sh() {
  ssh -i /root/.ssh/tb_key -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 "nkia@$host" "$@"
}
# One node call: the whole program and its arguments ride on ssh's stdin (a pipe of its own, so ssh
# never reads the rest of this script), and the node logs only `bash -s`.
node_link() {
  local as=$1 act=$2
  {
    printf 'set --'; printf ' %q' "$act" "$dev" "$job" "$ports" "$sources" "$delay" "$jitter" "$loss" "$rules" "$monitors" "$peers"; printf '\n'
    printf 'expire_after=%q guard_hold=%q\n' "$expire_after" "$guard_hold"
    cat <<'NODE_LINK'
'''

SCRIPT += NODE_PROGRAM + br'''NODE_LINK
  } | if [[ "$as" == root ]]; then node_sh sudo -n bash -s; else node_sh bash -s; fi
}
ip_int() { local a b c d; IFS=. read -r a b c d <<<"$1"; echo $(( (a << 24) | (b << 16) | (c << 8) | d )); }
in_cidr() {
  local ip=$1 net=${2%/*} bits=${2#*/} mask
  mask=$(( bits == 0 ? 0 : (0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF ))
  (( ($(ip_int "$ip") & mask) == ($(ip_int "$net") & mask) ))
}
inside() { (( ${1#*/} >= ${2#*/} )) && in_cidr "${1%/*}" "$2"; }
# The cluster as it is now, not as the contract remembers it: the node is Ready at this address,
# every rule leads into another node's pod network, no rule port is one a KCM container declares,
# and the KCM pods on this node (their traffic rides the same device) are passed through untouched.
sources=-; monitors=-; peers=-
cluster_check() {
  command -v kubectl >/dev/null; command -v jq >/dev/null; command -v ssh >/dev/null
  local nodes pods others kcm_ports rule port dst c found p
  nodes=$("${k[@]}" get nodes -o json)
  if ! jq -e --arg n "$node" --arg h "$host" '[.items[] | select(.metadata.name == $n)] | length == 1
      and (.[0].status.addresses | any(.type == "InternalIP" and .address == $h))
      and ([.[0].status.conditions[] | select(.type == "Ready") | .status] == ["True"])' <<<"$nodes" >/dev/null; then
    echo "$node is not a Ready node at $host" >&2; exit 1
  fi
  others=$(jq -r --arg n "$node" '.items[] | select(.metadata.name != $n) | .spec.podCIDR // empty' <<<"$nodes")
  pods=$("${k[@]}" get pods -A -o json)
  kcm_ports=$(jq -r '[.items[] | select(.metadata.namespace | startswith("kcm-")) | .spec.containers[].ports[]?.containerPort] | unique | .[]' <<<"$pods")
  for rule in ${rules//,/ }; do
    IFS=: read -r _ _ port dst _ <<<"$rule"
    found=no
    for c in $others; do if inside "$dst" "$c"; then found=yes; fi; done
    if [[ "$found" != yes ]]; then echo "$dst is not inside another node's pod network" >&2; exit 1; fi
    for p in $kcm_ports; do
      if [[ "$p" == "$port" ]]; then echo "rule port $port is a monitoring agent port" >&2; exit 1; fi
    done
  done
  sources=$(jq -r --arg n "$node" '[.items[] | select(.metadata.namespace | startswith("kcm-"))
      | select(.spec.nodeName == $n and .spec.hostNetwork != true) | .status.podIP // empty] | join(",")' <<<"$pods")
  if [[ -z "$sources" ]]; then sources=-; fi
  # Answers to KCM pods anywhere in the cluster, and DNS answers to the other nodes' host-network pods
  # (flannel gives each node the network address of its podCIDR on the overlay device).
  monitors=$(jq -r '[.items[] | select(.metadata.namespace | startswith("kcm-"))
      | select(.spec.hostNetwork != true) | .status.podIP // empty] | unique | join(",")' <<<"$pods")
  if [[ -z "$monitors" ]]; then monitors=-; fi
  peers=$(jq -r --arg n "$node" '[.items[] | select(.metadata.name != $n) | .spec.podCIDR // empty
      | split("/")[0]] | join(",")' <<<"$nodes")
  if [[ -z "$peers" ]]; then peers=-; fi
}
case "$action" in
  preflight) cluster_check; node_link user check; [[ ! -e "$state" ]] ;;
  # The state file goes first, so a run that fails half way is still cleaned up.
  run) cluster_check; if [[ -e "$state" ]]; then exit 1; fi
    mkdir -p "$state_root"; printf '%s\n' "$job" >"$state.tmp"; mv -T "$state.tmp" "$state"
    node_link root run ;;
  # Asks the node even without the state file: a runner container recreated during a run loses its
  # state but the node keeps the qdisc. Nothing on the node and no state means no-op.
  cleanup) node_link root cleanup
    rm -f "$state" ;;
  recovery) [[ ! -e "$state" ]]; s=$(node_link user state); [[ "$s" == default ]] ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
