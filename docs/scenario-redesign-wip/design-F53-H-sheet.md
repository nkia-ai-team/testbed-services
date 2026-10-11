---
title: F53-H 설계 시트 (결제 표 열 추가 마이그레이션이 같은 표의 느린 내보내기 뒤에서 메타데이터 잠금을 기다려 결제 표 질의가 모두 줄을 서고, payment 풀 고갈로 모든 새 음식 주문이 결제 단계에서 502)
status: Draft
owner: project
last_reviewed: 2026-10-11
tags:
  - scenario
  - design
  - mysql
  - migration
  - lock
summary: 결제 이력을 데이터 웨어하우스로 옮기는 느린 내보내기가 food MySQL fooddelivery.payments 의 공유 메타데이터 잠금을 쥔 사이, 운영자가 같은 표에 null 허용 열을 ALGORITHM=INSTANT 로 더하는 마이그레이션을 돌려 그 ALTER 가 배타 메타데이터 잠금을 기다리는 동안 결제마다 처음 하는 결제 INSERT 가 그 뒤에 줄을 서고, payment 풀이 차서 약 6분 동안 모든 새 주문이 배차까지 된 뒤 결제 단계에서 502 로 롤백되는 시나리오. 원본은 Honeycomb 2024-08-06. F53-P(같은 결제 표, 같은 결제 단계 502 이지만 표가 치워짐)와 같은 증상의 다른 원인(H).
---

# F53-H 설계 시트

## 1. 요약

food 의 payment-service 는 결제마다 외부 PG 를 부르기 전에 `fooddelivery.payments` 에 PENDING 결제 행을 먼저 넣는다. 6초마다 도는 보존 정리와 정산 배치도 같은 표를 읽고 쓴다. 회계팀의 내보내기 작업이 결제 이력 전체(약 173만 행)를 한 행씩 받아 일정한 속도로 데이터 웨어하우스에 옮기고 있다(`SELECT id, order_id, pg_provider, amount, status, processed_at, settled_at, created_at FROM payments`, 약 6분 동안 활성). 그 몇 초 뒤 운영자가 정산 대사에 쓸 PG 거래 번호 열을 더하는 마이그레이션을 돌린다:

```sql
ALTER TABLE payments ADD COLUMN pg_transaction_id varchar(64) NULL, ALGORITHM=INSTANT
```

MySQL 8.0 에서 INSTANT 열 추가는 데이터 사전만 바꾸는 안전한 변경이지만 그 순간에 표의 배타 메타데이터 잠금이 필요하다. 내보내기가 공유 메타데이터 잠금을 쥐고 있어 ALTER 는 `Waiting for table metadata lock` 으로 기다리고(lock_wait_timeout 기본값 1년), 기다리는 배타 요청은 새 공유 요청보다 앞서므로 그 뒤의 payments 질의는 모두 ALTER 뒤에 줄을 선다. 결제 요청마다 트랜잭션 안에서 연결을 쥔 채 기다려 payment 의 Hikari 15개가 결제 시도 약 15번 안에 차고, order 는 가게, 메뉴, 배달원 확인, 주문 저장, 배차까지 평시대로 끝낸 뒤 결제 단계에서 실패해 주문을 롤백하고 502 를 돌려준다. payment 는 health 실패로 빠지고 재시작된다. DB 에서는 아무 문장도 실패하지 않고 MySQL 은 한가하며, 다른 표는 평시대로 답한다. 내보내기가 끝나면 ALTER 가 잠금을 얻어 즉시 끝나고 줄이 풀린다.

비유: 회계 장부를 감사팀 직원이 한 장씩 천천히 베끼고 있다(장부를 펴 둔 상태). 경리 팀장이 "장부에 'PG 거래 번호' 칸을 하나 그어 넣겠다"며 장부 앞에 섰다. 칸 하나 긋는 데는 1초면 되지만 베끼던 사람이 덮을 때까지 기다려야 하고, 팀장이 줄 맨 앞에 서 있으니 결제를 적으러 온 경리 열다섯 명이 모두 그 뒤에 선다. 계산대는 주문을 받고 배달원까지 불렀는데 결제를 적을 사람이 없어 손님을 돌려보낸다. 베끼기가 끝나면 칸은 1초 만에 그어지고 줄이 풀린다.

## 2. 원본 사례

