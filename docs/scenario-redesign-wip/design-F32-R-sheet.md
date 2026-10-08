---
title: F32-R 설계 시트 (food dispatch 배차 동시 한도 설정 배포로 한도가 실제 배차 수 아래로)
status: Draft
owner: project
last_reviewed: 2026-10-08
tags:
  - scenario
  - design
  - config-deploy
summary: 음식배달 dispatch-service 에 배차 동시 한도(DISPATCH_MAX_CAPACITY)를 200 으로 거는 설정이 배포되어, 이미 배정된 배차 약 1,700 건보다 한도가 낮아지고 order 가 모든 새 주문을 503 으로 거절하는 시나리오. 원본은 Google 2020-12-14 쿼터 관리가 User ID Service 쿼터를 실제 사용량 아래로 줄인 인증 장애.
---

# F32-R 설계 시트

## 1. 요약

음식배달 배차 서비스(dispatch)에 "동시에 배정할 수 있는 배달원은 200명까지"라는 설정이 배포된다. 평시에는 늘
약 1,700건의 배차가 진행 중이라 한도가 이미 쓰고 있는 양보다 한참 낮다. dispatch 는 멀쩡히 돌면서
"남은 자리 0"이라고 정직하게 답하고, 주문 서비스(order)는 주문을 만들기 전에 그 답을 보고 모든 주문을
503 "배달원 없음"으로 거절한다. 아무것도 죽지 않았는데 새 주문만 전부 막힌다.

비유: 주차장 관리 규정이 "최대 200대"로 바뀌어 배포됐는데 이미 1,700대가 서 있다. 차단기는 정상 작동하지만
새 차는 한 대도 못 들어간다. 주차장이 붐벼서가 아니라 규정의 숫자가 틀려서다.

## 2. 원본 사례

