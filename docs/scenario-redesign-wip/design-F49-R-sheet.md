---
title: F49-R 설계 시트 (food restaurant-service 를 가게 상세 응답 구조를 바꾼 릴리스로 롤아웃)
status: Draft
owner: project
last_reviewed: 2026-10-09
tags:
  - scenario
  - design
  - release
summary: food restaurant-service 를 결함 있는 새 릴리스(food-delivery-restaurant:1.4.0, fault-images/f49-r 패치로 만든 별도 태그)로 롤아웃하면, 공개 API 정비로 가게 상세 응답의 status 가 문자열에서 {code, label} 객체로 바뀌어 restaurant 는 200 으로 답하고 Ready 인데 그 응답을 읽는 order 가 해석에 실패해 모든 새 주문이 502 로 실패하는 시나리오. 원본은 GitHub 2021-10-08 Codespaces API 응답 구조 변경으로 기존 클라이언트가 깨진 장애.
---

# F49-R 설계 시트

## 1. 요약

food restaurant-service 의 새 릴리스 1.4.0 은 공개 API 를 정비하면서 가게 상세 `GET /api/restaurants/{id}` 의 응답을 새 `RestaurantDetailResponse` 로 바꾼다. 앱 화면이 영업 상태를 바로 보여 주도록 `status` 를 문자열 `"OPEN"` 대신 `{"code":"OPEN","label":"영업 중"}` 객체로 내려준다. 메뉴, 인기 메뉴, 목록, health 끝점과 DB 는 그대로라 새 파드는 프로브를 통과하고 기본 롤링 전략(maxSurge 25%)으로 옛 파드를 대신하며, 상세 요청마다 200 으로 답한다. 그런데 이 끝점의 유일한 내부 호출자인 order-service 는 응답을 공용 DTO `RestaurantResponse`(status 는 `String`)로 읽는다. Jackson 이 객체를 문자열로 바꾸지 못해 RestClient 가 `Error while extracting response for type [com.fooddelivery.common.dto.RestaurantResponse] and content type [application/json]` 을 던지고, order 는 `Failed to fetch restaurant <id>: ...` ERROR 를 남기고 502 를 돌려준다. 재시도 3회와 restaurant 서킷브레이커는 같은 해석 실패를 되풀이할 뿐이라, 모든 주문이 첫 단계(가게 조회)에서 약 0.6초 만에 아무것도 쓰지 않고 실패한다.

비유: 가게 안내판(restaurant)이 새 디자인으로 바뀌어 "영업중" 글자 대신 "영업 상태: [코드] OPEN / [표시] 영업 중" 이라는 표를 걸었다. 손님(사람 화면)은 잘 읽지만, 안내판에서 "OPEN" 한 단어만 읽도록 만들어진 주문 접수 기계(order)는 표를 읽지 못해 모든 주문을 돌려보낸다. 안내판은 멀쩡히 서 있고, 고칠 곳은 안내판을 바꾼 새 릴리스다.

## 2. 원본 사례

