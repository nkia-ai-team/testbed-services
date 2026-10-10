---
title: F51-H 설계 시트 (banking account-service 를 DB 연결이 새는 릴리스로 롤아웃해 잔액 조회와 이체가 500, 502)
status: Draft
owner: project
last_reviewed: 2026-10-10
tags:
  - scenario
  - design
  - release
summary: banking account-service 를 결함 있는 새 릴리스(core-banking-account:1.4.0, fault-images/f51-h 패치로 만든 별도 태그)로 롤아웃하면, 잔액 조회에 더한 마지막 이체 시각 조회가 트랜잭션 밖에서 빌린 Hikari 연결을 돌려주지 않아 잔액 조회 약 10건 만에 풀(10)이 total=10, active=10, idle=0 으로 굳고(Oracle 은 한가함), account 의 모든 DB 작업이 3초 대기 끝에 실패하며 readiness 가 account 를 엔드포인트에서 빼 잔액 조회, 계좌 목록, 이체가 500, 502 로 이어서 실패하는 시나리오. liveness 는 DB 를 보지 않아 재시작으로 풀리지 않는다. 원본은 Octopus Deploy 2025-11-25 인가 서비스 배포가 들여온 DB 연결 누수. 사용자 증상은 F51-R(운영 명령으로 account 0개)과 같고 원인이 다른 H.
---

# F51-H 설계 시트

## 1. 요약

banking account-service 의 새 릴리스 1.4.0 은 고객 앱 잔액 화면의 '마지막 이체일' 을 위해 `GET /api/accounts/{id}` 응답에 `X-Last-Transfer-At` 헤더를 더했다. 공용 응답(shop-common `AccountResponse`)은 다른 서비스와 같이 쓰므로 바꾸지 않고 헤더로 낸 것이다. 새 `LastTransferLookup` 은 `DataSourceUtils.getConnection(dataSource)` 로 연결을 받아 `SELECT MAX(created_at) FROM transfers WHERE from_account = ?`(idx_transfers_from)를 돌리고, 문장과 결과 집합은 try-with-resources 로 닫지만 연결은 닫지 않는다. 주석은 "계좌 조회 트랜잭션의 연결을 그대로 쓴다" 고 가정한다. 서비스 메서드(`@Transactional(readOnly = true)`) 안에서 불렀다면 맞는 가정이지만, 컨트롤러가 서비스 호출이 끝난 뒤(트랜잭션 밖에서) 부르므로 DataSourceUtils 는 풀에서 새 연결을 내주고 아무도 돌려주지 않는다.

풀 크기(10), 연결 대기(3초), 프로브, 자원, env, Oracle 은 그대로다. 잔액 조회 1건에 연결 1개가 빌린 채로 남아(Oracle 에서는 INACTIVE BANKING 세션) 롤아웃 뒤 잔액 조회 약 10건(기준선과 동반 부하에서 몇 초) 만에 풀이 total=10, active=10, idle=0 으로 굳는다. 그 뒤 account 의 DB 작업 전부(잔액 조회, 계좌 목록, 이체 앞 계좌 확인)와 readiness 의 DataSource 확인이 연결 대기 3초 끝에 실패한다. readiness 3회 실패(약 30초)로 파드가 엔드포인트에서 빠지면 잔액 조회와 계좌 목록은 nginx 502, 이체는 api 의 Connection refused, 재시도, 서킷 끝에 502 다. liveness 는 `/actuator/health/liveness`(livenessState 만)라 kubelet 이 재시작하지 않고, 롤백할 때까지 아무것도 연결을 풀지 않는다. Oracle 과 transfer, ledger, 거래 내역, commerce 정산은 내내 정상이다.

비유: 은행 창구(account) 공용 도장 10개. 새 규칙은 "잔액 확인서에 마지막 이체일을 적어라" 인데, 담당자는 확인서를 쓰고 도장을 서랍에 넣지 않고 주머니에 넣는다. 손님 10명 뒤 도장이 없어 창구가 멈추고, 지점장(readiness)이 창구에 '업무 중지' 팻말을 건다. 금고(Oracle)는 한가하다. 같은 증상은 창구를 아예 닫은 날(F51-R)에도 나지만, 그날은 창구 직원이 없고 이날은 직원이 있는데 도장이 없다. 고칠 곳은 새 규칙(릴리스)이다.

## 2. 원본 사례

