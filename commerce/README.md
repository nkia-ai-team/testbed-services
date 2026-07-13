# Commerce

전자상거래 도메인 RCA 테스트베드. api-gateway(BFF) 뒤에 9개 Spring Boot 서비스가 붙어 있는
진짜 MSA — 5-hop 이상의 동기 호출 체인(checkout: `gateway → order → cart → pricing → product
→ inventory → payment`), Kafka 이벤트 백본 + outbox 패턴, 상주 배치 4종, 상시 부하 생성기
(`loadgen`, k6)로 평상시 baseline 트래픽까지 갖춘 구성이다.

(기존 전자담배 쇼핑몰 예제 프로젝트를 커머스 일반 도메인으로 리네이밍한 것에서 출발 —
`docs/spec-testbed-expansion.md`의 증분(3a/3b/4번/5번/6번)을 거쳐 지금 형태가 됐다.)

## 아키텍처

```
Client
  │
  ▼
nginx (:30080, 유일한 외부 진입점)
  │
  ▼
api-gateway (:8089) — 엣지 라우팅 + 쓰기요청 토큰 검증 + BFF 집계
  ├─▶ /api/users/**      → user-service (:8085)
  ├─▶ /api/carts/**      → cart-service (:8086) → Redis(캐시, DB fallback)
  ├─▶ /api/pricing/**    → pricing-service (:8087)
  ├─▶ /api/shipments/**  → shipping-service (:8088)
  ├─▶ /api/products/**, /api/categories/** → product-service (:8081) → inventory-service(재고 조회)
  ├─▶ /api/payments/**   → payment-service (:8083)
  └─▶ /api/orders/**     → order-service (:8080)
        ├─ POST /api/orders/checkout (신규, 5-hop):
        │    cart 조회 → pricing 견적 → product→inventory 예약 → payment 결제 → cart 비우기(best-effort)
        └─ POST /api/orders (기존, 시나리오 러너 전용):
             items 직접 지정 → product→inventory 예약 → payment 결제  (3-hop, 원형 그대로 유지)

payment-service ──cross-domain──▶ core-banking-transfer (namespace 분리, FQDN)
loadgen(k6)     ──cross-domain 직접──▶ core-banking-transfer (게이트웨이 우회)

Kafka 이벤트 백본(commerce.orders/payments/inventory/shipping/user):
  order/payment/inventory/shipping/user → outbox 릴레이 → Kafka → {order(결제상태 보정),
  shipping(주문→배송 생성), notification(알림)} 구독
```

### 동기 + 비동기 혼합

- **동기 (REST, Resilience4j timeout+retry+circuit breaker)**: 게이트웨이 프록시 전 구간,
  order→{cart,pricing,product,inventory,payment}, product→inventory, payment→core-banking-transfer(cross-domain)
- **비동기 (Kafka, outbox 패턴)**: order/payment/inventory/shipping/user가 각자 스키마의
  `outbox_events` 테이블에 커밋하면 `OutboxRelay`(공용, `commerce-common`)가 주기적으로
  폴링해 Kafka로 발행 — DB 트랜잭션과 이벤트 발행을 원자화한다.
  - `commerce.orders` → shipping(배송 생성), notification
  - `commerce.payments` → order(결제 상태 보정), notification
  - `commerce.inventory`, `commerce.shipping`, `commerce.user` → notification 등

## 기술 스택

| 항목 | 기술 |
|------|------|
| Language | Java 17 |
| Framework | Spring Boot 3.4.5 |
| Build | Maven (multi-module) |
| DB | PostgreSQL 16 (서비스별 스키마 분리) |
| Cache | Redis 7 (cart-service 캐시 전용) |
| Event backbone | Kafka (KRaft, outbox 패턴) |
| Resilience | Resilience4j (circuit breaker/retry/bulkhead) |
| HTTP Client | Spring RestClient |
| ORM | Spring Data JPA + Hibernate |
| Load generator | k6 (constant-arrival-rate, diurnal 프로파일) |

## 프로젝트 구조

