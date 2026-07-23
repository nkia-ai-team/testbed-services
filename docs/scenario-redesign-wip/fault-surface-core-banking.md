# core-banking — Fault Surface Map (bottom-up, code-grounded)

> 실측 대상: `/home/ydkim/project-2025/testbed-services/core-banking`
> 4 Spring Boot 서비스 (api / account / transfer / ledger) + `shop-common` 라이브러리, Java 17 / Spring Boot 3.4.5, Oracle 23ai Free (PDB FREEPDB1, schema `banking`), Kafka(KRaft) + outbox 패턴.
> 모든 근거는 실제 소스 file:line. injection 엔드포인트는 앱에 없음 — 주입은 rca-scenario-runner 담당.

---

## 0. 토폴로지 (실측)

```
                    ┌─ nginx :80 (NodePort 30082) ─┐
client / loadgen ──▶│ /api/transfers → api-service │
                    │ /api/accounts  → account-svc │   (ledger 는 nginx 노출 없음)
                    └──────────────────────────────┘
POST 이체 정상경로:  api ──RestClient──▶ account ──RestClient──▶ transfer ──▶ Oracle(FOR UPDATE) ──▶ outbox_events
                                                                         └outbox relay(2s)─▶ Kafka banking.transfers ─▶ ledger consumer ─▶ Oracle ledger_entries
                                                                                                                          └outbox relay─▶ Kafka banking.ledger (소비자 없음)
cross-domain:        commerce-payment ──NodePort 30282(testbed-transfer-external)──▶ transfer  (api·account 우회!)
GET 조회 프록시:     api /api/transfers(GET) ──RestClient──▶ transfer GET (account 우회)
```

동기 체인: api→account→transfer 는 전부 **동기 blocking RestClient**. transfer→Kafka→ledger 만 비동기.

### 서비스 인벤토리

| svc | port | 책임 | 외부노출 | DB | Kafka | 배치 |
|---|---|---|---|---|---|---|
| **api-service** | 8080 | 이체 진입 gateway, 입력검증, 조회프록시 | nginx `/api/transfers` | **없음** (DataSourceAutoConfig 제외) | 없음 | 없음 |
| **account-service** | 8081 | 계좌 조회/검색, 이체 전 검증(존재·ACTIVE·잔액), 이자배치 | nginx `/api/accounts` | Oracle (Hikari max=10) | 없음 | InterestBatch (cron 0 0 0) |
| **transfer-service** | 8082 | 이체 트랜잭션(FOR UPDATE lock 집중), 목록/통계, cleanup배치, outbox 발행 | **NodePort 30282 직행**(commerce) | Oracle (Hikari **max=15**) | producer(outbox→banking.transfers) | StaleTransferCleanup (10분) |
| **ledger-service** | 8083 | 원장 복식부기 반영(consumer), 원장 조회, 대사배치 | 없음 | Oracle (Hikari max=10) | **consumer** banking.transfers + producer banking.ledger | ReconciliationBatch (10분) |

전 서비스 공통 resource limit: **cpu 500m / mem 1Gi, replicas=1** (k8s/2x-*.yaml:76-91, :9). Tomcat thread pool·`@Transactional timeout` **어디에도 미설정** → tomcat 기본 200 스레드, 트랜잭션 타임아웃 무한.

---

## 1. Resilience 설정 실측치

| 항목 | 값 | 근거 |
|---|---|---|
| api→account read-timeout | **10s**, connect 3s | api application.yml:11-12 |
| api→transfer(조회) read-timeout | 10s, connect 3s | api application.yml:15-16 |
| account→transfer read-timeout | **15s**, connect 3s | account application.yml:29-30 |
| CircuitBreaker (accountClient/transferClient) | window=10, min-calls=5, **fail-rate=50%**, open=5s, half-open=3 | api yml:21-38 / account yml:35-43 |
| Retry | **max-attempts=3**, wait 200ms, exp backoff x2 | api yml:41-54 |
| CB/Retry ignore | `ClientErrorException`(4xx) 무시 — 4xx 는 CB 안 열림 | 전 client + ClientErrorException.java:7 |
| Hikari pool | account=10, **transfer=15**, ledger=10, **connection-timeout=3000ms** | 각 application.yml hikari |
| Oracle FOR UPDATE | PESSIMISTIC_WRITE, **NOWAIT/timeout 없음** → 무한 대기 | transfer AccountRepository.java:18-20 |
| Outbox relay | fixedDelay **2000ms**, batch **Top100** | OutboxRelay.java:33,36 |
| Kafka consumer concurrency | **미설정 → 기본 1 스레드** | ledger application.yml:27-31, TransferEventConsumer.java:28 |
| Kafka producer ack | relay `.send().get()` 동기 블로킹 | OutboxRelay.java:39 |

