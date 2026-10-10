---
title: F49-P 설계 시트 (food payment-service 를 결제 응답의 id 형식을 바꾼 릴리스로 롤아웃해 order 가 결제 응답을 못 읽음)
status: Draft
owner: project
last_reviewed: 2026-10-10
tags:
  - scenario
  - design
  - release
summary: food payment-service 를 결함 있는 새 릴리스(food-delivery-payment:1.3.0, fault-images/f49-p 패치로 만든 별도 태그)로 롤아웃하면, 결제 공개 API 정비로 응답의 id 가 숫자 행 번호에서 외부 결제 키 문자열(pay_ + 8자리)로 바뀌어, payment 는 결제를 끝내고 200 으로 답하는데 응답을 id Long 으로 읽는 order 가 해석에 실패해 모든 새 주문이 배차 뒤 결제 단계에서 502 로 롤백되고 승인된 결제만 남는 시나리오. 원본은 GitHub 2021-10-08 공개 API 출시 중 핵심 API 응답 구조 변경이 기존 클라이언트를 깨뜨린 장애.
---

# F49-P 설계 시트

## 1. 요약

food payment-service 의 새 릴리스 1.3.0 은 결제 공개 API 를 정비한다. 고객센터와 정산 화면이 같은 키로 결제를 찾도록, `POST /api/payments` 와 `GET /api/payments` 가 내부 행 번호(`"id": 20061`) 대신 외부에 보여 줄 결제 키(`"id": "pay_00020061"`)를 내려준다. 결제 처리 자체(결제 행 저장, 외부 PG 승인, APPROVED 저장, 'Processed payment' 로그)와 health, DB 는 그대로라 새 파드가 Ready 가 되어 기본 롤링 전략으로 옛 파드를 대신하고, 결제마다 200 으로 답한다.

order-service 는 바뀌지 않았다. 주문 생성은 가게 조회, 메뉴 확인, 배달원 용량 확인을 지나 주문 행을 넣고(`Created order id=...`), 배차를 요청하고, 마지막 동기 단계로 결제를 부른다. order 는 결제 응답을 공용 DTO `PaymentResponse`(id 는 Long)로 읽는데, Jackson 이 `"pay_00020061"` 을 Long 으로 바꾸지 못해 RestClient 가 `Error while extracting response for type [com.fooddelivery.common.dto.PaymentResponse] and content type [application/json]` 을 던진다. PaymentClient 는 `Failed to process payment for order <id>: ...` 를 남기고 502 로 바꾼다. 결제 재시도(2회, 300ms)는 포기하기 전에 **같은 주문을 한 번 더 결제시키고**, payment 서킷브레이커(10건 창 50%, 5초 열림, 반열림 3건)는 열림과 반열림을 되풀이한다. 그래서 주문은 두 갈래로 끝난다.

- 서킷이 닫히거나 반열림일 때 들어온 주문: payment 가 결제를 끝내고 200(대부분 두 번), order 는 해석 실패로 502, 주문 트랜잭션 롤백. **승인된 결제는 주문 없이 남는다.**
- 서킷이 열려 있을 때 들어온 주문(다수): 결제 단계에서 fallback 이 `Payment service unavailable: CircuitBreaker 'payment' is OPEN ...` 502 를 던지고 롤백. payment 는 불리지 않는다.

어느 쪽이든 주문 행을 넣은 뒤라 'Created order' 와 dispatch 의 배차 기록이 남고, 모든 새 주문이 약 0.35초 만에 502 다(로컬 실측 240/240).

비유: 계산대(payment)가 영수증 번호를 "20061" 에서 "pay_00020061" 로 바꿔 찍기 시작했다. 주문 접수처(order)는 영수증 번호 칸에 숫자만 받는 장부를 쓰고 있어서, 계산대가 돈을 이미 받고 영수증을 내줬는데도 그 영수증을 못 받아 적고 주문을 무른다. 손님은 돈이 빠져나갔는데 주문은 없다. 접수처는 예전과 똑같다. 고칠 곳은 영수증 형식을 바꾼 계산대의 새 릴리스다.

## 2. 원본 사례

