---
title: F51-P 설계 시트 — 재배차 정정 작업의 입력 누락이 최근 완료 배차를 모두 되살려 배달원 용량을 채움
status: Draft
owner: project
last_reviewed: 2026-10-11
tags:
  - scenario
  - design
  - database
summary: food 운영자의 재배차 정정 작업이 주문 id 목록 없이 기간 조건만으로 실행되어 최근 4시간에 끝난 배차 수천~만여 행을 fooddelivery.dispatches 에서 배정 중(ASSIGNED, 새 배정 시각)으로 되돌리고, dispatch 가 배정 중 행 수로 세는 배달원 용량(2000)이 한 번에 차서 모든 새 주문이 503 으로 거절되는 시나리오(AWS S3 2017-02-28, Atlassian 2022-04-05 재구성)의 설계 근거.
---

# F51-P 설계 시트

- id `F51-P`, slug `f51-p-food-redispatch-correction-reopens-finished-deliveries-fills-courier-pool`, 사례군 F51(F51-R 과 같은 원본 AWS S3 2017-02-28 '운영 명령의 입력 하나가 잘못되어 의도보다 큰 집합에 적용'. F51-R 은 Deployment 를 replicas 0 으로 줄여 용량을 없앴고, F51-P 는 데이터 행을 바꿔 용량 계산을 채운다. 최초 원인 꼴은 같고 대상, 수단, 피해 모양이 달라 P)
- 상태: 후보(ready + `stage: candidate`), 설계 강도 하나로 고정한 evaluation 모드, 녹화 대기 큐에 추가
- 주입 수단: 새 실행기 `db.row_update`(`scripts/scenarios/profiles/db_row_update_executor.py`). 동반 부하 없음(상주 기준선 loadgen-food)

## 1. 요약과 비유

food dispatch-service 는 배달원 용량을 따로 세지 않고 `fooddelivery.dispatches` 의 배정 중(`status = 'ASSIGNED'`) 행 수로 센다. 주문이 들어오면 order 가 먼저 용량을 묻고(GET /api/deliveries/capacity),
배정 중 행이 `dispatch.max-capacity`(2000) 이상이면 order 가 그 주문을 503 으로 거절한다. 배정 중 행은 ETA(15~34분)가 지나면 30초마다 도는 만료 배치가 배달 완료(DELIVERED)로 바꾼다.

배달원 앱 오류로 몇 건의 배달이 '배달 완료'로 잘못 찍혔다는 문의가 들어와, 운영자가 늘 쓰던 재배차 정정 작업(정정 전 백업 표를 만들고 대상 배차를 배정 중으로 되돌려 배정 시각을 새로 찍음)을 실행한다.
그런데 대상 주문 id 목록 칸이 비어 작업이 기간 조건(최근 4시간)만으로 대상을 고르고, 그 4시간 동안 끝난 배차 전부(평시 약 2,600~16,000 행)가 한 번에 배정 중으로 돌아간다.
그 순간 배정 중 행 수가 용량 2000 을 훌쩍 넘어, 다음 주문부터 모두 `Order rejected: courier pool exhausted (assigned=<수천~만여>, max=2000)` 으로 503 이다.
되살린 행은 배정 시각이 방금이라 ETA(최소 15분)가 지나기 전에는 만료 배치도 건드리지 않는다. 가게, 메뉴 둘러보기와 주문 조회, 결제는 정상이고 롤아웃, 재시작, 설정 변경은 하나도 없다.

비유: 주차장 관리 프로그램이 "지금 서 있는 차 수"를 세어 만차를 판단한다. 직원이 잘못 출차 처리된 차 몇 대를 '주차 중'으로 되돌리려다 차량 번호 칸을 비워 두고 실행했더니,
오늘 오후에 나간 차 수천 대가 모두 '주차 중'이 되어 입구 전광판이 만차를 띄운다. 주차장은 텅 비어 있고 전광판도 고장이 아니다. 문제는 기록을 한꺼번에 고친 정정 작업이다.

## 2. 원본 사례

