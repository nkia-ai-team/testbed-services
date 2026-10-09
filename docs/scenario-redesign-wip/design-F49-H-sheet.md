---
title: F49-H 설계 시트 (food order-service 를 시각 표기를 바꾼 릴리스로 롤아웃해 dispatch 응답을 못 읽음)
status: Draft
owner: project
last_reviewed: 2026-10-09
tags:
  - scenario
  - design
  - release
summary: food order-service 를 결함 있는 새 릴리스(food-delivery-order:2.3.0, fault-images/f49-h 패치로 만든 별도 태그)로 롤아웃하면, 주문 API 의 시각 표기를 앱 화면 표준(yyyy-MM-dd HH:mm:ss)으로 바꾸며 하류 호출에도 같은 ObjectMapper 를 써서, 바뀌지 않은 dispatch 가 ISO 시각으로 보내는 배차 응답을 order 가 읽지 못하고 모든 새 주문이 503 으로 실패하는 시나리오. 원본은 Onfido 2025-01-24 타임스탬프 필드의 하위 비호환 변경 배포, 보조는 Piano 2024-03-20 날짜·시각 라이브러리 갱신이 기존 표기를 거절한 장애.
---

# F49-H 설계 시트

## 1. 요약

food order-service 의 새 릴리스 2.3.0 은 주문 API 가 내보내는 시각을 앱 화면 표준 `yyyy-MM-dd HH:mm:ss` 로 맞추려고 Spring ObjectMapper 에 그 패턴의 `LocalDateTime` 직렬화기와 역직렬화기를 등록하고, "하류 호출도 같은 규칙" 이라며 restaurant, dispatch, payment 로 가는 RestClient 도 그 ObjectMapper 를 쓰게 바꾼다. 릴리스 자신의 단위 시험은 새 표기의 왕복만 확인해 통과한다. health 와 DB 는 그대로라 새 파드가 Ready 가 되어 기본 롤링 전략으로 옛 파드를 대신한다.

dispatch-service 는 바뀌지 않았다. 배차 요청 `POST /api/deliveries/dispatch` 에 배차를 기록하고 200 과 함께 `assignedAt` 을 평소대로 ISO 꼴(`2026-10-09T20:36:16.123456`)로 돌려준다. order 는 이 값을 새 패턴으로 읽다 실패하고 RestClient 가 `Error while extracting response for type [com.fooddelivery.common.dto.DispatchResponse] and content type [application/json]` 을 던진다. DispatchClient 는 `Failed to dispatch courier for order <id>: ...` 를 남기고 503 으로 바꾼다. 가게, 메뉴, 용량 응답에는 시각이 없어 그대로 읽힌다. 용량 확인과 배차 호출은 같은 `dispatch` 서킷브레이커를 쓰고, 해석 실패가 이 서킷을 열림(5초)과 반열림 사이로 계속 돌린다. 그래서 주문은 두 갈래로 끝난다.

- 서킷이 닫히거나 반열림일 때 들어온 주문: 검사를 지나 주문 행을 넣고('Created order id=...'), 시도마다(재시도 최대 3회) dispatch 가 배차를 기록한 **뒤에** DispatchResponse 해석 실패, 'Order fan-out dispatch failed: ... CircuitBreaker 'dispatch' is OPEN', 트랜잭션 롤백으로 끝난다.
- 서킷이 열려 있을 때 들어온 주문(다수): 용량 확인 단계에서 `checkCapacityFallback` 이 503 'Dispatch service unavailable: CircuitBreaker 'dispatch' is OPEN ...' 을 던지고 OrderService 가 그대로 다시 던진다. order 로그도 주문 행도 남지 않는다.

로컬 실측 240건 중 앞의 꼴이 65건, 뒤의 꼴이 175건이었다. 어느 쪽이든 모든 새 주문이 약 0.6초 만에 503 이다.

