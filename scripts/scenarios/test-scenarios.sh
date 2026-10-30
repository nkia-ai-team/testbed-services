#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd -- "$script_dir/../.." && pwd)"
catalog="$script_dir/catalog.json"
manifest_dir="$script_dir/manifests"

total="$(jq '.scenarios | length' "$catalog")"
# 2026-10-08: 60 → 61. F30-R(draft) 추가.
# 2026-10-08: 61 → 62. F32-R(후보) 추가.
# 2026-10-08: 62 → 63. F33-R(후보) 추가.
# 2026-10-08: 63 → 64. F35-R(후보) 추가.
# 2026-10-08: 64 → 65. F36-R(후보) 추가.
# 2026-10-08: 65 → 66. F37-R(후보) 추가.
# 2026-10-08: 66 → 67. F38-R(후보) 추가.
# 2026-10-08: 67 → 68. F39-R(후보) 추가.
# 2026-10-09: 68 → 69. F40-R(후보) 추가.
# 2026-10-09: 69 → 70. F17-H(후보) 추가.
# 2026-10-09: 70 → 71. F32-H(후보) 추가.
# 2026-10-09: 71 → 72. F33-P(후보) 추가.
# 2026-10-09: 72 → 73. F41-R(후보) 추가.
# 2026-10-09: 73 → 74. F42-R(후보) 추가.
# 2026-10-09: 74 → 75. F43-R(후보) 추가.
# 2026-10-09: 75 → 76. F44-R(후보) 추가.
# 2026-10-09: 76 → 77. F44-P(후보) 추가.
# 2026-10-09: 77 → 78. F47-R(후보) 추가.
# 2026-10-09: 78 → 79. F48-R(후보) 추가.
# 2026-10-09: 79 → 80. F49-R(후보) 추가.
# 2026-10-09: 80 → 81. F33-H(후보) 추가.
# 2026-10-09: 81 → 82. F49-H(후보) 추가.
# 2026-10-09: 82 → 83. F50-R(후보) 추가.
# 2026-10-09: 83 → 84. F48-P(후보) 추가.
# 2026-10-09: 84 → 85. F51-R(후보) 추가.
# 2026-10-10: 85 → 86. F43-P(후보) 추가.
# 2026-10-10: 86 → 87. F40-H(후보) 추가.
# 2026-10-10: 87 → 88. F52-R(후보) 추가.
# 2026-10-10: 88 → 89. F53-R(후보) 추가.
# 2026-10-10: 89 → 90. F53-P(후보) 추가.
# 2026-10-10: 90 → 91. F42-P(후보) 추가.
# 2026-10-10: 91 → 92. F35-H(후보) 추가.
# 2026-10-10: 92 → 93. F32-P(후보) 추가.
# 2026-10-10: 93 → 94. F54-R(후보) 추가.
# 2026-10-10: 94 → 95. F49-P(후보) 추가.
# 2026-10-10: 95 → 96. F55-R(후보) 추가.
# 2026-10-10: 96 → 97. F56-R(후보) 추가.
# 2026-10-10: 97 → 98. F51-H(후보) 추가.
# 2026-10-10: 98 → 99. F57-R(후보) 추가.
# 2026-10-10: 99 → 100. F23-P(후보) 추가.
# 2026-10-10: 100 → 101. F58-R(후보) 추가.
# 2026-10-10: 101 → 102. F59-R(후보) 추가.
# 2026-10-10: 102 → 103. F59-P(후보) 추가.
# 2026-10-10: 103 → 104. F36-H(후보) 추가.
# 2026-10-10: 104 → 105. F41-H(후보) 추가.
# 2026-10-10: 105 → 106. F60-R(후보) 추가.
# 2026-10-11: 106 → 107. F57-P(후보) 추가.
# 2026-10-11: 107 → 108. F63-R(후보) 추가.
# 2026-10-11: 108 → 109. F64-R(후보) 추가.
# 2026-10-11: 109 → 110. F65-R(후보) 추가.
# 2026-10-11: 110 → 111. F66-R(후보) 추가.
# 2026-10-11: 111 → 112. F64-P(후보) 추가.
# 2026-10-11: 112 → 113. F66-P(후보) 추가.
# 2026-10-11: 113 → 114. F60-H(후보) 추가.
# 2026-10-11: 114 → 115. F54-P(후보) 추가.
# 2026-10-11: 115 → 116. F51-P(후보) 추가.
# 2026-10-11: 116 → 117. F39-H(후보) 추가.
# 2026-10-11: 117 → 118. F63-P(후보) 추가.
# 2026-10-11: 118 → 119. F67-R(후보) 추가.
# 2026-10-11: 119 → 120. F37-H(후보) 추가.
# 2026-10-11: 120 → 121. F53-H(후보) 추가.
# 2026-10-11: 121 → 122. F67-H(후보) 추가.
# 2026-10-11: 122 → 123. F60-P(후보) 추가.
# 2026-10-11: 123 → 124. F35-P(후보) 추가.
# 2026-10-11: 124 → 125. F16-P(후보) 추가.
# 2026-10-12: 125 → 126. F68-R(후보) 추가.
# 2026-10-12: 126 → 127. F69-R(후보) 추가.
# 2026-10-12: 127 → 128. F36-P(후보) 추가.
# 2026-10-12: 128 → 129. F65-P(후보) 추가.
# 2026-10-12: 129 → 130. F70-R(후보) 추가.
# 2026-10-12: 130 → 131. F71-R(후보) 추가.
# 2026-10-12: 131 → 132. F72-R(후보) 추가.
# 2026-10-12: 132 → 133. F70-P(후보) 추가.
# 2026-10-12: 133 → 134. F73-R(후보) 추가.
# 2026-10-12: 134 → 135. F74-R(후보) 추가.
# 2026-10-12: 135 → 136. F75-R(후보) 추가.
# 2026-10-12: 136 → 137. F76-R(후보) 추가.
# 2026-10-12: 137 → 138. F76-P(후보) 추가.
# 2026-10-12: 138 → 139. F77-R(후보) 추가.
# 2026-10-12: 139 → 140. F71-H(후보) 추가.
# 2026-10-12: 140 → 141. F79-R(후보) 추가.
# 2026-10-16: 141 → 142. F80-R(후보) 추가.
# 2026-10-30: 142 → 143. F69-P(후보) 추가.
[[ "$total" -eq 143 ]]
# Internal consistency: ids, slugs and manifests track the catalog exactly, so
# these are derived rather than pinned — a pinned copy is what went stale here
# (the suite asserted 64 long after the catalog moved to 60, and failed silently
# in anything that did not check its exit code).
[[ "$(find "$manifest_dir" -maxdepth 1 -type f -name '*.yaml' | wc -l)" -eq "$total" ]]
[[ "$(jq '[.scenarios[].id] | unique | length' "$catalog")" -eq "$total" ]]
[[ "$(jq '[.scenarios[].slug] | unique | length' "$catalog")" -eq "$total" ]]
# bin/ predates the profile-executor architecture and has drifted: 18 scripts
# have no catalog entry and 14 catalog entries have no script. Pinned so new
# drift trips, not as an endorsement — cleanup is a separate backlog item.
[[ "$(find "$script_dir/bin" -maxdepth 1 -type f -name '*.sh' | wc -l)" -eq 64 ]]
# 2026-07-29: ready 41→43, parked 14→12. F15-H·F15-T2 승격(c83fcaf)이 이 핀을 두고 갔다.
# 이 스위트가 deploy-live-promotions.sh의 첫 관문이라, 핀이 어긋난 동안 배포 자체가
# 막혀 있었다 — 승격 커밋과 핀 갱신은 같은 커밋에 있어야 한다.
# The readiness split IS a governed decision, so it stays pinned.
# parked = 2026-07-27 골든 감사 CUT 26종. 설계 자산은 남기고 실행에서만 뺀다.
# 2026-07-28: ready 31→32, parked 26→25. F03-H가 복귀했다 — 주입 표면을 자백하던
# OrderController의 Thread.sleep(delayMs)을 실제 결함(직렬화된 O(n^2) 리포트 렌더러)으로
# 교체해 G6 누설을 없앴다.
# 2026-07-28: readiness에 cut을 신설하고 음성 시나리오 4종(F01-G·F03-G·F05-G·F11-G)을
# 옮겼다(parked 24→20). parked는 "회생 후보"라는 뜻이므로 회생 조건이 코드가 아니라
# 헌장 개정인 것을 같은 칸에 두면 영원히 열릴 것처럼 읽힌다. cut은 종착역이다.
# 2026-07-28: ready 33→37. 스토리지 포화 3종(F02-H·F10-H·F10-P)과 복합 자원 고갈
# F15-P가 들어왔다. 마지막까지 이들을 막고 있던 것은 도구도 계약도 아니라
# "장치가 바쁘다"를 셀 수 없다는 것이었다 — host.disk_io_utilization 신설로 풀렸다.
# 2026-07-28: ready 37→39. Tomcat 스레드풀 포화 짝(F21-P·F21-Q). 좌표는 배치 고정으로
# 이미 갈렸고(F09-R과 다른 노드), 마지막 차단은 busy-thread 신호가 non-daemon 스레드를
# 세고 있었다는 것이다 — Tomcat 워커는 daemon이라 그 합은 4~5에 붙박이였다.
# 2026-07-28: ready 39→41. F09-H·F09-P가 07-27 parked에서 복귀했다. 감별 신호가
# 없다던 판정이 둘 다 틀렸다 — GC는 재는 방법(used_after_last_gc/limit 비율)이 없었을
# 뿐이고, 스로틀은 신호가 있었으나 러너 템플릿이 testbed-product로 하드코딩돼 있어
# F12-H 말고는 아무도 쓸 수 없었다.
# 2026-07-28: ready 41→42. F17-P. 카탈로그가 적어둔 배선 4항 중 둘은 이미 완료돼
# 있었고, 원장 역분개는 주입 금액을 1~5로 낮춰 오염 자체를 없애는 것으로 대체했다.
# 2026-07-28: ready 42→43, blocked 1→0. F04-H. 유일한 blocked였고, 필요하던
# 제어 표면(@ConditionalOnProperty)은 앱에 이미 있었다 — 없던 것은 commerce PG의
# 미발행 outbox를 세는 관측이며 러너에 신설했다.
# 2026-07-28: ready 43→41. 부하 도달 가능성 전수 점검에서 F07-P·F20-P가 반증됐다.
# 둘 다 "부하를 부으면 뻗는다"를 서비스 시간 측정 없이 가정하고 있었다.
# 2026-07-29: ready 43→44. F14-P. 필요하던 "catch-swallow injector"는 앱에 심을 필요가
# 없었다 — 결함(삼킴 + 자동 ack = 영구 유실)은 이미 코드에 있고, 없던 것은 그것을
# 발화시킬 쓰기 실패다. ledger_entries만 READ ONLY로 뒤집는다(db.table_readonly 신설).
# 짝인 F14-R은 같은 심사에서 반증돼 parked로 남았다: 중첩 타임아웃이 안쪽(10s) <
# 바깥쪽(15s)이라 "커밋됐는데 응답만 유실"이 구조적으로 생기지 않는다.
# 2026-08-06: ready 44→41, parked 11→14. c887e20 이 F02-H·F21-P·F21-Q 를 파킹할 때
# 카탈로그만 옮기고 이 게이트를 안 고쳐서, 그날부터 clean tree 에서도 이 스크립트가
# exit 1 이었다(0806 배치 수리 중 발견). 셋 다 라이브 두 번이 인과 부재·성공↔감별자
# 상호배타를 실증한 건이고, 재설계는 별도 트랙(F21 은 app.control 지연 표면 구현 완료,
# 승격은 사다리 실측 후).
# 2026-08-09: ready 41→39, parked 14→16. F09-R·F03-P 파킹. 라이브 4회(0806~0809)에서
# 네 번 모두 스킵이고 실패 성격이 같다 — 주입은 도달하는데 피해가 생기지 않는다.
# F09-R 은 노드 CPU 99.9% 에 pricing 12.8, F03-P 는 payment 1,614ms 에 checkout 5xx 가
# 전 틱 0 이다. 살리려면 주입 지점 자체를 바꿔야 하므로 사실상 새 시나리오이고,
# 그동안 매 배치 40분 이상을 쓴다. 삭제가 아니라 보관이다(controllers-parked.json).
# 2026-10-08: ready 39→40. F30-R 을 첫 시험 실행(calibration)을 위해 ready + mode calibration 으로 올렸다
# (F10-P 방식). compile-plan 이 controller 를 ready 에만 허용하므로 러너에서 돌리려면 승격이 먼저다.
# 2026-10-08: ready 40→36, parked 16→20. 정상 녹화가 없는 후보 4종(F10-P, F15-R, F15-T2, F20-Q)을 사용자 결정으로 폐기했다.
# 2026-10-08: ready 36→37. F32-R(food dispatch 배차 한도 설정 배포)을 후보(ready + stage candidate)로 추가했다.
# 2026-10-08: ready 37→38. F33-R(food dispatches 인덱스 제거)을 후보(ready + stage candidate)로 추가했다.
# 2026-10-08: ready 38→39. F35-R(banking Oracle 애플리케이션 계정 잠금)을 후보(ready + stage candidate)로 추가했다.
# 2026-10-08: ready 39→40. F36-R(food restaurants 열 이름 변경 마이그레이션)을 후보(ready + stage candidate)로 추가했다.
# 2026-10-08: ready 40→41. F37-R(banking api 파드 DNS 정책 노드 resolver 배포)을 후보(ready + stage candidate)로 추가했다.
# 2026-10-08: ready 41→42. F38-R(food MySQL 인스턴스 전체 read_only)을 후보(ready + stage candidate)로 추가했다.
# 2026-10-08: ready 42→43. F39-R(banking account 이체 하류 주소 오설정)을 후보(ready + stage candidate)로 추가했다.
# 2026-10-09: ready 43→44. F40-R(commerce pricing 프로모션 할인율 오입력)을 후보(ready + stage candidate)로 추가했다.
# 2026-10-09: ready 44→45. F17-H(banking transfer 릴리스 이미지 부재 롤아웃)을 후보(ready + stage candidate)로 추가했다.
# 2026-10-09: ready 45→46. F32-H(food dispatch JSON 숫자 문자열 직렬화 설정 배포)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-09: ready 46→47. F33-P(commerce auth_tokens 인덱스 교체 마이그레이션 실패)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-09: ready 47→48. F41-R(banking account 메모리 누수 릴리스 롤아웃)을 후보(ready + stage candidate)로 추가했다.
# 2026-10-09: ready 48→49. F42-R(banking transfer 전수 스캔 한도 질의 릴리스 롤아웃)을 후보(ready + stage candidate)로 추가했다.
# 2026-10-09: ready 49→50. F43-R(commerce cart DB 연결 누수 릴리스 롤아웃)을 후보(ready + stage candidate)로 추가했다.
# 2026-10-09: ready 50→51. F44-R(food 워커 tb-w3 방화벽 허용 목록이 order 파드 대역을 빠뜨려 restaurant 로의 새 연결이 버려짐)을 후보(ready + stage candidate)로 추가했다.
# 2026-10-09: ready 51→52. F44-P(banking 워커 tb-w2 방화벽 허용 목록이 commerce 파드 대역을 빠뜨려 정산 이체 새 연결이 버려짐)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-09: ready 52→53. F47-R(banking api 릴리스가 이체마다 account 계좌 목록 전체를 다시 읽어 account 포화)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-09: ready 53→54. F48-R(food 인기 메뉴 집계 표를 취소된 인덱스 백필이 치움)을 후보(ready + stage candidate)로 추가했다.
# 2026-10-09: ready 54→55. F49-R(food restaurant 릴리스가 가게 상세 응답 구조를 바꿔 order 가 해석 실패)을 후보(ready + stage candidate)로 추가했다.
# 2026-10-09: ready 55→56. F33-H(food dispatch 릴리스가 배달 목록에 전체 건수 COUNT 를 붙여 MySQL 포화, dispatch 풀 고갈)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-09: ready 56→57. F49-H(food order 릴리스가 시각 표기를 바꿔 dispatch 배차 응답을 못 읽고 주문 503)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-09: ready 57→58. F50-R(banking 네임스페이스 메모리 할당량이 사용량 아래라 transfer 재배포의 새 파드가 거절됨)을 후보(ready + stage candidate)로 추가했다.
# 2026-10-09: ready 58→59. F48-P(food 주문 이벤트 outbox 표를 취소된 인덱스 백필이 치워 주문 생성이 1146 으로 되돌려짐)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-09: ready 59→60. F51-R(운영 용량 명령 입력 실수로 banking account Deployment 가 replicas 0)을 후보(ready + stage candidate)로 추가했다.
# 2026-10-10: ready 60→61. F43-P(food dispatch 릴리스의 중복 배차 확인이 배차마다 풀 연결을 새어 dispatch 풀 고갈, 주문 503)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-10: ready 61→62. F40-H(banking transfer 릴리스의 원 단위 금액 검증이 소수 표기 commerce 정산 이체를 400 으로 거절, checkout 502)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-10: ready 62→63. F52-R(banking 워커 tb-w2 의 이미지 보존 정리가 쓰고 있는 앱 이미지를 지워 transfer 일상 재배포의 새 파드가 ErrImageNeverPull, 이체와 commerce 정산 502)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-10: ready 63→64. F53-R(운영을 가리킨 로컬 마이그레이션이 food orders 표를 지워 주문 생성이 1146 으로 전량 500)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-10: ready 64→65. F53-P(운영을 가리킨 로컬 마이그레이션이 food payments 표를 지워 payment 가 1146 으로 500, 주문이 배차 뒤 결제 단계에서 전량 502)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-10: ready 65→66. F42-P(food restaurant 릴리스가 인기 메뉴를 주문 원장에서 바로 세어 MySQL 포화, restaurant 풀 고갈)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-10: ready 66→67. F35-H(운영을 가리킨 로컬 마이그레이션이 banking Oracle TRANSFERS 를 지워 이체, 거래 내역, commerce 정산이 ORA-04043/00942 로 실패)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-10: ready 67→68. F32-P(운영을 가리킨 로컬 마이그레이션이 food dispatches 표를 지워 dispatch 가 1146 으로 500, 주문이 배달원 용량 확인에서 전량 503)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-10: ready 68→69. F54-R(DB 자원 한도 정비가 banking Oracle 앱 계정에 호출당 논리 읽기 한도를 둔 프로필을 붙여 거래 내역이 ORA-02395 로 502)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-10: ready 69→70. F49-P(food payment 릴리스가 결제 응답 id 를 문자열 결제 키로 바꿔 order 가 해석 실패, 주문 502 롤백)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-10: ready 70→71. F55-R(food dispatch 릴리스가 요청 IP 기준 요청 한도를 더해 order 파드 하나로 모인 호출이 429, 주문 503)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-10: ready 71→72. F56-R(banking 운영자의 해지 계좌 정리가 잘못된 id 목록으로 commerce 정산 계좌 두 행을 지워 정산 이체 400, checkout 502)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-10: ready 72→73. F51-H(banking account 릴리스가 잔액 조회에 더한 마지막 이체 조회가 빌린 DB 연결을 돌려주지 않아 풀이 굳고 readiness 이탈, 잔액 조회와 이체 500, 502)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-10: ready 73→74. F57-R(표 이전이 banking Oracle TRANSFERS 의 identity 생성기 상태를 옮기지 않아 새 이체마다 기본 키 ORA-00001, 이체와 commerce 정산 실패)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-10: ready 74→75. F23-P(운영을 가리킨 로컬 마이그레이션이 commerce PostgreSQL 상품 표를 지워 상품 조회 42P01, 둘러보기 500, checkout 502)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-10: ready 75→76. F58-R(food order 릴리스가 개발 프로필에만 있는 정책값을 주문마다 읽다 운영에서 없어 주문 생성 전량 500)을 후보(ready + stage candidate)로 추가했다.
# 2026-10-10: ready 76→77. F59-R(food 내부 백필 Job 이 dispatch 배달 목록을 깊은 offset 페이지로 읽어 dispatch 풀이 묶이고 MySQL 포화, 주문 503)을 후보(ready + stage candidate)로 추가했다.
# 2026-10-10: ready 77→78. F59-P(banking 원장 쪽 명세서 백필 Job 이 계좌마다 이체 내역을 읽어 Oracle 전수 스캔, transfer 풀이 묶여 이체 500, 정산 실패)을 후보(ready + stage candidate)로 추가했다.
# 2026-10-10: ready 78→79. F36-H(food restaurant 릴리스가 개발 프로필에만 있는 정책값을 가게 상세마다 읽다 운영에서 없어 가게 상세 전량 500, order 주문 502)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-10: ready 79→80. F41-H(banking account 릴리스가 헬스 하위 그룹을 꺼 liveness 404 로 멀쩡한 컨테이너가 재시작을 되풀이, 잔액 조회와 이체 502)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-10: ready 80→81. F60-R(food 배차 이력 표에 외래 키를 더하는 마이그레이션이 표를 복사하며 배차 쓰기를 메타데이터 잠금으로 막아 dispatch 풀 고갈, 주문 503)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-11: ready 81→82. F57-P(표 이전이 commerce PostgreSQL 재고 이동 이력 표의 id 시퀀스 상태를 옮기지 않아 새 이동 이력 INSERT 가 기본 키 23505 로 실패, 재고 예약 500, checkout 502)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-11: ready 82→83. F63-R(banking api 릴리스가 요청마다 스레드 하나짜리 풀을 만들고 닫지 않아 스레드가 쌓여 컨테이너 메모리 한도에서 OOM 재시작을 되풀이, 이체와 거래 내역 502)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-11: ready 83→84. F64-R(commerce api-gateway 설정 롤아웃이 order 라우트 서킷브레이커에 checkout 정상 지연보다 낮은 느린 호출 임계를 켬)을 후보(ready + stage candidate)로 추가했다.
# 2026-10-11: ready 84→85. F65-R(banking transfer 일상 롤아웃이 낡은 DB 연결 서비스 이름을 다시 넣어 새 파드가 Oracle 에 붙지 못하고 maxSurge 0 이라 transfer 가 빔)을 후보(ready + stage candidate)로 추가했다.
# 2026-10-11: ready 85→86. F66-R(commerce 회원 표 열 추가 마이그레이션이 느린 내보내기 질의 뒤에서 잠금을 기다려 회원 표 질의가 모두 줄을 서고 인가된 쓰기 500)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-11: ready 86→87. F64-P(banking api 설정 롤아웃이 거래 내역 서킷브레이커에 거래 내역 정상 지연보다 낮은 느린 호출 임계를 켬)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-11: ready 87→88. F66-P(commerce 재고 이벤트 outbox 열 추가 마이그레이션이 느린 이벤트 내보내기 뒤에서 잠금을 기다려 재고 예약이 멈춤)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-11: ready 88→89. F60-H(food 배차 표 열 추가 마이그레이션이 느린 내보내기 뒤에서 메타데이터 잠금을 기다려 배차 표 질의가 모두 줄을 서고 모든 새 주문 503)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-11: ready 89→90. F54-P(DB 자원 한도 정비가 food MySQL 앱 계정에 시간당 쓰기 한도를 걸어 주문 저장이 1226 으로 500)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-11: ready 90→91. F51-P(food 재배차 정정 작업이 주문 id 목록 없이 최근 4시간에 끝난 배차를 모두 배정 중으로 되돌려 배달원 용량이 차고 주문 503)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-11: ready 91→92. F39-H(banking account 릴리스가 이체 접수 응답의 status 를 {code, label} 객체로 바꿔, account 와 transfer 는 이체를 실행하고 200 인데 api 가 응답을 읽지 못해 api 를 거치는 이체 전량 502)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-11: ready 92→93. F63-P(food dispatch 릴리스가 배달 조회마다 스레드 하나짜리 풀을 만들고 닫지 않아 컨테이너가 메모리 한도에서 OOM 재시작을 되풀이하고, 그 구간마다 order 의 배달원 용량 확인이 실패해 주문 503)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-11: ready 93→94. F67-R(운영자가 공간을 되찾으려고 commerce 결제 감사 로그와 배송 이벤트 로그에 VACUUM FULL 을 나란히 돌려 다시 쓰는 내내 잠가, 결제마다 쓰는 로그 INSERT 가 기다리고 checkout 502)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-11: ready 94→95. F37-H(banking api 릴리스가 개발 프로필에만 있는 1회 이체 한도 설정을 이체마다 읽어, 운영에서 api 가 Ready 인 채 모든 이체 접수를 account 호출 전에 500 으로 끝냄)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-11: ready 95→96. F53-H(food 결제 표 열 추가 마이그레이션이 느린 내보내기 뒤에서 메타데이터 잠금을 기다려 결제 표 질의가 모두 줄을 서고 모든 새 주문이 결제 단계에서 502)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-11: ready 96→97. F67-H(commerce 주문 품목 표 열 추가 마이그레이션이 느린 매출 내보내기 뒤에서 잠금을 기다려 checkout 마다 품목 INSERT 가 order 연결을 쥔 채 멈추고 checkout 5xx)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-11: ready 97→98. F60-P(food 배차 이벤트 outbox 표를 취소된 인덱스 백필이 치워 배차가 커밋에서 되돌려지고 모든 새 주문 503)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-11: ready 98→99. F35-P(운영을 가리킨 로컬 마이그레이션이 banking Oracle 계좌 표를 지워 잔액 조회, 고객 이체, commerce 정산이 ORA-00942 로 실패)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-11: ready 99→100. F16-P(commerce notification 릴리스가 주문 이벤트 수신자를 찾으려고 user-service 회원 목록 전체를 이벤트마다 페이지 상한까지 다시 읽어, 게이트웨이가 쓰기마다 토큰을 확인하는 user-service 가 묻히고 쓰기가 500·401 로 실패)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-12: ready 100→101. F68-R(banking transfer 릴리스의 계좌별 속도 제한이 이름 하나의 제한기라 모든 이체가 초당 2건을 나눠 쓰고 넘친 요청이 500, banking 이체와 commerce checkout 이 합친 부하에 비례해 실패)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-12: ready 101→102. F69-R(food 결제 감사 자료 내보내기 Job 이 mysqldump 기본값으로 결제 표를 떠 덤프 내내 표 읽기 잠금을 쥐고 결제 쓰기가 막혀 모든 새 주문이 결제 단계에서 502)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-12: ready 102→103. F36-P(food 메뉴 표 열 추가 마이그레이션이 메뉴를 주문 품목과 이어 읽는 느린 판매 실적 내보내기 뒤에서 메타데이터 잠금을 기다려 메뉴 읽기가 모두 줄을 서고, restaurant 풀이 차서 메뉴 보기와 주문이 불가)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-12: ready 103→104. F65-P(commerce user 일상 롤아웃이 회전 전 DB 계정 이름을 다시 넣어 새 파드가 PostgreSQL 인증에 실패하고 maxSurge 0 이라 user 가 비어 쓰기 500)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-12: ready 104→105. F70-R(banking 정산 대사 시스템의 page 없는 30일 거래 내역 조회가 transfer 힙을 다 써 GC·OutOfMemoryError 로 멈추고 liveness 재시작 반복, banking 이체와 commerce checkout 502)를 후보로 추가.
# 2026-10-12: ready 105→106. F71-R(commerce PostgreSQL 앱 역할 commerce 의 비밀번호 유효 기한이 갱신 없이 지나 새 로그인만 거절, 여덟 서비스 풀이 수명대로 비어 readiness 이탈·재시작 반복, 쓰기 500·상품 조회 502)를 후보로 추가.
# 2026-10-12: ready 106→107. F72-R(banking 워커 tb-w2 방화벽 허용 목록이 같은 노드 앱 파드를 빠뜨려 Oracle 재연결이 버려지고 세 서비스 풀이 수명대로 비어 readiness 이탈, banking·정산 502)을 후보로 추가.
# 2026-10-12: ready 107→108. F70-P(food 배달원 정산 시스템의 page 없는 배달 완료 전체 조회가 dispatch 힙을 다 써 연속 GC 로 멈추고 liveness 재시작 반복, 새 주문이 용량 확인에서 503)를 후보(ready + stage candidate)로 추가.
# 2026-10-12: ready 108→109. F73-R(commerce 상품 표 열 추가 마이그레이션이 느린 카테고리 매출 내보내기 뒤에서 잠금을 기다려 상품 읽기가 모두 줄을 서고 product 풀 고갈과 재시작으로 상품 둘러보기와 checkout 5xx)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-12: ready 109→110. F74-R(banking 가맹점 입금 확인 폴링 주기 단축이 거래 내역 Page 건수 질의를 초당 8회 늘려 Oracle CPU 를 포화, transfer 풀이 묶여 이체와 commerce checkout 이 간헐적으로 5xx)를 후보(ready + stage candidate)로 추가했다.
# 2026-10-12: ready 110→111. F75-R(food dispatch 만 용량 API 경로를 옮긴 릴리스 1.10.0 으로 롤아웃되어 짝 order 릴리스 없이 옛 경로가 400, 모든 새 주문 503, 후보) 추가.
# 2026-10-12: ready 111→112. F76-R(commerce product-service 릴리스 1.4.0 이 액상 상품 니코틴 함량을 설명에서 확인 없이 읽어 함량 표기 없는 액상 상품이 든 상품 목록이 500, 후보) 추가.
# 2026-10-12: ready 112→113. F76-P(food restaurant-service 릴리스 1.7.0 이 메뉴의 카테고리별 조리 시간을 출시 카테고리 셋뿐인 표에서 확인 없이 읽어 카테고리 4~8 가게의 메뉴가 500, 후보) 추가.
# 2026-10-12: ready 113→114. F77-R(transfer 매니페스트를 복사해 이름을 그대로 둔 원장 워커 매니페스트 적용이 살아 있는 transfer Deployment 를 원장 이미지로 바꿔 transfer 가 비고 banking 이체와 commerce checkout 502, 후보) 추가.
# 2026-10-12: ready 114→115. F71-H(commerce PostgreSQL 운영자가 pg_hba 모든 주소 규칙을 노드 주소와 관리망만 받도록 좁혀, 같은 노드 자기 파드 주소로 붙는 여덟 서비스의 새 로그인이 거절되고 풀이 수명대로 비어 readiness 이탈·재시작 반복, 쓰기 500·상품 조회 502)를 후보로 추가.
# 2026-10-12: ready 115→116. F79-R(food payment-service 릴리스 1.4.0 이 외부 PG 결제 주소를 기본 주소와 경로를 구분자 없이 이어 만들어 운영 주소에서 'Bad authority' 로 모든 결제 502, 주문 전량 502, 후보) 추가.
# 2026-10-16: ready 116→117. F80-R(banking 워커 tb-w2 의 Oracle 데이터 파일 일상 백업이 zstd 2 GiB 장거리 창으로 노드 남은 메모리를 거의 다 써 kubelet 이 Oracle 을 축출하거나 커널이 자바 컨테이너를 OOM 으로 죽여 이체와 commerce 정산 502, 후보) 추가.
# 2026-10-30: ready 117→118. F69-P(commerce inventory-service 릴리스 1.3.0 의 재조정 배치가 재고 행을 공유 잠금으로 쥔 채 45일 원장 합계를 돌아 재고 예약과 checkout 이 주기적으로 막힘) 후보 추가.
[[ "$(jq '[.scenarios[] | select(.readiness=="ready")] | length' "$catalog")" -eq 118 ]]
[[ "$(jq '[.scenarios[] | select(.readiness=="parked")] | length' "$catalog")" -eq 20 ]]
[[ "$(jq '[.scenarios[] | select(.readiness=="cut")] | length' "$catalog")" -eq 4 ]]
[[ "$(jq '[.scenarios[] | select(.readiness=="blocked")] | length' "$catalog")" -eq 0 ]]
# 2026-10-08: draft 1 → 2. F30-R(food payment JSON 명명 규칙 설정 배포) 추가, 첫 시험 실행 대기.
# 2026-10-08: draft 2 → 1. F30-R 이 ready(calibration)로 올라갔다.
[[ "$(jq '[.scenarios[] | select(.readiness=="draft")] | length' "$catalog")" -eq 1 ]]
[[ "$(jq '[.scenarios[] | select(.readiness=="partial")] | length' "$catalog")" -eq 0 ]]
# 2026-07-28: 13 → 17. 스토리지 IO 3종(F02-H·F10-H·F10-P)과 F15-P를 고정 계약에서
# 캘리브레이션 사다리로 전환했다. 고정 rate_iops가 장치 능력의 16~21%뿐이라 피해를
# 낼 수 없었고, 무릎은 정적으로 알 수 없어 런타임이 찾아야 한다.
# 2026-08-06: 18 → 19. 9e23a9a 가 F25-H 를 320Mi 고정에서 실측 사다리로 돌리며
# load_mode 를 adaptive 로 바꿨는데 이 개수를 안 고쳤다. 게이트가 첫 실패에서 멈추는
# 탓에 위쪽 ready 개수(8d1ef6b)를 고치고 나서야 드러났다 — 같은 계열의 두 번째 누락.
# 2026-08-07: 19 → 20. F20-R(d1df8a5). rps 30 고정은 정체성을 위반하고 있었다 —
# 그 부하에서 order 는 느려진 게 아니라 30초 타임아웃 벽에 처박혔고 헬스체크마저 503 이었다.
# 쓸 수 있는 띠는 실측상 1 rps 부근이라(단일 풀스캔이 1 rps 에서 이미 p95 1826ms,
# 2~3 rps 에서 곧장 5xx) 사다리 1·2·3 으로 강등해 런타임이 그 좁은 띠를 찾게 한다.
# 2026-08-20: 20 → 6. 사다리 pin 14종(F03-H·F05-P·F05-R·F07-H·F09-H·F09-P·F10-H·F11-R·
# F12-H·F15-P·F15-R·F15-T1·F20-R·F25-H)을 evaluation 고정 계약으로 승격했다. pin 은
# 강도를 고정할 뿐 승인이 아니어서 첫 캠페인 케이스(F15-R 82e0537f)가 규격은 완벽한데
# case_label=calibration·evaluation_eligible=false 로 찍혔다. 남은 adaptive 는 F10-P(미pin)
# 와 비라이브 5종(F02-H·F03-P·F07-P·F09-R·F21-P).
# 2026-10-08: 6 → 7. F30-R 은 강도 사다리(snake-case-lenient, snake-case-strict)로 들어온다.
# 2026-10-08: 7 → 6. 수명주기 결정(새 후보는 설계 강도로 고정 등록, spec-scenario-lifecycle §2)으로
# F30-R 을 lenient 1단 고정 evaluation(approved-fixed-f30-r)으로 바꾸고 strict 단을 버렸다.
# 남은 adaptive 는 비라이브(parked) 6종뿐이다.
[[ "$(jq '[.scenarios[] | select(.load_mode=="adaptive")] | length' "$catalog")" -eq 6 ]]
# 2026-08-06: 42 → 41. adaptive 의 짝 — F25-H 가 fixed 에서 빠져나갔으므로 함께 움직인다.
# 2026-08-07: 41 → 40. adaptive 의 짝 — F20-R 이 fixed 에서 빠져나갔다.
# 2026-08-20: 40 → 54. adaptive 의 짝 — pin 14종이 fixed 로 들어왔다.
# 2026-10-08: 54 → 55. adaptive 의 짝 — F30-R 이 lenient 1단 고정 evaluation 으로 들어왔다.
# 2026-10-08: 55 → 56. F32-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f32-r)으로 들어왔다.
# 2026-10-08: 56 → 57. F33-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f33-r)으로 들어왔다.
# 2026-10-08: 57 → 58. F35-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f35-r)으로 들어왔다.
# 2026-10-08: 58 → 59. F36-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f36-r)으로 들어왔다.
# 2026-10-08: 59 → 60. F37-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f37-r)으로 들어왔다.
# 2026-10-08: 60 → 61. F38-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f38-r)으로 들어왔다.
# 2026-10-08: 61 → 62. F39-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f39-r)으로 들어왔다.
# 2026-10-09: 62 → 63. F40-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f40-r)으로 들어왔다.
# 2026-10-09: 63 → 64. F17-H 가 설계 강도 1단 고정 evaluation(approved-fixed-f17-h)으로 들어왔다.
# 2026-10-09: 64 → 65. F32-H 가 설계 강도 1단 고정 evaluation(approved-fixed-f32-h)으로 들어왔다.
# 2026-10-09: 65 → 66. F33-P 가 설계 강도 1단 고정 evaluation(approved-fixed-f33-p)으로 들어왔다.
# 2026-10-09: 66 → 67. F41-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f41-r)으로 들어왔다.
# 2026-10-09: 67 → 68. F42-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f42-r)으로 들어왔다.
# 2026-10-09: 68 → 69. F43-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f43-r)으로 들어왔다.
# 2026-10-09: 69 → 70. F44-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f44-r)으로 들어왔다.
# 2026-10-09: 70 → 71. F44-P 가 설계 강도 1단 고정 evaluation(approved-fixed-f44-p)으로 들어왔다.
# 2026-10-09: 71 → 72. F47-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f47-r)으로 들어왔다.
# 2026-10-09: 72 → 73. F48-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f48-r)으로 들어왔다.
# 2026-10-09: 73 → 74. F49-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f49-r)으로 들어왔다.
# 2026-10-09: 74 → 75. F33-H 가 설계 강도 1단 고정 evaluation(approved-fixed-f33-h)으로 들어왔다.
# 2026-10-09: 75 → 76. F49-H 가 설계 강도 1단 고정 evaluation(approved-fixed-f49-h)으로 들어왔다.
# 2026-10-09: 76 → 77. F50-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f50-r)으로 들어왔다.
# 2026-10-09: 77 → 78. F48-P 가 설계 강도 1단 고정 evaluation(approved-fixed-f48-p)으로 들어왔다.
# 2026-10-09: 78 → 79. F51-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f51-r)으로 들어왔다.
# 2026-10-10: 79 → 80. F43-P 가 설계 강도 1단 고정 evaluation(approved-fixed-f43-p)으로 들어왔다.
# 2026-10-10: 80 → 81. F40-H 가 설계 강도 1단 고정 evaluation(approved-fixed-f40-h)으로 들어왔다.
# 2026-10-10: 81 → 82. F52-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f52-r)으로 들어왔다.
# 2026-10-10: 82 → 83. F53-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f53-r)으로 들어왔다.
# 2026-10-10: 83 → 84. F53-P 가 설계 강도 1단 고정 evaluation(approved-fixed-f53-p)으로 들어왔다.
# 2026-10-10: 84 → 85. F42-P 가 설계 강도 1단 고정 evaluation(approved-fixed-f42-p)으로 들어왔다.
# 2026-10-10: 85 → 86. F35-H 가 설계 강도 1단 고정 evaluation(approved-fixed-f35-h)으로 들어왔다.
# 2026-10-10: 86 → 87. F32-P 가 설계 강도 1단 고정 evaluation(approved-fixed-f32-p)으로 들어왔다.
# 2026-10-10: 87 → 88. F54-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f54-r)으로 들어왔다.
# 2026-10-10: 88 → 89. F49-P 가 설계 강도 1단 고정 evaluation(approved-fixed-f49-p)으로 들어왔다.
# 2026-10-10: 89 → 90. F55-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f55-r)으로 들어왔다.
# 2026-10-10: 90 → 91. F56-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f56-r)으로 들어왔다.
# 2026-10-10: 91 → 92. F51-H 가 설계 강도 1단 고정 evaluation(approved-fixed-f51-h)으로 들어왔다.
# 2026-10-10: 92 → 93. F57-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f57-r)으로 들어왔다.
# 2026-10-10: 93 → 94. F23-P 가 설계 강도 1단 고정 evaluation(approved-fixed-f23-p)으로 들어왔다.
# 2026-10-10: 94 → 95. F58-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f58-r)으로 들어왔다.
# 2026-10-10: 95 → 96. F59-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f59-r)으로 들어왔다.
# 2026-10-10: 96 → 97. F59-P 가 설계 강도 1단 고정 evaluation(approved-fixed-f59-p)으로 들어왔다.
# 2026-10-10: 97 → 98. F36-H 가 설계 강도 1단 고정 evaluation(approved-fixed-f36-h)으로 들어왔다.
# 2026-10-10: 98 → 99. F41-H 가 설계 강도 1단 고정 evaluation(approved-fixed-f41-h)으로 들어왔다.
# 2026-10-10: 99 → 100. F60-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f60-r)으로 들어왔다.
# 2026-10-11: 100 → 101. F57-P 가 설계 강도 1단 고정 evaluation(approved-fixed-f57-p)으로 들어왔다.
# 2026-10-11: 101 → 102. F63-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f63-r)으로 들어왔다.
# 2026-10-11: 102 → 103. F64-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f64-r)으로 들어왔다.
# 2026-10-11: 103 → 104. F65-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f65-r)으로 들어왔다.
# 2026-10-11: 104 → 105. F66-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f66-r)으로 들어왔다.
# 2026-10-11: 105 → 106. F64-P 가 설계 강도 1단 고정 evaluation(approved-fixed-f64-p)으로 들어왔다.
# 2026-10-11: 106 → 107. F66-P 가 설계 강도 1단 고정 evaluation(approved-fixed-f66-p)으로 들어왔다.
# 2026-10-11: 107 → 108. F60-H 가 설계 강도 1단 고정 evaluation(approved-fixed-f60-h)으로 들어왔다.
# 2026-10-11: 108 → 109. F54-P 가 설계 강도 1단 고정 evaluation(approved-fixed-f54-p)으로 들어왔다.
# 2026-10-11: 109 → 110. F51-P 가 설계 강도 1단 고정 evaluation(approved-fixed-f51-p)으로 들어왔다.
# 2026-10-11: 110 → 111. F39-H 가 설계 강도 1단 고정 evaluation(approved-fixed-f39-h)으로 들어왔다.
# 2026-10-11: 111 → 112. F63-P 가 설계 강도 1단 고정 evaluation(approved-fixed-f63-p)으로 들어왔다.
# 2026-10-11: 112 → 113. F67-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f67-r)으로 들어왔다.
# 2026-10-11: 113 → 114. F37-H 가 설계 강도 1단 고정 evaluation(approved-fixed-f37-h)으로 들어왔다.
# 2026-10-11: 114 → 115. F53-H 가 설계 강도 1단 고정 evaluation(approved-fixed-f53-h)으로 들어왔다.
# 2026-10-11: 115 → 116. F67-H 가 설계 강도 1단 고정 evaluation(approved-fixed-f67-h)으로 들어왔다.
# 2026-10-11: 116 → 117. F60-P 가 설계 강도 1단 고정 evaluation(approved-fixed-f60-p)으로 들어왔다.
# 2026-10-11: 117 → 118. F35-P 가 설계 강도 1단 고정 evaluation(approved-fixed-f35-p)으로 들어왔다.
# 2026-10-11: 118 → 119. F16-P 가 설계 강도 1단 고정 evaluation(approved-fixed-f16-p)으로 들어왔다.
# 2026-10-12: 119 → 120. F68-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f68-r)으로 들어왔다.
# 2026-10-12: 120 → 121. F69-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f69-r)으로 들어왔다.
# 2026-10-12: 121 → 122. F36-P 가 설계 강도 1단 고정 evaluation(approved-fixed-f36-p)으로 들어왔다.
# 2026-10-12: 122 → 123. F65-P 가 설계 강도 1단 고정 evaluation(approved-fixed-f65-p)으로 들어왔다.
# 2026-10-12: 123 → 124. F70-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f70-r)으로 들어왔다.
# 2026-10-12: 124 → 125. F71-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f71-r)으로 들어왔다.
# 2026-10-12: 125 → 126. F72-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f72-r)으로 들어왔다.
# 2026-10-12: 126 → 127. F70-P 가 설계 강도 1단 고정 evaluation(approved-fixed-f70-p)으로 들어왔다.
# 2026-10-12: 127 → 128. F73-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f73-r)으로 들어왔다.
# 2026-10-12: 128 → 129. F74-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f74-r)으로 들어왔다.
# 2026-10-12: 129 → 130. F75-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f75-r)으로 들어왔다.
# 2026-10-12: 130 → 131. F76-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f76-r)으로 들어왔다.
# 2026-10-12: 131 → 132. F76-P 가 설계 강도 1단 고정 evaluation(approved-fixed-f76-p)으로 들어왔다.
# 2026-10-12: 132 → 133. F77-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f77-r)으로 들어왔다.
# 2026-10-12: 133 → 134. F71-H 가 설계 강도 1단 고정 evaluation(approved-fixed-f71-h)으로 들어왔다.
# 2026-10-12: 134 → 135. F79-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f79-r)으로 들어왔다.
# 2026-10-16: 135 → 136. F80-R 이 설계 강도 1단 고정 evaluation(approved-fixed-f80-r)으로 들어왔다.
# 2026-10-30: 136 → 137. F69-P 가 설계 강도 1단 고정 evaluation(approved-fixed-f69-p)으로 들어왔다.
[[ "$(jq '[.scenarios[] | select(.load_mode=="fixed")] | length' "$catalog")" -eq 137 ]]
[[ "$(jq '[.scenarios[] | select(.load_mode=="no-load")] | length' "$catalog")" -eq 0 ]]
# 2026-07-29: 알려진 profile 목록을 손으로 적어두던 것을 레지스트리에서 유도하도록
# 바꿨다. 손으로 적힌 목록은 profile을 신설할 때마다 조용히 낡고, 그 결과가 0f40dd7의
# 배포 정지였다 — 검사하는 쪽과 실제 계약이 다른 것을 보고 있으면 안 된다.
jq -e --argjson known_profiles "$(jq '[.profiles | keys[]]' "$script_dir/registry/profiles.json")" '
  all(.scenarios[];
    (.id | test("^F[0-9]{2}-(R|H|P|G|Q|S|T[1-4])$")) and
    (.slug | test("^[a-z0-9][a-z0-9-]+$")) and
    (.readiness | IN("ready", "partial", "blocked", "draft", "parked", "cut")) and
    (.load_mode | IN("adaptive", "fixed", "no-load")) and
    (.injection_location | type == "string" and length > 0) and
    (.profiles | type == "array" and length > 0 and all(.[]; IN($known_profiles[]))) and
    (if .readiness == "ready" then true else (.prerequisite | length > 0) end)
  )
