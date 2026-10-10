---
title: F36-H 설계 시트 (food restaurant-service 를 운영에 없는 설정을 읽는 릴리스로 롤아웃해 가게 상세 전량 500, 주문 전량 502)
status: Draft
owner: project
last_reviewed: 2026-10-10
tags:
  - scenario
  - design
  - release
summary: food restaurant-service 를 결함 있는 새 릴리스(food-delivery-restaurant:1.6.0, fault-images/f36-h 패치로 만든 별도 태그)로 롤아웃하면, 가게 상세에 새로 더한 지역 영업 일시 중지가 요청마다 읽는 정책값이 릴리스의 개발 프로필 파일에만 있고 운영 설정에는 없어서, restaurant 가 Ready 인 채 가게 상세가 모두 500 이 되고 그것을 첫 단계에서 부르는 order 의 새 주문이 모두 502 로 실패하는 시나리오. 원본은 ServiceChannel 2025-05-08. F36-R(가게 표 열 이름 변경)과 증상이 같고 원인이 다른 H 짝.
---

# F36-H 설계 시트

## 1. 요약

food restaurant-service 의 새 릴리스 1.6.0 은 가게 상세(GET /api/restaurants/{id})에 "지역 단위 영업 일시 중지"(기상 악화, 배달 대행 중단 때 그 지역 가게를 CLOSED 로 보여 주문을 막음)를 더한다. 운영자가 재배포 없이 지역을 바꿀 수 있게 하겠다며 멈춘 지역 목록을 기동 때 묶지 않고 **요청마다** `Environment.getRequiredProperty("restaurant.policy.paused-regions")` 로 읽는다. 개발자는 그 값을 릴리스에 함께 넣은 `application-dev.yml`(dev 프로필, 빈 목록)에 적었고, 로컬과 개발 환경은 dev 프로필로 돌아 잘 동작했다. 운영 쪽(기본 `application.yml`, ConfigMap `service-config`, 파드 env, 활성 프로필 없음)에는 이 값이 옮겨지지 않았다.

기동과 `/actuator/health` 는 이 값을 읽지 않는다. 새 파드는 프로브를 모두 통과해 Ready 가 되고 기본 롤링 전략(maxSurge 25%)으로 옛 파드를 대신한다. 그 순간부터 모든 가게 상세 요청이 가게 행을 정상으로 읽은 다음 `IllegalStateException: Required key 'restaurant.policy.paused-regions' not found` 를 던지고, restaurant 의 `GlobalExceptionHandler` 가 `ServiceException` 만 다루므로 500 과 Tomcat SEVERE 가 된다. 메뉴, 인기 메뉴, 목록과 검색, health 는 이 코드를 지나지 않아 200 이다. order 는 주문 생성 첫 단계에서 가게 상세를 부르므로 'Failed to fetch restaurant N: 500' 뒤 재시도와 서킷 끝에 모든 주문이 502 다.

비유: 배달 앱의 가게 안내 데스크(restaurant)가 "날씨 나쁜 지역은 영업 중지로 안내하라" 는 새 지침을 받았는데, 중지 지역 목록이 연습용 사본(dev 프로필)에만 적혀 있다. 손님이 가게 상세를 물을 때마다 데스크는 본문에서 목록을 찾다 없어서 답을 못 하고, 그 답을 받아야 주문을 받는 접수처(order)도 모든 주문을 돌려보낸다. 메뉴판 코너와 가게 검색 코너는 멀쩡하다. 접수처가 가장 시끄럽지만 고칠 곳은 안내 데스크의 새 릴리스다.

## 2. 원본 사례

- **ServiceChannel, 2025-05-08** (공식 상태 페이지 사고와 사후 보고, 게시 2025-05-15): https://status.servicechannel.com/incidents/445h94td476q
  - 사고 제목 "Code Release Causes US Environment Outage". 예정된 미국 운영 코드 릴리스 뒤 2:29 AM EDT 부터 로그인 어려움(3:07 AM 까지)과 핵심 대시보드 기능 중단(4:12 AM 까지).
  - 대시보드 원인: 개발 환경에는 올바르게 설정된 설정 하나가 "had not been fully synchronized to the production environment", 새 코드가 그 설정에 접근하려다 실패. 이 틈은 "wasn't detected until the new code attempted to access the setting during the deployment".
  - 탐지: SRE 경보. 완화: 필요한 설정을 모두 운영에 적용해 회복(로그인은 롤백). 후속: 배포 절차에 설정 검증.
