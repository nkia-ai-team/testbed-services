---
title: F66-P 설계 시트 (commerce 재고 이벤트 outbox 표 열 추가 마이그레이션이 느린 이벤트 내보내기 뒤에서 잠금을 기다려 재고 예약이 재고 행 잠금과 연결을 쥔 채 멈춤)
status: Draft
owner: project
last_reviewed: 2026-10-11
tags:
  - scenario
  - design
  - database
summary: 운영자가 commerce PostgreSQL inventory_schema.outbox_events(재고 이벤트 outbox, 약 7만 2천 행)에 추적 id 열을 더하는 마이그레이션(카탈로그만 바꾸는 안전한 변경)을 돌렸는데, 몇 초 앞서 데이터 웨어하우스 적재용 이벤트 내보내기(같은 표의 COPY, 약 7분 활성)가 ACCESS SHARE 를 쥐고 있어 마이그레이션이 ACCESS EXCLUSIVE 를 기다리고, 그 뒤의 outbox 쓰기가 모두 줄을 서는 시나리오. 재고 예약과 해제는 재고 행을 FOR UPDATE 로 잡고 이동 이력을 쓴 뒤 같은 트랜잭션에서 outbox 에 INSERT 하므로, 그 INSERT 가 재고 행 잠금과 inventory 연결을 쥔 채 멈춘다. inventory 풀(10)이 15초 안에 차서 재고 조회까지 막히고, 상품 상세와 checkout 이 실패하며 게이트웨이의 product 서킷이 열려 상품 목록까지 502 가 된다. 내보내기가 끝나면 스스로 풀린다. 원본은 Honeycomb 2024-08-06, PostgreSQL 기전 보조는 GoCardless. F66-R(회원 표 판)과 원인은 같고 막히는 표, 문장 종류(읽기 대 트랜잭션 안 쓰기), 사슬 모양, 피해 경로가 다르다.
---

# F66-P 설계 시트

## 1. 요약

재고 이벤트를 분석 저장소로 옮기는 데이터 작업이 commerce PostgreSQL 의 재고 이벤트 outbox 표 `inventory_schema.outbox_events` 를 통째로 읽는 내보내기 `COPY (SELECT id, aggregate_type, aggregate_id, event_type, topic, payload, created_at, published_at FROM inventory_schema.outbox_events) TO STDOUT` 를 시작한다. 받는 쪽이 일정한 속도로 받아(24시간 보존 약 7만 2천 행) 문장이 약 7분 동안 활성이고, 그동안 이 표의 ACCESS SHARE 잠금을 쥔다. 몇 초 뒤 다른 팀이 이벤트에 추적 id 를 싣는 기능을 준비하며 `ALTER TABLE inventory_schema.outbox_events ADD COLUMN trace_id varchar(64)` 마이그레이션을 돌린다. 기본값도 NOT NULL 도 없는 열 추가는 카탈로그만 바꾸는 "안전한" 변경이지만 그 순간 ACCESS EXCLUSIVE 잠금이 필요하다. 마이그레이션은 내보내기 뒤에서 기다리고, 기다리는 ACCESS EXCLUSIVE 는 모든 잠금과 충돌하므로 그 뒤의 outbox 문장(INSERT 포함)은 모두 줄을 선다. 역할과 DB 에 lock_timeout, statement_timeout 이 없어(0) 아무도 포기하지 않는다.

inventory-service 의 예약(`reserve`)은 한 트랜잭션에서 재고 행을 `SELECT ... FOR UPDATE` 로 잡고, 재고를 줄이고, 이동 이력을 INSERT 한 다음 마지막으로 outbox 에 이벤트를 INSERT 한다. 이 마지막 INSERT 가 줄을 서면서 트랜잭션은 재고 행 잠금과 inventory 의 DB 연결을 쥔 채 멈춘다. 같은 상품을 예약하려는 다음 트랜잭션은 그 재고 행 잠금(transactionid)을 기다린다. inventory Hikari 풀(10, 기본값, 연결 대기 30초)이 로컬 실측으로 15초 안에 모두 이렇게 묶이고, 그 뒤로는 outbox 와 무관한 재고 조회(`GET /api/inventory/{id}`)까지 연결을 얻지 못한다. product-service 는 상품 상세마다 재고를 조회하고 예약을 대신 부르는데 inventory 응답을 10초 읽기 시간 제한 끝에 포기해 500 을 낸다. 게이트웨이의 product 서킷브레이커가 열려 상품 목록과 카테고리까지 502 가 되고, checkout 은 order 의 product 호출이 실패해 5xx 다. inventory health 의 DB 확인이 연결을 얻지 못해 readiness 가 빠지고 liveness 가 재시작하며, 다시 뜬 파드는 기동 schema.sql 의 `CREATE INDEX IF NOT EXISTS`(이동 이력 표, outbox 표)가 같은 잠금 뒤에 서서 기동 프로브에 다시 시작된다. 내보내기가 끝나면 마이그레이션이 잠금을 얻어 열을 즉시 더하고 줄이 한꺼번에 풀린다.

비유: 창고(재고 행)에서 물건을 꺼낸 직원은 출고 장부(outbox)에 한 줄을 적어야 일을 마친다. 출고 장부 앞에 "장부에 칸 하나 추가, 잠깐이면 됨" 공사 팻말(마이그레이션)이 섰는데, 마침 누가 장부를 통째로 베끼는 중(내보내기)이라 공사가 기다린다. 공사가 줄 맨 앞에 서 있으니 장부에 적으러 온 직원들은 창고 열쇠(재고 행 잠금)와 계산대(DB 연결)를 쥔 채 기다리고, 같은 선반을 쓰려는 다음 직원은 열쇠를 기다리고, 계산대가 다 차서 재고만 물어보러 온 손님도 돌아간다. 장부도 창고도 멀쩡한데 가게 문 앞이 7분 동안 막힌다. 고칠 곳은 출고 장부 앞 줄(그 표의 잠금 대기열)이다.

## 2. 원본 사례

