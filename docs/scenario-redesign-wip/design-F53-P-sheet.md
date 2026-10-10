---
title: F53-P 설계 시트 (운영을 가리킨 로컬 마이그레이션이 food 결제 표를 지워 모든 주문이 배차 뒤 결제 단계에서 502)
status: Draft
owner: project
last_reviewed: 2026-10-10
tags:
  - scenario
  - design
  - schema
  - mysql
  - operation
summary: 결제 표를 바꾸는 기능을 만들던 개발자가 자기 PC 에서 돌린 마이그레이션의 연결이 로컬 DB 대신 운영 food MySQL 을 가리켜, 마이그레이션의 표 지우기 단계가 살아 있는 fooddelivery.payments 를 지운다. payment-service 가 외부 PG 를 부르기 전 첫 쓰기(결제 INSERT)에서 MySQL 1146 으로 500 을 내고, order-service 는 주문 저장과 배차를 마친 뒤 결제 단계에서 502 로 답한다. 겉 증상은 외부 PG 장애(F19-S, F06-P)와 같고 정답은 결제 표다. 원본은 Resend 2024-02-21(F53-R 과 같은 원본).
---

# F53-P 설계 시트

## 1. 요약

food 의 payment-service 는 결제를 `fooddelivery.payments`(약 174만 행, 데이터 318MB, 인덱스 97MB, 인덱스 3개)에 저장한다. 이 표는 payment 만 쓰고 외래 키로 참조하는 표도 없다. 결제 표를 바꾸는 기능을 만들던 개발자가 자기 PC 에서 마이그레이션을 돌리는데, 명령이 쓰는 연결 설정이 로컬 DB 가 아니라 운영 food MySQL 을 가리키고 있었다. 마이그레이션의 표 지우기 단계가 운영의 살아 있는 `payments` 를 지운다. 그 순간부터 `processPayment` 는 외부 결제 대행(PG mock)을 부르기도 전에 첫 쓰기인 결제 PENDING INSERT 에서 MySQL 1146 으로 실패해 500 을 돌려준다. order-service 는 그때 이미 가게, 메뉴, 배달원 용량을 확인하고 주문을 저장했으며 배달원을 배차한 상태다. order 의 결제 클라이언트는 이 500 을 502 로 바꾸고, 몇 번 실패하면 결제 서킷이 열려 다음 주문부터는 곧바로 502 를 준다. 주문은 되돌려지고 배차만 남는다. 둘러보기, 배달 추적, 배차는 그대로다.

비유: 계산대 직원이 주문을 받아 주방에 넘기고 배달 기사까지 불렀는데, 마지막에 카드 결제를 적을 장부가 사라져 결제 창구가 "장부가 없다" 며 결제를 받지 못한다. 손님은 "결제 실패" 를 듣고 돌아간다. 카드사(PG)는 아무 연락도 받지 못했으니 카드사가 고장 난 것이 아니다.

F53-R(같은 원본, 같은 수단으로 주문 표를 지움)과 무엇이 다른가: 표를 쓰는 서비스가 order 가 아니라 payment 이고, 실패가 order 자신의 첫 INSERT(500, 배차 전)가 아니라 하류 payment 의 500 이 서킷을 거쳐 order 502 로 번진다(배차 뒤). 관제 AI 에게는 "결제 단계 502 면 외부 PG" 라는 지름길을 막는 짝이 된다(§8).

## 2. 원본 사례