비유: 주문 접수처(order)가 "날짜는 이제 2026-10-09 12:30:00 식으로만 쓴다" 는 새 규칙을 받아 들였는데, 그 규칙을 바깥에서 들어오는 서류를 읽는 데도 적용했다. 배차실(dispatch)은 예전처럼 2026-10-09T12:30:00 으로 적은 배차 확인서를 돌려주고, 접수처는 그 확인서를 못 읽어 주문을 모두 무른다. 배차실은 배차를 제대로 했고 서류도 예전과 똑같다. 고칠 곳은 규칙을 바꾼 접수처의 새 릴리스다.

## 2. 원본 사례

- **Onfido, 2025-01-24** (공식 상태 페이지 사고와 사후 보고, 게시 2025-01-31): https://status.onfido.com/incidents/r2pc6vdpjsm2
  - "Device Intelligence Report Failure to run", 14:48~15:26 UTC. 원인은 Device Intelligence 보고서 계산 갱신 중 들여온 타임스탬프 필드의 하위 비호환 변경("backwards-incompatible change to a timestamp field"). 보고서가 철회되고 함께 돌던 보고서도 영향, 이 보고서가 든 Studio 워크플로가 Error 상태(실행 중이던 워크플로 약 12%, Check 약 9%). 14:59 내부 경보가 비긴급으로 잘못 설정돼 있었고 15:19 담당 팀에 알려 15:25 되돌림, 15:27 해결. 후속으로 "시스템 전반의 엄격한 타임스탬프 처리 표준".
- **보조: Piano(Cxense API), 2024-03-20** (공개 RCA PDF): https://docs.piano.io/wp-content/uploads/2024/04/2024-03-20-RCA-Cx-APIs-Failed-Requests.pdf
  - 13:20 UTC Java 날짜, 시각 라이브러리 갱신을 API 서버에 배포. 그 뒤 "+0000" 시간대 표기를 쓰던 요청이 실패(약 15시간). 자동 시험 어느 것도 그 표기로 부르지 않아 모두 통과했다. 고객 문의로 알고 되돌림. 후속은 실제로 쓰이는 모든 날짜, 시각 표기를 시험에 넣고 API 실패율 경보를 더함.
  - 이 PDF 는 2026-10-09 에 직접 열면 첫 화면으로 돌려보내져(301) 원문을 받지 못했다. 내용은 같은 날 웹 검색 색인에 남은 본문에서 옮겼다(자료 문서 §3 에 적음). 그래서 원본은 링크가 살아 있는 Onfido 로 두고, Piano 는 "배포한 쪽이 기존 표기를 읽지 못한다" 는 방향과 "시험이 새 표기만 봤다" 는 세부의 보조 근거로만 쓴다.
- 자료 문서 `ref-real-world-incidents.md` M8 표에 두 사례를 더했다(출처가 말한 사실만).

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 계산 갱신 배포(Onfido), 날짜·시각 라이브러리 갱신 배포(Piano) | order-service 를 릴리스 food-delivery-order:2.3.0(시각 표기 표준화)으로 롤아웃(KCM ScalingReplicaSet, 새 파드 Pulled 이벤트에 태그) |
| 원인이 된 결함 | 타임스탬프 필드의 하위 비호환 변경(Onfido). 배포된 쪽이 상대가 계속 쓰는 기존 시각 표기를 받아들이지 못함, 시험은 그 표기를 다루지 않음(Piano) | order 의 ObjectMapper 시각 처리가 yyyy-MM-dd HH:mm:ss 만 받게 바뀌고 하류 호출에도 적용됨. dispatch 가 계속 쓰는 ISO 표기를 읽지 못함. 릴리스 시험은 새 표기 왕복만 확인 |
| 전파 경로 | 시각 필드를 주고받는 처리(보고서, 함께 돌던 보고서, 워크플로)가 실패 | order 의 배차 호출이 응답 해석에서 실패 → 재시도, 서킷 → 주문 트랜잭션 롤백 → 주문 생성 503 |
| 사용자 증상 | 워크플로 Error, 요청 실패. 다른 기능은 정상 | 모든 새 주문 503. 가게 둘러보기, 메뉴, 검색, 배달 조회, 주문 조회는 정상, dispatch 는 200 |
| 원본의 탐지 경로 | 내부 경보(비긴급 설정)와 담당 팀 확인(Onfido), 고객 문의(Piano) | lucida-next 의 order 오류율, order ERROR 로그 급증. dispatch 스팬은 200 이라 실패는 호출자(order) 안에서만 보인다 |
| 완화와 복구 | 배포 되돌림 | cleanup 이 order 이미지를 매니페스트 이미지로 되돌림(롤백) |

