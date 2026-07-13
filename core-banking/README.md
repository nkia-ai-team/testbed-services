# core-banking

> 은행/금융 도메인의 RCA 테스트베드. 이체 트랜잭션의 row-lock 경합과 원장 비동기 반영을 관측 대상으로 노출한다. Oracle 23ai Free 기반.

## Overview

계좌 이체(transfer)를 중심으로 한 코어뱅킹 마이크로서비스. api → account → transfer 동기 체인으로 이체를 실행하고, transfer 가 `accounts` 를 `FOR UPDATE` 로 lock 한 뒤 outbox 패턴으로 Kafka(`banking.transfers`)에 이벤트를 발행하면 ledger(consumer)가 소비해 복식부기 원장에 반영한다. transfer-service 는 commerce-payment 가 호출하는 **cross-domain 진입점**이다. commerce 플래그십 확장 템플릿(§1, `docs/spec-testbed-expansion.md`)을 복제해 Kafka/outbox·Resilience4j·상주 배치·read API·대량 시드·상주 부하생성기를 갖췄다.

- 도메인: 은행/금융
- 언어/프레임워크: Java 17 + Spring Boot 3.4.5, Maven 멀티모듈
- DB: **Oracle 23ai Free**(`gvenzl/oracle-free:23-slim`, PDB `FREEPDB1`)
- 이벤트 백본: **Kafka**(KRaft 단일 브로커) + outbox 패턴. 기존 Redis Streams(`transfer-events`)는 제거하고 Kafka 로 전면 대체(commerce 이벤트 백본 정합, 판단 근거는 최종 구현 보고 참조).
- 복원성: Resilience4j(timeout+retry+circuit breaker) — account→transfer, api→account 동기 호출. HikariCP 풀 명시.
- 상주 배치 3종: 원장 대사(ledger reconciliation), 이자 계산/기표(interest accrual), 미완료 이체 정리(stale transfer cleanup).
- 상주 부하생성기: `loadgen/`(k6, diurnal 프로파일 피크 4 / 저점 1 req/s).
- 서비스 개수: 4 (api / account / transfer / ledger) + shop-common
- 시연 가능 장애 패턴: lock-contention(이체 row-lock), slow query, connection pool 고갈, Kafka consumer lag/outbox 적체, cross-domain 경계 timeout, circuit breaker open

## Services

| service.name | 모듈 | 포트 | 역할 | 주요 호출 |
| --- | --- | --- | --- | --- |
| core-banking-api | api-service | 8080 | 이체 요청 진입(gateway) | → account (RestClient, Resilience4j) |
| core-banking-account | account-service | 8081 | 계좌 조회·검증·목록, 이자 배치 | → transfer (RestClient, Resilience4j), → Oracle |
| core-banking-transfer | transfer-service | 8082 | 이체 트랜잭션(lock 집중), 이체 목록/통계, 정리 배치 | → Oracle(FOR UPDATE), → outbox→Kafka(`banking.transfers`). **← commerce-payment(cross-domain)** |
| core-banking-ledger | ledger-service | 8083 | 원장 반영(consumer), 원장 목록, 대사 배치 | ← Kafka(`banking.transfers`), → Oracle, → outbox→Kafka(`banking.ledger`) |

### 호출 그래프

```
client ──▶ nginx(30082) ──▶ api ──▶ account ──▶ transfer ──▶ Oracle (accounts FOR UPDATE, transfers insert, outbox_events insert)
                                                     │
                                                     └─▶ Kafka "banking.transfers"(outbox relay) ──▶ ledger ──▶ Oracle (ledger_entries)
                                                                                                        └─▶ Kafka "banking.ledger"(outbox relay, 소비자는 현재 core-banking 내부에 없음 — 향후 확장 지점)

commerce-payment ─(FQDN, W3C traceparent)─▶ transfer  (동일 POST /api/transfers)
loadgen(k6, 상주) ──▶ nginx ──▶ api/account/transfer (잔액조회·거래내역·소액이체)
```

