---
title: F32-H 설계 시트 (food dispatch 가 JSON 숫자를 문자열로 쓰게 하는 설정 배포로 order 가 용량 응답을 읽지 못함)
status: Draft
owner: project
last_reviewed: 2026-10-09
tags:
  - scenario
  - design
  - config-deploy
  - data-format
summary: 음식배달 dispatch-service 에 Jackson 의 '숫자를 문자열로 쓰기' 설정(SPRING_JACKSON_GENERATOR_WRITE_NUMBERS_AS_STRINGS=true)이 배포되어 용량 응답의 정수가 "500" 처럼 문자열로 나가고, order 가 그 값을 Integer 로 꺼내다 ClassCastException 으로 모든 새 주문을 503 으로 거절하는 시나리오. 원본은 Flagsmith 2026-03-03 Edge API 릴리스가 정수형 값의 JSON 형식을 문자열과 숫자 사이에서 바꿔 정적 타입 SDK 가 깨진 장애.
---

# F32-H 설계 시트

## 1. 요약

음식배달 배차 서비스(dispatch)에 "JSON 의 숫자를 문자열로 내보낸다"는 직렬화 설정이 배포된다(큰 정수를 다루는
자바스크립트 클라이언트의 정밀도 손실을 막으려고 흔히 켜는 설정이다). 새 파드부터 dispatch 가 쓰는 모든 숫자가
따옴표로 감싸진다. 주문 서비스(order)는 주문을 만들기 전에 dispatch 에 남은 배차 자리를 묻는데, 답을 타입 없는
Map 으로 읽어 정수로 꺼내다 "String 을 Integer 로 바꿀 수 없다"는 오류로 실패하고, 그 주문을 503 으로 거절한다.
dispatch 는 멀쩡하고 자리도 남아 있다. 바뀐 것은 답의 "모양" 하나다.

비유: 창고가 재고 수량을 숫자 칸 대신 글자 칸에 적어 보내기 시작했다. 수량은 맞는데, 숫자 칸만 읽는 주문 창구의
기계가 그 전표를 읽지 못해 주문을 모두 돌려보낸다.

F32-R(같은 dispatch 의 한도 값 오배포)과 겉 증상(dispatch 롤아웃 직후 order 가 주문 저장 전에 503)이 같고 원인이
다른 짝이라 F32 의 H 다.

## 2. 원본 사례