' "$catalog" >/dev/null

python3 "$script_dir/generate-manifests.py" --check >/dev/null

while IFS= read -r row; do
  slug="$(jq -r '.slug' <<<"$row")"
  manifest="$manifest_dir/$slug.yaml"
  jq -e --argjson row "$row" '
    ["ssh", "kubectl", "api-via-kubectl", "local-orchestrator", "unresolved"] as $known_transports |
    ["catalog-integrity", "canonical-kubeconfig", "global-dirty-lease",
     "baseline-clean-window", "target-health", "profile-prerequisites",
     "transport-ssh-access", "transport-kubernetes-rbac", "transport-api-contract",
     "transport-local-runner-state", "transport-resolution-blocker",
     "db-session-tag-clean", "db-inverse-ddl-ready", "mock-restore-contract",
     "baseline-loadgen-active", "east-west-job-contract",
     "kubernetes-original-spec-snapshot", "kubernetes-recovery-capacity",
     "kubernetes-original-resource-snapshot", "kubernetes-original-probe-snapshot",
     "kubernetes-original-env-snapshot",
     "kafka-drain-capacity", "host-placement-and-oob-recovery",
     "cache-warmup-contract", "network-oob-recovery", "rollback-artifact",
     "wpm-probe-contract", "business-invariant-probe", "subinjection-timeline",
     "app-control-flag-armed"] as $known_preflights |
    ($row.profiles | map("injector-profiles/" + .)) as $profile_refs |
    .id == $row.id and
    .slug == $row.slug and
    .readiness == $row.readiness and
    .injection.location == $row.injection_location and
    (.injection.matrix_location_transport | type == "string" and length > 0) and
    (.injection.transport | IN($known_transports[])) and
    .injection.profile_refs == $profile_refs and
    (.execution.preflight_ids | length >= 8 and all(.[]; IN($known_preflights[]))) and
    (if .execution.controller.live_enabled
     then .execution.controller.dispatcher_mode == "trusted" and
          .execution.controller.binding.primary_ref == $row.profiles[0] and
          .execution.controller.binding.companion_refs == $row.profiles[1:] and
          (.execution.controller.runtime.mode | IN("calibration", "evaluation")) and
          (if $row.load_mode == "adaptive"
           then .execution.controller.runtime.profile.kind == "adaptive_ladder"
           else .execution.controller.runtime.profile.kind == "fixed" end)
     else .execution.controller.dispatcher_mode == "dry-run" and
          (.execution.controller.decision_mode | IN("calibration", "evaluation", "none")) and
          .execution.controller.profile.kind == $row.load_mode end) and
    all(.actions[]; .mode == "dry-run" and .mutation == false) and
    .actions.run.requires_preflight == true and
    .actions.cleanup.required == true and
    .actions.cleanup.order == "reverse" and
    .actions.cleanup.recovery_gate == true and
    .prerequisite_gate.live_allowed == .execution.controller.live_enabled and
    (if $row.readiness == "ready"
     then .prerequisite_gate.state == "satisfied" and .prerequisite_gate.required == false
     else .prerequisite_gate.state == "unresolved" and .prerequisite_gate.required == true
     end) and
    .capture_policy.policy_ref == "focused-window-v1" and
    .capture_policy.time_basis == "UTC" and
    .capture_policy.query_window == "[t1-10m,t2+20m]" and
    .capture_policy.export_not_before == "t2+20m" and
    .capture_policy.create_golden_anomaly == false
  ' "$manifest" >/dev/null
