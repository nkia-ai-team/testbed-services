# Batch1 감사 — F01~F03 계열 (12개)

대조: fault-surface-{commerce,food-delivery,core-banking}.md / registry(controllers·metadata)·catalog · 실코드 file:line.

| id | 앵커패턴 | answer-key 정합 | 판정 | 한 줄 근거 |
|---|---|---|---|---|
| F01-R pg-lock-checkout | P3 (무제한 pessimistic row-lock) | 정합 | ✅ | inventory reserve=`@Lock(PESSIMISTIC_WRITE)` no lock_timeout; 주입은 외부 태그락(product_id=1,600s)이나 전파(reserve 직렬화→order timeout)는 실코드. blocking session/외부·host 정상 evidence 일치. InventoryRepository.java:17-19 |
| F01-H commerce-pg-429 | P4-인접(PG hop CB 없음)/외부의존 | 정합 | ✅ | mock 429 → PgApiClient 비2xx→`ServiceException(502)` fast-reject→checkout 502. DB blocking 0 evidence 일치. commerce PgApiClient.java:38, CB/Retry 부재 |
| F01-P oracle-lock-cross-domain | P7+P3 (cross-domain+row-lock) | 정합 | ✅ | banking Oracle accounts 외부 FOR UPDATE 락(client_id 태그)→transfer 무한대기(NOWAIT 없음)→commerce payment 10s timeout→checkout 5xx. cross-domain trace evidence 강. banking FS-1/FS-5, TransferService.java:63-71 |
| F01-G absorbed-pg-delay | 가드레일(장애 아님) | 정합 | ✅ | 짧은 delay pulse<order→payment read15s → retry조차 미발동, dup 없음. "no incident" 정답 타당. adaptive load |
| F02-R pg-product-index-drop | 패턴외(DDL/slow-query) | **의심** | 🟡 | 드롭 대상 `idx_products_name`(btree)은 검색쿼리 `findByNameContainingIgnoreCase`=`name ILIKE '%q%'`(leading wildcard)를 **원래 못 탄다** → 드롭해도 plan 불변, 게다가 products 2016행. "실행계획/rows scanned 변화" evidence 미실현 가능. ProductRepository.java:12 |
| F02-H commerce-storage-io | 패턴외(인프라 host IO) | 미정의 | 🟡 | blocked, metadata answer-key **부재**. host.stress(PG PVC) 인프라 결함 자체는 정당하나 정답 근거 미정의+PVC backing device 전제 미충족 |
| F02-P mysql-menu-index-drop | 패턴외(DDL) | **불일치** | ❌ | 드롭 대상 `idx_menus_category`(menus.category_id)를 쓰는 쿼리가 **없음** — 유일 메뉴경로 `findByRestaurantId`는 idx_menus_restaurant 사용, category 필터 쿼리 부재(엔드포인트 `/{id}/menu`만). categoryId는 응답 필드일 뿐. "카테고리별 조회 지연" 인과 사슬 단절→증상 미발현. MenuRepository.java:12, RestaurantController.java:42 |
| F02-G batch-heavy-sql-only | 가드레일(추정) | 미정의 | 🟡 | blocked, metadata **부재**, db.workload no-load. 정답(무영향 흡수?) 미정의+batch trigger/session identity 전제 미충족 |
| F03-R food-payment-connection-leak | P2-인접(leak→pool 고갈) | 미정의 | 🟡 | blocked, metadata **부재**. app.release "fault build" 전제=미빌드 fault 이미지 의존(자연 코드결함 아님, 주입산물). 정답 근거 없음 |
| F03-H order-thread-saturation | Tomcat 워커 고갈(P군 외, 스레드풀) | 정합(자기완결) | 🟡 | 기제(Tomcat 200 미튜닝)는 실측이나 트리거가 **목적성 sleep 엔드포인트** `GET /api/orders/reports/render?delayMs`(Thread.sleep, 코드주석 "재현 유일 경로")=인위 주입 훅. F03-P와 구분 evidence는 정밀. OrderController.java:66-71 |
| F03-P small-hikari-checkout | P2(pool 고갈)이나 **env 인위축소** | 정합하나 인위 | 🟡 | payment 풀을 `SPRING_DATASOURCE_HIKARI_MAXIMUM_POOL_SIZE=5→3→2` env로 축소(k8s.env). 자연 고갈 아님—미설정 시 기본10. 원인=주입한 오설정. answer-key도 "undersized pool" 자인 |
| F03-G high-pool-usage-no-impact | 가드레일(장애 아님) | 정합 | ✅ | load.north_south만, 임계 아래 풀 사용률 상승. pending/timeout/err 정상=정답. 인위주입 없음 |

## 집계
- ✅ 탄탄: 5 (F01-R, F01-H, F01-P, F01-G, F03-G)
- 🟡 의심: 6 (F02-R, F02-H, F02-G, F03-R, F03-H, F03-P)
- ❌ 근거없음: 1 (F02-P)

## 최문제 Top 3
1. **F02-P ❌** — 드롭 인덱스(idx_menus_category)를 참조하는 쿼리 자체가 없음. 인과사슬 단절, 증상 미발현.
2. **F02-R 🟡** — btree idx_products_name은 ILIKE '%q%' contains 검색을 원래 못 탐(+2016행). 드롭 무효과 가능. F02-P와 동일 계열 결함(존재하나 미사용 인덱스 드롭).
3. **F03-P 🟡** — 풀을 env로 인위 축소(2까지). 자연 고갈 아니라 주입된 오설정이 root. (F03-H도 sleep 엔드포인트 인위훅으로 동류 우려.)
