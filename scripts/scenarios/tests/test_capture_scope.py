"""녹화 표 자동 발견 계획 (scripts/scenarios/pg-scope-export.py, ch-scope-plan.py, v4 R1).

캡처 스크립트 전체 경로는 scripts/test-capture-eval-case.sh 가 가짜 curl, docker 로 돌린다.
여기서는 두 계획기의 규칙을 하나씩 본다.
"""
from __future__ import annotations

import copy
import importlib.util
import json
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


def pg_catalog(tables, columns=None, fks=None, *, configured=True):
    """tables: {name: bytes} 또는 {name: (bytes, relkind)}. configured=True 면 설정의 표가 모두 원천에 있다."""
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
    col_lines = [f"{t}\t{c}\t{ty}" for t, cs in cols.items() for c, ty in cs]
    fk_lines = [f"{c}\t{cc}\t{p}\t{pc}" for c, cc, p, pc in (fks or [])]
    return pg.parse_catalog({"tables": "\n".join(rows), "columns": "\n".join(col_lines), "fks": "\n".join(fk_lines)})


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
        cat = pg_catalog({"security.pinned": 4096})
        cat["fks"]["security.pinned"] = [{"columns": ["incident_id"], "parent": "incidents", "parent_columns": ["id"]}]
        cat["fks"]["kedb_pending"] = [{"columns": ["incident_id"], "parent": "incidents", "parent_columns": ["id"]}]
        plan = pg.plan_scope(PG_CFG, cat)
        self.assertEqual([c["table"] for c in plan["conflicts"]], ["security.pinned"])

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
        cap = PG_CFG["auto"]["snap_max_bytes"]
        plan = self.plan({"security.big": cap + 1})
        self.assertEqual(plan["tables"]["security.big"]["scope"], "drop")
        args = pg.emit_exclude_args(PG_CFG, plan).splitlines()
        self.assertIn("--exclude-table-data=security.big", args)
        self.assertFalse(any("public.security" in a for a in args))

    def test_other_schemas_are_kept_whole(self):
        plan = self.plan({"security.audit_marks": 4096})
        self.assertEqual(plan["tables"]["security.audit_marks"]["scope"], "snap")

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
        for name in ("tables", "columns", "fks"):
            self.assertIn(f"\\o /out/catalog/postgres/{name}.tsv", sql)
        for q in pg.CATALOG_QUERIES.values():
            self.assertNotRegex(q, r"FROM public\.")


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


if __name__ == "__main__":
    unittest.main()