| 항목 | 내용 |
|---|---|
| 기업, 날짜 | Flagsmith, 2026-03-03 |
| 출처 | [공식 상태 페이지 사고 보고](https://status.flagsmith.com/incidents/q3h5kr2z2sgb) (`ref-real-world-incidents.md` M8 에 이미 있음, 2026-10-09 WebFetch 로 원문 다시 확인) |
| 요약 | "On Monday 3rd March at around 17:00 UTC, we deployed a release to the Edge API" 가 "inadvertently changed the JSON types returned for integer-typed feature state values". 문자열로 내던 값("300")이 숫자(300)로 나갔다. 대상은 로컬 평가 모드 SDK 가 쓰는 `/api/v1/environment-document/` 이고 "This caused issues for statically typed languages in particular". 18:12 보고, 18:27 P0, 18:36 이전 안정 릴리스로 롤백해 복구. 재발 방지는 직렬화 문제를 근원에서 고치고 다음 릴리스가 문자열 형식을 지키게 하는 것 |

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | Edge API 릴리스 배포 | food dispatch-service 설정 롤아웃(`SPRING_JACKSON_GENERATOR_WRITE_NUMBERS_AS_STRINGS=true` 한 줄) |
| 원인이 된 결함 | 배포가 정수형 값의 JSON 타입을 바꿈(문자열 → 숫자) | 배포가 dispatch 응답의 정수 JSON 타입을 바꿈(숫자 → 문자열). 키 이름과 값 자체는 그대로 |
| 전파 경로 | 그 문서를 읽는 SDK(정적 타입 언어)가 새 타입을 처리하지 못함 | 용량 응답을 읽는 order(자바, 정수 캐스트)가 ClassCastException → 주문 거절 |
| 사용자 증상 | SDK 기능 평가 실패 | 주문 생성 503. 식당 조회, 메뉴, 배달 추적은 정상 |
| 원본의 탐지 경로 | 사용자 보고(18:12) | 이 테스트베드에서는 order 5xx, order WARN 'Dispatch capacity check failed ... cannot be cast', notify 의 dispatch 이벤트 숫자에 붙은 따옴표, 직전 dispatch 롤아웃 이벤트 |
| 완화와 복구 | 이전 릴리스로 롤백 | env 를 baseline 으로 되돌리는 롤아웃(cleanup) |

**기전 동일성**: 원본과 재구성 모두 "보내는 쪽 배포가 같은 키의 값 타입을 문자열과 숫자 사이에서 바꿈 → 옛 타입을
기대하는 정적 타입 소비자가 값을 읽지 못함 → 그 소비자에 기대는 기능 실패 → 보내는 쪽을 되돌려 복구"다.
다른 점은 둘이다. (1) 방향이 반대다(원본은 문자열 → 숫자, 재구성은 숫자 → 문자열). 실패 고리는 같다: 받는 쪽이
고정 타입으로 꺼낸다. (2) 원본은 코드 릴리스, 재구성은 설정 배포다. 앱 코드를 고치지 않는다는 하네스 규칙 때문이고,
Spring Boot 는 직렬화 형식을 설정 한 줄로 바꿀 수 있어 "릴리스가 직렬화 형식을 바꿨다"는 결과는 같다.
같은 원본을 쓰는 F30-R 은 키 이름 규칙(camelCase → snake_case)이 바뀌어 필드가 사라지는 꼴이라, 값 타입이 바뀐 원본에는
F32-H 가 더 가깝다.

## 3. 숫자 근거 (2026-10-09, `scenario-stats.py`, 정식 + 후보 합계 45)

- 장부 묶음별: A 7(15%), B 7(15%), C 6, D 7(15%), E 2, F 2, G 6, H 2, I 1, J 1, **L 2**, M 1, O 1, K N 0
- 정답 위치별: 외부 결제 의존 6, 주문 서비스 6, 은행 이체 서비스 4, ..., **배달 서비스 1**, 결제 경로 합계 10(22%, 결제 쪽 금지)
- 서비스별: 쇼핑몰 24, 은행 12, **음식배달 9**(가장 적음)
- 이 후보: 묶음 **L 데이터 형식 불일치**(현실 근거 Liu 2019 버그 장애의 21%인데 우리는 2), 계기가 설정 배포라 L+G. 정답 위치 **배달 서비스**, 서비스 **음식배달**.
  추가 뒤 L 3/46(6.5%), 배달 서비스 2/46(4%), 음식배달 10. 어느 축도 20%에 닿지 않는다
- 0인 묶음 K, N 을 고르지 않은 이유: K 는 tb-w3 flannel 경로 삭제(Datadog 2023-03-08)를 검토했다. 원본 계기(systemd-networkd 재시작)는
  networkd 가 관리하지 않는 flannel.1 의 경로를 지우지 않아(tb-w3 `networkctl list` 에서 flannel.1 unmanaged, systemd 255) 재현이 수동 경로 삭제가 되고,
  food 파드가 다른 노드로 가는 것은 tb-cp 의 CoreDNS 뿐이라 증상이 F37-R(이름 해석 실패)과 같으며, Hikari 연결이 교체되며 food 의 DB 헬스가 떨어져
  입구 재시작(entry_status 0) 위험이 있다. N 은 모든 동기 호출의 서킷브레이커 때문에 F14-R 막힘 그대로다
- 함께 검토하고 버린 후보는 최종 보고와 로컬 기록(`~/dev/testbed-ops/rejected-candidates.md`)에 있다
- 기존 시나리오와의 거리: F32-R(같은 대상 Deployment, 같은 주입 수단 k8s.env, 같은 503)과는 원인(한도 값 대 출력 형식)과 order 로그, dispatch 출력이 다르다.
  F30-R(같은 원본, 같은 L)과는 바뀐 쪽(payment 대 dispatch), 깨지는 꼴(키 이름 → 필드 유실 대 값 타입 → 캐스트 실패), 실패 단계(결제 대 주문 저장 전)가 다르다.
  주입 지문은 env 값이 달라 겹치지 않는다(`test_no_two_scenarios_claim_the_same_injection` 통과)

## 4. 인과 사슬 (코드와 인프라 위치)

1. 계기: k8s.env 가 `rca-testbed-food/testbed-dispatch` 컨테이너 env 에 `SPRING_JACKSON_GENERATOR_WRITE_NUMBERS_AS_STRINGS=true` 한 줄을 더해 롤아웃
   (인프라 지점: `food-delivery/k8s/22-dispatch-deploy.yaml:38-86` 의 env. 실배치 값은 109 `kubectl -n rca-testbed-food get deploy testbed-dispatch -o json` 으로 본 11개 항목이며
   2026-10-09 F32-R baseline 과 바이트 단위로 같다. Jackson 관련 env 없음. replicas 1, maxSurge 25%, maxUnavailable 25%)
2. 설정 적용: Spring Boot `JacksonProperties.generator`(Map<JsonGenerator.Feature, Boolean>)가 env 를 `spring.jackson.generator.write-numbers-as-strings` 로 묶어
   자동 구성 ObjectMapper 에 `WRITE_NUMBERS_AS_STRINGS` 를 켠다. **로컬 실측**(dispatch-service-1.0.0.jar 의 Boot 3.4.5, Jackson 2.18.3 라이브러리, 2026-10-09):
   env 없음 → `{"currentAssigned":1500,"maxCapacity":2000,"available":500}`, env 있음 → `{"currentAssigned":"1500","maxCapacity":"2000","available":"500"}`
3. 출력: `food-delivery/dispatch-service/src/main/java/com/fooddelivery/dispatch/controller/DispatchController.java:63-66 (getCapacity)` 가
   `DispatchService.java:106-114 (getCapacity)` 의 Map<String, Integer> 를 그대로 직렬화해 200 으로 응답. dispatch 는 아무 오류도 남기지 않는다
4. 읽기: `food-delivery/order-service/src/main/java/com/fooddelivery/order/client/DispatchClient.java:32-39 (checkCapacity)` 가 `body(Map.class)` 로 읽는다.
   타입 정보 없는 Map 이라 값은 String 으로 담긴다(로컬 실측 위와 같음). 호출은 성공(200)이라 retry, CB 가 끼지 않는다
5. 실패: `food-delivery/order-service/src/main/java/com/fooddelivery/order/service/OrderService.java:94-112 (도메인 검증 3)` 의 `int available = cap.get("available")`
   언박싱이 `ClassCastException: class java.lang.String cannot be cast to class java.lang.Integer` 를 던지고(로컬 실측), `catch (Exception ex)` 가
   `log.warn("Dispatch capacity check failed, rejecting order: {}", ex.getMessage())` 후 `ServiceException(503, "Dispatch service unreachable")`. 주문 저장보다 앞이다
6. 응답: `order-service/.../config/GlobalExceptionHandler.java:12-20 (handleServiceException)` 가 503 응답
7. 부수 흔적: 같은 ObjectMapper 가 `shop-common/.../outbox/OutboxPublisher.java:25-32 (publish)` 에서 dispatch 이벤트 payload 를 쓴다. 장애 중에도
   `DispatchService.java:119-129 (deliverExpiredDispatches)` 가 30초마다 DELIVERED 이벤트를 내므로, `notify-service/.../DispatchEventConsumer.java:20-23 (onDispatchEvent)` 의
   INFO 'Received dispatch event: {...}' 에 `"dispatchId":"5045059"` 처럼 따옴표 친 숫자가 찍힌다. notify 는 원문을 로그로 남기기만 해서 실패하지 않는다

## 5. 원인 규정 (원칙 5, 6)

| 칸 | 값 | 근거 |
|---|---|---|
| `root_cause.target_kind` | `config` | 결함은 dispatch 의 배포된 설정값이다(F30-R, F32-R 과 같은 표기) |
| `root_cause.target_id` | `food-delivery-dispatch` | 출력 형식을 바꾼 곳, 되돌려야 고쳐지는 곳. lucida-next 가 이 앱을 부르는 APM 서비스 이름이고 같은 이름의 다른 개체가 없다 |
| `root_cause.trigger_target_id` | 비움(null) | 계기(dispatch 설정 배포)와 근본이 같은 곳 |
| `scoring.granularity` / `accept` | `service` / `food-delivery-dispatch`, `food-dispatch` | F32-R 과 같은 표기 |
| `scoring.partial` | `food-delivery-order`, `food-order` | 예외와 503 이 나오는 증상 위치 |

- 원칙 6: 원본처럼 받는 쪽(order)은 늘 받던 형식을 기대했을 뿐이고, 형식을 바꾼 것은 보내는 쪽 배포다. Flagsmith 도 롤백한 것은 보내는 쪽 릴리스이고 SDK 가 아니다
- 원칙 5: order 가 Map 으로 읽고 캐스트하는 취약함(타입 있는 레코드였다면 Jackson 이 "500" 을 500 으로 읽는다)은 피해를 키운 코드 결함이라 정답이 아니라 user_impact 에 적는다.
  정답 "dispatch 배포가 응답의 숫자 형식을 바꿔 order 가 읽지 못했다"는 관제 데이터(롤아웃 이벤트와 새 ReplicaSet env, order 의 cast 오류 로그, notify 의 따옴표 친 숫자)로 낼 수 있다
- "order 가 문제"라고 답하면 증상 위치라 부분 점수다. "dispatch 가 죽었다/느리다"라고 답하면 위치는 맞고 기전은 틀리다(서비스 입도라 accept, 기전 감별은 must_rule_out)

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 | 내용 |
|---|---|---|
| 감지(lucida-next) | 겉 증상 | order APM error_rate 상승(POST /api/orders 503), 새 WARN 템플릿 'Dispatch capacity check failed, rejecting order', 'Created order' 와 'Dispatched courier' 로그 정지. 같은 order 503 을 내는 F32-R 녹화 실행(F32-R-run-63b10537)에서 인시던트 ed90432c(critical, order 오류율 근거)가 실제로 생겼다(`~/dev/testbed-ops/verifications/2026-10-09-0111-F32-R-run-63b10537.md`) |
| 피해 판정(러너) | k6 live 문서 | F32-R 과 같은 관측: `loadgen.food_create_status_rate`(business_5xx_rate) ≥ 0.5, `loadgen.transfer_2xx_rate`(business_2xx_rate) < 0.2. 평시 business_5xx_rate 0 |
| RCA | 녹화 데이터 | §7 표 |

## 7. 관측 근거 (119 실조회, 2026-10-09 06:00~06:40 UTC)

| 증거 | 표, 칸 | 조회와 결과(평시) | 표본 여부 |
|---|---|---|---|
| 계기: dispatch 롤아웃 | CH `lucida.kcm_events_local` | `SELECT timestamp, object_kind, object_name, reason, body FROM lucida.kcm_events_local WHERE timestamp > now() - INTERVAL 2 DAY AND namespace='rca-testbed-food' AND object_name LIKE 'testbed-dispatch%' AND reason IN ('ScalingReplicaSet','SuccessfulCreate','Created','Killing')` → 2026-10-09 00:26:52 `Deployment testbed-dispatch ScalingReplicaSet "Scaled up replica set testbed-dispatch-6cbcbcbcf5 to 1 from 0"`, `ReplicaSet SuccessfulCreate`, `Pod Created`, 00:27:33 `Scaled down ... 54c6f458b8 to 0 from 1`, `Killing` | 전수 |
| 계기와 근본: 바뀐 설정값 | PG `kcm_resources_history` (`kind`, `name`, `yaml`, `captured_at`) | 119 `pg-1`(DB lucida) 읽기 전용 트랜잭션: `SELECT kind, name, captured_at, position('DISPATCH_MAX_CAPACITY' in yaml)>0, position('OTEL_SERVICE_NAME' in yaml)>0 FROM kcm_resources_history WHERE namespace='rca-testbed-food' AND name LIKE 'testbed-dispatch%' ORDER BY captured_at DESC LIMIT 6` → `replicaset testbed-dispatch-54c6f458b8 2026-10-09 00:27:33 t t`(F32-R 실행의 장애 ReplicaSet, 더해진 env 가 yaml 에 있다), `replicaset testbed-dispatch-6cbcbcbcf5 ... f t`. ReplicaSet yaml 에 컨테이너 env 전체가 남으므로 장애 시 `SPRING_JACKSON_GENERATOR_WRITE_NUMBERS_AS_STRINGS` 가 새 ReplicaSet 에 있어야 한다 | 전수 |
| 전파: order 형식 오류 로그 | CH `lucida.lucida_logs_local` | `SELECT countIf(body LIKE 'Dispatch capacity check failed%'), countIf(body LIKE 'Order rejected: courier pool exhausted%'), countIf(body LIKE 'Failed to check dispatch capacity%'), countIf(body LIKE 'Created order id=%'), countIf(body LIKE '%cannot be cast%') FROM lucida.lucida_logs_local WHERE service_name='food-delivery-order' AND timestamp > now() - INTERVAL 10 DAY` → **0**, 407(F32-R 실행), 622, 414,404, **0** (보존 2026-10-02 04:00 ~ 10-09 06:14). 같은 order 의 WARN 이 수집되는 것은 'SQL Error: 0, SQLState: null' 55건, 'DataSource health check failed' 10건 등으로 확인(7일). food 네임스페이스 전체에서 'cannot be cast' 8일 0건 | 전수 |
| 근본의 직접 흔적: dispatch 출력 형식 | 위 표 `service_name='food-delivery-notify' AND body LIKE 'Received dispatch event%'` | 8일 828,531건, 그중 `"dispatchId":"` 꼴(따옴표 친 숫자) **0건**. 평시 예: `{"dispatchId":5045059,"orderId":5052524,"status":"ASSIGNED","eventType":"DISPATCH_ASSIGNED"}`. DELIVERED 이벤트는 최근 6시간 5분 창 중앙 232건(p10 118)이라 장애 중에도(만료 배치가 계속 돌아) 따옴표 친 숫자가 5분당 수백 건 찍힌다 | 전수 |
| 피해: 주문 생성 로그 | 위 표 `body LIKE 'Created order id=%'` 5분 창 | 최근 6시간 73창: 중앙 239, p10 165, 최대 375. 장애 시 0 근처 | 전수 |
| 감별: dispatch 가용성 장애의 꼴 | 위 표 order `body LIKE 'Failed to check dispatch capacity%'` ERROR | 7일 622건('500 : Whitelabel Error Page' 330, 'I/O error on GET' 292). dispatch 가 응답하지 못할 때의 모습이고 F32-H 에서는 없어야 한다 | 전수 |
| 감별: 한도 부족(F32-R)의 꼴 | 위 표 order `body LIKE 'Order rejected: courier pool exhausted%'` | 10일 407건, 모두 F32-R 실행 구간. F32-H 에서는 없어야 한다 | 전수 |
| 보조: 주문 503, 용량 조회 스팬 | CH `lucida.otel_traces_local` | F32-R 시트 §7 의 평시 값(order POST /api/orders 503 하루 3건, capacity 200). 장애 시 order 503, capacity 200 | **10% 표본, 보조로만** |

핵심 증거(롤아웃 이벤트와 새 ReplicaSet 스펙, order cast 오류 로그, notify 의 형식 바뀐 dispatch 이벤트, 주문 로그 정지)는 모두 전수 수집 데이터다.
트레이스와 APM 지표는 보조다. 장애 시 형태는 첫 실행에서 다시 확인한다.

## 8. 감별

- must_support: dispatch 롤아웃 이벤트와 새 ReplicaSet env, 그 직후 order WARN 'Dispatch capacity check failed, rejecting order: class java.lang.String cannot be cast to class java.lang.Integer'(평시 0),
  notify 'Received dispatch event' 의 따옴표 친 숫자(평시 0), 'Created order', 'Dispatched courier' 정지, 주문 5xx 0 → 0.5 이상, dispatch Ready, dispatch 5xx 없음, 만료 배치 계속, 조회 정상
- must_rule_out:
  - 배차 한도 부족(F32-R): 'Order rejected: courier pool exhausted' 가 없고 실패 사유는 형식 불일치, 롤아웃 스펙에 DISPATCH_MAX_CAPACITY 변화 없음
  - dispatch 가용성 장애: 'Failed to check dispatch capacity', 'Dispatch service unavailable' 없음, 파드 Ready
  - order 쪽 변경: order 롤아웃 없음. 형식 변화는 order 와 무관한 notify 로그에도 똑같이 보인다
  - 결제 단계 실패(F30-R, F06-P): 주문 저장 전에 503 으로 끝나 payment 를 부르지 않는다
- contrast_with: F32-R(같은 503, 원인은 한도 값), F30-R(같은 원본 계열, 키 이름 변경으로 결제 요청 필드 유실), F36-R(같은 L, DB 스키마와 ORM 열 이름)

## 9. 러너 판정과 강도, 부하 계산

- 진행 대본: F32-R 과 같은 틀의 설계 강도 하나로 고정한 evaluation(`approved-fixed-f32-h`, `profile.kind: fixed`, escalate 없음). 첫 실행이 곧 녹화 실행
- 강도 근거: 설정은 켜짐/꺼짐 하나라 강도 사다리가 없다. 새 파드가 Ready 되어 옛 파드가 내려간 순간부터(평시 롤아웃 약 41~51초, §7) 용량 확인을 거치는 주문의 100% 가
  503 이다. F32-R 과 달리 배정 수가 내려갈 때까지 기다리는 시간이 없어 피해는 강도와 무관하게 결정적이다
- 서킷브레이커: 용량 조회는 200 응답이라 order 의 dispatch retry 와 CB 가 관여하지 않는다(예외는 checkCapacity 밖 createOrder 안에서 난다). 배차 요청과 결제는 호출되지 않는다
- 자연 포화가 끼어들지 않는가: F32-R §9 와 같은 계산(동반 3rps 의 주문 여정 10%, ASSIGNED 가 기본 한도 2000 까지 차려면 약 15분 초과 유입 필요). 장애 중에는 새 배정이 없다.
  F32-R 처럼 동반 부하를 3rps 로 둔다(seed 3233)
- success(all, 3틱): `order_create_5xx_rate` ≥ 0.5, `order_create_2xx_rate` < 0.2. 평시 business_5xx_rate 0.0, business_2xx_rate 0.912~1.0(F32-R 시트 §9 의 상주 기준선 12회 실측)
- must_rule_out(any, 2틱): `achieved_rps < 1`, `read_step_rate ≥ 0.1`, `pod_ready(testbed-mysql) == false`(F32-R 과 같다)
- **운영 메모(녹화 검증 때 필수 확인, 정답지 mechanism, 실행 매트릭스, 큐 note 에도 같은 기준을 적었다)**: 러너 success 는 F32-R, dispatch 불능과 이 시나리오를 구별하지 못한다(셋 다 order 503). 검증 하네스는 녹화본에서
  order WARN 'Dispatch capacity check failed ... cannot be cast ...' 와 notify 의 따옴표 친 dispatch 이벤트가 있고, 'Order rejected: courier pool exhausted',
  'Failed to check dispatch capacity', 'Dispatch service unavailable' 이 없는지 반드시 확인한다
- recovery: F32-R 과 같다(target 200, MySQL Ready, food 기준선 2xx ≥ 0.7, 실행기 cleanup 의 `healthy 180s`)
- 롤아웃: replicas 1, maxSurge 25%(올림 1), maxUnavailable 25%(내림 0). 새 파드는 Jackson 설정만 바뀌어 기동, 헬스(/actuator/health 는 상태 코드만 본다)가 평시와 같다.
  dispatch 가용성 공백이 없다. level `settle` 60초
- 부하 상한: target_rps 3 은 상한 180 의 2%

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례와 대응표, 기전 동일: Flagsmith 2026-03-03 공식 보고를 WebFetch 로 다시 열어 "inadvertently changed the JSON types returned for integer-typed feature state values", "statically typed languages" 를 확인. 대응표와 차이 둘은 §2
- [x] 분류 장부 갱신과 숫자: 장부 §4-1 에 F32-H(L+G, 배달 서비스), §6 기록 한 줄. 숫자는 §3(L 3/46 6.5%, 배달 서비스 2/46 4%, 결제 경로 아님, 음식배달 9→10). 어느 축도 20% 미만
- [x] 근본 원인 위치: §4 의 `file:line (심볼)` 과 로컬 실측(설정 적용, 직렬화 결과, ClassCastException). 인프라 지점은 109 kubectl 로 본 실배치 env 11개
- [x] 근본 원인 흔적 119 조회: PG `kcm_resources_history` 의 dispatch ReplicaSet yaml 에 env 가 남음(§7 두 번째 행), CH 로그에서 order cast 오류와 notify 따옴표 숫자 평시 0(§7 세, 네 번째 행)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: KCM 이벤트, PG 리소스 이력, 로그(모두 전수). 트레이스, APM 은 보조
- [x] 계기의 흔적: `kcm_events_local` 의 testbed-dispatch 롤아웃 이벤트가 평시에 실제로 남았다(2026-10-09 00:26:52). 인공 지연 없음, 주입은 설정값 하나
- [x] 정답이 관제 데이터로 낼 수 있는 결론: 형식 변화가 notify 로그에 직접 보이고 order 오류 메시지가 타입 불일치를 말한다. order 의 Map 캐스트 취약함은 정답이 아니라 user_impact(§5)
- [x] 정답지 세 칸: 근본 `food-delivery-dispatch`(config), 계기 null, 부분 점수 `food-delivery-order`, `food-order`
- [x] 감지, 피해 판정, RCA 증거 구분: §6 표
- [x] 주입이 시나리오 id 를 남기지 않음: env 값은 `SPRING_JACKSON_GENERATOR_WRITE_NUMBERS_AS_STRINGS=true` 뿐, ReplicaSet 이름은 해시. k6 `--tag scenario_id=F32-H` 는 tb-runner 로컬 지표 태그로 요청에 실리지 않는다(F32-R 과 같은 경로)
- [x] 부하 상한과 서킷브레이커를 넣은 피해 계산: §9(용량 조회 200 이라 CB 무관, 설정이 켜진 순간부터 결정적, 동반 3rps)

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- 새 ReplicaSet 행의 yaml env 에 `{"name":"SPRING_JACKSON_GENERATOR_WRITE_NUMBERS_AS_STRINGS","value":"true"}` 가 있는가
- order WARN 'Dispatch capacity check failed, rejecting order: class java.lang.String cannot be cast ...' 가 롤아웃 직후 주문마다 나오는가(109 의 실제 이미지에서도 로컬 실측과 같은지)
- notify 'Received dispatch event' 의 dispatchId 가 따옴표로 감싸지는가
- 인시던트가 생기는가(F32-R 의 ed90432c 처럼 order 오류율 근거)
