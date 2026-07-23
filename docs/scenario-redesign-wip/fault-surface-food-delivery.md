# food-delivery — 실측 Fault Surface 지도 (bottom-up)

대상: `/home/ydkim/project-2025/testbed-services/food-delivery`
스택: Spring Boot + OTel javaagent, 단일 flat MySQL DB(`fooddelivery`, 15테이블), Kafka(outbox relay), 외부 PG mock(mockserver).
서비스 5개: order(8080) / restaurant(8081) / dispatch(8082) / payment(8083) / notify(8084) + shop-common(공유 lib, outbox 베이스).

기준선(주의): 모든 DB 풀 `connection-timeout=3000ms`, HTTP `connect-timeout=3s`. Resilience4j CB는 전부 sliding-window=10 / min-calls=5 / 50% / open 5s. Retry는 지수백오프(200ms×2, payment/pg만 max=2·300ms). `ClientErrorException`(4xx)은 CB·Retry **ignore-exceptions**라 회로를 열지 않음(정상 업무 거절과 진짜 장애의 코드상 분리축).

---

## 1. 외부 진입 / 호출 그래프

**외부 진입(NodePort, loadgen이 직접 치는 3서비스):**
- order-service `30180` (`k8s/20-order-deploy.yaml:113,119`)
- restaurant-service `3018x` (`k8s/21`)
- dispatch-service `30182` (`k8s/22-dispatch-deploy.yaml:112,118`)
- payment / notify는 NodePort 없음 = **내부 전용**. Ingress/API-gateway 없음 — 각 서비스가 개별 NodePort.

**order-service = 오케스트레이터.** `createOrder`가 하나의 `@Transactional` 안에서 동기 fan-out (`OrderService.java:57`):
1. `restaurantClient.getRestaurant()` → restaurant `/api/restaurants/{id}` (`OrderService.java:60`)
2. `restaurantClient.getMenu()` → restaurant `/api/restaurants/{id}/menu` (`:73`)
3. `dispatchClient.checkCapacity()` → dispatch `GET /api/deliveries/capacity` (`:96`)
4. `dispatchClient.dispatchCourier()` → dispatch `POST /api/deliveries/dispatch` (`:145`)
5. `paymentClient.processPayment()` → payment `POST /api/payments` (`:154`)
6. `orderEventPublisher.publish()` → outbox → Kafka `food.orders` (`:162`, async)

**payment-service:** `processPayment` → `pgApiClient.pay()` → 외부 PG mock `POST /pay` (`PaymentService.java:70`, `PgApiClient.java:34`). `OrderClient`는 존재하나 주석대로 커밋-전 race 때문에 **main path에서 미사용**(reverse lookup 안 함).

**dispatch-service:** downstream 동기 호출 없음(order로의 reverse client 존재하나 미사용). capacity 게이트가 핵심.

**비동기(Kafka):** 각 서비스 outbox 테이블 → `OutboxRelay.relay()` **2초 폴링**(`OrderOutboxRelay.poll fixedDelay=2000`, `OutboxRelay.java:31`) → `kafkaTemplate.send().get()`(동기 blocking get). topics: `food.orders`/`food.payments`/`food.dispatch`. **notify-service가 3토픽 모두 소비**, 단일 consumer group `notify-service`(`OrderEventConsumer.java:25`, `application.yml:31`). notify는 no-op 로깅(`NotifyService.java:12`), DataSource 비활성(`notify application.yml:14`).

---

## 2. 서비스별 요약

### order-service (오케스트레이터, 진짜 장애 증폭기)
- 책임: 주문 생성 orchestration + 조회/집계(`/api/orders`, `/api/orders/stats/daily`).
- Hikari pool **15**, restaurant/dispatch read-timeout 5s, **payment read-timeout 10s**(`application.yml:62`).
- 치명 구조: `createOrder` 전체가 `@Transactional` (`OrderService.java:57`). **DB 커넥션을 잡은 채로 5개 원격 HTTP 호출을 순차 수행** → 커넥션 보유시간 = Σ(downstream latency). downstream이 느려지면 pool(15) 고갈 → order 자체가 새 요청에 `connection-timeout 3s` 초과로 5xx.
- `@Transactional timeout` 미설정(무제한) — 느린 downstream은 트랜잭션·커넥션을 read-timeout(최대 10s)까지 붙잡음.