기전 동일성: 원본과 재구성 모두 "어느 구성요소의 새 배포가 시각 값을 다루는 방식을 하위 비호환으로 바꿨고, 상대는 예전 표기 그대로라 그 값을 주고받는 처리가 실패한다, 되돌리면 낫는다" 는 고리다. Piano 처럼 바뀐 것은 값을 **읽는** 배포된 쪽이고 상대(Piano 의 고객, 여기서는 dispatch)는 그대로다. 대상(보고서 계산, API 서버 → 주문 서비스)과 필드(보고서 타임스탬프, 요청 시간대 → 배차 응답 assignedAt)만 우리 스택에 맞게 바꿨다. 결함은 한 서비스, 한 설정 클래스 꼴로 작게 두었다.

## 3. 숫자 근거 (2026-10-09, `scenario-stats.py`, 정식 42 + 후보 14 = 56 → 57)

- 서비스(정식 + 후보): commerce 26, core-banking 16, **food-delivery 14(가장 적음)** → 15
- 묶음: A 7, B 7, D 7(각 12%), C 6, G 6, J 6(10.7%), **L 4** → 5(57 중 8.8%), F 3, E 2, H 2, K 2, I 1, M 1, O 1, P 1, N 0. J 로 세도 6→7(12.3%). 20% 상한(12) 아래
- 정답 위치: **주문 서비스** 6 → 7(12.3%). 부품 지도에서 **food order 는 0**(주문 서비스 6 은 모두 commerce order). 상한 아래
- 결제 경로 합계 10(17.9%) → 그대로 10/57(17.5%)
- 왜 이 후보인가: 가장 적은 서비스(음식배달)에서 부품 지도 0 인 부품(food order, kafka, notify) 가운데 사용자 경로에 있는 것은 order 뿐이다(kafka, notify 는 비동기 소비라 막힌 목록의 원칙 7 벽). 그동안 food order 정답 후보는 "order 풀이 DB 대기로 묶이면 readiness 가 빠져 진입점 0" 벽에 막혔는데, 이 후보는 실패가 응답 해석에서 빠르게 나서 풀을 묶지 않는다(로컬 실측 order health 내내 UP). 증상(order 503, 하류는 200)이 F32-H, F49-R 과 같고 정답이 다른 관계 H 라 관제 AI 가 "누가 바뀌었나" 를 추론해야만 맞힌다. 가르는 관측 근거는 §8.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 계기: `rca-testbed-food` Deployment `testbed-order` 이미지 `food-delivery-order:latest` → `food-delivery-order:2.3.0`(`food-delivery/k8s/20-order-deploy.yaml:27-28`, imagePullPolicy Never, nodeSelector tb-w3 `:18-19`, replicas 1 `:9`, 전략 RollingUpdate maxSurge 25%(109 kubectl 2026-10-09)).
2. 결함(릴리스 2.3.0): `scripts/scenarios/fault-images/f49-h/order-service.patch:17-29`(JacksonTimeConfig: LocalDateTime 직렬화기·역직렬화기를 `yyyy-MM-dd HH:mm:ss` 로 등록), `:49-71`(RestClientConfig: 하류 RestClient 가 같은 ObjectMapper 의 컨버터로 응답을 읽음), `:94-112`(JacksonTimeConfigTest: 새 표기 왕복만 시험).
3. 계약: `food-delivery/shop-common/src/main/java/com/fooddelivery/common/dto/DispatchResponse.java:5-12`(assignedAt 은 LocalDateTime). dispatch 쪽 값 `food-delivery/dispatch-service/src/main/java/com/fooddelivery/dispatch/entity/Dispatch.java:26-27`(`LocalDateTime.now()`, 매니페스트 dispatch 는 Spring 기본 ISO 로 씀).
4. 호출자: `food-delivery/order-service/src/main/java/com/fooddelivery/order/service/OrderService.java:55-150`(createOrder: 가게, 메뉴, 용량 확인(`:94-112`, ServiceException 은 로그 없이 다시 던짐) → 주문 저장 → `:143-150` 배차 호출) → `order/client/DispatchClient.java:55-69`(`body(DispatchResponse.class)` 해석 실패 → RestClientException → `log.error("Failed to dispatch courier for order {}: {}")` → ServiceException 503). 재시도 3회 200ms 지수, 서킷 10건 창 50% 5초 열림(용량 확인과 배차 호출이 같은 `dispatch` 서킷, `order-service/src/main/resources/application.yml:55-58, 78-86, 105-111`). 서킷이 열려 있으면 용량 확인이 `DispatchClient.java:47-50`(checkCapacityFallback)에서 로그 없이 503 으로 끝나, 그 주문은 주문 행도 배차도 만들지 않는다.
5. 영향 밖: 가게 상세, 메뉴(RestaurantResponse, MenuResponse), 용량(Map)은 시각이 없어 그대로 읽힌다. order 의 health 는 DB 만 보고(`/actuator/health`), 주문 조회는 order 자신의 DB 읽기라 그대로다. payment 는 부르지 않는다.
6. 부작용: dispatch 는 배차 요청마다 ASSIGNED 배차를 기록하고 200 을 돌려주므로, 커밋되지 않은 주문의 배차가 남는다(로컬 실측 120초에 108건, 초당 약 0.9건). 평시 배차(초당 1.2~2.6건, 119 로그 2026-10-08~09 시간당 627~4,171건)보다 적어 용량 2000 을 채우지 않고, ETA 15~34분 뒤 만료 배치가 DELIVERED 로 넘긴다.

