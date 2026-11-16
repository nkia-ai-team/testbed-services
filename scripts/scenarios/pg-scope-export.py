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
    """pg_dump 에서 데이터를 빼야 하는 표: slice(여기서 따로 낸다), drop(아예 안 낸다), 비밀 계열."""
    out: list[str] = []
    out += list(cfg["slice_roots"])
    out += list(cfg["slice_children"])
    out += list(cfg["slice_grandchildren"])
    out += list(cfg["slice_by_time"])
    out += list(cfg.get("slice_by_epoch_ms", {}))
    out += list(cfg.get("slice_by_validity", {}))
    out += list(cfg["drop_data"])
    # 비밀 계열(2026-11-16): 카탈로그 없이 불러도(종전 경로) 행을 담지 않는다.
    out += list(cfg.get("secret_tables", {}))
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def explicit_slice_tables(cfg: dict) -> list[str]:
    return (list(cfg["slice_roots"]) + list(cfg["slice_children"]) + list(cfg["slice_grandchildren"])
            + list(cfg["slice_by_time"]) + list(cfg.get("slice_by_epoch_ms", {}))
            + list(cfg.get("slice_by_validity", {})))


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


def validity_slice_query(table: str, spec: dict) -> str:
    """판이 쌓이는 모델 상태 표를 창으로 자른다(2026-11-10 R1 수정 1회차, 사용자 결정 Q10).

    spec: key(판을 고르는 단위 열 목록), version(판 번호), valid_from(판이 유효해진 시각), active(지금 판).
    담는 행은 셋의 합이다.
    1. 키마다 창 시작 때 유효했던 판: valid_from 이 창 시작 이전인 판 중 판 번호가 가장 큰 것의 모든 행.
       재생이 창 시작부터 쓰는 임계값이다.
    2. 창 안에서 만든 판(valid_from 이 창 안).
    3. 캡처 때 활성인 판(active).
    판 단위로 고르는 이유: 제품은 한 판의 행을 한 트랜잭션에 넣고 판 전체를 한꺼번에 활성으로 바꾼다
    (lucida-next query store ai_trainer_trace.go). 행 단위 키로 최신을 고르면 새 판에서 빠진 항목의 옛
    임계값이 되살아난다.
    """
    keys = ", ".join(f'"{k}"' for k in spec["key"])
    v, vf, act = spec["version"], spec["valid_from"], spec["active"]
    return (
        f'SELECT t.* FROM public."{table}" t\n'
        f'WHERE t."{act}"\n'
        f'   OR (t."{vf}" >= :\'win_start\'::timestamptz AND t."{vf}" <= :\'win_end\'::timestamptz)\n'
        f'   OR ({", ".join(f"t.{chr(34)}{k}{chr(34)}" for k in spec["key"])}, t."{v}") IN (\n'
        f'        SELECT {keys}, max("{v}") FROM public."{table}"\n'
        f'         WHERE "{vf}" <= :\'win_start\'::timestamptz GROUP BY {keys})'
    )


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
    absent = (set(plan["absent"]) | {t for t, e in plan["tables"].items() if e["scope"] == "excluded"}) if plan else set()
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

    # 판(version)이 쌓이는 모델 상태 표: 창 시작 때 키마다 유효했던 판, 창 안에서 만든 판, 캡처 때 활성인 판.
    for t, spec in cfg.get("slice_by_validity", {}).items():
        if t in absent:
            continue
        items.append((t, validity_slice_query(t, spec)))

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
_BASE_QUERIES = {
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
    # 4회차(2026-11-16): 뒤 다섯 열은 비밀 열 가림값을 정하는 데 쓴다(NOT NULL, 기본값 식, 그 열을 부르는 CHECK,
    # 그 열이 든 UNIQUE 색인, 생성 열). 앞 세 열은 종전과 같다.
    "columns": (
        "SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod),\n"
        "  a.attnotnull, coalesce(pg_get_expr(d.adbin, d.adrelid), ''),\n"
        "  EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conrelid = c.oid AND k.contype = 'c' AND a.attnum = ANY(k.conkey)),\n"
        "  EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid = c.oid AND i.indisunique AND a.attnum = ANY(i.indkey)),\n"
        "  a.attgenerated::text\n"
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace\n"
        "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped\n"
        "LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum\n"
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition\n"
        "ORDER BY 1, a.attnum"
    ),
    "fks": (
        "SELECT cl.relname,\n"
        "  (SELECT string_agg(a.attname, ',' ORDER BY k.ord) FROM unnest(con.conkey) WITH ORDINALITY k(attnum, ord)\n"
        "     JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum),\n"
        "  pl.relname,\n"
        "  (SELECT string_agg(a.attname, ',' ORDER BY k.ord) FROM unnest(con.confkey) WITH ORDINALITY k(attnum, ord)\n"
        "     JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.attnum),\n"
        "  con.conname, pg_get_constraintdef(con.oid)\n"
        "FROM pg_constraint con\n"
        "JOIN pg_class cl ON cl.oid = con.conrelid JOIN pg_namespace n ON n.oid = cl.relnamespace\n"
        "JOIN pg_class pl ON pl.oid = con.confrelid JOIN pg_namespace pn ON pn.oid = pl.relnamespace\n"
        "WHERE con.contype = 'f' AND n.nspname = 'public' AND pn.nspname = 'public'\n"
        "  AND NOT cl.relispartition AND NOT pl.relispartition\n"
        "ORDER BY 1, 3, 2"
    ),
}

