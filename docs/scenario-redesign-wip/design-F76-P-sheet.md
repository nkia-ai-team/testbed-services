---
title: F76-P 설계 시트 (food restaurant-service 릴리스의 예상 조리 시간 칸이 출시 카테고리 밖의 메뉴에서 예외를 내 그 가게들의 메뉴가 500)
status: Draft
owner: project
last_reviewed: 2026-10-12
tags:
  - scenario
  - design
  - release
summary: food restaurant-service 를 릴리스 1.7.0(fault-images/f76-p 패치로 만든 별도 태그)으로 롤아웃한다. 앱 메뉴 화면의 '조리 약 N분' 을 위해 메뉴 응답에 카테고리별 예상 조리 시간을 더했는데, 조리 시간 표가 출시 카테고리(분식, 중식, 치킨) 셋뿐이고 Map.get 결과를 확인 없이 int 로 받아 카테고리 4~8 의 메뉴를 담는 순간 NullPointerException 으로 500 이 된다. 운영의 대량 등록 가게 11곳의 메뉴가 모두 실패하고 가게 상세, 지역 검색, 인기 메뉴, 나머지 가게 메뉴와 주문은 정상이다. 원본은 F76-R 과 같은 GitLab 2026-03-17 production#21573, 기능 변경의 잘못된 호출이 애드온이 있는 네임스페이스의 프로젝트에서만 API 직렬화 중 예외를 낸 장애.
---

# F76-P 설계 시트

## 1. 요약

food restaurant-service 의 새 릴리스 1.7.0 은 "메뉴마다 조리 시간을 보여 주자"(앱 메뉴 화면의 '조리 약 N분')는 기능을 더한다. 메뉴 응답 형을 restaurant-service 안의 `MenuItemResponse` 로 바꿔 공통 메뉴 칸에 `cookMinutes` 를 더하고, 값을 매장 운영팀이 준 카테고리별 평균 조리 시간 표 `COOK_MINUTES_BY_CATEGORY`(1 분식 10, 2 중식 15, 3 치킨 20)에서 꺼낸다. 그런데 `toMenuResponse` 가 `COOK_MINUTES_BY_CATEGORY.get(m.getCategoryId())` 를 확인 없이 `int` 에 넣는다.

데모 시드(가게 1~3, 메뉴 15개)는 카테고리 1~3 만 써서 이 경로가 실패한 적이 없다. 운영의 대량 등록 가게(16~35)는 가게마다 메뉴 카테고리가 하나(`1 + id % 8`)이고 그 가운데 19~23, 27~31, 35 의 11곳이 카테고리 4~8(피자, 카페/디저트, 한식, 양식, 일식)이다(109 읽기 전용 조회). 기동과 `/actuator/health` 는 그대로라 새 파드가 Ready 가 되어 기본 롤링 전략으로 옛 파드를 대신하고, 그 뒤 그 11곳의 메뉴 응답은 만드는 중에 `Map.get` 이 null 을 돌려 자동 언박싱에서 `NullPointerException` 이 나 500 이 된다.

비유: 배달 앱의 가게 메뉴판 담당(restaurant)이 새 메뉴판에 "조리 약 N분" 칸을 더했다. 칸을 채우려고 운영팀이 준 '음식 종류별 조리 시간표' 를 보는데, 그 표에는 처음 문을 연 분식, 중식, 치킨 세 줄뿐이다. 표에 없는 종류면 멈추라는 확인을 빼먹었다. 시험 매장(데모 가게 셋)은 모두 그 세 종류라 문제가 없었지만, 나중에 입점한 피자집, 카페, 한식집 메뉴판은 통째로 인쇄가 멈춘다. 가게 간판(상세)과 지역 검색, 인기 메뉴는 이 칸을 쓰지 않아 멀쩡하고, 메뉴판이 나오는 가게의 주문도 그대로다. 고칠 곳은 확인 없이 칸을 채우게 만든 메뉴판 담당의 새 버전이다(되돌림).

## 2. 원본 사례