- `docs/ref-real-world-incidents.md` M2 에 이미 있다(F58-R 이 추가). 이번에 고칠 것 없음.
- F58-R 과 같은 원본이다. F58-R 은 결함이 증상 서비스(order) 자신에 있어 order 가 하류를 부르기 전에 스스로 500 이고, F36-H 는 결함이 피호출자(restaurant)에 있어 restaurant 의 500 이 order 의 502 로 번진다. 진입 경로(restaurant NodePort 둘러보기와 order 주문 둘), 영향 범위(가게 상세와 주문), 정답 서비스가 달라 카탈로그 §1 의 이름만 바꾼 복제가 아니고, k8s.image release 모드에 새 계약(restaurant 1.6.0)을 더해 같은 주입 중복도 아니다.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 예정된 운영 코드 릴리스 | restaurant-service 를 릴리스 food-delivery-restaurant:1.6.0 으로 롤아웃(KCM ScalingReplicaSet, 새 파드 Pulled 이벤트에 태그) |
| 원인이 된 결함 | 새 코드가 접근하는 설정이 개발 환경에만 있고 운영에 동기화되지 않음 | 가게 상세의 새 지역 영업 일시 중지 검사가 요청마다 읽는 `restaurant.policy.paused-regions` 가 릴리스의 `application-dev.yml` 에만 있고 운영 설정(application.yml, ConfigMap, env)에 없음 |
| 전파 경로 | 새 코드가 설정에 접근할 때 실패 → 그 기능(대시보드) 불가 | 가게 상세가 설정 조회에서 실패 → 처리되지 않은 예외로 500 → 그 응답을 기다리는 order 의 주문 생성이 502 |
| 사용자 증상 | 핵심 대시보드 기능 사용 불가 | 가게 상세 화면 전량 500, 새 주문 전량 502. 메뉴, 검색, 배달 조회는 정상 |
| 탐지된 경로 | 배포 중 SRE 경보 | restaurant, order 오류율과 SEVERE/ERROR 로그 급증으로 lucida 이상 탐지와 인시던트(원칙 7) |
| 완화와 복구 | 필요한 설정을 운영에 적용(로그인은 롤백) | cleanup 이 매니페스트 이미지로 되돌림(롤백). 쓴 데이터가 없어 되돌릴 것이 없다 |

기전 고리(새 코드 배포 → 운영에 없는 설정 접근 → 그 기능 실패)는 원본과 같다. 바꾼 것은 대상(대시보드 → 가게 상세와 그것에 기대는 주문)과 규모뿐이다.

## 3. 숫자 근거 (2026-10-10, `scenario-stats.py`, 정식 43 + 후보 35 = 78 → 79)

- 서비스(정식 + 후보): commerce 27, core-banking 26, **food-delivery 25 → 26**(가장 적은 서비스, 원칙 2).
- 묶음: **J(결함 있는 새 버전 배포) 11 → 12, 79 중 15.2%**(20% 미만). F58-R 처럼 결함이 "새 코드가 기대한 설정이 운영에 없음" 이라 J+G 로 적는다(G 로 세도 8 → 9, 11.4%). 금지 묶음 없음(가장 많은 J 14%, P 12%).
- 정답 위치: **가게 서비스 2 → 3, 3.8%**(F49-R, F42-P 뒤 셋째). 결제 경로 합계 12/79(15.2%), 바뀌지 않음. 가장 많은 위치는 주문 서비스 8(10%).
- 왜 이 후보인가:
  - 현실에서 가장 흔한 계기(바이너리 배포 37%, 설정 배포 31%, Google SRE Workbook)의 겹침이다.
  - 겉 증상 "가게 상세 500 → order 주문 502" 는 정식 F36-R(가게 표 열 이름 변경, MySQL 1054)의 녹화와 같다(119 2026-10-09 10:19~10:35: restaurant GET /api/restaurants/{id} ERROR 500 313건, order POST /api/orders ERROR 502 190건, order 'Failed to fetch restaurant N: 500' 500건). 정답이 다른 H 짝이라 관제 AI 가 "가게 상세 500 이면 가게 표" 를 외우지 못하고 롤아웃 대상, 예외 종류, 목록 성공 여부로 추론하게 한다.
  - 같은 원본의 F58-R 은 정답이 증상 서비스 자신이었다. F36-H 는 가장 시끄러운 order 가 정답이 아니어서 "오류가 가장 많은 서비스를 찍는" 지름길도 막는다.
