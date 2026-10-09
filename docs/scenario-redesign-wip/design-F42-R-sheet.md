---
title: F42-R 설계 시트 (banking transfer-service 를 전수 스캔 한도 질의가 든 릴리스로 롤아웃)
status: Draft
owner: project
last_reviewed: 2026-10-09
tags:
  - scenario
  - design
  - release
summary: banking transfer-service 를 결함 있는 새 릴리스(core-banking-transfer:2.2.0, fault-images/f42-r 패치로 만든 별도 태그)로 롤아웃하면, 새로 더한 일일 이체 한도 확인이 이체마다 transfers 테이블 전체(약 630만 행)를 훑어 공유 Oracle(2 CPU)이 포화되고, 이체와 commerce 정산이 500, 502 로 실패하는 시나리오. 원본은 GitHub 2025-01-09 배포가 들여온 질의가 주 데이터베이스 서버를 포화시킨 장애.
---

# F42-R 설계 시트

## 1. 요약

banking transfer-service 의 새 릴리스 2.2.0 은 일일 이체 한도를 더했다. 이체마다 두 계좌를 잠그기 전에 출금 계좌의 오늘 완료 이체 합계를 구하는데, 조건을 `trunc(created_at) = trunc(sysdate)` 로 써서 created_at 인덱스를 못 타고, from_account 히스토그램 때문에 옵티마이저가 transfers 전체(약 630만 행, 9.9만 블록)를 읽는다. 한 번에 CPU 0.24~0.43초다. 한도 값(500억)에는 닿지 않아 거절되는 이체는 없다. 새 버전은 잘 떠서 프로브를 통과하고, 이체가 초당 몇 건만 넘으면 이 질의가 Oracle 이 가진 CPU 2개를 다 쓴다. Oracle 을 함께 쓰는 모든 경로가 느려지고, transfer 의 연결 풀이 스캔에 묶여 이체가 500, 502 로 실패하며, commerce checkout 은 정산 이체가 실패해 실패한다.

비유: 은행 창구에 "오늘 이 고객이 보낸 돈 합계"를 확인하는 절차를 새로 넣었는데, 확인할 때마다 창구 직원이 금고 장부 전체를 처음부터 넘긴다. 장부실(Oracle)에 직원이 몰려 다른 창구 업무까지 밀린다. 고칠 곳은 장부실이 아니라 새 절차를 넣은 창구 지침(릴리스)이다.

## 2. 원본 사례

- **GitHub, 2025-01-09** (공식 월간 가용성 보고): https://github.blog/news-insights/company-news/github-availability-report-january-2025/
- 01:26~01:56 UTC 많은 서비스가 광범위하게 중단되어 사용자가 여러 기능에서 서버 오류를 받음. 원인: "a deployment which introduced a query that saturated a primary database server". 오류율 평균 6%, 갱신 요청의 최대 6.85%. 내부 도구와 대시보드로 문제 질의의 출처를 찾아 배포를 되돌려 완화했고, 대응 시작부터 문제 질의를 찾기까지 14분. 재발 방지로 배포 전에 문제 질의를 잡는 도구에 투자. 질의 내용과 DB 종류는 적혀 있지 않다.
- 자료 문서 M2 에 추가했다(출처가 말한 사실만).

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 배포(새 코드) | transfer-service 를 릴리스 core-banking-transfer:2.2.0 으로 롤아웃(KCM ScalingReplicaSet, 새 파드 Pulled 이벤트에 태그가 남음) |
| 원인이 된 결함 | 배포가 들여온 질의 하나가 주 DB 서버를 포화 | 새 일일 한도 확인 질의가 열에 함수를 씌워 인덱스를 못 타고 이체마다 transfers 전수 스캔(호출당 9.9만 블록, CPU 0.24~0.43초) |
| 전파 경로 | 주 DB 포화 → 그 DB 에 기대는 여러 서비스의 요청 실패 | Oracle 2 CPU 포화(On CPU, resmgr:cpu quantum) → transfer Hikari 가 스캔에 묶여 3초 대기 초과 500, account 재시도와 서킷, api 10초 시간 초과 → banking 502, commerce 정산 실패. 같은 Oracle 을 쓰는 잔액 조회, 거래 내역도 느려짐 |
| 사용자 증상 | 여러 기능에서 서버 오류, 갱신 요청 오류율 평균 6%, 최대 6.85% | 이체(쓰기)의 상당 부분이 502, commerce checkout 실패. 조회는 느려지되 대개 성공 |
| 원본의 탐지 경로 | 내부 도구와 대시보드로 문제 질의의 출처를 찾음 | lucida-next 오류율, 지연 이상, DPM Top SQL 의 새 sql_id 와 Oracle CPU, DPM 세션의 machine(새 transfer 파드), 표본 트레이스 db.statement |
| 완화와 복구 | 배포 되돌림 | cleanup 이 이미지를 매니페스트 이미지로 되돌림(롤백) |

