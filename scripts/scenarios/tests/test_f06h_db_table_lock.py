"""F06-H payment DB table-lock executor contract (표면 상세화, 정적).

라이브 주입 없이 db_lock_executor의 F06-H 표면(payments 테이블 EXCLUSIVE 잠금)과
application_name 접미사 일반화만 검증한다. 공유 registry(catalog/controllers/
profiles) 패치 없이 실행되도록 compile_plan은 기존 live 시나리오(F01-R)에만 쓴다.
"""
from __future__ import annotations

import importlib.util
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


class F06HDbTableLockTests(unittest.TestCase):
    def _f06h_plan(self) -> dict:
        return {
            "scenario": {"id": "F06-H"},
            "profile_instances": [
                {"profile_id": "db.lock", "parameters": db_lock.CONTRACTS["F06-H"]}
            ],
        }

    def test_contract_is_table_exclusive_lock_on_payments(self) -> None:
        contract = db_lock.CONTRACTS["F06-H"]
        self.assertEqual(contract["engine"], "postgresql")
        self.assertEqual(contract["schema"], "payment_schema")
        self.assertEqual(contract["table"], "payments")
        self.assertEqual(contract["lock_scope"], "table")
        self.assertEqual(contract["lock_mode"], "EXCLUSIVE")
        self.assertEqual(contract["application_name"], "rca-F06-H-payment-lock")
        # row-lock 잔재가 남으면 안 된다 — payment는 신규 INSERT라 특정 row로는 못 막는다.
        self.assertNotIn("key_column", contract)
        self.assertNotIn("key_value", contract)

    def test_validate_exact_contract_match(self) -> None:
        db_lock.validate("F06-H", db_lock.CONTRACTS["F06-H"], {})
        tampered = dict(db_lock.CONTRACTS["F06-H"])
        tampered["lock_mode"] = "ACCESS EXCLUSIVE"
        with self.assertRaisesRegex(db_lock.ExecutorError, "verified lock contract"):
            db_lock.validate("F06-H", tampered, {})

    def test_build_invocation_locks_table_and_binds_tag(self) -> None:
        argv, stdin = db_lock.build_invocation(self._f06h_plan(), "run")
        self.assertEqual(argv[0], "/usr/bin/bash")
        self.assertIn("rca-F06-H-payment-lock", argv)
        self.assertIn("EXCLUSIVE", argv)
        script = stdin.decode()
        self.assertIn("LOCK TABLE $SCHEMA.$TABLE IN $MODE MODE", script)
        self.assertIn("PGAPPNAME=\"$TAG\"", script)
        self.assertIn("pg_terminate_backend", script)
        self.assertIn("to_regclass", script)
        # ACCESS SHARE(평문 SELECT)를 막는 잔재/행잠금 잔재 금지 — pod readiness 보호.
        self.assertNotIn("FOR UPDATE", script)
        self.assertNotIn("pkill", script)

    def test_lock_mode_allowlist_rejects_arbitrary_sql(self) -> None:
        # 잠금 모드는 승인 목록만 원격 스크립트에서 통과한다(SQL 주입 표면 차단).
        script = db_lock.POSTGRES_POD_REMOTE.decode()
        self.assertIn('case "$mode" in EXCLUSIVE|"ACCESS EXCLUSIVE"|"SHARE ROW EXCLUSIVE")', script)

    def test_application_name_suffix_is_generalized_not_hardcoded_inventory(self) -> None:
        profile = {
            "parameter_contract": {"allowed_scenarios": ["F0X-T"]},
            "scenario_parameters": {"F0X-T": {"application_name": "rca-F0X-T-payment-lock"}},
        }
        # payment-lock 접미사가 통과해야 한다(과거엔 -inventory-lock 하드코딩으로 거절됐다).
        db_lock.validate("F0X-T", {"application_name": "rca-F0X-T-payment-lock"}, profile)
        # 여전히 scenario id 결속 + -lock 접미사는 강제한다.
        bad_profile = {
            "parameter_contract": {"allowed_scenarios": ["F0X-T"]},
            "scenario_parameters": {"F0X-T": {"application_name": "rca-F0X-T-payment"}},
        }
        with self.assertRaisesRegex(db_lock.ExecutorError, "does not bind the scenario"):
            db_lock.validate("F0X-T", {"application_name": "rca-F0X-T-payment"}, bad_profile)

    def test_f01r_inventory_lock_contract_still_passes(self) -> None:
        # 기존 F01-R(-inventory-lock, ssh tb-runner row-lock) 계약 회귀 방지.
        plan = compiler.compile_plan("f01-r-pg-lock-checkout")
        instance = next(r for r in plan["profile_instances"] if r["profile_id"] == "db.lock")
        profiles = compiler.load_contracts()[2]["profiles"]
        db_lock.validate("F01-R", instance["parameters"], profiles["db.lock"])
        argv, stdin = db_lock.build_invocation(plan, "run")
        self.assertEqual(argv[0], "/usr/bin/ssh")
        self.assertIn("rca-F01-R-inventory-lock", argv)
        self.assertIn("FOR UPDATE", stdin.decode())


if __name__ == "__main__":
    unittest.main()