- 이번 실행에서 막힌 후보, 장부 §4-1, §5, rejected 와 같은 원본·부품·주입이 아니다(ServiceChannel 의 restaurant 판은 처음이고 1.6.0 태그와 이 패치는 새 주입이다).

## 4. 인과 사슬 (코드와 인프라 위치)

1. 롤아웃: `k8s.image` release 모드가 `testbed-restaurant` 컨테이너 이미지를 `food-delivery-restaurant:latest` → `food-delivery-restaurant:1.6.0`(food-delivery/k8s/21-restaurant-deploy.yaml:27-28, imagePullPolicy Never, replicas 1 :9, nodeSelector tb-w3 :18-19, envFrom service-config :31-33, NodePort 30181).
2. 새 파드 기동: 프로브가 모두 `/actuator/health`(:67-86)라 정책 키를 읽지 않아 통과, Ready → 옛 파드 Killing.
3. 결함: `scripts/scenarios/fault-images/f36-h/restaurant-service.patch:36-39`(getRestaurant 에서 가게 행을 읽은 뒤 `environment.getRequiredProperty("restaurant.policy.paused-regions")`), `:42-51`(isPausedRegion), `:55-65`(application-dev.yml, dev 프로필에만 값). 운영 기본 설정 `food-delivery/restaurant-service/src/main/resources/application.yml:1-44` 에 `restaurant.policy` 가 없고 ConfigMap `food-delivery/k8s/02-configmaps.yaml:4-18`, 파드 env(:38-) 에도 없다. `SPRING_PROFILES_ACTIVE` 는 어디에도 없다.
4. 실패: `AbstractPropertyResolver.getRequiredProperty` 가 `IllegalStateException("Required key 'restaurant.policy.paused-regions' not found")` → `food-delivery/restaurant-service/src/main/java/com/fooddelivery/restaurant/config/GlobalExceptionHandler.java:9-21` 은 `ServiceException` 만 처리 → 500, Tomcat SEVERE. 읽기 전용 트랜잭션은 바로 롤백되어 풀을 잡지 않는다.
5. 전파: `food-delivery/order-service/src/main/java/com/fooddelivery/order/service/OrderService.java:60`(createOrder 첫 단계 가게 조회) → `order/client/RestaurantClient.java:29-56`(5xx 를 ERROR 'Failed to fetch restaurant N: 500 ...' 로 남기고 `ServiceException(BAD_GATEWAY)`, Retry 3회 200ms·400ms, 서킷 열림 뒤 'Restaurant service unavailable') → 502. 메뉴, 용량, 배차, 결제 호출과 주문 행 쓰기 전이다.

로컬 실측(2026-10-10, 패치 restaurant jar + origin/main order jar, mysql:8.0 + food init.sql, 운영과 같은 프로필):
- 가게 상세 20건 모두 500(p50 0.014초), 메뉴, 목록(검색), 인기 메뉴 각 20건 모두 200.
- 주문 2rps 240건 모두 502(p50 0.608초, 최대 0.757초), 시험 고객의 주문 행 0건. restaurant 로그 'Required key 'restaurant.policy.paused-regions' not found' 176줄(상세 직접 20 + order 재시도), order ERROR 'Failed to fetch restaurant N: 500 ...'.
- restaurant, order `/actuator/health` 내내 UP(db UP).
- 같은 jar 를 `--spring.profiles.active=dev` 로: 가게 상세 200(빈 목록이라 status 그대로). 개발 환경에서는 동작했다는 원본의 조건과 같다.

