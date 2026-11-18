#!/usr/bin/env python3
"""66 Proxmox 에서 VM 을 만들고 이름을 바꾸고 스냅숏을 뜨는 명령을 인벤토리로 찍는다. 실행하지 않는다.

설계: docs/spec-testbed-vm-3tier.md §2-2(크기), §2-1(브리지), §8(T2 끝 스냅숏), §9(되돌리기).
적용은 지휘 세션이 맡긴 적용 에이전트가 runbook(docs/runbook-civil-petition-apply.md) 순서로, 66 잠금과
녹화 정지 창 아래에서 이 출력을 검토한 뒤 66 에 넘긴다. 이 스크립트는 66 에 접속하지 않는다.

단계:
  precheck  VMID 가 비어 있고 브리지가 있고 템플릿이 있는지 보는 읽기 명령
  create    1단계: 게스트 호스트 이름을 Proxmox 이름으로 써서 복제한다(cloud-init 이 Proxmox 이름을 게스트
            호스트 이름으로 쓰므로, 처음부터 게스트 이름으로 만들어 "t4" 같은 이름이 게스트에 남지 않게 한다)
  rename    2단계: 설치 단계가 cloud-init 을 끈 뒤 Proxmox 이름을 pve_name 으로 바꾼다
  snapshot  설치 단계 끝(T 는 T2, S 는 ST2) 재해 복구용 스냅숏(설계 §8, 녹화마다 되돌리지 않는다). 이름과 설명에
            그 단계를 적는다(S VM 에 "T2" 스냅숏이 남지 않게)
  (NIC 이름: 새 VM 은 cloud-init 이 MAC 맞춤과 set-name 으로 eth0 ~ 을 준다. 인벤토리 이름 ens18 ~ 으로는
            설치 단계 network 가 바꾼다. MAC 은 Proxmox 가 정하고, netN 과 ipconfigN 을 같은 번호로 주므로 설치
            단계가 인벤토리 주소를 가진 NIC 에서 읽는다. 여기서 MAC 을 정하지 않는다)
  nics      읽기: 인벤토리 NIC 순서대로 "호스트 NIC 이름 주소 MAC" 을 찍는 명령(MAC 은 qm config 의 netN,
            소문자). 설치 단계 verify-host 가 찍는 "nic <이름> = <장치> <MAC>" 과 서버마다 대조한다
  destroy   되돌리기. **사람 승인 뒤 사람이 66 에서 직접** 실행한다. 이 출력은 VMID 와 이름이 인벤토리와
            모두 맞을 때만 지우는 꼴로 찍힌다.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import shlex
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import render  # noqa: E402

# 스냅숏을 뜨는 단계 이름. 인벤토리 testbed 값마다 다르다(T 의 T2, S 의 ST2).
SNAPSHOT_STAGE = {"t": "T2", "s": "ST2"}


def snapshot_stage(inventory: dict) -> str:
    try:
        return SNAPSHOT_STAGE[inventory["testbed"]]
    except KeyError as error:
        raise render.InventoryError(f"스냅숏 단계를 모르는 testbed 값 {inventory.get('testbed')!r}") from error


def _prefix(inventory: dict, net: str) -> int:
    return ipaddress.ip_network(inventory["networks"][net]["cidr"]).prefixlen


def precheck(inventory: dict) -> list[str]:
    hv = inventory["hypervisor"]
    lines = ["set -eu", f"qm config {hv['template']} | grep -q '^template: 1'"]
    for host in inventory["hosts"]:
        lines.append(f"if qm status {host['vmid']} >/dev/null 2>&1; then echo 'VMID {host['vmid']} 이미 있음' >&2; exit 1; fi")
    for network in inventory["networks"].values():
        lines.append(f"ip link show {network['bridge']} >/dev/null")
    lines.append(f"pvesm status --storage {hv['storage']} | grep -q active")
    return lines


def create(inventory: dict, sshkeys_path: str) -> list[str]:
    hv = inventory["hypervisor"]
    operator = inventory["accounts"]["operator"]["name"]
    nameserver = " ".join(inventory.get("dns", []))
    lines = ["set -eu"]
    for host in inventory["hosts"]:
        vmid = host["vmid"]
        lines.append(f"# {host['name']} ({host['role']})")
        lines.append(f"qm clone {hv['template']} {vmid} --name {host['name']} --full 1 --storage {hv['storage']}")
        lines.append(f"qm set {vmid} --cores {host['vcpu']} --memory {host['memory_mb']} --onboot 1")
        disks = host["disks"]
        lines.append(f"qm resize {vmid} scsi0 {disks[0]['size_gb']}G")
        for index, disk in enumerate(disks[1:], start=1):
            option = f"{hv['storage']}:{disk['size_gb']},discard=on,iothread=1,ssd=1"
            if disk.get("serial"):
                option += f",serial={disk['serial']}"
            lines.append(f"qm set {vmid} --scsi{index} {option}")
        nets = []
        ipconfigs = []
        for index, nic in enumerate(host["nics"]):
            network = inventory["networks"][nic["net"]]
            nets.append(f"--net{index} virtio,bridge={network['bridge']}")
            ip = f"ip={nic['addr']}/{_prefix(inventory, nic['net'])}"
            if nic.get("gateway"):
                ip += f",gw={network['gateway']}"
            ipconfigs.append(f"--ipconfig{index} {ip}")
        lines.append(f"qm set {vmid} {' '.join(nets)}")
        lines.append(f"qm set {vmid} {' '.join(ipconfigs)}")
        lines.append(
            f"qm set {vmid} --ciuser {operator} --sshkeys {shlex.quote(sshkeys_path)} "
            f"--nameserver {shlex.quote(nameserver)} --ciupgrade 0"
        )
        lines.append(f"qm start {vmid}")
    return lines


def nics(inventory: dict) -> list[str]:
    lines = ["set -eu"]
    for host in inventory["hosts"]:
        for index, nic in enumerate(host["nics"]):
            mac = (f"$(qm config {host['vmid']} | sed -n 's/^net{index}: virtio=\\([^,]*\\),.*/\\1/p' "
                   f"| tr A-F a-f)")
            lines.append(f"printf '%s %s %s %s\\n' {host['name']} {nic['ifname']} {nic['addr']} \"{mac}\"")
    return lines


def rename(inventory: dict) -> list[str]:
    lines = ["set -eu"]
    for host in inventory["hosts"]:
        lines.append(f"test \"$(qm config {host['vmid']} | sed -n 's/^name: //p')\" = {host['name']}")
        lines.append(f"qm set {host['vmid']} --name {host['pve_name']}")
    return lines


def snapshot(inventory: dict) -> list[str]:
    stage = snapshot_stage(inventory)
    return ["set -eu"] + [
        f"qm snapshot {host['vmid']} {stage.lower()}-baseline --description '{stage} end, disaster recovery only'"
        for host in inventory["hosts"]
    ]


def destroy(inventory: dict) -> list[str]:
    lines = [
        "# 사람 승인 뒤 사람이 66 에서 직접 실행한다. 에이전트는 실행하지 않는다.",
        "# VMID 와 이름이 인벤토리와 맞지 않으면 그 VM 은 건드리지 않는다.",
        "set -eu",
    ]
    for host in inventory["hosts"]:
        vmid = host["vmid"]
        lines.append(
            f"name=$(qm config {vmid} 2>/dev/null | sed -n 's/^name: //p'); "
            f"if [ \"$name\" = {host['pve_name']} ] || [ \"$name\" = {host['name']} ]; then "
            f"qm stop {vmid} || true; qm destroy {vmid} --purge 1; "
            f"else echo \"VMID {vmid} 이름이 $name 이라 건너뜀\" >&2; fi"
        )
    hv = inventory["hypervisor"]
    down = " ".join(network["bridge"] for network in reversed(list(inventory["networks"].values())))
    lines.append(f"# 66 망 되돌리기(VM 을 지운 뒤): 파일을 지우기 전에 ifdown {down}, 그 다음 rm {hv['file']} "
                 f"{render.rules_path(inventory)}. ifreload -a 는 쓰지 않는다(runbook §4)")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--phase", required=True, choices=["precheck", "create", "nics", "rename", "snapshot", "destroy"])
    parser.add_argument("--sshkeys", default="",
                        help="66 위 운영자 공개 키 파일 경로(create 에 필요). 적용 때 인벤토리의 pubkey_file 을 66 으로 복사한 경로")
    args = parser.parse_args(argv)
    inventory = json.loads(args.inventory.read_text())
    try:
        render.validate(inventory)
        snapshot_stage(inventory)
    except render.InventoryError as error:
        print(f"plan: {error}", file=sys.stderr)
        return 2
    if args.phase == "create" and not args.sshkeys:
        print("plan: create 는 --sshkeys 가 필요함", file=sys.stderr)
        return 2
    phases = {
        "precheck": lambda: precheck(inventory),
        "create": lambda: create(inventory, args.sshkeys),
        "nics": lambda: nics(inventory),
        "rename": lambda: rename(inventory),
        "snapshot": lambda: snapshot(inventory),
        "destroy": lambda: destroy(inventory),
    }
    print("\n".join(phases[args.phase]()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
