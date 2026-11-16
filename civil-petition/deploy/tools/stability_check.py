#!/usr/bin/env python3
"""평시 24시간 안정 판정(설계 docs/spec-testbed-vm-3tier.md §1 Q3, §10 T2 "평시 24시간").

입력:
  --windows   부하기 모니터가 쓰는 5분 창 기록(JSON 줄, loadgen_monitor.py --window-log)
  --facts-before, --facts-after  선택. host-facts.sh 가 서버마다 낸 JSON 줄을 모은 파일 둘(창 처음과 끝).
               재시작 수는 두 값의 차, OOM 과 chrony 오프셋은 끝 값을 본다. 부팅 시각이 다르면(창 안 재부팅)
               실패로 센다.

기준(설계 값, 바꾸지 않는다):
  - 전체 비정상 비율(5xx 와 응답 없음(시간 초과, 연결 실패)의 합 / 전체 요청) 0.1% 이하.
    설계 Q3 의 문구는 "5xx와 시간 초과" 이고, 연결 실패도 사용자 비정상(§6-3)이라 함께 센다(더 엄격한 쪽).
  - 5분 창의 99% 이상에서 그 창의 비정상 비율 0.5% 이하. 기록이 없는 창(부하가 멈춘 창)은 못 지킨 창으로 센다.
  - 피크(KST 15~17시) 창들의 p95 최댓값 800ms 이하.
  - 서버별 예정 없는 서비스 재시작 0(NRestarts 변화 0), 커널 OOM 0, chrony 오프셋 50ms 이하(사실이 주어질 때).

판정 이름: PASS_Q3_24H 또는 FAIL_Q3_24H. 창 수가 24시간에 못 미치면 INCOMPLETE_Q3_24H.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

WINDOW_SEC = 300
TOTAL_ABNORMAL_MAX = 0.001
WINDOW_ABNORMAL_MAX = 0.005
WINDOW_PASS_SHARE_MIN = 0.99
PEAK_P95_MAX_MS = 800.0
PEAK_HOURS_KST = (15, 16, 17)
CHRONY_OFFSET_MAX_MS = 50.0
KST = datetime.timezone(datetime.timedelta(hours=9))


def parse_time(text: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(datetime.timezone.utc)


def load_windows(path: Path) -> dict[int, dict]:
    windows: dict[int, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        start = int(parse_time(record["window_start"]).timestamp())
        windows[start] = record
    return windows


def judge(windows: dict[int, dict], end: datetime.datetime, hours: int = 24, host_facts: dict | None = None) -> dict:
    end_epoch = int(end.timestamp())
    end_epoch -= end_epoch % WINDOW_SEC
    start_epoch = end_epoch - hours * 3600
    expected = list(range(start_epoch, end_epoch, WINDOW_SEC))
    present = [windows[t] for t in expected if t in windows]
    missing = [t for t in expected if t not in windows]
    total = sum(int(w.get("http_count", 0)) for w in present)
    abnormal = sum(int(w.get("http_5xx", 0)) + int(w.get("http_no_response", 0)) for w in present)
    timeouts = sum(int(w.get("http_timeout", 0)) for w in present)
    passing = 0
    worst = []
    for t in expected:
        record = windows.get(t)
        if not record or not record.get("http_count"):
            continue
        rate = (int(record.get("http_5xx", 0)) + int(record.get("http_no_response", 0))) / int(record["http_count"])
        if rate <= WINDOW_ABNORMAL_MAX:
            passing += 1
        else:
            worst.append({"window_start": record["window_start"], "abnormal_rate": round(rate, 5)})
    peak = [
        float(windows[t]["http_p95_ms"])
        for t in expected
        if t in windows and "http_p95_ms" in windows[t]
        and datetime.datetime.fromtimestamp(t, KST).hour in PEAK_HOURS_KST
    ]
    reasons: list[str] = []
    total_rate = abnormal / total if total else None
    share = passing / len(expected) if expected else 0.0
    if total_rate is None or total_rate > TOTAL_ABNORMAL_MAX:
        reasons.append(f"전체 비정상 비율 {total_rate} > {TOTAL_ABNORMAL_MAX}")
    if share < WINDOW_PASS_SHARE_MIN:
        reasons.append(f"0.5% 이하 창 비율 {share:.4f} < {WINDOW_PASS_SHARE_MIN}(빠진 창 {len(missing)})")
    if not peak:
        reasons.append("피크 시간대 p95 표본 없음")
    elif max(peak) > PEAK_P95_MAX_MS:
        reasons.append(f"피크 p95 최댓값 {max(peak)}ms > {PEAK_P95_MAX_MS}ms")
    host_summary = {}
    for host, facts in sorted((host_facts or {}).items()):
        before = facts.get("nrestarts_before", {})
        after = facts.get("nrestarts_after", {})
        restarts = {unit: int(after.get(unit, 0)) - int(before.get(unit, 0)) for unit in sorted(set(before) | set(after))}
        oom = int(facts.get("oom_kills", 0))
        offset = facts.get("chrony_offset_ms")
        host_summary[host] = {"restarts": restarts, "oom_kills": oom, "chrony_offset_ms": offset}
        for unit, delta in restarts.items():
            if delta != 0:
                reasons.append(f"{host} {unit} 재시작 {delta}")
        if oom:
            reasons.append(f"{host} 커널 OOM {oom}")
        if facts.get("rebooted"):
            reasons.append(f"{host} 창 안에서 재부팅했거나 사실 한쪽이 없음")
        if offset is not None and abs(float(offset)) > CHRONY_OFFSET_MAX_MS:
            reasons.append(f"{host} chrony 오프셋 {offset}ms > {CHRONY_OFFSET_MAX_MS}ms")
    first = min(windows) if windows else None
    if first is None or first > start_epoch:
        verdict = "INCOMPLETE_Q3_24H"
        reasons.insert(0, "창 기록이 판정 구간 시작보다 늦게 시작함")
    else:
        verdict = "PASS_Q3_24H" if not reasons else "FAIL_Q3_24H"
    return {
        "verdict": verdict,
        "start": datetime.datetime.fromtimestamp(start_epoch, KST).isoformat(),
        "end": datetime.datetime.fromtimestamp(end_epoch, KST).isoformat(),
        "windows_expected": len(expected),
        "windows_present": len(present),
        "windows_missing": len(missing),
        "requests": total,
        "abnormal": abnormal,
        "timeouts": timeouts,
        "total_abnormal_rate": total_rate,
        "window_pass_share": round(share, 5),
        "peak_p95_max_ms": max(peak) if peak else None,
        "worst_windows": sorted(worst, key=lambda item: -item["abnormal_rate"])[:10],
        "hosts": host_summary,
        "reasons": reasons,
    }


def merge_facts(before_path: Path, after_path: Path) -> dict:
    """host-facts.sh 의 줄 파일 둘을 judge 의 host_facts 꼴로 합친다."""
    def read(path: Path) -> dict[str, dict]:
        return {row["host"]: row for row in (json.loads(line) for line in path.read_text().splitlines() if line.strip())}
    before, after = read(before_path), read(after_path)
    merged = {}
    for host in sorted(set(before) | set(after)):
        start, end = before.get(host, {}), after.get(host, {})
        merged[host] = {
            "nrestarts_before": start.get("nrestarts", {}),
            "nrestarts_after": end.get("nrestarts", {}),
            "oom_kills": end.get("oom_kills", 0),
            "chrony_offset_ms": end.get("chrony_offset_ms"),
            "rebooted": bool(start and end and start.get("boot") != end.get("boot")) or not (start and end),
        }
    return merged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--windows", required=True, type=Path)
    parser.add_argument("--end", help="판정 구간 끝(ISO 시각). 없으면 마지막 창 끝")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--facts-before", type=Path)
    parser.add_argument("--facts-after", type=Path)
    args = parser.parse_args(argv)
    windows = load_windows(args.windows)
    if args.end:
        end = parse_time(args.end)
    elif windows:
        end = datetime.datetime.fromtimestamp(max(windows) + WINDOW_SEC, datetime.timezone.utc)
    else:
        print(json.dumps({"verdict": "INCOMPLETE_Q3_24H", "reasons": ["창 기록 없음"]}, ensure_ascii=False))
        return 2
    facts = merge_facts(args.facts_before, args.facts_after) if args.facts_before and args.facts_after else None
    result = judge(windows, end, args.hours, facts)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["verdict"].startswith("PASS") else 1


if __name__ == "__main__":
    sys.exit(main())
