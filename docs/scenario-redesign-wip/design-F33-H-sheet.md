---
title: F33-H 설계 시트 (food dispatch-service 를 배달 목록에 전체 건수를 더한 릴리스로 롤아웃)
status: Draft
owner: project
last_reviewed: 2026-10-09
tags:
  - scenario
  - design
  - release
  - slow-query
summary: food dispatch-service 를 결함 있는 새 릴리스(food-delivery-dispatch:1.6.0, fault-images/f33-h 패치로 만든 별도 태그)로 롤아웃하면, 배달 목록에 전체 건수(X-Total-Count)를 더하느라 목록 쿼리가 Slice 에서 Page 로 바뀌어 요청마다 끝난 배차 약 174만 건을 세는 COUNT 가 붙고, 배달 추적 피크에서 food MySQL(CPU 0.5)이 포화되어 dispatch 연결 풀이 마르고 주문이 503 으로 실패하는 시나리오. 원본은 GitHub 2025-01-09 배포가 들여온 질의가 주 DB 서버를 포화시킨 장애. F33-R(인덱스 제거)과 겉 증상이 같고 정답이 다른 H 다.
---

# F33-H 설계 시트

## 1. 요약

food dispatch-service 의 새 릴리스 1.6.0 은 배달 앱 목록 화면에 페이지 번호를 그리려고 `GET /api/deliveries` 에 조건에 맞는 배달의 전체 건수를
`X-Total-Count` 헤더로 더한다. 그러려고 목록 쿼리의 반환형을 `Slice` 에서 `Page` 로 바꿨고, Spring Data 는 `Page` 를 만들 때마다 같은 조건의
COUNT(`SELECT COUNT(d1_0.id) FROM dispatches d1_0 WHERE d1_0.status=?`)를 함께 낸다. 본문, 다른 끝점, health, 스키마, 인덱스는 그대로라 새 파드는
Ready 가 되어 옛 파드를 대신한다. 배달 추적 목록(`status=DELIVERED&page=0&size=20`)에서 이 COUNT 는 인덱스 `idx_dispatches_status_assigned` 의
끝난 배차 약 174만 항목을 훑는다(109 MySQL 이 한가할 때 0.44~0.53초). 페이지 자체(LIMIT 20)는 여전히 1ms 안팎이다. 저녁 피크처럼 배달 추적이 몰리면
(목록 초당 약 7건) COUNT 가 CPU 0.5 짜리 MySQL 이 실행할 수 있는 양(초당 약 2건)을 넘고, dispatch Hikari 연결 10개가 모두 COUNT 에 묶인다.
주문마다 필요한 용량 확인과 배차 요청은 연결을 3초 기다리다 500 이 되고, order 는 재시도와 서킷 끝에 새 주문을 503 으로 거절한다.

비유: 택배 조회 창구(dispatch)가 새 안내판에 "지금까지 배달 완료된 택배 총 ○○건" 을 띄우기로 했다. 조회 손님이 올 때마다 직원이 창고의 완료 장부
174만 줄을 처음부터 센다. 손님이 몰리면 직원 열 명이 모두 장부를 세느라, 정작 새 택배 접수(주문)에 필요한 "지금 배달원 여유 있나" 확인을 해 줄
사람이 없다. 장부(표와 색인)는 멀쩡하고, 고칠 곳은 세기를 시킨 새 안내판(릴리스)이다.

## 2. 원본 사례

- **GitHub, 2025-01-09** (공식 월간 가용성 보고, 2025년 1월): https://github.blog/news-insights/company-news/github-availability-report-january-2025/
- 01:26~01:56 UTC 많은 서비스가 광범위하게 중단되어 사용자가 여러 기능에서 서버 오류를 받음. 원인은 "a deployment which introduced a query that
  saturated a primary database server". 오류율 평균 6%, 갱신 요청 최대 6.85%. 내부 도구와 대시보드로 문제 질의의 출처를 찾아 배포를 되돌려 완화,
  대응 시작부터 문제 질의를 찾기까지 14분. 재발 방지로 배포 전에 문제 질의를 잡는 도구에 투자. 질의 내용과 DB 종류는 적혀 있지 않음.
  자료 문서 `ref-real-world-incidents.md` M2 에 이미 있다(F42-R 이 은행 Oracle 판으로 쓴 원본).
