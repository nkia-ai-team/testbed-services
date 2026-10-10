---
title: F43-P 설계 시트 (food dispatch-service 를 DB 연결이 새는 릴리스로 롤아웃해 주문이 503)
status: Draft
owner: project
last_reviewed: 2026-10-10
tags:
  - scenario
  - design
  - release
summary: food dispatch-service 를 결함 있는 새 릴리스(food-delivery-dispatch:1.7.0, fault-images/f43-p 패치로 만든 별도 태그)로 롤아웃하면, 배차 요청 앞에 더한 중복 배차 확인이 풀 연결을 빌리고 중복일 때만 돌려줘 Hikari 풀(10)이 배차 약 10건 만에 마르고(MySQL 은 한가함), 용량 확인과 배차가 연결 대기 시간 초과, dispatch 가 NotReady 와 liveness 재시작을 되풀이하며 모든 새 주문이 503 으로 거절되는 시나리오. 원본은 Octopus Deploy 2025-11-25 인가 서비스 배포가 들여온 DB 연결 누수. F43-R(commerce cart)의 P.
---

# F43-P 설계 시트

## 1. 요약

food dispatch-service 의 새 릴리스 1.7.0 은 "같은 주문에 배달원을 두 번 잡지 않기" 를 더했다. order 는 배차 호출을 재시도(최대 3회)하므로 앞선 시도가 이미 배차를 남겼을 수 있다는 이유다. 확인은 배차 트랜잭션을 열기 전에 `DataSource` 에서 연결을 직접 빌려 `SELECT count(*) FROM dispatches WHERE order_id = ? AND status = 'ASSIGNED'`(idx_dispatches_order, 1ms 미만)를 돌리고 결과 집합과 문장은 닫는다. 그런데 연결은 중복을 찾아 409 를 던지는 갈래에서만 닫는다. 중복이 없는 거의 모든 배차 요청에서 빌린 연결이 빌린 채로 남는다(MySQL 에서는 Sleep 세션). 릴리스 자신의 단위 시험은 중복 갈래(연결을 닫는 쪽)만 확인한다.

풀 크기(10), 대기 시간(3초), 프로브, 자원 한도, MySQL 설정은 그대로다. 배차 약 10건 뒤 풀이 total=10, active=10, idle=0 으로 굳고 대기 스레드는 0~3 이다. 그 뒤 dispatch 의 모든 JPA 작업(용량 COUNT, 배차 INSERT, 배달 추적 조회, 만료와 보존 배치, outbox 릴레이)과 DB 확인이 든 `/actuator/health` 가 연결 대기 3초 끝에 실패한다. order 는 용량 확인에서 500 을 받아('Failed to check dispatch capacity: 500 ...') 재시도와 서킷 끝에 주문을 아무것도 저장하지 않은 채 503 으로 거절한다. 약 30초 뒤 readiness 가 dispatch 를 엔드포인트에서 빼면 order 는 연결 거부로 곧바로 503 이고, liveness 5회 실패(약 75초)에 kubelet 이 재시작한다. 재시작은 쥐고 있던 연결을 닫아 새 파드가 배차 약 10건을 받고 다시 마른다. MySQL 은 내내 한가하다.

비유: 배차실(dispatch) 공용 볼펜 10자루. 새로 온 규칙은 "배정 전에 중복 장부를 확인하라" 인데, 담당자는 중복을 찾았을 때만 볼펜을 꽂아 두고 아무 일 없을 때는 주머니에 넣는다. 배정 10건 뒤 볼펜이 없어 배차실이 멈추고, 주문 접수처(order)는 "배차실 응답 없음" 으로 주문을 돌려보낸다. 교대(재시작)하면 주머니가 비워져 10건 동안만 돌아간다. 장부(MySQL)는 한가하다. 고칠 곳은 새 규칙(릴리스)이다.

## 2. 원본 사례