핵심 취약 상수: **transfer read-timeout(account측 15s) > transfer 트랜잭션이 lock 물고 버틸 수 있는 시간에 상한 없음**, retry x3 가 곱 부하, Hikari connection-timeout 3s(짧음).

---

## 2. DB / 트랜잭션 경계 (은행 정합성 핵심)

- 이체 실제 잔액변경은 **transfer-service `execute()` 단일 @Transactional** (TransferService.java:51-107). account-service `validateAndForward` 는 `@Transactional(readOnly=true)` — 검증만, 잔액변경 없음 (AccountService.java:57-81).
- deadlock 회피: 두 계좌를 **id 오름차순으로 순차 FOR UPDATE** (TransferService.java:63-71). 같은 계좌쌍 동시이체 = row-lock 직렬화.
- 인덱스: transfers(from/to/order/status/created), ledger(ref/account) 모두 존재 (db/init.sql:42-57). `transfer_ref` UNIQUE, ledger 멱등은 `findByTransferRef` 조회로 (LedgerService.java:35).
- 통계쿼리 `dailyStatsSince` = **native `trunc(created_at)` group by full scan** (TransferRepository.java:33-36) — created_at 인덱스 있으나 trunc 로 인덱스 미사용 가능.

---

## 3. 에러 처리 경로 (5xx 가 실제 어디서 나오나)

- 모든 서비스: `GlobalExceptionHandler` 가 `ServiceException` → 해당 status 로 매핑. **그 외 미처리 예외는 Spring 기본 500**.
- client 계층 3종 동일 패턴: 하류 4xx → `ClientErrorException` 그대로 전파(CB 무시), 그 외 RestClientException(타임아웃/커넥션/5xx) → **`ServiceException(502 BAD_GATEWAY)`** (AccountClient.java:41-46, account TransferClient.java:41-46, api TransferClient.java:43-48).
- CB open / retry 소진 → fallback → 역시 **502** (AccountClient.java:56).
- **502 는 항상 "호출한 쪽"에서 발생** → 근본원인 서비스(느린 transfer)와 증상 서비스(502 뱉는 api/account) 분리됨.
- ledger consumer 는 예외를 **catch 후 log.error 만** 하고 삼킴 (TransferEventConsumer.java:40-42) → offset 커밋됨 → **이벤트 유실**(재처리 안 됨).
- outbox publisher/event publisher 의 발행실패는 `non-critical` log 로 삼킴 (TransferEventPublisher.java:35-38, LedgerEventPublisher.java:41-43) — 단 OutboxPublisher.publish 자체 실패(직렬화)는 500 던져 이체 트랜잭션 롤백 (OutboxPublisher.java:32-35).

---

## 4. 관측

OTel javaagent hostPath 주입(JAVA_TOOL_OPTIONS), `OTEL_SERVICE_NAME=core-banking-<svc>`, traces/metrics/logs exporter=otlp (k8s/2x:39-63). 앱 커스텀 메트릭 **없음** — actuator health/info/metrics 만 노출, kafka health 는 disabled (전 application.yml management 블록). RCA 는 순수 javaagent span/JVM/DB 메트릭에만 의존.

---

## 5. 현실적 장애 시나리오 후보 (총 12)

각 후보: 트리거 → 전파경로 → 관측증상 → root vs 증상 → 근거.

### ⭐ TOP 5 (코드 근거 강)

