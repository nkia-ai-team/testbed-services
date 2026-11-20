"""전통 3계층 테스트베드(civil-petition) 구성 코드 시험.

설계 정본: docs/spec-testbed-vm-3tier.md §2-5, §3, §5, §10 T2. S 문서 spec-testbed-sms-only.md §2-3, §2-4.
VM 과 66, 119 에는 닿지 않는다. 렌더는 메모리와 임시 디렉터리에서, 큐 버림 보고는 사용자 네임스페이스의
진짜 커널 qdisc 로 본다(N1 의 netns 시험과 같은 방식).
"""
from __future__ import annotations

import collections
import contextlib
import fnmatch
import hashlib
import datetime
import importlib.util
import io
import json
import math
import os
import random
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
import unittest.mock
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
DOMAIN = REPO / "civil-petition"
DEPLOY = DOMAIN / "deploy"
T_INV = DEPLOY / "inventory" / "t.json"
S_INV = DEPLOY / "inventory" / "s.json"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


render = _load("render", DEPLOY / "render.py")
plan = _load("civil_plan", DEPLOY / "provision" / "plan.py")
ifdiscards = _load("ifdiscards", DEPLOY / "router" / "ifdiscards.py")
design = _load("design_load", DEPLOY / "tools" / "design_load.py")
stability = _load("stability_check", DEPLOY / "tools" / "stability_check.py")
scale = _load("scale_verdict", DEPLOY / "tools" / "scale_verdict.py")
monitor = _load("loadgen_monitor_cp", REPO / "scripts" / "scenarios" / "profiles" / "loadgen_monitor.py")
hvcheck = _load("hv_iptables_check", DEPLOY / "tools" / "hv_iptables_check.py")
t3reg = _load("t3_register", DEPLOY / "tools" / "t3_register.py")
t3check = _load("t3_check", DEPLOY / "tools" / "t3_check.py")
pairpkg = _load("pair_packages", DEPLOY / "tools" / "pair_packages.py")
S_SPEC = REPO / "docs" / "spec-testbed-sms-only.md"


def inventory(path: Path = T_INV) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def bundles(path: Path) -> dict[str, dict]:
    inv = inventory(path)
    render.validate(inv)
    return {host["name"]: render.render_host(inv, host) for host in inv["hosts"]}


def file_of(bundle: dict, dest: str) -> str:
    for entry in bundle["files"]:
        if entry["dest"] == dest:
            return entry["text"]
    raise AssertionError(f"{bundle['host']} 에 {dest} 없음")


# rsyslog 가 읽는 설정이 놓이는 곳.
RSYSLOG_DESTS = ("/etc/rsyslog.conf", "/etc/rsyslog.d/", "/etc/rsyslog.remote.d/")  # 마지막은 옛 자리(지금 0 이어야 함)
# RainerScript 큰따옴표 문자열의 이스케이프(rsyslog 가 풀어 쓰는 것만).
RAINER_ESCAPES = {"$": "$", "\\": "\\", '"': '"', "'": "'", "n": "\n", "t": "\t"}


def rainer_bare_dollars(text: str) -> list[str]:
    """큰따옴표 문자열 안에 이스케이프 없이 든 $ 를 '"문자열" (N행)' 으로 모두 낸다. # 로 시작하는 줄은 주석이다."""
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        index = 0
        while index < len(line):
            if line[index] != '"':
                index += 1
                continue
            end, bare = index + 1, False
            while end < len(line) and line[end] != '"':
                if line[end] == "\\":
                    end += 1
                elif line[end] == "$":
                    bare = True
                end += 1
            if bare:
                found.append(f"{line[index:end + 1]} ({number}행)")
            index = end + 1
    return found


def rainer_unescape(value: str) -> str:
    """설정 파일의 큰따옴표 문자열 내용을 rsyslog 가 쓰는 값으로 푼다. 모르는 이스케이프는 ValueError."""
    out, index = [], 0
    while index < len(value):
        char = value[index]
        if char == "\\":
            following = value[index + 1:index + 2]
            if following not in RAINER_ESCAPES:
                raise ValueError(f"모르는 이스케이프 \\{following}")
            out.append(RAINER_ESCAPES[following])
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


class InventoryValidationTests(unittest.TestCase):
    def test_both_inventories_validate_and_render_every_host(self) -> None:
        for path in (T_INV, S_INV):
            with self.subTest(inventory=path.name):
                rendered = bundles(path)
                self.assertEqual(set(rendered), {h["name"] for h in inventory(path)["hosts"]})

    def test_rejects_a_guest_name_carrying_a_testbed_word(self) -> None:
        for bad in ("t4-web", "tb-web", "testweb", "s5-db"):
            inv = inventory()
            inv["hosts"][1]["name"] = bad
            with self.subTest(name=bad), self.assertRaises(render.InventoryError):
                render.validate(inv)

    def test_rejects_executor_sharing_operator_identity(self) -> None:
        inv = inventory()
        inv["accounts"]["executor"]["uid"] = inv["accounts"]["operator"]["uid"]
        with self.assertRaises(render.InventoryError):
            render.validate(inv)

    def test_rejects_a_management_nic_with_a_default_route(self) -> None:
        inv = inventory()
        for nic in inv["hosts"][1]["nics"]:
            nic["gateway"] = nic["net"] == "mgmt"
        with self.assertRaises(render.InventoryError):
            render.validate(inv)

    def test_rejects_router_feature_without_router_host(self) -> None:
        inv = inventory()
        inv["hosts"] = [h for h in inv["hosts"] if h["role"] != "router"]
        with self.assertRaises(render.InventoryError):
            render.validate(inv)

    def test_rejects_a_data_disk_serial_the_db_step_cannot_match(self) -> None:
        # db 단계는 lsblk SERIAL 과 글자 그대로 맞춘다. serial 이 없거나, serial 없는 디스크가 보고하는 drive-scsiN 꼴이거나,
        # lsblk -r 이 바꿔 쓰는 글자가 있거나, Proxmox 한도(20바이트)를 넘으면 렌더에서 멈춘다.
        for bad in (None, "", "drive-scsi1", "pg data", "pg/data", "p" * 21):
            inv = inventory()
            disk = next(d for h in inv["hosts"] if h["role"] == "db" for d in h["disks"] if d.get("mount"))
            if bad is None:
                disk.pop("serial")
            else:
                disk["serial"] = bad
            with self.subTest(serial=bad), self.assertRaisesRegex(render.InventoryError, "데이터 디스크 serial"):
                render.validate(inv)
        for good in ("pgdata", "PG_data-2", "p" * 20):
            inv = inventory()
            next(d for h in inv["hosts"] if h["role"] == "db" for d in h["disks"] if d.get("mount"))["serial"] = good
            with self.subTest(serial=good):
                render.validate(inv)

    def test_rejects_an_app_context_other_than_the_war_name(self) -> None:
        inv = inventory()
        inv["app"]["context"] = "portal"
        with self.assertRaises(render.InventoryError):
            render.validate(inv)

    def test_kernel_modules_load_before_forwarding_starts(self) -> None:
        steps = bundles(T_INV)["web01"]["steps"]
        self.assertLess(steps.index("kernel-modules"), steps.index("syslog-forward"))

    def test_rejects_an_address_outside_its_network(self) -> None:
        inv = inventory()
        inv["hosts"][1]["nics"][0]["addr"] = "192.168.125.50"
        with self.assertRaises(render.InventoryError):
            render.validate(inv)

    def test_template_placeholder_without_value_stops_the_render(self) -> None:
        with self.assertRaises(render.InventoryError):
            render.render_text("x {{nope}} y", {"a": "b"}, "case")


def site_tokens(inv: dict) -> dict[str, str]:
    """S 문서 §2-3 "바꾸는 것" 가운데 주소 자리만 자리표시로(render.normalize 보다 좁게, 시험 전용).

    render.normalize 는 모든 IPv4 를 바꾸므로 설계가 바꾸라고 하지 않은 주소(DNS 등)가 달라도 같아 보인다. 여기서는
    인벤토리의 자리 값만 바꾼다: 호스트 NIC 주소(역할 순번과 업무, 관리 갈래), 망 대역(관리망과 그 밖), 망 게이트웨이,
    관리망의 66 주소와 운영자 단말 출발 주소. 그 밖의 IPv4 는 그대로 남아 T 와 S 가 같아야 한다.
    """
    management = render._management_net(inv)
    tokens: dict[str, str] = {}
    for name, net in inv["networks"].items():
        kind = "mgmt" if name == management else "biz"
        tokens[net["cidr"]] = f"@{kind}-net@"
        if name == management:
            tokens[net["host_addr"]] = "@mgmt-hv@"
            if net.get("operator_host_addr"):
                tokens[net["operator_host_addr"]] = "@mgmt-operator@"
    counter: dict[str, int] = {}
    for host in inv["hosts"]:
        index = counter.get(host["role"], 0)
        counter[host["role"]] = index + 1
        for nic in host["nics"]:
            kind = "mgmt" if nic["net"] == management else ("biz" if nic.get("gateway") else nic["net"])
            tokens[nic["addr"]] = f"@{host['role']}{index}.{kind}@"
    for name, net in inv["networks"].items():
        if net.get("gateway"):
            tokens[net["gateway"]] = "@gw@"
    return tokens


def site_normalize(text: str, inv: dict) -> str:
    tokens = site_tokens(inv)

    def swap(match: re.Match) -> str:
        value = match.group(0)
        if value in tokens:
            return tokens[value]
        address, slash, prefix = value.partition("/")
        return tokens[address] + slash + prefix if address in tokens else value
    text = text.replace(inv["domain"], "@domain@")
    counter: dict[str, int] = {}
    names = []
    for host in inv["hosts"]:
        index = counter.get(host["role"], 0)
        counter[host["role"]] = index + 1
        names.append((host["name"], f"@{host['role']}{index}@"))
    for name, token in sorted(names, key=lambda item: -len(item[0])):
        text = re.sub(rf"(?<![A-Za-z0-9_-]){re.escape(name)}(?![A-Za-z0-9_-])", token, text)
    return render.IPV4.sub(swap, text)


class SharedCodeTests(unittest.TestCase):
    """설계 §10 T2 "S 와 같이 쓰는 코드" (1)~(4)."""

    def test_two_inventories_one_installer(self) -> None:
        self.assertTrue(T_INV.is_file() and S_INV.is_file())
        self.assertEqual(inventory(T_INV)["testbed"], "t")
        self.assertEqual(inventory(S_INV)["testbed"], "s")
        source = (DEPLOY / "apply.sh").read_text(encoding="utf-8")
        self.assertIn("--inventory", source)

    def test_installer_and_templates_name_no_site_value(self) -> None:
        site_words = set()
        for path in (T_INV, S_INV):
            inv = inventory(path)
            site_words.add(inv["domain"])
            for host in inv["hosts"]:
                # loadgen 은 두 테스트베드의 호스트 이름이자 유닛 이름(loadgen.service, 설계 §5)이라 뺀다.
                if host["name"] != "loadgen":
                    site_words.add(host["name"])
        address = re.compile(r"192\.168\.(12[3-7]|13[1-3])\.|10\.250\.0\.")
        checked = 0
        for path in sorted(DEPLOY.rglob("*")):
            if not path.is_file() or "inventory" in path.parts or path.suffix in {".pyc"}:
                continue
            text = path.read_text(encoding="utf-8")
            if path.suffix == ".py":
                text = re.sub(r'"""[\s\S]*?"""', "", text)
            body = "\n".join(line for line in text.splitlines()
                             if not line.lstrip().startswith(("#", "//", "<!--", "--")))
            checked += 1
            with self.subTest(path=str(path.relative_to(DOMAIN))):
                self.assertIsNone(address.search(body), "이 도메인 주소 대역")
                for word in site_words:
                    self.assertNotRegex(body, rf"(?<![A-Za-z0-9_-]){re.escape(word)}(?![A-Za-z0-9_])", f"사이트 값 {word}")
        self.assertGreater(checked, 30)

    def test_s_render_drops_router_lldpd_and_forwarding_and_keeps_the_rest_identical(self) -> None:
        t_inv, s_inv = inventory(T_INV), inventory(S_INV)
        t = bundles(T_INV)
        s = bundles(S_INV)
        self.assertNotIn("router", {b["role"] for b in s.values()})
        optional_dests = {"/etc/rsyslog.d/remote/10-forward.conf", "/etc/default/lldpd"}
        for name, bundle in s.items():
            self.assertNotIn("lldpd", bundle["steps"])
            self.assertNotIn("syslog-forward", bundle["steps"])
            self.assertTrue(optional_dests.isdisjoint({f["dest"] for f in bundle["files"]}))
        by_role_t = self._by_role(t_inv, t)
        by_role_s = self._by_role(s_inv, s)
        self.assertEqual(set(by_role_s), set(by_role_t) - {"router0"})
        compared = 0
        for key, s_bundle in by_role_s.items():
            t_bundle = by_role_t[key]
            t_files = {f["dest"]: f for f in t_bundle["files"] if f["dest"] not in optional_dests}
            s_files = {f["dest"]: f for f in s_bundle["files"]}
            self.assertEqual(set(t_files), set(s_files), key)
            for dest in s_files:
                if key == "loadgen0" and dest == "/etc/netplan/01-host.yaml":
                    # T 부하기는 웹망으로 라우터 상향을 거치는 경로 한 줄이 더 있다(망 모양의 차이, 설계 §3-2).
                    self.assertIn("via", t_files[dest]["text"])
                    continue
                compared += 1
                with self.subTest(role=key, dest=dest):
                    self.assertEqual(t_files[dest]["normalized_sha256"], s_files[dest]["normalized_sha256"])
                    # 좁은 정규화(주소 자리만)로도 같아야 한다. 모든 IPv4 를 지우는 normalize 는 DNS 같은 값의 차이를 못 본다.
                    self.assertEqual(site_normalize(t_files[dest]["text"], t_inv), site_normalize(s_files[dest]["text"], s_inv))
            t_steps = [x for x in t_bundle["steps"] if x not in {"lldpd", "syslog-forward"}]
            self.assertEqual(t_steps, s_bundle["steps"])
        self.assertGreater(compared, 50)

    def test_s_host_env_differs_from_t_only_in_site_values_and_syslog_forwarding(self) -> None:
        # S 문서 §2-3 "바꾸는 것"(주소, 이름, 도메인 값)과 "빼는 것"(119 전달) 말고 설치 단계에 가는 값이 다르지 않다.
        # 주소, 이름, 도메인 값을 자리표시로 바꾼 뒤 같아야 하고, 다른 값은 syslog 전달 켜기 하나다(서버만, 부하기는 둘 다 끔).
        t_inv, s_inv = inventory(T_INV), inventory(S_INV)
        by_role_t = self._by_role(t_inv, bundles(T_INV))
        by_role_s = self._by_role(s_inv, bundles(S_INV))
        compared = 0
        for key, s_bundle in by_role_s.items():
            t_env, s_env = by_role_t[key]["env"], s_bundle["env"]
            with self.subTest(role=key):
                self.assertEqual(set(t_env), set(s_env))
                differ = {name for name in s_env
                          if site_normalize(t_env[name], t_inv) != site_normalize(s_env[name], s_inv)}
                if key.startswith("loadgen"):
                    self.assertEqual(differ, set())
                    self.assertEqual(s_env["SYSLOG_FORWARD"], "0")
                else:
                    self.assertEqual(differ, {"SYSLOG_FORWARD"})
                    self.assertEqual((t_env["SYSLOG_FORWARD"], s_env["SYSLOG_FORWARD"]), ("1", "0"))
                # 자리표시 뒤에 같다는 것이 값이 실제로 S 값이라는 뜻이 되게 한 번 더 본다.
                self.assertEqual(s_env["DOMAIN"], s_inv["domain"])
                self.assertTrue(s_env["MGMT_ADDR"].startswith("192.168.133."))
                compared += len(s_env)
        self.assertGreater(compared, 100)

    def test_s_vm_plan_differs_from_t_only_in_ids_bridges_and_site_values(self) -> None:
        # plan.py create 도 같은 좁은 정규화로 짝끼리 견준다(VMID, 브리지, 이름, 주소 자리 말고 같아야 한다. DNS 포함).
        t_inv, s_inv = inventory(T_INV), inventory(S_INV)

        def per_host(inv: dict) -> dict[str, list[str]]:
            management = render._management_net(inv)
            bridges = {net["bridge"]: ("@mgmt-br@" if name == management else "@biz-br@")
                       for name, net in inv["networks"].items()}
            out, counter, current = {}, {}, None
            keys = {}
            for host in inv["hosts"]:
                index = counter.get(host["role"], 0)
                counter[host["role"]] = index + 1
                keys[host["name"]] = (f"{host['role']}{index}", host["vmid"])
            for line in plan.create(inv, "/root/k.pub")[1:]:
                head = re.match(r"# (\S+) ", line)
                if head:
                    current = keys[head.group(1)]
                    out[current[0]] = []
                    continue
                line = re.sub(rf"\b{current[1]}\b", "@vmid@", line)
                line = re.sub(r"bridge=(vmbr\d+)", lambda m: "bridge=" + bridges[m.group(1)], line)
                out[current[0]].append(site_normalize(line, inv))
            return out
        t_plan, s_plan = per_host(t_inv), per_host(s_inv)
        self.assertEqual(set(s_plan), set(t_plan) - {"router0"})
        for key, lines in s_plan.items():
            with self.subTest(role=key):
                self.assertEqual(lines, t_plan[key])
                self.assertTrue(any("--nameserver" in line for line in lines))

    def test_s_servers_use_the_package_index_time_of_their_t_twin(self) -> None:
        # 쌍 조건 2: S 서버의 apt_snapshot 은 짝 T 서버(같은 역할 순번)의 값과 같다(runbook §14-3-1).
        t_inv, s_inv = inventory(T_INV), inventory(S_INV)
        t_keys = {k: b["host"] for k, b in self._by_role(t_inv, bundles(T_INV)).items()}
        s_keys = {k: b["host"] for k, b in self._by_role(s_inv, bundles(S_INV)).items()}
        t_hosts = {h["name"]: h for h in t_inv["hosts"]}
        s_hosts = {h["name"]: h for h in s_inv["hosts"]}
        for key, s_name in s_keys.items():
            with self.subTest(role=key):
                self.assertEqual(s_hosts[s_name]["apt_snapshot"], t_hosts[t_keys[key]]["apt_snapshot"])
        bad = json.loads(json.dumps(s_inv))
        for value in (None, "", "2026-11-18T02:20:12Z", "20261118T022012"):
            with self.subTest(value=value):
                if value is None:
                    bad["hosts"][0].pop("apt_snapshot", None)
                else:
                    bad["hosts"][0]["apt_snapshot"] = value
                with self.assertRaises(render.InventoryError):
                    render.validate(bad)

    @staticmethod
    def _by_role(inv: dict, rendered: dict[str, dict]) -> dict[str, dict]:
        out = {}
        counter: dict[str, int] = {}
        for host in inv["hosts"]:
            index = counter.get(host["role"], 0)
            counter[host["role"]] = index + 1
            out[f"{host['role']}{index}"] = rendered[host["name"]]
        return out

    def test_normalization_really_hides_site_values(self) -> None:
        t = bundles(T_INV)
        text = file_of(t["web01"], "/etc/apache2/sites-available/petition.conf")
        normalized = render.normalize(text, inventory(T_INV), {})
        self.assertIn("192.168.125.11", text)
        self.assertNotIn("192.168.", normalized)
        self.assertIn("route=@was0@", normalized)
        self.assertIn("route=@was1@", normalized)

    def test_no_inventory_creates_a_dpm_account_or_a_monitor_line_in_pg_hba(self) -> None:
        for path in (T_INV, S_INV):
            inv = inventory(path)
            db = next(b for b in bundles(path).values() if b["role"] == "db")
            hba = file_of(db, "/etc/postgresql/16/main/pg_hba.conf")
            active = [line for line in hba.splitlines() if line and not line.startswith("#")]
            with self.subTest(inventory=path.name):
                self.assertFalse(any(inv["monitoring"]["address"] in line for line in active))
                self.assertFalse(any("dpm" in line.lower() for line in active))
                self.assertEqual(sum(1 for line in active if inv["app"]["db_user"] in line), 1)
        steps_text = "\n".join(p.read_text() for p in (DEPLOY / "steps").glob("*.sh"))
        self.assertNotRegex(steps_text.lower(), r"create (role|user)[^\n]*(dpm|monitor)")
        self.assertNotIn("dpm", " ".join(render.STEP_ORDER))


def spec_section(path: Path, heading: str) -> str:
    """설계 문서에서 제목 줄 하나부터 다음 같은 깊이 이상의 제목 앞까지."""
    return spec_section_text(path.read_text(encoding="utf-8"), heading)


def spec_section_text(text: str, heading: str) -> str:
    start = text.index(heading + "\n")
    level = len(heading) - len(heading.lstrip("#"))
    following = re.search(rf"^#{{1,{level}}} ", text[start + len(heading):], re.M)
    return text[start:start + len(heading) + (following.start() if following else len(text))]


def table_rows(section: str) -> list[list[str]]:
    """마크다운 표의 데이터 줄(머리와 구분 줄을 뺀다)을 칸 목록으로."""
    rows = [[cell.strip() for cell in line.strip().strip("|").split("|")]
            for line in section.splitlines() if line.startswith("|")]
    return [row for row in rows[2:]]


class SmsOnlyDesignTests(unittest.TestCase):
    """S 렌더와 VM 계획을 S 문서 §2-2(VM 표), §3-2(주소표), §3-3(경로), §3-5(66 에 더하는 것)와 기계로 대조한다."""

    def setUp(self) -> None:
        self.inv = inventory(S_INV)
        self.t_inv = inventory(T_INV)
        self.hosts = {host["name"]: host for host in self.inv["hosts"]}

    def _vm_rows(self) -> list[dict]:
        rows = []
        for row in table_rows(spec_section(S_SPEC, "### 2-2. VM 목록과 크기")):
            if not row[0].isdigit():
                continue
            vmid, pve, name, vcpu, memory, disks, biz, mgmt, pair = row
            rows.append({"vmid": int(vmid), "pve_name": pve, "name": name, "vcpu": int(vcpu),
                         "memory_mb": int(re.fullmatch(r"(\d+)GB", memory).group(1)) * 1024,
                         "disks": [int(n) for n in re.findall(r"(\d+)GB", disks)],
                         "biz": re.match(r"[0-9.]+", biz).group(0),
                         "mgmt": "192.168" + mgmt, "pair": re.match(r"[a-z0-9-]+", pair).group(0),
                         "batch": "배치" in pair})
        return rows

    def test_inventory_and_vm_plan_match_the_vm_table(self) -> None:
        rows = self._vm_rows()
        self.assertEqual([r["name"] for r in rows], [h["name"] for h in self.inv["hosts"]])
        create = "\n".join(plan.create(self.inv, "/root/s5-operator.pub"))
        t_hosts = {h["name"]: h for h in self.t_inv["hosts"]}
        for row in rows:
            host = self.hosts[row["name"]]
            biz = next(nic for nic in host["nics"] if nic.get("gateway"))
            mgmt = render._mgmt_nic(host, self.inv)
            with self.subTest(host=row["name"]):
                self.assertEqual((host["vmid"], host["pve_name"], host["vcpu"], host["memory_mb"]),
                                 (row["vmid"], row["pve_name"], row["vcpu"], row["memory_mb"]))
                self.assertEqual([d["size_gb"] for d in host["disks"]], row["disks"])
                self.assertEqual((biz["addr"], mgmt["addr"]), (row["biz"], row["mgmt"]))
                self.assertEqual(bool(host.get("batch")), row["batch"])
                # 쌍 조건 2: T 의 짝과 같은 크기.
                twin = t_hosts[row["pair"]]
                self.assertEqual(twin["role"], host["role"])
                self.assertEqual((twin["vcpu"], twin["memory_mb"], [d["size_gb"] for d in twin["disks"]]),
                                 (host["vcpu"], host["memory_mb"], [d["size_gb"] for d in host["disks"]]))
                self.assertEqual(bool(twin.get("batch")), bool(host.get("batch")))
                # 계획이 표의 크기와 이름으로 만든다(처음엔 게스트 이름, rename 이 Proxmox 이름).
                vmid = row["vmid"]
                self.assertIn(f"qm clone 9000 {vmid} --name {row['name']} --full 1 --storage vmdata", create)
                self.assertIn(f"qm set {vmid} --cores {row['vcpu']} --memory {row['memory_mb']} --onboot 1", create)
                self.assertIn(f"qm resize {vmid} scsi0 {row['disks'][0]}G", create)
                for index, size in enumerate(row["disks"][1:], start=1):
                    self.assertIn(f"qm set {vmid} --scsi{index} vmdata:{size},", create)
        self.assertIn("qm set 214 --scsi1 vmdata:100,discard=on,iothread=1,ssd=1,serial=pgdata", create)
        self.assertEqual(sum(r["vcpu"] for r in rows), 12)
        self.assertEqual(sum(r["memory_mb"] for r in rows), 20 * 1024)
        self.assertEqual(sum(sum(r["disks"]) for r in rows), 240)
        rename = "\n".join(plan.rename(self.inv))
        for row in rows:
            self.assertIn(f"qm set {row['vmid']} --name {row['pve_name']}", rename)
        # T 의 VMID, 브리지와 겹치지 않는다.
        self.assertFalse({h["vmid"] for h in self.inv["hosts"]} & {h["vmid"] for h in self.t_inv["hosts"]})
        self.assertFalse({n["bridge"] for n in self.inv["networks"].values()}
                         & {n["bridge"] for n in self.t_inv["networks"].values()})

    def test_networks_match_the_address_table_and_paths(self) -> None:
        names = {"S 업무망": "biz", "S 사용자망": "user", "S 관리망": "mgmt"}
        seen = set()
        for row in table_rows(spec_section(S_SPEC, "### 3-2. 주소표")):
            if row[0] not in names:
                continue
            network = self.inv["networks"][names[row[0]]]
            seen.add(row[0])
            prefix = network["cidr"].rsplit(".", 1)[0]
            with self.subTest(network=row[0]):
                self.assertEqual(network["cidr"], row[1])
                self.assertEqual(network["host_addr"], prefix + "." + re.match(r"\.(\d+)", row[2]).group(1))
                for name, last in re.findall(r"([a-z][a-z0-9]+) \.(\d+)", row[3]):
                    nic = next(n for n in self.hosts[name]["nics"] if n["net"] == names[row[0]])
                    self.assertEqual(nic["addr"], f"{prefix}.{last}")
        self.assertEqual(seen, set(names))
        rendered = bundles(S_INV)
        for name, host in self.hosts.items():
            netplan = file_of(rendered[name], "/etc/netplan/01-host.yaml")
            gateway = self.inv["networks"]["user" if host["role"] == "loadgen" else "biz"]["gateway"]
            with self.subTest(host=name):
                # 기본 경로는 66(라우터 없음, 결정 3), 관리 NIC 에는 경로가 없다(§3-6 규칙 4).
                self.assertEqual(re.findall(r"- to: (\S+)\n\s+via: (\S+)", netplan), [("default", gateway)])
                self.assertEqual(rendered[name]["env"]["CHRONY_SERVER"], "192.168.133.1")
                self.assertEqual(rendered[name]["env"]["MGMT_HOST"], "192.168.133.1")
                if host["role"] in render.SERVER_ROLES:
                    self.assertEqual(rendered[name]["env"]["SMS_AGENT_HOST"], render._biz_nic(host, self.inv)["addr"])
                    self.assertEqual(rendered[name]["env"]["SMS_SERVER_URL"], "http://10.250.0.1:18090")
        # 사용자 요청: loadgen -> 66 -> websvr01. 계층 사이 호출은 같은 업무망 안.
        self.assertEqual(rendered["loadgen"]["env"]["ENTRY_URL"], "http://192.168.131.11:80/petition")
        web = file_of(rendered["websvr01"], "/etc/apache2/sites-available/petition.conf")
        self.assertIn('BalancerMember "http://192.168.131.21:8080"', web)
        self.assertIn('BalancerMember "http://192.168.131.22:8080"', web)
        hba = file_of(rendered["dbsvr01"], "/etc/postgresql/16/main/pg_hba.conf")
        self.assertRegex(hba, r"(?m)^host\s+petition\s+petition_app\s+192\.168\.131\.0/24\s+scram-sha-256$")

    def test_hypervisor_files_follow_the_design(self) -> None:
        files = render.hypervisor_files(self.inv)
        self.assertEqual(set(files), {"/etc/network/interfaces.d/s5-net", "/etc/network/s5-net.rules",
                                      "/etc/chrony/conf.d/s5-net.conf"})
        net = files["/etc/network/interfaces.d/s5-net"]
        # 브리지 셋, 셋 다 66 주소 .1. 119 의 길은 66 에서 바로 업무망이라 경로 줄(via)이 없다.
        self.assertEqual(re.findall(r"^auto (\S+)$", net, re.M), ["vmbr12", "vmbr13", "vmbr14"])
        self.assertEqual(re.findall(r"^\taddress (\S+)$", net, re.M),
                         ["192.168.131.1/24", "192.168.132.1/24", "192.168.133.1/24", "192.168.133.5/24"])
        self.assertNotIn("ip route", net)
        self.assertNotIn("wg0", net)
        self.assertEqual(sorted(set(re.findall(r"iptables -t nat -A (POSTROUTING .*?)$", net, re.M))), [
            "POSTROUTING -s 192.168.122.20 -o vmbr14 -p tcp --dport 22 -j SNAT --to-source 192.168.133.1",
            "POSTROUTING -s 192.168.122.21 -o vmbr14 -p tcp --dport 22 -j SNAT --to-source 192.168.133.5",
            "POSTROUTING -s 192.168.131.0/24 -o vmbr0.200 -j MASQUERADE",
            "POSTROUTING -s 192.168.132.0/24 -o vmbr0.200 -j MASQUERADE",
        ])
        self.assertEqual(files["/etc/chrony/conf.d/s5-net.conf"].splitlines()[-1], "allow 192.168.133.0/24")
        forward, inputs = render._chain_rules(self.inv)
        # 허용은 이것뿐이다(§3-5): 이미 맺은 연결, 러너 ssh, 운영자 단말 ssh(T 설계 §6-2 운영자 경로), 사용자망 -> 웹 80,
        # 업무망과 사용자망 -> 패키지 설치.
        self.assertEqual([r for r in forward if r.endswith("-j ACCEPT")], [
            "-A S5-FWD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
            "-A S5-FWD -s 192.168.122.20 -d 192.168.133.0/24 -o vmbr14 -p tcp --dport 22 -j ACCEPT",
            "-A S5-FWD -s 192.168.122.21 -d 192.168.133.0/24 -o vmbr14 -p tcp --dport 22 -j ACCEPT",
            "-A S5-FWD -s 192.168.132.10 -d 192.168.131.11 -p tcp --dport 80 -j ACCEPT",
            "-A S5-FWD -s 192.168.131.0/24 -o vmbr0.200 -j ACCEPT",
            "-A S5-FWD -s 192.168.132.0/24 -o vmbr0.200 -j ACCEPT",
        ])
        self.assertEqual([r for r in inputs if r.endswith("-j ACCEPT")], [
            "-A S5-IN -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
            "-A S5-IN -i vmbr12 -d 192.168.131.1 -p icmp --icmp-type echo-request -j ACCEPT",
            "-A S5-IN -i vmbr13 -d 192.168.132.1 -p icmp --icmp-type echo-request -j ACCEPT",
            "-A S5-IN -i vmbr14 -p udp --dport 123 -j ACCEPT",
        ])
        others = ["192.168.122.0/24"] + [f"192.168.{n}.0/24" for n in range(123, 128)]
        for cidr in ("192.168.131.0/24", "192.168.132.0/24"):
            with self.subTest(cidr=cidr):
                for other in others:
                    self.assertIn(f"-A S5-FWD -s {cidr} -d {other} -j DROP", forward)
                    self.assertIn(f"-A S5-FWD -s {other} -d {cidr} -j DROP", forward)
                real = forward.index(f"-A S5-FWD -s {cidr} -d 192.168.230.119 -j DROP")
                leak = forward.index(f"-A S5-FWD -s {cidr} -d 10.250.0.0/30 -o vmbr0.200 -j DROP")
                self.assertLess(max(real, leak), forward.index(f"-A S5-FWD -s {cidr} -o vmbr0.200 -j ACCEPT"))
        allow = forward.index("-A S5-FWD -s 192.168.132.10 -d 192.168.131.11 -p tcp --dport 80 -j ACCEPT")
        self.assertLess(allow, forward.index("-A S5-FWD -s 192.168.132.0/24 -d 192.168.131.0/24 -j DROP"))
        self.assertIn("-A S5-FWD -s 192.168.131.0/24 -d 192.168.132.0/24 -j DROP", forward)
        runner = forward.index("-A S5-FWD -s 192.168.122.20 -d 192.168.133.0/24 -o vmbr14 -p tcp --dport 22 -j ACCEPT")
        self.assertLess(runner, forward.index("-A S5-FWD -i vmbr14 -j DROP"))
        self.assertLess(runner, forward.index("-A S5-FWD -o vmbr14 -j DROP"))
        for bridge in ("vmbr12", "vmbr13", "vmbr14"):
            self.assertIn(f"-A S5-IN -i {bridge} -j DROP", inputs)


class RenderContentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.t = bundles(T_INV)

    def test_steps_follow_role_and_features(self) -> None:
        self.assertEqual(self.t["rt-core01"]["steps"][-2:], ["router", "verify"])
        self.assertIn("syslog-forward", self.t["web01"]["steps"])
        self.assertNotIn("syslog-forward", self.t["loadgen"]["steps"])
        self.assertNotIn("kernel-modules", self.t["loadgen"]["steps"])
        self.assertIn("was-batch", self.t["was02"]["steps"])
        self.assertNotIn("was-batch", self.t["was01"]["steps"])
        self.assertEqual([s for s in self.t["db01"]["steps"] if s.startswith("db")], ["db", "db-schema", "db-seed", "db-backup"])
        for bundle in self.t.values():
            self.assertEqual(bundle["steps"][0], "verify-host")
            self.assertEqual(bundle["steps"][-1], "verify")
            for step in bundle["steps"]:
                self.assertTrue((DEPLOY / "steps" / f"{step}.sh").is_file(), step)

    def test_every_file_spec_step_is_a_step_the_host_runs(self) -> None:
        for bundle in self.t.values():
            for entry in bundle["files"]:
                self.assertIn(entry["step"], bundle["steps"], (bundle["host"], entry["dest"]))

    def test_rsyslog_first_rule_names_the_executor_and_stops(self) -> None:
        inv = inventory()
        rule = file_of(self.t["web01"], "/etc/rsyslog.d/00-opsjobs.conf")
        executor = inv["accounts"]["executor"]
        self.assertIn(f'$!uid == "{executor["uid"]}"', rule)
        self.assertIn(executor["name"], rule)
        self.assertIn('run-r[0-9a-f]{32}', rule)
        self.assertIn('file="/var/log/opsjobs.log"', rule)
        self.assertRegex(rule, r"action\([^)]*\)\s*\n\s*stop")
        self.assertNotIn(inv["accounts"]["operator"]["name"], rule)
        conf = file_of(self.t["web01"], "/etc/rsyslog.conf")
        self.assertLess(conf.index("$IncludeConfig /etc/rsyslog.d/*.conf"), conf.index('include(file="/etc/rsyslog.d/remote/*.conf"'))

    def test_rsyslog_first_rule_regex_matches_executor_lines_only(self) -> None:
        inv = inventory()
        name, uid = inv["accounts"]["executor"]["name"], str(inv["accounts"]["executor"]["uid"])
        rule = file_of(self.t["web01"], "/etc/rsyslog.d/00-opsjobs.conf")
        word = re.search(r're_match\(\$msg, "(\(\^\|\[\^\[:alnum:\]_\.-\]\)' + re.escape(name) + r'[^"]*)"\)', rule).group(1)
        uid_re = re.search(r're_match\(\$msg, "(\(user-\|@[^"]*)"\)', rule).group(1)
        # rsyslog 가 문자열을 푼 뒤의 정규식으로 본다(설정 파일의 \$ 는 정규식의 $ 줄 끝 경계다).
        word, uid_re = rainer_unescape(word), rainer_unescape(uid_re)
        posix = lambda pattern: pattern.replace("[:alnum:]", "A-Za-z0-9")  # noqa: E731
        hit = [f"Accepted publickey for {name} from 192.0.2.1 port 50000 ssh2",
               f"pam_unix(sudo:session): session opened for user root(uid=0) by {name}(uid={uid})",
               f"New session 4 of user {name}.",
               f"pam_unix(sshd:session): session closed for user {name}"]
        miss = ["Accepted publickey for sysop from 192.0.2.1 port 1 ssh2", "Started session-1450.scope.",
                f"{name}s logged", f"/var/log/{name}s.log rotated", f"session closed for user x.{name}"]
        for line in hit:
            self.assertRegex(line, posix(word))
        for line in miss:
            self.assertNotRegex(line, posix(word))
        self.assertRegex(f"Started user@{uid}.service - User Manager for UID {uid}.", uid_re)
        self.assertRegex(f"Removed slice user-{uid}.slice.", uid_re)
        self.assertRegex(f"Stopped User Manager for UID {uid}", uid_re)
        self.assertNotRegex(f"Started session-{uid}.scope - Session {uid} of User sysop.", uid_re)
        self.assertNotRegex(f"Stopped User Manager for UID {uid}0", uid_re)

    def test_rsyslog_files_have_no_bare_dollar_in_double_quotes(self) -> None:
        # rsyslog 8.2312 은 큰따옴표 문자열 안의 맨 $ 를 거절하고 설정 전체를 못 읽는다(\$ 로 써야 한다).
        # 진짜 파서 검사는 tools/rsyslog-check.sh(docker, 적용 전 점검). 여기서는 꼴만 본다.
        seen = set()
        for path in (T_INV, S_INV):
            for bundle in bundles(path).values():
                for entry in bundle["files"]:
                    if not entry["dest"].startswith(RSYSLOG_DESTS):
                        continue
                    seen.add(entry["dest"])
                    with self.subTest(host=bundle["host"], dest=entry["dest"]):
                        self.assertEqual(rainer_bare_dollars(entry["text"]), [])
        self.assertEqual(seen, {"/etc/rsyslog.conf", "/etc/rsyslog.d/00-opsjobs.conf", "/etc/rsyslog.d/remote/10-forward.conf"})

    def test_bare_dollar_scan_catches_the_unescaped_form(self) -> None:
        self.assertEqual(rainer_bare_dollars('if re_match($msg, "(a|$)") then stop\n'), ['"(a|$)" (1행)'])
        self.assertEqual(rainer_bare_dollars('if re_match($msg, "(a|\\$)") then stop\n'), [])
        self.assertEqual(rainer_bare_dollars('# "$ 주석" 은 보지 않는다\n$IncludeConfig /etc/rsyslog.d/*.conf\n'), [])
        self.assertEqual(rainer_bare_dollars('action(type="omfile" file="/var/log/a\\"$b")\n'), ['"/var/log/a\\"$b" (1행)'])
        self.assertEqual(rainer_unescape("(a|\\$)\\\\"), "(a|$)\\")
        with self.assertRaises(ValueError):
            rainer_unescape("\\.")

    def test_syslog_forward_targets_the_tunnel_address(self) -> None:
        forward = file_of(self.t["db01"], "/etc/rsyslog.d/remote/10-forward.conf")
        self.assertIn('target="10.250.0.1"', forward)
        self.assertIn('protocol="udp"', forward)

    def test_router_turns_off_icmp_redirects_on_all_default_and_every_nic(self) -> None:
        # T3 차례 4(APT3 §2-5): default 는 나중에 생기는 NIC 몫이고 커널은 all 과 NIC 값의 OR 로 redirect 를 보내므로 NIC 마다 끈다.
        inv = inventory()
        router = next(h for h in inv["hosts"] if h["role"] == "router")
        bundle = self.t["rt-core01"]
        entry = next(e for e in bundle["files"] if e["dest"] == render.ROUTER_REDIRECTS_CONF)
        self.assertEqual(render.ROUTER_REDIRECTS_CONF, "/etc/sysctl.d/61-router-redirects.conf")
        self.assertEqual((entry["step"], entry["mode"], entry["owner"]), ("router", "0644", "root"))
        settings = [l for l in entry["text"].splitlines() if l and not l.startswith("#")]
        self.assertEqual(settings, ["net.ipv4.conf.all.send_redirects = 0", "net.ipv4.conf.default.send_redirects = 0"]
                         + [f"net.ipv4.conf.{nic['ifname']}.send_redirects = 0" for nic in router["nics"]])
        self.assertEqual(len(settings), 2 + 5)
        self.assertEqual(bundle["env"]["ROUTER_REDIRECTS_CONF"], render.ROUTER_REDIRECTS_CONF)
        # 60-host.conf 는 포워딩과 rp_filter 만, redirect 는 이 파일 하나가 맡는다.
        host_conf = file_of(bundle, "/etc/sysctl.d/60-host.conf")
        self.assertIn("net.ipv4.ip_forward = 1\n", host_conf)
        self.assertIn("net.ipv4.conf.all.rp_filter = 1\n", host_conf)
        self.assertNotRegex(host_conf, r"(?m)^[^#]*send_redirects")
        for path in (T_INV, S_INV):
            for name, other in bundles(path).items():
                if other["role"] == "router":
                    continue
                with self.subTest(inventory=path.name, host=name):
                    self.assertNotIn(render.ROUTER_REDIRECTS_CONF, {e["dest"] for e in other["files"]})
                    self.assertNotIn("ROUTER_REDIRECTS_CONF", other["env"])
                    self.assertNotIn("send_redirects", file_of(other, "/etc/sysctl.d/60-host.conf"))

    def test_management_nic_guard_on_servers_and_router(self) -> None:
        guard = file_of(self.t["was01"], "/etc/nftables.d/10-mgmt-guard.nft")
        self.assertIn('iifname "ens19" ip saddr 192.168.127.1 tcp dport 22 accept', guard)
        self.assertIn('iifname "ens19" drop', guard)
        router_guard = file_of(self.t["rt-core01"], "/etc/nftables.d/10-mgmt-guard.nft")
        self.assertIn('iifname "ens22" counter drop', router_guard)
        self.assertIn('oifname "ens22" counter drop', router_guard)
        # 관리 대역 거절은 forward 훅보다 먼저라 관리망 격리 (2) 의 거절은 이 카운터에 남는다(runbook §7).
        self.assertIn('iifname != "ens22" ip daddr 192.168.127.0/24 counter drop', router_guard)
        prerouting = router_guard[router_guard.index("chain prerouting"):router_guard.index("chain input")]
        self.assertEqual([l.strip() for l in prerouting.splitlines() if "drop" in l],
                         ['iifname != "ens22" ip daddr 192.168.127.0/24 counter drop'])
        netplan = file_of(self.t["was01"], "/etc/netplan/01-host.yaml")
        mgmt = netplan[netplan.index("ens19:"):]
        self.assertNotIn("routes:", mgmt)

    def test_management_ssh_is_untracked_on_every_host(self) -> None:
        # conntrack 표가 가득 차는 장애 동안에도 러너의 정리 SSH 가 붙게 관리 SSH 는 conntrack 에서 뺀다(APT2 §7, 라우터와 서버, T 와 S).
        # 빠진 흐름은 untracked 라 input 의 "관리 주소 22번 accept" 가 받는다(established 줄에 기대지 않는다).
        for path in (T_INV, S_INV):
            inv = inventory(path)
            mgmt_host = next(v for v in inv["networks"].values() if v.get("management"))["host_addr"]
            for host in inv["hosts"]:
                mgmt = next(n for n in host["nics"] if inv["networks"][n["net"]].get("management"))
                guard = file_of(bundles(path)[host["name"]], "/etc/nftables.d/10-mgmt-guard.nft")
                with self.subTest(inventory=path.name, host=host["name"]):
                    self.assertIn(f'iifname "{mgmt["ifname"]}" ip saddr {mgmt_host} ip daddr {mgmt["addr"]} tcp dport 22 notrack', guard)
                    self.assertIn(f'oifname "{mgmt["ifname"]}" ip saddr {mgmt["addr"]} ip daddr {mgmt_host} tcp sport 22 notrack', guard)
                    self.assertIn(f'iifname "{mgmt["ifname"]}" ip saddr {mgmt_host} tcp dport 22 accept', guard)
                    self.assertEqual(len(re.findall(r"notrack", re.sub(r"(?m)^#.*$", "", guard))), 2)
                    self.assertEqual(guard.count("priority -300"), 2)

    def test_rendered_nft_files_load_in_a_kernel(self) -> None:
        # 진짜 nft 가 렌더된 표를 받아들이는지(notrack 이 raw 우선순위 filter 사슬에서 되는지 포함) 네임스페이스에서 본다.
        for path in (T_INV, S_INV):
            for name, bundle in bundles(path).items():
                files = [e for e in bundle["files"] if e["dest"].startswith("/etc/nftables.d/")]
                with self.subTest(inventory=path.name, host=name), tempfile.TemporaryDirectory() as raw:
                    paths = []
                    for entry in files:
                        target = Path(raw) / Path(entry["dest"]).name
                        target.write_text(entry["text"])
                        paths.append(str(target))
                    script = "set -e; for f in \"$@\"; do nft -f \"$f\"; done; nft list ruleset"
                    done = subprocess.run(["unshare", "-rn", "sh", "-c", script, "_", *paths], capture_output=True, text=True)
                    self.assertEqual(done.returncode, 0, done.stderr)
                    self.assertIn("tcp dport 22 notrack", done.stdout)
                    self.assertIn("priority raw", done.stdout)

    def test_router_acl_allows_only_tier_flows_and_monitoring(self) -> None:
        acl = file_of(self.t["rt-core01"], "/etc/nftables.d/30-acl.nft")
        self.assertIn("policy drop", acl)
        self.assertIn("ip saddr 192.168.124.0/24 ip daddr 192.168.125.0/24 tcp dport 8080 accept", acl)
        self.assertIn("ip saddr 192.168.125.0/24 ip daddr 192.168.126.0/24 tcp dport 5432 accept", acl)
        self.assertNotRegex(acl, r"ip saddr 192\.168\.124\.0/24 ip daddr 192\.168\.126\.0/24 tcp")
        notrack = file_of(self.t["rt-core01"], "/etc/nftables.d/20-monitor-notrack.nft")
        self.assertIn("udp dport 161 notrack", notrack)
        self.assertIn("udp dport { 162, 514 } notrack", notrack)

    def test_snmpd_reports_speed_traps_and_overrides_only_ifoutdiscards(self) -> None:
        conf = file_of(self.t["rt-core01"], "/etc/snmp/snmpd.conf")
        self.assertIn("linkUpDownNotifications yes", conf)
        for ifname in ("ens19", "ens20", "ens21"):
            self.assertIn(f"interface {ifname} 6 1000000000", conf)
        self.assertIn("pass_persist .1.3.6.1.2.1.2.2.1.19 ", conf)
        self.assertNotIn("@@SECRET", conf)
        community = file_of(self.t["rt-core01"], "/etc/snmp/snmpd.community.conf")
        self.assertIn("@@SECRET:snmp_community@@", community)
        dropin = file_of(self.t["rt-core01"], "/etc/systemd/system/snmpd.service.d/10-monitor.conf")
        exec_lines = [line for line in dropin.splitlines() if line.startswith("ExecStart=/")]
        self.assertEqual(len(exec_lines), 1)
        self.assertNotIn("mteTrigger", exec_lines[0])
        self.assertIn("-I -smux ", exec_lines[0])
        self.assertEqual(json.loads(file_of(self.t["rt-core01"], "/etc/snmp/ifdiscards.json"))["augment"],
                         ["ens19", "ens20", "ens21"])

    def test_secrets_stay_placeholders(self) -> None:
        for bundle in self.t.values():
            for entry in bundle["files"]:
                self.assertNotRegex(entry["text"], r"password=\"[^@\"]")
        ctx = file_of(self.t["was01"], "/etc/tomcat10/Catalina/localhost/petition.xml")
        self.assertIn('password="@@SECRET:db_password@@"', ctx)
        self.assertIn("org.apache.tomcat.jdbc.pool.DataSourceFactory", ctx)
        self.assertIn("jdbc:postgresql://192.168.126.11:5432/petition", ctx)

    def test_web_balances_both_was_with_sticky_routes(self) -> None:
        site = file_of(self.t["web01"], "/etc/apache2/sites-available/petition.conf")
        self.assertIn('BalancerMember "http://192.168.125.11:8080" route=was01', site)
        self.assertIn('BalancerMember "http://192.168.125.12:8080" route=was02', site)
        self.assertIn("stickysession=JSESSIONID", site)
        server = file_of(self.t["was02"], "/etc/tomcat10/server.xml")
        self.assertIn('jvmRoute="was02"', server)
        self.assertIn('autoDeploy="false"', server)

    def test_no_apm_hooks_anywhere(self) -> None:
        for bundle in self.t.values():
            for entry in bundle["files"]:
                self.assertNotIn("javaagent", entry["text"])
                self.assertNotRegex(entry["text"], r"\bOTEL_")

    def test_postgres_logging_lives_in_one_file_with_fixed_values(self) -> None:
        logging = file_of(self.t["db01"], "/etc/postgresql/16/main/conf.d/20-logging.conf")
        values = dict(re.findall(r"^([a-z_]+) = (.+)$", logging, re.M))
        self.assertEqual(values["log_min_duration_statement"], "1000")
        self.assertEqual(values["log_lock_waits"], "on")
        self.assertEqual(values["log_statement"], "'ddl'")
        other = [entry for entry in self.t["db01"]["files"] if entry["dest"] != "/etc/postgresql/16/main/conf.d/20-logging.conf"]
        for entry in other:
            self.assertNotRegex(entry["text"], r"^log_", entry["dest"])

    def test_every_postgres_log_setting_changed_from_the_default_names_its_source_and_reason(self) -> None:
        # 검토 세션 Q20-3 답: 기본값에서 바꾸는 항목마다 출처, 기본보다 자세히 남기는 항목은 그 이유.
        logging = file_of(self.t["db01"], "/etc/postgresql/16/main/conf.d/20-logging.conf")
        pg16_defaults = {"log_min_duration_statement": "-1", "log_lock_waits": "off", "log_checkpoints": "on",
                         "log_connections": "off", "log_disconnections": "off", "log_statement": "'none'",
                         "log_temp_files": "-1", "log_autovacuum_min_duration": "10min"}
        blocks = re.findall(r"((?:^#.*\n)+)((?:^[a-z_]+ = .+\n)+)", logging, re.M)
        seen = {}
        for comment, settings in blocks:
            for name, value in re.findall(r"^([a-z_]+) = (.+)$", settings, re.M):
                seen[name] = (value, comment)
        self.assertEqual(set(pg16_defaults) - set(seen), set())
        changed = {name for name, default in pg16_defaults.items() if seen[name][0] != default}
        self.assertEqual(changed, {"log_min_duration_statement", "log_lock_waits", "log_connections",
                                   "log_disconnections", "log_statement"})
        for name in changed:
            with self.subTest(setting=name):
                comment = seen[name][1]
                self.assertIn("출처", comment)
                self.assertRegex(comment, r"\[(CIS|GitLab|pgBadger)\]")
                self.assertIn("기본보다 자세히 남기는 이유", comment)
        self.assertNotIn("추정", logging)

    def test_loadgen_unit_points_at_the_web_and_sends_nothing_out(self) -> None:
        unit = file_of(self.t["loadgen"], "/etc/systemd/system/loadgen.service")
        self.assertIn("Environment=ENTRY_URL=http://192.168.124.11:80/petition", unit)
        self.assertIn("Environment=PEAK_RPS=30", unit)
        self.assertIn("Environment=TROUGH_RPS=5", unit)
        self.assertIn("--window-log /var/lib/k6/k6-civil-petition-windows.jsonl", unit)
        # 평시 24시간 판정의 대표 질의(search)와 참고 둘의 단계별 p95 를 창 기록에 싣는다.
        self.assertIn("--window-step search --window-step my-list --window-step inbox", unit)
        dests = {entry["dest"] for entry in self.t["loadgen"]["files"]}
        self.assertNotIn("/etc/rsyslog.d/remote/10-forward.conf", dests)

    def test_written_bundle_has_tsv_steps_env_and_meta(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = render.write_bundle(self.t["db01"], Path(raw))
            rows = [line.split("\t") for line in (root / "files.tsv").read_text().splitlines()]
            self.assertTrue(all(len(row) == 6 for row in rows))
            for row in rows:
                self.assertTrue((root / row[5]).is_file())
            env = (root / "host.env").read_text()
            self.assertIn("DATA_DISK_SERIAL='pgdata'", env)
            self.assertIn("SYSLOG_FORWARD='1'", env)
            done = subprocess.run(["bash", "-c", f"set -u; . {root}/host.env; echo $HOST_NAME $DATA_MOUNT"],
                                  capture_output=True, text=True, check=True)
            self.assertEqual(done.stdout.strip(), "db01 /pgdata")
            meta = json.loads((root / "meta.json").read_text())
            self.assertEqual(len(meta["files"]), len(rows))


class HypervisorRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.inv = inventory(T_INV)
        self.files = render.hypervisor_files(self.inv)
        self.net = self.files["/etc/network/interfaces.d/t4-net"]

    def test_five_bridges_and_host_addresses_only_on_upstream_and_management(self) -> None:
        self.assertEqual(re.findall(r"^auto (vmbr\d+)$", self.net, re.M), ["vmbr7", "vmbr8", "vmbr9", "vmbr10", "vmbr11"])
        # 관리 브리지는 둘째 주소로 운영자 단말 출발 주소를 갖는다(설계 §6-2 운영자 경로, Q35).
        self.assertEqual(re.findall(r"^\taddress (\S+)$", self.net, re.M),
                         ["192.168.123.1/24", "192.168.127.1/24", "192.168.127.5/24"])
        for bridge in ("vmbr8", "vmbr9", "vmbr10"):
            self.assertIn(f"iface {bridge} inet manual", self.net)

    def test_nat_is_four_masquerades_and_one_runner_snat(self) -> None:
        added = set(re.findall(r"iptables -t nat -A (POSTROUTING .*?)$", self.net, re.M))
        masq = [rule for rule in added if "MASQUERADE" in rule]
        snat = [rule for rule in added if "SNAT" in rule]
        self.assertEqual(len(masq), 4)
        self.assertEqual(len(snat), 2)
        self.assertNotIn("192.168.127.0/24 -o vmbr0.200", " ".join(masq))
        # 실행기(러너 주소)와 운영자 단말(덧붙인 주소)은 관리 브리지에서 서로 다른 출발 주소가 된다(Q35 조건 ①).
        self.assertIn("POSTROUTING -s 192.168.122.20 -o vmbr11 -p tcp --dport 22 -j SNAT --to-source 192.168.127.1", snat)
        self.assertIn("POSTROUTING -s 192.168.122.21 -o vmbr11 -p tcp --dport 22 -j SNAT --to-source 192.168.127.5", snat)

    def test_business_routes_go_through_the_router(self) -> None:
        for cidr in ("192.168.124.0/24", "192.168.125.0/24", "192.168.126.0/24"):
            self.assertIn(f"ip route replace {cidr} via 192.168.123.254 dev vmbr7", self.net)

    def test_forward_chain_isolates_legacy_management_and_real_monitor(self) -> None:
        forward, inputs = render._chain_rules(self.inv)
        chain = self.inv["hypervisor"]["chain"]
        self.assertEqual(forward[0], f"-A {chain} -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT")
        self.assertEqual(forward[-1], f"-A {chain} -j RETURN")
        runner_allow = forward.index(f"-A {chain} -s 192.168.122.20 -d 192.168.127.0/24 -o vmbr11 -p tcp --dport 22 -j ACCEPT")
        self.assertLess(runner_allow, forward.index(f"-A {chain} -o vmbr11 -j DROP"))
        operator_allow = forward.index(f"-A {chain} -s 192.168.122.21 -d 192.168.127.0/24 -o vmbr11 -p tcp --dport 22 -j ACCEPT")
        self.assertLess(operator_allow, forward.index(f"-A {chain} -o vmbr11 -j DROP"))
        for cidr in ("192.168.123.0/24", "192.168.124.0/24", "192.168.125.0/24", "192.168.126.0/24"):
            self.assertIn(f"-A {chain} -s 192.168.122.0/24 -d {cidr} -j DROP", forward)
            self.assertIn(f"-A {chain} -s {cidr} -d 192.168.230.119 -j DROP", forward)
            self.assertIn(f"-A {chain} -s {cidr} -o vmbr0.200 -j ACCEPT", forward)
            # 터널이 내려가 있어도 관제 주소로 가는 보고는 사내망 NAT 로 나가지 않는다(T0 리뷰 지적 14).
            leak = forward.index(f"-A {chain} -s {cidr} -d 10.250.0.0/30 -o vmbr0.200 -j DROP")
            self.assertLess(leak, forward.index(f"-A {chain} -s {cidr} -o vmbr0.200 -j ACCEPT"))
        # wg0 의 허용과 거절은 T0 의 터널 스크립트 몫이라 이 파일에 wg0 가 없다.
        self.assertFalse(any("wg0" in rule for rule in forward))
        self.assertNotIn("wg0", self.net)
        drop_real = forward.index(f"-A {chain} -s 192.168.124.0/24 -d 192.168.230.119 -j DROP")
        allow_uplink = forward.index(f"-A {chain} -s 192.168.124.0/24 -o vmbr0.200 -j ACCEPT")
        self.assertLess(drop_real, allow_uplink)
        self.assertIn(f"-A {self.inv['hypervisor']['input_chain']} -i vmbr11 -p udp --dport 123 -j ACCEPT", inputs)
        self.assertIn(f"-A {self.inv['hypervisor']['input_chain']} -i vmbr11 -j DROP", inputs)

    def test_icmp_echo_to_the_hypervisor_only_reaches_the_bridge_own_address(self) -> None:
        # 업무 NIC 에서 66 의 다른 주소(관리 주소 192.168.127.1 등)로 가는 핑은 66 자신의 INPUT 이 되므로, 브리지마다
        # ICMP echo 허용을 그 브리지 자기 주소로 좁힌다(APT2 §7 관리망 격리 (2)). 관리 브리지는 ICMP 를 받지 않는다. S 도 같다.
        for path in (T_INV, S_INV):
            inv = inventory(path)
            _, inputs = render._chain_rules(inv)
            chain = inv["hypervisor"]["input_chain"]
            management = render._management_net(inv)
            icmp = [r for r in inputs if "icmp" in r]
            wanted = [f"-A {chain} -i {net['bridge']} -d {net['host_addr']} -p icmp --icmp-type echo-request -j ACCEPT"
                      for name, net in inv["networks"].items() if net.get("host_addr") and name != management]
            with self.subTest(inventory=path.name):
                self.assertTrue(wanted)
                self.assertEqual(icmp, wanted)
                for net in (n for name, n in inv["networks"].items() if n.get("host_addr")):
                    accept = inputs.index(next(r for r in inputs if f"-i {net['bridge']} " in r))
                    self.assertLess(accept, inputs.index(f"-A {chain} -i {net['bridge']} -j DROP"))
        self.assertIn("-A T4-IN -i vmbr7 -d 192.168.123.1 -p icmp --icmp-type echo-request -j ACCEPT",
                      render._chain_rules(self.inv)[1])

    def test_rules_are_idempotent_and_never_touch_the_default_policy(self) -> None:
        self.assertNotRegex(self.net, r"iptables[^\n]* -P ")
        self.assertNotIn("tb-nat", re.sub(r"^#.*$", "", self.net, flags=re.M))
        for line in self.net.splitlines():
            if line.startswith("\tpost-up iptables -t nat"):
                self.assertIn(" -C ", line)
                self.assertIn(" || ", line)
        hooks = re.findall(r"post-up iptables -C (FORWARD|INPUT) .* \|\| iptables -I \1 1 ", self.net)
        self.assertEqual(len(hooks), 5 * 2 + 2)

    def test_chrony_allow_and_modules_on_the_hypervisor(self) -> None:
        self.assertEqual(self.files["/etc/chrony/conf.d/t4-net.conf"].splitlines()[-1], "allow 192.168.127.0/24")
        self.assertIn("sch_netem", self.files["/etc/modules-load.d/t4-net-qdisc.conf"])
        s_files = render.hypervisor_files(inventory(S_INV))
        self.assertFalse(any(path.startswith("/etc/modules-load.d/") for path in s_files))
        s_rules = s_files["/etc/network/s5-net.rules"]
        self.assertIn("-s 192.168.132.10 -d 192.168.131.11 -p tcp --dport 80 -j ACCEPT", s_rules)
        self.assertIn("ifup vmbr12 vmbr13 vmbr14", render.hypervisor_commands(inventory(S_INV), "up"))

    def test_every_testbed_bridge_turns_ipv6_off_and_no_other_interface_is_touched(self) -> None:
        # 66 의 ssh, Proxmox 웹이 IPv6 로도 듣고 ip6tables 가 비어 있어, 브리지 IPv6 가 켜져 있으면 관리망 격리가 샌다.
        for path, bridges in ((T_INV, ["vmbr7", "vmbr8", "vmbr9", "vmbr10", "vmbr11"]), (S_INV, ["vmbr12", "vmbr13", "vmbr14"])):
            inv = inventory(path)
            files = render.hypervisor_files(inv)
            net = files[inv["hypervisor"]["file"]]
            stanzas = re.split(r"^auto ", net, flags=re.M)[1:]
            self.assertEqual([stanza.split()[0] for stanza in stanzas], bridges)
            for stanza in stanzas:
                bridge = stanza.split()[0]
                lines = stanza.splitlines()
                # ifupdown2 속성으로 끈다(브리지를 만든 뒤 관리상 올리기 전에 걸린다). 한 번만.
                self.assertEqual(lines.count("\tdisable-ipv6 yes"), 1, bridge)
                self.assertLess(lines.index("\tdisable-ipv6 yes"), min(i for i, l in enumerate(lines) if l.startswith("\tpost-up ")))
                # 올라온 뒤 꺼짐과 주소 0 을 확인하고, 아니면 그 브리지를 내리고 실패한다.
                check = [l for l in lines if l.startswith("\tpost-up ") and "disable_ipv6" in l]
                self.assertEqual(len(check), 1, bridge)
                self.assertIn(f"/proc/sys/net/ipv6/conf/{bridge}/disable_ipv6", check[0])
                self.assertIn(f"ip -6 -o addr show dev {bridge}", check[0])
                self.assertIn(f"ip link set dev {bridge} down", check[0])
                self.assertTrue(check[0].rstrip().endswith("false; }"))
                post = [l for l in lines if l.startswith("\tpost-up ")]
                routes_and_nat = [i for i, l in enumerate(post) if "ip route" in l or "-t nat" in l]
                if routes_and_nat:
                    self.assertLess(post.index(check[0]), min(routes_and_nat))
            # 기존 브리지(vmbr0, vmbr0.200, vmbr1)와 전역 값(all, default)에는 IPv6 줄이 없다.
            self.assertEqual(net.count("disable-ipv6"), len(bridges) + 1)   # 머리 주석 한 번
            for other in ("vmbr0", "vmbr0.200", "vmbr1", "all", "default"):
                self.assertNotIn(f"/conf/{other}/", net)
                self.assertNotIn(f"conf.{other}.", net)
            self.assertNotRegex(net, r"(?m)^auto (vmbr0|vmbr1)\b")
            self.assertFalse(any(dest.startswith("/etc/sysctl") for dest in files))
            self.assertEqual(
                re.findall(r"for b in ([^;]+);", render.hypervisor_commands(inv, "ipv6-state")[0]), [" ".join(bridges)]
            )


IPTABLES_SCRIPT = r'''
import json, re, subprocess, sys
net_file, rules_tmp, rules_path, ipv6_cmd = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
stanzas, current = {}, None
for line in open(net_file, encoding="utf-8"):
    m = re.match(r"auto (\S+)", line)
    if m:
        current = m.group(1)
        stanzas[current] = {"up": [], "down": [], "disable_ipv6": False}
        continue
    if current and line.rstrip("\n") == "\tdisable-ipv6 yes":
        stanzas[current]["disable_ipv6"] = True
        continue
    m = re.match(r"\t(post-up|pre-down) (.*)", line)
    if m and current:
        stanzas[current]["up" if m.group(1) == "post-up" else "down"].append(m.group(2).replace(rules_path, rules_tmp))
def run(cmd):
    if cmd.startswith("ip route"):
        return  # 브리지가 없는 네임스페이스라 경로는 건너뛴다(iptables 만 본다)
    subprocess.run(["sh", "-c", cmd], check=True)
def up(name):
    for c in stanzas[name]["up"]: run(c)
def down(name):
    for c in stanzas[name]["down"]: run(c)
def save():
    return subprocess.run(["iptables-save"], capture_output=True, text=True, check=True).stdout
def create(name, honour_disable_ipv6=True):
    # ifupdown2 의 address 애드온처럼 disable-ipv6 를 링크를 만든 뒤, 관리상 올리기 전에 건다.
    subprocess.run(["ip", "link", "add", name, "type", "dummy"], check=True)
    if honour_disable_ipv6 and stanzas.get(name, {}).get("disable_ipv6"):
        with open(f"/proc/sys/net/ipv6/conf/{name}/disable_ipv6", "w") as handle:
            handle.write("1")
    subprocess.run(["ip", "link", "set", name, "up"], check=True)
def ipv6_state():
    return subprocess.run(["sh", "-c", ipv6_cmd], capture_output=True, text=True, check=True).stdout
def link_ipv6(name):
    disabled = open(f"/proc/sys/net/ipv6/conf/{name}/disable_ipv6").read().strip()
    addrs = subprocess.run(["ip", "-6", "-o", "addr", "show", "dev", name], capture_output=True, text=True, check=True).stdout
    return {"disable_ipv6": disabled, "addrs": len(addrs.splitlines())}
def legacy():
    # 66 의 기존 tb-nat 과 같은 꼴(확인 없는 -A).
    subprocess.run("iptables -t nat -A POSTROUTING -s 192.168.122.0/24 -o vmbr0.200 -j MASQUERADE", shell=True, check=True)
create("vmbr1")                         # 66 의 기존 브리지 자리(IPv6 를 건드리지 않아야 한다)
for name in stanzas:
    create(name)
legacy()
subprocess.run("iptables -t nat -A PREROUTING -d 192.168.200.66/32 -i vmbr0.200 -p tcp --dport 8091 -j DNAT --to-destination 192.168.122.20:8091", shell=True, check=True)
out = {"order": list(stanzas), "before": save()}
for name in stanzas: up(name)
for name in stanzas: up(name)          # 같은 post-up 이 다시 돌아도 한 벌
out["up"] = save()
import time
time.sleep(0.3)                          # 링크 로컬이 생길 틈
out["ipv6_up"] = ipv6_state()
out["links_up"] = {name: link_ipv6(name) for name in stanzas}
out["legacy_link"] = link_ipv6("vmbr1")
mgmt = list(stanzas)[-1]
down(mgmt)
out["mgmt_down"] = save()
subprocess.run(["ip", "link", "del", mgmt], check=True)   # ifdown 은 브리지를 지운다
create(mgmt)                                              # ifup 이 다시 만든다
up(mgmt)
out["mgmt_up_again"] = save()
time.sleep(0.3)
out["mgmt_ipv6_again"] = link_ipv6(mgmt)
subprocess.run(["iptables-restore", "--noflush", rules_tmp], check=True)   # 규칙 다시 넣기
out["reloaded"] = save()
legacy()                                 # ifreload -a 가 tb-nat 을 다시 돌린 꼴
out["legacy_twice"] = save()
subprocess.run("iptables -t nat -D POSTROUTING -s 192.168.122.0/24 -o vmbr0.200 -j MASQUERADE", shell=True, check=True)
for name in reversed(list(stanzas)): down(name)
out["down"] = save()
# 규칙 파일이 없으면 브리지는 격리 없이 올라오지 않고 내려간다(fail closed).
import os
os.rename(rules_tmp, rules_tmp + ".gone")
first = list(stanzas)[1]
subprocess.run(["ip", "link", "set", first, "up"], check=True)
failed = [subprocess.run(["sh", "-c", c], capture_output=True, text=True).returncode
          for c in stanzas[first]["up"]
          if not c.startswith("ip route") and "-t nat" not in c and "disable_ipv6" not in c]
link = subprocess.run(["ip", "-j", "link", "show", first], capture_output=True, text=True, check=True).stdout
out["missing_rules"] = {"failed": failed, "link": json.loads(link)[0]["flags"], "save": save()}
# IPv6 가 켜진 채로 올라온 브리지(disable-ipv6 를 모르는 ifupdown 의 꼴)는 post-up 확인이 내리고 실패한다.
os.rename(rules_tmp + ".gone", rules_tmp)
open_one = list(stanzas)[2]
subprocess.run(["ip", "link", "del", open_one], check=True)
create(open_one, honour_disable_ipv6=False)
time.sleep(0.3)
out["ipv6_open_state"] = ipv6_state()
codes = [subprocess.run(["sh", "-c", c], capture_output=True, text=True) for c in stanzas[open_one]["up"]
         if not c.startswith("ip route") and "-t nat" not in c]
link = subprocess.run(["ip", "-j", "link", "show", open_one], capture_output=True, text=True, check=True).stdout
out["ipv6_open"] = {"codes": [c.returncode for c in codes], "stderr": "".join(c.stderr for c in codes),
                    "link": json.loads(link)[0]["flags"], "check_line": [c for c in stanzas[open_one]["up"] if "disable_ipv6" in c]}
print(json.dumps(out))
'''


class HypervisorIptablesKernelTests(unittest.TestCase):
    """렌더한 66 망 파일의 post-up, pre-down 을 사용자 네임스페이스의 진짜 iptables 로 돌린다(리뷰 지적 1, 2)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.inv = inventory(T_INV)
        files = render.hypervisor_files(cls.inv)
        with tempfile.TemporaryDirectory() as raw:
            net = Path(raw) / "net"
            rules = Path(raw) / "rules"
            net.write_text(files[cls.inv["hypervisor"]["file"]])
            rules.write_text(files[render.rules_path(cls.inv)])
            done = subprocess.run(["unshare", "-rn", sys.executable, "-c", IPTABLES_SCRIPT, str(net), str(rules),
                                   render.rules_path(cls.inv), render.hypervisor_commands(cls.inv, "ipv6-state")[0]],
                                  capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, done.stderr
        cls.out = json.loads(done.stdout.strip().splitlines()[-1])

    def test_up_adds_only_this_testbed_and_runs_twice_without_duplicates(self) -> None:
        self.assertEqual(hvcheck.check(self.inv, "up", self.out["before"], self.out["up"]), [])

    def test_icmp_echo_accept_names_the_bridge_address_in_the_kernel(self) -> None:
        # 실제 iptables 가 받은 꼴: 목적지 없는 ICMP 허용이 남지 않는다.
        icmp = [l for l in self.out["up"].splitlines() if l.startswith("-A T4-IN") and "icmp" in l]
        self.assertEqual(len(icmp), 1, icmp)
        self.assertRegex(icmp[0], r"-d 192\.168\.123\.1/32 -i vmbr7 -p icmp -m icmp --icmp-type 8 -j ACCEPT$")

    def test_down_returns_exactly_to_the_starting_rules(self) -> None:
        self.assertEqual(hvcheck.check(self.inv, "down", self.out["before"], self.out["down"]), [])
        self.assertNotIn("T4-", self.out["down"])

    def test_the_check_catches_a_policy_change(self) -> None:
        changed = self.out["up"].replace(":FORWARD ACCEPT", ":FORWARD DROP")
        self.assertTrue(hvcheck.check(self.inv, "up", self.out["before"] + "\n*filter\n:FORWARD ACCEPT [0:0]\nCOMMIT\n", changed))

    def test_the_check_catches_a_legacy_rule_added_twice(self) -> None:
        problems = hvcheck.check(self.inv, "up", self.out["before"], self.out["legacy_twice"])
        self.assertTrue(any("192.168.122.0/24" in p for p in problems), problems)

    def test_taking_down_the_management_bridge_alone_keeps_business_isolation(self) -> None:
        state = self.out["mgmt_down"]
        chain = self.inv["hypervisor"]["chain"]
        for bridge in ("vmbr7", "vmbr8", "vmbr9", "vmbr10"):
            self.assertIn(f"-A FORWARD -i {bridge} -j {chain}", state)
            self.assertIn(f"-A FORWARD -o {bridge} -j {chain}", state)
        self.assertNotIn(f"-A FORWARD -i vmbr11 -j {chain}", state)
        self.assertIn(f"-A {chain} -s 192.168.122.0/24 -d 192.168.124.0/24 -j DROP", state)
        self.assertIn(f"-A {chain} -s 192.168.124.0/24 -d 192.168.230.119/32 -j DROP", state)
        self.assertEqual(hvcheck.check(self.inv, "up", self.out["before"], self.out["mgmt_up_again"]), [])

    def test_a_missing_rules_file_takes_the_bridge_down_instead_of_running_open(self) -> None:
        result = self.out["missing_rules"]
        self.assertTrue(all(code != 0 for code in result["failed"]), result["failed"])
        self.assertNotIn("UP", result["link"])
        self.assertNotIn("T4-", result["save"])

    def test_the_check_points_at_tunnel_lines_taken_into_the_baseline(self) -> None:
        with_tunnel = self.out["up"] + "*mangle\n:TBWG-FWD - [0:0]\n-A FORWARD -i wg0 -j TBWG-FWD\nCOMMIT\n"
        problems = hvcheck.check(self.inv, "up", self.out["before"], with_tunnel)
        self.assertTrue(any("T0 터널 줄" in p for p in problems), problems)
        rolled = self.out["down"] + "*mangle\n:TBWG-FWD - [0:0]\nCOMMIT\n"
        self.assertTrue(any("T2 를 먼저 되돌리고" in p for p in hvcheck.check(self.inv, "down", self.out["before"], rolled)))

    def test_reloading_the_rules_file_replaces_the_chains_in_place(self) -> None:
        self.assertEqual(hvcheck.lines_of(self.out["reloaded"]), hvcheck.lines_of(self.out["up"]))

    def test_bridges_come_up_without_ipv6_and_the_legacy_bridge_keeps_it(self) -> None:
        for name, state in self.out["links_up"].items():
            self.assertEqual(state, {"disable_ipv6": "1", "addrs": 0}, name)
        # 이 테스트베드 것이 아닌 브리지는 그대로(IPv6 켜짐, 링크 로컬 있음).
        self.assertEqual(self.out["legacy_link"]["disable_ipv6"], "0")
        self.assertGreaterEqual(self.out["legacy_link"]["addrs"], 1)
        # 내렸다 지우고 다시 만들어 올려도 주소가 생기지 않는다(ifdown, ifup).
        self.assertEqual(self.out["mgmt_ipv6_again"], {"disable_ipv6": "1", "addrs": 0})

    def test_the_ipv6_check_passes_on_the_real_state_and_names_the_would_be_link_local(self) -> None:
        problems, notes = hvcheck.check_ipv6(self.inv, self.out["ipv6_up"])
        self.assertEqual(problems, [])
        self.assertEqual([note.split()[0] for note in notes], ["vmbr7", "vmbr8", "vmbr9", "vmbr10", "vmbr11"])
        self.assertTrue(all(re.search(r"링크 로컬 fe80::\S+$", note) for note in notes), notes)

    def test_a_bridge_left_with_ipv6_on_is_taken_down_and_fails_the_check(self) -> None:
        result = self.out["ipv6_open"]
        self.assertEqual(len(result["check_line"]), 1)
        self.assertNotEqual(result["codes"][-1], 0)
        self.assertTrue(all(code == 0 for code in result["codes"][:-1]), result["codes"])
        self.assertIn("vmbr9: IPv6 가 꺼지지 않아 내림", result["stderr"])
        self.assertNotIn("UP", result["link"])
        problems, _ = hvcheck.check_ipv6(self.inv, self.out["ipv6_open_state"])
        self.assertIn("vmbr9 IPv6 가 켜져 있다(disable_ipv6=0)", problems)
        self.assertTrue(any(re.fullmatch(r"vmbr9 에 IPv6 주소 [1-9]\d* 개", p) for p in problems), problems)
        self.assertFalse(any(p.startswith(("vmbr7 ", "vmbr8 ", "vmbr10 ", "vmbr11 ")) for p in problems), problems)


STACK_SCRIPT = r'''
import json, re, subprocess, sys, time
spec = json.loads(sys.argv[1])
def parse(net_file, rules_tmp, rules_path):
    stanzas, current = {}, None
    for line in open(net_file, encoding="utf-8"):
        m = re.match(r"auto (\S+)", line)
        if m:
            current = m.group(1)
            stanzas[current] = {"up": [], "down": [], "disable_ipv6": False}
            continue
        if current and line.rstrip("\n") == "\tdisable-ipv6 yes":
            stanzas[current]["disable_ipv6"] = True
            continue
        m = re.match(r"\t(post-up|pre-down) (.*)", line)
        if m and current:
            stanzas[current]["up" if m.group(1) == "post-up" else "down"].append(m.group(2).replace(rules_path, rules_tmp))
    return stanzas
def run(cmd):
    if cmd.startswith("ip route"):
        return
    subprocess.run(["sh", "-c", cmd], check=True)
def save():
    return subprocess.run(["iptables-save"], capture_output=True, text=True, check=True).stdout
def create(name, disable):
    subprocess.run(["ip", "link", "add", name, "type", "dummy"], check=True)
    if disable:
        with open(f"/proc/sys/net/ipv6/conf/{name}/disable_ipv6", "w") as handle:
            handle.write("1")
    subprocess.run(["ip", "link", "set", name, "up"], check=True)
first, second = (parse(*tb) for tb in spec["testbeds"])
create("vmbr1", False)
for stanzas in (first, second):
    for name, stanza in stanzas.items():
        create(name, stanza["disable_ipv6"])
subprocess.run("iptables -t nat -A POSTROUTING -s 192.168.122.0/24 -o vmbr0.200 -j MASQUERADE", shell=True, check=True)
subprocess.run("iptables -t nat -A PREROUTING -d 192.168.200.66/32 -i vmbr0.200 -p tcp --dport 8091 -j DNAT --to-destination 192.168.122.20:8091", shell=True, check=True)
out = {"before": save()}
for name in first:
    for c in first[name]["up"]: run(c)
out["first_up"] = save()
for name in second:
    for c in second[name]["up"]: run(c)
out["second_up"] = save()
time.sleep(0.3)
out["second_ipv6"] = subprocess.run(["sh", "-c", spec["ipv6"]], capture_output=True, text=True, check=True).stdout
for name in second:
    for c in second[name]["up"]: run(c)
out["second_up_again"] = save()
for name in reversed(list(second)):
    for c in second[name]["down"]: run(c)
out["second_down"] = save()
print(json.dumps(out))
'''


class HypervisorStackKernelTests(unittest.TestCase):
    """S 망 파일을 T 망이 이미 선 66 위에 올리고 내린다(실제 적용 차례, ST2 는 T 뒤). 사용자 네임스페이스의 진짜 iptables.

    S 의 검사기 기준(before)은 T 가 선 뒤에 뜨므로 T 줄은 차이에 나오지 않아야 하고, S 를 올리고 내려도 T 줄은 그대로다.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.t_inv, cls.s_inv = inventory(T_INV), inventory(S_INV)
        with tempfile.TemporaryDirectory() as raw:
            testbeds = []
            for tag, inv in (("t", cls.t_inv), ("s", cls.s_inv)):
                files = render.hypervisor_files(inv)
                net, rules = Path(raw) / f"{tag}.net", Path(raw) / f"{tag}.rules"
                net.write_text(files[inv["hypervisor"]["file"]])
                rules.write_text(files[render.rules_path(inv)])
                testbeds.append([str(net), str(rules), render.rules_path(inv)])
            spec = {"testbeds": testbeds, "ipv6": render.hypervisor_commands(cls.s_inv, "ipv6-state")[0]}
            done = subprocess.run(["unshare", "-rn", sys.executable, "-c", STACK_SCRIPT, json.dumps(spec)],
                                  capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, done.stderr
        cls.out = json.loads(done.stdout.strip().splitlines()[-1])

    def test_s_up_on_top_of_t_passes_the_check_with_the_baseline_taken_after_t(self) -> None:
        self.assertEqual(hvcheck.check(self.t_inv, "up", self.out["before"], self.out["first_up"]), [])
        self.assertEqual(hvcheck.check(self.s_inv, "up", self.out["first_up"], self.out["second_up"]), [])
        problems, notes = hvcheck.check_ipv6(self.s_inv, self.out["second_ipv6"])
        self.assertEqual(problems, [])
        self.assertEqual([note.split()[0] for note in notes], ["vmbr12", "vmbr13", "vmbr14"])

    def test_s_leaves_every_t_line_in_place_and_adds_four_nat_lines(self) -> None:
        # MASQUERADE 둘, 러너 SNAT, 운영자 단말 SNAT(설계 §6-2 운영자 경로, S 도 같은 실행기와 같은 주입).
        t_up, s_up = hvcheck.lines_of(self.out["first_up"]), hvcheck.lines_of(self.out["second_up"])
        self.assertEqual(t_up - s_up, collections.Counter())
        added = s_up - t_up
        nat = [line for (table, line) in added if table == "nat"]
        self.assertEqual(len(nat), 4, nat)
        self.assertIn("-A POSTROUTING -s 192.168.122.21/32 -o vmbr14 -p tcp -m tcp --dport 22 -j SNAT --to-source 192.168.133.5", nat)
        hooks = [line for (table, line) in added if table == "filter" and re.search(r"-j S5-(FWD|IN)$", line)]
        self.assertEqual(len(hooks), 3 * 2 + 3, hooks)
        self.assertFalse(any("T4-" in line for (_, line) in added))

    def test_s_again_is_idempotent_and_down_returns_to_the_t_state(self) -> None:
        self.assertEqual(hvcheck.lines_of(self.out["second_up_again"]), hvcheck.lines_of(self.out["second_up"]))
        self.assertEqual(hvcheck.check(self.s_inv, "down", self.out["first_up"], self.out["second_down"]), [])
        self.assertNotIn("S5-", self.out["second_down"])
        self.assertIn(":T4-FWD", self.out["second_down"])


class HypervisorIpv6CheckTests(unittest.TestCase):
    """검사기의 브리지 IPv6 검사(있음, 없음 둘 다)와 명령줄."""

    GOOD = "\n".join(f"{b} 1 0 be:a7:16:7f:dc:a5" for b in ("vmbr7", "vmbr8", "vmbr9", "vmbr10", "vmbr11")) + "\n"

    def setUp(self) -> None:
        self.inv = inventory(T_INV)

    def test_all_bridges_off_and_without_addresses_pass(self) -> None:
        problems, notes = hvcheck.check_ipv6(self.inv, self.GOOD)
        self.assertEqual(problems, [])
        self.assertIn("vmbr7 disable_ipv6=1, IPv6 주소 0, 켜져 있었다면 생겼을 링크 로컬 fe80::bca7:16ff:fe7f:dca5", notes)

    def test_each_kind_of_leak_fails(self) -> None:
        cases = {
            "vmbr8 0 0 be:a7:16:7f:dc:a5": "vmbr8 IPv6 가 켜져 있다(disable_ipv6=0)",
            "vmbr8 1 2 be:a7:16:7f:dc:a5": "vmbr8 에 IPv6 주소 2 개",
            "vmbr8 - 0 -": "vmbr8 가 없다(IPv6 상태를 뜰 때 브리지가 올라와 있지 않았다)",
            "vmbr8 1 0": "IPv6 상태 줄 꼴이 다르다(이름 disable_ipv6 주소수 MAC): vmbr8 1 0",
            "vmbr8 1 0 zz": "vmbr8 MAC 꼴이 다르다: zz",
        }
        for line, expected in cases.items():
            text = self.GOOD.replace("vmbr8 1 0 be:a7:16:7f:dc:a5", line)
            problems, _ = hvcheck.check_ipv6(self.inv, text)
            self.assertIn(expected, problems, line)
        missing = self.GOOD.replace("vmbr11 1 0 be:a7:16:7f:dc:a5\n", "")
        self.assertIn("IPv6 상태에 vmbr11 줄이 없다", hvcheck.check_ipv6(self.inv, missing)[0])
        doubled = self.GOOD + "vmbr7 1 0 be:a7:16:7f:dc:a5\n"
        self.assertIn("IPv6 상태에 vmbr7 이 두 번 있다", hvcheck.check_ipv6(self.inv, doubled)[0])
        # 다른 테스트베드의 상태만 준 경우.
        s_only = "\n".join(f"{b} 1 0 be:a7:16:7f:dc:a5" for b in ("vmbr12", "vmbr13", "vmbr14"))
        self.assertEqual(len(hvcheck.check_ipv6(self.inv, s_only)[0]), 5)

    def test_eui64_matches_the_address_the_kernel_gave_vmbr7_on_66(self) -> None:
        # 11-18 01:24 KST 66 조회: vmbr7 MAC be:a7:16:7f:dc:a5 에 fe80::bca7:16ff:fe7f:dca5 가 붙어 있었다.
        self.assertEqual(hvcheck.eui64_link_local("be:a7:16:7f:dc:a5"), "fe80::bca7:16ff:fe7f:dca5")
        self.assertEqual(hvcheck.eui64_link_local("02:00:00:00:00:01"), "fe80::ff:fe00:1")

    def _main(self, *extra: str, ipv6: str | None = None, mode: str = "up") -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "before").write_text("*filter\nCOMMIT\n")
            (root / "after").write_text("*filter\nCOMMIT\n")
            args = ["--inventory", str(T_INV), "--mode", mode, "--before", str(root / "before"), "--after", str(root / "after")]
            if ipv6 is not None:
                (root / "ipv6").write_text(ipv6)
                args += ["--ipv6-state", str(root / "ipv6")]
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = hvcheck.main(args + list(extra))
                except SystemExit as stop:
                    code = int(stop.code)
            return code, out.getvalue(), err.getvalue()

    def test_up_without_the_ipv6_state_is_refused(self) -> None:
        code, out, err = self._main()
        self.assertEqual(code, 2)
        self.assertNotIn("PASS", out)
        self.assertIn("--ipv6-state", err)

    def test_down_with_the_ipv6_state_is_refused(self) -> None:
        code, out, err = self._main(ipv6=self.GOOD, mode="down")
        self.assertEqual(code, 2)
        self.assertIn("--mode up 에서만", err)

    def test_up_reports_ipv6_problems_together_with_iptables_problems(self) -> None:
        # 빈 before, after 라 iptables 쪽은 MASQUERADE, SNAT, 체인 수 부족으로 실패한다. IPv6 줄도 함께 나와야 한다.
        code, out, _ = self._main(ipv6=self.GOOD.replace("vmbr9 1 0", "vmbr9 0 1"))
        self.assertEqual(code, 1)
        self.assertIn("FAIL vmbr9 IPv6 가 켜져 있다(disable_ipv6=0)", out)
        self.assertIn("FAIL MASQUERADE 0 != 4", out)
        self.assertNotIn("IPV6 ", out)
        self.assertNotIn("PASS", out)


    def _run(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = hvcheck.main(list(args))
            except SystemExit as stop:
                code = int(stop.code)
        return code, out.getvalue(), err.getvalue()

    def test_ipv6_mode_checks_only_the_bridge_state(self) -> None:
        # 11-19 S 차례 7(v4 APS2): 차례 3 add-net 이 TBWG 줄을 더한 뒤라 차례 2 기준의 up 은 늘 FAIL 이다.
        # 다시 볼 때는 브리지 IPv6 상태만 본다.
        s_good = "\n".join(f"{b} 1 0 be:a7:16:7f:dc:a5" for b in ("vmbr12", "vmbr13", "vmbr14")) + "\n"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "good").write_text(s_good)
            (root / "bad").write_text(s_good.replace("vmbr14 1 0", "vmbr14 0 2"))
            (root / "x").write_text("*filter\nCOMMIT\n")
            code, out, _ = self._run("--inventory", str(S_INV), "--mode", "ipv6", "--ipv6-state", str(root / "good"))
            self.assertEqual(code, 0)
            self.assertEqual(out.splitlines()[-1], "PASS ipv6")
            self.assertEqual(len([line for line in out.splitlines() if line.startswith("IPV6 ")]), 3)
            code, out, _ = self._run("--inventory", str(S_INV), "--mode", "ipv6", "--ipv6-state", str(root / "bad"))
            self.assertEqual(code, 1)
            self.assertIn("FAIL vmbr14 IPv6 가 켜져 있다(disable_ipv6=0)", out)
            self.assertIn("FAIL vmbr14 에 IPv6 주소 2 개", out)
            self.assertNotIn("PASS", out)
            code, out, err = self._run("--inventory", str(S_INV), "--mode", "ipv6")
            self.assertEqual(code, 2)
            self.assertIn("--ipv6-state", err)
            code, out, err = self._run("--inventory", str(S_INV), "--mode", "ipv6", "--ipv6-state", str(root / "good"),
                                       "--before", str(root / "x"), "--after", str(root / "x"))
            self.assertEqual(code, 2)
            self.assertIn("iptables 를 견주지 않는다", err)
            code, out, err = self._run("--inventory", str(S_INV), "--mode", "up", "--ipv6-state", str(root / "good"))
            self.assertEqual(code, 2)
            self.assertIn("--before, --after", err)
            code, out, err = self._run("--inventory", str(S_INV), "--mode", "down", "--before", str(root / "x"))
            self.assertEqual(code, 2)
            self.assertIn("--before, --after", err)

class HypervisorApplyCommandTests(unittest.TestCase):
    def test_bring_up_and_down_only_this_testbeds_bridges(self) -> None:
        inv = inventory(T_INV)
        self.assertEqual(render.hypervisor_commands(inv, "up"), ["ifup vmbr7 vmbr8 vmbr9 vmbr10 vmbr11"])
        self.assertEqual(render.hypervisor_commands(inv, "down"), ["ifdown vmbr11 vmbr10 vmbr9 vmbr8 vmbr7"])
        destroy = "\n".join(plan.destroy(inv))
        self.assertIn("ifdown vmbr11 vmbr10 vmbr9 vmbr8 vmbr7", destroy)
        runbook = (REPO / "docs" / "runbook-civil-petition-apply.md").read_text()
        for text in (runbook, destroy, (REPO / "docs" / "spec-testbed-vm-3tier.md").read_text()):
            for line in text.splitlines():
                if "ifreload -a" in line:
                    self.assertRegex(line, r"쓰지 않는다|다시 돌려", line)
        self.assertIn("hv_iptables_check.py --inventory .../t.json --mode up", runbook)
        self.assertIn("--mode down", runbook)
        # up 확인에는 IPv6 상태가 함께 간다(검사기가 없으면 거절한다).
        for line in runbook.splitlines():
            if "--mode up" in line and "hv_iptables_check.py" in line:
                self.assertIn("--ipv6-state", line)
        self.assertIn("--hypervisor-commands ipv6-state", runbook)
        # 적용 뒤 시간이 지나 IPv6 를 다시 볼 때는 브리지 상태만(--mode ipv6). 적용 직전 기준의 up 을 다시 돌리면 그 사이의
        # 다른 적용(add-net TBWG 줄, 다른 테스트베드 망) 때문에 늘 FAIL 이다(11-19 S 차례 7, v4 S14R).
        row = next(line for line in runbook.splitlines() if line.startswith("| 관리망 격리 (3) |"))
        self.assertIn("hv_iptables_check.py --inventory .../t.json --mode ipv6 --ipv6-state", row)
        self.assertIn("`PASS ipv6`", row)
        self.assertNotIn("PASS up` 이고", row)
        for line in runbook.splitlines():
            self.assertNotIn("확인을 지금 상태로 다시", line)
        # tb-ops 에서 200.66:8091 은 T2 전에도 안 되는 길이다(DNAT 는 -i vmbr0.200 에만, 66 은 8091 을 듣지 않음).
        for line in runbook.splitlines():
            if "tb-ops" in line and "192.168.200.66:8091" in line:
                self.assertRegex(line, r"안 되는|거절")

    def test_runbook_s_section_uses_the_s_inventory_and_skips_the_agent_on_first_install(self) -> None:
        # S 에서 다른 단계는 §14 한 곳에. 그 절의 설치는 등록 창 전이라 sms-agent 를 빼고, 66 확인은 S 인벤토리와 IPv6 상태로.
        runbook = (REPO / "docs" / "runbook-civil-petition-apply.md").read_text()
        start = runbook.index("## 14. S 적용 (ST2)")
        section = runbook[start:runbook.index("\n## ", start + 1)]
        installs = [line for line in section.splitlines() if "deploy/apply.sh --inventory $I --host" in line]
        self.assertTrue(installs)
        for line in installs:
            self.assertIn("--skip-steps sms-agent", line)
            self.assertNotIn("--apt-snapshot", line)
        # 서버마다 짝 T 서버의 apt 설치 시각(쌍 조건 2). 표는 S 의 모든 호스트를 덮고 인벤토리 값과 같다.
        table = {row[0]: row for row in table_rows(spec_section_text(section, "#### 14-3-1. 패키지 색인 시각 (쌍 조건 2)"))}
        self.assertEqual({name: row[-1].strip("`") for name, row in table.items()},
                         {h["name"]: h["apt_snapshot"] for h in inventory(S_INV)["hosts"]})
        self.assertIn("E(가장 이르면 2026-11-19 14:42:48 KST) 뒤에 짓는다", section)
        # 2시간 30분 녹화 시작 차단은 창 안 마지막 변경(가드 시험, S 부하 시작 포함)부터 센다.
        self.assertIn("2시간 30분 녹화 시작 차단은 이 창 안 S 의 마지막 변경 끝부터 센다(차례 7 의 가드 시험과 차례 8 의 S 부하 시작 포함)", section)
        # ST2 점검 표는 행마다 언제(차례 7, 차례 8 뒤, 차례 9) 하는지 칸이 있다.
        checks_table = table_rows(spec_section_text(section, "### 14-4. ST2 점검 (S 문서 §11 ST2 행마다)"))
        self.assertTrue(checks_table)
        self.assertTrue(all(row[1] in {"차례 7", "차례 8 뒤", "차례 9"} for row in checks_table), [r[:2] for r in checks_table])
        # T 를 다시 깔 때도 처음 색인 시각(§6).
        self.assertIn("t.json` 의 서버별 `apt_snapshot`", runbook[:start])
        checks = [line for line in section.splitlines() if "hv_iptables_check.py" in line]
        self.assertTrue(checks)
        for line in checks:
            self.assertIn("--inventory .../s.json", line)
            self.assertNotIn("t.json --mode", line)
        self.assertIn("--mode up", checks[0])
        self.assertIn("--ipv6-state", checks[0])
        # 차례 7 관리망 격리: websvr01 은 133/24 를 ens19 에 직접 가져 66 을 지나지 않고 wassvr01 mgmtguard 에서 버려진다
        # (11-19 실기, v4 APS2). 66 S5-FWD 의 vmbr14 거절은 늘지 않는 것이 기대이고, IPv6 다시 보기는 --mode ipv6.
        isolation = next(row for row in checks_table if row[0] == "관리망 격리")
        self.assertIn("ip route get 192.168.133.21 from 192.168.131.11", isolation[2])
        self.assertIn("sudo nft list chain inet mgmtguard input", isolation[2])
        self.assertIn("hv_iptables_check.py --inventory .../s.json --mode ipv6 --ipv6-state", isolation[2])
        self.assertIn('wassvr01 `mgmtguard input` 의 `iifname "ens19" drop`', isolation[3])
        self.assertIn("`-o vmbr14 -j DROP` 은 늘지 않는다", isolation[3])
        self.assertNotIn("패킷 수가 는다", isolation[3])
        self.assertIn("`PASS ipv6`", isolation[3])
        self.assertIn("차례 3 add-net 이 그 뒤 66 mangle `TBWG-FWD` 에 줄 둘을 더해 늘 FAIL", isolation[3])
        self.assertIn("add-net 192.168.131.0/24", section)
        self.assertIn("ifup vmbr12 vmbr13 vmbr14", section)
        self.assertIn("ifdown vmbr14 vmbr13 vmbr12", section)
        self.assertIn("scale_verdict.py --inventory $I", section)
        self.assertIn("pair_packages.py", section)
        self.assertIn("st2-baseline", section)
        self.assertIn("I=deploy/inventory/s.json", section)
        # 넘기는 패키지 이름은 lldpd 단계 몫뿐이다(T 에만 도는 단계, S 문서 §2-3).
        allowed = re.findall(r"--allow-(?:t-only|version) ([^\s`]+)", section)
        self.assertEqual(sorted(allowed), sorted(["lldpd", "libsnmp40t64", "libsnmp-base", "libevent-2.1-7t64",
                                                  "libevent-core-2.1-7t64"]))
        # 흩어진 S 문장을 남기지 않는다(§14 를 가리키는 줄만).
        before = runbook[:start]
        self.assertNotIn("s5-net.rules", before)
        self.assertNotIn("--inventory .../s.json", before)

    def test_every_bridge_creates_the_chains_when_missing_and_owns_only_its_hooks(self) -> None:
        inv = inventory(T_INV)
        net = render.hypervisor_files(inv)[inv["hypervisor"]["file"]]
        stanzas = re.split(r"^auto ", net, flags=re.M)[1:]
        self.assertEqual(len(stanzas), 5)
        for stanza in stanzas:
            bridge = stanza.split()[0]
            self.assertIn(f"iptables-restore --noflush {render.rules_path(inv)}", stanza)
            hooks = re.findall(r"-I (?:FORWARD|INPUT) 1 -[io] \S+ -j (T4-FWD|T4-IN) ", stanza)
            self.assertTrue(hooks)
            # 체인과 훅이 경로, NAT 보다 먼저 서고, 실패하면 브리지를 내린다(fail closed).
            post = [line for line in stanza.splitlines() if line.startswith("\tpost-up ")]
            self.assertIn("iptables-restore", post[0])
            for line in post[:len(hooks) + 1]:
                self.assertIn(f"ip link set dev {bridge} down", line)
            for line in stanza.splitlines():
                if " -I FORWARD 1 " in line or " -D FORWARD " in line:
                    self.assertRegex(line, rf"-[io] {bridge} ")


class ProvisionPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.inv = inventory(T_INV)

    def test_create_clones_with_the_guest_name_then_rename_sets_the_proxmox_name(self) -> None:
        create = "\n".join(plan.create(self.inv, "/root/k.pub"))
        self.assertIn("qm clone 9000 202 --name web01 --full 1 --storage vmdata", create)
        self.assertNotRegex(create, r"--name t4-")
        self.assertIn("qm set 205 --scsi1 vmdata:100,discard=on,iothread=1,ssd=1,serial=pgdata", create)
        self.assertIn("--ipconfig0 ip=192.168.123.254/24,gw=192.168.123.1", create)
        self.assertIn("--ipconfig4 ip=192.168.127.2/24", create)
        self.assertNotIn("ip=192.168.127.2/24,gw", create)
        self.assertIn("--net4 virtio,bridge=vmbr11", create)
        self.assertIn("qm set 201 --cores 2 --memory 2048 --onboot 1", create)
        self.assertIn("qm set 205 --cores 4 --memory 8192 --onboot 1", create)
        rename = "\n".join(plan.rename(self.inv))
        self.assertIn("qm set 201 --name t4-rt", rename)

    def test_nics_prints_inventory_name_address_and_the_proxmox_mac_per_nic(self) -> None:
        lines = plan.nics(self.inv)
        self.assertEqual(len(lines) - 1, sum(len(h["nics"]) for h in self.inv["hosts"]))
        self.assertNotRegex("\n".join(lines), r"qm (set|start|stop|destroy|clone)")
        with tempfile.TemporaryDirectory() as raw:
            qm = Path(raw) / "qm"
            qm.write_text("#!/bin/sh\n[ \"$2\" = 201 ] || exit 0\n"
                          "printf 'name: rt-core01\\nnet0: virtio=BC:24:11:16:6F:5A,bridge=vmbr7\\n"
                          "net4: virtio=BC:24:11:94:BE:4B,bridge=vmbr11\\n'\n")
            qm.chmod(0o755)
            done = subprocess.run(["bash", "-c", "\n".join(lines)], capture_output=True, text=True,
                                  env={"PATH": f"{raw}:/usr/bin:/bin"})
        self.assertEqual(done.returncode, 0, done.stderr)
        out = done.stdout.splitlines()
        self.assertEqual(out[0], "rt-core01 ens18 192.168.123.254 bc:24:11:16:6f:5a")
        self.assertEqual(out[4], "rt-core01 ens22 192.168.127.2 bc:24:11:94:be:4b")
        self.assertEqual(out[1], "rt-core01 ens19 192.168.124.1 ")

    def test_resources_match_the_design_total(self) -> None:
        hosts = self.inv["hosts"]
        self.assertEqual(sum(h["vcpu"] for h in hosts), 14)
        self.assertEqual(sum(h["memory_mb"] for h in hosts), 22 * 1024)
        self.assertEqual(sum(d["size_gb"] for h in hosts for d in h["disks"]), 256)

    def test_destroy_only_touches_vms_whose_name_matches(self) -> None:
        lines = plan.destroy(self.inv)
        self.assertTrue(lines[0].startswith("# 사람 승인"))
        for host in self.inv["hosts"]:
            line = next(l for l in lines if f"qm config {host['vmid']}" in l)
            self.assertIn(f"\"$name\" = {host['pve_name']}", line)
            self.assertIn("건너뜀", line)

    def test_snapshot_names_the_stage_of_each_testbed(self) -> None:
        # S VM 에 "T2" 스냅숏이 남지 않게 단계 이름을 인벤토리에서 고른다(T 는 그대로 t2-baseline).
        self.assertIn("qm snapshot 201 t2-baseline --description 'T2 end, disaster recovery only'", plan.snapshot(self.inv))
        s_lines = plan.snapshot(inventory(S_INV))
        self.assertEqual(len(s_lines) - 1, 5)
        self.assertIn("qm snapshot 211 st2-baseline --description 'ST2 end, disaster recovery only'", s_lines)
        self.assertFalse(any("T2 end" in line or "t2-baseline" in line for line in s_lines if "st2" not in line))
        other = dict(self.inv, testbed="x")
        with self.assertRaises(render.InventoryError):
            plan.snapshot(other)

    def test_cli_prints_and_never_runs_qm(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(plan.main(["--inventory", str(T_INV), "--phase", "precheck"]), 0)
        self.assertIn("qm status 201", out.getvalue())
        source = (DEPLOY / "provision" / "plan.py").read_text()
        self.assertNotIn("subprocess", source)


# NIC 이름(T2NIC): cloud-init 의 eth 이름을 netplan 의 MAC 맞춤과 set-name 으로 인벤토리 이름으로 바꾼다.
NETPLAN_STUB = r'''#!/usr/bin/env python3
"""netplan 대역. generate 는 YAML 을 읽어 보기만, apply 는 진짜 netplan 1.1 처럼 match.macaddress 와 set-name 이
있는 NIC 를 내리고 이름을 바꾸고 올린다(진짜 커널, 네임스페이스 안)."""
import os, pathlib, subprocess, sys
import yaml
with open(os.environ["STUB_LOG"], "a") as log:
    log.write("netplan " + " ".join(sys.argv[1:]) + "\n")
folder = pathlib.Path(os.environ["NETPLAN_DIR"])
docs = [yaml.safe_load(p.read_text()) for p in sorted(folder.glob("*.yaml"))]
if sys.argv[1] == "generate":
    sys.exit(int(os.environ.get("STUB_GENERATE_RC", "0")))
pairs = []
for doc in docs:
    for eth in (doc["network"].get("ethernets") or {}).values():
        mac = (eth.get("match") or {}).get("macaddress")
        if mac and eth.get("set-name"):
            pairs.append((mac.lower(), eth["set-name"]))
sysnet = pathlib.Path("/sys/class/net")
for mac, new in pairs:
    current = [p.name for p in sysnet.iterdir() if (p / "address").read_text().strip() == mac]
    if len(current) != 1 or current[0] == new:
        continue
    for args in (["down"], ["name", new]):
        subprocess.run(["ip", "link", "set", "dev", current[0], *args], check=True)
    subprocess.run(["ip", "link", "set", "dev", new, "up"], check=True)
'''

SYSTEMD_RUN_STUB = r'''#!/usr/bin/env python3
"""systemd-run 대역. --on-active 는 걸린 명령을 STUB_ARMED 에 적기만 하고, 나머지는 명령을 바로 돌린다."""
import os, pathlib, shlex, subprocess, sys
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write("systemd-run " + " ".join(args) + "\n")
command, timer, index = [], False, 0
while index < len(args):
    if args[index] == "-p":
        index += 2
        continue
    if args[index].startswith("--on-active"):
        timer = True
    if args[index].startswith("-"):
        index += 1
        continue
    command = args[index:]
    break
if timer:
    pathlib.Path(os.environ["STUB_ARMED"]).write_text(shlex.join(command))
    sys.exit(0)
rc = subprocess.run(command).returncode
if command[-2:] == ["netplan", "apply"] and os.environ.get("STUB_FIRE_DURING_APPLY") == "1":
    # 적용이 오래 걸려 그 사이 되돌림 타이머가 발화해 끝난 경우(발화한 일시 타이머는 기다림이 아니다).
    pathlib.Path(os.environ["STUB_ARMED"]).unlink(missing_ok=True)
sys.exit(rc)
'''

SYSTEMCTL_STUB = r'''#!/usr/bin/env python3
"""systemctl 대역. 되돌림 타이머를 끄면 STUB_ARMED 를 지우고, is-active 는 그 파일이 있는지로 답한다."""
import os, pathlib, sys
args = sys.argv[1:]
armed = pathlib.Path(os.environ["STUB_ARMED"])
with open(os.environ["STUB_LOG"], "a") as log:
    log.write("systemctl " + " ".join(args) + "\n")
if args and args[0] == "stop" and "netplan-rollback.timer" in args:
    armed.unlink(missing_ok=True)
if args and args[0] == "is-active":
    sys.exit(0 if armed.exists() else 3)
if args[:1] == ["show"] and args[-1] == "netplan-rollback.timer":
    print("waiting" if armed.exists() else "dead")
if args[:1] == ["show"] and args[-1] == "netplan-rollback.service":
    state = pathlib.Path(os.environ["STUB_ARMED"] + ".service")
    print(state.read_text().strip() if state.exists() else "inactive")
'''

RSYSLOGD_STUB = r'''#!/usr/bin/env python3
"""rsyslogd -N1 [-d] -f <설정> 대역. Ubuntu rsyslogd AppArmor 프로필의 설정 읽기 범위(RSYSLOG_ETC 의 rsyslog.conf 와
rsyslog.d/**)를 흉내 낸다: 그 밖의 -f 는 Permission denied 로 실패하고, 그 밖의 포함 대상은 mode="optional" 이면 조용히
건너뛰고(진짜처럼 오류 없음) 아니면 실패한다. $IncludeConfig 와 include(file=...) 를 따라 읽은 파일을 STUB_LOG 에 적고,
-d 이면 진짜 디버그처럼 읽은 포함 파일마다 "pushed config fragment on top of stack: <파일>" 을 낸다. 읽은 파일 가운데
BAD 가 든 것이 있거나 설정 경로가 FAKE_FAIL_CONF 이면 실패한다. 실패할 때 FAKE_LOCK_DIR 을 읽기 전용으로 바꾼다."""
import glob, os, re, sys
args = [a for a in sys.argv[1:] if a != "-d"]
debug = "-d" in sys.argv[1:]
assert args[:2] == ["-N1", "-f"] and len(args) == 3, sys.argv
conf = args[2]
etc = os.environ["RSYSLOG_ETC"]
log = open(os.environ["STUB_LOG"], "a")
def allowed(path):
    return path == f"{etc}/rsyslog.conf" or path.startswith(f"{etc}/rsyslog.d/")
def die(message):
    log.close()
    if os.environ.get("FAKE_LOCK_DIR"):
        os.chmod(os.environ["FAKE_LOCK_DIR"], 0o555)
    print(f"rsyslogd: {message}", file=sys.stderr)
    sys.exit(1)
if not allowed(conf):
    log.write(f"rsyslogd -f {conf} denied\n")
    die(f"could not open config file '{conf}': Permission denied")
seen, denied = [], []
def walk(path):
    seen.append(path)
    text = open(path, encoding="utf-8").read()
    for legacy, pattern, rest in re.findall(r'^\$IncludeConfig (\S+)|include\(file="([^"]+)"([^)]*)\)', text, re.M):
        pattern = legacy or pattern
        if not allowed(pattern):
            if 'mode="optional"' in rest:
                denied.append(pattern)
                continue
            die(f"could not open config file '{pattern}': Permission denied")
        for match in sorted(glob.glob(pattern)):
            if debug:
                print(f"0.0:main thread    : lexer.l: config parser: pushed config fragment on top of stack: {match}")
            walk(match)
walk(conf)
log.write("rsyslogd -f " + conf + " read " + " ".join(seen) + "".join(f" denied {d}" for d in denied) + "\n")
bad = [path for path in seen if "BAD" in open(path, encoding="utf-8").read()]
if bad:
    die(f"error during parsing file {bad[0]}")
if conf == os.environ.get("FAKE_FAIL_CONF"):
    die("error after placing")
log.close()
print("rsyslogd: End of config validation run. Bye.", file=sys.stderr)
'''

CLOUD_INIT_NETPLAN = textwrap.dedent('''\
    network:
      version: 2
      ethernets:
        eth0:
          match:
            macaddress: "bc:24:11:00:00:10"
          addresses:
          - "10.77.1.2/24"
          set-name: "eth0"
        eth1:
          match:
            macaddress: "bc:24:11:00:00:11"
          addresses:
          - "10.77.2.2/24"
          set-name: "eth1"
    ''')

NETWORK_KERNEL_SCRIPT = r'''
set -u
work=$1 mode=$2
mount -t sysfs sysfs /sys
ip link set lo up
ip link add eth0 type dummy
ip link set eth0 address bc:24:11:00:00:10
ip addr add 10.77.1.2/24 dev eth0
ip link set eth0 up
ip link add eth1 type veth peer name hv1
ip link set eth1 address bc:24:11:00:00:11
ip addr add 10.77.2.2/24 dev eth1
ip link set eth1 up
unshare -n sleep 300 &
peer=$!
sleep 0.3
ip link set hv1 netns "$peer"
if [ "$mode" != silent ]; then
    nsenter -t "$peer" -n --preserve-credentials ip addr add 10.77.2.1/24 dev hv1
    nsenter -t "$peer" -n --preserve-credentials ip link set hv1 up
fi
sleep 0.3
export PATH="$work/bin:$PATH" STUB_LOG="$work/calls" STUB_ARMED="$work/armed" NETPLAN_DIR="$work/etc/netplan" \
    NETPLAN_BACKUP="$work/state/netplan-before" STATE_DIR="$work/state" CLOUD_DIR="$work/etc/cloud" \
    NETPLAN_ROLLBACK="$work/sbin/netplan-rollback" NIC_MAC_FILE="$work/state/nic-macs" NET_SETTLE_SEC=4 \
    HOST_NAME=$(hostname)
step() {
    bash -c 'set -euo pipefail; BUNDLE=$1; . "$BUNDLE/host.env"; . "$2/lib.sh"; . "$3"' _ "$work/bundle" "$work/steps" "$1" \
        >>"$work/out" 2>&1
    echo "$?" >> "$work/rc"
}
links() { ip -br link | awk '$1 != "lo" && $1 !~ /^hv/ {sub(/@.*/, "", $1); print $1}' | sort | tr '\n' ' ' >> "$work/links"; echo >> "$work/links"; }
links
step "$work/steps/verify-host.sh"
case "$mode" in
    ok|silent)
        step "$work/steps/network.sh"; links
        [ "$mode" = ok ] && { step "$work/steps/network.sh"; links; } ;;
    interrupted)
        # 적용까지만 돌고 세션이 끊긴 경우: 확인과 타이머 끄기에 닿지 못한다. 그 뒤 타이머가 걸린 명령을 돌린다.
        sed '/^# 5\. 확인/,$d' "$work/steps/network.sh" > "$work/network-cut.sh"
        step "$work/network-cut.sh"; links
        bash -c "$(cat "$work/armed")" >>"$work/out" 2>&1; echo "$?" >> "$work/rc"; links ;;
    macchange)
        printf 'ens18 bc:24:11:00:00:99\nens19 bc:24:11:00:00:11\n' > "$work/state/nic-macs"
        step "$work/steps/network.sh"; links ;;
    rerun_pending)
        # 적용 뒤 세션이 끊겨 타이머가 기다리는 중에 다시 돌린 경우. 다시 돌림은 아무것도 바꾸지 않아야 한다.
        sed '/^# 5\. 확인/,$d' "$work/steps/network.sh" > "$work/network-cut.sh"
        step "$work/network-cut.sh"; links
        grep -c '^systemd-run' "$work/calls" > "$work/runs-before"
        step "$work/steps/network.sh"; links ;;
    fired)
        STUB_FIRE_DURING_APPLY=1 step "$work/steps/network.sh"; links ;;
esac
kill "$peer"
'''


class NicNamingRenderTests(unittest.TestCase):
    """netplan 이 NIC 를 MAC 으로 맞추고 인벤토리 이름을 준다(66 에서 eth0~ 로 뜬 VM, APT2 §3 멈춤)."""

    def test_netplan_matches_every_nic_by_mac_and_sets_the_inventory_name(self) -> None:
        for path in (T_INV, S_INV):
            inv = inventory(path)
            for host in inv["hosts"]:
                netplan = file_of(bundles(path)[host["name"]], "/etc/netplan/01-host.yaml")
                names = [nic["ifname"] for nic in host["nics"]]
                with self.subTest(inventory=path.name, host=host["name"]):
                    for name in names:
                        self.assertIn(f"    {name}:\n      match:\n        macaddress: \"@@MAC:{name}@@\"\n"
                                      f"      set-name: {name}\n", netplan)
                    self.assertEqual(sorted(render.MAC_SLOT.findall(netplan)), sorted(names))
                    self.assertEqual(netplan.count("set-name:"), len(names))
                    self.assertEqual(netplan.count("match:"), len(names))

    def test_filled_netplan_makes_mac_bound_names_with_the_real_generator(self) -> None:
        inv = inventory(T_INV)
        router = next(h for h in inv["hosts"] if h["role"] == "router")
        text = file_of(bundles(T_INV)[router["name"]], "/etc/netplan/01-host.yaml")
        macs = {nic["ifname"]: f"bc:24:11:00:01:{index:02x}" for index, nic in enumerate(router["nics"])}
        filled = render.MAC_SLOT.sub(lambda m: macs[m.group(1)], text)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "etc" / "netplan").mkdir(parents=True)
            target = root / "etc" / "netplan" / "01-host.yaml"
            target.write_text(filled)
            target.chmod(0o600)
            done = subprocess.run(["netplan", "generate", "--root-dir", raw], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            for nic in router["nics"]:
                name = nic["ifname"]
                with self.subTest(nic=name):
                    link = (root / "run" / "systemd" / "network" / f"10-netplan-{name}.link").read_text()
                    self.assertIn(f"PermanentMACAddress={macs[name]}", link)
                    self.assertIn(f"Name={name}", link)
                    network = (root / "run" / "systemd" / "network" / f"10-netplan-{name}.network").read_text()
                    self.assertIn(f"Address={nic['addr']}/24", network)
                    self.assertIn(f"Name={name}", network)

    def test_rejects_an_interface_name_the_kernel_would_not_take(self) -> None:
        for bad in ("Ens18", "ens18-management0", "18ens"):
            inv = inventory(T_INV)
            inv["hosts"][0]["nics"][0]["ifname"] = bad
            with self.subTest(name=bad), self.assertRaisesRegex(render.InventoryError, "커널 이름 꼴"):
                render.validate(inv)

    def test_render_stops_on_a_mac_slot_for_a_nic_the_host_does_not_have(self) -> None:
        inv = inventory(T_INV)
        host = inv["hosts"][1]
        with unittest.mock.patch.object(render, "_netplan", return_value='    ens99:\n      match:\n        macaddress: "@@MAC:ens99@@"'):
            with self.assertRaisesRegex(render.InventoryError, "없는 NIC 의 MAC 자리 ens99"):
                render.render_host(inv, host)

    def test_cloud_init_network_is_off_before_the_network_step_and_the_step_checks_it(self) -> None:
        order = render.STEP_ORDER
        self.assertLess(order.index("os-base"), order.index("network"))
        cfg = (DEPLOY / "templates" / "common" / "cloud-99-disable-network.cfg").read_text()
        self.assertIn("network: {config: disabled}", cfg)
        self.assertIn("touch /etc/cloud/cloud-init.disabled", (DEPLOY / "steps" / "os-base.sh").read_text())
        network = (DEPLOY / "steps" / "network.sh").read_text()
        self.assertIn("config: disabled", network)
        self.assertIn("cloud-init.disabled", network)
        bundle = bundles(T_INV)["web01"]
        steps = {entry["dest"]: entry["step"] for entry in bundle["files"]}
        self.assertEqual(steps["/etc/cloud/cloud.cfg.d/99-disable-network-config.cfg"], "os-base")
        self.assertEqual(steps["/usr/local/sbin/netplan-rollback"], "network")

    def test_rollback_script_and_network_step_use_the_same_backup_folder(self) -> None:
        rollback = (DEPLOY / "templates" / "common" / "netplan-rollback").read_text()
        network = (DEPLOY / "steps" / "network.sh").read_text()
        backup = re.search(r"backup=\$\{NETPLAN_BACKUP:-(\S+)\}", rollback).group(1)
        state = re.search(r"state=\$\{STATE_DIR:-(\S+)\}", network).group(1)
        self.assertEqual(backup, f"{state}/netplan-before")
        self.assertIn('backup="$state/netplan-before"', network)
        lib = (DEPLOY / "steps" / "lib.sh").read_text()
        self.assertEqual(re.search(r"NIC_MAC_FILE=\$\{NIC_MAC_FILE:-(\S+)\}", lib).group(1), f"{state}/nic-macs")
        self.assertIn('rollback_bin=${NETPLAN_ROLLBACK:-/usr/local/sbin/netplan-rollback}', network)

    def test_network_step_arms_the_rollback_before_it_changes_anything_and_applies_outside_the_session(self) -> None:
        source = (DEPLOY / "steps" / "network.sh").read_text()
        body = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        arm = body.index("--on-active=")
        self.assertLess(body.index('cp -p "${current[@]}" "$backup"/'), arm)
        self.assertLess(arm, body.index("place_files network ||"))
        self.assertLess(arm, body.index('rm -f "$netplan_dir/50-cloud-init.yaml"'))
        self.assertLess(body.index("netplan generate"), body.index("netplan apply"))
        self.assertRegex(body, r"systemd-run --quiet --wait --collect --unit=netplan-switch [^\n]*netplan apply")
        confirm = body.index('[[ "$state" == pending ]]')
        self.assertLess(body.index("nic_named_problem"), confirm)
        self.assertLess(confirm, body.rindex("systemctl stop netplan-rollback.timer"))
        self.assertLess(body.index("case \"$(rollback_state)\" in"), body.index('rm -rf "$backup"'))
        self.assertLess(body.index("case \"$(rollback_state)\" in"), body.index("nic_resolve"))

    def test_leftover_physical_nic_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            sysnet = Path(raw)
            for name, physical in (("ens18", True), ("ens19", True), ("eth2", True), ("lo", False), ("dummy0", False)):
                (sysnet / name).mkdir()
                if physical:
                    (sysnet / name / "device").mkdir()
            script = f". {DEPLOY / 'steps' / 'lib.sh'}; nic_leftover"
            done = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                  env={"PATH": "/usr/bin:/bin", "SYS_NET": raw,
                                       "NIC_EXPECT": "ens18=10.77.1.2 ens19=10.77.2.2"})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.split(), ["eth2"])


class NetworkStepKernelTests(unittest.TestCase):
    """설치 단계 verify-host, network 를 사용자 네임스페이스의 진짜 커널 NIC 로 돌린다.

    cloud-init 처럼 eth0(업무), eth1(관리, 66 관리 주소 역할의 짝 네임스페이스와 veth)로 뜬 VM 을 만들고, netplan,
    systemd-run, systemctl 만 대역으로 바꾼다(netplan apply 대역은 진짜 netplan 1.1 의 이름 바꾸기와 같이 내리고
    바꾸고 올린다). ip, ping, ARP 는 진짜다.
    """

    def _run(self, mode: str) -> dict:
        with tempfile.TemporaryDirectory() as raw:
            work = Path(raw)
            for folder in ("bin", "etc/netplan", "etc/cloud/cloud.cfg.d", "state", "sbin", "steps",
                           "bundle/files/netplan", "bundle/files/sbin"):
                (work / folder).mkdir(parents=True)
            for name, text in (("netplan", NETPLAN_STUB), ("systemd-run", SYSTEMD_RUN_STUB),
                               ("systemctl", SYSTEMCTL_STUB), ("sysctl", "#!/bin/sh\nexit 0\n")):
                (work / "bin" / name).write_text(text)
                (work / "bin" / name).chmod(0o755)
            for name in ("lib.sh", "verify-host.sh", "network.sh"):
                (work / "steps" / name).write_text((DEPLOY / "steps" / name).read_text())
            (work / "etc" / "netplan" / "50-cloud-init.yaml").write_text(CLOUD_INIT_NETPLAN)
            (work / "etc" / "cloud" / "cloud.cfg.d" / "99-disable-network-config.cfg").write_text(
                (DEPLOY / "templates" / "common" / "cloud-99-disable-network.cfg").read_text())
            (work / "etc" / "cloud" / "cloud-init.disabled").write_text("")
            mini = {"dns": ["192.0.2.53"], "networks": {
                "biz": {"cidr": "10.77.1.0/24", "gateway": "10.77.1.1"},
                "mgmt": {"cidr": "10.77.2.0/24", "management": True}}}
            host = {"nics": [{"net": "biz", "addr": "10.77.1.2", "ifname": "ens18", "gateway": True},
                             {"net": "mgmt", "addr": "10.77.2.2", "ifname": "ens19"}]}
            header = (DEPLOY / "templates" / "common" / "netplan.yaml").read_text()
            netplan_text = render.render_text(header, {"netplan_ethernets": render._netplan(host, mini)}, "netplan")
            (work / "bundle" / "files" / "netplan" / "01-host.yaml").write_text(netplan_text)
            (work / "bundle" / "files" / "sbin" / "netplan-rollback").write_text(
                (DEPLOY / "templates" / "common" / "netplan-rollback").read_text())
            (work / "bundle" / "files.tsv").write_text(
                f"network\t{work}/etc/netplan/01-host.yaml\t0600\troot\troot\tfiles/netplan/01-host.yaml\n"
                f"network\t{work}/sbin/netplan-rollback\t0755\troot\troot\tfiles/sbin/netplan-rollback\n")
            (work / "bundle" / "host.env").write_text(
                "NIC_EXPECT='ens18=10.77.1.2 ens19=10.77.2.2'\nMGMT_IF='ens19'\nMGMT_HOST='10.77.2.1'\n")
            done = subprocess.run(["unshare", "-rnm", "bash", "-c", NETWORK_KERNEL_SCRIPT, "_", raw, mode],
                                  capture_output=True, text=True, timeout=120)
            self.assertEqual(done.returncode, 0, done.stderr)
            read = lambda name: (work / name).read_text() if (work / name).exists() else ""
            return {
                "rc": [int(x) for x in read("rc").split()],
                "links": [line.split() for line in read("links").splitlines()],
                "out": read("out"),
                "calls": read("calls").splitlines(),
                "netplan": sorted(p.name for p in (work / "etc" / "netplan").glob("*.yaml")),
                "netplan_text": {p.name: p.read_text() for p in (work / "etc" / "netplan").glob("*.yaml")},
                "macs": read("state/nic-macs"),
                "armed": (work / "armed").exists(),
                "original": sorted(p.name for p in (work / "state" / "netplan-cloud-init").glob("*.yaml")),
                "backup": {p.name: p.read_text() for p in (work / "state" / "netplan-before").glob("*.yaml")},
                "runs_before": int(read("runs-before") or 0),
            }

    def test_eth_names_become_inventory_names_and_a_second_run_changes_nothing(self) -> None:
        result = self._run("ok")
        self.assertEqual(result["rc"], [0, 0, 0], result["out"])
        self.assertEqual(result["links"], [["eth0", "eth1"], ["ens18", "ens19"], ["ens18", "ens19"]])
        self.assertIn("nic ens18 = eth0 bc:24:11:00:00:10", result["out"])
        self.assertIn("network ok: eth0->ens18 eth1->ens19", result["out"])
        self.assertEqual(result["macs"], "ens18 bc:24:11:00:00:10\nens19 bc:24:11:00:00:11\n")
        self.assertEqual(result["netplan"], ["01-host.yaml"])
        self.assertIn('macaddress: "bc:24:11:00:00:11"\n      set-name: ens19', result["netplan_text"]["01-host.yaml"])
        self.assertEqual(result["original"], ["50-cloud-init.yaml"])
        self.assertFalse(result["armed"], "되돌림 타이머가 남음")
        calls = result["calls"]
        arm = next(i for i, c in enumerate(calls) if "--on-active=300s" in c)
        apply = next(i for i, c in enumerate(calls) if c.startswith("systemd-run") and "netplan apply" in c)
        stop = next(i for i, c in enumerate(calls) if i > apply and c == "systemctl stop netplan-rollback.timer")
        self.assertLess(calls.index("netplan generate"), apply)
        self.assertLess(arm, calls.index("netplan generate"))
        self.assertLess(apply, stop)
        self.assertIn("--unit=netplan-switch", calls[apply])
        # 두 번째 실행은 이름을 바꾸지 않는다(바꾼 이름 목록 없이 지금 이름만).
        self.assertEqual(re.findall(r"network ok: (.*)", result["out"])[-1].split(), ["ens18", "ens19"])

    def test_no_arp_answer_on_the_management_nic_rolls_back_to_the_cloud_init_names(self) -> None:
        result = self._run("silent")
        self.assertEqual(result["rc"][0], 0, result["out"])
        self.assertNotEqual(result["rc"][1], 0)
        self.assertIn("netplan 적용 뒤 확인 실패(4초): 관리 NIC ens19 로 10.77.2.1 가 ARP 에 답하지 않음. 사본으로 되돌림",
                      result["out"])
        self.assertIn("사본으로 되돌림", result["out"])
        self.assertEqual(result["links"][-1], ["eth0", "eth1"])
        self.assertEqual(result["netplan"], ["50-cloud-init.yaml"])
        self.assertFalse(result["armed"])
        self.assertTrue(any("--unit=netplan-rollback-now" in c for c in result["calls"]))

    def test_a_lost_session_after_apply_is_undone_by_the_armed_timer(self) -> None:
        result = self._run("interrupted")
        self.assertEqual(result["rc"], [0, 0, 0], result["out"])
        self.assertEqual(result["links"][1], ["ens18", "ens19"])
        self.assertEqual(result["links"][2], ["eth0", "eth1"])
        self.assertEqual(result["netplan"], ["50-cloud-init.yaml"])
        self.assertIn("netplan-rollback: 1 개 파일로 되돌림", result["out"])

    def test_a_rerun_while_the_rollback_waits_changes_nothing(self) -> None:
        result = self._run("rerun_pending")
        self.assertEqual(result["rc"][:2], [0, 0], result["out"])
        self.assertNotEqual(result["rc"][2], 0)
        self.assertIn("앞 망 전환의 되돌림 타이머가 기다리는 중", result["out"])
        runs = [c for c in result["calls"] if c.startswith("systemd-run")]
        self.assertEqual(len(runs), result["runs_before"], "다시 돌림이 systemd-run 을 불렀다")
        self.assertEqual(list(result["backup"]), ["50-cloud-init.yaml"], "사본이 지금 상태로 덮였다")
        self.assertEqual(result["backup"]["50-cloud-init.yaml"], CLOUD_INIT_NETPLAN)
        self.assertTrue(result["armed"], "기다리던 되돌림 타이머가 꺼졌다")
        self.assertNotIn("systemctl stop netplan-rollback.timer", result["calls"][-6:])

    def test_a_rollback_that_fired_during_apply_is_not_reported_as_ok(self) -> None:
        result = self._run("fired")
        self.assertNotEqual(result["rc"][1], 0)
        self.assertIn("확정 전에 되돌림이 발화함(clear)", result["out"])
        self.assertNotIn("network ok", result["out"])
        apply = next(c for c in result["calls"] if c.startswith("systemd-run") and "netplan apply" in c)
        self.assertIn("-p RuntimeMaxSec=120", apply)

    def test_a_changed_mac_stops_before_anything_is_touched(self) -> None:
        result = self._run("macchange")
        self.assertNotEqual(result["rc"][1], 0)
        self.assertIn("ens18 의 MAC 이 bc:24:11:00:00:99 에서 bc:24:11:00:00:10 로 바뀜", result["out"])
        self.assertEqual(result["links"][-1], ["eth0", "eth1"])
        self.assertEqual(result["netplan"], ["50-cloud-init.yaml"])
        self.assertFalse(any(c.startswith("systemd-run") for c in result["calls"]))


def _qdisc(dev, kind, handle, drops, parent=None):
    entry = {"dev": dev, "kind": kind, "handle": handle, "drops": drops}
    if parent is None:
        entry["root"] = True
    else:
        entry["parent"] = parent
    return entry


class IfDiscardsTests(unittest.TestCase):
    def test_netem_and_its_ancestors_are_not_counted(self) -> None:
        qdiscs = [
            _qdisc("ens19", "prio", "1:", 150),
            _qdisc("ens19", "netem", "10:", 100, "1:1"),
            _qdisc("ens19", "tbf", "20:", 50, "1:2"),
        ]
        shares = ifdiscards.qdisc_shares(qdiscs, "ens19")
        self.assertEqual(shares[("1:", "prio", "root")], (0, False))
        self.assertEqual(shares[("10:", "netem", "1:1")], (100, False))
        self.assertEqual(shares[("20:", "tbf", "1:2")], (50, True))
        acc = ifdiscards.Accumulator(None)
        self.assertEqual(acc.update("ens19", shares), 50)

    def test_parent_counts_only_its_own_share(self) -> None:
        qdiscs = [_qdisc("ens20", "tbf", "1:", 30), _qdisc("ens20", "bfifo", "2:", 30, "1:1")]
        shares = ifdiscards.qdisc_shares(qdiscs, "ens20")
        self.assertEqual(ifdiscards.Accumulator(None).update("ens20", shares), 30)

    def test_counter_never_drops_when_qdiscs_go_away_or_restart(self) -> None:
        acc = ifdiscards.Accumulator(None)
        dev = "ens21"
        self.assertEqual(acc.update(dev, ifdiscards.qdisc_shares([_qdisc(dev, "tbf", "8001:", 40)], dev)), 40)
        self.assertEqual(acc.update(dev, ifdiscards.qdisc_shares([], dev)), 40)
        self.assertEqual(acc.update(dev, ifdiscards.qdisc_shares([_qdisc(dev, "tbf", "8001:", 5)], dev)), 45)
        self.assertEqual(acc.update(dev, ifdiscards.qdisc_shares([_qdisc(dev, "tbf", "8001:", 2)], dev)), 47)

    def test_a_queue_that_becomes_a_netem_ancestor_keeps_what_it_had_counted(self) -> None:
        acc = ifdiscards.Accumulator(None)
        dev = "ens19"
        self.assertEqual(acc.update(dev, ifdiscards.qdisc_shares([_qdisc(dev, "tbf", "1:", 20)], dev)), 20)
        tree = [_qdisc(dev, "tbf", "1:", 120), _qdisc(dev, "netem", "10:", 100, "1:1")]
        self.assertEqual(acc.update(dev, ifdiscards.qdisc_shares(tree, dev)), 20)
        # netem 이 빠지고 tbf 만 남으면 그 시점부터 는 것만 센다(netem 이 올린 몫을 세지 않는다).
        self.assertEqual(acc.update(dev, ifdiscards.qdisc_shares([_qdisc(dev, "tbf", "1:", 120)], dev)), 20)
        self.assertEqual(acc.update(dev, ifdiscards.qdisc_shares([_qdisc(dev, "tbf", "1:", 125)], dev)), 25)

    def test_state_survives_a_helper_restart(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw) / "state.json"
            acc = ifdiscards.Accumulator(state)
            acc.update("ens19", ifdiscards.qdisc_shares([_qdisc("ens19", "tbf", "1:", 9)], "ens19"))
            acc.save()
            again = ifdiscards.Accumulator(state)
            self.assertEqual(again.update("ens19", ifdiscards.qdisc_shares([], "ens19")), 9)

    def test_table_adds_queue_drops_only_to_augmented_links_and_wraps(self) -> None:
        links = [
            {"ifindex": 2, "ifname": "ens18", "stats64": {"tx": {"dropped": 3}}},
            {"ifindex": 3, "ifname": "ens19", "stats64": {"tx": {"dropped": ifdiscards.COUNTER32 - 1}}},
        ]
        qdiscs = [_qdisc("ens18", "tbf", "1:", 50), _qdisc("ens19", "tbf", "1:", 5)]
        table = ifdiscards.Table(["ens19"], ifdiscards.Accumulator(None), links=lambda: links, qdiscs=lambda: qdiscs)
        rows = table.refresh(now=0)
        self.assertEqual(rows[2], 3)
        self.assertEqual(rows[3], 4)

    def test_pass_persist_protocol(self) -> None:
        links = [{"ifindex": 2, "ifname": "ens18", "stats64": {"tx": {"dropped": 1}}},
                 {"ifindex": 4, "ifname": "ens19", "stats64": {"tx": {"dropped": 0}}}]
        table = ifdiscards.Table(["ens19"], ifdiscards.Accumulator(None), links=lambda: links,
                                 qdiscs=lambda: [_qdisc("ens19", "tbf", "1:", 7)])
        base = ifdiscards.BASE_OID
        script = "\n".join(["PING", "get", f"{base}.4", "get", f"{base}.9", "getnext", base, "getnext", f"{base}.2",
                            "getnext", f"{base}.4", "set", f"{base}.4", "counter 1", ""]) + "\n"
        out = io.StringIO()
        ifdiscards.serve(table, io.StringIO(script), out)
        self.assertEqual(out.getvalue().splitlines(), [
            "PONG",
            f"{base}.4", "counter", "7",
            "NONE",
            f"{base}.2", "counter", "1",
            f"{base}.4", "counter", "7",
            "NONE",
            "not-writable",
        ])

    def test_a_failing_tc_answers_none_instead_of_hanging(self) -> None:
        def broken():
            raise RuntimeError("tc failed")
        table = ifdiscards.Table(["ens19"], ifdiscards.Accumulator(None),
                                 links=lambda: [{"ifindex": 2, "ifname": "ens19", "stats64": {"tx": {"dropped": 0}}}],
                                 qdiscs=broken)
        out = io.StringIO()
        ifdiscards.serve(table, io.StringIO(f"get\n{ifdiscards.BASE_OID}.2\nPING\n"), out)
        self.assertEqual(out.getvalue().splitlines(), ["NONE", "PONG"])


KERNEL_SCRIPT = r'''
import json, socket, subprocess, sys, time
helper, workdir = sys.argv[1], sys.argv[2]
def sh(cmd):
    subprocess.run(cmd, shell=True, check=True)
def drops(dev):
    return {q["kind"]: q["drops"] for q in json.loads(subprocess.run(["tc", "-s", "-j", "qdisc", "show", "dev", dev], capture_output=True, text=True, check=True).stdout)}
def dump(state):
    cfg = f"{workdir}/cfg.json"
    open(cfg, "w").write(json.dumps({"augment": ["dq0", "dq1"], "state": state}))
    out = subprocess.run([sys.executable, helper, "--config", cfg, "--dump"], capture_output=True, text=True, check=True).stdout
    index = {l["ifname"]: l["ifindex"] for l in json.loads(subprocess.run(["ip", "-j", "link", "show"], capture_output=True, text=True, check=True).stdout)}
    values = dict(line.split() for line in out.splitlines())
    return {name: int(values[f".1.3.6.1.2.1.2.2.1.19.{i}"]) for name, i in index.items() if name in ("dq0", "dq1")}
def blast(dst, prio, count):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_PRIORITY, prio)
    for _ in range(count):
        try:
            s.sendto(b"x" * 1000, (dst, 9))
        except OSError:
            pass
    s.close()
sh("ip link set lo up")
for n, net in (("dq0", "10.9.0"), ("dq1", "10.9.1")):
    sh(f"ip link add {n} type dummy && ip link set {n} up && ip addr add {net}.1/24 dev {n}")
# dq0: 속도 제한 큐 하나가 넘친다.
sh("tc qdisc add dev dq0 root tbf rate 100kbit burst 1600 limit 3000")
# dq1: prio 아래 netem(무작위 손실 100%)과 속도 제한 큐가 함께 있다.
sh("tc qdisc add dev dq1 root handle 1: prio")
sh("tc qdisc add dev dq1 parent 1:1 handle 10: netem loss 100%")
sh("tc qdisc add dev dq1 parent 1:2 handle 20: tbf rate 100kbit burst 1600 limit 3000")
state = f"{workdir}/state.json"
blast("10.9.0.2", 0, 300)
blast("10.9.1.2", 6, 200)   # 우선순위 6 -> priomap 0 -> 1:1 (netem)
blast("10.9.1.2", 0, 300)   # 우선순위 0 -> priomap 1 -> 1:2 (tbf)
time.sleep(0.2)
first = dump(state)
d0, d1 = drops("dq0"), drops("dq1")
sh("tc qdisc del dev dq0 root")
after_delete = dump(state)
print(json.dumps({"dq0": d0, "dq1": d1, "first": first, "after_delete": after_delete}))
'''


class IfDiscardsKernelTests(unittest.TestCase):
    """진짜 커널의 qdisc 로 결정 4 의 세 가지를 본다(설계 §10 T2 큐 버림 보고 (1)~(3)).

    (4) 도우미를 죽였을 때 snmpd 가 응답을 멈추지 않는지는 snmpd 가 있는 라우터에서 runbook 절차로 본다.
    """

    def test_overflow_counts_netem_loss_does_not_and_deletion_keeps_the_value(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            done = subprocess.run(
                ["unshare", "-rn", sys.executable, "-c", KERNEL_SCRIPT, str(DEPLOY / "router" / "ifdiscards.py"), raw],
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            result = json.loads(done.stdout.strip().splitlines()[-1])
        tbf0 = result["dq0"]["tbf"]
        self.assertGreater(tbf0, 100, "속도 제한 큐가 넘치지 않음")
        # (1) 넘침 버림 N 개만큼 는다(커널 송신 버림은 0 이라 그대로 같다).
        self.assertEqual(result["first"]["dq0"], tbf0)
        # (2) netem 무작위 손실은 더하지 않는다. 부모 prio 는 자식 netem 의 손실을 자기 drops 에도 센다.
        self.assertGreater(result["dq1"]["netem"], 100)
        self.assertEqual(result["dq1"]["prio"], result["dq1"]["netem"] + result["dq1"]["tbf"])
        self.assertEqual(result["first"]["dq1"], result["dq1"]["tbf"])
        # (3) qdisc 를 지운 뒤에도 값이 줄지 않는다.
        self.assertEqual(result["after_delete"]["dq0"], tbf0)


# 라우터 네임스페이스(unshare -rn 의 셸)에 업무 상향 NIC ens18(브리지, 손님과 66 자리가 같은 L2)과 하향 NIC ens19 ~ ens22
# (dummy)를 두고, 손님이 라우터를 거쳐 같은 ens18 쪽 다음 홉으로 가는 패킷을 보낸다(T3 차례 4 에서 하향 링크가 내려가
# 기본 경로로 되돌아 나가던 꼴). NIC 값은 1, all, default 는 0(부팅 뒤 T 라우터의 값과 같은 꼴).
REDIRECT_KERNEL_SCRIPT = r"""
set -uo pipefail
lib="$1"; defs="$2"; conf="$3"
ip link set lo up
mk() { unshare -n sleep 60 >/dev/null 2>&1 & echo $!; }
c=$(mk); g=$(mk); sleep 0.3
ip link add ens18 type bridge && ip link set ens18 up && ip addr add 10.9.0.254/24 dev ens18
for n in ens19 ens20 ens21 ens22; do ip link add $n type dummy; ip link set $n up; done
for n in c g; do
    pid=${!n}; ip link add p$n type veth peer name i$n
    ip link set p$n master ens18 up; ip link set i$n netns $pid
done
nsenter -t $c -n sh -c 'ip link set lo up; ip addr add 10.9.0.10/24 dev ic; ip link set ic up; ip route add default via 10.9.0.254'
nsenter -t $g -n sh -c 'ip link set lo up; ip addr add 10.9.0.1/24 dev ig; ip link set ig up'
echo 1 > /proc/sys/net/ipv4/ip_forward
# 부팅 뒤 T 라우터에서 읽은 값(11-19 15:15 KST): NIC 마다 1. dummy 는 만들 때 값이 커널마다 달라 직접 1 로 둔다.
for n in ens18 ens19 ens20 ens21 ens22; do echo 1 > /proc/sys/net/ipv4/conf/$n/send_redirects; done
echo 0 > /proc/sys/net/ipv4/conf/all/send_redirects
echo 0 > /proc/sys/net/ipv4/conf/default/send_redirects
ip route add 192.0.2.0/24 via 10.9.0.1
redirects() { awk '/^Icmp:/{if(!h){split($0,k);h=1}else{split($0,v);for(i=1;i<=NF;i++) if(k[i]=="OutRedirects") print v[i]}}' /proc/net/snmp; }
gw_rx() { nsenter -t $g -n awk '$1=="ig:"{print $3}' /proc/net/dev; }
send() { nsenter -t $c -n ping -c 3 -i 0.2 -W 1 "$1" >/dev/null 2>&1; true; }
export PROC_SYS=/proc/sys NIC_EXPECT="ens18=10.9.0.254 ens19=x ens20=x ens21=x ens22=x"
. "$lib"; . "$defs"
check() { if send_redirects_off; then echo PASS; else echo FAIL; fi; }
v0=$(check); r0=$(redirects); x0=$(gw_rx); send 192.0.2.5; r1=$(redirects); x1=$(gw_rx)
first=$(sysctl_apply_file "$conf"); rc1=$?
v1=$(check); sleep 1.1; send 192.0.2.6; r2=$(redirects); x2=$(gw_rx)
second=$(sysctl_apply_file "$conf"); rc2=$?
kill $c $g
printf '{"verify_before":"%s","redirects":[%s,%s,%s],"gw_rx":[%s,%s,%s],"first":"%s","rc1":%s,"verify_after":"%s","second":"%s","rc2":%s}\n' \
    "$v0" "$r0" "$r1" "$r2" "$x0" "$x1" "$x2" "$first" "$rc1" "$v1" "$second" "$rc2"
"""


class RouterRedirectKernelTests(unittest.TestCase):
    """진짜 커널로 본다: all, default 가 0 이어도 NIC 값 1 이면 redirect 를 보내고(커널은 둘의 OR), router 단계의
    sysctl_apply_file 이 렌더된 파일을 쓰면 멈추며, 포워딩은 그대로이고, verify 의 send-redirects-off 가 앞뒤를 가른다(APT3 §2-5)."""

    def test_rendered_file_stops_redirects_keeps_forwarding_and_verify_tells_before_from_after(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = (DEPLOY / "steps" / "verify.sh").read_text()
            (root / "verify-defs.sh").write_text(source[:re.search(r"(?m)^check ", source).start()])
            (root / "redirects.conf").write_text(file_of(bundles(T_INV)["rt-core01"], render.ROUTER_REDIRECTS_CONF))
            done = subprocess.run(
                ["unshare", "-rn", "bash", "-c", REDIRECT_KERNEL_SCRIPT, "redirect-kernel",
                 str(DEPLOY / "steps" / "lib.sh"), str(root / "verify-defs.sh"), str(root / "redirects.conf")],
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            result = json.loads(done.stdout.strip().splitlines()[-1])
        r0, r1, r2 = result["redirects"]
        x0, x1, x2 = result["gw_rx"]
        self.assertEqual(result["verify_before"], "FAIL")
        self.assertGreater(r1, r0, "NIC 값 1 이면 all, default 가 0 이어도 redirect 를 보낸다")
        self.assertEqual((result["rc1"], result["first"]),
                         (0, " ".join(f"net.ipv4.conf.ens{n}.send_redirects" for n in range(18, 23))))
        self.assertEqual(result["verify_after"], "PASS")
        self.assertEqual(r2, r1, "끈 뒤에는 redirect 를 보내지 않는다")
        self.assertGreaterEqual(x1 - x0, 3)
        self.assertGreaterEqual(x2 - x1, 3, "끈 뒤에도 같은 패킷을 다음 홉으로 포워딩한다")
        self.assertEqual((result["rc2"], result["second"]), (0, ""), "다시 돌면 아무것도 쓰지 않는다")


def _point(metric, stamp, value=1, **tags):
    return json.dumps({"type": "Point", "metric": metric,
                       "data": {"time": stamp.isoformat().replace("+00:00", "Z"), "value": value, "tags": tags}})


class LoadgenMonitorExtensionTests(unittest.TestCase):
    """loadgen_monitor.py 의 선택 기능(--http-summary, --window-log). 끈 도메인의 문서는 그대로다."""

    BASE = datetime.datetime(2026, 11, 16, 7, 0, tzinfo=datetime.timezone.utc)

    def _feed(self, builder):
        for i in range(20):
            stamp = self.BASE + datetime.timedelta(seconds=i)
            builder.consume(_point("iterations", stamp))
            status = "503" if i % 10 == 0 else ("0" if i == 5 else "200")
            extra = {"error_code": "1050"} if i == 5 else {}
            builder.consume(_point("http_reqs", stamp, step="submit" if i % 2 else "search", status=status, **extra))
            builder.consume(_point("http_req_duration", stamp, value=float(i * 10), step="search"))
        return builder.build()

    def test_off_by_default_keeps_the_document_shape(self) -> None:
        document = self._feed(monitor.LiveDocumentBuilder({"domain": "d", "unit": "u"}, "submit", "lookup"))
        self.assertFalse(any(key.startswith("http_") for key in document))
        self.assertFalse(monitor.LiveDocumentBuilder({"domain": "d", "unit": "u"}, "submit").consume(
            _point("http_req_duration", self.BASE, value=1.0)))

    def test_summary_counts_every_request_and_p95(self) -> None:
        document = self._feed(monitor.LiveDocumentBuilder({"domain": "d", "unit": "u"}, "submit", "lookup", http_summary=True))
        self.assertEqual(document["http_count"], 20)
        self.assertAlmostEqual(document["http_5xx_rate"], 2 / 20)
        self.assertAlmostEqual(document["http_failed_rate"], 1 / 20)
        self.assertAlmostEqual(document["http_timeout_rate"], 1 / 20)
        self.assertAlmostEqual(document["http_abnormal_rate"], 3 / 20)
        self.assertEqual(document["http_p95_ms"], 180.0)
        # 단계별 사업 집계는 그대로 이어진다.
        self.assertEqual(document["checkout_count"], 10)

    def test_summary_without_requests_publishes_only_the_denominator(self) -> None:
        builder = monitor.LiveDocumentBuilder({"domain": "d", "unit": "u"}, "submit", http_summary=True)
        builder.consume(_point("iterations", self.BASE))
        document = builder.build()
        self.assertEqual(document["http_count"], 0)
        for key in ("http_5xx_rate", "http_failed_rate", "http_abnormal_rate", "http_p95_ms"):
            self.assertNotIn(key, document)

    def test_window_log_writes_one_line_per_closed_five_minutes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "w.jsonl"
            log = monitor.WindowLog(str(path))
            builder = monitor.LiveDocumentBuilder({"domain": "d", "unit": "u"}, "submit", window_log=log)
            for i in range(10):
                stamp = self.BASE + datetime.timedelta(seconds=30 * i)
                builder.consume(_point("iterations", stamp))
                builder.consume(_point("http_reqs", stamp, step="lookup", status="500" if i == 0 else "200"))
                builder.consume(_point("http_req_duration", stamp, value=float(i)))
            self.assertFalse(path.exists(), "열린 창은 쓰지 않는다")
            builder.consume(_point("iterations", self.BASE + datetime.timedelta(minutes=5, seconds=1)))
            lines = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["window_start"], "2026-11-16T07:00:00Z")
        self.assertEqual(lines[0]["http_count"], 10)
        self.assertEqual(lines[0]["http_5xx"], 1)
        self.assertEqual(lines[0]["iterations"], 10)
        self.assertEqual(lines[0]["http_p95_ms"], 9.0)

    def test_window_log_adds_per_step_p95_only_for_named_steps(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            plain_path = Path(raw) / "plain.jsonl"
            step_path = Path(raw) / "step.jsonl"
            plain = monitor.LiveDocumentBuilder({"domain": "d", "unit": "u"}, "submit", window_log=monitor.WindowLog(str(plain_path)))
            stepped = monitor.LiveDocumentBuilder({"domain": "d", "unit": "u"}, "submit",
                                                  window_log=monitor.WindowLog(str(step_path), steps=("search", "inbox")))
            for builder in (plain, stepped):
                for i in range(20):
                    stamp = self.BASE + datetime.timedelta(seconds=10 * i)
                    step = "search" if i % 2 else "lookup"
                    builder.consume(_point("iterations", stamp))
                    builder.consume(_point("http_reqs", stamp, step=step, status="200"))
                    builder.consume(_point("http_req_duration", stamp, value=float(i), step=step))
                builder.consume(_point("iterations", self.BASE + datetime.timedelta(minutes=5, seconds=1)))
            plain_line = json.loads(plain_path.read_text().splitlines()[0])
            step_line = json.loads(step_path.read_text().splitlines()[0])
        self.assertNotIn("step_p95_ms", plain_line)
        self.assertNotIn("step_count", plain_line)
        self.assertEqual({k: v for k, v in step_line.items() if not k.startswith("step_")}, plain_line)
        # search 는 홀수 i(1, 3, ..., 19)의 10건, p95 는 가까운 순위 10번째 = 19. 이름만 준 inbox 는 표본이 없어 빠진다.
        self.assertEqual(step_line["step_count"], {"search": 10})
        self.assertEqual(step_line["step_p95_ms"], {"search": 19.0})

    def test_window_step_needs_a_window_log(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            monitor.parse(["--source", "/x", "--output", "/y", "--business-step", "submit",
                           "--domain", "d", "--unit", "loadgen", "--window-step", "search"])

    def test_cli_parses_the_new_flags(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = str(Path(raw) / "w.jsonl")
            args, builder = monitor.parse(["--source", "/x", "--output", "/y", "--business-step", "submit",
                                           "--domain", "d", "--unit", "loadgen", "--http-summary", "--window-log", path])
            self.assertTrue(builder.http_summary)
            self.assertIsInstance(builder.window_log, monitor.WindowLog)
            self.assertEqual(builder.window_log.path, path)
            self.assertEqual(builder.window_log.steps, ())
            _, stepped = monitor.parse(["--source", "/x", "--output", "/y", "--business-step", "submit", "--domain", "d",
                                        "--unit", "loadgen", "--window-log", path, "--window-step", "search", "--window-step", "inbox"])
            self.assertEqual(stepped.window_log.steps, ("search", "inbox"))
            _, plain = monitor.parse(["--source", "/x", "--output", "/y", "--business-step", "submit",
                                      "--domain", "d", "--unit", "loadgen"])
            self.assertFalse(plain.http_summary)
            self.assertIsNone(plain.window_log)
            self.assertEqual(args.mode, "tail")

    def test_publisher_forwards_extra_monitor_arguments_only_when_set(self) -> None:
        helper = REPO / "scripts" / "loadgen" / "baseline-publish.sh"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fake = root / "monitor.py"
            fake.write_text("import sys, time\nopen(sys.argv[0] + '.args', 'a').write(' '.join(sys.argv[1:]) + '\\n')\ntime.sleep(5)\n")
            for extra in ("", "--http-summary --window-log /x/w.jsonl"):
                script = (f". {helper}\nbaseline_publisher_start d loadgen submit lookup\nsleep 1\nbaseline_publisher_stop\n")
                env = {"PATH": "/usr/bin:/bin", "BASELINE_STATE_DIR": str(root), "BASELINE_MONITOR": str(fake)}
                if extra:
                    env["BASELINE_MONITOR_ARGS"] = extra
                subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=60, env=env, check=True)
            calls = (root / "monitor.py.args").read_text().splitlines()
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0].endswith("--unit loadgen"))
        self.assertTrue(calls[1].endswith("--unit loadgen --http-summary --window-log /x/w.jsonl"))


class LoadgenScriptTests(unittest.TestCase):
    def test_entrypoint_publishes_submit_and_lookup_like_the_other_domains(self) -> None:
        entry = (DOMAIN / "loadgen" / "entrypoint.sh").read_text()
        self.assertRegex(entry, r'baseline_publisher_start "\$DOMAIN" loadgen submit lookup')
        script = (DOMAIN / "loadgen" / "script.js").read_text()
        for step in ("submit", "lookup"):
            self.assertIn(f"'{step}'", script)

    def test_journey_weights_follow_the_design_table(self) -> None:
        script = (DOMAIN / "loadgen" / "script.js").read_text()
        bounds = [int(n) for n in re.findall(r"^\s*\[(\d+), \w+\],$", script, re.M)]
        weights = [b - a for a, b in zip([0] + bounds, bounds)]
        # 담당자 처리 11 은 처리 수요(신청 8 x 1.25 = 10)의 약 110%(검토 세션 Q23 답 (가)), 1 은 통계 화면에서 옮김.
        self.assertEqual(weights, [8, 30, 25, 15, 11, 6, 5])

    def test_requests_carry_a_browser_user_agent_not_the_tool_name(self) -> None:
        script = (DOMAIN / "loadgen" / "script.js").read_text()
        agent = re.search(r"userAgent: '([^']+)'", script).group(1)
        self.assertNotRegex(agent.lower(), r"k6|grafana|load")

    def test_diurnal_table_matches_the_existing_domains(self) -> None:
        pattern = re.compile(r"diurnal_pct\(\) \{.*?\n\}", re.S)
        mine = pattern.search((DOMAIN / "loadgen" / "entrypoint.sh").read_text()).group(0)
        theirs = pattern.search((REPO / "commerce" / "loadgen" / "entrypoint.sh").read_text()).group(0)
        self.assertEqual(mine, theirs)


class ArchiveBatchSettingsTests(unittest.TestCase):
    """보관 배치의 건수 상한과 야간 창(검토 세션 Q21 답 (나)). 앱 쪽 동작은 Java 시험 BatchServiceTest 가 본다."""

    PROPERTIES = DOMAIN / "petition-web" / "src" / "main" / "resources" / "application.properties"

    def _settings(self) -> dict[str, str]:
        return dict(re.findall(r"^petition\.archive\.([a-z-]+)=(.+)$", self.PROPERTIES.read_text(encoding="utf-8"), re.M))

    def test_cap_is_the_seed_size_and_the_timer_runs_inside_the_window(self) -> None:
        settings = self._settings()
        seed_scale = re.search(r"^\\set scale (\d+)$", (DOMAIN / "db" / "seed.sql").read_text(), re.M).group(1)
        self.assertEqual(settings["keep-rows"], seed_scale)
        self.assertLessEqual(int(settings["chunk-rows"]), 1000)
        self.assertEqual(settings["zone"], "Asia/Seoul")
        timer = (DEPLOY / "templates" / "was" / "petition-archive.timer").read_text()
        hour, minute = map(int, re.search(r"OnCalendar=\*-\*-\* (\d\d):(\d\d):00 Asia/Seoul", timer).groups())
        start, end = (tuple(map(int, part.split(":"))) for part in settings["window"].split("-"))
        self.assertLess(start, end, "야간 창은 자정을 넘지 않는 한 구간으로 둔다")
        self.assertTrue(start <= (hour, minute) < end, (settings["window"], hour, minute))
        # 창이 낮 부하(KST 9~22시)와 겹치지 않는다.
        self.assertTrue(end <= (9, 0) and start >= (0, 0))

    def test_daily_capacity_keeps_up_with_twice_the_daily_submissions(self) -> None:
        settings = self._settings()
        entry = (DOMAIN / "loadgen" / "entrypoint.sh").read_text()
        pct = [int(v) for v in re.findall(r"\d+\) echo (\d+) ;;", entry)]
        self.assertEqual(len(pct), 24)
        daily = sum(5 + ((30 - 5) * p + 50) // 100 for p in pct) * 3600
        submissions = daily * 8 // 100
        self.assertEqual(submissions, 121824)
        hour, minute = 2, 20
        start, end = (tuple(map(int, part.split(":"))) for part in settings["window"].split("-"))
        open_sec = (end[0] * 60 + end[1] - hour * 60 - minute) * 60
        # 묶음 하나의 시간은 200만 시드에서 잰 0.07초(T2C 결과)에 넉넉히 1초를 잡는다.
        per_chunk_sec = int(settings["pause-ms"]) / 1000 + 1.0
        capacity = int(open_sec / per_chunk_sec) * int(settings["chunk-rows"])
        self.assertGreater(capacity, 2 * submissions * 10)

    def test_batch_log_line_carries_the_result_the_app_returned(self) -> None:
        template = (DEPLOY / "templates" / "was" / "petition-batch").read_text()
        script = template.replace("{{was_port}}", "8080").replace("{{app_context}}", "petition")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "petition-batch").write_text(script)
            curl = root / "curl"
            for result in ("ok", "skipped", "partial", "busy"):
                with self.subTest(result=result):
                    curl.write_text('#!/bin/sh\nprintf \'{"job":"archive","rows":12,"result":"%s","tookMs":3}\' "$FAKE_RESULT"\n')
                    curl.chmod(0o755)
                    log = root / f"{result}.log"
                    done = subprocess.run(["sh", str(root / "petition-batch"), "archive"], capture_output=True, text=True, timeout=30,
                                          env={"PATH": f"{root}:/usr/bin:/bin", "FAKE_RESULT": result, "PETITION_BATCH_LOG": str(log)})
                    self.assertEqual(done.returncode, 0, done.stderr)
                    self.assertRegex(log.read_text(), rf"^\S+ job=archive result={result} \{{")
            curl.write_text("#!/bin/sh\necho 'connection refused' >&2\nexit 7\n")
            log = root / "fail.log"
            done = subprocess.run(["sh", str(root / "petition-batch"), "archive"], capture_output=True, text=True, timeout=30,
                                  env={"PATH": f"{root}:/usr/bin:/bin", "PETITION_BATCH_LOG": str(log)})
            self.assertEqual(done.returncode, 1)
            self.assertIn("job=archive result=fail", log.read_text())

    def test_seed_products_cannot_overflow_at_two_million(self) -> None:
        seed = (DOMAIN / "db" / "seed.sql").read_text()
        for cast, factor in re.findall(r"\(i(::bigint)? \* (\d+)\)", seed):
            with self.subTest(factor=factor):
                if int(factor) * 2_000_000 > 2**31 - 1:
                    self.assertEqual(cast, "::bigint")


class LookupReceiptTests(unittest.TestCase):
    """부하기 접수번호 조회(설계 §5, 검토 세션 Q22 답 (가)): 원표 범위 안 최근 쪽, 없는 번호 고정 1%.

    k6 스크립트를 104 에서 돌릴 수 없으므로 같은 규칙을 파이썬으로 옮겨, 보관 배치가 오래된 번호를 옮겨 가는
    여러 날을 모의한다. 규칙과 상수가 스크립트와 같은지는 첫 시험이 글자로 묶는다.
    """

    SCRIPT = DOMAIN / "loadgen" / "script.js"

    def _constants(self) -> dict[str, float]:
        text = self.SCRIPT.read_text()
        return {name: float(value) for name, value in
                re.findall(r"^const (LOOKUP_[A-Z_]+) = ([0-9.]+);", text, re.M)}

    def test_the_script_rule_and_constants_are_the_ones_modelled_here(self) -> None:
        text = self.SCRIPT.read_text()
        self.assertEqual(self._constants(), {"LOOKUP_NOT_FOUND_RATE": 0.01, "LOOKUP_RECENT_MEAN": 200000,
                                             "LOOKUP_LIVE_SPAN": 1500000, "LOOKUP_MISSING_BASE": 90000000})
        for line in ("if (rand() < LOOKUP_NOT_FOUND_RATE) return LOOKUP_MISSING_BASE + randInt(0, 9999999);",
                     "while (distance >= LOOKUP_LIVE_SPAN) distance = Math.floor(-Math.log(1 - rand()) * LOOKUP_RECENT_MEAN);",
                     "return Math.max(1, newest - distance);",
                     "const newest = Math.max((data && data.newest) || SEED_PETITIONS, ownNewest);",
                     "const receipt = `CP${pad(pickReceiptNumber(newest), 8)}`;"):
            self.assertIn(line, text)
        # 없는 번호대도 앱의 접수번호 꼴(CP + 8자리)이라 400 이 아니라 404 로 간다.
        self.assertEqual(len(str(int(self._constants()["LOOKUP_MISSING_BASE"]) + 9999999)), 8)
        # 거리 상한은 보존 건수 상한보다 작다(원표 밖을 고르지 않게).
        keep = int(re.search(r"^petition\.archive\.keep-rows=(\d+)$",
                             (DOMAIN / "petition-web" / "src" / "main" / "resources" / "application.properties").read_text(), re.M).group(1))
        self.assertLess(self._constants()["LOOKUP_LIVE_SPAN"], keep)

    @staticmethod
    def _pick(rng, newest: int, c: dict[str, float]) -> int:
        if rng.random() < c["LOOKUP_NOT_FOUND_RATE"]:
            return int(c["LOOKUP_MISSING_BASE"]) + rng.randint(0, 9999999)
        distance = int(c["LOOKUP_LIVE_SPAN"])
        while distance >= c["LOOKUP_LIVE_SPAN"]:
            distance = math.floor(-math.log(1 - rng.random()) * c["LOOKUP_RECENT_MEAN"])
        return max(1, newest - distance)

    def _simulate(self, days: int, pick) -> list[float]:
        """하루 404 비율. 신청은 부하기 하루 표대로, 보관은 02:20 KST 에 상한(200만)을 넘은 가장 오래된 번호를 옮긴다.
        조회는 그 시각의 setup 값(실행 시작의 가장 최근 번호, 한 시간까지 묵음)을 쓴다(자기 신청 번호는 쓰지 않아 보수적)."""
        entry = (DOMAIN / "loadgen" / "entrypoint.sh").read_text()
        pct = [int(v) for v in re.findall(r"\d+\) echo (\d+) ;;", entry)]
        hourly_submits = [(5 + (25 * p + 50) // 100) * 3600 * 8 // 100 for p in pct]
        keep = 2_000_000
        rng = random.Random(42)
        newest, lowest_live = 2_000_000, 1
        rates = []
        for _ in range(days):
            misses = total = 0
            for hour in range(24):
                if hour == 2:
                    lowest_live = max(lowest_live, newest - keep + 1)
                setup_newest = newest
                for _ in range(400):
                    receipt = pick(rng, setup_newest)
                    misses += not (lowest_live <= receipt <= newest)
                    total += 1
                newest += hourly_submits[hour]
            rates.append(misses / total)
        return rates

    def test_not_found_lookups_stay_near_one_percent_while_the_archive_moves_through_the_seed(self) -> None:
        c = self._constants()
        rates = self._simulate(40, lambda rng, newest: self._pick(rng, newest, c))
        for day, rate in enumerate(rates):
            with self.subTest(day=day):
                self.assertAlmostEqual(rate, 0.01, delta=0.0035)
        self.assertLess(max(rates) - min(rates), 0.006)
        # 대조: 옛 규칙(시드 번호 뒤 3/4 에서 고르기)은 보관이 시드를 지나며 404 가 0 에서 100% 까지 오른다.
        old = self._simulate(40, lambda rng, newest: rng.randint(500_000, 2_000_000))
        self.assertLess(old[0], 0.001)
        self.assertGreater(old[-1], 0.99)


class SeedSteadyStateTests(unittest.TestCase):
    """시드가 곧 정상 상태(설계 §2-3, 검토 세션 Q21 답 ①, Q22 답의 시험 조건, 지휘 세션 11-17 결정).

    접수 시각은 부하기 시간별 신청 수 그대로 바로 앞 시간부터 거꾸로, 처리 상태는 부하기 여정이 만드는 정상 상태 꼴.
    실제 200만 시드의 분포는 수동 도구 archive-sql-check.sh 가 컨테이너에서 확인한다(T2C 결과에 수치).
    """

    SEED = DOMAIN / "db" / "seed.sql"

    @staticmethod
    def _loadgen_hourly_submits() -> list[int]:
        entry = (DOMAIN / "loadgen" / "entrypoint.sh").read_text()
        pct = [int(v) for v in re.findall(r"\d+\) echo (\d+) ;;", entry)]
        script = (DOMAIN / "loadgen" / "script.js").read_text()
        bounds = [int(n) for n in re.findall(r"^\s*\[(\d+), \w+\],$", script, re.M)]
        submit_share = bounds[0]   # 첫 여정(신청)의 비중
        return [(5 + ((30 - 5) * p + 50) // 100) * 3600 * submit_share // 100 for p in pct]

    def _seed_hourly(self) -> list[int]:
        text = self.SEED.read_text()
        block = re.search(r"\(ARRAY\[([0-9,\s]+)\]\)\s*\[1 \+ extract\(hour FROM", text).group(1)
        return [int(v) for v in block.split(",")]

    def test_seed_submissions_per_kst_hour_are_the_load_generators(self) -> None:
        self.assertEqual(self._seed_hourly(), self._loadgen_hourly_submits())
        self.assertEqual(sum(self._seed_hourly()), 121824)
        text = self.SEED.read_text()
        # 바로 앞 시간부터 거꾸로, 시각은 KST 로 읽는다. 옛 730일 펼치기는 없다.
        self.assertIn("date_trunc('hour', now()) - (n + 1) * interval '1 hour'", text)
        self.assertIn("AT TIME ZONE 'Asia/Seoul'", text)
        self.assertNotIn("730 days", text)

    def test_seed_status_rule_is_the_simulated_steady_state(self) -> None:
        text = self.SEED.read_text()
        self.assertIn("CASE WHEN :scale - i >= :scale * 15 / 10000 THEN 'COMPLETED'", text)   # 가장 최근 0.15% 만 열림
        self.assertIn("WHEN i % 8 = 0 THEN 'IN_PROGRESS'", text)                                # 열린 건의 12.5%
        delays = [int(v) for v in re.search(r"\(ARRAY\[(0, 1, [0-9,\s]+)\]\)", text).group(1).split(",")]
        self.assertEqual(len(delays), 20)
        self.assertEqual(delays, sorted(delays))
        self.assertTrue(10 <= (delays[9] + delays[10]) / 2 <= 25)   # 모의 중앙값 17분 근처
        self.assertIn("least(c + done_after, now())", text)            # 완료 시각이 미래가 아니다

    def test_period_reads_see_the_same_order_of_rows_on_every_operating_day(self) -> None:
        # Q22 답의 시험 조건: 운영 일수를 늘려도 기간 조건 읽기가 보는 행 수가 같은 자릿수.
        # 원표 = 시드(시간별 신청 수로 바로 앞부터 거꾸로 200만) + 부하기 쓰기 - 02:20 보관(상한 200만 넘은 가장 오래된 것).
        hourly = self._seed_hourly()
        keep = 2_000_000
        rows: collections.deque = collections.deque()   # (나이 시간, 행 수) 가장 오래된 것이 왼쪽
        remaining, back = keep, 0
        seed_hours = []
        while remaining > 0:
            back += 1
            take = min(hourly[(24 - back) % 24], remaining)   # 운영 시작 = KST 0시, back 시간 전 그 KST 시각
            seed_hours.append(take)
            remaining -= take
        clock = 0   # 운영 시작 뒤 시간
        table = [(-(i + 1), n) for i, n in enumerate(seed_hours)][::-1]

        def reads(now: int) -> tuple[int, int, float]:
            last7 = sum(n for t, n in table if now - t <= 7 * 24)
            last30 = sum(n for t, n in table if now - t <= 30 * 24)
            return int(last7 * 0.6), int(last30 * 0.6), sum(n for t, n in table if now - t <= 90 * 24) / 49700

        seen = {0: reads(0)}
        for day in range(40):
            for hour in range(24):
                if hour == 2:
                    excess = sum(n for _, n in table) - keep
                    while excess > 0:
                        t, n = table[0]
                        if n <= excess:
                            table.pop(0)
                            excess -= n
                        else:
                            table[0] = (t, n - excess)
                            excess = 0
                table.append((clock, hourly[hour]))
                clock += 1
            if day + 1 in (1, 4, 16, 40):
                seen[day + 1] = reads(clock)
        first = seen[0]
        for day, values in seen.items():
            with self.subTest(day=day):
                for name, base, value in zip(("public_7d", "public_30d", "per_user_90d"), first, values):
                    self.assertEqual(len(str(int(base))), len(str(int(value))), (name, base, value))
                    self.assertLess(abs(value / base - 1), 0.1, (name, base, value))

    @staticmethod
    def _journey_shares() -> dict[str, int]:
        script = (DOMAIN / "loadgen" / "script.js").read_text()
        pairs = re.findall(r"^\s*\[(\d+), (\w+)\],$", script, re.M)
        bounds = [int(n) for n, _ in pairs]
        return {name: b - a for (_, name), a, b in zip(pairs, [0] + bounds, bounds)}

    def test_open_backlog_settles_near_a_fixed_level_in_every_department(self) -> None:
        # 검토 세션 Q23 답 (가): 처리 능력을 수요의 약 110% 로. 부하기 여정(신청, 담당자 처리, 부서의 가장 오래된 열린
        # 10건 가운데 하나, 접수면 25% 처리 중)을 부서 30곳으로 줄여(부서당 비율은 부서 300 과 같음) 시드의 열린 건
        # (부서당 3,000/300 = 10, 그 12.5% 처리 중)에서 180일 돌린다.
        # 추세 판정: 8~180일의 일별 평균(시간마다 잰 열린 건 수의 하루 평균, 부서 300 으로 환산)에 최소제곱 직선을 맞춰
        # 기울기 x 기간(172일)이 평균의 15% 보다 작고, 마지막 30일 평균과 8~37일 평균의 비가 0.85~1.15 안이면 기어가는
        # 추세가 없다고 본다. 부서별: 어느 부서도 마지막 30일 평균이 전체 부서 평균의 3배를 넘지 않는다.
        shares = self._journey_shares()
        self.assertEqual(shares["submit"], 8)
        demand = shares["submit"] * 1.25
        self.assertAlmostEqual(shares["staffProcess"] / demand, 1.1, delta=0.005)
        hourly = self._seed_hourly()
        depts, total_depts = 30, 300
        rng = random.Random(11)
        open_ = [collections.deque([["P" if k % 8 == 0 else "R"] for k in range(10)]) for _ in range(depts)]
        daily, per_dept_late = [], [0.0] * depts
        for day in range(180):
            hourly_totals = []
            for hour in range(24):
                iterations = hourly[hour] * 100 // shares["submit"] * depts // total_depts
                events = [0] * (iterations * shares["submit"] // 100) + [1] * (iterations * shares["staffProcess"] // 100)
                rng.shuffle(events)
                for event in events:
                    queue = open_[rng.randrange(depts)]
                    if event == 0:
                        queue.append(["R"])
                    elif queue:
                        j = rng.randrange(min(10, len(queue)))
                        if queue[j][0] == "R" and rng.random() < 0.25:
                            queue[j][0] = "P"
                        else:
                            del queue[j]
                hourly_totals.append(sum(len(q) for q in open_) * total_depts / depts)
                if day >= 150:
                    for d, q in enumerate(open_):
                        per_dept_late[d] += len(q) / (30 * 24)
            daily.append(sum(hourly_totals) / 24)
        settled = daily[7:]
        mean = sum(settled) / len(settled)
        xs = list(range(len(settled)))
        x_mean = sum(xs) / len(xs)
        slope = sum((x - x_mean) * (y - mean) for x, y in zip(xs, settled)) / sum((x - x_mean) ** 2 for x in xs)
        self.assertLess(abs(slope * (len(settled) - 1)), 0.15 * mean, (slope, mean))
        early, late = sum(daily[7:37]) / 30, sum(daily[150:]) / 30
        self.assertTrue(0.85 <= late / early <= 1.15, (early, late))
        for day, value in enumerate(daily[1:], start=2):
            with self.subTest(day=day):
                self.assertEqual(len(str(int(value))), len(str(3000)), value)
        dept_mean = sum(per_dept_late) / depts
        self.assertLess(max(per_dept_late), 3 * dept_mean, (max(per_dept_late), dept_mean))
        # 시드의 열린 건 수(3,000)가 이 정상 상태 수준의 같은 자릿수이고 20% 안이다.
        self.assertLess(abs(mean / 3000 - 1), 0.2, mean)

    def test_relogin_rotates_one_identity_with_one_login_request(self) -> None:
        script = (DOMAIN / "loadgen" / "script.js").read_text()
        body = re.search(r"function relogin\(\) \{(.*?)\n\}", script, re.S).group(1)
        self.assertIn("citizenId = randInt(1, CITIZENS);", body)
        self.assertIn("staffIndex = randInt(1, STAFF);", body)
        self.assertEqual(body.count("login("), 2)          # 두 갈래에 하나씩, 한 번에는 하나
        self.assertIn("if (rand() < 0.5) {", body)
        self.assertRegex(script, r"(?m)^let citizenId = randInt\(1, CITIZENS\);$")
        self.assertRegex(script, r"(?m)^let staffIndex = randInt\(1, STAFF\);$")


class StabilityCheckTests(unittest.TestCase):
    END = datetime.datetime(2026, 11, 17, 15, 0, tzinfo=datetime.timezone.utc)

    def _windows(self, bad=None, gap=None, p95=200.0, search_end=40.0):
        """판정 구간 24시간과 그 앞 2시간(대표 질의의 처음 구간)의 창. search_end: 마지막 2시간 창들의 search p95(나머지는 40ms)."""
        windows = {}
        start = int(self.END.timestamp()) - 24 * 3600
        for t in range(start - 2 * 3600, int(self.END.timestamp()), 300):
            if gap and t in gap:
                continue
            search = search_end if t >= int(self.END.timestamp()) - 2 * 3600 else 40.0
            record = {"window_start": datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
                      "http_count": 6000, "http_5xx": 0, "http_no_response": 0, "http_timeout": 0, "http_p95_ms": p95,
                      "step_p95_ms": {"search": search, "my-list": 30.0, "inbox": 20.0}}
            if bad and t in bad:
                record["http_5xx"] = bad[t]
            windows[t] = record
        return windows

    # 02:20 KST 에 돈 보관 배치(판정 구간 2026-11-16 15:00Z ~ 11-17 15:00Z 안).
    ARCHIVE_OK = {"started_at": "2026-11-16T17:20:00Z", "finished_at": "2026-11-16T17:23:10Z", "rows": 121000, "result": "ok"}

    def _db(self, start=(2_000_000, 5_900_000), end=(2_118_000, 6_170_000), runs=None):
        return {"before": {"rows": {"petition": start[0], "petition_history": start[1]}, "archive_runs": []},
                "after": {"rows": {"petition": end[0], "petition_history": end[1]},
                          "archive_runs": [self.ARCHIVE_OK] if runs is None else runs}}

    def test_quiet_day_passes(self) -> None:
        result = stability.judge(self._windows(), self.END, db_facts=self._db())
        self.assertEqual(result["verdict"], "PASS_Q3_24H", result["reasons"])
        self.assertEqual(result["windows_expected"], 288)
        self.assertEqual(result["steady_state"]["query"]["start_windows"], 24)
        self.assertEqual(result["steady_state"]["query"]["change"], 0.0)
        self.assertEqual([q["step"] for q in result["steady_state"]["reference_queries"]], ["my-list", "inbox"])

    def test_without_db_facts_the_steady_state_is_not_shown_and_it_fails(self) -> None:
        result = stability.judge(self._windows(), self.END)
        self.assertEqual(result["verdict"], "FAIL_Q3_24H")
        self.assertTrue(any(r.startswith("DB 사실") for r in result["reasons"]))

    def test_main_table_rows_must_stay_in_the_same_order_of_magnitude(self) -> None:
        grown = stability.judge(self._windows(), self.END, db_facts=self._db(end=(12_000_000, 6_170_000)))
        self.assertEqual(grown["verdict"], "FAIL_Q3_24H")
        self.assertIn("주 표 petition 행 수 자릿수가 바뀜 2000000 -> 12000000", grown["reasons"])
        history = stability.judge(self._windows(), self.END, db_facts=self._db(end=(2_100_000, 10_000_000)))
        self.assertTrue(any(r.startswith("주 표 petition_history") for r in history["reasons"]))

    def test_the_archive_batch_must_have_run_ok_inside_the_day(self) -> None:
        cases = {
            "없음": [],
            "창 밖에서 건너뜀만": [{"finished_at": "2026-11-17T05:00:01Z", "rows": 0, "result": "skipped"}],
            "다 못 옮김": [{"finished_at": "2026-11-16T20:00:01Z", "rows": 900000, "result": "partial"}],
            "구간 앞": [{"finished_at": "2026-11-15T17:23:10Z", "rows": 121000, "result": "ok"}],
        }
        for name, runs in cases.items():
            with self.subTest(case=name):
                result = stability.judge(self._windows(), self.END, db_facts=self._db(runs=runs))
                self.assertEqual(result["verdict"], "FAIL_Q3_24H")
                self.assertIn("판정 구간 안에 결과 ok 인 보관 배치가 없음", result["reasons"])

    def test_representative_query_p95_may_move_at_most_ten_percent(self) -> None:
        self.assertEqual(stability.judge(self._windows(search_end=43.6), self.END, db_facts=self._db())["verdict"], "PASS_Q3_24H")
        self.assertEqual(stability.judge(self._windows(search_end=36.4), self.END, db_facts=self._db())["verdict"], "PASS_Q3_24H")
        for value in (44.4, 35.6):
            with self.subTest(end_ms=value):
                result = stability.judge(self._windows(search_end=value), self.END, db_facts=self._db())
                self.assertEqual(result["verdict"], "FAIL_Q3_24H")
                self.assertTrue(any(r.startswith("대표 질의(search) p95 변화") for r in result["reasons"]), result["reasons"])

    def test_a_steady_daily_cycle_passes_at_every_end_hour_and_a_shift_at_the_same_hour_fails(self) -> None:
        # 일주기 정상 상태: search p95 가 그 시각 목표 요청률을 따라 움직이되 같은 시각이면 매일 같다(리뷰 H1).
        entry = (DOMAIN / "loadgen" / "entrypoint.sh").read_text()
        pct = [int(v) for v in re.findall(r"\d+\) echo (\d+) ;;", entry)]
        kst = datetime.timezone(datetime.timedelta(hours=9))

        def windows(end, shift_from=None, factor=1.0):
            out = {}
            e = int(end.timestamp())
            for t in range(e - 26 * 3600, e, 300):
                hour = datetime.datetime.fromtimestamp(t, kst).hour
                value = 40.0 + 1.0 * (5 + (25 * pct[hour] + 50) // 100)
                if shift_from is not None and t >= shift_from:
                    value *= factor
                out[t] = {"window_start": datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
                          "http_count": 6000, "http_5xx": 0, "http_no_response": 0, "http_timeout": 0, "http_p95_ms": 200.0,
                          "step_p95_ms": {"search": value, "my-list": 30.0, "inbox": 20.0}}
            return out

        for hour in range(24):
            end = datetime.datetime(2026, 11, 18, hour, 0, tzinfo=kst).astimezone(datetime.timezone.utc)
            db = self._db(runs=[{"started_at": (end - datetime.timedelta(hours=1, seconds=190)).isoformat().replace("+00:00", "Z"),
                                 "finished_at": (end - datetime.timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
                                 "rows": 121000, "result": "ok"}])
            with self.subTest(end_hour_kst=hour):
                steady = stability.judge(windows(end), end, db_facts=db)
                self.assertEqual(steady["verdict"], "PASS_Q3_24H", steady["reasons"])
                self.assertEqual(steady["steady_state"]["query"]["change"], 0.0)
                shifted = stability.judge(windows(end, int(end.timestamp()) - 2 * 3600, 1.11), end, db_facts=db)
                self.assertEqual(shifted["verdict"], "FAIL_Q3_24H")
                self.assertTrue(any(r.startswith("대표 질의(search) p95 변화") for r in shifted["reasons"]), shifted["reasons"])

    def test_a_record_younger_than_twenty_six_hours_is_incomplete_not_failed_or_shortened(self) -> None:
        # 리뷰 L8: 앞 2시간 창이 하나도 없거나(24시간 기록) 일부만 있으면(25시간 기록) 짧은 구간으로 견주지 않는다.
        start = int(self.END.timestamp()) - 24 * 3600
        for hours_before in (0, 1):
            with self.subTest(record_hours=24 + hours_before):
                windows = self._windows()
                for t in range(start - 2 * 3600, start - hours_before * 3600, 300):
                    windows.pop(t)
                result = stability.judge(windows, self.END, db_facts=self._db())
                self.assertEqual(result["verdict"], "INCOMPLETE_Q3_24H")
                self.assertTrue(result["reasons"][0].startswith("창 기록이 대표 질의 처음 구간"), result["reasons"])
        # 25시간 기록에 1시간 앞 값을 바꿔도 PASS 로 빠져나가지 않는다.
        windows = self._windows()
        for t in range(start - 2 * 3600, start - 3600, 300):
            windows.pop(t)
        self.assertNotEqual(stability.judge(windows, self.END, db_facts=self._db())["verdict"], "PASS_Q3_24H")

    def test_representative_query_needs_twenty_two_windows_in_each_span(self) -> None:
        start = int(self.END.timestamp()) - 24 * 3600
        for dropped, verdict in ((2, "PASS_Q3_24H"), (3, "FAIL_Q3_24H")):
            with self.subTest(dropped=dropped):
                windows = self._windows()
                for t in range(start - 3600, start - 3600 + 300 * dropped, 300):
                    windows[t].pop("step_p95_ms")
                result = stability.judge(windows, self.END, db_facts=self._db())
                self.assertEqual(result["verdict"], verdict, result["reasons"])
                if verdict.startswith("FAIL"):
                    self.assertEqual(result["reasons"], ["대표 질의(search) p95 표본 부족(처음 21창, 끝 24창, 구간마다 22창 이상)"])

    def test_the_night_batch_must_be_able_to_move_two_and_a_half_days_of_writes(self) -> None:
        # 검토 세션 Q24 답 조건 ①: 실측 처리량으로 창 하나(02:20~05:00, 9,600초)가 하루 쓰기 2.5일 몫(304,560) 이상을 옮긴다.
        passing = stability.judge(self._windows(), self.END, db_facts=self._db())
        capacity = passing["steady_state"]["night_capacity"]
        self.assertEqual(capacity["verdict"], "PASS_ARCHIVE_CAPACITY")
        self.assertEqual(capacity["required_rows"], 304560)
        self.assertEqual(capacity["capacity_rows"], int(121000 / 190 * 9600))
        # 같은 121,000 행에 3,900초가 걸린 밤(창 9,600초에 약 29.8만 행)은 2.5일 몫을 못 옮긴다.
        slow = {**self.ARCHIVE_OK, "started_at": "2026-11-16T16:18:10Z"}
        result = stability.judge(self._windows(), self.END, db_facts=self._db(runs=[slow]))
        self.assertEqual(result["verdict"], "FAIL_Q3_24H")
        self.assertEqual(result["reasons"], ["밤 배치 처리 능력 297846 < 하루 쓰기 2.5일 몫 304560(실측 121000행 3900.0초)"])
        # 리뷰 L12: 걸린 시간이 0 이면 처리 능력 0 으로 세지 않고 잴 수 없음으로 센다(시각은 밀리초까지 온다).
        instant = {**self.ARCHIVE_OK, "started_at": "2026-11-16T17:23:10.000Z", "finished_at": "2026-11-16T17:23:10.000Z"}
        result = stability.judge(self._windows(), self.END, db_facts=self._db(runs=[instant]))
        self.assertEqual(result["reasons"], ["보관 배치(121000행)의 걸린 시간이 0 이라 밤 배치 처리 능력을 잴 수 없음"])
        quick = {**self.ARCHIVE_OK, "started_at": "2026-11-16T17:23:10.120Z", "finished_at": "2026-11-16T17:23:10.870Z"}
        self.assertEqual(stability.judge(self._windows(), self.END, db_facts=self._db(runs=[quick]))["verdict"], "PASS_Q3_24H")
        self.assertIn('HH24:MI:SS.MS"Z"', (DEPLOY / "tools" / "db-facts.sh").read_text())
        # 행을 옮긴 배치가 없으면 잴 수 없어 실패로 센다(첫 밤은 부하 시작 뒤 쓰기만큼 옮긴다).
        empty = {**self.ARCHIVE_OK, "rows": 0}
        result = stability.judge(self._windows(), self.END, db_facts=self._db(runs=[empty]))
        self.assertIn("판정 구간 안에 행을 옮긴 보관 배치가 없어 밤 배치 처리 능력을 잴 수 없음", result["reasons"])

    def test_the_design_numbers_are_read_from_the_load_and_app_settings(self) -> None:
        # Q24 답 조건 ②: 손으로 적지 않고 부하 설정에서 읽어 계산한다.
        self.assertEqual(design.daily_submissions(), 121824)
        self.assertEqual(design.rows_limit(), {"limit": 2304560, "keep_rows": 2000000, "design_daily_writes": 121824, "days": 2.5})
        static = design.night_capacity()
        self.assertEqual((static["verdict"], static["open_sec"], static["capacity_rows"]), ("PASS_ARCHIVE_CAPACITY", 9600, 7680000))
        # 문턱의 뜻: 한 번 실패한 다음 밤 배치 직전 최대(상한 + 하루 쓰기 x 2)는 통과, 두 번 연속이면 그다음 날 낮에 넘는다.
        limit = design.rows_limit()["limit"]
        self.assertLess(2_000_000 + 2 * 121824, limit)
        self.assertGreater(2_000_000 + 3 * 121824, limit)
        # 부하 설정이 바뀌면 문턱도 따라 바뀐다(하루 쓰기를 손으로 적지 않음). 저장소 파일은 건드리지 않고
        # 읽기만 바꿔 끼운다(리뷰 L13: 시험이 강제로 끝나도 추적 파일이 바뀐 채 남지 않게).
        real_read = design._read

        def doubled_peak(relative: str) -> str:
            text = real_read(relative)
            return text.replace("Environment=PEAK_RPS=30", "Environment=PEAK_RPS=60") if relative.endswith("loadgen.service") else text

        with unittest.mock.patch.object(design, "_read", doubled_peak):
            doubled = design.daily_submissions()
            self.assertGreater(doubled, 121824)
            self.assertEqual(design.rows_limit()["limit"], 2_000_000 + math.ceil(doubled * 2.5))
        self.assertEqual(design.daily_submissions(), 121824)
        db = bundles(T_INV)["db01"]
        line = [l for l in file_of(db, "/usr/local/sbin/pg-table-stats").splitlines() if l.startswith("printf ")][0]
        self.assertIn('"rows_limit":{"limit":2304560,"keep_rows":2000000,"design_daily_writes":121824,"days":2.5}', line)
        # 놓친 밤 배치 창을 세는 데 쓰는 창(앱 설정의 타이머 시각, 창 끝, 시간대)도 줄이 싣는다(리뷰 M3).
        self.assertEqual(design.archive_schedule(),
                         {"timer": "02:20", "window_end": "05:00", "zone": "Asia/Seoul", "timer_zone": "Asia/Seoul"})
        self.assertIn('"archive_schedule":{"timer":"02:20","window_end":"05:00","zone":"Asia/Seoul","timer_zone":"Asia/Seoul"}', line)
        # 리뷰 L15: 타이머 유닛의 달력 시간대는 타이머 파일에서 읽는다(앱 zone 을 베끼지 않는다). 다르면 러너가 막는다.
        with unittest.mock.patch.object(design, "_read", lambda relative: real_read(relative).replace(
                "02:20:00 Asia/Seoul", "02:20:00 UTC") if relative.endswith("petition-archive.timer") else real_read(relative)):
            self.assertEqual(design.archive_schedule()["timer_zone"], "UTC")
            self.assertEqual(design.archive_schedule()["zone"], "Asia/Seoul")

    def test_representative_query_without_samples_fails(self) -> None:
        windows = self._windows()
        for record in windows.values():
            record.pop("step_p95_ms")
        result = stability.judge(windows, self.END, db_facts=self._db())
        self.assertEqual(result["verdict"], "FAIL_Q3_24H")
        self.assertIn("대표 질의(search) p95 표본 부족(처음 0창, 끝 0창, 구간마다 22창 이상)", result["reasons"])

    def test_the_table_stats_read_is_light_periodic_and_leaves_no_wait_or_error_lines(self) -> None:
        # 리뷰 rv-t2c M2 와 지휘 세션 D: 보관 상태 읽기는 표를 훑거나 민원 표를 잠그지 않고, 잠금 대기 줄과 오류 줄을
        # 남기지 않으며, 주입과 무관한 30분 고정 주기 타이머로 평시부터 늘 돈다. 출력 꼴은 러너가 읽는 꼴(db-facts.sh 와 같음).
        db = bundles(T_INV)["db01"]
        source = file_of(db, "/usr/local/sbin/pg-table-stats")
        sql = source[source.index("<<'SQL'"):source.index("\nSQL\n")]
        self.assertIn("-d petition <<'SQL'", source)
        self.assertNotIn("count(*)", sql)
        self.assertIn("SELECT n_live_tup FROM pg_stat_user_tables WHERE relname = 'petition'", sql)
        self.assertIn("result = 'ok'", sql)
        settings = dict(re.findall(r"^SET ([a-z_]+) = (.+);$", sql, re.M))
        self.assertEqual(settings["log_lock_waits"], "off")
        self.assertEqual(settings["log_min_duration_statement"], "-1")
        self.assertEqual(settings["log_min_messages"], "panic")
        self.assertEqual(settings["log_min_error_statement"], "panic")
        # lock_timeout 은 deadlock_timeout(PostgreSQL 기본 1초, logging.conf 와 tuning.conf 가 바꾸지 않음)보다 짧다.
        self.assertEqual(settings["lock_timeout"], "'200ms'")
        self.assertEqual(settings["statement_timeout"], "'1s'")
        for conf in ("logging.conf", "tuning.conf"):
            self.assertNotRegex((DEPLOY / "templates" / "db" / conf).read_text(), r"(?m)^\s*deadlock_timeout\s*=")
        # 읽은 뒤 log_min_messages 를 되돌려 종료 줄이 평소대로 남는다.
        self.assertLess(sql.index("SELECT json_build_object"), sql.index("RESET log_min_messages;"))
        self.assertIn("runuser -u postgres -- psql", source)
        # 30분 고정 주기(매시 05, 35분), 주입과 무관. 설치가 켜고 한 번 돌려 첫 파일을 만든다.
        timer = file_of(db, "/etc/systemd/system/pg-table-stats.timer")
        self.assertIn("OnCalendar=*-*-* *:05,35:00", timer)
        self.assertNotRegex(timer + file_of(db, "/etc/systemd/system/pg-table-stats.service"), r"(?i)test|tb|t4|scenario")
        step = (DEPLOY / "steps" / "db-backup.sh").read_text()
        self.assertIn("systemctl enable --now pg-backup.timer pg-table-stats.timer", step)
        self.assertIn("systemctl start pg-table-stats.service", step)
        self.assertIn("check table-stats-timer systemctl is-enabled pg-table-stats.timer",
                      (DEPLOY / "steps" / "verify.sh").read_text())
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "runuser").write_text('#!/bin/sh\ncat >/dev/null\nprintf \'{"rows" : {"petition" : 2080000}, "rows_source" : '
                                          '"n_live_tup", "last_archive_ok" : "2026-11-17T17:24:00Z"}\\n\'\n')
            (root / "runuser").chmod(0o755)
            script = root / "pg-table-stats"
            script.write_text(source.replace("/var/lib/pg-table-stats", str(root / "out")))
            done = subprocess.run(["sh", str(script)], capture_output=True, text=True, timeout=30,
                                  env={"PATH": f"{root}:/usr/bin:/bin"})
            self.assertEqual(done.returncode, 0, done.stderr)
            line = json.loads((root / "out" / "last.json").read_text())
            self.assertEqual(sorted(line), ["archive_schedule", "at", "db", "host", "rows_limit"])
            self.assertEqual(line["archive_schedule"]["window_end"], "05:00")
            self.assertEqual(line["rows_limit"]["limit"], 2304560)
            self.assertEqual(line["db"]["rows"]["petition"], 2080000)
            self.assertEqual(line["db"]["last_archive_ok"], "2026-11-17T17:24:00Z")
            self.assertEqual([p.name for p in (root / "out").iterdir()], ["last.json"])   # 임시 파일이 남지 않는다

    def test_db_facts_files_are_read_from_the_tool_output_shape(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            before = Path(raw) / "b.jsonl"
            after = Path(raw) / "a.jsonl"
            before.write_text('{"host":"db01","at":"2026-11-16T15:00:00Z","db":{"rows":{"petition":2000000,"petition_history":5900000},'
                              '"archive_runs":[]}}\n')
            after.write_text('{"host":"db01","at":"2026-11-17T15:00:00Z","db":{"rows":{"petition":2118000,"petition_history":6170000},'
                             '"archive_runs":[{"started_at":"2026-11-16T17:20:00Z","finished_at":"2026-11-16T17:23:10Z",'
                             '"rows":121000,"result":"ok"}]}}\n')
            facts = stability.read_db_facts(before, after)
        self.assertEqual(facts, self._db())
        source = (DEPLOY / "tools" / "db-facts.sh").read_text()
        # 점검 읽기는 느린 문장 기준에 걸려도 서버 로그에 남지 않는다. 출력 열쇠는 판정기가 읽는 이름과 같다.
        self.assertIn("SET log_min_duration_statement = -1;", source)
        for key in ("'rows'", "'petition'", "'petition_history'", "'archive_runs'", "'finished_at'", "'result'", '"db":%s',
                    "'last_archive_ok'", "result = 'ok'"):
            self.assertIn(key, source)

    def test_total_rate_over_point_one_percent_fails(self) -> None:
        start = int(self.END.timestamp()) - 24 * 3600
        bad = {start + 300 * i: 20 for i in range(100)}   # 창 비율 0.33%(창 기준은 지킴), 전체 0.116%
        result = stability.judge(self._windows(bad=bad), self.END)
        self.assertEqual(result["verdict"], "FAIL_Q3_24H")
        self.assertTrue(any("전체" in r for r in result["reasons"]))

    def test_missing_windows_count_against_the_ninety_nine_percent(self) -> None:
        start = int(self.END.timestamp()) - 24 * 3600
        gap = {start + 300 * i for i in range(1, 5)}
        result = stability.judge(self._windows(gap=gap), self.END, db_facts=self._db())
        self.assertEqual(result["windows_missing"], 4)
        self.assertEqual(result["verdict"], "FAIL_Q3_24H")
        self.assertEqual(result["reasons"], ["0.5% 이하 창 비율 0.9861 < 0.99(빠진 창 4)"])

    def test_slow_peak_and_host_facts_fail(self) -> None:
        slow = stability.judge(self._windows(p95=900.0), self.END, db_facts=self._db())
        self.assertEqual(slow["verdict"], "FAIL_Q3_24H")
        self.assertEqual(slow["reasons"], ["피크 p95 최댓값 900.0ms > 800.0ms"])
        facts = {"was01": {"nrestarts_before": {"tomcat10.service": 0}, "nrestarts_after": {"tomcat10.service": 1},
                           "oom_kills": 0, "chrony_offset_ms": 1.2}}
        result = stability.judge(self._windows(), self.END, host_facts=facts, db_facts=self._db())
        self.assertEqual(result["verdict"], "FAIL_Q3_24H")
        self.assertIn("was01 tomcat10.service 재시작 1", result["reasons"])

    def test_facts_files_merge_into_restart_deltas_and_reboots(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            before = Path(raw) / "b.jsonl"
            after = Path(raw) / "a.jsonl"
            before.write_text(
                '{"host":"was01","boot":"2026-11-16 10:00:00","nrestarts":{"tomcat10.service":2},"oom_kills":0,"chrony_offset_ms":0.4}\n'
                '{"host":"db01","boot":"2026-11-16 10:00:00","nrestarts":{"postgresql@16-main.service":0},"oom_kills":0,"chrony_offset_ms":0.2}\n')
            after.write_text(
                '{"host":"was01","boot":"2026-11-16 10:00:00","nrestarts":{"tomcat10.service":2},"oom_kills":0,"chrony_offset_ms":0.9}\n'
                '{"host":"db01","boot":"2026-11-17 03:12:00","nrestarts":{"postgresql@16-main.service":0},"oom_kills":0,"chrony_offset_ms":0.3}\n')
            facts = stability.merge_facts(before, after)
        self.assertFalse(facts["was01"]["rebooted"])
        self.assertTrue(facts["db01"]["rebooted"])
        result = stability.judge(self._windows(), self.END, host_facts=facts)
        self.assertEqual(result["verdict"], "FAIL_Q3_24H")
        self.assertEqual(result["hosts"]["was01"]["restarts"], {"tomcat10.service": 0})
        self.assertTrue(any(r.startswith("db01 창 안에서 재부팅") for r in result["reasons"]))

    def test_a_manual_restart_without_nrestarts_still_fails(self) -> None:
        facts = {"web01": {"nrestarts_before": {"apache2.service": 0}, "nrestarts_after": {"apache2.service": 0},
                           "invocations_before": {"apache2.service": "aa"}, "invocations_after": {"apache2.service": "bb"},
                           "oom_kills": 0, "chrony_offset_ms": 0.1}}
        result = stability.judge(self._windows(), self.END, host_facts=facts)
        self.assertEqual(result["verdict"], "FAIL_Q3_24H")
        self.assertIn("web01 apache2.service 다시 시작됨(InvocationID 바뀜)", result["reasons"])

    def test_short_record_is_incomplete(self) -> None:
        windows = self._windows()
        cut = int(self.END.timestamp()) - 3600
        result = stability.judge({t: w for t, w in windows.items() if t >= cut}, self.END)
        self.assertEqual(result["verdict"], "INCOMPLETE_Q3_24H")


class ScaleVerdictTests(unittest.TestCase):
    def _cpu(self, busy, softirq=1.0, steal=0.5):
        return [{"busy_pct": busy, "softirq_pct": softirq, "steal_pct": steal} for _ in range(180)]

    def _summary(self, requests=108000, s5=0, none=0, rate=60.0):
        return {"metrics": {"http_reqs": {"count": requests}, "http_5xx": {"count": s5},
                            "http_no_response": {"count": none}, "iterations": {"rate": rate},
                            "http_req_duration": {"p(95)": 320.0}}}

    def test_pass_and_name(self) -> None:
        cpu = {"router": self._cpu(30), "was01": self._cpu(40), "was02": self._cpu(45), "hypervisor": self._cpu(20, steal=0)}
        result = scale.judge(self._summary(), cpu)
        self.assertEqual(result["verdict"], "PASS_2X_60RPS_30M", result["reasons"])

    def test_each_limit_fails(self) -> None:
        ok = {"router": self._cpu(30), "was01": self._cpu(40), "hypervisor": self._cpu(20, steal=0)}
        cases = {
            "abnormal": (self._summary(s5=1200), ok),
            "router": (self._summary(), {**ok, "router": self._cpu(55)}),
            "softirq": (self._summary(), {**ok, "router": self._cpu(30, softirq=25)}),
            "was": (self._summary(), {**ok, "was01": self._cpu(75)}),
            "hypervisor": (self._summary(), {**ok, "hypervisor": self._cpu(75, steal=0)}),
            "steal": (self._summary(), {**ok, "was01": self._cpu(40, steal=6)}),
            "delivery": (self._summary(rate=40.0), ok),
            "missing": (self._summary(), {"router": self._cpu(30), "hypervisor": self._cpu(20)}),
        }
        for name, (summary, cpu) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(scale.judge(summary, cpu)["verdict"], "FAIL_2X_60RPS_30M")


    def test_a_testbed_without_a_router_needs_no_router_samples(self) -> None:
        # S 는 라우터가 없다(S 문서 결정 3). 인벤토리를 주면 라우터 표본을 요구하지 않고, T 인벤토리나 인벤토리가 없으면 요구한다.
        cpu = {"wassvr01": self._cpu(40), "wassvr02": self._cpu(45), "websvr01": self._cpu(10),
               "dbsvr01": self._cpu(20), "hypervisor": self._cpu(30, steal=0)}
        self.assertEqual(scale.needed_hosts(inventory(S_INV)), ("was", "hypervisor"))
        self.assertEqual(scale.needed_hosts(inventory(T_INV)), ("router", "was", "hypervisor"))
        self.assertEqual(scale.needed_hosts(None), ("router", "was", "hypervisor"))
        result = scale.judge(self._summary(), cpu, scale.needed_hosts(inventory(S_INV)))
        self.assertEqual(result["verdict"], "PASS_2X_60RPS_30M", result["reasons"])
        result = scale.judge(self._summary(), cpu, scale.needed_hosts(inventory(T_INV)))
        self.assertIn("router CPU 표본 파일이 없음", result["reasons"])
        # S 의 WAS 이름(wassvr..)에도 WAS 기준이 걸리고, 다른 VM 의 steal 기준도 그대로다.
        for name, sample in (("wassvr02", self._cpu(75)), ("dbsvr01", self._cpu(20, steal=6))):
            with self.subTest(case=name):
                result = scale.judge(self._summary(), {**cpu, name: sample}, scale.needed_hosts(inventory(S_INV)))
                self.assertEqual(result["verdict"], "FAIL_2X_60RPS_30M")
        without_was = {k: v for k, v in cpu.items() if not k.startswith("was")}
        self.assertIn("was CPU 표본 파일이 없음",
                      scale.judge(self._summary(), without_was, scale.needed_hosts(inventory(S_INV)))["reasons"])
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "k6.json").write_text(json.dumps(self._summary()))
            args = ["--summary", str(root / "k6.json"), "--inventory", str(S_INV)]
            for name, samples in cpu.items():
                (root / f"{name}.jsonl").write_text("".join(json.dumps(x) + "\n" for x in samples))
                args += ["--cpu", f"{name}={root / (name + '.jsonl')}"]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(scale.main(args), 0)
                self.assertEqual(scale.main([a if a != str(S_INV) else str(T_INV) for a in args]), 1)


class PairPackagesTests(unittest.TestCase):
    """tools/pair_packages.py: T 와 S 짝 서버의 패키지 판 대조(S 문서 §2-4 쌍 조건 2)."""

    BASE = "ii  chrony 4.5-1ubuntu4.2\nii  libc6:amd64 2.39-0ubuntu8.9\nii  zstd 1.5.5+dfsg2-2build1.1\n"

    def _run(self, t_lists: dict[str, str], s_lists: dict[str, str], *extra: str) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for side, lists in (("t", t_lists), ("s", s_lists)):
                (root / side).mkdir()
                for name, text in lists.items():
                    (root / side / f"{name}.txt").write_text(text)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = pairpkg.main(["--t-inventory", str(T_INV), "--s-inventory", str(S_INV),
                                     "--t-dir", str(root / "t"), "--s-dir", str(root / "s"), *extra])
            return code, out.getvalue()

    def _all(self, inv: dict, text: str, **override: str) -> dict[str, str]:
        return {h["name"]: override.get(h["name"], text) for h in inv["hosts"] if h["role"] != "router"}

    def test_pairs_are_role_and_order_and_the_router_has_no_pair(self) -> None:
        self.assertEqual(pairpkg.pairs(inventory(T_INV), inventory(S_INV)), [
            ("web0", "web01", "websvr01"), ("was0", "was01", "wassvr01"), ("was1", "was02", "wassvr02"),
            ("db0", "db01", "dbsvr01"), ("loadgen0", "loadgen", "loadgen")])

    def test_same_lists_pass_and_every_kind_of_difference_fails(self) -> None:
        t, s = inventory(T_INV), inventory(S_INV)
        code, out = self._run(self._all(t, self.BASE), self._all(s, self.BASE.replace("libc6:amd64", "libc6")))
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[-1], "PASS")
        self.assertEqual(out.count("문제 0"), 5)
        cases = {
            "판이 다름 zstd T 1.5.5+dfsg2-2build1.1 S 1.5.6": self.BASE.replace("zstd 1.5.5+dfsg2-2build1.1", "zstd 1.5.6"),
            "T 에만 있음 zstd": self.BASE.replace("ii  zstd 1.5.5+dfsg2-2build1.1\n", ""),
            "S 에만 있음 lldpd 1.0.18": self.BASE + "ii  lldpd 1.0.18\n",
            # 설정 파일만 남은 줄(rc)은 설치된 것으로 보지 않는다. 그래서 T 에만 설치된 것으로 잡힌다.
            "T 에만 있음 zstd 1.5.5": self.BASE.replace("ii  zstd", "rc  zstd"),
        }
        for expected, s_text in cases.items():
            with self.subTest(case=expected):
                code, out = self._run(self._all(t, self.BASE), self._all(s, self.BASE, dbsvr01=s_text))
                self.assertEqual(code, 1)
                self.assertIn(f"FAIL db0 {expected}", out)
                self.assertEqual(out.splitlines()[-1], "FAIL")
                self.assertEqual(out.count("문제 0"), 4)

    def test_named_t_only_step_packages_are_noted_not_failed(self) -> None:
        # T 에만 도는 단계(lldpd)가 깐 것과 올린 것. 이름을 준 것만 넘기고 기록은 남긴다. S 에만 있는 것은 이름을 줘도 실패다.
        t, s = inventory(T_INV), inventory(S_INV)
        t_text = self.BASE.replace("zstd 1.5.5+dfsg2-2build1.1", "zstd 1.5.6") + "ii  lldpd 1.0.18\n"
        code, out = self._run(self._all(t, t_text), self._all(s, self.BASE),
                              "--allow-t-only", "lldpd", "--allow-version", "zstd")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count("NOTE "), 10)
        self.assertIn("NOTE web0 T 에만 있음 lldpd 1.0.18", out)
        code, out = self._run(self._all(t, self.BASE), self._all(s, self.BASE + "ii  lldpd 1.0.18\n"), "--allow-t-only", "lldpd")
        self.assertEqual(code, 1)
        code, out = self._run(self._all(t, t_text), self._all(s, self.BASE), "--allow-t-only", "lldpd")
        self.assertEqual(code, 1)
        self.assertIn("FAIL web0 판이 다름 zstd", out)

    def test_missing_or_malformed_lists_fail(self) -> None:
        t, s = inventory(T_INV), inventory(S_INV)
        lists = self._all(s, self.BASE)
        del lists["wassvr02"]
        code, out = self._run(self._all(t, self.BASE), lists)
        self.assertEqual(code, 1)
        self.assertIn("FAIL was1 was02 wassvr02 목록을 읽지 못함", out)
        # 상태 칸 없는 옛 꼴, 칸 수가 다름, 같은 이름 두 번.
        for bad in ("chrony 4.5\n", "ii chrony\n", "ii  chrony 1\nrc  chrony 2\n"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                pairpkg.parse(bad)

    def test_non_installed_lines_must_match_on_both_sides(self) -> None:
        t, s = inventory(T_INV), inventory(S_INV)
        rc = self.BASE + "rc  unattended-upgrades 2.9.1+nmu4ubuntu1\n"
        code, out = self._run(self._all(t, rc), self._all(s, rc))
        self.assertEqual(code, 0, out)
        code, out = self._run(self._all(t, rc), self._all(s, self.BASE, dbsvr01=rc.replace("rc  unattended", "ii  unattended")))
        self.assertEqual(code, 1)
        self.assertIn("FAIL db0 설치 상태가 ii 가 아닌 줄이 다름 unattended-upgrades T rc 2.9.1+nmu4ubuntu1 S 없음", out)
        self.assertIn("FAIL db0 S 에만 있음 unattended-upgrades", out)

    def test_an_s_server_without_a_t_twin_fails(self) -> None:
        t, s = inventory(T_INV), inventory(S_INV)
        extra = json.loads(json.dumps(s))
        extra["hosts"].append(dict(extra["hosts"][1], name="wassvr03", vmid=216))
        self.assertEqual(pairpkg.unpaired_s(t, extra), [("was2", "wassvr03")])
        self.assertEqual(pairpkg.unpaired_s(t, s), [])
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "s.json"
            path.write_text(json.dumps(extra))
            root = Path(raw)
            for side, inv in (("t", t), ("s", extra)):
                (root / side).mkdir()
                for host in inv["hosts"]:
                    (root / side / f"{host['name']}.txt").write_text(self.BASE)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = pairpkg.main(["--t-inventory", str(T_INV), "--s-inventory", str(path),
                                     "--t-dir", str(root / "t"), "--s-dir", str(root / "s")])
        self.assertEqual(code, 1)
        self.assertIn("FAIL was2 wassvr03 짝 T 서버가 없다", out.getvalue())


class StepScriptTests(unittest.TestCase):
    def test_every_step_script_parses(self) -> None:
        for path in sorted((DEPLOY / "steps").glob("*.sh")) + [DEPLOY / "install-host.sh", DEPLOY / "apply.sh",
                                                               DOMAIN / "loadgen" / "entrypoint.sh"]:
            with self.subTest(path=path.name):
                done = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
                self.assertEqual(done.returncode, 0, done.stderr)

    def test_place_files_fills_secrets_and_refuses_missing_ones(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = Path(raw)
            (bundle / "files").mkdir()
            (bundle / "secrets").mkdir()
            (bundle / "files" / "a.conf").write_text("password=@@SECRET:db_password@@\n")
            (bundle / "secrets" / "db_password").write_text("Abc123def456ghi7\n")
            dest = bundle / "out" / "a.conf"
            group = subprocess.run(["id", "-gn"], capture_output=True, text=True, check=True).stdout.strip()
            user = subprocess.run(["id", "-un"], capture_output=True, text=True, check=True).stdout.strip()
            (bundle / "files.tsv").write_text(f"s1\t{dest}\t0600\t{user}\t{group}\tfiles/a.conf\n")
            script = f"set -e; BUNDLE={bundle}; . {DEPLOY / 'steps' / 'lib.sh'}; place_files s1"
            done = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(dest.read_text(), "password=Abc123def456ghi7\n")
            self.assertEqual(oct(dest.stat().st_mode & 0o777), "0o600")
            (bundle / "secrets" / "db_password").unlink()
            done = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("비밀값 파일 없음", done.stderr + done.stdout)

    def test_place_files_fills_nic_macs_and_refuses_a_missing_one(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = Path(raw)
            (bundle / "files").mkdir()
            (bundle / "files" / "n.yaml").write_text('a: "@@MAC:ens18@@"\nb: "@@MAC:ens19@@"\n')
            (bundle / "files" / "other.conf").write_text("x\n")
            macs = bundle / "nic-macs"
            macs.write_text("ens18 bc:24:11:16:6f:5a\nens19 bc:24:11:91:27:63\n")
            dest = bundle / "out" / "n.yaml"
            other = bundle / "out" / "other.conf"
            group = subprocess.run(["id", "-gn"], capture_output=True, text=True, check=True).stdout.strip()
            user = subprocess.run(["id", "-un"], capture_output=True, text=True, check=True).stdout.strip()
            (bundle / "files.tsv").write_text(f"s1\t{dest}\t0600\t{user}\t{group}\tfiles/n.yaml\n"
                                              f"s1\t{other}\t0644\t{user}\t{group}\tfiles/other.conf\n")
            script = f"set -e; BUNDLE={bundle}; NIC_MAC_FILE={macs}; . {DEPLOY / 'steps' / 'lib.sh'}; place_files s1 {dest}"
            done = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(dest.read_text(), 'a: "bc:24:11:16:6f:5a"\nb: "bc:24:11:91:27:63"\n')
            self.assertFalse(other.exists(), "경로를 주면 그 파일 하나만 놓는다")
            dest.unlink()
            macs.write_text("ens18 bc:24:11:16:6f:5a\n")
            # || 뒤에서 불려도(set -e 가 꺼져도) 실패를 돌려주고 파일을 놓지 않는다.
            script = (f"BUNDLE={bundle}; NIC_MAC_FILE={macs}; . {DEPLOY / 'steps' / 'lib.sh'}; "
                      f"place_files s1 {dest} || {{ echo refused; exit 7; }}")
            done = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
            self.assertEqual(done.returncode, 7, done.stdout + done.stderr)
            self.assertIn("NIC MAC 없음: ens19", done.stderr)
            self.assertFalse(dest.exists())
            self.assertFalse(Path(f"{dest}.new").exists())

    def _rsyslog_tree(self, root: Path, rule: str = "new rule\n", default: str = "default rules\n",
                      remote: str = "rsyslog.d/remote") -> tuple[dict, Path, Path]:
        """지금 설정(old main, 50-default, old rule)이 있는 가짜 /etc 와 syslog, syslog-forward 단계 묶음.
        remote 는 전달 설정을 두고 rsyslog.conf 끝에서 포함하는 곳(옛 자리 rsyslog.remote.d 는 AppArmor 범위 밖)."""
        etc = root / "etc"
        (etc / "rsyslog.d").mkdir(parents=True)
        includes = f'$IncludeConfig {etc}/rsyslog.d/*.conf\ninclude(file="{etc}/{remote}/*.conf" mode="optional")\n'
        (etc / "rsyslog.conf").write_text("old main\n" + includes)
        (etc / "rsyslog.d" / "50-default.conf").write_text(default)
        (etc / "rsyslog.d" / "00-opsjobs.conf").write_text("old rule\n")
        (etc / "rsyslog.d" / "00-opsjobs.conf").chmod(0o640)
        bundle = root / "bundle"
        (bundle / "files").mkdir(parents=True)
        (bundle / "files" / "rsyslog.conf").write_text("new main\n" + includes)
        (bundle / "files" / "00-opsjobs.conf").write_text(rule)
        (bundle / "files" / "logrotate").write_text("rotate\n")
        (bundle / "files" / "10-forward.conf").write_text("forward\n")
        (bundle / "files" / "annotate-proc").write_text("deny @{PROC}/[0-9]*/comm r,\n")
        apparmor = root / "apparmor.d"
        (apparmor / "rsyslog.d").mkdir(parents=True)
        (apparmor / "usr.sbin.rsyslogd").write_text("profile rsyslogd {\n  include if exists <rsyslog.d>\n}\n")
        (root / "aafs").mkdir()
        user = subprocess.run(["id", "-un"], capture_output=True, text=True, check=True).stdout.strip()
        group = subprocess.run(["id", "-gn"], capture_output=True, text=True, check=True).stdout.strip()
        rows = [("syslog", etc / "rsyslog.conf", "rsyslog.conf"), ("syslog", etc / "rsyslog.d" / "00-opsjobs.conf", "00-opsjobs.conf"),
                ("syslog", etc / "logrotate.d" / "opsjobs", "logrotate"),
                ("syslog", apparmor / "rsyslog.d" / "annotate-proc", "annotate-proc"),
                ("syslog-forward", etc / remote / "10-forward.conf", "10-forward.conf")]
        (bundle / "files.tsv").write_text("".join(f"{step}\t{dest}\t0644\t{user}\t{group}\tfiles/{src}\n" for step, dest, src in rows))
        bin_dir = root / "bin"
        bin_dir.mkdir()
        (bin_dir / "rsyslogd").write_text(RSYSLOGD_STUB)
        calls = root / "calls"
        log_dir = root / "log"
        log_dir.mkdir()
        (log_dir / "auth.log").write_text("")
        stubs = {
            "systemctl": f'#!/bin/sh\necho systemctl "$@" >> {calls}\n[ "$1" = show ] && echo "Wed 2026-11-18 05:00:00 UTC"\nexit 0\n',
            # 프로필 다시 읽기 대역: FAKE_AA_FAIL=1 이면 조각에 deny 줄이 있을 때 실패한다(되돌린 뒤 다시 읽기는 통과).
            "apparmor_parser": (f'#!/bin/sh\necho apparmor_parser "$@" >> {calls}\n'
                                f'if [ "$FAKE_AA_FAIL" = 1 ] && grep -qs deny {apparmor}/rsyslog.d/annotate-proc; then echo "parser error" >&2; exit 1; fi\n'),
            # 커널, 감사 기록 대역: FAKE_JOURNAL 파일 내용을 낸다. FAKE_JOURNAL_FAIL=1 이면 실패.
            "journalctl": f'#!/bin/sh\necho journalctl "$@" >> {calls}\n[ "$FAKE_JOURNAL_FAIL" = 1 ] && exit 1\ncat "$FAKE_JOURNAL" 2>/dev/null\nexit 0\n',
            "chown": f'#!/bin/sh\necho chown "$@" >> {calls}\n',
            # 실행기 계정으로 바꾼 셸에서 부른 logger 를 알 수 있게 표시를 넘긴다.
            "setpriv": f'#!/bin/sh\necho setpriv "$@" >> {calls}\nwhile [ "${{1#--}}" != "$1" ]; do shift; done\nSTUB_AS_EXECUTOR=1 exec "$@"\n',
            # 첫 규칙 대역: 실행기로 보낸 줄은 opsjobs.log 로(FAKE_UID_RULE=broken 이면 걸리지 않고 syslog 로), 나머지는 syslog 로.
            "logger": (f'#!/bin/sh\necho logger "$@" >> {calls}\ntag=x\nwhile [ $# -gt 1 ]; do case "$1" in -t) tag=$2; shift 2;; --) shift;; *) shift;; esac; done\n'
                       f'out={log_dir}/syslog\n[ -n "$STUB_AS_EXECUTOR" ] && [ "$FAKE_UID_RULE" != broken ] && out={log_dir}/opsjobs.log\n'
                       f'echo "host $tag: $1" >> "$out"\n'),
        }
        for tool, text in stubs.items():
            (bin_dir / tool).write_text(text)
        for tool in ("rsyslogd", *stubs):
            (bin_dir / tool).chmod(0o755)
        env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "STUB_LOG": str(calls), "RSYSLOG_ETC": str(etc),
               "RSYSLOG_LOG_DIR": str(log_dir), "EXECUTOR_NAME": "opsjob", "APPARMOR_ETC": str(apparmor),
               "APPARMOR_FS": str(root / "aafs"), "FAKE_JOURNAL": str(root / "journal"),
               # 줄이 안 오는 경우는 다시 읽기 상한까지 기다리므로 시험에서는 짧게 둔다(설치 기본은 30초).
               "SETTLE_SEC": "2", "SETTLE_EVERY": "0.2"}
        return env, etc, bundle

    def _run_step(self, bundle: Path, step: str, env: dict) -> subprocess.CompletedProcess:
        script = f"set -euo pipefail; BUNDLE={bundle}; . {DEPLOY / 'steps' / 'lib.sh'}; . {DEPLOY / 'steps' / (step + '.sh')}"
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)

    @staticmethod
    def _rsyslogd_calls(root: Path) -> list[tuple[str, list[str]]]:
        """대역 rsyslogd 의 부름마다 (설정 경로, 읽은 파일과 'denied <대상>')."""
        found = []
        for line in (root / "calls").read_text().splitlines():
            if line.startswith("rsyslogd -f "):
                conf, _, rest = line[len("rsyslogd -f "):].partition(" ")
                found.append((conf, rest.split()[1:] if rest.startswith("read") else [rest]))
        return found

    def _assert_no_check_dir(self, etc: Path) -> None:
        self.assertEqual(sorted(p.name for p in (etc / "rsyslog.d").iterdir() if p.name.startswith(".")), [])

    def test_syslog_step_leaves_etc_alone_when_the_new_config_does_not_parse(self) -> None:
        # 새 규칙이 틀린 경우와 지금 설정(패키지 파일)이 틀린 경우 모두, 놓기 전 검사가 설정 전체를 읽고 멈춘다.
        for rule, default in (("BAD rule\n", "default rules\n"), ("new rule\n", "BAD default\n")):
            with self.subTest(rule=rule, default=default), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                env, etc, bundle = self._rsyslog_tree(root, rule, default)
                done = self._run_step(bundle, "syslog", env)
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("rsyslog 설정 검사(놓기 전, 단계 syslog)", done.stderr)
                self.assertIn("error during parsing file", done.stderr)
                self.assertEqual((etc / "rsyslog.d" / "00-opsjobs.conf").read_text(), "old rule\n")
                self.assertTrue((etc / "rsyslog.conf").read_text().startswith("old main\n"))
                self.assertFalse((etc / "logrotate.d").exists())
                calls = self._rsyslogd_calls(root)
                self.assertEqual(len(calls), 1, calls)
                conf, read = calls[0]
                check = f"{etc}/rsyslog.d/.check-"
                self.assertTrue(conf.startswith(check), conf)
                self.assertTrue(all(path.startswith(check) for path in read), read)
                for name in ("rsyslog.d/00-opsjobs.conf", "rsyslog.d/50-default.conf"):
                    self.assertTrue(any(path.endswith(f"{etc}/{name}") for path in read), (name, read))
                self.assertNotIn("systemctl restart rsyslog", (root / "calls").read_text())
                self._assert_no_check_dir(etc)

    def test_rsyslog_forward_step_checks_a_copy_first_then_the_placed_config_then_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env, etc, bundle = self._rsyslog_tree(root)
            done = self._run_step(bundle, "syslog-forward", env)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual((etc / "rsyslog.d" / "remote" / "10-forward.conf").read_text(), "forward\n")
            self.assertEqual((etc / "rsyslog.d" / "00-opsjobs.conf").read_text(), "old rule\n")
            calls = self._rsyslogd_calls(root)
            self.assertEqual(len(calls), 2, calls)
            self.assertTrue(calls[0][0].startswith(f"{etc}/rsyslog.d/.check-"), calls[0])
            self.assertTrue(calls[0][1][-1].startswith(f"{etc}/rsyslog.d/.check-"), calls[0])
            self.assertTrue(calls[0][1][-1].endswith("/rsyslog.d/remote/10-forward.conf"), calls[0])
            self.assertEqual(calls[1], (f"{etc}/rsyslog.conf", [f"{etc}/rsyslog.conf", f"{etc}/rsyslog.d/00-opsjobs.conf",
                                                                f"{etc}/rsyslog.d/50-default.conf",
                                                                f"{etc}/rsyslog.d/remote/10-forward.conf"]))
            self.assertEqual((root / "calls").read_text().splitlines()[-1], "systemctl restart rsyslog")
            self._assert_no_check_dir(etc)

    def test_rsyslog_forward_step_refuses_a_forward_file_rsyslog_cannot_read(self) -> None:
        # 옛 자리(/etc/rsyslog.remote.d)는 AppArmor 범위 밖이다. 포함이 optional 이라 -N1 은 통과하지만 파일을 읽지 않으므로
        # 놓기 전에 멈추고 /etc 를 바꾸지 않는다.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env, etc, bundle = self._rsyslog_tree(root, remote="rsyslog.remote.d")
            done = self._run_step(bundle, "syslog-forward", env)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("rsyslog 가 읽지 않은 파일:", done.stderr)
            self.assertIn("rsyslog 설정 검사(놓기 전, 단계 syslog-forward)", done.stderr)
            self.assertFalse((etc / "rsyslog.remote.d" / "10-forward.conf").exists())
            calls = self._rsyslogd_calls(root)
            self.assertEqual(len(calls), 1, calls)
            self.assertIn(f"denied {etc}/rsyslog.remote.d/*.conf", " ".join(calls[0][1]))
            self.assertNotIn("systemctl", (root / "calls").read_text())
            self._assert_no_check_dir(etc)

    def test_rsyslog_steps_put_back_what_they_placed_when_the_placed_config_fails(self) -> None:
        for step in ("syslog", "syslog-forward"):
            with self.subTest(step=step), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                env, etc, bundle = self._rsyslog_tree(root)
                env["FAKE_FAIL_CONF"] = str(etc / "rsyslog.conf")
                done = self._run_step(bundle, step, env)
                self.assertNotEqual(done.returncode, 0)
                self.assertIn(f"rsyslog 설정 검사(놓은 뒤, 단계 {step}). 놓은 파일을 앞 내용으로 되돌림", done.stderr)
                self.assertIn("error after placing", done.stderr)
                self.assertEqual((etc / "rsyslog.d" / "00-opsjobs.conf").read_text(), "old rule\n")
                self.assertEqual(oct((etc / "rsyslog.d" / "00-opsjobs.conf").stat().st_mode & 0o777), "0o640")
                self.assertTrue((etc / "rsyslog.conf").read_text().startswith("old main\n"))
                self.assertFalse((etc / "logrotate.d" / "opsjobs").exists())
                self.assertFalse((etc / "rsyslog.d" / "remote" / "10-forward.conf").exists())
                self.assertEqual(sorted(p.name for p in etc.rglob("*.new")), [])
                self.assertEqual([conf for conf, _ in self._rsyslogd_calls(root)][1:], [f"{etc}/rsyslog.conf"])
                self.assertNotIn("systemctl restart rsyslog", (root / "calls").read_text())
                self._assert_no_check_dir(etc)

    def test_rsyslog_put_back_failure_is_named(self) -> None:
        # 되돌리지 못한 파일은 "되돌림" 이라 하지 않고 그 경로를 적는다.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env, etc, bundle = self._rsyslog_tree(root)
            env["FAKE_FAIL_CONF"] = str(etc / "rsyslog.conf")
            env["FAKE_LOCK_DIR"] = str(etc / "logrotate.d")
            try:
                done = self._run_step(bundle, "syslog", env)
            finally:
                (etc / "logrotate.d").chmod(0o755)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn(f"rsyslog 설정 검사(놓은 뒤, 단계 syslog). 되돌리지 못한 파일: {etc}/logrotate.d/opsjobs", done.stderr)
            self.assertNotIn("놓은 파일을 앞 내용으로 되돌림", done.stderr)
            self.assertEqual((etc / "rsyslog.d" / "00-opsjobs.conf").read_text(), "old rule\n")

    def test_rsyslog_step_that_cannot_place_says_so_and_puts_back(self) -> None:
        # 진짜 놓기가 실패하면 놓기 실패로 알리고(앞 검사의 성공 출력을 오류처럼 내지 않는다) 놓은 파일을 되돌린다.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env, etc, bundle = self._rsyslog_tree(root)
            (etc / "logrotate.d").write_text("not a directory\n")  # 사본에는 놓이고 진짜 자리에는 못 놓인다
            done = self._run_step(bundle, "syslog", env)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("단계 syslog 파일을 놓지 못함. 놓은 파일을 앞 내용으로 되돌림", done.stderr)
            self.assertNotIn("rsyslogd:", done.stderr)
            self.assertNotIn("pushed config fragment", done.stdout + done.stderr)
            self.assertEqual((etc / "rsyslog.d" / "00-opsjobs.conf").read_text(), "old rule\n")
            self.assertTrue((etc / "rsyslog.conf").read_text().startswith("old main\n"))
            self.assertEqual(len(self._rsyslogd_calls(root)), 1)
            self._assert_no_check_dir(etc)

    def test_rsyslog_check_copy_left_by_a_cut_run_is_removed_first(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env, etc, bundle = self._rsyslog_tree(root)
            (etc / "rsyslog.d" / ".check-1" / "keep").mkdir(parents=True)
            done = self._run_step(bundle, "syslog-forward", env)
            self.assertEqual(done.returncode, 0, done.stderr)
            self._assert_no_check_dir(etc)

    def test_syslog_step_sends_an_executor_uid_line_through_the_first_rule(self) -> None:
        for rule, ok in (("works", True), ("broken", False)):
            with self.subTest(rule=rule), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                env, etc, bundle = self._rsyslog_tree(root)
                env["FAKE_UID_RULE"] = rule
                done = self._run_step(bundle, "syslog", env)
                calls = (root / "calls").read_text()
                self.assertIn("setpriv --reuid=opsjob --regid=opsjob --init-groups logger -t rsyslog-check -- uid route check", calls)
                self.assertLess(calls.index("systemctl restart rsyslog"), calls.index("setpriv"))
                if ok:
                    self.assertEqual(done.returncode, 0, done.stderr)
                    self.assertIn("rsyslog ok", done.stdout)
                    self.assertIn("uid route check", (root / "log" / "opsjobs.log").read_text())
                else:
                    self.assertNotEqual(done.returncode, 0)
                    self.assertIn("실행기 uid 줄이", done.stderr)
                    self.assertIn("opsjobs.log 에 없음", done.stderr)

    def test_syslog_step_skips_the_check_lines_once_forwarding_is_on(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env, etc, bundle = self._rsyslog_tree(root)
            (etc / "rsyslog.d" / "remote").mkdir()
            (etc / "rsyslog.d" / "remote" / "10-forward.conf").write_text("forward\n")
            done = self._run_step(bundle, "syslog", env)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("전달이 켜져 있어 검사 줄 생략", done.stdout)
            calls = (root / "calls").read_text()
            self.assertNotIn("logger", calls)
            self.assertNotIn("setpriv", calls)

    def test_rendered_rsyslog_config_stays_inside_the_apparmor_read_scope(self) -> None:
        # Ubuntu 의 rsyslogd 프로필은 /etc/rsyslog.conf 와 /etc/rsyslog.d/** 만 읽게 둔다. 놓는 파일과 포함 대상이 그 안이어야
        # 하고, 전달 설정은 rsyslog.d/*.conf 포함에 들지 않는 하위 디렉터리에서 rsyslog.conf 끝 포함으로 모든 로컬 규칙 뒤에 읽힌다.
        inside = lambda path: path == "/etc/rsyslog.conf" or path.startswith("/etc/rsyslog.d/")  # noqa: E731
        for path in (T_INV, S_INV):
            for bundle in bundles(path).values():
                dests = [e["dest"] for e in bundle["files"] if e["dest"].startswith(RSYSLOG_DESTS)]
                conf = file_of(bundle, "/etc/rsyslog.conf")
                targets = re.findall(r'^\$IncludeConfig (\S+)|include\(file="([^"]+)"', conf, re.M)
                targets = [a or b for a, b in targets]
                with self.subTest(host=bundle["host"]):
                    self.assertEqual([d for d in dests + targets if not inside(d)], [])
                    self.assertEqual(targets, ["/etc/rsyslog.d/*.conf", "/etc/rsyslog.d/remote/*.conf"])
                    for dest in dests:
                        self.assertTrue(dest == "/etc/rsyslog.conf" or any(fnmatch.fnmatch(dest, t) for t in targets), dest)
                    forward = [d for d in dests if d.startswith("/etc/rsyslog.d/remote/")]
                    self.assertEqual(forward, ["/etc/rsyslog.d/remote/10-forward.conf"] if "syslog-forward" in bundle["steps"] else [])

    def test_verify_reads_the_rsyslog_config_rather_than_seeing_files(self) -> None:
        source = (DEPLOY / "steps" / "verify.sh").read_text()
        first = source[source.index("first_rule_present()"):source.index("forward_state_ok()")]
        forward = source[source.index("forward_state_ok()"):source.index("rsyslog_not_denied()")]
        denied = source[source.index("rsyslog_not_denied()"):source.index("ip_forward_ok()")]
        self.assertIn('rsyslog_reads "$RSYSLOG_ETC/rsyslog.conf" "$RSYSLOG_ETC/rsyslog.d/00-opsjobs.conf"', first)
        self.assertIn('rsyslog_reads "$RSYSLOG_ETC/rsyslog.conf" "$forward"', forward)
        self.assertIn('forward="$RSYSLOG_ETC/rsyslog.d/remote/10-forward.conf"', forward)
        self.assertIn("rsyslog_denials", denied)
        self.assertIn("check rsyslog-no-apparmor-denial rsyslog_not_denied", source)

    def test_rsyslog_reads_names_a_file_the_parser_did_not_open(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env, etc, bundle = self._rsyslog_tree(root, remote="rsyslog.remote.d")
            (etc / "rsyslog.remote.d").mkdir()
            (etc / "rsyslog.remote.d" / "10-forward.conf").write_text("forward\n")
            script = (f"set -euo pipefail; . {DEPLOY / 'steps' / 'lib.sh'}; "
                      f"rsyslog_reads {etc}/rsyslog.conf {etc}/rsyslog.d/00-opsjobs.conf && echo read-ok; "
                      f"rsyslog_reads {etc}/rsyslog.conf {etc}/rsyslog.remote.d/10-forward.conf || echo not-read")
            done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(done.stdout.split(), ["read-ok", "not-read"])
            self.assertIn(f"rsyslog 가 읽지 않은 파일: {etc}/rsyslog.remote.d/10-forward.conf", done.stderr)

    PROC_DENIAL_LINE = ('audit: type=1400 audit(1.0:1): apparmor="DENIED" operation="open" class="file" profile="rsyslogd" '
                        'name="/proc/1234/cmdline" pid=900 comm="in:imuxsock" requested_mask="r" denied_mask="r"')
    PTRACE_DENIAL_LINE = ('audit: type=1400 audit(1.0:2): apparmor="DENIED" operation="ptrace" class="ptrace" profile="rsyslogd" '
                          'pid=900 comm="in:imuxsock" requested_mask="read" denied_mask="read" peer="unconfined"')
    CONF_DENIAL_LINE = ('audit: type=1400 audit(1.0:3): apparmor="DENIED" operation="open" class="file" profile="rsyslogd" '
                        'name="/etc/rsyslog.remote.d/" pid=900 comm="rsyslogd" requested_mask="r" denied_mask="r"')

    def test_syslog_step_places_the_apparmor_fragment_and_reloads_before_restart(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env, etc, bundle = self._rsyslog_tree(root)
            done = self._run_step(bundle, "syslog", env)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual((root / "apparmor.d" / "rsyslog.d" / "annotate-proc").read_text(), "deny @{PROC}/[0-9]*/comm r,\n")
            calls = (root / "calls").read_text().splitlines()
            reload = f"apparmor_parser -r {root / 'apparmor.d' / 'usr.sbin.rsyslogd'}"
            self.assertEqual(calls.count(reload), 1, calls)
            self.assertLess(calls.index(reload), calls.index("systemctl restart rsyslog"))
            self.assertIn("apparmor proc denials 0", done.stdout)
            self.assertTrue(any(c.startswith("journalctl ") and "_TRANSPORT=kernel + _TRANSPORT=audit" in c for c in calls), calls)

    def test_apparmor_reload_failure_puts_the_fragment_back_and_reloads_again(self) -> None:
        for before in (None, "old fragment\n"):
            with self.subTest(before=before), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                env, etc, bundle = self._rsyslog_tree(root)
                fragment = root / "apparmor.d" / "rsyslog.d" / "annotate-proc"
                if before:
                    fragment.write_text(before)
                env["FAKE_AA_FAIL"] = "1"
                done = self._run_step(bundle, "syslog", env)
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("AppArmor 프로필 다시 읽기 실패(단계 syslog). 조각을 앞 상태로 되돌림", done.stderr)
                self.assertIn("parser error", done.stderr)
                if before:
                    self.assertEqual(fragment.read_text(), before)
                else:
                    self.assertFalse(fragment.exists())
                calls = (root / "calls").read_text().splitlines()
                self.assertEqual(sum(c.startswith("apparmor_parser -r ") for c in calls), 2, calls)
                self.assertEqual(self._rsyslogd_calls(root), [])
                self.assertNotIn("systemctl restart rsyslog", calls)
                self.assertEqual((etc / "rsyslog.d" / "00-opsjobs.conf").read_text(), "old rule\n")

    def test_apparmor_off_places_the_fragment_without_reloading(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env, etc, bundle = self._rsyslog_tree(root)
            (root / "aafs").rmdir()
            done = self._run_step(bundle, "syslog", env)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertTrue((root / "apparmor.d" / "rsyslog.d" / "annotate-proc").exists())
            self.assertNotIn("apparmor_parser", (root / "calls").read_text())

    def test_syslog_step_fails_when_proc_reads_are_still_audited(self) -> None:
        for line, ok in ((self.PROC_DENIAL_LINE, False), (self.PTRACE_DENIAL_LINE, False), (self.CONF_DENIAL_LINE, True), ("", True)):
            with self.subTest(line=line[:60]), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                env, etc, bundle = self._rsyslog_tree(root)
                (root / "journal").write_text(line + "\n")
                done = self._run_step(bundle, "syslog", env)
                if ok:
                    self.assertEqual(done.returncode, 0, done.stderr)
                else:
                    self.assertNotEqual(done.returncode, 0)
                    self.assertIn("rsyslogd 의 /proc 읽기 거부가 감사됨(AppArmor 조각이 안 먹음)", done.stderr)

    def test_rsyslog_denials_reads_since_the_rsyslog_start_and_never_hides_a_failed_read(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env, etc, bundle = self._rsyslog_tree(root)
            (root / "journal").write_text("\n".join([self.PROC_DENIAL_LINE, self.CONF_DENIAL_LINE,
                                                     'apparmor="DENIED" operation="open" profile="snapd" name="/proc/1/cmdline"']) + "\n")
            lib = DEPLOY / "steps" / "lib.sh"
            script = (f'set -euo pipefail; . {lib}; all=$(rsyslog_denials); proc=$(rsyslog_denials "$RSYSLOG_PROC_DENIAL"); '
                      f'echo "all=$(wc -l <<<"$all") proc=$(wc -l <<<"$proc")"; grep -c cmdline <<<"$proc"')
            done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(done.stdout.split(), ["all=2", "proc=1", "1"])
            since = int(subprocess.run(["date", "-d", "Wed 2026-11-18 05:00:00 UTC", "+%s"], capture_output=True, text=True).stdout)
            self.assertIn(f"journalctl -q --no-pager --since @{since} _TRANSPORT=kernel + _TRANSPORT=audit",
                          (root / "calls").read_text())
            env["FAKE_JOURNAL_FAIL"] = "1"
            done = subprocess.run(["bash", "-c", f". {lib}; rsyslog_denials && echo read || echo unread"],
                                  capture_output=True, text=True, env=env)
            self.assertEqual(done.stdout.strip(), "unread")

    def test_apparmor_fragment_only_denies_the_annotation_reads(self) -> None:
        for path in (T_INV, S_INV):
            for bundle in bundles(path).values():
                text = file_of(bundle, "/etc/apparmor.d/rsyslog.d/annotate-proc")
                rules = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]
                with self.subTest(host=bundle["host"]):
                    self.assertEqual(rules, ["deny @{PROC}/[0-9]*/comm r,", "deny @{PROC}/[0-9]*/cmdline r,",
                                             "deny @{PROC}/[0-9]*/exe r,", "deny ptrace (read),"])
                    self.assertEqual([e["step"] for e in bundle["files"] if e["dest"].startswith("/etc/apparmor.d/")], ["syslog"])
        source = (DEPLOY / "steps" / "syslog.sh").read_text()
        self.assertLess(source.index('apparmor_place_checked syslog "$APPARMOR_ETC/rsyslog.d/annotate-proc" "$APPARMOR_ETC/usr.sbin.rsyslogd"'),
                        source.index("systemctl restart rsyslog"))
        self.assertLess(source.index("systemctl restart rsyslog"), source.index('rsyslog_denials "$RSYSLOG_PROC_DENIAL"'))
        verify = (DEPLOY / "steps" / "verify.sh").read_text()
        self.assertIn("check rsyslog-no-proc-audit rsyslog_no_proc_denial", verify)
        self.assertIn('rsyslog_no_proc_denial() { local out; out=$(rsyslog_denials "$RSYSLOG_PROC_DENIAL") && [[ -z "$out" ]]; }', verify)

    def test_every_step_that_places_or_restarts_rsyslog_checks_before_placing(self) -> None:
        placing = set()
        for path in (T_INV, S_INV):
            for bundle in bundles(path).values():
                placing |= {entry["step"] for entry in bundle["files"] if entry["dest"].startswith(RSYSLOG_DESTS)}
        restarting = {p.stem for p in (DEPLOY / "steps").glob("*.sh") if "restart rsyslog" in p.read_text()}
        self.assertEqual(placing, {"syslog", "syslog-forward"})
        self.assertEqual(restarting, placing)
        for step in sorted(placing):
            source = (DEPLOY / "steps" / f"{step}.sh").read_text()
            with self.subTest(step=step):
                self.assertIn(f"rsyslog_place_checked {step}\n", source)
                self.assertLess(source.index("rsyslog_place_checked"), source.index("systemctl restart rsyslog"))
                self.assertNotRegex(source, r"(?m)^\s*place_files")
                self.assertNotIn("rsyslogd -N1", source)

    def test_scripts_with_a_shebang_are_executable_in_git(self) -> None:
        # runbook 은 deploy/apply.sh 를 바로 부른다. 저장소에서 실행 비트가 없으면 Permission denied(rc 126)로 아무것도 못 한다.
        listed = subprocess.run(["git", "-C", str(REPO), "ls-files", "-s", "--", "civil-petition/deploy"],
                                capture_output=True, text=True, check=True).stdout
        modes = {line.split("\t", 1)[1]: line.split()[0] for line in listed.splitlines()}
        wanted = {path for path in modes
                  if not path.startswith("civil-petition/deploy/templates/") and (REPO / path).read_bytes()[:2] == b"#!"}
        self.assertTrue({"civil-petition/deploy/apply.sh", "civil-petition/deploy/install-host.sh",
                         "civil-petition/deploy/tools/rsyslog-check.sh"} <= wanted, wanted)
        self.assertEqual({path: modes[path] for path in sorted(wanted) if modes[path] != "100755"}, {})

    def _stub_dir(self, root: Path) -> Path:
        bin_dir = root / "bin"
        bin_dir.mkdir()
        for name, text in (("systemctl", SYSTEMCTL_STUB), ("systemd-run", SYSTEMD_RUN_STUB)):
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)
        return bin_dir

    def test_network_step_refuses_while_an_earlier_rollback_waits_runs_or_failed(self) -> None:
        for state, words in (("pending", "타이머가 기다리는 중"), ("activating", "되돌림이 도는 중"),
                             ("active", "되돌림이 도는 중"), ("failed", "journalctl -u netplan-rollback")):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                bin_dir = self._stub_dir(root)
                (root / "cloud" / "cloud.cfg.d").mkdir(parents=True)
                (root / "cloud" / "cloud.cfg.d" / "99-disable-network-config.cfg").write_text("network: {config: disabled}\n")
                (root / "cloud" / "cloud-init.disabled").write_text("")
                (root / "state" / "netplan-before").mkdir(parents=True)
                (root / "state" / "netplan-before" / "50-cloud-init.yaml").write_text("saved\n")
                armed = root / "armed"
                if state == "pending":
                    armed.write_text("x")
                else:
                    Path(f"{armed}.service").write_text(state + "\n")
                env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "STUB_LOG": str(root / "calls"), "STUB_ARMED": str(armed),
                       "STATE_DIR": str(root / "state"), "CLOUD_DIR": str(root / "cloud"),
                       "NETPLAN_DIR": str(root / "netplan"), "NIC_MAC_FILE": str(root / "state" / "nic-macs")}
                script = (f"set -euo pipefail; BUNDLE={root}; . {DEPLOY / 'steps' / 'lib.sh'}; "
                          f". {DEPLOY / 'steps' / 'network.sh'}")
                done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
                self.assertNotEqual(done.returncode, 0)
                self.assertIn(words, done.stderr)
                calls = (root / "calls").read_text() if (root / "calls").exists() else ""
                self.assertNotIn("systemd-run", calls)
                self.assertNotIn("systemctl stop", calls)
                self.assertNotIn("reset-failed", calls)
                self.assertEqual((root / "state" / "netplan-before" / "50-cloud-init.yaml").read_text(), "saved\n")
                self.assertFalse((root / "state" / "nic-macs").exists())

    def test_network_step_refuses_a_rollback_timer_shorter_than_apply_and_check(self) -> None:
        script = f"set -euo pipefail; NET_ROLLBACK_SEC=200; . {DEPLOY / 'steps' / 'lib.sh'}; . {DEPLOY / 'steps' / 'network.sh'}"
        done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("NET_ROLLBACK_SEC(200)", done.stderr)

    def test_steps_that_lean_on_nic_names_stop_first_on_eth_names(self) -> None:
        name_steps = {"firewall", "lldpd", "router"}
        for name in sorted(name_steps):
            with self.subTest(step=name), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                sysnet = root / "sys"
                for dev, mac in (("eth0", "bc:24:11:00:00:10"), ("eth1", "bc:24:11:00:00:11")):
                    (sysnet / dev / "device").mkdir(parents=True)
                    (sysnet / dev / "address").write_text(mac + "\n")
                (root / "nic-macs").write_text("ens18 bc:24:11:00:00:10\nens19 bc:24:11:00:00:11\n")
                dest = root / "placed.conf"
                (root / "files").mkdir()
                (root / "files" / "a").write_text("x\n")
                (root / "files.tsv").write_text(f"{name}\t{dest}\t0644\troot\troot\tfiles/a\n")
                calls = root / "calls"
                bin_dir = root / "bin"
                bin_dir.mkdir()
                for tool in ("install", "apt-get", "nft", "systemctl", "systemd-tmpfiles", "python3", "chown"):
                    (bin_dir / tool).write_text(f"#!/bin/sh\necho {tool} \"$@\" >> {calls}\n")
                    (bin_dir / tool).chmod(0o755)
                env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "SYS_NET": str(sysnet), "NIC_MAC_FILE": str(root / "nic-macs"),
                       "NIC_EXPECT": "ens18=10.77.1.2 ens19=10.77.2.2", "MGMT_IF": "ens19", "ROUTER_AUGMENT": "ens19"}
                script = f"set -euo pipefail; BUNDLE={root}; . {DEPLOY / 'steps' / 'lib.sh'}; . {DEPLOY / 'steps' / (name + '.sh')}"
                done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("NIC 이름이 인벤토리와 다름(network 단계를 먼저): ens18 없음,ens19 없음,인벤토리에 없는 물리 NIC eth0 eth1",
                              done.stderr)
                self.assertFalse(calls.exists(), "이름 확인 전에 무언가를 했다")
                self.assertFalse(dest.exists())

    def _apt(self, root: Path, snapshot: str | None, update_out: str = "Hit:1 x\n", lists: tuple[str, ...] = (),
             call: str = "apt_update; apt_install chrony") -> tuple[subprocess.CompletedProcess, str]:
        bin_dir, listdir = root / "bin", root / "lists"
        bin_dir.mkdir()
        listdir.mkdir()
        for name in lists:
            (listdir / name).write_text("")
        calls = root / "calls"
        (root / "out").write_text(update_out)
        (bin_dir / "apt-get").write_text(f"#!/bin/sh\necho apt-get \"$@\" >> {calls}\n"
                                         f"[ \"$1\" = update ] && cat {root / 'out'}\nexit 0\n")
        (bin_dir / "apt-get").chmod(0o755)
        (root / "os-release").write_text("NAME=Ubuntu\nVERSION_CODENAME=noble\n")
        env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "APT_LISTS": str(listdir), "OS_RELEASE": str(root / "os-release")}
        if snapshot is not None:
            env["APT_SNAPSHOT"] = snapshot
        env["POLICY_RC"] = str(root / "policy-rc.d")
        script = f"set -euo pipefail; BUNDLE={root}; . {DEPLOY / 'steps' / 'lib.sh'}; {call}"
        done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
        return done, calls.read_text() if calls.exists() else ""

    SNAP_LISTS = tuple(f"snapshot.ubuntu.com_ubuntu_20261117T224105Z_dists_{suite}_InRelease"
                       for suite in ("noble", "noble-updates", "noble-security", "noble-backports"))

    def test_apt_snapshot_is_given_to_update_and_install_and_its_index_is_checked(self) -> None:
        # S 쌍 조건 2: T 짝 서버의 apt 설치 시각 색인(runbook §14). 받기와 설치 둘 다 같은 --snapshot 이어야 한다.
        with tempfile.TemporaryDirectory() as raw:
            done, calls = self._apt(Path(raw), "20261117T224105Z", lists=self.SNAP_LISTS)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("apt snapshot 20261117T224105Z", done.stdout + done.stderr)
        lines = calls.splitlines()
        self.assertEqual(lines[0], "apt-get update -q --snapshot 20261117T224105Z")
        self.assertRegex(lines[1], r"^apt-get install -y -q --no-install-recommends --snapshot 20261117T224105Z chrony$")

    def test_without_a_snapshot_apt_is_called_as_before(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            done, calls = self._apt(Path(raw), None, update_out="W: something\n")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(calls.splitlines(), ["apt-get update -q", "apt-get install -y -q --no-install-recommends chrony"])
        self.assertNotIn("apt snapshot", done.stdout + done.stderr)

    def test_a_snapshot_index_that_did_not_arrive_stops_before_installing(self) -> None:
        # apt 는 스냅숏 색인을 못 받아도 경고만 내고 0 으로 끝나 옛 색인을 쓴다(11-18 컨테이너 실측, 인증서 없음).
        cases = {
            "warning": ("W: Failed to fetch https://snapshot.ubuntu.com/ubuntu/20261117T224105Z/dists/noble/InRelease\n",
                        self.SNAP_LISTS, "스냅숏 색인을 받지 못함(20261117T224105Z): W: Failed to fetch"),
            "missing-security": ("Hit:1 x\n", tuple(n for n in self.SNAP_LISTS if "security" not in n),
                                 "스냅숏 색인 noble-security 이 없음(20261117T224105Z)"),
        }
        for name, (out, lists, message) in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as raw:
                done, calls = self._apt(Path(raw), "20261117T224105Z", update_out=out, lists=lists)
                self.assertNotEqual(done.returncode, 0)
                self.assertIn(message, done.stderr)
                self.assertNotIn("apt-get install", calls)

    def test_a_rerun_with_another_or_no_index_time_stops_before_apt_runs(self) -> None:
        # 앞서 스냅숏으로 깐 서버를 다른 시각이나 시각 없이 다시 돌리면 판이 갈라진다. 기록과 같거나 기록이 없을 때만 지나간다.
        cases = (("20261117T224105Z", "20261117T224105Z", True), (None, "20261117T224105Z", True),
                 ("", None, True), ("20261117T224105Z", "20261118T022012Z", False), ("20261117T224105Z", None, False))
        for before, now, passes in cases:
            with self.subTest(before=before, now=now), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                record = root / "apt-snapshot"
                if before is not None:
                    record.write_text(before + "\n")
                call = f"APT_SNAPSHOT_RECORD={record}; apt_snapshot_record_check; apt_update"
                done, calls = self._apt(root, now, lists=self.SNAP_LISTS, call=call)
                if passes:
                    self.assertEqual(done.returncode, 0, done.stderr)
                    self.assertTrue(calls.startswith("apt-get update"))
                else:
                    self.assertNotEqual(done.returncode, 0)
                    self.assertIn("앞서 깐 색인 시각 20261117T224105Z", done.stderr)
                    self.assertEqual(calls, "")

    def test_os_base_checks_the_record_first_and_never_blanks_it(self) -> None:
        source = (DEPLOY / "steps" / "os-base.sh").read_text()
        code = [line.strip() for line in source.splitlines() if line.strip() and not line.lstrip().startswith("#")]
        self.assertLess(code.index("apt_snapshot_record_check"), code.index("apt_update"))
        writes = [line for line in code if "APT_SNAPSHOT_RECORD" in line]
        self.assertEqual(writes, ['[[ -z "${APT_SNAPSHOT:-}" ]] || printf \'%s\\n\' "$APT_SNAPSHOT" > "$APT_SNAPSHOT_RECORD"'])

    def test_a_future_or_malformed_snapshot_is_refused_before_apt_runs(self) -> None:
        # 미래 시각은 snapshot 저장소가 최신 색인을 준다(11-18 실측 rc 0). 그러면 고정이 아니다.
        for value, message in (("20991231T000000Z", "미래 시각"), ("2026-11-17", "꼴이 다름")):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as raw:
                done, calls = self._apt(Path(raw), value, lists=self.SNAP_LISTS)
                self.assertNotEqual(done.returncode, 0)
                self.assertIn(message, done.stderr)
                self.assertEqual(calls, "")

    def test_require_inventory_names_passes_on_inventory_names(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for dev, mac in (("ens18", "bc:24:11:00:00:10"), ("ens19", "bc:24:11:00:00:11")):
                (root / "sys" / dev / "device").mkdir(parents=True)
                (root / "sys" / dev / "address").write_text(mac + "\n")
            (root / "nic-macs").write_text("ens18 bc:24:11:00:00:10\nens19 bc:24:11:00:00:11\n")
            (root / "bin").mkdir()
            (root / "bin" / "ip").write_text(
                "#!/bin/sh\ncase \"$6\" in ens18) echo ' inet 10.77.1.2/24';; ens19) echo ' inet 10.77.2.2/24';; esac\n")
            (root / "bin" / "ip").chmod(0o755)
            env = {"PATH": f"{root / 'bin'}:/usr/bin:/bin", "SYS_NET": str(root / "sys"),
                   "NIC_MAC_FILE": str(root / "nic-macs"), "NIC_EXPECT": "ens18=10.77.1.2 ens19=10.77.2.2"}
            done = subprocess.run(["bash", "-c", f"set -euo pipefail; . {DEPLOY / 'steps' / 'lib.sh'}; require_inventory_names; echo passed"],
                                  capture_output=True, text=True, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("passed", done.stdout)

    def test_every_step_that_uses_nic_names_checks_them_first(self) -> None:
        exempt = {"network", "verify-host", "verify"}
        found = set()
        for path in (T_INV, S_INV):
            inv = inventory(path)
            for host in inv["hosts"]:
                names = {nic["ifname"] for nic in host["nics"]}
                for entry in bundles(path)[host["name"]]["files"]:
                    if entry["step"] not in exempt and any(re.search(rf"\b{n}\b", entry["text"]) for n in names):
                        found.add(entry["step"])
        for script in (DEPLOY / "steps").glob("*.sh"):
            if script.stem not in exempt and script.stem != "lib" and re.search(
                    r"\$\{?(MGMT_IF|ROUTER_AUGMENT|NIC_EXPECT)", script.read_text()):
                found.add(script.stem)
        self.assertEqual(found, {"firewall", "lldpd", "router"})
        for step in found:
            lines = [l for l in (DEPLOY / "steps" / f"{step}.sh").read_text().splitlines() if l and not l.startswith("#")]
            with self.subTest(step=step):
                self.assertEqual(lines[0], "require_inventory_names")

    # db 단계의 데이터 디스크 찾기와 포맷 보호. 단계는 앞부분(포맷까지)만 돌린다(뒤는 /etc/fstab 과 PostgreSQL 을 만진다).
    # lsblk, blkid, mkfs.ext4 는 대역이다. [[ -b ]] 를 지나게 장치 경로는 이 호스트의 실제 블록 장치 이름을 빌려 쓰고,
    # 대역 밖의 어떤 명령도 그 장치를 건드리지 않는다.
    @staticmethod
    def _block_devices(n: int) -> list[str]:
        found = sorted(e.path for e in os.scandir("/dev") if stat.S_ISBLK(os.stat(e.path).st_mode))
        if len(found) < n:
            raise AssertionError(f"시험에 빌려 쓸 블록 장치가 {n}개 없음: {found}")
        return found[:n]

    def _db_disk(self, root: Path, table: str, probe: dict[str, int] | None = None) -> subprocess.CompletedProcess:
        calls = root / "calls"
        bin_dir = root / "bin"
        bin_dir.mkdir()
        probe = probe or {}
        cases = "".join(f'    "{dev}") exit {rc} ;;\n' for dev, rc in probe.items())
        stubs = {
            "lsblk": f'#!/bin/sh\necho lsblk "$@" >> {calls}\ncat {root / "table"}\n',
            "blkid": f'#!/bin/sh\necho blkid "$@" >> {calls}\nfor dev; do :; done\ncase "$dev" in\n{cases}esac\nexit 2\n',
            "mkfs.ext4": f'#!/bin/sh\necho mkfs.ext4 "$@" >> {calls}\n',
        }
        for tool, text in stubs.items():
            (bin_dir / tool).write_text(text)
            (bin_dir / tool).chmod(0o755)
        (root / "table").write_text(table)
        head = re.split(r'(?m)^install -d -m 0755 "\$DATA_MOUNT"', (DEPLOY / "steps" / "db.sh").read_text())[0]
        (root / "db-head.sh").write_text(head)
        script = f"set -euo pipefail; . {DEPLOY / 'steps' / 'lib.sh'}; . {root / 'db-head.sh'}; echo disk-done"
        env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "DATA_DISK_SERIAL": "pgdata", "DATA_MOUNT": str(root / "pgdata")}
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)

    def test_db_step_finds_the_data_disk_by_lsblk_serial_whatever_the_by_id_name(self) -> None:
        # 66 db01(11-18): by-id 는 scsi-SQEMU_QEMU_HARDDISK_pgdata 이고 scsi-0QEMU_ 꼴은 drive-scsiN 에만 붙었다.
        # 다른 QEMU 판에서는 scsi-0QEMU_QEMU_HARDDISK_pgdata 꼴일 수 있다. 어느 쪽이든 lsblk SERIAL 은 serial 그대로다.
        os_disk, data_disk, cdrom = self._block_devices(3)
        layouts = {
            "SQEMU": f"{os_disk} drive-scsi0 disk\n{data_disk} pgdata disk\n{cdrom} QM00003 rom\n",
            "0QEMU": f"{data_disk} pgdata disk\n{os_disk} drive-scsi0 disk\n",
        }
        for name, table in layouts.items():
            with self.subTest(by_id=name), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                done = self._db_disk(root, table)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertIn("disk-done", done.stdout)
                calls = (root / "calls").read_text().splitlines()
                self.assertEqual(calls, ["lsblk -dnr -o PATH,SERIAL,TYPE", f"blkid -p {data_disk}",
                                         f"mkfs.ext4 -q -L pgdata {data_disk}"])
        source = (DEPLOY / "steps" / "db.sh").read_text()
        self.assertNotIn("/dev/disk/by-id", source)
        self.assertNotIn("QEMU", source)

    def test_db_step_changes_nothing_without_exactly_one_data_disk(self) -> None:
        a, b, c = self._block_devices(3)
        tables = {
            # serial 을 안 준 디스크(drive-scsiN), 이름만 비슷한 serial, 파티션과 CD 는 데이터 디스크가 아니다.
            "none": (f"{a} drive-scsi0 disk\n{b} drive-scsi1 disk\n{c} pgdata part\n/dev/sr0 pgdata rom\n{c} pgdata2 disk\n", "0개"),
            "two": (f"{a} drive-scsi0 disk\n{b} pgdata disk\n{c} pgdata disk\n", "2개"),
            "empty": ("", "0개"),
        }
        for name, (table, count) in tables.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                done = self._db_disk(root, table)
                self.assertNotEqual(done.returncode, 0)
                self.assertNotIn("disk-done", done.stdout)
                self.assertIn(f"serial pgdata 인 디스크가 {count}(하나여야 함)", done.stderr)
                self.assertIn("FAIL 데이터 디스크(serial pgdata)를 하나로 찾지 못함. 아무것도 바꾸지 않음", done.stderr)
                self.assertEqual((root / "calls").read_text().splitlines(), ["lsblk -dnr -o PATH,SERIAL,TYPE"])
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            done = self._db_disk(root, f"{a} drive-scsi0 disk\n{b} pgdata disk\n")
            self.assertEqual(done.returncode, 0, done.stderr)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            done = self._db_disk(root, f"{a} drive-scsi0 disk\n{b}  disk\n{c} pgdata disk\n")
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn(f"mkfs.ext4 -q -L pgdata {c}", (root / "calls").read_text())
        # lsblk 가 실패하면 멈춘다.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            done = self._db_disk(root, "")
            (root / "bin" / "lsblk").write_text("#!/bin/sh\nexit 1\n")
            done = subprocess.run(["bash", "-c", f"set -euo pipefail; . {DEPLOY / 'steps' / 'lib.sh'}; . {root / 'db-head.sh'}; echo disk-done"],
                                  capture_output=True, text=True,
                                  env={"PATH": f"{root / 'bin'}:/usr/bin:/bin", "DATA_DISK_SERIAL": "pgdata", "DATA_MOUNT": str(root / "m")})
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("lsblk 실패", done.stderr)

    def test_db_step_formats_only_a_disk_with_no_signature(self) -> None:
        a, b = self._block_devices(2)
        table = f"{a} drive-scsi0 disk\n{b} pgdata disk\n"
        # 0 = 파일 시스템이나 파티션 표가 있음, 8 = 서명이 여럿(모호함), 4 = 사용법 오류, 1 = 그 밖 오류.
        for rc, ok in ((0, True), (8, False), (4, False), (1, False)):
            with self.subTest(blkid=rc), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                done = self._db_disk(root, table, {b: rc})
                calls = (root / "calls").read_text()
                self.assertNotIn("mkfs", calls)
                self.assertEqual(done.returncode == 0, ok, done.stderr)
                if ok:
                    self.assertIn(f"데이터 디스크 {b} 에 서명이 있어 포맷하지 않음", done.stdout)
                else:
                    self.assertIn(f"데이터 디스크 {b} 를 blkid 가 읽지 못함(rc {rc}). 포맷하지 않음", done.stderr)
        # 블록 장치가 아니면(lsblk 와 실제가 어긋남) blkid 전에 멈춘다. blkid 는 없는 경로에도 2(서명 없음)를 돌려준다.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            done = self._db_disk(root, f"{a} drive-scsi0 disk\n{root}/nodev pgdata disk\n")
            self.assertNotEqual(done.returncode, 0)
            self.assertIn(f"데이터 디스크가 블록 장치가 아님: {root}/nodev", done.stderr)
            self.assertNotIn("blkid", (root / "calls").read_text())
            self.assertNotIn("mkfs", (root / "calls").read_text())

    # db-schema, db-seed 는 묶음(/root/petition-deploy/<호스트>, root 만 들어감) 안 SQL 을 postgres 로 돌린다.
    # 가짜 runuser 는 명령을 도는 동안 묶음 디렉터리를 chmod 000 으로 막아 "다른 사용자는 묶음을 열 수 없음" 을 흉내 낸다
    # (시험은 root 가 아닌 사용자로 돌아야 막힘이 진짜다). 부르는 셸이 미리 연 표준 입력은 그대로 읽힌다.
    def _db_sql_tree(self, root: Path) -> tuple[Path, dict]:
        if os.geteuid() == 0:
            raise AssertionError("이 시험은 root 가 아닌 사용자로 돌아야 묶음 막힘을 흉내 낼 수 있다")
        bundle = root / "bundle"
        (bundle / "artifacts").mkdir(parents=True)
        (bundle / "artifacts" / "schema.sql").write_text("\\set ON_ERROR_STOP 1\nCREATE TABLE petition(id int);\n")
        (bundle / "artifacts" / "seed.sql").write_text("\\set ON_ERROR_STOP 1\nINSERT INTO petition VALUES (1);\n")
        subprocess.run("sha256sum schema.sql seed.sql > SHA256SUMS", shell=True, check=True, cwd=bundle / "artifacts")
        bundle.chmod(0o700)
        calls, out = root / "calls", root / "psql-read"
        bin_dir = root / "bin"
        bin_dir.mkdir()
        stubs = {
            "runuser": (f'#!/bin/sh\necho runuser "$@" >> {calls}\n[ "$1" = -u ] && shift 2\n[ "$1" = -- ] && shift\n'
                        f'chmod 000 {bundle}\n"$@"\nrc=$?\nchmod 700 {bundle}\nexit $rc\n'),
            "psql": (f'#!/bin/sh\necho psql "$@" >> {calls}\nfile=""; cmd=""\n'
                     'while [ $# -gt 0 ]; do case "$1" in -f) file=$2; shift 2;; -c) cmd=$2; shift 2;; *) shift;; esac; done\n'
                     'if [ -n "$cmd" ]; then case "$cmd" in "select count(*) from petition") echo "${FAKE_COUNT:-0}";; *) echo summary;; esac; exit 0; fi\n'
                     f'if [ "$file" = - ]; then cat >> {out}; else data=$(cat "$file" 2>/dev/null) || {{ echo "psql: error: $file: Permission denied" >&2; exit 2; }}; printf "%s\\n" "$data" >> {out}; fi\n'
                     '[ -n "$FAKE_PSQL_FAIL" ] && { echo "psql:<stdin>:2: ERROR: boom" >&2; exit 3; }\nexit 0\n'),
        }
        for tool, text in stubs.items():
            (bin_dir / tool).write_text(text)
            (bin_dir / tool).chmod(0o755)
        env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "DB_NAME": "petition", "DB_USER": "petition_app"}
        return bundle, env

    def _run_db_sql(self, bundle: Path, script: str, env: dict) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", "-c", f"set -euo pipefail; BUNDLE={bundle}; . {DEPLOY / 'steps' / 'lib.sh'}; {script}"],
                              capture_output=True, text=True, env=env)

    def test_fake_runuser_blocks_a_bundle_file_opened_by_the_other_user(self) -> None:
        # 흉내가 실제로 막는지: postgres 가 묶음 파일을 직접 여는 종전 꼴은 Permission denied 로 실패한다(APT2 §6-5).
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle, env = self._db_sql_tree(root)
            done = self._run_db_sql(bundle, 'runuser -u postgres -- psql -v ON_ERROR_STOP=1 -q -d "$DB_NAME" -f "$BUNDLE/artifacts/schema.sql"', env)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn(f"psql: error: {bundle}/artifacts/schema.sql: Permission denied", done.stderr)
            self.assertFalse((root / "psql-read").exists())
            self.assertEqual(oct(bundle.stat().st_mode & 0o777), "0o700")

    def test_db_schema_and_seed_hand_the_sql_over_stdin_opened_by_root(self) -> None:
        for step, sql in (("db-schema", "CREATE TABLE petition"), ("db-seed", "INSERT INTO petition")):
            with self.subTest(step=step), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                bundle, env = self._db_sql_tree(root)
                done = self._run_db_sql(bundle, f". {DEPLOY / 'steps' / (step + '.sh')}", env)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertIn(sql, (root / "psql-read").read_text())
                runs = [l for l in (root / "calls").read_text().splitlines() if l.startswith("psql") and " -f " in l]
                self.assertEqual(len(runs), 1, runs)
                self.assertIn("-v ON_ERROR_STOP=1", runs[0])
                self.assertIn("-v app_user=petition_app", runs[0])
                self.assertTrue(runs[0].endswith(" -f -"), runs[0])
                self.assertNotIn(str(bundle), runs[0])

    def test_db_schema_and_seed_stop_when_psql_fails(self) -> None:
        for step, ok_line in (("db-schema", "schema ok"), ("db-seed", "summary")):
            with self.subTest(step=step), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                bundle, env = self._db_sql_tree(root)
                env["FAKE_PSQL_FAIL"] = "1"
                done = self._run_db_sql(bundle, f". {DEPLOY / 'steps' / (step + '.sh')}; echo step-done", env)
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("ERROR: boom", done.stderr)
                self.assertNotIn(ok_line, done.stdout)
                self.assertNotIn("step-done", done.stdout)

    def test_db_seed_does_not_load_into_a_filled_database(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle, env = self._db_sql_tree(root)
            env["FAKE_COUNT"] = "2000000"
            done = self._run_db_sql(bundle, f". {DEPLOY / 'steps' / 'db-seed.sh'}", env)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertNotIn(" -f ", (root / "calls").read_text())
            self.assertFalse((root / "psql-read").exists())

    def test_nothing_running_as_another_user_opens_a_root_only_path(self) -> None:
        # 설치 단계와 놓는 파일(T, S 공통) 전부: 다른 사용자로 도는 명령(runuser, setpriv --reuid, sudo -u, su)이 묶음,
        # /root, 설치 상태 디렉터리를 인자로 받지 않는다. root 셸의 < 리다이렉트로 넘기는 것은 된다.
        # 다른 사용자로 도는 서비스 유닛(User=)도 그 경로를 가리키지 않는다.
        root_only = re.compile(r"\$\{?BUNDLE|/root\b|petition-deploy|\bsecrets/")
        other_user = re.compile(r"\b(runuser\b|setpriv\b[^\n]*--reuid|sudo\s+-u\b|su\s+(-c\b|-\s|-l\b|[a-z_][a-z0-9_-]*\s+-c\b))")
        sources = sorted((DEPLOY / "steps").glob("*.sh")) + sorted(p for p in (DEPLOY / "templates").rglob("*") if p.is_file())
        found, seen = [], 0
        for path in sources:
            text = re.sub(r"\\\n\s*", " ", path.read_text())
            for line in text.splitlines():
                if line.lstrip().startswith("#") or not other_user.search(line):
                    continue
                seen += 1
                command = re.sub(r"<\s*\"?\$\{?BUNDLE\}?[^\s\"]*\"?", "", line)
                if root_only.search(command):
                    found.append(f"{path.name}: {line.strip()}")
        self.assertEqual(found, [])
        self.assertGreaterEqual(seen, 8, "훑은 줄이 너무 적다(꼴이 바뀌어 아무것도 안 보고 있을 수 있음)")
        units = []
        for path in (T_INV, S_INV):
            for bundle in bundles(path).values():
                for entry in bundle["files"]:
                    if re.search(r"(?m)^User=(?!root$)", entry["text"]):
                        units.append(entry["dest"])
                        with self.subTest(inventory=path.name, dest=entry["dest"]):
                            self.assertIsNone(root_only.search(entry["text"]))
        self.assertIn("/etc/systemd/system/loadgen.service", units)

    # verify.sh 의 db 항목 함수만 떼어 대역 위에서 부른다(앞부분 정의만, check 줄 전).
    # 대역 psql 은 받은 질의를 그대로 sqlite 의 pg_roles 표(rolname, rolcanlogin)에 돌린다(!~ 는 NOT REGEXP, ilike 는
    # 대소문자 무시 LIKE 로만 바꿈). 같은 질의를 PostgreSQL 16 에서 돌린 결과는 agent-out/T2VER.md 에 적었다.
    PSQL_ROLES_STUB = r"""#!/usr/bin/env python3
import json, os, re, sqlite3, sys
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write("psql " + " ".join(args) + "\n")
if os.environ.get("FAKE_PSQL_FAIL"):
    sys.exit("psql: error: connection refused")
query = args[args.index("-c") + 1]
db = sqlite3.connect(":memory:")
db.create_function("REGEXP", 2, lambda pattern, value: re.search(pattern, value) is not None)
db.execute("create table pg_roles (rolname text, rolcanlogin boolean)")
db.executemany("insert into pg_roles values (?, ?)", json.loads(os.environ["FAKE_ROLES"]))
for (name,) in db.execute(query.replace(" !~ ", " NOT REGEXP ").replace(" ilike ", " LIKE ")):
    print(name)
"""
    PG16_ROLES = [["pg_monitor", 0], ["pg_read_all_stats", 0], ["pg_signal_backend", 0], ["postgres", 1], ["petition_app", 1]]

    def _verify_db(self, root: Path, call: str, env_extra: dict) -> subprocess.CompletedProcess:
        source = (DEPLOY / "steps" / "verify.sh").read_text()
        (root / "verify-defs.sh").write_text(source[:re.search(r"(?m)^check ", source).start()])
        bin_dir = root / "bin"
        bin_dir.mkdir(exist_ok=True)
        stubs = {"runuser": '#!/bin/sh\n[ "$1" = -u ] && shift 2\n[ "$1" = -- ] && shift\nexec "$@"\n',
                 "psql": self.PSQL_ROLES_STUB}
        for tool, text in stubs.items():
            (bin_dir / tool).write_text(text)
            (bin_dir / tool).chmod(0o755)
        hba = root / "pg_hba.conf"
        if not hba.exists():
            hba.write_text("local all postgres peer\nhost all all 127.0.0.1/32 scram-sha-256\nhost petition petition_app 192.168.126.21/32 scram-sha-256\n")
        env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "STUB_LOG": str(root / "calls"), "PG_HBA": str(hba),
               "MONITOR_ADDRESS": "10.250.0.1", "FAKE_ROLES": json.dumps(self.PG16_ROLES), **env_extra}
        script = f"set -uo pipefail; . {DEPLOY / 'steps' / 'lib.sh'}; . {root / 'verify-defs.sh'}; if {call}; then echo PASS; else echo FAIL; fi"
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)

    def test_verify_no_dpm_traces_ignores_builtin_roles_and_catches_login_monitor_accounts(self) -> None:
        cases = {
            "builtin pg_monitor only": (self.PG16_ROLES, "PASS"),
            "login monitor account": (self.PG16_ROLES + [["dpm_monitor", 1]], "FAIL"),
            "login account with upper case DPM": (self.PG16_ROLES + [["DPMUser", 1]], "FAIL"),
            "monitor role that cannot log in": (self.PG16_ROLES + [["monitor_ro", 0]], "PASS"),
            "pg_ prefixed login role": (self.PG16_ROLES + [["pg_dpm", 1]], "PASS"),
        }
        for name, (roles, want) in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                done = self._verify_db(root, "no_dpm_traces", {"FAKE_ROLES": json.dumps(roles)})
                self.assertEqual(done.stdout.strip(), want, done.stderr)
                query = (root / "calls").read_text()
                self.assertIn("rolcanlogin", query)
                self.assertIn("-v ON_ERROR_STOP=1", query)
        # 질의가 실패하면 "역할 없음" 으로 통과하지 않는다(종전 꼴은 grep 이 못 찾아 통과했다).
        with tempfile.TemporaryDirectory() as raw:
            done = self._verify_db(Path(raw), "no_dpm_traces", {"FAKE_PSQL_FAIL": "1"})
            self.assertEqual(done.stdout.strip(), "FAIL")

    def test_verify_no_dpm_traces_matches_the_monitor_address_exactly_in_pg_hba(self) -> None:
        cases = {
            "address": ("host all dpm 10.250.0.1/32 scram-sha-256\n", "FAIL"),
            "bare address": ("host all all 10.250.0.1 255.255.255.255 md5\n", "FAIL"),
            "longer address": ("host all all 10.250.0.10/32 scram-sha-256\n", "PASS"),
            "dots as any character": ("host all all 10x250y0z1/32 scram-sha-256\n", "PASS"),
            "commented": ("# host all dpm 10.250.0.1/32 scram-sha-256\n  # 10.250.0.1\n", "PASS"),
        }
        for name, (line, want) in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                (root / "pg_hba.conf").write_text("local all postgres peer\n" + line)
                done = self._verify_db(root, "no_dpm_traces", {})
                self.assertEqual(done.stdout.strip(), want, done.stderr)
        with tempfile.TemporaryDirectory() as raw:
            done = self._verify_db(Path(raw), "no_dpm_traces", {"PG_HBA": f"{raw}/missing"})
            self.assertEqual(done.stdout.strip(), "FAIL")

    def test_verify_data_mount_is_the_serial_disk(self) -> None:
        def run(root: Path, table: str, source: str, mounted: bool = True) -> str:
            bin_dir = root / "bin"
            bin_dir.mkdir(exist_ok=True)
            (bin_dir / "lsblk").write_text(f"#!/bin/sh\nprintf '%s' '{table}'\n")
            (bin_dir / "findmnt").write_text(
                f'#!/bin/sh\necho "findmnt $*" >> {root / "calls"}\n' + (f"echo '{source}'\n" if mounted else "exit 1\n"))
            for tool in ("lsblk", "findmnt"):
                (bin_dir / tool).chmod(0o755)
            done = self._verify_db(root, "data_mount_on_serial_disk", {"DATA_DISK_SERIAL": "pgdata", "DATA_MOUNT": "/pgdata"})
            return done.stdout.strip()
        table = "/dev/sda drive-scsi0 disk\n/dev/sdb pgdata disk\n"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.assertEqual(run(root, table, "/dev/sdb"), "PASS")
            self.assertIn("findmnt -n -o SOURCE --mountpoint /pgdata", (root / "calls").read_text())
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "by-label").mkdir()
            (root / "by-label" / "pgdata").symlink_to("/dev/sdb")
            self.assertEqual(run(root, table, f"{root}/by-label/pgdata"), "PASS", "링크로 보고돼도 같은 장치면 통과")
        for name, args in {"other disk": (table, "/dev/sda"), "not mounted": (table, "", False),
                           "no serial disk": ("/dev/sda drive-scsi0 disk\n/dev/sdb drive-scsi1 disk\n", "/dev/sdb"),
                           "two serial disks": ("/dev/sdb pgdata disk\n/dev/sdc pgdata disk\n", "/dev/sdb"),
                           "empty source": (table, "")}.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as raw:
                self.assertEqual(run(Path(raw), *args), "FAIL")
        source = (DEPLOY / "steps" / "verify.sh").read_text()
        db_block = source[source.index("    db)\n"):source.index("    loadgen)")]
        self.assertIn("check data-mount-on-serial-disk data_mount_on_serial_disk", db_block)
        self.assertIn("check no-dpm-traces no_dpm_traces", db_block)

    def test_verify_guard_checks_read_the_rules_not_just_the_table(self) -> None:
        # verify 의 mgmt-guard, monitor-notrack 은 표가 있는지가 아니라 지켜야 할 줄이 올라왔는지 본다. 렌더된 표를 네임스페이스의
        # 진짜 nft 에 올리고 verify 함수를 그 안에서 부른다. 한 줄을 빼고 올리면 실패한다.
        bundle = bundles(T_INV)["rt-core01"]
        env_text = bundle["env"]
        source = (DEPLOY / "steps" / "verify.sh").read_text()
        defs = source[:re.search(r"(?m)^check ", source).start()]
        cases = {"as rendered": (None, "PASS PASS"),
                 "no ssh notrack": ("tcp dport 22 notrack", "FAIL PASS"),
                 "no snmp notrack": ("udp dport 161 notrack", "PASS FAIL"),
                 "no drop counter": ("192.168.127.0/24 counter drop", "PASS FAIL")}
        for name, (cut, want) in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                paths = []
                for entry in (e for e in bundle["files"] if e["dest"].startswith("/etc/nftables.d/")):
                    text = entry["text"]
                    if cut:
                        text = "\n".join(l if cut not in l else (l.replace(" counter drop", " drop") if "counter drop" in cut else "")
                                         for l in text.splitlines()) + "\n"
                    target = root / Path(entry["dest"]).name
                    target.write_text(text)
                    paths.append(str(target))
                (root / "defs.sh").write_text(defs)
                env_lines = "".join(f"export {k}={render._shell_quote(v)}\n" for k, v in env_text.items())
                (root / "env.sh").write_text(env_lines)
                script = (f'set -e; for f in "$@"; do nft -f "$f"; done; set +e; . {root / "env.sh"}; . {DEPLOY / "steps" / "lib.sh"}; '
                          f'. {root / "defs.sh"}; r=""; for c in mgmt_guard_ok monitor_notrack_ok; do if $c; then r="$r PASS"; else r="$r FAIL"; fi; done; echo $r')
                done = subprocess.run(["unshare", "-rn", "bash", "-c", script, "_", *paths], capture_output=True, text=True)
                self.assertEqual(done.stdout.strip(), want, done.stderr)

    def test_db_step_keeps_the_password_out_of_argv_and_the_server_log(self) -> None:
        source = (DEPLOY / "steps" / "db.sh").read_text()
        block = source[source.index("<<SQL"):source.index("\nSQL\n")]
        self.assertIn("SET log_statement = 'none';", block)
        self.assertLess(block.index("SET log_statement"), block.index("PASSWORD"))
        self.assertNotRegex(source, r"psql[^\n]*-c[^\n]*PASSWORD")

    def test_db_cluster_locale_is_fixed(self) -> None:
        source = (DEPLOY / "steps" / "db.sh").read_text()
        self.assertIn("pg_createcluster --locale C.UTF-8 16 main", source)

    def test_tools_parse(self) -> None:
        for name in ("router-discards-selftest.sh", "host-facts.sh", "cpu-sample.sh", "db-facts.sh", "archive-sql-check.sh",
                     "rsyslog-check.sh"):
            with self.subTest(tool=name):
                done = subprocess.run(["bash", "-n", str(DEPLOY / "tools" / name)], capture_output=True, text=True)
                self.assertEqual(done.returncode, 0, done.stderr)
        selftest = (DEPLOY / "tools" / "router-discards-selftest.sh").read_text()
        self.assertIn("trap cleanup EXIT", selftest)
        self.assertNotIn("logger", selftest)

    def test_secrets_are_wiped_after_a_partial_or_failing_run(self) -> None:
        for steps, expect_ok in (("good", True), ("bad", False)):
            with self.subTest(steps=steps), tempfile.TemporaryDirectory() as raw:
                bundle = Path(raw)
                (bundle / "steps.d").mkdir()
                (bundle / "steps.d" / "lib.sh").write_text((DEPLOY / "steps" / "lib.sh").read_text())
                (bundle / "steps.d" / "good.sh").write_text("test -r \"$BUNDLE/secrets/db_password\"\necho fine\n")
                (bundle / "steps.d" / "bad.sh").write_text("false\n")
                (bundle / "host.env").write_text("HOST_NAME='x'\n")
                (bundle / "steps").write_text("good\nbad\nverify\n")
                (bundle / "files.tsv").write_text("")
                (bundle / "secrets").mkdir()
                (bundle / "secrets" / "db_password").write_text("Abc123def456ghi7\n")
                source = (DEPLOY / "install-host.sh").read_text().replace("[[ $EUID -eq 0 ]] ||", "true ||")
                source = source.replace("LOG=/var/log/petition-deploy.log", f"LOG={bundle}/log")
                (bundle / "install.sh").write_text(source)
                done = subprocess.run(["bash", str(bundle / "install.sh"), str(bundle), steps],
                                      capture_output=True, text=True)
                self.assertEqual(done.returncode == 0, expect_ok, done.stderr)
                self.assertFalse((bundle / "secrets").exists())

    def test_router_steps_never_put_the_community_on_a_command_line(self) -> None:
        for path in (DEPLOY / "steps" / "router.sh", DEPLOY / "tools" / "router-discards-selftest.sh"):
            source = path.read_text()
            with self.subTest(path=path.name):
                self.assertNotRegex(source, r"snmp\w*\s[^\n]*-c\s")
                self.assertIn("SNMPCONFPATH", source)
                self.assertIn("umask 077", source)

    def test_apply_accepts_new_host_keys_into_its_own_known_hosts(self) -> None:
        source = (DEPLOY / "apply.sh").read_text()
        self.assertIn("StrictHostKeyChecking=accept-new", source)
        self.assertIn('UserKnownHostsFile=$known_hosts', source)

    def test_syslog_check_line_is_skipped_once_forwarding_is_on(self) -> None:
        source = (DEPLOY / "steps" / "syslog.sh").read_text()
        self.assertIn('remote_dir="$RSYSLOG_ETC/rsyslog.d/remote"', source)
        guard = source.index('compgen -G "$remote_dir/*.conf"')
        self.assertLess(guard, source.index("logger -t"))

    def test_install_host_reports_a_failing_step(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = Path(raw)
            (bundle / "steps.d").mkdir()
            (bundle / "steps.d" / "lib.sh").write_text((DEPLOY / "steps" / "lib.sh").read_text())
            (bundle / "steps.d" / "bad.sh").write_text("false\necho never\n")
            (bundle / "steps.d" / "good.sh").write_text("echo fine\n")
            (bundle / "host.env").write_text("HOST_NAME='x'\n")
            (bundle / "steps").write_text("good\nbad\n")
            (bundle / "files.tsv").write_text("")
            source = (DEPLOY / "install-host.sh").read_text().replace("[[ $EUID -eq 0 ]] ||", "true ||")
            source = source.replace("LOG=/var/log/petition-deploy.log", f"LOG={bundle}/log")
            (bundle / "install.sh").write_text(source)
            done = subprocess.run(["bash", str(bundle / "install.sh"), str(bundle)], capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("fine", done.stdout)
        self.assertNotIn("never", done.stdout)
        self.assertIn("단계 bad 실패", done.stderr)


    # 서비스를 다시 띄운 직후의 확인(settle, lib.sh). 시계는 date +%s 와 sleep 대역이 함께 쓰는 파일이라 시험이 기다리지 않는다.
    #   sleep 대역은 부를 때마다 FAKE_TICK 초씩 시계를 민다.
    FAKE_CLOCK_STUBS = {
        "date": '#!/bin/sh\nif [ "$1" = +%s ]; then cat "$FAKE_CLOCK"; else exec /bin/date "$@"; fi\n',
        "sleep": '#!/bin/sh\necho "sleep $1" >> "$FAKE_CLOCK.sleeps"\necho $(( $(cat "$FAKE_CLOCK") + ${FAKE_TICK:-1} )) > "$FAKE_CLOCK"\n',
    }

    def _fake_clock(self, root: Path) -> tuple[Path, dict]:
        bin_dir = root / "bin"
        bin_dir.mkdir(exist_ok=True)
        for tool, text in self.FAKE_CLOCK_STUBS.items():
            (bin_dir / tool).write_text(text)
            (bin_dir / tool).chmod(0o755)
        clock = root / "clock"
        clock.write_text("1000\n")
        return clock, {"PATH": f"{bin_dir}:/usr/bin:/bin", "FAKE_CLOCK": str(clock)}

    def _settle(self, root: Path, bad: int, tick: int, limit: int = 30, output: str = "Gauge32: 0") -> subprocess.CompletedProcess:
        """앞 bad 번은 틀린 값(output)을 내고 실패, 그다음은 맞는 값을 내는 명령으로 settle 을 부른다."""
        clock, env = self._fake_clock(root)
        count = root / "count"
        reader = root / "reader.sh"
        reader.write_text(f'#!/bin/sh\nn=$(( $(cat {count} 2>/dev/null || echo 0) + 1 ))\necho $n > {count}\n'
                          f'if [ $n -le {bad} ]; then printf "%s\\n" "{output}"; exit 1; fi\necho "Gauge32: 1000"\n')
        reader.chmod(0o755)
        env["FAKE_TICK"] = str(tick)
        script = f'set -euo pipefail; . {DEPLOY / "steps" / "lib.sh"}; settle {limit} 1 "ens19 ifHighSpeed 가 1000 이 아님" {reader}; echo settled'
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)

    def test_settle_passes_once_the_value_comes_right_after_some_wrong_reads(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            done = self._settle(root, bad=3, tick=1)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("settled", done.stdout)
            self.assertEqual((root / "count").read_text().strip(), "4")
            self.assertEqual((root / "clock.sleeps").read_text().splitlines(), ["sleep 1"] * 3)
            self.assertNotIn("Gauge32", done.stdout + done.stderr, "통과하면 명령 출력은 버린다")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            done = self._settle(root, bad=0, tick=1)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual((root / "count").read_text().strip(), "1")
            self.assertFalse((root / "clock.sleeps").exists(), "처음부터 맞으면 기다리지 않는다")

    def test_settle_fails_at_the_time_limit_with_the_last_value(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            done = self._settle(root, bad=10**6, tick=1)
            self.assertNotEqual(done.returncode, 0)
            self.assertNotIn("settled", done.stdout)
            self.assertIn("FAIL ens19 ifHighSpeed 가 1000 이 아님(30초 동안 다시 읽음). 마지막 값: Gauge32: 0", done.stderr)
            # 시계 1000 에서 시작해 1030 까지 1초마다: 1000, 1001, ..., 1030 에서 31 번 읽고 1030 에서 멈춘다.
            self.assertEqual((root / "count").read_text().strip(), "31")
            self.assertEqual((root / "clock").read_text().strip(), "1030")

    def test_settle_limit_is_time_not_a_number_of_reads(self) -> None:
        # 같은 상한(30초)에 다시 읽기 한 번이 10초 걸리면 4 번(0, 10, 20, 30초), 1초면 31 번 읽는다.
        for tick, reads in ((10, 4), (1, 31), (31, 2)):
            with self.subTest(tick=tick), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                done = self._settle(root, bad=10**6, tick=tick)
                self.assertNotEqual(done.returncode, 0)
                self.assertEqual((root / "count").read_text().strip(), str(reads))
                self.assertGreaterEqual(int((root / "clock").read_text()), 1030)
        # 상한을 줄이면 같은 간격에서 읽는 수도 준다.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            done = self._settle(root, bad=10**6, tick=1, limit=5)
            self.assertIn("(5초 동안 다시 읽음)", done.stderr)
            self.assertEqual((root / "count").read_text().strip(), "6")

    def test_settle_last_value_is_one_line_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            done = self._settle(root, bad=10**6, tick=31, output="Timeout: No Response from 127.0.0.1\nsecond line")
            self.assertIn("마지막 값: Timeout: No Response from 127.0.0.1 | second line\n", done.stderr)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            done = self._settle(root, bad=10**6, tick=31, output="x" * 1000)
            last = done.stderr.split("마지막 값: ", 1)[1].rstrip("\n")
            self.assertEqual(last, "x" * 300)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            done = self._settle(root, bad=10**6, tick=31, output="")
            self.assertIn("마지막 값: (출력 없음)", done.stderr)

    def _router_tree(self, root: Path, bad_speed: int, bad_discards: int = 0) -> tuple[Path, dict]:
        """router 단계 대역: snmpd 가 막 서서 ifHighSpeed 를 앞 bad_speed 번은 0 으로, ifOutDiscards 를 앞 bad_discards 번은
        noSuchInstance 로 답한다. 커뮤니티는 SNMPCONFPATH 의 snmp.conf 로만 받는다."""
        clock, env = self._fake_clock(root)
        sysnet = root / "sys"
        for dev, mac, index in (("ens18", "bc:24:11:00:00:10", 2), ("ens19", "bc:24:11:00:00:11", 3)):
            (sysnet / dev / "device").mkdir(parents=True)
            (sysnet / dev / "address").write_text(mac + "\n")
            (sysnet / dev / "ifindex").write_text(f"{index}\n")
        (root / "nic-macs").write_text("ens18 bc:24:11:00:00:10\nens19 bc:24:11:00:00:11\n")
        (root / "files.tsv").write_text("")
        (root / "secrets").mkdir()
        (root / "secrets" / "snmp_community").write_text("c0mmunity\n")
        calls = root / "calls"
        bin_dir = root / "bin"
        snmpget = f"""#!/bin/sh
echo "snmpget $* conf=$(cat "$SNMPCONFPATH/snmp.conf" | tr '\\n' ' ')" >> {calls}
for oid; do :; done
n=$(( $(cat "{root}/n.$oid" 2>/dev/null || echo 0) + 1 )); echo $n > "{root}/n.$oid"
case "$oid" in
    .1.3.6.1.2.1.1.5.0) echo "$oid = STRING: rt-core01" ;;
    .1.3.6.1.2.1.2.2.1.19.*) if [ $n -le {bad_discards} ]; then echo "$oid = No Such Instance currently exists at this OID"; else echo "$oid = Counter32: 0"; fi ;;
    .1.3.6.1.2.1.31.1.1.1.15.*) if [ $n -le {bad_speed} ]; then echo "$oid = Gauge32: 0"; else echo "$oid = Gauge32: 1000"; fi ;;
esac
"""
        stubs = {"snmpget": snmpget,
                 "ip": "#!/bin/sh\ncase \"$6\" in ens18) echo ' inet 10.77.1.2/24';; ens19) echo ' inet 10.77.2.2/24';; esac\n"}
        for tool in ("install", "systemctl", "systemd-tmpfiles", "python3"):
            stubs[tool] = f'#!/bin/sh\necho {tool} "$@" >> {calls}\n'
        for tool, text in stubs.items():
            (bin_dir / tool).write_text(text)
            (bin_dir / tool).chmod(0o755)
        # ICMP redirect 끄기: 놓인 파일은 T 라우터 렌더 결과 그대로, 커널 값은 부팅 때처럼 NIC 마다 1(all, default 는 0).
        conf = root / "fs" / render.ROUTER_REDIRECTS_CONF.lstrip("/")
        conf.parent.mkdir(parents=True)
        conf.write_text(file_of(bundles(T_INV)["rt-core01"], render.ROUTER_REDIRECTS_CONF))
        for name, value in self.ROUTER_PROC_SYS.items():
            (root / "proc" / "net" / "ipv4" / "conf" / name).mkdir(parents=True)
            (root / "proc" / "net" / "ipv4" / "conf" / name / "send_redirects").write_text(f"{value}\n")
        env.update({"SYS_NET": str(sysnet), "NIC_MAC_FILE": str(root / "nic-macs"),
                    "NIC_EXPECT": "ens18=10.77.1.2 ens19=10.77.2.2", "ROUTER_AUGMENT": "ens19",
                    "PROC_SYS": str(root / "proc"), "PLACE_ROOT": str(root / "fs"),
                    "ROUTER_REDIRECTS_CONF": render.ROUTER_REDIRECTS_CONF})
        return calls, env

    ROUTER_PROC_SYS = {"all": 0, "default": 0, "lo": 1, "ens18": 1, "ens19": 1, "ens20": 1, "ens21": 1, "ens22": 1}

    def _redirects(self, root: Path) -> dict[str, str]:
        base = root / "proc" / "net" / "ipv4" / "conf"
        return {path.name: (path / "send_redirects").read_text().strip() for path in sorted(base.iterdir())}

    def _run_router(self, root: Path, env: dict) -> subprocess.CompletedProcess:
        script = f"set -euo pipefail; BUNDLE={root}; . {DEPLOY / 'steps' / 'lib.sh'}; . {DEPLOY / 'steps' / 'router.sh'}"
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)

    def test_router_step_rereads_snmp_values_until_they_settle_after_the_restart(self) -> None:
        # snmpd 가 막 섰을 때 속도 덮어쓰기와 도우미 열이 늦게 서도(APT2 §6) 다시 읽어 통과한다.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            calls, env = self._router_tree(root, bad_speed=3, bad_discards=2)
            done = self._run_router(root, env)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("router snmp ok", done.stdout)
            text = calls.read_text()
            self.assertLess(text.index("systemctl restart snmpd"), text.index("snmpget"))
            self.assertEqual((root / "n..1.3.6.1.2.1.31.1.1.1.15.3").read_text().strip(), "4")
            self.assertEqual((root / "n..1.3.6.1.2.1.2.2.1.19.3").read_text().strip(), "3")
            self.assertFalse((root / "n..1.3.6.1.2.1.31.1.1.1.15.2").exists(), "ROUTER_AUGMENT 밖 NIC 는 읽지 않는다")
            for line in (l for l in text.splitlines() if l.startswith("snmpget")):
                self.assertNotIn("c0mmunity", line.split(" conf=")[0], "커뮤니티가 명령 인자에 있다")
                self.assertIn("defCommunity c0mmunity", line.split(" conf=")[1])

    def test_router_step_fails_with_the_last_value_once_the_limit_passes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            calls, env = self._router_tree(root, bad_speed=10**6)
            done = self._run_router(root, env)
            self.assertNotEqual(done.returncode, 0)
            self.assertNotIn("router snmp ok", done.stdout)
            self.assertIn("FAIL ens19 ifHighSpeed 가 1000 이 아님(30초 동안 다시 읽음). "
                          "마지막 값: .1.3.6.1.2.1.31.1.1.1.15.3 = Gauge32: 0", done.stderr)
            # 기본 상한 30초, 간격 1초: 31 번 읽고 멈춘다.
            self.assertEqual((root / "n..1.3.6.1.2.1.31.1.1.1.15.3").read_text().strip(), "31")
        # 상한과 간격은 환경변수로 바꾼다.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            calls, env = self._router_tree(root, bad_speed=10**6)
            env.update({"SETTLE_SEC": "6", "SETTLE_EVERY": "2", "FAKE_TICK": "2"})
            done = self._run_router(root, env)
            self.assertIn("(6초 동안 다시 읽음)", done.stderr)
            self.assertEqual((root / "n..1.3.6.1.2.1.31.1.1.1.15.3").read_text().strip(), "4")
            self.assertEqual(set((root / "clock.sleeps").read_text().splitlines()), {"sleep 2"})

    def test_router_speed_check_wants_exactly_1000(self) -> None:
        # 종전 grep 'Gauge32: 1000' 은 10000 도 받았다.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            calls, env = self._router_tree(root, bad_speed=0)
            snmpget = root / "bin" / "snmpget"
            snmpget.write_text(snmpget.read_text().replace('echo "$oid = Gauge32: 1000"', 'echo "$oid = Gauge32: 10000"'))
            env["SETTLE_SEC"] = "2"
            done = self._run_router(root, env)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("마지막 값: .1.3.6.1.2.1.31.1.1.1.15.3 = Gauge32: 10000", done.stderr)

    def test_router_step_turns_off_icmp_redirects_now_and_writes_nothing_when_already_off(self) -> None:
        # T3 차례 4(APT3 §2-5): 부팅 때 먼저 선 NIC 는 send_redirects 1 이고 커널은 all 과 NIC 값의 OR 로 redirect 를 보낸다.
        # router 단계가 놓인 파일의 키를 커널에 바로 쓴다. 다시 돌면 이미 0 이라 아무것도 쓰지 않는다.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            calls, env = self._router_tree(root, bad_speed=0)
            done = self._run_router(root, env)
            self.assertEqual(done.returncode, 0, done.stderr)
            nics = ["ens18", "ens19", "ens20", "ens21", "ens22"]
            self.assertIn("router send_redirects off: changed "
                          + " ".join(f"net.ipv4.conf.{n}.send_redirects" for n in nics) + "\n", done.stdout)
            want = {name: "0" for name in self.ROUTER_PROC_SYS}
            want["lo"] = "1"
            self.assertEqual(self._redirects(root), want)
            # 키를 쓴 뒤에 snmpd 를 다시 띄운다(같은 단계 안, 끝까지 감).
            self.assertIn("router snmp ok", done.stdout)
            base = root / "proc" / "net" / "ipv4" / "conf"
            stamps = {}
            for name in self.ROUTER_PROC_SYS:
                (base / name / "send_redirects").chmod(0o444)
                stamps[name] = (base / name / "send_redirects").stat().st_mtime_ns
            again = self._run_router(root, env)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertIn("router send_redirects off: already\n", again.stdout)
            self.assertNotIn("changed", again.stdout)
            self.assertEqual({n: (base / n / "send_redirects").stat().st_mtime_ns for n in self.ROUTER_PROC_SYS}, stamps)
            self.assertEqual(self._redirects(root), want)

    def test_router_step_stops_before_snmpd_when_a_redirects_key_is_missing_or_the_file_is_not_placed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            calls, env = self._router_tree(root, bad_speed=0)
            shutil.rmtree(root / "proc" / "net" / "ipv4" / "conf" / "ens21")
            done = self._run_router(root, env)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("FAIL 커널에 net.ipv4.conf.ens21.send_redirects 가 없음(NIC 이름이 인벤토리와 다름)", done.stderr)
            self.assertNotIn("systemctl restart snmpd", calls.read_text())
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            calls, env = self._router_tree(root, bad_speed=0)
            (root / "fs" / render.ROUTER_REDIRECTS_CONF.lstrip("/")).unlink()
            done = self._run_router(root, env)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn(f"FAIL {render.ROUTER_REDIRECTS_CONF} 가 놓이지 않음", done.stderr)
            self.assertEqual(self._redirects(root)["ens18"], "1")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            calls, env = self._router_tree(root, bad_speed=0)
            conf = root / "fs" / render.ROUTER_REDIRECTS_CONF.lstrip("/")
            conf.write_text(conf.read_text() + "net.ipv4.conf.ens18.send_redirects 0\n")
            done = self._run_router(root, env)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("에 꼴이 아닌 줄: net.ipv4.conf.ens18.send_redirects 0", done.stderr)

    def _verify_redirects(self, root: Path, values: dict[str, int], nic_expect: str) -> str:
        source = (DEPLOY / "steps" / "verify.sh").read_text()
        (root / "verify-defs.sh").write_text(source[:re.search(r"(?m)^check ", source).start()])
        for name, value in values.items():
            (root / "proc" / "net" / "ipv4" / "conf" / name).mkdir(parents=True)
            (root / "proc" / "net" / "ipv4" / "conf" / name / "send_redirects").write_text(f"{value}\n")
        env = {"PATH": "/usr/bin:/bin", "PROC_SYS": str(root / "proc"), "NIC_EXPECT": nic_expect}
        script = (f"set -uo pipefail; . {DEPLOY / 'steps' / 'lib.sh'}; . {root / 'verify-defs.sh'}; "
                  "if send_redirects_off; then echo PASS; else echo FAIL; fi")
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env).stdout.strip()

    def test_verify_send_redirects_off_needs_all_default_and_every_nic_at_zero(self) -> None:
        expect = "ens18=192.0.2.1 ens19=198.51.100.1"
        off = {"all": 0, "default": 0, "lo": 1, "ens18": 0, "ens19": 0}
        cases = {
            "all off, lo left on": (off, expect, "PASS"),
            "one inventory NIC on (T3 before the fix)": ({**off, "ens18": 1}, expect, "FAIL"),
            "all on": ({**off, "all": 1}, expect, "FAIL"),
            "default on": ({**off, "default": 1}, expect, "FAIL"),
            "a NIC outside the inventory on": ({**off, "ens23": 1}, expect, "FAIL"),
            "a NIC outside the inventory off": ({**off, "ens23": 0}, expect, "PASS"),
            "inventory NIC missing in the kernel": ({k: v for k, v in off.items() if k != "ens19"}, expect, "FAIL"),
            "no NIC at all": ({"all": 0, "default": 0, "lo": 0}, "", "FAIL"),
        }
        for name, (values, nic_expect, want) in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as raw:
                self.assertEqual(self._verify_redirects(Path(raw), values, nic_expect), want)
        source = (DEPLOY / "steps" / "verify.sh").read_text()
        router_block = source[source.index("    router)\n"):source.index("    web)")]
        self.assertIn("check send-redirects-off send_redirects_off", router_block)
        self.assertEqual(source.count("check send-redirects-off "), 1, "라우터에서만 본다")

    def test_checks_right_after_a_service_restart_reread_through_settle(self) -> None:
        # 서비스를 다시 띄운 뒤 값을 읽는 단계는 settle 로 다시 읽는다. 손으로 쓴 기다림 고리(sleep, seq)나
        # 한 번 읽고 멈추는 확인을 다시 들이지 않는다. firewall 은 nftables(oneshot)가 표를 다 올린 뒤에야 restart 가 끝나
        # 바로 읽어도 된다. lldpd, syslog-forward 는 재시작 뒤 읽는 값이 없다.
        wanted = {"router": ["snmpget -On 127.0.0.1 .1.3.6.1.2.1.1.5.0", "ifOutDiscards 응답 없음", "ifHighSpeed 가 1000 이 아님"],
                  "web": ["정적 첫 화면 응답 없음"], "db": ["PostgreSQL 응답 없음"], "was": ["앱 health 응답 없음"],
                  "time": ["chrony 가 $CHRONY_SERVER 에 붙지 않음"],
                  "syslog": ["rsyslog 검사 줄이", "실행기 uid 줄이"],
                  "sms-agent": ["에이전트가 서지 않음"]}
        restarting = set()
        for path in sorted((DEPLOY / "steps").glob("*.sh")):
            source = path.read_text()
            # 들여 쓴(조건 안의) 재시작도 센다(sms-agent 는 바뀐 것이 있을 때만 다시 띄운다).
            hit = re.search(r"(?m)^\s*(restart_enabled |systemctl restart )", source)
            if path.stem == "lib" or not hit:
                continue
            restarting.add(path.stem)
            after = source[hit.start():]
            with self.subTest(step=path.stem):
                self.assertNotRegex(after, r"(?m)^\s*(sleep\b|for _ in \$\(seq)", "손으로 쓴 기다림 고리")
                for words in wanted.get(path.stem, []):
                    settled = [m.group(0) for m in re.finditer(r'(?ms)^\s*settle [^\n]*(?:\\\n[^\n]*)*', after)]
                    self.assertTrue(any(words in block for block in settled), f"{words} 확인이 settle 밖에 있다")
        self.assertEqual(restarting, set(wanted) | {"firewall", "lldpd", "syslog-forward"})


# ---------------------------------------------------------------- T3 준비(SMS 에이전트, 119 등록, 완료 조건 확인)

LOG_MONITOR_DEFS = DEPLOY / "monitoring" / "log-monitors.json"


class SmsAgentRenderTests(unittest.TestCase):
    """설계 §4, §2-5(SMS 에이전트 설치: 같은 방식, 같은 판, LUCIDA_HOST 는 업무 주소), runbook §11."""

    def test_servers_run_the_agent_after_their_role_and_router_and_loadgen_do_not(self) -> None:
        for path in (T_INV, S_INV):
            for name, bundle in bundles(path).items():
                with self.subTest(inventory=path.name, host=name):
                    if bundle["role"] in ("web", "was", "db"):
                        self.assertEqual(bundle["steps"][-2:], ["sms-agent", "verify"])
                        self.assertLess(bundle["steps"].index(bundle["role"]), bundle["steps"].index("sms-agent"))
                    else:
                        self.assertNotIn("sms-agent", bundle["steps"])
                        self.assertFalse(any(f["step"] == "sms-agent" for f in bundle["files"]))

    def test_agent_env_names_the_business_address_and_reports_to_the_monitor(self) -> None:
        for path in (T_INV, S_INV):
            inv = inventory(path)
            mgmt = next(n for n, v in inv["networks"].items() if v.get("management"))
            for host in inv["hosts"]:
                if host["role"] not in ("web", "was", "db"):
                    continue
                bundle = render.render_host(inv, host)
                env_file = next(f for f in bundle["files"] if f["dest"] == "/opt/lucida-agent/agent.env")
                text = env_file["text"]
                biz = next(n["addr"] for n in host["nics"] if n.get("gateway") and n["net"] != mgmt)
                mgmt_addr = next(n["addr"] for n in host["nics"] if n["net"] == mgmt)
                server = f"http://{inv['monitoring']['address']}:{inv['monitoring']['sms_port']}"
                settings = [line for line in text.splitlines() if line and not line.startswith("#")]
                with self.subTest(inventory=path.name, host=host["name"]):
                    self.assertEqual(settings, [f"LUCIDA_SERVER={server}", f"LUCIDA_HOST={biz}"])
                    self.assertNotIn(mgmt_addr, text)
                    self.assertTrue(text.isascii(), "lucidactl 이 source 하는 파일은 ASCII 만")
                    self.assertEqual(env_file["mode"], "0600")
                    self.assertEqual(bundle["env"]["SMS_AGENT_HOST"], biz)
                    self.assertEqual(bundle["env"]["SMS_SERVER_URL"], server)

    def test_unit_and_paths_carry_no_testbed_word_and_run_the_binary_as_root(self) -> None:
        bundle = bundles(T_INV)["was01"]
        unit = file_of(bundle, "/etc/systemd/system/lucida-agent.service")
        self.assertIn("ExecStart=/opt/lucida-agent/bin/lucida-agent\n", unit)
        self.assertIn("EnvironmentFile=/opt/lucida-agent/agent.env\n", unit)
        self.assertIn("StandardOutput=append:/opt/lucida-agent/logs/agent.log\n", unit)
        self.assertIn("Restart=always\n", unit)
        self.assertNotRegex(unit, r"(?m)^(User|Group)=", "로그 파일을 읽으려면 root 로 돈다")
        names = " ".join([unit, render.SMS_AGENT_HOME, render.SMS_AGENT_UNIT, "lucida-agent"]).lower()
        for word in render.FORBIDDEN_WORDS:
            with self.subTest(word=word):
                self.assertNotRegex(names, rf"(^|[^a-z]){word}([^a-z]|$)")
        self.assertNotIn("opsjobs", unit + file_of(bundle, "/opt/lucida-agent/agent.env"))

    def test_agent_log_is_rotated_with_copytruncate(self) -> None:
        text = file_of(bundles(T_INV)["db01"], "/etc/logrotate.d/lucida-agent")
        self.assertTrue(text.splitlines()[[i for i, l in enumerate(text.splitlines()) if l.endswith("{")][0]].startswith(
            "/opt/lucida-agent/logs/agent.log "))
        for word in ("copytruncate", "missingok", "rotate 7", "maxsize 50M"):
            self.assertIn(word, text)
        self.assertTrue(text.isascii())

    def test_verify_checks_the_agent_only_on_servers(self) -> None:
        source = (DEPLOY / "steps" / "verify.sh").read_text()
        block = source[source.index('if [[ -n "${SMS_AGENT_UNIT:-}" ]]; then'):]
        for name in ("sms-agent-active", "sms-agent-binary", "sms-agent-env"):
            self.assertIn(f"check {name} ", block)
        # 설치 전(기록도 자리도 없음)에만 건너뛰고, 둘 중 하나라도 있으면 세 항목을 모두 본다.
        guard = block.index('if [[ -e "$SMS_AGENT_RECORD" || -e "$SMS_AGENT_HOME" ]]; then')
        skipped = block[block.index("    else\n", guard):block.index("\n    fi\n", guard)]
        self.assertNotIn("check ", skipped)
        self.assertIn("NOT-INSTALLED", skipped)
        self.assertLess(guard, block.index("check sms-agent-active"))
        t = bundles(T_INV)
        self.assertIn("SMS_AGENT_UNIT", t["db01"]["env"])
        self.assertNotIn("SMS_AGENT_UNIT", t["rt-core01"]["env"])
        self.assertNotIn("SMS_AGENT_UNIT", t["loadgen"]["env"])


class SmsAgentApplyTests(unittest.TestCase):
    """apply.sh --sms-agent: 서버의 sms-agent 단계가 돌 때만 바이너리가 있어야 하고, 묶음 artifacts 에 실린다."""

    def _inventory(self, root: Path, path: Path = T_INV) -> Path:
        inv = inventory(path)
        for key in ("executor", "operator"):
            key_file = root / f"{key}.pub"
            key_file.write_text("ssh-ed25519 AAAA test\n")
            inv["accounts"][key]["pubkey_file"] = str(key_file)
        # 운영자 단말 키(설계 §6-2 운영자 경로): 인벤토리에 칸이 있으면 시험 파일로 바꾼다.
        if inv["accounts"]["operator"].get("terminal_pubkey_file"):
            terminal = root / "terminal.pub"
            terminal.write_text("ssh-ed25519 BBBB terminal\n")
            inv["accounts"]["operator"]["terminal_pubkey_file"] = str(terminal)
        for field in ("db_password_file",):
            secret = root / "db"
            secret.write_text("Abcdefgh12345678\n")
            inv["app"][field] = str(secret)
        out = root / path.name
        out.write_text(json.dumps(inv))
        return out

    def _apply(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(DEPLOY / "apply.sh"), *args, "--dry-run"], capture_output=True, text=True)

    def test_full_install_of_a_server_needs_the_agent_binary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            inv = self._inventory(root)
            done = self._apply("--inventory", str(inv), "--host", "web01")
            self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
            self.assertIn("web01 에 --sms-agent 필요", done.stderr)
            agent = root / "lucida-agent"
            agent.write_bytes(b"\x7fELF fake")
            done = self._apply("--inventory", str(inv), "--host", "web01", "--sms-agent", str(agent))
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("./artifacts/lucida-agent", done.stdout)

    def test_the_expected_checksum_is_compared_before_anything_is_sent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            inv = self._inventory(root)
            agent = root / "lucida-agent"
            agent.write_bytes(b"\x7fELF fake")
            good = subprocess.run(["sha256sum", str(agent)], capture_output=True, text=True, check=True).stdout.split()[0]
            done = self._apply("--inventory", str(inv), "--host", "web01", "--sms-agent", str(agent), "--sms-agent-sha256", good)
            self.assertEqual(done.returncode, 0, done.stderr)
            done = self._apply("--inventory", str(inv), "--host", "web01", "--sms-agent", str(agent), "--sms-agent-sha256", "0" * 64)
            self.assertEqual(done.returncode, 2)
            self.assertIn("기대값", done.stderr)
            self.assertNotIn("== web01", done.stdout)

    def test_other_steps_and_other_roles_do_not_need_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            inv = self._inventory(Path(raw))
            done = self._apply("--inventory", str(inv), "--host", "web01", "--steps", "firewall verify")
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertNotIn("artifacts/lucida-agent", done.stdout)
            done = self._apply("--inventory", str(inv), "--host", "web01", "--steps", "sms-agent verify")
            self.assertEqual(done.returncode, 2)


    def test_skip_steps_drops_the_agent_from_every_role_for_a_first_install(self) -> None:
        # 관제 서버 등록 창 전 처음 설치(S 는 ST2): 역할마다 다른 단계 목록을 손으로 적지 않고 sms-agent 만 뺀다.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            inv_path = self._inventory(root, S_INV)
            inv = inventory(S_INV)
            (root / "war").write_bytes(b"war")
            (root / "k6").write_bytes(b"k6")
            hosts = [h["name"] for h in inv["hosts"]]
            args = ["--inventory", str(inv_path), "--war", str(root / "war"), "--k6", str(root / "k6")]
            for name in hosts:
                args += ["--host", name]
            done = self._apply(*args, "--skip-steps", "sms-agent")
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertNotIn("artifacts/lucida-agent", done.stdout)
            shown = dict(re.findall(r"^== (\S+) .*\nsteps: (.*)$", done.stdout, re.M))
            self.assertEqual(set(shown), set(hosts))
            for host in inv["hosts"]:
                wanted = [step for step in render.host_steps(inv, host) if step != "sms-agent"]
                with self.subTest(host=host["name"]):
                    self.assertEqual(shown[host["name"]].split(), wanted)
                    self.assertIn("verify", wanted)
            # 원격 명령은 단계를 따로 주지 않는다(묶음의 단계 목록을 그대로 돈다).
            self.assertRegex(done.stdout, r"install-host\.sh /root/petition-deploy/websvr01 '")
            # 빼지 않으면 서버는 에이전트 바이너리를 요구한다(모든 단계).
            done = self._apply("--inventory", str(inv_path), "--host", "websvr01")
            self.assertEqual(done.returncode, 2)
            self.assertIn("websvr01 에 --sms-agent 필요", done.stderr)

    def test_each_server_gets_its_own_package_index_time_from_the_inventory(self) -> None:
        # 색인 시각은 사람이 명령마다 주지 않고 인벤토리에서 서버마다 읽는다(여러 서버를 한 번에 돌려도 섞이지 않는다).
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            inv_path = self._inventory(root, S_INV)
            (root / "war").write_bytes(b"war")
            args = ["--inventory", str(inv_path), "--war", str(root / "war"), "--skip-steps", "sms-agent"]
            names = ["websvr01", "wassvr01", "wassvr02", "dbsvr01"]
            for name in names:
                args += ["--host", name]
            done = self._apply(*args)
            self.assertEqual(done.returncode, 0, done.stderr)
            shown = dict(re.findall(r"^== (\S+) .*\nsteps: .*\napt-snapshot: (\S+)$", done.stdout, re.M))
            wanted = {h["name"]: h["apt_snapshot"] for h in inventory(S_INV)["hosts"] if h["name"] in names}
            self.assertEqual(shown, wanted)
            self.assertEqual(len(set(wanted.values())), 4)
            # 명령 인자로 주는 길은 없다(인벤토리 하나가 정본).
            done = self._apply(*args, "--apt-snapshot", "20261118T022012Z")
            self.assertEqual(done.returncode, 2)
            self.assertIn("모르는 인자 --apt-snapshot", done.stderr)

    def test_a_server_without_a_package_index_time_is_not_sent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            inv_path = self._inventory(root, S_INV)
            inv = json.loads(inv_path.read_text())
            inv["hosts"][0].pop("apt_snapshot")
            inv_path.write_text(json.dumps(inv))
            done = self._apply("--inventory", str(inv_path), "--host", "websvr01", "--skip-steps", "sms-agent")
            self.assertEqual(done.returncode, 2)
            self.assertIn("websvr01: apt_snapshot", done.stderr)
            self.assertNotIn("== websvr01", done.stdout)

    def test_skip_steps_refuses_unknown_steps_and_explicit_steps(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            inv = self._inventory(Path(raw), S_INV)
            done = self._apply("--inventory", str(inv), "--host", "websvr01", "--skip-steps", "sms-agnet")
            self.assertEqual(done.returncode, 2)
            self.assertIn("설치 단계가 아님", done.stderr)
            self.assertNotIn("== websvr01", done.stdout)
            done = self._apply("--inventory", str(inv), "--host", "websvr01", "--steps", "web verify",
                               "--skip-steps", "sms-agent")
            self.assertEqual(done.returncode, 2)
            self.assertIn("함께 쓰지 않는다", done.stderr)


class SmsAgentStepTests(unittest.TestCase):
    """steps/sms-agent.sh 를 대역 systemctl, pgrep 로 돌린다. 바뀐 것이 없으면 다시 띄우지 않는다(멱등)."""

    HOST = "192.0.2.11"
    SERVER = "http://198.51.100.1:18090"

    def _tree(self, root: Path, env_text: str | None = None) -> tuple[dict, Path]:
        bundle = root / "bundle"
        (bundle / "artifacts").mkdir(parents=True, exist_ok=True)
        (bundle / "files" / "x").mkdir(parents=True, exist_ok=True)
        (bundle / "artifacts" / "lucida-agent").write_bytes(b"agent-binary-v1")
        (bundle / "artifacts" / "SHA256SUMS").write_text(
            subprocess.run(["sha256sum", "lucida-agent"], cwd=bundle / "artifacts", capture_output=True, text=True,
                           check=True).stdout)
        home = root / "opt"
        unitdir = root / "units"
        (bundle / "files" / "x" / "agent.env").write_text(env_text or f"LUCIDA_SERVER={self.SERVER}\nLUCIDA_HOST={self.HOST}\n")
        (bundle / "files" / "x" / "unit").write_text("[Service]\nExecStart=x\n")
        user = subprocess.run(["id", "-un"], capture_output=True, text=True, check=True).stdout.strip()
        group = subprocess.run(["id", "-gn"], capture_output=True, text=True, check=True).stdout.strip()
        (bundle / "files.tsv").write_text(
            f"sms-agent\t{home}/agent.env\t0600\t{user}\t{group}\tfiles/x/agent.env\n"
            f"sms-agent\t{unitdir}/lucida-agent.service\t0644\t{user}\t{group}\tfiles/x/unit\n"
            f"other\t{root}/nope\t0644\t{user}\t{group}\tfiles/x/unit\n")
        bin_dir = root / "bin"
        bin_dir.mkdir(exist_ok=True)
        calls = root / "calls"
        state = root / "active"
        log = home / "logs" / "agent.log"
        # 대역 systemctl: is-active 는 state 파일로, restart 는 FAKE_START 가 ok 이면 시작 줄을 쓰고 state 를 켠다.
        (bin_dir / "systemctl").write_text(
            "#!/bin/sh\n"
            f'echo systemctl "$@" >> {calls}\n'
            'case "$1" in\n'
            f'  is-active) test -f {state} ;;\n'
            f'  restart) if [ "$FAKE_START" = ok ]; then touch {state}; mkdir -p {log.parent}; '
            f'host=$(sed -n "s/^LUCIDA_HOST=//p" {home}/agent.env); '
            f'echo "2026/11/19 INFO  [agent] agent started (version=x, variant=sms, host=$host, server={self.SERVER}, collect interval=60 sec)" >> {log}; fi ;;\n'
            '  *) exit 0 ;;\n'
            'esac\n')
        (bin_dir / "pgrep").write_text(f"#!/bin/sh\ntest -f {state}\n")
        for tool in ("systemctl", "pgrep"):
            (bin_dir / tool).chmod(0o755)
        env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "SMS_AGENT_HOME": str(home), "SMS_AGENT_UNIT": "lucida-agent",
               "SMS_AGENT_RECORD": str(root / "state" / "lucida-agent.sha256"), "SMS_AGENT_HOST": self.HOST,
               "SMS_SERVER_URL": self.SERVER, "SETTLE_EVERY": "0.2", "SMS_AGENT_SETTLE_SEC": "2", "FAKE_START": "ok"}
        return env, bundle

    def _run(self, bundle: Path, env: dict) -> subprocess.CompletedProcess:
        script = f"set -euo pipefail; BUNDLE={bundle}; . {DEPLOY / 'steps' / 'lib.sh'}; . {DEPLOY / 'steps' / 'sms-agent.sh'}"
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)

    @staticmethod
    def _restarts(root: Path) -> int:
        return sum(1 for line in (root / "calls").read_text().splitlines() if line.startswith("systemctl restart"))

    def test_first_run_installs_and_starts_and_a_second_run_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env, bundle = self._tree(root)
            done = self._run(bundle, env)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual((root / "opt" / "bin" / "lucida-agent").read_bytes(), b"agent-binary-v1")
            self.assertTrue(os.access(root / "opt" / "bin" / "lucida-agent", os.X_OK))
            self.assertEqual(stat.S_IMODE((root / "opt" / "agent.env").stat().st_mode), 0o600)
            self.assertTrue((root / "units" / "lucida-agent.service").is_file())
            self.assertFalse((root / "nope").exists(), "다른 단계 파일을 놓음")
            want = subprocess.run(["sha256sum", str(root / "opt" / "bin" / "lucida-agent")], capture_output=True,
                                  text=True, check=True).stdout.split()[0]
            self.assertEqual((root / "state" / "lucida-agent.sha256").read_text().strip(), want)
            self.assertEqual(self._restarts(root), 1)
            self.assertIn("systemctl enable lucida-agent", (root / "calls").read_text())
            again = self._run(bundle, env)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertIn("unchanged, not restarted", again.stdout)
            self.assertEqual(self._restarts(root), 1)

    def test_an_unchanged_rerun_after_log_rotation_still_passes_without_a_restart(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env, bundle = self._tree(root)
            self.assertEqual(self._run(bundle, env).returncode, 0)
            (root / "opt" / "logs" / "agent.log").write_text("")  # copytruncate 뒤
            again = self._run(bundle, env)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertEqual(self._restarts(root), 1)
            (root / "active").unlink()  # 유닛이 멈춰 있으면 같은 설정이어도 다시 띄우고 시작 줄을 본다
            third = self._run(bundle, env)
            self.assertEqual(third.returncode, 0, third.stderr)
            self.assertEqual(self._restarts(root), 2)

    def test_a_changed_setting_or_binary_restarts_once(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env, bundle = self._tree(root)
            self.assertEqual(self._run(bundle, env).returncode, 0)
            (bundle / "artifacts" / "lucida-agent").write_bytes(b"agent-binary-v2")
            (bundle / "artifacts" / "SHA256SUMS").write_text(
                subprocess.run(["sha256sum", "lucida-agent"], cwd=bundle / "artifacts", capture_output=True, text=True,
                               check=True).stdout)
            done = self._run(bundle, env)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(self._restarts(root), 2)
            self.assertEqual((root / "opt" / "bin" / "lucida-agent").read_bytes(), b"agent-binary-v2")

    def test_an_agent_that_does_not_come_up_or_reports_another_name_fails_the_step(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env, bundle = self._tree(root)
            env["FAKE_START"] = "no"
            done = self._run(bundle, env)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("에이전트가 서지 않음", done.stderr)
            self.assertFalse((root / "state" / "lucida-agent.sha256").exists())
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env, bundle = self._tree(root, env_text=f"LUCIDA_SERVER={self.SERVER}\nLUCIDA_HOST=192.0.2.99\n")
            done = self._run(bundle, env)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("이름이나 보고 주소가 다름", done.stderr)

    def test_a_wrong_artifact_checksum_stops_before_anything_is_placed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env, bundle = self._tree(root)
            (bundle / "artifacts" / "lucida-agent").write_bytes(b"tampered")
            done = self._run(bundle, env)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("artifact 체크섬 다름: lucida-agent", done.stderr)
            self.assertFalse((root / "opt").exists())


class LogMonitorDefinitionTests(unittest.TestCase):
    """설계 §4 로그 감시: 일반 패턴을 한 번 정하고 시나리오별로 고치지 않는다, opsjobs.log 는 감시하지 않는다."""

    def setUp(self) -> None:
        self.data = json.loads(LOG_MONITOR_DEFS.read_text(encoding="utf-8"))

    def test_every_server_role_watches_the_design_files(self) -> None:
        files = {role: {m["params"]["fileName"] for m in t3reg.log_monitor_specs(role)} for role in ("web", "was", "db")}
        common = {"/var/log/syslog", "/var/log/auth.log"}
        self.assertEqual(files["web"], common | {"/var/log/apache2/access.log", "/var/log/apache2/error.log"})
        self.assertEqual(files["was"], common | {"/var/log/tomcat10/catalina.out", "/var/log/tomcat10/petition.log",
                                                 "/var/log/tomcat10/localhost.[YYYY]-[MM]-[DD].log",
                                                 "/var/log/tomcat10/localhost_access_log.[YYYY]-[MM]-[DD].txt"})
        self.assertEqual(files["db"], common | {"/var/log/postgresql/postgresql-16-main.log"})
        everything = json.dumps(self.data["common"]) + json.dumps(self.data["roles"])
        self.assertNotIn("opsjobs", everything)

    def test_system_log_patterns_are_the_other_domains_patterns(self) -> None:
        # 기존 세 도메인 작업 노드 감시(119 등록값, 11-18 조회)와 같은 글자. 바꾸려면 이 시험을 함께 고친다.
        common = {m["name"]: m for m in self.data["common"]}
        self.assertEqual(common["syslog"]["errorPattern"], r".*\b([Ee]rror|ERROR|[Ff]ailed|FAILED|[Ff]ailure)\b.*")
        self.assertEqual(common["syslog"]["fatalPattern"], ".*(kernel panic|I/O error|Out of memory|oom-kill|segfault).*")
        self.assertEqual(common["syslog"]["warnPattern"], r".*\b([Ww]arn(ing)?|WARN)\b.*")
        self.assertEqual(common["auth-log"]["errorPattern"], ".*([Ff]ailed password|authentication failure|[Ii]nvalid user).*")
        self.assertEqual(common["auth-log"]["warnPattern"], ".*(session opened for user root|sudo:.*COMMAND).*")

    def test_patterns_compile_without_constructs_go_regexp_lacks(self) -> None:
        for role in ("web", "was", "db"):
            for spec in t3reg.log_monitor_specs(role):
                for key, value in spec["params"].items():
                    if not key.endswith("Pattern") or not value:
                        continue
                    with self.subTest(role=role, name=spec["name"], key=key):
                        re.compile(value)
                        self.assertNotRegex(value, r"\(\?[=!<]|\\[1-9]", "에이전트(Go regexp) 가 못 읽는 꼴")

    def test_patterns_match_the_lines_they_are_for_and_not_ordinary_lines(self) -> None:
        cases = {
            ("web", "apache-access"): ('10.0.0.1 - - [19/Nov/2026:15:00:00 +0000] "GET /x HTTP/1.1" 502 123 "-" "Mozilla"',
                                       '10.0.0.1 - - [19/Nov/2026:15:00:00 +0000] "GET /x HTTP/1.1" 200 5023 "-" "Mozilla"'),
            ("web", "apache-error"): ("[Thu Nov 19 15:00:00.1 2026] [proxy:error] [pid 1:tid 2] AH00957: connect failed",
                                      "[Thu Nov 19 15:00:00.1 2026] [mpm_event:notice] [pid 1:tid 2] AH00489: resuming"),
            ("was", "app-log"): ("2026-11-19 15:00:00.000 ERROR [http-nio-8080-exec-1] c.c.w.Portal - db down",
                                 "2026-11-19 15:00:00.000 INFO  [main] c.c.w.Portal - started"),
            ("was", "tomcat-access"): ('10.0.0.2 - - [19/Nov/2026:15:00:00 +0000] "GET /petition/api/x HTTP/1.1" 500 12',
                                       '10.0.0.2 - - [19/Nov/2026:15:00:00 +0000] "GET /petition/api/x HTTP/1.1" 200 12'),
            ("db", "postgresql"): ("2026-11-19 15:00:00 UTC [1] FATAL:  terminating connection",
                                   "2026-11-19 15:00:00 UTC [1] LOG:  connection authorized: user=x"),
        }
        for (role, name), (hit, miss) in cases.items():
            spec = next(s for s in t3reg.log_monitor_specs(role) if s["name"] == name)["params"]
            patterns = [spec[f"{k}Pattern"] for k in t3reg.LOG_PATTERN_KEYS if spec[f"{k}Pattern"]]
            with self.subTest(role=role, name=name):
                self.assertTrue(any(re.fullmatch(p, hit) for p in patterns))
                self.assertFalse(any(re.fullmatch(p, miss) for p in patterns))


class FakeProduct:
    """제품 API 대역. 경로와 몸통을 기록하고, 등록 대기 서버, 하위 대상, 감시, 망 대상을 상태로 쥔다.

    응답 꼴은 lucida-next d0186576d2 의 실제 직렬화를 따른다: 대상 목록(include_collectors=1)은 대상에 수집기 칸이 없고
    최상위 target_collectors 에 {"target", "collectors"} 쌍(query/handler/targets.go, store.Collector 의 json 칸),
    하위 트리는 {"root_id", "revision", "nodes"}(store.TargetSubtree, 뿌리 노드 포함), 감시 목록은 {"custom_monitors", "total"}
    이고 감시는 id 와 params(객체)를 가진다(collector-sms store.CustomMonitor).
    """

    def __init__(self, inv: dict) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.pending: dict[str, dict] = {}
        self.approved: dict[str, dict] = {}
        self.nodes: dict[str, list[dict]] = {}
        self.monitors: dict[str, list[dict]] = {}
        self.network: list[dict] = []
        self.collectors: dict[str, list[dict]] = {}
        self.fail: set[str] = set()
        self.next = 0
        for host in t3reg.servers(inv):
            tid = self._new()
            self.pending[host["agent_host"]] = {"id": tid, "name": host["agent_host"], "address": host["agent_host"],
                                                "registration_status": "pending"}
            self.nodes[tid] = [{"id": tid, "resource_type": "sms.server"},
                               {"id": self._new(), "resource_type": "sms.network_interface", "resource_key": host["biz_if"],
                                "management_status": "managed"},
                               {"id": self._new(), "resource_type": "sms.network_interface", "resource_key": host["mgmt_if"],
                                "management_status": "managed"},
                               {"id": self._new(), "resource_type": "sms.file_system", "resource_key": "/"}]

    def _new(self) -> str:
        self.next += 1
        return f"00000000-0000-4000-8000-{self.next:012d}"

    def login(self, user, password) -> None:
        self.calls.append(("LOGIN", user, None))

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        parsed = urllib.parse.urlparse(path)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        if method in self.fail or path in self.fail:
            return 500, {"error": "boom"}
        if method == "GET" and parsed.path == "/api/v1/targets":
            if query.get("type") == "server":
                pool = self.pending if query.get("registration_status") == "pending" else self.approved
                return 200, {"targets": [t for t in pool.values() if query.get("search", "") in t["name"]]}
            found = [t for t in self.network if query.get("search", "") in (t.get("address") or t["name"])]
            body = {"targets": found}
            if query.get("include_collectors") == "1":
                body["target_collectors"] = [{"target": t, "collectors": self.collectors.get(t["id"], [])} for t in found]
            return 200, body
        if method == "POST" and parsed.path == "/api/v1/targets/approve":
            for tid in body["ids"]:
                name = next(n for n, t in self.pending.items() if t["id"] == tid)
                self.approved[name] = dict(self.pending.pop(name), registration_status="approved")
            return 200, {"approved_count": len(body["ids"])}
        if method == "GET" and parsed.path.endswith("/subtree"):
            root = parsed.path.split("/")[4]
            return 200, {"root_id": root, "revision": "1", "nodes": self.nodes.get(root, [])}
        if method == "PUT" and "/authz/nodes/target/" in parsed.path:
            for nodes in self.nodes.values():
                for node in nodes:
                    if node["id"] == parsed.path.split("/")[6]:
                        node["management_status"] = body["state"]
            return 200, {}
        if method == "GET" and parsed.path == "/sm/api/custom-monitors":
            return 200, {"custom_monitors": self.monitors.get(query["parent"], []), "total": 0}
        if method == "POST" and parsed.path == "/sm/api/custom-monitors":
            item = {"id": self._new(), "params": body["params"]}
            self.monitors.setdefault(body["parent_target_id"], []).append(item)
            return 201, item
        if method == "POST" and parsed.path == "/api/v1/targets":
            item = {"id": self._new(), "type": body["type"], "name": body["name"], "address": body.get("address", ""),
                    "status": "active", "registration_status": "approved", "meta": body.get("meta", {})}
            self.network.append(item)
            return 201, item
        if method == "POST" and parsed.path.endswith("/collectors"):
            tid = parsed.path.split("/")[4]
            cid = self._new()
            self.collectors.setdefault(tid, []).append({"id": cid, "target_id": tid, "kind": body["kind"],
                                                        "enabled": body["enabled"], "config": body["config"],
                                                        "storage_mode": "", "created_at": "2026-11-19T06:00:00Z"})
            return 201, {"id": cid, "target_id": tid, "kind": body["kind"]}
        if method == "DELETE":
            tid = parsed.path.split("/")[-1]
            self.network = [t for t in self.network if t["id"] != tid]
            self.collectors.pop(tid, None)
            return 204, None
        return 404, {"error": f"no route {method} {path}"}

    def writes(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p, _ in self.calls if m in ("POST", "PUT", "DELETE")]


class T3RegisterTests(unittest.TestCase):
    """runbook §12 의 119 등록 도구(제품 API 대역으로)."""

    def setUp(self) -> None:
        self.inv = inventory(T_INV)
        self.product = FakeProduct(self.inv)
        self.out = io.StringIO()

    def session(self, yes: bool = True):
        return t3reg.Session(self.product, yes, self.out)

    def test_approve_finds_every_pending_server_by_its_business_address_in_one_call(self) -> None:
        ids = t3reg.phase_approve(self.session(), self.inv)
        self.assertEqual(len(ids), 4)
        posts = [c for c in self.product.calls if c[0] == "POST"]
        self.assertEqual(posts, [("POST", "/api/v1/targets/approve", {"ids": ids})])
        self.assertEqual(t3reg.phase_approve(self.session(), self.inv), [])
        self.assertEqual(len([c for c in self.product.calls if c[0] == "POST"]), 1, "다시 돌려도 승인 호출 없음")

    def test_approve_refuses_a_pending_server_whose_address_is_not_the_business_one(self) -> None:
        name = next(iter(self.product.pending))
        self.product.pending[name]["address"] = "192.0.2.200"
        with self.assertRaisesRegex(t3reg.RegisterError, "업무 주소가 아님"):
            t3reg.phase_approve(self.session(), self.inv)
        self.assertEqual(self.product.writes(), [])

    def test_without_yes_nothing_is_written(self) -> None:
        t3reg.phase_approve(self.session(yes=False), self.inv)
        self.assertEqual(self.product.writes(), [])
        self.assertIn("(찍기만) POST /api/v1/targets/approve", self.out.getvalue())

    def test_nic_phase_accepts_the_management_nic_and_writes_nothing(self) -> None:
        # 11-19 지휘 세션 결정(v4 MNIC): 관리 NIC 수집은 받아들임. «관리 안 함» PUT 은 제품 승격이 행을 지우고 managed 로
        # 다시 만들게 해(가용성 이력도 지움) 쓰지 않는다.
        t3reg.phase_approve(self.session(), self.inv)
        before = len(self.product.writes())
        done = t3reg.phase_nic(self.session(), self.inv)
        mgmt = {n["id"] for nodes in self.product.nodes.values() for n in nodes
                if n.get("resource_type") == "sms.network_interface" and n.get("resource_key") == "ens19"}
        self.assertEqual(set(done), mgmt)
        self.assertEqual(len(done), 4)
        self.assertEqual(self.product.writes()[before:], [])
        self.assertFalse([c for c in self.product.calls if c[0] == "PUT" or "/authz/" in c[1]])
        states = {n.get("resource_key"): n.get("management_status") for nodes in self.product.nodes.values() for n in nodes
                  if n.get("resource_type") == "sms.network_interface"}
        self.assertEqual(states, {"ens18": "managed", "ens19": "managed"})
        self.assertEqual(self.out.getvalue().count("ens19 받아들임"), 4)
        # 누가 이미 관리 안 함으로 두었어도 되돌리거나 다시 쓰지 않는다.
        for nodes in self.product.nodes.values():
            for node in nodes:
                if node.get("resource_key") == "ens19":
                    node["management_status"] = "unmanaged"
        t3reg.phase_nic(self.session(), self.inv)
        self.assertEqual(self.product.writes()[before:], [])
        self.assertIn("관리 안 함이어도 손대지 않는다", self.out.getvalue())

    def test_nic_phase_waits_for_the_child_target(self) -> None:
        t3reg.phase_approve(self.session(), self.inv)
        for nodes in self.product.nodes.values():
            nodes[:] = [n for n in nodes if n.get("resource_key") != "ens19"]
        with self.assertRaisesRegex(t3reg.RegisterError, "첫 지표를 기다려 다시"):
            t3reg.phase_nic(self.session(), self.inv)

    def test_log_monitors_follow_the_role_file_and_a_rerun_adds_nothing(self) -> None:
        t3reg.phase_approve(self.session(), self.inv)
        added = t3reg.phase_log_monitors(self.session(), self.inv)
        self.assertEqual(added, 4 + 6 + 6 + 3)
        bodies = [b for m, p, b in self.product.calls if m == "POST" and p == "/sm/api/custom-monitors"]
        for body in bodies:
            self.assertEqual(body["kind"], "log")
            self.assertTrue(body["enabled"])
            self.assertEqual(body["params"]["matchingType"], "Exact")
            self.assertEqual(body["params"]["scanType"], "End")
            self.assertNotRegex(body["name"].lower(), r"(^|[^a-z])(test|testbed|tb|t4)([^a-z]|$)")
        self.assertEqual(t3reg.phase_log_monitors(self.session(), self.inv), 0)

    def test_nms_registration_never_prints_the_community_and_undoes_a_half_registration(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            community = Path(raw) / "community"
            community.write_text("s3cretCommunityValue\n")
            self.inv["monitoring"]["snmp_community_file"] = str(community)
            t3reg.phase_nms(self.session(yes=False), self.inv)
            self.assertNotIn("s3cretCommunityValue", self.out.getvalue())
            tid = t3reg.phase_nms(self.session(), self.inv)
            collector = next(b for m, p, b in self.product.calls if m == "POST" and p.endswith("/collectors"))
            self.assertEqual(collector["config_encrypted"], {"community": "s3cretCommunityValue"})
            self.assertEqual(collector["config"], {"port": 161, "version": "2c"})
            self.assertEqual(collector["collection_policy_id"], t3reg.NMS_DEFAULT_POLICY)
            target = next(t for t in self.product.network if t["id"] == tid)
            self.assertEqual(target["address"], "192.168.123.254")
            self.assertNotIn("s3cretCommunityValue", self.out.getvalue())
            self.assertEqual(t3reg.phase_nms(self.session(), self.inv), tid, "이미 등록됨")
            self.product.network.clear()
            self.product.fail.add("POST /collectors")
            self.product.request = self._failing_collectors(self.product.request)
            with self.assertRaises(t3reg.RegisterError):
                t3reg.phase_nms(self.session(), self.inv)
            self.assertEqual(self.product.writes()[-1][0], "DELETE")

    @staticmethod
    def _failing_collectors(request):
        def wrapped(method, path, body=None):
            if method == "POST" and path.endswith("/collectors"):
                return 500, {"error": "boom"}
            return request(method, path, body)
        return wrapped

    def test_trap_receiver_is_udp_162_and_not_added_twice(self) -> None:
        tid = t3reg.phase_trap(self.session())
        collector = next(b for m, p, b in self.product.calls if m == "POST" and p.endswith("/collectors"))
        self.assertEqual((collector["kind"], collector["config"]["protocol"], collector["config"]["port"]), ("polestar_trap", "udp", 162))
        self.assertFalse(collector["config"]["allow_unregistered"])
        self.assertEqual(t3reg.phase_trap(self.session()), tid)
        self.assertEqual(len([c for c in self.product.calls if c[0] == "POST"]), 2)

    def test_record_then_rollback_deletes_in_reverse_and_servers_last(self) -> None:
        t3reg.phase_approve(self.session(), self.inv)
        t3reg.phase_nic(self.session(), self.inv)
        t3reg.phase_log_monitors(self.session(), self.inv)
        t3reg.phase_trap(self.session())
        table = t3reg.phase_record(self.session(), self.inv)
        self.assertEqual(table["schema"], "petition-portal-targets/1")
        self.assertEqual(len(table["servers"]), 4)
        self.assertEqual({s["agent_host"] for s in table["servers"]}, set(self.product.approved))
        self.assertTrue(all("sms.network_interface" in s["children"] for s in table["servers"]))
        for rec in table["servers"]:
            self.assertNotIn("unmanaged", rec)
            self.assertEqual(rec["management_nic"], {"ifname": "ens19", "decision": "accepted",
                                                     "target_id": rec["children"]["sms.network_interface"]["ens19"]})
        self.product.calls.clear()
        n = t3reg.phase_rollback(self.session(), table)
        deletes = [p for m, p, _ in self.product.calls if m == "DELETE"]
        self.assertEqual(n, len(deletes))
        self.assertFalse([p for p in deletes if "/authz/" in p], "관리 NIC 는 관리 안 함으로 두지 않아 지울 경계가 없다")
        self.assertEqual(n, 1 + sum(len([v for v in r["log_monitors"].values() if v]) for r in table["servers"]) + 4)
        server_ids = {s["target_id"] for s in table["servers"]}
        first_server = next(i for i, p in enumerate(deletes) if p.split("/")[-1] in server_ids)
        self.assertTrue(all(p.split("/")[-1] in server_ids for p in deletes[first_server:]), "서버 대상은 맨 끝")
        self.assertTrue(deletes[0].endswith(table["trap_receivers"][0]["target_id"]))

    def test_rollback_of_an_old_table_does_not_touch_its_unmanaged_list(self) -> None:
        # 11-19 이전 표(관리 NIC 를 관리 안 함으로 둔 때)의 "unmanaged" 목록: 그 선언은 제품 승격이 이미 지웠다(v4 MNIC).
        t3reg.phase_approve(self.session(), self.inv)
        table = t3reg.phase_record(self.session(), self.inv)
        for rec in table["servers"]:
            rec.pop("management_nic")
            rec["unmanaged"] = [rec["children"]["sms.network_interface"]["ens19"]]
        self.product.calls.clear()
        t3reg.phase_rollback(self.session(), table)
        self.assertFalse([p for m, p, _ in self.product.calls if "/authz/" in p])
        self.assertEqual([p.split("/")[-1] for m, p, _ in self.product.calls if m == "DELETE"],
                         [r["target_id"] for r in table["servers"]])

    def _community(self, raw: str) -> None:
        community = Path(raw) / "community"
        community.write_text("s3cretCommunityValue\n")
        self.inv["monitoring"]["snmp_community_file"] = str(community)

    def test_monitor_form_follows_the_other_domains(self) -> None:
        # 11-18 지휘 세션 결정: unified_log_enabled, log_type_tag 는 기존 세 도메인 감시와 같게(syslog, auth.log 는 system,
        # DB 서버 로그는 database, 앱 파일은 태그 없음).
        t3reg.phase_approve(self.session(), self.inv)
        t3reg.phase_log_monitors(self.session(), self.inv)
        bodies = {b["params"]["fileName"]: b for m, p, b in self.product.calls if m == "POST" and p == "/sm/api/custom-monitors"}
        self.assertTrue(all(b["unified_log_enabled"] is True for b in bodies.values()))
        self.assertEqual(bodies["/var/log/syslog"]["log_type_tag"], "system")
        self.assertEqual(bodies["/var/log/auth.log"]["log_type_tag"], "system")
        self.assertEqual(bodies["/var/log/postgresql/postgresql-16-main.log"]["log_type_tag"], "database")
        self.assertNotIn("log_type_tag", bodies["/var/log/apache2/access.log"])

    def test_no_phase_writes_without_yes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            self._community(raw)
            t3reg.phase_approve(self.session(), self.inv)
            t3reg.phase_trap(self.session())
            t3reg.phase_nms(self.session(), self.inv)
            table = t3reg.phase_record(self.session(), self.inv)
            for nodes in self.product.nodes.values():
                for node in nodes:
                    node.pop("management_status", None)
            before = len(self.product.writes())
            dry = self.session(yes=False)
            t3reg.phase_nic(dry, self.inv)
            t3reg.phase_log_monitors(dry, self.inv)
            self.product.network.clear()
            t3reg.phase_nms(dry, self.inv)
            t3reg.phase_trap(dry)
            t3reg.phase_rollback(dry, table)
            self.assertEqual(len(self.product.writes()), before, self.product.writes()[before:])
            self.assertGreater(len(dry.writes), 10)
            self.assertNotIn("s3cretCommunityValue", self.out.getvalue())

    def test_an_nms_device_left_without_its_collector_is_not_reported_as_registered(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            self._community(raw)
            tid = t3reg.phase_nms(self.session(), self.inv)
            self.product.collectors.pop(tid)
            with self.assertRaisesRegex(t3reg.RegisterError, "polestar_nms 수집기가 있는 하나가 아님"):
                t3reg.phase_nms(self.session(), self.inv)

    def test_a_failed_undo_reports_both_reasons(self) -> None:
        request = self.product.request

        def failing(method, path, body=None):
            if method == "POST" and path.endswith("/collectors"):
                return 500, {"error": "collector boom"}
            if method == "DELETE":
                return 403, {"error": "function auth required"}
            return request(method, path, body)
        self.product.request = failing
        with self.assertRaises(t3reg.RegisterError) as caught:
            t3reg.phase_trap(self.session())
        message = str(caught.exception)
        self.assertIn("collector boom", message)
        self.assertIn("지우지 못함", message)
        self.assertIn("function auth required", message)

    def test_trap_receiver_is_found_again_recorded_and_rolled_back_in_the_real_list_shape(self) -> None:
        t3reg.phase_approve(self.session(), self.inv)
        tid = t3reg.phase_trap(self.session())
        self.assertEqual(t3reg.phase_trap(self.session()), tid)
        self.assertEqual(sum(1 for m, p, _ in self.product.calls if m == "POST" and p == "/api/v1/targets"), 1)
        table = t3reg.phase_record(self.session(), self.inv)
        self.assertEqual([r["target_id"] for r in table["trap_receivers"]], [tid])
        t3reg.phase_rollback(self.session(), table)
        self.assertEqual(t3reg._trap_receivers(self.session()), [])

    def test_a_list_without_target_collectors_stops_instead_of_finding_nothing(self) -> None:
        request = self.product.request

        def old_shape(method, path, body=None):
            status, resp = request(method, path, body)
            if method == "GET" and isinstance(resp, dict):
                resp.pop("target_collectors", None)
            return status, resp
        self.product.request = old_shape
        with self.assertRaisesRegex(t3reg.RegisterError, "target_collectors"):
            t3reg.phase_trap(self.session())

    def test_main_needs_the_runner_account_in_the_environment(self) -> None:
        with unittest.mock.patch.dict(os.environ, {"LUCIDA_LOGIN_USER": "", "LUCIDA_LOGIN_PASSWORD": ""}):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(t3reg.main(["--inventory", str(T_INV), "--phase", "approve"],
                                            client_factory=lambda base: self.product), 2)
        self.assertIn("LUCIDA_LOGIN_USER", err.getvalue())



class OriginGate:
    """query corsMiddleware 판정을 흉내 낸 로컬 HTTP 대역(lucida-next backend/services/query/main.go corsMiddleware,
    backend/pkg/originguard Allowed 는 목록에 글자로 같은 값이 있는지만 본다).

    GET, HEAD, OPTIONS 가 아니고 Cookie 머리가 있는데 Origin 이 없거나 허용 목록에 없으면 403 본문 "forbidden origin".
    로그인(POST /api/v1/login)은 쿠키가 아직 없어 이 판정을 받지 않고 세션 쿠키를 준다. 받은 (메서드, 경로, Origin) 을 적는다.
    """

    def __init__(self, allowed_hosts: tuple[str, ...] = ("127.0.0.1",)) -> None:
        import http.server
        import threading
        gate = self
        self.seen: list[tuple[str, str, str | None]] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def _reply(self, status: int, body: bytes, ctype: str = "application/json", cookie: bool = False) -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                if cookie:
                    self.send_header("Set-Cookie", "lucida_session=s1; Path=/; HttpOnly")
                self.end_headers()
                self.wfile.write(body)

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                origin = self.headers.get("Origin")
                gate.seen.append((self.command, self.path, origin))
                unsafe = self.command not in ("GET", "HEAD", "OPTIONS")
                if unsafe and self.headers.get("Cookie") and (not origin or origin not in gate.allowed):
                    self._reply(403, b"forbidden origin\n", "text/plain; charset=utf-8")
                    return
                if self.command == "POST" and self.path == "/api/v1/login":
                    self._reply(200, b'{"ok":true}', cookie=True)
                elif self.command == "POST" and self.path == "/api/v1/targets":
                    self._reply(201, b'{"id":"00000000-0000-4000-8000-000000000001"}')
                elif self.command == "POST":
                    self._reply(201, b'{"id":"00000000-0000-4000-8000-000000000002"}')
                elif self.command == "GET":
                    self._reply(200, b'{"targets":[],"target_collectors":[]}')
                else:
                    self._reply(204, b"")

            do_GET = do_POST = do_PUT = do_DELETE = _handle

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        # 설치기 파생 꼴(http 게이트웨이 포트, https 포트) 그대로.
        self.allowed = {f"http://{h}:{self.port}" for h in allowed_hosts} | {f"https://{h}:13443" for h in allowed_hosts}
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05})
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


class T3RegisterOriginTests(unittest.TestCase):
    """등록 도구의 쓰기 호출이 query Origin 판정을 통과하는지(11-19 119 첫 --yes 가 403 forbidden origin 으로 멈춤)."""

    def setUp(self) -> None:
        self.gate = OriginGate()
        self.addCleanup(self.gate.close)
        env = unittest.mock.patch.dict(os.environ, {})
        env.start()
        self.addCleanup(env.stop)
        for key in (t3reg.ORIGIN_ENV, "http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
            os.environ.pop(key, None)

    def test_origin_is_scheme_host_port_like_the_installer_derives_it(self) -> None:
        cases = [
            # 119: LUCIDA_QUERY_URL 과 query-env LUCIDA_ALLOWED_ORIGINS 첫 값(11-19 조회)이 같다.
            ("http://192.168.230.119:13080", "http://192.168.230.119:13080"),
            ("http://192.168.230.119:13080/", "http://192.168.230.119:13080"),
            ("http://192.168.230.119:13080/api", "http://192.168.230.119:13080"),
            ("HTTPS://Polestar.Example:13443", "https://polestar.example:13443"),
            ("http://h.example:80", "http://h.example"),
            ("https://h.example:443/x", "https://h.example"),
            ("http://h.example", "http://h.example"),
            ("http://[fd00::1]:13080", "http://[fd00::1]:13080"),
            ("http://user@h.example:13080", "http://h.example:13080"),
        ]
        for base, want in cases:
            with self.subTest(base=base):
                self.assertEqual(t3reg.web_origin(base), want)
        self.assertIn(t3reg.web_origin("http://192.168.230.119:13080"),
                      "http://192.168.230.119:13080,https://192.168.230.119:13443".split(","))

    def test_bad_base_or_override_stops(self) -> None:
        for base in ("ftp://h.example", "192.168.230.119:13080", "http://"):
            with self.subTest(base=base), self.assertRaises(t3reg.RegisterError):
                t3reg.web_origin(base)
        for override in ("http://h.example:13080/", "http://h.example:13080/api", "h.example:13080",
                         "http://u@h.example:13080", "http://h.example:13080?x=1"):
            with self.subTest(override=override), self.assertRaisesRegex(t3reg.RegisterError, t3reg.ORIGIN_ENV):
                t3reg.web_origin("http://h.example:13080", override)
        self.assertEqual(t3reg.web_origin("http://a.example:13080", "https://b.example:13443"), "https://b.example:13443")

    def test_writes_pass_the_gate_and_every_call_carries_the_same_origin(self) -> None:
        client = t3reg.Client(self.gate.base, timeout=5)
        client.login("u", "p")
        session = t3reg.Session(client, True, io.StringIO())
        session.get("/api/v1/targets?type=network")
        status, body = session.write("POST", "/api/v1/targets", {"type": "network", "name": "trap-udp-162"})
        self.assertEqual(status, 201)
        self.assertEqual(body, {"id": "00000000-0000-4000-8000-000000000001"})
        session.write("DELETE", "/api/v1/targets/00000000-0000-4000-8000-000000000001")
        self.assertEqual([m for m, _, _ in self.gate.seen], ["POST", "GET", "POST", "DELETE"])
        self.assertEqual({o for _, _, o in self.gate.seen}, {self.gate.base})

    def test_the_gate_refuses_a_write_without_origin_like_119_did(self) -> None:
        client = t3reg.Client(self.gate.base, timeout=5)
        client.login("u", "p")
        status, body = client.request("GET", "/api/v1/targets")
        self.assertEqual(status, 200)
        bare = urllib.request.Request(self.gate.base + "/api/v1/targets", data=b"{}", method="POST",
                                      headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            client.opener.open(bare, timeout=5)
        self.assertEqual(caught.exception.code, 403)
        self.assertEqual(caught.exception.read().strip(), b"forbidden origin")
        self.assertEqual(self.gate.seen[-1], ("POST", "/api/v1/targets", None))

    def test_a_wrong_override_is_refused_and_the_tool_reports_403_forbidden_origin(self) -> None:
        os.environ[t3reg.ORIGIN_ENV] = "http://192.168.230.119:13080"
        client = t3reg.Client(self.gate.base, timeout=5)
        self.assertEqual(client.origin, "http://192.168.230.119:13080")
        client.login("u", "p")
        session = t3reg.Session(client, True, io.StringIO())
        with self.assertRaisesRegex(t3reg.RegisterError, "403 forbidden origin"):
            session.write("POST", "/api/v1/targets", {"type": "network"})

    def test_a_right_override_passes(self) -> None:
        gate = OriginGate(allowed_hosts=("localhost",))
        self.addCleanup(gate.close)
        os.environ[t3reg.ORIGIN_ENV] = f"http://localhost:{gate.port}"
        client = t3reg.Client(gate.base, timeout=5)
        client.login("u", "p")
        status, _ = t3reg.Session(client, True, io.StringIO()).write("POST", "/api/v1/targets", {"type": "network"})
        self.assertEqual(status, 201)
        self.assertEqual(gate.seen[-1], ("POST", "/api/v1/targets", f"http://localhost:{gate.port}"))

    def test_main_runs_a_phase_through_the_gate_and_prints_the_origin(self) -> None:
        os.environ.update({"LUCIDA_QUERY_URL": self.gate.base, "LUCIDA_LOGIN_USER": "u", "LUCIDA_LOGIN_PASSWORD": "p"})
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            rc = t3reg.main(["--inventory", str(T_INV), "--phase", "trap", "--yes"])
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn(f"Origin {self.gate.base}", out.getvalue())
        posts = [(p, o) for m, p, o in self.gate.seen if m == "POST" and p != "/api/v1/login"]
        self.assertEqual(posts, [("/api/v1/targets", self.gate.base),
                                 ("/api/v1/targets/00000000-0000-4000-8000-000000000001/collectors", self.gate.base)])
        self.assertIn("trap 수신기 00000000-0000-4000-8000-000000000001", out.getvalue())

    def test_main_stops_on_a_malformed_override_before_logging_in(self) -> None:
        os.environ.update({"LUCIDA_QUERY_URL": self.gate.base, "LUCIDA_LOGIN_USER": "u", "LUCIDA_LOGIN_PASSWORD": "p",
                           t3reg.ORIGIN_ENV: "http://h.example:13080/"})
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            rc = t3reg.main(["--inventory", str(T_INV), "--phase", "trap", "--yes"])
        self.assertEqual(rc, 2)
        self.assertIn(t3reg.ORIGIN_ENV, err.getvalue())
        self.assertEqual(self.gate.seen, [])


class NmsVmStore:
    """VictoriaMetrics 대역: 계열(라벨, 표본 시각)을 쥐고 t3_check NMS 행이 쓰는 꼴의 질의를 실제 의미로 푼다.

    선택자의 =, =~ 라벨 맞춤, last_over_time(창 안 표본이 있는 계열), scrape_interval(창 안 표본 2개 이상인 계열의 이웃 간격 평균),
    count, count by (라벨 하나), max. 모르는 꼴의 질의는 시험을 멈춘다(substring 대역이 축이 틀린 질의를 통과시킨 일, 11-19).
    """

    QUERY = re.compile(r'^(?P<agg>count by \((?P<by>\w+)\) |count|max)\s*\((?P<fn>last_over_time|scrape_interval)'
                       r'\(\{(?P<sel>[^}]*)\}\[(?P<w>\d+)s\]\)\)$')
    MATCHER = re.compile(r'(\w+)(=~|=)"((?:[^"\\]|\\.)*)"')

    def __init__(self) -> None:
        self.series: list[tuple[dict, list[float]]] = []
        self.pg_rules: list[tuple[str, list]] = []
        self.queries: list[str] = []

    def add(self, labels: dict, times: list[float]) -> None:
        if times:
            self.series.append((labels, list(times)))

    def pg(self, sql):
        return next((answer for piece, answer in self.pg_rules if piece in sql), [])

    def _match(self, labels: dict, sel: str) -> bool:
        for name, op, raw in self.MATCHER.findall(sel):
            value = raw.replace("\\\\", "\\")
            got = labels.get(name, "")
            if (op == "=" and got != value) or (op == "=~" and not re.fullmatch(value, got)):
                return False
        return True

    def vm(self, path, params):
        query = params["query"]
        self.queries.append(query)
        at = float(params["time"])
        m = self.QUERY.match(query)
        assert path == "/api/v1/query" and m, f"대역이 모르는 질의: {query}"
        window = int(m["w"])
        per = []
        for labels, times in self.series:
            if not self._match(labels, m["sel"]):
                continue
            inside = [t for t in times if at - window <= t <= at]
            if m["fn"] == "last_over_time" and inside:
                per.append((labels, 1.0))
            elif m["fn"] == "scrape_interval" and len(inside) >= 2:
                per.append((labels, (inside[-1] - inside[0]) / (len(inside) - 1)))
        if m["agg"] == "count":
            return {"result": [{"metric": {}, "value": [at, str(len(per))]}] if per else []}
        if m["agg"] == "max":
            return {"result": [{"metric": {}, "value": [at, str(max(v for _, v in per))]}] if per else []}
        groups = collections.Counter(labels.get(m["by"], "") for labels, _ in per)
        return {"result": [{"metric": {m["by"]: n}, "value": [at, str(c)]} for n, c in sorted(groups.items())]}

class FakeRemote:
    """t3_check 의 읽기 통로 대역. 질의 글자에 든 조각으로 답을 고른다."""

    def __init__(self) -> None:
        self.pg_rules: list[tuple[str, list]] = []
        self.ch_rules: list[tuple[str, list]] = []
        self.vm_rules: list[tuple[str, list]] = []
        self.queries: list[str] = []

    def _pick(self, rules, text):
        self.queries.append(text)
        for piece, answer in rules:
            if callable(piece):
                got = piece(text)
                if got is not None:
                    return got
            elif piece in text:
                return answer
        return []

    def pg(self, sql):
        return self._pick(self.pg_rules, sql)

    def ch(self, sql):
        return self._pick(self.ch_rules, sql)

    def vm(self, path, params):
        # 실제 통로(Remote.vm)는 응답의 data 를 돌려준다: /api/v1/series 는 계열 목록, /api/v1/query 는 {"result": [...]}.
        answer = self._pick(self.vm_rules, params.get("query") or params.get("match[]", ""))
        return answer if path == "/api/v1/series" else {"result": answer}

    def host(self, command):
        return ""

    def guest(self, addr, command):
        self.queries.append(command)
        return self.guest_answer

    guest_answer = ""


class T3CheckTests(unittest.TestCase):
    """runbook §13 의 T3 완료 조건 확인 도구(판정 함수와 읽기 대역)."""

    def setUp(self) -> None:
        self.ctx = t3check.context(inventory(T_INV))
        self.until = datetime.datetime(2026, 11, 20, 15, 0, tzinfo=datetime.timezone.utc)
        self.since = self.until - datetime.timedelta(hours=1)

    def test_context_reads_servers_router_and_loadgen_from_the_inventory(self) -> None:
        self.assertEqual([s["name"] for s in self.ctx["servers"]], ["web01", "was01", "was02", "db01"])
        self.assertEqual(self.ctx["router"]["addr"], "192.168.123.254")
        self.assertEqual(self.ctx["router"]["downlinks"], ["ens19", "ens20", "ens21"])
        self.assertEqual(next(s for s in self.ctx["servers"] if s["role"] == "db")["data_mount"], "/pgdata")
        s_ctx = t3check.context(inventory(S_INV))
        self.assertIsNone(s_ctx["router"])
        self.assertEqual(t3check.item_nms(FakeRemote(), s_ctx, {"router": None}, self.until).verdict, "해당 없음")

    def test_syslog_needs_a_row_in_every_ten_minutes_from_each_source_and_none_from_the_load_generator(self) -> None:
        expected = {"192.0.2.1": "a", "192.0.2.2": "b"}
        rows = [["192.0.2.1", str(i), "3"] for i in range(6)] + [["192.0.2.2", str(i), "1"] for i in range(6)]
        ok, _, bad = t3check.syslog_verdict(rows, expected, ["192.0.2.9"], 6)
        self.assertTrue(ok, bad)
        ok, _, bad = t3check.syslog_verdict(rows[:-1], expected, ["192.0.2.9"], 6)
        self.assertFalse(ok)
        self.assertIn("b 10분 칸 1개에 행 없음", bad)
        ok, _, bad = t3check.syslog_verdict(rows + [["192.0.2.9", "0", "2"]], expected, ["192.0.2.9"], 6)
        self.assertFalse(ok)
        self.assertIn("부하기 출발 행 2", bad)

    def test_a_silent_monitor_passes_only_when_the_file_has_no_matching_line_either(self) -> None:
        expected = [("web01", "/a"), ("web01", "/b"), ("web01", "/c")]
        verdict, numbers, bad = t3check.monitor_verdict({("web01", "/a"): 4}, {("web01", "/b"): 0, ("web01", "/c"): 0}, expected)
        self.assertEqual(verdict, "충족")
        self.assertIn("줄 없음(파일에도 맞는 줄 없음) 2", " ".join(numbers))
        verdict, _, bad = t3check.monitor_verdict({("web01", "/a"): 4}, {("web01", "/b"): 3, ("web01", "/c"): 0}, expected)
        self.assertEqual(verdict, "미충족")
        self.assertEqual(bad, ["web01 /b 줄 0(파일 맞는 줄 3)"])
        verdict, _, bad = t3check.monitor_verdict({("web01", "/a"): 4}, None, expected)
        self.assertEqual(verdict, "미충족", "VM 을 읽지 않았으면 줄 없음은 실패")

    def test_date_named_files_match_the_day_file_the_agent_reports(self) -> None:
        pattern = "/var/log/tomcat10/localhost_access_log.[YYYY]-[MM]-[DD].txt"
        self.assertTrue(t3check._same_file(pattern, "/var/log/tomcat10/localhost_access_log.2026-11-19.txt"))
        self.assertFalse(t3check._same_file(pattern, "/var/log/tomcat10/localhost_access_log.txt"))
        self.assertTrue(t3check._same_file("/var/log/syslog", "/var/log/syslog"))

    def test_sadf_average_counts_only_samples_inside_the_window(self) -> None:
        text = ("# hostname;interval;timestamp;CPU;%user;%nice;%system;%iowait;%steal;%idle\n"
                "h;600;2026-11-20 13:50:00 UTC;-1;90.00;0.00;5.00;0.00;5.00;0.00\n"
                "h;600;2026-11-20 14:10:00 UTC;-1;30.00;1.00;9.00;1.00;2.00;57.00\n"
                "h;600;2026-11-20 14:20:00 UTC;-1;40.00;0.00;10.00;3.00;4.00;43.00\n")
        got = t3check.parse_sadf(text, self.since, self.until)
        self.assertEqual(got, {"samples": 2, "cpu_busy_avg": 45.0, "iowait_avg": 2.0, "steal_avg": 3.0})
        self.assertEqual(t3check.parse_sadf("", self.since, self.until)["samples"], 0)

    def _budget(self, **over) -> dict:
        base = {"ch_memory_resident_max_gib": 3.4, "ch_memory_resident_p99_gib": 2.6, "ch_memory_limit_gib": 7.2,
                "node_mem_available_min_gib": 8.0, "capture_intervals": [], "ch_capture_peak_gib": None,
                "vm_resident_max_gib": 2.5, "vm_limit_gib": 8.0, "disk_pct_now": 60.0,
                "cpu": {"samples": 144, "cpu_busy_avg": 45.0, "iowait_avg": 1.0, "steal_avg": 2.0},
                "wg0": {"rx_bytes": 0, "tx_bytes": 0, "at": 0}}
        base.update(over)
        return base

    def test_budget_verdict_checks_all_five_resource_limits(self) -> None:
        before = self._budget()
        self.assertEqual(t3check.budget_verdict(before, self._budget(wg0={"rx_bytes": 10 ** 9, "tx_bytes": 0, "at": 86400})).verdict, "충족")
        failing = {
            "ClickHouse 메모리 예산(최댓값)": {"ch_memory_resident_max_gib": 3.6},
            "ClickHouse 메모리 예산(p99)": {"ch_memory_resident_p99_gib": 3.6},
            "노드 가용 메모리": {"node_mem_available_min_gib": 4.5},
            "VictoriaMetrics 메모리": {"vm_resident_max_gib": 6.4},
            "디스크": {"disk_pct_now": 80.0},
            "steal 증가": {"cpu": {"samples": 1, "cpu_busy_avg": 45.0, "iowait_avg": 1.0, "steal_avg": 5.1}},
            "CPU": {"cpu": {"samples": 1, "cpu_busy_avg": 70.1, "iowait_avg": 1.0, "steal_avg": 2.0}},
        }
        for reason, change in failing.items():
            with self.subTest(reason=reason):
                it = t3check.budget_verdict(before, self._budget(**change))
                self.assertEqual(it.verdict, "미충족")
                self.assertIn(reason, " ".join(it.detail))
        it = t3check.budget_verdict(before, self._budget(ch_memory_resident_max_gib=None))
        self.assertEqual(it.verdict, "미충족", "값이 없으면 통과가 아니다")

    def test_capture_intervals_skip_the_daily_dump_group_exports_and_pad(self) -> None:
        # 실제 119 query_log 꼴(11-17 ~ 11-18 KST 조회): 02:20 평시 구간 덤프 셋과 23:27 녹화 캡처 열셋.
        dump = ["2026-11-16T15:00:00Z", "2026-11-16T17:00:00Z"]
        cap = ["2026-11-17T11:29:05Z", "2026-11-17T14:27:08Z"]
        rows = [["1794849614", "1794849618", "lucida.otel_traces_local", *dump],
                ["1794849618", "1794849620", "lucida.lucida_logs_local", *dump],
                ["1794925648", "1794925654", "lucida.otel_traces_local", *cap],
                ["1794925655", "1794925659", "lucida.lucida_logs_local", *cap],
                ["1794925661", "1794925662", "lucida.process_meta", *cap],
                ["1794926500", "1794926501", "lucida.topology_edges", "", ""]]
        got = t3check.capture_intervals_from_exports(rows)
        self.assertEqual(len(got), 2, got)
        self.assertEqual((got[0]["start_epoch"], got[0]["end_epoch"]), (1794925648 - 60, 1794925662 + 60))
        self.assertEqual(got[0]["start"], "2026-11-17T23:26:28+09:00")
        self.assertEqual(got[0]["exports"], 3)
        self.assertEqual(got[1]["tables"], ["lucida.topology_edges"], "범위를 못 읽은 내보내기도 캡처로 센다")
        self.assertTrue(t3check.is_normal_segment_dump(*dump))
        self.assertFalse(t3check.is_normal_segment_dump("2026-11-16T15:00:00Z", "2026-11-16T18:00:00Z"))
        self.assertFalse(t3check.is_normal_segment_dump(*cap))

    def test_clickhouse_judgement_leaves_capture_seconds_out_of_the_query(self) -> None:
        intervals = [{"start_epoch": 10, "end_epoch": 20}, {"start_epoch": 30, "end_epoch": 40}]
        self.assertEqual(t3check._not_in(intervals),
                         "not (toUnixTimestamp(event_time) between 10 and 20 or toUnixTimestamp(event_time) between 30 and 40)")
        self.assertEqual(t3check._not_in([]), "1")
        self.assertEqual(t3check._in([]), "0")
        it = t3check.budget_verdict(self._budget(capture_intervals=[{"start": "a", "end": "b", "exports": 3, "tables": []}]),
                                    self._budget())
        self.assertIn("뺀 캡처 구간(전) 1개: a~b", " ".join(it.numbers))

    def test_capture_peak_plus_growth_must_stay_under_85_percent_of_the_clickhouse_limit(self) -> None:
        ok = t3check.capture_margin_verdict(self._budget(ch_capture_peak_gib=3.475), self._budget())
        self.assertEqual(ok.verdict, "충족")
        self.assertIn("3.475 + T, S 추정 증가분 0.22 = 3.695", ok.numbers[0])
        self.assertIn("6.12", ok.numbers[0])
        over = t3check.capture_margin_verdict(self._budget(), self._budget(ch_capture_peak_gib=6.0))
        self.assertEqual(over.verdict, "미충족")
        self.assertIn("ST3 전에", " ".join(over.detail))
        self.assertEqual(t3check.capture_margin_verdict(self._budget(), self._budget()).verdict, "미판정")
        self.assertEqual(t3check._limit_gib([["7730941132", "8589934592"]]), 7.2, "max_server_memory_usage 와 cgroup 중 낮은 것")

    def test_node_available_memory_is_the_lowest_sample_in_the_window(self) -> None:
        text = ("# hostname;interval;timestamp;kbmemfree;kbavail;kbmemused\n"
                "h;600;2026-11-20 13:50:00 UTC;1;1048576;1\n"
                "h;600;2026-11-20 14:10:00 UTC;1;9437184;1\n"
                "h;600;2026-11-20 14:20:00 UTC;1;8388608;1\n")
        self.assertEqual(t3check.parse_sadf_mem(text, self.since, self.until), 8.0)
        self.assertIsNone(t3check.parse_sadf_mem("", self.since, self.until))

    def test_tunnel_average_comes_from_the_two_counter_readings(self) -> None:
        it = t3check.budget_verdict(self._budget(), self._budget(wg0={"rx_bytes": 54 * 10 ** 9, "tx_bytes": 0, "at": 86400}))
        self.assertIn("터널 평균 5.0 Mbps", " ".join(it.numbers))
        it = t3check.budget_verdict(self._budget(), self._budget(wg0={"rx_bytes": 60 * 10 ** 9, "tx_bytes": 0, "at": 86400}))
        self.assertIn("직렬 지점 기준을 넘음", " ".join(it.detail))

    def test_trap_needs_link_down_and_link_up_from_the_router_address(self) -> None:
        remote = FakeRemote()
        remote.ch_rules.append(("source_ip != '192.168.123.254'", [["192.0.2.50", "3"]]))
        remote.ch_rules.append(("snmp_traps_local", [[".1.3.6.1.6.3.1.1.5.3", "1", "1"], ["1.3.6.1.6.3.1.1.5.4", "2", "1"]]))
        it = t3check.item_trap(remote, self.ctx, self.since, self.until)
        self.assertEqual(it.verdict, "충족")
        self.assertIn("source_ip = '192.168.123.254'", remote.queries[0])
        self.assertIn("참고: 라우터 아닌 출발 3행(192.0.2.50)", " ".join(it.numbers), "다른 장비 trap 은 기록만(리뷰 L6)")
        remote.ch_rules[1] = ("snmp_traps_local", [["1.3.6.1.6.3.1.1.5.3", "1", "1"]])
        self.assertEqual(t3check.item_trap(remote, self.ctx, self.since, self.until).verdict, "미충족")
        self.assertEqual(t3check.item_trap(remote, self.ctx, None, self.until).verdict, "미판정")

    def test_icmp_wants_status_one_rtt_the_business_address_and_no_override(self) -> None:
        remote = FakeRemote()
        ids = {"servers": {}, "children": {}, "router": {"id": "00000000-0000-4000-8000-0000000000ff"}}
        for n, s in enumerate(self.ctx["servers"]):
            ids["servers"][s["name"]] = {"id": f"00000000-0000-4000-8000-{n:012d}", "address": s["addr"]}
        remote.vm_rules += [("icmp_status", [{"metric": {}, "value": [0, "1"]}]), ("rtt", [{"metric": {}, "value": [0, "1"]}])]
        remote.pg_rules += [("target_availability_latest", [["UP"]]), ("policy_device_overrides", [["0"]])]
        self.assertEqual(t3check.item_icmp(remote, self.ctx, ids, self.until).verdict, "충족")
        remote.pg_rules[1] = ("policy_device_overrides", [["1"]])
        it = t3check.item_icmp(remote, self.ctx, ids, self.until)
        self.assertEqual(it.verdict, "미충족")
        self.assertIn("덮어쓰기 1건", " ".join(it.detail))
        remote.pg_rules[1] = ("policy_device_overrides", [["0"]])
        ids["servers"]["db01"]["address"] = "192.168.127.41"
        it = t3check.item_icmp(remote, self.ctx, ids, self.until)
        self.assertIn("db01 대상 주소 192.168.127.41 가 업무 주소 192.168.126.11 가 아님", " ".join(it.detail))

    ROUTER = "00000000-0000-4000-8000-0000000000ff"

    def _nms_remote(self, missing: tuple[str, str] | None = None) -> FakeRemote:
        """라우터 하위 대상(ens18~ens22, lo)과 인터페이스마다 네 이름군. missing=(인터페이스, 지표) 는 그 계열을 뺀다."""
        remote = FakeRemote()
        names = {"ens18": "2", "ens19": "3", "ens20": "4", "ens21": "5", "ens22": "6", "lo": "1"}
        remote.pg_rules.append(("'network_interface'", [[k, v, f"00000000-0000-4000-8000-00000000010{v}"] for k, v in names.items()]))
        all_names = [n for label, ns in t3check.NMS_FAMILIES.items() if label != "CPU" for n in ns]

        def iface(text):
            match = re.search(r'snmp_index="(\d+)"', text)
            if not match:
                return None
            name = next(k for k, v in names.items() if v == match.group(1))
            return [{"metric": {"__name__": n}, "value": [0, "1"]} for n in all_names if (name, n) != missing]
        remote.vm_rules.append((iface, None))
        remote.vm_rules.append(("nms.cpu.usage", [{"metric": {"__name__": "nms.cpu.usage"}, "value": [0, "10"]}]))
        return remote

    def test_nms_checks_every_design_interface_by_its_own_index(self) -> None:
        ids = {"router": {"id": self.ROUTER}}
        it = t3check.item_nms(self._nms_remote(), self.ctx, ids, self.until)
        self.assertEqual(it.verdict, "충족", it.detail)
        self.assertEqual(len([n for n in it.numbers if "snmp_index" in n]), 4, "상향과 하위 셋만(관리 NIC, lo 는 세지 않음)")
        it = t3check.item_nms(self._nms_remote(("ens21", "nms.interface.if_out_discards")), self.ctx, ids, self.until)
        self.assertEqual(it.verdict, "미충족")
        self.assertIn("ens21 버림 없음", " ".join(it.detail))
        remote = self._nms_remote()
        remote.pg_rules[0] = ("'network_interface'", [r for r in remote.pg_rules[0][1] if r[0] != "ens20"])
        it = t3check.item_nms(remote, self.ctx, ids, self.until)
        self.assertIn("ens20 인터페이스 대상(snmp_index) 없음", " ".join(it.detail))

    def _nms_store(self, poll: int, polls: int, last_ago: int, cpu: bool = True) -> "NmsVmStore":
        """collector-nms 의 실제 계열 꼴(119 VM 실측 11-19): 첫 폴링은 target_id = 장비, 그 뒤는 target_id = 하위 대상,
        host_target_id = 장비는 늘 있다. polls 번 폴링하고 마지막 폴링이 until 보다 last_ago 초 앞이다."""
        names = {"ens18": "2", "ens19": "3", "ens20": "4", "ens21": "5", "ens22": "6", "lo": "1"}
        child = {v: f"00000000-0000-4000-8000-00000000010{v}" for v in names.values()}
        cpu_child = {"196608": "00000000-0000-4000-8000-000000000201", "196609": "00000000-0000-4000-8000-000000000202"}
        end = self.until.timestamp() - last_ago
        times = [end - poll * (polls - 1 - i) for i in range(polls)]
        store = NmsVmStore()
        store.pg_rules.append(("'network_interface'", [[k, v, child[v]] for k, v in names.items()]))
        iface_names = [n for label, ns in t3check.NMS_FAMILIES.items() if label != "CPU" for n in ns]
        for index in names.values():
            for name in iface_names:
                labels = {"__name__": name, "host_target_id": self.ROUTER, "snmp_index": index}
                store.add(dict(labels, target_id=self.ROUTER), times[:1])
                store.add(dict(labels, target_id=child[index]), times[1:])
        if cpu:
            for key, tid in cpu_child.items():
                labels = {"__name__": "nms.cpu.usage", "host_target_id": self.ROUTER, "resource_key": key}
                store.add(dict(labels, target_id=self.ROUTER), times[:1])
                store.add(dict(labels, target_id=tid), times[1:])
        return store

    def test_nms_finds_cpu_moved_to_child_targets_after_the_first_poll(self) -> None:
        ids = {"router": {"id": self.ROUTER}}
        # 11-19 15:42 확인 3회와 같은 꼴: 등록 뒤 40분, 60초 폴링, 장비 대상 계열은 첫 폴링 하나뿐.
        it = t3check.item_nms(self._nms_store(poll=60, polls=40, last_ago=30), self.ctx, ids, self.until)
        self.assertEqual(it.verdict, "충족", it.detail)
        self.assertIn("폴링 간격 60.0초", it.numbers)
        self.assertIn("창 900초(폴링 간격 2배, 최소 900초)", it.numbers)
        self.assertIn("CPU 2개", it.numbers)

    def test_nms_window_follows_the_measured_poll(self) -> None:
        ids = {"router": {"id": self.ROUTER}}
        it = t3check.item_nms(self._nms_store(poll=600, polls=6, last_ago=590), self.ctx, ids, self.until)
        self.assertEqual(it.verdict, "충족", it.detail)
        self.assertIn("폴링 간격 600.0초", it.numbers)
        self.assertIn("창 1200초(폴링 간격 2배, 최소 900초)", it.numbers)
        # 30분 폴링에서 마지막 폴링이 1000초 앞: 고정 15분 창이면 놓치고 2배 창(3600초)이면 본다.
        it = t3check.item_nms(self._nms_store(poll=1800, polls=3, last_ago=1000), self.ctx, ids, self.until)
        self.assertEqual(it.verdict, "충족", it.detail)
        self.assertIn("창 3600초(폴링 간격 2배, 최소 900초)", it.numbers)
        self.assertEqual([t3check.nms_window_seconds(p) for p in (None, 0, 60, 450, 450.2, 600)], [900, 900, 900, 900, 901, 1200])

    def test_nms_still_needs_cpu_series_in_the_window(self) -> None:
        ids = {"router": {"id": self.ROUTER}}
        it = t3check.item_nms(self._nms_store(poll=60, polls=40, last_ago=30, cpu=False), self.ctx, ids, self.until)
        self.assertEqual(it.verdict, "미충족")
        self.assertEqual(it.detail, ["CPU 없음(nms.cpu.usage)"])
        self.assertIn("CPU 0개", it.numbers)
        # 폴링이 멈춘 지 폴링 간격 2배(최소 15분)를 넘으면 미충족(창을 넓혀 옛 계열로 통과시키지 않는다).
        it = t3check.item_nms(self._nms_store(poll=60, polls=40, last_ago=901), self.ctx, ids, self.until)
        self.assertEqual(it.verdict, "미충족")
        self.assertIn("CPU 없음(nms.cpu.usage)", " ".join(it.detail))
        self.assertIn("ens18 상태, 옥텟, 오류, 버림 없음", " ".join(it.detail))
        self.assertIn("폴링 간격 60.0초", it.numbers)

    def test_nms_right_after_registration_has_one_poll_and_unknown_interval(self) -> None:
        ids = {"router": {"id": self.ROUTER}}
        it = t3check.item_nms(self._nms_store(poll=60, polls=1, last_ago=90), self.ctx, ids, self.until)
        self.assertEqual(it.verdict, "충족", it.detail)
        self.assertIn("폴링 간격 알 수 없음(최근 60분 안 같은 CPU 계열 표본 2개 미만)", it.numbers)
        self.assertIn("창 900초(폴링 간격 2배, 최소 900초)", it.numbers)
        self.assertIn("CPU 2개", it.numbers)
        # 둘째 폴링 뒤: 장비 대상 계열과 하위 대상 계열이 한 창에 함께 들어도 CPU 는 둘이다.
        it = t3check.item_nms(self._nms_store(poll=60, polls=3, last_ago=10), self.ctx, ids, self.until)
        self.assertIn("CPU 2개", it.numbers)
        self.assertIn("폴링 간격 60.0초", it.numbers)

    def _mgmt_nic_case(self, recent: int = 12):
        """등록 도구 record 표(제품 대역)와 그 번호로 답하는 119 PG 행(resolve_ids 질의 꼴), 관리 NIC 계열 수."""
        product = FakeProduct(inventory(T_INV))
        session = t3reg.Session(product, True, io.StringIO())
        t3reg.phase_approve(session, inventory(T_INV))
        table = json.loads(json.dumps(t3reg.phase_record(session, inventory(T_INV))))
        remote = FakeRemote()
        remote.pg_rules.append(("type='server'", [[t["id"], t["name"], t["address"], "approved", "managed"]
                                                  for t in product.approved.values()]))
        remote.pg_rules.append(("meta->>'host_target_id' in", [
            [n["id"], sid, n["resource_type"], n.get("resource_key", ""), n.get("management_status", "managed")]
            for sid, nodes in product.nodes.items() for n in nodes if n["id"] != sid]))
        remote.vm_rules.append((lambda q: ([{"metric": {}, "value": [0, str(recent)]}] if recent else [])
                                if 'device="ens19"' in q else None, None))
        return remote, table, product

    def test_mgmt_nic_is_accepted_when_the_table_marks_it_and_its_series_come_in(self) -> None:
        remote, table, _ = self._mgmt_nic_case()
        ids = t3check.resolve_ids(remote, self.ctx)
        it = t3check.item_mgmt_nic(remote, self.ctx, ids, self.until, table)
        self.assertEqual(it.verdict, "충족", it.detail)
        self.assertEqual(len([n for n in it.numbers if "표 accepted, managed, 최근 10분 계열 12" in n]), 4)

    def test_mgmt_nic_fails_without_the_mark_or_with_unmanaged_or_without_series(self) -> None:
        remote, table, product = self._mgmt_nic_case()
        ids = t3check.resolve_ids(remote, self.ctx)
        it = t3check.item_mgmt_nic(remote, self.ctx, ids, self.until, None)
        self.assertEqual((it.verdict, it.detail), ("미충족", ["대상 번호 표(--table) 가 없어 받아들임 표시를 볼 수 없음"]))
        old = json.loads(json.dumps(table))
        for rec in old["servers"]:
            rec.pop("management_nic")
            rec["unmanaged"] = [rec["children"]["sms.network_interface"]["ens19"]]
        it = t3check.item_mgmt_nic(remote, self.ctx, ids, self.until, old)
        self.assertEqual(it.verdict, "미충족")
        self.assertIn("web01 대상 번호 표에 관리 NIC ens19 받아들임 표시 없음", " ".join(it.detail))
        wrong = json.loads(json.dumps(table))
        wrong["servers"][0]["management_nic"]["ifname"] = "ens18"
        wrong["servers"][1]["management_nic"]["target_id"] = "00000000-0000-4000-8000-000000000999"
        it = t3check.item_mgmt_nic(remote, self.ctx, ids, self.until, wrong)
        detail = " ".join(it.detail)
        self.assertIn("web01 대상 번호 표에 관리 NIC ens19 받아들임 표시 없음", detail)
        self.assertIn("was01 표의 관리 NIC 번호 00000000-0000-4000-8000-000000000999 가 119 하위 대상", detail)
        for child in ids["children"].values():
            for c in child:
                if c["resource_key"] == "ens19":
                    c["management_status"] = "unmanaged"
        it = t3check.item_mgmt_nic(remote, self.ctx, ids, self.until, table)
        self.assertIn("관리 NIC 가 unmanaged(받아들임이면 managed, 관리 안 함 PUT 을 하지 않는다)", " ".join(it.detail))
        remote, table, _ = self._mgmt_nic_case(recent=0)
        ids = t3check.resolve_ids(remote, self.ctx)
        it = t3check.item_mgmt_nic(remote, self.ctx, ids, self.until, table)
        self.assertEqual(it.verdict, "미충족")
        self.assertIn("db01 관리 NIC 최근 10분 계열 0(받아들였으면 수집돼야 함)", " ".join(it.detail))
        for child in ids["children"].values():
            child[:] = [c for c in child if c["resource_key"] != "ens19"]
        it = t3check.item_mgmt_nic(remote, self.ctx, ids, self.until, table)
        self.assertIn("web01 관리 NIC ens19 하위 대상 없음", " ".join(it.detail))

    def _db_remote(self, lines: int) -> tuple[FakeRemote, dict]:
        remote = FakeRemote()
        db = "00000000-0000-4000-8000-000000000004"
        ids = {"servers": {"db01": {"id": db, "address": "192.168.126.11"}}, "children": {}, "router": None}
        remote.vm_rules.append(("sms\\\\.file_system", [{"metric": {}, "value": [0, "6"]}]))
        remote.vm_rules.append(("sms\\\\.disk", [{"metric": {}, "value": [0, "9"]}]))
        remote.ch_rules.append(("process_snapshot", [["12"]]))
        remote.ch_rules.append(("lucida_logs_local", [[str(lines)]]))
        return remote, ids

    def test_db_server_log_lines_follow_the_same_rule_as_the_log_monitor_row(self) -> None:
        remote, ids = self._db_remote(5)
        self.assertEqual(t3check.item_db_server(remote, self.ctx, ids, self.since, self.until).verdict, "충족")
        remote, ids = self._db_remote(0)
        it = t3check.item_db_server(remote, self.ctx, ids, self.since, self.until)
        self.assertEqual(it.verdict, "미충족", "VM 을 읽지 않으면 줄 없음은 실패")
        remote, ids = self._db_remote(0)
        remote.guest_answer = "0\n"
        it = t3check.item_db_server(remote, self.ctx, ids, self.since, self.until, vm_reads=True)
        self.assertEqual(it.verdict, "충족", it.detail)
        self.assertIn("서버 로그 감시 줄 없음(파일에도 맞는 줄 없음)", it.numbers)
        self.assertIn("grep -cE", remote.queries[-1])
        remote, ids = self._db_remote(0)
        remote.guest_answer = "4\n"
        self.assertEqual(t3check.item_db_server(remote, self.ctx, ids, self.since, self.until, vm_reads=True).verdict, "미충족")

    def test_target_table_needs_the_device_its_interfaces_and_the_trap_receiver(self) -> None:
        def table(**over) -> dict:
            base = {"servers": [{"target_id": f"00000000-0000-4000-8000-00000000000{n}",
                                 "children": {"sms.network_interface": {"ens18": f"00000000-0000-4000-8000-00000000001{n}"},
                                              "sms.file_system": {}, "sms.process": {}}} for n in range(4)],
                    "network_devices": [{"target_id": self.ROUTER, "interfaces": {"ens19": "00000000-0000-4000-8000-000000000201"}}],
                    "trap_receivers": [{"target_id": "00000000-0000-4000-8000-000000000301"}]}
            base.update(over)
            return base

        class Echo(FakeRemote):
            def pg(self, sql):
                return [[i] for i in re.findall(r"'([0-9a-f-]{36})'", sql)]
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "t.targets.json"
            for over, ok in (({}, True), ({"trap_receivers": []}, False), ({"network_devices": []}, False),
                             ({"network_devices": [{"target_id": self.ROUTER, "interfaces": {}}]}, False)):
                path.write_text(json.dumps(table(**over)))
                with self.subTest(over=over):
                    self.assertEqual(t3check.item_table(Echo(), path, self.ctx).verdict, "충족" if ok else "미충족")
            path.write_text(json.dumps(table(network_devices=[], trap_receivers=[])))
            s_ctx = t3check.context(inventory(S_INV))
            self.assertEqual(t3check.item_table(Echo(), path, s_ctx).verdict, "충족", "라우터 없는 S 는 장비와 수신기를 요구하지 않음")

    def test_budget_limits_come_from_the_one_criteria_file(self) -> None:
        budget = t3check.load_budget()
        self.assertEqual(budget["clickhouse_memory_resident_max_gib"], 3.6)
        self.assertEqual(budget["disk_used_pct_max"], 80.0)
        stricter = dict(budget, clickhouse_memory_resident_max_gib=3.0)
        self.assertEqual(t3check.budget_verdict(self._budget(), self._budget(), stricter).verdict, "미충족")
        source = (DEPLOY / "tools" / "t3_check.py").read_text()
        self.assertNotRegex(source, r"(?m)^[A-Z_]+ = (3\.6|0\.8|80\.0|70\.0)\b", "기준값을 도구 안에 다시 두지 않는다")

    def test_process_unit_row_records_the_labels(self) -> None:
        remote = FakeRemote()
        ids = {"servers": {"web01": {"id": "00000000-0000-4000-8000-000000000001"}}}
        remote.vm_rules.append(("sms.process.cpu_utilization", [{"__name__": "x", "name": "java", "pid": "1", "host_target_id": "h"}]))
        it = t3check.item_process_unit(remote, self.ctx, ids, self.until)
        self.assertEqual(it.verdict, "충족")
        self.assertIn("pid", it.numbers[0])
        remote.vm_rules[0] = ("sms.process.cpu_utilization", [])
        self.assertEqual(t3check.item_process_unit(remote, self.ctx, ids, self.until).verdict, "미충족")

    def test_only_target_numbers_go_into_a_promql_selector(self) -> None:
        self.assertEqual(t3check.regex_ids(["00000000-0000-4000-8000-000000000001"]), "00000000-0000-4000-8000-000000000001")
        with self.assertRaises(t3check.QueryError):
            t3check.regex_ids(['x"}) or vector(1'])
        self.assertEqual(t3check.promql_regex(["nms.cpu.usage"]), "nms\\\\.cpu\\\\.usage")

    def test_every_design_row_has_an_item(self) -> None:
        design = (REPO / "docs" / "spec-testbed-vm-3tier.md").read_text(encoding="utf-8")
        block = design[design.index("### T3 (119 등록과 수집)"):design.index("### T4 (러너 연동)")]
        rows = [line.split("|")[1].strip() for line in block.splitlines() if line.startswith("| ") and not line.startswith("| 조건")]
        self.assertGreaterEqual(len(rows), 15, rows)
        source = (DEPLOY / "tools" / "t3_check.py").read_text(encoding="utf-8")
        covered = {"SMS": 'Item("SMS")', "관리 NIC": 'Item("관리 NIC(Q7d)")', "journal": 'Item("journal")',
                   "프로세스 계열 단위": 'Item("프로세스 계열 단위")', "ICMP": 'Item("ICMP")', "NMS": 'Item("NMS")',
                   "trap": 'Item("trap")', "DPM 없음": 'Item("DPM 없음")', "DB 서버 관제": 'Item("DB 서버 관제")',
                   "syslog": 'Item("syslog")', "로그 감시": 'Item("로그 감시")', "탐지 밴드": 'Item("탐지 밴드")',
                   "부담": 'Item("부담(Q8)")', "대상 번호 표": 'Item("대상 번호 표")'}
        for row in rows:
            with self.subTest(row=row):
                if row == "원칙 문서":
                    continue  # 문서를 고치는 행(도구는 수치만 낸다, runbook §13)
                self.assertIn(row, covered)
                self.assertIn(covered[row], source)


if __name__ == "__main__":
    unittest.main()


trace = _load("trace_table", DEPLOY / "tools" / "trace_table.py")
HV = DEPLOY / "hypervisor"


class HypervisorHelperFilesTests(unittest.TestCase):
    """66 실행기 계정, 강제 명령, sudoers, rsyslog 거르기(설계 §6-1 "66 호스트 권한", §6-2 "66 호스트", 결정 2)."""

    def render(self, tmp: Path) -> Path:
        """runbook §15-1 차례 2, 3 처럼 도우미 폴더를 옮기고 build.py 로 설치본을 그 안에 만든 뒤 render."""
        stage = tmp / "stage"
        shutil.copytree(HV, stage)
        subprocess.run([sys.executable, str(HV / "build.py"), "--inventory", str(T_INV), "--stage", str(stage)],
                       check=True, capture_output=True, timeout=60)
        out = tmp / "out"
        done = subprocess.run(["bash", str(stage / "install-hv-helper.sh"), "render", "--out", str(out)],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return out

    def test_render_names_one_helper_one_account_and_a_forced_command(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            out = self.render(Path(raw))
            sudoers = (out / "60-hvjob").read_text()
            rules = [l for l in sudoers.splitlines() if l and not l.startswith("#")]
            self.assertEqual(rules, ["Defaults:hvjob env_reset, !requiretty, !syslog, !pam_session",
                                     'hvjob ALL=(root) NOPASSWD: /usr/local/sbin/t4-hv-exec-root ""'])
            if shutil.which("visudo"):
                check = subprocess.run(["visudo", "-cf", str(out / "60-hvjob")], capture_output=True, text=True)
                self.assertEqual(check.returncode, 0, check.stderr)
            self.assertEqual((out / "authorized_keys.prefix").read_text(),
                             'command="/usr/local/sbin/t4-hv-exec",no-pty,no-port-forwarding,no-agent-forwarding,'
                             'no-X11-forwarding,from="192.168.122.20" ')
            self.assertEqual((out / "t4-hv-exec").read_bytes(), (HV / "t4-hv-exec").read_bytes())
            sums = json.loads((HV / "t4-hv-exec.sha256.json").read_text())["files"]
            import hashlib
            for name in ("t4-hv-exec", "t4-hv-exec-root"):
                self.assertEqual(hashlib.sha256((out / name).read_bytes()).hexdigest(), sums[name], name)
            # Without the built files in the folder render refuses (the repository keeps no copy).
            for missing in ("hv.env", "t4-hv-exec-root", "05-hvjobs.conf"):
                bare = Path(raw) / f"bare-{missing}"
                shutil.copytree(Path(raw) / "stage", bare)
                (bare / missing).unlink()
                done = subprocess.run(["bash", str(bare / "install-hv-helper.sh"), "render", "--out", str(Path(raw) / f"o-{missing}")],
                                      capture_output=True, text=True, timeout=30)
                self.assertNotEqual(done.returncode, 0, missing)
                self.assertIn("build.py", done.stderr, missing)
            # The filter sorts before 66's forward file (60-lucida-forward.conf) and names the account, uid, helper and run-r.
            self.assertLess("05-hvjobs.conf", "60-lucida-forward.conf")
            text = (out / "05-hvjobs.conf").read_text()
            for piece in ("hvjob", "(user-|@|UID |uid=|/user/)1460", "t4-hv-exec", "run-r[0-9a-f]{32}",
                          "192[.]168[.]122[.]20", "/var/log/hvjobs.log", "stop"):
                self.assertIn(piece, text)
            # The account's sudo leaves no line (sudoers), so the filter does not take every sudo line (review M4).
            self.assertNotIn('$programname == "sudo"', text)
            self.assertNotIn("{{", text)

    @unittest.skipUnless(shutil.which("rsyslogd") and shutil.which("logger"), "rsyslogd 와 logger 가 필요")
    def test_filter_diverts_pipeline_lines_and_keeps_the_rest_with_a_real_rsyslogd(self) -> None:
        """진짜 rsyslogd(-N1 문법 검사와 실제로 띄워 줄을 보냄). 66 의 rsyslog 판(8.2504)과 104 판(8.2112)은 다르지만
        RainerScript 문자열 탈출 규칙(APT2 의 `$` 결함)은 같다."""
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory(dir="/tmp", prefix="rs") as short:
            tmp, sock = Path(raw), Path(short)
            out = self.render(tmp)
            conf_d = tmp / "rsyslog.d"
            conf_d.mkdir()
            (conf_d / "05-hvjobs.conf").write_text((out / "05-hvjobs.conf").read_text().replace(
                "/var/log/hvjobs.log", str(sock / "hvjobs.log")))
            (tmp / "work").mkdir()
            conf = tmp / "rsyslog.conf"
            conf.write_text(f'global(workDirectory="{tmp / "work"}")\nmodule(load="imuxsock" SysSock.Use="off")\n'
                            f'input(type="imuxsock" Socket="{sock / "l.sock"}")\ninclude(file="{conf_d}/*.conf")\n'
                            f'*.* action(type="omfile" file="{sock / "forward.log"}")\n')
            check = subprocess.run(["rsyslogd", "-N1", "-f", str(conf)], capture_output=True, text=True, timeout=30)
            self.assertEqual(check.returncode, 0, check.stderr)
            proc = subprocess.Popen(["rsyslogd", "-n", "-f", str(conf), "-i", str(sock / "pid")],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for _ in range(50):
                    if (sock / "l.sock").exists():
                        break
                    time_sleep(0.1)
                diverted = [("sshd", "Accepted publickey for hvjob from 192.168.122.20 port 5 ssh2"),
                            ("sshd-session", "Received disconnect from 192.168.122.20 port 5:11: disconnected by user"),
                            ("systemd", "Started user@1460.service - User Manager for UID 1460."),
                            ("systemd-logind", "New session 12 of user hvjob."),
                            ("systemd", "Started run-r0123456789abcdef0123456789abcdef.timer."),
                            ("sudo", "hvjob : PWD=/ ; USER=root ; COMMAND=/usr/local/sbin/t4-hv-exec-root")]
                kept = [("sshd", "Accepted publickey for root from 192.168.230.104 port 1 ssh2"),
                        ("sshd-session", "Disconnected from 192.168.122.201 port 9"),
                        ("systemd", "Started user@14600.service"),
                        ("CRON", "hvjobx is another word"),
                        ("systemd", "session-12.scope: Deactivated successfully."),
                        # Another root's sudo on 66 goes to the monitoring server as before (review M4).
                        ("sudo", "root : PWD=/root ; USER=root ; COMMAND=/usr/bin/qm list"),
                        ("sudo", "pam_unix(sudo:session): session closed for user root")]
                for tag, message in diverted + kept:
                    subprocess.run(["logger", "-u", str(sock / "l.sock"), "-t", tag, message], check=True, timeout=10)
                for _ in range(50):
                    lines = (sock / "forward.log").read_text().count("\n") if (sock / "forward.log").exists() else 0
                    if lines >= len(kept):
                        break
                    time_sleep(0.1)
                time_sleep(0.3)
            finally:
                proc.terminate()
                proc.wait(timeout=10)
            forward = (sock / "forward.log").read_text()
            hvjobs = (sock / "hvjobs.log").read_text()
            for _, message in diverted:
                self.assertIn(message, hvjobs)
                self.assertNotIn(message, forward)
            for _, message in kept:
                self.assertIn(message, forward)
                self.assertNotIn(message, hvjobs)

    def test_install_puts_the_filter_first_and_remove_takes_it_last(self) -> None:
        """리뷰 M1: 계정 추가, 삭제 줄도 걸러지게 거르기를 설치 맨 앞(계정보다 먼저, 실제로 돌리는지 확인까지)과
        삭제 맨 끝에 둔다."""
        source = (HV / "install-hv-helper.sh").read_text()
        install = source[source.index("do_install() {"):source.index("\n}\n", source.index("do_install() {"))]
        remove = source[source.index("do_remove() {"):source.index("\n}\n", source.index("do_remove() {"))]
        place_filter = install.index('install -m 0644 "$stage/05-hvjobs.conf" "$filter"')
        diverts = install.index("filter_diverts || fail")
        for later in ("apt-get install", "useradd", "passwd -l", "visudo -cf", "authorized_keys\"", 'install -o root -g root -m 0755'):
            with self.subTest(step=later):
                self.assertLess(place_filter, install.index(later))
                self.assertLess(diverts, install.index(later))
        self.assertLess(install.index("systemctl restart rsyslog"), diverts)
        drop_filter = remove.index('rm -f "$filter"')
        for earlier in ("userdel", 'rm -f "$sudoers"', 'rm -rf -- "$home"'):
            with self.subTest(step=earlier):
                self.assertLess(remove.index(earlier), drop_filter)

    FAKES = {
        "id": '#!/bin/bash\nd="$HV_FAKE"\nif [[ "$1" == -u && -z "${2:-}" ]]; then echo 0; exit 0; fi\n'
              'u="${@: -1}"; [[ -e "$d/user-$u" ]] || exit 1\nif [[ "$1" == -u ]]; then cat "$d/user-$u"; fi\n',
        "useradd": '#!/bin/bash\necho "useradd $*" >>"$HV_FAKE/calls"\nu="${@: -1}"\nwhile (($#)); do [[ "$1" == --uid ]] && echo "$2" >"$HV_FAKE/user-$u"; shift; done\nmkdir -p "$HV_FAKE/home/$u"\n',
        "userdel": '#!/bin/bash\necho "userdel $*" >>"$HV_FAKE/calls"\nrm -f "$HV_FAKE/user-$1"\n',
        "getent": '#!/bin/bash\n[[ -e "$HV_FAKE/user-$2" ]] || exit 2\necho "$2:x:$(cat "$HV_FAKE/user-$2"):1::$HV_FAKE/home/$2:/bin/bash"\n',
        "passwd": '#!/bin/bash\n[[ "$1" == -S ]] && { echo "$2 L"; exit 0; }\necho "passwd $*" >>"$HV_FAKE/calls"\n',
        "systemctl": '#!/bin/bash\nd="$HV_FAKE"\nf=no; [[ -e "$HV_FILTER" ]] && f=yes\necho "systemctl $* filter=$f" >>"$d/calls"\n'
                     'case "$1" in is-enabled) [[ -e "$d/masked-$2" ]] && echo masked || echo static ;; '
                     'mask) touch "$d/masked-$2" ;; unmask) rm -f "$d/masked-$2" ;; esac\n',
        "rsyslogd": '#!/bin/bash\nexit 0\n',
        # The filter is in place and diverts: the probe line lands in hvjobs.log only.
        "logger": '#!/bin/bash\necho "logger $*" >>"$HV_FAKE/calls"\n[[ -e "$HV_FILTER" ]] && echo "${@: -1}" >>"$HV_LOG_DIR/hvjobs.log"\n',
        "sleep": '#!/bin/bash\nexit 0\n',
        "apt-get": '#!/bin/bash\necho "apt-get $*" >>"$HV_FAKE/calls"\n',
        "visudo": '#!/bin/bash\nexit 0\n',
        "pkill": '#!/bin/bash\nexit 1\n',
        "install": '#!/bin/bash\na=(); while (($#)); do case "$1" in -o|-g) shift 2 ;; *) a+=("$1"); shift ;; esac; done\nexec /usr/bin/install "${a[@]}"\n',
    }

    def test_install_masks_the_user_manager_after_the_filter_and_before_the_account_and_remove_unmasks(self) -> None:
        """T4F 확인 필요 1 결정 (나): 설치 스크립트를 대역 명령(id, useradd, systemctl, logger ...)으로 돌려 순서를 본다.
        거르기를 놓고 실제로 돌리는지 본 뒤, 계정을 만들기 전에 user@<uid>.service 를 mask 하고, remove 는 계정을 지운 뒤
        거르기가 아직 있을 때 unmask 한다. check 는 mask 를 PASS 줄로 낸다."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            stage = root / "stage"
            shutil.copytree(HV, stage)
            subprocess.run([sys.executable, str(HV / "build.py"), "--inventory", str(T_INV), "--stage", str(stage)],
                           check=True, capture_output=True, timeout=60)
            fake, bindir, logs = root / "fake", root / "bin", root / "log"
            for d in (fake, bindir, logs, root / "sbin", root / "rsyslog.d", root / "sudoers.d", root / "logrotate.d"):
                d.mkdir()
            for name, body in self.FAKES.items():
                (bindir / name).write_text(body)
                (bindir / name).chmod(0o755)
            (root / "rsyslog.d" / "60-lucida-forward.conf").write_text("*.* @x\n")
            (root / "runner.pub").write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyOnlyForTheTest runner\n")
            env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "HV_FAKE": str(fake), "HV_LOG_DIR": str(logs),
                   "HV_SBIN": str(root / "sbin"), "HV_SUDOERS": str(root / "sudoers.d" / "60-hvjob"),
                   "HV_FILTER": str(root / "rsyslog.d" / "05-hvjobs.conf"), "HV_LOGROTATE": str(root / "logrotate.d" / "hvjobs"),
                   "HV_FORWARD": str(root / "rsyslog.d" / "60-lucida-forward.conf")}
            done = subprocess.run(["bash", str(stage / "install-hv-helper.sh"), "install", "--pubkey", str(root / "runner.pub")],
                                  capture_output=True, text=True, env=env, timeout=60)
            self.assertIn("masked user@1460.service", done.stdout, done.stderr)
            self.assertIn("PASS user@1460.service masked", done.stdout)
            calls = (fake / "calls").read_text().splitlines()
            at = lambda prefix: next(i for i, c in enumerate(calls) if c.startswith(prefix))  # noqa: E731
            self.assertLess(at("systemctl restart rsyslog filter=yes"), at("logger"))
            self.assertLess(at("logger"), at("systemctl mask user@1460.service"))
            self.assertLess(at("systemctl mask user@1460.service"), at("useradd"))
            # Running install again does not mask twice.
            subprocess.run(["bash", str(stage / "install-hv-helper.sh"), "install", "--pubkey", str(root / "runner.pub")],
                           capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(sum(c.startswith("systemctl mask ") for c in (fake / "calls").read_text().splitlines()), 1)
            (fake / "calls").write_text("")
            done = subprocess.run(["bash", str(stage / "install-hv-helper.sh"), "remove"], capture_output=True, text=True,
                                  env=env, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            calls = (fake / "calls").read_text().splitlines()
            self.assertLess(at("userdel"), at("systemctl unmask user@1460.service filter=yes"))
            self.assertLess(at("systemctl unmask"), at("systemctl restart rsyslog filter=no"))
            self.assertFalse((fake / "masked-user@1460.service").exists())

    def test_front_helper_refuses_unknown_and_malformed_requests_without_calling_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            fake = tmp / "sudo"
            fake.write_text('#!/bin/bash\necho called >>"$(dirname "$0")/log"\ncat >/dev/null\n')
            fake.chmod(0o755)
            env = {**os.environ, "HV_SUDO": str(fake), "HV_HELPER_DIR": str(HV)}
            for request in ("", "rm -rf /", "link-restore 202 net0", "version extra", "link-down 202 net0 `id`",
                            "link-down 202 net0 x x x x x x", "LINK-DOWN 202 net0", "bash"):
                with self.subTest(request=request):
                    done = subprocess.run([str(HV / "t4-hv-exec")], input=request + "\n", env=env, capture_output=True,
                                          text=True, timeout=30)
                    self.assertEqual(done.returncode, 3, (request, done.stderr))
            done = subprocess.run([str(HV / "t4-hv-exec")], input="link-down 202 net0\nexpire_after=1800; id\n", env=env,
                                  capture_output=True, text=True, timeout=30)
            self.assertEqual(done.returncode, 3)
            self.assertFalse((tmp / "log").exists())
            done = subprocess.run([str(HV / "t4-hv-exec")], input="link-down 202 net0\nexpire_after=1800 guard_hold=960\n",
                                  env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual((tmp / "log").read_text(), "called\n")


def time_sleep(seconds: float) -> None:
    import time
    time.sleep(seconds)


class TraceTableTests(unittest.TestCase):
    """흔적 표 질의 도구(docs/ref-civil-petition-injection-traces.md, 설계 §6-7). 119 에 닿지 않는 대역으로 본다."""

    START = datetime.datetime(2026, 11, 20, 10, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=9)))

    def remote(self) -> FakeRemote:
        r = FakeRemote()
        r.pg_rules = [("from targets where type='server'", [
            ["11111111-1111-1111-1111-111111111111", "192.168.124.11", "", "approved", "managed"],
            ["22222222-2222-2222-2222-222222222222", "192.168.126.11", "", "approved", "managed"]]),
            ("type='network'", [["33333333-3333-3333-3333-333333333333", "approved"]]),
            ("meta->>'host_target_id'", [])]
        return r

    def run_tool(self, remote: FakeRemote, *extra: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = trace.main(["--inventory", str(T_INV), "--start", self.START.isoformat(),
                             "--end", (self.START + datetime.timedelta(minutes=20)).isoformat(), "--executor", "router.qdisc",
                             "--variant", "큐 넘침", "--run-id", "trial-1", "--words", "T4-TRIAL,queue-ens20", *extra],
                            remote_factory=lambda: remote)
        return rc, out.getvalue()

    def test_every_clickhouse_query_names_its_partition_and_time_range(self) -> None:
        remote = self.remote()
        rc, out = self.run_tool(remote)
        self.assertEqual(rc, 0)
        partition = {"lucida.process_snapshot": "toDate(ts) between", "lucida.host_connections": "toYYYYMMDD(timestamp) between",
                     "lucida.lucida_logs_local": "timestamp_date between", "lucida.syslog_local": "toYYYYMM(received_at) between",
                     "lucida.snmp_traps_local": "toYYYYMM(received_at) between", "lucida.lucida_events_local": "occurred_at between"}
        seen = set()
        for q in remote.queries:
            for table, piece in partition.items():
                if f"from {table} " in q:
                    seen.add(table)
                    self.assertIn(piece, q, q)
                    self.assertRegex(q, r"between '\d{4}-\d\d-\d\d \d\d:\d\d:\d\d' and '\d{4}-\d\d-\d\d \d\d:\d\d:\d\d'", q)
        self.assertEqual(seen, set(partition))
        # The window is the injection window and baseline windows are the same length, 30 minutes before it.
        self.assertIn("2026-11-20 01:00:00", " ".join(remote.queries))
        self.assertIn("2026-11-20 00:30:00", " ".join(remote.queries))
        row = out.splitlines()[0]
        self.assertEqual(len(row[2:-2].split(" | ")), 16)
        self.assertNotIn("| |", row)
        self.assertIn("trial-1, 11-20 10:00 ~ 10:20 KST", row)

    def test_q7_looks_for_every_hidden_word_and_counts_unmeasured_cells(self) -> None:
        remote = self.remote()
        remote.ch_rules = [(lambda q: [["2"]] if "lucida.syslog_local" in q and "match(lower(message)" in q
                            and "t4-trial" in q and "select count()" in q else None, None)]
        rc, out = self.run_tool(remote)
        self.assertEqual(rc, 0)
        word_queries = [q for q in remote.queries if "match(lower(" in q and "select count()" in q]
        for word in ("test", "testbed", "tb", "t4", "t4-trial", "queue-ens20"):
            self.assertTrue(all(word in q for q in word_queries), word)
        self.assertIn("Q7 공통 낱말 미충족", out)
        pipe = [q for q in remote.queries if "run-r[0-9a-f]{32}" in q]
        self.assertTrue(pipe)
        self.assertTrue(all("opsjob" in q and "hvjob" in q and "1450" in q and "1460" in q for q in pipe))
        # A cell whose query fails is "미측정, 이유", never empty.
        class Broken(FakeRemote):
            def vm(self, path, params):
                raise t3check.QueryError("vm down")
        broken = Broken()
        broken.pg_rules = self.remote().pg_rules
        rc, out = self.run_tool(broken)
        self.assertEqual(rc, 0)
        self.assertIn("미측정, QueryError: vm down", out.splitlines()[0])
        ctx = t3check.context(inventory())
        ctx["executor_source"], ctx["operator_source"] = "192.0.2.1", "192.0.2.5"
        w, base = trace.windows(self.START, self.START + datetime.timedelta(minutes=5), 1, datetime.timedelta(minutes=30))
        for bad in ("a b", "x'; drop", "..."):
            with self.subTest(word=bad), self.assertRaises(ValueError):
                trace.q7(self.remote(), ctx, {"web01": "1"}, "opsjob", 1450, "hvjob", 1460, [bad], w, w, base)
        with self.assertRaises(ValueError):
            trace.q7(self.remote(), ctx, {"web01": "1"}, "ops'job", 1450, "hvjob", 1460, [], w, w, base)

    def test_sudo_session_close_lines_are_counted_against_the_baseline_and_values_are_quoted(self) -> None:
        """리뷰 M3: VM 출발 sudo 세션 닫힘 줄을 주입 창과 평시 창 최댓값으로 견준다. 리뷰 L3: 119 에서 읽은 값은 따옴표
        처리를 거쳐 다음 질의에 들어간다."""
        remote = self.remote()
        injection = "2026-11-20 01:00:00"
        remote.ch_rules = [
            (lambda q: [["9", "0"]] if "session closed for user root" in q and injection in q else None, None),
            (lambda q: [["4", "0"]] if "session closed for user root" in q else None, None),
            (lambda q: [["11111111-1111-1111-1111-111111111111", "/var/log/a'b.log", "3"]]
             if "log_attributes['file_path'], count()" in q and injection in q else None, None),
        ]
        rc, out = self.run_tool(remote)
        self.assertEqual(rc, 0)
        self.assertIn("VM sudo 세션 닫힘 줄(운영자 몫 뺌) 미충족 창 9 평시 최대 4", out)
        closes = [q for q in remote.queries if "session closed for user root" in q]
        self.assertTrue(closes)
        self.assertTrue(all("192.168.200.66" not in q and "app_name = 'sudo'" in q for q in closes))
        sample = [q for q in remote.queries if "substring(body, 1, 160)" in q]
        self.assertEqual(len(sample), 1)
        self.assertIn("in ('/var/log/ab.log')", sample[0])

    def test_operator_sudo_lines_are_not_counted_as_pipeline(self) -> None:
        """리뷰 L5: 운영자 경로의 sudo 도 닫힘 줄을 남긴다(원인 흔적). 운영자 명령 줄 수만큼 빼고 따로 센다."""
        remote = self.remote()
        injection = "2026-11-20 01:00:00"
        remote.ch_rules = [
            # 주입 창: 닫힘 9 가운데 6 은 운영자 명령 6 의 짝, 실행기 몫 3. 평시 창: 닫힘 4, 운영자 0.
            (lambda q: [["9", "6"]] if "session closed for user root" in q and injection in q else None, None),
            (lambda q: [["4", "0"]] if "session closed for user root" in q else None, None),
        ]
        rc, out = self.run_tool(remote)
        self.assertEqual(rc, 0)
        self.assertIn("VM sudo 세션 닫힘 줄(운영자 몫 뺌) 충족 창 3 평시 최대 4, 운영자 sudo 명령 줄 6", out)
        closes = [q for q in remote.queries if "session closed for user root" in q]
        self.assertTrue(all("startsWith(message, 'sysop : ')" in q for q in closes))

    def test_q7c_is_judged_by_source_address_and_operator_logins_are_counted_apart(self) -> None:
        """Q35 조건 ①: Q7c 는 실행기 출발 주소(관리망 host_addr)의 새 연결만 본다. 운영자 단말 주소(operator_host_addr)의
        연결은 판정에 들지 않고 수만 따로 낸다(리뷰 L2). 운영자 로그인 수(주입 창 대 평시 창 최댓값)는 합과 VM 별로(M3),
        경로 줄 넷과 함께 낸다(조건 ②, ④)."""
        remote = self.remote()
        injection = "2026-11-20 01:00:00"
        remote.ch_rules = [
            # Connections in the injection window: two operator sessions to web01 (two source ports).
            (lambda q: [["11111111-1111-1111-1111-111111111111", "192.168.127.5", "22", "51234"],
                        ["11111111-1111-1111-1111-111111111111", "192.168.127.5", "22", "51240"]]
             if "from lucida.host_connections" in q and "remote_addr = '192.168.127.5'" in q and injection in q else None, None),
            (lambda q: [["11111111-1111-1111-1111-111111111111", "192.168.127.5", "22", "50000"]]
             if "from lucida.host_connections" in q and "remote_addr = '192.168.127.5'" in q else None, None),
            (lambda q: [["2", "2", "5", "2"]] if "TTY=pts/" in q else None, None),
            (lambda q: [["192.168.124.11", "2"]] if "Accepted publickey for sysop from 192.168.127.5 " in q and injection in q else None, None),
            (lambda q: [["192.168.125.11", "1"]] if "Accepted publickey for sysop from 192.168.127.5 " in q and "group by 1" in q else None, None),
        ]
        rc, out = self.run_tool(remote)
        self.assertEqual(rc, 0)
        # The Q7c reads (the 연결 표본 column reads every connection on purpose and is not a Q7c judgement).
        conn = [q for q in remote.queries if "toString(remote_port) from lucida.host_connections" in q]
        executor = [q for q in conn if "remote_addr = '192.168.127.1'" in q]
        operator = [q for q in conn if "remote_addr = '192.168.127.5'" in q]
        # Executor: one "seen" read from the first baseline window up to the injection window, one around it (N1).
        self.assertEqual((len(executor), len(operator), len(conn)), (2, 7, 9))
        self.assertFalse(any("startsWith(remote_addr" in q for q in conn))
        self.assertIn("Q7c(출발 192.168.127.1) 충족 []", out)
        self.assertIn("운영자 로그인(출발 192.168.127.5) 창 2 평시 최대 1", out)
        self.assertIn("'sshd_accepted': 2, 'session_opened': 2, 'sudo_tty_pts': 5, 'session_closed': 2", out)
        self.assertIn("VM 별 운영자 로그인(창, 평시 최대) {'rt-core01': (0, 0), 'web01': (2, 0), 'was01': (0, 1), "
                      "'was02': (0, 0), 'db01': (0, 0)}", out)
        self.assertIn("운영자 출발 연결 창 2 평시 최대 1", out)

    def test_a_new_executor_ssh_beside_the_always_open_one_is_a_new_connection(self) -> None:
        """리뷰 rv-t4o M1: VM 으로 들어오는 ssh 는 늘 22 번이라 출발 포트가 열쇠에 있어야 늘 열린 연결(평시에도 보임) 옆의
        새 실행기 연결이 새 연결로 잡힌다."""
        remote = self.remote()
        target = "11111111-1111-1111-1111-111111111111"
        around = "timestamp between '2026-11-20 00:50:00' and '2026-11-20 01:10:00'"
        remote.ch_rules = [
            (lambda q: [[target, "192.168.127.1", "22", "40000"], [target, "192.168.127.1", "22", "40123"]]
             if "remote_addr = '192.168.127.1'" in q and around in q else None, None),
            (lambda q: [[target, "192.168.127.1", "22", "40000"]] if "remote_addr = '192.168.127.1'" in q else None, None),
        ]
        rc, out = self.run_tool(remote)
        self.assertEqual(rc, 0)
        self.assertIn(f"Q7c(출발 192.168.127.1) 미충족 [['{target}', '192.168.127.1', '22', '40123']]", out)
        # The key is the whole tuple in the query too (ClickHouse groups by exactly those four).
        q7c = [q for q in remote.queries if "remote_addr = '192.168.127.1'" in q]
        self.assertTrue(q7c)
        for q in q7c:
            self.assertIn("select target_id, remote_addr, toString(local_port), toString(remote_port) from", q)
            self.assertIn("group by 1, 2, 3, 4 format TSV;", q)
        # Only the always-open connection: satisfied.
        remote = self.remote()
        remote.ch_rules = [(lambda q: [[target, "192.168.127.1", "22", "40000"]] if "remote_addr = '192.168.127.1'" in q else None, None)]
        rc, out = self.run_tool(remote)
        self.assertIn("Q7c(출발 192.168.127.1) 충족 []", out)

    def test_a_master_reopened_after_the_baseline_windows_is_not_a_new_connection(self) -> None:
        """rv-t4o2 N1: the always-open master reopened between the baseline windows and the injection (new source port)
        is the one connection the injection window shows: satisfied. "Seen" covers everything up to the window."""
        remote = self.remote()
        target = "11111111-1111-1111-1111-111111111111"
        around = "timestamp between '2026-11-20 00:50:00' and '2026-11-20 01:10:00'"
        remote.ch_rules = [
            (lambda q: [[target, "192.168.127.1", "22", "40200"]] if "remote_addr = '192.168.127.1'" in q and around in q else None, None),
            # The read that ends where the injection window starts saw the old master and the reopened one.
            (lambda q: [[target, "192.168.127.1", "22", "40000"], [target, "192.168.127.1", "22", "40200"]]
             if "remote_addr = '192.168.127.1'" in q and "and '2026-11-20 00:50:00'" in q else None, None),
            (lambda q: [[target, "192.168.127.1", "22", "40000"]] if "remote_addr = '192.168.127.1'" in q else None, None),
        ]
        rc, out = self.run_tool(remote)
        self.assertEqual(rc, 0)
        self.assertIn("Q7c(출발 192.168.127.1) 충족 []", out)
        seen = [q for q in remote.queries if "remote_addr = '192.168.127.1'" in q and "and '2026-11-20 00:50:00'" in q]
        self.assertEqual(len(seen), 1)

    def test_long_windows_and_naive_times_are_refused(self) -> None:
        for start, end in (("2026-11-20T10:00:00", "2026-11-20T10:20:00"),
                           ("2026-11-20T10:00:00+09:00", "2026-11-20T14:00:00+09:00"),
                           ("2026-11-20T10:00:00+09:00", "2026-11-20T09:00:00+09:00")):
            with self.subTest(start=start, end=end):
                with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                    trace.main(["--inventory", str(T_INV), "--start", start, "--end", end, "--executor", "x",
                                "--variant", "y", "--run-id", "z"], remote_factory=self.remote)


class InstallRecordTests(unittest.TestCase):
    """설치 기록 expected.sha256(러너 계약 (라), 설계 §6-4 버전 표류, 지휘 세션 결정 11-19). 러너는 `cd / && sha256sum -c` 로
    견준다. 비밀값을 채운 파일(렌더 meta.json 의 secrets 표시)은 기록하지 않는다."""

    def bundle(self, root: Path, meta_secrets: list[str] | None = None, meta: bool = True) -> Path:
        bundle = root / "bundle"
        (bundle / "steps.d").mkdir(parents=True)
        for name in ("lib.sh", "record.sh"):
            (bundle / "steps.d" / name).write_text((DEPLOY / "steps" / name).read_text())
        (bundle / "steps.d" / "place.sh").write_text("place_files place\n")
        (bundle / "host.env").write_text("HOST_NAME='x'\n")
        (bundle / "steps").write_text("place\n")
        (bundle / "files").mkdir()
        (bundle / "files" / "a").write_text("alpha\n")
        (bundle / "files" / "b").write_text("beta\n")
        (bundle / "files" / "c").write_text("password=@@SECRET:db_password@@\n")
        (bundle / "secrets").mkdir()
        (bundle / "secrets" / "db_password").write_text("Abc123def456ghi7\n")
        rows = [("/etc/app/a.conf", "files/a"), ("/etc/app/b.conf", "files/b"), ("/etc/app/c.conf", "files/c")]
        (bundle / "files.tsv").write_text("".join(f"place\t{d}\t0644\t{os.getuid()}\t{os.getgid()}\t{src}\n" for d, src in rows))
        if meta:
            secrets = {"/etc/app/c.conf": ["db_password"] if meta_secrets is None else meta_secrets}
            (bundle / "meta.json").write_text(json.dumps({"files": [
                {"dest": d, "secrets": secrets.get(d, [])} for d, _ in rows]}))
        source = (DEPLOY / "install-host.sh").read_text().replace("[[ $EUID -eq 0 ]] ||", "true ||")
        source = source.replace("LOG=/var/log/petition-deploy.log", f"LOG={bundle}/log")
        (bundle / "install.sh").write_text(source)
        return bundle

    def run_install(self, bundle: Path, place: Path, *steps: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(bundle / "install.sh"), str(bundle), *steps], capture_output=True, text=True,
                              env={**os.environ, "PLACE_ROOT": str(place)})

    def test_a_full_run_records_placed_files_without_secret_ones_and_a_partial_run_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle = self.bundle(root)
            place = root / "place"
            done = self.run_install(bundle, place, "place")
            self.assertEqual(done.returncode, 0, done.stderr)
            record = place / "var" / "lib" / "petition-deploy" / "expected.sha256"
            self.assertFalse(record.exists())
            self.assertIn("Abc123def456ghi7", (place / "etc" / "app" / "c.conf").read_text())
            (bundle / "secrets").mkdir(exist_ok=True)
            (bundle / "secrets" / "db_password").write_text("Abc123def456ghi7\n")
            done = self.run_install(bundle, place)
            self.assertEqual(done.returncode, 0, done.stderr)
            text = record.read_text()
            lines = text.splitlines()
            self.assertEqual([l.split("  ", 1)[1] for l in lines], ["/etc/app/a.conf", "/etc/app/b.conf"])
            secret_sum = hashlib.sha256((place / "etc" / "app" / "c.conf").read_bytes()).hexdigest()
            self.assertNotIn("c.conf", text)
            self.assertNotIn(secret_sum, text)
            self.assertEqual(stat.S_IMODE(record.stat().st_mode), 0o644)
            # The runner's check (cd / && sha256sum -c) passes on the placed tree and names a changed file.
            check = lambda: subprocess.run(["sha256sum", "-c", "--quiet", "-"], cwd=place, capture_output=True, text=True,
                                           input="\n".join(l.replace("  /", "  ") for l in lines) + "\n")
            self.assertEqual(check().returncode, 0)
            (place / "etc" / "app" / "a.conf").write_text("changed\n")
            done = check()
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("etc/app/a.conf: FAILED", done.stdout)
            # An installed server gets the record on its own with the record step.
            record.unlink()
            done = self.run_install(bundle, place, "record")
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(len(record.read_text().splitlines()), 2)

    def test_record_refuses_without_the_render_secret_marks(self) -> None:
        """meta.json 이 없거나, 비밀값 자리를 품은 원본의 표시가 비었으면(렌더와 묶음이 어긋남) 기록하지 않는다."""
        for case, kwargs in (("no meta", {"meta": False}), ("empty mark", {"meta_secrets": []})):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                bundle = self.bundle(root, **kwargs)
                place = root / "place"
                (place / "etc" / "app").mkdir(parents=True)
                for name in ("a", "b", "c"):
                    (place / "etc" / "app" / f"{name}.conf").write_text((bundle / "files" / name).read_text())
                done = self.run_install(bundle, place, "record")
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("설치 기록을 남기지 못함", done.stdout + done.stderr)
                self.assertIn("record 실패", done.stderr)
                self.assertFalse((place / "var" / "lib" / "petition-deploy" / "expected.sha256").exists())

    def test_real_render_bundles_leave_the_secret_files_out(self) -> None:
        """T 렌더 묶음 그대로: 비밀값을 채우는 파일(SNMP 커뮤니티, Tomcat 컨텍스트)이 빠지고 나머지는 모두 든다."""
        inv = inventory(T_INV)
        for host in inv["hosts"]:
            with self.subTest(host=host["name"]), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                bundle_root = render.write_bundle(render.render_host(inv, host), root / "bundles")
                (bundle_root / "steps.d").mkdir()
                for name in ("lib.sh", "record.sh"):
                    (bundle_root / "steps.d" / name).write_text((DEPLOY / "steps" / name).read_text())
                source = (DEPLOY / "install-host.sh").read_text().replace("[[ $EUID -eq 0 ]] ||", "true ||")
                (bundle_root / "install.sh").write_text(source.replace("LOG=/var/log/petition-deploy.log", f"LOG={root}/log"))
                place = root / "place"
                meta = json.loads((bundle_root / "meta.json").read_text())["files"]
                for entry in meta:
                    target = place / entry["dest"].lstrip("/")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(bundle_root / "files" / entry["dest"].lstrip("/"), target)
                done = subprocess.run(["bash", str(bundle_root / "install.sh"), str(bundle_root), "record"], capture_output=True,
                                      text=True, env={**os.environ, "PLACE_ROOT": str(place)})
                self.assertEqual(done.returncode, 0, done.stderr)
                recorded = [l.split("  ", 1)[1] for l in
                            (place / "var/lib/petition-deploy/expected.sha256").read_text().splitlines()]
                secret = {e["dest"] for e in meta if e["secrets"]}
                self.assertEqual(set(recorded), {e["dest"] for e in meta} - secret)
                self.assertTrue(secret.isdisjoint(recorded))
                if host["role"] == "router":
                    self.assertIn("/etc/snmp/snmpd.community.conf", secret)
                if host["role"] == "was":
                    self.assertIn("/etc/tomcat10/Catalina/localhost/petition.xml", secret)


patterns = _load("pipeline_patterns", DEPLOY / "tools" / "pipeline_patterns.py")


class PipelinePatternTests(unittest.TestCase):
    """실행기 배관 줄 꼴의 정의 한 곳(tools/pipeline_patterns.py, v4 T4F, 11-20 APT4 차례 8 의 `user-runtime-dir@1460` 틈).
    VM 첫 규칙, 66 거르기, runbook 확인 명령, 흔적 표 도구가 모두 같은 꼴을 쓰고, systemd 255/257 이 세션을 열고 닫을 때
    내는 uid, 이름 줄이 모두 걸리며, 운영자 줄과 다른 uid 는 걸리지 않는다."""

    OTHER_UIDS = ("14500", "1451", "11450", "145")

    def ere(self, pattern: str) -> re.Pattern:
        return re.compile(pattern.replace("[:alnum:]", "A-Za-z0-9"))

    def test_every_session_line_of_the_account_matches_and_others_do_not(self) -> None:
        for name, uid in (("opsjob", 1450), ("hvjob", 1460)):
            check = self.ere(patterns.check_ere([(name, uid)]))
            uid_only = self.ere(patterns.uid_ere(uid))
            for prog, line in patterns.systemd_session_lines(name, uid):
                with self.subTest(account=name, line=line):
                    self.assertRegex(line, check)
                    if name not in line:
                        self.assertRegex(line, uid_only, "이름이 없는 줄은 uid 꼴이 잡아야 한다")
            # The line APT4 found: the template unit instance with the uid after @.
            self.assertRegex(f"user-runtime-dir@{uid}.service: Deactivated successfully.", uid_only)
            for other in self.OTHER_UIDS:
                for prog, line in patterns.systemd_session_lines("sysop", other):
                    with self.subTest(account=name, other=other, line=line):
                        self.assertNotRegex(line, check)
            for prog, line in patterns.systemd_session_lines("sysop", 1000):
                self.assertNotRegex(line, check)
            # A session number equal to the uid is not the uid.
            self.assertNotRegex(f"Started session-{uid}.scope - Session {uid} of User sysop.", uid_only)
            for line in patterns.UNATTRIBUTABLE:
                self.assertNotRegex(line.format(s=uid), check)

    def _session(self, name: str, uid: int | str) -> list[tuple[str, str, str]]:
        return [("daemon.info" if prog == "systemd" else "authpriv.info", prog, line)
                for prog, line in patterns.systemd_session_lines(name, uid)]

    @unittest.skipUnless(shutil.which("rsyslogd") and shutil.which("logger"), "rsyslogd 와 logger 가 필요")
    def test_real_rsyslogd_diverts_every_session_line_on_the_vm_and_on_66(self) -> None:
        sys.path.insert(0, str(HERE))
        import vm_sandbox
        inv = inventory()
        vm_rule = file_of(bundles(T_INV)["web01"], "/etc/rsyslog.d/00-opsjobs.conf").replace("/var/log/opsjobs.log", "{diverted}")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            stage = root / "stage"
            shutil.copytree(HV, stage)
            subprocess.run([sys.executable, str(HV / "build.py"), "--inventory", str(T_INV), "--stage", str(stage)],
                           check=True, capture_output=True, timeout=60)
            hv_rule = (stage / "05-hvjobs.conf").read_text().replace("/var/log/hvjobs.log", "{diverted}")
            executor = inv["accounts"]["executor"]
            operator = inv["accounts"]["operator"]
            for label, rule, name, uid in (("vm", vm_rule, executor["name"], executor["uid"]), ("66", hv_rule, "hvjob", 1460)):
                with self.subTest(where=label):
                    own = self._session(name, uid)
                    others = self._session(operator["name"], operator["uid"])
                    for other in self.OTHER_UIDS:
                        others += self._session("someone", other)
                    forward, diverted = vm_sandbox.run_rsyslogd(rule, own + others, root / f"rs-{label}")
                    for _, _, line in own:
                        self.assertIn(line, diverted)
                        self.assertNotIn(line, forward)
                    for _, _, line in others:
                        self.assertIn(line, forward)
                        self.assertNotIn(line, diverted)

    def test_both_rules_and_the_tools_take_the_pattern_from_the_one_definition(self) -> None:
        inv = inventory()
        executor = inv["accounts"]["executor"]
        vm_rule = file_of(bundles(T_INV)["web01"], "/etc/rsyslog.d/00-opsjobs.conf")
        self.assertIn(patterns.rainer(patterns.uid_ere(executor["uid"])), vm_rule)
        self.assertIn(patterns.rainer(patterns.name_ere(executor["name"])), vm_rule)
        template = (DEPLOY / "templates" / "common" / "rsyslog-00-opsjobs.conf").read_text()
        hv_template = (HV / "rsyslog-05-hvjobs.conf").read_text()
        for text in (template, hv_template):
            body = "\n".join(l for l in text.splitlines() if not l.startswith("#"))
            self.assertNotIn("user[@-]", body)
            self.assertNotIn("UID |", body)
        trace_source = (DEPLOY / "tools" / "trace_table.py").read_text()
        self.assertIn("pipeline_patterns.check_ere(", trace_source)
        self.assertNotIn("(user[@-]", trace_source)

    def test_runbook_checks_call_the_pattern_tool_and_its_output_counts_the_session_lines(self) -> None:
        runbook = (REPO / "docs" / "runbook-civil-petition-apply.md").read_text()
        rows = [l for l in runbook.splitlines() if l.startswith("| rsyslog 첫 규칙 |") or l.startswith("| 8 | 119 읽기")]
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertIn("pipeline_patterns.py --account", row)
        self.assertIn("--account hvjob:1460 --extra t4-hv-exec --format clickhouse", rows[2])
        with tempfile.TemporaryDirectory() as raw:
            for account, extra in (("opsjob:1450", []), ("hvjob:1460", ["--extra", "t4-hv-exec"])):
                name, uid = account.split(":")
                ere = subprocess.run([sys.executable, str(DEPLOY / "tools" / "pipeline_patterns.py"), "--account", account, *extra],
                                     check=True, capture_output=True, text=True).stdout.strip()
                ch = subprocess.run([sys.executable, str(DEPLOY / "tools" / "pipeline_patterns.py"), "--account", account, *extra,
                                     "--format", "clickhouse"], check=True, capture_output=True, text=True).stdout.strip()
                self.assertEqual(ch, ere)  # no backslash or quote to escape today
                own = Path(raw) / f"own-{name}"
                own.write_text("".join(f"Nov 20 02:41:04 host {p}[1]: {l}\n" for p, l in patterns.systemd_session_lines(name, uid)))
                other = Path(raw) / f"other-{name}"
                other.write_text("".join(f"Nov 20 02:41:04 host {p}[1]: {l}\n" for o in self.OTHER_UIDS + ("1000",)
                                         for p, l in patterns.systemd_session_lines("sysop", o)))
                count = lambda path: int(subprocess.run(["grep", "-E", "-c", ere, str(path)], capture_output=True, text=True).stdout.strip() or 0)  # noqa: E731
                self.assertEqual(count(own), len(patterns.systemd_session_lines(name, uid)))
                self.assertEqual(count(other), 0)


class OperatorTerminalTests(unittest.TestCase):
    """운영자 단말(설계 §6-2 운영자 경로, Q35 조건 ①): 실행기 늘 열린 연결과 다른 출발 주소. 인벤토리 세 칸, 66 의 관리 브리지
    둘째 주소와 SNAT, FORWARD 허용, VM 과 라우터 관리 보호 표의 받아들임 줄, 운영자 authorized_keys 의 from= 줄."""

    def test_inventory_fields_come_together_and_differ_from_the_executor(self) -> None:
        for path in (T_INV, S_INV):
            inv = inventory(path)
            render.validate(inv)
            for change in ("drop_hv", "drop_host", "drop_key", "same_host", "same_runner", "outside"):
                bad = json.loads(json.dumps(inv))
                mgmt = next(n for n in bad["networks"].values() if n.get("management"))
                if change == "drop_hv":
                    del bad["hypervisor"]["operator_addr"]
                elif change == "drop_host":
                    del mgmt["operator_host_addr"]
                elif change == "drop_key":
                    del bad["accounts"]["operator"]["terminal_pubkey_file"]
                elif change == "same_host":
                    mgmt["operator_host_addr"] = mgmt["host_addr"]
                elif change == "same_runner":
                    bad["hypervisor"]["operator_addr"] = bad["hypervisor"]["runner_addr"]
                else:
                    mgmt["operator_host_addr"] = "192.168.200.5"
                with self.subTest(inventory=path.name, change=change), self.assertRaises(render.InventoryError):
                    render.validate(bad)

    def test_guard_tables_take_the_operator_source_and_keep_it_tracked(self) -> None:
        for path, host, mgmt_if, source in ((T_INV, "web01", "ens19", "192.168.127.5"), (T_INV, "rt-core01", "ens22", "192.168.127.5"),
                                            (S_INV, None, None, "192.168.133.5")):
            all_bundles = bundles(path)
            names = [host] if host else [b for b in all_bundles if all_bundles[b]["role"] != "loadgen"][:1]
            for name in names:
                guard = file_of(all_bundles[name], "/etc/nftables.d/10-mgmt-guard.nft")
                nic = mgmt_if or next(l.split('"')[1] for l in guard.splitlines() if "iifname" in l)
                with self.subTest(inventory=path.name, host=name):
                    self.assertIn(f'iifname "{nic}" ip saddr {source} tcp dport 22 accept', guard)
                    # Only the executor's management ssh is untracked (cleanup during a full conntrack table).
                    self.assertNotIn(f"ip saddr {source} ip daddr", guard)
                    self.assertEqual(all_bundles[name]["env"]["MGMT_OPERATOR"], source)
        verify = (DEPLOY / "steps" / "verify.sh").read_text()
        self.assertIn('nft_table_has mgmtguard "iifname \\"$MGMT_IF\\" ip saddr $MGMT_OPERATOR tcp dport 22 accept"', verify)

    def test_terminal_key_is_bound_to_the_operator_source(self) -> None:
        apply = (DEPLOY / "apply.sh").read_text()
        self.assertIn("terminal_key=$(field_or_empty accounts.operator.terminal_pubkey_file)", apply)
        self.assertIn('bind_from "$(field_or_empty networks.mgmt.operator_host_addr)" "$(expand "$terminal_key")" '
                      '>> "$bundle/secrets/$operator.pub"', apply)
        # field_or_empty answers an empty string for a missing field (S without a terminal would not break).
        with tempfile.TemporaryDirectory() as raw:
            inv = Path(raw) / "inv.json"
            inv.write_text(json.dumps({"networks": {"mgmt": {"operator_host_addr": "192.0.2.5"}}}))
            fn = re.search(r"^field_or_empty\(\) \{.*?\n[^\n]*\n[^\n]*\n[^\n]*; \}\n", apply, re.S | re.M).group(0)
            out = subprocess.run(["bash", "-c", fn + f'inventory={shlex.quote(str(inv))}; field_or_empty networks.mgmt.operator_host_addr; '
                                  'echo "[$(field_or_empty accounts.operator.terminal_pubkey_file)]"'],
                                 capture_output=True, text=True, check=True).stdout.splitlines()
            self.assertEqual(out, ["192.0.2.5", "[]"])

    def test_executor_key_is_bound_to_the_management_host_address(self) -> None:
        """지휘 세션 결정(T4O 확인 필요 1): 실행기 키도 from="<관리망 host_addr>" 로 묶어, 실행기가 운영자 출발 주소로 붙을 길을
        키로 막는다. 묶는 함수의 동작, 설치 묶음이 그 함수로 실행기 키를 쓰는지, verify 가 VM 에 놓인 키 줄을 보는지."""
        apply = (DEPLOY / "apply.sh").read_text()
        self.assertIn('bind_from "$(field networks.mgmt.host_addr)" "$(expand "$(field accounts.executor.pubkey_file)")" '
                      '> "$bundle/secrets/$executor.pub"', apply)
        self.assertNotIn('install -m 0600 "$(expand "$(field accounts.executor.pubkey_file)")"', apply)
        fn = re.search(r"^bind_from\(\) \{\n.*?\n\}\n", apply, re.S | re.M).group(0)
        with tempfile.TemporaryDirectory() as raw:
            key = Path(raw) / "k.pub"
            key.write_text("ssh-ed25519 AAAA one\n\n# note\nssh-ed25519 BBBB two\n")
            done = subprocess.run(["bash", "-c", fn + 'bind_from 192.168.127.1 "$1"', "_", str(key)],
                                  capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(done.stdout.splitlines(), ['from="192.168.127.1" ssh-ed25519 AAAA one',
                                                        'from="192.168.127.1" ssh-ed25519 BBBB two'])
            for bad in ("", "192.168.127", "192.168.127.1 x"):
                with self.subTest(addr=bad):
                    self.assertEqual(subprocess.run(["bash", "-c", fn + 'bind_from "$1" "$2"', "_", bad, str(key)],
                                                    capture_output=True, text=True).returncode, 2)
        # The verify item on the VM: every executor key line from the management host address, one operator terminal line.
        verify = (DEPLOY / "steps" / "verify.sh").read_text()
        self.assertIn("check account-keys-from account_keys_from_ok", verify)
        fn = re.search(r"^account_keys_from_ok\(\) \{\n.*?\n\}\n", verify, re.S | re.M).group(0)
        good_exec = 'from="192.168.127.1" ssh-ed25519 AAAA x\n'
        good_op = 'ssh-ed25519 CCCC laptop\nfrom="192.168.127.5" ssh-ed25519 DDDD terminal\n'
        cases = [("good", good_exec, good_op, "192.168.127.5", 0),
                 ("good without terminal", good_exec, "ssh-ed25519 CCCC laptop\n", "", 0),
                 ("executor unbound", "ssh-ed25519 AAAA x\n", good_op, "192.168.127.5", 1),
                 ("executor second line unbound", good_exec + "ssh-ed25519 EEEE y\n", good_op, "192.168.127.5", 1),
                 ("executor bound to the operator source", 'from="192.168.127.5" ssh-ed25519 AAAA x\n', good_op,
                  "192.168.127.5", 1),
                 ("executor empty", "", good_op, "192.168.127.5", 1),
                 ("terminal missing", good_exec, "ssh-ed25519 CCCC laptop\n", "192.168.127.5", 1),
                 ("terminal twice", good_exec, good_op + 'from="192.168.127.5" ssh-ed25519 DDDD terminal\n',
                  "192.168.127.5", 1)]
        for name, exec_keys, op_keys, op_addr, want in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                for account, text in (("opsjob", exec_keys), ("sysop", op_keys)):
                    (root / account / ".ssh").mkdir(parents=True)
                    (root / account / ".ssh" / "authorized_keys").write_text(text)
                script = (f'getent() {{ echo "$2:x:1:1::{root}/$2:/bin/bash"; }}\n' + fn +
                          f'EXECUTOR_NAME=opsjob OPERATOR_NAME=sysop MGMT_HOST=192.168.127.1 MGMT_OPERATOR="{op_addr}" '
                          'account_keys_from_ok')
                self.assertEqual(subprocess.run(["bash", "-c", script], capture_output=True, text=True).returncode, want)

    def test_only_the_operator_session_binds_the_terminal_address(self) -> None:
        """실행기 쪽 새 연결(cleanup, recovery 가 마스터 없이 여는 연결)과 러너의 늘 열린 연결, 평시 점검 읽기는 출발 주소를 묶지
        않아 tb-ops 의 첫 주소(runner_addr)로 나가고, 66 이 관리망 host_addr 로 SNAT 한다. 덧붙인 운영자 주소는 관리 브리지의
        둘째 주소라 66 자신이 여는 연결도 host_addr 로 나간다. 66 hvjob 키는 이미 from="<runner_addr>" 로 묶여 있다."""
        import importlib.util
        profiles = str(HERE.parent / "profiles")
        sys.path.insert(0, profiles)
        try:
            spec = importlib.util.spec_from_file_location("vm_executor_common_t4o", HERE.parent / "profiles" / "vm_executor_common.py")
            vm = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(vm)
        finally:
            sys.path.remove(profiles)
        head = vm.RUNNER_HEAD.decode()
        arrays = dict(re.findall(r"^(\w+)=\(ssh (.*?)\)\n", head, re.S | re.M))
        self.assertEqual(sorted(arrays), ["op_ssh", "ssh_hv", "ssh_vm"])
        for name in ("ssh_vm", "ssh_hv"):
            self.assertNotIn(" -b ", f" {arrays[name]} ", name)
            self.assertNotIn("BindAddress", arrays[name], name)
        self.assertIn("-b 192.168.122.21", arrays["op_ssh"])
        self.assertEqual(head.count(" -b "), 1)
        inv = inventory(T_INV)
        self.assertEqual(vm.OPERATOR_BIND_ADDR, inv["hypervisor"]["operator_addr"])
        net = render.hypervisor_files(inv)["/etc/network/interfaces.d/t4-net"]
        stanza = net.split("iface vmbr11 ", 1)[1].split("\nauto ", 1)[0]
        self.assertEqual(re.findall(r"^\taddress (\S+)$", stanza, re.M), ["192.168.127.1/24", "192.168.127.5/24"])
        self.assertIn("-s 192.168.122.20 -o vmbr11 -p tcp --dport 22 -j SNAT --to-source 192.168.127.1", stanza)
        self.assertIn("-s 192.168.122.21 -o vmbr11 -p tcp --dport 22 -j SNAT --to-source 192.168.127.5", stanza)
        helper = (DEPLOY / "hypervisor" / "install-hv-helper.sh").read_text()
        self.assertIn('from="%s" \' \\\n    "$sbin" "$runner_addr"', helper)


    def test_the_install_path_from_104_is_not_bound(self) -> None:
        """지휘 세션 덧(T4O 3회차): 실행기 키와 운영자 단말 키에 from= 을 묶어도 104 의 설치 경로(운영자 계정, 104 사용자 자신의
        키, 66 을 거쳐 66 관리 주소에서 나감)는 묶이지 않아 되돌리기가 잠기지 않는다."""
        apply = (DEPLOY / "apply.sh").read_text()
        # The installer's ssh: the operator account, through 66 (-J), no key option (the 104 user's own key).
        self.assertIn('ssh_opts=(-o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new '
                      '-o "UserKnownHostsFile=$known_hosts" -J "$jump")', apply)
        self.assertIn('jump="root@192.168.200.66"', apply)
        self.assertIn('ssh "${ssh_opts[@]}" "$operator@$addr" "$remote"', apply)
        self.assertNotRegex(apply, r"ssh_opts=\([^)]*-i ")
        self.assertIn('operator=$(field accounts.operator.name)', apply)
        # The operator's own key (the 104 key) goes in as it is; only the terminal line gets from=.
        self.assertIn('install -m 0600 "$(expand "$(field accounts.operator.pubkey_file)")" "$bundle/secrets/$operator.pub"', apply)
        for path in (T_INV, S_INV):
            inv = inventory(path)
            with self.subTest(inventory=path.name):
                self.assertNotEqual(inv["accounts"]["operator"]["pubkey_file"], inv["accounts"]["operator"]["terminal_pubkey_file"])
                self.assertNotEqual(inv["accounts"]["operator"]["pubkey_file"], inv["accounts"]["executor"]["pubkey_file"])
        # The VM check allows the 104 key line next to the one bound terminal line.
        verify = (DEPLOY / "steps" / "verify.sh").read_text()
        self.assertIn('[[ "$(grep -c "^from=\\"$MGMT_OPERATOR\\" " "$keys")" == 1 ]]', verify)
        # The jump's last hop leaves 66 from the management bridge's first (primary) address, the executor's.
        net = render.hypervisor_files(inventory(T_INV))["/etc/network/interfaces.d/t4-net"]
        stanza = net.split("iface vmbr11 ", 1)[1]
        self.assertEqual(re.findall(r"^\taddress (\S+)$", stanza, re.M)[0], "192.168.127.1/24")
