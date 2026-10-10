---
title: F59-R 설계 시트 (내부 백필 Job 이 food 배달 목록을 깊은 페이지까지 읽어 dispatch 풀이 묶이고 주문 503)
status: Draft
owner: project
last_reviewed: 2026-10-10
tags:
  - scenario
  - design
  - batch
  - mysql
summary: 알림 쪽 보관소 백필 Job 이 워커 12개로 dispatch 배달 목록 API 를 offset 페이지로 끝까지 읽어, 깊은 페이지마다 MySQL 이 앞선 끝난 배달을 모두 읽고 그 호출이 dispatch Hikari 연결을 모두 쥐어 food MySQL 이 포화되고 주문이 503 이 되는 시나리오. 원본은 Atlassian Bitbucket 2025-05-08(내부 서비스의 대규모 백필 작업이 API 엔드포인트를 거쳐 비싼 질의로 주 DB 를 압박).
---

# F59-R 설계 시트

## 1. 요약

알림 쪽이 알림 보관소를 채우려고 일회성 백필 Job `notify-delivery-backfill` 을 rca-testbed-food 에 띄운다. notify-service 이미지에서 bash 와 curl 로 도는 스크립트가 워커 12개로 dispatch 배달 목록 `GET /api/deliveries?status=DELIVERED&page=N&size=500` 을 페이지마다 끝까지 읽는다. 목록은 정렬 키 없이 offset 으로 페이지를 나누므로 페이지 N 은 `LIMIT N*500, 500` 이 되고, MySQL 은 끝난 배달 약 173만 항목의 앞에서부터 앞선 행을 모두 읽은 뒤에야 500 행을 돌려준다. 워커 12개의 깊은 페이지 호출이 dispatch Hikari 연결 10개를 모두 쥐고 food MySQL(CPU 0.5)을 포화시켜, 주문마다 필요한 용량 확인과 배차 요청이 연결을 기다리다 실패하고 주문이 503 이 된다. dispatch, order, MySQL 의 코드, 설정, 스키마, 인덱스는 하나도 바뀌지 않는다.

비유: 도서관 대출 창구(dispatch)에 사서가 열 명 있다. 문서 보관팀이 "옛 대출 기록 전부 복사"를 열두 명에게 시켜 창구에 보냈는데, 기록이 번호순 색인 없이 쌓여 있어 "3,000번째 묶음"을 달라고 하면 사서가 앞의 2,999 묶음을 한 장씩 넘겨야 한다. 사서 열 명이 모두 넘기기에 매달리자, 책을 빌리러 온 손님(주문)은 줄을 서다 돌아간다. 사서도 책도 멀쩡하고, 바뀐 것은 창구에 온 복사 요청뿐이다.

## 2. 원본 사례