- **함께 본 사례(질의 꼴)**: MIT Open Learning learn.mit.edu, 2026-03-24 팀 공개 사후 보고
  https://engineering.ol.mit.edu/runbooks_post_mortems/20260324_mitlearn_outage . 정규 릴리스의 질의 패턴 변경이 원인이고, 작성자 설명으로는
  DRF 가 페이지 응답의 count 칸을 "SELECT COUNT * ($QUERY)" 로 계산하는데 변경이 "fine on datasets with smaller ordinality but not in prod" 였다.
  Postgres 저장 공간과 임시 공간 thrash, 504, 되돌림과 긴 질의 종료로 회복. 이 사례로 "페이지 응답의 전체 건수 COUNT 가 운영 데이터 규모에서만
  비싸다" 는 결함 꼴이 실제로 있음을 확인했다. 자료 문서 M2 에 출처가 말한 사실만 더했다. 원본은 GitHub 이고 MIT 는 질의 꼴의 근거로만 쓴다
  (MIT 는 포화 자원이 임시 디스크였고, 이 재구성은 CPU 다).
- 테스트베드 자체 선례: 2026-10-08 커밋 `27a3d0a` 전까지 이 목록이 `Page` 였고(7월 13일 commerce 복제 때 딸려 온 것), 119 DPM 에 그 COUNT
  (`sql_id e5ffcbfa…`, 당시 약 500만 행)가 평균 1.8~4.0초, 최대 15.4초로 남아 있으며 같은 기간 dispatch `GET /api/deliveries` 서버 스팬 p50 이
  약 2.4초였다(10-09 에는 2.5ms). 같은 꼴의 질의가 이 MySQL 과 dispatch 풀을 실제로 말린다는 실측 근거다.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 배포(새 버전) | dispatch-service 를 릴리스 food-delivery-dispatch:1.6.0 으로 롤아웃(KCM ScalingReplicaSet, 새 파드 Pulled 이벤트에 태그가 남음) |
| 원인이 된 결함 | 배포가 들여온 질의 하나가 운영 규모에서 비쌈 | 목록이 Page 가 되어 요청마다 끝난 배차 약 174만 항목을 세는 COUNT 가 새로 붙음(MIT 사례와 같은 '페이지 응답의 전체 건수' 꼴) |
| 전파 경로 | 그 질의가 주 DB 서버를 포화 → 같은 DB 를 쓰는 여러 기능에서 서버 오류 | COUNT 가 food MySQL(CPU 0.5)을 포화, dispatch Hikari 10개가 COUNT 에 묶임 → 용량 확인, 배차 요청이 연결 대기 3초 뒤 500 → order 재시도, 서킷 → 주문 503. 같은 MySQL 의 다른 food 질의도 느려짐 |
| 사용자 증상 | 여러 기능에서 서버 오류(평균 6%) | 새 주문 대부분 503, 배달 추적 목록 시간 초과나 500, 가게 둘러보기는 느려짐 |
| 원본의 탐지 경로 | 내부 도구와 대시보드로 문제 질의의 출처를 찾음 | DPM Top SQL 의 COUNT digest rowExamined 와 만료 스윕 digest, VM index_count, dispatch Hikari 고갈 로그, order 오류율, KCM 롤아웃 이벤트 |
| 완화와 복구 | 배포 되돌림 | cleanup 이 dispatch 이미지를 매니페스트 이미지로 되돌림(롤백). 매니페스트 버전은 COUNT 를 내지 않음 |

기전 동일성: 원본과 재구성 모두 "새 버전이 들여온 질의 → 운영 데이터 규모에서 DB 서버 포화 → 그 DB 를 쓰는 기능들이 서버 오류 → 배포를 되돌려 복구"
고리다. 원본이 질의 내용을 밝히지 않아, 결함은 MIT 사례가 보여 준 실제 꼴(페이지 응답의 전체 건수 COUNT)로 한 끝점, 한 질의 크기로 작게 두었다.
스키마, 인덱스, 설정, 자원 한도는 바꾸지 않는다.

## 3. 숫자 근거 (2026-10-09, `scenario-stats.py`, 정식 42 + 후보 13 = 55)

