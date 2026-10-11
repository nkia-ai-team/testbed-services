---
title: F67-R 설계 시트 (운영자의 VACUUM FULL 이 commerce 결제 감사 로그를 다시 쓰는 내내 잠가 결제가 멈춤)
status: Draft
owner: project
last_reviewed: 2026-10-11
tags:
  - scenario
  - design
  - database
summary: 운영자가 commerce PostgreSQL 의 디스크 공간을 되찾으려고 가장 큰 두 표(결제 감사 로그 payment_logs, 배송 이벤트 로그 shipment_events)에 VACUUM (FULL) 을 트래픽 중에 나란히 돌린다. 두 표는 읽는 질의가 없어 차가운 로그로 보였지만 payment-service 는 결제마다 같은 트랜잭션에서 payment_logs 에 쓴다. VACUUM FULL 이 다시 쓰는 내내 ACCESS EXCLUSIVE 를 쥐어 결제가 로그 INSERT 에서 멈추고, payment 풀이 차고 order 의 결제 호출이 시간 초과 끝에 서킷 502 가 되어 checkout 이 다시 쓰기가 끝날 때까지 실패한다. 원본은 Coveralls 2025-04-06(쓰이지 않는다고 여긴 큰 표들의 VACUUM FULL 을 업무 시간까지 돌게 두었는데 백그라운드 작업이 그 표를 간접적으로 건드려 표 잠금 뒤에 질의가 쌓이고 연결을 다 써 사이트 500).
---

# F67-R 설계 시트

## 1. 요약

commerce PostgreSQL 은 11GB 로 자랐다. 운영자가 공간을 되찾으려고 `pg_stat_user_tables` 를 보니 가장 큰 두 표 `payment_schema.payment_logs`(약 1,865만 행, 인덱스 포함 2.6GB)와 `shipping_schema.shipment_events`(약 1,411만 행, 1.5GB)는 읽은 적이 없다(seq_scan 0, idx_scan 0 근처). 쌓이기만 하는 차가운 로그로 보고 둘에 `VACUUM (FULL)` 을 나란히 돌린다. 트래픽 중이다.

VACUUM FULL 은 표를 새 파일로 복사하고 인덱스를 다시 만드는 내내 그 표의 ACCESS EXCLUSIVE 잠금을 쥔다. 두 다시 쓰기가 DB 의 0.5 CPU 와 디스크를 나눠 써 payment_logs 는 약 3분(로컬 197초, 109 추정 170~200초) 걸린다. 그런데 payment-service 는 결제마다 같은 트랜잭션 안에서 결제 행을 넣은 바로 뒤, 외부 PG 를 부르기 전에 `payment_logs` 에 로그 행을 쓴다. 이 INSERT 가 잠금 시간 제한 없이 연결을 쥔 채 기다린다. payment 풀(10)이 차고, order 의 결제 호출은 15초 read-timeout 과 재시도 끝에 서킷이 열려 502 를 낸다. checkout 이 실패하고 예약 재고가 풀린다. payment 의 health 에 DB 확인이 있어 readiness 가 빠지고 재시작한다. 상품 둘러보기, 장바구니, 로그인, 결제 표(`payments`) 자체는 멀쩡하다. `payment_logs` 다시 쓰기가 끝나면 줄 선 INSERT 가 한꺼번에 지나가고 checkout 이 스스로 돌아온다.

비유: 창고 정리 담당이 "아무도 안 꺼내 보는 상자"라며 영수증 보관함(payment_logs)과 배송 일지(shipment_events)를 통째로 새 상자로 옮겨 담기 시작했다. 옮기는 동안 상자 뚜껑은 닫혀 있다. 그런데 계산대 직원은 결제할 때마다 영수증 한 장을 그 상자에 넣어야 계산을 끝낼 수 있다. 꺼내 보는 사람은 없어도 넣는 사람은 있었다. 계산대 줄(결제)이 상자 앞에서 멈추고, 손님(checkout)은 기다리다 돌아간다. 고칠 곳은 계산대가 아니라 업무 중에 뚜껑을 닫은 상자 정리(그 표의 VACUUM FULL)다.

## 2. 원본 사례

- **Coveralls, 2025-04-06** (공식 상태 페이지 사고와 사후 보고, 사후 보고 게시 2025-04-11 3:16 PM PDT): https://status.coveralls.io/incidents/ph6p14vg1fyr
  - 예정된 유지보수 창에서 레거시 파티션을 정리해 약 20TB 를 되찾고, "bloat-heavy legacy tables" 에 VACUUM FULL 9개를 병렬로 돌렸다. 창이 끝날 때 3개가 남았는데, 1~2시간 안에 끝나리라 보고 위험이 작다고 판단해 취소하지 않고("Rather than risk losing the disk space gains by canceling mid-operation, we opted to let them finish") 트래픽을 다시 열었다.
  - 그 표들은 "no longer accessed by active jobs or user queries" 라고 여겼지만 "Background reconciliation jobs still touched those locked tables indirectly". 질의가 "quickly piled up behind table-level locks, consuming all available connections", RDS 활성 연결 1,400개 넘게(대부분 막힌 트랜잭션), 약 10:45 PM PDT 부터 사이트 500, DB 가 "Out of connections: Connections reserved for superuser" 로 새 연결 거절. 커버리지 업로드, 보고서 생성, 대시보드, 알림이 영향을 받음.
  - 탐지 "Alerts indicated multiple servers were down". 완화: 유지보수 모드, 백그라운드 작업과 막힌 시스템 질의를 끊고 남은 VACUUM FULL 을 취소하자 밀린 것이 풀림, 약 1:15 AM 복구. 직접 원인으로 창 뒤에도 VACUUM FULL 3개를 돌게 둔 결정을 든다(바탕은 약 6주 동안의 정기 유지보수 저하). 교훈 "`VACUUM FULL` must never run in production again."