- **Octopus Deploy, 2025-11-25** (공식 상태 페이지 사후 보고, 게시 2025-12-08): https://status.octopus.com/incidents/q5mhnrsm7q6j
- 사고 "OctopusID signin intermittent for cloud customers". 11-25 12:43 pm(AEST) 인가(authorization) 서비스에 변경을 배포했고 "This change introduced a bug resulting in database connection leaks." 그 결과 "a database connection leak, which caused some sign-in requests to time out". 고객 보고로 사고 선언, DB 연결 시간 초과까지 추적, "restarting services to release database connections" 로 임시 완화, 다음 날 근본 원인을 연결 누수로 확인하고 누수를 없앤 버전을 배포해 해결. 테스트가 놓친 이유는 "The connection leak only became apparent in high-traffic scenarios, which our current test suite doesn't replicate."
- 자료 문서 M2 표에 이미 있다(F43-R 이 더함). 새로 더할 사실 없음.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 인가 서비스에 변경 배포 | account-service 를 릴리스 core-banking-account:1.4.0 으로 롤아웃(KCM ScalingReplicaSet, 새 파드 Pulled 이벤트에 태그가 남음) |
| 원인이 된 결함 | 배포한 변경이 DB 연결 누수 버그를 들여옴 | 새 마지막 이체 조회가 트랜잭션 밖에서 빌린 연결을 돌려주지 않음(문장, 결과 집합만 닫음) |
| 전파 경로 | 연결 누수 → 연결 고갈 → 그 서비스에 기대는 로그인 요청의 DB 작업 시간 초과 | 잔액 조회 1건에 1개 누수 → 풀(10) 고갈 → account 의 모든 DB 작업 3초 대기 실패 → 그 서비스에 기대는 잔액 조회, 계좌 목록, 이체 실패. health 의 DB 확인 실패로 readiness 이탈 |
| 사용자 증상 | 로그인이 시간 초과(간헐적) | 잔액 조회, 계좌 목록 500(3초 뒤) 다음 502, 이체 502. 롤백까지 이어짐 |
| 원본의 탐지 경로 | 고객 보고, DB 연결 시간 초과 추적 | lucida-next 오류율, account 풀 지표(db.client.connections.*), account 오류 로그(Connection is not available), KCM Unhealthy readiness, api 오류 로그 |
| 완화와 복구 | 서비스 재시작으로 연결을 풀어 임시 완화, 누수 없는 버전 배포로 해결 | cleanup 이 매니페스트 이미지로 되돌림(롤백, 옛 파드가 내려가며 연결이 닫힘) |

기전 일치: 원인(배포한 코드가 DB 연결을 돌려주지 않음)에서 증상(그 서비스의 DB 작업이 연결 대기로 시간 초과, 그 서비스에 기대는 요청 실패, 재시작이나 롤백으로 풀림)까지 고리가 같다. 원본이 "고트래픽에서만 드러났고 시험 묶음이 재현하지 못했다" 고 한 점은 '트랜잭션 안에서 부르면 맞는 가정' 을 담은 주석 꼴로 둔다(단위 시험이나 서비스 안 호출에서는 새지 않는다).

바꾼 것(기전 고리 밖):
- 스택: 원본 서비스 스택 미상 → Spring Boot, HikariCP, Oracle 23ai Free.
- 간헐성: 원본은 일부 요청만 시간 초과(간헐). account 는 레플리카 1개, 풀 10개라 몇 초 만에 다 마르고, readiness 가 파드를 빼서 이어지는 실패가 된다. 원본의 '사람이 재시작해 잠깐 풀림' 은 재시작 주체가 없어(liveness 가 DB 와 떨어져 있음) 재현하지 않는다.
- 속도: 원본은 탐지까지 9시간 41분. 테스트베드는 잔액 조회마다 새어 수 초에 마른다.

### F51-R 과의 관계 (H, 같은 증상 다른 원인)

F51-R 은 운영자의 용량 명령이 account 를 replicas 0 으로 만든 장애다. 사용자 증상이 같다: 잔액 조회, 계좌 목록 nginx 502, 이체 api 502, 거래 내역과 commerce 정산 정상. 원인이 다르고, 가르는 관측 근거가 결정적이다.

| 근거 | F51-R | F51-H |
|---|---|---|
| KCM | ScalingReplicaSet 'Scaled down ... to 0 from 1', Killing, 그 뒤 새 파드 없음 | ScalingReplicaSet(새 ReplicaSet 1, 옛 0), 새 파드 Pulled 'core-banking-account:1.4.0', 그 뒤 Unhealthy 'Readiness probe failed', Killing 없음 |
| account 파드 | 없음 | Running, NotReady |
| account 로그와 지표 | 끊김 | ERROR 'Connection is not available ... (total=10, active=10, idle=0 ...)', db.client.connections.usage used 10, idle 0 |

