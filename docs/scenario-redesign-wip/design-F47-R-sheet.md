---
title: F47-R 설계 시트 (banking api-service 를 이체마다 account 의 계좌 목록 전체를 다시 읽는 릴리스로 롤아웃)
status: Draft
owner: project
last_reviewed: 2026-10-09
tags:
  - scenario
  - design
  - release
summary: banking api-service 를 결함 있는 새 릴리스(core-banking-api:1.4.0, fault-images/f47-r 패치로 만든 별도 태그)로 롤아웃하면, 새로 더한 이체 전 활성 계좌 확인이 이체마다 account-service 의 계좌 목록 API 를 끝 페이지까지(약 49번) 뒤에서 다시 읽어 account 가 CPU 한도에서 요청에 묻히고, 잔액 조회가 초 단위로 늦어지며 이체가 502 로 몰려 실패하는 시나리오. account 는 아무것도 바뀌지 않았다. 원본은 Cloudflare 2025-09-12 대시보드 릴리스의 버그가 Tenant Service API 호출을 크게 늘려 그 서비스를 과부하시킨 장애.
---

# F47-R 설계 시트

## 1. 요약

banking api-service 의 새 릴리스 1.4.0 은 이체를 넘기기 전에 출금, 입금 계좌가 ACTIVE 인지 확인한다. 활성 계좌 id 집합을 두고, account-service 의 목록 API(`GET /api/accounts?status=ACTIVE&page=N&size=20`)를 짧은 페이지가 나올 때까지 읽어 만든다. 활성 계좌가 976개라 한 번에 49번 요청이다. 응답을 늦추지 않으려고 이체를 받을 때마다 `CompletableFuture.runAsync` 로 그 집합을 뒤에서 다시 읽는데, 새로 고침에 수명도 겹침 방지도 없다. 그래서 이체 1건이 account 로 목록 요청 49건을 더 보낸다(실패하면 기존 재시도 3회가 붙는다). 동반 부하로 이체가 초당 약 8건이면 account 는 초당 약 390건의 목록 요청을 받고, CPU 한도 500m 에 붙어 모든 요청이 줄을 선다. 게이트웨이를 거쳐 account 로 오는 잔액 조회가 밀리초에서 초 단위로 늦어지고, 일부는 Hikari 대기 3초 끝에 500, readiness 가 늦어 잠깐씩 엔드포인트에서 빠지며, api 의 account 서킷이 열린 동안 이체가 502 로 몰려 실패한다. account 는 코드, 설정, 자원이 그대로이고 Oracle 은 여유가 있다.

비유: 창구 직원(account)은 그대로인데, 옆 부서(api)에 새로 온 직원이 손님 한 명을 받을 때마다 "혹시 바뀌었을까 봐" 고객 명부 49쪽을 처음부터 끝까지 다시 복사해 달라고 창구에 줄을 세운다. 창구는 명부 복사에 묻혀 진짜 손님(잔액 조회)을 몇 초씩 기다리게 한다. 창구 직원을 바꿔도 소용없고, 고칠 곳은 새로 온 직원(api 릴리스)이다.

## 2. 원본 사례

- **Cloudflare, 2025-09-12** (공식 블로그 사후 보고 "A deep dive into Cloudflare's September 12, 2025 dashboard and API outage"): https://blog.cloudflare.com/deep-dive-into-cloudflares-sept-12-dashboard-and-api-outage/
- 대시보드와 일부 API 가 약 1시간 쓸 수 없거나 일부만 됐다(17:57 UTC 시작). 16:32 배포한 대시보드 새 버전에 버그가 있었다: "a bug that will trigger many more calls to the /organizations endpoint, including retries in the event of failure". API 호출을 하는 React useEffect 의 의존성 배열에 렌더마다 새로 만들어지는 객체가 들어가, 한 번 렌더에 호출이 한 번이 아니라 여러 번 실행됐다. 17:50 Tenant Service API 새 버전이 배포됐고 17:57 Tenant Service 가 과부하됐다. Tenant Service 는 API 요청 인가 판단의 일부라 "Without Tenant Service, API request authorization can not be evaluated", 인가 평가가 실패하면 API 요청이 5xx 를 반환했다. 탐지는 자동 경보였고, API 사용량 급증을 봤지만 재시도와 새 요청을 구분하기 어려워 대시보드의 반복 호출을 찾는 데 시간이 걸렸다. 자원 추가(18:17, API 98% 회복), 임시 레이트 리밋(19:01), 문제 변경 되돌림(19:12)과 대시보드 핫픽스로 복구했다. 사후 보고는 "the immediate trigger was a bug in the dashboard" 라 하고, Tenant Service 에 이런 부하 급증을 감당할 용량이 배정되지 않은 점을 함께 든다.
- 자료 문서 M2 표에 이 사례를 더했다(출처가 말한 사실만).

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 대시보드 새 버전 배포(16:32) | api-service 를 릴리스 core-banking-api:1.4.0 으로 롤아웃(KCM ScalingReplicaSet, 새 파드 Pulled 이벤트에 태그가 남음) |
| 원인이 된 결함 | 클라이언트 코드가 같은 엔드포인트를 사용자 동작 한 번에 여러 번 부름(렌더마다 다시 만들어지는 객체 때문에 효과가 매번 다시 돎), 실패하면 재시도 | 새 활성 계좌 확인이 이체마다 계좌 목록 전체(49 페이지)를 다시 읽음(수명, 겹침 방지 없는 새로 고침), 실패하면 기존 재시도 3회 |
| 전파 경로 | 호출 폭증 → 인가를 맡은 Tenant Service 과부하 → 그것에 기대는 API 요청 5xx | 목록 요청 폭증 → account 가 CPU 한도에서 과부하 → account 에 기대는 잔액 조회 지연·실패, 이체(api → account → transfer) 지연과 서킷 열림의 502 |
| 사용자 증상 | 대시보드 전면 장애, API 두 구간 심각 저하(5xx) | 잔액 조회 초 단위 지연과 일부 500, 502, 이체 502 몰림과 지연. 거래 내역, commerce 정산은 정상 |
| 원본의 탐지 경로 | 자동 경보, API 사용량 급증(재시도와 새 요청 구분 어려움) | lucida-next 지연, 오류율 이상, account CPU 와 지연, 목록 문장 실행 수 폭증(DPM), api 로그와 스레드 수, KCM Unhealthy |
| 완화와 복구 | 자원 추가, 레이트 리밋, 변경 되돌림, 대시보드 핫픽스 | cleanup 이 api 이미지를 매니페스트 이미지로 되돌림(롤백) |

