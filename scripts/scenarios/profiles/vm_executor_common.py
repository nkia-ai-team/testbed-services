#!/usr/bin/env python3
"""Shared ground for the civil-petition VM executors (docs/spec-testbed-vm-3tier.md §6-1, §6-2, §3-5).

The civil-petition domain runs on six VMs on 66 instead of Kubernetes. Its executors never call the
cluster API. They reach a VM over its management NIC as the executor account and send one program
on stdin (`sudo -n bash -s`, the U8 form), or they reach 66 through the forced-command helper
(`t4-hv-exec`) that changes the hypervisor side of a link.

Fixed tables (design §3-5 rule 2): which VM, which NIC, which tap device an injection may change.
A request for anything else is refused before any connection is made, and the node or helper refuses
it again. The tables are written here, not read from the inventory; a test holds them equal to
civil-petition/deploy/inventory/t.json so the two cannot drift apart unseen.

Content guard (design §3-5 rule 6): the executors whose change is not tied to one interface
(router.config, vmhost.firewall) refuse any item that names a protected address, network,
interface or port, and the chain they build starts with fixed lines that accept the protected flows,
so a broad rule further down cannot catch them.

Control connection (design §6-2, runner contract with rca-scenario-runner): the runner keeps one
OpenSSH ControlMaster per VM; executors use the same ControlPath with ControlMaster=no. A run refuses
while the VM's master is down (a fresh connection would add a new management connection to the
recording); cleanup and recovery go through a fresh connection if they have to (undo first).
"""
from __future__ import annotations

import ipaddress
import json
import re
import shlex
from pathlib import Path
from typing import Any, Iterable

from executor_common import ExecutorError, GUARD_FUNCTIONS, kubectl_bash_argv, profile_instance, with_guard_values

DOMAIN = "civil-petition"
REPO = Path(__file__).resolve().parents[3]
INVENTORY = REPO / "civil-petition" / "deploy" / "inventory" / "t.json"
HELPER_DIR = REPO / "civil-petition" / "deploy" / "hypervisor"

# Accounts and keys (runner contract, T4S/T4R 2026-11-19). Paths are on tb-ops, in the runner container.
EXECUTOR_USER = "opsjob"
OPERATOR_USER = "sysop"
# One read-only folder mounted into the runner container (contract item 2, the runner's VM_OPS_KEY_DIR).
KEY_DIR_ENV = "VM_OPS_KEY_DIR"
KEY_DIR = "/root/.ssh/vm-ops"
EXECUTOR_KEY = f"{KEY_DIR}/t4-executor"
KNOWN_HOSTS = f"{KEY_DIR}/known_hosts_civil-petition"
CONTROL_DIR_ENV = "CIVIL_PETITION_SSH_CONTROL_DIR"
CONTROL_DIR_DEFAULT = "/app/state/ssh-control"
HV_USER = "hvjob"
HV_UID = 1460
# The operator terminal (design 6-2 operator path, Q35 answer): the runner opens operator sessions from a second
# address of tb-ops; 66 SNATs it on the management bridge so the VMs see OPERATOR_SOURCE_ADDR, apart from the
# executor's MGMT_HOST. The inventory (t.json hypervisor.operator_addr, networks.mgmt.operator_host_addr) holds the
# same values; a test holds them equal.
OPERATOR_KEY = f"{KEY_DIR}/t4-operator"
OPERATOR_BIND_ADDR = "192.168.122.21"
OPERATOR_SOURCE_ADDR = "192.168.127.5"
# The runner container's host (tb-ops); 66's forced-command key takes only this source (from="...").
RUNNER_ADDR = "192.168.122.20"
HV_KEY = f"{KEY_DIR}/t4-hv"
HV_HELPER = "/usr/local/sbin/t4-hv-exec"

VMS: dict[str, dict[str, Any]] = {
    "rt-core01": {"vmid": 201, "mgmt": "192.168.127.2", "role": "router", "location": "petition-rt-core01"},
    "web01": {"vmid": 202, "mgmt": "192.168.127.21", "role": "web", "location": "petition-web01"},
    "was01": {"vmid": 203, "mgmt": "192.168.127.31", "role": "was", "location": "petition-was01"},
    "was02": {"vmid": 204, "mgmt": "192.168.127.32", "role": "was", "location": "petition-was02"},
    "db01": {"vmid": 205, "mgmt": "192.168.127.41", "role": "db", "location": "petition-db01"},
    "loadgen": {"vmid": 206, "mgmt": "192.168.127.10", "role": "loadgen", "location": "petition-loadgen"},
}
HYPERVISOR = {"host": "192.168.122.1", "node": "pve66", "location": "petition-hypervisor"}
ROUTER = "rt-core01"
SERVERS = ("web01", "was01", "was02", "db01")

