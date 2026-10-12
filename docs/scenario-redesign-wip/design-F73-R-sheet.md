---
title: F73-R 설계 시트 (commerce 상품 표 열 추가 마이그레이션이 주문 품목에 상품을 붙여 읽는 느린 카테고리 매출 내보내기 뒤에서 잠금을 기다려 상품 읽기가 모두 줄을 섬)
status: Draft
owner: project
last_reviewed: 2026-10-12
tags:
  - scenario
  - design
  - database
summary: 카테고리별 매출을 분석 저장소로 옮기는 데이터 작업이 주문 품목(약 618만 행)에 상품의 카테고리, 이름, 가격을 붙여 읽는 느린 내보내기(COPY, 약 6분 활성)로 commerce PostgreSQL 의 작은 상품 표 product_schema.products 의 ACCESS SHARE 를 쥔 사이, 상품 팀이 같은 표에 브랜드 열을 더하는 마이그레이션(카탈로그만 바꾸는 안전한 변경)을 돌려 ACCESS EXCLUSIVE 를 기다리고, 그 뒤의 상품 읽기가 모두 줄을 서서 product-service 풀이 마르고 재시작하며 약 6~7분 동안 상품 둘러보기와 checkout 이 실패하는 시나리오. 원본 Honeycomb 2024-08-06.
---

# F73-R 설계 시트

## 1. 요약

카테고리별 매출을 분석 저장소로 옮기는 데이터 작업이 commerce PostgreSQL 에 느린 읽기 `COPY (SELECT m.id, m.category_id, m.name, m.price, i.order_id, i.quantity, i.unit_price, i.subtotal FROM product_schema.products m JOIN order_schema.order_items i ON i.product_id = m.id) TO STDOUT` 를 시작한다. 주문 품목 약 618만 행마다 상품의 카테고리, 이름, 가격을 붙여 받는 쪽이 일정한 속도로 받으므로 문장이 약 6분 동안 활성이고, 그동안 작은 상품 표 `product_schema.products`(2,016행, 상품 둘러보기와 checkout 마다 읽힘)의 ACCESS SHARE 잠금을 쥔다. 몇 초 뒤 상품 팀이 상품 표에 기본값 없는 null 허용 열 `brand varchar(100)` 을 더하는 마이그레이션을 돌린다. 카탈로그만 바꾸는 안전한 변경이지만 그 순간에 ACCESS EXCLUSIVE 잠금이 필요해 내보내기 뒤에서 기다리고, 기다리는 ACCESS EXCLUSIVE 는 모든 잠금과 충돌하므로 그 뒤의 상품 표 문장은 단순 읽기까지 모두 줄을 선다. 역할과 DB 에 lock_timeout, statement_timeout 이 없어 누구도 포기하지 않는다.

product-service 는 상품 목록, 상세, 검색, 변형 조회와 checkout 의 재고 예약 앞 상품 조회마다 상품 표를 읽는다. 요청이 연결을 쥔 채 줄에 서서 Hikari 풀(기본 10, 연결 대기 30초)이 1초 안에 묶이고, 뒤의 요청과 health 확인은 30초 뒤 연결 대기로 500 이다. 게이트웨이는 product 읽기 시간 제한(10초 x 3회) 끝에 product 서킷을 열어 상품 목록, 상세, 검색, 카테고리가 5xx 이고, order 의 재고 예약 호출도 시간 초과 끝에 서킷이 열려 checkout 이 결제 전에 502 로 끝난다. product 는 NotReady 와 재시작을 되풀이하고(다시 뜬 파드의 기동 스크립트도 같은 줄에 선다), 내보내기가 끝나면 마이그레이션이 지나가며 스스로 풀린다. 로그인, 장바구니, 견적, 다른 표는 정상이다.

비유: 진열대 안내판(products)에 "브랜드 칸 하나 추가, 잠깐이면 됨" 공사 팻말이 섰는데, 마침 누가 판매 장부를 넘기며 안내판을 계속 보고 있어(내보내기) 공사가 기다린다. 공사가 줄 맨 앞에 서 있어 안내판을 보려는 손님(상품 조회)이 모두 그 뒤에 서고, 안내원(product)은 손님을 붙잡은 채 손이 묶여 결국 자리를 비운다. 계산대(checkout)도 계산 전에 안내판을 확인해야 해서 같이 멈춘다.

## 2. 원본 사례

- **Honeycomb, 2024-08-06** (공식 상태 페이지 사고 "UI and API unavailable" 과 사후 보고, 2024-08-07 게시): https://status.honeycomb.io/incidents/ycgvhvs201q1
  - 12:59:39~13:20:05 PDT. 약 17분 동안 들어온 텔레메트리의 약 4분의 1 을 버리고 API 가 요청의 75% 를 거절(대부분 /1/auth 끝점), UI 는 적어도 19분 쓸 수 없었고 트리거 평가가 멈춤.
  - "a column addition to the `teams` table using an `INSTANT` algorithm" 으로 기술적으로 안전한 변경이었다. 마이그레이션 몇 초 앞서 같은 표를 읽는 질의가 시작됐고 그 느린 SELECT 는 일부 고객 조직에서 5분 넘게 걸릴 수 있었다. 마이그레이션이 잠금을 얻으려고 기다리는 동안("the `ALTER` statement itself waits for already running queries and transactions using this table to terminate") 인증을 포함한 그 표의 다른 질의를 막았다.
  - 탐지: 13:00 경 저하 인지, 13:04 경보. 배포 직후라 진단이 어려웠고 DB 성능과 스키마 마이그레이션의 상관을 좁히는 데 몇 분. 13:29 UI 와 API 회복, 14:00 해결. 후속: 느린 질의 점검, 클라이언트 시간 제한과 맞는 DB 쪽 시간 제한.