# 2026-11-13 CAPFIX: 캡처 사용자가 읽지 못하는 것과 그것에 기대는 것. pg_dump 는 담는 표마다 ACCESS SHARE 잠금을
# 걸고(표 SELECT 와 스키마 USAGE 가 필요하다. 데이터만 빼는 --exclude-table-data 로는 잠금이 남는다), 담는 시퀀스의
# 현재값을 SELECT 로 읽는다. 하나라도 막히면 덤프 전체가 실패한다(G2 F49-H, 119 의 schema security).
# 뷰와 구체화 뷰는 잠그지도 읽지도 않아 권한이 없어도 덤프된다(PG 17 실측). 권한은 역할을 받아 판정한다:
# 캡처는 자기 자신(current_user), 녹화 전 점검은 superuser 로 붙으므로 캡처 사용자 이름을 넘긴다.
_ACCESS_QUERIES = {
    "schemas": (
        "SELECT n.nspname, has_schema_privilege({role}, n.oid, 'USAGE')\n"
        "FROM pg_namespace n\n"
        "WHERE n.nspname <> 'information_schema' AND n.nspname !~ '^pg_'\n"
        "ORDER BY 1"
    ),
    # 2회차(리뷰 하): 행 수준 보안(RLS)이 켜진 표는 SELECT 가 있어도 pg_dump 가 "query would be affected by
    # row-level security policy" 로 덤프 전체를 멈춘다. 캡처 사용자가 superuser, BYPASSRLS 가 아니고 표 소유
    # 권한도 없으면(소유자라도 FORCE 면) 같은 자리로 뺀다(reason row_security). 분할 잎이면 뿌리 부모를 함께 낸다.
    "denied": (
        "SELECT n.nspname, c.relname, c.relkind::text,\n"
        "  CASE WHEN NOT has_table_privilege({role}, c.oid, 'SELECT') THEN 'no_select' ELSE 'row_security' END,\n"
        "  coalesce(rn.nspname, ''), coalesce(r.relname, '')\n"
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace\n"
        "LEFT JOIN pg_class r ON c.relispartition AND r.oid = pg_partition_root(c.oid)\n"
        "LEFT JOIN pg_namespace rn ON rn.oid = r.relnamespace\n"
        "WHERE c.relkind IN ('r', 'p', 'S') AND n.nspname <> 'information_schema' AND n.nspname !~ '^pg_'\n"
        "  AND (NOT has_table_privilege({role}, c.oid, 'SELECT')\n"
        "       OR (c.relkind IN ('r', 'p') AND c.relrowsecurity\n"
        "           AND NOT coalesce((SELECT a.rolsuper OR a.rolbypassrls FROM pg_roles a WHERE a.rolname = {role}), false)\n"
        "           AND (c.relforcerowsecurity OR NOT pg_has_role({role}, c.relowner, 'USAGE'))))\n"
        "ORDER BY 1, 2"
    ),
    # 담는 객체가 빼는 스키마나 표에 기대면(트리거가 부르는 함수, 외래 키가 가리키는 표, 뷰가 읽는 표) 덤프는
    # 되지만 복원 때 그 객체 생성이 깨진다. 객체가 속한 표(dep_rel)를 함께 내 빼는 표에 딸린 것은 거른다.
    "deps": (
        "SELECT DISTINCT dn.nspname AS dep_schema, coalesce(dc.relname, '') AS dep_rel,\n"
        "  rn.nspname AS ref_schema, CASE WHEN rc.relkind IN ('r', 'p') THEN rc.relname ELSE '' END AS ref_rel\n"
        "FROM (\n"
        "  SELECT d.classid, d.objid, d.objsubid, d.refclassid, d.refobjid, d.refobjsubid,\n"
        "    CASE d.classid WHEN 'pg_class'::regclass THEN d.objid\n"
        "      WHEN 'pg_constraint'::regclass THEN (SELECT x.conrelid FROM pg_constraint x WHERE x.oid = d.objid)\n"
        "      WHEN 'pg_trigger'::regclass THEN (SELECT x.tgrelid FROM pg_trigger x WHERE x.oid = d.objid)\n"
        "      WHEN 'pg_rewrite'::regclass THEN (SELECT x.ev_class FROM pg_rewrite x WHERE x.oid = d.objid)\n"
        "      WHEN 'pg_attrdef'::regclass THEN (SELECT x.adrelid FROM pg_attrdef x WHERE x.oid = d.objid)\n"
        "      WHEN 'pg_policy'::regclass THEN (SELECT x.polrelid FROM pg_policy x WHERE x.oid = d.objid) END AS dep_relid\n"
        "  FROM pg_depend d WHERE d.deptype = 'n'\n"
        ") d\n"
        "LEFT JOIN pg_class dc ON dc.oid = d.dep_relid\n"
        "LEFT JOIN pg_class rc ON d.refclassid = 'pg_class'::regclass AND rc.oid = d.refobjid\n"
        "JOIN pg_namespace dn ON dn.nspname = coalesce((SELECT n.nspname FROM pg_namespace n WHERE n.oid = dc.relnamespace),\n"
        "                                              (pg_identify_object(d.classid, d.objid, d.objsubid)).schema)\n"
        "JOIN pg_namespace rn ON rn.nspname = coalesce((SELECT n.nspname FROM pg_namespace n WHERE n.oid = rc.relnamespace),\n"
        "                                              (pg_identify_object(d.refclassid, d.refobjid, d.refobjsubid)).schema)\n"
        "WHERE dn.nspname <> 'information_schema' AND dn.nspname !~ '^pg_'\n"
        "  AND rn.nspname <> 'information_schema' AND rn.nspname !~ '^pg_'\n"
        "  AND (dn.nspname <> rn.nspname OR (rc.relkind IN ('r', 'p') AND d.dep_relid IS DISTINCT FROM rc.oid))\n"
        "ORDER BY 1, 2, 3, 4"
    ),
}