### restaurant-service (읽기 전용 리프, 최상류 의존)
- 책임: restaurant/menu 조회 + 인기메뉴 캐시. downstream 없음. Hikari pool **10**.
- `PopularMenuBatch`(`@Scheduled` 1h)가 **네이티브 크로스-테이블 조인**(`order_items ⋈ orders ⋈ menus`, `MenuRepository.java:17`)으로 `orders`(20k행) 전체 집계 → `deleteAll()` 후 재삽입(`PopularMenuBatch.java:45`). 무거운 스캔 + 배치 트랜잭션.
- order의 모든 주문이 여기 2회 의존(getRestaurant+getMenu) → restaurant 지연은 order로 직결.

### dispatch-service (capacity 게이트 = 정상 거절 vs 장애 분기점)
- `dispatchCourier`: `countByStatus("ASSIGNED") >= maxCapacity`면 **503 "Courier pool exhausted"**(`DispatchService.java:60-64`). 이건 **정상 업무 거절**(biz_reject)이지 장애 아님.
- `max-capacity` env 기본 **2000**(`dispatch application.yml:83`). 과거 50이라 상시 503이던 결함 이력이 주석에 기록됨. **시나리오는 이 env를 낮춰 배차부족 재현**.
- `deliverExpiredDispatches`(`@Scheduled 30s`)가 ETA 지난 ASSIGNED→DELIVERED 전이(`:119`). **이게 멈추면 capacity 영구 소진→전량 503**.
- Hikari pool **10**(최소).

### payment-service (외부 PG 의존 = external-timeout 표면)
- `processPayment`: PENDING 저장 → `pgApiClient.pay()` → 상태 반영(`PaymentService.java:50`).
- PG mock은 `k8s/30-external-pg-mock.yaml` — **명시적 failure_surface: external-timeout**(파일 주석 line 2). mockserver 9090→1080.
- CB `pg` + Retry `pg` max=2(중복결제 억제, `PgApiClient.java:32-33`, `application.yml:94`). PG 4xx→`ClientErrorException`(전파, 회로 안 엶); 5xx/timeout→`ServiceException(502 BAD_GATEWAY)` 또는 fallback `502`(`PgApiClient.java:54,65`).
- Hikari pool **15**, PG read-timeout 10s.

### notify-service (async sink, 백프레셔 지점)
- Kafka 3토픽 소비, no-op. consumer 예외를 catch-and-log(`OrderEventConsumer.java:30`) → **정지/에러 나도 upstream엔 안 보임**. 관측상 lag로만 나타남.

---

## 3. DB 락/무거운 쿼리 실측
- **`createOrder`의 긴 트랜잭션**(`OrderService.java:57`): orders+order_items INSERT를 원격호출 5개와 같은 트랜잭션에 묶음 → 커넥션 점유 = 락/풀 압박의 근원. food-delivery 배차 특성상 최고 위험 지점.
- `PopularMenuBatch` 집계 조인(`MenuRepository.java:17`): 20k `orders` × `order_items` 풀스캔, `created_at >= since` 필터. `idx_orders_created_at` 존재(`init.sql:83`)하나 order_items→menus 조인은 인덱스 의존.
- `OrderRepository.aggregateDailyStats`(`:36`): `GROUP BY DATE(created_at)` — **함수 적용으로 인덱스 무력화 가능**(range는 타되 group은 filesort). `/api/orders/stats/daily` 챗봇 질의가 트리거.
- `OrderRepository.search`(`:22`): 5개 optional 파라미터 `(:x IS NULL OR ...)` 패턴 — **파라미터 스니핑/인덱스 미선택**으로 풀스캔 위험. `page/size` 미지정 시 `Pageable.unpaged()`(`OrderController.java:41`) = **무제한 반환**.
- `SettlementBatch`(`@Scheduled 1h`): `findByStatusInAndSettledAtIsNullAndCreatedAtLessThan` → `idx_payments_settlement(status,settled_at,created_at)` 커버(`init.sql:152`). 결과 saveAll 벌크 업데이트.
- `deliverExpiredDispatches` 네이티브 쿼리 `DATE_ADD(assigned_at, INTERVAL eta_minutes MINUTE) < NOW()`(`DispatchRepository.java:26`) — **컬럼 함수라 `idx_dispatches` 안 탐, status='ASSIGNED' 스캔**.