- **GoCardless** (공식 엔지니어링 블로그 "Zero-downtime Postgres migrations - the hard parts"): https://gocardless.com/blog/zero-downtime-postgres-migrations-the-hard-parts/ — 같은 기전의 PostgreSQL 설명. 얻지 못한 잠금은 대기열에 들어가고 그와 충돌하는 뒤 잠금이 모두 그 뒤에 줄을 서며, 예시가 "느린 SELECT 가 부모 표의 AccessShare 를 쥔 동안 ALTER TABLE 이 AccessExclusive 를 기다리고 그 뒤의 평범한 SELECT 가 멈춤" 이다(재구성의 '큰 표를 읽으며 작은 표를 붙잡는 읽기' 꼴).
- 둘 다 자료 문서 M21 에 이미 있다(F66-R 에서 추가). 이번에 더한 사실은 없다. 같은 반복에서 버린 후보의 원본 SchemeServe 2025-03-31 을 M17 에 더했다(§3 후보 목록).

### 요소별 대응표

| 요소 | 원본 사례 (Honeycomb 2024-08-06) | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 요청마다 읽히는 작은 teams 표에 열을 더하는 스키마 마이그레이션(INSTANT, 기술적으로 안전) | 둘러보기와 checkout 마다 읽히는 작은 상품 표에 기본값 없는 null 허용 열(brand varchar(100))을 더하는 마이그레이션(카탈로그만) |
| 원인이 된 결함 | 몇 초 앞서 시작된, 그 표를 읽는 느린 SELECT(5분 넘게)가 끝나기를 마이그레이션이 기다리며 잠금 요청을 대기열에 세움. DB 쪽 시간 제한 없음 | 몇 초 앞서 시작된, 주문 품목에 상품 표를 이어 읽는 느린 카테고리 매출 내보내기(COPY, 약 6분 활성)가 상품 표의 ACCESS SHARE 를 쥐고 마이그레이션의 ACCESS EXCLUSIVE 요청이 대기열에 섬. lock_timeout, statement_timeout 0 |
| 전파 경로 | 기다리는 잠금 요청 뒤로 그 표의 다른 질의(인증 포함)가 막힘 | 기다리는 ACCESS EXCLUSIVE 뒤로 상품 표 읽기가 모두 막힘 → product-service 요청이 연결을 쥔 채 멈춤 → product 풀 고갈 → 게이트웨이 product 시간 초과와 서킷, order 의 재고 예약 실패, product NotReady 와 재시작 |
| 사용자 증상 | API 요청 75% 거절, UI 사용 불가 | commerce 상품 목록, 상세, 검색, 카테고리 5xx, checkout 결제 전 5xx. 로그인, 장바구니는 정상 |
| 원본의 탐지 경로 | 저하 인지와 경보, DB 성능과 스키마 마이그레이션의 상관 | lucida-next 의 게이트웨이 오류 로그, commerce-product Hikari 오류 로그, DPM 세션 잠금 대기 사슬, KCM testbed-product Unhealthy |
| 완화와 복구 | 느린 질의가 끝나 마이그레이션이 지나가며 회복. 후속으로 DB 쪽 시간 제한 | 내보내기가 끝나 마이그레이션이 지나가며 스스로 회복. cleanup 이 더한 열을 지움 |

기전 일치: 원인("그 자체로는 안전한 열 추가가, 요청마다 읽히는 작은 표를 붙잡은 느린 읽기 뒤에서 잠금을 기다리며 대기열 맨 앞에 섬")에서 증상("그 표를 읽는 모든 요청 경로가 막혀 요청을 거절")까지 고리가 같다. 막히는 것이 원본처럼 쓰기가 아닌 **읽기**이고, 표가 원본처럼 작고 요청마다 읽히는 표라는 점이 F67-H(쓰기가 막힘), F66-P(outbox 쓰기)보다 원본에 가깝다.

바꾼 것(기전 고리 밖):
- 엔진: MySQL 메타데이터 잠금 → PostgreSQL 표 잠금. 두 엔진 모두 "기다리는 배타 요청이 뒤의 공유 요청을 막는" 대기열 규칙이 같다(GoCardless). 열 추가가 표를 다시 쓰지 않는 점(INSTANT ↔ 기본값 없는 null 허용 열)도 같다.
- 느린 읽기의 정체: 원본은 앱의 느린 SELECT(조직이 크면 5분 넘게). 재구성은 큰 주문 품목 표에 작은 상품 표를 붙여 분석 저장소로 옮기는 내보내기로, 받는 쪽이 일정 속도로 받아 약 6분 활성이다. 서버 쪽에서는 둘 다 작은 표의 공유 잠금을 쥔 채 오래 도는 읽기 문장이다. 주문 품목에는 상품 이름과 단가가 이미 있지만 카테고리가 없어(order_items 열 7개) 카테고리별 매출에는 상품 표를 붙이는 것이 자연스럽다. 618만 행을 웨어하우스로 옮기는 내보내기가 수 분 걸리는 것도 자연스럽다(F36-P 의 메뉴 판 근거와 같음).
- 길이: 원본 약 20분, 재구성 약 6~7분(대기 세션이 연결 한도를 넘지 않게, §9).

## 3. 숫자 근거 (2026-10-12, `scenario-stats.py`, 정식 52 + 후보 56 = 108, 이 후보 전)