- **Octopus Deploy, 2025-11-25** (공식 상태 페이지 사후 보고, 게시 2025-12-08): https://status.octopus.com/incidents/q5mhnrsm7q6j
- 사고 "OctopusID signin intermittent for cloud customers". 11-25 12:43 pm(AEST) 인가(authorization) 서비스에 변경을 배포했고 "This change introduced a bug resulting in database connection leaks." 그 결과 "a database connection leak, which caused some sign-in requests to time out". 증상은 간헐적이었다. 고객 보고로 사고 선언(배포부터 9시간 41분), DB 연결 시간 초과까지 추적, "restarting services to release database connections" 로 임시 완화, 다음 날 근본 원인을 연결 누수로 확인하고 누수를 없앤 버전을 배포해 해결. 테스트가 놓친 이유는 "The connection leak only became apparent in high-traffic scenarios, which our current test suite doesn't replicate."
- 자료 문서 M2 표에 이미 있다(F43-R 이 더함). 새로 더할 사실 없음.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 인가 서비스에 변경 배포 | dispatch-service 를 릴리스 food-delivery-dispatch:1.7.0 으로 롤아웃(KCM ScalingReplicaSet, 새 파드 Pulled 이벤트에 태그가 남음) |
| 원인이 된 결함 | 배포한 변경이 DB 연결 누수 버그를 들여옴 | 새 중복 배차 확인이 빌린 연결을 중복 갈래에서만 닫음 |
| 전파 경로 | 연결 누수 → 연결 고갈 → 그 서비스에 기대는 로그인 요청의 DB 작업 시간 초과 | 배차 1건에 1개 누수 → 풀(10) 고갈 → dispatch 의 모든 DB 작업 3초 대기 실패 → 그 서비스에 기대는 order 의 용량 확인 실패 → 주문 503. health 의 DB 확인 실패로 readiness 이탈, liveness 재시작 |
| 사용자 증상 | 로그인이 시간 초과, 간헐적 | 새 주문 503(빈 풀로 Ready 인 동안 약 3.6초, NotReady 동안 즉시), 재시작 직후 약 10건만 통과하는 간헐 회복. 배달 추적도 실패 |
| 원본의 탐지 경로 | 고객 보고, DB 연결 시간 초과 추적 | lucida-next 오류율, dispatch Hikari 지표(db.client.connections.*), dispatch 오류 로그, KCM Unhealthy, Killing, order 오류 로그 |
| 완화와 복구 | 서비스 재시작으로 연결을 풀어 임시 완화, 누수 없는 버전 배포로 해결 | liveness 재시작이 임시로 풀지만 되풀이, cleanup 이 매니페스트 이미지로 되돌림(롤백) |

기전 일치: 원인(배포한 코드가 DB 연결을 돌려주지 않음)에서 증상(그 서비스의 DB 작업이 연결 대기로 시간 초과, 그 서비스에 기대는 요청 실패, 재시작하면 잠깐 풀림)까지 고리가 같다. 원본이 "고트래픽에서만 드러났고 시험 묶음이 재현하지 못했다" 고 한 점은 릴리스의 시험이 중복 갈래만 다루는 꼴로 둔다.

바꾼 것(기전 고리 밖):
- 스택: 원본 서비스 스택 미상 → Spring Boot, HikariCP, MySQL 8.
- 속도: 원본은 탐지까지 9시간 41분. 테스트베드 dispatch 는 배차마다 새어 평시 주문만으로 수 초~수십 초에 마른다. '배포와 증상 사이가 멀어 배포를 의심하지 않는' 함정은 재현하지 않는다.
- 재시작 주체: 원본은 사람이 재시작했다. 테스트베드는 health 의 DB 확인이 실패해 kubelet 이 재시작한다.

F43-R 과의 관계(P, 일부 비슷): 원본, 결함 꼴(릴리스의 연결 누수), 주입 수단(k8s.image release)이 같다. 다른 것: DB 엔진(PostgreSQL → MySQL), 진입 경로(nginx → gateway → cart 대 loadgen → order NodePort → 용량 확인 서킷), 피해 서비스의 자리(cart 는 checkout 의 하류, dispatch 는 order 가 주문 저장 전에 묻는 검사 대상이라 order 가 따로 503 으로 거절), 누수 꼴(문장만 닫는 try-with-resources 대 한 갈래에서만 닫음). R 은 F43-R 이 이미 써서 P 로 둔다(F44-P, F48-P 선례).

## 3. 숫자 근거 (2026-10-10, `scenario-stats.py`, 정식 43 + 후보 17 = 60, 이 후보 전)