- **GitHub, 2021-10-08** (공식 월간 가용성 보고, 2021년 10월): https://github.blog/2021-11-03-github-availability-report-october-2021/
- 17:16 UTC 부터 1시간 36분. 공개 API 출시 과정에서 Codespaces 의 핵심 API 응답 하나가 의도치 않게 구조가 바뀌었고("inadvertently restructured"), 안정된 스키마("stable schema")에 기대던 기존 API 클라이언트가 깨졌다. VS Code 데스크톱 클라이언트에서 새 Codespace 를 시작할 수 없었고, 웹 편집기와 기존 데스크톱 세션은 저하됐다. 모니터링이 처음에는 영향을 잡지 못했고, 사고 중 시작된 무관한 배포가 되돌림을 늦췄다. 회귀를 되돌려 모든 클라이언트가 다시 연결됐다. 재발 방지로 확장의 API 사용에 대한 종단 간 시험 도구와 내부 서비스 경계의 모니터링을 늘린다고 했다.
- 자료 문서 `ref-real-world-incidents.md` M8 표에 이 사례를 더했다(출처가 말한 사실만).

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 공개 API 출시를 위한 Codespaces 서비스 변경 배포 | restaurant-service 를 릴리스 food-delivery-restaurant:1.4.0(공개 API 정비)으로 롤아웃(KCM ScalingReplicaSet, 새 파드 Pulled 이벤트에 태그가 남음) |
| 원인이 된 결함 | 핵심 API 응답 구조가 의도치 않게 바뀜(기존 클라이언트가 기대던 안정 스키마를 깸) | 가게 상세 응답의 status 가 문자열에서 {code, label} 객체로 바뀜(호출자가 기대던 `RestaurantResponse.status: String` 을 깸) |
| 전파 경로 | 응답을 읽는 기존 클라이언트(VS Code 확장)가 해석 실패 → 새 Codespace 시작 불가 | 응답을 읽는 order 의 RestaurantClient 가 해석 실패 → 재시도, 서킷 → 주문 생성 첫 단계에서 502 |
| 사용자 증상 | 데스크톱에서 새 Codespace 시작 불가, 기존 세션과 웹 편집기 저하. 서비스 자체는 응답함 | 모든 새 주문이 502. 가게 둘러보기, 메뉴, 검색, 배달 조회는 정상이고 restaurant 는 200 으로 답함 |
| 원본의 탐지 경로 | 모니터링이 처음엔 못 잡음(클라이언트 쪽 실패), 내부 서비스 경계 모니터링을 늘리기로 함 | lucida-next 의 order 오류율, order ERROR 로그 급증. restaurant 서버 스팬은 200 이라 서비스 경계(호출자 쪽)에서만 보인다 |
| 완화와 복구 | 회귀 되돌림 | cleanup 이 restaurant 이미지를 매니페스트 이미지로 되돌림(롤백) |

기전 동일성: 원본과 재구성 모두 "API 제공자의 배포가 응답 구조를 바꿨고, 제공자는 정상 응답하는데 그 구조에 기대던 기존 클라이언트가 해석에 실패해 기능이 멈춘다"는 고리다. 대상(Codespaces API → 가게 상세 API)과 클라이언트(VS Code 확장 → order-service)만 우리 스택에 맞게 바꿨다. 결함은 원본처럼 한 응답, 한 필드 꼴로 작게 두었다.

## 3. 숫자 근거 (2026-10-09, `scenario-stats.py`, 정식 42 + 후보 12 = 54)

- 서비스(정식 + 후보): commerce 26, core-banking 16, **food-delivery 12(가장 적음)** → 13
- 묶음: A 7, B 7, C 6, D 7(각 12%), G 6, J 5, F 3, **L 3** → 4(55 중 7.3%), E 2, H 2, K 2, I 1, M 1, O 1, P 1, N 0. J 로 세도 5→6(10.9%). 20% 상한(11) 아래
- 정답 위치: **가게 서비스 0** → 1(§2-1 에 이미 있는 말, 부품 지도 food restaurant 1 은 DB 테이블(가게)). 최다는 외부 결제 의존, 주문 서비스 각 6(11%)
- 결제 경로 합계 10(18.5%) → 그대로 10/55(18.2%)
- 왜 이 후보인가: N(0) 묶음 후보는 모두 막혔고(§10 버린 후보), 그다음 우선인 "정답 위치 0" 이면서 "가장 적은 서비스" 를 동시에 채우는 후보다. 같은 원본과 재구성의 반복 6 후보 F45-R 은 '롤아웃 서비스가 정답' 비공식 축(장부 공식 축 아님)과 증상 겹침만으로 폐기됐고, 2026-10-09 사용자 정정으로 둘 다 버릴 사유가 아니게 되어 이번 실행의 막힌 목록에서도 빠졌다(F45 는 재사용 금지라 F49). 증상(order 의 가게 조회 실패로 주문 502)이 F36-R, F44-R 과 같다는 것은 카탈로그 관계 H 로서 추론을 요구하는 가치이고, 셋을 가르는 관측 근거는 §8 에 적는다.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 계기: `rca-testbed-food` Deployment `testbed-restaurant` 이미지 `food-delivery-restaurant:latest` → `food-delivery-restaurant:1.4.0`(`food-delivery/k8s/21-restaurant-deploy.yaml:27-28`, imagePullPolicy Never, nodeSelector tb-w3 `:18-19`, 기본 전략 maxSurge 25%, 109 kubectl 2026-10-09).
2. 결함(릴리스 1.4.0): `scripts/scenarios/fault-images/f49-r/restaurant-service.patch:17-22`(RestaurantController.getRestaurant 가 RestaurantDetailResponse 를 돌려줌), `:38-44`(RestaurantDetailResponse, status 가 Status(code, label) 객체), `:62-78`(RestaurantService.getRestaurantDetail, statusLabel). 매니페스트 버전은 `food-delivery/restaurant-service/src/main/java/com/fooddelivery/restaurant/controller/RestaurantController.java:37-40` 에서 공용 `RestaurantResponse` 를 그대로 돌려준다.
3. 계약: `food-delivery/shop-common/src/main/java/com/fooddelivery/common/dto/RestaurantResponse.java:3-8`(status 는 String).
4. 호출자: `food-delivery/order-service/src/main/java/com/fooddelivery/order/service/OrderService.java:56-70`(createOrder 첫 단계 getRestaurant, status 비교) → `order/client/RestaurantClient.java:29-46`(`body(RestaurantResponse.class)` 해석 실패 → RestClientException → `log.error("Failed to fetch restaurant {}: {}")` → ServiceException 502). 재시도 3회 200ms 지수, 서킷 10건 창 50% 5초 열림(`order-service/src/main/resources/application.yml:50-110`). 서킷이 열린 동안에도 바깥 재시도가 백오프만큼 기다려 응답은 약 0.6초.
5. 영향 밖: 메뉴(`/menu`), 인기 메뉴, 목록은 공용 DTO 그대로라 loadgen 의 둘러보기, 주문 여정의 메뉴 조회는 200. 주문은 가게 조회에서 끝나 dispatch, payment 를 부르지 않는다.