- **Honeycomb, 2024-08-06** (공식 상태 페이지 사고 "UI and API unavailable" 과 사후 보고, 2024-08-07 게시): https://status.honeycomb.io/incidents/ycgvhvs201q1
  - 12:59:39~13:20:05 PDT. API 가 요청의 75% 를 거절(대부분 /1/auth 인증 끝점), UI 는 적어도 19분 쓸 수 없었고 텔레메트리 약 4분의 1 을 버렸으며 트리거 평가가 멈춤.
  - "The schema migration was technically safe—a column addition to the `teams` table using an `INSTANT` algorithm". 마이그레이션 몇 초 앞서 같은 표를 읽는 질의가 시작됐고 그 느린 SELECT 는 일부 고객 조직에서 5분 넘게 걸릴 수 있었음. 마이그레이션이 잠금을 얻으려다 기다리는 동안("the `ALTER` statement itself waits for already running queries and transactions using this table to terminate") 그 요청이 그 표의 다른 질의를 막음.
  - 탐지: 13:00 경 저하 인지, 13:04 경보. 배포 직후라 진단이 어려웠고 DB 성능과 스키마 마이그레이션의 상관을 좁히는 데 몇 분. 후속: 느린 질의 점검, 클라이언트 시간 제한과 맞는 DB 쪽 시간 제한.
- **GoCardless** (공식 엔지니어링 블로그 "Zero-downtime Postgres migrations - the hard parts"): https://gocardless.com/blog/zero-downtime-postgres-migrations-the-hard-parts/ — 같은 기전의 PostgreSQL 설명(얻지 못한 잠금은 대기열에 들어가고 그와 충돌하는 뒤 잠금이 모두 뒤에 줄을 섬, AccessExclusive 는 모든 잠금과 충돌)과 그 기전으로 약 15초 API 가 멈춘 사고. 재구성 엔진(PostgreSQL)에서 고리가 원본(MySQL 메타데이터 잠금)과 같음을 보이는 보조 원본이다.
- 둘 다 자료 문서 M21 에 이미 있다(F66-R 에서 추가). 이번에 더한 사실은 없다.

### 요소별 대응표

| 요소 | 원본 사례 (Honeycomb 2024-08-06) | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | teams 표에 열을 더하는 스키마 마이그레이션(INSTANT, 기술적으로 안전) | 재고 이벤트 outbox 표에 기본값 없는 null 허용 열(trace_id varchar(64))을 더하는 마이그레이션(카탈로그만) |
| 원인이 된 결함 | 몇 초 앞서 시작된 같은 표의 느린 SELECT(5분 넘게 걸릴 수 있음)가 끝나기를 마이그레이션이 기다리며 잠금 요청을 대기열에 세움. DB 쪽 시간 제한 없음 | 몇 초 앞서 시작된 같은 표의 느린 내보내기(COPY, 약 7분 활성)가 ACCESS SHARE 를 쥐고 마이그레이션의 ACCESS EXCLUSIVE 요청이 대기열에 섬. lock_timeout, statement_timeout 0 |
| 전파 경로 | 기다리는 잠금 요청 뒤로 그 표의 다른 질의(인증 포함)가 막힘 | 기다리는 ACCESS EXCLUSIVE 뒤로 outbox 쓰기가 막힘 → 그 쓰기를 하는 재고 예약 트랜잭션이 재고 행 잠금과 연결을 쥔 채 멈춤 → inventory 풀 고갈 → product 상세, 예약 실패 → 게이트웨이 product 서킷 열림, checkout 5xx |
| 사용자 증상 | API 요청 75% 거절(대부분 /1/auth), UI 사용 불가 | commerce 상품 상세, 목록, 카테고리 502, checkout 5xx. 로그인, 장바구니, 은행 쪽은 정상 |
| 원본의 탐지 경로 | 저하 인지와 경보, DB 성능과 스키마 마이그레이션의 상관 | lucida-next 의 게이트웨이 오류율, commerce-inventory Hikari 오류 로그, DPM 세션 잠금 대기 사슬, KCM testbed-inventory Unhealthy |
| 완화와 복구 | 느린 질의가 끝나 마이그레이션이 지나가며 회복. 후속으로 느린 질의 점검, DB 쪽 시간 제한 | 내보내기가 끝나 마이그레이션이 지나가며 스스로 회복. cleanup 이 더한 열을 지움 |

기전 일치: 원인("그 자체로는 안전한 열 추가가 같은 표의 느린 읽기 뒤에서 잠금을 기다리며 대기열 맨 앞에 섬")에서 증상("그 표를 쓰는 요청 경로가 막혀 API 가 요청을 거절")까지 고리가 같다.

바꾼 것(기전 고리 밖):
- 엔진: MySQL 메타데이터 잠금 → PostgreSQL 표 잠금. 두 엔진 모두 "기다리는 배타 요청이 뒤의 공유 요청을 막는" 대기열 규칙이 같다(GoCardless). 열 추가가 표를 다시 쓰지 않는 점(INSTANT ↔ 기본값 없는 null 허용 열)도 같다.
- 느린 읽기의 정체: 원본은 앱의 느린 SELECT. 재구성은 이벤트 표를 분석 저장소로 옮기는 내보내기의 COPY 로, 받는 쪽이 일정 속도로 받아 약 7분 활성이다. 서버 쪽에서는 둘 다 같은 표의 공유 잠금을 쥔 채 오래 도는 읽기 문장이다. DB CPU 를 쓰는 무거운 질의가 아니다(F66-R 과 같은 이유).
- 막히는 경로: 원본은 그 표를 읽는 인증 경로. 재구성은 그 표에 쓰는 재고 예약 트랜잭션이다. 막힌 문장이 트랜잭션 안의 쓰기라 앞서 잡은 재고 행 잠금까지 함께 붙들려 두 번째 단의 대기(재고 행)가 생긴다.
- 길이: 원본 약 20분, 재구성 약 7분(대기 세션이 연결 한도를 넘지 않게, §9).

## 3. 숫자 근거 (2026-10-11, `scenario-stats.py`, 정식 45 + 후보 42 = 87, 이 후보 전)

