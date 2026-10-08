---
title: F35-R 설계 시트 (banking Oracle 애플리케이션 계정 잠금으로 풀 세션이 수명을 다하며 재접속 실패)
status: Draft
owner: project
last_reviewed: 2026-10-08
tags:
  - scenario
  - design
  - credential
  - oracle
summary: 자격 증명 회전 단계가 banking Oracle 의 애플리케이션 계정 BANKING 을 잠갔는데 account, transfer, ledger 가 그 계정으로 계속 접속해, Hikari 풀의 세션이 10분 수명을 다하며 다시 접속하지 못하고(ORA-28000) readiness 에서 빠져 banking 조회, 이체, commerce 정산이 502 로 실패하는 시나리오. 원본은 Harness 2026-01-08 비밀 회전 중 DB 사용자 비활성화 장애.
---

# F35-R 설계 시트

## 1. 요약

banking 의 account, transfer, ledger 는 Oracle PDB FREEPDB1 에 애플리케이션 계정 BANKING 하나로 접속한다. 자격 증명 회전 단계가 이 계정을 `ALTER USER BANKING ACCOUNT LOCK` 으로 비활성화한다. 서비스 쪽은 아무것도 바뀌지 않는다(롤아웃, 새 Secret, 재시작 없음). Oracle 은 이미 열린 세션은 두고 새 로그인만 ORA-28000 으로 거절하므로 바로는 멀쩡하다. 각 Hikari 풀은 평시 유휴 세션(account 3, transfer 5, ledger 3)을 max-lifetime 10분까지만 쓰고 새로 만들지 못해, 잠금 뒤 10분 안에 풀이 빈다. 그때부터 요청은 연결을 3초 기다리다 실패하고, DB 를 보는 readiness 가 세 파드를 Service 에서 뺀다. 잔액 조회와 계좌 목록(nginx → account)은 502, 이체와 거래 내역(nginx → api)은 502, commerce checkout 의 banking 정산 이체도 502 다. banking 동반 부하 10rps 를 붙여 core-banking-api 골든 시그널(오류율)이 실패를 드러낼 만큼 표본 스팬을 확보하고, 전면 실패 구간을 최악의 경우에도 약 10.5분 이상으로 잡는다(§6, §9).

비유: 건물 관리인이 출입증을 새로 바꾸면서 옛 출입증을 먼저 막았는데 직원들에게 새 출입증을 안 나눠 줬다. 이미 안에 있던 사람은 일하다가, 퇴근하고 다시 들어오려는 순간 하나씩 문 앞에서 막힌다.

## 2. 원본 사례