- **GitHub, 2021-10-08** (공식 월간 가용성 보고, 2021년 10월): https://github.blog/2021-11-03-github-availability-report-october-2021/
- 17:16 UTC 부터 1시간 36분. 공개 API 출시 과정에서 Codespaces 의 핵심 API 응답 하나가 의도치 않게 구조가 바뀌었고("inadvertently restructured"), 안정된 스키마("stable schema")에 기대던 기존 API 클라이언트가 깨졌다. VS Code 데스크톱 클라이언트에서 새 Codespace 를 시작할 수 없었고, 웹 편집기와 기존 데스크톱 세션은 저하됐다. 모니터링이 처음에는 영향을 잡지 못했고, 회귀를 되돌려 모든 클라이언트가 다시 연결됐다. 재발 방지로 확장의 API 사용에 대한 종단 간 시험 도구와 내부 서비스 경계의 모니터링을 늘린다고 했다.
- 자료 문서 `ref-real-world-incidents.md` M8 표에 이미 있다(F49-R 이 더함). 새로 더한 사실은 없다.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 공개 API 출시를 위한 Codespaces 서비스 변경 배포 | payment-service 를 릴리스 food-delivery-payment:1.3.0(결제 공개 API 정비)으로 롤아웃(KCM ScalingReplicaSet, 새 파드 Pulled 이벤트에 태그가 남음) |
| 원인이 된 결함 | 핵심 API 응답 구조가 의도치 않게 바뀜(기존 클라이언트가 기대던 안정 스키마를 깸) | 결제 응답의 id 가 숫자에서 결제 키 문자열로 바뀜(호출자가 기대던 `PaymentResponse.id: Long` 을 깸) |
| 전파 경로 | 응답을 읽는 기존 클라이언트(VS Code 확장)가 해석 실패 → 새 Codespace 시작 불가 | 응답을 읽는 order 의 PaymentClient 가 해석 실패 → 재시도(같은 주문 재결제), 서킷 → 주문 생성 마지막 단계에서 502, 롤백 |
| 사용자 증상 | 데스크톱에서 새 Codespace 시작 불가, 기존 세션과 웹 편집기 저하. 서비스 자체는 응답함 | 모든 새 주문이 502, 일부는 결제만 승인된 채 남음. 가게 둘러보기, 메뉴, 배달 조회는 정상이고 payment 는 200 으로 답함 |
| 원본의 탐지 경로 | 모니터링이 처음엔 못 잡음(클라이언트 쪽 실패), 내부 서비스 경계 모니터링을 늘리기로 함 | lucida-next 의 order 오류율, order ERROR 로그 급증. payment 서버 스팬은 200 이라 서비스 경계(호출자 쪽)에서만 보인다 |
| 완화와 복구 | 회귀 되돌림 | cleanup 이 payment 이미지를 매니페스트 이미지로 되돌림(롤백) |

기전 동일성: 원본과 재구성 모두 "API 제공자의 배포가 응답 구조를 바꿨고, 제공자는 정상 응답하는데 그 구조에 기대던 기존 클라이언트가 해석에 실패해 기능이 멈춘다, 되돌리면 낫는다" 는 고리다. 대상(Codespaces API → 결제 API)과 클라이언트(VS Code 확장 → order-service)만 우리 스택에 맞게 바꿨다. 결함은 원본처럼 한 응답, 한 필드 꼴로 작게 두었다(패치 두 파일). 결제가 이미 끝난 뒤 클라이언트가 실패하므로 승인된 결제가 남는 것은 재구성이 만든 결함이 아니라 order 의 기존 구조(결제를 마지막에 부르고, 결제 재시도 2회)가 드러내는 피해다(원칙 5: 정답이 아니라 user_impact 에 적는다).

## 3. 숫자 근거 (2026-10-10, `scenario-stats.py`, 정식 43 + 후보 26 = 69 → 70)