## Cross-domain 고정 인터페이스

transfer-service 가 노출하며 commerce-payment 가 호출한다(반대편은 별도 에이전트 구현). traceparent 는 OTel javaagent 가 자동 전파. **Oracle 전환 이후에도 경로·스키마·계정 ID 는 전부 불변.**

```
POST /api/transfers
Request : { "fromAccount": string, "toAccount": string, "amount": number, "orderId": string }
Response: { "transferId": string, "status": "COMPLETED" | "FAILED" }
```

FQDN: `transfer-service` 는 k8s Service `testbed-transfer.rca-testbed-banking.svc.cluster.local:8082`(불변).

## Read API (신규, 6번 증분 패턴 — 배열 응답 + nullable page/size)

| 서비스 | 엔드포인트 | 설명 |
|---|---|---|
| account | `GET /api/accounts?status=&holder=&page=&size=` | 계좌 목록/검색 |
| transfer | `GET /api/transfers?fromAccount=&toAccount=&status=&from=&to=&page=&size=` | 이체 목록/검색 |
| transfer | `GET /api/transfers/stats/daily?days=30` | 일별 이체 건수/합계 집계 |
| ledger | `GET /api/ledger-entries?accountId=&transferRef=&direction=&page=&size=` | 원장 목록/검색 |

기존 `GET /api/accounts/{id}`, `POST /api/transfers`, `GET /api/transfers/{transferRef}` 시그니처는 그대로 유지(추가만, 변경 없음).

## Kafka 토픽

| 토픽 | 발행자 | 구독자 | 용도 |
|---|---|---|---|
| `banking.transfers` | transfer-service(outbox relay) | ledger-service | 이체 완료 이벤트 → 원장 반영 트리거 |
| `banking.ledger` | ledger-service(outbox relay) | (현재 core-banking 내부 소비자 없음) | 원장 반영 완료 이벤트 — 향후 cross-domain 분석/알림 확장 지점 |

## DB Schema

Oracle 단일 PDB(`FREEPDB1`), 스키마 사용자 `banking`. 테이블 4개(outbox_events 신규). 전체 DDL + 코어 시드: [`db/init.sql`](db/init.sql). 대량 시드(계좌 1000개, 이체/원장 각 수만 행, 90일 diurnal 분포): [`db/seed-all.sql`](db/seed-all.sql)(init.sql 로드 후 단독 실행 가능).

### `accounts`
| 컬럼 | 타입 | PK |
|---|---|---|
| id | VARCHAR2(64) | ✓ |
| holder | VARCHAR2(128) | |
| balance | NUMBER(18,2) | |
| status | VARCHAR2(16) | | (ACTIVE / FROZEN / CLOSED)

### `transfers`
| 컬럼 | 타입 | PK |
|---|---|---|
| id | NUMBER (IDENTITY) | ✓ |
| transfer_ref | VARCHAR2(64) UNIQUE | |
| from_account | VARCHAR2(64) | |
| to_account | VARCHAR2(64) | |
| amount | NUMBER(18,2) | |
| status | VARCHAR2(16) | | (COMPLETED / FAILED / PENDING)
| order_id | VARCHAR2(64) | |

### `ledger_entries`
| 컬럼 | 타입 | PK |
|---|---|---|
| id | NUMBER (IDENTITY) | ✓ |
| transfer_ref | VARCHAR2(64) | |
| account_id | VARCHAR2(64) | |
| direction | VARCHAR2(8) | | (DEBIT / CREDIT)
| amount | NUMBER(18,2) | |

### `outbox_events` (신규)
| 컬럼 | 타입 | PK |
|---|---|---|
| id | NUMBER (IDENTITY) | ✓ |
| aggregate_type / aggregate_id / event_type / topic | VARCHAR2 | |
| payload | CLOB | |
| created_at / published_at | TIMESTAMP | |

## 관측 태깅 (식별자 계약 §3)

