---
title: F75-R 설계 시트 (food dispatch-service 만 용량 API 경로를 옮긴 릴리스로 롤아웃해 짝 order 릴리스 없이 주문 생성 전량 503)
status: Draft
owner: project
last_reviewed: 2026-10-12
tags:
  - scenario
  - design
  - release
summary: order-service 2.6.0 과 짝으로 나가야 했던 food dispatch-service 릴리스 1.10.0(fault-images/f75-r 패치로 만든 별도 태그)만 롤아웃되어, 배달원 용량 조회가 /api/deliveries/capacity 에서 /api/couriers/capacity 로 옮겨진다. 바뀌지 않은 order 의 용량 확인이 옛 경로로 가 새 dispatch 의 GET /api/deliveries/{id} 에 걸려 400 을 받고, order 와 dispatch 가 모두 Ready 인 채 모든 새 주문이 503 이 되는 시나리오. 원본은 Stitch 2023-03-13, 정기 배포에서 일부 내부 서비스만 배포되어 서비스 사이 인터페이스 버전이 어긋난 장애.
---

# F75-R 설계 시트

## 1. 요약

food dispatch-service 의 새 릴리스 1.10.0 은 "배달원 용량은 배차 한 건이 아니라 배달원 풀의 상태" 라는 정리로 용량 조회를 배차 자원에서 새 배달원 자원으로 옮긴다. 새 `CourierController` 가 `GET /api/couriers/capacity`(본문 그대로)를 내고, `DispatchController` 의 `GET /api/deliveries/capacity` 를 지운다. 주문 생성 전 용량 확인을 새 경로로 바꾼 짝 릴리스 order-service 2.6.0 이 같은 배포에서 나가야 했는데, 나가지 않았다(원본에서는 배포 도구 문제로 일부 서비스만 배포됨).

dispatch 의 기동과 `/actuator/health` 는 그대로라 새 파드가 Ready 가 되고 기본 롤링 전략(maxSurge 25%)으로 옛 파드를 대신한다. 그 순간부터 매니페스트 이미지 그대로인 order 가 주문마다 `GET /api/deliveries/capacity` 를 부르면, 새 dispatch 는 그 매핑이 없어 경로를 `GET /api/deliveries/{id}` 에 맞추고 `capacity` 를 `Long` 으로 바꾸지 못해 400 을 돌려준다(WARN `Resolved [org.springframework.web.method.annotation.MethodArgumentTypeMismatchException: Method parameter 'id': Failed to convert value of type 'java.lang.String' to required type 'java.lang.Long'; For input string: "capacity"]`). order `DispatchClient` 가 ERROR `Failed to check dispatch capacity: 400 ...` 를 남기고 502 로 바꾸며, 재시도 3회와 dispatch 서킷 대체 응답 끝에 주문이 약 0.65초 만에 503 `Dispatch service unavailable` 이 된다. 용량 확인은 주문 행 저장 전이라 주문, 배차, 결제가 남지 않는다.

비유: 배차실(dispatch)이 "배달원 현황판" 을 배차 창구에서 새 배달원 창구로 옮기고 옛 창구 팻말을 떼었다. 주문 접수처(order)에도 "새 창구로 가라" 는 공문이 같은 날 가야 했는데 배달이 안 됐다. 접수처 직원은 늘 하던 대로 옛 창구에 가서 "현황판 주세요" 하고, 그 창구는 "배차 번호 'capacity' 는 없는 번호" 라며 돌려보낸다. 접수처는 배달원을 확인하지 못해 주문을 모두 돌려보낸다. 배차실은 멀쩡히 일한다. 고칠 곳은 짝 공문 없이 창구를 옮긴 배차실의 새 릴리스다(되돌리거나 짝 릴리스를 함께 낸다).

## 2. 원본 사례