- 기업: Resend (메일 발송 API)
- 날짜: 2024-02-21 (UTC 04:56 마이그레이션 시작, 17:05 해결)
- 링크: [Incident report for February 21, 2024 (공식 블로그)](https://resend.com/blog/incident-report-for-february-21-2024) (자료 문서 `ref-real-world-incidents.md` M22 에 이미 있음, F53-R 이 추가)
- 요약(출처가 말한 것만): 기능을 만들던 엔지니어가 로컬 환경에서 데이터베이스 마이그레이션을 돌렸는데 그 명령이 운영 환경을 가리켜 운영의 모든 표를 지웠다("incorrectly pointed to the production environment instead, which dropped all tables in production"). 04:57 운영에서 표가 지워지는 것을 알아챘고 05:01 백업 복원을 시작했다. 첫 복원은 잘못된 백업 시각 선택으로 실패했고, 더 오래된 백업에서 다시 복원해 17:02 API 요청 수락을 재개했다. 약 12시간 모든 사용자가 메일 발송, API, 대시보드를 쓰지 못했고("no API requests were being accepted and no data was being stored"), 마이그레이션 직전 5분의 기록을 잃었다.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 기능을 만들던 엔지니어가 로컬 환경에서 데이터베이스 마이그레이션 실행 | 결제 표를 바꾸는 기능을 만들던 개발자가 자기 PC 에서 마이그레이션 실행 |
| 원인이 된 결함 | 명령이 로컬이 아니라 운영 환경을 가리켜 운영의 모든 표를 지움 | 명령의 연결이 로컬 DB 가 아니라 운영 food MySQL 을 가리켜 마이그레이션의 표 지우기 단계가 운영 `fooddelivery.payments` 를 지움(그 기능이 바꾸는 표 하나로 규모를 줄임) |
| 전파 경로 | 표가 없어 API 가 요청을 받지도 저장하지도 못함 | payment 의 `Payment` 엔티티가 여전히 `payments` 에 씀 → 결제 INSERT 1146 → POST /api/payments 500 → order 결제 클라이언트 502(재시도 2회, 서킷 열림) → 주문 트랜잭션 되돌림 → POST /api/orders 502. 보존 정리 배치도 같은 오류 |
| 사용자 증상 | 메일 발송, API, 대시보드 약 12시간 전면 불가 | 주문 생성 100% 502(결제 단계). 가게 둘러보기, 메뉴, 배달 추적, 배차는 정상. 결제가 일어나지 않음 |
| 탐지된 경로 | 1분 뒤 운영에서 표가 지워지는 것을 알아챔 | payment 오류 로그(1146 WARN, 'doesn't exist' ERROR, Servlet ERROR, 보존 정리 WARN)와 order 오류율, ERROR 'Failed to process payment' 급증 → 119 이상 탐지 → 인시던트. DPM 표 수 16→15 |
| 완화와 복구 | 백업에서 복원(두 번째 시도에 성공), 5분 기록 손실 | cleanup 이 표를 원래 자리로 되돌림(행 그대로, 서킷이 닫히는 약 5초 뒤 회복). 원본의 백업 복원 몇 시간은 재현하지 않는다 |

기전은 원본과 같다: "개발 중 로컬에서 돌린 마이그레이션 명령이 운영을 가리켜 운영의 표를 지움 → 앱이 그 표를 찾지 못해 그 표를 쓰는 요청이 모두 실패 → 표를 되살리면 회복". 원본은 모든 표였고, 여기서는 그 기능이 바꾸던 표 하나(`payments`)다. 모든 표를 지우면 가게, 메뉴 표까지 사라져 loadgen 의 메뉴 조회 단계에서 여정이 끝나고 원인 신호가 흐려진다(F53-R 시트 §2). 원본에서도 표가 없어진 API 의 의존자들이 함께 실패했다는 점에서, 표를 쓰는 서비스(payment)의 실패가 그 서비스를 부르는 쪽(order)의 실패로 번지는 이 재구성은 원본의 전파와 같은 꼴이다. 안전 장치는 F53-R 과 같다: 진짜 DROP 은 174만 행을 잃으므로 실행기는 표를 앱 스키마 밖의 시스템 스키마 `mysql` 로 옮긴다(`mysql.fooddelivery_payments`). DPM 수집기는 `mysql` 스키마를 보고하지 않아 앱과 DB 목록 지표에는 DROP 그 자체로 보인다.

## 3. 숫자 근거 (2026-10-10, `scenario-stats.py`, 정식 + 후보 합계 64, 이 후보 전)

| 축 | 값 | 판단 |
|---|---|---|
| 서비스 | 쇼핑몰 26, 은행 20, 음식배달 18 | 음식배달이 가장 적다 → 음식배달(18→19) |
| 묶음 | A 7, B 7, D 7, G 7, J 7(각 10.9%), C 6, L 7 중 주 묶음 L 6, P 5, F 3, E 2, H 2, K 2, I 1, M 1, O 1, N 0 | P(운영 작업의 대상 착오) 5→6(65 중 9.2%). 결함이 앱이 기대하는 표가 없는 꼴로 드러나 F53-R, F48-R, F48-P 처럼 P+L. L 로 세도 7→8(12.3%) |
| 정답 위치 | DB 테이블(결제) 1(F06-H) | 1→2(3.1%). 결제 서비스(3)나 외부 결제 의존(6)이 아니다 |
| 결제 경로 합계 | 10(15.6%) | 이 후보는 결제 경로(결제 테이블)라 10→11/65(16.9%), 20% 미만 |
| 어느 축이든 | 최대는 A, B, D, G, J 7/65(10.8%), 주문 서비스 7/65(10.8%) | 20% 미만 |

묶음 해석: 바뀐 것은 표 하나의 존재다. 설정값이 아니라 G 가 아니고, 코드나 이미지 배포가 없어 J 가 아니며, 계획된 스키마 변경이 코드보다 먼저 적용된 F36-R(L)과도 다르다. 의도한 변경 내용은 맞았는데 적용 대상(환경)이 틀렸다. 그래서 P 다. DB 프로세스, 자원, 잠금, 질의 비용은 그대로라 A, B, D, F 가 아니고, 외부 PG 는 멀쩡해 C 가 아니다.

관계와 id: F53-R 의 원본(Resend)과 수단(db.ddl 표 지우기 모드)을 다른 표에 쓴다. F48-P 가 F48-R 의 원본과 수단을 다른 표에 쓴 것과 같은 꼴이라 F53 사례군의 P(일부 비슷)로 둔다. 다른 점: 표를 쓰는 서비스(order 대 payment), 전파(자기 INSERT 500 대 하류 500 이 서킷을 거쳐 order 502), 실패 지점(배차 전 대 배차 뒤 결제 단계), 정답 위치(DB 테이블(주문) 대 DB 테이블(결제)). 같은 수단의 다른 표는 다른 파라미터라 같은 주입 중복(같은 수단과 같은 파라미터)이 아니다.

겉 증상(food 주문 502 'Payment service ...')은 외부 PG 장애 F19-S(정식), F06-P(정식, 429)와 결제 단계라는 점이 같고 정답이 다르다(카탈로그 관계 H). 관제 AI 가 "결제 단계에서 주문이 깨지면 외부 PG" 를 외워 찍지 못하게 하는 짝이다. 가르는 관측 근거는 §8 에 둔다.

### 3단계 후보 목록 (실제 기전 × 부품, 숫자 순으로 줄 세움, 버린 이유 포함)

기존 시나리오 목록이 아니라 자료 문서의 기전(M1~M23)과 웹에서 찾은 사례에서 출발해 부품 7곳(food MySQL 결제, 메뉴 표, banking Oracle 이체, 계좌 표, tb-w3 노드, commerce PostgreSQL, food payment 앱과 배치)에 걸쳐 뽑았다. 웹 검색은 9회(배치 겹침, 반올림 결함 결제 사고, 'too long for column', 트리거, 커밋 전 조회, VACUUM FULL, ORA-01502, conntrack, 결제 배포 NullPointerException)다.

| 순위 | 후보 | 원본 사례 | 부품(정답 위치 수, 서비스) | 결과 |
|---|---|---|---|---|
| 1 | 운영을 가리킨 로컬 마이그레이션이 food payments 표를 지움 | Resend 2024-02-21 공식 블로그 | food MySQL, DB 테이블(결제) 1, 음식배달 18 | **채택(F53-P)** |
| 2 | 운영을 가리킨 로컬 마이그레이션이 food menus 표를 지움 | Resend 2024-02-21 | food MySQL, DB 테이블(메뉴) 0, 음식배달 18 | 버림(원칙 7): loadgen 주문 여정이 메뉴 조회(200)를 지나야 주문을 보내므로(`script.js:121-134`) 메뉴 표가 없으면 주문 표본이 사라지고 restaurant 메뉴 500 만 남는다. rejected 의 'restaurant DB_NAME 빈 스키마' 행, F53-R 시트 §2 와 같은 벽 |
| 3 | 운영을 가리킨 로컬 마이그레이션이 banking Oracle transfers 를 지움 | Resend 2024-02-21 | banking Oracle, DB 테이블(이체) 0, 은행 20 | 뒤로 미룸(F53-R 시트와 같음): 은행이 음식배달보다 2 많고, Oracle 은 스키마 사이 RENAME 이 없어 진짜 DROP 과 휴지통(FLASHBACK) 복원이 필요하다. 다음 은행 차례의 후보 |
| 4 | 운영 삭제 스크립트에 잘못된 id 목록을 넘겨 살아 있는 banking 계좌 행(commerce 정산 계좌 포함)을 지움 → 이체 400 'Account not found' → commerce checkout 502 | Atlassian 2022-04-05 공식 사후 보고(앱 id 대신 사이트 id 를 넘겨 883개 사이트 삭제) | banking Oracle accounts, DB 테이블(은행 계좌) 3, 은행 20 | 뒤로 미룸: 은행이 음식배달보다 많고, 행을 지웠다 되돌리는 실행기(보관 표 복사와 재삽입)를 새로 만들어야 한다. rejected 의 같은 원본 행(계좌 FROZEN)은 FROZEN 검사가 account 경로에만 있어 막혔는데 행 삭제는 transfer 의 조회에서 실패해 그 벽에 걸리지 않는다. 다음 은행 차례의 후보 |
| 5 | 정기 작업(크론잡)이 겹쳐 돌며 food MySQL 을 과부하 | PagerDuty 공식 블로그(예약 배치 잡이 Cassandra 를 몰아쳐 노드 과부하, 클라이언트 재시도가 연장) | food MySQL, '배치와 크론' 0 | 버림(원칙 9, 1): 바깥 세션이 MySQL CPU 를 다 써도 ms 단위 OLTP 는 거의 굶지 않는다(F53-R 시트 후보 5, rejected 의 PopularMenuBatch, InterestBatch 행과 같은 벽). 원본은 Cassandra 노드의 요청 취소와 재시도 연장이라 고리가 다르다 |
| 6 | 큐에서 실패를 되풀이하는 작업 하나가 루프를 돌며 처리 경로를 포화 | Payplug 2025-09-04 공식 상태 페이지(배포가 두 API 사이 연결을 끊고 작업이 루프) | food payment outbox 릴레이(결제 서비스 3) | 버림(원칙 7, 1): outbox 발행은 비동기라 사용자 경로에 증상이 없고, 원본은 배포한 구성 요소와 변경을 밝히지 않는다 |
| 7 | 오프라인 표 재구성(ALTER TABLE MOVE)이 인덱스를 UNUSABLE 로 남겨 이체 INSERT 가 ORA-01502 | 공식 사후 보고 없음(웹 검색 1회: SAP KB, 벤더 블로그뿐) | banking Oracle transfers, DB 테이블(이체) 0 | 버림(원칙 1) |
| 8 | 업무 시간 VACUUM FULL 이 표를 ACCESS EXCLUSIVE 로 잠금 | GitLab 2016-11-28 공개 사고 이슈(VACUUM FULL 은 원인이 아니라 부풀린 표를 고친 조치) | commerce PostgreSQL 표(쇼핑몰 26) | 버림(원칙 1): 원인으로 VACUUM FULL 을 든 공식 사후 보고를 찾지 못함 |
| 9 | 새 릴리스가 결제 상태에 열 폭(VARCHAR 16)보다 긴 값을 써 1406 | 공식 사후 보고 없음(웹 검색 1회: 열 폭 초과를 원인으로 든 사후 보고 없음) | food payment(결제 서비스 3), J | 버림(원칙 1). rejected 의 'PG 가 더 긴 상태값' 행과 같은 결론 |
| 10 | 새 릴리스가 order 트랜잭션 안에서 아직 커밋되지 않은 주문을 되돌려 조회(404 경쟁) | 공식 사후 보고 없음(웹 검색 1회) | food payment(결제 서비스 3), J | 버림(원칙 1, F34-R 교훈): payment 가 404 를 내면 order 가 4xx 를 그대로 전파해 4xx 단일 신호가 된다 |
| 11 | conntrack 표 가득으로 새 연결 SYN 버림 | 공식 사후 보고 없음(웹 검색 1회 더: 벤더 KB, 포럼, 개인 블로그뿐) | tb-w3 노드(노드, 디스크 6) | 버림(원칙 1), rejected 2026-10-09 행과 같은 결론 |
| 12 | 배포 도구 결함이 이전 설정 변경을 다시 실행 | Xendit 2023-11-29 공식 상태 페이지 | food payment 설정(결제 서비스 3), G | 버림(원칙 1): 사후 보고가 어떤 설정이 다시 적용됐는지 밝히지 않고, 같은 사고의 상태 갱신은 원인을 네트워크 문제로 적어 서로 맞지 않는다 |

rejected 기록, 장부 §4-1, §5, 이번 실행에서 막힌 목록과 대조: 같은 원본(Resend)은 F53-R(orders)이 썼고 같은 주입은 orders 표뿐이다(다른 표는 다른 파라미터). rejected 의 '취소된 백필이 food payments 표를 치움'(GitHub 2026-07-24 원본)은 174만 행 그림자 복사(INSERT … SELECT)가 수십 초 걸리며 원본 표 끝의 공유 잠금이 결제 INSERT 를 막는 벽이었다. 이 후보는 그림자 복사 없이 메타데이터 한 문장(RENAME)이라 그 벽이 없다(§9).

## 4. 인과 사슬 (코드와 인프라 위치)

1. 엔티티: `food-delivery/payment-service/src/main/java/com/fooddelivery/payment/entity/Payment.java:7-12` `@Entity @Table(name = "payments")`, `@GeneratedValue(strategy = GenerationType.IDENTITY)` 라 `save` 가 곧바로 INSERT 를 보낸다.
2. 결제 처리: `PaymentService.java:50-88` `processPayment`(@Transactional). 첫 쓰기 `paymentRepository.save(payment)`(:66, PENDING)에서 실패한다. PG 호출 `pgApiClient.pay`(:70), 'Processed payment' 로그(:85)는 그 뒤라 실행되지 않는다. PG 실패 때 쓰는 FAILED 행(:72-75)도 생기지 않는다.
3. 예외 처리: `GlobalExceptionHandler.java:10-20` 는 `ServiceException` 만 다룬다. `InvalidDataAccessResourceUsageException` 은 Spring 기본 처리로 500 이 되고 Tomcat 이 ERROR 'Servlet.service() … [Request processing failed: … could not execute statement [Table 'fooddelivery.payments' doesn't exist] [insert into payments …]]' 를 남긴다. 그 앞에 Hibernate `SqlExceptionHelper` 의 WARN 'SQL Error: 1146, SQLState: 42S02' 와 ERROR "Table 'fooddelivery.payments' doesn't exist".
4. 하류에서 상류로: `order-service/.../client/PaymentClient.java:30-57` 가 5xx 를 ERROR 'Failed to process payment for order {}: 500 …' 과 `ServiceException(502)` 로 바꾼다. Retry `payment` max-attempts 2(`order-service/src/main/resources/application.yml:112-113`), CircuitBreaker `payment`(창 10, 최소 5회, 실패율 50%, 열림 5초) 가 열리면 fallback 이 502 'Payment service unavailable: CircuitBreaker 'payment' is OPEN …'.
5. 주문: `OrderService.java:128-159` 주문 저장(:128), 'Created order'(:141), 배차(:145) 뒤 결제 호출(:154) 실패를 INFO 'Order fan-out payment failed: … status=502' 로 남기고 다시 던져 트랜잭션이 되돌려진다. 배차 행은 dispatch 쪽에 남는다.
6. 보존 정리: `PaymentRetentionBatch.java:41-57` 가 6초마다 payments 를 읽고(`select p1_0.id, p1_0.status, … from payments p1_0 …`) 실패를 WARN 'Payment retention purge failed, will retry next cycle: …' 로 남긴다. 요청량과 무관한 시각 고정 신호다. 정산 배치 `SettlementBatch.java:36-42` 는 1시간 주기라 창 안에 돌면 같은 오류.
7. 재시작해도 표를 만들지 않는다: `payment-service/src/main/resources/application.yml:20` `ddl-auto: none`.
8. health: payment 의 readiness(10초, 3회), liveness(15초, 5회)는 `/actuator/health`(`k8s/23-payment-deploy.yaml:73-86`)이고 DB 확인은 질의 없는 `isValid()` 라 표가 없어도 UP 이다(로컬 실측).
9. 스키마: `food-delivery/db/init.sql:154-165` payments(PRIMARY, idx_payments_order, idx_payments_settlement). 외래 키 없음(`init.sql:110` 주석).
10. 인프라 109 실측(2026-10-10 06:5x UTC, 읽기 전용 조회): testbed-mysql-0 MySQL 8.0.46, fooddelivery 표 16개, 인덱스 31개(payments 3), payments 1,739,898행(데이터 318MB, 인덱스 97MB), 시스템 스키마 mysql 에 fooddelivery_ 로 시작하는 표 0개, payments 를 참조하는 외래 키 0개, @@lock_wait_timeout 31536000(기본). testbed-payment 파드 Ready, 이미지 food-delivery-payment:latest.
11. 로컬 실측(MySQL 8.0 컨테이너, init.sql, origin/main 의 order, restaurant, dispatch, payment 1.0.0 jar, PG 는 POST /pay 에 APPROVED 를 주는 작은 HTTP 서버, Kafka 없음과 outbox 릴레이 끔):
    - 평시 주문 2/2 가 200.
    - `RENAME TABLE fooddelivery.payments TO mysql.fooddelivery_payments`(lock_wait_timeout 10) 즉시 성공, fooddelivery 표 수 16→15, 인덱스 수 31→28.
    - 표가 없는 동안 주문 8/8 이 502(약 0.4초). 첫 주문은 'Payment service failed: 500', 이후는 'CircuitBreaker 'payment' is OPEN'. payment 직접 호출도 500. order, payment health 내내 UP.
    - payment 로그: WARN 'SQL Error: 1146, SQLState: 42S02', ERROR "Table 'fooddelivery.payments' doesn't exist", ERROR 'Servlet.service() … could not execute statement [Table 'fooddelivery.payments' doesn't exist] [insert into payments (amount,created_at,order_id,pg_provider,processed_at,settled_at,status) values …]' 가 결제 요청마다, WARN 'Payment retention purge failed, will retry next cycle: JDBC exception executing SQL [select p1_0.id,p1_0.status,… from payments p1 …' 이 6초마다.
    - order 로그: ERROR 'Failed to process payment for order N: 500 : …' 과 INFO 'Order fan-out payment failed: orderId=N status=502 …'. dispatch 'Dispatched courier' 는 실패한 주문 수만큼 계속(8건). PG 서버는 그 동안 요청을 받지 않음.
    - 되돌린 뒤(반대 RENAME) 표 수 16, 행 그대로, 주문 10/10 이 200(서킷 반열림 5초 뒤).
    - 실행기 원격 스크립트(`MYSQL_DROP_REMOTE`, kubectl 대신 docker exec 로 바꾼 시험 껍데기, 인자 payments mysql 1000): preflight 0 → run 0 → preflight 1("Table 'fooddelivery.payments' doesn't exist") → cleanup 0 → cleanup 0(두 번째는 할 일 없음) → recovery 0, 시스템 스키마에 남은 표 0.

## 5. 원인 규정 (원칙 5, 6)

| 칸 | 값 | 근거 |
|---|---|---|
| `root_cause.target_id` | `food-mysql:fooddelivery.payments` | 결함은 표 쪽이다(운영을 가리킨 마이그레이션이 살아 있는 표를 지움). 원본 보고의 원인과 복구도 표 지우기와 그 복원이다. 표기는 F53-R, F48-P, F48-R, F36-R, F33-R 과 같은 '인스턴스:스키마.테이블' 꼴 |
| `trigger_target_id` | null | 근본과 계기가 같은 곳(이 표에 대한 잘못된 대상의 마이그레이션) |
| `scoring.partial` | `food-delivery-payment`, `food-payment`, `food-mysql` | 오류가 찍히는 서비스와 DB 인스턴스는 원인 근처다. 증상 서비스 order 와 외부 PG 는 부분 점수가 아니다(PG 는 불리지도 않는다) |

원칙 5: 정답은 로그의 오류 문장("없는 표 payments"), 실패한 문장이 결제 INSERT 라는 사실, 같은 DB 의 다른 쓰기(주문 저장, 배차) 성공, 같은 분 DPM 표 수 감소, 그 시각에 배포나 재시작이 없다는 사실로 낼 수 있는 결론이다. "payment 가 결제에서 SQL 오류를 낸다" 고 답하면 payment(부분), "payments 표가 사라졌다(DDL, 운영 작업)" 고 답하면 정답이다. 누가 어느 PC 에서 왜 마이그레이션을 돌렸는지는 요구하지 않는다.

원칙 6: order 의 주문과 payment 의 결제 요청, 배치는 늘 하던 정당한 요청이고 결함 있는 곳(사라진 표)에서 실패했다. 표를 지운 잘못된 대상의 마이그레이션이 원인이다.

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 | 기대 |
|---|---|---|
| 감지 | payment 서버 스팬 오류와 새 오류 로그 템플릿(1146 WARN, 'doesn't exist' ERROR, Servlet ERROR, 보존 정리 WARN), order 서버 스팬 502 와 ERROR 'Failed to process payment' | 주문 생성 100% 502. payment 의 보존 정리 WARN 이 부하와 무관하게 6초마다 이어진다 |
| 피해 판정 | 러너: 기준선(loadgen-food) 주문 생성 5xx 비율 ≥ 0.8 과 2xx 비율 < 0.2(3틱) | 평시 주문 생성 5xx 는 0 근처(배차 한도 503 이 드물게) |
| 원인 설명 | 로그 전수(1146 오류 문장, 실패 문장 'insert into payments', 'Processed payment' 멈춤, 'PG /pay failed' 없음, order 'Created order' 와 dispatch 'Dispatched courier' 계속), DPM 표 수, 인덱스 수, 행 수, KCM 무변화 | §7 |

인시던트 근거와 위험:

- 오류 로그가 두 서비스(payment, order)에 나고, payment 의 새 템플릿이 넷(1146 WARN, 표 없음 ERROR, Servlet ERROR, 보존 정리 WARN)이다. 보존 정리 WARN 은 요청량과 무관하게 6초마다 이어져 가장 한가한 시간에도 로그 급증 이벤트가 날 것으로 본다. order 의 ERROR 'Failed to process payment' 는 평시 F30-R 실행 창(400)과 2026-10-08 07시(500, 2건)에만 있었다.
- 서킷이 열리면 payment 로 가는 요청은 5초마다 반열림 시도 3건으로 줄어든다. 그래도 보존 정리 WARN 과 반열림 시도마다 1146 이 이어져 payment 쪽 신호가 끊기지 않는다.

## 7. 관측 근거 표 (119 실조회, 2026-10-10 06:40~07:00 UTC)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 결정 증거는 전수 로그와 DPM, KCM 이다.

| 증거 | 119 표와 칸 | 조회 | 평시 결과 |
|---|---|---|---|
| 근본: 없는 표 오류 | CH `lucida_logs_local` (`service_name`, `body`) | `SELECT service_name, countIf(position(body,'SQL Error: 1146')>0), countIf(position(body,'payments'' doesn''t exist')>0), countIf(position(body,'Payment retention purge failed')>0), countIf(position(body,'Processed payment id=')>0), min(timestamp), count() FROM lucida.lucida_logs_local WHERE service_name IN ('food-delivery-payment','food-delivery-order') GROUP BY 1` | payment: 1146 0건, 표 문장 0건, 보존 정리 실패 1건(2026-10-08 20:33 'Could not open JPA EntityManager', 다른 문장), 'Processed payment' 415,996건, 가장 이른 로그 2026-10-03 04:00 UTC, 전체 418,103건. order: 1146 0건 |
| 근본: 예외 종류 | 같은 표 | payment 의 `countIf(position(body,'InvalidDataAccessResourceUsageException')>0)`, `'SQLSyntaxErrorException'`, `'PG /pay failed'` | 셋 다 0건 |
| 수집 경로: payment 의 Hibernate 오류 로그 | 같은 표 | 최근 1일 payment 템플릿별 | F38-R 실행 창의 WARN 'SQL Error: 1290, SQLState: HY000' 483건, ERROR '--read-only …' 483건. 같은 로거(SqlExceptionHelper)의 1146 문장도 수집된다 |
| 상류 증상의 평시 | 같은 표 | order 의 'Failed to process payment for order' 시간대별 | 750건 모두 2026-10-08(07시 500 2건, 09, 14, 18시 F30-R 실행 창의 400 'orderId required') |
| 기준선 주문량 | 같은 표 | 'Created order id=' 를 KST 시각별 7일 평균(F53-R 시트) | 분당 11.8(06시)~68.4(17시)건 |
| 계기: 표 지우기 | VM `dpm.mysql.database.table_count{db_name="fooddelivery"}`, `index_count`, `row_count` (target c8c558e5-…, 60초 간격) | 현재값, `min_over_time`, `max_over_time` [7d] | table_count 현재 16, 7일 최소 16 최대 16. index_count 현재 31. 109 실측으로 payments 인덱스 3개, 약 174만 행이라 지우면 15, 28, 약 174만 감소 |
| 계기: 시스템 스키마는 보고 안 함 | VM `dpm.mysql.database.*` 의 `db_name` 값 | F53-R 시트 §7 | db_name 은 fooddelivery, information_schema, performance_schema, sys 넷뿐. mysql 스키마로 옮긴 표는 어느 지표에도 새로 나타나지 않는다 |
| 계기: DDL 문장 자체 | CH `dpm_topsql_local` | F48-R, F48-P 시트와 같음 | DDL 은 Top SQL 에 남지 않는다 |
| 다른 변경 없음 | CH `kcm_events_local` | `namespace='rca-testbed-food' AND position(object_name,'testbed-payment')=1` 사유별 | Unhealthy 549건(마지막 2026-10-08 20:33), ScalingReplicaSet 135건(마지막 2026-10-08 18:53), Created, Pulled, Started 각 81건. 롤아웃은 이 표에 남는다 |
| 피해: 주문 생성 스팬 | CH `otel_traces_local` (`span_kind`, `span_name`, `span_attributes['http.response.status_code']`) | 7일 order 서버 스팬 POST /api/orders 상태별 | 200 19,404, 400 929, 502 190, 503 170, 500 170(대부분 시나리오 실행 창) |
| 배제: PG 가 아님 | CH `otel_traces_local` | payment CLIENT 스팬 'POST'(PG 호출) 최근 1일 | 5,795건(10% 표본). 고장 중에는 생기지 않아야 한다(PG 를 부르기 전 실패) |

## 8. 감별

- must_support: payment 의 1146 과 "Table 'fooddelivery.payments' doesn't exist"(평시 0), 6초마다 보존 정리 WARN, order 502 와 ERROR 'Failed to process payment … 500' 뒤 서킷 열림, 같은 시도에 'Created order' 와 'Dispatched courier' 는 남고 'Processed payment' 는 멈춤, PG 호출 스팬 사라짐과 'PG /pay failed' 없음, 같은 분 DPM 표 수 16→15, 인덱스 수 31→28, 행 수 약 174만 감소, 롤아웃과 재시작 없음, payment Hikari 대기 0.
- must_rule_out(정답지에 문장으로): 외부 PG 장애(F06-P, F19-P, F19-S), payment 새 버전이나 설정 배포(F30-R), 주문 표 지우기(F53-R), MySQL 인스턴스 읽기 전용(F38-R), MySQL 다운이나 재시작, payment 파드 멈춤이나 부하, 용량 부족.
- contrast_with: F53-R, F19-S, F06-P, F38-R, F30-R.
- 외부 PG 장애(F19-S, F06-P, F19-P)와 가르는 관측 근거(같은 결제 단계 실패):
  - payment 로그: PG 장애는 ERROR 'PG /pay failed for order=…'(`PgApiClient.java:48`)와 PG 응답 코드, 시간 초과를 남기고 FAILED 결제 행을 쓴다. F53-P 는 1146 과 표 이름뿐이고 'PG /pay failed' 가 없다.
  - PG 호출: PG 장애는 payment 의 PG 호출(CLIENT 스팬)이 실패하거나 느려진다. F53-P 는 PG 호출 자체가 사라진다.
  - 손님이 받는 코드: F06-P 는 PG 의 429 가 그대로 손님에게 간다. F19-P 는 order 자신의 풀이 마른다. F19-S 와 F53-P 는 둘 다 502 라 위 두 근거와 DPM 표 수로 가른다.
- F53-R 과 가르는 관측 근거(같은 원본, 같은 수단): F53-R 은 order 로그에 1146 이 나고 오류가 orders 를 가리키며, 주문이 500 이고 'Created order' 와 배차가 없다. 인덱스 수는 27 이 된다. F53-P 는 payment 로그에 1146 이 나고 payments 를 가리키며, 주문이 502 이고 'Created order' 와 배차가 남는다. 인덱스 수는 28 이 된다.
- F38-R 과 가르는 관측 근거: F38-R 은 1290 '--read-only' 이고 order, dispatch, payment 쓰기가 함께 실패한다. F53-P 는 1146 이고 payments 를 부르는 문장만 실패한다.
- F30-R 과 가르는 관측 근거: F30-R 은 testbed-payment 롤아웃이 KCM 에 남고 payment 가 400 'orderId required' 를 낸다(주문은 4xx). F53-P 는 롤아웃이 없고 payment 가 500 이다.
- 판별력: 서비스 입도 채점에서는 payment 가 부분 점수다. 정답 입도(database-relation)에서는 표 이름이 정답이고 인스턴스(food-mysql)는 부분 점수다. 외부 PG(외부 결제 의존)를 찍은 답은 오답이다.

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

진행 대본: 설계 강도 하나로 고정한 평가 모드(`approved-fixed-f53-p`). 강도라는 축이 없다: 표가 없으면 결제 INSERT 는 요청량과 무관하게 100% 실패한다.

| 항목 | 값 | 근거 |
|---|---|---|
| primary | db.ddl(표 지우기 모드, F53-R 과 같은 원격 스크립트), 계약 `{engine mysql, rca-testbed-food, testbed-mysql-0, fooddelivery, payments, hold_schema mysql, minimum_rows 1000}` | 실행기 CONTRACTS 와 profiles.json 같은 값(G2). 원격 스크립트가 F53-R 과 같다는 것을 테스트로 고정 |
| companion | 없음 | 실패한 주문도 배차를 남긴다(로컬 실측 8건). F48-P 와 같은 이유로 주문 동반 부하를 두지 않는다 |
| entry_status | 기준선 문서(loadgen-food)의 주문 생성 단계(domain food-delivery) | 고장 중 502(0 아님). 0 은 노드나 order 가 죽었을 때만 |
| min_hold / settle / timeout | 15m / 30s / 20m, max_injection 25m | F48-P, F53-R 과 같음 |
| success(3틱) | 기준선 주문 생성 5xx 비율 ≥ 0.8, 2xx 비율 < 0.2 | loadgen 집계(표본 아님). F48-P 와 같은 관측 |
| must_rule_out(2틱) | MySQL NotReady, order NotReady, payment NotReady | DB 다운, order 다운, payment 다운이면 다른 시나리오다 |
| abort | entry_status == 0 (필수) | |
| recovery(2틱, 10m) | target_health 200, MySQL, order, payment Ready, 기준선 주문 2xx ≥ 0.7 | 되돌리면 서킷 반열림(5초) 뒤 정상(로컬 실측 10/10) |

부하 계산:

- 기준선: 하루 주기 1~6 iter/s 로 주문 생성이 분당 약 12~68건, 30초 창에 6~34건. 고장 중 모두 502 → 5xx 비율 1.0, 2xx 비율 0. 문턱(0.8, 0.2)을 어느 시간대에도 넘는다.
- 배달원 한도: 실패한 주문도 배차 행(ASSIGNED)을 남기지만 평소 성공한 주문이 남기던 만큼과 같은 속도다(기준선만 쓰므로 늘지 않는다). 동반 부하를 더하면 F48-P 시트처럼 바쁜 시간(기준선 ASSIGNED 약 1,700, 한도 2000)에 약 4분 만에 한도가 차 503 이 섞이므로 두지 않는다.
- 강도와 피해: 주입은 메타데이터 문장 하나라 표 크기(174만 행)와 무관하게 즉시 끝난다. RENAME 은 payments 의 배타적 메타데이터 잠금이 필요해 진행 중인 결제 트랜잭션(평소 약 7ms, 119 POST /api/payments p50 6.6ms)과 보존 정리 한 묶음(findRetentionHeads p50 7.8ms + 묶음 삭제)을 기다리고, 그동안 새 결제 문장이 뒤에 줄 선다. 1시간 주기 정산 배치(미정산 승인 결제 수천 건을 읽고 건마다 UPDATE, 한 트랜잭션)와 겹치면 수 초 기다릴 수 있다: 그동안 order 가 결제를 기다리며 쥐는 연결은 기준선 최대 약 1.1건/초 × 수 초로 order 풀 15 아래이고, payment 풀(15)도 같은 계산이며, readiness 는 10초 주기 3회 실패가 필요해 빠지지 않는다. lock_wait_timeout 10초 안에 잠금을 못 얻으면 아무것도 바뀌지 않은 채 run 이 실패하고 MySQL 오류 첫 줄이 러너 로그에 남는다.
- 실패 뒤: payment 는 수 ms 안에 500 을 돌려주고 order 는 재시도 대기(최대 수백 ms)와 서킷 열림 뒤 즉시 502 로 끝나 풀을 오래 쥐지 않는다(로컬 실측 약 0.4초). health 는 isValid() 라 둘 다 Ready 다.
- 서킷브레이커: order 의 payment 서킷이 열리는 것이 이 장애의 전파 모양이다(손님은 502 를 그대로 받는다). restaurant, dispatch 서킷은 하류가 200 이라 열리지 않는다. payment 의 pg 서킷은 PG 를 부르지 않으므로 움직이지 않는다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Resend 2024-02-21, 공식 블로그 사고 보고)와 요소별 대응표가 있고 기전이 같다(규모만 모든 표 → 그 기능의 표 하나) (§2)
- [x] 분류 장부 §4-1, §6 갱신. 묶음 P 5→6(9.2%, L 로 세도 12.3%), 정답 위치 DB 테이블(결제) 1→2(3.1%), 결제 경로 10→11/65(16.9%), 음식배달 18→19. 어느 축도 20% 미만 (§3)
- [x] 근본 원인 위치: `Payment.java:7-12`, `PaymentService.java:66`, `PaymentRetentionBatch.java:41-57`, 인프라 testbed-mysql-0 fooddelivery.payments (§4)
- [x] 근본 원인 흔적: CH `lucida_logs_local` 1146 과 표 이름 오류 문장(전수, 평시 0건). 같은 로거의 문장이 payment 에서 수집되는 것은 F38-R 실행 창의 1290 문장으로 확인 (§7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: 로그 전수, DPM 표 수, 인덱스 수, 행 수, KCM. 스팬은 보조 (§7)
- [x] 계기 흔적: DPM table_count 16→15, index_count 31→28, row_count 약 174만 감소(1분 간격, 표 수 7일 동안 16 고정), 오류 문장의 표 이름, 그 시각에 롤아웃과 재시작 없음. 인공 지연 없음 (§7)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다 (§5)
- [x] 정답지 세 칸이 원칙 6대로 (§5)
- [x] 감지, 피해 판정, RCA 증거 구분과 인시던트 근거 (§6)
- [x] 주입 도구가 시나리오 id 를 남기지 않는다: 옮긴 표 이름은 `fooddelivery_payments` 이고 인자와 스크립트에 id 없음(테스트로 고정). 동반 부하 없음
- [x] 부하 상한과 서킷브레이커를 고려한 피해 계산, 메타데이터 잠금 대기의 최악 경우 (§9)

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- payment 로그에 'SQL Error: 1146, SQLState: 42S02' 와 "Table 'fooddelivery.payments' doesn't exist", 'Servlet.service() … [insert into payments …]' 가 결제 요청마다, 'Payment retention purge failed' 가 6초마다 남는지. POST /api/payments 스팬이 500 인지.
- order 에 ERROR 'Failed to process payment for order N: 500' 뒤 'CircuitBreaker 'payment' is OPEN' 이 이어지고 POST /api/orders 가 502 인지. 같은 시도에 'Created order' 와 dispatch 'Dispatched courier' 는 남고 payment 'Processed payment', PG 호출 스팬, 'PG /pay failed' 는 없는지.
- VM 의 `dpm.mysql.database.table_count{db_name="fooddelivery"}` 가 주입 분에 15, `index_count` 가 28 이 되고 `row_count` 가 약 174만 줄었다가 cleanup 뒤 16, 31 로 돌아오는지.
- 인시던트: 묶음 멤버 템플릿(payment, order)과 판정 사유, 실행 시각대. 관제 AI 가 외부 PG 를 찍는지(이 짝의 판별력).
- 주입이 메타데이터 잠금 대기로 실패(lock_wait_timeout 10초)하면 아무것도 바뀌지 않은 채 run 이 실패하고 MySQL 오류 첫 줄이 러너 로그에 남는다. 정산 배치(정시 근처)와 겹치는지 시각을 본다.
- cleanup 뒤 fooddelivery.payments 가 행 그대로이고 `mysql.fooddelivery_payments` 가 없는지(recovery 가 확인).
- 녹화 창에 food MySQL OOM 재시작이 끼면 녹화로 쓰지 않는다(testbed-mysql-0 재시작 44회, 마지막 2026-10-08). 재시작이 끼어도 표는 mysql 스키마에 남아 있고 cleanup 이 되돌린다.
