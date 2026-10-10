---
title: F59-P 설계 시트 (원장 쪽 명세서 백필 Job 이 은행 모든 계좌의 이체 내역을 읽어 transfer 풀이 묶이고 이체와 정산 실패)
status: Draft
owner: project
last_reviewed: 2026-10-10
tags:
  - scenario
  - design
  - batch
  - oracle
summary: 원장 쪽 계좌 명세서 백필 Job 이 워커 48개로 계좌마다 transfer 이체 내역 API 를 페이지마다 읽어, 호출마다 Oracle 이 이체 표 638만 행을 전수 스캔하고 그 호출이 transfer Hikari 연결을 모두 쥐어 이체가 500, api 502, commerce 정산이 실패하는 시나리오. 원본은 Atlassian Bitbucket 2025-05-08(내부 서비스의 대규모 백필 작업이 API 엔드포인트를 거쳐 비싼 질의로 주 DB 를 압박). F59-R 의 banking 판.
---

# F59-P 설계 시트

## 1. 요약

원장(ledger) 쪽이 계좌 명세서 보관소를 채우려고 일회성 백필 Job `ledger-statement-backfill` 을 rca-testbed-banking 에 띄운다. ledger-service 이미지에서 bash 와 curl 로 도는 스크립트가 account-service 에서 계좌 목록(1,001개)을 한 번 받고, 워커 48개가 계좌를 같은 순서로 돌며 계좌마다 쪽을 나눠(워커 w 는 w, w+48, w+96, ... 쪽) transfer-service 의 이체 내역 `GET /api/transfers?fromAccount=<계좌>&page=N&size=500` 을 짧은 페이지가 나올 때까지 읽는다. 계좌가 아니라 쪽을 나누므로 이체가 몰린 몇 계좌에서도 48개가 늘 바쁘다. 이체 내역 목록은 선택 조건(`(:fromAccount is null or from_account = :fromAccount) and ...`) 꼴이라 Oracle 이 페이지와, Spring Data 가 페이지 질의에 붙이는 건수 질의를 모두 BANKING.TRANSFERS(약 638만 행) 전수 스캔으로 답한다. 호출 하나가 약 0.25초 CPU 를 쓰며 transfer Hikari 연결 하나를 쥐고, Oracle 2 CPU 는 초당 약 3~6 호출만 끝낸다. 동시 48 > 풀 15 라 풀이 늘 백필 스캔에 묶이고, 이체가 같은 풀에서 연결을 3초 기다리다 500 이 되어 account 재시도와 서킷 열림, api 502, commerce checkout 정산 실패로 이어진다. transfer, account, api, Oracle 의 코드, 설정, 스키마, 인덱스는 하나도 바뀌지 않는다.

비유: 은행 창구(transfer)에 직원 열다섯 명이 있다. 원장팀이 "모든 고객의 거래 내역 출력"을 마흔여덟 명에게 시켜 창구로 보냈는데, 거래 장부에 고객별 색인 카드가 있어도 이 조회 양식은 장부 전체를 처음부터 끝까지 넘겨야 답할 수 있다. 직원 열다섯이 모두 장부 넘기기에 매달리자 송금하러 온 손님은 번호표를 뽑고 3초 만에 돌아간다. 직원도 장부도 멀쩡하고, 바뀐 것은 창구에 몰려온 출력 요청뿐이다.

## 2. 원본 사례