기전 일치: 원인(호출하는 쪽의 새 버전이 같은 엔드포인트를 사용자 동작마다 여러 번, 실패하면 재시도까지 부름)에서 증상(그 엔드포인트를 가진 공유 서비스가 과부하되고, 그것에 기대는 요청이 5xx)까지 고리가 같다. 피해를 입는 서비스는 아무것도 바뀌지 않았고, 고칠 곳은 호출하는 쪽의 새 버전이다.

바꾼 것(기전 고리 밖):
- 클라이언트: 브라우저의 React 대시보드 대신 서버 쪽 api-service. 반복의 꼴은 '렌더마다 효과 재실행' 대신 '이체마다 목록 전체 새로 고침'이다(둘 다 사용자 동작 하나가 같은 엔드포인트 호출을 여러 번 만든다).
- 공유 서비스: 인가를 맡은 Tenant Service 대신 계좌 조회를 맡은 account-service. 원본도 Tenant Service 를 거치는 API 요청이 실패했고, 테스트베드도 account 를 거치는 잔액 조회와 이체가 실패한다.
- 겹친 배포: 원본은 과부하 직전 Tenant Service 새 버전도 배포됐다. 테스트베드는 api 하나만 롤아웃한다(account 는 그대로). 원본 사후 보고도 직접 계기를 대시보드 버그로 지목한다.
- 원본의 후반부(되돌린 패치가 다시 영향, 재인증 몰림)는 재현하지 않는다.

## 3. 숫자 근거 (2026-10-09, `scenario-stats.py`, 정식 + 후보 합계 52, 이 후보 전)

- 묶음: A 7, B 7, D 7(각 13%), C 6, G 6(각 11%), J 4(7.7%), F 3, L 3, E 2, H 2, K 2, I 1, M 1, O 1, N 0. 어느 묶음도 20% 에 닿지 않는다. J 는 현실 트리거 1위(Google 바이너리 배포 37%)인데 4개다. 이 후보 J 4→5(53 중 9.4%), 피해 모양으로 E 를 함께 적는다(E 로 세도 2→3, 5.7%).
- 정답 위치: 외부 결제 의존 6, 주문 서비스 6(각 11.5%), 노드·디스크 5, 은행 이체 서비스 5(각 9.6%), 결제 경로 합계 10(19.2%). **은행 API 1 → 2(3.8%)**, §2-1 에 이미 있는 말. 결제 경로는 이 후보로 10/53(18.9%).
- 서비스: 쇼핑몰 26, 은행 15, 음식배달 11. 은행 15 → 16.
- 왜 이 후보인가: 음식배달(11)이 가장 적어 같은 기전을 먼저 food 에 걸어 봤다(후보 2). food order 는 `OrderService.createOrder` 가 `@Transactional` 안에서 restaurant 를 불러(`food-delivery/order-service/src/main/java/com/fooddelivery/order/service/OrderService.java:57-60`), restaurant 가 과부하로 느려지면 order 의 Hikari(15)가 그 대기에 묶여 마르고 readiness(/actuator/health, DB 확인)가 빠져 loadgen 입구가 연결 불가가 된다(entry_status 0, 필수 중단 조건). 증상도 order→restaurant 조회 실패라 F36-R, F44-R 과 겹친다. 쇼핑몰은 가장 많고, 가장 가까운 꼴(게이트웨이 → user 인가 확인 폭증, 후보 3)은 겉 증상이 F33-P 와 같다. 은행은 진입점(nginx → api)이 DB 를 쓰지 않아(api 는 DataSource 가 없음) 하류가 과부하돼도 api 가 5xx 로 답하고 entry_status 가 0 이 되지 않는다. 은행 API 는 정답 위치 1 로 적다.

