#!/usr/bin/env python3
"""T 와 S 의 짝 서버가 같은 패키지 판을 쓰는지 본다(S 문서 §2-4 쌍 조건 2, §11 ST2 "쌍 조건 1, 2").

짝은 두 인벤토리에서 같은 역할의 같은 순번이다(web 첫째와 web 첫째, was 둘째와 was 둘째 ...). 라우터처럼 한쪽에만 있는
역할(T 의 라우터)은 짝이 없어 보지 않고, S 에만 있는 역할 순번은 짝 없음으로 FAIL 이다. 입력은 서버마다 지금 목록 하나
(`dpkg-query -W -f '${db:Status-Abbrev} ${Package} ${Version}\\n'`, 설치 상태 칸 포함)를
`<디렉터리>/<게스트 호스트 이름>.txt` 로 둔 것이다(설치 단계 os-base 가 남기는 packages.txt 는 그 단계 끝의 목록이라
뒤 단계가 깐 것과 올린 것이 빠진다. 그래서 설치가 끝난 뒤 새로 뜬다).

  pair_packages.py --t-inventory t.json --s-inventory s.json --t-dir <T 목록> --s-dir <S 목록> \\
      [--allow-t-only <이름> ...] [--allow-version <이름> ...]

판정: 짝마다 설치된(상태 ii) 패키지는 양쪽에 다 있고 판이 같다. 상태가 ii 가 아닌 줄(설정 파일만 남은 rc 등)은 양쪽의
상태와 판이 같아야 한다(설치된 것처럼 견주지 않는다). T 에만 도는 단계(lldpd 같은, S 문서 §2-3
"빼는 것")가 깔거나 올린 패키지는 사람이 근거를 보고 --allow-t-only, --allow-version 으로 이름을 준다. 준 이름은 짝마다
NOTE 로 찍는다(그런 차이가 있었다는 기록). 끝 줄 PASS 또는 FAIL, 끝 코드 0 또는 1.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def pairs(t_inventory: dict, s_inventory: dict) -> list[tuple[str, str, str]]:
    """(역할 순번, T 호스트, S 호스트). 역할 순번은 인벤토리 순서대로 센다."""
    def by_role(inventory: dict) -> dict[str, str]:
        out: dict[str, str] = {}
        counter: dict[str, int] = {}
        for host in inventory["hosts"]:
            index = counter.get(host["role"], 0)
            counter[host["role"]] = index + 1
            out[f"{host['role']}{index}"] = host["name"]
        return out
    t, s = by_role(t_inventory), by_role(s_inventory)
    return [(key, t[key], s[key]) for key in t if key in s]


def unpaired_s(t_inventory: dict, s_inventory: dict) -> list[tuple[str, str]]:
    """S 에만 있는 역할 순번(짝 T 서버가 없는 S 서버). 대조 밖으로 조용히 빠지지 않게 따로 낸다."""
    paired = {key for key, _, _ in pairs(t_inventory, s_inventory)}
    counter: dict[str, int] = {}
    out = []
    for host in s_inventory["hosts"]:
        index = counter.get(host["role"], 0)
        counter[host["role"]] = index + 1
        key = f"{host['role']}{index}"
        if key not in paired:
            out.append((key, host["name"]))
    return out


def parse(text: str) -> tuple[dict[str, str], dict[str, str]]:
    """`상태 이름 판` 줄을 (설치된 것 이름: 판, 그 밖 이름: "상태 판") 둘로. 아키텍처 꼬리(:amd64)는 뗀다.
    꼴이 다르거나 같은 이름이 두 번이면 ValueError(상태 칸 없는 옛 꼴도 거절한다)."""
    installed: dict[str, str] = {}
    other: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 3 or not (len(parts[0]) in (2, 3) and parts[0].isalpha()):
            raise ValueError(f"{number}행 꼴이 다르다(상태 이름 판): {line}")
        name = parts[1].split(":")[0]
        if name in installed or name in other:
            raise ValueError(f"{number}행 {name} 이 두 번 있다")
        if parts[0] == "ii":
            installed[name] = parts[2]
        else:
            other[name] = f"{parts[0]} {parts[2]}"
    return installed, other


def compare(t_list: dict[str, str], s_list: dict[str, str], allow_t_only: set[str],
            allow_version: set[str]) -> tuple[list[str], list[str]]:
    """(문제, 기록). 문제가 없으면 이 짝은 같은 판이다."""
    problems: list[str] = []
    notes: list[str] = []
    for name in sorted(set(t_list) | set(s_list)):
        if name in t_list and name in s_list:
            if t_list[name] == s_list[name]:
                continue
            line = f"판이 다름 {name} T {t_list[name]} S {s_list[name]}"
            (notes if name in allow_version else problems).append(line)
        elif name in t_list:
            line = f"T 에만 있음 {name} {t_list[name]}"
            (notes if name in allow_t_only else problems).append(line)
        else:
            problems.append(f"S 에만 있음 {name} {s_list[name]}")
    return problems, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--t-inventory", required=True, type=Path)
    parser.add_argument("--s-inventory", required=True, type=Path)
    parser.add_argument("--t-dir", required=True, type=Path)
    parser.add_argument("--s-dir", required=True, type=Path)
    parser.add_argument("--allow-t-only", action="append", default=[], help="T 에만 있어도 되는 패키지 이름(여러 번)")
    parser.add_argument("--allow-version", action="append", default=[], help="판이 달라도 되는 패키지 이름(여러 번)")
    args = parser.parse_args(argv)
    t_inventory = json.loads(args.t_inventory.read_text())
    s_inventory = json.loads(args.s_inventory.read_text())
    failed = 0
    for key, s_host in unpaired_s(t_inventory, s_inventory):
        print(f"FAIL {key} {s_host} 짝 T 서버가 없다(같은 역할 순번이 T 에 없음)")
        failed += 1
    for key, t_host, s_host in pairs(t_inventory, s_inventory):
        try:
            t_list, t_other = parse((args.t_dir / f"{t_host}.txt").read_text())
            s_list, s_other = parse((args.s_dir / f"{s_host}.txt").read_text())
        except (OSError, ValueError) as error:
            print(f"FAIL {key} {t_host} {s_host} 목록을 읽지 못함: {error}")
            failed += 1
            continue
        problems, notes = compare(t_list, s_list, set(args.allow_t_only), set(args.allow_version))
        for name in sorted(set(t_other) | set(s_other)):
            if t_other.get(name) != s_other.get(name):
                problems.append(f"설치 상태가 ii 가 아닌 줄이 다름 {name} T {t_other.get(name, '없음')} S {s_other.get(name, '없음')}")
        same = sum(1 for name in t_list if s_list.get(name) == t_list[name])
        print(f"PAIR {key} {t_host} {s_host} 같은 판 {same} 문제 {len(problems)}")
        for note in notes:
            print(f"NOTE {key} {note}")
        for problem in problems:
            print(f"FAIL {key} {problem}")
        failed += bool(problems)
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