- 기업: Atlassian Bitbucket Cloud
- 날짜: 2025-05-08 15:26 UTC 시작(사후 보고 게시 2025-05-19 16:43 UTC), 2025-05-09 11:19 UTC 완전 정상
- 링크: [공식 상태 페이지 사후 보고](https://bitbucket.status.atlassian.com/incidents/z1lk6hmkp9gt) "Bitbucket has degraded performance"
- 요약(원문 인용 중심): "The event was caused by a backfill job running from an internal Atlassian service, which triggered an excessive call volume of expensive queries and pressure on database resources." 근본 원인 절: "an internal high-scale backfill job that triggered excessive load on certain API endpoints, which eventually impacted the database through resource-intensive queries and operations. This led to additional load from retries by dependent services, increasing the total recovery time." 실시간 감시가 즉시 탐지했고 백필 작업을 멈춤, 주 DB 자동 장애 조치 뒤 15분 만에 회복, 그 뒤 하류 서비스의 밀린 재시도가 DB 성능에 계속 영향. 재발 방지: 읽기를 복제본으로, 자원 소모가 큰 내부 API 엔드포인트의 요청 한도 조정, 질의 최적화, 하류 재시도 정책 조정.
- `docs/ref-real-world-incidents.md` M20 에 이미 있다(F59-R 이 2026-10-10 추가). 같은 원본을 다른 도메인, 다른 질의 기전에 쓰는 선례: F48-R/F48-P(GitHub 2026-07-24), F53-R/F53-P/F32-P/F35-H/F23-P(Resend 2024-02-21).
- 원본이 적지 않은 것: 작업 이름, 엔드포인트, DB 종류. 재구성의 "계좌마다 이체 내역 API 를 끝까지 읽음"은 원본이 말한 "API 엔드포인트를 거친 자원 소모가 큰 질의"의 구체화이고, 이 테스트베드 banking 에서 실제로 비싼 API 질의가 그것이다(§4).

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 내부 서비스에서 대규모 백필 작업이 돌기 시작 | 원장 쪽 계좌 명세서 백필 Job `ledger-statement-backfill` 시작(ledger 이미지, 워커 48) |
| 원인이 된 결함 | 작업이 특정 API 엔드포인트에 과도한 호출을 보내고, 그 엔드포인트의 질의가 자원 소모가 큼 | 작업이 transfer 이체 내역 API 를 모든 계좌에 대해 병렬로 페이지마다 호출. 그 질의가 호출마다 이체 표를 전수 스캔(호출당 약 0.25초 CPU 두 번까지) |
| 전파 경로 | API 엔드포인트 → 비싼 질의 → 주 DB 자원 압박 → 웹과 API 전반 지연, 간헐 오류. 하류 재시도가 부하를 더함 | transfer 내역 → Oracle 전수 스캔 → transfer Hikari 15 고갈과 Oracle 2 CPU 포화 → 이체 연결 대기 실패 500 → account 재시도와 서킷 열림 → api 502, commerce 정산 실패. 백필 워커도 실패한 페이지를 다시 부름 |
| 사용자 증상 | 웹사이트, API 지연과 간헐 오류 | 은행 이체 절반 이상 실패, commerce checkout 정산 단계 실패, 거래 내역 느려지거나 실패, 잔액 조회는 느려지되 대개 성공 |
| 탐지된 경로 | 실시간 감시가 즉시 탐지 | lucida 이상 탐지(transfer, account, api 로그 이상, Oracle 지표 수준 이동, transfer 프로브 실패 이벤트), 인시던트 |
| 완화와 복구 | 백필 작업 중지, DB 부하 덜기, 장애 조치 | cleanup 이 Job 을 지움. 진행 중이던 스캔이 수 초 안에 끝나며 풀이 풀림 |

기전 확인: 원인(내부 작업이 비싼 API 질의를 과도하게 부름)에서 증상(그 API 와 같은 풀, 같은 DB 를 쓰는 사용자 기능의 지연과 오류)까지 고리가 원본과 같다. 서비스나 DB 의 결함, 설정 변경은 원본에도 재구성에도 없다.

## 3. 숫자 근거 (2026-10-10, `scenario-stats.py`, 정식 + 후보 합계 77, 이 후보 전)

| 축 | 값 | 판단 |
|---|---|---|
| 서비스 | commerce 27, core-banking 25, food-delivery 25 | 은행과 음식배달이 같이 가장 적다. 은행 25 → 26 |
| 묶음 | A 7, B 7, C 6, D 7, E 3, F 3, G 8, H 2, I 1, J 11, K 2, L 7, M 1, O 1, P 10, Q 1 (N 0) | 주 묶음 E 3(3.9%) → 4(5.1%). 보조 F 로 세도 3 → 4. 20% 에 멀다 |
| 정답 위치 | 주문 서비스 8(10%)이 최다. 내부 배치 작업 1 | 1 → 2(2.6%) |
| 결제 경로 합계 | 12(15.6%) | 이 후보는 결제 경로가 아니라 12/78(15.4%) |
| 부품 지도 | core-banking: oracle 8, account 8, transfer 8, api 2, ledger 1, kafka 0, nginx 0 | 증상 부품(transfer, Oracle)은 많지만 정답은 그 밖의 "배치와 크론" 인프라 층. 0 인 kafka, nginx 는 §3 목록에서 막힘 |

왜 이 후보인가: 3단계 목록에서 0 인 묶음(N)과 0 인 정답 위치(메시지 브로커, 게이트웨이, 알림 서비스, DNS)를 노린 후보는 모두 원칙 3, 7, 9 로 막혔고, 남은 것 가운데 서비스가 가장 적은 쪽(은행)이고 묶음(E 3)과 정답 위치(1)가 낮다. 관제 AI 가 "transfer 풀 고갈과 Oracle 포화면 transfer 릴리스(F42-R)나 행 잠금(F01-P)" 이라고 외워 찍는 지름길을 막는 H 짝이기도 하다: 같은 증상에서 정답이 클러스터 안의 새 작업이다. 같은 원본의 F59-R(food)과는 DB 엔진(MySQL offset 건너 읽기 대 Oracle 선택 조건 전수 스캔), 풀, 피해 경로(주문 대 이체와 교차 도메인 정산)가 다르다.

### 3단계 후보 목록 (실제 기전 × 부품, 순서는 숫자 순)

| # | 후보 | 원본 | 판단 |
|---|---|---|---|
| 1 | 원장 쪽 명세서 백필 Job 이 계좌마다 쪽을 나눠 transfer 이체 내역을 끝까지 읽음(k8s.job, 정답 내부 배치 작업) | Atlassian Bitbucket 2025-05-08 공식 | **채택**. 은행 최소, E 3, 정답 위치 1 |
| 2 | 폐기된 내역 API 가 지나친 양을 읽어 DB 과부하: banking ledger 의 무제한 원장 목록(GET /api/ledger-entries, page 미지정 시 unpaged)을 부르는 호출자 | OpenNode 2024-05-14 공식 상태 페이지("a deprecated API that was loading an excessive amount of data into the database, causing an overload") | 버림(원칙 9, 7): ledger 는 사용자 경로 밖이고, Oracle CPU 만 누르는 꼴은 짧은 OLTP 를 굶기지 못한다는 Onfido 행 실측(rejected 2026-10-09)과 같은 벽. 피해는 앱이 기다리는 풀을 함께 쥘 때만 나는데 ledger 풀은 이체가 쓰지 않는다 |
| 3 | 클라이언트 새 버전 버그가 백엔드로 요청 폭증(은행 앱 → api) | Close 2024-09 공식 상태 페이지(브라우저 앱 새 버전 버그가 영향 큰 요청을 크게 늘려 DB 과부하) | 버림(원칙 3, 4): 클라이언트는 loadgen 이라 그 릴리스가 119 에 남지 않아 계기 흔적이 없고, 남는 꼴은 F07-H(진입 폭주)와 같다 |
| 4 | 외부 API 의 이례적 부하가 은행 내역 경로를 누름 | Lever 2025-02-18 공식 상태 페이지(이례적인 외부 API 부하가 DB 문제를 일으킴, 요청당 DB 시간 제한을 재발 방지로) | 버림(카탈로그 §1, 원칙 1): 진입점 폭주는 F07-H 와 같은 수단(load.north_south)이고, 원본이 부하의 정체와 질의를 밝히지 않아 대응표가 서지 않는다 |
| 5 | food payments 의 order_id 유일 제약(인덱스 역할) 제거로 결제 생성 조회 전수 스캔 | Chargebee 2018-03-02 공식 | 버림(원칙 9): payment 의 결제 생성 경로(PaymentService.processPayment)는 order_id 로 조회하지 않아 느려질 질의가 없다. 결제 경로 12/77 이기도 하다 |
| 6 | food payment 릴리스가 인덱스 없는 중복 결제 확인 질의를 더해 MySQL 포화(J+F, 결제 서비스) | ImprovMX 2025-10-08 공식 | 순위 낮음(버리지 않음): J 가 11 로 가장 많은 묶음이고 결제 경로 12 → 13(16.7%). 채택 후보보다 숫자가 뒤 |
| 7 | 은행 Kafka 브로커 멈춤(정답 메시지 브로커 0) | Dead Man's Snitch 2014-08-10(발행자 차단) 계열 | 버림(원칙 7): banking 발행은 모두 outbox 릴레이라 이체 요청 경로와 끊겨 있고 원장 적재만 늦어지는 조용한 장애. ledger 소비자 실패 로그만으로는 인시던트가 서지 않는다는 반려(2026-10-09, 10)와 같은 벽 |
| 8 | food notify 새 릴리스의 CPU, 메모리 과사용이 같은 노드 서비스를 굶김(정답 알림 서비스 0) | Balena 2025-03-03(2차), CircleCI 2024-08-01 공식 | 버림(원칙 9, 7): notify 컨테이너 한도가 cpu 500m, 1Gi 라 노드를 굶기지 못하고, notify 자체는 사용자 경로 밖이다 |
| 9 | banking nginx 레이트 리미터나 라우트 설정 배포(정답 게이트웨이 0) | GitHub 2025-09-15 공식 월간 보고 | 버림(원칙 3): 119 에 nginx 로그와 스팬이 없어 계기와 거절이 관측되지 않음(rejected 2026-10-08, 10 과 같은 벽) |
| 10 | 같은 원본으로 banking 이체 일별 통계(GET /api/transfers/stats/daily?days=90)를 부르는 리포팅 작업 | Atlassian Bitbucket 2025-05-08 | 버림(#1 과 같은 주입 대상): #1 과 같은 Job 수단, 같은 transfer 풀이고 질의만 다르다. 하나만 낸다 |

부품: banking transfer(+Job), banking ledger, 은행 앱 클라이언트, banking api 진입, food payments 표, food payment, banking Kafka, food notify, banking nginx(9개, 4개 이상 충족).

## 4. 인과 사슬 (코드와 인프라 위치)

1. 계기: Job `ledger-statement-backfill` 생성(`scripts/scenarios/profiles/k8s_job_executor.py:90-103 CONTRACTS["F59-P"]`, `:111-143 STATEMENT_SCRIPT`). 스크립트는 `GET http://testbed-account:8081/api/accounts`(page 미지정이라 unpaged, 1,001행)를 한 번 받아 계좌 id 를 뽑고, 워커 48개가 모두 그 순서대로 계좌를 돈다. 계좌마다 워커 w 는 w, w+48, w+96, ... 쪽을 `curl --max-time 60` 으로 읽고, 200 이 아니면 2초 뒤 같은 쪽을 다시 부르며, 500행 미만 쪽(끝 너머의 빈 쪽 포함)이 오면 다음 계좌로 간다. 목록 순서(109 조회)는 ACC-1001~1007 이 1~7번, commerce-settlement 가 1001번이고, from_account 출금이 500건을 넘는 계좌는 9개뿐이다(settlement 401만, ACC-1001~1007 각 32.5만~34.9만, ACC-9001 987).
2. 요청: `core-banking/transfer-service/src/main/java/com/corebanking/transfer/controller/TransferController.java:51-64 (list)` 가 page, size 를 받아 `PageRequest.of(page, size)` 를 만든다. page 상한, 호출자 구분이 없다.
3. 질의: `TransferRepository.java:17-28 (search)` 의 JPQL 이 `(:fromAccount is null or t.fromAccount = :fromAccount) and (:toAccount is null or ...) and ...` 꼴이라 Oracle 이 idx_transfers_from 을 쓰지 못하고 TABLE ACCESS FULL 로 답한다(rejected 2026-10-08 행의 실행 계획 실측, 2026-10-10 재확인). Page 반환이라 Spring Data 가 건수 질의를 붙인다(0쪽에서 내용이 size 보다 적으면 생략).
4. DB 비용(109 Oracle 2026-10-10, 읽기 전용): TRANSFERS 6,383,259행(commerce-settlement 출금 401만, ACC-1001~1007 각 약 33~35만, 나머지 계좌 대부분 수십 행 이하). 같은 꼴 바인드 질의로 ACC-1001 건수 0.25초, ACC-5863(15행) 건수 0.22초, ACC-1001 offset 30만 페이지 0.38초. DPM Top SQL 의 건수 문장 byb2y2hv7p4ag 평시 실행당 평균 약 0.25초 CPU, 논리 읽기와 물리 읽기 약 10만(직접 경로 읽기). cpu_count 2, parallel_max_servers 1(질의 하나가 CPU 하나).
5. 풀: `core-banking/transfer-service/src/main/resources/application.yml:12-17`(Hikari maximum-pool-size 15, connection-timeout 3000). 백필 호출이 연결 15개를 모두 쥐면 나머지 요청은 3초 뒤 `HikariPool-1 - Connection is not available, request timed out after 3000ms` 로 실패. 119 에 같은 문구가 앞선 시험(F42-R 계열)에서 transfer 로그로 남은 것 확인(30일 242건).
6. 전파: `core-banking/account-service/src/main/java/com/corebanking/account/client/TransferClient.java:28-58`(재시도, 서킷브레이커 transferClient) → api `POST /api/transfers` 502. commerce-payment 의 정산 이체는 transfer Service 를 바로 부르므로(`commerce/k8s/02-configmaps.yaml:46` BANKING_TRANSFER_URL testbed-transfer.rca-testbed-banking.svc.cluster.local:8082) 같은 풀 대기에 걸려 checkout 정산 실패.
7. 부수: transfer readiness(`core-banking/k8s/22-transfer-service.yaml:87-93`, /actuator/health DB 확인, 시간 제한 3초)가 같은 풀을 기다려 실패할 수 있다. 세 번 연속이면 NotReady 로 빠져 백필 호출도 연결 거절로 빨리 실패하고, 진행 중이던 스캔이 끝나 풀이 풀리면 다시 Ready, 백필이 다시 채운다. liveness(`:97-103`)는 /actuator/health/liveness(livenessState)라 재시작하지 않는다.

인프라: rca-testbed-banking(tb-w2), Job 은 nodeSelector 로 tb-w2, 이미지 core-banking-ledger:latest(tb-w2 containerd 에 있음, ledger Deployment 가 imagePullPolicy Never 로 사용 중, Ubuntu 24.04 기반에 bash, curl 8.5.0, grep 있음 — 2026-10-10 ledger 파드에서 확인). transfer 파드 하나(CPU 한도 500m), Oracle testbed-oracle-0(gvenzl/oracle-free:23-slim, 한도 cpu 2000m).

## 5. 원인 규정 (원칙 5, 6)

- `root_cause.target_id`: `rca-testbed-banking/job/ledger-statement-backfill`(target_kind batch_job). 감당할 수 없는 양의 비싼 호출을 보낸 작업이 원인이다. transfer 와 Oracle 은 정당한 질의를 처리했을 뿐이고, 같은 질의를 은행 앱은 일곱 계좌에 0쪽 20건으로 초당 약 0.25회 부른다(원칙 6: 요청 A 가 B 를 실행시켰고 C 가 났다면 A).
- `trigger_target_id`: 비움(계기 = 작업 시작 = 근본).
- `scoring.partial`: core-banking-transfer(풀이 묶인 곳), banking-oracle 과 그 TRANSFERS(포화된 곳), core-banking-ledger(작업이 쓴 이미지의 주인, Deployment 는 그대로), core-banking-account, core-banking-api, commerce-payment(증상 서비스).
- 원본 층위: Bitbucket 사후 보고도 원인을 "내부 서비스의 대규모 백필 작업"으로 지목하고 작업을 멈춰 완화했다. "내역 목록이 선택 조건 꼴이라 인덱스를 못 쓰는 설계"는 정답이 아니라 피해를 키운 조건이다(원칙 5). 원본도 질의 최적화를 재발 방지 항목의 하나로만 둔다.
- 관제 AI 가 낼 수 있는가: KCM 에 실패 직전 새 Job 과 파드가 있고, DPM Top SQL 에서 평시 내역 문장의 실행 수가 몇 배로 뛰며, transfer 스팬의 호출자가 그 파드(curl)이고 계좌가 수백 개다. 롤아웃, 설정, 인덱스 변화는 없다. 이 넷으로 "새 배치 작업이 transfer 와 Oracle 을 눌렀다"는 결론에 닿는다.

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 | 기대 |
|---|---|---|
| 감지 | transfer, account, api 로그 이상(ERROR, WARN 급증), Oracle stream-anomaly(cpu_time, db_time, active_session 수준 이동), transfer 프로브 실패 이벤트(Unhealthy), commerce checkout 실패 | 인시던트 생성. 같은 transfer 풀 고갈, Oracle 포화 꼴인 F42-R 과 같은 신호 |
| 피해 판정(러너) | 동반 부하 transfer-heavy-surge 의 이체 5xx 비율 ≥ 0.3 (3틱) | 평시 0. §9 계산 |
| RCA 증거 | KCM Job, 파드 이벤트(계기), DPM Top SQL 실행 수와 실행당 비용(기전), transfer, account 로그(전파), transfer 스팬 url.query, client.address, user_agent(보조) | §7 |

## 7. 관측 근거 표 (119 실조회, 2026-10-10 18:00~18:30 UTC, 평시)

| 증거 | 표, 칸 | 조회 | 평시 결과 |
|---|---|---|---|
| 계기: Job 과 파드 이벤트 | CH `lucida.kcm_events_local`(namespace, object_kind, reason, object_name) | `SELECT object_kind, reason, count() FROM lucida.kcm_events_local WHERE timestamp > now() - INTERVAL 30 DAY AND namespace='rca-testbed-banking' GROUP BY 1,2` | Pod Unhealthy 1,285, Deployment ScalingReplicaSet 124, Pod Started, Pulled, Scheduled, Created 각 62 등. **Job 0건**. Job 이벤트 수집 자체는 30일 창 안의 kcm-128 Job kcm-agent-enroll(SuccessfulCreate 4, Completed 2, FailedCreate 4, BackoffLimitExceeded 2, 2026-09-17)로 확인. 그보다 앞선 commerce F03-H Job(2026-08-14, 08-25)도 같은 표에 남아 있다 |
| 기전: 이체 내역 건수 문장 | CH `lucida.dpm_topsql_local`(engine='oracle', sql_id, body JSON executions, avgElapsedTime, avgCpuTime, avgLogicalReads) | `... sql_id='byb2y2hv7p4ag'` 시간별 합 | 시간당 실행 313~1,829(분당 약 15~30), 실행당 평균 245~248ms, CPU 약 242ms, 논리 읽기 약 10만. 페이지 문장 99nsgbv90qrn7(0쪽 20건) 실행당 약 2ms. Oracle Top SQL 본문에는 sqlText 가 없어 sql_id 로 가른다 |
| 기전: 질의 비용 | 109 Oracle 직접(읽기 전용, SET TRANSACTION READ ONLY) | 선택 조건 꼴 바인드 질의(건수, 0쪽 20건, offset 30만) | 건수 0.25초(ACC-1001), 0.22초(ACC-5863, 15행), offset 30만 페이지 0.38초. TRANSFERS 6,383,259행, 계좌 1,001개 |
| 기전: Oracle 상태 | VM `dpm.oracle.instance.cpu_time`, `db_time`, `session.active_session` | 6시간 중앙, 최대 | cpu_time 중앙 0.14, 최대 1.07 / db_time 중앙 0.15, 최대 1.16 / active_session 중앙 0, 최대 2 |
| 호출자: transfer 스팬 | CH `lucida.otel_traces_local` span_attributes `url.query`, `user_agent.original`, `client.address`(10% 표본) | `... service_name='core-banking-transfer' AND span_name='GET /api/transfers' GROUP BY 1,2,3`(1일) | 일곱 행뿐: fromAccount=ACC-1001~1007&page=0&size=20, Java/17.0.18, 10.244.1.222(api 파드), 행마다 700~848건, 평균 244~246ms |
| 전파: transfer 풀 고갈 문구 | CH `lucida.lucida_logs_local`(전수) | `body ILIKE '%Connection is not available%'`(30일, banking) | transfer 'HikariPool-1 - Connection is not available, request timed out after 3000ms' 242건, account 56건(앞선 시험들). 수집과 문구 확인 |
| 피해 표본: 이체 완료 | CH `lucida.lucida_logs_local` 'Transfer COMPLETED%' | 1일 | 73,322건 |
| 배제: 롤아웃 없음 | KCM ScalingReplicaSet, Pulled(이미지 태그) | 녹화 구간 | 평시 transfer, account, api 의 Pulled 는 모두 :latest |

표본 데이터만으로 증명하지 않는다: 계기(KCM 이벤트), 기전(DPM Top SQL 실행 수와 비용, Oracle 지표), 전파(로그)는 전수 수집이다. 스팬의 호출자 정보는 보조다(백필 호출이 초당 수 건이라 10% 표본에도 분당 수십 건이 남을 것으로 본다). 작업 파드 자신의 stdout 은 119 에 수집되지 않는다(kcm_pod_logs_local 0행, OTel 없는 파드).

## 8. 감별

- must_support, must_rule_out: 정답지 그대로(`scenario-metadata.json` F59-P).
- contrast_with:
  - F42-R: 같은 transfer 풀 고갈, Oracle 포화, 이체 5xx. 원인이 transfer 릴리스(ScalingReplicaSet, 새 태그 Pulled)이고 무거운 문장이 이체 경로의 새 문장(오늘 합계). F59-P 는 롤아웃 없음, 늘어난 것은 평시 내역 문장, 호출자는 새 Job 파드.
  - F01-P: 같은 transfer 풀 고갈, 정산 실패. 대기 이벤트가 enq: TX, blocking_session 있음. F59-P 는 잠금 대기 없음.
  - F54-R: 같은 내역 문장이 중심이지만 계정 프로필 한도로 내역만 ORA-02395, 이체는 정상.
  - F47-R: 은행 안 호출 폭주 꼴이지만 api 릴리스가 account 를 누름.
  - F17-R: transfer 의 Service 이탈 꼴이지만 원인이 프로브 설정.
  - F59-R: 같은 원본, 같은 수단의 food 판(dispatch 목록 offset, MySQL).
- 관계: F59-P 는 사례군 F59 의 P(같은 원본과 원인 꼴, 대상 도메인과 질의 기전이 다름). F42-R 과는 "같은 증상, 다른 원인" 관계라 시트와 정답지 contrast_with 에 적었다.

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

- 강도(고정 1단 `approved-fixed-f59-p`): 워커 48, 페이지 500, 요청 시간 제한 60초, activeDeadlineSeconds 1800. min_hold 10m, timeout 18m, max_injection_duration 25m.
- 처리량 계산: 호출 하나의 Oracle 비용은 쪽에 따라 다르다. 큰 계좌의 내용 있는 쪽은 건수(약 0.25초)와 그 offset 까지의 부분 스캔(0~0.4초), 작은 계좌의 0쪽은 끝까지 훑는 페이지 하나(약 0.22초, 내용이 size 보다 적으면 Spring Data 가 건수를 생략), 작은 계좌의 1쪽 이상(끝 너머 빈 쪽)은 페이지 전수 스캔과 건수 전수 스캔 둘(약 0.45초, offset 이 0 이 아닌 빈 쪽은 PageableExecutionUtils 가 건수를 부른다)이다. 평균 약 0.3~0.45초 CPU. Oracle 2 CPU(parallel_max_servers 1, 자원 관리자가 2 CPU 로 가둠)에서 동반 부하의 내역 조회(초당 약 0.75건 × 0.25초)와 평시 사용(0.1~0.5 CPU)을 빼면 백필 몫은 약 1.2~1.7 CPU, 곧 초당 약 3~6 호출이다.
- 국면별 동시 수(1차 평가 FAIL 반영): 워커들이 계좌를 같은 순서로 돌며 쪽을 나누므로 어느 국면에서나 워커 48개가 각자 호출 하나를 들고 있다. ① ACC-1001~1007(목록 1~7번): 계좌마다 약 650~700쪽이 48개 워커에 고르게 나뉜다(워커당 약 14~15쪽). 일곱 계좌 합 약 4,700쪽은 초당 3~6 호출로 약 13~26분이라 min_hold 10m 와 성공 3틱이 모두 이 국면 안에 든다. ② 작은 계좌(8~1000번): 계좌마다 48개 워커가 모두 한 번씩 부른다(하나는 0쪽, 나머지는 빈 쪽). 계좌당 48 호출 × 약 990 계좌라 25분 주입 창이 끝날 때까지 끝나지 않는다. 어느 국면이든 동시 48 > 풀 15 가 유지되고, 처리량이 계산보다 절반으로 떨어져도 국면 ①이 길어질 뿐 동시 수는 줄지 않는다. 1차 설계(계좌를 워커에 나눔)는 작은 계좌 약 850개를 몇 분 만에 끝낸 뒤 큰 계좌를 맡은 워커 8개만 남아 동시 8 < 풀 15 가 되는 잘못이 있었다.
- 풀 계산: 호출 하나가 transfer Hikari 연결 하나를 스캔 동안 쥔다. 연결 15개는 늘 스캔에 묶이고(연결당 보유 약 15 / 4.5 ≈ 3.3초), 나머지 33 이 연결을 기다린다. 대기자는 3초 안에 연결을 못 얻으면 500 으로 돌아가 2초 뒤 다시 줄을 선다. 백필 대기자의 도착률은 약 33/5 + 처리량 ≈ 10~13/초로 처리량(3~6/초)의 두 배 이상이라, 이체 요청이 연결을 얻는 몫도 대략 처리량/도착률(0.25~0.5)에 그친다. 이체 실패 0.5 이상이 계산되고, account 재시도와 서킷 열림(실패율 50% 이상이면 이후 호출을 바로 거절), readiness 이탈 순간의 연결 거부가 더해진다. 피해의 크기가 이체 수와 무관하게 워커 수로 정해져 하루 주기 기준선에 덜 좌우된다.
- 앞선 반려와의 차이: Onfido 후보(rejected 2026-10-09)는 Oracle CPU 만 누르는 외부 세션이라 짧은 OLTP 가 굶지 않았다(로컬 실측). 이 후보는 무거운 호출이 transfer 자기 풀 연결을 쥐는 꼴이라 그 반려가 남긴 "풀린다면: 무거운 세션이 앱이 기다리는 자원(풀 연결)을 함께 붙잡는 꼴"에 해당한다. F42-R 의 피해도 같은 풀에서 났다.
- 진입점: nginx, api 는 그대로 살아 5xx 로 답한다. 백필은 진입점을 지나지 않는다. abort 는 entry_status 0(2틱)뿐이다.
- 동반 부하: load.north_south core-banking transfer-heavy-surge.js 5rps(F57-R 과 같은 값, seed 5916). 이체 표본을 초당 약 2건 더한다. F42-R 의 20rps 보다 낮게 두어 동반 부하 자신의 내역 조회(전수 스캔)가 Oracle 을 누르는 몫을 줄였다.
- 러너 판정: 성공 transfer_5xx_rate ≥ 0.3(3틱), 배제 achieved_rps < 1.25, Oracle 파드 NotReady, transfer 재시작 수 ≥ 2. 회복 target_health 200, transfer Ready, available 1, 기준선 이체 5xx < 0.05(2틱).
- cleanup: Job foreground 삭제 → 파드 종료(terminationGracePeriodSeconds 5, 스크립트가 TERM 에 워커를 모두 끝냄) → 진행 중 스캔이 transfer 쪽에서 끝까지 돈 뒤 연결 반납(수 초) → testbed-transfer available 확인(상한 300초). 백필은 읽기만 해 남는 데이터가 없다. 마지막 페이지와 명세서 줄은 파드의 emptyDir(상한 512Mi, 실제로는 워커당 페이지 하나와 계좌당 한 줄)와 함께 사라진다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Atlassian Bitbucket 2025-05-08, 공식 사후 보고 링크)와 요소별 대응표가 있고, 기전(내부 백필 작업 → API 엔드포인트의 비싼 질의 → 공유 DB 압박 → 사용자 기능 지연과 오류, 재시도가 더함)이 원본과 같다 (원칙 1, §2)
- [x] 분류 장부를 갱신했다: 묶음 E(3→4, 5.1%), 정답 위치 내부 배치 작업(1→2, 2.6%), 결제 경로 12/78(15.4%), 서비스 은행 25→26. 어느 축도 20% 에 닿지 않는다 (원칙 2, §3)
- [x] 근본 원인 위치가 인프라 지점(rca-testbed-banking Job ledger-statement-backfill)과 코드 위치(TransferController.java:51-64, TransferRepository.java:17-28, application.yml:12-17, k8s_job_executor.py:111-143)로 확인됐다 (G1, §4)
- [x] 근본 원인의 흔적이 119 에 남는 것을 조회로 확인했다: KCM 이벤트의 Job, Pod 이벤트 수집(kcm-128 Job 2026-09-17 실례와 banking 파드 이벤트), DPM Top SQL 의 문장별 실행 수와 비용, Oracle 지표 (원칙 3, §7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: 계기는 KCM, 기전은 DPM, 전파는 로그(전수). 스팬은 보조 (원칙 3)
- [x] 계기의 흔적(Job 생성, 파드 시작 이벤트)이 남는다. 인공 지연을 쓰지 않는다: 지연은 실제 질의 비용과 풀 대기에서 난다 (원칙 4)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다(새 Job + 평시 문장의 실행 폭증 + 호출자 파드). 내역 질의의 선택 조건 설계 결함 추론을 요구하지 않는다 (원칙 5, §5)
- [x] 정답지 세 칸: 근본 = Job, 계기 비움, 부분 점수 = transfer, Oracle, ledger, account, api, commerce-payment (원칙 6)
- [x] 감지, 피해 판정, RCA 증거를 나눠 적었다 (원칙 7, §6)
- [x] 주입 도구가 시나리오 id 를 남기지 않는다: Job 이름, 레이블(app 하나), 스크립트, 환경 변수, 상태 파일 어디에도 없다(실행기 테스트가 확인). 작업은 실제 회사 이미지(ledger)와 평범한 curl 로 돈다 (원칙 8)
- [x] 부하 상한과 서킷브레이커를 넣은 피해 계산이 있다(국면별로 동시 48 > 풀 15 유지, 도착률 대 처리량, 연결 대기 3초) (원칙 9, §9)

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

1. KCM 에 Job ledger-statement-backfill SuccessfulCreate, 파드 Scheduled(tb-w2), Pulled(core-banking-ledger:latest), Started(backfill)가 남는가. Pulled 문구가 ledger Deployment 와 같은 이미지라 파드 이름(ledger-statement-backfill-xxxxx)으로 구분되는가.
2. DPM Top SQL 에서 byb2y2hv7p4ag 와 내역 페이지 문장의 실행 수가 몇 배로 뛰는가, 큰 계좌의 1쪽 이상 페이지(offset 꼴)가 새 sql_id 로 잡히는가.
3. transfer Hikari 고갈 로그와 account 'Transfer service call failed', api 502 가 Job 시작 1~2분 안에 나오는가. 성공 3틱까지 걸린 시간과 이체 5xx 비율(계산 0.4 이상).
4. transfer 가 NotReady 로 흔들리는 정도(재시작은 없어야 함).
5. cleanup 뒤 회복 시간(기준선 이체 5xx < 0.05).
6. transfer 스팬에 client.address 가 백필 파드 IP 로, user_agent 가 curl 로, url.query 에 수백 개 계좌와 size=500 이 남는가(표본 수).
7. Oracle 프로브(healthcheck.sh, 시간 제한 10초: readiness 20초 간격 3회, liveness 30초 간격 5회)가 2 CPU 포화 아래에서 지연, 실패하는가, KCM 에 testbed-oracle-0 Unhealthy 가 남는가. 동시 전수 스캔은 transfer 풀 15 세션으로 묶여(F42-R 과 같은 상한) 그 이상 늘지 않지만, 견딘다는 실측 선례는 아직 없다. readiness 가 빠지면 must_rule_out oracle-down 으로 실행이 배제되고, liveness 재시작이면 F25-H 꼴 DB 중단이 섞이므로 그 경우 운영 하네스가 워커 수를 낮춘다.
8. 강도가 모자라면(이체 5xx 가 0.3 아래) 운영 하네스가 워커 수를 올린다. 너무 세서 nginx 나 api 가 죽는 일은 이 주입 경로에서 일어나지 않는다(백필은 transfer 로 바로 간다).
