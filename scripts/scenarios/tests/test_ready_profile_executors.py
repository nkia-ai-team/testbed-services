from __future__ import annotations

import importlib.util
import json
import re
import signal
import subprocess
import tempfile
import os
import sys
import time
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / "profiles"
sys.path.insert(0, str(PROFILES))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from executor_common import ExecutorError  # noqa: E402  (sys.path 조작 뒤에야 import 가능)
import node_guard_fakes  # noqa: E402
import netns_sandbox  # noqa: E402
import db_operator_fakes  # noqa: E402


def load(name: str):
    path = PROFILES / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


compiler_spec = importlib.util.spec_from_file_location("ready_compile", ROOT / "compile-plan.py")
assert compiler_spec and compiler_spec.loader
compiler = importlib.util.module_from_spec(compiler_spec)
compiler_spec.loader.exec_module(compiler)

db_lock = load("db_lock_executor")
mock = load("mock_expectation_executor")
k8s_patch = load("k8s_patch_executor")
cache = load("cache_control_executor")
timeline = load("timeline_compose_executor")
db_ddl = load("db_ddl_executor")
kafka_control = load("kafka_control_executor")
host_stress = load("host_stress_executor")
app_control = load("app_control_executor")
db_workload = load("db_workload_executor")
db_account = load("db_account_executor")
db_cursor_reset = load("db_cursor_reset_executor")
k8s_dns = load("k8s_dns_executor")
k8s_quota = load("k8s_quota_executor")
k8s_scale = load("k8s_scale_executor")
k8s_job = load("k8s_job_executor")
k8s_image = load("k8s_image_executor")
host_firewall = load("host_firewall_executor")
host_image = load("host_image_executor")
host_job = load("host_job_executor")
db_instance_readonly = load("db_instance_readonly_executor")
db_config_row = load("db_config_row_executor")
db_row_delete = load("db_row_delete_executor")
db_row_update = load("db_row_update_executor")
network_fault = load("network_fault_executor")
db_operator_remote = load("db_operator_remote")
pg_lock_remote = load("pg_lock_remote")
own_body = db_operator_remote.without_library


class ReadyProfileExecutorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.profiles = json.loads((ROOT / "registry" / "profiles.json").read_text())["profiles"]

    def assert_contract(self, module, slug: str, profile_id: str) -> str:
        plan = compiler.compile_plan(slug)
        instance = next(row for row in plan["profile_instances"] if row["profile_id"] == profile_id)
        module.validate(plan["scenario"]["id"], instance["parameters"], self.profiles[profile_id])
        argv, stdin = module.build_invocation(plan, "cleanup")
        self.assertEqual(argv[0], "/usr/bin/bash" if profile_id != "db.lock" else "/usr/bin/ssh")
        script = stdin.decode()
        self.assertNotIn("shell=True", script)
        return script

    def test_every_approved_ladder_rung_passes_its_own_executor(self) -> None:
        # 승인 레지스트리에 등재된 사다리 단이 정작 실행기 검증에서 거부되면, 그 단은
        # 라이브에서만 드러나는 사문이 된다. `bind_level_parameters`가 단을 정확히
        # 묶어 주는데도 검증이 `scenario_parameters` 한 벌만 보면 기본값과 다른 단은
        # 전부 막힌다 — F15-T1이 768Mi 단에서 그렇게 막혔고(배치 #12), F15-R의
        # `episode-fault-240`도 같은 상태였다. 실행기 세 개가 같은 형태였다.
        #
        # 한 실행기만 고치고 형제로 안 옮기는 것이 이 저장소의 반복 결함이므로
        # (#9·#29·#30) 파일 단위가 아니라 사다리를 가진 프로파일 전체를 훑는다.
        # `.py`만 훑던 탓에 정작 검증을 가진 실행기 둘을 통째로 빠뜨렸다 — 레지스트리가
        # 셸 래퍼(`profiles/load-east-west.sh`)를 가리키는데 validate는 그 옆
        # `load_east_west_executor.py`에 있기 때문이다. 그 사각에서 F03-H의 target_url이
        # 레지스트리(`?delayMs=5000`)와 실행기(`?days=120`) 사이로 갈라진 채 살아남았고,
        # 라이브에서 주입과 **정리가 함께** 거부돼 전역 DIRTY가 됐다(2026-08-05 배치).
        # host.stress도 같은 사각에 있었다(사다리 6종).
        checked = 0
        for profile_id, profile in self.profiles.items():
            levels = profile.get("scenario_levels") or {}
            if not levels:
                continue
            stem = Path(profile["executor"]).stem
            if profile["executor"].endswith(".sh"):
                stem = f"{stem.replace('-', '_')}_executor"
            module = load(stem)
            # load.north_south는 별도 validate 없이 build_invocation 안에서 검사한다.
            if not hasattr(module, "validate"):
                continue
            for scenario_id, rungs in levels.items():
                for rung in rungs:
                    with self.subTest(profile=profile_id, scenario=scenario_id, rung=rung["level_id"]):
                        module.validate(scenario_id, rung["parameters"], profile)
                        checked += 1
        # 2026-08-20: pin 14종이 고정 승격되며 남은 사다리는 F10-P 와 비라이브 5종
        # (F02-H·F03-P·F07-P·F09-R·F21-P) = 18단. 고정 계약 쪽은 아래
        # test_every_live_fixed_contract_passes_its_own_executor 가 같은 질문을 던진다.
        self.assertGreater(checked, 15, "ladder sweep found almost nothing to check")

    def test_every_live_fixed_contract_passes_its_own_executor(self) -> None:
        # 2026-08-20 승격이 드러낸 사각: k8s_patch 의 validate 가 사다리(scenario_levels)
        # 만 보고 있어, F09-P·F12-H 가 고정 계약이 되는 순간 정작 승인된 고정 파라미터를
        # "사전 선언된 단이 아니다"로 거부했다 — 라이브였다면 주입과 정리가 함께 막혀
        # 전역 DIRTY 가 됐을 형태(F15-T1 768Mi 단, 배치 #12 와 동일). 사다리 스윕이
        # 사다리만 훑는 동안 고정 계약은 아무도 안 훑었으므로, 라이브 컨트롤러 전부의
        # 모든 프로파일 인스턴스를 그 실행기의 validate 에 통과시킨다 — 신뢰 디스패처가
        # apply·cleanup 에서 묻는 것과 같은 질문이다.
        controllers = json.loads((ROOT / "registry" / "controllers.json").read_text())["controllers"]
        catalog = {row["id"]: row for row in json.loads((ROOT / "catalog.json").read_text())["scenarios"]}
        checked = 0
        for scenario_id, controller in sorted(controllers.items()):
            plan = compiler.compile_plan(catalog[scenario_id]["slug"])
            self.assertTrue(plan["live_allowed"], scenario_id)
            for instance in plan["profile_instances"]:
                profile = self.profiles[instance["profile_id"]]
                stem = Path(profile["executor"]).stem
                if profile["executor"].endswith(".sh"):
                    stem = f"{stem.replace('-', '_')}_executor"
                module = load(stem)
                validate = getattr(module, "validate", None) or getattr(
                    module, "validate_parameters", None
                )
                self.assertIsNotNone(validate, f"{stem} has no validate")
                with self.subTest(scenario=scenario_id, profile=instance["profile_id"]):
                    validate(scenario_id, instance["parameters"], profile)
                    checked += 1
                if instance["profile_id"] == controller["profile"]["primary_ref"]:
                    expected = [
                        {"level_id": level["id"], "parameters": level["parameters"]}
                        for level in controller["profile"]["levels"]
                    ]
                    self.assertEqual(instance["approved_levels"], expected, scenario_id)
                    if controller["mode"] == "evaluation":
                        self.assertEqual(
                            instance["selected_level_id"], expected[0]["level_id"], scenario_id
                        )
        self.assertGreater(checked, 60, "fixed sweep found almost nothing to check")

    def test_db_lock_injects_inside_the_cluster_and_reclaims_its_client(self) -> None:
        # 품질 기준서 G6 / 부록 A: 주입 세션은 실 앱 세션과 구별되지 않아야 하므로
        # (1) 클러스터 안에서 접속하고 (2) 신원을 사칭하며 (3) 정리는 파드 삭제로 한다.
        # v4 C6b: 파드 안 프로세스는 psql 하나(러너가 attach 로 문장을 보냄), DB 파드 exec 는 없다.
        plan = compiler.compile_plan("f01-r-pg-lock-checkout")
        argv, stdin = db_lock.build_invocation(plan, "cleanup")
        self.assertEqual(argv[0], "/usr/bin/bash")
        self.assertIn(db_lock.APP_IDENTITY, argv)
        self.assertNotIn("nkia@192.168.122.206", argv)
        script = stdin.decode()
        self.assertIn("kind: Pod", script)
        self.assertIn("dbo_k delete pod", script)
        self.assertIn("lucida.io/db-client: session", script)
        self.assertIn('command: ["psql", ${args}]', script)
        for trace in ("pg_terminate_backend", "pg_sleep", "pkill", "SQL_PRE", 'sleep "\\$HOLD"', "sh -c"):
            self.assertNotIn(trace, own_body(script))
            self.assertNotIn(trace, script[script.index("pgl_pod() {"):script.index("pgl_name() {")])

    def test_oracle_lock_runs_from_the_runner_and_leaves_no_injector_trace(self) -> None:
        """v4 C6: Oracle 잠금은 DB 파드 밖에서, 운영 도구 모양의 네트워크 세션으로 잡는다.

        옛 방식(DB 파드 exec, `sqlplus / as sysdba`, `host sleep`)은 119 녹화에 SYS 차단
        세션(machine=testbed-oracle-0)과 DB 노드의 sqlplus, sleep 프로세스를 남겼다
        (2026-09-01 F01-P). F08-G, F15-G 의 oracle_lock 단계도 같은 함수를 쓴다.
        """
        oracle_bodies = {
            "F01-P": db_lock.build_invocation(compiler.compile_plan("f01-p-oracle-lock-cross-domain"), "run")[1].decode(),
        }
        multi = load("timeline_multi_injection_executor")
        oracle_bodies["timeline.multi"] = multi.ORCHESTRATOR.decode()
        for name, script in oracle_bodies.items():
            with self.subTest(name):
                script = own_body(script)
                oracle_part = script[script.index("# ---- Oracle row-lock client"):]
                oracle_part = oracle_part.split("# ---- pg_lock primitive")[0]
                for trace in ("exec ", "sqlplus", "as sysdba", "host sleep", "dbms_session", "/tmp/dba-maintenance",
                              "alter system kill"):
                    self.assertNotIn(trace, oracle_part)
                # 값은 env 로만 건너가고 키 값은 바인드 변수다(옛 heredoc 이 값을 비운 사고, 2026-08-04).
                self.assertIn('LOCK_TAG="$tag" LOCK_HOLD="$hold" READY_FILE="$dir/ready"', oracle_part)
                self.assertIn('export LOCK_SCHEMA="$1" LOCK_TABLE="$2" LOCK_KEYCOL="$3" LOCK_KEY="$4"', oracle_part)
                self.assertIn("where {keycol} = :k for update wait", oracle_part)
                self.assertIn("export CLIENT_PROGRAM=SQLcl", oracle_part)
                # 앱 계정은 Secret 에서 읽는다. 비밀번호 원문이 스크립트에 없다.
                self.assertIn("ORA_SECRET=oracle-secret", oracle_part)
                self.assertNotIn("banking1234", script)
        # 신원 상수는 시나리오를 가리키지 않는다(기준서 G6).
        remote = load("oracle_lock_remote")
        self.assertNotRegex(remote.CLIENT_DRIVER, r"(?i)python|oracledb|rca|F\d\d")
        for value in (remote.CLIENT_PROGRAM, remote.CLIENT_MACHINE, remote.CLIENT_OSUSER):
            self.assertNotRegex(value, r"(?i)rca|F\d\d|scenario|lock|test")
            # python-oracledb 가 받는 문자만(공백, 괄호는 DPY-3029 로 접속 전에 거절된다, 11-09 실측).
            self.assertRegex(value, r"^[A-Za-z0-9._@-]+$")

    # v4 C6b: the executors that changed the databases from inside the pod. F02-R still runs psql from tb-runner
    # (a network session already).
    C6B_EXECUTORS = {
        "db_account_executor": db_account, "db_ddl_executor": db_ddl, "db_row_delete_executor": db_row_delete,
        "db_table_readonly_executor": None, "db_cursor_reset_executor": db_cursor_reset,
        "app_control_executor": app_control, "db_lock_executor": db_lock,
        "db_config_row_executor": db_config_row, "db_instance_readonly_executor": db_instance_readonly,
        "db_row_update_executor": db_row_update,
    }
    C6B_EXCEPTIONS = {("db_ddl_executor", "F02-R")}

    def test_database_executors_neither_exec_into_the_pod_nor_name_the_scenario(self) -> None:
        """No body the DB executors send runs anything in the database pod or starts a client pod: no kubectl
        exec, no sqlplus / mysql / psql in a pod, no `as sysdba`, no pod manifest, no file under the pod's /tmp.
        The scenario id never reaches the body. Every contract of every converted executor is checked."""
        checked = 0
        for name, module in self.C6B_EXECUTORS.items():
            module = module or load(name)
            for scenario_id, params in module.CONTRACTS.items():
                if (name, scenario_id) in self.C6B_EXCEPTIONS:
                    continue
                plan = {"scenario": {"id": scenario_id},
                        "profile_instances": [{"profile_id": module.PROFILE_ID, "parameters": dict(params),
                                               "location": {}}]}
                for action in ("preflight", "run", "cleanup", "recovery"):
                    with self.subTest(executor=name, scenario=scenario_id, action=action):
                        argv, stdin = module.build_invocation(plan, action)
                        self.assertEqual(argv[:3], ["/usr/bin/bash", "-s", "--"])
                        script = stdin.decode()
                        # Comments may still name the old tools (they describe SQL*Plus behaviour the client keeps).
                        own = "\n".join(line for line in own_body(script).splitlines()
                                        if not line.lstrip().startswith("#"))
                        for trace in (" exec ", "exec -i", "sqlplus -s", "as sysdba", "kind: Pod", "-uroot",
                                      'cat >/tmp', "/tmp/", "kubectl run", "port-forward"):
                            self.assertNotIn(trace, own)
                        self.assertNotIn(scenario_id, script)
                        # The Oracle row lock (F01-P) has its own client from v4 C6 (oracle_lock_remote).
                        marker = ("Oracle row-lock client" if params.get("engine") == "oracle" and name == "db_lock_executor"
                                  else db_operator_remote.START_MARKER)
                        self.assertIn(marker, script)
                        checked += 1
        self.assertGreater(checked, 140)
        # The shared bash functions read from the API, except two execs, each starting one client alone in the
        # database pod (user decision Q8): the mysql client that the answer keys tell as root@localhost or
        # fooddelivery@localhost (mdb_pod_spawn), and psql on the local socket for the two changes that lock the
        # network out of the only administrator role (pdb_pod: F71-R's expiry, F71-H's narrowed host rule).
        library = db_operator_remote.bash_functions().decode()
        functions = library[library.index("\nDB_OPERATOR_SESSION_PY\n"):]
        writes = [line for line in functions.splitlines()
                  if "kubectl" in line and not line.lstrip().startswith("#")
                  and re.search(r"\b(exec|apply|delete|patch|create|attach|run)\b", line)]
        self.assertEqual(len(writes), 1, writes)
        self.assertIn('exec -i \\"\\$POD\\" -- $DBO_EXEC', writes[0])
        spawn = functions[functions.index("mdb_pod_spawn() {"):functions.index("\n}\n", functions.index("mdb_pod_spawn() {"))]
        self.assertIn("cmd=(mysql --defaults-extra-file=/dev/stdin", spawn)
        local = functions[functions.index("pdb_pod() {"):functions.index("\n}\n", functions.index("pdb_pod() {"))]
        execs = [line for line in functions.splitlines() if "dbo_k exec" in line and not line.lstrip().startswith("#")]
        self.assertEqual(execs, ['    | dbo_k exec -i "$DBO_POD" -- psql -X -q -At -v ON_ERROR_STOP=1 -U "$u" -d "$d" 2>"$e"; rc=$?',
                                 """    dbo_k exec "$DBO_POD" -- bash -c '"""])
        self.assertIn(execs[0], local)
        # The second is the failure paths' one exec (C6b review 3): only mdb_pod_reap, only through mdb_pod_clear, and
        # no body calls it from preflight or run.
        reap = functions[functions.index("mdb_pod_reap() {"):functions.index("\n}\n", functions.index("mdb_pod_reap() {"))]
        self.assertIn(execs[1], reap)
        self.assertEqual(sum("mdb_pod_reap " in line for line in functions.splitlines()
                             if not line.lstrip().startswith("#")), 1)
        self.assertIn('ended="$(mdb_pod_reap "$@")"', functions)
        # The forbidden-word checks above look at each body's own text; the shared function sets themselves are checked
        # here, so nothing SQL-level the bodies must not do can arrive through them (C6b test audit 다4).
        shared = (db_operator_remote.bash_functions() + pg_lock_remote.bash_functions()).decode().lower()
        for word in ("kill session", "identified by", "drop user", "account lock", "alter profile", "revoke", "grant ",
                     "set global", "max_queries_per_hour", "lucida_mon", "pg_terminate_backend", "pg_cancel_backend",
                     "pg_sleep", "pkill", "valid until", "nologin", "alter system", "alter role", "pg_ctl",
                     "postgresql.conf"):
            self.assertNotIn(word, shared, word)
        # And the row and DDL level words of the old per-body checks (F35-R, F54-R, F54-P, F71-H and the db.ddl bodies),
        # on the lines that run (C6b test re-audit). Left out, because the shared functions use them for their own
        # plumbing: "kill" (bash's kill for the runner's process groups and the lost client), "password" (the drivers'
        # argument and variable names), "delete " (kubectl delete of the client pods and export Jobs).
        code = "\n".join(line for line in shared.splitlines() if not line.lstrip().startswith("#"))
        for word in ("kill session", "identified by", "drop user", "account lock", "alter profile default",
                     "$profile cascade", "identified", "revoke", "grant ", "drop ", "password expire",
                     "max_queries_per_hour", "set global", "lucida_mon", "valid until", "nologin", "password '",
                     "pg_terminate_backend", "pg_cancel_backend", "alter system", "alter role", "pg_ctl",
                     "postgresql.conf", "drop table", "drop index", "truncate", "insert ", "update ", "rename ",
                     "delete from", "alter table"):
            self.assertNotIn(word, code, word)
        # The three words left out above, in the narrow forms that only a database change or a secret takes, so the
        # shared functions' own process and pod cleanup (bash kill, kubectl delete) and their password variables do not
        # meet them (C6b test re-audit, Q14 option (나)).
        for name, pattern in self.SHARED_FORBIDDEN_FORMS.items():
            self.assertIsNone(re.search(pattern, code), name)

    # Narrow forbidden forms for the shared function sets (lower-cased, lines that run).
    SHARED_FORBIDDEN_FORMS = {
        "sql delete": r"\bdelete\s+from\b",
        "sql kill": r"\bkill\s+(?:query|connection|session)\b|\bkill\s+\d|\balter\s+system\s+(?:kill|disconnect)\s+session\b",
        "password clause": r"\bidentified\s+(?:by|with)\b",
        "password literal": r"password\s*[=:]\s*['\"](?![$%'\"])",
        "password literal in a client's environment": (
            r"\b(?:pgpassword|mysql_pwd|mdb_password|odb_password)=(?![\"']?\$)['\"]?[^\s'\"]"),
        "password on a mysql command line": r"(?<![\w-])-p['\"]?[a-z0-9!@#^&*]",
    }

    def test_shared_forbidden_forms_catch_what_they_name_and_spare_the_cleanup(self) -> None:
        caught = {
            "sql delete": ['x="DELETE FROM banking.transfers WHERE id = 1"', "DELETE\tFROM t"],
            "sql kill": ["x=\"ALTER SYSTEM KILL SESSION '12,34'\"", 'x="KILL QUERY 12"', 'x="KILL 12"',
                         'x="KILL CONNECTION $id"', "alter system disconnect session '1,2' immediate"],
            "password clause": ['x="ALTER USER app IDENTIFIED BY secret"', "create user a identified with caching_sha2_password"],
            "password literal": ["password='hunter2'", 'conn(password = "hunter2")'],
            "password literal in a client's environment": ["PGPASSWORD=hunter2 psql -c 1", 'MYSQL_PWD="hunter2" mysql',
                                                           "odb_password=hunter2 python"],
            "password on a mysql command line": ["mysql -uroot -phunter2 -e 1", "mysql -u root -p'hunter2'"],
        }
        spared = ['kill -TERM -- "-$g" 2>/dev/null || true', 'kill "$attach" 2>/dev/null', "kill -0 \"$attach\"",
                  'kill -TERM $pids', 'dbo_k delete pod "$p" --now', 'dbo_k delete "$j" --cascade=foreground',
                  'password=_env("ODB_PASSWORD"),', 'ODB_PASSWORD="$(dbo_secret "$DBO_ORACLE_SECRET" key)"',
                  'MDB_PASSWORD="${!p}"', 'PGPASSWORD="$(dbo_secret s POSTGRES_PASSWORD)"',
                  "printf '[client]\\npassword=\"%s\"' \"$pw\"", 'mkdir -p -- "$dir"', "SELECT 1 FOR UPDATE"]
        for name, samples in caught.items():
            for sample in samples:
                self.assertIsNotNone(re.search(self.SHARED_FORBIDDEN_FORMS[name], sample.lower()), (name, sample))
        for sample in spared:
            for name, pattern in self.SHARED_FORBIDDEN_FORMS.items():
                self.assertIsNone(re.search(pattern, sample.lower()), (name, sample))
        # Only these two account changes use it.
        users = []
        for name, module in self.C6B_EXECUTORS.items():
            module = module or load(name)
            for scenario_id, params in module.CONTRACTS.items():
                if (name, scenario_id) in self.C6B_EXCEPTIONS:
                    continue
                plan = {"scenario": {"id": scenario_id},
                        "profile_instances": [{"profile_id": module.PROFILE_ID, "parameters": dict(params),
                                               "location": {}}]}
                if "pdb_pod " in own_body(module.build_invocation(plan, "run")[1].decode()):
                    users.append(scenario_id)
        self.assertEqual(sorted(users), ["F71-H", "F71-R"])

    # The four whose answer keys need the sessions to come from inside the database host (root@localhost,
    # fooddelivery@localhost): their export and migration are mysql clients in the database pod, nothing else
    # (user decision Q8: the node's process snapshot shows the client alone, no shell, awk, sleep or runner name).
    POD_CLIENT_SCENARIOS = ("F60-R", "F60-H", "F53-H", "F36-P")

    def test_localhost_scenarios_start_one_mysql_client_in_the_database_pod(self) -> None:
        for scenario_id in self.POD_CLIENT_SCENARIOS:
            with self.subTest(scenario_id):
                plan = {"scenario": {"id": scenario_id}, "profile_instances": [
                    {"profile_id": db_ddl.PROFILE_ID, "parameters": dict(db_ddl.CONTRACTS[scenario_id]), "location": {}}]}
                own = own_body(db_ddl.build_invocation(plan, "run")[1].decode())
                run = own[own.index(" run)"):own.index(" cleanup)")]
                spawns = re.findall(r"mdb_pod_spawn (\w+) ", run)
                if scenario_id == "F60-R":
                    self.assertEqual(spawns, ["root"])
                else:
                    self.assertEqual(spawns, ["app", "root"])
                    self.assertIn('"$export_log" paced', run)
                    self.assertIn("mdb_pod_pace", run)
                for helper in ("awk", "sleep \"$", "nohup", "sh -c", "kubectl", "mysqlsh", "deadline"):
                    self.assertNotIn(helper, run)
                # A client a lost runner left in the pod stops preflight and recovery (look only); cleanup ends it
                # (mdb_pod_clear, the one exec of the failure path) (C6b review 3, 4).
                pre = own_body(db_ddl.build_invocation(plan, "preflight")[1].decode())
                check = pre[pre.index("check() {"):pre.index("\n}\n", pre.index("check() {"))] if "check() {" in pre else ""
                preflight = pre[pre.index(" preflight)"):]
                preflight = preflight[:preflight.index(";;")]
                self.assertTrue("mdb_pod_alone" in preflight or ("check" in preflight and "mdb_pod_alone" in check))
                self.assertNotIn("mdb_pod_clear", preflight + check)
                body = own_body(db_ddl.build_invocation(plan, "cleanup")[1].decode())
                case = body[body.index(" cleanup)"):]
                self.assertIn("mdb_pod_clear", case[:case.index(";;")])
                # recovery is read-only verification (the executor contract; C6b review 4 하 3): it looks, never execs.
                body = own_body(db_ddl.build_invocation(plan, "recovery")[1].decode())
                case = body[body.index(" recovery)"):]
                case = case[:case.index(";;")]
                self.assertIn("mdb_pod_alone", case)
                self.assertNotIn("mdb_pod_clear", case)
                self.assertNotIn("mdb_pod_reap", own)

    def test_mysql_client_in_the_pod_is_exec_ed_alone_with_its_password_on_stdin(self) -> None:
        # The migration path: `kubectl exec -i POD -- mysql ...` with the option file on stdin. The fake records the
        # command the pod would start and what it read on stdin.
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = db_operator_fakes.install(tmp, kubectl_cases=(
                f"  *' exec -i testbed-mysql-0 -- '*) printf '%s\\n' \"$@\" >{tmp}/exec.argv; cat >{tmp}/exec.stdin ;;"))
            script = (b"#!/usr/bin/env bash\nset -euo pipefail\n" + db_operator_remote.bash_functions()
                      + b'db_target rca-testbed-food testbed-mysql-0\nmdb_env root\n'
                      + b'mdb_pod_spawn root fooddelivery "ALTER TABLE t ADD COLUMN c int" "$1/m.log"\n'
                      + b'for _ in $(seq 1 50); do [[ -s "$2/exec.stdin" ]] && break; sleep 0.1; done\n')
            done = subprocess.run(["bash", "-s", "--", str(tmp / "run"), str(tmp)], input=script, env=env,
                                  capture_output=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr.decode())
            argv = (tmp / "exec.argv").read_text().split("\n")
            command = argv[argv.index("--") + 1:-1]
            self.assertEqual(command, ["mysql", "--defaults-extra-file=/dev/stdin", "-uroot", "fooddelivery", "-e",
                                       "ALTER TABLE t ADD COLUMN c int"])
            self.assertEqual((tmp / "exec.stdin").read_text(),
                             '[client]\npassword="pw-root-from-secret"\n')
            for text in ((tmp / "exec.argv").read_text(), (tmp / "kubectl.log").read_text()):
                db_operator_fakes.assert_no_password(self, text)


    def test_background_clients_return_at_once_and_record_their_group(self) -> None:
        # C6b test audit 다5: run holds the runner's coordinator lock (30 s lease), so the migration, export and VACUUM
        # sessions start in the background and run returns without waiting for them. Here each client blocks for
        # 30 s; mdb_pod_spawn and pdb_spawn must come back within a few seconds and leave its group recorded.
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = db_operator_fakes.install(tmp, psql="import time\ntime.sleep(30)\n", kubectl_cases=(
                "  *' exec -i testbed-mysql-0 -- '*) cat >/dev/null; sleep 30 ;;"))
            script = (b"#!/usr/bin/env bash\nset -euo pipefail\n" + db_operator_remote.bash_functions()
                      + b'db_target rca-testbed-food testbed-mysql-0\n'
                      + b'mdb_pod_spawn root fooddelivery "ALTER TABLE t ADD COLUMN c int" "$1/m.log"\n'
                      + b'db_target rca-testbed-commerce testbed-postgres-0\n'
                      + b'pdb_spawn "VACUUM (FULL) a.b" "$1/v.log" psql\n'
                      + b'echo spawned\n')
            started = time.monotonic()
            done = subprocess.run(["bash", "-s", "--", str(tmp / "run")], input=script, env=env, capture_output=True,
                                  timeout=25)
            took = time.monotonic() - started
            try:
                self.assertEqual(done.returncode, 0, done.stderr.decode())
                self.assertIn(b"spawned", done.stdout)
                self.assertLess(took, 8.0)
                boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
                for log in ("m.log", "v.log"):
                    pgid, start, recorded_boot = (tmp / "run" / f"{log}.pgid").read_text().split()
                    self.assertEqual(recorded_boot, boot)
                    self.assertTrue(start.isdigit())
                    # The client is still running in its own group.
                    self.assertTrue(any(Path(f"/proc/{d.name}/stat").exists() and
                                        Path(f"/proc/{d.name}/stat").read_text().rsplit(")", 1)[1].split()[2] == pgid
                                        for d in Path("/proc").iterdir() if d.name.isdigit()), log)
            finally:
                subprocess.run(["bash", "-s", "--", str(tmp / "run")], env=env, capture_output=True, timeout=30,
                               input=b"#!/usr/bin/env bash\nset -euo pipefail\n" + db_operator_remote.bash_functions()
                               + b'dbo_reap "$1"\n')
        library = db_operator_remote.bash_functions().decode()
        for fn in ("mdb_pod_spawn() {", "pdb_spawn() {"):
            body = library[library.index(fn):library.index("\n}\n", library.index(fn))]
            self.assertIn("setsid nohup", body)
            self.assertRegex(body, r"2>&1 &\n")
            self.assertRegex(body, r'dbo_pgid_record "\$!"')

    def test_mysql_clients_in_the_pod_are_mysql_from_their_first_moment(self) -> None:
        # C6b review 3 상 1, criterion ①: the command the pod starts is `mysql` itself for the export (pod-exec) and the
        # migration (kubectl exec), never a launcher (mysqlsh), a shell or anything else, so no process table sample
        # can catch anything but the client.
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = db_operator_fakes.install(tmp, kubectl_cases=(
                "  *' config view --raw --minify -o json'*) printf '{}' ;;\n"
                f"  *' exec -i testbed-mysql-0 -- '*) printf '%s\\0' \"$@\" >{tmp}/exec.argv; cat >/dev/null ;;"))
            recorder = tmp / "bin" / "python-recorder"
            recorder.write_text("#!/usr/bin/env bash\n"
                                'if [[ "$1" == -c && "$2" == "import "* ]]; then exit 0; fi\n'
                                f'printf "%s\\0" "${{@:3}}" >{tmp}/pod-exec.argv; cat >{tmp}/pod-exec.stdin\n')
            recorder.chmod(0o755)
            env["DB_OPERATOR_PYTHON"] = str(recorder)
            script = (b"#!/usr/bin/env bash\nset -euo pipefail\n" + db_operator_remote.bash_functions()
                      + b'db_target rca-testbed-food testbed-mysql-0\n'
                      + b'mdb_pod_spawn app fooddelivery "SELECT id, eta FROM dispatches" "$1/e.log" paced\n'
                      + b'mdb_pod_spawn root fooddelivery "ALTER TABLE dispatches ADD COLUMN c int" "$1/m.log"\n'
                      + b'for _ in $(seq 1 50); do [[ -e "$2/pod-exec.stdin" && -s "$2/exec.argv" ]] && break; sleep 0.1; done\n'
                      + b'sleep 0.3\n')
            done = subprocess.run(["bash", "-s", "--", str(tmp / "run"), str(tmp)], input=script, env=env,
                                  capture_output=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr.decode())
            exported = (tmp / "pod-exec.argv").read_text().split("\0")[:-1]
            migrated = (tmp / "exec.argv").read_text().split("\0")[:-1]
            commands = {"export": exported[exported.index("--") + 1:], "migration": migrated[migrated.index("--") + 1:]}
            self.assertEqual(commands["export"], ["mysql", "--defaults-extra-file=/dev/stdin", "-ufooddelivery", "--quick",
                                                  "-N", "-B", "fooddelivery", "-e", "SELECT id, eta FROM dispatches"])
            self.assertEqual(commands["migration"], ["mysql", "--defaults-extra-file=/dev/stdin", "-uroot", "fooddelivery",
                                                     "-e", "ALTER TABLE dispatches ADD COLUMN c int"])
            for name, command in commands.items():
                self.assertEqual(command[0], "mysql", name)
                for word in ("mysqlsh", "sh", "bash", "python", "alarm", "execvp"):
                    self.assertFalse(any(word == item or f"{word}(" in item for item in command), (name, word))
            self.assertEqual((tmp / "pod-exec.stdin").read_text(), '[client]\npassword="pw-food-from-secret"\n')
            db_operator_fakes.assert_no_password(self, (tmp / "pod-exec.argv").read_text(), (tmp / "exec.argv").read_text())
        library = db_operator_remote.bash_functions().decode()
        self.assertNotIn("mysqlsh", library)
        self.assertNotIn("signal.alarm", library)

    def test_lost_mysql_client_is_ended_by_one_exec_of_bash_builtins(self) -> None:
        # mdb_pod_reap, criterion ④'s one exec: bash with builtins only reads /proc/[0-9]*/cmdline and TERMs the
        # processes whose argv0 is mysql with the option file on stdin and `-u<account>`. Run here against this host's
        # /proc (the fake kubectl runs the exec's command locally) with stand-ins whose argv0 is mysql: only the one
        # for the account is ended. The account name is unique to this run so nothing else on the host matches.
        account = f"reaptest{os.getpid()}"
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = db_operator_fakes.install(tmp, kubectl_cases=(
                "  *' exec testbed-mysql-0 -- bash -c '*) for ((i = 1; i <= $#; i++)); do [[ ${!i} == -- ]] && break; done; "
                "shift \"$i\"; exec \"$@\" ;;"))
            stand_in = "import time; time.sleep(120)"
            def start(*args: str) -> subprocess.Popen:
                return subprocess.Popen(["mysql", "-c", stand_in, *args], executable=sys.executable)
            target = start("--defaults-extra-file=/dev/stdin", f"-u{account}", "fooddelivery", "-e", "SELECT 1")
            other_account = start("--defaults-extra-file=/dev/stdin", f"-u{account}x", "fooddelivery")
            not_ours = start(f"-u{account}", "fooddelivery")
            try:
                time.sleep(0.5)
                script = (b"#!/usr/bin/env bash\nset -euo pipefail\n" + db_operator_remote.bash_functions()
                          + b'db_target rca-testbed-food testbed-mysql-0\nmdb_pod_reap "$1"\n')
                done = subprocess.run(["bash", "-s", "--", account], input=script, env=env, capture_output=True,
                                      timeout=60)
                self.assertEqual(done.returncode, 0, done.stderr.decode())
                self.assertEqual(done.stdout.decode().split(), [str(target.pid)])
                self.assertEqual(target.wait(timeout=10), -signal.SIGTERM)
                self.assertIsNone(other_account.poll())
                self.assertIsNone(not_ours.poll())
            finally:
                for proc in (target, other_account, not_ours):
                    proc.kill()
                    proc.wait()
        reap = db_operator_remote.bash_functions().decode()
        reap = reap[reap.index("mdb_pod_reap() {"):reap.index("\n}\n", reap.index("mdb_pod_reap() {"))]
        self.assertIn('[[ $p == 1 ]] && continue', reap)
        # Builtins only: no program but bash itself is started in the pod.
        inner = reap[reap.index("bash -c '") + 9:reap.index("' _ ")]
        for program in ("ps ", "pgrep", "pkill", "grep", "awk", "sed", "cat ", "/bin/kill", "xargs"):
            self.assertNotIn(program, inner)

    def test_postgres_export_runs_as_a_batch_job_on_its_domain_node(self) -> None:
        # C6b review 2 상 1: the answer keys of F66-R, F66-P, F67-H, F73-R tell an export job or a data job, so the export
        # is a Job in the database's namespace (a pod address), on the node the domain runs on, ending itself at its
        # deadline (U8) and deleted after its TTL. A privileged init container shrinks only that pod's TCP receive
        # buffer, so the reader's pace holds the server back.
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = db_operator_fakes.install(tmp)
            sql = "COPY (SELECT id, topic FROM inventory_schema.outbox_events) TO STDOUT"
            script = (b"#!/usr/bin/env bash\nset -euo pipefail\n" + db_operator_remote.bash_functions()
                      + pg_lock_remote.bash_functions()
                      + b'db_target rca-testbed-commerce testbed-postgres-0\n'
                      + b'n="$(pdb_export_name)"; [[ "$n" =~ ^commerce-data-export-[0-9a-f]{8}$ ]] || exit 7\n'
                      + f'pdb_export_job "$n" "{sql}" 172 780 "PostgreSQL JDBC Driver"\n'.encode())
            done = subprocess.run(["bash", "-s"], input=script, env=env, capture_output=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr.decode())
            job = json.loads((tmp / "applied.yaml").read_text())
            self.assertEqual((job["apiVersion"], job["kind"]), ("batch/v1", "Job"))
            self.assertEqual(job["metadata"]["labels"], {"app.kubernetes.io/component": "data-export"})
            self.assertEqual((job["spec"]["backoffLimit"], job["spec"]["activeDeadlineSeconds"],
                              job["spec"]["ttlSecondsAfterFinished"]), (0, 780, 300))
            pod = job["spec"]["template"]["spec"]
            self.assertEqual(job["spec"]["template"]["metadata"]["labels"], job["metadata"]["labels"])
            # The fake database pod has no nodeSelector, so its node by hostname.
            self.assertEqual(pod["nodeSelector"], {"kubernetes.io/hostname": "tb-w9"})
            self.assertEqual(pod["initContainers"], [{
                "name": "init", "image": "postgres:16-alpine",
                "command": ["sysctl", "-w", "net.ipv4.tcp_rmem=4096 8192 8192"],
                "securityContext": {"privileged": True}}])
            (container,) = pod["containers"]
            self.assertEqual(container["command"],
                             ["sh", "-c", 'psql -X -q -c "$SQL" | awk -v r="$RATE" "$PACE"'])
            values = {e["name"]: e.get("value", e.get("valueFrom")) for e in container["env"]}
            self.assertEqual(values["PGHOST"], "testbed-postgres")
            self.assertEqual(values["PGAPPNAME"], "PostgreSQL JDBC Driver")
            self.assertEqual((values["SQL"], values["RATE"]), (sql, "172"))
            self.assertEqual(values["PACE"], 'NR % r == 0 { system("sleep 1") }')
            self.assertEqual(values["PGPASSWORD"], {"secretKeyRef": {"name": "postgres-secret", "key": "POSTGRES_PASSWORD"}})
            db_operator_fakes.assert_no_password(self, (tmp / "applied.yaml").read_text(), done.stderr)
            for word in ("runner", "scenario", "rca", "F66", "lucida"):
                self.assertNotIn(word, json.dumps(pod["containers"]) + job["metadata"]["labels"]["app.kubernetes.io/component"])

    def test_leftover_client_in_the_mysql_pod_stops_preflight_until_it_is_gone(self) -> None:
        # mdb_pod_alone reads the pod's process count from the kubelet summary of its node (no exec): mysqld alone is 1,
        # a client an earlier run lost makes it 2 until its deadline ends it.
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            (tmp / "count").write_text("2")
            env = db_operator_fakes.install(tmp, kubectl_cases=(
                f"  *'get --raw /api/v1/nodes/tb-w2/proxy/stats/summary'*) printf '{{\"pods\":[{{\"podRef\":"
                f"{{\"namespace\":\"rca-testbed-food\",\"name\":\"testbed-mysql-0\"}},\"process_stats\":"
                f"{{\"process_count\":%s}}}}]}}' \"$(cat {tmp}/count)\" ;;"))
            script = (b"#!/usr/bin/env bash\nset -euo pipefail\n" + db_operator_remote.bash_functions()
                      + b'db_target rca-testbed-food testbed-mysql-0\nsleep() { :; }\nmdb_pod_alone\n')
            left = subprocess.run(["bash", "-s"], input=script, env=env, capture_output=True, timeout=60)
            self.assertNotEqual(left.returncode, 0)
            self.assertIn(b"1 process(es) besides the database server in testbed-mysql-0", left.stderr)
            (tmp / "count").write_text("1")
            alone = subprocess.run(["bash", "-s"], input=script, env=env, capture_output=True, timeout=60)
            self.assertEqual(alone.returncode, 0, alone.stderr.decode())
            db_operator_fakes.assert_reads_only(self, tmp)

    def test_lost_mysql_client_is_ended_by_cleanup_only_after_the_kubelet_refresh(self) -> None:
        # mdb_pod_clear (cleanup only): the kubelet's count drops up to about 20 s after a client ends (C6b review 4 상 1,
        # review 5: 19.87 s measured), so the first look waits six looks (25 s) before deciding; a client that just
        # ended in a normal run is never exec'd for. Only a client still counted after that is ended (one exec per
        # account).
        def run(counts: list[int]) -> tuple[subprocess.CompletedProcess, list[str]]:
            with tempfile.TemporaryDirectory() as raw:
                tmp = Path(raw)
                (tmp / "counts").write_text("\n".join(str(c) for c in counts) + "\n")
                env = db_operator_fakes.install(tmp, kubectl_cases=(
                    # Each summary read takes the next count; the last one stays.
                    f"  *'get --raw /api/v1/nodes/tb-w2/proxy/stats/summary'*) c=$(head -n 1 {tmp}/counts); "
                    f"[[ $(wc -l <{tmp}/counts) -gt 1 ]] && sed -i 1d {tmp}/counts; "
                    f"printf '{{\"pods\":[{{\"podRef\":{{\"namespace\":\"rca-testbed-food\",\"name\":\"testbed-mysql-0\"}},"
                    f"\"process_stats\":{{\"process_count\":%s}}}}]}}' \"$c\" ;;\n"
                    f"  *' exec testbed-mysql-0 -- bash -c '*) [[ $(head -n 1 {tmp}/counts) == 1 ]] || "
                    f"{{ echo 1 >{tmp}/counts; echo 4242; }} ;;"))
                script = (b"#!/usr/bin/env bash\nset -euo pipefail\n" + db_operator_remote.bash_functions()
                          + b'db_target rca-testbed-food testbed-mysql-0\nsleep() { :; }\nmdb_pod_clear fooddelivery root\n')
                done = subprocess.run(["bash", "-s"], input=script, env=env, capture_output=True, timeout=60)
                return done, [line for line in db_operator_fakes.kubectl_calls(tmp) if " exec " in line]

        # The kubelet still counts a client that just ended for five looks (20 s): no exec.
        done, execs = run([2, 2, 2, 2, 2, 1])
        self.assertEqual(done.returncode, 0, done.stderr.decode())
        self.assertEqual(execs, [])
        # Alone from the start: no exec.
        done, execs = run([1])
        self.assertEqual((done.returncode, execs), (0, []))
        # Still there after the refresh: ended, one exec per account.
        done, execs = run([2])
        self.assertEqual(done.returncode, 0, done.stderr.decode())
        self.assertIn(b"ended a mysql client left in testbed-mysql-0 (pid 4242)", done.stderr)
        self.assertEqual(len(execs), 2)
        clear = db_operator_remote.bash_functions().decode()
        clear = clear[clear.index("mdb_pod_clear() {"):clear.index("\n}\n", clear.index("mdb_pod_clear() {"))]
        self.assertIn("mdb_pod_alone 6 2>/dev/null && return 0", clear)

    # C6b review 4 상 2: the run directory outlives a runner container (host volume) and a new container counts pids
    # from 1 again. A recorded process group that is gone, or whose number now belongs to another process, must neither
    # fail cleanup under set -e nor be signalled. One case per body that keeps clients on the runner.
    STALE_GROUP_BODIES = (
        ("F60-H", "db-ddl-queue-fooddelivery-dispatches", "table-export.log"),
        ("F60-R", "db-ddl-fk-fooddelivery-dispatch_events", "schema-migration.log"),
        ("F66-R", "db-ddl-queue-user_schema-users", "schema-migration.log"),
        ("F67-R", "db-ddl-maintenance-rca-testbed-commerce", "table-maintenance-x.log"),
    )

    def test_cleanup_ignores_process_groups_a_restarted_runner_no_longer_has(self) -> None:
        mysql = """import os, re
sql = os.environ["SQL"]
if re.search(r"COUNT\\(\\*\\) >= ", sql): print(1)
elif "COUNT(*)" in sql: print(0)
"""
        psql = """import sys
args = sys.argv[1:]; sql = args[args.index("-c") + 1] if "-c" in args else ""
if "count(*) >=" in sql: print("t")
elif "count(*)" in sql: print(0)
"""
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        for scenario_id, run_name, log in self.STALE_GROUP_BODIES:
            with self.subTest(scenario_id), tempfile.TemporaryDirectory() as raw:
                tmp = Path(raw)
                env = db_operator_fakes.install(tmp, mysql=mysql, psql=psql)
                # An unrelated live group (its own session, so its pid is its group id) whose number the old file names.
                bystander = subprocess.Popen(["sleep", "120"], start_new_session=True)
                try:
                    run_dir = tmp / "state" / run_name
                    (run_dir / f"{log}.pace").mkdir(parents=True)
                    # Written before the start-time format, a bare number.
                    (run_dir / "old.log.pgid").write_text(f"{bystander.pid}\n")
                    # This format, the number reused by another process (another start time).
                    (run_dir / f"{log}.pgid").write_text(f"{bystander.pid} 1 {boot}\n")
                    # A group that no longer exists at all.
                    (run_dir / "gone.log.pgid").write_text("4194000 7 " + boot + "\n")
                    plan = {"scenario": {"id": scenario_id}, "profile_instances": [
                        {"profile_id": db_ddl.PROFILE_ID, "parameters": dict(db_ddl.CONTRACTS[scenario_id]),
                         "location": {}}]}
                    argv, stdin = db_ddl.build_invocation(plan, "cleanup")
                    self.assertEqual(argv[:2], ["/usr/bin/bash", "-s"])
                    done = subprocess.run(argv, input=stdin, env=env, capture_output=True, timeout=180)
                    self.assertEqual(done.returncode, 0, done.stderr.decode())
                    self.assertFalse(run_dir.exists())
                    self.assertIsNone(bystander.poll(), "an unrelated process group was signalled")
                finally:
                    bystander.kill()
                    bystander.wait()

    def test_recorded_process_group_is_ours_only_while_its_leader_is_the_same_process(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = db_operator_fakes.install(tmp)
            script = (b"#!/usr/bin/env bash\nset -euo pipefail\n" + db_operator_remote.bash_functions()
                      + b'mkdir -p "$1/run"\n'
                      + b'setsid nohup sleep 120 </dev/null >/dev/null 2>&1 &\n'
                      + b'dbo_pgid_record "$!" "$1/run/c.log.pgid"\n'
                      + b'echo "ours=$(dbo_pgid_ours "$1/run/c.log.pgid")"\n'
                      + b'read -r g start boot <"$1/run/c.log.pgid"; echo "recorded=$g"\n'
                      + b'echo "$g $((start + 1)) $boot" >"$1/run/d.log.pgid"; echo "other=$(dbo_pgid_ours "$1/run/d.log.pgid")"\n'
                      + b'echo "$g" >"$1/run/e.log.pgid"; echo "bare=$(dbo_pgid_ours "$1/run/e.log.pgid")"\n'
                      + b'rm "$1/run/d.log.pgid" "$1/run/e.log.pgid"\n'
                      + b'dbo_reap "$1/run"\n'
                      + b'for _ in $(seq 1 20); do dbo_group_alive "$g" || break; sleep 0.1; done\n'
                      + b'dbo_group_alive "$g" && echo alive || echo ended\n')
            done = subprocess.run(["bash", "-s", "--", str(tmp)], input=script, env=env, capture_output=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr.decode())
            out = dict(line.split("=", 1) for line in done.stdout.decode().splitlines() if "=" in line)
            self.assertEqual(out["ours"], out["recorded"])
            self.assertEqual(out["other"], "")
            self.assertEqual(out["bare"], "")
            self.assertIn("ended", done.stdout.decode())
            self.assertEqual(done.stderr, b"")

    def test_proc_scans_stay_quiet_while_processes_come_and_go(self) -> None:
        # C6b review 4 하 4: a process that ends between the /proc listing and the read must not print the
        # redirection's own error (the redirection is inside braces whose stderr goes to /dev/null).
        script = (b"#!/usr/bin/env bash\nset -euo pipefail\n" + db_operator_remote.bash_functions()
                  + b'( for _ in $(seq 1 400); do /bin/true & done; wait ) &\n'
                  + b'for _ in $(seq 1 30); do dbo_group_alive 4194001 || true; done\nwait\n')
        with tempfile.TemporaryDirectory() as raw:
            env = db_operator_fakes.install(Path(raw))
            done = subprocess.run(["bash", "-s"], input=script, env=env, capture_output=True, timeout=120)
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stderr, b"")
        library = db_operator_remote.bash_functions().decode()
        for line in library.splitlines():
            if "/proc/" in line and "<" in line and "2>/dev/null" in line:
                self.assertRegex(line, r"\{ [^}]*<[^}]*; \} 2>/dev/null", line)

    def _operator_run(self, module, scenario_id: str, action: str, env: dict[str, str],
                      slug: str | None = None) -> subprocess.CompletedProcess:
        if slug:
            plan = compiler.compile_plan(slug)
        else:
            plan = {"scenario": {"id": scenario_id},
                    "profile_instances": [{"profile_id": module.PROFILE_ID,
                                           "parameters": dict(module.CONTRACTS[scenario_id]), "location": {}}]}
        argv, stdin = module.build_invocation(plan, action)
        return subprocess.run(argv, input=stdin, env=env, capture_output=True, timeout=120, check=False)

    def test_oracle_account_lock_goes_through_a_dba_session_on_the_listener(self) -> None:
        # F35-R end to end against a fake Oracle behind the operator client: the account flips LOCKED and back,
        # the session is SYSTEM through the listener the API names, kubectl only reads, no password in argv.
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            (tmp / "status").write_text("OPEN")
            fake = f"""import sys
from pathlib import Path
status = Path({str(tmp / "status")!r})
sql = sys.stdin.read()
if "account lock;" in sql: status.write_text("LOCKED")
elif "account unlock;" in sql: status.write_text("OPEN")
elif "select account_status from dba_users" in sql: print(status.read_text())
else: sys.exit(7)
"""
            env = db_operator_fakes.install(tmp, oracle=fake)
            slug = "f35-r-banking-oracle-app-account-locked"
            self.assertEqual(self._operator_run(db_account, "F35-R", "preflight", env, slug).returncode, 0)
            run = self._operator_run(db_account, "F35-R", "run", env, slug)
            self.assertEqual(run.returncode, 0, run.stderr.decode())
            self.assertEqual((tmp / "status").read_text(), "LOCKED")
            self.assertNotEqual(self._operator_run(db_account, "F35-R", "preflight", env, slug).returncode, 0)
            self.assertEqual(self._operator_run(db_account, "F35-R", "cleanup", env, slug).returncode, 0)
            self.assertEqual((tmp / "status").read_text(), "OPEN")
            calls = (tmp / "client.log").read_text().splitlines()
            self.assertTrue(calls)
            for line in calls:
                # Banking's database: through tb-w3, not tb-w1 where commerce runs (C6b review 2 중 2).
                self.assertTrue(line.startswith("oracle SYSTEM 192.168.122.14:30999/FREEPDB1 "), line)
            db_operator_fakes.assert_reads_only(self, tmp)
            db_operator_fakes.assert_no_password(self, (tmp / "client.log").read_text(),
                                                 (tmp / "kubectl.log").read_text(), run.stderr)

    def test_mysql_write_cap_goes_through_a_root_session_on_the_listener(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            (tmp / "cap").write_text("0")
            fake = f"""import os, re, sys
from pathlib import Path
cap = Path({str(tmp / "cap")!r})
sql = os.environ["SQL"]
m = re.search(r"WITH MAX_UPDATES_PER_HOUR (\\d+);", sql)
if m: cap.write_text(m.group(1))
elif "FROM mysql.user" in sql: print(cap.read_text() + "|0|0|0")
else: sys.exit(7)
"""
            env = db_operator_fakes.install(tmp, mysql=fake)
            slug = "f54-p-food-mysql-app-account-hourly-write-cap-breaks-orders"
            run = self._operator_run(db_account, "F54-P", "run", env, slug)
            self.assertEqual(run.returncode, 0, run.stderr.decode())
            self.assertEqual((tmp / "cap").read_text(), "500")
            self.assertEqual(self._operator_run(db_account, "F54-P", "cleanup", env, slug).returncode, 0)
            self.assertEqual((tmp / "cap").read_text(), "0")
            for line in (tmp / "client.log").read_text().splitlines():
                self.assertTrue(line.startswith("mysql root 192.168.122.184:30999 "), line)
            db_operator_fakes.assert_reads_only(self, tmp)
            db_operator_fakes.assert_no_password(self, (tmp / "client.log").read_text(), run.stderr)

    def test_postgres_host_rule_is_rewritten_by_the_server_through_psql_alone_in_the_pod(self) -> None:
        # F71-H against a fake PostgreSQL: the file travels base64 through pg_read_binary_file and lo_export, the
        # narrowed and restored contents are exact, and the reload is confirmed by a later configuration load time.
        # Every statement goes through `kubectl exec -i POD -- psql ...` with the statement on stdin (pdb_pod): the
        # pod starts psql alone, as the local socket's administrator, with no password anywhere.
        original = ("# TYPE DATABASE USER ADDRESS METHOD\nlocal all all trust\n"
                    "host all all 127.0.0.1/32 trust\nhost all all all scram-sha-256\n")
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            (tmp / "hba").write_text(original)
            (tmp / "loaded").write_text("1")
            fake = f"""import base64, os, re, sys
from pathlib import Path
hba = Path({str(tmp / "hba")!r}); loaded = Path({str(tmp / "loaded")!r})
args = sys.argv[1:]
stdin = sys.stdin.read().split("\\n", 1)
assert stdin[0] == "SET pg_stat_statements.track = none;", stdin[0]
sql = stdin[1].rstrip("\\n")
lines = [l.split() for l in hba.read_text().splitlines() if l.strip() and not l.startswith("#")]
nets = {{"10.244.3.1/32", "192.168.122.0/24", "192.168.213.0/24", "192.168.230.0/24"}}
if "FROM pg_hba_file_rules" in sql:
    catch = sum(1 for l in lines if l[0] == "host" and l[3] == "all")
    narrowed = sum(1 for l in lines if l[0] == "host" and l[3] in nets)
    print(f"{{catch}}|{{narrowed}}|0")
elif "pg_read_binary_file" in sql:
    print(base64.encodebytes(hba.read_bytes()).decode(), end="")
elif "lo_export" in sql:
    hba.write_bytes(base64.b64decode(re.search(r"decode\\('([^']*)', 'base64'\\)", sql).group(1)))
elif sql == "SELECT pg_reload_conf()":
    loaded.write_text(str(int(loaded.read_text()) + 1)); print("t")
elif sql == "SELECT pg_conf_load_time()":
    print(loaded.read_text())
elif "pg_conf_load_time() >" in sql:
    print("t" if int(loaded.read_text()) > int(re.search(r"'(\\d+)'", sql).group(1)) else "f")
elif "FROM pg_stat_activity WHERE usename" in sql:
    print("t")
else:
    print("unexpected " + sql, file=sys.stderr); sys.exit(3)
"""
            (tmp / "pod_psql.py").write_text(fake)
            env = db_operator_fakes.install(tmp, kubectl_cases=(
                f"  *' exec -i testbed-postgres-0 -- psql '*) printf '%s\\n' \"$*\" >>{tmp}/exec.argv; "
                f"exec {sys.executable} {tmp}/pod_psql.py ;;"))
            slug = "f71-h-commerce-postgres-access-rule-omits-app-pods"
            preflight = self._operator_run(db_account, "F71-H", "preflight", env, slug)
            self.assertEqual(preflight.returncode, 0, preflight.stderr.decode())
            run = self._operator_run(db_account, "F71-H", "run", env, slug)
            self.assertEqual(run.returncode, 0, run.stderr.decode())
            narrowed = (tmp / "hba").read_text()
            self.assertNotIn("host all all all scram-sha-256", narrowed)
            self.assertIn("host all all 10.244.3.1/32 scram-sha-256\nhost all all 192.168.122.0/24", narrowed)
            self.assertTrue(narrowed.startswith("# TYPE DATABASE USER ADDRESS METHOD\nlocal all all trust\n"))
            self.assertEqual((tmp / "loaded").read_text(), "2")
            cleanup = self._operator_run(db_account, "F71-H", "cleanup", env, slug)
            self.assertEqual(cleanup.returncode, 0, cleanup.stderr.decode())
            self.assertEqual((tmp / "hba").read_text(), original)
            self.assertEqual(self._operator_run(db_account, "F71-H", "recovery", env, slug).returncode, 0)
            # Every exec started the same psql, with the Secret's user and database names and nothing else.
            execs = (tmp / "exec.argv").read_text().splitlines()
            self.assertGreater(len(execs), 10)
            for line in execs:
                self.assertTrue(line.endswith(" exec -i testbed-postgres-0 -- psql -X -q -At -v ON_ERROR_STOP=1 "
                                              "-U commerce -d commerce"), line)
            others = [line for line in db_operator_fakes.kubectl_calls(tmp) if " exec " not in line]
            self.assertTrue(others)
            for line in others:
                self.assertIn(" get ", line)
            db_operator_fakes.assert_no_password(self, (tmp / "kubectl.log").read_text(),
                                                 (tmp / "exec.argv").read_text())

    def test_postgres_password_expiry_is_set_in_the_pod_and_checked_from_the_network(self) -> None:
        # F71-R against a fake PostgreSQL: the expiry and the renewal go through psql alone in the pod (pdb_pod, the
        # local socket the expiry cannot lock out); the login check is the runner's network session through the
        # entry node, refused only for the expired password. No password on any command line.
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            (tmp / "until").write_text("NONE")
            pod = f"""import re, sys
from pathlib import Path
until = Path({str(tmp / "until")!r})
head, sql = sys.stdin.read().split("\\n", 1)
assert head == "SET pg_stat_statements.track = none;", head
sql = sql.rstrip("\\n")
if "FROM pg_roles r WHERE r.rolname = 'commerce'" in sql:
    print(until.read_text() + "|true|scram-sha-256")
elif "to_char(clock_timestamp()" in sql:
    print("2026-11-10 01:02:03.000004+00")
elif sql == 'ALTER ROLE "commerce" VALID UNTIL ' + chr(39) + 'infinity' + chr(39):
    until.write_text("NONE")
elif sql == 'ALTER ROLE "commerce" VALID UNTIL ' + chr(39) + '2026-11-10 01:02:03.000004+00' + chr(39):
    until.write_text("EXPIRED")
else:
    print("unexpected " + sql, file=sys.stderr); sys.exit(3)
"""
            net = f"""import os, sys
from pathlib import Path
with open({str(tmp / "net.log")!r}, "a") as log:
    log.write(" ".join([os.environ["PGHOST"], os.environ["PGPORT"], os.environ["PGUSER"]] + sys.argv[1:]) + "\\n")
assert os.environ["PGPASSWORD"] == "pw-pg-from-secret"
if Path({str(tmp / "until")!r}).read_text() == "EXPIRED":
    print('psql: error: FATAL:  password authentication failed for user "commerce"', file=sys.stderr); sys.exit(2)
print(1)
"""
            (tmp / "pod_psql.py").write_text(pod)
            env = db_operator_fakes.install(tmp, psql=net, kubectl_cases=(
                f"  *' exec -i testbed-postgres-0 -- psql '*) printf '%s\\n' \"$*\" >>{tmp}/exec.argv; "
                f"exec {sys.executable} {tmp}/pod_psql.py ;;"))
            slug = "f71-r-commerce-postgres-app-account-password-expiry"
            for action, until in (("preflight", "NONE"), ("run", "EXPIRED"), ("cleanup", "NONE"), ("recovery", "NONE")):
                done = self._operator_run(db_account, "F71-R", action, env, slug)
                self.assertEqual(done.returncode, 0, (action, done.stderr.decode()))
                self.assertEqual((tmp / "until").read_text(), until, action)
            for line in (tmp / "exec.argv").read_text().splitlines():
                self.assertTrue(line.endswith(" exec -i testbed-postgres-0 -- psql -X -q -At -v ON_ERROR_STOP=1 "
                                              "-U commerce -d commerce"), line)
            logins = (tmp / "net.log").read_text().splitlines()
            self.assertEqual(len(logins), 3)  # preflight ok, run refused, cleanup ok
            for line in logins:
                self.assertTrue(line.startswith("192.168.122.184 "), line)
            db_operator_fakes.assert_no_password(self, (tmp / "kubectl.log").read_text(),
                                                 (tmp / "exec.argv").read_text(), "\n".join(logins))
            # A network failure that is not a refused password is not read as the expiry having bitten.
            (tmp / "until").write_text("NONE")
            broken = dict(env, PGCONNECT_TIMEOUT="1")
            (tmp / "bin" / "psql").write_text("#!/usr/bin/env bash\necho 'psql: error: connection timed out' >&2; exit 2\n")
            done = self._operator_run(db_account, "F71-R", "preflight", broken, slug)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("network login failed otherwise", done.stderr.decode())

    def test_node_filesystem_is_read_only_for_node_local_volumes(self) -> None:
        # The free-space checks (F60-R, F67-R) read the kubelet's node filesystem instead of df in the pod. That is
        # the data volume's filesystem only for a local-path (hostPath or local) volume; anything else is refused.
        body = db_operator_remote.bash_functions() + b'db_target rca-testbed-food testbed-mysql-0\nnode_fs "$1"\n'
        script = b"#!/usr/bin/env bash\nset -euo pipefail\n" + body
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = db_operator_fakes.install(tmp)
            out = subprocess.run(["bash", "-s", "--", "MB"], input=script, env=env, capture_output=True, timeout=30)
            self.assertEqual(out.stdout.decode().split(), ["102400", "20480", "81920"], out.stderr.decode())
            out = subprocess.run(["bash", "-s", "--", "KB"], input=script, env=env, capture_output=True, timeout=30)
            self.assertEqual(out.stdout.decode().split()[2], str(80 * 1024 * 1024))
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = db_operator_fakes.install(tmp, kubectl_cases=(
                "  *\"get pv pv-data-0 -o json\"*) printf '%s' '{\"spec\":{\"nfs\":{\"path\":\"/x\"}}}' ;;"))
            out = subprocess.run(["bash", "-s", "--", "MB"], input=script, env=env, capture_output=True, timeout=30)
            self.assertNotEqual(out.returncode, 0)
            self.assertIn(b"not on the node's filesystem", out.stderr)

    def _oracle_lock_stub(self, tmp: Path) -> dict[str, str]:
        """Fake kubectl and a fake `oracledb` around the real executor body. The fake
        database keeps one row lock in `tmp/lock` (owner pid) and logs every connect."""
        bindir = tmp / "bin"
        bindir.mkdir()
        pylib = tmp / "pylib"
        pylib.mkdir()
        (pylib / "oracledb.py").write_text(f"""import json, os
from pathlib import Path
TMP = Path({str(tmp)!r})
LOCK = TMP / "lock"
class _Err:
    def __init__(self, code): self.code = code
class DatabaseError(Exception): pass
def _holder():
    try: pid = int(LOCK.read_text())
    except (FileNotFoundError, ValueError): return None
    try: os.kill(pid, 0)
    except OSError: return None
    return pid
class Cursor:
    def __init__(self, conn): self.conn = conn; self.rows = []
    def execute(self, sql, **binds):
        # Oracle 23ai drops the connection ("Bad TTC Packet") when action is set to "".
        if self.conn.action == "": raise DatabaseError(_Err(4011))
        self.conn.log("sql", sql=sql, binds=binds, client_identifier=self.conn.client_identifier)
        if binds.get("k") != "commerce-settlement": raise DatabaseError(_Err(942))
        if "count(*)" in sql: self.rows = [(1,)]; return
        holder = _holder()
        if holder not in (None, os.getpid()): raise DatabaseError(_Err(30006))
        LOCK.write_text(str(os.getpid())); self.conn.locked = True; self.rows = [("commerce-settlement",)]
    def fetchone(self): return self.rows[0]
    def fetchall(self): return list(self.rows)
class Connection:
    def __init__(self, **kw):
        self.kw = kw; self.client_identifier = None; self.module = None; self.action = None; self.locked = False
        self.log("connect", **{{k: v for k, v in kw.items() if k != "password"}})
    def log(self, kind, **data):
        with (TMP / "calls").open("a") as f: f.write(json.dumps({{"kind": kind, "pid": os.getpid(), **data}}) + "\\n")
    def cursor(self): return Cursor(self)
    def rollback(self):
        if self.locked and _holder() == os.getpid(): LOCK.unlink()
        self.locked = False; self.log("rollback")
    def close(self): self.rollback(); self.log("close")
def connect(**kw):
    # python-oracledb thin refuses these before connecting (DPY-3029).
    for field in ("program", "machine", "osuser", "terminal"):
        if any(ch in kw.get(field, "") for ch in " ()="):
            raise DatabaseError(_Err(3029))
    return Connection(**kw)
""")
        secret = {"APP_USER": "banking", "APP_USER_PASSWORD": "pw-from-secret"}
        import base64
        kubectl = bindir / "kubectl"
        remote = load("oracle_lock_remote")
        # 실행 중인 DB 파드와 기동 스크립트 ConfigMap 은 매니페스트(10-oracle.yaml)에서 그대로 가져온다.
        import yaml
        docs = list(yaml.safe_load_all((ROOT.parents[1] / "core-banking/k8s/10-oracle.yaml").read_text(encoding="utf-8")))
        sts = next(d for d in docs if d["kind"] == "StatefulSet")
        cm = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == remote.STARTDB_CONFIGMAP)
        (tmp / "pod.json").write_text(json.dumps({"spec": sts["spec"]["template"]["spec"]}))
        (tmp / "cm.json").write_text(json.dumps(cm))
        kubectl.write_text("#!/usr/bin/env bash\n"
                           f"printf '%s\\n' \"$*\" >>{tmp}/kubectl\n"
                           f"[[ -e {tmp}/kubectl-down ]] && {{ echo 'The connection to the server was refused' >&2; exit 1; }}\n"
                           "case \"$*\" in\n"
                           "  *'jsonpath={.spec.nodeName}'*) printf tb-w2 ;;\n"
                           # The commerce pods (the domain the lock's symptoms show in) run on tb-w1.
                           "  *'-n rca-testbed-commerce get pods -o jsonpath={range .items[*]}{.spec.nodeName}'*) printf 'tb-w1\\ntb-w1\\n' ;;\n"
                           f"  *'get nodes -o json'*) printf '%s' '{json.dumps({'items': db_operator_fakes.NODES})}' ;;\n"
                           f"  *'get pod testbed-oracle-0 -o json') cat {tmp}/pod.json ;;\n"
                           f"  *'get configmap oracle-startdb-scripts -o json') cat {tmp}/cm.json ;;\n"
                           "  *nodePort*) printf 30308 ;;\n"
                           f"  *APP_USER_PASSWORD*) printf {base64.b64encode(secret['APP_USER_PASSWORD'].encode()).decode()} ;;\n"
                           f"  *APP_USER*) printf {base64.b64encode(secret['APP_USER'].encode()).decode()} ;;\n"
                           "  *) exit 9 ;;\n"
                           "esac\n")
        kubectl.chmod(0o755)
        python = bindir / "python-with-oracledb"
        python.write_text(f"#!/usr/bin/env bash\nPYTHONPATH={pylib} exec {sys.executable} \"$@\"\n")
        python.chmod(0o755)
        return dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", ORACLE_LOCK_PYTHON=str(python),
                    SCENARIO_PROFILE_STATE_ROOT=str(tmp / "state"))

    @staticmethod
    def _process_ended(pid: int) -> bool:
        """Gone or a zombie. The holder outlives the executor's shell, so whoever adopts it reaps it
        when it likes; kill(pid, 0) still succeeds on a zombie (seen 1 in 10 runs on 104)."""
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except FileNotFoundError:
            return True
        return stat.rsplit(")", 1)[1].split()[0] == "Z"

    def _oracle_lock_run(self, action: str, env: dict[str, str], hold: str | None = None) -> subprocess.CompletedProcess:
        argv, stdin = db_lock.build_invocation(compiler.compile_plan("f01-p-oracle-lock-cross-domain"), action)
        if hold is not None:
            argv[-1] = hold
        return subprocess.run(argv, input=stdin, env=env, capture_output=True, timeout=120, check=False)

    def test_oracle_lock_holds_the_row_as_an_operator_session_and_releases_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = self._oracle_lock_stub(tmp)
            (tmp / "state").mkdir()
            self.assertEqual(self._oracle_lock_run("preflight", env).returncode, 0)
            run = self._oracle_lock_run("run", env)
            self.assertEqual(run.returncode, 0, run.stderr.decode())
            holder = int((tmp / "lock").read_text())
            calls = [json.loads(line) for line in (tmp / "calls").read_text().splitlines()]
            hold_connect = next(c for c in calls if c["kind"] == "connect" and c["pid"] == holder)
            # Through tb-w3, a worker that runs neither the Oracle pod (tb-w2) nor the control plane nor the commerce
            # pods (tb-w1) whose payments the lock stops (user decision Q8, C6b review 2 중 2).
            self.assertEqual(hold_connect["dsn"], "192.168.122.14:30308/FREEPDB1")
            self.assertEqual(hold_connect["user"], "banking")
            self.assertEqual((hold_connect["program"], hold_connect["machine"], hold_connect["osuser"],
                              hold_connect["driver_name"]),
                             ("SQLcl", "ops-bastion", "dba", "jdbcthin : 23.7.0.25.1"))
            lock_sql = next(c for c in calls if c["kind"] == "sql" and c["pid"] == holder)
            self.assertEqual(lock_sql["sql"], "select id from accounts where id = :k for update wait 60"
                             .replace("accounts", "BANKING.accounts"))
            self.assertEqual(lock_sql["client_identifier"], "dba-maintenance")
            # 짧은 확인 세션에는 태그를 붙이지 않는다(러너의 태그 세션 수 관측이 잠금 세션만 센다).
            self.assertTrue(all(c["client_identifier"] is None for c in calls if c["kind"] == "sql" and c["pid"] != holder))
            # 잠금 중에는 같은 실행이 다시 시작되지 않는다.
            self.assertNotEqual(self._oracle_lock_run("preflight", env).returncode, 0)
            # kubectl 은 조회만 한다. DB 파드에 exec 하지 않는다.
            for line in (tmp / "kubectl").read_text().splitlines():
                self.assertEqual(next(w for w in line.split() if w in ("get", "exec", "apply", "delete", "patch")), "get", line)
            cleanup = self._oracle_lock_run("cleanup", env)
            self.assertEqual(cleanup.returncode, 0, cleanup.stderr.decode())
            self.assertFalse((tmp / "lock").exists())
            self.assertTrue(self._process_ended(holder))
            self.assertEqual(self._oracle_lock_run("recovery", env).returncode, 0)
            self.assertEqual(self._oracle_lock_run("cleanup", env).returncode, 0)
            self.assertNotIn("pw-from-secret", (tmp / "calls").read_text())

    def test_oracle_lock_releases_by_itself_when_the_hold_ends(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = self._oracle_lock_stub(tmp)
            (tmp / "state").mkdir()
            run = self._oracle_lock_run("run", env, hold="2")
            self.assertEqual(run.returncode, 0, run.stderr.decode())
            holder = int((tmp / "lock").read_text())
            deadline = time.monotonic() + 15
            while (tmp / "lock").exists() and time.monotonic() < deadline:
                time.sleep(0.2)
            self.assertFalse((tmp / "lock").exists())
            kinds = [json.loads(line)["kind"] for line in (tmp / "calls").read_text().splitlines()
                     if json.loads(line)["pid"] == holder]
            # 보유 시간이 끝나면 잠금 세션이 스스로 롤백하고 닫는다.
            self.assertEqual(kinds[-1], "close")
            self.assertIn("rollback", kinds)
            self.assertEqual(self._oracle_lock_run("cleanup", env).returncode, 0)

    def test_oracle_lock_cleanup_fails_while_another_session_still_holds_the_row(self) -> None:
        # 해제 확인은 서버 쪽이다: 우리 프로세스가 죽었어도 행을 잡을 수 없으면 실패한다.
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = self._oracle_lock_stub(tmp)
            (tmp / "state").mkdir()
            run = self._oracle_lock_run("run", env)
            self.assertEqual(run.returncode, 0, run.stderr.decode())
            other = subprocess.Popen(["sleep", "60"])
            try:
                self._oracle_lock_run("cleanup", env)  # stops our holder first
                (tmp / "state" / "db-lock-oracle-F01-P").mkdir(exist_ok=True)
                (tmp / "lock").write_text(str(other.pid))
                result = self._oracle_lock_run("cleanup", env)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotEqual(self._oracle_lock_run("recovery", env).returncode, 0)
            finally:
                other.kill()
                other.wait()

    def test_oracle_lock_cleanup_stops_the_local_session_even_when_the_api_is_down(self) -> None:
        # 리뷰 C6 2번: kube API 가 실패해도 이 호스트의 잠금 세션은 반드시 멈추고 실패를 보고한다.
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = self._oracle_lock_stub(tmp)
            (tmp / "state").mkdir()
            run = self._oracle_lock_run("run", env)
            self.assertEqual(run.returncode, 0, run.stderr.decode())
            holder = int((tmp / "lock").read_text())
            (tmp / "kubectl-down").touch()
            cleanup = self._oracle_lock_run("cleanup", env)
            self.assertNotEqual(cleanup.returncode, 0)
            self.assertTrue(self._process_ended(holder))
            self.assertFalse((tmp / "lock").exists())
            # 상태는 남아 API 가 돌아오면 다시 정리, 확인한다.
            (tmp / "kubectl-down").unlink()
            self.assertEqual(self._oracle_lock_run("cleanup", env).returncode, 0)
            self.assertEqual(self._oracle_lock_run("recovery", env).returncode, 0)

    def test_oracle_lock_cleanup_finds_a_session_whose_pid_file_is_gone(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = self._oracle_lock_stub(tmp)
            (tmp / "state").mkdir()
            self.assertEqual(self._oracle_lock_run("run", env).returncode, 0)
            holder = int((tmp / "lock").read_text())
            (tmp / "state" / "db-lock-oracle-F01-P" / "pid").unlink()
            self.assertNotEqual(self._oracle_lock_run("preflight", env).returncode, 0)
            self.assertEqual(self._oracle_lock_run("cleanup", env).returncode, 0)
            self.assertTrue(self._process_ended(holder))

    def test_oracle_lock_refuses_without_server_dead_connection_detection(self) -> None:
        # 리뷰 C6 1번(U8): 러너가 FIN 없이 사라지면 서버만이 잠금을 풀 수 있다. DB 파드가
        # SQLNET.EXPIRE_TIME 이 든 sqlnet.ora 를 마운트하지 않았으면 주입하지 않는다.
        remote = load("oracle_lock_remote")
        key = remote.STARTDB_SCRIPT_KEY

        def mounts(pod):
            return pod["spec"]["containers"][0]["volumeMounts"]

        def startdb(pod):
            return next(m for m in mounts(pod) if m["mountPath"] == remote.STARTDB_PATH)

        cases = {
            "not mounted": lambda pod, cm: pod["spec"]["containers"][0].update(
                volumeMounts=[m for m in mounts(pod) if m["mountPath"] != remote.STARTDB_PATH]),
            "other path": lambda pod, cm: startdb(pod).update(mountPath="/container-entrypoint-initdb.d/x"),
            "other configmap": lambda pod, cm: [v["configMap"].update(name="oracle-init-scripts")
                                                for v in pod["spec"]["volumes"] if v["name"] == startdb(pod)["name"]],
            "no script": lambda pod, cm: cm["data"].pop(key),
            "too slow": lambda pod, cm: cm["data"].update({key: cm["data"][key].replace("EXPIRE_MINUTES=1", "EXPIRE_MINUTES=10")}),
            "value not used": lambda pod, cm: cm["data"].update({key: "EXPIRE_MINUTES=1\n"}),
        }
        for name, spoil in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as raw:
                tmp = Path(raw)
                env = self._oracle_lock_stub(tmp)
                (tmp / "state").mkdir()
                pod, cm = json.loads((tmp / "pod.json").read_text()), json.loads((tmp / "cm.json").read_text())
                spoil(pod, cm)
                (tmp / "pod.json").write_text(json.dumps(pod))
                (tmp / "cm.json").write_text(json.dumps(cm))
                for action in ("preflight", "run"):
                    result = self._oracle_lock_run(action, env)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(b"dead connection detection is not in effect", result.stderr)
                self.assertFalse((tmp / "lock").exists())
        self.assertEqual(remote.SQLNET_EXPIRE_MAX_MINUTES, 2)

    def test_oracle_lock_hold_never_outlasts_the_injection_the_runner_allows(self) -> None:
        # U8: 러너가 죽어도 러너 컨테이너가 살아 있으면 잠금 세션은 hold 가 끝날 때 스스로 롤백한다.
        # 그 시각이 러너가 주입을 끝냈을 시각(max_injection_duration)보다 늦으면 안 된다.
        # (러너 컨테이너까지 사라지면 서버 dead connection detection 이 푼다. 아래 매니페스트 시험.)
        controllers = json.loads((ROOT / "registry/controllers.json").read_text())["controllers"]
        multi = load("timeline_multi_injection_executor")

        def seconds(value: str) -> int:
            number, unit = re.fullmatch(r"(\d+)([smh]?)", value).groups()
            return int(number) * {"": 1, "s": 1, "m": 60, "h": 3600}[unit]

        holds = {sid: [c["hold_seconds"]] for sid, c in db_lock.CONTRACTS.items() if c.get("engine") == "oracle"}
        for sid, contract in multi.CONTRACTS.items():
            holds[sid] = [step["hold_seconds"] for step in contract["steps"] if step["kind"] == "oracle_lock"]
        self.assertEqual(set(holds), {"F01-P", "F08-G", "F15-G"})
        for sid, values in holds.items():
            limit = seconds(controllers[sid]["max_injection_duration"])
            for hold in values:
                self.assertLessEqual(hold, limit, sid)

    def test_banking_oracle_manifest_turns_on_dead_connection_detection(self) -> None:
        # 기동 스크립트가 진짜 sqlnet.ora(oradata/dbconfig/<SID>, PVC 위)에 EXPIRE_TIME 을 한 줄만
        # 넣는지 실제로 돌려 본다: 처음, 다시 기동(같은 줄 유지), 다른 값이 있던 경우. sqlnet.ora 를
        # 마운트로 덮으면 이미지 진입 스크립트의 mv, rm 이 기동을 멈추므로 덮는 마운트가 없어야 한다.
        import yaml
        remote = load("oracle_lock_remote")
        docs = list(yaml.safe_load_all((ROOT.parents[1] / "core-banking/k8s/10-oracle.yaml").read_text(encoding="utf-8")))
        cm = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == remote.STARTDB_CONFIGMAP)
        script = cm["data"][remote.STARTDB_SCRIPT_KEY]
        self.assertNotRegex(script, r"(?m)^\s*exit\b")  # 진입 스크립트가 source 해도 안전
        image_default = ("NAMES.DIRECTORY_PATH = (EZCONNECT, TNSNAMES)\n"
                         "# See https://github.com/gvenzl/oci-oracle-xe/issues/43\nDISABLE_OOB=ON\nBREAK_POLL_SKIP=1000\n")
        # 리뷰 2회차 T-F: 소문자 표기의 기존 줄도 같은 파라미터라 하나로 합친다.
        for before in (image_default, image_default + "SQLNET.EXPIRE_TIME = 10\n",
                       image_default + "sqlnet.expire_time = 10\n", "  Sqlnet.Expire_Time=5\n" + image_default):
            with tempfile.TemporaryDirectory() as raw:
                base = Path(raw)
                conf = base / "oradata" / "dbconfig" / "FREE"
                conf.mkdir(parents=True)
                (conf / "sqlnet.ora").write_text(before)
                runner = base / "10-sqlnet-expire-time.sh"
                runner.write_text(script)
                env = dict(os.environ, ORACLE_BASE=str(base), ORACLE_SID="FREE")
                for _ in range(2):
                    subprocess.run(["bash", str(runner)], env=env, check=True, capture_output=True)
                after = (conf / "sqlnet.ora").read_text()
                self.assertEqual(re.findall(r"(?im)^\s*SQLNET\.EXPIRE_TIME.*$", after), ["SQLNET.EXPIRE_TIME = 1"])
                for kept in ("NAMES.DIRECTORY_PATH = (EZCONNECT, TNSNAMES)", "DISABLE_OOB=ON", "BREAK_POLL_SKIP=1000"):
                    self.assertIn(kept, after)
        sts = next(d for d in docs if d["kind"] == "StatefulSet")
        spec = sts["spec"]["template"]["spec"]
        volume = next(v for v in spec["volumes"] if (v.get("configMap") or {}).get("name") == remote.STARTDB_CONFIGMAP)
        self.assertEqual(volume["configMap"].get("defaultMode"), 0o555)
        mounts = spec["containers"][0]["volumeMounts"]
        self.assertIn({"name": volume["name"], "mountPath": remote.STARTDB_PATH}, mounts)
        self.assertFalse([m for m in mounts if m["mountPath"].endswith("/network/admin/sqlnet.ora")])

    def test_oracle_lock_client_refuses_identifiers_that_are_not_plain_names(self) -> None:
        # 식별자는 따옴표 없이 SQL 에 들어가므로 이름 모양만 받는다. 키 값은 바인드 변수다.
        client = load("oracle_lock_session")
        for bad in ("accounts; drop table x", "\"accounts\"", "", "1accounts", "acc ounts"):
            with self.subTest(bad=bad), unittest.mock.patch.dict(
                os.environ, {"LOCK_SCHEMA": "BANKING", "LOCK_TABLE": bad, "LOCK_KEYCOL": "id"}, clear=False
            ):
                with self.assertRaises(SystemExit):
                    client._target()
        with unittest.mock.patch.dict(os.environ, {"LOCK_SCHEMA": "BANKING", "LOCK_TABLE": "accounts", "LOCK_KEYCOL": "id"}):
            self.assertEqual(client._target(), ("BANKING.accounts", "id"))

    def test_db_account_locks_only_the_app_account_and_unlocks_it(self) -> None:
        # F35-R: 주입은 계정 상태 한 칸(ACCOUNT_STATUS)만 뒤집는다. 세션을 끊거나
        # 비밀번호를 바꾸면 "기존 세션은 살고 새 로그인만 막힌다"는 지문이 사라진다.
        plan = compiler.compile_plan("f35-r-banking-oracle-app-account-locked")
        argv, stdin = db_account.build_invocation(plan, "run")
        self.assertEqual(argv[0], "/usr/bin/bash")
        # 시나리오 id 는 kubectl 이쪽에 머문다(G6, 원칙 8).
        self.assertNotIn("F35-R", argv)
        script = stdin.decode()
        self.assertNotIn("F35-R", script)
        run_case = script.split("run)")[1].split(";;")[0]
        self.assertIn("expect OPEN", run_case)
        self.assertIn("flip lock", run_case)
        self.assertIn("expect LOCKED", run_case)
        cleanup_case = script.split("cleanup)")[1].split(";;")[0]
        self.assertIn("flip unlock", cleanup_case)
        self.assertIn("expect OPEN", cleanup_case)
        for forbidden in ("kill session", "identified by", "drop user", "password"):
            self.assertNotIn(forbidden, own_body(script).lower())
        params = dict(db_account.CONTRACTS["F35-R"])
        params["account"] = "LUCIDA_MON"
        with self.assertRaisesRegex(ExecutorError, "exactly match"):
            db_account.validate("F35-R", params, {})
        with self.assertRaisesRegex(ExecutorError, "no verified database account contract"):
            db_account.validate("F14-P", db_account.CONTRACTS["F35-R"], {})

    def test_db_account_profile_limit_assigns_one_capped_profile_and_reverts_it(self) -> None:
        # F54-R: 주입은 새 프로필 하나(호출당 논리 읽기 한도)를 만들어 앱 계정에 붙이는 것뿐이다.
        # 계정 상태를 잠그거나 세션을 끊으면 "기존 세션은 그대로, 새 세션의 무거운 호출만 끊긴다"는
        # 지문이 F35-R(잠금)이나 다른 장애가 된다. DEFAULT 프로필을 고치면 DPM 계정(LUCIDA_MON)까지 묶인다.
        plan = compiler.compile_plan("f54-r-banking-oracle-app-account-call-read-limit-breaks-history")
        argv, stdin = db_account.build_invocation(plan, "run")
        self.assertEqual(argv[:4], ["/usr/bin/bash", "-s", "--", "run"])
        self.assertEqual(argv[4:], ["rca-testbed-banking", "testbed-oracle-0", "FREEPDB1", "BANKING",
                                    "APP_CALL_LIMITS", "LOGICAL_READS_PER_CALL", "30000"])
        self.assertNotIn("F54-R", argv)
        script = stdin.decode()
        self.assertNotIn("F54-R", script)
        self.assertNotEqual(stdin, db_account.ORACLE_REMOTE)
        run_case = script.split("run)")[1].split(";;")[0]
        self.assertIn('expect "DEFAULT|NONE|TRUE"', run_case)
        self.assertIn('"create profile $profile limit $resource $limit;" "alter user $account profile $profile;"', run_case)
        self.assertIn('expect "$profile|$limit|TRUE"', run_case)
        cleanup_case = script.split("cleanup)")[1].split(";;")[0]
        self.assertIn('"alter user $account profile default;"', cleanup_case)
        self.assertIn('"drop profile $profile;"', cleanup_case)
        self.assertIn('expect "DEFAULT|NONE|TRUE"', cleanup_case)
        for forbidden in ("kill session", "identified by", "account lock", "drop user", "password",
                          "alter profile default", "$profile cascade"):
            self.assertNotIn(forbidden, own_body(script).lower())
        # F35-R 은 그대로 잠금 스크립트를 쓴다.
        f35r_argv, f35r_stdin = db_account.build_invocation(
            compiler.compile_plan("f35-r-banking-oracle-app-account-locked"), "run")
        self.assertEqual(f35r_stdin, db_account.ORACLE_REMOTE)
        self.assertEqual(len(f35r_argv), 8)
        params = dict(db_account.CONTRACTS["F54-R"])
        params["limit"] = 100000
        with self.assertRaisesRegex(ExecutorError, "exactly match"):
            db_account.validate("F54-R", params, {})
        with self.assertRaisesRegex(ExecutorError, "exactly match"):
            db_account.validate("F54-R", db_account.CONTRACTS["F35-R"], {})

    def test_db_account_mysql_hourly_write_cap_sets_one_limit_and_lifts_it(self) -> None:
        # F54-P: 주입은 food 앱 계정 하나에 시간당 쓰기 한도(MAX_UPDATES_PER_HOUR) 하나를 거는 것뿐이다.
        # 질의 수 한도(MAX_QUERIES_PER_HOUR)는 드라이버의 접속 초기화와 health 까지 막아 풀이 비고 입구가 끊긴다.
        # 연결을 끊거나 잠그거나 권한을 건드리면 "기존 연결은 그대로, 새 연결의 쓰기만 공유 카운터가 차면 1226"이라는
        # 지문이 F35-R(잠금), 권한 회수(1142), F38-R(인스턴스 read_only) 같은 다른 장애가 된다. DPM 계정 lucida_mon 은 대상이 아니다.
        plan = compiler.compile_plan("f54-p-food-mysql-app-account-hourly-write-cap-breaks-orders")
        argv, stdin = db_account.build_invocation(plan, "run")
        self.assertEqual(argv[:4], ["/usr/bin/bash", "-s", "--", "run"])
        self.assertEqual(argv[4:], ["rca-testbed-food", "testbed-mysql-0", "fooddelivery", "%",
                                    "MAX_UPDATES_PER_HOUR", "500"])
        self.assertNotIn("F54-P", argv)
        self.assertEqual(stdin, db_account.MYSQL_LIMIT_REMOTE)
        script = stdin.decode()
        self.assertNotIn("F54-P", script)
        self.assertIn("  MAX_UPDATES_PER_HOUR) ;;\n  *) exit 2 ;;", script)
        self.assertIn("FROM mysql.user WHERE user='$account' AND host='$host'", script)
        run_case = script.split("  run)")[1].split(";;")[0]
        order = ['expect "0|0|0|0"', 'cap "$limit"', 'expect "$limit|0|0|0"']
        positions = [run_case.index(x) for x in order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("ALTER USER '$account'@'$host' WITH $resource $1;", script)
        cleanup_case = script.split("  cleanup)")[1].split(";;")[0]
        self.assertIn("cap 0", cleanup_case)
        self.assertIn('expect "0|0|0|0"', cleanup_case)
        for forbidden in ("kill", "identified", "account lock", "revoke", "grant ", "drop ", "password expire",
                          "max_queries_per_hour", "set global", "lucida_mon"):
            self.assertNotIn(forbidden, own_body(script).lower())
        # Oracle 두 시나리오는 그대로 자기 스크립트를 쓴다.
        _, f54r_stdin = db_account.build_invocation(
            compiler.compile_plan("f54-r-banking-oracle-app-account-call-read-limit-breaks-history"), "run")
        self.assertEqual(f54r_stdin, db_account.ORACLE_PROFILE_REMOTE)
        params = dict(db_account.CONTRACTS["F54-P"])
        for change in ({"resource": "MAX_QUERIES_PER_HOUR"}, {"account": "lucida_mon"}, {"limit": 0}, {"host": "localhost"}):
            with self.assertRaisesRegex(ExecutorError, "exactly match"):
                db_account.validate("F54-P", {**params, **change}, {})
        with self.assertRaisesRegex(ExecutorError, "exactly match"):
            db_account.validate("F54-P", db_account.CONTRACTS["F54-R"], {})

    def test_db_account_postgres_password_expiry_dates_one_role_and_renews_it(self) -> None:
        # F71-R: 주입은 commerce 애플리케이션 역할 하나의 비밀번호 기한(VALID UNTIL)을 지금으로 두는 것뿐이다.
        # 비밀번호를 바꾸거나 역할을 잠그거나(NOLOGIN) 세션을 끊으면 "열린 연결은 그대로, 새 비밀번호 로그인만 거절"이라는
        # 지문이 F65-P(없는 역할 이름), F35-R(계정 잠금), F25-H(DB 다운) 같은 다른 장애가 된다. 실행기는 파드 local 소켓(trust)으로
        # 들어가 만료에 막히지 않고(파드 안에는 psql 하나, pdb_pod), 로그인 확인은 러너의 네트워크 세션이다. DPM 역할 lucida_mon 은
        # 대상이 아니다.
        plan = compiler.compile_plan("f71-r-commerce-postgres-app-account-password-expiry")
        argv, stdin = db_account.build_invocation(plan, "run")
        self.assertEqual(argv[:4], ["/usr/bin/bash", "-s", "--", "run"])
        self.assertEqual(argv[4:], ["rca-testbed-commerce", "testbed-postgres-0", "commerce"])
        self.assertNotIn("F71-R", argv)
        self.assertEqual(stdin, db_account.POSTGRES_EXPIRY_REMOTE)
        script = stdin.decode()
        self.assertNotIn("F71-R", script)
        own = own_body(script)
        self.assertIn('sql() { pdb_pod "$1"; }', own)
        self.assertIn("SET pg_stat_statements.track = none;", script)
        self.assertNotIn(" exec ", own)
        self.assertNotIn("kubectl", own)
        self.assertIn('if err="$(pdb "SELECT 1" 2>&1 >/dev/null)"; then echo ok', own)
        # Only a refused password reads as refused; no route or a timeout stops the action instead.
        self.assertIn('elif [[ "$err" == *"password authentication failed"* ]]; then echo refused', own)
        script = own
        run_case = script.split("  run)")[1].split(";;")[0]
        order = ['expect "NONE|true|scram-sha-256"', "VALID UNTIL '$now'", 'expect "EXPIRED|true|scram-sha-256"',
                 '[[ "$(netlogin)" == refused ]]']
        positions = [run_case.index(x) for x in order]
        self.assertEqual(positions, sorted(positions))
        preflight_case = script.split("  preflight)")[1].split(";;")[0]
        self.assertIn('[[ "$(netlogin)" == ok ]]', preflight_case)
        cleanup_case = script.split("  cleanup)")[1].split(";;")[0]
        self.assertIn("VALID UNTIL 'infinity'", cleanup_case)
        self.assertIn('expect "NONE|true|scram-sha-256"', cleanup_case)
        self.assertIn('[[ "$(netlogin)" == ok ]]', cleanup_case)
        for forbidden in ("password '", "encrypted password", "nologin", "pg_terminate_backend", "pg_cancel_backend",
                          "revoke", "grant ", "drop ", "alter system", "lucida_mon", "pg_hba.conf"):
            self.assertNotIn(forbidden, script.lower())
        # 다른 세 시나리오는 그대로 자기 스크립트를 쓴다.
        _, f35r_stdin = db_account.build_invocation(
            compiler.compile_plan("f35-r-banking-oracle-app-account-locked"), "run")
        self.assertEqual(f35r_stdin, db_account.ORACLE_REMOTE)
        params = dict(db_account.CONTRACTS["F71-R"])
        for change in ({"account": "lucida_mon"}, {"engine": "mysql"}, {"pod": "testbed-postgres-1"},
                       {"change": "password-reset"}, {"namespace": "rca-testbed-food"}):
            with self.assertRaisesRegex(ExecutorError, "exactly match"):
                db_account.validate("F71-R", {**params, **change}, {})
        with self.assertRaisesRegex(ExecutorError, "exactly match"):
            db_account.validate("F71-R", db_account.CONTRACTS["F35-R"], {})

    def test_db_account_postgres_host_rule_narrows_the_catch_all_and_restores_it(self) -> None:
        # F71-H: 주입은 pg_hba 의 모든 주소 네트워크 규칙 한 줄을 노드 주소와 관리망 규칙으로 바꾸고 설정을 다시 읽히는 것뿐이다.
        # 역할의 비밀번호, 기한, 로그인 가능 여부를 건드리거나 세션을 끊으면 F71-R(기한 경과), F35-R(잠금), F25-H(DB 다운)의
        # 지문이 된다. local 규칙은 그대로라 실행기는 파드 local 소켓(trust)으로 계속 들어가고(파드 안에는 psql 하나, pdb_pod),
        # NodePort 로 노드 주소에 닿는 DPM 은 허용 목록 안이다. 빠지는 것은 같은 노드 서비스 파드 대역뿐이다.
        plan = compiler.compile_plan("f71-h-commerce-postgres-access-rule-omits-app-pods")
        argv, stdin = db_account.build_invocation(plan, "run")
        self.assertEqual(argv[:4], ["/usr/bin/bash", "-s", "--", "run"])
        self.assertEqual(argv[4:], ["rca-testbed-commerce", "testbed-postgres-0", "commerce",
                                    "10.244.3.1/32,192.168.122.0/24,192.168.213.0/24,192.168.230.0/24",
                                    "10.244.3.0/24"])
        self.assertNotIn("F71-H", argv)
        self.assertEqual(stdin, db_account.POSTGRES_HBA_REMOTE)
        script = stdin.decode()
        self.assertNotIn("F71-H", script)
        own = own_body(script)
        self.assertIn('sql() { pdb_pod "$1"; }', own)
        self.assertIn("FROM pg_hba_file_rules", own)
        # v4 C6b: the file is rewritten by the server's own functions through psql alone in the pod, not by a
        # shell there; the reload is confirmed by a new session's configuration load time. No network session,
        # so no check of the runner's own source (it must not be the DB monitor's allowed address).
        self.assertIn("pg_read_binary_file(current_setting('hba_file'))", own)
        self.assertIn("lo_export(o, current_setting('hba_file'))", own)
        self.assertNotIn(" exec ", own)
        self.assertNotIn("kubectl", own)
        run_case = own.split("  run)")[1].split(";;")[0]
        order = ['expect "1|0|0"', "inside", "bites", "edit narrow", 'expect "0|$n|0"', "reload",
                 'until_reloaded "$before"']
        positions = [run_case.index(x) for x in order]
        self.assertEqual(positions, sorted(positions))
        preflight_case = own.split("  preflight)")[1].split(";;")[0]
        self.assertIn("bites", preflight_case)
        self.assertNotIn("edit", preflight_case)
        self.assertNotIn("self_allowed", own)
        self.assertNotIn("pdb ", own)
        cleanup_case = own.split("  cleanup)")[1].split(";;")[0]
        self.assertIn("edit restore", cleanup_case)
        self.assertIn('until_reloaded "$before"', cleanup_case)
        for forbidden in ("valid until", "nologin", "password '", "pg_terminate_backend", "pg_cancel_backend",
                          "revoke", "grant ", "drop ", "alter system", "alter role", "lucida_mon", "pg_ctl",
                          "postgresql.conf"):
            self.assertNotIn(forbidden, own.lower())
        # F71-R 은 그대로 기한 스크립트를 쓴다.
        _, f71r_stdin = db_account.build_invocation(
            compiler.compile_plan("f71-r-commerce-postgres-app-account-password-expiry"), "run")
        self.assertEqual(f71r_stdin, db_account.POSTGRES_EXPIRY_REMOTE)
        params = dict(db_account.CONTRACTS["F71-H"])
        for change in ({"allowed_sources": ["10.244.3.0/24"]}, {"allowed_sources": []}, {"omitted_cidr": "10.244.1.0/24"},
                       {"change": "password-valid-until-now"}, {"pod": "testbed-postgres-1"}, {"engine": "mysql"}):
            with self.assertRaisesRegex(ExecutorError, "exactly match"):
                db_account.validate("F71-H", {**params, **change}, {})
        with self.assertRaisesRegex(ExecutorError, "exactly match"):
            db_account.validate("F71-H", db_account.CONTRACTS["F71-R"], {})

    F74R_SLUG = "f74-r-banking-transfer-arrival-poll-count-saturates-oracle"

    def test_db_cursor_reset_is_a_read_only_companion_that_purges_one_statement_after_the_load(self) -> None:
        # F74-R(runbook §8-11, 2026-11-09 사용자 결정): 폴링이 건수 질의 byb2y2hv7p4ag 에 다른 plan(847607780) 자식
        # 커서를 남겨 은행 평시가 약 33시간 느렸다. 정리는 그 문장 하나의 커서를 비우는 것이고, SPM 이나 통계 재수집
        # (표 통계 자체를 바꿈), 공유 풀 전체 비우기는 쓰지 않는다.
        plan = compiler.compile_plan(self.F74R_SLUG)
        self.assertEqual([i["profile_id"] for i in plan["profile_instances"]], ["load.north_south", "db.cursor_reset"])
        self.assertEqual(plan["controller"]["binding"]["companion_refs"], ["db.cursor_reset"])
        argv, stdin = db_cursor_reset.build_invocation(plan, "cleanup")
        self.assertEqual(argv, ["/usr/bin/bash", "-s", "--", "cleanup", "rca-testbed-banking", "testbed-oracle-0",
                                "FREEPDB1", "byb2y2hv7p4ag", "2603328921", "300"])
        script = stdin.decode()
        # 시나리오 id 는 kubectl 이쪽에 머문다(G6, 원칙 8).
        self.assertNotIn("F74-R", argv)
        self.assertNotIn("F74-R", script)
        run_case = script.split("\n  run)")[1].split(";;")[0]
        self.assertNotIn("purge", run_case)
        cleanup_case = script.split("\n  cleanup)")[1].split(";;")[0]
        self.assertIn("purge", cleanup_case)
        self.assertIn("sys.dbms_shared_pool.purge(c.name, 'C')", script)
        self.assertIn("select address||','||hash_value as name from v\\$sqlarea where sql_id=$q$sql_id$q", script)
        recovery_case = script.split("\n  recovery)")[1].split(";;")[0]
        self.assertNotIn("purge", recovery_case)
        self.assertEqual(script.count("whenever sqlerror exit failure"), 3)
        for forbidden in ("flush shared_pool", "dbms_spm", "gather_table_stats", "gather_schema_stats", "no_invalidate",
                          "alter system", "kill session", "delete ", "update ", "insert "):
            self.assertNotIn(forbidden, script.lower())
        for change in ({"sql_id": "bzbd2xfdtjdxv"}, {"expected_plan_hash_value": 847607780},
                       {"reparse_wait_seconds": 3600}, {"pdb": "FREE"}):
            with self.assertRaisesRegex(ExecutorError, "exactly match"):
                db_cursor_reset.validate("F74-R", {**db_cursor_reset.CONTRACTS["F74-R"], **change}, {})
        with self.assertRaisesRegex(ExecutorError, "no verified cursor reset contract"):
            db_cursor_reset.validate("F42-R", db_cursor_reset.CONTRACTS["F74-R"], {})

    def _cursor_stub(self, tmp: Path, children: list[tuple[int, int]], after_purge: list[tuple[int, int]] | None,
                     ) -> dict[str, str]:
        """A fake Oracle behind the operator client (v4 C6b; it used to be kubectl exec -> sqlplus) around
        the real script. `children` is v$sql for the statement as (plan_hash_value, executions) lines; a
        purge replaces it with `after_purge` (the steady load's re-parse), or leaves it as it is when
        `after_purge` is None (a child the purge could not free)."""
        rows = lambda items: "".join(f"{plan} {execs}\n" for plan, execs in items)
        (tmp / "children").write_text(rows(children))
        (tmp / "after_purge").write_text("keep" if after_purge is None else rows(after_purge))
        fake = f"""import re, sys
from pathlib import Path
tmp = Path({str(tmp)!r})
sql = sys.stdin.read()
with (tmp / "calls").open("a") as log:
    log.write(sql.replace("\\n", " ") + "\\n")
if "dbms_shared_pool.purge" in sql:
    after = (tmp / "after_purge").read_text()
    if after != "keep":
        (tmp / "children").write_text(after)
elif "from dba_objects" in sql:
    print("VALID")
elif "from v$sql where sql_id=" in sql:
    expected = int(re.search(r"plan_hash_value<>(\\d+)", sql).group(1))
    rows = [tuple(map(int, line.split())) for line in (tmp / "children").read_text().splitlines() if line.strip()]
    other = sorted({{p for p, e in rows if p != expected}})
    print("|".join(map(str, [len(rows), sum(1 for p, e in rows if e > 0), len([r for r in rows if r[0] != expected]),
                            sum(1 for p, e in rows if e > 0 and p != expected)])) + "|" + (",".join(map(str, other)) or "-"))
else:
    sys.exit(7)
"""
        return db_operator_fakes.install(tmp, oracle=fake)

    def _cursor_run(self, action: str, env: dict[str, str], wait: str | None = None) -> subprocess.CompletedProcess:
        argv, stdin = db_cursor_reset.build_invocation(compiler.compile_plan(self.F74R_SLUG), action)
        if wait is not None:
            argv[-1] = wait
        return subprocess.run(argv, input=stdin, env=env, capture_output=True, timeout=120, check=False)

    def test_db_cursor_reset_refuses_to_start_on_a_stuck_plan(self) -> None:
        # 은행 녹화 전 계획 점검: 이미 다른 plan 으로 실행 중이면 주입 전에 멈춘다. 커서가 공유 풀에 없으면(밀려남)
        # 고착의 증거가 아니라 통과한다.
        with tempfile.TemporaryDirectory() as raw:
            env = self._cursor_stub(Path(raw), [(2603328921, 215897)], None)
            self.assertEqual(self._cursor_run("preflight", env).returncode, 0)
            self.assertEqual(self._cursor_run("run", env).returncode, 0)
            self.assertNotIn("purge", (Path(raw) / "calls").read_text())
        with tempfile.TemporaryDirectory() as raw:
            env = self._cursor_stub(Path(raw), [(2603328921, 900), (847607780, 40)], None)
            for action in ("preflight", "run"):
                result = self._cursor_run(action, env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"runs on plan 847607780, expected 2603328921", result.stderr)
        with tempfile.TemporaryDirectory() as raw:
            env = self._cursor_stub(Path(raw), [], None)
            self.assertEqual(self._cursor_run("preflight", env).returncode, 0)

    def test_db_cursor_reset_cleanup_purges_and_recovery_proves_the_plan_came_back(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = self._cursor_stub(tmp, [(2603328921, 900), (847607780, 4000)], [(2603328921, 3)])
            self.assertEqual(self._cursor_run("cleanup", env).returncode, 0)
            self.assertEqual((tmp / "calls").read_text().count("dbms_shared_pool.purge"), 1)
            self.assertEqual(self._cursor_run("recovery", env).returncode, 0)

    def test_db_cursor_reset_recovery_fails_when_the_statement_comes_back_on_another_plan(self) -> None:
        # 계획 복귀 확인 실패는 recovery 실패이고, profile-control 은 recovery 가 실패한 cleanup 을 실패로 보고한다
        # (test_profile_control.test_cleanup_is_reported_failed_when_recovery_check_fails).
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = self._cursor_stub(tmp, [(847607780, 4000)], [(2603328921, 0), (847607780, 2)])
            # 다시 파싱된 자식이 아직 실행 전이면 cleanup 은 남은 다른 plan 자식을 본다.
            result = self._cursor_run("cleanup", env)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((tmp / "calls").read_text().count("dbms_shared_pool.purge"), 3)
            result = self._cursor_run("recovery", env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"came back on plan 847607780, expected 2603328921", result.stderr)

    def test_db_cursor_reset_cleanup_fails_when_the_stuck_child_survives_the_purge(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = self._cursor_stub(tmp, [(847607780, 4000)], None)
            result = self._cursor_run("cleanup", env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"attempt 3: sql_id byb2y2hv7p4ag still has a child on plan 847607780", result.stderr)

    def test_db_cursor_reset_recovery_fails_when_the_statement_is_not_run_again_in_time(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            env = self._cursor_stub(Path(raw), [], None)
            result = self._cursor_run("recovery", env, wait="0")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"was not executed again within 0s", result.stderr)

    def test_host_firewall_adds_one_allowlist_chain_for_new_connections_and_removes_it(self) -> None:
        # F44-R: 주입은 tb-w3 FORWARD 맨 위에 한 포트로 가는 새 연결(conntrack NEW)만 허용 목록 체인으로
        # 보내는 규칙과 그 체인뿐이다. 이미 맺어진 연결을 끊거나(원본의 '지속 소켓은 다시 맺어질 때까지 무사'가
        # 사라진다) 정책, INPUT, 다른 체인을 건드리면 다른 장애(노드 단절, SSH 복구 경로 차단)가 된다.
        plan = compiler.compile_plan("f44-r-food-node-firewall-allowlist-omits-order")
        argv, stdin = host_firewall.build_invocation(plan, "run")
        self.assertEqual(argv[:3], ["/usr/bin/bash", "-s", "--"])
        self.assertEqual(argv[3:10], ["run", "192.168.122.14", "tb-w3", "rca-testbed-food",
                                      "testbed-restaurant", "testbed-order", "10.244.2.0/24"])
        # 시나리오 id 는 체인 이름, 주석, 로그 접두어, 상태 파일 어디에도 없다(G6, 원칙 8).
        self.assertFalse(any("F44" in arg or "f44" in arg for arg in argv))
        script = stdin.decode()
        self.assertNotIn("F44-R", script)
        self.assertIn("--ctstate NEW", script)
        run_case = script.split("\n  run)")[1].split(";;")[0]
        self.assertIn('ipt -N "$chain"', run_case)
        self.assertIn('-j RETURN', run_case)
        self.assertIn('-j LOG --log-prefix "$prefix"', run_case)
        self.assertIn('ipt -A "$chain" -j DROP', run_case)
        self.assertIn('ipt -I "${jump[@]:0:1}" 1', run_case)
        # 상태 파일을 규칙보다 먼저 써서 중간에 실패한 run 도 cleanup 이 치운다.
        self.assertLess(run_case.index('mv -T "$state.tmp" "$state"'), run_case.index('ipt -N "$chain"'))
        cleanup_case = script.split("\n  cleanup)")[1].split(";;")[0]
        self.assertIn('ipt -D "${jump[@]}"', cleanup_case)
        self.assertIn('ipt -F "$chain"; ipt -X "$chain"', cleanup_case)
        for forbidden in ("-P FORWARD", "INPUT", "OUTPUT", "REJECT", "-F FORWARD", "conntrack -F", "ufw", "tc qdisc", "sudo bash"):
            self.assertNotIn(forbidden, script)
        # 빠뜨린 호출자(order)가 실제로 허용 목록 밖인지는 preflight 가 살아 있는 파드로 확인한다.
        self.assertIn("allowed_by_list", script.split("\nplacement_ok()")[1].split("\n}")[0])
        params = dict(host_firewall.CONTRACTS["F44-R"])
        params["allowed_sources"] = params["allowed_sources"] + ["10.244.2.0/24"]
        with self.assertRaisesRegex(ExecutorError, "exactly match"):
            host_firewall.validate("F44-R", params, {})
        with self.assertRaisesRegex(ExecutorError, "no verified host firewall contract"):
            host_firewall.validate("F10-H", host_firewall.CONTRACTS["F44-R"], {})
        bad = json.loads(json.dumps(plan))
        for row in bad["profile_instances"]:
            if row["profile_id"] == "host.firewall":
                row["location"] = {**row["location"], "host": "192.168.122.11"}
        with self.assertRaisesRegex(ExecutorError, "measured worker"):
            host_firewall.build_invocation(bad, "run")
        # 같은 노드 호출자(F44-R)는 호출자 네임스페이스, 노드, 대역이 지켜지는 쪽과 같다.
        self.assertEqual(argv[16:], ["rca-testbed-food", "tb-w3", "10.244.2.0/24"])

    def test_host_firewall_cross_node_caller_and_cleanup_without_state(self) -> None:
        # F44-P: 지켜지는 transfer 는 tb-w2, 빠진 호출자 commerce payment 는 다른 노드 tb-w1 의 다른 네임스페이스에
        # 있다. preflight 는 호출자를 자기 노드, 자기 대역에서 찾고 허용 목록 밖인지 본다(지켜지는 대역 안에서
        # 찾으면 다른 노드 호출자는 늘 실패한다). 주입은 F44-R 과 같은 꼴(FORWARD 맨 위 NEW 점프와 체인 하나)이다.
        plan = compiler.compile_plan("f44-p-banking-node-firewall-omits-commerce-settlement")
        argv, stdin = host_firewall.build_invocation(plan, "run")
        self.assertEqual(argv[3:10], ["run", "192.168.122.11", "tb-w2", "rca-testbed-banking",
                                      "testbed-transfer", "testbed-payment", "10.244.1.0/24"])
        self.assertEqual(argv[16:], ["rca-testbed-commerce", "tb-w1", "10.244.3.0/24"])
        self.assertFalse(any("F44" in arg or "f44" in arg for arg in argv))
        script = stdin.decode()
        placement = script.split("\nplacement_ok()")[1].split("\n}")[0]
        self.assertIn('placed "$caller_ns" "$caller" "$caller_node"', placement)
        self.assertIn('in_cidr "$from" "$caller_cidr"', placement)
        self.assertIn("allowed_by_list", placement)
        # 러너 컨테이너가 규칙이 걸린 채 다시 만들어져 상태 파일을 잃어도 cleanup 이 노드의 규칙을 읽어 지운다.
        cleanup_case = script.split("\n  cleanup)")[1].split(";;")[0]
        self.assertNotIn('[[ -e "$state" ]] || exit 0', cleanup_case)
        self.assertLess(cleanup_case.index("r=$(rules)"), cleanup_case.index('ipt -D "${jump[@]}"'))
        contract = host_firewall.CONTRACTS["F44-P"]
        # 빠진 호출자 대역은 허용 목록에 없고, banking 자신의 파드 대역과 진입 노드 대역은 있다.
        self.assertNotIn(contract["omitted_caller_cidr"], contract["allowed_sources"])
        self.assertIn(contract["protected_cidr"], contract["allowed_sources"])
        self.assertIn("10.244.0.0/24", contract["allowed_sources"])
        params = dict(contract)
        params["allowed_sources"] = params["allowed_sources"] + ["10.244.3.0/24"]
        with self.assertRaisesRegex(ExecutorError, "exactly match"):
            host_firewall.validate("F44-P", params, {})

    def test_host_firewall_oracle_listener_allowlist_omits_same_node_app_pods(self) -> None:
        # F72-R: 지켜지는 것은 tb-w2 의 Oracle 리스너(1521)이고, 빠진 호출자는 같은 노드에서 자기 파드 주소로
        # 붙는 banking 앱(대표 transfer, account·ledger 도 같은 대역)이다. 허용 목록은 노드 주소(/32)와 호스트,
        # 관리망뿐이라 앱 파드 대역(10.244.1.0/24)이 통째로 빠지고, NodePort 로 SNAT 되어 오는 DB 감시는 남는다.
        plan = compiler.compile_plan("f72-r-banking-node-firewall-omits-oracle-app-clients")
        argv, stdin = host_firewall.build_invocation(plan, "run")
        self.assertEqual(argv[3:12], ["run", "192.168.122.11", "tb-w2", "rca-testbed-banking",
                                      "testbed-oracle", "testbed-transfer", "10.244.1.0/24", "1521",
                                      "ORACLE-LSNR-IN"])
        # 같은 노드 호출자라 호출자 네임스페이스, 노드, 대역은 지켜지는 쪽과 같다.
        self.assertEqual(argv[16:], ["rca-testbed-banking", "tb-w2", "10.244.1.0/24"])
        self.assertFalse(any("F72" in arg or "f72" in arg for arg in argv))
        self.assertNotIn(b"F72", stdin)
        contract = host_firewall.CONTRACTS["F72-R"]
        self.assertNotIn(contract["protected_cidr"], contract["allowed_sources"])
        self.assertIn("10.244.1.1/32", contract["allowed_sources"])
        # 119 DPM 은 tb-w3 NodePort 로 들어와 tb-w3 flannel 주소로 SNAT 되어 온다.
        self.assertIn("10.244.2.0/32", contract["allowed_sources"])
        self.assertTrue(all(src.endswith("/32") for src in contract["allowed_sources"] if src.startswith("10.244.")))
        params = dict(contract)
        params["allowed_sources"] = params["allowed_sources"] + ["10.244.1.0/24"]
        with self.assertRaisesRegex(ExecutorError, "exactly match"):
            host_firewall.validate("F72-R", params, {})
        with self.assertRaisesRegex(ExecutorError, "exactly match"):
            host_firewall.validate("F72-R", host_firewall.CONTRACTS["F44-P"], {})
        # 규칙 제거 직후 호출자 파드는 풀이 다시 붙기 전까지 NotReady 다. recovery 가 한 번만 보면 효과가 이미
        # 끝난 실행이 DIRTY 로 멈춘다(2026-10-23 F72-R 2회). 규칙 부재는 바로 보고, 파드 Ready 는 기한까지 기다린다.
        recovery_case = stdin.decode().split("\n  recovery)")[1].split(";;")[0]
        self.assertLess(recovery_case.index('if ours "$r"; then exit 1; fi'), recovery_case.index("until placed"))
        self.assertIn("deadline=$((SECONDS + 300))", recovery_case)
        self.assertIn("sleep 5", recovery_case)

    def test_k8s_quota_only_adds_one_namespace_quota_and_a_routine_restart(self) -> None:
        # F50-R: 주입은 네임스페이스 ResourceQuota 하나를 만들고 transfer 를 일상 재배포하는 것뿐이다.
        # 이미지, 프로브, env, 자원 한도를 바꾸면 F17-H, F17-R, F42-R 의 지문(바뀐 파드 템플릿)이 섞이고,
        # 떠 있는 파드를 지우면 할당량이 아니라 삭제가 원인이 된다.
        plan = compiler.compile_plan("f50-r-banking-namespace-quota-blocks-transfer-rollout")
        argv, stdin = k8s_quota.build_invocation(plan, "run")
        self.assertEqual(argv[0], "/usr/bin/bash")
        self.assertEqual(argv[3:], ["run", "rca-testbed-banking", "compute-resources",
                                    "requests.memory", "4Gi", "testbed-transfer"])
        # 시나리오 id 는 kubectl 이쪽에 머문다(G6, 원칙 8). 상태 파일은 네임스페이스와 할당량 이름으로 짓는다.
        self.assertNotIn("F50-R", argv)
        script = stdin.decode()
        self.assertNotIn("F50-R", script)
        run_case = script.split("\n  run)")[1].split(";;")[0]
        self.assertIn('create quota "$quota" --hard="$resource=$hard"', run_case)
        self.assertIn('rollout restart deploy "$deploy"', run_case)
        # 할당량이 사용량을 센 뒤에 재배포해야 거절 사유가 'status unknown' 이 아니라 'exceeded quota' 다.
        self.assertLess(run_case.index("quota_counted"), run_case.index("rollout restart"))
        cleanup_case = script.split("\n  cleanup)")[1].split(";;")[0]
        self.assertIn('delete resourcequota "$quota"', cleanup_case)
        self.assertIn('healthy "$cleanup_wait"', cleanup_case)
        self.assertIn("\ncleanup_wait=420s\n", script)
        preflight = script.split("\ncheck() {")[1].split("\n}")[0]
        self.assertIn("! any_quota", preflight)
        self.assertIn("replace_first", preflight)
        self.assertIn("$u > $h", preflight)
        for forbidden in ("set image", "delete pod", "delete deploy", "scale", "patch deploy \"$deploy\"", "evict"):
            self.assertNotIn(forbidden, script)
        params = dict(k8s_quota.CONTRACTS["F50-R"])
        params["hard"] = "8Gi"
        with self.assertRaisesRegex(ExecutorError, "exactly match"):
            k8s_quota.validate("F50-R", params, {})
        with self.assertRaisesRegex(ExecutorError, "no verified resource quota contract"):
            k8s_quota.validate("F17-H", k8s_quota.CONTRACTS["F50-R"], {})

    def test_host_image_only_removes_node_images_and_restarts_one_deployment(self) -> None:
        # F52-R: 주입은 워커 노드 저장소에서 쓰고 있는 앱 이미지를 지우는 정리(이미지마다 sudo ctr images rm)와
        # transfer 일상 재배포뿐이다. 파드 템플릿(이미지 태그, 프로브, env, 자원)을 바꾸면 F17-H, F17-R, F42-R 의
        # 지문이 섞이고, 파드를 지우면 이미지가 아니라 삭제가 원인이 된다.
        plan = compiler.compile_plan("f52-r-banking-node-image-retention-removes-live-transfer-image")
        argv, stdin = host_image.build_invocation(plan, "run")
        self.assertEqual(argv[0], "/usr/bin/bash")
        self.assertEqual(argv[3:], [
            "run", "192.168.122.11", "tb-w2", "rca-testbed-banking",
            "core-banking-api:latest,core-banking-account:latest,core-banking-transfer:latest,core-banking-ledger:latest",
            "api-service,account-service,transfer-service,ledger-service",
            "testbed-transfer", "transfer-service",
        ])
        # 시나리오 id 는 러너 쪽에 머문다(G6, 원칙 8). 상태 파일은 노드와 네임스페이스 이름으로 짓는다.
        self.assertNotIn("F52-R", argv)
        script = stdin.decode()
        self.assertNotIn("F52-R", script)
        run_case = script.split("\n  run)")[1].split(";;")[0]
        self.assertIn("sudo -n ctr -n k8s.io images rm $refs", run_case)
        self.assertIn('rollout restart deploy "$deploy"', run_case)
        # 상태 파일을 먼저 쓰고, 모든 이미지가 노드에서 사라진 것을 확인한 뒤에 재배포한다.
        self.assertLess(run_case.index('"$state.tmp"'), run_case.index("images rm"))
        self.assertLess(run_case.index("images rm"), run_case.index("rollout restart"))
        cleanup_case = script.split("\n  cleanup)")[1].split(";;")[0]
        self.assertIn("restore_one", cleanup_case)
        self.assertIn('healthy "$cleanup_wait"', cleanup_case)
        self.assertIn("\ncleanup_wait=420s\n", script)
        restore = script.split("\nrestore_one() {")[1].split("\n}")[0]
        self.assertIn('docker save "$img" | node_sh sudo -n ctr -n k8s.io images import -', restore)
        # 지울 때의 이미지 ID 를 지우기 전에 적는다.
        self.assertLess(run_case.index('>>"$state.tmp"'), run_case.index("images rm"))
        preflight = script.split("\ncheck() {")[1].split("\n}")[0]
        for guard in ("never_pull", "replace_first", "on_node", "local_build", "same_image", "healthy 1s"):
            self.assertIn(guard, preflight)
        for forbidden in ("set image", "set env", "delete pod", "delete deploy", "scale deploy", "evict", "crictl rmi", "--prune"):
            self.assertNotIn(forbidden, script)
        params = dict(host_image.CONTRACTS["F52-R"])
        params["containers"] = ["transfer-service"]
        with self.assertRaisesRegex(ExecutorError, "exactly match"):
            host_image.validate("F52-R", params, {})
        params = dict(host_image.CONTRACTS["F52-R"])
        params["images"] = ["core-banking-transfer:latest"]
        with self.assertRaisesRegex(ExecutorError, "exactly match"):
            host_image.validate("F52-R", params, {})
        with self.assertRaisesRegex(ExecutorError, "no verified node image retention contract"):
            host_image.validate("F17-H", host_image.CONTRACTS["F52-R"], {})

    def _host_image_stub(self, tmp: Path, node: dict[str, str], docker: dict[str, str],
                         running: dict[str, str]) -> tuple[Path, dict[str, str]]:
        """Fake ssh/docker/kubectl around the real script: the node store is a file of
        "<ref> <id>" lines, crictl ps reports the given running containers' imageRefs."""
        bindir = tmp / "bin"
        bindir.mkdir()
        store = tmp / "store"
        store.write_text("".join(f"docker.io/library/{name} {iid}\n" for name, iid in node.items()))
        (tmp / "docker_ids").write_text("".join(f"{name} {iid}\n" for name, iid in docker.items()))
        (tmp / "running").write_text(json.dumps({"containers": [
            {"metadata": {"name": c}, "imageRef": r, "state": "CONTAINER_RUNNING",
             "labels": {"io.kubernetes.pod.namespace": "rca-testbed-banking"}} for c, r in running.items()]}))
        deploy = json.dumps({
            "metadata": {"generation": 1},
            "spec": {"replicas": 1, "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1}},
                     "template": {"spec": {"nodeSelector": {"kubernetes.io/hostname": "tb-w2"},
                                           "containers": [{"name": "transfer-service", "image": "core-banking-transfer:latest",
                                                           "imagePullPolicy": "Never"}]}}},
            "status": {"observedGeneration": 1, "updatedReplicas": 1, "availableReplicas": 1, "replicas": 1}})
        (tmp / "deploy.json").write_text(deploy)
        scripts = {
            "ssh": f"""#!/usr/bin/env bash
while [[ "$1" == -* ]]; do [[ "$1" == -i || "$1" == -o ]] && shift; shift; done
shift  # nkia@host
cmd="$*"
case "$cmd" in
  *"images ls -q"*) awk '{{print $1; print $2}}' {store} ;;
  *"images rm"*) for r in ${{cmd#*images rm }}; do grep -v -F -e "$r" {store} >{store}.n || true; mv {store}.n {store}; done ;;
  *"images import"*) name=$(cat); echo "docker.io/library/$name $(awk -v n="$name" '$1==n{{print $2}}' {tmp}/docker_ids)" >>{store} ;;
  *"crictl inspecti"*) id=$(awk -v r="${{cmd##* }}" '$1==r{{print $2}}' {store}); printf '{{"status":{{"id":"%s","repoDigests":[]}}}}' "$id" ;;
  *"crictl ps"*) cat {tmp}/running ;;
  *) exit 9 ;;
esac
""",
            "docker": f"""#!/usr/bin/env bash
case "$1" in
  image) awk -v n="${{@: -1}}" '$1==n{{print $2}}' {tmp}/docker_ids ;;
  save) printf '%s' "$2" ;;
esac
""",
            "kubectl": f"""#!/usr/bin/env bash
case "$*" in
  *"auth can-i"*) echo yes ;;
  *"get deploy"*) cat {tmp}/deploy.json ;;
  *"rollout restart"*) echo restart >>{tmp}/restarts ;;
esac
""",
        }
        for name, body in scripts.items():
            path = bindir / name
            path.write_text(body)
            path.chmod(0o755)
        env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", SCENARIO_PROFILE_STATE_ROOT=str(tmp / "state"))
        return store, env

    def _host_image_run(self, action: str, env: dict[str, str]) -> subprocess.CompletedProcess:
        plan = compiler.compile_plan("f52-r-banking-node-image-retention-removes-live-transfer-image")
        argv, stdin = host_image.build_invocation(plan, action)
        return subprocess.run(argv, input=stdin, env=env, capture_output=True, timeout=60, check=False)

    def test_host_image_cleanup_restores_only_the_build_the_node_held(self) -> None:
        # F52-R: cleanup 은 run 이 적어 둔 이미지 ID 와 같은 빌드만 되돌린다. 그 사이 109 docker 의 :latest 가
        # 다시 빌드되었으면(평소 배포가 하는 일) 아무것도 넣지 않고 실패해야 한다. 새 빌드를 넣으면 앱 버전이 조용히 바뀐다.
        ids = {f"core-banking-{s}:latest": f"sha256:{c * 64}" for s, c in
               (("api", "a"), ("account", "b"), ("transfer", "c"), ("ledger", "d"))}
        running = {"api-service": ids["core-banking-api:latest"], "account-service": ids["core-banking-account:latest"],
                   "transfer-service": ids["core-banking-transfer:latest"], "ledger-service": ids["core-banking-ledger:latest"]}
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            store, env = self._host_image_stub(tmp, dict(ids), dict(ids), running)
            self.assertEqual(self._host_image_run("run", env).returncode, 0)
            state = (tmp / "state" / "tb-w2-rca-testbed-banking-images").read_text().split("\n")
            self.assertIn(f"core-banking-transfer:latest {ids['core-banking-transfer:latest']}", state)
            self.assertNotIn("core-banking-transfer", store.read_text())
            # 109 의 transfer :latest 가 다시 빌드됨
            (tmp / "docker_ids").write_text((tmp / "docker_ids").read_text().replace("c" * 64, "e" * 64))
            result = self._host_image_run("cleanup", env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"not loading a different build", result.stderr)
            self.assertNotIn("e" * 64, store.read_text())
            # 같은 빌드가 돌아오면 cleanup 이 넷을 되돌리고 상태 파일을 지운다.
            (tmp / "docker_ids").write_text((tmp / "docker_ids").read_text().replace("e" * 64, "c" * 64))
            self.assertEqual(self._host_image_run("cleanup", env).returncode, 0)
            for name, iid in ids.items():
                self.assertIn(f"docker.io/library/{name} {iid}", store.read_text())
            self.assertFalse((tmp / "state" / "tb-w2-rca-testbed-banking-images").exists())
            self.assertEqual(self._host_image_run("recovery", env).returncode, 0)

    def test_host_image_cleanup_without_state_uses_running_containers_or_fails(self) -> None:
        # 상태 파일이 없으면(러너 컨테이너가 다시 만들어짐) 기대 ID 는 노드에서 그 이미지로 아직 도는 컨테이너의
        # imageRef 다. 도는 컨테이너가 없는 이미지(재배포로 옛 파드가 내려간 transfer)는 넣지 않고 실패한다.
        ids = {f"core-banking-{s}:latest": f"sha256:{c * 64}" for s, c in
               (("api", "a"), ("account", "b"), ("transfer", "c"), ("ledger", "d"))}
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            running = {"api-service": ids["core-banking-api:latest"], "account-service": ids["core-banking-account:latest"],
                       "ledger-service": ids["core-banking-ledger:latest"]}
            store, env = self._host_image_stub(tmp, {}, dict(ids), running)
            result = self._host_image_run("cleanup", env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"no known image ID for core-banking-transfer:latest", result.stderr)
            self.assertNotIn("core-banking-transfer", store.read_text())
        # api 의 도는 컨테이너와 109 빌드가 다르면 그 이미지도 넣지 않는다.
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            node = {k: v for k, v in ids.items() if k != "core-banking-api:latest"}
            docker = dict(ids, **{"core-banking-api:latest": "sha256:" + "f" * 64})
            store, env = self._host_image_stub(tmp, node, docker, {"api-service": ids["core-banking-api:latest"]})
            result = self._host_image_run("cleanup", env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"not loading a different build", result.stderr)
            # recovery 는 이름만이 아니라 도는 컨테이너와 같은 ID 인지 본다.
            store.write_text(store.read_text() + f"docker.io/library/core-banking-api:latest {'f' * 64}\n")
            self.assertNotEqual(self._host_image_run("recovery", env).returncode, 0)

    def test_host_job_only_starts_one_backup_unit_and_removes_only_its_output(self) -> None:
        # F80-R: 주입은 노드에서 일상 백업 작업 하나(systemd 일시 유닛)를 띄우는 것뿐이다. 파드, 매니페스트, 이미지,
        # 설정을 건드리면 F25-H, F63-R 같은 다른 원인의 지문이 섞이고, 메모리를 직접 점유하면(F05-P memhog, F15-P
        # stress-ng) 계기의 흔적(백업 유닛 시작, zstd 와 tar)이 사라진다.
        plan = compiler.compile_plan("f80-r-banking-oradata-backup-zstd-exhausts-node-memory")
        argv, stdin = host_job.build_invocation(plan, "run")
        self.assertEqual(argv[0], "/usr/bin/bash")
        self.assertEqual(argv[3:], [
            "run", "192.168.122.11", "tb-w2", "rca-testbed-banking", "oracledata-testbed-oracle-0", "FREE",
            "FREE/FREEPDB1/users01.dbf", "2048", "oradata-backup", "Oracle datafile backup", "/var/backups/oradata",
            "12", "31", "10", "600", "20480", "1536", "3584", "768", "256",
            "testbed-nginx,testbed-api,testbed-account,testbed-transfer,testbed-ledger", "testbed-oracle",
        ])
        # 시나리오 id 는 러너 쪽에 머문다(G6, 원칙 8). 노드의 sudo 기록에 남는 것은 백업 명령뿐이다.
        self.assertNotIn("F80-R", argv)
        script = stdin.decode()
        self.assertNotIn("F80-R", script)
        run_case = script.split("\n  run)")[1].split(";;")[0]
        self.assertIn("sudo -n systemd-run", run_case)
        for flag in ('--unit="$unit"', "--collect", "--property=RuntimeMaxSec=", "--property=Nice=", "IOSchedulingClass=idle",
                     '--property=MemoryHigh="${high_mib}M"', '--property=MemoryMax="${max_mib}M"'):
            self.assertIn(flag, run_case)
        # 2026-10-17(F80-R-run-bad63a76): 상한 없는 작업이 노드를 헛돌게 해 NotReady, SSH 불통까지 갔다. 유닛의 메모리 상한은
        # 시작 때 여유에서 노드 몫을 뺀 값이라 노드가 살아 증거(노드 에이전트 수집)가 남는다.
        self.assertIn("high_mib=$(( avail_mib - reserve_mib )); max_mib=$(( high_mib + margin_mib ))", run_case)
        # 두 흐름을 나란히: 가장 큰 데이터 파일 하나, 나머지 디렉터리 tar 하나.
        self.assertIn("tar -cf - --exclude=%q %q | zstd -q -%d --long=%d -T1 -o %q & zstd -q -%d --long=%d -T1 -o %q %q; wait;", run_case)
        # 작업이 쓰는 곳은 preflight 가 없음을 확인한 출력 디렉터리 하나이고, cleanup 은 그 안의 백업 파일과
        # 디렉터리만 지운다.
        cleanup_case = script.split("\n  cleanup)")[1].split(";;")[0]
        self.assertIn('sudo -n systemctl stop "$unit.service"', cleanup_case)
        self.assertIn("-name '*.zst' -delete", cleanup_case)
        self.assertIn('sudo -n rmdir "$outdir"', cleanup_case)
        self.assertIn('healthy "$cleanup_wait"', cleanup_case)
        preflight = script.split("\ncheck() {")[1].split("\n}")[0]
        for guard in ("datadir", "largest_min_mib", "min_free_mib", "max_avail_mib", "min_avail_mib", "healthy 1s", "spec.nodeName"):
            self.assertIn(guard, preflight)
        for forbidden in ("rm -rf", "rm -r ", "drop_caches", "delete pod", "set image", "set env", "rollout restart",
                          "scale deploy", "drain", "cordon", "stress-ng", "python3", "fallocate"):
            self.assertNotIn(forbidden, script)
        for change in ({"level": 19}, {"long_window_log": 27}, {"output_dir": "/var/lib/oradata"},
                       {"runtime_seconds": 3600}, {"deployments": ["testbed-transfer"]}, {"max_available_mib": 8192},
                       {"memory_reserve_mib": 0}, {"memory_hard_margin_mib": 4096}):
            with self.assertRaisesRegex(ExecutorError, "exactly match"):
                host_job.validate("F80-R", {**host_job.CONTRACTS["F80-R"], **change}, {})
        with self.assertRaisesRegex(ExecutorError, "no verified host maintenance job contract"):
            host_job.validate("F15-P", host_job.CONTRACTS["F80-R"], {})
        # 상한의 모양 자체도 계약이다: 노드 몫이 preflight 최소 여유보다 작아야 MemoryHigh 가 양수이고, 단단한 여유분이
        # 노드 몫을 다 먹으면 안 된다.
        for change in ({"memory_reserve_mib": 1536}, {"memory_hard_margin_mib": 768}, {"memory_hard_margin_mib": 0}):
            bad = {**host_job.CONTRACTS["F80-R"], **change}
            with unittest.mock.patch.dict(host_job.CONTRACTS, {"F80-R": bad}):
                with self.assertRaisesRegex(ExecutorError, "memory bound"):
                    host_job.validate("F80-R", bad, {})

    def _host_job_stub(self, tmp: Path) -> dict[str, str]:
        """Fake ssh/kubectl around the real script. The node is two flag files (unit active,
        output directory present); every remote command is unquoted and logged one argument per line.
        The node's own undo (`sudo -n bash -s`) runs for real against the fake systemd of
        node_guard_fakes."""
        bindir = tmp / "bin"
        bindir.mkdir()
        node = tmp / "node"
        node.mkdir()
        log = tmp / "log"
        scripts = {
            "ssh": f"""#!/usr/bin/env bash
while [[ "$1" == -* ]]; do [[ "$1" == -i || "$1" == -o ]] && shift; shift; done
shift  # nkia@host
cmd="$*"
# The node not answering: {node}/down holds how many more connections time out (ssh exits 255).
if [[ -s {node}/down ]] && (( $(cat {node}/down) > 0 )); then
  echo $(( $(cat {node}/down) - 1 )) >{node}/down
  echo "Connection timed out during banner exchange" >&2; exit 255
fi
if [[ -e {node}/drop_is_active && "$cmd" == *"is-active"* ]]; then
  echo "Connection to 192.168.122.11 port 22 timed out" >&2; exit 255
fi
eval "set -- $cmd"
printf '%s\\n' "--- $1" "$@" >>{log}
case "$cmd" in
  "sudo -n bash -s") exec bash -s ;;
  true) : ;;
  *"systemctl is-active"*) [[ -e {node}/active ]] ;;
  "sh -c"*) echo /usr/bin/tar; echo /usr/bin/zstd; echo /usr/bin/systemd-run ;;
  "test "*) [[ ! -e {node}/outdir ]] ;;
  *"stat -c"*) echo 5369765888 ;;
  "df "*) echo 80000 ;;
  *"MemAvailable"*) cat {node}/memavail 2>/dev/null || echo 2700000 ;;
  *"systemd-run"*) touch {node}/active {node}/outdir ;;
  *"systemctl stop"*) rm -f {node}/active ;;
  *"find "*) : ;;
  *"rmdir"*) rm -f {node}/outdir ;;
  *) exit 9 ;;
esac
""",
            # Backoff waits are recorded, not slept.
            "sleep": f"""#!/usr/bin/env bash
echo "$1" >>{tmp}/sleeps
""",
            "kubectl": f"""#!/usr/bin/env bash
case "$*" in
  *"get pvc"*) printf 'pvc-x' ;;
  *"get pv pvc-x"*) echo '{{"spec":{{"local":{{"path":"/opt/local-path-provisioner/pvc-x_rca-testbed-banking_oracledata-testbed-oracle-0"}},"nodeAffinity":{{"required":{{"nodeSelectorTerms":[{{"matchExpressions":[{{"key":"kubernetes.io/hostname","operator":"In","values":["tb-w2"]}}]}}]}}}}}}}}' ;;
  *"get pod testbed-oracle-0"*) printf 'tb-w2' ;;
  *"get deploy"*) echo '{{"metadata":{{"generation":1}},"spec":{{"replicas":1}},"status":{{"observedGeneration":1,"updatedReplicas":1,"availableReplicas":1}}}}' ;;
  *"get sts"*) echo '{{"spec":{{"replicas":1}},"status":{{"readyReplicas":1}}}}' ;;
  *) exit 9 ;;
esac
""",
        }
        for name, body in scripts.items():
            path = bindir / name
            path.write_text(body)
            path.chmod(0o755)
        return dict(os.environ, **node_guard_fakes.install(tmp), PATH=f"{bindir}:{os.environ['PATH']}")

    @staticmethod
    def _host_job_calls(tmp: Path) -> list[list[str]]:
        calls: list[list[str]] = []
        for line in (tmp / "log").read_text().split("\n"):
            if line.startswith("--- "):
                calls.append([])
            elif line and calls:
                calls[-1].append(line)
        return calls

    def _host_job_run(self, action: str, env: dict[str, str]) -> subprocess.CompletedProcess:
        plan = compiler.compile_plan("f80-r-banking-oradata-backup-zstd-exhausts-node-memory")
        argv, stdin = host_job.build_invocation(plan, action)
        return subprocess.run(argv, input=stdin, env=env, capture_output=True, timeout=60, check=False)

    def test_host_job_runs_the_backup_as_one_unit_and_cleanup_undoes_exactly_it(self) -> None:
        # F80-R: run 이 띄우는 것은 백업 유닛 하나이고 그 명령은 실제 Oracle 볼륨 디렉터리를 tar 로 묶어 zstd 장거리
        # 모드로 두 흐름 나란히 압축한다. 이미 유닛이 돌거나 출력 디렉터리가 있으면 preflight 와 run 은 아무것도 하지
        # 않고 실패한다.
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = self._host_job_stub(tmp)
            # 노드 여유가 작업(약 4.6 GiB)보다 1 GiB 이상 작을 때만, 이미 압박 중이 아닐 때만 주입한다.
            for kib in ("3800000", "1400000"):
                (tmp / "node" / "memavail").write_text(kib + "\n")
                self.assertNotEqual(self._host_job_run("preflight", env).returncode, 0)
                self.assertNotEqual(self._host_job_run("run", env).returncode, 0)
            self.assertFalse((tmp / "node" / "active").exists())
            (tmp / "node" / "memavail").write_text("2764800\n")
            self.assertEqual(self._host_job_run("preflight", env).returncode, 0)
            result = self._host_job_run("run", env)
            self.assertEqual(result.returncode, 0, result.stderr)
            started = [c for c in self._host_job_calls(tmp) if c[:3] == ["sudo", "-n", "systemd-run"]]
            self.assertEqual(len(started), 1)
            started = started[0]
            self.assertEqual(started[3], "--unit=oradata-backup")
            self.assertIn("--description=Oracle datafile backup", started)
            self.assertIn("--property=RuntimeMaxSec=600", started)
            # 여유 2,700 MiB 에서 노드 몫 768 을 남긴 1,932 MiB 가 MemoryHigh, 그 256 위가 MemoryMax 다.
            self.assertIn("--property=MemoryHigh=1932M", started)
            self.assertIn("--property=MemoryMax=2188M", started)
            inner = started[started.index("-c") + 1]
            self.assertRegex(inner, r"^mkdir -p /var/backups/oradata && cd /opt/local-path-provisioner/"
                                    r"pvc-x_rca-testbed-banking_oracledata-testbed-oracle-0 && \{ "
                                    r"tar -cf - --exclude=FREE/FREEPDB1/users01\.dbf FREE \| "
                                    r"zstd -q -12 --long=31 -T1 -o /var/backups/oradata/oradata-[0-9]{8}\.tar\.zst & "
                                    r"zstd -q -12 --long=31 -T1 -o /var/backups/oradata/users01-[0-9]{8}\.dbf\.zst "
                                    r"FREE/FREEPDB1/users01\.dbf; wait; \}$")
            # 두 번째 run 과 preflight 는 돌고 있는 유닛 앞에서 멈춘다.
            self.assertNotEqual(self._host_job_run("preflight", env).returncode, 0)
            self.assertNotEqual(self._host_job_run("run", env).returncode, 0)
            self.assertEqual(len([c for c in self._host_job_calls(tmp) if c[:3] == ["sudo", "-n", "systemd-run"]]), 1)
            # 회복 전에는 recovery 가 실패하고, cleanup 뒤에는 유닛도 출력도 없다.
            self.assertNotEqual(self._host_job_run("recovery", env).returncode, 0)
            self.assertEqual(self._host_job_run("cleanup", env).returncode, 0)
            self.assertFalse((tmp / "node" / "active").exists())
            self.assertFalse((tmp / "node" / "outdir").exists())
            calls = self._host_job_calls(tmp)
            self.assertIn(["sudo", "-n", "find", "/var/backups/oradata", "-maxdepth", "1", "-type", "f",
                           "-name", "*.zst", "-delete"], calls)
            self.assertIn(["sudo", "-n", "rmdir", "/var/backups/oradata"], calls)
            self.assertIn(["sudo", "-n", "systemctl", "stop", "oradata-backup.service"], calls)
            self.assertEqual(self._host_job_run("recovery", env).returncode, 0)
            # 노드 메모리가 돌아오지 않았으면 회복이 아니다.
            (tmp / "node" / "memavail").write_text("500000\n")
            self.assertNotEqual(self._host_job_run("recovery", env).returncode, 0)

    def test_host_job_cleanup_waits_for_an_unreachable_node_and_never_reads_ssh_loss_as_idle(self) -> None:
        # 2026-10-17 F80-R-run-bad63a76: abort 직후 cleanup 이 아직 메모리에 눌린 노드에 닿지 못했다(SSH banner 시간 초과).
        # 첫 실패에서 멈추지 말고 정해진 백오프로 기다린다. 또 SSH 실패(255)를 "유닛 없음, 출력 없음"으로 읽으면 안 된다.
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = self._host_job_stub(tmp)
            node = tmp / "node"
            (node / "memavail").write_text("2764800\n")
            self.assertEqual(self._host_job_run("run", env).returncode, 0)
            # 노드가 세 번 응답하지 않다가 돌아온다: cleanup 은 기다렸다가 유닛을 멈추고 출력을 지운다.
            (node / "down").write_text("3\n")
            result = self._host_job_run("cleanup", env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((node / "active").exists())
            self.assertFalse((node / "outdir").exists())
            self.assertEqual((tmp / "sleeps").read_text().split()[:3], ["5", "10", "15"])
            self.assertIn(["sudo", "-n", "systemctl", "stop", "oradata-backup.service"], self._host_job_calls(tmp))
            # 노드가 끝내 돌아오지 않으면 백오프(합계 230초)를 다 쓴 뒤 실패하고, 노드에는 아무 명령도 보내지 않는다.
            self.assertEqual(self._host_job_run("run", env).returncode, 0)
            (tmp / "sleeps").unlink()
            before = len(self._host_job_calls(tmp))
            (node / "down").write_text("1000\n")
            result = self._host_job_run("cleanup", env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"unreachable over ssh", result.stderr)
            self.assertIn(b"banner exchange", result.stderr)
            self.assertEqual(sum(int(x) for x in (tmp / "sleeps").read_text().split()), 230)
            self.assertEqual(len(self._host_job_calls(tmp)), before)
            self.assertTrue((node / "active").exists())
            # 연결이 된 뒤에도 질의 하나가 SSH 에서 끊기면 그것을 "유닛이 멈춤"으로 읽지 않고 실패한다.
            (node / "down").write_text("0\n")
            (node / "drop_is_active").touch()
            for action in ("cleanup", "recovery"):
                result = self._host_job_run(action, env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"ssh to 192.168.122.11 failed during: systemctl is-active", result.stderr)
            self.assertNotIn(["sudo", "-n", "rmdir", "/var/backups/oradata"], self._host_job_calls(tmp)[before:])
            self.assertTrue((node / "outdir").exists())

    # U8 node self-recovery (executor_common guard). 2026-10-17 F80-R-run-bad63a76: cleanup could not
    # reach tb-w2 in NotReady and the runner stopped DIRTY with the job's output on the node. Every
    # executor that changes a node now arms, before the change, a timer on the node that undoes it by
    # itself; cleanup postpones it when it reaches the node and cancels it when done.

    def test_host_job_arms_the_node_undo_before_the_job_and_cleanup_cancels_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = self._host_job_stub(tmp)
            (tmp / "node" / "memavail").write_text("2764800\n")
            result = self._host_job_run("run", env)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = self._host_job_calls(tmp)
            guard = calls.index(["sudo", "-n", "bash", "-s"])
            job = next(i for i, c in enumerate(calls) if c[:3] == ["sudo", "-n", "systemd-run"])
            self.assertLess(guard, job, "the undo is armed before the job starts")
            # F80-R max_injection_duration 20m + 600s margin: never before the runner itself ends the run.
            self.assertEqual(node_guard_fakes.armed_seconds(env, "oradata-backup"), [1800])
            self.assertEqual(node_guard_fakes.journal_words(env, "oradata-backup"), [])
            undo = (tmp / "state" / "oradata-backup.undo").read_text()
            outputs = re.findall(r"/var/backups/oradata/(?:oradata-[0-9]{8}\.tar|users01-[0-9]{8}\.dbf)\.zst", undo)
            self.assertEqual(len(outputs), 2)
            self.assertIn("systemctl stop oradata-backup.service 2>/dev/null || true", undo)
            self.assertIn("rmdir /var/backups/oradata 2>/dev/null || [[ ! -e /var/backups/oradata ]]", undo)
            for forbidden in ("rm -rf", "rm -r ", "find ", "F80"):
                self.assertNotIn(forbidden, undo)
            # The outputs survive a reboot, so the next boot removes exactly them (files before the directory).
            boot = Path(node_guard_fakes.unit_record(env, "oradata-backup")[2]).read_text().splitlines()
            self.assertEqual(boot[:3], [f"r! {outputs[0]}", f"r! {outputs[1]}", "r! /var/backups/oradata"])
            # Nothing on the node's sudo line names the scenario or the guard's arguments.
            self.assertNotIn(b"F80", (tmp / "log").read_bytes())
            result = self._host_job_run("cleanup", env)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = self._host_job_calls(tmp)[len(calls):]
            guards = [i for i, c in enumerate(calls) if c == ["sudo", "-n", "bash", "-s"]]
            stop = calls.index(["sudo", "-n", "systemctl", "stop", "oradata-backup.service"])
            rmdir = calls.index(["sudo", "-n", "rmdir", "/var/backups/oradata"])
            self.assertEqual(len(guards), 2)
            self.assertLess(guards[0], stop, "postponed before cleanup touches the job")
            self.assertGreater(guards[1], rmdir, "cancelled only after the job and its output are gone")
            # On time, the postponement restarts nothing (no extra journal lines); disarm cancels.
            self.assertEqual(node_guard_fakes.armed_seconds(env, "oradata-backup"), [1800])
            self.assertEqual(node_guard_fakes.timers(env), [])
            self.assertEqual(sorted(x.name for x in (tmp / "state").iterdir()), [])
            self.assertEqual(list((tmp / "tmpfiles.d").iterdir()), [])
            self.assertNotIn(b"by itself", result.stderr)

    def _host_job_node_fakes(self, tmp: Path) -> dict[str, str]:
        """What the node's own undo meets when it runs as root on tb-w2: systemctl stops the job
        unit (the stub's 'active' flag), and the output directory is the stub's 'outdir' flag."""
        node = tmp / "node"
        fakebin = tmp / "undo-bin"
        fakebin.mkdir()
        scripts = {
            "rm": f"""#!/usr/bin/env bash
flags=(); real=()
for a in "$@"; do case "$a" in -*) flags+=("$a");; /var/backups/oradata/*) echo "rm $a" >>{node}/undo-log;; *) real+=("$a");; esac; done
(( ${{#real[@]}} == 0 )) || exec /bin/rm "${{flags[@]}}" "${{real[@]}}"
""",
            "rmdir": f"""#!/usr/bin/env bash
if [[ "$1" == /var/backups/oradata ]]; then
  echo "rmdir $1" >>{node}/undo-log
  [[ -e {node}/outdir ]] || exit 1
  /bin/rm -f {node}/outdir
  exit 0
fi
exec /bin/rmdir "$@"
""",
            "stop-hook": f"""#!/usr/bin/env bash
if [[ "$1" == oradata-backup.service ]]; then /bin/rm -f {node}/active; fi
""",
        }
        for name, body in scripts.items():
            (fakebin / name).write_text(body)
            (fakebin / name).chmod(0o755)
        guard_bin = Path(node_guard_fakes.install(tmp)["OPSJOB_SYSTEMCTL"]).parent
        return {"PATH": f"{guard_bin}:{fakebin}:{os.environ['PATH']}", "FAKE_STOP_HOOK": str(fakebin / "stop-hook")}

    def test_host_job_node_undoes_the_backup_by_itself_when_cleanup_never_reaches_it(self) -> None:
        # The sandbox's /var/backups/oradata stands for the node's; the real host must not have one.
        self.assertFalse(Path("/var/backups/oradata").exists())
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = self._host_job_stub(tmp)
            node = tmp / "node"
            (node / "memavail").write_text("2764800\n")
            self.assertEqual(self._host_job_run("run", env).returncode, 0)
            # The runner loses the node (F80-R-run-bad63a76): cleanup fails after its backoff and
            # never reaches the node, so the timer is neither postponed nor cancelled.
            (node / "down").write_text("1000\n")
            self.assertNotEqual(self._host_job_run("cleanup", env).returncode, 0)
            self.assertEqual(node_guard_fakes.armed_seconds(env, "oradata-backup"), [1800])
            self.assertEqual(len(node_guard_fakes.timers(env)), 1)
            self.assertTrue((node / "active").exists())
            undo = tmp / "state" / "oradata-backup.undo"
            spare = tmp / "undo-copy"
            spare.write_bytes(undo.read_bytes())
            # The deadline passes: PID 1 runs the undo; the job stops and its outputs and directory go.
            extra = self._host_job_node_fakes(tmp)
            fired = node_guard_fakes.fire(env, "oradata-backup", extra_env=extra)
            self.assertEqual(fired.returncode, 0, fired.stderr)
            self.assertFalse((node / "active").exists())
            self.assertFalse((node / "outdir").exists())
            log = (node / "undo-log").read_text().splitlines()
            self.assertEqual([line.split()[0] for line in log], ["rm", "rm", "rmdir"])
            self.assertEqual(sorted(x.name for x in (tmp / "state").iterdir()), ["oradata-backup.expired"])
            self.assertEqual(list((tmp / "tmpfiles.d").iterdir()), [])
            # Idempotent: run twice more, it changes nothing and does not fail.
            for _ in range(2):
                again = subprocess.run(["/bin/bash", str(spare)], env={**os.environ, **env, **extra},
                                       capture_output=True, timeout=60)
                self.assertEqual(again.returncode, 0, again.stderr)
                self.assertFalse((node / "active").exists())
                self.assertFalse((node / "outdir").exists())
            # The node answers again: cleanup finds nothing left, says when the node undid it, and the
            # node counts as recovered.
            (node / "down").write_text("0\n")
            late = self._host_job_run("cleanup", env)
            self.assertEqual(late.returncode, 0, late.stderr)
            self.assertIn(b"the node undid the change by itself at ", late.stderr)
            self.assertNotIn(["sudo", "-n", "systemctl", "stop", "oradata-backup.service"],
                             self._host_job_calls(tmp)[-12:])
            self.assertEqual(self._host_job_run("recovery", env).returncode, 0)
            self.assertEqual(sorted(x.name for x in (tmp / "state").iterdir()), [])

    def _host_firewall_stub(self, tmp: Path) -> dict[str, str]:
        """tb-ops side of host.firewall with the node in a file: kubectl answers with the F44-R
        placement, ssh runs the remote command here, and iptables keeps its rules as JSON."""
        bindir = tmp / "bin"
        bindir.mkdir()
        scripts = {
            "ssh": f"""#!/usr/bin/env bash
while [[ "$1" == -* ]]; do [[ "$1" == -i || "$1" == -o ]] && shift; shift; done
shift
cmd="$*"
printf '%s\\n' "$cmd" >>{tmp}/ssh-log
eval "set -- $cmd"
if [[ "$1" == sudo ]]; then shift; [[ "$1" == -n ]] && shift; fi
exec "$@"
""",
            "sysctl": "#!/usr/bin/env bash\necho 1\n",
            "iptables": f"""#!/usr/bin/env python3
import json, os, sys
path = "{tmp}/iptables.json"
st = json.load(open(path)) if os.path.exists(path) else {{"chains": [], "rules": []}}
a = sys.argv[1:]
def save():
    json.dump(st, open(path, "w"))
def show(chain, spec):
    return " ".join(["-A", chain] + [('"' + x + '"') if " " in x else x for x in spec])
op = a[0]
if op == "-S":
    if len(a) > 1 and a[1] not in st["chains"]:
        sys.exit(1)
    if len(a) == 1:
        print("-P FORWARD ACCEPT")
    for c in st["chains"]:
        if len(a) == 1 or c == a[1]:
            print("-N " + c)
    for c, spec in st["rules"]:
        if len(a) == 1 or c == a[1]:
            print(show(c, spec))
elif op == "-N":
    if a[1] in st["chains"]:
        sys.exit(1)
    st["chains"].append(a[1]); save()
elif op == "-A":
    st["rules"].append([a[1], a[2:]]); save()
elif op == "-I":
    st["rules"].insert(0, [a[1], a[3:]]); save()
elif op == "-C":
    sys.exit(0 if [a[1], a[2:]] in st["rules"] else 1)
elif op == "-D":
    if [a[1], a[2:]] not in st["rules"]:
        sys.exit(1)
    st["rules"].remove([a[1], a[2:]]); save()
elif op == "-F":
    if a[1] not in st["chains"]:
        sys.exit(1)
    st["rules"] = [r for r in st["rules"] if r[0] != a[1]]; save()
elif op == "-X":
    referenced = any(r[1][-2:] == ["-j", a[1]] for r in st["rules"])
    if a[1] not in st["chains"] or referenced or any(r[0] == a[1] for r in st["rules"]):
        sys.exit(1)
    st["chains"].remove(a[1]); save()
else:
    sys.exit(9)
""",
            "kubectl": """#!/usr/bin/env python3
import json, sys
a = sys.argv[1:]
ns = a[a.index("-n") + 1]
pods = {
    ("rca-testbed-food", "testbed-restaurant"): ("tb-w3", "10.244.2.10"),
    ("rca-testbed-food", "testbed-order"): ("tb-w3", "10.244.2.20"),
}
if "pods" in a:
    app = a[a.index("-l") + 1].split("=", 1)[1]
    node, ip = pods[(ns, app)]
    print(json.dumps({"items": [{"metadata": {}, "spec": {"nodeName": node},
                                 "status": {"podIP": ip, "conditions": [{"type": "Ready", "status": "True"}]}}]}))
elif "svc" in a:
    print(json.dumps({"spec": {"ports": [{"targetPort": 8081}]}}))
else:
    sys.exit(9)
""",
        }
        for name, body in scripts.items():
            (bindir / name).write_text(body)
            (bindir / name).chmod(0o755)
        guard = node_guard_fakes.install(tmp)
        return dict(os.environ, **guard, SCENARIO_PROFILE_STATE_ROOT=str(tmp / "runner-state"),
                    PATH=f"{bindir}:{os.environ['PATH']}")

    def _host_firewall_run(self, action: str, env: dict[str, str]) -> subprocess.CompletedProcess:
        plan = compiler.compile_plan("f44-r-food-node-firewall-allowlist-omits-order")
        argv, stdin = host_firewall.build_invocation(plan, action)
        return subprocess.run(argv, input=stdin, env=env, capture_output=True, timeout=120, check=False)

    @staticmethod
    def _iptables(tmp: Path) -> dict:
        return json.loads((tmp / "iptables.json").read_text())

    def test_host_firewall_arms_the_node_undo_before_the_rule_and_cleanup_cancels_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = self._host_firewall_stub(tmp)
            result = self._host_firewall_run("run", env)
            self.assertEqual(result.returncode, 0, result.stderr)
            remote = (tmp / "ssh-log").read_text().splitlines()
            self.assertLess(remote.index("sudo -n bash -s"),
                            next(i for i, c in enumerate(remote) if c.startswith("sudo -n iptables -N")))
            self.assertEqual(self._iptables(tmp)["chains"], ["RESTAURANT-API-IN"])
            # F44-R max_injection_duration 25m + 600s margin.
            self.assertEqual(node_guard_fakes.armed_seconds(env, "restaurant-api-in"), [2100])
            self.assertEqual(node_guard_fakes.journal_words(env, "restaurant-api-in"), [])
            # iptables rules end with a reboot; the next boot only removes the guard's own files.
            rule = Path(node_guard_fakes.unit_record(env, "restaurant-api-in")[2])
            self.assertEqual(rule.read_text().splitlines(), [f"r! {tmp / 'state' / 'restaurant-api-in.undo'}",
                                                              f"r! {tmp / 'state' / 'restaurant-api-in.unit'}",
                                                              f"r! {rule}"])
            mark = len(remote)
            result = self._host_firewall_run("cleanup", env)
            self.assertEqual(result.returncode, 0, result.stderr)
            remote = (tmp / "ssh-log").read_text().splitlines()[mark:]
            guards = [i for i, c in enumerate(remote) if c == "sudo -n bash -s"]
            deletes = [i for i, c in enumerate(remote) if c.startswith("sudo -n iptables -D") or c.startswith("sudo -n iptables -X")]
            self.assertEqual(len(guards), 2)
            self.assertLess(guards[0], deletes[0])
            self.assertGreater(guards[1], deletes[-1])
            self.assertEqual(self._iptables(tmp), {"chains": [], "rules": []})
            self.assertEqual(node_guard_fakes.armed_seconds(env, "restaurant-api-in"), [2100])
            self.assertEqual(node_guard_fakes.timers(env), [])
            self.assertEqual(sorted(x.name for x in (tmp / "state").iterdir()), [])
            self.assertEqual(self._host_firewall_run("recovery", env).returncode, 0)

    def test_host_firewall_node_removes_the_rule_by_itself_when_cleanup_never_comes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = self._host_firewall_stub(tmp)
            self.assertEqual(self._host_firewall_run("run", env).returncode, 0)
            before = self._iptables(tmp)
            self.assertEqual(len(before["rules"]), 7)
            undo = tmp / "state" / "restaurant-api-in.undo"
            spare = tmp / "undo-copy"
            spare.write_bytes(undo.read_bytes())
            # The undo removes exactly the jump and the chain the run added, as root on the node.
            body = undo.read_text()
            self.assertIn("while iptables -C FORWARD -d 10.244.2.0/24 -p tcp --dport 8081", body)
            self.assertIn("iptables -F RESTAURANT-API-IN; iptables -X RESTAURANT-API-IN", body)
            for forbidden in ("-P FORWARD", "INPUT", "OUTPUT", "-F FORWARD", "conntrack -F", "F44"):
                self.assertNotIn(forbidden, body)
            # Another FORWARD rule on the node is not the run's and stays.
            st = self._iptables(tmp)
            st["rules"].append(["FORWARD", ["-d", "10.244.9.0/24", "-j", "ACCEPT"]])
            (tmp / "iptables.json").write_text(json.dumps(st))
            fired = node_guard_fakes.fire(env, "restaurant-api-in")
            self.assertEqual(fired.returncode, 0, fired.stderr)
            self.assertEqual(self._iptables(tmp), {"chains": [], "rules": [["FORWARD", ["-d", "10.244.9.0/24", "-j", "ACCEPT"]]]})
            self.assertEqual(sorted(x.name for x in (tmp / "state").iterdir()), ["restaurant-api-in.expired"])
            for _ in range(2):
                again = subprocess.run(["/bin/bash", str(spare)], env=env, capture_output=True, timeout=60)
                self.assertEqual(again.returncode, 0, again.stderr)
                self.assertEqual(self._iptables(tmp)["chains"], [])
                self.assertEqual(len(self._iptables(tmp)["rules"]), 1)
            late = self._host_firewall_run("cleanup", env)
            self.assertEqual(late.returncode, 0, late.stderr)
            self.assertIn(b"the node undid the change by itself at ", late.stderr)
            self.assertEqual(sorted(x.name for x in (tmp / "state").iterdir()), [])
            self.assertEqual(self._host_firewall_run("recovery", env).returncode, 0)

    def test_k8s_scale_only_lowers_one_deployment_replica_count_and_restores_it(self) -> None:
        # F51-R: 주입은 Deployment 하나의 replicas 를 1 에서 0 으로 줄이는 운영 명령뿐이다.
        # 이미지, 프로브, env, 자원을 바꾸면 F41-R, F39-R 의 지문(바뀐 파드 템플릿)이 섞이고,
        # 파드나 Deployment 를 지우면 용량 제거가 아니라 삭제가 원인이 된다.
        plan = compiler.compile_plan("f51-r-banking-account-scaled-to-zero-by-operator-command")
        argv, stdin = k8s_scale.build_invocation(plan, "run")
        self.assertEqual(argv[0], "/usr/bin/bash")
        self.assertEqual(argv[3:], ["run", "rca-testbed-banking", "testbed-account", "1", "0"])
        # 시나리오 id 는 kubectl 이쪽에 머문다(G6, 원칙 8). 상태 파일은 네임스페이스와 Deployment 이름으로 짓는다.
        self.assertNotIn("F51-R", argv)
        script = stdin.decode()
        self.assertNotIn("F51-R", script)
        run_case = script.split("\n  run)")[1].split(";;")[0]
        self.assertIn('scale deploy "$deploy" --replicas="$to"', run_case)
        # 되돌릴 값을 먼저 적고 나서 줄인다.
        self.assertLess(run_case.index('replicas >"$state.tmp"'), run_case.index("scale deploy"))
        cleanup_case = script.split("\n  cleanup)")[1].split(";;")[0]
        self.assertIn('--replicas="$original"', cleanup_case)
        self.assertIn('healthy "$cleanup_wait"', cleanup_case)
        self.assertIn("\ncleanup_wait=420s\n", script)
        preflight = script.split("\ncheck() {")[1].split("\n}")[0]
        self.assertIn("no_autoscaler", preflight)
        self.assertIn('[[ "$(replicas)" == "$from" ]]', preflight)
        for forbidden in ("set image", "set env", "delete", "rollout restart", "patch deploy \"$deploy\"", "evict"):
            self.assertNotIn(forbidden, script)
        params = dict(k8s_scale.CONTRACTS["F51-R"])
        params["deployment"] = "testbed-transfer"
        with self.assertRaisesRegex(ExecutorError, "exactly match"):
            k8s_scale.validate("F51-R", params, {})
        with self.assertRaisesRegex(ExecutorError, "no verified deployment scale contract"):
            k8s_scale.validate("F50-R", k8s_scale.CONTRACTS["F51-R"], {})

    def test_k8s_job_only_creates_one_backfill_job_and_deletes_it(self) -> None:
        # F59-R: 주입은 네임스페이스에 일회성 Job 하나(알림 쪽 보관소 백필)를 만드는 것뿐이다. dispatch, order,
        # MySQL 의 이미지, 설정, 스키마, 인덱스를 바꾸면 F33-H(릴리스), F33-R(인덱스 제거)의 지문이 섞인다.
        plan = compiler.compile_plan("f59-r-food-notify-backfill-deep-pages-drain-dispatch-pool")
        argv, stdin = k8s_job.build_invocation(plan, "run")
        self.assertEqual(argv[0], "/usr/bin/bash")
        self.assertEqual(argv[3:10], ["run", "rca-testbed-food", "notify-delivery-backfill", "testbed-dispatch",
                                      "testbed-notify", "food-delivery-notify:latest", "tb-w3"])
        manifest = json.loads(argv[10])
        # 시나리오 id 는 Job, 레이블, 스크립트, 환경 어디에도 없다(G6, 원칙 8). 상태 파일은 네임스페이스와 Job 이름으로 짓는다.
        script = stdin.decode()
        for text in (" ".join(argv), script):
            self.assertNotIn("F59-R", text)
            self.assertNotIn("f59", text.lower())
        for word in ("scenario", "fault", "chaos", "inject", "rca-"):
            self.assertNotIn(word, json.dumps(manifest["metadata"]["labels"]).lower())
            self.assertNotIn(word, k8s_job.BACKFILL_SCRIPT.lower())
        self.assertEqual(manifest["kind"], "Job")
        self.assertEqual(manifest["metadata"]["labels"], {"app": "notify-delivery-backfill"})
        pod = manifest["spec"]["template"]["spec"]
        self.assertEqual(pod["restartPolicy"], "Never")
        self.assertEqual(pod["nodeSelector"], {"kubernetes.io/hostname": "tb-w3"})
        container = pod["containers"][0]
        self.assertEqual((container["image"], container["imagePullPolicy"]), ("food-delivery-notify:latest", "Never"))
        env = {item["name"]: item["value"] for item in container["env"]}
        self.assertEqual(env["DISPATCH_URL"], "http://testbed-dispatch:8082")
        self.assertEqual((env["WORKERS"], env["PAGES_PER_WORKER"], env["PAGE_SIZE"]), ("12", "290", "500"))
        # 작업은 목록 API 를 읽기만 한다(GET). 쓰는 요청이나 DB 직접 접속이 없다.
        self.assertIn("/api/deliveries?status=$DELIVERY_STATUS&page=$page&size=$PAGE_SIZE", k8s_job.BACKFILL_SCRIPT)
        for forbidden in ("-X", "--data", "mysql", "POST"):
            self.assertNotIn(forbidden, k8s_job.BACKFILL_SCRIPT)
        self.assertEqual(manifest["spec"]["activeDeadlineSeconds"], 1800)
        run_case = script.split("\n  run)")[1].split(";;")[0]
        # 지울 대상을 먼저 적고 나서 만든다.
        self.assertLess(run_case.index('>"$state.tmp"'), run_case.index("apply -f -"))
        cleanup_case = script.split("\n  cleanup)")[1].split(";;")[0]
        self.assertIn('delete job "$job"', cleanup_case)
        self.assertIn("until job_absent", cleanup_case)
        self.assertIn('available "$cleanup_wait"', cleanup_case)
        self.assertIn("\ncleanup_wait=420s\n", script)
        for forbidden in ("set image", "set env", "scale ", "patch ", "rollout restart", "delete deploy", "delete pod"):
            self.assertNotIn(forbidden, script)
        params = dict(k8s_job.CONTRACTS["F59-R"])
        params["workers"] = 40
        with self.assertRaisesRegex(ExecutorError, "exactly match"):
            k8s_job.validate("F59-R", params, {})
        with self.assertRaisesRegex(ExecutorError, "no verified batch job contract"):
            k8s_job.validate("F51-R", k8s_job.CONTRACTS["F59-R"], {})

    def test_k8s_job_statement_backfill_only_reads_history_and_leaves_the_food_contract(self) -> None:
        # F59-P: 같은 실행기로 banking 네임스페이스에 원장 쪽 명세서 백필 Job 하나를 만든다. transfer, account, api,
        # Oracle 의 이미지, 설정, 스키마, 인덱스를 바꾸면 F42-R(릴리스), F57-R(표 이전)의 지문이 섞인다.
        plan = compiler.compile_plan("f59-p-banking-ledger-statement-backfill-drains-transfer-pool")
        argv, stdin = k8s_job.build_invocation(plan, "run")
        self.assertEqual(argv[3:10], ["run", "rca-testbed-banking", "ledger-statement-backfill", "testbed-transfer",
                                      "testbed-ledger", "core-banking-ledger:latest", "tb-w2"])
        manifest = json.loads(argv[10])
        script = stdin.decode()
        for text in (" ".join(argv), script):
            self.assertNotIn("F59-P", text)
            self.assertNotIn("f59", text.lower())
        for word in ("scenario", "fault", "chaos", "inject", "rca-"):
            self.assertNotIn(word, json.dumps(manifest["metadata"]["labels"]).lower())
            self.assertNotIn(word, k8s_job.STATEMENT_SCRIPT.lower())
        self.assertEqual(manifest["metadata"]["labels"], {"app": "ledger-statement-backfill"})
        pod = manifest["spec"]["template"]["spec"]
        self.assertEqual(pod["nodeSelector"], {"kubernetes.io/hostname": "tb-w2"})
        container = pod["containers"][0]
        self.assertEqual(container["args"], [k8s_job.STATEMENT_SCRIPT])
        self.assertEqual((container["image"], container["imagePullPolicy"]), ("core-banking-ledger:latest", "Never"))
        env = {item["name"]: item["value"] for item in container["env"]}
        self.assertEqual(env, {"ACCOUNT_URL": "http://testbed-account:8081", "TRANSFER_URL": "http://testbed-transfer:8082",
                               "PAGE_SIZE": "500", "WORKERS": "48", "REQUEST_TIMEOUT": "60"})
        # 작업은 계좌 목록과 이체 내역을 읽기만 한다(GET). 이체를 만들거나 DB 에 바로 붙지 않는다.
        self.assertIn("$ACCOUNT_URL/api/accounts", k8s_job.STATEMENT_SCRIPT)
        self.assertIn("/api/transfers?fromAccount=$account&page=$page&size=$PAGE_SIZE", k8s_job.STATEMENT_SCRIPT)
        # 계좌를 워커에 나누지 않고 계좌마다 쪽을 나눈다: 이체가 몰린 몇 계좌에서도 워커 전부가 바빠야
        # 동시 호출이 transfer 풀(15)을 늘 넘는다(1차 평가: 계좌를 나누면 몇 분 뒤 동시 8 로 줄었다).
        self.assertIn('page="$1"', k8s_job.STATEMENT_SCRIPT)
        self.assertIn("page=$((page + WORKERS))", k8s_job.STATEMENT_SCRIPT)
        self.assertIn('for account in "${accounts[@]}"', k8s_job.STATEMENT_SCRIPT)
        for forbidden in ("-X", "--data", "sqlplus", "POST"):
            self.assertNotIn(forbidden, k8s_job.STATEMENT_SCRIPT)
        # food 판의 Job 은 그대로 배달 목록 스크립트를 돈다.
        food = k8s_job.job_manifest(k8s_job.CONTRACTS["F59-R"])["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(food["args"], [k8s_job.BACKFILL_SCRIPT])
        params = dict(k8s_job.CONTRACTS["F59-P"])
        params["workers"] = 96
        with self.assertRaisesRegex(ExecutorError, "exactly match"):
            k8s_job.validate("F59-P", params, {})

    def test_k8s_job_payment_export_dumps_with_default_table_locks_and_leaves_the_backfills(self) -> None:
        # F69-R: 같은 실행기로 food 네임스페이스에 결제 감사 자료 내보내기 Job 하나를 만든다. payment, order, MySQL 의
        # 이미지, 설정, 스키마를 바꾸면 F49-P(릴리스), F53-H(열 추가 마이그레이션)의 지문이 섞인다. 잠금은 mysqldump 기본값
        # (--lock-tables)에서만 나와야 한다: 스크립트가 LOCK TABLES 를 직접 쓰거나 --single-transaction 을 쓰면 원본 고리가 아니다.
        plan = compiler.compile_plan("f69-r-food-payment-audit-export-locks-payment-tables")
        argv, stdin = k8s_job.build_invocation(plan, "run")
        self.assertEqual(argv[3:10], ["run", "rca-testbed-food", "payment-audit-export", "testbed-payment",
                                      "statefulset/testbed-mysql", "mysql:8.0", "tb-w3"])
        manifest = json.loads(argv[10])
        script = stdin.decode()
        for text in (" ".join(argv), script):
            self.assertNotIn("F69-R", text)
            self.assertNotIn("f69", text.lower())
        for word in ("scenario", "fault", "chaos", "inject", "rca-"):
            self.assertNotIn(word, json.dumps(manifest["metadata"]["labels"]).lower())
            self.assertNotIn(word, k8s_job.EXPORT_SCRIPT.lower())
        self.assertEqual(manifest["metadata"]["labels"], {"app": "payment-audit-export"})
        pod = manifest["spec"]["template"]["spec"]
        self.assertEqual(pod["restartPolicy"], "Never")
        self.assertEqual(pod["nodeSelector"], {"kubernetes.io/hostname": "tb-w3"})
        container = pod["containers"][0]
        self.assertEqual(container["args"], [k8s_job.EXPORT_SCRIPT])
        self.assertEqual((container["image"], container["imagePullPolicy"]), ("mysql:8.0", "Never"))
        self.assertEqual(container["resources"]["limits"], {"cpu": "150m", "memory": "256Mi"})
        # 계정은 food 서비스들이 쓰는 Secret 에서 받는다. 매니페스트와 인자에 비밀번호가 없다.
        env = {item["name"]: item for item in container["env"]}
        self.assertEqual(env["EXPORT_TABLES"]["value"], "payments settlement_summary")
        self.assertEqual(env["MYSQL_PWD"]["valueFrom"]["secretKeyRef"], {"name": "mysql-secret", "key": "MYSQL_PASSWORD"})
        self.assertEqual(env["DB_USER"]["valueFrom"]["secretKeyRef"], {"name": "mysql-secret", "key": "MYSQL_USER"})
        self.assertTrue(all("value" not in item or "PASS" not in item["name"] for item in container["env"]))
        self.assertIn('mysqldump --host="$DB_HOST" --user="$DB_USER" --no-tablespaces "$DB_NAME" $EXPORT_TABLES | xz -6 -T1',
                      k8s_job.EXPORT_SCRIPT)
        for forbidden in ("--single-transaction", "LOCK TABLES", "--lock-all-tables", "FLUSH", "INSERT", "DELETE", "UPDATE",
                          "ALTER", "DROP", "sleep"):
            self.assertNotIn(forbidden, k8s_job.EXPORT_SCRIPT)
        # 이미지 주인이 StatefulSet 이면 kind/name 으로 찾는다. Deployment 이름만 적은 백필 계약은 그대로 deploy/ 로 찾는다.
        self.assertIn('[[ "$ref" == */* ]] || ref="deploy/$ref"', script)
        cleanup_case = script.split("\n  cleanup)")[1].split(";;")[0]
        self.assertIn('delete job "$job"', cleanup_case)
        self.assertIn('available "$cleanup_wait"', cleanup_case)
        self.assertIn("\ncleanup_wait=420s\n", script)
        # 백필 계약은 그대로 API 스크립트를 돈다.
        food = k8s_job.job_manifest(k8s_job.CONTRACTS["F59-R"])["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(food["args"], [k8s_job.BACKFILL_SCRIPT])
        self.assertEqual(food["resources"]["limits"], {"cpu": "500m", "memory": "256Mi"})
        params = dict(k8s_job.CONTRACTS["F69-R"])
        params["tables"] = "payments orders"
        with self.assertRaisesRegex(ExecutorError, "exactly match"):
            k8s_job.validate("F69-R", params, {})

    def test_k8s_dns_flips_only_the_pod_dns_policy_and_restores_it(self) -> None:
        # F37-R: 주입은 파드 템플릿의 dnsPolicy 한 칸만 바꾼다. dnsConfig 로 가짜 네임서버를
        # 넣거나 Service, CoreDNS 를 건드리면 "배포된 resolver 설정이 내부 이름을 모른다"는
        # 지문이 아니라 다른 장애(F31 폐기 후보의 Service 삭제, 공용 DNS 장애)가 된다.
        plan = compiler.compile_plan("f37-r-banking-api-dns-policy-node-resolver")
        argv, stdin = k8s_dns.build_invocation(plan, "run")
        self.assertEqual(argv[0], "/usr/bin/bash")
        self.assertEqual(argv[3:], ["run", "rca-testbed-banking", "testbed-api", "ClusterFirst", "Default"])
        # 시나리오 id 는 kubectl 이쪽에 머문다(G6, 원칙 8). 상태 파일도 Deployment 로 이름 짓는다.
        self.assertNotIn("F37-R", argv)
        script = stdin.decode()
        self.assertNotIn("F37-R", script)
        self.assertIn('"/spec/template/spec/dnsPolicy"', script)
        run_case = script.split("\n  run)")[1].split(";;")[0]
        self.assertIn('patch "$fault"', run_case)
        cleanup_case = script.split("\n  cleanup)")[1].split(";;")[0]
        self.assertIn('patch "$original"', cleanup_case)
        self.assertIn('healthy "$cleanup_wait"', cleanup_case)
        self.assertIn("\ncleanup_wait=420s\n", script)
        for forbidden in ("dnsConfig\"", "nameservers", "delete", "coredns", "configmap"):
            self.assertNotIn(forbidden, script)
        params = dict(k8s_dns.CONTRACTS["F37-R"])
        params["deployment"] = "testbed-account"
        with self.assertRaisesRegex(ExecutorError, "exactly match"):
            k8s_dns.validate("F37-R", params, {})
        with self.assertRaisesRegex(ExecutorError, "no verified DNS policy contract"):
            k8s_dns.validate("F35-R", k8s_dns.CONTRACTS["F37-R"], {})

    def test_service_calls_resolve_names_not_ip_literals(self) -> None:
        # C3 (spec-scenario-design §9.3 G15): 이름 해석을 거치지 않는 호출에는 DNS 장애를
        # 걸 수 없다. commerce payment 의 PG 기본값이 109 호스트 IP 리터럴로 남아 있었다.
        # 앱의 base-url 기본값과 configmap 의 *_URL 값 모두 이름이어야 한다. OTel 수집 주소는
        # 클러스터 밖 tb-ops 라 대상이 아니다(앱 호출 경로가 아님).
        repo = ROOT.parents[1]
        ip_url = re.compile(r"https?://\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?")
        checked = 0
        for domain in ("commerce", "core-banking", "food-delivery"):
            for yml in sorted((repo / domain).glob("*-service/src/main/resources/application*.yml")):
                for line in yml.read_text(encoding="utf-8").splitlines():
                    if "base-url" in line or "_URL:" in line:
                        checked += 1
                        self.assertIsNone(ip_url.search(line), f"{yml.relative_to(repo)}: {line.strip()}")
            configmap = (repo / domain / "k8s" / "02-configmaps.yaml").read_text(encoding="utf-8")
            for line in configmap.splitlines():
                if re.match(r"\s+[A-Z_]+_URL:", line):
                    checked += 1
                    self.assertIsNone(ip_url.search(line), f"{domain} configmap: {line.strip()}")
        self.assertGreater(checked, 20, "sweep found almost nothing to check")
        payment = (repo / "commerce/payment-service/src/main/resources/application.yml").read_text(encoding="utf-8")
        self.assertIn("${PG_API_URL:http://testbed-external-pg-mock:9090}", payment)

    def test_db_instance_readonly_flips_only_the_global_flag_and_restores_it(self) -> None:
        # F38-R: 주입은 MySQL 전역 변수 read_only 한 칸만 켠다. 계정, 세션, 테이블을 건드리거나
        # SET PERSIST 로 남기면 "운영 세션이 인스턴스를 읽기 전용으로 바꿔 모든 쓰기가 거절된다"는
        # 지문이 아니라 다른 장애(F35-R 계정 잠금, F14-P 테이블 하나)가 되거나 재시작 뒤에도 남는다.
        plan = compiler.compile_plan("f38-r-food-mysql-global-read-only")
        argv, stdin = db_instance_readonly.build_invocation(plan, "run")
        self.assertEqual(argv[0], "/usr/bin/bash")
        self.assertEqual(argv[3:], ["run", "rca-testbed-food", "testbed-mysql-0", "fooddelivery"])
        # 시나리오 id 는 kubectl 이쪽에 머문다(G6, 원칙 8).
        self.assertNotIn("F38-R", argv)
        script = own_body(stdin.decode())
        self.assertNotIn("F38-R", script)
        run_case = script.split("\n  run)")[1].split(";;")[0]
        self.assertIn("expect 00", run_case)
        self.assertIn("flip ON", run_case)
        self.assertIn("expect 10", run_case)
        cleanup_case = script.split("\n  cleanup)")[1].split(";;")[0]
        self.assertIn("flip OFF", cleanup_case)
        self.assertIn("expect 00", cleanup_case)
        preflight_case = script.split("\n  preflight)")[1].split(";;")[0]
        self.assertIn('"$(bypass)" == 0', preflight_case)
        for forbidden in ("persist", "super_read_only=", "kill ", "alter user", "grant ", "revoke ", "drop "):
            self.assertNotIn(forbidden, script.lower())
        params = dict(db_instance_readonly.CONTRACTS["F38-R"])
        params["app_account"] = "lucida_mon"
        with self.assertRaisesRegex(ExecutorError, "exactly match"):
            db_instance_readonly.validate("F38-R", params, {})
        with self.assertRaisesRegex(ExecutorError, "no verified read-only instance contract"):
            db_instance_readonly.validate("F36-R", db_instance_readonly.CONTRACTS["F38-R"], {})

    def test_db_config_row_inserts_one_row_restarts_its_reader_and_takes_it_back(self) -> None:
        # F40-R: 주입은 pricing_schema.promotions 의 행 하나(할인율 100.00)와 그 행을 읽는 pricing 재시작뿐이다.
        # 스키마, 다른 행, pricing 의 이미지와 env 를 건드리면 "검증 없이 들어간 정책 데이터를 소비 경로가 그대로
        # 쓴다"는 지문이 아니라 다른 장애(F36-R 꼴 스키마 변경, F08-P 꼴 설정 배포)가 된다.
        plan = compiler.compile_plan("f40-r-commerce-pricing-promotion-full-discount")
        argv, stdin = db_config_row.build_invocation(plan, "run")
        self.assertEqual(argv[0], "/usr/bin/bash")
        self.assertEqual(argv[3:], [
            "run", "rca-testbed-commerce", "testbed-postgres-0", "pricing_schema.promotions", "4",
            "가을 정기 세일", "전 품목 10% 할인", "100.00", "testbed-pricing",
        ])
        # 시나리오 id 는 kubectl 이쪽에 머문다(G6, 원칙 8).
        self.assertNotIn("F40-R", argv)
        script = own_body(stdin.decode())
        self.assertNotIn("F40-R", script)
        run_case = script.split("\n  run)")[1].split(";;")[0]
        self.assertIn("check; insert;", run_case)
        self.assertIn('[[ "$(row_count)" == 1 ]]', run_case)
        self.assertIn("restart", run_case)
        # run 은 롤아웃을 기다리지 않는다(조정 잠금 동안 러너 임대가 끊기지 않게, k8s.env 와 같은 이유).
        self.assertNotIn('healthy "$cleanup_wait"', run_case)
        cleanup_case = script.split("\n  cleanup)")[1].split(";;")[0]
        self.assertIn("remove", cleanup_case)
        self.assertIn("restart", cleanup_case)
        self.assertIn('healthy "$cleanup_wait"', cleanup_case)
        self.assertIn("\ncleanup_wait=420s\n", script)
        self.assertIn('DELETE FROM $table WHERE id = $row_id;', script)
        # 확인 조회는 행 id 만 묻는다. Top SQL 에 남아도 할인율이나 활성 여부(정답 필드)를 가리키지 않게.
        for line in script.splitlines():
            if "SELECT" in line:
                self.assertNotIn("discount_percent", line)
                self.assertNotIn("active", line)
        for forbidden in ("drop ", "truncate", "alter ", "update ", "set image", "set env", '"${k[@]}" patch'):
            self.assertNotIn(forbidden, script.lower())
        params = dict(db_config_row.CONTRACTS["F40-R"])
        params["discount_percent"] = "10.00"
        with self.assertRaisesRegex(ExecutorError, "exactly match"):
            db_config_row.validate("F40-R", params, {})
        with self.assertRaisesRegex(ExecutorError, "no verified configuration row contract"):
            db_config_row.validate("F38-R", db_config_row.CONTRACTS["F40-R"], {})

    def test_db_row_delete_archives_and_deletes_two_listed_rows_and_puts_them_back(self) -> None:
        # F56-R: 주입은 BANKING.ACCOUNTS 의 계약된 두 행(commerce 정산 계좌)을 보관 표에 옮기고 지우는 것뿐이다.
        # 표를 지우거나(F35-H), 잠그거나(F01-P), 다른 행이나 스키마를 바꾸면 "정리 작업이 살아 있는 대상을 잘못
        # 지웠다"는 지문이 아니라 다른 장애가 된다. 되돌리기는 보관한 행을 그대로 되넣는 것이고 값을 지어내지 않는다.
        plan = compiler.compile_plan("f56-r-banking-partner-settlement-accounts-deleted-by-purge-with-wrong-ids")
        argv, stdin = db_row_delete.build_invocation(plan, "run")
        self.assertEqual(argv[:3], ["/usr/bin/bash", "-s", "--"])
        self.assertEqual(argv[3:], [
            "run", "rca-testbed-banking", "testbed-oracle-0", "BANKING", "ACCOUNTS", "ID",
            "ACCOUNTS_PURGE_ARCHIVE", "commerce-merchant,commerce-settlement",
        ])
        # 시나리오 id 는 kubectl 이쪽에 머문다(G6, 원칙 8). 보관 표 이름에도 없다.
        self.assertNotIn("F56-R", " ".join(argv))
        script = stdin.decode()
        self.assertNotIn("F56-R", script)
        self.assertNotIn("f56", script.lower())
        run_case = script.split("\n  run)")[1].split(";;")[0]
        # 보관이 지우기보다 먼저이고, 잠금, 보관, 지우기, 커밋이 한 트랜잭션이다.
        self.assertLess(run_case.index("create table $schema.$archive"), run_case.index("delete from"))
        self.assertIn("for update;\ninsert into $schema.$archive", run_case)
        self.assertIn("delete from $schema.$table where $keycol in ($inlist);\ncommit;", run_case)
        self.assertIn('[[ "$(live_rows)" == 0 ]]', run_case)
        self.assertIn('[[ "$(archived_rows)" == "$want" ]]', run_case)
        cleanup_case = script.split("\n  cleanup)")[1].split(";;")[0]
        self.assertIn("insert into $schema.$table select a.* from $schema.$archive a", cleanup_case)
        self.assertIn("not exists", cleanup_case)
        self.assertIn("drop table $schema.$archive purge;", cleanup_case)
        self.assertIn('[[ "$(live_rows)" == "$want" ]]', cleanup_case)
        preflight_case = script.split("\n  preflight)")[1].split(";;")[0]
        self.assertIn("check", preflight_case)
        self.assertIn('[[ "$(archive_exists)" == 0 ]]', script.split("check() {")[1].split("}")[0])
        for forbidden in ("truncate", "drop table $schema.$table", "alter ", "update ", "lock table", "grant ", "revoke "):
            self.assertNotIn(forbidden, script.lower().replace("alter session set container", ""))
        params = dict(db_row_delete.CONTRACTS["F56-R"])
        params["keys"] = ["commerce-merchant", "commerce-settlement", "ACC-1001"]
        with self.assertRaisesRegex(ExecutorError, "exactly match"):
            db_row_delete.validate("F56-R", params, {})
        with self.assertRaisesRegex(ExecutorError, "no verified row deletion contract"):
            db_row_delete.validate("F35-H", db_row_delete.CONTRACTS["F56-R"], {})

    def test_db_row_update_backs_up_one_window_reopens_exactly_those_rows_and_writes_them_back(self) -> None:
        # F51-P: 주입은 fooddelivery.dispatches 에서 최근 4시간에 끝난 배차를 백업 표에 복사하고 그 행들만 배정 중으로
        # 되돌리는 것뿐이다. 표를 지우거나(F32-P), 잠그거나(F60-H), 배차 한도 설정을 바꾸면(F32-R) "정정 작업이 의도보다
        # 큰 집합에 적용됐다"는 지문이 아니라 다른 장애가 된다. 되돌리기는 백업한 상태와 배정 시각을 같은 id 에 되쓰는 것이다.
        plan = compiler.compile_plan("f51-p-food-redispatch-correction-reopens-finished-deliveries-fills-courier-pool")
        argv, stdin = db_row_update.build_invocation(plan, "run")
        self.assertEqual(argv[:3], ["/usr/bin/bash", "-s", "--"])
        self.assertEqual(argv[3:], [
            "run", "rca-testbed-food", "testbed-mysql-0", "fooddelivery", "dispatches",
            "dispatches_redispatch_backup", "DELIVERED", "ASSIGNED", "4", "2500", "2000",
        ])
        # 시나리오 id 는 kubectl 이쪽에 머문다(G6, 원칙 8). 백업 표 이름에도 없다.
        self.assertNotIn("F51-P", " ".join(argv))
        script = own_body(stdin.decode())
        self.assertNotIn("F51-P", script)
        self.assertNotIn("f51", script.lower())
        run_case = script.split("\n  run)")[1].split(";;")[0]
        # 백업이 바꾸기보다 먼저이고, 바꾸는 행은 정확히 백업한 행이다.
        self.assertLess(run_case.index("CREATE TABLE $db.$backup AS SELECT id, status, assigned_at"), run_case.index("UPDATE $db.$table d JOIN $db.$backup b"))
        self.assertIn("SET d.status = '$to', d.assigned_at = NOW() WHERE d.status = '$from';", run_case)
        self.assertIn('[[ "$(reopened_rows)" == "$saved" ]]', run_case)
        self.assertIn("(( $(busy_rows) >= capacity ))", run_case)
        cleanup_case = script.split("\n  cleanup)")[1].split(";;")[0]
        self.assertIn("SET d.status = b.status, d.assigned_at = b.assigned_at;", cleanup_case)
        self.assertIn('[[ "$(unrestored_rows)" == 0 ]]', cleanup_case)
        self.assertIn("DROP TABLE $db.$backup;", cleanup_case)
        self.assertIn("(( $(busy_rows) < capacity ))", cleanup_case)
        check = script.split("check() {")[1].split("\n}")[0]
        self.assertIn('[[ "$(backup_exists)" == 0 ]]', check)
        self.assertIn("(( $(window_rows) >= minimum ))", check)
        self.assertIn("(( $(busy_rows) < capacity ))", check)
        for forbidden in ("truncate", "drop table $db.$table", "alter ", "delete ", "lock table", "grant ", "set global", "rename "):
            self.assertNotIn(forbidden, script.lower())
        params = dict(db_row_update.CONTRACTS["F51-P"])
        for change in ({"window_hours": 24}, {"minimum_rows": 1000}, {"table": "orders"}, {"backup_table": "dispatches"}):
            with self.assertRaisesRegex(ExecutorError, "exactly match"):
                db_row_update.validate("F51-P", {**params, **change}, {})
        with self.assertRaisesRegex(ExecutorError, "no verified row update contract"):
            db_row_update.validate("F56-R", db_row_update.CONTRACTS["F51-P"], {})

    def test_k8s_image_rolls_one_container_to_an_absent_tag_and_restores_it(self) -> None:
        # F17-H: 주입은 컨테이너 image 한 칸을 노드에 없는 태그로 바꾸는 롤아웃뿐이다. 노드에서 이미지를
        # 지우거나(정리가 다시 적재에 기대게 된다) 전략, 프로브를 바꾸면 다른 장애가 된다. 피해의 전제인
        # maxSurge 0 과 "기준 이미지는 노드에 있고 장애 태그는 없다"는 preflight 가 살아 있는 객체에서 확인한다.
        plan = compiler.compile_plan("f17-h-banking-transfer-release-image-missing")
        argv, stdin = k8s_image.build_invocation(plan, "run")
        self.assertEqual(argv[0], "/usr/bin/bash")
        self.assertEqual(argv[3:], ["run", "rca-testbed-banking", "testbed-transfer", "transfer-service",
                                    "core-banking-transfer:latest", "core-banking-transfer:2.1.0"])
        # 시나리오 id 는 kubectl 이쪽에 머문다(G6, 원칙 8). 상태 파일도 Deployment 로 이름 짓는다.
        self.assertNotIn("F17-H", argv)
        script = stdin.decode()
        self.assertNotIn("F17-H", script)
        self.assertIn("set image deploy", script)
        self.assertIn("maxSurge", script)
        self.assertIn(".status.images", script)
        run_case = script.split("\n  run)")[1].split(";;")[0]
        self.assertIn('set_image "$fault"', run_case)
        cleanup_case = script.split("\n  cleanup)")[1].split(";;")[0]
        self.assertIn('set_image "$original"', cleanup_case)
        self.assertIn("healthy 420s", cleanup_case)
        self.assertIn("proxy/stats/summary", script)
        for forbidden in ("rmi", "images rm", "crictl", "delete", "strategy\":", "Probe"):
            self.assertNotIn(forbidden, script)
        params = dict(k8s_image.CONTRACTS["F17-H"])
        params["deployment"] = "testbed-account"
        with self.assertRaisesRegex(ExecutorError, "exactly match"):
            k8s_image.validate("F17-H", params, {})
        with self.assertRaisesRegex(ExecutorError, "no verified release image contract"):
            k8s_image.validate("F17-R", k8s_image.CONTRACTS["F17-H"], {})

    def test_k8s_image_foreign_mode_rolls_transfer_to_the_ledger_image_and_back(self) -> None:
        # F77-R: 이름이 겹친 매니페스트 적용이 바꾸는 것은 transfer 컨테이너 image 한 칸뿐이다. 그 이미지는 같은 노드의
        # testbed-ledger 가 쓰는 것이라 노드에 올리지도 지우지도 않는다(지우면 진짜 원장이 다음 재시작에 뜨지 못한다).
        # preflight 는 원장 이미지가 원장 Deployment 의 것이고 같은 노드에서 가용인지, maxSurge 0 인지 살아 있는 객체에서 본다.
        plan = compiler.compile_plan("f77-r-banking-transfer-deployment-overwritten-by-name-colliding-ledger-manifest")
        argv, stdin = k8s_image.build_invocation(plan, "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-banking", "testbed-transfer", "transfer-service",
                                    "core-banking-transfer:latest", "core-banking-ledger:latest", "testbed-ledger"])
        self.assertNotIn("F77-R", argv)
        self.assertEqual(stdin, k8s_image.FOREIGN_SCRIPT)
        script = stdin.decode()
        self.assertNotIn("F77-R", script)
        self.assertIn("set image deploy", script)
        self.assertIn("maxSurge", script)
        self.assertIn("owner_ok", script)
        run_case = script.split("\n  run)")[1].split(";;")[0]
        self.assertIn('set_image "$fault"', run_case)
        cleanup_case = script.split("\n  cleanup)")[1].split(";;")[0]
        self.assertIn('set_image "$original"', cleanup_case)
        self.assertIn("healthy 420s", cleanup_case)
        recovery_case = script.split("\n  recovery)")[1].split(";;")[0]
        self.assertIn("owner_ok", recovery_case)
        for forbidden in ("rmi", "images rm", "crictl", "delete", "strategy\":", "Probe"):
            self.assertNotIn(forbidden, script)
        # 노드 적재는 run 에 없다(cleanup 의 재적재 안내 문구만 docker save 를 말한다).
        for forbidden in ("docker save", "images import", "node_sh"):
            self.assertNotIn(forbidden, run_case)
        # 원장 Deployment 의 매니페스트 이미지가 곧 장애 이미지다.
        ledger = (ROOT.parent.parent / "core-banking" / "k8s" / "23-ledger-service.yaml").read_text()
        self.assertIn("image: core-banking-ledger:latest", ledger)
        self.assertIn("port: 8083", ledger)
        transfer = (ROOT.parent.parent / "core-banking" / "k8s" / "22-transfer-service.yaml").read_text()
        self.assertIn("maxSurge: 0", transfer)
        self.assertIn("port: 8082", transfer)
        params = dict(k8s_image.CONTRACTS["F77-R"])
        k8s_image.validate("F77-R", params, {})
        for change in ({"fault_image": "core-banking-transfer:2.9.9"}, {"fault_owner": "testbed-account"},
                       {"mode": "release"}):
            with self.assertRaisesRegex(ExecutorError, "exactly match"):
                k8s_image.validate("F77-R", {**params, **change}, {})

    def test_k8s_image_release_mode_loads_the_release_only_for_the_fault(self) -> None:
        # F41-R: 결함 릴리스는 109 docker 에만 있다. 노드에는 주입 직전에 올리고(run), 매니페스트 이미지로
        # 되돌려 그 릴리스를 쓰는 파드가 없어진 뒤 노드에서 지운다(cleanup). 기존 F17-H 스크립트는 그대로다.
        plan = compiler.compile_plan("f41-r-banking-account-release-memory-leak")
        argv, stdin = k8s_image.build_invocation(plan, "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-banking", "testbed-account", "account-service",
                                    "core-banking-account:latest", "core-banking-account:1.3.0"])
        self.assertNotIn("F41-R", argv)
        script = stdin.decode()
        self.assertNotIn("F41-R", script)
        self.assertEqual(stdin, k8s_image.RELEASE_SCRIPT)
        self.assertNotEqual(k8s_image.RELEASE_SCRIPT, k8s_image.SCRIPT)
        preflight_case = script.split("\n  preflight)")[1].split(";;")[0]
        self.assertIn("check", preflight_case)
        check_fn = script.split("\ncheck() {")[1].split("\n}")[0]
        for needle in ('docker image inspect "$fault"', 'in_containerd "$baseline"', "disk_ok", 'release_absent "$id"'):
            self.assertIn(needle, check_fn)
        run_case = script.split("\n  run)")[1].split(";;")[0]
        self.assertIn("ctr -n k8s.io images import -", run_case)
        self.assertLess(run_case.index("images import"), run_case.index('set_image "$fault"'))
        cleanup_case = script.split("\n  cleanup)")[1].split(";;")[0]
        self.assertIn('set_image "$original"; healthy 420s; release_unused; unload "$id"; rm -f "$release_ref" "$state"', cleanup_case)
        # CRI 는 이미지마다 이름 참조 말고 "sha256:<이미지 ID>" 참조를 따로 둔다. 이름만 지우면 릴리스 내용이
        # 노드에 남으므로(tb-w2 에 이름 없는 ID 참조가 이미 있었다) run 이 ID 를 남기고 cleanup 이 둘 다 지운다.
        self.assertIn('release_id >"$release_ref.tmp"', run_case)
        self.assertIn("docker image inspect --format '{{.Id}}' \"$fault\"", script)
        refs_fn = script.split("\nrelease_refs() {")[1].split("\n}")[0]
        self.assertIn('-e "docker.io/library/$fault" ${1:+-e "$1"}', refs_fn)
        self.assertIn("|| return 1", refs_fn)
        unload_fn = script.split("\nunload() {")[1].split("\n}")[0]
        self.assertIn('for ref in $(release_refs "$id")', unload_fn)
        self.assertIn('images rm "$ref"', unload_fn)
        self.assertIn('release_absent "$id"', unload_fn)
        recovery_case = script.split("\n  recovery)")[1].split(";;")[0]
        self.assertIn('release_absent "$(release_id)"', recovery_case)
        self.assertIn('[[ ! -e "$release_ref" ]]', recovery_case)
        # 매니페스트 이미지, 전략, 프로브, 자원은 건드리지 않는다.
        for forbidden in ("docker rmi", "docker tag", "docker build", "strategy\":", "Probe", "set resources", "set env"):
            self.assertNotIn(forbidden, script)
        # 함수를 그대로 떼어 가짜 노드(ctr images ls/rm)에 돌려, 이름 참조와 ID 참조가 모두 지워지고
        # 기준 이미지와 다른 ID 참조는 남는지, 목록 조회가 실패하면 "없음"으로 읽지 않는지 본다.
        functions = "\n".join([
            "release_refs() {" + refs_fn + "\n}",
            next(line for line in script.splitlines() if line.startswith("release_absent()")),
            "unload() {" + unload_fn + "\n}",
        ])
        harness = (
            "set -euo pipefail\nfault=core-banking-account:1.3.0\n"
            "REFS=$'docker.io/library/core-banking-account:latest\\nsha256:base\\n"
            "docker.io/library/core-banking-account:1.3.0\\nsha256:release'\n"
            "node_sh() { [[ -n \"${DOWN:-}\" ]] && return 255\n"
            "  if [[ \"$*\" == *'images ls -q'* ]]; then printf '%s\\n' \"$REFS\"; "
            "else REFS=$(grep -vFx \"${@: -1}\" <<<\"$REFS\"); fi; }\n"
            + functions +
            "\nif DOWN=1 release_absent sha256:release; then echo listing-failure-read-as-absent; fi\n"
            "if release_absent sha256:release; then echo release-missed; fi\n"
            "unload sha256:release\nprintf '%s\\n' \"$REFS\"\n"
        )
        result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.split(),
                         ["docker.io/library/core-banking-account:latest", "sha256:base"])
        params = dict(k8s_image.CONTRACTS["F41-R"])
        params["fault_image"] = "core-banking-account:latest"
        with self.assertRaisesRegex(ExecutorError, "exactly match"):
            k8s_image.validate("F41-R", params, {})

    def test_k8s_image_release_mode_rolls_transfer_to_its_own_release_tag(self) -> None:
        # F42-R: 같은 release 스크립트로 transfer 를 2.2.0 으로 롤아웃한다. F17-H 의 2.1.0 은 노드에 없어야
        # 하는 태그라 겹치면 F17-H 가 깨진다. 릴리스 태그는 fault-images/f42-r/image.json 과 같아야 한다.
        plan = compiler.compile_plan("f42-r-banking-transfer-release-full-scan-limit-query")
        argv, stdin = k8s_image.build_invocation(plan, "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-banking", "testbed-transfer", "transfer-service",
                                    "core-banking-transfer:latest", "core-banking-transfer:2.2.0"])
        self.assertNotIn("F42-R", argv)
        self.assertEqual(stdin, k8s_image.RELEASE_SCRIPT)
        self.assertNotEqual(k8s_image.CONTRACTS["F42-R"]["fault_image"], k8s_image.CONTRACTS["F17-H"]["fault_image"])
        image = json.loads((ROOT / "fault-images" / "f42-r" / "image.json").read_text())
        self.assertEqual((image["base"], image["fault"]),
                         (k8s_image.CONTRACTS["F42-R"]["baseline_image"], k8s_image.CONTRACTS["F42-R"]["fault_image"]))
        patch = (ROOT / "fault-images" / "f42-r" / "transfer-service.patch").read_text()
        added = "\n".join(line for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
        for word in ("F42", "f42", "fault", "bug", "chaos", "scenario"):
            self.assertNotIn(word, added)

    def test_k8s_image_release_mode_rolls_commerce_cart_on_its_own_node(self) -> None:
        # F43-R: 같은 release 스크립트를 commerce 네임스페이스(cart, tb-w1)에 쓴다. 노드는 스크립트가
        # Deployment 의 nodeSelector 에서 읽는다. 릴리스 태그는 fault-images/f43-r/image.json 과 같아야 하고,
        # 다른 시나리오의 릴리스 태그와 겹치지 않는다.
        plan = compiler.compile_plan("f43-r-commerce-cart-release-db-connection-leak")
        argv, stdin = k8s_image.build_invocation(plan, "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-commerce", "testbed-cart", "cart-service",
                                    "commerce-cart:latest", "commerce-cart:1.2.0"])
        self.assertNotIn("F43-R", argv)
        self.assertEqual(stdin, k8s_image.RELEASE_SCRIPT)
        tags = [c["fault_image"] for sid, c in k8s_image.CONTRACTS.items() if sid != "F43-R"]
        self.assertNotIn(k8s_image.CONTRACTS["F43-R"]["fault_image"], tags)
        image = json.loads((ROOT / "fault-images" / "f43-r" / "image.json").read_text())
        self.assertEqual((image["base"], image["fault"]),
                         (k8s_image.CONTRACTS["F43-R"]["baseline_image"], k8s_image.CONTRACTS["F43-R"]["fault_image"]))
        patch = (ROOT / "fault-images" / "f43-r" / "cart-service.patch").read_text()
        added = "\n".join(line for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
        for word in ("F43", "f43", "fault", "bug", "chaos", "scenario"):
            self.assertNotIn(word, added)

    def test_k8s_image_release_mode_rolls_banking_api(self) -> None:
        # F47-R: 같은 release 스크립트로 banking api 를 1.4.0 으로 롤아웃한다(tb-w2, 노드는 스크립트가
        # Deployment 의 nodeSelector 에서 읽는다). 릴리스 태그는 fault-images/f47-r/image.json 과 같아야 하고,
        # 다른 시나리오의 릴리스 태그와 겹치지 않는다.
        plan = compiler.compile_plan("f47-r-banking-api-release-account-directory-request-flood")
        argv, stdin = k8s_image.build_invocation(plan, "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-banking", "testbed-api", "api-service",
                                    "core-banking-api:latest", "core-banking-api:1.4.0"])
        self.assertNotIn("F47-R", argv)
        self.assertEqual(stdin, k8s_image.RELEASE_SCRIPT)
        tags = [c["fault_image"] for sid, c in k8s_image.CONTRACTS.items() if sid != "F47-R"]
        self.assertNotIn(k8s_image.CONTRACTS["F47-R"]["fault_image"], tags)
        image = json.loads((ROOT / "fault-images" / "f47-r" / "image.json").read_text())
        self.assertEqual((image["base"], image["fault"]),
                         (k8s_image.CONTRACTS["F47-R"]["baseline_image"], k8s_image.CONTRACTS["F47-R"]["fault_image"]))
        patch = (ROOT / "fault-images" / "f47-r" / "api-service.patch").read_text()
        added = "\n".join(line for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
        for word in ("F47", "f47", "fault", "bug", "chaos", "scenario"):
            self.assertNotIn(word, added)

    def test_k8s_image_release_mode_rolls_food_restaurant(self) -> None:
        # F49-R: 같은 release 스크립트로 food restaurant 를 1.4.0 으로 롤아웃한다(tb-w3, 노드는 스크립트가
        # Deployment 의 nodeSelector 에서 읽는다). 릴리스 태그는 fault-images/f49-r/image.json 과 같아야 하고,
        # 다른 시나리오의 릴리스 태그와 겹치지 않는다. 패치는 restaurant-service/src 안만 고친다.
        plan = compiler.compile_plan("f49-r-food-restaurant-release-detail-response-shape-breaks-order")
        argv, stdin = k8s_image.build_invocation(plan, "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-food", "testbed-restaurant", "restaurant-service",
                                    "food-delivery-restaurant:latest", "food-delivery-restaurant:1.4.0"])
        self.assertNotIn("F49-R", argv)
        self.assertEqual(stdin, k8s_image.RELEASE_SCRIPT)
        tags = [c["fault_image"] for sid, c in k8s_image.CONTRACTS.items() if sid != "F49-R"]
        self.assertNotIn(k8s_image.CONTRACTS["F49-R"]["fault_image"], tags)
        image = json.loads((ROOT / "fault-images" / "f49-r" / "image.json").read_text())
        self.assertEqual((image["base"], image["fault"]),
                         (k8s_image.CONTRACTS["F49-R"]["baseline_image"], k8s_image.CONTRACTS["F49-R"]["fault_image"]))
        patch = (ROOT / "fault-images" / "f49-r" / "restaurant-service.patch").read_text()
        for line in patch.splitlines():
            if line.startswith(("+++ ", "--- ")) and not line.endswith("/dev/null"):
                self.assertTrue(line[4:].startswith(("a/food-delivery/restaurant-service/src/",
                                                     "b/food-delivery/restaurant-service/src/")), line)
        added = "\n".join(line for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
        for word in ("F49", "f49", "fault", "bug", "chaos", "scenario"):
            self.assertNotIn(word, added)

    def test_k8s_image_release_mode_rolls_food_dispatch(self) -> None:
        # F33-H: 같은 release 스크립트로 food dispatch 를 1.6.0 으로 롤아웃한다(tb-w3, 노드는 스크립트가
        # Deployment 의 nodeSelector 에서 읽는다). 릴리스 태그는 fault-images/f33-h/image.json 과 같아야 하고,
        # 다른 시나리오의 릴리스 태그와 겹치지 않는다. 패치는 dispatch-service/src 안만 고친다.
        plan = compiler.compile_plan("f33-h-food-dispatch-release-list-total-count-saturates-mysql")
        argv, stdin = k8s_image.build_invocation(plan, "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-food", "testbed-dispatch", "dispatch-service",
                                    "food-delivery-dispatch:latest", "food-delivery-dispatch:1.6.0"])
        self.assertNotIn("F33-H", argv)
        self.assertEqual(stdin, k8s_image.RELEASE_SCRIPT)
        tags = [c["fault_image"] for sid, c in k8s_image.CONTRACTS.items() if sid != "F33-H"]
        self.assertNotIn(k8s_image.CONTRACTS["F33-H"]["fault_image"], tags)
        image = json.loads((ROOT / "fault-images" / "f33-h" / "image.json").read_text())
        self.assertEqual((image["base"], image["fault"]),
                         (k8s_image.CONTRACTS["F33-H"]["baseline_image"], k8s_image.CONTRACTS["F33-H"]["fault_image"]))
        patch = (ROOT / "fault-images" / "f33-h" / "dispatch-service.patch").read_text()
        for line in patch.splitlines():
            if line.startswith(("+++ ", "--- ")) and not line.endswith("/dev/null"):
                self.assertTrue(line[4:].startswith(("a/food-delivery/dispatch-service/src/",
                                                     "b/food-delivery/dispatch-service/src/")), line)
        added = "\n".join(line for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
        for word in ("F33", "f33", "fault", "bug", "chaos", "scenario"):
            self.assertNotIn(word, added)

    def test_k8s_image_release_mode_rolls_food_order(self) -> None:
        # F49-H: 같은 release 스크립트로 food order 를 2.3.0 으로 롤아웃한다(tb-w3, 노드는 스크립트가
        # Deployment 의 nodeSelector 에서 읽는다). 릴리스 태그는 fault-images/f49-h/image.json 과 같아야 하고,
        # 다른 시나리오의 릴리스 태그와 겹치지 않는다. 패치는 order-service/src 안만 고친다.
        plan = compiler.compile_plan("f49-h-food-order-release-date-format-breaks-dispatch-response")
        argv, stdin = k8s_image.build_invocation(plan, "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-food", "testbed-order", "order-service",
                                    "food-delivery-order:latest", "food-delivery-order:2.3.0"])
        self.assertNotIn("F49-H", argv)
        self.assertEqual(stdin, k8s_image.RELEASE_SCRIPT)
        tags = [c["fault_image"] for sid, c in k8s_image.CONTRACTS.items() if sid != "F49-H"]
        self.assertNotIn(k8s_image.CONTRACTS["F49-H"]["fault_image"], tags)
        image = json.loads((ROOT / "fault-images" / "f49-h" / "image.json").read_text())
        self.assertEqual((image["base"], image["fault"]),
                         (k8s_image.CONTRACTS["F49-H"]["baseline_image"], k8s_image.CONTRACTS["F49-H"]["fault_image"]))
        patch = (ROOT / "fault-images" / "f49-h" / "order-service.patch").read_text()
        for line in patch.splitlines():
            if line.startswith(("+++ ", "--- ")) and not line.endswith("/dev/null"):
                self.assertTrue(line[4:].startswith(("a/food-delivery/order-service/src/",
                                                     "b/food-delivery/order-service/src/")), line)
        added = "\n".join(line for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
        for word in ("F49", "f49", "fault", "bug", "chaos", "scenario"):
            self.assertNotIn(word, added)

    def test_k8s_image_release_mode_rolls_food_dispatch_second_release(self) -> None:
        # F43-P: 같은 release 스크립트로 food dispatch 를 1.7.0 으로 롤아웃한다(tb-w3, 노드는 스크립트가
        # Deployment 의 nodeSelector 에서 읽는다). F33-H 도 dispatch 릴리스(1.6.0)를 쓰므로 태그가 겹치지 않아야
        # 둘 다 "노드에 릴리스 없음" preflight 를 지킨다. 패치는 dispatch-service/src 안만 고친다.
        plan = compiler.compile_plan("f43-p-food-dispatch-release-connection-leak-rejects-orders")
        argv, stdin = k8s_image.build_invocation(plan, "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-food", "testbed-dispatch", "dispatch-service",
                                    "food-delivery-dispatch:latest", "food-delivery-dispatch:1.7.0"])
        self.assertNotIn("F43-P", argv)
        self.assertEqual(stdin, k8s_image.RELEASE_SCRIPT)
        tags = [c["fault_image"] for sid, c in k8s_image.CONTRACTS.items() if sid != "F43-P"]
        self.assertNotIn(k8s_image.CONTRACTS["F43-P"]["fault_image"], tags)
        image = json.loads((ROOT / "fault-images" / "f43-p" / "image.json").read_text())
        self.assertEqual((image["base"], image["fault"]),
                         (k8s_image.CONTRACTS["F43-P"]["baseline_image"], k8s_image.CONTRACTS["F43-P"]["fault_image"]))
        patch = (ROOT / "fault-images" / "f43-p" / "dispatch-service.patch").read_text()
        for line in patch.splitlines():
            if line.startswith(("+++ ", "--- ")) and not line.endswith("/dev/null"):
                self.assertTrue(line[4:].startswith(("a/food-delivery/dispatch-service/src/",
                                                     "b/food-delivery/dispatch-service/src/")), line)
        added = "\n".join(line for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
        for word in ("F43", "f43", "fault", "bug", "chaos", "scenario"):
            self.assertNotIn(word, added)

    def test_k8s_image_release_mode_rolls_banking_transfer_third_release(self) -> None:
        # F40-H: 같은 release 스크립트로 banking transfer 를 2.3.0 으로 롤아웃한다(tb-w2). transfer 의 다른 릴리스
        # F17-H(2.1.0, 노드에 없어야 하는 태그), F42-R(2.2.0)과 태그가 겹치지 않아야 셋 다 preflight 를 지킨다.
        # 패치는 transfer-service/src 안만 고친다.
        plan = compiler.compile_plan("f40-h-banking-transfer-release-whole-won-check-rejects-commerce-settlement")
        argv, stdin = k8s_image.build_invocation(plan, "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-banking", "testbed-transfer", "transfer-service",
                                    "core-banking-transfer:latest", "core-banking-transfer:2.3.0"])
        self.assertNotIn("F40-H", argv)
        self.assertEqual(stdin, k8s_image.RELEASE_SCRIPT)
        tags = [c["fault_image"] for sid, c in k8s_image.CONTRACTS.items() if sid != "F40-H"]
        self.assertNotIn(k8s_image.CONTRACTS["F40-H"]["fault_image"], tags)
        image = json.loads((ROOT / "fault-images" / "f40-h" / "image.json").read_text())
        self.assertEqual((image["base"], image["fault"]),
                         (k8s_image.CONTRACTS["F40-H"]["baseline_image"], k8s_image.CONTRACTS["F40-H"]["fault_image"]))
        patch = (ROOT / "fault-images" / "f40-h" / "transfer-service.patch").read_text()
        for line in patch.splitlines():
            if line.startswith(("+++ ", "--- ")) and not line.endswith("/dev/null"):
                self.assertTrue(line[4:].startswith(("a/core-banking/transfer-service/src/",
                                                     "b/core-banking/transfer-service/src/")), line)
        added = "\n".join(line for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
        for word in ("F40", "f40", "fault", "bug", "chaos", "scenario"):
            self.assertNotIn(word, added)

    def test_k8s_image_release_mode_rolls_food_restaurant_live_popular_ranking(self) -> None:
        # F42-P: 같은 release 스크립트로 food restaurant 를 1.5.0 으로 롤아웃한다(tb-w3). F49-R 도 restaurant 를 쓰지만
        # 태그가 1.4.0 이라 겹치지 않는다. 릴리스 태그는 fault-images/f42-p/image.json 과 같아야 하고, 패치는
        # restaurant-service/src 안만 고친다.
        plan = compiler.compile_plan("f42-p-food-restaurant-release-live-popular-menu-aggregation-saturates-mysql")
        argv, stdin = k8s_image.build_invocation(plan, "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-food", "testbed-restaurant", "restaurant-service",
                                    "food-delivery-restaurant:latest", "food-delivery-restaurant:1.5.0"])
        self.assertNotIn("F42-P", argv)
        self.assertEqual(stdin, k8s_image.RELEASE_SCRIPT)
        tags = [c["fault_image"] for sid, c in k8s_image.CONTRACTS.items() if sid != "F42-P"]
        self.assertNotIn(k8s_image.CONTRACTS["F42-P"]["fault_image"], tags)
        image = json.loads((ROOT / "fault-images" / "f42-p" / "image.json").read_text())
        self.assertEqual((image["base"], image["fault"]),
                         (k8s_image.CONTRACTS["F42-P"]["baseline_image"], k8s_image.CONTRACTS["F42-P"]["fault_image"]))
        patch = (ROOT / "fault-images" / "f42-p" / "restaurant-service.patch").read_text()
        for line in patch.splitlines():
            if line.startswith(("+++ ", "--- ")) and not line.endswith("/dev/null"):
                self.assertTrue(line[4:].startswith(("a/food-delivery/restaurant-service/src/",
                                                     "b/food-delivery/restaurant-service/src/")), line)
        added = "\n".join(line for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
        for word in ("F42", "f42", "fault", "bug", "chaos", "scenario"):
            self.assertNotIn(word, added)

    def test_k8s_image_release_mode_rolls_food_payment(self) -> None:
        # F49-P: 같은 release 스크립트로 food payment 를 1.3.0 으로 롤아웃한다(tb-w3). 릴리스 태그는
        # fault-images/f49-p/image.json 과 같아야 하고, 다른 시나리오의 릴리스 태그와 겹치지 않는다.
        # 패치는 payment-service/src 안만 고친다.
        plan = compiler.compile_plan("f49-p-food-payment-release-payment-key-breaks-order")
        argv, stdin = k8s_image.build_invocation(plan, "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-food", "testbed-payment", "payment-service",
                                    "food-delivery-payment:latest", "food-delivery-payment:1.3.0"])
        self.assertNotIn("F49-P", argv)
        self.assertEqual(stdin, k8s_image.RELEASE_SCRIPT)
        tags = [c["fault_image"] for sid, c in k8s_image.CONTRACTS.items() if sid != "F49-P"]
        self.assertNotIn(k8s_image.CONTRACTS["F49-P"]["fault_image"], tags)
        image = json.loads((ROOT / "fault-images" / "f49-p" / "image.json").read_text())
        self.assertEqual((image["base"], image["fault"]),
                         (k8s_image.CONTRACTS["F49-P"]["baseline_image"], k8s_image.CONTRACTS["F49-P"]["fault_image"]))
        patch = (ROOT / "fault-images" / "f49-p" / "payment-service.patch").read_text()
        for line in patch.splitlines():
            if line.startswith(("+++ ", "--- ")) and not line.endswith("/dev/null"):
                self.assertTrue(line[4:].startswith(("a/food-delivery/payment-service/src/",
                                                     "b/food-delivery/payment-service/src/")), line)
        added = "\n".join(line for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
        for word in ("F49", "f49", "fault", "bug", "chaos", "scenario"):
            self.assertNotIn(word, added)

    def test_k8s_image_release_mode_rolls_food_dispatch_third_release(self) -> None:
        # F55-R: 같은 release 스크립트로 food dispatch 를 1.8.0 으로 롤아웃한다(tb-w3). F33-H(1.6.0), F43-P(1.7.0)도
        # dispatch 릴리스를 쓰므로 태그가 겹치지 않아야 셋 다 "노드에 릴리스 없음" preflight 를 지킨다. 패치는
        # dispatch-service/src 안만 고친다.
        plan = compiler.compile_plan("f55-r-food-dispatch-release-client-rate-limit-rejects-orders")
        argv, stdin = k8s_image.build_invocation(plan, "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-food", "testbed-dispatch", "dispatch-service",
                                    "food-delivery-dispatch:latest", "food-delivery-dispatch:1.8.0"])
        self.assertNotIn("F55-R", argv)
        self.assertEqual(stdin, k8s_image.RELEASE_SCRIPT)
        tags = [c["fault_image"] for sid, c in k8s_image.CONTRACTS.items() if sid != "F55-R"]
        self.assertNotIn(k8s_image.CONTRACTS["F55-R"]["fault_image"], tags)
        image = json.loads((ROOT / "fault-images" / "f55-r" / "image.json").read_text())
        self.assertEqual((image["base"], image["fault"]),
                         (k8s_image.CONTRACTS["F55-R"]["baseline_image"], k8s_image.CONTRACTS["F55-R"]["fault_image"]))
        patch = (ROOT / "fault-images" / "f55-r" / "dispatch-service.patch").read_text()
        for line in patch.splitlines():
            if line.startswith(("+++ ", "--- ")) and not line.endswith("/dev/null"):
                self.assertTrue(line[4:].startswith(("a/food-delivery/dispatch-service/src/",
                                                     "b/food-delivery/dispatch-service/src/")), line)
        added = "\n".join(line for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
        for word in ("F55", "f55", "fault", "bug", "chaos", "scenario"):
            self.assertNotIn(word, added)

    def test_f55r_relies_on_order_dispatch_client_turning_4xx_into_5xx(self) -> None:
        # F55-R 은 dispatch 릴리스가 돌려주는 429 를 food order 의 DispatchClient 가 502, 503 으로 바꿔 order 서버 스팬
        # 오류와 오류율이 오르는 데 기댄다. 다른 클라이언트들은 4xx 를 그대로 전파하도록 고쳐졌는데(dc44dc6, 4d2df10)
        # DispatchClient 만 모든 RestClientException 을 ServiceException 으로 바꾼다. 이 매핑이 바뀌어 4xx 가 그대로
        # 전파되면 F55-R 은 429 단일 신호(막힌 restaurant 판과 같은 벽)가 되므로 시나리오를 다시 평가해야 한다.
        source = (ROOT.parent.parent / "food-delivery" / "order-service" / "src" / "main" / "java" / "com"
                  / "fooddelivery" / "order" / "client" / "DispatchClient.java").read_text()
        self.assertNotIn("ClientErrorException", source)
        self.assertNotIn("is4xxClientError", source)
        for method, status in (("checkCapacity", "BAD_GATEWAY"), ("dispatchCourier", "SERVICE_UNAVAILABLE")):
            body = source[source.index(f" {method}("):]
            body = body[:body.index("\n    }\n")]
            self.assertIn("catch (RestClientException ex)", body, method)
            self.assertIn(f"throw new ServiceException(HttpStatus.{status},", body, method)
        config = (ROOT.parent.parent / "food-delivery" / "order-service" / "src" / "main" / "resources"
                  / "application.yml").read_text()
        dispatch_blocks = re.findall(r"\n      dispatch:\n((?:        .*\n)+)", config)
        self.assertEqual(len(dispatch_blocks), 2)
        for block in dispatch_blocks:
            self.assertIn("ignore-exceptions:\n          - com.fooddelivery.common.exception.ClientErrorException", block)

    def test_k8s_image_release_mode_rolls_banking_account_second_release(self) -> None:
        # F51-H: 같은 release 스크립트로 banking account 를 1.4.0 으로 롤아웃한다(tb-w2). F41-R(1.3.0)도 account 릴리스를
        # 쓰므로 태그가 겹치지 않아야 둘 다 "노드에 릴리스 없음" preflight 를 지킨다. 패치는 account-service/src 안만 고친다.
        plan = compiler.compile_plan("f51-h-banking-account-release-connection-leak-drains-pool")
        argv, stdin = k8s_image.build_invocation(plan, "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-banking", "testbed-account", "account-service",
                                    "core-banking-account:latest", "core-banking-account:1.4.0"])
        self.assertNotIn("F51-H", argv)
        self.assertEqual(stdin, k8s_image.RELEASE_SCRIPT)
        tags = [c["fault_image"] for sid, c in k8s_image.CONTRACTS.items() if sid != "F51-H"]
        self.assertNotIn(k8s_image.CONTRACTS["F51-H"]["fault_image"], tags)
        image = json.loads((ROOT / "fault-images" / "f51-h" / "image.json").read_text())
        self.assertEqual((image["base"], image["fault"]),
                         (k8s_image.CONTRACTS["F51-H"]["baseline_image"], k8s_image.CONTRACTS["F51-H"]["fault_image"]))
        patch = (ROOT / "fault-images" / "f51-h" / "account-service.patch").read_text()
        for line in patch.splitlines():
            if line.startswith(("+++ ", "--- ")) and not line.endswith("/dev/null"):
                self.assertTrue(line[4:].startswith(("a/core-banking/account-service/src/",
                                                     "b/core-banking/account-service/src/")), line)
        added = "\n".join(line for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
        for word in ("F51", "f51", "fault", "bug", "chaos", "scenario", "leak"):
            self.assertNotIn(word, added)

    def test_f51h_relies_on_account_probes_and_pool_settings(self) -> None:
        # F51-H 는 새 릴리스가 빌린 연결을 돌려주지 않아 account 풀(10)이 굳으면 readiness(/actuator/health 의 DataSource
        # 확인)가 실패해 엔드포인트에서 빠지고, liveness(/actuator/health/liveness)는 DB 를 보지 않아 재시작으로 연결이
        # 풀리지 않는 데 기댄다. 프로브 경로나 풀 크기, 연결 대기 시간이 바뀌면 피해 모양(지속 대 재시작 반복)과
        # 성공 판정 시간이 달라지므로 시나리오를 다시 평가해야 한다.
        manifest = (ROOT.parent.parent / "core-banking" / "k8s" / "21-account-service.yaml").read_text()
        readiness = manifest[manifest.index("readinessProbe:"):manifest.index("livenessProbe:")]
        liveness = manifest[manifest.index("livenessProbe:"):manifest.index("resources:", manifest.index("livenessProbe:"))]
        self.assertIn("path: /actuator/health\n", readiness)
        self.assertIn("path: /actuator/health/liveness", liveness)
        config = (ROOT.parent.parent / "core-banking" / "account-service" / "src" / "main" / "resources"
                  / "application.yml").read_text()
        self.assertIn("maximum-pool-size: ${DB_POOL_MAX:10}", config)
        self.assertIn("connection-timeout: 3000", config)
        self.assertIn("probes:\n        enabled: true", config)

    def test_k8s_image_release_mode_rolls_food_order_policy_release(self) -> None:
        # F58-R: 같은 release 스크립트로 food order 를 2.5.0 으로 롤아웃한다(tb-w3). F49-H(2.3.0)도 order 릴리스를
        # 쓰므로 태그가 겹치지 않아야 둘 다 "노드에 릴리스 없음" preflight 를 지킨다. 패치는 order-service/src 안만 고친다.
        plan = compiler.compile_plan("f58-r-food-order-release-reads-unsynced-setting")
        argv, stdin = k8s_image.build_invocation(plan, "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-food", "testbed-order", "order-service",
                                    "food-delivery-order:latest", "food-delivery-order:2.5.0"])
        self.assertNotIn("F58-R", argv)
        self.assertEqual(stdin, k8s_image.RELEASE_SCRIPT)
        tags = [c["fault_image"] for sid, c in k8s_image.CONTRACTS.items() if sid != "F58-R"]
        self.assertNotIn(k8s_image.CONTRACTS["F58-R"]["fault_image"], tags)
        image = json.loads((ROOT / "fault-images" / "f58-r" / "image.json").read_text())
        self.assertEqual((image["base"], image["fault"]),
                         (k8s_image.CONTRACTS["F58-R"]["baseline_image"], k8s_image.CONTRACTS["F58-R"]["fault_image"]))
        patch = (ROOT / "fault-images" / "f58-r" / "order-service.patch").read_text()
        for line in patch.splitlines():
            if line.startswith(("+++ ", "--- ")) and not line.endswith("/dev/null"):
                self.assertTrue(line[4:].startswith(("a/food-delivery/order-service/src/",
                                                     "b/food-delivery/order-service/src/")), line)
        added = "\n".join(line for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
        for word in ("F58", "f58", "fault", "bug", "chaos", "scenario"):
            self.assertNotIn(word, added)
        self.assertIn('getRequiredProperty("order.policy.minimum-amount", BigDecimal.class)', added)

    def test_f58r_relies_on_policy_key_missing_from_production_config(self) -> None:
        # F58-R 은 릴리스가 읽는 정책값 order.policy.minimum-amount 가 릴리스의 개발 프로필 파일에만 있고 운영 쪽
        # (기본 application.yml, ConfigMap service-config, order 파드 env, 활성 프로필)에는 없다는 데 기댄다. 운영 설정에
        # 그 값이 생기거나 dev 프로필이 켜지면 주문이 실패하지 않으므로 시나리오를 다시 평가해야 한다.
        food = ROOT.parent.parent / "food-delivery"
        config = (food / "order-service" / "src" / "main" / "resources" / "application.yml").read_text()
        configmap = (food / "k8s" / "02-configmaps.yaml").read_text()
        deploy = (food / "k8s" / "20-order-deploy.yaml").read_text()
        for text in (config, configmap, deploy):
            self.assertNotIn("minimum-amount", text)
            self.assertNotIn("MINIMUM_AMOUNT", text)
            self.assertNotIn("SPRING_PROFILES_ACTIVE", text)
        self.assertIn("path: /actuator/health\n", deploy[deploy.index("readinessProbe:"):])
        patch = (ROOT / "fault-images" / "f58-r" / "order-service.patch").read_text()
        self.assertIn("+++ b/food-delivery/order-service/src/main/resources/application-dev.yml", patch)

    def test_k8s_image_release_mode_rolls_food_restaurant_policy_release(self) -> None:
        # F36-H: 같은 release 스크립트로 food restaurant 를 1.6.0 으로 롤아웃한다(tb-w3). F49-R(1.4.0), F42-P(1.5.0)도
        # restaurant 릴리스를 쓰므로 태그가 겹치지 않아야 셋 다 "노드에 릴리스 없음" preflight 를 지킨다. 패치는
        # restaurant-service/src 안만 고친다.
        plan = compiler.compile_plan("f36-h-food-restaurant-release-reads-unsynced-setting")
        argv, stdin = k8s_image.build_invocation(plan, "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-food", "testbed-restaurant", "restaurant-service",
                                    "food-delivery-restaurant:latest", "food-delivery-restaurant:1.6.0"])
        self.assertNotIn("F36-H", argv)
        self.assertEqual(stdin, k8s_image.RELEASE_SCRIPT)
        tags = [c["fault_image"] for sid, c in k8s_image.CONTRACTS.items() if sid != "F36-H"]
        self.assertNotIn(k8s_image.CONTRACTS["F36-H"]["fault_image"], tags)
        image = json.loads((ROOT / "fault-images" / "f36-h" / "image.json").read_text())
        self.assertEqual((image["base"], image["fault"]),
                         (k8s_image.CONTRACTS["F36-H"]["baseline_image"], k8s_image.CONTRACTS["F36-H"]["fault_image"]))
        patch = (ROOT / "fault-images" / "f36-h" / "restaurant-service.patch").read_text()
        for line in patch.splitlines():
            if line.startswith(("+++ ", "--- ")) and not line.endswith("/dev/null"):
                self.assertTrue(line[4:].startswith(("a/food-delivery/restaurant-service/src/",
                                                     "b/food-delivery/restaurant-service/src/")), line)
        added = "\n".join(line for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
        for word in ("F36", "f36", "fault", "bug", "chaos", "scenario"):
            self.assertNotIn(word, added)
        self.assertIn('getRequiredProperty("restaurant.policy.paused-regions")', added)

    def test_f36h_relies_on_policy_key_missing_from_production_config(self) -> None:
        # F36-H 는 릴리스가 가게 상세마다 읽는 정책값 restaurant.policy.paused-regions 가 릴리스의 개발 프로필 파일에만
        # 있고 운영 쪽(기본 application.yml, ConfigMap service-config, restaurant 파드 env, 활성 프로필)에는 없다는 데
        # 기댄다. 운영 설정에 그 값이 생기거나 dev 프로필이 켜지면 가게 상세가 실패하지 않으므로 시나리오를 다시 평가해야 한다.
        food = ROOT.parent.parent / "food-delivery"
        config = (food / "restaurant-service" / "src" / "main" / "resources" / "application.yml").read_text()
        configmap = (food / "k8s" / "02-configmaps.yaml").read_text()
        deploy = (food / "k8s" / "21-restaurant-deploy.yaml").read_text()
        for text in (config, configmap, deploy):
            self.assertNotIn("paused-regions", text)
            self.assertNotIn("PAUSED_REGIONS", text)
            self.assertNotIn("SPRING_PROFILES_ACTIVE", text)
        self.assertIn("path: /actuator/health\n", deploy[deploy.index("readinessProbe:"):])
        patch = (ROOT / "fault-images" / "f36-h" / "restaurant-service.patch").read_text()
        self.assertIn("+++ b/food-delivery/restaurant-service/src/main/resources/application-dev.yml", patch)

    def test_k8s_image_release_mode_rolls_banking_account_probe_cleanup_release(self) -> None:
        # F41-H: 같은 release 스크립트로 banking account 를 1.5.0 으로 롤아웃한다(tb-w2). F41-R(1.3.0), F51-H(1.4.0)도
        # account 릴리스를 쓰므로 태그가 겹치지 않아야 셋 다 "노드에 릴리스 없음" preflight 를 지킨다. 패치는
        # account-service/src 안만 고친다.
        plan = compiler.compile_plan("f41-h-banking-account-release-drops-liveness-endpoint")
        argv, stdin = k8s_image.build_invocation(plan, "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-banking", "testbed-account", "account-service",
                                    "core-banking-account:latest", "core-banking-account:1.5.0"])
        self.assertNotIn("F41-H", argv)
        self.assertEqual(stdin, k8s_image.RELEASE_SCRIPT)
        tags = [c["fault_image"] for sid, c in k8s_image.CONTRACTS.items() if sid != "F41-H"]
        self.assertNotIn(k8s_image.CONTRACTS["F41-H"]["fault_image"], tags)
        image = json.loads((ROOT / "fault-images" / "f41-h" / "image.json").read_text())
        self.assertEqual((image["base"], image["fault"]),
                         (k8s_image.CONTRACTS["F41-H"]["baseline_image"], k8s_image.CONTRACTS["F41-H"]["fault_image"]))
        patch = (ROOT / "fault-images" / "f41-h" / "account-service.patch").read_text()
        for line in patch.splitlines():
            if line.startswith(("+++ ", "--- ")) and not line.endswith("/dev/null"):
                self.assertTrue(line[4:].startswith(("a/core-banking/account-service/src/",
                                                     "b/core-banking/account-service/src/")), line)
        added = "\n".join(line for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
        for word in ("F41", "f41", "fault", "bug", "chaos", "scenario"):
            self.assertNotIn(word, added)
        self.assertIn("+        enabled: false", patch)
        self.assertIn("-        enabled: true", patch)

    def test_f41h_relies_on_liveness_probe_using_health_sub_path(self) -> None:
        # F41-H 는 매니페스트의 시작, 준비 프로브가 /actuator/health 이고 liveness 만 /actuator/health/liveness 를 쓴다는 데
        # 기댄다. 릴리스가 하위 그룹을 끄면 새 파드는 Ready 가 되어 옛 파드를 대체한 뒤 liveness 404 로만 죽는다. 프로브
        # 경로가 바뀌거나 운영 설정이 management.endpoint.health.probes.enabled 를 따로 켜면 시나리오를 다시 평가해야 한다.
        banking = ROOT.parent.parent / "core-banking"
        deploy = (banking / "k8s" / "21-account-service.yaml").read_text()
        self.assertIn("path: /actuator/health\n", deploy[deploy.index("startupProbe:"):deploy.index("readinessProbe:")])
        self.assertIn("path: /actuator/health\n", deploy[deploy.index("readinessProbe:"):deploy.index("livenessProbe:")])
        self.assertIn("path: /actuator/health/liveness\n", deploy[deploy.index("livenessProbe:"):])
        configmap = (banking / "k8s" / "02-configmaps.yaml").read_text()
        for text in (configmap, deploy):
            self.assertNotIn("PROBES_ENABLED", text)
            self.assertNotIn("probes.enabled", text)
        config = (banking / "account-service" / "src" / "main" / "resources" / "application.yml").read_text()
        self.assertIn("probes:\n        enabled: true", config)

    def test_k8s_image_release_mode_rolls_banking_account_transfer_receipt_release(self) -> None:
        # F39-H: 같은 release 스크립트로 banking account 를 1.6.0 으로 롤아웃한다(tb-w2). F41-R(1.3.0), F51-H(1.4.0),
        # F41-H(1.5.0)도 account 릴리스를 쓰므로 태그가 겹치지 않아야 넷 다 "노드에 릴리스 없음" preflight 를 지킨다.
        # 패치는 account-service/src 안만 고치고, 바꾸는 것은 이체 접수 응답의 status 모양뿐이다.
        plan = compiler.compile_plan("f39-h-banking-account-release-transfer-receipt-breaks-api")
        argv, stdin = k8s_image.build_invocation(plan, "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-banking", "testbed-account", "account-service",
                                    "core-banking-account:latest", "core-banking-account:1.6.0"])
        self.assertNotIn("F39-H", argv)
        self.assertEqual(stdin, k8s_image.RELEASE_SCRIPT)
        tags = [c["fault_image"] for sid, c in k8s_image.CONTRACTS.items() if sid != "F39-H"]
        self.assertNotIn(k8s_image.CONTRACTS["F39-H"]["fault_image"], tags)
        image = json.loads((ROOT / "fault-images" / "f39-h" / "image.json").read_text())
        self.assertEqual((image["base"], image["fault"]),
                         (k8s_image.CONTRACTS["F39-H"]["baseline_image"], k8s_image.CONTRACTS["F39-H"]["fault_image"]))
        patch = (ROOT / "fault-images" / "f39-h" / "account-service.patch").read_text()
        for line in patch.splitlines():
            if line.startswith(("+++ ", "--- ")) and not line.endswith("/dev/null"):
                self.assertTrue(line[4:].startswith(("a/core-banking/account-service/src/",
                                                     "b/core-banking/account-service/src/")), line)
        added = "\n".join(line for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
        for word in ("F39", "f39", "fault", "bug", "chaos", "scenario"):
            self.assertNotIn(word, added)
        self.assertIn("+    public ResponseEntity<TransferReceiptResponse> transfer(@RequestBody TransferRequest request) {", patch)
        self.assertIn("+    public record Status(String code, String label) {}", patch)

    def test_f39h_relies_on_api_reading_account_transfer_answer_as_shared_dto(self) -> None:
        # F39-H 는 POST /api/accounts/transfer 의 유일한 호출자가 api 이고, api 가 그 응답을 status 가 String 인 공용
        # TransferResponse 로 읽는다는 데 기댄다. 호출자가 응답을 문자열로 그대로 넘기거나(거래 내역 프록시처럼) DTO 가
        # 바뀌면 릴리스가 api 를 깨지 않으므로 시나리오를 다시 평가해야 한다.
        banking = ROOT.parent.parent / "core-banking"
        client = (banking / "api-service" / "src" / "main" / "java" / "com" / "corebanking" / "api" / "client"
                  / "AccountClient.java").read_text()
        self.assertIn('.uri("/api/accounts/transfer")', client)
        self.assertIn(".body(TransferResponse.class);", client)
        dto = (banking / "shop-common" / "src" / "main" / "java" / "com" / "corebanking" / "common" / "dto"
               / "TransferResponse.java").read_text()
        self.assertIn("String status", dto)
        controller = (banking / "account-service" / "src" / "main" / "java" / "com" / "corebanking" / "account"
                      / "controller" / "AccountController.java").read_text()
        self.assertIn("public ResponseEntity<TransferResponse> transfer(@RequestBody TransferRequest request)", controller)

    def test_k8s_image_release_mode_rolls_banking_api_thread_leak_release(self) -> None:
        # F63-R: 같은 release 스크립트로 banking api 를 1.5.0 으로 롤아웃한다(tb-w2). F47-R 도 api 릴리스(1.4.0)를 쓰므로
        # 태그가 겹치지 않아야 둘 다 "노드에 릴리스 없음" preflight 를 지킨다. 패치는 api-service/src 안만 고친다.
        plan = compiler.compile_plan("f63-r-banking-api-release-leaks-thread-per-request")
        argv, stdin = k8s_image.build_invocation(plan, "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-banking", "testbed-api", "api-service",
                                    "core-banking-api:latest", "core-banking-api:1.5.0"])
        self.assertNotIn("F63-R", argv)
        self.assertEqual(stdin, k8s_image.RELEASE_SCRIPT)
        tags = [c["fault_image"] for sid, c in k8s_image.CONTRACTS.items() if sid != "F63-R"]
        self.assertNotIn(k8s_image.CONTRACTS["F63-R"]["fault_image"], tags)
        image = json.loads((ROOT / "fault-images" / "f63-r" / "image.json").read_text())
        self.assertEqual((image["base"], image["fault"]),
                         (k8s_image.CONTRACTS["F63-R"]["baseline_image"], k8s_image.CONTRACTS["F63-R"]["fault_image"]))
        patch = (ROOT / "fault-images" / "f63-r" / "api-service.patch").read_text()
        for line in patch.splitlines():
            if line.startswith(("+++ ", "--- ")) and not line.endswith("/dev/null"):
                self.assertTrue(line[4:].startswith(("a/core-banking/api-service/src/",
                                                     "b/core-banking/api-service/src/")), line)
        added = "\n".join(line for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
        for word in ("F63", "f63", "fault", "bug", "chaos", "scenario", "leak"):
            self.assertNotIn(word, added)
        # 누수의 고리: 요청마다 고정 크기 풀(작업 스레드가 비데몬이고 풀을 붙잡아 회수되지 않음)을 만들고 닫지 않는다.
        # newSingleThreadExecutor 는 감싼 객체가 회수될 때 스스로 닫혀 누수가 되지 않는다(2026-10-11 로컬 실측).
        self.assertIn("Executors.newFixedThreadPool(1)", added)
        self.assertNotIn("newSingleThreadExecutor", added)
        self.assertNotIn("shutdown", added)
        self.assertIn('withinDeadline("transfer submit"', added)
        self.assertIn('withinDeadline("transfer list"', added)

    def test_f63r_relies_on_api_memory_limit_and_default_jvm_options(self) -> None:
        # F63-R 의 주기(약 8분에 한도 도달)는 매니페스트 api 의 메모리 한도 1Gi 와 JVM 옵션이 OTel 에이전트뿐이라는 데
        # 기댄다. 한도나 스레드 스택(-Xss)이 바뀌거나 nginx 가 /api/transfers 를 api 로 보내지 않으면 다시 평가해야 한다.
        banking = ROOT.parent.parent / "core-banking"
        deploy = (banking / "k8s" / "20-api-service.yaml").read_text()
        self.assertIn("memory: 1Gi", deploy[deploy.index("limits:"):])
        self.assertIn('value: "-javaagent:/opt/apm/opentelemetry-javaagent.jar"', deploy)
        self.assertNotIn("-Xss", deploy)
        configmap = (banking / "k8s" / "02-configmaps.yaml").read_text()
        self.assertIn("location /api/transfers {\n            proxy_pass http://api-service;", configmap)
        self.assertIn("location /api/accounts {\n            proxy_pass http://account-service;", configmap)


    def test_k8s_image_release_mode_rolls_banking_api_transfer_limit_release(self) -> None:
        # F37-H: 같은 release 스크립트로 banking api 를 1.6.0 으로 롤아웃한다(tb-w2). F47-R(1.4.0), F63-R(1.5.0)도 api
        # 릴리스를 쓰므로 태그가 겹치지 않아야 셋 다 "노드에 릴리스 없음" preflight 를 지킨다. 패치는 api-service/src 안만 고친다.
        plan = compiler.compile_plan("f37-h-banking-api-release-reads-unsynced-transfer-limit")
        argv, stdin = k8s_image.build_invocation(plan, "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-banking", "testbed-api", "api-service",
                                    "core-banking-api:latest", "core-banking-api:1.6.0"])
        self.assertNotIn("F37-H", argv)
        self.assertEqual(stdin, k8s_image.RELEASE_SCRIPT)
        tags = [c["fault_image"] for sid, c in k8s_image.CONTRACTS.items() if sid != "F37-H"]
        self.assertNotIn(k8s_image.CONTRACTS["F37-H"]["fault_image"], tags)
        image = json.loads((ROOT / "fault-images" / "f37-h" / "image.json").read_text())
        self.assertEqual((image["base"], image["fault"]),
                         (k8s_image.CONTRACTS["F37-H"]["baseline_image"], k8s_image.CONTRACTS["F37-H"]["fault_image"]))
        patch = (ROOT / "fault-images" / "f37-h" / "api-service.patch").read_text()
        for line in patch.splitlines():
            if line.startswith(("+++ ", "--- ")) and not line.endswith("/dev/null"):
                self.assertTrue(line[4:].startswith(("a/core-banking/api-service/src/",
                                                     "b/core-banking/api-service/src/")), line)
        added = "\n".join(line for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
        for word in ("F37", "f37", "fault", "bug", "chaos", "scenario"):
            self.assertNotIn(word, added)
        self.assertIn('getRequiredProperty("api.policy.single-transfer-limit", BigDecimal.class)', added)
        # 확인은 'Submitting transfer' 로그와 account 호출보다 앞에 있어 account 를 부르기 전에 실패한다.
        body = patch[patch.index("public TransferResponse submitTransfer"):]
        self.assertLess(body.index("getRequiredProperty"), body.index("accountClient.requestTransfer"))
        self.assertNotIn("listTransfers", added)

    def test_f37h_relies_on_policy_key_missing_from_production_config(self) -> None:
        # F37-H 는 릴리스가 이체마다 읽는 정책값 api.policy.single-transfer-limit 이 릴리스의 개발 프로필 파일에만 있고 운영
        # 쪽(기본 application.yml, ConfigMap service-config, api 파드 env, 활성 프로필)에는 없다는 데 기댄다. 운영 설정에 그
        # 값이 생기거나 dev 프로필이 켜지거나 api 예외 처리기가 IllegalStateException 을 다르게 다루면 다시 평가해야 한다.
        banking = ROOT.parent.parent / "core-banking"
        config = (banking / "api-service" / "src" / "main" / "resources" / "application.yml").read_text()
        configmap = (banking / "k8s" / "02-configmaps.yaml").read_text()
        deploy = (banking / "k8s" / "20-api-service.yaml").read_text()
        for text in (config, configmap, deploy):
            self.assertNotIn("single-transfer-limit", text)
            self.assertNotIn("SINGLE_TRANSFER_LIMIT", text)
            self.assertNotIn("SPRING_PROFILES_ACTIVE", text)
        self.assertIn("path: /actuator/health\n", deploy[deploy.index("readinessProbe:"):])
        handler = (banking / "api-service" / "src" / "main" / "java" / "com" / "corebanking" / "api" / "config"
                   / "GlobalExceptionHandler.java").read_text()
        self.assertEqual(handler.count("@ExceptionHandler("), 1)
        self.assertIn("@ExceptionHandler(ServiceException.class)", handler)
        patch = (ROOT / "fault-images" / "f37-h" / "api-service.patch").read_text()
        self.assertIn("+++ b/core-banking/api-service/src/main/resources/application-dev.yml", patch)
    def test_k8s_image_release_mode_rolls_commerce_notification_recipient_directory_release(self) -> None:
        # F16-P: 같은 release 스크립트로 commerce notification 을 1.3.0 으로 롤아웃한다(tb-w1). notification 의 다른 릴리스
        # 태그는 없지만 모든 계약의 태그가 서로 달라야 "노드에 릴리스 없음" preflight 를 지킨다. 패치는 notification-service/src
        # 안만 고친다.
        plan = compiler.compile_plan("f16-p-commerce-notification-release-user-directory-flood-breaks-auth")
        argv, stdin = k8s_image.build_invocation(plan, "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-commerce", "testbed-notification", "notification-service",
                                    "commerce-notification:latest", "commerce-notification:1.3.0"])
        self.assertNotIn("F16-P", argv)
        self.assertEqual(stdin, k8s_image.RELEASE_SCRIPT)
        tags = [c["fault_image"] for sid, c in k8s_image.CONTRACTS.items() if sid != "F16-P"]
        self.assertNotIn(k8s_image.CONTRACTS["F16-P"]["fault_image"], tags)
        image = json.loads((ROOT / "fault-images" / "f16-p" / "image.json").read_text())
        self.assertEqual((image["base"], image["fault"]),
                         (k8s_image.CONTRACTS["F16-P"]["baseline_image"], k8s_image.CONTRACTS["F16-P"]["fault_image"]))
        patch = (ROOT / "fault-images" / "f16-p" / "notification-service.patch").read_text()
        for line in patch.splitlines():
            if line.startswith(("+++ ", "--- ")) and not line.endswith("/dev/null"):
                self.assertTrue(line[4:].startswith(("a/commerce/notification-service/src/",
                                                     "b/commerce/notification-service/src/")), line)
        added = "\n".join(line for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
        for word in ("F16", "f16", "fault", "bug", "chaos", "scenario", "flood"):
            self.assertNotIn(word, added)
        # 홍수의 고리: 목록에 없는 이름마다 뒤에서(겹침 방지 없이) 새로 고치고, 짧은 페이지가 올 때까지 상한 100번 읽는다.
        self.assertIn("CompletableFuture.runAsync(this::refresh);", added)
        self.assertIn("while (chunk.size() >= PAGE_SIZE && page < MAX_PAGES);", added)
        self.assertIn("MAX_PAGES = 100;", added)
        self.assertIn('.uri("/api/users?page={page}&size={size}", page, size)', added)
        self.assertIn("recipientDirectory.findEmail(event.customerName())", added)

    def test_f16p_relies_on_unpaged_user_list_and_write_token_check(self) -> None:
        # F16-P 는 user-service 회원 목록 API 가 페이지 인자를 받지 않고 전부를 돌려준다는 데(그래서 새 notification 의
        # 페이지 읽기가 짧은 페이지를 못 만나 상한까지 간다), 주문 이벤트의 이메일이 비어 있고 이름이 회원 표의 이름과
        # 다르다는 데(그래서 이벤트마다 목록을 못 찾아 새로 고친다), 게이트웨이가 쓰기마다 user-service 로 토큰을 확인하고
        # notification 이 service-config 의 USER_SERVICE_URL 을 받는다는 데, 두 컨테이너의 CPU 한도가 500m 라는 데 기댄다.
        commerce = ROOT.parent.parent / "commerce"
        controller = (commerce / "user-service" / "src" / "main" / "java" / "com" / "commerce" / "user" / "controller"
                      / "UserController.java").read_text()
        listing = controller[controller.index("    @GetMapping\n"):controller.index('@GetMapping("/{id}")')]
        self.assertNotIn("page", listing)
        self.assertNotIn("Pageable", listing)
        service = (commerce / "user-service" / "src" / "main" / "java" / "com" / "commerce" / "user" / "service"
                   / "UserService.java").read_text()
        self.assertIn("users = userRepository.findAll();", service)
        guard = (commerce / "api-gateway" / "src" / "main" / "java" / "com" / "commerce" / "gateway" / "service"
                 / "AuthGuard.java").read_text()
        self.assertIn('Set.of("POST", "PUT", "DELETE", "PATCH")', guard)
        self.assertIn('.path("/api/users/verify-token")', guard)
        configmap = (commerce / "k8s" / "02-configmaps.yaml").read_text()
        self.assertIn("USER_SERVICE_URL: http://testbed-user:8085", configmap)
        notification = (commerce / "k8s" / "24-notification-service.yaml").read_text()
        self.assertIn("name: service-config", notification)
        self.assertIn("cpu: 500m", notification[notification.index("limits:"):])
        user = (commerce / "k8s" / "25-user-service.yaml").read_text()
        self.assertIn("cpu: 500m", user[user.index("limits:"):])
        self.assertIn("path: /actuator/health\n", user[user.index("readinessProbe:"):user.index("livenessProbe:")])
        order = (commerce / "order-service" / "src" / "main" / "java" / "com" / "commerce" / "order" / "service"
                 / "OrderService.java").read_text()
        self.assertIn("order.getCustomerEmail(),", order)


    def test_k8s_image_release_mode_rolls_banking_transfer_velocity_limit_release(self) -> None:
        # F68-R: 같은 release 스크립트로 banking transfer 를 2.4.0 으로 롤아웃한다(tb-w2). transfer 의 다른 릴리스
        # F17-H(2.1.0, 노드에 없어야 하는 태그), F42-R(2.2.0), F40-H(2.3.0)와 태그가 겹치지 않아야 넷 다 preflight 를
        # 지킨다. 패치는 transfer-service/src 안만 고친다.
        plan = compiler.compile_plan("f68-r-banking-transfer-release-velocity-limit-applied-globally")
        argv, stdin = k8s_image.build_invocation(plan, "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-banking", "testbed-transfer", "transfer-service",
                                    "core-banking-transfer:latest", "core-banking-transfer:2.4.0"])
        self.assertNotIn("F68-R", argv)
        self.assertEqual(stdin, k8s_image.RELEASE_SCRIPT)
        tags = [c["fault_image"] for sid, c in k8s_image.CONTRACTS.items() if sid != "F68-R"]
        self.assertNotIn(k8s_image.CONTRACTS["F68-R"]["fault_image"], tags)
        image = json.loads((ROOT / "fault-images" / "f68-r" / "image.json").read_text())
        self.assertEqual((image["base"], image["fault"]),
                         (k8s_image.CONTRACTS["F68-R"]["baseline_image"], k8s_image.CONTRACTS["F68-R"]["fault_image"]))
        patch = (ROOT / "fault-images" / "f68-r" / "transfer-service.patch").read_text()
        for line in patch.splitlines():
            if line.startswith(("+++ ", "--- ")) and not line.endswith("/dev/null"):
                self.assertTrue(line[4:].startswith(("a/core-banking/transfer-service/src/",
                                                     "b/core-banking/transfer-service/src/")), line)
        added = "\n".join(line for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
        for word in ("F68", "f68", "fault", "bug", "chaos", "scenario"):
            self.assertNotIn(word, added)
        # 결함의 고리: 계좌별로 쓴 한도가 이름 하나의 제한기라 모든 이체가 한 몫(초당 2건)을 나눠 쓰고, 넘치면 기다리지 않는다.
        self.assertIn('@RateLimiter(name = "transferVelocity")', added)
        self.assertIn("limit-for-period: 2", added)
        self.assertIn("limit-refresh-period: 1s", added)
        self.assertIn("timeout-duration: 0s", added)
        self.assertNotIn("RequestNotPermitted", added)

    def test_f68r_relies_on_unmapped_limiter_error_and_shared_transfer_callers(self) -> None:
        # F68-R 은 매니페스트 transfer 에 속도 제한이 없고 예외 처리기가 ServiceException 만 매핑한다는 데(그래서 2.4.0 의
        # RequestNotPermitted 가 처리되지 않은 500 이 된다), transfer 가 resilience4j 와 AOP 를 이미 의존성으로 갖는다는 데
        # (패치가 src 만 고쳐도 주석이 동작한다), account 가 transfer 의 5xx 를 502 로 바꾸고 commerce payment 가 정산 이체를
        # 재시도 없이 같은 transfer 로 보낸다는 데, transfer 롤아웃이 maxSurge 0 이라는 데 기댄다.
        banking = ROOT.parent.parent / "core-banking"
        transfer = banking / "transfer-service"
        handler = (transfer / "src" / "main" / "java" / "com" / "corebanking" / "transfer" / "config"
                   / "GlobalExceptionHandler.java").read_text()
        self.assertEqual(handler.count("@ExceptionHandler("), 1)
        self.assertIn("@ExceptionHandler(ServiceException.class)", handler)
        self.assertNotIn("ratelimiter", (transfer / "src" / "main" / "resources" / "application.yml").read_text())
        pom = (transfer / "pom.xml").read_text()
        self.assertIn("<artifactId>resilience4j-spring-boot3</artifactId>", pom)
        self.assertIn("<artifactId>spring-boot-starter-aop</artifactId>", pom)
        service = (transfer / "src" / "main" / "java" / "com" / "corebanking" / "transfer" / "service"
                   / "TransferService.java").read_text()
        self.assertIn("    @Transactional\n    public TransferResponse execute(TransferRequest request) {", service)
        client = (banking / "account-service" / "src" / "main" / "java" / "com" / "corebanking" / "account" / "client"
                  / "TransferClient.java").read_text()
        self.assertIn('log.error("Transfer service call failed for order {}: {}"', client)
        self.assertIn("HttpStatus.BAD_GATEWAY", client)
        commerce = ROOT.parent.parent / "commerce" / "payment-service" / "src" / "main" / "java" / "com" / "commerce"
        settle = (commerce / "payment" / "client" / "BankingTransferClient.java").read_text()
        self.assertIn('.uri("/api/transfers")', settle)
        self.assertNotIn("@Retry", settle)
        deploy = (banking / "k8s" / "22-transfer-service.yaml").read_text()
        self.assertIn("maxSurge: 0", deploy)
    def test_k8s_image_release_mode_rolls_food_dispatch_thread_leak_release(self) -> None:
        # F63-P: 같은 release 스크립트로 food dispatch 를 1.9.0 으로 롤아웃한다(tb-w3). F33-H(1.6.0), F43-P(1.7.0),
        # F55-R(1.8.0)도 dispatch 릴리스를 쓰므로 태그가 겹치지 않아야 넷 다 "노드에 릴리스 없음" preflight 를 지킨다.
        # 패치는 dispatch-service/src 안만 고친다.
        plan = compiler.compile_plan("f63-p-food-dispatch-release-thread-leak-oom-restarts-reject-orders")
        argv, stdin = k8s_image.build_invocation(plan, "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-food", "testbed-dispatch", "dispatch-service",
                                    "food-delivery-dispatch:latest", "food-delivery-dispatch:1.9.0"])
        self.assertNotIn("F63-P", argv)
        self.assertEqual(stdin, k8s_image.RELEASE_SCRIPT)
        tags = [c["fault_image"] for sid, c in k8s_image.CONTRACTS.items() if sid != "F63-P"]
        self.assertNotIn(k8s_image.CONTRACTS["F63-P"]["fault_image"], tags)
        image = json.loads((ROOT / "fault-images" / "f63-p" / "image.json").read_text())
        self.assertEqual((image["base"], image["fault"]),
                         (k8s_image.CONTRACTS["F63-P"]["baseline_image"], k8s_image.CONTRACTS["F63-P"]["fault_image"]))
        patch = (ROOT / "fault-images" / "f63-p" / "dispatch-service.patch").read_text()
        for line in patch.splitlines():
            if line.startswith(("+++ ", "--- ")) and not line.endswith("/dev/null"):
                self.assertTrue(line[4:].startswith(("a/food-delivery/dispatch-service/src/",
                                                     "b/food-delivery/dispatch-service/src/")), line)
        added = "\n".join(line for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
        for word in ("F63", "f63", "fault", "bug", "chaos", "scenario", "leak"):
            self.assertNotIn(word, added)
        # 누수의 고리는 F63-R 과 같다: 요청마다 고정 크기 풀을 만들고 닫지 않는다(newSingleThreadExecutor 는 스스로 닫힘).
        self.assertIn("Executors.newFixedThreadPool(1)", added)
        self.assertNotIn("newSingleThreadExecutor", added)
        self.assertNotIn("shutdown", added)
        self.assertIn('withinDeadline("delivery list"', added)
        self.assertIn('withinDeadline("delivery lookup"', added)
        # 용량 확인과 배차 요청(order 가 부르는 두 경로)은 감싸지 않는다: 주문 실패는 dispatch 가 죽고 다시 뜨는 구간에만 난다.
        self.assertNotIn("getCapacity()", added)
        self.assertNotIn("dispatchCourier(request)", added)

    def test_f63p_relies_on_dispatch_memory_limit_order_path_and_status_surge(self) -> None:
        # F63-P 의 주기는 매니페스트 dispatch 의 메모리 한도 1Gi, JVM 옵션이 OTel 에이전트뿐이라는 것, 동반 부하가 배달 조회만
        # 부르고 주문을 만들지 않는다는 것(배차 한도 2000 을 채우지 않음)에 기댄다. order 가 주문 행을 저장하기 전에 용량을
        # 확인한다는 것(실패한 주문이 배차를 남기지 않음)도 전제다. 어느 하나가 바뀌면 다시 평가해야 한다.
        food = ROOT.parent.parent / "food-delivery"
        deploy = (food / "k8s" / "22-dispatch-deploy.yaml").read_text()
        self.assertIn("memory: 1Gi", deploy[deploy.index("limits:"):])
        self.assertIn('value: "-javaagent:/opt/apm/opentelemetry-javaagent.jar"', deploy)
        self.assertNotIn("-Xss", deploy)
        surge = (food / "loadgen" / "delivery-status-surge.js").read_text()
        self.assertIn("/api/deliveries?status=DELIVERED&page=0&size=20", surge)
        self.assertIn("/api/deliveries/${dispatchId}", surge)
        self.assertNotIn("/api/orders", surge)
        self.assertNotIn("RESTAURANT_URL", surge)
        service = (food / "order-service" / "src" / "main" / "java" / "com" / "fooddelivery" / "order" / "service"
                   / "OrderService.java").read_text()
        self.assertLess(service.index("dispatchClient.checkCapacity()"), service.index("orderRepository.save(order)"))

    def test_k8s_image_release_mode_rolls_food_dispatch_capacity_path_move(self) -> None:
        # F75-R: 같은 release 스크립트로 food dispatch 만 1.10.0 으로 롤아웃한다(tb-w3). order 는 건드리지 않는다.
        # dispatch 의 다른 릴리스(F33-H 1.6.0, F43-P 1.7.0, F55-R 1.8.0, F63-P 1.9.0)와 태그가 겹치지 않아야 모두
        # "노드에 릴리스 없음" preflight 를 지킨다. 패치는 dispatch-service/src 안만 고친다.
        plan = compiler.compile_plan("f75-r-food-dispatch-release-moves-capacity-api-ahead-of-order")
        argv, stdin = k8s_image.build_invocation(plan, "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-food", "testbed-dispatch", "dispatch-service",
                                    "food-delivery-dispatch:latest", "food-delivery-dispatch:1.10.0"])
        self.assertNotIn("F75-R", argv)
        self.assertEqual(stdin, k8s_image.RELEASE_SCRIPT)
        tags = [c["fault_image"] for sid, c in k8s_image.CONTRACTS.items() if sid != "F75-R"]
        self.assertNotIn(k8s_image.CONTRACTS["F75-R"]["fault_image"], tags)
        image = json.loads((ROOT / "fault-images" / "f75-r" / "image.json").read_text())
        self.assertEqual((image["base"], image["fault"]),
                         (k8s_image.CONTRACTS["F75-R"]["baseline_image"], k8s_image.CONTRACTS["F75-R"]["fault_image"]))
        patch = (ROOT / "fault-images" / "f75-r" / "dispatch-service.patch").read_text()
        for line in patch.splitlines():
            if line.startswith(("+++ ", "--- ")) and not line.endswith("/dev/null"):
                self.assertTrue(line[4:].startswith(("a/food-delivery/dispatch-service/src/",
                                                     "b/food-delivery/dispatch-service/src/")), line)
        added = "\n".join(line for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
        removed = "\n".join(line for line in patch.splitlines() if line.startswith("-") and not line.startswith("---"))
        for word in ("F75", "f75", "fault", "bug", "chaos", "scenario", "mismatch", "skew"):
            self.assertNotIn(word, added)
        # 릴리스는 용량 조회를 새 자원으로 옮기고 옛 매핑을 지운다. 응답 본문(getCapacity)과 배차, 배달 조회는 그대로다.
        self.assertIn('@RequestMapping("/api/couriers")', added)
        self.assertIn('@GetMapping("/capacity")', added)
        self.assertIn('@GetMapping("/capacity")', removed)
        self.assertIn("dispatchService.getCapacity()", added)
        for kept in ('@PostMapping("/dispatch")', '@GetMapping("/{id}")', "@GetMapping\n"):
            self.assertNotIn(kept, removed)

    def test_f75r_relies_on_order_old_capacity_path_and_id_route(self) -> None:
        # F75-R 의 고리는 바뀌지 않은 order 가 옛 경로 /api/deliveries/capacity 를 부르고, dispatch 의
        # GET /api/deliveries/{id} 가 Long id 라 옛 경로가 400 이 된다는 것, order 가 용량 확인 실패를 주문 행 저장
        # 전에 503 으로 바꾼다는 것에 기댄다. 어느 하나가 바뀌면 다시 평가해야 한다.
        food = ROOT.parent.parent / "food-delivery"
        client = (food / "order-service" / "src" / "main" / "java" / "com" / "fooddelivery" / "order" / "client"
                  / "DispatchClient.java").read_text()
        self.assertIn('.uri("/api/deliveries/capacity")', client)
        self.assertIn("Failed to check dispatch capacity", client)
        controller = (food / "dispatch-service" / "src" / "main" / "java" / "com" / "fooddelivery" / "dispatch"
                      / "controller" / "DispatchController.java").read_text()
        self.assertIn('@GetMapping("/{id}")', controller)
        self.assertIn("@PathVariable Long id", controller)
        service = (food / "order-service" / "src" / "main" / "java" / "com" / "fooddelivery" / "order" / "service"
                   / "OrderService.java").read_text()
        self.assertLess(service.index("dispatchClient.checkCapacity()"), service.index("orderRepository.save(order)"))

    def test_k8s_image_release_mode_rolls_commerce_product_nicotine_label(self) -> None:
        # F76-R: 같은 release 스크립트로 commerce product 를 1.4.0 으로 롤아웃한다(tb-w1). product 의 다른 릴리스
        # 태그는 없고, 다른 시나리오의 결함 태그와 겹치지 않아야 "노드에 릴리스 없음" preflight 를 지킨다. 패치는
        # product-service/src 안만 고친다.
        plan = compiler.compile_plan("f76-r-commerce-product-release-nicotine-label-breaks-liquid-listings")
        argv, stdin = k8s_image.build_invocation(plan, "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-commerce", "testbed-product", "product-service",
                                    "commerce-product:latest", "commerce-product:1.4.0"])
        self.assertNotIn("F76-R", argv)
        self.assertEqual(stdin, k8s_image.RELEASE_SCRIPT)
        tags = [c["fault_image"] for sid, c in k8s_image.CONTRACTS.items() if sid != "F76-R"]
        self.assertNotIn(k8s_image.CONTRACTS["F76-R"]["fault_image"], tags)
        image = json.loads((ROOT / "fault-images" / "f76-r" / "image.json").read_text())
        self.assertEqual((image["base"], image["fault"]),
                         (k8s_image.CONTRACTS["F76-R"]["baseline_image"], k8s_image.CONTRACTS["F76-R"]["fault_image"]))
        patch = (ROOT / "fault-images" / "f76-r" / "product-service.patch").read_text()
        for line in patch.splitlines():
            if line.startswith(("+++ ", "--- ")) and not line.endswith("/dev/null"):
                self.assertTrue(line[4:].startswith(("a/commerce/product-service/src/",
                                                     "b/commerce/product-service/src/")), line)
        added = "\n".join(line for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
        for word in ("F76", "f76", "fault", "bug", "chaos", "scenario"):
            self.assertNotIn(word, added)
        # 릴리스는 액상 상품의 함량을 설명에서 읽는데 일치 여부를 보지 않고 group(1) 을 부른다. 질의와 reserve-stock 은
        # 그대로다.
        self.assertIn("private static final long LIQUID_CATEGORY_ID = 2L;", added)
        self.assertIn("matcher.find();", added)
        self.assertIn("return Integer.valueOf(matcher.group(1));", added)
        self.assertNotIn("if (matcher.find())", added)
        self.assertIn('description.contains("무니코틴")', added)
        self.assertNotIn("reserveStock", added)

    def test_f76r_relies_on_catalog_descriptions_and_gateway_passthrough(self) -> None:
        # F76-R 의 고리는 운영 카탈로그의 대량 등록 액상 상품 설명에 함량 표기가 없고(개발 시드의 대표 액상 4개는 'N mg' 이나
        # '무니코틴'), 목록 응답이 상품마다 toResponse 를 거치며, 상세는 inventory 확인이 먼저이고, checkout 의 reserve-stock 은
        # 다른 응답 형을 쓰며, 게이트웨이가 product 의 500 을 그대로 넘긴다는 것에 기댄다. 어느 하나가 바뀌면 다시 평가해야 한다.
        commerce = ROOT.parent.parent / "commerce"
        seed = (commerce / "db" / "seed-all.sql").read_text()
        self.assertIn("1 + (n % 4) AS category_id", seed)
        self.assertIn("'자동 생성 시드 상품 #' || n AS description", seed)
        data = (commerce / "product-service" / "src" / "main" / "resources" / "data.sql").read_text()
        for described in ("니코틴 9mg", "니코틴 6mg", "니코틴 3mg", "무니코틴"):
            self.assertIn(described, data)
        service = (commerce / "product-service" / "src" / "main" / "java" / "com" / "commerce" / "product" / "service"
                   / "ProductService.java").read_text()
        self.assertIn("products.map(this::toResponse)", service)
        detail = service[service.index("getProductWithStock"):]
        self.assertLess(detail.index("inventoryClient.getStock(id)"), detail.index("product.getImageUrl()"))
        reserve = service[service.index("public ReserveStockResponse reserveStock"):]
        self.assertIn("return new ReserveStockResponse(", reserve[:reserve.index("private Product findById")])
        proxy = (commerce / "api-gateway" / "src" / "main" / "java" / "com" / "commerce" / "gateway" / "service"
                 / "ProxyService.java").read_text()
        self.assertIn("return ResponseEntity.status(ex.getStatusCode()).body(ex.getResponseBodyAsByteArray());", proxy)

    def test_k8s_image_release_mode_rolls_food_restaurant_cook_time(self) -> None:
        # F76-P: 같은 release 스크립트로 food restaurant 를 1.7.0 으로 롤아웃한다(tb-w3). F49-R(1.4.0), F42-P(1.5.0),
        # F36-H(1.6.0)와 태그가 겹치지 않아야 "노드에 릴리스 없음" preflight 를 지킨다. 패치는 restaurant-service/src
        # 안만 고친다.
        plan = compiler.compile_plan("f76-p-food-restaurant-release-cook-time-breaks-expansion-category-menus")
        argv, stdin = k8s_image.build_invocation(plan, "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-food", "testbed-restaurant", "restaurant-service",
                                    "food-delivery-restaurant:latest", "food-delivery-restaurant:1.7.0"])
        self.assertNotIn("F76-P", argv)
        self.assertEqual(stdin, k8s_image.RELEASE_SCRIPT)
        tags = [c["fault_image"] for sid, c in k8s_image.CONTRACTS.items() if sid != "F76-P"]
        self.assertNotIn(k8s_image.CONTRACTS["F76-P"]["fault_image"], tags)
        image = json.loads((ROOT / "fault-images" / "f76-p" / "image.json").read_text())
        self.assertEqual((image["base"], image["fault"]),
                         (k8s_image.CONTRACTS["F76-P"]["baseline_image"], k8s_image.CONTRACTS["F76-P"]["fault_image"]))
        patch = (ROOT / "fault-images" / "f76-p" / "restaurant-service.patch").read_text()
        for line in patch.splitlines():
            if line.startswith(("+++ ", "--- ")) and not line.endswith("/dev/null"):
                self.assertTrue(line[4:].startswith(("a/food-delivery/restaurant-service/src/",
                                                     "b/food-delivery/restaurant-service/src/")), line)
        added = "\n".join(line for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
        for word in ("F76", "f76", "fault", "bug", "chaos", "scenario"):
            self.assertNotIn(word, added)
        # 릴리스는 메뉴마다 카테고리별 조리 시간을 출시 카테고리 셋뿐인 표에서 꺼내 확인 없이 int 로 받는다. 질의,
        # 가게 상세, 목록, 인기 메뉴는 그대로다.
        self.assertIn("private static final Map<Long, Integer> COOK_MINUTES_BY_CATEGORY = Map.of(", added)
        self.assertIn("3L, 20);", added)
        self.assertNotIn("4L,", added)
        self.assertIn("int cookMinutes = COOK_MINUTES_BY_CATEGORY.get(m.getCategoryId());", added)
        self.assertNotIn("getOrDefault", added)
        self.assertIn("public ResponseEntity<List<MenuItemResponse>> getMenu(@PathVariable Long id)", added)
        self.assertNotIn("getRestaurant(", added)
        self.assertNotIn("findByRestaurantId", added)

    def test_f76p_relies_on_bulk_menu_categories_and_menu_gated_orders(self) -> None:
        # F76-P 의 고리는 데모 시드(가게 1~3)의 메뉴가 카테고리 1~3 이고 대량 등록 가게의 메뉴 카테고리가 1 + id % 8
        # 이라 일부 가게가 카테고리 4~8 이며, order 가 메뉴를 공통 MenuResponse 로 읽고(더한 칸은 무시), 주문 여정이 메뉴
        # 조회가 200 일 때만 주문을 보낸다는 것에 기댄다. 어느 하나가 바뀌면 다시 평가해야 한다.
        food = ROOT.parent.parent / "food-delivery"
        init = (food / "db" / "init.sql").read_text()
        self.assertIn("    1 + (r.id % 8),", init)
        demo = init[init.index("INSERT IGNORE INTO menus"):init.index("대량 시드 1")]
        self.assertIn("(5, 1, 1, ", demo)
        self.assertIn("(10, 2, 2, ", demo)
        self.assertIn("(15, 3, 3, ", demo)
        for absent in (", 4, '", ", 5, '", ", 6, '", ", 7, '", ", 8, '"):
            self.assertNotIn(absent, demo)
        menu_dto = (food / "shop-common" / "src" / "main" / "java" / "com" / "fooddelivery" / "common" / "dto"
                    / "MenuResponse.java").read_text()
        self.assertNotIn("cookMinutes", menu_dto)
        order = (food / "order-service" / "src" / "main" / "java" / "com" / "fooddelivery" / "order" / "service"
                 / "OrderService.java").read_text()
        self.assertIn("List<MenuResponse> menus = restaurantClient.getMenu(request.restaurantId());", order)
        for script in ("script.js", "surge.js"):
            journey = (food / "loadgen" / script).read_text()
            lookup = journey.index("step: 'menu-lookup'")
            self.assertLess(lookup, journey.index("if (menuRes.status !== 200) {", lookup))
            self.assertLess(journey.index("if (menuRes.status !== 200) {", lookup), journey.index("/api/orders", lookup))

    def test_k8s_image_release_mode_rolls_food_payment_glued_pg_address(self) -> None:
        # F79-R: 같은 release 스크립트로 food payment 를 1.4.0 으로 롤아웃한다(tb-w3). F49-P(1.3.0)와 태그가 겹치지
        # 않아야 "노드에 릴리스 없음" preflight 를 지킨다. 패치는 payment-service/src 안만 고친다.
        plan = compiler.compile_plan("f79-r-food-payment-release-pg-address-missing-slash")
        argv, stdin = k8s_image.build_invocation(plan, "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-food", "testbed-payment", "payment-service",
                                    "food-delivery-payment:latest", "food-delivery-payment:1.4.0"])
        self.assertNotIn("F79-R", argv)
        self.assertEqual(stdin, k8s_image.RELEASE_SCRIPT)
        tags = [c["fault_image"] for sid, c in k8s_image.CONTRACTS.items() if sid != "F79-R"]
        self.assertNotIn(k8s_image.CONTRACTS["F79-R"]["fault_image"], tags)
        image = json.loads((ROOT / "fault-images" / "f79-r" / "image.json").read_text())
        self.assertEqual((image["base"], image["fault"]),
                         (k8s_image.CONTRACTS["F79-R"]["baseline_image"], k8s_image.CONTRACTS["F79-R"]["fault_image"]))
        patch = (ROOT / "fault-images" / "f79-r" / "payment-service.patch").read_text()
        for line in patch.splitlines():
            if line.startswith(("+++ ", "--- ")) and not line.endswith("/dev/null"):
                self.assertTrue(line[4:].startswith(("a/food-delivery/payment-service/src/",
                                                     "b/food-delivery/payment-service/src/")), line)
        added = "\n".join(line for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
        for word in ("F79", "f79", "fault", "bug", "chaos", "scenario"):
            self.assertNotIn(word, added)
        # 릴리스는 결제 주소를 기본 주소와 결제 경로를 구분자 없이 이어 만들고, 끝 '/' 는 로컬 기본값에만 둔다.
        # 결제 저장, 오류 처리, health 는 그대로다.
        self.assertIn("this.payUrl = pgBaseUrl + payPath;", added)
        self.assertIn(".uri(payUrl)", added)
        self.assertIn("base-url: ${PG_API_URL:http://localhost:9090/}", added)
        self.assertIn("pay-path: pay", added)
        self.assertNotIn("catch (", added)
        self.assertNotIn("log.", added)

    def test_k8s_image_release_mode_rolls_commerce_inventory_ledger_check(self) -> None:
        # F69-P: 같은 release 스크립트로 commerce inventory 를 1.3.0 으로 롤아웃한다(tb-w1). inventory 의 다른 릴리스
        # 태그는 없고, 어느 계약과도 태그가 겹치지 않아야 "노드에 릴리스 없음" preflight 를 지킨다. 패치는 inventory-service/src 안만 고친다.
        plan = compiler.compile_plan("f69-p-commerce-inventory-release-ledger-check-holds-stock-locks")
        argv, stdin = k8s_image.build_invocation(plan, "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-commerce", "testbed-inventory", "inventory-service",
                                    "commerce-inventory:latest", "commerce-inventory:1.3.0"])
        self.assertNotIn("F69-P", argv)
        self.assertEqual(stdin, k8s_image.RELEASE_SCRIPT)
        tags = [c["fault_image"] for sid, c in k8s_image.CONTRACTS.items() if sid != "F69-P"]
        self.assertNotIn(k8s_image.CONTRACTS["F69-P"]["fault_image"], tags)
        image = json.loads((ROOT / "fault-images" / "f69-p" / "image.json").read_text())
        self.assertEqual((image["base"], image["fault"]),
                         (k8s_image.CONTRACTS["F69-P"]["baseline_image"], k8s_image.CONTRACTS["F69-P"]["fault_image"]))
        patch = (ROOT / "fault-images" / "f69-p" / "inventory-service.patch").read_text()
        for line in patch.splitlines():
            if line.startswith(("+++ ", "--- ")) and not line.endswith("/dev/null"):
                self.assertTrue(line[4:].startswith(("a/commerce/inventory-service/src/",
                                                     "b/commerce/inventory-service/src/")), line)
        added = "\n".join(line for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
        for word in ("F69", "f69", "fault", "bug", "chaos", "scenario"):
            self.assertNotIn(word, added)
        # 릴리스는 재조정 배치가 재고 행 전체를 공유 잠금으로 읽어 한 트랜잭션 끝까지 쥐고, 상품마다 45일 이동 이력을
        # 합산한다. 예약(FOR UPDATE), 조회 API, health, 보충 로직은 그대로다.
        self.assertIn("@Lock(LockModeType.PESSIMISTIC_READ)", added)
        self.assertIn("List<Inventory> findAllForReconciliation();", added)
        self.assertIn("List<Inventory> all = inventoryRepository.findAllForReconciliation();", added)
        self.assertIn("ledger-window-days:45", added)
        self.assertIn("SUM(m.quantity)", added)
        self.assertNotIn("findByProductIdForUpdate", added)
        self.assertNotIn("PESSIMISTIC_WRITE", added)

    def test_f79r_relies_on_production_pg_address_without_trailing_slash(self) -> None:
        # F79-R 의 고리는 운영 PG_API_URL 이 '/' 로 끝나지 않고, PgApiClient 가 RestClientException 만 'PG /pay failed' 로
        # 남기며 그 밖의 예외를 서킷 fallback 이 'PG mock unavailable: ' 502 로 바꾸고, processPayment 가 그 예외를 다시
        # 던져 트랜잭션째 되돌리며, order 가 payment 의 5xx 를 'Failed to process payment for order' 로 남긴다는 것에
        # 기댄다. 어느 하나가 바뀌면 다시 평가해야 한다.
        food = ROOT.parent.parent / "food-delivery"
        configmap = (food / "k8s" / "02-configmaps.yaml").read_text()
        self.assertIn("  PG_API_URL: http://testbed-external-pg-mock:9090\n", configmap)
        deploy = (food / "k8s" / "23-payment-deploy.yaml").read_text()
        self.assertNotIn("PG_API_URL", deploy)
        self.assertIn("name: service-config", deploy)
        pg = (food / "payment-service" / "src" / "main" / "java" / "com" / "fooddelivery" / "payment" / "client"
              / "PgApiClient.java").read_text()
        self.assertIn('.uri("/pay")', pg)
        self.assertIn("} catch (RestClientException ex) {", pg)
        self.assertIn('throw new ServiceException(HttpStatus.BAD_GATEWAY, "PG mock unavailable: " + ex.getMessage());', pg)
        service = (food / "payment-service" / "src" / "main" / "java" / "com" / "fooddelivery" / "payment" / "service"
                   / "PaymentService.java").read_text()
        body = service[service.index("public PaymentResponse processPayment("):service.index("Processed payment")]
        self.assertIn("} catch (ServiceException ex) {", body)
        self.assertIn("throw ex;", body)
        self.assertIn("@Transactional\n    public PaymentResponse processPayment(", service)
        client = (food / "order-service" / "src" / "main" / "java" / "com" / "fooddelivery" / "order" / "client"
                  / "PaymentClient.java").read_text()
        self.assertIn('log.error("Failed to process payment for order {}: {}", orderId, ex.getMessage());', client)

    def test_mock_expectations_are_snapshotted_and_restored(self) -> None:
        script = self.assert_contract(mock, "f01-h-commerce-pg-429", "mock.expectation")
        self.assertIn("ACTIVE_EXPECTATIONS", script)
        self.assertIn('--data-binary "@$state"', script)
        self.assertIn("/root/tb-kubeconfig", script)
        plan = compiler.compile_plan("f06-r-commerce-external-hang")
        self.assertEqual(next(x for x in plan["profile_instances"] if x["profile_id"] == "mock.expectation")["parameters"]["delay_seconds"], 30)

    def test_kubernetes_patch_has_fixed_target_and_original_value_snapshot(self) -> None:
        script = self.assert_contract(k8s_patch, "f09-p-inventory-cpu-throttle", "k8s.patch")
        self.assertIn('current >"$state.tmp"', script)
        self.assertIn('--limits="cpu=$original"', script)
        self.assertIn("testbed-inventory", " ".join(k8s_patch.build_invocation(compiler.compile_plan("f09-p-inventory-cpu-throttle"), "run")[0]))

    def test_kubernetes_patch_lowers_the_request_with_the_limit(self) -> None:
        # 쿠버네티스는 자기 request보다 낮은 CPU limit을 표현할 수 없다 — 패치 전체가
        # 거부된다. 2026-08-04까지 이 실행기는 --limits만 설정했고, 두 시나리오의
        # 사다리는 request(200m) 아래인 100m·50m로 내려간다. 그래서 F12-H·F09-P는
        # 주입 자체가 한 번도 성립하지 않았다(배치 #1).
        #
        # 109 실측 2026-08-04 평시 CPU: product 16m / inventory 7m. request를 사다리
        # 값까지 내려도 파드는 그대로 스케줄된다.
        script = self.assert_contract(k8s_patch, "f12-h-pod-cpu-network-lookalike", "k8s.patch")
        self.assertIn('--requests="cpu=$request"', script)
        # 스냅샷은 limit·request 두 값을 함께 잡고, 정리는 둘 다 되돌린다. request를
        # 스냅샷하지 않으면 내려간 request가 영구히 남는다.
        self.assertIn("current() { echo \"$(field limits) $(field requests)\"; }", script)
        self.assertIn('--requests="cpu=$original_request"', script)
        # 사다리에 request 아래 단이 실제로 남아 있어야 이 수리가 의미가 있다.
        # F09-P는 여기서 빠진다 — 2026-08-04 라이브 실측이 그 사다리를 request 위로
        # 되돌려 놓았기 때문이다. 평시 CPU가 낮아 request를 내려도 스케줄은 되지만
        # (inventory 7m), 스케줄과 **서빙**은 다른 질문이었다: baseline 부하 아래에서
        # inventory는 200m에서 Ready 12/12·재시작 0인데 175m에서는 12틱 내내 Ready가
        # 되지 못하고 재시작한다. 같은 날 product는 175m을 버텼다 — 바닥은 서비스마다
        # 다르므로 사다리마다 재야 한다.
        #
        # 2026-08-20: F12-H·F09-P 가 pin 단(둘 다 250m, request 200m 위)으로 고정
        # 승격돼 k8s.patch 에는 request 아래로 내려가는 라이브 단이 더 없다. clamp 는
        # 계약이 다시 request 아래를 쓸 때를 위해 스크립트에 남긴다(위 단언이 지킨다).
        # 고정값 자체가 request 위에 있다는 사실만 기록해 둔다.
        for scenario_id in ("F12-H", "F09-P"):
            limit = int(
                self.profiles["k8s.patch"]["scenario_parameters"][scenario_id]["fault_cpu_limit"]
                .removesuffix("m")
            )
            self.assertGreaterEqual(limit, 200, f"{scenario_id} fixed limit sits under the 200m request")

    def test_kubernetes_patch_adaptive_ladder_accepts_only_exact_levels(self) -> None:
        # 2026-08-20: F09-P 가 pin 단(conservative-250m)으로 고정 승격돼 k8s.patch 의
        # 사다리는 F21-P(파킹, 비라이브)만 남았다. 보려는 성질은 "사다리 단이 사전 선언된
        # 것만 통과하는가"이므로 같은 실행기의 남은 사다리로 옮긴다.
        profile = self.profiles["k8s.patch"]
        levels = profile["scenario_levels"]["F21-P"]
        self.assertEqual([level["level_id"] for level in levels], ["conservative-400m", "constrained-300m", "floor-200m"])
        self.assertEqual([level["parameters"]["fault_cpu_limit"] for level in levels], ["400m", "300m", "200m"])
        for level in levels:
            k8s_patch.validate("F21-P", level["parameters"], profile)
        instance = next(row for row in compiler.compile_plan("f21-p-banking-api-tomcat-thread-saturation")["profile_instances"] if row["profile_id"] == "k8s.patch")
        self.assertEqual(instance["selected_level_id"], "floor-200m")
        self.assertEqual(instance["scenario_levels"], levels)
        tampered = dict(levels[1]["parameters"])
        tampered["fault_cpu_limit"] = "99m"
        with self.assertRaisesRegex(k8s_patch.ExecutorError, "predeclared"):
            k8s_patch.validate("F21-P", tampered, profile)
        # 고정 승격된 F09-P 는 scenario_parameters 한 벌만 승인된다 — 그 값은 통과하고
        # 옛 사다리의 다른 단(225m·200m)은 이제 거부돼야 한다.
        fixed = profile["scenario_parameters"]["F09-P"]
        self.assertEqual(fixed["fault_cpu_limit"], "250m")
        k8s_patch.validate("F09-P", fixed, profile)
        for retired in ("225m", "200m"):
            with self.assertRaisesRegex(k8s_patch.ExecutorError, "predeclared"):
                k8s_patch.validate("F09-P", {**fixed, "fault_cpu_limit": retired}, profile)

    def test_kubernetes_patch_reaches_banking_and_still_pins_each_scenario(self) -> None:
        # Until 2026-07-31 build_invocation hardcoded the commerce namespace, so
        # F21-P's "throttle transfer alone" lever could not be dispatched at all
        # even though allowed_locations already listed banking-namespace. Revert
        # the ALLOWLIST lookup to the old constant and this first assertion fails.
        profile = self.profiles["k8s.patch"]
        argv, _ = k8s_patch.build_invocation(
            compiler.compile_plan("f21-p-banking-api-tomcat-thread-saturation"), "run"
        )
        self.assertIn("rca-testbed-banking", argv)
        self.assertIn("testbed-transfer", argv)
        # Generalising the namespace must not turn it into a free parameter: a
        # scenario may only patch the deployment its own answer key names.
        with self.assertRaisesRegex(k8s_patch.ExecutorError, "not allowlisted"):
            k8s_patch.validate(
                "F21-P", profile["scenario_parameters"]["F09-P"], profile
            )
        with self.assertRaisesRegex(k8s_patch.ExecutorError, "not allowlisted"):
            k8s_patch.validate(
                "F09-P", profile["scenario_levels"]["F21-P"][0]["parameters"], profile
            )

    def test_kubernetes_patch_ladder_stays_above_the_declared_cpu_request(self) -> None:
        # The API server rejects any limit below requests.cpu, and every testbed
        # deployment declares 200m. A rung below that is a rung that can never be
        # applied — measured 2026-07-31 on testbed-transfer with 100m and 50m.
        profile = self.profiles["k8s.patch"]
        levels = profile["scenario_levels"]["F21-P"]
        self.assertEqual(
            [level["parameters"]["fault_cpu_limit"] for level in levels],
            ["400m", "300m", "200m"],
        )
        for level in levels:
            millicores = int(level["parameters"]["fault_cpu_limit"].removesuffix("m"))
            self.assertGreaterEqual(millicores, 200, level["level_id"])

    def test_kubernetes_patch_level_override_changes_exact_builder_argv(self) -> None:
        # 2026-08-20: F09-P 고정 승격으로 k8s.patch 의 다단 사다리는 F21-P 만 남았다.
        plan = compiler.compile_plan("f21-p-banking-api-tomcat-thread-saturation")
        instance = next(row for row in plan["profile_instances"] if row["profile_id"] == "k8s.patch")
        levels = instance["approved_levels"]
        canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"))
        low, low_id = sys.modules["executor_common"].bind_level_parameters(plan, "k8s.patch", 0, canonical(levels[0]["parameters"]))
        endpoint, endpoint_id = sys.modules["executor_common"].bind_level_parameters(plan, "k8s.patch", 2, canonical(levels[2]["parameters"]))
        low_argv, _ = k8s_patch.build_invocation(low, "run")
        endpoint_argv, _ = k8s_patch.build_invocation(endpoint, "run")
        self.assertEqual((low_id, endpoint_id), ("conservative-400m", "floor-200m"))
        self.assertIn("400m", low_argv)
        self.assertIn("200m", endpoint_argv)
        self.assertNotEqual(low_argv, endpoint_argv)
        tampered = dict(levels[0]["parameters"])
        tampered["fault_cpu_limit"] = "399m"
        with self.assertRaisesRegex(Exception, "predeclared"):
            sys.modules["executor_common"].bind_level_parameters(plan, "k8s.patch", 0, canonical(tampered))

    def test_f12h_patch_targets_product_and_restores_exact_cpu_limit(self) -> None:
        plan = compiler.compile_plan("f12-h-pod-cpu-network-lookalike")
        instance = next(row for row in plan["profile_instances"] if row["profile_id"] == "k8s.patch")
        self.assertEqual(
            [level["parameters"]["fault_cpu_limit"] for level in instance["approved_levels"]],
            # 2026-08-20: 사다리(250→200→175m)를 pin 단 conservative-250m 으로 고정 승격.
            ["250m"],
        )
        self.assertEqual(instance["parameters"]["deployment"], "testbed-product")
        self.assertEqual(instance["parameters"]["container"], "product-service")
        k8s_patch.validate("F12-H", instance["parameters"], self.profiles["k8s.patch"])
        argv, script = k8s_patch.build_invocation(plan, "cleanup")
        self.assertIn("testbed-product", argv)
        self.assertIn("product-service", argv)
        self.assertIn('--limits="cpu=$original"', script.decode())

    def test_cache_cleanup_restores_snapshot_and_waits_for_dependents(self) -> None:
        script = self.assert_contract(cache, "f11-r-redis-down-fallback-overload", "cache.control")
        self.assertIn('--replicas="$original"', script)
        self.assertIn('rollout status deploy/"$dependent"', script)

    def test_composite_cleanup_is_reverse_order(self) -> None:
        script = self.assert_contract(timeline, "f08-h-rollout-plus-external-fault", "timeline.compose")
        cleanup = script[script.index("cleanup)"):script.index("recovery)")]
        self.assertLess(cleanup.index("reset_mock"), cleanup.index("restore_rollout"))
        self.assertIn("Reverse sub-injection order is mandatory", cleanup)

    def test_app_control_flips_the_flag_without_touching_the_deployment(self) -> None:
        # F18-P 재설계(0804 #13 집행): 재기동 없는 DB 플래그 토글. 롤아웃을 유발하는
        # 어떤 kubectl 동사도 스크립트에 있어선 안 된다 — patch/rollout이 다시 들어오면
        # maxSurge=0 아래서 자기 감별자(transfer-pod-failure)를 켜는 원래 병이 재발한다.
        script = self.assert_contract(app_control, "f18-p-banking-outbox-relay-halt", "app.control")
        # v4 C6b: a DBA's session through the listener from the runner, not sqlplus inside the pod.
        self.assertIn("| odb dba", own_body(script))
        self.assertNotIn("sqlplus -s", own_body(script))
        self.assertIn("update $table set $col=$1", script)
        for rollout_verb in ("kubectl patch", "rollout", "set env"):
            self.assertNotIn(rollout_verb, script)
        # 정리는 상태 파일 없이 멱등 UPDATE 하나여야 한다.
        self.assertNotIn("state_root", script)

    def test_app_control_delay_scenarios_carry_a_bounded_value(self) -> None:
        # F21-P·F21-Q 병합 재설계(0804 #27·#28): 무딘 레버가 대상을 죽여버렸으므로
        # 제어 행은 켜고 끄는 플래그가 아니라 "얼마나 느리게"라는 값을 나른다. 값형은
        # 사다리가 단마다 다른 값을 물고 오므로 동일성이 아니라 범위로 검증해야 한다 —
        # 실행기가 한 벌만 보고 다른 단을 전부 막던 병(배치 #12)을 물려받지 않는다.
        for scenario_id, contract in app_control.CONTRACTS.items():
            if "delay_ms" not in contract:
                continue
            with self.subTest(scenario=scenario_id):
                profile = self.profiles["app.control"]
                app_control.validate(scenario_id, dict(contract, delay_ms=2500), profile)
                column, injected, clean = app_control.control_values(contract)
                self.assertEqual(column, "delay_ms")
                self.assertEqual(injected, str(contract["delay_ms"]))
                # 평시 값이 0 이어야 preflight·cleanup 이 "지연 없음"을 기대치로 삼는다.
                self.assertEqual(clean, "0")
                # 0 은 평시값과 같다 — 아무 행도 바꾸지 않는 주입이 성공을 보고한다.
                for bad in (0, -1, app_control.MAX_DELAY_MS + 1, "1000", 1000.0, True, None):
                    with self.assertRaises(ExecutorError):
                        app_control.validate(scenario_id, dict(contract, delay_ms=bad), profile)
                with self.assertRaises(ExecutorError):
                    app_control.validate(scenario_id, dict(contract, service_id="somebody-else"), profile)

    def test_app_control_speaks_both_engines_without_leaking_a_local_name(self) -> None:
        # F21-Q 는 food/MySQL 이다. 오라클 한 벌만 있는 스크립트에 mysql 계약을 물리면
        # 주입은 조용히 아무 행도 건드리지 않는다. 그리고 원격 텍스트 안의 이름은 원격
        # 것이어야 한다 — 로컬 이름이 원격 heredoc 에서 빈 문자열로 퍼진 것이 F01-P 를
        # 일주일 죽였다(4398722). 비밀번호는 파드 자기 env 에서만 온다.
        script = app_control.SCRIPT.decode()
        self.assertIn("engine=", script)
        for engine in ("oracle)", "mysql)"):
            self.assertIn(engine, script)
        own = own_body(script)
        remote = own[own.index("mysql)"):own.index("esac")]
        # v4 C6b: root through the listener from the runner; the password comes from mysql-secret into the
        # client's environment and the statement rides in the environment too, so neither reaches a
        # command line (nothing runs in the pod any more, and the runner's own argv carries neither).
        self.assertIn('sql() { mdb "$schema" "$1"; }', remote)
        self.assertNotIn("exec", remote)
        library = script[script.index(db_operator_remote.START_MARKER):script.index(db_operator_remote.END_MARKER)]
        self.assertRegex(library, r'MDB_PASSWORD="\$\{!p\}" DB="\$3" SQL="\$4" \\\n\s+"\$dbo_py" -c "\$DBO_SRC" "\$2" </dev/null')
        self.assertNotIn("rootpassword", script)
        # 식별자를 따옴표로 감싸면 테이블이 아니라 문자열을 읽는다.
        self.assertIn("select $col from $table", script)
        for engine, expected in (("oracle", "systimestamp"), ("mysql", "now()")):
            with self.subTest(engine=engine):
                self.assertIn(f'now="{expected}"', script)
        for scenario_id, contract in app_control.CONTRACTS.items():
            with self.subTest(scenario=scenario_id):
                self.assertIn(f'{contract["engine"]})', script,
                              f"{scenario_id}: contract names an engine the script cannot speak")

    def test_the_delay_ceiling_is_one_number_in_every_place_that_enforces_it(self) -> None:
        # 상한은 세 곳이 각자 조인다: 앱(Thread.sleep 직전 클램프), DDL 의 CHECK,
        # 실행기의 값 검증. 하나만 올리면 주입은 성공을 보고하는데 앱은 다른 값을 쓰고,
        # 판정은 있지도 않은 지연을 전제로 결론을 적는다. 한 값임을 고정한다.
        repo = ROOT.parents[1]
        ceilings: dict[str, int] = {}
        for relative in ("core-banking/shop-common/src/main/java/com/corebanking/common/delay"
                         "/ResponseDelayFilter.java",
                         "food-delivery/restaurant-service/src/main/java/com/fooddelivery/restaurant/delay"
                         "/ResponseDelayFilter.java"):
            text = (repo / relative).read_text(encoding="utf-8")
            found = re.findall(r"MAX_DELAY_MS\s*=\s*([\d_]+)L", text)
            self.assertEqual(len(found), 1, f"{relative}: expected exactly one ceiling")
            ceilings[relative] = int(found[0].replace("_", ""))
        # DDL 은 init.sql 과 배포 스크립트(기존 PVC 대응) 두 벌로 존재한다.
        sql_sources = ("core-banking/db/init.sql", "food-delivery/db/init.sql",
                       "core-banking/k8s/build-and-deploy.sh", "food-delivery/k8s/build-and-deploy.sh")
        for relative in sql_sources:
            text = (repo / relative).read_text(encoding="utf-8")
            found = re.findall(r"delay_ms BETWEEN 0 AND (\d+)", text)
            self.assertEqual(len(found), 1, f"{relative}: expected exactly one delay_ms CHECK")
            ceilings[relative] = int(found[0])
        ceilings["app_control_executor.MAX_DELAY_MS"] = app_control.MAX_DELAY_MS
        self.assertEqual(len(set(ceilings.values())), 1, f"delay ceilings disagree: {ceilings}")

    def test_app_control_flag_scenarios_keep_their_one_or_zero_contract(self) -> None:
        # 값형을 들이면서 플래그형(F18-P)의 의미가 뒤집히면 릴레이가 정리 후에도
        # 멈춘 채로 남는다. enabled 는 주입 0 / 평시 1 이다.
        self.assertEqual(app_control.control_values(app_control.CONTRACTS["F18-P"]), ("enabled", "0", "1"))

    def test_app_control_contract_matches_the_approved_profile(self) -> None:
        approved = self.profiles["app.control"]["scenario_parameters"]
        for scenario_id, contract in app_control.CONTRACTS.items():
            self.assertEqual(
                contract, approved.get(scenario_id),
                f"{scenario_id}: executor CONTRACTS drifted from profiles.json",
            )

    def test_db_lock_contracts_do_not_drift_from_the_approved_profile(self) -> None:
        # F03-H(0804 #21)는 수리가 실행기에만 들어가 레지스트리와 어긋난 채 일주일을
        # 죽어 있었다. db.lock은 반대 방향도 가능하다 — CONTRACTS가 실행기 안에
        # 하드코딩돼 있어 registry만 고치면 validate()가 주입을 거부한다(F01-P
        # hold 600->900에서 실제로 걸릴 뻔했다). 두 벌이 같은 값을 주장하는지 고정.
        approved = self.profiles["db.lock"]["scenario_parameters"]
        for scenario_id, contract in db_lock.CONTRACTS.items():
            if scenario_id in approved:
                self.assertEqual(
                    contract, approved[scenario_id],
                    f"{scenario_id}: executor CONTRACTS drifted from profiles.json",
                )

    def test_live_matrix_matches_the_governed_ready_set(self) -> None:
        expected = {
            "F01-H", "F01-P", "F01-R", "F04-R", "F05-H", "F05-P", "F05-R",
            "F06-H", "F06-R", "F07-H", "F08-G", "F08-H", "F08-P",
            "F11-R", "F12-H", "F15-G", "F15-T1", "F16-H", "F17-R", "F18-P",
            "F19-P", "F19-S", "F20-R", "F23-R", "F25-H",
            "F03-H", "F06-P",
            # 2026-08-06: F02-H·F21-P·F21-Q parked (0804 #24·27·28)
            "F10-H", "F15-P",
            "F09-H", "F09-P", "F17-P",
            "F04-H",
            "F15-H",
            # 2026-07-29: 원장 테이블 READ ONLY. 필요하던 "catch-swallow injector"는
            # 앱에 심을 필요가 없었다 — 삼킴 + 자동 ack = 영구 유실이 이미 코드에 있고,
            # 없던 것은 그것을 발화시킬 쓰기 실패였다.
            "F14-P",
            # 2026-10-08: F10-P, F15-R, F15-T2, F20-Q 폐기(정상 녹화 없음, 사용자 결정)로 이 목록에서 뺐다.
            # 2026-10-08: food payment JSON 명명 규칙 설정 배포(데이터 형식 불일치). 첫 시험 실행을 위해 ready + calibration.
            # 2026-10-08: 같은 날 F30-R 을 lenient 1단 고정 evaluation 으로 바꿨다(수명주기 결정). 라이브 소속과 순서는 그대로다.
            "F30-R",
            # 2026-10-08: food dispatch 배차 한도 설정 배포(설정 오배포). 새 후보, 설계 강도 1단 고정 evaluation.
            "F32-R",
            # 2026-10-08: food dispatches 인덱스 제거(느린 쿼리). 새 후보, 설계 강도 1단 고정 evaluation.
            "F33-R",
            # 2026-10-08: banking Oracle 애플리케이션 계정 잠금(자격 증명 회전 실수). 새 후보, 설계 강도 1단 고정 evaluation.
            "F35-R",
            # 2026-10-08: food restaurants 열 이름 변경 마이그레이션(스키마와 ORM 형식 불일치). 새 후보, 설계 강도 1단 고정 evaluation.
            "F36-R",
            # 2026-10-08: banking api 파드 DNS 정책을 노드 resolver 로 바꾸는 배포(이름 해석 실패). 새 후보, 설계 강도 1단 고정 evaluation.
            "F37-R",
            # 2026-10-08: food MySQL 인스턴스 전체 read_only(운영 세션의 설정 실수로 모든 쓰기 거절). 새 후보, 설계 강도 1단 고정 evaluation.
            "F38-R",
            # 2026-10-08: banking account 의 이체 하류 주소를 다른 내부 호스트로 덮어쓰는 설정 배포(잘못된 호스트로 연결 시간 초과). 새 후보, 설계 강도 1단 고정 evaluation.
            "F39-R",
            # 2026-10-09: commerce pricing 프로모션 행의 할인율 오입력(10%→100.00)과 pricing 재시작으로 모든 checkout 견적 0 원, 은행이 정산 이체 거절. 새 후보, 설계 강도 1단 고정 evaluation.
            "F40-R",
            # 2026-10-09: banking transfer 를 노드에 없는 릴리스 이미지 태그로 롤아웃(maxSurge 0 이라 옛 파드가 먼저 내려가고 새 파드는 ErrImageNeverPull). 새 후보, 설계 강도 1단 고정 evaluation.
            "F17-H",
            # 2026-10-09: food dispatch 가 JSON 숫자를 문자열로 쓰게 하는 직렬화 설정 배포(값 형식 불일치로 order 의 용량 응답 캐스트 실패). 새 후보, 설계 강도 1단 고정 evaluation.
            "F32-H",
            # 2026-10-09: commerce auth_tokens 인덱스 교체 마이그레이션이 동시 재생성을 INVALID 로 남긴 채 옛 인덱스를 지움(토큰 확인 전수 스캔, PostgreSQL 포화). 새 후보, 설계 강도 1단 고정 evaluation.
            "F33-P",
            # 2026-10-09: banking account-service 를 메모리가 새는 릴리스 1.3.0(fault-images/f41-r 패치로 만든 별도 태그)으로 롤아웃(힙이 몇 분마다 차서 OutOfMemoryError, liveness 재시작이 되풀이). 새 후보, 설계 강도 1단 고정 evaluation.
            "F41-R",
            # 2026-10-09: banking transfer-service 를 이체마다 transfers 를 전수 스캔하는 일일 한도 질의가 든 릴리스 2.2.0(fault-images/f42-r)으로 롤아웃(공유 Oracle 2 CPU 포화). 새 후보, 설계 강도 1단 고정 evaluation.
            "F42-R",
            # 2026-10-09: commerce cart-service 를 담기마다 DB 연결을 빌려 돌려주지 않는 릴리스 1.2.0(fault-images/f43-r)으로 롤아웃(cart Hikari 풀 고갈, health 실패, 재시작 반복). 새 후보, 설계 강도 1단 고정 evaluation.
            "F43-R",
            # 2026-10-09: food 워커 tb-w3 호스트 방화벽에 restaurant API(8081) 허용 목록을 넣으며 같은 노드의 order 파드 대역을 빠뜨림(새 실행기 host.firewall). 새 후보, 설계 강도 1단 고정 evaluation.
            "F44-R",
            # 2026-10-09: banking 워커 tb-w2 호스트 방화벽에 transfer API(8082) 허용 목록을 넣으며 다른 노드 tb-w1 의 commerce 파드 대역(정산 이체 호출자 payment)을 빠뜨림(Central 1 2025-04-09 재구성, host.firewall 에 다른 노드 호출자 계약 추가). 새 후보, 설계 강도 1단 고정 evaluation.
            "F44-P",
            # 2026-10-09: banking api-service 를 이체마다 account 의 활성 계좌 목록 전체(페이지 약 49번)를 뒤에서 다시 읽는 릴리스 1.4.0(fault-images/f47-r)으로 롤아웃(account CPU 한도 포화, 잔액 조회 초 단위, 이체 502). 새 후보, 설계 강도 1단 고정 evaluation.
            "F47-R",
            # 2026-10-09: food 인기 메뉴 집계 표를 취소된 인덱스 백필이 치움(MySQL 1146, 인기 메뉴만 500). 새 후보, 설계 강도 1단 고정 evaluation.
            "F48-R",
            # 2026-10-09: food restaurant-service 를 가게 상세 응답의 status 를 문자열에서 {code, label} 객체로 바꾼 릴리스 1.4.0(fault-images/f49-r)으로 롤아웃(restaurant 200, order 해석 실패로 주문 502). 새 후보, 설계 강도 1단 고정 evaluation.
            "F49-R",
            # 2026-10-09: food dispatch-service 를 배달 목록에 전체 건수(X-Total-Count)를 더한 릴리스 1.6.0(fault-images/f33-h)으로 롤아웃(목록마다 끝난 배차 약 174만 행 COUNT, MySQL 포화, dispatch 풀 고갈로 주문 503). 새 후보, 설계 강도 1단 고정 evaluation.
            "F33-H",
            # 2026-10-09: food order-service 를 시각 표기를 yyyy-MM-dd HH:mm:ss 로 바꾸고 하류 호출에도 같은 ObjectMapper 를 쓰게 한 릴리스 2.3.0(fault-images/f49-h)으로 롤아웃(dispatch 는 200, order 가 배차 응답의 ISO 시각을 못 읽어 주문 503). 새 후보, 설계 강도 1단 고정 evaluation.
            "F49-H",
            # 2026-10-09: banking 네임스페이스에 사용량(4928Mi)보다 작은 메모리 할당량(ResourceQuota compute-resources, requests.memory 4Gi)을 건 뒤 transfer 일상 재배포의 새 파드가 exceeded quota 로 거절됨(transfer 파드 없음, 이체와 commerce 정산 502). 새 후보, 설계 강도 1단 고정 evaluation.
            "F50-R",
            # 2026-10-09: food 주문 이벤트 outbox 표(order_outbox_events)를 취소된 인덱스 백필이 치움(MySQL 1146, 주문 생성이 outbox 기록에서 되돌려져 전량 500). 새 후보, 설계 강도 1단 고정 evaluation.
            "F48-P",
            # 2026-10-09: 운영자의 용량 명령 입력이 잘못되어 banking account-service Deployment 가 replicas 0(kubectl scale), account 파드가 없어 잔액 조회, 계좌 목록, 이체 502(거래 내역, commerce 정산 정상). 새 후보, 설계 강도 1단 고정 evaluation.
            "F51-R",
            # 2026-10-10: food dispatch-service 를 같은 주문의 중복 배차 확인을 더한 릴리스 1.7.0(fault-images/f43-p)으로 롤아웃(확인이 배차 요청마다 풀 연결을 빌려 중복일 때만 돌려줘 Hikari 풀 10 이 배차 약 10건 만에 고갈, MySQL 은 한가함, 주문 503). 새 후보, 설계 강도 1단 고정 evaluation.
            "F43-P",
            # 2026-10-10: banking transfer-service 를 원 단위 정수 금액 검증을 더한 릴리스 2.3.0(fault-images/f40-h)으로 롤아웃(검증이 값이 아니라 표기 자릿수로 판정해 소수 둘째 자리까지 적힌 commerce 정산 이체를 400 으로 거절, checkout 502, banking 자체 정수 이체는 정상). 새 후보, 설계 강도 1단 고정 evaluation.
            "F40-H",
            # 2026-10-10: banking 워커 tb-w2 의 이미지 보존 정리가 레지스트리 다이제스트 없는 banking 앱 이미지를 지운 뒤 transfer 일상 재배포의 새 파드가 ErrImageNeverPull(transfer 파드 없음, 이체와 commerce 정산 502). 새 후보, 설계 강도 1단 고정 evaluation.
            "F52-R",
            # 2026-10-10: food MySQL fooddelivery.orders 를 운영을 가리킨 로컬 마이그레이션이 지움(MySQL 1146, 주문 생성이 첫 주문 INSERT 에서 전량 500, 배차, 결제 전). 새 후보, 설계 강도 1단 고정 evaluation.
            "F53-R",
            # 2026-10-10: food MySQL fooddelivery.payments 를 운영을 가리킨 로컬 마이그레이션이 지움(MySQL 1146, payment 가 결제 INSERT 에서 500, order 가 배차 뒤 결제 단계에서 전량 502, PG 미호출). 새 후보, 설계 강도 1단 고정 evaluation.
            "F53-P",
            # 2026-10-10: food restaurant-service 를 인기 메뉴를 주문 원장에서 바로 세는 릴리스 1.5.0(fault-images/f42-p)으로 롤아웃(인기 메뉴 요청마다 가게 한 곳의 주문 품목 수만 행 조인 집계, MySQL 포화, restaurant 풀 고갈로 가게 조회와 주문 실패). 새 후보, 설계 강도 1단 고정 evaluation.
            "F42-P",
            # 2026-10-10: banking Oracle BANKING.TRANSFERS 를 운영을 가리킨 로컬 마이그레이션이 지움(같은 스키마 안 이름 바꾸기, 이체 INSERT ORA-04043, 거래 내역 ORA-00942, account, api 502, commerce 정산 502, 잔액 조회 정상). 새 후보, 설계 강도 1단 고정 evaluation.
            "F35-H",
            # 2026-10-10: food MySQL fooddelivery.dispatches 를 운영을 가리킨 로컬 마이그레이션이 지움(MySQL 1146, dispatch 의 배달원 용량 확인 500, order 가 주문 저장 전에 전량 503, 주문 행, 배차, 결제 없음). 새 후보, 설계 강도 1단 고정 evaluation.
            "F32-P",
            # 2026-10-10: banking Oracle 애플리케이션 계정 BANKING 에 호출당 논리 읽기 한도(30000) 프로필을 붙임(거래 내역 건수 질의가 ORA-02395 로 끊겨 api 502, 이체와 commerce 정산 정상). 새 후보, 설계 강도 1단 고정 evaluation.
            "F54-R",
            # 2026-10-10: food payment-service 를 결제 응답의 id 를 숫자에서 외부 결제 키 문자열로 바꾼 릴리스 1.3.0(fault-images/f49-p)으로 롤아웃(payment 200, order 해석 실패로 주문 502 롤백, 승인 결제만 남음). 새 후보, 설계 강도 1단 고정 evaluation.
            "F49-P",
            # 2026-10-10: food dispatch-service 를 배달 API 에 요청 IP 기준 클라이언트별 요청 한도를 더한 릴리스 1.8.0(fault-images/f55-r)으로 롤아웃(호출자가 order 파드와 노드 주소 하나씩뿐이라 한도가 사실상 모든 요청에 걸려 429, order 가 503 으로 주문 거절, dispatch 는 Ready). 새 후보, 설계 강도 1단 고정 evaluation.
            "F55-R",
            # 2026-10-10: banking 운영자의 해지 계좌 정리가 잘못된 id 목록을 받아 Oracle BANKING.ACCOUNTS 에서 commerce 정산 계좌 두 행(commerce-settlement, commerce-merchant)을 지움(정산 이체마다 transfer 400 'Account not found: commerce-merchant', payment 502, checkout 502, banking 자신의 거래 정상). 새 후보, 설계 강도 1단 고정 evaluation.
            "F56-R",
            # 2026-10-10: banking account-service 를 잔액 조회에 마지막 이체 시각 헤더를 더한 릴리스 1.4.0(fault-images/f51-h)으로 롤아웃(새 조회가 트랜잭션 밖에서 빌린 Hikari 연결을 돌려주지 않아 잔액 조회 약 10건 만에 풀 active=10 으로 굳고 readiness 이탈, 잔액 조회, 계좌 목록, 이체 500, 502, 거래 내역과 commerce 정산 정상). 새 후보, 설계 강도 1단 고정 evaluation.
            "F51-H",
            # 2026-10-10: banking Oracle BANKING.TRANSFERS 를 옮긴 표 이전이 identity 생성기 상태를 옮기지 않아 생성기가 표보다 40만 키 뒤에서 다시 시작(db.ddl Oracle identity 되감기, 새 이체 INSERT 가 기본 키 ORA-00001 로 실패, account, api 502, commerce 정산 502, 거래 내역과 잔액 조회 정상). 새 후보, 설계 강도 1단 고정 evaluation.
            "F57-R",
            # 2026-10-10: commerce PostgreSQL product_schema.products 를 운영을 가리킨 로컬 마이그레이션이 지움(db.ddl PostgreSQL 표 지우기, 기록되지 않는 세션이 public 으로 옮기고 옮긴 표와 인덱스의 통계 카운터를 0 으로, 상품 조회 42P01, 둘러보기 500, checkout 502). 새 후보, 설계 강도 1단 고정 evaluation.
            "F23-P",
            # 2026-10-10: food order-service 를 최소 주문 금액 검사를 더한 릴리스 2.5.0(fault-images/f58-r)으로 롤아웃(새 검사가 주문마다 읽는 정책값 order.policy.minimum-amount 가 릴리스의 개발 프로필 파일에만 있고 운영 설정에 없어 IllegalStateException, 주문 생성 전량 500, 하류 호출 없음, order Ready). 새 후보, 설계 강도 1단 고정 evaluation.
            "F58-R",
            # 2026-10-10: food 알림 쪽 보관소 백필 Job(notify-delivery-backfill, 워커 12)이 dispatch 배달 목록을 offset 페이지로 끝까지 읽어 깊은 페이지마다 MySQL 이 앞선 끝난 배달을 모두 읽음(k8s.job, dispatch Hikari 고갈과 MySQL 포화, 용량 확인 500, 주문 503, 롤아웃·설정·인덱스 변경 없음). 새 후보, 설계 강도 1단 고정 evaluation.
            "F59-R",
            # 2026-10-10: banking 원장 쪽 계좌 명세서 백필 Job(ledger-statement-backfill, 워커 48)이 계좌마다 transfer 이체 내역을 페이지 500 으로 끝까지 읽어 호출마다 Oracle 이 transfers 전수 스캔(k8s.job, transfer Hikari 고갈, 이체 500, api 502, commerce 정산 실패, 롤아웃·설정·인덱스 변경 없음). 새 후보, 설계 강도 1단 고정 evaluation.
            "F59-P",
            # 2026-10-10: food restaurant-service 를 지역 영업 일시 중지를 더한 릴리스 1.6.0(fault-images/f36-h)으로 롤아웃(가게 상세가 요청마다 읽는 정책값 restaurant.policy.paused-regions 가 릴리스의 개발 프로필 파일에만 있고 운영 설정에 없어 IllegalStateException, 가게 상세 전량 500, order 주문 502, restaurant Ready). 새 후보, 설계 강도 1단 고정 evaluation.
            "F36-H",
            # 2026-10-10: banking account-service 를 관리 끝점을 정리한 릴리스 1.5.0(fault-images/f41-h)으로 롤아웃(헬스 하위 그룹을 꺼 매니페스트 liveness /actuator/health/liveness 가 404, 멀쩡한 컨테이너가 재시작을 되풀이해 잔액 조회 nginx 502, 이체 api 502)
            "F41-H",
            # 2026-10-10: food MySQL fooddelivery.dispatch_events 에 외래 키를 ALGORITHM, LOCK 없이 더하는 정합성 보강 마이그레이션(MySQL 이 표를 복사하며 dispatch_events 와 부모 dispatches 쓰기가 메타데이터 잠금 대기, dispatch 풀 고갈과 재시작, 주문 503, 복사가 끝나면 스스로 회복). 새 후보, 설계 강도 1단 고정 evaluation.
            "F60-R",
            # 2026-10-11: commerce PostgreSQL 을 새 인스턴스로 옮긴 이전이 inventory_schema.inventory_movements 의 id 시퀀스 상태를 옮기지 않아 시퀀스가 표보다 10만 키 뒤에서 다시 시작(db.ddl PostgreSQL 시퀀스 되감기: 옛 쪽 읽기 전용 기본값과 앱 세션 끊기로 약 6초 쓰기 25006, 잠금 아래 키 구간 확인 뒤 setval, 읽기 전용 해제와 다시 끊기, 표 통계는 그대로, 재고 예약 INSERT 가 기본 키 23505 로 실패, product 500, checkout 502, 둘러보기와 재고 조회 정상). 새 후보, 설계 강도 1단 고정 evaluation.
            "F57-P",
            # 2026-10-11: banking api-service 를 처리 마감 시간을 더한 릴리스 1.5.0(fault-images/f63-r)으로 롤아웃(요청마다 Executors.newFixedThreadPool(1) 을 만들고 닫지 않아 멈춘 스레드가 쌓여 컨테이너 메모리 한도에서 OOM 재시작 반복, 이체와 거래 내역 502).
            "F63-R",
            # 2026-10-11: commerce api-gateway 설정 롤아웃이 order 라우트 서킷브레이커에 checkout 정상 지연보다 낮은 느린 호출 임계(30ms)를 켜 멀쩡한 order 앞에서 서킷이 계속 열림. 새 후보, 설계 강도 1단 고정 evaluation.
            "F64-R",
            # 2026-10-11: banking transfer 일상 롤아웃이 낡은 DB 연결 서비스 이름(DB_SERVICE=XEPDB1)을 다시 넣어 새 파드가 ORA-12514 로 Oracle 에 붙지 못하고 maxSurge 0 이라 transfer 가 빔(이체·거래 내역·commerce 정산 502). 새 후보, 설계 강도 1단 고정 evaluation.
            "F65-R",
            # 2026-10-11: commerce 회원 표에 열을 더하는 마이그레이션이 같은 표를 읽는 느린 내보내기 질의 뒤에서 ACCESS EXCLUSIVE 를 기다려 그 뒤의 회원 표 질의가 모두 줄을 섬(토큰 확인이 user 풀을 채워 인가된 쓰기 500). 새 후보, 설계 강도 1단 고정 evaluation.
            "F66-R",
            # 2026-10-11: banking api 설정 롤아웃이 거래 내역 서킷브레이커(transferClient)에 거래 내역 정상 지연(약 240~250ms)보다 낮은 느린 호출 임계(100ms)를 켜 멀쩡한 transfer 앞에서 서킷이 계속 열림. 새 후보, 설계 강도 1단 고정 evaluation.
            "F64-P",
            # 2026-10-11: commerce 재고 이벤트 outbox 표에 열을 더하는 마이그레이션이 같은 표를 읽는 느린 이벤트 내보내기 뒤에서 ACCESS EXCLUSIVE 를 기다려 그 뒤의 outbox INSERT 가 모두 줄을 섬(재고 예약이 재고 행 잠금과 연결을 쥔 채 멈춰 inventory 풀 고갈, 상품 조회와 checkout 실패). 새 후보, 설계 강도 1단 고정 evaluation.
            "F66-P",
            # 2026-10-11: food 배차 표에 열을 더하는 마이그레이션(ALGORITHM=INSTANT)이 같은 표의 느린 내보내기 뒤에서 메타데이터 잠금을 기다려 그 뒤의 배차 표 질의가 모두 줄을 섬(dispatch 풀 고갈과 재시작, 모든 새 주문 503). 새 후보, 설계 강도 1단 고정 evaluation.
            "F60-H",
            # 2026-10-11: food MySQL 애플리케이션 계정 fooddelivery 에 시간당 쓰기 한도(MAX_UPDATES_PER_HOUR 500)를 걸음(연결이 바뀌는 대로 공유 카운터가 다 쓰여 주문 저장이 1226 으로 500, 읽기와 파드는 정상). 새 후보, 설계 강도 1단 고정 evaluation.
            "F54-P",
            # 2026-10-11: food 재배차 정정 작업이 주문 id 목록 없이 기간 조건(최근 4시간)만으로 실행되어 fooddelivery.dispatches 의 끝난 배차 수천 행을 배정 중으로 되돌려 배달원 용량(2000)이 가득 차고 모든 새 주문이 503(dispatch, order, MySQL 은 Ready, 설정 그대로). 새 후보, 설계 강도 1단 고정 evaluation.
            "F51-P",
            # 2026-10-11: banking account-service 를 이체 접수 응답을 정비한 릴리스 1.6.0(fault-images/f39-h)으로 롤아웃(POST /api/accounts/transfer 응답의 status 가 {code, label} 객체가 되어 account 와 transfer 는 이체를 실행하고 200 인데 api 가 공용 TransferResponse 로 읽지 못해 api 를 거치는 이체 전량 502, 잔액 조회와 commerce 정산 정상). 새 후보, 설계 강도 1단 고정 evaluation.
            "F39-H",
            # 2026-10-11: food dispatch-service 를 배달 조회 처리 마감 시간을 더한 릴리스 1.9.0(fault-images/f63-p)으로 롤아웃(배달 목록, 상세 조회마다 스레드 하나짜리 풀을 만들고 닫지 않아 요청마다 멈춘 비데몬 스레드가 남고, dispatch 컨테이너가 메모리 한도 1Gi 에서 커널 OOM 으로 죽고 다시 뜨기를 되풀이하며, 그 구간마다 order 의 배달원 용량 확인이 연결 거부로 끝나 주문 503. order, restaurant, MySQL 은 그대로). 새 후보, 설계 강도 1단 고정 evaluation.
            "F63-P",
            # 2026-10-11: 운영자가 공간을 되찾으려고 commerce 의 가장 큰 두 표 payment_logs, shipment_events 에 VACUUM (FULL) 을 트래픽 중에 나란히 돌려 다시 쓰는 내내 ACCESS EXCLUSIVE 를 쥠(결제마다 쓰는 payment_logs INSERT 가 기다려 payment 풀이 차고 order 결제 호출이 시간 초과 끝에 서킷 502, checkout 실패. 상품 둘러보기와 payments 표는 정상). 새 후보, 설계 강도 1단 고정 evaluation.
            "F67-R",
            # 2026-10-11: banking api-service 를 1회 이체 한도 확인을 더한 릴리스 1.6.0(fault-images/f37-h)으로 롤아웃(한도값을 이체마다 Environment.getRequiredProperty 로 읽는데 값이 개발 프로필 파일에만 있어 운영에서 Required key 예외, api 가 Ready 인 채 모든 이체 접수 500, 거래 내역과 잔액 조회 정상).
            "F37-H",
            # 2026-10-11: food 결제 표에 열을 더하는 마이그레이션(ALGORITHM=INSTANT)이 같은 표의 느린 내보내기 뒤에서 메타데이터 잠금을 기다려 그 뒤의 결제 표 질의가 모두 줄을 섬(payment 풀 고갈과 재시작, 모든 새 주문이 결제 단계에서 502). 새 후보, 설계 강도 1단 고정 evaluation.
            "F53-H",
            # 2026-10-11: commerce 주문 품목 표에 열을 더하는 마이그레이션이 같은 표의 느린 매출 내보내기 뒤에서 ACCESS EXCLUSIVE 를 기다려 그 뒤의 order_items 문장이 모두 줄을 섬(checkout 마다 품목 INSERT 가 결제 전에 order 연결을 쥔 채 멈춰 order 풀 고갈과 재시작, checkout 5xx). 새 후보, 설계 강도 1단 고정 evaluation.
            "F67-H",
            # 2026-10-11: food 배차 이벤트 outbox 표(dispatch_outbox_events)를 취소된 인덱스 백필이 치움(MySQL 1146, dispatch 가 outbox 기록 실패를 'non-critical' 로 잡지만 배차 트랜잭션이 rollback-only 라 커밋에서 되돌려져 500, 모든 새 주문 503). 새 후보, 설계 강도 1단 고정 evaluation.
            "F60-P",
            # 2026-10-11: 운영을 가리킨 로컬 마이그레이션이 banking Oracle 계좌 표(ACCOUNTS)를 지움(ORA-00942, 잔액 조회와 계좌 목록 500, 고객 이체는 account 계좌 확인에서 500 → api 502, commerce 정산은 transfer 계좌 잠금에서 500 → checkout 502, 거래 내역은 정상). 새 후보, 설계 강도 1단 고정 evaluation.
            "F35-P",
            # 2026-10-11: commerce notification-service 를 주문 이벤트 수신자 이메일을 회원 목록에서 찾는 릴리스 1.3.0(fault-images/f16-p)으로 롤아웃(목록에 없는 이름마다 뒤에서 user-service 회원 목록 전체를 페이지 상한 100번까지 다시 읽어 user 가 CPU 한도와 풀 대기에 묻히고, 게이트웨이의 쓰기 토큰 확인이 늦어지다 500·401 로 실패, 읽기 정상). 새 후보, 설계 강도 1단 고정 evaluation.
            "F16-P",
            # 2026-10-12: banking transfer-service 를 '출금 계좌당 초당 2건' 이상 거래 방지 한도를 더한 릴리스 2.4.0(fault-images/f68-r)으로 롤아웃(한도를 이름 하나의 resilience4j @RateLimiter 로 걸어 모든 계좌의 이체와 commerce 정산이 초당 2건 한 몫을 나눠 쓰고, 넘친 요청은 처리되지 않은 RequestNotPermitted 로 500 → banking 이체 502, commerce checkout 502, 합친 부하에 비례). 새 후보, 설계 강도 1단 고정 evaluation.
            "F68-R",
            # 2026-10-12: food 결제 감사 자료 내보내기 Job(payment-audit-export, mysql:8.0)이 mysqldump 기본값(--lock-tables)으로 payments, settlement_summary 를 떠 덤프 내내 표 읽기 잠금을 쥠(k8s.job 내보내기 계약, 결제 INSERT 가 메타데이터 잠금을 기다려 payment 풀 고갈과 재시작, 모든 새 주문이 결제 단계에서 502, 스키마·설정·롤아웃 변경 없음). 새 후보, 설계 강도 1단 고정 evaluation.
            "F69-R",
            # 2026-10-12: food 메뉴 표에 열을 더하는 마이그레이션(ALGORITHM=INSTANT)이 메뉴를 주문 품목과 이어 읽는 느린 판매 실적 내보내기 뒤에서 메타데이터 잠금을 기다려 그 뒤의 메뉴 읽기가 모두 줄을 섬(restaurant 풀 고갈과 재시작, 메뉴 보기와 주문 불가). 새 후보, 설계 강도 1단 고정 evaluation.
            "F36-P",
            # 2026-10-12: commerce user 일상 롤아웃이 회전 전 DB 계정 이름(DB_USER=commerce_app)을 Secret 참조 대신 다시 넣어 새 파드가 PostgreSQL 인증에 실패해 기동 중 죽고 maxSurge 0 이라 user 가 빔(게이트웨이 토큰 확인이 연결 거부로 실패해 쓰기 500). 새 후보, 설계 강도 1단 고정 evaluation.
            "F65-P",
            # 2026-10-12: banking 정산 대사 시스템이 거래 내역 API 에서 30일치 완료 이체를 page 없이 받아 가자 transfer 가 약 210만 행을 512MiB 힙에 올려 GC 와 OutOfMemoryError 로 멈추고 liveness 재시작을 되풀이함(load.north_south 새 부하 스크립트 statement-export.js, 배포·설정·DB 변경 없음 → banking 이체 502, commerce checkout 502). 새 후보, 설계 강도 1단 고정 evaluation.
            "F70-R",
            # 2026-10-12: commerce PostgreSQL 애플리케이션 역할 commerce 의 비밀번호 유효 기한(VALID UNTIL)이 갱신 없이 지나 새 로그인만 거절되고, 여덟 서비스의 Hikari 풀이 수명(10분, 30분)대로 비어 readiness 이탈과 재시작 반복(db.account PostgreSQL 비밀번호 기한 모드 → 쓰기 500, 상품 조회 502). 새 후보, 설계 강도 1단 고정 evaluation.
            "F71-R",
            # 2026-10-12: banking 워커 tb-w2 호스트 방화벽에 Oracle 리스너(1521) 허용 목록을 넣으며 같은 노드의 banking 앱 파드(account, transfer, ledger)를 빠뜨려 Hikari 풀이 수명대로 비고 readiness 이탈(Harness 2024-09-01 재구성, host.firewall 에 Oracle 리스너 계약 추가). 새 후보, 설계 강도 1단 고정 evaluation.
            "F72-R",
            # 2026-10-12: food 배달원 정산 시스템이 배달 목록 API 에서 배달 완료 내역을 page 없이 받아 가자 dispatch 가 약 174만 행을 기본 256MiB 힙에 올려 연속 GC 로 멈추고 liveness 재시작을 되풀이함(load.north_south 새 부하 스크립트 rider-settlement-export.js, 배포·설정·DB 변경 없음 → order 용량 확인 실패로 새 주문 503). 새 후보, 설계 강도 1단 고정 evaluation.
            "F70-P",
            # 2026-10-12: commerce 상품 표에 브랜드 열을 더하는 마이그레이션이 주문 품목에 상품을 붙여 읽는 느린 카테고리 매출 내보내기 뒤에서 ACCESS EXCLUSIVE 를 기다려 그 뒤의 products 문장이 모두 줄을 섬(product 풀 고갈과 재시작, 상품 둘러보기와 checkout 5xx). 새 후보, 설계 강도 1단 고정 evaluation.
            "F73-R",
            # 2026-10-12: banking 가맹점 입금 알림 제휴 연동이 거래 내역 API 확인 주기를 계좌당 1초로 줄이자 page=0&size=1 확인마다 transfer 가 표 전체를 세는 Page 건수 질의를 돌려 Oracle CPU 를 포화하고 transfer 풀이 묶임(load.north_south 새 부하 스크립트 settlement-arrival-poll.js, 배포·설정·DB 변경 없음 → banking 이체와 commerce checkout 간헐적 5xx). 새 후보, 설계 강도 1단 고정 evaluation.
            "F74-R",
            # 2026-10-12: food dispatch-service 만 배달원 용량 API 를 /api/deliveries/capacity 에서 /api/couriers/capacity 로 옮긴 릴리스 1.10.0(fault-images/f75-r)으로 롤아웃되고 짝 order 2.6.0 은 나가지 않아, 바뀌지 않은 order 의 용량 확인이 /api/deliveries/{id} 에 걸려 400 → 모든 새 주문 503(Stitch 2023-03-13 재구성, 기존 k8s.image release 모드에 계약 추가). 새 후보, 설계 강도 1단 고정 evaluation.
            "F75-R",
            # 2026-10-12: commerce product-service 를 액상 상품 니코틴 함량(mg)을 목록, 상세 응답에 더한 릴리스 1.4.0(fault-images/f76-r)으로 롤아웃, 설명의 'N mg' 을 Matcher.find() 확인 없이 group(1) 으로 읽어 함량 표기 없는 액상 상품이 든 상품 목록, 검색이 IllegalStateException 으로 500(상세, reserve-stock, checkout 정상, GitLab 2026-03-17 production#21573 재구성, 기존 k8s.image release 모드에 계약 추가). 새 후보, 설계 강도 1단 고정 evaluation.
            "F76-R",
            # 2026-10-12: food restaurant-service 를 메뉴 응답에 카테고리별 예상 조리 시간을 더한 릴리스 1.7.0(fault-images/f76-p)으로 롤아웃, 출시 카테고리 셋뿐인 표를 Map.get 확인 없이 int 로 받아 카테고리 4~8 가게 11곳의 메뉴가 NullPointerException 으로 500(상세, 검색, 인기 메뉴, 주문 정상, GitLab 2026-03-17 production#21573 재구성, 기존 k8s.image release 모드에 계약 추가). 새 후보, 설계 강도 1단 고정 evaluation.
            "F76-P",
            # 2026-10-12: 원장 재처리 워커를 띄우려고 transfer 매니페스트를 복사해 이미지만 원장 이미지로 바꾸고 이름을 그대로 둔 채 적용, 살아 있는 transfer Deployment 가 core-banking-ledger:latest 로 롤아웃되어 원장 앱이 8082 프로브에 답하지 못하고 재시작을 되풀이, transfer 엔드포인트가 비어 banking 이체와 거래 내역, commerce checkout 502(Coinbase 2026-07-14 재구성, k8s.image 에 foreign 모드 추가). 새 후보, 설계 강도 1단 고정 evaluation.
            "F77-R",
            # 2026-10-12: commerce PostgreSQL 운영자가 pg_hba 의 모든 주소 네트워크 규칙을 노드 주소와 관리망만 받도록 좁히고 다시 읽혀, 같은 노드 자기 파드 주소로 붙는 여덟 서비스의 새 로그인이 'no pg_hba.conf entry for host' 로 거절되고 Hikari 풀이 수명(10분, 30분)대로 비어 readiness 이탈과 재시작 반복, 쓰기 500·상품 조회 502(Harness 2024-09-01 재구성, 기존 db.account 실행기에 PostgreSQL 접근 규칙 좁히기 모드 추가). 새 후보, 설계 강도 1단 고정 evaluation.
            "F71-H",
            # 2026-10-12: food payment-service 를 외부 PG 결제 주소를 기본 주소와 결제 경로를 구분자 없이 이어 만드는 릴리스 1.4.0(fault-images/f79-r)으로 롤아웃, 운영 PG_API_URL 이 '/' 로 끝나지 않아 'http://testbed-external-pg-mock:9090pay' 가 되어 결제 요청을 만들기 전에 'Bad authority' 로 모든 결제 502, 주문 전량 502(PG 수신 0, rust-lang crates.io 2023-07-20 재구성, 기존 k8s.image release 모드에 계약 추가). 새 후보, 설계 강도 1단 고정 evaluation.
            "F79-R",
            # 2026-10-16: banking 워커 tb-w2 의 Oracle 데이터 파일 일상 백업(systemd 유닛 oradata-backup, tar | zstd -12 --long=31)이 2 GiB 장거리 창으로 노드 남은 메모리를 거의 다 써 kubelet 이 Oracle 을 축출하거나 커널이 banking 자바 컨테이너를 OOM 으로 죽임(배포·설정 변경 없음 → banking 이체 502, commerce checkout 502, GitHub 2026-09-20 재구성, 새 실행기 host.job). 새 후보, 설계 강도 1단 고정 evaluation.
            "F80-R",
            # 2026-10-30: commerce inventory-service 를 재고 재조정 배치가 재고 행 16개를 공유 잠금(FOR SHARE)으로 쥔 채 상품마다 45일 이동 이력을 합산하는 릴리스 1.3.0(fault-images/f69-p)으로 롤아웃, 배치마다 약 45초 동안 재고 예약이 기다려 checkout 이 약 105초마다 5xx(KnowBe4 2025-10-13 재구성, 기존 k8s.image release 모드에 계약 추가). 새 후보, 설계 강도 1단 고정 evaluation.
            "F69-P",
        }
        catalog = json.loads((ROOT / "catalog.json").read_text())
        actual = {row["id"] for row in catalog["scenarios"] if compiler.compile_plan(row["slug"])["live_allowed"]}
        self.assertEqual(actual, expected)
        plan = compiler.compile_plan("f12-h-pod-cpu-network-lookalike")
        self.assertTrue(plan["live_allowed"])
        self.assertEqual(plan["profile_instances"][0]["location_id"], "commerce-namespace")

    def test_f04r_uses_exact_live_contracts(self) -> None:
        # F02-P dropped out of this test on 2026-07-27: it is parked (no food
        # query rides idx_menus_category, so the DDL drop injures nothing), and
        # a parked scenario must not compile to a live plan. Its executor
        # contract is still guarded — see the parked-archive assertion in
        # test_data_network_executor_foundations.py.
        cases = [
            ("f04-r-commerce-shipping-consumer-stop", "kafka.control", kafka_control, "commerce-namespace"),
        ]
        for slug, profile_id, module, location_id in cases:
            plan = compiler.compile_plan(slug)
            self.assertTrue(plan["live_allowed"])
            instance = next(row for row in plan["profile_instances"] if row["profile_id"] == profile_id)
            self.assertEqual(instance["location_id"], location_id)
            self.assertEqual(instance["parameters"], module.CONTRACTS[plan["scenario"]["id"]])
            module.validate(plan["scenario"]["id"], instance["parameters"], self.profiles[profile_id])

    def test_new_adaptive_profiles_accept_only_predeclared_levels(self) -> None:
        cases = [
            # 2026-08-09: host.stress 사례를 F09-R 에서 F10-H 로 옮겼다. F09-R 이 파킹되어
            # live_allowed 가 False 가 됐기 때문이고, 이 테스트가 보려는 것은 시나리오가
            # 아니라 "사다리 레벨이 사전 선언된 것만 통과하는가"이므로 같은 프로파일을
            # 쓰는 라이브 3단 시나리오면 목적이 유지된다.
            # 2026-08-20: F11-R·F10-H 가 pin 단으로 고정 승격됐다. 라이브 3단 사다리로
            # 남은 것은 F10-P(미pin) 하나라 그리로 옮긴다. load.north_south 에는 더 이상
            # 사다리가 없어 그 사례는 뺀다.
            # 2026-10-08: F10-P 가 폐기(정상 녹화 없음, 사용자 결정)돼 라이브 host.stress 사다리가
            # 하나도 남지 않았다. 이 테스트가 보는 것은 사다리 레벨 검증이고 그 계약은 파킹 뒤에도
            # 그대로 컴파일되므로 F10-P 를 두고, 라이브 여부만 현재 상태(False)로 맞춘다.
            ("f10-p-oracle-io-saturation", "host.stress", host_stress, False),
        ]
        for slug, profile_id, module, expected_live in cases:
            plan = compiler.compile_plan(slug)
            instance = next(
                row for row in plan["profile_instances"] if row["profile_id"] == profile_id
            )
            self.assertEqual(len(instance["approved_levels"]), 3)
            self.assertEqual(plan["live_allowed"], expected_live)
            if module is not None:
                for level in instance["approved_levels"]:
                    module.validate(plan["scenario"]["id"], level["parameters"], self.profiles[profile_id])


    def _db_workload_plan(self) -> dict:
        """F02-H는 아직 db.workload에 묶여 있지 않다(판정 계약은 실측 후 별건).

        그래서 `compile_plan`으로는 이 프로파일 인스턴스를 얻을 수 없다. 실행기가
        받는 것과 같은 모양의 계획을 계약 표에서 직접 만든다 — 검증 대상은 계획
        컴파일러가 아니라 실행기 자신이다.
        """
        scenario_id = "F02-H"
        return {
            "scenario": {"id": scenario_id},
            "profile_instances": [
                {
                    "profile_id": "db.workload",
                    "parameters": dict(db_workload.CONTRACTS[scenario_id]),
                }
            ],
        }

    def test_db_workload_contract_matches_the_approved_profile(self) -> None:
        # 실행기 표와 레지스트리가 갈라지면 주입은 승인되지 않은 값으로 나간다.
        profile = self.profiles["db.workload"]
        self.assertTrue(profile["live_supported"])
        self.assertEqual(
            sorted(profile["parameter_contract"]["allowed_scenarios"]),
            sorted(db_workload.CONTRACTS),
        )
        for scenario_id, contract in db_workload.CONTRACTS.items():
            self.assertEqual(profile["scenario_parameters"][scenario_id], contract)

    def test_db_workload_scan_never_confesses_the_injection(self) -> None:
        # G6/L2. 세 축이 전부 자연 분포 안에 있어야 한다(2026-07-27 캡처 실측).
        plan = self._db_workload_plan()
        params = plan["profile_instances"][0]["parameters"]
        argv, stdin = db_workload.build_invocation(plan, "run")
        script = stdin.decode()

        # ① 세션 신원이 실제 앱과 같아야 한다 — 시나리오 이름이 새면 그 자체가 정답이다.
        self.assertEqual(params["client_identity"], "PostgreSQL JDBC Driver")
        for fragment in db_workload.FORBIDDEN_IDENTITY_FRAGMENTS:
            with self.assertRaises(ExecutorError):
                db_workload.validate(
                    "F02-H", dict(params, client_identity=f"batch-{fragment}-job"), {}
                )

        # ② 주기 대기는 클라이언트측이어야 한다. 서버측 pg_sleep은 실운영에 없는
        #    PgSleep 대기를 남겨 주입을 혼자 식별시킨다.
        self.assertNotIn("pg_sleep", script.lower())
        self.assertIn('sleep "\\$PERIOD"', script)

        # ③ 질의문이 파드 argv에 실리면 노드 프로세스 수집기가 그대로 캡처한다.
        #    SQL은 env(SCAN_SQL)로만 들어가고, 러너 argv에도 완성된 SELECT는 없다.
        self.assertIn("SCAN_SQL", script)
        self.assertNotIn("SELECT count(*)", " ".join(argv))
        self.assertNotIn("payment_logs;", " ".join(argv))

    def test_db_workload_stays_read_only_and_inside_the_cluster(self) -> None:
        plan = self._db_workload_plan()
        params = plan["profile_instances"][0]["parameters"]
        script = db_workload.build_invocation(plan, "run")[1].decode()

        # 읽기 전용은 선언이 아니라 서버측 강제여야 한다. 업무 테이블 쓰기·잠금은
        # F02-H 설계의 명시 금지이고, 잠금은 db.lock의 표면이라 겹치면 원인이 갈리지 않는다.
        self.assertIn("default_transaction_read_only = on", script)
        self.assertIn("statement_timeout", script)
        for banned in ("LOCK TABLE", "FOR UPDATE", "INSERT ", "UPDATE ", "DELETE "):
            self.assertNotIn(banned, script)

        # 정리는 파드 삭제여야 한다. 신원이 앱과 같아졌으므로 이름으로 쏘는
        # pg_terminate_backend는 실 서비스 세션을 죽인다(db.lock이 먼저 배운 것).
        self.assertNotIn("pg_terminate_backend", script)
        self.assertIn("delete pod", script)

        # 클러스터 밖에서 붙으면 client_addr이 tb-runner의 NAT 주소로 드러난다.
        with self.assertRaises(ExecutorError):
            db_workload.validate("F02-H", dict(params, access="tb-runner"), {})

    def test_db_workload_period_is_a_range_not_an_equality(self) -> None:
        # 사다리가 단마다 주기를 바꾼다. 동일성으로만 검증하면 기본값 아닌 단이
        # 통째로 거부된다(배치 #12의 재발 형태).
        params = dict(db_workload.CONTRACTS["F02-H"])
        for good in (db_workload.MIN_PERIOD_SECONDS, 2, 5, db_workload.MAX_PERIOD_SECONDS):
            db_workload.validate("F02-H", dict(params, period_seconds=good), {})
        for bad in (0, -1, db_workload.MAX_PERIOD_SECONDS + 1, "10", 10.0, True, None):
            with self.assertRaises(ExecutorError):
                db_workload.validate("F02-H", dict(params, period_seconds=bad), {})
        # 나머지 키는 여전히 동일성이다 — 대상이 조용히 바뀌면 안 된다.
        for drift in ("table", "schema", "namespace", "db_pod"):
            with self.assertRaises(ExecutorError):
                db_workload.validate("F02-H", dict(params, **{drift: "somewhere-else"}), {})

    def test_db_workload_refuses_unlisted_scenarios_and_modes(self) -> None:
        params = dict(db_workload.CONTRACTS["F02-H"])
        with self.assertRaises(ExecutorError):
            db_workload.validate("F03-P", params, {})
        with self.assertRaises(ExecutorError):
            db_workload.validate("F02-H", dict(params, mode="occupy"), {})
        with self.assertRaises(ExecutorError):
            db_workload.validate("F02-H", dict(params, engine="mysql"), {})
        # 레지스트리가 막으면 실행기도 막아야 한다.
        with self.assertRaises(ExecutorError):
            db_workload.validate(
                "F02-H", params, {"parameter_contract": {"allowed_scenarios": ["F09-Z"]}}
            )


class NodeGuardTests(unittest.TestCase):
    """executor_common guard: the timer a node executor arms on the node so that the node undoes
    the change by itself when cleanup never comes (U8, after F80-R-run-bad63a76)."""

    common = sys.modules["executor_common"]

    def _guard(self, env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
        import shlex

        preamble = ("set -- " + " ".join(shlex.quote(a) for a in args) + "\n").encode()
        return subprocess.run(["bash", "-s"], input=preamble + self.common.GUARD_PROGRAM,
                              env={**os.environ, **env}, capture_output=True, timeout=60)

    @staticmethod
    def _state(tmp: Path) -> list[str]:
        return sorted(x.name for x in (tmp / "state").iterdir())

    def test_arm_postpone_disarm_and_a_second_disarm(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = node_guard_fakes.install(tmp)
            body = f"echo undone >>{tmp}/undone"
            done = self._guard(env, "arm", "nightly", "1800", body, "/var/x/out.bin", "/var/x")
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(len(node_guard_fakes.timers(env)), 1)
            [start] = node_guard_fakes.starts(env, "nightly")
            self.assertEqual((start["seconds"], start["props"]), ("1800", "AccuracySec=1s RemainAfterElapse=no"))
            undo = tmp / "state" / "nightly.undo"
            self.assertTrue(node_guard_fakes.launches(env, "nightly"))
            self.assertEqual(undo.stat().st_mode & 0o777, 0o700)
            self.assertEqual((tmp / "state").stat().st_mode & 0o777, 0o700)
            self.assertEqual(undo.read_text().splitlines()[:3], ["#!/bin/bash", "set -euo pipefail", body])
            unit, due, rule = node_guard_fakes.unit_record(env, "nightly")
            self.assertEqual(unit, start["unit"])
            rule = Path(rule)
            self.assertEqual(rule.read_text().splitlines(),
                             ["r! /var/x/out.bin", "r! /var/x", f"r! {undo}", f"r! {tmp / 'state' / 'nightly.unit'}", f"r! {rule}"])
            # cleanup on time: far from the deadline, postpone changes nothing (no journal line).
            self.assertEqual(self._guard(env, "postpone", "nightly", "960").returncode, 0)
            self.assertEqual(node_guard_fakes.armed_seconds(env, "nightly"), [1800])
            # cleanup late: fewer seconds left than the hold, so the timer restarts under a fresh name.
            (tmp / "state" / "nightly.unit").write_text(f"{unit} {int(due) - 1700} {rule}\n")
            self.assertEqual(self._guard(env, "postpone", "nightly", "960").returncode, 0)
            self.assertEqual(node_guard_fakes.armed_seconds(env, "nightly"), [1800, 960])
            self.assertEqual(len(node_guard_fakes.timers(env)), 1)
            later = node_guard_fakes.unit_record(env, "nightly")
            self.assertNotEqual(later[0], unit)
            self.assertEqual(later[2], str(rule))
            self.assertTrue(undo.exists())
            # cleanup undid the change itself: no timer, no undo, no boot rule; nothing ran.
            done = self._guard(env, "disarm", "nightly")
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(node_guard_fakes.timers(env), [])
            self.assertEqual(self._state(tmp), [])
            self.assertEqual(list((tmp / "tmpfiles.d").iterdir()), [])
            self.assertFalse((tmp / "undone").exists())
            # A retried cleanup: postpone arms nothing without an undo, disarm is a no-op.
            self.assertEqual(self._guard(env, "postpone", "nightly", "960").returncode, 0)
            self.assertEqual(node_guard_fakes.armed_seconds(env, "nightly"), [1800, 960])
            self.assertEqual(self._guard(env, "disarm", "nightly").returncode, 0)
            self.assertEqual(node_guard_fakes.timers(env), [])

    def test_the_timer_undoes_by_itself_once_and_cleanup_hears_of_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = node_guard_fakes.install(tmp)
            body = f"echo undone >>{tmp}/undone"
            self.assertEqual(self._guard(env, "arm", "nightly", "1800", body).returncode, 0)
            fired = node_guard_fakes.fire(env, "nightly")
            self.assertEqual(fired.returncode, 0, fired.stderr)
            self.assertEqual((tmp / "undone").read_text(), "undone\n")
            # The undo leaves only the time it ran; its own file, the unit record and the boot rule are gone.
            self.assertEqual(self._state(tmp), ["nightly.expired"])
            self.assertRegex((tmp / "state" / "nightly.expired").read_text(), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\n$")
            self.assertEqual(list((tmp / "tmpfiles.d").iterdir()), [])
            # A late cleanup postpones nothing (the undo is gone) and its disarm reports the expiry.
            self.assertEqual(self._guard(env, "postpone", "nightly", "960").returncode, 0)
            self.assertEqual(node_guard_fakes.timers(env), [])
            done = self._guard(env, "disarm", "nightly")
            self.assertEqual(done.returncode, 0)
            self.assertIn(b"the node undid the change by itself at ", done.stderr)
            self.assertEqual(self._state(tmp), [])

    def test_rearm_replaces_a_stale_timer_and_waits_for_a_running_undo(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = node_guard_fakes.install(tmp)
            self.assertEqual(self._guard(env, "arm", "nightly", "1800", "true").returncode, 0)
            stale, _, stale_rule = node_guard_fakes.unit_record(env, "nightly")
            done = self._guard(env, "arm", "nightly", "2400", "true")
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(node_guard_fakes.armed_seconds(env, "nightly"), [1800, 2400])
            self.assertEqual(len(node_guard_fakes.timers(env)), 1)
            # The stale timer is stopped and its boot rule dropped before the new one starts.
            calls = node_guard_fakes.calls(env)
            fresh = node_guard_fakes.starts(env, "nightly")[1]["unit"]
            self.assertLess(calls.index(f"ctl stop {stale}.timer"),
                            next(i for i, c in enumerate(calls) if c.startswith(f"run {fresh} ")))
            self.assertFalse(Path(stale_rule).exists())
            self.assertEqual(len(list((tmp / "tmpfiles.d").iterdir())), 1)
            # Whoever holds the job's lock (an undo under way, another guard call) is waited for; one
            # that never lets go stops arm and disarm, which fail rather than run beside it.
            (tmp / "locks").mkdir(exist_ok=True)
            holder = subprocess.Popen(["flock", "-o", str(tmp / "locks" / "nightly.lock"), "sleep", "30"])  # -o: only flock holds it
            try:
                time_limit = dict(env, OPSJOB_LOCK_WAIT="1")
                for _ in range(50):
                    if subprocess.run(["flock", "-n", str(tmp / "locks" / "nightly.lock"), "true"]).returncode:
                        break
                    import time as _t
                    _t.sleep(0.1)
                for args in (("disarm", "nightly"), ("arm", "nightly", "1800", "true"), ("postpone", "nightly", "960")):
                    stuck = self._guard(time_limit, *args)
                    self.assertNotEqual(stuck.returncode, 0)
                    self.assertIn(b"guard: nightly is busy after 1s", stuck.stderr)
                self.assertEqual(node_guard_fakes.timers(env), [f"{fresh}.timer"])
            finally:
                holder.kill()
                holder.wait(timeout=10)

    def test_a_failed_undo_keeps_the_boot_rule_and_late_cleanup_reports_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = node_guard_fakes.install(tmp)
            self.assertEqual(self._guard(env, "arm", "nightly", "1800", "false", "/var/x/out.bin").returncode, 0)
            unit, due, rule = node_guard_fakes.unit_record(env, "nightly")
            fired = node_guard_fakes.fire(env, "nightly")
            self.assertNotEqual(fired.returncode, 0)
            # Nothing claims an expiry that did not finish; a reboot or a person can still finish it.
            self.assertEqual(self._state(tmp), ["nightly.undo", "nightly.unit"])
            self.assertEqual(len(list((tmp / "tmpfiles.d").iterdir())), 1)
            (tmp / "systemd" / f"{unit}.service.failed").touch()
            # The real cleanup order: a late postpone (the deadline has passed) gets there first and says
            # it, before its reset-failed clears the state; disarm then has nothing more to say.
            (tmp / "state" / "nightly.unit").write_text(f"{unit} {int(due) - 1800} {rule}\n")
            late = self._guard(env, "postpone", "nightly", "960")
            self.assertEqual(late.returncode, 0)
            self.assertIn(f"{unit}.service failed; cleanup undoes the change itself".encode(), late.stderr)
            self.assertFalse((tmp / "systemd" / f"{unit}.service.failed").exists())
            done = self._guard(env, "disarm", "nightly")
            self.assertEqual(done.returncode, 0)
            self.assertNotIn(b"by itself at", done.stderr)
            self.assertEqual(self._state(tmp), [])
            self.assertEqual(list((tmp / "tmpfiles.d").iterdir()), [])

    def _fires_during_stop(self, tmp: Path, env: dict[str, str]) -> tuple[dict[str, str], Path]:
        """systemctl as PID 1 does it when the deadline passes just as a guard call stops the timer:
        the timer has already started its service, which stopping the timer does not stop. The old
        timer's command runs in the background, its rc and output kept for the test."""
        wrap = tmp / "ctl-wrap"
        out = tmp / "fired"
        wrap.write_text(f"""#!/usr/bin/env bash
if [[ "$1" == stop && "$2" == *.timer && ! -e {out}.rc ]]; then
  mapfile -t argv <"$FAKE_SYSTEMD/$2"
  # PID 1 starts the service, so it shares no descriptor (the guard's lock) with this caller.
  ( exec 9>&-; "${{argv[@]}}" >{out}.out 2>{out}.err; echo $? >{out}.rc.tmp; mv {out}.rc.tmp {out}.rc ) &
  sleep 0.3
fi
exec {env['OPSJOB_SYSTEMCTL']} "$@"
""")
        wrap.chmod(0o755)
        return dict(env, OPSJOB_SYSTEMCTL=str(wrap)), out

    @staticmethod
    def _wait_rc(out: Path) -> int:
        import time as _t
        for _ in range(200):
            if (out.parent / f"{out.name}.rc").exists():
                return int((out.parent / f"{out.name}.rc").read_text())
            _t.sleep(0.05)
        raise AssertionError("the fired undo never finished")

    def test_a_timer_that_fires_while_postpone_stops_it_leaves_no_orphan(self) -> None:
        # 2026-11-09 review, reproduced: the timer fired between postpone's check and its stop. postpone
        # then armed a new timer, the old undo removed the new record on its way out, disarm could not
        # see the new timer, and 960s later it failed with "/bin/bash: .../soak.undo: No such file or
        # directory" in the node's journal (forwarded to 119).
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = node_guard_fakes.install(tmp)
            body = f"echo undone >>{tmp}/undone"
            self.assertEqual(self._guard(env, "arm", "soak", "1800", body).returncode, 0)
            unit, due, rule = node_guard_fakes.unit_record(env, "soak")
            (tmp / "state" / "soak.unit").write_text(f"{unit} {int(due) - 1700} {rule}\n")
            racing, out = self._fires_during_stop(tmp, env)
            late = self._guard(racing, "postpone", "soak", "960")
            self.assertEqual(late.returncode, 0, late.stderr)
            # The old undo waited for the lock, saw it was no longer the armed unit and left quietly.
            self.assertEqual(self._wait_rc(out), 0)
            self.assertEqual((tmp / "fired.err").read_text(), "")
            self.assertFalse((tmp / "undone").exists())
            self.assertEqual(node_guard_fakes.armed_seconds(env, "soak"), [1800, 960])
            fresh = node_guard_fakes.unit_record(env, "soak")[0]
            self.assertEqual(node_guard_fakes.timers(env), [f"{fresh}.timer"])
            # cleanup finishes: disarm sees the new timer and stops it; nothing is left to fire.
            self.assertEqual(self._guard(env, "disarm", "soak").returncode, 0)
            self.assertEqual(node_guard_fakes.timers(env), [])
            self.assertEqual(self._state(tmp), [])

    def test_a_timer_that_fires_while_disarm_stops_it_does_nothing_and_says_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = node_guard_fakes.install(tmp)
            body = f"echo undone >>{tmp}/undone"
            self.assertEqual(self._guard(env, "arm", "soak", "1800", body).returncode, 0)
            racing, out = self._fires_during_stop(tmp, env)
            done = self._guard(racing, "disarm", "soak")
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(self._wait_rc(out), 0)
            self.assertEqual((tmp / "fired.err").read_text(), "")
            self.assertFalse((tmp / "undone").exists())
            self.assertEqual(node_guard_fakes.timers(env), [])
            self.assertEqual(self._state(tmp), [])

    def test_an_undo_that_wins_the_lock_runs_once_and_postpone_then_does_nothing(self) -> None:
        # The other order: the timer's undo takes the lock first. It does the job, and the postpone
        # behind it finds no undo left and arms nothing.
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = node_guard_fakes.install(tmp)
            body = f"echo undone >>{tmp}/undone; sleep 1"
            self.assertEqual(self._guard(env, "arm", "soak", "1800", body).returncode, 0)
            unit, due, rule = node_guard_fakes.unit_record(env, "soak")
            (tmp / "state" / "soak.unit").write_text(f"{unit} {int(due) - 1700} {rule}\n")
            argv = node_guard_fakes.command(env, "soak")
            undo = subprocess.Popen(argv, env={**os.environ, **env}, stderr=subprocess.PIPE)
            import time as _t
            for _ in range(50):
                if (tmp / "undone").exists():
                    break
                _t.sleep(0.05)
            late = self._guard(env, "postpone", "soak", "960")
            self.assertEqual(undo.wait(timeout=30), 0)
            self.assertEqual(undo.stderr.read(), b"")
            undo.stderr.close()
            self.assertEqual(late.returncode, 0, late.stderr)
            self.assertEqual((tmp / "undone").read_text(), "undone\n")
            self.assertEqual(node_guard_fakes.armed_seconds(env, "soak"), [1800])
            self.assertEqual(self._state(tmp), ["soak.expired"])

    def test_lock_and_state_directories_are_closed_to_others(self) -> None:
        # Both hold file names that carry the job's name (oradata-backup, the chain), so only root reads them.
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = node_guard_fakes.install(tmp)
            (tmp / "locks").mkdir(mode=0o755)
            self.assertEqual(self._guard(env, "arm", "nightly", "1800", "true").returncode, 0)
            self.assertEqual((tmp / "locks").stat().st_mode & 0o777, 0o700)
            self.assertEqual((tmp / "state").stat().st_mode & 0o777, 0o700)
            (tmp / "locks").chmod(0o755)
            self.assertEqual(self._guard(env, "disarm", "nightly").returncode, 0)
            self.assertEqual((tmp / "locks").stat().st_mode & 0o777, 0o700)

    def test_an_undo_that_cannot_get_the_lock_fails_loudly_instead_of_waiting_forever(self) -> None:
        # 2026-11-09 review: a leaked lock left launchers waiting in `flock 9` for good. Now the undo
        # gives up after the launch wait, fails (the unit fails, the next guard call reports it) and
        # leaves its files for cleanup or the next boot; the message names no job.
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = dict(node_guard_fakes.install(tmp), OPSJOB_LAUNCH_WAIT="1")
            self.assertEqual(self._guard(env, "arm", "nightly", "1800", f"echo undone >>{tmp}/undone").returncode, 0)
            self.assertTrue(node_guard_fakes.launches(env, "nightly"))
            holder = subprocess.Popen(["flock", "-o", str(tmp / "locks" / "nightly.lock"), "sleep", "30"])  # -o: only flock holds it
            try:
                for _ in range(50):
                    if subprocess.run(["flock", "-n", str(tmp / "locks" / "nightly.lock"), "true"]).returncode:
                        break
                    import time as _t
                    _t.sleep(0.1)
                fired = node_guard_fakes.fire(env, "nightly")
            finally:
                holder.kill()
                holder.wait(timeout=10)
            self.assertEqual(fired.returncode, 1)
            self.assertEqual(fired.stderr, b"lock busy after 1s\n")
            self.assertFalse((tmp / "undone").exists())
            self.assertEqual(self._state(tmp), ["nightly.undo", "nightly.unit"])

    def test_guarded_bodies_keep_the_failure_trace_and_their_line_numbers(self) -> None:
        # The deadline rides on the `set -euo pipefail` line: the shebang stays first, so the
        # executor's failure trace (with_failure_trace) still applies, and no line is added, so the
        # line it reports is the source line.
        for slug, module in (("f80-r-banking-oradata-backup-zstd-exhausts-node-memory", host_job),
                             ("f44-r-food-node-firewall-allowlist-omits-order", host_firewall)):
            with self.subTest(executor=module.PROFILE_ID):
                plan = compiler.compile_plan(slug)
                _, stdin = module.build_invocation(plan, "run")
                self.assertTrue(stdin.startswith(b"#!/usr/bin/env bash\n"))
                self.assertEqual(stdin.count(b"\n"), module.SCRIPT.count(b"\n"))
                expiry = self.common.guard_expiry_seconds(plan)
                self.assertEqual(stdin.split(b"\n")[1], f"set -euo pipefail; expire_after={expiry} guard_hold=960".encode())
                self.assertNotEqual(self.common.with_failure_trace(stdin), stdin)
        with self.assertRaises(ExecutorError):
            self.common.with_guard_values(b"set -euo pipefail\n", {})

    def test_arm_refuses_a_bad_deadline_or_boot_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = node_guard_fakes.install(tmp)
            for args in (("0", "true"), ("12m", "true"), ("1800", "true", "relative/path"), ("1800", "true", "/a b")):
                with self.subTest(args=args):
                    done = self._guard(env, "arm", "nightly", *args)
                    self.assertNotEqual(done.returncode, 0)
                    self.assertEqual(node_guard_fakes.timers(env), [])
            self.assertNotEqual(self._guard(env, "fire", "nightly").returncode, 0)

    def test_the_journal_trace_names_nothing(self) -> None:
        # Design principle 8: the expiry timer is a tool trace no real incident leaves, so what the
        # node's journal (forwarded to 119) shows of it is only the generic systemd-run form.
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = node_guard_fakes.install(tmp)
            for job in ("oradata-backup", "restaurant-api-in", "burnin"):
                self.assertEqual(self._guard(env, "arm", job, "1800", "true").returncode, 0)
                (tmp / "state" / f"{job}.unit").write_text(
                    " ".join([node_guard_fakes.unit_record(env, job)[0], "0", node_guard_fakes.unit_record(env, job)[2]]) + "\n")
                self.assertEqual(self._guard(env, "postpone", job, "960").returncode, 0)
                self.assertEqual(node_guard_fakes.armed_seconds(env, job), [1800, 960])
                self.assertEqual(node_guard_fakes.journal_words(env, job), [])
                rule = Path(node_guard_fakes.unit_record(env, job)[2]).name
                self.assertRegex(rule, r"^run-r[0-9a-f]{32}\.conf$")
            # The program itself passes an empty description and never a unit name of its own making.
            program = self.common.GUARD_PROGRAM.decode()
            self.assertIn("--description= ", program)
            self.assertEqual(program.count("--unit="), 1)
            self.assertIn('--unit="$g_unit"', program)
            # Callers hand the guard no wording: arm takes the job, the deadline, the undo and paths only.
            for name in ("host_job_executor.py", "host_firewall_executor.py", "host_stress_executor.py"):
                source = (PROFILES / name).read_text()
                self.assertNotIn("expiry", source)
                calls = re.findall(r'(?:node_guard arm|guard_arm) [^\n]*', source)
                self.assertTrue(calls, name)
                for call in calls:
                    self.assertRegex(call, r'^(?:node_guard arm "\$(?:unit|gjob)" "\$expire_after" "\$undo"|guard_arm "\$job" "\$expire_after" "\$@"; \})')

    def test_deadline_follows_the_runner_limit_plus_margin(self) -> None:
        f80 = compiler.compile_plan("f80-r-banking-oradata-backup-zstd-exhausts-node-memory")
        f72 = compiler.compile_plan("f72-r-banking-node-firewall-omits-oracle-app-clients")
        self.assertEqual(self.common.guard_expiry_seconds(f80), 20 * 60 + 600)
        self.assertEqual(self.common.guard_expiry_seconds(f72), 30 * 60 + 600)
        # The runner's own default when a plan states no limit (rca-scenario-runner controller: 1200).
        self.assertEqual(self.common.guard_expiry_seconds({"scenario": {"id": "X"}}), 1200 + 600)
        self.assertEqual(self.common.guard_stdin_line(f80), b"expire_after=1800 guard_hold=960\n")
        # The hold outlasts one executor call (execute() bounds it at 900s).
        self.assertGreater(self.common.GUARD_CLEANUP_HOLD_SECONDS, 900)
        for bad in ("10x", "-5m", True, 0):
            with self.subTest(bad=bad):
                with self.assertRaises(ExecutorError):
                    self.common.guard_expiry_seconds({"controller": {"runtime": {"max_injection_duration": bad}}})

    def test_names_on_the_node_are_neutral(self) -> None:
        # The guard's units, files and descriptions reach the node's journal (forwarded to 119).
        for raw in self.common.GUARD_PROGRAM.decode().splitlines():
            for word in ("lucida", "scenario", "testbed", "rca", "inject"):
                self.assertNotIn(word, raw.lower())

    # Every profile whose plan runs it on a cluster node must arm the guard, or be listed here with the
    # reason it cannot. The node set comes from the compiled plans (location.node or a node address),
    # not from strings in the executor, so a new node executor (for example the network injector,
    # once its location resolves) cannot land without one or the other.
    CLUSTER_NODES = {"192.168.122.77", "192.168.122.184", "192.168.122.11", "192.168.122.14"}
    NO_NODE_UNDO = {
        # The change is the removal of images the node held. Undoing it on the node needs a copy of
        # the images on the node (about 670 MB for F52-R), written at the injection; that would add a
        # disk write and an export process to the recording (decided 2026-11-09: left as it is). The
        # node stays Ready, so the runner's cleanup can always reach it.
        "host.image",
    }
    # Runs from tb-ops through the cluster API and reaches a node only on the side.
    NODE_SIDE_ONLY = {
        # The change is the Deployment's image (cluster API); the node only gains one extra image.
        "k8s.image",
    }
    # Node executors that no catalog scenario uses yet, with a sample contract in their own shape.
    UNUSED_NODE_EXECUTORS = {
        # N1: delay and loss on a node's pod overlay device; scenarios come with N4.
        "network.fault": ("N1-GUARD", {
            "host": "192.168.122.184", "node": "tb-w1", "device": "flannel.1",
            "delay_ms": 100, "jitter_ms": 0, "loss_percent": 0,
            "rules": [{"dst": "10.244.1.0/24", "protocol": "tcp", "dport": 8082}],
        }),
    }
    LOCATIONS = json.loads((ROOT / "registry" / "locations.json").read_text())["locations"]

    @staticmethod
    def _executor_source(profile: dict) -> Path:
        path = ROOT / profile["executor"]
        if path.suffix == ".sh":
            match = re.search(r'([a-z_]+_executor\.py)', path.read_text())
            if match:
                return PROFILES / match.group(1)
        return path

    @staticmethod
    def _arms(stdin: bytes) -> bool:
        code = [line for line in stdin.decode().splitlines() if not line.lstrip().startswith("#")]
        defined = any(line.startswith("guard_arm_now()") for line in code)
        called = any(re.search(r'(node_guard arm "|(^|[\s;])arm "\$\()', line) for line in code)
        return defined and called

    def test_every_node_executor_arms_the_guard_or_says_why_not(self) -> None:
        registry = json.loads((ROOT / "registry" / "profiles.json").read_text())["profiles"]
        catalog = json.loads((ROOT / "catalog.json").read_text())["scenarios"]
        rows = catalog.values() if isinstance(catalog, dict) else catalog
        on_node: dict[str, dict] = {}
        for row in rows:
            plan = compiler.compile_plan(row["slug"])
            for instance in plan["profile_instances"]:
                location = instance.get("location", {})
                if location.get("transport") != "ssh":
                    continue
                if location.get("host") is None:
                    # An unresolved place cannot be live; once it resolves to a node it is checked below.
                    self.assertFalse(registry[instance["profile_id"]]["live_supported"], instance["profile_id"])
                elif location.get("node") or location["host"] in self.CLUSTER_NODES:
                    on_node.setdefault(instance["profile_id"], plan)
        self.assertEqual(set(on_node), {"host.firewall", "host.image", "host.job", "host.stress"})
        # A node executor no scenario uses yet is checked with a contract of its own shape, as long as
        # the registry lets it run on a node. Once a scenario uses it, the catalog check above takes over.
        node_locations = {lid for lid, loc in self.LOCATIONS.items() if loc.get("node")}
        unused: dict[str, tuple[str, dict]] = {}
        for profile_id, (scenario_id, params) in self.UNUSED_NODE_EXECUTORS.items():
            self.assertNotIn(profile_id, on_node, f"{profile_id} has a scenario now; drop its sample contract")
            self.assertTrue(node_locations & set(registry[profile_id]["allowed_locations"]), profile_id)
            on_node[profile_id] = {
                "scenario": {"id": scenario_id}, "controller": {"runtime": {}},
                "profile_instances": [{"profile_id": profile_id, "parameters": params, "location": {
                    "transport": "ssh", "host": params["host"], "node": params["node"], "resolved": True}}],
            }
            unused[profile_id] = (scenario_id, params)
        guarded = set()
        for profile_id, plan in on_node.items():
            path = self._executor_source(registry[profile_id])
            module = load(path.stem)
            if profile_id in unused:
                scenario_id, params = unused[profile_id]
                with unittest.mock.patch.dict(module.CONTRACTS, {scenario_id: params}):
                    _, stdin = module.build_invocation(plan, "run")
            else:
                _, stdin = module.build_invocation(plan, "run")
            if self._arms(stdin):
                guarded.add(profile_id)
        self.assertEqual(set(on_node) - guarded, self.NO_NODE_UNDO)
        # Executors that reach a node over ssh without a node location must be named, too.
        side = {pid for pid, profile in registry.items()
                if pid not in on_node and "nkia@" in self._executor_source(profile).read_text()}
        self.assertEqual(side, self.NODE_SIDE_ONLY)

    def test_the_arm_check_is_not_fooled_by_comments(self) -> None:
        guard = self.common.GUARD_FUNCTIONS
        self.assertTrue(self._arms(guard + b'node_guard arm "$unit" "$expire_after" "$undo"\n'))
        self.assertFalse(self._arms(guard + b'# node_guard arm "$unit"\n'))
        self.assertFalse(self._arms(b'node_guard arm "$unit" "$expire_after" "$undo"\n'))

if __name__ == "__main__":
    unittest.main()


class ExecutorFailureTraceTests(unittest.TestCase):
    """A failed precondition must say which one (2026-10-22 F35-P "no detail")."""

    common = sys.modules["executor_common"]

    def run_bash(self, body: bytes, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", "-s", "--", *args], input=self.common.with_failure_trace(body),
            capture_output=True, timeout=30,
        )

    def test_a_silent_failing_check_reports_its_line_and_command(self) -> None:
        body = (
            b"#!/usr/bin/env bash\n"
            b"set -euo pipefail\n"
            b'need="$1"\n'
            b"free() { echo 267; }\n"
            b"check() {\n"
            b'  [[ "$(free)" -ge "$need" ]]\n'
            b"}\n"
            b"check\n"
            b"echo unreachable\n"
        )
        result = self.run_bash(body, "1024")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(
            result.stderr.decode().strip(),
            'failed at line 6: [[ "$(free)" -ge "$need" ]]',
        )

    def test_success_and_handled_failures_stay_silent(self) -> None:
        body = (
            b"#!/usr/bin/env bash\n"
            b"set -euo pipefail\n"
            b"probe() { false; }\n"
            b"if probe; then echo yes; else echo no; fi\n"
            b"x=$(false) || x=fallback\n"
            b"grep -q nothing /dev/null || true\n"
            b'echo "$x"\n'
        )
        result = self.run_bash(body)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"no\nfallback\n")
        self.assertEqual(result.stderr, b"")

    def test_value_of_a_variable_is_never_printed(self) -> None:
        body = b'#!/usr/bin/env bash\nset -euo pipefail\nsecret="$1"\n[[ "$secret" == other ]]\n'
        result = self.run_bash(body, "top-secret-value")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(b"top-secret-value", result.stderr)
        self.assertIn(b'[[ "$secret" == other ]]', result.stderr)

    def test_scripts_it_must_not_touch_are_left_byte_for_byte(self) -> None:
        own_trap = b"#!/usr/bin/env bash\nset -euo pipefail\ntrap 'rollback' ERR\nfalse\n"
        no_errexit = b"#!/usr/bin/env bash\nset -u\nfalse\necho on\n"
        not_bash = b"#!/bin/sh\nset -e\nfalse\n"
        python = b"#!/usr/bin/env python3\nraise SystemExit(1)\n"
        for body in (own_trap, no_errexit, not_bash, python, b""):
            self.assertEqual(self.common.with_failure_trace(body), body)

    def test_every_bash_body_still_parses_with_the_trace(self) -> None:
        bodies = 0
        for path in sorted(PROFILES.glob("*_executor.py")):
            module = load(path.stem)
            for name, value in vars(module).items():
                if not isinstance(value, bytes) or not value.startswith(b"#!"):
                    continue
                traced = self.common.with_failure_trace(value)
                if traced == value:
                    continue
                # Only whole bodies: a prefix the builder completes later does
                # not parse on its own, trace or not.
                raw = subprocess.run(["bash", "-n"], input=value, capture_output=True, timeout=30)
                if raw.returncode != 0:
                    continue
                bodies += 1
                with self.subTest(executor=path.stem, body=name):
                    checked = subprocess.run(
                        ["bash", "-n"], input=traced, capture_output=True, timeout=30
                    )
                    self.assertEqual(checked.returncode, 0, checked.stderr.decode())
        self.assertGreater(bodies, 30)


class NetworkLinkExecutorTests(unittest.TestCase):
    """network.fault: delay and loss on one node's pod overlay device (N1). The node side runs against
    a real kernel in user and network namespaces (netns_sandbox); ssh and kubectl are fakes."""

    SCENARIO = "N1-SANDBOX"
    JOB = "link-flannel-1"
    # tb-w1 to banking transfer on tb-w2, the commerce settlement call that crosses nodes on 66.
    PARAMS = {
        "host": "192.168.122.184", "node": "tb-w1", "device": "flannel.1",
        "delay_ms": 300, "jitter_ms": 0, "loss_percent": 0,
        "rules": [{"dst": "10.244.1.0/24", "protocol": "tcp", "dport": 8082}],
    }

    def _plan(self, params: dict, scenario: str | None = None) -> dict:
        return {
            "scenario": {"id": scenario or self.SCENARIO},
            "controller": {"runtime": {"max_injection_duration": "20m"}},
            "profile_instances": [{
                "profile_id": "network.fault", "parameters": params,
                "location": {"transport": "ssh", "host": params["host"], "node": params["node"], "resolved": True},
            }],
        }

    def _sandbox(self, tmp: Path, **extra: str) -> tuple[netns_sandbox.Sandbox, dict[str, str]]:
        bindir = tmp / "bin"
        bindir.mkdir()
        box = netns_sandbox.Sandbox(tmp, bindir)
        self.addCleanup(box.close)
        env = dict(os.environ, **node_guard_fakes.install(tmp), SANDBOX=str(tmp),
                   SCENARIO_PROFILE_STATE_ROOT=str(tmp / "runner-state"),
                   PATH=f"{bindir}:{os.environ['PATH']}", **extra)
        return box, env

    def _act(self, box: netns_sandbox.Sandbox, env: dict[str, str], action: str,
             params: dict | None = None) -> subprocess.CompletedProcess:
        params = params or self.PARAMS
        with unittest.mock.patch.dict(network_fault.CONTRACTS, {self.SCENARIO: params}):
            argv, stdin = network_fault.build_invocation(self._plan(params), action)
        return box.in_node(argv, input=sys.modules["executor_common"].with_failure_trace(stdin), env=env)

    def _fire(self, box: netns_sandbox.Sandbox, env: dict[str, str]) -> subprocess.CompletedProcess:
        """The timer elapses on the node: PID 1 runs its command once (in the node's namespace)."""
        unit = node_guard_fakes.unit_record(env, self.JOB)[0]
        timer = Path(env["FAKE_SYSTEMD"]) / f"{unit}.timer"
        argv = timer.read_text().splitlines()
        timer.unlink()
        return box.in_node(argv, env=env)

    def test_contract_bounds_keep_the_change_on_the_overlay_and_off_monitoring_ports(self) -> None:
        check = network_fault.check_parameters
        self.assertEqual(check(self.PARAMS), ["tcp:dport:8082:10.244.1.0/24:10.244.1.1"])
        sport = dict(self.PARAMS, rules=[{"dst": "10.244.3.0/24", "protocol": "udp", "sport": 53}],
                     host="192.168.122.77", node="tb-cp", delay_ms=0, loss_percent=2.5)
        self.assertEqual(check(sport), ["udp:sport:53:10.244.3.0/24:10.244.3.1"])

        def refused(pattern: str, **change) -> None:
            with self.subTest(change=change):
                with self.assertRaisesRegex(ExecutorError, pattern):
                    check(dict(self.PARAMS, **change))

        rule = self.PARAMS["rules"][0]
        refused("overlay device", device="eth0")
        refused("overlay device", device="ens19")
        refused("same cluster node", host="192.168.122.11")
        refused("same cluster node", node="tb-runner", host="192.168.122.206")
        # Observation and management destinations never sit inside the pod network.
        for dst in ("192.168.122.20/32", "192.168.230.119/32", "192.168.200.66/32", "10.244.0.0/16", "10.96.0.10/32"):
            refused("pod network", rules=[dict(rule, dst=dst)])
        refused("exact IPv4", rules=[dict(rule, dst="10.244.1.5/24")])
        for port in network_fault.PROTECTED_PORTS:
            refused("monitoring or management", rules=[dict(rule, dport=port)])
        refused("between 1 and 32767", rules=[dict(rule, dport=40000)])
        refused("exactly one of dport or sport", rules=[dict(rule, sport=8082)])
        refused("exactly one of dport or sport", rules=[{"dst": "10.244.1.0/24", "protocol": "tcp"}])
        refused("tcp or udp", rules=[dict(rule, protocol="icmp")])
        refused("must not repeat", rules=[rule, rule])
        refused("1 to 8", rules=[])
        refused("delay or loss", delay_ms=0)
        refused("jitter_ms", jitter_ms=301)
        refused("delay_ms", delay_ms=True)
        refused("loss_percent", loss_percent=101)
        refused("loss_percent", loss_percent=0.125)
        with self.assertRaisesRegex(ExecutorError, "parameters must be exactly"):
            check(dict(self.PARAMS, iface="flannel.1"))

    def test_no_scenario_runs_without_an_exact_contract(self) -> None:
        with self.assertRaisesRegex(ExecutorError, "no verified network link contract"):
            network_fault.validate(self.SCENARIO, self.PARAMS, {})
        with self.assertRaisesRegex(ExecutorError, "no verified network link contract"):
            network_fault.build_invocation(self._plan(self.PARAMS), "run")
        with unittest.mock.patch.dict(network_fault.CONTRACTS, {self.SCENARIO: self.PARAMS}):
            network_fault.validate(self.SCENARIO, self.PARAMS, {})
            with self.assertRaisesRegex(ExecutorError, "exactly match"):
                network_fault.validate(self.SCENARIO, dict(self.PARAMS, delay_ms=200), {})
            with self.assertRaisesRegex(ExecutorError, "not enabled"):
                network_fault.validate(self.SCENARIO, self.PARAMS, {"parameter_contract": {"allowed_scenarios": []}})
            moved = self._plan(self.PARAMS)
            moved["profile_instances"][0]["location"]["host"] = "192.168.122.11"
            with self.assertRaisesRegex(ExecutorError, "contract's node"):
                network_fault.build_invocation(moved, "run")
        # The registry keeps the profile plan-only until a scenario brings its contract.
        self.assertFalse(json.loads((ROOT / "registry" / "profiles.json").read_text())["profiles"]["network.fault"]["live_supported"])

    def test_nothing_on_the_node_command_line_or_in_the_node_program_names_the_change(self) -> None:
        with unittest.mock.patch.dict(network_fault.CONTRACTS, {self.SCENARIO: self.PARAMS}):
            argv, stdin = network_fault.build_invocation(self._plan(self.PARAMS), "run")
        self.assertEqual(argv[:3], ["/usr/bin/bash", "-s", "--"])
        self.assertNotIn(self.SCENARIO, " ".join(argv))
        self.assertNotIn(self.SCENARIO.encode(), stdin)
        # cls_u32 prints "u32 classifier" when it loads; flower loads silently.
        self.assertNotIn(b" u32 ", stdin)
        self.assertIn(b"flower", stdin)
        # sch_netem prints its version when it loads, so the node refuses unless it is loaded already.
        self.assertIn(b'"$module_dir/sch_netem"', stdin)
        self.assertNotIn(b"modprobe", stdin)

    def test_only_the_contract_traffic_is_delayed_and_cleanup_restores_the_device(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            box, env = self._sandbox(tmp)
            box.listen(8082, 4222, 9000)
            self.assertTrue(box.root_qdisc().startswith("qdisc noqueue 0: root"))
            done = self._act(box, env, "preflight")
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(node_guard_fakes.timers(env), [])
            done = self._act(box, env, "run")
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertTrue(box.root_qdisc().startswith("qdisc prio 1a7e: root"))
            # The rule's port to the other node's pods waits 300 ms; KCM's port, another port and the
            # node's own KCM pod do not.
            self.assertGreaterEqual(box.connect_ms(8082), 290)
            # Traffic masqueraded to the node's overlay address (NodePort entries) stays subject to the
            # rules; only its DNS lookups pass.
            self.assertGreaterEqual(box.connect_ms(8082, src=netns_sandbox.NODE_IP), 290)
            self.assertLess(box.connect_ms(4222), 100)
            self.assertLess(box.connect_ms(9000), 100)
            self.assertLess(box.connect_ms(8082, src=netns_sandbox.KCM_POD_IP), 100)
            # Armed before the change: 20m + 600s, a neutral unit, and a boot rule with only the guard's
            # own files (a qdisc ends with a reboot).
            self.assertEqual(node_guard_fakes.armed_seconds(env, self.JOB), [1800])
            self.assertEqual(node_guard_fakes.journal_words(env, self.JOB), [])
            rule = Path(node_guard_fakes.unit_record(env, self.JOB)[2])
            self.assertEqual(rule.read_text().splitlines(), [f"r! {tmp / 'state' / (self.JOB + '.undo')}",
                                                              f"r! {tmp / 'state' / (self.JOB + '.unit')}",
                                                              f"r! {rule}"])
            done = self._act(box, env, "cleanup")
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertNotIn(b"by itself", done.stderr)
            self.assertTrue(box.root_qdisc().startswith("qdisc noqueue 0: root"))
            self.assertLess(box.connect_ms(8082), 100)
            self.assertEqual(node_guard_fakes.timers(env), [])
            self.assertEqual(sorted(x.name for x in (tmp / "state").iterdir()), [])
            self.assertEqual(list((tmp / "runner-state").iterdir()), [])
            self.assertEqual(self._act(box, env, "recovery").returncode, 0)
            # A second cleanup has nothing to do.
            again = self._act(box, env, "cleanup")
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertEqual(node_guard_fakes.armed_seconds(env, self.JOB), [1800])
            # The node saw only `bash -s` command lines: no tc, no device, no port, no number.
            self.assertEqual(sorted(set((tmp / "ssh-log").read_text().splitlines())), ["bash -s", "sudo -n bash -s"])

    def test_the_node_removes_the_delay_by_itself_when_cleanup_never_comes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            box, env = self._sandbox(tmp)
            box.listen(8082)
            self.assertEqual(self._act(box, env, "run").returncode, 0)
            undo = tmp / "state" / f"{self.JOB}.undo"
            spare = tmp / "undo-copy"
            spare.write_bytes(undo.read_bytes())
            body = undo.read_text()
            self.assertIn("tc qdisc del dev flannel.1 root", body)
            for forbidden in ("eth0", "ens19", "8082", "netem", "filter", self.SCENARIO):
                self.assertNotIn(forbidden, body)
            # The runner loses the node: cleanup fails and the delay stays.
            (tmp / "down").touch()
            failed = self._act(box, env, "cleanup")
            self.assertNotEqual(failed.returncode, 0)
            self.assertTrue(box.root_qdisc().startswith("qdisc prio 1a7e: root"))
            self.assertGreaterEqual(box.connect_ms(8082), 290)
            # The deadline passes: the node deletes its own root qdisc and leaves only the time.
            fired = self._fire(box, env)
            self.assertEqual(fired.returncode, 0, fired.stderr)
            self.assertTrue(box.root_qdisc().startswith("qdisc noqueue 0: root"))
            self.assertLess(box.connect_ms(8082), 100)
            self.assertEqual(sorted(x.name for x in (tmp / "state").iterdir()), [f"{self.JOB}.expired"])
            for _ in range(2):
                rerun = box.in_node(["/bin/bash", str(spare)], env=env)
                self.assertEqual(rerun.returncode, 0, rerun.stderr)
                self.assertTrue(box.root_qdisc().startswith("qdisc noqueue 0: root"))
            # The node comes back: the late cleanup finds nothing and says when the node undid it.
            (tmp / "down").unlink()
            late = self._act(box, env, "cleanup")
            self.assertEqual(late.returncode, 0, late.stderr)
            self.assertIn(b"the node undid the change by itself at ", late.stderr)
            self.assertEqual(sorted(x.name for x in (tmp / "state").iterdir()), [])
            self.assertEqual(self._act(box, env, "recovery").returncode, 0)

    def test_monitoring_ports_pass_even_where_a_rule_would_match(self) -> None:
        # A rule on a source port also matches any connection that happens to leave from that port.
        # Packets to or from a protected port (here KCM's 4222) pass ahead of every rule.
        params = dict(self.PARAMS, rules=[{"dst": "10.244.1.0/24", "protocol": "tcp", "sport": 9100}])
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            box, env = self._sandbox(tmp)
            box.listen(4222, 9000)
            self.assertEqual(self._act(box, env, "run", params).returncode, 0)
            self.assertGreaterEqual(box.connect_ms(9000, sport=9100), 290)
            self.assertLess(box.connect_ms(4222, sport=9100), 100)
            self.assertEqual(self._act(box, env, "cleanup", params).returncode, 0)

    def test_name_lookups_of_host_network_agents_pass_a_dns_rule(self) -> None:
        # N1 review 2026-11-10: the kcm-monitoring node agent runs on the host network with
        # ClusterFirstWithHostNet, so its lookups of the headless master service leave the node from
        # the node's overlay address (kube-proxy masquerade), not from a pod. A DNS rule delayed them.
        params = dict(self.PARAMS, rules=[{"dst": "10.244.1.0/24", "protocol": "tcp", "dport": 53}])
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            box, env = self._sandbox(tmp)
            box.listen(53)
            done = self._act(box, env, "run", params)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertLess(box.connect_ms(53, src=netns_sandbox.NODE_IP), 100)
            self.assertLess(box.connect_ms(53, src=netns_sandbox.KCM_POD_IP), 100)
            # An application pod's lookup is the change itself.
            self.assertGreaterEqual(box.connect_ms(53, src=netns_sandbox.APP_POD_IP), 290)
            self.assertEqual(self._act(box, env, "cleanup", params).returncode, 0)

    def test_replies_to_monitoring_agents_on_other_nodes_pass_a_reply_rule(self) -> None:
        # N1 review 2026-11-10: a rule on a source port (a DNS answer on the CoreDNS node) also
        # delayed the answers to KCM agents on the other nodes, both pods and host-network agents.
        params = dict(self.PARAMS, rules=[{"dst": "10.244.1.0/24", "protocol": "tcp", "sport": 53}])
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            box, env = self._sandbox(tmp)
            box.listen(53, on_node=True)
            done = self._act(box, env, "run", params)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertLess(box.connect_from_peer_ms(53, netns_sandbox.PEER_KCM_IP), 100)
            self.assertLess(box.connect_from_peer_ms(53, netns_sandbox.PEER_NODE_IP), 100)
            # Answers to an application pod on that node are the change itself.
            self.assertGreaterEqual(box.connect_from_peer_ms(53, netns_sandbox.PEER_IP), 290)
            self.assertEqual(self._act(box, env, "cleanup", params).returncode, 0)

    def test_a_node_without_its_overlay_address_is_refused_before_anything_is_armed(self) -> None:
        # N1 re-review 2026-11-10: without the address the host-network lookups' pass-through would
        # silently drop out, so the run refuses instead (fail closed).
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            box, env = self._sandbox(tmp)
            box.in_node(["ip", "addr", "del", f"{netns_sandbox.NODE_IP}/32", "dev", "flannel.1"], check=True)
            for action in ("preflight", "run"):
                done = self._act(box, env, action)
                self.assertNotEqual(done.returncode, 0, action)
                self.assertIn(b"no IPv4 address on flannel.1", done.stderr)
            self.assertEqual(node_guard_fakes.timers(env), [])
            self.assertTrue(box.root_qdisc().startswith("qdisc noqueue 0: root"))
            self.assertEqual(self._act(box, env, "cleanup").returncode, 0)

    def test_a_qdisc_that_is_not_ours_is_neither_replaced_nor_removed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            box, env = self._sandbox(tmp)
            box.in_node(["tc", "qdisc", "add", "dev", "flannel.1", "root", "handle", "1:", "prio"], check=True)
            for action in ("preflight", "run"):
                done = self._act(box, env, action)
                self.assertNotEqual(done.returncode, 0, action)
                self.assertIn(b"root qdisc is foreign", done.stderr)
            self.assertEqual(node_guard_fakes.timers(env), [])
            done = self._act(box, env, "cleanup")
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertTrue(box.root_qdisc().startswith("qdisc prio 1: root"))
            self.assertNotEqual(self._act(box, env, "recovery").returncode, 0)

    def test_the_run_refuses_before_arming_when_the_place_is_not_as_the_contract_says(self) -> None:
        own_pods = dict(self.PARAMS, rules=[{"dst": "10.244.3.0/24", "protocol": "tcp", "dport": 8082}])
        unrouted = dict(self.PARAMS, rules=[{"dst": "10.244.2.0/24", "protocol": "tcp", "dport": 8082}])
        cases = [
            ("sch_netem is not loaded", self.PARAMS, {"LINK_MODULE_DIR": "EMPTY"}),
            ("monitoring agent port", self.PARAMS, {"FAKE_KCM_PORTS": "8082"}),
            ("not a Ready node", self.PARAMS, {"FAKE_NODE_READY": "False"}),
            ("not inside another node's pod network", own_pods, {}),
            ("does not leave through flannel.1", unrouted, {}),
        ]
        for message, params, extra in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as raw:
                tmp = Path(raw)
                if extra.get("LINK_MODULE_DIR") == "EMPTY":
                    (tmp / "modules").mkdir()
                    extra = {"LINK_MODULE_DIR": str(tmp / "modules")}
                box, env = self._sandbox(tmp, **extra)
                done = self._act(box, env, "run", params)
                self.assertNotEqual(done.returncode, 0)
                self.assertIn(message.encode(), done.stderr)
                self.assertEqual(node_guard_fakes.timers(env), [])
                self.assertTrue(box.root_qdisc().startswith("qdisc noqueue 0: root"))
                # A run that failed on the node left its state file; cleanup clears it without touching the node.
                self.assertEqual(self._act(box, env, "cleanup", params).returncode, 0)
                self.assertFalse(any((tmp / "runner-state").glob("*")))

    def test_loss_is_a_queue_drop_that_no_interface_counter_shows(self) -> None:
        # What 119 can see (design principle 4): the agents read interface drop and error counters,
        # and netem's losses are not among them. Only the queue's own statistics count them.
        params = dict(self.PARAMS, delay_ms=0, loss_percent=50,
                      rules=[{"dst": "10.244.1.0/24", "protocol": "udp", "dport": 9999}])
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            box, env = self._sandbox(tmp)
            self.assertEqual(self._act(box, env, "run", params).returncode, 0)
            send = ("import socket\ns = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
                    f"for _ in range(2000): s.sendto(b'x' * 100, ('{netns_sandbox.PEER_IP}', 9999))\n")
            box.in_node(["python3", "-c", send], check=True)
            stats = box.in_node(["tc", "-s", "qdisc", "show", "dev", "flannel.1"], check=True).stdout.decode()
            dropped = int(re.search(r"qdisc netem 1a7f:.*?\(dropped (\d+),", stats, re.S).group(1))
            self.assertGreater(dropped, 500)
            line = next(x for x in box.in_node(["cat", "/proc/net/dev"], check=True).stdout.decode().splitlines()
                        if x.strip().startswith("flannel.1:"))
            fields = [int(x) for x in line.split(":", 1)[1].split()]
            # rx errs, rx drop, tx errs, tx drop
            self.assertEqual((fields[2], fields[3], fields[10], fields[11]), (0, 0, 0, 0))
            self.assertLess(fields[9], 1600)
            self.assertEqual(self._act(box, env, "cleanup", params).returncode, 0)
