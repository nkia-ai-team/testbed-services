#!/usr/bin/env python3
"""Node firewall allowlist executor (a tightened host rule that omits one caller).

F44-R reconstructs Harness 2024-09-01 (status.harness.io/incidents/bs6qp18g8l21): a
firewall rule in front of an internal registry "that was previously too permissive"
was tightened, the new rule "did not account for the NAT IP address" of one component
that calls the registry, and because that component "maintains a persistent socket
connection" it kept working until its connection was re-established, then failed.

Here the guarded service is the food restaurant API (port 8081 on tb-w3's pod network).
The change is a host firewall rule on tb-w3: new connections to that port are accepted
only from the node and entry networks (this node, the control-plane node through which
every NodePort request arrives, and the 192.168 host networks), and everything else is
logged and dropped. The in-cluster caller, order-service on the same node's pod network,
is not in the list. Connections that already exist stay open (the jump matches only
conntrack NEW), exactly like the persistent socket in the original.

Every rule is one `sudo iptables` command on the node, so the node's auth log (forwarded
to 119 as syslog) records each one with its arguments, as an operator's change would.
Nothing carries the scenario id: the chain and the local state file are named after the
host and the guarded service. Recovery is out of band: the rules match only forwarded
pod traffic to one port, so SSH to the node over the host network is never affected.
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance

PROFILE_ID = "host.firewall"

# 2026-10-09. F44-R: tb-w3 호스트 방화벽에 restaurant API(8081) 허용 목록을 넣는다. 허용 출발지는 이 노드
# (10.244.2.1, kubelet 프로브), 제어 노드의 파드 대역(10.244.0.0/24, tb-cp NodePort 로 들어오는 요청이
# flannel 주소로 SNAT 되어 온다), 호스트 망 둘이다. 같은 노드 파드 대역의 order(10.244.2.x)가 빠진다.
# 2026-10-09 실측: restaurant 파드의 8081 상대는 10.244.2.77(order, ESTABLISHED 1개 유지), 10.244.2.1, 10.244.0.0 뿐.
CONTRACTS = {
    "F44-R": {
        "host": "192.168.122.14",
        "node": "tb-w3",
        "namespace": "rca-testbed-food",
        "protected_app": "testbed-restaurant",
        "omitted_caller_app": "testbed-order",
        "protected_cidr": "10.244.2.0/24",
        "port": 8081,
        "chain": "RESTAURANT-API-IN",
        "allowed_sources": ["10.244.2.1/32", "10.244.0.0/24", "192.168.122.0/24", "192.168.200.0/24"],
        "comment": "restaurant-api: allow node and entry networks",
        "log_prefix": "[FW BLOCK] ",
        "log_limit": "6/min",
    },
}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    contract = CONTRACTS.get(scenario_id)
    if contract is None:
        raise ExecutorError("scenario has no verified host firewall contract")
    allowed = profile.get("parameter_contract", {}).get("allowed_scenarios")
    if allowed is not None and scenario_id not in allowed:
        raise ExecutorError("scenario is not enabled by the profile registry")
    if params != contract:
        raise ExecutorError("parameters must exactly match the approved host firewall contract")
    if not params["allowed_sources"]:
        raise ExecutorError("an allowlist without sources would drop every new connection")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    validate(plan["scenario"]["id"], p, {})
    location = instance["location"]
    if location.get("transport") != "ssh" or location.get("host") != p["host"] or location.get("node") != p["node"]:
        raise ExecutorError("host firewall location must be the measured worker")
    return kubectl_bash_argv([
        action, p["host"], p["node"], p["namespace"], p["protected_app"], p["omitted_caller_app"],
        p["protected_cidr"], str(p["port"]), p["chain"], ",".join(p["allowed_sources"]),
        p["comment"], p["log_prefix"], p["log_limit"],
    ]), SCRIPT


SCRIPT = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; host="$2"; node="$3"; ns="$4"; protected="$5"; caller="$6"; cidr="$7"; port="$8"
chain="$9"; allowed="${10}"; comment="${11}"; prefix="${12}"; limit="${13}"
state_root="${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}"
state="$state_root/${node}-${chain}-firewall"
k=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$ns")
node_sh() {
  ssh -i /root/.ssh/tb_key -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 "nkia@$host" "$@"
}
# ssh joins its arguments into one remote command line, so each iptables argument is
# shell-quoted here (the comment and the log prefix carry spaces and brackets). sudo then
# logs the command with its arguments, one auth line per rule.
ipt() { node_sh "sudo -n iptables $(printf '%q ' "$@")"; }
jump=(FORWARD -d "$cidr" -p tcp --dport "$port" -m conntrack --ctstate NEW -m comment --comment "$comment" -j "$chain")
# The rule set is read with one `iptables -S` (an innocuous auth line) and filtered here.
# `r=$(rules)` stands alone so that a failed listing stops the script under set -e
# instead of reading as "absent" (cleanup would otherwise drop its state on an ssh hiccup).
rules() { node_sh sudo -n iptables -S; }
ours() { grep -Fx -e "-N $chain" <<<"$1" >/dev/null || grep -E -e "^-A FORWARD .* -j ${chain}\$" <<<"$1" >/dev/null; }
jumps() { grep -E -e "^-A FORWARD .* -j ${chain}\$" <<<"$1" >/dev/null; }
has_chain() { grep -Fx -e "-N $chain" <<<"$1" >/dev/null; }
pod() { "${k[@]}" get pods -l "app=$1" -o json; }
# One Ready pod of the app on this node whose IP is inside the guarded pod network.
placed() {
  pod "$1" | jq -e --arg n "$node" '[.items[] | select(.metadata.deletionTimestamp == null)] | length == 1
      and (.[0].spec.nodeName == $n)
      and ([.[0].status.conditions[]? | select(.type == "Ready") | .status] == ["True"])' >/dev/null
}
pod_ip() { pod "$1" | jq -r '.items[] | select(.metadata.deletionTimestamp == null) | .status.podIP' | head -n1; }
ip_int() { local a b c d; IFS=. read -r a b c d <<<"$1"; echo $(( (a << 24) | (b << 16) | (c << 8) | d )); }
in_cidr() {
  local ip=$1 net=${2%/*} bits=${2#*/} mask
  mask=$(( bits == 0 ? 0 : (0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF ))
  (( ($(ip_int "$ip") & mask) == ($(ip_int "$net") & mask) ))
}
allowed_by_list() { local src; for src in ${allowed//,/ }; do in_cidr "$1" "$src" && return 0; done; return 1; }
# The change only bites when the guarded service and the omitted caller sit where the
# rule says: the service inside the guarded network, the caller inside it too but outside
# every allowed source. Check the live pods instead of trusting the contract.
placement_ok() {
  placed "$protected"; placed "$caller"
  local target from; target=$(pod_ip "$protected"); from=$(pod_ip "$caller")
  in_cidr "$target" "$cidr"; in_cidr "$from" "$cidr"
  if allowed_by_list "$from"; then return 1; fi
  "${k[@]}" get svc "$protected" -o json | jq -e --argjson p "$port" '[.spec.ports[].targetPort] | any(. == $p)' >/dev/null
}
# Forwarded pod traffic only meets iptables when the bridge hands it to netfilter.
bridged() { [[ "$(node_sh sysctl -n net.bridge.bridge-nf-call-iptables)" == "1" ]]; }
check() {
  command -v kubectl >/dev/null; command -v jq >/dev/null; command -v ssh >/dev/null
  rules >/dev/null
  bridged
  placement_ok
}
case "$action" in
  # `! ours` would not stop the script under set -e, so test it explicitly.
  preflight) check; r=$(rules); if ours "$r"; then exit 1; fi; [[ ! -e "$state" ]] ;;
  # The state file goes first, so a run that fails halfway is still cleaned up.
  run) check; r=$(rules); if ours "$r" || [[ -e "$state" ]]; then exit 1; fi
    mkdir -p "$state_root"; printf '%s\n' "$chain" >"$state.tmp"; mv -T "$state.tmp" "$state"
    ipt -N "$chain"
    for src in ${allowed//,/ }; do ipt -A "$chain" -s "$src" -m comment --comment "$comment" -j RETURN; done
    ipt -A "$chain" -m limit --limit "$limit" -j LOG --log-prefix "$prefix"
    ipt -A "$chain" -j DROP
    ipt -I "${jump[@]:0:1}" 1 "${jump[@]:1}"
    r=$(rules); jumps "$r" ;;
  cleanup) [[ -e "$state" ]] || exit 0
    r=$(rules)
    while jumps "$r"; do ipt -D "${jump[@]}"; r=$(rules); done
    if has_chain "$r"; then ipt -F "$chain"; ipt -X "$chain"; fi
    r=$(rules); if ours "$r"; then exit 1; fi
    rm -f "$state" ;;
  recovery) [[ ! -e "$state" ]]; r=$(rules); if ours "$r"; then exit 1; fi
    placed "$protected"; placed "$caller" ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