### 후보 목록 (3단계, 기존 목록이 아니라 기전 × 0 부품, 인프라 층에서 시작)

| # | 후보 | 층, 부품 | 원본 사례 | 결과 |
|---|---|---|---|---|
| 1 | banking api 새 릴리스가 이체마다 account 계좌 목록 전체를 다시 읽어 account 과부하 | 앱, banking api(J+E) | Cloudflare 2025-09-12 공식 블로그 | **채택** |
| 2 | food order 새 릴리스가 주문마다 restaurant 를 여러 번 불러 restaurant 과부하 | 앱, food order(J, 부품 지도 0) | 같은 원본 | 버림: order 는 트랜잭션 안에서 restaurant 를 불러 restaurant 가 느려지면 order 풀이 마르고 readiness(DB)가 빠져 entry_status 0(필수 중단 조건). 증상(order→restaurant 실패)이 F36-R, F44-R 과 겹침 |
| 3 | commerce gateway 새 릴리스가 쓰기마다 토큰 확인을 여러 번 불러 user-service 과부하 | 앱, commerce gateway(J, 정답 위치 0) | 같은 원본(인가 서비스 과부하라 가장 가까움) | 버림: user 가 과부하되면 게이트웨이의 인가된 쓰기가 500(AuthGuard 자기 호출이라 서킷 없음)이 되어 F33-P 와 겉 증상이 같고 F16-H(쓰기 401)와도 겹침(카탈로그 §1) |
| 4 | banking api 새 릴리스가 거래 내역 행마다 account 를 불러 보강(HTTP N+1) | 앱, banking api(J) | 같은 원본 | 버림: 거래 내역은 transfer 의 Oracle 전수 스캔(1회 약 0.21초 CPU)이라 거래 내역을 늘리는 동반 부하는 Oracle 을 먼저 포화시켜 F42-R 증상과 섞이고, 거래 내역 초당 3건 × 20 = 60건/초로는 account 를 포화시키지 못함(원칙 9) |
| 5 | banking Kafka 소비 지연(리밸런싱, 적체)으로 원장 반영 지연 | 인프라, 큐 컨슈머 지연(메시지 브로커 0) | M10 공식 사례 없음 | 버림: 원칙 1, 원칙 7(원장은 비동기라 사용자 증상 없음, rejected 의 Kafka 소비자 행과 같은 벽) |
| 6 | 운영자가 업무 시간에 banking Oracle 인스턴스를 재시작(계획 정비) | DB 인스턴스 | 공신력 있는 사례 확인 못 함 | 버림: 원칙 1. 묶음 B(13%)와 F25-H 꼴 '부품 멈춤'이기도 함 |
| 7 | 텔레메트리 DaemonSet 배포가 K8s API 서버를 과부하해 클러스터 DNS 실패 | 인프라, 컨트롤 플레인과 DNS | OpenAI 2024-12-11 공식 | 버림: 원칙 3. 119 KCM 이 kube-system 과 컨트롤 플레인을 수집하지 않음(rejected 의 CoreDNS 행과 같은 벽) |
| 8 | DB 권한 변경으로 생성 질의가 중복 행을 돌려 소비자의 고정 한도를 넘음 | 앱, food restaurant 인기 메뉴 | Cloudflare 2025-11-18 공식 | 버림: 소비자 코드에 고정 한도가 없어 앱 코드 변경과 데이터 계기의 복합이 필요(F43-R 후보 10 과 같은 벽) |
| 9 | 클라이언트 재시도 정책 오배포로 재시도 폭풍 | 앱, 재시도(N 0) | AWS 2021-12-07 공식 | 버림: 하류 실패라는 두 번째 계기가 필요(rejected 의 gateway 재시도 행과 같은 주입) |

DB 가 정답인 후보는 1개(6), 부품 7종(banking api, food order, commerce gateway, banking Kafka, banking Oracle, 컨트롤 플레인과 DNS, food restaurant), 앱과 인프라 두 층을 모두 냈다. 지휘 세션이 넘긴 이번 실행의 막힌 후보와 같은 원본, 같은 주입은 없다(Cloudflare 2025-09-12 는 처음 쓰는 원본이고, api 릴리스는 처음 쓰는 주입 대상이다).

## 4. 인과 사슬 (코드와 인프라 위치)