- APM `service.name`: deployment env `OTEL_SERVICE_NAME=core-banking-<svc>` (등록 application target `meta.service_name` 과 정확일치).
- `OTEL_RESOURCE_ATTRIBUTES=lucida.organizationId=${POLESTAR_ORG_ID},lucida.groupId=core-banking,lucida.target_id=${<SVC>_TARGET_ID}`.
- OTel javaagent 는 이미지에 굽지 않고 hostPath(`/opt/polestar10/apm`, 인프라 공급)를 `JAVA_TOOL_OPTIONS` 로 주입.
- DB/pod/host 의 `lucida.target_id` 는 collector(db_poll/kcm/sms)가 emit — 앱이 넣지 않는다.
- Oracle 은 DPM `db_poll` 로 engine=oracle 등록. NodePort 30308(외부 접속, 기존 MariaDB 시절 30307 과 구분).

## Failure Scenarios (RCA 검증용)

앱에는 injection 엔드포인트를 두지 않는다. 실제 주입은 `rca-scenario-runner` 가 담당.

- **lock-contention** — 동시 이체가 같은 계좌를 대상으로 `SELECT ... FOR UPDATE` 경합. `kubectl exec` 로 Oracle 에 장기 보유 lock 주입.
- **slow query** — transfers/ledger_entries 대상 slow query 주입.
- **connection pool 고갈** — 이체 부하로 HikariCP 고갈.
- **Kafka consumer lag / outbox 적체** — ledger consumer 지연 시 outbox_events 미발행 적체 또는 컨슈머 랙.
- **circuit breaker open** — account→transfer, api→account 호출 실패율 급등 시 브레이커 open.
- **cross-domain timeout** — commerce-payment → transfer 경계에서 지연/504.

## Build & Deploy

### 로컬 (Docker Compose)

```bash
docker-compose -f docker-compose.dev.yml up --build
```

Oracle 기동은 수분 소요 — `healthcheck.sh` 기반 healthcheck 로 서비스 컨테이너들이 대기한다.

### 타겟 서버 (kubeadm)

```bash
export OTLP_ENDPOINT=http://<collector>:6565
export POLESTAR_ORG_ID=<24-hex-org-id>
# (선택) 등록 루프가 발급한 application target UUID
# export TRANSFER_TARGET_ID=... 등
cd core-banking && bash k8s/build-and-deploy.sh
```

Phase 1(docker build) → Phase 2(`ctr -n k8s.io images import`, kubeadm/containerd) → Phase 2.5(oracle-init-scripts/loadgen-scripts ConfigMap 을 `db/`, `loadgen/` 원본 파일에서 `--from-file` 생성) → Phase 3(envsubst 화이트리스트 치환 + apply) → Phase 4(rollout status, fail-fast — 실패 시 스크립트 즉시 종료).

## 한계 + 알려진 제약

- 비즈니스 로직은 LLM 자동 생성. `mvnw clean package` 컴파일 + 실 Oracle/Kafka 컨테이너 기동 후 `POST /api/transfers` 실제 호출까지 검증됨(구현 보고 참조). 도메인 정확성은 PR review 권장.
- api-service 는 DB 를 쓰지 않아 `DataSourceAutoConfiguration`/`HibernateJpaAutoConfiguration` 을 제외한다(shop-common 이 data-jpa 를 classpath 로 끌어오기 때문).
- `db/seed-all.sql` 로 시딩된 과거 이체/원장 이력은 계좌 현재 잔액과 소급 정합시키지 않는다(조회/집계 다양성이 목적, 잔액 재계산은 범위 밖).
- Kafka 이미지(`apache/kafka:3.9.0`)를 KRaft combined mode(`process.roles=broker,controller`)로 띄울 때 `advertised.listeners` 에 CONTROLLER 리스너도 명시해야 한다 — 누락하면 기동 자체가 실패한다(실기동 검증 중 발견, `k8s/11-kafka.yaml` 주석 참조).