## 5. 원인 규정 (원칙 6)

- `root_cause.target_id`: `food-delivery-order`(target_kind container). 결함을 가진 곳이자 되돌려야 재발이 막히는 곳은 시각 처리 계약을 깬 order 새 릴리스다. dispatch 의 응답(B)은 예전과 같고 정당하다. 요청 A(order 의 배차 호출)를 보낸 order 의 새 로직이 정당한 응답을 읽지 못한 것이므로, 원칙 6 의 "잘못된 명령 A" 쪽인 order 가 원인이다. 원본 Onfido, Piano 모두 변경을 배포한 쪽을 되돌렸다(같은 층위). 결함 이미지 규칙 7(근본은 그 서비스의 새 버전)과 맞는다.
- `trigger_target_id`: 없음(계기인 롤아웃과 근본이 같은 곳).
- `scoring.partial`: `food-delivery-dispatch`, `food-dispatch`, `testbed-dispatch`(해석에 실패한 응답을 낸 곳. 실패 로그가 DispatchResponse 를 가리켜 이쪽을 짚을 수 있다).
- 코드 줄을 맞히라고 요구하지 않는다(원칙 5). "order 롤아웃 직후 order 가 바뀌지 않은 dispatch 의 200 응답을 해석하지 못함 → order 릴리스" 는 관제 데이터(KCM 이벤트, order 로그, 스팬 상태, dispatch 무변경)로 낼 수 있는 결론이다. 시각 패턴이라는 결함 내용까지는 요구하지 않는다(로그에 해석 실패의 내부 원인은 나오지 않는다).

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 | 기대 |
|---|---|---|
| 감지(lucida-next) | order 서버 스팬 오류율(POST /api/orders 503), order ERROR 로그 급증(해석 실패 문장, application/json 판 평시 0) | 별도 규칙 없이 이벤트, 인시던트 생성. 같은 꼴(order 503 + order 오류 로그)의 F32-R 이 정식 녹화됐다 |
| 피해 판정(러너) | 동반 부하 k6 문서의 주문 생성 5xx 비율 ≥ 0.5, 2xx 비율 < 0.2 (3틱) | 평시 5xx 0 근처, 장애 시 1.0 |
| 원인 설명(RCA) | KCM 의 testbed-order 롤아웃과 이미지 태그, order 로그 문장(DispatchResponse, application/json), order→dispatch 클라이언트 스팬 200, dispatch 'Dispatched courier ... order=<id>' 와 order 실패 주문 번호의 일치, dispatch 무변경 | §7 |