- 서비스(정식 + 후보): commerce 26, core-banking 22, **food-delivery 21(가장 적음)** → 22
- 묶음: 최다 G, J, P 각 8(11.6%). **L 6** → 7(70 중 10%), J 로 세도 8→9(12.9%). N 0, I, M, O 각 1, E, H, K 각 2. 20% 상한(14) 아래
- 정답 위치: **결제 서비스 3** → 4(5.7%). 부품 지도 food payment 2(결제 서비스 F30-R, DB 테이블(결제) F53-P), commerce payment 3. 최다는 주문 서비스 7(10%)
- 결제 경로 합계 11(15.9%) → **12/70(17.1%)**. 20% 미만
- 왜 이 후보인가: 가장 적은 서비스(음식배달)에서 사용자 경로에 있는 부품 가운데 정답 위치가 적은 쪽이 payment 다(food order, restaurant, dispatch 는 이번 실행에서 각각 정답 후보가 생겼고 kafka, notify 는 비동기라 원칙 7 벽). food payment 는 그동안 결제 경로 상한(18~20%)에 막혀 F49-H 시트의 후보 4번처럼 뒤로 밀렸는데, 결제 경로가 15.9% 로 내려가 풀렸다. 새 버전 배포(J)는 현실에서 가장 흔한 계기(Google 37%)다. 겉 증상(주문이 배차 뒤 결제 단계에서 502)은 F53-P(결제 표 삭제)와 같고 정답이 다른 관계 H 라 관제 AI 가 "payment 가 정말 실패했나" 를 추론해야 맞힌다. 가르는 관측 근거는 §8.

### id 를 R 이 아니라 P 로 둔 이유

F49-R 과 원본, 꼴(피호출자 릴리스가 응답 계약을 깸)이 같아 R 에 가깝지만, 깨지는 필드와 형식(status 문자열 → 객체 대 id 숫자 → 문자열), 실패 단계(가게 조회에서 아무것도 쓰지 않음 대 배차, 결제 뒤 롤백), 남는 피해(없음 대 승인 결제와 배차 잔류), 정답 위치(가게 서비스 대 결제 서비스)가 달라 "같은 원인, 다른 대상" 의 단순 대상 교체가 아니다. 그래서 일부만 비슷한 P 로 둔다. testbed-payment 에 k8s.image 를 쓰는 시나리오는 F49-P 뿐이다.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 계기: `rca-testbed-food` Deployment `testbed-payment` 이미지 `food-delivery-payment:latest` → `food-delivery-payment:1.3.0`(`food-delivery/k8s/23-payment-deploy.yaml:27-28`, imagePullPolicy Never, nodeSelector tb-w3 `:18-19`, replicas 1 `:9`, readiness/liveness `/actuator/health` `:73-86`, 전략 RollingUpdate maxSurge 25%(109 kubectl 2026-10-10)).
2. 결함(릴리스 1.3.0): `scripts/scenarios/fault-images/f49-p/payment-service.patch:17-23`(PaymentController.processPayment 가 PaymentView 를 돌려줌), `:27-39`(목록도 PaymentView), `:60-77`(PaymentView, id 가 String, `of()` 가 `String.format("pay_%08d", id)`). 매니페스트 버전은 `food-delivery/payment-service/src/main/java/com/fooddelivery/payment/controller/PaymentController.java:26-29` 에서 공용 `PaymentResponse` 를 그대로 돌려준다.
3. 결제 처리(그대로): `food-delivery/payment-service/src/main/java/com/fooddelivery/payment/service/PaymentService.java:51-89`(PENDING 저장 `:66`, 외부 PG 승인 `:70`, APPROVED 저장 `:82`, 'Processed payment' 로그 `:85`). 응답 변환은 컨트롤러에서만 일어나므로 결제는 끝난 뒤다.
4. 계약: `food-delivery/shop-common/src/main/java/com/fooddelivery/common/dto/PaymentResponse.java:6-13`(id 는 Long).
5. 호출자: `food-delivery/order-service/src/main/java/com/fooddelivery/order/service/OrderService.java:57`(@Transactional createOrder) `:128`(주문 행 저장) `:141`('Created order') `:145`(배차 호출) `:154-160`(결제 호출, 실패 시 'Order fan-out payment failed' 를 남기고 다시 던져 롤백) → `order/client/PaymentClient.java:30-47`(`body(PaymentResponse.class)` 해석 실패 → RestClientException → `log.error("Failed to process payment for order {}: {}")` → ServiceException 502), fallback `:52-57`(서킷 열림 시 'Payment service unavailable'). 재시도 2회 300ms 지수, 서킷 10건 창 50% 5초 열림(`order-service/src/main/resources/application.yml:59-62, 87-95, 112-118`). 이 끝점의 다른 호출자는 없다(저장소 grep: `/api/payments` 를 부르는 곳은 order PaymentClient 뿐, loadgen 도 부르지 않음).
6. 부작용: (a) 승인된 결제가 주문 없이 남는다(로컬 실측 주문 번호 43개에 APPROVED 65건, 22개는 두 번). (b) 배차가 결제 앞이라 실패한 주문마다 ASSIGNED 배차가 남는다(로컬 실측 주문 240건에 ASSIGNED 240건 증가). 배차는 ETA 15~35분(`food-delivery/dispatch-service/src/main/java/com/fooddelivery/dispatch/service/DispatchService.java:69`) 뒤 30초 주기 만료 배치(`:119`)가 넘긴다. **기준선 부하만 쓰면** 실패한 주문의 배차는 평소 성공한 주문이 남기던 만큼과 같은 속도라 ASSIGNED 수준이 그대로다. 그러나 주문 동반 부하를 더하면 그만큼이 ETA 동안 쌓인다: 109 실조회(2026-10-10 11:16 UTC, `/api/deliveries/capacity`) currentAssigned 1475, 한도 2000, 남은 525 에서 동반 부하가 주문을 초당 약 1.2건 더하면(F36-R 녹화 실측) 약 7분 만에 한도가 차고, 그 뒤 주문은 결제 단계 502 가 아니라 용량 확인 503 'No courier available' 로 거절되어 F32-R 꼴로 섞인다. cleanup 뒤에도 ASSIGNED 가 한도 근처에 남아 recovery 와 다음 케이스 기준선을 해친다. 그래서 동반 부하를 두지 않는다(F53-P, F48-P 와 같은 이유, §9). (c) 주문 없는 승인 결제는 오류나 지표를 만들지 않고, 다음 1시간 주기 정산 배치 로그('Settlement batch finished: count=N, total=N')의 count, total 만 조금 키운다. PG 는 클러스터 안 mockserver 라 실제 돈은 움직이지 않는다.