- **Stitch, 2023-03-13** (공식 상태 페이지 사고 "Stitch API Downtime due to deploy" 와 사후 설명): https://status.stitch.money/incidents/rqsn1ccn5dsb
  - 근본 원인: "During a routine deployment, not all internal services were successfully deployed." "This meant that the interface versions used to communicate between one another became out of sync." 배포가 덜 된 계기는 정기 배포 중 GitHub Action 문제.
  - 영향: "timeouts and bad request statuses (400-403) across all products and clients", "The outage lasted approximately 24 minutes." 상태 페이지 갱신 10:25 AM, 11:03 AM(시간대 표기 없음).
  - 완화: "A manual redeployment of services was done to ensure that all services were aligned", 실패한 Action 재실행. 후속: 버전이 어긋나도 동작하도록 서비스 배포 규칙 강화.
  - 어느 서비스, 어느 인터페이스였는지와 탐지 경로는 적혀 있지 않다.
- `docs/ref-real-world-incidents.md` M8(데이터 형식 불일치, 버전 비호환)에 이번에 추가했다(출처가 말한 사실만).
- 같은 꼴의 설계 수준 근거(사고 보고 아님): GitLab Gitaly 이슈 6934, Rails 를 Gitaly 보다 먼저 배포하면 앞뒤 호환이 없는 gRPC 호출이 롤아웃 창 내내 실패한다.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 정기 배포에서 일부 내부 서비스만 배포됨(GitHub Action 문제) | 짝 릴리스(dispatch 1.10.0 + order 2.6.0) 중 dispatch 만 롤아웃(KCM ScalingReplicaSet, 새 파드 Pulled 이벤트에 태그 1.10.0), order 는 그대로 |
| 원인이 된 결함 | 서비스 사이 통신 인터페이스 버전이 어긋남 | dispatch 가 용량 API 를 새 경로로 옮기고 옛 경로를 지웠는데, 옛 order 가 옛 경로를 부름 |
| 전파 경로 | 어긋난 인터페이스로 가는 내부 호출 실패 → 제품 전반 요청 실패 | 옛 경로가 `/api/deliveries/{id}` 에 걸려 400 → order 용량 확인 실패 → 재시도, 서킷 → 주문 503 |
| 사용자 증상 | 모든 제품과 고객에게 시간 초과와 400~403(약 24분) | food 의 모든 새 주문이 503. 둘러보기, 메뉴, 검색, 주문 조회, 배달 조회는 정상 |
| 탐지된 경로 | 적혀 있지 않음 | order 오류율, ERROR 로그 급증으로 lucida 이상 탐지와 인시던트(원칙 7) |
| 완화와 복구 | 모든 서비스를 다시 배포해 버전을 맞춤 | cleanup 이 dispatch 를 매니페스트 이미지로 되돌림(버전 맞춤). 쓴 데이터가 없다 |

기전 고리(짝 배포가 한쪽만 나감 → 서비스 사이 인터페이스 어긋남 → 내부 호출이 4xx 로 실패 → 사용자 요청 실패)는 원본과 같다. 바꾼 것은 규모(내부 서비스 여럿 → dispatch, order 둘)와 대상뿐이다.

## 3. 숫자 근거 (2026-10-12, `scenario-stats.py`, 정식 52 + 후보 58 = 110 → 111)

- 서비스(정식 + 후보): commerce 37, core-banking 37, **food-delivery 36 → 37**(가장 적은 서비스, 원칙 2).
- 묶음: **L(데이터 형식 불일치) 8 → 9, 111 중 8.1%**. 보내는 쪽과 받는 쪽이 기대하는 인터페이스가 어긋난 장애라 L, 계기가 dispatch 새 버전 배포라 J 를 덧붙여 L+J(F49-R, F49-P, F49-H 와 같은 표기, J 는 주 묶음이 아니라 17 그대로). 금지 묶음 없음(최다 D, J 각 17, 15%).
- 정답 위치: **배달 서비스 7 → 8, 111 중 7.2%**. 결제 경로 합계 13/111(11.7%, 무관). 20% 에 닿는 위치 없음.
- 왜 이 후보인가:
  - 현실에서 가장 흔한 계기(바이너리 배포 37%, Google SRE Workbook)와 하위 호환 문제(Ghosh 2022 버그의 14.6%, Liu 2019 버그의 21%)의 겹침이다. "두 서비스 바이너리의 배포 짝이 어긋남" 은 테스트베드에 없던 결함 꼴이다(기존 L 은 응답 구조, 시각 표기, 숫자 표기, JSON 명명, 스키마 열 이름).
  - 겉 증상(주문 전량 503, order 로그에 dispatch 용량 확인 실패)은 F49-H 와 같고 정답이 반대다: F49-H 는 order 가 롤아웃되어 order 가 정답, F75-R 은 dispatch 가 롤아웃되어 dispatch 가 정답. 관제 AI 가 "용량 확인 실패면 dispatch" 나 "order 가 계약을 깼다" 를 외워 찍지 못하고, 어느 쪽 롤아웃 뒤에 실패가 시작됐는지로 추론해야 한다(카탈로그 관계 H 의 가치). F32-R, F55-R, F63-P, F70-P, F44-P 와도 증상이 같고 결정 증거(dispatch 가 돌려준 400, dispatch 롤아웃)로 갈린다.