**FS-1. 이체 row-lock 경합 → 동기체인 역압 → api/account 502 폭증**
- 트리거: 소수 hot 계좌(예: 정산계좌) 대상 동시 이체 폭주, 또는 `kubectl exec` 로 Oracle 에 해당 row 장기 lock 주입.
- 전파: transfer `execute()` 가 `findByIdForUpdate` 에서 **NOWAIT 없이 무한 대기** (transfer AccountRepository.java:18-20) → 트랜잭션 timeout 없음(TransferService.java:51) → account→transfer 호출이 15s read-timeout 초과 → account 가 502, retry x3 로 부하 증폭 → api 도 502.
- 증상: **transfer 는 5xx 안 뱉고 지연만**, account-service/api-service 가 502. lock 대기로 transfer Hikari(15) 커넥션 점유 → pool 고갈 동반.
- root=transfer(lock), 증상=account·api(502). 근거: transfer/AccountRepository.java:18-20, TransferService.java:51-71, account application.yml:30, api yml:42.

**FS-2. transfer 지연 → account CircuitBreaker OPEN → 이체 전면 거절**
- 트리거: transfer-service 지연(FS-1 의 lock 또는 slow query 주입) 실패율 50% 돌파.
- 전파: account 의 `transferClient` CB window=10/min=5/fail=50% 돌파 → **OPEN 5s** → fallback 즉시 502 (account TransferClient.java:28,50-57). api 의 accountClient CB 도 연쇄 OPEN 가능.
- 증상: account 가 하류 안 부르고 즉시 502(빠른 실패), 이체 성공률 0. transfer 자체는 정상 회복돼도 5s 쿨다운.
- root=transfer, 증상=account(CB open). 근거: account application.yml:35-41, account TransferClient.java:28.

**FS-3. Kafka consumer lag → 원장 반영 지연(단일 스레드 병목)**
- 트리거: 이체 TPS 급등 or ledger Oracle 지연/DB lock.
- 전파: ledger consumer **concurrency 미설정=단일 스레드** (TransferEventConsumer.java:28, ledger yml:27) 로 `banking.transfers` 순차소비. `recordTransfer` 가 매 건 2 insert + 멱등조회 (LedgerService.java:33-44). 처리속도 < 유입속도 → consumer lag 증가.
- 증상: transfer/account/api 는 **정상 200**(이체는 성공). 원장만 지연 → `GET /api/ledger-entries` 최신 이체 미반영, ReconciliationBatch 가 임시 imbalance WARN 가능.
- root=ledger(consumer 처리량), 증상=데이터 지연(5xx 아님, silent). 근거: ledger yml:27-31, TransferEventConsumer.java:28-39.

**FS-4. Outbox relay 적체 → 이체 성공하지만 원장 이벤트 미발행**
- 트리거: Kafka 브로커 불가/지연, 또는 이체 폭주로 outbox 생성속도 > 발행속도.
- 전파: relay 는 **2s 주기 x Top100** = 이론상 ~50 events/s 상한 (OutboxRelay.java:33,36), `send().get()` 동기 → 브로커 지연 시 relay 트랜잭션 자체가 느려짐. Kafka 다운 시 `published_at=null` 로 무한 누적(at-least-once 재시도).
- 증상: 이체 API 는 200(outbox insert 는 로컬 트랜잭션이라 성공), `outbox_events` 미발행 행 적체 → ledger 반영 정지. transfer Hikari 커넥션을 relay 스케줄이 추가 점유.
- root=transfer relay/Kafka, 증상=원장 정지 + outbox 테이블 팽창. 근거: OutboxRelay.java:33-46, OutboxEventRepository.java:8.

**FS-5. cross-domain 검증 우회 — commerce 직행이 account 검증(ACTIVE) 스킵**
- 트리거: commerce-payment 가 NodePort 30282(testbed-transfer-external) 로 **transfer 직행** (k8s/22:109-120 주석). api·account 우회.
- 전파: transfer `execute()` 는 amount>0·from≠to·계좌존재·잔액만 검사하고 **계좌 status(FROZEN/CLOSED) 검사 없음** (TransferService.java:52-89). ACTIVE 체크는 오직 account `validateAndForward`(AccountService.java:66-71)에만 존재.
- 증상: 정상경로(nginx→api→account) 이체는 FROZEN 거절(400)되나, **commerce 직행 이체는 FROZEN/CLOSED 계좌에서도 성공(200 COMPLETED)** — 도메인 정합성 결함. cross-domain timeout 주입 시엔 transfer 지연이 commerce 쪽 504 로 관측(경계 반대편).
- root=transfer(검증 비대칭), 증상=commerce 측 or 무결성 위반. 근거: TransferService.java:52-89 vs AccountService.java:66-71, k8s/22:109-120.

