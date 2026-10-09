---
title: F48-P 설계 시트 (취소된 인덱스 백필이 food 주문 이벤트 outbox 표를 치워 주문 생성이 전량 되돌려짐)
status: Draft
owner: project
last_reviewed: 2026-10-09
tags:
  - scenario
  - design
  - schema
  - mysql
  - operation
  - outbox
summary: 운영자가 food MySQL 의 주문 이벤트 outbox 표(order_outbox_events)에 aggregate_id 인덱스를 더하는 온라인 백필을 시작했다가 취소했는데, 취소 정리가 그림자 표 대신 살아 있는 표를 보류 이름으로 치워 order-service 의 주문 생성이 마지막 단계(같은 트랜잭션의 outbox 기록)에서 MySQL 1146 Table doesn't exist 로 되돌려지고 전량 500 이 되는 시나리오. 둘러보기, 주문 조회, 배달 추적, dispatch, payment 는 정상이다. 원본은 GitHub 2026-07-24 Vitess 백필 워크플로 취소가 기반 표를 지워 PR '생성'만 실패한 장애.
---

# F48-P 설계 시트

## 1. 요약

food 의 order-service 는 주문을 만들 때 주문 행, 품목 행과 함께 주문 이벤트를 `fooddelivery.order_outbox_events` 에 같은 트랜잭션으로 쓴다(트랜잭션 outbox). `OrderOutboxRelay` 가 2초마다 이 표의 미발행 행을 읽어 Kafka(food.orders)로 보내고, 24시간이 지난 발행분은 지운다(약 5만 6천 행). 운영자가 주문 id 로 이벤트 이력을 찾는 일이 잦아 `aggregate_id` 인덱스를 더하는 온라인 인덱스 백필을 시작한다. 도구는 새 인덱스를 가진 그림자 표(`_vt_vrp_<uuid>_<시각>_`)를 만들고 행을 옮긴다. 그런데 백필이 취소되고, 취소 정리가 그림자 표가 아니라 살아 있는 `order_outbox_events` 를 테이블 수명주기의 보류 이름(`_vt_hld_<uuid>_<보류 기한>_`, Vitess 가 표를 지우는 방식)으로 치운다. 그 순간부터 `createOrder` 는 가게, 메뉴, 배달원 용량을 확인하고 주문과 품목을 저장하고 배차와 결제까지 마친 뒤, 마지막 outbox 기록에서 MySQL 1146 으로 실패해 주문 트랜잭션 전체가 되돌려진다. 손님은 아무도 주문하지 못하고(500), 가게 둘러보기, 주문 조회, 배달 추적은 그대로다.

비유: 주문서를 받아 주방과 배달 기사에게 다 전하고 카드 결제까지 한 뒤, 마지막에 "주문 접수 알림판"에 한 줄 적어야 주문이 확정되는 가게다. 알림판에 칸막이를 새로 달려고 옆에 임시 알림판을 세우다 작업을 취소했는데, 정리하던 사람이 임시판 대신 원래 알림판을 창고로 치웠다. 직원은 매번 마지막 단계에서 "알림판이 없다" 며 주문서를 찢고, 손님에게는 주문 실패라고 말한다. 메뉴판과 배달 조회 창구는 멀쩡하다.

## 2. 원본 사례

