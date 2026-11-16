"""녹화 표 자동 발견 계획 (scripts/scenarios/pg-scope-export.py, ch-scope-plan.py, v4 R1).

캡처 스크립트 전체 경로는 scripts/test-capture-eval-case.sh 가 가짜 curl, docker 로 돌린다.
여기서는 두 계획기의 규칙을 하나씩 본다.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import re
import sqlite3
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pg = _load("pg_scope_export", "pg-scope-export.py")
tsc = _load("threshold_slice_check", "threshold-slice-check.py")
ch = _load("ch_scope_plan", "ch-scope-plan.py")
PG_CFG = pg.load(ROOT / "pg-scope.json")
CH_CFG = ch.load(ROOT / "ch-scope.json")
V3_CH = ["otel_traces_local", "lucida_logs_local", "lucida_events_local", "dpm_session_local",
         "dpm_topsql_local", "kcm_events_local", "process_snapshot", "trace_error_chains_local",
         "trace_path_signatures_local", "syslog_local", "host_connections", "process_meta",
         "event_cluster_projection_local"]


def pg_catalog(tables, columns=None, fks=None, *, configured=True, schemas=None, denied=None, deps=None):
    """tables: {name: bytes} 또는 {name: (bytes, relkind)}. configured=True 면 설정의 표가 모두 원천에 있다.

    schemas: {스키마: USAGE 여부}, denied: {"스키마.이름" 또는 public 이름: relkind}, deps: [(dep 스키마, dep 표,
    ref 스키마, ref 표)]. 셋 다 없으면 권한 질의가 없던 옛 카탈로그 모양이다(2026-11-13 CAPFIX).
    """
    rows = []
    names = dict(tables)
    if configured:
        for t in pg.explicit_slice_tables(PG_CFG) + list(PG_CFG["drop_data"]) + list(PG_CFG["snap_tables"]):
            names.setdefault(t, 8192)
    for t, v in names.items():
        size, kind = v if isinstance(v, tuple) else (v, "r")
        schema, _, name = t.rpartition(".")
        rows.append(f"{schema or 'public'}\t{name}\t{kind}\t{size}\t1\t1\t0")
    cols = dict(columns or {})
    if configured:
        # 설정이 부르는 열을 모두 둔다(자기참조 포함). 시험이 지우고 싶은 열은 columns 로 덮는다.
        need = {}
        for t, s in PG_CFG["slice_roots"].items():
            need.setdefault(t, []).extend(["id", s["time_column"], s["self_reference"]])
        for t, s in PG_CFG["slice_children"].items():
            need.setdefault(t, []).append(s["column"])
        for t, s in PG_CFG["slice_grandchildren"].items():
            need.setdefault(t, []).append(s["column"])
            need.setdefault(s["parent"], []).append(s["parent_key"])
        for t, c in list(PG_CFG["slice_by_time"].items()) + list(PG_CFG["slice_by_epoch_ms"].items()):
            need.setdefault(t, []).append(c)
        for t, sp in PG_CFG["slice_by_validity"].items():
            need.setdefault(t, []).extend(sp["key"] + [sp["version"], sp["valid_from"], sp["active"]])
        for sp in PG_CFG["slice_roots"].values():
            for ls in sp.get("lineage_seed", []):
                if ls["table"] in need:  # 설정 표인 씨앗 표(incident_cluster_links)는 열을 다 갖는다
                    need[ls["table"]].extend([ls["column"], ls["via"]])
        for t, cs in need.items():
            cols.setdefault(t, [(c, "timestamp with time zone" if c.endswith("_at") else "uuid") for c in cs])
    # 열 항목은 (이름, 타입) 또는 (이름, 타입, notnull, 기본값, check, unique, generated)(4회차 8열 카탈로그).
    col_lines = []
    for t, cs in cols.items():
        for c in cs:
            if len(c) == 2:
                col_lines.append(f"{t}\t{c[0]}\t{c[1]}")
            else:
                n, ty, nn, df, ck, uq, gen = c
                col_lines.append("\t".join([t, n, ty, "t" if nn else "f", df, "t" if ck else "f", "t" if uq else "f", gen]))
    fk_lines = ["\t".join(f) for f in (fks or [])]  # (자식, 열, 부모, 부모 열[, 제약 이름, 정의])
    texts = {"tables": "\n".join(rows), "columns": "\n".join(col_lines), "fks": "\n".join(fk_lines)}
    if schemas is not None:
        texts["schemas"] = "\n".join(f"{k}\t{'t' if v else 'f'}" for k, v in schemas.items())
    if denied is not None:
        # 값은 relkind 하나(옛 3열) 또는 (relkind, 이유, 뿌리 부모 키)(6열).
        lines = []
        for k, v in denied.items():
            sch, name = (k.rpartition('.')[0] or 'public'), k.rpartition('.')[2]
            if isinstance(v, tuple):
                kind, why, root = v
                rs, rn = ((root.rpartition('.')[0] or 'public'), root.rpartition('.')[2]) if root else ("", "")
                lines.append(f"{sch}\t{name}\t{kind}\t{why}\t{rs}\t{rn}")
            else:
                lines.append(f"{sch}\t{name}\t{v}")
        texts["denied"] = "\n".join(lines)
    if deps is not None:
        texts["deps"] = "\n".join("\t".join(d) for d in deps)
    return pg.parse_catalog(texts)


class PgPlanTest(unittest.TestCase):
    def plan(self, *a, cfg=None, **k):
        return pg.plan_scope(cfg or PG_CFG, pg_catalog(*a, **k))

    def test_without_unknown_tables_the_v3_output_is_unchanged(self):
        # 계보, 씨앗 표가 없는 옛 원천 모양이면 v3 와 같은 글자를 낸다(씨앗 열이 없어 씨앗을 쓰지 않음).
        plan = self.plan({}, columns={"incident_cluster_links": [("incident_id", "uuid")]})
        self.assertEqual(pg.ordered_slices(PG_CFG, plan), pg.ordered_slices(PG_CFG))
        self.assertEqual(pg.emit_exclude_args(PG_CFG, plan), pg.emit_exclude_args(PG_CFG))
        self.assertEqual(pg.emit_restore_sql(PG_CFG, plan), pg.emit_restore_sql(PG_CFG))
        self.assertEqual(plan["auto_slices"], [])
        self.assertEqual(plan["conflicts"], [])

    def test_child_of_a_slice_table_is_cut_through_its_parent(self):
        plan = self.plan({"incident_snapshots": 4096},
                         columns={"incident_snapshots": [("incident_id", "uuid"), ("captured_at", "timestamp with time zone")]},
                         fks=[("incident_snapshots", "incident_id", "incidents", "id")])
        e = plan["tables"]["incident_snapshots"]
        self.assertEqual((e["scope"], e["rule"]), ("slice", "fk_closure"))
        q = dict(pg.ordered_slices(PG_CFG, plan))["incident_snapshots"]
        self.assertIn('c."incident_id" IN (SELECT _p0."id" FROM (', q)
        self.assertIn('public."incidents"', q)
        self.assertIn("--exclude-table-data=public.incident_snapshots", pg.emit_exclude_args(PG_CFG, plan))
        restore = pg.emit_restore_sql(PG_CFG, plan).splitlines()
        self.assertLess(restore.index("\\copy incidents FROM 'incidents.csv' WITH (FORMAT csv, HEADER true)"),
                        restore.index("\\copy incident_snapshots FROM 'incident_snapshots.csv' WITH (FORMAT csv, HEADER true)"))

    def test_two_parents_each_must_be_kept_and_rows_without_any_parent_are_left_out(self):
        plan = self.plan({"event_cluster_successors": 4096},
                         fks=[("event_cluster_successors", "predecessor_cluster_id", "event_clusters", "id"),
                              ("event_cluster_successors", "successor_cluster_id", "event_clusters", "id")])
        q = dict(pg.ordered_slices(PG_CFG, plan))["event_cluster_successors"]
        self.assertIn('(c."predecessor_cluster_id" IS NOT NULL OR c."successor_cluster_id" IS NOT NULL)', q)
        self.assertIn('(c."predecessor_cluster_id" IS NULL OR c."predecessor_cluster_id" IN (', q)
        self.assertIn('(c."successor_cluster_id" IS NULL OR c."successor_cluster_id" IN (', q)

    def test_closure_follows_children_of_discovered_children(self):
        plan = self.plan({"a_child": 4096, "a_grandchild": 4096},
                         fks=[("a_child", "incident_id", "incidents", "id"),
                              ("a_grandchild", "child_id", "a_child", "id")])
        self.assertEqual(plan["auto_slices"], ["a_child", "a_grandchild"])
        q = dict(pg.ordered_slices(PG_CFG, plan))["a_grandchild"]
        self.assertIn('public."a_child"', q)

    def test_history_table_is_cut_by_its_event_time_but_latest_table_with_same_column_stays_whole(self):
        plan = self.plan({"target_config_history": (4096, "p"), "target_config_latest": 4096,
                          "collector_x_inv": 4096},
                         columns={"target_config_history": [("changed_at", "timestamp with time zone")],
                                  "target_config_latest": [("changed_at", "timestamp with time zone")],
                                  "collector_x_inv": [("collected_at", "timestamp with time zone")]})
        h = plan["tables"]["target_config_history"]
        self.assertEqual((h["scope"], h["rule"], h["time_column"]), ("slice", "history", "changed_at"))
        self.assertEqual(plan["tables"]["target_config_latest"]["scope"], "snap")
        self.assertEqual(plan["tables"]["collector_x_inv"]["scope"], "snap")
        # 분할 표는 잎 분할의 데이터까지 빼야 한다.
        self.assertIn("--exclude-table-data-and-children=public.target_config_history",
                      pg.emit_exclude_args(PG_CFG, plan))

    def test_history_time_column_preference_follows_the_config_order(self):
        plan = self.plan({"x_history": 4096},
                         columns={"x_history": [("created_at", "timestamp with time zone"),
                                                ("occurred_at", "timestamp with time zone")]})
        self.assertEqual(plan["tables"]["x_history"]["time_column"], "occurred_at")

    def test_history_table_without_a_known_event_column_stays_whole(self):
        plan = self.plan({"collect_assignment_history": 4096},
                         columns={"collect_assignment_history": [("from_ts", "timestamp with time zone")]})
        self.assertEqual(plan["tables"]["collect_assignment_history"]["scope"], "snap")

    def test_unknown_big_reference_table_keeps_schema_only_but_explicit_snap_is_kept_whole(self):
        cap = PG_CFG["auto"]["snap_max_bytes"]
        plan = self.plan({"big_unknown": cap + 1, "small_unknown": cap,
                          "collector_sms_process_snapshot": cap * 10})
        self.assertEqual(plan["tables"]["big_unknown"]["scope"], "drop")
        self.assertEqual(plan["tables"]["big_unknown"]["rule"], "over_cap")
        self.assertEqual(plan["over_cap"], ["big_unknown"])
        self.assertEqual(plan["tables"]["small_unknown"]["scope"], "snap")
        self.assertEqual(plan["tables"]["collector_sms_process_snapshot"]["rule"], "config")
        args = pg.emit_exclude_args(PG_CFG, plan)
        self.assertIn("--exclude-table-data=public.big_unknown", args)
        self.assertNotIn("collector_sms_process_snapshot", args)
        self.assertNotIn("small_unknown", args)

    def test_trainer_work_table_is_dropped_by_config_not_over_cap(self):
        # 지휘 세션 결정: 학습 작업용 표라 크기가 오르내려도 녹화 전 점검이 막지 않게 drop 으로 명시.
        cap = PG_CFG["auto"]["snap_max_bytes"]
        plan = self.plan({"ai_trainer_stream_rows": cap + 50 * 2**20})
        e = plan["tables"]["ai_trainer_stream_rows"]
        self.assertEqual((e["scope"], e["rule"]), ("drop", "config"))
        self.assertEqual(plan["over_cap"], [])
        self.assertIn("--exclude-table-data=public.ai_trainer_stream_rows", pg.emit_exclude_args(PG_CFG, plan))

    def test_whole_dump_budget_counts_only_whole_tables(self):
        budget = PG_CFG["auto"]["snap_budget_bytes"]
        under = self.plan({"collector_sms_process_snapshot": budget - 10**6, "incident_snapshots": budget},
                          fks=[("incident_snapshots", "incident_id", "incidents", "id")])
        load = pg.snap_load(PG_CFG, under)
        self.assertFalse(load["over_budget"])
        over = self.plan({"collector_sms_process_snapshot": budget + 1})
        load = pg.plan_summary(over, PG_CFG)
        self.assertTrue(load["over_budget"])
        self.assertEqual(load["largest_snap"][0], {"table": "collector_sms_process_snapshot", "bytes": budget + 1})

    def test_missing_self_reference_column_drops_the_recursion_only_for_that_root(self):
        cat = pg_catalog({})
        cat["columns"]["incidents"] = ["id", "created_at"]
        plan = pg.plan_scope(PG_CFG, cat)
        self.assertEqual(plan["missing_columns"], ["incidents.superseded_by"])
        q = dict(pg.ordered_slices(PG_CFG, plan))
        self.assertNotIn("superseded_by", q["incidents"])
        self.assertNotIn("superseded_by", q["incident_members"])
        self.assertIn("merged_into", q["event_clusters"])
        self.assertIn("WITH RECURSIVE", q["event_clusters"])

    def test_configured_table_absent_at_source_is_not_exported(self):
        cat = pg_catalog({})
        del cat["tables"]["db_sql_plan"]
        plan = pg.plan_scope(PG_CFG, cat)
        self.assertIn("db_sql_plan", plan["absent"])
        self.assertNotIn("db_sql_plan", [t for t, _ in pg.ordered_slices(PG_CFG, plan)])

    def test_missing_configured_column_is_a_conflict(self):
        cat = pg_catalog({})
        cat["columns"]["alert_history"] = ["id"]
        plan = pg.plan_scope(PG_CFG, cat)
        self.assertTrue(any(c["table"] == "alert_history" and "started_at" in c["problem"] for c in plan["conflicts"]))

    def test_snap_tables_pointing_at_a_slice_table_are_kept_whole_and_listed(self):
        # 상태, 대기열, 지식 표는 사람이 snap_tables 에 이유와 함께 적어 통째로 둔다(R1 수정 1회차).
        state = ["alert_active", "alarm_ack_state", "incident_snapshot_queue", "incident_judge_retries", "kedb_wiki_case"]
        for t in state:
            self.assertIn(t, PG_CFG["snap_tables"])
        fks = [("alert_active", "alert_history_id", "alert_history", "id"),
               ("alarm_ack_state", "alert_history_id", "alert_history", "id"),
               ("incident_snapshot_queue", "incident_id", "incidents", "id"),
               ("incident_judge_retries", "cluster_id", "event_clusters", "id"),
               ("kedb_wiki_case", "incident_id", "incidents", "id"),
               ("kedb_pending", "incident_id", "incidents", "id"),
               ("alarm_ack_log", "alert_history_id", "alert_history", "id")]
        plan = self.plan({"alarm_ack_log": 4096}, fks=fks)
        for t in state:
            self.assertEqual((plan["tables"][t]["scope"], plan["tables"][t]["rule"]), ("snap", "config"), t)
            self.assertNotIn(t, pg.emit_exclude_args(PG_CFG, plan))
        self.assertEqual(sorted(x["table"] for x in plan["snap_fk_to_slice"]), sorted(state))
        self.assertEqual(plan["conflicts"], [])
        # 사건 기록 표는 그대로 부모에 묶여 잘린다.
        self.assertEqual(plan["tables"]["alarm_ack_log"]["rule"], "fk_closure")

    def test_explicit_snap_pointing_at_a_slice_table_without_reason_is_a_conflict_but_drop_is_not(self):
        # 설정 밖 표는 fk_closure 가 가져가므로 충돌은 이유 없이 snap 이 된 경우(public 밖 스키마)에서 생긴다.
        # 2026-11-13 CAPFIX: 담는 public 밖 스키마는 capture_schemas 에 적힌 것뿐이라 그 스키마로 본다.
        cat = pg_catalog({"polestar_notification.pinned": 4096})
        cat["fks"]["polestar_notification.pinned"] = [{"columns": ["incident_id"], "parent": "incidents", "parent_columns": ["id"]}]
        cat["fks"]["kedb_pending"] = [{"columns": ["incident_id"], "parent": "incidents", "parent_columns": ["id"]}]
        plan = pg.plan_scope(PG_CFG, cat)
        self.assertEqual([c["table"] for c in plan["conflicts"]], ["polestar_notification.pinned"])

    def test_lineage_keeps_successors_created_after_the_window(self):
        lineage = {"incident_event_cluster_successors": 4096, "event_cluster_successors": 4096}
        cols = {"incident_event_cluster_successors": [("predecessor_incident_id", "uuid"), ("successor_incident_id", "uuid"),
                                                      ("successor_cluster_id", "uuid")],
                "event_cluster_successors": [("predecessor_cluster_id", "uuid"), ("successor_cluster_id", "uuid")]}
        fks = [("incident_event_cluster_successors", "predecessor_incident_id", "incidents", "id"),
               ("incident_event_cluster_successors", "successor_incident_id", "incidents", "id"),
               ("incident_event_cluster_successors", "successor_cluster_id", "event_clusters", "id"),
               ("event_cluster_successors", "predecessor_cluster_id", "event_clusters", "id"),
               ("event_cluster_successors", "successor_cluster_id", "event_clusters", "id")]
        cat = pg_catalog(lineage, columns=cols, fks=fks)
        cat["columns"]["incidents"] = ["id", "created_at"]  # superseded_by 가 지워진 판
        plan = pg.plan_scope(PG_CFG, cat)
        self.assertEqual(plan["lineage"], ["event_cluster_successors", "incident_cluster_links",
                                           "incident_event_cluster_successors"])
        q = dict(pg.ordered_slices(PG_CFG, plan))
        inc = q["incidents"]
        self.assertIn("WITH RECURSIVE", inc)
        self.assertNotIn("superseded_by", inc)
        self.assertIn('SELECT l."successor_incident_id" AS id FROM public."incident_event_cluster_successors" l '
                      'WHERE l."predecessor_incident_id" = k.id', inc)
        # 자식 표도 같은 _keep 을 쓴다(대체 인시던트의 멤버, 타임라인도 실린다).
        self.assertIn("incident_event_cluster_successors", q["incident_members"])
        ec = q["event_clusters"]
        self.assertIn('l."successor_cluster_id" AS id FROM public."event_cluster_successors"', ec)
        self.assertIn("merged_into", ec)
        # 인시던트 계보 간선의 대체 군집도 씨앗에 든다.
        self.assertIn('SELECT l."successor_cluster_id" FROM public."incident_event_cluster_successors" l', ec)
        self.assertEqual(plan["tables"]["incident_event_cluster_successors"]["rule"], "fk_closure")

    def test_clusters_linked_to_kept_incidents_are_seeded(self):
        # 확인 필요 11: 창 안 인시던트가 창 전 군집에 링크되면 그 군집이 없어 restore.sql 이 FK 위반으로 멈췄다.
        cat = pg_catalog({}, columns={"incident_cluster_links": [("incident_id", "uuid"), ("cluster_id", "uuid")]})
        plan = pg.plan_scope(PG_CFG, cat)
        self.assertIn("incident_cluster_links", plan["lineage"])
        q = dict(pg.ordered_slices(PG_CFG, plan))
        self.assertIn('SELECT l."cluster_id" FROM public."incident_cluster_links" l\n'
                      '   WHERE l."incident_id" IN (', q["event_clusters"])
        # 군집 자식 표도 같은 씨앗을 쓴다(링크된 군집의 멤버 등도 실린다).
        self.assertIn('public."incident_cluster_links"', q["event_cluster_members"])
        # 인시던트 자식인 링크 표 자체의 고르는 방식은 그대로다.
        self.assertNotIn('l."cluster_id"', q["incident_cluster_links"].split("SELECT c.*")[1])

    def test_lineage_table_with_missing_column_is_not_used_and_is_reported(self):
        cat = pg_catalog({"event_cluster_successors": 4096},
                         columns={"event_cluster_successors": [("predecessor_cluster_id", "uuid")],
                                  "incident_cluster_links": [("incident_id", "uuid")]})
        plan = pg.plan_scope(PG_CFG, cat)
        self.assertNotIn("event_cluster_successors", plan["lineage"])
        self.assertIn("event_cluster_successors.successor_cluster_id", plan["missing_columns"])
        self.assertEqual(dict(pg.ordered_slices(PG_CFG, plan))["event_clusters"],
                         dict(pg.ordered_slices(PG_CFG))["event_clusters"])

    def test_configured_slice_table_absent_at_source_is_reported_but_drop_is_not(self):
        cat = pg_catalog({})
        del cat["tables"]["alert_history"]
        del cat["tables"]["kedb_pending"]
        plan = pg.plan_scope(PG_CFG, cat)
        self.assertEqual(plan["absent_slice"], ["alert_history"])
        self.assertIn("kedb_pending", plan["absent"])
        self.assertEqual(pg.plan_summary(plan, PG_CFG)["absent_slice"], ["alert_history"])

    def test_auto_child_with_self_reference_is_a_conflict(self):
        plan = self.plan({"rca_notes": 4096},
                         fks=[("rca_notes", "incident_id", "incidents", "id"), ("rca_notes", "parent_id", "rca_notes", "id")])
        self.assertNotEqual(plan["tables"]["rca_notes"]["rule"], "fk_closure")
        self.assertIn("rca_notes", [c["table"] for c in plan["conflicts"]])

    def test_other_schema_over_cap_is_excluded_by_its_own_name(self):
        # 2026-11-13 CAPFIX: 담는 스키마는 capture_schemas 에 적힌 것뿐이라 그 목록의 public 밖 스키마로 본다.
        cap = PG_CFG["auto"]["snap_max_bytes"]
        plan = self.plan({"polestar_notification.big": cap + 1})
        self.assertEqual(plan["tables"]["polestar_notification.big"]["scope"], "drop")
        args = pg.emit_exclude_args(PG_CFG, plan).splitlines()
        self.assertIn("--exclude-table-data=polestar_notification.big", args)
        self.assertFalse(any("public.polestar_notification" in a for a in args))

    def test_other_schemas_are_kept_whole(self):
        plan = self.plan({"polestar_notification.audit_marks": 4096})
        self.assertEqual(plan["tables"]["polestar_notification.audit_marks"]["scope"], "snap")
        self.assertEqual(plan["excluded"], [])

    def test_catalog_header_lines_are_ignored(self):
        cat = pg.parse_catalog({
            "tables": "nspname\trelname\trelkind\tbytes\test_rows\tn_live_tup\tn_dead_tup\npublic\tt1\tr\t10\t1\t1\t0",
            "columns": "relname\tattname\tformat_type\nt1\tcreated_at\ttimestamp with time zone",
            "fks": "relname\tstring_agg\trelname\tstring_agg"})
        self.assertEqual(list(cat["tables"]), ["t1"])
        self.assertEqual(cat["timecols"], {"t1": ["created_at"]})
        self.assertEqual(cat["fks"], {})

    def test_catalog_sql_reads_only_the_catalog(self):
        sql = pg.emit_catalog_sql("/out/catalog/postgres")
        for name in ("tables", "columns", "fks", "schemas", "denied", "deps"):
            self.assertIn(f"\\o /out/catalog/postgres/{name}.tsv", sql)
        for q in pg.CATALOG_QUERIES.values():
            self.assertNotRegex(q, r"FROM public\.")


class PgAccessTest(unittest.TestCase):
    """캡처 사용자가 못 읽는 것과 담지 않기로 한 스키마(2026-11-13 CAPFIX, G2 F49-H 의 schema security 실패).

    가짜 pg_dump 로 캡처 전체를 돌리는 시험은 scripts/test-capture-eval-case.sh 에 있다.
    """
    # 119 실측 모양(2026-11-13): security 는 USAGE 없음, public 표 4개와 시퀀스 2개는 SELECT 없음,
    # public 표의 트리거가 polestar_notification 함수를 부른다.
    SCHEMAS_119 = {"polestar_notification": True, "public": True, "security": False}
    DENIED_119 = {"collect_control_position_changes": "r", "omp_confine_pending": "r",
                  "automation_notification_cancellation_progress": "r",
                  "automation_notification_cancellation_install": "r",
                  "collect_desired_inputs_input_seq_seq": "S", "automation_notification_cancellations_id_seq": "S",
                  "security.password_rotation_ledger": "r", "security.password_rotation_ledger_id_seq": "S"}
    DEPS_119 = [("public", "software_execution_runs", "polestar_notification", ""),
                ("public", "automation_notification_cancellation_progress", "public", "automation_notification_cancellations")]
    TABLES_119 = {"security.password_rotation_ledger": 8192, "collect_control_position_changes": 24576,
                  "omp_confine_pending": 0, "automation_notification_cancellation_progress": 8192,
                  "automation_notification_cancellation_install": 16384, "software_execution_runs": 8192,
                  "automation_notification_cancellations": 8192, "collect_desired_inputs": 8192}

    def plan(self, tables, cfg=None, **k):
        return pg.plan_scope(cfg or PG_CFG, pg_catalog(tables, **k))

    def plan_119(self, cfg=None):
        return self.plan(self.TABLES_119, cfg, schemas=self.SCHEMAS_119, denied=self.DENIED_119, deps=self.DEPS_119)

    def test_119_shape_dumps_without_touching_unreadable_objects(self):
        plan = self.plan_119()
        self.assertEqual(plan["conflicts"], [])
        self.assertEqual(plan["excluded_schemas"], ["security"])
        self.assertTrue(all(e["acknowledged"] for e in plan["excluded"]), plan["excluded"])
        self.assertEqual([(e["object"], e["kind"], e["reason"]) for e in plan["excluded"]], [
            ("security", "schema", "no_usage"),
            ("automation_notification_cancellation_install", "table", "no_select"),
            ("automation_notification_cancellation_progress", "table", "no_select"),
            ("automation_notification_cancellations_id_seq", "sequence_data", "no_select"),
            ("collect_control_position_changes", "table", "no_select"),
            ("collect_desired_inputs_input_seq_seq", "sequence_data", "no_select"),
            ("omp_confine_pending", "table", "no_select")])
        args = pg.emit_exclude_args(PG_CFG, plan).splitlines()
        self.assertEqual(args[len(args) - 7:], [
            "--exclude-schema=security",
            "--exclude-table=public.automation_notification_cancellation_install",
            "--exclude-table=public.automation_notification_cancellation_progress",
            "--exclude-table-data=public.automation_notification_cancellations_id_seq",
            "--exclude-table=public.collect_control_position_changes",
            "--exclude-table-data=public.collect_desired_inputs_input_seq_seq",
            "--exclude-table=public.omp_confine_pending"])
        # 앞부분(설정과 자동 발견의 데이터 빼기)은 권한 질의와 스키마 목록이 없던 판과 같다.
        before = copy.deepcopy(PG_CFG)
        del before["capture_schemas"]
        old_plan = self.plan(self.TABLES_119, before)
        self.assertEqual(old_plan["excluded"], [])
        old = pg.emit_exclude_args(before, old_plan).splitlines()
        self.assertEqual(args[:len(args) - 7], old)
        self.assertNotIn("--exclude-table-data=security.password_rotation_ledger", args)
        self.assertNotIn("--exclude-table-data=public.collect_control_position_changes", args)
        self.assertEqual(plan["tables"]["security.password_rotation_ledger"]["scope"], "excluded")
        self.assertEqual(plan["tables"]["collect_control_position_changes"]["rule"], "no_select")
        self.assertEqual(plan["tables"]["automation_notification_cancellations"]["scope"], "snap")
        self.assertEqual(pg.ordered_slices(PG_CFG, plan), pg.ordered_slices(before, old_plan))
        s = pg.plan_summary(plan, PG_CFG)
        self.assertEqual(s["by_scope"]["excluded"], 5)
        self.assertEqual(s["excluded"], plan["excluded"])

    def test_unreadable_schema_is_dropped_whole_even_if_not_acknowledged(self):
        cfg = copy.deepcopy(PG_CFG)
        cfg["excluded_schemas"] = {}
        plan = self.plan({"vault.keys": 4096}, cfg, schemas={"public": True, "vault": False})
        self.assertEqual(plan["excluded"], [{"object": "vault", "kind": "schema", "reason": "no_usage",
                                             "acknowledged": False, "note": None}])
        self.assertEqual(plan["tables"]["vault.keys"], {"table": "vault.keys", "bytes": 4096, "relkind": "r",
                                                        "scope": "excluded", "rule": "schema"})
        args = pg.emit_exclude_args(cfg, plan).splitlines()
        self.assertIn("--exclude-schema=vault", args)
        self.assertFalse(any("vault.keys" in a for a in args))  # 스키마째 빼므로 표 인자는 필요 없다

    def test_readable_schema_not_in_the_capture_list_is_left_out(self):
        # 인증, 비밀 계열 스키마가 새로 생겨 캡처 사용자가 읽을 수 있어도 사람이 담기로 정하기 전에는 싣지 않는다.
        plan = self.plan({"auth_tokens.sessions": 4096}, schemas={"public": True, "auth_tokens": True})
        self.assertEqual([(e["object"], e["reason"], e["acknowledged"]) for e in plan["excluded"]],
                         [("auth_tokens", "not_listed", False)])
        self.assertEqual(plan["tables"]["auth_tokens.sessions"]["scope"], "excluded")
        self.assertIn("--exclude-schema=auth_tokens", pg.emit_exclude_args(PG_CFG, plan).splitlines())

    def test_without_a_capture_list_every_readable_schema_is_kept(self):
        cfg = copy.deepcopy(PG_CFG)
        del cfg["capture_schemas"]
        plan = self.plan({"audit.marks": 4096}, cfg, schemas={"public": True, "audit": True, "vault": False})
        self.assertEqual([e["object"] for e in plan["excluded"]], ["vault"])
        self.assertEqual(plan["tables"]["audit.marks"]["scope"], "snap")

    def test_listed_schema_without_usage_is_a_conflict(self):
        plan = self.plan({}, schemas={"public": True, "polestar_notification": False})
        self.assertIn("polestar_notification", [c["table"] for c in plan["conflicts"]])

    def test_unreadable_table_is_excluded_whole_and_partitioned_with_its_children(self):
        plan = self.plan({"locked": 4096, "locked_parted": (4096, "p")}, schemas={"public": True},
                         denied={"locked": "r", "locked_parted": "p", "locked_leaf_202611": "r"})
        args = pg.emit_exclude_args(PG_CFG, plan).splitlines()
        self.assertIn("--exclude-table=public.locked", args)
        self.assertIn("--exclude-table-and-children=public.locked_parted", args)
        self.assertIn("--exclude-table=public.locked_leaf_202611", args)  # 읽을 수 있는 부모 밑의 못 읽는 잎
        self.assertNotIn("--exclude-table-data=public.locked", args)
        self.assertEqual(plan["tables"]["locked"]["scope"], "excluded")

    def test_unreadable_sequence_keeps_its_definition_and_drops_only_its_value(self):
        plan = self.plan({"owner_tab": 4096}, schemas={"public": True}, denied={"owner_tab_id_seq": "S"})
        args = pg.emit_exclude_args(PG_CFG, plan).splitlines()
        self.assertIn("--exclude-table-data=public.owner_tab_id_seq", args)
        self.assertNotIn("--exclude-table=public.owner_tab_id_seq", args)
        self.assertEqual(plan["tables"]["owner_tab"]["scope"], "snap")

    def test_configured_slice_table_that_cannot_be_read_is_a_conflict_and_not_exported(self):
        plan = self.plan({}, schemas={"public": True}, denied={"alert_history": "r"})
        self.assertIn("alert_history", [c["table"] for c in plan["conflicts"]])
        self.assertNotIn("alert_history", [t for t, _ in pg.ordered_slices(PG_CFG, plan)])
        self.assertNotIn("\\copy alert_history ", pg.emit_restore_sql(PG_CFG, plan))
        args = pg.emit_exclude_args(PG_CFG, plan).splitlines()
        self.assertIn("--exclude-table=public.alert_history", args)
        self.assertNotIn("--exclude-table-data=public.alert_history", args)

    def test_kept_objects_that_depend_on_a_dropped_schema_or_table_are_conflicts(self):
        plan = self.plan({"runs": 4096, "locked": 4096, "locked_child": 4096, "view_owner": 4096},
                         schemas={"public": True, "polestar_notification": True, "vault": False},
                         denied={"locked": "r", "locked_child": "r"},
                         deps=[("public", "runs", "vault", ""),               # 트리거가 빠지는 스키마의 함수를 부름
                               ("public", "view_owner", "public", "locked"),  # 담는 표가 빠지는 표를 가리킴
                               ("public", "locked_child", "public", "locked"),  # 빠지는 표끼리는 괜찮다
                               ("vault", "", "public", "runs"),               # 빠지는 쪽이 담는 표에 기댐: 괜찮다
                               ("public", "runs", "polestar_notification", "")])  # 담는 스키마: 괜찮다
        self.assertEqual(sorted((c["table"], c["problem"]) for c in plan["conflicts"]), [
            ("runs", "녹화에서 빠지는 스키마 vault 에 기대어 복원 때 생성이 깨진다"),
            ("view_owner", "녹화에서 빠지는 표 locked 에 기대어 복원 때 생성이 깨진다")])

    def test_old_catalog_without_access_queries_plans_as_before(self):
        plan = self.plan({"vault.keys": 4096, "polestar_notification.f": 4096}, cfg=None)
        self.assertEqual(plan["tables"]["polestar_notification.f"]["scope"], "snap")
        # 권한을 모르면 읽을 수 있다고 보지만, 목록 밖 스키마는 그래도 담지 않는다.
        self.assertEqual([(e["object"], e["reason"]) for e in plan["excluded"]], [("vault", "not_listed")])

    def test_access_catalog_header_lines_and_unknown_kinds_are_ignored(self):
        cat = pg.parse_catalog({
            "tables": "public\tt1\tr\t10\t1\t1\t0",
            "schemas": "nspname\thas_schema_privilege\npublic\tt\nsecurity\tf",
            "denied": "nspname\trelname\trelkind\npublic\tv1\tv\npublic\ts1\tS\nsecurity\tl\tr",
            "deps": "dep_schema\tdep_rel\tref_schema\tref_rel\npublic\tt1\tsecurity\t\nrelname\tstring_agg\trelname\tstring_agg"})
        self.assertEqual(cat["schemas"], {"public": True, "security": False})
        self.assertEqual(cat["denied"], {"s1": "S", "security.l": "r"})
        self.assertEqual(cat["deps"], [{"schema": "public", "relation": "t1", "ref_schema": "security", "ref_relation": ""}])

    def test_privileges_are_judged_for_the_given_role(self):
        mine = pg.catalog_queries()
        theirs = pg.catalog_queries("lucida")
        self.assertIn("has_schema_privilege(current_user, n.oid, 'USAGE')", mine["schemas"])
        self.assertIn("has_schema_privilege('lucida', n.oid, 'USAGE')", theirs["schemas"])
        self.assertIn("has_table_privilege('lucida', c.oid, 'SELECT')", theirs["denied"])
        self.assertEqual(mine, pg.CATALOG_QUERIES)
        self.assertEqual({k: mine[k] for k in ("tables", "columns", "fks")},
                         {k: theirs[k] for k in ("tables", "columns", "fks")})
        with self.assertRaises(ValueError):
            pg.catalog_queries("x'); DROP TABLE t; --")

    def test_names_that_need_quoting_are_quoted_for_pg_dump(self):
        plan = self.plan({}, schemas={"public": True, "Vault": False}, denied={"Mixed": "r"})
        args = pg.emit_exclude_args(PG_CFG, plan).splitlines()
        self.assertIn('--exclude-schema="Vault"', args)
        self.assertIn('--exclude-table=public."Mixed"', args)

    def test_row_security_table_is_excluded_whole_even_though_it_can_be_selected(self):
        # 2회차(리뷰 하): RLS 표는 SELECT 가 있어도 pg_dump 가 "query would be affected by row-level security
        # policy" 로 전체를 멈춘다(PG 17 실측, 2026-11-16).
        plan = self.plan({"rls_t": 4096}, schemas={"public": True}, denied={"rls_t": ("r", "row_security", "")})
        self.assertEqual([(e["object"], e["kind"], e["reason"]) for e in plan["excluded"]],
                         [("rls_t", "table", "row_security")])
        self.assertEqual(plan["tables"]["rls_t"]["scope"], "excluded")
        self.assertIn("--exclude-table=public.rls_t", pg.emit_exclude_args(PG_CFG, plan).splitlines())

    def test_unreadable_leaf_is_acknowledged_through_its_partition_root(self):
        cfg = copy.deepcopy(PG_CFG)
        cfg["excluded_relations"] = {**cfg["excluded_relations"], "parted": "잎마다 권한이 빠지는 이유"}
        denied = {"parted_202611": ("r", "no_select", "parted"), "other_202611": ("r", "no_select", "other")}
        plan = self.plan({"parted": (4096, "p"), "other": (4096, "p")}, cfg, schemas={"public": True}, denied=denied)
        got = {e["object"]: e for e in plan["excluded"]}
        self.assertEqual((got["parted_202611"]["partition_root"], got["parted_202611"]["acknowledged"],
                          got["parted_202611"]["note"]), ("parted", True, "잎마다 권한이 빠지는 이유"))
        self.assertEqual((got["other_202611"]["partition_root"], got["other_202611"]["acknowledged"]), ("other", False))
        args = pg.emit_exclude_args(cfg, plan).splitlines()
        self.assertIn("--exclude-table=public.parted_202611", args)
        self.assertEqual(plan["tables"]["parted"]["scope"], "snap")  # 읽을 수 있는 부모는 담는다

    def test_six_column_denied_rows_parse_and_three_column_rows_mean_no_select(self):
        cat = pg.parse_catalog({
            "tables": "public\tt1\tr\t10\t1\t1\t0",
            "denied": "public\told\tr\npublic\trls\tr\trow_security\t\t\n"
                      "audit\tleaf_1\tr\tno_select\taudit\tparted\npublic\tleaf_2\tr\tno_select\tpublic\tp2"})
        self.assertEqual(cat["denied"], {"old": "r", "rls": "r", "audit.leaf_1": "r", "leaf_2": "r"})
        self.assertEqual(cat["denied_reason"], {"old": "no_select", "rls": "row_security",
                                                "audit.leaf_1": "no_select", "leaf_2": "no_select"})
        self.assertEqual(cat["denied_root"], {"audit.leaf_1": "audit.parted", "leaf_2": "p2"})

    def test_denied_query_names_row_security_and_the_partition_root(self):
        q = pg.catalog_queries("lucida")["denied"]
        for want in ("relrowsecurity", "relforcerowsecurity", "rolbypassrls", "pg_has_role('lucida', c.relowner",
                     "pg_partition_root(c.oid)", "'row_security'"):
            self.assertIn(want, q)
        # 질의의 뜻은 로컬 PG 17 에서 확인했다(CAPFIX.md 2회차: RLS 표 rls_t 는 row_security, 소유한 RLS 표는 빠지지
        # 않음, 못 읽는 잎 parted_b 는 뿌리 parted). 여기서는 그 조건 줄이 그대로인지를 본다.
        self.assertIn("  AND (NOT has_table_privilege('lucida', c.oid, 'SELECT')\n"
                      "       OR (c.relkind IN ('r', 'p') AND c.relrowsecurity\n"
                      "           AND NOT coalesce((SELECT a.rolsuper OR a.rolbypassrls FROM pg_roles a WHERE "
                      "a.rolname = 'lucida'), false)\n"
                      "           AND (c.relforcerowsecurity OR NOT pg_has_role('lucida', c.relowner, 'USAGE'))))\n", q)

    def test_capture_warns_on_stderr_for_each_left_out_object(self):
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "tables.tsv").write_text("public\tt1\tr\t10\t1\t1\t0\nsecurity\tl\tr\t10\t1\t1\t0\n")
            (Path(d) / "columns.tsv").write_text("")
            (Path(d) / "fks.tsv").write_text("")
            (Path(d) / "schemas.tsv").write_text("public\tt\nsecurity\tf\n")
            (Path(d) / "denied.tsv").write_text("public\tlocked\tr\nsecurity\tl\tr\n")
            (Path(d) / "deps.tsv").write_text("")
            r = subprocess.run(["python3", str(ROOT / "pg-scope-export.py"), "--catalog", d, "--emit", "plan-summary"],
                               capture_output=True, text=True, check=True)
        self.assertEqual(r.stderr.splitlines(), [
            "[WARN] PostgreSQL schema security left out of the dump: no_usage",
            "[WARN] PostgreSQL table locked left out of the dump: no_select (not acknowledged in pg-scope.json)"])
        self.assertEqual([e["object"] for e in json.loads(r.stdout)["excluded"]], ["security", "locked"])

    def test_config_reasons_are_written_for_every_acknowledged_object(self):
        for k in ("capture_schemas", "excluded_schemas", "excluded_relations"):
            self.assertTrue(PG_CFG[k], k)
            for name, why in PG_CFG[k].items():
                self.assertTrue(isinstance(why, str) and len(why) > 10, (k, name))
        self.assertIn("public", PG_CFG["capture_schemas"])
        self.assertFalse(set(PG_CFG["capture_schemas"]) & set(PG_CFG["excluded_schemas"]))


class PgSecretTest(unittest.TestCase):
    """public 안 비밀 계열 표는 행을 담지 않고 스키마만(2026-11-16 사용자 결정, CAPFIX 2회차)."""
    # 119 카탈로그(2026-11-16)에서 secret_detect 꼴에 걸린 표 57개. 값은 보지 않았다.
    CANDIDATES_119 = (
        "alarm_linkages api_keys automation_agent_worker_credentials automation_agents automation_workers "
        "collect_credential_auth_blocks collect_credential_auth_failures collect_credential_holdings "
        "collect_credential_issuances collect_credential_participants collect_credential_reports "
        "collect_credential_rotation_changes collect_credential_rotation_states collect_credential_rotations "
        "collect_credential_safety_revisions collect_installer_worker_approvals collect_worker_join_tokens "
        "collect_worker_removals collect_worker_seal_keys collect_workers collector_sms_agent_keys "
        "collector_sms_enrollment_tokens collectors credential_least_privilege_checks credential_profile_targets "
        "credential_profiles credential_roles de_destinations dpm_agent_enrollment_tokens dpm_agent_keys "
        "dpm_custom_sql_variable_keys dpm_zone_probe_credentials external_systems information_provisions "
        "inspection_credential_target_map inspection_credentials kcm_agent_identity kcm_enrollment_codes "
        "migration_sources network_script_config_versions nms_discovery_profiles nms_worker_result_keys "
        "notification_channels omp_effect_audit_keys omp_target_endpoint_keys system_mail_settings "
        "system_map_tile_settings system_sms_settings two_factor_challenges user_invitations user_otp_secrets "
        "user_password_history users workload_connection_profiles workload_db_connection_profiles "
        "workload_notification_destinations wpm_profile_credentials "
        # 3회차: 사용자가 든 낱말(seal, token, key)로 꼴을 넓혀 더 걸린 표
        "alarm_severity_levels automation_host_inventory_runs software_omp_executions peer_link_peers").split()

    def plan(self, tables, cfg=None, **k):
        return pg.plan_scope(cfg or PG_CFG, pg_catalog(tables, **k))

    def test_listed_secret_table_keeps_its_schema_and_drops_its_rows(self):
        plan = self.plan({"api_keys": 4096, "user_otp_secrets": (4096, "p")},
                         columns={"api_keys": [("key_hash", "bytea")]})
        self.assertEqual((plan["tables"]["api_keys"]["scope"], plan["tables"]["api_keys"]["rule"]), ("drop", "secret"))
        got = {e["object"]: e for e in plan["excluded"]}
        self.assertEqual({k: got["api_keys"][k] for k in ("kind", "reason", "acknowledged")},
                         {"kind": "table_data", "reason": "secret", "acknowledged": True})
        self.assertEqual(got["api_keys"]["note"], "비밀 계열, 사용자 결정 11-16: " + PG_CFG["secret_tables"]["api_keys"])
        self.assertIn("key_hash", got["api_keys"]["note"])
        args = pg.emit_exclude_args(PG_CFG, plan).splitlines()
        self.assertIn("--exclude-table-data=public.api_keys", args)
        self.assertIn("--exclude-table-data-and-children=public.user_otp_secrets", args)
        self.assertNotIn("--exclude-table=public.api_keys", args)  # 표 정의는 남는다
        self.assertEqual(len(args), len(set(args)))
        self.assertEqual(pg.plan_summary(plan, PG_CFG)["excluded"], plan["excluded"])

    def test_unlisted_table_with_a_secret_column_is_emptied_and_unacknowledged(self):
        plan = self.plan({"vault_material_v2": 4096, "plain": 4096},
                         columns={"vault_material_v2": [("token_hash", "text")], "plain": [("note", "text")]})
        e = next(x for x in plan["excluded"] if x["object"] == "vault_material_v2")
        self.assertEqual((e["reason"], e["acknowledged"]), ("secret_detected", False))
        self.assertEqual(e["note"], "비밀 계열, 사용자 결정 11-16, 목록에 없음. 이름이나 열 꼴: 열 token_hash:text")
        self.assertEqual(plan["tables"]["vault_material_v2"]["rule"], "secret_detected")
        self.assertEqual(plan["tables"]["plain"]["scope"], "snap")
        self.assertIn("--exclude-table-data=public.vault_material_v2", pg.emit_exclude_args(PG_CFG, plan).splitlines())

    def test_unlisted_table_with_a_secret_name_is_emptied(self):
        plan = self.plan({"tenant_api_key_rotation": 4096})
        e = next(x for x in plan["excluded"] if x["object"] == "tenant_api_key_rotation")
        self.assertEqual((e["reason"], e["note"]),
                         ("secret_detected", "비밀 계열, 사용자 결정 11-16, 목록에 없음. 이름이나 열 꼴: 표 이름"))

    def test_seal_and_token_table_names_are_caught_by_name(self):
        # 3회차: 사용자가 든 낱말 seal, token 은 표 이름에서도 잡는다(열이 비밀 꼴이 아니어도).
        plan = self.plan({"worker_seal_material": 4096, "tenant_tokens": 4096, "plain_runs": 4096})
        got = {e["object"]: e["note"] for e in plan["excluded"] if e["reason"] == "secret_detected"}
        self.assertEqual(sorted(got), ["tenant_tokens", "worker_seal_material"])
        self.assertTrue(all(v.endswith("이름이나 열 꼴: 표 이름") for v in got.values()))

    def test_reviewed_keep_and_other_drop_tables_are_not_flagged(self):
        plan = self.plan({"users": 4096, "chat_sessions": 4096},
                         columns={"users": [("password_hash", "character varying(255)")],
                                  "chat_sessions": [("secret_note", "text")]})
        # 4회차: 표째 비우지는 않고(행은 담음) 비밀 열 값만 가린다(PgMaskTest).
        self.assertEqual(plan["tables"]["users"]["scope"], "masked")
        self.assertEqual((plan["tables"]["chat_sessions"]["scope"], plan["tables"]["chat_sessions"]["rule"]),
                         ("drop", "config"))
        self.assertEqual([e for e in plan["excluded"] if e["kind"] == "table_data"], [])

    def test_columns_that_do_not_hold_values_are_not_secret(self):
        plan = self.plan({"system_security_settings": 4096, "kcm_resources": 4096, "network_x": 4096},
                         columns={"system_security_settings": [("password_digit_count", "integer"),
                                                               ("password_expire_days", "integer")],
                                  "kcm_resources": [("secret_refs", "text[]")],
                                  "network_x": [("secret_masked", "boolean"), ("token_count", "integer")]})
        self.assertEqual([e for e in plan["excluded"] if e["kind"] == "table_data"], [])

    def test_fk_to_an_emptied_secret_table(self):
        plan = self.plan({"inspection_credentials": 4096, "inspection_jobs": 4096},
                         fks=[("inspection_jobs", "credential_id", "inspection_credentials", "id"),
                              ("alert_history", "credential_id", "inspection_credentials", "id")])
        self.assertIn({"table": "inspection_jobs", "parents": ["inspection_credentials"]}, plan["fk_to_secret"])
        self.assertIn(("alert_history", "slice 표가 비우는 비밀 계열 표를 외래 키로 가리킨다"),
                      [(c["table"], c["problem"]) for c in plan["conflicts"]])

    def test_without_a_catalog_listed_secret_tables_are_still_emptied(self):
        args = pg.emit_exclude_args(PG_CFG).splitlines()
        for t in PG_CFG["secret_tables"]:
            self.assertIn(f"--exclude-table-data=public.{t}", args)
        for t in PG_CFG["drop_data"]:
            self.assertIn(f"--exclude-table-data=public.{t}", args)

    def test_every_secret_candidate_on_119_has_a_decision(self):
        decided = set(PG_CFG["secret_tables"]) | set(PG_CFG["secret_reviewed_keep"]) | set(PG_CFG["drop_data"])
        self.assertEqual(sorted(set(self.CANDIDATES_119) - decided), [])
        self.assertEqual(len(self.CANDIDATES_119), 61)
        for k in ("secret_tables", "secret_reviewed_keep"):
            for name, why in PG_CFG[k].items():
                self.assertTrue(isinstance(why, str) and len(why) > 15, (k, name))
        self.assertFalse(set(PG_CFG["secret_tables"]) & set(PG_CFG["secret_reviewed_keep"]))
        self.assertFalse(set(PG_CFG["secret_tables"]) & set(PG_CFG["drop_data"]))
        for t in ("api_keys", "user_otp_secrets", "credential_profiles", "collect_worker_seal_keys", "dpm_agent_keys"):
            self.assertIn(t, PG_CFG["secret_tables"])  # 사용자 결정에 든 예

    def test_secret_detect_patterns_catch_the_119_column_shapes(self):
        det = PG_CFG["secret_detect"]
        col, ty = re.compile(det["column_name"], re.I), re.compile(det["column_type"], re.I)
        for c, t in (("key_hash", "bytea"), ("credential_hash", "character(64)"), ("auth_encrypted", "text"),
                     ("secret_encrypted", "text"), ("password_hash", "character varying(255)"), ("public_key", "bytea"),
                     ("private_encrypted", "text"), ("config_encrypted", "jsonb"), ("sql_ciphertext", "text"),
                     ("secrets", "bytea"), ("aes_key_encrypted", "text"), ("code_hash", "bytea"),
                     ("token", "text"), ("refresh_token", "text"), ("seal_material", "bytea"), ("signing_key", "text"),
                     ("access_key", "character varying(64)"), ("run_token", "text")):
            self.assertTrue(col.search(c) and ty.search(t), (c, t))
        for c, t in (("password_digit_count", "integer"), ("secret_refs", "text[]"), ("method_otp", "boolean"),
                     ("content_hash", "text"), ("resource_key", "text"), ("idempotency_key", "text"),
                     ("execution_token", "uuid"), ("sealed_at", "timestamp with time zone"), ("partition_key", "text"),
                     ("token_count", "integer")):
            self.assertFalse(col.search(c) and ty.search(t), (c, t))


def col(name, ty, notnull=False, default="", check=False, unique=False, generated=""):
    return (name, ty, notnull, default, check, unique, generated)


class PgMaskTest(unittest.TestCase):
    """행을 담는 표의 비밀 열은 값만 가린다(2026-11-16 지휘 세션 결정, CAPFIX 4회차)."""

    def plan(self, tables, cfg=None, **k):
        return pg.plan_scope(cfg or PG_CFG, pg_catalog(tables, **k))

    USERS = [col("id", "uuid", True), col("username", "character varying(64)", True),
             col("password_hash", "character varying(255)"), col("must_change_password", "boolean", True, "false")]

    def test_listed_nullable_column_is_nulled_and_the_table_goes_through_copy(self):
        cat = pg_catalog({"users": 4096, "asset_tree_groups": 4096},
                         columns={"users": self.USERS, "asset_tree_groups": [col("id", "uuid", True), col("owner_id", "uuid")]},
                         fks=[("asset_tree_groups", "owner_id", "users", "id", "asset_tree_groups_owner_fk",
                               "FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE SET NULL")])
        plan = pg.plan_scope(PG_CFG, cat)
        self.assertEqual((plan["tables"]["users"]["scope"], plan["tables"]["users"]["rule"]), ("masked", "default+masked"))
        self.assertEqual(plan["masked_tables"], ["users"])
        m = plan["masks"]["users"]
        self.assertEqual([(x["column"], x["method"], x["acknowledged"]) for x in m], [("password_hash", "null", True)])
        self.assertTrue(m[0]["note"].startswith("비밀 계열, 사용자 결정 11-16: 계정 비밀번호 해시"))
        self.assertIn("--exclude-table-data=public.users", pg.emit_exclude_args(PG_CFG, plan).splitlines())
        sql = pg.emit_export_sql(PG_CFG, "/out/postgres", plan, cat)
        self.assertIn('COPY (SELECT _m."id", _m."username", CAST(NULL AS character varying(255)) AS "password_hash", '
                      '_m."must_change_password" FROM public."users" _m) TO STDOUT WITH (FORMAT csv, HEADER true);', sql)
        self.assertNotIn('_m."password_hash"', sql)
        restore = pg.emit_restore_sql(PG_CFG, plan)
        copies = [l for l in restore.splitlines() if l.startswith("\\copy ")]
        self.assertEqual(copies[0], "\\copy users FROM 'users.csv' WITH (FORMAT csv, HEADER true)")
        self.assertIn("('asset_tree_groups', 'asset_tree_groups_owner_fk', "
                      "'FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE SET NULL')", restore)
        self.assertLess(restore.index("\\copy users"), restore.index("DO $capfix$"))
        self.assertEqual(pg.plan_summary(plan, PG_CFG)["masked_columns"],
                         [{k: m[0][k] for k in ("table", "column", "method", "acknowledged", "note")}])
        self.assertNotIn("users", [t for t, _ in pg.ordered_slices(PG_CFG, plan)])

    def test_fk_readd_is_summarised_and_recorded_even_when_nothing_fails(self):
        # 5회차: 실패해도 복원은 멈추지 않되 조용히 넘기지 않는다. 요약 WARNING 한 줄과 복원 DB 의 보고 표.
        cat = pg_catalog({"users": 4096, "asset_tree_groups": 4096},
                         columns={"users": self.USERS},
                         fks=[("asset_tree_groups", "owner_id", "users", "id", "atg_owner_fk",
                               "FOREIGN KEY (owner_id) REFERENCES users(id)")])
        plan = pg.plan_scope(PG_CFG, cat)
        restore = pg.emit_restore_sql(PG_CFG, plan)
        self.assertIn("CREATE TABLE IF NOT EXISTS _capture_restore_report", restore)
        self.assertIn("RAISE WARNING 'CAPFIX_FK_READD_SUMMARY failed=% attempted=% added=% existing=% child_missing=% %'",
                      restore)
        for item in ("fk_readd_attempted", "fk_readd_added", "fk_readd_existing", "fk_readd_child_missing",
                     "fk_readd_failed", "fk_readd_first_error"):
            self.assertIn(f"('{item}', ", restore)
        self.assertIn("\n  INSERT INTO _capture_restore_report (item, value, detail) VALUES\n", restore)
        self.assertIn("  ON CONFLICT (item) DO UPDATE SET value = EXCLUDED.value, detail = EXCLUDED.detail", restore)
        self.assertIn("failed := failed || (r.child || '.' || r.conname);", restore)
        # 이 SQL 이 실제로 도는지는 로컬 PG 17 로 확인했다(CAPFIX.md 12절: 성공 failed=0, 고아 행 하나로 failed=1).
        self.assertIn("first_err := coalesce(first_err, r.child || '.' || r.conname || ': ' || SQLERRM);", restore)
        self.assertNotIn("RAISE NOTICE 'CAPFIX_FK_READD_FAILED", restore)
        self.assertEqual(pg.plan_summary(plan, PG_CFG)["fk_readd_count"], 1)
        # 가린 표는 있는데 다시 달 FK 가 없어도 요약은 낸다(0).
        plan0 = pg.plan_scope(PG_CFG, pg_catalog({"users": 4096}, columns={"users": self.USERS}))
        r0 = pg.emit_restore_sql(PG_CFG, plan0)
        self.assertIn("FROM (SELECT NULL::text, NULL::text, NULL::text WHERE false) v(child, conname, def)", r0)
        self.assertIn("CAPFIX_FK_READD_SUMMARY", r0)
        self.assertEqual(pg.plan_summary(plan0, PG_CFG)["fk_readd_count"], 0)
        # 가린 표가 없으면 종전 글자 그대로(블록 없음).
        self.assertNotIn("CAPFIX_FK_READD_SUMMARY", pg.emit_restore_sql(PG_CFG, pg.plan_scope(PG_CFG, pg_catalog({}))))

    def test_mask_values_follow_nullability_default_and_constraints(self):
        cfg = copy.deepcopy(PG_CFG)
        cfg["secret_reviewed_keep"] = {**cfg["secret_reviewed_keep"], "t1": "시험"}
        cfg["secret_columns"] = {"t1": {"a_secret": "r", "b_secret": "r", "c_secret": "r", "d_secret": "r",
                                        "e_secret": "r", "f_secret": "r", "g_secret": {"reason": "r", "value": "'x0'"},
                                        "h_secret": "r"}}
        cols = [col("id", "integer", True), col("a_secret", "text"), col("b_secret", "text", True, "''::text"),
                col("c_secret", "text", True), col("d_secret", "character varying(80)", True, unique=True),
                col("e_secret", "integer", True), col("f_secret", "text", True, check=True),
                col("g_secret", "text", True, check=True), col("h_secret", "bytea", True)]
        plan = self.plan({"t1": 4096}, cfg, columns={"t1": cols})
        got = {m["column"]: (m["method"], m["expr"]) for m in plan["masks"]["t1"]}
        self.assertEqual(got, {
            "a_secret": ("null", "CAST(NULL AS text)"),
            "b_secret": ("default", "CAST((''::text) AS text)"),
            "c_secret": ("placeholder", "CAST('masked' AS text)"),
            "d_secret": ("placeholder_unique", "CAST('masked-' || row_number() OVER () AS character varying(80))"),
            "e_secret": ("placeholder", "CAST(NULL AS integer)"),
            "f_secret": ("placeholder", "CAST('masked' AS text)"),
            "g_secret": ("config", "CAST('x0' AS text)"),
            "h_secret": ("placeholder", "'\\x6d61736b6564'::bytea")})
        problems = sorted(c["problem"].split(":")[0] for c in plan["conflicts"] if c["table"] == "t1")
        self.assertEqual(problems, ["비밀 열 e_secret", "비밀 열 f_secret"])

    def test_unlisted_column_shape_is_masked_unacknowledged_and_keep_list_is_respected(self):
        cfg = copy.deepcopy(PG_CFG)
        cfg["secret_columns_keep"] = {**cfg["secret_columns_keep"], "t2.idp_secret_ref": "식별자"}
        cfg["secret_reviewed_keep"] = {**cfg["secret_reviewed_keep"], "t2": "담기로 한 표에 나중에 생긴 비밀 열"}
        plan = self.plan({"t2": 4096}, cfg, columns={"t2": [col("id", "integer", True), col("refresh_token", "text"),
                                                              col("idp_secret_ref", "text")]})
        m = plan["masks"]["t2"]
        self.assertEqual([(x["column"], x["acknowledged"]) for x in m], [("refresh_token", False)])
        self.assertEqual(m[0]["note"], "비밀 계열, 사용자 결정 11-16, 목록에 없음. 열 꼴에 걸림")

    def test_slice_table_secret_column_is_masked_inside_the_window_query(self):
        cfg = copy.deepcopy(PG_CFG)
        cfg["secret_columns"] = {"alert_history": {"webhook_secret": "r"}}
        cfg["secret_reviewed_keep"] = {**cfg["secret_reviewed_keep"], "alert_history": "시험"}
        cat = pg_catalog({}, columns={"alert_history": [col("id", "bigint", True), col("started_at", "timestamp with time zone", True),
                                                        col("webhook_secret", "text")]})
        plan = pg.plan_scope(cfg, cat)
        self.assertEqual(plan["tables"]["alert_history"]["scope"], "slice")
        self.assertEqual(plan["masked_tables"], [])
        sql = pg.emit_export_sql(cfg, "/out/postgres", plan, cat)
        block = sql.split("-- alert_history\n", 1)[1].split("\\o\n", 1)[0]
        self.assertIn('COPY (SELECT _m."id", _m."started_at", CAST(NULL AS text) AS "webhook_secret" FROM (\nSELECT t.* '
                      'FROM public."alert_history" t WHERE t."started_at"', block)
        self.assertIn("alert_history", pg.emit_restore_sql(cfg, plan))

    def test_a_new_table_with_a_secret_column_is_emptied_not_masked(self):
        # 담음 목록에 없는 표는 2회차 규칙대로 표째 비운다(사람이 담기로 정하기 전).
        plan = self.plan({"t3": 4096}, columns={"t3": [col("id", "integer", True), col("api_secret", "text")]})
        self.assertEqual(plan["tables"]["t3"]["rule"], "secret_detected")
        self.assertNotIn("t3", plan["masks"])

    def test_generated_columns_are_left_to_the_restore(self):
        plan_cat = pg_catalog({"users": 4096}, columns={"users": self.USERS + [col("search", "tsvector", generated="s")]})
        plan = pg.plan_scope(PG_CFG, plan_cat)
        sql = pg.emit_export_sql(PG_CFG, "/out/postgres", plan, plan_cat)
        self.assertNotIn('"search"', sql)

    def test_fk_readd_skips_children_that_are_emptied_or_not_captured(self):
        cat = pg_catalog({"users": 4096, "api_keys": 4096, "plain": 4096},
                         columns={"users": self.USERS},
                         fks=[("api_keys", "user_id", "users", "id", "api_keys_user_fk", "FOREIGN KEY (user_id) REFERENCES users(id)"),
                              ("plain", "user_id", "users", "id", "plain_user_fk", "FOREIGN KEY (user_id) REFERENCES users(id)")])
        plan = pg.plan_scope(PG_CFG, cat)
        self.assertEqual([r["table"] for r in plan["fk_readd"]], ["plain"])

    def test_old_three_column_catalog_reads_as_nullable(self):
        plan = self.plan({"users": 4096}, columns={"users": [("id", "uuid"), ("password_hash", "character varying(255)")]})
        self.assertEqual([(m["column"], m["method"]) for m in plan["masks"]["users"]], [("password_hash", "null")])

    def test_every_secret_column_shape_on_119_has_a_decision(self):
        hits_119 = ["alarm_severity_levels.color_token", "automation_host_inventory_runs.run_token",
                    "alarm_linkages.secret_encrypted", "collectors.config_encrypted", "collectors.opamp_token_hash",
                    "network_script_config_versions.content_encrypted", "software_omp_executions.run_token",
                    "peer_link_peers.api_key_env", "workload_notification_destinations.secret_encrypted",
                    "users.password_hash", "collect_workers.enroll_token_hash", "collect_workers.credential_hash",
                    "information_provisions.sql_ciphertext", "automation_workers.enroll_token_hash",
                    "automation_agents.enroll_token_hash"]
        listed = {f"{t}.{c}" for t, cs in PG_CFG["secret_columns"].items() for c in cs}
        self.assertEqual(sorted(set(hits_119) - listed - set(PG_CFG["secret_columns_keep"])), [])
        self.assertFalse(listed & set(PG_CFG["secret_columns_keep"]))
        self.assertEqual(PG_CFG["secret_columns"]["information_provisions"]["sql_ciphertext"]["value"], "'masked'")
        for t in PG_CFG["secret_columns"]:
            self.assertIn(t, PG_CFG["secret_reviewed_keep"])  # 표째 담는 인벤토리 표의 열이다

    def test_masked_tables_emit_lists_only_whole_masked_tables(self):
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "tables.tsv").write_text("public\tusers\tr\t10\t1\t1\t0\n")
            (Path(d) / "columns.tsv").write_text("users\tid\tuuid\tt\t\tf\tt\t\nusers\tpassword_hash\tcharacter varying(255)\tf\t\tf\tf\t\n")
            (Path(d) / "fks.tsv").write_text("")
            r = subprocess.run(["python3", str(ROOT / "pg-scope-export.py"), "--catalog", d, "--emit", "masked-tables"],
                               capture_output=True, text=True, check=True)
            e = subprocess.run(["python3", str(ROOT / "pg-scope-export.py"), "--catalog", d, "--emit", "export-sql"],
                               capture_output=True, text=True, check=True)
        self.assertEqual(r.stdout.split(), ["users"])
        self.assertIn('CAST(NULL AS character varying(255)) AS "password_hash"', e.stdout)


class ThresholdValiditySliceTest(unittest.TestCase):
    """trainer_trace_window_threshold 를 판 단위로 자른다(사용자 결정 Q10). 생성된 질의를 sqlite 에서 실제로 돌린다."""
    WS, WE = "2026-11-10T00:00:00Z", "2026-11-10T03:00:00Z"
    COLS = ["rule_id", "agent_id", "service", "unit", "unit_key", "window_minutes", "threshold",
            "valid_from", "version", "active"]

    def rows(self):
        def gen(agent, version, at, active, services):
            return [{"rule_id": f"{agent}:v{version}:{i}", "agent_id": agent, "service": svc, "unit": "op",
                     "unit_key": "", "window_minutes": 60, "threshold": 1.0 + version,
                     "valid_from": tsc._ts(at), "version": version, "active": int(active)}
                    for i, svc in enumerate(services)]
        return (gen("a", 1, "2026-11-09T10:00:00Z", False, ["s1", "s2", "s3"])
                + gen("a", 2, "2026-11-09T22:00:00Z", False, ["s1", "s2"])      # 창 시작 때 유효(s3 은 빠진 판)
                + gen("a", 3, "2026-11-10T01:00:00Z", False, ["s1", "s2"])      # 창 안에서 만든 판
                + gen("a", 4, "2026-11-10T06:00:00Z", True, ["s1"])             # 캡처 때 활성
                + gen("b", 1, "2026-11-08T00:00:00Z", True, ["s9"])             # 창 시작 때 유효이자 활성
                + gen("b", 0, "2026-11-01T00:00:00Z", False, ["s9"])
                + gen("c", 1, "2026-11-10T05:00:00Z", True, ["s7"])             # 창 시작 뒤에만 있는 키
                + gen("d", 1, "2026-11-09T20:00:00Z", False, ["s5"])
                + gen("d", 2, self.WS, False, ["s5"])                            # 경계: valid_from = 창 시작
                + gen("d", 3, "2026-11-10T07:00:00Z", True, ["s5"]))

    def run_slice(self, rows):
        spec = PG_CFG["slice_by_validity"]["trainer_trace_window_threshold"]
        q = pg.validity_slice_query("trainer_trace_window_threshold", spec)
        q = (q.replace("::timestamptz", "")
              .replace(":'win_start'", f"'{tsc._ts(self.WS)}'").replace(":'win_end'", f"'{tsc._ts(self.WE)}'"))
        db = sqlite3.connect(":memory:")
        db.execute("ATTACH ':memory:' AS public")
        db.execute(f"CREATE TABLE public.trainer_trace_window_threshold ({', '.join(self.COLS)})")
        db.executemany(f"INSERT INTO public.trainer_trace_window_threshold VALUES ({', '.join('?' * len(self.COLS))})",
                       [[r[c] for c in self.COLS] for r in rows])
        cur = db.execute(q)
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]

    def test_the_table_is_sliced_by_validity_and_no_longer_dumped_whole(self):
        self.assertNotIn("trainer_trace_window_threshold", PG_CFG["snap_tables"])
        self.assertIn("--exclude-table-data=public.trainer_trace_window_threshold", pg.emit_exclude_args(PG_CFG))
        self.assertIn("trainer_trace_window_threshold", [t for t, _ in pg.ordered_slices(PG_CFG)])

    def test_versions_valid_at_window_start_are_the_same_in_the_slice_and_the_source(self):
        spec = PG_CFG["slice_by_validity"]["trainer_trace_window_threshold"]
        src = self.rows()
        got = self.run_slice(src)
        self.assertEqual(tsc.valid_at_start(got, spec, self.WS), tsc.valid_at_start(src, spec, self.WS))
        self.assertEqual(tsc.keys_of(got, spec), tsc.keys_of(src, spec))
        self.assertEqual(len(tsc.keys_of(got, spec)), 4)
        versions = sorted({(r["agent_id"], r["version"]) for r in got})
        # a: 창 시작 때 판 2, 창 안 판 3, 활성 판 4. 옛 판 1 은 빠진다. b: 1(창 시작 때이자 활성), 0 은 빠진다. c: 활성 1.
        # d: valid_from 이 창 시작과 같은 판 2 가 창 시작 때 판이다(경계 포함). 그 전 판 1 은 빠진다.
        self.assertEqual(versions, [("a", 2), ("a", 3), ("a", 4), ("b", 1), ("c", 1), ("d", 2), ("d", 3)])
        self.assertEqual(tsc.valid_at_start(src, spec, self.WS)[("d",)]["version"], 2)

    def test_the_check_script_agrees_with_the_slice_and_catches_a_time_only_slice(self):
        spec = PG_CFG["slice_by_validity"]["trainer_trace_window_threshold"]
        src = self.rows()
        source_gens, source_keys = tsc.valid_at_start(src, spec, self.WS), len(tsc.keys_of(src, spec))
        self.assertEqual(tsc.compare(self.run_slice(src), spec, self.WS, source_gens, source_keys), [])
        ws, we = tsc._ts(self.WS), tsc._ts(self.WE)
        time_only = [r for r in src if ws <= r["valid_from"] <= we]
        problems = tsc.compare(time_only, spec, self.WS, source_gens, source_keys)
        self.assertTrue(any("창 시작 때 판" in p for p in problems))
        self.assertTrue(any("키 수" in p for p in problems))

    def test_source_query_output_is_parsed(self):
        spec = PG_CFG["slice_by_validity"]["trainer_trace_window_threshold"]
        sql = tsc.source_sql(spec, self.WS)
        self.assertIn("max(version)", sql)
        self.assertTrue(sql.startswith("WITH "))  # q119-pg.sh 는 SELECT, WITH 로 시작하는 질의만 받는다
        gens, keys = tsc.parse_source("kind\tagent_id\tversion\tcount\tmd5\ngen\ta\t2\t2\tabc\nkeys\t3\t\t\t\n", spec)
        self.assertEqual((gens, keys), ({("a",): {"version": 2, "rows": 2, "ids_md5": "abc"}}, 3))


def ch_catalog(tables, columns, mviews=()):
    t = "\n".join("\t".join(map(str, r)) for r in tables)
    c = "\n".join("\t".join(r) for r in columns)
    m = "\n".join("\t".join(r) for r in mviews)
    return ch.parse_catalog({"tables": "database\tname\tengine\tpartition_key\tsorting_key\ttotal_rows\ttotal_bytes\n" + t,
                             "columns": c, "mviews": m})


class ChPlanTest(unittest.TestCase):
    WS, WE = "2026-11-10T00:00:00Z", "2026-11-10T02:40:00Z"

    def plan(self, tables, columns, mviews=(), skip=()):
        return ch.plan_tables(CH_CFG, ch_catalog(tables, columns, mviews), set(skip), self.WS, self.WE)

    def by_name(self, plan):
        return {f'{e["database"]}.{e["table"]}': e for e in plan["tables"]}

    def test_v3_tables_are_left_to_the_capture_script(self):
        rows = [("lucida", t, "MergeTree", "toYYYYMMDD(timestamp)", "timestamp", 1, 1) for t in V3_CH]
        cols = [("lucida", t, "timestamp", "DateTime64(9)") for t in V3_CH]
        plan = self.plan(rows, cols, skip=[f"lucida.{t}" for t in V3_CH])
        self.assertEqual(plan["tables"], [])
        self.assertEqual(plan["excluded"], [])

    def test_time_series_is_cut_by_the_partition_time_column(self):
        plan = self.plan([("lucida", "kcm_pod_logs_local", "MergeTree",
                           "(retention_hours, if(retention_hours <= 48, toStartOfHour(timestamp), toStartOfDay(timestamp)))",
                           "cluster, namespace, timestamp", 1, 10)],
                         [("lucida", "kcm_pod_logs_local", "ingested_at", "DateTime"),
                          ("lucida", "kcm_pod_logs_local", "timestamp", "DateTime64(9)")])
        e = self.by_name(plan)["lucida.kcm_pod_logs_local"]
        self.assertEqual((e["scope"], e["time_column"], e["format"]), ("slice", "timestamp", "Parquet"))
        self.assertEqual(e["file"], "data/clickhouse/kcm_pod_logs_local.parquet")
        self.assertIn(f"`timestamp` >= parseDateTime64BestEffort('{self.WS}')", e["where"])
        self.assertIn(f"`timestamp` <= parseDateTime64BestEffort('{self.WE}')", e["where"])

    def test_date_column_is_compared_by_day(self):
        plan = self.plan([("lucida", "agg_1d", "MergeTree", "day", "day", 0, 0)],
                         [("lucida", "agg_1d", "day", "Date")])
        e = self.by_name(plan)["lucida.agg_1d"]
        self.assertEqual(e["time_type"], "Date")
        self.assertIn(f"`day` >= toDate(parseDateTime64BestEffort('{self.WS}'))", e["where"])

    def test_sorting_key_first_time_column_also_cuts(self):
        plan = self.plan([("lucida", "events_by_hash", "MergeTree", "modulo(cityHash64(id), 16)", "ts, id", 1, 10)],
                         [("lucida", "events_by_hash", "ts", "DateTime")])
        e = self.by_name(plan)["lucida.events_by_hash"]
        self.assertEqual((e["time_column"], e["slice_basis"]), ("ts", "sorting_key_first"))
        self.assertEqual(e["where"], f" WHERE `ts` >= parseDateTime64BestEffort('{self.WS}')"
                                     f" AND `ts` <= parseDateTime64BestEffort('{self.WE}')")

    def test_dictionary_table_is_cut_by_its_day_key_not_by_the_last_write_time(self):
        # 겹쳐 쓰는 엔진에서 분할 시각 열이 정렬 키에 없으면 그 열은 마지막으로 쓴 시각이다(사전 표).
        rows = [("lucida", "apm_sql_texts_local", "ReplacingMergeTree", "toYYYYMMDD(timestamp)",
                 "service_namespace, service_name, timestamp_date, sql_dictionary_ref", 1, 1000)]
        cols = [("lucida", "apm_sql_texts_local", "timestamp", "DateTime64(9, 'UTC')", "", ""),
                ("lucida", "apm_sql_texts_local", "timestamp_date", "Date", "MATERIALIZED", "toDate(timestamp)")]
        e = self.by_name(self.plan(rows, cols))["lucida.apm_sql_texts_local"]
        self.assertEqual((e["scope"], e["time_column"], e["time_type"], e["slice_basis"]),
                         ("slice", "timestamp_date", "Date", "sorting_key_time"))
        self.assertIn(f"`timestamp_date` >= toDate(parseDateTime64BestEffort('{self.WS}'))", e["where"])
        self.assertIn(f"`timestamp_date` <= toDate(parseDateTime64BestEffort('{self.WE}'))", e["where"])
        # 날짜 열이 분할 열에서 계산되므로 같은 조건을 분할 열에도 걸어 부분을 건너뛴다.
        self.assertIn(f"toDate(`timestamp`) >= toDate(parseDateTime64BestEffort('{self.WS}'))", e["where"])
        self.assertNotIn("`timestamp` >= parseDateTime64BestEffort", e["where"])

    def test_dictionary_day_key_not_derived_from_the_partition_cannot_prune_so_the_cap_applies(self):
        cap = CH_CFG["whole_max_bytes"]
        rows = [("lucida", "dict_small", "ReplacingMergeTree", "toYYYYMMDD(ts)", "k, d", 1, cap),
                ("lucida", "dict_big", "ReplacingMergeTree", "toYYYYMMDD(ts)", "k, d", 1, cap + 1)]
        cols = [(t, n, c, ty, "", "") for t, n in (("lucida", "dict_small"), ("lucida", "dict_big"))
                for c, ty in (("ts", "DateTime"), ("d", "Date"))]
        plan = self.plan(rows, cols)
        e = self.by_name(plan)
        self.assertEqual(e["lucida.dict_small"]["time_column"], "d")
        self.assertNotIn("toDate(`ts`)", e["lucida.dict_small"]["where"])
        self.assertEqual([x["table"] for x in plan["excluded"]], ["dict_big"])

    def test_merging_table_without_a_time_key_is_kept_whole(self):
        rows = [("lucida", "last_seen", "ReplacingMergeTree", "toYYYYMMDD(seen_at)", "target_id", 1, 1000)]
        cols = [("lucida", "last_seen", "seen_at", "DateTime")]
        e = self.by_name(self.plan(rows, cols))["lucida.last_seen"]
        self.assertEqual((e["scope"], e["where"]), ("snap", ""))

    def test_aggregates_with_bucket_time_in_the_key_stay_partition_cut(self):
        rows = [("lucida", "agg_x", "AggregatingMergeTree", "toYYYYMM(minute)", "svc, minute", 1, 1000)]
        cols = [("lucida", "agg_x", "minute", "DateTime")]
        e = self.by_name(self.plan(rows, cols))["lucida.agg_x"]
        self.assertEqual((e["time_column"], e["slice_basis"]), ("minute", "partition_time"))

    def test_set_engine_cannot_be_read_and_is_excluded_but_join_is_kept(self):
        plan = self.plan([("lucida", "allow_set", "Set", "", "", 3, 100),
                          ("lucida", "dict_join", "Join", "", "", 3, 100)],
                         [("lucida", "dict_join", "k", "UInt8")])
        self.assertEqual([(x["table"], x["engine"]) for x in plan["excluded"]], [("allow_set", "Set")])
        self.assertIn("SELECT 로 읽을 수 없는", plan["excluded"][0]["reason"])
        self.assertEqual(self.by_name(plan)["lucida.dict_join"]["scope"], "snap")

    def test_local_storage_engines_are_kept_whole_and_remote_engines_are_left_out(self):
        plan = self.plan([("lucida", "mem", "Memory", "", "", 1, 100),
                          ("lucida", "s3_src", "S3", "", "", "\\N", "\\N")],
                         [("lucida", "mem", "at", "DateTime")])
        e = self.by_name(plan)
        self.assertEqual((e["lucida.mem"]["scope"], e["lucida.mem"]["where"]), ("snap", ""))
        self.assertEqual([x["table"] for x in plan["excluded"]], ["s3_src"])
        self.assertIn("바깥 원천", plan["excluded"][0]["reason"])

    def test_state_table_with_time_value_outside_the_keys_is_kept_whole(self):
        plan = self.plan([("lucida", "agg_watermark", "ReplacingMergeTree", "", "key", 10, 2000)],
                         [("lucida", "agg_watermark", "key", "String"), ("lucida", "agg_watermark", "value", "DateTime")])
        e = self.by_name(plan)["lucida.agg_watermark"]
        self.assertEqual((e["scope"], e["time_column"], e["where"]), ("snap", None, ""))

    def test_reference_table_over_the_cap_is_excluded_with_its_size(self):
        cap = CH_CFG["whole_max_bytes"]
        plan = self.plan([("lucida", "big_state", "ReplacingMergeTree", "", "key", 10, cap + 1),
                          ("lucida", "small_state", "ReplacingMergeTree", "", "key", 10, cap)],
                         [("lucida", "big_state", "key", "String")])
        self.assertEqual([e["table"] for e in plan["excluded"]], ["big_state"])
        self.assertEqual(plan["excluded"][0]["total_bytes"], cap + 1)
        self.assertIn("lucida.small_state", self.by_name(plan))

    def test_storage_less_engines_inner_tables_and_configured_exclusions_are_listed(self):
        plan = self.plan([("lucida", "mv_x", "MaterializedView", "", "", "\\N", "\\N"),
                          ("lucida", "v_x", "View", "", "", "\\N", "\\N"),
                          ("lucida", "ingest", "Null", "", "", "\\N", "\\N"),
                          ("lucida", ".inner_id.abc", "AggregatingMergeTree", "toYYYYMM(minute)", "minute", 1, 1),
                          ("lucida", "schema_migrations", "ReplacingMergeTree", "", "version", 1, 1)],
                         [("lucida", ".inner_id.abc", "minute", "DateTime")])
        self.assertEqual(plan["tables"], [])
        self.assertEqual(sorted(e["table"] for e in plan["excluded"]),
                         [".inner_id.abc", "ingest", "mv_x", "schema_migrations", "v_x"])
        self.assertTrue(all(e["reason"] for e in plan["excluded"]))

    def test_aggregate_states_go_native_simple_aggregates_stay_parquet(self):
        plan = self.plan([("lucida", "agg_live", "AggregatingMergeTree", "toStartOfHour(minute)", "svc, minute", 1, 1),
                          ("lucida", "agg_simple", "AggregatingMergeTree", "toYYYYMMDD(minute)", "minute", 1, 1)],
                         [("lucida", "agg_live", "minute", "DateTime"),
                          ("lucida", "agg_live", "lat", "AggregateFunction(quantilesTDigest(0.5, 0.95), Float64)"),
                          ("lucida", "agg_simple", "minute", "DateTime"),
                          ("lucida", "agg_simple", "n", "SimpleAggregateFunction(sum, UInt64)")])
        e = self.by_name(plan)
        self.assertEqual((e["lucida.agg_live"]["format"], e["lucida.agg_live"]["file"]),
                         ("Native", "data/clickhouse/agg_live.native"))
        self.assertEqual(e["lucida.agg_simple"]["format"], "Parquet")

    def test_uuid_columns_become_strings_and_other_databases_get_their_own_folder(self):
        plan = self.plan([("lucida_selfsystem", "otel_traces_local", "MergeTree", "toYYYYMMDD(timestamp)", "timestamp", 1, 1)],
                         [("lucida_selfsystem", "otel_traces_local", "timestamp", "DateTime64(9)"),
                          ("lucida_selfsystem", "otel_traces_local", "uid", "Nullable(UUID)")])
        e = self.by_name(plan)["lucida_selfsystem.otel_traces_local"]
        self.assertEqual(e["file"], "data/clickhouse/lucida_selfsystem/otel_traces_local.parquet")
        self.assertEqual(e["select"], "* REPLACE(toString(`uid`) AS `uid`)")
        self.assertEqual(e["source"], "`lucida_selfsystem`.`otel_traces_local`")

    def test_materialized_view_links_are_recorded(self):
        plan = self.plan([("lucida", "agg_live", "AggregatingMergeTree", "toStartOfHour(minute)", "minute", 1, 1),
                          ("lucida", "logs", "MergeTree", "toYYYYMMDD(timestamp)", "timestamp", 1, 1),
                          ("lucida", "mv_logs", "MaterializedView", "", "", "\\N", "\\N")],
                         [("lucida", "agg_live", "minute", "DateTime"), ("lucida", "logs", "timestamp", "DateTime")],
                         mviews=[("lucida", "mv_logs", "lucida.agg_live", ""),
                                 ("lucida", "logs", "", "lucida.mv_logs")])
        e = self.by_name(plan)
        self.assertEqual(e["lucida.agg_live"]["mv_target_of"], ["lucida.mv_logs"])
        self.assertEqual(e["lucida.logs"]["mv_source_of"], ["lucida.mv_logs"])

    def test_catalog_queries_touch_only_system_tables(self):
        for q in ch.catalog_queries(CH_CFG["databases"]).values():
            self.assertRegex(q, r"FROM system\.(tables|columns)\b")
        self.assertEqual(json.loads(json.dumps(CH_CFG["databases"])), ["lucida", "lucida_selfsystem"])



class ChRetentionTest(unittest.TestCase):
    """캡처만 재시도하기 전 보존 기간(TTL) 계산(2026-11-16 CAPFIX 2회차, 리뷰 상 1).

    TTL 식은 119 의 실제 모양(2026-11-16 system.tables engine_full)을 그대로 쓴다. 이 식들을 실제
    ClickHouse 26.3 에 돌린 값은 CAPFIX.md 2회차 절(119 읽기)에 있고, 여기서는 나누기, 질의 모양, 판단을 본다.
    """
    WS, WE = "2026-11-13T06:42:51Z", "2026-11-13T09:40:45Z"

    def test_delete_terms_follow_the_shapes_on_119(self):
        cases = {
            "MergeTree PARTITION BY toDate(ts) ORDER BY ts TTL toDateTime(timestamp) + toIntervalDay(3) "
            "SETTINGS ttl_only_drop_parts = 1, index_granularity = 8192": ["toDateTime(timestamp) + toIntervalDay(3)"],
            "ReplacingMergeTree ORDER BY k TTL toDate(bucket_ts) + toIntervalDay(30) WHERE bitAnd(store_mask, 4) != 0, "
            "toDate(bucket_ts) + toIntervalDay(2) WHERE bitAnd(store_mask, 4) = 0 SETTINGS ttl_only_drop_parts = 1":
                ["toDate(bucket_ts) + toIntervalDay(30)", "toDate(bucket_ts) + toIntervalDay(2)"],
            # TSV 로 온 작은따옴표, 따옴표 안 쉼표, 지우지 않는 항
            "MergeTree ORDER BY k TTL (toDateTime(toDate(timestamp) + 1, \\'UTC\\') + toIntervalDay(7)) WHERE "
            "event_source IN (\\'a, b\\'), timestamp + toIntervalDay(90) TO VOLUME \\'cold\\' SETTINGS x = 1":
                ["(toDateTime(toDate(timestamp) + 1, 'UTC') + toIntervalDay(7))"],
            "MergeTree ORDER BY k TTL bucket_ts + toIntervalDay(multiIf(tier = \\'5m\\', 30, 3650)) SETTINGS x = 1":
                ["bucket_ts + toIntervalDay(multiIf(tier = '5m', 30, 3650))"],
            "MergeTree ORDER BY k SETTINGS index_granularity = 8192": [],
            "Memory": [],
            "MergeTree ORDER BY k TTL a + toIntervalDay(1), , b SETTINGS x = 1": None,
        }
        for engine_full, want in cases.items():
            self.assertEqual(ch.ttl_delete_terms(ch._tsv_unescape(engine_full)), want, engine_full)

    def test_probe_puts_the_window_start_in_every_time_column_and_defaults_elsewhere(self):
        cat = ch_catalog([("lucida", "t", "MergeTree", "toYYYYMMDD(timestamp)", "timestamp", 1, 1)],
                         [("lucida", "t", "timestamp", "DateTime64(9, \\'UTC\\')", "", ""),
                          ("lucida", "t", "retention_days", "UInt16", "DEFAULT", "90"),
                          ("lucida", "t", "derived", "UInt16", "DEFAULT", "retention_days * 2"),
                          ("lucida", "t", "unused", "String", "", "")])
        sql = ch.retention_probe(cat, "lucida.t", ["toDateTime(timestamp) + toIntervalDay(retention_days)",
                                                   "timestamp + toIntervalDay(derived)"], self.WS)
        ws = "parseDateTime64BestEffort('2026-11-13T06:42:51Z', 3, 'UTC')"
        self.assertEqual(sql, (
            "SELECT arrayMin([dateDiff('second', " + ws + ", toDateTime64(toDateTime(timestamp) + "
            "toIntervalDay(retention_days), 3, 'UTC')), dateDiff('second', " + ws + ", toDateTime64(timestamp + "
            "toIntervalDay(derived), 3, 'UTC'))]) FROM (SELECT CAST(" + ws + " AS DateTime64(9, 'UTC')) AS `timestamp`, "
            "CAST((90) AS UInt16) AS `retention_days`, defaultValueOfTypeName('UInt16') AS `derived`)"))
        self.assertNotIn("unused", sql)
        self.assertNotRegex(sql, r"FROM `?lucida`?\.")  # 표 데이터를 읽지 않는다

    def fake(self, ttl, probe_out=None, extra_tables=()):
        tables = [("lucida", t, "MergeTree", "toYYYYMMDD(timestamp)", "timestamp", 1, 1) for t in V3_CH]
        tables += list(extra_tables)
        names = [r[1] if r[0] == "lucida" else f"{r[0]}.{r[1]}" for r in tables]
        calls = []

        def query(sql):
            calls.append(sql)
            if "partition_key" in sql:
                return "\n".join("\t".join(map(str, r)) for r in tables) + "\n"
            if "FROM system.columns" in sql:
                return "\n".join(f"{r[0]}\t{r[1]}\ttimestamp\tDateTime" for r in tables) + "\n"
            if "engine_full" in sql:
                return "\n".join(f"{r[0]}\t{r[1]}\t{ttl.get(f'{r[0]}.{r[1]}', 'MergeTree ORDER BY timestamp')}"
                                 for r in tables) + "\n"
            if "create_table_query" in sql:
                return ""
            if sql.startswith("SELECT arrayMin"):
                for key, out in (probe_out or {}).items():
                    if f"toIntervalDay({key})" in sql:
                        if isinstance(out, Exception):
                            raise out
                        return f"{out}\n"
                raise AssertionError(sql)
            raise AssertionError(sql)
        return query, calls, names

    def test_shortest_ttl_across_fixed_and_discovered_tables_and_selfsystem(self):
        ttl = {"lucida.otel_traces_local": "MergeTree ORDER BY t TTL toDateTime(timestamp) + toIntervalDay(3) SETTINGS a = 1",
               "lucida_selfsystem.otel_traces_local": "MergeTree ORDER BY t TTL toDateTime(timestamp) + toIntervalDay(1)",
               "lucida.big_unknown": "MergeTree ORDER BY t TTL timestamp + toIntervalDay(5)"}
        query, calls, _ = self.fake(ttl, {"3": 259200, "1": 86400, "5": 432000},
                                    [("lucida_selfsystem", "otel_traces_local", "MergeTree", "toYYYYMMDD(timestamp)",
                                      "timestamp", 1, 1)])
        r = ch.retention_check(CH_CFG, query, {f"lucida.{t}" for t in V3_CH}, self.WS, self.WE)
        self.assertEqual(r["shortest"], {"table": "lucida_selfsystem.otel_traces_local", "ttl_seconds": 86400})
        self.assertEqual(r["unreadable"], [])
        got = {t["table"]: t["ttl_seconds"] for t in r["tables"]}
        self.assertEqual(got["lucida.otel_traces_local"], 259200)
        self.assertIsNone(got["lucida.syslog_local"])  # TTL 없는 표
        self.assertEqual(len(got), len(V3_CH) + 1)  # v3 고정 표는 계획이 건너뛰어도 센다
        self.assertNotIn("lucida.big_unknown", got)  # 원천에 없는 설정 이름은 묻지 않는다

    def test_tables_the_capture_leaves_out_are_not_counted(self):
        ttl = {"lucida.schema_migrations": "MergeTree ORDER BY t TTL timestamp + toIntervalDay(1)"}
        query, _, _ = self.fake(ttl, {}, [("lucida", "schema_migrations", "MergeTree", "toYYYYMMDD(timestamp)",
                                           "timestamp", 1, 1)])
        r = ch.retention_check(CH_CFG, query, {f"lucida.{t}" for t in V3_CH}, self.WS, self.WE)
        self.assertIsNone(r["shortest"])
        self.assertNotIn("lucida.schema_migrations", [t["table"] for t in r["tables"]])

    def test_tables_whose_ttl_cannot_be_computed_are_reported_not_skipped(self):
        ttl = {"lucida.otel_traces_local": "MergeTree ORDER BY t TTL toDateTime(timestamp) + toIntervalDay(3)",
               "lucida.syslog_local": "MergeTree ORDER BY t TTL a + toIntervalDay(9), , b",
               "lucida.process_meta": "MergeTree ORDER BY t TTL toDateTime(timestamp) + toIntervalDay(7)"}
        query, _, _ = self.fake(ttl, {"3": 259200, "7": RuntimeError("Code: 62. Syntax error")})
        v3 = {f"lucida.{t}" for t in V3_CH} | {"lucida.gone_table"}
        r = ch.retention_check(CH_CFG, query, v3, self.WS, self.WE)
        self.assertEqual(sorted((u["table"], u["error"][:12]) for u in r["unreadable"]), [
            ("lucida.gone_table", "원천에 표가 없다"), ("lucida.process_meta", "TTL 계산 실패: C"),
            ("lucida.syslog_local", "TTL 절을 나누지 못")])
        self.assertEqual(r["shortest"], {"table": "lucida.otel_traces_local", "ttl_seconds": 259200})

    def test_v3_keys_are_read_from_the_capture_script(self):
        keys = ch.v3_keys_from_capture_script(ROOT.parent / "capture-eval-case.sh")
        self.assertEqual(keys, {f"lucida.{t}" for t in V3_CH})

    def test_capture_script_retention_mode_queries_clickhouse_with_header_credentials(self):
        import http.server
        import os
        import subprocess
        import threading

        ttl = {"lucida.otel_traces_local": "MergeTree ORDER BY t TTL toDateTime(timestamp) + toIntervalDay(3)"}
        query, _, _ = self.fake(ttl, {"3": 259200})
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                seen.append((self.headers.get("X-ClickHouse-User"), self.headers.get("X-ClickHouse-Key"), self.path))
                try:
                    out, code = query(body.replace(" FORMAT TSV", "")).encode(), 200
                except Exception as error:  # noqa: BLE001
                    out, code = str(error).encode(), 500
                self.send_response(code)
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            env = {**os.environ, "CH_URL": f"http://127.0.0.1:{server.server_port}", "CH_USER": "lucida",
                   "CH_PASSWORD": "pw-not-in-argv"}
            script = ROOT.parent / "capture-eval-case.sh"
            r = subprocess.run(["bash", str(script), "--retention-check", "--window-start", self.WS,
                                "--window-end", self.WE], capture_output=True, text=True, env=env, timeout=60)
            bad = subprocess.run(["bash", str(script), "--retention-check", "--window-start", self.WS],
                                 capture_output=True, text=True, env=env, timeout=60)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertEqual(doc["shortest"], {"table": "lucida.otel_traces_local", "ttl_seconds": 259200})
        self.assertEqual((doc["window_start"], doc["window_end"]), (self.WS, self.WE))
        self.assertTrue(seen and all(u == "lucida" and k == "pw-not-in-argv" and p == "/" for u, k, p in seen))
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("--window-end", bad.stderr)

if __name__ == "__main__":
    unittest.main()