- 묶음(정식 + 후보): A 7, B 7, D 7, G 7(각 11%), C 6, J 6(10%), L 5, F 3, P 3, E 2, H 2, K 2, I 1, M 1, O 1, N 0. 금지(20%) 없음. 이 후보 J 6→7(61 중 11.5%), 피해 모양으로 E 를 함께 적는다(E 로 세도 2→3, 4.9%).
- 정답 위치: 주문 서비스 7(11%), 외부 결제 의존 6(10%), 노드, 디스크 5, 은행 이체 서비스 5, **배달 서비스 3**(5%) → 4(6.6%). 결제 경로 합계 10(16.7% → 61 중 16.4%, 이 후보와 무관).
- 서비스: 음식배달 16(가장 적음), 은행 18, 쇼핑몰 26. 음식배달 16 → 17.
- 부품 지도: food dispatch 4(배달 서비스 3 + DB 테이블(배차) 1). 0 인 food 부품은 kafka, notify 뿐이고 둘 다 비동기 소비자라 사용자 증상이 없다(원칙 7, 아래 후보 7).
- 왜 이 후보인가: 가장 적은 서비스(음식배달)에서 상한 아래 정답 위치이고, 원본이 공식 사후 보고다. F43-R 설계 때 dispatch 판을 '증상 겹침(order 503 이 F32-R, F32-H, F33-R 과 넷째)' 으로 버렸지만 2026-10-09 사용자 정정으로 증상 겹침은 버릴 사유가 아니다. 오히려 같은 dispatch 풀 고갈인 F33-R, F33-H 와 증상이 같고 원인이 다른(MySQL 포화 대 연결 누수) 짝이라, 관제 AI 가 "dispatch 풀 고갈이면 DB 탓" 으로 외워 찍지 못하게 하는 감별 데이터가 된다.

### 후보 목록 (3단계, "실제 기전 × 부품", 숫자 순)

| # | 후보 | 층, 부품 | 원본 사례 | 결과 |
|---|---|---|---|---|
| 1 | dispatch 새 릴리스의 DB 연결 누수, 풀 고갈과 재시작 반복 | 앱, food dispatch(J+E, 배달 서비스 3) | Octopus Deploy 2025-11-25 공식 상태 페이지 | **채택** |
| 2 | dispatch 새 릴리스가 배차 만료 배치를 깨 배달원 한도 2000 소진, 주문 503(F32-R 과 H) | 앱, food dispatch(J+B) | 찾지 못함 | 버림: 원칙 1. '배포가 정리, 만료 작업을 깨 자원이 반납되지 않고 한도가 찼다' 는 공식 사후 보고를 찾지 못함(웹 검색 3회: danluu 목록, CircleCI 상태 페이지, GitHub 2026-08 월간 보고. 이슈 추적기와 포럼 글뿐) |
| 3 | banking nginx 에 IP 기준 limit_req 를 더하는 설정 배포, 모든 요청이 한 IP 로 보여 거절 | 인프라, banking nginx(G, 게이트웨이 0) | PostHog 2025-10-24 공식 | 버림: 원칙 3. 119 lucida_logs_local 에 nginx 로그가 없고(2026-10-10 서비스 목록 실측: 앱 서비스와 kcm, sms 에이전트뿐) 스팬도 없어 계기와 거절이 관측되지 않음. 막힌 목록 15행과 같은 주입 |
| 4 | commerce 서비스의 Redis 연결 누수(오류 경로에서만 반납 누락)로 Redis 연결 한도 | 앱, commerce Redis(캐시 1) | Transloadit 2025-09 공식 블로그(us-east Redis 연결 고갈) | 버림: 원칙 2. commerce 는 최다 서비스(26)라 정답 위치 0 부품만 낼 수 있는데 캐시는 1 |
| 5 | banking account 새 릴리스가 이체 응답 구조를 바꿔 api 가 해석 실패 | 앱, banking account(L+J, 은행 계좌 서비스 3) | GitHub 2021-10-08 공식 월간 보고 | 남김(차순위): 막힌 목록 106행의 사유는 서비스 균형뿐이라 이제 버릴 사유가 아니다. 숫자 순(0 인 묶음, 0 인 정답 위치 없음 → 적은 서비스)에서 음식배달인 1 이 앞선다 |
| 6 | dispatch 새 릴리스가 용량 응답 구조를 바꿔 order 가 해석 실패 | 앱, food dispatch(L+J) | GitHub 2021-10-08 | 버림: 카탈로그 §1. F49-R(같은 원본, 같은 수단, 같은 호출자 order, 같은 진입과 피해 범위)의 피호출자 이름만 바꾼 복제가 되고, 실패 자리(용량 응답 해석)는 F32-H 와 같다 |
| 7 | banking ledger 소비자 새 릴리스가 이체 이벤트를 못 읽음 | 앱, banking ledger(L+J, 은행 원장 1) | Onfido 2025-01-24 공식 | 버림: 원칙 7. 원장 소비는 outbox 뒤 비동기라 사용자 5xx 가 없다(막힌 목록의 GitHub 2026-04-20 행과 같은 벽) |
| 8 | 데이터가 늘어 처리 시간이 시간 제한에 닿고 재시도가 장애를 유지 | 앱, banking transfer 거래 내역(N, 0) | AWS DynamoDB 2015-09-20 공식 | 버림: 원칙 9. 거래 내역 COUNT 는 약 0.2초라 order, api 의 시간 제한(5초 안팎)에 닿으려면 데이터가 수십 배여야 하고, 모든 동기 호출의 서킷브레이커가 재시도 증폭을 끊는다(F14-R, 막힌 N 행들과 같은 벽) |
| 9 | tb-w3 시각을 앞당겨 dispatch 만료가 한꺼번에 도는 시계 어긋남 | 인프라, 시계(0) | Cloudflare 2017-01-01 공식 | 버림: 원칙 1. 원본은 시간 역행이 음수 기간을 만든 panic 이고 앞당김 만료와 기전이 다르다. tb-w2 판은 막힌 목록 51행 |

