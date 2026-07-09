# PlopVape Shop

PlopVape 전자담배 온라인 쇼핑몰 마이크로서비스 프로젝트.

5개의 Spring Boot 서비스로 구성된 주문 처리 시스템으로, 3-hop 호출 체인(`order → product → inventory`)을 통해 다단계 서비스 간 통신을 구현합니다.

## 아키텍처

```
Client
  │
  ▼
order-service (:8080)
  ├──▶ product-service (:8081)
  │       └──▶ inventory-service (:8082)   ← 3-hop 체인
  ├──▶ payment-service (:8083)
  │       └──▶ pg-mock-server (:8090)      ← 외부 PG API
  └──▶ Redis Streams ("order-events")
          └──▶ notification-service (:8084) ← 비동기 소비
```

### 동기 + 비동기 혼합

- **동기 (REST)**: order → product → inventory, order → payment
- **비동기 (Redis Streams)**: order → notification

## 기술 스택

| 항목 | 기술 |
|------|------|
| Language | Java 21 |
| Framework | Spring Boot 3.4.5 |
| Build | Maven (multi-module) |
| DB | PostgreSQL 16 (스키마 분리) |
| Cache/Queue | Redis 7 (Streams) |
| HTTP Client | Spring RestClient |
| ORM | Spring Data JPA + Hibernate |

## 프로젝트 구조

```
plopvape-shop/
├── pom.xml                        # Parent POM
├── shop-common/                   # 공통 모듈 (DTO, 예외, BaseEntity)
│   └── src/main/java/com/plopvape/common/
│       ├── dto/                   # 서비스 간 통신 DTO (11개)
│       ├── exception/             # ServiceException, ErrorResponse
│       └── config/                # BaseEntity (@PreUpdate)
│
├── order-service/                 # :8080 — 주문 생성/조회 허브
│   └── src/main/java/com/plopvape/order/
│       ├── controller/            # OrderController
│       ├── service/               # OrderService (오케스트레이션)
│       ├── client/                # ProductClient, PaymentClient, InventoryClient
│       ├── event/                 # OrderEventPublisher (Redis Streams)
│       ├── entity/                # Order, OrderItem
│       ├── repository/            # OrderRepository
│       └── config/                # RestClientConfig, GlobalExceptionHandler
│
├── product-service/               # :8081 — 상품 목록/검색
│   └── src/main/java/com/plopvape/product/
│       ├── controller/            # ProductController
│       ├── service/               # ProductService
│       ├── client/                # InventoryClient
│       ├── entity/                # Product, Category
│       ├── repository/            # ProductRepository
│       └── config/                # RestClientConfig, GlobalExceptionHandler
│
├── inventory-service/             # :8082 — 재고 관리 (SELECT FOR UPDATE)
│   └── src/main/java/com/plopvape/inventory/
│       ├── controller/            # InventoryController
│       ├── service/               # InventoryService (pessimistic lock)
│       ├── entity/                # Inventory
│       ├── repository/            # InventoryRepository
│       └── config/                # GlobalExceptionHandler
│
├── payment-service/               # :8083 — 결제 처리
│   └── src/main/java/com/plopvape/payment/
│       ├── controller/            # PaymentController
│       ├── service/               # PaymentService
│       ├── client/                # PgApiClient (외부 PG API 호출)
│       ├── entity/                # Payment, PaymentLog
│       ├── repository/            # PaymentRepository, PaymentLogRepository
│       └── config/                # RestClientConfig, GlobalExceptionHandler
│
├── notification-service/          # :8084 — 알림 (Redis Streams consumer)
│   └── src/main/java/com/plopvape/notification/
│       ├── consumer/              # OrderEventConsumer
│       ├── service/               # NotificationService
│       └── config/                # RedisStreamConfig
│
├── db/
│   ├── init-schemas.sql           # 4개 스키마 생성
│   └── seed-all.sql               # 통합 시딩 (K3s InitContainer용)
│
└── docker-compose.dev.yml         # 로컬 개발용 (PostgreSQL + Redis)
```

