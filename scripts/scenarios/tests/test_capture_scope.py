"""녹화 표 자동 발견 계획 (scripts/scenarios/pg-scope-export.py, ch-scope-plan.py, v4 R1).

캡처 스크립트 전체 경로는 scripts/test-capture-eval-case.sh 가 가짜 curl, docker 로 돌린다.
여기서는 두 계획기의 규칙을 하나씩 본다.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pg = _load("pg_scope_export", "pg-scope-export.py")
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
        for t, cs in need.items():
            cols.setdefault(t, [(c, "timestamp with time zone" if c.endswith("_at") else "uuid") for c in cs])
    col_lines = [f"{t}\t{c}\t{ty}" for t, cs in cols.items() for c, ty in cs]
    fk_lines = [f"{c}\t{cc}\t{p}\t{pc}" for c, cc, p, pc in (fks or [])]
    return pg.parse_catalog({"tables": "\n".join(rows), "columns": "\n".join(col_lines), "fks": "\n".join(fk_lines)})


class PgPlanTest(unittest.TestCase):
    def plan(self, *a, cfg=None, **k):
        return pg.plan_scope(cfg or PG_CFG, pg_catalog(*a, **k))

    def test_without_unknown_tables_the_v3_output_is_unchanged(self):
        plan = self.plan({})
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
                          "trainer_trace_window_threshold": cap * 10})
        self.assertEqual(plan["tables"]["big_unknown"]["scope"], "drop")
        self.assertEqual(plan["tables"]["big_unknown"]["rule"], "over_cap")
        self.assertEqual(plan["over_cap"], ["big_unknown"])
        self.assertEqual(plan["tables"]["small_unknown"]["scope"], "snap")
        self.assertEqual(plan["tables"]["trainer_trace_window_threshold"]["rule"], "config")
        args = pg.emit_exclude_args(PG_CFG, plan)
        self.assertIn("--exclude-table-data=public.big_unknown", args)
        self.assertNotIn("trainer_trace_window_threshold", args)
        self.assertNotIn("small_unknown", args)

    def test_whole_dump_budget_counts_only_whole_tables(self):
        budget = PG_CFG["auto"]["snap_budget_bytes"]
        under = self.plan({"trainer_trace_window_threshold": budget - 10**6, "incident_snapshots": budget},
                          fks=[("incident_snapshots", "incident_id", "incidents", "id")])
        load = pg.snap_load(PG_CFG, under)
        self.assertFalse(load["over_budget"])
        over = self.plan({"trainer_trace_window_threshold": budget + 1})
        load = pg.plan_summary(over, PG_CFG)
        self.assertTrue(load["over_budget"])
        self.assertEqual(load["largest_snap"][0], {"table": "trainer_trace_window_threshold", "bytes": budget + 1})

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

    def test_explicit_snap_pointing_at_a_slice_table_is_a_conflict_but_drop_is_not(self):
        cfg = copy.deepcopy(PG_CFG)
        cfg["snap_tables"]["pinned_child"] = "시험"
        plan = pg.plan_scope(cfg, pg_catalog({"pinned_child": 4096},
                                             fks=[("pinned_child", "incident_id", "incidents", "id"),
                                                  ("kedb_pending", "incident_id", "incidents", "id")]))
        self.assertEqual([c["table"] for c in plan["conflicts"]], ["pinned_child"])

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
        self.assertEqual(self.by_name(plan)["lucida.events_by_hash"]["time_column"], "ts")

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