- 기업: Atlassian Bitbucket Cloud
- 날짜: 2025-05-08 15:26 UTC 시작(사후 보고 게시 2025-05-19 16:43 UTC), 2025-05-09 11:19 UTC 완전 정상
- 링크: [공식 상태 페이지 사후 보고](https://bitbucket.status.atlassian.com/incidents/z1lk6hmkp9gt) "Bitbucket has degraded performance"
- 요약(원문 인용 중심): "The event was caused by a backfill job running from an internal Atlassian service, which triggered an excessive call volume of expensive queries and pressure on database resources." 근본 원인 절: "an internal high-scale backfill job that triggered excessive load on certain API endpoints, which eventually impacted the database through resource-intensive queries and operations. This led to additional load from retries by dependent services, increasing the total recovery time." 주 DB 가 자동 장애 조치되어 15분 만에 회복했고 백필 작업을 멈췄으나, 그 뒤 하류 서비스의 밀린 재시도가 DB 성능에 계속 영향을 줌. 재발 방지: 읽기를 복제본으로, 자원 소모가 큰 내부 API 엔드포인트의 요청 한도 조정, 질의 최적화, 하류 재시도 정책 조정.
- `docs/ref-real-world-incidents.md` M20 에 이 사례를 추가했다(출처가 말한 사실만).
- 원본이 적지 않은 것: 작업 이름, 엔드포인트, 페이지 방식, DB 종류. 재구성의 "offset 페이지 목록을 깊게 읽음"은 원본이 말한 "API 엔드포인트를 거친 자원 소모가 큰 질의"의 구체화이고, 이 테스트베드에서 실제로 비싼 API 질의가 그것이다(§4).

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 내부 서비스에서 대규모 백필 작업이 돌기 시작 | 알림 쪽 보관소 백필 Job `notify-delivery-backfill` 시작(notify 이미지, 워커 12) |
| 원인이 된 결함 | 작업이 특정 API 엔드포인트에 과도한 호출을 보내고, 그 엔드포인트의 질의가 자원 소모가 큼 | 작업이 dispatch 배달 목록 API 를 깊은 offset 페이지로 병렬 호출. 그 질의가 페이지마다 앞선 끝난 배달을 모두 읽음(호출당 0.5~6초) |
| 전파 경로 | API 엔드포인트 → 비싼 질의 → 주 DB 자원 압박 → 웹과 API 전반 지연, 간헐 오류. 하류 재시도가 부하를 더함 | dispatch 목록 → MySQL 깊은 읽기 → dispatch Hikari 10 고갈과 food MySQL 포화 → 용량 확인과 배차 요청 연결 대기 실패 → order 재시도와 서킷 열림 → 주문 503. 백필 워커도 실패한 페이지를 다시 부름 |
| 사용자 증상 | 웹사이트, API 지연과 간헐 오류 | 새 주문 대부분 503, 배달 추적 목록과 상세 실패, 가게 둘러보기 느려짐 |
| 탐지된 경로 | 실시간 감시가 즉시 탐지 | lucida 이상 탐지(order, dispatch 로그 이상, MySQL 지표 수준 이동), 인시던트 |
| 완화와 복구 | 백필 작업 중지, DB 부하 덜기, 장애 조치 | cleanup 이 Job 을 지움. 진행 중이던 깊은 질의가 1분 안팎에 끝나며 풀이 풀림 |

기전 확인: 원인(내부 작업이 비싼 API 질의를 과도하게 부름)에서 증상(그 API 와 같은 DB 를 쓰는 사용자 기능의 지연과 오류)까지 고리가 원본과 같다. 서비스나 DB 의 결함, 설정 변경은 원본에도 재구성에도 없다.

## 3. 숫자 근거 (2026-10-10, `scenario-stats.py`, 정식 + 후보 합계 76, 이 후보 전)

| 축 | 값 | 판단 |
|---|---|---|
| 서비스 | commerce 27, core-banking 25, food-delivery 24 | 음식배달이 가장 적어 우선 |
| 묶음 | A 7, B 7, C 6, D 7, E 2, F 3, G 8, H 2, I 1, J 11, K 2, L 7, M 1, O 1, P 10, Q 1 (N 0) | 주 묶음 E 는 2(2.6%) → 3(3.9%). 보조 F 3 → 4 로 세도 5.2%. 20% 에 멀다 |
| 정답 위치 | 주문 서비스 8(10%)이 최다. "내부 배치 작업" 0 | 새 정답 위치 0 → 1. §2-1 에 정의를 더했다 |
| 결제 경로 합계 | 12(15.8%) | 이 후보는 결제 경로가 아니라 그대로 12/77(15.6%) |
| 부품 지도 | food: mysql 8, dispatch 7, order 4, restaurant 3, payment 3, kafka 0, notify 0 | 증상 부품(dispatch, MySQL)은 많지만 정답은 그 밖의 "배치와 크론" 인프라 층(0) |

왜 이 후보인가: 서비스 최소(음식배달), 묶음 E 가 낮고(2), 정답 위치가 처음 나오는 부품이며, 현실 근거가 공식 사후 보고다. 관제 AI 가 "dispatch 풀 고갈과 MySQL 포화면 인덱스나 dispatch 릴리스" 라고 외워 찍는 지름길(F33-R, F33-H)을 막는 H 짝이기도 하다: 같은 증상에서 정답이 클러스터 안의 새 작업이다.

### 3단계 후보 목록 (실제 기전 × 부품, 순서는 숫자 순)

| # | 후보 | 원본 | 판단 |
|---|---|---|---|
| 1 | 내부 백필 Job 이 food dispatch 목록 API 를 깊은 offset 페이지로 병렬 호출(k8s.job) | Atlassian Bitbucket 2025-05-08 공식 | **채택**. food 최소, E 2, 정답 위치 0 |
| 2 | 같은 원본으로 banking transfer 이체 내역 API 를 백필이 깊게 호출 | 같음 | 보류. 은행 25 로 food 보다 많고 같은 원본은 한 번만 쓴다 |
| 3 | banking api → transfer 읽기 시간 제한을 초 단위로 적은 "3" 이 Spring Duration 에서 3ms 로 읽히는 설정 배포 + 재시도 3회 | Flagsmith 2022-08-18 공식(Node 클라이언트가 초 단위 값을 ms 로 넘겨 3ms 시간 초과와 재시도 3회, 비싼 엔드포인트로 DB 과부하) | 버림(원칙 1, 카탈로그 §1): 원본 고리의 "재시도 증폭 → 공유 DB 과부하"가 api transferClient 서킷브레이커(최소 5회, 50%, 5초)에 막혀 재현되지 않고, 남는 관측 꼴(클라이언트 시간 초과 502)이 F08-P(read-timeout 20ms 설정 배포)와 같다 |
| 4 | food payment 릴리스가 인덱스 없는 중복 결제 확인 질의를 더해 MySQL 포화 | ImprovMX 2025-10-08 공식 | 순위 낮음: J 가 14% 로 가장 많은 묶음이고 같은 꼴(F42-R, F42-P, F33-H)이 이미 셋, 결제 경로 13/77 |
| 5 | food 파드 dnsConfig 가 사라진 resolver 를 가리키는 배포 | Let's Encrypt 2025-07-21 공식 | 버림(원칙 9): food 서비스는 모두 기동 때 MySQL 이름을 풀어야 해 새 파드가 Ready 가 못 되고, 기본 롤링(maxSurge 25%)이 옛 파드를 남겨 피해가 없다. DB 를 쓰지 않는 banking api 판은 F37-R 과 같은 수단과 대상 |
| 6 | food dispatch 만료 배치를 멈추게 하는 독 데이터 행 | Healthchecks.io 2020-02-07(불가능한 cron 식으로 송신기 멈춤) | 버림(원칙 1): 원문 미확인, 독 행을 넣는 길이 이번 실행에서 막힌 직접 SQL 값 오염(Clerk 2025-02-06)과 같다 |
| 7 | tb-w3 호스트 방화벽이 dispatch → MySQL 3306 을 빠뜨림 | Snowflake 2025-11-20(F44-P 보조 원본) | 버림(카탈로그 §1): 수단 host.firewall 과 노드가 F44-R 과 같고 대상 포트만 바뀜 |
| 8 | 배포가 가리킨 이미지가 저장소에 없음 | GitHub 2025-02-03 월간 보고 | 버림(같은 주입): F17-H(k8s.image 없는 태그)와 같다 |
| 9 | food notify 소비자 워커 풀이 피크에 모자라 알림 지연 | GitHub 2025-02-25 월간 보고 | 버림(원칙 7): 사용자 경로 밖 비동기 소비자 |
| 10 | food payments 표 열 추가가 느린 SELECT 뒤 메타데이터 잠금을 기다리며 결제 쓰기를 막음 | Honeycomb 2024-08-06 공식 | 버림(컨트롤러 필수 중단 조건): payment 대기 10초 동안 order 가 트랜잭션 연결을 쥐어 order 풀 고갈, readiness 이탈로 entry 0(rejected 2026-10-08 행과 같은 벽) |

부품: dispatch 목록, banking transfer, banking api, food payment, 파드 DNS, dispatch 배치, 노드 방화벽, 이미지, notify 소비자, MySQL 표(9개, 4개 이상 충족).

## 4. 인과 사슬 (코드와 인프라 위치)

1. 계기: Job `notify-delivery-backfill` 생성(`scripts/scenarios/profiles/k8s_job_executor.py:33-49 CONTRACTS`, `:53-72 BACKFILL_SCRIPT`). 워커 w 는 페이지 290w ~ 290(w+1)-1 을 차례로 `curl --max-time 60` 으로 부르고, 200 이 아니면 2초 뒤 같은 페이지를 다시 부른다.
2. 요청: `food-delivery/dispatch-service/src/main/java/com/fooddelivery/dispatch/controller/DispatchController.java:31-40 (getDeliveries)` 가 page, size 를 받아 정렬 없는 `PageRequest.of(page, size)` 를 만든다. page 상한이 없다.
3. 질의: `DispatchService.java:87-91 (searchDispatches)` → `DispatchRepository.findByStatus(status, pageable)`(Slice). Hibernate 가 `select ... from dispatches d1_0 where d1_0.status=? limit ?,?` 를 낸다(109 MySQL performance_schema digest `... WHERE d1_0.status = ? LIMIT ?, ...` 확인, 0쪽은 `LIMIT ?`).
4. DB: MySQL 이 `idx_dispatches_status_assigned`(status, assigned_at) 의 DELIVERED 범위 앞에서부터 offset + 500 항목을 읽고 각 행을 클러스터 인덱스에서 가져온다(EXPLAIN: ref, rows 867,488 추정). 109 실측: offset 20만 0.53초(Handler_read_next 200,199), offset 100만 6.3초(버퍼 풀 128MB 를 넘어 Innodb_buffer_pool_reads 증가).
5. 풀: `dispatch-service/src/main/resources/application.yml:13-15 (Hikari maximum-pool-size 10, connection-timeout 3000)`. 백필 호출 12개가 연결 10개를 모두 쥐면 나머지 요청은 3초 뒤 `Connection is not available, request timed out after 3000ms` 로 500.
6. 전파: `order-service/.../client/DispatchClient.java:32-34 (checkCapacity, 서킷브레이커와 재시도 3회)`, read-timeout 5s(`order-service/src/main/resources/application.yml:58`). `OrderService.java:96-111` 이 용량 확인 실패를 'Dispatch capacity check failed, rejecting order' 로 남기고 503 'Dispatch service unreachable'.
7. 부수: dispatch readiness, liveness(`food-delivery/k8s/22-dispatch-deploy.yaml:73-87`, /actuator/health DB 확인, 시간 제한 3초)가 같은 풀을 기다려 실패할 수 있다. NotReady 로 빠지면 백필 호출도 연결 거절로 실패하고, 진행 중이던 질의가 끝나 풀이 풀리면 다시 Ready, 백필이 다시 채운다.

인프라: rca-testbed-food(tb-w3), Job 은 nodeSelector 로 tb-w3, 이미지 food-delivery-notify:latest(tb-w3 containerd 에 있음, notify Deployment 가 imagePullPolicy Never 로 사용 중). food MySQL testbed-mysql-0(CPU 한도 0.5, innodb_buffer_pool_size 128MB, max_connections 151, 8.0.46), dispatches 약 174만 행(데이터 126MB + 인덱스 95MB, 2026-10-10 information_schema).

## 5. 원인 규정 (원칙 5, 6)

- `root_cause.target_id`: `rca-testbed-food/job/notify-delivery-backfill`(target_kind batch_job). 감당할 수 없는 양과 깊이의 호출을 보낸 작업이 원인이다. dispatch 와 MySQL 은 정당한 질의를 처리했을 뿐이고, 같은 질의를 배달 앱이 0쪽으로 부르면 약 2.5ms 다(원칙 6: 요청 A 가 B 를 실행시켰고 C 가 났다면 A).
- `trigger_target_id`: 비움(계기 = 작업 시작 = 근본).
- `scoring.partial`: food-delivery-dispatch(풀이 묶인 곳), food-mysql(포화된 곳), food-delivery-notify(작업이 쓴 이미지의 주인, Deployment 는 그대로), food-delivery-order(증상 서비스).
- 원본 층위: Bitbucket 사후 보고도 원인을 "내부 서비스의 대규모 백필 작업"으로 지목하고 작업을 멈춰 완화했다. "dispatch 목록이 정렬 없이 offset 으로 페이지를 나누는 설계"는 정답이 아니라 피해를 키운 조건이다(원칙 5: 코드 설계 결함 추론을 정답으로 요구하지 않음). 원본도 질의 최적화를 재발 방지 항목의 하나로만 둔다.
- 관제 AI 가 낼 수 있는가: KCM 에 실패 직전 새 Job 과 파드가 있고, DPM Top SQL 에 평시 없던 offset 꼴 목록 문장이 호출당 수 초, 수십만 행으로 나타나며, dispatch 스팬의 호출자가 그 파드(curl)다. 롤아웃, 설정, 인덱스 변화는 없다. 이 넷으로 "새 배치 작업이 dispatch 와 MySQL 을 눌렀다"는 결론에 닿는다.

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 | 기대 |
|---|---|---|
| 감지 | order, dispatch 로그 이상(ERROR, WARN 급증), MySQL stream-anomaly(검사 행, select_scan, 응답 시간 수준 이동), dispatch 프로브 실패 이벤트 | 인시던트 생성. 같은 포화 꼴의 F33-R 녹화에서 food 인시던트 12건 |
| 피해 판정(러너) | 동반 부하 order-surge 의 주문 생성 5xx 비율 ≥ 0.5, 2xx 비율 < 0.2 (3틱) | F33-R 녹화 5xx 0.68~0.88, 2xx 0.09~0.26 |
| RCA 증거 | KCM Job, 파드 이벤트(계기), DPM Top SQL offset 꼴 문장(기전), dispatch, order 로그(전파), dispatch 스팬 url.query 와 client.address(보조) | §7 |

## 7. 관측 근거 표 (119 실조회, 2026-10-10 17:30~17:45 UTC, 평시)

| 증거 | 표, 칸 | 조회 | 평시 결과 |
|---|---|---|---|
| 계기: Job 과 파드 이벤트 | CH `lucida.kcm_events_local`(namespace, object_kind, reason, body) | `SELECT object_kind, reason, count() FROM lucida.kcm_events_local WHERE timestamp > now() - INTERVAL 30 DAY AND namespace='rca-testbed-food' AND (object_kind='Job' OR object_name NOT LIKE 'testbed-%') GROUP BY 1,2` | 0행(30일 동안 food Job 이벤트 없음). 수집 자체는 확인: commerce 의 F03-H Job 에서 Job SuccessfulCreate, Pod Pulled('Container image "grafana/k6:latest" already present on machine'), Created, Started, Killing 이 남음(2026-08-14, 08-25). food 의 Pod Scheduled, Pulled, Created, Started, Killing 은 7일 26~34건 |
| 기전: offset 꼴 목록 문장 | CH `lucida.dpm_topsql_local`(body JSON sqlText, calls, avgExecTime, rowExamined; 분당 상위 약 15문장) | `... WHERE engine='mysql' AND body ILIKE '%courier_id%' AND body ILIKE '%LIMIT%' GROUP BY sqlText`(2일) | 0쪽 꼴 `... WHERE d1_0.status = ? LIMIT ?` 만 96행(1,106 호출). offset 꼴 `LIMIT ?, ?` 는 없다. 109 performance_schema digest 로 offset 꼴이 따로 잡히는 것 확인(설계 측정 4회: 2026-10-10 17:36~17:38 UTC, 직접 SELECT 1회와 API page 1~3 size 5 각 1회, 상위에 들지 못해 Top SQL 에는 없음) |
| 기전: 질의 비용 | 109 MySQL 직접(읽기 전용) | `SELECT ... FROM dispatches d1_0 WHERE d1_0.status='DELIVERED' LIMIT 200000,200` / `LIMIT 1000000,3` | 0.53초(Handler_read_next 200,199) / 6.3초. EXPLAIN ref idx_dispatches_status_assigned rows 867,488 |
| 호출자: dispatch 스팬 | CH `lucida.otel_traces_local` span_attributes `url.query`, `client.address`, `user_agent.original`(10% 표본) | `... service_name='food-delivery-dispatch' AND span_name='GET /api/deliveries' GROUP BY url.query, user_agent`(7일) | `status=DELIVERED&page=0&size=20`, `k6/0.57.0` 4,916건. 그 밖은 설계 측정 1건(page=2&size=5, curl/8.5.0). 평균 2.47ms, p95 3.4ms(6시간 317건) |
| 전파: dispatch, order 로그 | CH `lucida.lucida_logs_local`(전수) | `body ILIKE '%Connection is not available%' OR '%capacity check failed%' OR '%health check failed%'`(1일, dispatch, order) | 0건. F33-R 녹화에서 같은 문구가 5분 창 42, 47(dispatch), 140, 186(order)으로 남았다 |
| 피해 표본: 주문 생성 | CH `lucida.lucida_logs_local` 'Created order' | 시간별(2026-10-10 11~17시 UTC) | 604~2,877 /시간 |
| MySQL 상태 | VM `dpm.mysql.session.active_session`, `dpm.mysql.instance.avg_query_response_time` | 6시간 중앙, 최대 | active_session 중앙 1, 최대 2 / avg_query_response_time 중앙 0.58, 최대 0.6 |
| 배제: 인덱스 그대로 | VM `dpm.mysql.database.index_count{db_name="fooddelivery"}` | F33-R 검증 보고서의 같은 조회 | 31 |

표본 데이터만으로 증명하지 않는다: 계기(KCM 이벤트), 기전(DPM Top SQL), 전파(로그)는 전수 수집이다. 스팬의 호출자 정보는 보조다(백필 호출이 분당 수십~수백이라 10% 표본에도 수십 건이 남을 것으로 본다). 작업 파드 자신의 stdout 은 119 에 수집되지 않는다(kcm_pod_logs_local 0행, OTel 없는 파드).

## 8. 감별

- must_support, must_rule_out: 정답지 그대로(`scenario-metadata.json` F59-R).
- contrast_with:
  - F33-R: 같은 dispatch 풀 고갈, MySQL 포화, 주문 503. 원인이 인덱스 제거라 index_count 31→30, status COUNT 와 만료 스윕이 호출마다 전수. F59-R 은 index_count 31, COUNT 정상, 무거운 것은 offset 꼴 목록 문장, 새 Job 있음.
  - F33-H: 같은 목록 경로. dispatch 릴리스 롤아웃(ScalingReplicaSet, 새 태그 Pulled)이 있고 무거운 문장이 목록 COUNT, 호출자는 평소 배달 앱(k6, page=0). F59-R 은 롤아웃 없음, COUNT 없음, 호출자는 작업 파드(curl, 깊은 page).
  - F42-P: 같은 공유 MySQL 포화, 원인이 restaurant 릴리스.
  - F07-H: 같은 묶음 E(요청이 처리 능력을 넘음)지만 바깥 사용자 트래픽. F59-R 은 클러스터 안 내부 작업이고 진입점 요청 수는 동반 부하만큼만 는다.
  - F32-R: 같은 주문 503 이지만 dispatch 가 200 'available 0' 으로 답함.
- 관계: F59-R 은 새 사례군(F59, 원본 Bitbucket 2025-05-08)의 R. F33 사례군과는 "같은 증상, 다른 원인" 관계라 시트와 정답지 contrast_with 에 적었다.

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

- 강도(고정 1단 `approved-fixed-f59-r`): 워커 12, 페이지 500, 워커당 290 페이지(12 × 290 = 3,480 ≥ 끝난 배달 약 173만 / 500 = 약 3,470), 요청 시간 제한 60초, activeDeadlineSeconds 1800. min_hold 12m, max_injection_duration 25m.
- 풀 계산: 워커 w 의 첫 offset 은 w × 145,000(0 ~ 1,595,000). 한가할 때 offset 20만 0.53초, 100만 6.3초이므로 워커 대부분의 호출이 수 초이고, 열 개가 동시에 돌면 IO 와 CPU(0.5)를 나눠 각각 더 길어진다. 동시 호출 12 > Hikari 10 이라 백필만으로 풀이 늘 차 있다. 주문 수와 무관하게 강도가 워커 수로 정해져 하루 주기 기준선에 덜 좌우된다.
- 피해 계산: 주문마다 용량 확인(→ dispatch 연결 필요)이 3초 연결 대기 끝에 500, order 재시도 3회와 서킷 열림 끝에 503. 같은 dispatch 풀 고갈의 F33-R 녹화가 같은 성공 조건(5xx ≥ 0.5, 2xx < 0.2, 3틱)을 436초에 맞췄다. F33-R 은 주문마다 3.7초 질의 두 번이 풀을 쥐었고(동시 약 5~11), 여기서는 백필 호출이 풀 전부를 쥔다.
- 진입점: order 는 dispatch 실패에 503 을 돌려주고 재시작하지 않는다(F33-R 녹화: entry_status 503, 500, 200 섞임, order liveness 실패 8회지만 재시작 없음). 백필은 진입점을 지나지 않는다. abort 는 entry_status 0(2틱)뿐이다.
- 동반 부하: load.north_south order-surge.js 2rps(F33-R, F58-R 과 같은 값, seed 5959). 주문 표본을 초당 약 1.4건 더한다.
- 러너 판정: 성공 order_create_5xx_rate ≥ 0.5 와 order_create_2xx_rate < 0.2(3틱), 배제 achieved_rps < 0.5, food MySQL 파드 NotReady. 회복 target_health 200, MySQL Ready, 기준선 2xx ≥ 0.7.
- cleanup: Job foreground 삭제 → 파드 종료(terminationGracePeriodSeconds 5, 스크립트가 TERM 에 워커를 모두 끝냄) → 진행 중 질의가 dispatch 쪽에서 끝까지 돈 뒤 연결 반납(최대 1분 안팎) → testbed-dispatch available 확인(상한 300초). 백필은 읽기만 해 남는 데이터가 없다. 받은 페이지는 파드의 emptyDir(상한 512Mi)와 함께 사라진다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Atlassian Bitbucket 2025-05-08, 공식 사후 보고 링크)와 요소별 대응표가 있고, 기전(내부 백필 작업 → API 엔드포인트의 비싼 질의 → 공유 DB 압박 → 사용자 기능 지연과 오류, 재시도가 더함)이 원본과 같다 (원칙 1, §2)
- [x] 분류 장부를 갱신했다: 묶음 E(2→3, 3.9%), 정답 위치 내부 배치 작업(0→1, §2-1 에 정의 추가), 결제 경로 12/77(15.6%), 서비스 음식배달 24→25. 어느 축도 20% 에 닿지 않는다 (원칙 2, §3)
- [x] 근본 원인 위치가 인프라 지점(rca-testbed-food Job notify-delivery-backfill)과 코드 위치(DispatchController.java:31-40, DispatchService.java:87-91, application.yml:13-15, k8s_job_executor.py:53-72)로 확인됐다 (G1, §4)
- [x] 근본 원인의 흔적이 119 에 남는 것을 조회로 확인했다: KCM 이벤트의 Job, Pod 이벤트 수집(commerce Job 실례), DPM Top SQL 의 문장별 호출 수, 시간, 검사 행 (원칙 3, §7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: 계기는 KCM, 기전은 DPM, 전파는 로그(전수). 스팬은 보조 (원칙 3)
- [x] 계기의 흔적(Job 생성, 파드 시작 이벤트)이 남는다. 인공 지연을 쓰지 않는다: 지연은 실제 질의 비용에서 난다 (원칙 4)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다(새 Job + 새 질의 꼴 + 호출자 파드). 목록의 offset 설계 결함 추론을 요구하지 않는다 (원칙 5, §5)
- [x] 정답지 세 칸: 근본 = Job, 계기 비움, 부분 점수 = dispatch, MySQL, notify, order (원칙 6)
- [x] 감지, 피해 판정, RCA 증거를 나눠 적었다 (원칙 7, §6)
- [x] 주입 도구가 시나리오 id 를 남기지 않는다: Job 이름, 레이블(app 하나), 스크립트, 환경 변수, 상태 파일 어디에도 없다(실행기 테스트가 확인). 작업은 실제 회사 이미지(notify)와 평범한 curl 로 돈다 (원칙 8)
- [x] 부하 상한과 서킷브레이커를 넣은 피해 계산이 있다(동시 12 > 풀 10, F33-R 녹화 실측 대비) (원칙 9, §9)

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

1. KCM 에 Job notify-delivery-backfill SuccessfulCreate, 파드 Scheduled, Pulled(food-delivery-notify:latest), Started(backfill)가 남는가. 이미지 Pulled 문구가 notify Deployment 와 같은 이미지라 헷갈리지 않게 파드 이름(notify-delivery-backfill-xxxxx)으로 구분되는가.
2. DPM Top SQL 에 offset 꼴 목록 문장이 상위로 오르고 호출당 rowExamined 가 수십만인가. 0쪽 꼴과 sql_id 가 다른가.
3. dispatch Hikari 고갈 로그와 order 'Failed to check dispatch capacity' 가 Job 시작 1~2분 안에 나오는가. 성공 3틱까지 걸린 시간.
4. dispatch 가 NotReady 나 재시작으로 흔들리는 정도. 재시작이 반복되면 KCM 이 Killing, BackOff 를 남기는데 그래도 정답 경로(Job 이 먼저)가 보이는가.
5. cleanup 뒤 진행 중 질의가 끝나는 시간과 회복 시간(기준선 2xx ≥ 0.7).
6. dispatch 스팬에 client.address 가 백필 파드 IP 로, user_agent 가 curl 로 남는가(표본 수).