- 묶음(정식 + 후보): J 17(15.7%), D 16(14.8%), P 13, G 12, A 9, L 8, B 7, C 6, E 4, F 3, K 3, O 3, H 2, I 2, Q 2, M 1, N 0. 금지(20%) 없음. 이 후보 D 16→17(109 중 15.6%).
- 정답 위치: 은행 이체 서비스 9(8.3%)가 가장 크고, **DB 테이블(상품) 1 → 2**(F23-P 다음, 1.8%). 결제 경로 합계 13(109 중 11.9%, 이 후보와 무관).
- 서비스: 쇼핑몰 36, 은행 36, 음식배달 36(세 도메인 동률). 2026-10-12 규칙(원칙 2: 가장 적은 도메인 하나, 동률이면 어느 것이든, 그 안의 정답 위치는 20% 상한만)대로 쇼핑몰에서 만든다. 쇼핑몰은 후보가 10 으로 셋 중 가장 적어(은행 21, 음식배달 25) 녹화 대기 중 쏠림도 가장 작다. 쇼핑몰 36 → 37.
- 부품 지도: 0 인 부품은 세 도메인 kafka, commerce, banking nginx 인데 막힌 목록의 벽(outbox 비동기라 사용자 증상 없음, nginx 는 119 관측 없음)에 걸린다. commerce 의 서비스와 표 가운데 정답 위치가 1 이하인 것(상품 서비스 1, DB 테이블(상품) 1, 장바구니 1, 가격 1, 게이트웨이 1 등)을 우선했다.
- 겉 증상(상품 둘러보기와 checkout 실패)은 F23-P(상품 표 지우기)와 같다. 2026-10-09 사용자 정정대로 증상 겹침은 버릴 사유가 아니라 판별력을 더한다: F23-P 는 표가 사라져 42P01 로 곧바로 실패하고 product 가 Ready 로 남으며, F73-R 은 표가 그대로인 채 잠금 대기 사슬에 서서 product 가 연결을 쥔 채 멈추고 풀이 말라 재시작한다.

### 같은 원본, 같은 수단의 다른 시나리오와의 관계 (카탈로그 §1)

같은 원본, 같은 db.ddl PostgreSQL 잠금 대기열 모드(F66-R, F66-P, F67-H)다. 파라미터가 다르고(표, 열, 이어 읽는 표, 내보내기 열 목록), 서비스 이름만 바꾼 복제가 아니다:
- 막히는 서비스와 문장: F66-R 은 user 의 회원 표 단순 읽기(게이트웨이 인가), F66-P 는 inventory 의 예약 트랜잭션 끝 outbox INSERT, F67-H 는 order 의 checkout 트랜잭션 중간 품목 INSERT, F73-R 은 product 의 모든 상품 표 읽기(둘러보기 전체와 checkout 의 재고 예약 앞)다.
- 진입 경로와 영향 범위(카탈로그 §1 의 R 조건): F66-R 은 인가된 쓰기와 로그인, F67-H 는 checkout 만 실패하고 둘러보기는 정상이다. F73-R 은 둘러보기(목록, 상세, 검색, 카테고리)와 checkout 이 함께 실패하고 로그인, 장바구니는 정상이다. 마르는 풀이 각각 user, inventory, order, product 다.
- F36-P(food MySQL 메뉴 표를 주문 품목과 이어 읽는 내보내기 뒤의 메타데이터 잠금)와는 엔진, 도메인, 진입 경로가 다르다.
- 사례군 번호: 같은 원인과 실행 모드의 사례군 F66 은 R, P 를 썼고, 같은 증상의 사례군 F23 의 H 는 지휘 세션이 다시 쓰지 말라고 한 폐기 id 라 새 사례군 F73 을 열었다. 관계는 정답지 related_scenarios 에 둔다.
- 관제 AI 가 배울 판단: 재시작하는 product 와 둘러보기 실패만 보면 product 서비스(상품 서비스)나 상품 표 지우기(F23-P)를 찍기 쉽다. DB 세션 사슬의 relNames 를 따라가 products 와 그 앞의 DDL, 내보내기에 닿아야 정답이다.

### 후보 목록 (3단계, "실제 기전 × 부품", 숫자 순)

