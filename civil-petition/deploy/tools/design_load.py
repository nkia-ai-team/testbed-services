#!/usr/bin/env python3
"""설계 부하에서 읽어 계산하는 값(설계 docs/spec-testbed-vm-3tier.md §2-3, §6-4, 검토 세션 Q24 답 (다)).

손으로 적은 숫자를 두지 않고 부하 설정 파일에서 읽는다:
  - 하루 신청 수(설계 하루 쓰기): loadgen/entrypoint.sh 의 시간별 비율표와 목표 요청률 식, 상주 부하 유닛
    (deploy/templates/loadgen/loadgen.service)의 PEAK_RPS, TROUGH_RPS, loadgen/script.js 의 신청 여정 비중.
  - 보관 배치 설정: petition-web 의 application.properties(petition.archive.*), 타이머 시각(petition-archive.timer).
  - 녹화 막기 행 수 문턱 = 보존 건수 상한 + 설계 하루 쓰기 x 2.5. 밤 배치 한 번 실패는 견디고(다음 밤 배치 직전 최대
    상한 + 하루 쓰기 x 2) 두 번 연속이면 그다음 날 낮에 넘는다. 놓친 밤 배치 창 조건과 같은 "두 번 연속에 막음" 뜻이다.
  - 밤 배치가 한 번에 옮길 수 있는 양: 창이 열린 시간(타이머 시각부터 창 끝까지) / (묶음 시간 + 묶음 사이 쉼) x 묶음 크기.
    Q24 답 조건 ①: 하루 쓰기 2.5일 몫 이상이어야 한 번 실패 뒤 다음 밤에 따라잡는다.

`python3 design_load.py` 는 이 값들과 판정(PASS_ARCHIVE_CAPACITY 또는 FAIL_ARCHIVE_CAPACITY)을 JSON 으로 낸다.
"""
from __future__ import annotations

import datetime
import json
import math
import re
import sys
from pathlib import Path

DOMAIN = Path(__file__).resolve().parents[2]
ROWS_LIMIT_DAYS = 2.5
# 묶음 하나의 시간 상한 가정(로컬 PostgreSQL 16, 200만 시드 실측 29~215ms 를 넉넉히). 실측 처리량이 있으면 그것을 쓴다.
CHUNK_SECONDS_ASSUMED = 0.25


def _read(relative: str) -> str:
    return (DOMAIN / relative).read_text(encoding="utf-8")


