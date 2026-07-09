# core-banking

> 은행/금융 도메인의 RCA 테스트베드. 이체 트랜잭션의 row-lock 경합과 원장 비동기 반영을 관측 대상으로 노출한다. MariaDB 기반.

## Overview

계좌 이체(transfer)를 중심으로 한 코어뱅킹 마이크로서비스. api → account → transfer 동기 체인으로 이체를 실행하고, transfer 가 `accounts` 를 `FOR UPDATE` 로 lock 한 뒤 Redis Stream 으로 이벤트를 발행하면 ledger(worker)가 consumer group 으로 소비해 복식부기 원장에 반영한다. transfer-service 는 commerce-payment 가 호출하는 **cross-domain 진입점**이다.

- 도메인: 은행/금융
- 언어/프레임워크: Java 17 + Spring Boot 3.4.5, Maven 멀티모듈
- DB: MariaDB 11 (단일 `banking` DB)
- 비동기: Redis Streams (`transfer-events`)
- 서비스 개수: 4 (api / account / transfer / ledger) + shop-common
- 시연 가능 장애 패턴: lock-contention(이체 row-lock), slow query, connection pool 고갈, 비동기 backlog(원장 지연), cross-domain 경계 timeout

## Services

| service.name | 모듈 | 포트 | 역할 | 주요 호출 |
| --- | --- | --- | --- | --- |
| core-banking-api | api-service | 8080 | 이체 요청 진입(gateway) | → account (RestClient) |
| core-banking-account | account-service | 8081 | 계좌 조회·검증 | → transfer (RestClient), → MariaDB |
| core-banking-transfer | transfer-service | 8082 | 이체 트랜잭션(lock 집중) | → MariaDB(FOR UPDATE), → Redis publish. **← commerce-payment(cross-domain)** |
| core-banking-ledger | ledger-service | 8083 | 원장 반영(worker) | ← Redis consumer group, → MariaDB |

### 호출 그래프

```
client ──▶ nginx(30082) ──▶ api ──▶ account ──▶ transfer ──▶ MariaDB (accounts FOR UPDATE, transfers insert)
                                                     │
                                                     └─▶ Redis "transfer-events" ──▶ ledger ──▶ MariaDB (ledger_entries)

commerce-payment ─(FQDN, W3C traceparent)─▶ transfer  (동일 POST /api/transfers)
```

## Cross-domain 고정 인터페이스

transfer-service 가 노출하며 commerce-payment 가 호출한다(반대편은 별도 에이전트 구현). traceparent 는 OTel javaagent 가 자동 전파.

```
POST /api/transfers
Request : { "fromAccount": string, "toAccount": string, "amount": number, "orderId": string }
Response: { "transferId": string, "status": "COMPLETED" | "FAILED" }
```

FQDN: `transfer-service` 는 k8s Service `testbed-transfer.rca-testbed-banking.svc.cluster.local:8082`. commerce 쪽 `service-config` configmap 에 이 URL 을 신규 추가해야 한다.

## DB Schema

MariaDB 단일 `banking` DB. 테이블 3개. 전체 DDL + 시드: [`db/init.sql`](db/init.sql).

### `accounts`
| 컬럼 | 타입 | PK |
|---|---|---|
| id | VARCHAR(64) | ✓ |
| holder | VARCHAR(128) | |
| balance | DECIMAL(18,2) | |
| status | VARCHAR(16) | | (ACTIVE / FROZEN / CLOSED)

### `transfers`
| 컬럼 | 타입 | PK |
|---|---|---|
| id | BIGINT AUTO_INCREMENT | ✓ |
| transfer_ref | VARCHAR(64) UNIQUE | |
| from_account | VARCHAR(64) | |
| to_account | VARCHAR(64) | |
| amount | DECIMAL(18,2) | |
| status | VARCHAR(16) | | (COMPLETED / FAILED)
| order_id | VARCHAR(64) | |

### `ledger_entries`
| 컬럼 | 타입 | PK |
|---|---|---|
| id | BIGINT AUTO_INCREMENT | ✓ |
| transfer_ref | VARCHAR(64) | |
| account_id | VARCHAR(64) | |
| direction | VARCHAR(8) | | (DEBIT / CREDIT)
| amount | DECIMAL(18,2) | |

## 관측 태깅 (식별자 계약 §3)

- APM `service.name`: deployment env `OTEL_SERVICE_NAME=core-banking-<svc>` (등록 application target `meta.service_name` 과 정확일치).
- `OTEL_RESOURCE_ATTRIBUTES=lucida.organizationId=${POLESTAR_ORG_ID},lucida.groupId=core-banking,lucida.target_id=${<SVC>_TARGET_ID}`.
- OTel javaagent 는 이미지에 굽지 않고 hostPath(`/opt/polestar10/apm`, 인프라 공급)를 `JAVA_TOOL_OPTIONS` 로 주입.
- DB/pod/host 의 `lucida.target_id` 는 collector(db_poll/kcm/sms)가 emit — 앱이 넣지 않는다.
- MariaDB 는 DPM `db_poll` 로 engine=mariadb 등록. NodePort 30307(외부 접속).

## Failure Scenarios (RCA 검증용)

앱에는 injection 엔드포인트를 두지 않는다. 실제 주입은 `rca-scenario-runner` 가 담당.

- **lock-contention** — 동시 이체가 같은 계좌를 대상으로 `SELECT ... FOR UPDATE` 경합. `kubectl exec` 로 MariaDB 에 장기 보유 lock 주입.
- **slow query** — transfers/ledger_entries 대상 slow query 주입.
- **connection pool 고갈** — 이체 부하로 HikariCP 고갈.
- **비동기 backlog** — ledger consumer 지연 시 `transfer-events` stream backlog.
- **cross-domain timeout** — commerce-payment → transfer 경계에서 지연/504.

## Build & Deploy

### 로컬 (Docker Compose)

```bash
docker-compose -f docker-compose.dev.yml up --build
```

### 타겟 서버 (K3s)

```bash
export OTLP_ENDPOINT=http://<collector>:6565
export POLESTAR_ORG_ID=<24-hex-org-id>
# (선택) 등록 루프가 발급한 application target UUID
# export TRANSFER_TARGET_ID=... 등
cd core-banking && bash k8s/build-and-deploy.sh
```

4단계: docker build → cluster image import(k3d/native 자동감지) → envsubst 화이트리스트 치환 + apply → rollout status.

## 한계 + 알려진 제약

- 비즈니스 로직은 LLM 자동 생성. `mvnw clean package` 컴파일까지 검증되며 도메인 정확성은 PR review 권장.
- api-service 는 DB 를 쓰지 않아 `DataSourceAutoConfiguration`/`HibernateJpaAutoConfiguration` 을 제외한다(shop-common 이 data-jpa 를 classpath 로 끌어오기 때문).