## 5. 원인 규정 (원칙 6)

- `root_cause.target_id`: `food-delivery-restaurant`(target_kind container). 결함을 가진 곳이자 고쳐야(되돌려야) 재발이 막히는 곳은 응답 계약을 깬 restaurant 새 릴리스다. order 의 요청 A(가게 조회)는 정당했고, 그것을 받은 B(restaurant 1.4.0)의 응답이 계약을 어겼으므로 원칙 6 의 "B 의 로직이 잘못" 경우다. 원본 포스트모템도 응답을 바꾼 API 쪽 회귀를 되돌렸다(같은 층위). 결함 이미지 규칙 7(근본은 그 서비스의 새 버전)과도 맞는다. F46-R 처럼 배포 절차(마이그레이션) 결함이 아니라 코드 변경 자체가 결함이라 두 규칙이 충돌하지 않는다.
- `trigger_target_id`: 없음(계기인 롤아웃과 근본이 같은 곳).
- `scoring.partial`: `food-delivery-order`, `food-order`, `testbed-order`(증상을 낸 곳, 해석 실패 로그가 나는 곳).
- 코드 줄(패치의 어느 줄)을 맞히라고 요구하지 않는다(원칙 5). "restaurant 롤아웃 직후 order 가 restaurant 응답을 해석 못 함 → restaurant 릴리스" 는 관제 데이터(KCM 이벤트, order 로그, 스팬 상태)로 낼 수 있는 결론이다.

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 | 기대 |
|---|---|---|
| 감지(lucida-next) | order 서버 스팬 오류율(POST /api/orders 502), order ERROR 로그 급증(해석 실패 문장, 평시 0) | 별도 규칙 없이 이벤트, 인시던트 생성. 같은 꼴(order 502 + order 오류 로그)의 F36-R 이 정식 녹화됐다 |
| 피해 판정(러너) | 동반 부하 k6 문서의 주문 생성 5xx 비율 ≥ 0.5, 2xx 비율 < 0.2 (3틱) | 평시 5xx 0 근처, 장애 시 1.0 |
| 원인 설명(RCA) | KCM 롤아웃 이벤트와 이미지 태그, order 로그 문장(대상 DTO 이름), restaurant 스팬 200, order 클라이언트 스팬 200 | §7 |

## 7. 관측 근거 (119 실조회, 평시)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 핵심 증거는 KCM 이벤트, 자원 이력, 전수 로그다.