def catalog_queries(role: str | None = None) -> dict[str, str]:
    """카탈로그 질의 전부. role 이 없으면 접속한 사용자(current_user)의 권한으로 판정한다."""
    if role is None:
        lit = "current_user"
    elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", role):
        lit = f"'{role}'"
    else:
        raise ValueError(f"역할 이름이 이상하다: {role!r}")
    return {**_BASE_QUERIES, **{k: q.format(role=lit) for k, q in _ACCESS_QUERIES.items()}}


CATALOG_QUERIES = catalog_queries()


def _num(v: str) -> int | None:
    return None if v in ("", "\\N", None) else int(float(v))


def _unescape(v: str) -> str:
    """COPY TO STDOUT 텍스트 형식의 역슬래시 이스케이프를 푼다."""
    return re.sub(r"\\(.)", lambda m: {"t": "\t", "n": "\n", "r": "\r"}.get(m.group(1), m.group(1)), v or "")


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
    coltypes: dict[str, list[tuple[str, str]]] = {}
    colinfo: dict[str, list[dict]] = {}
    for p in rows("columns", 3):
        if p[0] not in tables:
            continue
        columns.setdefault(p[0], []).append(p[1])
        coltypes.setdefault(p[0], []).append((p[1], p[2]))
        # 옛 3열 카탈로그는 NULL 허용, 기본값, 제약, 생성 열이 없는 것으로 읽는다.
        colinfo.setdefault(p[0], []).append({
            "name": p[1], "type": _unescape(p[2]), "notnull": len(p) > 3 and p[3] == "t",
            "default": _unescape(p[4]) if len(p) > 4 else "", "check": len(p) > 5 and p[5] == "t",
            "unique": len(p) > 6 and p[6] == "t", "generated": len(p) > 7 and p[7] not in ("", "\\N")})
        if p[2].startswith(("timestamp", "date")):
            timecols.setdefault(p[0], []).append(p[1])
    fks: dict[str, list[dict]] = {}
    for p in rows("fks", 4):
        if p[0] not in tables:
            continue
        fks.setdefault(p[0], []).append(
            {"columns": p[1].split(","), "parent": p[2], "parent_columns": p[3].split(","),
             "name": p[4] if len(p) > 5 else None, "def": _unescape(p[5]) if len(p) > 5 else None})
    # 권한(2026-11-13 CAPFIX). 옛 카탈로그(이 질의가 없던 판)는 표가 있는 스키마를 "모름"(None)으로 두고 읽을 수
    # 있다고 본다. 그래야 옛 카탈로그의 계획이 종전과 같다.
    schemas: dict[str, bool | None] = {}
    for p in rows("schemas", 2):
        if p[1] in ("t", "f"):  # 머리줄은 거른다
            schemas[p[0]] = p[1] == "t"
    for info in tables.values():
        schemas.setdefault(info["schema"], None)
    denied: dict[str, str] = {}
    denied_reason: dict[str, str] = {}
    denied_root: dict[str, str] = {}
    for p in rows("denied", 3):
        if p[2] in ("r", "p", "S"):
            key = p[1] if p[0] == "public" else f"{p[0]}.{p[1]}"
            denied[key] = p[2]
            # 3열짜리 옛 카탈로그는 SELECT 없음뿐이었다.
            denied_reason[key] = p[3] if len(p) > 3 and p[3] in ("no_select", "row_security") else "no_select"
            if len(p) > 5 and p[5]:
                denied_root[key] = p[5] if p[4] == "public" else f"{p[4]}.{p[5]}"
    deps: list[dict] = []
    for p in rows("deps", 4):
        if p[0] in schemas and p[2] in schemas:  # 머리줄과 모르는 스키마는 거른다
            deps.append({"schema": p[0], "relation": p[1], "ref_schema": p[2], "ref_relation": p[3]})
    return {"tables": tables, "columns": columns, "coltypes": coltypes, "colinfo": colinfo, "timecols": timecols, "fks": fks,
            "schemas": schemas, "denied": denied, "denied_reason": denied_reason, "denied_root": denied_root,
            "deps": deps}


def load_catalog(d: Path) -> dict:
    return parse_catalog({k: (d / f"{k}.tsv").read_text() if (d / f"{k}.tsv").exists() else ""
                          for k in CATALOG_QUERIES})


def _rel_key(schema: str, name: str) -> str:
    return name if schema == "public" else f"{schema}.{name}"


