from __future__ import annotations

import copy
import os
import subprocess
import tempfile
import time
import importlib.util
import sys
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / "profiles"
sys.path.insert(0, str(PROFILES))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import node_guard_fakes  # noqa: E402  (sys.path 조작 뒤에야 import 가능)
import db_operator_fakes  # noqa: E402


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
# v4 C6b: the bodies carry the shared operator-session functions; checks on what a body itself does
# look at its own text only.
db_operator_remote = load("db_operator_remote")
own_body = db_operator_remote.without_library


def plan(profile: str, scenario: str, params: dict, location: dict | None = None) -> dict:
    return {"scenario": {"id": scenario}, "profile_instances": [{"profile_id": profile, "parameters": copy.deepcopy(params), "location": location or {}}]}


class DbHostFoundations(unittest.TestCase):
    def test_mysql_inverse_ddl_is_exact_and_recoverable(self) -> None:
        p = ddl.CONTRACTS["F02-P"]
        ddl.validate("F02-P", p, {})
        argv, body = ddl.build_invocation(plan("db.ddl", "F02-P", p), "run")
        self.assertEqual(argv[:2], ["/usr/bin/bash", "-s"])
        script = own_body(body.decode())
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
        script = own_body(body.decode())
        self.assertNotIn("F36-R", script)
        self.assertIn("information_schema.columns", script)
        self.assertIn("SET SESSION lock_wait_timeout=10; ALTER TABLE $table RENAME COLUMN $1 TO $2, ALGORITHM=INSTANT;", script)
        self.assertIn('rename "$column" "$renamed"', script)
        self.assertIn('rename "$renamed" "$column"', script)
        self.assertNotIn("DROP", script)
        # 메타데이터 잠금 대기 실패(1205) 같은 MySQL 오류 첫 줄을 러너 로그에 남긴다(v4 C6b: 운영자 세션 클라이언트가
        # 첫 오류 줄을 stderr 로 낸다, OperatorClientTests). DB 파드에 exec 하지 않는다.
        self.assertIn('mysql() { mdb "$db" "$1"; }', script)
        self.assertNotIn("2>/dev/null", script)
        self.assertNotIn(" exec ", script)
        # 그 사이의 mdb_run 도 클라이언트의 stderr 를 버리지 않는다(C6b 시험 감사 다3): 본문, mdb, mdb_run, 클라이언트로
        # 이어지는 길 전체를 가짜 MySQL 의 오류 한 줄로 확인한다.
        lib = db_operator_remote.bash_functions().decode()
        mdb_run = lib[lib.index("mdb_run() {"):lib.index("\n}\n", lib.index("mdb_run() {"))]
        self.assertNotIn("2>", mdb_run)
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = db_operator_fakes.install(tmp, mysql=(
                "import sys\nprint('ERROR 1205 (HY000): Lock wait timeout exceeded; try restarting transaction',"
                " file=sys.stderr)\nsys.exit(1)\n"))
            failed = subprocess.run(argv, input=body, env=env, capture_output=True, timeout=60)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn(b"ERROR 1205 (HY000): Lock wait timeout exceeded", failed.stderr)
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
        script = own_body(body.decode())
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
        script = own_body(body.decode())
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
        self.assertIn('mysql() { mdb "$db" "$1"; }', script)
        self.assertNotIn(" exec ", script)
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

    def test_mysql_cancelled_backfill_on_dispatch_outbox_uses_the_same_hold_script(self) -> None:
        # F60-P: 같은 보류 표 모드를 배차 이벤트 outbox 표에 쓴다. 스크립트는 F48-R, F48-P 와 같고 계약(표, 인덱스)만 다르다.
        p = ddl.CONTRACTS["F60-P"]
        ddl.validate("F60-P", p, {})
        argv, body = ddl.build_invocation(plan("db.ddl", "F60-P", p), "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-food", "testbed-mysql-0", "fooddelivery",
                                    "dispatch_outbox_events", "idx_dispatch_outbox_aggregate", "aggregate_id", "1000"])
        self.assertNotIn("F60-P", argv)
        self.assertNotIn("F60-P", body.decode())
        _, f48p_body = ddl.build_invocation(plan("db.ddl", "F48-P", ddl.CONTRACTS["F48-P"]), "run")
        self.assertEqual(body, f48p_body)
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F60-P", ddl.CONTRACTS["F48-P"], {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F60-P", {**p, "table": "dispatches"}, {})

    def test_mysql_dropped_table_moves_out_of_the_schema_and_back(self) -> None:
        # F53-R: 운영을 가리킨 로컬 마이그레이션의 표 지우기. 진짜 DROP 대신 DPM 이 보지 않는 시스템 스키마 mysql 로
        # 옮기고(RENAME 한 문장), cleanup 은 같은 RENAME 을 반대로 한다. 그림자 표나 _vt_ 이름이 없다.
        p = ddl.CONTRACTS["F53-R"]
        ddl.validate("F53-R", p, {})
        argv, body = ddl.build_invocation(plan("db.ddl", "F53-R", p), "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-food", "testbed-mysql-0", "fooddelivery",
                                    "orders", "mysql", "1000"])
        self.assertNotIn("F53-R", argv)
        script = own_body(body.decode())
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

    def test_mysql_dropped_dispatches_table_uses_the_same_drop_path(self) -> None:
        # F32-P: 같은 표 지우기 모드를 배차 표에. 계약은 표 이름만 다르고 원격 스크립트는 같다.
        p = ddl.CONTRACTS["F32-P"]
        ddl.validate("F32-P", p, {})
        argv, body = ddl.build_invocation(plan("db.ddl", "F32-P", p), "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-food", "testbed-mysql-0", "fooddelivery",
                                    "dispatches", "mysql", "1000"])
        self.assertNotIn("F32-P", argv)
        self.assertNotIn("F32-P", body.decode())
        _, f53r_body = ddl.build_invocation(plan("db.ddl", "F53-R", ddl.CONTRACTS["F53-R"]), "run")
        self.assertEqual(body, f53r_body)
        self.assertEqual({k: v for k, v in p.items() if k != "table"},
                         {k: v for k, v in ddl.CONTRACTS["F53-R"].items() if k != "table"})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F32-P", {**p, "table": "dispatch_events"}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F32-P", ddl.CONTRACTS["F33-R"], {})

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
        script = own_body(body.decode())
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
                         sorted(["SYS_C008748", "SYS_C008749", "IDX_TRANSFERS_FROM", "IDX_TRANSFERS_TO",
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

    def test_oracle_dropped_accounts_table_brings_its_row_trigger_back(self) -> None:
        # F35-P: 같은 Oracle 표 지우기를 계좌 표에. 표의 행 트리거도 휴지통으로 갔다가 FLASHBACK 뒤 BIN$ 이름으로 돌아오므로
        # 계약이 트리거 이름을 사건:시점:상태로 짝지어 들고, cleanup 이 인덱스, 제약과 함께 원래 이름을 다시 붙인다.
        p = ddl.CONTRACTS["F35-P"]
        ddl.validate("F35-P", p, {})
        argv, body = ddl.build_invocation(plan("db.ddl", "F35-P", p), "run")
        self.assertEqual(argv[3:9], ["run", "rca-testbed-banking", "testbed-oracle-0", "FREEPDB1",
                                     "BANKING", "ACCOUNTS"])
        self.assertEqual(argv[9:], [p["indexes"], p["constraints"], "1000", "1024", p["triggers"]])
        self.assertEqual(p["triggers"], "UPDATE:BEFORE_EACH_ROW:ENABLED=TRG_ACCOUNTS_UPDATED_AT")
        self.assertNotIn("F35-P", " ".join(argv))
        script = own_body(body.decode())
        self.assertNotIn("F35-P", script)
        self.assertIn("alter trigger $schema.", script)
        self.assertIn("from dba_triggers t where t.table_owner='$schema' and t.table_name='$table'", script)
        # F35-H 는 트리거 인자 없이 같은 스크립트를 그대로 쓴다(트리거 없는 표는 목록에 트리거 줄이 없다).
        h_argv, h_body = ddl.build_invocation(plan("db.ddl", "F35-H", ddl.CONTRACTS["F35-H"]), "run")
        self.assertEqual(h_body, body)
        self.assertEqual(len(h_argv), len(argv) - 1)
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F35-P", {k: v for k, v in p.items() if k != "triggers"}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F35-P", ddl.CONTRACTS["F35-H"], {})

    def test_postgres_dropped_table_moves_out_unrecorded_and_back(self) -> None:
        # F23-P: 운영을 가리킨 로컬 마이그레이션의 표 지우기, PostgreSQL 판. 기록되지 않는 세션(track=none)이 원본을 public 으로
        # 옮기고 옮긴 표의 통계 카운터를 0 으로 돌린다(DPM 이 합산하는 seq_scan, idx_scan 이 실제 지우기처럼 내려감).
        # cleanup 도 기록되지 않는 세션에서 원본을 되돌린다.
        p = ddl.CONTRACTS["F23-P"]
        ddl.validate("F23-P", p, {})
        argv, body = ddl.build_invocation(plan("db.ddl", "F23-P", p), "run")
        self.assertEqual(argv[:2], ["/usr/bin/bash", "-s"])
        self.assertEqual(argv[3:], ["run", "rca-testbed-commerce", "testbed-postgres-0", "product_schema",
                                    "products", "public", "1000"])
        self.assertNotIn("F23-P", argv)
        script = own_body(body.decode())
        self.assertNotIn("F23-P", script)
        self.assertIn('quiet="-c pg_stat_statements.track=none"', script)
        run = script[script.index(" run)"):script.index(" cleanup)")]
        # 순서: 확인 → 원본을 옮김 → 옮긴 표의 카운터 초기화 → 결과 확인.
        order = ["check", 'move "$schema" "$hold"', "count(pg_stat_reset_single_table_counters(o))",
                 '[[ "$(present "$schema" "$table")" == 0']
        positions = [run.index(x) for x in order]
        self.assertEqual(positions, sorted(positions))
        # 주입이 보내는 SQL 은 모두 기록되지 않는 세션에서 돈다(Top SQL 에 주입 흔적이 남지 않음).
        calls = [line for line in script.splitlines() if "sql \"" in line and not line.lstrip().startswith(("sql()", "#"))]
        self.assertTrue(calls)
        for line in calls:
            self.assertIn("$quiet", line, line)
        # 표 idx_scan 은 인덱스 카운터에서 읽으므로 인덱스도 함께 0 으로 돌린다.
        self.assertIn("SELECT indexrelid FROM pg_index WHERE indrelid = '$hold.$table'::regclass", run)
        self.assertNotIn("DROP TABLE IF EXISTS", script)
        self.assertNotIn("LIKE", script)
        self.assertIn("SET lock_timeout = '10s'; ALTER TABLE $1.$table SET SCHEMA $2;", script)
        cleanup = script[script.index(" cleanup)"):script.index(" recovery)")]
        self.assertIn('move "$hold" "$schema"', cleanup)
        # 재시작한 서비스가 만든 빈 표만 지운다(1000행 미만이고 아무도 참조하지 않을 때). 아니면 사람에게 남긴다.
        self.assertIn('[[ "$small" == t && "$refs" == 0 ]] || {', cleanup)
        self.assertIn("exit 3", cleanup)
        self.assertIn("original", cleanup)
        self.assertIn("FROM (SELECT 1 FROM $schema.$table LIMIT $minimum) t", script)
        _, f33p_body = ddl.build_invocation(plan("db.ddl", "F33-P", ddl.CONTRACTS["F33-P"]), "run")
        _, f53r_body = ddl.build_invocation(plan("db.ddl", "F53-R", ddl.CONTRACTS["F53-R"]), "run")
        self.assertNotEqual(body, f33p_body)
        self.assertNotEqual(body, f53r_body)
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F23-P", {**p, "hold_schema": "product_schema"}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F23-P", {**p, "table": "product_variants"}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F23-P", ddl.CONTRACTS["F53-R"], {})

    def test_oracle_identity_generator_rewinds_behind_the_table_and_returns_past_it(self) -> None:
        # F57-R: 행만 옮긴 표 이전의 전환. TRANSFERS 를 read_only_seconds 동안 읽기 전용으로 둔 뒤 identity 생성기를 최대 키보다
        # rewind_rows 뒤에서 다시 시작시키고 쓰기를 연다. cleanup 은 READ WRITE 와 START WITH LIMIT VALUE 로 되돌린다.
        p = ddl.CONTRACTS["F57-R"]
        ddl.validate("F57-R", p, {})
        argv, body = ddl.build_invocation(plan("db.ddl", "F57-R", p), "run")
        self.assertEqual(argv[3:9], ["run", "rca-testbed-banking", "testbed-oracle-0", "FREEPDB1",
                                     "BANKING", "TRANSFERS"])
        self.assertEqual(argv[9:], ["ID", "SYS_C008748", "60000", "6", "1000"])
        self.assertNotIn("F57-R", " ".join(argv))
        script = own_body(body.decode())
        self.assertNotIn("F57-R", script)
        self.assertIn("whenever sqlerror exit failure", script)
        self.assertIn("ddl_lock_timeout = $wait", script)
        # 기본 키가 없으면 되감기가 실패 대신 중복 키를 쓴다: 계약의 기본 키가 그 열 하나를 덮는지 본다.
        self.assertIn("k.constraint_type='P' and k.constraint_name='$pk'", script)
        self.assertIn("generation_type='BY DEFAULT'", script)
        run = script[script.index(" run)"):script.index(" cleanup)")]
        # 전환 순서: 원래 상태와 키 구간 확인 → 읽기 전용 → 유지 → 멈춘 최대 키로 다시 확인 → 되감기 → 쓰기 열기.
        order = ["original", 'dense "$high"', "read only;", 'sleep "$hold"', "if ! dense",
                 "(start with $((high - rewind + 1)))"]
        positions = [run.index(x) for x in order] + [run.rindex("read write;")]
        self.assertEqual(positions, sorted(positions))
        # 다시 확인이 실패하면 쓰기를 먼저 열고 멈춘다.
        failed = run[run.index("if ! dense"):run.index("fi\n", run.index("if ! dense"))]
        self.assertIn("read write;", failed)
        self.assertIn("exit 4", failed)
        cleanup = script[script.index(" cleanup)"):script.index(" recovery)")]
        self.assertIn("read write;", cleanup)
        self.assertIn("(start with limit value)", cleanup)
        self.assertIn("original", cleanup)
        self.assertIn("original", script[script.index(" recovery)"):])
        # 빈 답이 '앞' 이나 '뒤' 로 읽히지 않는다: 수는 숫자인지 확인하고 아니면 멈춘다(fail-closed).
        self.assertIn('[[ "$v" =~ ^[0-9]+$ ]] || { echo "not a number', script)
        self.assertIn("n=$(next_key) || exit 5", script)
        # run 은 러너 coordinator lock 을 쥔 채 돌고 lease 는 30초다(profile-control.py, runner.py). run 안의 대기를 모두
        # 묶는다: DDL 은 run_wait 초씩 run_tries 번(1초 간격), run 이 내는 DDL 은 많아야 셋, 그 밖의 대기는 읽기 전용 유지뿐.
        wait = int(re.search(r"run_wait=(\d+)", script).group(1))
        tries = int(re.search(r"run_tries=(\d+)", script).group(1))
        # DDL 호출은 넷이지만 한 경로에서는 많아야 셋이다(읽기 전용 → 재확인 실패면 쓰기 열기, 또는 되감기와 쓰기 열기 →
        # 실패면 쓰기 열기). 넷 모두 run 예산(run_wait, run_tries)을 쓴다.
        self.assertEqual(run.count("ddl "), 4)
        self.assertEqual(run.count('ddl "$run_wait" "$run_tries"'), 4)
        self.assertEqual(run.count("sleep"), 1)
        worst = p["read_only_seconds"] + 3 * (tries * wait + tries - 1)
        self.assertLessEqual(worst, 22, "run must stay well inside the runner's 30 s lease")
        # cleanup 도 대기를 묶는다(5초씩 두 번).
        self.assertEqual(cleanup.count("ddl 5 2 "), 2)
        # 키 구간은 다시 시작할 자리부터 최대 키 - 1000 까지 전부를 센다(가장 새 1000 키는 커밋 전 거래의 것일 수 있다).
        self.assertIn("between $((high - rewind + 1)) and $((high - 1000))", script)
        self.assertIn('== "$((rewind - 1000))"', script)
        # 생성기가 앞인지 볼 때 최대 키를 먼저 읽는다(반대 순서는 캐시 블록이 넘어가는 순간 거짓이 된다).
        ahead = script[script.index("ahead() {"):]
        self.assertLess(ahead.index("high_key"), ahead.index("next_key"))
        # 행, 인덱스, 제약은 손대지 않는다.
        for word in ("drop ", "delete ", "truncate", "insert ", "update "):
            self.assertNotIn(word, script.lower())
        _, f35h_body = ddl.build_invocation(plan("db.ddl", "F35-H", ddl.CONTRACTS["F35-H"]), "run")
        self.assertNotEqual(body, f35h_body)
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F57-R", {**p, "rewind_rows": 4000000}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F57-R", {**p, "primary_key": "SYS_C008749"}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F57-R", {**p, "read_only_seconds": 0}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F57-R", ddl.CONTRACTS["F35-H"], {})

    def test_mysql_foreign_key_migration_copies_the_table_in_the_background_and_is_dropped_after(self) -> None:
        # F60-R: 정합성 보강 마이그레이션이 dispatch_events 에 dispatches 로의 외래 키를 ALGORITHM, LOCK 없이 더한다.
        # foreign_key_checks 가 켜져 있으면 MySQL 은 표를 복사하고 그동안 자식, 부모 표 쓰기가 메타데이터 잠금을 기다린다.
        p = ddl.CONTRACTS["F60-R"]
        ddl.validate("F60-R", p, {})
        argv, body = ddl.build_invocation(plan("db.ddl", "F60-R", p), "run")
        self.assertEqual(argv[3:], ["run", "rca-testbed-food", "testbed-mysql-0", "fooddelivery", "dispatch_events",
                                    "dispatch_id", "fk_dispatch_events_dispatch", "dispatches", "id", "300", "1000", "90"])
        self.assertNotIn("F60-R", " ".join(argv))
        script = own_body(body.decode())
        self.assertNotIn("F60-R", script)
        # 마이그레이션은 쓴 그대로: 방식 지정이 없어야 서버가 복사를 고른다. 방식 지정은 되돌리기(외래 키 지우기)에만 있다.
        self.assertIn('statement="ALTER TABLE $table ADD CONSTRAINT $fk FOREIGN KEY ($column) REFERENCES $parent ($parent_column)"', script)
        self.assertEqual(script.count("ALGORITHM="), 1)
        self.assertIn("DROP FOREIGN KEY $fk, ALGORITHM=INPLACE, LOCK=NONE", script)
        self.assertIn("@@global.foreign_key_checks", script)
        run = script[script.index(" run)"):script.index(" cleanup)")]
        # run 은 러너 coordinator lock 을 쥔 채 돈다: 마이그레이션은 DB 파드 안의 mysql 클라이언트 하나로 띄우고(정답지의
        # root@localhost, v4 C6b 사용자 결정 Q8. 셸, nohup 없이 exec 가 mysql 을 바로 띄운다), 세션이 보이는지 1초씩 많아야
        # 10번 본 뒤 돌아온다. 복사를 기다리지 않는다.
        self.assertIn('mdb_pod_spawn root "$db" "$statement" "$log"', run)
        self.assertNotIn(" exec ", script)
        # 데이터 볼륨 여유는 파드 안 df 가 아니라 kubelet 요약 API 의 노드 파일시스템이다.
        self.assertIn("free=$(number \"$(node_fs KB | awk '{print $3}')\")", script)
        self.assertIn("for _ in 1 2 3 4 5 6 7 8 9 10; do", run)
        self.assertEqual(run.count("sleep"), 1)
        # 마이그레이션 세션은 문장으로 찾고 찾는 질의 자기 세션은 뺀다.
        self.assertIn("id <> CONNECTION_ID()", script)
        cleanup = script[script.index(" cleanup)"):script.index(" recovery)")]
        # cleanup 은 복사가 스스로 끝나기를 drain 까지 기다리고, 남았으면 멈춘 뒤 외래 키를 지운다.
        order = ["end=$((SECONDS + drain))", "KILL QUERY", "DROP FOREIGN KEY", "dbo_reap", "rm -rf"]
        positions = [cleanup.index(x) for x in order] + [cleanup.rindex("original")]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("lock_wait_timeout=10", cleanup)
        # preflight 만 고아 이력을 센다(전체 조인이라 run 에서는 다시 하지 않는다).
        self.assertIn(" preflight) check; [[ \"$(orphans)\" == 0 ]]; mdb_pod_alone ;;", script)
        # A client a lost runner left in the database pod stops preflight and recovery (look only); cleanup ends it with
        # the failure path's one exec (mdb_pod_clear, C6b review 3, 4).
        self.assertIn(" recovery) rows; original; mdb_pod_alone ;;", script)
        self.assertRegex(cleanup, r"\n  original\n  mdb_pod_clear root\n  ;;")
        self.assertNotIn("orphans", run)
        # 행, 인덱스, 부모 표는 손대지 않는다.
        for word in ("drop table", "drop index", "delete ", "truncate", "insert ", "update ", "rename "):
            self.assertNotIn(word, script.lower())
        _, f33r_body = ddl.build_invocation(plan("db.ddl", "F33-R", ddl.CONTRACTS["F33-R"]), "run")
        self.assertNotEqual(body, f33r_body)
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F60-R", {**p, "table": "dispatches"}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F60-R", {**p, "drain_seconds": 3600}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F60-R", ddl.CONTRACTS["F32-P"], {})

    def test_postgres_sequence_rewinds_behind_the_table_after_a_read_only_switch_and_returns_past_it(self) -> None:
        # F57-P: 행만 옮긴 DB 이전의 전환, 원본 엔진(PostgreSQL) 판. 원본처럼 옛 쪽을 읽기 전용으로 두고(DB 기본값) 앱 세션을 끊어
        # read_only_seconds 동안 쓰기가 실패하게 한 뒤, 한 트랜잭션의 EXCLUSIVE 잠금 아래 키 구간을 다시 세고 id 시퀀스를 최대 키보다
        # rewind_rows 뒤로 돌리고, 읽기 전용을 풀고 세션을 다시 끊는다. 표 통계는 건드리지 않는다(복사와 복제로 채운 표는 이미
        # 행을 센다). cleanup 은 읽기 전용이 남았으면 풀고 setval(최대 키)(원본의 해결). 모든 SQL 은 기록되지 않는 세션에서 돈다.
        p = ddl.CONTRACTS["F57-P"]
        ddl.validate("F57-P", p, {})
        argv, body = ddl.build_invocation(plan("db.ddl", "F57-P", p), "run")
        self.assertEqual(argv[:2], ["/usr/bin/bash", "-s"])
        self.assertEqual(argv[3:], ["run", "rca-testbed-commerce", "testbed-postgres-0", "inventory_schema",
                                    "inventory_movements", "id", "inventory_movements_id_seq",
                                    "inventory_movements_pkey", "100000", "6", "1000"])
        self.assertNotIn("F57-P", " ".join(argv))
        script = own_body(body.decode())
        self.assertNotIn("F57-P", script)
        # 실행기 세션은 기록되지 않고(track=none), 전환 중 DB 기본값(읽기 전용)에 걸리지 않게 쓰기 가능으로 연다.
        self.assertIn('quiet="-c pg_stat_statements.track=none -c default_transaction_read_only=off"', script)
        self.assertIn('cutover="$quiet -c lock_timeout=2s"', script)
        # 여러 줄에 걸친 DO 블록까지 sql 호출마다 세션 옵션을 넘기는지 본다.
        calls = re.findall(r'(?<!\w)sql "((?:[^"\\]|\\.)*)"( "\$\w+")?', script)
        self.assertGreaterEqual(len(calls), 8)
        for text, option in calls:
            self.assertIn(option, (' "$quiet"', ' "$cutover"'), text[:60])
        # 계약 시퀀스가 키 열의 기본값이고 계약 기본 키가 그 열 하나를 덮어야 한다(아니면 되감기가 실패 대신 중복을 쓴다).
        self.assertIn("pg_get_serial_sequence('$schema.$table', '$column')", script)
        self.assertIn("contype = 'p' AND conname = '$pk' AND conkey = ARRAY[", script)
        # 전환: DB 기본값을 읽기 전용으로, 앱 계정의 클라이언트 세션만 끊는다(자기 세션 제외).
        self.assertIn("ALTER DATABASE %I SET default_transaction_read_only = on', current_database()", script)
        self.assertIn("ALTER DATABASE %I RESET default_transaction_read_only', current_database()", script)
        self.assertIn("usename = current_user AND backend_type = 'client backend' AND pid <> pg_backend_pid()", script)
        run = script[script.index(" run)"):script.index(" cleanup)")]
        # 순서: 원래 상태와 키 구간 확인 → 읽기 전용 → 세션 끊기 → 유지 → 잠금 → 잠금 아래 다시 세기 → 맞지 않으면 실패 →
        # 시퀀스 되감기 → 쓰기 가능으로 되돌리고 세션 끊기 → 시퀀스가 뒤인지 확인.
        order = ["original", 'dense "$(high_key)"', "  read_only\n", "  cut ||", 'sleep "$hold"',
                 "LOCK TABLE $schema.$table IN EXCLUSIVE MODE;", "SELECT count(*) INTO n", "RAISE EXCEPTION",
                 "PERFORM setval('$schema.$sequence', h - $rewind, true);", "  restore\n  read_only_set &&",
                 "if ahead; then"]
        positions = [run.index(x) for x in order]
        self.assertEqual(positions, sorted(positions))
        # 읽기 전용 뒤 어느 단계가 실패해도 먼저 쓰기 가능으로 되돌리고 세션을 끊는다.
        failed = run[run.index("if ! sql"):run.index("  fi\n", run.index("if ! sql"))]
        self.assertIn("restore", failed)
        self.assertIn("exit 4", failed)
        self.assertIn('cut || { restore;', run)
        self.assertIn("restore() { read_write; cut; }", script)
        self.assertIn("BETWEEN h - $rewind + 1 AND h - 1000", run)
        self.assertIn("n <> $rewind - 1000", run)
        # run 은 러너 coordinator lock(lease 30초)을 쥔 채 돈다: 유지 대기는 셸 sleep 하나뿐(SQL 안에 대기 문장 없음),
        # 잠금 대기는 2초로 묶는다. 호출 약 15번 × 0.4초 + 유지 + 잠금 대기가 lease 안이다.
        self.assertEqual(run.count("sleep"), 1)
        self.assertNotIn("pg_sleep", script)
        self.assertEqual(run.count('"$cutover"'), 1)
        self.assertLessEqual(p["read_only_seconds"] + 2 + 15 * 0.4, 22, "run must stay well inside the runner's 30 s lease")
        # 실제 전환에 없는 흔적(누적 통계 0, 통계 수집, 정리)을 만들지 않는다.
        for word in ("pg_stat_reset", "analyze", "vacuum"):
            self.assertNotIn(word, script.lower())
        # 빈 답이 '앞' 이나 '뒤' 로 읽히지 않는다(fail-closed), 시퀀스보다 최대 키를 먼저 읽는다.
        self.assertIn('[[ "$v" =~ ^[0-9]+$ ]] || { echo "not a number', script)
        ahead = script[script.index("ahead() {"):]
        self.assertLess(ahead.index("high_key"), ahead.index("next_key"))
        self.assertIn("BETWEEN $((high - rewind + 1)) AND $((high - 1000))", script)
        self.assertIn('== "$((rewind - 1000))"', script)
        self.assertIn("original() { check; ahead; ! read_only_set; }", script)
        cleanup = script[script.index(" cleanup)"):script.index(" recovery)")]
        self.assertIn("if read_only_set; then restore; fi", cleanup)
        self.assertIn("ahead || sql \"SELECT setval('$schema.$sequence', (SELECT max($column) FROM $schema.$table));\"", cleanup)
        # cleanup 과 recovery 는 끝에 원래 상태를 실제로 확인한다(주석의 낱말이 아니라 호출 줄).
        self.assertRegex(cleanup, r"\n  original\n  ;;")
        self.assertRegex(script[script.index(" recovery)"):], r"^ recovery\) original ;;")
        # 행, 인덱스, 제약, 권한은 손대지 않는다.
        for word in ("drop ", "delete ", "truncate", "insert ", "update ", "revoke", "grant ", "alter table"):
            self.assertNotIn(word, script.lower())
        _, f23p_body = ddl.build_invocation(plan("db.ddl", "F23-P", ddl.CONTRACTS["F23-P"]), "run")
        _, f57r_body = ddl.build_invocation(plan("db.ddl", "F57-R", ddl.CONTRACTS["F57-R"]), "run")
        self.assertNotEqual(body, f23p_body)
        self.assertNotEqual(body, f57r_body)
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F57-P", {**p, "rewind_rows": 1000000}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F57-P", {**p, "read_only_seconds": 60}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F57-P", {**p, "table": "inventory"}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F57-P", ddl.CONTRACTS["F57-R"], {})

    def test_postgres_column_migration_queues_behind_a_slow_export_and_the_column_is_dropped_after(self) -> None:
        # F66-R: 회원 표에 null 허용 열을 더하는 마이그레이션(카탈로그만)이 같은 표를 읽는 느린 내보내기 질의 뒤에서
        # ACCESS EXCLUSIVE 를 기다리고, 그 뒤의 회원 표 질의가 모두 줄을 선다. 내보내기가 스스로 끝나면 줄이 풀린다.
        p = ddl.CONTRACTS["F66-R"]
        ddl.validate("F66-R", p, {})
        argv, body = ddl.build_invocation(plan("db.ddl", "F66-R", p), "run")
        self.assertEqual(argv[:2], ["/usr/bin/bash", "-s"])
        self.assertEqual(argv[3:], ["run", "rca-testbed-commerce", "testbed-postgres-0", "user_schema", "users",
                                    "marketing_opt_in", "boolean", "auth_tokens", "user_id", "420", "300", "10", "70"])
        self.assertNotIn("F66-R", " ".join(argv))
        script = own_body(body.decode())
        self.assertNotIn("F66-R", script)
        # 두 문장은 원본 꼴 그대로: 기본값 없는 열 추가(카탈로그만), 같은 표를 읽는 내보내기. 세션은 이 문장으로 찾는다.
        self.assertIn('migration_sql="ALTER TABLE $rel ADD COLUMN $column $ctype"', script)
        self.assertIn('export_sql="COPY (SELECT u.id, u.email, u.name, t.created_at, t.expires_at FROM $rel u JOIN '
                      '$schema.$join t ON t.$join_column = u.id) TO STDOUT"', script)
        # 기본값도 NOT NULL 도 없는 열이라야 표를 다시 쓰지 않는 카탈로그 변경이다.
        self.assertEqual(p["add_column_type"], "boolean")
        migration_line = next(line for line in script.splitlines() if line.startswith("migration_sql="))
        self.assertNotIn("DEFAULT", migration_line.upper())
        self.assertNotIn("NOT NULL", migration_line.upper())
        self.assertIn("pid <> pg_backend_pid() AND query = '$1'", script)
        # 탐침은 Top SQL 에 남지 않고, 두 세션은 드라이버 기본 이름으로 붙는다(시나리오 id 없음).
        self.assertIn('sql() { pdb "$1" "-c pg_stat_statements.track=none"; }', script)
        self.assertIn('identity="PostgreSQL JDBC Driver"', script)
        self.assertNotIn(" exec ", script)
        # 마이그레이션이 포기하지 않고 줄을 서려면 시간 제한이 없어야 하고, 대기 세션이 쌓여도 연결 한도 아래여야 한다.
        self.assertIn('[[ "$(sql "SHOW lock_timeout;")" == 0 && "$(sql "SHOW statement_timeout;")" == 0 ]]', script)
        self.assertIn('[[ "$sessions" -le "$max_sessions" ]]', script)
        run = script[script.index(" run)"):script.index(" cleanup)")]
        # 순서: 확인 → 행 추정으로 속도 → 내보내기를 백그라운드로 → 그 잠금 확인 → 마이그레이션을 백그라운드로 →
        # 내보내기 뒤에서 기다리는지 확인. run 은 러너 coordinator lock(30초 lease)을 쥔 채 돌므로 둘 다 기다리지 않는다.
        # v4 C6b 수정 2회차: 내보내기는 정답지의 배치 작업 그대로 DB 네임스페이스의 Job(도메인 노드, 파드 주소)이고, 마이그레이션은
        # 운영자 세션이다(러너의 백그라운드 클라이언트). Job 은 내보내기, drain, 1분 뒤 스스로 끝난다(U8).
        order = ["  check\n", "reltuples", "rate=$(( est / export_seconds + 1 ))", 'job="$(pdb_export_name)"',
                 'pdb_export_job "$job" "$export_sql" "$rate" "$deadline" "$identity"',
                 "l.granted", 'pdb_spawn "$migration_sql" "$migration_log" "$identity"', "pg_blocking_pids(m.pid)"]
        positions = [run.index(x) for x in order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("deadline=$(( export_seconds + drain + 60 ))", script)
        self.assertEqual(run.count("for _ in $(seq 1 60); do"), 1)
        self.assertEqual(run.count("for _ in 1 2 3 4 5 6 7 8 9 10; do"), 1)
        self.assertEqual(run.count("sleep 1\n"), 2)
        self.assertNotIn("pdb_spawn_paced", script)
        self.assertNotIn("pg_sleep", script)
        # 어느 쪽이 자리를 잡지 못하면 띄운 것을 거두고 멈춘다.
        self.assertIn("|| { stop_export;", run)
        self.assertIn("cancel_migration; stop_export; pdb_delete_exports", run)
        cleanup = script[script.index(" cleanup)"):script.index(" recovery)")]
        # cleanup 은 내보내기가 스스로 끝나기를 drain 까지 기다리고, 남았으면 끝내고 마이그레이션을 취소한 뒤 열을 지우고
        # 내보내기 Job 을 지운다.
        order = ["end=$((SECONDS + drain))", "stop_export", "cancel_migration", "DROP COLUMN IF EXISTS $column",
                 "pdb_delete_exports", "dbo_reap", "rm -rf"]
        positions = [cleanup.index(x) for x in order] + [cleanup.rindex("original")]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("SET lock_timeout = '5s'", cleanup)
        self.assertRegex(cleanup, r"\n  original\n  ;;")
        # 앞 회차가 잃어버린 내보내기 Job 이 남아 있으면 preflight(check)와 recovery 가 멈춘다.
        self.assertIn(' recovery) rows; original; [[ "$(waiting)" == 0 ]]; pdb_export_gone ;;', script)
        check = script[script.index("check() {"):script.index("\n}\n", script.index("check() {"))]
        self.assertIn("pdb_export_gone", check)
        # 세션은 문장으로 찾은 pid 로만 끝내거나 취소한다. 행, 인덱스, 다른 표, 권한은 손대지 않는다.
        self.assertIn("pg_terminate_backend($pid)", script)
        self.assertIn("pg_cancel_backend($pid)", script)
        for word in ("drop table", "drop index", "delete ", "truncate", "insert ", "update ", "revoke", "grant ",
                     "create index", "lock table"):
            self.assertNotIn(word, script.lower())
        _, f57p_body = ddl.build_invocation(plan("db.ddl", "F57-P", ddl.CONTRACTS["F57-P"]), "run")
        _, f33p_body = ddl.build_invocation(plan("db.ddl", "F33-P", ddl.CONTRACTS["F33-P"]), "run")
        self.assertNotEqual(body, f57p_body)
        self.assertNotEqual(body, f33p_body)
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F66-R", {**p, "table": "auth_tokens"}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F66-R", {**p, "export_seconds": 3600}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F66-R", {**p, "maximum_sessions": 95}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F66-R", ddl.CONTRACTS["F57-P"], {})

    def test_postgres_event_table_migration_queues_behind_an_event_export_with_writes_in_flight(self) -> None:
        # F66-P: F66-R 의 잠금 대기열을 재고 이벤트 outbox 에. 내보내기는 그 표 자기 행을 일정한 속도로 내보내고(열 목록 인자),
        # 줄을 서는 것은 재고 예약 트랜잭션 끝의 outbox INSERT 다. F66-R 의 인자와 내보내기 문장은 그대로다.
        p = ddl.CONTRACTS["F66-P"]
        ddl.validate("F66-P", p, {})
        argv, body = ddl.build_invocation(plan("db.ddl", "F66-P", p), "run")
        self.assertEqual(argv[:2], ["/usr/bin/bash", "-s"])
        self.assertEqual(argv[3:], ["run", "rca-testbed-commerce", "testbed-postgres-0", "inventory_schema",
                                    "outbox_events", "trace_id", "varchar(64)", "", "", "420", "300", "10000", "70",
                                    "id, aggregate_type, aggregate_id, event_type, topic, payload, created_at, "
                                    "published_at"])
        self.assertNotIn("F66-P", " ".join(argv))
        script = own_body(body.decode())
        self.assertNotIn("F66-P", script)
        _, f66r_body = ddl.build_invocation(plan("db.ddl", "F66-R", ddl.CONTRACTS["F66-R"]), "run")
        self.assertEqual(body, f66r_body, "both run the same remote script; only the arguments differ")
        f66r_argv, _ = ddl.build_invocation(plan("db.ddl", "F66-R", ddl.CONTRACTS["F66-R"]), "run")
        self.assertEqual(len(f66r_argv), 3 + 13, "F66-R keeps its thirteen arguments and its joined export")
        # 열 목록이 있으면 표 자기 행을 내보내고 그 표의 행 추정으로 속도를 정한다. 없으면 F66-R 의 회원 ⋈ 토큰이다.
        self.assertIn('export_columns="${14:-}"', script)
        branch = script[script.index('if [[ -n "$export_columns" ]]; then'):script.index("\nfi\n", script.index('if [[ -n "$export_columns" ]]; then'))]
        self.assertIn('export_sql="COPY (SELECT $export_columns FROM $rel) TO STDOUT"', branch)
        self.assertIn('paced="$rel"', branch)
        self.assertIn('paced="$schema.$join"', branch)
        self.assertIn("oid = '$paced'::regclass", script)
        self.assertIn("(( est >= 100 * export_seconds ))", script)
        # 열 목록은 문장 텍스트로 세션을 찾는 탐침의 작은따옴표 안에 들어가므로 따옴표가 없어야 한다.
        self.assertNotIn("'", p["export_columns"])
        self.assertNotIn(";", p["export_columns"])
        # 더하는 열은 기본값도 NOT NULL 도 없어 카탈로그만 바꾼다.
        self.assertNotIn("DEFAULT", p["add_column_type"].upper())
        self.assertNotIn("NOT NULL", p["add_column_type"].upper())
        # 실행 폴더 이름에 시나리오 id 가 없다(v4 C6b: 러너의 상태 디렉터리, 파드가 아니다). 내보내기 Job 이름도
        # 도메인과 무작위 꼬리뿐이다(pdb_export_name).
        self.assertIn('run_dir="$(dbo_run_dir "db-ddl-queue-$schema-$table")"', script)
        # 24시간 보존 약 7만 2천 행: 내보내기 시작 조건(행 추정 >= 100 x 420)과 preflight 최소 행.
        self.assertLessEqual(100 * p["export_seconds"], 72000)
        self.assertGreaterEqual(p["minimum_rows"], 10000)
        self.assertLessEqual(p["maximum_sessions"], 70)
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F66-P", {**p, "table": "inventory"}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F66-P", {**p, "export_columns": "*"}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F66-P", {**p, "export_seconds": 3600}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F66-P", ddl.CONTRACTS["F66-R"], {})

    def test_mysql_instant_column_migration_queues_behind_a_slow_export_on_the_metadata_lock(self) -> None:
        # F60-H: F66-R 의 잠금 대기열을 원본 엔진 MySQL 에. 배차 표 자기 행을 한 행씩 일정한 속도로 내보내는 동안
        # ALGORITHM=INSTANT 열 추가가 배타 메타데이터 잠금을 기다리고, 그 뒤의 배차 표 질의가 모두 줄을 선다.
        p = ddl.CONTRACTS["F60-H"]
        ddl.validate("F60-H", p, {})
        argv, body = ddl.build_invocation(plan("db.ddl", "F60-H", p), "run")
        self.assertEqual(argv[:2], ["/usr/bin/bash", "-s"])
        self.assertEqual(argv[3:], ["run", "rca-testbed-food", "testbed-mysql-0", "fooddelivery", "dispatches",
                                    "cancel_reason", "varchar(64)",
                                    "id, order_id, courier_id, eta_minutes, status, assigned_at",
                                    "360", "300", "100000", "90"])
        self.assertNotIn("F60-H", " ".join(argv))
        script = own_body(body.decode())
        self.assertNotIn("F60-H", script)
        self.assertEqual(body, ddl.MYSQL_QUEUE_REMOTE)
        _, f66r_body = ddl.build_invocation(plan("db.ddl", "F66-R", ddl.CONTRACTS["F66-R"]), "run")
        _, f60r_body = ddl.build_invocation(plan("db.ddl", "F60-R", ddl.CONTRACTS["F60-R"]), "run")
        self.assertNotEqual(body, f66r_body)
        self.assertNotEqual(body, f60r_body)
        # 두 문장은 원본 꼴 그대로: 표 자기 행의 내보내기, 메타데이터만 바꾸는 INSTANT 열 추가. 세션은 이 문장으로 찾는다.
        self.assertIn('export_sql="SELECT $export_columns FROM $table"', script)
        self.assertIn('migration_sql="ALTER TABLE $table ADD COLUMN $column $ctype NULL, ALGORITHM=INSTANT"', script)
        self.assertNotIn("DEFAULT", p["add_column_type"].upper())
        self.assertNotIn("NOT NULL", p["add_column_type"].upper())
        self.assertIn("WHERE id <> CONNECTION_ID() AND info = '$1'", script)
        # 열 목록은 문장 텍스트로 세션을 찾는 탐침의 작은따옴표 안에 들어가므로 따옴표가 없어야 한다.
        self.assertNotIn("'", p["export_columns"])
        self.assertNotIn(";", p["export_columns"])
        # 마이그레이션이 포기하지 않고 줄을 서고 내보내기가 끝까지 돌려면 시간 제한이 없어야 하고,
        # INSTANT 행 버전 여유, 메타데이터 잠금 계측, 연결 한도 아래의 세션 수를 확인한다.
        self.assertIn('[[ "$(number "$(mysql "SELECT @@global.lock_wait_timeout;")")" -ge 3600 ]]', script)
        self.assertIn('[[ "$(mysql "SELECT @@global.max_execution_time;")" == 0 ]]', script)
        self.assertIn("wait/lock/metadata/sql/mdl", script)
        self.assertIn('[[ "$versions" -le 60 ]]', script)
        self.assertIn('[[ "$sessions" -le "$max_sessions" ]]', script)
        run = script[script.index(" run)"):script.index(" cleanup)")]
        # 순서: 확인 → 행 추정으로 속도 → 내보내기를 백그라운드로(앱 계정, 한 행씩) → 그 잠금 확인 → 마이그레이션을
        # 백그라운드로(root) → 메타데이터 잠금 대기 확인. run 은 러너 coordinator lock(30초 lease)을 쥔 채 돌므로 기다리지 않는다.
        # v4 C6b(사용자 결정 Q8): 두 세션은 DB 파드 안의 mysql 클라이언트다(정답지의 fooddelivery@localhost,
        # root@localhost). 내보내기 행은 러너가 정한 바이트 속도로만 읽혀 서버가 export_seconds 동안 보낸다(mdb_pod_pace).
        order = ["  check\n", "table_rows", 'mdb_pod_spawn app "$db" "$export_sql" "$export_log" paced',
                 "lock_status = 'GRANTED'", 'mdb_pod_pace "$export_log" "$id" "$est"',
                 'mdb_pod_spawn root "$db" "$migration_sql" "$migration_log"',
                 "'Waiting for table metadata lock'"]
        positions = [run.index(x) for x in order]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(run.count("for _ in 1 2 3 4 5 6 7 8 9 10; do"), 2)
        # 러너가 FIN 없이 사라져도 서버가 쓰기를 못 하면 net_write_timeout 뒤 내보내기를 끝내 공유 잠금이 풀린다.
        self.assertIn("@@global.net_write_timeout", script)
        # 인위 지연(SLEEP())이 아니라 받는 쪽이 천천히 받아 문장이 활성으로 남는다.
        self.assertNotIn("SLEEP(", script)
        self.assertIn("|| { stop_export;", run)
        self.assertIn("cancel_migration; stop_export", run)
        # 로그 파일 이름에 시나리오 id 가 없다(러너의 상태 디렉터리).
        self.assertIn('run_dir="$(dbo_run_dir "db-ddl-queue-$db-$table")"', script)
        cleanup = script[script.index(" cleanup)"):script.index(" recovery)")]
        order = ["end=$((SECONDS + drain))", "stop_export", "cancel_migration",
                 "DROP COLUMN $column, ALGORITHM=INSTANT", "dbo_reap", "rm -rf"]
        # 내보내기를 끝낼 때 서버 문장을 먼저, 그다음 러너의 읽기를 풀어 파드 안 클라이언트가 끝나게 한다.
        # 리더가 사라져 흐름이 끝나지 않으면 파드 안 클라이언트를 정리 경로의 exec 한 번으로 끝낸다(mdb_pod_clear).
        self.assertIn('mysql "KILL QUERY $id;" >/dev/null; mdb_pod_drain "$export_log" || mdb_pod_clear "$app_user" root; }', script)
        positions = [cleanup.index(x) for x in order] + [cleanup.rindex("original")]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("SET SESSION lock_wait_timeout = 5", cleanup)
        self.assertRegex(cleanup, r"\n  original\n  mdb_pod_clear \"\$app_user\" root\n  ;;")
        # 러너가 사라지면 파드 안 내보내기 클라이언트가 막힌 채 남는다. preflight(check)는 그것을 보고 멈추기만 하고
        # (mdb_pod_alone), cleanup 이 exec 한 번으로 끝낸다(mdb_pod_clear, C6b 리뷰 3회차 상 1, 4회차 하 3). 기동기는 없다.
        self.assertNotIn("deadline", script)
        self.assertNotIn("mysqlsh", script)
        self.assertIn('app_user="$MDB_APP_USER"', script)
        # recovery 는 계약대로 읽기 전용 확인이다(C6b 리뷰 4회차 하 3): 남은 클라이언트를 보기만 한다.
        self.assertIn(' recovery) rows; original; [[ "$(waiting)" == 0 ]]; mdb_pod_alone ;;', script)
        check = script[script.index("check() {"):script.index("\n}\n", script.index("check() {"))]
        self.assertIn("mdb_pod_alone", check)
        self.assertNotIn("mdb_pod_clear", check)
        # 세션은 문장으로 찾은 id 로만 끝낸다. 행, 인덱스, 다른 표, 권한은 손대지 않는다.
        self.assertIn("KILL QUERY $id;", script)
        for word in ("drop table", "drop index", "delete ", "truncate", "insert ", "update ", "revoke", "grant ",
                     "create index", "lock table", "rename"):
            self.assertNotIn(word, script.lower())
        # 행 추정 약 174만: 내보내기 시작 조건(행 추정 >= 100 x 360)과 preflight 최소 행.
        self.assertLessEqual(100 * p["export_seconds"], 1_000_000)
        self.assertGreaterEqual(p["minimum_rows"], 100 * p["export_seconds"])
        self.assertLessEqual(p["maximum_sessions"], 90)
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F60-H", {**p, "table": "dispatch_events"}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F60-H", {**p, "export_columns": "*"}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F60-H", {**p, "export_seconds": 3600}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F60-H", ddl.CONTRACTS["F60-R"], {})

    def test_mysql_metadata_lock_queue_on_the_payments_table_reuses_the_f60h_path(self) -> None:
        # F53-H: F60-H 와 같은 MySQL 메타데이터 잠금 대기열을 결제 표에. 결제 표 자기 행을 한 행씩 일정한 속도로 내보내는
        # 동안 INSTANT 열 추가가 기다리고, 결제마다 처음 하는 결제 INSERT 가 그 뒤에 줄을 선다. 실행기 본문은 F60-H 와 같다.
        p = ddl.CONTRACTS["F53-H"]
        ddl.validate("F53-H", p, {})
        argv, body = ddl.build_invocation(plan("db.ddl", "F53-H", p), "run")
        self.assertEqual(argv[:2], ["/usr/bin/bash", "-s"])
        self.assertEqual(argv[3:], ["run", "rca-testbed-food", "testbed-mysql-0", "fooddelivery", "payments",
                                    "pg_transaction_id", "varchar(64)",
                                    "id, order_id, pg_provider, amount, status, processed_at, settled_at, created_at",
                                    "360", "300", "100000", "90"])
        self.assertNotIn("F53-H", " ".join(argv))
        self.assertNotIn("F53-H", body.decode())
        self.assertEqual(body, ddl.MYSQL_QUEUE_REMOTE)
        # 행 추정 약 173만: 내보내기 시작 조건(행 추정 >= 100 x 360)과 preflight 최소 행.
        self.assertGreaterEqual(p["minimum_rows"], 100 * p["export_seconds"])
        self.assertLessEqual(p["maximum_sessions"], 90)
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F53-H", {**p, "table": "payment_outbox_events"}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F53-H", ddl.CONTRACTS["F60-H"], {})

    def test_mysql_metadata_lock_queue_on_the_small_menus_table_behind_a_joined_sales_export(self) -> None:
        # F36-P: F60-H 와 같은 MySQL 메타데이터 잠금 대기열을 요청마다 읽히는 작은 메뉴 표(95행)에. 판매 실적 내보내기가
        # 큰 주문 품목 표를 메뉴 표와 이어 읽는 동안(주문 품목 행 추정으로 속도) INSTANT 열 추가가 기다리고, 메뉴 읽기가
        # 그 뒤에 줄을 선다. 실행기 본문은 F60-H 와 같고 이어 읽는 표 인자 두 개만 끝에 더한다.
        p = ddl.CONTRACTS["F36-P"]
        ddl.validate("F36-P", p, {})
        argv, body = ddl.build_invocation(plan("db.ddl", "F36-P", p), "run")
        self.assertEqual(argv[:2], ["/usr/bin/bash", "-s"])
        self.assertEqual(argv[3:], ["run", "rca-testbed-food", "testbed-mysql-0", "fooddelivery", "menus",
                                    "allergen_note", "varchar(255)",
                                    "m.id, m.name, m.price, i.order_id, i.qty, i.unit_price",
                                    "360", "300", "50", "90", "order_items", "menu_id"])
        self.assertNotIn("F36-P", " ".join(argv))
        self.assertNotIn("F36-P", body.decode())
        self.assertEqual(body, ddl.MYSQL_QUEUE_REMOTE)
        # 이어 읽는 인자가 없는 F60-H, F53-H 의 argv 는 그대로 12개다.
        for other in ("F60-H", "F53-H"):
            other_argv, _ = ddl.build_invocation(plan("db.ddl", other, ddl.CONTRACTS[other]), "run")
            self.assertEqual(len(other_argv[3:]), 12)
        script = own_body(body.decode())
        # 이어 읽으면 내보내기는 큰 표의 판매 줄에 작은 표의 이름과 가격을 붙인 질의이고 속도는 큰 표의 행 추정으로 맞춘다.
        self.assertIn('export_sql="SELECT $export_columns FROM $table m JOIN $join i ON i.$join_column = m.id"', script)
        self.assertIn("table_name = '$paced'", script)
        self.assertIn('join="${13:-}"; join_column="${14:-}"', script)
        # preflight 가 이어 읽는 표에 내보내기가 export_seconds 동안 돌 행이 있는지 본다(작은 표의 minimum_rows 와 따로).
        check = script[script.index("check() {"):script.index("stop_export()")]
        self.assertLess(check.index("  rows\n"), check.index("  join_rows\n"))
        self.assertIn('[[ -z "$join" ||', script)
        # 열 목록은 문장 텍스트로 세션을 찾는 탐침의 작은따옴표 안에 들어가므로 따옴표와 세미콜론이 없어야 한다.
        self.assertNotIn("'", p["export_columns"])
        self.assertNotIn(";", p["export_columns"])
        self.assertNotIn("DEFAULT", p["add_column_type"].upper())
        self.assertNotIn("NOT NULL", p["add_column_type"].upper())
        # 메뉴 표는 95행이라 minimum_rows 는 작은 표 하한이고, 주문 품목(행 추정 약 258만)은 시작 조건 100 x 360 을 넘는다.
        self.assertLess(p["minimum_rows"], 100)
        self.assertLessEqual(p["maximum_sessions"], 90)
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F36-P", {**p, "table": "restaurants"}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F36-P", {k: v for k, v in p.items() if not k.startswith("export_join")}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F36-P", ddl.CONTRACTS["F60-H"], {})

    def test_postgres_lock_queue_on_the_order_line_table_reuses_the_f66p_path(self) -> None:
        # F67-H: F66-P 와 같은 PostgreSQL 잠금 대기열을 주문 품목 표에. 표 자기 행을 일정한 속도로 내보내는 동안 품목 할인
        # 금액 열 추가가 기다리고, checkout 마다 주문 행 뒤에 보내는 품목 INSERT 가 그 뒤에 줄을 선다. 실행기 본문은 F66-P 와 같다.
        p = ddl.CONTRACTS["F67-H"]
        ddl.validate("F67-H", p, {})
        argv, body = ddl.build_invocation(plan("db.ddl", "F67-H", p), "run")
        self.assertEqual(argv[:2], ["/usr/bin/bash", "-s"])
        self.assertEqual(argv[3:], ["run", "rca-testbed-commerce", "testbed-postgres-0", "order_schema", "order_items",
                                    "discount_amount", "numeric(12,2)", "", "", "360", "300", "1000000", "70",
                                    "id, order_id, product_id, product_name, quantity, unit_price, subtotal"])
        self.assertNotIn("F67-H", " ".join(argv))
        self.assertNotIn("F67-H", body.decode())
        _, f66p_body = ddl.build_invocation(plan("db.ddl", "F66-P", ddl.CONTRACTS["F66-P"]), "run")
        self.assertEqual(body, f66p_body, "both run the same remote script; only the arguments differ")
        # 열 목록은 문장 텍스트로 세션을 찾는 탐침의 작은따옴표 안에 들어가므로 따옴표와 세미콜론이 없어야 한다.
        self.assertNotIn("'", p["export_columns"])
        self.assertNotIn(";", p["export_columns"])
        # 더하는 열은 기본값도 NOT NULL 도 없어 카탈로그만 바꾼다.
        self.assertNotIn("DEFAULT", p["add_column_type"].upper())
        self.assertNotIn("NOT NULL", p["add_column_type"].upper())
        # 약 643만 행: 내보내기 시작 조건(행 추정 >= 100 x 360)과 preflight 최소 행.
        self.assertGreaterEqual(p["minimum_rows"], 100 * p["export_seconds"])
        self.assertLessEqual(p["maximum_sessions"], 70)
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F67-H", {**p, "table": "orders"}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F67-H", {**p, "export_seconds": 3600}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F67-H", ddl.CONTRACTS["F66-P"], {})

    def test_postgres_lock_queue_on_the_small_products_table_behind_a_joined_sales_export(self) -> None:
        # F73-R: F66-R 와 같은 PostgreSQL 잠금 대기열을 요청마다 읽히는 작은 상품 표(2,016행)에. 카테고리 매출 내보내기가 큰
        # 주문 품목 표에 상품의 카테고리, 이름, 가격을 붙여 읽는 동안(주문 품목 행 추정으로 속도) 브랜드 열 추가가 기다리고, 상품
        # 읽기가 모두 그 뒤에 줄을 선다. 실행기 본문은 F66-R, F66-P, F67-H 와 같고 이어 읽는 표 인자 두 자리만 채운다.
        p = ddl.CONTRACTS["F73-R"]
        ddl.validate("F73-R", p, {})
        argv, body = ddl.build_invocation(plan("db.ddl", "F73-R", p), "run")
        self.assertEqual(argv[:2], ["/usr/bin/bash", "-s"])
        self.assertEqual(argv[3:], ["run", "rca-testbed-commerce", "testbed-postgres-0", "product_schema", "products",
                                    "brand", "varchar(100)", "order_schema.order_items", "product_id",
                                    "360", "300", "1000", "70",
                                    "m.id, m.category_id, m.name, m.price, i.order_id, i.quantity, i.unit_price, i.subtotal"])
        self.assertNotIn("F73-R", " ".join(argv))
        self.assertNotIn("F73-R", body.decode())
        _, f66r_body = ddl.build_invocation(plan("db.ddl", "F66-R", ddl.CONTRACTS["F66-R"]), "run")
        self.assertEqual(body, f66r_body, "all PostgreSQL queue scenarios run the same remote script")
        # 이어 읽는 표가 없는 F66-P, F67-H 의 argv 는 그 두 자리가 빈 문자열 그대로다.
        for other in ("F66-P", "F67-H"):
            other_argv, _ = ddl.build_invocation(plan("db.ddl", other, ddl.CONTRACTS[other]), "run")
            self.assertEqual(other_argv[3 + 7:3 + 9], ["", ""])
        script = own_body(body.decode())
        # 열 목록과 이어 읽는 표가 모두 있으면 내보내기는 큰 표의 줄에 작은 표의 열을 붙인 질의이고 속도는 큰 표의 행 추정으로
        # 맞춘다. 열 목록만 있으면 F66-P 의 자기 행 내보내기, 열 목록이 없으면 F66-R 의 회원과 토큰 내보내기 그대로다.
        self.assertIn('if [[ -n "$export_columns" && -n "$join" ]]; then\n'
                      '  export_sql="COPY (SELECT $export_columns FROM $rel m JOIN $join i ON i.$join_column = m.id) TO STDOUT"\n'
                      '  paced="$join"', script)
        self.assertIn('elif [[ -n "$export_columns" ]]; then\n  export_sql="COPY (SELECT $export_columns FROM $rel) TO STDOUT"',
                      script)
        self.assertIn('paced="$schema.$join"', script)
        # preflight 가 이어 읽는 표에 내보내기가 export_seconds 동안 돌 행이 있는지 본다(작은 표의 minimum_rows 와 따로).
        check = script[script.index("check() {"):script.index("stop_export()")]
        self.assertLess(check.index("  rows\n"), check.index("  join_rows\n"))
        self.assertIn('if [[ -z "$export_columns" || -z "$join" ]]; then return 0; fi', script)
        # 이어 읽는 표는 다른 스키마라 스키마까지 적고, 정규식 cast 와 LIMIT 질의에 그대로 들어간다.
        self.assertEqual(p["export_join_table"].count("."), 1)
        self.assertNotIn("'", p["export_columns"])
        self.assertNotIn(";", p["export_columns"])
        self.assertNotIn("DEFAULT", p["add_column_type"].upper())
        self.assertNotIn("NOT NULL", p["add_column_type"].upper())
        # 상품 표는 2,016행이라 minimum_rows 는 작은 표 하한이고, 주문 품목(행 추정 약 618만)은 시작 조건 100 x 360 을 넘는다.
        self.assertLess(p["minimum_rows"], 100 * p["export_seconds"])
        self.assertLessEqual(p["maximum_sessions"], 70)
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F73-R", {**p, "table": "categories"}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F73-R", {k: v for k, v in p.items() if not k.startswith("export_join")}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F73-R", ddl.CONTRACTS["F67-H"], {})

    def test_postgres_vacuum_full_holds_the_tables_until_the_rewrite_ends_and_nothing_is_undone(self) -> None:
        # F67-R: 운영자가 공간을 되찾으려고 가장 큰 두 표에 VACUUM (FULL) 을 나란히 돌린다. 다시 쓰는 내내 각 표의
        # ACCESS EXCLUSIVE 를 쥐어 결제마다 쓰는 payment_logs INSERT 가 기다린다. 다시 쓰기가 끝나면 스스로 풀린다.
        p = ddl.CONTRACTS["F67-R"]
        ddl.validate("F67-R", p, {})
        argv, body = ddl.build_invocation(plan("db.ddl", "F67-R", p), "run")
        self.assertEqual(argv[:2], ["/usr/bin/bash", "-s"])
        self.assertEqual(argv[3:], ["run", "rca-testbed-commerce", "testbed-postgres-0",
                                    "payment_schema.payment_logs shipping_schema.shipment_events", "720", "1000000",
                                    "70", "8000"])
        self.assertNotIn("F67-R", " ".join(argv))
        script = own_body(body.decode())
        self.assertNotIn("F67-R", script)
        # 문장은 원본 꼴 그대로(VACUUM FULL), 표마다 세션 하나. 세션은 이 문장으로 찾고 운영자의 psql 이름으로 붙는다.
        self.assertIn("statement() { printf 'VACUUM (FULL) %s' \"$1\"; }", script)
        # v4 C6b: 운영자의 psql 세션은 러너에서 리스너로 붙는다(예전에는 DB 파드 안). 파드에 exec 하지 않는다.
        self.assertIn('pdb_spawn "$(statement "$rel")" "$(log_of "$rel")" psql', script)
        self.assertIn("pid <> pg_backend_pid() AND query = '$(statement \"$1\")'", script)
        self.assertIn('sql() { pdb "$1" "-c pg_stat_statements.track=none"; }', script)
        self.assertNotIn(" exec ", script)
        # 데이터 볼륨의 크기, 사용, 여유는 kubelet 요약 API 의 노드 파일시스템이다(파드 안 df 가 아니다).
        self.assertIn("read -r size used avail < <(node_fs MB)", script)
        # 쓰기가 포기하지 않고 줄을 서려면 시간 제한이 없어야 하고, 대기 세션과 디스크 사본에 자리가 있어야 한다.
        self.assertIn('[[ "$(sql "SHOW lock_timeout;")" == 0 && "$(sql "SHOW statement_timeout;")" == 0 ]]', script)
        self.assertIn('[[ "$sessions" -le "$max_sessions" ]]', script)
        self.assertIn("(used + need + 1024) * 100 < size * 80", script)
        run = script[script.index(" run)"):script.index(" cleanup)")]
        # 순서: 확인 → 표마다 백그라운드 세션 → 모든 세션이 자기 표의 AccessExclusiveLock 을 쥔 것 확인. 못 쥐면 거두고 멈춤.
        order = ["  check\n", "pdb_spawn", "l.mode = 'AccessExclusiveLock' AND l.granted", "cancel_all"]
        positions = [run.index(x) for x in order]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(run.count("for _ in 1 2 3 4 5 6 7 8 9 10; do"), 1)
        cleanup = script[script.index(" cleanup)"):script.index(" recovery)")]
        # cleanup 은 다시 쓰기가 스스로 끝나기를 drain 까지 기다리고, 남았으면 취소(옛 사본 유지)한 뒤 로그만 지운다.
        order = ["end=$((SECONDS + drain))", "cancel_all", "rm -rf"]
        positions = [cleanup.index(x) for x in order] + [cleanup.rindex("rows")]
        self.assertEqual(positions, sorted(positions))
        self.assertIn(" recovery) rows; original; quiet ;;", script)
        self.assertIn("pg_cancel_backend($pid)", script)
        for word in ("drop table", "drop index", "delete ", "truncate", "insert ", "update ", "revoke", "grant ",
                     "create index", "lock table", "alter ", "pg_terminate_backend"):
            self.assertNotIn(word, script.lower())
        _, f66r_body = ddl.build_invocation(plan("db.ddl", "F66-R", ddl.CONTRACTS["F66-R"]), "run")
        self.assertNotEqual(body, f66r_body)
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F67-R", {**p, "tables": "payment_schema.payments"}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F67-R", {**p, "drain_seconds": 3600}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F67-R", {**p, "minimum_free_mb": 100}, {})
        with self.assertRaisesRegex(ddl.ExecutorError, "exactly match"):
            ddl.validate("F67-R", ddl.CONTRACTS["F66-R"], {})

    def test_oracle_and_payment_locks_are_bounded_and_reversible(self) -> None:
        for sid in ("F01-P", "F06-H"):
            p = lock.CONTRACTS[sid]
            lock.validate(sid, p, {})
            _, body = lock.build_invocation(plan("db.lock", sid, p), "run")
            text = body.decode()
            if sid == "F01-P":
                # Oracle account row-lock (settlement row exists, single-key contention).
                # v4 C6: the tag is set on the runner-side session through the driver
                # (conn.client_identifier), no longer by a PL/SQL call inside the DB pod.
                self.assertIn("FOR UPDATE", text.upper())
                self.assertIn("conn.client_identifier = _env(\"LOCK_TAG\")", text)
                self.assertIn("FREEPDB1", text)
            else:
                # F06-H payment writes are fresh INSERTs, so a row-lock cannot block them;
                # the session holds the payments table in EXCLUSIVE MODE instead.
                # 스크립트는 row/table scope를 공유하므로 잠금 형태는 계약으로 고정한다.
                self.assertEqual(p["lock_scope"], "table")
                self.assertEqual(p["lock_mode"], "EXCLUSIVE")
                self.assertIn("printf 'LOCK TABLE %s.%s IN %s MODE;' \"$1\" \"$2\" \"$4\"", text)
                self.assertIn('name: PGAPPNAME\n          value: "${identity}"', text)
                # 정리는 파드 삭제로 한다 — 신원이 실 앱과 같아 이름 기반 종료는 금지
                # (품질 기준서 G6 / 부록 A).
                self.assertIn("dbo_k delete pod", text)
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