- 서비스(정식 + 후보): commerce 26, core-banking 16, **food-delivery 13(가장 적음)** → 14
- 묶음: A 7, B 7, D 7(각 12.7%), C 6, G 6, **J 5** → 6(56 중 10.7%), L 4, F 3(F 로 세도 → 4, 7.1%), E 2, H 2, K 2, I 1, M 1, O 1, P 1, N 0. 20% 상한(11) 아래
- 정답 위치: **배달 서비스 2** → 3(5.4%, §2-1 에 이미 있는 말). 최다는 외부 결제 의존, 주문 서비스 각 6(10.9%)
- 결제 경로 합계 10(18.2%) → 그대로 10/56(17.9%)
- 부품 지도에서 food dispatch 는 3(F32-R, F32-H, F33-R 의 DB 테이블(배차)은 정답 위치로는 따로 센다)
- 왜 이 후보인가: 가장 적은 서비스(food)이고 상한에 닿은 축이 없다. 숫자 순위가 더 높은 후보(0 인 묶음 N, 0 인 정답 위치 메시지 브로커, 게이트웨이,
  알림)는 §10 버린 후보처럼 원칙 7, 9, 1 에서 막혔다. 이 후보는 F33-R 과 겉 증상(MySQL 포화, dispatch 풀 'Connection is not available', 주문 503)이
  같고 정답(인덱스가 빠진 표 대 COUNT 를 들여온 릴리스)이 달라, 관제 AI 가 변경 이력(롤아웃), 인덱스 수, 같은 표의 다른 질의(만료 스윕) 비용을 갈라 봐야 맞힌다(관계 H).
  F42-R 장부 메모가 같은 기전의 dispatch 판을 증상 겹침으로 고르지 않았지만, 2026-10-09 사용자 정정으로 증상 겹침은 버릴 사유가 아니다.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 계기: `rca-testbed-food` Deployment `testbed-dispatch` 이미지 `food-delivery-dispatch:latest` → `food-delivery-dispatch:1.6.0`
   (`food-delivery/k8s/22-dispatch-deploy.yaml:27-28`, imagePullPolicy Never, replicas 1 `:9`, nodeSelector tb-w3 `:18-19`, 기본 전략 maxSurge 25%).
2. 결함(릴리스 1.6.0): `scripts/scenarios/fault-images/f33-h/dispatch-service.patch:17-22`(DispatchController.getDeliveries 가 Page 를 받아
   `X-Total-Count` 에 getTotalElements()), `:43-49`(DispatchRepository.findByStatus, findAllBy 반환형 Slice → Page), `:69-79`(DispatchService.searchDispatches
   가 Page 를 돌려줌). 패치는 릴리스 자신의 테스트 `DispatchSearchTest` 도 Page 계약으로 고친다(Dockerfile 의 `package -DskipTests` 는 테스트를 실행하지 않지만
   컴파일은 하므로, 옛 Slice 단언이 남으면 빌드가 실패한다). 매니페스트 버전은 `food-delivery/dispatch-service/src/main/java/com/fooddelivery/dispatch/repository/DispatchRepository.java:16-18`
   ("Slice 라 COUNT 를 내지 않는다").
3. 비용: Spring Data 의 Page 는 첫 페이지가 꽉 차면(20건) COUNT 를 낸다. 끝난 배차는 1,742,002행, 배정 중 307행(2026-10-09 19:24 UTC, 109 MySQL
   읽기 조회). `EXPLAIN` 은 `idx_dispatches_status_assigned` ref, Using index(rows 추정 867,266). 실행 0.44, 0.45, 0.53초(109, 같은 문장 3회).
4. 포화: MySQL CPU 한도 500m(`food-delivery/k8s/10-mysql.yaml:48-50`). 한 문장은 한 스레드라 한도 안에서 최대 0.5 코어를 쓰므로 COUNT 하나가
   CPU 약 0.22~0.27초, 처리 가능량 초당 약 2건이다.
5. 풀: dispatch Hikari maximum-pool-size 10, connection-timeout 3000ms(`food-delivery/dispatch-service/src/main/resources/application.yml:12-15`).
   배차 요청 `DispatchService.java:60`, 용량 조회 `:106-107` 이 같은 풀로 `countByStatus("ASSIGNED")` 를 실행한다.
6. 호출자: `food-delivery/order-service/src/main/java/com/fooddelivery/order/service/OrderService.java:94-112`(주문 저장 전 용량 확인, 실패 시 503
   'Dispatch service unreachable'), `order/client/DispatchClient.java:32-66`(재시도 3회 200ms 지수, 서킷 10건 창 50% 5초 열림,
   'Failed to check dispatch capacity' ERROR). order 의 dispatch 읽기 시간 제한 5초(`order-service/src/main/resources/application.yml:55-58`).