## 5. 원인 규정 (원칙 6)

- `root_cause.target_id`: `food-delivery-restaurant`(target_kind container). 결함을 가진 곳은 운영 설정 없이 나간 restaurant 릴리스이고, 되돌리거나 운영 설정을 더해야 재발이 막힌다. 원본 사후 보고가 지목한 층위(코드 릴리스와 설정 틈)와 같다.
- `trigger_target_id`: 비움(계기인 롤아웃과 근본이 같은 서비스).
- `scoring.partial`: order(`food-delivery-order`, `food-order`, `testbed-order`). 오류 로그와 502 가 가장 많이 보이는 증상 서비스라 원인 근처지만 정답이 아니다. order 는 정당하게 가게를 조회했을 뿐이다(원칙 6 의 두 번째 경우와 반대: 요청은 정당하고 피호출자 로직이 틀렸다).
- 원칙 5: 정답에 코드 설계 추론이 필요 없다. restaurant 롤아웃 시각과 바로 뒤 restaurant 의 'Required key ... not found' 로그만으로 "restaurant 새 릴리스가 운영에 없는 설정을 읽는다" 에 이른다.

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 | 내용 |
|---|---|---|
| 감지 | lucida 이상 탐지 | restaurant SEVERE 로그 급증(평시 restaurant 로그는 시간당 수 줄), restaurant 상세와 order 주문 서버 스팬 오류율 급증, order ERROR 'Failed to fetch restaurant' 급증, 'Created order' 소멸. 별도 업무 규칙 없이 겉 증상이 크다 |
| 피해 판정 | 러너 | 동반 부하 k6 의 주문 생성 5xx 비율 ≥ 0.5 와 2xx 비율 < 0.2(3틱 연속) |
| RCA | 녹화 데이터 | KCM 롤아웃과 Pulled 태그 1.6.0, restaurant 'Required key' SEVERE(앞에 SQL 오류 없음), 자식 SELECT 가 성공한 상세 500, 목록·메뉴 200, MySQL 무오류 |

## 7. 관측 근거 (119 실조회, 평시)

