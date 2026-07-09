# Commerce

전자상거래 도메인 RCA 테스트베드. 5개의 Spring Boot 서비스로 구성된 주문 처리 시스템으로, 3-hop 호출 체인(`order → product → inventory`)을 통해 다단계 서비스 간 통신을 구현합니다.

(기존 `plopvape-shop`(전자담배 쇼핑몰)을 커머스 일반 도메인으로 리네이밍한 것 — 코드 골격·DB 스키마·시드 데이터는 그대로 승계.)

## 아키텍처

```
Client
  │
  ▼
order-service (:8080)
  ├──▶ product-service (:8081)
  │       └──▶ inventory-service (:8082)   ← 3-hop 체인
  ├──▶ payment-service (:8083)
  │       ├──▶ pg-mock-server (:8190)      ← 외부 PG API
  │       └──▶ core-banking-transfer        ← cross-domain 이체 API
  └──▶ Redis Streams ("order-events")
          └──▶ notification-service (:8084) ← 비동기 소비
```

### 동기 + 비동기 혼합

- **동기 (REST)**: order → product → inventory, order → payment, payment → core-banking-transfer(cross-domain)
- **비동기 (Redis Streams)**: order → notification

## 기술 스택

| 항목 | 기술 |
|------|------|
| Language | Java 17 |
| Framework | Spring Boot 3.4.5 |
| Build | Maven (multi-module) |
| DB | PostgreSQL 16 (스키마 분리) |
| Cache/Queue | Redis 7 (Streams) |
| HTTP Client | Spring RestClient |
| ORM | Spring Data JPA + Hibernate |

## 프로젝트 구조

```
commerce/
├── pom.xml                        # Parent POM
├── shop-common/                   # 공통 모듈 (DTO, 예외, BaseEntity)
├── order-service/                 # :8080 — 주문 생성/조회 허브
├── product-service/               # :8081 — 상품 목록/검색
├── inventory-service/             # :8082 — 재고 관리 (SELECT FOR UPDATE)
├── payment-service/               # :8083 — 결제 처리 + cross-domain 이체 호출
├── notification-service/          # :8084 — 알림 (Redis Streams consumer)
├── db/                            # init schemas + seed
├── k8s/                           # K8s manifest + build-and-deploy.sh
└── docker-compose.dev.yml         # 로컬 개발용 (PostgreSQL + Redis)
```

## Services

| service.name | 모듈 | 포트 | 역할 | 주요 호출 |
| --- | --- | --- | --- | --- |
| commerce-order | order-service | 8080 | 주문 생성, 재고 확인, 결제 이벤트 발행 | → product, → inventory(RestClient), → PostgreSQL, → Redis `order-events` publish |
| commerce-product | product-service | 8081 | 상품 조회 | → PostgreSQL |
| commerce-inventory | inventory-service | 8082 | 재고 차감(`SELECT FOR UPDATE` lock 대상) | → PostgreSQL |
| commerce-payment | payment-service | 8083 | 결제, 외부 PG mock 호출, cross-domain 이체 | → payment mock, → **core-banking-transfer(cross-domain)** |
| commerce-notification | notification-service | 8084 | Redis `order-events` 소비(worker, DB 미사용) | ← Redis consumer group |

결제 실패 시 order-service 가 `POST /api/inventory/release` 를 호출하여 재고를 자동 복구합니다.

## DB 구조

PostgreSQL 1인스턴스, 4개 스키마 분리:

```
PostgreSQL (commerce)
├── order_schema      → orders, order_items
├── product_schema    → products, categories
├── inventory_schema  → inventory
└── payment_schema    → payments, payment_logs
```

### 초기 데이터

- 카테고리 4개
- 상품 16개 (카테고리별 4개)
- 재고: 상품별 30~500개

## Cross-domain 호출

**commerce-payment → core-banking-transfer**. namespace 가 분리(`rca-testbed-commerce` / `rca-testbed-banking`)되어 있어 FQDN 으로 호출한다.

```
POST /api/transfers  →  testbed-transfer.rca-testbed-banking.svc.cluster.local:8082
```

`service-config` configmap 의 `BANKING_TRANSFER_URL` 로 주입된다. RestClient + OTel javaagent 사용 시 W3C traceparent 가 자동 전파되어 같은 trace 로 이어진다.

