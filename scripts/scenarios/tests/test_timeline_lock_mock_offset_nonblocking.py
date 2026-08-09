"""timeline.compose 의 start_offset 은 apply 를 붙잡으면 안 된다.

러너는 프로파일 제어 호출을 기다리는 **같은 스레드에서 리스를 갱신**한다. apply 안에서
offset 만큼 자면 그동안 하트비트가 멈추고, 워치독이 런을 죽은 것으로 판정해 먼저 정리한 뒤
러너 자신의 cleanup 은 `operation rejected by runner lease/fencing proof` 로 거부된다.

F15-T2 가 정확히 이렇게 실패했다(2026-08-08, run-7ca4c5ee):
  00:52:17 apply 시작 -> 00:56:20 반환(243초 = offset 240 + 실작업 3초)
  00:56:25 watchdog-cleanup 개시
  00:56:35 러너 cleanup -> 펜싱 거부 -> DIRTY
틱은 한 번도 실행되지 않았다(last_elapsed_sec 0, ticks.jsonl 부재).

이 결함은 **오류를 내지 않는다** — 그냥 DIRTY 로 끝나고 다음 배치에서 똑같이 재발한다.
세 배치 연속 같은 증상이 나오고도 원인이 다른 곳(하트비트 고갈)으로 읽혔던 이유다.
그래서 계약으로 묶는다.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

PROFILES = Path(__file__).resolve().parents[1] / "profiles"


def _script() -> str:
    sys.path.insert(0, str(PROFILES))
    try:
        spec = importlib.util.spec_from_file_location(
            "timeline_lock_mock_executor", PROFILES / "timeline_lock_mock_executor.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.SCRIPT.decode("ascii")
    finally:
        sys.path.remove(str(PROFILES))


def _run_block(script: str) -> str:
    """`run)` 부터 다음 case 분기(`  cleanup)`) 직전까지."""
    start = script.index("\n  run)")
    end = script.index("\n  cleanup)", start)
    return script[start:end]


def test_run_does_not_sleep_the_offset_inline() -> None:
    run = _run_block(_script())
    inline = [
        line.strip()
        for line in run.splitlines()
        # 백그라운드로 넘긴 ( ... ) & 안의 sleep 은 대상이 아니다.
        if re.search(r'\bsleep\s+"\$offset"', line) and "&" not in line
    ]
    assert not inline, (
        "apply 경로가 offset 만큼 동기 대기한다: "
        f"{inline}. 러너 리스가 그동안 갱신되지 않아 워치독에 선점되고 "
        "cleanup 이 펜싱에 거부된다(F15-T2). offset 대기는 백그라운드로 보낼 것."
    )


def test_deferred_arm_is_backgrounded_and_recorded() -> None:
    run = _run_block(_script())
    assert re.search(r'sleep "\$offset"; arm_food \).*&', run), (
        "지연 무장이 백그라운드 서브셸로 분리되어 있지 않다."
    )
    assert 'echo $! >"$arm_pid"' in run, (
        "백그라운드 무장의 pid 를 기록하지 않으면 cleanup 이 그것을 취소할 수 없다."
    )


def test_cleanup_cancels_a_pending_arm() -> None:
    script = _script()
    start = script.index("\n  cleanup)")
    end = script.index("\n  recovery)", start)
    cleanup = script[start:end]
    assert "cancel_arm" in cleanup, (
        "cleanup 이 대기 중인 지연 무장을 취소하지 않는다. offset 이 지나기 전에 정리가 들어오면 "
        "복원 뒤에 mock 이 무장돼 expectation 이 런 밖으로 샌다."
    )
    # 취소가 복원보다 먼저여야 한다.
    assert cleanup.index("cancel_arm") < cleanup.index("reset_mock"), (
        "cancel_arm 이 reset_mock 보다 나중이면 취소 전에 복원이 끝나 같은 누수가 난다."
    )


def test_state_dir_is_removable_after_cleanup() -> None:
    """arm_pid/arm_log 를 지우지 않으면 rmdir 이 실패해 cleanup 이 DIRTY 를 낸다."""
    script = _script()
    rm_line = next(
        line for line in script.splitlines() if line.strip().startswith("rm -f -- ")
        and "rmdir" in line
    )
    for var in ('"$arm_pid"', '"$arm_log"'):
        assert var in rm_line, f"cleanup 의 rm 이 {var} 를 지우지 않아 rmdir 이 실패한다: {rm_line.strip()}"
