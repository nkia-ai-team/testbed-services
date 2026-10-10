---
title: F42-P 설계 시트 (food restaurant-service 를 인기 메뉴를 주문 원장에서 바로 세는 릴리스로 롤아웃)
status: Draft
owner: project
last_reviewed: 2026-10-10
tags:
  - scenario
  - design
  - release
  - slow-query
summary: food restaurant-service 를 결함 있는 새 릴리스(food-delivery-restaurant:1.5.0, fault-images/f42-p 패치로 만든 별도 태그)로 롤아웃하면, 인기 메뉴 순위를 실시간으로 바꾸려고 1시간 배치가 채우는 캐시 표 대신 요청마다 그 가게의 최근 7일 주문을 order_items, orders 조인으로 세게 되어, 인기 메뉴 요청마다 수 초짜리 집계가 돌고 food MySQL(CPU 0.5, 버퍼 풀 128MB)이 포화되며 restaurant 연결 풀이 말라 가게 화면과 주문 생성이 실패하는 시나리오. 원본은 GitHub 2025-01-09 배포가 들여온 질의가 주 DB 서버를 포화시킨 장애(F42-R, F33-H 와 같은 원본).
---

# F42-P 설계 시트

## 1. 요약

food restaurant-service 의 새 릴리스 1.5.0 은 가게 화면의 인기 메뉴 순위가 한 시간 늦는 문제를 고치려고, `GET /api/restaurants/{id}/popular-menu` 가
1시간 배치(PopularMenuBatch)가 채우는 캐시 표 `menu_popularity_summary`(88행)를 읽지 않고 요청마다 그 가게의 최근 7일 주문을 바로 세게 했다.
질의는 배치가 쓰던 집계의 가게 한 곳 판이다(`order_items JOIN orders JOIN menus WHERE m.restaurant_id = ? AND o.created_at >= ? GROUP BY 메뉴`).
응답 모양, 다른 끝점, health, 스키마, 인덱스는 그대로라 새 파드는 Ready 가 되어 옛 파드를 대신한다. 가게 한 곳이어도 이 질의는 그 가게의 주문 품목을
메뉴 색인으로 훑고 날짜를 거르려고 품목마다 orders 를 기본 키로 찾는다. 109 MySQL 이 한가할 때 4.05, 4.39, 7.47초가 걸렸고, 한 번에 약 2만 9천 쪽(약
450MB)을 디스크에서 읽었다(orders 364MB, order_items 296MB 가 128MB 버퍼 풀에 다 들어가지 않는다). 둘러보기 트래픽의 인기 메뉴 요청(동반 부하 포함 초당
약 2.5건)은 CPU 0.5 짜리 MySQL 이 이 집계를 처리하는 양의 열 배쯤이라, restaurant Hikari 연결 10개가 모두 실행 중인 집계에 묶이고 가게 상세, 메뉴, 인기 메뉴가
연결을 3초 기다리다 500 이 된다. 주문마다 가게와 메뉴를 restaurant 에 묻는 order 는 재시도와 서킷 끝에 502 로 실패한다.

비유: 식당 안내판의 "이번 주 인기 메뉴" 를 매시간 한 번 직원이 장부를 세어 적어 두었는데, 새 안내판은 손님이 볼 때마다 주방 직원이 지난 일주일 주문서 더미를
처음부터 다시 센다. 손님이 몇 명만 들여다봐도 주방 직원 열 명이 모두 주문서를 세느라, 정작 메뉴판을 가져다주거나 주문을 받을 사람이 없다. 장부(표와 색인)는
멀쩡하고 고칠 곳은 세기를 시킨 새 안내판(릴리스)이다.

## 2. 원본 사례

- **GitHub, 2025-01-09** (공식 월간 가용성 보고, 2025년 1월):
  https://github.blog/news-insights/company-news/github-availability-report-january-2025/
  - 01:26~01:56 UTC 많은 서비스가 광범위하게 중단되어 사용자가 여러 기능에서 서버 오류를 받음. 원인은 "a deployment which introduced a query that
    saturated a primary database server". 오류율 평균 6%, 갱신 요청 최대 6.85%. 내부 도구와 대시보드로 문제 질의의 출처를 찾아 배포를 되돌려 완화,
    대응 시작부터 문제 질의를 찾기까지 14분. 재발 방지로 배포 전에 문제 질의를 잡는 도구에 투자. 질의 내용, 바꾼 이유, DB 종류는 적혀 있지 않다.
  - 자료 문서 `ref-real-world-incidents.md` M2 에 이미 있다. F42-R(은행 transfer, Oracle), F33-H(food dispatch 목록 COUNT)가 같은 원본을 쓴다.
    이 후보는 같은 원인 꼴을 다른 서비스(food restaurant)와 다른 질의(요청 경로의 조인 집계)에 둔 것이다(카탈로그 관계 P).
