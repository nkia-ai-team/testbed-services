#!/usr/bin/env python3
"""녹화 창과 덤프 대기 동안에는 배포를 미룬다. 판정은 러너 HTTP 로만 읽는다(ssh 없음).

막는 이유는 둘이다.

1. 녹화 창(평시 시작 ~ 식힘 끝) 동안 66 쪽 ssh 를 하지 않는다(v4 검토 세션 Q26 답 2, Q27 답 1).
   배포는 66 호스트를 거쳐 tb-ops, tb-runner 로 ssh 하고, 그 접속 줄이 녹화 syslog 에 담긴다.
   G2 녹화에 운영자 ssh 가 평시 207, 장애 137 접속으로 남았고, 주입 시각과 상관된 단서가 될 수
   있다. 녹화 창 단계는 운영 스킬 testbed-operate 의 recording-window.sh 와 같다:
   cycle_reset(평시 직전 몇 분, 경계를 초로 맞출 수 없어 함께 막는다), cycle_normal, cycle_buffer,
   running, cycle_cooldown, 그리고 큐 밖 웹 실행(/api/active 의 mode run).
2. 덤프의 카탈로그 해시. 러너는 실행이 끝날 때 카탈로그 해시를 증거에 적고, 덤프가 그 해시를
   라이브 카탈로그와 다시 비교한다. 그 사이 배포가 카탈로그를 바꾸면 덤프가
   "scenario catalog hash mismatch" 로 실패하고 주기 하나를 잃는다(2026-10-10 F40-R). 그래서
   waiting_capture 와 waiting_clean_window(같은 실행의 덤프 재시도가 남을 수 있다)도 막는다.

   덤프가 실패해 멈춘 큐(paused)도 같다. 재개하면 러너가 주입 없이 "캡처만 다시" 하고, 그 캡처가
   같은 해시를 다시 비교한다(러너 live_queue.py _capture_retry_check). 그래서 paused 라도 덤프 대기에서
   멈춘 것(paused_from_phase 가 덤프 대기 단계, 그 필드가 없던 옛 러너 기록은 reason 이
   "capture failed:" 나 "capture retry failed:" 로 시작)이면 "덤프 대기(일시 정지)" 로 막는다.

통과는 idle, completed, 그리고 덤프 대기에서 멈춘 것이 아닌 paused 뿐이다. 이들은 러너가 스스로
다음 단계로 넘어가지 않는다(시작, 재개, 웹 실행이라는 사람이나 하네스의 요청이 있어야 한다).
모르는 phase, 러너 HTTP 무응답, 읽을 수 없는 응답은 모두 막는다(fail closed). 배포는 다음 기회에
처음부터 다시 나가면 된다.

검사와 원격 쓰기 사이에 큐가 시작되는 틈을 줄이려고, deploy-live-promotions.sh 는 원격 명령
(ssh, rsync)마다 바로 앞에서 이 스크립트를 다시 부른다.

v4 의 미끼, 평시 배포(러너가 cycle_normal 안에서 테스트베드 앱에 넣는 무해한 배포)는 러너가 직접
하는 것이라 이 가드와 상관이 없다. 판단은 미끼 기록과 무관하게 phase 와 진행 중 실행으로만 한다.

사용: recording-guard.py [<단계 이름>]
  러너 HTTP 주소는 TESTBED_RUNNER_API(기본 http://192.168.200.66:8091, 66 호스트의 러너 포워딩).
  종료 코드: 0 배포 가능, 3 보류(이유는 표준 오류).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

DEFAULT_RUNNER_API = "http://192.168.200.66:8091"
HTTP_TIMEOUT_SEC = 10

# 운영 스킬 recording-window.sh 의 RECORDING 과 같은 집합. 바꾸면 그쪽도 함께 바꾼다.
RECORDING_WINDOW_PHASES = frozenset(
    {"cycle_reset", "cycle_normal", "cycle_buffer", "running", "cycle_cooldown"}
)
# 녹화 창은 끝났지만 덤프가 카탈로그 해시를 다시 비교하는 단계.
CAPTURE_PHASES = frozenset({"waiting_capture", "waiting_clean_window"})
# 러너가 스스로 녹화 창으로 넘어가지 않는 단계. 이 밖은 모두 막는다(paused 는 아래 덤프 대기 확인을 거친다).
SAFE_PHASES = frozenset({"idle", "paused", "completed"})
# paused_from_phase 가 없던 옛 러너가 덤프 실패로 멈출 때 쓰는 reason 머리(러너 _capture_retry_check 와 같음).
CAPTURE_PAUSE_REASON_PREFIXES = ("capture failed:", "capture retry failed:")


def _paused_waiting_for_capture(queue: dict) -> bool:
    """덤프 대기에서 멈춘 paused 인가. 재개하면 캡처만 다시 하므로 카탈로그 해시를 지켜야 한다."""
    origin = queue.get("paused_from_phase")
    if origin is not None:
        if not isinstance(origin, str):
            return True  # 읽을 수 없으면 막는다
        # 덤프 대기에서 멈춤이면 막는다. 러너가 모르는 값(모르는 phase 와 같은 이유로)도 막는다.
        return origin in CAPTURE_PHASES or origin not in SAFE_PHASES | RECORDING_WINDOW_PHASES
    reason = queue.get("reason")
    return isinstance(reason, str) and reason.startswith(CAPTURE_PAUSE_REASON_PREFIXES)


def blocking_reason(queue_raw: str, active_raw: str) -> str | None:
    """/api/live-queue 와 /api/active 응답 본문으로 보류 이유를 낸다. 배포 가능이면 None."""
    try:
        queue = json.loads(queue_raw)
        phase = queue["phase"]
    except (ValueError, TypeError, KeyError):
        return "live-queue 응답을 읽을 수 없다"
    if not isinstance(phase, str):
        return "live-queue 응답을 읽을 수 없다(phase 가 문자열이 아님)"
    try:
        active = json.loads(active_raw)
        is_active = active["is_active"]
    except (ValueError, TypeError, KeyError):
        return "active 응답을 읽을 수 없다"
    where = (
        f"phase={phase}, queue={queue.get('queue_id')}, "
        f"scenario={queue.get('current_scenario_id')}, run={queue.get('current_run_id')}"
    )
    if phase in RECORDING_WINDOW_PHASES:
        return f"녹화 창({where})"
    if phase in CAPTURE_PHASES:
        return f"덤프 대기({where})"
    if phase not in SAFE_PHASES:
        return f"모르는 phase({where})"
    if phase == "paused" and _paused_waiting_for_capture(queue):
        return (f"덤프 대기(일시 정지, paused_from_phase={queue.get('paused_from_phase')}, "
                f"reason={queue.get('reason')}, {where})")
    if is_active and active.get("mode") == "run":
        return f"러너가 실행 {active.get('run_id')} 을 돌리는 중(큐 밖 웹 실행 포함, {where})"
    return None


def _get(api: str, path: str, timeout: float) -> str:
    with urllib.request.urlopen(api + path, timeout=timeout) as response:
        return response.read().decode("utf-8")


def check(api: str, timeout: float = HTTP_TIMEOUT_SEC) -> str | None:
    """러너 HTTP 를 읽어 보류 이유를 낸다. 응답이 없거나 오류면 보류(fail closed)."""
    api = api.rstrip("/")
    try:
        queue_raw = _get(api, "/api/live-queue", timeout)
        active_raw = _get(api, "/api/active", timeout)
    except Exception as exc:  # noqa: BLE001 - 무엇이 실패하든 배포하지 않는다
        return f"러너 HTTP {api} 응답 없음({type(exc).__name__})"
    return blocking_reason(queue_raw, active_raw)


def main(argv: list[str]) -> int:
    stage = argv[1] if len(argv) > 1 else "원격 쓰기"
    api = os.environ.get("TESTBED_RUNNER_API") or DEFAULT_RUNNER_API
    try:
        reason = check(api)
    except Exception as exc:  # noqa: BLE001 - 판정이 죽어도 배포 실패(1)가 아니라 보류(3)로 낸다
        reason = f"판정 중 예외({type(exc).__name__})"
    if reason is None:
        return 0
    print(
        f"[deploy] 배포 보류({stage} 앞): {reason}. 녹화 창과 덤프가 끝난 뒤 처음부터 다시 실행한다.",
        file=sys.stderr,
    )
    return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv))
