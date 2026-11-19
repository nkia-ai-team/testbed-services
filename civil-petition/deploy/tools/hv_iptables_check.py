#!/usr/bin/env python3
"""66 에 망 파일을 적용하고 되돌릴 때 iptables 가 이 테스트베드 몫만 바뀌었는지, 적용 뒤 브리지 IPv6 가 꺼졌는지 본다.

설계 docs/spec-testbed-vm-3tier.md §10 T2 "기존 테스트베드 영향 없음", runbook §4.
적용 전후에 `iptables-save` 를 떠서 준다. 같은 규칙이 두 번 생긴 것도 잡으려고 줄을 집합이 아니라 개수로 센다
(66 의 ifupdown2 는 reload 때 기존 tb-nat 의 post-up 을 다시 돌려 확인 없는 `-A` 규칙을 한 벌 더 만든다).

  hv_iptables_check.py --inventory t.json --mode up   --before 적용전.txt --after 적용후.txt --ipv6-state ipv6.txt
  hv_iptables_check.py --inventory t.json --mode down --before 적용전.txt --after 되돌린뒤.txt
  hv_iptables_check.py --inventory t.json --mode ipv6 --ipv6-state ipv6.txt

up:   지워진 줄 0, 늘어난 줄은 전용 체인(선언, 규칙, 훅)과 이 인벤토리의 MASQUERADE, 러너 SNAT 뿐이고
      MASQUERADE 수는 NAT 대역 수, SNAT 은 1.
      그리고 이 테스트베드 브리지마다 IPv6 가 꺼져 있고(disable_ipv6=1) IPv6 주소가 0 개다. iptables 격리는
      IPv4 만 막으므로, 66 의 ssh, Proxmox 웹이 IPv6 로도 듣는 한 브리지의 IPv6 가 켜져 있으면 관리망 격리가 샌다.
      ipv6.txt 는 66 에서 `render.py --hypervisor-commands ipv6-state` 가 찍는 명령을 돌린 출력이다.
      PASS 때 브리지마다 IPv6 가 켜져 있었다면 생겼을 링크 로컬 주소(MAC 의 EUI-64)를 함께 찍는다(VM 쪽 확인용).
down: 적용 전과 줄과 개수가 같다.
ipv6: 브리지 IPv6 상태만(up 의 IPv6 판정과 같다). 적용 뒤 시간이 지나 다시 볼 때 쓴다. 그 사이 다른 적용(T0 add-net 의
      TBWG 줄, 다른 테스트베드의 망)이 iptables 를 바꾸면 적용 직전 기준과 견주는 up 은 늘 FAIL 이라 다시 보기에 쓰지 않는다
      (11-19 S 차례 7, v4 APS2). iptables 쪽은 적용 직후의 up 이 판정한다.
"""
from __future__ import annotations

import argparse
import collections
import ipaddress
import json
import re
import sys
from pathlib import Path

COUNTERS = re.compile(r"\s*\[\d+:\d+\]\s*$")
BUILTIN = {"INPUT", "FORWARD", "OUTPUT", "PREROUTING", "POSTROUTING"}


def lines_of(text: str) -> collections.Counter:
    """iptables-save 출력을 (표, 줄) 개수로. 주석과 카운터와 COMMIT 은 뺀다."""
    table = ""
    out: collections.Counter = collections.Counter()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line == "COMMIT":
            continue
        if line.startswith("*"):
            table = line[1:]
            continue
        if line.startswith(":"):
            line = COUNTERS.sub("", line)
            name, policy = (line[1:].split() + ["", ""])[:2]
            if name in BUILTIN and policy == "ACCEPT":
                # 표가 처음 쓰일 때 iptables-save 에 나타나는 기본 정책 줄. 정책이 ACCEPT 가 아니게 바뀌면
                # 원래 줄이 없어진 것으로 잡힌다.
                continue
        out[(table, line)] += 1
    return out


def _cidr(text: str) -> str:
    return str(ipaddress.ip_network(text, strict=False))


def classify(inventory: dict, table: str, line: str) -> str | None:
    """이 테스트베드 몫이면 종류 이름, 아니면 None."""
    hv = inventory["hypervisor"]
    chains = {hv["chain"], hv["input_chain"]}
    if table == "filter":
        if line.startswith(":") and line.split()[0][1:] in chains:
            return "chain"
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "-A" and parts[1] in chains:
            return "rule"
        if parts[-2:-1] == ["-j"] and parts[-1] in chains:
            return "hook"
        return None
    if table == "nat" and line.startswith("-A POSTROUTING "):
        source = re.search(r"-s (\S+)", line)
        if not source:
            return None
        src = _cidr(source.group(1))
        nat_cidrs = {_cidr(v["cidr"]) for v in inventory["networks"].values() if v.get("nat")}
        if line.endswith("-j MASQUERADE") and src in nat_cidrs and f"-o {hv['uplink']} " in line:
            return "masquerade"
        mgmt = next(v for v in inventory["networks"].values() if v.get("management"))
        if (src == _cidr(hv["runner_addr"]) and f"-o {mgmt['bridge']} " in line
                and line.endswith(f"-j SNAT --to-source {mgmt['host_addr']}")):
            return "snat"
    return None