같은 원본(Octopus)의 F43-R(commerce cart, PostgreSQL), F43-P(food dispatch, MySQL)와도 다르다(카탈로그 §1: R 도 DB 엔진, 진입 경로, 영향 범위 중 하나 이상을 달리한다). DB 엔진이 Oracle 이고, 진입이 nginx → account 직행과 nginx → api → account 두 갈래이며, liveness 가 DB 와 떨어져 있어 F43-P 처럼 재시작으로 잠깐 풀리는 일 없이 이어진다. 누수 꼴도 다르다: F43-R 은 문장만 닫는 try-with-resources, F43-P 는 한 갈래에서만 닫음, F51-H 는 트랜잭션 안에서만 맞는 DataSourceUtils 가정이 트랜잭션 밖 호출에서 깨짐.

## 3. 숫자 근거 (2026-10-10, `scenario-stats.py`, 정식 43 + 후보 29 = 72, 이 후보 전)

- 묶음(정식 + 후보): J 9, P 9(각 12%), G 8(11%), A 7, B 7, D 7, L 7(각 9%), C 6, F 3, E 2, H 2, K 2, I 1, M 1, O 1, N 0. 금지(20%) 없음. 이 후보 J 9→10(73 중 13.7%), 피해 모양으로 E 를 함께 적는다(E 로 세도 2→3, 4.1%).
- 정답 위치: 주문 서비스 7(9%), 노드, 디스크 6, 외부 결제 의존 6, 은행 이체 서비스 6(각 8%), 배달 서비스 5, **은행 계좌 서비스 3**(4%) → 4(5.5%). 결제 경로 합계 12(16.7%, 이 후보와 무관).
- 서비스: 쇼핑몰 26, 은행 23, 음식배달 23. 은행 23 → 24. 은행과 음식배달이 가장 적다(같은 수).
- 부품 지도: core-banking account 7(은행 계좌 서비스 3 + DB 테이블(은행 계좌) 4). 0 인 banking 부품은 kafka, nginx 뿐인데 kafka 는 비동기 소비자 경로라 사용자 증상이 없고(원칙 7), nginx 는 119 에 로그와 스팬이 없다(원칙 3, 아래 후보 4).
- 왜 이 후보인가: 가장 적은 서비스(은행) 중 상한 아래 정답 위치이고, 원본이 공식 사후 보고다. 같은 사용자 증상의 F51-R 과 원인이 달라(운영 명령 대 릴리스 결함) 관제 AI 가 "account 가 사라졌으면 용량 명령" 이나 "풀이 말랐으면 DB 탓" 으로 외워 찍지 못하게 하는 감별 데이터가 된다(F35-R 의 total=0, F01-P 의 행 잠금과도 갈린다). 이 축(같은 증상, 다른 원인)이 다양성의 목적(정답을 외워 찍지 못하게)에 가장 직접 맞는다.

### 후보 목록 (3단계, "실제 기전 × 부품", 숫자 순)

