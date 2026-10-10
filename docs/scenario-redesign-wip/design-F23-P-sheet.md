---
title: F23-P 설계 시트 (운영을 가리킨 로컬 마이그레이션이 commerce 상품 표를 지워 상품 둘러보기 500, checkout 재고 예약 단계 502)
status: Draft
owner: project
last_reviewed: 2026-10-10
tags:
  - scenario
  - design
  - schema
  - postgresql
  - operation
summary: 상품 표를 바꾸는 기능을 만들던 개발자가 자기 PC 에서 돌린 마이그레이션의 연결이 로컬 DB 대신 운영 commerce PostgreSQL 을 가리켜, 지우기 단계(DROP TABLE IF EXISTS product_schema.products CASCADE)가 살아 있는 상품 표를 지운다. product-service 의 상품 조회가 모두 42P01 'relation does not exist' 로 실패해 gateway 를 거친 둘러보기가 500, checkout 은 재고 예약 단계에서 전량 502 이고 로그인, 장바구니, 가격은 정상이다. 원본은 Resend 2024-02-21(F53-R 과 같은 원본의 PostgreSQL, commerce 판). 지운 표의 스캔 카운터가 빠져 DPM 의 commerce 스캔 합이 그 분에 내려간다.
---

# F23-P 설계 시트

## 1. 요약

commerce 의 product-service 는 상품 목록을 PostgreSQL `product_schema.products`(2026-10-10 109 조회 2,016행, 인덱스 셋 포함 432kB)에 둔다. `product_schema.product_variants`(6,048행)가 외래 키로 이 표를 가리키고, 두 표를 읽고 쓰는 것은 product 하나다(저장소 grep). product 는 commerce 에서 가장 바쁜 서비스다(119 표본 서버 스팬 시간당 3,784, commerce PostgreSQL Top SQL 행의 36.5% 가 이 표 질의). 상품 표를 바꾸는 기능을 만들던 개발자가 자기 PC 에서 마이그레이션을 돌리는데, 명령이 쓰는 연결이 로컬 DB 가 아니라 운영 commerce PostgreSQL 을 가리키고 있었다. 마이그레이션의 지우기 단계 `DROP TABLE IF EXISTS product_schema.products CASCADE` 가 운영의 살아 있는 표를 지운다.

그 순간부터 이 표를 부르는 모든 문장이 PostgreSQL 42P01 `relation "product_schema.products" does not exist` 로 실패한다. gateway 를 거친 상품 목록, 검색, 상세, 변형 화면은 product 가 데이터 접근 예외를 서블릿까지 올려 500 이고, gateway 는 하류 상태를 그대로 넘긴다(서킷 없음). checkout 은 장바구니와 가격 견적 뒤 품목마다 product 에 재고 예약(`POST /api/products/{id}/reserve-stock`)을 부르는데, 예약이 상품 조회로 시작해 product 가 즉시 500 을 내고 order 의 productClient 가 200ms, 400ms 재시도 뒤 서킷을 열어 502 'Product service unavailable for product N' 으로 끝난다. 주문, 결제, banking 정산은 쓰이지 않는다.

이 표를 부르지 않는 것은 같은 PostgreSQL 에서 그대로다: 로그인과 토큰 확인, 장바구니, 가격 견적, 재고, 배송. 롤아웃도 재시작도 설정 변경도 없고 PostgreSQL 은 떠 있으며 product 는 Ready 다(health 는 DB 연결 확인). 지우기는 DPM 이 합산하는 DB 통계에 단차를 남긴다: 표의 스캔 카운터가 빠져 commerce 의 순차, 인덱스 스캔 합이 그 표의 누적값만큼(첫 실행 기준 약 43%, 16%) 내려간다.

비유: 쇼핑몰의 상품 진열 대장을, 본사 직원이 연습용 매장 것을 치운다는 게 실제 매장 것을 치웠다. 손님은 들어와 로그인하고 장바구니도 들 수 있지만, 진열대 조회도 계산대의 재고 확인도 "대장이 없다" 며 막힌다.

## 2. 원본 사례