# Interfaces an injection may change (§3-5 rule 2). Router: the web, WAS and DB side NICs. Servers:
# their business NIC. Hypervisor: the tap device behind exactly those NICs (Proxmox tap<vmid>i<n> is
# the VM's net<n>).
ROUTER_DOWNLINKS = {"ens19": "web", "ens20": "was", "ens21": "db"}
SERVER_NICS = {"web01": "ens18", "was01": "ens18", "was02": "ens18", "db01": "ens18"}
HV_LINKS = {
    (201, "net1"): "tap201i1", (201, "net2"): "tap201i2", (201, "net3"): "tap201i3",
    (202, "net0"): "tap202i0", (203, "net0"): "tap203i0", (204, "net0"): "tap204i0", (205, "net0"): "tap205i0",
}
# Never changed: the router's upstream and management NICs, every management NIC, the load generator,
# and 66's tunnel, uplink and management bridges. The tests ask for each one and expect a refusal.
NEVER_INTERFACES = {
    "rt-core01": ("ens18", "ens22"),
    "web01": ("ens19",), "was01": ("ens19",), "was02": ("ens19",), "db01": ("ens19",),
    "loadgen": ("ens18", "ens19"),
    "pve66": ("wg0", "vmbr0", "vmbr0.200", "vmbr1", "vmbr7", "vmbr8", "vmbr9", "vmbr10", "vmbr11",
              "tap201i0", "tap201i4", "tap202i1", "tap203i1", "tap204i1", "tap205i1", "tap206i0", "tap206i1"),
}

# Protected flows (§3-5 rule 6).
TUNNEL_NET = "10.250.0.0/30"
MONITOR_ADDR = "10.250.0.1"
HV_TUNNEL_ADDR = "10.250.0.2"
ROUTER_UPLINK_ADDR = "192.168.123.254"
UPSTREAM_NET = "192.168.123.0/24"
MGMT_NET = "192.168.127.0/24"
MGMT_HOST = "192.168.127.1"
BUSINESS_NETS = ("192.168.123.0/24", "192.168.124.0/24", "192.168.125.0/24", "192.168.126.0/24")
# Routes an injection may not touch: the tunnel, the upstream network, the default route, management.
PROTECTED_ROUTES = (TUNNEL_NET, UPSTREAM_NET, "0.0.0.0/0", MGMT_NET)
# SMS reports, syslog, SNMP, traps, management ssh, and every tunnel port candidate (scripts/vm-tunnel/tunnel.env).
MONITOR_PORTS = (22, 161, 162, 514, 18090, 51820, 51821, 4500, 1194, 443)
ROUTER_MGMT_IF = "ens22"
ROUTER_UPLINK_IF = "ens18"
SERVER_MGMT_IF = "ens19"

# Names the executors leave on a VM (unit, chain, table, file) must stay neutral (design principle 8).
FORBIDDEN_WORDS = ("test", "testbed", "tb", "t4", "lucida", "scenario", "inject", "rca", "fault")
NAME = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")


def neutral_name(value: Any, field: str) -> str:
    """A unit, table, chain or file name the VM keeps: short, lowercase, and no tool or testbed word."""
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ExecutorError(f"{field} must be a short lowercase name")
    words = set(re.split(r"[-.]", value))
    for word in FORBIDDEN_WORDS:
        if word in words or (len(word) > 3 and word in value):
            raise ExecutorError(f"{field} must not carry the word {word}")
    return value


DESCRIPTION = re.compile(r"^[A-Za-z][A-Za-z0-9 ,.-]{2,60}$")


def description(value: Any) -> str:
    """A unit description the VM's journal shows: plain words, no tool or testbed word."""
    if not isinstance(value, str) or not DESCRIPTION.fullmatch(value):
        raise ExecutorError("description must be plain words")
    words = set(re.split(r"[^a-z0-9]+", value.lower()))
    for word in FORBIDDEN_WORDS:
        if word in words:
            raise ExecutorError(f"description must not carry the word {word}")
    return value