| 증거 | 119 표와 칸 | 조회 | 결과 |
|---|---|---|---|
| 계기: 롤아웃과 이미지 태그 | CH `lucida.kcm_events_local`(timestamp, reason, object_name, body) | `namespace='rca-testbed-food' AND reason IN ('Pulled','ScalingReplicaSet','Killing')` 30일, body 별 | food 의 Pulled 'Container image "food-delivery-dispatch:latest" already present on machine'(43건), ScalingReplicaSet 'Scaled up replica set testbed-dispatch-6cbcbcbcf5 to 1 from 0', Killing 이 2026-10-09 00:26~00:27 까지 수집됨(태그가 본문에 담김). testbed-restaurant 는 30일 동안 Unhealthy 60건뿐(롤아웃 없음) → 장애 때 1.6.0 Pulled 가 유일한 restaurant 롤아웃이다 |
| 계기: ReplicaSet, 파드 스펙의 이미지 | PG lucida `kcm_resources_history`(kind, name, yaml, captured_at) | `namespace='rca-testbed-food' AND name LIKE 'testbed-restaurant%'` 종류별 수와 `position('food-delivery-restaurant:latest' in yaml)>0` 수 | replicaset 25건, pod 19건 전부 :latest(마지막 2026-08-11 00:04). food 의 replicaset, pod 기록은 2026-10-09 00:27 까지 계속 쌓임 → 장애 때 1.6.0 ReplicaSet 이 남아야 한다 |
| 근본: 처리되지 않은 예외 로그가 수집됨 | CH `lucida.lucida_logs_local`(service_name, severity_text, body) | `service_name='food-delivery-restaurant' AND severity_text NOT IN ('INFO','DEBUG')` 30일 | restaurant 의 Tomcat 'Servlet.service() for servlet [dispatcherServlet] ... threw exception [...]' SEVERE 4,108건이 수집됨(F36-R 실행 2026-10-09 10:19~10:34 의 InvalidDataAccessResourceUsage 3,952건 등). 이 경로의 로그가 녹화에 남는다 |
| 근본: 새 문장이 평시에 없음 | 같은 표 | `body LIKE '%Required key%' OR body LIKE '%paused-regions%'` 30일 전 서비스 | 0건 |
| H 짝 감별: F36-R 의 앞선 SQL 오류 | 같은 표 | 같은 restaurant 비INFO 30일 | F36-R 은 SEVERE 마다 WARN 'SQL Error: 1054, SQLState: 42S22' 와 ERROR "Unknown column 'r1_0.region' in 'field list'" 가 앞선다(각 3,952건). F36-H 는 SQL 오류 없이 SEVERE 만 |
| H 짝 감별: 상세와 목록 | CH `otel_traces_local`(service_name, span_name, span_kind, status_code, span_attributes['http.response.status_code']) | F36-R 실행 창의 restaurant, order 서버 스팬 | F36-R: GET /api/restaurants/{id} ERROR 500 313, GET /api/restaurants ERROR 500 64, POST /api/orders ERROR 502 190, 메뉴 200. F36-H 는 목록이 200 이고 상세의 자식 'SELECT fooddelivery.restaurants' 스팬이 성공한다(로컬 실측 목록 200) |
| 전파: order 의 가게 조회 실패 | 같은 로그 표 | `service_name='food-delivery-order' AND body LIKE 'Failed to fetch restaurant%'` 30일 일별 | 2026-10-09 500건(F36-R 실행)뿐. 장애 때 같은 문장이 다시 나온다(본문만으로는 F36-R 과 구별 안 됨 → 감별은 restaurant 쪽 증거로) |
| 피해: 주문 생성량 | 같은 로그 표 | `service_name='food-delivery-order' AND body LIKE 'Created order%'` 시간별 24시간 | 시간당 63 ~ 4,145건. 장애 때 0 이 된다 |
| 평시 restaurant 로그량 | 같은 로그 표 | 최근 1시간 서비스별 | restaurant INFO 2줄(요청마다 로그를 남기지 않음). 장애 때 SEVERE 가 상세 요청 수만큼 늘어 이상 탐지에 걸린다 |

트레이스(10% 표본)와 APM 지표는 보조 증거로만 쓴다. 핵심 증거(롤아웃 이벤트, ReplicaSet 이미지, 'Required key' SEVERE 와 그 앞에 없는 SQL 오류, 'Created order' 소멸)는 전수 수집 표에 있다.

## 8. 감별

- must_support: 정답지 4개(restaurant 롤아웃과 1.6.0 Pulled, SQL 오류 없는 'Required key' SEVERE, 상세 500 과 목록·메뉴 200, order 'Failed to fetch restaurant' 와 502, Ready 와 무재시작).
- must_rule_out:
  - F36-R(가게 표 열 이름 변경): 1054 와 SQL 오류가 없고 목록과 검색이 200, 상세의 DB 조회 스팬이 성공, 롤아웃이 있다.
  - F49-R(restaurant 다른 릴리스, 응답 모양): restaurant 가 200 이 아니라 500 이고 order 로그가 'Error while extracting response' 가 아니다.
  - F44-R(tb-w3 방화벽): 연결 시간 초과가 아니라 500 응답이고 둘러보기 요청이 restaurant 에 닿는다.
  - F58-R, F49-H(order 릴리스): order 롤아웃이 없다.
  - restaurant 자원 고갈, 다운, DB 고장(F42-P, F25-H 꼴): Ready, 재시작 없음, 메뉴·목록 빠름, MySQL 무오류.
- contrast_with: F36-R, F49-R, F44-R, F58-R, F42-P(정답지에 각 차이).

## 9. 러너 판정 조건과 강도, 부하 계산

