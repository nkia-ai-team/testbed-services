"""녹화 구간 배포 보류 (scripts/scenarios/recording-guard.py)."""
from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("recording_guard", ROOT / "recording-guard.py")
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


def _queue(phase: str) -> str:
    return json.dumps({"phase": phase, "current_scenario_id": "X"})


class RecordingGuardTests(unittest.TestCase):
    def test_blocks_from_injection_until_capture_finishes(self) -> None:
        # 2026-10-10: 덤프 대기 중 배포가 카탈로그 해시를 바꿔 덤프가 실패했다.
        for phase in ("cycle_buffer", "running", "cycle_cooldown", "waiting_capture", "waiting_clean_window"):
            self.assertIsNotNone(guard.blocking_reason(_queue(phase)), phase)

    def test_allows_outside_recording(self) -> None:
        for phase in ("idle", "paused", "completed", "cycle_reset", "cycle_normal"):
            self.assertIsNone(guard.blocking_reason(_queue(phase)), phase)

    def test_decoy_deployments_in_the_normal_phase_do_not_block(self) -> None:
        """v4: 러너가 평시 구간에 넣는 미끼, 평시 배포는 녹화가 담는 사건이지 오염이 아니다."""
        for status in ("planned", "rolling", "applied", "rolled_back", "skipped"):
            raw = json.dumps({
                "phase": "cycle_normal", "current_scenario_id": "X",
                "cycle_decoy_seed": 7,
                "cycle_decoys": [{"id": "d1", "kind": "restart", "status": status}],
            })
            self.assertIsNone(guard.blocking_reason(raw), status)

    def test_recording_phases_still_block_with_decoys_recorded(self) -> None:
        for phase in ("cycle_buffer", "running", "cycle_cooldown", "waiting_capture"):
            raw = json.dumps({
                "phase": phase, "current_scenario_id": "X",
                "cycle_decoys": [{"id": "d1", "kind": "restart", "status": "applied"}],
            })
            self.assertIsNotNone(guard.blocking_reason(raw), phase)

    def test_unreadable_response_blocks(self) -> None:
        for raw in ("", "not json", "{}", "[]"):
            self.assertIsNotNone(guard.blocking_reason(raw), raw)

    def test_deploy_script_runs_guard_before_first_remote_write(self) -> None:
        script = (ROOT / "deploy-live-promotions.sh").read_text(encoding="utf-8")
        guard_at = script.index("recording-guard.py")
        self.assertLess(guard_at, script.index("tar czf"))
        self.assertLess(guard_at, script.index("rsync -az"))

    def test_deploy_script_builds_missing_fault_images_after_guard(self) -> None:
        # 2026-10-11: 녹화로 미룬 배포에서 build.sh 가 빠져 F63-R 이미지가 없었다.
        script = (ROOT / "deploy-live-promotions.sh").read_text(encoding="utf-8")
        build_at = script.index("fault-images/build.sh")
        self.assertLess(script.index("recording-guard.py"), build_at)
        self.assertLess(build_at, script.index("rsync -az"))


if __name__ == "__main__":
    unittest.main()