## 서비스별 상세

### order-service (:8080)

주문 생성 허브. 다른 서비스를 호출하여 전체 주문 플로우를 오케스트레이션합니다.

| API | 설명 |
|-----|------|
| `POST /api/orders` | 주문 생성 (product → inventory → payment → DB → Redis) |
| `GET /api/orders` | 전체 주문 조회 |
| `GET /api/orders/{id}` | 단건 주문 조회 |

결제 실패 시 `POST /api/inventory/release`를 호출하여 재고를 자동 복구합니다.

### product-service (:8081)

상품 정보 관리. 재고 확인 시 inventory-service를 호출합니다.

| API | 설명 |
|-----|------|
| `GET /api/products` | 상품 목록 (카테고리/이름 필터 지원) |
| `GET /api/products/{id}` | 상품 상세 + 재고 |
| `POST /api/products/{id}/reserve-stock` | 재고 차감 (주문 플로우 전용) |

### inventory-service (:8082)

재고 관리. 동시 차감 시 `SELECT FOR UPDATE`로 row lock을 사용합니다.

| API | 설명 |
|-----|------|
| `GET /api/inventory/{productId}` | 재고 조회 |
| `POST /api/inventory/reserve` | 재고 차감 (pessimistic lock) |
| `POST /api/inventory/release` | 재고 복구 (결제 실패 보상) |
| `PUT /api/inventory/{productId}` | 재고 수동 설정 |

### payment-service (:8083)

결제 처리. 외부 PG API(pg-mock-server)에 실제 HTTP 호출을 수행합니다.

| API | 설명 |
|-----|------|
| `POST /api/payments` | 결제 요청 → PG API 호출 → 결과 저장 |

### notification-service (:8084)

Redis Streams consumer. DB를 사용하지 않으며, 주문 완료 이벤트를 비동기로 소비합니다.

- Stream: `order-events`
- Consumer Group: `notification-group`
- 이벤트 수신 시 알림 발송 시뮬레이션 (로그 출력)

## DB 구조

PostgreSQL 1인스턴스, 4개 스키마 분리:

```
PostgreSQL (plopvape)
├── order_schema      → orders, order_items
├── product_schema    → products, categories
├── inventory_schema  → inventory
└── payment_schema    → payments, payment_logs
```

### 초기 데이터

- 카테고리 4개: 기기, 액상, 코일/팟, 악세서리
- 상품 16개 (카테고리별 4개)
- 재고: 상품별 30~500개

## 시작하기

### 사전 요구사항

- Java 21+
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
| `DB_USER` | plopvape | DB 사용자 |
| `DB_PASS` | plopvape1234 | DB 비밀번호 |
| `REDIS_HOST` | localhost | Redis 호스트 |
| `REDIS_PORT` | 6379 | Redis 포트 |
| `PRODUCT_SERVICE_URL` | http://localhost:8081 | product-service URL |
| `PAYMENT_SERVICE_URL` | http://localhost:8083 | payment-service URL |
| `INVENTORY_SERVICE_URL` | http://localhost:8082 | inventory-service URL |
| `PG_API_URL` | http://192.168.200.109:8090 | PG Mock Server URL |

## Docker

각 서비스는 multi-stage Dockerfile을 제공합니다. 빌드 컨텍스트는 프로젝트 루트입니다:

```bash
docker build -f order-service/Dockerfile -t plopvape-order .
docker build -f product-service/Dockerfile -t plopvape-product .
docker build -f inventory-service/Dockerfile -t plopvape-inventory .
docker build -f payment-service/Dockerfile -t plopvape-payment .
docker build -f notification-service/Dockerfile -t plopvape-notification .
```

베이스 이미지 `eclipse-temurin:21`은 `linux/amd64`, `linux/arm64` 모두 지원합니다.

## 관련 프로젝트

| 프로젝트 | 설명 |
|----------|------|
| [plopvape-pg-mock](https://github.com/BangSungjoon/plopvape-pg-mock) | 외부 PG API 시뮬레이션 서버 (FastAPI) |

## License

MIT