- 자료 문서 M22(운영 명령이나 스크립트 실수)에 이번에 추가했다(출처가 말한 사실만, 웹 조회 2026-10-11).
- 막힌 목록 210행(2026-10-10, 업무 시간 VACUUM FULL, GitLab 2016-11-28)은 "원본에서 VACUUM FULL 은 원인이 아니라 조치, 원인으로 든 공식 사후 보고 없음" 으로 버렸다. 이 원본이 그 사유를 푼다.

### 요소별 대응표

| 요소 | 원본 사례 (Coveralls 2025-04-06) | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 공간을 되찾으려는 VACUUM FULL 여러 개(병렬 9, 남은 3)를 트래픽을 연 뒤에도 돌게 둠 | 공간을 되찾으려는 VACUUM (FULL) 둘(가장 큰 두 표)을 트래픽 중에 나란히 돌림 |
| 원인이 된 결함 | 대상 표가 더는 쓰이지 않는다는 잘못된 가정. VACUUM FULL 이 표 잠금을 오래 쥠 | 두 표가 읽히지 않는다(스캔 0)는 이유로 차가운 표로 본 잘못된 가정. 실제로는 결제마다 payment_logs 에 쓰고 shipping 소비자가 shipment_events 에 씀. VACUUM FULL 이 다시 쓰는 내내 ACCESS EXCLUSIVE |
| 전파 경로 | 그 표를 간접적으로 건드리는 백그라운드 작업의 질의가 표 잠금 뒤에 쌓여 연결을 다 씀 | 결제 트랜잭션의 로그 INSERT 가 표 잠금 뒤에 쌓여 payment 풀(10)과 order 의 checkout 트랜잭션 연결을 다 씀 → order 결제 호출 시간 초과, 서킷 open |
| 사용자 증상 | 사이트 500, 업로드, 보고서, 대시보드, 알림 실패 | checkout 5xx(처음 약 15초 뒤 게이트웨이 시간 초과, 서킷이 열린 동안 곧바로 502 나 503). 둘러보기, 장바구니, 로그인은 정상. 배송 이벤트 지연 |
| 원본의 탐지 경로 | 서버 다운 경보 | lucida-next 의 order 오류율과 오류 로그, DPM 세션 잠금 대기(relNames payment_logs)와 오래 도는 psql 세션, DPM 체크포인트와 임시 파일 지표, KCM testbed-payment Unhealthy |
| 완화와 복구 | 남은 VACUUM FULL 취소와 막힌 질의 정리로 회복 | payment_logs 다시 쓰기가 끝나 스스로 회복. cleanup 은 끝나기를 기다리고 남으면 취소(옛 사본 유지) |

기전 일치: 원인("쓰이지 않는다고 여긴 표를 VACUUM FULL 이 오래 잠그는데 실제로는 그 표를 건드리는 경로가 있었다")에서 증상("그 경로의 질의가 표 잠금 뒤에 쌓여 연결을 다 쓰고 서비스가 오류를 냄")까지 고리가 같다.

바꾼 것(기전 고리 밖):
- 규모: 원본은 65TB 인스턴스와 표마다 수 TB, 테스트베드는 11GB DB 와 2.6GB, 1.5GB 표. 원본의 몇 시간이 몇 분이 된다. 원본처럼 여러 표를 나란히 돌려 DB 자원을 나눠 쓰게 해 다시 쓰기가 혼자일 때보다 길다(로컬 109초 → 197초, §4).
- 연결이 마르는 곳: 원본은 DB 의 max_connections(1,400개 넘게 쌓임). 테스트베드는 앱 풀(payment 10)과 order 의 checkout 트랜잭션이 먼저 마른다. DB 연결 한도까지 가지 않게 시작 조건(열린 세션 70 이하)을 둔다(§9). 고리("표 잠금 뒤 질의가 쌓여 연결을 다 씀")는 같다.
- 간접 경로: 원본은 백그라운드 대사 작업, 재구성은 결제 트랜잭션의 감사 로그 쓰기(사용자 경로)와 shipping 소비자(백그라운드). 둘 다 "읽지 않지만 쓰는" 경로라 스캔 수만 보면 차가워 보인다.

## 3. 숫자 근거 (2026-10-11, `scenario-stats.py`, 정식 47 + 후보 46 = 93, 이 후보 전)