def plan_access(cfg: dict, catalog: dict) -> tuple[list[dict], set[str], set[str]]:
    """덤프에서 통째로 빼는 스키마와 표, 데이터만 빼는 시퀀스(2026-11-13 CAPFIX).

    1. 스키마는 cfg["capture_schemas"] 에 이유와 함께 적힌 것만 담는다(not_listed). 비밀번호 회전 기록 같은
       인증, 비밀 계열 스키마가 새로 생겨도 사람이 담기로 정하기 전에는 녹화에 실리지 않는다. 스키마 단위의
       막음이다: 담는 스키마 안의 비밀 계열 표는 이 규칙이 아니라 plan_scope 의 secret_tables, secret_detect 가
       행을 비운다. 목록이 없는 옛 설정이면 모든 스키마가 대상이다.
    2. 캡처 사용자에게 USAGE 가 없는 스키마는 목록에 있어도 뺀다(no_usage). 목록에 있는데 못 읽으면 conflicts 에도 둔다.
    3. 담는 스키마 안에서 캡처 사용자가 SELECT 하지 못하는 표(분할 잎 포함)는 통째로 뺀다(no_select). 데이터만
       빼면 pg_dump 가 정의를 덤프하려고 거는 잠금에서 같은 권한 오류로 죽는다.
    4. SELECT 하지 못하는 시퀀스는 정의는 담고 현재값만 뺀다(no_select). 표의 기본값과 identity 가 그 시퀀스를
       부르므로 정의를 빼면 복원이 깨진다. 복원된 시퀀스는 시작값에서 다시 센다.
    5. 행 수준 보안이 캡처 사용자에게 걸리는 표는 SELECT 가 있어도 통째로 뺀다(row_security). pg_dump 가 그 표
       하나로 전체를 멈추기 때문이다.
    6. 못 읽는 분할 잎은 잎째 빼고 partition_root 에 뿌리 부모를 적는다. 확인은 잎 이름이나 뿌리 부모 이름 어느
       쪽에 적혀도 된다.
    빼는 것마다 cfg["excluded_schemas"], cfg["excluded_relations"] 에 이유가 적혀 있으면 acknowledged=True 다.
    녹화 전 점검은 acknowledged 가 아닌 것을 막는다. 캡처는 막지 않고 빼고 meta 에 남긴다.
    """
    listed = cfg.get("capture_schemas")
    ack_s, ack_r = cfg.get("excluded_schemas", {}), cfg.get("excluded_relations", {})
    excluded: list[dict] = []
    gone_schemas: set[str] = set()
    for sch, usage in sorted(catalog.get("schemas", {}).items()):
        if usage is False:
            reason = "no_usage"
        elif listed is not None and sch not in listed:
            reason = "not_listed"
        else:
            continue
        gone_schemas.add(sch)
        excluded.append({"object": sch, "kind": "schema", "reason": reason,
                         "acknowledged": sch in ack_s, "note": ack_s.get(sch)})
    gone_tables: set[str] = set()
    reasons, roots = catalog.get("denied_reason", {}), catalog.get("denied_root", {})
    for key, kind in sorted(catalog.get("denied", {}).items()):
        if (key.rpartition(".")[0] or "public") in gone_schemas:
            continue
        if kind != "S":
            gone_tables.add(key)
        entry = {"object": key, "kind": "sequence_data" if kind == "S" else "table", "relkind": kind,
                 "reason": reasons.get(key, "no_select"), "acknowledged": key in ack_r, "note": ack_r.get(key)}
        root = roots.get(key)
        if root is not None and root != key:
            # 2회차(리뷰 하): 못 읽는 분할 잎은 잎째 빠져 그 구간 데이터가 부모에서 사라진다. 날마다 새 잎이 생기는
            # 표면 잎마다 확인을 받을 수 없으므로 뿌리 부모 이름 하나로 확인을 받는다(이유도 거기에).
            entry["partition_root"] = root
            if not entry["acknowledged"] and root in ack_r:
                entry.update(acknowledged=True, note=ack_r[root])
        excluded.append(entry)
    return excluded, gone_schemas, gone_tables


SECRET_NOTE = "비밀 계열, 사용자 결정 11-16"


def secret_candidates(cfg: dict, catalog: dict) -> dict[str, list[str]]:
    """이름이나 열 꼴이 비밀 계열인 public 표와 그 근거(2026-11-16 사용자 결정, CAPFIX 2회차).

    cfg["secret_detect"]: table_name(표 이름 정규식), column_name(열 이름 정규식), column_type(그 열의 타입 정규식.
    문자열, bytea, json 처럼 값을 담는 타입만 본다: password_digit_count 같은 정책 숫자 열을 거르려고). 값은 보지 않는다.
    """
    det = cfg.get("secret_detect")
    if not det:
        return {}
    name_re = re.compile(det["table_name"], re.I)
    col_re = re.compile(det["column_name"], re.I)
    type_re = re.compile(det["column_type"], re.I)
    types = catalog.get("coltypes", {})
    out: dict[str, list[str]] = {}
    for t, info in catalog["tables"].items():
        if info["schema"] != "public":
            continue
        why = ["표 이름"] if name_re.search(t) else []
        why += [f"열 {c}:{ty}" for c, ty in types.get(t, []) if col_re.search(c) and type_re.search(ty)]
        if why:
            out[t] = why
    return out


TEXTY = re.compile(r"^(text|character varying|character|varchar|char)\b", re.I)


def _mask_value(col: dict, configured: str | None) -> tuple[str, str, str | None]:
    """(방식, SQL 식, 막을 이유). 막을 이유는 자동으로 정한 값이 제약을 지킨다고 장담할 수 없을 때."""
    ty = col["type"]
    if configured is not None:
        return "config", f"CAST({configured} AS {ty})", None
    if not col["notnull"]:
        return "null", f"CAST(NULL AS {ty})", None
    if col["default"]:
        return "default", f"CAST(({col['default']}) AS {ty})", None
    if col["unique"]:
        if TEXTY.match(ty):
            return "placeholder_unique", f"CAST('masked-' || row_number() OVER () AS {ty})", None
        return "placeholder_unique", f"CAST(NULL AS {ty})", "NOT NULL 이고 UNIQUE 인 문자열 밖 열이라 자동 가림값이 없다"
    if TEXTY.match(ty):
        value = f"CAST('masked' AS {ty})"
    elif ty.lower() == "bytea":
        value = "'\\x6d61736b6564'::bytea"
    elif ty.lower() in ("json", "jsonb"):
        value = f"CAST('{{}}' AS {ty})"
    else:
        return "placeholder", f"CAST(NULL AS {ty})", "NOT NULL 인데 문자열, bytea, json 이 아니라 자동 가림값이 없다"
    return "placeholder", value, ("그 열을 부르는 CHECK 제약이 있어 자동 가림값이 지킨다고 장담할 수 없다"
                                  if col["check"] else None)


