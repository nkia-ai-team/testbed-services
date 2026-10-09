---
title: F48-R 설계 시트 (취소된 인덱스 백필이 food 인기 메뉴 집계 표를 치워 인기 메뉴만 실패)
status: Draft
owner: project
last_reviewed: 2026-10-09
tags:
  - scenario
  - design
  - schema
  - mysql
  - operation
summary: 운영자가 food MySQL 의 인기 메뉴 집계 캐시 표(menu_popularity_summary)에 restaurant_id 인덱스를 더하는 온라인 백필을 시작했다가 취소했는데, 취소 정리가 그림자 표 대신 살아 있는 표를 보류 이름으로 치워 restaurant-service 의 인기 메뉴 조회가 모두 MySQL 1146 Table doesn't exist 로 500 이 되는 시나리오. 가게 상세, 메뉴, 검색, 주문은 정상이다. 원본은 GitHub 2026-07-24 Vitess 백필 워크플로 취소가 기반 표를 지워 PR 생성만 실패한 장애.
---

# F48-R 설계 시트

## 1. 요약

food 의 restaurant-service 는 인기 메뉴 순위를 작은 캐시 표 `fooddelivery.menu_popularity_summary`(88행, 기본 키뿐)에 둔다. 한 시간마다 `PopularMenuBatch` 가 표를 비우고 다시 채우며, `GET /api/restaurants/{id}/popular-menu` 가 `findByRestaurantIdOrderByOrderCountDesc` 로 읽는다. 표에 `restaurant_id` 인덱스가 없어(이 질의는 지금 매번 표 전체를 훑고 DPM Top SQL 에 자주 올라온다) 운영자가 온라인 인덱스 백필을 시작한다. 도구는 새 인덱스를 가진 그림자 표(`_vt_vrp_<uuid>_<시각>_`)를 만들고 행을 옮긴다. 그런데 백필이 취소되고, 취소 정리가 그림자 표가 아니라 살아 있는 표를 치운다. 표는 테이블 수명주기의 보류 이름(`_vt_hld_<uuid>_<보류 기한>_`, Vitess 가 표를 지울 때 먼저 하는 일)으로 바뀌고 그림자는 남는다. 그 순간부터 `menu_popularity_summary` 를 부르는 모든 문장이 `ERROR 1146 (42S02): Table 'fooddelivery.menu_popularity_summary' doesn't exist` 로 거절된다. 인기 메뉴 엔드포인트는 매번 500 으로 답하고, 같은 파드, 같은 DB 에서 가게 상세, 메뉴, 검색, 그리고 주문 경로 전체는 이 표를 읽지 않아 그대로다. 롤아웃, 재시작, 설정 변경은 없고 MySQL 은 빠르고 건강하다.

비유: 창고에서 "인기 상품" 선반에 새 칸막이를 달려고 옆에 임시 선반을 짜고 물건을 옮겨 담다가 작업을 취소했는데, 정리하던 사람이 임시 선반 대신 원래 선반을 보관 창고로 치워 버렸다. 다른 선반 일은 멀쩡하고, "인기 상품" 을 찾는 직원만 매번 "그런 선반이 없다" 며 빈손으로 돌아온다.

## 2. 원본 사례