def daily_submissions() -> int:
    entry = _read("loadgen/entrypoint.sh")
    pct = [int(v) for v in re.findall(r"\d+\) echo (\d+) ;;", entry)]
    if len(pct) != 24:
        raise ValueError("entrypoint.sh 의 시간별 비율표가 24칸이 아님")
    if "TROUGH_RPS + ( (PEAK_RPS - TROUGH_RPS) * pct + 50 ) / 100" not in entry:
        raise ValueError("entrypoint.sh 의 목표 요청률 식이 바뀜")
    unit = _read("deploy/templates/loadgen/loadgen.service")
    peak = int(re.search(r"^Environment=PEAK_RPS=(\d+)$", unit, re.M).group(1))
    trough = int(re.search(r"^Environment=TROUGH_RPS=(\d+)$", unit, re.M).group(1))
    script = _read("loadgen/script.js")
    pairs = re.findall(r"^\s*\[(\d+), (\w+)\],$", script, re.M)
    bounds = [int(n) for n, _ in pairs]
    shares = {name: b - a for (_, name), a, b in zip(pairs, [0] + bounds, bounds)}
    iterations = sum(trough + ((peak - trough) * p + 50) // 100 for p in pct) * 3600
    return iterations * shares["submit"] // 100


def archive_settings() -> dict:
    props = _read("petition-web/src/main/resources/application.properties")
    values = dict(re.findall(r"^petition\.archive\.([a-z-]+)=(.+)$", props, re.M))
    timer = _read("deploy/templates/was/petition-archive.timer")
    calendar = re.search(r"OnCalendar=\*-\*-\* (\d\d):(\d\d):00 (\S+)$", timer, re.M)
    hour, minute = int(calendar.group(1)), int(calendar.group(2))
    start, end = (datetime.time(*map(int, part.split(":"))) for part in values["window"].split("-"))
    timer_at = datetime.time(hour, minute)
    if not (start <= timer_at < end):
        raise ValueError("보관 타이머 시각이 배치 창 밖")
    open_sec = (end.hour * 60 + end.minute - hour * 60 - minute) * 60
    return {
        "zone": values["zone"].strip(),
        "timer_zone": calendar.group(3),
        "keep_rows": int(values["keep-rows"]),
        "chunk_rows": int(values["chunk-rows"]),
        "pause_ms": int(values["pause-ms"]),
        "window": values["window"],
        "timer": f"{hour:02d}:{minute:02d}",
        "open_sec": open_sec,
    }


def rows_limit() -> dict:
    daily = daily_submissions()
    keep = archive_settings()["keep_rows"]
    return {
        "limit": keep + math.ceil(daily * ROWS_LIMIT_DAYS),
        "keep_rows": keep,
        "design_daily_writes": daily,
        "days": ROWS_LIMIT_DAYS,
    }


def archive_schedule() -> dict:
    """밤 배치 창(놓친 창 세기용, 설계 §6-4, 지휘 세션 11-17 결정): 타이머 시각과 창 끝, 시간대."""
    settings = archive_settings()
    # 타이머 유닛의 달력 시간대도 싣는다. 앱 창 시간대와 다르면 러너가 그 이유로 막는다(리뷰 rv-t2c L15).
    return {"timer": settings["timer"], "window_end": settings["window"].split("-")[1].strip(), "zone": settings["zone"],
            "timer_zone": settings["timer_zone"]}


def night_capacity(rows_per_second: float | None = None) -> dict:
    """밤 배치 한 번이 옮길 수 있는 양과 Q24 조건 ① 판정. rows_per_second 는 실측 처리량(쉼 포함 전)."""
    settings = archive_settings()
    daily = daily_submissions()
    chunk = settings["chunk_rows"]
    if rows_per_second:
        chunk_seconds = chunk / rows_per_second
        source = "measured"
    else:
        chunk_seconds = CHUNK_SECONDS_ASSUMED
        source = "assumed"
    per_chunk = chunk_seconds + settings["pause_ms"] / 1000
    capacity = int(settings["open_sec"] / per_chunk) * chunk
    required = math.ceil(daily * ROWS_LIMIT_DAYS)
    return {
        "verdict": "PASS_ARCHIVE_CAPACITY" if capacity >= required else "FAIL_ARCHIVE_CAPACITY",
        "capacity_rows": capacity,
        "required_rows": required,
        "chunk_seconds": round(chunk_seconds, 4),
        "chunk_seconds_source": source,
        **settings,
        "design_daily_writes": daily,
    }


def night_capacity_measured(rows: int, seconds: float) -> dict:
    """실제 밤 배치 한 번(옮긴 행 수, 걸린 시간. 묶음 사이 쉼 포함)의 처리량으로 창 하나가 옮길 수 있는 양."""
    settings = archive_settings()
    required = math.ceil(daily_submissions() * ROWS_LIMIT_DAYS)
    if rows <= 0 or seconds <= 0:
        raise ValueError("옮긴 행이나 걸린 시간이 0 이라 처리량을 잴 수 없음")
    capacity = int(rows / seconds * settings["open_sec"])
    return {
        "verdict": "PASS_ARCHIVE_CAPACITY" if capacity >= required else "FAIL_ARCHIVE_CAPACITY",
        "capacity_rows": capacity,
        "required_rows": required,
        "measured_rows": rows,
        "measured_seconds": round(seconds, 1),
        "open_sec": settings["open_sec"],
    }


def main() -> int:
    capacity = night_capacity()
    print(json.dumps({"rows_limit": rows_limit(), "night_capacity": capacity}, ensure_ascii=False, indent=2))
    return 0 if capacity["verdict"].startswith("PASS") else 1


if __name__ == "__main__":
    sys.exit(main())
