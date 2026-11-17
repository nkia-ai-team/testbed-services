"""전통 3계층 테스트베드(civil-petition) 구성 코드 시험.

설계 정본: docs/spec-testbed-vm-3tier.md §2-5, §3, §5, §10 T2. S 문서 spec-testbed-sms-only.md §2-3, §2-4.
VM 과 66, 119 에는 닿지 않는다. 렌더는 메모리와 임시 디렉터리에서, 큐 버림 보고는 사용자 네임스페이스의
진짜 커널 qdisc 로 본다(N1 의 netns 시험과 같은 방식).
"""
from __future__ import annotations

import collections
import contextlib
import datetime
import importlib.util
import io
import json
import math
import random
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
import unittest.mock
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
RSYSLOG_DESTS = ("/etc/rsyslog.conf", "/etc/rsyslog.d/", "/etc/rsyslog.remote.d/")
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
        optional_dests = {"/etc/rsyslog.remote.d/10-forward.conf", "/etc/default/lldpd"}
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
            t_steps = [x for x in t_bundle["steps"] if x not in {"lldpd", "syslog-forward"}]
            self.assertEqual(t_steps, s_bundle["steps"])
        self.assertGreater(compared, 50)

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
        self.assertLess(conf.index("$IncludeConfig /etc/rsyslog.d/*.conf"), conf.index("rsyslog.remote.d"))

    def test_rsyslog_first_rule_regex_matches_executor_lines_only(self) -> None:
        inv = inventory()
        name, uid = inv["accounts"]["executor"]["name"], str(inv["accounts"]["executor"]["uid"])
        rule = file_of(self.t["web01"], "/etc/rsyslog.d/00-opsjobs.conf")
        word = re.search(r're_match\(\$msg, "(\(\^\|\[\^\[:alnum:\]_\.-\]\)' + re.escape(name) + r'[^"]*)"\)', rule).group(1)
        uid_re = re.search(r're_match\(\$msg, "(\(user\[@-\][^"]*)"\)', rule).group(1)
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
        self.assertEqual(seen, {"/etc/rsyslog.conf", "/etc/rsyslog.d/00-opsjobs.conf", "/etc/rsyslog.remote.d/10-forward.conf"})

    def test_bare_dollar_scan_catches_the_unescaped_form(self) -> None:
        self.assertEqual(rainer_bare_dollars('if re_match($msg, "(a|$)") then stop\n'), ['"(a|$)" (1행)'])
        self.assertEqual(rainer_bare_dollars('if re_match($msg, "(a|\\$)") then stop\n'), [])
        self.assertEqual(rainer_bare_dollars('# "$ 주석" 은 보지 않는다\n$IncludeConfig /etc/rsyslog.d/*.conf\n'), [])
        self.assertEqual(rainer_bare_dollars('action(type="omfile" file="/var/log/a\\"$b")\n'), ['"/var/log/a\\"$b" (1행)'])
        self.assertEqual(rainer_unescape("(a|\\$)\\\\"), "(a|$)\\")
        with self.assertRaises(ValueError):
            rainer_unescape("\\.")

    def test_syslog_forward_targets_the_tunnel_address(self) -> None:
        forward = file_of(self.t["db01"], "/etc/rsyslog.remote.d/10-forward.conf")
        self.assertIn('target="10.250.0.1"', forward)
        self.assertIn('protocol="udp"', forward)

    def test_management_nic_guard_on_servers_and_router(self) -> None:
        guard = file_of(self.t["was01"], "/etc/nftables.d/10-mgmt-guard.nft")
        self.assertIn('iifname "ens19" ip saddr 192.168.127.1 tcp dport 22 accept', guard)
        self.assertIn('iifname "ens19" drop', guard)
        router_guard = file_of(self.t["rt-core01"], "/etc/nftables.d/10-mgmt-guard.nft")
        self.assertIn('iifname "ens22" counter drop', router_guard)
        self.assertIn('oifname "ens22" counter drop', router_guard)
        self.assertIn('iifname != "ens22" ip daddr 192.168.127.0/24 drop', router_guard)
        netplan = file_of(self.t["was01"], "/etc/netplan/01-host.yaml")
        mgmt = netplan[netplan.index("ens19:"):]
        self.assertNotIn("routes:", mgmt)

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
        self.assertNotIn("/etc/rsyslog.remote.d/10-forward.conf", dests)

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
        self.assertEqual(re.findall(r"^\taddress (\S+)$", self.net, re.M), ["192.168.123.1/24", "192.168.127.1/24"])
        for bridge in ("vmbr8", "vmbr9", "vmbr10"):
            self.assertIn(f"iface {bridge} inet manual", self.net)

    def test_nat_is_four_masquerades_and_one_runner_snat(self) -> None:
        added = set(re.findall(r"iptables -t nat -A (POSTROUTING .*?)$", self.net, re.M))
        masq = [rule for rule in added if "MASQUERADE" in rule]
        snat = [rule for rule in added if "SNAT" in rule]
        self.assertEqual(len(masq), 4)
        self.assertEqual(len(snat), 1)
        self.assertNotIn("192.168.127.0/24 -o vmbr0.200", " ".join(masq))
        self.assertIn("-s 192.168.122.20 -o vmbr11 -p tcp --dport 22 -j SNAT --to-source 192.168.127.1", snat[0])

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
        # tb-ops 에서 200.66:8091 은 T2 전에도 안 되는 길이다(DNAT 는 -i vmbr0.200 에만, 66 은 8091 을 듣지 않음).
        for line in runbook.splitlines():
            if "tb-ops" in line and "192.168.200.66:8091" in line:
                self.assertRegex(line, r"안 되는|거절")

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
"""rsyslogd -N1 -f <설정> 대역. $IncludeConfig 와 include(file=...) 를 따라 읽은 파일을 STUB_LOG 에 적고,
읽은 파일 가운데 BAD 가 든 것이 있거나 설정 경로가 FAKE_FAIL_CONF 이면 실패한다."""
import glob, os, re, sys
args = sys.argv[1:]
assert args[:2] == ["-N1", "-f"] and len(args) == 3, args
conf = args[2]
seen = []
def walk(path):
    seen.append(path)
    text = open(path, encoding="utf-8").read()
    for pattern in re.findall(r'^\$IncludeConfig (\S+)|include\(file="([^"]+)"', text, re.M):
        for match in sorted(glob.glob(pattern[0] or pattern[1])):
            walk(match)