def plan_masks(cfg: dict, catalog: dict, kept: list[str]) -> dict[str, list[dict]]:
    """행을 담는 표(kept)의 비밀 열을 값만 가린다(2026-11-16 지휘 세션 결정, 사용자 결정 11-16 의 취지).

    열 꼴에 걸린 열이 있는 표는 secret_reviewed_keep 에 있어야 행이 담긴다(없으면 표째 비운다, plan_scope). 그래서
    여기 오는 표는 사람이 담기로 한 표이고, 그 표에 나중에 생긴 비밀 열도 가린다.
    가리는 열: cfg["secret_columns"][표] 에 적힌 열(이유, 원하면 value), 그리고 secret_detect 의 열 꼴에 걸리는데
    secret_columns_keep("표.열", 비밀이 아닌 이유)에 없는 열(확인 안 됨, 녹화 전 점검이 막는다). 가림값은 NULL 허용이면
    NULL, 아니면 열 기본값, 없으면 고정 자리표시(문자열 'masked', UNIQUE 면 'masked-<행 번호>', bytea, json 은 그에 맞는
    값). 자동 값이 제약을 지킨다고 장담할 수 없으면 conflict 로 올리고 설정의 value 로 정하게 한다.
    """
    det = cfg.get("secret_detect")
    listed = cfg.get("secret_columns", {})
    keep = cfg.get("secret_columns_keep", {})
    col_re = re.compile(det["column_name"], re.I) if det else None
    type_re = re.compile(det["column_type"], re.I) if det else None
    out: dict[str, list[dict]] = {}
    for t in kept:
        conf = listed.get(t, {})
        for col in catalog.get("colinfo", {}).get(t, []):
            name = col["name"]
            hit = col_re is not None and col_re.search(name) and type_re.search(col["type"])
            if name not in conf and not (hit and f"{t}.{name}" not in keep):
                continue
            if col["generated"]:
                continue  # 생성 열은 COPY 로 내지 않는다(복원 때 다시 계산된다)
            spec = conf.get(name)
            reason = spec.get("reason") if isinstance(spec, dict) else spec
            value = spec.get("value") if isinstance(spec, dict) else None
            method, expr, block = _mask_value(col, value)
            out.setdefault(t, []).append({
                "table": t, "column": name, "type": col["type"], "method": method, "expr": expr,
                "acknowledged": name in conf, "note": (f"{SECRET_NOTE}: {reason}" if reason
                                                       else f"{SECRET_NOTE}, 목록에 없음. 열 꼴에 걸림"),
                "block": block})
    return out


