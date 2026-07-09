# Food Delivery

음식배달 도메인 RCA 테스트베드. 5개의 Spring Boot 서비스로 구성된 주문-배차-결제-알림 시스템. 기존 PostgreSQL 기반 구성을 **MySQL 8.0 으로 전환**하고, 기존엔 없던 **Redis 비동기 경로(notify-service 신규)**를 추가했다.

## 아키텍처

```
Client
  │
  ▼
order-service (:8080)
  ├──▶ restaurant-service (:8081)   ← 매장·메뉴 조회
  ├──▶ dispatch-service (:8082)     ← 배차
  ├──▶ payment-service (:8083)
  │       └──▶ external-pg-mock (:9090)   ← 외부 PG API (mockserver)
  └──▶ Redis Streams
          └──▶ notify-service (:8084) ← 비동기 소비 (신규)
```

### 동기 + 비동기 혼합

- **동기 (REST)**: order → restaurant, order → dispatch, order → payment
- **비동기 (Redis Streams)**: order → notify (신규 추가 경로)

## 기술 스택

| 항목 | 기술 |
|------|------|
| Language | Java 17 |
| Framework | Spring Boot 3.4.5 |
| Build | Maven (multi-module) |
| DB | MySQL 8.0 (전환: 기존 PostgreSQL → MySQL) |
| Cache/Queue | Redis 7 (Streams, 신규) |
| HTTP Client | Spring RestClient |
| ORM | Spring Data JPA + Hibernate |

## 프로젝트 구조

```
food-delivery/
├── pom.xml                        # Parent POM
├── shop-common/                   # 공통 모듈 (DTO, 예외, BaseEntity)
├── order-service/                 # :8080 — 주문 허브
├── restaurant-service/            # :8081 — 매장·메뉴 조회
├── dispatch-service/              # :8082 — 배차
├── payment-service/                # :8083 — 결제, 외부 PG mock 호출
├── notify-service/                 # :8084 — Redis 소비(worker, 신규)
├── db/                            # init.sql (MySQL 8.0 스키마 + 시드)
├── k8s/                           # K8s manifest + build-and-deploy.sh
└── docker-compose.dev.yml         # 로컬 개발용 (MySQL + Redis)
```

## Services

| service.name | 모듈 | 포트 | 역할 | 주요 호출 |
| --- | --- | --- | --- | --- |
| food-delivery-order | order-service | 8080 | 주문 허브 | → restaurant, → dispatch, → payment(RestClient), → MySQL, → Redis publish |
| food-delivery-restaurant | restaurant-service | 8081 | 매장·메뉴 조회 | → MySQL |
| food-delivery-dispatch | dispatch-service | 8082 | 배차 | → MySQL |
| food-delivery-payment | payment-service | 8083 | 결제, 외부 PG mock 호출 | → external-pg-mock |
| food-delivery-notify | notify-service(신규) | 8084 | Redis 소비(worker, DB 미사용) | ← Redis consumer group |

## DB 구조

MySQL 8.0 단일 `fooddelivery` DB. 테이블 6개. 전체 DDL + 시드: [`db/init.sql`](db/init.sql).

- `restaurants`, `menus` — 매장·메뉴
- `orders`, `order_items` — 주문
- `dispatches` — 배차 (order_id 에 FK 없음 — cross-service eventual consistency)
- `payments` — 결제 (order_id 에 FK 없음)

초기 데이터: 매장 3개(강남/홍대/잠실), 매장별 메뉴 5개.

## 관측 태깅 (식별자 계약)

- APM `service.name`: deployment env `OTEL_SERVICE_NAME=food-delivery-<svc>` (등록 application target `meta.service_name` 과 정확일치).
- `OTEL_RESOURCE_ATTRIBUTES=lucida.organizationId=${POLESTAR_ORG_ID},lucida.groupId=food-delivery,lucida.target_id=${<SVC>_TARGET_ID}`.
- OTel javaagent 는 이미지에 굽지 않고 hostPath(`/opt/polestar10/apm`, 인프라 공급)를 `JAVA_TOOL_OPTIONS` 로 주입.
- DB/pod/host 의 `lucida.target_id` 는 collector(db_poll/kcm/sms)가 emit — 앱이 넣지 않는다.

## K8s 배포

- Namespace: `rca-testbed-food`
- 이미지: `food-delivery-<svc>:latest` (`imagePullPolicy: Never`)
- MySQL 외부 NodePort(DPM 접속용): `30236`
- 클라이언트 진입용 nginx/gateway 없음 — order-service(`testbed-order:8080`)에 클러스터 내부 ClusterIP 로 직접 접근(port-forward 또는 내부 호출 전용)

```bash
export OTLP_ENDPOINT=http://<collector>:6565
export POLESTAR_ORG_ID=<24-hex-org-id>
cd food-delivery && bash k8s/build-and-deploy.sh
```

## 시작하기 (로컬 개발)

### 사전 요구사항

- Java 17+
- Docker & Docker Compose

### 1. 인프라 + 서비스 기동

```bash
docker compose -f docker-compose.dev.yml up -d --build
```

### 2. 빌드 (직접 실행 시)

```bash
./mvnw clean package -DskipTests
```

### 3. 테스트

```bash
# 매장 목록 조회
curl http://localhost:8081/api/restaurants

# 주문 생성
curl -X POST http://localhost:8080/api/orders \
  -H "Content-Type: application/json" \
  -d '{"customerId":"cust-001","restaurantId":1,"items":[{"menuId":1,"qty":2}]}'
```

### 환경변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `DB_HOST` | mysql | MySQL 호스트 |
| `DB_PORT` | 3306 | MySQL 포트 |
| `DB_NAME` | fooddelivery | DB명 |
| `REDIS_HOST` | redis | Redis 호스트 |
| `REDIS_PORT` | 6379 | Redis 포트 |
| `ORDER_SERVICE_URL` | http://testbed-order:8080 | order-service URL |
| `RESTAURANT_SERVICE_URL` | http://testbed-restaurant:8081 | restaurant-service URL |
| `DISPATCH_SERVICE_URL` | http://testbed-dispatch:8082 | dispatch-service URL |
| `PAYMENT_SERVICE_URL` | http://testbed-payment:8083 | payment-service URL |
| `PG_API_URL` | http://testbed-external-pg-mock:9090 | PG Mock Server URL |

## Failure Scenarios (RCA 검증용)

앱에는 injection 엔드포인트를 두지 않는다. 실제 주입은 `rca-scenario-runner` 가 담당.

- matching/조회 slow query (MySQL)
- 비동기 backlog (Redis, notify-service 소비 지연)
- 외부 mock timeout/5xx (`external-pg-mock`, mockserver 로 동적 expectation 주입)
- MySQL lock/wait, connection pool 고갈

## 한계 + 알려진 제약

- 비즈니스 로직은 LLM 자동 생성. `mvnw clean package` 컴파일까지 검증되며 도메인 정확성은 PR review 권장.
