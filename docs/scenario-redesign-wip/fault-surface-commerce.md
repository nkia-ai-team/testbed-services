# Commerce 도메인 Fault Surface 지도 (bottom-up, 코드 실측)

대상: `/home/ydkim/project-2025/testbed-services/commerce` — 10개 Spring Boot 서비스 + commerce-common + loadgen(k6).
스택: Java 17 / Spring Boot 3.4.5 / PostgreSQL 16 (단일 인스턴스, 스키마 8개 분리) / Redis 7 (cart 전용) / Kafka (outbox 패턴) / Resilience4j / Spring RestClient(`SimpleClientHttpRequestFactory`).
근거는 전부 실제 소스 file:line. 앱에는 injection 엔드포인트 없음 — 주입은 rca-scenario-runner 담당.

---

## 0. 전역 실측 상수 (모든 시나리오의 근거 토대)

이 값들이 "장애가 어디서 터지는지"를 결정한다. 명시 안 된 건 Spring 기본값이며, 기본값 자체가 fault surface다.

| 항목 | 값 | 근거 | 함의 |
|---|---|---|---|
| Tomcat worker threads | **기본 200** (튜닝 없음) | 모든 application.yml에 `server.tomcat.*` 없음 (grep 무결과) | 동기 hop이 느려지면 스레드가 여기까지 쌓임 |
| Hikari pool: order/product/inventory/payment | **기본 10** (설정 없음) | 각 application.yml에 hikari 블록 부재 | 트랜잭션이 원격호출을 감싸면 10개로 금방 고갈 |
| Hikari pool: cart=20, pricing=15, user=10, shipping=10 | 명시값 | cart yml:13, pricing yml:12, user yml:12, shipping yml:12 | cart만 캐시 fallback 흡수용으로 큼 |
| `@Transactional(timeout)` | **어디에도 없음** | grep `Transactional(...timeout` 무결과 | 원격호출 포함 트랜잭션이 DB 커넥션을 read-timeout 합계만큼 점유 |
| RestClient factory | `SimpleClientHttpRequestFactory` (JDK, 커넥션 풀 없음) | order RestClientConfig.java:19,30,41,52,63 | HTTP 클라 측 풀 한계는 없음 → 제약은 Tomcat 스레드 + DB 풀 |
| 게이트웨이 read-timeout | order 15s, 나머지 10s, connect 3s | api-gateway yml:17-45 | 하류 지연 시 게이트웨이 스레드가 최대 이만큼 점유 |
| order→payment | connect 3s / read **15s** / retry **2회** | order yml:51-54,134-137 | 결제 지연이 최장 30s까지 order 스레드 점유 |
| PG mock / banking 클라 | read 10s, **CB/Retry 없음** | payment yml:40-50; PgApiClient.java(전체), BankingTransferClient.java(전체) | 결제 경로 최약점 — 서킷 보호 없음 |

핵심 구조 결함 3가지 (아래 시나리오 대부분의 뿌리):
1. **OrderService.createOrder/checkout이 `@Transactional`인 채 5-hop 동기 원격호출을 트랜잭션 안에서 수행** — OrderService.java:59-139(createOrder), 144-228(checkout). DB 커넥션을 잡은 채 product/inventory/payment/(pricing) 응답을 기다림. 하류 지연 → order Hikari(10) 즉시 고갈.
2. **payment.processPayment가 `@Transactional`인 채 PG mock + banking 이체를 순차 동기 호출** — PaymentService.java:53-89. PG 성공 후 banking 실패 시 트랜잭션 전체 롤백(payment 행도 사라짐).
3. **AuthGuard fail-close** — user-service 장애 시 게이트웨이의 모든 쓰기요청이 401 — AuthGuard.java:31-63.

---

## 1. 서비스 인벤토리 (정체 / endpoint / 진입 / 호출 / DB / 비동기)

외부 진입점은 nginx(:30080) → **api-gateway(:8089) 단일 통로**. 개별 서비스는 클러스터 내부 전용.

### api-gateway (:8089, commerce-gateway) — DB 없음
- 책임: 엣지 라우팅 + 쓰기요청 토큰검증(AuthGuard) + BFF 집계.
- endpoint: `/api/{users,carts,pricing,shipments,products,categories,payments,orders}/**` 프록시, `/api/bff/users/{id}/overview` (BffController).
- 호출: 7개 하류로 RestClient 프록시, 라우트별 독립 CB+Retry(ProxyService.java:57-100). BFF는 user(필수)+cart+order+shipping 순차 집계(AggregationService.java:47-53).
- resilience: 7개 CB 인스턴스(sliding-window 10, 실패율 50%, open 5s), retry 3회(order/payment는 2회) — api-gateway yml:48-136.