- 원본이 질의 내용과 바꾼 이유를 밝히지 않으므로, 재구성은 원본이 말한 고리("배포가 들여온 질의 하나 → 주 DB 서버 포화 → 그 DB 를 쓰는 여러 기능의 서버
  오류 → 질의의 출처를 찾아 배포를 되돌림")만 따른다. 결함의 구체 모양(인기 메뉴를 캐시 표 대신 요청마다 주문 원장에서 셈)과 그 동기(순위가 한 시간 늦던
  문제)는 이 테스트베드의 코드에서 고른 것이고 원본에서 온 것이 아니다.
- 처음 설계는 MIT Open Learning 2026-03-24(조회 수 집계를 주 질의에 넣은 릴리스) 를 원본으로 썼다. 평가에서 원본의 고리는 "집계가 임시 공간과 저장
  공간을 채움 → DB 내부 적체 → 되돌린 뒤에도 수동 개입(복제본 삭제, 긴 질의 종료)이 있어야 회복" 이고 동기도 "느린 실시간 집계를 빠르게" 라서, 막힌
  자원(임시 디스크 대 CPU, 버퍼 풀), 회복 꼴(수동 개입 대 되돌리면 1~2분), 동기(반대)가 모두 다르다는 지적을 받아 뺐다.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 배포(새 버전) | restaurant-service 를 릴리스 food-delivery-restaurant:1.5.0 으로 롤아웃(KCM ScalingReplicaSet, 새 파드 Pulled 이벤트에 태그가 남음) |
| 원인이 된 결함 | 배포가 들여온 질의 하나가 운영 규모에서 비쌈(내용 미공개) | 인기 메뉴 요청마다 그 가게의 주문 품목을 orders 와 조인해 세는 집계(운영 규모 품목 약 258만, 주문 약 173만에서 4~7.5초) |
| 전파 경로 | 그 질의가 주 DB 서버를 포화 → 같은 DB 를 쓰는 여러 기능에서 서버 오류 | 집계가 food MySQL 의 CPU(0.5)와 버퍼 풀(128MB) 밖 디스크 읽기를 포화 → restaurant Hikari 10개가 집계에 묶임 → 가게 화면 연결 대기 500, health 실패로 readiness 이탈 → order 가 가게, 메뉴 조회 실패로 502. 같은 MySQL 의 다른 food 질의도 느려짐 |
| 사용자 증상 | 여러 기능에서 서버 오류(평균 6%) | 가게 화면(인기 메뉴, 상세, 메뉴) 500 과 시간 초과, 새 주문 대부분 502 |
| 원본의 탐지 경로 | 내부 도구와 대시보드로 문제 질의의 출처를 찾음 | KCM 롤아웃과 이미지 태그, DPM 세션(긴 활성 세션의 출발 IP 가 restaurant 파드), DPM 지표(활성 세션, 데이터 파일 읽기, 검사 행 수, 버퍼 풀 적중률), restaurant Hikari 로그, order 오류 로그, restaurant 스팬 지연 |
| 완화와 복구 | 배포 되돌림 | cleanup 이 restaurant 이미지를 매니페스트 이미지로 되돌림(캐시 표를 읽는 버전). 끊고 떠난 집계가 MySQL 안에서 끝나는 1~2분 뒤 회복 |

기전 동일성: 원본과 재구성 모두 "새 버전이 들여온 질의 → 운영 데이터 규모에서 DB 서버 포화 → 그 DB 를 쓰는 기능들이 서버 오류 → 배포를 되돌려 복구"
고리다. 원본이 밝히지 않은 질의 내용은 한 서비스, 한 끝점의 집계 하나로 작게 두었다. 스키마, 인덱스, 설정, 자원 한도는 바꾸지 않는다.
임시 디스크를 채우는 질의 꼴은 MySQL 데이터가 노드 디스크와 같은 볼륨이라 노드 DiskPressure 로 원인이 가려져(rejected 의 MIT 임시 공간 행, F10-R parked)
고르지 않았다(109 실측 이 집계의 Created_tmp_disk_tables 증가 0).

## 3. 숫자 근거 (2026-10-10, `scenario-stats.py`, 정식 43 + 후보 22 = 65 → 66)