- 묶음(정식 + 후보): J 14(16.1%), G 11(12.6%), P 10(11.5%), D 9(10.3%), A 7, B 7, L 7, C 6, E 4, F 3, H 2, K 2, Q 2, I 1, M 1, O 1, N 0. 금지(20%, 18 이상) 없음. 이 후보 D 9→10(88 중 11.4%).
- 정답 위치: 주문 서비스 8(9.2%)이 가장 크고, **DB 테이블(재고 이벤트 outbox) 0 → 1**(1.1%). 결제 경로 합계 12(13.8% → 88 중 13.6%, 이 후보와 무관).
- 서비스: 쇼핑몰 30, 은행 30, 음식배달 27. 쇼핑몰 30 → 31. 쇼핑몰은 은행과 최다 동률이라 원칙 2 완화 규칙(최다 서비스는 정답 위치가 0 인 부품만)을 따른다. 재고 이벤트 outbox 표는 0 인 정답 위치다(같은 'outbox' 라도 food 주문 이벤트 outbox 는 F48-P 의 다른 정답 위치).
- 부품 지도: commerce postgres 9(표마다 정답 위치가 갈림: 재고 3, 결제 2, 인증 토큰 1, 상품 1, 재고 이동 이력 1, 사용자 1, 재고 이벤트 outbox 0). 0 인 commerce 부품은 kafka, notification, nginx 인데 막힌 목록의 벽(비동기 소비자라 사용자 증상 없음, notification 은 DB 도 없음, nginx OTel 없음)에 걸린다.
- 왜 이 후보인가: 가장 적은 서비스(음식배달 27)에서 먼저 찾았지만 이번 반복의 음식배달 후보가 원칙 9, 1, 컨트롤러 필수 중단 조건으로 막혔다(아래 표 3, 4, 7, 8, 9, 10). 다음으로 쇼핑몰, 은행 동률 중 정답 위치가 0 인 부품을 정답으로 하는 후보가 이것이다. 은행은 0 인 부품(kafka, nginx, LEDGER_ENTRIES, OUTBOX_EVENTS, OUTBOX_RELAY_CONTROL)이 모두 막힌 목록의 벽(비동기, OTel 없음, F35-H 와 같은 주입, 원칙 7)에 걸린다.
- 겉 증상(inventory 풀 고갈과 재시작, checkout 5xx, 게이트웨이 product 서킷)은 F01-R(재고 한 행을 FOR UPDATE 로 쥔 트랜잭션)과 같다. 2026-10-09 사용자 정정대로 증상 겹침은 버릴 사유가 아니라 H 관계로 판별력을 더한다: F01-R 은 사슬 맨 앞이 재고 행을 쥔 채 노는 세션(ClientRead)이고, F66-P 는 재고 행을 쥔 세션들이 outbox 표의 relation 잠금을 기다리며 그 앞에 기다리는 DDL 과 오래 도는 내보내기가 있다.

### F66-R 과의 관계 (카탈로그 §1)

같은 원본, 같은 db.ddl 잠금 대기열 모드다. 파라미터가 다르고(표, 열, 내보내기 문장), 서비스 이름만 바꾼 복제가 아니다:
- 막히는 문장: F66-R 은 회원 표의 단순 읽기(토큰 확인). F66-P 는 트랜잭션 끝의 쓰기(outbox INSERT)라 앞서 잡은 재고 행 잠금이 함께 붙들린다.
- 사슬 모양: F66-R 은 앱 세션 → 기다리는 DDL → 내보내기의 두 단. F66-P 는 같은 상품을 예약하려는 세션 → (재고 행 transactionid) → outbox INSERT 를 기다리는 세션 → DDL → 내보내기의 세 단이고, 앱 세션의 relNames 에 outbox_events 와 inventory 가 함께 있다.
- 피해 경로: F66-R 은 게이트웨이 인가(쓰기만, 상품 둘러보기 정상). F66-P 는 inventory 풀 고갈이 product 상세와 예약을 막아 게이트웨이 product 서킷이 열리고 둘러보기까지 실패한다(인가와 로그인 정상).
- 관제 AI 가 배울 판단: 잠금 대기가 보이는 표(재고 행)와 고칠 표(outbox)가 다르다. 대기 사슬을 맨 앞까지 따라가야 정답에 닿는다.

### 후보 목록 (3단계, "실제 기전 × 부품", 숫자 순)