기전 일치: 원인(배포가 들여온 비싼 질의)에서 증상(공유 주 DB 포화, 그 DB 를 쓰는 요청들의 서버 오류)까지 고리가 같다. 원본은 질의 내용을 밝히지 않아, 실무에서 가장 흔한 꼴(인덱스가 있는 열에 함수를 씌워 전수 스캔이 되는 조건)을 한 기능 크기로 둔다.

바꾼 것(기전 고리 밖):
- 규모: 원본은 GitHub 주 DB 서버, 테스트베드는 Oracle Free(2 CPU) 하나다.
- 요청률: 원본은 평소 트래픽에서 포화했다. 테스트베드 banking 이체는 평시 초당 0.3~1.4건이라 이 질의만으로는 0.1~0.6 CPU 다. 동반 부하로 이체를 초당 약 8건으로 올려 포화 영역에 둔다(§9). 같은 부하에 매니페스트 버전이면 Oracle 은 절반 안팎이라 부하만으로는 포화하지 않는다.
- 롤아웃 공백: transfer 는 매니페스트 전략이 maxSurge 0 이라 새 파드가 Ready 가 될 때까지 약 1분 이체가 실패한다. 원본에는 없는 짧은 구간이고, 계기(롤아웃)와 같은 곳을 가리키므로 정답을 흐리지 않는다.

## 3. 숫자 근거 (2026-10-09, `scenario-stats.py`, 정식 + 후보 합계 48, 이 후보 전)

- 묶음: A 7, B 7, D 7(각 14%), C 6, G 6(12%), F 3, L 3, E 2, H 2, **J 2**, I 1, M 1, O 1, K 0, N 0. J(결함 있는 새 버전 배포)는 현실 트리거 1위(Google 바이너리 배포 37%)인데 2개(F17-H 산출물 결함, F41-R 메모리 누수)뿐이다. 이 후보로 J 3(6%). F 로 세더라도 F 4(8%).
- 정답 위치: 외부 결제 의존 6, 주문 서비스 6(각 12%), 결제 경로 합계 10(20%, 금지). **은행 이체 서비스 4**(8%) → 5(10%).
- 서비스: 음식배달 10, 은행 13, 쇼핑몰 25. 은행 13 → 14.
- 부품 지도의 0 부품: commerce kafka, notification, cart, gateway, nginx / banking kafka, nginx / food kafka, order, notify. 인프라 층 전부.
- 왜 이 후보인가: J 가 현실 대비 가장 모자라고(2026-10-09 결함 버전 이미지 규칙으로 처음 만들 수 있게 됨), 원본(GitHub 2025-01-09)이 공식이며 "배포가 들여온 질의" 라는 기전이 결함 이미지 하나로 그대로 재구성된다. 계기 흔적(이미지 태그), 근본 흔적(DPM 의 새 sql_id, 세션 machine, Oracle CPU)이 모두 119 에 남는다(§7).
- 음식배달(10)이 더 적지만 고르지 않은 이유: 같은 기전(요청 경로의 새 비싼 질의)을 걸 food 서비스가 막힌다. order 는 질의가 자기 Hikari(15)를 묶으면 readiness 와 liveness(둘 다 DB 를 보는 /actuator/health, `food-delivery/k8s/20-order-deploy.yaml:78-90`)가 실패해 loadgen 입구가 연결 불가(필수 중단 조건 entry_status 0)가 된다(rejected 의 연결 한도, 풀 축소, FTWRL 행과 같은 벽). restaurant 는 주문 여정의 메뉴 조회 입구라 표본이 사라지고, dispatch 는 배달 서비스 2개와 F33-R(MySQL 포화로 order 503)이 이미 있으며, payment 는 결제 경로 상한, notify 는 DB 를 쓰지 않는 Kafka 소비자다.
- 은행에서 transfer 를 고른 이유: account 에는 같은 날 J(F41-R)가 있고, api 는 DB 를 쓰지 않으며, ledger 는 단일 스레드 Kafka 소비자라 Oracle 세션 하나(최대 1 CPU)만 써서 포화 계산이 서지 않는다(rejected 의 원장 배치 행과 같은 벽).