- 서비스(정식 + 후보): commerce 26, core-banking 20, **food-delivery 19(가장 적음)** → 20
- 묶음: **J 7 → 8**(66 중 12.1%), A 7, B 7, D 7, G 7, C 6, L 6, P 6, F 3, E 2, H 2, K 2, I 1, M 1, O 1, N 0. 20% 상한(13.2) 아래. F 로 세도 3 → 4(6.1%)
- 정답 위치: **가게 서비스 1 → 2**(3.0%, §2-1 에 이미 있는 말). 최다는 주문 서비스 7(10.6%)
- 결제 경로 합계 11(16.7%) 그대로
- 부품 지도: food restaurant 2(F49-R 가게 서비스, F48-R 은 DB 테이블(인기 메뉴 집계)로 따로 센다). MySQL 7 은 정답 위치로는 표, 인스턴스로 나뉘어 이 후보와 무관
- 왜 이 후보인가: 가장 적은 서비스(food)이고, 가장 흔한 현실 원인(새 버전 배포, Google 37%)이며, 정답 위치가 적은 쪽(가게 서비스 1)이다. 숫자 순위가 더 높은
  후보(0 인 묶음 N, 0 인 정답 위치 메시지 브로커, 알림, 게이트웨이, 비밀값)는 §10 후보 목록처럼 원칙 1, 7 에서 막히거나 숫자 순위로 뒤로 밀렸다.
  F48-R(인기 메뉴 캐시 표 소멸)과 같은 끝점이 실패하지만 원인과 오류 꼴이 다르고, F33-H(dispatch 목록 COUNT)와 MySQL 포화라는 겉 증상이 닮았지만 정답 서비스가
  달라, 관제 AI 가 롤아웃 이력과 DPM 세션의 출발 파드를 갈라 봐야 맞힌다.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 계기: `rca-testbed-food` Deployment `testbed-restaurant` 이미지 `food-delivery-restaurant:latest` → `food-delivery-restaurant:1.5.0`
   (`food-delivery/k8s/21-restaurant-deploy.yaml:27`, imagePullPolicy Never, replicas 1 `:9`, nodeSelector tb-w3 `:18-19`, 기본 전략 maxSurge 25%,
   readinessProbe `/actuator/health` 10초 간격 3초 제한 3회 `:73-79`, livenessProbe 15초 간격 3초 제한 5회 `:80-86`).
2. 결함(릴리스 1.5.0): `scripts/scenarios/fault-images/f42-p/restaurant-service.patch:8-19`(MenuRepository.rankRecentMenus, 배치 집계
   `aggregatePopularMenus` 의 가게 한 곳 판), `:60-76`(RestaurantService.getPopularMenu 가 캐시 표 대신 7일 창으로 rankRecentMenus 를 부름),
   `:42-58`(창 길이 `menu.popularity.lookback-days`, 기본 7, 배치와 같은 설정). 매니페스트 버전은
   `food-delivery/restaurant-service/src/main/java/com/fooddelivery/restaurant/service/RestaurantService.java:43-52`(캐시 표 조회),
   배치는 `PopularMenuBatch.java:37-56`(1시간마다 전체 가게 집계, 109 실측 1회 약 5.7초 = digest f5cfd640… 35회 200.7초).
3. 비용: 질의 계획(109 EXPLAIN): menus `idx_menus_restaurant` ref(4~5행) → order_items `fk_order_items_menu` ref(추정 83,190/메뉴) → orders PRIMARY
   eq_ref(created_at 걸러냄) → Using temporary; Using filesort. 실행 4.05초(가게 21), 4.39초(같은 질의 2회째), 7.47초(가게 1, 메뉴 5개). 가게 1 한 번에
   `Innodb_buffer_pool_reads` +28,668 쪽, `Handler_read_key` +162,902, `Created_tmp_disk_tables` +0(109 SHOW GLOBAL STATUS 전후 차, 2026-10-10 07:3x UTC).
   표 크기: order_items 2,580,746행(138+158MB), orders 1,734,537행(114+250MB), 버퍼 풀 128MB.
4. 포화: MySQL CPU 한도 500m, 메모리 1Gi(`food-delivery/k8s/10-mysql.yaml:45-50`, 프로브 없음). 집계 하나가 한가한 MySQL 을 수 초 차지하므로 처리 가능량은
   초당 약 0.2~0.25건이다.
5. 풀: restaurant Hikari maximum-pool-size 10, connection-timeout 3000ms(`food-delivery/restaurant-service/src/main/resources/application.yml:12-16`).
   가게 상세, 메뉴, 인기 메뉴, health 의 DB 확인이 모두 같은 풀을 쓴다.
6. 호출자: `food-delivery/order-service/src/main/java/com/fooddelivery/order/service/OrderService.java:58-83`(createOrder 가 가게와 메뉴를 먼저 조회),
   `order/client/RestaurantClient.java:38,67`('Failed to fetch restaurant', 'Failed to fetch menu for restaurant' ERROR), restaurant 읽기 시간 제한 5초
   (`order-service/src/main/resources/application.yml:51-54`), 재시도 3회와 서킷브레이커(같은 파일 resilience4j restaurant). loadgen 주문 여정은 메뉴 조회가
   200 일 때만 주문을 보낸다(`food-delivery/loadgen/script.js:119-150`).
7. 영향 밖: 스키마, 인덱스, 캐시 표, MySQL 설정, 다른 서비스 이미지와 설정.

## 5. 원인 규정 (원칙 6)