done < <(jq -c '.scenarios[]' "$catalog")

# Parked and cut scenarios keep their design assets (manifest, executors) but
# must never compile to an executable plan. compile-plan gates live_allowed on
# readiness == "ready", so this is the guard that keeps a withdrawn scenario
# from re-entering the capture queue by accident. cut is covered by the same
# loop deliberately: a terminal decision needs the same mechanical proof as a
# temporary one, or the enum value becomes documentation instead of a gate.
while IFS= read -r slug; do
  [[ "$(python3 "$script_dir/compile-plan.py" --scenario "$slug" | jq -r '.live_allowed')" == "false" ]]
done < <(jq -r '.scenarios[] | select(.readiness=="parked" or .readiness=="cut") | .slug' "$catalog")

ready_live_false=0
ready_live_true=0
# bin/ only covers the pre-profile-executor generation of scenarios: 46 of the
# 60 catalog slugs have a script. The newer ones are driven through
# profile-control/trusted_dispatcher instead, so the round-trip below runs over
# the intersection rather than the whole catalog.
while IFS= read -r slug; do
  [[ -f "$script_dir/bin/$slug.sh" ]] || continue
  expected_plan="$(python3 "$script_dir/compile-plan.py" --scenario "$slug")"
  shared_contract=""
  for action in plan run cleanup; do
    output="$("$script_dir/bin/$slug.sh" "--$action")"
    jq -e --arg slug "$slug" --arg action "$action" --argjson expected_plan "$expected_plan" \
      '.side_effects == false and .action == $action and .scenario.slug == $slug and
       .manifest.slug == $slug and .selected_action.mode == "dry-run" and
       .selected_action.mutation == false and .cleanup.required == true and
       .selected_action == .manifest.actions[$action] and
       .normalized_plan == $expected_plan and
       .digests == {
         scenario: $expected_plan.scenario_digest,
         manifest: $expected_plan.manifest_digest,
         registry: $expected_plan.registry_digest,
         plan: $expected_plan.plan_digest
       } and
       .profile_instances == $expected_plan.profile_instances and
       .profile_executor_hashes == ($expected_plan.profile_instances | map({profile_id, executor, executor_sha256})) and
       .observation_query_ids == $expected_plan.observation_query_ids and
       .location_ids == ($expected_plan.profile_instances | map(.location_id)) and
       all(.profile_executor_hashes[]; .executor_sha256 | test("^[0-9a-f]{64}$")) and
       all(.profile_instances[];
         if .location.transport == "kubectl"
         then .location.kubeconfig == "/root/tb-kubeconfig"
         else true end) and
       .normalized_plan.live_allowed == $expected_plan.live_allowed and
       .prerequisite_gate.live_allowed == $expected_plan.live_allowed and
       .capture.create_golden_anomaly == false' \
      <<<"$output" >/dev/null

    current_shared="$(jq -Sc '{normalized_plan,digests,profile_executor_hashes,observation_query_ids,location_ids}' <<<"$output")"
    if [[ -z "$shared_contract" ]]; then
      shared_contract="$current_shared"
    else
      [[ "$current_shared" == "$shared_contract" ]]
    fi
    while IFS= read -r executor; do
      [[ -f "$script_dir/$executor" ]]
    done < <(jq -r '.profile_executor_hashes[].executor' <<<"$output")
  done
  readiness="$(jq -r '.scenario.readiness' <<<"$expected_plan")"
  live_allowed="$(jq -r '.live_allowed' <<<"$expected_plan")"
  if [[ "$readiness" == "ready" && "$live_allowed" == "false" ]]; then
    ready_live_false=$((ready_live_false + 1))
  elif [[ "$readiness" == "ready" && "$live_allowed" == "true" ]]; then
    ready_live_true=$((ready_live_true + 1))
  fi