| # | 후보 | 층, 부품 | 원본 사례 | 결과 |
|---|---|---|---|---|
| 1 | commerce 재고 이벤트 outbox 열 추가 마이그레이션이 느린 이벤트 내보내기 뒤에서 잠금을 기다려 재고 예약이 재고 행 잠금과 연결을 쥔 채 멈춤 | DB, commerce outbox_events(D, DB 테이블(재고 이벤트 outbox) 0) | Honeycomb 2024-08-06 공식, GoCardless 공식 | **채택** |
| 2 | commerce 재고 이동 이력 인덱스(product_id, created_at)를 잃어 대사 배치의 상품별 최신 이력 조회가 전수 스캔, 재입고가 늦어 재고 소진 409 | DB, commerce inventory_movements(F, DB 테이블(재고 이동 이력) 1) | Buildkite 2025-11-10 공식, Vapi 2024-10-02 공식 | 버림: 원칙 2. 쇼핑몰이 최다 동률이라 정답 위치가 0 이어야 하는데 재고 이동 이력은 F57-P 로 1 |
| 3 | food 서비스 릴리스가 DB 클라이언트 풀 기본값을 크게 올려 food MySQL 연결 한도를 차지 | 앱, food 서비스(J, DB 인스턴스 2) | Netlify 2020-09-15 공식(의존성 갱신이 DB 클라이언트 풀 기본값을 높여 연결 급증) | 버림: 원칙 9, 컨트롤러 필수 중단 조건. 굶는 풀이 정해지지 않고 order 가 굶으면 health 가 빠져 입구가 끊긴다(막힌 목록 12, 61행과 같은 벽) |
| 4 | food restaurant 릴리스가 조회마다 가게 행에 마지막 조회 시각을 써 잠금 경합 | 앱, food restaurant(J+D, 가게 서비스 3) | Nango 2025-10-06 공식 | 버림: 원칙 1(막힌 목록 189행: 원본 계기가 부하 급증) |
| 5 | commerce 장바구니 표 열 추가 마이그레이션이 이탈 장바구니 내보내기 뒤에서 잠금 대기 | DB, commerce carts(D, DB 테이블(장바구니) 0) | Honeycomb 2024-08-06 공식 | 버림: 원칙 1. carts 가 20행이라 내보내기가 오래 돌려면 주문 이력 같은 큰 표를 억지로 이어야 하고, 1번과 정답 위치 축 이득이 같다 |
| 6 | commerce 재고 이벤트 outbox 표를 운영을 가리킨 로컬 마이그레이션이 지움 | DB, commerce outbox_events(P, DB 테이블(재고 이벤트 outbox) 0) | Resend 2024-02-21 공식 | 버림(보류): 같은 정답 위치를 1번이 쓰고, Resend 는 이미 다섯 표(F53-R, F53-P, F35-H, F32-P, F23-P)에 쓴 원본이라 다양성 이득이 1번보다 작다 |
| 7 | food 결제 정산 배치가 운영자의 정산 표시 초기화로 밀린 결제를 한 번에 읽어 메모리 고갈 | 앱, food payment(P+A, 결제 서비스 4) | 공식 사례 못 찾음 | 버림: 원칙 1. 결제 경로이고 결제 실패는 배차 뒤 단계라 배달원 용량이 샌다(막힌 목록 293행과 같은 벽) |
| 8 | food dispatch 또는 restaurant 검색 릴리스가 한 질의의 읽기 상한을 없앰 | 앱, food dispatch/restaurant(J+F) | ModernLoop 2026-08-26 공식 상태 페이지(https://status.modernloop.io/incidents/cgkyj0dk9bpr) | 버림: 카탈로그 §1, 원칙 9. 'release 질의가 DB 를 포화' 꼴은 F33-H, F42-P, F42-R(GitHub 2025-01-09)이 이미 쓰고, restaurants 는 23행이라 상한을 없애도 비용이 없다 |
| 9 | food 서비스 릴리스가 요청마다 사용량 지표를 DB 에 씀 | 앱, food 서비스(J+A) | Mezmo 2024 상태 페이지(사용량 지표를 Postgres 에 쓰기 시작해 과부하) | 버림: 원칙 9. food 기준선 1~6rps 에서 요청당 INSERT 하나는 MySQL 에 부하가 되지 않는다 |
| 10 | food order_items 에 유일 제약을 더하는 마이그레이션이 정상 주문 일부를 1062 로 거절 | DB, food order_items(L, DB 테이블(주문 품목) 0) | 공식 사례 못 찾음(웹 검색 1회: 포럼, 이슈뿐) | 버림: 원칙 1(막힌 목록 264행도 같은 결과) |

DB 가 정답인 후보 5개(1, 2, 5, 6, 10), 부품 8종(commerce outbox, 재고 이동 이력, 장바구니, food MySQL 인스턴스, restaurant, payment, dispatch, order_items), 앱, DB 층.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 주입: `scripts/scenarios/profiles/db_ddl_executor.py` `PG_QUEUE_REMOTE` `run`(내보내기 열 목록 인자가 있는 모드) — testbed-postgres-0 파드 안에서 백그라운드로 내보내기 `COPY (SELECT id, aggregate_type, aggregate_id, event_type, topic, payload, created_at, published_at FROM inventory_schema.outbox_events) TO STDOUT` 를 N 행마다 1초 쉬는 읽기 쪽(awk)에 흘리고(N = outbox 행 추정 / 420 + 1, 109 실측 행 추정 71,947 → N 172), 그 세션이 outbox 의 공유 잠금을 쥔 것을 본 뒤 `ALTER TABLE inventory_schema.outbox_events ADD COLUMN trace_id varchar(64)` 를 백그라운드로 띄우고, 서버가 ALTER 를 내보내기 뒤에서 기다리는 것으로 보이면 돌아온다.
2. outbox 표: `commerce/inventory-service/src/main/resources/schema.sql:23-34`(outbox_events, 미발행 부분 인덱스). 더하는 열은 엔티티(`commerce-common` OutboxEvent)에 없어 앱 읽기, 쓰기에 영향이 없다.
3. 재고 예약: `commerce/inventory-service/src/main/java/com/commerce/inventory/service/InventoryService.java:73-96` reserve(@Transactional) — `findByProductIdForUpdate`(:75, `InventoryRepository.java:17` PESSIMISTIC_WRITE) → 재고 UPDATE → 이동 이력 INSERT(:88) → `publishEvent`(:89, :138-142) → `commerce-common/.../outbox/OutboxPublisher.java:24-32` 가 같은 트랜잭션에서 outbox INSERT. 해제(:98-111), 재고 조정(:113-127)도 같다.
4. inventory 풀: `commerce/inventory-service/src/main/resources/application.yml:7-10`(datasource, hikari 설정 없음 → 기본 최대 10, 연결 대기 30초). 줄 선 예약 트랜잭션들이 연결을 쥐고, 뒤의 요청은 30초 뒤 'Connection is not available' 500.
5. 릴레이: `commerce-common/.../outbox/OutboxRelay.java:33-47` 가 2초마다 outbox 를 읽는데 이것도 줄에 선다(연결 하나 더 묶임, 재고 이벤트 발행 정지).
6. 기동: `application.yml:18-22`(spring.sql.init.mode always) → `schema.sql:21`(이동 이력 표 CREATE INDEX IF NOT EXISTS, 멈춘 예약 트랜잭션이 그 표의 쓰기 잠금을 쥐고 있어 기다림), `:34`(outbox 표 CREATE INDEX IF NOT EXISTS, 대기열 뒤). 재시작한 inventory 는 이 문장에서 멈춰 뜨지 못한다.
7. product: `commerce/product-service/src/main/java/com/commerce/product/service/ProductService.java:75-86`(상품 상세마다 `inventoryClient.getStock`), `:89-98`(예약 대행 `inventoryClient.reserve`), `InventoryClient.java:23-50`(시간 초과는 잡지 않아 500), `product-service/src/main/resources/application.yml:25-28`(inventory read-timeout 10s).
8. order checkout: `commerce/order-service/src/main/java/com/commerce/order/service/OrderService.java:144-160`(@Transactional checkout, cart → pricing → productClient.reserveStock), `order-service/src/main/resources/application.yml:73-81, 120-126`(productClient 서킷, 재시도 3회).
9. 게이트웨이: `commerce/api-gateway/src/main/resources/application.yml:38-41`(product read-timeout 10s), `:86-91`(product 서킷: 창 10, 최소 5, 실패율 50%, 열림 5초), `service/ProxyService.java:102-106`(서킷이 열리거나 실패하면 'Downstream unavailable for ...' WARN 과 502). product 서킷은 상품 목록, 카테고리, 상세를 함께 묶는다.
10. inventory 프로브: `commerce/k8s/22-inventory-service.yaml:69-88` startupProbe 5초 x 30, readinessProbe 10초 x 3(시간 제한 3초), livenessProbe 15초 x 5, 모두 `/actuator/health`(DB 확인 포함, 풀에서 연결을 30초 기다림).
11. DB: `commerce/k8s/10-postgres.yaml` testbed-postgres(PostgreSQL 16.14 alpine, tb-w1). 109 읽기 전용 실측(2026-10-11 08:06 UTC): max_connections 100, 열린 세션 62, lock_timeout 0, statement_timeout 0, outbox_events 71,965행(24시간 보존, 행 추정 71,947, 미발행 2), payload 평균 75바이트, 인덱스 셋(pkey, 같은 정의의 미발행 부분 인덱스 둘).

### 로컬 실측 (2026-10-11, 원칙 9 근거)

origin/main(551016c)의 commerce 를 임시 폴더에 풀어 inventory-service jar 를 빌드하고(`mvnw -o -pl inventory-service -am package`), PostgreSQL 16.14 컨테이너(같은 판)에 schema.sql, data.sql 과 outbox 72,000행, 이동 이력 20,000행을 넣고 붙여 띄웠다(Kafka 3.9.0 단일 브로커 함께). 예약 약 1.7건/초, 재고 조회 약 5건/초를 클라이언트 시간 제한 10초(product 와 같음)로 보냈다. 실행기 스크립트를 그대로(가짜 kubectl 로 docker exec) export_seconds 90 으로 돌렸다.

| 시험 | 결과 |
|---|---|
| run | preflight 0, run 2.0초 만에 0. 15초 뒤 pg_stat_activity: inventory 세션 9개가 Lock/relation 으로 outbox INSERT 를 기다리며 모두 ALTER 의 pid 에 막힘, 1개는 Lock/transactionid 로 같은 상품의 예약 세션에 막힘, ALTER 는 Lock/relation 으로 내보내기에 막힘, 내보내기는 Client/ClientWrite 활성 |
| 피해 | 주입 15초 창부터 예약 25건 중 0건, 재고 조회 57~75건 중 0건 성공(모두 10초 시간 초과). inventory health 5초 안에 응답 없음. inventory 로그 'HikariPool-1 - Connection is not available, request timed out after 30000ms (total=10, active=10, idle=0, waiting=187)' 666줄. 내보내기가 끝난 약 87초 창부터 모두 200(평균 0.01초) |
| inventory 를 죽였다 다시 띄움(대기열 유지) | 죽인 프로세스의 줄 선 백엔드 10개(outbox INSERT 8, 릴레이 SELECT 1, transactionid 1)가 그대로 남음. 다시 띄운 inventory 는 schema.sql 의 이동 이력 표 CREATE INDEX IF NOT EXISTS 가 Lock/relation 으로 막혀 health 응답 없음(유휴 연결 9개 + 줄 선 1개). 내보내기가 끝나자 곧 'Started InventoryServiceApplication in 59.984 seconds', 그 창부터 예약 200 |
| cleanup, recovery | cleanup 2.6초(열 지움, 파드 안 파일 삭제), recovery 0. 처음 시험은 Kafka 없이 띄워 릴레이가 Kafka 연결을 기다리며 outbox 에 열린 트랜잭션(idle in transaction)을 남겨 열 지우기가 lock_timeout 5초에 다섯 번 걸려 cleanup 이 1 을 냈다. Kafka 를 붙인 뒤에는 릴레이 트랜잭션이 ms 단위라 통과했다. 109 commerce 는 Kafka 가 떠 있고 Top SQL 의 릴레이 조회 평균 2.29ms, 발행 표시 UPDATE 0.064ms 다(§7) |

운영과의 차이: 109 는 aarch64 alpine(busybox awk, F66-R 이 109 에서 `awk ... system()` 확인), inventory 는 쿠버네티스 프로브로 재시작된다(로컬은 손으로). 대기열은 잠금 규칙으로만 정해져 CPU 속도와 무관하다. 109 예약 속도는 표본 트레이스로 시간당 약 4,000건(약 1.1건/초, 17시 KST)이고 재고 조회는 약 6.7건/초라 로컬과 비슷하다.

## 5. 원인 규정 (원칙 5, 6)

- 근본(`root_cause.target_id`): `commerce-postgres:inventory_schema.outbox_events` — 그 표에 생긴 잠금 대기열(이미 안전한 DDL 이 같은 표의 긴 읽기 뒤에서 ACCESS EXCLUSIVE 를 기다림). F66-R, F06-H, F01-R 과 같은 '인스턴스:스키마.테이블' 꼴.
- 계기(`trigger_target_id`): 비움. 계기(그 표에 대한 마이그레이션)와 결함 위치가 같다. inventory, product, order, 게이트웨이, 부하는 평시 그대로 정당하다(원칙 6).
- 부분 점수: `commerce-inventory`(증상이 드러나는 서비스), `commerce-postgres`(인스턴스 단위 답), `commerce-postgres:inventory_schema.inventory`(잠금 대기가 눈에 보이는 재고 행 표: 사슬의 둘째 단까지만 따라간 답).
- 층위: 원본 사후 보고의 결론("안전한 열 추가가 같은 표의 느린 SELECT 뒤에서 잠금을 기다리며 그 표 질의를 막음")과 같다. 관제 데이터로 낼 수 있는 결론: "outbox_events 표에서 잠금 대기 사슬(재고 예약 세션 → 기다리는 세션 → 오래 도는 읽기)이 생겨 예약 트랜잭션이 연결과 재고 행 잠금을 쥔 채 멈추고 inventory 풀이 말랐다". 마이그레이션이 무슨 열을 더했는지, 내보내기가 무엇이었는지는 요구하지 않는다(세션 스냅숏에 문장 원문이 없다, §7).

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 |
|---|---|
| 감지 | commerce-gateway 오류율(product, checkout 502, 서버 스팬 ERROR), commerce-inventory, commerce-product 오류 로그 급증(평시 inventory WARN 이상 4일 26줄, Hikari 0), KCM testbed-inventory Unhealthy, Killing |
| 피해 판정(러너) | 상주 기준선(domain commerce) checkout 5xx 비율 ≥ 0.5 와 2xx 비율 < 0.3, 그리고 재고 표에 걸린 채 막힌 DB 세션 ≥ 1(러너 database.blocked_session_count, inventory_schema.inventory) 이 3틱 |
| RCA | DPM 세션 스냅숏의 잠금 대기 사슬과 relNames, DB CPU 와 Top SQL 평시, 롤아웃 없음, 내보내기 세션이 사라지는 분에 함께 회복 |

## 7. 관측 근거 표 (119 실조회, 2026-10-11 07:30~08:20 UTC)

표본 데이터(트레이스 10%, APM 스팬 지표)만으로 증명하지 않는다. 결정 증거는 DPM 세션 스냅숏(분마다), 전수 로그, KCM 이벤트다.

| 증거 | 119 표와 칸 | 조회 | 평시 결과 |
|---|---|---|---|
| 근본: outbox 잠금 대기 사슬 | CH `lucida.dpm_session_local`(engine, body 의 pid, program, ip, waitType, waitEvent, sessionType, blockingPid, relNames, queryTime) | 60일 `engine='postgresql' AND JSONExtractString(body,'relNames') LIKE '%outbox_events%'`, 30일 날짜별 `waitType='Lock' AND waitEvent='relation'` | relNames 에 outbox_events 가 든 행 60일 5건(2026-08-18, 08-31, 09-01 의 다른 실행: 재고 예약 트랜잭션의 relNames 'inventory, inventory_movements, ..., outbox_events, outbox_events_id_seq', waitEvent transactionid, WALWrite, ClientRead). 예약 트랜잭션이 잡은 표 전부가 relNames 에 오르는 것과, Lock/relation 대기가 outbox 로는 한 번도 없었음을 확인. Lock/relation 은 2026-09-25 뒤로 0건. 장애 때 inventory 파드 IP 세션의 blocked Lock/relation(relNames 에 outbox_events) → 같은 blockingPid 의 대기 세션(ALTER, Lock/relation) → queryTime 이 약 420초까지 느는 활성 ClientWrite 세션 사슬과, 같은 상품 예약 세션의 Lock/transactionid 가 처음 나타나야 한다 |
| 계기: 마이그레이션과 긴 읽기 | 같은 표 | 같은 행의 blockingPid 를 따라감 | 사슬 둘째 단(ALTER)과 맨 앞(내보내기). 두 세션은 program 'PostgreSQL JDBC Driver', DB 파드 안 연결이다. 세션 행에는 문장 원문이 없고, DPM Top SQL 은 앞선 수집에서 본 문장의 증분만 내보내 한 번 도는 ALTER, COPY 는 닿지 않는다(F66-R, F23-P 시트와 같은 확인) |
| 전파: inventory 풀 고갈 | CH `lucida.lucida_logs_local` | 8일 `service_name='commerce-inventory'` 템플릿별, 날짜별 Hikari | 평시는 대사 배치 INFO 뿐. Hikari 'Connection is not available, request timed out after 30000ms (total=10, active=10, idle=0, ...)' 682줄, 'SQL Error: 0' 797줄, Servlet 785줄은 모두 2026-10-06 하루(다른 실행, 재고 경로 시나리오)이고 그 밖의 날은 0. 장애 때 이 템플릿이 다시 나타나야 한다(로컬 같은 jar 666줄) |
| 전파: product, 게이트웨이 | CH `lucida.lucida_logs_local` | 2026-10-06 10:30~11:40 의 같은 증상 실행, 4일 평시 | 그 실행: commerce-product Servlet 오류 9,394, 게이트웨이 'Downstream unavailable for POST /api/orders/checkout: ...CallNotPermittedException' 8,700, 'Downstream unavailable for GET /api/products: ...CallNotPermittedException' 5,593, 상세와 카테고리 같은 꼴. 평시 4일 commerce-product, 게이트웨이 WARN 이상 0줄 |
| 전파: inventory 재시작 | CH `lucida.kcm_events_local`(reason, object_name) | 30일 `object_name LIKE 'testbed-inventory%'` reason 별 | Unhealthy 1,003, Killing 78, Started 78 은 2026-10-06 10:40~11:29(같은 실행), 롤아웃은 2026-10-01 이 마지막. 장애 때 롤아웃 없이 Unhealthy, Killing 이 생겨야 한다 |
| 감별: DB 는 한가, 쓰기는 평시 ms | CH `lucida.dpm_topsql_local`(sql_hash, body 의 calls, avgExecTime) | 1일 outbox 관련 문장 | outbox INSERT(sql_hash -1413230298953187308) 28,824호출 평균 0.048ms, 발행 표시 UPDATE 15,599호출 0.064ms, 릴레이 조회 486호출 2.29ms, 재고 FOR UPDATE 조회 0.211ms. 장애 때 outbox INSERT 호출이 멎고, 풀린 뒤 줄 섰던 INSERT 의 실행 시간이 수백 초로 기록된다(잠금 대기 포함). CPU 를 쓰는 문장이 새로 늘지 않는다 |
| 감별: 부하 | CH `lucida.otel_traces_local`(10% 표본, 보조) | 1시간 commerce-inventory SERVER 스팬 이름별 | 재고 조회 2,398, 예약 407, health 55(표본, 전수 약 10배 = 조회 약 6.7건/초, 예약 약 1.1건/초). 동반 부하는 없다 |
| 참고: DB 설정 | 109 PostgreSQL 읽기 전용 조회 | `SHOW lock_timeout`, `statement_timeout`, pg_stat_activity 수, outbox 행 수와 행 추정, 인덱스 정의 | 0, 0, 62, 71,965행(추정 71,947), pkey 와 미발행 부분 인덱스 둘(2026-10-11) |

## 8. 감별

- must_support: DPM 세션의 outbox_events 잠금 대기 사슬(inventory 세션 blocked Lock/relation relNames outbox_events → 같은 blockingPid 의 대기 세션 → 오래 도는 ClientWrite 활성 세션), 같은 상품 예약 세션의 Lock/transactionid, commerce-inventory Hikari 'Connection is not available ... 30000ms'(평시 0, 다른 실행 날만), product Servlet 오류와 게이트웨이 'Downstream unavailable'(평시 0), KCM testbed-inventory Unhealthy, Killing(롤아웃 없음), checkout 5xx ≥ 0.5 와 2xx < 0.3, PostgreSQL Ready 와 Top SQL 평시, 내보내기 세션이 사라지는 분에 함께 회복.
- must_rule_out: 재고 행을 쥔 채 노는 트랜잭션(F01-R), 재고 이동 이력 시퀀스 되감기(F57-P), 인덱스를 잃은 전수 스캔(F33-P 꼴), PostgreSQL 다운(F25-H), inventory 배포나 프로브 오설정, 재입고 배치 정지(F23-R).
- contrast_with: F01-R, F66-R, F57-P, F23-R, F25-H.
- 관계: F01-R 과 겉 증상(inventory 풀 고갈, checkout 5xx, 게이트웨이 product 서킷, inventory 재시작)이 같고 정답 표가 다르다(H). 판별력은 사슬 맨 앞에 있다: F01-R 은 재고 행을 쥔 세션이 ClientRead 로 놀고 그 세션의 relNames 에 outbox 가 없다. F66-P 는 재고 행을 쥔 세션들이 outbox 의 relation 잠금을 기다리고 그 앞에 DDL 과 내보내기가 있다. F66-R 과는 원인 꼴이 같고 표, 막힌 문장, 사슬 단수, 피해 경로가 다르다(위 §3). F57-P 와는 같은 inventory 쓰기 경로지만 F57-P 는 쓰기가 23505 로 곧바로 실패하고(연결을 쥐지 않음) 앞서 DB 전체가 몇 초 읽기 전용이 된다.

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

- 강도는 하나로 고정(approved-fixed-f66-p): 내보내기 약 420초(export_seconds), cleanup 대기 상한 300초(drain_seconds), 시작 조건 열린 세션 70 이하(maximum_sessions), outbox 행 10,000 이상(minimum_rows), 행 추정이 export_seconds 의 100배(42,000) 이상(실행기 공통 확인, 109 71,947). 대기열은 잠금 규칙으로 정해져 결정적이다: 내보내기가 도는 동안 outbox 에 쓰는 트랜잭션은 하나도 끝나지 않는다.
- 부하: 동반 부하를 두지 않는다. 판정은 상주 기준선(loadgen-commerce, `commerce/loadgen/entrypoint.sh:11-17` 하루 주기 2~10rps, 사전 할당 VU 20, 최대 50)에서 한다. F66-R 과 달리 로그인(users)은 막히지 않아 checkout 여정(여정의 8%)이 모두 checkout 까지 간다: 30초 창마다 checkout 약 5~24건이 표본이다.
- 피해 계산: inventory 풀 10 이 기준선 예약(약 0.2~1.1건/초)과 재고 조회(상품 상세, 약 1~7건/초) 앞에서 로컬 실측 15초 안에 찬다. 예약 트랜잭션은 outbox INSERT 에서 멈춰 연결을 놓지 않고, 그 뒤 요청은 30초 연결 대기에 걸려 product 의 10초 읽기 시간 제한이 먼저 끝난다(product 500). order 의 checkout 은 productClient 재시도와 서킷 끝에 5xx, 게이트웨이 product 서킷이 열려 상품 둘러보기까지 502 다(2026-10-06 같은 증상 실행에서 확인한 꼴). 캐시나 대체 경로가 없어 checkout 은 모두 5xx 다.
- k6 VU: 처음 서킷이 열리기 전 약 10~20초 동안 요청이 10~15초씩 매달려 피크(10rps)에는 VU 50 이 모자라 achieved_rps 가 잠시 떨어질 수 있다. 서킷이 열리면 곧바로 502 라 회복된다. 배제 조건 achieved_rps < 1 은 VU 50 / 여정 약 20초 = 약 2.5rps 보다 낮아 이 구간에 발화하지 않는다.
- 연결 한도 계산: 줄 선 백엔드는 inventory 파드가 죽어도 잠금이 풀릴 때까지 남는다(로컬 확인). 첫 파드의 10개, liveness 재시작(약 75초 + 기동) 뒤 다시 뜬 파드마다 기동 CREATE INDEX 1개(유휴 연결 9개는 파드가 죽으면 닫힘), 기동 프로브 실패(150초)와 백오프로 7분 동안 재시작 약 2~3번 → 약 13개. order 는 checkout 트랜잭션이 연결을 쥔 채 product 를 기다려 풀(10)이 찰 수 있으나 그 연결은 대기열에 서지 않아(idle in transaction) order 가 재시작하면 닫힌다. 평시 62 + 주입 세션 2 + 새 inventory 파드 9 + 남는 대기 13 ≈ 86 < 100. 시작 조건 70 이하가 여유를 지킨다. 길이를 7분으로 둔 이유다(원본 20분).
- success: 기준선 checkout_5xx_rate ≥ 0.5, 기준선 checkout 2xx 비율 < 0.3, 재고 표에 걸린 채 막힌 세션 ≥ 1(database.blocked_session_count, inventory_schema.inventory: pg_blocking_pids 가 비지 않고 재고 표에 잠금을 가진 세션. 줄 선 예약 트랜잭션이 재고 표의 쓰기 잠금을 쥔 채 outbox 를 기다려 여기에 잡힌다), 3틱(틱 15초, 관측 갱신 30초라 약 90초). min_hold 3m(inventory 재시작 고리까지 녹화되게), settle 30s, timeout 7m(내보내기가 끝나는 시각).
- must_rule_out: 기준선 achieved_rps < 1, commerce PostgreSQL 파드 NotReady(2틱). 상품 목록 실패율은 배제 조건으로 쓰지 않는다: 게이트웨이 product 서킷이 상세와 목록을 함께 묶어 이 시나리오에서는 목록도 실패한다(F66-R 과 다른 점).
- abort: 기준선 entry_status == 0(2틱, 기준선의 마지막 checkout 응답 상태). commerce 입구는 게이트웨이(30080)이고 게이트웨이는 DB 를 쓰지 않는다. checkout 은 게이트웨이 → order 읽기 시간 제한 15초 안에 5xx 로 끝나 상태 0 이 되지 않는다. 게이트웨이 Tomcat 200 에 비해 매달린 요청은 서킷이 열리기 전 약 10~20초 동안의 수십 건이다.
- recovery: target_health 200, inventory Ready, PostgreSQL Ready, 재고 표에 막힌 세션 0, 기준선 쓰기 nonok < 0.1(2틱, 10분). 내보내기가 끝나면 줄이 한꺼번에 풀리고, 재시작 중이던 inventory 는 기동 문장이 끝나 곧 뜬다(로컬). CrashLoopBackOff 지연이 남을 수 있어 10분이다.
- cleanup: 내보내기가 스스로 끝나기를 최대 300초 기다리고(넘으면 pg_terminate_backend, 남은 마이그레이션은 pg_cancel_backend), 두 세션이 없어진 뒤 `SET lock_timeout = '5s'; ALTER TABLE inventory_schema.outbox_events DROP COLUMN IF EXISTS trace_id`(카탈로그만, 최대 5번), 파드 안 스크립트, 로그 파일 삭제. 행, 인덱스, 다른 표는 손대지 않는다. 내보내기 출력은 파드 안 awk 가 버린다(파일로 남기지 않음).
- run 시간: 탐침 약 8번 + kubectl exec 3번 + 확인 대기 각 1~2초, 로컬 2.0초. 러너 coordinator lease(30초) 안이다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Honeycomb 2024-08-06 공식 사후 보고, PostgreSQL 기전 GoCardless 공식 블로그)와 요소별 대응표, 기전 동일(§2)
- [x] 분류 장부 갱신(§4-1 행, §6 기록), 묶음 D(9→10, 88 중 11.4%), 정답 위치 DB 테이블(재고 이벤트 outbox)(0→1), 결제 경로 12(13.6%), 서비스 쇼핑몰 30→31(최다 동률이지만 정답 위치 0 부품), 고른 이유(§3). 어느 축도 20% 미만
- [x] 근본 원인 위치 `file:line`(예약 트랜잭션, outbox 쓰기, 풀, 기동, product, 게이트웨이, 프로브)과 인프라 지점(testbed-postgres-0, 설정 실측)(§4)
- [x] 근본 원인 흔적 119 실조회: dpm_session_local 이 예약 트랜잭션 세션의 relNames 에 outbox_events 를 남김(다른 실행 행으로 확인), 대기 행에 blockingPid 가 남음(F66-R 확인), outbox 로의 Lock/relation 대기 60일 0건(§7)
- [x] 핵심 증거가 표본에만 있지 않음: DPM 세션 스냅숏(분마다), 전수 로그, KCM 이벤트. 트레이스는 보조
- [x] 계기 흔적: 사슬 둘째 단의 대기 세션(마이그레이션)과 맨 앞의 오래 도는 읽기 세션(내보내기)이 세션 스냅숏에 남음. 인공 지연 없음(대기는 잠금 규칙, 내보내기 속도는 받는 쪽이 정하고 pg_sleep 없음)
- [x] 정답이 관제 데이터로 낼 수 있는 결론(outbox 표의 잠금 대기 사슬이 예약 트랜잭션과 inventory 풀을 묶음), 코드 설계 결함 추론 불필요. 예약 트랜잭션 끝에 outbox 를 쓰는 구조와 게이트웨이 서킷이 목록까지 묶는 것은 피해 설명(user_impact)
- [x] 정답지 세 칸이 원칙 6 대로(근본 outbox 표, 계기 같음, 부분 점수 commerce-inventory, commerce-postgres, inventory 표)
- [x] 감지, 피해 판정, RCA 증거 구분(§6)
- [x] 주입 도구가 시나리오 id 를 남기지 않음: 실행기 인자는 네임스페이스, 파드, 스키마, 표, 열, 형식, 숫자, 열 목록뿐이고, 두 세션은 드라이버 기본 이름 'PostgreSQL JDBC Driver', 파드 안 파일 이름은 /tmp/event-export.sh, /tmp/event-export.log, /tmp/schema-migration.log, 탐침은 pg_stat_statements 에 남지 않음(테스트 `test_postgres_event_table_migration_queues_behind_an_event_export_with_writes_in_flight`)
- [x] 부하 상한과 서킷브레이커를 고려한 피해 계산(§9: inventory 풀 15초 고갈, product 시간 제한과 게이트웨이 서킷, k6 VU 여유, 연결 한도 여유 계산, 로컬 실측)