def integer(value: Any, field: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ExecutorError(f"{field} must be an integer between {low} and {high}")
    return value


def percent(value: Any, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100 or round(value, 2) != value:
        raise ExecutorError(f"{field} must be a number between 0 and 100 with at most two decimals")
    return f"{value:g}" if isinstance(value, float) else str(value)


def exact_keys(params: Any, keys: Iterable[str], what: str) -> dict[str, Any]:
    keys = set(keys)
    if not isinstance(params, dict) or set(params) != keys:
        raise ExecutorError(f"{what} parameters must be exactly {sorted(keys)}")
    return params


def vm_named(params: dict[str, Any], roles: Iterable[str] | None = None) -> str:
    """The contract's VM: node is a known VM name and host its management address."""
    node = params.get("node")
    vm = VMS.get(node) if isinstance(node, str) else None
    if vm is None:
        raise ExecutorError("node must name one of the domain's VMs")
    if params.get("host") != vm["mgmt"]:
        raise ExecutorError("host must be the VM's management address")
    if roles is not None and vm["role"] not in set(roles):
        raise ExecutorError(f"node {node} is not a {'/'.join(sorted(roles))} VM")
    return node


def check_vm_location(instance: dict[str, Any], params: dict[str, Any]) -> None:
    location = instance.get("location", {})
    if location.get("transport") != "ssh" or location.get("host") != params["host"] or location.get("node") != params["node"]:
        raise ExecutorError("location must be the contract's VM")


def check_hv_location(instance: dict[str, Any]) -> None:
    location = instance.get("location", {})
    if (location.get("transport") != "ssh" or location.get("host") != HYPERVISOR["host"]
            or location.get("node") != HYPERVISOR["node"]):
        raise ExecutorError("location must be the hypervisor")


def interface_allowed(node: str, ifname: Any) -> str:
    """§3-5 rule 2: the router's downlinks or the server's business NIC, nothing else."""
    allowed = set(ROUTER_DOWNLINKS) if node == ROUTER else ({SERVER_NICS[node]} if node in SERVER_NICS else set())
    if not isinstance(ifname, str) or ifname not in allowed:
        raise ExecutorError(f"interface {ifname!r} on {node} is not one an injection may change")
    return ifname


def hv_link(vmid: Any, net: Any) -> str:
    tap = HV_LINKS.get((vmid, net)) if isinstance(vmid, int) and isinstance(net, str) else None
    if tap is None:
        raise ExecutorError(f"VM {vmid!r} {net!r} is not a link an injection may change")
    return tap


# ---------------------------------------------------------------- content guard (§3-5 rule 6)

def _network(value: Any, field: str) -> ipaddress.IPv4Network:
    try:
        net = ipaddress.ip_network(value, strict=True)
    except (TypeError, ValueError) as exc:
        raise ExecutorError(f"{field} must be an exact IPv4 network") from exc
    if net.version != 4:
        raise ExecutorError(f"{field} must be IPv4")
    return net


def business_network(value: Any, field: str) -> str:
    """An address in a rule: inside one business network, never the router's upstream address, the
    tunnel or the management network."""
    net = _network(value, field)
    if not any(net.subnet_of(ipaddress.ip_network(b)) for b in BUSINESS_NETS):
        raise ExecutorError(f"{field} {value} is outside the domain's business networks")
    if ipaddress.ip_address(ROUTER_UPLINK_ADDR) in net and net.prefixlen == 32:
        raise ExecutorError(f"{field} names the router's upstream address")
    return str(net)


def routable_destination(value: Any, field: str) -> str:
    net = _network(value, field)
    if net.prefixlen == 0:
        raise ExecutorError(f"{field} {value} is the default route")
    for protected in PROTECTED_ROUTES:
        if protected != "0.0.0.0/0" and net.overlaps(ipaddress.ip_network(protected)):
            raise ExecutorError(f"{field} {value} touches the protected route {protected}")
    if not any(net.subnet_of(ipaddress.ip_network(b)) for b in BUSINESS_NETS[1:]):
        raise ExecutorError(f"{field} {value} is not a web, WAS or DB network")
    return str(net)


def guarded_port(value: Any, field: str) -> int:
    port = integer(value, field, 1, 65535)
    if port in MONITOR_PORTS:
        raise ExecutorError(f"{field} {port} carries monitoring, management or tunnel traffic")
    return port


RULE_KEYS = {"hook", "proto", "src", "dst", "dport", "verdict"}
HOOKS = {"router": ("forward", "input"), "server": ("input", "output")}
VERDICTS = ("drop", "reject")


def filter_rules(rules: Any, kind: str, protected_ifs: Iterable[str]) -> list[str]:
    """Validated rule tokens hook:proto:src:dst:dport:verdict ("-" for any)."""
    if not isinstance(rules, list) or not 1 <= len(rules) <= 8:
        raise ExecutorError("rules must be a list of 1 to 8 rules")
    tokens = []
    for rule in rules:
        if not isinstance(rule, dict) or not set(rule) <= RULE_KEYS or not {"hook", "verdict"} <= set(rule):
            raise ExecutorError(f"a rule names only {sorted(RULE_KEYS)} and at least hook and verdict")
        for key, value in rule.items():
            if isinstance(value, str) and (value in protected_ifs or value in (MONITOR_ADDR, HV_TUNNEL_ADDR)):
                raise ExecutorError(f"rule {key} names a protected interface or address")
        if rule["hook"] not in HOOKS[kind]:
            raise ExecutorError(f"rule hook must be one of {HOOKS[kind]}")
        if rule["verdict"] not in VERDICTS:
            raise ExecutorError(f"rule verdict must be one of {VERDICTS}")
        proto = rule.get("proto", "-")
        if proto not in ("tcp", "udp", "-"):
            raise ExecutorError("rule proto must be tcp or udp")
        src = business_network(rule["src"], "rule src") if "src" in rule else "-"
        dst = business_network(rule["dst"], "rule dst") if "dst" in rule else "-"
        if src == "-" and dst == "-":
            raise ExecutorError("a rule names src or dst")
        dport = "-"
        if "dport" in rule:
            if proto == "-":
                raise ExecutorError("a port needs tcp or udp")
            dport = str(guarded_port(rule["dport"], "rule dport"))
        tokens.append(":".join((rule["hook"], proto, src, dst, dport, rule["verdict"])))
    if len(set(tokens)) != len(tokens):
        raise ExecutorError("rules must not repeat")
    return tokens


def protected_accept_lines(kind: str, mgmt_if: str) -> list[str]:
    """The first lines of every chain an executor builds: the protected flows pass whatever follows."""
    lines = [
        f"ip saddr {TUNNEL_NET} accept",
        f"ip daddr {TUNNEL_NET} accept",
        f'iifname "{mgmt_if}" accept',
        f'oifname "{mgmt_if}" accept',
        f"ip saddr {MGMT_NET} accept",
        f"ip daddr {MGMT_NET} accept",
    ]
    if kind == "router":
        lines += [f"ip saddr {ROUTER_UPLINK_ADDR} accept", f"ip daddr {ROUTER_UPLINK_ADDR} accept"]
    return lines


PREFIX = re.compile(r"^(\[[A-Z][A-Z ]{1,18}\] |[A-Z][A-Z-]{1,18}: )$")


def log_prefix(value: Any) -> str:
    """The kernel log prefix of a blocked packet: empty, "[WORDS] " or "WORD: " (as an operator writes it)."""
    if value == "":
        return ""
    if not isinstance(value, str) or not PREFIX.fullmatch(value):
        raise ExecutorError('log_prefix must be empty, "[WORDS] " or "WORD: "')
    words = set(re.split(r"[^a-z]+", value.lower()))
    for word in FORBIDDEN_WORDS:
        if word in words:
            raise ExecutorError(f"log_prefix must not carry the word {word}")
    return value


def nft_table(table: str, kind: str, mgmt_if: str, tokens: list[str], prefix: str = "") -> str:
    """One nftables table: a base chain per hook the rules use, protected lines first."""
    by_hook: dict[str, list[str]] = {}
    for token in tokens:
        hook, proto, src, dst, dport, verdict = token.split(":")
        parts = []
        if src != "-":
            parts.append(f"ip saddr {src}")
        if dst != "-":
            parts.append(f"ip daddr {dst}")
        if proto != "-":
            parts.append(f"meta l4proto {proto}")
            if dport != "-":
                parts.append(f"th dport {dport}")
        match = " ".join(parts)
        if prefix:
            by_hook.setdefault(hook, []).append(f'{match} limit rate 6/minute log prefix "{prefix}"')
        by_hook.setdefault(hook, []).append(f"{match} counter {verdict}")
    out = [f"table inet {table} {{"]
    for hook in sorted(by_hook):
        out.append(f"\tchain {hook} {{")
        out.append(f"\t\ttype filter hook {hook} priority -5; policy accept;")
        out += [f"\t\t{line}" for line in protected_accept_lines(kind, mgmt_if)]
        out += [f"\t\t{line}" for line in by_hook[hook]]
        out.append("\t}")
    out.append("}")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- runner side

SSH_OPTIONS = (
    "-F none -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes "
    f"-o UserKnownHostsFile={KNOWN_HOSTS} -o ConnectTimeout=10"
)

# Runs in the runner container. argv: action host node <program arguments...>. stdin of each remote call
# is the `set --` line, the guard values and the node program; the VM's auth log sees only `bash -s`.
RUNNER_HEAD = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; host="$2"; node="$3"; shift 3
node_args=("$@")
ctl="${CIVIL_PETITION_SSH_CONTROL_DIR:-/app/state/ssh-control}"
keys="${VM_OPS_KEY_DIR:-/root/.ssh/vm-ops}"
ssh_vm=(ssh -F none -i "$keys/t4-executor" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes
  -o "UserKnownHostsFile=$keys/known_hosts_civil-petition" -o ConnectTimeout=10 -o "ControlPath=$ctl/%r@%h:%p")
ssh_hv=(ssh -F none -i "$keys/t4-hv" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes
  -o "UserKnownHostsFile=$keys/known_hosts_civil-petition" -o ConnectTimeout=10 -o "ControlPath=$ctl/%r@%h:%p"
  -o ControlMaster=no)
# The runner's own master for this VM (rca-scenario-runner keeps one per VM, design 6-2).
master_up() { "${ssh_vm[@]}" -O check "opsjob@$host" >/dev/null 2>&1; }
need_master() { if ! master_up; then echo "control connection to $host is down" >&2; exit 1; fi; }
# 66's helper connection: the runner keeps one master to 66 as well, so a run does not add login lines.
hv_master_up() { "${ssh_hv[@]}" -O check hvjob@192.168.122.1 >/dev/null 2>&1; }
need_hv_master() { if ! hv_master_up; then echo "control connection to 192.168.122.1 is down" >&2; exit 1; fi; }
# One request to 66's forced-command helper: the request line, then the guard values.
hv_call() { printf '%s\nexpire_after=%s guard_hold=%s\n' "$1" "$expire_after" "$guard_hold" | "${ssh_hv[@]}" hvjob@192.168.122.1; }
# The operator path (design 6-2, Q35): a fresh ssh login as the operator from the operator terminal address, with a
# pty (sudo logs TTY=pts/N), key authentication, a session that opens and closes. The commands go on stdin to bash.
# One operator session per VM at a time: the runner's normal-time operator sessions take the same lock.
op_ssh=(ssh -tt -F none -i "$keys/t4-operator" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes
  -o "UserKnownHostsFile=$keys/known_hosts_civil-petition" -o ConnectTimeout=10 -o ControlPath=none -b @OPERATOR_BIND@)
op_session() {
  mkdir -p "$ctl"
  {
    if ! flock -w 600 8; then echo "operator session on $node is busy" >&2; return 1; fi
    { printf 'set -e\n'; cat; printf 'exit 0\n'; } | "${op_ssh[@]}" "sysop@$host" bash -s
  } 8>"$ctl/operator-$node.lock"
}
'''.replace(b"@OPERATOR_BIND@", OPERATOR_BIND_ADDR.encode())


def runner_script(node_program: bytes, dispatch: bytes) -> bytes:
    """A runner-side bash body: shared head, node_call (the program on stdin), then dispatch."""
    return RUNNER_HEAD + br'''node_call() {
  local act=$1
  {
    printf 'set --'; printf ' %q' "$act" ${node_args[@]+"${node_args[@]}"}; printf '\n'
    printf 'expire_after=%q guard_hold=%q\n' "$expire_after" "$guard_hold"
    cat <<'VM_NODE_PROGRAM'
''' + node_program + br'''VM_NODE_PROGRAM
  } | "${ssh_vm[@]}" -o ControlMaster=no "opsjob@$host" sudo -n bash -s
}
''' + dispatch


# What every guarded change does with its node program: check, read state, arm before change, apply,
# undo on cleanup. A program defines check, state (prints default, ours or something else), undo_body,
# apply, remove, and may set boot_files (paths the guard removes at the next boot).
STANDARD_DISPATCH = br'''case "$action" in
  preflight) node_call check ;;
  run) need_master; node_call run ;;
  cleanup) node_call cleanup ;;
  recovery) s=$(node_call state); [[ "$s" == default ]] ;;
  *) exit 2 ;;