| 증거 | 119 표와 칸 | 조회 | 결과(2026-10-09) |
|---|---|---|---|
| 계기: 롤아웃과 이미지 태그 | CH `lucida.kcm_events_local`(reason, object_name, body) | `namespace='rca-testbed-food' AND reason IN ('Pulled','ScalingReplicaSet','Killing',...)` 최근 2일 | food 의 Pulled 'Container image "food-delivery-dispatch:latest" already present on machine', ScalingReplicaSet 'Scaled up replica set testbed-dispatch-6cbcbcbcf5 to 1', Killing 이 2026-10-09 00:26~00:27 에 수집됨(태그가 본문에 담김). testbed-restaurant 는 30일 동안 Unhealthy 60건뿐(롤아웃 없음) |
| 계기: ReplicaSet, 파드 스펙의 이미지 | PG lucida `kcm_resources_history`(kind, name, yaml, captured_at) | `namespace='rca-testbed-food' AND name LIKE 'testbed-restaurant%'` 종류별 수와 `position('food-delivery-restaurant:latest' in yaml)>0` 수 | replicaset 25건, pod 19건 전부 :latest(마지막 2026-08-11 00:04). food 의 ReplicaSet 은 2026-10-09 00:27 까지 계속 쌓임(dispatch) → 장애 때 1.4.0 ReplicaSet 이 남아야 한다 |
| 전파: order 해석 실패 로그 | CH `lucida.lucida_logs_local`(service_name, severity_text, body) | `body LIKE '%Error while extracting response%'` 30일, 서비스와 문장별 | food-delivery-order 의 RestaurantResponse 대상 문장 0건(있는 것은 10-09 03:36 DispatchResponse octet-stream 154건, commerce 2건). `Failed to fetch restaurant%` 는 30일 동안 10-09 하루 500건(다른 시나리오의 restaurant 500 'Whitelabel Error Page') |
| 전파: order 주문 스팬 | CH `otel_traces_local`(service_name, span_name, span_attributes['http.response.status_code']) | order `POST /api/orders` 서버 스팬 7일 | 20,427건 중 5xx 530건(다른 시나리오 구간) |
| 감별: restaurant 는 200 | CH `otel_traces_local` | restaurant `GET /api/restaurants/{id}` 서버 스팬 7일, order 의 CLIENT `GET` server.address=testbed-restaurant | restaurant 상세 76,020건 중 5xx 316건, order → restaurant 클라이언트 스팬 39,723건 중 200 39,686건. 장애 때는 이 둘이 200 인데 order 서버 스팬만 502 여야 한다 |

로컬 실측(2026-10-09, 104, mysql:8.0 + food init.sql, origin/main 9479b65 로 빌드한 order jar 와 패치를 얹은 restaurant jar, OTel 에이전트 없음):
- 릴리스 restaurant: `GET /api/restaurants/1` → 200 `{"id":1,...,"status":{"code":"OPEN","label":"영업 중"}}`, 메뉴와 목록 200, health UP.
- 주문 2rps 240건: 502 240건, p50 0.61초, p95 0.63초, 최대 0.71초. order 로그 `Failed to fetch restaurant 2: Error while extracting response for type [com.fooddelivery.common.dto.RestaurantResponse] and content type [application/json]` 71줄(서킷이 열린 동안의 호출은 로그 없이 fallback). order, restaurant health 내내 UP.
- 같은 order 에 매니페스트 restaurant jar: 가게 조회를 통과해 다음 단계(dispatch, 로컬에 없음)에서 503 'Dispatch service unreachable' — 502 는 릴리스 때문임을 확인.

## 8. 감별