- 기업: Harness
- 날짜: 2026-01-08 10:25~10:47 UTC
- 링크: [공식 사후 보고](https://status.harness.io/incidents/5650h9byz6l5)
- 요약(출처가 말한 것만): 예정된 비밀 회전 중 DB 사용자 자격 증명 하나가 회전 과정에서 잘못되어("one database user credential errored out during rotation") Template Service 가 DB 인증을 잃었다. 고객은 UI 에서 템플릿을 불러오지 못했고 실행 중인 파이프라인은 영향이 없었다. 옛 DB 사용자를 다시 활성화해("Re-enabled the old database user") 인증과 기능을 복구했고, 탐지 뒤 2분 안에 완전 복구했다. 재발 방지는 옛 사용자를 비활성화하기 전에 새 사용자가 GCP Secret Manager 에서 활성인지 확인하는 단계와 회전 절차 보강이다.
- 현실 비중: Ghosh 외 SoCC 2022(Microsoft Teams 고심각도 152건)에서 배포 오류 20% 가운데 55%가 인증서 만료나 회전 실수, 전체의 약 11%(`ref-real-world-incidents.md` §1, M7).

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 예정된 비밀 회전 | 회전 단계가 Oracle 애플리케이션 계정 BANKING 을 비활성화(`ALTER USER BANKING ACCOUNT LOCK`) |
| 원인이 된 결함 | 새 사용자가 활성인지 확인하지 않고 쓰던 옛 사용자를 비활성화(재발 방지 항목이 그 확인 단계) | 서비스가 여전히 BANKING 으로 접속하는데(Secret `oracle-secret` 의 APP_USER) 그 계정을 잠금 |
| 전파 경로 | Template Service 가 DB 인증을 잃음 | 새 로그인 ORA-28000, 기존 세션만 남음 → Hikari 풀이 max-lifetime 10분 안에 비고 채워지지 않음 → 요청 실패, health DOWN → 세 파드 NotReady |
| 사용자 증상 | UI 에서 템플릿을 불러오지 못함, 다른 기능(파이프라인)은 정상 | banking 잔액 조회, 계좌 목록, 이체, 거래 내역 502, commerce checkout 502. commerce 의 조회와 장바구니 등 banking 을 거치지 않는 기능은 정상 |
| 원본의 탐지 경로 | 사후 보고에 적혀 있지 않음 | 로그 ORA-28000(전수), DPM 세션 감소, KCM readiness 실패, 오류율 |
| 완화와 복구 | 옛 DB 사용자를 다시 활성화 | `ALTER USER BANKING ACCOUNT UNLOCK`, 풀이 스스로 다시 차고 readiness 복귀 |

기전은 원본과 같다: "회전 중 쓰고 있던 DB 사용자가 비활성화되어 그 사용자를 쓰던 서비스가 DB 인증을 잃는다". 바꾼 것은 규모다. 원본은 서비스 하나였고, 여기서는 세 서비스가 계정 하나를 함께 써서 폭발 반경이 넓다. 풀 세션이 수명을 다할 때까지 증상이 늦게 드러나는 것은 원본이 말하지 않은 부분이고, 우리 스택(Hikari max-lifetime 600000ms)의 성질이다.

## 3. 숫자 근거 (2026-10-08, `scenario-stats.py`, 정식 + 후보 합계 38, 이 후보 전)

| 축 | 값 |
|---|---|
| 묶음 | A 7, B 7, C 6, D 7(각 18%), E 2, F 2, G 3, H 2, I 1, L 1, J, K, M, N 0. 이 후보는 새 묶음 **O 자격 증명 만료, 회전 실수**(0) |
| 정답 위치 | 외부 결제 의존 6, 주문 서비스 6, 결제 경로 합계 10(26%, 금지). 이 후보는 새 위치 **DB 계정**(0) |
| 서비스 | 쇼핑몰 23, 은행 8, 음식배달 7 |

이 후보를 고른 이유:

- 현실에서 흔한데(Teams 고심각도의 약 11%) 우리에게 0인 기전이다. I(인증 의존 실패)는 인증을 맡은 서비스가 고장 나는 묶음이고, 여기서는 인증 주체(Oracle)는 멀쩡하고 자격 증명 쪽이 바뀐다. DB 는 살아 있고 CPU, 메모리, 잠금도 정상이라 A, B, D 가 아니다. 그래서 §2 에 O 를 새로 둔다.
- 정답 위치가 새 위치(DB 계정)라 어느 위치에도 몰리지 않고, 결제 쪽이 정답이 아니다.
- 서비스는 음식배달(7)이 은행(8)보다 1 적다. 같은 기전을 음식배달 MySQL 계정(fooddelivery)에 먼저 검토했으나 버렸다: 음식배달은 게이트웨이 없이 order, restaurant 가 NodePort 로 바로 노출되고, 네 서비스가 한 계정과 DB 를 보는 liveness(`/actuator/health`)를 함께 써서 풀이 비면 파드가 재시작 루프에 들어가고 입구가 연결 불가(상태 0)가 된다. compile-plan 이 모든 컨트롤러에 요구하는 중단 조건 `entry_status == 0` 이 시나리오 자체의 증상으로 발화해 실행이 중단된다. 은행은 진입점 nginx 와 api 가 DB 를 쓰지 않아 502 로 답하고, liveness 가 livenessState 만 봐서 재시작이 없다. 쇼핑몰 23 과의 차이 규칙(원칙 2)에는 둘 다 걸리지 않는다.
- rejected 기록과 장부 §4-1, §5 에 같은 원본 사례나 같은 주입(계정 잠금, 자격 증명 변경)이 없다. 최근 F34-R 폐기 교훈("4xx 업무 거절 단일 신호")과 달리 이 후보의 피해는 연결 거부와 5xx, 여러 서비스, readiness 실패다.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 계정: `core-banking/k8s/01-secrets.yaml:9` (APP_USER banking), `core-banking/k8s/21-account-service.yaml:39-48`, `22-transfer-service.yaml:48-57`, `23-ledger-service.yaml:39-48` (DB_USER 와 DB_PASS 를 Secret 에서 받음). 세 서비스가 같은 계정이다.
2. 풀: `core-banking/{account,transfer,ledger}-service/src/main/resources/application.yml:7-17` (username, hikari: minimum-idle 3/5/3, connection-timeout 3000, idle-timeout 60000, max-lifetime 600000). 평시 VM `db.client.connections.usage{state="idle"}` 3, 5, 3, used 0.
3. Hikari 동작(HikariCP 5.1, Spring Boot 3.4.5): 세션은 max-lifetime(최대 2.5% 앞당김)에 은퇴하고, 최소 유휴 수를 채우려 새 연결을 만드는데 실패(ORA-28000)는 DEBUG 로만 남는다. 풀이 0 이 되면 요청은 connection-timeout 3초 뒤 `HikariPool-1 - Connection is not available, request timed out after 3000ms (total=0, active=0, idle=0, ...)` 를 받고, 이 예외는 마지막 연결 실패(ORA-28000)를 원인과 다음 예외로 단다. Hibernate SqlExceptionHelper 가 두 메시지를 모두 ERROR 로 남긴다(평시 transfer 와 dispatch 의 같은 템플릿이 119 로그에 있음, §7). 라이브러리 코드로 확인했다(로컬 ~/.m2 의 HikariCP-5.1.0.jar, hibernate-core-6.6.13.Final.jar 를 javap 로 역어셈블): `HikariPool.createTimeoutException` 은 `getLastConnectionFailure()` 가 SQLException 이면 그것을 원인으로 넣고 `setNextException` 으로도 단다. `SqlExceptionHelper.logExceptions` 는 `getNextException()` 을 따라가며 예외마다 WARN `SQL Error: <code>, SQLState: <state>` 와 ERROR `<message>` 를 남긴다. 반대로 `PoolBase.newConnection`, `HikariPool.createPoolEntry` 의 연결 생성 실패 로그는 debug 라, 풀이 비기 전의 거절된 재접속 시도는 로그에 남지 않는다. 그 밖에 DataSourceHealthIndicator 의 WARN 'DataSource health check failed' 와 서블릿 SEVERE 의 `exception.stacktrace` 에 'Caused by: … ORA-28000' 이 붙는다.
4. 프로브: `core-banking/k8s/21-account-service.yaml:73-87`, `22-transfer-service.yaml:87-103`, `23-ledger-service.yaml:73-87`. readiness `/actuator/health`(DB 포함, period 10s, timeout 3s, 3회), liveness `/actuator/health/liveness`(livenessState 만). 설계 주석 `account-service/src/main/resources/application.yml:65-70`.
5. 경로: 잔액 조회와 계좌 목록 `core-banking/k8s/02-configmaps.yaml:45-46` (nginx → account) → `AccountService.java:35-36` (getAccount, findById). 이체 nginx → api → `AccountClient.java:28-58` (requestTransfer, 실패와 서킷 열림이 502) → account → transfer. commerce `BankingTransferClient.java:37-58` (transfer 실패 502).
6. 인프라: StatefulSet testbed-oracle(gvenzl/oracle-free:23-slim, FREEPDB1, tb-w2). BANKING 은 DEFAULT 프로필, PASSWORD_LIFE_TIME, FAILED_LOGIN_ATTEMPTS 모두 UNLIMITED, ACCOUNT_STATUS OPEN(2026-10-08 109 sysdba 읽기 조회). DPM 은 LUCIDA_MON, 러너 조회와 실행기는 sysdba 라 잠금 영향이 없다.

로컬 실측(2026-10-08, 같은 이미지 gvenzl/oracle-free:23-slim): 잠금 전 열린 세션은 잠금 뒤에도 질의를 처리했고(`existing-session-ok`), 새 로그인은 `ORA-28000: The account is locked; login denied.` 로 거절됐으며, `ACCOUNT UNLOCK` 직후 로그인이 됐다.

## 5. 원인 규정 (원칙 5, 6)

| 칸 | 값 | 근거 |
|---|---|---|
| 근본(`target_id`) | `banking-oracle:BANKING` | 결함을 가진 곳은 잠긴 계정이다. 고쳐야 재발이 막히는 곳도 계정 상태(와 회전 절차). Oracle 에서 계정은 곧 스키마라 F01-P 의 `banking-oracle:BANKING.accounts` 와 같은 표기 계열이다 |
| 계기(`trigger_target_id`) | 비움 | 계기(회전 단계의 잠금)가 근본과 같은 곳에서 일어났다 |
| 부분 점수 | banking-oracle, banking-account, banking-transfer, banking-ledger, banking-api, commerce-payment | 인스턴스만 지목한 답은 덜 정밀하다. 나머지는 증상 서비스다 |
| 입도 | database-relation | 인스턴스보다 한 층 아래(계정, 스키마)를 지목해야 만점 |

- 원칙 6: account, transfer, ledger 의 로그인 요청은 정당하고(코드, 설정, 요청량 그대로), 그 요청을 거절하게 만든 잠금 명령이 원인이다. 증상을 낸 서비스(account 등)는 부분 점수.
- 원칙 5: 정답은 로그 문구 `ORA-28000: The account is locked` 와 DPM 세션 감소로 바로 낼 수 있다. 코드 설계 결함 추론이 필요 없다. 세 서비스가 계정 하나를 공유한 설계는 피해 범위 설명(user_impact)일 뿐 정답이 아니다. 회전 절차의 어느 단계가 왜 틀렸는지는 요구하지 않는다(원본 사후 보고의 층위: "회전 중 DB 사용자 자격 증명이 잘못되어 DB 인증을 잃음").
- class B(운영 사건, 코드 결함 아님), fault_pattern 없음.

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 | 이 시나리오 |
|---|---|---|
| 감지 | lucida-next 이상 탐지 | banking 네 서비스와 commerce-payment, commerce-order 의 ERROR 로그 급증(연결 거부, 5xx, ORA-28000), api 오류율, KCM readiness 실패 이벤트 3종, DPM Oracle 세션 지표 변화. 업무 규칙 없이 겉 증상으로 이벤트가 생긴다 |
| 피해 판정 | 러너 | 상주 기준선 문서의 잔액 조회(step get) 실패율, 평시 0 에서 0.5 이상 |
| 원인 설명 | 녹화 데이터 | §7 의 로그(전수), DPM 지표, KCM 이벤트 |

인시던트가 생길 조건과 근거(119 PG `event_clusters`, `incident_judge_decisions`, `incidents`, VM APM 지표 조회):

- 판정의 골든 시그널 입력은 VM `apm.agent.otel.java.error_rate` 가 아니라 CH MV `agg_service_golden_signals`(표본 SERVER, CONSUMER 스팬의 status_code='ERROR')를 **벽시계 기준 최근 15분 대 그 전 3시간**으로 비교한 값이다(lucida-next `service_signals.go:20-21`, `incidents_judge.go:1774-1800`, `judge_repo.go:30`, 2회차 평가 확인). 동반 부하 없이 같은 꼴이 난 가장 가까운 평시 사례 2026-10-01 20:13(transfer Hikari 'Connection is not available', 'DataSource health check failed', CircuitBreaker OPEN, 'Connection refused', transfer readiness 실패 20:13:36~20:16:46)은 묶음이 전부 drop 됐고 사유는 거의 다 "골든 시그널 오류율 0.0%, 영향 없음"이었다. 실제 VM `apm.agent.otel.java.error_rate{service_name="core-banking-api"}` 는 20:13, 20:14 에 100, 20:15 에 50, 20:16 에 33 이었다가 20:17 부터 0 이었다. 그런데 이 묶음들의 drop 판정(`incident_judge_decisions.updated_at`)은 2026-10-02 05:25~05:34, 이벤트 뒤 약 9시간이었다(10-01 LLM 판정 지연 p10 544분, 중앙 637분). 판정 시점의 최근 15분 창에는 장애 스팬이 이미 없었으니 오류율 0.0% 는 판정 적체 때문이다. 10-03 이후 promote 지연은 중앙 3~6분이다(2회차 평가 조회). 평시 api 표본 스팬이 분당 1~4개(`apm.agent.otel.java.rps` 0.017~0.067/s)로 적다는 점도 남는다. 동반 부하 없는 banking 실행 F14-P(10-02 01:00)도 suppress, drop 뿐이었다.
- 앞 회차에 근거로 든 2026-10-01 17:20~17:31 창은 F17-R 실행이었다(17:20:04 testbed-transfer 롤아웃, readiness 404 16회). 그 실행에는 commerce 동반 부하(`load.north_south`, commerce surge.js, target_rps 35)가 붙어 있었고, commerce-order 쪽 promote(오류율 40.2% 에서 91.1%, 44.3% 에서 88.5%)는 그 부하가 만든 commerce-order ERROR(분당 약 1,000건)에서 나왔다. 동반 부하 없는 설계에는 이 경로가 없으므로 근거에서 뺀다. 동반 부하 없이 banking 이 promote 된 기록은 7일 로그 보존 범위에서 찾지 못했다(banking 동반 부하가 붙은 실행 창도 없다: account 'Account validation passed' 5분 창 최대 141건).
- 그래서 설계로 조건을 확보한다. ① banking 동반 부하 10rps(core-banking surge.js)를 붙인다. api 로 가는 몫은 거래 내역 25% 와 이체 10% 로 초당 약 3.5건이라, 10% 표본에서 core-banking-api 서버 스팬이 분당 약 21개다(평시 7일 평균 `apm.agent.otel.java.rps` 0.079/s, 분당 약 5개). ② 전면 실패를 F17-R 실행(약 10분) 수준 이상으로 잡는다. 풀이 모두 비는 최악 시각이 잠금 뒤 약 10.5분이고 min_hold 21m 이라, 최악에도 약 10.5분, 보통은 약 13분 동안 전면 실패가 이어진다.
- 어느 스팬이 ERROR 가 되는가: core-banking-api 의 HTTP 서버 스팬(POST /api/transfers, GET /api/transfers)이 502 로 끝난다(`AccountClient.requestTransfer`, `TransferClient.listTransfers` 와 두 fallback 이 BAD_GATEWAY). OTel HTTP 서버 스팬은 5xx 면 상태가 ERROR 다. 평시 api 의 업무 거절은 400(잔액 부족)이라 오류율에 들어가지 않고(7일 중앙, 99백분위 0), 평시 20:13 사례에서도 502 가 난 분에 값이 100 이었다. account, transfer, ledger 는 NotReady 가 되면 요청을 받지 못해 서버 스팬이 사라지므로(그 전에는 500 스팬), 골든 시그널은 api 와 commerce-payment(정산 이체 클라이언트 스팬과 결제 서버 스팬 502), commerce-order(결제 실패) 쪽에서 오른다.
- 한계: 판정은 묶음 멤버 빈도 상위 3개 서비스의 골든 시그널만 입력에 넣는다(`service_signals.go:28-46` topMemberServices). 묶음 멤버가 account, transfer, ledger 로그로 채워지면 core-banking-api 의 수치는 판정 입력에 들어가지 않을 수 있고, NotReady 뒤 account, transfer 의 서버 스팬은 사라져(요청을 받지 못함) 그 오류율은 낮게 나올 수 있다. 그래서 api 오류율 상승은 인시던트 생성을 돕는 조건이지 보장이 아니다. 같은 구간에 5xx 와 연결 거부 로그가 여러 서비스에서 10분 넘게 이어지고, readiness 실패 이벤트 3종과 DPM Oracle 세션 감소가 함께 난다.
- 판정이 drop 한 평시 꼴은 4xx 업무 거절 단일 로그(F34-R 폐기 사유)와, 위 20:13 처럼 판정 적체로 창이 빈 경우였다.

## 7. 관측 근거 표 (119 실조회, 2026-10-08 14:30~15:10 UTC)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 결정 증거는 전수 로그와 DPM, KCM 이다. APM 오류율은 인시던트 생성(감지) 근거로만 쓴다.

**잠금 시각 자체는 119 에 남지 않는다.** VM `dpm.oracle.*` 지표(149개)에 로그인 실패, logon, 계정 상태 항목이 없고, Hikari 의 연결 생성 실패 로그는 debug 다. 잠금 명령(DDL 성격의 ALTER USER)은 DPM Top SQL 에도 잡히지 않는다(F33-R 의 ALTER TABLE 도 없었다). 그래서 첫 흔적은 잠금 뒤 1분 안팎부터 보이는 `dpm.oracle.session.idle_session` 의 계단식 감소(풀 세션이 수명을 다하고 채워지지 않음)이고, ORA-28000 문구는 각 풀이 빈 뒤에야 로그에 나온다. 녹화 구간은 러너가 기록한 주입 시각 t1(잠금 시각) 앞 정상 2시간과 capture pre_window 10m 을 포함하므로 잠금 시각과 그 전 평시는 녹화본에 들어 있다. 정답지는 잠금 시각을 데이터로 보여 달라고 요구하지 않는다.

| 증거 | 119 표와 칸 | 조회 | 평시 결과 |
|---|---|---|---|
| 근본: 계정 잠금 문구 | CH `lucida_logs_local` body, `log_attributes['exception.stacktrace']` | `countIf(body ILIKE '%ORA-28000%' OR log_attributes['exception.stacktrace'] ILIKE '%ORA-28000%')`, `ILIKE '%account is locked%'`, service_name LIKE 'core-banking%', 7일(CH 로그 보존 한계, 최소 timestamp 2026-10-01 12:00) | 0, 0 (ORA-01017 도 0) |
| 근본 전달 경로(템플릿 존재) | 같은 표 | Hikari 'Connection is not available, request timed out after 3000ms' 가 core-banking-transfer 에서 2026-10-01 20:13~10-02 08:08 에 188건 등, 다음 줄에 WARN 'SQL Error: 0, SQLState: null' 과 SEVERE 예외 속성이 남음 | 템플릿이 전수 수집된다. 이번에는 원인 예외가 있어 'SQL Error: 28000' 과 ORA-28000 ERROR 가 이어진다(Hibernate SqlExceptionHelper 가 다음 예외를 순회) |
| 계기, DB 쪽: 세션이 채워지지 않음 | VM `dpm.oracle.session.idle_session{target_id=00149c2f-…}`(Oracle-corebanking) | `quantile_over_time(…, [8d])`, `min_over_time` | 중앙 11(= 3+5+3 풀), 1백분위 10, 최소 6(2026-10-01 17:32 transfer 교체 때 11 에서 6 으로 내려간 1분), 최대 21 |
| 같은 | VM `dpm.oracle.session.current_session` | `min_over_time(…[8d])` | 최저 86, 현재 90. 앱 세션 11개가 빠지면 8일 최저 아래 |
| DB 는 살아 있음 | CH `dpm_session_local` | schema 별 세션 수, 1시간 | BACKGROUND 89 세션, LUCIDA_MON(collector-dpm) 59행, BANKING(testbed-transfer, testbed-ledger 머신) 18행 |
| 전파: readiness | CH `kcm_events_local` | namespace='rca-testbed-banking', 2026-08-24 03:46(이 표의 최소 시각, 보존 약 45일) 이후, object_name, reason, body | testbed-account 의 Unhealthy 0건. transfer 는 다른 시나리오 실행 창에만(404 16회, timeout 78회) |
| 전파: api | CH 로그 | api ERROR 'Account service call failed' 중 400, 409 가 아닌 것, 5분 창 | 2,051개 창 중 10개(다른 시나리오 실행). 평시 api ERROR 는 거의 'Insufficient balance' 400(최근 1일 1,386건 전부) |
| 전파: commerce | CH 로그 | commerce-payment 'core-banking transfer call failed', 5분 창 | 7일 3개 창, 최대 1 |
| 골든 시그널(참고, 판정 입력은 CH MV `agg_service_golden_signals`) | VM `apm.agent.otel.java.error_rate`, `apm.agent.otel.java.rps` {service_name="core-banking-api"} | `quantile_over_time(…[7d])`, `avg_over_time`, 2026-10-01 20:05~20:25 범위 조회 | 오류율 7일 중앙 0, 99백분위 0. 표본 rps 평균 0.079/s. 20:13 사례에서 502 가 난 20:13, 20:14 에 100 |
| 부하 배제 | 녹화본 case-f18-p-v3-d2c566ad `lucida_logs_local.parquet` | 2026-08-22 07:15~07:24(F18-P, banking surge.js 30rps 동반 부하) banking WARN, ERROR 집계 | 'Submitting transfer' 1,590건, WARN, ERROR 는 400 잔액 부족 거절 50건의 로그 100줄뿐, Hikari 시간 초과 0 |
| 피해 | tb-runner 기준선 문서 `/tmp/rca-baseline-core-banking-live.json` | 직접 읽기 | read_nonok_rate 0.0, read_count 34(KST 23시대 2 iter/s, 30초 창), checkout_count 4, entry_status 200 |
| 피해(보조) | CH 로그 5분 창 | banking 네 서비스 ERROR, SEVERE 수 | 7일 중앙 6, 99백분위 57 |
| 대조: Hikari 풀 크기 | VM `db.client.connections.usage`, `db.client.connections.max` | 서비스별 | idle 3/5/3, used 0, max 10/15/10 |

## 8. 감별

- must_support: 정답지 `must_support` 6항목(ORA-28000 로그, DPM idle_session 계단식 0, 세 파드 readiness 실패와 재시작, 롤아웃 없음, api 연결 거부와 commerce 502, core-banking-api 오류율 상승, 풀이 비는 순간부터 시작되는 지연).
- must_rule_out(정답지): Oracle 다운(F25-H 꼴), 행 잠금 경합(F01-P, F08-G, F15-G), transfer 프로브 오설정(F17-R), 부하 증가와 풀 고갈, 네트워크 단절, 앱 배포나 설정 변경. 각각 배제 근거는 정답지에 적었다. 핵심은 ORA-28000 이 "DB 에 닿았고 로그인이 거절됐다"는 뜻이라 다운과 네트워크가 빠지고, Hikari 시간 초과 예외에 ORA-28000 이 원인으로 붙어 "연결을 새로 만들지 못한 풀"임이 드러나 잠금 경합과 부하가 빠진다는 점이다(풀이 다 비면 total=0 이 되지만, 그 전에 남은 세션이 바빠 먼저 시간 초과가 날 수 있어 total 값은 단정하지 않는다). 동반 부하 10rps 는 유입을 늘리지만 부하 증가 경쟁 가설은 관측으로 배제된다: 오류가 나도 Hikari 예외에 ORA-28000 이 붙고, Oracle 활성 세션이 오르지 않으며, 같은 경로가 30rps 를 9분 동안 Hikari 시간 초과 없이 받은 기록(F18-P 녹화)이 있다.
- contrast_with: F17-R(같은 transfer NotReady 와 commerce 502, 원인은 프로브 경로 오설정, transfer 하나만), F01-P(같은 Oracle, 행 잠금 대기), F25-H(공유 DB 프로세스 죽음), F33-R(같은 Hikari 템플릿, 풀이 느린 질의로 꽉 참 vs 새 연결을 못 만들어 비어 감, ORA-28000 원인 예외).

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

설계 강도 하나로 고정한 평가 모드(`approved-fixed-f35-r`). 강도라 할 만한 값은 없다: 잠금은 켜거나 끄는 것뿐이고, 피해 크기는 요청량이 아니라 풀 세션의 수명으로 정해진다.

- 시간 계산: 세션은 생성 뒤 max-lifetime 600000ms 안에 은퇴한다(Hikari 는 최대 2.5% 앞당긴다). 잠금 시각에 있던 세션은 모두 그 전에 만들어졌으므로 잠금 뒤 10분이면 세 풀이 모두 빈다. 풀이 빈 뒤 readiness 3회 실패(30초)로 NotReady. 최악의 경우 잠금 뒤 약 10.5분에 전면 실패가 확정된다. 보통은 풀마다 세션 3~5개 중 마지막 것의 남은 수명이라 약 7.5~8.5분.
- 전면 실패 길이: min_hold 21m 이라 판정(성공 3틱)은 잠금 뒤 약 21.75분에 난다. 전면 실패는 최악에도 약 10.5분, 보통 약 13분 이어진다(F17-R 실행의 약 10분 이상).
- 시간대: 가장 한가한 KST 02~06시(기준선 1 iter/s)에도 세션 수명은 같으므로 전면 실패 시각은 같다. 동반 부하가 시간대와 무관하게 api 스팬과 판정 표본을 준다(잔액 조회 30초 창에 약 165건, 이체 약 30건). 부하가 많아도 잠금 뒤에는 풀이 늘 수 없어 남은 세션만으로 받다가 일부 요청이 3초 대기 끝에 먼저 실패할 수 있다(전면 실패보다 이른 부분 실패).
- 부하: banking surge.js 10rps(ramp 2m, hold 27m, ramp_down 15s). 기준선 최고 4 iter/s 에 더해도 최대 14rps 이고, 같은 경로가 30rps 를 9분 동안 Hikari 시간 초과 없이 받았다(F18-P 녹화). 부하 증가 배제는 §8.
- 서킷브레이커: api → account 서킷이 열리면 api 는 바로 502 로 답한다. 반개방에서 시도해도 account 가 NotReady 라 다시 열린다. 서킷은 피해를 숨기지 않는다.

| 항목 | 값 |
|---|---|
| level | `approved-fixed-f35-r`, min_hold 21m, settle 60s, timeout 27m |
| max_injection_duration | 30m |
| companion | `load.north_south` core-banking surge.js, target_rps 10, entry 30082, ramp 2m + hold 27m + ramp_down 15s |
| success | 동반 부하 잔액 조회 실패율(`loadgen.read_step_status_rate`) ≥ 0.5 그리고 동반 부하 이체 5xx 비율(`loadgen.food_create_status_rate`, 선택자 business.5xx.rate) ≥ 0.5, 3틱 연속 |
| must_rule_out | 동반 부하 achieved_rps < 2.5(부하 끊김), Oracle 파드 NotReady(DB 다운), 2틱 |
| abort | entry_status(domain core-banking, 이체 단계 nginx → api) == 0, 2틱. api 와 nginx 는 DB 를 쓰지 않아 502 로 답하므로 이 시나리오로는 0 이 나오지 않는다 |
| recovery | target_health 200, Oracle Ready, transfer Ready, 기준선 잔액 조회 실패율 < 0.05, 2틱, 15m |
| cleanup | `ALTER USER BANKING ACCOUNT UNLOCK` 뒤 ACCOUNT_STATUS OPEN 확인, 동반 부하 종료, 10m |

recovery 15m 의 근거: 보통은 잠금 해제 뒤 Hikari 가 수 초 안에 다시 연결하고 readiness 가 10초 주기로 돌아와 1분 안에 끝난다. 다만 잠금 중 banking 파드가 다른 이유로 재시작되면 시작 단계의 DB 확인이 실패해 startupProbe(`21-account-service.yaml` 등, failureThreshold 30 x period 5초 = 150초)로 재시작이 반복되고, 해제 뒤에도 CrashLoopBackOff 대기(최대 5분)와 기동, startupProbe 통과(약 1~2.5분)를 기다려야 한다. 합해서 약 7.5분이라 15m 이면 dirty 없이 닫힌다. 이 시나리오의 주입만으로는 재시작이 나지 않는다(liveness 는 livenessState 만 본다).

원장 유실의 뒤끝: ledger 풀이 transfer 풀보다 먼저 비면 그 사이 완료된 이체의 원장 반영이 영구 유실된다(`TransferEventConsumer.java:40-42` 가 catch 후 log.error 만 한다). 잠금 해제가 되돌리지 않는다. 다른 시나리오에 주는 영향은 없다: F14-P 의 판정 질의 `database.ledger_unmatched_transfer_count` 는 최근 창(window_minutes 5, grace 60초)의 COMPLETED 이체만 세고, 원장 대사 배치 `ReconciliationBatch` 는 DEBIT 와 CREDIT 의 차(imbalance)만 보는데 유실은 두 행이 함께 사라져 차가 바뀌지 않는다. 그래서 평시 0 가정은 실행이 지나면 그대로다.

새 관측 쿼리와 러너 변경은 없다(`kubernetes.pod_ready` 의 testbed-oracle, testbed-transfer 는 이미 허용 목록에 있다). 새 실행기 `db.account`(profiles/db_account_executor.py)만 더했다. 109 에서 실행기 preflight(읽기 전용 상태 조회)를 직접 돌려 OPEN 판정을 확인했다(rc 0).

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Harness 2026-01-08, 공식 링크)와 요소별 대응표가 있고, 기전("회전 중 쓰던 DB 사용자가 비활성화되어 서비스가 DB 인증을 잃음")이 같다 (원칙 1, §2)
- [x] 장부를 갱신했다: 새 묶음 O(0), 새 정답 위치 DB 계정(0), 결제 경로 26%와 무관, 서비스 은행 8(음식배달 7 을 고르지 않은 이유는 §3), 어느 축도 20%에 닿지 않는다 (원칙 2, §3)
- [x] 근본 원인 위치: Secret APP_USER, 세 Deployment 의 DB_USER, Hikari 설정, Oracle StatefulSet 이 `file:line` 으로 확인됐다 (G1, §4)
- [x] 근본 원인의 흔적: CH 로그의 ORA-28000(평시 7일 0, 전수 수집), VM DPM `dpm.oracle.session.idle_session` (원칙 3, §7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: 로그 전수, DPM 60초 수집, KCM 이벤트 (원칙 3)
- [x] 계기의 흔적: 잠금 시각과 거절된 로그인 시도 자체는 119 에 남지 않는다(DPM 에 로그인 실패 지표 없음, Hikari 연결 생성 실패 로그는 debug). 첫 흔적은 DPM `idle_session` 의 계단식 감소이고, 잠금이 만든 현상(새 로그인 거절)은 풀이 빈 뒤 ORA-28000 문구로 로그에 남는다. 인공 지연을 쓰지 않는다 (원칙 4, §7)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다: 로그 문구가 계정 잠금을 직접 말한다 (원칙 5)
- [x] 정답지 세 칸: 근본 `banking-oracle:BANKING`, 계기 비움, 부분 점수 인스턴스와 증상 서비스 (원칙 6, §5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다. 골든 시그널이 오를 근거(core-banking-api 서버 스팬 502 가 ERROR, 동반 부하로 표본 스팬 분당 약 21개, 전면 실패 최악 약 10.5분)를 설계로 확보했고, 판정 입력의 한계(벽시계 최근 15분 대 3시간 MV 비교, 멤버 상위 3개 서비스만)와 평시 20:13 drop 의 실제 사유(판정 적체로 창이 빔)를 적었다 (원칙 7, §6, §11)
- [x] 주입 도구가 시나리오 id 를 남기지 않는다: 실행기는 계정 이름과 PDB 만 kubectl 로 넘기고, DB 에 닿는 것은 `ALTER USER BANKING ACCOUNT LOCK/UNLOCK` 뿐이다. 세션 태그나 프로세스 이름이 없다(단위 테스트로 고정) (원칙 8)
- [x] 피해 계산: 세션 수명 10분으로 시간대와 무관하게 전면 실패, 서킷브레이커는 피해를 숨기지 않음, 동반 부하 10rps 는 기준선 최고 위 최대 14rps 로 같은 경로가 견딘 30rps 아래, 부하 증가 경쟁 가설은 관측으로 배제 (원칙 9, §8, §9)

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- 로그에 ORA-28000 이 Hikari 메시지의 다음 예외 또는 'DataSource health check failed' 의 원인으로 실제로 남는지. 남지 않으면 정답지의 결정 증거 문구를 녹화본에 맞게 고친다.
- DPM `dpm.oracle.session.idle_session` 이 계단식으로 0 이 되는지(60초 수집이라 단계가 보여야 한다).
- 세 파드가 재시작 없이 NotReady 가 되는지, commerce checkout 502 가 나는지.
- 판정이 promote 해 인시던트가 생기는지, promote 된 묶음의 판정 입력(상위 3개 서비스)에 어떤 서비스가 들어갔는지, core-banking-api 오류율이 전면 실패 구간 내내 오르는지.
- 인시던트가 안 생기면 먼저 판정 지연(`incident_judge_decisions.updated_at` − `event_clusters.first_event_at`)을 본다. 판정 적체였으면 부하나 구간을 바꾸지 말고 다시 실행한다. 적체가 아니었을 때만 판정 사유를 보고 동반 부하나 구간 길이를 조정한다.
- 잠금 해제 뒤 1분 안에 회복되는지(recovery 15m 안).
