---
title: F43-R 설계 시트 (commerce cart-service 를 DB 연결이 새는 릴리스로 롤아웃)
status: Draft
owner: project
last_reviewed: 2026-10-09
tags:
  - scenario
  - design
  - release
summary: commerce cart-service 를 결함 있는 새 릴리스(commerce-cart:1.2.0, fault-images/f43-r 패치로 만든 별도 태그)로 롤아웃하면, 새로 더한 장바구니 품목 수 상한 확인이 담기마다 풀 연결을 빌려 돌려주지 않아 Hikari 풀(20)이 몇 초 만에 마르고, cart 의 DB 작업과 health 가 시간 초과되어 readiness 가 빠지고 liveness 재시작이 되풀이되며 checkout 이 502 로 실패하는 시나리오. 원본은 Octopus Deploy 2025-11-25 인가 서비스 배포가 들여온 DB 연결 누수.
---

# F43-R 설계 시트

## 1. 요약

commerce cart-service 의 새 릴리스 1.2.0 은 장바구니 품목 수 상한(cart.max-items, 50)을 더했다. 담기 트랜잭션을 열기 전에 품목 수를 세는데, 일반 JDBC 로 `try (PreparedStatement ps = dataSource.getConnection().prepareStatement(...))` 를 쓴다. try-with-resources 가 닫는 것은 문장뿐이고 그 문장을 만든 연결은 닫지 않아, 담기마다 Hikari 풀의 연결 하나가 빌린 채로 남는다. 풀 크기(20), 대기 시간(3초), 프로브, 자원 한도는 그대로라 담기 약 20번 뒤 풀이 total=20, active=20, idle=0 으로 굳고, 처리 중인 요청이 없어도 빈 연결이 없다. 그 뒤 cart 의 DB 작업은 모두 3초 대기 끝에 실패하고 DB 확인이 든 /actuator/health 가 시간 초과되어 readiness 가 파드를 엔드포인트에서 빼고 liveness 가 컨테이너를 재시작한다. 재시작은 쥐고 있던 연결을 닫아 잠깐 살리지만 같은 릴리스가 다음 담기 20번에 풀을 또 잃는다. PostgreSQL 은 cart 가 쥔 idle 세션이 늘 뿐 바쁘지 않다.

비유: 공용 펜 20자루를 둔 창구에서, 새로 온 직원이 서류를 확인할 때마다 펜을 하나씩 주머니에 넣고 돌려놓지 않는다. 20번째 손님 뒤로는 펜이 없어 창구가 멈추고, 직원을 교대시키면(재시작) 주머니가 비워져 잠깐 돌아가다 또 멈춘다. 펜이 모자란 것도, 손님이 많은 것도 아니다. 고칠 곳은 새로 온 직원(릴리스)이다.

## 2. 원본 사례

- **Octopus Deploy, 2025-11-25** (공식 상태 페이지 사후 보고, 게시 2025-12-08): https://status.octopus.com/incidents/q5mhnrsm7q6j
- 사고 "OctopusID signin intermittent for cloud customers". 11-25 12:43 pm(AEST) 인가(authorization) 서비스에 변경을 배포했고 "This change introduced a bug resulting in database connection leaks." 그 결과 "a database connection leak, which caused some sign-in requests to time out". 증상은 간헐적이었고 일부 고객에게는 약 5분 뒤 저절로 풀린 것처럼 보였다. 10:24 pm 고객 보고로 사고 선언(배포부터 9시간 41분), DB 연결 시간 초과까지 추적, 11:42 pm "restarting services to release database connections" 로 임시 완화, 다음 날 아침 근본 원인을 연결 누수로 확인하고 누수를 없앤 버전을 배포해 해결. 테스트가 놓친 이유는 "The connection leak only became apparent in high-traffic scenarios, which our current test suite doesn't replicate."
- 자료 문서 M2 표에 이 사례를 더했다(출처가 말한 사실만).

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 인가 서비스에 변경 배포 | cart-service 를 릴리스 commerce-cart:1.2.0 으로 롤아웃(KCM ScalingReplicaSet, 새 파드 Pulled 이벤트에 태그가 남음) |
| 원인이 된 결함 | 배포한 변경이 DB 연결 누수 버그를 들여옴 | 새 품목 수 확인이 빌린 연결을 닫지 않음(문장만 닫는 try-with-resources) |
| 전파 경로 | 연결 누수 → 연결 고갈 → 로그인 요청의 DB 작업 시간 초과 | 담기마다 1개 누수 → 풀(20) 고갈 → cart 의 모든 DB 작업 3초 대기 실패 → health 의 DB 확인 시간 초과로 readiness 이탈, liveness 재시작 → 호출자(gateway, order) 502 |
| 사용자 증상 | 로그인이 간헐적으로 시간 초과, 일부는 몇 분 뒤 저절로 풀린 것처럼 보임 | 담기, 비우기 500(3초), 502, checkout 502. kubelet 재시작 직후 잠깐 되다가 다시 끊기는 간헐 장애 |
| 원본의 탐지 경로 | 고객 보고, DB 연결 시간 초과 추적 | lucida-next 오류율, cart Hikari 지표(db.client.connections.*), cart 오류 로그, KCM Unhealthy, Killing |
| 완화와 복구 | 서비스 재시작으로 연결을 풀어 임시 완화, 누수 없는 버전 배포로 해결 | liveness 재시작이 임시로 풀지만 되풀이, cleanup 이 이미지를 매니페스트 이미지로 되돌림(롤백) |

