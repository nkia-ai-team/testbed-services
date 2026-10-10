#!/usr/bin/env python3
"""녹화 구간에는 배포를 미룬다.

러너는 실행이 끝날 때 카탈로그 해시를 증거에 적고, 덤프가 그 해시를 109 의 라이브
카탈로그와 다시 비교한다. 그 사이에 배포가 카탈로그를 바꾸면 덤프가
"scenario catalog hash mismatch" 로 실패하고 주기 하나(약 2시간 반)를 잃는다
(2026-10-10 F40-R: 덤프 예약 21:10Z 뒤 21:17Z 생성 하네스 배포).

표준 입력으로 /api/live-queue JSON 을 받아, 주입 직전부터 덤프가 끝날 때까지의 단계면
종료 코드 3 으로 막는다. 읽을 수 없는 응답도 막는다(배포는 다음 기회에 나가면 된다).
"""
from __future__ import annotations

import json
import sys

# cycle_buffer(주입 직전) .. waiting_capture(덤프). waiting_clean_window 는 비주기 모드의
# 덤프 뒤 대기라 안전하지만, 같은 실행의 덤프 재시도가 남을 수 있어 함께 막는다.
RECORDING_PHASES = frozenset(
    {"cycle_buffer", "running", "cycle_cooldown", "waiting_capture", "waiting_clean_window"}
)


def blocking_reason(raw: str) -> str | None:
    try:
        state = json.loads(raw)
        phase = state["phase"]
    except (ValueError, TypeError, KeyError):
        return "live-queue 응답을 읽을 수 없다"
    if phase in RECORDING_PHASES:
        return f"녹화 중(phase={phase}, scenario={state.get('current_scenario_id')})"
    return None


def main() -> int:
    reason = blocking_reason(sys.stdin.read())
    if reason is None:
        return 0
    print(f"[deploy] 배포 보류: {reason}. 녹화 구간이 끝난 뒤 다시 실행한다.", file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main())