esac
'''

NODE_DISPATCH = br'''boot_files=()
arm() { guard_arm "$job" "$expire_after" "$@"; }
not_default() { local s; s=$(state); if [[ "$s" != default ]]; then echo "the change is $s" >&2; return 0; fi; return 1; }
dispatch() {
  case "$act" in
    check) check; if not_default; then exit 1; fi ;;
    state) state ;;
    # Checked first, armed before the change; a run that fails half way leaves a change that is still
    # ours, which cleanup (or the node's own undo) removes.
    run) check; if not_default; then exit 1; fi
      prepare
      arm "$(undo_body)" ${boot_files[@]+"${boot_files[@]}"}
      apply
      s=$(state); [[ "$s" == ours ]] ;;
    # Postponed before anything is read, cancelled only once the change is gone.
    cleanup) guard_postpone "$job" "$guard_hold"
      remove
      s=$(state); if [[ "$s" == ours ]]; then exit 1; fi
      guard_disarm "$job" ;;
    # The operator path (design 6-2, Q35): the node checks and arms, then prints the operator's commands; the runner
    # types them in an operator ssh session. cleanup: hold prints the operator's undo commands, finish takes back
    # whatever is left as root and disarms. Only the operator's commands go to stdout.
    arm) { check; if not_default; then exit 1; fi
        prepare
        arm "$(undo_body)" ${boot_files[@]+"${boot_files[@]}"}
      } >&2
      op_apply_script ;;
    hold) guard_postpone "$job" "$guard_hold" >&2
      s=$(state); if [[ "$s" == ours ]]; then op_remove_script; fi ;;
    finish) remove >&2
      s=$(state); if [[ "$s" == ours ]]; then exit 1; fi
      guard_disarm "$job" ;;
    *) exit 2 ;;
  esac
}
prepare() { :; }
op_apply_script() { :; }
op_remove_script() { :; }
'''


def node_program(body: bytes) -> bytes:
    """`set -euo pipefail`, the U8 guard, the standard dispatch, the executor's body, then dispatch."""
    return b"set -euo pipefail\n" + GUARD_FUNCTIONS + NODE_DISPATCH + body + b"dispatch\n"


# The operator path, node side (design 6-2, Q35). The operator's commands are typed in a real operator ssh login
# (runner side op_session); this node side only names the operator and prints the commands, shell-quoted.
# This block and op_session (RUNNER_HEAD) are the whole operator path; router.config and vm.service use nothing else.
OPERATOR_FUNCTIONS = br'''operator=sysop
op_home="${OPS_HOME:-$(getent passwd "$operator" | cut -d: -f6)}"
# One operator command line: the words shell-quoted, as the operator's bash will read them.
op_line() { local first=1 w; for w in "$@"; do if (( first )); then first=0; else printf ' '; fi; printf '%q' "$w"; done; printf '\n'; }
'''

# A bounded background job in its own process group (cpu loops, memory hold, IO, table fill): the
# group leader writes its pgid, the undo kills exactly that group while the leader's command line still
# names the pgid file (a reboot or a long wait can hand the number on), then removes the job's files.
GROUP_FUNCTIONS = br'''group_setup() {
  job="$1"
  state_root="${OPSJOB_STATE_ROOT:-/var/lib/opsjobs}"
  pgidfile="$state_root/$job.pgid"; logfile="$state_root/$job.log"
  boot_files=("$pgidfile" "$logfile")
}
group_live() { [[ -s "$pgidfile" ]] && kill -0 -"$(cat "$pgidfile")" 2>/dev/null; }
group_start() {
  mkdir -p "$state_root"; chmod 700 "$state_root"
  setsid bash -c 'echo $$ >"$1.tmp"; mv -T "$1.tmp" "$1"; trap "kill 0" EXIT; eval "$2"' group "$pgidfile" "$1" >"$logfile" 2>&1 </dev/null &
  local _
  for _ in $(seq 1 25); do if [[ -s "$pgidfile" ]]; then return 0; fi; sleep 0.2; done
  [[ -s "$pgidfile" ]]
}
group_undo() {
  printf 'pgid=$(cat %q 2>/dev/null || true)\n' "$pgidfile"
  printf 'if [[ "$pgid" =~ ^[0-9]+$ ]] && kill -0 -"$pgid" 2>/dev/null; then\n'
  printf '  if [[ ! -e /proc/$pgid ]] || tr "\\0" " " </proc/$pgid/cmdline | grep -Fq %q; then\n' "$pgidfile"
  printf '    kill -TERM -"$pgid" 2>/dev/null || true\n'
  printf '    for _ in $(seq 1 30); do kill -0 -"$pgid" 2>/dev/null || break; sleep 1; done\n'
  printf '    kill -KILL -"$pgid" 2>/dev/null || true\n'
  printf '  fi\n'
  printf 'fi\n'
  printf 'rm -f %q %q %q\n' "$pgidfile" "$pgidfile.tmp" "$logfile"
}
group_stop() {
  if group_live; then
    local g; g=$(cat "$pgidfile")
    kill -TERM -"$g" 2>/dev/null || true
    for _ in $(seq 1 30); do group_live || break; sleep 1; done
    if group_live; then kill -KILL -"$g" 2>/dev/null || true; fi
  fi
  rm -f "$pgidfile" "$pgidfile.tmp" "$logfile"
}
'''

# A named transient unit (a backup job, a maintenance session): its name and description are the
# contract's, an operator's plain words, and its start line reaches 119 as such a job's would.
UNIT_FUNCTIONS = br'''unit_ctl="${UNIT_SYSTEMCTL:-systemctl}"
unit_run="${UNIT_SYSTEMD_RUN:-systemd-run}"
unit_active() { "$unit_ctl" is-active --quiet "$unit.service" </dev/null 2>/dev/null; }
unit_known() { "$unit_ctl" cat "$unit.service" </dev/null >/dev/null 2>&1; }
unit_start() {
  "$unit_run" --quiet --collect --unit="$unit" --description="$description" "$@" </dev/null
}
unit_undo() { printf 'if %q is-active --quiet %q; then %q stop %q; fi\n' "$unit_ctl" "$unit.service" "$unit_ctl" "$unit.service"; }
unit_stop() { if unit_active; then "$unit_ctl" stop "$unit.service" </dev/null; fi; }
'''


# router.config (mode operator) and vmhost.firewall (mode root): one nftables table, routes, sysctls.
# set -- <act> <mode> <table> <nft text or -> <routes dst=type=via,... or -> <sysctls key=value,... or ->
FIREWALL_BODY = OPERATOR_FUNCTIONS + br'''act="$1"; mode="$2"; table="$3"; text="$4"; routes="$5"; sysctls="$6"
job="cfg-$table"
state_root="${OPSJOB_STATE_ROOT:-/var/lib/opsjobs}"
prev="$state_root/$job.prev"
file="$op_home/$table.nft"
table_present() { nft list table inet "$table" >/dev/null 2>&1; }
route_parts() { dst="${1%%=*}"; local rest="${1#*=}"; rtype="${rest%%=*}"; via="${rest#*=}"; shown="${dst%/32}"; }
route_ours() {
  local r; route_parts "$1"; r=$(ip route show exact "$dst" 2>/dev/null || true)
  if [[ "$rtype" == via ]]; then [[ "$r" == "$shown via $via "* || "$r" == "$shown via $via" ]]; else [[ "$r" == "$rtype $shown"* ]]; fi
}
items() { if [[ "$1" != - ]]; then printf '%s\n' ${1//,/ }; fi; }
state() {
  local t k v
  if table_present; then echo ours; return 0; fi
  if [[ "$mode" == operator && "$text" != - && -e "$file" ]]; then echo ours; return 0; fi
  while read -r t; do if [[ -n "$t" ]] && route_ours "$t"; then echo ours; return 0; fi; done < <(items "$routes")
  if [[ -s "$prev" ]]; then
    while IFS== read -r k v; do if [[ "$(sysctl -n "$k")" != "$v" ]]; then echo ours; return 0; fi; done <"$prev"
    echo leftover; return 0
  fi
  echo default
}
check() {
  local t
  command -v nft >/dev/null; command -v ip >/dev/null
  if [[ "$mode" == operator && ( -z "$op_home" || ! -d "$op_home" ) ]]; then echo "no home for $operator" >&2; exit 1; fi
  if [[ "$text" != - ]]; then printf '%s' "$text" | nft -c -f -; fi
  while read -r t; do
    [[ -n "$t" ]] || continue
    route_parts "$t"
    if [[ -n "$(ip route show exact "$dst")" ]]; then echo "a route for $dst exists" >&2; exit 1; fi
  done < <(items "$routes")
  while read -r t; do if [[ -n "$t" ]]; then sysctl -n "${t%%=*}" >/dev/null; fi; done < <(items "$sysctls")
}
prepare() {
  local t
  mkdir -p "$state_root"; chmod 700 "$state_root"
  if [[ "$sysctls" != - ]]; then
    while read -r t; do if [[ -n "$t" ]]; then printf '%s=%s\n' "${t%%=*}" "$(sysctl -n "${t%%=*}")"; fi; done < <(items "$sysctls") >"$prev.tmp"
    mv -T "$prev.tmp" "$prev"
    boot_files+=("$prev")
  fi
  if [[ "$mode" == operator && "$text" != - ]]; then boot_files+=("$file"); fi
}
undo_body() {
  local t k v
  if [[ "$text" != - ]]; then
    printf 'if nft list table inet %q >/dev/null 2>&1; then nft delete table inet %q; fi\n' "$table" "$table"
    if [[ "$mode" == operator ]]; then printf 'rm -f %q\n' "$file"; fi
  fi
  while read -r t; do
    [[ -n "$t" ]] || continue
    route_parts "$t"
    if [[ "$rtype" == via ]]; then
      printf 'r=$(ip route show exact %q); if [[ "$r" == %q* ]]; then ip route del %q via %q; fi\n' "$dst" "$shown via $via" "$dst" "$via"
    else
      printf 'r=$(ip route show exact %q); if [[ "$r" == %q* ]]; then ip route del %q %q; fi\n' "$dst" "$rtype $shown" "$rtype" "$dst"
    fi
  done < <(items "$routes")
  if [[ -s "$prev" ]]; then
    while IFS== read -r k v; do printf 'sysctl -q -w %q\n' "$k=$v"; done <"$prev"
    printf 'rm -f %q\n' "$prev"
  fi
}
# Root mode (vmhost.firewall): the executor applies. Operator mode (router.config): the operator does, in its own
# ssh session (op_apply_script below); the root apply refuses so no change can bypass the operator.
apply() {
  local t
  if [[ "$mode" == operator ]]; then echo "an operator change goes through the operator session" >&2; exit 2; fi
  if [[ "$text" != - ]]; then printf '%s' "$text" | nft -f -; fi
  while read -r t; do
    [[ -n "$t" ]] || continue
    route_parts "$t"
    if [[ "$rtype" == via ]]; then ip route add "$dst" via "$via"; else ip route add "$rtype" "$dst"; fi
  done < <(items "$routes")
  while read -r t; do if [[ -n "$t" ]]; then sysctl -w "$t" >/dev/null; fi; done < <(items "$sysctls")
}
# What is left after the operator's undo (or all of it when the operator session could not run): root takes it back.
remove() {
  local t k v
  if table_present; then nft delete table inet "$table"; fi
  if [[ "$mode" == operator && "$text" != - && -e "$file" ]]; then rm -f "$file"; fi
  while read -r t; do
    [[ -n "$t" ]] || continue
    if route_ours "$t"; then
      route_parts "$t"
      if [[ "$rtype" == via ]]; then ip route del "$dst" via "$via"; else ip route del "$rtype" "$dst"; fi
    fi
  done < <(items "$routes")
  if [[ -s "$prev" ]]; then
    while IFS== read -r k v; do if [[ "$(sysctl -n "$k")" != "$v" ]]; then sysctl -w "$k=$v" >/dev/null; fi; done <"$prev"
    rm -f "$prev"
  fi
}
# The operator's commands. The rules file is written by the operator into its home (spaces, not tabs), then loaded.
op_apply_script() {
  local t
  if [[ "$text" != - ]]; then
    printf 'cat > %q <<%s\n' "$file" "'NFT_RULES'"
    printf '%s' "${text//$'\t'/    }"
    printf 'NFT_RULES\n'
    op_line sudo nft -f "$file"
  fi
  while read -r t; do
    [[ -n "$t" ]] || continue
    route_parts "$t"
    if [[ "$rtype" == via ]]; then op_line sudo ip route add "$dst" via "$via"; else op_line sudo ip route add "$rtype" "$dst"; fi
  done < <(items "$routes")
  while read -r t; do if [[ -n "$t" ]]; then op_line sudo sysctl -w "$t"; fi; done < <(items "$sysctls")
}
op_remove_script() {
  local t k v
  if table_present; then op_line sudo nft delete table inet "$table"; fi
  if [[ "$text" != - && -e "$file" ]]; then op_line rm -f "$file"; fi
  while read -r t; do
    [[ -n "$t" ]] || continue
    if route_ours "$t"; then
      route_parts "$t"
      if [[ "$rtype" == via ]]; then op_line sudo ip route del "$dst" via "$via"; else op_line sudo ip route del "$rtype" "$dst"; fi
    fi
  done < <(items "$routes")
  if [[ -s "$prev" ]]; then
    while IFS== read -r k v; do if [[ "$(sysctl -n "$k")" != "$v" ]]; then op_line sudo sysctl -w "$k=$v"; fi; done <"$prev"
  fi
}
'''

# A change that is not tied to one interface is checked right after it lands (§3-5 rule 6): 66 pings
# @PROBE@ from a tunnel source and the executor reads the VM over its management connection. If either
# fails, the change is undone on the spot and the run fails.
CHECKED_DISPATCH = br'''case "$action" in
  preflight) node_call check ;;
  run) need_master; node_call run
    if ! hv_call "probe @PROBE@" || ! s=$(node_call state) || [[ "$s" != ours ]]; then
      echo "the change cut a protected path or did not land; undoing it" >&2
      node_call cleanup || true
      exit 1
    fi ;;
  cleanup) node_call cleanup ;;
  recovery) s=$(node_call state); [[ "$s" == default ]] ;;
  *) exit 2 ;;
