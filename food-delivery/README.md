# Food Delivery

음식배달 도메인 RCA 테스트베드. 5개의 Spring Boot 서비스로 구성된 주문-배차-결제-알림 시스템.
MySQL 8.0 + Kafka(KRaft 단일 브로커) 기반. commerce 플래그십에서 확정한 공통 패턴(outbox+Kafka,
Resilience4j, 상주 배치, 읽기 API 보강, 대량 시드, 지속 부하 생성기)을 이식했다.

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
  │
  ├──▶ [outbox] food.orders ──▶ notify-service (:8084)
  ├──▶ [outbox] food.payments (payment-service 발행) ──▶ notify-service
  └──▶ [outbox] food.dispatch (dispatch-service 발행) ──▶ notify-service
```

### 동기 + 비동기 혼합

- **동기 (REST, Resilience4j 적용)**: order → restaurant / dispatch / payment. payment 계열
  호출(payment→PG mock)만 `max-attempts=2`(중복결제 위험 완화), 나머지는 3회 지수 백오프.
- **비동기 (Kafka, outbox 패턴)**: order/payment/dispatch 각 서비스가 자신의 트랜잭션과 같은
  커밋 안에서 outbox 테이블에 이벤트를 남기고, `OutboxRelay`(2초 주기 폴링)가 Kafka로
  at-least-once 발행한다. notify-service 는 `food.orders`/`food.payments`/`food.dispatch`
  세 토픽을 모두 구독해 알림 발송을 시뮬레이션한다(Redis Streams 는 완전히 제거됨).

## 기술 스택

| 항목 | 기술 |
|------|------|
| Language | Java 17 |
| Framework | Spring Boot 3.4.5 |
| Build | Maven (multi-module) |
| DB | MySQL 8.0 (단일 flat DB, 서비스별 스키마 분리 없음) |
| Messaging | Kafka 3.9 (KRaft 단일 브로커) — Redis Streams 대체(제거) |
| Resilience | Resilience4j (CircuitBreaker + Retry) |
| HTTP Client | Spring RestClient |
| ORM | Spring Data JPA + Hibernate |
| Load Testing | k6 (지속 부하 생성기, `loadgen/`) |

## 프로젝트 구조

```
food-delivery/
├── pom.xml                        # Parent POM
├── shop-common/                   # 공통 모듈 (DTO, 예외, outbox 제네릭 베이스)
├── order-service/                 # :8080 — 주문 허브 + stale order cleanup 배치
├── restaurant-service/            # :8081 — 매장·메뉴 조회 + 인기메뉴 집계 배치
├── dispatch-service/               # :8082 — 배차 + 배차만료 배치
├── payment-service/                # :8083 — 결제, 외부 PG mock 호출 + 정산 배치
├── notify-service/                 # :8084 — Kafka 3토픽 소비(worker)
├── db/                            # init.sql (MySQL 8.0 15테이블 스키마 + 대량 시드)
├── loadgen/                       # k6 지속 부하 생성기 (script.js + entrypoint.sh)
├── k8s/                           # K8s manifest + build-and-deploy.sh
└── docker-compose.dev.yml         # 로컬 개발용 (MySQL + Kafka)
```

## Services

| service.name | 모듈 | 포트 | 역할 | 주요 호출 |
| --- | --- | --- | --- | --- |
| food-delivery-order | order-service | 8080 | 주문 허브 + stale order cleanup 배치 | → restaurant, → dispatch, → payment(RestClient, Resilience4j), → MySQL, → outbox→Kafka(food.orders) |
| food-delivery-restaurant | restaurant-service | 8081 | 매장·메뉴 조회 + 인기메뉴 집계 배치 | → MySQL |
| food-delivery-dispatch | dispatch-service | 8082 | 배차 + 배차만료 배치 | → MySQL, → outbox→Kafka(food.dispatch) |
| food-delivery-payment | payment-service | 8083 | 결제, 외부 PG mock 호출 + 정산 배치 | → external-pg-mock, → outbox→Kafka(food.payments) |
| food-delivery-notify | notify-service | 8084 | Kafka 3토픽 소비(worker, DB 미사용) | ← food.orders / food.payments / food.dispatch |

## 상주 배치

| 배치 | 서비스 | 주기 | 하는 일 |
| --- | --- | --- | --- |
| OutboxRelay (order/payment/dispatch 3종) | 각 서비스 | 2초 | outbox 테이블 폴링 → Kafka 발행 → publishedAt 마킹 |
| 배차만료 전이 | dispatch-service | 30초 | ETA 지난 ASSIGNED → DELIVERED 전이 + dispatch_events 기록 |
| Stale order cleanup | order-service | 1시간(`order.cleanup.interval-ms`) | `retention-days`(기본 30일) 넘긴 PENDING 주문 → CANCELLED |
| SettlementBatch | payment-service | 1시간(`settlement.poll-interval-ms`) | 미정산 완료 결제 집계 → settlement_summary 기록 |
| PopularMenuBatch | restaurant-service | 1시간(`menu.popularity.interval-ms`) | 최근 `lookback-days`(기본 7일) 주문 집계 → menu_popularity_summary 재계산 |

## DB 구조

MySQL 8.0 단일 `fooddelivery` DB. 테이블 15개. 전체 DDL + 대량 시드: [`db/init.sql`](db/init.sql).

- `restaurants`, `menu_categories`, `menus` — 매장·메뉴(매장 23개: 데모 3 + 대량 20)
- `customers`, `riders` — 고객/배달원 디렉토리(느슨한 참조, FK 없음)
- `orders`, `order_items`, `order_outbox_events` — 주문
- `dispatches`, `dispatch_events`, `dispatch_outbox_events` — 배차 + 상태 전이 이력
- `payments`, `payment_outbox_events` — 결제
- `settlement_summary`, `menu_popularity_summary` — 배치 산출 집계(시드 단계엔 비어 있고 런타임 첫 배치가 채움)

대량 시드: 고객 2,000명, 배달원 150명, 과거 주문 20,000건(최근 90일, 점심/저녁 피크 + 주말 가중
분포), 주문당 1~3개 아이템, 배차/결제 이력 동반 생성. MySQL 재귀 CTE 기반, `RAND(42)` 로 세션
시드 고정(완전한 바이트 재현이 아니라 분포 특성 재현이 목적).

## 읽기 API (§7 신규)

기존 엔드포인트 시그니처는 전부 유지(배열 응답, 하위호환). 신규 엔드포인트는 모두 배열 응답
+ `page`/`size` nullable Integer 패턴(둘 다 없으면 무제한 조회).

- restaurant-service: `GET /api/restaurants?region=&status=&page=&size=`, `GET /api/restaurants/{id}/popular-menu`
- order-service: `GET /api/orders?customerId=&status=&restaurantId=&from=&to=&page=&size=`, `GET /api/orders/stats/daily?days=30`
- dispatch-service: `GET /api/deliveries?status=&page=&size=`, `GET /api/deliveries/{id}/events`
- payment-service: `GET /api/payments?status=&from=&to=&page=&size=`, `GET /api/payments/settlements?page=&size=`

## 지속 부하 생성기 (loadgen)

`loadgen/` — k6 기반, api-gateway 가 없어 각 서비스를 k8s Service DNS로 직접 호출한다.
여정 가중: 매장/메뉴 브라우징 55% / 지역 검색 15% / 주문 생성 20% / 배달 상태 조회 10%.
`entrypoint.sh` 가 KST 시각 기준 diurnal 곡선으로 목표 RPS를 계산해 매시 새 k6 run 을 반복한다
(기본 `PEAK_RPS=6` / `TROUGH_RPS=1`, commerce 대비 저율).

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
- kubeadm containerd 이미지 임포트(`sudo ctr -n k8s.io images import -`), Phase 2.5 에서
  `db/init.sql`·`loadgen/*` 로부터 ConfigMap 을 동적 생성(정본-사본 드리프트 방지), rollout
  fail-fast(`|| true` 없음).

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
  -d '{"customerId":"cust-001","restaurantId":1,"items":[{"menuId":1,"qty":2,"unitPrice":7000}]}'
```

### 환경변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `DB_HOST` | mysql | MySQL 호스트 |
| `DB_PORT` | 3306 | MySQL 포트 |
| `DB_NAME` | fooddelivery | DB명 |
| `KAFKA_BOOTSTRAP_SERVERS` | kafka:9092 | Kafka 브로커 주소 |
| `ORDER_SERVICE_URL` | http://testbed-order:8080 | order-service URL |
| `RESTAURANT_SERVICE_URL` | http://testbed-restaurant:8081 | restaurant-service URL |
| `DISPATCH_SERVICE_URL` | http://testbed-dispatch:8082 | dispatch-service URL |
| `PAYMENT_SERVICE_URL` | http://testbed-payment:8083 | payment-service URL |
| `PG_API_URL` | http://testbed-external-pg-mock:9090 | PG Mock Server URL |

## Failure Scenarios (RCA 검증용)

앱에는 injection 엔드포인트를 두지 않는다. 실제 주입은 `rca-scenario-runner` 가 담당.

- matching/조회 slow query (MySQL)
- Kafka consumer lag / 브로커 장애로 인한 notify 지연
- 외부 mock timeout/5xx (`external-pg-mock`, mockserver 로 동적 expectation 주입)
- MySQL lock/wait, connection pool 고갈
- 배차 포화(courier capacity exhausted, 503)

## 한계 + 알려진 제약

- 비즈니스 로직은 LLM 자동 생성. `mvnw clean package` 컴파일까지 검증되며 도메인 정확성은 PR review 권장.
- MySQL 대량 시드는 `RAND(42)` 세션 시드로 재현성을 근사하지만, 완전한 바이트 단위 재현은 보장하지 않는다(분포 특성 재현이 목적).