- must_support: 정답지 `must_support` 4개(롤아웃 이벤트와 태그, order 해석 실패 로그, order 502 와 restaurant 200 의 대비, restaurant Ready·DB 정상).
- must_rule_out: F36-R, F48-R(가게 표 고장: restaurant 가 1054/1146 으로 500), F44-R(경로 차단: Connect timed out), F32-R, F32-H, F33-R(배달 쪽: 503 'Dispatch service unreachable'), order 자체 결함이나 설정 배포, restaurant 다운이나 과부하.
- contrast_with: F32-H, F36-R, F44-R, F30-R, F47-R.
- **같은 증상, 다른 정답(관계 H)을 가르는 관측 근거**: F36-R, F44-R, F49-R 은 모두 "order 가 가게 조회에 실패해 주문 502" 다. 가르는 것은 (1) restaurant 서버 스팬 상태: F36-R 은 500, F44-R 과 F49-R 은 요청이 닿으면 200 (2) order 로그 문장: F36-R '500 ... Whitelabel Error Page', F44-R 'I/O error ... Connect timed out', F49-R 'Error while extracting response for type [...RestaurantResponse]' (3) 계기: F36-R DPM 의 열 이름 변경, F44-R syslog 의 '[FW BLOCK]', F49-R KCM 의 testbed-restaurant 롤아웃과 1.4.0 태그. F32-H 와는 같은 '피호출자 형식 변경' 틀이지만 계기(env 설정 배포 대 이미지), 피호출자(dispatch 대 restaurant), 증상 코드(503 대 502)가 다르다.

## 9. 러너 판정 조건과 강도, 부하 계산

- 주입: k8s.image release 모드, 설계 강도 1단 고정 evaluation(`approved-fixed-f49-r`, min_hold 15m, settle 30s, timeout 20m, max_injection_duration 25m). 강도 사다리 없음, 첫 실행이 곧 녹화 실행.
- success(3틱): 동반 부하 주문 생성 5xx ≥ 0.5, 2xx < 0.2.
- must_rule_out(2틱): achieved_rps < 0.5(부하 끊김), food MySQL NotReady, restaurant NotReady, restaurant 서버 스팬 5xx ≥ 10%(restaurant 자신이 5xx 면 F36-R 꼴).
- abort: entry_status == 0(2틱). order 는 해석 실패를 약 0.6초에 502 로 돌려주고 DB 연결은 그동안만 잡혀(주문 초당 약 1.6~3건 × 0.6초 ≈ 1~2개, 풀 15) readiness 가 빠지지 않는다. restaurant 는 health 가 그대로라 Ready 다. 0 은 노드나 order 파드가 죽었을 때만 나온다.
- 부하: load.north_south `order-surge.js` 2rps(주문 생성 초당 약 1.4건, F36-R, F44-R 과 같은 값). 피해는 부하 크기와 상관없이 모든 주문이 실패하는 결정적 꼴이라 강도 계산은 "주문이 들어오기만 하면 된다" 이다. 상주 기준선만으로는 새벽에 주문이 초당 약 0.2건이라 러너 표본과 로그 신호를 시간대와 무관하게 하려고 동반 부하를 둔다. 180rps 상한과 무관.
- 롤아웃 시간: tb-w3 에서 새 restaurant JVM 기동과 readiness 통과까지 약 1분(옛 파드는 그동안 서비스). 그 뒤 즉시 전량 실패.
- 배포 전제: 109 docker 에 `food-delivery-restaurant:1.4.0` 이 있어야 preflight 가 통과한다(`bash scripts/scenarios/fault-images/build.sh f49-r`, 배포 단계에서 실행). tb-w3 이미지 파일시스템 52%(2026-10-09, 상한 80%).

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(GitHub 2021-10-08, 공식 월간 가용성 보고 링크)와 요소별 대응표, 기전 동일(§2)
- [x] 분류 장부 갱신(§4-1 행, §6 기록), 묶음 L+J(L 3→4, 7.3%), 정답 위치 가게 서비스(0→1), 결제 경로 10(18.2%), 서비스 food 12→13(가장 적음), 고른 이유(§3). 어느 축도 20% 미만
- [x] 근본 원인 위치 `file:line` 확인(패치 줄, 매니페스트 코드, 공용 DTO, order 클라이언트)과 인프라 지점(21-restaurant-deploy.yaml)(§4)
- [x] 근본 원인 흔적 119 실조회: kcm_events_local 의 롤아웃, Pulled 본문 태그 수집, kcm_resources_history 의 restaurant ReplicaSet, 파드 이미지(§7)
- [x] 핵심 증거가 표본에만 있지 않음: KCM 이벤트, 자원 이력, 전수 로그(order 해석 실패 문장). 스팬은 보조
- [x] 계기 흔적: 롤아웃 이벤트와 새 이미지 태그. 인공 지연 없음(응답은 빠르고 결함은 응답 구조)
- [x] 정답이 관제 데이터로 낼 수 있는 결론(롤아웃 직후 호출자 해석 실패, 피호출자 정상), 코드 설계 결함 추론 불필요(§5)
- [x] 정답지 세 칸이 원칙 6 대로(근본 restaurant 릴리스, 계기 같음, 부분 점수 order)
- [x] 감지, 피해 판정, RCA 증거 구분(§6)
- [x] 주입 도구가 시나리오 id 를 남기지 않음: 실행기 인자는 네임스페이스, Deployment, 컨테이너, 이미지 둘뿐이고, 패치가 더한 문자열에 시나리오 id, fault, bug, chaos 같은 말이 없음(테스트 `test_k8s_image_release_mode_rolls_food_restaurant` 가 확인)
- [x] 부하 상한과 서킷브레이커를 고려한 피해 계산(§9: 서킷이 열려도 fallback 502, 결정적 전량 실패, 로컬 240/240)