---

## 4. 에러 경로: 정상 거절(4xx/biz) vs 진짜 장애(5xx) — 코드상 분리
| 유형 | 코드 | HTTP | 성격 |
|---|---|---|---|
| restaurant CLOSED | `OrderService.java:65` | 400 | biz reject |
| menu sold out | `:86` | 400 | biz reject |
| courier capacity 0 | `:99-103`, `DispatchService.java:61` | **503** | **biz reject(장애 아님)** |
| downstream 4xx | `ClientErrorException` | 4xx 전파 | biz reject, CB 무시 |
| restaurant/menu down·timeout | `RestaurantClient.java:44` fallback | **502** | 진짜 장애 |
| dispatch down·timeout | `DispatchClient.java:49,66` | **503** | 진짜 장애 |
| payment/PG down·timeout | `PaymentClient.java:46`, `PgApiClient.java:54` | **502** | 진짜 장애 |
| dispatch capacity check 실패 | `OrderService.java:110` | 503 "unreachable" | 장애(생성 차단) |

**핵심 함정: 503이 두 원인(capacity biz-reject vs dispatch 장애)에서 나온다.** capacity 소진(정상)과 dispatch 다운(장애)을 5xx 코드만으로 구분 불가 — 메시지/로그(`"Courier pool exhausted"` vs `"unavailable"`)로만 구분. RCA judge의 사각지대.

`GlobalExceptionHandler`는 `ServiceException`만 처리(`order/config/GlobalExceptionHandler.java:12`) → status 그대로 반환. 미매핑 예외는 Spring 기본 500.

---

## 5. 관측
- OTel javaagent hostPath `/opt/polestar10/apm`, 데이터 119 및 .104:4317.
- 커스텀 메트릭 없음(코드에 Micrometer 커스텀 카운터 없음) — actuator health/info만 노출, `management.health.kafka.enabled=false`(모든 서비스). 배차 capacity/outbox lag는 **메트릭 미노출**, 로그로만.

---

## 6. 현실적 장애 시나리오 후보 (코드 근거)

### C1. ★ order 롱-트랜잭션 커넥션풀 고갈 (downstream 지연 증폭)
- 트리거: restaurant 또는 payment(PG mock) 응답 지연 주입(external-timeout, `k8s/30-external-pg-mock.yaml`).
- 전파: `createOrder`가 `@Transactional`로 커넥션 잡은 채 순차 원격호출(`OrderService.java:57,60,73,145,154`) → 커넥션 보유 = Σlatency(최대 payment 10s) → Hikari pool 15 고갈.
- 증상: **order-service**에서 신규 요청이 `connection-timeout 3s` 초과로 5xx, order 지연 급증. PG는 502로만 표면.
- root cause = restaurant/payment(또는 PG mock) / 증상 = **order-service**(전형적 오진 유발).
- 근거: `OrderService.java:57`, `order application.yml:12-16,62`.

### C2. ★ 배차 만료 배치 정지 → capacity 소진 → 전량 503
- 트리거: `deliverExpiredDispatches` 스케줄러 정지(pod 교란/GC stall) 또는 `DISPATCH_MAX_CAPACITY` 하향.
- 전파: ASSIGNED가 DELIVERED로 안 빠짐 → `countByStatus("ASSIGNED") >= maxCapacity`(`DispatchService.java:60`) → 신규 배차 503.
- 증상: **order-service**가 `createOrder` fan-out에서 503 "No courier available"로 주문 거절(`OrderService.java:99-103`). dispatch 자체는 정상 응답(200 capacity=0).
- root cause = dispatch(배치/capacity) / 증상 = order 주문 실패. **503이 biz-reject 형태라 장애로 안 보일 위험**.
- 근거: `DispatchService.java:60,119`, `dispatch application.yml:83`.