### order-service (:8080, commerce-order) — order_schema
- 책임: 주문 오케스트레이터. createOrder(3-hop, 시나리오 러너 전용) + checkout(5-hop) + 일별통계.
- endpoint: `POST /api/orders`, `POST /api/orders/checkout`, `GET /api/orders?userId=&status=&from=&to=&page=&size=`, `GET /api/orders/stats/daily?days=`, `GET /api/orders/{id}`.
- 호출(동기, 전부 CB+Retry): product.reserveStock, inventory.releaseStock(보상), payment.requestPayment, cart.getCart/clearCart, pricing.calculateQuote — client/*.java.
- DB: orders(50k), order_items(~100k), outbox_events. 인덱스는 user_id만(init-schemas.sql:215). status/created_at 인덱스 없음.
- 비동기: OutboxRelay로 commerce.orders 발행; commerce.payments 구독(PaymentEventConsumer.java) → 상태 사후보정.

### product-service (:8081, commerce-product) — product_schema
- 책임: 상품/카테고리/variants 조회 + reserve-stock 중계(inventory 앞단).
- endpoint: `GET /api/products?category=&q=&page=&size=`, `/api/products/{id}/variants`, `/api/categories`, `POST /api/products/{id}/reserve-stock`.
- 호출: inventory (reserve, getStock) — **CB/Retry 없음**, 예외를 status 그대로 전파(product InventoryClient.java:38-51). order→product에는 CB가 있으나 product→inventory는 벌거벗음.
- DB: products(2016), variants(6048). 인덱스 category_id, name(init-schemas.sql:46-47).

### inventory-service (:8082, commerce-inventory) — inventory_schema
- 책임: 재고 차감(`SELECT FOR UPDATE`)/movements 원장/재조정+재입고 배치.
- endpoint: `POST /api/inventory/reserve`, `/api/inventory/release`, `GET /api/inventory/{productId}`, `/api/inventory?lowStockOnly=`, `/api/inventory/{productId}/movements`.
- **핵심 락**: `findByProductIdForUpdate` = `@Lock(PESSIMISTIC_WRITE)` (InventoryRepository.java:17-19) → reserve() 트랜잭션 동안 productId 행 배타락(InventoryService.java:73-96).
- DB: inventory, inventory_movements(원장), outbox_events.
- 배치: ReconciliationBatch 10분 — `findAll()` 전건 2회 순회 + 재입고 write, 단일 `@Transactional`(ReconciliationBatch.java:46-95).

### payment-service (:8083, commerce-payment) — payment_schema
- 책임: 결제 + 외부 PG mock + cross-domain banking 이체 + 정산 배치.
- endpoint: `POST /api/payments`, `GET /api/payments?status=&from=&to=`, `/api/payments/settlements`.
- 호출: PgApiClient(`POST {PG_API_URL}/v1/payments`) + BankingTransferClient(`POST {BANKING_TRANSFER_URL}/api/transfers`) — **둘 다 CB/Retry 없음, RestClient 10s timeout만**.
- DB: payments(50k), payment_logs, settlement_summary, outbox_events. 인덱스 order_id, unsettled(부분)(init:256-257).
- 배치: SettlementBatch 매시 정각 — cross-domain 이체를 `@Transactional` 안에서(SettlementBatch.java:42-85).

### cart-service (:8086, commerce-cart) — cart_schema + Redis
- 책임: 장바구니. Redis 캐시 우선, 장애 시 CB open → DB fallback.
- endpoint: `GET /api/carts/{userId}`, `POST/PUT/DELETE /api/carts/{userId}/items...`.
- resilience: `@CircuitBreaker(cartRedis, fallback=getCartFromDb)` + `@Retry(2회)`(CartService.java:72-88). Redis timeout 500ms(cart yml:33). Redis health 집계 제외(yml:39-42).
- 배치: cleanupExpiredCarts 5분, 24h 유휴 삭제(CartService.java:54-68).

### pricing-service (:8087, commerce-pricing) — pricing_schema
- 책임: 견적 계산 + 프로모션 캐시 배치.
- endpoint: `POST /api/pricing/quote`, `/api/pricing/{productId}`, promotions/coupons.
- resilience: **`@Bulkhead(pricingQuote)` max-concurrent 20, max-wait 0 → 초과 즉시 거부**(pricing yml:32-36; PricingService.java:77). 프로모션은 인메모리 캐시(매시 갱신, PricingService.java:53-60) — DB 미조회.

### shipping-service (:8088, commerce-shipping) — shipping_schema
- 책임: 배송 생성(Kafka commerce.orders 구독) + 상태 자동전이 스케줄러(30s poll).
- 비동기: OrderEventConsumer(groupId=shipping-service) — PAID 이벤트만 배송 생성(OrderEventConsumer.java:28-38).

### user-service (:8085, commerce-user) — user_schema
- 책임: 회원가입/로그인(opaque token)/주소/verify-token.
- endpoint: `POST /api/users/register`, `/login`, `/verify-token?token=`, `GET /api/users/{id}`.
- **게이트웨이 AuthGuard가 모든 쓰기요청마다 verify-token 호출** → user-service는 전 도메인 쓰기경로의 임계 의존.

### notification-service (:8084) — DB 없음
- 순수 Kafka consumer(commerce.orders/payments, groupId=notification-service). 죽어도 blast radius 최소(다른 서비스 무영향, consumer lag만).

### commerce-common — OutboxRelay
- `@Scheduled(fixedDelay 2s)`, `findTop100...PublishedAtIsNull` 폴링 후 `kafkaTemplate.send(...).get()` **동기 블로킹**(OutboxRelay.java:33-47). 발행 실패 시 published_at 미마킹 → at-least-once 재시도.

---

## 2. 도출된 현실적 장애 시나리오 후보 (총 12개)

각 후보: 트리거 / 전파경로 / 관측증상 / root cause vs 증상 / 근거.

### C1. ★ PG mock 지연 → 결제 트랜잭션 정체 → payment DB풀(10) 고갈 → checkout 전멸
- 트리거: PG mock 응답 지연(>수 초, <10s) 또는 timeout.
- 전파: order.checkout(`@Transactional`, order Hikari 점유) → payment.requestPayment(read 15s, retry 2 = 최대 30s) → payment.processPayment(`@Transactional`, payment Hikari 점유) → PgApiClient(CB 없음, 10s 대기). 두 서비스 모두 트랜잭션이 원격호출을 감싸 DB 커넥션을 장시간 점유.
- 증상: payment 커넥션풀(기본 10) 고갈 → payment 5xx/503 → order가 502(BAD_GATEWAY)로 감싸 전파 → 게이트웨이 502. checkout 지연 급증 후 payment CB(order 쪽 paymentClient) open.
- root cause: **PG mock(외부)**. 증상 표면: payment, order, gateway.
- 근거: PaymentService.java:53-89, PgApiClient.java:22-41, OrderService.java:189-200, order yml:51-54/134-137, payment yml:40-44.

### C2. ★ 재고 row-lock 경합 — 인기 flagship 상품 동시 checkout
- 트리거: 같은 productId(1~16)에 동시 reserve 폭주(loadgen checkout + 시나리오 부하).
- 전파: `findByProductIdForUpdate`가 PESSIMISTIC_WRITE로 해당 행 배타락 → 동일 상품 reserve들이 직렬화 대기. 대기 중 order/product/inventory 트랜잭션 각각 DB 커넥션 점유 → inventory 풀(10) + product + order 풀 연쇄 압박.
- 증상: inventory reserve 지연 → product 지연 → order checkout 지연 → 게이트웨이 지연. lock_timeout 미설정이면 무한 대기, Tomcat 스레드 누적.
- root cause: **inventory (락 경합)**. 증상: product/order/gateway 지연.
- 근거: InventoryRepository.java:17-19, InventoryService.java:73-96, hikari 기본 10.

### C3. ★ user-service 장애 → 게이트웨이 fail-close → 전 도메인 쓰기요청 401 (광역 blast radius)
- 트리거: user-service 다운/지연.
- 전파: 게이트웨이 AuthGuard가 모든 POST/PUT/DELETE/PATCH마다 verify-token 호출 → user CB open → verifyTokenFallback이 **401 throw(fail-close)**. register/login만 예외.
- 증상: checkout·장바구니 담기·주문생성 등 **모든 쓰기가 401 Unauthorized**. 읽기(GET)는 정상 → 부분장애처럼 보이나 원인은 한 서비스.
- root cause: **user-service**. 증상: gateway가 광범위 401(오진 유발 강력 — RCA 난이도 높음).
- 근거: AuthGuard.java:31-63, api-gateway yml:51-57(user CB).

### C4. ★ cross-domain banking 이체 실패 → 결제 롤백 → checkout 실패 (namespace 경계 넘는 RCA)
- 트리거: core-banking transfer-service(별 namespace) 다운/지연/5xx.
- 전파: payment.processPayment가 PG 성공 후 BankingTransferClient.transfer 호출(CB 없음) → RestClientException → ServiceException(502) → payment `@Transactional` **전체 롤백**(PG 성공분·payment 행까지 소멸) → order가 502 감싸 전파 → 재고 보상 해제.
- 증상: payment 5xx, checkout 실패, 재고는 release되나 "PG는 성공했는데 결제 없음". commerce 지표만 보면 원인 안 보임(원인=banking 도메인).
- root cause: **core-banking-transfer (타 도메인)**. 증상: commerce payment/order.
- 근거: PaymentService.java:75-89, BankingTransferClient.java:37-59, payment yml:46-50.

### C5. ★ Kafka 지연/다운 → outbox 릴레이 적체 → 배송/알림 중단 + outbox 테이블 누적
- 트리거: Kafka 브로커 지연/다운.
- 전파: OutboxRelay가 `send().get()` 동기 블로킹 → 발행 실패 → published_at 미마킹 → 다음 주기 재시도. 각 서비스 outbox_events 미발행 행 누적. 다운스트림(shipping/order-reconcile/notification) 이벤트 미수신.
- 증상: PAID 주문에 배송 미생성(shipping OrderEventConsumer 미도달), 알림 중단, order 상태 사후보정 정지, DB `outbox_events` row 급증(idx_*_outbox_unpublished 부분인덱스로 관측 가능). **동기 결제 경로는 정상** — 비동기만 조용히 고장.
- root cause: **Kafka**. 증상: shipping/notification 무행동 + outbox 백로그.
- 근거: OutboxRelay.java:33-47, OrderEventConsumer.java:28-38, init-schemas.sql:93/144/240/291/328.

### C6. pricing bulkhead 포화 → checkout 급증 시 견적 거부 → pricingClient CB open
- 트리거: 동시 checkout(견적 호출) > 20.
- 전파: `@Bulkhead(max-concurrent 20, max-wait 0)` → 초과분 BulkheadFullException(즉시 거부) → order 쪽 pricingClient가 5xx로 집계 → CB open → 이후 checkout 전부 fast-fail.
- 증상: checkout 일부 즉시 실패(과부하 신호) → CB open 후 전량 실패. 견적 자체는 빠름(캐시 기반).
- root cause: **pricing 동시성 상한**. 증상: order checkout 실패.
- 근거: PricingService.java:77, pricing yml:32-36, order yml:109-117/149-155.

### C7. Redis 장애 → cart 지연 스파이크 후 CB open → DB fallback (자가치유형)
- 트리거: Redis 다운/지연.
- 전파: getCart가 Redis 500ms timeout × retry 2회 지불 → 5회 실패 누적 후 cartRedis CB open → getCartFromDb fallback. cart DB풀 20이 fallback 트래픽 흡수.
- 증상: cart 초기 지연 급증(수백ms~초) 후 CB open되면 회복·DB부하로 안정. Redis health 집계 제외라 pod 재시작 안 함.
- root cause: **Redis**. 증상: cart 일시 지연(설계상 격리됨 — 정상 회복 검증용).
- 근거: CartService.java:72-88, cart yml:33/39-42/53-67.

### C8. order 통계/검색 슬로우쿼리 → order DB 부하 → 공유 PG 경유 교차 영향
- 트리거: `GET /api/orders/stats/daily?days=90` 또는 넓은 status/기간 검색 폭주.
- 전파: dailyStats가 orders(50k)에서 `GROUP BY date(created_at)` — **created_at 인덱스 없음(user_id만)** → 풀스캔. search도 OR-null 술어라 인덱스 활용 저조.
- 증상: order-service 응답 지연 + **단일 PostgreSQL 인스턴스** CPU 상승 → 다른 스키마(payment/inventory 등) 쿼리까지 동반 지연(공유자원 오염).
- root cause: **order 쿼리/인덱스 부재**. 증상: order + 공유 PG 상의 타 서비스.
- 근거: OrderRepository.java:19-35, init-schemas.sql:215(user_id 인덱스뿐), README:119(단일 인스턴스 8스키마).

### C9. 게이트웨이 스레드 고갈 — 하류 지연이 read-timeout 안에서 누적
- 트리거: 임의 하류 서비스가 timeout 미만으로 느려짐(예 order 15s).
- 전파: 게이트웨이 프록시가 하류 응답을 Tomcat 스레드로 블로킹 대기(SimpleClientHttpRequestFactory, 풀 없음). 라우트별 CB가 있으나 CB open 전(window 10, 5s)까지 스레드 누적 + retry가 부하 증폭.
- 증상: 특정 라우트 지연이 게이트웨이 Tomcat 스레드(기본 200) 잠식 → 건강한 라우트까지 지연/503 가능. CB open되면 완화.
- root cause: 느린 **하류 1개**. 증상: gateway 전반(격리 부분 실패).
- 근거: ProxyService.java:108-129, api-gateway yml:17-45(read 10~15s), Tomcat 기본 200.

### C10. inventory 재고 소진 → checkout 영구 실패 (배치 의존 자립성)
- 트리거: ReconciliationBatch(재입고 담당) 정지 또는 소비속도 > 10분 재입고 주기.
- 전파: loadgen이 재고를 계속 소비 → 재입고 없으면 stock 0 → reserve가 409 CONFLICT(InventoryService.java:79-83).
- 증상: checkout이 409(재고부족)로 실패 급증. 4xx라 CB엔 안 잡힘(정상 업무 거절로 분류) → 조용한 기능 마비.
- root cause: **inventory 재입고 배치 정지**. 증상: order checkout 409.
- 근거: ReconciliationBatch.java:20-24 주석(실측 버그 기록)/73-91, InventoryService.java:79-83.

### C11. settlement 배치 cross-domain 실패 (비사용자 영향, 정산 지연)
- 트리거: 매시 정각 banking 다운.
- 전파: SettlementBatch가 summary FAILED 기록 + payments 미정산 유지 → 다음 주기 자동 누적 재시도.
- 증상: settlement_summary의 banking_transfer_status=FAILED 누적, 미정산 payments 증가. 사용자 경로 무영향.
- root cause: **banking**. 증상: payment 정산 지표.
- 근거: SettlementBatch.java:42-85.

### C12. order Hikari 풀(10) 고갈 — 임의 하류 지연의 공통 귀결
- 트리거: product/inventory/payment/pricing 중 어느 하나라도 지연.
- 전파: createOrder/checkout이 트랜잭션(=DB 커넥션 점유) 안에서 하류를 기다림 → 동시 주문 10건만으로 order 풀 소진 → 신규 주문이 커넥션 획득 대기(connection-timeout 3s) 후 실패.
- 증상: order-service가 실제 느린 하류와 무관하게 "DB 커넥션 timeout"으로 5xx — **오진 유발**(order DB가 원인처럼 보이나 진짜 원인은 하류 지연).
- root cause: 하류 지연(가변). 증상: order DB 풀 고갈 — RCA 함정.
- 근거: OrderService.java:59/144(`@Transactional`+원격호출), hikari 미설정(기본 10), `@Transactional(timeout)` 부재.

---

## 3. root cause vs 증상 서비스 요약표

| # | root cause | 1차 증상 표면 | 유형 | RCA 난이도 |
|---|---|---|---|---|
| C1 | PG mock(외부) | payment→order→gateway 5xx/지연 | 지연+풀고갈 | 중 |
| C2 | inventory 락 | product/order 지연 | 락 경합 | 중 |
| C3 | user-service | gateway 광역 401(쓰기 전체) | fail-close | **높음** |
| C4 | core-banking(타도메인) | payment/order 5xx | cross-domain+롤백 | **높음** |
| C5 | Kafka | shipping/notification 무행동+outbox 백로그 | 비동기 적체 | 중 |
| C6 | pricing bulkhead | order checkout 실패 | 동시성 상한 | 중 |
| C7 | Redis | cart 일시 지연 | 캐시(자가치유) | 낮음 |
| C8 | order 인덱스 부재 | order+공유PG 타서비스 | 슬로우쿼리 | 중 |
| C9 | 느린 하류 1개 | gateway 전반 | 스레드 고갈 | 중 |
| C10 | 재입고 배치 정지 | order 409 | 자원 고갈 | 중 |
| C11 | banking | 정산 지표 | 배치(비사용자) | 낮음 |
| C12 | 하류 지연(가변) | order DB풀 | 오진 함정 | **높음** |
