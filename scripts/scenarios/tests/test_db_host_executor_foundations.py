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

    def test_oracle_and_payment_locks_are_bounded_and_reversible(self) -> None:
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
                # the session holds the payments table in EXCLUSIVE MODE instead.
                # 스크립트는 row/table scope를 공유하므로 잠금 형태는 계약으로 고정한다.
                self.assertEqual(p["lock_scope"], "table")
                self.assertEqual(p["lock_mode"], "EXCLUSIVE")
                self.assertIn("LOCK TABLE ${schema}.${table} IN ${mode} MODE", text)
                self.assertIn("PGAPPNAME", text)
                # 정리는 파드 삭제로 한다 — 신원이 실 앱과 같아 이름 기반 종료는 금지
                # (품질 기준서 G6 / 부록 A).
                self.assertIn("delete pod", text)
                self.assertNotIn("pg_terminate_backend", text)
                self.assertNotIn("pg_sleep", text)

    def test_batch_workload_is_read_only_tagged_and_bounded(self) -> None:
        # 2026-08-07 F02-H 캐시 무력화 재설계로 이 프로파일이 라이브가 되면서, 이 테스트가
        # 고정하던 세 가지가 전부 뒤집혔다 — 전부 db.lock이 실측으로 금지한 패턴이었다.
        #   · PGAPPNAME이 `rca-F02-G-batch-heavy-sql` — 시나리오 이름을 세션에 적는 자백.
        #   · 정리가 `pg_terminate_backend` — 신원이 앱과 같아진 뒤에는 실 서비스 세션을
        #     죽인다. 정리는 클라이언트 파드 삭제여야 한다.
        #   · 대상 시나리오 F02-G가 카탈로그에서 사라졌다(계획될 수 없는 시나리오를
        #     라이브 allowlist에 두는 것은 fail-open이라 계약에서 제거).
        # 그래서 같은 이름으로 새 계약을 고정한다: 읽기 전용·유한·앱 신원 사칭.
        scenario_id = "F02-H"
        p = workload.CONTRACTS[scenario_id]
        workload.validate(scenario_id, p, {})
        _, body = workload.build_invocation(plan("db.workload", scenario_id, p), "run")
        text = body.decode()
        self.assertIn("PGAPPNAME", text)
        self.assertIn("statement_timeout", text)
        self.assertIn("SELECT count(*)", text)
        self.assertIn("default_transaction_read_only = on", text)
        self.assertNotIn("DELETE FROM", text.upper())
        self.assertNotIn("pg_terminate_backend", text)
        self.assertNotIn("pg_sleep", text)
        self.assertIn("delete pod", text)
        # 유한성: 파드가 스스로 끝나는 상한(RUNTIME)을 갖는다.
        self.assertIn("RUNTIME", text)

    def test_storage_contracts_use_measured_pvc_workers_and_exact_cleanup(self) -> None:
        # 2026-07-28 nodeSelector 도입 후 실측 배치. 각 도메인 DB가 자기 워커의 장치를
        # 단독으로 쓴다 — commerce PG=tb-w1(.184), banking Oracle=tb-w2(.11),
        # food MySQL=tb-w3(.14). 이전에는 PG와 Oracle이 tb-w1 한 장치를 공유해
        # F02-H와 F10-P가 서로를 오염시켰다.
        #
        # F02-H·F10-H·F10-P·F15-P는 고정 계약이 아니라 사다리다(rate_iops 고정값이
        # 장치 능력의 16~21%뿐이라 피해를 못 냈다). 모든 레벨이 같은 워커를 향해야 한다.
        expected = {"F02-H": "192.168.122.184", "F10-H": "192.168.122.14", "F10-P": "192.168.122.11"}
        cases = [(sid, addr, p) for sid, addr in expected.items() for p in host.STORAGE_LEVELS[sid]]
        cases += [("F15-P", "192.168.122.11", p) for p in host.F15P_LEVELS]
        cases += [("F10-R", "192.168.122.184", host.CONTRACTS["F10-R"])]
        for sid, address, p in cases:
            with self.subTest(scenario=sid, level=p.get("rate_iops") or p.get("vm_bytes") or "fixed"):
                self.assertEqual(p["host"], address)
                host.validate(sid, p, {})
                argv, body = host.build_invocation(plan("host.stress", sid, p, {"transport": "ssh", "host": address}), "cleanup")
                self.assertIn(f"nkia@{address}", argv)
                text = body.decode()
                self.assertIn("StrictHostKeyChecking=yes", " ".join(argv))
                self.assertIn("rm -f", text)
                self.assertIn("kill -9", text)

    def test_storage_ladders_span_below_and_above_measured_device_capacity(self) -> None:
        # 2026-07-28 실측: tb-w3 /dev/vda1 ≈ 18,600 randwrite IOPS. 사다리는 경합이
        # 미미한 구간부터 사실상 무제한까지 걸쳐야 무릎을 찾을 수 있다. 옛 고정값
        # 3000~4000은 능력의 16~21%라 상한으로서 DB를 굶길 수 없었다.
        measured_capacity = 18_600
        for sid, levels in host.STORAGE_LEVELS.items():
            with self.subTest(scenario=sid):
                iops = [level["rate_iops"] for level in levels]
                self.assertEqual(iops, sorted(iops), "사다리는 오름차순이어야 한다")
                self.assertLess(iops[0], measured_capacity * 0.5, "첫 단은 경합이 약해야 한다")
                self.assertGreater(iops[-1], measured_capacity, "마지막 단은 사실상 무제한이어야 한다")

    def test_contract_drift_and_unverified_log_partition_are_rejected(self) -> None:
        drift = copy.deepcopy(host.STORAGE_LEVELS["F10-H"][0])
        drift["target_dir"] = "/dev/sda"
        with self.assertRaises(host.ExecutorError):
            host.validate("F10-H", drift, {})
        # 사다리 밖의 강도도 거부해야 한다 — 옛 고정 계약값(4000)이 대표적이다.
        stale = copy.deepcopy(host.STORAGE_LEVELS["F10-H"][0])
        stale["rate_iops"] = 4000
        with self.assertRaises(host.ExecutorError):
            host.validate("F10-H", stale, {})
        with self.assertRaisesRegex(host.ExecutorError, "no verified"):
            host.validate("F10-G", {}, {})


