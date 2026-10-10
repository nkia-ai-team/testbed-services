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

    def test_mysql_column_rename_is_exact_bounded_and_reversible(self) -> None:
        # F36-R: 마이그레이션이 열 이름만 바꾼다(메타데이터, 행 재작성 없음). 역 DDL 은 같은 꼴로 되돌리기.
        p = ddl.CONTRACTS["F36-R"]
        ddl.validate("F36-R", p, {})
        argv, body = ddl.build_invocation(plan("db.ddl", "F36-R", p), "run")
        self.assertEqual(argv[:2], ["/usr/bin/bash", "-s"])
        self.assertEqual(argv[3:], ["run", "rca-testbed-food", "testbed-mysql-0", "fooddelivery", "restaurants", "region", "delivery_region", "20"])
        self.assertNotIn("F36-R", argv)
        script = body.decode()
        self.assertNotIn("F36-R", script)
        self.assertIn("information_schema.columns", script)
        self.assertIn("SET SESSION lock_wait_timeout=10; ALTER TABLE $table RENAME COLUMN $1 TO $2, ALGORITHM=INSTANT;", script)
        self.assertIn('rename "$column" "$renamed"', script)
        self.assertIn('rename "$renamed" "$column"', script)
        self.assertNotIn("DROP", script)
        # 메타데이터 잠금 대기 실패(1205) 같은 MySQL 오류 첫 줄을 러너 로그에 남긴다.
        self.assertIn('grep -v "Using a password" "$e" | head -n 1 >&2', script)
        self.assertNotIn("2>/dev/null", script)
        # 인덱스 계약(F33-R)은 여전히 인덱스 스크립트로 간다.
        _, index_body = ddl.build_invocation(plan("db.ddl", "F33-R", ddl.CONTRACTS["F33-R"]), "run")
        self.assertIn("DROP INDEX", index_body.decode())
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F36-R", {**p, "renamed_to": "region_v2"}, {})

    def test_postgres_index_swap_leaves_only_an_invalid_index_and_is_reversible(self) -> None:
        # F33-P: 동시 재생성이 statement_timeout 에 끊겨 INVALID 로 남은 채 옛 제약과 인덱스를 지우고 이름을 바꾼다.
        p = ddl.CONTRACTS["F33-P"]
        ddl.validate("F33-P", p, {})
        argv, body = ddl.build_invocation(plan("db.ddl", "F33-P", p), "run")
        self.assertEqual(argv[:2], ["/usr/bin/bash", "-s"])
        self.assertEqual(argv[3:], [
            "run", "rca-testbed-commerce", "testbed-postgres-0", "user_schema", "auth_tokens", "token",
            "auth_tokens_token_key", "idx_auth_tokens_token", "idx_auth_tokens_token_new", "2s", "100000",
        ])
        self.assertNotIn("F33-P", argv)
        script = body.decode()
        self.assertNotIn("F33-P", script)
        # CIC 는 여러 문장 문자열(암묵 트랜잭션) 안에서 돌 수 없어 statement_timeout 을 PGOPTIONS 로 건다.
        self.assertIn('"CREATE UNIQUE INDEX CONCURRENTLY $rebuild ON $rel ($column);" "-c statement_timeout=$timeout"', script)
        # 끊긴 생성이 INVALID 로 남지 않았으면 교체하지 않고 멈춘다.
        self.assertIn('if [[ "$state" != f ]]; then', script)
        self.assertIn("ALTER TABLE $rel DROP CONSTRAINT $constraint; DROP INDEX $schema.$index; ALTER INDEX $schema.$rebuild RENAME TO $index;", script)
        self.assertIn("ADD CONSTRAINT $constraint UNIQUE USING INDEX $constraint", script)
        self.assertIn("CREATE INDEX IF NOT EXISTS $index ON $rel ($column)", script)
        # 행 수 확인은 LIMIT 으로 묶어 녹화 구간에 전수 COUNT 를 남기지 않는다.
        self.assertIn("FROM (SELECT 1 FROM $rel LIMIT $minimum) t", script)
        # 기존 PostgreSQL 인덱스 계약(F02-R)은 tb-runner ssh 경로 그대로다.
        self.assertEqual(ddl.build_invocation(plan("db.ddl", "F02-R", ddl.CONTRACTS["F02-R"], {"host": "192.168.122.206", "transport": "ssh"}), "run")[0][0], "/usr/bin/ssh")
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F33-P", {**p, "statement_timeout": "60s"}, {})

    def test_mysql_cancelled_backfill_holds_the_live_table_and_restores_it(self) -> None:
        # F48-R: 취소된 인덱스 백필이 그림자 표 대신 살아 있는 표를 보류 이름으로 치운다. 행은 지우지 않고,
        # cleanup 은 보류 표를 원래 이름으로 되돌린 뒤 도구가 만든 그림자 표만 지운다.
        p = ddl.CONTRACTS["F48-R"]
        ddl.validate("F48-R", p, {})
        argv, body = ddl.build_invocation(plan("db.ddl", "F48-R", p), "run")
        self.assertEqual(argv[:2], ["/usr/bin/bash", "-s"])
        self.assertEqual(argv[3:], ["run", "rca-testbed-food", "testbed-mysql-0", "fooddelivery",
                                    "menu_popularity_summary", "idx_menu_popularity_restaurant", "restaurant_id", "20"])
        self.assertNotIn("F48-R", argv)
        script = body.decode()
        self.assertNotIn("F48-R", script)
        self.assertIn("CREATE TABLE $shadow LIKE $table; ALTER TABLE $shadow ADD INDEX $index ($column); INSERT INTO $shadow SELECT * FROM $table;", script)
        self.assertIn("RENAME TABLE $table TO $held;", script)
        self.assertIn("RENAME TABLE $held TO $table;", script)
        self.assertIn('shadow="_vt_vrp_', script)
        self.assertIn('held="_vt_hld_', script)
        self.assertEqual(script.count("lock_wait_timeout=10"), 3)
        # 지우는 것은 그림자 표뿐이고, 원래 표 곁에 보류 표가 남아 있으면 지우지 않고 멈춘다.
        self.assertEqual(script.count("DROP TABLE"), 1)
        self.assertIn('mysql "DROP TABLE $shadow;"', script)
        self.assertIn("held table left beside", script)
        self.assertNotIn("DELETE", script)
        self.assertIn('grep -v "Using a password" "$e" | head -n 1 >&2', script)
        # 열 이름 바꾸기(F36-R)와 인덱스 계약(F33-R)은 각자의 스크립트로 간다.
        _, rename_body = ddl.build_invocation(plan("db.ddl", "F36-R", ddl.CONTRACTS["F36-R"]), "run")
        self.assertIn("RENAME COLUMN", rename_body.decode())
        self.assertNotIn("RENAME TABLE", rename_body.decode())
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F48-R", {**p, "table": "menus"}, {})

    def test_mysql_cancelled_backfill_on_order_outbox_uses_the_same_hold_script(self) -> None:
        # F48-P: 같은 보류 표 모드를 주문 이벤트 outbox 표에 쓴다. 스크립트는 F48-R 과 같고 계약(표, 인덱스, 최소 행)만 다르다.
        p = ddl.CONTRACTS["F48-P"]
        ddl.validate("F48-P", p, {})
        argv, body = ddl.build_invocation(plan("db.ddl", "F48-P", p), "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-food", "testbed-mysql-0", "fooddelivery",
                                    "order_outbox_events", "idx_order_outbox_aggregate", "aggregate_id", "1000"])
        self.assertNotIn("F48-P", argv)
        self.assertNotIn("F48-P", body.decode())
        _, f48r_body = ddl.build_invocation(plan("db.ddl", "F48-R", ddl.CONTRACTS["F48-R"]), "run")
        self.assertEqual(body, f48r_body)
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F48-P", ddl.CONTRACTS["F48-R"], {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F48-P", {**p, "table": "orders"}, {})

    def test_mysql_dropped_table_moves_out_of_the_schema_and_back(self) -> None:
        # F53-R: 운영을 가리킨 로컬 마이그레이션의 표 지우기. 진짜 DROP 대신 DPM 이 보지 않는 시스템 스키마 mysql 로
        # 옮기고(RENAME 한 문장), cleanup 은 같은 RENAME 을 반대로 한다. 그림자 표나 _vt_ 이름이 없다.
        p = ddl.CONTRACTS["F53-R"]
        ddl.validate("F53-R", p, {})
        argv, body = ddl.build_invocation(plan("db.ddl", "F53-R", p), "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-food", "testbed-mysql-0", "fooddelivery",
                                    "orders", "mysql", "1000"])
        self.assertNotIn("F53-R", argv)
        script = body.decode()
        self.assertNotIn("F53-R", script)
        self.assertIn('held="${db}_${table}"', script)
        self.assertIn('move "$db.$table" "$hold.$held"', script)
        self.assertIn('move "$hold.$held" "$db.$table"', script)
        self.assertIn("lock_wait_timeout=10", script)
        self.assertNotIn("DROP TABLE", script)
        self.assertNotIn("_vt_", script)
        _, f48p_body = ddl.build_invocation(plan("db.ddl", "F48-P", ddl.CONTRACTS["F48-P"]), "run")
        self.assertNotEqual(body, f48p_body)
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F53-R", {**p, "hold_schema": "fooddelivery"}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F53-R", {**p, "table": "order_items"}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F53-R", ddl.CONTRACTS["F48-P"], {})

    def test_mysql_dropped_payments_table_uses_the_same_drop_path(self) -> None:
        # F53-P: F53-R 과 같은 표 지우기 모드를 결제 표에. 계약은 표 이름만 다르고 원격 스크립트는 같다.
        p = ddl.CONTRACTS["F53-P"]
        ddl.validate("F53-P", p, {})
        argv, body = ddl.build_invocation(plan("db.ddl", "F53-P", p), "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-food", "testbed-mysql-0", "fooddelivery",
                                    "payments", "mysql", "1000"])
        self.assertNotIn("F53-P", argv)
        self.assertNotIn("F53-P", body.decode())
        _, f53r_body = ddl.build_invocation(plan("db.ddl", "F53-R", ddl.CONTRACTS["F53-R"]), "run")
        self.assertEqual(body, f53r_body)
        self.assertEqual({k: v for k, v in p.items() if k != "table"},
                         {k: v for k, v in ddl.CONTRACTS["F53-R"].items() if k != "table"})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F53-P", {**p, "table": "orders"}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F53-P", {**p, "hold_schema": "fooddelivery"}, {})

    def test_oracle_dropped_transfers_table_goes_to_the_recycle_bin_and_back(self) -> None:
        # F35-H: 같은 표 지우기를 banking Oracle 에. 실제 DROP TABLE(PURGE 없음)이라 공간이 빈 공간으로 세어지고, cleanup 은
        # FLASHBACK TABLE 뒤 BIN$ 이름으로 돌아온 인덱스와 제약에 계약의 원래 이름을 다시 붙인다(상태 파일 없음).
        p = ddl.CONTRACTS["F35-H"]
        ddl.validate("F35-H", p, {})
        argv, body = ddl.build_invocation(plan("db.ddl", "F35-H", p), "run")
        self.assertEqual(argv[3:9], ["run", "rca-testbed-banking", "testbed-oracle-0", "FREEPDB1",
                                     "BANKING", "TRANSFERS"])
        self.assertEqual(argv[9:], [p["indexes"], p["constraints"], "1000", "2048"])
        self.assertNotIn("F35-H", " ".join(argv))
        script = body.decode()
        self.assertNotIn("F35-H", script)
        self.assertIn("drop table $schema.$table;", script)
        # PURGE 가 있으면 휴지통을 거치지 않아 되돌릴 수 없다.
        self.assertNotIn("purge;", script.lower())
        self.assertNotIn("purge recyclebin", script.lower())
        self.assertIn("flashback table $schema.$table to before drop;", script)
        self.assertIn("rename constraint", script)
        self.assertEqual(script.count("ddl_lock_timeout = 10"), 3)
        self.assertIn("whenever sqlerror exit failure", script)
        self.assertIn("name='recyclebin'", script)
        # 계약의 이름이 109 실측(2026-10-10) 그대로이고 덮는 열마다 하나씩이다.
        self.assertEqual(sorted(x.split("=")[1] for x in p["indexes"].split(",")),
                         sorted(["SYS_C008658", "SYS_C008659", "IDX_TRANSFERS_FROM", "IDX_TRANSFERS_TO",
                                 "IDX_TRANSFERS_ORDER", "IDX_TRANSFERS_STATUS", "IDX_TRANSFERS_CREATED"]))
        self.assertEqual(len({x.split("=")[0] for x in p["constraints"].split(",")}), 8)
        _, f53r_body = ddl.build_invocation(plan("db.ddl", "F53-R", ddl.CONTRACTS["F53-R"]), "run")
        self.assertNotEqual(body, f53r_body)
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F35-H", {**p, "table": "ACCOUNTS"}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F35-H", {**p, "minimum_free_mb": 0}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F35-H", ddl.CONTRACTS["F53-R"], {})

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