| # | 후보 | 층, 부품 | 원본 사례 | 결과 |
|---|---|---|---|---|
| 1 | account 새 릴리스의 DB 연결 누수로 풀 고갈, readiness 이탈(F51-R 과 H) | 앱, banking account(J+E, 은행 계좌 서비스 3) | Octopus Deploy 2025-11-25 공식 상태 페이지 | **채택** |
| 2 | account 새 릴리스가 이체 응답 구조를 바꿔 api 가 해석 실패(돈은 옮겨짐) | 앱, banking account(L+J, 은행 계좌 서비스 3) | GitHub 2021-10-08 공식 월간 보고 | 차순위: 1 과 같은 정답 위치라 한 반복에 하나만 낸다. 같은 원본이 F49-R, F49-P 로 두 번 쓰여 1 보다 새로움이 적다 |
| 3 | food tb-w3 호스트 방화벽 변경이 파드의 DNS(53) 질의를 막아 JVM DNS 캐시가 끝나면 order 의 하류 호출이 UnknownHost(F37-R 과 H) | 인프라, 노드 네트워크와 DNS(K+M, 노드 6) | Square 2023-09-07 공식 사고 요약 | 버림: 원칙 1. 원본은 방화벽 규칙 집합이 커져 노드가 불안정해지고 DNS 서버가 부하로 실패한 것이지 규칙이 DNS 를 막은 것이 아니다. '방화벽 규칙이 DNS 를 막은' 공식 사후 보고는 웹 검색 2회로 못 찾음. 이번 실행에서 막힌 'tb-w2 DNS 송신 차단(사례 없음)' 과 같은 주입 |
| 4 | banking nginx 라우트, 시간 제한 설정 배포 | 인프라, banking nginx(G, 게이트웨이 0) | GitHub 2026-03-05 공식(LB 설정이 잘못된 호스트로) | 버림: 원칙 3. 119 lucida_logs_local 에 nginx 로그가 없다(2026-10-10 실측: 앱 서비스와 kcm, sms 에이전트뿐), 스팬도 없어 계기와 실패가 관측되지 않음. 막힌 목록 46행과 같은 벽 |
| 5 | 남용 대응 작업이 고객 대신 플랫폼 쪽 대상(commerce 정산 계좌)을 FROZEN 으로 묶음 | 데이터, BANKING.ACCOUNTS(P, DB 테이블(은행 계좌) 4) | Cloudflare 2025-02-06 R2 공식(남용 대응이 R2 Gateway 전체를 끔) | 버림: 같은 주입 대상. F56-R 이 같은 두 행(commerce-settlement, commerce-merchant)을 지웠고, 막힌 목록 45행(같은 행 FROZEN)과 피해 경로가 같다 |
| 6 | 마이그레이션이 외래 키를 더하며 표 복사 잠금으로 배차 INSERT 를 막음 | 데이터, food dispatches(D, DB 테이블(배차) 2) | Modern Treasury 상태 페이지(외래 키 추가 잠금이 Events INSERT 를 막음, 웹 검색 요약만) | 버림: 원칙 1(원문 미확인)과 원칙 9, 컨트롤러 필수 중단 조건. 큰 표 복사 잠금이 order 풀을 묶는 벽은 막힌 목록 177, 182행과 같다 |
| 7 | 안 쓴다고 본 표를 지운 마이그레이션이 재시작 때 ORM 스키마 읽기에서 크래시 루프 | 데이터, food 스키마(P+L) | Sardine 2022-11-17 공식 상태 페이지 | 버림: 원칙 1. Hibernate ddl-auto none 이라 부팅 때 스키마를 읽지 않아 기전(재시작 뒤 부팅 실패)이 재현되지 않는다 |
| 8 | 이벤트 표 id 열이 정수 한계를 넘어 INSERT 실패 | 데이터, food, banking id 열(M5) | Open Build Service 2022-05-18 공식 블로그 | 버림: 원칙 1. 두 도메인 id 가 전부 BIGINT, NUMBER 라 한계가 없다(막힌 목록 '정수 키 소진' 과 같은 벽) |
| 9 | 배포 뒤 과도한 병렬과 풀 초기화 실패로 크래시 루프 | 앱, banking 서비스 풀(E) | PostHog 2025-10-21 공식 사후 보고 | 버림: 원칙 1. 원본은 CPU 과밀 배치, 재시도 증폭, 캐시 채우기 폭주가 겹친 복합이라 단일 계기로 재구성하면 기전이 바뀐다 |