기전 일치: 원인(배포한 코드가 DB 연결을 돌려주지 않음)에서 증상(연결 고갈로 그 서비스의 DB 작업이 시간 초과, 재시작하면 잠깐 풀림)까지 고리가 같다. 원본은 누수 코드를 밝히지 않아, 자바 서비스에서 흔한 꼴(풀 연결로 만든 문장만 닫는 try-with-resources)을 한 기능 크기로 둔다.

바꾼 것(기전 고리 밖):
- 스택: 원본 서비스 스택 미상 → Spring Boot, HikariCP, PostgreSQL.
- 속도: 원본은 높은 트래픽에서만 드러나 탐지까지 9시간 41분이 걸렸다. 테스트베드 cart 는 담기마다 새므로 기준선 트래픽만으로 수 초~수십 초에 풀이 마른다. 그래서 '배포와 증상 사이가 멀어 배포를 의심하지 않는' 함정은 재현하지 않는다.
- 재시작 주체: 원본은 사람이 재시작했다. 테스트베드는 health 의 DB 확인이 실패해 kubelet 이 재시작한다(원본의 '재시작하면 연결이 풀린다'는 성질은 그대로).
- 서비스: 인가 서비스 대신 장바구니 서비스. 원본도 영향이 '그 서비스를 쓰는 기능(로그인)'에 한정됐고, 테스트베드도 장바구니를 쓰는 기능(담기, checkout)에 한정된다.

## 3. 숫자 근거 (2026-10-09, `scenario-stats.py`, 정식 + 후보 합계 49, 이 후보 전)

- 묶음: A 7, B 7, D 7(각 14%), C 6, G 6(12%), F 3, J 3, L 3, E 2, H 2, I 1, M 1, O 1, K 0, N 0. J 는 현실 트리거 1위(Google 바이너리 배포 37%)인데 후보 3개뿐이다. 이 후보 J 3→4(8%), 피해 모양으로 E 를 함께 적는다(E 로 세도 2→3, 6%). K, N 은 0 이지만 막힌 시도가 많다(아래 후보 7, 8).
- 정답 위치: 외부 결제 의존 6, 주문 서비스 6(각 12%), 은행 이체 서비스 5(10%), 결제 경로 합계 10(20%, 금지). **장바구니 서비스 0** → 1(2%), §2-1 에 새 말로 더함.
- 서비스: 음식배달 10, 은행 14, 쇼핑몰 25. 쇼핑몰 25 → 26. 원칙 2 의 완화 규칙(가장 많은 서비스도 정답 위치가 0 인 부품이면 낸다)에 따른다. 부품 지도의 commerce cart 0.
- 왜 이 후보인가: 음식배달(10)을 먼저 봤으나 같은 기전을 걸 자리가 막혔다. order 는 loadgen 생성 입구이고 readiness, liveness 가 DB 를 봐 풀이 마르면 입구가 연결 불가(entry_status 0, 필수 중단 조건), restaurant 는 주문 여정의 메뉴 조회 입구라 표본이 사라지고(rejected 의 restaurant OOM 행과 같은 벽), payment 는 결제 경로 금지, notify 는 DB 가 없다. dispatch 는 풀이 마르면 order 가 503 'Dispatch service unreachable' 로 거절해 F32-R, F32-H, F33-R 과 같은 증상이 넷째가 되고, 장부의 F32-H 평가 메모가 다음 food 후보는 부품을 바꾸라고 권고한다. 이번 실행의 앞 두 반복이 모두 은행(F41-R, F42-R)이라 은행도 피했다. cart 는 정답 위치와 부품 모두 0 이고, 진입점(nginx → gateway)이 따로 있어 cart 가 죽어도 entry_status 는 200 이다.