1. 롤아웃: `scripts/scenarios/profiles/k8s_image_executor.py` RELEASE_SCRIPT `run` — 109 docker 의 core-banking-api:1.4.0 을 tb-w2 containerd 에 올리고 `kubectl set image deploy testbed-api api-service=core-banking-api:1.4.0`. 노드는 Deployment 의 nodeSelector 에서 읽는다(F41-R, F42-R 과 같은 스크립트, 계약만 더함).
2. 배포: `core-banking/k8s/20-api-service.yaml:9`(replicas 1), `:19`(nodeSelector tb-w2), `:27-28`(image, imagePullPolicy Never), `:81-82`(limits cpu 500m). 전략은 기본값(maxSurge 25%)이라 새 파드가 Ready 가 된 뒤 옛 파드가 내려간다(109 kubectl 2026-10-09: replicas 1, generation 20, 파드 testbed-api-bdd59894b-ghfxw, tb-w2 containerd 의 core-banking-api:latest 이미지 ID 와 109 docker 의 ID 가 같은 sha256:f37dcf71…, tb-w2 이미지 파일시스템 73.3%).
3. 결함: `scripts/scenarios/fault-images/f47-r/api-service.patch:101-115`(AccountDirectory.requireActive — 집합이 비었으면 한 번 읽고, 이체마다 `CompletableFuture.runAsync(this::refresh)`), `:125-137`(load — status=ACTIVE, size=20 으로 짧은 페이지까지 읽음), `:34-52`(AccountClient.listAccounts — accountClient 서킷과 재시도 3회를 이체 요청과 함께 씀), `:162`(ApiService.submitTransfer 가 넘기기 전에 requireActive). 매니페스트 버전은 `core-banking/api-service/src/main/java/com/corebanking/api/service/ApiService.java:28-38`(검증 뒤 바로 requestTransfer).
4. 스레드: api 컨테이너는 CPU 하나라 JVM 의 availableProcessors 가 1 이다(119 VM `apm.agent.otel.java.jvm.cpu.count{service_name="core-banking-api"}` 7일 내내 1). 이때 ForkJoinPool 공용 풀 병렬도가 1 이라 `CompletableFuture.runAsync` 는 공용 풀이 아니라 작업마다 새 스레드를 만든다(JDK CompletableFuture 의 ThreadPerTaskExecutor). 새로 고침은 겹쳐서 쌓이고, account 가 느려질수록 스레드가 늘어난다.
5. 피해 쪽: account `AccountController.list`(`core-banking/account-service/src/main/java/com/corebanking/account/controller/AccountController.java:32-42`)가 페이지마다 건수 질의와 페이지 질의를 한다(로컬 Oracle 의 v$sql 로 확인한 sql_id 82k5d7uauh4xd 페이지, 36z5znswd9sxt 건수, 9550zwkkzcb36 첫 페이지. 같은 문장이라 109 Oracle 에서도 같은 sql_id 다). account 의 limits cpu 500m(`core-banking/k8s/21-account-service.yaml:92-94`), Hikari 10, connection-timeout 3000(`core-banking/account-service/src/main/resources/application.yml:12-17`), readinessProbe `/actuator/health` timeout 3s(`21-account-service.yaml:73-79`), livenessProbe `/actuator/health/liveness`(`:81-87`).
6. 하류로의 번짐: nginx 가 `/api/accounts` 를 account 로 바로 보낸다(`core-banking/k8s/02-configmaps.yaml` nginx-conf). api 의 이체는 `AccountClient.requestTransfer`(accountClient 서킷, 10건 창 50%, 재시도 3회 200ms 지수, 읽기 10초: `core-banking/api-service/src/main/resources/application.yml`)로 account → transfer 를 탄다. 새 목록 호출이 같은 서킷을 쓰므로 목록 실패가 서킷을 열면 이체도 502 로 바로 실패한다.
7. 영향 밖: 거래 내역(nginx → api → transfer)과 commerce 정산(commerce payment → transfer 직행)은 account 를 거치지 않는다. Oracle 은 accounts(1,001행) 페이지와 건수라 여유가 있다.

### 로컬 실측 (2026-10-09, 원칙 9 근거)

origin/main core-banking 에 패치를 얹어 `./mvnw -o -pl api-service,account-service,transfer-service -am package` 로 jar 를 만들고, eclipse-temurin 21-jre(x86_64) 컨테이너에 OTel 자바 에이전트(내보내기 끔)를 붙여 띄웠다. api, account, transfer 는 `--cpus 0.5 -m 1g`(운영 한도와 같음), Oracle Free 23-slim 은 `--cpus 2`, 스키마와 시드는 `core-banking/db/init.sql`, `seed-all.sql`(계좌 1,001, 활성 976). 열린 고리 부하(정해진 속도로 보내고 60초 제한): 이체 POST(api), 잔액 GET(account 직접, 운영의 nginx 경로 대신).