- `root_cause.target_id`: `food-delivery-restaurant`(target_kind container). 결함을 가진 곳이자 되돌려야 재발이 막히는 곳은 요청 경로에 집계를 넣은 restaurant
  새 릴리스다. 손님의 인기 메뉴 요청 A 는 평시와 같은 끝점, 같은 매개변수로 정당하고, 그것을 받은 B(restaurant 1.5.0)의 로직이 요청마다 주문 이력을 세므로
  원칙 6 의 "B 의 로직이 잘못" 경우다. 원본 포스트모템도 질의를 들여온 배포를 지목해 되돌렸다(같은 층위). 결함 이미지 규칙 7(근본은 그 서비스의 새 버전).
- `trigger_target_id`: 없음(계기인 롤아웃과 근본이 같은 곳).
- `scoring.partial`: `food-mysql`, `food-mysql:fooddelivery.order_items`, `food-mysql:fooddelivery.orders`(포화된 DB 와 집계가 훑는 표),
  `food-delivery-order`, `food-order`, `testbed-order`(주문 실패라는 증상을 낸 곳).
- 코드 줄을 맞히라고 요구하지 않는다(원칙 5). "restaurant 롤아웃 직후 MySQL 이 포화되고 긴 활성 세션이 새 restaurant 파드에서 오는데, 스키마와 다른 서비스는
  그대로" → restaurant 릴리스는 관제 데이터(KCM, DPM 세션과 지표, 로그)로 낼 수 있는 결론이다.

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 | 기대 |
|---|---|---|
| 감지(lucida-next) | restaurant, order log-anomaly(Hikari 고갈, 가게, 메뉴 조회 실패), MySQL stream-anomaly(활성 세션, 데이터 파일 읽기, 검사 행 수), restaurant, order 서버 스팬 오류율과 지연, KCM Unhealthy | 같은 MySQL 포화와 풀 고갈 꼴의 F33-R 실행(F33-R-run-73fed92a)이 food 인시던트 12건으로 승격됐다 |
| 피해 판정(러너) | 동반 부하 k6 문서의 인기 메뉴 비정상 비율 ≥ 0.8 (3틱) | 평시 0 근처(119 표본 3일 15,015건 중 5xx 1건) |
| 원인 설명(RCA) | KCM 롤아웃 이벤트와 이미지 태그, DPM 세션(출발 IP, queryTime), DPM 지표, 전수 로그 | §7 |

## 7. 관측 근거 (119 실조회, 평시)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 핵심 증거는 KCM 이벤트, DPM 세션(폴링), VM DPM 지표, 전수 로그다.

| 증거 | 119 표와 칸 | 조회 | 결과(2026-10-10) |
|---|---|---|---|
| 계기: 롤아웃과 이미지 태그 | CH `lucida.kcm_events_local`(object_name, reason, body) | `object_name LIKE 'testbed-restaurant%'` 전체 기간, reason 별 수 | Unhealthy 70건(2026-08-24 ~ 10-08, 'Readiness probe failed ... context deadline exceeded')뿐. Pulled, ScalingReplicaSet 0건(돌던 파드는 60일 된 :latest, 109 kubectl). 같은 수집기가 dispatch 의 롤아웃 Pulled 49건을 담았으므로 장애 때 'food-delivery-restaurant:1.5.0' 이 처음 나타나야 한다 |
| 근본: 긴 활성 세션의 출발지 | CH `lucida.dpm_session_local`(body JSON 의 hostname, queryTime(ms), state, sessionType) | engine='mysql', sessionType='active', 최근 3일 hostname 별 수, 최대, 평균 queryTime | restaurant 파드 10.244.2.218: 39건, 최대 1,398.69ms, 평균 64.7ms. 장애 때는 새 restaurant 파드 IP 의 활성 세션이 매 수집마다 여러 개, queryTime 수 초~수십 초여야 한다(파드 IP 는 KCM, 스팬의 k8s_pod_name 과 맞춘다) |
| 근본: MySQL 포화 모양 | VM `dpm.mysql.session.active_session`, `dpm.mysql.instance.data_file_read_bytes`, `dpm.mysql.sql.row_examined_count`, `dpm.mysql.buffer_pool.hit_ratio` | 최근 6시간 평균, 최대 | active_session 평균 1.04, 최대 2. data_file_read_bytes 평균 189,414, 최대 29,175,688. row_examined_count 평균 4,834, 최대 34,126. buffer_pool.hit_ratio 평균 99.958%. 장애 때 활성 세션 10 안팎, 디스크 읽기와 검사 행 수 급증, 적중률 하락 |
| 배제: MySQL 재시작 | 러너 kubernetes 관측 `kubernetes.container_uptime_seconds`(testbed-mysql, mysql), 109 cgroup | kubectl 상태 startedAt, memory.stat | 2026-10-08T20:33Z 부터 떠 있음(약 35시간), anon 877MiB. 실행 중 uptime 이 1800초 아래로 내려가면 재시작이다(KCM 의 testbed-mysql Started 이벤트로도 확인, 30일 26회) |
| 감별: 스키마 그대로 | VM `dpm.mysql.database.table_count`, `index_count`{db_name="fooddelivery"} | 최근 2시간 | 표 16, 인덱스 31 고정(F48-R 은 16→17, F33-R 은 31→30) |
| 감별: DPM Top SQL 의 맹점 | CH `lucida.dpm_topsql_local` | 배치 집계 digest(`f5cfd640…`, performance_schema 35회 200.7초)와 설계 실측 digest(`8ef0dd4a…`) | 0행(평시에도 이 조인 집계는 Top SQL 에 담기지 않음. DPM 이 대신 낸 EXPLAIN 문장만 2de2397e… 로 남음). 그래서 근본 증거로 쓰지 않는다 |
| 전파: restaurant 풀 고갈 | CH `lucida.lucida_logs_local` | `service_name='food-delivery-restaurant' AND body LIKE '%Connection is not available%'` 7일 일별 | 10-04 12건, 10-06 48건, 10-08 93건(다른 시나리오 실행 날), 나머지 0. 장애 때 분당 수십 건 |
| 전파: order 의 가게, 메뉴 조회 실패 | 같은 표 | `service_name='food-delivery-order' AND (body LIKE 'Failed to fetch restaurant%' OR body LIKE 'Failed to fetch menu%')` 7일 일별 | 10-09 500건(다른 실행)뿐, 나머지 0 |
| 전파: 인기 메뉴 지연 | CH `otel_traces_local`(service_name, span_name, duration_ns) | restaurant 서버 스팬 3일 끝점별 | popular-menu 15,015건 p50 1.6ms, p99 66.1ms, 5xx 1. 상세 69,314건 p50 1.6ms, 메뉴 87,019건 p50 1.8ms. 장애 때 초 단위(보조, 표본) |

