---
title: F54-R 설계 시트 — banking Oracle 앱 계정에 붙인 호출당 읽기 한도가 거래 내역을 끊음
status: Draft
owner: project
last_reviewed: 2026-10-10
tags:
  - scenario
  - design
  - database
summary: DB 자원 한도 정비가 banking Oracle 애플리케이션 계정 BANKING 에 호출당 논리 읽기 한도(30000)를 둔 새 프로필을 붙였는데 한도가 거래 내역 페이지의 건수 질의(호출당 약 5만~20만 블록)보다 낮아, transfer 풀이 세션을 새로 여는 대로 거래 내역이 ORA-02395 로 끊겨 api 502 가 되고 이체와 commerce 정산은 멀쩡한 시나리오(Google 2020-12-14 재구성)의 설계 근거.
---

# F54-R 설계 시트

- id `F54-R`, slug `f54-r-banking-oracle-app-account-call-read-limit-breaks-history`, 새 사례군 F54 의 첫 시나리오(R)
- 상태: 후보(ready + `stage: candidate`), 설계 강도 하나로 고정한 evaluation 모드, 녹화 대기 큐에 추가
- 주입 수단: 기존 실행기 `db.account`(`scripts/scenarios/profiles/db_account_executor.py`)에 프로필 한도 모드를 더함 + `load.north_south` 동반 부하

## 1. 요약과 비유

banking 의 account, transfer, ledger 는 Oracle 에 애플리케이션 계정 BANKING 하나로 접속한다. 이 계정은 커널 한도가 없는 DEFAULT 프로필이다.
DB 자원 한도 정비가 "폭주 질의를 막자"며 프로필 APP_CALL_LIMITS(LOGICAL_READS_PER_CALL 30000)를 만들어 BANKING 에 붙인다.
한도 값은 평소 질의들이 수백 블록이라 넉넉해 보였지만, 거래 내역 페이지마다 도는 transfers 건수 질의는 표 전체를 읽어 호출당 약 5만~20만 블록이다.
Oracle 은 한도를 로그인 때 읽으므로 이미 열린 세션은 그대로이고, transfer 의 Hikari 풀이 세션을 새로 열 때마다 그 세션이 한도를 지닌다.
그때부터 거래 내역 건수 질의가 3만 블록에서 ORA-02395 로 끊겨 transfer 500, api 502 가 된다. 이체, 잔액 조회, 계좌 목록, commerce 정산처럼 가벼운 호출은 같은 세션에서 계속 성공한다.

비유: 은행이 창구 직원 한 사람이 한 번에 꺼낼 수 있는 서류 상자 수를 "평소 몇 상자면 되니 30 상자"로 정했는데, 거래 내역 조회는 원래 서고 전체를 훑어야 하는 일이었다.
입금, 출금, 잔액 확인은 그대로 되고 거래 내역 창구만 매번 "한도 초과"로 멈춘다. 이미 출근해 있던 직원은 새 규칙을 모르다가, 교대해 들어온 직원부터 규칙을 지킨다.

## 2. 원본 사례