```
commerce/
├── pom.xml                        # Parent POM (11개 Java 모듈)
├── commerce-common/                # 공통 모듈 (DTO, 예외, BaseEntity, outbox)
├── api-gateway/                   # :8089 — BFF, DB 없음
├── order-service/                 # :8080 — 주문 생성/checkout 오케스트레이션
├── product-service/               # :8081 — 상품/카테고리/variants
├── inventory-service/             # :8082 — 재고 관리 + movements 원장
├── payment-service/                # :8083 — 결제 + cross-domain 이체 + settlement 배치
├── notification-service/          # :8084 — Kafka consumer (DB 없음)
├── user-service/                  # :8085 — 회원/주소/인증(opaque token)
├── cart-service/                  # :8086 — 장바구니(PG+Redis)
├── pricing-service/                # :8087 — 가격/프로모션/쿠폰 견적
├── shipping-service/               # :8088 — 배송(Kafka consume+outbox)
├── loadgen/                       # k6 스크립트(Java 모듈 아님) — 지속 부하 생성기
├── db/                             # init-schemas.sql(DDL 합본) + seed-all.sql(대량 시드)
├── k8s/                            # K8s manifest(00~40) + build-and-deploy.sh
└── docker-compose.dev.yml         # 로컬 개발용 (PostgreSQL + Redis + Kafka)
```

## Services

| service.name | 모듈 | 포트 | 역할 | DB |
| --- | --- | --- | --- | --- |
| commerce-gateway | api-gateway | 8089 | 엣지 라우팅, 쓰기요청 토큰 검증, BFF 집계(`/api/bff/users/{id}/overview`) | 없음 |
| commerce-order | order-service | 8080 | 주문 생성(기존)/checkout(신규 5-hop), 일별 통계 | order_schema |
| commerce-product | product-service | 8081 | 상품/카테고리/variants 조회 | product_schema |
| commerce-inventory | inventory-service | 8082 | 재고 차감(`SELECT FOR UPDATE`)/movements 원장/재조정 배치 | inventory_schema |
| commerce-payment | payment-service | 8083 | 결제, 외부 PG mock, cross-domain 이체, settlement 배치 | payment_schema |
| commerce-notification | notification-service | 8084 | Kafka(`commerce.orders`/`commerce.payments`) 소비 | 없음 |
| commerce-user | user-service | 8085 | 회원가입/로그인(opaque token)/주소 | user_schema |
| commerce-cart | cart-service | 8086 | 장바구니(Redis 캐시+DB fallback)/유휴 cart 정리 배치 | cart_schema + Redis |
| commerce-pricing | pricing-service | 8087 | 가격/프로모션/쿠폰 견적, 프로모션 캐시 갱신 배치 | pricing_schema |
| commerce-shipping | shipping-service | 8088 | 배송 생성(Kafka 구독)/상태 자동 전이 | shipping_schema |
| (loadgen) | loadgen | — | k6 지속 부하 생성기, Java 서비스 아님 | 없음 |

결제 실패 시 order-service가 `POST /api/inventory/release`를 호출해 재고를 자동 복구합니다
(기존 3-hop 경로·신규 checkout 경로 모두 동일 보상 로직 사용).

## 상주 배치 4종

| 배치 | 서비스 | 주기 | 하는 일 |
|---|---|---|---|
| 정산(settlement) | payment-service | 매시 정각 | 미정산 결제 집계 → `settlement_summary` 기록 → core-banking 이체 1건(cross-domain ②). 이체 실패해도 미정산 상태로 남겨 다음 주기 자동 재시도 |
| 재고 재조정(reconciliation) | inventory-service | 10분 | `inventory_movements` 최신 원장값 vs 현재 stock 대사, 불일치 시 WARN + 보정 ADJUST 기록 |
| 장바구니 만료 정리 | cart-service | 5분 | 24시간 무갱신 cart 삭제 + Redis 캐시 무효화 |
| 프로모션 캐시 갱신 | pricing-service | 매시 정각 | 활성 프로모션을 인메모리 캐시로 재적재(견적 계산이 DB 대신 이 캐시를 읽음) |

## DB 구조

PostgreSQL 1인스턴스, 8개 스키마 분리(서비스당 1개, api-gateway/notification-service는 DB 없음):

```
PostgreSQL (commerce)
├── order_schema      → orders, order_items, outbox_events
├── product_schema    → categories, products, product_variants
├── inventory_schema  → inventory, inventory_movements, outbox_events
├── payment_schema    → payments, settlement_summary, payment_logs, outbox_events
├── user_schema       → users, addresses, auth_tokens, outbox_events
├── cart_schema       → carts, cart_items
├── pricing_schema    → prices, promotions, coupons
└── shipping_schema   → shipments, shipment_events, outbox_events
```