### C3. ★ PG mock 지연/다운 → payment 502 → 회로 개방
- 트리거: PG mock 응답 지연/500(파일이 명시한 external-timeout 표면).
- 전파: `PgApiClient.pay` timeout → Retry 2회(300ms×2 백오프) 증폭 → 502(`PgApiClient.java:54`). 실패율 50%↑면 CB `pg` open 5s → fallback 502(`:65`).
- 증상: **payment 502** → order fan-out에서 502 전파(`OrderService.java:154`, `PaymentClient.java:46`) → order 주문 실패.
- root cause = 외부 PG / 증상 = payment→order 연쇄 5xx.
- 근거: `PgApiClient.java:32-56`, `payment application.yml:76-100`, `k8s/30-external-pg-mock.yaml`.

### C4. ★ restaurant 지연/다운 → order 최상류 차단 (모든 주문 502)
- 트리거: restaurant CPU 포화(PopularMenuBatch 무거운 조인과 겹침) 또는 pod 다운.
- 전파: order의 **모든** 주문이 getRestaurant+getMenu 2회 의존(`OrderService.java:60,73`). restaurant 지연 → Retry 3회(200/400ms) 증폭 → 5초 read-timeout → 502 fallback(`RestaurantClient.java:44`).
- 증상: **order 전량 502**, restaurant 자체 CPU/지연 알람. restaurant CB open 시 즉시 502.
- root cause = restaurant / 증상 = order. C1과 결합하면 커넥션풀 고갈까지 동반.
- 근거: `RestaurantClient.java:29-56`, `MenuRepository.java:17`(배치 부하원), `order application.yml:69-77`.

### C5. Kafka/outbox relay 정지 → 이벤트 적체 + relay 블로킹
- 트리거: Kafka 브로커 다운/지연.
- 전파: `OutboxRelay.relay`의 `kafkaTemplate.send().get()`(동기 blocking, `OutboxRelay.java:36`) → send 대기. published_at 미마킹으로 outbox_events 테이블 무한 적체(at-least-once). notify consumer lag 급증.
- 증상: **order/payment/dispatch outbox 테이블 행 증가**(미발행), notify 이벤트 수신 중단. **동기 createOrder path는 영향 없음**(publish는 fan-out 3, 예외 삼킴 아님 — 단 relay는 별 스레드). 온라인 주문 성공하나 알림 누락.
- root cause = Kafka / 증상 = notify 무음 + outbox 적체(무증상 데이터 드리프트).
- 근거: `OutboxRelay.java:31-44`, `OrderOutboxRelay poll fixedDelay=2000`.

### C6. 무제한 조회/집계 쿼리로 order DB·힙 압박
- 트리거: `/api/orders`를 page/size 없이 호출(`Pageable.unpaged()`, `OrderController.java:41`) 또는 `/stats/daily?days=90` 챗봇 질의.
- 전파: 20k orders 무제한 반환/`GROUP BY DATE()` filesort(`OrderRepository.java:22,36`) → order 힙·DB CPU.
- 증상: order 지연/OOM 위험(limit 1Gi, `k8s/20`), DB slow query.
- root cause = order 쿼리 설계 / 증상 = order 지연. C1 커넥션 압박과 상승작용.
- 근거: `OrderController.java:41`, `OrderRepository.java:22,36`, `MenuRepository.java:17`.

### C7. MySQL 인덱스 드롭/락 (기존 F02 시나리오 계열)
- 트리거: `menus`/`orders` 인덱스 드롭(브랜치에 `f02-p-mysql-menu-index-drop.yaml` 존재) 또는 DDL 락.
- 전파: `findByRestaurantId`/집계 조인이 풀스캔화 → restaurant·order 전반 지연.
- 증상: restaurant getMenu 지연 → order 지연 연쇄(C4 경로).
- root cause = DB(인덱스/DDL) / 증상 = restaurant→order.
- 근거: `init.sql:48,81-83,152`, `MenuRepository.java:12,17`.

---

## 7. root cause vs 증상 요약축
- **증상은 거의 항상 order-service에 모인다**(오케스트레이터). 진짜 원인은 restaurant/dispatch/payment/PG/Kafka/DB 중 하나.
- **503 이중성**(capacity biz-reject vs dispatch 장애)과 **502 연쇄 전파**(downstream→order)가 이 도메인의 대표적 오진 함정.
- notify/outbox 경로 장애는 5xx 없이 **데이터 드리프트/lag**로만 나타나 판정 사각.