### 후보 목록 (3단계, 기존 목록이 아니라 기전 × 0 부품, 인프라 층에서 시작)

| # | 후보 | 층, 부품 | 원본 사례 | 결과 |
|---|---|---|---|---|
| 1 | cart 새 릴리스의 DB 연결 누수, 풀 고갈과 재시작 반복 | 앱, commerce cart(J) | Octopus Deploy 2025-11-25 공식 상태 페이지 | **채택** |
| 2 | dispatch 새 릴리스의 DB 연결 누수 | 앱, food dispatch(J) | 같은 원본 | 버림: 음식배달이 가장 적지만 증상이 order 503 으로 F32-R, F32-H, F33-R 과 넷째로 겹침(F32-H 평가 메모). 음식배달의 다른 자리는 위 '왜 이 후보인가'의 벽 |
| 3 | 새 릴리스의 정규식 역추적으로 CPU 포화 | 앱, food restaurant 또는 commerce product(J) | Stack Exchange 2016-07-20(원문 상태 페이지는 못 열고 2차 인용만 확인) | 버림: 원칙 1(2차 출처뿐), 원본 계기는 공백 2만 자 게시물(데이터)이고 정규식은 이미 있던 코드라 릴리스 + 데이터 두 계기 복합, 이름 열이 VARCHAR(128)이라 다항 역추적이 피해를 못 냄(원칙 9) |
| 4 | 새 버전의 무한 루프로 프런트엔드가 요청을 못 받음 | 앱(J) | Azure Storage 2014-11-18 공식 최종 RCA | 버림: 원본 계기는 설정 스위치를 잘못 켠 것(잠재 결함 + 설정 배포 복합)이고, 테스트베드에선 요청 스레드가 CPU 한도를 묶어 주기 재시작이 되는 F41-R 꼴과 겹침. 단일 계기 후보 1이 앞섬 |
| 5 | food order 새 릴리스가 NULL 값에서 예외 | 앱, food order(J) | Zendesk 지원 문서(새 구현이 NULL 처리 실패) | 버림: 원칙 1(원본은 오탐 정지라 5xx 장애가 아님, 웹 검색 1회에 공식 5xx 사례 없음), order 는 생성 입구 |
| 6 | 새 버전 Kafka 소비자가 특정 메시지에서 멈춤 | 앱, banking ledger(J) | PostHog 2026-07-23(2차) | 버림: 원칙 1(2차), 원칙 7(비동기라 사용자 증상 없음, rejected 27 과 같은 벽) |
| 7 | 워커 노드 경로, MTU, conntrack 변경으로 파드 통신 손실 | 인프라, 네트워크(K) | Datadog 2023-03-08 등 | 버림: rejected 14, 25, 33, 38, 44 와 같은 원본, 같은 주입 |
| 8 | 게이트웨이 새 릴리스의 백오프 없는 재시도 확대 | 앱, commerce gateway(N) | AWS 2021-12-07 공식 | 버림: rejected 45 와 같은 주입(하류 실패라는 두 번째 계기가 필요) |
| 9 | 새 버전 롤아웃 중 CPU 압박으로 startupProbe 크래시 루프 | 인프라, 프로브와 재시작 정책 | PostHog 2025-10-28 공식 | 버림: rejected 42(maxSurge 25% 가 옛 파드를 남김) |
| 10 | 코드의 고정 한도를 데이터가 넘어 panic | 앱(J) | Cloudflare 2025-11-18 공식 | 버림: 원본 계기는 DB 권한 변경으로 커진 피처 파일(데이터)이고 한도 코드는 이전부터 있음(rejected 75 가 다룬 원본), 복합 계기 |