### 버린 후보 (이번 반복, 숫자 순으로 줄 세운 뒤 관문에서 막힌 것)

1. N 묶음(0) 재시도 증폭 계열: 이번 실행의 막힌 목록(GitHub 2026-05-05 결제 경로 상한, GitHub 2026-08-17 은 겹침만이라 풀렸으나 재시도 3배로 180rps 안 포화 계산 불성립, 원칙 9).
2. Jamf School 2025-09-24, Atlassian Statuspage 2019-08-12 × banking Oracle transfers 비온라인 인덱스 생성이 표 잠금으로 이체 INSERT 를 막음(정답 위치 DB 테이블(이체) 0): 원칙 9. 로컬 Oracle Free 23-slim(2 CPU, transfers 631만 행 712MB) 실측으로 비온라인 CREATE INDEX 가 9.9초, 14.2초에 끝나 표 잠금이 십여 초뿐이다. 오래 막으려면 긴 트랜잭션 보유자를 따로 주입해야 하고(Oracle 은 ddl_lock_timeout 0 이 기본이라 DDL 이 바로 실패), 원본의 자연 보유자(작업자 잡 활동)가 banking 에 없다.
3. GoCardless 2015, Atlassian Statuspage 2019 × commerce orders 표 ALTER 잠금 대기열(DB 테이블(주문) 0): 컨트롤러 필수 중단 조건과 원칙 9. commerce order liveness 가 DB 를 보는 /actuator/health 라 풀이 마르면 75초마다 재시작하고, 클라이언트가 죽어도 잠금을 기다리는 백엔드가 남아 15분이면 기본 max_connections 100 에 다가간다(commerce 전 서비스로 번져 원인이 가려짐).
4. 같은 잠금 대기열 × food dispatches(MySQL 메타데이터 잠금): 원칙 9, 중단 조건. dispatch liveness 재시작마다 대기 스레드가 쌓여 max_connections 151 에 다가가고(반려 기록의 측정), 정답 위치 DB 테이블(배차) 1 이라 0 인 위 후보들보다 뒤.
5. GitHub 2026-05-04 × food dispatches 온라인 스키마 마이그레이션 부하로 DB 포화: 원칙 9 미확정(MySQL 0.5 CPU 에서 재구축 스레드가 OLTP 를 얼마나 늦출지 계산이 서지 않음), F33-R 과 같은 MySQL 포화 꼴. 이 후보보다 숫자 순위가 낮아(정답 위치 1) 실측 전에 뒤로 둠.
6. Octopus Deploy 2025-11-25 × food dispatch 연결 누수 릴리스, Honeycomb 2019-11-06 × food dispatch 메모리 누수 릴리스: 카탈로그 §1(F43-R, F41-R 과 같은 원본, 같은 결함, 서비스만 바꿈).
7. PostHog 2025-10-24 × commerce gateway 레이트 리미터 오설정: F34-R 교훈(429 단일 신호, 오류율 0).
8. Cloudflare 2025-11-18 × (권한 변경으로 중복 행, 고정 한도 초과): 원칙 1. 앱 코드에 메타데이터를 읽어 고정 한도에 담는 경로가 없어 재현하려면 원본에 없는 코드를 심어야 함.
