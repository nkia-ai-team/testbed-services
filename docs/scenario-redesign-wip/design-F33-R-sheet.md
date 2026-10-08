---
title: F33-R 설계 시트 (food dispatches 인덱스 제거로 주문마다 도는 배차 COUNT 가 전수 스캔)
status: Draft
owner: project
last_reviewed: 2026-10-08
tags:
  - scenario
  - design
  - slow-query
  - schema-change
summary: 음식배달 MySQL 의 dispatches 테이블에서 인덱스 idx_dispatches_status_assigned 가 스키마 변경으로 제거되어, 주문마다 두 번 도는 배차 수 COUNT 가 약 501만 행 전수 스캔이 되고 MySQL 포화와 dispatch 커넥션 풀 고갈로 주문 생성이 503 으로 실패하는 시나리오. 원본은 Buildkite 2025-11-10 마이그레이션의 인덱스 제거 장애.
---

# F33-R 설계 시트

## 1. 요약

음식배달 배차 서비스(dispatch)는 주문이 들어올 때마다 "지금 배정 중인 배차가 몇 건인가"를 두 번 센다(order 의 용량 조회 한 번,
배차 요청 안에서 한 번). 이 COUNT 는 dispatches 테이블의 복합 인덱스 `idx_dispatches_status_assigned(status, assigned_at)` 덕분에
약 1ms, 약 1,300행만 보고 끝난다. 스키마 변경이 이 인덱스를 지운다. 같은 COUNT 가 이제 표 전체(약 501만 행)를 읽어 한 번에
1.6~5초가 걸린다(시간대별 MySQL 부하에 따라). 그런 COUNT 는 하루 주기로 오르내리는 상주 부하(분당 12~139회)에 동반 부하가 시간대와 무관하게 초당 약 1.4건을 더해 들어온다(30초마다 도는 만료 배치도 같은 인덱스를 잃는다). CPU 0.5 코어짜리 MySQL 이 포화되고, dispatch 커넥션 풀 10개가 전수 스캔에
묶여 용량 조회가 500 이나 타임아웃이 되고, order 는 주문을 저장하기 전에 503 으로 거절한다. 서비스 코드와 설정은 그대로다. 주문 유입은 동반 부하만큼 늘지만(시간대에 따라 약 2~8배), 원인을 가리키는 것은 호출 수가 아니라 호출당 검사 행 수(약 1,300 → 약 500만)이고 부하만으로는 이 값이 바뀌지 않는다(§7, §8).

비유: 도서관이 "대출 중인 책" 칸을 따로 세던 색인 카드를 정리 작업 중에 버렸다. 사서는 여전히 같은 질문("대출 중인 책이 몇 권?")을
받는데, 이제 서고 500만 권을 한 권씩 넘겨 봐야 답할 수 있다. 질문이 늘어난 것도 사서가 게을러진 것도 아니다.

## 2. 원본 사례