DB 가 정답인 후보 0개, 부품 7종(dispatch, banking nginx, commerce Redis, banking account, banking ledger, banking transfer, 노드 시계), 앱과 인프라 두 층.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 롤아웃: `scripts/scenarios/profiles/k8s_image_executor.py` RELEASE_SCRIPT `run` — 109 docker 의 food-delivery-dispatch:1.7.0 을 tb-w3 containerd 에 올리고(`docker save | ssh nkia@<tb-w3> sudo ctr -n k8s.io images import -`) `kubectl set image deploy testbed-dispatch dispatch-service=food-delivery-dispatch:1.7.0`. F33-H, F49-H 와 같은 스크립트이고 계약만 더했다.
2. 배포: `food-delivery/k8s/22-dispatch-deploy.yaml:9`(replicas 1), `:19`(nodeSelector tb-w3), `:27-28`(image, imagePullPolicy Never), 기본 전략(maxSurge 25%). 109 kubectl 2026-10-10: replicas 1, generation 22, 파드 testbed-dispatch-6cbcbcbcf5-9nz45(재시작 0, 26시간), 109 docker 에 food-delivery-dispatch:1.6.0(F33-H)과 :latest 등이 있고 1.7.0 은 없음, tb-w3 containerd 에는 :latest 만, tb-w3 루트 파일시스템 53%.
3. 결함: `scripts/scenarios/fault-images/f43-p/dispatch-service.patch:7`(DispatchController.dispatchCourier 가 배차 전에 rejectDuplicateAssignment 를 부름), `:47-70`(rejectDuplicateAssignment: `Connection conn = dataSource.getConnection()` :54, 결과 집합과 문장만 닫고 :60-61, `conn.close()` 는 중복 갈래 :62-66 에만), `:94-143`(DuplicateAssignmentTest: 중복 갈래만 시험하고 conn.close 를 확인). 매니페스트 버전은 `food-delivery/dispatch-service/src/main/java/com/fooddelivery/dispatch/controller/DispatchController.java:25-28`(확인 없이 dispatchCourier). 확인이 트랜잭션 밖이라 배차 트랜잭션 연결과 겹치지 않는다.
4. 풀: `food-delivery/dispatch-service/src/main/resources/application.yml:12-17`(maximum-pool-size 10, minimum-idle 3, connection-timeout 3000). 빌린 연결은 autocommit 이라 MySQL 에서 Sleep 이다. Hikari 는 빌려 간 연결을 회수하지 않는다(leakDetectionThreshold 미설정). MySQL wait_timeout 기본 8시간이라 서버도 끊지 않는다.
5. 프로브: startupProbe /actuator/health 5s×30(`22-dispatch-deploy.yaml:67-72`), readiness 같은 경로 10s, 시간 제한 3s, 3회(`:73-79`), liveness 같은 경로 15s, 3s, 5회(`:80-86`). 풀이 마르면 DB 확인이 연결 대기 3초에 걸려 프로브 시간 제한(3초)과 같다.
6. 호출자: order `OrderService.createOrder` 의 용량 확인(`food-delivery/order-service/src/main/java/com/fooddelivery/order/service/OrderService.java:94-112`)이 주문 저장 전에 `DispatchClient.checkCapacity`(`.../order/client/DispatchClient.java:32-50`)를 부른다. 500 이나 연결 실패는 'Failed to check dispatch capacity: ...' ERROR, 재시도 3회(200ms 지수), 서킷(10건 창 50%, 5초 열림) 끝에 fallback 503 'Dispatch service unavailable: ...'(`order-service/src/main/resources/application.yml:55-58, 78-86, 105-111`). 주문 행은 남지 않는다.
7. DB 여유: food MySQL max_session 151, 평시 current 56~75, idle 16~36, active 1~12(12 는 F33-R 실행). 누수 중에는 dispatch 가 쥔 연결이 평시 약 4~5 에서 10 으로, 약 +5~7. 재시작하면 소켓이 닫혀 돌아간다.

