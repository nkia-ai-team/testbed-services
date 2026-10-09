"""시나리오 수명주기 표시(stage)와 녹화 대기 큐의 계약 (docs/spec-scenario-lifecycle.md)."""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CATALOG = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
QUEUE = json.loads((ROOT / "recording-queue.json").read_text(encoding="utf-8"))
CONTROLLERS = json.loads((ROOT / "registry" / "controllers.json").read_text(encoding="utf-8"))

REASONS = {"new", "no-recording", "recapture", "user"}
STATUSES = {"waiting", "hold"}
ENTRY_KEYS = {"scenario_id", "reason", "note", "requested_by", "requested_at", "attempts", "status"}


class StageTests(unittest.TestCase):
    def test_every_ready_row_has_a_stage_and_only_ready_rows_do(self) -> None:
        for row in CATALOG["scenarios"]:
            if row["readiness"] == "ready":
                self.assertIn(row.get("stage"), {"official", "candidate"}, row["id"])
            else:
                self.assertNotIn("stage", row, row["id"])

    def test_stage_counts(self) -> None:
        # 2026-10-08: 정식 기준을 "정상 녹화 1개 이상"으로 바꾸며 ready 40종을 정식 35, 후보 5로 나눴다.
        stages = [row.get("stage") for row in CATALOG["scenarios"] if row["readiness"] == "ready"]
        # 2026-10-08: 35 → 36. F30-R 정식 승격(녹화 case-f30-r-v3-0cd9193b).
        # 2026-10-09: 36 → 37. F32-R 정식 승격(녹화 case-f32-r-v3-63b10537).
        # 2026-10-09: 37 → 38. F33-R 정식 승격(녹화 case-f33-r-v3-73fed92a).
        # 2026-10-09: 38 → 39. F35-R 정식 승격(녹화 case-f35-r-v3-bbbee443).
        # 2026-10-09: 39 → 40. F36-R 정식 승격(녹화 case-f36-r-v3-776b522b).
        # 2026-10-09: 40 → 41. F37-R 정식 승격(녹화 case-f37-r-v3-43c16731).
        self.assertEqual(stages.count("official"), 41)
        # 2026-10-08: 후보 5종 중 F10-P, F15-R, F15-T2, F20-Q 를 폐기(정상 녹화 없음, 사용자 결정)해 F30-R 하나만 남았다.
        # 2026-10-08: 1 → 2. F32-R(food dispatch 배차 한도 설정 배포) 후보 추가.
        # 2026-10-08: 2 → 3. F33-R(food dispatches 인덱스 제거) 후보 추가.
        # 2026-10-08: 3 → 4. F35-R(banking Oracle 애플리케이션 계정 잠금) 후보 추가.
        # 2026-10-08: 4 → 5. F36-R(food restaurants 열 이름 변경 마이그레이션) 후보 추가.
        # 2026-10-08: 5 → 6. F37-R(banking api 파드 DNS 정책 노드 resolver 배포) 후보 추가.
        # 2026-10-08: 6 → 7. F38-R(food MySQL 인스턴스 전체 read_only) 후보 추가.
        # 2026-10-08: 7 → 6. F30-R 정식 승격(녹화 case-f30-r-v3-0cd9193b).
        # 2026-10-08: 6 → 7. F39-R(banking account 이체 하류 주소 오설정) 후보 추가.
        # 2026-10-09: 7 → 6. F32-R 정식 승격(녹화 case-f32-r-v3-63b10537).
        # 2026-10-09: 6 → 5. F33-R 정식 승격(녹화 case-f33-r-v3-73fed92a).
        # 2026-10-09: 5 → 6. F40-R(commerce pricing 프로모션 할인율 오입력) 후보 추가.
        # 2026-10-09: 6 → 7. F17-H(banking transfer 릴리스 이미지 부재) 후보 추가.
        # 2026-10-09: 7 → 8. F32-H(food dispatch JSON 숫자 문자열 직렬화 설정 배포) 후보 추가.
        # 2026-10-09: 8 → 9. F33-P(commerce auth_tokens 인덱스 교체 마이그레이션 실패) 후보 추가.
        # 2026-10-09: 9 → 8. F35-R 정식 승격(녹화 case-f35-r-v3-bbbee443).
        # 2026-10-09: 8 → 9. F41-R(banking account 메모리 누수 릴리스 롤아웃) 후보 추가.
        # 2026-10-09: 9 → 10. F42-R(banking transfer 전수 스캔 한도 질의 릴리스 롤아웃) 후보 추가.
        # 2026-10-09: 10 → 9. F36-R 정식 승격(녹화 case-f36-r-v3-776b522b).
        # 2026-10-09: 9 → 10. F43-R(commerce cart DB 연결 누수 릴리스 롤아웃) 후보 추가.
        # 2026-10-09: 10 → 11. F44-R(food 워커 tb-w3 방화벽 허용 목록이 order 파드 대역을 빠뜨려 restaurant 로의 새 연결이 버려짐) 후보 추가.
        # 2026-10-09: 11 → 12. F44-P(banking 워커 tb-w2 방화벽 허용 목록이 commerce 파드 대역을 빠뜨려 정산 이체 새 연결이 버려짐) 후보 추가.
        # 2026-10-09: 12 → 11. F37-R 정식 승격(녹화 case-f37-r-v3-43c16731).
        self.assertEqual(stages.count("candidate"), 11)


class RecordingQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ready = {row["id"]: row for row in CATALOG["scenarios"] if row["readiness"] == "ready"}
        self.entries = QUEUE["entries"]

    def test_schema(self) -> None:
        self.assertEqual(QUEUE["schema_version"], 1)
        for entry in self.entries:
            sid = entry.get("scenario_id")
            self.assertTrue(ENTRY_KEYS <= set(entry), sid)
            self.assertLessEqual(set(entry) - ENTRY_KEYS, {"hold_reason"}, sid)
            self.assertIn(entry["reason"], REASONS, sid)
            self.assertIn(entry["status"], STATUSES, sid)
            self.assertIsInstance(entry["attempts"], int, sid)
            self.assertGreaterEqual(entry["attempts"], 0, sid)
            self.assertRegex(entry["requested_at"], r"^\d{4}-\d{2}-\d{2}", sid)
            if entry["status"] == "hold":
                self.assertTrue(entry.get("hold_reason"), sid)
            else:
                self.assertNotIn("hold_reason", entry, sid)

    def test_entries_are_unique_runnable_scenarios(self) -> None:
        ids = [entry["scenario_id"] for entry in self.entries]
        self.assertEqual(len(ids), len(set(ids)))
        live = set(CONTROLLERS["live_scenario_ids"])
        for sid in ids:
            self.assertIn(sid, self.ready, f"{sid}: 큐에는 ready 시나리오만 넣는다")
            self.assertIn(sid, live, f"{sid}: 러너가 돌릴 수 있어야 한다(controller 등록)")

    def test_reason_matches_stage(self) -> None:
        for entry in self.entries:
            stage = self.ready[entry["scenario_id"]]["stage"]
            if entry["reason"] in {"new", "no-recording"}:
                self.assertEqual(stage, "candidate", entry["scenario_id"])
            if entry["reason"] == "recapture":
                self.assertEqual(stage, "official", entry["scenario_id"])

    def test_every_candidate_is_queued(self) -> None:
        queued = {entry["scenario_id"] for entry in self.entries}
        for sid, row in self.ready.items():
            if row["stage"] == "candidate":
                self.assertIn(sid, queued, f"{sid}: 후보는 정식이 되거나 폐기될 때까지 큐에 있어야 한다")


if __name__ == "__main__":
    unittest.main()