- 묶음(정식 + 후보): J 15(16.1%), G 12(12.9%), D 11, P 11(각 11.8%), L 8, A 7, B 7, C 6, E 4, F 3, H 2, K 2, Q 2, I 1, M 1, O 1, N 0. 금지(20%, 19 이상) 없음. 이 후보 D 11 → 12(94 중 12.8%).
- 정답 위치: 주문 서비스 8(8.6%)이 가장 크고, **DB 테이블(결제 로그) 0 → 1**. 결제 경로(외부 결제 의존 6 + 결제 서비스 4 + DB 테이블(결제) 2) 12(12.9%). `scenario-stats.py` 는 DB 테이블(결제) 이름만 결제 경로로 세지만, 보수적으로 결제 로그까지 넣어도 13/94(13.8%)라 상한 아래다.
- 서비스: 쇼핑몰 31, 은행 31, 음식배달 31(동률). 세 도메인이 모두 최다 동률이라 원칙 2 완화 규칙(최다 서비스는 정답 위치가 0 인 부품만)을 따라, 어느 도메인이든 정답 위치 0 인 부품만 냈다. 결제 로그 표는 0 이다. 쇼핑몰 31 → 32.
- 부품 지도: commerce postgres 10(표마다 정답 위치가 갈림: 재고 3, 결제 2, 인증 토큰 1, 상품 1, 재고 이동 이력 1, 사용자 1, 재고 이벤트 outbox 1. 결제 로그 0). 0 인 부품(kafka, notification, nginx)은 막힌 목록의 벽(outbox 비동기라 사용자 증상 없음, nginx OTel 없음)에 걸린다.
- 왜 이 후보인가: 세 도메인 동률이라 정답 위치 0 이 먼저다. 기전(유지보수 명령이 살아 있는 쓰기 표를 오래 잠금)이 테스트베드에 처음이고 원본이 공식 사후 보고이며, 막힌 목록 210행의 사유(원본 없음)를 이 원본이 푼다.
- 겉 증상(결제가 DB 표 잠금에서 멈춰 checkout 502, payment 재시작)은 F06-H 와 같다. 2026-10-09 사용자 정정대로 증상 겹침은 버릴 사유가 아니라 판별력을 더한다: F06-H 는 payments 를 EXCLUSIVE 로 쥔 채 노는 JDBC 트랜잭션, F67-R 은 payment_logs 를 다시 쓰는 활성 psql 세션(디스크 쓰기, 임시 파일, 요청 체크포인트 동반)이고 payments 에는 대기가 없다(§8).

### 후보 목록 (3단계, "실제 기전 × 부품", 숫자 순)

| # | 후보 | 층, 부품 | 원본 사례 | 결과 |
|---|---|---|---|---|
| 1 | 운영자가 공간을 되찾으려고 commerce 가장 큰 두 표(payment_logs, shipment_events)에 VACUUM FULL 을 나란히 돌려 결제 로그 INSERT 가 멈춤, checkout 502 | DB, commerce payment_logs(D, DB 테이블(결제 로그) 0) | Coveralls 2025-04-06 공식 | **채택** |
| 2 | 같은 VACUUM FULL 을 commerce order_items(908MB, 쓰기 전용) 하나에 | DB, commerce order_items(D, DB 테이블(주문 품목) 0) | Coveralls 2025-04-06 | 버림: 원칙 9. 로컬 같은 크기 사본(0.5 CPU) 다시 쓰기 34초라 판정 창(min_hold + 3틱)을 채우지 못한다. checkout 이 결제 전에 막혀 order 단 증상만 남는 점도 원본의 '간접 경로' 고리와 덜 맞는다 |
| 3 | 같은 VACUUM FULL 을 부풀어 오른 outbox 표(shipping 633MB / 12만 행, inventory 429MB / 7만 행)에 | DB, commerce outbox 표(D) | Coveralls 2025-04-06 | 버림: 원칙 9. 다시 쓰기는 살아 있는 행만 복사해 몇 초에 끝난다(공간 회수 동기는 가장 그럴듯하지만 잠금이 짧음). inventory outbox 는 정답 위치 1(F66-P) |
| 4 | 같은 VACUUM FULL 을 shipment_events 하나에 | DB, commerce shipment_events(D) | Coveralls 2025-04-06 | 버림: 원칙 7. shipping Kafka 소비자만 쓰는 표라 배송 이벤트만 늦고 사용자 증상이 없다(채택안에서는 함께 도는 두 번째 정리로만 쓴다) |
| 5 | food MySQL 큰 표(dispatches, orders)에 OPTIMIZE TABLE | DB, food MySQL(D) | Coveralls 2025-04-06 | 버림: 원칙 1. MySQL 8 InnoDB 의 OPTIMIZE 는 온라인 DDL(INPLACE)로 처음과 끝에만 잠깐 잠그고 다시 만드는 동안 쓰기를 받아, '다시 쓰는 내내 표 잠금' 고리가 원본과 다르다 |
| 6 | banking Oracle 큰 표에 ALTER TABLE MOVE(오프라인 재구성) | DB, banking TRANSFERS / LEDGER_ENTRIES(D) | Coveralls 2025-04-06 | 버림: 원칙 2, 7. TRANSFERS 는 정답 위치 DB 테이블(이체) 2 인데 세 도메인 동률이라 0 위치만 낸다. LEDGER_ENTRIES 는 원장 Kafka 소비자만 써 조용한 장애(막힌 목록 242행과 같은 벽) |
| 7 | commerce user 토큰 유효 시간 설정 오배포(auth.token-ttl-hours) | 앱 설정, commerce user(G, 사용자 서비스 1) | (공식 사례 못 찾음) | 버림: 원칙 1, 9. 토큰 수명 설정이 원인인 공식 사후 보고 없음(웹 검색 1회), 기준선이 토큰을 24시간 캐시해 새 로그인만 영향(막힌 목록 24행과 같은 벽) |
| 8 | 운영자가 banking 워커 tb-w2 에 NoSchedule taint 를 건 뒤 transfer(maxSurge 0) 일상 롤아웃이 새 파드를 못 띄움 | 인프라, 스케줄링(G, 노드, 디스크 6) | (공식 사례 못 찾음) | 버림: 원칙 2(노드, 디스크 6 이라 0 위치 아님), 원칙 1(공식 사례 없음), 카탈로그 §1(F50-R 의 '승인 단계가 새 파드를 막음' 꼴) |
| 9 | 낡은 Secret 매니페스트 재적용으로 commerce pricing DB 비밀번호가 바뀌고 일상 재시작 | 인프라, 비밀값(O) | E2B 2026-06-25 공식(설정 드리프트) | 버림: 원칙 9(기본 롤링이 Ready 못 된 새 파드 옆에 옛 파드를 남김, 막힌 목록 26행 food 판과 같은 벽), banking transfer 판은 F65-R 과 같은 꼴 |
| 10 | Kafka 브로커 또는 알림 서비스 고장(정답 위치 0) | 메시지 브로커, notification(B, C) | 여러 원본 | 버림: 원칙 7. 세 도메인 Kafka 발행이 outbox 비동기뿐이라 사용자 증상이 없다(막힌 목록 27, 146, 188, 258행) |

