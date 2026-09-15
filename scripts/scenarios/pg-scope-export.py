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
"""
from __future__ import annotations

import argparse
import json
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


def root_cte(table: str, spec: dict) -> str:
    """창 안의 뿌리 + 자기참조 폐포.

    superseded_by·merged_into 는 창 밖(대개 미래)을 가리킬 수 있어, 그대로 두면
    복원 때 FK 가 깨진다. 재귀로 참조 대상을 함께 싣는다.
    """
    tc, sr = spec["time_column"], spec["self_reference"]
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


def ordered_slices(cfg: dict) -> list[tuple[str, str]]:
    """(표, 질의) 를 복원 가능한 순서로. 뿌리 → 자식 → 손자 → 시간슬라이스."""
    items: list[tuple[str, str]] = []

    for t, spec in cfg["slice_roots"].items():
        items.append((t, f'{root_cte(t, spec)}\nSELECT t.* FROM public."{t}" t '
                         f'WHERE t.id IN (SELECT id FROM _keep)'))

    for t, spec in cfg["slice_children"].items():
        root = spec["root"]
        items.append((t, f'{root_cte(root, cfg["slice_roots"][root])}\n'
                         f'SELECT c.* FROM public."{t}" c '
                         f'WHERE c."{spec["column"]}" IN (SELECT id FROM _keep)'))

    for t, spec in cfg["slice_grandchildren"].items():
        parent, col, pkey = spec["parent"], spec["column"], spec["parent_key"]
        proot = cfg["slice_children"][parent]["root"]
        pcol = cfg["slice_children"][parent]["column"]
        items.append((t, f'{root_cte(proot, cfg["slice_roots"][proot])}\n'
                         f'SELECT g.* FROM public."{t}" g WHERE g."{col}" IN ('
                         f'SELECT p."{pkey}" FROM public."{parent}" p '
                         f'WHERE p."{pcol}" IN (SELECT id FROM _keep))'))

    for t, col in cfg["slice_by_time"].items():
        items.append((t, f'SELECT t.* FROM public."{t}" t '
                         f'WHERE t."{col}" >= :\'win_start\'::timestamptz '
                         f'AND t."{col}" <= :\'win_end\'::timestamptz'))

    # epoch millis 컬럼(bigint)은 timestamptz 비교가 안 된다 — 창을 epoch 로 옮긴다.
    for t, col in cfg.get("slice_by_epoch_ms", {}).items():
        items.append((t, f'SELECT t.* FROM public."{t}" t '
                         f'WHERE t."{col}" >= (extract(epoch FROM :\'win_start\'::timestamptz) * 1000)::bigint '
                         f'AND t."{col}" <= (extract(epoch FROM :\'win_end\'::timestamptz) * 1000)::bigint'))
    return items


def emit_exclude_args(cfg: dict) -> str:
    return "\n".join(f"--exclude-table-data=public.{t}" for t in scoped_tables(cfg))


def emit_export_sql(cfg: dict, out_dir: str) -> str:
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
    for t, q in ordered_slices(cfg):
        lines.append(f"-- {t}")
        lines.append(f"\\o {d}/{t}.csv")
        lines.append(f"COPY ({q}) TO STDOUT WITH (FORMAT csv, HEADER true);")
        lines.append("\\o")
        lines.append("")
    return "\n".join(lines)


def emit_restore_sql(cfg: dict) -> str:
    lines = [
        "-- 케이스 동봉 스크립트. pg_restore 로 postgres.dump 를 복원한 **뒤에** 돌린다.",
        "-- 이 디렉터리(data/postgres/)를 cwd 로 두고:  psql -U lucida -d lucida -f restore.sql",
        "-- 순서가 곧 FK 순서다 — 바꾸지 말 것.",
        "\\set ON_ERROR_STOP on",
        "",
    ]
    for t, _ in ordered_slices(cfg):
        # 표 이름은 전부 소문자 무인용부호라 \copy 의 자체 파서로도 안전하다.
        lines.append(f"\\copy {t} FROM '{t}.csv' WITH (FORMAT csv, HEADER true)")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--emit", required=True,
                    choices=["exclude-args", "export-sql", "restore-sql", "slice-tables"])
    ap.add_argument("--out-dir", default="/out/postgres",
                    help="export-sql 이 CSV 를 쓸 경로(psql 이 보는 경로)")
    args = ap.parse_args()

    cfg = load(args.config)
    if args.emit == "exclude-args":
        print(emit_exclude_args(cfg))
    elif args.emit == "export-sql":
        print(emit_export_sql(cfg, args.out_dir))
    elif args.emit == "restore-sql":
        print(emit_restore_sql(cfg))
    else:
        print("\n".join(t for t, _ in ordered_slices(cfg)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