| # | 후보 | 층, 부품(묶음, 정답 위치 합계) | 원본 사례 | 결과 |
|---|---|---|---|---|
| 1 | **commerce 상품 표 열 추가 마이그레이션이 주문 품목에 상품을 붙여 읽는 느린 카테고리 매출 내보내기 뒤에서 잠금 대기열** | DB, commerce products(D 16, DB 테이블(상품) 1) | Honeycomb 2024-08-06 공식, GoCardless 공식 | **채택** |
| 2 | 중복 인덱스 정리 마이그레이션이 inventory_movements 의 (product_id, created_at) 인덱스를 지워 재조정 배치의 상품별 최신 이력 조회가 전수 스캔, 재입고가 늦어 checkout 409 | DB, commerce inventory_movements(F 3, DB 테이블(재고 이동 이력) 1) | Buildkite 2025-11-10 공식, Chargebee 2018-03-02 공식 | 버림: 원칙 9. 로컬 postgres:16-alpine 0.5 CPU 같은 크기 사본(892만 행, 힙 581MB, 109 647MB)에서 인덱스가 없으면 조회 하나가 병렬 전수 스캔 약 2.5초(인덱스 0.7ms)라 16개 상품 배치가 약 45초로 늘 뿐이고, 1분 지연 배치 주기가 약 105초가 된다. 인덱스가 있을 때 409 가 없을 수요(상품당 분당 20개 미만)에서 재고 소진 시간 비율이 상품마다 약 1~2할(checkout 409 약 1~2.5할)에 그치고, 단일 스캔 흐름이 PG CPU 를 다른 질의 지연 수십 ms 로만 늘린다(로컬 실측). 남는 피해가 4xx 일부뿐 |
| 3 | food dispatch 롤아웃이 JVM 시간대를 KST 로 바꿔 assigned_at(JVM) 과 만료 질의의 MySQL NOW()(UTC)가 9시간 어긋나 배차가 만료되지 않고 배차 한도 소진, 주문 503 | 앱 설정, food dispatch(새 묶음 '시간 어긋남' 0, 배달 서비스 7) | SchemeServe 2025-03-31 공식(발급 쪽 BST, 검증 쪽 UTC 로 토큰이 미래 발급처럼 보여 인증 실패, 자료 문서 M17 에 이번에 추가), Piano 2026-04-06(컨테이너 이전이 JVM 기본 시간대를 잃어 날짜 칸이 어긋남, PDF 원문 미확보) | 버림: 원칙 9. 새로 쓴 배차만 어긋나고 옛 배차는 평시대로 만료되므로 차이는 ETA(15~34분) 뒤에야 생기고, 한도(2000) 도달이 기준선 주문률(초당 0.37~0.8)에 따라 30~60분으로 흔들린다. 당기려고 동반 부하를 늘리면 고장 없이도 한도에 닿고(초당 1.28건 넘으면 정상 상태 배정이 2000 초과), cleanup 뒤에도 미래 시각 배차가 남는다(막힌 목록의 food 용량 벽) |
| 4 | 데이터 이전이 시퀀스 상태를 옮기지 않아 commerce order_items 또는 payment_logs 기본 키 충돌 | DB, commerce order_items/payment_logs(Q 2) | RevenueCat 2022-11-23 공식 | 버림: 카탈로그 §1. F57-P 가 같은 commerce DB 이전 이야기(읽기 전용 전환, 세션 끊기, 시퀀스 되감기)를 같은 실행 모드로 이미 썼고 표만 다르다 |
| 5 | 비온라인 CREATE INDEX 마이그레이션이 commerce payment_logs(1,868만 행) 쓰기를 막음 | DB, commerce payment_logs(D, DB 테이블(결제 로그) 1) | Harness 2026-01-26 공식 | 버림: 카탈로그 §1. F67-R 이 같은 표를 표 잠금으로 막아 결제 단계에서 멈추는 꼴을 이미 썼다(막힌 목록 315행의 균형 사유는 풀렸지만 이 벽이 남음) |
| 6 | 용량 이동 중 commerce Redis 연결 한도 | 캐시(B, 캐시 1) | GitHub 2026-07-25 | 버림: 원칙 9. cart 의 Redis 는 연결 하나를 계속 쓰고, 끊겨도 서킷과 DB 폴백(carts 20행)이 받아 사용자 피해가 없다(막힌 목록 46, 197행 벽) |
| 7 | commerce PostgreSQL 앱 역할 CONNECTION LIMIT 하향 | DB 계정(O 3, DB 계정 4) | Google 2020-12-14 공식 | 버림: 카탈로그 §1. F71-R(같은 역할의 비밀번호 기한)과 같은 '새 로그인만 거절, 풀이 수명대로 빔' 꼴 |
| 8 | commerce product 릴리스가 요청마다 스레드 풀을 만들고 닫지 않아 OOM 재시작 | 앱, commerce product(J 17, 상품 서비스 1) | Intercom 2025-04-24 공식 | 버림: 카탈로그 §1. F63-R, F63-P 와 같은 패치 꼴을 서비스만 바꿔 쓴다 |
| 9 | commerce payment 롤아웃이 은행 이체 주소를 다른 네임스페이스의 짧은 서비스 이름으로 넣어 이름 해석 실패 | 앱 설정, DNS(M 1, 결제 서비스 4) | Intercom 2024-11-05 공식 | 버림: 원칙 1, 카탈로그 §1. 원본은 resolver 교체가 사설 레코드를 못 푼 것이라 설정값 오기와 고리가 다르고, 잘못된 하류 주소 설정 배포는 F39-R 꼴 |
| 10 | 게이트웨이 재시도 설정 확대로 재시도가 장애를 키움 | 게이트웨이(N 0) | Clerk 2025-02-06 공식(재시도 과다는 피해 확대 요인) | 버림: 원칙 1, 9. 재시도가 늘려면 먼저 실패가 있어야 해 계기가 둘이고, 층마다 서킷이 재시도를 끊는다(막힌 목록 45행 벽) |

부품: commerce products, inventory_movements, food dispatch, order_items/payment_logs, Redis, DB 역할, product, payment, gateway(9종). 앱, 설정, DB, 캐시, 계정, DNS 층.

## 4. 인과 사슬과 코드, 인프라 위치