| 판 | 부하 | 결과 |
|---|---|---|
| 매니페스트 api | 이체 12/s + 잔액 10/s, 90초 | JIT 예열 20초 뒤 전부 200, 잔액 p50 10ms 안쪽, 이체 p50 20~30ms, account CPU 한도의 1/4~2/5 |
| 릴리스(동기판: 이체 요청 스레드에서 49번 읽음) | 이체 12/s + 잔액 10/s | account CPU 한도에 붙고 api 요청 스레드 200개가 모두 묶여 api health 시간 초과, 2분 뒤부터 이체가 60초 클라이언트 제한에 걸림(상태 0). 운영이면 기준선 이체가 60초를 넘겨 entry_status 0(필수 중단 조건)이 되므로, 새로 고침을 요청 밖으로 뺀 판(아래)을 채택했다(실제로도 '응답을 늦추지 않으려고 뒤에서 새로 고침'이 흔한 꼴) |
| 릴리스(채택판) | 이체 12/s + 잔액 10/s, 5분 | account CPU 5분 내내 한도(0.5), 이체 첫 80초 대부분 502(서킷), 그 뒤 느린 200(p50 0.5~7초)과 502 섞임, 잔액 p50 0.1~5초에 500 섞임, api 스레드 180 → 1,846, api 메모리 590MiB(한도 1GiB), 60초 초과 0건 |
| 릴리스(채택판) | 이체 **8/s** + 잔액 7/s, 5분(설계 강도) | account CPU 5분 내내 0.49~0.56(한도), 잔액 p50 0.06~2.1초·p95 최대 3.3초에 500 섞임, 이체 첫 2분 502 몰림(10초 창 61~210건 중 대부분), 그 뒤 약 40초마다 502 몰림(36~56건)과 p50 0.5~2.2초의 느린 200, account health 1~3초(3초 시간 초과 섞임), api 스레드 158 → 883, api 메모리 최대 507MiB, 60초 초과 0건 |

채택판 로그(8/s, 5분, api): INFO 'Loaded active account directory: pages=49 accounts=976' 1,348건, WARN 'Active account directory refresh failed: ...' 733건, ERROR 'Account list call failed: status=ACTIVE page=N: ...' 376건, 'Account service circuit open/exhausted ...' 362건. account: ERROR 'HikariPool-1 - Connection is not available, request timed out after 3000ms', 'Servlet.service() ... threw exception'. 로컬 Oracle v$sql: 목록 페이지와 건수 문장이 각각 약 19만 회 실행.

운영과의 차이: 운영은 eclipse-temurin 17-jre, aarch64(tb-w2)이고 에이전트가 119 로 스팬, 지표, 로그를 내보낸다. 내보내기와 aarch64 는 요청당 CPU 를 줄이지 않으므로, 같은 요청 수에 운영 account 가 더 일찍 한도에 닿는다. 런타임 판 차이(17/21)는 runAsync 의 스레드 정책(공용 풀 병렬도 1 이면 작업마다 새 스레드)에 영향이 없다. 운영 account 는 readiness 3회 실패(약 30초)면 엔드포인트에서 빠지므로 health 가 3초를 넘는 구간이 이어지면 nginx 502 와 api 연결 거부(서킷 열림)가 로컬보다 많을 것이다.

## 5. 원인 규정 (원칙 5, 6)

- 근본(`root_cause.target_id`): `core-banking-api`(api-service 의 새 릴리스). 결함을 가진 곳이자 되돌려야 재발이 막히는 곳.
- 계기(`trigger_target_id`): 비움. 계기(릴리스 롤아웃)와 결함이 같은 곳이다. 사용자, loadgen 의 요청은 평시 그대로 정당하다(원칙 6).
- 부분 점수: `core-banking-account`, `testbed-account`(과부하된 증상 위치). account 가 과부하를 견딜 용량이 없었던 것은 원본 사후 보고도 함께 든 요인이지만, 직접 계기와 고칠 곳은 호출자의 새 버전이라 부분 점수로 둔다.
- 층위: 원본 사후 보고의 결론("the immediate trigger was a bug in the dashboard", 되돌림과 핫픽스로 복구)과 같은 층위. 패치 속 코드 줄을 맞히라고 요구하지 않는다(원칙 5, 결함 버전 이미지 규칙 7). 관제 데이터로 낼 수 있는 결론: "api 를 1.4.0 으로 롤아웃한 직후부터 account 로 가는 목록 요청이 수백 배로 늘었고(DPM 실행 수, api 로그, 트레이스) 그 요청은 게이트웨이가 아니라 api 에서 온다. account 는 바뀐 것이 없고 CPU 한도에 붙었다 → api 새 버전의 요청 폭증, 롤백".

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 |
|---|---|
| 감지 | core-banking-account 서버 스팬 지연(p95)과 오류율, account 오류 로그(Hikari, Servlet) 급증(평시 F35-R 실행 때만), core-banking-api 오류 로그('Account list call failed', 서킷 열림) 급증, KCM Unhealthy, account JVM CPU 지표 이상 |
| 피해 판정(러너) | account 서버 스팬 p95 ≥ 500ms 가 3틱(평시 7일 중앙값 7.7ms). 기록: 동반 부하 이체 5xx 비율, 잔액 조회 비정상 비율, api daemon 스레드 수 |
| RCA | api 롤아웃 이벤트와 새 이미지 태그, 그 뒤 api 로그 'Loaded active account directory: pages=49 accounts=976', account 목록 문장의 DPM 실행 수 폭증, account CPU 가 한도에 붙음, account 로의 게이트웨이 요청은 그대로, api 스레드 증가, account 는 바뀐 것이 없음 |