DB 가 정답인 후보 3개(5, 6, 8), 부품 8종(account, 노드 네트워크와 DNS, nginx, 계좌 표, 배차 표, food 스키마, id 열, banking 풀), 앱, 인프라, 데이터 세 층.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 롤아웃: `scripts/scenarios/profiles/k8s_image_executor.py` RELEASE_SCRIPT `run` — 109 docker 의 core-banking-account:1.4.0 을 tb-w2 containerd 에 올리고(`docker save | ssh nkia@<tb-w2> sudo ctr -n k8s.io images import -`) `kubectl set image deploy testbed-account account-service=core-banking-account:1.4.0`. F41-R 과 같은 스크립트이고 계약(CONTRACTS["F51-H"])만 더했다.
2. 배포: `core-banking/k8s/21-account-service.yaml:9`(replicas 1), `:18-19`(nodeSelector tb-w2), `:27-28`(image, imagePullPolicy Never), 기본 전략(maxSurge 25%, maxUnavailable 25%). 109 kubectl 2026-10-10: generation 20, RollingUpdate 25%/25%, 이미지 core-banking-account:latest, 파드 testbed-account-d466dc8d4-bmcq7(재시작 0, 16시간). 109 docker 에 core-banking-account:latest 와 1.3.0(F41-R)이 있고 1.4.0 은 없음, tb-w2 containerd 에는 :latest 만, tb-w2 루트 파일시스템 74%.
3. 결함: `scripts/scenarios/fault-images/f51-h/account-service.patch:25-33`(AccountController.getAccount 가 서비스 호출 뒤 lastOutgoingTransferAt 결과를 X-Last-Transfer-At 헤더로 붙임), `:58-64`(주석의 '계좌 조회 트랜잭션의 연결을 그대로 쓴다' 가정), `:76-90`(lastOutgoingTransferAt: `DataSourceUtils.getConnection(dataSource)` :78, PreparedStatement 와 ResultSet 만 try-with-resources :79-85, 연결은 어디서도 닫거나 releaseConnection 하지 않음). 매니페스트 버전은 `core-banking/account-service/src/main/java/com/corebanking/account/controller/AccountController.java:24-27`(서비스 결과만 반환).
4. 트랜잭션 경계: `AccountService.getAccount`(`core-banking/account-service/src/main/java/com/corebanking/account/service/AccountService.java:34-39`)가 `@Transactional(readOnly = true)` 라 그 안의 JPA 연결은 트랜잭션이 끝나면 풀로 돌아간다. 컨트롤러의 새 조회는 그 뒤라 동기화가 없고, DataSourceUtils 는 풀에서 새 연결을 받아 그대로 돌려준다(돌려받을 트랜잭션이 없음). open-in-view 는 false(`application.yml:24`).
5. 풀: `core-banking/account-service/src/main/resources/application.yml:12-17`(maximum-pool-size 10, minimum-idle 3, connection-timeout 3000, max-lifetime 600000). Hikari 는 빌려 간 연결을 수명이 지나도 회수하지 않고(돌려받을 때 닫음) leakDetectionThreshold 도 없다. Oracle 의 BANKING 프로필에 IDLE_TIME 이 없어 서버도 끊지 않는다.
6. 프로브: startupProbe /actuator/health 5s×30(`21-account-service.yaml:67-72`, 트래픽 없이 통과), readiness /actuator/health 10s, 시간 제한 3s, 3회(`:73-79`, DataSource 확인 포함), liveness /actuator/health/liveness 15s, 3s, 5회(`:81-87`, `application.yml` 의 `management.endpoint.health.probes.enabled` 로 livenessState 만 봄). 풀이 마르면 DB 확인이 연결 대기 3초에 걸려 readiness 시간 제한(3초)과 같고, liveness 는 통과한다.
7. 호출자: 잔액 조회와 계좌 목록은 nginx `location /api/accounts` → account 직행(`core-banking/k8s/02-configmaps.yaml:45-47`), 이체와 거래 내역은 `location /api/transfers` → api(`:38-40`). api `AccountClient.requestTransfer`(`core-banking/api-service/src/main/java/com/corebanking/api/client/AccountClient.java:28-58`)가 account 의 500, 연결 거절을 'Account service call failed ...' ERROR 로 남기고 재시도 3회, 서킷(accountClient) 끝에 502. 거래 내역(api → transfer)과 commerce 정산(commerce-payment → transfer)은 account 를 거치지 않는다.

### 로컬 실측 (2026-10-10, 원칙 9 근거)

origin/main core-banking 에 같은 패치를 얹어 104 에서 account jar 를 빌드하고(`mvn -o -pl account-service -am package`, JDK 21. 운영 이미지는 eclipse-temurin 17), 로컬 Oracle Free(gvenzl/oracle-free 23-slim, `core-banking/db/init.sql` 시드 14계좌)에 붙여 띄운 뒤 같은 계좌 잔액을 14번 연달아 조회했다.

| 조회 | 결과 |
|---|---|
| 1~10번 | 200, 0.01~0.2초, 응답 헤더 `X-Last-Transfer-At` |
| 11~14번 | 500, 각 3.0초(연결 대기) |
| 그 직후 `/actuator/health` | 503, 3.0초(DataSource health check failed) |
| `/actuator/health/liveness` | 200 |
| Oracle `v$session`(username BANKING) | INACTIVE 10 |

로그(로컬, 그대로): `WARN o.h.engine.jdbc.spi.SqlExceptionHelper : SQL Error: 0, SQLState: null`, `ERROR o.h.engine.jdbc.spi.SqlExceptionHelper : HikariPool-1 - Connection is not available, request timed out after 3000ms (total=10, active=10, idle=0, waiting=0)`, `ERROR ... [dispatcherServlet] ... threw exception [Request processing failed: org.springframework.transaction.CannotCreateTransactionException: Could not open JPA EntityManager for transaction]`, `WARN o.s.b.a.jdbc.DataSourceHealthIndicator : DataSource health check failed`.

10번째까지 성공하는 까닭: 매 조회의 JPA 트랜잭션 연결은 돌아오고 새 조회의 연결만 남으므로 10번째 조회가 마지막 남은 연결을 가져간다. 운영에서는 기준선(잔액 55%, 1~4 iter/s)과 동반 부하(5rps 중 잔액 35%)가 합쳐 초당 잔액 조회 약 2~4건이라 Ready 뒤 약 3~5초에 마른다.