done < <(jq -r '.scenarios[].slug' "$catalog")
# 21 of the 31 ready scenarios have a bin/ script; every one of them must
# compile to live_allowed == true. The remaining 10 are covered by the
# catalog-level checks above and by tests/test_registry_contracts.py.
# 2026-07-28: 21→22. F03-H 복귀분이 bin/ 스크립트를 가진 ready 집합에 더해졌다.
# 2026-07-28: 23→27. 스토리지 포화 3종과 F15-P가 더해졌고 넷 다 bin/ 스크립트를 갖고 있다.
# 2026-07-28: 27→29. F09-H·F09-P 복귀분.
# 2026-07-28: 29→30. F04-H 승격분(bin/ 스크립트를 이미 갖고 있었다).
# 2026-07-28: 30→29. F07-P 강등분(F20-P는 bin/ 스크립트가 없어 이 카운트에
# 애초에 들어 있지 않았다).
# 2026-07-29: 29→31. F15-H·F15-T2 승격분(둘 다 bin/ 스크립트를 갖고 있다).
# 2026-07-29: 31→32. F14-P 승격분(bin/ 스크립트를 이미 갖고 있었다).
# 2026-08-07: 32→31. c887e20 의 파킹 3종 중 bin/ 스크립트를 가진 것은 F02-H 하나뿐이라
# (F21-P·F21-Q 는 애초에 이 집합 밖) 하나만 빠진다. 8d1ef6b 가 위쪽 ready 개수를 고치자
# 드러난 같은 계열의 세 번째 누락이다 — 게이트가 첫 실패에서 멈추므로 낡은 핀은
# 한 번에 하나씩만 보인다.
# 2026-08-09: 31→29. F09-R·F03-P 파킹분. 둘 다 bin/ 스크립트를 갖고 있어 함께 빠진다.
# 2026-10-08: 29→26. F10-P, F15-R, F15-T2 폐기분(셋 다 bin/ 스크립트가 있다. F20-Q 는 없어 애초에 이 집합 밖).
[[ $((ready_live_false + ready_live_true)) -eq 26 ]]
[[ "$ready_live_true" -eq 26 ]]
[[ "$ready_live_false" -eq 0 ]]

if "$script_dir/bin/f15-t2-pg-lock-then-food-429.sh" --live 2>/dev/null; then
  echo "blocked scenario unexpectedly passed live fail-closed gate" >&2
  exit 1
fi
if RUNNER_SCRIPT=/bin/true SCENARIO_CATALOG=/tmp/forged.json \
  "$script_dir/bin/f07-h-north-south-surge.sh" --live 2>/dev/null; then
  echo "ready scenario unexpectedly accepted caller-controlled live inputs" >&2
  exit 1
fi

echo "[PASS] 64 scenario entrypoints"