7. 영향 밖: 스키마, 인덱스, MySQL 설정, 다른 서비스 이미지와 설정.

## 5. 원인 규정 (원칙 6)

- `root_cause.target_id`: `food-delivery-dispatch`(target_kind container). 결함을 가진 곳이자 되돌려야 재발이 막히는 곳은 비싼 COUNT 를 들여온
  dispatch 새 릴리스다. 배달 추적 요청 A 는 정당하고(평시와 같은 끝점, 같은 쿼리 문자열), 그것을 받은 B(dispatch 1.6.0)의 로직이 요청마다 전수 COUNT 를
  붙이므로 원칙 6 의 "B 의 로직이 잘못" 경우다. 원본 포스트모템도 질의를 들여온 배포를 되돌렸다(같은 층위). 결함 이미지 규칙 7(근본은 그 서비스의 새 버전).
- `trigger_target_id`: 없음(계기인 롤아웃과 근본이 같은 곳).
- `scoring.partial`: `food-mysql`, `food-mysql:fooddelivery.dispatches`(포화된 DB 와 비싼 질의가 도는 표, F33-R 의 정답), `food-delivery-order`,
  `food-order`, `testbed-order`(증상을 낸 곳).
- 코드 줄을 맞히라고 요구하지 않는다(원칙 5). "dispatch 롤아웃 직후 MySQL 이 포화되고 status COUNT 의 평균 검사 행 수가 오르는데,
  인덱스 수와 만료 스윕 비용은 그대로" → dispatch 릴리스는 관제 데이터(KCM, DPM Top SQL, VM index_count, 로그)로 낼 수 있는 결론이다.

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 | 기대 |
|---|---|---|
| 감지(lucida-next) | order, dispatch log-anomaly(Hikari 고갈, 용량 확인 실패), MySQL stream-anomaly(row_examined, select_scan 등), order 서버 스팬 오류율 | F33-R 실행(F33-R-run-73fed92a)에서 같은 꼴이 food 인시던트 12건으로 승격됐다 |
| 피해 판정(러너) | 동반 부하 k6 문서의 주문 생성 5xx 비율 ≥ 0.5, 2xx 비율 < 0.3 (3틱) | 평시 5xx 0 근처 |
| 원인 설명(RCA) | KCM 롤아웃 이벤트와 이미지 태그, DPM Top SQL(COUNT digest 의 평균 rowExamined, 만료 스윕 digest), VM index_count, 전수 로그 | §7 |

## 7. 관측 근거 (119 실조회, 평시)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 핵심 증거는 KCM 이벤트, DPM Top SQL(폴링 전수), VM DPM 지표, 전수 로그다.