## 7. 관측 근거 (119 실조회, 평시)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 핵심 증거는 KCM 이벤트, 자원 이력, 전수 로그다.

| 증거 | 119 표와 칸 | 조회 | 결과(2026-10-09) |
|---|---|---|---|
| 계기: 롤아웃과 이미지 태그 | CH `lucida.kcm_events_local`(timestamp, reason, object_kind, object_name, body) | `namespace='rca-testbed-food' AND object_name LIKE 'testbed-order%' AND reason!='Unhealthy'` 30일 | 2026-10-01 04:04, 2026-10-08 18:52 두 번의 롤아웃이 ScalingReplicaSet 'Scaled up replica set testbed-order-8694c6f795 to 1', Pulled 'Container image "food-delivery-order:latest" already present on machine', Killing, SuccessfulDelete 로 수집됨(태그가 본문에 담김). 새 파드 Scheduled 18:52:14 → 옛 파드 Killing 18:53:00, 46초 |
| 계기: ReplicaSet, 파드 스펙의 이미지 | PG lucida `kcm_resources_history`(kind, name, yaml, captured_at) | `namespace='rca-testbed-food' AND name LIKE 'testbed-order%'` 종류별 수와 `position('food-delivery-order:latest' in yaml)>0` 수 | replicaset 35건, pod 29건 전부 :latest(마지막 2026-10-08 18:53). 장애 때는 2.3.0 ReplicaSet 이 남아야 한다 |
| 전파: order 해석 실패 로그 | CH `lucida.lucida_logs_local`(service_name, severity_text, body) | food-delivery-order, `body LIKE '%Error while extracting response for type [com.fooddelivery.common.dto.DispatchResponse]%'` 30일, content type 별 | 156건, 모두 `[application/octet-stream]`(2026-10-09 03:36~03:44, dispatch 가 응답 없이 끊긴 다른 실행 구간). `[application/json]` 판 0건. 같은 구간 로그로 'Created order id=N' → dispatch 'Dispatched courier=... for order=N' → order 'Failed to dispatch courier for order N' → 'Order fan-out dispatch failed: ... CircuitBreaker 'dispatch' is OPEN' 순서가 전수 로그에 그대로 남는 것을 확인 |
| 전파: 스팬 | CH `otel_traces_local`(service_name, span_kind, span_name, span_attributes['http.response.status_code']) | 7일 order 서버 POST /api/orders, order CLIENT(server.address=testbed-dispatch), dispatch 서버 POST | order 서버 200 17,179, 400 826, 500 170, 502 190, 503 170. order→dispatch 클라이언트 POST 200 17,257, 500 5. dispatch 서버 POST /api/deliveries/dispatch 200 17,274, 500 5. 장애 때는 order 서버 503 이 몰리고 클라이언트, dispatch 서버는 200 이어야 한다 |
| 감별: dispatch 는 그대로 | CH `lucida_logs_local` dispatch | 'Dispatched courier=' 시간당 수 | 평시 시간당 627~4,171건(전수). 장애 때는 order 가 실패라고 남긴 주문 번호로도 나온다 |