109 실측(2026-10-10 07:2x~07:3x UTC, 읽기 전용, 러너 `/api/active` 비어 있음): EXPLAIN 과 실행 시간은 §4. 같은 문장을 릴리스 jar(패치를 얹어 104 에서 빌드)로
104 에서 띄워 109 MySQL 에 붙여 `GET /api/restaurants/21/popular-menu` 를 한 번 부르니 200, 6.01초, 응답 모양은 매니페스트와 같은 `[{menuId, menuName, orderCount}]`
였고, 같은 jar 의 메뉴 조회는 0.07초였다(jar 는 바로 내렸다). 패치를 얹은 모듈의 `mvnw -pl restaurant-service -am test` 통과, origin/main 에 `git apply --check` 통과.
109 docker 의 `food-delivery-restaurant:1.5.0` 은 배포 단계에서 `build.sh f42-p` 로 만든다.

## 8. 감별

- must_support: 정답지 `must_support` 4개(롤아웃과 태그, DPM 세션의 출발지와 MySQL 포화 지표, restaurant 풀 고갈과 order 가게, 메뉴 조회 실패와 인기 메뉴 지연,
  스키마와 캐시 표 그대로이고 다른 서비스 변경 없음).
- must_rule_out: F48-R(캐시 표 소멸), F36-R(가게 표 스키마), F49-R(응답 계약), F33-R, F33-H(dispatch 쪽 포화), F10-H, F25-H(디스크 IO, DB 다운), 부하만 늘어난 것,
  restaurant 프로브 오설정이나 재시작 자체(F05-H 꼴).
- contrast_with: F33-H, F42-R, F48-R, F33-R, F49-R, F36-R.
- **같은 끝점, 다른 정답(F48-R 대 F42-P)**:

| 관측 | F48-R(캐시 표를 취소된 백필이 치움) | F42-P(릴리스가 요청마다 집계) |
|---|---|---|
| 인기 메뉴 오류 | 즉시 500, 'Table ... menu_popularity_summary doesn't exist'(1146) | 느리거나 Hikari 연결 대기 3초 뒤 500, 1146 없음 |
| DPM table_count | 16 → 17 | 16 그대로 |
| MySQL | 한가 | 활성 세션 10 안팎, 디스크 읽기 급증, 긴 세션의 출발지가 restaurant 파드 |
| 다른 끝점과 주문 | 정상 | 가게 상세, 메뉴 500, 주문 502 |
| KCM | 롤아웃 없음 | testbed-restaurant ScalingReplicaSet, Pulled 'food-delivery-restaurant:1.5.0' |

- **같은 겉 증상(MySQL 포화), 다른 정답 서비스(F33-H 대 F42-P)**: F33-H 는 dispatch 롤아웃, dispatch 풀 'Connection is not available', order 'Failed to check
  dispatch capacity' 와 503 이고, F42-P 는 restaurant 롤아웃, restaurant 풀 고갈, order 'Failed to fetch restaurant / menu' 와 502 다. DPM 세션의 긴 질의 출발 IP 가
  dispatch 파드냐 restaurant 파드냐로도 갈린다.