| 증거 | 119 표와 칸 | 조회 | 결과(2026-10-09) |
|---|---|---|---|
| 계기: 롤아웃과 이미지 태그 | CH `lucida.kcm_events_local`(reason, object_name, body) | `reason='Pulled' AND object_name LIKE 'testbed-dispatch%'` 전체 기간, 본문에 담긴 태그별 수 | 49건 전부 'Container image "food-delivery-dispatch:latest" already present on machine', 2026-08-14 00:39 ~ 2026-10-09 00:26(F32-R, F32-H 의 env 롤아웃). :latest 가 아닌 태그 0건 → 장애 때 1.6.0 이 처음 나타나야 한다 |
| 근본: COUNT digest | CH `lucida.dpm_topsql_local`(sql_id, body JSON 의 calls, avgExecTime, rowExamined, sqlText) | `sql_id LIKE '4ca1b0a1%'` 최근 3시간(관측 10분 전까지) | sqlText `SELECT COUNT ( d1_0.id ) FROM dispatches d1_0 WHERE d1_0.status = ?`. 27행(1분 delta, 매분 잡히지는 않음. 최근 3시간을 다시 보면 11행인 창도 있음), calls 합 1,143. 평소 rowExamined 약 1,100~1,300(배정 중 행). 최대 227,486 과 57ms 는 이 설계의 109 실측 3회(19:24 UTC)가 섞인 1분이다. 릴리스의 목록 COUNT 도 같은 문장이라 같은 digest 로 합쳐진다(로컬 performance_schema 에서 확인: 릴리스 jar 의 COUNT 가 같은 digest 4ca1b0a14f48194a). 장애 때는 평균 rowExamined 가 배정 중(약 300)과 끝난 배차(약 174만)가 섞인 수십만이 되어야 한다. 호출 수는 판별에 쓰지 않는다: 실행 수는 MySQL 처리량(초당 약 2건)에 묶이고, 풀 고갈로 배정 중 COUNT(용량 확인, 배차 요청)는 오히려 줄 수 있다 |
| 감별: 만료 스윕 digest | 같은 표 `sql_id LIKE 'b79f4ca8%'` | 같은 창 | calls 71, rowExamined 평균 535, 최대 598, avg 0.92ms. 인덱스가 그대로면 장애 때도 이 값이어야 한다(F33-R 에서는 약 175만으로 뜀) |
| 감별: 목록 페이지 질의 | 같은 표 `sql_id LIKE 'adf27719%'` | 같은 창 | 2행, calls 6, rowExamined 21(LIMIT 21 조회, 호출이 적어 Top SQL 상위에 드물게 잡힘). 장애 때 Top SQL 에 더 자주 잡힐 수 있으나 판별 근거로 쓰지 않는다(보조) |
| 감별: 인덱스 수 | VM `dpm.mysql.database.index_count{db_name="fooddelivery"}` | 최근 2시간 | 31 고정(F33-R 은 30 으로 내려감) |
| 전파: dispatch 풀 고갈 | CH `lucida.lucida_logs_local` | `service_name='food-delivery-dispatch' AND body LIKE '%Connection is not available%'` 최근 24시간 시간별 | 10-08 20시 3건, 10-09 03시 90건(F33-R 실행)뿐, 나머지 0 |
| 전파: order 용량 확인 실패 | 같은 표 | `service_name='food-delivery-order' AND body LIKE 'Failed to check dispatch capacity%'` 7일 일별 | 10-03~10-07 하루 3~30건, 10-08 205건, 10-09 326건(시나리오 실행 날). 장애 때 분당 수십 건 |
| 전파: 목록 지연 | CH `otel_traces_local`(service_name, span_name, duration_ns) | dispatch `GET /api/deliveries` 서버 스팬 3일 일별 | 10-07 p50 2,348ms, 10-08 2,394ms(목록 COUNT 가 있던 때), 10-09 p50 2.5ms, p99 9.8ms(Slice). 장애 때 초 단위(보조, 표본) |

109 MySQL 읽기 조회(2026-10-09 19:24 UTC): 끝난 배차 1,742,002, 배정 중 307, 인덱스 PRIMARY, idx_dispatches_order, idx_dispatches_status_assigned
(status, assigned_at), buffer pool 128MB, MySQL 8.0.46. COUNT 3회 0.44, 0.45, 0.53초.

로컬 실측(2026-10-09, 104, mysql:8.0.46 `--cpus 0.5 --memory 1g`, food init.sql + 끝난 배차 1,740,000행과 배정 중 300행, origin/main 41ee49c 로 빌드한
dispatch jar 와 패치를 얹은 jar, OTel 에이전트 없음, 보존 배치와 outbox 릴레이 끔). 로컬 COUNT 는 0.93~1.30초라 109 보다 약 2.3배 느리다.

| jar | 부하(초당) | 용량 확인 | 배차 요청 | 배달 상세 | 목록 | health(10초마다) | Hikari 대기 초과 로그 |
|---|---|---|---|---|---|---|---|
| 매니페스트 | 목록 1.6, 용량 1, 배차 1, 상세 1 (60초) | 60/60 200, p50 10ms | 60/60 200 | 60/60 200 | 96/96 200 | 모두 UP | 0 |
| 릴리스 1.6.0 | 같음 (120초) | 47/120 200, 나머지 500(3.0초) | 40/120 200 | 44/120 200 | 13/192 200, 106 은 10초 시간 초과 | 10회 중 8회 503 | 618 |
| 릴리스 1.6.0 | 목록 2.4(= 109 의 약 5.5건/초), 나머지 같음 | **23/120 200** | **23/120 200** | 16/120 200 | 8/288 200 | 10회 중 7회 503, 2회 시간 초과 | 947 |

릴리스 응답에는 `X-Total-Count: 1740000` 헤더가 붙고 본문 배열은 그대로다.