def masked_select(catalog: dict, t: str, masks: list[dict], source: str) -> str:
    """source(표 이름이나 괄호 친 부질의)의 열을 표 순서대로 내되 가린 열은 가림값으로. 생성 열은 뺀다."""
    by = {m["column"]: m["expr"] for m in masks}
    cols = []
    for col in catalog["colinfo"][t]:
        if col["generated"]:
            continue
        q = '"' + col["name"].replace('"', '""') + '"'
        cols.append(f"{by[col['name']]} AS {q}" if col["name"] in by else f"_m.{q}")
    return f"SELECT {', '.join(cols)} FROM {source} _m"


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

    excluded, gone_schemas, gone_tables = plan_access(cfg, catalog)

    # 비밀 계열 표(2026-11-16 사용자 결정): 행을 담지 않고 스키마만(drop). secret_tables 에 이유와 함께 적힌 표와,
    # 이름이나 열 꼴이 비밀 계열인데 secret_reviewed_keep(검토해 담기로 한 표)에 없는 표. 뒤의 것은 사람이 정하기
    # 전이라도 녹화에 값이 실리지 않게 비우고(닫힌 쪽), 녹화 전 점검이 막는다.
    secret_cfg = cfg.get("secret_tables", {})
    keep_cfg = cfg.get("secret_reviewed_keep", {})
    detected = secret_candidates(cfg, catalog)
    secret_rule: dict[str, str] = {}
    for t in tables:
        if tables[t]["schema"] in gone_schemas or t in gone_tables:
            continue
        if t in secret_cfg:
            secret_rule[t] = "secret"
        elif t in detected and t not in keep_cfg and t not in drop:
            secret_rule[t] = "secret_detected"
    for t in sorted(secret_rule):
        # meta 의 note: 사람이 읽는 이유(사용자 결정 11-16). 목록의 표는 설정의 근거, 꼴로만 걸린 표는 걸린 꼴.
        why = (f"{SECRET_NOTE}: {secret_cfg[t]}" if t in secret_cfg
               else f"{SECRET_NOTE}, 목록에 없음. 이름이나 열 꼴: " + ", ".join(detected[t]))
        excluded.append({"object": t, "kind": "table_data", "relkind": tables[t]["relkind"], "reason": secret_rule[t],
                         "acknowledged": secret_rule[t] == "secret", "note": why})

    out: dict[str, dict] = {}
    for t, info in tables.items():
        base = {"table": t, "bytes": info["bytes"], "relkind": info["relkind"]}
        if info["schema"] in gone_schemas:
            out[t] = {**base, "scope": "excluded", "rule": "schema"}
        elif t in gone_tables:
            out[t] = {**base, "scope": "excluded", "rule": "no_select"}
        elif t in secret_rule:
            out[t] = {**base, "scope": "drop", "rule": secret_rule[t]}
        elif t in explicit_slice:
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

    # 빼는 것 때문에 녹화가 달라지거나 복원이 깨지는 경우(2026-11-13 CAPFIX). 사람이 설정을 고칠 일이다.
    for e in excluded:
        if e["kind"] == "schema" and e["reason"] == "no_usage" and e["object"] in cfg.get("capture_schemas", {}):
            conflicts.append({"table": e["object"], "problem": "capture_schemas 에 있는 스키마를 캡처 사용자가 읽지 못한다",
                              "parents": []})
    for t, e in sorted(out.items()):
        if e["scope"] == "excluded" and (t in explicit_slice or t in snap_ok):
            conflicts.append({"table": t, "problem": "설정이 담으라고 한 표를 캡처 사용자가 읽지 못해 녹화에서 빠진다",
                              "parents": []})
    for d in catalog.get("deps", []):
        dep = _rel_key(d["schema"], d["relation"]) if d["relation"] else None
        if d["schema"] in gone_schemas or dep in gone_tables:
            continue
        ref = _rel_key(d["ref_schema"], d["ref_relation"]) if d["ref_relation"] else None
        if d["ref_schema"] in gone_schemas:
            gone = f"스키마 {d['ref_schema']}"
        elif ref in gone_tables:
            gone = f"표 {ref}"
        else:
            continue
        conflicts.append({"table": dep or f"스키마 {d['schema']}",
                          "problem": f"녹화에서 빠지는 {gone} 에 기대어 복원 때 생성이 깨진다", "parents": []})

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
    for t, spec in cfg.get("slice_by_validity", {}).items():
        wanted += [(t, c) for c in spec["key"] + [spec["version"], spec["valid_from"], spec["active"]]]
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
    # 비밀 열 가리기(4회차): 행을 담는 표(snap, slice)의 비밀 열 값만 가린다. 통째로 담던 표는 pg_dump 에서 데이터를
    # 빼고 가린 SELECT 를 서버측 COPY 로 CSV 에 낸다(scope masked). 원문 비밀 값은 원천 PG 밖으로 나오지 않는다.
    # slice 표는 구간 질의를 감싸 같은 가림을 건다.
    masks = plan_masks(cfg, catalog, sorted(t for t, e in out.items() if e["scope"] in ("snap", "slice")))
    for t in masks:
        if out[t]["scope"] == "snap":
            out[t].update(scope="masked", rule=f"{out[t]['rule']}+masked")
        for m in masks[t]:
            if m["block"]:
                conflicts.append({"table": t, "problem": f"비밀 열 {m['column']}: {m['block']}. pg-scope.json "
                                  "secret_columns 에 value 로 가림값을 정하라", "parents": []})
    masked_tables = sorted(t for t, e in out.items() if e["scope"] == "masked")
    # 가린 표를 FK 로 가리키는 표의 제약은 pg_restore 때 부모가 비어 있어 생기지 않는다. restore.sql 이 가린 행을 실은
    # 뒤 다시 단다(정의는 원천 카탈로그에서).
    fk_readd = sorted(({"table": c, "name": fk["name"], "def": fk["def"], "parent": fk["parent"]}
                       for c, lst in fks.items() for fk in lst
                       if fk["parent"] in masked_tables and fk.get("def") and out.get(c, {}).get("scope")
                       in ("snap", "slice", "masked")), key=lambda x: (x["table"], x["name"]))

    # 비밀 계열로 비우는 표를 외래 키로 가리키는 표. slice 표면 복원의 구간 CSV 적재(\\copy)가 그 FK 에 걸려
    # 멈추므로 충돌. 통째로 담는 표면 pg_restore 가 그 FK 제약만 만들지 못하고 넘어간다(평가 restore.sh 는
    # pg_restore 오류로 멈추지 않는다, snap_fk_to_slice 와 같은 처지). 알림 목록에 둔다.
    fk_to_secret: list[dict] = []
    for t, e in sorted(out.items()):
        if e["scope"] not in ("snap", "slice", "masked"):
            continue
        refs = sorted({fk["parent"] for fk in fks.get(t, []) if fk["parent"] in secret_rule and fk["parent"] != t})
        if not refs:
            continue
        if e["scope"] == "slice":
            conflicts.append({"table": t, "problem": "slice 표가 비우는 비밀 계열 표를 외래 키로 가리킨다",
                              "parents": refs})
        else:
            fk_to_secret.append({"table": t, "parents": refs})
    for t in secret_rule:
        if t in explicit_slice or t in snap_ok:
            conflicts.append({"table": t, "problem": "설정이 담으라고 한 표가 비밀 계열이라 비운다", "parents": []})

    snap_fk_to_slice: list[dict] = []
    for t, e in out.items():
        if e["scope"] in ("snap", "masked"):
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
        # 덤프에서 빼는 스키마, 표, 시퀀스 현재값과 이유(2026-11-13 CAPFIX). meta.postgres_scope.excluded 로 간다.
        "excluded": excluded,
        "excluded_schemas": sorted(gone_schemas),
        "secret_tables": sorted(secret_rule),
        "fk_to_secret": fk_to_secret,
        "masks": masks,
        "masked_tables": masked_tables,
        "fk_readd": fk_readd,
    }