## 5. 원인 규정 (원칙 6)

- `root_cause.target_id`: `food-delivery-payment`(target_kind container). 결함을 가진 곳이자 되돌려야 재발이 막히는 곳은 응답 계약을 깬 payment 새 릴리스다. order 의 요청 A(결제)는 정당했고, 그것을 받은 B(payment 1.3.0)의 응답이 계약을 어겼으므로 원칙 6 의 "B 의 로직이 잘못" 경우다. 원본 포스트모템도 응답을 바꾼 API 쪽 회귀를 되돌렸다(같은 층위). 결함 이미지 규칙 7(근본은 그 서비스의 새 버전)과 맞는다.
- `trigger_target_id`: 없음(계기인 롤아웃과 근본이 같은 곳).
- `scoring.partial`: `food-delivery-order`, `food-order`, `testbed-order`(증상을 낸 곳, 해석 실패 로그가 나는 곳).
- 코드 줄을 맞히라고 요구하지 않는다(원칙 5). "payment 롤아웃 직후 order 가 payment 의 200 응답을 해석하지 못함, payment 는 결제를 계속 끝냄 → payment 릴리스" 는 관제 데이터(KCM 이벤트, order 로그 문장, payment 로그와 스팬, order 무변경)로 낼 수 있는 결론이다. 이중 결제는 피해 설명이지 정답 요구가 아니다.

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 | 기대 |
|---|---|---|
| 감지(lucida-next) | order 서버 스팬 오류율(POST /api/orders 502), order ERROR 로그 급증(PaymentResponse 해석 실패 문장, 평시 0) | 별도 규칙 없이 이벤트, 인시던트 생성. 같은 꼴(order 502 + order 오류 로그)의 F30-R(4xx 인데도), F38-R 이 정식 녹화됐다 |
| 피해 판정(러너) | 기준선(loadgen-food) k6 문서의 주문 생성 5xx 비율 ≥ 0.8, 2xx 비율 < 0.2 (3틱) | 평시 5xx 0 근처(배차 한도 503 이 드물게), 장애 시 1.0 |
| 원인 설명(RCA) | KCM 의 testbed-payment 롤아웃과 이미지 태그, order 로그 문장(PaymentResponse, application/json), 같은 주문 번호의 payment 'Processed payment ... APPROVED', payment 서버 스팬과 payment→PG 스팬 200, order 무변경 | §7 |