DB 가 정답인 후보 6개(1~6, 정답 위치가 표마다 갈림), 부품 9종(commerce payment_logs, order_items, outbox, shipment_events, food MySQL, banking Oracle, user 설정, 노드 스케줄링, Secret, Kafka/notification), 앱, DB, 인프라 층.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 주입: `scripts/scenarios/profiles/db_ddl_executor.py` `PG_VACUUM_REMOTE` `run` — testbed-postgres-0 파드 안에서 표마다 psql 세션 하나(application_name psql)를 백그라운드로 띄워 `VACUUM (FULL) payment_schema.payment_logs` 와 `VACUUM (FULL) shipping_schema.shipment_events` 를 나란히 돌리고, 두 세션이 각 표의 AccessExclusiveLock 을 쥔 것을 보면 돌아온다.
2. 결제 로그 쓰기: `commerce/payment-service/src/main/java/com/commerce/payment/service/PaymentService.java:53-61` processPayment(@Transactional) — payments INSERT 직후 `logAction(payment, "PG_REQUEST", ...)`, `:114-120` logAction 이 `paymentLogRepository.save`. 외부 PG 호출(`:63`)보다 앞이다. 표: `PaymentLog.java:7`(@Table payment_logs), `schema.sql:28-37`.
3. payment 풀: `payment-service/src/main/resources/application.yml` 에 hikari 설정이 없어 Spring Boot 기본(maximum-pool-size 10, connection-timeout 30초). 줄 선 INSERT 10건이 연결을 쥔다.
4. order: `commerce/order-service/src/main/java/com/commerce/order/service/OrderService.java:144-200` checkout(@Transactional) 이 orders 저장 뒤 결제를 부르고 실패 시 `releaseAllCartItems`. `PaymentClient.java:27-28` @CircuitBreaker + @Retry(paymentClient), `:48-53` fallback 502. `order-service/src/main/resources/application.yml:51-54` payment read-timeout 15s, `:91-97` 서킷(최소 5회, 50%, open 5s), `:134-139` 재시도 2회.
5. payment 프로브: `commerce/k8s/23-payment-service.yaml:69-88` startupProbe 5초 x 30, readinessProbe 10초 x 3(시간 제한 3초), livenessProbe 15초 x 5, 모두 `/actuator/health`(DB 확인 포함). 다시 뜬 파드는 기동 `schema.sql:37` 의 `CREATE INDEX IF NOT EXISTS idx_payment_logs_payment` 가 표의 SHARE 잠금을 얻으려다 같은 줄에 선다.
6. shipment_events: `commerce/shipping-service/src/main/java/com/commerce/shipping/service/ShippingService.java:111-114`(shipmentEventRepository.save) — Kafka 소비자와 상태 진행 스케줄러가 쓴다. 비동기라 배송 이벤트가 늦을 뿐이다.
7. DB: `commerce/k8s/10-postgres.yaml` testbed-postgres(PostgreSQL 16.14 alpine, aarch64, cpu 500m, 512Mi, tb-w1). 109 읽기 전용 실측(2026-10-11): shared_buffers 128MB, maintenance_work_mem 64MB, max_wal_size 1GB, max_connections 100, 열린 세션 62, lock_timeout 0, statement_timeout 0, commerce 는 표 소유자이자 superuser, client_connection_check_interval 0. payment_logs 힙 2,004MB + 인덱스 633MB(행 1,865만, seq_scan 0, idx_scan 2, n_tup_ins 832만, 죽은 행 20만), shipment_events 852MB + 616MB(seq_scan 0, idx_scan 0). 데이터 볼륨 /dev/vda1 38,608MB 중 21,421MB 사용(56%), 17,172MB 여유.

### 로컬 실측 (2026-10-11, 원칙 9 근거)

postgres:16-alpine 컨테이너(`--cpus 0.5 --memory 512m`, shared_buffers 128MB, maintenance_work_mem 64MB, max_wal_size 1GB)에 같은 열과 인덱스, 같은 크기의 사본을 만들었다: payment_logs 1,865만 행(힙 2,052MB + 인덱스 618MB, payments 470만 행 참조), shipment_events 1,411만 행(885MB + 523MB). 결제처럼 `BEGIN; INSERT payments; INSERT payment_logs; COMMIT` 을 2초마다 새 연결로 보내고, 다른 표 단순 조회를 1초마다 보냈다.

| 시험 | 결과 |
|---|---|
| payment_logs 하나만 VACUUM (FULL) | 99~109초. 그동안 결제 INSERT 모두 대기. 통계 초기화 뒤 한 번에 요청 체크포인트 5번, 임시 파일 713MB(인덱스 정렬이 maintenance_work_mem 을 넘음) |
| 실행기 스크립트 그대로(가짜 kubectl 로 docker exec), 두 표 나란히 | preflight 0(1.5초), run 0(5.3초, 두 세션 AccessExclusiveLock 확인). shipment_events 약 155초, payment_logs 197초 동안 결제 INSERT 가 모두 기다림(처음 것 195.8초 대기), 끝나는 순간 모두 지나감. 다른 표 조회 평균 0.24초, 최대 1.5초(331건) |
| 쌓임 | 1초마다 새 연결로 결제를 보낸 첫 시험에서 줄 선 연결이 max_connections(100)에 닿아 'too many clients' — 원본의 연결 고갈과 같은 꼴이다. 앱은 풀(10)로 연결 수를 묶어 이 꼴이 되지 않는다(§9) |
| cleanup, recovery | 끝난 뒤 cleanup 3.0초, recovery 0. drain 5초로 일찍 cleanup 하면 두 세션을 취소(9.1초), recovery 0, 행 그대로(1,865만 + 시험 중 추가) |