## 7. 관측 근거 표 (119 실조회, 2026-10-09 15:30~16:45 UTC)

| 증거 | 119 표와 칸 | 조회 | 평시 결과 |
|---|---|---|---|
| 계기: 롤아웃과 이미지 태그 | CH `kcm_events_local`(reason, object_name, body) | `namespace='rca-testbed-banking' AND object_name LIKE 'testbed-api%'` reason 별 | ScalingReplicaSet 8, Pulled 4('Container image "core-banking-api:latest" already present on machine', 태그가 본문에 담김), Killing 4, Unhealthy(startup) 등. 마지막 2026-10-09 14:08(api 재배포가 수집됨) |
| 계기: 새 ReplicaSet 스펙 | PG `kcm_resources_history`(kind, name, yaml, captured_at) | `namespace='rca-testbed-banking' AND name LIKE 'testbed-api%'` 종류별 집계와 'core-banking-api:latest' 수 | replicaset 36건, pod 28건 전부 :latest(마지막 2026-10-09 14:08). deployment, service 는 7월 뒤로 안 쌓임(F42-R 시트와 같음) |
| 근본: 새 코드가 남기는 로그 | CH `lucida_logs_local`(service_name, body) | core-banking-api 7일 'Submitting transfer%', 'Loaded active account directory%', 'Account list call failed%' 수 | 158,555 / 0 / 0. api INFO 로그는 전수 수집되고, 새 문장은 평시 0건이다. 고장 때 이체마다 'Loaded active account directory: pages=49 accounts=976' 이 남는다(로컬 실측 5분 1,348건) |
| 근본: 목록 요청 폭증(DB 쪽) | CH `dpm_topsql_local`(engine oracle, sql_id, body 의 executions) | sql_id IN (82k5d7uauh4xd, 36z5znswd9sxt, 9550zwkkzcb36) 7일 | 36z5znswd9sxt(건수) 16행 110회, 9550zwkkzcb36(첫 페이지) 3행 70회, 82k5d7uauh4xd(페이지) 0행(평시 상위 목록 밖). 비교로 PK 조회 2nfgkpu2q8jqw 9,256행 111만 회. 고장 때 목록 두 문장이 초당 수백 회로 상위에 오른다. DPM Top SQL 은 표본이 아니라 Oracle 누적 통계의 차분이다 |
| 피해: account CPU | VM `apm.agent.otel.java.jvm.cpu.recent_utilization{service_name="core-banking-account"}`, `jvm.cpu.count` | 7일 중앙값, 최대 | 중앙값 0.0146, 최대 0.0607(CPU 하나 기준, 한도 0.5). cpu.count 1. 에이전트가 10초마다 내보내는 런타임 지표라 트레이스 표본과 무관 |
| 피해: account 지연 | VM `apm.agent.otel.java.percentile95{service_name="core-banking-account"}` | 7일 중앙값, 최대 | 중앙값 7.69ms, 최대 3,003ms(F35-R 실행 때). 러너 피해 판정에 쓴다(표본 기반이지만 고장 때 표본이 초당 수십 개) |
| 전파: api 스레드 | VM `apm.agent.otel.java.jvm.thread.count{service_name="core-banking-api", jvm_thread_daemon="true"}`(러너 템플릿 otel-jvm-daemon-thread-count-v1) | 7일 | 중앙값 44, 최대 49. api cpu.count 7일 내내 1(새로 고침이 작업마다 새 스레드) |
| 전파: account 고장 로그 | CH `lucida_logs_local` | core-banking-account 7일 '%Connection is not available%' | 443건, 모두 F35-R 실행(ORA-28000) 때. 평시 0 |
| 전파: account readiness | CH `kcm_events_local` | `object_name LIKE 'testbed-account%'` | Unhealthy 29건(마지막 2026-10-09 07:12, 다른 시나리오 실행 때). account 프로브 실패가 수집된다 |
| 참고: 요청 경로 | CH `otel_traces_local` | core-banking-api 7일 span_name, kind | 서버 'GET /api/transfers' 17,333, 'POST /api/transfers' 7,548, 클라이언트 'GET'(transfer 로) 16,994, 'POST'(account 로) 7,440. 고장 때 account 로 가는 클라이언트 'GET' 이 이체 수의 약 49배로 는다(표본 10%, 보조 증거) |
| 감별: Oracle 여유 | VM `dpm.oracle.instance.cpu_time` | 7일 | 중앙값 0.17, 최대 1.37(상한 2). 목록 질의는 1,001행 표라 고장 때도 여유(로컬 Oracle 2 CPU 중 0.25~0.45) |

표본 데이터(트레이스 10%, APM 스팬 지표)만으로 증명하지 않는다. 결정 증거는 KCM 이벤트(전수), api 와 account 로그(전수), DPM Top SQL 실행 수(Oracle 누적 통계), JVM 런타임 지표(CPU, 스레드)다.