## 7. 관측 근거 (119 실조회, 평시)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 핵심 증거는 KCM 이벤트, 자원 이력, 전수 로그다.

| 증거 | 119 표와 칸 | 조회 | 결과(2026-10-10) |
|---|---|---|---|
| 계기: 롤아웃과 이미지 태그 | CH `lucida.kcm_events_local`(timestamp, reason, object_name, body) | `namespace='rca-testbed-food' AND object_name LIKE 'testbed-payment%' AND reason IN ('ScalingReplicaSet','Pulled','Killing','SuccessfulCreate')` 30일 | 2026-10-01, 2026-10-08 롤아웃이 'Scaled up replica set testbed-payment-5d6dff7df6 to 1', 'Pulled: Container image "food-delivery-payment:latest" already present on machine'(14건), Killing, SuccessfulCreate 로 수집됨(태그가 본문에 담김) |
| 계기: ReplicaSet, 파드 스펙의 이미지 | PG lucida `kcm_resources_history`(kind, name, yaml, captured_at) | `namespace='rca-testbed-food' AND name LIKE 'testbed-payment%'` 종류별 수와 `position('food-delivery-payment:latest' in yaml)>0` 수 | replicaset 137건, pod 130건 전부 :latest(마지막 2026-10-08 18:53). 장애 때는 1.3.0 ReplicaSet 이 남아야 한다 |
| 전파: order 해석 실패 로그 | CH `lucida.lucida_logs_local`(service_name, severity_text, body) | food-delivery-order, `body LIKE '%PaymentResponse%'` 30일 | **0건**. 평시 order 의 결제 관련 로그는 F30-R 실행 구간의 'Failed to process payment for order N: 400 ... orderId required' 748건과 'Order fan-out payment failed ... BAD_REQUEST' 748건뿐(7일) |
| 감별: payment 는 결제를 끝냄 | CH `lucida_logs_local` food-delivery-payment | 'Processed payment id=N, order=N, amount=N, status=APPROVED' 시간당 수 | 평시 시간당 4,078~4,145건(2026-10-10 05~09시, 전수, 주문 번호가 본문에 있음). 7일 405,151건. 장애 때는 order 가 실패라고 남긴 주문 번호로도 나온다(대부분 두 번) |
| 전파: 스팬 | CH `otel_traces_local`(service_name, span_kind, span_name, span_attributes['server.address'], ['http.response.status_code']) | 7일 order 서버 POST /api/orders, order CLIENT(server.address=testbed-payment), payment 서버 POST /api/payments, payment CLIENT(testbed-external-pg-mock) | order 서버 200 18,189, 400 886, 502 190, 500 170, 503 170. order→payment 클라이언트 200 18,189, 400 78. payment 서버 200 18,230, 400 78, 5xx 0. payment→PG 200 18,190. 장애 때는 order 서버 502 가 몰리고 payment 서버, payment→PG 는 200 이어야 한다 |

로컬 실측(2026-10-10, 104, mysql:8.0 + food init.sql, origin/main 456e966 의 order, restaurant, dispatch jar, 패치 전후 payment jar, 외부 PG 대신 200 {"status":"APPROVED"} 를 주는 작은 HTTP 서버, Kafka 없음(outbox 릴레이 끔), OTel 에이전트 없음):
- 매니페스트 payment: 주문 2rps 30초 60건 모두 200(p50 0.11초), PG 호출 60건.
- 릴리스 payment(1.3.0): 주문 2rps 120초 240건 **모두 502**, p50 0.35초, p95 0.40초, 최대 0.74초. 응답 본문 208건 'Payment service unavailable: CircuitBreaker 'payment' is OPEN ...', 32건 'Payment service unavailable: Payment service failed: Error while extracting response ...'. order 로그 'Created order' 240줄 = 'Order fan-out payment failed' 240줄, 'Failed to process payment for order N: Error while extracting response for type [com.fooddelivery.common.dto.PaymentResponse] and content type [application/json]' 65줄(주문 번호 43개). payment 'Processed payment ... APPROVED' 65줄(같은 주문 번호 43개, 22개는 두 번), payment ERROR 0줄, PG 호출 65건. orders 최대 id 21060 → 21060(커밋 0), dispatches ASSIGNED 60 → 300(주문 시도마다 1건). order, payment health 실행 전후 UP.
- 릴리스 jar 의 payment-service 기존 단위 시험(PaymentRetentionTest 9건)은 통과한다.