### 초기 데이터 규모

`commerce/db/init-schemas.sql`(전체 DDL 합본, 앱 기동 없이도 단독 실행 가능) +
`commerce/db/seed-all.sql`(대량 시드, `generate_series` 기반) 기준:

| 대상 | 규모 |
|---|---|
| 유저 | 3,020명(데모 20 + 대량 3,000) |
| 상품 | 2,016개(flagship 16 + 대량 2,000) + variants 6,048개 |
| 과거 주문 | 50,000건 (order_items ~100,000, 최근 90일 diurnal+주말 가중 분포) |
| 결제/배송 | payments 50,000, shipments ~47,500, shipment_events ~137,000 |

임시 postgres 컨테이너에서 위 두 파일만으로 로드 시 **10초 내외**(4번 증분 실측). 대량 주문
시뮬레이션은 product/pricing/inventory 3개 서비스에 걸쳐 완전히 정합된 flagship 상품(id 1~16)
만 사용한다 — 대량 생성 상품 2,000개는 카탈로그 브라우징 표면 확장이 목적이라 가격/재고
데이터는 없다.

## 읽기 API 표면 (§7)

목록/검색/필터/집계 read API — 기존 엔드포인트 시그니처(응답 배열 형태 포함)는 전부
하위호환 유지, page/size는 Spring Data `Pageable`로 조회만 슬라이싱한다.

| 서비스 | 신규 엔드포인트 |
|---|---|
| product | `GET /api/products?category=&q=&page=&size=`(categoryId=/name=은 legacy alias), `GET /api/products/{id}/variants`, `GET /api/categories`, `GET /api/categories/{id}/products` |
| order | `GET /api/orders?userId=&status=&from=&to=&page=&size=`, `GET /api/orders/stats/daily?days=30` |
| inventory | `GET /api/inventory?lowStockOnly=&page=&size=`, `GET /api/inventory/{productId}/movements?page=&size=` |
| payment | `GET /api/payments?status=&from=&to=&page=&size=`, `GET /api/payments/settlements` |

api-gateway는 `/api/categories/**`(product CB 재사용), `/api/payments/**`(신규 payment CB)를
추가로 프록시한다.

## Cross-domain 호출

**두 경로**:

1. `commerce-payment → core-banking-transfer` (결제 시점 동기) — namespace 분리
   (`rca-testbed-commerce` / `rca-testbed-banking`)라 FQDN으로 호출.
   ```
   POST /api/transfers  →  testbed-transfer.rca-testbed-banking.svc.cluster.local:8082
   ```
2. `commerce-payment(settlement 배치) → core-banking-transfer` (매시 정산, ①과 같은 API 재사용).

`service-config` configmap의 `BANKING_TRANSFER_URL`로 주입된다. RestClient + OTel javaagent
사용 시 W3C traceparent가 자동 전파되어 같은 trace로 이어진다. loadgen의 cross-domain 이체
여정도 같은 API를 (게이트웨이를 거치지 않고) 직접 호출한다.

## 관측 태깅 (식별자 계약)

- APM `service.name`: deployment env `OTEL_SERVICE_NAME=commerce-<svc>` (등록 application target `meta.service_name` 과 정확일치).
- `OTEL_RESOURCE_ATTRIBUTES=lucida.organizationId=${POLESTAR_ORG_ID},lucida.groupId=commerce,lucida.target_id=${<SVC>_TARGET_ID}`.
- OTel javaagent 는 이미지에 굽지 않고 hostPath(`/opt/polestar10/apm`, 인프라 공급)를 `JAVA_TOOL_OPTIONS` 로 주입.
- DB/pod/host 의 `lucida.target_id` 는 collector(db_poll/kcm/sms)가 emit — 앱이 넣지 않는다.
- loadgen(k6)은 APM 계측 대상이 아니다(JVM이 아니라 javaagent 주입 불가) — 미결 항목(§8 참조).

## K8s 배포

