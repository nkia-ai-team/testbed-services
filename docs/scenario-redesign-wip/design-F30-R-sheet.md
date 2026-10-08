---
title: F30-R 설계 시트 (food payment JSON 명명 규칙 설정 배포로 결제 요청 형식 불일치)
status: Draft
owner: project
last_reviewed: 2026-10-08
tags:
  - scenario
  - design
  - data-format
summary: 음식배달 payment-service 에 JSON 명명 규칙을 snake_case 로 바꾸는 설정이 배포되어, camelCase 로 보내는 order 의 모든 결제 요청에서 orderId 가 사라지고 주문 생성이 실패하는 시나리오. 원본은 Flagsmith 2026-03-03 Edge API 직렬화 형식 변경 장애.
---

# F30-R 설계 시트

## 1. 요약

음식배달 결제 서비스(payment)에 "JSON 필드 이름을 snake_case 로 쓴다"는 설정이 배포된다. 결제를 부르는 쪽인
주문 서비스(order)는 그대로 camelCase(`orderId`)로 보낸다. payment 는 이제 `order_id` 를 기다리므로 `orderId` 를
모르는 키로 버리고, 주문 번호가 비어 있다며 모든 결제를 400 으로 거절한다. 아무것도 죽지 않았는데 주문만 전부 실패한다.

비유: 우체국이 하룻밤 사이에 "주소는 영문으로만 받는다"로 규정을 바꿨는데, 손님들은 여전히 한글 주소를 쓴다.
창구는 멀쩡히 열려 있지만 모든 소포가 "주소 없음"으로 반송된다.

## 2. 원본 사례