로컬 실측(2026-10-09, 104, mysql:8.0 + food init.sql, origin/main d8203b5 의 restaurant, dispatch jar, 패치 전후 order jar, OTel 에이전트 없음, payment 없음):
- 매니페스트 order: 주문 2rps 30초 60건이 가게, 메뉴, 용량, 배차를 지나 payment 단계에서 502(로컬에 payment 없음) — 배차 응답은 읽힘.
- 릴리스 order: 주문 2rps 120초 240건 모두 503, p50 0.63초, p95 1.27초, 최대 1.30초. 응답 본문 'Dispatch service unavailable: CircuitBreaker 'dispatch' is OPEN ...'. 배차 단계까지 간 주문은 65건이다('Created order' 65줄 = 'Order fan-out dispatch failed: ... CircuitBreaker 'dispatch' is OPEN' 65줄 = 'Failed to dispatch courier' 의 서로 다른 주문 번호 65개, 그 로그 줄은 재시도까지 108줄). 나머지 175건은 서킷이 열린 동안 용량 확인 단계에서 로그 없이 503 으로 끝났다. dispatch 'Dispatched courier' 108건(ASSIGNED 60 → 168), orders 표 11,979 → 11,979(커밋 0). order health 실행 전후 UP.
- 릴리스 jar 의 `JacksonTimeConfigTest` 는 통과한다(새 표기만 본다).

## 8. 감별

- must_support: 정답지 `must_support` 4개(롤아웃 이벤트와 태그, 서킷 주기마다 되풀이되는 order 해석 실패 로그와 서킷 로그, order 503 과 dispatch 200 그리고 같은 주문 번호의 배차 기록, dispatch·MySQL·order 의 무고장).
- must_rule_out: F32-H, F33-H, F32-R(배달 쪽 설정, 릴리스, 한도), F33-R, F38-R(배차 표, DB), F49-R, F36-R, F44-R(가게 쪽), F06-P, F19-P, F30-R(결제 쪽), order 자원 고갈이나 다운.
- contrast_with: F49-R, F32-H, F33-H, F32-R, F30-R.
- **같은 증상, 다른 정답(관계 H)을 가르는 관측 근거**:
  - F49-R 과: 둘 다 "order 가 하류 200 응답을 해석하지 못함" 이다. (1) 롤아웃 대상: F49-R 은 testbed-restaurant(1.4.0), F49-H 는 testbed-order(2.3.0) (2) 로그 문장: F49-R 'Failed to fetch restaurant ... RestaurantResponse', F49-H 'Failed to dispatch courier ... DispatchResponse' (3) 단계와 코드: F49-R 은 첫 단계 가게 조회에서 502, F49-H 는 503 이고 해석 실패가 나는 주문은 'Created order' 뒤 배차 단계에서 끝난다(서킷이 열린 동안의 주문은 용량 확인에서 로그 없이 끝남) (4) 하류의 무변경: F49-H 의 dispatch 는 롤아웃이 없다.
  - F32-H 와: 둘 다 order 503 이고 dispatch 는 200 이며, 둘 다 다수 주문이 용량 확인 단계에서 끝난다. 가르는 것은 (1) 로그 문장: F32-H 는 ClassCastException 이 든 'Dispatch capacity check failed, rejecting order', F49-H 는 서킷 주기마다 'Failed to dispatch courier ... DispatchResponse' (F49-H 의 용량 확인 실패는 서킷 열림 fallback 이라 로그가 없다) (2) 롤아웃 대상: F32-H 는 dispatch env 배포, F49-H 는 order 이미지 롤아웃이고 dispatch 는 그대로다.
  - F33-H, F33-R 과: 둘 다 order 503 이지만 dispatch 가 5xx(풀 고갈)를 내고 MySQL 이 포화된다. F49-H 는 dispatch 200, MySQL 평시.
  - F32-R 과: order 503 이지만 'Courier pool exhausted' / 'No courier available' 거절이다. F49-H 는 용량 확인이 통과한다.