def snap_load(cfg: dict, plan: dict) -> dict:
    """통째로 덤프하는 표의 크기 합과 예산. 예산을 넘으면 pg_dump 가 제한 시간 안에 끝나지 않을 수 있다."""
    snaps = sorted(((e["bytes"] or 0, t) for t, e in plan["tables"].items() if e["scope"] in ("snap", "masked")),
                   reverse=True)
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
        "excluded": plan.get("excluded", []),
        "fk_to_secret": plan.get("fk_to_secret", []),
        # 복원 때 다시 달 외래 키 수(5회차). 복원 뒤 _capture_restore_report 의 fk_readd_attempted 와 같아야 한다.
        "fk_readd_count": len(plan.get("fk_readd", [])),
        # 값만 가린 비밀 열(4회차): 표, 열, 방식(null, default, placeholder, placeholder_unique, config), 이유.
        "masked_columns": [{k: m[k] for k in ("table", "column", "method", "acknowledged", "note")}
                           for t in sorted(plan.get("masks", {})) for m in plan["masks"][t]],
    }


def _pattern(name: str) -> str:
    """pg_dump 패턴의 이름 한 토막. 소문자 밑줄 이름은 그대로, 그 밖은 큰따옴표로(패턴은 따옴표 밖을 소문자로 읽는다)."""
    return name if re.fullmatch(r"[a-z_][a-z0-9_]*", name) else '"' + name.replace('"', '""') + '"'


def _qualified(t: str) -> str:
    # public 밖 스키마 표의 키는 이미 "스키마.표" 다.
    schema, _, name = t.rpartition(".")
    return f"{_pattern(schema or 'public')}.{_pattern(name)}"


def _exclude_flag(plan: dict | None, t: str) -> str:
    # 분할 표는 부모만 빼면 잎 분할의 데이터가 그대로 실린다(pg_dump 16 부터 -and-children).
    name = t if "." in t else f"public.{t}"
    if plan and plan["tables"].get(t, {}).get("relkind") == "p":
        return f"--exclude-table-data-and-children={name}"
    return f"--exclude-table-data={name}"


def emit_exclude_args(cfg: dict, plan: dict | None = None) -> str:
    gone = {t for t, e in plan["tables"].items() if e["scope"] == "excluded"} if plan else set()
    names = [t for t in scoped_tables(cfg) if t not in gone]
    if plan:
        names += [t for t in plan["auto_slices"] + plan["over_cap"] + plan.get("masked_tables", []) if t not in names]
    lines = [_exclude_flag(plan, t) for t in names]
    # 캡처 사용자가 못 읽거나 담지 않기로 한 것(2026-11-13 CAPFIX). 스키마와 표는 통째로(잠금도 걸지 않게),
    # 시퀀스는 현재값만 뺀다.
    for e in (plan or {}).get("excluded", []):
        if e["kind"] == "schema":
            lines.append(f"--exclude-schema={_pattern(e['object'])}")
        elif e["kind"] == "table":
            flag = "--exclude-table-and-children" if e.get("relkind") == "p" else "--exclude-table"
            lines.append(f"{flag}={_qualified(e['object'])}")
        elif e["kind"] == "table_data":
            # 비밀 계열: 스키마는 남기고 행만 뺀다(복원하면 빈 표). 분할 표는 잎 데이터까지.
            flag = "--exclude-table-data-and-children" if e.get("relkind") == "p" else "--exclude-table-data"
            line = f"{flag}={_qualified(e['object'])}"
            if line not in lines:
                lines.append(line)
        else:
            lines.append(f"--exclude-table-data={_qualified(e['object'])}")
    return "\n".join(lines)


def ordered_exports(cfg: dict, plan: dict | None = None, catalog: dict | None = None) -> list[tuple[str, str, str]]:
    """(표, 질의, scope). 가린 통째 표가 먼저(부모 쪽), 그다음 slice 순서 그대로. 가린 열이 있으면 질의를 감싼다."""
    items: list[tuple[str, str, str]] = []
    masks = (plan or {}).get("masks", {})
    for t in (plan or {}).get("masked_tables", []):
        items.append((t, masked_select(catalog, t, masks[t], f'public."{t}"'), "masked"))
    for t, q in ordered_slices(cfg, plan):
        if t in masks:
            q = masked_select(catalog, t, masks[t], f"(\n{q}\n)")
        items.append((t, q, "slice"))
    return items