DB 가 정답인 후보는 0개, 부품 8종(cart, dispatch, restaurant/product, order, ledger, 워커 네트워크, gateway, 프로브), 앱과 인프라 두 층을 모두 냈다.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 롤아웃: `scripts/scenarios/profiles/k8s_image_executor.py` RELEASE_SCRIPT `run` — 109 docker 의 commerce-cart:1.2.0 을 tb-w1 containerd 에 올리고(`docker save | ssh nkia@<tb-w1> sudo ctr -n k8s.io images import -`) `kubectl set image deploy testbed-cart cart-service=commerce-cart:1.2.0`. 노드는 Deployment 의 nodeSelector 에서 읽는다(F41-R, F42-R 과 같은 스크립트, commerce 네임스페이스 허용만 더함).
2. 배포: `commerce/k8s/26-cart-service.yaml:9`(replicas 1), `:19`(nodeSelector tb-w1), `:27-28`(image, imagePullPolicy Never), 전략은 기본값(maxSurge 25%, maxUnavailable 25%)이라 새 파드가 Ready 가 된 뒤 옛 파드가 내려간다(109 kubectl 2026-10-09: replicas 1, generation 16, 파드 testbed-cart-d77865575-b65t7 59일, 노드 이미지 ID 와 109 docker commerce-cart:latest ID 같음).
3. 결함: `scripts/scenarios/fault-images/f43-r/cart-service.patch:85-95`(countItems — `try (PreparedStatement ps = dataSource.getConnection().prepareStatement("SELECT count(*) FROM cart_schema.cart_items WHERE cart_id = ?"))`, 문장과 결과 집합만 닫음), `:68-76`(checkItemLimit — 담기 트랜잭션 밖에서 cartRepository.findByUserId 뒤 countItems), `:7`(CartController.addItem 이 먼저 checkItemLimit 을 부름). 매니페스트 버전은 `commerce/cart-service/src/main/java/com/commerce/cart/controller/CartController.java:24-27`(확인 없이 cartService.addItem). 확인이 트랜잭션 밖이라 담기 트랜잭션 연결과 겹치지 않고 20개가 모두 샌다(트랜잭션 안에 두면 19개만 새고 마지막 하나가 돌아 health 가 살아남는다: 로컬 1차 실측에서 확인).
4. 풀: `commerce/cart-service/src/main/resources/application.yml:11-15`(maximum-pool-size 20, minimum-idle 5, connection-timeout 3000). 빌린 연결은 autocommit 이라 PostgreSQL 에서 idle 세션이다. Hikari 는 빌려 간 연결을 회수하지 않는다(leakDetectionThreshold 미설정).
5. 프로브: readiness `/actuator/health`(DB 구성 요소 포함, redis 제외, period 10s, timeout 3s, 3회, `:75-81`), liveness 같은 경로(period 15s, timeout 3s, 5회, `:82-88`), startupProbe 5s×30(`:69-74`). 풀이 마르면 DB 확인이 연결 대기 3초에 걸려 프로브 시간 초과(3초)와 같다.
6. 하류: gateway `ProxyService.forwardToCart`(`commerce/api-gateway/src/main/java/com/commerce/gateway/service/ProxyService.java:63-67`)가 cart 500 은 그대로 넘기고 연결 실패는 재시도, 서킷 끝에 `unavailable`(`:103-105`) WARN 'Downstream unavailable for ...' 와 502. order `CartClient.getCart`(`commerce/order-service/src/main/java/com/commerce/order/client/CartClient.java:26-48`)는 재시도, 서킷 끝에 'Cart service unavailable' 502(order 는 이 예외를 로그로 남기지 않는다).
7. DB 여유: commerce PostgreSQL max_connections 100, 평시 세션 61~66(dpm.postgresql.session.current_session, 7일 중 89 는 2026-10-06 11:30 잠금 시나리오 실행 한 번). cart 는 평시 5개를 쥐고 누수 뒤 20개가 되므로 약 +15, 합 약 77. 재시작하면 프로세스가 닫혀 세션이 돌아간다.

### 로컬 실측 (2026-10-09, 원칙 9 근거)

패치를 얹은 commerce 트리에서 `mvn -o -pl cart-service -am package` 로 jar 를 만들고 JDK 21(x86_64)로 띄워 PostgreSQL 16(alpine, 운영 매니페스트와 같은 판)과 Redis 7 에 붙였다. 담기 POST 와 비우기 DELETE, 캐시에 없는 장바구니 GET, /actuator/health(3초 제한)를 약 0.7초 간격으로 보냈다.

