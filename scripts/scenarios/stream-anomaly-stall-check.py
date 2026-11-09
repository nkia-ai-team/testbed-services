#!/usr/bin/env python3
"""녹화본의 녹화 구간 동안 119 stream-anomaly 산출이 멈췄는지 판정한다.

읽는 것은 녹화본 하나당 두 파일뿐이다.

- `meta.json` 의 capture_start, capture_end, t1, t2
- `data/clickhouse/lucida_events_local.parquet` 에서 녹화 구간 안의 행만
  (occurred_at 범위 조건을 parquet 읽기 필터로 건다. 열은 occurred_at, detector,
  attributes 만 읽는다)

VictoriaMetrics export(`*.bands` 시계열)는 녹화본마다 수백 MB 라 읽지 않는다. 밴드 쓰기가
`+Inf` 로 실패하면 워커가 재시작을 되풀이해 이벤트도 함께 끊기므로(장부 §6 의 기존 표시가
모두 이 꼴), 이벤트만으로 판정한다.

판정 근거:

- stream-anomaly 이벤트의 `attributes['metric']` 첫 마디로 경로를 나눈다(sms, nms, jvm, db,
  dpm 등). 정지 때 가장 먼저 멈추고 평소 가장 촘촘한 경로가 호스트 경로(sms, nms)다.
  평소 녹화본에서 sms 는 분당 약 10건, sms 와 nms 를 합친 최대 빈 구간은 6분 이하였다
  (v3 녹화본 125개 실측, 2026-11-09).
- 녹화 구간(capture_start~capture_end) 안에서 sms 와 nms 이벤트가 함께 하나도 없는 가장 긴
  구간(앞뒤 끝 포함)을 잰다. 30분 이상이면 정지, 10분 이상 30분 미만이면 애매로 둔다.
  sms 만 30분 이상 비고 nms 가 이어지면 애매로 둔다.
- 이벤트 표 자체가 녹화 구간을 덮지 못하면(어떤 탐지기든 첫 행이 capture_start 뒤 15분을
  넘거나 마지막 행이 capture_end 앞 15분보다 이르면) 녹화 쪽 문제일 수 있어 애매로 둔다.

출력은 녹화본마다 한 줄(TSV) 또는 --json 일 때 JSON 목록이다. 종료 코드는 0 이다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

HOST_FAMILIES = ("sms", "nms")
REPORT_FAMILIES = ("sms", "nms", "jvm", "db", "dpm")
STOP_MINUTES = 30
AMBIGUOUS_MINUTES = 10
COVERAGE_SLACK_MINUTES = 15

STOPPED = "stopped"
AMBIGUOUS = "ambiguous"
NORMAL = "normal"


def parse_utc(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def family_of(metric: str) -> str:
    return metric.split(".", 1)[0] if metric else ""


def longest_quiet(times, start: dt.datetime, end: dt.datetime):
    """[start, end] 안에서 이벤트가 없는 가장 긴 구간 (분, 시작, 끝). 앞뒤 끝을 포함한다."""
    points = [start] + sorted(t for t in times if start <= t <= end) + [end]
    best = (0.0, start, start)
    for a, b in zip(points, points[1:]):
        minutes = (b - a).total_seconds() / 60
        if minutes > best[0]:
            best = (minutes, a, b)
    return best


def classify(meta: dict, stream_events, any_event_range):
    """stream_events: (occurred_at, metric) 목록. any_event_range: 녹화 구간 안 모든 탐지기 행의 (최소, 최대) 또는 None."""
    cs, ce = parse_utc(meta["capture_start"]), parse_utc(meta["capture_end"])
    t1, t2 = parse_utc(meta["t1"]), parse_utc(meta["t2"])
    by_family: dict[str, list] = {}
    for at, metric in stream_events:
        if cs <= at <= ce:
            by_family.setdefault(family_of(metric), []).append(at)

    families = {}
    for name in REPORT_FAMILIES:
        times = sorted(by_family.get(name, []))
        families[name] = {
            "count": len(times),
            "in_fault_window": sum(1 for t in times if t1 <= t <= t2),
            "last": times[-1] if times else None,
        }

    host_times = [t for name in HOST_FAMILIES for t in by_family.get(name, [])]
    host_q = longest_quiet(host_times, cs, ce)
    sms_q = longest_quiet(by_family.get("sms", []), cs, ce)

    slack = dt.timedelta(minutes=COVERAGE_SLACK_MINUTES)
    covered = bool(any_event_range) and any_event_range[0] <= cs + slack and any_event_range[1] >= ce - slack

    if not covered:
        verdict, reason = AMBIGUOUS, "이벤트 표가 녹화 구간을 덮지 못함"
    elif host_q[0] >= STOP_MINUTES:
        verdict, reason = STOPPED, "sms, nms 이벤트가 함께 30분 이상 없음"
    elif host_q[0] >= AMBIGUOUS_MINUTES:
        verdict, reason = AMBIGUOUS, "sms, nms 빈 구간이 10분 이상 30분 미만"
    elif sms_q[0] >= STOP_MINUTES:
        verdict, reason = AMBIGUOUS, "sms 만 30분 이상 비고 nms 는 이어짐"
    else:
        verdict, reason = NORMAL, ""

    return {
        "verdict": verdict,
        "reason": reason,
        "capture_start": cs,
        "capture_end": ce,
        "t1": t1,
        "t2": t2,
        "host_quiet_minutes": round(host_q[0], 1),
        "host_quiet_from": host_q[1],
        "host_quiet_to": host_q[2],
        "quiet_covers_fault_window": host_q[1] <= t1 and host_q[2] >= t2 and host_q[0] >= STOP_MINUTES,
        "families": families,
    }


def read_case(case_dir: Path):
    import pyarrow.parquet as pq  # 시험에서는 읽기를 쓰지 않는다.

    meta = json.loads((case_dir / "meta.json").read_text())
    cs, ce = parse_utc(meta["capture_start"]), parse_utc(meta["capture_end"])
    path = case_dir / "data" / "clickhouse" / "lucida_events_local.parquet"
    in_range = [("occurred_at", ">=", cs), ("occurred_at", "<=", ce)]
    all_times = pq.read_table(path, columns=["occurred_at"], filters=in_range).column(0).to_pylist()
    any_range = (min(all_times), max(all_times)) if all_times else None
    table = pq.read_table(
        path,
        columns=["occurred_at", "attributes"],
        filters=in_range + [("detector", "=", "stream-anomaly")],
    )
    events = [
        (at, dict(attrs).get("metric", ""))
        for at, attrs in zip(table.column("occurred_at").to_pylist(), table.column("attributes").to_pylist())
    ]
    return meta, events, any_range


def _hm(value):
    return value.strftime("%m-%dT%H:%MZ") if value else "-"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cases", nargs="*", help="녹화본 폴더. 없으면 --root 아래 --contains 를 포함한 case-* 전부")
    ap.add_argument("--root", default="/data/eval-cases")
    ap.add_argument("--contains", default="-v3-")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.cases:
        dirs = [Path(c) for c in args.cases]
    else:
        dirs = sorted(p for p in Path(args.root).glob("case-*") if args.contains in p.name)

    results = []
    for case_dir in dirs:
        if not (case_dir / "meta.json").exists():
            results.append({"case": case_dir.name, "verdict": "skipped", "reason": "meta.json 없음(완성되지 않은 녹화본)"})
            continue
        meta, events, any_range = read_case(case_dir)
        row = classify(meta, events, any_range)
        row["case"] = case_dir.name
        results.append(row)

    if args.json:
        json.dump(results, sys.stdout, ensure_ascii=False, indent=1, default=lambda v: v.isoformat() if hasattr(v, "isoformat") else str(v))
        print()
        return 0
    for row in results:
        if row["verdict"] == "skipped":
            print(f"{row['case']}\tskipped\t{row['reason']}")
            continue
        fam = " ".join(
            f"{n}={f['count']}/{f['in_fault_window']}@{_hm(f['last'])}" for n, f in row["families"].items()
        )
        print(
            f"{row['case']}\t{row['verdict']}\t{row['host_quiet_minutes']}\t"
            f"{_hm(row['host_quiet_from'])}~{_hm(row['host_quiet_to'])}\t"
            f"covers_fault={int(row['quiet_covers_fault_window'])}\t{fam}\t{row['reason']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