- 기업: GitHub (풀 리퀘스트 생성)
- 날짜: 2026-07-24 19:17~20:02 UTC(57분)
- 링크: [GitHub Availability Report: July 2026 (공식)](https://github.blog/news-insights/company-news/github-availability-report-july-2026/)
- 요약(출처가 말한 것만): 사용자가 웹, CLI, API 로 풀 리퀘스트를 만들지 못했다("users were unable to create pull requests through web, command line, or API interfaces"). 시도 113,930회, 사용자 50,904명, 평균 오류율 1.75%, 최대 2.25%. 기존 풀 리퀘스트와 다른 기능은 영향이 없었고 PR 을 만드는 워크플로도 실패했다. 근본 원인은 풀 리퀘스트 데이터를 담은 Vitess keyspace 로의 백필 워크플로였고, "The cancellation executed a misunderstood Vitess codepath that dropped the backing table to the target keyspace" 라 낡은 vschema 참조가 남았다. 데이터베이스 변경을 되돌리자 PR 생성이 바로 재개됐고("upon which pull request creation immediately resumed"), 낡은 참조를 지워 마무리했다. 재발 방지는 인덱스 백필 운영 지침과 예상 밖 취소 동작 문서화, 사전 검증, 더 안전한 내장 코드 경로, 하위 환경 종단 시험이다. 탐지 경로는 적혀 있지 않다. `ref-real-world-incidents.md` M21 에 추가했다.
- Vitess 쪽 사실(제품 문서, 사고 보고 아님): Vitess 는 표를 지울 때 바로 지우지 않고 수명주기(hold → purge → evac → drop)를 밟으며 상태를 표 이름에 담는다(`_vt_hld_<uuid>_<시각>_` 등). 온라인 스키마 변경의 남은 표도 이 수명주기로 보낸다([Vitess table lifecycle](https://vitess.io/docs/user-guides/schema-changes/table-lifecycle/)). 재구성은 "지움" 을 보류 이름으로 바꾸기로 둔다: 원본의 복구(변경 되돌림 → 즉시 재개)와 같은 꼴이고 행을 잃지 않는다.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 풀 리퀘스트 데이터 keyspace 로의 백필 워크플로를 취소 | `menu_popularity_summary` 에 `restaurant_id` 인덱스를 더하는 온라인 인덱스 백필(그림자 표 생성, 인덱스, 행 복사)을 시작했다가 취소 |
| 원인이 된 결함 | 취소가 잘못 이해된 Vitess 코드 경로를 타 기반 표를 지움 | 취소 정리가 그림자 표가 아니라 살아 있는 표를 보류 이름(`_vt_hld_...`)으로 치움, 그림자 표는 남음 |
| 전파 경로 | 앱이 여전히 그 표를 참조(낡은 vschema 참조) → 그 표를 쓰는 연산만 실패 | restaurant-service 엔티티가 여전히 `menu_popularity_summary` 를 읽음 → 인기 메뉴 질의 1146 → 500 |
| 사용자 증상 | PR 생성만 실패(평균 1.75%), 기존 PR 과 다른 기능은 정상 | 인기 메뉴만 100% 500, 가게 상세, 메뉴, 검색, 주문, 배달, 결제는 정상(부분 장애) |
| 탐지된 경로 | 적혀 있지 않음 | restaurant 오류율과 새 오류 로그(1146) 급증(119 이상 탐지 → 인시던트) |
| 완화와 복구 | 데이터베이스 변경을 되돌리자 즉시 재개, 낡은 참조 정리 | cleanup 이 보류 표를 원래 이름으로 되돌림(즉시 재개, 행 그대로), 남은 그림자 표를 지움 |

기전은 원본과 같다: "백필 취소가 살아 있는 표를 지움 → 앱이 여전히 그 표를 참조 → 그 표를 쓰는 기능 하나만 실패, 나머지는 정상 → 변경을 되돌리면 즉시 회복". 우리 스택에 맞춘 것은 대상(MySQL 8.0 의 캐시 표 하나)과 규모다. 원본의 PR 생성은 전체 요청 중 작은 비율이라 오류율 1.75% 였고, 우리도 인기 메뉴는 restaurant 업무 요청의 일부(평시 약 8%)다.

## 3. 숫자 근거 (2026-10-09, `scenario-stats.py`, 정식 + 후보 합계 53, 이 후보 전)

| 축 | 값 | 판단 |
|---|---|---|
| 서비스 | 쇼핑몰 26, 은행 16, 음식배달 11 | 음식배달이 가장 적다 → 음식배달(11→12) |
| 묶음 | A 7, B 7, D 7(각 13%), C 6, G 6(각 11%), J 5, F 3, L 3, E 2, H 2, K 2, I 1, M 1, O 1, N 0 | 새 묶음 P(운영 작업의 대상 착오) 0→1. 결함이 앱이 기대하는 표가 없는 꼴로 드러나 P+L 로 적는다. L 로 세더라도 3→4(7.4%) |
| 정답 위치 | DB 테이블(인기 메뉴 집계) 0 | §2-1 의 'DB 테이블(<무엇>)' 꼴, 0→1 |
| 결제 경로 합계 | 10(18.9%) | 결제가 정답이 아니므로 그대로(10/54, 18.5%) |
| DB 계열 정답 위치 합계(참고) | DB 테이블 10 + DB 인스턴스 2 + DB 계정 1 = 13(24.5%) | 14/54(25.9%). 참고 값일 뿐 상한 축이 아니다: 정답 위치는 표, 인스턴스, 계정으로 나뉘어 각각 20% 상한을 센다(2026-10-09 사용자 정정). 이 후보의 위치는 0→1 이다 |

묶음 해석: 바뀐 것은 표 하나의 존재다. 설정값이 아니라 G 가 아니고, 코드나 이미지 배포가 없어 J 가 아니며, 계획된 스키마 변경이 코드보다 먼저 적용된 F36-R(L)과도 다르다: 백필이 의도한 변경(인덱스 추가)은 맞았고 그것을 적용하던 도구의 정리 단계가 엉뚱한 대상(살아 있는 표)을 치웠다. 현실 근거 M22(Atlassian 2022-04-05 잘못된 id, AWS S3 2017-02-28 명령 오타)와 같은 "운영 작업이 의도와 다른 대상을 바꿈" 이라 새 묶음 P 를 장부 §2 에 더했다. DB 프로세스, 자원, 잠금, 질의 비용은 그대로라 A, B, D, F 가 아니다.

### 후보 목록 (실제 기전 × 0인 부품 또는 인프라 층, 숫자 순으로 줄 세움, 버린 이유 포함)

부품 지도의 0인 부품: commerce kafka, notification, gateway, nginx / banking kafka, nginx / food kafka, order, notify. 인프라 층 전체. 출발은 기존 목록이 아니라 최근 공식 사후 보고(GitHub 2026-07, 2026-08 월간 가용성 보고를 이번에 새로 읽음)의 기전이다.

| # | 후보 | 원본 사례 | 부품, 층 | 결과 |
|---|---|---|---|---|
| 1 | food 인기 메뉴 집계 표를 취소된 인덱스 백필이 치움 | GitHub 2026-07-24 | food MySQL 표(가게 서비스 쪽), 앱 층 | **채택**: 음식배달 11(최소), 새 묶음 P 0, 정답 위치 0, 주문 경로와 order 증상이 아니어서 food 의 구조적 제약(order 입구, restaurant 메뉴 입구)에 안 걸림 |
| 2 | food notify 소비자가 하류 지연으로 처리가 밀림(고정 파티션 처리기 적체) | GitHub 2026-08-20 | food notify(0), 큐 컨슈머 지연 층 | 버림(원칙 7): 사용자 경로 밖 비동기 소비자라 5xx, 지연 증상이 없음(이번 실행 앞선 소비자 반려와 같은 벽) |
| 3 | 노드 내부 인증서 수명주기 실패 | GitHub 2026-07-19~20 | 인증서와 비밀값 층 | 버림(사람 검토, 인프라): 서비스 사이 TLS 가 없어 신설 필요(rejected 의 내부 TLS 만료 행과 같은 벽) |
| 4 | commerce gateway 가 무관한 인프라 변경 뒤 덜 쓰이는 인증 방식을 거절 | GitHub 2026-07-21 | commerce gateway(0) | 버림(F34-R 교훈): 결과가 쓰기 401 단일 신호(rejected 의 gateway 401 행들과 같은 벽) |
| 5 | 사이드카 동시성 한도와 클라이언트 재시도 버그가 인증 엔드포인트를 증폭 | GitHub 2026-08-17 | banking api(2), 묶음 N(0) | 버림(원칙 9): 메시 사이드카가 없고, 재시도 3배로도 180rps 안에서 포화 계산이 서지 않음(이번 실행 N 반려와 같은 벽) |
| 6 | 한도 근처 공유 DB 에 이벤트 몰림으로 주 DB 포화 | GitHub 2026-08-26 | food MySQL(DB 인스턴스 2), 묶음 E | 남김(순위 아래): F33-R 과 겉 증상(MySQL 포화, order 503)이 같지만 정답이 다른 H 관계라 버릴 사유가 아니다(2026-10-09 사용자 정정). 묶음 E(2), 정답 위치 DB 인스턴스(2)라 0 인 묶음, 0 인 위치를 가진 1번보다 뒤. 다음 반복 재료 |
| 7 | 자동 메타데이터 처리가 실행 중인 VM 의 런타임 값을 바꿔 서비스 디스커버리가 깨짐 | GitHub 2026-07-08 | 노드 런타임 설정, 서비스 디스커버리 층 | 버림(원칙 4): 재현에 kube-proxy, CoreDNS 같은 kube-system 조작이 필요한데 119 KCM 이 kube-system 을 수집하지 않음 |
| 8 | 일상 배포가 한 사이트의 파드를 잠깐 줄여 남은 쪽이 한계를 넘음 | GitHub 2026-08-06 | 리소스 할당량과 스케줄링 층 | 버림(원칙 1): replicas 1 고정, 다중 사이트 없음이라 기전이 성립하지 않음 |
| 9 | 용량 이동 중 Redis 노드가 연결 한도에 닿음 | GitHub 2026-07-25 | commerce Redis | 버림(원칙 2): 쇼핑몰(26, 최다)은 정답 위치 0 인 부품만 내는데 캐시는 1 |
| 10 | banking 릴레이 제어 표를 같은 취소 백필이 치움 | GitHub 2026-07-24 | banking Oracle 표 | 버림(원칙 7): OutboxRelay 가 제어 행 조회 실패를 fail-open 으로 넘겨 증상이 없음(DB 정답 후보 2개째) |

같은 원본으로 다른 food 표도 견줬다(같은 수단이지만 파라미터가 달라 같은 주입 중복은 아니다). `order_items`(정답 위치 DB 테이블(주문 품목) 0, 주문 생성 500, order 는 자기 풀이 멀쩡해 Ready), `dispatch_events`(DB 테이블(배차 이력) 0, 배차 기록 실패로 order 503, F32 계열과 H 관계), `restaurants`(DB 테이블(가게) 1, F36-R 과 H 관계)는 수치상 1번과 같거나 바로 아래라 다음 반복 재료로 남긴다(겉 증상 겹침은 2026-10-09 사용자 정정으로 버릴 사유가 아니다). `menus` 는 loadgen 주문 여정의 메뉴 조회가 막혀 주문 표본이 사라지므로 러너 판정이 서지 않아(원칙 7) 뺀다. 1번을 먼저 고른 이유는 원본의 피해 모양(PR 생성 한 기능만 실패, 나머지 정상)과 가장 가깝기 때문이다: 사용자 경로에서 다른 서비스를 거치지 않고 한 기능만 깨진다.

## 4. 인과 사슬 (코드와 인프라 위치)

```
db.ddl(mysql, 보류 표 모드): testbed-mysql-0 안 MySQL 클라이언트, lock_wait_timeout 10초
  CREATE TABLE _vt_vrp_<uuid>_<시각>_ LIKE menu_popularity_summary;
  ALTER TABLE _vt_vrp_... ADD INDEX idx_menu_popularity_restaurant (restaurant_id);
  INSERT INTO _vt_vrp_... SELECT * FROM menu_popularity_summary          (88행, 백필)
  RENAME TABLE menu_popularity_summary TO _vt_hld_<uuid>_<보류 기한>_     (취소 정리의 착오)
  → DPM: fooddelivery table_count 16→17, index_count 31→33 (그 분)
  → restaurant GET /api/restaurants/{id}/popular-menu: RestaurantService.getPopularMenu
       existsById(restaurants, 성공) → findByRestaurantIdOrderByOrderCountDesc
       'select mps1_0.id, ... from menu_popularity_summary mps1_0 where mps1_0.restaurant_id=? order by mps1_0.order_count desc'
       → MySQL 1146 Table 'fooddelivery.menu_popularity_summary' doesn't exist
       → Hibernate SqlExceptionHelper WARN 'SQL Error: 1146, SQLState: 42S02', ERROR "Table ... doesn't exist"
       → GlobalExceptionHandler 가 ServiceException 만 매핑 → 처리되지 않은 예외, Tomcat SEVERE, 500
  → PopularMenuBatch(한 시간 주기, 매시 약 48분 UTC): 고장 중에 돌면 deleteAll 이 같은 1146 으로 실패
  ↔ restaurant GET /{id}, /{id}/menu, /api/restaurants(검색): restaurants, menus 만 읽음 → 200
  ↔ order POST /api/orders: getRestaurant, getMenu, dispatch, payment → 이 표 무관, 정상
  ↔ restaurant /actuator/health: DataSource 확인은 SELECT 1 → UP, Ready 유지
```

앵커(정답지 `code_anchor` 와 같음):

- `food-delivery/restaurant-service/src/main/java/com/fooddelivery/restaurant/entity/MenuPopularitySummary.java:6-7` (`@Table(name = "menu_popularity_summary")`)
- `food-delivery/restaurant-service/src/main/java/com/fooddelivery/restaurant/service/RestaurantService.java:43-52` (getPopularMenu), `:54-69` (getRestaurant, getMenu)
- `food-delivery/restaurant-service/src/main/java/com/fooddelivery/restaurant/repository/MenuPopularitySummaryRepository.java:14`
- `food-delivery/restaurant-service/src/main/java/com/fooddelivery/restaurant/controller/RestaurantController.java:47-51`
- `food-delivery/restaurant-service/src/main/java/com/fooddelivery/restaurant/service/PopularMenuBatch.java:37-45`
- `food-delivery/restaurant-service/src/main/java/com/fooddelivery/restaurant/config/GlobalExceptionHandler.java:9-20`
- `food-delivery/restaurant-service/src/main/resources/application.yml:19-20` (ddl-auto none: 재시작해도 표를 만들지 않음)
- `food-delivery/db/init.sql:191-198` (menu_popularity_summary, 기본 키뿐)
- 인프라: rca-testbed-food `testbed-mysql-0`(MySQL 8.0.46, tb-w3). 109 실측(2026-10-09): 표 88행, `SHOW INDEX` 는 PRIMARY 하나, fooddelivery 표 16개, 고유 인덱스 31개(DPM 값과 같음), 외래 키 참조 없음

로컬 실측(mysql:8.0.46 컨테이너, 같은 표 정의, 88행): 실행기 스크립트 전체를 가짜 kubectl 로 돌림. preflight 통과 → run 뒤 표 목록이 `_vt_hld_..._20261011175812_`, `_vt_vrp_..._20261009175812_` 이고 같은 SELECT 가 `ERROR 1146 (42S02) ... Table 'fooddelivery.menu_popularity_summary' doesn't exist` → 두 번째 run 은 check 에서 거절(rc 1) → cleanup 두 번(두 번째는 할 일 없음) → recovery 통과, 표 하나에 88행 그대로.

## 5. 원인 규정 (원칙 5, 6)

| 칸 | 값 | 근거 |
|---|---|---|
| `root_cause.target_id` | `food-mysql:fooddelivery.menu_popularity_summary` | 결함은 표 쪽이다(운영 도구가 살아 있는 표를 치움). 원본 사후 보고의 원인과 복구도 데이터베이스 변경(표를 지움)과 그 되돌림을 가리킨다. 표기는 F36-R, F33-R 과 같은 '인스턴스:스키마.테이블' 꼴 |
| `trigger_target_id` | null | 근본과 계기가 같은 곳(이 표에 대한 운영 작업) |
| `scoring.partial` | `food-delivery-restaurant`, `food-restaurant`, `food-mysql` | 오류가 찍히는 서비스와 DB 인스턴스는 원인 근처다 |

원칙 5: 정답은 로그의 오류 문장("없는 표 menu_popularity_summary"), 엔드포인트별 갈림, 같은 분 DPM 표 수 증가, 그 시각에 배포나 재시작이 없다는 사실로 낼 수 있는 결론이다. 코드 설계 결함 추론이 필요 없다. "restaurant 가 인기 메뉴에서 SQL 오류를 낸다" 고 답하면 restaurant(부분), "menu_popularity_summary 표가 사라졌다(DDL, 운영 작업)" 고 답하면 정답이다. 누가 왜 백필을 취소했는지는 요구하지 않는다.

원칙 6: restaurant 와 배치는 늘 하던 정당한 요청을 보냈고 결함 있는 곳(사라진 표)에서 실패했다. 표를 치운 운영 작업이 원인이다.

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 | 기대 |
|---|---|---|
| 감지 | restaurant 서버 스팬 오류(골든 시그널), 새 오류 로그 템플릿(1146 WARN, ERROR)과 로그 급증 | 고장 중 restaurant 업무 요청의 약 24~31% 가 500(동반 부하 5rps 가 가게 화면마다 상세, 메뉴, 인기 메뉴를 하나씩). 실패 요청마다 WARN, ERROR, SEVERE 세 줄, 초당 약 15줄 |
| 피해 판정 | 러너: 동반 부하 인기 메뉴 비정상 비율 ≥ 0.8 과 restaurant 표본 스팬 5xx ≥ 10%(3틱) | 평시 인기 메뉴 5xx 7일 표본 16,057건 중 1건, restaurant 5xx 205,276건 중 8건 |
| 원인 설명 | 로그 전수(1146 오류 문장), DPM 표 수와 인덱스 수, 스팬 엔드포인트별 갈림, KCM 무변화, DPM 세션과 응답 시간 정상 | §7 |

인시던트 근거와 위험:

- 같은 restaurant 의 스키마 쪽 고장인 F36-R 이 정식(녹화 2026-10-09 10:19~10:35 UTC)이다. F36-R 은 restaurant 업무 요청 약 40~50% 와 order 주문 100% 가 실패했고, F48-R 은 restaurant 하나에 약 24~31% 다. F36-R 시트 §6 의 실측(promote 는 대개 열린 묶음의 첫 판정, 고장 2~4분째에 나고 그 판정의 15분 창에 고장이 2~4분만 들어가 희석됨)을 그대로 적용하면 첫 판정 때 restaurant 오류율은 대략 4~8% 로 보인다(평시 0 근처). F36-R(대략 6~12%)보다 약하므로, 동반 부하를 F36-R(2rps 주문 여정)보다 크게(5rps, 인기 메뉴 매 방문) 두었다.
- 묶음은 restaurant 하나라 F36-R 의 "restaurant 와 order 가 쪼개질 위험" 은 없다. 대신 새 템플릿 이벤트가 caution 등급이면 cheap-gate 로 걸러질 위험이 있다. 실패 로그가 초당 약 15줄로 F36-R(15분 3,952 ERROR, 분당 약 260)의 약 3배라 surge 이벤트가 함께 날 것으로 본다.
- 동반 부하의 램프업(2분)이 restaurant 요청량을 기준선의 몇 배로 올리므로 고장 전에 restaurant 트래픽 증가 이벤트가 날 수 있다. 오류 없는 트래픽 증가라 판정은 고장 뒤 이벤트가 정한다.
- 인시던트가 안 생기면 첫 실행 검증에서 묶음의 멤버 템플릿, 판정 사유, promoted_at 을 본다(§11).

## 7. 관측 근거 표 (119 실조회, 2026-10-09 17:30~18:00 UTC)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 결정 증거는 전수 로그와 DPM, KCM 이다.

| 증거 | 119 표와 칸 | 조회 | 평시 결과 |
|---|---|---|---|
| 근본: 없는 표 오류 | CH `lucida_logs_local` (`service_name`, `body`) | `SELECT min(timestamp), countIf(position(body,'1146')>0 AND position(body,'SQL Error')>0), countIf(position(body,'doesn''t exist')>0) FROM lucida.lucida_logs_local WHERE service_name='food-delivery-restaurant'` | 0건, 0건(보존 창의 가장 이른 로그 2026-10-02 17:29 UTC). food 전체로 넓히면 '1146' 이 든 행은 주문, 결제 id 숫자뿐 |
| 근본 경로가 CH 에 남는가(같은 로거) | 같은 표 | restaurant 'SQL Error: 1054' 집계 | 3,952건, 2026-10-09 10:19:50~10:34:56 UTC(F36-R 녹화 실행). 같은 파드의 Hibernate SqlExceptionHelper WARN/ERROR 와 Tomcat SEVERE 가 전수로 남는다. 1146 도 같은 경로다 |
| 계기: 백필(그림자 표와 새 인덱스) | VM `dpm.mysql.database.table_count{db_name="fooddelivery"}`, `dpm.mysql.database.index_count` (target c8c558e5-…, 60초 간격) | VM export 2026-10-01~10-09 | table_count 12,401점 모두 16(변화 0). index_count 31, 바뀐 것은 2026-10-09 03:36~03:44 UTC 30 한 번(F33-R 실행의 인덱스 제거). DDL 이 이 지표에 1분 해상도로 보인다는 선례다. 109 실측 표 16개, 고유 인덱스 31개와 같다 |
| 계기: DDL 문장 자체 | CH `dpm_topsql_local` | food MySQL target 의 DDL 문장 검색 | 127만 행 중 0건. DDL 은 Top SQL 에 남지 않는다(F36-R 과 같음) |
| 질의 모양 | `dpm_topsql_local` `body.sqlText` | `menu_popularity_summary` 를 포함한 문장 | 인기 메뉴 SELECT(restaurant_id 조건, 인덱스 없음) 3,355행(2026-08-13~10-08)과 그 EXPLAIN 77행. 이 질의가 Top SQL 에 자주 오르는 것이 인덱스 백필의 동기다 |
| 다른 변경 없음 | CH `kcm_events_local` | `namespace='rca-testbed-food' AND object_name LIKE 'testbed-restaurant%'` | Unhealthy 70건(2026-08-24~10-08 간헐 프로브)뿐, ScalingReplicaSet 0건. 109 kubectl: 파드 `testbed-restaurant-549cc6cc64-8b2kz` 나이 59일, 재시작 0 |
| 피해: 엔드포인트별 | CH `otel_traces_local` (`span_kind='SERVER'`, `span_name`, `span_attributes['http.response.status_code']`) | 7일 restaurant 서버 스팬, F36-R 실행 창 제외 | 메뉴 94,128(5xx 2), 상세 75,247(3), 인기 메뉴 16,057(1), 검색 14,682(1), health 5,162(1) |
| 대조: F36-R 녹화 창 | 같은 표 | 2026-10-09 10:19~10:35 UTC | 상세 339 중 313, 검색 69 중 64 가 5xx, 인기 메뉴 98 중 0, 메뉴 525 중 0. F48-R 은 정확히 반대 칸이 깨진다 |
| 배치 주기 | `lucida_logs_local` | 'Popular menu aggregation batch started' | 매시 약 48분 UTC(시간당 약 6초씩 밀림), 'finished: menusRanked=88' |
| 배제: DB 정상 | VM `dpm.mysql.session.active_session`, `dpm.mysql.instance.avg_query_response_time` | F36-R 시트 §7 과 같은 조회 | 8일 중앙 1, 7.28ms |

## 8. 감별

- must_support: 1146 오류 로그(평시 0), 같은 분 DPM 표 수 16→17과 인덱스 수 31→33, 엔드포인트별 갈림(인기 메뉴 500 / 상세, 메뉴, 검색 200), 주문 생성 정상, 롤아웃과 재시작 없음, MySQL 과 restaurant 풀 정상.
- must_rule_out(정답지에 문장으로): restaurant 새 버전, MySQL 다운이나 재시작, 부하 증가나 용량 부족, 열 이름 변경 마이그레이션(F36-R), 집계 배치의 데이터 문제(그러면 200 과 빈 목록).
- 동반 부하 경쟁 가설 배제: 동반 부하가 인기 메뉴 요청을 수십 배 늘리지만 실패는 즉시 나는 SQL 오류이고 문장이 없는 표 이름을 가리킨다. 같은 부하에서 같은 파드, 같은 DB 의 메뉴와 상세는 200 이다. 고장 전 램프업 구간에도 인기 메뉴는 200 이다.
- contrast_with: F36-R(같은 restaurant 와 food MySQL 의 표 쪽 고장, 계획된 열 이름 변경, 깨지는 칸이 정확히 반대), F33-R(food MySQL DDL 계기, 느린 쿼리, index_count 감소), F25-H(DB 프로세스 중단).
- 판별력: 서비스 입도 채점에서는 F36-R 과 같은 'restaurant' 근처 답이 부분 점수다. 정답 입도(database-relation)에서는 표가 다르고(restaurants 대 menu_popularity_summary), 오류 번호(1054 대 1146)와 DPM 표 수 변화가 가른다.

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

진행 대본: 설계 강도 하나로 고정한 평가 모드(`approved-fixed-f48-r`). 강도라는 축이 없다: 표가 없으면 그 표를 읽는 질의는 요청량과 무관하게 100% 실패한다.

| 항목 | 값 | 근거 |
|---|---|---|
| primary | db.ddl(보류 표 모드), 계약 `{engine mysql, rca-testbed-food, testbed-mysql-0, fooddelivery, menu_popularity_summary, backfill_index idx_menu_popularity_restaurant, backfill_column restaurant_id, minimum_rows 20}` | 실행기 CONTRACTS 와 profiles.json 같은 값(G2) |
| companion | load.north_south, 새 스크립트 `popular-menu-surge.js`, target_rps 5, ramp 2m, hold 21m, food 진입점 30181, 기준선 loadgen-food | 방문마다 상세(step detail), 메뉴(step menu-view), 인기 메뉴(step menu, 이 domain_profile 의 read_step). 주문을 넣지 않아 배차 한도와 order 를 건드리지 않는다. slowquery.js 가 무거운 조회를 같은 이름으로 태깅한 선례를 따른다 |
| entry_status | 기준선 문서(domain food-delivery)의 주문 생성 단계 | 동반 부하 문서에 주문 단계가 없어 비므로 기준선을 읽는다(F35-R, F37-R 등 은행 시나리오와 같은 방식). 고장 중 order 는 정상이라 0 은 노드나 order 가 죽었을 때만 |
| min_hold / settle / timeout | 15m / 30s / 20m, max_injection 25m | F36-R 과 같음(§6) |
| success(3틱) | popular_menu_nonok_rate ≥ 0.8, restaurant_error_rate ≥ 10(%) | 앞은 표본 아닌 k6 집계(30초 창 약 150건), 뒤는 실패가 restaurant 가 답한 5xx 임을 확인 |
| must_rule_out(2틱) | achieved_rps < 1.25, MySQL NotReady, restaurant NotReady, 기준선 주문 생성 5xx ≥ 0.5 | 마지막은 주문 경로까지 깨지면 F36-R, F32-R, F33-R 꼴이라는 뜻 |
| abort | entry_status == 0 (필수) | |
| recovery(2틱, 10m) | target_health 200, MySQL Ready, restaurant Ready, restaurant_error_rate < 5, 기준선 주문 2xx ≥ 0.7 | 되돌리면 다음 질의부터 즉시 200 |

부하 계산(가장 한가한 KST 02~06시 기준선 1 iter/s, 가장 바쁜 시간 6 iter/s):

- 기준선 restaurant 요청: iter 당 약 1.6건(둘러보기 55% × 2.3, 검색 15%, 주문 여정 메뉴 20%) → 초당 1.6~9.7건, 그중 인기 메뉴 0.17~1.0건.
- 동반 부하 5rps: restaurant 초당 15건, 인기 메뉴 초당 5건.
- 고장 중 restaurant 5xx: 초당 5.2~6.0건, restaurant 요청 대비 약 31%(한가함)~24%(바쁨). 60초 표본 스팬 약 100~150건 중 오류 약 30~36건 → 판정 문턱 10% 의 2배 이상.
- 인기 메뉴 비정상 비율: 표가 없는 동안 1.0(30초 창 약 150건) → 문턱 0.8.
- 용량: restaurant 평시 p95 28.8ms, 동반 부하 초당 15건은 surge.js 실측(초당 100건 넘게 무릎 없음, surge.js 머리말 §8.1-2) 아래다. 고장 중 인기 메뉴는 MySQL 이 즉시 거절해 더 가볍다. 주문이 없어 배차 한도(F30-R 에서 동반 부하 주문이 dispatch 풀을 말린 일)와 무관하다.
- 서킷브레이커: restaurant 앞에 서킷이 있는 호출자(order)는 인기 메뉴를 부르지 않는다. 손님이 직접 부르는 경로라 실패가 그대로 500 으로 보인다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(GitHub 2026-07-24, 공식 월간 가용성 보고)와 요소별 대응표가 있고 기전이 같다 (§2)
- [x] 분류 장부 §2(새 묶음 P), §4-1, §6 갱신. 묶음 P 0→1(1.9%, L 로 세도 7.4%), 정답 위치 DB 테이블(인기 메뉴 집계) 0→1, 결제 경로 10/54(18.5%), 음식배달 11→12. 어느 축도 20% 미만 (§3)
- [x] 근본 원인 위치: `MenuPopularitySummary.java:6-7`, `RestaurantService.java:43-52`, 인프라 testbed-mysql-0 menu_popularity_summary (§4)
- [x] 근본 원인 흔적: CH `lucida_logs_local` 1146 오류 문장(전수, 같은 로거의 1054 기록으로 경로 확인) (§7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: 로그 전수, DPM 표 수와 인덱스 수, KCM. 스팬은 엔드포인트별 갈림의 보조 (§7)
- [x] 계기 흔적: DPM table_count 16→17, index_count 31→33(1분 간격, 9일 동안 표 수 16 고정, F33-R 의 index_count 변화가 선례), 오류 문장의 표 이름, 그 시각에 롤아웃과 재시작 없음. 인공 지연 없음 (§7)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다 (§5)
- [x] 정답지 세 칸이 원칙 6대로 (§5)
- [x] 감지, 피해 판정, RCA 증거 구분과 인시던트 위험 (§6)
- [x] 주입 도구가 시나리오 id 를 남기지 않는다: 표 이름은 Vitess 꼴 접두사, 무작위 uuid, 시각뿐이고 인자와 스크립트에 id 없음(테스트로 고정). k6 태그는 tb-runner 의 k6 출력에만 남는다
- [x] 부하 상한과 서킷브레이커를 고려한 피해 계산, 가장 한가한 시간대 기준 (§9)

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- restaurant 로그에 'SQL Error: 1146, SQLState: 42S02' 와 "Table 'fooddelivery.menu_popularity_summary' doesn't exist" 가 실제로 남는지, 인기 메뉴 스팬이 500 이고 상세, 메뉴, 검색이 200 인지.
- VM 의 `dpm.mysql.database.table_count{db_name="fooddelivery"}` 가 주입 분에 17, `index_count` 가 33 이 되고 cleanup 뒤 16, 31 로 돌아오는지(보류 표 이름 바꾸기만으로는 값이 바뀌지 않고 그림자 표가 +1, +2 를 만든다).
- 인시던트: 묶음 멤버 템플릿과 판정 사유, promoted_at. caution 새 템플릿만 남아 cheap-gate 로 걸러졌다면 그 사실과 그 시각 restaurant 오류율을 검증 보고서에 남긴다(§6). promote 가 약하면 보강 후보는 동반 부하 rps 다.
- 주입이 메타데이터 잠금 대기로 실패(lock_wait_timeout 10초)하면 아무것도 바뀌지 않은 채 run 이 실패하고 MySQL 오류 첫 줄이 러너 로그에 남는다. 그림자 표만 만들어진 채 RENAME 이 실패했다면 cleanup 이 그림자를 지운다. 매시 약 48분 UTC 의 PopularMenuBatch(deleteAll 과 다시 채우기를 한 트랜잭션에서) 직후에 겹쳤는지 본다.
- cleanup 뒤 `menu_popularity_summary` 가 88행 그대로이고 `_vt_` 로 시작하는 표가 없는지(recovery 가 확인). 아니면 다음 무인 실행 전에 수동으로 되돌린다.
- 녹화 창에 food MySQL OOM 재시작이 끼면 녹화로 쓰지 않는다.