## 8. 감별

- must_support: api 롤아웃과 1.4.0 태그, api 로그 'Loaded active account directory' 가 이체마다(평시 0), DPM 의 account 목록 문장 실행 수 폭증, account CPU 한도와 p95 초 단위, api daemon 스레드 수백, account Hikari 오류와 Unhealthy, api 'Account list call failed', 'Active account directory refresh failed', 서킷 열림. account 롤아웃, 설정 변경 없음, Oracle 여유, transfer 와 commerce 정산 정상.
- must_rule_out: account 자체 결함(F41-R), DB 문제(F42-R, F01-P, F35-R), 하류 주소 오설정(F39-R), 단순 트래픽 급증(F07-H: 게이트웨이에서 account 로 오는 요청은 그대로), api 이름 해석 실패(F37-R).
- contrast_with: F41-R, F42-R, F07-H, F39-R, F37-R.
- 러너 success(account p95)는 F41-R 녹화에서도 오를 수 있다. 녹화 검증이 롤아웃 대상(F47-R 은 api, F41-R 은 account)과 api 로그 'Loaded active account directory', DPM 목록 문장 실행 수로 가른다.

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

- 강도는 릴리스 하나로 고정(approved-fixed-f47-r, 실행기 파라미터 = 이미지 태그). 피해는 이체 수에 비례한다(이체 1건 = 목록 요청 49건). 동반 부하가 피해를 만든다: 기준선만(이체 초당 약 0.2건)이면 목록 요청 초당 약 10건으로 account 가 버틴다.
- 부하 계산: 동반 부하 load.north_south core-banking transfer-heavy-surge.js 20rps(이체 40% 약 8건/초, 잔액 35% 약 7건/초, 거래 내역 15% 약 3건/초, 목록 10% 약 2건/초). 목록 요청 약 8 × 49 = 392건/초. account 목록 한 페이지는 평시 서버 스팬 1.8ms(119 트레이스 7일 6,979건 p50), PK 조회 1.05ms 에 요청당 CPU 약 1.4ms(VM jvm.cpu.recent_utilization 0.029 at 약 12rps, 바닥 0.015). 페이지 요청을 요청당 CPU 2ms 로 잡으면 0.78 CPU 로 한도 0.5 의 약 1.6배다. 실패하면 재시도(3회)가 더한다. 같은 부하에 매니페스트 api 면 account 는 잔액 약 7건/초 + 이체 검증 약 8건/초 + 목록 2건/초로 0.05 CPU 안팎이다. 로컬 실측(§4)도 8건/초에서 account 가 5분 내내 한도에 붙었다.
- 거래 내역 3건/초는 Oracle 전수 스캔 약 0.64 CPU(F42-R 과 같은 동반 부하)라 Oracle(2 CPU)은 여유가 있다.
- success: account_p95 ≥ 500ms, 3틱(15초 틱, 관측은 60초 창 최대 30초 지연). 로컬 8건/초에서 잔액 p50 이 0.5~2초라 서버 p95 는 이를 넘는다.
- min_hold 10m, timeout 18m, max_injection_duration 26m: 새 api 파드 기동 약 1분(startupProbe), 옛 파드는 새 파드가 Ready 가 된 뒤 내려가고, 동반 부하 ramp 2m 뒤 포화가 자리 잡는다. 동반 부하 22분 15초(ramp 2m + hold 20m + ramp_down 15s)를 덮는다.
- must_rule_out: achieved_rps < 5, Oracle 파드 NotReady(2틱).
- abort: entry_status == 0(2틱). api 는 새로 고침을 요청 밖에서 돌려 요청 스레드가 묶이지 않고, account 가 느리거나 빠져도 api 는 느린 200 이나 502 로 답한다(로컬 실측 두 번 모두 60초 초과 0건). nginx 와 노드는 건드리지 않는다.
- recovery: target_health 200, api 파드 Ready, 기준선 이체 5xx 비율 < 0.05(2틱). account Ready 와 api available 수는 러너 허용 목록에 없어 두지 않는다(account 는 요청이 멈추면 스스로 돌아온다).
- cleanup: 이미지 원복, available(180초), 1.4.0 을 쓰는 파드가 없어진 뒤 tb-w2 containerd 에서 1.4.0 의 이름 참조와 ID 참조를 모두 지우고 둘 다 없는지 확인. 옛 api 프로세스가 내려가면 쌓인 새로 고침 스레드도 함께 사라진다. 데이터 부작용 없음(새 코드는 읽기만 한다).
- 노드 디스크: tb-w2 이미지 파일시스템 73.3%(2026-10-09). 릴리스는 기본 이미지와 같은 eclipse-temurin 기반이라 새로 쓰는 것은 앱 층뿐이다. preflight 가 80% 미만을 요구한다. 109 scenario-runner 컨테이너의 known_hosts 에 tb-w2(192.168.122.11)가 있다(2026-10-09 확인).
- api 메모리: 로컬 실측 api 메모리 최대 590MiB(12건/초, 스레드 1,846). 운영 api 는 -Xmx 없이 한도 1GiB 라 같은 부하면 한도 아래다. 스레드가 더 쌓여 OOMKill 이 나더라도 api 재시작은 새 코드가 만든 일이라 정답은 그대로다(must_rule_out 에 두지 않는다).

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Cloudflare 2025-09-12, 공식 블로그 링크)와 요소별 대응표, 기전 같음 (§2)
- [x] 장부 갱신(§4-1, §6), 묶음 J 5/53(9.4%), E 로 세도 3/53(5.7%), 정답 위치 은행 API 2/53(3.8%), 결제 경로 10/53(18.9%, 이 후보와 무관), 은행 16(쇼핑몰 26, 음식배달 11. 음식배달은 같은 기전이 필수 중단 조건에 걸림). 어느 축도 20% 에 닿지 않는다 (§3)
- [x] 근본 원인 위치: 패치 `api-service.patch:101-115`, `:125-137`, `:34-52`, `:162`, 매니페스트 `ApiService.java:28-38`, `20-api-service.yaml`, 피해 쪽 `AccountController.java:32-42`, `21-account-service.yaml` 각 줄 (§4)
- [x] 근본 원인의 흔적 조회: CH kcm_events_local Pulled 본문의 이미지 태그, PG kcm_resources_history ReplicaSet image, CH lucida_logs_local api 로그(새 문장 평시 0), CH dpm_topsql_local 목록 문장 sql_id(평시 7일 16행), VM account jvm.cpu.recent_utilization, api jvm.thread.count (§7)
- [x] 핵심 증거가 표본 데이터만이 아님: KCM 이벤트, 로그 전수, DPM 실행 수, JVM 런타임 지표 (§7)
- [x] 계기 흔적: 롤아웃 이벤트와 새 태그(인공 지연 없음, 지연은 account 의 실제 CPU 포화에서 남) (§7)
- [x] 정답이 관제 데이터로 낼 수 있는 결론: "api 롤아웃 직후 api 에서 account 로 목록 요청이 폭증, account 는 그대로이고 포화 → api 새 버전, 롤백". 코드 줄 추론 불필요 (§5)
- [x] 정답지 세 칸(근본 core-banking-api, 계기 비움, 부분 core-banking-account, testbed-account) (§5). class A(코드 결함). fault_pattern 은 P1~P8 어느 것도 '호출자의 요청 폭증'에 맞지 않아 생략(새 P 번호를 만들지 않는 규칙)
- [x] 감지, 피해 판정, RCA 증거 구분 (§6)
- [x] 시나리오 id 흔적 없음: 패치 문자열(AccountDirectory, requireActive, listAccounts, 'Loaded active account directory', 'Active account directory refresh failed', 'Account list call failed', 'From account is not active')에 id, fault, bug, chaos, scenario 없음(테스트로 확인), 태그 1.4.0, 실행기 인자와 상태 파일에 id 없음
- [x] 피해 계산: 목록 요청 약 392건/초 × 요청당 CPU 약 2ms ≈ 0.78 CPU > 한도 0.5, 로컬 실측 8건/초에서 account 가 5분 내내 한도, 잔액 p50 0.5~2초, 이체 502 몰림. 서킷브레이커는 api 쪽이라 account 의 부하를 잠깐씩 끊을 뿐 새로 고침이 다시 몰린다 (§9)

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- 배포 단계에서 `bash scripts/scenarios/fault-images/build.sh f47-r` 로 109 docker 에 core-banking-api:1.4.0 이 있어야 preflight 가 통과한다(빌드 전후 기본 이미지 ID 비교는 스크립트가 한다).
- KCM 이 새 파드의 'Pulled ... core-banking-api:1.4.0' 을 잡는지, kcm_resources_history 가 새 ReplicaSet 을 잡는지.
- api 로그 'Loaded active account directory: pages=49 accounts=976' 이 이체 수만큼 남는지, DPM Top SQL 에 82k5d7uauh4xd, 36z5znswd9sxt 가 상위로 오르는지, account jvm.cpu.recent_utilization 이 0.5 에 붙는지, api daemon 스레드가 수백으로 오르는지.
- account p95 가 500ms 를 넘겨 success 가 서는지, account readiness 가 실제로 빠지는지(빠지면 nginx 502 와 api 서킷 열림이 로컬보다 많다), 인시던트가 생기는지(원칙 7).
- entry_status 가 0 이 되지 않는지(기준선 이체가 60초를 넘지 않는지), api 메모리가 한도 아래인지.
- 녹화 검증: F41-R 과 러너 success 가 겹칠 수 있으므로 롤아웃 대상이 api 이고 api 로그 'Loaded active account directory' 와 DPM 목록 문장 폭증이 있어야 F47-R 녹화로 인정.
- cleanup 뒤 tb-w2 containerd 에 1.4.0 의 이름 참조와 ID 참조가 모두 남지 않았는지(recovery 가 확인).