| 담기 횟수 | 관찰 |
|---|---|
| 1~20 | 담기, 비우기, 조회 200(수십 ms), PostgreSQL 의 cart 세션이 5 → 20 으로 담기마다 하나씩 늘고 모두 idle |
| 21번째부터 | 담기, 비우기가 3.0초 뒤 500, /actuator/health 가 3초 시간 초과, PostgreSQL cart 세션 20 으로 고정 |
| 이후 5분 | 그대로(스스로 회복하지 않음). 캐시에 없는 조회도 몇 분 뒤 5초 클라이언트 제한에 걸림 |

로그(같은 jar): ERROR 'Servlet.service() for servlet [dispatcherServlet] ... threw exception [Request processing failed: org.springframework.dao.DataAccessResourceFailureException: Unable to acquire JDBC Connection [HikariPool-1 - Connection is not available, request timed out after 3000ms (total=20, active=20, idle=0, waiting=0)] [n/a]]', 'CannotCreateTransactionException: Could not open JPA EntityManager for transaction', WARN 'DataSource health check failed'(25건). waiting 은 0~1 이다.

1차 실측(확인을 담기 트랜잭션 안에 둔 판)에서는 19개만 새고 트랜잭션 연결 하나가 돌아 담기만 실패하고 health 와 조회는 살았다. 그러면 checkout 은 빈 장바구니 400 이 되어 '4xx 업무 거절 단일 신호'(rejected F34-R 교훈)에 걸리므로 확인을 트랜잭션 밖으로 옮겼다(실제로도 흔한 '트랜잭션 전에 검증' 꼴).

운영과의 차이: 운영은 eclipse-temurin 17-jre, aarch64(Dockerfile, tb-w1)이고 OTel 에이전트가 붙는다. 누수는 요청 수에만 달려 있어(담기 1번에 1개) JVM 판이나 CPU 속도와 무관하다. 운영 기준선 담기는 초당 약 1.2건(119 트레이스 10% 표본 422건/시간 × 10), 동반 부하 3rps 의 checkout 여정 담기 약 1.5건/초가 더해져 풀은 새 파드가 트래픽을 받은 뒤 약 10초 안에 마른다.

한 주기(추정): 재기동 약 40~60초(startupProbe 통과) → 담기 약 20번(약 7~15초) → readiness 3회 실패(약 30초) 동안 Ready 이지만 DB 작업 실패 → NotReady → liveness 5회 실패(첫 실패부터 약 75초)에 재시작. kubelet 재시작 대기(0, 10, 20, 40 … 최대 300초)가 붙어 회차가 갈수록 NotReady 구간이 길어진다.

## 5. 원인 규정 (원칙 5, 6)

- 근본(`root_cause.target_id`): `commerce-cart`(cart-service 의 새 릴리스). 결함을 가진 곳이자 되돌려야 재발이 막히는 곳.
- 계기(`trigger_target_id`): 비움. 계기(릴리스 롤아웃)와 결함이 같은 곳이다. 호출자(gateway, order, 사용자, loadgen)의 요청은 평시 그대로 정당하다(원칙 6).
- 부분 점수: `commerce-order`, `commerce-gateway`(증상이 드러나는 곳).
- 층위: 원본 사후 보고의 결론("배포한 변경이 DB 연결 누수 버그를 들여왔고, 재시작은 임시 완화, 누수 없는 버전이 해결")과 같은 층위. 패치 속 코드 줄을 맞히라고 요구하지 않는다(원칙 5, 결함 버전 이미지 규칙 7). 관제 데이터로 낼 수 있는 결론: "cart 를 1.2.0 으로 롤아웃한 직후부터 cart 의 연결 풀이 처리 중 요청 없이 꽉 차고 DB 는 한가하며, 재시작할 때마다 같은 일이 되풀이되고 설정과 한도는 그대로다 → 새 버전의 연결 누수, 롤백".

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 |
|---|---|
| 감지 | commerce-cart 서버 스팬 오류율과 지연(3초), gateway, order 오류율, cart 오류 로그 급증(평시 0), KCM Unhealthy, Killing, db.client.connections 지표 이상 |
| 피해 판정(러너) | 동반 부하 checkout 5xx 비율 ≥ 0.5 와 2xx 비율 < 0.3 이 3틱. 평시 checkout 5xx 는 0 근처 |
| RCA | 롤아웃 이벤트와 새 이미지 태그, cart 연결 사용량이 max 에 붙음(대기 적음), 'Connection is not available (total=20, active=20, idle=0, waiting=0)', 재시작 뒤 같은 상승의 반복, PostgreSQL 은 idle 세션만 늘고 한가함, 바뀌지 않은 설정과 한도 |

