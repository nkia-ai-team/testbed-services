"""PostgreSQL 락 주입 계약 (F01-R row / F06-H table, 정적).

라이브 주입 없이 db_lock_executor의 PostgreSQL 경로를 검증한다. 2026-07-27
품질 기준서(G6 정답 무누설, `docs/spec-scenario-quality-charter.md` 부록 A)에 따라
주입 세션은 **실제 앱 세션과 구별되지 않아야** 하므로, 아래 세 축이 회귀하지
않는지를 계약으로 고정한다.

    신원  : application_name에 시나리오를 인코딩하지 않고 실 앱과 동일해야 한다(L1)
    유지  : pg_sleep이 아니라 idle in transaction으로 잡아야 한다(L2)
    출처  : 클러스터 내부 파드에서 접속해야 한다(L2 — 외부 접속은 NAT 주소로 튄다. v4 C6b 사용자 결정 Q8 로 다시
            확인: 러너에서 NodePort 로 붙으면 DPM 관제 세션과 같은 주소가 된다)

정리는 세션 이름이 아니라 **전용 클라이언트 파드 삭제**로 한다. 이름 기반
pg_terminate_backend는 실 서비스 세션을 죽일 수 있어 금지한다. v4 C6b: 파드 안 프로세스는 psql
하나이고(옛 sh, sleep 600, SQL 이 든 명령줄 없음) 러너가 attach 로 문장을 보내며 보유 시간을 센다.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / "profiles"
sys.path.insert(0, str(PROFILES))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import db_operator_fakes  # noqa: E402


def _load(name: str):
    path = PROFILES / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


db_lock = _load("db_lock_executor")
pg_lock_remote = _load("pg_lock_remote")
own_body = _load("db_operator_remote").without_library
compiler_spec = importlib.util.spec_from_file_location("f06h_compile", ROOT / "compile-plan.py")
assert compiler_spec and compiler_spec.loader
compiler = importlib.util.module_from_spec(compiler_spec)
compiler_spec.loader.exec_module(compiler)


class PostgresLockContractTests(unittest.TestCase):
    def _plan(self, scenario_id: str) -> dict:
        return {
            "scenario": {"id": scenario_id},
            "profile_instances": [
                {"profile_id": "db.lock", "parameters": db_lock.CONTRACTS[scenario_id]}
            ],
        }

    # --- 표면 계약 ---------------------------------------------------------

    def test_f06h_contract_is_table_exclusive_lock_on_payments(self) -> None:
        contract = db_lock.CONTRACTS["F06-H"]
        self.assertEqual(contract["engine"], "postgresql")
        self.assertEqual(contract["schema"], "payment_schema")
        self.assertEqual(contract["table"], "payments")
        self.assertEqual(contract["lock_scope"], "table")
        self.assertEqual(contract["lock_mode"], "EXCLUSIVE")
        # payment는 결제마다 신규 INSERT라 특정 row로는 막을 수 없다.
        self.assertEqual(contract["key_column"], "")
        self.assertEqual(contract["key_value"], "")

    def test_f01r_contract_is_row_lock_on_inventory(self) -> None:
        contract = db_lock.CONTRACTS["F01-R"]
        self.assertEqual(contract["engine"], "postgresql")
        self.assertEqual(contract["schema"], "inventory_schema")
        self.assertEqual(contract["table"], "inventory")
        self.assertEqual(contract["lock_scope"], "row")
        self.assertEqual(contract["key_column"], "product_id")

    def test_validate_exact_contract_match(self) -> None:
        db_lock.validate("F06-H", db_lock.CONTRACTS["F06-H"], {})
        tampered = dict(db_lock.CONTRACTS["F06-H"])
        tampered["lock_mode"] = "ACCESS EXCLUSIVE"
        with self.assertRaisesRegex(db_lock.ExecutorError, "verified lock contract"):
            db_lock.validate("F06-H", tampered, {})

    # --- G6/L1: 신원에 정답을 인코딩하지 않는다 ---------------------------

    def test_client_identity_impersonates_the_real_application(self) -> None:
        for scenario_id in ("F01-R", "F06-H"):
            contract = db_lock.CONTRACTS[scenario_id]
            self.assertEqual(contract["client_identity"], db_lock.APP_IDENTITY)
            # 세션 이름 어디에도 시나리오 ID가 남아서는 안 된다.
            self.assertNotIn(scenario_id, contract["client_identity"])
            self.assertNotIn("rca-", contract["client_identity"])

    def test_scenario_encoded_identity_is_rejected(self) -> None:
        profile = {
            "parameter_contract": {"allowed_scenarios": ["F0X-T"]},
            "scenario_parameters": {"F0X-T": {}},
        }
        params = dict(db_lock.CONTRACTS["F01-R"])
        params["client_identity"] = "rca-F0X-T-payment-lock"
        profile["scenario_parameters"]["F0X-T"] = params
        with self.assertRaisesRegex(db_lock.ExecutorError, "impersonate the real application"):
            db_lock.validate("F0X-T", params, profile)

    def test_no_scenario_tag_reaches_the_injection_argv(self) -> None:
        for scenario_id in ("F01-R", "F06-H"):
            argv, _ = db_lock.build_invocation(self._plan(scenario_id), "run")
            joined = " ".join(argv)
            self.assertNotIn("rca-F", joined)
            self.assertIn(db_lock.APP_IDENTITY, argv)

    # --- G6/L2: 주입 서명을 남기지 않는다 ---------------------------------

    def test_lock_is_held_idle_in_transaction_not_by_pg_sleep(self) -> None:
        script = db_lock.POSTGRES_CLIENT.decode()
        # 서버측 수면은 waitEvent=PgSleep이라는 유일값을 남긴다 — 금지.
        self.assertNotIn("pg_sleep", script)
        # 클라이언트가 트랜잭션을 열어둔 채 대기해야 idle in transaction이 된다. 보유 시간은 러너의 구동기가 센다.
        self.assertIn("pgl_send \"$dir\" 'BEGIN;' 'SELECT pg_backend_pid();' \"$sql\" '\\echo locked'", script)
        self.assertIn("end=$((SECONDS + DEADLINE))", pg_lock_remote.DRIVER_SCRIPT)
        self.assertIn("'ROLLBACK;' '\\q'", pg_lock_remote.DRIVER_SCRIPT)
        self.assertIn("state = 'idle in transaction'", script)
        # 잠금을 얻었다는 표시는 psql 의 \\echo 라 서버로 가지 않는다(세션의 마지막 질의는 잠금 문장 그대로).
        self.assertNotIn("SELECT 'locked'", script)

    def test_injection_originates_inside_the_cluster(self) -> None:
        script = db_lock.POSTGRES_CLIENT.decode()
        self.assertIn("kind: Pod", script)
        # 클러스터 밖 tb-runner ssh 경로가 되살아나면 출처 IP가 NAT로 튄다.
        self.assertNotIn("/usr/bin/ssh", script)
        # 파드 안 프로세스는 psql 하나다. 정의에 SQL, sleep, 셸이 없다.
        pod = script[script.index("pgl_pod() {"):script.index("pgl_name() {")]
        self.assertIn('command: ["psql", ${args}]', pod)
        self.assertIn("stdinOnce: true", pod)
        self.assertIn("activeDeadlineSeconds:", pod)
        for trace in ("sh -c", "sleep", "SQL_PRE", "SQL_POST", "HOLD"):
            self.assertNotIn(trace, pod)
        # DB 파드에는 exec 하지 않는다(확인 조회는 운영자 세션).
        self.assertNotIn(" exec ", own_body(script))
        for scenario_id in ("F01-R", "F06-H"):
            argv, _ = db_lock.build_invocation(self._plan(scenario_id), "run")
            self.assertEqual(argv[0], "/usr/bin/bash")

    def test_external_access_mode_is_refused(self) -> None:
        for access in ("tb-runner", "runner-network"):
            params = dict(db_lock.CONTRACTS["F01-R"])
            params["access"] = access
            profile = {
                "parameter_contract": {"allowed_scenarios": ["F0X-T"]},
                "scenario_parameters": {"F0X-T": params},
            }
            with self.assertRaisesRegex(db_lock.ExecutorError, "inside the cluster"):
                db_lock.validate("F0X-T", params, profile)

    # --- 정리 안전성 -------------------------------------------------------

    def test_cleanup_deletes_the_client_pod_not_sessions_by_name(self) -> None:
        script = db_lock.POSTGRES_CLIENT.decode()
        # 신원이 앱과 같아졌으므로 이름 기반 종료는 실 서비스를 죽일 수 있다.
        self.assertNotIn("pg_terminate_backend", script)
        self.assertNotIn("pkill", script)
        self.assertIn("dbo_k delete pod", script)
        self.assertIn("lucida.io/db-client=session", script)
        release = script[script.index("pgl_release() {"):]
        release = release[:release.index("\n}\n")]
        # 러너의 구동기(롤백), 파드 삭제, 서버에서 그 backend(pid 와 시작 시각)가 사라졌는지 순서.
        self.assertLess(release.index("pgl_stop"), release.index("pgl_delete_pods"))
        self.assertLess(release.index("pgl_delete_pods"), release.index("pgl_released"))
        self.assertIn("backend_start = '$bstart'::timestamptz", script)

    # --- SQL 주입 표면 -----------------------------------------------------

    def test_lock_mode_allowlist_rejects_arbitrary_sql(self) -> None:
        script = db_lock.POSTGRES_CLIENT.decode()
        self.assertIn('case "$4" in EXCLUSIVE|"ACCESS EXCLUSIVE"|"SHARE ROW EXCLUSIVE")', script)
        params = dict(db_lock.CONTRACTS["F06-H"])
        params["lock_mode"] = "; DROP TABLE payments; --"
        profile = {
            "parameter_contract": {"allowed_scenarios": ["F0X-T"]},
            "scenario_parameters": {"F0X-T": params},
        }
        with self.assertRaisesRegex(db_lock.ExecutorError, "unsupported table lock mode"):
            db_lock.validate("F0X-T", params, profile)

    def test_row_scope_requires_a_key(self) -> None:
        params = dict(db_lock.CONTRACTS["F01-R"])
        params["key_value"] = ""
        profile = {
            "parameter_contract": {"allowed_scenarios": ["F0X-T"]},
            "scenario_parameters": {"F0X-T": params},
        }
        with self.assertRaisesRegex(db_lock.ExecutorError, "key_column and key_value"):
            db_lock.validate("F0X-T", params, profile)

    # --- 레지스트리 정합 ---------------------------------------------------

    def test_f01r_registry_plan_matches_the_executor_contract(self) -> None:
        plan = compiler.compile_plan("f01-r-pg-lock-checkout")
        instance = next(r for r in plan["profile_instances"] if r["profile_id"] == "db.lock")
        profiles = compiler.load_contracts()[2]["profiles"]
        db_lock.validate("F01-R", instance["parameters"], profiles["db.lock"])
        argv, script = db_lock.build_invocation(plan, "run")
        self.assertIn("FOR UPDATE", script.decode())
        self.assertIn(db_lock.APP_IDENTITY, argv)

    # --- 세션 흐름(가짜 kubectl, 가짜 PostgreSQL) -------------------------

    def _env(self, tmp: Path) -> dict[str, str]:
        return db_operator_fakes.install(tmp, psql=db_operator_fakes.pg_server(tmp))

    def _run(self, scenario_id: str, action: str, env: dict[str, str]) -> subprocess.CompletedProcess:
        argv, stdin = db_lock.build_invocation(self._plan(scenario_id), action)
        return subprocess.run(argv, input=stdin, env=env, capture_output=True, timeout=120, check=False)

    def test_backend_pid_is_read_from_the_session_and_the_lock_is_held_and_released(self) -> None:
        """psql 은 -q 라 BEGIN 태그를 찍지 않고, 첫 숫자 줄이 pid, 행 잠금이면 그다음이 키다(2026-07-31 F01-R
        "did not report a backend pid" 의 재발 방지). 클라이언트 파드는 계약의 이미지, 서비스, 비밀과 앱 신원으로
        만들어지고, 정리는 그 파드를 지운다."""
        for scenario_id in ("F01-R", "F06-H"):
            with self.subTest(scenario_id), tempfile.TemporaryDirectory() as raw:
                tmp = Path(raw)
                env = self._env(tmp)
                self.assertEqual(self._run(scenario_id, "preflight", env).returncode, 0)
                run = self._run(scenario_id, "run", env)
                self.assertEqual(run.returncode, 0, run.stderr.decode())
                sessions = db_operator_fakes.pg_sessions(tmp)
                self.assertEqual(len(sessions), 1)
                state = tmp / "state" / f"db-lock-postgres-{scenario_id}"
                self.assertEqual((state / "backend").read_text().split()[0], sessions[0])
                pod = (tmp / "applied.yaml").read_text()
                contract = db_lock.CONTRACTS[scenario_id]
                for line in (f"image: {contract['image']}", 'value: "PostgreSQL JDBC Driver"',
                             f'value: "{contract["service"]}"', f"name: {contract['secret']}, key: POSTGRES_PASSWORD",
                             'command: ["psql", "-X", "-q", "-At"]',
                             f"activeDeadlineSeconds: {contract['hold_seconds'] + pg_lock_remote.DEADLINE_MARGIN_SECONDS}"):
                    self.assertIn(line, pod)
                self.assertNotIn("SELECT", pod)
                expected = ("SELECT product_id FROM inventory_schema.inventory WHERE product_id='1' FOR UPDATE;"
                            if scenario_id == "F01-R" else "LOCK TABLE payment_schema.payments IN EXCLUSIVE MODE;")
                self.assertEqual((tmp / "pg-lock-statement").read_text(), expected)
                # 잠근 동안에는 다시 시작하지 않는다.
                self.assertNotEqual(self._run(scenario_id, "preflight", env).returncode, 0)
                cleanup = self._run(scenario_id, "cleanup", env)
                self.assertEqual(cleanup.returncode, 0, cleanup.stderr.decode())
                self.assertEqual(db_operator_fakes.pg_sessions(tmp), [])
                self.assertEqual(list((tmp / "pods").iterdir()), [])
                self.assertFalse(state.exists())
                self.assertEqual(self._run(scenario_id, "recovery", env).returncode, 0)
                self.assertEqual(self._run(scenario_id, "cleanup", env).returncode, 0)
                calls = (tmp / "kubectl.log").read_text()
                self.assertNotIn("testbed-postgres-0 --", calls)  # never an exec into the database pod
                db_operator_fakes.assert_no_password(self, calls, run.stderr, cleanup.stderr)

    def test_cleanup_stops_the_driver_even_when_the_api_is_down(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = self._env(tmp)
            self.assertEqual(self._run("F01-R", "run", env).returncode, 0)
            (tmp / "kubectl-down").write_text("")
            cleanup = self._run("F01-R", "cleanup", env)
            self.assertNotEqual(cleanup.returncode, 0)
            # The driver rolled back and quit psql whatever the API said.
            self.assertEqual(db_operator_fakes.pg_sessions(tmp), [])
            self.assertTrue((tmp / "state" / "db-lock-postgres-F01-R" / "backend").exists(),
                            "the record stays until the pod is gone and the server side is confirmed")
            (tmp / "kubectl-down").unlink()
            self.assertEqual(self._run("F01-R", "cleanup", env).returncode, 0)
            self.assertEqual(self._run("F01-R", "recovery", env).returncode, 0)

    def test_a_lock_that_cannot_be_taken_fails_the_run_and_leaves_no_pod(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = self._env(tmp)
            (tmp / "pg-held-by-other").write_text("")
            run = self._run("F01-R", "run", env)
            self.assertNotEqual(run.returncode, 0)
            self.assertIn(b"did not report the lock", run.stderr)
            self.assertEqual(db_operator_fakes.pg_sessions(tmp), [])
            self.assertEqual(list((tmp / "pods").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