1. 주입: db.ddl 실행기(`scripts/scenarios/profiles/db_ddl_executor.py`, 계약 `CONTRACTS["F73-R"]`, 원격 본문 `PG_QUEUE_REMOTE` 의 새 `export_columns` + 이어 읽는 표 분기)가 러너에서 kubectl exec 로 testbed-postgres-0 안의 psql 을 띄운다. 내보내기 `COPY (SELECT m.id, m.category_id, m.name, m.price, i.order_id, i.quantity, i.unit_price, i.subtotal FROM product_schema.products m JOIN order_schema.order_items i ON i.product_id = m.id) TO STDOUT` 를 order_items 행 추정/360 행마다 1초 쉬는 awk 에 흘리고, 그 세션이 products 잠금을 쥔 것을 본 뒤 `ALTER TABLE product_schema.products ADD COLUMN brand varchar(100)` 를 띄운다. 서버가 ALTER 를 내보내기 뒤에서 기다리는 것(pg_blocking_pids)이 보이면 run 이 끝난다. 이어 읽는 표 인자는 F36-P 의 MySQL 판과 같은 이름(export_join_table, export_join_column)이고, PostgreSQL 에서는 다른 스키마의 표라 스키마까지 적는다. 이 인자가 없는 F66-R, F66-P, F67-H 의 argv 와 동작은 그대로다(테스트).
2. 상품 읽기: `commerce/product-service/src/main/java/com/commerce/product/service/ProductService.java:42-104` — getProducts(42, 목록, 카테고리, 이름 검색), getVariants(56, findById 로 존재 확인 뒤 변형), getCategories(63), getProductWithStock(75, 상세), reserveStock(89, checkout 재고 예약 앞 findById), findById(101). Top SQL 의 상품 단건 조회는 `select p1_0.id,c1_0.id,... from product_schema.products p1_0 left join product_schema.categories c1_0 ... where p1_0.id=$1`(2026-10-12 KST 21~22시 한 시간 33,913회), 목록, 건수, 카테고리 건수, 검색 건수 문장을 더해 products 문장 약 63,700회/시간(분당 약 1,060).
3. product 풀과 기동: `commerce/product-service/src/main/resources/application.yml:7-10` hikari 설정 없음(기본 최대 10, 연결 대기 30초). `:18-22` spring.sql.init.mode always, schema.sql 과 data.sql. 기동 `schema.sql:21` `CREATE INDEX IF NOT EXISTS idx_products_category ON product_schema.products(category_id)` 가 products 의 SHARE 잠금을 먼저 얻으려 해 같은 줄에 선다(로컬 실측). `data.sql:8-25` 상품 INSERT ... ON CONFLICT (id) DO NOTHING 도 products 를 쓴다.
4. 게이트웨이: `commerce/api-gateway/src/main/resources/application.yml:38-41`(product read-timeout 10s), `86-92`(product 서킷: 창 10, 최소 5, 실패율 50%, 열림 5초), `127-131`(product 재시도 3회, 200ms 지수). `ProxyService.java:92` forwardToProduct(/api/products, /api/categories 공용), `:104` 'Downstream unavailable for ...' WARN. 하류의 5xx 응답은 예외가 아니라 그대로 돌려주고(`doForward`), 읽기 시간 초과만 재시도와 서킷에 잡힌다.
5. order: `commerce/order-service/src/main/java/com/commerce/order/service/OrderService.java:160` checkout 의 품목별 productClient.reserveStock(결제 전). `ProductClient.java:25-55` @CircuitBreaker productClient, @Retry, fallback 502 'Product service unavailable for product N'. `order-service/.../application.yml:47-50` product read-timeout 10s.
6. 프로브: `commerce/k8s/21-product-service.yaml:79-98` startupProbe 5초 x 30, readinessProbe 10초 x 3(시간 제한 3초), livenessProbe 15초 x 5, 모두 `/actuator/health`(DB 확인 포함).
7. DB: `commerce/k8s/10-postgres.yaml` testbed-postgres(PostgreSQL 16.14 alpine, cpu 500m, 512Mi, tb-w1). 109 읽기 전용 실측(2026-10-12): product_schema.products 2,016행(432kB), brand 열 없음, order_schema.order_items 행 추정 6,180,524(힙 622MB, 전체 918MB), 열린 세션 62, max_connections 100, lock_timeout 0, statement_timeout 0, pg_db_role_setting 비어 있음. products 를 외래 키로 가리키는 표는 product_variants 뿐이고 order_items.product_id 에는 외래 키가 없다(init-schemas.sql:51 product_variants 의 외래 키, :233-241 order_items). 그래서 내보내기와 주문 품목 INSERT 는 서로 막지 않는다.

### 로컬 실측 (2026-10-12, 원칙 9 근거)

postgres:16-alpine 컨테이너(`--cpus 0.5 --memory 512m`, shared_buffers 128MB)에 같은 열과 인덱스의 product_schema(categories 4, products 2,016, product_variants 6,048)와 order_items 사본 618만 행(498MB)을 만들고, main 을 빌드한 product-service jar(같은 application.yml, 기본 Hikari)를 그 DB 에 붙였다. 실행기의 `PG_QUEUE_REMOTE` 본문을 그대로 꺼내 가짜 kubectl(docker exec 로 바꿈)로 돌렸다(export_seconds 만 120, 150 으로 줄임). 부하는 상품 목록, 검색, 카테고리, 변형 조회를 초당 약 10건(curl 40초 시간 제한)으로 보냈다.

| 단계 | 결과 |
|---|---|
| preflight | 0(1.2초) |
| run | 0(2.5초). COPY 세션 ClientWrite 활성, ALTER 가 Lock/relation 으로 COPY 에 막힘, product 세션 10개가 모두 Lock/relation 으로 ALTER 뒤에 섬(pg_blocking_pids = ALTER) |
| product | 로그 'HikariPool-1 - Connection is not available, request timed out after 30001ms (total=10, active=10, idle=0, waiting=189)', /actuator/health 3초 시간 초과가 내보내기 내내(000). 20초 구간별 결과: 주입 전 200 195건, 주입 뒤 0~20초 시작분 500 164·시간 초과 10·200 21(주입 직전 시작), 20~100초 시작분 모두 500 또는 40초 시간 초과, 100~120초 시작분부터 200 |
| 내보내기 길이 | 목표 120초가 약 148초, 150초가 약 184초(받는 쪽 속도 오차 약 2할 3푼). 끝나자 ALTER 가 지나가고(brand 열 생김) 대기 잠금 0, product 요청 곧 200 |
| 재시작 | 줄 중에 product 를 죽이고 다시 띄움: 죽은 파드 세션 10개가 Lock/relation 그대로 남고, 새 파드는 Hikari 연결 9개를 연 뒤 기동 schema.sql 의 CREATE INDEX IF NOT EXISTS idx_products_category 에서 Lock/relation 으로 멈춤. 내보내기가 끝나자 3초 뒤 'Started ProductServiceApplication', 목록 200 |
| cleanup, recovery | cleanup 2.0초(열 지움, 파드 안 파일 삭제), recovery 0. 두 번째 실행 뒤 cleanup 도 같음 |

109 와의 차이: 109 는 aarch64 alpine(busybox awk, F66-R 에서 `awk ... system()` 확인), product 는 쿠버네티스 프로브로 재시작된다(로컬은 손으로). 대기열은 잠금 규칙으로만 정해져 CPU 속도와 무관하다. 내보내기 길이는 받는 쪽 속도가 정해 109 에서도 목표의 약 1.2배(360초 → 약 440초)로 본다(F67-H 로컬 1.2배와 같음).

## 5. 원인 규정 (원칙 5, 6)