- 이번 실행에서 막힌 후보, 장부 §4-1, §5, rejected 와 같은 원본 사례나 같은 주입이 아니다(Stitch 는 처음 쓰는 원본이고, dispatch 1.10.0 태그와 이 패치는 새 주입이다).

## 4. 인과 사슬 (코드와 인프라 위치)

1. 롤아웃: `k8s.image` release 모드가 `testbed-dispatch` 컨테이너 이미지를 `food-delivery-dispatch:latest` → `food-delivery-dispatch:1.10.0`(food-delivery/k8s/22-dispatch-deploy.yaml:27, imagePullPolicy Never, replicas 1 :9, nodeSelector tb-w3). order(food-delivery/k8s/20-order-deploy.yaml:27-28) 는 그대로.
2. 새 dispatch 파드 기동: 프로브가 모두 `/actuator/health`(:67-86)라 통과, Ready → 옛 파드 Killing(평시 롤아웃 약 42초: KCM 2026-10-11 06:46:15 새 ReplicaSet → 06:46:57 옛 ReplicaSet 축소).
3. 결함: `scripts/scenarios/fault-images/f75-r/dispatch-service.patch:17-39`(새 `CourierController`, `@RequestMapping("/api/couriers")` + `@GetMapping("/capacity")`, 주석 "주문 생성 전 용량 확인은 order-service 2.6.0 부터 새 경로를 부른다"), `:53-64`(`DispatchController` 의 `@GetMapping("/capacity")` 삭제).
4. 어긋남: order `DispatchClient.checkCapacity`(food-delivery/order-service/src/main/java/com/fooddelivery/order/client/DispatchClient.java:32-50)는 `.uri("/api/deliveries/capacity")` 를 부른다. 새 dispatch 에서 이 경로는 `GET /api/deliveries/{id}`(food-delivery/dispatch-service/src/main/java/com/fooddelivery/dispatch/controller/DispatchController.java:42-46, `@PathVariable Long id`)에 걸려 형 변환 실패 400, Spring `DefaultHandlerExceptionResolver` 가 WARN `Resolved [...MethodArgumentTypeMismatchException ... For input string: "capacity"]`.
5. 전파: `checkCapacity` 가 ERROR `Failed to check dispatch capacity: 400 ...` 를 남기고 `ServiceException(502)` → 재시도 3회(200ms 지수, application.yml:105-111) → dispatch 서킷(10건 창 50% 5초 열림, :78-86) 대체 응답 `ServiceException(503, "Dispatch service unavailable: ...")` → `OrderService.createOrder`(food-delivery/order-service/src/main/java/com/fooddelivery/order/service/OrderService.java:94-112)가 그대로 던져 503. 주문 행 저장(:128) 전이라 주문, 배차, 결제, outbox 쓰기가 없다.

