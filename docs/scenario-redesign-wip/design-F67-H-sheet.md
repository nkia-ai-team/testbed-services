---
title: F67-H 설계 시트 (commerce 주문 품목 표 열 추가 마이그레이션이 느린 매출 내보내기 뒤에서 잠금을 기다려 checkout 마다 품목 INSERT 가 order 연결을 쥔 채 멈춤)
status: Draft
owner: project
last_reviewed: 2026-10-11
tags:
  - scenario
  - design
  - database
summary: 주문 품목을 매출 분석 저장소로 옮기는 데이터 작업이 commerce PostgreSQL order_schema.order_items(약 643만 행)를 통째로 읽는 느린 내보내기(COPY, 약 6분 활성)로 ACCESS SHARE 를 쥔 사이, 다른 팀이 같은 표에 품목 할인 금액 열을 더하는 마이그레이션(카탈로그만 바꾸는 안전한 변경)을 돌려 ACCESS EXCLUSIVE 를 기다리고, 그 뒤의 order_items 문장이 모두 줄을 서는 시나리오. order 의 checkout 은 한 트랜잭션에서 주문 행 바로 뒤, 결제 호출 전에 품목을 INSERT 하므로 그 INSERT 가 order 연결을 쥔 채 멈추고 order 풀(10)이 약 2초 안에 마른다. 게이트웨이 order 시간 초과와 서킷으로 checkout 이 5xx 이고, order 가 NotReady 와 재시작에 빠진다. payment 는 불리지 않는다. 내보내기가 끝나면 스스로 풀린다. 원본은 Honeycomb 2024-08-06, PostgreSQL 기전 보조는 GoCardless. F67-R(결제 로그 표 VACUUM FULL 로 checkout 5xx)과 겉 증상이 같고 원인이 다르다(H).
---

# F67-H 설계 시트

## 1. 요약

주문 품목을 매출 분석 저장소로 옮기는 데이터 작업이 commerce PostgreSQL 의 주문 품목 표 `order_schema.order_items` 를 통째로 읽는 내보내기 `COPY (SELECT id, order_id, product_id, product_name, quantity, unit_price, subtotal FROM order_schema.order_items) TO STDOUT` 를 시작한다. 받는 쪽이 일정한 속도로 받아(약 643만 행) 문장이 약 6분 동안 활성이고, 그동안 이 표의 ACCESS SHARE 잠금을 쥔다. 몇 초 뒤 다른 팀이 품목 할인 기능을 준비하며 `ALTER TABLE order_schema.order_items ADD COLUMN discount_amount numeric(12,2)` 마이그레이션을 돌린다. 기본값도 NOT NULL 도 없는 열 추가는 카탈로그만 바꾸는 "안전한" 변경이지만 그 순간 ACCESS EXCLUSIVE 잠금이 필요하다. 마이그레이션은 내보내기 뒤에서 기다리고, 기다리는 ACCESS EXCLUSIVE 는 모든 잠금과 충돌하므로 그 뒤의 order_items 문장(INSERT, 읽기)은 모두 줄을 선다. 역할과 DB 에 lock_timeout, statement_timeout 이 없어(0) 아무도 포기하지 않는다.

order-service 의 checkout 은 한 트랜잭션이다. cart 조회, pricing 견적, 재고 예약 뒤 주문을 저장하는데 IDENTITY 키라 주문 행(orders)과 품목 행(order_items)이 곧바로 INSERT 되고, 그 뒤에야 payment 를 부른다. 주문 행은 들어가고 품목 INSERT 가 줄을 서면서 트랜잭션은 order 의 DB 연결을 쥔 채 멈춘다. 동반 부하의 checkout(초당 약 5건)으로 order Hikari 풀(10, 기본값, 연결 대기 30초)이 약 2초 안에 묶이고, 뒤의 checkout 은 트랜잭션을 시작하지도 못한다. 게이트웨이는 order 읽기 시간 제한 15초(재시도 1회) 끝에 5xx 를 내고 order 서킷이 열린다. order 의 health 에 DB 확인이 있어 readiness 가 빠지고 liveness 가 재시작하며, 다시 뜬 파드는 기동 schema.sql 의 첫 인덱스 문장 `CREATE INDEX IF NOT EXISTS idx_orders_user`(:14, orders 표의 SHARE 잠금)가, 죽은 파드가 남긴 멈춘 checkout 세션들이 주문 행 INSERT 로 쥔 orders 의 ROW EXCLUSIVE 잠금 뒤에 서서 startupProbe 에 다시 시작된다. payment 는 불리지 않아 결제와 은행 정산이 없다. 상품 둘러보기, 로그인, 장바구니는 정상이다. 내보내기가 끝나면 마이그레이션이 잠금을 얻어 열을 즉시 더하고 줄이 풀린다.

비유: 계산대 직원(order)은 영수증 본문(orders)을 쓰고 이어서 품목 칸(order_items)을 채워야 카드 단말(payment)로 넘어간다. 품목 장부 앞에 "칸 하나 추가, 잠깐이면 됨" 공사 팻말(마이그레이션)이 섰는데, 마침 누가 품목 장부를 통째로 베끼는 중(내보내기)이라 공사가 기다린다. 공사가 줄 맨 앞에 서 있어 품목을 적으러 온 직원들은 계산대(DB 연결)를 쥔 채 서 있고, 계산대 열 개가 다 차서 뒤 손님은 줄도 못 선다. 카드 단말도 장부도 멀쩡한데 계산이 6분 동안 막힌다.