## 8. 감별

- must_support: 정답지 `must_support` 4개(롤아웃 이벤트와 태그, order 해석 실패 로그, 같은 주문 번호의 payment 결제 완료 로그와 payment 200 대 order 502, payment·MySQL·order 의 무고장과 무변경).
- must_rule_out: F53-P(결제 표), F06-P, F19-P, F19-S(외부 결제), F30-R(결제 요청 계약), F49-R, F36-R, F44-R, F32-R, F32-H, F33-H, F49-H(가게, 배달 쪽), order 자체 결함이나 설정 배포.
- contrast_with: F53-P, F49-R, F30-R, F06-P/F19-P, F49-H.
- **같은 증상, 다른 정답(관계 H)을 가르는 관측 근거**:
  - F53-P 와: 둘 다 "주문이 가게, 용량, 배차를 지나 결제 단계에서 전량 502" 다. (1) payment 의 상태: F53-P 는 payment 가 'Table 'fooddelivery.payments' doesn't exist' 1146 으로 500 을 내고 결제를 끝내지 못함, F49-P 는 payment 가 'Processed payment ... APPROVED' 를 남기고 200 (2) order 로그 문장: F53-P 는 'Failed to process payment ... 500 ...', F49-P 는 '... Error while extracting response for type [...PaymentResponse]' (3) 계기: F53-P 는 KCM 무변화와 DPM 표 수 감소, F49-P 는 testbed-payment 롤아웃과 1.3.0 태그.
  - F49-R 과: 둘 다 피호출자 릴리스가 응답 계약을 깬 해석 실패다. 롤아웃 대상(restaurant 대 payment), 로그 문장(RestaurantResponse 대 PaymentResponse), 실패 단계(가게 조회에서 아무것도 쓰지 않음 대 배차, 결제 뒤 롤백)로 가른다.
  - F30-R 과: 같은 payment 의 JSON 계약 문제지만 F30-R 은 env 설정 배포(스팬에 롤아웃 없음, 'orderId required' 400)이고 payment 가 요청을 못 읽는다. F49-P 는 이미지 릴리스이고 payment 가 처리해 200 이다.
  - F06-P, F19-P, F19-S 와: 결제 단계 502 지만 payment 의 외부 PG 호출이 429 나 지연으로 실패하고 payment 가 'PG /pay failed' 를 남긴다. F49-P 의 payment→PG 는 200 으로 빠르다.
- 러너 배제 조건은 MySQL, payment, order 파드 준비 상태, payment 서버 스팬 5xx 다(모두 러너 허용 목록에 있음: APPROVED_K8S_TARGETS 의 testbed-payment, testbed-order, APPROVED_APM_SERVICES 의 food-delivery-payment). 러너 변경 없음.

## 9. 러너 판정 조건과 강도, 부하 계산