| 항목 | 내용 |
|---|---|
| 기업, 날짜 | Google, 2020-12-14 (Google Cloud Infrastructure Components Incident #20013) |
| 출처 | [공식 사후 보고](https://status.cloud.google.com/incident/zall/20013) (`ref-real-world-incidents.md` M1 에 추가, 2026-10-08 WebFetch 로 원문 확인) |
| 요약 | 새 쿼터 시스템 이전 중 남아 있던 옛 시스템이 User ID Service 의 사용량을 0으로 잘못 보고했고("incorrectly reported the usage for the User ID Service as 0"), 쿼터 적용 유예 기간이 끝나자 자동 쿼터 관리가 이 서비스의 쿼터를 줄였다. "As a result, the quota for the account database was reduced, which prevented the Paxos leader from writing." 인증 조회 오류로 인증이 필요한 Google Cloud, Workspace API 전반에 5xx. 03:43 용량 경보, 03:46 서비스 오류 경보, 04:08 원인 식별, "disabling the quota enforcement in one datacenter at 04:22", 04:27 전 데이터센터 적용, 04:33 오류율 정상. 재발 방지 첫 항목은 쿼터 관리 자동화가 전역 변경을 빠르게 적용하지 못하게 검토, 둘째는 잘못된 설정을 더 빨리 잡는 감시 |

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 쿼터 관리 자동화가 User ID Service 의 쿼터(한도)를 낮추는 변경을 적용 | food dispatch-service 설정 롤아웃(`DISPATCH_MAX_CAPACITY=200`, 기본 2000) |
| 원인이 된 결함 | 새 한도가 서비스의 실제 사용량보다 낮았다(사용량을 0으로 잘못 본 값으로 계산) | 새 배차 동시 한도 200 이 평시 실제 배차 수(ASSIGNED 약 1,700)보다 낮다 |
| 전파 경로 | 한도 집행 → 계정 DB 쓰기 불가(Paxos 리더) → 인증 조회 오류 → 인증에 의존하는 API 5xx | 한도 집행(dispatch 용량 응답 available=0) → order 의 용량 확인이 주문 거절 → 주문 생성 503 |
| 사용자 증상 | 인증이 필요한 요청 실패(5xx), 로그인 불가 | 주문 생성 실패(503). 식당 조회, 메뉴, 배달 추적은 정상 |
| 원본의 탐지 경로 | 자동 용량 경보(03:43), 서비스 오류 경보(03:46) | 이 테스트베드에서는 order 5xx, order 'Order rejected: courier pool exhausted' 로그 급증, 주문과 배차 로그 정지, 직전 dispatch 롤아웃 이벤트 |
| 완화와 복구 | 쿼터 적용을 끔(한도 집행 해제) | env 를 baseline 으로 되돌리는 롤아웃(cleanup)으로 한도 2000 복원 |

**기전 동일성**: 원본과 재구성 모두 "한도(쿼터) 설정이 실제 사용량 아래로 바뀜 → 서비스는 살아 있지만 그 한도를 집행해
정상 요청을 거절함 → 그 서비스에 기대는 기능이 실패함 → 한도를 풀거나 되돌려 복구"다. 수요는 늘지 않았고 부족한 물리 자원도 없다.
다른 점은 셋이다. (1) 원본은 자동화가 잘못 계산된 사용량(0)으로 한도를 줄였고, 재구성은 잘못된 한도 값이 설정 배포로 들어온다.
둘 다 "한도 설정의 변경"이 계기이고, 원본 재발 방지도 "전역 변경을 빠르게 적용하지 못하게", "잘못된 설정을 더 빨리 잡게"를 꼽았다.
앱 코드를 고치지 않는다는 하네스 규칙 때문에 사용량 오보고 단계는 재현하지 않는다. (2) 원본의 한도는 저장 용량이라 쓰기가 막혔고,
재구성의 한도는 동시 배차 수라 신규 배정 요청이 막힌다. 한도 아래로 내려간 사용량이 정상 요청을 거절하게 만드는 고리는 같다. 원본의 중간 단계(쓰기 차단 → 읽기 대부분이 낡은 데이터가 됨 → 인증 조회 거절)는 재구성에서 생략되고 한도 집행이 곧바로 거절로 이어진다. 원본 계기의 "자동화가 사용량을 0으로 잘못 봄" 단계도 (1)대로 재현하지 않는다.
(3) 원본은 한도를 가진 서비스(User ID Service)가 스스로 실패했고, 재구성은 한도를 가진 dispatch 가 "남은 자리 0"을 알려 주면
호출자 order 가 거절한다. 거절 판단의 근거가 한도를 가진 쪽의 값이라는 점은 같고, 그 값(max=200)이 order 로그에 그대로 찍힌다.

## 3. 숫자 근거 (2026-10-08, `scenario-stats.py`, 정식 + 후보 합계)

- 장부 묶음별: A 7(19%), B 7(19%), C 6(16%), D 7(19%), E 2, **G 2**, H 2, F 1, I 1, L 1(F30-R 후보), J K M N 0. 합계 36
- 정답 위치별: 외부 결제 의존 6, 주문 서비스 6, 결제 경로 합계 10(27%, 결제 쪽 금지), **배달 서비스 0**
- 서비스별: 쇼핑몰 23, 은행 8, 음식배달 5. 최다와 최소 차이가 3 을 넘어 쇼핑몰 금지, 음식배달 최우선
- 이 후보: 묶음 **G 설정 오배포**(현실 근거 Google SRE Workbook 트리거 31% 로 두 번째로 흔한데 우리는 2), 정답 위치 **배달 서비스**(0),
  서비스 **음식배달**. 추가 뒤 G 3/37(8%), 배달 서비스 1/37(3%), 음식배달 6. 어느 축도 20%에 닿지 않는다
- 묶음 판단: "한도에 걸림"이라 A(자원 부족, 한도)로 볼 수도 있지만 A 는 CPU, 메모리, 디스크 IO, 디스크 용량 같은 물리 자원이다.
  여기서는 dispatch 의 CPU, 메모리, DB 모두 그대로이고 바뀐 것은 배포된 한도 값 하나라 G 다. A 는 19%라 어차피 넣지 않는다
- 다른 빈 묶음을 고르지 않은 이유: K 는 네트워크 주입기가 껍데기(§5 F13-H, F13-P), J 는 결함 이미지가 필요해 앱 코드 변경 금지에 걸리고,
  M(DNS)은 같은 날 Service 삭제 후보가 폐기됐으며(같은 원본, 같은 주입 금지) 다른 DNS 주입은 클러스터 공용 CoreDNS 를 건드린다.
  N 은 재시도 증폭을 180rps 안에서 일으킬 지점이 확인되지 않았다(§5 F14-R, F24-Q 실측 반증)
- 기존 시나리오와의 거리: 장부 §4, §4-1 에 dispatch 가 정답인 시나리오가 없다. 업무 한도 거절로는 F23-R(재고 고갈 409)이 있으나
  그쪽은 사용량이 한도까지 차서 막히고, 이쪽은 사용량은 그대로인데 한도 값이 내려간다(§8 감별쌍)
- **번호**: `scenario-stats.py` 는 F31 을 알려 주지만, F31-R 은 같은 날 적대적 평가에서 폐기된 다른 후보(food restaurant Service 삭제)가
  쓴 번호이고 그 변경분이 되살릴 후보로 보관돼 있다. 되살릴 때 번호가 겹치지 않게 F32 를 쓴다. 새 사례군이라 접미사는 R

## 4. 인과 사슬 (코드와 인프라 위치)

1. 계기: k8s.env 가 `rca-testbed-food/testbed-dispatch` 컨테이너 env 에 `DISPATCH_MAX_CAPACITY=200` 한 줄을 더해 롤아웃
   (인프라 지점: `food-delivery/k8s/22-dispatch-deploy.yaml:38-66` 의 env. 실배치 값은 109 `kubectl -n rca-testbed-food get deploy testbed-dispatch -o json` 으로
   확인한 11개 항목이고 DISPATCH_MAX_CAPACITY 는 없다. replicas 1, maxSurge 25%, maxUnavailable 25%, progressDeadlineSeconds 600)
2. 한도 값: `food-delivery/dispatch-service/src/main/resources/application.yml:78-83 (dispatch.max-capacity: ${DISPATCH_MAX_CAPACITY:2000})`,
   `food-delivery/dispatch-service/src/main/java/com/fooddelivery/dispatch/service/DispatchService.java:39-48 (생성자, @Value("${dispatch.max-capacity:50}") int maxCapacity)`
3. 한도 집행: `DispatchService.java:105-114 (getCapacity: available = max(0, maxCapacity - countByStatus("ASSIGNED")))`,
   `food-delivery/dispatch-service/src/main/java/com/fooddelivery/dispatch/controller/DispatchController.java:59-66 (GET /api/deliveries/capacity)`.
   dispatch 자신은 이 값으로 아무 로그도 남기지 않고 200 으로 답한다
4. 거절: `food-delivery/order-service/src/main/java/com/fooddelivery/order/client/DispatchClient.java:31-45 (checkCapacity)` 가 응답을 그대로 돌려주고(200 이라 retry, CB 무관),
   `food-delivery/order-service/src/main/java/com/fooddelivery/order/service/OrderService.java:94-105 (createOrder 도메인 검증 3)` 이 `available <= 0` 이면
   `log.info("Order rejected: courier pool exhausted (assigned={}, max={})")` 후 `ServiceException(503, "No courier available (capacity full)")`.
   주문 저장(115-128), 배차 요청(144-150), 결제(153-159)보다 앞이다
5. 응답: `food-delivery/order-service/src/main/java/com/fooddelivery/order/config/GlobalExceptionHandler.java:12-19 (handleServiceException)` 가 로그 없이 503 응답
6. 회복 쪽: `DispatchService.java:119-130 (deliverExpiredDispatches, 30초마다)` 는 계속 돌아 ASSIGNED 를 줄인다. 새 배정이 없으니 ASSIGNED 는 줄기만 한다.
   `DispatchService.java:60-64 (dispatchCourier 의 같은 한도 검사)` 는 order 가 먼저 거절하므로 이 장애에서는 닿지 않는다

## 5. 원인 규정 (원칙 5, 6)

| 칸 | 값 | 근거 |
|---|---|---|
| `root_cause.target_kind` | `config` | 결함은 dispatch 의 배포된 설정값이다(F08-P, F18-P 와 같은 표기) |
| `root_cause.target_id` | `food-delivery-dispatch` | 잘못된 한도를 가진 곳, 되돌려야 고쳐지는 곳. lucida-next 가 이 앱을 부르는 APM 서비스 이름(OTEL_SERVICE_NAME, 로그와 트레이스의 service_name)이고 같은 이름의 다른 개체가 없다. Deployment `testbed-dispatch`, Service `testbed-dispatch` 와도 이름이 다르다 |
| `root_cause.trigger_target_id` | 비움(null) | 계기(dispatch 설정 배포)와 근본이 같은 곳 |
| `scoring.granularity` / `accept` | `service` / `food-delivery-dispatch`, `food-dispatch` | 앱 서비스 단위. `food-dispatch` 는 기존 정답지의 food 서비스 표기(F30-R `food-payment`, F24-Q `food-restaurant`)와 맞춘 별칭이다. dispatch 의 Deployment, Service, 파드를 가리키는 답도 같은 앱 서비스를 지목한 것이라 서로 다른 정답이 겹치지 않는다(이 시나리오에서 정답과 오답이 같은 이름을 나눠 갖지 않는다) |
| `scoring.partial` | `food-delivery-order`, `food-order` | 거절 로그와 503 이 나오는 증상 위치. order 의 용량 확인과 요청은 늘 하던 그대로라 정당하다(원칙 6: A 가 정당하면 B 가 원인) |

- 정답은 "dispatch 설정 배포가 배차 한도를 실제 배차 수 아래로 내려 주문이 거절됐다"까지다. 값 200 은 관제 데이터(order 로그의 max=200, KCM 이 잡은 새 ReplicaSet 스펙)에
  있으므로 관제 AI 가 낼 수 있는 결론이다(원칙 5). 누가 왜 그 값을 넣었는지는 요구하지 않는다
- 원본 사후 보고도 "쿼터가 줄어 서비스가 쓰지 못했다"를 원인으로 지목했다. 같은 층위다
- "배달원이 정말로 모자라다(수요 포화)"라고 답하면 위치(dispatch)는 맞고 기전은 틀리다. 서비스 입도 채점이라 accept 로 처리된다. 기전 감별은 §8 must_rule_out 의 몫이다

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 | 내용 |
|---|---|---|
| 감지(lucida-next) | 겉 증상 | (1) order APM error_rate 상승: POST /api/orders 가 503 이 되므로 식당과 메뉴 확인을 지난 주문 비율만큼 오른다(평시 6시간 중앙 0, 기대 약 0.7, 조회 요청이 섞여 1 보다 낮다). (2) INFO 새 템플릿 'Order rejected: courier pool exhausted' 의 Tier3 분류(unknown_anomaly, new_template). 119 에서 같은 order 의 INFO 거절 템플릿 'Order fan-out dispatch failed' 가 최근 8일 new_template 6건, unknown_anomaly 12건을 낸 선례가 있다(2026-10-08 09:1x 조회). 새 템플릿의 surge 는 기대하지 않는다: log-anomaly 의 surge 는 템플릿 자기 기준선 대비 비율 변화를 요구해(lucida-next `loganomaly/service/eventtime_rate.go:73-106`) 기준선이 없는 새 템플릿에는 성립하기 어렵다. (3) 'Created order', 'Dispatched courier' 로그 감소(정지). food 서비스에는 log-anomaly, trace-anomaly, stream-anomaly 가 이미 동작 중(§7 감지기 행) |
| 피해 판정(러너) | k6 live 문서 | `loadgen.food_create_status_rate`(business_5xx_rate, food business_step=create) ≥ 0.5 이고 `loadgen.transfer_2xx_rate`(business_2xx_rate) < 0.2. 5xx 비율은 평시 0(아래 표) 이라 원칙 7 의 "평시 0 근처에서 오르는 값"이다 |
| RCA | 녹화 데이터 | §7 표 |

인시던트가 실제로 생기는지는 첫 실행(곧 녹화 실행)의 확인 항목이다(§11, 원칙 7). 5xx 로 실패하므로 4xx 로만 실패하는 F30-R, F06-P 보다 가용성 신호가 강하다.

## 7. 관측 근거 (119 실조회, 2026-10-08 08:40~08:55 UTC)

| 증거 | 표, 칸 | 조회와 결과(평시) | 표본 여부 |
|---|---|---|---|
| 계기: dispatch 롤아웃 | CH `lucida.kcm_events_local` (`namespace`, `object_kind`, `object_name`, `reason`, `body`) | `SELECT timestamp, object_kind, object_name, reason, substring(body,1,110) FROM lucida.kcm_events_local WHERE timestamp > now() - INTERVAL 8 DAY AND namespace='rca-testbed-food' AND reason IN ('ScalingReplicaSet','SuccessfulCreate','Created') ORDER BY timestamp` → 2026-10-01 04:05:07 `Deployment testbed-dispatch ScalingReplicaSet "Scaled up replica set testbed-dispatch-69ff6c4468 to 1"`, `ReplicaSet SuccessfulCreate "Created pod: testbed-dispatch-69ff6c4468-drvj7"`, `Pod Created "Created container: dispatch-service"`, 04:05:58 `"Scaled down replica set testbed-dispatch-654cc46655 to 0 from 1"`(새 파드 Ready 까지 약 51초) | 전수 |
| 계기와 근본: 바뀐 설정값 | PG `kcm_resources_history` (`kind`, `name`, `yaml`, `captured_at`), `kcm_resources` | 119 `pg-1` 에서 읽기 전용 트랜잭션으로 `SELECT kind, name, captured_at, position('OTEL_SERVICE_NAME' in yaml)>0 FROM kcm_resources_history WHERE namespace='rca-testbed-food' AND name LIKE 'testbed-dispatch%' ORDER BY captured_at` → 롤아웃마다 새 ReplicaSet 이 생성 시각에 1행씩 남는다(2026-10-01 04:05:07.86 `replicaset testbed-dispatch-69ff6c4468`, `creationTimestamp 2026-10-01T04:05:07Z`). 이 ReplicaSet yaml 에 컨테이너 `env` 배열 전체(`OTEL_SERVICE_NAME` 등 11개)가 들어 있다. 장애 시 새 ReplicaSet yaml 의 env 에 `{"name":"DISPATCH_MAX_CAPACITY","value":"200"}` 가 더해져 있어야 한다(직전 ReplicaSet 과의 차이가 이 한 줄). 녹화 범위: `scripts/scenarios/pg-scope.json:55` 에서 `kcm_resources_history` 는 `captured_at` 창 자르기, `kcm_resources` 는 기본 snap | 전수 |
| 전파: order 거절 로그 | CH `lucida.lucida_logs_local` (`service_name`, `severity_text`, `body`, `trace_id`) | `SELECT countIf(body LIKE 'Order rejected: courier pool exhausted%'), countIf(body LIKE 'Order rejected: restaurant%'), countIf(body LIKE 'Created order id=%') FROM lucida.lucida_logs_local WHERE service_name='food-delivery-order' AND timestamp > now() - INTERVAL 8 DAY` → **0**, 17,914, 408,658 (보존 2026-10-01 08:00 ~ 10-08 08:46). 같은 메서드의 같은 꼴 INFO 거절 로그(`OrderService.java:66` 'Order rejected: restaurant N status=CLOSED')가 시간당 약 200건 수집되므로 같은 로거의 INFO 가 전수로 들어온다. 장애 시 기대값은 `Order rejected: courier pool exhausted (assigned=<약 1,700에서 줄어드는 값>, max=200)` 주문당 1줄 | 전수 |
| 피해: 주문 생성 로그 | 위 표 `body LIKE 'Created order id=%'`, 5분 창 | 최근 6시간 72창: 중앙 311.5, p10 224, 최대 370. 낮은 창은 6시간 범위 첫 부분창(02:40)과 MySQL 파드 재시작(07:25~07:35, KCM `testbed-mysql-0 Created` 07:38)뿐. 장애 시 0 이 기대값 | 전수 |
| 피해: 배차 로그 | 위 표 `service_name='food-delivery-dispatch' AND body LIKE 'Dispatched courier=%'` | 같은 창에서 주문 로그와 같은 수준(예: 07:35 창 created 177, dispatched 176). 장애 시 0 | 전수 |
| 감별: 만료 배치가 돈다 | 위 표 `body LIKE 'Dispatch expiry batch finished%'` | 최근 1시간 119건(30초마다), `delivered=24~43`. 장애 시에도 계속돼야 하고, 멈추면 배치 정지(F19-Q 류) 쪽이다 | 전수 |
| 감별: dispatch 가용성 장애의 꼴 | 위 표 order `body LIKE 'Failed to check dispatch capacity%'` | 7일 5분 창 2,023개 중 dispatch 경로 실패 비율 중앙 0, p99 0.015. 높은 창 4개(2026-10-01 08:50~08:55, 14:30~14:35)는 'Failed to check dispatch capacity: I/O error ...'(218건), '500 : Whitelabel Error Page'(319건) 꼴의 ERROR 로, dispatch 가 응답하지 못할 때의 모습이다. F32-R 에서는 이 ERROR 가 없어야 한다 | 전수 |
| 보조: 현재 사용량 | food MySQL(테스트베드 앱 DB, 읽기 전용 조회. **lucida-next 수집 대상 아님**) | `SELECT status, count(*) FROM dispatches GROUP BY status` → ASSIGNED 1,726, DELIVERED 4,995,911 (2026-10-08 08:41:53). 강도 계산에만 쓴다 | 러너 판단용 |
| 보조: 주문 503, 용량 조회 스팬 | CH `lucida.otel_traces_local` | 최근 1일 `food-delivery-order POST /api/orders` SERVER: 200 5,758, 400 254, 503 3. `food-delivery-dispatch GET /api/deliveries/capacity`: 200 5,757, 500 3. 장애 시 order 503 이 대부분, 용량 조회는 200 그대로 | **10% 표본, 보조로만** |
| 보조: order APM 오류율 | VM `apm.agent.otel.java.error_rate{service_name="food-delivery-order"}` | 6시간 중앙 0, 최대 50(MySQL 재시작 구간) | **표본 기반, 보조로만** |
| 감지기 동작 | CH `lucida.lucida_events_local` (`detector`, `reason`, `service_name`) | 최근 8일 food-delivery-order: log-anomaly surge 101, new_template 36, unknown_anomaly 130, trace-anomaly distribution_shift 475, stream-anomaly 74 | 전수 |

핵심 증거(계기 이벤트와 새 ReplicaSet 스펙, order 거절 로그, 주문과 배차 로그 정지, 만료 배치 지속)는 모두 전수 수집 데이터다.
트레이스와 APM 지표는 보조로만 쓴다. 장애 시 형태는 첫 실행에서 다시 확인한다.

## 8. 감별

- must_support: dispatch 롤아웃 이벤트와 새 ReplicaSet 스펙의 DISPATCH_MAX_CAPACITY=200, 그 직후 order 'Order rejected: courier pool exhausted (assigned=N, max=200)'(평시 7일 0건),
  'Created order', 'Dispatched courier' 정지, 주문 5xx 0 → 0.5 이상과 2xx 0.91~1.0 → 0.2 미만, dispatch Ready, dispatch 5xx 없음, 만료 배치 계속, 조회 정상
- must_rule_out:
  - 실제 배차 포화(수요 급증, 만료 배치 정지): 거절 로그의 assigned 가 max 의 약 8배이고 줄어든다. 직전에 'Created order' 가 늘지 않았고 'delivered=N' 이 계속된다
  - dispatch 가용성 장애: 'Failed to check dispatch capacity' ERROR 와 'Dispatch service unavailable' 이 없고 파드 Ready
  - order 쪽 변경(F08-P 꼴): order 롤아웃이 없고, 거절 로그의 max 는 dispatch 가 알려 준 값이다
  - 결제 단계 실패(F30-R, F06-P): 주문 저장 전에 503 으로 끝나 payment 로그가 주문 실패와 무관하게 조용하다
- contrast_with: F23-R(재고가 실제로 바닥나 409, 사용량이 한도까지 참), F30-R(같은 food 설정 배포지만 payment, 결제 단계 400), F08-P(설정 배포지만 바뀐 쪽이 order 자신)
- 같은 주입 중복: k8s.env 를 food testbed-dispatch 에 쓰는 기존 시나리오 없음(`test_no_two_scenarios_claim_the_same_injection` 통과). DISPATCH_MAX_CAPACITY 하향은
  `spec-scenario-design-charter.md:111` 과 F19-Q 설계에서 "배치 정지(Class A) 시나리오를 흉내 내는 수단"으로 금지됐다. 그 금지는 정상 용량 포화를 다른 결함처럼 꾸미지 말라는 뜻이고,
  F32-R 은 한도 값의 오배포 자체가 결함인 Class B(설정) 시나리오로 정답지에 그대로 적는다(헌장 G1 금지 2: "자연 고갈이 아니면 Class B 로 명시")

## 9. 러너 판정과 강도, 부하 계산

- 진행 대본: 설계 강도 하나로 고정한 evaluation(`approved-fixed-f32-r`, `profile.kind: fixed`, escalate 없음). 첫 실행이 곧 녹화 실행이다(`spec-scenario-lifecycle.md` §2)
- 강도 근거: 한도 200 은 평시 ASSIGNED 1,726(2026-10-08 08:41) 의 약 12%다. 평시 정상 상태는 주문 약 1.15/s(최근 1시간 'Created order' 4,130건) × 평균 ETA 24.5분(15~34분 균등) ≈ 1,690 으로 실측과 맞는다.
  롤아웃 뒤 새 배정이 0 이므로 ASSIGNED 는 만료 배치만큼만 준다(30초마다 약 35건, 약 1.15/s). 200 아래로 내려가려면 (1,726 − 200) / 1.15 ≈ 1,330초, 약 22분이 걸린다.
  `max_injection_duration` 20분, `min_hold` 6분이므로 주입 내내 available=0, 곧 식당과 메뉴 확인을 지난 주문의 100% 가 503 이다
- 자연 포화가 끼어들지 않는가: 기본 한도 2000 에 도달하려면 평시보다 주문이 더 들어와야 한다. 동반 부하 3rps 의 주문 여정 비중 10%(surge.js `default` 의 `r` 0.75~0.85 구간, `orderJourneyBounded`)로 약 0.3 주문/s 가 더해지는데,
  ASSIGNED 가 1,726 에서 2,000 까지 차려면 그 초과분만으로 약 15분이 필요하다. 주입 전 램프(2분)로는 닿지 않고, 주입 중에는 새 배정이 없으며, cleanup 뒤에는 ASSIGNED 가 줄어든 상태라 녹화 구간에서 자연 포화가 생기지 않는다.
  같은 이유로 F30-R 의 5rps 대신 3rps 로 낮췄다
- success(all, 3틱): `order_create_5xx_rate`(business_5xx_rate) ≥ 0.5, `order_create_2xx_rate`(business_2xx_rate) < 0.2.
  평시: 상주 기준선 문서(`/tmp/rca-baseline-food-delivery-live.json`, 2026-10-08 08:45:59~08:51:37, 30초 간격 12회) business_5xx_rate 0.0~0.0, business_2xx_rate 0.912~1.0, business_4xx_rate 0~0.088(닫힌 식당 400).
  119 로그 기준 7일 5분 창의 dispatch 경로 실패 비율 중앙 0, p99 0.015. 장애 시 기대: 5xx 약 0.9 이상(닫힌 식당 400 몫만 빠짐), 2xx 0 근처. 장애 시 값은 첫 실행에서 실측한다
- must_rule_out(any, 2틱): `achieved_rps < 1`(부하 미도달), `read_step_rate ≥ 0.1`(조회 경로까지 깨짐 = 진입이나 food 전반 장애),
  `pod_ready(testbed-mysql) == false`(food DB 재시작. 119 에서 최근 8일 `testbed-mysql-0 Created` 5회, 07:25~07:35 창처럼 주문과 배차가 함께 멈춘다. 가장 잦은 환경 교란이다)
- 러너가 직접 못 보는 것: 러너 관측 허용 목록(`rca-scenario-runner/backend/app/live_probes.py` 의 `APPROVED_K8S_TARGETS`, `APPROVED_APM_SERVICES`)에 food dispatch 가 없다.
  그래서 "dispatch 파드 Ready", "dispatch 5xx 없음"은 러너 판정이 아니라 녹화 데이터의 RCA 증거(§7, 정답지 must_support, must_rule_out)로 둔다. 러너를 고치면 재빌드가 필요하고,
  재빌드 전에 큐가 이 시나리오를 집으면 관측이 계속 읽기 실패가 되므로 이번에는 고치지 않는다. dispatch 롤아웃이 실제로 끝났는지는 k8s.env 실행기 cleanup 의
  `healthy 180s`(observedGeneration, updated/available replicas)가 확인하고, 실패하면 cleanup 이 실패한다
- **운영 메모(녹화 검증 때 필수 확인)**: 러너 success(5xx ≥ 0.5, 2xx < 0.2)는 "용량 0 거절"과 "dispatch 불능"을 구별하지 못한다. dispatch 가 응답하지 못해도 `DispatchClient.java:47-50`(checkCapacityFallback, 503 'Dispatch service unavailable')와 `OrderService.java:108-111`(503 'Dispatch service unreachable')로 같은 503 이 난다. 그래서 검증 하네스는 녹화본에서 order 로그 `Order rejected: courier pool exhausted (… max=200)` 가 있고, `Failed to check dispatch capacity`, `Dispatch service unavailable`, `Dispatch capacity check failed` 가 없는지를 반드시 확인한다. 둘 중 하나라도 어긋나면 이 시나리오의 녹화가 아니다. 다음 러너 변경 때 `testbed-dispatch` 를 `APPROVED_K8S_TARGETS`(`rca-testbed-food`, `app=testbed-dispatch`)에, `food-delivery-dispatch` 를 `APPROVED_APM_SERVICES` 에 넣어 러너가 dispatch 파드 Ready 와 5xx 를 직접 배제하게 한다
- recovery: target 200, `pod_ready(testbed-mysql)` true, food 기준선 2xx ≥ 0.7. recovery 의 `pod_ready(testbed-mysql)` 는 compile-plan 형식 요건(recovery 에 `pod_ready == true` 필수, `compile-plan.py:238-240`)을 채우는 칸이고, 실질 회복 판정은 `create-recovered`(food 기준선 2xx ≥ 0.7)와 실행기 cleanup 의 `healthy 180s`(`scripts/scenarios/profiles/k8s_env_executor.py:111`, dispatch Deployment 의 observedGeneration, updated/available replicas 확인)다. 반면 must_rule_out 의 `pod_ready(testbed-mysql) == false` 는 실질 배제다: MySQL 재시작은 주문과 배차를 함께 멈춰 같은 증상을 내고 잦다(파드 72일 재시작 43회). 한도 2000 복원 뒤 ASSIGNED 는 줄어든 상태라 즉시 수용된다
- 서킷브레이커: 용량 조회는 200 응답이라 order 의 dispatch retry 와 CB(`application.yml` resilience4j `dispatch`)가 관여하지 않는다. 배차 요청과 결제는 호출되지 않는다. 그래서 피해는 강도와 무관하게 결정적이다
- 롤아웃: replicas 1, maxSurge 25%(올림 1), maxUnavailable 25%(내림 0). 새 파드가 Ready(약 51초, §7) 된 뒤 옛 파드가 내려가 dispatch 가용성 공백이 없다. level `settle` 60초
- 부하 상한: target_rps 3 은 상한 180 의 2%

## 10. 설계 원칙 §5 점검표 (각 칸의 근거는 2026-10-08 이 세션에서 실행한 조회와 파일 확인)

- [x] 원본 실제 사례와 대응표, 기전 동일: Google 2020-12-14 공식 사후 보고를 WebFetch 로 열어 근본 원인 문단("incorrectly reported the usage ... as 0", "the quota for the account database was reduced", "disabling the quota enforcement")을 확인했다. 대응표와 차이 셋은 §2
- [x] 분류 장부 갱신과 숫자: 장부 §4-1 에 F32-R(G, 배달 서비스), §6 기록 한 줄. `scenario-stats.py` 결과는 §3(G 2→3/37 8%, 배달 서비스 0→1/37 3%, 결제 경로 합계 10 그대로이고 이 후보는 결제 쪽이 아님, 음식배달 5→6). 어느 축도 20% 미만
- [x] 근본 원인 위치: `application.yml:78-83`, `DispatchService.java:39-48`, `:105-114`, `DispatchController.java:59-66`, `OrderService.java:94-105` 를 열어 심볼 확인(검사 `test_code_anchors_still_name_a_symbol_that_lives_there` 통과). 인프라 지점은 109 kubectl 로 본 실배치 env 11개(§4)
- [x] 근본 원인 흔적 119 조회: PG `kcm_resources_history` 에 dispatch ReplicaSet 이 롤아웃마다 env 포함 yaml 로 남는 것을 조회(§7 두 번째 행), CH `lucida_logs_local` 의 order 거절 로그 꼴과 평시 0 건(§7 세 번째 행)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: 계기와 근본은 KCM 이벤트와 PG 리소스 이력, 전파와 피해는 로그(모두 전수). 트레이스와 APM 은 보조(§7 표 오른쪽 칸)
- [x] 계기의 흔적: `kcm_events_local` 의 testbed-dispatch ScalingReplicaSet, SuccessfulCreate, Created 이벤트가 평시 롤아웃에서 실제로 남았다(2026-10-01 04:05:07). 인공 지연 없음, 주입은 설정값 하나
- [x] 정답이 관제 데이터로 낼 수 있는 결론: 한도 값 200 이 order 로그와 새 ReplicaSet 스펙에 직접 남는다. 코드 설계 결함 추론이 필요 없다(§5)
- [x] 정답지 세 칸: 근본 `food-delivery-dispatch`(config), 계기 null(같은 곳), 부분 점수 `food-delivery-order`, `food-order`. 근본은 부분 점수에 없다(검사 `test_written_keys_are_machine_matchable` 통과)
- [x] 감지, 피해 판정, RCA 증거 구분: §6 표. 피해 판정 값(business_5xx_rate)은 평시 12회 실측 모두 0.0
- [x] 주입이 시나리오 id 를 남기지 않음: env 값은 `DISPATCH_MAX_CAPACITY=200` 뿐이고 ReplicaSet 이름은 Kubernetes 해시. k6 의 `--tag scenario_id=F32-R` 는 tb-runner 로컬 지표 태그로 HTTP 요청에 실리지 않는다(surge.js 는 `journey`, `step` 태그만 요청에 붙임, 기존 모든 시나리오와 같음). executor 상태 파일 `F32-R-container-env.json` 과 kubectl 호출은 109 러너 컨테이너 안(`kubectl_bash_argv`, `/var/lib/lucida/scenario-profile-state`)에서 일어나 F30-R 등 기존 k8s.env 시나리오와 같은 경로다
- [x] 부하 상한과 서킷브레이커를 넣은 피해 계산: §9(ASSIGNED 1,726 이 200 아래로 내려가는 데 약 22분 > 주입 20분, 용량 조회가 200 이라 CB 무관, 자연 포화가 끼지 않는 동반 부하 3rps)

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- **새 ReplicaSet 행에 DISPATCH_MAX_CAPACITY=200 이 있는가**: PG `kcm_resources_history` 에서 장애 창 안 `kind='replicaset' AND name LIKE 'testbed-dispatch-%'` 행의 yaml env 에 `{"name":"DISPATCH_MAX_CAPACITY","value":"200"}` 이 있어야 한다. 