### 후보 목록 (3단계, 기존 목록이 아니라 기전 × 0 부품, 인프라 층에서 시작)

| # | 후보 | 층, 부품 | 원본 사례 | 결과 |
|---|---|---|---|---|
| 1 | transfer 새 릴리스의 이체마다 도는 전수 스캔 한도 질의로 Oracle 포화 | 앱, banking transfer(J) | GitHub 2025-01-09 공식 | **채택** |
| 2 | food order 새 릴리스가 주문마다 고객 주문 집계 질의를 더해 MySQL 포화 | 앱, food order(J, 0 부품) | GitHub 2025-01-09 공식 | 버림: 컨트롤러 필수 중단 조건. order 자신의 풀이 묶이면 DB 를 보는 readiness, liveness 가 실패해 loadgen 입구가 연결 불가 |
| 3 | commerce gateway 새 릴리스의 HTTP 클라이언트 연결 누수로 상류 풀 고갈 | 앱, commerce gateway(J, 0 부품) | 없음(웹 검색 1회: 블로그, 유료 글뿐) | 버림: 원칙 1 |
| 4 | banking ledger 새 릴리스가 소비 이벤트마다 무거운 원장 질의 | 앱, banking ledger(J) | GitHub 2025-01-09 | 버림: 원칙 9. 단일 스레드 소비자라 Oracle 세션 하나뿐(rejected 원장 배치 행과 같은 벽) |
| 5 | notify, commerce notification 새 릴리스가 소비 중 예외 반복 | 앱, food notify / commerce notification(J, 0 부품) | PostHog 2026-07-23(2차 출처) | 버림: 원칙 1(2차 출처), 원칙 7(사용자 경로 아님) |
| 6 | commerce cart 새 릴리스의 캐시 직렬화 형식이 옛 Redis 항목과 안 맞음 | 앱, commerce cart(J+L, 0 부품) | 찾지 못함 | 버림: 원칙 1, 그리고 rejected 의 cart Redis 행(쓰기 실패 삼킴, DB 폴백)과 같은 벽(원칙 7) |
| 7 | banking transfer 새 릴리스가 계좌 잠금 순서를 바꿔 교착(ORA-00060) | 앱, banking transfer(J+D) | 찾지 못함 | 버림: 원칙 1, 원칙 9(반대 방향 동시 이체가 드물고 정산은 한 방향) |
| 8 | banking Kafka 토픽 보존 시간 축소로 원장 소비자가 이벤트를 건너뜀 | 인프라, banking kafka(0 부품, 큐 컨슈머 지연 층) | 찾지 못함 | 버림: 원칙 1, 원칙 7(사용자 경로 아님, 조용한 누락) |
| 9 | 새 버전의 프로브 경로 변경으로 롤아웃 정지 | 인프라, 프로브와 재시작 정책 | PostHog 2025-10-28 | 버림: rejected(롤아웃 실패류, maxSurge 25% 가 옛 파드를 남김) |