| 항목 | 내용 |
|---|---|
| 기업, 날짜 | Flagsmith, 2026-03-03 |
| 출처 | [공식 상태 페이지](https://status.flagsmith.com/incidents/q3h5kr2z2sgb) (`ref-real-world-incidents.md` M8 에 추가) |
| 요약 | 17:00 UTC Edge API 배포가 정수형 기능 값의 JSON 형식을 문자열 `"300"` 에서 숫자 `300` 으로 바꿨다. 로컬 평가 모드 SDK 가 쓰는 `/api/v1/environment-document/` 가 대상이었고 주로 정적 타입 언어 클라이언트가 깨졌다. 기존 테스트가 형식 변화를 잡지 못했다. 18:12 사용자 보고, 18:36 이전 릴리스로 롤백해 복구했다 |

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | Edge API 새 릴리스 배포 | food payment-service 설정 롤아웃(`SPRING_APPLICATION_JSON` 으로 `spring.jackson.property-naming-strategy=SNAKE_CASE`) |
| 원인이 된 결함 | 배포가 API 의 JSON 직렬화 형식을 바꿨고, 기존 클라이언트가 기대하는 형식과 어긋났다 | 배포가 payment 의 JSON 계약(필드 이름)을 바꿨고, 기존 호출자 order 가 보내는 형식과 어긋났다 |
| 전파 경로 | 바뀐 형식의 응답 → 정적 타입 SDK 해석 실패 → 고객 서비스의 기능 평가 실패 | camelCase 요청 → payment 가 `orderId=null` 로 해석 → 400 `orderId required` → order 가 4xx 전파, 주문 롤백 |
| 사용자 증상 | 기능 플래그 평가 실패(정적 타입 언어 사용자) | 주문 생성 실패(400). 조회는 정상 |
| 원본의 탐지 경로 | 사용자 보고(배포 후 72분) | 이 테스트베드에서는 order ERROR 로그 급증, 주문 처리량 붕괴, 직전 롤아웃 이벤트 |
| 완화와 복구 | 이전 안정 릴리스로 롤백 | env 를 baseline 으로 되돌리는 롤아웃(cleanup) |

**기전 동일성**: 원본과 재구성 모두 "제공하는 쪽 서비스의 배포가 서로 주고받는 JSON 계약을 깨뜨림 → 바뀌지 않은 상대(소비자, 호출자)의
기능이 실패함 → 롤백으로 복구"다. 다른 점은 둘이다. (1) 원본은 응답 형식이 어긋나 바뀌지 않은 소비자가 해석에 실패했고,
재구성은 요청 형식이 어긋나 형식을 바꾼 쪽(payment)이 기존 형식의 요청을 해석하지 못해 바뀌지 않은 호출자의 기능이 실패한다.
해석 실패 위치가 반대라 재구성 쪽이 원본보다 오류가 바뀐 쪽 가까이에서 보이고, 그만큼 원본보다 감지와 위치 찾기가 쉬운 방향이다. (2) 원본은 코드 릴리스,
재구성은 설정 배포로 형식을 바꾼다. 앱 코드를 고치지 않는다는 하네스 규칙 때문이며, 형식 변경이 배포로 들어온다는 점은 같다.

## 3. 숫자 근거 (2026-10-08, `scenario-stats.py`)

- 장부 묶음별 ready: A 8(20%), B 7(17%), C 7(17%), D 8(20%), E 2, F 2, G 2, H 2, I 1, **J K L M N 0**
- 서비스별 ready: 쇼핑몰 25, 은행 9, 음식배달 5. 차이가 3 을 넘으므로 쇼핑몰 금지, 음식배달 최우선
- draft 대기(§4-1): 없음. 막힌 시도(§5)에 L 묶음 시도 없음
- 이 후보: 묶음 **L 데이터 형식 불일치**(ready 0, 현실 근거 Liu 2019 버그의 21%), 계기는 설정 배포라 `L+G`, 서비스 **음식배달**
- 다른 빈 묶음을 고르지 않은 이유: K 는 네트워크 주입기가 껍데기(§5 F13-H, F13-P), J 는 결함 있는 이미지가 필요해 앱 코드 변경 금지에 걸리고,
  M(DNS)은 클러스터 공용 CoreDNS 를 건드리거나 새 주입기가 필요하며, N 은 같은 날 원칙 1, 4, 5 로 떨어진 후보가 있다(rejected 목록).
  L 은 기존 `k8s.env` 주입기와 기존 관측만으로 만들 수 있다

## 4. 인과 사슬 (코드와 인프라 위치)

1. 계기: k8s.env 가 `rca-testbed-food/testbed-payment` 컨테이너 env 에 `SPRING_APPLICATION_JSON` 한 줄을 더해 롤아웃
   (인프라 지점: `food-delivery/k8s/23-payment-deploy.yaml:38-66` 의 env, 실배치 값은 109 `kubectl get deploy` 로 확인한 11개 항목)
2. payment 의 Spring MVC 가 Boot ObjectMapper(이제 SNAKE_CASE)로 요청을 해석:
   `food-delivery/payment-service/src/main/java/com/fooddelivery/payment/controller/PaymentController.java:26-27 (@PostMapping, @RequestBody PaymentRequest)`
3. 계약: `food-delivery/shop-common/src/main/java/com/fooddelivery/common/dto/PaymentRequest.java:5-9 (record PaymentRequest(orderId, amount, pgProvider))`.
   SNAKE_CASE 에서 기대 키는 `order_id`, `amount`, `pg_provider`. 로컬 검증(Jackson 2.18.3, Boot 3.4.5 와 같은 계열):
   - 관대(기본, FAIL_ON_UNKNOWN_PROPERTIES 끔): `PaymentRequest[orderId=null, amount=15000, pgProvider=null]`
   - 엄격(2단): `Unrecognized field "orderId" ... (3 known properties: "amount", "pg_provider", "order_id")`
4. 거절: `food-delivery/payment-service/src/main/java/com/fooddelivery/payment/service/PaymentService.java:54-55 (orderId == null → 400 "orderId required")`.
   DB 쓰기(66)와 PG 호출(70)보다 앞이라 payment 는 DB 도 PG 도 건드리지 않는다
5. order 전파: `food-delivery/order-service/src/main/java/com/fooddelivery/order/client/PaymentClient.java:40-44 (log.error "Failed to process payment for order {}: {}", 4xx 를 ClientErrorException 으로 전파)`.
   `food-delivery/order-service/src/main/resources/application.yml:94-95` 의 CB, 117-118 의 retry 가 ClientErrorException 을 무시하므로 재시도도 회로 열림도 없다
6. 주문 실패: `food-delivery/order-service/src/main/java/com/fooddelivery/order/service/OrderService.java:154-158 (processPayment 실패 → 예외 전파, @Transactional 롤백)` → 사용자에게 400

## 5. 원인 규정 (원칙 5, 6)

| 칸 | 값 | 근거 |
|---|---|---|
| `root_cause.target_id` | `food-payment` | 형식을 바꾼 쪽. 고쳐야(되돌려야) 재발이 막히는 곳 |
| `root_cause.trigger_target_id` | 비움 | 계기(payment 설정 배포)와 근본이 같은 곳 |
| `scoring.partial` | `food-order` | 오류 로그가 쌓이는 증상 위치. 요청은 기존 계약 그대로라 정당하다(원칙 6: A 가 정당하면 B 의 로직이 원인) |

- 정답은 "payment 배포가 결제 요청 형식 해석을 깨뜨렸다"까지다. 명명 규칙 설정값 자체는 관제 데이터에 없으므로 정답에 요구하지 않는다(원칙 5)
- 원본 포스트모템도 "배포가 직렬화 형식을 바꿨다"를 원인으로 지목했다. 같은 층위다
- 결제 전에 배차를 먼저 잡는 order 순서(고아 배차)는 피해 설명(user_impact)에만 적고 정답에 넣지 않는다

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 | 내용 |
|---|---|---|
| 감지(lucida-next) | 겉 증상 | order ERROR 로그 0 → 주문당 1줄(log-anomaly 의 surge, new_template 대상), order 와 payment 처리량 변화, payment 'Processed payment' 로그 정지(absence). 119 에는 log-anomaly(surge 2,068, new_template 195, absence 42건/14일)와 trace-anomaly 가 food 서비스에서 이미 동작 중 |
| 피해 판정(러너) | k6 live 문서 | `loadgen.transfer_2xx_rate`(business_2xx_rate, food business_step=create) < 0.2 |
| RCA | 녹화 데이터 | §7 표 |

같은 주문 단계에서 4xx 로만 실패하는 F06-P 가 ready 이므로 4xx 증상으로도 인시던트가 생길 수 있다는 선례가 있다. 실제 생성 여부는 시험 실행에서 확인한다(원칙 7).

## 7. 관측 근거 (119 실조회, 2026-10-08)

| 증거 | 표, 칸 | 조회와 결과(평시) | 표본 여부 |
|---|---|---|---|
| 계기: payment 롤아웃 | CH `lucida.kcm_events_local` (`namespace`, `object_kind`, `object_name`, `reason`, `body`) | `SELECT object_kind, reason, substring(body,1,90), count(), max(timestamp) FROM lucida.kcm_events_local WHERE namespace='rca-testbed-food' AND object_name LIKE 'testbed-payment%' GROUP BY 1,2,3` → `Deployment ScalingReplicaSet "Scaled up replica set testbed-payment-7674869b85 to 1 from 0"`(2026-10-01 06:08), `ReplicaSet SuccessfulDelete`, `Pod Created/Pulled/Started`. 보존 2026-08-13~ | 전수 |
| 계기 보조: 파드 상태 변화 | CH `lucida.lucida_events_local` detector=`change-detect`, `attributes['resource_kind']='k8s_pod'` | 14일 66건(`kcm.pod.pod_status`) | 전수 |
| 전파: order 결제 실패 로그 | CH `lucida.lucida_logs_local` (`service_name`, `severity_text`, `body`, `trace_id`) | 같은 log 문(`PaymentClient.java:40`)이 다른 원인으로 남은 기록: `ERROR Failed to process payment for order 4586615: I/O error on POST request for "http://testbed-payment:8083/api/payments": Connection refused`, `... Error while extracting response for type [...PaymentResponse]`(2026-10-01). 같은 문의 실제 4xx/5xx 기록 형식은 `Failed to process payment for order 4616341: 502 : "{"status":502,...}"`(Tomcat 이 상태 문구를 보내지 않아 `502 : ` 꼴). 장애 시에는 `400 : "{"status":400,"error":"Bad Request","message":"orderId required",...}"` 꼴이 기대값이다(1단). 2단은 Boot 기본 오류 본문이라 message 가 없다 | 전수 |
| 평시 order ERROR | 위 표 | 최근 6시간 food-delivery-order ERROR 0건 | 전수 |
| 피해: payment 처리 로그 | 위 표 `body LIKE 'Processed payment%'` | 시간당 414~1,730건(최근 3시간), 장애 시 0 이 기대값 | 전수 |
| 피해: 주문 성공 비율 | 위 표, 5분 창 `Processed payment` / (`Created order` + `Order rejected`) | 최근 6시간 73개 창: 최소 0.912, p10 0.939, 중앙 0.966, 최대 1.0 | 전수 |
| 보조: payment 서버 스팬 | CH `lucida.otel_traces_local` (`span_name='POST /api/payments'`, `status_code`) | 최근 1시간 165건 UNSET. 같은 트레이스 안에 `INSERT fooddelivery.payments` 165건(직속 자식은 `PaymentRepository.save`, 그 아래 INSERT). 장애 시 400 응답이고 저장, INSERT 스팬이 없어야 한다 | **10% 표본, 보조로만** |

핵심 증거(계기 이벤트, order ERROR 로그, payment 처리 로그 정지)는 모두 전수 수집 데이터다. 트레이스와 APM 지표는 보조로만 쓴다.
payment 는 400 을 낼 때 로그를 남기지 않는다(`GlobalExceptionHandler` 가 로깅 없이 응답만 만든다). 그래서 1단의 payment 측 직접 증거는
"처리 로그가 끊김"이고, 거절 사유는 order 로그가 인용한 payment 응답 본문에 있다. 2단(엄격)이면 Spring 의
`DefaultHandlerExceptionResolver` 가 payment 에 WARN `Resolved [...HttpMessageNotReadableException: JSON parse error: Unrecognized field "orderId" ...]` 를 남긴다(시험 실행에서 확인).

## 8. 감별

- must_support: payment 롤아웃 이벤트 직후 실패 시작, order ERROR 로그 'Failed to process payment ... 400'(1단은 본문에 orderId required, 2단은 payment WARN Unrecognized field "orderId"), payment 처리 로그 정지, 주문 2xx 0.91~1.0 → 0.2 미만, payment 파드 Ready, 5xx 없음, 조회 정상
- must_rule_out: 외부 PG 429(F06-P), payment 가용성 장애(연결 거부, 5xx), order 쪽 변경(F08-P), 배차 용량 부족(503)
- contrast_with: F06-P(같은 단계 4xx 지만 PG 기원, payment 가 PG 를 호출), F08-P(설정 배포 계기지만 바뀐 쪽이 order), F19-S(PG 지연으로 502)
- 같은 주입 중복: k8s.env 를 food testbed-payment 에 쓰는 기존 시나리오 없음(기존 k8s.env 대상은 commerce, banking)

## 9. 러너 판정과 강도, 부하 계산

- **2026-10-08 승격 완료**: 첫 시험 실행을 위해 아래 ①~④대로 ready + mode calibration 으로 올렸다(controller 는 `registry/controllers.json`, WIP 사본은 삭제).
- controller 위치(승격 전 기록): `compile-plan.py:150-152` 가 controller 를 readiness=ready 시나리오에만 허용하므로, draft 동안은
  `registry/controllers.json` 에 넣지 않고 `docs/scenario-redesign-wip/design-F30-R-controller.json` 에 둔다(F24-Q 와 같은 방식).
  정답지 `injected_fault` 도 같은 이유로 null 이다. 이 저장소에서는 첫 시험 실행(calibration) 자체가 ready 승격을 먼저 요구한다(F10-P 처럼 ready + mode calibration).
  승격 때 할 일: ① WIP 블록을 controllers 와 `live_scenario_ids` 끝에 옮긴다 ② `injected_fault` 를 companions 까지 채운다(실제 적용된 단 기준) ③ 끝 순서 고정 테스트(test_f02p_and_f04r..., live matrix 등)와 test-scenarios.sh 의 ready, draft 핀을 갱신한다 ④ 장부 §4-1 에서 §4 로 옮긴다
- 사다리(calibration): 1단 `snake-case-lenient`(명명 규칙만), 2단 `snake-case-strict`(+ `fail-on-unknown-properties=true`). 1단에서
  주문 2xx 가 0.5 이상으로 3틱 유지되면(형식 불일치가 안 드러남, 예: 설정이 적용 안 됨) 2단으로 올린다. 각 단 min_hold 6분, settle 60초(롤아웃 시간)
- 피해 판정 값: 원칙 7 은 평시 0 근처에서 오르는 값을 요구하지만, 러너 queries.json 에는 food 생성 단계의 400 비율 선택자가 없고(2xx, 429, 5xx, nonok 만) 러너 변경 없이 만든다. nonok 도 평시 배차 503 으로 약 0.1 이다. 그래서 평시 0.91~1.0 에서 떨어지는 2xx 비율을 쓴다. 평시와의 간격이 커서 판정력은 있다. 보조 피해 판정은 119 로그의 5분 창 성공 비율(평시 최소 0.912)이다
- success: `order_create_2xx_rate < 0.2` 3틱. 평시 0.91~1.0(위 §7, 6시간 73창), 장애 시 기대 0(결제 단계에 닿는 주문 전부 400). 장애 시 값은 시험 실행에서 실측한다
- must_rule_out: `achieved_rps < 1`, `payment_error_rate >= 5`(payment 자체 5xx), `order_create_429_rate >= 0.1`(PG 429), `read_step_rate >= 0.1`(조회 경로), `pod_ready(payment) == false`
- recovery: target 200, payment 파드 Ready, food 기준선 2xx >= 0.7
- 부하: food surge.js target_rps 5(상한 180 의 3%). surge.js 주문 비중 10% 로 k6 주문 약 0.5/s, 기준선 loadgen 주문 약 0.5/s. F06-P 와 같은 강도로, 그 실측에서 배차 503 이 약 10% 였으므로 2xx 기준선이 무너지지 않는다
- 서킷브레이커: order 의 payment CB 와 retry 가 ClientErrorException 을 무시(`application.yml:94-95`, `117-118`)하므로 회로가 열려 502 로 바뀌지 않고 400 이 그대로 전달된다. 따라서 피해는 강도와 무관하게 결제 단계 도달 주문의 100% 다
- 롤아웃: replicas 1, maxSurge 25%(올림 1), maxUnavailable 25%(내림 0). 새 파드가 Ready 된 뒤 옛 파드가 내려가 결제 가용성 공백이 없다

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Flagsmith, 2026-03-03, 공식 상태 페이지)와 요소별 대응표, 기전 동일(§2)
- [x] 분류 장부 갱신(§4-1 에 F30-R), 묶음 L ready 0, 서비스 음식배달 5, 선택 이유(§3)
- [x] 근본 원인 위치: `PaymentController.java:26-27`, `PaymentRequest.java:5-9`, `PaymentService.java:54-55` + 인프라 `23-payment-deploy.yaml:38-66` env(§4)
- [x] 근본 원인 흔적 119 조회: `kcm_events_local` 의 testbed-payment 롤아웃, `lucida_logs_local` 의 `Processed payment`(§7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: 이벤트와 로그(전수), 트레이스는 보조(§7)
- [x] 계기의 흔적: 롤아웃 이벤트(ScalingReplicaSet, SuccessfulCreate/Delete, Created/Started), 인공 지연 없음(§7)
- [x] 정답이 관제 데이터로 낼 수 있는 결론: "payment 배포가 요청 형식 해석을 깨뜨림". 설정값 자체를 요구하지 않음(§5)
- [x] 정답지 세 칸: 근본 food-payment, 계기 비움(같은 곳), 부분 점수 food-order(§5)
- [x] 감지, 피해 판정, RCA 증거 구분(§6, §7)
- [x] 주입이 시나리오 id 를 남기지 않음: env 값은 Spring 속성 JSON 뿐, 롤아웃 이름은 Kubernetes 가 붙이는 해시. k6 의 `--tag scenario_id=F30-R` 는 기존 모든 시나리오와 같은 tb-runner 로컬 지표 태그로 HTTP 요청에 실리지 않는다(surge.js 는 `journey`, `step` 태그만 요청에 붙임)
- [x] 부하 상한과 서킷브레이커를 넣은 피해 계산(§9)