- 근본(`root_cause.target_id`): `commerce-postgres:product_schema.products` — 이 표에 걸린 잠금 대기열(안전한 열 추가 DDL 이 이 표를 붙잡은 긴 읽기 뒤에서 기다림). 고칠 곳은 이 표에 대한 스키마 변경 절차(lock_timeout 없이, 긴 읽기와 겹쳐 돌린 것)다.
- 계기(`trigger_target_id`): 비움. 근본과 계기가 같은 곳(이 표의 스키마 변경과 이 표를 붙잡는 긴 읽기의 겹침)이다(F66-R, F66-P, F67-H 와 같은 규정).
- 부분 점수: `commerce-product`(증상을 낸 서비스, 재시작), `commerce-postgres`(인스턴스 단위로 본 답).
- 내보내기가 이어 읽은 order_items 는 정답이 아니다: 그 표에는 줄이 서지 않고 주문 품목 INSERT 는 평시대로 돈다. 부분 점수에도 넣지 않는다.
- product, 게이트웨이, order 의 코드, 설정, 요청량은 그대로라 정당하다. product 의 health 가 DB 확인을 포함해 재시작을 부르는 것은 피해를 키운 설정이지만 정답이 아니라 피해 설명에 둔다(원칙 5).
- 원본 층위: Honeycomb 사후 보고가 지목한 것은 "teams 표의 마이그레이션이 느린 질의 뒤에서 잠금을 기다린 것"이다. 같은 층위(표의 잠금 대기열)로 지목한다.

## 6. 감지, 피해 판정, RCA 증거 (원칙 7)

| 층 | 무엇으로 | 기대 |
|---|---|---|
| 감지 | 게이트웨이 WARN 'Downstream unavailable for GET /api/products ...' 급증(상품 상세, 카테고리, checkout 포함), commerce-product Hikari 오류 로그와 Servlet 오류, KCM testbed-product Unhealthy, Killing, APM product 지연과 게이트웨이 오류율 | 평시 없던 오류 로그 패턴과 상태 변화로 이벤트, 인시던트 |
| 피해 판정 | 러너: 기준선 상품 목록 실패율 ≥ 0.5, 동반 부하 checkout 5xx ≥ 0.5, 2xx < 0.3 | 평시 0 근처, 장애 때 1 에 가깝다 |
| RCA | DPM 세션 사슬(product 세션 → ALTER → COPY), relNames products, 내보내기 queryTime | §7 |

## 7. 관측 근거 (119 실조회, 평시)

트레이스(10% 표본)와 APM 지표는 보조로만 쓴다. 핵심 증거는 DPM 세션 스냅숏(분마다), 전수 로그, KCM 이벤트다.

| 증거 | 표와 칸 | 조회 | 결과 |
|---|---|---|---|
| 근본: products 잠금 대기 사슬 | CH `lucida.dpm_session_local`(body 의 sessionType, waitType, waitEvent, blockingPid, relNames, queryTime, program) | `SELECT count() FROM lucida.dpm_session_local WHERE engine='postgresql' AND timestamp > now() - INTERVAL 60 DAY AND JSONExtractString(body,'relNames') LIKE '%products%'` | 0건(수집은 2026-08-13 부터 103,492행, 최근 1일 1,675행으로 분마다 수집 중). 다른 실행의 blocked 행 예: `relNames 'inventory, inventory_pkey, inventory_product_id_key', sessionType blocked, waitEvent tuple, blockingPid 481548, program 'PostgreSQL JDBC Driver'` — 대기 행에 표 이름과 막는 pid 가 남는다는 확인. 장애 때 product 파드 IP 세션의 blocked + relNames products 와 그 사슬이 처음 나타나야 한다 |
| 계기: 마이그레이션과 긴 읽기 | 같은 표 | 같은 행의 blockingPid 를 따라감 | 사슬 둘째 단(ALTER, Lock/relation, relNames products)과 맨 앞(내보내기, Client/ClientWrite, queryTime 이 분마다 늘어 약 360~440초, relNames products, order_items). 두 세션은 program 'PostgreSQL JDBC Driver', DB 파드 안 연결이다. 세션 행에는 문장 원문이 없고, DDL 과 COPY 는 한 번 도는 문장이라 Top SQL 에 닿지 않는다(F23-P, F66-R 시트와 같은 확인) |
| 전파: product 풀 고갈 | CH `lucida.lucida_logs_local` | 30일 `service_name LIKE 'commerce-%' AND body LIKE '%Connection is not available%'` 서비스별 | commerce-inventory 802, commerce-user 661, commerce-cart 579(모두 다른 실행), commerce-product 0. 장애 때 commerce-product 'HikariPool-1 - Connection is not available, request timed out after 30000ms' 가 처음 나타나야 한다(로컬 실측 문장) |
| 전파: 평시 product 로그 | 같은 표 | 30일 `service_name='commerce-product'` 템플릿별 | SEVERE 'Servlet.service() ... threw exception' 9,394(마지막 2026-10-06, 다른 실행) 뿐. 평시에는 product 가 로그를 거의 남기지 않는다 |
| 전파: 게이트웨이 둘러보기 실패 | 같은 표 | 30일 `service_name='commerce-gateway'` 에서 'Downstream unavailable for GET /api/products%' 또는 '/api/categories%' | 12,789(마지막 2026-10-11 20:32, 다른 실행). 평시 0. 장애 때 읽기 시간 초과(ResourceAccessException) 뒤 서킷 'product' 의 CallNotPermittedException 이 이어져야 한다 |
| 전파: product 재시작 | CH `lucida.kcm_events_local`(reason, object_name) | 30일 `object_name LIKE '%product%'` | 0건(testbed-product 는 30일 이벤트 없음). 장애 때 롤아웃 없이 Unhealthy, Killing 이 생겨야 한다 |
| 감별: DB 는 한가 | CH `lucida.dpm_topsql_local`(log_attributes sql_id, body 의 calls, avgExecTime) | 최근 1시간 `sqlText ILIKE '%product_schema.products%'` | 상품 단건 조회 33,913, 건수 8,097, 목록 7,697, 카테고리 건수 7,690, 검색 건수 3,149 등 약 63,700호출. 장애 때 이 호출들이 멎고 다른 표(users, carts, inventory, orders) 문장은 이어지며 DB CPU 는 평시여야 한다(F33-P, F20-R 류 포화와 가르는 근거) |
| 참고: DB 설정 | 109 PostgreSQL 읽기 전용 조회 | `SHOW lock_timeout`, `statement_timeout`, `max_connections`, pg_stat_activity 수, pg_db_role_setting | 0, 0, 100, 62, 설정 없음(2026-10-12) |