- restaurant 재시작: liveness(/actuator/health, 15초 간격, 3초 제한, 5회 실패면 재시작)가 DB 확인 때 풀 연결을 3초 기다린다. 평시에도 readiness 시간 초과가 70건
  있었고, 상주 포화 중에는 Unhealthy, Killing 이 나올 수 있다. 재시작은 결과다: 프로브 설정은 그대로이고, 재시작마다 클라이언트가 끊고 떠난 집계는 MySQL 안에서
  끝까지 돌아 포화를 잇는다. 재시작만 보고 프로브 오설정(F05-H 꼴)으로 답하면 감별 실패다.

## 9. 러너 판정 조건과 강도, 부하 계산

- 주입: k8s.image release 모드, 설계 강도 1단 고정 evaluation(`approved-fixed-f42-p`, min_hold 15m, settle 30s, timeout 20m, max_injection_duration 25m).
  강도 사다리 없음, 첫 실행이 곧 녹화 실행.
- success(3틱): 동반 부하 인기 메뉴 비정상 비율(`loadgen.read_step_status_rate`, 4xx, 5xx, 연결 실패) ≥ 0.8. 하나로 둔 이유: restaurant 서버 스팬 5xx 비율은
  readiness 이탈 동안 스팬이 없어 흔들리고(기록용으로만 관측), 기준선 주문은 loadgen 주문 여정이 메뉴 조회 실패에서 멈춰 표본이 줄기 때문이다.
- must_rule_out(2틱): achieved_rps < 0.5(동반 부하 2rps 의 1/4, 부하 끊김), food MySQL NotReady(오래 빠지면 DB 다운, F25-H 꼴),
  **food MySQL 컨테이너 uptime < 1800초**(실행 중 재시작. 새 질의 `kubernetes.container_uptime_seconds`, 러너가 상태의 startedAt 으로 셈).
- preflight(새 검사 `db-memory-headroom`, 러너 서버 쪽 등록표 DB_MEMORY_HEADROOM["F42-P"]): food MySQL 의 cgroup memory.stat anon 이 900MiB 이하이고
  컨테이너가 1800초 넘게 떠 있을 때만 시작한다. 그래서 위 uptime 규칙은 재시작이 없으면 실행 내내 거짓이다.
- **MySQL 메모리 위험(평가 지적 2)**: 109 tb-w3 cgroup(2026-10-10): memory.current 1,073,078,272 / memory.max 1,073,741,824, anon 919,715,840(877MiB),
  file 137,023,488, kernel 16,384,000, VmRSS = VmHWM 934MB. 재시작 44회, lastState OOMKilled 2026-10-08T20:33Z, 30일 KCM Started 26회. 이 주입은 집계 세션을
  restaurant 풀 10개만큼 띄우고, restaurant 재시작이 끊고 떠난 집계도 MySQL 안에서 끝까지 돌아 잠깐 20개 안팎이 된다. 109 performance_schema 실측으로 집계
  세션 하나는 약 2.3MiB(current_allocated 2.29MiB, total 6.42MiB)라 20개면 약 46MiB 다. anon 900MiB 이하에서 시작하면 900 + 46 + kernel 16 ≈ 962MiB 로
  한도(1024MiB) 아래이고, 지금 값(877MiB)이면 약 85MiB 여유가 남는다. memory.current 는 페이지 캐시(file, 회수 가능) 때문에 평시에도 한도에 붙어 있어
  여유 조건으로 쓸 수 없어 anon 을 읽는다. 그래도 OOMKill 이 나면 MySQL 은 프로브가 없어 몇 초 만에 Ready 로 돌아오므로 pod_ready 는 놓치고, uptime 규칙이
  DB 재시작이 섞인 실행을 배제한다. restart_count(평시 44), last_termination_reason(평시 OOMKilled), memory_current_bytes 는 기록용으로만 관측한다.
- abort: entry_status == 0(2틱, 기준선 문서 domain food-delivery 의 order 진입 health). order 의 health DB 확인은 ping 이라 포화된 MySQL 에서도 빠르고, restaurant 호출
  실패는 재시도와 서킷으로 order 풀을 오래 잡지 않는다(서킷이 열리면 즉시 실패). 같은 MySQL 포화 꼴의 F33-R 실행에서도 order 는 연결 가능 상태를 지켰다.