로컬 실측(2026-10-12, mysql:8.0(0.5 CPU, 1GiB) + food init.sql, 매니페스트 restaurant jar, 매니페스트 order jar, 패치 dispatch jar, Kafka 없음):
- dispatch `GET /api/couriers/capacity` 200 `{"currentAssigned":0,"maxCapacity":2000,"available":2000}`, `GET /api/deliveries/capacity` 400 `{"status":400,"error":"Bad Request","path":"/api/deliveries/capacity"}`.
- `POST /api/orders` 13건 모두 503, 0.64~0.85초. order ERROR `Failed to check dispatch capacity: 400 : "{...\"status\":400,\"error\":\"Bad Request\",\"path\":\"/api/deliveries/capacity\"}"`(서킷이 닫히거나 반열림일 때만 호출해 13건에 9줄), dispatch WARN `Resolved [...MethodArgumentTypeMismatchException: Method parameter 'id': Failed to convert value of type 'java.lang.String' to required type 'java.lang.Long'; For input string: "capacity"]` 9줄. `Created order` 0줄, 새 주문 행 0, `order_outbox_events` 0, ASSIGNED 0. order `/actuator/health` 내내 UP.
- 109 의 오류 본문은 JSON 대신 Whitelabel HTML 일 수 있다(119 의 기존 용량 확인 실패 로그가 `500 : "<html>...Whitelabel Error Page...(type=Internal Server Error, status=500)"`). 어느 쪽이든 줄 머리 `Failed to check dispatch capacity: 400` 은 같다.

## 5. 원인 규정 (원칙 6)

- `root_cause.target_id`: `food-delivery-dispatch`(target_kind container). 결함을 가진 곳은 짝 없이 나가 옛 계약(공개된 `/api/deliveries/capacity`)을 끊은 dispatch 릴리스이고, 되돌리거나 짝 릴리스를 함께 내야 재발이 막힌다. order 의 옛 경로 호출은 공개 계약 안의 정당한 요청이다("A 가 정당하고 B 의 로직이 잘못" → B). 원본 사후 설명이 지목한 층위(배포가 어긋나 인터페이스 버전 불일치, 다시 배포해 맞춤)와 같다.
- `trigger_target_id`: 비움(계기인 롤아웃과 근본이 같은 서비스).
- `scoring.partial`: order(실패가 드러나고 로그가 남는 곳, "order 를 새 버전으로 맞춰야 한다" 는 답도 부분 점수).
- 원칙 5: 정답에 코드 설계 추론이 필요 없다. dispatch 롤아웃 시각, 바로 뒤 order 의 `Failed to check dispatch capacity: 400`, dispatch 의 `MethodArgumentTypeMismatchException ... "capacity"` 로 "dispatch 새 릴리스 뒤 order 가 부르는 용량 경로가 dispatch 에 없다" 에 이른다.

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 | 내용 |
|---|---|---|
| 감지 | lucida 이상 탐지 | order 서버 스팬 오류율(503) 급증, order ERROR, dispatch WARN 로그 급증, `Created order` 와 `Dispatched courier` 로그 소멸. 별도 업무 규칙 없이 겉 증상이 크다(주문 생성 전량 실패) |
| 피해 판정 | 러너 | 동반 부하 k6 의 주문 생성 5xx 비율 ≥ 0.5 와 2xx 비율 < 0.2(3틱 연속) |
| RCA | 녹화 데이터 | KCM dispatch 롤아웃과 Pulled 태그 1.10.0(order 롤아웃 없음), order `Failed to check dispatch capacity: 400`, dispatch `MethodArgumentTypeMismatchException ... "capacity"`, dispatch 서버 스팬 `GET /api/deliveries/{id}` 400, `GET /api/deliveries/capacity` 서버 스팬 소멸 |

## 7. 관측 근거 (119 실조회, 평시)