- 주입: k8s.image release 모드, 설계 강도 1단 고정 evaluation(`approved-fixed-f49-p`, min_hold 15m, settle 30s, timeout 20m, max_injection_duration 25m). 강도 사다리 없음, 첫 실행이 곧 녹화 실행. 강도라는 축이 없다: 릴리스가 도는 동안 결제 단계에 닿는 주문은 요청량과 무관하게 100% 실패한다.
- **동반 부하: 없음.** 실패한 주문은 이미 용량 확인과 배차를 지났으므로, 주문 동반 부하(order-surge.js 2rps, 75% 가 주문 여정, 주문 초당 약 1.2~1.4건)는 그만큼 ASSIGNED 배차를 ETA 15~35분 동안 쌓는다. 2026-10-10 11:16 UTC 실조회 남은 칸 525 가 약 7분 만에 차고(525 ÷ 1.2/초 ≈ 7.3분), 그 뒤 실패 지점이 용량 확인 503 으로 옮겨 가며 cleanup 뒤에도 한도 근처가 남는다. F36-R, F44-R, F49-R 은 배차 전에 실패해 이 문제가 없어 동반 부하를 썼던 것이고, 배차 뒤에 실패하는 F53-P, F48-P 와 같은 이유로 두지 않는다. 동반 부하를 남은 칸 안으로 낮추는 대안(예: 15분 + 식힘 동안 쌓여도 525 아래가 되는 초당 0.4건 이하)은 시간대마다 ASSIGNED 기준선이 1,475~1,700 으로 달라 안전한 값이 고정되지 않아 택하지 않았다.
- success(3틱): 기준선(loadgen-food) 주문 생성 5xx ≥ 0.8, 2xx < 0.2. 기준선은 하루 주기 1~6 iter/s 로 주문 생성이 분당 약 12~68건, 30초 창에 6~34건이다(F53-P 와 같은 관측). 고장 중 모두 502 → 5xx 1.0, 2xx 0 이라 문턱을 어느 시간대에도 넘는다.
- must_rule_out(2틱): food MySQL NotReady, payment NotReady, order NotReady, payment 서버 스팬 5xx ≥ 10%.
- abort: entry_status == 0(2틱, 기준선 문서의 주문 생성 단계). 롤아웃 동안 옛 payment 파드가 새 파드 Ready 까지 요청을 받는다. 새 payment 는 결제를 끝내고 바로 200 을 돌려주고, order 는 해석 실패를 재시도 1회(300ms) 뒤 약 0.35초(최대 0.74초)에 502 로 돌려준다. DB 연결은 그동안만 잡혀(기준선 주문 최대 약 1.1건/초 × 0.35초 ≈ 1개, 풀 15) readiness 가 빠지지 않는다(로컬 health 내내 UP). 0 은 노드나 order 파드가 죽었을 때만 나온다.
- 서킷브레이커: order 의 payment 서킷이 열리는 것이 이 장애의 전파 모양이다(손님은 502 를 그대로 받는다). 서킷이 열려도 fallback 502 라 실패율은 그대로다. restaurant, dispatch 서킷은 하류가 200 이라 열리지 않는다.
- 회복: cleanup 뒤 payment 서킷이 반열림 3회 성공으로 닫힌다. 기준선만 쓰므로 ASSIGNED 수준은 평시와 같아 recovery(기준선 주문 2xx ≥ 0.7, 10분)를 막지 않는다.
- 배포 전제: 109 docker 에 `food-delivery-payment:1.3.0` 이 있어야 preflight 가 통과한다(`bash scripts/scenarios/fault-images/build.sh f49-p`, 배포 단계에서 실행). tb-w3 이미지 파일시스템 53%(2026-10-10, 상한 80%), tb-w3 containerd 에는 food-delivery-payment:latest 만 있음. 태그 1.3.0 은 저장소의 다른 시나리오(controllers, profiles, fault-images)가 쓰지 않고 109 docker 에도 없다(food-delivery-payment 는 latest, 커밋 태그뿐).

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(GitHub 2021-10-08 공식 월간 가용성 보고)와 요소별 대응표, 기전 동일(§2)
- [x] 분류 장부 갱신(§4-1 행, §6 기록), 묶음 L+J(L 6→7, 10%), 정답 위치 결제 서비스(3→4, 5.7%), 결제 경로 12/70(17.1%), 서비스 food 21→22(가장 적음), 고른 이유(§3). 어느 축도 20% 미만
- [x] 근본 원인 위치 `file:line` 확인(패치 줄, 공용 DTO, payment 서비스와 컨트롤러, order 클라이언트와 서비스)과 인프라 지점(23-payment-deploy.yaml)(§4)
- [x] 근본 원인 흔적 119 실조회: kcm_events_local 의 payment 롤아웃과 Pulled 본문 태그, kcm_resources_history 의 payment ReplicaSet, 파드 이미지(§7)
- [x] 핵심 증거가 표본에만 있지 않음: KCM 이벤트, 자원 이력, 전수 로그(order 해석 실패 문장, payment 결제 완료 문장과 주문 번호). 스팬은 보조
- [x] 계기 흔적: 롤아웃 이벤트와 새 이미지 태그. 인공 지연 없음(응답은 빠르고 결함은 응답 필드 형식)
- [x] 정답이 관제 데이터로 낼 수 있는 결론(피호출자 롤아웃 직후 호출자 해석 실패, 피호출자는 결제를 끝내고 200, 호출자 무변경), 코드 설계 결함 추론 불필요(§5)
- [x] 정답지 세 칸이 원칙 6 대로(근본 payment 릴리스, 계기 같음, 부분 점수 order)
- [x] 감지, 피해 판정, RCA 증거 구분(§6)
- [x] 주입 도구가 시나리오 id 를 남기지 않음: 실행기 인자는 네임스페이스, Deployment, 컨테이너, 이미지 둘뿐이고, 패치가 더한 문자열에 시나리오 id, fault, bug, chaos 같은 말이 없음(테스트 `test_k8s_image_release_mode_rolls_food_payment` 가 확인)
- [x] 부하 상한과 서킷브레이커를 고려한 피해 계산(§9: 서킷이 열려도 fallback 502, 결정적 전량 실패, 로컬 240/240, 배달원 한도를 채우지 않도록 동반 부하 없음)