## 7. 관측 근거 표 (119 실조회, 2026-10-09 11:00~11:40 UTC)

| 증거 | 119 표와 칸 | 조회 | 평시 결과 |
|---|---|---|---|
| 계기: 롤아웃과 이미지 태그 | CH `kcm_events_local`(reason, object_name, body) | `namespace='rca-testbed-commerce' AND object_name LIKE 'testbed-cart%'` reason 별 | Pulled 4건 'Container image "commerce-cart:latest" already present on machine'(태그가 본문에 담김), Unhealthy 109건(2026-08-20~31, 'Liveness probe failed ... context deadline exceeded', 노드 장애 때), Killing 2건. 2026-08-31 뒤로 cart 이벤트 없음. commerce ScalingReplicaSet 은 수집됨(마지막 2026-10-02) |
| 계기: 새 ReplicaSet 스펙 | PG `kcm_resources_history`(kind, name, yaml, captured_at) | `namespace='rca-testbed-commerce' AND name LIKE 'testbed-cart%'` 종류별 집계와 'commerce-cart:latest' 수 | ReplicaSet 33건, 파드 22건 전부 :latest(마지막 2026-08-11). commerce ReplicaSet 수집은 2026-10-02, 파드는 2026-10-06 까지 됨 |
| 근본: 연결 사용량 | VM `db.client.connections.usage{service_name="commerce-cart", state="used"}`, `db.client.connections.max`, `db.client.connections.pending_requests`, `db.client.connections.timeouts`(host_name=파드, pool_name HikariPool-1) | 7일 max_over_time, timeouts 14일 3시간 간격 | used 최대 2, max 20, pending 최대 0, timeouts 누적값 3427 이 2026-10-01 부터 그대로(증가 없음). 에이전트가 10초마다 내보내는 런타임 지표라 트레이스 표본과 무관 |
| 근본: 연결 고갈 로그 | CH `lucida_logs_local`(service_name, severity_text, body) | cart ERROR, WARN 7일(로그 보존 시작 2026-10-02 08:00) | 2건뿐(2026-10-06 중복 키 1건과 그 SQL WARN). 'Connection is not available', 'DataSource health check failed' 는 cart 0건. 같은 문장은 inventory 에서 수집된 적이 있음('HikariPool-1 - Connection is not available, request timed out after 30000ms (total=10, active=10, idle=0, waiting=190)', 'DataSource health check failed' 70건, 2026-10-02~06): 잠금 때는 waiting 이 크고, 누수 때는 waiting 이 0~1 이다(로컬 실측) |
| 참고: 버전 표시 | VM, CH 의 `service_version` | cart 지표, 로그 | 1.0.0. 릴리스는 소스만 바꾸고 pom 버전은 그대로라 1.2.0 에서도 1.0.0 이다. 버전 변화는 이미지 태그(KCM)로만 보인다 |
| 감별: DB 는 한가함 | VM `dpm.postgresql.session.{current,idle,active,blocked,blocking}_session`, `max_session` | 7일 min/max | current 61~89(89 는 2026-10-06 11:30 잠금 실행 한 번, 그 밖 61~66), idle 45~64, active 1~3, blocked 0~1, blocking 0~30(잠금 실행), max 100. 누수 중에는 idle 만 약 15 늘고 active, blocked 는 그대로일 것 |
| 감별: DB 세션 상세 | CH `dpm_session_local` | 상태 분포 30일 | DPM 은 PostgreSQL 의 active, idle in transaction, blocking, blocked 세션만 남기고 idle 은 남기지 않는다. 누수 연결은 autocommit idle 이라 세션 표에는 안 보이고, 세션 수 지표(위)로만 보인다 |
| 전파: 재시작 | CH `kcm_events_local` | 위 첫 줄 | cart liveness 실패와 Killing 은 수집된다(2026-08-20 노드 장애 때) |
| 전파: 호출자 | CH `lucida_logs_local` | commerce-gateway body LIKE '%/api/carts%', commerce-order body LIKE '%Cart service%' 7일 | 0건, 0건. 고장 때 gateway WARN 'Downstream unavailable for POST /api/carts/...' 가 남는다(order 는 예외를 로그로 남기지 않음, 응답 본문과 표본 트레이스에만) |

