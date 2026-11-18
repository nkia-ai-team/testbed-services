#!/usr/bin/env python3
"""인벤토리 하나로 서버별 설치 묶음과 하이퍼바이저 망 파일을 만든다.

설계 정본: docs/spec-testbed-vm-3tier.md §2-5 (S 문서 spec-testbed-sms-only.md 결정 5).
T 와 S 두 테스트베드가 같은 설정 틀(templates/)과 같은 설치 단계(steps/)를 쓴다. 다른 것은
인벤토리 파일(inventory/t.json, inventory/s.json)뿐이고, 라우터, lldpd, syslog 전달, 하이퍼바이저
모듈은 인벤토리의 켜기 값(features)으로만 돈다. 주소, 이름, 도메인 값은 설정 틀 본문에 쓰지 않는다.

묶음 꼴(서버 하나, 출력 디렉터리/<호스트 이름>/):
  host.env     설치 단계가 읽는 값(KEY='값' 줄)
  steps        도는 단계 이름, 순서대로
  files.tsv    단계, 놓을 경로, 권한, 소유자, 그룹, 묶음 안 경로
  files/...    렌더된 설정 파일
  meta.json    파일별 체크섬과 정규화 체크섬(주소, 이름 자리를 자리표시로 바꾼 뒤)

비밀값(DB 비밀번호, SNMP 커뮤니티, 공개 키)은 묶음에 넣지 않는다. 설정 틀에는 @@SECRET:<이름>@@
자리만 두고, 적용 스크립트가 인벤토리의 경로에서 읽어 설치 직전에 secrets/ 로 넣는다.

쓰는 법:
  render.py --inventory inventory/t.json --out <디렉터리>            서버별 묶음
  render.py --inventory inventory/t.json --hypervisor <디렉터리>     66 망 파일, chrony 허용, 모듈
  render.py --inventory inventory/t.json --check                      검사만
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "tools"))
import design_load  # noqa: E402  (설계 부하에서 읽어 계산하는 값, tools/design_load.py)
TEMPLATES = HERE / "templates"

PLACEHOLDER = re.compile(r"\{\{\s*([a-z0-9_]+)\s*\}\}")
SECRET = re.compile(r"@@SECRET:([a-z0-9_]+)@@")
# NIC 의 MAC 자리. 설치 단계 network 가 VM 안에서 그 NIC 의 MAC 으로 채운다(steps/network.sh, lib.sh place_files).
MAC_SLOT = re.compile(r"@@MAC:([a-z0-9]+)@@")
# 커널 NIC 이름 꼴(IFNAMSIZ 16 에서 끝 NUL 을 뺀 15자). netplan set-name 이 이보다 길면 이름을 바꾸지 않는다.
IFNAME = re.compile(r"^[a-z][a-z0-9]{0,14}$")
# 데이터 디스크 serial: Proxmox 는 20바이트까지 받는다. db 단계가 lsblk SERIAL 과 글자 그대로 맞추므로 lsblk -r 이 바꿔 쓰는
# 글자(공백 등)를 막고, serial 을 주지 않은 디스크가 보고하는 drive-scsiN 과 겹치지 않게 drive- 로 시작하지 못하게 한다.
DISK_SERIAL = re.compile(r"^(?!drive-)[A-Za-z0-9_-]{1,20}$")
ROLES = ("router", "web", "was", "db", "loadgen")
SERVER_ROLES = ("web", "was", "db")
# 게스트 쪽에 드러나는 이름에 쓰지 않는 낱말(설계 §2-2 원칙 8). Proxmox 이름은 관제 서버로 가지 않아 예외다.
FORBIDDEN_WORDS = ("test", "testbed", "tb", "t4", "s5")
WAR_CONTEXT = "petition"


class InventoryError(ValueError):
    pass


@dataclass(frozen=True)
class FileSpec:
    step: str
    dest: str
    template: str
    mode: str = "0644"
    owner: str = "root"
    group: str = "root"


# 역할별로 놓는 파일. 단계 이름은 steps/<단계>.sh 와 같다.
COMMON_FILES = (
    FileSpec("os-base", "/etc/apt/apt.conf.d/20auto-upgrades", "common/apt-20auto-upgrades"),
    FileSpec("os-base", "/etc/cloud/cloud.cfg.d/99-disable-network-config.cfg", "common/cloud-99-disable-network.cfg"),
    FileSpec("network", "/etc/netplan/01-host.yaml", "common/netplan.yaml", "0600"),
    FileSpec("network", "/usr/local/sbin/netplan-rollback", "common/netplan-rollback", "0755"),
    FileSpec("accounts", "/etc/sudoers.d/60-ops", "common/sudoers-ops", "0440"),
    FileSpec("time", "/etc/chrony/chrony.conf", "common/chrony.conf"),
    FileSpec("syslog", "/etc/rsyslog.conf", "common/rsyslog.conf"),
    FileSpec("syslog", "/etc/rsyslog.d/00-opsjobs.conf", "common/rsyslog-00-opsjobs.conf"),
    FileSpec("syslog", "/etc/apparmor.d/rsyslog.d/annotate-proc", "common/apparmor-rsyslogd-annotate"),
    FileSpec("syslog", "/etc/logrotate.d/opsjobs", "common/logrotate-opsjobs"),
    FileSpec("firewall", "/etc/nftables.conf", "common/nftables.conf", "0755"),
)
SYSLOG_FORWARD_FILES = (
    FileSpec("syslog-forward", "/etc/rsyslog.d/remote/10-forward.conf", "common/rsyslog-forward.conf"),
)
MODULE_FILES = (FileSpec("kernel-modules", "/etc/modules-load.d/qdisc.conf", "common/modules-qdisc.conf"),)
LLDPD_FILES = (FileSpec("lldpd", "/etc/default/lldpd", "common/lldpd-default"),)
# SMS 에이전트(설계 §4, runbook §11). T 와 S 가 같은 자리, 같은 유닛을 쓴다(설계 §2-5 "같은 방식, 같은 에이전트 판").
# 자리와 유닛 이름은 게스트에 드러나므로 테스트베드 낱말을 쓰지 않는다(설계 §2-2).
SMS_AGENT_HOME = "/opt/lucida-agent"
SMS_AGENT_UNIT = "lucida-agent"
SMS_AGENT_RECORD = "/var/lib/petition-deploy/lucida-agent.sha256"
SMS_AGENT_FILES = (
    FileSpec("sms-agent", f"{SMS_AGENT_HOME}/agent.env", "common/lucida-agent.env", "0600"),
    FileSpec("sms-agent", f"/etc/systemd/system/{SMS_AGENT_UNIT}.service", "common/lucida-agent.service"),
)
NON_ROUTER_FILES = (
    FileSpec("network", "/etc/sysctl.d/60-host.conf", "common/sysctl-server.conf"),
    FileSpec("firewall", "/etc/nftables.d/10-mgmt-guard.nft", "common/nft-mgmt-guard.nft"),
)
ROLE_FILES = {
    "router": (
        FileSpec("network", "/etc/sysctl.d/60-host.conf", "router/sysctl-router.conf"),
        FileSpec("firewall", "/etc/nftables.d/10-mgmt-guard.nft", "router/nft-mgmt-guard.nft"),
        FileSpec("firewall", "/etc/nftables.d/20-monitor-notrack.nft", "router/nft-monitor-notrack.nft"),
        FileSpec("firewall", "/etc/nftables.d/30-acl.nft", "router/nft-acl.nft"),
        FileSpec("router", "/etc/snmp/snmpd.conf", "router/snmpd.conf", "0600"),
        FileSpec("router", "/etc/snmp/snmpd.community.conf", "router/snmpd.community.conf", "0600"),
        FileSpec("router", "/etc/systemd/system/snmpd.service.d/10-monitor.conf", "router/snmpd-dropin.conf"),
        FileSpec("router", "/etc/snmp/ifdiscards.json", "router/ifdiscards.json"),
        FileSpec("router", "/etc/tmpfiles.d/ifdiscards.conf", "router/tmpfiles-ifdiscards.conf"),
        FileSpec("router", "/usr/local/lib/ifdiscards/ifdiscards.py", "../router/ifdiscards.py", "0755"),
    ),
    "web": (
        FileSpec("web", "/etc/apache2/sites-available/petition.conf", "web/petition-site.conf"),
        FileSpec("web", "/var/www/petition/index.html", "web/index.html"),
        FileSpec("web", "/var/www/petition/static/portal.css", "web/portal.css"),
    ),
    "was": (
        FileSpec("was", "/etc/default/tomcat10", "was/tomcat10-default"),
        FileSpec("was", "/etc/tomcat10/server.xml", "was/server.xml", "0640", "root", "tomcat"),
        FileSpec("was", "/etc/tomcat10/Catalina/localhost/petition.xml", "was/petition-context.xml", "0640", "root", "tomcat"),
    ),
    "db": (
        FileSpec("db", "/etc/postgresql/16/main/conf.d/05-listen.conf", "db/listen.conf", "0644", "postgres", "postgres"),
        FileSpec("db", "/etc/postgresql/16/main/conf.d/10-tuning.conf", "db/tuning.conf", "0644", "postgres", "postgres"),
        FileSpec("db", "/etc/postgresql/16/main/conf.d/20-logging.conf", "db/logging.conf", "0644", "postgres", "postgres"),
        FileSpec("db", "/etc/postgresql/16/main/pg_hba.conf", "db/pg_hba.conf", "0640", "postgres", "postgres"),
        FileSpec("db-backup", "/usr/local/sbin/pg-backup", "db/pg-backup", "0755"),
        FileSpec("db-backup", "/etc/systemd/system/pg-backup.service", "db/pg-backup.service"),
        FileSpec("db-backup", "/etc/systemd/system/pg-backup.timer", "db/pg-backup.timer"),
        FileSpec("db-backup", "/etc/logrotate.d/pg-backup", "db/logrotate-pg-backup"),
        FileSpec("db-backup", "/usr/local/sbin/pg-table-stats", "db/pg-table-stats", "0755"),
        FileSpec("db-backup", "/etc/systemd/system/pg-table-stats.service", "db/pg-table-stats.service"),
        FileSpec("db-backup", "/etc/systemd/system/pg-table-stats.timer", "db/pg-table-stats.timer"),
    ),
    "loadgen": (
        FileSpec("loadgen", "/etc/systemd/system/loadgen.service", "loadgen/loadgen.service"),
    ),
}
BATCH_FILES = (
    FileSpec("was-batch", "/usr/local/sbin/petition-batch", "was/petition-batch", "0755"),
    FileSpec("was-batch", "/etc/systemd/system/petition-batch@.service", "was/petition-batch@.service"),
    FileSpec("was-batch", "/etc/systemd/system/petition-stats.timer", "was/petition-stats.timer"),
    FileSpec("was-batch", "/etc/systemd/system/petition-archive.timer", "was/petition-archive.timer"),
    FileSpec("was-batch", "/etc/logrotate.d/petition-batch", "was/logrotate-petition-batch"),
)

# 단계 순서. 켜기 값이나 역할에 맞지 않는 단계는 빠진다.
STEP_ORDER = (
    # 모듈 적재(커널이 netem 판 줄을 찍는다)는 원격 전달을 켜기 전에 한다(리뷰 지적 6).
    "verify-host", "os-base", "network", "accounts", "time", "syslog", "kernel-modules", "syslog-forward",
    "firewall", "lldpd",
    "router", "db", "db-schema", "db-seed", "db-backup", "was", "was-batch", "web", "loadgen",
    # SMS 에이전트는 서버 역할(웹, WAS, DB)에만, 역할 설치 뒤에 둔다(설계 §4, runbook §11).
    "sms-agent",
    "verify",
)


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InventoryError(f"인벤토리를 읽지 못함 {path}: {error}") from error


def _net(inventory: dict, name: str) -> dict:
    try:
        return inventory["networks"][name]
    except KeyError as error:
        raise InventoryError(f"없는 망 이름 {name}") from error


def _prefix(cidr: str) -> int:
    return ipaddress.ip_network(cidr).prefixlen


def validate(inventory: dict) -> None:
    """인벤토리의 모양과 서로 맞아야 하는 값을 본다. 어긋나면 렌더 전에 멈춘다."""
    if inventory.get("schema") != "petition-portal-inventory/1":
        raise InventoryError("schema 가 petition-portal-inventory/1 이 아님")
    for key in ("testbed", "domain", "features", "accounts", "monitoring", "time", "app", "networks", "hypervisor", "hosts"):
        if key not in inventory:
            raise InventoryError(f"필수 항목 없음: {key}")
    if inventory["app"].get("context") != WAR_CONTEXT:
        # WAR 파일, Tomcat 문맥 파일, 배포 디렉터리 이름이 이 값으로 고정이다(steps/was.sh). T 와 S 가 같은 값을 쓴다.
        raise InventoryError(f"app.context 는 {WAR_CONTEXT} 이어야 함(WAR 와 Tomcat 문맥 이름이 고정)")
    features = inventory["features"]
    for key in ("router", "lldpd", "syslog_forward", "hypervisor_modules"):
        if not isinstance(features.get(key), bool):
            raise InventoryError(f"features.{key} 는 참 거짓 값이어야 함")
    executor = inventory["accounts"]["executor"]
    operator = inventory["accounts"]["operator"]
    if executor["name"] == operator["name"] or executor["uid"] == operator["uid"]:
        raise InventoryError("실행기 계정과 운영자 계정은 이름과 uid 가 달라야 함(설계 §6-2)")
    names = set()
    vmids = set()
    roles: dict[str, int] = {}
    management = [n for n, v in inventory["networks"].items() if v.get("management")]
    if len(management) != 1:
        raise InventoryError("관리망(management: true)은 하나여야 함")
    for network in inventory["networks"].values():
        ipaddress.ip_network(network["cidr"])
    for host in inventory["hosts"]:
        name = host["name"]
        lowered = name.lower()
        for word in FORBIDDEN_WORDS:
            if re.search(rf"(^|[^a-z]){word}([^a-z]|$)", lowered) or lowered.startswith(word):
                raise InventoryError(f"게스트 호스트 이름 {name} 에 쓰지 않는 낱말 {word}(설계 §2-2)")
        if name in names:
            raise InventoryError(f"호스트 이름 중복: {name}")
        names.add(name)
        if host["vmid"] in vmids:
            raise InventoryError(f"VMID 중복: {host['vmid']}")
        vmids.add(host["vmid"])
        if host["role"] not in ROLES:
            raise InventoryError(f"모르는 역할: {host['role']}")
        roles[host["role"]] = roles.get(host["role"], 0) + 1
        gateways = [nic for nic in host["nics"] if nic.get("gateway")]
        if len(gateways) != 1:
            raise InventoryError(f"{name}: 기본 경로를 가진 NIC 는 하나여야 함")
        mgmt_nics = [nic for nic in host["nics"] if nic["net"] == management[0]]
        if len(mgmt_nics) != 1 or mgmt_nics[0].get("gateway"):
            raise InventoryError(f"{name}: 관리 NIC 는 하나이고 기본 경로가 없어야 함(설계 §3-5 규칙 4)")
        ifnames = set()
        for nic in host["nics"]:
            network = _net(inventory, nic["net"])
            if ipaddress.ip_address(nic["addr"]) not in ipaddress.ip_network(network["cidr"]):
                raise InventoryError(f"{name}: {nic['addr']} 가 {nic['net']} 대역 밖")
            if not IFNAME.match(nic["ifname"]):
                raise InventoryError(f"{name}: NIC 이름 {nic['ifname']} 이 커널 이름 꼴(소문자로 시작, 15자 이하)이 아님")
            if nic["ifname"] in ifnames:
                raise InventoryError(f"{name}: NIC 이름 중복 {nic['ifname']}")
            ifnames.add(nic["ifname"])
        db_disks = [disk for disk in host.get("disks", []) if disk.get("mount")]
        if host["role"] == "db" and len(db_disks) != 1:
            raise InventoryError(f"{name}: DB 는 데이터 디스크 하나를 따로 가져야 함(설계 §2-2)")
        for disk in db_disks:
            if not DISK_SERIAL.match(str(disk.get("serial", ""))):
                raise InventoryError(f"{name}: 데이터 디스크 serial {disk.get('serial')!r} 이 꼴(영문, 숫자, _, - 20자 이하, drive- 로 시작 안 함)이 아님."
                                     " db 단계가 이 serial 로 디스크를 찾는다")
    for role in SERVER_ROLES + ("loadgen",):
        if role not in roles:
            raise InventoryError(f"역할 {role} 호스트가 없음")
    if roles.get("db", 0) != 1:
        raise InventoryError("DB 호스트는 하나여야 함")
    if roles.get("web", 0) != 1:
        raise InventoryError("웹 호스트는 하나여야 함")
    batch = [h for h in inventory["hosts"] if h.get("batch")]
    if len(batch) != 1 or batch[0]["role"] != "was":
        raise InventoryError("야간 배치 노드(batch: true)는 WAS 하나여야 함")
    if features["router"] != ("router" in roles):
        raise InventoryError("features.router 와 router 역할 호스트 유무가 다름")
    if features["router"] and not inventory.get("router"):
        raise InventoryError("라우터를 켜면 router 절(acl)이 있어야 함")
    if features["router"] and not inventory["monitoring"].get("snmp_community_file"):
        raise InventoryError("라우터를 켜면 SNMP 커뮤니티 비밀값 파일 경로가 있어야 함")


def _hosts(inventory: dict, role: str) -> list[dict]:
    return [host for host in inventory["hosts"] if host["role"] == role]


def _biz_nic(host: dict, inventory: dict) -> dict:
    management = _management_net(inventory)
    return next(nic for nic in host["nics"] if nic.get("gateway") and nic["net"] != management)


def _management_net(inventory: dict) -> str:
    return next(n for n, v in inventory["networks"].items() if v.get("management"))


def _mgmt_nic(host: dict, inventory: dict) -> dict:
    management = _management_net(inventory)
    return next(nic for nic in host["nics"] if nic["net"] == management)


def _netplan(host: dict, inventory: dict) -> str:
    """NIC 마다 MAC 으로 맞추고 인벤토리 이름을 준다.

    Proxmox cloud-init 은 NIC 를 MAC 으로 맞춰 eth0, eth1 ... 로 이름 짓는다. 인벤토리와 설치 코드(nftables,
    lldpd, snmpd, 라우터 도우미)는 인벤토리 이름(ens18 ...)을 쓰므로, 이 파일이 같은 MAC 에 set-name 으로 그
    이름을 붙인다. MAC 은 렌더 때 모르므로 자리(@@MAC:<이름>@@)로 두고 설치 단계 network 가 VM 안에서 채운다.
    """
    lines: list[str] = []
    for nic in host["nics"]:
        network = _net(inventory, nic["net"])
        lines.append(f"    {nic['ifname']}:")
        lines.append("      match:")
        lines.append(f"        macaddress: \"@@MAC:{nic['ifname']}@@\"")
        lines.append(f"      set-name: {nic['ifname']}")
        lines.append("      dhcp4: false")
        lines.append("      accept-ra: false")
        lines.append(f"      addresses: [{nic['addr']}/{_prefix(network['cidr'])}]")
        routes = []
        if nic.get("gateway"):
            routes.append(("default", network["gateway"]))
        for route in nic.get("routes", []):
            routes.append((route["to"], route["via"]))
        if routes:
            lines.append("      routes:")
            for to, via in routes:
                lines.append(f"        - to: {to}")
                lines.append(f"          via: {via}")
        if nic.get("gateway"):
            servers = ", ".join(inventory.get("dns", []))
            lines.append("      nameservers:")
            lines.append(f"        addresses: [{servers}]")
    return "\n".join(lines)


def _acl_rules(inventory: dict, router: dict) -> str:
    app = inventory["app"]
    up = next(nic for nic in router["nics"] if nic.get("gateway"))
    biz = sorted({_net(inventory, nic["net"])["cidr"] for nic in router["nics"]
                  if nic.get("downlink")})
    biz_set = "{ " + ", ".join(biz) + " }"
    lines: list[str] = []
    for rule in inventory["router"]["acl"]:
        src = _net(inventory, rule["src"])["cidr"]
        dst = _net(inventory, rule["dst"])["cidr"]
        port = app[rule["port"]] if isinstance(rule["port"], str) else rule["port"]
        lines.append(f"\t\tip saddr {src} ip daddr {dst} {rule['proto']} dport {port} accept")
    monitor = inventory["monitoring"]["address"]
    lines.append(f"\t\tip saddr {monitor} ip daddr {biz_set} icmp type echo-request accept")
    lines.append(f"\t\tip saddr {biz_set} ip daddr {biz_set} ip protocol icmp accept")
    lines.append(f"\t\tip saddr {biz_set} oifname \"{up['ifname']}\" accept")
    return "\n".join(lines)


def host_context(inventory: dict, host: dict) -> dict[str, str]:
    """설정 틀이 쓰는 값. 여기 없는 자리를 틀이 쓰면 렌더가 멈춘다."""
    app = inventory["app"]
    accounts = inventory["accounts"]
    monitoring = inventory["monitoring"]
    management = _management_net(inventory)
    mgmt_nic = _mgmt_nic(host, inventory)
    mgmt_net = _net(inventory, management)
    context: dict[str, str] = {
        "hostname": host["name"],
        "domain": inventory["domain"],
        "role": host["role"],
        "mgmt_if": mgmt_nic["ifname"],
        "mgmt_addr": mgmt_nic["addr"],
        "mgmt_cidr": mgmt_net["cidr"],
        "mgmt_host": mgmt_net["host_addr"],
        "executor_name": accounts["executor"]["name"],
        "executor_uid": str(accounts["executor"]["uid"]),
        "operator_name": accounts["operator"]["name"],
        "operator_uid": str(accounts["operator"]["uid"]),
        "monitor_address": monitoring["address"],
        "syslog_port": str(monitoring["syslog_port"]),
        "sms_port": str(monitoring["sms_port"]),
        "sms_server_url": f"http://{monitoring['address']}:{monitoring['sms_port']}",
        "sms_agent_home": SMS_AGENT_HOME,
        "chrony_server": inventory["time"]["chrony_server"],
        "web_port": str(app["web_port"]),
        "was_port": str(app["was_port"]),
        "db_port": str(app["db_port"]),
        "app_context": app["context"],
        "db_name": app["db_name"],
        "db_user": app["db_user"],
        "netplan_ethernets": _netplan(host, inventory),
    }
    if host["role"] == "db":
        # 녹화 막기 행 수 문턱(상한 + 설계 하루 쓰기 x 2.5)은 부하 설정에서 읽어 계산한다(설계 §6-4, 검토 세션 Q24 답).
        limit = design_load.rows_limit()
        context["rows_limit"] = str(limit["limit"])
        context["rows_limit_keep_rows"] = str(limit["keep_rows"])
        context["rows_limit_daily_writes"] = str(limit["design_daily_writes"])
        context["rows_limit_days"] = str(limit["days"])
        schedule = design_load.archive_schedule()
        context["archive_timer"] = schedule["timer"]
        context["archive_window_end"] = schedule["window_end"]
        context["archive_zone"] = schedule["zone"]
        context["archive_timer_zone"] = schedule["timer_zone"]
    biz_nics = [nic for nic in host["nics"] if nic["net"] != management]
    lldp_ifaces = ",".join(nic["ifname"] for nic in biz_nics)
    context["lldpd_args"] = ("-x " if host["role"] == "router" else "") + f"-I {lldp_ifaces}"
    db_host = _hosts(inventory, "db")[0]
    db_addr = _biz_nic(db_host, inventory)["addr"]
    context["jdbc_url"] = f"jdbc:postgresql://{db_addr}:{app['db_port']}/{app['db_name']}"
    if host["role"] != "router":
        context["biz_if"] = _biz_nic(host, inventory)["ifname"]
        context["biz_addr"] = _biz_nic(host, inventory)["addr"]
    if host["role"] == "web":
        members = []
        for was in _hosts(inventory, "was"):
            addr = _biz_nic(was, inventory)["addr"]
            members.append(
                f"\t\tBalancerMember \"http://{addr}:{app['was_port']}\" route={was['name']} "
                "connectiontimeout=3 retry=30"
            )
        context["balancer_members"] = "\n".join(members)
    if host["role"] == "was":
        context["jvm_route"] = host["name"]
    if host["role"] == "db":
        context["db_listen"] = _biz_nic(host, inventory)["addr"]
        was_cidrs = sorted({_net(inventory, _biz_nic(w, inventory)["net"])["cidr"] for w in _hosts(inventory, "was")})
        context["pg_hba_app_lines"] = "\n".join(
            f"host    {app['db_name']:<15} {app['db_user']:<15} {cidr:<23} scram-sha-256" for cidr in was_cidrs
        )
    if host["role"] == "loadgen":
        web = _hosts(inventory, "web")[0]
        context["entry_url"] = f"http://{_biz_nic(web, inventory)['addr']}:{app['web_port']}/{app['context']}"
    if host["role"] == "router":
        up = next(nic for nic in host["nics"] if nic.get("gateway"))
        router_conf = inventory["router"]
        context["up_if"] = up["ifname"]
        context["up_addr"] = up["addr"]
        context["acl_rules"] = _acl_rules(inventory, host)
        context["snmp_location"] = router_conf["snmp_location"]
        context["snmp_contact"] = router_conf["snmp_contact"]
        context["snmp_interface_lines"] = "\n".join(
            f"interface {nic['ifname']} 6 {nic['speed_bps']}" for nic in host["nics"] if nic.get("speed_bps")
        )
        context["ifdiscards_augment"] = json.dumps([nic["ifname"] for nic in host["nics"] if nic.get("downlink")])
    return context


def render_text(text: str, context: dict[str, str], source: str) -> str:
    missing = sorted({name for name in PLACEHOLDER.findall(text) if name not in context})
    if missing:
        raise InventoryError(f"{source}: 값이 없는 자리 {', '.join(missing)}")
    return PLACEHOLDER.sub(lambda match: context[match.group(1)], text)


def file_specs(inventory: dict, host: dict) -> list[FileSpec]:
    features = inventory["features"]
    role = host["role"]
    specs = list(COMMON_FILES)
    if features["syslog_forward"] and role != "loadgen":
        specs += SYSLOG_FORWARD_FILES
    if role != "loadgen":
        specs += MODULE_FILES
    if features["lldpd"] and role != "loadgen":
        specs += LLDPD_FILES
    if role != "router":
        specs += NON_ROUTER_FILES
    specs += ROLE_FILES[role]
    if role in SERVER_ROLES:
        specs += SMS_AGENT_FILES
    if host.get("batch"):
        specs += BATCH_FILES
    return specs


def host_steps(inventory: dict, host: dict) -> list[str]:
    features = inventory["features"]
    role = host["role"]
    wanted = {"verify-host", "os-base", "network", "accounts", "time", "syslog", "firewall", "verify"}
    if features["syslog_forward"] and role != "loadgen":
        wanted.add("syslog-forward")
    if role != "loadgen":
        wanted.add("kernel-modules")
    if features["lldpd"] and role != "loadgen":
        wanted.add("lldpd")
    wanted.add(role)
    if role == "db":
        wanted |= {"db-schema", "db-seed", "db-backup"}
    if host.get("batch"):
        wanted.add("was-batch")
    if role in SERVER_ROLES:
        wanted.add("sms-agent")
    return [step for step in STEP_ORDER if step in wanted]


IPV4 = re.compile(r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?:/[0-9]{1,2})?(?![0-9.])")


def normalize(text: str, inventory: dict, host: dict) -> str:
    """주소, 이름, 도메인 값을 자리표시로 바꾼다(설계 §10 T2 "S 와 같이 쓰는 코드" (3)).

    IPv4 주소와 대역은 모두 @ip@ 로, 도메인 값은 @domain@ 으로, 게스트 호스트 이름은 역할과 순번
    (예: WAS 의 둘째 = @was1@)으로 바꾼다. 두 테스트베드에서 같은 자리가 같은 자리표시가 되어야
    체크섬을 비교할 수 있다. 호스트 이름은 긴 것부터 바꿔 짧은 이름이 긴 이름의 일부를 먼저 먹지 않게 한다.
    """
    del host
    result = text.replace(inventory["domain"], "@domain@")
    role_index: dict[str, int] = {}
    names: list[tuple[str, str]] = []
    for other in inventory["hosts"]:
        index = role_index.get(other["role"], 0)
        role_index[other["role"]] = index + 1
        names.append((other["name"], f"@{other['role']}{index}@"))
    for name, token in sorted(names, key=lambda item: -len(item[0])):
        result = re.sub(rf"(?<![A-Za-z0-9_-]){re.escape(name)}(?![A-Za-z0-9_-])", token, result)
    return IPV4.sub("@ip@", result)


def render_host(inventory: dict, host: dict) -> dict:
    """서버 하나의 묶음 내용을 메모리에 만든다(파일로 쓰는 것은 write_bundle)."""
    context = host_context(inventory, host)
    files = []
    for spec in file_specs(inventory, host):
        source = TEMPLATES / spec.template
        text = render_text(source.read_text(encoding="utf-8"), context, spec.template)
        unknown = sorted(set(MAC_SLOT.findall(text)) - {nic["ifname"] for nic in host["nics"]})
        if unknown:
            raise InventoryError(f"{spec.template}: 이 호스트에 없는 NIC 의 MAC 자리 {', '.join(unknown)}")
        files.append({
            "step": spec.step,
            "dest": spec.dest,
            "mode": spec.mode,
            "owner": spec.owner,
            "group": spec.group,
            "template": spec.template,
            "text": text,
            "secrets": sorted(set(SECRET.findall(text))),
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "normalized_sha256": hashlib.sha256(normalize(text, inventory, host).encode()).hexdigest(),
        })
    env = {
        "HOST_NAME": host["name"],
        "HOST_ROLE": host["role"],
        "DOMAIN": inventory["domain"],
        "APP_CONTEXT": inventory["app"]["context"],
        "DB_NAME": inventory["app"]["db_name"],
        "DB_USER": inventory["app"]["db_user"],
        "DB_PORT": str(inventory["app"]["db_port"]),
        "WAS_PORT": str(inventory["app"]["was_port"]),
        "WEB_PORT": str(inventory["app"]["web_port"]),
        "EXECUTOR_NAME": inventory["accounts"]["executor"]["name"],
        "EXECUTOR_UID": str(inventory["accounts"]["executor"]["uid"]),
        "OPERATOR_NAME": inventory["accounts"]["operator"]["name"],
        "MGMT_IF": context["mgmt_if"],
        "MGMT_HOST": context["mgmt_host"],
        "MGMT_ADDR": context["mgmt_addr"],
        "CHRONY_SERVER": context["chrony_server"],
        "NIC_EXPECT": " ".join(f"{nic['ifname']}={nic['addr']}" for nic in host["nics"]),
        "MODULES": "sch_netem sch_tbf sch_prio cls_flower" if host["role"] != "loadgen" else "",
        "BATCH": "1" if host.get("batch") else "0",
        "MONITOR_ADDRESS": inventory["monitoring"]["address"],
        "SYSLOG_FORWARD": "1" if inventory["features"]["syslog_forward"] and host["role"] != "loadgen" else "0",
    }
    data_disk = next((disk for disk in host.get("disks", []) if disk.get("mount")), None)
    if data_disk:
        env["DATA_DISK_SERIAL"] = data_disk["serial"]
        env["DATA_MOUNT"] = data_disk["mount"]
    if host["role"] == "router":
        env["ROUTER_AUGMENT"] = " ".join(nic["ifname"] for nic in host["nics"] if nic.get("downlink"))
    if host["role"] == "loadgen":
        env["ENTRY_URL"] = context["entry_url"]
    if host["role"] in SERVER_ROLES:
        env["SMS_AGENT_HOME"] = SMS_AGENT_HOME
        env["SMS_AGENT_UNIT"] = SMS_AGENT_UNIT
        env["SMS_AGENT_RECORD"] = SMS_AGENT_RECORD
        env["SMS_AGENT_HOST"] = context["biz_addr"]
        env["SMS_SERVER_URL"] = context["sms_server_url"]
    return {"host": host["name"], "role": host["role"], "steps": host_steps(inventory, host), "env": env, "files": files}


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def write_bundle(bundle: dict, out: Path) -> Path:
    root = out / bundle["host"]
    files_dir = root / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    meta_files = []
    for entry in bundle["files"]:
        relative = entry["dest"].lstrip("/")
        target = files_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(entry["text"], encoding="utf-8")
        rows.append("\t".join([entry["step"], entry["dest"], entry["mode"], entry["owner"], entry["group"], "files/" + relative]))
        meta_files.append({k: entry[k] for k in ("step", "dest", "template", "sha256", "normalized_sha256", "secrets")})
    (root / "files.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (root / "steps").write_text("\n".join(bundle["steps"]) + "\n", encoding="utf-8")
    (root / "host.env").write_text(
        "".join(f"{key}={_shell_quote(value)}\n" for key, value in sorted(bundle["env"].items())), encoding="utf-8"
    )
    (root / "meta.json").write_text(
        json.dumps({"host": bundle["host"], "role": bundle["role"], "steps": bundle["steps"], "files": meta_files},
                   ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return root


# ---------------------------------------------------------------- 하이퍼바이저(66) 쪽

def _ipt(table: str | None, rule: str) -> str:
    """같은 규칙을 두 번 넣지 않는 꼴(post-up 이 다시 돌아도 한 줄만 남는다)."""
    flag = f"-t {table} " if table else ""
    return f"iptables {flag}-C {rule} 2>/dev/null || iptables {flag}-A {rule}"


def hypervisor_files(inventory: dict) -> dict[str, str]:
    """66 에 놓을 파일: 망 파일(브리지, 경로, NAT, FORWARD 와 INPUT 전용 체인), chrony 허용, 모듈.

    wg0 인터페이스와 wg0 의 FORWARD 규칙, MSS 클램프는 T0 의 터널 스크립트가 터널과 함께 걸고 지운다.
    """
    hv = inventory["hypervisor"]
    networks = inventory["networks"]
    management = _management_net(inventory)
    mgmt = networks[management]
    uplink = hv["uplink"]
    lines = [
        f"# {inventory['domain']} 테스트베드 망. deploy/render.py 가 인벤토리에서 만든다. 손으로 고치지 않는다.",
        "# 기존 tb-nat, vmbr0, FORWARD 기본 정책은 건드리지 않고, 이 테스트베드 브리지에만 걸리는 전용 체인을 쓴다.",
        "# 체인 내용은 규칙 파일 하나(iptables-restore 꼴)에 있고, 어느 브리지가 먼저 올라오든 없을 때만 한 번에 만든다.",
        "# 브리지마다 자기 훅만 걸고 지우며, 체인은 훅이 하나도 남지 않았을 때만 지운다.",
        "# 적용과 되돌리기는 ifup, ifdown 에 이 파일의 브리지 이름만 준다(ifreload -a 는 기존 tb-nat 의 post-up 을 다시 돌린다).",
        "# 브리지마다 IPv6 를 끈다(disable-ipv6). 66 의 ssh, Proxmox 웹은 IPv6 로도 듣고 ip6tables 는 비어 있어, 켜 두면",
        "# VM 이 링크 로컬 주소로 하이퍼바이저 관리면에 닿는다. ifupdown2 는 브리지를 만든 뒤 올리기 전에 이 값을 건다.",
        "",
    ]
    for name in networks:
        network = networks[name]
        bridge = network["bridge"]
        lines.append(f"auto {bridge}")
        if network.get("host_addr"):
            lines.append(f"iface {bridge} inet static")
            lines.append(f"\taddress {network['host_addr']}/{_prefix(network['cidr'])}")
        else:
            lines.append(f"iface {bridge} inet manual")
        lines += ["\tbridge-ports none", "\tbridge-stp off", "\tbridge-fd 0", "\tdisable-ipv6 yes"]
        lines += [f"\tpost-up {command}" for command in _bridge_up_commands(inventory, network)]
        lines.append(f"\tpost-up {_bridge_ipv6_off_check(bridge)}")
        for other_name, other in networks.items():
            if other.get("via") and ipaddress.ip_address(other["via"]) in ipaddress.ip_network(network["cidr"]):
                lines.append(f"\tpost-up ip route replace {other['cidr']} via {other['via']} dev {bridge}")
        if network.get("nat"):
            lines.append("\tpost-up " + _ipt("nat", f"POSTROUTING -s {network['cidr']} -o {uplink} -j MASQUERADE"))
            lines.append(f"\tpre-down iptables -t nat -D POSTROUTING -s {network['cidr']} -o {uplink} -j MASQUERADE 2>/dev/null || true")
        if name == management:
            lines.append("\tpost-up " + _ipt(
                "nat",
                f"POSTROUTING -s {hv['runner_addr']} -o {bridge} -p tcp --dport 22 -j SNAT --to-source {mgmt['host_addr']}",
            ))
            lines.append(
                f"\tpre-down iptables -t nat -D POSTROUTING -s {hv['runner_addr']} -o {bridge} -p tcp --dport 22 "
                f"-j SNAT --to-source {mgmt['host_addr']} 2>/dev/null || true"
            )
        lines += [f"\tpre-down {command}" for command in _bridge_down_commands(inventory, network)]
        lines.append("")
    files = {hv["file"]: "\n".join(lines), rules_path(inventory): rules_text(inventory)}
    chrony_name = Path(hv["file"]).name
    files[f"/etc/chrony/conf.d/{chrony_name}.conf"] = (
        f"# {inventory['domain']} 테스트베드 VM 이 관리망으로 이 호스트의 시간을 따른다(설계 §2-4).\n"
        f"allow {mgmt['cidr']}\n"
    )
    if inventory["features"]["hypervisor_modules"]:
        files[f"/etc/modules-load.d/{chrony_name}-qdisc.conf"] = (
            "# 하이퍼바이저 쪽 링크 주입이 쓰는 큐 계열 모듈(설계 §2-4).\n"
            "sch_netem\nsch_tbf\nsch_prio\ncls_flower\n"
        )
    return files


def _chain_rules(inventory: dict) -> tuple[list[str], list[str]]:
    hv = inventory["hypervisor"]
    networks = inventory["networks"]
    management = _management_net(inventory)
    mgmt = networks[management]
    chain = hv["chain"]
    input_chain = hv["input_chain"]
    uplink = hv["uplink"]
    tunnel_cidr = hv["tunnel_cidr"]
    domain_nets = [v["cidr"] for n, v in networks.items() if n != management]
    forward: list[str] = [f"-A {chain} -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT"]
    # 관리망: 러너의 ssh 만 들어가고 나머지는 드나들지 못한다.
    forward.append(f"-A {chain} -s {hv['runner_addr']} -d {mgmt['cidr']} -o {mgmt['bridge']} -p tcp --dport 22 -j ACCEPT")
    forward.append(f"-A {chain} -i {mgmt['bridge']} -j DROP")
    forward.append(f"-A {chain} -o {mgmt['bridge']} -j DROP")
    # 기존 테스트베드 내부망과 다른 테스트베드 대역과 격리(Q2).
    for cidr in domain_nets:
        forward.append(f"-A {chain} -s {hv['legacy_net']} -d {cidr} -j DROP")
        forward.append(f"-A {chain} -s {cidr} -d {hv['legacy_net']} -j DROP")
        for other in hv.get("other_domain_nets", []):
            forward.append(f"-A {chain} -s {cidr} -d {other} -j DROP")
            forward.append(f"-A {chain} -s {other} -d {cidr} -j DROP")
        # 관제 서버 실주소로 가는 것은 조용히 통과시키지 않는다(출발지가 바뀌어 대상 구분이 깨짐).
        forward.append(f"-A {chain} -s {cidr} -d {hv['monitor_real_addr']} -j DROP")
    # 터널(wg0) 쪽 허용과 거절은 T0 의 터널 스크립트(TBWG-FWD, wg0 PostUp/PreDown)가 쥔다. 여기서는 터널이
    # 내려가 있을 때 관제 주소로 가는 보고가 기본 경로(사내망 NAT)로 새지 않게만 막는다(T0 리뷰 지적 14).
    for cidr in domain_nets:
        forward.append(f"-A {chain} -s {cidr} -d {tunnel_cidr} -o {uplink} -j DROP")
    # 망 사이 허용 흐름(라우터가 없는 테스트베드의 사용자망 -> 업무망 같은).
    for rule in hv.get("allow_forward", []):
        forward.append(f"-A {chain} -s {rule['src']} -d {rule['dst']} -p {rule['proto']} --dport {rule['port']} -j ACCEPT")
    # 이 테스트베드 대역끼리 66 을 지나는 그 밖의 흐름은 막는다(라우터가 있으면 66 을 지나지 않는다).
    for cidr in domain_nets:
        for other in domain_nets:
            if cidr != other:
                forward.append(f"-A {chain} -s {cidr} -d {other} -j DROP")
    # 패키지 설치: NAT 대역에서 사내망 쪽으로.
    for name, network in networks.items():
        if network.get("nat"):
            forward.append(f"-A {chain} -s {network['cidr']} -o {uplink} -j ACCEPT")
    forward.append(f"-A {chain} -j RETURN")
    # INPUT: 관리 브리지는 chrony 와 이미 맺은 연결 응답만. 주소를 가진 다른 브리지는 그 브리지 자기 주소로 오는 ICMP echo 와
    # 응답만(VM 이 하이퍼바이저 관리면 ssh, 8006 에 닿지 못하게 한다). echo 를 목적지로 좁히는 것은 업무 NIC 에서 66 의 다른
    # 주소(관리 주소 등)로 가는 핑이 66 자신의 INPUT 이 되어 응답하던 것을 막기 위해서다(설계 §10 관리망 격리 (2), APT2 §7).
    inputs = [f"-A {input_chain} -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT"]
    for name, network in networks.items():
        if not network.get("host_addr"):
            continue
        if name == management:
            inputs.append(f"-A {input_chain} -i {network['bridge']} -p udp --dport 123 -j ACCEPT")
        else:
            inputs.append(f"-A {input_chain} -i {network['bridge']} -d {network['host_addr']} -p icmp --icmp-type echo-request -j ACCEPT")
        inputs.append(f"-A {input_chain} -i {network['bridge']} -j DROP")
    inputs.append(f"-A {input_chain} -j RETURN")
    return forward, inputs


def rules_path(inventory: dict) -> str:
    """전용 체인 규칙 파일 경로. interfaces.d 밖(ifupdown 이 읽지 않는 곳)에 둔다."""
    return f"/etc/network/{Path(inventory['hypervisor']['file']).name}.rules"


def rules_text(inventory: dict) -> str:
    """두 전용 체인의 선언과 규칙 전부(iptables-restore --noflush 로 한 번에 넣는다)."""
    hv = inventory["hypervisor"]
    forward, inputs = _chain_rules(inventory)
    lines = [
        f"# {inventory['domain']} 테스트베드 전용 체인. deploy/render.py 가 만든다. 손으로 고치지 않는다.",
        "# 브리지가 올라올 때 체인이 없으면 이 파일로 만든다. 내용을 바꿔 다시 넣을 때는",
        f"# iptables-restore --noflush {rules_path(inventory)} 한 번으로 두 체인을 통째로 바꾼다(빈 체인 구간 없음).",
        "*filter",
        f":{hv['chain']} - [0:0]",
        f":{hv['input_chain']} - [0:0]",
    ]
    lines += forward + inputs
    lines.append("COMMIT")
    return "\n".join(lines) + "\n"


def _bridge_hooks(inventory: dict, network: dict) -> list[str]:
    hv = inventory["hypervisor"]
    hooks = [f"FORWARD -i {network['bridge']} -j {hv['chain']}", f"FORWARD -o {network['bridge']} -j {hv['chain']}"]
    if network.get("host_addr"):
        hooks.append(f"INPUT -i {network['bridge']} -j {hv['input_chain']}")
    return hooks


def _bridge_up_commands(inventory: dict, network: dict) -> list[str]:
    """브리지 하나가 올라올 때: 체인이 없으면 규칙 파일로 만들고, 자기 훅만 맨 앞에 건다.

    격리 없이 올라오지 않게(fail closed) 규칙 파일이 없거나 적용에 실패하거나 훅을 못 걸면 그 브리지를
    내리고 post-up 을 실패로 끝낸다(재리뷰 낮음 12). 이 명령들은 경로, NAT 보다 앞에 둔다.
    """
    hv = inventory["hypervisor"]
    chain, input_chain = hv["chain"], hv["input_chain"]
    bridge = network["bridge"]
    closed = _closed(bridge, "전용 체인을 걸지 못해")
    commands = [
        f"{{ iptables -n -L {chain} >/dev/null 2>&1 && iptables -n -L {input_chain} >/dev/null 2>&1; }} "
        f"|| {{ test -r {rules_path(inventory)} && iptables-restore --noflush {rules_path(inventory)}; }} || {closed}"
    ]
    for hook in _bridge_hooks(inventory, network):
        builtin, rest = hook.split(" ", 1)
        commands.append(f"iptables -C {hook} 2>/dev/null || iptables -I {builtin} 1 {rest} || {closed}")
    return commands


def _closed(bridge: str, reason: str) -> str:
    return f"{{ ip link set dev {bridge} down; echo '{bridge}: {reason} 내림' >&2; false; }}"


def _bridge_ipv6_off_check(bridge: str) -> str:
    """브리지가 올라온 뒤 IPv6 가 꺼져 있고 IPv6 주소가 하나도 없는지 본다. 아니면 그 브리지를 내린다(fail closed).

    끄는 일은 stanza 의 `disable-ipv6 yes` 가 한다(66 의 ifupdown2 는 브리지를 만든 뒤, 관리상 올리기 전에 건다).
    이 줄은 그 속성을 모르는 ifupdown 이거나 값이 다시 켜진 경우에 IPv6 가 열린 채로 남지 않게 하는 확인이다.
    """
    return (
        f"test \"$(cat /proc/sys/net/ipv6/conf/{bridge}/disable_ipv6 2>/dev/null)\" = 1 "
        f"&& test -z \"$(ip -6 -o addr show dev {bridge})\" || {_closed(bridge, 'IPv6 가 꺼지지 않아')}"
    )


def _bridge_down_commands(inventory: dict, network: dict) -> list[str]:
    """브리지 하나가 내려갈 때: 자기 훅만 지우고, 체인은 어떤 훅도 남지 않았을 때만 비우고 지운다."""
    hv = inventory["hypervisor"]
    commands = [f"iptables -D {hook} 2>/dev/null || true" for hook in _bridge_hooks(inventory, network)]
    for chain in (hv["chain"], hv["input_chain"]):
        commands.append(
            f"iptables -S | grep -q -- '-j {chain}$' "
            f"|| {{ iptables -F {chain} 2>/dev/null; iptables -X {chain} 2>/dev/null; true; }}"
        )
    return commands


def hypervisor_commands(inventory: dict, direction: str) -> list[str]:
    """66 에서 이 테스트베드 브리지만 올리고 내리는 명령(ifreload -a 를 쓰지 않는다).

    ipv6-state: 브리지마다 `이름 disable_ipv6 IPv6주소수 MAC` 한 줄을 찍는 명령. 출력을 검사기
    (tools/hv_iptables_check.py --ipv6-state)에 준다. 브리지가 없으면 disable_ipv6 와 MAC 자리가 `-`.
    """
    bridges = [network["bridge"] for network in inventory["networks"].values()]
    if direction == "up":
        return [f"ifup {' '.join(bridges)}"]
    if direction == "down":
        return [f"ifdown {' '.join(reversed(bridges))}"]
    if direction == "ipv6-state":
        return [
            f"for b in {' '.join(bridges)}; do "
            "d=$(cat /proc/sys/net/ipv6/conf/$b/disable_ipv6 2>/dev/null); "
            "m=$(ip -br link show dev $b 2>/dev/null | awk '{print $3}'); "
            "printf '%s %s %s %s\\n' \"$b\" \"${d:--}\" \"$(ip -6 -o addr show dev $b 2>/dev/null | wc -l)\" \"${m:--}\"; done"
        ]
    raise InventoryError(f"모르는 방향 {direction}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--out", type=Path, help="서버별 묶음을 쓸 디렉터리")
    parser.add_argument("--hypervisor", type=Path, help="66 쪽 파일을 경로 그대로 쓸 디렉터리")
    parser.add_argument("--hypervisor-commands", choices=["up", "down", "ipv6-state"],
                        help="66 에서 브리지를 올리고 내리는 명령, 또는 브리지 IPv6 상태를 뜨는 명령만 찍는다")
    parser.add_argument("--host", action="append", default=[], help="이 호스트만(여러 번)")
    parser.add_argument("--check", action="store_true", help="렌더만 해 보고 아무것도 쓰지 않는다")
    args = parser.parse_args(argv)
    inventory = _load(args.inventory)
    try:
        validate(inventory)
        hosts = [h for h in inventory["hosts"] if not args.host or h["name"] in args.host]
        if args.host and len(hosts) != len(set(args.host)):
            raise InventoryError(f"인벤토리에 없는 호스트: {sorted(set(args.host) - {h['name'] for h in hosts})}")
        bundles = [render_host(inventory, host) for host in hosts]
        hv_files = hypervisor_files(inventory)
    except InventoryError as error:
        print(f"render: {error}", file=sys.stderr)
        return 2
    if args.hypervisor_commands:
        print("\n".join(hypervisor_commands(inventory, args.hypervisor_commands)))
        return 0
    if args.out:
        for bundle in bundles:
            root = write_bundle(bundle, args.out)
            print(f"{bundle['host']}: {root} 단계 {len(bundle['steps'])} 파일 {len(bundle['files'])}")
    if args.hypervisor:
        for dest, text in hv_files.items():
            target = args.hypervisor / dest.lstrip("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            print(f"hypervisor: {target}")
    if args.check or not (args.out or args.hypervisor):
        for bundle in bundles:
            print(f"{bundle['host']} ({bundle['role']}): {' '.join(bundle['steps'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