walk(conf)
with open(os.environ["STUB_LOG"], "a") as log:
    log.write("rsyslogd -f " + conf + " read " + " ".join(seen) + "\n")
bad = [path for path in seen if "BAD" in open(path, encoding="utf-8").read()]
if bad:
    print(f"error during parsing file {bad[0]}", file=sys.stderr)
    sys.exit(1)
if conf == os.environ.get("FAKE_FAIL_CONF"):
    print("error after placing", file=sys.stderr)
    sys.exit(1)
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

    def _rsyslog_tree(self, root: Path, rule: str = "new rule\n", default: str = "default rules\n") -> tuple[dict, Path, Path]:
        """지금 설정(old main, 50-default, old rule)이 있는 가짜 /etc 와 syslog, syslog-forward 단계 묶음."""
        etc = root / "etc"
        (etc / "rsyslog.d").mkdir(parents=True)
        includes = f'$IncludeConfig {etc}/rsyslog.d/*.conf\ninclude(file="{etc}/rsyslog.remote.d/*.conf" mode="optional")\n'
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
        user = subprocess.run(["id", "-un"], capture_output=True, text=True, check=True).stdout.strip()
        group = subprocess.run(["id", "-gn"], capture_output=True, text=True, check=True).stdout.strip()
        rows = [("syslog", etc / "rsyslog.conf", "rsyslog.conf"), ("syslog", etc / "rsyslog.d" / "00-opsjobs.conf", "00-opsjobs.conf"),
                ("syslog", etc / "logrotate.d" / "opsjobs", "logrotate"),
                ("syslog-forward", etc / "rsyslog.remote.d" / "10-forward.conf", "10-forward.conf")]
        (bundle / "files.tsv").write_text("".join(f"{step}\t{dest}\t0644\t{user}\t{group}\tfiles/{src}\n" for step, dest, src in rows))
        bin_dir = root / "bin"
        bin_dir.mkdir()
        (bin_dir / "rsyslogd").write_text(RSYSLOGD_STUB)
        (bin_dir / "systemctl").write_text(f"#!/bin/sh\necho systemctl \"$@\" >> {root / 'calls'}\n")
        for tool in ("rsyslogd", "systemctl"):
            (bin_dir / tool).chmod(0o755)
        (root / "tmp").mkdir()
        env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "STUB_LOG": str(root / "calls"), "TMPDIR": str(root / "tmp"),
               "RSYSLOG_ETC": str(etc)}
        return env, etc, bundle

    def _run_step(self, bundle: Path, step: str, env: dict) -> subprocess.CompletedProcess:
        script = f"set -euo pipefail; BUNDLE={bundle}; . {DEPLOY / 'steps' / 'lib.sh'}; . {DEPLOY / 'steps' / (step + '.sh')}"
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)

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
                calls = (root / "calls").read_text().splitlines()
                self.assertEqual(len(calls), 1, calls)
                staged = calls[0]
                self.assertTrue(staged.startswith(f"rsyslogd -f {root / 'tmp'}/"), staged)
                self.assertNotIn(f" {etc}/", staged)
                for name in ("rsyslog.d/00-opsjobs.conf", "rsyslog.d/50-default.conf"):
                    self.assertIn(f"{etc}/{name}", staged)
                self.assertEqual(list((root / "tmp").iterdir()), [])

    def test_rsyslog_forward_step_checks_a_copy_first_then_the_placed_config_then_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env, etc, bundle = self._rsyslog_tree(root)
            done = self._run_step(bundle, "syslog-forward", env)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual((etc / "rsyslog.remote.d" / "10-forward.conf").read_text(), "forward\n")
            self.assertEqual((etc / "rsyslog.d" / "00-opsjobs.conf").read_text(), "old rule\n")
            calls = (root / "calls").read_text().splitlines()
            self.assertEqual(len(calls), 3, calls)
            self.assertTrue(calls[0].startswith(f"rsyslogd -f {root / 'tmp'}/"), calls[0])
            self.assertIn("rsyslog.remote.d/10-forward.conf", calls[0])
            self.assertNotIn(f" {etc}/", calls[0])
            self.assertEqual(calls[1], f"rsyslogd -f {etc}/rsyslog.conf read {etc}/rsyslog.conf {etc}/rsyslog.d/00-opsjobs.conf "
                                       f"{etc}/rsyslog.d/50-default.conf {etc}/rsyslog.remote.d/10-forward.conf")
            self.assertEqual(calls[2], "systemctl restart rsyslog")
            self.assertEqual(list((root / "tmp").iterdir()), [])

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
                self.assertFalse((etc / "rsyslog.remote.d" / "10-forward.conf").exists())
                self.assertEqual(sorted(p.name for p in etc.rglob("*.new")), [])
                calls = (root / "calls").read_text().splitlines()
                self.assertEqual([c.split(" read ")[0] for c in calls][1:], [f"rsyslogd -f {etc}/rsyslog.conf"])
                self.assertNotIn("systemctl restart rsyslog", calls)
                self.assertEqual(list((root / "tmp").iterdir()), [])

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
        guard = source.index("rsyslog.remote.d/*.conf")
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


if __name__ == "__main__":
    unittest.main()
