#!/usr/bin/env python3
"""66 에 망 파일을 적용하고 되돌릴 때 iptables 가 이 테스트베드 몫만 바뀌었는지 본다.

설계 docs/spec-testbed-vm-3tier.md §10 T2 "기존 테스트베드 영향 없음", runbook §4.
적용 전후에 `iptables-save` 를 떠서 준다. 같은 규칙이 두 번 생긴 것도 잡으려고 줄을 집합이 아니라 개수로 센다
(66 의 ifupdown2 는 reload 때 기존 tb-nat 의 post-up 을 다시 돌려 확인 없는 `-A` 규칙을 한 벌 더 만든다).

  hv_iptables_check.py --inventory t.json --mode up   --before 적용전.txt --after 적용후.txt
  hv_iptables_check.py --inventory t.json --mode down --before 적용전.txt --after 되돌린뒤.txt

up:   지워진 줄 0, 늘어난 줄은 전용 체인(선언, 규칙, 훅)과 이 인벤토리의 MASQUERADE, 러너 SNAT 뿐이고
      MASQUERADE 수는 NAT 대역 수, SNAT 은 1.
down: 적용 전과 줄과 개수가 같다.
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
    nat_count = sum(1 for v in inventory["networks"].values() if v.get("nat"))
    if kinds["masquerade"] != nat_count:
        problems.append(f"MASQUERADE {kinds['masquerade']} != {nat_count}")
    if kinds["snat"] != 1:
        problems.append(f"러너 SNAT {kinds['snat']} != 1")
    if kinds["chain"] != 2:
        problems.append(f"전용 체인 선언 {kinds['chain']} != 2")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--mode", required=True, choices=["up", "down"])
    parser.add_argument("--before", required=True, type=Path)
    parser.add_argument("--after", required=True, type=Path)
    args = parser.parse_args(argv)
    problems = check(json.loads(args.inventory.read_text()), args.mode, args.before.read_text(), args.after.read_text())
    for problem in problems:
        print(f"FAIL {problem}")
    if not problems:
        print(f"PASS {args.mode}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