## 5. 원인 규정 (원칙 5, 6)

- 근본(`root_cause.target_id`): `core-banking-account`(account-service 의 새 릴리스). 결함을 가진 곳이자 되돌려야 재발이 막히는 곳.
- 계기(`trigger_target_id`): 비움. 계기(릴리스 롤아웃)와 결함이 같은 곳이다. 호출자(nginx, api, loadgen)의 요청은 평시 그대로 정당하다(원칙 6).
- 부분 점수: `core-banking-api`, `banking-api`(이체 실패가 드러나는 곳).
- 층위: 원본 사후 보고의 결론("배포한 변경이 DB 연결 누수를 들여왔고 고친 버전으로 복구")과 같은 층위. 패치 속 코드 줄을 맞히라고 요구하지 않는다(원칙 5, 결함 버전 이미지 규칙 7). 관제 데이터로 낼 수 있는 결론: "account 를 1.4.0 으로 롤아웃한 직후 account 풀만 used=10, idle=0 으로 굳고 Oracle 과 다른 서비스 풀은 한가하며 설정은 그대로다 → 새 버전이 연결을 돌려주지 않는다(누수), 롤백".

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 |
|---|---|
| 감지 | core-banking-api 서버 스팬 오류율(이체 502), account 서버 스팬 오류율과 지연(500, 3초), api 와 account ERROR 로그 급증, KCM Unhealthy readiness. banking 선례(F39-R, F51-R)의 promote 지연(약 18~20분) 안에서 고장이 15분 넘게 이어진다 |
| 피해 판정(러너) | 동반 부하 이체 5xx 비율 ≥ 0.5 와 잔액 조회(step get) 실패율 ≥ 0.5 가 함께 3틱. 평시 둘 다 0 |
| RCA | 롤아웃 이벤트와 새 이미지 태그, 새 파드의 풀 지표 used 10 idle 0, 'Connection is not available (total=10, active=10, idle=0 ...)', readiness 실패와 재시작 없음, 한가한 Oracle 과 정상인 transfer, ledger 풀, 바뀌지 않은 설정 |

## 7. 관측 근거 표 (119 실조회, 2026-10-10 13:20~13:50 UTC)

| 증거 | 119 표와 칸 | 조회 | 평시 결과 |
|---|---|---|---|
| 계기: 롤아웃과 이미지 태그 | CH `kcm_events_local`(object_name, reason, body) | `namespace='rca-testbed-banking' AND object_name LIKE 'testbed-account%' AND reason='Pulled'` | 'Container image "core-banking-account:latest" already present on machine' 4건(2026-10-09 20:34, 20:49 의 재배포)뿐. ScalingReplicaSet, Scheduled, Started, Unhealthy, Killing 이 모두 수집된다(같은 조회 5일 17종). F41-R 시트(2026-10-09)의 PG `kcm_resources_history` 실측: testbed-account ReplicaSet 33건, 파드 24건 전부 :latest |
| 근본: 풀이 빌린 연결로 참 | VM `db.client.connections.usage`(service_name, host_name=파드, pool_name, state), `db.client.connections.max`, `db.client.connections.timeouts`, `db.client.connections.pending_requests` | `max_over_time(...{service_name="core-banking-account",state="used"}[7d])` 등 | used 최대 1(현재 파드 d466dc8d4-bmcq7, 7일), idle 2~6, max 10. timeouts 와 pending 은 F35-R 실행 때의 파드(d466dc8d4-84hhz: timeouts 550, pending 33, 그때 풀은 total=0)에만 있고 현재 파드는 0. 같은 지표가 food dispatch 파드 풀 고갈 때 used=10 으로 찍힌 기록이 있어(2026-10-01~08, testbed-dispatch-69ff6c4468-drvj7) 수집이 확인된다. 에이전트 지표라 트레이스 표본과 무관 |
| 근본: 풀 상태 로그 | CH `lucida_logs_local`(service_name, severity_text, body) | `body LIKE '%Connection is not available%'` 20일 | account 'HikariPool-1 - Connection is not available, request timed out after 3000ms (total=0, active=0, idle=0, waiting=30)'(F35-R 계정 잠금, total=0)와 '(total=1, active=1 ...)' 만 있다. total=10, active=10 은 account 에 없다(food dispatch 에는 'total=10, active=10, idle=0, waiting=7' 꼴이 수집됨). 'DataSource health check failed' WARN 107건(F35-R) |
| 전파: readiness 이탈, 재시작 없음 | CH `kcm_events_local` | `reason IN ('Unhealthy','Killing') AND object_name LIKE 'testbed-account%'` | 'Readiness probe failed: ... context deadline exceeded' 29건(2026-10-09 07:12, F35-R)이 수집됨. 고장 때는 같은 꼴(또는 'HTTP probe failed with statuscode: 503')이 새 파드에 이어지고 Killing 은 없을 것 |
| 전파: api 실패 | CH `lucida_logs_local` | core-banking-api ERROR 7일 | 'Account service call failed for order null: 400 ...(잔액 부족)' 평시 4,840건이 있다. 고장 때는 같은 문장 뒤가 '500 ...' 다음 'I/O error on POST request for "http://testbed-account:8081/api/accounts/transfer": Connection refused' 가 되고(F51-R 실행 꼴 668건 수집), 'Account service circuit open/exhausted' 가 따른다 |
| 배제: DB 는 한가 | VM `dpm.oracle.session.active_session`, CH `dpm_session_local`(Oracle 은 ACTIVE 세션만 담김), `dpm_topsql_local` | 7일 최대, 하루 평균 | active_session 7일 최대 3, 하루 평균 0.18. dpm_session_local 의 Oracle USER 세션은 평시 수집기 자신뿐(INACTIVE 는 담기지 않는다). 누수 연결은 INACTIVE 라 활성 세션이 늘지 않는 것이 'DB 가 느려 풀이 찬 것이 아님' 의 증거다 |
| 배제: 다른 서비스 풀 | VM `db.client.connections.usage{service_name=~"core-banking-(transfer|ledger)"}` | 최근 10분 | transfer used 0~1 idle 4~5, ledger used 0 idle 3. 고장 때도 같아야 한다 |