109 와의 차이(1차 평가 지적으로 다시 견줌): 109 는 aarch64 VM 이다. 처음에는 109 Top SQL 의 `CREATE INDEX IF NOT EXISTS idx_inventory_movements_product` 행(99~182초)을 생성 시간으로 읽어 109 가 2~3.5배 느리다고 봤는데, 그 행들은 sharedHit 134~402, sharedRead 0~3, tempWritten 0, walBytes 0 으로 인덱스가 이미 있어 기동 schema.sql 이 표 잠금을 기다린 시간이었다(idx_payments_unsettled 165초, 2026-08-20 F06-H 실행도 같은 꼴). 60일 Top SQL 에서 tempWritten 이 0 보다 큰 CREATE INDEX 는 하나뿐이다: 2026-10-11 10:12 `CREATE INDEX IF NOT EXISTS idx_auth_tokens_token ON user_schema.auth_tokens(token)` 14.7초(sharedRead 15,578, tempWritten 31,272, WAL 131MB, F33-P 실행 뒤 다시 만든 실제 생성, 트래픽 중). 같은 꼴 로컬 사본(auth_tokens 245만 행, 힙 237MB 대 109 241MB, 토큰 32자 md5, 같은 컨테이너 설정)에서 같은 문장이 17.4, 17.1, 16.0초(마지막은 컨테이너 재시작 뒤)였다. 그래서 109 는 이 꼴의 작업(전수 읽기, 정렬, 임시 파일, WAL)에서 로컬과 같거나 조금 빠르다(0.85~0.92배). 단일 코어 계산도 비슷하다(같은 파이썬 반복 2.83초 대 3.07초). 109 의 payment_logs 잠금 구간은 두 표 나란히 약 170~200초, 혼자면 약 85~110초로 본다. 혼자 돌리면 판정 창이 잠금 구간을 넘으므로 원본처럼 여러 표를 나란히 돌린다.

## 5. 원인 규정 (원칙 5, 6)

- 근본(`root_cause.target_id`): `commerce-postgres:payment_schema.payment_logs` — 그 표를 다시 쓰는 내내 ACCESS EXCLUSIVE 를 쥔 표 정리(VACUUM FULL). F06-H, F66-R 과 같은 '인스턴스:스키마.테이블' 꼴.
- 계기(`trigger_target_id`): 비움. 계기(그 표에 대한 VACUUM FULL)와 결함 위치(그 표의 잠금)가 같다. payment, order, 부하는 평시 그대로 정당하다(원칙 6: 결제마다 감사 로그를 쓰는 것은 정상, 잘못은 살아 있는 쓰기 표를 업무 중에 통째로 잠근 정리).
- 부분 점수: `commerce-payment`(증상이 드러나는 서비스), `commerce-postgres`(인스턴스 단위 답), `commerce-postgres:shipping_schema.shipment_events`(함께 정리된 두 번째 표), `commerce-postgres:payment_schema.payments`(같은 결제 트랜잭션의 앞 표).
- 층위: 원본 사후 보고의 결론("쓰이지 않는다고 여긴 표의 VACUUM FULL 이 오래 표를 잠가 그 표를 간접적으로 건드리는 질의가 쌓였다")과 같다. 관제 데이터로 낼 수 있는 결론: "payment_logs 표를 오래 쥔 psql 세션 때문에 payment 의 쓰기가 잠금을 기다려 결제와 checkout 이 실패했다". 그 세션의 문장이 VACUUM FULL 이었다는 것까지는 요구하지 않는다(세션 스냅숏에 문장 원문이 없고, DPM Top SQL 은 앞선 수집에서 본 문장의 증분만 내보내 한 번 도는 정리 문장은 닿지 않는다: F23-P, F66-R 시트와 같은 확인).

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 |
|---|---|
| 감지 | commerce-order 오류율과 오류 로그 급증('Checkout payment failed, releasing reserved stock'), checkout 5xx(게이트웨이 서버 스팬 ERROR), KCM testbed-payment Unhealthy, Killing |
| 피해 판정(러너) | 동반 부하(surge.js 10rps, 여정 절반 checkout)의 checkout 5xx 비율 ≥ 0.5 와 2xx 비율 < 0.3 이 3틱 |
| RCA | DPM 세션 스냅숏의 payment 세션 잠금 대기(relNames payment_logs)와 그 blockingPid 인 오래 도는 psql 활성 세션, DPM 지표(요청 체크포인트, 임시 파일, DB 사용 크기 상승과 하강), payments 대기 없음, 롤아웃 없음, psql 세션이 사라지는 분에 함께 회복 |

## 7. 관측 근거 표 (119 실조회, 2026-10-11 13:20~14:20 UTC)

표본 데이터(트레이스 10%, APM 스팬 지표)만으로 증명하지 않는다. 결정 증거는 DPM 세션 스냅숏(분마다), DPM 지표, 전수 로그, KCM 이벤트다.