- 주입: `k8s.image` release 모드, 레벨 하나 `approved-fixed-f36-h`(min_hold 15m, settle 30s, timeout 20m). 강도 사다리 없음(설정이 없으면 모든 상세가 실패하는 이진 결함).
- 동반 부하: `load.north_south` order-surge.js, target_rps 2, entry 30181, ramp_up 2m, hold 21m(F36-R, F49-R 과 같은 값). 여정의 75% 가 주문(메뉴 조회 → 주문 생성, 초당 약 1.5건), 10% 가 가게 둘러보기(상세, 메뉴). 상주 기준선은 하루 주기로 주문 생성 시간당 63 ~ 4,145건이라 한가한 시간에도 판정 표본이 서게 한다.
- 부하 상한: 2rps 는 상한 180 의 1.1%. restaurant 상세는 0.01초 만에 500 이고, order 는 주문당 재시도 백오프 약 0.6초 동안 트랜잭션 연결 하나를 쥔다: 동시 약 1.5 × 0.6 ≈ 0.9 < order 풀 15 → order 풀과 readiness 가 묶이지 않는다(로컬 health 내내 UP). 서킷이 열리면(최소 5회, 실패율 50%, 5초) 그동안은 즉시 502 라 더 가볍다.
- 서킷브레이커: order 의 restaurant 서킷은 열림, 반열림을 되풀이하지만 반열림 시도도 모두 500 이라 피해가 줄지 않는다(모든 주문 502).
- success: `order_create_5xx_rate ≥ 0.5` 와 `order_create_2xx_rate < 0.2`, 3틱. must_rule_out: `achieved_rps < 0.5`, food MySQL 파드 NotReady, restaurant 파드 NotReady. abort: `entry_status == 0`(order, restaurant 모두 Ready 라 500, 502 가 남는다). recovery: target_health 200, MySQL·restaurant Ready, 기준선 2xx ≥ 0.7.
- restaurant 서버 스팬 5xx 비율은 표본(10%) 60초 창이라 판정 조건에 넣지 않는다(F49-R 은 배제 조건으로 썼지만 여기서는 restaurant 가 5xx 를 내는 것이 정상 경로다). 감별은 녹화된 전수 로그로 한다.
- cleanup: 매니페스트 이미지로 되돌리고 Deployment available, 1.6.0 을 쓰는 파드가 없어지면 tb-w3 containerd 에서 이름과 ID 참조 삭제. 장애 동안 쓴 데이터가 없다.
- 결함 이미지 규칙: 앱 소스와 매니페스트, 기본 이미지 그대로. 패치는 `food-delivery/restaurant-service/src/` 안만(테스트가 확인). 태그 1.6.0 은 저장소 어디에도 없다(restaurant 의 다른 릴리스는 F49-R 1.4.0, F42-P 1.5.0, 테스트가 확인). 추가 문자열에 시나리오 id, fault, bug, chaos, scenario 없음(테스트가 확인). 빌드는 `bash scripts/scenarios/fault-images/build.sh f36-h`.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(ServiceChannel 2025-05-08, 공식 상태 페이지 사후 보고, 링크)와 요소별 대응표가 있고 기전이 같다(§2)
- [x] 분류 장부를 갱신했고 묶음 J+G(J 12/79, 15.2%), 정답 위치 가게 서비스(3/79, 3.8%), 결제 경로 12/79(15.2%), 서비스 food 26(가장 적었음)과 고른 이유를 적었다. 어느 축도 20% 에 닿지 않는다(§3)
- [x] 근본 원인 위치가 패치 줄과 운영 설정 파일 줄로 확인됐다(§4)
- [x] 근본 원인 흔적이 119 에서 조회됐다: kcm_events_local 의 food 롤아웃과 Pulled 태그 수집, kcm_resources_history 의 restaurant ReplicaSet·파드 이미지, lucida_logs_local 의 restaurant Tomcat SEVERE 수집(§7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: KCM 이벤트, 리소스 이력, 전수 로그(§7)
- [x] 계기의 흔적이 조회됐다: 롤아웃 이벤트와 이미지 태그. 인공 지연 없음
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다(restaurant 롤아웃 + restaurant 'Required key' 로그)
- [x] 정답지 세 칸이 원칙 6대로 채워졌다(§5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다(§6)
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: 이미지 태그, 패치 문자열, 실행기 argv 에 없음(테스트)
- [x] 부하 상한과 서킷브레이커를 고려해 피해가 실제로 날 계산이 있다(§9, 로컬 실측 240/240 502)

### 후보 목록 (이번 반복, "실제 기전 × 부품", 숫자 순)

숫자: food 25(가장 적음) → banking 26 → commerce 27(정답 위치 0 부품만). 묶음 J 11(14%), P 10(13%)이 가장 많고 금지 없음. 정답 위치 상한 근처 없음(최대 주문 서비스 10%), 결제 경로 15%.

| 순위 | 후보(원본 / 부품 / 기전) | 결과 |
|---|---|---|
| 1 | ServiceChannel 2025-05-08 / food restaurant 가게 상세 / 운영에 동기화되지 않은 설정을 읽는 새 릴리스(피호출자 결함이 order 502 로 번짐, F36-R 의 H) | **채택** |
| 2 | ServiceChannel 2025-05-08 / banking transfer 이체 생성 / 같은 기전, commerce 정산까지 번짐 | 보류: banking 26 > food 25, 은행 이체 서비스 6 이고 transfer 릴리스 정답이 이미 셋(F17-H, F40-H, F42-R) |
| 3 | ServiceChannel 2025-05-08 / banking api 이체 접수 / 같은 기전 | 보류: banking, 오류가 증상 서비스 자신에서 나 추론 가치가 1 보다 낮음 |
| 4 | GitHub 2021-10-08 / banking account 이체 위임 응답 / 응답 구조 변경 릴리스로 api 해석 실패 | 보류: 같은 원본이 F49-R, F49-P 둘, 은행 계좌 서비스 4 와 account 릴리스 정답 둘(F41-R, F51-H) |
| 5 | ServiceChannel 2025-05-08 / food dispatch 용량 확인 / 같은 기전 | 보류: order 503 'Dispatch service unreachable' 증상의 시나리오가 이미 여섯(F32-R, F32-H, F33-H, F43-P, F55-R, F59-R) |
| 6 | Harness 2026-04-24(공식 https://status.harness.io/incidents/bsp454fy2v0p) / food outbox 정리, banking OutboxPurger·보존 배치 / 배치 없이 한 트랜잭션으로 지우는 정리 작업의 잠금 경합 | 버림(원칙 1, 9): 세 도메인의 정리 작업이 PK 목록이나 오래된 행만 지워 새 행 INSERT 와 잠금이 겹치지 않고, 보존 정리가 상시 따라잡고 있어 한 번에 지울 적체가 없다. 기간까지 바꾸면 되돌릴 수 없는 데이터 삭제다 |
| 7 | awork 2026-02-26(공식 상태 페이지) / food MySQL, banking Oracle / 성능 설정(SQL 재컴파일)이 한 질의 꼴과 겹쳐 DB CPU 100% | 버림(원칙 1): 실행마다 재컴파일하는 SQL Server 설정에 해당하는 것이 MySQL, Oracle 에 없어 기전이 바뀜 |
| 8 | Teslemetry 2026-02-27(공식 상태 페이지) / (서비스) / health 통과 뒤 크래시하는 릴리스 | 버림(원칙 1): 원본이 결함 내용을 밝히지 않음 |
| 9 | Learnosity 2025-08-19(공식 상태 페이지) / banking, food 유일성 검사 / v7 UUID 유일성 검사 교착 | 버림(원칙 1): 계기가 고객 요청 폭증이지 배포가 아니고, 테스트베드 id 가 정수 자동 증가 |
| 10 | KnowBe4 2025-11-24(pingoru 집계) / banking ACCOUNTS / 기본값 있는 열 추가 배포가 표를 잠금 | 버림: 2차 출처, Oracle 은 기본값 열 추가가 메타데이터만 바꿔 잠금이 없고, food 판은 order 풀이 묶여 입구 0(필수 중단 조건) |
| 11 | Clerk.io 2026-02-24(pingoru 집계) / (서비스) / 개발과 운영에서 다르게 동작한 기능 갱신 | 버림(원칙 1): 2차 출처뿐이고 기전 미공개. 같은 꼴의 공식 원본은 1(ServiceChannel)이 씀 |