## 관측 태깅 (식별자 계약)

- APM `service.name`: deployment env `OTEL_SERVICE_NAME=commerce-<svc>` (등록 application target `meta.service_name` 과 정확일치).
- `OTEL_RESOURCE_ATTRIBUTES=lucida.organizationId=${POLESTAR_ORG_ID},lucida.groupId=commerce,lucida.target_id=${<SVC>_TARGET_ID}`.
- OTel/WPM javaagent 는 이미지에 굽지 않고 hostPath(`/opt/polestar10/{apm,wpm}`)를 `JAVA_TOOL_OPTIONS` 로 주입.
- DB/pod/host 의 `lucida.target_id` 는 collector(db_poll/kcm/sms)가 emit — 앱이 넣지 않는다.

## K8s 배포

- Namespace: `rca-testbed-commerce`
- 이미지: `commerce-<svc>:latest` (`imagePullPolicy: Never`)
- PostgreSQL 외부 NodePort(DPM 접속용): `30432`
- nginx 외부 NodePort(클라이언트 진입): `30080`

```bash
export OTLP_ENDPOINT=http://<collector>:6565
export POLESTAR_ORG_ID=<24-hex-org-id>
cd commerce && bash k8s/build-and-deploy.sh
```

## 시작하기 (로컬 개발)

### 사전 요구사항

- Java 17+
- Docker & Docker Compose

### 1. 인프라 기동

```bash
docker compose -f docker-compose.dev.yml up -d
```

### 2. 빌드

```bash
./mvnw clean package -DskipTests
```

### 3. 서비스 기동

서비스 의존 순서에 맞춰 기동합니다:

```bash
# 1. 리프 서비스 (의존 없음)
java -jar inventory-service/target/inventory-service-1.0.0.jar &
java -jar payment-service/target/payment-service-1.0.0.jar &
java -jar notification-service/target/notification-service-1.0.0.jar &

# 2. 중간 서비스
java -jar product-service/target/product-service-1.0.0.jar &

# 3. 허브 서비스
java -jar order-service/target/order-service-1.0.0.jar &
```

### 4. 테스트

```bash
# 상품 목록 조회
curl http://localhost:8081/api/products

# 주문 생성 (3-hop 체인 동작)
curl -X POST http://localhost:8080/api/orders \
  -H "Content-Type: application/json" \
  -d '{"customerName":"김철수","customerEmail":"chulsoo@test.com","items":[{"productId":1,"quantity":1},{"productId":5,"quantity":2}]}'
```

### 환경변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `DB_HOST` | localhost | PostgreSQL 호스트 |
| `DB_PORT` | 5432 | PostgreSQL 포트 |
| `REDIS_HOST` | localhost | Redis 호스트 |
| `REDIS_PORT` | 6379 | Redis 포트 |
| `PRODUCT_SERVICE_URL` | http://localhost:8081 | product-service URL |
| `PAYMENT_SERVICE_URL` | http://localhost:8083 | payment-service URL |
| `INVENTORY_SERVICE_URL` | http://localhost:8082 | inventory-service URL |
| `PG_API_URL` | http://192.168.200.109:8190 | PG Mock Server URL |
| `BANKING_TRANSFER_URL` | http://testbed-transfer.rca-testbed-banking.svc.cluster.local:8082 | core-banking cross-domain 이체 API |

## Failure Scenarios (RCA 검증용)

앱에는 injection 엔드포인트를 두지 않는다. 실제 주입은 `rca-scenario-runner` 가 담당.

- 재고 row-lock 경합 (`inventory` 테이블 `SELECT FOR UPDATE`)
- 재고 slow query
- payment 외부 mock timeout/5xx
- 비동기 backlog(Redis `order-events`)
- connection pool 고갈, pod CPU throttle
- cross-domain timeout (commerce-payment → core-banking-transfer)

## 관련 프로젝트

| 프로젝트 | 설명 |
|----------|------|
| [plopvape-pg-mock](https://github.com/BangSungjoon/plopvape-pg-mock) | 외부 PG API 시뮬레이션 서버 (FastAPI) — 원본 plopvape 프로젝트에서 승계 |

## License

MIT