| 증거 | 119 표와 칸 | 조회 | 결과 |
|---|---|---|---|
| 계기: dispatch 롤아웃과 이미지 태그 | CH `lucida.kcm_events_local`(timestamp, reason, object_kind, object_name, body) | `object_name LIKE 'testbed-dispatch%'` 30일 reason, body 별 | `Pulled` `Container image "food-delivery-dispatch:latest" already present on machine` 47건(마지막 2026-10-11 06:52). 2026-10-11 06:46:15 `ScalingReplicaSet` 'Scaled up replica set testbed-dispatch-5cbb57dd5b to 1' → 06:46:16 Pulled → 06:46:57 'Scaled down replica set testbed-dispatch-6cbcbcbcf5 to 0 from 1' 까지 수집됨. 장애 때는 1.10.0 이 본문에 담긴다 |
| 근본: order 의 용량 확인 400 | CH `lucida.lucida_logs_local`(service_name, severity_text, body) | food 서비스 30일 `body LIKE 'Failed to check dispatch capacity: 4%' OR body LIKE '%status=400)%' OR body LIKE '%regions/%'` | 0건. 기존 용량 확인 실패는 7일 `500 : "<html>...Whitelabel..."` 268줄, `I/O error on GET request` 295줄(다른 시나리오 실행분)뿐이라 400 이 새 문장이다. order ERROR 가 전수 수집됨도 확인 |
| 근본: dispatch 의 경로 해석 실패 | 같은 표 | 30일 `body LIKE 'Resolved [%'` 전 서비스 | 0건(그런 요청이 없었다). food-delivery-dispatch 의 WARN(`Failed to publish outbox event ...` 453줄 등)이 수집되므로 같은 로거 수준의 `DefaultHandlerExceptionResolver` WARN 도 남는다. 문장은 로컬 실측(§4) |
| 전파: dispatch 서버 스팬 상태 | CH `lucida.otel_traces_local`(service_name, span_name, span_kind, span_attributes['http.response.status_code']) | food dispatch, order 1일 `span_name LIKE '%capacity%' OR span_name LIKE 'GET /api/deliveries%'` | `GET /api/deliveries/capacity` SERVER 200 5,879건, `GET /api/deliveries/{id}` SERVER 404 1,608건, `GET /api/deliveries` 200 1,539건. 30일 `GET /api/deliveries/{id}` 상태는 404 만(5,267건). 장애 때 `{id}` 에 400 이 생기고 capacity 스팬이 사라진다(표본 10%, 보조 증거) |
| 피해: 주문 생성량 | `lucida.lucida_logs_local` | `service_name='food-delivery-order' AND body LIKE 'Created order%'` 시간별 24시간 | 2026-10-11 15시 ~ 10-12 15시 시간당 585 ~ 4,124건(초당 약 0.16 ~ 1.15). 장애 때 0 이 된다 |

트레이스(10% 표본)와 APM 지표는 보조 증거로만 쓴다. 핵심 증거(롤아웃 이벤트, order ERROR, dispatch WARN, `Created order` 소멸)는 전수 수집 표에 있다.

## 8. 감별

- must_support: 정답지 4개(dispatch 롤아웃과 1.10.0 Pulled, order 400 ERROR 와 dispatch WARN, `{id}` 400 과 capacity 스팬 소멸, `Created order` 0 과 두 서비스 Ready).
- must_rule_out:
  - F49-H, F58-R(order 자신의 릴리스): testbed-order 롤아웃이 없고, 실패가 응답 해석 실패나 `Required key` 가 아니라 dispatch 가 돌려준 400.
  - F32-R(배달원 용량 한도 설정 축소): dispatch 가 200 과 available 0 이 아니라 400, order 로그에 `courier pool exhausted` 없음, ASSIGNED 가 늘지 않음.
  - F55-R(dispatch 릴리스의 요청 한도): 429 가 아니라 400, 다른 dispatch 경로는 정상.
  - F63-P, F70-P, F43-P, F33-H, F33-R(dispatch 가 느려지거나 죽음): 시간 초과, 연결 거부, 재시작, OOM, 풀 고갈, MySQL 포화 없음.
  - F44-P(방화벽): 연결 시간 초과 I/O 오류가 아니라 HTTP 400, 롤아웃 있음.
- contrast_with: F49-H, F32-R, F55-R, F63-P, F44-P(정답지에 각 차이).

## 9. 러너 판정 조건과 강도, 부하 계산