- 러너 배제 조건은 achieved_rps, MySQL 파드 둘뿐이다. dispatch 파드 준비 상태와 dispatch 스팬 오류율은 러너 관측 허용 목록(APPROVED_K8S_TARGETS, APPROVED_APM_SERVICES)에 없다(F32-H, F33-H 와 같음, 러너 변경 없이 가기 위해 두지 않음). dispatch 가 멀쩡하다는 감별은 녹화본에서 RCA 가 한다.

## 9. 러너 판정 조건과 강도, 부하 계산

- 주입: k8s.image release 모드, 설계 강도 1단 고정 evaluation(`approved-fixed-f49-h`, min_hold 15m, settle 30s, timeout 20m, max_injection_duration 25m). 강도 사다리 없음, 첫 실행이 곧 녹화 실행.
- success(3틱): 동반 부하 주문 생성 5xx ≥ 0.5, 2xx < 0.2.
- must_rule_out(2틱): achieved_rps < 0.5(부하 끊김), food MySQL NotReady.
- abort: entry_status == 0(2틱). 롤아웃 동안 옛 order 파드가 새 파드 Ready 까지 요청을 받는다(평시 롤아웃 46초). 새 order 는 해석 실패를 약 0.6초(최대 1.3초)에 503 으로 돌려주고 DB 연결은 그동안만 잡혀(주문 초당 약 1.6~2.6건 × 0.6초 ≈ 1~2개, 풀 15) readiness 가 빠지지 않는다(로컬 health 내내 UP). 0 은 노드나 order 파드가 죽었을 때만 나온다.
- 부하: load.north_south `order-surge.js` 2rps(주문 생성 초당 약 1.4건, F36-R, F44-R, F49-R 과 같은 값). 피해는 부하 크기와 상관없이 모든 주문이 실패하는 결정적 꼴이라 강도 계산은 "주문이 들어오기만 하면 된다" 이다. 180rps 상한과 무관.
- 회복: cleanup 뒤 dispatch 서킷이 반열림 3회 성공으로 닫힌다. 주인 없는 배차는 평시 배차보다 적게 쌓여 용량을 채우지 않으므로 recovery(기준선 주문 2xx ≥ 0.7, 10분)를 막지 않는다.
- 배포 전제: 109 docker 에 `food-delivery-order:2.3.0` 이 있어야 preflight 가 통과한다(`bash scripts/scenarios/fault-images/build.sh f49-h`, 배포 단계에서 실행). tb-w3 이미지 파일시스템 52%(2026-10-09, 상한 80%), tb-w3 containerd 에는 food-delivery-order:latest 만 있음. 태그 2.3.0 은 저장소의 다른 시나리오(controllers, profiles, fault-images)가 쓰지 않고 109 docker 에도 없다(폐기된 F46-R 의 2.4.0 과도 다름).

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Onfido 2025-01-24 공식 상태 페이지, 보조 Piano 2024-03-20 공식 RCA)와 요소별 대응표, 기전 동일(§2)
- [x] 분류 장부 갱신(§4-1 행, §6 기록), 묶음 L+J(L 4→5, 8.8%), 정답 위치 주문 서비스(6→7, 12.3%, food order 0→1), 결제 경로 10(17.5%), 서비스 food 14→15(가장 적음), 고른 이유(§3). 어느 축도 20% 미만
- [x] 근본 원인 위치 `file:line` 확인(패치 줄, 공용 DTO, dispatch 엔티티, order 클라이언트와 서비스)과 인프라 지점(20-order-deploy.yaml)(§4)
- [x] 근본 원인 흔적 119 실조회: kcm_events_local 의 order 롤아웃과 Pulled 본문 태그, kcm_resources_history 의 order ReplicaSet, 파드 이미지(§7)
- [x] 핵심 증거가 표본에만 있지 않음: KCM 이벤트, 자원 이력, 전수 로그(order 해석 실패 문장, dispatch 배차 기록). 스팬은 보조
- [x] 계기 흔적: 롤아웃 이벤트와 새 이미지 태그. 인공 지연 없음(응답은 빠르고 결함은 시각 처리)
- [x] 정답이 관제 데이터로 낼 수 있는 결론(자기 롤아웃 직후 호출자 해석 실패, 피호출자 무변경·정상), 코드 설계 결함 추론 불필요(§5)
- [x] 정답지 세 칸이 원칙 6 대로(근본 order 릴리스, 계기 같음, 부분 점수 dispatch)
- [x] 감지, 피해 판정, RCA 증거 구분(§6)
- [x] 주입 도구가 시나리오 id 를 남기지 않음: 실행기 인자는 네임스페이스, Deployment, 컨테이너, 이미지 둘뿐이고, 패치가 더한 문자열에 시나리오 id, fault, bug, chaos 같은 말이 없음(테스트 `test_k8s_image_release_mode_rolls_food_order` 가 확인)
- [x] 부하 상한과 서킷브레이커를 고려한 피해 계산(§9: 서킷이 열려도 fallback 503, 결정적 전량 실패, 로컬 240/240)