## 8. 감별

- must_support: DPM 세션의 products 잠금 대기 사슬(product 세션 blocked → 같은 blockingPid 의 대기 세션(ALTER) → 오래 도는 ClientWrite 활성 세션, relNames products 와 order_items), 첫 재시작 뒤 새 파드 기동 세션도 같은 줄, commerce-product Hikari 'Connection is not available'(30일 0줄), 게이트웨이 'Downstream unavailable for GET /api/products ...', KCM testbed-product Unhealthy, Killing(롤아웃 없음), 기준선 상품 목록 실패율 ≥ 0.5 와 checkout 5xx ≥ 0.5, 2xx < 0.3, 로그인과 장바구니 정상, PostgreSQL Ready 와 DB CPU 평시, 다른 표 문장 지속, 내보내기 세션이 사라지는 분에 함께 회복.
- must_rule_out: 상품 표 지우기(F23-P), product CPU 한도나 장애(F12-H), 다른 표의 잠금 대기열(F66-R, F66-P, F67-H), PostgreSQL 다운(F25-H), 무거운 질의나 인덱스 손실로 DB 포화(F33-P, F20-R).
- contrast_with: F23-P, F66-R, F36-P, F67-H, F12-H, F25-H.
- 가르는 관측 근거 한 줄: 상품 둘러보기와 checkout 이 함께 실패하는데 products 표는 그대로(42P01 없음)이고, DPM 세션 스냅숏에 product 파드 세션들이 relNames products 의 relation 잠금으로 한 대기 세션(ALTER) 뒤에 서 있으며 그 앞에 queryTime 이 계속 느는 ClientWrite 읽기 하나가 있고, DB CPU 는 평시다.

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