- **AWS S3, 2017-02-28** (us-east-1), 공식 요약 [aws.amazon.com/message/41926](https://aws.amazon.com/message/41926/). `docs/ref-real-world-incidents.md` M22 에 이미 있는 행이고 F51-R 과 같은 원본이다. 2026-10-11 WebFetch 로 원문 다시 확인:
  - "At 9:37AM PST, an authorized S3 team member using an established playbook executed a command"
  - "one of the inputs to the command was entered incorrectly" 이고 의도보다 많은 서버가 빠짐. 빠진 서버는 "supported two other S3 subsystems"(index, placement).
  - "Removing a significant portion of the capacity caused each of these systems to require a full restart." "While these subsystems were being restarted, S3 was unable to service requests."
  - 재발 방지: 도구가 용량을 천천히 빼고, 하위 시스템이 "below its minimum required capacity level" 로 내려가는 제거를 막는 안전장치를 더함.
- **같은 꼴의 보조 사례 Atlassian, 2022-04-05** (공식 사후 검토, M22 에 이미 있음, F56-R 원본): 정리 스크립트에 앱 id 대신 사이트 id 가 넘어가 API 가 입력을 그대로 믿고 의도보다 넓은 살아 있는 대상을 즉시 바꿈.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 정해진 플레이북으로 S3 과금 하위 시스템의 서버 몇 대를 빼려는 명령 실행 | 정해진 재배차 정정 작업으로 '배달 완료'가 잘못 찍힌 배차 몇 건을 배정 중으로 되돌리려는 실행 |
| 원인이 된 결함 | 명령 입력 하나가 잘못 들어가 의도보다 큰 서버 집합에 적용, 도구에 최소 용량 안전장치 없음 | 대상 주문 id 입력이 비어 기간 조건만 남아 최근 4시간의 끝난 배차 전부(수천~만여 행)에 적용, 작업에 대상 수 상한 없음 |
| 전파 경로 | 빠진 용량이 다른 두 하위 시스템(index, placement)이 기대던 것 → 전체 재시작 → S3 요청 처리 불가 | 되살린 행이 dispatch 가 배달원 용량으로 세는 배정 중 행 → 용량 2000 이 가득 참 → order 의 용량 확인이 available 0 → 주문 503 |
| 사용자 증상 | us-east-1 S3 요청 실패, S3 에 기대는 서비스 장애 | 모든 새 음식 주문이 503 'No courier available (capacity full)'. 둘러보기, 주문 조회, 결제는 정상 |
| 원인 쪽 상태 | 서버와 하위 시스템 자체는 정상, 용량이 빠짐 | dispatch, order, MySQL 은 Ready 이고 빠르다. 용량 설정 값(2000)도 그대로이고 배정 중 행 수만 부풀었다 |
| 탐지된 경로 | 요청 실패(감시, 고객) | order 오류율(POST /api/orders 503), order 로그 'Order rejected: courier pool exhausted (assigned=…, max=2000)'(평시 0), 같은 분 DPM fooddelivery 표 수 16→17, 행 수 급증 |
| 완화와 복구 | 하위 시스템 재시작, 도구에 안전장치 | 정정 전 백업 표에서 상태와 배정 시각을 되돌림(cleanup). 되돌리지 않아도 ETA(15~34분)가 지나면 만료 배치가 배달 완료로 바꾸며 저절로 풀리지만 그동안 주문이 막힌다 |

기전 확인: 운영 명령의 입력 실수 → 의도보다 큰 대상 집합에 적용 → 살아 있는 서비스가 기대던 용량이 사라짐(원본은 서버를 빼서, 테스트베드는 용량 계산을 채워서) → 요청 거절이라는 고리가 같다.
의도한 작업 내용(재배차 정정)은 맞았고 적용 범위가 틀렸다는 점에서 장부 P 묶음의 정의 그대로다. 원본의 "하위 시스템 재시작"은 테스트베드에 해당하는 단계가 없고, 그 대신 '배정 중 행이 만료될 때까지'가 저절로 풀리는 시간이 된다.

## 3. 숫자 근거 (2026-10-11, `scenario-stats.py`, 정식 + 후보 90 종, 이 후보 전)

- 서비스: commerce 31, core-banking 30, **food-delivery 29 → 30**. 음식배달이 가장 적다.
- 묶음: A 7, B 7, C 6, D 11, E 4, F 3, G 12, H 2, I 1, J 14, K 2, L 7, M 1, O 1, **P 10 → 11**, Q 2. 20% 상한(18)에 닿는 묶음 없음(가장 큰 J 15.6%). P 는 11/91 = 12.1%.
  0 묶음 N(재시도가 장애를 키움)은 이번 실행에서 계속 막힌 벽(재시도 증폭은 하류 실패라는 두 번째 계기가 필요하고 180rps 안에서 포화 계산이 서지 않음)이라 이번 후보 목록에서도 서지 않았다(§11).
- 정답 위치: **DB 테이블(배차) 3 → 4**(F33-R, F32-P, F60-H 와 같은 위치, 4/91 = 4.4%). 가장 큰 위치는 주문 서비스 8(8.8%). 결제 경로 합계 12(13.2%, 무관).
- 장부와 catalog 대조 어긋남 없음, 큐 대조 어긋남 없음.
- 왜 이 후보인가: 음식배달이 가장 적고, P 묶음과 정답 위치 모두 상한과 거리가 멀다. 겉 증상(주문 503, 'courier pool exhausted')이 F32-R(배차 한도 설정 2000→200 배포)과 같고 정답이 다르다(배달 서비스 설정 대 배차 표의 데이터).
  관제 AI 는 order 로그의 assigned 와 max 값(F32-R 은 max=200 으로 줄고 assigned 는 평시 크기, F51-P 는 max=2000 그대로 assigned 가 수천~만여로 부풂), 롤아웃 유무(KCM), DPM 데이터베이스 목록의 변화로 갈라야 하므로 추론 없이 "dispatch 설정"을 찍는 지름길이 막힌다.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 정정 작업(주입): `CREATE TABLE fooddelivery.dispatches_redispatch_backup AS SELECT id, status, assigned_at FROM fooddelivery.dispatches WHERE status = 'DELIVERED' AND assigned_at >= NOW() - INTERVAL 4 HOUR;` 뒤
   `UPDATE fooddelivery.dispatches d JOIN fooddelivery.dispatches_redispatch_backup b ON b.id = d.id SET d.status = 'ASSIGNED', d.assigned_at = NOW() WHERE d.status = 'DELIVERED';`
   (MySQL 8.0.46, gtid_mode OFF, binlog ROW, REPEATABLE-READ. 2026-10-11 11:06 UTC 109 읽기 전용 실측: 4시간 창의 DELIVERED 14,389 행, ASSIGNED 1,443, 전체 1,744,576)
2. `food-delivery/dispatch-service/src/main/java/com/fooddelivery/dispatch/service/DispatchService.java:106-113 (getCapacity)` — `countByStatus("ASSIGNED")` 로 배정 중 행을 세어 available = max(0, maxCapacity - currentAssigned)
3. `food-delivery/dispatch-service/src/main/resources/application.yml:83` — `dispatch.max-capacity: ${DISPATCH_MAX_CAPACITY:2000}`(평시 ASSIGNED 약 300~1,700 의 Little 법칙 크기)
4. `food-delivery/order-service/src/main/java/com/fooddelivery/order/client/DispatchClient.java:32-45 (checkCapacity)` — GET /api/deliveries/capacity, 200 본문을 그대로 돌려줌
5. `food-delivery/order-service/src/main/java/com/fooddelivery/order/service/OrderService.java:94-104` — available ≤ 0 이면 INFO 'Order rejected: courier pool exhausted (assigned={}, max={})' 와 ServiceException 503 'No courier available (capacity full)'. 첫 쓰기(:128) 앞이라 주문 행, 배차, 결제가 생기지 않음
6. `food-delivery/dispatch-service/src/main/java/com/fooddelivery/dispatch/service/DispatchService.java:60-63 (dispatchCourier)` — 같은 셈으로 503 'Courier pool exhausted'(order 의 용량 확인이 먼저 거절해 여기까지 오는 주문은 없음)
7. `food-delivery/dispatch-service/src/main/java/com/fooddelivery/dispatch/repository/DispatchRepository.java:26-29 (findExpiredAssigned)` 와 `DispatchService.java:119-130 (deliverExpiredDispatches, 30초 주기)` — 되살린 행은 assigned_at 이 방금이라 ETA(eta_minutes 15~34) 전에는 후보가 아님
8. `food-delivery/db/init.sql:111-131 (dispatches)` — 인덱스 `idx_dispatches_status_assigned (status, assigned_at)` 덕에 수만 행 ASSIGNED 에서도 COUNT 와 만료 질의는 범위 스캔(ms)
9. 인프라: rca-testbed-food StatefulSet testbed-mysql(파드 testbed-mysql-0, tb-w3). 실행기는 러너에서 kubectl exec 로 MySQL 클라이언트를 root 로 연다(db.account MySQL 모드와 같은 방식).

### 로컬 실측 (mysql:8.0, 2026-10-11)

같은 표 정의에 3초 간격 2만 행(최근 1,000 행 ASSIGNED)을 넣고 실행기 원격 스크립트를 kubectl 대신 docker exec 로 돌렸다.
preflight 통과 → run: 백업 3,797 행, ASSIGNED 1,000 → 4,797, 되살린 행의 배정 시각이 모두 실행 분 → run 중 preflight, recovery 는 실패(백업 있음, 용량 가득) → cleanup: 상태별 수와 배정 시각 최소, 최대가 run 전과 같고 백업 표 없음 → recovery 통과, cleanup 반복도 통과(멱등).

## 5. 원인 규정 (원칙 5, 6)

| 칸 | 값 | 근거 |
|---|---|---|
| `root_cause.target_id` | `food-mysql:fooddelivery.dispatches` | 결함을 지닌 곳은 운영 작업이 잘못 바꾼 배차 표의 데이터다. dispatch 의 용량 계산과 order 의 거절은 표에 적힌 대로 정당하게 동작했다(원칙 6: 정당한 요청이 결함 있는 곳에서 실패). F33-R, F32-P, F60-H 와 같은 '인스턴스:스키마.테이블' 꼴 |
| `trigger_target_id` | null | 근본과 계기가 같은 곳(이 표에 대한 잘못된 범위의 정정 작업) |
| `scoring.partial` | food-delivery-dispatch, food-dispatch, food-mysql | 용량 거절이 나오는 dispatch(설정 문제로 오인한 답), DB 인스턴스 전체 |

원칙 5: 정답은 order 로그의 assigned 값(평시 앱이 만들 수 있는 배정 수보다 몇 배 큼)과 max 값이 그대로라는 사실, 같은 시간 dispatch 의 'Dispatched courier' 와 만료 배치 'delivered=' 가 평시 크기라는 사실(앱이 그만큼 배정한 적이 없음),
같은 분 DPM fooddelivery 표 수 16→17 과 행 수 급증(정정 백업 표), 롤아웃과 재시작이 없다는 사실로 낼 수 있는 결론이다. 코드 설계 결함 추론이 필요 없다. 누가 왜 그 작업을 돌렸는지는 요구하지 않는다.

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 | 기대 |
|---|---|---|
| 감지 | order 서버 스팬 POST /api/orders 503(ERROR), order 로그 surge('Order rejected: courier pool exhausted' 새 값), 'Created order' 와 'Dispatched courier' 중단 | F32-R(같은 거절 경로) 정식 녹화에서 인시던트가 생성됐다. 고장 내내 주문 생성 5xx |
| 피해 판정 | 러너: 기준선(loadgen-food) 주문 생성 5xx 비율 ≥ 0.5, 2xx 비율 < 0.2(3틱) | 평시 5xx 0 근처(배차 한도 거절은 10월 4일 이후 F32-R 실행 때뿐) |
| 원인 설명 | 로그 전수(assigned, max 값, 앱 배정 수), DPM 데이터베이스 목록, KCM 무변화 | §7 |

## 7. 관측 근거 표 (119 실조회, 2026-10-11 10:50~11:20 UTC, 평시)

| 증거 | 표와 칸 | 쿼리 | 결과 |
|---|---|---|---|
| 거절 문장과 값 | CH `lucida_logs_local`(service_name food-delivery-order, body) | `body LIKE 'Order rejected: courier pool exhausted%'` 날짜별 건수, `extract(body,'assigned=([0-9]+)')` 최소·최대, `extract(body,'max=([0-9]+)')` | 보존 로그(가장 이른 2026-10-04 08:02 UTC) 중 10-08 181건(assigned 213~306), 10-09 226건(434~582), 모두 max=200(F32-R 실행). max=2000 인 거절은 0건 → 평시에는 용량이 차지 않는다 |
| 앱이 만든 배정과 완료 | CH 같은 표(food-delivery-dispatch) | 시간별 `countIf(body LIKE 'Dispatched courier=%')`, `sumIf(extract(body,'delivered=([0-9]+)'))`, `maxIf(extract(body,'candidates=([0-9]+)'))` | 26시간 시간당 배정 719~4,134, 완료 712~4,158 로 맞물림, 30초 만료 후보 최대 13~61. 앱이 34분(최대 ETA) 안에 만들 수 있는 배정 중 행은 2,000 미만이다 |
| 주문 실패 | CH `otel_traces_local`(service_name food-delivery-order, span_name POST%, status_code, span_attributes['http.response.status_code']) | 3일 상태별 건수 | 200 52,300, 503 187(F32-H 녹화 등 시나리오 실행), 500 173. 스팬은 10% 표본이라 추세 보조로만 쓴다 |
| 정정 작업의 흔적 | VM `dpm.mysql.database.table_count{db_name="fooddelivery"}`, `dpm.mysql.database.row_count` (1분) | `min_over_time`, `max_over_time` 7일 | 표 수 7일 내내 16(최소 16, 최대 16). 행 수 약 1,150만. 백업 표가 생기면 17, 행 수가 되살린 행 수만큼 늘고 cleanup 뒤 16 으로 돌아온다 |
| 배제: DB 정상 | VM `dpm.mysql.session.active_session`, `dpm.mysql.instance.avg_query_response_time` | (F38-R, F60-H 시트와 같은 7일 분포) | 정정 문장 하나는 수 초 안에 끝나고 세션과 응답 시간은 평시 |
| 배제: 롤아웃 없음 | CH `kcm_events_local`, KCM ReplicaSet | rca-testbed-food 주입 구간 | 주입은 쿠버네티스 객체를 바꾸지 않는다 |
| 행 수준 지표 | VM `dpm.mysql.sql.update_count`, `row_examined_count`, `row_lock_count` 7일 분위 | `quantile_over_time` | update 중앙 258/분, row_examined 중앙 약 33만(최대 1,536만): 정정 문장 하나는 이 분포 안이라 증거로 쓰지 않는다 |

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 결정 증거는 전수 로그(assigned, max 값과 앱 배정 수)와 DPM 데이터베이스 목록, KCM 이다.
정정 문장 자체는 DPM Top SQL 에 남지 않는다(자주 도는 다이제스트만 남음, F38-R, F56-R 시트와 같은 한계). 계기는 assigned 값의 갑작스러운 급증과 같은 분의 표 수, 행 수 변화로 드러난다.

## 8. 감별

- must_support: order 'Order rejected: courier pool exhausted (assigned=<수천~만여>, max=2000)'(평시 max=2000 거절 0), POST /api/orders 503, 'Created order' 와 dispatch 'Dispatched courier' 중단, 만료 배치 'delivered=' 평시 크기, 같은 분 DPM fooddelivery 표 수 16→17 과 행 수 급증, 롤아웃과 재시작 없음, 둘러보기와 결제 정상.
- must_rule_out:
  - F32-R(배차 한도 설정 배포): 그쪽은 testbed-dispatch 롤아웃(새 ReplicaSet)이 있고 max=200, assigned 는 평시 크기. 여기는 롤아웃이 없고 max=2000 그대로 assigned 가 부풂.
  - F32-H(숫자 형식 설정 배포): 그쪽은 롤아웃 뒤 order 가 용량 응답을 못 읽어 'Dispatch capacity check failed' 로 거절하고 'courier pool exhausted' 는 0건.
  - F32-P, F33-R, F60-H(배차 표 지우기, 인덱스 제거, 잠금 대기열): 그쪽은 dispatch 가 SQL 오류나 시간 초과로 실패하고 용량 확인이 200 을 못 준다. 여기는 dispatch 가 200 으로 빠르게 답한다.
  - 부하 급증(F07-H 꼴): 주문 요청량과 앱 배정 수가 평시 크기이고, assigned 의 급증이 배정 로그로 설명되지 않는다.
  - 배차 만료 배치 정지(rejected 20 꼴): 만료 배치가 30초마다 평시처럼 돌고 'delivered=' 를 남긴다. assigned 는 서서히가 아니라 한 분에 뛴다.
- contrast_with: F32-R, F32-H, F32-P, F33-R, F60-H, F51-R(같은 원본의 replicas 0 판), F56-R(같은 꼴의 Oracle 행 삭제 판).

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

- 강도(고정 한 단계): 창 4시간, 최소 2,500 행, 용량 2,000. 4시간 창의 끝난 배차는 평시 시간당 720~4,130 건(10-09~10-11 실측)이라 약 2,600(새벽)~16,000(낮) 행이다.
  되살린 행만으로 용량을 넘으므로(최소 2,500 > 2,000) 평시 배정 중 행이 얼마든, 고장 중 새 배정이 없어 배정 중 행이 줄더라도 주입 내내 가득 차 있다.
  preflight 는 백업 표가 없고, 창의 행이 2,500 이상이며, 배정 중 행이 2,000 미만(용량이 이미 차 있지 않음)인지 본다.
- 동반 부하 없음: 동반 주문 부하는 주입 전 램프 동안 배달원 풀을 채워(낮 시간 평시 배정 중 약 1,700 + 동반 부하 초당 1.4건 × 평균 ETA 24.5분) 주입 없이도 용량이 찰 수 있다(F54-P 시트와 rejected 20행의 교훈).
  상주 기준선(loadgen-food, 하루 주기 1~6 iter/s, 주문 분당 약 12~68건)이 주문 실패 신호를 낸다.
- 판정: success 기준선 주문 생성 5xx ≥ 0.5 와 2xx < 0.2(3틱), must_rule_out MySQL 파드 NotReady, order 파드 NotReady, 기준선 읽기 단계 실패율 ≥ 0.1(dispatch 파드 준비 상태는 러너 관측 허용 목록에 없어 녹화 검증에서 로그로 가른다), abort entry_status 0(2틱).
- 시간: min_hold 9m, level timeout 11m, max_injection_duration 13m. 되살린 행 중 가장 이른 만료가 15분 뒤라 cleanup 이 그 전에 끝나 만료 배치가 되살린 행을 '배달 완료'로 다시 처리하지 않는다(중복 배달 완료 이벤트와 알림 없음).
- 서킷브레이커: order 의 dispatch 서킷은 예외에만 반응하는데 용량 확인은 200 으로 돌아오므로 열리지 않는다. 거절은 order 도메인 검증이라 재시도도 없다. 주입 직후 첫 주문부터 503 이다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(AWS S3 2017-02-28 공식, 보조 Atlassian 2022-04-05 공식)와 요소별 대응표, 기전 같음 (§2)
- [x] 장부 갱신(§4-1 행, §6 기록), 묶음 P 10→11(12.1%), 정답 위치 DB 테이블(배차) 3→4(4.4%), 결제 경로 12(13.2%), 서비스 음식배달 29→30(최소), 어느 축도 20% 미만 (§3)
- [x] 근본 원인 위치: 인프라 지점(food MySQL fooddelivery.dispatches 행)과 코드 위치(DispatchService.java:106-113, OrderService.java:94-104) (§4)
- [x] 근본 원인 흔적 119 실조회: CH `lucida_logs_local` 거절 문장의 assigned, max 값과 앱 배정 수, VM DPM `dpm.mysql.database.table_count`, `row_count` (§7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: 로그 전수와 DPM, KCM (§7)
- [x] 계기 흔적: assigned 의 한 분 급증(앱 배정 수로 설명 불가)과 같은 분 표 수 16→17, 행 수 급증. 인공 지연 없음 (§7)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다 (§5)
- [x] 정답지 세 칸 원칙 6대로 (§5)
- [x] 감지, 피해 판정, RCA 증거 구분 (§6)
- [x] 주입 도구가 시나리오 id 를 남기지 않는다: 백업 표 이름과 문장, 인자에 id 없음(테스트로 고정). 실행기의 확인 SELECT 가 DPM 표에 남는지는 첫 녹화 누설 검사에서 본다
- [x] 부하 상한과 서킷브레이커를 고려한 피해 계산 (§9)

## 11. 후보 목록 (반복 49, 2026-10-11, 실제 기전 × 부품, 숫자 순)

| # | 후보 | 원본 사례 | 부품, 묶음 | 판정 |
|---|---|---|---|---|
| 1 | **재배차 정정 작업의 입력 누락으로 최근 완료 배차가 모두 배정 중으로 되살아나 배달원 용량을 채움** | AWS S3 2017-02-28 공식, Atlassian 2022-04-05 공식 | food dispatches 표 데이터, P | **채택**(음식배달 최소, P 12%, DB 테이블(배차) 4, F32-R 과 같은 증상 다른 정답) |
| 2 | food MySQL 전역 max_join_size 하향으로 배차 건수 질의가 1104 | Google 2020-12-14 | food MySQL 인스턴스, G | 버림: 카탈로그 §1. F54-R(호출당 논리 읽기 한도)의 엔진과 범위만 바꾼 꼴이고 같은 원본의 세 번째 판 |
| 3 | food Kafka 브로커 설정 변경으로 발행 정지 | (공식 사례 미확보) | food Kafka, B | 버림: 원칙 7. 세 도메인 Kafka 발행이 모두 비동기 outbox 라 사용자 증상 없음(rejected 27, 146 행과 같은 벽) |
| 4 | banking nginx 설정 배포(경로, 시간 제한) 오류 | (공식 사례 미확보) | banking nginx, G | 버림: 원칙 3, 4. 119 에 nginx 로그가 없고(로그 서비스 목록에 없음) ConfigMap 변경도 KCM 에 남지 않음 |
| 5 | food 워커 tb-w3 MTU 변경으로 큰 패킷 손실 | LiveKit 2025-09 공식 상태 페이지(SIP 게이트웨이 MTU 1500 하향으로 큰 INVITE 5% 손실) | tb-w3 네트워크, K | 버림: 원칙 1, 9. 원본은 UDP 큰 메시지이고 food 서비스 사이 TCP 는 같은 노드 브리지에서 MSS 로 맞춰져 손실 경로가 서지 않음 |
| 6 | dispatch 시간대 설정이나 MySQL time_zone 변경으로 배차 만료 정지 | GOV.UK Notify DST 사고(스케줄러 라이브러리 결함) | dispatch 설정, 시계, G | 버림: 원칙 1. 앱과 DB 시간대 불일치가 원인인 공식 사후 보고 없음(웹 검색 2회, rejected 19 행과 같음) |
| 7 | food MySQL 버퍼 풀 축소 + 매시 예약 작업 | Slack 2023-08-02 공식(클러스터 이전이 용량을 줄인 상태에서 매시 예약 작업이 몰림) | food MySQL 인스턴스, A | 버림: 원칙 9, 1. 버퍼 풀이 이미 기본 128MB 라 더 줄일 여지가 없고 원본은 줄어든 용량의 정체를 밝히지 않은 복합 계기 |
| 8 | 한 서비스의 풀 최대·최소 유휴 확대가 DB 연결 상한을 차지 | Resend 2026-02-15 공식 | food MySQL, E | 버림: rejected 61 행과 같은 주입(굶는 풀이 정해지지 않음), food 는 restaurant 입구 풀이 굶으면 필수 중단 조건 |
| 9 | commerce cart_items 시퀀스 되감기 | RevenueCat 2022-11-23 공식 | commerce 장바구니 표, Q | 버림: 카탈로그 §1. F57-P 와 같은 DB 이전 이야기에 표만 바꾼 꼴 |
| 10 | 질의 계획 변경으로 큰 표 전수 스캔 | Neon 2025-05-16 공식 | food MySQL 표, F | 버림: 원칙 1, 4. 원본이 계획이 바뀐 계기를 밝히지 않고, MySQL 통계를 손으로 바꾸는 주입은 인공적 |
| 11 | food payment 외부 결제 자격 증명 회전 실수 | Cloudflare R2 2025-03-21 | food payment, O | 버림: PG mock 에 인증이 없어 인증 경로 신설 필요(rejected 302 행과 같음) |
| 12 | banking ledger 설정 배포 | (여러 원본) | banking ledger, G | 버림: 원칙 7. ledger 는 Kafka 소비자와 배치뿐이라 사용자 경로 밖 |
