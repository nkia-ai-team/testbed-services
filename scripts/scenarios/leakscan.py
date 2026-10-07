#!/usr/bin/env python3
"""케이스 input 데이터의 정답 누설 게이트.

[벤치마크 계약 §2.4](../../docs/spec-benchmark-contract.md)의 캡처 게이트 구현.
케이스가 소비자에게 나가는 묶음(`input/`)에 시나리오 정답이 남아 있는지 전수
검색한다. 하나라도 걸리면 그 케이스는 평가에 쓸 수 없다.

찾는 것 (2026-08-14 실측으로 확정된 누설면):

  scenario_id  F\\d\\d-[A-Z]\\d?          시나리오 식별자 그 자체
  inject_tag   rca-F\\d\\d-...            주입이 남긴 DB 세션 program/application_name
                                        (실측: dpm_session_local.body 의
                                         "program":"rca-F01-R-inventory-lock")
  inject_path  scenario-profile-state 등  주입 기구 경로
                                        (실측: process_meta.cmdline 에
                                         /var/lib/lucida/scenario-profile-state/F09-R.pgid)

오탐 주의 — `rca-testbed-*` 는 k8s 네임스페이스(정상 인프라 명명)다. `rca-` 단독
패턴을 쓰면 전 케이스가 거짓 불합격한다. 반드시 `rca-F\\d\\d-` 로 좁힌다.

사용:
  python3 leakscan.py <case_dir>                 # 케이스 전체(parquet + csv + 텍스트)
  python3 leakscan.py --own F01-R <case_dir>     # 자기 시나리오를 표시
  echo $?                                        # 0 = 합격, 1 = 누설
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import re
import sys
from pathlib import Path

# ai_model_state_snapshots.csv 는 state_meta jsonb 가 한 필드에 수 MB 로 실린다 —
# 기본 한도(128KB)면 스캔이 그 파일에서 죽는다(2026-08-20 실측).
csv.field_size_limit(sys.maxsize)

PATTERNS: dict[str, re.Pattern[str]] = {
    "scenario_id": re.compile(r"\bF\d{2}-[A-Z]\d?\b"),
    "inject_tag": re.compile(r"\brca-F\d{2}-[A-Za-z0-9_-]+"),
    "inject_path": re.compile(
        r"scenario-profile-state|scenario-runs|run-scenario\.sh|/opt/loadgen"
    ),
}

# 이 문자 중 하나도 없으면 어떤 패턴도 못 맞는다 — 값당 정규식 3회를 아낀다.
_PREFILTER = ("F", "rca-", "scenario")


def _hit(value: str, found, cols, samples, col: str) -> None:
    if not any(tok in value for tok in _PREFILTER):
        return
    for name, pat in PATTERNS.items():
        for m in pat.findall(value):
            found[name][m] += 1
            cols[name].add(col)
            samples.setdefault((name, m), value[:200])


def scan_parquet(path: Path, found, cols, samples) -> None:
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(path)
    str_cols = [
        f.name
        for f in pf.schema_arrow
        if "string" in str(f.type) or "binary" in str(f.type)
    ]
    if not str_cols:
        return
    for batch in pf.iter_batches(batch_size=20000, columns=str_cols):
        for col in batch.schema.names:
            for v in batch.column(col).to_pylist():
                if v:
                    _hit(v if isinstance(v, str) else str(v), found, cols, samples, col)


def scan_text(path: Path, found, cols, samples) -> None:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            _hit(line.rstrip("\n"), found, cols, samples, path.name)


def scan_csv(path: Path, found, cols, samples) -> None:
    with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
        for row in csv.reader(fh):
            for cell in row:
                if cell:
                    _hit(cell, found, cols, samples, path.name)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("case_dir", type=Path)
    ap.add_argument("--own", default=None, help="케이스 자신의 시나리오 id")
    ap.add_argument("--json", action="store_true", help="결과를 JSON 으로 출력")
    args = ap.parse_args()

    root = args.case_dir
    # 새 레이아웃이면 input/ 만, 옛 레이아웃이면 정답 파일을 빼고 본다.
    scope = root / "input" if (root / "input").is_dir() else root
    skip = {"golden.json", "scenario.md", "causality-audit.json", "meta.json"}

    found = {k: collections.Counter() for k in PATTERNS}
    cols: dict[str, set[str]] = collections.defaultdict(set)
    samples: dict[tuple[str, str], str] = {}
    scanned = 0

    for path in sorted(scope.rglob("*")):
        if not path.is_file() or path.name in skip or "answer" in path.parts:
            continue
        try:
            if path.suffix == ".parquet":
                scan_parquet(path, found, cols, samples)
            elif path.suffix == ".csv":
                scan_csv(path, found, cols, samples)
            elif path.suffix in {".json", ".jsonl", ".export", ".sql", ".txt"}:
                scan_text(path, found, cols, samples)
            else:
                continue
            scanned += 1
        except Exception as exc:  # 읽기 실패를 합격으로 넘기지 않는다
            print(f"[ERROR] {path}: {exc}", file=sys.stderr)
            return 2

    total = sum(sum(c.values()) for c in found.values())
    if args.json:
        print(
            json.dumps(
                {
                    "case": str(root),
                    "files_scanned": scanned,
                    "verdict": "clean" if total == 0 else "leak",
                    "hits": {k: dict(v) for k, v in found.items() if v},
                    "columns": {k: sorted(v) for k, v in cols.items() if v},
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if total == 0 else 1

    print(f"case      : {root}")
    print(f"scanned   : {scanned} files")
    if total == 0:
        print("verdict   : clean — 누설 0건")
        return 0
    print(f"verdict   : LEAK — {total} 건\n")
    for name, counter in found.items():
        if not counter:
            continue
        print(f"  [{name}] columns={sorted(cols[name])}")
        for tok, n in counter.most_common(10):
            mark = "  <-- 자기 시나리오" if args.own and tok == args.own else ""
            print(f"    {tok!r} x{n}{mark}")
            print(f"      e.g. {samples[(name, tok)][:130]}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