### 후보 목록 (이번 반복, "실제 기전 × 부품", 숫자 순)

1. **[채택] food order(주문 서비스, food order 0) × M8 시각 처리 하위 비호환 릴리스** (Onfido 2025-01-24, Piano 2024-03-20).
2. food orders, order_items 표(DB 테이블(주문) 0) × M21/P 취소된 백필이 살아 있는 표를 보류 이름으로 치움 (GitHub 2026-07-24): 버리지 않고 뒤로. 같은 원본과 같은 수단(db.ddl hold)을 이번 실행 F48-R 이 막 썼고 표만 바뀌는 R 이라, 새 정답 위치이긴 하지만 1번이 food order 0 과 관계 H 를 함께 채워 앞선다.
3. food dispatch(배달 서비스 3) × M11 liveness 오설정 재시작 루프 (Google SRE 책 22장, AWS 2025-10-20 헬스체크로 용량 제거): 뒤로. F05-H(commerce payment liveness) 와 같은 주입(k8s.probe)의 서비스 교체에 가깝고, 정답 위치 배달 서비스가 이미 3.
4. food payment(결제 경로 17.5%) × M8 시각 처리 릴리스가 외부 결제 응답을 못 읽음 (Piano): 뒤로. 결제 경로가 11/57(19.3%)로 상한에 붙는다.
5. commerce gateway(게이트웨이 0, commerce 최다라 0 부품만 허용) × M8 시각 처리 릴리스 (Piano): 버림(F34-R 교훈). 게이트웨이가 본문을 해석하는 곳은 AuthGuard 의 토큰 확인뿐이라 결과가 쓰기 401 단일 신호다(막힌 목록의 Jackson 엄격 역직렬화 행과 같은 벽).
6. food Kafka(메시지 브로커 0) × M10 리밸런스, 보존 축소: 버림(원칙 7). 세 도메인 모두 Kafka 는 outbox 릴레이 뒤 비동기라 사용자 증상이 없다(GitHub 2026-06-25 반려와 같은 벽).
7. food notify(알림 서비스 0) × M9 독 메시지, M2 소비자 릴리스: 버림(원칙 7). 사용자 경로 밖 비동기 소비자(앞선 소비자 반려와 같은 벽).
8. banking ledger(은행 원장 1) × M9 독 메시지: 버림(원칙 7). 원장 소비는 비동기라 사용자 5xx 가 없다.
9. food MySQL(DB 인스턴스 2) × M17 전역 time_zone 변경으로 배차 만료가 어긋남: 버림(원칙 1, 반려 기록 19행과 같은 원본 부재).
10. food order × 결함 릴리스가 배차를 중복 요청해 배달원 용량 소진(F32-R 과 H): 버림(원칙 1). 배포가 들여온 비멱등 중복 요청으로 고정 용량이 찬 공식 사후 보고를 찾지 못함(웹 검색 1회: GitLab 클라이언트 MR, Beam 이슈 등 이슈 추적기뿐).
