#!/usr/bin/env python3
"""판이 쌓이는 모델 상태 표를 창으로 자른 녹화가 맞는지 대조한다(v4 R1, 사용자 결정 Q10).

pg-scope.json slice_by_validity 의 표(지금은 trainer_trace_window_threshold)는 캡처가 "키마다 창 시작 때
유효했던 판 + 창 안에서 만든 판 + 캡처 때 활성인 판" 만 담는다. 재생에 필요한 것은 창 시작 때의 판이므로,
그 판을 녹화본 CSV 와 원천 표에서 각각 뽑아 같아야 하고, 녹화본의 키(에이전트) 수가 원천의 고유 키 수와
같아야 한다.

첫 v4 녹화본에서:

    # 1) 원천 쪽 집계 질의를 뽑는다(창 시작 시각은 녹화본 meta.json 의 capture_start)
    python3 threshold-slice-check.py --print-source-sql --window-start <capture_start> > src.sql
    # 2) 119 PG 에서 읽기 전용으로 돌려 탭 구분으로 받는다(q119-pg.sh 는 머리줄을 붙여도 된다)
    bash q119-pg.sh "$(cat src.sql)" > src.tsv
    # 3) 녹화본 CSV 와 대조한다. 종료 코드 0 = 같음, 1 = 다름
    python3 threshold-slice-check.py --csv <case>/data/postgres/trainer_trace_window_threshold.csv \\
        --window-start <capture_start> --source-tsv src.tsv

원천 질의는 판마다 행 수와 rule_id 목록의 md5 만 돌려준다(표 본문을 옮기지 않는다). 녹화 뒤 원천에 새 판이
생겨도 창 시작 때의 판은 바뀌지 않으므로 대조는 녹화 뒤 언제 해도 된다(행을 지우는 정리 작업이 없다는 전제).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent / "pg-scope.json"
TABLE = "trainer_trace_window_threshold"
ROW_ID = "rule_id"


def spec_of(config: Path, table: str) -> dict:
    return json.loads(config.read_text())["slice_by_validity"][table]


def valid_at_start(rows: list[dict], spec: dict, window_start: str) -> dict[tuple, dict]:
    """키마다 창 시작 때 유효했던 판: {키: {"version", "rows", "ids_md5"}}.

    rows 의 시각은 같은 꼴의 ISO 문자열이거나 비교 가능한 값이어야 한다.
    """
    key, v, vf = spec["key"], spec["version"], spec["valid_from"]
    best: dict[tuple, int] = {}
    for r in rows:
        if _ts(r[vf]) <= _ts(window_start):
            k = tuple(r[c] for c in key)
            best[k] = max(best.get(k, -1), int(r[v]))
    out: dict[tuple, dict] = {}
    for k, ver in best.items():
        ids = sorted(r[ROW_ID] for r in rows if tuple(r[c] for c in key) == k and int(r[v]) == ver)
        out[k] = {"version": ver, "rows": len(ids), "ids_md5": hashlib.md5(",".join(ids).encode()).hexdigest()}
    return out


def keys_of(rows: list[dict], spec: dict) -> set[tuple]:
    return {tuple(r[c] for c in spec["key"]) for r in rows}


def _ts(value: str) -> str:
    # PG CSV 는 "2026-11-10 01:02:03.456+00", 인자는 "2026-11-10T01:02:03Z". 같은 꼴로 맞춘다(UTC 전제).
    s = str(value).replace("T", " ").replace("Z", "+00")
    if "+" not in s[10:]:
        s += "+00"
    head, tz = s.rsplit("+", 1)
    if "." not in head:
        head += ".000000"
    whole, frac = head.split(".")
    return f"{whole}.{frac.ljust(6, '0')[:6]}+{tz}"


def source_sql(spec: dict, window_start: str, table: str = TABLE) -> str:
    keys = ", ".join(spec["key"])
    v, vf = spec["version"], spec["valid_from"]
    return (
        f"WITH pick AS (SELECT {keys}, max({v}) AS {v} FROM public.{table} "
        f"WHERE {vf} <= '{window_start}'::timestamptz GROUP BY {keys}) "
        f"SELECT 'gen' AS kind, {keys}, t.{v}::text, count(*)::text, "
        f"md5(string_agg(t.{ROW_ID}, ',' ORDER BY t.{ROW_ID})) "
        f"FROM public.{table} t JOIN pick p USING ({keys}, {v}) GROUP BY {keys}, t.{v} "
        f"UNION ALL SELECT 'keys', count(DISTINCT ({keys}))::text, '', '', '' FROM public.{table}"
    )


def parse_source(text: str, spec: dict) -> tuple[dict[tuple, dict], int]:
    n = len(spec["key"])
    gens, total = {}, None
    for line in text.splitlines():
        p = line.split("\t")
        if p[0] == "gen":
            k = tuple(p[1:1 + n])
            gens[k] = {"version": int(p[1 + n]), "rows": int(p[2 + n]), "ids_md5": p[3 + n]}
        elif p[0] == "keys":
            total = int(p[1])
    if total is None:
        raise ValueError("원천 결과에 keys 줄이 없다")
    return gens, total


def compare(recorded: list[dict], spec: dict, window_start: str,
            source_gens: dict[tuple, dict], source_keys: int) -> list[str]:
    problems = []
    mine = valid_at_start(recorded, spec, window_start)
    if mine != source_gens:
        for k in sorted(set(mine) | set(source_gens)):
            if mine.get(k) != source_gens.get(k):
                problems.append(f"창 시작 때 판이 다르다: {k}: 녹화본 {mine.get(k)} 원천 {source_gens.get(k)}")
    rec_keys = len(keys_of(recorded, spec))
    if rec_keys != source_keys:
        problems.append(f"키 수가 다르다: 녹화본 {rec_keys}, 원천 고유 키 {source_keys}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--window-start", required=True)
    ap.add_argument("--print-source-sql", action="store_true")
    ap.add_argument("--csv", type=Path)
    ap.add_argument("--source-tsv", type=Path)
    a = ap.parse_args()
    spec = spec_of(a.config, TABLE)
    if a.print_source_sql:
        print(source_sql(spec, a.window_start))
        return 0
    if not (a.csv and a.source_tsv):
        sys.exit("--csv 와 --source-tsv 가 필요하다")
    with a.csv.open(newline="") as f:
        recorded = list(csv.DictReader(f))
    gens, total = parse_source(a.source_tsv.read_text(), spec)
    problems = compare(recorded, spec, a.window_start, gens, total)
    if problems:
        print("\n".join(problems))
        return 1
    print(f"같음: 키 {total}개, 창 시작 때 판 {len(gens)}개")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