DB 가 정답인 후보는 0개, 부품 7종(transfer, food order, gateway, ledger, notify/notification, cart, banking kafka)과 프로브 층, 앱과 인프라 두 층을 모두 냈다.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 롤아웃: `scripts/scenarios/profiles/k8s_image_executor.py` RELEASE_SCRIPT `run` — 109 docker 의 core-banking-transfer:2.2.0 을 tb-w2 containerd 에 올리고(`docker save | ssh nkia@<tb-w2> sudo ctr -n k8s.io images import -`) `kubectl set image deploy testbed-transfer transfer-service=core-banking-transfer:2.2.0`. CONTRACTS["F42-R"] 이 계약.
2. 배포: `core-banking/k8s/22-transfer-service.yaml:9`(replicas 1), `:17-18`(maxUnavailable 1, maxSurge 0 → 옛 파드 먼저 내림), `:27-28`(nodeSelector tb-w2), `:36-37`(image, imagePullPolicy Never), `:87-94`(readiness /actuator/health, DB 포함, 3초), `:97-103`(liveness /actuator/health/liveness, livenessState 만), `:108-110`(limits cpu 500m, 1Gi).
3. 결함: `scripts/scenarios/fault-images/f42-r/transfer-service.patch:17-21`(TransferRepository.sumCompletedOutgoingToday — `select coalesce(sum(amount), 0) from transfers where from_account = :account and status = 'COMPLETED' and trunc(created_at) = trunc(sysdate)`), `:53-59`(TransferService.execute 가 계좌 잠금 전에 이체마다 호출, 한도 `transfer.limit.daily-amount` 기본 500억). 매니페스트 버전은 `core-banking/transfer-service/src/main/java/com/corebanking/transfer/service/TransferService.java:52-75`(검증 뒤 바로 FOR UPDATE, 합계 질의 없음).
4. 실행 계획(109 Oracle 2026-10-09, 읽기 전용): transfers 6,289,608행, 97,817블록. 인덱스 idx_transfers_from(from_account), idx_transfers_created(created_at) 등. from_account 는 TOP-FREQUENCY 히스토그램(956 값, commerce-settlement 3,957,098행, ACC-100x 각 약 33만 행). 같은 조건을 바인드로 실행하면 commerce-settlement 를 먼저 보든 ACC-1004 를 먼저 보든 TABLE ACCESS FULL(plan_hash 1913588322), 실행당 98,843 블록, CPU 244~612ms(평균 428ms). `trunc(created_at)` 조건에 대해 Oracle SQL Analysis Report 가 "predicates which preclude their use as keys in index range scan" 을 낸다. 같은 합계를 `created_at >= trunc(sysdate)` 로 쓰면 idx_transfers_created 범위 스캔(비용 4)이다.
5. 풀과 시간 초과: transfer Hikari 15, connection-timeout 3000(`core-banking/transfer-service/src/main/resources/application.yml:12-17`), account → transfer 읽기 15초와 재시도, 서킷(`core-banking/account-service/src/main/java/com/corebanking/account/client/TransferClient.java:28-58`, `application.yml:32-40`), api → account 읽기 10초(`core-banking/api-service/src/main/resources/application.yml:11-16`), commerce payment → transfer 읽기 10초.
6. Oracle: `core-banking/k8s/10-oracle.yaml:43-49`(limits cpu 2000m), Oracle Free cpu_count 2(v$parameter). 자원 관리자가 2 CPU 로 가둬 넘치는 세션은 `resmgr:cpu quantum` 을 기다린다(119 DPM 에 평시 30일 199건).

### 로컬 실측 (2026-10-09, 기능 확인)