### 로컬 실측 (2026-10-10, 원칙 9 근거)

origin/main(c5fde43)의 food-delivery 를 풀어 restaurant, order 는 그대로, dispatch 는 패치를 얹어 `mvn -o package` 로 jar 를 만들고(패치를 얹은 dispatch 시험 15건 통과, DuplicateAssignmentTest 포함) JDK 21 로 띄워 mysql:8.0(food init.sql, 배차 2만 행)에 붙였다. Kafka 와 payment 는 띄우지 않았다(outbox 발행 실패 WARN, 결제 단계 502 는 이 실측과 무관). 주문 POST 를 초당 1.5건, 180초 보내며 10초마다 dispatch health, order health, dispatch 용량 GET, MySQL processlist 를 봤다. kubelet 이 없어 재시작은 일어나지 않는다.

| 시점 | 관찰 |
|---|---|
| 0~약 7초 | 주문 9건이 배차까지 감(dispatch 'Dispatched courier' 9건, 결제 단계에서 502) |
| 10초부터 끝까지 | dispatch health 503(3.0초), 용량 GET 500(3.0초), order health 200(즉시). MySQL fooddelivery Sleep 세션 13 → 23 으로 고정 |
| 20~180초 주문 | 전부 503, p50 3.64초, 최대 3.66초(용량 확인 3초 대기 + 재시도 백오프) |

로그(같은 jar): dispatch ERROR 'HikariPool-1 - Connection is not available, request timed out after 3000ms (total=10, active=10, idle=0, waiting=N)' 288건, waiting 분포 0: 98, 1: 72, 2: 69, 3: 33, 4 이상 16. ERROR 'Servlet.service() ... CannotCreateTransactionException', 'Unexpected error occurred in scheduled task' 24건, WARN 'DataSource health check failed' 17건, 'Dispatch retention purge failed, will retry next cycle: Could not open JPA EntityManager for transaction'. order ERROR 'Failed to check dispatch capacity: 500 : "{...\"path\":\"/api/deliveries/capacity\"}"' 60건(운영 dispatch 는 오류 본문이 Whitelabel HTML 이다: 119 order 로그 'Failed to check dispatch capacity: 500 : "<html><body><h1>Whitelabel Error Page...'). order 는 내내 health 200.

이미지 빌드 경로 확인: `build.sh` 와 같은 순서로 origin/main 의 food-delivery 를 git archive 로 풀고 패치를 `git apply -p1` 한 뒤 `docker build --network=host -f food-delivery/dispatch-service/Dockerfile food-delivery` 를 로컬 임시 태그로 돌려 성공(임시 이미지는 지움). 109 docker 의 food-delivery-dispatch:1.7.0 은 배포 단계에서 `build.sh f43-p` 로 만든다.

운영과의 차이: 운영은 eclipse-temurin 17-jre, aarch64, OTel 에이전트, CPU 한도 500m. 누수는 배차 요청 수에만 달려 있어(1건에 1개) JVM 판, CPU 속도와 무관하다. 운영 기준선 배차는 초당 0.2~1.2건(119 'Dispatched courier' 로그 시간별, 2026-10-09), 동반 부하 2rps 가 주문 초당 약 1.4건을 더해 풀은 새 파드가 트래픽을 받은 뒤 약 5~10초 안에 마른다.

한 주기(추정): 재기동 약 40초(평시 롤아웃 KCM 2026-10-09 00:26:52 Pulled → 00:27:33 옛 ReplicaSet 축소) → Ready, 배차 약 10건(약 5~10초) → readiness 3회 실패(약 30초) 동안 Ready 이지만 DB 작업 실패(order 3.6초 503) → NotReady(order 즉시 503) → liveness 5회 실패(첫 실패부터 약 75초)에 재시작. kubelet 재시작 대기(10, 20, 40 … 최대 300초)가 붙어 회차가 갈수록 NotReady 구간이 길어진다.

## 5. 원인 규정 (원칙 5, 6)