표본 데이터(트레이스 10%, APM 스팬 지표)만으로 증명하지 않는다. 결정 증거는 KCM 이벤트(전수), Hikari 런타임 지표(에이전트 지표, 전수), 로그(전수), DPM 세션 수 지표다.

## 8. 감별

- must_support: 롤아웃과 1.2.0 태그, cart 연결 사용량이 max 20 에 붙음(평시 최대 2)과 timeouts 증가, 'Connection is not available (total=20, active=20, idle=0, waiting 작음)' 과 'DataSource health check failed', Unhealthy 와 Killing 반복, PostgreSQL idle 만 증가하고 active, blocked 평탄, gateway 'Downstream unavailable' 와 checkout 5xx.
- must_rule_out: PostgreSQL 느린 질의나 잠금(F33-P, F06-H, F01-R: 차단 세션, CPU 상승 없음, 다른 서비스 풀 낮음), PostgreSQL 장애(F25-H), Redis(F11-R), 인증 경로(F16-H, F33-P), 부하 증가, 설정 변경.
- contrast_with: F33-P, F41-R, F11-R, F25-H.
- 러너 success 가 F33-P 와 같다(checkout 5xx). 녹화 검증이 cart 의 'Connection is not available'(total=20, active=20, 대기 적음)과 PostgreSQL 이 한가함(CPU, active 세션 평탄, 'verify-token' 오류 없음)으로 가른다(F32-R, F32-H 의 선례).

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

