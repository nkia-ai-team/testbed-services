---
title: F64-R 설계 시트 (commerce 게이트웨이 설정 배포가 order 라우트 서킷브레이커의 느린 호출 임계를 의도보다 낮게 넣어 멀쩡한 checkout 을 끊음)
status: Draft
owner: project
last_reviewed: 2026-10-11
tags:
  - scenario
  - design
  - config-deploy
  - circuit-breaker
summary: commerce api-gateway 설정 롤아웃이 order 라우트 서킷브레이커에 느린 호출 차단(slow-call-duration-threshold 30ms, slow-call-rate-threshold 50%)을 켜는데 임계가 checkout 의 정상 처리 시간(게이트웨이에서 본 order 호출 최소 약 37ms)보다 낮아, order 가 멀쩡히 200 으로 답하는데도 서킷이 계속 열려 checkout 대부분이 게이트웨이에서 502 가 되는 시나리오. 원본은 Spreedly 2022-03-28 서킷브레이커의 잘못된 설정이 의도보다 낮은 임계에서 작동해 멀쩡한 Adyen 게이트웨이로 가는 요청을 503 으로 돌려보낸 장애.
---

# F64-R 설계 시트

## 1. 요약

commerce 의 모든 사용자 요청은 nginx → api-gateway(BFF) 를 지나 하류 서비스로 간다. 게이트웨이는 하류마다 따로 Resilience4j 서킷브레이커(`user`, `cart`, `pricing`, `shipping`, `order`, `product`, `payment`)를 붙여 한 하류의 장애가 다른 라우트로 번지지 않게 막는다(`ProxyService.java`). 평시 설정은 실패율만 본다: 최근 10건 중 최소 5건이 모였을 때 **예외로 끝난 호출**이 50% 이상이면 5초 열린다(`application.yml:49-99`). 느린 호출 기준은 Resilience4j 기본값(60초, 100%)이라 사실상 꺼져 있다.

설정 롤아웃 하나가 게이트웨이 컨테이너에 `SPRING_APPLICATION_JSON` 한 줄을 더해 order 라우트 서킷에 느린 호출 차단을 켠다: `slow-call-duration-threshold: 30ms`, `slow-call-rate-threshold: 50`. 의도는 checkout 이 비정상으로 늘어질 때 끊는 것이었지만 들어간 임계(30ms)가 checkout 의 정상 처리 시간보다 낮다. 게이트웨이가 order 를 부르는 시간은 평시 최소 약 37ms, 중앙 47ms 다(119 트레이스 24시간, §7). 그래서 새 게이트웨이 파드에서는 order 로 간 호출이 **전부 느린 호출**로 기록되고, 5건이 모이자마자 서킷이 열린다. 열린 동안 checkout 은 order 에 닿지도 못하고 게이트웨이 fallback 이 502 를 낸다. 5초 뒤 half-open 시험 호출 3건이 order 로 가서 200 으로 끝나지만 역시 30ms 를 넘으므로 서킷이 다시 열린다. 이 주기가 계속되어 checkout 의 약 85~90% 가 502 다. order, payment, 외부 결제, 은행 정산은 아무 잘못이 없고 상품 조회, 장바구니, 로그인 같은 다른 라우트도 정상이다.

비유: 백화점 안내 데스크가 각 매장으로 가는 손님 줄을 관리한다. 새 운영 지침이 "계산대 매장 안내가 0.03초를 넘기면 그 매장이 막힌 것으로 보고 5초 동안 손님을 돌려보내라"로 내려왔다. 계산대는 평소에도 한 손님에 0.04~0.05초가 걸리는 곳이라 안내 데스크는 멀쩡한 계산대를 계속 "막힘"으로 판정하고 손님 대부분을 입구에서 돌려보낸다. 계산대 직원들은 오는 손님을 평소처럼 받고 있다.

## 2. 원본 사례