패치를 얹은 transfer jar 를 104 에서 빌드하고(`mvn -o -pl transfer-service -am package`, 성공) 로컬 Oracle Free(gvenzl/oracle-free 23-slim, init.sql 그대로, transfers 20만 행 시드)에 붙여 띄웠다. 정산 이체(commerce-settlement → commerce-merchant)와 개인 이체가 200 COMPLETED, 한도를 넘는 금액은 400 'daily transfer limit exceeded' 와 INFO 'Transfer rejected (daily limit)'. v$sql 에 앱 문장 `select coalesce(sum(amount), 0) from transfers where from_account = :1 and status = 'COMPLETED' and trunc(created_at) = trunc(sysdate)` 가 보였다(로컬은 통계가 없어 idx_transfers_from 범위 스캔. 운영은 히스토그램이 있어 전수 스캔, 위 4). 질의 비용은 운영 데이터에서만 의미가 있어 4 의 109 실측을 근거로 쓴다.

## 5. 원인 규정 (원칙 5, 6)

- 근본(`root_cause.target_id`): `core-banking-transfer`(transfer-service 의 새 릴리스). 비싼 문장을 들여온 곳이자 되돌려야 재발이 막히는 곳.
- 계기(`trigger_target_id`): 비움. 계기(릴리스 롤아웃)와 결함이 같은 곳이다. 호출자(api, account, commerce payment, loadgen)의 이체 요청은 평시와 같은 정당한 요청이고, Oracle 은 시킨 질의를 실행했을 뿐이다(원칙 6).
- 부분 점수: `banking-oracle`(포화가 드러난 곳), `core-banking-account`, `core-banking-api`, `commerce-payment`(증상이 드러나는 곳).
- 층위: 원본 사후 보고의 결론("배포가 들여온 질의가 주 DB 를 포화시켰고 배포를 되돌려 복구")과 같은 층위. 패치 속 코드 줄이나 SQL 을 고치는 방법을 요구하지 않는다(원칙 5, 결함 버전 이미지 규칙 7). 관제 데이터로 낼 수 있는 결론: "transfer 를 2.2.0 으로 롤아웃한 직후부터 Oracle Top SQL 에 없던 문장이 나타나 실행당 약 10만 블록을 읽으며 Oracle CPU 를 다 쓰고, 그 세션들이 새 transfer 파드에서 온다 → 새 버전의 질의, 롤백".

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 |
|---|---|
| 감지 | core-banking-transfer, account, api 서버 스팬 오류율과 지연, commerce payment, order 오류율(정산 실패), Oracle CPU(dpm.oracle.instance.cpu_time) 이상, transfer Hikari 오류 로그 급증, KCM Unhealthy(readiness) |
| 피해 판정(러너) | 동반 부하 이체(step transfer) 5xx 비율 ≥ 0.3 이 3틱. 평시 0 |
| RCA | 롤아웃 이벤트와 새 이미지 태그, 롤아웃 직후 처음 나타난 sql_id 와 그 실행당 논리 읽기, DPM 세션 machine 이 새 transfer 파드, Oracle CPU 상한, 표본 트레이스 db.statement, 잠금 대기, DDL, 통계 변경 없음 |

## 7. 관측 근거 표 (119 실조회, 2026-10-09 10:20~10:45 UTC)