- 기업: Honeycomb
- 날짜: 2024-08-06 12:59:39~13:20:05 PDT(사후 보고 2024-08-07 15:42 PDT 게시)
- 링크: [공식 상태 페이지 사고 "UI and API unavailable"과 사후 보고](https://status.honeycomb.io/incidents/ycgvhvs201q1)
- 요약(`docs/ref-real-world-incidents.md` M21 에 이미 있는 행): 스키마 마이그레이션은 기술적으로 안전한 변경이었다("a column addition to the `teams` table using an `INSTANT` algorithm"). 마이그레이션 몇 초 앞서 같은 표를 읽는 질의가 시작됐고 그 느린 SELECT 는 5분 넘게 걸릴 수 있었다. 마이그레이션이 메타데이터 잠금을 얻으려다 그 질의 뒤에서 기다리는 동안 그 잠금 요청이 그 표의 다른 질의를 막아, 약 17분 동안 텔레메트리의 약 4분의 1 을 버리고 API 가 요청의 75% 를 거절했다. 후속: 느린 질의 점검, 클라이언트 쪽 시간 제한과 맞는 DB 쪽 시간 제한.
- 자료 문서에 이미 있어 새로 더하지 않는다. 같은 원본으로 F66-R, F66-P(PostgreSQL), F60-H(MySQL 배차 표)가 있다. 이 후보는 같은 원본 엔진(MySQL)의 다른 표이고, 막히는 곳이 주문 흐름의 마지막 단계(결제)라 실패 지점, 남는 부작용, 대기 세션의 주인이 F60-H 와 다르다(§8).

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | teams 표에 INSTANT 알고리즘으로 열을 더하는 마이그레이션 | payments 에 `ADD COLUMN pg_transaction_id varchar(64) NULL, ALGORITHM=INSTANT` 마이그레이션 |
| 원인이 된 결함 | 몇 초 앞서 시작된 5분 넘는 느린 SELECT 가 같은 표를 쓰는 동안 마이그레이션이 메타데이터 잠금을 기다리고, 그 대기가 뒤의 모든 질의를 막음 | 앱 계정의 내보내기 SELECT(payments 전체, 한 행씩 일정 속도로 받아 약 6분 활성)가 공유 메타데이터 잠금을 쥔 사이 ALTER 가 `Waiting for table metadata lock`, 뒤의 payments 질의 모두 대기 |
| 전파 경로 | 그 표를 쓰는 인증 경로가 막혀 API 요청 대부분이 거절됨 | 결제마다 처음 하는 결제 INSERT 가 막혀 payment Hikari 15개 고갈 → order 의 결제 호출 실패, 결제 서킷 열림 → 모든 새 주문이 결제 단계에서 롤백 502, payment NotReady 와 재시작 |
| 사용자 증상 | API 요청 75% 거절, UI 19분 사용 불가 | 약 6분 동안 음식 주문 전량 502(배차까지 된 뒤 결제 단계). 둘러보기, 배달 조회, 주문 조회는 정상 |
| 탐지된 경로 | 13:00 경 저하를 알아채고 13:04 경보, 배포 직후라 DB 성능과 마이그레이션의 상관을 좁히는 데 몇 분 | lucida 이상 탐지(order 오류 로그와 502, payment 풀 고갈 로그, KCM Unhealthy, DPM 세션 수 상승), 인시던트 |
| 완화와 복구 | 느린 질의가 끝나 마이그레이션이 지나가고 회복 | 내보내기가 스스로 끝나 ALTER 가 즉시 끝나고 줄이 풀림. cleanup 은 그 뒤 더한 열을 즉시 지움 |

기전 확인: 원인(안전한 INSTANT 열 추가가 같은 표의 긴 읽기 뒤에서 메타데이터 잠금을 기다림)에서 증상(그 표를 쓰는 경로 전체가 막혀 서비스 요청 대부분 실패)까지 고리가 원본과 같고, 엔진(MySQL), 알고리즘(INSTANT), 잠금 종류(메타데이터 잠금), DB 쪽 시간 제한 없음까지 같다. 원본의 느린 질의는 제품 질의였고 이 재구성은 분석용 내보내기다(F66-R, F60-H 와 같은 선택). 둘 다 "같은 표를 오래 읽는 정당한 읽기"라는 점이 고리의 요소다.

## 3. 숫자 근거 (2026-10-11, `scenario-stats.py`, 정식 + 후보 합계 95, 이 후보 전)

| 축 | 값 | 판단 |
|---|---|---|
| 서비스 | commerce 32, core-banking 32, food-delivery 31 | 음식배달이 가장 적어 우선(→ 32) |
| 묶음 | A 7, B 7, C 6, D 12, E 4, F 3, G 12, H 2, I 1, J 16(16.8%), K 2, L 8, M 1, O 1, P 11, Q 2, N 0 | 주 묶음 D 12 → 13(96 중 13.5%). 20% 아래 |
| 정답 위치 | 최다 주문 서비스 8(8.4%). DB 테이블(결제) 2(F06-H, F53-P) | 2 → 3(3.1%) |
| 결제 경로 합계 | 12(12.6%) | 13/96(13.5%). 20% 아래 |
| 부품 지도 | food: mysql 12, dispatch 11, order 4, restaurant 4, payment 3, notify 1, kafka 0 | 증상 부품(payment)은 적고, 정답은 결제 표의 잠금 대기열 |

장부와 catalog 대조, 큐 대조는 모두 어긋남 없음(이번 반복에서 장부를 따로 맞출 id 없음).

왜 이 후보인가: 서비스 최소(음식배달), 묶음, 정답 위치, 결제 경로 모두 상한에서 멀다. 겉 증상(주문이 배차까지 된 뒤 결제 단계에서 전량 502)이 F53-P(결제 표가 치워짐), F49-P(payment 릴리스의 응답 형식 변경), 외부 PG 장애(F06-P, F19-S)와 같아 관제 AI 가 증상만 보고 찍을 수 없고, DB 세션 상태와 payment 의 대기, 재시작으로만 갈리는 H 후보다(지시서: 증상이 같고 정답이 다른 후보는 오히려 가치가 크다). 2026-10-08 의 같은 기전 반려(rejected 22행)는 orders, menus, restaurants, dispatches 를 따졌고 payments 는 따지지 않았다. payments 는 orders 와 외래 키가 없어(`food-delivery/db/init.sql:110` 주석) 주문 INSERT 가 줄에 서지 않고, 주문 여정의 메뉴 조회도 막지 않아 그 반려의 입구 중단 벽에 걸리지 않는다(§9).

### 3단계 후보 목록 (실제 기전 × 부품, 숫자 순으로 줄 세움)

| # | 후보 | 원본 | 부품, 정답 위치(현재 수) | 판단 |
|---|---|---|---|---|
| 1 | INSTANT 열 추가가 결제 이력의 느린 내보내기 뒤에서 메타데이터 잠금을 기다려 결제 INSERT 가 모두 줄을 섬 | Honeycomb 2024-08-06 공식 | food MySQL payments, DB 테이블(결제) 2 | **채택(F53-H)**. 음식배달 최소, D 13.5%, 결제 경로 13.5%. 로컬 실측으로 기전 확인(§4) |
| 2 | 같은 기전을 결제 이벤트 outbox(payment_outbox_events) 에 | Honeycomb 2024-08-06 공식 | food MySQL, DB 테이블(결제 이벤트 outbox) 0 | 보류: 표가 약 5.8만 행(10MB)이라 6분짜리 내보내기를 만들려면 초당 약 160행으로 억지로 늦춰야 하고(rejected 301행 carts 와 같은 벽), 'outbox + 이벤트 내보내기' 가 F66-P 와 같은 꼴 |
| 3 | 백엔드가 서버 쪽 prepared statement 를 쌓아 MySQL max_prepared_stmt_count 에 닿아 뒤의 요청 실패 | Slido 2022-04-22 공식 상태 페이지(https://stspg.io/n08ql3v01zsw , 기계 생성 트래픽이 한 끝점을 때려 한도 도달) | food MySQL 인스턴스, DB 인스턴스 2 | 버림(원칙 1): food 서비스는 클라이언트 쪽 prepared statement 라 109 Prepared_stmt_count 0(Com_stmt_prepare = Com_stmt_close 40,714). 재현하려면 문장을 쌓는 릴리스와 트래픽 두 계기가 필요 |
| 4 | 기능 경로가 숨은 기동 의존을 더해 재시작한 인스턴스가 readiness 를 통과 못하고 용량이 깎임 | Inngest 2026-07-16(https://www.inngest.com/blog/2026-07-23-incident-rca-july-2026) | food 배포들, 배달/가게 서비스 | 버림(원칙 9): food Deployment 는 replicas 1, 기본 롤링이라 새 파드가 Ready 가 못 되면 옛 파드가 계속 받아 피해가 없음 |
| 5 | 기능 플래그 제공자 단절로 설정 기본값으로 떨어져 결제 PG 주소가 기본값(localhost)으로 | Inngest 2026-07-10(같은 보고) | food payment 설정, 결제 서비스 4 | 버림(원칙 1, 카탈로그 §1): 원본은 외부 플래그 제공자 단절과 코드 회귀의 복합이고, 남는 꼴(하류 주소 오설정 롤아웃)이 F39-R, F65-R 과 같음 |
| 6 | 마이그레이션이 지운 인덱스를 질의 힌트가 가리켜 질의 실패 | GitHub 2026-08 상태 페이지(인덱스 힌트가 최근 마이그레이션이 지운 인덱스를 가리킴) | food dispatch 질의, DB 테이블(배차) 4 | 버림(원칙 1): food 코드에 인덱스 힌트가 없어 힌트를 넣는 릴리스와 인덱스를 지우는 마이그레이션 두 변경이 필요(지휘 세션 공통 앞머리의 '스키마-코드 순서' 보류 꼴) |
| 7 | 자동 증가 키가 정수 최대값에 닿아 발급 중단 | Let's Encrypt 2019-11-17 공식 커뮤니티 공지 | food 표 id, DB 테이블 | 버림(원칙 1, 9): food 의 id 가 모두 BIGINT |
| 8 | 관리형 PG 마이너 업그레이드 뒤 연결 재활용이 동기화되어 15분마다 몰림 | Clerk 2025-09-14~18 공식 사후 보고(https://clerk.com/blog/2025-09-18-database-incident-postmortem) | commerce PG 연결 풀, DB 인스턴스 2 | 버림(원칙 1, 2): DB 엔진 동작 변경(업그레이드)이 계기라 재현 불가, commerce 는 최다 동률인데 DB 인스턴스는 0 이 아님 |
| 9 | DNS 자동화가 서비스 끝점 레코드를 비워 이름 해석 실패 | AWS 2025-10-19~20 공식(자료 문서 M16) | food PG mock Service 삭제, DNS 0 | 버림(원칙 4): Service 삭제는 KCM 이벤트, VM 지표 어디에도 남지 않고(kcm_events_local 은 Pod/Deployment/ReplicaSet 만, kube-system 미수집) 계기 흔적이 없음 |
| 10 | 배포가 API 의 DB 사용자를 바꿔 풀러 파일 기술자 한도 초과 | Adapty 2025-12-05 공식 상태 페이지(https://status.adapty.io/incidents/xffp232rjrg2) | food 서비스 DB 접속, DB 계정 3 | 버림(원칙 1): 테스트베드에 DB 앞 연결 풀러가 없음 |
| 11 | 운영자의 직접 SQL 이 앱이 엄격한 타입으로 읽는 칸에 다른 값을 써 읽기 실패 | Clerk 2025-02-06 공식 | commerce user 주소 표, DB 테이블(주소) 0 | 버림(원칙 1): commerce addresses.is_default 가 NOT NULL 이라 NULL 을 못 쓰고 checkout 경로도 아님(rejected 110행 벽 재확인) |

서로 다른 부품: payments 표, 결제 outbox 표, MySQL 인스턴스, food 배포, payment 설정, dispatch 질의, 표 id, commerce PG 풀, PG mock Service, DB 계정, commerce 주소 표(4개 이상).

## 4. 인과 사슬과 코드, 인프라 위치

1. 내보내기: 앱 계정(`fooddelivery@localhost`) 세션이 `SELECT id, order_id, pg_provider, amount, status, processed_at, settled_at, created_at FROM payments` 를 `mysql --quick`(한 행씩 받기)로 실행하고 받는 쪽이 N 행마다 1초 쉰다. 서버 세션 상태 `Sending to client`, payments 의 SHARED_READ 메타데이터 잠금 GRANTED.
2. 마이그레이션: root 세션의 `ALTER TABLE payments ADD COLUMN pg_transaction_id varchar(64) NULL, ALGORITHM=INSTANT` 가 EXCLUSIVE PENDING, 상태 `Waiting for table metadata lock`.
3. 뒤의 질의: payments 의 INSERT, SELECT 가 모두 `Waiting for table metadata lock`.
   - `food-delivery/payment-service/src/main/java/com/fooddelivery/payment/service/PaymentService.java:50-66`(processPayment: @Transactional 안에서 PG 호출 전에 PENDING 행 INSERT, 66)
   - `PaymentService.java:68-83`(PG 호출과 결과 저장: 줄이 풀리기 전에는 닿지 않음)
   - `PaymentRetentionService.java:52-64`(6초마다 보존 정리), `SettlementBatch.java:36-62`(정산 배치)
4. payment 풀: `food-delivery/payment-service/src/main/resources/application.yml:12-15`(maximum-pool-size 15, connection-timeout 3000). 기다리는 INSERT 가 연결을 쥐어 뒤의 요청은 `HikariPool-1 - Connection is not available, request timed out after 3000ms` 500.
5. order: `food-delivery/order-service/src/main/java/com/fooddelivery/order/service/OrderService.java:152-160`(주문 저장, 배차 요청 뒤 마지막 동기 단계로 결제 호출, 실패하면 주문 트랜잭션 롤백), `order/client/PaymentClient.java:30-59`(서킷, 재시도 2회, 실패와 fallback 502), `order-service/src/main/resources/application.yml:59-62`(결제 read-timeout 10초). order 자신의 표(orders, order_items, order_outbox_events)와 dispatches 는 payments 와 외래 키가 없어(`food-delivery/db/init.sql:110` 주석, `:154-165` 정의) 줄에 서지 않는다.
6. payment 재시작: `food-delivery/k8s/23-payment-deploy.yaml:67-86`(모든 프로브 /actuator/health, readiness 10초 × 3, liveness 15초 × 5). 죽은 파드의 서버 쪽 대기 세션은 잠금이 풀릴 때까지 남는다(rejected 22행의 로컬 실측과 같음).
7. 109 실측(2026-10-11, 읽기 전용, 앱 계정): payments 행 추정 1,726,617(데이터 298MB, 인덱스 99MB), 열 8개(pg_transaction_id 없음), lock_wait_timeout 31536000, max_execution_time 0, net_write_timeout 60, max_connections 151, 열린 세션 19, mysql 8.0.46. information_schema.innodb_tables 는 앱 계정 권한 밖(PROCESS 필요)이라 total_row_versions 는 preflight 가 root 로 확인한다(이 표에 INSTANT 변경 이력 없음: 열이 init.sql 정의 그대로 8개).
8. 로컬 실측(2026-10-11, mysql:8.0 = 8.0.46, cpus 1, 1GiB, init.sql 의 payments 정의 그대로 20만 행): 초당 5,000행으로 받는 `--quick` 내보내기가 `Sending to client` 인 동안 INSTANT 열 추가가 `Waiting for table metadata lock`. 그때 payments INSERT 와 id 단건 SELECT 탐침(lock_wait_timeout 3초)이 모두 3.01초에 1205, 다른 표 INSERT 는 0.016초. 내보내기가 끝나자(약 40초) 열 추가가 바로 끝났고 그 뒤 payments INSERT 와 열 지우기(INSTANT)가 0.036초, total_row_versions 2.

## 5. 원인 규정

- 근본(`root_cause.target_id`): `food-mysql:fooddelivery.payments` — 이 표에 대한 스키마 변경이 같은 표의 긴 읽기와 겹쳐 생긴 메타데이터 잠금 대기열. 원본 Honeycomb 의 사후 보고도 "teams 표의 마이그레이션이 느린 SELECT 뒤에서 잠금을 기다리며 그 표의 질의를 막음" 을 원인으로 적는다(같은 층위).
- 계기(`trigger_target_id`): 비움. 근본과 계기가 같은 곳(이 표의 스키마 변경과 긴 읽기)이다. F60-H, F66-R 과 같은 규정.
- 부분 점수(`scoring.partial`): `food-delivery-payment`, `food-payment`(증상 서비스), `food-mysql`(인스턴스까지만 지목). F53-P 와 같은 꼴.
- 원칙 5: "DB 안에서 결제 표에 DDL 이 긴 읽기 뒤에 서서 그 표의 질의가 모두 기다림" 은 DPM 세션 상태만으로 내릴 수 있는 결론이다. 무슨 열을 더했는지, 내보내기가 무엇이었는지는 요구하지 않는다.
- 원칙 6: payment, order 의 코드, 설정, 요청량은 그대로라 정당하다. 내보내기 자체도 정당한 읽기이고, 잘못은 그 표를 쓰는 긴 질의가 도는 중에 배타 잠금이 필요한 DDL 을 시간 제한 없이 낸 것이다(원본 재발 방지: DB 쪽 시간 제한).

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 |
|---|---|
| 감지(lucida) | order ERROR 로그 급증('Failed to process payment for order', 처음엔 Read timed out, 그 뒤엔 500), order POST /api/orders 502, payment 'Connection is not available' 급증과 'Processed payment' 멈춤, KCM testbed-payment Unhealthy, Killing, DPM active_session 상승 |
| 피해 판정(러너) | 기준선 loadgen-food 의 주문 생성 5xx 비율 ≥ 0.5, 2xx 비율 < 0.2 (3틱, 판정 관측 갱신 30초) |
| RCA 증거 | DPM 세션 스냅숏의 fooddelivery@localhost 'Sending to client'(queryTime 이 분마다 늘어 약 360초), root@localhost 와 payment 파드 IP 의 'Waiting for table metadata lock', 롤아웃 없음, 1146 없음, 'PG /pay failed' 없음, 표·인덱스 수 불변 |

## 7. 관측 근거 (119 실조회, 2026-10-11)

| 증거 | 표, 칸 | 조회와 결과(평시) | 표본 여부 |
|---|---|---|---|
| 계기: 긴 내보내기 세션 | CH `lucida.dpm_session_local`(body JSON `user`, `hostname`, `state`, `queryTime`, `dbName`, `command`) | `SELECT JSONExtractString(body,'state') st, count(), max(JSONExtractFloat(body,'queryTime')), max(timestamp) FROM lucida.dpm_session_local WHERE engine='mysql' AND timestamp > now() - INTERVAL 60 DAY AND JSONExtractString(body,'state') IN ('Sending to client','Waiting for table metadata lock','executing','Sending data') GROUP BY 1` → executing 113,625, Waiting for table metadata lock 8, **'Sending to client' 0건**. 스냅숏은 1분마다(최근 1시간 `count(DISTINCT timestamp)` 59, 177행, 명령은 Daemon 과 Query 만: Sleep 세션은 남기지 않음) | 전수(DPM 1분 폴링) |
| 계기, 근본: 메타데이터 잠금 대기 | 위 표 state, user, hostname | 'Waiting for table metadata lock' 60일 8건, 모두 2026-10-09 03:43 한 스냅숏(예: `{"user":"fooddelivery","hostname":"10.244.2.82","state":"Waiting for table metadata lock","queryTime":1889.38,...}`, root@localhost 1). 파드 IP 와 localhost 세션 모두 기록된다. 장애 시 root@localhost(ALTER)와 payment 파드 IP(user fooddelivery)의 세션이 분마다 쌓인다 | 전수 |
| 전파: 세션 수 | VM `dpm.mysql.session.active_session`{target_id="c8c558e5-711f-4e6e-b00f-1adeb097c795"} | 7일 중앙값 1, 최대 12. 장애 시 대기 세션 수만큼 오른다 | 전수 |
| 감별: 복사·스캔 없음, 표 그대로 | VM `dpm.mysql.instance.data_file_write_bytes`, `dpm.mysql.database.table_count`, `index_count` | F60-H, F60-R 시트 실측(쓰기 7일 중앙 0.41MB/s, 표 16, 인덱스 31 고정). INSTANT 열 추가는 셋 다 바꾸지 않는다. 내보내기 읽기는 298MB 를 6분에 나눠 약 0.8MB/s | 전수 |
| 전파: order 결제 실패 | CH `lucida.lucida_logs_local` `service_name='food-delivery-order' AND body LIKE '%payment%'` | 7일 동안 'Failed to process payment for order N: 400/500 ...' 과 'Order fan-out payment failed: orderId=... status=...' 가 다른 시나리오 창에서만 몇 건. 장애 시 주문마다 오른다 | 전수 |
| 전파: payment 풀 고갈 | 위 표 `service_name='food-delivery-payment' AND body LIKE '%Connection is not available%'` | 7일 10건(다른 시나리오 창, 'HikariPool-1 - Connection is not available, request timed out after 3000ms (total=0, ...)') | 전수 |
| 피해: 결제, 주문 로그 | 위 표 'Processed payment id=%'(payment), 'Created order id=%'(order) | 최근 1시간 각 1,140건, 969건(초당 약 0.3건). 장애 시 'Processed payment' 는 멈추고 'Created order' 는 이어진다(주문 저장 뒤 결제 단계에서 롤백) | 전수 |
| 전파: payment 재시작 | CH `lucida.kcm_events_local` `namespace='rca-testbed-food' AND object_name LIKE 'testbed-payment%'` | 7일 Unhealthy 79, Killing 14, ScalingReplicaSet 27(모두 시나리오 실행 창). 장애 시 Unhealthy, Killing 이 ScalingReplicaSet 없이 나온다 | 전수 |
| 보조: 주문 502, 결제 스팬 | CH `lucida.otel_traces_local` | 장애 시 order POST /api/orders 502, payment POST /api/payments 긴 지속 시간 | **10% 표본, 보조로만** |

핵심 증거(내보내기와 DDL 세션 상태, 메타데이터 잠금 대기 세션, 세션 수, order 와 payment 로그, KCM 이벤트)는 모두 전수 수집이다. 트레이스와 APM 지표는 보조로만 쓴다. 일회성 DDL 문장은 DPM Top SQL 에 남지 않는다(F60-H 와 같음). 장애 시 형태는 첫 실행에서 다시 확인한다.

## 8. 감별

- must_support: 'Sending to client' 의 긴 앱 계정 세션과 payment 파드 IP 의 메타데이터 잠금 대기 세션, active_session 상승과 데이터 파일 쓰기·표·인덱스 수 불변, order 'Created order' 는 이어지고 'Failed to process payment' 뒤 502, payment 'Processed payment' 멈춤과 풀 고갈, KCM testbed-payment Unhealthy, Killing(롤아웃 없음), 둘러보기와 배달 조회 정상, 다른 표 쓰기 정상, 내보내기가 사라지는 분에 회복.
- must_rule_out: F53-P(1146, 표 수 감소, payment 는 Ready 로 곧바로 500), payment 릴리스나 설정 배포(F49-P, F30-R: 롤아웃), 외부 PG 장애(F06-P, F19-S: 'PG /pay failed'), F60-H(배차 표 대기열: 용량 확인 단계 503), MySQL 다운·읽기 전용(다른 표 쓰기 성공).
- contrast_with: F53-P, F60-H, F49-P, F66-R(정답지 related_scenarios 에 차이를 적음).
- 관계: F53-P 와 겉 증상(주문이 배차까지 된 뒤 결제 단계에서 전량 502, 정답 표 payments)이 같고 원인이 다르다(H). 가르는 근거: 1146 과 표 수 16 → 15 대 'Sending to client' 앱 계정 읽기와 'Waiting for table metadata lock', payment 가 Ready 인 채 곧바로 500 대 기다리다 풀이 차고 재시작.
- F60-H 와는 원인(같은 원본, 같은 주입 모드)이 같고 대상 표가 다르다. 카탈로그 §1 의 R 조건(진입 경로, 영향 범위 중 하나 이상 다름)을 넘는다: 막히는 서비스가 dispatch 가 아니라 payment, 실패가 주문 저장 전 용량 확인 503 이 아니라 주문 저장과 배차 뒤 결제 단계 502, 부작용(실패 주문마다 배차가 남음, 줄이 풀릴 때 살아 있던 결제가 롤백된 주문에 대해 승인됨)이 다르다. 정답지는 이 차이를 must_rule_out 과 contrast_with 에 적는다.

## 9. 러너 판정과 강도, 부하 계산

- 강도 축이 없다: 잠금 대기열 길이는 내보내기 길이(export_seconds 360)로 정해진다. 설계 강도 하나로 고정한다(F60-H 와 같은 값).
- 내보내기 속도: run 이 행 추정(1,726,617)을 360 으로 나눠 약 4,797 행마다 1초 쉰다. 행 추정 ≥ 100 × 360 이 아니면 시작하지 않는다. 받는 쪽이 쉬는 1초는 net_write_timeout(60초)에 걸리지 않는다(F60-H 로컬 실측과 같음).
- 시간표(기준선 주문 r ≈ 0.2~1.15건/초, 2026-10-11 17시 UTC 약 0.3건/초): 0초 ALTER 대기 시작 → 결제 INSERT, 보존 정리(6초), 정산 배치가 줄에 섬. 주문은 가게, 메뉴, 용량 확인, 주문 저장, 배차를 평시대로 지나 결제 호출에서 10초 시간 초과(2회, 300ms 간격). order 의 결제 서킷은 10건 창, 최소 5건, 50% 라 시간 초과 5건이 쌓이면(r 에 따라 약 10~25초) 열리고 그 뒤 주문은 곧바로 502. 반열림(5초 뒤 3건)마다 결제 요청이 다시 들어가 payment 풀(15)은 약 30~60초 안에 찬다. 그 뒤 결제 요청은 연결 대기 3초 끝 500, payment NotReady(약 30초) 뒤엔 연결 거부로 곧바로 502. 판정 관측 둘은 30초마다 갱신되므로 min_hold 90s + 3틱이면 판정은 약 3분에 끝나 잠금 대기열(약 6분) 안에 든다. timeout 6m.
- order 풀(15) 압박: 서킷이 열리기 전 십여 초 동안 주문 트랜잭션이 결제 응답을 최대 10초씩 기다리며 연결을 쥔다(동시 약 r × 10초 ≈ 2~12, 서킷 열림 뒤엔 곧 풀림). F60-H(r × 16초 ≈ 3~18)보다 짧다. order readiness 는 10초 × 3번 연속 실패가 필요해 빠지지 않는다고 본다. 그래서 동반 부하를 두지 않는다. must_rule_out 의 order NotReady 가 이를 지킨다.
- 동반 부하 없음의 둘째 이유: 결제는 배차 뒤 단계라 실패한 주문마다 ASSIGNED 배차가 ETA(15~35분) 동안 남는다. 기준선만이면 평시 성공 주문이 남기던 만큼과 같은 속도라 배정 수준이 그대로이고, 주문 동반 부하를 더하면 그만큼 배차 용량(2000)이 차서 F32-R 증상이 섞이고 cleanup 뒤에도 남는다(F49-P, F53-P 와 같은 이유).
- 입구 중단 조건(abort entry_status==0): 주문 여정은 restaurant 메뉴 조회 뒤 order 로 가고, order 표와 dispatches 는 payments 와 외래 키가 없어 줄에 서지 않는다(로컬 실측: 다른 표 INSERT 0.016초). entry_status 는 order 파드가 빠질 때만 0 이다.
- MySQL 세션 상한: payment 파드 한 생애에 서버 쪽 대기 세션이 최대 약 15개(풀 크기) 남는다. 풀이 찬 뒤 readiness(약 30초), liveness 재시작(약 75초) + 기동(약 40~60초)이라 6분에 두세 번 재시작, 약 30~45개. preflight 가 열린 세션 90 이하(지금 19)를 요구하므로 최악 약 137 으로 max_connections 151 아래.
- 줄이 풀릴 때: 살아 있는 payment 파드의 대기 INSERT(최대 약 15건)는 그대로 진행되어 PG 를 부르고 APPROVED 로 커밋된다. 그 주문은 이미 롤백돼 주문 없는 승인 결제가 남는다(F49-P 의 부작용과 같은 종류, PG 는 클러스터 안 mock 이라 실제 돈은 움직이지 않음). 죽은 파드의 대기 세션은 클라이언트가 없어 롤백된다.
- InnoDB INSTANT 행 버전: 표당 64, 실행 한 번(열 추가 + cleanup 열 지우기)에 2(로컬 실측 2). preflight 가 60 이하를 요구한다.
- cleanup: F60-H 와 같다. 내보내기가 스스로 끝나기를 최대 300초 기다리고 넘으면 KILL QUERY 로 내보내기와 남은 마이그레이션을 끝낸 뒤 열을 INSTANT 로 지운다(lock_wait_timeout 5초, 5번까지). 안전 중단 뒤에도 내보내기가 남아 있으면 장애가 최대 300초 더 이어진다(데이터를 바꾸지 않고 스스로 끝난다).
- 재시작 고리에 있던 payment 는 CrashLoopBackOff 지연이 남을 수 있어 recovery timeout 10m.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Honeycomb, 2024-08-06, 공식 상태 페이지 사후 보고)와 요소별 대응표, 기전 동일(엔진, INSTANT, 메타데이터 잠금까지 같음)(§2).
- [x] 분류 장부를 갱신했고 묶음 D(12→13, 13.5%), 정답 위치 DB 테이블(결제)(2→3), 결제 경로 13/96(13.5%), 서비스(음식배달 31→32), 이 후보를 고른 이유를 적었다. 어느 축도 20% 미만(§3).
- [x] 근본 원인 위치: food MySQL fooddelivery.payments(`food-delivery/db/init.sql:154-165`), 막히는 질의 `PaymentService.java:50-66`, `PaymentRetentionService.java:52-64`, `SettlementBatch.java:36-62`, 109 실측 값(§4).
- [x] 근본 원인 흔적이 119 에 조회됐다: dpm_session_local 의 state(평시 'Sending to client' 0건, 메타데이터 잠금 대기 한 스냅숏), 파드 IP 와 localhost 세션 기록 가능(§7).
- [x] 핵심 증거가 표본 데이터에만 있지 않다: DPM 세션, 로그, KCM 이 전수다(§7).
- [x] 계기 흔적: 내보내기 세션과 DDL 세션이 분마다 각 상태로 남는다. 인공 지연(SLEEP)이 아니라 받는 쪽이 천천히 받는 실제 읽기와 현실의 스키마 변경 문장이다(원칙 4).
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다: "결제 표에 대한 DDL 이 긴 읽기 뒤에 서서 그 표의 질의가 모두 기다림"(§5).
- [x] 정답지 세 칸이 원칙 6대로다(§5).
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다(§6).
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: 문장, 열 이름(pg_transaction_id), 파드 안 파일(/tmp/table-export.sh, /tmp/table-export.log, /tmp/schema-migration.log, cleanup 이 지움)에 시나리오 id 가 없고 테스트(`test_mysql_metadata_lock_queue_on_the_payments_table_reuses_the_f60h_path`)로 고정했다. 내보내기는 앱 계정, 마이그레이션은 root@localhost 다.
- [x] 부하 상한과 서킷브레이커를 고려한 피해 계산(§9). 강도는 한 단, 동반 부하 없음.

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- dpm_session_local 에 'Sending to client'(fooddelivery@localhost)와 payment 파드 IP 의 'Waiting for table metadata lock' 이 실제로 분마다 담기는지.
- order 가 Ready 를 지키는지(must_rule_out), MySQL 현재 세션 최대값(151 아래).
- 내보내기가 약 360초 이어지는지(행 추정과 실제 행 수 차이).
- 줄이 풀릴 때 나오는 주문 없는 APPROVED 결제 수(최대 약 15).
- cleanup 뒤 pg_transaction_id 열 없음, total_row_versions 2 증가, /tmp 파일 없음, payment 회복.