def remote(sandbox: Path, *args: str, path: str | None = None):
    """The worker side of host.stress in a sandbox: the state roots move into the sandbox and
    systemd is the fake from node_guard_fakes (the guard arms a timer on every run). The second
    stdin line is the one build_invocation sends (guard_stdin_line), with a plan's 20m limit."""
    import os
    import shlex
    import subprocess

    preamble = ("set -- " + " ".join(shlex.quote(a) for a in args) + "\n").encode()
    env = dict(os.environ, **node_guard_fakes.install(sandbox), OPSJOB_LEGACY_ROOT=str(sandbox / "legacy"))
    if path:
        env["PATH"] = f"{path}:{env['PATH']}"
    guard = b"expire_after=1800 guard_hold=960\n"
    return subprocess.run(["bash", "-s"], input=preamble + guard + host.REMOTE, env=env, capture_output=True, timeout=120)


class HostStressLeavesNoScenarioTraceTests(unittest.TestCase):
    """Nothing host.stress leaves on a worker may name the scenario (design principle 8).

    Until 2026-10-16 the scenario id rode on the sudo command line
    (`bash -s -- run F15-P pressure ...`), which the worker's auth log forwards to 119,
    and it named the fio job, the fill file on the DB volume and the state files whose
    paths sit in process cmdlines. Recorded cases carried those lines as a ready answer.
    """

    FORBIDDEN = ("lucida", "scenario")

    @staticmethod
    def every_parameter_set():
        sets = [(sid, p) for sid, p in host.CONTRACTS.items()]
        sets += [(sid, p) for sid, levels in host.STORAGE_LEVELS.items() for p in levels]
        sets += [("F09-R", p) for p in host.F09R_LEVELS]
        sets += [("F05-P", p) for p in host.F05P_LEVELS]
        sets += [("F15-P", p) for p in host.F15P_LEVELS]
        return sets

    def test_command_line_and_stdin_carry_no_scenario_id(self) -> None:
        import subprocess

        mode_words = {"fio", "watermark", "pressure", "cpu", "memhog"}
        for sid, p in self.every_parameter_set():
            for action in ("preflight", "run", "cleanup", "recovery"):
                with self.subTest(scenario=sid, mode=p["mode"], action=action):
                    argv, stdin = host.build_invocation(
                        plan("host.stress", sid, p, {"transport": "ssh", "host": p["host"]}), action
                    )
                    # sudo logs exactly what follows it; nothing may follow `bash -s`.
                    self.assertEqual(argv[-3:], ["sudo", "bash", "-s"])
                    line = " ".join(argv)
                    self.assertNotIn(sid, line)
                    for word in (*self.FORBIDDEN, *mode_words):
                        self.assertNotIn(word, line.split())
                    # The id is not derivable on the worker at all: it is absent from the
                    # script and its arguments, not merely from the logged command line.
                    self.assertNotIn(sid.encode(), stdin)
                    self.assertNotIn(sid.lower().encode(), stdin)
                    preamble = stdin.split(b"\n", 1)[0] + b"\n"
                    self.assertTrue(preamble.startswith(b"set -- "))
                    completed = subprocess.run(
                        ["bash", "-s"], input=preamble + b'printf "%s\\n" "$@"\n',
                        capture_output=True, timeout=30,
                    )
                    received = completed.stdout.decode().splitlines()
                    self.assertEqual(received[:3], [action, p["mode"], p["host"]])

    def test_names_that_reach_the_worker_are_neutral(self) -> None:
        # Comments never reach the worker. The only executable lines allowed to spell
        # the old wording are the ones that find leftovers of the old names.
        for raw in host.REMOTE.decode().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "legacy_root=" in line or ".lucida-*-" in line:
                continue
            with self.subTest(line=line[:80]):
                for word in self.FORBIDDEN:
                    self.assertNotIn(word, line)
                self.assertNotIn("$scenario", line)

    def _remote(self, sandbox: Path, *args: str) -> int:
        return remote(sandbox, *args).returncode

    def test_cpu_job_lifecycle_and_cmdline_in_a_sandbox(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Path(tmp)
            (sandbox / "legacy").mkdir()
            cpu = ("cpu", "192.168.122.14", "1", "30")
            self.assertEqual(self._remote(sandbox, "preflight", *cpu), 0)
            try:
                self.assertEqual(self._remote(sandbox, "run", *cpu), 0)
                pgid = (sandbox / "state" / "burnin.pgid").read_text().strip()
                cmdline = Path(f"/proc/{pgid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
                # The sandbox path is the test's own; on the worker it is the state root.
                cmdline = cmdline.replace(str(sandbox / "state"), "/var/lib/opsjobs")
                for word in self.FORBIDDEN:
                    self.assertNotIn(word, cmdline)
                self.assertNotEqual(self._remote(sandbox, "recovery", *cpu), 0, "a running job is not recovered")
            finally:
                self.assertEqual(self._remote(sandbox, "cleanup", *cpu), 0)
            self.assertEqual(self._remote(sandbox, "recovery", *cpu), 0)
            self.assertEqual(sorted(p.name for p in (sandbox / "state").iterdir()), [])

    def test_leftovers_under_the_old_names_block_preflight_and_are_cleaned_up(self) -> None:
        import os
        import subprocess
        import tempfile
        import time

        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Path(tmp)
            legacy = sandbox / "legacy"
            target = sandbox / "target"
            legacy.mkdir()
            target.mkdir()
            # An old cpu burner group that outlived its run, with its log. Detached the
            # way the old executor left it (orphaned, so init reaps it), not as our child.
            subprocess.run(
                ["bash", "-c", f'setsid bash -c \'echo $$ >"{legacy}/F09-R.pgid"; sleep 60\' >/dev/null 2>&1 &'],
                check=True, timeout=10,
            )
            pgid = ""
            for _ in range(50):
                if (legacy / "F09-R.pgid").exists():
                    pgid = (legacy / "F09-R.pgid").read_text().strip()
                    if pgid:
                        break
                time.sleep(0.1)
            self.assertTrue(pgid)
            try:
                (legacy / "F09-R.log").write_text("x")
                cpu = ("cpu", "192.168.122.184", "1", "30")
                self.assertNotEqual(self._remote(sandbox, "preflight", *cpu), 0)
                self.assertNotEqual(self._remote(sandbox, "recovery", *cpu), 0)
                self.assertEqual(self._remote(sandbox, "cleanup", *cpu), 0)
                self.assertEqual(sorted(p.name for p in legacy.iterdir()), [])
                self.assertEqual(self._remote(sandbox, "recovery", *cpu), 0)
                time.sleep(0.5)
                with self.assertRaises(ProcessLookupError):
                    os.killpg(int(pgid), 0)
            finally:
                try:
                    os.killpg(int(pgid), 9)
                except ProcessLookupError:
                    pass
            # An old fill file on the DB volume blocks recovery of the same mode only.
            (target / ".lucida-F10-R-watermark").write_bytes(b"")
            (target / ".lucida-F02-H-fio").write_bytes(b"")
            watermark = ("watermark", "192.168.122.184", str(target), "85", "10240", "51200")
            self.assertNotEqual(self._remote(sandbox, "recovery", *watermark), 0)
            self.assertEqual(self._remote(sandbox, "cleanup", *watermark), 0)
            self.assertEqual(self._remote(sandbox, "recovery", *watermark), 0)
            self.assertEqual([p.name for p in target.iterdir()], [".lucida-F02-H-fio"])
            # A stale old pid file whose pid now belongs to something else is dropped,
            # and that process is left alone.
            bystander = subprocess.Popen(["sleep", "60"])
            try:
                (legacy / "F10-H.pid").write_text(f"{bystander.pid}\n")
                pressure = ("pressure", "192.168.122.11", "1", "1", "1M", "5")
                self.assertEqual(self._remote(sandbox, "recovery", *pressure), 0)
                self.assertEqual(self._remote(sandbox, "cleanup", *pressure), 0)
                self.assertIsNone(bystander.poll())
                self.assertFalse((legacy / "F10-H.pid").exists())
            finally:
                bystander.kill()
                bystander.wait(timeout=10)


class HostStressNodeUndoTests(unittest.TestCase):
    """U8 node self-recovery: every host.stress job arms, before it starts, a timer on the worker
    that undoes it by itself; cleanup postpones and then cancels it (executor_common guard).

    2026-10-17 F80-R-run-bad63a76: cleanup could not reach a node that the injection had pushed into
    NotReady, and the runner stopped DIRTY with the change still on the node.
    """

    JOBS = {"fio": "iobench", "watermark": "bulkcopy", "pressure": "soak", "cpu": "burnin", "memhog": "memhold"}
    STARTS = {"fio": "nohup fio", "watermark": "fallocate", "pressure": "nohup stress-ng", "cpu": "setsid bash", "memhog": "setsid bash"}

    @staticmethod
    def _branch(mode: str, action: str) -> str:
        block = host.REMOTE.decode().split(f"\n {mode})", 1)[1].split("esac ;;", 1)[0]
        return block.split(f"{action})", 1)[1].split(";;", 1)[0]

    def test_every_job_arms_its_undo_before_it_starts_and_cleanup_cancels_it_last(self) -> None:
        for mode, start in self.STARTS.items():
            with self.subTest(mode=mode):
                run_case = self._branch(mode, "run")
                self.assertIn("arm ", run_case)
                self.assertLess(run_case.index("arm "), run_case.index(start))
                cleanup_case = self._branch(mode, "cleanup")
                self.assertTrue(cleanup_case.strip().startswith('guard_postpone "$job" "$guard_hold"'), cleanup_case)
                self.assertTrue(cleanup_case.strip().endswith('guard_disarm "$job"'), cleanup_case)
        # The deadline comes from the plan through stdin, never through the sudo command line.
        for sid, p in HostStressLeavesNoScenarioTraceTests.every_parameter_set():
            argv, stdin = host.build_invocation(plan("host.stress", sid, p, {"transport": "ssh", "host": p["host"]}), "run")
            self.assertEqual(argv[-3:], ["sudo", "bash", "-s"])
            self.assertEqual(stdin.split(b"\n")[1], b"expire_after=1800 guard_hold=960")

    def test_cpu_job_is_undone_by_the_worker_when_cleanup_never_comes(self) -> None:
        import os
        import shutil
        import subprocess
        import tempfile
        import time

        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Path(tmp)
            (sandbox / "legacy").mkdir()
            env = node_guard_fakes.install(sandbox)
            cpu = ("cpu", "192.168.122.14", "1", "300")
            state = sandbox / "state"
            result = remote(sandbox, "run", *cpu)
            self.assertEqual(result.returncode, 0, result.stderr)
            pgid = int((state / "burnin.pgid").read_text())
            try:
                self.assertEqual(len(node_guard_fakes.timers(env)), 1)
                self.assertEqual(node_guard_fakes.armed_seconds(env, "burnin"), [1800])
                # The node's journal sees a unit named like any unnamed systemd-run, with no description.
                for start in node_guard_fakes.starts(env, "burnin"):
                    self.assertRegex(start["unit"], r"^run-r[0-9a-f]{32}$")
                    self.assertEqual(start["description"], "")
                # The guard is armed before the burner starts: its undo exists when the group does.
                undo = state / "burnin.undo"
                self.assertTrue(node_guard_fakes.launches(env, "burnin"))
                # A reboot ends the burner by itself; what it leaves (pgid and log files) goes at the next boot.
                rule = Path(node_guard_fakes.unit_record(env, "burnin")[2])
                self.assertEqual(rule.parent, sandbox / "tmpfiles.d")
                self.assertEqual(rule.read_text().splitlines(), [
                    f"r! {state / 'burnin.pgid'}", f"r! {state / 'burnin.log'}",
                    f"r! {undo}", f"r! {state / 'burnin.unit'}", f"r! {rule}",
                ])
                spare = sandbox / "undo-copy"
                shutil.copy(undo, spare)
                # cleanup never comes: the timer elapses and the worker undoes the job by itself.
                fired = node_guard_fakes.fire(env, "burnin")
                self.assertEqual(fired.returncode, 0, fired.stderr)
                time.sleep(0.5)
                with self.assertRaises(ProcessLookupError):
                    os.killpg(pgid, 0)
                self.assertEqual(sorted(x.name for x in state.iterdir()), ["burnin.expired"])
                self.assertEqual(list((sandbox / "tmpfiles.d").iterdir()), [])
                self.assertEqual(remote(sandbox, "recovery", *cpu).returncode, 0)
                # The undo is idempotent: run again (twice) it finds nothing and changes nothing.
                for _ in range(2):
                    again = subprocess.run(["/bin/bash", str(spare)], env=dict(os.environ, **env), capture_output=True, timeout=60)
                    self.assertEqual(again.returncode, 0, again.stderr)
                    self.assertEqual(sorted(x.name for x in state.iterdir()), ["burnin.expired"])
            finally:
                try:
                    os.killpg(pgid, 9)
                except ProcessLookupError:
                    pass
            # cleanup comes late: nothing left to undo, it says when the worker did it and leaves nothing.
            late = remote(sandbox, "cleanup", *cpu)
            self.assertEqual(late.returncode, 0, late.stderr)
            self.assertIn(b"the node undid the change by itself at ", late.stderr)
            self.assertEqual(sorted(x.name for x in state.iterdir()), [])
            self.assertEqual(node_guard_fakes.timers(env), [])

    def test_cpu_job_cleanup_cancels_the_timer_and_adds_no_restart_on_time(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Path(tmp)
            (sandbox / "legacy").mkdir()
            env = node_guard_fakes.install(sandbox)
            cpu = ("cpu", "192.168.122.14", "1", "300")
            self.assertEqual(remote(sandbox, "run", *cpu).returncode, 0)
            done = remote(sandbox, "cleanup", *cpu)
            self.assertEqual(done.returncode, 0, done.stderr)
            # On time (far from the deadline) cleanup does not restart the timer, it only cancels it;
            # nothing on the worker reports an expiry because none happened.
            self.assertEqual(node_guard_fakes.armed_seconds(env, "burnin"), [1800])
            self.assertEqual(node_guard_fakes.timers(env), [])
            self.assertNotIn(b"by itself", done.stderr)
            self.assertEqual(sorted(x.name for x in (sandbox / "state").iterdir()), [])
            self.assertEqual(list((sandbox / "tmpfiles.d").iterdir()), [])
            self.assertEqual(remote(sandbox, "recovery", *cpu).returncode, 0)

    def test_run_alone_refuses_a_job_that_is_still_running(self) -> None:
        # A negated command in the middle of a line never stopped the script under set -e, so fio and
        # pressure runs called on their own started a second job beside a running one (preflight caught
        # it only because there the negation is the last command). A live pid file now stops run
        # before anything is armed or started.
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Path(tmp)
            (sandbox / "legacy").mkdir()
            (sandbox / "target").mkdir()
            fake = sandbox / "fake-bin"
            fake.mkdir()
            for tool in ("fio", "stress-ng"):
                (fake / tool).write_text("#!/usr/bin/env bash\nexit 0\n")
                (fake / tool).chmod(0o755)
            env = node_guard_fakes.install(sandbox)
            (sandbox / "state").mkdir()
            running = subprocess.Popen(["sleep", "60"])
            try:
                for job, args in (("soak", ("pressure", "192.168.122.11", "1", "1", "1M", "5")),
                                  ("iobench", ("fio", "192.168.122.14", str(sandbox / "target"), "1", "5", "10"))):
                    with self.subTest(job=job):
                        pidfile = sandbox / "state" / f"{job}.pid"
                        pidfile.write_text(f"{running.pid}\n")
                        result = remote(sandbox, "run", *args, path=f"{fake}")
                        self.assertNotEqual(result.returncode, 0)
                        self.assertEqual(pidfile.read_text(), f"{running.pid}\n")
                        self.assertEqual(node_guard_fakes.armed_seconds(env, job), [])
                        self.assertFalse((sandbox / "target" / f".{job}.dat").exists())
                        pidfile.unlink()
                self.assertIsNone(running.poll())
            finally:
                running.kill()
                running.wait(timeout=10)
            # The refusal is the run's own, not a missing tool: with no job running the same run arms and starts.
            result = remote(sandbox, "run", "pressure", "192.168.122.11", "1", "1", "1M", "5", path=f"{fake}")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(node_guard_fakes.armed_seconds(env, "soak"), [1800])
            self.assertEqual(remote(sandbox, "cleanup", "pressure", "192.168.122.11", "1", "1", "1M", "5").returncode, 0)

    def test_pid_undo_leaves_a_process_that_took_over_the_number(self) -> None:
        # A long wait or a reboot can hand the job's pid to another process: the undo checks the
        # command name before it kills, then removes the job's files either way.
        import os
        import subprocess
        import tempfile

        source = host.REMOTE.decode()
        undo_pid = "undo_pid() {" + source.split("undo_pid() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Path(tmp)
            pidfile, logfile = sandbox / "soak.pid", sandbox / "soak.log"
            bystander = subprocess.Popen(["sleep", "60"])
            try:
                pidfile.write_text(f"{bystander.pid}\n")
                logfile.write_text("x")
                body = subprocess.run(
                    ["bash", "-c", undo_pid + 'undo_pid "$1" stress-ng "$2"', "-", str(pidfile), str(logfile)],
                    capture_output=True, text=True, timeout=30, check=True,
                ).stdout
                for _ in range(2):
                    ran = subprocess.run(["bash", "-c", "set -euo pipefail\n" + body], capture_output=True, timeout=60)
                    self.assertEqual(ran.returncode, 0, ran.stderr)
                self.assertIsNone(bystander.poll())
                self.assertFalse(pidfile.exists())
                self.assertFalse(logfile.exists())
            finally:
                bystander.kill()
                bystander.wait(timeout=10)
        del os


if __name__ == "__main__":
    unittest.main()


class OraclePreflightReasonTests(unittest.TestCase):
    """2026-10-22 F35-P: USERS had 267MB free against minimum_free_mb 1024 and the
    run said only "profile preflight failed: no detail"."""

    # v4 C6b: the body asks through the operator client (db_operator_fakes), no longer kubectl exec sqlplus.
    FAKE_ORACLE = r'''#!/usr/bin/env bash
sql=$(cat)
case "$sql" in
  *"name='recyclebin'"*) echo on ;;
  *"rownum <="*) echo "$FAKE_MINIMUM" ;;
  *dba_recyclebin*) echo 0 ;;
  *listagg*) printf 'C:P:ID=PK_ACCOUNTS\nI:ID=PK_ACCOUNTS\n' ;;
  *dba_free_space*) echo "$FAKE_FREE_MB" ;;
  *"from dba_tables where owner"*) echo 1 ;;
  *) echo "unexpected sql" >&2; exit 9 ;;
esac
'''

    def preflight(self, free_mb: int, recyclebin: str = "on") -> subprocess.CompletedProcess:
        common = sys.modules["executor_common"]
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "fake_oracle.sh"
            fake.write_text(self.FAKE_ORACLE.replace("echo on ;;", f"echo {recyclebin} ;;"))
            fake.chmod(0o755)
            env = {**db_operator_fakes.install(Path(tmp), oracle=f"import os, sys\nos.execv({str(fake)!r}, [{str(fake)!r}])\n"),
                   "FAKE_MINIMUM": "5", "FAKE_FREE_MB": str(free_mb)}
            return subprocess.run(
                ["bash", "-s", "--", "preflight", "rca-testbed-banking", "testbed-oracle-0",
                 "FREEPDB1", "BANKING", "ACCOUNTS", "ID=PK_ACCOUNTS", "P:ID=PK_ACCOUNTS", "5", "1024"],
                input=common.with_failure_trace(ddl.ORACLE_DROP_REMOTE),
                capture_output=True, env=env, timeout=60,
            )

    def test_short_tablespace_is_named_with_both_numbers(self) -> None:
        result = self.preflight(267)
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            b"tablespace of BANKING.ACCOUNTS has 267MB free, preflight needs at least 1024MB",
            result.stderr,
        )

    def test_enough_space_passes_quietly(self) -> None:
        result = self.preflight(1786)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stderr, b"")

    def test_any_other_failed_check_names_its_source_line(self) -> None:
        result = self.preflight(1786, recyclebin="off")
        self.assertEqual(result.returncode, 1)
        lines = ddl.ORACLE_DROP_REMOTE.decode().splitlines()
        line_no = next(i for i, text in enumerate(lines, 1) if "name='recyclebin'" in text)
        self.assertIn(f"failed at line {line_no}: ".encode(), result.stderr)
        self.assertIn(b"recyclebin", result.stderr)


class OperatorClientTests(unittest.TestCase):
    """profiles/db_operator_session.py (v4 C6b): the client that replaced sqlplus and mysql inside the pod.

    The bodies feed it the SQL*Plus scripts they always fed sqlplus; it must split and run them the way
    SQL*Plus does and print what `set pages 0 heading off feedback off` printed. Real Oracle Free 23,
    MySQL 8.0 and PostgreSQL 16 runs on 104 are in the C6b report."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.client = load("db_operator_session")

    def _fake_oracle(self, tmp: Path, failing: str = "nope") -> dict[str, str]:
        (tmp / "oracledb.py").write_text(f"""
import json
from pathlib import Path
LOG = Path({str(tmp / "ora.log")!r})
class DatabaseError(Exception): pass
class Cursor:
    def __init__(self): self.description = None; self.rows = []
    def execute(self, sql):
        LOG.open("a").write(json.dumps({{"sql": sql}}) + "\\n")
        if {failing!r} in sql: raise DatabaseError("ORA-00942: table or view does not exist\\nHelp: x")
        if sql.lower().startswith("select"):
            self.description = [("a",), ("b",)]; self.rows = [(1, None), (2.0, "x y"), (1.5, "z")]
        else:
            self.description = None; self.rows = []
    def fetchall(self): return self.rows
class Conn:
    def __init__(self, **kw): LOG.open("a").write(json.dumps({{"connect": {{k: v for k, v in kw.items() if k != "password"}}}}) + "\\n")
    def cursor(self): return Cursor()
    def commit(self): LOG.open("a").write('{{"tx": "commit"}}\\n')
    def rollback(self): LOG.open("a").write('{{"tx": "rollback"}}\\n')
    def close(self): pass
    module = None
def connect(**kw): return Conn(**kw)
""")
        return {**os.environ, "PYTHONPATH": str(tmp), "ODB_USER": "SYSTEM", "ODB_PASSWORD": "pw-x",
                "ODB_DSN": "h:1/FREEPDB1", "CLIENT_PROGRAM": "SQLcl", "CLIENT_MACHINE": "ops-bastion",
                "CLIENT_OSUSER": "dba", "CLIENT_DRIVER": "jdbcthin : 23.7.0.25.1"}

    def _oracle(self, tmp: Path, script: str, **kw) -> tuple[subprocess.CompletedProcess, list[dict]]:
        import json as _json
        env = self._fake_oracle(tmp, **kw)
        result = subprocess.run([sys.executable, str(PROFILES / "db_operator_session.py"), "oracle"],
                                input=script.encode(), env=env, capture_output=True, timeout=30)
        log = [_json.loads(line) for line in (tmp / "ora.log").read_text().splitlines()]
        return result, log

    def test_statements_split_the_way_sqlplus_splits_them(self) -> None:
        script = ("set pages 0 lines 400 feedback off heading off trimspool on\nwhenever sqlerror exit failure\n"
                  "alter session set container=FREEPDB1;\nselect a\n  from b;\n"
                  "begin\n  for c in (select 1 from dual) loop null; end loop;\nend;\n/\n"
                  "-- a comment\nupdate t set x = 1;\ncommit;\nexit;\n")
        items = list(self.client.oracle_statements(script))
        self.assertEqual(items, [
            ("cmd", "set pages 0 lines 400 feedback off heading off trimspool on"),
            ("cmd", "whenever sqlerror exit failure"),
            ("sql", "alter session set container=FREEPDB1"),
            ("sql", "select a\n  from b"),
            ("sql", "begin\n  for c in (select 1 from dual) loop null; end loop;\nend;"),
            ("sql", "update t set x = 1"),
            ("sql", "commit"),
            ("cmd", "exit"),
        ])

    def test_output_identity_and_transactions_follow_sqlplus(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            result, log = self._oracle(Path(raw), "set pages 0 feedback off heading off\n"
                                       "alter session set container=FREEPDB1;\nselect a, b from t;\nexit;\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            # One line per row, columns joined by one space, NULL empty, an integral float as an integer.
            self.assertEqual(result.stdout.decode(), "1 \n2 x y\n1.5 z\n")
            connect = log[0]["connect"]
            self.assertEqual((connect["user"], connect["dsn"], connect["program"], connect["machine"],
                              connect["osuser"], connect["driver_name"]),
                             ("SYSTEM", "h:1/FREEPDB1", "SQLcl", "ops-bastion", "dba", "jdbcthin : 23.7.0.25.1"))
            self.assertNotIn("password", connect)
            # The container switch is not sent: the listener service already names the PDB.
            self.assertEqual([e["sql"] for e in log if "sql" in e], ["select a, b from t"])
            self.assertEqual(log[-1], {"tx": "commit"})

    def test_errors_continue_or_stop_as_whenever_sqlerror_says(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            result, log = self._oracle(Path(raw), "select * from nope;\nselect 7 from dual;\n")
            # No WHENEVER: SQL*Plus prints the error and goes on, exit 0.
            self.assertEqual(result.returncode, 0)
            self.assertIn(b"ORA-00942", result.stdout)
            self.assertNotIn(b"Help:", result.stdout)
            self.assertEqual(len([e for e in log if "sql" in e]), 2)
        with tempfile.TemporaryDirectory() as raw:
            result, log = self._oracle(Path(raw), "whenever sqlerror exit failure rollback\n"
                                       "update a set x=1;\nselect * from nope;\nselect 8 from dual;\n")
            self.assertEqual(result.returncode, 1)
            self.assertIn(b"ORA-00942", result.stdout)
            self.assertIn(b"ORA-00942", result.stderr)
            self.assertEqual([e["sql"] for e in log if "sql" in e], ["update a set x=1", "select * from nope"])
            self.assertEqual(log[-1], {"tx": "rollback"})

    def _fake_mysql(self, tmp: Path) -> dict[str, str]:
        (tmp / "pymysql").mkdir()
        (tmp / "pymysql" / "__init__.py").write_text(f"""
import json
from pathlib import Path
LOG = Path({str(tmp / "my.log")!r})
class MySQLError(Exception): pass
class Cursor:
    def __init__(self): self.description = None; self.sets = []; self.i = 0; self.rows = []
    def execute(self, sql):
        LOG.open("a").write(json.dumps({{"sql": sql}}) + "\\n")
        if "nope" in sql: raise MySQLError(1146, "Table 'fooddelivery.nope' doesn't exist")
        self.sets = [[(1, None, "a\\tb")], [("root@%",)]]; self._load()
    def _load(self):
        self.rows = self.sets[self.i]; self.description = [("x",)]
    def __iter__(self): return iter(self.rows)
    def nextset(self):
        self.i += 1
        if self.i >= len(self.sets): return None
        self._load(); return True
class Conn:
    def __init__(self, **kw): self.kw = kw; self._connect_attrs = {{"_client_name": "pymysql"}}
    def connect(self): LOG.open("a").write(json.dumps({{"attrs": self._connect_attrs, "user": self.kw["user"], "db": self.kw["database"]}}) + "\\n")
    def cursor(self, kind=None): return Cursor()
    def close(self): pass
def connect(**kw): return Conn(**kw)
""")
        (tmp / "pymysql" / "cursors.py").write_text("class Cursor: pass\nclass SSCursor: pass\n")
        (tmp / "pymysql" / "constants.py").write_text("class CLIENT:\n    MULTI_STATEMENTS = 65536\n")
        (tmp / "pymysql" / "connections.py").write_text("import socket\n")
        return {**os.environ, "PYTHONPATH": str(tmp), "MDB_HOST": "h", "MDB_PORT": "1", "MDB_USER": "root",
                "MDB_PASSWORD": "pw-x", "DB": "fooddelivery"}

    def test_mysql_prints_like_mysql_batch_mode_and_names_the_tool(self) -> None:
        import json as _json
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            env = self._fake_mysql(tmp)
            ok = subprocess.run([sys.executable, str(PROFILES / "db_operator_session.py"), "mysql"],
                                env={**env, "SQL": "SELECT 1, NULL, 'a\tb'; SELECT CURRENT_USER();"},
                                capture_output=True, timeout=30)
            self.assertEqual(ok.returncode, 0, ok.stderr)
            self.assertEqual(ok.stdout.decode(), "1\tNULL\ta\\tb\nroot@%\n")
            bad = subprocess.run([sys.executable, str(PROFILES / "db_operator_session.py"), "mysql"],
                                 env={**env, "SQL": "SELECT * FROM nope;"}, capture_output=True, timeout=30)
            self.assertEqual(bad.returncode, 1)
            self.assertEqual(bad.stderr.decode().strip(), "ERROR 1146: Table 'fooddelivery.nope' doesn't exist")
            first = _json.loads((tmp / "my.log").read_text().splitlines()[0])
            self.assertEqual(first["attrs"]["program_name"], "mysql")
            self.assertEqual(first["attrs"]["_client_name"], "libmysql")
            self.assertEqual((first["user"], first["db"]), ("root", "fooddelivery"))

    def test_paced_connections_fix_a_small_window_before_connecting(self) -> None:
        import socket
        import threading
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        threading.Thread(target=lambda: server.accept()[0].close(), daemon=True).start()
        sock = self.client._small_buffer_connection(server.getsockname(), timeout=5)
        try:
            # Linux doubles the value it is given; autotuning is off once it is set.
            self.assertLessEqual(sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF), 2 * self.client.PACED_RCVBUF)
            self.assertLessEqual(self.client.PACED_RCVBUF, 16384)
        finally:
            sock.close()
            server.close()

    def _pace(self, tmp: Path, rate: str, sample: int = 100):
        (tmp / "rate").write_text(rate)
        pace = self.client._Pace(str(tmp), sample)
        pace.took(b"x" * 99 + b"\n")  # the sample: 100 bytes, one row
        self.assertEqual((tmp / "sample").read_text(), "100 1\n")
        return pace

    def _waits(self, pace, seconds: float) -> bool:
        """True while wait() is still blocked after SECONDS."""
        import threading
        done = threading.Event()
        thread = threading.Thread(target=lambda: (pace.wait(), done.set()), daemon=True)
        thread.start()
        return not done.wait(seconds)

    def test_pace_holds_the_reader_at_the_rate_it_is_told(self) -> None:
        # C6b test audit 다6: the MySQL export queue (F60-H, F53-H, F36-P) stays on the server for export_seconds only
        # because pod-exec reads its stream at the rate _Pace allows, steered through files (mdb_pod_pace).
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            # "0": after the sample the reader waits for a plan, and "max" releases it at once.
            pace = self._pace(tmp, "0")
            self.assertTrue(self._waits(pace, 1.2))
            (tmp / "rate").write_text("max")
            time.sleep(1.0)
            self.assertFalse(self._waits(pace, 0.1))
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            # A number is bytes a second: 3000 bytes at 2000 B/s take 1.5 s.
            pace = self._pace(tmp, "2000")
            pace.wait()
            started = time.monotonic()
            pace.took(b"y" * 3000)
            pace.wait()
            self.assertGreaterEqual(time.monotonic() - started, 1.4)
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            # A plan "ROWS HELD DEADLINE": the rate is what is left on the server over the time left. 11 rows of the
            # 100 bytes seen, 0 held, 10 s left: (1100 - 100) / 10 = 100 B/s.
            pace = self._pace(tmp, "0")
            (tmp / "plan").write_text(f"11 0 {time.time() + 10}")
            pace.checked = 0.0
            pace._refresh()
            self.assertAlmostEqual(pace.rate, 100.0, delta=2.0)
            # A deadline that has passed lets everything through.
            (tmp / "plan").write_text(f"11 0 {time.time() - 1}")
            pace.checked = 0.0
            pace._refresh()
            self.assertEqual(pace.rate, float("inf"))
        # Without a pace directory nothing is held (the migration and other pod-execs).
        free = self.client._Pace("", 100)
        free.took(b"z" * 10_000)
        self.assertFalse(self._waits(free, 0.1))
        # And the paced export does get one: mdb_pod_spawn hands pod-exec its LOG.pace directory, rate "0" at first.
        lib = db_operator_remote.bash_functions().decode()
        spawn = lib[lib.index("mdb_pod_spawn() {"):lib.index("\n}\n", lib.index("mdb_pod_spawn() {"))]
        self.assertIn('mkdir -p -- "$4.pace" && printf \'0\\n\' >"$4.pace/rate"', spawn)
        self.assertIn('KEXEC_PACE_DIR="$4.pace"', spawn)

    def test_pg_relay_carries_one_connection_both_ways(self) -> None:
        import socket
        import threading
        upstream = socket.socket()
        upstream.bind(("127.0.0.1", 0))
        upstream.listen(1)

        def echo() -> None:
            conn, _ = upstream.accept()
            data = conn.recv(100)
            conn.sendall(data.upper() * 3000)
            conn.close()

        threading.Thread(target=echo, daemon=True).start()
        with tempfile.TemporaryDirectory() as raw:
            env = {**os.environ, "PG_UPSTREAM_HOST": "127.0.0.1", "PG_UPSTREAM_PORT": str(upstream.getsockname()[1])}
            relay = subprocess.Popen([sys.executable, str(PROFILES / "db_operator_session.py"), "pg-relay", raw, "5432"],
                                     env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            path = Path(raw) / ".s.PGSQL.5432"
            for _ in range(50):
                if path.exists():
                    break
                time.sleep(0.1)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(path))
            client.sendall(b"hello")
            received = b""
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                received += chunk
            client.close()
            self.assertEqual(received, b"HELLO" * 3000)
            self.assertEqual(relay.wait(timeout=10), 0)
            self.assertFalse(path.exists(), "the socket is removed once its one connection is taken")
        upstream.close()