| 증거 | 119 표와 칸 | 조회 | 평시 결과 |
|---|---|---|---|
| 계기: 롤아웃과 이미지 태그 | CH `kcm_events_local`(reason, object_name, body) | `namespace='rca-testbed-banking' AND object_name LIKE 'testbed-transfer%'` reason 별 집계 | Pulled 64건 'Container image "core-banking-transfer:latest" already present on machine', ScalingReplicaSet 128건, Killing 34건(마지막 2026-10-01 17:31). 태그가 본문에 담긴다 |
| 계기: 새 ReplicaSet 스펙 | PG `kcm_resources_history`(kind, name, yaml, captured_at) | `namespace='rca-testbed-banking' AND name LIKE 'testbed-transfer%'` 종류별 집계와 'core-banking-transfer:latest' 수 | replicaset 175건, pod 155건 전부 :latest(마지막 2026-10-01 17:31). deployment 3건, service 3건은 7월 뒤로 안 쌓임(F31, F39-R 시트와 같음) |
| 근본: 새 문장의 비용 | CH `dpm_topsql_local`(engine, sql_id, body 의 executions, avgLogicalReads, avgElapsedTime, planHashValue) | oracle 최근 1시간 sql_id 별 최대 avgElapsedTime, avgLogicalReads, 실행 합 | 최상위 byb2y2hv7p4ag(거래 내역 건수 질의, 전수 스캔) 실행당 약 10.1만 블록, 231ms, 시간당 3,200회. 그 밖은 10,437 블록 이하. 문장 본문은 없고 sql_id 와 수치만 있어, 새 sql_id 가 롤아웃 뒤 처음 나타나는 것과 실행당 약 9.9만 블록이 증거다. 하루 oracle 20,009행, sql_id 115종 |
| 근본: 누가 돌리나 | CH `dpm_session_local`(body 의 machine, sqlId, event, waitClass, planHashValue) | oracle 30일 비유휴 이벤트 집계, 세션 표본 | machine 이 파드 이름(예 'testbed-transfer-74b498b67b-r5lqw'), sqlId 가 Top SQL 과 같은 열쇠. 비유휴: On CPU 52,904, direct path read 21,069, enq: TX - row lock contention 2,384(마지막 2026-10-02 F01-P 녹화), resmgr:cpu quantum 199 |
| 근본: Oracle CPU | VM `dpm.oracle.instance.cpu_time` | 1시간 평균, 2일 | 0.10~0.46(UTC 04~10시 최고), 하루 최대 1.35. 상한 2 |
| 보조: 새 문장 본문 | CH `otel_traces_local`(service_name, span_kind, span_attributes 의 db.statement, duration_ns) | core-banking-transfer 클라이언트 스팬 1시간 | 'select count(t1_0.id) from transfers ...' 315건 평균 223.5ms 등 문장 본문이 남는다(10% 표본). 새 문장은 이체마다 돌아 표본에도 많이 남는다. 이 증거만으로 증명하지 않는다 |
| 전파: transfer, account, api, commerce | CH `lucida_logs_local` | 서비스별 ERROR | Hikari 'Connection is not available', account 'Transfer service call failed', 'circuit open' 는 F01-P 녹화(2026-10-02)에 남은 꼴 그대로 수집된다(로그 전수) |
| 이체 속도 | CH `lucida_logs_local` | core-banking-transfer 'Transfer COMPLETED' 시간별 | 시간당 1,114~5,038건(초당 0.3~1.4), 그중 commerce-settlement 출금 약 55~60% |

표본 데이터(트레이스 10%, APM 스팬 지표)만으로 증명하지 않는다. 결정 증거는 KCM 이벤트(전수), DPM Top SQL 과 세션(수집기 주기 전수), DPM CPU 지표, 로그(전수)다.

## 8. 감별

- must_support: 롤아웃과 2.2.0 태그, 롤아웃 뒤 처음 나타난 sql_id(실행당 약 9.9만 블록, 실행 수가 이체를 따라감), DPM 세션 machine=새 transfer 파드와 On CPU·resmgr:cpu quantum, Oracle CPU 상한, transfer Hikari 오류와 account·commerce 정산 실패, transfer 재시작 없음과 Oracle Ready.
- must_rule_out: 행 잠금(F01-P, F15-G, F08-G: enq: TX 아님, blockingSession 없음), 계정·자격 증명(F35-R), 이미지 부재·readiness 오설정(F17-H, F17-R), 인덱스 제거·통계 변경(F33-R, F33-P: 기존 문장 planHashValue 그대로, DDL 없음), 단순 부하 급증(같은 부하에 매니페스트 버전이면 0.7~1.1 CPU), Oracle 자체 장애.
- contrast_with: F20-R, F33-R, F01-P, F17-H, F41-R.

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