- 기업: Resend (메일 발송 API)
- 날짜: 2024-02-21 (UTC 04:56 마이그레이션 시작, 17:05 해결)
- 링크: [Incident report for February 21, 2024 (공식 블로그)](https://resend.com/blog/incident-report-for-february-21-2024) (자료 문서 `ref-real-world-incidents.md` M22 에 있음, F53-R 이 추가)
- 요약(출처가 말한 것만): 기능을 만들던 엔지니어가 로컬 환경에서 데이터베이스 마이그레이션을 돌렸는데 그 명령이 운영 환경을 가리켜 운영의 모든 표를 지웠다("incorrectly pointed to the production environment instead, which dropped all tables in production"). 04:57 운영에서 표가 지워지는 것을 알아챘고 05:01 백업 복원을 시작했다. 첫 복원은 잘못된 백업 시각 선택으로 실패했고, 더 오래된 백업에서 다시 복원해 17:02 API 요청 수락을 재개했다. 약 12시간 모든 사용자가 메일 발송, API, 대시보드를 쓰지 못했고, 마이그레이션 직전 5분의 기록을 잃었다.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 기능을 만들던 엔지니어가 로컬 환경에서 데이터베이스 마이그레이션 실행 | 상품 표를 바꾸는 기능을 만들던 개발자가 자기 PC 에서 마이그레이션 실행 |
| 원인이 된 결함 | 명령이 로컬이 아니라 운영 환경을 가리켜 운영의 모든 표를 지움 | 명령의 연결이 운영 commerce PostgreSQL 을 가리켜 지우기 단계 `DROP TABLE IF EXISTS product_schema.products CASCADE` 가 운영 상품 표를 지움(그 기능이 바꾸는 표 하나로 규모를 줄임) |
| 전파 경로 | 표가 없어 API 가 요청을 받지도 저장하지도 못함 | product 의 `Product` 엔티티가 `product_schema.products` 를 부름 → 42P01 → 상품 둘러보기 500(gateway 통과) → order checkout 의 재고 예약 500 → 재시도, 서킷 → checkout 502 |
| 사용자 증상 | 메일 발송, API, 대시보드 약 12시간 전면 불가 | commerce 상품 둘러보기와 구매 100% 실패. 로그인, 장바구니, 가격 견적은 정상 |
| 탐지된 경로 | 1분 뒤 운영에서 표가 지워지는 것을 알아챔 | commerce-product, gateway, order 오류율과 새 오류 로그(42P01 WARN, ERROR, Servlet SEVERE) 급증 → 119 이상 탐지 → 인시던트. DPM 스캔 합의 단차와 상품 질의 행 소멸 |
| 완화와 복구 | 백업에서 복원(두 번째 시도에 성공), 5분 기록 손실 | cleanup 이 표를 되돌림(행 그대로, 즉시 회복). 원본의 백업 복원 몇 시간은 재현하지 않는다 |

기전은 원본과 같다: "개발 중 로컬에서 돌린 마이그레이션 명령이 운영을 가리켜 운영의 표를 지움 → 앱이 그 표를 찾지 못해 그 표를 쓰는 요청이 모두 실패 → 표를 되살리면 회복". 우리 스택에 맞춘 것은 규모(표 하나)와 엔진(PostgreSQL)이다.

재구성의 모양: PostgreSQL 에는 휴지통이 없어 실제 DROP 은 행과 product_variants 의 외래 키를 잃는다. 그래서 pg_stat_statements 가 기록하지 않는 세션(`PGOPTIONS=-c pg_stat_statements.track=none`, 앱 계정 commerce 는 슈퍼유저라 시작 옵션으로 줄 수 있음)이 원본을 `public` 스키마로 옮기고(`ALTER TABLE ... SET SCHEMA public`, 상품 엔티티는 모두 `@Table(schema = "product_schema")` 라 앱은 찾지 않음), 옮긴 표와 인덱스 셋의 통계 카운터를 `pg_stat_reset_single_table_counters` 로 0 으로 돌린다. 실제 DROP 이면 표의 카운터가 pg_stat_user_tables 에서 빠져 DPM(`postgres_delta_db.go` buildTableStatMetrics)이 DB 별로 합산하는 `dpm.postgresql.sql.seq_scans.raw`, `index_scans.raw` 가 그 분에 표 몫만큼 내려간다(109 2026-10-10: products seq_scan 31,743,420 / 전체 73,844,974 = 43%, idx_scan 42,169,455 / 265,647,669 = 16%). 옮기기만 하면 통계가 OID 를 따라가 이 단차가 없으므로 카운터를 0 으로 돌려 같은 단차를 남긴다(표의 idx_scan 은 인덱스 카운터에서 읽혀 인덱스도 함께 0 으로). 백업에서 되살린 표의 카운터가 0 에서 시작하는 현실과도 맞다. 지우기 문장 자체는 재현하지 않는다: DPM Top SQL 수집기(`postgres_topsql.go` delta)는 앞선 폴에 본 문장의 증분만 내므로(처음 보는 queryid 는 건너뛰고 다음 폴은 증분 0) 한 번 실행되는 DROP 은 실제 장애에서도 녹화에 남지 않는다(119 PG DDL 행 24종은 모두 기동 때마다 되풀이되는 CREATE ... IF NOT EXISTS). F53-R 이 MySQL 에서 DPM 이 보지 않는 시스템 스키마로 옮긴 것과 같은 꼴로, 옮기는 동작은 관측되지 않고 앱과 관측 데이터에는 지우기 그 자체로 보인다. 표 크기(432kB)와 live_rows(products n_live_tup 0)는 DB 전체(11.99GB, 약 6,533만 행)에 비해 작거나 0 이라 실제 DROP 이었어도 크기, 행 지표는 바뀌지 않는다.

(1차 평가 FAIL 반영, 2026-10-10: 처음 설계는 기록되지 않는 세션으로 표를 옮긴 뒤 빈 대역 표를 두고 보통 세션의 DROP 으로 대역을 지워 DPM Top SQL 에 DROP 행을 남기려 했다. 평가가 Top SQL 수집기는 처음 보는 queryid 를 건너뛰어 한 번 실행되는 DROP 이 녹화에 끝내 남지 않고, 옮기기만 하면 스캔 카운터가 OID 를 따라가 실제 지우기의 단차가 사라진다는 것을 수집기 코드와 109, 119 조회로 보였고, 맞다. 대역과 DROP 을 빼고 카운터 초기화를 더했다. 로컬 재실측은 §4.)

관계와 id: 원본과 도구(표 지우기)는 F53 사례군(F53-R, F53-P)과 같지만 그 사례군의 R, P 는 이미 쓰였다. checkout 이 재고 예약 단계에서 실패하는 전파의 일부가 F23-R(재입고 배치 정지로 재고 소진, 409)과 같고 최초 원인과 영향 범위(둘러보기까지)가 달라 F23 사례군의 P 로 둔다(F12-P 는 옛 네트워크 시나리오가 예약한 id). F53-R, F53-P, F32-P, F35-H 와는 DB 엔진(PostgreSQL), 도메인과 진입 경로(commerce gateway), 영향 범위(둘러보기와 checkout), 계기 흔적(지운 표의 스캔 카운터가 빠지는 DPM 단차)이 달라 카탈로그 §1 의 "서비스 이름만 바꾼 복제" 가 아니다. 같은 수단(db.ddl)이지만 새 모드(PostgreSQL 표 지우기)와 새 파라미터라 같은 주입 중복도 아니다.

## 3. 숫자 근거 (2026-10-10, `scenario-stats.py`, 정식 + 후보 합계 74, 이 후보 전)

| 축 | 값 | 판단 |
|---|---|---|
| 서비스 | 쇼핑몰 26, 은행 25, 음식배달 23 | 쇼핑몰이 가장 많다. 원칙 2 완화 규칙(최다 서비스는 정답 위치가 0 인 부품만)에 따라 정답 위치 0 인 DB 테이블(상품)로 낸다. 음식배달, 은행 쪽 후보는 아래 목록에서 카탈로그 §1, 원칙 1, 9 로 막혔다 |
| 묶음 | J 10(13.5%), P 9(12.2%), G 8, A, B, D, L 7, C 6, F 3, E, H, K 2, I, M, O, Q 1, N 0 | P(운영 작업의 대상 착오) 9→10(13.3%). 결함이 앱이 기대하는 표가 없는 꼴로 드러나 F53-R 처럼 P+L. L 로 세도 7→8 |
| 정답 위치 | DB 테이블(상품) 0 | §2-1 'DB 테이블(<무엇>)' 꼴, 0→1(1.3%). 상품 서비스(1)가 아니다 |
| 결제 경로 합계 | 12(16.2%) | 결제가 정답이 아니므로 12/75(16.0%) |
| 어느 축이든 | 최대는 J, P 각 10/75(13.3%), 주문 서비스 7/75(9.3%) | 20% 미만 |

묶음 해석: 바뀐 것은 표 하나의 존재다. 설정값이 아니라 G 가 아니고, 코드나 이미지 배포가 없어 J 가 아니며, DB 계정(O), 잠금(D), 자원(A), 질의 비용(F)은 그대로다. 마이그레이션이 의도한 변경 내용은 맞았는데 적용 대상(환경)이 틀렸으니 P 다.

### 3단계 후보 목록 (실제 기전 × 부품, 숫자 순으로 줄 세움, 버린 이유 포함)

기존 시나리오와 장부는 피할 것 확인에만 썼다. 부품 여덟(commerce PostgreSQL 상품 표, banking Oracle outbox, food MySQL 결제 outbox, food 주문 품목, commerce 장바구니와 주문 품목 시퀀스, food payment 와 외부 PG, banking transfer API, commerce gateway)에 걸쳐 실제 기전으로 후보를 뽑았다. 0 인 묶음 N 과 적은 E, H, I, M, O, Q 의 후보는 rejected 기록(재시도 증폭, 처리 용량, 인증, DNS, 자격 증명)의 벽을 이번에도 넘지 못했다.

| 순위 | 후보 | 원본 사례 | 부품(정답 위치 수), 묶음, 서비스 | 결과 |
|---|---|---|---|---|
| 1 | 운영을 가리킨 로컬 마이그레이션이 commerce PostgreSQL product_schema.products 를 지움 | Resend 2024-02-21 공식 블로그 | DB 테이블(상품) 0, P 9, 쇼핑몰 26(정답 위치 0 이라 허용) | **채택(F23-P)**. 엔진, 도메인, 진입 경로, 범위가 기존 Resend 판과 모두 다르고, 지운 표의 스캔 카운터가 빠지는 DPM 단차를 로컬과 109, 119 조회로 확인했다(§4, §7) |
| 2 | 같은 꼴을 banking Oracle BANKING.OUTBOX_EVENTS 에(이체 트랜잭션 안 outbox 쓰기 실패) | Resend 2024-02-21 | DB 테이블(이체 outbox) 0, P, 은행 25 | 보류(카탈로그 §1): F35-H 와 엔진, 진입 경로(이체 → commerce 정산 502), 수단 모드가 모두 같고 실패한 표만 다르다. CLOB 열의 LOB 세그먼트가 FLASHBACK 이름 복원 계약 밖이라 실행기 확장도 필요. 다음 은행 차례에 범위 차이(거래 내역 정상, 원장 소비 실패)를 근거로 다시 본다 |
| 3 | 같은 꼴을 food MySQL payment_outbox_events 에 | Resend 2024-02-21 | 결제 경로(12), P, 음식배달 23 | 버림(카탈로그 §1): F53-P(payments 지우기)와 엔진, 진입 경로(order → payment 502), 수단 모드가 같고 실패 시점만 PG 승인 뒤로 바뀐다 |
| 4 | 같은 꼴을 food MySQL order_items 에 | Resend 2024-02-21 | DB 테이블(주문 품목) 0, P, 음식배달 23 | 버림(카탈로그 §1): F53-R(orders 지우기)과 같은 서비스, 같은 트랜잭션의 같은 실패 지점(order 500) |
| 5 | 표 이전이 commerce cart_items 시퀀스 상태를 옮기지 않음 | RevenueCat 2022-11-23 공식 블로그 | DB 테이블(장바구니) 0, Q 1, 쇼핑몰 | 버림(원칙 9): 장바구니 품목 행은 checkout 마다 지워져(2026-10-09 cart_items 0행) 되감긴 키와 부딪힐 옛 행이 없다 |
| 6 | 같은 기전을 commerce order_schema.order_items 시퀀스에 | RevenueCat 2022-11-23 | DB 테이블(주문) 1, Q, 쇼핑몰 | 버림(원칙 2): 쇼핑몰이 가장 많은 서비스인데 정답 위치(주문 표 계열)가 0 이 아니다 |
| 7 | 외부 PG 가 더 긴 새 상태값을 보내 food payment 가 VARCHAR(16) 저장 실패(1406) | Cloudflare 2023-10-04 | 결제 경로, L | 버림(rejected 와 같은 원본, 같은 주입) |
| 8 | banking transfer 릴리스가 옛 이체 경로를 지워 commerce 정산이 404 → 502 | (사례 없음) | 은행 이체 서비스 6, J | 버림(원칙 1): 웹 검색 1회, 쓰던 내부 엔드포인트 제거가 원인인 공식 사후 보고 없음(블로그, 지침 문서뿐) |
| 9 | commerce gateway 릴리스가 하류의 hop-by-hop 헤더를 그대로 넘겨 응답 거절 | (사례 없음) | 게이트웨이 0, J | 버림(원칙 1, 3): 공식 사후 보고 없음, 거절은 nginx 와 클라이언트에서 나고 nginx 는 119 에 남지 않음 |
| 10 | 서버 keep-alive 가 호출자 연결 재사용보다 짧은 설정 배포로 연결 재설정 오류 | (사례 없음) | 서비스 설정, G | 버림(원칙 1): 웹 검색 1회, 벤더 문서와 블로그뿐 |
| 11 | food payment 설정 배포가 빈 스키마를 가리켜 결제 1146 | (사례 없음) | 결제 경로, G | 버림(원칙 1): 웹 검색 1회, 설정이 잘못된 DB 를 가리킨 공식 사후 보고 없음(지원 게시판뿐) |
| 12 | 결함 릴리스의 파일 기술자 누수로 'Too many open files' | (사례 없음) | 서비스, J | 버림(원칙 1): 웹 검색 1회, 큰 회사 공식 사후 보고 없음(GitLab 이슈 20601 은 요청 한도 기전, 그 밖은 버그 노트) |
| 13 | 결제 핵심 표의 가용성 문제로 결제 개시 실패 | Tink 2026-02-02 공식 상태 페이지 | DB 테이블, ? | 버림(원칙 1): 원본이 표에 무슨 일이 있었는지(기전)를 밝히지 않아 대응표를 채울 수 없다 |

rejected 기록, 장부 §4-1, §5, 이번 실행에서 막힌 목록에 같은 원본(Resend 2024-02-21)과 같은 주입(commerce PostgreSQL 상품 표 지우기)의 짝이 없다. Resend 는 F53-R, F53-P, F32-P(food MySQL), F35-H(banking Oracle)에서 쓰였고 이번 후보는 엔진, 도메인, 수단 모드, 대상이 다르다.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 엔티티: `commerce/product-service/src/main/java/com/commerce/product/entity/Product.java:7-9` `@Entity @Table(name = "products", schema = "product_schema")`. 표가 그 스키마에서 사라지면 다른 스키마에 같은 이름이 있어도 찾지 않는다(로컬 실측).
2. 둘러보기: `ProductService.java:42-53` `getProducts`(목록, 검색, 분류별), `:56-61` `getVariants`, `:75-86` `getProductWithStock` 이 모두 products 를 먼저 읽는다.
3. 재고 예약: `ProductService.java:89-99` `reserveStock` 이 `findById`(:101-105)로 상품부터 찾는다. 실패하면 inventory 를 부르지 않는다.
4. 오류 처리: `product-service/.../config/GlobalExceptionHandler.java:10-21` 은 ServiceException 만 다뤄 DataAccessException 은 Spring 기본 처리로 500 과 'Servlet.service() ... threw exception' 로그.
5. 호출자: `commerce/order-service/.../client/ProductClient.java:25-54`(5xx 를 ServiceException 으로, @Retry 3회 200ms 지수, @CircuitBreaker productClient, fallback 502 'Product service unavailable for product N'), `OrderService.java:157-161`(checkout 의 품목별 재고 예약, 주문 저장과 결제 `:191` 보다 앞), `api-gateway/.../ProxyService.java:108-131`(하류 오류 상태를 그대로 넘김, 서킷 안 열림).
6. 기동 스크립트: `product-service/src/main/resources/application.yml:18-22` `spring.sql.init.mode: always` — 재시작하면 schema.sql 이 빈 products 를 만들고 data.sql 이 상품 16개를 넣는다(로컬 실측).
7. 프로브: `commerce/k8s/21-product-service.yaml:78-98` startup, readiness, liveness 모두 `/actuator/health`(DB 연결 확인). 표가 없어도 UP(로컬 실측).
8. 인프라: rca-testbed-commerce StatefulSet testbed-postgres(파드 testbed-postgres-0, postgres:16-alpine 16.14, tb-w1, DB commerce). 109 읽기 전용 조회(2026-10-10): products 2,016행, 432kB, 인덱스 products_pkey, idx_products_category, idx_products_name, 소유 시퀀스 products_id_seq, 참조 외래 키 product_variants_product_id_fkey(product_variants 6,048행), 뷰 의존 0, public 에는 pg_stat_statements 뷰 둘뿐, 앱 계정 commerce 슈퍼유저, testbed-product 재시작 0회(40일).

### 로컬 실측 (postgres:16-alpine + pg_stat_statements, 저장소 product-service 1.0.0 jar, 2026-10-10)

- 저장소 init-schemas.sql 과 data.sql, 생성 행으로 109 와 같은 규모(products 2,016, variants 6,048). 평시 목록, 변형 200(준비된 문장 캐시가 서도록 각 8회 이상 호출).
- run(원격 스크립트를 kubectl 대신 docker exec 로 돌린 시험 껍데기): 목록, 변형, reserve-stock 모두 500. 로그 WARN 'SQL Error: 0, SQLState: 42P01', ERROR 'ERROR: relation "product_schema.products" does not exist', ERROR 'Servlet.service() for servlet [dispatcherServlet] ... InvalidDataAccessResourceUsageException: JDBC exception executing SQL [select ... from product_schema.products ...]'. `/actuator/health` UP. 고장 중 preflight 1.
- 통계 단차: run 전 pg_stat_user_tables 합 seq_scan 145, idx_scan 14,531(products 91, 24) → run 뒤 54, 14,507(public.products 0, 0). 고장 중 products 몫은 0 근처에 머문다(reset 직전 앱 백엔드가 쥐고 있던 미반영 카운터 32, 16 이 1초 안에 더해짐, 109 규모에서는 무시할 수준). pg_stat_statements 에는 옮기기, 카운터 초기화, cleanup 모두 기록되지 않음(DDL, pg_stat_reset 행 0).
- cleanup 0(두 번째도 0, 멱등), recovery 0. 되돌린 뒤 목록, 변형 200, products 2,016행, 인덱스 셋, 외래 키, 소유 시퀀스 `product_schema.products_id_seq` 그대로.
- 창 안 재시작: run 뒤 product 를 다시 띄우자 schema.sql 이 빈 products 를 만들고 data.sql 이 16개를 넣어 목록이 200 이 되었다. cleanup 이 그 표(16행, 참조 없음)를 지우고 원본을 되돌려 0, recovery 0.
- 부하 중: 목록 요청 400건을 잇달아 보내는 동안 run 0.9초, cleanup 0.6초, 그 사이 166건 500, 나머지 200.
- 109 에서 같은 원격 스크립트의 preflight(읽기 전용)를 계약 그대로 돌려 0.

## 5. 원인 규정

| 칸 | 값 | 근거 |
|---|---|---|
| `root_cause.target_id` | `commerce-postgres:product_schema.products` | 결함을 가진 곳은 사라진 표다. product, order, gateway 의 코드와 요청은 늘 하던 그대로라 정당하다(원칙 6). 표기는 F33-P(`commerce-postgres:user_schema.auth_tokens`)와 같은 '인스턴스:스키마.테이블' |
| `trigger_target_id` | null | 근본과 계기가 같은 곳(이 표에 대한 잘못된 대상의 마이그레이션) |
| `scoring.partial` | commerce-product, testbed-product:product-service, commerce-postgres | 오류를 낸 서비스(product), 인스턴스까지만 짚은 답(PostgreSQL) |

원본 포스트모템도 "마이그레이션 명령이 운영을 가리켜 표를 지웠다" 를 원인으로 들었다. 같은 층위(사라진 표)로 지목한다. 누가 왜 어느 PC 에서 돌렸는지는 요구하지 않는다(원칙 5).

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 | 기대 |
|---|---|---|
| 감지 | commerce-product, gateway, order 의 오류율(APM 표본, product 는 commerce 최다 트래픽), 새 WARN, ERROR, SEVERE 로그(42P01, relation does not exist, Servlet 예외) 급증 | 이상 탐지 이벤트와 인시던트. 둘러보기(평시 기준선의 65%)와 checkout 이 함께 실패해 신호가 크다 |
| 피해 판정 | 러너: 동반 부하 checkout 5xx 비율 ≥ 0.5 와 checkout 2xx < 0.3, 3틱 | 평시 5xx 0 근처, 고장 중 약 1.0(409 재고 부족, 400 쿠폰 오류만 예외) |
| RCA | product 오류 문장이 사라진 표 이름을 가리킴, 같은 분 DPM 스캔 합 단차와 Top SQL 상품 질의 소멸, 다른 표 정상, 파드와 PostgreSQL 정상, KCM 변경 없음 | §7, §8 |

## 7. 관측 근거 표 (119 실조회, 평시)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 핵심 증거는 전수 수집되는 로그와 DPM DB 통계다.

| 증거 | 표, 칸 | 조회 | 결과 |
|---|---|---|---|
| 근본: 표가 없다는 DB 오류 | CH `lucida_logs_local`(service_name, severity_text, body) | commerce-% 전체에서 `countIf(body LIKE '%42P01%')`, `'%does not exist%'`, `'%product_schema.products%'` | 0, 0, 0(남아 있는 것은 2026-10-03 13:59 UTC 부터) |
| 근본 문장이 로그로 남는 경로 | 같은 표, commerce-cart, commerce-inventory, commerce-product | severity 별 수 | cart WARN 'SQL Error: 0, SQLState: 23505' 1, ERROR 1, SEVERE 12(2026-10-06), inventory WARN 'SQL Error: 0, SQLState: null' 797, product SEVERE 'Servlet.service() ... threw exception' 9,394(2026-10-06 inventory 장애 때). Hibernate SqlExceptionHelper 의 WARN + ERROR 짝과 Tomcat 서블릿 SEVERE 가 commerce 에서 수집된다(product 는 같은 OTel 에이전트 설정, 21-product-service.yaml:59-75) |
| 계기: 지운 표의 스캔 카운터 | VM `dpm.postgresql.sql.seq_scans.raw`, `index_scans.raw`{db_name="commerce"}(DPM 이 pg_stat_user_tables 를 DB 별로 합산, postgres_delta_db.go buildTableStatMetrics) | 현재값, 109 pg_stat_user_tables 의 products 몫 | 119 73,844,669 / 265,646,112, 109 products 31,743,420 / 42,169,455(43%, 16%). 늘기만 하는 누적 카운터라 부하, 롤아웃, 장애로는 내려가지 않는다. 지운 분에 이만큼 내려간다(델타 시리즈는 음수가 0 으로 잘림). cleanup 뒤에는 카운터가 0 에서 다시 쌓이므로, 다시 돌리거나 다시 녹화할 때의 단차는 직전 cleanup 이후 쌓인 양이라 이보다 작다. 단일 표 카운터 초기화는 pg_stat_database.stats_reset 시각을 찍지만 DPM 은 이 값을 수집하지 않는다. 참고: Top SQL 수집기(postgres_topsql.go delta)는 처음 보는 queryid 를 건너뛰어 한 번 실행되는 DROP 문장은 남지 않는다(119 PG 앱 DDL 행 454건은 모두 기동 때마다 되풀이되는 CREATE ... IF NOT EXISTS 24종) |
| 계기: 상품 질의 소멸 | 같은 표 | 1일 행 중 sqlText 에 product_schema.products 가 든 비율 | 23,729 중 8,668(36.5%). 지운 분부터 이 질의들은 성공하지 않아(pg_stat_statements 는 성공한 문장만 센다) 사라진다 |
| 전파: product 트래픽 | CH `otel_traces_local`(service_name, span_kind) | 1시간 commerce SERVER 스팬 | commerce-product 3,784(표본), gateway 4,361, order 192. VM `apm.agent.otel.java.rps{service_name="commerce-product"}` 1h 평균 0.76(표본), error_rate 최대 0 |
| 배제: 롤아웃, 재시작 | CH `kcm_events_local`(namespace, object_kind, object_name, reason) | 7일 rca-testbed-commerce | 수집됨(2026-10-06 testbed-order, testbed-inventory Unhealthy 등). 주입 구간에는 없어야 한다 |
| 배제: DB 크기 | VM `dpm.postgresql.database.used_size{db_name="commerce"}` | 현재값 | 11,986,336,791B. 상품 표 432kB 라 실제 지우기였어도 구별되지 않는다(F35-H 와 달리 크기 흔적에 기대지 않음) |
| 배제: DB 세션, 잠금 | VM `dpm.postgresql.session.blocked_session`, `active_session` | 평시 | 표 지우기는 잠금을 남기지 않는다(옮기기 ACCESS EXCLUSIVE 는 lock_timeout 10초 안, 로컬 1초 안) |

계기의 흔적은 셋이다: 오류가 시작된 분에 DPM 스캔 합이 지운 표 몫만큼 내려가고, 같은 분부터 Top SQL 의 상품 질의 행이 사라지며, 오류 문장이 사라진 표 이름을 직접 가리킨다. 그 시각에 다른 변경(롤아웃, 재시작, 설정)이 없다. 인공 지연은 없다.

원칙 8 참고: 실행기는 F33-P 처럼 파드 안 psql(앱 계정)로 접속해 DPM 세션 표본에 psql 세션이 잡힐 수 있다. 시나리오 id 는 argv, 원격 스크립트, SQL 어디에도 없다(테스트). 옮기기, 카운터 초기화, 되돌리기는 모두 pg_stat_statements 가 기록하지 않는 세션에서 돌아 Top SQL 에 남지 않고, public.products 라는 이름은 119 어디에도 수집되지 않는다(평가 확인).

## 8. 감별

- must_support: product WARN 'SQL Error: 0, SQLState: 42P01', ERROR 'ERROR: relation "product_schema.products" does not exist', SEVERE 'Servlet.service() ... InvalidDataAccessResourceUsageException ... product_schema.products'(조회마다), 같은 분 dpm.postgresql.sql.seq_scans.raw, index_scans.raw 의 단차(그 표의 누적값, 첫 실행 기준 약 43%, 16%)와 그 분부터 Top SQL 상품 질의 소멸, gateway 둘러보기 500 과 checkout 502 'Product service unavailable for product N', 로그인, 장바구니, 가격 200, KCM 변경 없음, product 와 PostgreSQL Ready.
- must_rule_out: product 배포, 설정, 파드 소실(롤아웃, 재시작 없음), F12-H(product CPU 한도: 느려짐과 시간 초과), F23-R(재고 소진 409), PostgreSQL 다운, 과부하, 잠금(F25-H, F33-P, F06-H), 다른 DB 표 지우기(F53-R, F53-P, F32-P, F35-H), 부하.
- contrast_with: F23-R, F12-H, F53-R, F33-P, F25-H(정답지 `related_scenarios`).

가르는 관측 근거 한 줄: 오류가 PostgreSQL 이 낸 42P01 이고 그 문장이 표 하나를 가리키며, 같은 분 DPM 스캔 합이 그 표 몫만큼 내려가고, 같은 DB 의 다른 표 질의와 모든 파드, 세션이 정상이다.

## 9. 러너 판정 조건과 강도, 부하

- 주입: db.ddl(PostgreSQL 표 지우기 모드) 1단 고정(`approved-fixed-f23-p`). 강도 축이 없다: 표가 없으면 그 표를 부르는 문장은 요청량과 무관하게 100% 실패한다. min_hold 15m, settle 30s, timeout 20m, max_injection_duration 25m.
- 동반 부하: load.north_south `commerce surge.js` 3rps(여정 절반이 checkout, 나머지 둘러보기와 장바구니), ramp 2m, hold 21m, entry 30080(F33-P 와 같은 스크립트와 세기). checkout 은 flagship 상품 16개 중에서 담아 상품 목록에 기대지 않으므로 둘러보기가 실패해도 checkout 표본이 이어진다. 부하 상한 180 의 2%.
- 피해 계산(원칙 9): 실패가 시간 초과가 아니라 즉시 나는 SQL 오류라 서킷브레이커는 실패를 줄이지 않고 빠르게 만들 뿐이다(order productClient 서킷 열림 → 502). 상주 commerce 부하(둘러보기 65%, checkout 8%)도 함께 실패한다.
- success: checkout 5xx ≥ 0.5, checkout 2xx < 0.3, 3틱.
- must_rule_out: achieved_rps < 1, PostgreSQL 파드 NotReady, product 파드 NotReady. 창 안 product 재시작은 Spring 기동 동안 NotReady 가 30초를 넘어 product-pod-gone(2틱)으로 배제된다(러너의 컨테이너 재시작 관측 허용 목록에는 product 가 없어 재시작 수는 따로 보지 않는다). 재시작하면 기동 스크립트가 빈 표와 시드 상품 16개를 만들어 flagship checkout 도 되살아나므로 성공 조건이 서지 않고, cleanup 이 그 빈 표를 지운다(로컬 실측).
- abort: entry_status == 0(nginx, gateway 나 노드가 죽음). 표 지우기는 파드를 건드리지 않아 gateway 가 500, 502 로 답한다.
- recovery: target_health 200, product Ready, PostgreSQL Ready, 기준선 쓰기 실패율 < 0.1, 2틱, 10m.
- cleanup: 기록되지 않는 세션에서 `ALTER TABLE public.products SET SCHEMA product_schema`(창 안 재시작으로 생긴 1000행 미만, 참조 없는 products 가 있으면 먼저 지움, 아니면 사람에게 남기고 3), 원래 표가 있고 public 에 남은 것이 없는지 확인, 부하 종료.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Resend 2024-02-21, 공식 블로그)와 요소별 대응표, 기전이 원본과 같다 (§2). 원본 행을 지키려고 표를 옮기되, 실제 지우기가 관측 데이터에 남기는 것(표 부재, 스캔 카운터 단차)을 그대로 남긴다
- [x] 분류 장부를 갱신했고 묶음 P(9→10, 13.3%), 정답 위치 DB 테이블(상품)(0→1), 결제 경로 12/75(16.0%), 서비스 쇼핑몰 26→27(최다지만 정답 위치 0 이라 완화 규칙)과 고른 이유를 적었다. 어느 축도 20% 미만 (§3)
- [x] 근본 원인 위치가 인프라 지점(product_schema.products, 109 사전 조회)과 `file:line`(Product.java:7-9, ProductService.java:89-105)으로 확인됐다 (§4)
- [x] 근본 원인의 흔적이 119 에 남는 경로를 조회했다: lucida_logs_local 의 SqlExceptionHelper WARN + ERROR 와 서블릿 SEVERE 꼴(commerce 수집 확인), 42P01 평시 0건, VM dpm.postgresql.sql.seq_scans.raw, index_scans.raw 와 109 products 몫(43%, 16%), Top SQL 상품 질의 비중 36.5% (§7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: 로그 전수, DPM DB 통계(스캔 합) (§7)
- [x] 계기 흔적: 같은 분 DPM 스캔 합 단차와 Top SQL 상품 질의 소멸, 오류 문장이 사라진 표 이름을 가리키며, 그 시각에 롤아웃, 재시작, 설정 변경이 없다(KCM). 인공 지연 없음 (§7)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다: "commerce PostgreSQL 의 product_schema.products 가 없다" 는 오류 문장과 같은 분 스캔 합 단차에서 바로 나온다 (§5)
- [x] 정답지 세 칸이 원칙 6대로 채워졌다 (§5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다 (§6)
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: argv, 원격 스크립트에 id 없음(테스트), 옮기기와 카운터 초기화는 Top SQL 에 기록되지 않음 (§7)
- [x] 부하 상한과 서킷브레이커를 고려해 피해가 실제로 날 계산이 있다: 즉시 실패라 요청량과 무관하게 100%, 동반 3rps (§9)