esac
'''


# The operator path, runner side (design 6-2, Q35): the node arms and prints the operator's commands, the runner
# types them in an operator ssh session, then @CHECK@ (a condition that holds when the change landed and nothing
# protected was cut). cleanup: the operator types the undo, then the node takes back whatever is left as root.
OPERATOR_DISPATCH = br'''case "$action" in
  preflight) node_call check ;;
  run) need_master
    script=$(node_call arm)
    if ! printf '%s\n' "$script" | op_session; then
      echo "the operator session failed; undoing" >&2
      node_call cleanup || true
      exit 1
    fi
    if ! { @CHECK@; }; then
      echo "the change cut a protected path or did not land; undoing it" >&2
      node_call cleanup || true
      exit 1
    fi ;;
  cleanup) script=$(node_call hold)
    if [[ -n "$script" ]] && ! printf '%s\n' "$script" | op_session; then
      echo "the operator session failed; the executor takes the change back" >&2
    fi
    node_call finish ;;
  recovery) s=$(node_call state); [[ "$s" == default ]] ;;
  *) exit 2 ;;
esac
'''
LANDED = b's=$(node_call state) && [[ "$s" == ours ]]'


def operator_dispatch(check: bytes = LANDED) -> bytes:
    return OPERATOR_DISPATCH.replace(b"@CHECK@", check)


def vm_invocation(plan: dict[str, Any], action: str, params: dict[str, Any], node_body: bytes,
                  program_args: list[str], dispatch: bytes = STANDARD_DISPATCH) -> tuple[list[str], bytes]:
    script = runner_script(node_program(node_body), dispatch)
    return (kubectl_bash_argv([action, params["host"], params["node"], *program_args]),
            with_guard_values(script, plan))


def hv_runner_script(dispatch: bytes) -> bytes:
    return RUNNER_HEAD + dispatch


def contract_lookup(contracts: dict[str, dict[str, Any]], scenario_id: str, params: dict[str, Any],
                    profile: dict[str, Any], what: str) -> None:
    contract = contracts.get(scenario_id)
    if contract is None:
        raise ExecutorError(f"scenario has no verified {what} contract")
    allowed = profile.get("parameter_contract", {}).get("allowed_scenarios")
    if allowed is not None and scenario_id not in allowed:
        raise ExecutorError("scenario is not enabled by the profile registry")
    if params != contract:
        raise ExecutorError(f"parameters must exactly match the approved {what} contract")


def quoted(values: Iterable[str]) -> str:
    return " ".join(shlex.quote(v) for v in values)


def inventory() -> dict[str, Any]:
    return json.loads(INVENTORY.read_text(encoding="utf-8"))