- 강도는 릴리스 하나로 고정(approved-fixed-f42-r, 실행기 파라미터 = 이미지 태그)과 동반 부하 20rps.
- **부하 계산**(Oracle CPU 초/초):
  - 새 질의 1회: 0.24~0.43초(109 실측, 위 §4-4). 계획은 바인드와 무관하게 전수 스캔.
  - 동반 부하 transfer-heavy-surge.js 20rps: 이체 40% = 8건/초, 거래 내역 15% = 3건/초(건수 질의 전수 스캔 약 0.2초), 잔액 35%, 계좌 목록 10%(PK, 작은 표).
  - 기준선과 commerce 정산 이체: 초당 0.3~1.4건, 평시 Oracle 0.10~0.46.
  - 릴리스 없음: 0.6(거래 내역) + 0.10~0.46 = **약 0.7~1.1 CPU**(상한 2 의 35~55%).
  - 릴리스 있음: 새 질의 (8 + 0.3~1.4) × 0.24~0.43 = 2.0~4.0, 합 **2.7~5.0 CPU**(상한의 135~250%).
  - 포화 뒤: Oracle 이 감당하는 이체는 초당 2 / 0.24~0.43 ≈ 4.7~8.3건 이하(다른 일을 빼면 더 적다). transfer Hikari 15 가 동시에 스캔을 돌리면 한 번에 15 × 0.35 / 2 ≈ 2.6초씩 걸려 풀이 늘 차고, 넘친 요청은 3초 뒤 'Connection is not available' 500, account 재시도가 질의를 더 보내고 실패율 50% 를 넘으면 서킷이 5초씩 열린다. api 는 10초에 포기한다. 동반 부하 이체 5xx 비율은 0.3 이상으로 본다.