- 기업: GitHub (풀 리퀘스트 생성)
- 날짜: 2026-07-24 19:17~20:02 UTC(57분)
- 링크: [GitHub Availability Report: July 2026 (공식)](https://github.blog/news-insights/company-news/github-availability-report-july-2026/) (자료 문서 `ref-real-world-incidents.md` M21 에 이미 있음)
- 요약(출처가 말한 것만): 사용자가 웹, CLI, API 로 풀 리퀘스트를 만들지 못했다("users were unable to create pull requests through web, command line, or API interfaces"). 시도 113,930회, 사용자 50,904명, 평균 오류율 1.75%, 최대 2.25%. 기존 풀 리퀘스트와 다른 기능은 영향이 없었고 PR 을 만드는 워크플로도 실패했다. 근본 원인은 풀 리퀘스트 데이터를 담은 Vitess keyspace 로의 백필 워크플로였고, "The cancellation executed a misunderstood Vitess codepath that dropped the backing table to the target keyspace" 라 낡은 vschema 참조가 남았다. 데이터베이스 변경을 되돌리자 PR 생성이 바로 재개됐고, 낡은 참조를 지워 마무리했다. 재발 방지로 인덱스 백필 운영 지침을 더하고 예상 밖 취소 동작을 문서화했다. 탐지 경로는 적혀 있지 않다.
- Vitess 쪽 사실(제품 문서, 사고 보고 아님): 표를 지울 때 수명주기(hold → purge → evac → drop)를 밟고 상태를 표 이름에 담는다([Vitess table lifecycle](https://vitess.io/docs/user-guides/schema-changes/table-lifecycle/)). F48-R 과 같이 "지움" 을 보류 이름으로 바꾸기로 재구성한다: 원본의 복구(변경 되돌림 → 즉시 재개)와 같은 꼴이고 행을 잃지 않는다.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | PR 데이터 keyspace 로의 백필 워크플로를 취소 | `order_outbox_events` 에 `aggregate_id` 인덱스를 더하는 온라인 인덱스 백필(그림자 표 생성, 인덱스, 행 복사)을 시작했다가 취소 |
| 원인이 된 결함 | 취소가 잘못 이해된 Vitess 코드 경로를 타 기반 표를 지움 | 취소 정리가 그림자 표가 아니라 살아 있는 표를 보류 이름(`_vt_hld_...`)으로 치움, 그림자 표는 남음 |
| 전파 경로 | 앱이 여전히 그 표를 참조(낡은 vschema 참조) → 그 표를 쓰는 연산만 실패 | order-service 의 `OrderOutboxEvent` 엔티티가 여전히 `order_outbox_events` 에 씀 → 주문 트랜잭션의 outbox INSERT 1146 → 트랜잭션 되돌림 → 500. 릴레이 폴링도 같은 오류 |
| 사용자 증상 | PR **생성**만 실패(평균 1.75%), 기존 PR 과 다른 기능은 정상, PR 을 만드는 워크플로도 실패 | 주문 **생성**만 100% 실패, 주문 조회, 가게 둘러보기, 배달 추적, 결제 서비스 자체는 정상, 주문 이벤트 발행(릴레이)도 실패 |
| 탐지된 경로 | 적혀 있지 않음 | order 오류율과 새 오류 로그(1146, 스케줄러 ERROR) 급증(119 이상 탐지 → 인시던트) |
| 완화와 복구 | 데이터베이스 변경을 되돌리자 즉시 재개, 낡은 참조 정리 | cleanup 이 보류 표를 원래 이름으로 되돌림(즉시 재개, 행 그대로), 남은 그림자 표를 지움 |

기전은 원본과 같다: "백필 취소가 살아 있는 표를 지움 → 앱이 여전히 그 표를 참조 → 그 표에 쓰는 '만들기' 기능만 실패, 나머지는 정상 → 변경을 되돌리면 즉시 회복". 원본에서 실패한 것이 기존 PR 읽기가 아니라 PR **생성**이었다는 점을, 같은 원본의 F48-R(읽기 기능 하나만 실패)보다 이 후보가 더 가깝게 재현한다. 우리 스택에 맞춘 것은 대상(MySQL 8.0 의 outbox 표 하나)과 규모다. 테스트베드 고유의 차이 하나: createOrder 가 하류 호출(배차, 결제) 뒤에 outbox 를 쓰므로 실패한 주문마다 배차와 결제가 남는다. 이것은 코드 구조가 키운 피해라 user_impact 에 적고 정답에 넣지 않는다(원칙 5).

## 3. 숫자 근거 (2026-10-09, `scenario-stats.py`, 정식 + 후보 합계 58, 이 후보 전)

| 축 | 값 | 판단 |
|---|---|---|
| 서비스 | 쇼핑몰 26, 은행 17, 음식배달 15 | 음식배달이 가장 적다 → 음식배달(15→16) |
| 묶음 | A 7, B 7, D 7, G 7(각 12%), C 6, J 6(각 10%), L 5, F 3, E 2, H 2, K 2, I 1, M 1, O 1, P 1, N 0 | P(운영 작업의 대상 착오) 1→2(3.4%). 결함이 앱이 기대하는 표가 없는 꼴로 드러나 F48-R 처럼 P+L. L 로 세도 5→6(10.2%) |
| 정답 위치 | DB 테이블(주문 이벤트 outbox) 0 | §2-1 의 'DB 테이블(<무엇>)' 꼴, 0→1(1.7%). 주문 서비스(7, 12%)가 아니다 |
| 결제 경로 합계 | 10(17.2%) | 결제가 정답이 아니므로 그대로(10/59, 16.9%) |
| 어느 축이든 | 최대는 A, B, D, G 7/59(11.9%), 주문 서비스 7/59(11.9%) | 20% 미만 |

묶음 해석: 바뀐 것은 표 하나의 존재다. 설정값이 아니라 G 가 아니고, 코드나 이미지 배포가 없어 J 가 아니며, 계획된 스키마 변경이 코드보다 먼저 적용된 F36-R(L)과도 다르다: 백필이 의도한 변경(인덱스 추가)은 맞았고 그것을 적용하던 도구의 정리 단계가 엉뚱한 대상(살아 있는 표)을 치웠다. DB 프로세스, 자원, 잠금, 질의 비용은 그대로라 A, B, D, F 가 아니다.

관계와 id: 원인 기전(취소된 백필이 살아 있는 표를 치움)은 F48-R 과 같고, 진입 경로(주문 생성 대 인기 메뉴 읽기)와 영향 범위(핵심 쓰기 경로 전량 대 읽기 기능 하나)가 다르다. 카탈로그 §1 의 R 조건(DB 엔진, 진입 경로, 영향 범위, 노이즈 중 하나 이상이 다름)을 만족하지만 F48-R 이 이미 R 이라, 같은 기전을 다른 대상에 건 F44-P 선례처럼 P 로 둔다. 같은 수단(db.ddl 보류 표 모드)이지만 표, 인덱스, 최소 행이 달라 같은 주입 중복(같은 수단과 같은 파라미터)이 아니다(이번 실행 앞머리: "같은 수단의 다른 표는 다른 파라미터").

겉 증상(order 주문 생성 500)은 정식 F38-R 과 같고 정답이 다르다(카탈로그 관계 H). 관제 AI 가 "food 주문 500 이면 MySQL 인스턴스" 를 외워 찍지 못하게 하는 쌍이다. 가르는 관측 근거는 §8 에 둔다.

### 후보 목록 (실제 기전 × 부품, 숫자 순으로 줄 세움, 버린 이유 포함)

부품 지도의 0인 부품(commerce kafka, notification, gateway, nginx / banking kafka, nginx / food kafka, notify)은 이번 실행의 앞선 반복에서 원칙 7(비동기 경로라 사용자 증상 없음), 원칙 3(nginx OTel 없음), F34-R 교훈(게이트웨이 4xx 단일 신호)으로 거듭 막혔다(rejected-candidates.md). 출발은 `ref-real-world-incidents.md` 의 기전과 테스트베드의 실제 쓰기 경로다.

| # | 후보 | 원본 사례 | 부품, 층 | 결과 |
|---|---|---|---|---|
| 1 | food 주문 이벤트 outbox 표를 취소된 인덱스 백필이 치움 | GitHub 2026-07-24 | food MySQL 표(주문 쪽), 앱 층 | **채택**: 음식배달 15(최소), 정답 위치 0, 묶음 P 1. 원본의 '생성만 실패' 와 가장 가깝다. 로컬 실측과 119 조회로 관문 통과(§4, §7) |
| 2 | banking outbox_events 를 같은 백필이 치움(Oracle) | GitHub 2026-07-24 | banking Oracle 표, 앱 층 | 남김(순위 아래): 은행 17 > 음식배달 15, db.ddl 에 Oracle 보류 모드 신설 필요. TransferEventPublisher 는 outbox 실패를 'non-critical' 로 삼키지만 save 가 같은 트랜잭션을 rollback-only 로 표시해 이체가 되돌려질 것으로 보인다(로컬 미실측). outbox_events 14만 행, CLOB. 다음 반복 재료 |
| 3 | food payment outbox 표를 같은 백필이 치움 | GitHub 2026-07-24 | food MySQL 표(결제 쪽) | 남김(순위 아래): 결제 경로 10→11(18.6%)로 상한 아래지만 상한에 가깝고, 증상(order 502 'Payment service failed')이 F30-R, F06-P 와 겹쳐 1번보다 뒤 |
| 4 | food order_items 를 같은 백필이 치움 | GitHub 2026-07-24 | food MySQL 표(주문 품목) | 버림(컨트롤러 필수 중단 조건 위험, 원칙 9): 258만 행 138MB 라 그림자 복사(INSERT ... SELECT, REPEATABLE READ)가 수십 초 동안 원본 행과 표 끝에 공유 잠금을 걸어 주문 품목 INSERT 가 기다리고, order 풀(15)이 묶이면 health 의 연결 확인이 늦어 readiness 가 빠질 수 있다. 반복 8 보류 후보와 같은 표 |
| 5 | commerce shipments.order_id 유일 제약(인덱스 역할) 제거 | Chargebee 2018-03-02 공식 | commerce PostgreSQL 표(배송) | 버림(원칙 9, 7): 그 질의(findByOrderId)를 부르는 곳은 shipping 의 단일 스레드 Kafka 소비자뿐이라 사용자 경로 밖이고, 한 세션의 연속 전수 스캔(463만 행)이 0.5 CPU PostgreSQL 을 나눠 쓰는 정도라 checkout 5xx 계산이 서지 않음 |
| 6 | banking Oracle USERS 테이블스페이스 READ ONLY | Vapi 2025-01-21 공식 | banking Oracle 인스턴스 | 버림(카탈로그 §1): F38-R 과 원본이 같고 실패 꼴(쓰기만 거절, 읽기 정상)도 같아 DB 엔진만 바꾼 복제에 가깝다 |
| 7 | food payment 새 릴리스의 메모리 누수 | Honeycomb 2019-11-06 공식 | food payment | 버림(카탈로그 §1, 컨트롤러 필수 중단 조건): F41-R 과 원본, 기전이 같은 서비스 교체이고, order 가 트랜잭션 안에서 payment 를 기다려(F19-P 꼴) payment 가 GC 로 느려지면 order 풀이 묶여 entry 0 |
| 8 | banking Oracle 앱 계정 비밀번호 만료(PASSWORD_LIFE_TIME) | 공식 사례 못 찾음 | banking DB 계정 | 버림(원칙 1): 공식 사후 보고 없음, 증상(풀 수명 뒤 재접속 실패)이 F35-R 과 같음 |
| 9 | food MySQL wait_timeout 하향 | 공식 사례 못 찾음 | food MySQL 인스턴스 | 버림(원칙 1, 7): Hikari 가 빌릴 때 isValid 로 서버가 끊은 연결을 걸러 사용자 오류가 나지 않음 |
| 10 | commerce gateway 새 릴리스가 하류로 사용자 헤더를 빠뜨림 | 공식 사례 못 찾음 | commerce gateway(0) | 버림(원칙 1, F34-R 교훈): 하류가 401/400 으로 거절하는 4xx 단일 신호 |

1번을 먼저 고른 이유: 음식배달이 가장 적고(15), 정답 위치가 0 이며, 원본의 피해 모양('만들기' 하나만 실패, 나머지 정상, 되돌리면 즉시 회복)과 가장 가깝다. 2번은 기전이 같아 다음 반복에 은행 쪽으로 옮길 재료로 남긴다.

## 4. 인과 사슬 (코드와 인프라 위치)

```
db.ddl(mysql, 보류 표 모드): testbed-mysql-0 안 MySQL 클라이언트, lock_wait_timeout 10초
  CREATE TABLE _vt_vrp_<uuid>_<시각>_ LIKE order_outbox_events;
  ALTER TABLE _vt_vrp_... ADD INDEX idx_order_outbox_aggregate (aggregate_id);
  INSERT INTO _vt_vrp_... SELECT * FROM order_outbox_events          (약 5만 6천 행, 1~2초)
  RENAME TABLE order_outbox_events TO _vt_hld_<uuid>_<보류 기한>_     (취소 정리의 착오)
  → DPM: fooddelivery table_count 16→17, index_count 31→34 (그 분, 그림자 표의 PRIMARY, idx_order_outbox_unpublished, 새 인덱스)
  → order POST /api/orders: OrderService.createOrder (@Transactional)
       restaurantClient.getRestaurant, getMenu → 200
       dispatchClient.checkCapacity → 200
       orderRepository.save, orderItemRepository.save (주문과 품목 INSERT, 아직 커밋 전)
       log.info "Created order id=..."
       dispatchClient.dispatchCourier → dispatch 가 자기 트랜잭션으로 배차 기록, 200
       paymentClient.processPayment → payment 가 외부 PG 승인, 결제 기록, 200
       orderEventPublisher.publish → OutboxPublisher.publish → outboxEventRepository.save (IDENTITY, 즉시 INSERT)
         'insert into order_outbox_events (...)' → MySQL 1146 Table 'fooddelivery.order_outbox_events' doesn't exist
         → Hibernate SqlExceptionHelper WARN 'SQL Error: 1146, SQLState: 42S02', ERROR "Table ... doesn't exist"
         → ServiceException(500, "Failed to record outbox event: could not execute statement [...] [insert into order_outbox_events ...]")
       → 주문 트랜잭션 되돌림(주문, 품목 행 없음), GlobalExceptionHandler 가 500 과 메시지로 응답
  → OrderOutboxRelay.poll(2초마다) → OutboxRelay.relay (@Transactional)
       findTop100ByPublishedAtIsNullOrderByCreatedAtAsc → 1146
       → 스케줄러 ERROR 'Unexpected error occurred in scheduled task'(exception.message 에 그 SELECT 와 표 이름)
  → OrderOutboxRelay.purge(30초마다) → WARN 'Outbox purge failed, will retry next cycle: ...'
  ↔ order GET /api/orders/{id}, 검색: orders 만 읽음 → 200
  ↔ restaurant, dispatch(배달 조회), payment: 각자의 표와 각자의 outbox(dispatch_outbox_events, payment_outbox_events) → 정상
  ↔ order /actuator/health: DataSource isValid() → UP, Ready 유지
```

앵커(정답지 `code_anchor` 와 같음):

- `food-delivery/order-service/src/main/java/com/fooddelivery/order/entity/OrderOutboxEvent.java:7-9` (`@Table(name = "order_outbox_events")`)
- `food-delivery/order-service/src/main/java/com/fooddelivery/order/service/OrderService.java:57-170` (createOrder, `:141` 'Created order', `:162` outbox 기록)
- `food-delivery/shop-common/src/main/java/com/fooddelivery/common/outbox/OutboxPublisher.java:25-37` (save 실패 → ServiceException 500)
- `food-delivery/shop-common/src/main/java/com/fooddelivery/common/outbox/OutboxEvent.java:14-15` (IDENTITY)
- `food-delivery/order-service/src/main/java/com/fooddelivery/order/config/GlobalExceptionHandler.java:12-20`
- `food-delivery/order-service/src/main/java/com/fooddelivery/order/event/OrderOutboxRelay.java:19-27`
- `food-delivery/shop-common/src/main/java/com/fooddelivery/common/outbox/OutboxRelay.java:46-48`
- `food-delivery/order-service/src/main/resources/application.yml:20` (ddl-auto none)
- `food-delivery/db/init.sql:98-108` (order_outbox_events, 기본 키와 idx_order_outbox_unpublished, 외래 키 없음)
- 인프라: rca-testbed-food `testbed-mysql-0`(MySQL 8.0.46, tb-w3). 109 실측(2026-10-09 22:10 UTC, 읽기 전용 세션): 표 55,982행(미발행 3), `SHOW INDEX` 는 PRIMARY 와 idx_order_outbox_unpublished, fooddelivery 표 16개, `_vt` 표 0개, lock_wait_timeout 기본 31536000(스크립트가 세션에서 10 으로 낮춤), REPEATABLE READ. order 파드 `testbed-order-8694c6f795-kf4sq`(이미지 food-delivery-order:latest, 재시작 0)

로컬 실측(mysql:8.0 = 8.0.46 컨테이너, init.sql 의 orders, order_items, order_outbox_events 정의 그대로, outbox 3,000행 + 109 와 같은 main 의 order-service jar, 하류는 HTTP 모의):

- 주입 전 주문 3건 200(orders 3행, outbox 3,003행).
- 실행기 스크립트(`MYSQL_HOLD_REMOTE`)를 가짜 kubectl 로 돌림: preflight 통과, run 1초 안에 끝. 표 목록이 `_vt_hld_..._20261011220754_`, `_vt_vrp_..._20261009220753_`.
- 곧바로 주문 3건 모두 500, 본문 `Failed to record outbox event: could not execute statement [Table 'fooddelivery.order_outbox_events' doesn't exist] [insert into order_outbox_events (...)]`. orders 행 수 3 그대로(되돌림). health UP, GET /api/orders/1 200.
- 로그: 주문마다 WARN 'SQL Error: 1146, SQLState: 42S02' + ERROR "Table 'fooddelivery.order_outbox_events' doesn't exist", 2초마다 스케줄러 스레드에서 같은 두 줄과 ERROR 'Unexpected error occurred in scheduled task'(12초에 6번).
- 두 번째 run 은 check 에서 거절(rc 1, 원래 표 없음), cleanup 두 번(두 번째는 할 일 없음), recovery 통과, 곧바로 주문 200, `_vt` 표 0, outbox 행 그대로.

## 5. 원인 규정 (원칙 5, 6)

| 칸 | 값 | 근거 |
|---|---|---|
| `root_cause.target_id` | `food-mysql:fooddelivery.order_outbox_events` | 결함은 표 쪽이다(운영 도구가 살아 있는 표를 치움). 원본 사후 보고의 원인과 복구도 데이터베이스 변경(표를 지움)과 그 되돌림을 가리킨다. 표기는 F48-R, F36-R, F33-R 과 같은 '인스턴스:스키마.테이블' 꼴 |
| `trigger_target_id` | null | 근본과 계기가 같은 곳(이 표에 대한 운영 작업) |
| `scoring.partial` | `food-delivery-order`, `food-order`, `food-mysql` | 오류가 찍히는 서비스와 DB 인스턴스는 원인 근처다 |

원칙 5: 정답은 로그의 오류 문장("없는 표 order_outbox_events"), 실패한 문장이 outbox INSERT 라는 사실, 같은 DB 의 다른 쓰기 성공, 같은 분 DPM 표 수 증가, 그 시각에 배포나 재시작이 없다는 사실로 낼 수 있는 결론이다. 코드 설계 결함 추론이 필요 없다. "order 가 주문 생성에서 SQL 오류를 낸다" 고 답하면 order(부분), "order_outbox_events 표가 사라졌다(DDL, 운영 작업)" 고 답하면 정답이다. 실패한 주문의 배차와 결제가 남는 것(outbox 를 하류 호출 뒤에 쓰는 코드 구조)은 피해 설명이고 정답이 아니다.

원칙 6: order 와 릴레이는 늘 하던 정당한 요청을 보냈고 결함 있는 곳(사라진 표)에서 실패했다. 표를 치운 운영 작업이 원인이다.

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 | 기대 |
|---|---|---|
| 감지 | order 서버 스팬 오류(골든 시그널), 새 오류 로그 템플릿(1146 WARN, ERROR, 스케줄러 ERROR)과 로그 급증 | 주문 생성 100% 500. 로그는 부하와 무관하게 릴레이에서만 분당 약 90줄(2초마다 WARN, ERROR, 스택 붙은 ERROR), 여기에 주문마다 두 줄 |
| 피해 판정 | 러너: 기준선 주문 생성 5xx 비율 ≥ 0.8 과 2xx 비율 < 0.2(3틱) | 평시 주문 생성 5xx 는 0 근처(배차 한도 503 이 드물게) |
| 원인 설명 | 로그 전수(1146 오류 문장, 실패 문장, 'Created order' 계속, 'Recorded outbox event' 멈춤), DPM 표 수와 인덱스 수, dispatch, payment 로그와 스팬, KCM 무변화 | §7 |

인시던트 근거와 위험:

- 같은 order 주문 생성 500 꼴인 F38-R 이 정식(녹화 2026-10-09 17:14~17:29 UTC)이다. F38-R 은 order 외에 dispatch, payment 에도 오류 로그(read-only 966~3,874건)가 났지만, 이 후보는 order 하나에만 오류가 난다. 대신 order 의 새 템플릿이 셋(1146 WARN, ERROR, 스케줄러 ERROR)이고 릴레이 오류가 부하와 무관하게 2초마다 이어져 로그 급증 이벤트가 날 것으로 본다.
- 동반 부하가 없어 가장 한가한 시간(KST 03~06시)에는 주문 생성이 분당 약 12건, order 표본 스팬은 분당 약 1~2건이다. 스팬 기반 오류율 신호는 약하고 로그 신호가 주가 된다. 인시던트가 안 생기면 첫 실행 검증에서 묶음의 멤버 템플릿, 판정 사유, promoted_at 과 실행 시각대를 본다(§11).

## 7. 관측 근거 표 (119 실조회, 2026-10-09 21:50~22:20 UTC)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 결정 증거는 전수 로그와 DPM, KCM 이다.

| 증거 | 119 표와 칸 | 조회 | 평시 결과 |
|---|---|---|---|
| 근본: 없는 표 오류 | CH `lucida_logs_local` (`service_name`, `body`) | `SELECT min(timestamp), countIf(position(body,'1146')>0 AND position(body,'SQL Error')>0), countIf(position(body,'Failed to record outbox event')>0), countIf(position(body,'Unexpected error occurred in scheduled task')>0), countIf(position(body,'Created order id=')>0), count() FROM lucida.lucida_logs_local WHERE service_name='food-delivery-order'` | 1146 0건, 'Failed to record outbox event' 0건, 스케줄러 ERROR 14건(MySQL 재시작 때의 'Communications link failure'), 'Created order' 406,712건, 전체 839,975건(가장 이른 로그 2026-10-02 20:00 UTC) |
| 근본 경로가 CH 에 남는가(같은 로거, 같은 릴레이) | 같은 표, `log_attributes` | 스케줄러 ERROR 한 건의 `log_attributes` | `exception.message` 에 실패한 문장 전체('JDBC exception executing SQL [select ooe1_0.id, ... from order_outbox_events ooe1_0 where ooe1_0.published_at is null order by ooe1_0.created_at limit ?] [Communications link failure ...]')와 `exception.stacktrace` 가 남는다. 1146 도 같은 경로로 표 이름이 남는다 |
| 대조: F38-R 녹화 창(같은 주문 500) | 같은 표 | `position(body,'read-only')>0` 서비스별 | order 3,874, dispatch 1,298, payment 966건(2026-10-09 17:14~17:29 UTC). order 릴레이는 SELECT 는 되고 UPDATE 가 거절('Failed to publish outbox event ... read-only' 453건). F48-P 는 order 에만, 1146 으로, 릴레이 SELECT 부터 실패한다 |
| 다른 쓰기는 계속 | 같은 표 | 최근 1일 'Dispatched courier=', 'Processed payment id=', 'Recorded outbox event for order' | dispatch 56,222, payment 56,068, order 56,069건(주문마다 하나씩). 고장 중 앞의 둘은 이어지고 마지막만 멈춘다 |
| 계기: 백필(그림자 표와 새 인덱스) | VM `dpm.mysql.database.table_count{db_name="fooddelivery"}`, `dpm.mysql.database.index_count` (target c8c558e5-…, 60초 간격) | 현재값, `max_over_time`, `min_over_time` [7d] | table_count 현재 16, 7일 최대 16, 최소 16. index_count 31. F48-R 시트 §7 이 2026-10-01~10-09 표 수 16 고정과 F33-R 의 index_count 변화(30)를 같은 지표로 확인한 선례다 |
| 계기: DDL 문장 자체 | CH `dpm_topsql_local` | DDL 검색 | F48-R 시트와 같이 DDL 은 Top SQL 에 남지 않는다 |
| outbox 표의 평시 질의 | `dpm_topsql_local` `body.sqlText` | `order_outbox_events` 를 포함한 문장 | 릴레이 SELECT 11,351행, 정리 SELECT 2,496, DELETE 1,669, UPDATE 1,446, INSERT 1,076행(2026-08-13~10-09). 이 표가 주문마다 쓰이는 표임을 보여 준다 |
| 다른 변경 없음 | CH `kcm_events_local` | `namespace='rca-testbed-food' AND position(object_name,'testbed-order')=1` 사유별 | Unhealthy 196건(마지막 2026-10-09 03:43), ScalingReplicaSet 8건(마지막 2026-10-08 18:53, 롤아웃은 이 표에 남는다). 109 kubectl: 파드 나이 27시간, 재시작 0 |
| 피해: 주문 생성 스팬 | CH `otel_traces_local` (`span_kind='SERVER'`, `span_name`, `http.response.status_code`) | 7일 order 서버 스팬 | POST /api/orders 18,652(5xx 530, 대부분 F38-R, F49-H 등 시나리오 실행 창), health 4,885(5xx 2) |
| 기준선 주문량 | `lucida_logs_local` | 'Created order id=' 를 KST 시각별 7일 평균 | 분당 11.9(06시)~68.4(17시)건 |
| 배제: DB 정상 | VM `dpm.mysql.session.active_session`, `dpm.mysql.instance.avg_query_response_time` (target c8c558e5-…) | `quantile_over_time(0.5, ...[7d])` | 7일 중앙 1 세션, 7.25ms |

## 8. 감별

- must_support: 1146 오류 로그(평시 0), 실패 문장이 outbox INSERT 이고 'Created order' 는 이어지며 'Recorded outbox event' 는 멈춤, 같은 분 DPM 표 수 16→17과 인덱스 수 31→34, dispatch, payment 쓰기와 로그 계속, 롤아웃과 재시작 없음, MySQL 과 order 풀 정상.
- must_rule_out(정답지에 문장으로): order 새 버전, MySQL 인스턴스 읽기 전용(F38-R), MySQL 다운이나 재시작, 하류(가게, 배차, 결제) 고장, 부하 증가나 용량 부족.
- contrast_with: F48-R(같은 원본과 도구, 읽기 기능 하나만 실패), F38-R(같은 주문 500, 인스턴스 read_only), F36-R(food MySQL 스키마 쪽 고장, 가게 조회 실패로 주문 502).
- F38-R 과 가르는 관측 근거(겉 증상이 같은 H 관계):
  - 오류 번호와 문장: F38-R 은 1290 'The MySQL server is running with the --read-only option', F48-P 는 1146 "Table 'fooddelivery.order_outbox_events' doesn't exist".
  - 실패 범위: F38-R 은 order, dispatch, payment 모든 쓰기가 실패한다(녹화 창 로그 3,874/1,298/966건). F48-P 는 order 의 그 표 문장만 실패하고 dispatch 'Dispatched courier', payment 'Processed payment' 는 이어진다.
  - order 안의 위치: F38-R 은 주문 INSERT 에서 실패해 하류 호출이 없고, F48-P 는 'Created order' 와 배차, 결제 호출 뒤 outbox 기록에서 실패한다.
  - 계기: F48-P 만 DPM 표 수와 인덱스 수가 바뀐다.
- 판별력: 서비스 입도 채점에서는 F38-R, F49-H 와 같은 'order' 근처 답이 부분 점수다. 정답 입도(database-relation)에서는 표 이름이 정답이고, 인스턴스(food-mysql)는 부분 점수다.

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

진행 대본: 설계 강도 하나로 고정한 평가 모드(`approved-fixed-f48-p`). 강도라는 축이 없다: 표가 없으면 그 표에 쓰는 문장은 요청량과 무관하게 100% 실패한다.

| 항목 | 값 | 근거 |
|---|---|---|
| primary | db.ddl(보류 표 모드), 계약 `{engine mysql, rca-testbed-food, testbed-mysql-0, fooddelivery, order_outbox_events, backfill_index idx_order_outbox_aggregate, backfill_column aggregate_id, minimum_rows 1000}` | 실행기 CONTRACTS 와 profiles.json 같은 값(G2). 스크립트는 F48-R 과 같고 계약만 다르다(테스트로 고정) |
| companion | 없음 | 아래 배달원 한도 계산. F14-P 가 동반 부하 없는 평가 모드 선례 |
| entry_status | 기준선 문서(domain food-delivery)의 주문 생성 단계 | 고장 중 500(0 아님). 0 은 노드나 order 가 죽었을 때만 |
| min_hold / settle / timeout | 15m / 30s / 20m, max_injection 25m | F48-R, F38-R 과 같음 |
| success(3틱) | 기준선 주문 생성 5xx 비율 ≥ 0.8, 2xx 비율 < 0.2 | loadgen 집계(표본 아님), 30초 창에 6~34건 |
| must_rule_out(2틱) | MySQL NotReady, order NotReady | DB 다운이나 order 다운이면 다른 시나리오다 |
| abort | entry_status == 0 (필수) | |
| recovery(2틱, 10m) | target_health 200, MySQL Ready, order Ready, 기준선 주문 2xx ≥ 0.7 | 되돌리면 다음 주문부터 즉시 200(로컬 실측) |

부하 계산:

- 기준선 주문 생성: 분당 약 12(KST 03~06시)~68건(15~18시), 30초 창 6~34건. 고장 중 모두 500 → 5xx 비율 1.0, 2xx 비율 0. 문턱(0.8, 0.2)을 어느 시간대에도 넘는다.
- 왜 동반 부하를 두지 않나: 실패한 주문도 배차가 dispatch 의 자기 트랜잭션으로 남는다(ASSIGNED, ETA 15~34분 뒤 만료). 평시 ASSIGNED 는 주문률 × 평균 ETA(약 24.5분)라 한가할 때 약 300(109 실측 2026-10-09 22:11 UTC 327), 가장 바쁠 때 약 1,700 이다(한도 2000). 주문 동반 부하 order-surge.js 2rps(주문 생성 초당 약 1.4건, F38-R, F49-H 와 같은 값)를 더하면 가장 바쁜 시간에 (2000 − 1,700) / 1.4 ≈ 214초 만에 한도가 차서 주문이 용량 확인 단계에서 503 'No courier available' 로 바뀌고 실패 지점과 오류가 섞인다. 초당 0.3건만 더해도 25분 안에 한도를 넘는다. 기준선만 쓰면 ASSIGNED 는 평시와 같다.
- 강도와 피해: 주입은 표 이름 하나를 바꾸는 메타데이터 작업이고 그림자 복사는 약 5만 6천 행(12MB) 1~2초다. 복사 동안 REPEATABLE READ 의 공유 잠금이 원본 표 끝에 걸려 그 사이 들어온 outbox INSERT 가 1~2초 기다릴 수 있지만 그 뒤 곧바로 RENAME 이 이어진다. order 는 하류 호출(평소 수백 ms) 뒤 즉시 SQL 오류로 끝나 풀을 오래 쥐지 않는다(F38-R 과 같은 모양, F38-R 녹화에서 order Ready 유지).
- 서킷브레이커: order 의 restaurant, dispatch, payment 서킷은 하류가 200 이라 열리지 않는다. order 앞에는 서킷이 없어 손님이 500 을 그대로 받는다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(GitHub 2026-07-24, 공식 월간 가용성 보고)와 요소별 대응표가 있고 기전이 같다 (§2)
- [x] 분류 장부 §4-1, §6 갱신. 묶음 P 1→2(3.4%, L 로 세도 10.2%), 정답 위치 DB 테이블(주문 이벤트 outbox) 0→1(1.7%), 결제 경로 10/59(16.9%), 음식배달 15→16. 어느 축도 20% 미만 (§3)
- [x] 근본 원인 위치: `OrderOutboxEvent.java:7-9`, `OrderService.java:162`, `OutboxPublisher.java:25-37`, 인프라 testbed-mysql-0 order_outbox_events (§4)
- [x] 근본 원인 흔적: CH `lucida_logs_local` 1146 오류 문장(전수, 같은 릴레이 경로의 스케줄러 ERROR 가 exception.message 에 문장과 표 이름을 남기는 것 확인) (§7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: 로그 전수, DPM 표 수와 인덱스 수, KCM. 스팬은 보조 (§7)
- [x] 계기 흔적: DPM table_count 16→17, index_count 31→34(1분 간격, 7일 동안 16 고정), 오류 문장의 표 이름, 그 시각에 롤아웃과 재시작 없음. 인공 지연 없음 (§7)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다. 배차, 결제가 남는 코드 구조는 피해 설명에만 둔다 (§5)
- [x] 정답지 세 칸이 원칙 6대로 (§5)
- [x] 감지, 피해 판정, RCA 증거 구분과 인시던트 위험 (§6)
- [x] 주입 도구가 시나리오 id 를 남기지 않는다: 표 이름은 Vitess 꼴 접두사, 무작위 uuid, 시각뿐이고 인자와 스크립트에 id 없음(테스트로 고정). 동반 부하가 없어 k6 태그도 없다
- [x] 부하 상한과 서킷브레이커를 고려한 피해 계산, 가장 한가한 시간대와 가장 바쁜 시간대 둘 다 (§9)

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- order 로그에 'SQL Error: 1146, SQLState: 42S02' 와 "Table 'fooddelivery.order_outbox_events' doesn't exist", 스케줄러 ERROR 'Unexpected error occurred in scheduled task' 가 2초마다 남는지. POST /api/orders 스팬이 500 이고 응답이 'Failed to record outbox event' 인지.
- 'Created order id=' 는 이어지고 'Recorded outbox event for order' 가 멈추는지, dispatch 'Dispatched courier' 와 payment 'Processed payment' 가 이어지는지.
- VM 의 `dpm.mysql.database.table_count{db_name="fooddelivery"}` 가 주입 분에 17, `index_count` 가 34 가 되고 cleanup 뒤 16, 31 로 돌아오는지.
- 인시던트: 묶음 멤버 템플릿과 판정 사유, promoted_at, 실행 시각대. 한가한 시간에 스팬 신호가 약해 인시던트가 안 생겼다면 그 사실을 검증 보고서에 남긴다. 보강 후보는 주문 동반 부하가 아니라(배달원 한도, §9) 한가한 시간을 피하는 실행 시각이다.
- 주입이 메타데이터 잠금 대기로 실패(lock_wait_timeout 10초)하면 아무것도 바뀌지 않은 채 run 이 실패하고 MySQL 오류 첫 줄이 러너 로그에 남는다. 그림자 표만 만들어진 채 RENAME 이 실패했다면 cleanup 이 그림자를 지운다.
- cleanup 뒤 `order_outbox_events` 가 행 그대로이고 `_vt_` 로 시작하는 표가 없는지(recovery 가 확인). 고장 중 쌓이지 못한 주문 이벤트는 없다(주문이 되돌려졌으므로). 실패한 주문의 배차(ASSIGNED)는 ETA 뒤 평소처럼 만료되고, 승인된 결제 행은 남는다(F49-H 와 같은 성격의 잔여 데이터).
- 녹화 창에 food MySQL OOM 재시작이 끼면 녹화로 쓰지 않는다.