| 증거 | 119 표와 칸 | 조회 | 평시 결과 |
|---|---|---|---|
| 근본: payment_logs 잠금 대기 | CH `lucida.dpm_session_local`(engine, body 의 pid, user, program, ip, backendType, waitType, waitEvent, sessionType, blockingPid, relNames, queryTime) | 60일 `engine='postgresql' AND JSONExtractString(body,'relNames') LIKE '%payment_logs%'` | 0건. 장애 때 payment 파드 IP 의 JDBC 세션이 Lock/relation, relNames payment_logs 로 blocked 로 처음 나타나야 한다 |
| 근본: 막는 세션의 모양 | 같은 표 | 60일 blockingPid 0 인 blocking, idle in transaction 행 표본; 60일 program 이 JDBC 가 아닌 세션 종류 | 막는 세션 행에도 relNames 가 남는다(예: `{"program":"PostgreSQL JDBC Driver","sessionType":"blocking","blockingPid":0,"relNames":"cart_items, ..."}`). Lock 대기 행은 모두 relNames 를 가진다(blocked relation 1,668, blocking relation 585 등). JDBC 가 아닌 세션은 autovacuum worker, parallel worker, collector-dpm 뿐이고 program psql 의 앱 DB 세션은 없다. 장애 때 program psql 의 client backend 가 blocking, IO/CPU 대기, queryTime 수 분, relNames payment_logs 로 나타나야 한다 |
| 계기: 무거운 표 다시 쓰기 | VM `dpm.postgresql.log.checkpoint_req`, `dpm.postgresql.instance.temp_write_size`, `dpm.postgresql.database.used_size`(target_id cf97076f-..., commerce) | 7일 max_over_time, min_over_time | checkpoint_req 7일 최대 0, temp_write_size 7일 최대 약 4.2MB(평시 0), used_size 11.5~16.1GB. 장애 때 요청 체크포인트가 생기고(로컬: payment_logs 하나에 5번) 임시 쓰기가 오르며(로컬 713MB) DB 크기가 사본만큼 올랐다 내려가야 한다 |
| 전파: 결제 실패와 서킷 | CH `lucida.lucida_logs_local` | 14일 `service_name IN ('commerce-order','commerce-payment')` 템플릿별 | order 는 'Cleared cart after checkout' INFO 뿐(초당 약 0.25건), payment 는 정산 배치 INFO 와 은행 이체 실패 ERROR 3건뿐. 장애 때 order 'Checkout payment failed, releasing reserved stock: ...'(read timeout, 이어서 서킷 open) 가 처음 늘어나야 한다(OrderService.java:193) |
| 전파: payment 재시작 | CH `lucida.kcm_events_local`(reason, object_name) | 30일 `object_name LIKE 'testbed-payment%'` reason 별; F06-H 실행 2026-08-20 22:30~23:10 | 30일 마지막 2026-10-08(다른 실행). F06-H(payments 잠금) 실행 때 22:39:59 부터 testbed-payment Unhealthy, 22:41:09 Killing, 22:41:39 재시작, 같은 시각 testbed-order Unhealthy — 결제가 DB 잠금에서 멈추면 이 테스트베드에서 payment 와 order 가 실제로 이렇게 번진다는 확인. 그때 dpm_session_local 에 payment 파드 IP(10.244.3.142)의 blocked 가 분당 10(풀 크기)이었다 |
| 감별: payments 와 DB 전체 | dpm_session_local, 기준선 상품 목록 | 장애 때 relNames payments 행, 다른 표 대기 | 장애 때 payments 에는 대기가 없고(결제 행 INSERT 는 지나간 뒤 로그 INSERT 에서 멈춤) 다른 표 질의는 이어져야 한다(로컬: 다른 표 조회 평균 0.24초) |
| 참고: DB 설정 | 109 PostgreSQL 읽기 전용 조회 | `SHOW lock_timeout`, `statement_timeout`, pg_settings, pg_stat_user_tables, df | 0, 0, §4-7 의 값(2026-10-11) |

## 8. 감별

- must_support: DPM 세션의 payment_logs 잠금 대기(payment 세션 blocked → 같은 blockingPid 의 psql 활성 세션, queryTime 수 분), 같은 꼴의 shipment_events 사슬(shipping 파드, 사용자 경로 밖), order 'Checkout payment failed' 증가, KCM testbed-payment Unhealthy, Killing(롤아웃 없음), checkout 5xx ≥ 0.5 와 2xx < 0.3, 상품 둘러보기 정상, PostgreSQL Ready 와 payments 대기 없음, psql 세션이 사라지는 분에 회복.
- must_rule_out: payments 를 쥔 트랜잭션 잠금(F06-H), 잠금 대기열(F66-R, F66-P, F60-H), 외부 결제 의존(F01-H, F06-R, F19 계열), payment 프로브나 자체 고장(F05-H, F05-R), PostgreSQL 다운이나 연결 한도(F25-H 꼴).
- contrast_with: F06-H, F66-R, F60-R, F01-H.
- 관계: F06-H 와 겉 증상(결제가 DB 잠금에서 멈춰 checkout 502, payment, order Unhealthy)이 같고 정답 표와 원인이 다르다. 판별력은 DPM 세션에 있다: F06-H 는 대기 relNames 가 payments 이고 막는 세션이 idle in transaction 의 JDBC 세션(아무 일도 하지 않음), F67-R 은 대기 relNames 가 payment_logs 이고 막는 세션이 psql 의 활성 세션(디스크와 CPU 를 쓰며 queryTime 이 늚)이며 DPM 지표에 요청 체크포인트와 임시 쓰기가 함께 오른다. F66-R 과는 '표 잠금 뒤 앱 질의가 쌓임' 꼴이 같고 사슬 모양(한 단 대 두 단)이 다르다. F60-R 과는 'DDL 이 표를 다시 쓰는 동안 쓰기를 막음' 꼴이 같고 엔진(MySQL 외래 키 추가 대 PostgreSQL VACUUM FULL)과 계기(마이그레이션 대 공간 회수 정리)가 다르다.

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