- 강도는 하나로 고정(approved-fixed-f73-r): 내보내기 약 360초(export_seconds), cleanup 대기 상한 300초(drain_seconds), 시작 조건 products 행 1,000 이상과 order_items 행 36,000 이상, 열린 세션 70 이하(maximum_sessions). 대기열은 잠금 규칙으로 정해져 결정적이다: 내보내기가 도는 동안 products 문장은 하나도 끝나지 않는다.
- 부하: 상주 기준선 loadgen-commerce(하루 주기 2~10 iter/s, 둘러보기 여정이 상품 목록을 읽음)가 상품 목록 표본을 내고, 동반 surge.js 3rps(ramp 30s, hold 12m, 여정의 절반이 checkout → checkout 초당 약 1.5건, F23-P, F33-P 와 같은 강도)가 checkout 표본을 낸다. 동반 k6 는 막 떠 로그인부터 하지만 이 주입은 users, carts, pricing, inventory 를 건드리지 않아 로그인, 장바구니, 견적은 지나가고 checkout 이 재고 예약 앞 상품 조회에서 멈춘다.
- 피해 계산: products 문장이 평시 분당 약 1,060(초당 약 18)이라 product 풀 10 은 주입 뒤 1초 안에 찬다. 그 뒤 요청은 30초 연결 대기 끝에 500 이지만, 게이트웨이 product 읽기 시간 제한이 10초라 둘러보기는 10초 x 3회 시도 끝에 5xx 이고 시간 초과가 5번 쌓이면 product 서킷(최소 5, 50%)이 열려 곧바로 5xx 다(반열림 3회도 다시 시간 초과). checkout 은 order 의 productClient 가 10초 시간 초과와 재시도(200, 400ms) 끝에 서킷이 열려 502, 게이트웨이 order 읽기 시간 제한 15초가 먼저 끝나면 게이트웨이 5xx 다. product 의 health 는 DB 확인이 30초 기다리다 프로브 시간 제한(3초)에 걸려 readiness 가 약 30초에 빠지고(엔드포인트 제거, 게이트웨이 연결 거부로 곧바로 5xx) liveness 가 약 75초에 재시작한다. 다시 뜬 파드는 기동 CREATE INDEX IF NOT EXISTS 에서 멈춰 startupProbe(150초)에 다시 시작된다. 기준선 상품 목록과 동반 checkout 이 모두 1 에 가깝게 실패한다(로컬: 초당 10건 상품 읽기 전부 실패).
- 판정 시각: 첫 참 표본은 약 30~60초(10~30초 시간 초과 + 30초 창), 3표본째는 약 90~120초. 잠금 구간은 내보내기 시간(109 추정 360~440초)이라 판정은 그 약 3할 지점이다. min_hold 1m 은 판정을 늦추지 않는다. timeout 6m 은 내보내기가 끝나는 시각이다. 성공 뒤 cleanup 이 내보내기가 스스로 끝나기를 기다리므로(drain 300초) 녹화에는 대기열 전체와 스스로 풀리는 모습이 담긴다.
- 기준선 반복률: 둘러보기 요청이 한동안 10~30초씩 매달려 기준선 k6 VU(최대 50)가 일부 묶일 수 있지만, product 서킷이 열리면 곧바로 실패해 반복률이 회복된다. 그래서 '부하 미전달' 배제는 동반 부하의 achieved_rps < 1(3rps 의 1/3)로만 둔다.
- 연결 한도 계산: 줄 선 백엔드는 product 파드가 죽어도 잠금이 풀릴 때까지 남는다(로컬 확인). 첫 파드의 10개, 재시작마다 기동 CREATE INDEX 1개(새 파드가 연 나머지 유휴 연결은 파드가 죽으면 닫힘), 6~7분 동안 재시작 약 2~3번(75초 liveness + 기동 + 150초 startup) → 약 13개. 평시 62 + 주입 세션 2 + 13 ≈ 77 < 100. 시작 조건 70 이하가 여유를 지킨다.
- success: 동반 부하 checkout_5xx_rate ≥ 0.5, checkout 2xx 비율 < 0.3, 기준선 상품 목록 실패율 ≥ 0.5, 3틱. min_hold 1m, settle 30s, timeout 6m.
- must_rule_out: achieved_rps < 1(동반 3rps), commerce PostgreSQL 파드 NotReady(2틱). product 의 NotReady 는 이 시나리오의 증상이라 배제에 넣지 않는다.
- abort: entry_status == 0(2틱). 게이트웨이(30080)는 DB 를 쓰지 않고, 상품 요청 스레드는 시간 초과(10초 x 3)까지만 묶이며 서킷이 열리면 곧바로 끝나 Tomcat 200 에 비해 적다.
- recovery: target_health 200, product Ready, PostgreSQL Ready, 기준선 상품 목록 실패율 < 0.1(2틱, 10분). 동반 부하는 cleanup 에서 먼저 멈추므로 checkout 비율은 넣지 않는다(F67-H 와 같음).
- cleanup: 내보내기가 스스로 끝나기를 최대 300초 기다리고(넘으면 pg_terminate_backend, 남은 마이그레이션은 pg_cancel_backend), 두 세션이 없어진 뒤 `SET lock_timeout = '5s'; ALTER TABLE product_schema.products DROP COLUMN IF EXISTS brand`(카탈로그만, 최대 5번), 파드 안 스크립트와 로그 파일(/tmp/sales-export.sh, /tmp/sales-export.log, /tmp/schema-migration.log) 삭제. 행, 인덱스, 다른 표는 손대지 않는다. 내보내기 출력은 파드 안 awk 가 버린다. controller cleanup timeout 12m.
- 남는 영향: 실패한 checkout 은 재고 예약 단계에서 끝나 주문, 결제, 정산 이체를 남기지 않는다. product 의 재고 예약은 inventory 를 부르기 전에 상품 조회(findById)에서 멈추므로 예약된 재고도 남지 않는다.
- run 시간: 탐침 약 9번 + kubectl exec 3번 + 확인 대기, 로컬 2.5초. 러너 coordinator lease(30초) 안이다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Honeycomb 2024-08-06 공식 사후 보고, PostgreSQL 기전 GoCardless 공식 블로그)와 요소별 대응표, 기전 동일(§2)
- [x] 분류 장부 갱신(§4-1 행, §6 기록), 묶음 D(16→17, 109 중 15.6%), 정답 위치 DB 테이블(상품)(1→2), 결제 경로 13(11.9%), 서비스 쇼핑몰 36→37(세 도메인 동률, 2026-10-12 규칙), 고른 이유(§3). 어느 축도 20% 미만
- [x] 근본 원인 위치 `file:line`(상품 읽기 ProductService.java:42-104, 풀과 기동 application.yml, schema.sql:21, 게이트웨이, order ProductClient, 프로브)과 인프라 지점(testbed-postgres-0, 표 크기와 설정 실측)(§4)
- [x] 근본 원인 흔적 119 실조회: dpm_session_local 이 대기 행에 relNames 와 blockingPid 를 남김(다른 실행 행으로 확인), 60일 relNames products 0건(§7)
- [x] 핵심 증거가 표본에만 있지 않음: DPM 세션 스냅숏(분마다), 전수 로그, KCM 이벤트. 트레이스는 보조
- [x] 계기 흔적: 사슬 둘째 단의 대기 세션(마이그레이션)과 맨 앞의 오래 도는 읽기 세션(내보내기)이 세션 스냅숏에 남음. 인공 지연 없음(대기는 잠금 규칙, 내보내기 속도는 받는 쪽이 정하고 pg_sleep 없음)
- [x] 정답이 관제 데이터로 낼 수 있는 결론(products 표의 잠금 대기 사슬이 product 풀을 마르게 함), 코드 설계 결함 추론 불필요. DB 확인을 포함한 health 는 피해 설명
- [x] 정답지 세 칸이 원칙 6 대로(근본 products 표, 계기 같음, 부분 점수 commerce-product, commerce-postgres)
- [x] 감지, 피해 판정, RCA 증거 구분(§6)
- [x] 주입 도구가 시나리오 id 를 남기지 않음: 실행기 인자는 네임스페이스, 파드, 스키마, 표, 열, 이어 읽는 표, 숫자, 열 목록뿐이고, 두 세션은 드라이버 기본 이름 'PostgreSQL JDBC Driver', 파드 안 파일 이름은 /tmp/sales-export.sh, /tmp/sales-export.log, /tmp/schema-migration.log, 탐침은 pg_stat_statements 에 남지 않음(테스트 `test_postgres_lock_queue_on_the_small_products_table_behind_a_joined_sales_export`). 동반 부하 태그는 기존 시나리오와 같은 꼴(scenario_tag 는 k6 쪽 태그)
- [x] 부하 상한과 서킷브레이커를 고려한 피해 계산(§9: product 풀 1초 안에 고갈, 게이트웨이와 order 의 product 서킷으로 둘러보기와 checkout 이 1 에 가깝게 실패, 판정 시각 약 90~120초 대 잠금 구간 360~440초, 연결 한도 여유, 로컬 실측)

## 11. 평가 반영 기록

(없음)