- 근본(`root_cause.target_id`): `food-delivery-dispatch`(dispatch-service 의 새 릴리스). 결함을 가진 곳이자 되돌려야 재발이 막히는 곳.
- 계기(`trigger_target_id`): 비움. 계기(릴리스 롤아웃)와 결함이 같은 곳이다. 호출자(order, loadgen)의 요청은 평시 그대로 정당하다(원칙 6).
- 부분 점수: `food-delivery-order`(주문 503 이 드러나는 곳).
- 층위: 원본 사후 보고의 결론("배포한 변경이 DB 연결 누수 버그를 들여왔고, 재시작은 임시 완화, 누수 없는 버전이 해결")과 같은 층위. 패치 속 코드 줄을 맞히라고 요구하지 않는다(원칙 5, 결함 버전 이미지 규칙 7). 관제 데이터로 낼 수 있는 결론: "dispatch 를 1.7.0 으로 롤아웃한 직후부터 dispatch 의 연결 풀이 대기 요청 없이 꽉 차고 MySQL 은 한가하며, 재시작할 때마다 같은 일이 되풀이되고 설정과 한도는 그대로다 → 새 버전의 연결 누수, 롤백".

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 |
|---|---|
| 감지 | food-delivery-order 오류율(503), food-delivery-dispatch 오류율과 지연(3초), dispatch, order 오류 로그 급증, KCM Unhealthy, Killing, db.client.connections 지표 이상 |
| 피해 판정(러너) | 동반 부하 주문 생성 5xx 비율 ≥ 0.5 와 2xx 비율 < 0.2 가 3틱. 평시 주문 5xx 는 0 근처 |
| RCA | 롤아웃 이벤트와 새 이미지 태그, dispatch 연결 사용량이 max 에 붙음(대기 적음), 'Connection is not available (total=10, active=10, idle=0, waiting 작음)', 재시작 뒤 같은 상승의 반복, MySQL 은 idle 세션만 늘고 active, 응답 시간 평탄, 바뀌지 않은 설정과 한도 |

## 7. 관측 근거 표 (119 실조회, 2026-10-10 03:00~03:40 UTC)

표본 데이터(트레이스 10%, APM 스팬 지표)만으로 증명하지 않는다. 결정 증거는 KCM 이벤트(전수), 자원 이력, Hikari 런타임 지표(에이전트가 주기로 내보내는 지표, 표본과 무관), 로그(전수), DPM 세션 지표다.

| 증거 | 119 표와 칸 | 조회 | 평시 결과 |
|---|---|---|---|
| 계기: 롤아웃과 이미지 태그 | CH `lucida.kcm_events_local`(reason, object_name, body) | `reason='Pulled' AND object_name LIKE 'testbed-dispatch%'` 본문의 이미지별 수 | 49건 전부 'Container image "food-delivery-dispatch:latest" already present on machine'(2026-08-14 00:39 ~ 2026-10-09 00:26). 같은 30일 dispatch 이벤트: Unhealthy 1102, Pulled 47, Killing 46, ScalingReplicaSet 24(마지막 2026-10-09 00:27:33). 장애 때 1.7.0 이 처음 나타나야 한다 |
| 계기: ReplicaSet, 파드 스펙의 이미지 | PG lucida `kcm_resources_history`(kind, name, yaml, captured_at) | `namespace='rca-testbed-food' AND name LIKE 'testbed-dispatch%'` 종류별 수와 `position('food-delivery-dispatch:latest' in yaml)>0` 수 | replicaset 40건 전부 :latest(마지막 2026-10-09 00:27), pod 33건 전부 :latest(마지막 2026-10-09 00:26). 장애 때 1.7.0 ReplicaSet 이 남아야 한다 |
| 근본: 연결 사용량 | VM `db.client.connections.usage{service_name="food-delivery-dispatch",state="used"}`, `db.client.connections.max`, `db.client.connections.pending_requests`, `db.client.connections.timeouts`(host_name=파드, pool_name HikariPool-1) | 7일 파드별 max_over_time | used: 평시 파드 1~2, 10 은 두 파드(69ff6c4468-drvj7, 6cbcbcbcf5-9nz45, F33-R 실행 때). max 10. pending: 평시 0, F33-R 실행 파드 11~20. 현재 파드 used 0, idle 3, timeouts 누적 98(F33-R 실행). 누수 중에는 used 10 에 pending 0~3 이어야 한다 |
| 근본: 연결 고갈 로그 | CH `lucida.lucida_logs_local`(service_name, severity_text, body) | dispatch ERROR, WARN 8일(로그 보존 시작 2026-10-02) | 'Connection is not available, request timed out after 3000ms (total=10, active=10, idle=0, waiting=4..13)' 이 2026-10-03~10-09 시나리오 실행 때만(waiting 은 4~13 이 대부분), 'DataSource health check failed' 67건, 'Unexpected error occurred in scheduled task' 104건(MySQL read_only 실행 F38-R 포함). 누수 때는 waiting 0~3(로컬 실측 98% 가 3 이하) |
| 참고: 버전 표시 | VM, CH 의 `service_version` | dispatch 지표, 로그 | 1.0.0. 릴리스는 소스만 바꾸고 pom 버전은 그대로라 1.7.0 에서도 1.0.0 이다. 버전 변화는 이미지 태그(KCM)로만 보인다 |
| 감별: MySQL 은 한가함 | VM `dpm.mysql.session.{current,idle,active}_session`, `max_session` | 7일 min/max/최근 | current 56~75(최근 56), idle 16~36(최근 20), active 1~12(최근 1, 12 는 F33-R 실행), max 151. 누수 중에는 idle 만 약 +5~7 이고 active 는 평시 그대로일 것(F33-R 은 active 1 → 11, avg_query_response_time 0.5 → 2.8) |
| 감별: Top SQL | CH `lucida.dpm_topsql_local` | 장애 구간 | 새 질의는 `SELECT count(*) FROM dispatches WHERE order_id = ? AND status = 'ASSIGNED'`(idx_dispatches_order, 배차마다 1회, 1ms 미만)뿐이고 평시 상위 질의의 rowExamined 가 그대로여야 한다(F33-H 는 COUNT 의 rowExamined 수십만) |
| 전파: 재시작 | CH `lucida.kcm_events_local` | 위 첫 줄 | dispatch Unhealthy, Killing 은 수집된다(F33-R 실행, 2026-10-09 03:37~03:44 readiness 실패 6회) |
| 전파: 호출자 | CH `lucida.lucida_logs_local` | `service_name='food-delivery-order' AND body LIKE 'Failed to check dispatch capacity%'` 8일 | 'Failed to check dispatch capacity: 500 : "<html><body><h1>Whitelabel Error Page...' 313건(2026-10-03~10-09), '... I/O error ... Read timed out' 292건(F33-R 실행). 장애 때 분당 수십 건 |

