#!/usr/bin/env python3
"""케이스 input 데이터의 정답 누설 게이트.

[벤치마크 계약 §2.4](../../docs/spec-benchmark-contract.md)의 캡처 게이트 구현.
케이스가 소비자에게 나가는 묶음(`input/`)에 시나리오 정답이 남아 있는지 전수
검색한다. 하나라도 걸리면 그 케이스는 평가에 쓸 수 없다.

찾는 것 (2026-08-14 실측으로 확정된 누설면):

  scenario_id  F\\d\\d-[A-Z]\\d?          시나리오 식별자 그 자체. 대소문자를 가리지 않는다
                                        (2026-11-12 v4 LEAK: 주입 값은 `scenario-f03-h-...`,
                                         `/actuator/health/f16-h-fail` 처럼 소문자로 들어갔고
                                         옛 대문자 패턴은 이것을 합격시켰다)
  inject_tag   rca-F\\d\\d-...            주입이 남긴 DB 세션 program/application_name
                                        (실측: dpm_session_local.body 의
                                         "program":"rca-F01-R-inventory-lock"),
               rca-scenario, rca.lucida.ai/  실행기가 붙인 라벨 키와 주석 키
  inject_name  scenario-...               주입 자원 이름에 붙은 꼬리표(Job, ConfigMap, 이미지 태그,
                                        mock 응답 값). scenario-profile-state, scenario-runs 는
                                        inject_path 가 센다
  inject_path  scenario-profile-state 등  주입 기구 경로와 도구 명령줄
                                        (실측: process_meta.cmdline 에
                                         /var/lib/lucida/scenario-profile-state/F09-R.pgid,
                                         v4 C6b 리뷰: signal.alarm, os.execvp, mysqlsh ... --py,
                                         tcp_rmem=)

오탐 주의 — `rca-testbed-*` 는 k8s 네임스페이스(정상 인프라 명명)다. `rca-` 단독
패턴을 쓰면 전 케이스가 거짓 불합격한다. 반드시 `rca-F\\d\\d-` 로 좁힌다.

보지 않는 파일: 정답 파일(golden.json, scenario.md, causality-audit.json, meta.json, answer/)과 캡처 메타
topology/manifest.json(case_id, run_id 가 들어 있고 평가 복원이 제품에 넣지 않는다).

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
    # 앞뒤가 영숫자가 아니어야 한다(밑줄, 하이픈, 점, 슬래시는 경계). 대소문자 무시.
    "scenario_id": re.compile(r"(?i)(?<![a-z0-9])F\d{2}-[A-Z]\d?(?![a-z0-9])"),
    "inject_tag": re.compile(
        r"(?i)(?<![a-z0-9])rca-F\d{2}-[A-Za-z0-9_-]+|(?<![a-z0-9])rca-scenario(?![a-z0-9])"
        r"|(?<![a-z0-9])rca\.lucida\.ai/"
    ),
    "inject_name": re.compile(r"(?i)(?<![a-z0-9])scenario-(?!profile-state|runs(?![a-z0-9]))[a-z0-9]+"),
    # v4 C6b 리뷰 3회차 하 2: 주입 도구의 명령줄(파드 안 기동기의 알람과 exec, 셸 이름, 수신 버퍼 sysctl)도
    # process_meta 에 찍히면 주입 기구다.
    "inject_path": re.compile(
        r"scenario-profile-state|scenario-runs|run-scenario\.sh|/opt/loadgen"
        r"|signal\.alarm|os\.execvp|\bmysqlsh\b.*--py|tcp_rmem="
    ),
}

# 어떤 패턴이든 맞으려면 이 느슨한 식이 먼저 맞아야 한다. 대소문자 무시 패턴이 생겨 옛 글자
# 거름("F" 포함)은 거의 모든 값을 통과시킨다. parquet 은 이 식을 pyarrow(RE2, 앞뒤 보기 없음)로
# 열 단위로 먼저 걸러 파이썬으로 넘기는 값을 줄이고, 텍스트와 csv 는 줄 단위로 먼저 본다.
# `rca-` 와 `alarm` 을 홀로 두지 않는다: 네임스페이스 rca-testbed-* 와 알람 데이터가 거의 모든 행에 있어
# 거름이 아무것도 거르지 못한다(2026-11-12 실측: otel_traces 46MB 에 162초).
_LOOSE = (
    r"f\d\d-[a-z]|rca-(?:f\d|scenario)|rca\.lucida|scenario|signal\.alarm|execvp|mysqlsh|tcp_rmem|/opt/loadgen"
)
_LOOSE_RE = re.compile(_LOOSE, re.IGNORECASE)
# 같은 식을 소문자 글에 대소문자 구분으로 돌리면 큰 글에서 약 18배 빠르다(VM export 40MB: 3.8초 → 0.2초).
_LOOSE_LOWER_RE = re.compile(_LOOSE)


def _hit(value: str, found, cols, samples, col: str) -> None:
    if not _LOOSE_RE.search(value):
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
            for v in _loose_candidates(batch.column(col)):
                if v:
                    _hit(v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v), found, cols, samples, col)


def _string_leaves(array):
    """맵, 목록, 구조체를 풀어 문자열과 바이너리 배열만. 맵은 키와 값 둘 다."""
    import pyarrow as pa

    kind = array.type
    if pa.types.is_dictionary(kind):
        yield from _string_leaves(array.cast(kind.value_type))
    elif pa.types.is_map(kind):
        array = pa.concat_arrays([array])  # keys and items ignore a slice; a fresh copy is exactly the rows
        yield from _string_leaves(array.keys)
        yield from _string_leaves(array.items)
    elif pa.types.is_list(kind) or pa.types.is_large_list(kind) or pa.types.is_fixed_size_list(kind):
        yield from _string_leaves(array.flatten())
    elif pa.types.is_struct(kind):
        for index in range(kind.num_fields):
            yield from _string_leaves(array.field(index))
    elif pa.types.is_fixed_size_binary(kind):
        yield array.cast(pa.binary())
    elif pa.types.is_string(kind) or pa.types.is_large_string(kind) or pa.types.is_binary(kind) or pa.types.is_large_binary(kind):
        yield array


def _loose_candidates(array) -> list:
    """느슨한 식에 맞는 값만, 열 안의 맵과 목록은 풀어서. pyarrow 가 다루지 못하는 형이면 그 열 전체를
    돌려준다(파이썬이 다시 본다). 2026-11-12 전에는 맵, 목록, 고정 길이 바이너리 열(otel 의 trace_id,
    span_attributes 등)을 값마다 파이썬 문자열로 바꿔 봐서 otel_traces 한 파일에 160초가 걸렸다."""
    import pyarrow as pa
    import pyarrow.compute as pc

    try:
        if isinstance(array, pa.ChunkedArray):
            array = array.combine_chunks()
        out: list = []
        for leaf in _string_leaves(array):
            mask = pc.match_substring_regex(leaf, pattern=_LOOSE, ignore_case=True)
            out.extend(pc.filter(leaf, mask).to_pylist())
        return out
    except (pa.ArrowNotImplementedError, pa.ArrowInvalid, pa.ArrowTypeError):
        return array.to_pylist()


_TEXT_BLOCK = 1 << 24  # 글자 수. 시험이 줄여서 덩어리 경계를 지나는 줄을 본다.


def scan_text(path: Path, found, cols, samples) -> None:
    """줄마다 본다. 큰 덩어리를 느슨한 식으로 한 번 훑어 맞은 줄만 꺼낸다(한 줄에 여러 번 맞아도 한 번).
    느슨한 식에 안 맞는 줄은 _hit 이 어차피 버리므로 결과는 줄을 하나씩 보던 때와 같다."""
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        tail = ""
        while True:
            block = fh.read(_TEXT_BLOCK)
            if not block:
                break
            text = tail + block
            cut = text.rfind("\n") + 1
            if cut == 0:
                tail = text
                continue
            text, tail = text[:cut], text[cut:]
            _scan_lines(text, found, cols, samples, path.name)
        if tail:
            _scan_lines(tail, found, cols, samples, path.name)


def _scan_lines(text: str, found, cols, samples, col: str) -> None:
    done = -1
    lowered = text.lower()
    # 소문자로 바꿔 길이가 달라지는 글자(유니코드 일부)가 있으면 자리가 어긋나므로 느린 식으로 본다.
    matches = _LOOSE_LOWER_RE.finditer(lowered) if len(lowered) == len(text) else _LOOSE_RE.finditer(text)
    for match in matches:
        if match.start() < done:
            continue
        start = text.rfind("\n", 0, match.start()) + 1
        end = text.find("\n", match.start())
        end = len(text) if end < 0 else end
        _hit(text[start:end], found, cols, samples, col)
        done = end


def scan_csv(path: Path, found, cols, samples) -> None:
    with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
        for row in csv.reader(fh):
            if not _LOOSE_RE.search("\x00".join(row)):
                continue
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
    # topology/manifest.json 은 러너가 모은 토폴로지 묶음의 목록(case_id, run_id, 수집 실패 기록)이다.
    # 평가 복원(eval-incident-rca restore.sh, runbook-eval-case-restore.md)은 meta.json, data/clickhouse,
    # data/postgres(.dump, csv), data/victoriametrics.export 만 제품에 넣고 topology/ 는 읽지 않는다
    # (2026-11-12 v4 LEAK 2회차 확인). 제품 입력이 아닌 캡처 메타라 보지 않는다. 같은 폴더의 graph/,
    # service-tree/ 스냅샷은 제품 API 응답이라 계속 본다.
    skip_paths = {("topology", "manifest.json")}

    found = {k: collections.Counter() for k in PATTERNS}
    cols: dict[str, set[str]] = collections.defaultdict(set)
    samples: dict[tuple[str, str], str] = {}
    scanned = 0

    for path in sorted(scope.rglob("*")):
        if not path.is_file() or path.name in skip or "answer" in path.parts:
            continue
        if (path.parent.name, path.name) in skip_paths:
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
