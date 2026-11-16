#!/usr/bin/env python3
"""net-snmp pass_persist 도우미: IF-MIB ifOutDiscards(.1.3.6.1.2.1.2.2.1.19) 열을 대신 답한다.

설계 정본: docs/spec-testbed-vm-3tier.md GT0 결정 4, §5 "큐 버림 보고".

리눅스는 qdisc 가 버린 패킷을 인터페이스 카운터(/proc/net/dev 의 drop)에 세지 않는다(N1 커널 시험).
실제 장비는 출력 큐가 넘쳐 버린 패킷을 ifOutDiscards 로 센다. 그래서 라우터의 하위 NIC(설정의
augment 목록)에 대해 이 열을 "커널 송신 버림 + 큐 계열 qdisc 의 넘침 버림" 으로 보고한다. 목록 밖
NIC 는 커널 송신 버림만 보고한다(내장 값과 같다).

세는 규칙(qdisc 나무):
- netem 과 그 조상 qdisc 의 버림은 세지 않는다. netem 의 무작위 손실은 실제 장비에서 송신 버림이 아니라
  상대 쪽 수신 오류로 남는 장애이고(결정 4), 부모 qdisc 는 자식이 버린 수를 자기 drops 에도 더한다
  (prio 아래 netem 의 손실이 prio 의 drops 에 올라간다, 커널 시험으로 확인).
- 나머지 qdisc 는 자기 몫(자기 drops 에서 바로 아래 자식 qdisc 들의 drops 를 뺀 값)을 센다. 부모가
  자식의 버림을 함께 세므로 이렇게 해야 한 패킷이 두 번 세지지 않는다. ingress, clsact 는 송신 큐가 아니라 뺀다.
- 카운터는 줄지 않는다. qdisc 가 사라지거나(되돌림), 다시 만들어져 0 부터 세거나, netem 의 조상이 되어
  더는 세지 않게 될 때, 그때까지 센 값을 따로 쌓아 둔다. 세지 않던 qdisc 가 다시 세게 되면 그 시점의
  값을 기준으로 삼아 그 뒤에 는 것만 센다. 마지막으로 낸 값보다 작은 값은 내지 않는다.
- 상태는 /run 아래에 두어 도우미가 다시 떠도 이어지고, 재부팅 때는 0 부터 시작한다(실제 장비의 재부팅과 같다).

표는 Counter32 이므로 2^32 로 나눈 나머지를 낸다.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

BASE_OID = ".1.3.6.1.2.1.2.2.1.19"
COUNTER32 = 2 ** 32
CACHE_SEC = 1.0
NOT_TX_QUEUES = {"ingress", "clsact"}


def run_json(argv: list[str]) -> list:
    done = subprocess.run(argv, capture_output=True, text=True, timeout=5, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)}: {done.stderr.strip()}")
    return json.loads(done.stdout or "[]")


def read_links() -> list[dict]:
    return run_json(["ip", "-j", "-s", "link", "show"])


def read_qdiscs() -> list[dict]:
    return run_json(["tc", "-s", "-j", "qdisc", "show"])


def _major(handle: str | None) -> str | None:
    """'1:' 이나 '1:1' 에서 qdisc 핸들 '1:' 을 꺼낸다."""
    if not handle:
        return None
    return handle.split(":", 1)[0] + ":"


def qdisc_shares(qdiscs: list[dict], device: str) -> dict[tuple, tuple[int, bool]]:
    """한 장치의 qdisc 마다 (자기 몫 버림, 세는가) 를 낸다. 키는 (핸들, 종류, 부모)."""
    mine = [q for q in qdiscs if q.get("dev") == device and q.get("kind") not in NOT_TX_QUEUES]
    by_handle = {q.get("handle"): q for q in mine}
    parent_of: dict[str, str | None] = {}
    children: dict[str, list[dict]] = {}
    for q in mine:
        parent = None if q.get("root") else _major(q.get("parent"))
        if parent not in by_handle:
            parent = None
        parent_of[q.get("handle")] = parent
        if parent:
            children.setdefault(parent, []).append(q)
    excluded: set[str] = set()
    for q in mine:
        if q.get("kind") == "netem":
            handle = q.get("handle")
            while handle:
                excluded.add(handle)
                handle = parent_of.get(handle)
    shares: dict[tuple, tuple[int, bool]] = {}
    for q in mine:
        handle = q.get("handle")
        own = int(q.get("drops", 0)) - sum(int(c.get("drops", 0)) for c in children.get(handle, []))
        key = (handle, q.get("kind"), q.get("parent") or "root")
        shares[key] = (max(0, own), handle not in excluded)
    return shares


class Accumulator:
    """장치별 넘침 버림 누적. 상태는 JSON 하나에 둔다."""

    def __init__(self, state_path: Path | None) -> None:
        self.state_path = state_path
        self.devices: dict[str, dict] = {}
        if state_path and state_path.exists():
            try:
                self.devices = json.loads(state_path.read_text())["devices"]
            except (OSError, ValueError, KeyError):
                self.devices = {}

    def update(self, device: str, shares: dict[tuple, tuple[int, bool]]) -> int:
        state = self.devices.setdefault(device, {"retired": 0, "qdiscs": {}, "published": 0})
        known: dict[str, dict] = state["qdiscs"]
        current = {json.dumps(list(key)): value for key, value in shares.items()}
        for key in list(known):
            entry = known[key]
            if key not in current:
                state["retired"] += entry["counted"]
                del known[key]
        for key, (own, counting) in current.items():
            entry = known.get(key)
            if entry is None:
                # 처음 보는 qdisc. 세는 것이면 0 부터(새로 만든 qdisc 는 0 에서 시작한다).
                known[key] = {"baseline": 0 if counting else own, "counted": 0, "last": own, "counting": counting}
                entry = known[key]
            elif own < entry["last"]:
                # 같은 키로 다시 만들어져 0 부터 센다: 지금까지 센 것을 쌓고 새로 시작한다.
                state["retired"] += entry["counted"]
                entry.update({"baseline": 0, "counted": 0})
            if entry["counting"] and not counting:
                state["retired"] += entry["counted"]
                entry["counted"] = 0
            elif not entry["counting"] and counting:
                entry["baseline"] = own
                entry["counted"] = 0
            entry["counting"] = counting
            entry["last"] = own
            if counting:
                entry["counted"] = max(0, own - entry["baseline"])
        total = state["retired"] + sum(entry["counted"] for entry in known.values())
        total = max(total, state["published"])
        state["published"] = total
        return total

    def save(self) -> None:
        if not self.state_path:
            return
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"devices": self.devices}, sort_keys=True))
        os.replace(temporary, self.state_path)


class Table:
    def __init__(self, augment: list[str], accumulator: Accumulator, links=read_links, qdiscs=read_qdiscs) -> None:
        self.augment = set(augment)
        self.accumulator = accumulator
        self.links = links
        self.qdiscs = qdiscs
        self.cached_at = -1e9
        self.rows: dict[int, int] = {}

    def refresh(self, now: float | None = None) -> dict[int, int]:
        now = time.monotonic() if now is None else now
        if now - self.cached_at < CACHE_SEC:
            return self.rows
        links = self.links()
        qdiscs = self.qdiscs() if self.augment else []
        rows: dict[int, int] = {}
        for link in links:
            tx_dropped = int(link.get("stats64", {}).get("tx", {}).get("dropped", 0))
            value = tx_dropped
            if link["ifname"] in self.augment:
                value += self.accumulator.update(link["ifname"], qdisc_shares(qdiscs, link["ifname"]))
            rows[int(link["ifindex"])] = value % COUNTER32
        if self.augment:
            self.accumulator.save()
        self.rows = rows
        self.cached_at = now
        return rows

    def get(self, oid: str) -> tuple[str, int] | None:
        index = _index(oid)
        if index is None:
            return None
        rows = self.refresh()
        if index not in rows:
            return None
        return f"{BASE_OID}.{index}", rows[index]

    def getnext(self, oid: str) -> tuple[str, int] | None:
        rows = self.refresh()
        if oid == BASE_OID or not oid.startswith(BASE_OID + "."):
            if _before(oid, BASE_OID):
                after = -1
            else:
                return None
        else:
            index = _index(oid)
            if index is None:
                # .19.N.M 같은 더 깊은 OID: N 다음부터.
                head = oid[len(BASE_OID) + 1:].split(".", 1)[0]
                after = int(head) if head.isdigit() else -1
            else:
                after = index
        later = sorted(i for i in rows if i > after)
        if not later:
            return None
        return f"{BASE_OID}.{later[0]}", rows[later[0]]


def _index(oid: str) -> int | None:
    if not oid.startswith(BASE_OID + "."):
        return None
    rest = oid[len(BASE_OID) + 1:]
    return int(rest) if rest.isdigit() else None


def _before(oid: str, base: str) -> bool:
    def parts(text: str) -> list[int]:
        return [int(p) for p in text.strip(".").split(".") if p.isdigit()]
    return parts(oid) <= parts(base)


def serve(table: Table, stdin=sys.stdin, stdout=sys.stdout) -> None:
    """pass_persist 규약: PING/PONG, get, getnext, set(쓰기 불가)."""
    while True:
        line = stdin.readline()
        if not line:
            return
        command = line.strip()
        if command == "PING":
            stdout.write("PONG\n")
        elif command in ("get", "getnext"):
            oid = stdin.readline().strip()
            try:
                answer = table.get(oid) if command == "get" else table.getnext(oid)
            except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired):
                answer = None
            if answer is None:
                stdout.write("NONE\n")
            else:
                stdout.write(f"{answer[0]}\ncounter\n{answer[1]}\n")
        elif command == "set":
            stdin.readline()
            stdin.readline()
            stdout.write("not-writable\n")
        elif command == "":
            return
        else:
            stdout.write("NONE\n")
        stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--dump", action="store_true", help="표를 한 번 출력하고 끝낸다(점검용)")
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text())
    state = Path(config["state"]) if config.get("state") else None
    if state:
        state.parent.mkdir(parents=True, exist_ok=True)
    table = Table(config.get("augment", []), Accumulator(state))
    if args.dump:
        for index, value in sorted(table.refresh().items()):
            print(f"{BASE_OID}.{index} {value}")
        return 0
    serve(table)
    return 0


if __name__ == "__main__":
    sys.exit(main())