- success: transfer_5xx_rate ≥ 0.3, 3틱. 롤아웃 공백(약 1분)만으로 판정되지 않도록 min_hold 10m.
- must_rule_out: achieved_rps < 5(20 의 1/4), Oracle 파드 NotReady, transfer 재시작 ≥ 2(2틱). 잔액 조회 실패는 배제에 쓰지 않는다(같은 Oracle 을 쓰는 다른 경로가 느려지는 것은 원본의 '여러 기능 서버 오류'와 같은 장애의 일부).
- abort: entry_status == 0(2틱). nginx, api 는 살아 502 로 답한다.
- recovery: target_health 200, transfer Ready, available 1, 기준선 이체 5xx < 0.05(2틱).
- cleanup: 이미지 원복, available(180초), 2.2.0 을 쓰는 파드가 없어진 뒤 tb-w2 containerd 에서 2.2.0 의 이름 참조와 ID 참조를 지우고 둘 다 없는지 확인(F41-R 과 같은 release 모드 스크립트).
- 시간: 동반 부하 ramp 2m + hold 20m + ramp_down 15s, min_hold 10m, timeout 18m, max_injection_duration 25m.
- 노드 디스크: tb-w2 / 74%(2026-10-09). 릴리스는 기본 이미지와 같은 eclipse-temurin 기반이라 새로 쓰는 것은 앱 층뿐이다. preflight 가 80% 미만을 요구한다.
- 태그: core-banking-transfer:2.2.0. 저장소 grep 으로 다른 시나리오가 쓰는 transfer 태그는 :latest, 2.1.0(F17-H, 노드에 없어야 함), pre-outbox-purge-20260807(문서)뿐이라 겹치지 않는다. 109 docker 에도 2.2.0 은 없다.
- Oracle 에 남는 것: 새 문장의 커서가 공유 풀에 남는다(기존 문장과 별개, 시간이 지나면 밀려남). 데이터, 인덱스, 통계는 바뀌지 않는다. 한도 확인은 거절하지 않으므로 업무 데이터도 평시와 같다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(GitHub 2025-01-09, 공식 링크)와 요소별 대응표, 기전 같음 (§2)
- [x] 장부 갱신(§4-1, §6), 묶음 J 3/49(6%), 정답 위치 은행 이체 서비스 5/49(10%), 결제 경로 10/49(20%, 이 후보와 무관), 은행 14. 어느 축도 이 후보로 20% 에 닿지 않는다 (§3)
- [x] 근본 원인 위치: 패치 `transfer-service.patch:17-21`, `:53-59`, 매니페스트 `22-transfer-service.yaml` 각 줄, Oracle 실행 계획 (§4)
- [x] 근본 원인의 흔적 조회: DPM Top SQL(실행당 블록, sql_id), DPM 세션(machine, event), VM dpm.oracle.instance.cpu_time, KCM Pulled 본문의 이미지 태그, PG kcm_resources_history ReplicaSet image (§7)
- [x] 핵심 증거가 표본 데이터만이 아님: KCM 이벤트, DPM, 로그 (§7)
- [x] 계기 흔적: 롤아웃 이벤트와 새 태그(인공 지연 없음, 비용은 실제 질의의 실행 계획에서 나옴) (§7)
- [x] 정답이 관제 데이터로 낼 수 있는 결론: "새 버전 롤아웃 직후 처음 나타난 비싼 문장이 Oracle 을 포화, 그 세션은 새 transfer 파드 → 새 버전의 질의, 롤백". 코드 줄 추론 불필요 (§5)
- [x] 정답지 세 칸(근본 core-banking-transfer, 계기 비움, 부분 banking-oracle, account, api, commerce-payment) (§5). class A(코드 결함), fault_pattern P8(슬로우 쿼리가 공유 DB CPU 를 오염, F20-R 계열)
- [x] 감지, 피해 판정, RCA 증거 구분 (§6)
- [x] 시나리오 id 흔적 없음: 패치 문자열(메서드 sumCompletedOutgoingToday, 설정 키 transfer.limit.daily-amount, 로그 'Transfer rejected (daily limit)', 예외 'daily transfer limit exceeded')에 id, fault, bug, chaos 없음, 태그 2.2.0, 실행기 인자와 상태 파일에 id 없음(테스트로 확인)
- [x] 피해 계산: 109 실측 질의 비용과 이체 속도로 릴리스 유무 Oracle CPU 0.7~1.1 대 2.7~5.0 (§9)

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- 배포 단계에서 `bash scripts/scenarios/fault-images/build.sh f42-r` 로 109 docker 에 core-banking-transfer:2.2.0 이 있어야 preflight 가 통과한다.
- 운영 Oracle 에서 앱 문장(`... from_account = :1 ...`)이 첫 하드 파스에서 전수 스캔을 고르는지(DPM Top SQL avgLogicalReads 약 9.9만). 바인드를 본 첫 값에 따라 idx_transfers_from 범위 스캔이 되면 ACC 계좌는 약 3.7만 블록, commerce-settlement 는 수십만 블록으로 평균이 달라진다. 어느 쪽이든 비용은 수백 ms 다.
- Oracle CPU 가 실제로 상한에 붙는지, 동반 부하 이체 5xx 비율이 0.3 을 넘는지. 넘지 못하면 운영 하네스가 동반 부하를 올린다(25~30rps 까지는 릴리스 없이 거래 내역 질의만으로 약 0.9~1.4 CPU).
- DPM 세션의 machine 이 새 ReplicaSet 해시의 파드 이름으로 남는지, resmgr:cpu quantum 이 잡히는지.
- 인시던트가 생기는지(원칙 7). banking 과 commerce 양쪽에 생길 수 있다.
- cleanup 뒤 tb-w2 containerd 에 2.2.0 의 이름 참조와 ID 참조가 남지 않았는지(recovery 가 확인).