빌드 경로 확인(2026-10-09, 104): `build.sh` 와 같은 순서로 origin/main 의 food-delivery 를 git archive 로 풀고 패치를 `git apply -p1` 한 뒤
`docker build --network=host -f food-delivery/dispatch-service/Dockerfile food-delivery` 를 로컬 임시 태그로 돌려 성공했다(빌드 산출 jar 의
`DispatchRepository.findByStatus` 반환형 Page 를 javap 로 확인, 임시 이미지는 지움). 패치를 얹은 모듈의 `mvnw -pl dispatch-service test` 14건 통과.
109 docker 의 `food-delivery-dispatch:1.6.0` 은 배포 단계에서 `build.sh f33-h` 로 만든다.

## 8. 감별

- must_support: 정답지 `must_support` 4개(롤아웃과 태그, index_count 불변과 만료 스윕 불변, COUNT digest 의 섞인 평균 rowExamined, dispatch 풀 고갈과 order 용량 확인 실패
  로그, 목록 스팬 지연, MySQL Ready 와 다른 변경 없음).
- must_rule_out: F33-R(인덱스 제거), F32-R, F32-H(배차 한도, 응답 형식), F25-H, F38-R(DB 다운, DB 설정), 부하만 늘어난 것, order 자체 결함, dispatch 프로브 오설정이나 재시작 자체(F05-H 꼴).
- contrast_with: F33-R, F42-R, F32-R, F32-H, F49-R.
- **같은 증상, 다른 정답(관계 H)을 가르는 관측 근거(F33-R 대 F33-H)**:

| 관측 | F33-R(인덱스 제거) | F33-H(릴리스가 COUNT 를 들여옴) |
|---|---|---|
| VM index_count(fooddelivery) | 31 → 30 → 31 | 31 그대로 |
| COUNT digest 호출당 rowExamined | 모든 호출이 약 175만(전수 스캔) | 배정 중 COUNT 는 약 300, 목록 COUNT 는 약 174만이 섞여 평균 수십만 |
| 만료 스윕 digest b79f4ca8… rowExamined | 약 175만 | 약 300~600 그대로 |
| KCM | dispatch 롤아웃 없음 | testbed-dispatch ScalingReplicaSet, Pulled 'food-delivery-dispatch:1.6.0' |

- F32-R 과는 order 로그('Order rejected: courier pool exhausted' 대 'Failed to check dispatch capacity'), dispatch 응답(200 'available 0' 대 500 과
  시간 초과)으로, F32-H 와는 'cannot be cast' 해석 실패 유무와 dispatch 지연으로 갈린다.
- 결정 근거는 index_count 불변, 만료 스윕 rowExamined 불변, COUNT digest 의 섞인 평균 rowExamined, KCM 롤아웃 넷이다. COUNT digest 의 호출 수는 MySQL 처리량에
  묶이고 풀 고갈로 배정 중 COUNT 가 줄 수 있어 판별에 쓰지 않는다.
- dispatch 재시작: liveness(/actuator/health, 15초 간격, 3초 제한, 5회 실패면 재시작, `22-dispatch-deploy.yaml:80-86`)가 DB 확인 때 풀 연결을 3초 기다린다.
  로컬 실측 health 10회 중 7~8회 503 이고 F33-R 실행에도 Liveness 실패 이벤트가 있었으므로, 상주 포화 중 Unhealthy, Killing, BackOff 가 나올 수 있다.
  재시작은 결과다: 프로브 설정은 그대로이고 실패는 롤아웃 뒤 포화 구간에만 있으며, 재시작마다 잠시 호출이 거부된 뒤 다시 뜬 1.6.0 이 곧 같은 포화에 든다.
  그래도 정답은 dispatch 릴리스이고(정답 위치가 같은 서비스), 재시작만 보고 프로브 오설정(F05-H 꼴)으로 답하면 감별 실패다.
- 러너 success 는 F33-R 과 같은 지표(주문 생성 5xx, 2xx)라 녹화 검증이 위 표의 관측으로 가른다.

## 9. 러너 판정 조건과 강도, 부하 계산

- 주입: k8s.image release 모드, 설계 강도 1단 고정 evaluation(`approved-fixed-f33-h`, min_hold 15m, settle 30s, timeout 20m, max_injection_duration 25m).
  강도 사다리 없음, 첫 실행이 곧 녹화 실행.
- success(3틱): 동반 부하 주문 생성 5xx ≥ 0.5, 2xx < 0.3. 2xx 기준을 F33-R(0.2)보다 넓힌 이유: order 재시도 3회가 용량 확인과 배차 요청 각각에서 일부를
  건져 2xx 가 0.1~0.3 을 오갈 수 있다(F33-R 실행은 0.09~0.26 으로 3틱 연속까지 6분). 5xx 0.5 이상과 함께면 주문 다수가 실제로 실패한 것이다.