## 8. 감별

- must_support: 롤아웃과 1.7.0 태그(과거 기록은 전부 :latest), dispatch 연결 사용량이 max 10 에 붙고 pending 0 근처, 'Connection is not available (total=10, active=10, idle=0, waiting 작음)', 'DataSource health check failed', 'Unexpected error occurred in scheduled task', 같은 파드 Unhealthy 와 Killing 반복, MySQL idle 만 증가하고 active, 응답 시간 평탄, order 'Failed to check dispatch capacity: 500'(NotReady 뒤 'Connection refused')와 주문 503.
- must_rule_out: MySQL 포화나 느린 질의(F33-R, F33-H), 배차 한도 설정, 응답 형식(F32-R, F32-H), order 쪽 릴리스(F49-H), MySQL 다운, 읽기 전용, 계정(F25-H 꼴, F38-R), dispatch 자원, 프로브 설정(F05-H 꼴), 가게 쪽(F49-R, F36-R, F44-R).
- contrast_with: F43-R, F33-R, F33-H, F32-R, F32-H, F05-H.
- 관계: F33-R, F33-H 와 같은 증상(dispatch 'Connection is not available', order 503)이고 원인이 다르다. 서비스 입도 채점에서는 F33-H(정답 dispatch)와 정답이 같으므로 이 짝의 판별력은 mechanism 과 must_support(MySQL active 세션과 Top SQL, Hikari 대기 스레드 수)에 있다. F33-R(정답 DB 테이블(배차))과는 정답 자체가 다르다.

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