- 강도는 하나로 고정(approved-fixed-f67-r): 정리 대상 두 표(tables), cleanup 대기 상한 720초(drain_seconds), 시작 조건 두 표 행 100만 이상, 열린 세션 70 이하, 데이터 볼륨 여유 8,000MB 이상이고 두 표 크기(4,106MB) + WAL 1GB 를 더해도 사용률 80% 미만(109 현재 (21,421 + 4,106 + 1,024) / 38,608 = 69%). 잠금 구간은 표 크기와 DB 자원으로 정해지고 실행기는 구간을 늘리거나 줄이지 않는다.
- 부하: 동반 surge.js 10rps(ramp 30s, hold 12m, 여정 50% 가 checkout → checkout 초당 약 5건, 30초 창마다 약 150건. ramp 를 30초로 둬 첫 30초 창부터 checkout 표본이 충분하다). 동반 k6 는 막 떠 토큰 캐시가 비어 login 부터 하지만 이 주입은 users 를 건드리지 않아 로그인, 장바구니는 그대로 지나가고 checkout 만 결제에서 막힌다(F66-R 1차 평가 지적의 '표본 0' 문제가 없다). 기준선 commerce(2~10rps)도 함께 돈다.
- 피해 계산: checkout 초당 약 5건이 각 결제에서 멈춘다. payment 풀 10 은 2초 안에 찬다. order 의 결제 호출은 15초 read-timeout, 재시도 2회(300ms, 지수)인데 게이트웨이의 order read-timeout 도 15초라(`api-gateway/src/main/resources/application.yml:34-37`) 처음 checkout 은 약 15초 뒤 게이트웨이에서 5xx 가 되고, order 쪽은 실패 5건이 쌓이면 결제 서킷이 열려(최소 5회, 50%) 뒤의 checkout 은 곧바로 502, 5초마다 반열림 3건이 다시 15초씩 막힌다. 게이트웨이 order 서킷이 열리면 'Downstream unavailable for POST /api/orders/checkout' 로 곧바로 거절된다(평시 14일 로그에 다른 실행 때 이 WARN 이 있음). order 는 checkout 트랜잭션 안에서 결제를 기다리며 연결을 쥐어 order 풀도 찬다(F06-H 실행 때 testbed-order Unhealthy 와 같은 꼴). 그래서 checkout 5xx 비율은 1 에 가깝다.
- 연결 한도 계산: 원본은 DB max_connections 를 다 썼다. 테스트베드는 앱 풀이 먼저 묶는다. 줄 선 백엔드는 클라이언트가 죽어도 잠금이 풀릴 때까지 남을 수 있다(F66-R 로컬 확인, F06-H 실행 때 109 에서는 payment 재시작 뒤 그 IP 의 blocked 가 10 → 1 로 줄었다). 나쁜 경우: 첫 payment 파드 10 + 재시작마다 기동 CREATE INDEX 1(유휴 연결은 파드가 죽으면 닫힘, 잠금 구간 약 3분 안에 재시작은 1번) + shipping 소비자와 스케줄러 약 2 + 정리 세션 2 → 평시 62 + 15 ≈ 77 < 100. 시작 조건 70 이하가 여유를 지킨다.
- 판정 시각과 피해 구간(1차 평가 지적으로 다시 계산): 러너는 성공 streak 을 settle, min_hold 동안에도 쌓고(rca-scenario-runner `backend/app/adaptive.py` advance 는 모든 틱에 streak 을 갱신하고 `_drop_pending_streaks` 는 must_rule_out, escalate 만 버림), 같은 표본을 다시 읽은 틱은 세지 않는다(독립 간격 = 신호 갱신 간격 30초, `_updated_streaks`, `_independence_sec`). 그래서 성공 시각 = max(min_hold, 첫 참 표본 + 60초)다.
  - 첫 참 표본: 주입 뒤 결제가 곧바로 멈추고, 그때 걸린 checkout 은 게이트웨이 order read-timeout 15초(`api-gateway/src/main/resources/application.yml:34-37`)에 5xx 가 된다. 동반 k6 의 30초 창이 이를 담는 첫 창은 약 30~45초. 3표본째는 약 90~105초.
  - min_hold 1m 은 이보다 짧아 판정을 늦추지 않는다(must_rule_out 판정만 1분 뒤부터). 판정은 주입 뒤 약 90~105초, 잠금 구간(109 추정 170~200초, 로컬 197초)의 약 5할 지점이다.
  - 피해 구간 = 잠금 구간 + 재시작 꼬리. payment 는 풀이 찬 뒤 readiness(10초 x 3, 각 3초 시간 초과)가 약 30~40초에 빠지고 liveness(15초 x 5)가 약 80~90초에 죽인다. 다시 뜬 파드는 기동 schema.sql 의 CREATE INDEX IF NOT EXISTS 가 잠금에 서서 기동이 멈추고(startupProbe 5초 x 30 = 150초라 잠금 구간 안에 다시 죽지 않음), 잠금이 풀리면 몇 초 안에 기동을 마치고(F66-R 로컬 실측: user 가 잠금 해제 3초 뒤 Started) 다음 readiness(10초)에 Ready 다. 그 뒤 order 와 게이트웨이 서킷이 반열림(5초)에서 닫힌다. 꼬리 약 20~30초라 피해 구간은 약 190~230초다. 잠금이 끝날 때 payment 가 막 죽어 CrashLoopBackOff 첫 지연(10초)에 있으면 꼬리가 10초쯤 더 길다.