- Namespace: `rca-testbed-commerce`
- 이미지: `commerce-<svc>:latest`(`imagePullPolicy: Never`, loadgen만 공개 이미지 `grafana/k6:latest` 그대로 사용)
- PostgreSQL 외부 NodePort(DPM 접속용): `30432`
- nginx 외부 NodePort(클라이언트 진입): `30080`

### manifest 목록 (`k8s/`, 번호순 적용)

| 파일 | 내용 |
|---|---|
| `00-namespace.yaml` | Namespace |
| `01-secrets.yaml` | postgres-secret |
| `02-configmaps.yaml` | `service-config`(DB/Redis/Kafka/서비스간 URL), `nginx-conf` |
| `10-postgres.yaml` | PostgreSQL StatefulSet(초기화 ConfigMap은 build-and-deploy.sh가 `db/*.sql`에서 동적 생성) |
| `11-redis.yaml` | Redis(cart 캐시) |
| `12-kafka.yaml` | Kafka(KRaft 단일 브로커, 3파티션) |
| `20`~`24` | order/product/inventory/payment/notification |
| `25`~`28` | user/cart/pricing/shipping |
| `29-api-gateway.yaml` | api-gateway |
| `30-nginx.yaml` | nginx reverse proxy(외부 진입점) |
| `40-loadgen.yaml` | loadgen(k6) — 스크립트는 `loadgen-scripts` ConfigMap(빌드 시 `loadgen/*`에서 동적 생성)으로 마운트 |

```bash
export OTLP_ENDPOINT=http://<collector>:6565
export POLESTAR_ORG_ID=<24-hex-org-id>
cd commerce && bash k8s/build-and-deploy.sh
```

`build-and-deploy.sh`가 하는 일: (1) 10개 Java 서비스 Docker 이미지 빌드(ARM 네이티브,
loadgen 제외) → (2) 클러스터(k3d/kubeadm 자동 감지) 이미지 임포트 → (2.5) `postgres-init-scripts`/
`loadgen-scripts` ConfigMap을 `db/*.sql`·`loadgen/*`에서 직접 생성(YAML에 내용을 베껴 넣지
않아 정본-사본 드리프트를 구조적으로 막는다) → (3) 나머지 manifest를 envsubst 치환 후 적용
→ (4) rollout 상태 확인.

## 지속 부하 생성기 (loadgen)

`commerce/loadgen/`은 시나리오(장애 주입)와 별개로 상시 동작하는 baseline 부하 생성기다
(`docs/spec-testbed-expansion.md` §8). rca-scenario-runner가 장애를 주입하는 동안에도,
그리고 평상시에도 끊김 없이 현실적인 사용자 여정을 만들어 "평소 데이터"가 계속 쌓이게 한다.

- **진입점**: api-gateway(`GATEWAY_URL`, 기본 `http://testbed-gateway:8089`) 하나뿐이다.
  단, cross-domain 이체 여정만 게이트웨이를 거치지 않고 core-banking transfer API를
  직접 호출한다(게이트웨이가 `/api/transfers`를 프록시하지 않음).
- **여정 가중**: 브라우징 65%(§7 category/q/page + variants/categories 포함) / 검색 15%(q=) /
  장바구니 10% / 체크아웃 8% / cross-domain 이체 2%. 체크아웃·장바구니는 flagship 상품
  (id 1~16)만 사용하고, 브라우징 목록/상세는 대량 시드 상품(최대 id 3000)까지 페이지네이션으로
  훑는다.
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

PostgreSQL(`db/init-schemas.sql`+`db/seed-all.sql` 자동 마운트), Redis, Kafka가 뜬다.

### 2. 빌드

```bash
./mvnw clean package -DskipTests
```

commerce-common + 10개 서비스 jar(loadgen은 Java 모듈이 아니라 빌드 대상 아님).

### 3. 서비스 기동

의존 순서(리프 → 중간 → 오케스트레이터 → 게이트웨이)에 맞춰 기동합니다:

```bash
# 1. 리프 서비스 (의존 없음)
java -jar inventory-service/target/inventory-service-1.0.0.jar &
java -jar notification-service/target/notification-service-1.0.0.jar &
java -jar user-service/target/user-service-1.0.0.jar &
java -jar pricing-service/target/pricing-service-1.0.0.jar &
java -jar shipping-service/target/shipping-service-1.0.0.jar &

# 2. 중간 서비스
java -jar product-service/target/product-service-1.0.0.jar &
java -jar payment-service/target/payment-service-1.0.0.jar &
java -jar cart-service/target/cart-service-1.0.0.jar &

# 3. 오케스트레이터
java -jar order-service/target/order-service-1.0.0.jar &

# 4. 게이트웨이(최후, DB 없음)
java -jar api-gateway/target/api-gateway-1.0.0.jar &
```