### 후보 목록 (이번 반복, "실제 기전 × 부품", 숫자 순)

1. **[채택] food payment(결제 서비스 3, 결제 경로 15.9%) × M8 응답 계약을 깬 릴리스** (GitHub 2021-10-08). 위 시트.
2. food payment × M8 시각, 형식 처리 릴리스가 바뀌지 않은 외부 PG 응답을 못 읽음 (Onfido 2025-01-24, Piano 2024-03-20): 버리지 않고 뒤로. 같은 부품, 같은 묶음이고 호출자 쪽 변경 꼴은 F49-H 가 막 썼다. 1번이 "제공자는 결제를 끝냈는데 호출자가 무름" 이라는 새 피해 꼴(승인 결제 잔류)을 더해 앞선다.
3. banking ledger(은행 원장 1) × M9 독 메시지 (PostHog 2026-07-23): 버림(원칙 1, 7). 출처가 집계 사이트(2차)이고 원장 소비는 outbox 뒤 비동기라 사용자 5xx 가 없다.
4. food notify(알림 서비스 0) × M2 소비자 새 릴리스: 버림(원칙 7). notify 는 DB 도 없는 비동기 소비자라 사용자 경로에 증상이 없다(119 로그 notify 시간당 약 4.5만 줄이지만 사용자 요청과 무관).
5. food Kafka(메시지 브로커 0) × M10 리밸런스 폭풍: 버림(원칙 1, 7). 공식 사후 보고가 없고(자료 문서 M10), 세 도메인 Kafka 는 모두 outbox 릴레이 뒤라 사용자 증상이 없다(저장소 grep: 요청 경로에 KafkaTemplate 없음).
6. banking nginx(0) × M19 레이트 리밋 오설정 (PostHog 2025-10-24): 버림(원칙 3). nginx:alpine 은 OTel 이 없고 119 lucida_logs_local 의 최근 1시간 서비스 목록에 nginx 가 없다(2026-10-10 실조회). 계기와 거절이 관제 데이터에 남지 않는다(앞선 반려와 같은 벽).
7. food payment × N 응답 코드 탓 백오프 미적용 재시도 (GitHub 2026-05-05): 버림(원칙 1, 9). 원문(2026-10-10 다시 확인)은 VM 증설이 내부 레이트 리밋에 걸린 것이 계기라 "증설 + 재시도 정책" 두 계기의 복합이고, 외부 PG mock 의 429 를 늘리려면 별도 주입이 하나 더 필요하다.
8. food dispatch / banking account × M11 liveness 오설정 재시작 루프 (Google SRE 책 22장, AWS 2025-10-20): 버림(카탈로그 §1). 웹 검색 1회에서 공식 사후 보고는 찾지 못했고(블로그, 이슈 추적기뿐), 같은 주입(k8s.probe 경로 변경)을 서비스만 바꿔 F05-H 를 복제하게 된다.
9. tb-w3 노드(노드, 디스크 6) × M15 패킷 손실 (Gunawi 네트워크 15%): 버림(원칙 4). tc netem 손실은 원인(장비 고장)을 재현하지 않는 인공 손실이다(앞선 netem 반려와 같은 벽).
10. commerce gateway(게이트웨이 0, commerce 최다라 0 부품만) × M8 응답 형식 릴리스: 버림(F34-R 교훈). 게이트웨이가 본문을 해석하는 곳은 토큰 확인뿐이라 쓰기 401 단일 신호다(앞선 반려와 같은 벽).
