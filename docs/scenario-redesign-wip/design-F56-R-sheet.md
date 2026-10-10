---
title: F56-R 설계 시트 (해지 계좌 정리가 잘못된 id 목록으로 commerce 정산 계좌를 지워 checkout 이 실패)
status: Draft
owner: project
last_reviewed: 2026-10-10
tags:
  - scenario
  - design
  - oracle
  - operation
  - cross-domain
summary: banking 운영자의 해지 계좌 정리 스크립트가 해지 계좌 대신 commerce 가맹 계약의 살아 있는 정산 계좌 두 개(commerce-settlement, commerce-merchant)의 id 를 받아 banking Oracle BANKING.ACCOUNTS 에서 지운다. commerce 의 모든 checkout 이 바로 그 두 계좌 사이의 이체로 정산하므로 transfer 가 정산마다 즉시 400 'Account not found: commerce-merchant' 로 답하고 checkout 이 502 로 실패한다. banking 자신의 손님 거래는 정상이다. Atlassian 2022-04-05(잘못된 id 를 받은 정리 스크립트가 살아 있는 고객 사이트를 지움)의 재구성.
---

# F56-R 설계 시트

## 1. 요약

commerce checkout 은 결제마다 core-banking 이체로 정산한다. payment-service 가 외부 결제 승인 뒤 `testbed-transfer.rca-testbed-banking.svc.cluster.local:8082` 로 `POST /api/transfers`(fromAccount `commerce-settlement`, toAccount `commerce-merchant`)를 동기로 보낸다. 두 계좌는 commerce 가맹 계약을 위해 banking Oracle `BANKING.ACCOUNTS` 에 시딩된 고정 계좌이고 payment 코드에 박혀 있다(`BankingTransferClient.java:25-29`). 평시 정산 이체는 분당 약 29건(119 로그, 설계 전날 41,734건)이다.

banking 운영자가 해지 계좌 정리를 돌린다. 꼼꼼한 스크립트처럼 지울 행을 보관 표 `ACCOUNTS_PURGE_ARCHIVE` 에 먼저 옮기고 한 트랜잭션에서 지우지만, 받은 id 목록에 해지 계좌 대신 commerce 가맹 계약의 정산 계좌 두 개가 들어 있다. 커밋 순간부터 transfer 의 `execute` 가 두 계좌를 id 순서로 `FOR UPDATE` 로 찾다가 먼저 찾는 `commerce-merchant` 가 없어 즉시 400 `Account not found: commerce-merchant` 를 돌려준다(로그 없이). payment 가 이 4xx 를 502 `core-banking transfer call failed: 400 Bad Request: ...` 로 바꾸고 결제를 되돌리며, order 는 재시도와 서킷 끝에 ERROR `Checkout payment failed, releasing reserved stock: ...`(banking 의 거절 문장이 그대로 실림)을 남기고 checkout 에 502 로 답한다. 매시 commerce 정산 배치도 실패한다.

나머지 999개 계좌, 이체, 원장, outbox 표, Oracle, 파드, 설정은 그대로다. banking 자신의 손님(ACC-*)은 잔액 조회, 내역, 이체를 계속하고, commerce 기준선의 ACC- 계좌끼리 직행 이체도 계속 끝난다.

비유: 은행 창구 직원이 해지된 통장을 정리하라는 목록을 받았는데, 목록에 거래처 쇼핑몰의 정산 통장 번호가 들어 있었다. 통장을 정리 상자에 옮겨 담고 장부에서 지운다. 은행의 다른 손님은 아무 일 없지만, 그 쇼핑몰은 물건을 팔 때마다 "그런 통장이 없다" 는 답을 받아 결제를 마무리하지 못한다.

## 2. 원본 사례