| 항목 | 내용 |
|---|---|
| 기업, 날짜 | Buildkite, 2025-11-10 (사후 보고 2025-11-18) |
| 출처 | [공식 상태 페이지 사후 보고](https://www.buildkitestatus.com/incidents/sv6phcn6xwwg) (`ref-real-world-incidents.md` M21 에 추가, 2026-10-08 WebFetch 로 원문 확인) |
| 요약 | 04:48 UTC DB 마이그레이션이 Pipelines annotations 테이블의 인덱스를 제거했다. 대체 인덱스가 있었지만 "a small number of high traffic queries that were not hitting the new index as expected". 04:53 annotation 질의 타임아웃으로 탐지했고, 이 질의들이 "substantially increased CPU load on the impacted databases" 를 일으켜 Agent API, REST API, GraphQL 이 저하됐다. 인덱스를 다시 만들어 복구했는데 진행 중인 annotation 질의가 남은 샤드의 인덱스 생성을 막아, 앱 계정의 테이블 권한을 잠시 회수하고 생성을 마쳤다. 08:43 지연과 오류율 정상, 12:00 종료. 주원인으로 "a database migration was applied to remove an index" 를 들었고(원문 표현), 재발 방지 첫 항목은 인덱스 제거를 위험 마이그레이션 검사에 넣어 미사용 확인을 요구하는 것 |
| 함께 본 사례 | Chargebee 2018-03-02 [공식](https://status.chargebee.com/incidents/pffpjnwyr92p): 배포 중 인덱스 역할을 하던 열의 제약을 제거, 직후 DB CPU 100%, 요청이 쌓여 타임아웃, 재생성에 필요한 메타 잠금을 실행 중 질의 때문에 못 얻어 서비스를 내리고 인덱스 재추가(같은 M21 절에 추가) |

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 마이그레이션(스키마 변경)이 인덱스를 제거 | DDL `ALTER TABLE dispatches DROP INDEX idx_dispatches_status_assigned`(food MySQL) |
| 원인이 된 결함 | 고빈도 질의 일부가 그 인덱스에 기대고 있었는데 제거 후 탈 인덱스가 없음 | 주문마다 두 번 도는 `countByStatus("ASSIGNED")` 가 이 인덱스에 기대고 있었는데 제거 후 전수 스캔 |
| 전파 경로 | 질의 타임아웃 → DB CPU 부하 급증 → 같은 DB 를 쓰는 API 전반 저하 | COUNT 1건당 약 500만 행, 2.5~5초 → MySQL(0.5 CPU) 포화 → dispatch Hikari 풀(10) 고갈, 용량 조회 500/타임아웃 → order 재시도, 서킷브레이커 → 주문 503. 같은 MySQL 을 쓰는 다른 food 조회도 느려짐 |
| 사용자 증상 | Agent API, REST, GraphQL 저하, 웹 UI 오류율 상승 | 주문 생성 실패(503). 식당, 메뉴 조회는 답하되 느려질 수 있음 |
| 원본의 탐지 경로 | annotation 질의 타임아웃 탐지(04:53, 시작 5분 뒤), DB CPU | order 5xx, dispatch Hikari 고갈 ERROR, order 'Failed to check dispatch capacity' ERROR, DPM Top SQL 검사 행 수 급증, DPM 인덱스 수 31→30 |
| 완화와 복구 | 인덱스 재생성(진행 중 질의가 재생성을 막아 권한 회수까지 동원) | cleanup 이 같은 이름과 열로 `CREATE INDEX`. 메타데이터 잠금이 진행 중 스캔을 기다릴 수 있다(원본과 같은 꼴) |

**기전 동일성**: 원본과 재구성 모두 "스키마 변경이 인덱스를 제거 → 그 인덱스에 기대던 고빈도 질의가 전수 스캔 → DB CPU 과부하와
질의 타임아웃 → 그 DB 를 쓰는 기능이 실패 → 인덱스를 다시 만들어 복구"다. 수요도 코드도 그대로이고 바뀐 것은 질의 하나의 실행 비용이다.
다른 점은 둘이다. (1) 원본은 대체 인덱스가 있었는데 일부 질의가 그것을 타지 않았고, 재구성은 대체 인덱스 없이 제거만 한다. 질의가
결국 탈 인덱스를 잃는다는 결과는 같고, 대체 인덱스를 만들어 두는 단계는 재현하지 않는다(원인을 흐리는 단계가 하나 더 생길 뿐 고리는 같다).
(2) 원본의 계기는 배포 파이프라인의 마이그레이션이고, 재구성은 MySQL 클라이언트로 같은 DDL 을 직접 실행한다. 앱 코드나 이미지를 바꾸지 않는다는
하네스 규칙 때문이다. 관제 데이터가 보는 것(인덱스 수와 질의 비용의 변화)은 같다.

## 3. 숫자 근거 (2026-10-08, `scenario-stats.py`, 정식 + 후보 합계 37)

- 장부 묶음별: A 7(18%), B 7(18%), C 6(16%), D 7(18%), E 2, **F 1**, G 3(F32-R 후보 포함), H 2, I 1, L 1(F30-R 후보), J K M N 0
- 정답 위치별: 외부 결제 의존 6, 주문 서비스 6, 결제 경로 합계 10(27%, 결제 쪽 금지), **DB 테이블(배차) 0**
- 서비스별: 쇼핑몰 23, 은행 8, 음식배달 6. 음식배달이 가장 적어 최우선
- 이 후보: 묶음 **F 느린 쿼리**(정식 + 후보 1, 현실 근거는 `ref-real-world-incidents.md` M21 의 공식 사례 셋: GitHub 2026-05-04, Buildkite 2025-11-10, Chargebee 2018-03-02),
  정답 위치 **DB 테이블(배차)**(0, §2-1 의 "DB 테이블(<무엇>)" 꼴), 서비스 **음식배달**. 추가 뒤 F 2/38(5%), DB 테이블(배차) 1/38(3%), 음식배달 7/38.
  어느 축도 20% 에 닿지 않는다
- 묶음 판단: 계기가 변경(DDL)이지만 설정값 배포가 아니라 G 가 아니다. 피해가 MySQL CPU 포화로 번지지만 CPU 한도와 노드 자원은 그대로이고
  바뀐 것은 한 질의가 검사하는 행 수라 A 가 아니다. 원인 정의가 F("인덱스 부재나 비효율 쿼리로 DB 응답이 느려짐")와 그대로 맞는다
- 다른 빈 묶음을 고르지 않은 이유: K 는 네트워크 주입기가 껍데기이고(§5 F13-H, F13-P) 109 클러스터는 flannel 뿐이라 NetworkPolicy 가 집행되지 않는다(2026-10-08 `kubectl get pods -A` 확인, kube-router 없음).
  J 는 결함 이미지가 필요해 앱 코드 변경 금지에 걸린다. M 은 같은 날 Service 삭제 후보가 폐기됐고(같은 원본, 같은 주입 금지) 다른 DNS 주입은 클러스터 공용 CoreDNS 를 건드린다.
  N 은 재시도 증폭을 180rps 안에서 일으킬 지점이 확인되지 않았다(§5 F14-R, F24-Q). 그다음으로 적은 F, I, L(각 1) 중 I 는 food, 은행에 인증 서비스가 없고, L 은 food 에 F30-R 이 막 들어갔다
- 기존 시나리오와의 거리: F 묶음의 유일한 정식 F20-R 은 commerce 에서 질의 모양이 처음부터 인덱스를 탈 수 없고 부하가 계기다. F33-R 은 인덱스를 타던 질의가 스키마 변경으로 인덱스를 잃는다.
  같은 "인덱스 제거" 발상의 F02-P(§5, 배관 부족)는 핫 경로 밖의 인덱스(menus.category_id)라 피해가 나지 않아 막혔다. F33-R 의 인덱스는 주문마다 도는 질의가 쓰므로 그 막힘이 없다.
  서로 다른 테이블과 인덱스라 같은 주입이 아니다(`test_no_two_scenarios_claim_the_same_injection` 통과)
- **번호**: `scenario-stats.py` 가 알려 준 다음 사례군 F33. 원본 사례(Buildkite 2025-11-10)가 새 사례군이라 접미사 R. 인덱스 제거 발상의 옛 번호 F02-R(commerce, catalog 에서 빠짐, executor 계약은 남음)과 F02-P(parked)는 둘 다 쓰고 있어 F02 의 관계 접미사로 붙이지 않았다

## 4. 인과 사슬 (코드와 인프라 위치)

1. 계기: `ALTER TABLE dispatches DROP INDEX idx_dispatches_status_assigned`(인프라 지점: food MySQL `testbed-mysql-0` 의 `fooddelivery.dispatches`.
   2026-10-08 실측 인덱스는 PRIMARY(id), idx_dispatches_order(order_id), idx_dispatches_status_assigned(status, assigned_at) 셋, 약 501만 행(ASSIGNED 1,208, DELIVERED 5,011,733), 데이터 239MB, 인덱스 225MB.
   인덱스 정의는 `food-delivery/db/init.sql:111-131`, 기존 PVC 에는 `food-delivery/k8s/build-and-deploy.sh:145-165` 가 멱등하게 더한다)
2. 결함이 드러나는 질의: `food-delivery/dispatch-service/src/main/java/com/fooddelivery/dispatch/repository/DispatchRepository.java:14 (countByStatus)`.
   부르는 곳은 `DispatchService.java:105-114 (getCapacity)`(order 의 `GET /api/deliveries/capacity`, `DispatchController.java:59-66`)와 `DispatchService.java:51-64 (dispatchCourier)`.
   같은 인덱스를 `DispatchRepository.java:26-29 (findExpiredAssigned)` 의 30초 만료 배치(`WHERE status='ASSIGNED' AND DATE_ADD(...) < NOW()`, status 접두로 좁힘)도 쓴다.
   인덱스가 없으면 `EXPLAIN` 이 `type=ALL, key=NULL, rows≈3,360,127(통계 추정), Using where`(2026-10-08 `IGNORE INDEX` 로 읽기 전용 확인)
3. 포화: MySQL 컨테이너 CPU limit 500m(`food-delivery/k8s/10-mysql.yaml:40-46`), `innodb_buffer_pool_size` 128MB(표와 인덱스 합 464MB 보다 작음).
   dispatch Hikari `maximum-pool-size 10`, `connection-timeout 3000`(`dispatch-service/src/main/resources/application.yml:12-17`). 풀이 차면 `getCapacity` 의 `@Transactional(readOnly)` 가 연결을 3초 기다리다 실패하고,
   dispatch `GlobalExceptionHandler` 는 `ServiceException` 만 다루므로 Spring 기본 500(Whitelabel)으로 답한다
4. 거절: `order-service/.../client/DispatchClient.java:31-50 (checkCapacity)` 가 500 이나 5초 read-timeout 을 받으면 ERROR 'Failed to check dispatch capacity: ...' 후 502, `@Retry(dispatch)` 3회, `@CircuitBreaker(dispatch)` 가 열리면 fallback 503 'Dispatch service unavailable'.
   `OrderService.java:94-112 (createOrder 도메인 검증 3)` 이 주문 저장(115-128) 전에 503 으로 끝낸다
5. 파생: dispatch 의 readiness, liveness 가 `/actuator/health`(DB 확인 포함, `food-delivery/k8s/22-dispatch-deploy.yaml:73-86`)라 풀 고갈이 길어지면 트래픽에서 빠지고(order 는 I/O error) 재시작될 수 있다.
   2026-08-07 인덱스가 없던 시절 실측으로 dispatch `restartCount=10`(커밋 `fd9b7e8`)
6. 회복: cleanup 의 `CREATE INDEX idx_dispatches_status_assigned ON dispatches(status,assigned_at)`. 메타데이터 잠금을 잡을 때 진행 중 스캔이 끝나기를 기다린다

**같은 결함의 실제 이력**: 이 인덱스는 처음엔 없었다. 2026-08-07 dispatches 가 137만 행일 때 같은 COUNT 의 전수 스캔으로 용량 조회가 500 을 내기 시작했고,
F19-P, F19-S 의 통과 창에서 `order_create_5xx_rate` 0.90~0.98 이 실은 이 결함이었다(order 503 트레이스 482건에 payment 스팬 0건, 커밋 `fd9b7e8`, `docs/scenario-redesign-wip/batch-41-live-findings-0807.md:586-598`).
그 뒤 인덱스를 더해 고쳤다. 지금 표는 그때의 약 3.7배다.

## 5. 원인 규정 (원칙 5, 6)

| 칸 | 값 | 근거 |
|---|---|---|
| `root_cause.target_kind` | `database` | 결함은 테이블의 스키마(인덱스 부재)다. F06-H, F01-R 과 같은 표기 |
| `root_cause.target_id` | `food-mysql:fooddelivery.dispatches` | 인덱스를 잃은 곳, 인덱스를 되살려야 고쳐지는 곳. `인스턴스:스키마.테이블` 꼴(헌장 G7 "target_id 와 granularity 는 같은 층위"). 같은 이름의 다른 개체가 없다 |
| `root_cause.trigger_target_id` | 비움(null) | 계기(DDL)가 일어난 곳이 근본과 같은 테이블 |
| `scoring.granularity` / `accept` | `database-relation` / `food-mysql:fooddelivery.dispatches` | 테이블 단위 |
| `scoring.partial` | `food-delivery-dispatch`, `food-dispatch`, `food-mysql` | dispatch 는 느린 질의를 내고 500 을 낸 곳(증상 서비스, 원칙 6 의 B: 요청은 정당), food-mysql 은 인스턴스만 지목한 덜 정밀한 답(헌장 §G7 의 F06-H 규약) |

- 정답은 "dispatches 테이블의 인덱스가 제거되어 배차 COUNT 가 전수 스캔이 됐다"까지다. 인덱스 수(31→30)와 그 COUNT 의 검사 행 수(약 1,300→약 500만)가
  DPM 에 직접 남으므로 관제 AI 가 낼 수 있는 결론이다(원칙 5). 누가 왜 지웠는지는 요구하지 않는다
- 원본 사후 보고도 "인덱스를 제거한 마이그레이션"을 주원인으로 지목했다. 같은 층위다
- "MySQL CPU 가 모자라다"라고 답하면 위치는 인스턴스(partial)이고 기전은 틀리다. "dispatch 가 망가졌다"는 증상 서비스(partial)다

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 | 내용 |
|---|---|---|
| 감지(lucida-next) | 겉 증상 | (1) dispatch ERROR 'HikariPool-1 - Connection is not available, request timed out after 3000ms' 급증(평시 5분 창 중앙 0, p99 7), order ERROR 'Failed to check dispatch capacity' 급증(평시 중앙 0, p99 3): log-anomaly surge 대상(기존 템플릿, 기준선 있음). (2) order APM error_rate 상승. (3) MySQL 활성 세션, 평균 응답 시간, select scan 증가(DPM 지표, 평시 6시간 active_session 중앙 2·p95 7, avg_query_response_time 중앙 12.4ms, select_scan_count 중앙 280). (4) dispatch readiness 실패와 재시작이 생기면 KCM 이벤트 |
| 피해 판정(러너) | 동반 부하(order-surge.js 2rps)의 k6 live 문서 | `loadgen.food_create_status_rate`(business_5xx_rate) ≥ 0.5, `loadgen.transfer_2xx_rate`(business_2xx_rate) < 0.2. 평시 기준선 0.0, 0.917~1.0(아래). 회복은 상주 기준선 문서(`domain: food-delivery`)로 본다 |
| RCA | 녹화 데이터 | §7 표 |

인시던트가 실제로 생기는지는 첫 실행(곧 녹화 실행)의 확인 항목이다(§11, 원칙 7). 5xx 로 실패하고 기존 ERROR 템플릿이 급증하므로 가용성 신호가 강하다.

## 7. 관측 근거 (119 실조회, 2026-10-08 12:30~13:10 UTC)

| 증거 | 표, 칸 | 조회와 결과(평시) | 표본 여부 |
|---|---|---|---|
| 계기: 인덱스 수 | VM `dpm.mysql.database.index_count{db_name="fooddelivery", target_id="c8c558e5-…"}` | `/api/v1/export` 8일 10,683 표본 전부 **31**, 수집 간격 60~61초. 이 값은 MySQL `information_schema.statistics` 의 `COUNT(DISTINCT table_name, index_name)`(PRIMARY 포함)과 같다(같은 날 읽기 전용 조회로 31 확인). 인덱스 하나를 지우면 30 이 된다 | 전수(DPM 폴링) |
| 근본: COUNT 의 검사 행 수 | CH `lucida.dpm_topsql_local`(`sql_id`, `body` JSON 의 `calls`, `avgExecTime`, `rowExamined`, `sqlText`) | `SELECT timestamp, JSONExtractInt(body,'calls'), JSONExtractFloat(body,'avgExecTime'), JSONExtractInt(body,'rowExamined') FROM lucida.dpm_topsql_local WHERE sql_id='4ca1b0a14f48194a5f848faa725ad01487a6d0f9f231421f810e611417447bd0' AND timestamp > now() - INTERVAL 20 MINUTE` → 1분 delta 마다 calls 78~124, avgExecTime 0.4~2.2ms, rowExamined 1,225~1,354(같은 날 12:24~13:24 로 넓히면 calls 4~146, rowExamined 1,071~1,367). sqlText `SELECT COUNT ( d1_0.id ) FROM dispatches d1_0 WHERE d1_0.status = ?`. 인덱스 제거 뒤 같은 sql_id(문장 digest 는 그대로)의 rowExamined 가 약 501만, avgExecTime 이 수 초가 돼야 한다 | 전수(DPM 폴링) |
| 근본: 전수 스캔의 실제 비용 | 위 표, `sql_id='e5ffcbfa…'` | 평시부터 있는 배차 목록 COUNT `SELECT COUNT(d1_0.id) FROM dispatches d1_0 WHERE (? IS NULL OR d1_0.status = ?)`(질의 모양이 인덱스를 못 탐)가 같은 창에서 calls 6~21, rowExamined 5,010,630~5,011,620, avgExecTime 1,662~6,641ms. 같은 표의 전수 스캔 비용이 이 MySQL 에서 호출당 2~6초라는 현장 실측이다. 2026-10-08 13:0x 109 에서 `SELECT COUNT(id) FROM dispatches IGNORE INDEX (idx_dispatches_status_assigned) WHERE status='ASSIGNED'` 를 읽기 전용으로 두 번 재서 2.65초, 4.93초(kubectl exec 왕복 포함), 인덱스 사용 시 0.14초 | 전수 |
| 근본: 만료 배치도 같은 인덱스 | 위 표, `sql_id='b79f4ca8bd6134be14de319d7997a75f9cebee5c2628c8a4dd1f6978c0d7d2c8'` | 최근 1시간(2026-10-08 12:24~13:24) 1분 delta 59개: calls 2~3, rowExamined 1,084~1,383, avgExecTime 1.3~45.7ms. sqlText `SELECT * FROM dispatches WHERE STATUS = ? AND DATE_ADD ( assigned_at , INTERVAL eta_minutes SQL_TSI_MINUTE ) < NOW ( )`. 인덱스 제거 뒤 호출 수는 그대로(30초마다)이고 rowExamined 가 약 500만으로 올라야 한다 | 전수(DPM 폴링) |
| 감별: 부하만으로 풀이 고갈된 평시 창 | 위 표 status COUNT(`4ca1b0a1…`), 5분 합계 | 2026-10-01 14:20~14:45 와 2026-10-08 09:05~09:30(dispatch Hikari 고갈 1,327건, 260건이 난 창): calls 66~693, 호출당 rowExamined 최대 730~1,701, avgExecTime 최대 0.7~16.8ms. 같은 창 index_count 31. 부하로 인한 고갈은 검사 행 수를 바꾸지 않는다 | 전수 |
| 전파: dispatch 풀 고갈 | CH `lucida.lucida_logs_local`(`service_name`, `severity_text`, `body`) | `service_name='food-delivery-dispatch' AND body LIKE 'HikariPool-% - Connection is not available%'` 8일 5분 창 2,029개: 중앙 0, p99 7, 0 아닌 창 44개. 큰 창은 2026-10-01 14:30, 14:35(1,327, 1,015, 같은 때 dispatch 재시작 KCM Killing 14:31)와 2026-10-08 09:15(260, F30-R 실행 중 surge.js 5rps 동반 부하). 같은 서비스 WARN 'DataSource health check failed' 8일 105건 | 전수 |
| 전파: order 용량 확인 실패 | 위 표 `service_name='food-delivery-order' AND body LIKE 'Failed to check dispatch capacity%'` | 같은 창: 중앙 0, p99 3, 최대 147(2026-10-01 14:30). 꼴은 '...: 500 : "<html><body><h1>Whitelabel Error Page...'(8일 373건), '...: I/O error on GET request for "http://testbed-dispatch:8082/api/deliveries/capa...'(113건) | 전수 |
| 피해: 주문과 배차 로그 | 위 표 `body LIKE 'Created order id=%'`(order), `'Dispatched courier=%'`(dispatch) | 'Created order' 5분 창 8일 p10 60, 중앙 218. 장애 시 0 근처가 기대값 | 전수 |
| 감별: 롤아웃 없음 | CH `lucida.kcm_events_local` | food 의 ScalingReplicaSet 은 롤아웃 때만 남는다(2026-10-01 04:05:07 testbed-dispatch 롤아웃 예). 장애 창에 없어야 한다 | 전수 |
| 감별: MySQL 재시작 아님 | 위 표 `object_name LIKE 'testbed-mysql%' AND reason='Created'` | 8일 5회(10-01 06:49, 14:40, 10-04 03:58, 10-06 09:55, 10-08 07:38. 마지막 종료 사유 OOMKilled). 장애 창에 없어야 한다 | 전수 |
| 보조: MySQL 부하 지표 | VM `dpm.mysql.session.active_session`, `dpm.mysql.instance.avg_query_response_time`, `dpm.mysql.sql.select_scan_count`, `dpm.mysql.sql.row_examined_count` | 최근 6시간: active_session 중앙 2·p95 7·최대 12, avg_query_response_time 중앙 12.4·최대 14.3, select_scan_count 중앙 280. 장애 시 오르는 방향 | 전수(DPM 폴링) |
| 보조: 주문 503, 용량 조회 스팬 | CH `lucida.otel_traces_local` | 장애 시 order `POST /api/orders` 503, dispatch `GET /api/deliveries/capacity` 500 과 긴 지속 시간 | **10% 표본, 보조로만** |
| 감지기 동작 | CH `lucida.lucida_events_local` | 최근 8일 food-delivery-order 에서 log-anomaly surge 101, new_template 36, unknown_anomaly 130, trace-anomaly distribution_shift 475(F32-R 설계 시 조회) | 전수 |

핵심 증거(인덱스 수, Top SQL 검사 행 수, dispatch 풀 고갈과 order 용량 확인 실패 로그, 주문 로그 정지, 롤아웃과 MySQL 재시작 부재)는 모두 전수 수집 데이터다.
트레이스와 APM 지표는 보조로만 쓴다. 장애 시 형태는 첫 실행에서 다시 확인한다.

## 8. 감별

- must_support: index_count 31→30(cleanup 뒤 31), status COUNT 와 만료 배치 질의의 호출당 rowExamined 약 1,300 → 약 500만과 avgExecTime 수 초(판단 근거는 호출 수가 아니라 호출당 검사 행 수),
  dispatch Hikari 고갈과 'DataSource health check failed', order 'Failed to check dispatch capacity' 급증, 'Created order'·'Dispatched courier' 급감, 주문 5xx 0.5 이상과 2xx 0.2 미만,
  롤아웃 없음, MySQL Ready·재시작 없음, MySQL 세션과 응답 시간 상승
- must_rule_out:
  - 배차 한도 설정 오배포(F32-R): dispatch 롤아웃과 새 ReplicaSet 이 없고 'Order rejected: courier pool exhausted' 가 없다. 용량 조회가 200 'available 0' 이 아니라 500, 타임아웃이다
  - MySQL 다운, 재시작(F25-H 꼴): MySQL Ready, KCM Created/OOMKilled 없음, 'Communications link failure' 가 아니라 풀 대기 타임아웃이다
  - 부하 증가, 자원 한도 변경: 동반 부하로 주문 유입은 시간대에 따라 약 2~8배가 되고 status COUNT 호출 수도 늘지만, 같은 유입에서 status COUNT 와 만료 배치의 호출당 검사 행 수가 약 1,300 에서 약 500만으로 뛴다(호출 수가 아니라 검사 행 수). 만료 배치도 호출 수가 늘지 않는데(분당 2회 이하, 보조 증거) 같이 뛴다. 평시 목록 COUNT 가 몰려 풀이 고갈된 창(2026-10-01 14:30 은 목록 COUNT 5분당 434~596회 폭주에 14:40 MySQL 재시작이 겹친 창, 2026-10-08 09:15 는 F30-R 의 surge.js 5rps 가 목록 조회를 늘린 창)은 index_count 31, status COUNT 호출당 rowExamined 최대 1,701 이었다(§7). StatefulSet 스펙과 노드 자원도 그대로다
  - 평시부터 있던 목록 COUNT 의 전수 스캔: 그 질의는 호출당 rowExamined 가 그대로이고(대기 때문에 실행 시간만 길어질 수 있다), 새로 호출당 수백만 행을 읽는 것은 이 인덱스를 쓰던 `status = ?` COUNT 와 만료 배치 질의다
  - dispatch 앱 결함, 프로브 오설정(F05-H 꼴): 롤아웃과 설정 변경이 없고, 재시작이 있다면 풀 고갈과 health 실패 뒤에 온다
- contrast_with: F32-R(같은 용량 조회 경로, 원인은 dispatch 한도 설정), F20-R(같은 F 묶음, 질의 모양 + 부하), F25-H(공유 DB 프로세스 사망), F10-H(같은 food MySQL, 노드 디스크 IO)
- 같은 주입 중복: db.ddl 을 쓰는 다른 ready 시나리오 없음(F02-P 는 parked, menus 인덱스)

## 9. 러너 판정과 강도, 부하 계산

- 진행 대본: 설계 강도 하나로 고정한 evaluation(`approved-fixed-f33-r`, `profile.kind: fixed`, escalate 없음). 첫 실행이 곧 녹화 실행이다(`spec-scenario-lifecycle.md` §2). 강도 = 인덱스 하나 제거(크기 조절 없음)
- **상주 기준선은 하루 주기다**: tb-runner loadgen-food 는 `PEAK_RPS=6`, `TROUGH_RPS=1`(`food-delivery/loadgen/entrypoint.sh:11-41` 시간대 표). KST 2~6시 1 iter/s, 0·1·7·8시 2, 9·23시 3, 10·11·21·22시 4, 14~18시 6.
  DPM 의 status COUNT 호출 수와 평시 목록 COUNT(전수 스캔) 비용도 이를 따른다(최근 2일, KST 시간대별):

  | KST | status COUNT 분당 | 목록 COUNT 분당 | 목록 COUNT 1회(전수 스캔) 평균 |
  |---|---|---|---|
  | 2~6시 | 12~43(4~6시는 Top SQL 상위에서 빠질 만큼 적음) | 6~8 | 1.6~1.8초 |
  | 7~9시 | 47~58 | 6~22 | 1.7~2.1초 |
  | 10~13시 | 80~117 | 11~15 | 2.4~3.5초 |
  | 14~19시 | 127~139 | 17~19 | 4.2~5.3초 |
  | 20~23시 | 83~117 | 11~16 | 2.6~3.8초 |

- **동반 부하가 강도 바닥이다**(2회차 평가 차단 반영): 상주 부하만이면 KST 2~6시에는 status COUNT 가 초당 약 0.2~0.7건, 전수 스캔 1회 약 1.6초라 필요한 스캔 시간이 초당 0.3~1.1초다.
  0.5 코어 MySQL 의 처리 한도 근처이거나 아래라 주문이 느려질 뿐(용량 확인 1.6초 < order read-timeout 5초) 5xx 가 나지 않을 수 있다. 운영 주기가 주입을 미루는 시간은 KST 0~2시뿐이라 이 시간대를 피할 수 없다(러너와 큐에 시간대 제한 기능이 없다).
  그래서 `order-surge.js`(주문 75 / browsing 10 / 검색 5 / 추적 10)를 2rps 로 동반한다. 주문 여정 초당 1.5건 중 닫힌 식당 거절을 빼고 용량 확인이 초당 약 1.4건, 실패하면 재시도 3회라 초당 최대 약 4건이 시간대와 무관하게 더해진다.
  가장 한가한 시간대도 COUNT 수요가 초당 약 1.6건 이상 × 1.6초 = 초당 2.5초 이상의 스캔 시간으로 0.5 코어의 수 배라 포화되고, 바쁜 시간대(14~19시)는 초당 약 3.7건 × 4~5초다.
  포화되면 dispatch 풀 10개가 막혀 나머지 요청은 3초 뒤 500, order read-timeout 5초, 재시도 3회, CB(창 10, 실패율 50%)가 열리면 즉시 503 이다.
  동반 부하는 주문 유입을 늘리지만(기준선 주문 초당 0.2~1.2건 대비 약 2~8배), 원인 판단은 호출당 검사 행 수로 하므로 경쟁 가설을 만들지 않는다(§8). 목록 조회는 초당 0.1건만 더한다(surge.js 는 7.5% 라 쓰지 않는다)
- 강도 근거는 위 계산과 첫 실행 실측이다. 2026-08-07(137만 행, 인덱스 없음)의 `order_create_5xx_rate` 0.90~0.98 은 F19-P, F19-S 의 surge.js 동반 부하가 돌던 창의 값이라(커밋 `fd9b7e8`, `batch-41-live-findings-0807.md:575-598`) 같은 결함이 실제로 5xx 를 냈다는 선례로만 쓴다
- success(all, 3틱): 동반 부하 k6 live 문서의 `order_create_5xx_rate`(business_5xx_rate) ≥ 0.5, `order_create_2xx_rate`(business_2xx_rate) < 0.2. 주문 표본은 30초당 약 40건으로 시간대와 무관하다.
  평시(상주 기준선 문서 `/tmp/rca-baseline-food-delivery-live.json`, 2026-10-08 13:01~13:04 KST 22시, 30초 간격 6회): business_5xx_rate 0.0×6, business_2xx_rate 0.917~1.0, business_4xx_rate 0~0.083(닫힌 식당 400)
- must_rule_out(any, 2틱): 동반 부하 `achieved_rps < 0.5`(설계 2rps 의 1/4. order-surge.js 는 ramping-arrival-rate, maxVUs 기본 300 이라 실패 여정이 재시도로 약 15초 걸려도 동시 VU 약 30 으로 반복률이 유지된다. 부하가 실제로 끊겼을 때만 발화), `pod_ready(testbed-mysql) == false`(food DB 재시작. 8일 5회 OOMKilled 로 가장 잦은 환경 교란이고, 주문과 배차를 함께 멈춰 같은 증상을 낸다).
  상주 기준선의 반복률은 배제 조건으로 쓰지 않는다: 하루 주기로 1~6 iter/s 를 오가고, script.js 는 `maxVUs: 30` 에 반복마다 `sleep(0~1)` 이라 반복 평균 시간이 30/목표치 초를 넘으면 반복률이 목표 아래로 떨어진다(포화 중 주문 여정이 길어지면 그렇게 될 수 있다).
  F32-R 의 `read_step_rate ≥ 0.1` 도 두지 않는다: 포화된 MySQL 을 함께 쓰는 메뉴 조회가 느려지거나 일부 실패하는 것이 정상 전파다
- 러너는 동반 부하를 primary 직전에 함께 시작하고(`production_runtime.py` apply 순서 companion → primary) cleanup 은 역순이라, 주입 전 녹화 구간에는 동반 부하가 없다.
  주입 중 주문이 거의 성공하지 못해 ASSIGNED 는 만료 배치만큼 줄어든다(평시 1,208, 한도 2000 에 닿지 않아 F32-R 꼴의 자연 포화가 끼지 않음)
- 러너가 직접 못 보는 것: 러너 관측 허용 목록에 food dispatch 와 MySQL 질의 지표가 없다. 그래서 인덱스 수, Top SQL, dispatch 풀 고갈은 러너 판정이 아니라 녹화 데이터의 RCA 증거(§7, 정답지 must_support)로 둔다. 러너 변경은 없다
- **운영 메모(녹화 검증 때 필수 확인)**: 러너 success 는 "인덱스 제거로 인한 포화"와 "다른 이유의 dispatch 불능"을 구별하지 못한다. 검증 하네스는 녹화본에서 (1) `dpm.mysql.database.index_count{db_name="fooddelivery"}` 가 주입 시각에 30 으로 내려갔고 (2) Top SQL `sql_id 4ca1b0a1…`(status COUNT)와 `b79f4ca8…`(만료 배치)의 호출당 rowExamined 가 수백만으로 올랐는지를 본다. 판단은 호출당 rowExamined 로 한다: status COUNT 호출 수는 동반 부하만큼 늘 수 있어(포화 중 완료 호출 분당 약 20~37회가 KST 2~6시 평시 12~43회보다 클 수 있다) 기준이 아니고, 만료 배치 호출 수는 늘지 않는다(분당 2회 이하, 보조 증거). (3) 'Order rejected: courier pool exhausted' 와 MySQL Created 이벤트가 없고 (4) cleanup 뒤 `index_count` 가 31 로 돌아왔는지를 확인한다. (4)가 아니면 인덱스가 없는 채로 남은 것이라 다음 무인 실행 전에 §11 의 수동 복구가 먼저다
- recovery: target 200, `pod_ready(testbed-mysql)` true, 상주 기준선 문서의 2xx ≥ 0.7, timeout 10m. 인덱스 재생성 뒤 dispatch 가 재시작 백오프 중이면 회복이 수 분 늦을 수 있어 10분을 둔다
- cleanup timeout 15m(executor 프로세스 상한 900초와 같음): `CREATE INDEX` 는 진행 중 스캔이 끝나야 메타데이터 잠금을 잡고, 501만 행 정렬을 0.5 코어에서 한다. 2026-08-07 137만 행 생성은 "초 단위" 예상이었으므로 몇 분 안을 예상하나 실측은 첫 실행에서 한다
- 부하 상한: target_rps 2 는 상한 180 의 1%

## 10. 설계 원칙 §5 점검표 (각 칸의 근거는 2026-10-08 이 세션에서 실행한 조회와 파일 확인)

- [x] 원본 실제 사례와 대응표, 기전 동일: Buildkite 2025-11-10 공식 상태 페이지 사후 보고를 WebFetch 로 열어 근본 원인("a database migration was applied to remove an index", 원문 표현), 고빈도 질의가 인덱스를 못 탐, DB CPU 부하, 인덱스 재생성 복구를 확인했다. 대응표와 차이 둘은 §2
- [x] 분류 장부 갱신과 숫자: 장부 §4-1 에 F33-R(F, DB 테이블(배차)), §6 기록 한 줄. `scenario-stats.py` 결과는 §3(F 1→2/38 5%, DB 테이블(배차) 0→1/38 3%, 결제 경로 합계 10 그대로이고 이 후보는 결제 쪽이 아님, 음식배달 6→7). 어느 축도 20% 미만
- [x] 근본 원인 위치: `DispatchRepository.java:14`, `DispatchService.java:51-64`, `:105-114`, `DispatchController.java:59-66`, `init.sql:111-131`, `build-and-deploy.sh:145-165` 를 열어 심볼 확인(검사 `test_code_anchors_still_name_a_symbol_that_lives_there` 통과). 인프라 지점은 109 에서 읽기 전용으로 본 인덱스 셋, 행 수, 표 크기, 버퍼 풀(§4)
- [x] 근본 원인 흔적 119 조회: VM `dpm.mysql.database.index_count`(8일 31 고정, §7 첫 행), CH `dpm_topsql_local` 의 status COUNT digest(rowExamined 약 1,300, §7 둘째 행)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: 계기와 근본은 DPM 폴링 지표와 Top SQL, 전파와 피해는 로그(모두 전수). 트레이스와 APM 은 보조(§7 오른쪽 칸)
- [x] 계기의 흔적: 인덱스 제거는 `index_count` 31→30 이라는 상태 변화로 남는다(60초 간격, 8일 동안 한 번도 바뀌지 않은 값). 인공 지연 없음, 주입은 실제 스키마 변경 하나
- [x] 정답이 관제 데이터로 낼 수 있는 결론: 인덱스 수 변화와 같은 분의 질의 비용 급변이 DPM 에 직접 남는다. 코드 설계 결함 추론이 필요 없다(§5)
- [x] 정답지 세 칸: 근본 `food-mysql:fooddelivery.dispatches`(database), 계기 null(같은 곳), 부분 점수 `food-delivery-dispatch`, `food-dispatch`, `food-mysql`. 근본은 부분 점수에 없다(검사 `test_written_keys_are_machine_matchable` 통과)
- [x] 감지, 피해 판정, RCA 증거 구분: §6 표. 피해 판정 값(business_5xx_rate)은 평시 6회 실측 모두 0.0
- [x] 주입이 시나리오 id 를 남기지 않음: DDL 은 `ALTER TABLE dispatches DROP INDEX idx_dispatches_status_assigned`, 역 DDL 은 같은 이름의 `CREATE INDEX` 뿐이다. MySQL 클라이언트는 러너 컨테이너의 `kubectl exec` 로 root 접속하며 세션 이름이나 태그를 붙이지 않는다(PostgreSQL 쪽 F02-R 계약의 `application_name` 같은 칸이 MySQL 경로에는 없다). 사전 확인과 recovery 의 행 수 검사는 `SELECT COUNT(*) >= 1000 FROM (SELECT 1 FROM dispatches LIMIT 1000) t` 로 1,000행만 읽어 녹화 구간에 무거운 질의를 남기지 않는다(평시 Top SQL 에 없는 digest 이므로 녹화 검증에서 주입 도구의 사전/사후 확인으로 표시한다). k6 의 `--tag scenario_id=F33-R` 는 tb-runner 로컬 지표 태그로 HTTP 요청에 실리지 않는다(기존 모든 시나리오와 같음)
- [x] 부하 상한과 서킷브레이커를 넣은 피해 계산: §9(하루 주기 기준선 1~6 iter/s 의 가장 한가한 시간대에도 동반 부하로 COUNT 수요 초당 약 1.6건 이상 × 1.6초 이상 대 MySQL 0.5 코어, 풀 10, CB 가 열려도 반개방 호출마다 전수 스캔. 2026-08-07 선례는 동반 부하가 있던 창이라 선례로만 씀)

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- **index_count 가 30 으로 내려갔다가 31 로 돌아오는가**: VM `dpm.mysql.database.index_count{db_name="fooddelivery"}` 를 주입 창 앞뒤로 본다. 60초 폴링이라 주입 1분 안에 바뀌어야 한다
- **Top SQL 이 바뀌는가**: `dpm_topsql_local` 의 `sql_id='4ca1b0a1…'` 가 rowExamined 수백만으로 오르는가. 주입 뒤 digest 가 같은 sql_id 로 남는지(문장 모양은 같으므로 같아야 한다)
- **DDL 자체가 Top SQL 에 남는가**: `ALTER TABLE dispatches DROP INDEX` 가 digest 로 잡히는지는 모른다(평시 8일 Top SQL 에 ALTER, CREATE INDEX, DROP 문장은 0건). 남으면 계기의 직접 증거가 하나 더 생기고, 없어도 index_count 가 계기 흔적이다
- **피해 형태**: order 5xx 가 0.5 이상으로 3틱 유지되는가, dispatch 가 재시작하는가(KCM Unhealthy/Killing), 메뉴 조회가 얼마나 느려지는가
- **cleanup 시간**: `CREATE INDEX` 가 몇 분 걸리는가. 러너 cleanup 순서가 primary 먼저(`production_runtime.py:886` `cleanup_order = [primary, *reversed(companions)]`)라 인덱스 재생성은 동반 부하 2rps 와 포화된 MySQL 아래에서 돈다. 소요 시간을 기록하고, 길면 동반 부하를 먼저 멈추는 방안(순서 변경 또는 hold 단축)을 검토한다. 재 보지 않았고, 상주 부하가 계속 전수 스캔을 내며 `lock_wait_timeout` 이 31536000(사실상 무제한)이라 메타데이터 잠금 대기에 상한이 없다. 실행기 상한 900초 안에 끝나지 않아 끊기면 인덱스가 없는 채로 남아 다음 무인 실행이 오염된다. 그래서 실행이 끝날 때마다 `index_count` 31 복귀를 확인한다(§9 운영 메모 (4)). 실행기 cleanup 은 `CREATE INDEX` 뒤 정의를 다시 확인하지 않고(확인은 recovery 단계의 `check`), kubectl exec 가 끊겨도 파드 안 DDL 은 계속되거나 되돌려질 수 있으므로 결과는 실행기 종료 코드가 아니라 `index_count` 로 판단한다. 그때는 `food-delivery/k8s/build-and-deploy.sh` 의 멱등 블록(145-165)이나 db.ddl cleanup 을 다시 돌려 되살리고 `index_count` 31 을 확인한다
- **MySQL OOM 과 겹치지 않았는가**: 평시에도 약 2일마다 OOMKilled. 녹화 창에 testbed-mysql Created 가 있으면 그 실행은 녹화로 쓰지 않는다
- **시간대**: 실행 시각(KST)과 그때 상주 기준선 목표치(1~6 iter/s)를 기록한다. 가장 한가한 KST 2~6시 실행에서도 5xx ≥ 0.5 가 3틱 이어졌는지가 §9 강도 바닥 계산의 실측 확인이다
