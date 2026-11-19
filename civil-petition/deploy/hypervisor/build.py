#!/usr/bin/env python3
"""66 root 도우미 설치본(t4-hv-exec-root)을 틀(t4-hv-exec-root.in), U8 가드 함수, 인벤토리로 만든다.

가드 함수의 정본은 scripts/scenarios/profiles/executor_common.py 의 GUARD_FUNCTIONS 하나다(설계 §6-1 "U8 시험과의 연결").
허용 표(VMID, NIC, tap)와 probe 주소는 인벤토리에서 채운다: 라우터의 하위 NIC(downlink), 서버(웹, WAS, DB)의 기본 경로 NIC,
라우터 상향 주소와 서버 업무 주소. 설치본은 그 고정 표를 가진 채 66 에 놓인다(설계 §3-5 규칙 2 "코드 안 고정 표"). 주소와
이름이 든 설치본은 저장소에 두지 않고(설계 §2-5, 설치 스크립트와 틀에 사이트 값 없음) 적용 때 만든다. 저장소에는 T 인벤토리로
만든 두 파일의 sha256 만 둔다(t4-hv-exec.sha256.json, 러너 preflight 의 버전 표류 기대값, 설계 §6-4).

  build.py --inventory <인벤토리> --out <파일>     설치본을 만든다
  build.py --inventory <인벤토리> --sha256         설치될 두 파일의 sha256 을 JSON 한 줄로 낸다
  build.py --check                                 t4-hv-exec.sha256.json 이 T 인벤토리로 만든 것과 같은지 본다(다르면 끝 코드 1)
  build.py --write-sha256                          t4-hv-exec.sha256.json 을 T 인벤토리로 다시 쓴다
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import ipaddress
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
PROFILES = REPO / "scripts" / "scenarios" / "profiles"
TEMPLATE = HERE / "t4-hv-exec-root.in"
FRONT = HERE / "t4-hv-exec"
SUMS = HERE / "t4-hv-exec.sha256.json"
T_INVENTORY = HERE.parent / "inventory" / "t.json"
SERVER_ROLES = ("web", "was", "db")


def guard_functions() -> bytes:
    sys.path.insert(0, str(PROFILES))
    spec = importlib.util.spec_from_file_location("executor_common", PROFILES / "executor_common.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.GUARD_FUNCTIONS


def tables(inv: dict) -> tuple[list[str], list[str]]:
    """(허용 표 줄 "<vmid> net<i> tap<vmid>i<i>", probe 주소). Proxmox 는 VM 의 net<i> 를 tap<vmid>i<i> 로 만든다."""
    mgmt = {name for name, net in inv["networks"].items() if net.get("management")}
    links, probes = [], []
    router = [h for h in inv["hosts"] if h["role"] == "router"]
    servers = [h for h in inv["hosts"] if h["role"] in SERVER_ROLES]
    for host in router + servers:
        for i, nic in enumerate(host["nics"]):
            if nic["net"] in mgmt:
                continue
            allowed = nic.get("downlink") if host["role"] == "router" else nic.get("gateway")
            if allowed:
                links.append(f"{host['vmid']} net{i} tap{host['vmid']}i{i}")
    for host in router:
        probes += [nic["addr"] for nic in host["nics"] if nic.get("gateway")]
    for host in servers:
        probes += [nic["addr"] for nic in host["nics"] if nic.get("gateway") and nic["net"] not in mgmt]
    if not links or not probes:
        raise SystemExit("인벤토리에 하이퍼바이저 쪽 링크가 없다(라우터 하위 NIC, 서버 업무 NIC)")
    return links, probes


def render(inv: dict) -> bytes:
    text = TEMPLATE.read_text(encoding="utf-8")
    for mark in ("@GUARD_FUNCTIONS@\n", "@LINKS@", "@PROBES@", "@MONITOR@", "@TUNNEL_SRC@"):
        if text.count(mark) != 1:
            raise SystemExit(f"{TEMPLATE}: {mark.strip()} 자리가 하나가 아님")
    links, probes = tables(inv)
    monitor = inv["monitoring"]["address"]
    ends = [str(a) for a in ipaddress.ip_network(inv["hypervisor"]["tunnel_cidr"]).hosts() if str(a) != monitor]
    if len(ends) != 1:
        raise SystemExit("터널 대역에서 66 끝을 하나로 정할 수 없다")
    text = (text.replace("@LINKS@", "\n".join(links)).replace("@PROBES@", " ".join(probes))
            .replace("@MONITOR@", monitor).replace("@TUNNEL_SRC@", ends[0]))
    return text.encode().replace(b"@GUARD_FUNCTIONS@\n", guard_functions())


def sums(inv: dict) -> dict:
    sha = lambda data: hashlib.sha256(data).hexdigest()  # noqa: E731
    return {"files": {"t4-hv-exec": sha(FRONT.read_bytes()), "t4-hv-exec-root": sha(render(inv))}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--inventory", type=Path, default=T_INVENTORY)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--sha256", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--write-sha256", action="store_true")
    args = parser.parse_args()
    inv = json.loads(args.inventory.read_text(encoding="utf-8"))
    if args.sha256:
        print(json.dumps(sums(inv), sort_keys=True))
        return 0
    if args.check or args.write_sha256:
        expected = json.dumps(sums(json.loads(T_INVENTORY.read_text(encoding="utf-8"))), sort_keys=True, indent=2) + "\n"
        if args.write_sha256:
            SUMS.write_text(expected)
            return 0
        if not SUMS.exists() or SUMS.read_text() != expected:
            print(f"{SUMS.name} 가 지금 틀, 가드 함수, T 인벤토리로 만든 것과 다르다. build.py --write-sha256", file=sys.stderr)
            return 1
        return 0
    if not args.out:
        parser.error("--out, --sha256, --check, --write-sha256 중 하나")
    args.out.write_bytes(render(inv))
    args.out.chmod(0o755)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
