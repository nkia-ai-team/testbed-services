from __future__ import annotations

import copy
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / "profiles"
sys.path.insert(0, str(PROFILES))


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, PROFILES / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


host = load("host_stress_executor")
ddl = load("db_ddl_executor")
lock = load("db_lock_executor")
workload = load("db_workload_executor")


def plan(profile: str, scenario: str, params: dict, location: dict | None = None) -> dict:
    return {"scenario": {"id": scenario}, "profile_instances": [{"profile_id": profile, "parameters": copy.deepcopy(params), "location": location or {}}]}


class DbHostFoundations(unittest.TestCase):
    def test_mysql_inverse_ddl_is_exact_and_recoverable(self) -> None:
        p = ddl.CONTRACTS["F02-P"]
        ddl.validate("F02-P", p, {})
        argv, body = ddl.build_invocation(plan("db.ddl", "F02-P", p), "run")
        self.assertEqual(argv[:2], ["/usr/bin/bash", "-s"])
        script = body.decode()
        self.assertIn("information_schema.statistics", script)
        self.assertIn("ALTER TABLE $table DROP INDEX $index", script)
        self.assertIn("CREATE INDEX $index ON $table($column)", script)

    def test_oracle_and_payment_locks_are_tagged_and_terminated(self) -> None:
        for sid in ("F01-P", "F06-H"):
            p = lock.CONTRACTS[sid]
            lock.validate(sid, p, {})
            _, body = lock.build_invocation(plan("db.lock", sid, p), "run")
            text = body.decode()
            if sid == "F01-P":
                # Oracle account row-lock (settlement row exists, single-key contention).
                self.assertIn("FOR UPDATE", text.upper())
                self.assertIn("dbms_session.set_identifier", text)
                self.assertIn("FREEPDB1", text)
            else:
                # F06-H payment writes are fresh INSERTs, so a row-lock cannot block them;
                # the tagged session holds the payments table in EXCLUSIVE MODE instead.
                self.assertNotIn("FOR UPDATE", text.upper())
                self.assertIn("LOCK TABLE $SCHEMA.$TABLE IN $MODE MODE", text)
                self.assertIn("pg_terminate_backend", text)
                self.assertIn("PGAPPNAME", text)

    def test_batch_workload_is_read_only_tagged_and_bounded(self) -> None:
        p = workload.CONTRACTS["F02-G"]
        workload.validate("F02-G", p, {})
        _, body = workload.build_invocation(plan("db.workload", "F02-G", p), "run")
        text = body.decode()
        self.assertIn("PGAPPNAME", text)
        self.assertIn("statement_timeout", text)
        self.assertIn("SELECT count(*)", text)
        self.assertNotIn("DELETE FROM", text.upper())
        self.assertIn("pg_terminate_backend", text)

    def test_storage_contracts_use_measured_pvc_workers_and_exact_cleanup(self) -> None:
        expected = {"F02-H": "192.168.122.184", "F10-R": "192.168.122.184", "F10-H": "192.168.122.14", "F10-P": "192.168.122.184", "F15-P": "192.168.122.11"}
        for sid, address in expected.items():
            p = host.CONTRACTS[sid]
            host.validate(sid, p, {})
            argv, body = host.build_invocation(plan("host.stress", sid, p, {"transport": "ssh", "host": address}), "cleanup")
            self.assertIn(f"nkia@{address}", argv)
            text = body.decode()
            self.assertIn("StrictHostKeyChecking=yes", " ".join(argv))
            self.assertIn("rm -f", text)
            self.assertIn("kill -9", text)

    def test_contract_drift_and_unverified_log_partition_are_rejected(self) -> None:
        drift = copy.deepcopy(host.CONTRACTS["F10-H"])
        drift["target_dir"] = "/dev/sda"
        with self.assertRaises(host.ExecutorError):
            host.validate("F10-H", drift, {})
        with self.assertRaisesRegex(host.ExecutorError, "no verified"):
            host.validate("F10-G", {}, {})


if __name__ == "__main__":
    unittest.main()