- success: 동반 부하 checkout_5xx_rate ≥ 0.5 와 checkout 2xx 비율 < 0.3, 독립 표본 3개. min_hold 1m, settle 30s, timeout 5m(피해 구간 상한 약 230초에 여유).
- must_rule_out: achieved_rps < 5(동반 10rps), 기준선 상품 목록 실패율 ≥ 0.2(상품은 다른 표라 이어져야 함. 로컬에서 다른 표 조회 최대 1.5초), commerce PostgreSQL 파드 NotReady(2틱).
- abort: entry_status == 0(2틱). commerce 입구는 게이트웨이(30080)이고 DB 를 쓰지 않는다. checkout 스레드는 order 응답까지만 묶이고(게이트웨이 order read-timeout 안), 서킷이 열린 뒤에는 곧바로 끝난다.
- recovery: target_health 200, payment Ready, PostgreSQL Ready(2틱, 10분). 동반 부하는 cleanup 에서 먼저 멈추므로 checkout 비율은 넣지 않는다(F06-H 와 같음). payment_logs 다시 쓰기가 끝나면 줄 선 INSERT 가 한꺼번에 지나간다. 재시작 고리의 CrashLoopBackOff 지연이 남을 수 있어 10분이다.
- cleanup: 두 다시 쓰기가 스스로 끝나기를 최대 720초 기다리고(넘으면 pg_cancel_backend, 취소된 VACUUM FULL 은 옛 사본을 그대로 둬 되돌릴 것이 없음), 두 세션이 없어진 뒤 파드 안 로그 파일 삭제, 두 표 행 확인. 행, 인덱스, 제약, 다른 표는 손대지 않는다. 다시 쓰기가 끝나면 표 파일이 새로 쓰여 크기가 조금 줄 뿐 내용은 같고, 다음 실행도 같은 시간이 걸린다(다시 쓰기 시간은 살아 있는 행 수로 정해짐). 실행기 프로세스 상한(900초) 안이다.
- run 시간: 탐침 약 14번 + kubectl exec 3번 + 잠금 확인, 로컬 5.3초. 러너 coordinator lease(30초) 안이다.
- 디스크: 두 사본과 WAL 이 잠깐 약 5.1GB 를 더 쓴다(사용률 56% → 약 69%, kubelet 축출선 85% 아래). 다시 쓰기가 끝나면 옛 파일이 지워진다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Coveralls 2025-04-06 공식 상태 페이지 사후 보고)와 요소별 대응표, 기전 동일(§2)
- [x] 분류 장부 갱신(§4-1 행, §6 기록), 묶음 D(11→12, 94 중 12.8%), 정답 위치 DB 테이블(결제 로그)(0→1), 결제 경로 12(보수적으로 결제 로그를 넣어도 13/94, 13.8%), 서비스 쇼핑몰 31→32(세 도메인 동률이라 정답 위치 0 부품만), 고른 이유(§3). 어느 축도 20% 미만
- [x] 근본 원인 위치 `file:line`(결제 로그 쓰기, 풀, order 결제 호출과 서킷, 프로브, 기동 인덱스)과 인프라 지점(testbed-postgres-0, 설정과 표 크기 실측)(§4)
- [x] 근본 원인 흔적 119 실조회: dpm_session_local 이 대기 행과 막는 행에 relNames, blockingPid, program, queryTime 을 남김(다른 실행 행으로 확인), 60일 relNames payment_logs 0건, JDBC 가 아닌 앱 세션 0(§7)
- [x] 핵심 증거가 표본에만 있지 않음: DPM 세션 스냅숏(분마다), DPM 지표, 전수 로그, KCM 이벤트. 트레이스는 보조
- [x] 계기 흔적: 막는 세션(program psql, 활성, IO/CPU, queryTime 수 분)과 DPM 요청 체크포인트, 임시 쓰기, DB 크기 변화. 인공 지연 없음(잠금 구간은 실제 다시 쓰기 시간)
- [x] 정답이 관제 데이터로 낼 수 있는 결론(payment_logs 를 오래 쥔 세션이 payment 쓰기를 막음), 코드 설계 결함 추론 불필요. 결제 트랜잭션 안의 동기 감사 로그 쓰기는 피해 설명(§5, user_impact)
- [x] 정답지 세 칸이 원칙 6 대로(근본 payment_logs 표, 계기 같음, 부분 점수 commerce-payment, commerce-postgres, shipment_events, payments)
- [x] 감지, 피해 판정, RCA 증거 구분(§6)
- [x] 주입 도구가 시나리오 id 를 남기지 않음: 실행기 인자는 네임스페이스, 파드, 표 이름, 숫자뿐이고, 정리 세션은 psql 기본 이름, 파드 안 파일은 /tmp/table-maintenance-<스키마-표>.log, 탐침은 pg_stat_statements 에 남지 않음(테스트 `test_postgres_vacuum_full_holds_the_tables_until_the_rewrite_ends_and_nothing_is_undone`)
- [x] 부하 상한과 서킷브레이커를 고려한 피해 계산(§9: payment 풀 2초 안에 고갈, 게이트웨이 15초 시간 초과와 order 서킷 open 으로 checkout 5xx 가 1 에 가까움, 판정 시각(약 90~105초)과 잠금 구간(170~200초), 재시작 꼬리를 넣은 피해 구간(190~230초), 109 속도를 실제 인덱스 생성(14.7초 대 로컬 16.0~17.4초)으로 견준 계산, 연결 한도 여유 계산, 로컬 실측)
