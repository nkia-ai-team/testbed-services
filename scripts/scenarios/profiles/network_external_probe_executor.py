#!/usr/bin/env python3
"""Bounded external-origin flow generator for the F12-G unrelated-flow case."""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance

PROFILE_ID = "network.fault"

CONTRACT = {
    "host": "192.168.200.57",
    "interface": "eno1",
    "target_ip": "192.168.122.77",
    "target_port": 39999,
    "rate_per_second": 5,
    "duration_seconds": 120,
    "gateway": "192.168.200.109",
    "forward_host": "192.168.200.109",
    "forward_key": "/root/.ssh/tb_key",
}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    del profile
    if scenario_id != "F12-G" or params != CONTRACT:
        raise ExecutorError("parameters must exactly match the bounded F12-G external-flow contract")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    location = instance["location"]
    if location.get("transport") != "ssh" or location.get("host") != p["host"]:
        raise ExecutorError("F12-G must originate from the canonical external .57 host")
    return kubectl_bash_argv([
        action, plan["scenario"]["id"], p["host"], p["interface"], p["target_ip"],
        str(p["target_port"]), str(p["rate_per_second"]), str(p["duration_seconds"]),
        p["gateway"], p["forward_host"], p["forward_key"],
    ]), SCRIPT


SCRIPT = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; scenario_id="$2"; host="$3"; interface="$4"; target="$5"; port="$6"; rate="$7"; duration="$8"
gateway="$9"; forward_host="${10}"; forward_key="${11}"
state_root="${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}"
state="$state_root/${scenario_id}-external-flow"; tag="rca-${scenario_id}-external-nping"
route_prefix="192.168.122.0/24"; rule_tag="rca-${scenario_id}-external-flow"
ssh_cmd=(sshpass -e ssh -o BatchMode=no -o StrictHostKeyChecking=yes -o ConnectTimeout=8 "root@$host")
forward_ssh=(ssh -i "$forward_key" -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=8 "root@$forward_host")
remote_check="test \"\$(hostname)\" = dev-svr-200-57 && test \"\$(cat /sys/class/net/$interface/operstate)\" = up && command -v nping >/dev/null && command -v timeout >/dev/null"
route_check="ip route show exact '$route_prefix'"
rule=(iptables -w 5 -s "$host/32" -d "$target/32" -p tcp --dport "$port" -m comment --comment "$rule_tag" -j ACCEPT)
route_absent() { local output; output="$("${ssh_cmd[@]}" "$route_check")" || return 1; [[ -z "$output" ]]; }
rule_absent() {
  local check=("${rule[@]}") rc
  check[0]="iptables -C FORWARD"
  if "${forward_ssh[@]}" "${check[@]}" >/dev/null 2>&1; then return 1; else rc=$?; fi
  [[ "$rc" -eq 1 ]]
}
remove_route() { "${ssh_cmd[@]}" "ip route del '$route_prefix' via '$gateway' dev '$interface' proto static" 2>/dev/null || true; }
remove_rule() { local delete=("${rule[@]}"); delete[0]="iptables -D FORWARD"; "${forward_ssh[@]}" "${delete[@]}" 2>/dev/null || true; }
rollback_partial() {
  [[ -d "$state" ]] || return 0
  if [[ -e "$state/pid" ]]; then
    pid="$(cat "$state/pid")"
    "${ssh_cmd[@]}" "if test -r /proc/$pid/cmdline && tr '\\0' ' ' </proc/$pid/cmdline | grep -Fq '$tag'; then kill -TERM $pid; fi" || true
  fi
  if [[ -e "$state/rule-added" ]]; then remove_rule; fi
  if [[ -e "$state/route-added" ]]; then remove_route; fi
  return 0
}
remove_state() {
  rm -f "$state/pid" "$state/rule-added" "$state/route-added"
  rmdir "$state"
}
case "$action" in
  preflight)
    command -v sshpass >/dev/null; command -v ssh >/dev/null; [[ -n "${SSHPASS:-}" && ! -e "$state" && -r "$forward_key" ]]
    "${ssh_cmd[@]}" "$remote_check"
    "${forward_ssh[@]}" 'test "$(cat /proc/sys/net/ipv4/ip_forward)" = 1 && command -v iptables >/dev/null && ip -4 addr show dev br0 | grep -Fq "192.168.200.109/24" && ip -4 addr show dev virbr0 | grep -Fq "192.168.122.1/24"'
    route_absent; rule_absent
    ;;
  run)
    [[ ! -e "$state" && -n "${SSHPASS:-}" && -r "$forward_key" ]]; mkdir -p "$state"; trap 'rollback_partial; remove_state' ERR
    "${ssh_cmd[@]}" "$remote_check"; route_absent; rule_absent
    "${ssh_cmd[@]}" "ip route add '$route_prefix' via '$gateway' dev '$interface' proto static"; : >"$state/route-added"
    insert=("${rule[@]}"); insert[0]="iptables -I FORWARD 1"; "${forward_ssh[@]}" "${insert[@]}"; : >"$state/rule-added"
    pid="$("${ssh_cmd[@]}" "nohup bash -c 'exec -a $tag timeout ${duration}s nping --tcp -p $port --rate $rate --interface $interface $target' >/tmp/$tag.log 2>&1 & echo \$!")"
    [[ "$pid" =~ ^[0-9]+$ ]]; printf '%s\n' "$pid" >"$state/pid"; trap - ERR
    ;;
  cleanup)
    [[ -d "$state" ]] || exit 0; [[ -n "${SSHPASS:-}" && -r "$forward_key" ]]
    rollback_partial; route_absent; rule_absent; remove_state
    ;;
  recovery)
    [[ ! -e "$state" && -n "${SSHPASS:-}" && -r "$forward_key" ]]; "${ssh_cmd[@]}" "$remote_check"
    route_absent; rule_absent
    ! "${ssh_cmd[@]}" "pgrep -f '^$tag '" >/dev/null 2>&1
    ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