- **GitLab, 2026-03-17** (공식 운영 사고 기록 production#21573, incident.io INC-8530, 심각도 2): https://gitlab.com/gitlab-com/gl-infra/production/-/work_items/21573
  - 근본 원인: 최근의 Duo Code Review 기능 변경이 결함을 들였고 "an incorrect call to the Feature.enabled? method caused a NoMethodError during API serialization". 이 예외는 GitLab Credits 애드온이 있는 네임스페이스의 프로젝트에서만 났다.
  - 영향: 그 프로젝트들은 머지 리퀘스트를 만들 수 없고 /api/v4/projects 를 포함한 여러 API 엔드포인트로 접근하지 못함, "Over 15,000 errors occurred".
  - 완화: 프로젝트 단위로 GitLab Duo 를 끄는 우회책 안내, 근본 원인을 고친 되돌림(revert) 배포로 오류율 정상화.
  - 기록에 UTC 시각과 탐지 경로는 적혀 있지 않다.
- 자료 문서 `docs/ref-real-world-incidents.md` M2 에 F76-R 때 이미 들어가 있다(이번에 더한 것 없음).
- F76-R(commerce product 릴리스가 함량 표기 없는 액상 상품에서 응답을 못 만듦)과 같은 원본의 food 판이다. 결함 꼴(기능 변경의 새 코드 경로가 운영에만 있는 데이터 모양에서 응답 직렬화 중 예외)은 같고, 도메인, 서비스, DB 엔진, 데이터 축(메뉴 카테고리), 결함 호출(Map.get 결과의 자동 언박싱), 전파(게이트웨이 없이 NodePort, 주문 여정이 메뉴 단계에서 멈춤)가 다르다. 그래서 관계 접미사를 P 로 둔다(같은 원본을 다른 도메인에 옮긴 F63-R/F63-P, F70-R/F70-P 선례).

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | Duo Code Review 기능 변경 배포 | restaurant-service 릴리스 1.7.0 롤아웃(KCM ScalingReplicaSet, 새 파드 Pulled 이벤트에 태그 1.7.0) |
| 원인이 된 결함 | 새 코드의 잘못된 호출(`Feature.enabled?`)이 API 직렬화 중 NoMethodError(nil 에 메서드 호출) | 새 코드의 확인 빠진 조회(`COOK_MINUTES_BY_CATEGORY.get(...)` 를 int 로 받음)가 응답 변환 중 NullPointerException(null 의 `intValue()` 호출) |
| 결함이 드러나는 범위 | GitLab Credits 애드온이 있는 네임스페이스의 프로젝트만(그 기능 경로를 타는 데이터) | 표에 없는 카테고리(4~8)의 메뉴를 가진 가게만(운영 11곳, 데모 시드 0곳) |
| 전파 경로 | 그 프로젝트를 응답에 담는 API 와 MR 생성이 500 | 그 가게의 메뉴 응답 전체가 500, 앱은 메뉴가 열려야 주문으로 가므로 그 가게 고객은 주문까지 못 감 |
| 사용자 증상 | 해당 고객의 MR 생성, 프로젝트 API 실패(15,000건 넘는 오류), 다른 고객 정상 | 메뉴 보기의 절반 가까이 500(11곳 전부), 가게 상세, 지역 검색, 인기 메뉴, 나머지 12곳 메뉴와 주문 정상 |
| 탐지된 경로 | 적혀 있지 않음 | restaurant 오류율, SEVERE 로그 급증으로 lucida 이상 탐지와 인시던트(원칙 7) |
| 완화와 복구 | 기능 끄기 우회책, 변경 되돌림 | cleanup 이 restaurant 를 매니페스트 이미지로 되돌림. 쓴 데이터가 없다 |

기전 고리(기능 변경 배포 → 새 코드 경로의 호출 결함 → 그 경로를 타는 일부 데이터의 응답 직렬화에서 예외 → 그 데이터를 담는 API 500, 나머지는 정상 → 되돌림으로 복구)는 원본과 같다.

## 3. 숫자 근거 (2026-10-12, `scenario-stats.py`, 정식 52 + 후보 60 = 112 → 113)

- 서비스(정식 + 후보): commerce 38, core-banking 37, food-delivery 37 → 가장 적은 둘(은행, 음식배달 37 동률) 가운데 음식배달에서 만든다(원칙 2, 2026-10-12 결정) → **food 38**.
- 묶음: **J(결함 있는 새 버전 배포) 18 → 19, 113 중 16.8%**. 바뀐 것은 restaurant-service 이미지 하나이고 설정, 자원, DB, 데이터는 그대로라 G, A, P 가 아니고, 호출자 쪽 해석 실패가 아니라 릴리스 자신이 특정 데이터에서 응답을 못 만드는 것이라 L 이 아니다. 금지 묶음 없음(최다 J 19, D 17).
- 정답 위치: **가게 서비스 3 → 4, 113 중 3.5%**. 결제 경로 합계 13/113(11.5%, 무관). 20% 에 닿는 위치 없음(최다 은행 이체 서비스 10, 8.8%).
- 부품 지도(food): mysql 15, dispatch 14, payment 5, order 4, restaurant 4 → 5, notify 1, kafka 0(kafka 는 outbox 비동기 벽, notify 는 무동작 소비자 벽으로 앞선 반복들이 막혔다).
- 왜 이 후보인가:
  - 현실에서 가장 흔한 계기(바이너리 배포 37%, Google SRE Workbook)이고, "새 코드 경로가 운영에만 있는 데이터 모양에서 실패" 는 food 에 없던 결함 꼴이다. food 의 기존 J 는 응답 구조 변경(F49-R, F49-P, F49-H), 무거운 질의(F42-P, F33-H), 연결 누수(F43-P), 설정 키 누락(F58-R, F36-H), 요청 한도(F55-R), 스레드 누수(F63-P), 경로 이동(F75-R)이다.
  - 실패가 부하나 자원이 아니라 데이터를 따른다(같은 메뉴 엔드포인트가 가게에 따라 200 과 500). 같은 '메뉴 읽기 실패' 의 F36-P(메뉴 표 메타데이터 잠금 대기)와 정답이 갈리는 짝이라 관제 AI 가 "메뉴가 깨지면 메뉴 표" 를 외워 찍지 못하고 롤아웃 시각과 새 예외, 실패하는 요청의 공통점으로 추론해야 한다.
  - 같은 restaurant 릴리스 F36-H(설정 누락으로 모든 상세 500, 주문 502), F49-R(상세 구조 변경으로 order 해석 실패)과 실패 끝점(메뉴), 예외(NullPointerException), 범위(일부 가게), 주문 영향(없음)이 다르다.
- 이번 실행에서 막힌 후보, 장부 §4-1, §5, rejected 와 같은 원본 사례·같은 주입이 아니다(GitLab 2026-03-17 은 F76-R 이 commerce product 에 쓴 원본이고, restaurant 1.7.0 태그와 이 패치는 새 주입이다).

## 4. 인과 사슬 (코드와 인프라 위치)

1. 롤아웃: `k8s.image` release 모드가 `testbed-restaurant` 컨테이너 이미지를 `food-delivery-restaurant:latest` → `food-delivery-restaurant:1.7.0`(food-delivery/k8s/21-restaurant-deploy.yaml:27-28, imagePullPolicy Never, replicas 1 :9, nodeSelector tb-w3 :18-19).
2. 새 restaurant 파드 기동: 프로브가 모두 `/actuator/health` 라 통과, Ready → 기본 전략(maxSurge 25%)으로 옛 파드 교체.
3. 결함: `scripts/scenarios/fault-images/f76-p/restaurant-service.patch:71-75`(`COOK_MINUTES_BY_CATEGORY = Map.of(1L, 10, 2L, 15, 3L, 20)`), `:93-106`(`toMenuResponse`: `int cookMinutes = COOK_MINUTES_BY_CATEGORY.get(m.getCategoryId());`), `:30-46`(`MenuItemResponse`), `:17-21`(메뉴 끝점이 새 형을 돌려줌).
4. 데이터: food-delivery/db/init.sql:212-239(데모 시드 가게 1~3, 메뉴 1~15 의 카테고리 1~3), :257-271(대량 등록 메뉴 `category_id = 1 + (r.id % 8)`). 109 food MySQL 읽기 전용 조회(menus 95행): 가게 1~3 카테고리 1~3, 16·24·32 → 1, 17·25·33 → 2, 18·26·34 → 3, 19·27·35 → 4, 20·28 → 5, 21·29 → 6, 22·30 → 7, 23·31 → 8, `category_id IS NULL` 0.
5. 실패 범위: `RestaurantService.getMenu`(food-delivery/restaurant-service/src/main/java/com/fooddelivery/restaurant/service/RestaurantService.java:62-69)가 가게의 메뉴마다 `toMenuResponse` 를 부르므로 메뉴 하나의 예외가 응답 전체를 500 으로 만든다. 처리기(GlobalExceptionHandler)는 ServiceException 만 다뤄 NullPointerException 은 Tomcat 까지 올라가 SEVERE 와 500. 가게 상세, 목록 검색, 인기 메뉴는 다른 응답 형을 써 바뀌지 않는다.
6. 주문: k6 주문 여정(food-delivery/loadgen/script.js:120-126, surge.js:110-114)은 메뉴 조회가 200 일 때만 주문을 보내고, order 는 메뉴를 공통 `MenuResponse`(shop-common, cookMinutes 없음)로 읽어 더한 칸을 무시한다(OrderService.java:71-91, RestClient.builder() 기본 Jackson 은 모르는 칸을 무시). 그래서 주문은 메뉴가 열리는 가게(카테고리 1~3)에서만 오고 모두 평소대로다. 11곳 고객은 주문까지 가지 못한다.

로컬 실측(2026-10-12, MySQL 8.0 을 같은 init.sql 로 초기화, origin/main 5804eef food-delivery 사본에 패치를 얹어 만든 restaurant jar 와 매니페스트 order jar, dispatch·payment 는 띄우지 않음):

| 요청 | 릴리스 jar 결과 |
|---|---|
| `GET /api/restaurants/{id}/menu`, 가게 1~3, 16~18, 24~26, 32~34 | 12곳 200(cookMinutes 10/15/20 포함) |
| 같은 요청, 가게 19~23, 27~31, 35 | **11곳 500** |
| `GET /api/restaurants/{id}`, `/popular-menu`, 23곳 | 모두 200 |
| `GET /api/restaurants?region=GANGNAM|NOWON|GURO&status=OPEN&page=0&size=20` | 200 |
| `POST /api/orders` 가게 16(정상 카테고리) | 메뉴 해석을 지나 용량 확인 단계에서 503(dispatch 를 띄우지 않아서) → 공통 MenuResponse 로 읽는 데 문제 없음 |
| `POST /api/orders` 가게 19(실패 카테고리, k6 는 보내지 않는 경우) | order 'Failed to fetch menu for restaurant 19: 500' 재시도 3회 뒤 502 |

릴리스 jar 로그: `ERROR ... o.a.c.c.C.[.[.[/].[dispatcherServlet] : Servlet.service() for servlet [dispatcherServlet] in context with path [] threw exception [Request processing failed: java.lang.NullPointerException: Cannot invoke "java.lang.Integer.intValue()" because the return value of "java.util.Map.get(Object)" is null] with root cause`. 운영에서는 이 로그가 OTel 로그 수집기로 `severity_text = SEVERE` 로 들어간다(아래 §7, 2026-10-09 restaurant 의 같은 꼴 SEVERE 3,952줄이 수집됨). 매니페스트 restaurant 는 같은 요청 모두 200 이다(119 1일 메뉴 서버 스팬 표본 29,243건 모두 200).

메뉴 단계 실패 비율 예상치: k6 가 가게를 3/23 확률로 1~3, 나머지는 16~35 에서 고르므로 (20/23) × (11/20) = **11/23 ≈ 0.48**(기준선 script.js 와 동반 surge.js 같은 고르기).

## 5. 원인 규정 (원칙 6)

- `root_cause.target_id`: `food-delivery-restaurant`(target_kind container). 결함을 가진 곳은 응답 변환에 실패 처리 없는 조회를 들인 restaurant 릴리스이고, 되돌리거나 고쳐야 재발이 막힌다. 카테고리 4~8 의 메뉴는 운영의 정당한 데이터다("A 가 정당하고 B 의 로직이 잘못" → B). 원본 기록이 지목한 층위(기능 변경이 들인 잘못된 호출, 되돌림으로 복구)와 같다.
- `trigger_target_id`: 비움(계기인 롤아웃과 근본이 같은 서비스).
- `scoring.partial`: 비움. 증상이 근본과 같은 서비스(restaurant 자신의 500)에서 나고, 메뉴 데이터나 MySQL 은 바뀌지 않았고 정상이라 부분 점수에 넣지 않는다(F58-R 선례).
- 원칙 5: 정답에 코드 설계 추론이 필요 없다. restaurant 롤아웃 시각, 바로 뒤 restaurant 자신의 SEVERE `NullPointerException ... Map.get(Object) is null`(스택에 toMenuResponse), 실패가 일부 가게의 메뉴에만 나고 MySQL 과 호출자가 멀쩡하다는 것으로 "restaurant 새 릴리스가 일부 메뉴 응답을 못 만든다" 에 이른다.

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 | 내용 |
|---|---|---|
| 감지 | lucida 이상 탐지 | restaurant 서버 스팬 오류율(500) 급증, restaurant APM 오류율 0 → 상승, restaurant SEVERE 로그 급증. 별도 업무 규칙 없이 겉 증상이 크다(메뉴 보기 절반 가까이 실패) |
| 피해 판정 | 러너 | 동반 부하 메뉴 단계(step menu) 4xx+5xx 비율 ≥ 0.25(3틱 연속, 예상 약 0.48, 평시 0 근처) |
| RCA | 녹화 데이터 | KCM restaurant 롤아웃과 Pulled 태그 1.7.0, restaurant SEVERE NullPointerException(스택 toMenuResponse), restaurant GET /api/restaurants/{id}/menu 500, 상세·검색·인기 메뉴·주문 정상, MySQL·order·dispatch·payment 정상 |

## 7. 관측 근거 (119 실조회, 평시)

| 증거 | 119 표와 칸 | 조회 | 결과 |
|---|---|---|---|
| 계기: restaurant 롤아웃과 이미지 태그 | CH `lucida.kcm_events_local`(timestamp, namespace, object_kind, object_name, reason, body) | 30일 `object_name='testbed-restaurant' AND reason='ScalingReplicaSet'`, 14일 `reason='Pulled' AND body LIKE '%:1.%'` | testbed-restaurant 롤아웃 0건(30일). food 롤아웃은 같은 표에 수집된다(30일 testbed-payment ScalingReplicaSet 119, testbed-dispatch 32, testbed-order 8). 릴리스 태그는 Pulled 본문에 남는다: 2026-10-12 15:16:39 `testbed-api-8dc7cb754-bdfv7` 'Container image "core-banking-api:1.4.0" already present on machine', 2026-10-11 20:22:18 `testbed-cart-...` 'commerce-cart:1.2.0'. 장애 때는 'food-delivery-restaurant:1.7.0' 이 본문에 담긴다. 같은 표 Deployment ScalingReplicaSet 예: 2026-10-12 15:34:42 testbed-api 'Scaled up replica set testbed-api-bdd59894b to 1 from 0' |
| 근본: restaurant 의 응답 변환 예외 | CH `lucida.lucida_logs_local`(service_name, severity_text, body) | 30일 모든 서비스 `body LIKE '%because the return value of "java.util.Map.get(Object)" is null%'`, 7일 restaurant WARN 이상 | 0건. restaurant 의 SEVERE 7일: 'Servlet.service() for servlet [dispatcherServlet] in context with path [] threw exception [Request processing failed: org.springframework.dao.InvalidDataAccessR...' 3,952(2026-10-09 10:19~10:34, 열 이름 변경 시나리오), 'Request processing failed: org.springframework.transaction.CannotCrea...' 143(2026-10-06~08 DB 연결 실패), 마지막 SEVERE 2026-10-09 10:34:56. 처리되지 않은 예외의 Tomcat SEVERE 가 restaurant 에서 전수 수집됨을 확인했다 |
| 전파: restaurant 메뉴 서버 스팬 상태 | CH `lucida.otel_traces_local`(service_name, span_name, span_kind, span_attributes['http.response.status_code']) | 1일, restaurant `span_name='GET /api/restaurants/{id}/menu'` 상태별 | 200 29,243건(500 없음). 같은 1일 restaurant SERVER `GET /api/restaurants/{id}` 22,984, CLIENT `SELECT fooddelivery.menus` 29,235. 장애 때 메뉴가 500 으로 바뀌고 상세는 그대로다(표본 10%, 보조 증거) |
| 전파: restaurant APM 오류율 | VM `apm.agent.otel.java.error_rate{service_name="food-delivery-restaurant"}` | 즉시 질의 | 시계열 있음, 값 0(2026-10-12). 장애 때 오른다(표본 기반 보조 증거) |
| 데이터 모양(관제 데이터 아님, 설계 확인용) | 109 food MySQL 읽기 전용 | `SELECT restaurant_id, category_id, count(*) ... FROM menus GROUP BY 1,2`(95행 표), `SELECT status, count(*) FROM restaurants`(23행) | 가게마다 카테고리 하나, 4~8 은 19~23, 27~31, 35 의 11곳, NULL 0. 가게 OPEN 22 / CLOSED 1(24, 카테고리 1) |
| 피해 범위가 DB 아님 | CH `lucida.dpm_topsql_local`(engine, body JSON sqlText, calls, avgExecTime) | 1시간 food MySQL 상위 질의 | restaurant 질의는 `SELECT COUNT(*) FROM restaurants WHERE id = ?`(existsById) 3,942회 평균 0.27ms 가 상위이고 메뉴 질의는 상위에 없다(95행 표). 릴리스는 질의를 바꾸지 않아 장애 때도 같은 질의, 같은 시간이어야 한다 |

트레이스(10% 표본)와 APM 지표는 보조 증거로만 쓴다. 핵심 증거(롤아웃 이벤트, restaurant SEVERE 예외 로그, 다른 경로 정상)는 전수 수집 표에 있다.

## 8. 감별

- must_support: 정답지 4개(restaurant 롤아웃과 1.7.0 Pulled, restaurant SEVERE NullPointerException 과 toMenuResponse 스택, 메뉴 서버 스팬 500 과 APM 오류율 상승, 실패가 가게 카테고리를 따르고 상세·검색·인기 메뉴·주문·DB 정상).
- must_rule_out:
  - F53-R, F36-P, F36-R 꼴(메뉴 표 지우기, 잠금 대기, 열 이름 변경): SQL 오류와 DDL, 메타데이터 잠금 대기가 없고 같은 표를 읽는 정상 가게의 메뉴가 200.
  - F36-H(restaurant 설정 누락): 예외가 설정 조회가 아니라 응답 변환의 NullPointerException, 가게 상세가 그대로, 실패가 일부 가게 메뉴에만.
  - F49-R(restaurant 응답 구조 변경으로 order 해석 실패): 실패가 restaurant 자신의 500 이고 order 에 실패 로그가 없다.
  - F42-P, F48-R(인기 메뉴 집계, 그 표): 인기 메뉴가 200 이고 MySQL 부하와 질의가 그대로.
  - F41-R, F63-P 꼴(자원, 재시작): 지연 증가, 재시작, OOM 이 없고 실패가 카테고리를 따른다.
- contrast_with: F76-R, F36-H, F49-R, F36-P, F42-P(정답지에 각 차이).

## 9. 러너 판정 조건과 강도, 부하 계산

- 주입: `k8s.image` release 모드, 레벨 하나 `approved-fixed-f76-p`(min_hold 8m, settle 60s, timeout 14m, max_injection_duration 20m). 강도 사다리 없음(표에 없는 카테고리면 매번 실패하는 결정적 결함).
- 동반 부하: `load.north_south` food surge.js, target_rps 3, entry 30181(RESTAURANT_URL), ramp_up 2m, hold 16m, seed 7616(F32-R, F32-H 와 같은 rps, 스크립트). 여정 45% 가 상세 → 메뉴 → 30% 인기 메뉴 둘러보기, 30% 지역 검색, 10% 제한된 주문, 15% 배달 추적. 메뉴 단계(step menu)는 초당 약 1.35건이라 60초 창마다 표본 약 80건.
- 판정은 동반 부하의 메뉴 단계(`loadgen.read_step_status_rate`, food 의 read_step = menu)로 한다. 예상 실패 비율 약 0.48, 문턱 0.25. 상주 기준선 메뉴 단계는 기록과 회복 판정에만 쓴다(하루 주기로 1~6 iter/s 라 한가한 시간 표본이 적다).
- 부하 상한: 3rps 는 상한 180 의 1.7%. 예외는 질의가 끝난 뒤 응답 변환에서 몇 ms 안에 나 restaurant 스레드, 풀, MySQL 에 더 얹는 부담이 없다.
- 서킷브레이커: restaurant 앞에는 게이트웨이가 없고 k6 가 NodePort 로 직접 부른다. order 의 restaurant 서킷은 k6 가 메뉴가 열린 가게만 주문해 order 의 getMenu 가 성공하므로 열리지 않는다(로컬에서 실패 가게로 직접 주문하면 재시도 3회 뒤 서킷이 열리는 것도 확인했지만 k6 여정은 그 경로를 타지 않는다). 그래서 실패가 서킷으로 가려지거나 번지지 않고 데이터에 따라 일정하다.
- success: `menu_nonok_rate ≥ 0.25`, 3틱. must_rule_out: `achieved_rps < 1`, food MySQL NotReady, restaurant NotReady(2틱). abort: `entry_status == 0`(order 주문 생성 단계 연결 불가, 이 주입은 order 를 건드리지 않는다). recovery: target_health 200, restaurant Ready, MySQL Ready, 기준선 메뉴 단계 실패 < 0.1.
- 주문 생성 5xx 는 기록만 한다: 한낮에 배달원 용량(2000)이 차면 503 이 섞일 수 있어 판정에 쓰지 않는다(rejected 20행의 용량 시간 흔들림).
- cleanup: restaurant 를 매니페스트 이미지로 되돌리고 Deployment available, 1.7.0 을 쓰는 파드가 없어지면 tb-w3 containerd 에서 이름과 ID 참조 삭제. 장애 동안 쓴 데이터가 없다.
- 결함 이미지 규칙: 앱 소스와 매니페스트, 기본 이미지 그대로. 패치는 `food-delivery/restaurant-service/src/` 안만(테스트가 확인, origin/main 사본에 `git apply --check` 통과, 같은 사본으로 빌드한 jar 로 §4 실측). 태그 1.7.0 은 저장소 어디에도 없다(restaurant 의 다른 릴리스 1.4.0, 1.5.0, 1.6.0 은 F49-R, F42-P, F36-H). 추가 문자열에 시나리오 id, fault, bug, chaos, scenario 없음(테스트가 확인). 빌드는 `bash scripts/scenarios/fault-images/build.sh f76-p`(배포 스크립트가 녹화 구간 확인 뒤 실행).

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(GitLab 2026-03-17 production#21573, 공식 운영 사고 기록, 링크)와 요소별 대응표가 있고 기전이 같다(§2)
- [x] 분류 장부를 갱신했고 묶음 J(19/113, 16.8%), 정답 위치 가게 서비스(4/113, 3.5%), 결제 경로 13/113(11.5%), 서비스 food 38(은행과 37 동률에서)과 고른 이유를 적었다. 어느 축도 20% 에 닿지 않는다(§3)
- [x] 근본 원인 위치가 패치 줄과 restaurant, order 소스 줄, 시드 줄로 확인됐다(§4)
- [x] 근본 원인 흔적이 119 에서 조회됐다: kcm_events_local 의 food 롤아웃과 Pulled 태그, lucida_logs_local 의 restaurant SEVERE 수집, otel_traces_local 의 메뉴 경로 상태, VM restaurant APM 오류율(§7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: KCM 이벤트와 전수 로그(§7)
- [x] 계기의 흔적이 조회됐다: 롤아웃 이벤트와 이미지 태그. 인공 지연 없음
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다(restaurant 롤아웃 + restaurant 자신의 예외 + 데이터를 따르는 실패)
- [x] 정답지 세 칸이 원칙 6대로 채워졌다(§5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다(§6)
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: 이미지 태그, 패치 문자열, 실행기 argv 에 없음(테스트)
- [x] 부하 상한과 서킷브레이커를 고려해 피해가 실제로 날 계산이 있다(§4 로컬 실측, §9)

### 후보 목록 (이번 반복, "실제 기전 × 부품", 숫자 순)

숫자: commerce 38, banking 37, food 37 → 은행이나 음식배달. 묶음 J 18(16%), D 17(15%)이 가장 많고 금지 없음, 0 인 묶음 N. 정답 위치 상한 근처 없음(최다 은행 이체 서비스 10, 9%), 결제 경로 11.6%. 부품 지도 0: banking·food kafka, banking nginx(앞선 반려의 벽: outbox 비동기, nginx 119 미수집).

| 순위 | 후보(원본 / 부품 / 기전) | 결과 |
|---|---|---|
| 1 | GitLab 2026-03-17 production#21573 / food restaurant-service 메뉴 / 릴리스의 새 코드 경로가 운영에만 있는 데이터 모양(출시 카테고리 밖의 메뉴)에서 응답 변환 중 예외, 그 가게들의 메뉴 500 | **채택**(가게 서비스 3, restaurant 부품 4) |
| 2 | 같은 원본 / food order-service 주문 합계 / 지역별 배달비 표가 출시 지역(강남, 홍대, 잠실) 셋뿐이라 확장 지역 가게 12곳 주문이 500 | 보류: 동작은 성립(주문의 약 52% 500)하지만 정답 위치 주문 서비스가 8 로 가게 서비스(3)보다 많고, 같은 order 릴리스 F58-R(주문 생성 첫 단계에 더한 정책 확인이 모든 주문 500)과 계기·실패 지점·증상이 겹친다 |
| 3 | 같은 원본 / food dispatch-service 배차 / 이미 보내지던 region 을 권역 표로 읽다 확장 지역 배차 500 → order 503, 서킷 열림 | 보류: dispatch 부품이 14 로 food 최다이고 dispatch 릴리스가 이미 다섯(F33-H, F43-P, F55-R, F63-P, F75-R), 'dispatch 릴리스 → 주문 503' 관측 모양이 F75-R, F55-R 과 같다 |
| 4 | KINTO Technologies 2024-04-18(공식 기술 블로그 https://blog.kinto-technologies.com/posts/2024-12-11-mysql-deadlock-en/ , 중복 결제 확인 SELECT ... FOR UPDATE 가 0행이면 갭 잠금, 단조 증가 키의 동시 INSERT 가 교착, 결제 실패) / food payment + MySQL payments / 릴리스가 같은 꼴의 중복 확인을 더함 | 버림: 원칙 9. 교착은 두 트랜잭션이 확인과 INSERT 사이 창에서 겹쳐야 나는데, food 주문(=결제)은 배달원 용량(2000, ETA 15~34분) 때문에 지속 가능한 속도가 초당 약 1.4건이라 창 수 ms 에서 겹칠 확률이 1% 안팎이고 order 의 결제 재시도(2회)가 가린다. banking Oracle, commerce PostgreSQL 은 갭 잠금이 없어 기전이 성립하지 않는다 |
| 5 | GitLab 2026-07-27 INC-12449(production#22584, 한 사용자의 다운로드 폭주가 GET 마다의 동기 갱신으로 적은 행에 잠금 대기열, PgBouncer 풀 포화로 플랫폼 전체 503) / banking transfer + ACCOUNTS 정산 계좌 행 / 제휴사의 일괄 이체 폭주가 한 계좌 행 FOR UPDATE 로 줄 섬 | 버림: 원칙 1, 9. 원본 결함은 읽기마다의 불필요한 부기 쓰기인데 banking 이체의 계좌 행 잠금은 필요한 쓰기라 고칠 곳이 다르고, 한 행 직렬 처리량(트랜잭션 수 ms 기준 초당 150~300건)이 부하 상한 180rps 근처라 피해가 확정되지 않는다 |
| 6 | GitHub 2025-01-09(배포가 들인 질의가 주 DB 포화) / banking ledger 소비자 / 릴리스가 소비자 동시성 3(파티션 3)과 이벤트마다 무거운 질의 | 버림: 원칙 9, 7. 세 세션이 Oracle 2 CPU 를 나눠 써도 짧은 OLTP 가 몇 ms 늘 뿐 5xx 계산이 서지 않고(rejected 53, 272행 벽), 같은 원본이 F42-R, F42-P, F33-H 에 있다 |
| 7 | Flagsmith 2022-08-18(초 단위 값을 ms 로 넘긴 시간 제한) / banking transfer JPA 질의 시간 제한 / 거래 내역 질의가 ORA-01013 으로 취소 | 버림: 원칙 1, 카탈로그 §1. 원본 고리는 3ms 시간 제한 + 재시도 증폭이 비싼 끝점으로 DB 를 과부하시킨 것이고, 남는 꼴(거래 내역만 실패)이 F64-P 와 같다 |
| 8 | GitLab 2026-03-17 / banking transfer 정산 계좌 / 릴리스가 입금 후 잔액을 int 로 바꿔 6,800억 원대 commerce 정산 계좌에서 ArithmeticException | 버림: 카탈로그 §1. 같은 서비스, 같은 피해 경로(commerce 정산 이체만 실패)가 F40-H 에 있다 |
| 9 | Zendesk 2023-02-14(내부 경로 설정 배포의 짧은 요청 시간 제한, 재시도로 중복 이벤트) / food order → dispatch / 시간 제한 축소 + 재시도 중복 배차(묶음 N 0) | 버림: 카탈로그 §1, 원칙 9. 같은 꼴이 rejected(F08-P 꼴)에 있고 dispatch 응답이 ms 단위라 시간 제한을 지어내야 하며 용량 도달 시간이 흔들린다 |
| 10 | Intercom 2024-11-05(새 서버 이미지의 캐싱 resolver 가 사설 DNS 를 못 풀어 백그라운드 워커만 실패) / banking ledger, food notify 소비자 | 버림: 원칙 7. 우리 백그라운드 워커가 모두 비동기 소비자라 사용자 증상이 없다(rejected 253행 벽) |