- 부하: load.north_south `popular-menu-surge.js`(기존, F48-R 과 같은 스크립트) 2rps, 방문마다 가게 상세, 메뉴(step menu-view), 인기 메뉴(step menu), 주문 없음.
  - 인기 메뉴 요청: 동반 부하 초당 2건 + 기준선 초당 약 0.3~0.6건(표본 3일 15,015건 × 10 / 259,200초 ≈ 0.58) = 약 2.5건.
  - 처리 가능량: 집계 하나가 한가한 MySQL 에서 4~7.5초(CPU 0.5 와 버퍼 풀 밖 읽기)라 동시에 하나면 초당 약 0.2건. 요구량이 열 배쯤이라 restaurant 풀 10개가 늘
    실행 중인 집계에 묶이고, 동시 10개가 MySQL 을 나눠 쓰니 하나가 수십 초가 된다. 풀 처리량(약 10 / 40초 = 0.25건/초)을 넘는 나머지 인기 메뉴, 상세, 메뉴 요청은
    연결 대기 3초 뒤 500 이고, health 가 실패해 readiness 에서 빠지는 동안은 연결 실패다. 인기 메뉴 비정상 비율은 0.9 안팎이 된다.
  - 부하를 크게 잡지 않은 이유: 기준선만으로도 수요가 처리량을 넘는다. 동반 부하는 시간대와 무관하게 판정 표본(30초 창 약 60건)을 고르게 만드는 몫이다.
- 롤아웃 시간: tb-w3 에서 새 restaurant JVM 기동과 readiness 통과까지 약 1분(옛 파드가 그동안 서비스). 동반 부하는 2분에 걸쳐 오른다.
- 회복: 되돌린 뒤 매니페스트 버전은 캐시 표 88행을 읽는다. 옛 파드가 끊고 떠난 집계는 MySQL 안에서 끝까지 돌아(하나당 수 초~수십 초) 1~2분 안에 풀린다.
  recovery timeout 10m.
- 배포 전제: 109 docker 에 `food-delivery-restaurant:1.5.0` 이 있어야 preflight 가 통과한다(`bash scripts/scenarios/fault-images/build.sh f42-p`, 배포 단계에서 실행).
  새 부하 스크립트 없음(popular-menu-surge.js 는 이미 허용 목록과 tb-runner 에 있다), 상주 유닛 재시작 불필요.
  러너 변경이 필요하다(rca-scenario-runner: food MySQL 관측 대상 허용, 새 질의 `kubernetes.container_uptime_seconds`, 새 preflight `db-memory-headroom`).
  러너를 109 에 다시 배포하기 전에는 preflight 가 'unknown approved check ids' 로 막혀 실행되지 않는다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(GitHub 2025-01-09, 공식 월간 가용성 보고 링크)와 요소별 대응표, 기전 동일(§2). 처음 쓴 MIT 원본은 평가 지적(막힌 자원, 회복 꼴, 동기 불일치)으로 뺐다
- [x] 분류 장부 갱신(§4-1 행, §6 기록), 묶음 J+F(J 7→8, 12.1%), 정답 위치 가게 서비스(1→2, 3.0%), 결제 경로 11(16.7%), 서비스 food 19→20(가장 적음),
  고른 이유(§3). 어느 축도 20% 미만
- [x] 근본 원인 위치 `file:line` 확인(패치 줄, 매니페스트 코드, 풀 설정, order 호출자)과 인프라 지점(21-restaurant-deploy.yaml, 10-mysql.yaml, init.sql 색인)(§4)
- [x] 근본 원인 흔적 119 실조회: kcm_events_local 의 restaurant 이벤트, dpm_session_local 의 출발 IP 와 queryTime, VM DPM 지표(§7)
- [x] 핵심 증거가 표본에만 있지 않음: KCM 이벤트, DPM 세션과 지표(폴링), 전수 로그. 스팬은 보조. DPM Top SQL 의 맹점은 실측으로 확인하고 증거에서 뺐다
- [x] 계기 흔적: 롤아웃 이벤트와 새 이미지 태그. 인공 지연 없음(느려지는 이유는 실제 조인 집계의 실행 비용)
- [x] 정답이 관제 데이터로 낼 수 있는 결론(restaurant 롤아웃 직후 MySQL 포화, 긴 세션의 출발지가 restaurant 파드, 스키마와 다른 서비스 불변), 코드 설계 결함 추론 불필요(§5)
- [x] 정답지 세 칸이 원칙 6 대로(근본 restaurant 릴리스, 계기 같음, 부분 점수 MySQL 과 집계가 훑는 표, order)
- [x] 감지, 피해 판정, RCA 증거 구분(§6)
- [x] 주입 도구가 시나리오 id 를 남기지 않음: 실행기 인자는 네임스페이스, Deployment, 컨테이너, 이미지 둘뿐이고, 패치가 더한 문자열에 시나리오 id, fault, bug,
  chaos 같은 말이 없음(테스트 `test_k8s_image_release_mode_rolls_food_restaurant_live_popular_ranking` 이 확인). 동반 부하의 k6 태그는 기존 스크립트와 같은 규약
- [x] 부하 상한과 서킷브레이커를 고려한 피해 계산(§9: 인기 메뉴 약 2.5건/초 대 처리량 약 0.2건/초, 풀 10, 연결 대기 3초, order 재시도 3회와 서킷)

