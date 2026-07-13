# Commerce

전자상거래 도메인 RCA 테스트베드. 5개의 Spring Boot 서비스로 구성된 주문 처리 시스템으로, 3-hop 호출 체인(`order → product → inventory`)을 통해 다단계 서비스 간 통신을 구현합니다.

(기존 전자담배 쇼핑몰 예제 프로젝트를 커머스 일반 도메인으로 리네이밍한 것 — 코드 골격·DB 스키마·시드 데이터는 그대로 승계.)

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
├── commerce-common/                # 공통 모듈 (DTO, 예외, BaseEntity)
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
- OTel javaagent 는 이미지에 굽지 않고 hostPath(`/opt/polestar10/apm`, 인프라 공급)를 `JAVA_TOOL_OPTIONS` 로 주입.
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

## 지속 부하 생성기 (loadgen)

> 이 절 이전의 README 본문(아키텍처·Services·DB 구조 등)은 최초 5서비스 시절 그대로이며
> 3a/3b/4번 증분(user/cart/pricing/shipping/api-gateway 신설, checkout 오케스트레이션,
> 배치 4종, Kafka 전환)이 반영되지 않은 상태다 — 이번 5번 증분은 loadgen 절만 추가했고
> 나머지 README 갱신은 범위 밖이라 손대지 않았다(발견만 남겨둔다).

`commerce/loadgen/`은 시나리오(장애 주입)와 별개로 상시 동작하는 baseline 부하 생성기다
(`docs/spec-testbed-expansion.md` §8). rca-scenario-runner가 장애를 주입하는 동안에도,
그리고 평상시에도 끊김 없이 현실적인 사용자 여정을 만들어 "평소 데이터"가 계속 쌓이게 한다.

- **진입점**: api-gateway(`GATEWAY_URL`, 기본 `http://testbed-gateway:8089`) 하나뿐이다.
  단, cross-domain 이체 여정만 게이트웨이를 거치지 않고 core-banking transfer API를
  직접 호출한다(게이트웨이가 `/api/transfers`를 프록시하지 않음).
- **여정 가중**: 브라우징 65% / 검색 15% / 장바구니 10% / 체크아웃 8% / cross-domain 이체 2%.
  체크아웃·장바구니는 flagship 상품(id 1~16, product/pricing/inventory 3개 서비스에 걸쳐
  정합된 시드)만 사용하고, 브라우징 상세 조회는 대량 시드 상품(최대 id 3000)까지 포함한다.
- **RPS 프로파일**: `commerce/loadgen/entrypoint.sh`가 매시 KST 시각 기준 코사인 근사
  diurnal 곡선으로 목표 RPS를 계산해 k6 constant-arrival-rate로 1시간씩 반복 실행한다.
- **끄는 법**: `kubectl -n rca-testbed-commerce scale deployment testbed-loadgen --replicas=0`
  (또는 `kubectl delete -f k8s/40-loadgen.yaml`).

### 환경변수 (loadgen)

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `GATEWAY_URL` | http://testbed-gateway:8089 | api-gateway 진입점 |
| `BANKING_TRANSFER_URL` | http://testbed-transfer.rca-testbed-banking.svc.cluster.local:8082 | cross-domain 이체 직접 호출 대상 |
| `PEAK_RPS` | 10 | 낮 피크(16시경) 목표 RPS |
| `TROUGH_RPS` | 2 | 새벽 저점(4시경) 목표 RPS |
| `LOADGEN_SEED` | 42 | mulberry32 PRNG 시드 — 여정/유저/상품 선택 재현성 |
| `PRE_ALLOCATED_VUS` / `MAX_VUS` | 20 / 50 | k6 constant-arrival-rate VU 풀 크기 |

로컬에서 스크립트만 검증할 때:

```bash
docker run --rm -v "$(pwd)/loadgen:/scripts" grafana/k6 inspect /scripts/script.js
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
| [plopvape-pg-mock](https://github.com/BangSungjoon/plopvape-pg-mock) | 외부 PG API 시뮬레이션 서버 (FastAPI) — 원본 전자담배 쇼핑몰 예제 프로젝트에서 승계 |

## License

MIT
