"""PostgreSQL 락 주입 계약 (F01-R row / F06-H table, 정적).

라이브 주입 없이 db_lock_executor의 PostgreSQL 경로를 검증한다. 2026-07-27
품질 기준서(G6 정답 무누설, `docs/spec-scenario-quality-charter.md` 부록 A)에 따라
주입 세션은 **실제 앱 세션과 구별되지 않아야** 하므로, 아래 세 축이 회귀하지
않는지를 계약으로 고정한다.

    신원  : application_name에 시나리오를 인코딩하지 않고 실 앱과 동일해야 한다(L1)
    유지  : pg_sleep이 아니라 idle in transaction으로 잡아야 한다(L2)
    출처  : 클러스터 내부 파드에서 접속해야 한다(L2 — 외부 접속은 NAT 주소로 튄다)

정리는 세션 이름이 아니라 **전용 클라이언트 파드 삭제**로 한다. 이름 기반
pg_terminate_backend는 실 서비스 세션을 죽일 수 있어 금지한다.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / "profiles"
sys.path.insert(0, str(PROFILES))


def _load(name: str):
    path = PROFILES / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


db_lock = _load("db_lock_executor")
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
        script = db_lock.POSTGRES_CLIENT_POD.decode()
        # 서버측 수면은 waitEvent=PgSleep이라는 유일값을 남긴다 — 금지.
        self.assertNotIn("pg_sleep", script)
        # 클라이언트가 트랜잭션을 열어둔 채 대기해야 idle in transaction이 된다.
        self.assertIn("BEGIN;", script)
        self.assertIn('sleep "\\$HOLD"', script)
        self.assertIn("idle in transaction", script)

    def test_injection_originates_inside_the_cluster(self) -> None:
        script = db_lock.POSTGRES_CLIENT_POD.decode()
        self.assertIn("kind: Pod", script)
        # 클러스터 밖 tb-runner ssh 경로가 되살아나면 출처 IP가 NAT로 튄다.
        self.assertNotIn("/usr/bin/ssh", script)
        for scenario_id in ("F01-R", "F06-H"):
            argv, _ = db_lock.build_invocation(self._plan(scenario_id), "run")
            self.assertEqual(argv[0], "/usr/bin/bash")

    def test_external_access_mode_is_refused(self) -> None:
        params = dict(db_lock.CONTRACTS["F01-R"])
        params["access"] = "tb-runner"
        profile = {
            "parameter_contract": {"allowed_scenarios": ["F0X-T"]},
            "scenario_parameters": {"F0X-T": params},
        }
        with self.assertRaisesRegex(db_lock.ExecutorError, "inside the cluster"):
            db_lock.validate("F0X-T", params, profile)

    # --- 정리 안전성 -------------------------------------------------------

    def test_cleanup_deletes_the_client_pod_not_sessions_by_name(self) -> None:
        script = db_lock.POSTGRES_CLIENT_POD.decode()
        # 신원이 앱과 같아졌으므로 이름 기반 종료는 실 서비스를 죽일 수 있다.
        self.assertNotIn("pg_terminate_backend", script)
        self.assertNotIn("pkill", script)
        self.assertIn("delete pod", script)
        self.assertIn("lucida.io/db-client=session", script)

    # --- SQL 주입 표면 -----------------------------------------------------

    def test_lock_mode_allowlist_rejects_arbitrary_sql(self) -> None:
        script = db_lock.POSTGRES_CLIENT_POD.decode()
        self.assertIn('case "$mode" in EXCLUSIVE|"ACCESS EXCLUSIVE"|"SHARE ROW EXCLUSIVE")', script)
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

    # --- 백엔드 pid 추출 ---------------------------------------------------

    def test_backend_pid_is_read_from_real_psql_output(self) -> None:
        """주입 세션의 pid를 실제 psql 로그 모양에서 뽑아낼 수 있어야 한다.

        psql은 결과보다 `BEGIN` 명령 태그를 먼저 찍는다. 첫 줄만 보던 구현은
        그래서 한 번도 pid를 찾지 못했고, PostgreSQL db.lock 주입이 전부
        "did not report a backend pid"로 죽었다(F01-R, 2026-07-31).
        아래 입력은 그날 파드에서 실측한 로그 그대로다.
        """
        script = db_lock.POSTGRES_CLIENT_POD.decode()
        pipeline = next(
            line.split('logs "$pod" 2>/dev/null |', 1)[1].rstrip(')"').strip()
            for line in script.splitlines()
            if 'backend_pid="$(' in line and "logs" in line
        )
        observed_pod_log = "BEGIN\n35250\n1\n"
        extracted = subprocess.run(
            ["sh", "-c", f"cat | {pipeline}"],
            input=observed_pod_log,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(extracted, "35250")


if __name__ == "__main__":
    unittest.main()