def emit_export_sql(cfg: dict, out_dir: str, plan: dict | None = None, catalog: dict | None = None) -> str:
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
    for t, q, _ in ordered_exports(cfg, plan, catalog):
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
    for t in (plan or {}).get("masked_tables", []) + [t for t, _ in ordered_slices(cfg, plan)]:
        # 표 이름은 전부 소문자 무인용부호라 \copy 의 자체 파서로도 안전하다. 가린 통째 표가 먼저(부모 쪽).
        lines.append(f"\\copy {t} FROM '{t}.csv' WITH (FORMAT csv, HEADER true)")
    readd = (plan or {}).get("fk_readd", [])
    if (plan or {}).get("masked_tables"):
        # 가린 표(부모)는 pg_restore 때 비어 있어 그 표를 가리키는 FK 제약이 생기지 못했다. 행을 실은 뒤 다시 단다.
        # 이미 있으면 건너뛰고, 고아 행 등으로 실패해도 복원은 멈추지 않는다. 다만 조용히 넘기지 않는다(5회차):
        # 끝에 WARNING 한 줄 요약(시도, 새로 단 것, 이미 있던 것, 자식 표 없음, 실패 수와 실패한 표.제약, 첫 오류)을
        # 내고, 같은 수를 복원된 DB 의 작은 표 _capture_restore_report 에 남겨 복원 뒤에도 확인할 수 있게 한다.
        # 시도 수는 meta.postgres_scope.fk_readd_count 와 같아야 한다.
        def lit(v: str) -> str:
            return "'" + v.replace("'", "''") + "'"
        values = ",\n    ".join(f"({lit(r['table'])}, {lit(r['name'])}, {lit(r['def'])})" for r in readd)
        source = (f"(VALUES\n    {values}\n  ) v(child, conname, def)" if readd
                  else "(SELECT NULL::text, NULL::text, NULL::text WHERE false) v(child, conname, def)")
        lines += [
            "-- 가린 표를 가리키는 외래 키를 다시 단다(CAPFIX 4회차), 결과를 요약하고 남긴다(5회차).",
            "CREATE TABLE IF NOT EXISTS _capture_restore_report (item text PRIMARY KEY, value bigint NOT NULL, detail text,",
            "  recorded_at timestamptz NOT NULL DEFAULT now());",
            "DO $capfix$ DECLARE r record; n_try int := 0; n_add int := 0; n_had int := 0; n_gone int := 0;",
            "  failed text[] := '{}'; first_err text; BEGIN",
            f"  FOR r IN SELECT * FROM {source} LOOP",
            "    n_try := n_try + 1;",
            "    IF to_regclass(r.child) IS NULL THEN n_gone := n_gone + 1; CONTINUE; END IF;",
            "    IF EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conrelid = to_regclass(r.child) AND c.conname = r.conname) THEN",
            "      n_had := n_had + 1; CONTINUE;",
            "    END IF;",
            "    BEGIN",
            "      EXECUTE format('ALTER TABLE %s ADD CONSTRAINT %I %s', r.child, r.conname, r.def);",
            "      n_add := n_add + 1;",
            "    EXCEPTION WHEN others THEN",
            "      failed := failed || (r.child || '.' || r.conname);",
            "      first_err := coalesce(first_err, r.child || '.' || r.conname || ': ' || SQLERRM);",
            "    END;",
            "  END LOOP;",
            "  INSERT INTO _capture_restore_report (item, value, detail) VALUES",
            "    ('fk_readd_attempted', n_try, NULL), ('fk_readd_added', n_add, NULL), ('fk_readd_existing', n_had, NULL),",
            "    ('fk_readd_child_missing', n_gone, NULL),",
            "    ('fk_readd_failed', coalesce(array_length(failed, 1), 0), nullif(array_to_string(failed, ', '), '')),",
            "    ('fk_readd_first_error', coalesce(array_length(failed, 1), 0), first_err)",
            "  ON CONFLICT (item) DO UPDATE SET value = EXCLUDED.value, detail = EXCLUDED.detail, recorded_at = now();",
            "  RAISE WARNING 'CAPFIX_FK_READD_SUMMARY failed=% attempted=% added=% existing=% child_missing=% %',",
            "    coalesce(array_length(failed, 1), 0), n_try, n_add, n_had, n_gone,",
            "    CASE WHEN array_length(failed, 1) > 0",
            "      THEN 'failed_fks=' || array_to_string(failed, ', ') || ' first_error=' || first_err ELSE '' END;",
            "END $capfix$;",
        ]
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--emit", required=True,
                    choices=["exclude-args", "export-sql", "restore-sql", "slice-tables", "masked-tables",
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
    catalog = load_catalog(args.catalog) if args.catalog else None
    plan = plan_scope(cfg, catalog) if catalog else None
    if args.emit in ("plan-json", "plan-summary"):
        if plan is None:
            sys.exit("--emit plan-json, plan-summary 는 --catalog 가 필요하다")
        if args.emit == "plan-summary":
            # 캡처 로그에 남긴다(캡처는 이 요약을 한 번 부르고 stderr 는 capture-error.log 로 간다).
            for t in sorted(plan["masks"]):
                for m in plan["masks"][t]:
                    if not m["acknowledged"]:
                        print(f"[WARN] PostgreSQL column {t}.{m['column']} masked ({m['method']}) "
                              "(not acknowledged in pg-scope.json)", file=sys.stderr)
            for e in plan["excluded"]:
                print(f"[WARN] PostgreSQL {e['kind']} {e['object']} left out of the dump: {e['reason']}"
                      + ("" if e["acknowledged"] else " (not acknowledged in pg-scope.json)"), file=sys.stderr)
        print(json.dumps(plan if args.emit == "plan-json" else plan_summary(plan, cfg),
                         ensure_ascii=False, indent=1, sort_keys=True))
    elif args.emit == "exclude-args":
        print(emit_exclude_args(cfg, plan))
    elif args.emit == "export-sql":
        print(emit_export_sql(cfg, args.out_dir, plan, catalog))
    elif args.emit == "restore-sql":
        print(emit_restore_sql(cfg, plan))
    elif args.emit == "masked-tables":
        print("\n".join((plan or {}).get("masked_tables", [])))
    else:
        print("\n".join(t for t, _ in ordered_slices(cfg, plan)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