### 4. 테스트

```bash
# 상품 목록 조회(§7: category/q/page/size)
curl "http://localhost:8089/api/products?q=Air&page=0&size=5"

# 기존 주문 생성(3-hop, 시나리오 러너 호환 경로) — 게이트웨이 경유
curl -X POST http://localhost:8089/api/orders \
  -H "Content-Type: application/json" \
  -d '{"customerName":"김철수","customerEmail":"chulsoo@test.com","items":[{"productId":1,"quantity":1},{"productId":5,"quantity":2}]}'

# 로그인 → checkout(5-hop, 신규 경로)
TOKEN=$(curl -s -X POST http://localhost:8089/api/users/login \
  -H "Content-Type: application/json" \
  -d '{"email":"user01@example.com","password":"Passw0rd!"}' | python3 -c "import json,sys;print(json.load(sys.stdin)['token'])")
curl -X POST http://localhost:8089/api/carts/1/items -H "Content-Type: application/json" -H "Authorization: Bearer $TOKEN" -d '{"productId":1,"quantity":1}'
curl -X POST http://localhost:8089/api/orders/checkout -H "Content-Type: application/json" -H "Authorization: Bearer $TOKEN" -d '{"userId":1}'
```

### 환경변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `DB_HOST` / `DB_PORT` | localhost / 5432 | PostgreSQL |
| `REDIS_HOST` / `REDIS_PORT` | localhost / 16381(dev compose) | Redis(cart 전용) |
| `KAFKA_BOOTSTRAP_SERVERS` | localhost:19092(dev compose) | Kafka |
| `PRODUCT_SERVICE_URL` ~ `SHIPPING_SERVICE_URL` | http://localhost:808x | 서비스간 호출(order/gateway가 사용) |
| `GATEWAY_SERVICE_URL` | http://localhost:8089 | loadgen 등이 참조하는 게이트웨이 주소 |
| `PG_API_URL` | http://testbed-external-pg-mock:9090 | PG Mock (클러스터 내부 mockserver, 31-external-pg-mock.yaml) |
| `BANKING_TRANSFER_URL` | http://testbed-transfer.rca-testbed-banking.svc.cluster.local:8082 | core-banking cross-domain 이체 API |

## Failure Scenarios (RCA 검증용)

앱에는 injection 엔드포인트를 두지 않는다. 실제 주입은 `rca-scenario-runner` 가 담당.

- 재고 row-lock 경합 (`inventory` 테이블 `SELECT FOR UPDATE`)
- 재고 slow query
- payment 외부 mock timeout/5xx
- Kafka consumer lag / outbox 릴레이 적체
- Redis 장애 시 cart 캐시 스탬피드(circuit breaker open → DB fallback)
- connection pool 고갈, pod CPU throttle
- cross-domain timeout (commerce-payment → core-banking-transfer)
- api-gateway 하류 서비스 장애(circuit breaker open → 502)

## 문서 간 상충 (발견 사항)

`docs/spec-testbed-services.md` §4.1은 commerce를 "5-service, Redis Streams 비동기"로
설명하는데, 이는 3a~5번 증분 이전의 구(舊) 베이스라인이다. 실제로는 Redis Streams가 Kafka로
전환됐고(outbox 패턴 도입), user/cart/pricing/shipping/api-gateway 5개가 추가돼 총 10개
Java 서비스 구성이다. `docs/spec-testbed-expansion.md`가 정본이며, `spec-testbed-services.md`
쪽 commerce 절 갱신은 이번 작업 범위 밖이라 손대지 않고 여기 기록만 남긴다.

## 관련 프로젝트

| 프로젝트 | 설명 |
|----------|------|
| [plopvape-pg-mock](https://github.com/BangSungjoon/plopvape-pg-mock) | 외부 PG API 시뮬레이션 서버 (FastAPI) — 원본 전자담배 쇼핑몰 예제 프로젝트에서 승계 |

## License

MIT