- **Google, 2020-12-14**, Google Cloud 상태 페이지 사고 보고 [공식](https://status.cloud.google.com/incident/zall/20013). `docs/ref-real-world-incidents.md` M1 에 이미 있는 행(쿼터 관리, 같은 문서 M19 의 '함께 볼 사례').
  - 새 쿼터 시스템으로 옮기는 중 남아 있던 옛 시스템이 User ID Service 의 사용량을 0 으로 잘못 보고했고, 쿼터 적용 유예 기간이 끝나자 자동 쿼터 관리가 이 서비스의 쿼터를 줄였다.
  - 단일 서비스가 0 부하를 보고하는 경우를 기존 안전 검사가 막지 못했다. 계정 DB 쿼터가 깎여 쓰기를 못 하고 인증 조회 오류, 인증이 필요한 API 전반에 5xx.
  - 03:43 용량 경보, 03:46 오류 경보, 04:08 원인 식별, 04:22~04:27 쿼터 적용을 끄는 것으로 완화, 04:33 정상. 재발 방지로 쿼터 관리 자동화가 전역 변경을 빠르게 적용하지 못하게 하고 잘못된 설정을 더 빨리 잡는 감시.
- 보조(같은 꼴, 원본 아님): GitHub 2025-09-15 월간 가용성 보고, 일부 사용자의 한도를 낮추려던 레이트 리미터 기능 플래그 부분 배포가 리미터를 잘못된 상태로 만들어 모든 요청을 403 으로 거절, 되돌려 즉시 회복
  ([공식](https://github.blog/news-insights/company-news/github-availability-report-september-2025/), 2026-10-10 WebFetch 로 원문 확인). M19 에 추가했다(출처가 말한 사실만).

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 자동 쿼터 관리가 한 서비스의 쿼터를 줄임(옛 시스템이 사용량을 0 으로 보고) | DB 자원 한도 정비가 앱 계정 BANKING 에 호출당 논리 읽기 한도 30000 의 새 프로필을 붙임(한도를 평소 질의 크기로 잡아 무거운 정상 호출을 셈하지 않음) |
| 원인이 된 결함 | 쿼터가 그 서비스의 실제 사용량 아래로 내려감, 안전 검사가 막지 못함 | 한도가 계정이 정상으로 하는 가장 무거운 호출(거래 내역 건수 질의 약 5만~20만 블록, ledger 대사 SUM 약 14.6만)보다 낮음 |
| 전파 경로 | 쿼터를 넘는 쓰기가 거절되어 인증 조회 실패, 그 서비스에 기대는 API 가 5xx | 한도를 지닌 새 세션에서 무거운 호출이 ORA-02395 로 끊김 → transfer 500 → api 재시도, 서킷 → 502 |
| 사용자 증상 | 인증이 필요한 Google Cloud, Workspace API 5xx | banking 거래 내역 조회가 모두 502. 이체, 잔액 조회, 계좌 목록, commerce 정산은 정상 |
| 원인 쪽 상태 | User ID Service 와 DB 는 떠 있고 쿼터만 깎임 | Oracle 과 transfer 는 Ready, 세션도 살아 있고 한도 아래의 호출은 성공 |
| 탐지된 경로 | 용량 경보와 오류 경보 | core-banking-api 오류율, transfer ERROR 'ORA-02395', api 'Transfer service list call failed: 500' |
| 완화와 복구 | 쿼터 적용을 꺼서 회복 | cleanup 이 BANKING 을 DEFAULT 로 되돌리고 프로필을 지움. 한도를 지닌 세션이 풀에서 바뀌며(최대 10분) 회복 |

기전 확인: 원인(자원 한도를 실제 사용량 아래로 잡은 변경)에서 증상(그 한도를 넘는 정상 요청만 거절, 대상 시스템은 살아 있음)까지의 고리가 원본과 같다.
원본의 쿼터는 저장 쓰기량이고 재구성의 한도는 호출당 읽기량이라 자원 종류가 다르다. 우리 스택(Oracle)에서 계정 단위로 사용량 한도를 거는 수단이 프로필이고,
"한도 관리가 실제 사용량을 잘못 보고 줄였다"는 고리 자체는 같다. 원본처럼 원인 쪽 시스템(DB)은 멀쩡하고 특정 요청만 거절된다.
원본에 없는 테스트베드 쪽 성질은 "한도가 로그인 때 읽혀 세션이 바뀌는 대로 번진다"(최대 10분)뿐이고 증상의 꼴을 바꾸지 않는다.

## 3. 숫자 근거 (2026-10-10, `scenario-stats.py`, 정식 + 후보 68 종, 이 후보 전)

- 서비스: commerce 26, **core-banking 21 → 22**, food-delivery 21. 은행과 음식배달이 가장 적다.
- 묶음: A 7, B 7, C 6, D 7, E 2, F 3, **G 7 → 8**, H 2, I 1, J 8, K 2, L 6, M 1, O 1, P 8. 20% 상한(13.6)에 닿는 묶음 없음.
  주 묶음 G(잘못된 설정값, 여기서는 DB 계정의 한도 값을 배포), 피해 모양이 한도에 걸린 정상 요청이라 G+A(A 로 세도 7 → 8, 11.6%).
- 정답 위치: **DB 계정 1 → 2**(69 중 2.9%). 최다 위치는 주문 서비스 7(10.3%). 결제 경로 11/69(15.9%, 이 후보는 무관).
- 부품 지도: banking 의 0 부품은 kafka(비동기, 사용자 증상 없음), nginx(OTel 없음). Oracle 5 중 계정 1(F35-R).
- 왜 이 후보인가: 은행이 가장 적은 서비스 축이고, 정답 위치가 아직 1 인 DB 계정이다. 같은 계정 정답(F35-R)은 로그인 거절로 banking 전체가 실패하는데,
  F54-R 은 로그인과 풀이 정상이고 무거운 호출 둘만 실패해 "같은 범인, 다른 증상"이다. 관제 AI 는 오류 문구와 "무거운 질의만 실패"에서 계정 한도를 추론해야 한다.
  거래 내역 실패라는 겉 증상은 F35-H(이체 표 삭제)와 겹치고 정답이 다르다(표 대 계정).

## 4. 인과 사슬 (코드와 인프라 위치)

1. 계기: testbed-oracle-0 의 FREEPDB1 에서 sysdba 로 `CREATE PROFILE APP_CALL_LIMITS LIMIT LOGICAL_READS_PER_CALL 30000`, `ALTER USER BANKING PROFILE APP_CALL_LIMITS`.
   109 조회(2026-10-10): `RESOURCE_LIMIT=TRUE`, BANKING 과 LUCIDA_MON 모두 DEFAULT 프로필(커널 한도 전부 UNLIMITED).
2. 접속: `core-banking/k8s/22-transfer-service.yaml:48-57`(DB_USER = Secret APP_USER = banking), `transfer-service/src/main/resources/application.yml:7-17`(minimum-idle 5, maximum 15, idle-timeout 60000, max-lifetime 600000).
   한도는 세션 로그인 때 정해진다. 로컬 gvenzl/oracle-free:23-slim(109 와 같은 이미지) 실측: 프로필을 붙인 뒤 이미 열린 세션은 6천 블록 전수 스캔을 마치고, 새 세션은 한도 1000 에서 같은 질의가 `ORA-02395: exceeded call limit on IO usage`,
   같은 세션의 가벼운 질의는 성공. 프로필을 DEFAULT 로 되돌리거나, 한도를 UNLIMITED 로 바꾸거나, 프로필을 지워도 한도를 지닌 세션은 계속 ORA-02395 이고 새 세션은 정상.
3. 무거운 질의: `transfer-service/.../repository/TransferRepository.java:17-28`(search, 선택 조건이 모두 `:x is null or` 라 Spring Data Page 의 count 가 transfers 전수 스캔), `TransferService.java:122-124`, `TransferController.java:51-63`.
   109 v$sql: 건수 질의 `byb2y2hv7p4ag` 실행당 60,829 블록(345만 회, 315ms), 같은 요청의 페이지 본 질의 `99nsgbv90qrn7` 78 블록. DPM top SQL 7일: 건수 질의 avgLogicalReads 최소 46,362, 중앙 95,782, 최대 197,684.
   ledger 대사 `bzbd2xfdtjdxv`(`LedgerEntryRepository.java:19-20`, 방향별 SUM) 146,033 블록, `ReconciliationBatch.java:26-34` 가 예외를 잡지 않아 스케줄러가 ERROR 로 남긴다.
4. 가벼운 질의(한도 아래, 109 v$sql): outbox 릴레이 폴링 `470as8m1vuxbs` 10,100(DPM 7일 최대 10,456), 이체의 계좌 FOR UPDATE 5, transfers INSERT, ledger INSERT 14, 계좌 목록 count 15, 보존 정리 select 3, 그 밖의 BANKING 질의 모두 400 아래.
   DPM 7일 BANKING 질의 중 1만 블록을 넘는 것은 위 셋뿐(한 번 돈 DBMS_SCHEDULER 통계 수집은 SYS 세션이라 무관). 한도 30000 은 릴레이의 약 2.9배, 건수 질의 최소값의 약 0.65배다.
5. 전파: transfer 가 GET /api/transfers 에 500(Hibernate ERROR 'ORA-02395 ...', Tomcat `Servlet.service() ... threw exception`) → api `TransferClient.java:32-60` 이 재시도 3회(200ms 지수) 뒤 502,
   `api-service/.../application.yml:30-54` transferClient 서킷(10회 중 50%, 5초 열림)이 열려 fallback 502. 이체(POST)는 api → account(AccountClient) → transfer 경로라 영향 없음.
6. nginx 경로: `core-banking/k8s/02-configmaps.yaml:38-39`(/api/transfers → api), 45-46(/api/accounts → account). commerce 정산은 commerce payment → transfer POST.

## 5. 원인 규정 (원칙 5, 6)

- 근본(`root_cause.target_id`): `banking-oracle:BANKING`(target_kind database, F35-R 과 같은 표기). 결함은 계정에 붙은 한도 값이다. 고쳐야 재발이 막히는 곳이 그 프로필이다.
- 계기(`trigger_target_id`): 비움. 계기도 같은 곳(BANKING 의 프로필 변경)이다.
- 원칙 6: 거래 내역 요청과 건수 질의는 늘 하던 그대로 정당하다(질의가 비효율인 것은 원래부터이고 피해를 키운 성질이라 user_impact 쪽 설명이다, 원칙 5). 잘못된 것은 그것을 끊는 한도다. 원본도 쿼터 변경을 원인으로 적었다(같은 층위).
- 부분 점수(`scoring.partial`): `banking-oracle`(인스턴스까지만), `banking-transfer`(오류를 낸 서비스), `banking-api`(증상), `banking-ledger`(같은 오류의 다른 서비스).
- 채점 입도 `database-relation`, accept `banking-oracle:BANKING`.
- 원칙 5: 정답은 "BANKING 계정에 새로 걸린 호출당 읽기 한도가 거래 내역 건수 질의를 끊었다"이고 관제 데이터(ORA-02395 문구, 두 서비스의 가장 무거운 질의만 실패, DPM top SQL 의 읽기량, 이체 정상)로 낼 수 있다.
  한도 값이 어떻게 정해졌는지나 건수 질의를 고치는 법은 요구하지 않는다.

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 | 기대 |
|---|---|---|
| 감지(lucida-next) | core-banking-api 오류율(APM error_rate, 서버 스팬 502), transfer ERROR 로그 새 꼴(ORA-02395, Servlet 예외) 급증, api ERROR 'Transfer service list call failed' | 별도 업무 규칙 없이 이벤트와 인시던트가 나야 한다. 거래 내역은 api 요청의 약 70%(동반 부하 중) |
| 피해 판정(러너) | core-banking-api 서버 스팬(표본 10%) 60초 창 5xx 백분율 ≥ 30, 3틱 | 평시 1시간 표본 441건 중 5xx 0 |
| RCA | §7 의 transfer, ledger 오류 문구, DPM top SQL, 이체 정상, KCM 무변화 | 근본 위치 BANKING 계정까지 간다 |

## 7. 관측 근거 (119 실조회, 2026-10-10 10:00~10:40 UTC)

| 증거 | 표, 칸 | 조회와 결과(평시) | 표본 여부 |
|---|---|---|---|
| 계기의 현상: 호출 한도 초과 | CH `lucida.lucida_logs_local` (`service_name`, `severity_text`, `body`) | `countIf(body LIKE '%ORA-02395%')`, `countIf(body LIKE '%exceeded call limit%')` (core-banking-%) → 0, 0. 보존 시작 2026-10-03 09:47. 같은 꼴의 ORA 오류가 Hibernate ERROR 로 남는 것은 F35-R 실행(2026-10-09 06:55) 'ORA-28000 ...' transfer 342, account 443, ledger 15 건으로 확인 | 전수 |
| 전파: transfer 의 요청 실패 | 같은 표 | Tomcat SEVERE `Servlet.service() for servlet [dispatcherServlet] ... threw exception` 이 transfer 에 남는 꼴을 평시 기록(2026-10-05, 10-06 CannotAcquireLockException 각 1건)으로 확인 | 전수 |
| 전파: api 의 거래 내역 502 | 같은 표 `service_name='core-banking-api'` | 7일 'Transfer service list call failed', 'circuit open/exhausted' 는 2026-10-09 06:55~14:08(다른 시나리오 실행 창)에만 있음. 그 안의 transfer 500 응답 꼴: `500 : "<html><body><h1>Whitelabel Error Page...` 22건 | 전수 |
| 감별: 같은 계정의 다른 무거운 질의 | 같은 표 `service_name='core-banking-ledger'` | 7일 'Ledger reconciliation batch started' 1,004회(약 10분마다), 'finished' 1,003회, 'Unexpected error occurred in scheduled task' 1회. 장애 시 started 뒤 finished 없이 그 ERROR 와 ORA-02395 | 전수 |
| 감별: 가벼운 일은 정상 | 같은 표 `service_name='core-banking-transfer'` | 최근 1시간 'Transfer COMPLETED ... from=commerce-settlement' 2,607, 'from=ACC-' 1,921. 장애 중에도 유지되어야 한다 | 전수 |
| 근본의 단서: 질의별 읽기량 | CH `lucida.dpm_topsql_local` (`sql_id`, `body` 의 avgLogicalReads, schema) | 7일 oracle BANKING: byb2y2hv7p4ag 9,823 기록, avgLogicalReads 최소 46,362, 중앙 95,782 / 470as8m1vuxbs 중앙 10,100 / 그 밖 4천 아래. 분당 기록이라 장애 중에도 건수 질의 기록이 남는다(실행은 일찍 끊김) | 수집 주기 1분, 전수 아님(상위 SQL) |
| 감별: 쿠버네티스 무변화 | CH `lucida.kcm_events_local` | 7일 rca-testbed-banking 이벤트는 Unhealthy 114, ScalingReplicaSet 16, 그 밖의 배포 관련 각 8건. 장애 구간에 ScalingReplicaSet, Killing, Unhealthy 가 없어야 한다 | 전수 |
| 감별: 풀 정상 | VM `dpm.oracle.session.idle_session` | F35-R 시트의 8일 분포(중앙 11, 1백분위 10). 이 장애는 세션을 끊지 않아 평시 범위에 머문다(F35-R 은 0 까지 떨어짐) | 1분 수집 |
| 피해 보조: 스팬 | CH `lucida.otel_traces_local` | 최근 1시간 표본 core-banking-api GET /api/transfers 298건(평균 251ms), POST 143, transfer GET /api/transfers 298(250ms). 장애 시 api GET 이 502, transfer GET 이 500 | 10% 표본 |

핵심 증거(ORA-02395 문구, 무거운 질의 둘만 실패, 이체와 정산 지속, KCM 무변화)는 모두 전수 수집 로그와 이벤트다. 스팬과 APM 지표는 보조이고 러너 판정에만 쓴다.
프로필 변경 명령 자체는 119 에 남지 않는다(DPM 에 DDL, 계정 속성 항목이 없음). F35-R 과 같은 기준으로, 변경이 만든 현상(ORA-02395)이 계기의 흔적이다(원칙 4).

## 8. 감별

- must_support: 정답지 6개(transfer ORA-02395 와 Servlet 예외, ledger 대사 실패, api 거래 내역 502 와 서킷, 이체와 정산 지속, DPM top SQL 읽기량, KCM 무변화와 풀 정상).
- must_rule_out: 계정 잠금(F35-R), 표 삭제(F35-H), Oracle 포화와 느린 질의(F42-R), transfer 다운이나 엔드포인트 비움(F17-R, F17-H, F50-R, F52-R), DNS, 네트워크(F37-R, F44-P), 배포.
- contrast_with: F35-R(같은 계정, 로그인 거절), F35-H(같은 거래 내역 실패, 표 삭제), F42-R(같은 transfers 전수 스캔, 릴리스로 포화), F51-R(banking 한 경로만 실패, account replicas 0).
- 러너 배제 조건: 동반 부하 미전달(achieved_rps < 2.5), Oracle NotReady, 동반 부하 이체 5xx ≥ 0.2(이체까지 실패면 다른 원인), 잔액 조회 실패율 ≥ 0.2.

## 9. 러너 판정과 강도, 부하 계산 (원칙 9)

- 강도: 하나로 고정(approved-fixed-f54-r, 한도 30000). 사다리 없음. 한도는 건수 질의의 7일 최소(46,362) 아래, 릴레이 7일 최대(10,456) 위라 실행 시각과 무관하게 같은 질의만 걸린다.
- 피해 계산: 건수 질의는 거래 내역 요청마다 한 번 돈다(실행 345만 회 대 본 질의 338만 회). 한도를 지닌 세션에서는 예외 없이 실패한다(결정적).
  transfer 풀은 idle-timeout 60초(minimum-idle 5 위)와 max-lifetime 10분으로 세션을 바꿔, 늦어도 변경 뒤 10분이면 모든 세션이 한도를 지닌다.
  동반 부하 10rps 의 거래 내역 25%(2.5건/초)와 기준선(약 0.8건/초)이 모두 실패하고, api 재시도 3회(200ms 지수) 뒤 서킷이 열리면 즉시 502 다.
  api 서버 스팬은 거래 내역 약 3.3건/초, 이체 약 1.4건/초라 5xx 백분율은 약 70%, 60초 창 표본 약 28개다. 판정 임계 30 은 표본 흔들림(표준편차 약 9%p)에도 넉넉하다.
- 부하: `load.north_south` core-banking surge.js 10rps(F35-R 과 같은 값, 상한 180 의 6%), ramp 2m, hold 27m. 은행 건강 상한 20rps 아래. 한도에 걸린 건수 질의는 3만 블록에서 멈춰 평시(약 9.6만 블록)보다 DB 일이 준다.
- 판정: success = api_error_rate ≥ 30 이 3틱. min_hold 21m, level timeout 27m, max_injection_duration 30m(F35-R 과 같은 이유: 최악 10분 뒤 전면 실패, 그 뒤 약 11분 유지).
- cleanup: BANKING 을 DEFAULT 로, 프로필이 남아 있으면 지우고, `DEFAULT|NONE|TRUE` 확인(멱등). recovery: 사전 상태와 같고 Oracle, transfer Ready, 기준선 잔액 조회 실패율 < 0.05, api 5xx 백분율 < 10.
  한도를 지닌 세션이 풀에서 바뀌는 데 최대 10분이라 recovery timeout 15m.
- 실행기 변경: F35-R 계약과 잠금 스크립트는 그대로 두고, 계약에 profile 칸이 있으면 프로필 한도 스크립트를 쓰는 모드를 더했다. preflight 는 사전 상태(DEFAULT 프로필, 프로필 없음, RESOURCE_LIMIT TRUE)만 읽는다.
  2026-10-10 로컬 Oracle 에서 preflight → run → (preflight 실패 확인) → cleanup → cleanup(멱등) → recovery 를 돌려 모두 기대대로였고, 109 에서 preflight(읽기 전용)를 돌려 통과했다.
- 안전: 바꾸는 것은 새 프로필 하나와 BANKING 의 프로필 칸 하나다. DEFAULT 프로필, LUCIDA_MON(DPM), SYS, 비밀번호, 계정 상태는 건드리지 않고 세션도 끊지 않는다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Google 2020-12-14, 링크)와 요소별 대응표가 있고 기전이 같다(§2) (원칙 1)
- [x] 분류 장부를 갱신했다(§4-1 행, §6 기록). 묶음 G 7→8(11.6%), 정답 위치 DB 계정 1→2(2.9%), 결제 경로 11/69(15.9%, 무관), banking 21→22. 어느 축도 20% 에 닿지 않는다(§3) (원칙 2)
- [x] 근본 원인 위치가 인프라 지점(FREEPDB1 BANKING 프로필, RESOURCE_LIMIT, v$sql 실행당 블록)과 코드 위치로 확인됐다(§4) (G1)
- [x] 근본 원인의 흔적이 119 실데이터에서 조회됐다: 로그의 ORA 오류 꼴, DPM top SQL 의 질의별 읽기량(§7) (원칙 3)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: 로그, KCM 이벤트는 전수(§7) (원칙 3)
- [x] 계기의 흔적이 조회됐다: 변경이 만드는 오류 문구(ORA-02395)가 로그에 남는 경로와 평시 0건. 인공 지연 없음(§7) (원칙 4)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다(§5) (원칙 5)
- [x] 정답지 세 칸이 원칙 6대로 채워졌다(§5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다(§6) (원칙 7)
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: 프로필 이름 APP_CALL_LIMITS, 실행기 인자와 스크립트에 id 없음(실행기 테스트가 확인). k6 동반 부하 태그는 기존 시나리오와 같은 방식 (원칙 8)
- [x] 부하 상한과 서킷브레이커를 넣은 피해 계산이 있다(§9) (원칙 9)

## 11. 후보 목록 (반복 25, 2026-10-10)

기존 목록이 아니라 "실제 기전 × 부품"에서 출발했다. 0 묶음은 N(막힌 시도 다수), 0 부품은 kafka, notify, gateway, nginx(모두 비동기이거나 관측 없음)라 그 밖의 부품과 인프라 층까지 넓혔다.

| 순위 | 후보 | 원본 사례 | 부품, 층 | 판정 |
|---|---|---|---|---|
| 1 | **banking Oracle 앱 계정에 호출당 논리 읽기 한도 프로필, 거래 내역 건수 질의가 ORA-02395** | Google 2020-12-14 공식(보조 GitHub 2025-09-15) | Oracle 계정, DB(G+A) | **채택**(DB 계정 1, 은행 21, G 7) |
| 2 | banking 워커 tb-w2 에 정적 경로를 더하며 접두사 오타로 commerce 파드 대역 일부를 엉뚱한 다음 홉으로 보냄(정산 응답 유실) | UC Berkeley EECS 2015-07-07 기관 공지(경로 추가 실수로 /24 의 1/4 이 다른 건물로), GitHub 2025-06-17 공식(라우팅 정책 배포로 도달 불가) | tb-w2 노드 경로, 인프라(K) | 순위 뒤(버림 아님): 정답 위치 노드, 디스크 6, F44-P 와 정답(tb-w2), 끊기는 흐름(commerce 정산), 증상이 같아 정답 쏠림에 보탬 |
| 3 | banking Oracle USERS 표 공간 할당량(QUOTA)을 사용량 아래로 | Google 2020-12-14 | Oracle 계정, DB | 버림: 원칙 9. 쿼터는 새 익스텐트를 잡을 때만 검사하는데 USERS 는 하루 약 40~50MB, 8MB 단위로 자라(VM dpm.oracle.tablespace.used 7일) 몇 시간에 한 번만 걸려 피해 시각이 정해지지 않음 |
| 4 | banking 앱 계정 SESSIONS_PER_USER 를 풀 합 아래로 | Google 2020-12-14 | Oracle 계정, DB | 버림: 원칙 9. 네 서비스가 한 계정을 함께 써 남은 세션을 어느 풀이 차지할지 정해지지 않고(rejected 61 과 같은 벽), 그래도 증상이 F35-R 과 같음 |
| 5 | 동기 위 비동기(sync-over-async) 릴리스로 스레드 풀 기아 | Azure DevOps 2018-10 공식 | 앱(J) | 버림: 원칙 1. 원본은 잠재 결함 배포 뒤 의존 서비스 지연이 계기라 계기가 둘 |
| 6 | 토큰 조회를 스레드 풀로 옮긴 릴리스 뒤 인가 서비스 지연으로 풀 고갈 | Atlassian Confluence 2023-01 공식 | 앱(J+E) | 버림: 원칙 1, 계기 둘(릴리스와 의존 지연) |
| 7 | 오류 페이지가 큰 요청 본문을 로그로 남겨 노드 디스크가 차는 릴리스 | Scalingo 공식 블로그 'Outage of Wed 29th July postmortem' | 노드 디스크, 앱(J+A) | 버림: 컨트롤러 필수 중단 조건과 같은 벽(F10-R: 디스크가 차면 노드 전체 DiskPressure, 로그 전수 수집으로 119 에도 부담) |
| 8 | 전역 레이트 리미터 플래그 부분 배포로 전부 403 | GitHub 2025-09-15 공식 | 앱(G) | 버림: food, banking 에 레이트 리미터가 없어 패치로 넣으면 기전이 바뀜(원칙 1), 4xx 단일 신호(F34-R 교훈) |
| 9 | GIN 대기 목록 정리 잠금으로 notes INSERT 정지 | GitLab 2018-03-12 사고 이슈 | commerce PostgreSQL | 버림: 원칙 1(PostgreSQL 9.6.7 이전 버그 고유), commerce 는 최다 서비스인데 정답 위치가 0 이 아님 |
| 10 | 노드 레이블 이름 변경으로 nodeSelector 가 아무 노드도 못 골라 새 파드 Pending | Reddit 2023-03-14 공식 | 스케줄링, 인프라 | 버림: 원칙 9. 앱은 kubernetes.io/hostname 으로 고정되어 레이블 이름 변경의 영향이 없고, 바꾸려면 Deployment 를 고쳐야 하는데 maxSurge 0 인 transfer 만 피해가 나 F50-R, F52-R, F17-H 와 같은 꼴 |

서로 다른 부품 7개(Oracle 계정, tb-w2 노드 경로, 앱 스레드 풀, 노드 디스크, 앱 리미터, commerce PostgreSQL, 스케줄링), 앱, DB, 인프라 세 층.