class HostStressRegistrationIsTwoSidedTests(unittest.TestCase):
    """registry/profiles.json and the executor must agree, in both directions.

    The registry decides what a scenario is *allowed* to ask for; the executor
    decides what it will actually *run*. A scenario listed only in the registry
    dispatches fine and then dies inside the executor — F21-P did exactly that on
    2026-07-30, failing nine seconds into a live run with "scenario has no
    verified bounded host-stress contract". The quality charter raised this as G2
    on 07-27 and asked for this cross-check; until now nothing enforced it, and a
    hand audit of profiles.json alone reported the scenario as registered.
    """

    def test_every_allowlisted_scenario_has_executor_parameters_it_accepts(self) -> None:
        import json

        profile = json.loads((ROOT / "registry" / "profiles.json").read_text())["profiles"]["host.stress"]
        allowed = profile["parameter_contract"]["allowed_scenarios"]
        declared = profile["scenario_parameters"]
        self.assertTrue(allowed, "host.stress allowlist is empty")
        rejected = []
        for scenario_id in allowed:
            parameters = declared.get(scenario_id)
            if parameters is None:
                rejected.append((scenario_id, "no scenario_parameters in the registry"))
                continue
            try:
                host.validate(scenario_id, copy.deepcopy(parameters), profile)
            except host.ExecutorError as exc:
                rejected.append((scenario_id, str(exc)))
        self.assertEqual(rejected, [], f"registry allows what the executor refuses: {rejected}")

    def test_executor_contracts_only_cover_scenarios_that_still_exist(self) -> None:
        # The reverse direction is deliberately weaker than the forward one. A
        # parked scenario keeps its executor contract while dropping out of the
        # live allowlist — F10-R is parked and that is correct, not a defect. What
        # must not survive is a contract for a scenario that no longer exists at
        # all, which is dead code authorising an injection nobody reviews.
        import json

        catalog = json.loads((ROOT / "catalog.json").read_text())
        scenarios = catalog.get("scenarios", catalog)
        known = set(scenarios) if isinstance(scenarios, dict) else {s["id"] for s in scenarios}
        self.assertEqual(
            sorted(set(host.CONTRACTS) - known), [],
            "executor carries contracts for scenarios that are not in the catalog",
        )


if __name__ == "__main__":
    unittest.main()