## 2. 원본 사례

- **Honeycomb, 2024-08-06** (공식 상태 페이지 사고 "UI and API unavailable" 과 사후 보고, 2024-08-07 게시): https://status.honeycomb.io/incidents/ycgvhvs201q1
  - 12:59:39~13:20:05 PDT. API 가 요청의 75% 를 거절(대부분 /1/auth 인증 끝점), UI 는 적어도 19분 쓸 수 없었고 텔레메트리 약 4분의 1 을 버렸으며 트리거 평가가 멈춤.
  - "The schema migration was technically safe—a column addition to the `teams` table using an `INSTANT` algorithm". 마이그레이션 몇 초 앞서 같은 표를 읽는 질의가 시작됐고 그 느린 SELECT 는 5분 넘게 걸릴 수 있었음. 마이그레이션이 잠금을 얻으려다 기다리는 동안 그 표의 다른 질의를 막음.
  - 탐지: 13:00 경 저하 인지, 13:04 경보. DB 성능과 스키마 마이그레이션의 상관을 좁히는 데 몇 분. 후속: 느린 질의 점검, 클라이언트 시간 제한과 맞는 DB 쪽 시간 제한.
- **GoCardless** (공식 엔지니어링 블로그 "Zero-downtime Postgres migrations - the hard parts"): https://gocardless.com/blog/zero-downtime-postgres-migrations-the-hard-parts/ — 같은 기전의 PostgreSQL 설명(얻지 못한 잠금은 대기열에 들어가고 그와 충돌하는 뒤 잠금이 모두 뒤에 줄을 섬)과 그 기전으로 약 15초 API 가 멈춘 사고. 재구성 엔진(PostgreSQL)에서 고리가 원본과 같음을 보이는 보조 원본이다.
- 둘 다 자료 문서 M21 에 이미 있다(F66-R 에서 추가). 이번에 더한 사실은 없다. 같은 반복에서 확인한 Bolt 2019-12-19(공식 상태 페이지 https://status.bolt.com/incidents/nlzmc2cw0jgn: 일상 제품 갱신의 DB 마이그레이션이 끝나지 않아 DB 가 잠겨 15분 동안 checkout 과 가맹점 대시보드 중단, DB 재시작으로 복구)도 같은 고리로 보이나 무엇이 마이그레이션을 막았는지는 공식 블로그가 403 으로 열리지 않아 확인하지 못해 원본으로 쓰지 않았다.

### 요소별 대응표

| 요소 | 원본 사례 (Honeycomb 2024-08-06) | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | teams 표에 열을 더하는 스키마 마이그레이션(INSTANT, 기술적으로 안전) | 주문 품목 표에 기본값 없는 null 허용 열(discount_amount numeric(12,2))을 더하는 마이그레이션(카탈로그만) |
| 원인이 된 결함 | 몇 초 앞서 시작된 같은 표의 느린 SELECT(5분 넘게)가 끝나기를 마이그레이션이 기다리며 잠금 요청을 대기열에 세움. DB 쪽 시간 제한 없음 | 몇 초 앞서 시작된 같은 표의 느린 매출 내보내기(COPY, 약 6분 활성)가 ACCESS SHARE 를 쥐고 마이그레이션의 ACCESS EXCLUSIVE 요청이 대기열에 섬. lock_timeout, statement_timeout 0 |
| 전파 경로 | 기다리는 잠금 요청 뒤로 그 표의 다른 질의(인증 포함)가 막힘 | 기다리는 ACCESS EXCLUSIVE 뒤로 order_items 쓰기가 막힘 → checkout 트랜잭션이 품목 INSERT 에서 order 연결을 쥔 채 멈춤 → order 풀 고갈 → 게이트웨이 order 시간 초과와 서킷, order NotReady 와 재시작 |
| 사용자 증상 | API 요청 75% 거절, UI 사용 불가 | commerce checkout 5xx. 상품 둘러보기, 로그인, 장바구니, 은행 쪽은 정상 |
| 원본의 탐지 경로 | 저하 인지와 경보, DB 성능과 스키마 마이그레이션의 상관 | lucida-next 의 게이트웨이 오류 로그, commerce-order Hikari 오류 로그, DPM 세션 잠금 대기 사슬, KCM testbed-order Unhealthy |
| 완화와 복구 | 느린 질의가 끝나 마이그레이션이 지나가며 회복. 후속으로 DB 쪽 시간 제한 | 내보내기가 끝나 마이그레이션이 지나가며 스스로 회복. cleanup 이 더한 열을 지움 |

기전 일치: 원인("그 자체로는 안전한 열 추가가 같은 표의 느린 읽기 뒤에서 잠금을 기다리며 대기열 맨 앞에 섬")에서 증상("그 표를 쓰는 요청 경로가 막혀 요청을 거절")까지 고리가 같다.

바꾼 것(기전 고리 밖):
- 엔진: MySQL 메타데이터 잠금 → PostgreSQL 표 잠금. 두 엔진 모두 "기다리는 배타 요청이 뒤의 공유 요청을 막는" 대기열 규칙이 같다(GoCardless). 열 추가가 표를 다시 쓰지 않는 점(INSTANT ↔ 기본값 없는 null 허용 열)도 같다.
- 느린 읽기의 정체: 원본은 앱의 느린 SELECT. 재구성은 큰 표를 분석 저장소로 옮기는 내보내기의 COPY 로, 받는 쪽이 일정 속도로 받아 약 6분 활성이다. 서버 쪽에서는 둘 다 같은 표의 공유 잠금을 쥔 채 오래 도는 읽기 문장이다. 643만 행 표를 웨어하우스로 옮기는 내보내기가 수 분 걸리는 것은 자연스럽다(F66-P 의 7만 행 outbox 와 달리 억지가 아니다).
- 막히는 경로: 원본은 그 표를 읽는 인증 경로. 재구성은 그 표에 쓰는 checkout 트랜잭션이다.
- 길이: 원본 약 20분, 재구성 약 6~7분(대기 세션이 연결 한도를 넘지 않게, §9).

## 3. 숫자 근거 (2026-10-11, `scenario-stats.py`, 정식 48 + 후보 48 = 96, 이 후보 전)

- 묶음(정식 + 후보): J 16(16.7%), D 13(13.5%), G 12, P 11, L 8, A 7, B 7, C 6, E 4, F 3, H 2, K 2, Q 2, I 1, M 1, O 1, N 0. 금지(20%) 없음. 이 후보 D 13→14(97 중 14.4%).
- 정답 위치: 주문 서비스 8(8.3%)이 가장 크고, **DB 테이블(주문 품목) 0 → 1**(1.0%). 같은 '주문' 이라도 주문 행 표(DB 테이블(주문), food orders F53-R)와 다른 표다(장부에서 배차/배차 이력, 결제/결제 로그를 나눠 센 것과 같은 기준). 결제 경로 합계 13(13.5% → 97 중 13.4%, 이 후보와 무관).
- 서비스: 쇼핑몰 32, 은행 32, 음식배달 32(세 도메인 동률). 원칙 2 완화 규칙의 앞선 적용(반복 45, 52: 최다 동률이면 최다로 본다)대로 세 도메인 모두 정답 위치가 0 인 부품만 낸다. 쇼핑몰 32 → 33.
- 부품 지도: 0 인 부품은 세 도메인 kafka, commerce notification, nginx 인데 막힌 목록의 벽(outbox 비동기라 사용자 증상 없음, nginx 는 119 에 로그와 스팬이 없음)에 걸린다. 정답 위치가 0 으로 남은 것은 대부분 DB 표이고, 그중 사용자 경로에서 쓰이며 큰 표는 commerce order_items 다. food menus 와 food order_items 는 order(음식배달 입구)를 막아 컨트롤러 필수 중단 조건에 걸린다(아래 표).
- 겉 증상(checkout 5xx, 결제 단계 근처의 풀 고갈과 재시작)은 F67-R(결제 로그 표 VACUUM FULL)과 같다. 증상 겹침은 버릴 사유가 아니라 H 관계로 판별력을 더한다: F67-R 은 payment 가 결제 로그 INSERT 에서 멈추고 사슬 맨 앞이 잠금을 쥔 채 다시 쓰는 VACUUM FULL 이다. F67-H 는 order 가 결제 전에 품목 INSERT 에서 멈추고 사슬이 기다리는 DDL → 오래 도는 내보내기의 두 단이다.

### 같은 원본의 다른 시나리오와의 관계 (카탈로그 §1)

같은 원본, 같은 db.ddl PostgreSQL 잠금 대기열 모드(F66-R, F66-P)다. 파라미터가 다르고(표, 열, 내보내기 열 목록), 서비스 이름만 바꾼 복제가 아니다:
- 막히는 서비스와 문장: F66-R 은 user 의 회원 표 단순 읽기(게이트웨이 인가), F66-P 는 inventory 의 예약 트랜잭션 끝 outbox INSERT(재고 행 잠금까지 붙들림), F67-H 는 order 의 checkout 트랜잭션 중간 품목 INSERT 다(주문 행만 쓴 채, 결제 전).
- 피해 경로: F66-R 은 인가된 쓰기 전체 500(둘러보기 정상), F66-P 는 product 서킷이 열려 둘러보기까지 실패, F67-H 는 checkout 만 실패하고 결제가 일어나지 않는다. 마르는 풀이 각각 user, inventory, order 다.
- MySQL 판(F60-H dispatches, F53-H payments)과는 엔진, 도메인, 잠금 종류(메타데이터 잠금 대 표 잠금)가 다르다.
- 관제 AI 가 배울 판단: 재시작하는 order 와 checkout 실패만 보면 order 서비스(주문 서비스 8, 최다 정답 위치)를 찍기 쉽다. DB 세션 사슬의 relNames 를 따라가 order_items 와 그 앞의 DDL, 내보내기에 닿아야 정답이다.

### 후보 목록 (3단계, "실제 기전 × 부품", 숫자 순)

| # | 후보 | 층, 부품 | 원본 사례 | 결과 |
|---|---|---|---|---|
| 1 | 배포 마이그레이션의 비온라인 CREATE INDEX 가 commerce order_items 쓰기를 막아 checkout 실패 | DB, commerce order_items(D, DB 테이블(주문 품목) 0) | Harness 2026-01-26 공식(자료 문서 M21) | 버림: 원칙 9. 로컬 postgres:16-alpine 0.5 CPU 같은 크기 사본(643만 행)에서 CREATE INDEX (product_id) 12.3초, (product_id, order_id) 15.6초, (product_name) 32.8초. 판정(약 105초)보다 짧다(F67-R 시트의 order_items VACUUM FULL 34초 벽과 같음) |
| 2 | 같은 인덱스 생성을 결제 로그 표(1,868만 행)에 | DB, commerce payment_logs(D, DB 테이블(결제 로그) 1) | Harness 2026-01-26 공식 | 버림: 원칙 2. 세 도메인 동률이라 정답 위치 0 인 부품만 |
| 3 | **commerce order_items 열 추가 마이그레이션이 느린 매출 내보내기 뒤에서 잠금 대기열** | DB, commerce order_items(D, DB 테이블(주문 품목) 0) | Honeycomb 2024-08-06 공식, GoCardless 공식 | **채택** |
| 4 | order_items 의 외래 키를 ON DELETE CASCADE 로 다시 거는 마이그레이션(표 검증 스캔 동안 orders, order_items 쓰기 차단) | DB, commerce order_items(D) | Pulumi 2023-10-06 공식 | 버림: 원칙 9. 같은 PG 판을 payment_logs(1,868만 행)에 해도 22.1초(막힌 목록 303행), order_items 는 더 작다 |
| 5 | 열 추가와 전 행 백필을 한 트랜잭션에 묶은 마이그레이션이 order_items 를 수 분 잠금 | DB, commerce order_items(D) | 공식 사후 보고 못 찾음(웹 검색 1회: 개인 블로그, Django 글뿐) | 버림: 원칙 1 |
| 6 | 운영을 가리킨 로컬 마이그레이션이 commerce order_items 를 지움 | DB, commerce order_items(P) | Resend 2024-02-21 공식 | 버림: 카탈로그 §1. F53-R(주문 표 지우기로 주문 저장 즉시 실패)과 같은 꼴이고 엔진만 다르다(막힌 목록 244행 벽) |
| 7 | 이전 뒤 시퀀스 상태 누락으로 commerce order_items 기본 키 충돌 | DB, commerce order_items(Q) | RevenueCat 2022-11-23 공식 | 버림: 카탈로그 §1. F57-P 가 같은 commerce DB 이전 이야기를 다른 표에 이미 썼다 |
| 8 | food menus 열 추가 마이그레이션이 메뉴 내보내기 뒤에서 잠금 대기 | DB, food menus(D, DB 테이블(메뉴) 0) | Honeycomb 2024-08-06 공식 | 버림: 컨트롤러 필수 중단 조건, 원칙 1. menus 는 order_items 의 외래 키 부모라 주문 INSERT 도 메타데이터 잠금을 기다려 order(입구) 풀과 readiness 가 빠지고(막힌 목록 264행 로컬 실측), 표가 작아 수 분 내보내기가 억지다 |
| 9 | 끝나지 않은 마이그레이션(사용 안 하는 열 지우기)의 DB 잠금으로 checkout 중단 | DB, commerce order_items(D) | Bolt 2019-12-19 공식 상태 페이지 | 버림: 원칙 1, 6. 무엇이 막았는지 공식 블로그가 403 이라 확인 불가, DROP COLUMN 은 끝나면 쓰는 열을 지워 되돌릴 수 없다(같은 고리는 3번이 Honeycomb 으로 담음) |
| 10 | 세 도메인 Kafka, commerce notification, food notify, nginx(정답 위치 0) | 메시지 브로커, 알림, 진입 프록시 | 막힌 목록의 여러 원본 | 버림: 원칙 7, 3(outbox 비동기라 사용자 증상 없음, nginx 는 119 관측 없음, 막힌 목록 27, 98, 238행) |
| 11 | tb-w3 conntrack 표 축소 | 노드 커널 한도 | 공식 사후 보고 없음 | 버림: 원칙 1(막힌 목록 25행) |
| 12 | 새 버전 배포(J) 후보 일반 | 앱 서비스 | 여러 원본 | 버림: 원칙 2. 세 도메인 동률인데 모든 동기 앱 서비스의 정답 위치가 1 이상 |

부품: commerce order_items, payment_logs, food menus, Kafka, notification, nginx, 노드(7종). 앱, DB, 인프라 층.

## 4. 인과 사슬과 코드, 인프라 위치

1. 주입: db.ddl 실행기(`scripts/scenarios/profiles/db_ddl_executor.py`, 계약 `CONTRACTS["F67-H"]`, 원격 본문 `PG_QUEUE_REMOTE` 의 `export_columns` 분기)가 러너에서 kubectl exec 로 testbed-postgres-0 안의 psql 을 띄운다. 내보내기 `COPY (SELECT ... FROM order_schema.order_items) TO STDOUT` 를 행 추정/360 행마다 1초 쉬는 awk 에 흘리고, 그 세션이 표 잠금을 쥔 것을 본 뒤 `ALTER TABLE order_schema.order_items ADD COLUMN discount_amount numeric(12,2)` 를 띄운다. 서버가 ALTER 를 내보내기 뒤에서 기다리는 것(pg_blocking_pids)이 보이면 run 이 끝난다.
2. checkout: `commerce/order-service/src/main/java/com/commerce/order/service/OrderService.java:144-200` (checkout @Transactional). cart(147), pricing(155), productClient.reserveStock(160) 뒤 `orderRepository.save(order)`(187), 그 뒤 `paymentClient.requestPayment`(191).
3. 품목 INSERT: `commerce/order-service/src/main/java/com/commerce/order/entity/Order.java:34` (items @OneToMany cascade ALL), `OrderItem.java:7-11` (order_schema.order_items, IDENTITY). IDENTITY 라 save 때 주문 행에 이어 품목 INSERT 가 곧바로 나간다. Top SQL 의 품목 INSERT 문장은 `insert into order_schema.order_items (order_id,product_id,product_name,quantity,subtotal,unit_price) values (...)`(sql_hash 705941650521247677).
4. order 풀: `commerce/order-service/src/main/resources/application.yml:7-10` hikari 설정 없음(기본 최대 10, 연결 대기 30초). 기동마다 `spring.sql.init.mode always`(19-21)로 `schema.sql` 을 돈다. order_items 의 인덱스 문장(`schema.sql:26`)보다 앞의 `schema.sql:14` `CREATE INDEX IF NOT EXISTS idx_orders_user ON order_schema.orders(user_id)` 가 orders 의 SHARE 잠금을 먼저 얻으려 하는데, 죽은 파드의 멈춘 checkout 세션들이 주문 행 INSERT 로 orders 의 ROW EXCLUSIVE 를 쥔 채(트랜잭션이 끝나지 않아) 서버에 남아 있어 그 뒤에 선다. 다시 뜬 파드는 :26 까지 가지 못한다(1차 평가 로컬 postgres:17 실측: 기동 세션의 pg_blocking_pids = 멈춘 checkout 세션, orders ShareLock granted=f, checkout 세션은 orders RowExclusiveLock granted=t).
5. 게이트웨이: `commerce/api-gateway/src/main/resources/application.yml:34-37`(order read-timeout 15s), 79-85(order 서킷: 창 10, 최소 5, 실패율 50%, 열림 5초), 122-126(order 재시도 2회, 300ms 지수). `ProxyService.java:104` 'Downstream unavailable for ...' WARN.
6. 프로브: `commerce/k8s/20-order-service.yaml:87-106` startupProbe 5초 x 30, readinessProbe 10초 x 3(시간 제한 3초), livenessProbe 15초 x 5, 모두 `/actuator/health`(DB 확인 포함).
7. DB: `commerce/k8s/10-postgres.yaml` testbed-postgres(PostgreSQL 16.14 alpine, cpu 500m, 512Mi, tb-w1). 109 읽기 전용 실측(2026-10-11): order_schema.order_items 6,429,852행(힙 616MB, 전체 910MB, pkey 150MB, idx_order_items_order 143MB), 열 7개(id, order_id, product_id, product_name, quantity, unit_price, subtotal), order_schema.orders 4,710,624행, 열린 세션 63, max_connections 100, lock_timeout 0, statement_timeout 0, shared_buffers 160MB, maintenance_work_mem 64MB. 재고 16행(재고 18~60).

### 로컬 실측 (2026-10-11, 원칙 9 근거)

postgres:16-alpine 컨테이너(`--cpus 0.5`, shared_buffers 160MB, maintenance_work_mem 64MB)에 같은 열과 인덱스의 order_items 사본 643만 행(힙 670MB, 전체 938MB, orders 471만 행 참조)을 만들고, 실행기의 `PG_QUEUE_REMOTE` 본문을 그대로 꺼내 가짜 kubectl(docker exec 로 바꿈)로 돌렸다(export_seconds 만 120 으로 줄임).

| 단계 | 결과 |
|---|---|
| preflight | 0(1.0초) |
| run | 0(1.9초). COPY 세션 ClientWrite 활성, ALTER 가 Lock/relation 으로 COPY 에 막힘 |
| checkout 흉내 | `BEGIN; INSERT orders; INSERT order_items; COMMIT` 세 개가 모두 Lock/relation 으로 ALTER 뒤에 섬(pg_blocking_pids = ALTER). 같은 때 orders 조회 1.6ms |
| 내보내기 길이 | 목표 120초가 약 144초(받는 쪽 속도 오차 약 2할). 끝나자 ALTER 가 지나가고 세 트랜잭션 모두 커밋, 대기 잠금 0 |
| cleanup, recovery | cleanup 2.2초(열 지움), recovery 0. 두 번째 run 뒤 1초 만의 이른 cleanup 은 64초(drain 60초를 기다린 뒤 내보내기를 끝내고 마이그레이션 취소), 열 없음 |
| 인덱스 생성 비교(1번 후보) | CREATE INDEX 12.3~32.8초 |

## 5. 원인 규정 (원칙 5, 6)

- 근본(`root_cause.target_id`): `commerce-postgres:order_schema.order_items` — 이 표에 걸린 잠금 대기열(안전한 열 추가 DDL 이 같은 표의 긴 읽기 뒤에서 기다림). 고칠 곳은 이 표에 대한 스키마 변경 절차(lock_timeout 없이, 긴 읽기와 겹쳐 돌린 것)다.
- 계기(`trigger_target_id`): 비움. 근본과 계기가 같은 곳(이 표의 스키마 변경과 긴 읽기의 겹침)이다(F66-R, F66-P 와 같은 규정).
- 부분 점수: `commerce-order`(증상을 낸 서비스, 재시작), `commerce-postgres`(인스턴스 단위로 본 답), `commerce-postgres:order_schema.orders`(같은 트랜잭션이 쓴 표).
- order, 게이트웨이, payment 의 코드, 설정, 요청량은 그대로라 정당하다. checkout 이 HTTP 호출을 트랜잭션 안에 두는 것은 피해를 키운 설계지만 정답이 아니라 피해 설명에 둔다(원칙 5).
- 원본 층위: Honeycomb 사후 보고가 지목한 것은 "teams 표의 마이그레이션이 느린 질의 뒤에서 잠금을 기다린 것"이다. 같은 층위(표의 잠금 대기열)로 지목한다.

## 6. 감지, 피해 판정, RCA 증거 (원칙 7)

| 층 | 무엇으로 | 기대 |
|---|---|---|
| 감지 | 게이트웨이 WARN 'Downstream unavailable for POST /api/orders/checkout' 급증, commerce-order Hikari 오류 로그, KCM testbed-order Unhealthy, APM order 오류율 | 평시 없던 오류 로그 패턴과 상태 변화로 이벤트, 인시던트 |
| 피해 판정 | 러너: 동반 부하 checkout 5xx 비율 ≥ 0.5, 2xx < 0.3 | 평시 0 근처, 장애 때 1 에 가깝다 |
| RCA | DPM 세션 사슬(order 세션 → ALTER → COPY), relNames order_items, 내보내기 queryTime | §7 |

## 7. 관측 근거 (119 실조회, 평시)

트레이스(10% 표본)와 APM 지표는 보조로만 쓴다. 핵심 증거는 DPM 세션 스냅숏(분마다), 전수 로그, KCM 이벤트다.

| 증거 | 표와 칸 | 조회 | 결과 |
|---|---|---|---|
| 근본: order_items 잠금 대기 사슬 | CH `lucida.dpm_session_local`(body 의 sessionType, waitType, waitEvent, blockingPid, relNames, queryTime) | `SELECT count() FROM lucida.dpm_session_local WHERE timestamp > now() - INTERVAL 60 DAY AND JSONExtractString(body,'relNames') LIKE '%order_items%'` | 0건. 같은 표의 30일 blocked/blocking 행 예(다른 실행): relNames inventory 의 blocking tuple 1,685, blocked relation 563 — 대기 행에 표 이름과 막는 pid 가 남는다는 확인. 최근 1일 PostgreSQL 세션 행 1,610(분마다 수집). 장애 때 order 파드 IP 세션의 blocked + relNames order_items(와 orders), 같은 blockingPid 의 ALTER(Lock/relation), 맨 앞 COPY(ClientWrite, queryTime 증가)가 처음 나타나야 한다. 첫 재시작(약 75~90초) 뒤에는 그 checkout 세션들이 다시 뜬 파드의 기동 세션을 막아 sessionType 'blocking'(blockingPid 는 여전히 ALTER)으로 바뀌고, 기동 세션이 relNames orders, Lock/relation, blockingPid = 멈춘 checkout 세션으로 그 뒤에 선다. 119 실데이터로 자신도 막힌 채 남을 막는 세션이 'blocking' 으로 남고 blockingPid 가 채워지는 것을 확인했다(30일 PostgreSQL blocking + waitType Lock + blockingPid 있음: relation 8, tuple 2,447, transactionid 179) |
| 계기: 마이그레이션과 긴 읽기 | 같은 표 | 같은 행의 blockingPid 를 따라감 | 두 세션은 program 'PostgreSQL JDBC Driver', DB 파드 안 연결이다. 세션 행에는 문장 원문이 없다(F66-R 시트와 같은 확인). DDL 과 COPY 는 한 번 도는 문장이라 Top SQL 에 닿지 않는다 |
| 전파: order 풀 고갈 | CH `lucida.lucida_logs_local` | 30일 `service_name LIKE 'commerce-%' AND body LIKE '%Connection is not available%'` 서비스별 | commerce-inventory 802(2026-10-06 다른 실행), commerce-user 661(2026-10-11 다른 실행), commerce-order 0. 장애 때 commerce-order 'HikariPool-1 - Connection is not available, request timed out after 30000ms' 가 처음 나타나야 한다 |
| 전파: 평시 order 로그 | 같은 표 | 14~30일 commerce-order 템플릿별 | 'Cleared cart after checkout' INFO(14일 사용자별 약 1만 4천~1만 7천), 'Recorded outbox event for order ... PAID' 29만, 다른 실행 때의 'Checkout payment failed ...'(결제 서킷) 뿐 |
| 전파: 게이트웨이 checkout 실패 | 같은 표 | 30일 `service_name='commerce-gateway'` 템플릿별 | 'Downstream unavailable for POST /api/orders/checkout: ... CallNotPermittedException ... order' 8,700, '... I/O error on POST' 380, 마지막 2026-10-06(다른 실행). 평시 0 |
| 전파: order 재시작 | CH `lucida.kcm_events_local`(reason, object_name) | 30일 `object_name LIKE 'testbed-order%'` reason 별 | Unhealthy 1,042(마지막 2026-10-09), ScalingReplicaSet 100, Killing 58(마지막 2026-10-08) — 다른 실행 때. 장애 때 롤아웃 없이 Unhealthy, Killing 이 생겨야 한다 |
| 감별: DB 는 한가 | CH `lucida.dpm_topsql_local`(sql_hash, body 의 calls, avgExecTime) | 1일 `body LIKE '%order_items%'` | 품목 INSERT(sql_hash 705941650521247677) 2,564호출, 평균 0.165ms. 장애 때 이 호출이 멎고 DB CPU 는 평시여야 한다(F20-R, F33-P 류 전수 스캔과 가르는 근거) |
| 참고: DB 설정 | 109 PostgreSQL 읽기 전용 조회 | `pg_settings`, `pg_stat_activity` 수, `pg_stat_user_tables` | lock_timeout 0, statement_timeout 0, max_connections 100, 세션 63, order_items 642만 행(2026-10-11) |

## 8. 감별

- must_support: DPM 세션의 order_items 잠금 대기 사슬(첫 스냅숏: order 세션 blocked → 같은 blockingPid 의 대기 세션(ALTER) → 오래 도는 ClientWrite 활성 세션. 첫 재시작 뒤: 그 checkout 세션들은 blocking(blockingPid ALTER)으로 바뀌고, 기동 세션이 relNames orders 에서 그 뒤에 섬), commerce-order Hikari 'Connection is not available'(30일 0줄), 게이트웨이 checkout WARN, KCM testbed-order Unhealthy, Killing(롤아웃 없음), checkout 5xx ≥ 0.5 와 2xx < 0.3, 상품 둘러보기 정상, PostgreSQL Ready 와 DB CPU 평시, payment 오류 없음, 내보내기 세션이 사라지는 분에 함께 회복.
- must_rule_out: 결제 로그 표 다시 쓰기(F67-R), order 스레드 풀이나 진입 폭주(F03-H, F07-H), order 배포나 프로브 오설정, 통계 질의 전수 스캔(F20-R), PostgreSQL 다운(F25-H).
- contrast_with: F67-R, F66-R, F66-P, F03-H, F25-H.
- 관계: F67-R 과 겉 증상(checkout 5xx, commerce PostgreSQL 표 잠금 대기로 풀 고갈과 재시작)이 같고 원인이 다르다(H). 판별력은 DPM 세션 사슬의 표 이름(order_items 대 payment_logs), 사슬 맨 앞 세션의 모양(ClientWrite 로 오래 도는 COPY 와 그 뒤에서 기다리는 DDL 대 잠금을 쥔 채 일하는 VACUUM FULL), 마르는 풀(order 대 payment), payment 호출의 유무에 있다.

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

- 강도는 하나로 고정(approved-fixed-f67-h): 내보내기 약 360초(export_seconds), cleanup 대기 상한 300초(drain_seconds), 시작 조건 행 1,000,000 이상, 열린 세션 70 이하(maximum_sessions). 대기열은 잠금 규칙으로 정해져 결정적이다: 내보내기가 도는 동안 order_items 문장은 하나도 끝나지 않는다.
- 부하: 동반 surge.js 10rps(ramp 30s, hold 12m, 여정의 50% 가 checkout → checkout 초당 약 5건, F67-R 과 같은 설정). 동반 k6 는 막 떠 토큰 캐시가 비어 login 부터 하지만 이 주입은 users, carts, 상품, pricing, inventory 를 건드리지 않아 로그인, 장바구니, 견적, 예약은 그대로 지나가고 checkout 만 order 안에서 멈춘다(F66-R 1차 평가 차단 지적의 '표본 0' 위험이 없다).
- 피해 계산: checkout 초당 약 5건이 각 품목 INSERT 에서 멈춘다. order 풀 10 은 약 2초 안에 찬다. 그 뒤 checkout 은 트랜잭션 시작에서 연결을 30초 기다린다. 게이트웨이 order read-timeout 15초, 재시도 1회(300ms)라 처음 checkout 은 약 15~30초 뒤 5xx 가 되고, 실패가 쌓여 order 서킷(최소 5, 50%)이 열리면 곧바로 5xx 다. order 의 health 는 DB 확인이 30초 기다리다 프로브 시간 제한(3초)에 걸려 readiness 가 약 30~40초에 빠지고(엔드포인트 제거, 게이트웨이 연결 거부로 곧바로 5xx), liveness(15초 x 5)가 약 75~90초에 죽인다. 다시 뜬 파드는 기동 schema.sql:14 의 CREATE INDEX IF NOT EXISTS idx_orders_user 가 멈춘 checkout 세션들이 쥔 orders ROW EXCLUSIVE 뒤에 서서 startupProbe 150초 안에 뜨지 못한다(줄이 풀리면 멈춘 세션들의 품목 INSERT 가 지나간 뒤 클라이언트가 없어 트랜잭션이 되돌려지고 orders 잠금이 풀린다). 그래서 checkout 5xx 비율은 1 에 가깝고 2xx 는 0 에 가깝다.
- 판정 시각: 첫 참 표본은 약 30~60초(15~30초 시간 초과 + 30초 창), 3표본째는 약 90~120초. 잠금 구간은 내보내기 시간(109 추정 360~430초, 로컬 오차 2할)이라 판정은 그 약 3할 지점이다. min_hold 1m 은 판정을 늦추지 않는다. timeout 6m 은 내보내기가 끝나는 시각이다. 성공 뒤 cleanup 이 내보내기가 스스로 끝나기를 기다리므로(drain 300초) 녹화에는 대기열 전체와 스스로 풀리는 모습이 담긴다.
- 연결 한도 계산: 줄 선 백엔드는 order 파드가 죽어도 잠금이 풀릴 때까지 남는다(F66-R 로컬 확인). 첫 파드의 10개, 재시작마다 기동 CREATE INDEX 1개(유휴 연결은 파드가 죽으면 닫힘), 6~7분 동안 재시작 약 2~3번 → 약 13개. 평시 63 + 주입 세션 2 + 13 ≈ 78 < 100. 시작 조건 70 이하가 여유를 지킨다.
- success: 동반 부하 checkout_5xx_rate ≥ 0.5 와 checkout 2xx 비율 < 0.3, 3틱. min_hold 1m, settle 30s, timeout 6m.
- must_rule_out: achieved_rps < 5(동반 10rps), 기준선 상품 목록 실패율 ≥ 0.2(상품은 다른 표, 게이트웨이 product 서킷은 order 와 따로라 이어져야 함. 로컬 대기열 중 다른 표 조회 1.6ms), commerce PostgreSQL 파드 NotReady(2틱).
- abort: entry_status == 0(2틱). 게이트웨이(30080)는 DB 를 쓰지 않고, checkout 스레드는 order 시간 초과(15초 x 2)까지만 묶이며 서킷이 열리면 곧바로 끝나 Tomcat 200 에 비해 적다.
- recovery: target_health 200, order Ready, PostgreSQL Ready(2틱, 10분). 동반 부하는 cleanup 에서 먼저 멈추므로 checkout 비율은 넣지 않는다(F67-R 과 같음).
- cleanup: 내보내기가 스스로 끝나기를 최대 300초 기다리고(넘으면 pg_terminate_backend, 남은 마이그레이션은 pg_cancel_backend), 두 세션이 없어진 뒤 `SET lock_timeout = '5s'; ALTER TABLE order_schema.order_items DROP COLUMN IF EXISTS discount_amount`(카탈로그만, 최대 5번), 파드 안 스크립트와 로그 파일 삭제. 행, 인덱스, 다른 표는 손대지 않는다. 내보내기 출력은 파드 안 awk 가 버린다. controller cleanup timeout 12m.
- 남는 영향: order 가 재시작될 때 멈춰 있던 checkout(최대 약 10건)이 예약한 재고는 order 가 풀어 주지 못해 재입고 배치까지 예약된 채 남는다(재고 상품마다 18~60). 피해 설명(user_impact)에 적었다.
- run 시간: 탐침 약 8번 + kubectl exec 3번 + 확인 대기, 로컬 1.9초. 러너 coordinator lease(30초) 안이다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Honeycomb 2024-08-06 공식 사후 보고, PostgreSQL 기전 GoCardless 공식 블로그)와 요소별 대응표, 기전 동일(§2)
- [x] 분류 장부 갱신(§4-1 행, §6 기록), 묶음 D(13→14, 97 중 14.4%), 정답 위치 DB 테이블(주문 품목)(0→1), 결제 경로 13(13.4%), 서비스 쇼핑몰 32→33(세 도메인 동률이라 정답 위치 0 부품), 고른 이유(§3). 어느 축도 20% 미만
- [x] 근본 원인 위치 `file:line`(checkout 트랜잭션, 품목 cascade, 풀, 기동 인덱스 schema.sql:14, 게이트웨이, 프로브)과 인프라 지점(testbed-postgres-0, 표 크기와 설정 실측)(§4)
- [x] 근본 원인 흔적 119 실조회: dpm_session_local 이 대기 행에 relNames 와 blockingPid 를 남김(다른 실행 행으로 확인), 60일 relNames order_items 0건(§7)
- [x] 핵심 증거가 표본에만 있지 않음: DPM 세션 스냅숏(분마다), 전수 로그, KCM 이벤트. 트레이스는 보조
- [x] 계기 흔적: 사슬 둘째 단의 대기 세션(마이그레이션)과 맨 앞의 오래 도는 읽기 세션(내보내기)이 세션 스냅숏에 남음. 인공 지연 없음(대기는 잠금 규칙, 내보내기 속도는 받는 쪽이 정하고 pg_sleep 없음)
- [x] 정답이 관제 데이터로 낼 수 있는 결론(order_items 표의 잠금 대기 사슬이 order 풀을 마르게 함), 코드 설계 결함 추론 불필요. HTTP 호출을 감싼 트랜잭션은 피해 설명
- [x] 정답지 세 칸이 원칙 6 대로(근본 order_items 표, 계기 같음, 부분 점수 commerce-order, commerce-postgres, orders)
- [x] 감지, 피해 판정, RCA 증거 구분(§6)
- [x] 주입 도구가 시나리오 id 를 남기지 않음: 실행기 인자는 네임스페이스, 파드, 스키마, 표, 열, 숫자, 열 목록뿐이고, 두 세션은 드라이버 기본 이름 'PostgreSQL JDBC Driver', 파드 안 파일 이름은 /tmp/event-export.sh, /tmp/event-export.log, /tmp/schema-migration.log, 탐침은 pg_stat_statements 에 남지 않음(테스트 `test_postgres_lock_queue_on_the_order_line_table_reuses_the_f66p_path`). 동반 부하 태그는 기존 시나리오와 같은 꼴(scenario_tag 는 k6 쪽 태그)
- [x] 부하 상한과 서킷브레이커를 고려한 피해 계산(§9: order 풀 약 2초에 고갈, 게이트웨이 15초 시간 초과와 order 서킷으로 checkout 5xx 가 1 에 가까움, 판정 시각 약 90~120초 대 잠금 구간 360~430초, 연결 한도 여유, 로컬 실측)

## 11. 평가 반영 기록

- 2026-10-11 1차 평가 FAIL(원칙 6): 다시 뜬 order 파드의 기동 정체 지점을 schema.sql:26(order_items, ALTER 뒤)으로 적었으나 실제로는 그보다 앞의 schema.sql:14 `CREATE INDEX IF NOT EXISTS idx_orders_user` 가 멈춘 checkout 세션들의 orders ROW EXCLUSIVE 뒤에 선다(평가자 로컬 postgres:17 실측). 그래서 첫 재시작 뒤 세션 사슬 모양이 바뀐다(checkout 세션 blocked → blocking, 기동 세션 relNames orders). §1, §4-4, §7, §8, §9, controller pod_ready 설명, 정답지 description, cause, mechanism, code_anchor, must_support, distinguishing_evidence, 실행 매트릭스를 맞췄다. 피해 계산, 강도, 결과(startupProbe 실패와 재시작)는 그대로다.
