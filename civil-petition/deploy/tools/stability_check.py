#!/usr/bin/env python3
"""평시 24시간 안정 판정(설계 docs/spec-testbed-vm-3tier.md §1 Q3, §10 T2 "평시 24시간").

입력:
  --windows   부하기 모니터가 쓰는 5분 창 기록(JSON 줄, loadgen_monitor.py --window-log)
  --facts-before, --facts-after  선택. host-facts.sh 가 서버마다 낸 JSON 줄을 모은 파일 둘(창 처음과 끝).
               재시작 수는 두 값의 차, OOM 과 chrony 오프셋은 끝 값을 본다. 부팅 시각이 다르면(창 안 재부팅)
               실패로 센다.
  --db-before, --db-after  db-facts.sh 가 db01 에서 낸 JSON 한 줄 파일 둘(창 처음과 끝). 없으면 실패로 센다.

기준(설계 값, 바꾸지 않는다):
  - 전체 비정상 비율(5xx 와 응답 없음(시간 초과, 연결 실패)의 합 / 전체 요청) 0.1% 이하.
    설계 Q3 의 문구는 "5xx와 시간 초과" 이고, 연결 실패도 사용자 비정상(§6-3)이라 함께 센다(더 엄격한 쪽).
  - 5분 창의 99% 이상에서 그 창의 비정상 비율 0.5% 이하. 기록이 없는 창(부하가 멈춘 창)은 못 지킨 창으로 센다.
  - 피크(KST 15~17시) 창들의 p95 최댓값 800ms 이하.
  - 서버별 예정 없는 서비스 재시작 0(NRestarts 변화 0, 그리고 사람이 다시 띄운 것까지 잡으려고 InvocationID 같음),
    커널 OOM 0, chrony 오프셋 50ms 이하(사실이 주어질 때).
  - 정상 상태(검토 세션 Q21 답 (나) 의 조건 ③): 주 표(민원, 처리 이력) 행 수가 처음과 끝에 같은 자릿수이고,
    보관 배치(archive)가 판정 구간 안에서 결과 ok 로 한 번 이상 돌았고, 대표 질의 시간의 p95 가 처음과 끝에서
    10% 안으로 같다. 대표 질의는 공개 민원 검색(부하기 단계 search)이다. 앱의 세 읽기(내 민원 목록, 공개 민원
    검색, 담당자 함) 가운데 주 표의 기간 범위를 가장 넓게 훑고(공개 인덱스로 기간을 거꾸로 읽으며 제목 LIKE 로
    거름) 다른 둘은 사용자별, 부서별 열린 민원으로 좁혀 읽으므로, 표 크기와 내용 변화가 시간에 가장 먼저
    드러나는 질의이기 때문이다. 부하기는 시간별 일주기로 돌므로 처음과 끝은 정확히 24시간 떨어진 같은 시계 구간이다:
    처음은 판정 구간 시작 바로 앞 2시간 [S-2h, S), 끝은 판정 구간 마지막 2시간 [E-2h, E)(E = S + 24h).
    그 안 5분 창들의 search p95 중앙값을 견준다. 목록과 담당자 함의 같은 값은 참고로 싣는다.
    창 기록이 처음 구간 시작([E-24h-2h])보다 늦게 시작했으면(부하 시작 뒤 26시간이 안 됨) 짧은 구간으로 견주지
    않고 INCOMPLETE 다. 기록이 처음 구간을 덮는데 어느 구간이든 search 표본이 있는 창이 22개(2시간 24창의 약 90%)
    보다 적으면 부하가 멈춘 것이라 실패다.
  - 밤 배치 처리 능력(검토 세션 Q24 답 조건 ①): 판정 구간 안 결과 ok 이고 옮긴 행이 있는 보관 배치의 처리량(옮긴 행 /
    걸린 시간, 묶음 사이 쉼 포함)으로 창 하나(타이머 시각부터 창 끝)가 옮길 수 있는 양이 설계 하루 쓰기의 2.5일 몫
    이상이어야 한다. 한 번 실패한 뒤 다음 밤에 다 따라잡아야 녹화 막기 문턱(상한 + 하루 쓰기 x 2.5)이 회복 뒤에도
    막지 않는다. 하루 쓰기와 창은 design_load.py 가 부하 설정과 앱 설정에서 읽는다.

판정 이름: PASS_Q3_24H 또는 FAIL_Q3_24H. 창 수가 24시간에 못 미치면 INCOMPLETE_Q3_24H.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import design_load  # noqa: E402

WINDOW_SEC = 300
TOTAL_ABNORMAL_MAX = 0.001
WINDOW_ABNORMAL_MAX = 0.005
WINDOW_PASS_SHARE_MIN = 0.99
PEAK_P95_MAX_MS = 800.0
PEAK_HOURS_KST = (15, 16, 17)
CHRONY_OFFSET_MAX_MS = 50.0
MAIN_TABLES = ("petition", "petition_history")
QUERY_STEP = "search"
REFERENCE_STEPS = ("my-list", "inbox")
QUERY_SPAN_SEC = 2 * 3600
DAY_SEC = 24 * 3600
QUERY_P95_CHANGE_MAX = 0.10
QUERY_SPAN_MIN_WINDOWS = 22
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


def digits(value: int) -> int:
    return len(str(abs(int(value))))


def median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def step_p95_change(windows: dict[int, dict], start_epoch: int, end_epoch: int, step: str) -> dict:
    """24시간 떨어진 같은 시계 구간 [S-2h, S) 와 [E-2h, E) 의 5분 창 step p95 중앙값과 그 변화율."""
    def span(lo: int, hi: int) -> list[float]:
        return [float(windows[t]["step_p95_ms"][step]) for t in sorted(windows)
                if lo <= t < hi and step in windows[t].get("step_p95_ms", {})]
    # 판정 구간 길이(--hours)와 무관하게 끝 구간에서 정확히 하루 앞의 같은 시계 구간과 견준다.
    first = span(end_epoch - DAY_SEC - QUERY_SPAN_SEC, end_epoch - DAY_SEC)
    last = span(end_epoch - QUERY_SPAN_SEC, end_epoch)
    result = {"step": step, "start_windows": len(first), "end_windows": len(last)}
    if first and last:
        result["start_p95_ms"] = median(first)
        result["end_p95_ms"] = median(last)
        result["change"] = round(result["end_p95_ms"] / result["start_p95_ms"] - 1, 4) if result["start_p95_ms"] else None
    return result


def judge_steady_state(windows: dict[int, dict], start_epoch: int, end_epoch: int, db_facts: dict | None) -> tuple[dict, list[str]]:
    reasons: list[str] = []
    summary: dict = {}
    if not db_facts or not db_facts.get("before") or not db_facts.get("after"):
        reasons.append("DB 사실(--db-before, --db-after)이 없어 주 표 행 수와 보관 배치를 못 봄")
    else:
        before, after = db_facts["before"]["rows"], db_facts["after"]["rows"]
        summary["rows"] = {table: {"start": before.get(table), "end": after.get(table)} for table in MAIN_TABLES}
        for table in MAIN_TABLES:
            if before.get(table) is None or after.get(table) is None:
                reasons.append(f"주 표 {table} 행 수 없음")
            elif digits(before[table]) != digits(after[table]):
                reasons.append(f"주 표 {table} 행 수 자릿수가 바뀜 {before[table]} -> {after[table]}")
        runs = [run for run in db_facts["after"].get("archive_runs", [])
                if start_epoch <= int(parse_time(run["finished_at"]).timestamp()) <= end_epoch]
        summary["archive_runs"] = runs
        if not any(run.get("result") == "ok" for run in runs):
            reasons.append("판정 구간 안에 결과 ok 인 보관 배치가 없음")
        moved = [run for run in runs if run.get("result") == "ok" and int(run.get("rows") or 0) > 0 and run.get("started_at")]
        if moved:
            run = max(moved, key=lambda item: int(item["rows"]))
            seconds = (parse_time(run["finished_at"]) - parse_time(run["started_at"])).total_seconds()
            capacity = design_load.night_capacity_measured(int(run["rows"]), seconds)
            summary["night_capacity"] = capacity
            if not capacity["verdict"].startswith("PASS"):
                reasons.append(f"밤 배치 처리 능력 {capacity['capacity_rows']} < 하루 쓰기 2.5일 몫 {capacity['required_rows']}"
                               f"(실측 {capacity['measured_rows']}행 {capacity['measured_seconds']}초)")
        elif runs:
            reasons.append("판정 구간 안에 행을 옮긴 보관 배치가 없어 밤 배치 처리 능력을 잴 수 없음")
    query = step_p95_change(windows, start_epoch, end_epoch, QUERY_STEP)
    summary["query"] = query
    summary["reference_queries"] = [step_p95_change(windows, start_epoch, end_epoch, step) for step in REFERENCE_STEPS]
    if min(query["start_windows"], query["end_windows"]) < QUERY_SPAN_MIN_WINDOWS:
        reasons.append(f"대표 질의({QUERY_STEP}) p95 표본 부족(처음 {query['start_windows']}창, 끝 {query['end_windows']}창, "
                       f"구간마다 {QUERY_SPAN_MIN_WINDOWS}창 이상)")
    elif query["change"] is None or abs(query["change"]) > QUERY_P95_CHANGE_MAX:
        reasons.append(f"대표 질의({QUERY_STEP}) p95 변화 {query['change']} 가 ±{QUERY_P95_CHANGE_MAX} 밖"
                       f"({query['start_p95_ms']}ms -> {query['end_p95_ms']}ms)")
    return summary, reasons


def judge(windows: dict[int, dict], end: datetime.datetime, hours: int = 24, host_facts: dict | None = None,
          db_facts: dict | None = None) -> dict:
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
        inv_before = facts.get("invocations_before", {})
        inv_after = facts.get("invocations_after", {})
        for unit in sorted(set(inv_before) | set(inv_after)):
            if inv_before.get(unit) != inv_after.get(unit) and not restarts.get(unit):
                reasons.append(f"{host} {unit} 다시 시작됨(InvocationID 바뀜)")
        if oom:
            reasons.append(f"{host} 커널 OOM {oom}")
        if facts.get("rebooted"):
            reasons.append(f"{host} 창 안에서 재부팅했거나 사실 한쪽이 없음")
        if offset is not None and abs(float(offset)) > CHRONY_OFFSET_MAX_MS:
            reasons.append(f"{host} chrony 오프셋 {offset}ms > {CHRONY_OFFSET_MAX_MS}ms")
    steady, steady_reasons = judge_steady_state(windows, start_epoch, end_epoch, db_facts)
    reasons.extend(steady_reasons)
    first = min(windows) if windows else None
    query_start = end_epoch - DAY_SEC - QUERY_SPAN_SEC
    if first is None or first > start_epoch:
        verdict = "INCOMPLETE_Q3_24H"
        reasons.insert(0, "창 기록이 판정 구간 시작보다 늦게 시작함")
    elif first > query_start:
        verdict = "INCOMPLETE_Q3_24H"
        reasons.insert(0, "창 기록이 대표 질의 처음 구간(판정 구간 앞 2시간) 시작보다 늦게 시작함(부하 시작 뒤 26시간 전)")
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
        "steady_state": steady,
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
            "invocations_before": start.get("invocations", {}),
            "invocations_after": end.get("invocations", {}),
            "oom_kills": end.get("oom_kills", 0),
            "chrony_offset_ms": end.get("chrony_offset_ms"),
            "rebooted": bool(start and end and start.get("boot") != end.get("boot")) or not (start and end),
        }
    return merged


def read_db_facts(before_path: Path, after_path: Path) -> dict:
    """db-facts.sh 의 한 줄 파일 둘을 judge 의 db_facts 꼴로 읽는다(마지막 줄을 쓴다)."""
    def read(path: Path) -> dict:
        lines = [line for line in path.read_text().splitlines() if line.strip()]
        return json.loads(lines[-1])["db"] if lines else {}
    return {"before": read(before_path), "after": read(after_path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--windows", required=True, type=Path)
    parser.add_argument("--end", help="판정 구간 끝(ISO 시각). 없으면 마지막 창 끝")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--facts-before", type=Path)
    parser.add_argument("--facts-after", type=Path)
    parser.add_argument("--db-before", type=Path)
    parser.add_argument("--db-after", type=Path)
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
    db_facts = read_db_facts(args.db_before, args.db_after) if args.db_before and args.db_after else None
    result = judge(windows, end, args.hours, facts, db_facts)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["verdict"].startswith("PASS") else 1


if __name__ == "__main__":
    sys.exit(main())
