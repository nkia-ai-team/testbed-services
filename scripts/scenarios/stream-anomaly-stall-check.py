#!/usr/bin/env python3
"""녹화본의 녹화 구간 동안 119 stream-anomaly 산출이 멈췄는지 판정한다.

읽는 것은 녹화본 하나당 세 파일이다.

- `meta.json` 의 capture_start, capture_end, t1, t2
- `data/clickhouse/lucida_events_local.parquet` 에서 녹화 구간 안의 행만
  (occurred_at 범위 조건을 parquet 읽기 필터로 건다. 열은 occurred_at, detector,
  attributes 만 읽는다)
- `data/victoriametrics.export` 에서 이름이 `sms.*.bands`, `nms.*.bands` 인 줄만. 줄 머리의
  `__name__` 만 보고 거르며, 고른 줄의 timestamps 중 녹화 구간 안의 것만 분 단위로 센다.
  나머지 줄은 파싱하지 않는다(녹화본 하나에 수 초).

판정 근거:

- stream-anomaly 이벤트의 `attributes['metric']` 첫 마디로 경로를 나눈다(sms, nms, jvm, db,
  dpm 등). 정지 때 가장 먼저 멈추는 경로가 호스트 경로(sms, nms)다. 평소 녹화본에서 sms 와
  nms 를 합친 이벤트 최대 빈 구간은 6분 이하였다(v3 녹화본 125개 실측, 2026-11-09). 경로별
  이벤트 밀도는 시기마다 달라(sms 가 10분에 2~8건뿐인 정상 녹화도 있다) 한 경로의 이벤트만으로는
  정지를 가르지 못하므로 두 경로를 합쳐 본다.
- 녹화 구간(capture_start~capture_end) 안에서 sms 와 nms 이벤트가 함께 하나도 없는 가장 긴
  구간(앞뒤 끝 포함)을 잰다. 10분 이상 30분 미만이면 애매로 둔다. sms 만 30분 이상 비고
  nms 가 이어지면 애매로 둔다.
- 이벤트 빈 구간이 30분 이상이면 탐지기 출력인 밴드를 함께 본다. sms 와 nms 밴드가 둘 다
  녹화 구간 안에서 30분 이상 끊겼으면 정지다. 한 경로라도 밴드가 30분 넘게 끊기지 않았으면
  그 경로의 탐지기는 돌고 있었고 이벤트가 드물었을 뿐이므로 정지로 보지 않고 애매로 둔다.
  밴드 파일이 없으면 이벤트만으로 정지로 두고 사유에 적는다.
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


def classify(meta: dict, stream_events, any_event_range, band_gaps=None):
    """stream_events: (occurred_at, metric) 목록. any_event_range: 녹화 구간 안 모든 탐지기 행의 (최소, 최대) 또는 None.
    band_gaps: {"sms": 분, "nms": 분} 녹화 구간 안 밴드가 끊긴 가장 긴 구간, 밴드 파일이 없으면 None."""
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
        if band_gaps is None:
            verdict, reason = STOPPED, "sms, nms 이벤트가 함께 30분 이상 없음(밴드 파일 없음, 이벤트만으로 판정)"
        elif all(band_gaps.get(name, 0) >= STOP_MINUTES for name in HOST_FAMILIES):
            verdict, reason = STOPPED, "sms, nms 이벤트가 함께 30분 이상 없고 두 경로 밴드도 30분 이상 끊김"
        else:
            alive = ", ".join(n for n in HOST_FAMILIES if band_gaps.get(n, 0) < STOP_MINUTES)
            verdict, reason = AMBIGUOUS, f"이벤트는 30분 이상 비었으나 {alive} 밴드가 이어져 그 경로 탐지기는 동작"
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
        "band_gaps": band_gaps,
        "quiet_covers_fault_window": host_q[1] <= t1 and host_q[2] >= t2 and host_q[0] >= STOP_MINUTES,
        "families": families,
    }


def band_gap_minutes(export_path: Path, start: dt.datetime, end: dt.datetime):
    """VM export 에서 sms, nms 밴드가 녹화 구간 안에서 끊긴 가장 긴 구간(분). 앞뒤 끝을 포함한다."""
    start_ms, end_ms = start.timestamp() * 1000, end.timestamp() * 1000
    total = int((end_ms - start_ms) // 60000) + 1
    minutes = {name: set() for name in HOST_FAMILIES}
    with open(export_path, "rb") as fh:
        for line in fh:
            head = line[:200]
            i = head.find(b'"__name__":"')
            if i < 0:
                continue
            j = head.find(b'"', i + 12)
            name = head[i + 12 : j].decode(errors="replace")
            family = name.split(".", 1)[0]
            if family not in minutes or not name.endswith(".bands"):
                continue
            k = line.rfind(b'"timestamps":[')
            for ts in json.loads(line[k + 13 : line.index(b"]", k) + 1]):
                if start_ms <= ts <= end_ms:
                    minutes[family].add(int((ts - start_ms) // 60000))
    gaps = {}
    for name, seen in minutes.items():
        points = [-1] + sorted(seen) + [total]
        gaps[name] = max(b - a - 1 for a, b in zip(points, points[1:]))
    return gaps


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
    export = case_dir / "data" / "victoriametrics.export"
    bands = band_gap_minutes(export, cs, ce) if export.exists() else None
    return meta, events, any_range, bands


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
        meta, events, any_range, bands = read_case(case_dir)
        row = classify(meta, events, any_range, bands)
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
            f"covers_fault={int(row['quiet_covers_fault_window'])}\tbands={row['band_gaps']}\t{fam}\t{row['reason']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
