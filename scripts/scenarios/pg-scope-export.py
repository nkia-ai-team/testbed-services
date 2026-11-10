#!/usr/bin/env python3
"""pg-scope.json 을 pg_dump 인자와 psql \\copy 스크립트로 펼친다.

capture-eval-case.sh 가 두 번 부른다.

    # 1) pg_dump 에서 뺄 표
    pg-scope-export.py --emit exclude-args --config ... > /tmp/excl.txt
    pg_dump -Fc $(cat /tmp/excl.txt) ...

    # 2) 창으로 자른 행을 CSV 로 내보내는 psql 스크립트
    pg-scope-export.py --emit export-sql --window-start ... --window-end ... \\
        --out-dir /out/postgres > /tmp/export.sql
    psql -v ON_ERROR_STOP=1 -f /tmp/export.sql

    # 3) 소비자가 복원 때 돌릴 스크립트 (케이스 안에 동봉)
    pg-scope-export.py --emit restore-sql > <case>/data/postgres/restore.sql

drop_data 는 표를 만들되 행을 넣지 않는다(스키마는 남는다). slice 는 pg_dump 에서
빼고 여기서 창으로 잘라 CSV 로 낸다. 그 외는 전부 pg_dump 가 통째로 담는다(snap).

복원 순서: pg_restore 는 데이터를 넣은 뒤 FK 제약을 만든다. slice 표는 그 시점에
비어 있으므로 제약 생성은 통과하고, 그 뒤 \\copy 로 행을 넣을 때 FK 가 행 단위로
검사된다 — 그래서 뿌리(incidents·event_clusters)를 자식보다 먼저 넣어야 한다.
restore-sql 은 그 순서로 낸다.

v4 자동 발견(--catalog, 2026-11-10 R1): 캡처가 먼저 원천 PG 의 카탈로그(표, 크기, 시각 열,
외래 키)를 --emit catalog-sql 로 떠 오고, 그 카탈로그를 --catalog 로 넘기면 설정에 없는 표도
규칙으로 분류한다(plan_scope 참고). --catalog 없이 부르면 출력은 종전과 한 글자도 같다.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent / "pg-scope.json"


def load(path: Path) -> dict:
    cfg = json.loads(path.read_text())
    if cfg.get("schema_version") != 1:
        sys.exit(f"pg-scope.json schema_version={cfg.get('schema_version')} 를 모른다")
    return cfg


def scoped_tables(cfg: dict) -> list[str]:
    """pg_dump 에서 데이터를 빼야 하는 표 — slice(여기서 따로 낸다) + drop(아예 안 낸다)."""
    out: list[str] = []
    out += list(cfg["slice_roots"])
    out += list(cfg["slice_children"])
    out += list(cfg["slice_grandchildren"])
    out += list(cfg["slice_by_time"])
    out += list(cfg.get("slice_by_epoch_ms", {}))
    out += list(cfg["drop_data"])
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def explicit_slice_tables(cfg: dict) -> list[str]:
    return (list(cfg["slice_roots"]) + list(cfg["slice_children"]) + list(cfg["slice_grandchildren"])
            + list(cfg["slice_by_time"]) + list(cfg.get("slice_by_epoch_ms", {})))


def root_cte(table: str, spec: dict, ctx: dict | None = None, roots: dict | None = None) -> str:
    """창 안의 뿌리 + 대체 관계 폐포.

    superseded_by·merged_into 는 창 밖(대개 미래)을 가리킬 수 있어, 그대로 두면
    복원 때 FK 가 깨진다. 재귀로 참조 대상을 함께 싣는다.

    ctx(plan 의 카탈로그 정보)가 있으면 v4 경로다(2026-11-10 R1 수정 1회차):
    - 원천에 자기참조 열이 없으면(ctx["missing"] 에 "표.열") 그 단계를 뺀다. develop-ai DDL 367 이
      incidents.superseded_by 를 지웠다.
    - 대체 관계가 옮겨 간 계보 표(spec["lineage"]: 표, from, to 열)가 원천에 있으면 그 간선으로 창 끝 뒤
      대체 행을 재귀로 더한다. v3 의 superseded_by 재귀와 같은 뜻을 지킨다(창 안 인시던트를 대체한
      인시던트가 창 끝 뒤에 생겨도 싣는다).
    - spec["lineage_seed"](표, column, via, root)는 다른 뿌리가 남긴 행의 계보 간선이 가리키는 이 표의
      행을 씨앗에 더한다. 인시던트 계보 간선의 대체 군집이 그것이다(간선 행이 복원 때 FK 를 지키도록).
    ctx 가 없으면 종전과 같은 글자를 낸다.
    """
    tc, sr = spec["time_column"], spec["self_reference"]
    seed = (f'  SELECT id FROM public."{table}"\n'
            f'   WHERE "{tc}" >= :\'win_start\'::timestamptz AND "{tc}" <= :\'win_end\'::timestamptz')
    if ctx is not None:
        usable = ctx["lineage"]
        steps = []
        if f"{table}.{sr}" not in ctx["missing"]:
            steps.append(f'SELECT p."{sr}" AS id FROM public."{table}" p WHERE p.id = k.id AND p."{sr}" IS NOT NULL')
        for lin in spec.get("lineage", []):
            if lin["table"] in usable:
                steps.append(f'SELECT l."{lin["to"]}" AS id FROM public."{lin["table"]}" l '
                             f'WHERE l."{lin["from"]}" = k.id AND l."{lin["to"]}" IS NOT NULL')
        seeds = [seed]
        for ls in spec.get("lineage_seed", []):
            if ls["table"] in usable and roots and ls["root"] in roots:
                other = f'(\n{root_cte(ls["root"], roots[ls["root"]], ctx, None)}\nSELECT id FROM _keep)'
                seeds.append(f'  SELECT l."{ls["column"]}" FROM public."{ls["table"]}" l\n'
                             f'   WHERE l."{ls["via"]}" IN {other} AND l."{ls["column"]}" IS NOT NULL')
        seed_sql = "\n  UNION\n".join(seeds)
        if not steps:
            return f'WITH _keep AS (\n{seed_sql}\n)'
        if len(steps) == 1 and len(seeds) == 1 and steps[0].startswith('SELECT p.'):
            pass  # 자기참조 하나뿐: 아래 종전 모양 그대로
        else:
            union = "\n    UNION ALL\n    ".join(steps)
            return (
                f'WITH RECURSIVE _seed AS (\n{seed_sql}\n), _keep AS (\n'
                f'  SELECT id FROM _seed\n'
                f'  UNION\n'
                f'  SELECT n.id FROM _keep k CROSS JOIN LATERAL (\n    {union}\n  ) n\n'
                f')'
            )
    return (
        f'WITH RECURSIVE _seed AS (\n'
        f'  SELECT id FROM public."{table}"\n'
        f'   WHERE "{tc}" >= :\'win_start\'::timestamptz AND "{tc}" <= :\'win_end\'::timestamptz\n'
        f'), _keep AS (\n'
        f'  SELECT id FROM _seed\n'
        f'  UNION\n'
        f'  SELECT p."{sr}" FROM public."{table}" p JOIN _keep k ON p.id = k.id\n'
        f'   WHERE p."{sr}" IS NOT NULL\n'
        f')'
    )


def time_slice_query(table: str, col: str) -> str:
    return (f'SELECT t.* FROM public."{table}" t '
            f'WHERE t."{col}" >= :\'win_start\'::timestamptz '
            f'AND t."{col}" <= :\'win_end\'::timestamptz')


def fk_child_query(table: str, fks: list[dict], kept: dict[str, str]) -> str:
    """부모(slice 표)가 남긴 행을 가리키는 행만. 자동 발견된 자식 표용.

    외래 키 열이 모두 NULL 인 행은 어느 부모에도 묶이지 않아 창 안이라고 말할 수 없으므로
    싣지 않는다. 부모가 둘 이상이면(예: 앞뒤 군집을 둘 다 가리키는 표) 각 열이 NULL 이거나
    그 부모가 남긴 행이어야 한다. 그래야 \\copy 때 FK 가 깨지지 않는다.
    """
    any_set = " OR ".join(f'c."{fk["column"]}" IS NOT NULL' for fk in fks)
    conds = [f"({any_set})"]
    for i, fk in enumerate(fks):
        conds.append(
            f'(c."{fk["column"]}" IS NULL OR c."{fk["column"]}" IN ('
            f'SELECT _p{i}."{fk["parent_column"]}" FROM ({kept[fk["parent"]]}) _p{i}))'
        )
    return f'SELECT c.* FROM public."{table}" c WHERE ' + "\n  AND ".join(conds)


def ordered_slices(cfg: dict, plan: dict | None = None) -> list[tuple[str, str]]:
    """(표, 질의) 를 복원 가능한 순서로. 뿌리 → 자식 → 손자 → 시간슬라이스 → 자동 발견분.

    plan(plan_scope 의 결과)이 없으면 설정에 적힌 표만, 종전과 같은 순서와 글자로 낸다.
    plan 이 있으면 원천에 없는 표(absent)를 빼고, 자동 발견된 slice 표를 뒤에 붙인다.
    """
    absent = set(plan["absent"]) if plan else set()
    ctx = {"missing": set(plan["missing_columns"]), "lineage": set(plan["lineage"])} if plan else None
    roots = cfg["slice_roots"]
    items: list[tuple[str, str]] = []

    for t, spec in cfg["slice_roots"].items():
        if t in absent:
            continue
        items.append((t, f'{root_cte(t, spec, ctx, roots)}\nSELECT t.* FROM public."{t}" t '
                         f'WHERE t.id IN (SELECT id FROM _keep)'))

    for t, spec in cfg["slice_children"].items():
        root = spec["root"]
        if t in absent or root in absent:
            continue
        items.append((t, f'{root_cte(root, cfg["slice_roots"][root], ctx, roots)}\n'
                         f'SELECT c.* FROM public."{t}" c '
                         f'WHERE c."{spec["column"]}" IN (SELECT id FROM _keep)'))

    for t, spec in cfg["slice_grandchildren"].items():
        parent, col, pkey = spec["parent"], spec["column"], spec["parent_key"]
        proot = cfg["slice_children"][parent]["root"]
        if t in absent or parent in absent or proot in absent:
            continue
        pcol = cfg["slice_children"][parent]["column"]
        items.append((t, f'{root_cte(proot, cfg["slice_roots"][proot], ctx, roots)}\n'
                         f'SELECT g.* FROM public."{t}" g WHERE g."{col}" IN ('
                         f'SELECT p."{pkey}" FROM public."{parent}" p '
                         f'WHERE p."{pcol}" IN (SELECT id FROM _keep))'))

    for t, col in cfg["slice_by_time"].items():
        if t in absent:
            continue
        items.append((t, time_slice_query(t, col)))

    # epoch millis 컬럼(bigint)은 timestamptz 비교가 안 된다 — 창을 epoch 로 옮긴다.
    for t, col in cfg.get("slice_by_epoch_ms", {}).items():
        if t in absent:
            continue
        items.append((t, f'SELECT t.* FROM public."{t}" t '
                         f'WHERE t."{col}" >= (extract(epoch FROM :\'win_start\'::timestamptz) * 1000)::bigint '
                         f'AND t."{col}" <= (extract(epoch FROM :\'win_end\'::timestamptz) * 1000)::bigint'))

    if plan:
        kept = dict(items)
        for t in plan["auto_slices"]:
            entry = plan["tables"][t]
            if entry["rule"] == "history":
                q = time_slice_query(t, entry["time_column"])
            else:
                q = fk_child_query(t, entry["fks"], kept)
            kept[t] = q
            items.append((t, q))
    return items


# ---------------------------------------------------------------- v4 자동 발견 (R1)
#
# 캡처가 원천 PG 에서 떠 오는 카탈로그. 큰 열은 읽지 않는다(크기는 pg_table_size, 행 수는 통계
# 추정치). 크기는 덤프가 실제로 읽는 표 본체와 TOAST 이고 인덱스는 뺀다(pg_dump 는 인덱스를 정의로만
# 담는다). 119 실측 ai_trainer_stream_rows: 전체 147MB 가 인덱스이고 표 본체는 0.2MB.
# 분할 표는 부모 한 줄로 세고 크기는 잎 분할의 합이다.
CATALOG_QUERIES = {
    "tables": (
        "SELECT n.nspname, c.relname, c.relkind::text,\n"
        "  CASE WHEN c.relkind = 'p' THEN (SELECT coalesce(sum(pg_table_size(pt.relid)), 0)\n"
        "         FROM pg_partition_tree(c.oid) pt WHERE pt.isleaf)\n"
        "       ELSE pg_table_size(c.oid) END AS bytes,\n"
        "  greatest(c.reltuples, 0)::bigint AS est_rows, s.n_live_tup, s.n_dead_tup\n"
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace\n"
        "LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid\n"
        "WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition\n"
        "  AND n.nspname <> 'information_schema' AND n.nspname !~ '^pg_'\n"
        "ORDER BY 1, 2"
    ),
    "columns": (
        "SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod)\n"
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace\n"
        "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped\n"
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition\n"
        "ORDER BY 1, a.attnum"
    ),
    "fks": (
        "SELECT cl.relname,\n"
        "  (SELECT string_agg(a.attname, ',' ORDER BY k.ord) FROM unnest(con.conkey) WITH ORDINALITY k(attnum, ord)\n"
        "     JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum),\n"
        "  pl.relname,\n"
        "  (SELECT string_agg(a.attname, ',' ORDER BY k.ord) FROM unnest(con.confkey) WITH ORDINALITY k(attnum, ord)\n"
        "     JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.attnum)\n"
        "FROM pg_constraint con\n"
        "JOIN pg_class cl ON cl.oid = con.conrelid JOIN pg_namespace n ON n.oid = cl.relnamespace\n"
        "JOIN pg_class pl ON pl.oid = con.confrelid JOIN pg_namespace pn ON pn.oid = pl.relnamespace\n"
        "WHERE con.contype = 'f' AND n.nspname = 'public' AND pn.nspname = 'public'\n"
        "  AND NOT cl.relispartition AND NOT pl.relispartition\n"
        "ORDER BY 1, 3, 2"
    ),
}


def _num(v: str) -> int | None:
    return None if v in ("", "\\N", None) else int(float(v))


def parse_catalog(texts: dict[str, str]) -> dict:
    """카탈로그 질의 결과(탭 구분, 머리줄이 있어도 되고 없어도 된다)를 구조로."""
    def rows(name, width):
        out = []
        for line in (texts.get(name) or "").splitlines():
            parts = line.split("\t")
            if len(parts) < width:
                continue
            out.append(parts)
        return out

    tables: dict[str, dict] = {}
    for p in rows("tables", 5):
        schema, name, relkind, bytes_ = p[0], p[1], p[2], p[3]
        if not re.fullmatch(r"-?\d+(\.\d+)?", bytes_ or ""):
            continue  # 머리줄
        key = name if schema == "public" else f"{schema}.{name}"
        tables[key] = {
            "schema": schema, "name": name, "relkind": relkind, "bytes": _num(bytes_),
            "est_rows": _num(p[4]),
            "live_rows": _num(p[5]) if len(p) > 5 else None,
            "dead_rows": _num(p[6]) if len(p) > 6 else None,
        }
    columns: dict[str, list[str]] = {}
    timecols: dict[str, list[str]] = {}
    for p in rows("columns", 3):
        if p[0] not in tables:
            continue
        columns.setdefault(p[0], []).append(p[1])
        if p[2].startswith(("timestamp", "date")):
            timecols.setdefault(p[0], []).append(p[1])
    fks: dict[str, list[dict]] = {}
    for p in rows("fks", 4):
        if p[0] not in tables:
            continue
        fks.setdefault(p[0], []).append(
            {"columns": p[1].split(","), "parent": p[2], "parent_columns": p[3].split(",")})
    return {"tables": tables, "columns": columns, "timecols": timecols, "fks": fks}


def load_catalog(d: Path) -> dict:
    return parse_catalog({k: (d / f"{k}.tsv").read_text() if (d / f"{k}.tsv").exists() else ""
                          for k in CATALOG_QUERIES})


def plan_scope(cfg: dict, catalog: dict) -> dict:
    """원천 PG 의 모든 표를 snap, slice, drop 으로 가른다.

    설정에 적힌 표는 설정대로(rule=config). 설정에 없는 public 표는 차례로:

    1. fk_closure: slice 표를 외래 키로 가리키는 표는 그 부모가 남긴 행에 묶인 행만 싣는다.
       통째로 담으면 다른 날 인시던트, 군집에 딸린 행이 딸려 오고(누설), 복원 때 부모에 없는
       행을 가리켜 FK 제약 생성이 깨진다. 새로 생긴 자식 표도 사람 손 없이 같은 자리에 간다.
    2. history: 이름이 이력 접미사(auto.history_suffixes)로 끝나고 사건 시각 열
       (auto.history_time_columns 순서의 첫 열)이 있으면 그 열로 창을 자른다. 이력은
       "무엇이 일어났는가"라 창 밖 행은 다른 녹화의 일이다. 이름 규칙 없이 시각 열만 보고
       자르지 않는다: latest, state, 인벤토리 표도 같은 이름의 시각 열(changed_at,
       collected_at, observed_at)을 갖고, 자르면 창 이전부터 있던 자산과 상태가 사라진다
       (spec-eval-data-capture §5.1).
    3. 나머지는 기준 표로 보고 통째로(default). 단 auto.snap_max_bytes 를 넘고 snap_tables 에
       이유가 적혀 있지 않으면 over_cap: 스키마만 담고 데이터는 뺀다. 모르는 큰 표가 덤프
       제한 시간을 넘겨 녹화 전체를 잃는 것(2026-10-08 F30-R)을 막는다. 녹화 전 점검
       (pg-scope-guard)이 같은 계획으로 이 표를 미리 알린다.

    1, 2 는 서로를 부르므로(이력 표를 가리키는 자식) 더 바뀌지 않을 때까지 되풀이한다.
    """
    auto = cfg.get("auto", {})
    suffixes = tuple(auto.get("history_suffixes", []))
    time_pref = list(auto.get("history_time_columns", []))
    cap = auto.get("snap_max_bytes")
    snap_ok = cfg.get("snap_tables", {})
    tables, timecols, fks = catalog["tables"], catalog["timecols"], catalog["fks"]

    explicit_slice = [t for t in explicit_slice_tables(cfg)]
    drop = list(cfg["drop_data"])
    configured = set(explicit_slice) | set(drop) | set(snap_ok)
    absent = sorted(t for t in configured if t not in tables)
    # 설정이 이름으로 부르는 열 가운데 원천에 없는 것. 지금은 뿌리의 자기참조 열만 대체 경로가 있다.
    missing_columns = sorted(
        f"{t}.{spec['self_reference']}" for t, spec in cfg["slice_roots"].items()
        if t in tables and spec.get("self_reference") not in catalog["columns"].get(t, []))
    # 뿌리의 계보 표: 원천에 표와 열이 다 있을 때만 재귀에 쓴다. 표는 있는데 열이 없으면 알린다.
    lineage: set[str] = set()
    for spec in cfg["slice_roots"].values():
        for lin in spec.get("lineage", []) + spec.get("lineage_seed", []):
            need = [lin[k] for k in ("from", "to", "column", "via") if k in lin]
            if lin["table"] not in tables:
                continue
            gone = [c for c in need if c not in catalog["columns"].get(lin["table"], [])]
            if gone:
                missing_columns += [f"{lin['table']}.{c}" for c in gone]
            else:
                lineage.add(lin["table"])
    missing_columns = sorted(set(missing_columns))

    out: dict[str, dict] = {}
    for t, info in tables.items():
        base = {"table": t, "bytes": info["bytes"], "relkind": info["relkind"]}
        if t in explicit_slice:
            out[t] = {**base, "scope": "slice", "rule": "config"}
        elif t in drop:
            out[t] = {**base, "scope": "drop", "rule": "config"}
        elif t in snap_ok:
            out[t] = {**base, "scope": "snap", "rule": "config", "reason": snap_ok[t]}
        else:
            out[t] = {**base, "scope": None, "rule": None}

    slice_set = {t for t, e in out.items() if e["scope"] == "slice"}
    auto_order: list[str] = []
    conflicts: list[dict] = []
    conflicted: set[str] = set()

    def closure_pass() -> bool:
        changed = False
        for t in sorted(out):
            e = out[t]
            if e["scope"] is not None or t in conflicted or tables[t]["schema"] != "public":
                continue
            refs = [fk for fk in fks.get(t, []) if fk["parent"] in slice_set and fk["parent"] != t]
            if not refs:
                continue
            if any(fk["parent"] == t for fk in fks.get(t, [])):
                # 창 안 행이 창 밖 같은 표 행을 가리키면 \copy 때 FK 가 깨진다. 자동으로 정하지 않는다.
                conflicts.append({"table": t, "problem": "자기참조 외래 키가 있는 표가 slice 표를 가리킨다",
                                  "parents": sorted({fk["parent"] for fk in refs})})
                conflicted.add(t)
                continue
            if any(len(fk["columns"]) != 1 for fk in refs):
                conflicts.append({"table": t, "problem": "여러 열 외래 키로 slice 표를 가리킨다",
                                  "parents": sorted({fk["parent"] for fk in refs})})
                conflicted.add(t)
                continue
            e.update(scope="slice", rule="fk_closure", fks=[
                {"column": fk["columns"][0], "parent": fk["parent"], "parent_column": fk["parent_columns"][0]}
                for fk in refs])
            slice_set.add(t)
            auto_order.append(t)
            changed = True
        return changed

    def history_pass() -> bool:
        changed = False
        for t in sorted(out):
            e = out[t]
            if e["scope"] is not None or t in conflicted or tables[t]["schema"] != "public":
                continue
            if not suffixes or not t.endswith(suffixes):
                continue
            col = next((c for c in time_pref if c in timecols.get(t, [])), None)
            if col is None:
                continue
            e.update(scope="slice", rule="history", time_column=col)
            slice_set.add(t)
            auto_order.append(t)
            changed = True
        return changed

    while True:
        while closure_pass():
            pass
        if not history_pass():
            break

    # 설정이 부르는 열이 원천에 없으면 그 표의 추출 질의가 캡처를 죽인다(ON_ERROR_STOP).
    # 자기참조 열은 위에서 재귀를 빼는 대체 경로가 있어 여기서 빼고, 나머지는 알린다.
    cols = catalog["columns"]
    wanted: list[tuple[str, str]] = []
    for t, spec in cfg["slice_roots"].items():
        wanted.append((t, spec["time_column"]))
    for t, spec in cfg["slice_children"].items():
        wanted.append((t, spec["column"]))
    for t, spec in cfg["slice_grandchildren"].items():
        wanted += [(t, spec["column"]), (spec["parent"], spec["parent_key"])]
    for t, col in list(cfg["slice_by_time"].items()) + list(cfg.get("slice_by_epoch_ms", {}).items()):
        wanted.append((t, col))
    for t, col in wanted:
        if t in tables and col not in cols.get(t, []):
            conflicts.append({"table": t, "problem": f"설정이 부르는 열 {col} 이 원천에 없다", "parents": []})

    over_cap: list[str] = []
    for t, e in out.items():
        if e["scope"] is not None:
            continue
        if cap is not None and (e["bytes"] or 0) > cap:
            e.update(scope="drop", rule="over_cap", cap_bytes=cap)
            over_cap.append(t)
        else:
            e.update(scope="snap", rule="default")

    # 통째로 담는 표가 slice 표를 외래 키로 가리키면 복원 때 제약 생성이 깨진다(비우는 drop 표는
    # 행이 없어 괜찮다). 설정을 사람이 고칠 일이라 자동으로 바꾸지 않고 알린다. snap_tables 에 이유와 함께
    # 적힌 표는 사람이 이미 정한 것이라(상태, 대기열, 지식 표는 자르지 않는다) 알림 목록에만 남긴다.
    snap_fk_to_slice: list[dict] = []
    for t, e in out.items():
        if e["scope"] == "snap":
            refs = sorted({fk["parent"] for fk in fks.get(t, []) if fk["parent"] in slice_set})
            if not refs:
                continue
            if t in snap_ok:
                snap_fk_to_slice.append({"table": t, "parents": refs})
            else:
                conflicts.append({"table": t, "problem": f"{e['scope']} 표가 slice 표를 외래 키로 가리킨다",
                                  "parents": refs})

    return {
        "tables": out,
        "auto_slices": auto_order,
        "over_cap": sorted(over_cap),
        "absent": absent,
        # 설정이 구간으로 자르라고 한 표가 원천에 없음: 이름이 바뀐 것이면 녹화가 조용히 그 표 없이 끝난다.
        # drop 표는 비우는 표라 없어도 녹화가 달라지지 않으므로 뺀다.
        "absent_slice": sorted(set(absent) & set(explicit_slice)),
        "missing_columns": missing_columns,
        "lineage": sorted(lineage),
        "snap_fk_to_slice": snap_fk_to_slice,
        "conflicts": conflicts,
    }


def snap_load(cfg: dict, plan: dict) -> dict:
    """통째로 덤프하는 표의 크기 합과 예산. 예산을 넘으면 pg_dump 가 제한 시간 안에 끝나지 않을 수 있다."""
    snaps = sorted(((e["bytes"] or 0, t) for t, e in plan["tables"].items() if e["scope"] == "snap"), reverse=True)
    total = sum(b for b, _ in snaps)
    budget = cfg.get("auto", {}).get("snap_budget_bytes")
    return {"snap_bytes": total, "snap_budget_bytes": budget,
            "over_budget": budget is not None and total > budget,
            "largest_snap": [{"table": t, "bytes": b} for b, t in snaps[:5]]}


def plan_summary(plan: dict, cfg: dict | None = None) -> dict:
    """meta.json 의 postgres_scope 에 싣는 요약. 표마다의 결정은 plan 파일 전문에."""
    counts: dict[str, int] = {}
    for e in plan["tables"].values():
        counts[e["scope"]] = counts.get(e["scope"], 0) + 1
    return {
        **(snap_load(cfg, plan) if cfg is not None else {}),
        "tables": len(plan["tables"]),
        "by_scope": dict(sorted(counts.items())),
        "auto_slices": [{"table": t, "rule": plan["tables"][t]["rule"]} for t in plan["auto_slices"]],
        "over_cap": [{"table": t, "bytes": plan["tables"][t]["bytes"]} for t in plan["over_cap"]],
        "absent": plan["absent"],
        "absent_slice": plan["absent_slice"],
        "missing_columns": plan["missing_columns"],
        "lineage": plan["lineage"],
        "snap_fk_to_slice": plan["snap_fk_to_slice"],
        "conflicts": plan["conflicts"],
    }


def _exclude_flag(plan: dict | None, t: str) -> str:
    # 분할 표는 부모만 빼면 잎 분할의 데이터가 그대로 실린다(pg_dump 16 부터 -and-children).
    # public 밖 스키마 표의 키는 이미 "스키마.표" 다.
    name = t if "." in t else f"public.{t}"
    if plan and plan["tables"].get(t, {}).get("relkind") == "p":
        return f"--exclude-table-data-and-children={name}"
    return f"--exclude-table-data={name}"


def emit_exclude_args(cfg: dict, plan: dict | None = None) -> str:
    names = scoped_tables(cfg)
    if plan:
        names += [t for t in plan["auto_slices"] + plan["over_cap"] if t not in names]
    return "\n".join(_exclude_flag(plan, t) for t in names)


def emit_export_sql(cfg: dict, out_dir: str, plan: dict | None = None) -> str:
    """서버측 COPY ... TO STDOUT + \\o 로 낸다.

    \\copy 를 쓰지 않는 이유(2026-08-13 실측): \\copy 는 psql 자체 파서를 타는
    한 줄짜리 메타명령이라 `public."incidents"` 를 `public. "incidents"` 로,
    `:'win_start'` 를 `: 'win_start'` 로 쪼개 질의를 깨뜨린다. COPY ... TO STDOUT
    은 보통 SQL 로 파싱되므로 여러 줄과 변수 보간이 그대로 동작한다.
    """
    d = out_dir.rstrip("/")
    lines = [
        "-- pg-scope-export.py 생성물. 캡처 창으로 자른 행을 CSV 로 낸다.",
        "\\set ON_ERROR_STOP on",
        "\\pset footer off",
        "",
    ]
    for t, q in ordered_slices(cfg, plan):
        lines.append(f"-- {t}")
        lines.append(f"\\o {d}/{t}.csv")
        lines.append(f"COPY ({q}) TO STDOUT WITH (FORMAT csv, HEADER true);")
        lines.append("\\o")
        lines.append("")
    return "\n".join(lines)


def emit_catalog_sql(out_dir: str) -> str:
    d = out_dir.rstrip("/")
    lines = ["-- pg-scope-export.py 생성물. 원천 PG 카탈로그(표, 크기, 시각 열, 외래 키). 표 데이터는 읽지 않는다.",
             "\\set ON_ERROR_STOP on", ""]
    for name, q in CATALOG_QUERIES.items():
        lines += [f"\\o {d}/{name}.tsv", f"COPY ({q}) TO STDOUT;", "\\o", ""]
    return "\n".join(lines)


def emit_restore_sql(cfg: dict, plan: dict | None = None) -> str:
    lines = [
        "-- 케이스 동봉 스크립트. pg_restore 로 postgres.dump 를 복원한 **뒤에** 돌린다.",
        "-- 이 디렉터리(data/postgres/)를 cwd 로 두고:  psql -U lucida -d lucida -f restore.sql",
        "-- 순서가 곧 FK 순서다 — 바꾸지 말 것.",
        "\\set ON_ERROR_STOP on",
        "",
    ]
    for t, _ in ordered_slices(cfg, plan):
        # 표 이름은 전부 소문자 무인용부호라 \copy 의 자체 파서로도 안전하다.
        lines.append(f"\\copy {t} FROM '{t}.csv' WITH (FORMAT csv, HEADER true)")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--emit", required=True,
                    choices=["exclude-args", "export-sql", "restore-sql", "slice-tables",
                             "catalog-sql", "plan-json", "plan-summary"])
    ap.add_argument("--out-dir", default="/out/postgres",
                    help="export-sql, catalog-sql 이 파일을 쓸 경로(psql 이 보는 경로)")
    ap.add_argument("--catalog", type=Path,
                    help="catalog-sql 로 떠 온 디렉터리(tables.tsv, columns.tsv, fks.tsv). "
                         "주면 설정에 없는 표도 규칙으로 분류한다")
    args = ap.parse_args()

    if args.emit == "catalog-sql":
        print(emit_catalog_sql(args.out_dir))
        return 0
    cfg = load(args.config)
    plan = plan_scope(cfg, load_catalog(args.catalog)) if args.catalog else None
    if args.emit in ("plan-json", "plan-summary"):
        if plan is None:
            sys.exit("--emit plan-json, plan-summary 는 --catalog 가 필요하다")
        print(json.dumps(plan if args.emit == "plan-json" else plan_summary(plan, cfg),
                         ensure_ascii=False, indent=1, sort_keys=True))
    elif args.emit == "exclude-args":
        print(emit_exclude_args(cfg, plan))
    elif args.emit == "export-sql":
        print(emit_export_sql(cfg, args.out_dir, plan))
    elif args.emit == "restore-sql":
        print(emit_restore_sql(cfg, plan))
    else:
        print("\n".join(t for t, _ in ordered_slices(cfg, plan)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