- 강도는 릴리스 하나로 고정(approved-fixed-f43-p, 실행기 파라미터 = 이미지 태그). 누수 속도는 배차 수로 정해지고(1건에 1개), 풀이 마르는 데 배차 약 10건이면 충분해 요청률, 시각과 무관하게 결정적이다. 동반 부하는 판정 표본과 시간대 무관한 누수 속도를 위해 둔다.
- 동반 부하: load.north_south food order-surge.js 2rps(주문 초당 약 1.4건), ramp 2m + hold 21m + ramp_down 15s, entry 30181. F49-H 와 같은 값.
- success: order_create_5xx_rate ≥ 0.5 와 order_create_2xx_rate < 0.2, 3틱(틱 15초). 2xx 는 재시작 직후 새 풀로 받는 약 10건뿐이라 한 주기(재기동 약 40초 + 고갈 뒤 약 75초 + 재시작 대기)에서 2xx<0.2 창이 3틱 넘게 이어진다.
- min_hold 15m, timeout 20m, max_injection_duration 25m: 롤아웃 약 1분 뒤 첫 실패, 15분 동안 재시작 서너 번이 녹화 구간에 들어간다(원본의 간헐 장애).
- must_rule_out: achieved_rps < 0.5(동반 부하 2rps 의 1/4, ramping-arrival-rate 라 주문이 3~4초 걸려도 유지), food MySQL 파드 NotReady(2틱).
- abort: entry_status == 0(2틱). 진입점은 order, restaurant NodePort 이고 둘 다 이 주입과 무관하게 Ready 다. 로컬 실측에서 order health 는 내내 200 이었다. 위험 계산: order 가 용량 확인 동안 자기 DB 연결을 쥐는 시간은 Ready 인 빈 풀 구간에서 주문당 약 3.6초, 기준선 최대 초당 약 1.5건(분 최대 1.47) + 동반 1.4건 = 약 2.9건 × 3.6초 ≈ 10.4개로 order 풀 15 아래다. 그 구간은 주기마다 약 30초뿐이고, 나머지(NotReady, 재시작) 구간은 연결 거부로 즉시 끝난다. F33-R(같은 dispatch 풀 고갈, 3초 대기)에서도 order 는 연결 가능 상태를 지켰다(order liveness 실패 8회는 있었으나 재시작 0).
- recovery: target_health 200, MySQL Ready, 기준선 주문 2xx 비율 ≥ 0.7(2틱, 10분).
- cleanup: 이미지 원복, available(180초), 1.7.0 을 쓰는 파드가 없어진 뒤 tb-w3 containerd 에서 1.7.0 의 이름 참조와 ID 참조를 모두 지우고 둘 다 없는지 확인. 원복한 새 파드는 빈 풀로 시작하고 더 새지 않는다. 데이터 부작용 없음(새는 것은 SELECT count 연결뿐, 거절된 주문은 아무것도 저장하지 않음). 고갈 동안 실패한 만료 배치는 복구 뒤 첫 주기(30초)에 밀린 ASSIGNED 를 DELIVERED 로 넘긴다.
- 노드 디스크: tb-w3 루트 53%(2026-10-10). 릴리스는 기본 이미지와 같은 eclipse-temurin 기반이라 새로 쓰는 것은 앱 층뿐이다. preflight 가 80% 미만을 요구한다.
- DB 여유: 누수 중 MySQL 세션 약 +5~7, max 151 에 한참 못 미친다. 다른 서비스 풀이 함께 늘어날 일은 없다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Octopus Deploy 2025-11-25 공식 상태 페이지 사후 보고)와 요소별 대응표, 기전 동일(§2)
- [x] 분류 장부 갱신(§4-1 행, §6 기록), 묶음 J+E(J 6→7, 61 중 11.5%), 정답 위치 배달 서비스(3→4, 6.6%), 결제 경로 10(16.4%), 서비스 음식배달 16→17(가장 적음), 고른 이유(§3). 어느 축도 20% 미만
- [x] 근본 원인 위치 `file:line` 확인(패치 줄, dispatch 풀 설정, order 클라이언트와 서비스)과 인프라 지점(22-dispatch-deploy.yaml)(§4)
- [x] 근본 원인 흔적 119 실조회: kcm_events_local 의 dispatch 롤아웃과 Pulled 본문 태그, kcm_resources_history 의 dispatch ReplicaSet, 파드 이미지, db.client.connections.* 지표(§7)
- [x] 핵심 증거가 표본에만 있지 않음: KCM 이벤트, 자원 이력, Hikari 런타임 지표, 전수 로그, DPM 세션 지표. 스팬은 보조
- [x] 계기 흔적: 롤아웃 이벤트와 새 이미지 태그. 인공 지연 없음(지연은 연결 대기 시간 제한 3초, 그 이유는 고갈된 풀)
- [x] 정답이 관제 데이터로 낼 수 있는 결론(자기 롤아웃 직후 DB 는 한가한데 풀만 참, 재시작마다 반복), 코드 설계 결함 추론 불필요(§5)
- [x] 정답지 세 칸이 원칙 6 대로(근본 dispatch 릴리스, 계기 같음, 부분 점수 order)
- [x] 감지, 피해 판정, RCA 증거 구분(§6)
- [x] 주입 도구가 시나리오 id 를 남기지 않음: 실행기 인자는 네임스페이스, Deployment, 컨테이너, 이미지 둘뿐이고, 패치가 더한 문자열에 시나리오 id, fault, bug, chaos 같은 말이 없음(테스트 `test_k8s_image_release_mode_rolls_food_dispatch_second_release` 가 확인)
- [x] 부하 상한과 서킷브레이커를 고려한 피해 계산(§9: 서킷이 열려도 fallback 503, 결정적 고갈, 로컬 실측 주문 전량 503, order 풀 여유 계산)