- 기업: Spreedly(결제 오케스트레이션, Core Transactional API 를 통해 여러 결제대행사로 거래를 보냄)
- 날짜: 2022-03-28 18:45~19:22 UTC(상태 페이지 첫 공지 14:45~15:22 EDT, 약 37분). 사후 보고 게시 2022-04-08
- 링크: [공식 상태 페이지 사고와 사후 보고 "Degradation of service with Adyen gateway"](https://status.spreedly.com/incidents/yplj1mlfrvsl)
- 요약(출처가 말한 것만): 사후 보고 제목은 "Circuit Breaker Triggered at Lower than Intended Threshold for Adyen Gateway"(제목의 날짜는 May 28, 2022 로 적혔지만 본문과 상태 페이지 시간표는 March 28). 2022-03-28 18:45~19:22 UTC 에 Adyen 게이트웨이용 서킷브레이커가 "erroneously activated" 했고, 그 때문에 일부 고객이 요청에 HTTP 503 을 받았다. 서킷브레이커는 "to manage traffic flow when a downstream gateway is producing too many failures, or consuming too many resources" 하려고 둔 것이다. 조사에서 "an errant configuration in the Circuit Breaker" 를 찾아 설정을 고쳐 정상으로 되돌렸고, 재발 방지로 서킷브레이커를 어떻게 설정하고 쓰는지 다시 검토하는 작업을 시작했다. 어떤 설정값이 얼마였는지, 어떻게 탐지했는지는 보고에 없다.
- 현실 비중: 설정 배포는 Google 포스트모템 트리거의 31%(M1)다. 서킷브레이커 오설정(M23)은 자료 문서에 공식 사례가 없던 기전이라 이번에 이 사례를 M23 에 더했다(2026-10-11 웹 조사).

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 서킷브레이커의 잘못된 설정(errant configuration) | commerce api-gateway 설정 롤아웃: 컨테이너 env 에 `SPRING_APPLICATION_JSON` 으로 order 라우트 서킷의 느린 호출 임계 30ms, 느린 호출 비율 50% 를 더함 |
| 원인이 된 결함 | 서킷브레이커가 의도보다 낮은 임계에서 작동(Triggered at Lower than Intended Threshold) | 임계 30ms 가 checkout 의 정상 처리 시간(게이트웨이에서 본 order 호출 최소 약 37ms)보다 낮아, 멀쩡한 호출이 모두 느린 호출로 세어져 서킷이 열림 |
| 전파 경로 | 서킷이 하류 결제대행사(Adyen)로 가는 요청을 막음. 하류는 실패를 내지 않았는데 오작동("erroneously activated") | 서킷이 열린 동안 게이트웨이가 order 를 부르지 않고 fallback 이 502. half-open 시험 호출 3건은 order 에 닿아 200 이지만 다시 30ms 를 넘어 서킷이 또 열림. order, payment, 외부 결제, 은행 정산은 정상 |
| 사용자 증상 | 그 결제대행사로 가는 고객 요청 일부가 HTTP 503 | commerce checkout(`POST /api/orders/checkout`)의 약 85~90% 가 502 'Downstream service unavailable'. 상품 조회, 장바구니, 로그인은 정상(이 게이트웨이는 fallback 을 BAD_GATEWAY 로 만든다) |
| 탐지 경로 | 보고에 없음. 조사에서 서킷브레이커 설정 오류를 찾음 | 게이트웨이 5xx 와 WARN 로그 'CircuitBreaker 'order' is OPEN and does not permit further calls' 급증. 그 앞에 하류 실패 로그가 없고 order 는 200 으로 정상 처리. 롤아웃 직후 시작되고 새 ReplicaSet 스펙에 그 설정이 있음 |
| 완화와 복구 | 설정을 고쳐 정상 운영으로 되돌림 | cleanup 이 env 를 원래 배열로 되돌리고 게이트웨이 롤아웃(새 파드는 기본 설정, 서킷 상태도 새로 시작)이 끝나면 checkout 이 회복 |

기전 대조: 원본의 고리는 "서킷브레이커 설정 오류 → 의도보다 낮은 임계에서 작동 → 멀쩡한 하류로 가는 요청을 차단 → 그 경로 요청만 5xx → 설정을 고쳐 복구"다. 재구성도 같은 고리다. 대상이 외부 결제대행사에서 내부 order 서비스로, 503 이 502 로 바뀐 것은 스택에 맞춘 대상과 표면의 차이다(이 게이트웨이의 fallback 코드가 BAD_GATEWAY 를 던진다, `ProxyService.java:102-106`). 원본이 어떤 설정 항목(실패율, 느린 호출, 최소 호출 수)이었는지 밝히지 않아, 이 게이트웨이에서 평시에 실제로 작동할 수 있는 항목을 골랐다: 게이트웨이는 하류의 4xx, 5xx 응답을 예외로 바꾸지 않고 그대로 돌려주므로(`doForward` 의 `RestClientResponseException` 처리) 평시 실패율은 0 이고 실패율 임계를 낮춰도 서킷이 열리지 않는다. 멀쩡한 하류에서 서킷이 열리려면 느린 호출 기준이어야 한다.

## 3. 숫자 근거 (2026-10-11, `scenario-stats.py`, 정식 + 후보 합계 83, 이 후보 전)

- 묶음(정식 + 후보): J 14(16.9%), P 10(12%), D 8, G 8(9.6%), A 7, B 7, L 7, C 6, E 4, F 3, H 2, K 2, Q 2, I 1, M 1, O 1, N 0. 20%(83 기준 16.6) 에 닿은 묶음은 없다. 이 후보는 **G**(8 → 9, 84 중 10.7%). 현실에서 두 번째로 흔한 트리거(설정 배포 31%)인데 장부 비율은 10% 안팎이다.
- 정답 위치(정식 + 후보): 주문 서비스 8(9.6%)이 가장 많고 **게이트웨이 0**. 이 후보는 게이트웨이(0 → 1). 결제 경로 합계 12/83(14.5%) 은 이 후보와 무관하다(정답이 결제 쪽이 아니다).
- 서비스(정식 + 후보, 문제가 시작된 서비스): commerce 28, core-banking 28, food-delivery 27. commerce 는 최다 동률이지만 원칙 2 완화 규칙대로 **정답 위치가 0 인 부품(게이트웨이)** 을 정답으로 하는 후보라 낸다. 부품 지도에서도 commerce gateway 는 0 이다.
- 왜 이 후보인가: 게이트웨이는 commerce 사용자 요청이 모두 지나는 부품인데 정답으로 쓰인 적이 없다. 그동안 게이트웨이 후보(릴리스 CPU, 메모리, HTTP 클라이언트 교체, 재시도 정책, 하류 주소, 토큰 검증)는 원칙 1, 9, 카탈로그 §1 로 막혔다(rejected 20, 26, 30, 34, 35, 45, 67, 86, 108, 113, 120~122, 129, 181, 184, 263, 275). 서킷브레이커 오설정은 자료 문서에 공식 사례가 없어 food order 판(rejected 21)이 원칙 1 로 막혔는데, 이번에 공식 사후 보고(Spreedly 2022-03-28)를 찾았다. 그리고 겉 증상(checkout 502, 게이트웨이 로그의 order 서킷 열림)이 order 자체 장애(F03-H 스레드풀 고갈, 2026-10-06 order 재기동 구간)와 같아서, "증상 라우트의 하류가 범인"이라는 지름길로는 틀리고 하류의 건강과 게이트웨이 설정 변경을 함께 봐야 맞히는 H 성격의 데이터가 된다.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 계기: Deployment `testbed-gateway`(rca-testbed-commerce, replicas 1, RollingUpdate maxSurge 25%(올림 1) / maxUnavailable 25%(내림 0), `29-api-gateway.yaml`) 컨테이너 `api-gateway` 의 env 배열에 `SPRING_APPLICATION_JSON={"resilience4j":{"circuitbreaker":{"instances":{"order":{"slow-call-duration-threshold":"30ms","slow-call-rate-threshold":50}}}}}` 한 줄이 더해진 롤아웃. 평시 env 는 OTel 관련 9줄뿐이고 공용 ConfigMap `service-config` 에도 이 키가 없다(109 kubectl 2026-10-11).
2. 설정 적용: Spring Boot 3.4.5 는 `SPRING_APPLICATION_JSON` 을 `application.yml` 보다 앞선 속성 원천으로 읽어 `resilience4j.circuitbreaker.instances.order.slow-call-duration-threshold=30ms`, `...slow-call-rate-threshold=50` 을 더한다. 나머지 order 서킷 설정(창 10, 최소 5, 실패율 50, 열림 5s, half-open 3, 자동 전이)은 `commerce/api-gateway/src/main/resources/application.yml:79-85` 그대로다. 다른 6개 서킷은 바뀌지 않는다.
3. 기록: `ProxyService.forwardToOrder`(`ProxyService.java:81-85`, `@CircuitBreaker(name="order", fallbackMethod="unavailable")` + `@Retry(name="order")`)가 `doForward`(`ProxyService.java:108-128`)로 order 를 부른다. 호출은 200 으로 끝나도 30ms 를 넘기면 느린 호출로 기록된다. 게이트웨이에서 본 order 호출은 평시 최소 약 37ms(119 트레이스, §7)라 모두 느린 호출이다.
4. 열림: 최소 호출 5건이 모이면 느린 호출 비율 100% ≥ 50% 라 서킷이 OPEN. 열린 동안 호출은 `CallNotPermittedException` 으로 막히고 fallback `unavailable`(`ProxyService.java:102-106`)이 WARN 'Downstream unavailable for POST /api/orders/checkout: io.github.resilience4j.circuitbreaker.CallNotPermittedException: CircuitBreaker 'order' is OPEN and does not permit further calls' 를 남기고 `ServiceException(BAD_GATEWAY)` 를 던진다. `@Retry(order)`(최대 2회, 300ms, `application.yml:122-126`)가 한 번 더 시도하지만 역시 막혀 같은 WARN 이 한 번 더 남고 `GlobalExceptionHandler`(`GlobalExceptionHandler.java`)가 502 로 답한다.
5. 되풀이: 5초 뒤 자동으로 HALF_OPEN, 시험 호출 3건이 order 로 가서 200 으로 끝나지만 모두 30ms 를 넘어 느린 호출 비율 100% 로 다시 OPEN. 시험 호출 자리가 찬 동안 들어온 호출은 'is HALF_OPEN and does not permit further calls' 로 막힌다.
6. 하류는 정상: order-service 는 들어온 checkout 을 평소처럼 처리해 'Cleared cart after checkout' INFO 를 남기고 200 으로 답한다. 들어오는 checkout 수만 서킷이 허용한 만큼으로 줄어든다. order, payment, 외부 결제 mock, banking transfer 에 오류가 없다.
7. 다른 라우트: product(평시 게이트웨이 서버 스팬 p99 6.9ms), cart(POST p90 10.2ms), user(POST p90 8.3ms) 서킷은 설정이 바뀌지 않았고 바뀌었더라도 30ms 아래라 영향이 없다. 토큰 검증(AuthGuard → user)도 정상이라 checkout 실패는 401 이 아니라 502 다.

로컬 실측(2026-10-11, 104): 저장소 `commerce/api-gateway` 를 그대로 빌드한 jar(Boot 3.4.5, Resilience4j 2.2.0, 109 이미지 `commerce-gateway:latest` 2026-08-07 빌드 이후 게이트웨이 소스 변경 없음)를 같은 `SPRING_APPLICATION_JSON` 으로 띄우고, 45ms 뒤 200 을 주는 가짜 order 와 토큰 검증 200 을 붙여 checkout 40건을 0.1초 간격으로 보냈다. 결과는 `200×5, 502×11, 200×3, 502×11, 200×3, 502×7`: 5건 뒤 열리고 half-open 3건이 통과한 뒤 다시 열렸다. 게이트웨이 로그에 'CircuitBreaker 'order' is OPEN' 과 'is HALF_OPEN' WARN 이 실패 한 건에 두 줄(재시도 포함)씩 남았다. env 없이 띄우면 모두 200 이다.

## 5. 원인 규정 (원칙 5, 6)

| 칸 | 값 | 근거 |
|---|---|---|
| 근본(`root_cause.target_id`) | `commerce-gateway`(target_kind config) | 고쳐야 재발이 막히는 곳은 게이트웨이의 서킷브레이커 설정이다. order 는 정당한 요청을 정상 시간 안에 처리했고, 서킷을 연 판단은 게이트웨이 설정이 내렸다. 원본도 "errant configuration in the Circuit Breaker" 를 원인으로 지목했다 |
| 계기(`trigger_target_id`) | 비움 | 계기(게이트웨이 설정 롤아웃)가 근본과 같은 곳에서 일어났다 |
| 부분 점수(`scoring.partial`) | `commerce-order` | 증상 라우트의 하류. 게이트웨이 로그가 'CircuitBreaker 'order' is OPEN' 이라 order 를 지목하는 답이 가장 흔할 것이다. 원인 근처(그 서킷이 지키는 하류)지만 정답은 아니다 |

원칙 5: 정답은 관제 데이터로 낼 수 있다. ① 고장 시작 직전 게이트웨이 롤아웃(KCM 이벤트)과 새 ReplicaSet 스펙의 env 한 줄(`kcm_resources_history`), ② 게이트웨이 WARN 이 'is OPEN' 뿐이고 그 앞에 하류 실패 로그(I/O error, Read timed out, 5xx)가 없음, ③ 같은 시각 order 가 200 으로 정상 처리하고 처리 시간이 평소(약 45ms)와 같음. 셋을 이으면 "하류는 멀쩡한데 게이트웨이가 스스로 끊는다, 바뀐 것은 게이트웨이 설정"이 된다. 코드 설계 결함을 추론할 필요가 없다. 임계 30ms 와 order 처리 시간 37~50ms 의 비교도 스펙과 지연 지표만으로 된다.

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 | 이 시나리오에서 |
|---|---|---|
| 감지 | 게이트웨이 골든 시그널과 로그 | commerce-gateway 서버 스팬 POST /api/orders/** 가 502 로 끝나 게이트웨이 오류율(VM `apm.agent.otel.java.error_rate{service_name="commerce-gateway"}` 평시 0, CH 트레이스 5xx)이 오른다. 게이트웨이 WARN 로그가 평시 0(30일 중 2026-10-06 10:39~11:30 한 구간뿐)에서 실패 checkout 한 건에 두 줄씩, 동반 부하 기준 초당 약 7줄로 는다. 인시던트가 생겨야 하는 근거: 고장 약 14분 연속, 동반 부하 checkout 초당 약 4건의 약 85~90% 가 502 |
| 피해 판정 | 러너 k6 요약 | 동반 부하 checkout 5xx 비율(평시 0) ≥ 0.5, 3틱. 상품 목록 실패율은 0 그대로 |
| RCA | 녹화된 데이터 | 게이트웨이 롤아웃 KCM 이벤트, 새 ReplicaSet 스펙의 `SPRING_APPLICATION_JSON`(resilience4j order slow-call 30ms), 하류 실패 없이 시작되는 'is OPEN' WARN, order 의 정상 처리('Cleared cart after checkout' 는 서킷이 허용한 만큼만, 오류 0), order 처리 시간 평소와 같음 |

## 7. 관측 근거 표 (119 실조회, 2026-10-11 04:00~04:40 UTC, 평시)

| 증거 | 119 표와 칸 | 조회 | 결과(평시) |
|---|---|---|---|
| 근본: 설정이 스펙에 잡히는가 | PG `kcm_resources_history`(kind, namespace, name, yaml, captured_at) | `kind='replicaset' AND namespace='rca-testbed-commerce'` | commerce ReplicaSet 1,715건(2026-07-13~10-11), 그중 `SPRING_APPLICATION_JSON` 56건(모두 order, inventory: F08-P, F23-R 실행), `resilience4j` 를 담은 레코드 0건. testbed-gateway ReplicaSet 32건 모두 `SPRING_APPLICATION_JSON` 없음, 마지막 2026-08-11. yaml 에 컨테이너 env 배열 전체가 담김(예 testbed-gateway-75975949bb: JAVA_TOOL_OPTIONS, OTEL_* ). ReplicaSet 수집은 오늘까지 이어짐(최근 5일 47건, 마지막 2026-10-11 03:38) |
| 계기: 게이트웨이 롤아웃 | CH `kcm_events_local`(namespace, object_name, reason, timestamp) | `namespace='rca-testbed-commerce' AND object_name LIKE 'testbed-gateway%'` | 보존(2026-08-13~) 안 게이트웨이 이벤트는 2026-08-20 노드 NotReady 축출과 재기동, Unhealthy 55건뿐이고 ScalingReplicaSet 0건. 주입의 롤아웃은 보존 안에서 처음인 게이트웨이 롤아웃이다 |
| 전파: 서킷 열림 로그 | CH `lucida_logs_local`(service_name, severity_text, body) | `service_name='commerce-gateway'` 30일, 날짜별 | 2026-10-06 하루(10:39~11:30 UTC) 22,022줄뿐(order 서킷 8,700, product 서킷 다수). 그 구간에는 'I/O error on POST request for "http://testbed-order:8080/..."' 1,066줄이 함께 있었다(그날 10:44 order 가 재기동, 'Started OrderServiceApplication in 58.636 seconds'). 그 밖의 날은 0줄. 이 시나리오에서는 'is OPEN' 이 하류 실패 로그 없이 나와야 한다 |
| 하류 정상: order 처리 | CH `lucida_logs_local` | `service_name='commerce-order' AND body LIKE 'Cleared cart after checkout%'` 일별 | 2026-10-08 33,388, 10-09 40,413, 10-10 41,682줄(checkout 성공마다 한 줄, 전수). 고장 중에는 서킷이 허용한 호출 수만큼으로 줄고 order ERROR 는 없어야 한다 |
| 결함의 크기: 임계 대 정상 처리 시간 | CH `otel_traces_local`(service_name, span_kind, span_name, duration_ns) | 24시간, 게이트웨이 CLIENT 스팬 중 `url.full` 이 testbed-order, order SERVER `POST /api/orders/checkout` 의 분위 | 게이트웨이 → order 호출 1% 36.9ms, 5% 38.1, 10% 39.0, 25% 41.2, 50% 47.0, 75% 51.5, 90% 324.6, 99% 344.1ms(4,631건). order 서버 스팬 1% 35.3, 50% 45.4ms. 30ms 아래는 0건이다. 표본(10%)이지만 분포의 하한을 보이는 보조 증거이고 핵심 증거로 쓰지 않는다 |
| 다른 라우트 | 같은 표 | 게이트웨이 SERVER 스팬 이름별 6시간 | GET /api/products/** p99 6.9ms(25,484건), POST /api/carts/** p90 10.2, POST /api/users/** p90 8.3, POST /api/orders/** p50 53.7, p90 334ms |
| 감지 신호 | VM `apm.agent.otel.java.error_rate{service_name="commerce-gateway"}` | 최근 1시간 | 0(분당 값). 표본 기반이라 추세 신호로만 쓴다 |

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다: 근본은 ReplicaSet 스펙(전수)과 KCM 이벤트(전수), 전파와 하류 정상은 로그(전수)로 보인다.

## 8. 감별

- must_support: ① 고장 시작 직전 testbed-gateway 롤아웃(KCM, 보존 안 처음)과 새 ReplicaSet 스펙의 `SPRING_APPLICATION_JSON`(order 서킷 slow-call 30ms, 50%). ② 게이트웨이 WARN 'CircuitBreaker 'order' is OPEN/HALF_OPEN and does not permit further calls' 가 롤아웃 뒤 급증하고, 그 앞뒤에 'I/O error', 'Read timed out', order 의 5xx 가 없다. ③ order 는 같은 시각 'Cleared cart after checkout' 를 계속(허용된 호출만큼) 남기고 오류가 없으며 처리 시간이 평소 37~50ms 다. ④ 다른 라우트(상품 목록, 장바구니, 로그인)와 product, cart, user 서킷은 정상. ⑤ 동반 부하 checkout 5xx 비율 평시 0 → 약 0.85~0.9.
- must_rule_out:
  - order 자체 장애가 아님: order 파드 Ready, 재시작 없음, 오류 로그 없음, 처리 시간 평소와 같음. order 가 죽거나 막혔다면(2026-10-06 구간, F03-H 스레드풀 고갈) 게이트웨이 로그에 I/O error 나 Read timed out 이 먼저 나와 실패율로 서킷이 열린다.
  - payment, 외부 결제, 은행 정산 장애가 아님: 그 경우 order 가 checkout 을 처리하다 실패해 order 오류 로그(결제 실패, 502)가 나고 게이트웨이는 order 의 5xx 응답을 그대로 돌려준다(F01-H, F06-R, F08-P, F40-R). 이 시나리오에서는 order 에 닿은 호출이 모두 200 이다.
  - 진입 폭주나 게이트웨이 과부하가 아님: 상품 목록, 장바구니 라우트 지연이 그대로이고 게이트웨이 재시작, CPU 포화가 없다. 동반 부하는 F08-P(20rps)보다 낮은 8rps 다.
  - 인증 실패가 아님: 실패 코드가 401 이 아니라 502 이고 user 서킷과 로그인은 정상(F16-H 와 다름).
  - 코드나 이미지 배포가 아님: 새 ReplicaSet 의 이미지(commerce-gateway:latest), 프로브, 자원이 옛 ReplicaSet 과 같고 env 한 줄만 다르다.
- contrast_with: F08-P(같은 checkout 경로의 설정 배포지만 바뀐 것이 order 의 payment read-timeout 이라 요청이 하류에 닿은 뒤 시간 초과로 끊기고 order 가 5xx 를 낸다), F03-H(order 스레드풀 고갈로 게이트웨이 order 서킷이 하류 실패 때문에 열린다), F16-H(게이트웨이를 지나는 쓰기가 실패하지만 원인은 user 하류의 readiness, 401), F39-R(호출하는 쪽 설정 배포가 정상 하류 호출을 스스로 끊는 같은 계열, 은행 account 의 하류 주소).

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

설계 강도 하나로 고정한 평가 모드(`approved-fixed-f64-r`). 강도라 할 값은 임계 하나(30ms)이고, 게이트웨이에서 본 order 호출의 평시 최소(약 37ms)보다 낮게 골라 모든 호출이 느린 호출이 된다. 임계를 정상 분포 안(예 45ms)에 두면 열림 여부가 지연 변동에 따라 들쭉날쭉해지므로 피한다.

- 열림 주기: 창 10, 최소 5, 느린 비율 100% ≥ 50% → OPEN 5초 → HALF_OPEN 시험 3건(모두 느림) → OPEN. 한 주기 ≈ 5초 + 시험 3건이 모이는 시간.
- 동반 부하: commerce surge.js(여정 browsing 35%, cart 15%, checkout 50%), target_rps 8 → checkout 초당 약 4건 + 기준선 약 0.1~0.5건. 한 주기 약 5.7초에 막힘 약 22건 대 통과 3건 → checkout 5xx 비율 약 0.85~0.9(성공 문턱 0.5). 30초 창 checkout 약 120건이라 표본 잡음이 아니다.
- 8rps 를 고른 이유: 실패 전 구간(패치 뒤 새 게이트웨이 파드 Ready 까지 약 1분, 동반 부하 ramp 2분의 앞부분)의 checkout 이 재고를 줄인다. 재입고 배치(1분마다 20 미만 상품을 60 으로)가 따라가는 F40-R 6rps 근처로 두었다. 고장 중에는 order 에 닿는 checkout 이 시험 호출뿐이라 재고가 거의 줄지 않는다. 20rps(F08-P)도 가능하지만 판정에 필요 없다.
- 한 요청의 지연: 막힌 checkout 은 시도 2회(재시도 대기 300ms) 뒤 약 0.3초 만에 502. 스레드나 연결을 오래 쥐지 않아 게이트웨이 Tomcat(기본 200) 과 order 풀에 부담이 없다.
- 기준선만 있을 때(동반 부하 끝난 뒤, 한가한 시간 checkout 초당 약 0.1~0.5건): 5건이 모이는 데 수십 초가 걸려 실패 비율이 약 0.5 안팎으로 낮아지지만 고장은 이어진다. 판정은 동반 부하 구간에서 끝난다.
- 서킷브레이커가 피해를 숨기지 않는다: 이 시나리오에서 서킷 자체가 피해를 만든다. 재시도는 막힌 호출을 한 번 더 막힐 뿐이다.
- 게이트웨이 health 는 서킷 상태와 무관하다(서킷 health indicator 미등록, management 설정 없음). 그래서 진입점이 살아 있고 entry_status 가 0 이 되지 않는다.

| 항목 | 값 |
|---|---|
| level | `approved-fixed-f64-r`, min_hold 10m, settle 60s, timeout 16m |
| max_injection_duration | 25m |
| companion | `load.north_south` commerce surge.js, target_rps 8, entry 30080, ramp 2m + hold 17m + ramp_down 15s, seed 6464 |
| success | 동반 부하 checkout 5xx 비율(`loadgen.checkout_5xx_rate`) ≥ 0.5, 3틱 연속 |
| must_rule_out | 동반 부하 achieved_rps < 2(부하 끊김), 동반 부하 상품 목록 실패율(`loadgen.read_step_status_rate`) ≥ 0.2(게이트웨이 전체나 product 장애), order 파드 NotReady(order 자체 장애), order 서버 스팬 5xx 비율(`clickhouse.service_error_rate` commerce-order) ≥ 10%(하류가 실제로 실패하는 F08-P, F03-H 꼴), 2틱 |
| abort | entry_status(commerce 진입점) == 0, 2틱. 게이트웨이와 nginx 는 살아서 다른 라우트에 답하므로 이 시나리오로는 0 이 나오지 않는다 |
| recovery | 기준선 checkout 5xx 비율(`loadgen.checkout_5xx_rate` {domain: commerce}) < 0.05 가 판정력을 가진 조건, target_health 200 과 order 파드 Ready 는 형식 조건. 2틱, 10m |
| cleanup | env 를 원래 배열로 패치한 뒤 롤아웃 available 확인(180초), 동반 부하 종료, 10m |

판정 근거(평시 값과 장애 값):

| 조건 | 관측 | 평시 | 장애 중 | 판정력 |
|---|---|---|---|---|
| success | 동반 부하 checkout 5xx 비율 | 0(업무 거절은 400, 409) | 약 0.85~0.9 | 있음(문턱 0.5) |
| must_rule_out reads-failing | 동반 부하 상품 목록 실패율 | 0 | 0(product 서킷 무관) | 교란(게이트웨이 전체, product 장애)일 때만 0.2 이상 |
| must_rule_out order-erroring | order 서버 스팬 5xx 비율(1분, 표본) | 0 | 0(order 에 닿은 호출은 200) | 교란(order, payment 쪽 실패)일 때만 오른다 |
| recovery | 기준선 checkout 5xx 비율 | 0 | 약 0.5 안팎(기준선만일 때) | 있음(문턱 0.05) |

새 관측 쿼리, 새 실행기, 러너 변경은 없다. 기존 `k8s.env` 실행기의 허용 표(APPROVED_TARGETS, APPROVED_KEYS)에 F64-R(testbed-gateway, api-gateway, 키 SPRING_APPLICATION_JSON)만 더했다. 러너 kubernetes 프로브 허용 목록(live_probes APPROVED_K8S_TARGETS)에 testbed-gateway 와 testbed-order 가 이미 있다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Spreedly 2022-03-28, 공식 상태 페이지 사후 보고)와 요소별 대응표가 있고, 기전("서킷브레이커 설정 오류로 의도보다 낮은 임계에서 작동해 멀쩡한 하류로 가는 요청을 막고, 설정을 고쳐 복구")이 같다 (원칙 1, §2)
- [x] 장부를 갱신했다: 묶음 G(8 → 9, 84 중 10.7%), 정답 위치 게이트웨이(0 → 1), 결제 경로 12/84(14.3%, 무관), 서비스 commerce 29(최다지만 정답 위치 0 부품), 어느 축도 20% 에 닿지 않는다 (원칙 2, §3)
- [x] 근본 원인 위치: 게이트웨이 Deployment env(`29-api-gateway.yaml`), 서킷 설정(`application.yml:49-99, 122-126`), 서킷과 fallback(`ProxyService.java:81-85, 102-128`) 확인 (G1, §4)
- [x] 근본 원인의 흔적: PG `kcm_resources_history` ReplicaSet env(평시 commerce 1,715건 중 resilience4j 0건, 게이트웨이 32건 모두 SPRING_APPLICATION_JSON 없음) (원칙 3, §7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: ReplicaSet 스펙과 KCM 이벤트(전수), 게이트웨이와 order 로그(전수). 트레이스 지연 분포는 보조 (원칙 3)
- [x] 계기의 흔적: 게이트웨이 롤아웃 KCM 이벤트(보존 안 처음)와 새 ReplicaSet 스펙. 인공 지연을 쓰지 않는다. order 의 처리 시간은 평소 그대로다 (원칙 4)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다: 롤아웃 직후 하류 실패 없이 시작된 서킷 열림 로그, 정상인 하류, 스펙의 env 한 줄 (원칙 5)
- [x] 정답지 세 칸: 근본 `commerce-gateway`(config), 계기 비움, 부분 점수 commerce-order (원칙 6, §5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다 (원칙 7, §6)
- [x] 주입 도구가 시나리오 id 를 남기지 않는다: k8s.env 실행기는 네임스페이스, Deployment, 컨테이너, env 배열만 kubectl 로 넘기고, 클러스터에 남는 것은 env 한 줄(값에 id 없음)과 롤아웃뿐이다. 동반 부하 태그는 tb-runner 의 k6 쪽에만 남는다(기존 시나리오와 같음) (원칙 8)
- [x] 피해 계산: 임계가 정상 분포 하한 아래라 모든 호출이 느린 호출, 열림 주기와 동반 부하로 checkout 5xx 약 0.85~0.9, 재고와 스레드 여유, 진입점 생존 (원칙 9, §9)

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- 새 게이트웨이 파드가 Ready 가 되어 옛 파드를 대체하는 시간과 고장 시작 시각.
- 게이트웨이 WARN 이 실제로 'CircuitBreaker 'order' is OPEN and does not permit further calls' 와 'is HALF_OPEN ...' 꼴이고 하류 실패 로그(I/O error, Read timed out)가 없는지. 다르면 정답지의 결정 증거 문구를 녹화본에 맞게 고친다.
- order 'Cleared cart after checkout' 가 고장 중 몇 건으로 줄었는지(서킷이 허용한 시험 호출 수와 맞는지), order 오류 0.
- KCM 이 새 ReplicaSet 을 `SPRING_APPLICATION_JSON`(resilience4j order slow-call 30ms) env 로 잡는지.
- 동반 부하 checkout 5xx 비율이 계산(0.85~0.9) 근처인지, 상품 목록 실패율 0 인지.