- 주입: `k8s.image` release 모드, 레벨 하나 `approved-fixed-f75-r`(min_hold 15m, settle 30s, timeout 20m, max_injection_duration 25m). 강도 사다리 없음(경로가 없으면 모든 용량 확인이 실패하는 이진 결함).
- 동반 부하: `load.north_south` order-surge.js, target_rps 2, entry 30181, ramp_up 2m, hold 21m(F49-H, F58-R 과 같은 값, 주문 생성 초당 약 1.4건). 상주 기준선은 하루 주기로 주문 생성 초당 0.16 ~ 1.15건이라 한가한 시간에도 판정 표본이 서게 한다.
- 부하 상한: 2rps 는 상한 180 의 1.1%. 주문이 1초 안에 끝나고 dispatch 의 400 은 형 변환에서 바로 나 order 스레드, 풀, dispatch, MySQL 에 부담이 없다.
- 서킷브레이커: order 의 dispatch 서킷이 열리면 호출 없이 대체 응답 503 이 나가고, 반열림 때 다시 400 을 받아 열림을 되풀이한다. 어느 상태든 주문은 503 이라 피해는 서킷이나 재시도로 줄지 않는다. 용량 확인과 배차 호출이 같은 서킷이지만 배차 호출까지 가는 주문이 없다.
- 배차 용량: 실패한 주문은 배차를 남기지 않아 ASSIGNED 가 늘지 않는다(payment 쪽 고장처럼 배차 용량이 새지 않음).
- success: `order_create_5xx_rate ≥ 0.5` 와 `order_create_2xx_rate < 0.2`, 3틱. must_rule_out: `achieved_rps < 0.5`, food MySQL 파드 NotReady, order 파드 NotReady. abort: `entry_status == 0`(order 는 Ready 라 503 이 남는다). recovery: target_health 200, MySQL Ready, 기준선 2xx ≥ 0.7.
- cleanup: dispatch 를 매니페스트 이미지로 되돌리고 Deployment available, 1.10.0 을 쓰는 파드가 없어지면 tb-w3 containerd 에서 이름과 ID 참조 삭제. 장애 동안 쓴 데이터가 없다.
- 결함 이미지 규칙: 앱 소스와 매니페스트, 기본 이미지 그대로. 패치는 `food-delivery/dispatch-service/src/` 안만(테스트가 확인, origin/main 사본에 `git apply --check` 통과). 태그 1.10.0 은 저장소 어디에도 없다(dispatch 의 다른 릴리스는 F33-H 1.6.0, F43-P 1.7.0, F55-R 1.8.0, F63-P 1.9.0). 추가 문자열에 시나리오 id, fault, bug, chaos, scenario 없음(테스트가 확인). 빌드는 `bash scripts/scenarios/fault-images/build.sh f75-r`(배포 스크립트가 녹화 구간 확인 뒤 실행).

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Stitch 2023-03-13, 공식 상태 페이지 사고와 사후 설명, 링크)와 요소별 대응표가 있고 기전이 같다(§2)
- [x] 분류 장부를 갱신했고 묶음 L+J(L 9/111, 8.1%), 정답 위치 배달 서비스(8/111, 7.2%), 결제 경로 13/111(11.7%), 서비스 food 37(가장 적었음)과 고른 이유를 적었다. 어느 축도 20% 에 닿지 않는다(§3)
- [x] 근본 원인 위치가 패치 줄과 order, dispatch 소스 줄로 확인됐다(§4)
- [x] 근본 원인 흔적이 119 에서 조회됐다: kcm_events_local 의 dispatch 롤아웃과 Pulled 태그, lucida_logs_local 의 order ERROR 와 dispatch WARN 수집, otel_traces_local 의 dispatch 경로별 상태(§7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: KCM 이벤트와 전수 로그(§7)
- [x] 계기의 흔적이 조회됐다: 롤아웃 이벤트와 이미지 태그. 인공 지연 없음
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다(dispatch 롤아웃 + 400 로그)
- [x] 정답지 세 칸이 원칙 6대로 채워졌다(§5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다(§6)
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: 이미지 태그, 패치 문자열, 실행기 argv 에 없음(테스트)
- [x] 부하 상한과 서킷브레이커를 고려해 피해가 실제로 날 계산이 있다(§9, 로컬 실측 13/13 503)

### 후보 목록 (이번 반복, "실제 기전 × 부품", 숫자 순)

숫자: food 36(가장 적음) → commerce 37, banking 37. 묶음 D, J 각 17(15%)이 가장 많고 금지 없음. 정답 위치 상한 근처 없음(최다 은행 이체 서비스 10, 9%), 결제 경로 11.8%. food 부품 지도: kafka 0, notify 1, external-pg-mock 3, order 4, restaurant 4, payment 5, dispatch 13, mysql 15.

| 순위 | 후보(원본 / 부품 / 기전) | 결과 |
|---|---|---|
| 1 | Stitch 2023-03-13 / food dispatch(+order) / 짝 릴리스 중 dispatch 만 배포되어 용량 API 경로가 바뀌고 옛 order 가 400 | **채택** |
| 2 | Stitch 2023-03-13 / food order / order 만 배포되어 새 dispatch API 를 부르다 404 | 버림: F49-H 와 롤아웃된 쪽, 서비스 쌍, 증상, 정답(order)이 같고 실패 문장만 달라 카탈로그 §1 의 부분 복제. dispatch 판이 F49-H 와 정답이 갈리는 H 짝이 되어 더 가치가 크다. restaurant, payment 판은 order 가 하류 4xx 를 그대로 넘겨 4xx 단일 신호(F34-R 교훈) |
| 3 | Cloudflare 2025-06-12(M14) / food external-pg-mock / 외부 의존의 하위 저장소 장애로 /pay 5xx | 버림: 같은 대상, 같은 수단(mock.expectation)의 F06-P(429)와 F19-S(시간 초과 502 연쇄)와 실패 지점, 정답 위치가 같아 상태 코드만 바꾼 복제(카탈로그 §1), 결제 실패마다 배차가 남아 용량이 샘(rejected 293행 벽) |
| 4 | CircleCI 2022-01-03 공지(+ Sardine 2026-01, 원문 미검증) / food external-pg-mock / 제공자의 예고 없는 응답 형식 변경으로 payment 해석 실패 | 버림: 정답 위치와 대상이 F06-P 와 같아 "결제 실패면 PG" 지름길을 강화하고, 공급자 쪽 변경은 mock 에 흔적이 없다는 앞선 반려(rejected 264행)와 같은 벽, 원본 원문을 확인하지 못함(원칙 1) |
| 5 | Sendcloud 2025-09-10 / food restaurants, menus 부모 행 / 일괄 정정 FOR UPDATE 번짐이 주문 INSERT 의 외래 키 확인을 막음 | 버림: 같은 원본, 같은 도메인이 rejected 334행에 있음(order 풀과 입구) |
| 6 | GitHub 2025-03 월간 보고 / food MySQL 앱 계정 fooddelivery / 비밀번호 만료 | 버림: 네 서비스가 한 계정이라 풀 수명(600초) 뒤 order 풀이 비면 입구 0(컨트롤러 필수 중단), 굶는 순서가 무작위(rejected 12행 벽) |
| 7 | Harness 2025-10-28 / food restaurant / 노드에 없는 이미지로 롤아웃 | 버림: 기본 롤링(maxSurge 25%)이 옛 파드를 남겨 피해 없음(원칙 9) |
| 8 | Slack 2025-11-01 / food MySQL / 보존 정리 설정을 공격적으로 바꿔 DB CPU 과다 | 버림: 원본이 작업 종류와 설정 변경을 밝히지 않음(원칙 1, 웹 확인), 바깥 세션 CPU 는 ms OLTP 를 굶기지 않음(rejected 207행 벽) |
| 9 | GitHub 2026-09-13 / food MySQL / 정리 Job 쓰기가 공유 DB 연결 한도에 닿음 | 버림: 원본이 Job + 재시도 루프 + 시간 제한 부재의 복합(원칙 1, 웹 확인), 연결 한도 고갈은 굶는 풀이 무작위 |
| 10 | Inngest 2025-10-16 / food Kafka / 브로커 디스크 가득 | 버림: outbox 비동기라 사용자 증상 없음(원칙 7), Kafka PVC 가 노드 루트 디스크라 F10-R 과 같은 구조적 불가 |
| 11 | PostHog 2026-07-23 / food notify / 독 메시지로 파티션 정지 | 버림: 2차 출처(원칙 1), notify 는 사용자 경로 밖(원칙 7) |
| 12 | AWS S3 2017-02-28 / tb-w3 노드 / 잘못된 노드 drain | 버림: food 전 파드가 tb-w3 고정이라 order 가 빠져 입구 0(컨트롤러 필수 중단) |