- must_rule_out(2틱): achieved_rps < 3(동반 부하 12rps 의 1/4, 부하 끊김), food MySQL NotReady(DB 다운이면 F25-H 꼴).
- abort: entry_status == 0(2틱). order 는 dispatch 실패를 3초 대기 × 재시도 뒤 503 으로 돌려주고 서킷이 열리면 즉시 503 이다. 같은 dispatch 풀 고갈 꼴의
  F33-R 실행에서 order 는 liveness 실패 8회가 있었지만 재시작 없이 연결 가능 상태를 지켰다. dispatch 파드 준비 상태는 러너 관측 허용 목록
  (`live_probes.py` 의 food 파드는 restaurant 만)에 없어 판정과 회복에 쓰지 않는다.
  dispatch 가 liveness 로 재시작되어도 order 는 연결 거부를 바로 503 으로 돌려주므로(재시도 백오프만큼) order 풀을 오래 잡지 않는다.
- 부하: load.north_south `delivery-tracking-surge.js`(새 스크립트) 12rps, 여정 가중 추적 목록 60%, 추적 상세 15%, 주문 10%, 둘러보기 15%.
  - 목록 약 7.2건/초 × COUNT CPU 약 0.22~0.27초 = 초당 약 1.6~1.9 코어 수요 대 MySQL 0.5 코어 → 3~4배 초과. 상주 기준선의 목록은 초당 약 0.1~0.2건
    (dispatch `GET /api/deliveries` 표본 스팬 6시간 210건, 하루 1,415건 × 10)이라 동반 부하 없이는 포화되지 않는다(배달 추적 피크가 강도다).
  - 로컬 대응: 목록 2.4건/초(109 의 약 5.5건/초)에서 용량 확인과 배차 요청 성공 각 19%. order 는 둘을 차례로 거쳐야 하고 각 재시도 3회를 감안해도
    주문 성공은 20~25% 안팎이며 서킷이 열린 동안은 즉시 503 이다. 109 목록 7.2건/초는 로컬 시험보다 더 세다.
  - 주문 약 1.2건/초는 러너 표본(30초당 약 36건)을 위한 것이다. 릴리스가 효과가 없어 모든 주문이 성공하더라도 배정 중 배차는 307 + 약 1,600 으로
    배차 한도 2000 아래라 F32-R 꼴 'courier pool exhausted' 가 섞이지 않는다(ETA 15~34분 뒤 만료로 실제로는 더 적다).
  - 다른 food 질의(가게 둘러보기 1.8건/초 등)는 포화된 MySQL 에서 느려지지만 restaurant 와 order 의 질의는 짧고 풀이 따로라 F33-R 실행처럼 입구는
    유지된다.
- 롤아웃 시간: tb-w3 에서 새 dispatch JVM 기동과 readiness 통과까지 약 1분(옛 파드가 그동안 서비스). 동반 부하는 2분에 걸쳐 오른다.
- 배포 전제: 109 docker 에 `food-delivery-dispatch:1.6.0` 이 있어야 preflight 가 통과한다(`bash scripts/scenarios/fault-images/build.sh f33-h`, 배포 단계에서
  실행). 새 부하 스크립트는 deploy-live-promotions.sh 가 허용 목록을 읽어 tb-runner `/opt/loadgen/food-delivery/` 에 올린다(상주 유닛 재시작 불필요).

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(GitHub 2025-01-09, 공식 월간 가용성 보고 링크)와 요소별 대응표, 기전 동일(§2). 질의 꼴은 MIT Open Learning 2026 공개 사후 보고로 뒷받침
- [x] 분류 장부 갱신(§4-1 행, §6 기록), 묶음 J+F(J 5→6, 10.7%), 정답 위치 배달 서비스(2→3, 5.4%), 결제 경로 10(17.9%), 서비스 food 13→14(가장 적음),
  고른 이유(§3). 어느 축도 20% 미만
