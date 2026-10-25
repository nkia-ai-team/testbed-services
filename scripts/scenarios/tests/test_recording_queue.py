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
        # 2026-10-11: 43 → 44. F40-R 정식 승격(녹화 case-f40-r-v3-50e3ecb9).
        # 2026-10-11: 44 → 45. F17-H 정식 승격(녹화 case-f17-h-v3-632a04d1).
        # 2026-10-11: 45 → 46. F32-H 정식 승격(녹화 case-f32-h-v3-531094f2).
        # 2026-10-11: 46 → 47. F33-P 정식 승격(녹화 case-f33-p-v3-e79a2db0).
        # 2026-10-11: 47 → 48. F41-R 정식 승격(녹화 case-f41-r-v3-f37b1984).
        # 2026-10-11: 48 → 49. F42-R 정식 승격(녹화 case-f42-r-v3-26143861).
        # 2026-10-11: 49 → 50. F43-R 정식 승격(녹화 case-f43-r-v3-aecd7e38).
        # 2026-10-12: 50 → 51. F44-R 정식 승격(녹화 case-f44-r-v3-f7f70c65).
        # 2026-10-12: 51 → 52. F44-P 정식 승격(녹화 case-f44-p-v3-4ee5c786).
        # 2026-10-12: 52 → 53. F48-R 정식 승격(녹화 case-f48-r-v3-574963e1).
        # 2026-10-12: 53 → 54. F49-R 정식 승격(녹화 case-f49-r-v3-1af8dad9).
        # 2026-10-13: 54 → 55. F33-H 정식 승격(녹화 case-f33-h-v3-f19ed3f1).
        # 2026-10-13: 55 → 56. F49-H 정식 승격(녹화 case-f49-h-v3-b873cb3e).
        # 2026-10-13: 56 → 57. F50-R 정식 승격(녹화 case-f50-r-v3-75ae96d0).
        # 2026-10-14: 57 → 58. F48-P 정식 승격(녹화 case-f48-p-v3-3667031d).
        # 2026-10-14: 58 → 59. F51-R 정식 승격(녹화 case-f51-r-v3-0c3126f0).
        # 2026-10-14: 59 → 60. F43-P 정식 승격(녹화 case-f43-p-v3-e132f196).
        # 2026-10-14: 60 → 61. F40-H 정식 승격(녹화 case-f40-h-v3-b472a2f0).
        # 2026-10-14: 61 → 62. F52-R 정식 승격(녹화 case-f52-r-v3-13501208).
        # 2026-10-14: 62 → 63. F53-R 정식 승격(녹화 case-f53-r-v3-d0f23131).
        # 2026-10-14: 63 → 64. F53-P 정식 승격(녹화 case-f53-p-v3-b029727d).
        # 2026-10-15: 64 → 65. F35-H 정식 승격(녹화 case-f35-h-v3-a1206bfd).
        # 2026-10-15: 65 → 66. F32-P 정식 승격(녹화 case-f32-p-v3-58d43138).
        # 2026-10-15: 66 → 67. F54-R 정식 승격(녹화 case-f54-r-v3-31a93bbe).
        # 2026-10-15: 67 → 68. F49-P 정식 승격(녹화 case-f49-p-v3-0a4fe786).
        # 2026-10-15: 68 → 69. F55-R 정식 승격(녹화 case-f55-r-v3-ba950165).
        # 2026-10-16: 69 → 70. F56-R 정식 승격(녹화 case-f56-r-v3-24b89933).
        # 2026-10-16: 70 → 71. F51-H 정식 승격(녹화 case-f51-h-v3-c093a2ed).
        # 2026-10-17: 71 → 72. F57-R 정식 승격(녹화 case-f57-r-v3-419736f6).
        # 2026-10-17: 72 → 73. F23-P 정식 승격(녹화 case-f23-p-v3-339d153c).
        # 2026-10-17: 73 → 74. F58-R 정식 승격(녹화 case-f58-r-v3-4568fd65).
        # 2026-10-18: 74 → 75. F59-R 정식 승격(녹화 case-f59-r-v3-cdc14d3d).
        # 2026-10-18: 75 → 76. F59-P 정식 승격(녹화 case-f59-p-v3-6fd09c9b).
        # 2026-10-18: 76 → 77. F36-H 정식 승격(녹화 case-f36-h-v3-b671604f).
        # 2026-10-18: 77 → 78. F41-H 정식 승격(녹화 case-f41-h-v3-2c392569).
        # 2026-10-18: 78 → 79. F60-R 정식 승격(녹화 case-f60-r-v3-22036218).
        # 2026-10-19: 79 → 80. F57-P 정식 승격(녹화 case-f57-p-v3-2b7db69d).
        # 2026-10-19: 80 → 81. F64-R 정식 승격(녹화 case-f64-r-v3-9014da8a).
        # 2026-10-20: 81 → 82. F65-R 정식 승격(녹화 case-f65-r-v3-b5383723).
        # 2026-10-20: 82 → 83. F66-R 정식 승격(녹화 case-f66-r-v3-346c6170).
        # 2026-10-20: 83 → 84. F64-P 정식 승격(녹화 case-f64-p-v3-7b180ead).
        # 2026-10-20: 84 → 85. F66-P 정식 승격(녹화 case-f66-p-v3-a79bda26).
        # 2026-10-20: 85 → 86. F60-H 정식 승격(녹화 case-f60-h-v3-1aece1d2).
        # 2026-10-20: 86 → 87. F54-P 정식 승격(녹화 case-f54-p-v3-fb67a747).
        # 2026-10-20: 87 → 88. F51-P 정식 승격(녹화 case-f51-p-v3-1c6b181c).
        # 2026-10-21: 88 → 89. F39-H 정식 승격(녹화 case-f39-h-v3-255629f2).
        # 2026-10-21: 89 → 90. F63-P 정식 승격(녹화 case-f63-p-v3-ddf34faa).
        # 2026-10-21: 90 → 91. F67-R 정식 승격(녹화 case-f67-r-v3-654a133c).
        # 2026-10-21: 91 → 92. F37-H 정식 승격(녹화 case-f37-h-v3-a6cd30bd).
        # 2026-10-21: 92 → 93. F53-H 정식 승격(녹화 case-f53-h-v3-d20267b1).
        # 2026-10-21: 93 → 94. F67-H 정식 승격(녹화 case-f67-h-v3-53919475).
        # 2026-10-22: 94 → 95. F60-P 정식 승격(녹화 case-f60-p-v3-04d37516).
        # 2026-10-22: 95 → 96. F35-P 정식 승격(녹화 case-f35-p-v3-11824edf).
        # 2026-10-22: 96 → 97. F16-P 정식 승격(녹화 case-f16-p-v3-e40ec615).
        # 2026-10-22: 97 → 98. F68-R 정식 승격(녹화 case-f68-r-v3-3f6561ef).
        # 2026-10-22: 98 → 99. F69-R 정식 승격(녹화 case-f69-r-v3-ca74a36b).
        # 2026-10-23: 99 → 100. F36-P 정식 승격(녹화 case-f36-p-v3-9121d2b4).
        # 2026-10-23: 100 → 101. F65-P 정식 승격(녹화 case-f65-p-v3-a5bd3c0c).
        # 2026-10-23: 101 → 102. F70-R 정식 승격(녹화 case-f70-r-v3-676ed238).
        # 2026-10-23: 102 → 103. F71-R 정식 승격(녹화 case-f71-r-v3-1dcb2af1).
        # 2026-10-23: 103 → 104. F70-P 정식 승격(녹화 case-f70-p-v3-3ce5d49b).
        # 2026-10-24: 104 → 105. F73-R 정식 승격(녹화 case-f73-r-v3-4ef2a131).
        # 2026-10-24: 105 → 106. F75-R 정식 승격(녹화 case-f75-r-v3-2e645346).
        # 2026-10-24: 106 → 107. F76-R 정식 승격(녹화 case-f76-r-v3-7b1b6306).
        # 2026-10-24: 107 → 108. F76-P 정식 승격(녹화 case-f76-p-v3-ec3be401).
        # 2026-10-24: 108 → 109. F77-R 정식 승격(녹화 case-f77-r-v3-6040e57b).
        # 2026-10-25: 109 → 110. F71-H 정식 승격(녹화 case-f71-h-v3-402fa1c3).
        # 2026-10-25: 110 → 111. F79-R 정식 승격(녹화 case-f79-r-v3-3ae8c5c1).
        self.assertEqual(stages.count("official"), 111)
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
        # 2026-10-10: 27 → 28. F55-R(food dispatch 릴리스의 요청 IP 기준 요청 한도로 order 호출이 429, 주문 503) 후보 추가.
        # 2026-10-10: 28 → 29. F56-R(banking 해지 계좌 정리가 잘못된 id 로 commerce 정산 계좌 두 행을 지워 checkout 502) 후보 추가.
        # 2026-10-10: 29 → 30. F51-H(banking account 릴리스의 DB 연결 누수로 풀이 굳어 잔액 조회와 이체 500, 502) 후보 추가.
        # 2026-10-10: 30 → 31. F57-R(표 이전이 banking TRANSFERS identity 생성기 상태를 옮기지 않아 새 이체가 기본 키 ORA-00001 로 실패) 후보 추가.
        # 2026-10-10: 31 → 32. F23-P(운영을 가리킨 로컬 마이그레이션이 commerce 상품 표를 지워 상품 조회 42P01, checkout 502) 후보 추가.
        # 2026-10-10: 32 → 33. F58-R(food order 릴리스가 개발 프로필에만 있는 정책값을 읽다 운영에서 없어 주문 생성 전량 500) 후보 추가.
        # 2026-10-10: 33 → 34. F59-R(food 내부 백필 Job 이 dispatch 배달 목록을 깊은 offset 페이지로 읽어 dispatch 풀이 묶이고 주문 503) 후보 추가.
        # 2026-10-10: 34 → 35. F59-P(banking 원장 쪽 명세서 백필 Job 이 계좌마다 이체 내역을 읽어 transfer 풀이 묶이고 이체 500, 정산 실패) 후보 추가.
        # 2026-10-10: 35 → 36. F36-H(food restaurant 릴리스가 개발 프로필에만 있는 정책값을 가게 상세마다 읽다 운영에서 없어 상세 500, 주문 502) 후보 추가.
        # 2026-10-10: 36 → 37. F41-H(banking account 릴리스가 헬스 하위 그룹을 꺼 liveness 404 재시작 반복, 잔액 조회와 이체 502) 후보 추가.
        # 2026-10-10: 37 → 38. F60-R(food 배차 이력 표 외래 키 마이그레이션의 표 복사 잠금으로 배차 쓰기 대기, dispatch 풀 고갈, 주문 503) 후보 추가.
        # 2026-10-11: 38 → 39. F57-P(표 이전이 commerce 재고 이동 이력 표 id 시퀀스 상태를 옮기지 않아 재고 예약이 기본 키 23505 로 실패, checkout 502) 후보 추가.
        # 2026-10-11: 39 → 38. F40-R 정식 승격(녹화 case-f40-r-v3-50e3ecb9).
        # 2026-10-11: 38 → 39. F63-R(banking api 릴리스가 요청마다 스레드 하나짜리 풀을 만들고 닫지 않아 컨테이너 메모리 한도에서 OOM 재시작 반복, 이체와 거래 내역 502) 후보 추가.
        # 2026-10-11: 39 → 38. F17-H 정식 승격(녹화 case-f17-h-v3-632a04d1).
        # 2026-10-11: 38 → 39. F64-R(commerce api-gateway 설정 롤아웃이 order 서킷브레이커에 checkout 정상 지연보다 낮은 느린 호출 임계를 켬) 후보 추가.
        # 2026-10-11: 39 → 40. F65-R(banking transfer 일상 롤아웃이 낡은 DB 연결 서비스 이름을 다시 넣어 transfer 가 Oracle 에 붙지 못함) 후보 추가.
        # 2026-10-11: 40 → 41. F66-R(commerce 회원 표 열 추가 마이그레이션이 느린 내보내기 질의 뒤에서 잠금을 기다려 회원 표 질의가 모두 줄을 섬) 후보 추가.
        # 2026-10-11: 41 → 42. F64-P(banking api 설정 롤아웃이 거래 내역 서킷브레이커에 거래 내역 정상 지연보다 낮은 느린 호출 임계를 켬) 후보 추가.
        # 2026-10-11: 42 → 41. F32-H 정식 승격(녹화 case-f32-h-v3-531094f2).
        # 2026-10-11: 41 → 42. F66-P(commerce 재고 이벤트 outbox 열 추가 마이그레이션이 느린 이벤트 내보내기 뒤에서 잠금을 기다려 재고 예약이 멈춤) 후보 추가.
        # 2026-10-11: 42 → 43. F60-H(food 배차 표 열 추가 마이그레이션이 느린 내보내기 뒤에서 메타데이터 잠금을 기다려 배차 표 질의가 모두 줄을 섬) 후보 추가.
        # 2026-10-11: 43 → 44. F54-P(food MySQL 앱 계정의 시간당 쓰기 한도로 주문 저장이 1226, 주문 500) 후보 추가.
        # 2026-10-11: 44 → 43. F33-P 정식 승격(녹화 case-f33-p-v3-e79a2db0).
        # 2026-10-11: 43 → 44. F51-P(food 재배차 정정 작업의 입력 누락으로 끝난 배차가 모두 배정 중으로 되살아나 배달원 용량이 차고 주문 503) 후보 추가.
        # 2026-10-11: 44 → 45. F39-H(banking account 릴리스의 이체 접수 응답 구조 변경으로 api 가 응답을 읽지 못해 api 를 거치는 이체 전량 502) 후보 추가.
        # 2026-10-11: 45 → 46. F63-P(food dispatch 릴리스의 배달 조회 스레드 누수로 컨테이너 OOM 재시작이 되풀이되고 그 구간마다 주문 503) 후보 추가.
        # 2026-10-11: 46 → 45. F41-R 정식 승격(녹화 case-f41-r-v3-f37b1984).
        # 2026-10-11: 45 → 46. F67-R(운영자의 VACUUM FULL 이 commerce 결제 감사 로그를 다시 쓰는 내내 잠가 결제가 멈추고 checkout 502) 후보 추가.
        # 2026-10-11: 46 → 47. F37-H(banking api 릴리스가 개발 프로필에만 있는 이체 한도 설정을 읽어 모든 이체 접수 500) 후보 추가.
        # 2026-10-11: 47 → 48. F53-H(food 결제 표 열 추가 마이그레이션이 느린 내보내기 뒤에서 메타데이터 잠금을 기다려 모든 새 주문이 결제 단계에서 502) 후보 추가.
        # 2026-10-11: 48 → 47. F42-R 정식 승격(녹화 case-f42-r-v3-26143861).
        # 2026-10-11: 47 → 48. F67-H(commerce 주문 품목 표 열 추가 마이그레이션이 느린 매출 내보내기 뒤에서 잠금을 기다려 checkout 마다 품목 INSERT 가 order 연결을 쥔 채 멈추고 checkout 5xx) 후보 추가.
        # 2026-10-11: 48 → 49. F60-P(food 배차 이벤트 outbox 표를 취소된 인덱스 백필이 치워 배차가 커밋에서 되돌려지고 모든 새 주문 503) 후보 추가.
        # 2026-10-11: 49 → 48. F43-R 정식 승격(녹화 case-f43-r-v3-aecd7e38).
        # 2026-10-11: 48 → 49. F35-P(운영을 가리킨 로컬 마이그레이션이 banking Oracle 계좌 표를 지워 잔액 조회, 고객 이체, commerce 정산 실패) 후보 추가.
        # 2026-10-11: 49 → 50. F16-P(commerce notification 릴리스가 회원 목록 전체를 이벤트마다 다시 읽어 user-service 가 묻히고 쓰기 토큰 확인 실패) 후보 추가.
        # 2026-10-12: 50 → 49. F44-R 정식 승격(녹화 case-f44-r-v3-f7f70c65).
        # 2026-10-12: 49 → 50. F68-R(banking transfer 릴리스의 계좌별 속도 제한이 이름 하나의 제한기라 모든 이체가 초당 2건을 나눠 써 banking 이체와 commerce checkout 이 함께 실패) 후보 추가.
        # 2026-10-12: 50 → 49. F44-P 정식 승격(녹화 case-f44-p-v3-4ee5c786).
        # 2026-10-12: 49 → 50. F69-R(food 결제 감사 자료 내보내기 Job 의 mysqldump 표 읽기 잠금이 결제 쓰기를 막아 모든 새 주문이 결제 단계에서 502) 후보 추가.
        # 2026-10-12: 50 → 51. F36-P(food 메뉴 표 열 추가 마이그레이션이 느린 판매 실적 내보내기 뒤에서 메타데이터 잠금을 기다려 메뉴 보기와 주문 불가) 후보 추가.
        # 2026-10-12: 51 → 52. F65-P(commerce user 일상 롤아웃이 회전 전 DB 계정 이름을 다시 넣어 user 가 PostgreSQL 인증에 실패하고 비어 쓰기 500) 후보 추가.
        # 2026-10-12: 52 → 53. F70-R(banking 정산 대사 시스템의 page 없는 30일 거래 내역 조회가 transfer 힙을 다 써 GC·OutOfMemoryError 로 멈추고 liveness 재시작 반복, banking 이체와 commerce checkout 502) 후보 추가.
        # 2026-10-12: 53 → 54. F71-R(commerce PostgreSQL 앱 역할 비밀번호 기한 경과로 여덟 서비스의 풀이 수명대로 비어 쓰기 500, 상품 조회 502) 후보 추가.
        # 2026-10-12: 54 → 55. F72-R(banking 워커 tb-w2 방화벽이 같은 노드 앱 파드의 Oracle 재연결을 버려 세 서비스 풀이 수명대로 비어 banking·정산 502) 후보 추가.
        # 2026-10-12: 55 → 56. F70-P(food 배달원 정산 시스템의 page 없는 배달 완료 전체 조회가 dispatch 힙을 다 써 연속 GC 로 멈추고 liveness 재시작 반복, 새 주문이 용량 확인에서 503) 후보 추가.
        # 2026-10-12: 56 → 57. F73-R(commerce 상품 표 열 추가 마이그레이션이 느린 카테고리 매출 내보내기 뒤에서 잠금을 기다려 상품 둘러보기와 checkout 5xx) 후보 추가.
        # 2026-10-12: 57 → 58. F74-R(banking 가맹점 입금 확인 폴링 주기 단축이 거래 내역 Page 건수 질의로 Oracle CPU 를 포화, transfer 풀이 묶여 이체와 commerce checkout 간헐적 5xx) 후보 추가.
        # 2026-10-12: 58 → 59. F75-R(food dispatch 만 용량 API 경로를 옮긴 릴리스로 롤아웃되어 짝 order 릴리스 없이 옛 경로가 400, 모든 새 주문 503) 후보 추가.
        # 2026-10-12: 59 → 60. F76-R(commerce product-service 릴리스 1.4.0 이 액상 상품 니코틴 함량을 설명에서 확인 없이 읽어 함량 표기 없는 액상 상품이 든 상품 목록 500) 후보 추가.
        # 2026-10-12: 60 → 61. F76-P(food restaurant-service 릴리스 1.7.0 이 메뉴의 카테고리별 조리 시간을 확인 없이 읽어 카테고리 4~8 가게의 메뉴 500) 후보 추가.
        # 2026-10-12: 61 → 62. F77-R(이름이 겹친 원장 워커 매니페스트 적용이 transfer Deployment 를 원장 이미지로 바꿔 transfer 가 비고 banking 이체와 commerce checkout 502) 후보 추가.
        # 2026-10-12: 62 → 61. F48-R 정식 승격(녹화 case-f48-r-v3-574963e1).
        # 2026-10-12: 61 → 62. F71-H(commerce PostgreSQL 접근 규칙을 좁혀 같은 노드 자기 파드 주소로 붙는 여덟 서비스의 새 로그인이 거절되고 풀이 수명대로 비어 쓰기 500, 상품 조회 502) 후보 추가.
        # 2026-10-12: 62 → 63. F79-R(food payment-service 릴리스 1.4.0 이 외부 PG 결제 주소를 구분자 없이 이어 만들어 모든 결제 502, 주문 전량 502) 후보 추가.
        # 2026-10-12: 63 → 62. F49-R 정식 승격(녹화 case-f49-r-v3-1af8dad9).
        # 2026-10-13: 62 → 61. F33-H 정식 승격(녹화 case-f33-h-v3-f19ed3f1).
        # 2026-10-13: 61 → 60. F49-H 정식 승격(녹화 case-f49-h-v3-b873cb3e).
        # 2026-10-13: 60 → 59. F50-R 정식 승격(녹화 case-f50-r-v3-75ae96d0).
        # 2026-10-14: 59 → 58. F48-P 정식 승격(녹화 case-f48-p-v3-3667031d).
        # 2026-10-14: 58 → 57. F51-R 정식 승격(녹화 case-f51-r-v3-0c3126f0).
        # 2026-10-14: 57 → 56. F43-P 정식 승격(녹화 case-f43-p-v3-e132f196).
        # 2026-10-14: 56 → 55. F40-H 정식 승격(녹화 case-f40-h-v3-b472a2f0).
        # 2026-10-14: 55 → 54. F52-R 정식 승격(녹화 case-f52-r-v3-13501208).
        # 2026-10-14: 54 → 53. F53-R 정식 승격(녹화 case-f53-r-v3-d0f23131).
        # 2026-10-14: 53 → 52. F53-P 정식 승격(녹화 case-f53-p-v3-b029727d).
        # 2026-10-15: 52 → 51. F35-H 정식 승격(녹화 case-f35-h-v3-a1206bfd).
        # 2026-10-15: 51 → 50. F32-P 정식 승격(녹화 case-f32-p-v3-58d43138).
        # 2026-10-15: 50 → 49. F54-R 정식 승격(녹화 case-f54-r-v3-31a93bbe).
        # 2026-10-15: 49 → 48. F49-P 정식 승격(녹화 case-f49-p-v3-0a4fe786).
        # 2026-10-15: 48 → 47. F55-R 정식 승격(녹화 case-f55-r-v3-ba950165).
        # 2026-10-16: 47 → 46. F56-R 정식 승격(녹화 case-f56-r-v3-24b89933).
        # 2026-10-16: 46 → 45. F51-H 정식 승격(녹화 case-f51-h-v3-c093a2ed).
        # 2026-10-16: 45 → 46. F80-R(banking 워커 tb-w2 의 Oracle 데이터 파일 일상 백업이 zstd 장거리 창으로 노드 메모리를 거의 다 써 banking 파드 축출, OOM) 후보 추가.
        # 2026-10-17: 46 → 45. F57-R 정식 승격(녹화 case-f57-r-v3-419736f6).
        # 2026-10-17: 45 → 44. F23-P 정식 승격(녹화 case-f23-p-v3-339d153c).
        # 2026-10-17: 44 → 43. F58-R 정식 승격(녹화 case-f58-r-v3-4568fd65).
        # 2026-10-18: 43 → 42. F59-R 정식 승격(녹화 case-f59-r-v3-cdc14d3d).
        # 2026-10-18: 42 → 41. F59-P 정식 승격(녹화 case-f59-p-v3-6fd09c9b).
        # 2026-10-18: 41 → 40. F36-H 정식 승격(녹화 case-f36-h-v3-b671604f).
        # 2026-10-18: 40 → 39. F41-H 정식 승격(녹화 case-f41-h-v3-2c392569).
        # 2026-10-18: 39 → 38. F60-R 정식 승격(녹화 case-f60-r-v3-22036218).
        # 2026-10-19: 38 → 37. F57-P 정식 승격(녹화 case-f57-p-v3-2b7db69d).
        # 2026-10-19: 37 → 36. F64-R 정식 승격(녹화 case-f64-r-v3-9014da8a).
        # 2026-10-20: 36 → 35. F65-R 정식 승격(녹화 case-f65-r-v3-b5383723).
        # 2026-10-20: 35 → 34. F66-R 정식 승격(녹화 case-f66-r-v3-346c6170).
        # 2026-10-20: 34 → 33. F64-P 정식 승격(녹화 case-f64-p-v3-7b180ead).
        # 2026-10-20: 33 → 32. F66-P 정식 승격(녹화 case-f66-p-v3-a79bda26).
        # 2026-10-20: 32 → 31. F60-H 정식 승격(녹화 case-f60-h-v3-1aece1d2).
        # 2026-10-20: 31 → 30. F54-P 정식 승격(녹화 case-f54-p-v3-fb67a747).
        # 2026-10-20: 30 → 29. F51-P 정식 승격(녹화 case-f51-p-v3-1c6b181c).
        # 2026-10-21: 29 → 28. F39-H 정식 승격(녹화 case-f39-h-v3-255629f2).
        # 2026-10-21: 28 → 27. F63-P 정식 승격(녹화 case-f63-p-v3-ddf34faa).
        # 2026-10-21: 27 → 26. F67-R 정식 승격(녹화 case-f67-r-v3-654a133c).
        # 2026-10-21: 26 → 25. F37-H 정식 승격(녹화 case-f37-h-v3-a6cd30bd).
        # 2026-10-21: 25 → 24. F53-H 정식 승격(녹화 case-f53-h-v3-d20267b1).
        # 2026-10-21: 24 → 23. F67-H 정식 승격(녹화 case-f67-h-v3-53919475).
        # 2026-10-22: 23 → 22. F60-P 정식 승격(녹화 case-f60-p-v3-04d37516).
        # 2026-10-22: 22 → 21. F35-P 정식 승격(녹화 case-f35-p-v3-11824edf).
        # 2026-10-22: 21 → 20. F16-P 정식 승격(녹화 case-f16-p-v3-e40ec615).
        # 2026-10-22: 20 → 19. F68-R 정식 승격(녹화 case-f68-r-v3-3f6561ef).
        # 2026-10-22: 19 → 18. F69-R 정식 승격(녹화 case-f69-r-v3-ca74a36b).
        # 2026-10-23: 18 → 17. F36-P 정식 승격(녹화 case-f36-p-v3-9121d2b4).
        # 2026-10-23: 17 → 16. F65-P 정식 승격(녹화 case-f65-p-v3-a5bd3c0c).
        # 2026-10-23: 16 → 15. F70-R 정식 승격(녹화 case-f70-r-v3-676ed238).
        # 2026-10-23: 15 → 14. F71-R 정식 승격(녹화 case-f71-r-v3-1dcb2af1).
        # 2026-10-23: 14 → 13. F70-P 정식 승격(녹화 case-f70-p-v3-3ce5d49b).
        # 2026-10-24: 13 → 12. F73-R 정식 승격(녹화 case-f73-r-v3-4ef2a131).
        # 2026-10-24: 12 → 11. F75-R 정식 승격(녹화 case-f75-r-v3-2e645346).
        # 2026-10-24: 11 → 10. F76-R 정식 승격(녹화 case-f76-r-v3-7b1b6306).
        # 2026-10-24: 10 → 9. F76-P 정식 승격(녹화 case-f76-p-v3-ec3be401).
        # 2026-10-24: 9 → 8. F77-R 정식 승격(녹화 case-f77-r-v3-6040e57b).
        # 2026-10-25: 8 → 7. F71-H 정식 승격(녹화 case-f71-h-v3-402fa1c3).
        # 2026-10-25: 7 → 6. F79-R 정식 승격(녹화 case-f79-r-v3-3ae8c5c1).
        self.assertEqual(stages.count("candidate"), 6)


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