### 추가 후보 (6~12, 근거 중간)

**FS-6. Hikari pool 고갈 (transfer max=15)** — FS-1/FS-4 로 lock 대기·relay 가 15 커넥션 점유 → 신규 이체가 connection-timeout **3s** 초과 → transfer 내부 500(CannotGetJdbcConnection) → account 502. root=transfer pool. 근거: transfer yml:13-15.

**FS-7. slow query — dailyStats full scan** — `GET /api/transfers/stats/daily` 의 native `trunc(created_at) group by` (TransferRepository.java:33-36) 가 대량 transfers(seed 수만행)에서 느림 + transfer 커넥션 점유 → 이체 경합 악화. root=transfer 조회. 증상 자체 서비스 지연.

**FS-8. api-service SPOF / gateway 지연** — replicas=1, 모든 이체가 api 단일 pod 경유(정상경로). api 장애=전 이체 진입 차단. cross-domain 경로만 생존(transfer 직행). 근거: k8s/20:9, nginx configmap:38-43.

**FS-9. retry 증폭 storm** — 하류 502/타임아웃 시 api·account 각 max-attempts=3 → 이체 1건이 최대 3x3=9 하류호출. transfer 이미 과부하일 때 retry 가 부하 배가. 근거: api yml:41-54, account yml:44-52.

**FS-10. ledger 이벤트 유실 (at-most-once 실질)** — consumer 가 처리 예외를 catch·log 후 삼켜 offset 커밋 (TransferEventConsumer.java:40-42) → DB 오류 순간의 이벤트는 원장 영구 누락 → ReconciliationBatch imbalance WARN (ReconciliationBatch.java:30-31)로만 사후 관측. root=ledger consumer 에러처리.

**FS-11. 원장 대사 불일치(정합성 관측)** — seed 이력이 잔액과 비정합(README:155) + FS-10 유실 시 DEBIT≠CREDIT → `sumImbalance` ≠0 → 10분 배치 WARN (LedgerService.java:50-57, ReconciliationBatch.java). 순수 관측 시나리오.

**FS-12. Oracle 자체 지연/다운** — 단일 Oracle PDB 를 account·transfer·ledger 3서비스 공유. Oracle slow/다운 → 3서비스 동시 열화, connection-timeout 3s 로 빠르게 500/502 전파. root=Oracle(인프라), 증상=전 서비스. 공유 DB 라 blast radius 최대.

---

## 6. root-cause vs 증상 요약표

| 후보 | root 서비스 | 증상 관측 위치 | 증상 형태 |
|---|---|---|---|
| FS-1 lock 경합 | transfer | account, api | 502 + 지연 (transfer 는 지연만) |
| FS-2 CB open | transfer | account, api | 즉시 502, 이체 0% |
| FS-3 consumer lag | ledger | (5xx 없음) | 원장 데이터 지연 |
| FS-4 outbox 적체 | transfer/Kafka | (5xx 없음) | 원장 정지 + 테이블 팽창 |
| FS-5 검증 우회 | transfer | commerce/무결성 | FROZEN 이체 성공(200) |
| FS-6 pool 고갈 | transfer | account, api | 500→502 |
| FS-7 slow query | transfer | 자체 | 조회 지연 + 경합 |
| FS-8 gateway SPOF | api | 전 정상경로 | 이체 진입 차단 |
| FS-9 retry storm | (증폭기) | transfer | 부하 배가 |
| FS-10 이벤트 유실 | ledger | 원장 | 영구 누락 |
| FS-11 대사 불일치 | ledger/data | 배치 WARN | imbalance |
| FS-12 Oracle | Oracle(인프라) | 3서비스 | 광역 500/502 |

## 7. 관측성 사각(장애 판정 난이도 상승 요인)
- 커스텀 메트릭 0 → outbox 적체·consumer lag 은 앱 지표로 안 보임(FS-3·FS-4 는 DB row count/Kafka lag 외부관측 필요).
- 이벤트 유실(FS-10)·검증우회(FS-5)는 **5xx 없이 성공(200)** 으로 나타나 trace 만으론 정상과 구분 불가 — 데이터 정합 검사 필요.
- 502 는 항상 호출자쪽 → naive RCA 가 api/account 를 범인으로 오판하기 쉬움(진짜 root 는 transfer/Oracle).