- [x] 근본 원인 위치 `file:line` 확인(패치 줄, 매니페스트 코드, 풀 설정, order 호출자)과 인프라 지점(22-dispatch-deploy.yaml, 10-mysql.yaml)(§4)
- [x] 근본 원인 흔적 119 실조회: kcm_events_local 의 Pulled 본문 태그, dpm_topsql_local 의 COUNT digest 와 만료 스윕 digest, VM index_count(§7)
- [x] 핵심 증거가 표본에만 있지 않음: KCM 이벤트, DPM Top SQL(폴링), VM DPM 지표, 전수 로그. 스팬은 보조
- [x] 계기 흔적: 롤아웃 이벤트와 새 이미지 태그. 인공 지연 없음(느려지는 이유는 실제 COUNT 실행 비용)
- [x] 정답이 관제 데이터로 낼 수 있는 결론(롤아웃 직후 MySQL 포화, 인덱스 수와 만료 스윕 비용 불변, COUNT 평균 검사 행 수 상승), 코드 설계 결함 추론 불필요(§5)
- [x] 정답지 세 칸이 원칙 6 대로(근본 dispatch 릴리스, 계기 같음, 부분 점수 MySQL/dispatches 표, order)
- [x] 감지, 피해 판정, RCA 증거 구분(§6)
- [x] 주입 도구가 시나리오 id 를 남기지 않음: 실행기 인자는 네임스페이스, Deployment, 컨테이너, 이미지 둘뿐이고, 패치가 더한 문자열에 시나리오 id, fault, bug,
  chaos 같은 말이 없음(테스트 `test_k8s_image_release_mode_rolls_food_dispatch` 가 확인). 동반 부하의 k6 태그는 기존 스크립트와 같은 규약
- [x] 부하 상한과 서킷브레이커를 고려한 피해 계산(§9: 12rps 로 MySQL 수요 3~4배, 로컬 2.4건/초에서 용량 확인 성공 19%, 서킷 열림 동안 즉시 503)

### 후보 목록 (숫자 순으로 줄 세운 뒤 관문에서 걸러진 것)

| # | 실제 기전 × 부품 | 사례 | 결과 |
|---|---|---|---|
| 1 | 메시지 브로커(Kafka, 세 도메인 0) 설정이나 소비자 고장 | GitHub 2026-06-25 등 | 버림, 원칙 7: 세 도메인 모두 Kafka 를 outbox 릴레이에서만 써서 사용자 증상이 없다(앞선 반려와 같은 벽) |
| 2 | commerce gateway(0) 메모리 누수 릴리스 | Honeycomb 2019-11-06 | 뒤로 미룸: commerce 가 가장 많고(26), F41-R 과 같은 원본, 같은 결함 꼴이라 R 접미사가 이미 쓰였다(새 번호로 내면 원본 중복). 이번 반복에서는 food 후보를 먼저 |
| 3 | banking transfer CPU 한도 축소로 기동이 느려져 startupProbe 가 죽이는 크래시 루프 | PostHog 2025-10-28(M11) | 뒤로 미룸: 정답 위치 은행 이체 서비스 5(가장 많은 축 중 하나)이고 겉 증상과 정답이 F17-H 와 같다 |
| 4 | food MySQL 임시 공간 thrash(집계 질의 릴리스) | MIT Open Learning 2026 | 버림, 원칙 9와 cleanup: MySQL 데이터가 노드 디스크와 같은 볼륨이라 임시 파일이 차면 노드 DiskPressure 로 원인이 가려진다(F10-R parked 와 같은 벽). 같은 원본의 질의 꼴만 이 후보에 씀 |
| 5 | food MySQL max_connect_errors 로 호스트 차단 | (공식 사후 보고 못 찾음) | 버림, 원칙 1 |
| 6 | banking Oracle open_cursors 하향(ORA-01000) | (공식 사후 보고 못 찾음) | 버림, 원칙 1 |
| 7 | banking Oracle 임시 테이블스페이스 가득(ORA-01652) | (공식 사후 보고 못 찾음) | 버림, 원칙 1 |
| 8 | food order 릴리스가 외부 호출을 트랜잭션 안으로 넣어 풀 고갈 | (공식 사후 보고 못 찾음) | 버림, 원칙 1, 컨트롤러 필수 중단 조건(order readiness) |
| 9 | food order 의 JVM DNS 캐시 무기한 + Service IP 변경 | (공식 사후 보고 못 찾음) | 버림, 원칙 1, 계기가 둘(복합) |
| 10 | **food dispatch 릴리스가 목록에 전체 건수 COUNT 를 들여옴** | GitHub 2025-01-09, MIT Open Learning 2026 | **채택(F33-H)** |