### 후보 목록 (숫자 순으로 줄 세운 뒤 관문에서 걸러진 것)

| # | 실제 기전 × 부품 | 사례 | 결과 |
|---|---|---|---|
| 1 | 메시지 브로커(Kafka, 세 도메인 0), food notify, commerce notification(0) 의 소비 고장 | GitHub 2026-06-25, PagerDuty 2023-08-28 등 | 버림, 원칙 7: 세 도메인 모두 Kafka 를 outbox 릴레이와 비동기 소비자에서만 쓴다(commerce notification 도 @KafkaListener 뿐, grep). 사용자 증상이 없다(앞선 반려와 같은 벽) |
| 2 | commerce gateway(0) 메모리 누수 릴리스 | Honeycomb 2019-11-06 | 뒤로 미룸: 정답 위치 게이트웨이 0 이라 commerce(26, 최다)여도 낼 수 있으나 숫자 순위는 가장 적은 food 가 먼저다. F41-R 과 같은 원본, 같은 결함 꼴이라 다음 commerce 차례 후보로 남긴다 |
| 3 | food 워커 tb-w3 conntrack 표 가득(K, 노드) | (공식 사후 보고 못 찾음) | 버림, 원칙 1: 웹 검색 3회(SUSE KB, 개인 블로그, Netdata 안내, Preply DNS 사후 보고는 conntrack 경쟁 조건으로 생긴 5초 DNS 지연이라 표 가득이 아님). rejected 의 같은 행과 같은 벽 |
| 4 | banking transfer 의 DB 자격 증명 비밀값을 회전이 틀린 값으로 바꾸고 일상 재배포(maxSurge 0)가 새 값으로 뜸(O, 비밀값 0) | Harness 2026-01-08 | 버림, 원칙 1: Harness 는 옛 DB 사용자를 비활성화해 서비스가 인증을 잃은 것(복구가 옛 사용자 재활성화, F35-R 의 해석)이라 클라이언트 쪽 비밀값 불일치와 기전이 다르다. 클라이언트 쪽 비밀값이 틀린 공식 사후 보고를 웹 검색 2회로 못 찾음(GitLab 운영 이슈 2023-06-20 은 원인 미기재). GitHub 2026-04-01 은 이번 실행에서 막힌 원본 |
| 5 | food dispatch 만료 배치를 멈추게 한 릴리스로 배차 한도가 차 주문 503(F32-R 의 H) | (공식 사후 보고 못 찾음) | 버림, 원칙 1: 회수 작업이 멈춰 고정 용량이 찬 공식 사후 보고를 웹 검색 3회로 못 찾음(Redocly 2026-01-14 는 정리 작업의 백오프 없는 재시도가 큰 표를 때린 것이고 표와 메모리 고갈 이유를 밝히지 않음, Healthchecks.io 2020-02-07 은 불가능한 cron 식으로 송신기가 멈춘 것). 109 실측: 평시 배정 중 약 1,700, 한도 2000 이라 만료가 멈추면 약 4~5분에 차는 것은 확인 |
| 6 | food DB 계정 권한 정리 누락으로 주문 INSERT 만 1142(DB 계정) | (공식 사후 보고 못 찾음) | 버림, 원칙 1: 웹 검색 1회도 트러블슈팅 문서뿐(rejected 의 같은 행과 같은 벽) |
| 7 | DB 권한 변경이 메타데이터 질의에 중복 행을 돌려 생성 파일이 하드 한도를 넘음(food restaurant) | Cloudflare 2025-11-18 | 버림, 원칙 1(복합): 하드 한도를 가진 코드가 테스트베드에 없어 결함 릴리스와 권한 변경 두 주입이 필요하다(Fastly 2021 반려와 같은 벽) |
| 8 | 운영자 입력 실수로 banking 네임스페이스 파드를 한꺼번에 재시작(P+B) | Joyent 2014-05-27 | 버림, 원칙 1과 컨트롤러 필수 중단 조건: 원문을 이번에 확인하지 않았고, Oracle StatefulSet 까지 재시작하면 F25-H 꼴 DB 중단이 먼저 피해를 내며 F51-R(운영 명령 입력 실수)과 원인 꼴이 겹친다 |
| 9 | commerce PostgreSQL 마이그레이션의 TRUNCATE CASCADE 가 살아 있는 표들을 비움 | Linear 2024-01-24 | 버림, 원칙 9(cleanup): 지운 행을 되돌리려면 백업 복원이 필요하고, commerce 최다 서비스에서 정답 위치 0 인 표를 고르기 어려우며 사후 보고 본문의 CASCADE 문장을 확인하지 못함(3차 요약만) |
| 10 | **food restaurant 릴리스가 인기 메뉴를 요청마다 주문 원장에서 셈** | GitHub 2025-01-09(처음 MIT Open Learning 2026-03-24 로 냈다가 평가에서 원칙 1 지적으로 바꿈) | **채택(F42-P)** |