표본 데이터(트레이스 10%, APM 스팬 지표)만으로 증명하지 않는다. 결정 증거는 KCM 이벤트(전수), 풀 지표(에이전트 지표, 전수), 로그(전수)다.

## 8. 감별

- must_support: 롤아웃과 1.4.0 태그, 새 파드 풀 used 10 idle 0 과 timeouts, 'Connection is not available (total=10, active=10, idle=0 ...)' 와 'DataSource health check failed', Readiness probe failed 와 재시작 없음, 잔액 조회와 이체 실패 및 거래 내역, 정산 정상.
- must_rule_out: 운영 명령 replicas 0(F51-R), Oracle 이나 DB 계정(F35-R, F01-P), DB 가 느려 풀이 찬 것(활성 세션, Top SQL 평탄), 풀 설정 축소(env, max 10 그대로), 메모리 누수 릴리스(F41-R), account 하류 주소(F39-R).
- contrast_with: F51-R, F41-R, F35-R, F43-R/F43-P.

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

- 강도는 릴리스 하나로 고정(approved-fixed-f51-h, 실행기 파라미터 = 이미지 태그). 누수는 잔액 조회 1건에 1개라 풀(10)이 마르는 데 조회 10건이면 되고, 그 뒤 피해는 요청률과 무관하게 이어진다.
- 동반 부하: load.north_south core-banking transfer-heavy-surge.js 5rps(이체 40% → 초당 2건, 잔액 35% → 1.75건, 30초 창 각 약 60, 50건), ramp 2m + hold 21m + ramp_down 15s. 피해를 만드는 데 필요한 것은 아니고(기준선 잔액 조회만으로도 마른다) 판정 표본용이다. F51-R 과 같은 부하라 두 시나리오의 녹화본이 같은 증상 위에서 원인만 다르다.
- success: transfer_5xx_rate ≥ 0.5 와 balance_read_nonok_rate ≥ 0.5, 3틱. 롤아웃(새 파드 기동 약 40~50초) 뒤 몇 초에 풀이 마르고, 잔액 조회는 그 순간부터 500(3초), 약 30초 뒤 502. 이체는 account 계좌 확인이 500 이 되는 순간부터 api 502.
- min_hold 15m, timeout 20m, max_injection_duration 25m(F51-R 과 같음): banking 선례의 promote 지연(약 18~20분) 안에서 고장이 15분 넘게 이어지고, 동반 부하 ramp 2m + hold 21m + ramp_down 15s(23m15s)를 덮는다.
- must_rule_out: achieved_rps < 1.25, transfer 파드 NotReady, Oracle 파드 NotReady(2틱).
- abort: entry_status == 0(2틱). account 가 빠져도 nginx, api 는 살아 502 로 답한다.
- recovery: target_health 200, transfer Ready, 기준선 이체 5xx < 0.05, 기준선 잔액 조회 실패율 < 0.05(2틱).
- cleanup: 이미지 원복, available(180초, 새 파드는 새 풀로 뜬다), 옛 파드가 내려가며 빌린 연결이 닫혀 Oracle 세션이 풀림, 1.4.0 을 쓰는 파드가 없어진 뒤 tb-w2 containerd 에서 1.4.0 의 이름 참조와 ID 참조를 모두 지우고 둘 다 없는지 확인.
- 서킷브레이커: api 의 accountClient 서킷은 피해를 줄이지 않고 502 로 바꿀 뿐이다. account 의 transferClient 서킷은 이 경로에 닿기 전에 실패하므로 무관하다.
- 노드 디스크: tb-w2 루트 74%(2026-10-10). 릴리스는 기본 이미지와 같은 eclipse-temurin 기반이라 새로 쓰는 것은 앱 층뿐이다. preflight 가 80% 미만을 요구한다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Octopus Deploy 2025-11-25, 공식 링크)와 요소별 대응표, 기전 같음 (§2)
- [x] 장부 갱신(§4-1, §6), 묶음 J 9→10/73(13.7%), 정답 위치 은행 계좌 서비스 3→4/73(5.5%), 결제 경로 12/73(16.4%, 이 후보와 무관), 은행 23→24. 어느 축도 20% 미만 (§3)
- [x] 근본 원인 위치: 패치 `account-service.patch:76-90`, `:25-33`, `:58-64`, 매니페스트 `AccountController.java:24-27`, `application.yml:12-17`, `21-account-service.yaml:67-87` (§4)
- [x] 근본 원인의 흔적 조회: VM db.client.connections.usage(account used 7일 최대 1, idle 2~6, max 10, 같은 지표가 다른 서비스 풀 고갈 때 used=10 으로 찍힘), CH kcm_events_local Pulled 본문의 이미지 태그, CH lucida_logs_local 'Connection is not available' 꼴 (§7)
- [x] 핵심 증거가 표본 데이터만이 아님: KCM 이벤트, 풀 에이전트 지표, 로그 전수 (§7)
- [x] 계기 흔적: 롤아웃 이벤트와 새 태그(인공 지연 없음, 누수는 실제 코드) (§7)
- [x] 정답이 관제 데이터로 낼 수 있는 결론: "새 버전 롤아웃 직후 그 서비스 풀만 빌린 연결로 차고 DB 는 한가, 설정 불변 → 새 버전 누수". 코드 줄 추론 불필요 (§5)
- [x] 정답지 세 칸(근본 core-banking-account, 계기 비움, 부분 api) (§5). class A(코드 결함). fault_pattern 은 P1~P8 어느 것도 연결 누수에 맞지 않아 생략(F43-R, F43-P 와 같음, 새 P 번호를 만들지 않는 규칙)
- [x] 감지, 피해 판정, RCA 증거 구분 (§6)
- [x] 시나리오 id 흔적 없음: 패치 문자열(새 클래스 LastTransferLookup, 헤더 X-Last-Transfer-At, 로그 'Last transfer lookup failed ...')에 id, fault, bug, chaos, leak 없음(테스트로 확인), 태그 1.4.0, 실행기 인자와 상태 파일에 id 없음(테스트로 확인)
- [x] 피해 계산: 로컬 실측(§4) 11번째 조회부터 3초 뒤 500, health 503, liveness 200. 운영 잔액 조회율로 Ready 뒤 수 초에 마르고, 재시작 주체가 없어 롤백까지 이어진다 (§9)

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- 배포 단계에서 `bash scripts/scenarios/fault-images/build.sh f51-h` 로 109 docker 에 core-banking-account:1.4.0 이 있어야 preflight 가 통과한다(빌드 전후 기본 이미지 ID 비교는 스크립트가 한다).
- 새 파드의 db.client.connections.usage 가 used=10, idle=0 으로 굳는지, 'Connection is not available ... (total=10, active=10, idle=0 ...)' 가 119 에 남는지.
- readiness 가 'context deadline exceeded'(3초 시간 제한)와 'statuscode: 503' 중 어느 꼴로 남는지, Killing 이 없는지.
- 인시던트가 고장 구간에 생기는지(원칙 7). F51-R 녹화본과 증상(이체, 잔액 조회 502)이 겹치는지.
- cleanup 뒤 Oracle 의 INACTIVE BANKING 세션이 평시 수로 돌아오는지, tb-w2 containerd 에 1.4.0 의 이름 참조와 ID 참조가 모두 남지 않았는지(recovery 가 확인).