- 강도는 릴리스 하나로 고정(approved-fixed-f43-r, 실행기 파라미터 = 이미지 태그). 누수 속도는 담기 수로 정해지고(1번에 1개), 풀이 마르는 데 담기 20번이면 충분해 요청률, 시각과 무관하게 결정적이다. 동반 부하는 피해를 만드는 데 필요하지 않고 판정 표본(checkout)용이다.
- 동반 부하: load.north_south commerce surge.js 3rps(둘러보기 35%, 장바구니 15%, checkout 50%: 비우기, 담기, checkout. checkout 초당 약 1.5건, 30초 창 약 45건), ramp 2m + hold 16m + ramp_down 15s. F33-P 와 같은 꼴.
- success: checkout_5xx_rate ≥ 0.5 와 checkout_2xx_rate < 0.3, 3틱. cart 가 Ready 인 채로 풀만 마른 약 30초 동안은 order 의 cart 조회가 Redis 캐시로 답해 checkout 이 성공하거나 빈 장바구니 400 이 될 수 있어, 성공 판정은 NotReady, 재시작 구간(엔드포인트가 비어 order CartClient 연결 거부 → 재시도, 서킷 끝에 502)에서 선다. 첫 주기에서 NotReady 구간은 약 45초 + 재기동 약 1분이라 30초 창 기준 3틱을 채운다.
- min_hold 8m, timeout 14m, max_injection_duration 20m: 롤아웃 뒤 약 2~3분에 첫 실패 구간, 8분 동안 재시작 두세 번이 녹화 구간에 들어간다(원본의 간헐 장애). 동반 부하 18분 15초를 덮는다.
- must_rule_out: achieved_rps < 1, commerce PostgreSQL 파드 NotReady, user 파드 NotReady(2틱).
- abort: entry_status == 0(2틱). cart 가 죽어도 nginx, gateway 는 살아 상품 목록에 200 으로 답한다.
- recovery: target_health 200, cart 파드 Ready, PostgreSQL Ready, 기준선 checkout 비정상 비율 < 0.1(2틱).
- cleanup: 이미지 원복, available(180초), 1.2.0 을 쓰는 파드가 없어진 뒤 tb-w1 containerd 에서 1.2.0 의 이름 참조와 ID 참조를 모두 지우고 둘 다 없는지 확인. 원복한 새 파드는 빈 풀로 시작하고 더 새지 않는다. 데이터 부작용 없음(새는 것은 SELECT count 연결뿐, 담기 데이터는 평시 경로 그대로).
- 노드 디스크: tb-w1 이미지 파일시스템 61.7%(2026-10-09). 릴리스는 기본 이미지와 같은 eclipse-temurin 기반이라 새로 쓰는 것은 앱 층뿐이다. preflight 가 80% 미만을 요구한다. 109 scenario-runner 컨테이너의 known_hosts 에 tb-w1(192.168.122.184)이 있다(2026-10-09 확인).
- DB 여유: 누수 중 PostgreSQL 세션 약 77/100(평시 61~66 + 15). 다른 서비스 풀이 함께 늘어날 일은 없다(처리량이 그대로).

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Octopus Deploy 2025-11-25, 공식 상태 페이지 링크)와 요소별 대응표, 기전 같음 (§2)
- [x] 장부 갱신(§2-1 '장바구니 서비스', §4-1, §6), 묶음 J 4/50(8%), E 로 세도 3/50(6%), 정답 위치 장바구니 서비스 1/50(2%), 결제 경로 10/50(20%, 이 후보와 무관), 쇼핑몰 26(정답 위치 0 인 부품이라 허용). 어느 축도 이 후보로 20% 에 닿지 않는다 (§3)
- [x] 근본 원인 위치: 패치 `cart-service.patch:85-95`, `:68-76`, `:7`, 매니페스트 `26-cart-service.yaml` 각 줄 (§4)
- [x] 근본 원인의 흔적 조회: VM db.client.connections.*(cart used 평시 최대 2, max 20, timeouts 증가 없음), CH kcm_events_local Pulled 본문의 이미지 태그, PG kcm_resources_history ReplicaSet image (§7)
- [x] 핵심 증거가 표본 데이터만이 아님: KCM 이벤트, Hikari 런타임 지표, 로그 전수, DPM 세션 수 (§7)
- [x] 계기 흔적: 롤아웃 이벤트와 새 태그(인공 지연 없음, 누수는 실제 코드) (§7)
- [x] 정답이 관제 데이터로 낼 수 있는 결론: "새 버전 롤아웃 뒤 처리 중 요청 없이 연결 풀이 꽉 차고 DB 는 한가, 재시작마다 반복, 설정 불변 → 새 버전의 연결 누수". 코드 줄 추론 불필요 (§5)
- [x] 정답지 세 칸(근본 commerce-cart, 계기 비움, 부분 commerce-order, commerce-gateway) (§5). class A(코드 결함). fault_pattern 은 P1~P7 어느 것도 연결 누수에 맞지 않아 생략(새 P 번호를 만들지 않는 규칙)
- [x] 감지, 피해 판정, RCA 증거 구분 (§6)
- [x] 시나리오 id 흔적 없음: 패치 문자열(checkItemLimit, countItems, 'Cart item limit reached', 'Failed to count cart items', cart.max-items)에 id, fault, bug, chaos, scenario 없음(테스트로 확인), 태그 1.2.0, 실행기 인자와 상태 파일에 id 없음
- [x] 피해 계산: 로컬 실측(§4) 담기 20번 뒤 담기, 비우기 500, health 시간 초과, 스스로 회복하지 않음. 서킷브레이커는 gateway, order 쪽이라 피해를 줄이지 않고 502 로 바꿀 뿐이다 (§9)

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- 배포 단계에서 `bash scripts/scenarios/fault-images/build.sh f43-r` 로 109 docker 에 commerce-cart:1.2.0 이 있어야 preflight 가 통과한다(빌드 전후 기본 이미지 ID 비교는 스크립트가 한다).
- KCM 이 새 파드의 'Pulled ... commerce-cart:1.2.0', Unhealthy, Killing 을 잡는지, kcm_resources_history 가 새 ReplicaSet 을 잡는지.
- db.client.connections.usage(used)가 새 파드에서 20 에 붙는지, pending_requests 가 작게 유지되는지, dpm.postgresql.session.idle_session 이 약 15 오르는지.
- success 가 첫 NotReady 구간에서 섰는지, 실패 구간이 재시작마다 되풀이되는지, 인시던트가 생기는지(원칙 7).
- 녹화 검증: F33-P 와 러너 success 가 같으므로 cart 'Connection is not available'(total=20, active=20) 이 있고 commerce-user Hikari 오류, gateway 'verify-token' 오류가 없어야 F43-R 녹화로 인정.
- cleanup 뒤 tb-w1 containerd 에 1.2.0 의 이름 참조와 ID 참조가 모두 남지 않았는지(recovery 가 확인).
