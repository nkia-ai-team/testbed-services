"""전통 3계층 테스트베드(civil-petition) 구성 코드 시험.

설계 정본: docs/spec-testbed-vm-3tier.md §2-5, §3, §5, §10 T2. S 문서 spec-testbed-sms-only.md §2-3, §2-4.
VM 과 66, 119 에는 닿지 않는다. 렌더는 메모리와 임시 디렉터리에서, 큐 버림 보고는 사용자 네임스페이스의
진짜 커널 qdisc 로 본다(N1 의 netns 시험과 같은 방식).
"""
from __future__ import annotations

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
        posix = lambda pattern: pattern.replace("[:alnum:]", "A-Za-z0-9")  # noqa: E731
        hit = [f"Accepted publickey for {name} from 192.0.2.1 port 50000 ssh2",
               f"pam_unix(sudo:session): session opened for user root(uid=0) by {name}(uid={uid})",
               f"New session 4 of user {name}."]
        miss = ["Accepted publickey for sysop from 192.0.2.1 port 1 ssh2", "Started session-1450.scope.",
                f"{name}s logged", f"/var/log/{name}s.log rotated"]
        for line in hit:
            self.assertRegex(line, posix(word))
        for line in miss:
            self.assertNotRegex(line, posix(word))
        self.assertRegex(f"Started user@{uid}.service - User Manager for UID {uid}.", uid_re)
        self.assertRegex(f"Removed slice user-{uid}.slice.", uid_re)
        self.assertNotRegex(f"Started session-{uid}.scope - Session {uid} of User sysop.", uid_re)

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


IPTABLES_SCRIPT = r'''
import json, re, subprocess, sys
net_file, rules_tmp, rules_path = sys.argv[1], sys.argv[2], sys.argv[3]
stanzas, current = {}, None
for line in open(net_file, encoding="utf-8"):
    m = re.match(r"auto (\S+)", line)
    if m:
        current = m.group(1)
        stanzas[current] = {"up": [], "down": []}
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
def legacy():
    # 66 의 기존 tb-nat 과 같은 꼴(확인 없는 -A).
    subprocess.run("iptables -t nat -A POSTROUTING -s 192.168.122.0/24 -o vmbr0.200 -j MASQUERADE", shell=True, check=True)
for name in stanzas:
    subprocess.run(["ip", "link", "add", name, "type", "dummy"], check=True)
    subprocess.run(["ip", "link", "set", name, "up"], check=True)
legacy()
subprocess.run("iptables -t nat -A PREROUTING -d 192.168.200.66/32 -i vmbr0.200 -p tcp --dport 8091 -j DNAT --to-destination 192.168.122.20:8091", shell=True, check=True)
out = {"order": list(stanzas), "before": save()}
for name in stanzas: up(name)
for name in stanzas: up(name)          # 같은 post-up 이 다시 돌아도 한 벌
out["up"] = save()
mgmt = list(stanzas)[-1]
down(mgmt)
out["mgmt_down"] = save()
up(mgmt)
out["mgmt_up_again"] = save()
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
          for c in stanzas[first]["up"] if not c.startswith("ip route") and "-t nat" not in c]
link = subprocess.run(["ip", "-j", "link", "show", first], capture_output=True, text=True, check=True).stdout
out["missing_rules"] = {"failed": failed, "link": json.loads(link)[0]["flags"], "save": save()}
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
                                   render.rules_path(cls.inv)], capture_output=True, text=True, timeout=120)
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
        self.assertEqual(weights, [8, 30, 25, 15, 10, 7, 5])

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
    ARCHIVE_OK = {"finished_at": "2026-11-16T17:23:10Z", "rows": 121000, "result": "ok"}

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
            db = self._db(runs=[{"finished_at": (end - datetime.timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
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

    def test_representative_query_without_samples_fails(self) -> None:
        windows = self._windows()
        for record in windows.values():
            record.pop("step_p95_ms")
        result = stability.judge(windows, self.END, db_facts=self._db())
        self.assertEqual(result["verdict"], "FAIL_Q3_24H")
        self.assertIn("대표 질의(search) p95 표본 부족(처음 0창, 끝 0창, 구간마다 22창 이상)", result["reasons"])

    def test_the_recording_preflight_read_is_light_and_leaves_no_wait_or_error_lines(self) -> None:
        # 리뷰 rv-t2c M2: 녹화마다 한 번 읽는 보관 상태는 표를 훑거나 민원 표를 잠그지 않고, 잠금 대기 줄과 오류 줄을
        # 남기지 않는다. 출력 꼴은 러너가 읽는 db-facts.sh 와 같다(rows.petition, last_archive_ok).
        source = (DEPLOY / "tools" / "archive-state.sh").read_text()
        sql = source[source.index("<<'SQL'"):source.index("\nSQL\n")]
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
        for key in ("'rows'", "'petition'", "'last_archive_ok'", '"db":%s', '"at":"%s"'):
            self.assertIn(key, source)

    def test_db_facts_files_are_read_from_the_tool_output_shape(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            before = Path(raw) / "b.jsonl"
            after = Path(raw) / "a.jsonl"
            before.write_text('{"host":"db01","at":"2026-11-16T15:00:00Z","db":{"rows":{"petition":2000000,"petition_history":5900000},'
                              '"archive_runs":[]}}\n')
            after.write_text('{"host":"db01","at":"2026-11-17T15:00:00Z","db":{"rows":{"petition":2118000,"petition_history":6170000},'
                             '"archive_runs":[{"finished_at":"2026-11-16T17:23:10Z","rows":121000,"result":"ok"}]}}\n')
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
                     "archive-state.sh"):
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