- 기업: Atlassian
- 날짜: 2022-04-05 (07:38 UTC 스크립트 실행, 08:01 UTC 끝, 4월 18일 전부 복구)
- 링크: [Post-Incident Review on the Atlassian April 2022 outage (공식)](https://www.atlassian.com/engineering/post-incident-review-april-2022-outage) (자료 문서 `ref-real-world-incidents.md` M22 행, 이번에 공식 사후 검토에서 세부 보강)
- 요약(출처가 말한 것만): Jira Service Management 로 기능이 옮겨 간 레거시 앱 "Insight – Asset Management" 를 아직 설치된 고객 사이트에서 지우려던 스크립트에 앱 인스턴스 id 대신 그 앱이 있는 클라우드 사이트 전체의 id 가 넘어갔다("the team provided the IDs of the entire cloud site where the apps were to be deleted"). 동료 검토는 엔드포인트와 호출 방식만 봤고, API 는 입력이 맞다고 가정해 경고 없이 사이트 삭제를 실행했다. 앞선 30개 사이트 운영 실행은 올바른 앱 id 로 성공했었다. 775개 고객의 883개 사이트가 즉시 지워져 그 고객들만 Jira, Confluence 등 접속을 잃었다. 첫 고객 지원 요청 07:46 UTC, 삭제가 표준 작업 흐름으로 실행되어 내부 감시는 잡지 못했고, 08:53 지원 요청과 스크립트 실행을 연결했다. 백업(삭제 5분 전 시점)에서 묶음별로 복구했다.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 운영 정리 스크립트(레거시 앱 삭제) 실행 | banking 운영자의 해지 계좌 정리(보관 뒤 삭제) 실행 |
| 원인이 된 결함 | 스크립트에 앱 id 대신 살아 있는 고객 사이트 전체의 id 가 넘어감, API 가 입력을 그대로 믿고 즉시 삭제 | 정리 목록에 해지 계좌 대신 commerce 가맹 계약의 살아 있는 정산 계좌 두 개(commerce-settlement, commerce-merchant)의 id 가 들어가, 정리가 그 두 행을 `BANKING.ACCOUNTS` 에서 지움(보관 표에 먼저 옮김) |
| 전파 경로 | 사이트가 없어 그 고객의 모든 제품 요청이 실패 | 정산 이체마다 transfer 가 계좌를 못 찾아 400 → payment 502 와 롤백 → order 재시도, 재고 해제, 결제 서킷 → checkout 502. 매시 정산 배치 실패 |
| 사용자 증상 | 지워진 사이트의 고객만 접속 불가, 다른 고객은 무영향 | 그 가맹 고객사(commerce)의 구매가 100% 실패, banking 의 다른 손님은 무영향 |
| 탐지된 경로 | 고객 지원 요청(내부 감시는 표준 작업 흐름이라 못 잡음) | commerce order, payment 오류율과 새 ERROR 로그('Checkout payment failed ... Account not found: commerce-merchant') 급증 → 119 이상 탐지 → 인시던트. banking 쪽 지표는 조용하다(4xx, 로그 없음) |
| 완화와 복구 | 백업에서 지워진 사이트 복구 | cleanup 이 보관 표의 두 행을 그대로 되넣고 보관 표를 지움(즉시 회복). 원본의 며칠짜리 백업 복원은 재현하지 않는다 |

기전은 원본과 같다: "운영 정리 작업이 올바른 동작을 하되 잘못된 대상 id 를 받아 살아 있는 고객 개체를 지움 → 그 고객에 속한 요청만 모두 실패, 나머지는 정상 → 지운 것을 되살리면 회복". 우리 스택에 맞춘 것은 대상(클라우드 사이트 → 은행에 있는 고객사의 정산 계좌 행)과 규모(두 행)다. 원본처럼 감시를 거치지 않는 정상 작업 흐름(보관 뒤 삭제, 한 트랜잭션)이고, 지운 쪽(banking)은 조용하고 고객 쪽(commerce)에서 터진다.

관계와 id: 새 사례군(F56)의 첫 시나리오라 R 이다. 같은 'banking 이 commerce 정산을 거절해 checkout 502' 증상의 F40-H(transfer 릴리스의 금액 검증), F44-P(tb-w2 방화벽)와 원인이 다른 짝이다(시트 §8).

## 3. 숫자 근거 (2026-10-10, `scenario-stats.py`, 정식 + 후보 합계 71, 이 후보 전)

| 축 | 값 | 판단 |
|---|---|---|
| 서비스 | 쇼핑몰 26, 은행 22, 음식배달 23 | 은행이 가장 적다 → 은행(22→23) |
| 묶음 | J 9(12.7%), G 8, P 8(각 11.3%), A, B, D, L 7, C 6, F 3, E, H, K 2, I, M, O 1, N 0 | P(운영 작업의 대상 착오) 8→9(9/72, 12.5%) |
| 정답 위치 | DB 테이블(은행 계좌) 3 | 3→4(5.6%). F01-P 와 같은 표지만 잠금이 아니라 행 부재다 |
| 결제 경로 합계 | 12(16.9%) | 정답이 banking 계좌 표라 그대로(12/72, 16.7%) |
| 어느 축이든 | 최대는 J 9/72(12.5%), 주문 서비스 7/72(9.7%) | 20% 미만 |

묶음 해석: 의도한 작업(해지 계좌 정리, 보관 뒤 삭제)의 내용은 맞았고 적용 대상(id 목록)이 틀렸다(P). 계좌 표의 스키마와 제약, 코드, 이미지, 설정, 파드는 그대로라 L, J, G 가 아니고, 잠금(D), 자원(A), 질의 비용(F), 자격 증명(O)도 그대로다.

### 3단계 후보 목록 (실제 기전 × 부품, 숫자 순으로 줄 세움, 버린 이유 포함)

장부와 기존 목록은 피할 것 확인에만 썼다. 부품 여섯(banking Oracle 표, banking Oracle 인스턴스, food MySQL 인스턴스, food restaurant 캐시 표, banking transfer 와 이체 표, 외부 결제 mock)에 걸쳐 실제 사례로 후보를 뽑았고, 이번에 웹에서 새 원본(Rippling, Atlassian 세부, GitHub 2025-08, GitHub 2026-03-03, CircleCI 2022-03-01, GoCardless 2018-04-06, Cronofy 2023-08-21, Templafy 2025-12-17)을 찾았다.

| 순위 | 후보 | 원본 사례 | 부품(정답 위치 수), 묶음 | 결과 |
|---|---|---|---|---|
| 1 | 운영 정리 스크립트가 잘못된 id 목록으로 commerce 정산 계좌 두 행을 지움 | Atlassian 2022-04-05 공식 사후 검토 | banking Oracle ACCOUNTS, DB 테이블(은행 계좌) 3, P 8, 은행 22 | **채택(F56-R)**. 원본이 쓰인 적 없고(rejected 의 FROZEN 행은 같은 원본으로 '삭제' 대신 '동결'을 써서 commerce 정산이 동결 검사를 우회해 막혔다. 이번은 원본과 같은 삭제라 우회가 없다), 실패가 즉시 나는 400 이라 강도 축이 없다 |
| 2 | 디버깅 중 켠 '모든 질의 프로파일러'가 운영 DB 쓰기 용량을 잠식(MySQL general_log TABLE, Oracle SQL trace) | Rippling 2025-08-04 공식 상태 페이지 사후 보고 | food MySQL / banking Oracle 인스턴스(2), G+A | 버림(원칙 9, 4): 원본 피해는 프로파일러의 쓰기가 IOPS 를 다 먹은 것인데 general_log, 추적 파일은 버퍼 쓰기라 테스트베드 부하(food MySQL 최대 qps 약 1.2만)에서 쓰기 용량 포화 계산이 서지 않는다. SET GLOBAL, ALTER SYSTEM 은 119 에 흔적이 없다 |
| 3 | 캐시 쓰기를 줄이려던 릴리스의 버그가 모든 캐시를 만료, 재계산, 재기록 | GitHub 2026-03-03 월간 가용성 보고 | food restaurant 인기 메뉴 캐시 표(가게 서비스 2), J+F | 버림(카탈로그 §1): 테스트베드의 캐시 표는 menu_popularity_summary 뿐이고 재구성이 F42-P(같은 표를 우회해 요청마다 실시간 집계)와 같은 꼴이 된다. 다른 캐시는 commerce cart Redis(쇼핑몰 최다, 캐시 정답 1)뿐 |
| 4 | 운영 마이그레이션이 ORM 이 아직 참조하는 열을 지움 | GitHub 2025-08-05, 2025-08-27 월간 가용성 보고 | banking TRANSFERS, L | 버림(지휘 세션 지시): '스키마-코드 순서' 꼴은 사람 결정 전까지 내지 않는다 |
| 5 | 공급자가 엔드포인트를 옮기고 옛 경로를 404 로 브라운아웃, 소비자 실패 | CircleCI 2022-03-01 공식 사후 보고 | 외부 결제 mock(외부 결제 의존 6), C+L | 뒤로 미룸: 숫자상 가능(결제 경로 13/72, 18%)하나 정답 위치가 이미 가장 많은 축(외부 결제 의존 6)이라 1번보다 뒤다 |
| 6 | 감사 트리거의 기록 표 정수 id 가 최대값에 닿아 원래 쓰기까지 실패 | GoCardless 2018-04-06 공식 블로그 | banking Oracle 표, D/H | 버림(원칙 1): 테스트베드 id 가 모두 NUMBER, BIGINT 라 최대값 근처 시퀀스를 인위로 만들어야 한다(rejected 의 Basecamp, Oracle MAXVALUE 행과 같은 벽) |
| 7 | 요청 기록(journal) 변경 릴리스의 버그로 생성, 삭제 API 실패 | Cronofy 2023-08-21 공식 상태 페이지 | banking transfer(은행 이체 서비스 6), J | 버림(원칙 1): 원본이 버그의 기전을 밝히지 않아("an internal component being overwhelmed") 결함 꼴을 정할 수 없다 |
| 8 | 질의 최적화 기능 플래그를 켠 뒤 SQL CPU 상승 | Templafy 2025-12-17 상태 페이지 | banking Oracle 인스턴스(2), G | 버림(원칙 1): 원본이 '잠재적 기여 요인' 이라고만 하고 원인으로 확정하지 않는다 |
| 9 | 설정 변경이 작업자의 접근 권한을 지움 | GitHub 2025-04-23 월간 가용성 보고 | food DB 계정(2), O | 버림(원칙 1): 원본이 무엇에 대한 접근인지 밝히지 않아 대응표를 채울 수 없고, food 표 단위 권한 회수는 rejected 에 원칙 1 로 막혀 있다 |

rejected 기록, 장부 §4-1, §5, 이번 실행에서 막힌 목록에 같은 원본(Atlassian 2022-04-05)과 같은 주입(Oracle 행 삭제)의 짝이 없다. rejected 의 '운영 명령에 잘못된 id 를 넘겨 정산/고객 계좌를 FROZEN 으로' 는 같은 원본이지만 주입(상태 열 갱신)과 막힌 사유(commerce 정산은 FROZEN 검사가 없는 transfer 직행이라 통과, 고객 계좌는 api 400 단일 신호)가 다르다. 이번에는 행이 없어 transfer 가 정산을 거절하고, commerce 쪽 증상은 502 다.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 정산 호출: `commerce/payment-service/src/main/java/com/commerce/payment/client/BankingTransferClient.java:25-29` 고정 계좌 `commerce-settlement`, `commerce-merchant`. `:37-59` `transfer` 가 재시도, 서킷 없이 POST, `RestClientException`(4xx 의 `HttpClientErrorException` 포함)을 502 `core-banking transfer call failed: ` + 메시지로.
2. 결제: `commerce/payment-service/.../service/PaymentService.java:53-90` `processPayment`(@Transactional) — 외부 결제 승인 뒤 `bankingTransferClient.transfer`(:77), 실패하면 결제 행 롤백.
3. 주문: `commerce/order-service/.../service/OrderService.java:189-199` — 결제 실패 시 ERROR `Checkout payment failed, releasing reserved stock: userId={}, {}`(:193, 메시지에 payment 응답 본문 전체), 재고 해제, 502. paymentClient 재시도 2회, 서킷(10회 중 50%, 5초 열림).
4. 이체: `core-banking/transfer-service/.../service/TransferService.java:53-72` `execute` — 두 계좌를 작은 id 먼저 `findByIdForUpdate`(:69, :71), 없으면 `ServiceException(400, "Account not found: " + id)`. 'commerce-merchant' < 'commerce-settlement' 라 문장은 늘 `commerce-merchant`. `AccountRepository.java:18-20` PESSIMISTIC_WRITE.
5. 오류 처리: `transfer-service/.../config/GlobalExceptionHandler.java:12-20` 은 ServiceException 을 로그 없이 본문으로 돌려준다. 그래서 banking 쪽 로그에는 거절이 남지 않고 `Transfer COMPLETED`(:88) 만 끊긴다.
6. 영향 없는 경로: banking 손님 거래(nginx → api → account → transfer, ACC-*), commerce 기준선 직행 이체(`commerce/loadgen/script.js:86-87` `BANK_ACCOUNTS` 가 ACC- 뿐, `:235-248` `crossDomainJourney`), 매일 0시 이자 배치(ACTIVE 계좌만 돈다, 지워진 두 행은 건너뜀).
7. 인프라: rca-testbed-banking StatefulSet testbed-oracle(파드 testbed-oracle-0, PDB FREEPDB1, 스키마 BANKING). 109 읽기 전용 조회(2026-10-10): `BANKING.ACCOUNTS` 1,001행, 외래 키 참조 없음, `commerce-merchant` 잔액 약 3,270억 ACTIVE, `commerce-settlement` 약 6,894억 ACTIVE. 시드: `core-banking/db/init.sql:99-123`.

### 로컬 실측 (Oracle Free 23-slim, 109 와 같은 이미지 계열, 2026-10-10)

- 저장소 `core-banking/db/init.sql` 로 만든 BANKING 스키마 + 저장소 transfer-service 1.0.0 jar(이 커밋 기준 main 소스 그대로 빌드).
- 주입 전: 정산 꼴 이체(commerce-settlement → commerce-merchant, 25920.00) 200 COMPLETED, 손님 이체(ACC-1001 → ACC-1002) 200.
- 실행기 원격 스크립트 run 뒤: 정산 꼴 3/3 `400 {"status":400,"error":"Bad Request","message":"Account not found: commerce-merchant"}`(각 수십 ms), 손님 이체 200 COMPLETED. transfer 로그에는 거절이 남지 않고 손님 이체의 `Transfer COMPLETED` 만 이어진다.
- cleanup 뒤: 정산 꼴 200, 손님 이체 200. 행 수 1,001 → 999 → 1,001, 잔액과 생성 시각 그대로.
- 실행기 원격 스크립트(kubectl 을 docker exec 로 바꾼 시험 껍데기): preflight 0, run 0, 고장 중 recovery 1, 고장 중 preflight 1, cleanup 0(두 번째도 0, 멱등), recovery 0, preflight 0. 보관 표만 만들어진 반쯤 실패한 run 상태에서 preflight 1, cleanup 0(아무것도 되넣지 않고 보관 표만 지움), recovery 0.
- 109 에서 같은 원격 스크립트의 preflight(읽기 전용 조회만)를 계약 그대로 돌려 0: 두 id 있음, 보관 표 없음.

## 5. 원인 규정

| 칸 | 값 | 근거 |
|---|---|---|
| `root_cause.target_id` | `banking-oracle:BANKING.accounts` | 결함을 가진 곳은 고객사 정산 계좌 행이 사라진 계좌 표다. commerce payment 의 정산 요청과 transfer 의 처리 로직은 늘 하던 그대로라 정당하다(원칙 6). 표기는 F01-P 와 같은 '인스턴스:스키마.테이블' |
| `trigger_target_id` | null | 근본과 계기가 같은 곳(이 표에 대한 잘못된 대상의 정리 작업) |
| `scoring.partial` | banking-transfer, core-banking-transfer, banking-oracle, commerce-payment | 거절을 낸 서비스(transfer), 인스턴스까지만 짚은 답(Oracle), 도메인 밖 증상만 본 답(commerce-payment) |

원본 사후 검토도 "잘못된 id 를 받은 스크립트가 살아 있는 사이트를 지웠다" 를 원인으로 들었다. 같은 층위(사라진 고객 개체가 든 표)로 지목한다. 누가 왜 그 목록을 만들었는지는 요구하지 않는다(원칙 5). 관제 데이터로 "banking 에서 commerce 정산 계좌가 없어졌다(계좌 표)" 는 오류 문장에서 바로 나온다.

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 | 기대 |
|---|---|---|
| 감지 | commerce-order, payment 의 오류율(APM 표본), 새 ERROR 로그('Checkout payment failed ... Account not found: commerce-merchant', 서킷 열림, payment Servlet 502) 급증 | 이상 탐지 이벤트와 인시던트. 같은 경로(commerce checkout 502 가 banking 정산 실패에서 옴)의 F17-R 은 정식(정상 녹화 있음)이다 |
| 피해 판정 | 러너: 동반 부하 checkout 5xx 비율 ≥ 0.5, 2xx 비율 < 0.3, 3틱 | 평시 0 근처, 고장 중 약 1.0 |
| RCA | commerce 오류 문장이 banking 의 '계좌 없음' 거절과 그 계좌 id 를 직접 실어 나르고, banking 정산 완료 로그가 그 분에 끊기며, 다른 변경과 banking 쪽 오류가 없다 | §7, §8 |

## 7. 관측 근거 표 (119 실조회, 평시)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 핵심 증거는 전수 수집되는 로그다.

| 증거 | 표, 칸 | 조회 | 결과 |
|---|---|---|---|
| 근본: 계좌가 없다는 거절 문장 | CH `lucida_logs_local`(service_name, body) | `count() WHERE (service_name LIKE 'commerce%' OR service_name LIKE 'core-banking%') AND body LIKE '%Account not found%'`, 30일 | 0건. 주입 때 commerce-order 에 처음 나타난다 |
| 전파: commerce 오류 본문이 banking 응답 본문을 싣는다 | 같은 표, commerce-order | `body LIKE 'Checkout payment failed%core-banking transfer call failed%'` 최근 2건, 30일 | 다른 시나리오 시험 때만 있고 본문 전체가 실린다: 'Checkout payment failed, releasing reserved stock: userId=17, Payment service unavailable: Payment service error: 502 : "{...\"message\":\"core-banking transfer call failed: I/O error ... Connection refused\"...}"'(2026-10-09 07:12, F35-R 시험). 주입 때는 같은 자리에 '400 Bad Request: ...\"Account not found: commerce-merchant\"' 가 실린다(로컬 실측의 transfer 응답 본문) |
| 계기와 전파: 정산 완료 흐름 | 같은 표, core-banking-transfer INFO | `countIf(body LIKE 'Transfer COMPLETED%from=commerce-settlement to=commerce-merchant%')`, 1일, 시간대별 6시간 | 1일 41,734건(분당 약 29), 손님 이체 31,425건. 시간당 1,906~2,895건. 주입 분에 정산 줄만 끊기고 손님 줄은 이어져야 한다 |
| 전파: transfer 정산 응답 400 | CH `otel_traces_local`(service_name, span_kind, span_name, span_attributes['http.response.status_code']) | 7일 core-banking-transfer SERVER `POST /api/transfers` 집계 | 200 24,064건, 500 6건, 400 0건. 주입 뒤 정산 표본이 400 이고 그 아래 `AccountRepository.findByIdForUpdate` INTERNAL 스팬(1일 14,892건)이 보인다 |
| 증상 | 같은 표, commerce-order | `body LIKE 'Checkout payment failed%'` 날짜별 30일 | 2026-10-09 821건(다른 시나리오 시험 구간), 그 밖 0 |
| 배제: 롤아웃, 재시작 | CH `kcm_events_local`(namespace, object_name, reason) | 7일 rca-testbed-banking, rca-testbed-commerce | 수집됨(F35-H, F40-H 시트 조회). 주입 구간에는 없어야 한다 |
| 배제: 잠금(F01-P) | VM `dpm.oracle.session.blocked_session` | max_over_time 7d | 최대 2(평시 순간값, F35-H 시트). 이 주입은 두 행을 수십 ms 잠글 뿐이다 |
| 배제: Oracle 부하 | VM `dpm.oracle.session.active_session` | avg_over_time 1d | 약 0.19(F35-H 시트) |

계기의 흔적: DELETE 문장 자체는 119 에 남지 않는다(DPM Top SQL 은 sql id 와 수치만 남기고 문장이 없다, F35-H 시트 §7 조회). 계기는 행의 소멸로 드러난다: 바로 전 분까지 정상 완료되던 두 계좌의 이체가 그 분부터 '계좌가 없다' 는 문장으로 거절되고, 그 문장이 계좌 id 를 직접 이름으로 가리키며, 같은 시각에 롤아웃, 재시작, 설정 변경, banking 쪽 오류가 없다. 인공 지연은 없다. 원본도 삭제가 표준 작업 흐름으로 실행되어 내부 감시가 잡지 못했고 고객 쪽 증상으로 알았다.

원칙 8 참고: 실행기는 기존 Oracle 실행기(F14-P, F35-R, F35-H, F54-R)처럼 파드 안 SYSDBA sqlplus 로 접속해, DPM 세션 표본에 SYS 세션이 잡힐 수 있다. 보관 표 이름(`ACCOUNTS_PURGE_ARCHIVE`)과 모든 문장에 시나리오 id 가 없다. 보관 뒤 삭제는 현실의 정리 스크립트 모양이다.

## 8. 감별

- must_support: commerce-order ERROR 'Checkout payment failed ... core-banking transfer call failed: 400 Bad Request: ...\"Account not found: commerce-merchant\"', checkout 502, transfer 의 정산 `Transfer COMPLETED` 소멸과 손님 이체 지속, transfer 정산 스팬 400, transfer, Oracle Ready, KCM 변경 없음, Oracle 세션과 차단 세션 평시.
- must_rule_out: F44-P(방화벽, Connect timed out), F17-R, F17-H, F50-R, F52-R(transfer 부재, Connection refused, banking 자신의 이체도 실패), F40-H(transfer 2.3.0 롤아웃, 금액 검증 문장), F01-P(정산 계좌 행 잠금, 대기와 시간 초과), F35-R, F35-H, F42-R, F25-H(banking DB 장애, banking 자신의 요청 실패), commerce 자체 장애(F05-R, F05-H, F25-H, F01-H, F06-R).
- contrast_with: F40-H, F44-P, F01-P, F35-H, F51-R(정답지 `related_scenarios`).

가르는 관측 근거 한 줄: commerce 의 실패가 banking transfer 가 응답한 즉시 400 이고 그 문장이 계좌 id 하나가 없다고 말하며, 바로 전 분까지 같은 두 계좌로 이체가 완료됐고, banking 의 다른 손님 거래와 모든 파드, 풀, 세션, 배포가 그대로다.

이 후보가 관제 AI 에게 주는 것: 같은 'commerce checkout 502, 원인은 banking' 이 이제 넷(F44-P 노드 방화벽, F40-H transfer 릴리스, F35-H 이체 표, F56-R 계좌 행)이다. "banking 이 범인" 까지는 같고, 실패 문장의 종류(연결 시간 초과, 금액 검증, ORA 오류, 계좌 없음)와 롤아웃 유무, banking 자신의 거래가 성한지를 읽어야 정답(노드, 서비스, 이체 표, 계좌 표)이 갈린다.

## 9. 러너 판정 조건과 강도, 부하

- 주입: db.row_delete(Oracle) 1단 고정(`approved-fixed-f56-r`). 강도 축이 없다: 행이 없으면 그 두 계좌를 쓰는 이체는 요청량과 무관하게 100% 즉시 거절된다. min_hold 15m, settle 30s, timeout 20m, max_injection_duration 25m.
- 동반 부하: load.north_south `commerce surge.js` 3rps(여정의 절반이 checkout), ramp 2m, hold 21m, entry 30080(F44-P 와 같은 스크립트와 세기). checkout 초당 약 1.5건이 상주 기준선(평시 정산 분당 약 29건) 위에 실패 표본을 시간대와 무관하게 보장한다. 부하 상한 180 의 1.7%.
- 피해 계산(원칙 9): 실패가 잠금 대기나 시간 초과가 아니라 응답을 받은 즉시 400 이라 서킷브레이커는 실패를 줄이지 않고 빠르게 만들 뿐이다(order paymentClient 서킷 열림 → fallback 502). 로컬 실측에서 정산 꼴 이체 3/3 이 수십 ms 안에 400. commerce 쪽 checkout 성공은 행이 돌아오기 전까지 0 이다.
- success: 동반 부하 checkout 5xx 비율 ≥ 0.5, 2xx 비율 < 0.3, 3틱.
- must_rule_out: achieved_rps < 1, transfer 파드 NotReady, payment 파드 NotReady, PostgreSQL NotReady, Oracle 파드 NotReady, banking 기준선 잔액 조회 실패율 ≥ 0.2.
- abort: entry_status == 0(commerce nginx 나 노드가 죽음). 행 삭제는 commerce 진입 경로를 건드리지 않는다.
- recovery: target_health 200, transfer, payment, Oracle Ready, 기준선 checkout 실패율 < 0.1, 2틱, 10m.
- cleanup: 보관 행 가운데 ACCOUNTS 에 없는 것만 그대로 되넣고(삭제 시점 잔액, 없는 계좌로는 돈이 움직이지 않았다) 두 id 있음 확인, 보관 표 `DROP ... PURGE` 뒤 없음 확인. 보관 표가 없으면 행이 이미 돌아와 있을 때만 통과하고 값을 지어내지 않는다. 부하 종료.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Atlassian 2022-04-05, 공식 사후 검토)와 요소별 대응표, 기전이 원본과 같다: 잘못된 id 를 받은 정리 작업이 살아 있는 고객 개체를 지워 그 고객의 요청만 실패 (§2)
- [x] 분류 장부를 갱신했고 묶음 P(8→9, 12.5%), 정답 위치 DB 테이블(은행 계좌)(3→4, 5.6%), 결제 경로 12/72(16.7%), 서비스 은행 22→23 과 고른 이유를 적었다. 어느 축도 20% 미만 (§3)
- [x] 근본 원인 위치가 인프라 지점(BANKING.ACCOUNTS 의 두 행, 109 읽기 전용 조회)과 `file:line`(TransferService.java:53-72, BankingTransferClient.java:25-59)으로 확인됐다 (§4)
- [x] 근본 원인의 흔적이 119 에 남는 경로를 조회했다: lucida_logs_local 의 commerce-order 'Checkout payment failed' 가 payment 응답 본문 전체를 싣는 것(평시 기록), 'Account not found' 평시 0건, 정산 완료 로그 분당 약 29건, transfer 서버 스팬 상태 코드 분포 (§7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: 로그 전수(오류 문장, 정산 완료 흐름) (§7)
- [x] 계기 흔적: 사라진 계좌 id 를 직접 가리키는 거절 문장과, 그 분에 다른 변경 없이 그 두 계좌의 완료 흐름만 끊김. DELETE 문장은 남지 않음(F35-H 와 같은 한계)을 적었다. 인공 지연 없음 (§7)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다: "banking 계좌 표에서 commerce 정산 계좌가 없어졌다" 는 오류 문장에서 바로 나온다 (§5)
- [x] 정답지 세 칸이 원칙 6대로 채워졌다 (§5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다 (§6)
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: 원격 스크립트, argv, 보관 표 이름에 id 없음(테스트). SYS sqlplus 세션은 기존 Oracle 실행기 공통 한계 (§7)
- [x] 부하 상한과 서킷브레이커를 고려해 피해가 실제로 날 계산이 있다: 즉시 400 이라 요청량과 무관하게 100%, 동반 3rps, 로컬 실측 (§9)