def check(inventory: dict, mode: str, before_text: str, after_text: str) -> list[str]:
    before, after = lines_of(before_text), lines_of(after_text)
    removed = before - after
    added = after - before
    problems: list[str] = []
    if mode == "down":
        for (table, line), n in sorted(removed.items()):
            problems.append(f"되돌린 뒤 없어진 줄 {n}: [{table}] {line}")
        for (table, line), n in sorted(added.items()):
            problems.append(f"되돌린 뒤 남은 줄 {n}: [{table}] {line}")
        if any(re.search(r"wg0|TBWG", p) for p in problems):
            problems.append("T0 터널 줄이 차이에 들어 있다: T2 를 먼저 되돌리고 T0 를 되돌린다(runbook §4 적용 순서)")
        return problems
    for (table, line), n in sorted(removed.items()):
        problems.append(f"적용으로 없어진 줄 {n}: [{table}] {line}")
    kinds: collections.Counter = collections.Counter()
    for (table, line), n in sorted(added.items()):
        kind = classify(inventory, table, line)
        if kind is None or n != 1:
            problems.append(f"이 테스트베드 몫이 아니거나 두 벌인 줄 {n}: [{table}] {line}")
            continue
        kinds[kind] += 1
    if any(re.search(r"wg0|TBWG", p) for p in problems):
        problems.append("T0 터널 줄이 차이에 들어 있다: 기준(before)을 T0 적용 뒤에 다시 뜨거나, T2 를 먼저 되돌린 뒤 T0 를 "
                        "되돌린다(runbook §4 적용 순서)")
    nat_count = sum(1 for v in inventory["networks"].values() if v.get("nat"))
    if kinds["masquerade"] != nat_count:
        problems.append(f"MASQUERADE {kinds['masquerade']} != {nat_count}")
    if kinds["snat"] != 1:
        problems.append(f"러너 SNAT {kinds['snat']} != 1")
    if kinds["chain"] != 2:
        problems.append(f"전용 체인 선언 {kinds['chain']} != 2")
    return problems


def eui64_link_local(mac: str) -> str:
    """커널 기본(addr_gen_mode eui64)이 이 MAC 에 붙이는 IPv6 링크 로컬 주소."""
    octets = [int(part, 16) for part in mac.split(":")]
    if len(octets) != 6:
        raise ValueError(mac)
    octets[0] ^= 0x02
    eui = octets[:3] + [0xFF, 0xFE] + octets[3:]
    return str(ipaddress.IPv6Address(bytes([0xFE, 0x80] + [0] * 6 + eui)))


def check_ipv6(inventory: dict, state_text: str) -> tuple[list[str], list[str]]:
    """이 테스트베드 브리지마다 disable_ipv6=1, IPv6 주소 0 인지. (문제, 브리지별 참고 줄)."""
    bridges = [network["bridge"] for network in inventory["networks"].values()]
    seen: dict[str, list[str]] = {}
    problems: list[str] = []
    for raw in state_text.splitlines():
        parts = raw.split()
        if not parts:
            continue
        if len(parts) != 4:
            problems.append(f"IPv6 상태 줄 꼴이 다르다(이름 disable_ipv6 주소수 MAC): {raw.strip()}")
            continue
        if parts[0] in seen:
            problems.append(f"IPv6 상태에 {parts[0]} 이 두 번 있다")
        seen[parts[0]] = parts
    notes: list[str] = []
    for bridge in bridges:
        if bridge not in seen:
            problems.append(f"IPv6 상태에 {bridge} 줄이 없다")
            continue
        _, disabled, count, mac = seen[bridge]
        if disabled == "-":
            problems.append(f"{bridge} 가 없다(IPv6 상태를 뜰 때 브리지가 올라와 있지 않았다)")
            continue
        if disabled != "1":
            problems.append(f"{bridge} IPv6 가 켜져 있다(disable_ipv6={disabled})")
        if count != "0":
            problems.append(f"{bridge} 에 IPv6 주소 {count} 개")
        try:
            notes.append(f"{bridge} disable_ipv6=1, IPv6 주소 0, 켜져 있었다면 생겼을 링크 로컬 {eui64_link_local(mac)}")
        except ValueError:
            problems.append(f"{bridge} MAC 꼴이 다르다: {mac}")
    return problems, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--mode", required=True, choices=["up", "down", "ipv6"])
    parser.add_argument("--before", type=Path, help="up, down 에서 반드시")
    parser.add_argument("--after", type=Path, help="up, down 에서 반드시")
    parser.add_argument("--ipv6-state", type=Path,
                        help="up, ipv6 에서 반드시: 66 에서 render.py --hypervisor-commands ipv6-state 명령을 돌린 출력")
    args = parser.parse_args(argv)
    if args.mode in ("up", "ipv6") and args.ipv6_state is None:
        parser.error(f"--mode {args.mode} 은 --ipv6-state 가 있어야 한다(브리지 IPv6 끔 확인을 빼고 PASS 를 내지 않는다)")
    if args.mode == "down" and args.ipv6_state is not None:
        parser.error("--ipv6-state 는 --mode up 에서만 iptables 와 함께 쓰고 --mode ipv6 는 그것만 본다(되돌린 뒤에는 브리지가 없다)")
    if args.mode in ("up", "down") and (args.before is None or args.after is None):
        parser.error(f"--mode {args.mode} 은 --before, --after 가 있어야 한다")
    if args.mode == "ipv6" and (args.before is not None or args.after is not None):
        parser.error("--mode ipv6 는 iptables 를 견주지 않는다(--before, --after 를 주지 않는다)")
    inventory = json.loads(args.inventory.read_text())
    problems = [] if args.mode == "ipv6" else check(inventory, args.mode, args.before.read_text(), args.after.read_text())
    notes: list[str] = []
    if args.ipv6_state is not None:
        ipv6_problems, notes = check_ipv6(inventory, args.ipv6_state.read_text())
        problems += ipv6_problems
    for problem in problems:
        print(f"FAIL {problem}")
    if not problems:
        for note in notes:
            print(f"IPV6 {note}")
        print(f"PASS {args.mode}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
