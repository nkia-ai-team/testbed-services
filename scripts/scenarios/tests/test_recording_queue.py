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
        # 2026-10-09: 41 → 42. F38-R 정식 승격(녹화 case-f38-r-v3-54918c10).
        # 2026-10-09: 42 → 43. F39-R 정식 승격(녹화 case-f39-r-v3-dc45499a).
        self.assertEqual(stages.count("official"), 43)
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
        # 2026-10-09: 11 → 12. F47-R(banking api 릴리스가 이체마다 account 계좌 목록 전체를 다시 읽어 account 포화) 후보 추가.
        # 2026-10-09: 12 → 11. F38-R 정식 승격(녹화 case-f38-r-v3-54918c10).
        # 2026-10-09: 11 → 12. F48-R(food 인기 메뉴 집계 표를 취소된 인덱스 백필이 치움) 후보 추가.
        # 2026-10-09: 12 → 13. F49-R(food restaurant 릴리스가 가게 상세 응답 구조를 바꿔 order 가 해석 실패) 후보 추가.
        # 2026-10-09: 13 → 14. F33-H(food dispatch 릴리스가 배달 목록에 전체 건수 COUNT 를 붙여 MySQL 포화) 후보 추가.
        # 2026-10-09: 14 → 15. F49-H(food order 릴리스가 시각 표기를 바꿔 dispatch 응답을 못 읽음) 후보 추가.
        # 2026-10-09: 15 → 14. F39-R 정식 승격(녹화 case-f39-r-v3-dc45499a).
        # 2026-10-09: 14 → 15. F50-R(banking 네임스페이스 메모리 할당량이 사용량 아래라 transfer 재배포의 새 파드가 거절됨) 후보 추가.
        # 2026-10-09: 15 → 16. F48-P(food 주문 이벤트 outbox 표를 취소된 인덱스 백필이 치워 주문 생성이 1146 으로 되돌려짐) 후보 추가.
        # 2026-10-09: 16 → 17. F51-R(운영 용량 명령 입력 실수로 banking account Deployment 가 replicas 0) 후보 추가.
        # 2026-10-10: 17 → 18. F43-P(food dispatch 릴리스의 중복 배차 확인이 배차마다 풀 연결을 새어 dispatch 풀 고갈) 후보 추가.
        # 2026-10-10: 18 → 19. F40-H(banking transfer 릴리스의 원 단위 금액 검증이 commerce 정산 이체를 400 으로 거절) 후보 추가.
        # 2026-10-10: 19 → 20. F52-R(banking 워커 tb-w2 의 이미지 보존 정리가 쓰고 있는 앱 이미지를 지워 transfer 재배포의 새 파드가 ErrImageNeverPull) 후보 추가.
        # 2026-10-10: 20 → 21. F53-R(운영을 가리킨 로컬 마이그레이션이 food orders 표를 지워 주문 생성이 1146 으로 전량 500) 후보 추가.
        # 2026-10-10: 21 → 22. F53-P(운영을 가리킨 로컬 마이그레이션이 food payments 표를 지워 주문이 결제 단계에서 502) 후보 추가.
        # 2026-10-10: 22 → 23. F42-P(food restaurant 릴리스가 인기 메뉴를 주문 원장에서 바로 세어 MySQL 포화) 후보 추가.
        # 2026-10-10: 23 → 24. F35-H(운영을 가리킨 로컬 마이그레이션이 banking Oracle TRANSFERS 를 지워 이체와 commerce 정산 실패) 후보 추가.
        # 2026-10-10: 24 → 25. F32-P(운영을 가리킨 로컬 마이그레이션이 food dispatches 표를 지워 주문이 배달원 용량 확인에서 전량 503) 후보 추가.
        # 2026-10-10: 25 → 26. F54-R(banking Oracle 앱 계정의 호출당 논리 읽기 한도로 거래 내역 502) 후보 추가.
        # 2026-10-10: 26 → 27. F49-P(food payment 릴리스가 결제 응답 id 형식을 바꿔 order 가 해석 실패) 후보 추가.
        self.assertEqual(stages.count("candidate"), 27)


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
