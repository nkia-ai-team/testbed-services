---
title: F74-R 설계 시트 (가맹점 입금 확인 폴링의 주기 단축이 거래 내역 건수 질의로 banking Oracle CPU 를 포화)
status: Draft
owner: project
last_reviewed: 2026-10-12
tags:
  - scenario
  - design
  - slow-query
  - polling
summary: 가맹점 입금 알림 제휴 연동이 banking 거래 내역 API 로 가맹점 계좌 8개의 "오늘 완료 입금이 있는가"를 확인하는 주기를 60초에서 1초로 줄이자(초당 8건, page=0&size=1), 한 건만 필요한 확인마다 transfer 의 목록 API 가 Page 건수 질의(OR-null 술어라 transfers 약 650만 행 전수, 실행당 논리 읽기 약 10만, CPU 약 0.5초)를 함께 돌려 Oracle CPU 2개를 다 쓰고, transfer 의 연결 풀이 건수 질의에 묶여 banking 이체와 commerce 정산이 간헐적으로 5xx 가 된다. 배포, 설정, DB 변경은 없다. 원본 Fly.io 2026-04-23.
---

# F74-R 설계 시트

## 1. 요약

banking 거래 내역 API(`GET /api/transfers`)는 page/size 를 주면 Spring Data `Page` 로 답한다. 결과가 한 쪽을 꽉 채우면(받은 행 수 = size) 조건에 맞는 전체 건수를 세는 count 질의를 하나 더 돌린다. 이 count 질의(sql_id `byb2y2hv7p4ag`)는 OR-null 술어(`(:1 is null or from_account = :2) and ...`)라 어떤 인덱스도 못 타고 BANKING.TRANSFERS 약 650만 행을 매번 전수로 읽는다(실행당 논리 읽기 약 10.2만, CPU 0.25~0.6초). 평시에는 기준선 거래 내역 조회가 초당 0.2~1.2건이라 Oracle CPU 2개 가운데 0.1~0.6개만 쓴다.

가맹점 입금 알림을 해 주는 제휴 연동이 가맹점 정산 계좌 8개(시드 계좌 ACC-1001~1007 과 commerce 정산이 들어오는 commerce-merchant)마다 "오늘 완료된 입금이 한 건이라도 있는가"를 거래 내역 API 로 확인한다. 맨 앞 한 건만 있으면 되므로 `toAccount=<계좌>&status=COMPLETED&from=<오늘 0시>&page=0&size=1` 로 부른다. 알림을 빠르게 하려고 확인 주기를 계좌당 60초에서 1초로 줄인 새 버전을 내보내자 호출이 초당 0.13건에서 8건이 된다. 한 건을 찾는 질의 자체는 논리 읽기 89번이지만, 한 쪽(1행)이 꽉 차므로 매 호출 count 질의가 따라 붙어 전수 스캔이 초당 8번 늘어난다(CPU 약 4개분). Oracle(Oracle Free cpu_count 2, 컨테이너 2000m)이 상한에 붙어 세션이 `resmgr:cpu quantum` 을 기다리고, transfer 의 Hikari 15개가 느려진 건수 질의에 묶여 이체 쓰기가 연결을 3초 안에 못 얻고 500, account 재시도와 서킷, api 502 가 되며 commerce 정산(payment → transfer)도 실패해 checkout 이 502 가 된다. api 의 transferClient 서킷이 열리면 폴링이 잠깐 끊겼다가 반열림에서 다시 몰려, 피해는 몇 초 단위로 들쭉날쭉하다(원본의 "간헐적 500").

비유: 은행 창구에 제휴사 직원이 "오늘 이 가게 앞으로 입금이 하나라도 있나요?"를 묻는데, 창구는 첫 장만 보여 주면 될 것을 매번 "전체 몇 건인지"까지 장부 전체를 넘겨 세서 답한다. 제휴사가 묻는 간격을 1분에서 1초로 줄이자 장부 넘기는 일만으로 창구 직원 둘이 다 묶여, 입금하러 온 다른 손님까지 줄을 서다 돌아간다. 제휴사의 질문은 정당하고, 고칠 곳은 "있나 없나"에 전체 건수를 세는 창구 절차다.

## 2. 원본 사례

- **Fly.io, 2026-04-23**(공식 인프라 기록 "April 23: Extension provider polling overloaded Postgres", 11:12 UTC): <https://fly.io/infra-log/provider-polling-postgres/>
- 요약(출처가 말한 것만): 확장 기능 제공사가 Fly.io 비공개 API 엔드포인트 하나를 부르는 폴링 빈도를 높였다. 그 엔드포인트는 비싼 Postgres 질의를 돌렸고, 높아진 빈도에서 대시보드와 GraphQL API 를 받치는 데이터베이스 CPU 를 포화시켜 대시보드와 GraphQL API 엔드포인트가 약 40분 간헐적 500 을 냈다. 제공사가 폴링 빈도 변경을 되돌리자 정상으로 돌아왔다. 그 질의는 한 조직이 제공사에 확장 기능을 등록했는지를 확인하는 것이었는데 필요한 것보다 훨씬 많은 행을 훑었다("scanning far more rows than it needed to"). 첫 일치에서 멈추도록 질의를 고쳐 썼고 그 엔드포인트에 요청 한도를 더하는 중이다.
- 자료 문서 `ref-real-world-incidents.md` M3 에 이번에 추가했다(앞선 시나리오에 없던 원본).

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 제휴 제공사가 비공개 API 엔드포인트 폴링 빈도를 높임 | 가맹점 입금 알림 제휴 연동이 계좌당 확인 주기를 60초 → 1초로 줄임(가맹점 계좌 8개, 초당 0.13 → 8건) |
| 원인이 된 결함 | 존재 확인 질의가 필요한 것보다 훨씬 많은 행을 훑음(첫 일치에서 멈추지 않음) | "오늘 입금이 하나라도 있나"(page=0&size=1)에 transfer 목록 API 가 Page 건수 질의를 붙여 OR-null 술어로 transfers 약 650만 행을 전수로 셈(한 건 찾기 89 읽기 대 건수 약 10.3만 읽기) |
| 전파 경로 | 그 DB CPU 포화 → 같은 DB 를 쓰는 대시보드, GraphQL API 요청 실패 | Oracle CPU 2개 포화(`resmgr:cpu quantum`) → transfer Hikari 15개가 건수 질의에 묶임 → 이체 쓰기 연결 대기 3초 초과 500 → account 재시도와 서킷, api 502 → commerce 정산 실패로 checkout 502. 같은 Oracle 을 쓰는 잔액 조회도 느려짐 |
| 사용자 증상 | 대시보드와 GraphQL API 간헐적 500(약 40분) | banking 이체 5xx, commerce checkout 502 가 몇 초~몇십 초 단위로 들쭉날쭉(api 서킷 열림, 반열림 주기). 잔액 조회 일부 실패. 상품 조회 정상 |
| 원본의 탐지 경로 | 기재 없음(질의와 폴링 빈도 변경을 짚음) | lucida-next 오류율, 지연 이상. DPM Top SQL 의 기존 건수 질의 실행 수 급증과 Oracle CPU 상한, DPM 세션 machine(transfer 파드), transfer Hikari 오류 로그, 표본 트레이스의 url.query `toAccount=...&size=1` |
| 완화와 복구 | 제공사가 폴링 빈도를 되돌림. 질의를 첫 일치에서 멈추게 고침, 요청 한도 추가 | 폴링이 멈추면(cleanup 이 k6 종료) 몇 초 안에 Oracle 이 비고 정상. 근본 수리는 존재 확인에 건수를 세지 않게(Slice, exists, 인덱스 타는 질의) — 시나리오는 cleanup 으로 폴링을 멈춘다 |

기전 고리(외부 호출자의 폴링 빈도 증가 → 호출마다 필요 이상으로 많은 행을 훑는 질의 → 공유 DB CPU 포화 → 같은 DB 를 쓰는 다른 요청의 간헐적 실패)는 원본과 같다. 원본은 PostgreSQL, 여기는 Oracle 이고 비싼 부분이 존재 확인 질의 자체가 아니라 같은 호출의 건수 질의라는 점만 다르다. 두 경우 모두 "확인에 필요한 것은 한 행인데 매번 훨씬 많은 행을 읽는다"가 결함이다.

## 3. 숫자 근거 (2026-10-12, `scenario-stats.py`, 정식 + 후보 합계 109, 이 후보 전)

- 서비스: 쇼핑몰 37, 은행 **36**, 음식배달 **36** → 합계가 가장 적은 은행, 음식배달 동률에서 은행을 골랐다(원칙 2, 2026-10-12 규칙). 음식배달에 두지 않은 이유는 아래 후보 목록 2, 3(order readiness 가 DB health 라 MySQL 포화가 food 입구를 0 으로 만드는 필수 중단 벽, dispatch 목록은 Slice 라 건수 질의가 없음).
- 묶음: D 17(15.6%), J 17(15.6%), P 13, G 12, A 9, L 8, B 7, C 6, E 4, F 3, K 3, O 3, H 2, I 2, Q 2, M 1, N 0. 20%(21.8) 에 닿은 묶음 없음. 이 후보는 **F+E**(주 묶음 F 느린 쿼리, 3 → 4, 110 중 3.6%). F 는 현실 근거(M21 인덱스, M2 배포 질의)에 비해 적은 묶음이다.
- 정답 위치: 은행 이체 서비스 9(8.3%) → 10(9.1%). 20% 에 닿은 위치 없음. 결제 경로 13/110(11.8%), 이 후보와 무관.
- 은행 부품 지도: oracle 9, kafka 0, api 5, account 11, transfer 11, ledger 2, nginx 0. kafka(outbox 비동기라 사용자 증상 없음)와 nginx(119 미수집)는 이번에도 막혀 있다(아래 목록 10, 그리고 rejected 333 행).
- 왜 이 후보인가: 묶음 F 가 3개뿐이고, 은행의 F 계열(F42-R)은 배포가 들여온 새 질의였다. 이 후보는 아무것도 배포하지 않고 기존 질의를 부르는 바깥 호출 빈도만 바뀐다. F42-R(새 릴리스 질의로 같은 Oracle 포화, 같은 transfer 풀 고갈)과 겉 증상이 같고 원인이 달라(롤아웃 없음, 새 sql_id 아님, 기존 건수 질의 실행 수만 급증) 관제 AI 가 Top SQL 과 롤아웃 유무로 가려야 한다(카탈로그 관계 H 의 가치). 같은 거래 내역 API 를 바깥 호출자가 부르는 F70-R 과는 피해 기전이 다르다(F70-R 은 page 없는 한 건이 transfer 힙을 채움, F74-R 은 페이지를 지킨 작은 호출 다수가 Oracle CPU 를 채움, transfer 는 멀쩡히 기다릴 뿐).

### 3단계 후보 목록 (실제 기전 × 부품, 숫자 순, 버린 이유 포함)

| # | 부품 | 기전 × 사례 | 결과 |
|---|---|---|---|
| 1 | banking transfer 목록 API 건수 질의 → Oracle | 외부 폴링 빈도 증가 × 필요 이상 행을 훑는 질의 → DB CPU 포화 (Fly.io 2026-04-23 공식) | **채택**(F+E, F 3 → 4, 은행 이체 서비스 9 → 10) |
| 2 | food order 일별 통계 → MySQL | 같은 기전으로 `GET /api/orders/stats/daily` 폴링 | 버림: order 가 food 입구이고 readiness 가 DB health 라 MySQL 포화 때 order NotReady → entry_status 0(컨트롤러 필수 중단, rejected 54·83 행과 같은 벽) |
| 3 | food dispatch 배달 목록 → MySQL | 같은 기전으로 배달 목록 폴링 | 버림: 목록이 Slice 라 건수 질의가 없어 '필요 이상 훑기'가 없고, 같은 표 COUNT 꼴은 F33-H(릴리스), 피해 문장 'Dispatch service unreachable' 은 F32-R·F33-R·F70-P 와 겹침(rejected 135 행) |
| 4 | banking account 계좌 목록 | 같은 기전으로 계좌 목록 폴링 | 버림: ACCOUNTS 1,001행이라 건수 질의가 싸서 포화가 안 남(원칙 9, F70-R 후보 2 와 같은 벽) |
| 5 | banking ledger 원장 목록 | 같은 기전으로 원장 목록 폴링 | 버림: ledger 는 바깥 진입 경로가 없고 사용자 경로 밖(원칙 1·7, rejected 253 행) |
| 6 | banking account JVM 힙 설정 | -Xmx 를 컨테이너 한도보다 크게 둔 설정 배포로 OOMKill | 버림: 공식 사후 보고 없음(웹 검색 1회, 업체 블로그·dev.to 뿐, 원칙 1) |
| 7 | banking Oracle TRANSFERS 잠금 대기열 | 느린 읽기 뒤에서 열 추가 DDL 이 기다리며 뒤 질의를 막음 (Honeycomb 2024-08-06) | 버림: Oracle 은 SELECT 가 TM 잠금을 잡지 않아 느린 읽기 뒤에서 DDL 이 기다리지 않음(기전 성립 안 함, 원칙 1). DML 뒤 대기는 DDL_LOCK_TIMEOUT 0 기본에서 ORA-00054 로 즉시 실패 |
| 8 | banking transfer 보존 배치 | 보존 기간 0 배포로 끝난 이체 삭제 (Azure DevOps 2023-05-24) | 버림: rejected 49 행(조용한 손실, 원칙 7) |
| 9 | banking account 이자 배치 | cron 오배포 | 버림: rejected(반복 68), 공식 사례 없음·1,001행 갱신이라 잠금 창 1~2초 |
| 10 | banking Kafka | 보존 설정으로 디스크 가득 (Inngest 2025-10-16) | 버림: rejected 333 행, outbox 비동기라 사용자 증상 없음(원칙 7) |
| 11 | banking api 폴링 백오프 없음 | 실패에 백오프 없는 화면 폴링 + 분석 값 즉석 계산 (Buttondown 2025-11-06 공식 https://buttondown.com/blog/incident-0022 ) | 버림: 원본 근본 원인이 '분석 캐시가 뒤처짐'과 '백오프 없는 폴링'의 복합이라 계기가 둘(원칙 1), 은행에 캐시 경로가 없음. 같은 꼴은 rejected 326 행 |

## 4. 인과 사슬 (코드와 인프라 위치)

1. 제휴 연동(k6 constant-arrival-rate 8/s) → `nginx` `/api/transfers` → api `TransferController.list`(`core-banking/api-service/src/main/java/com/corebanking/api/controller/TransferController.java:27-32`) → `ApiService.listTransfers` → `core-banking/api-service/src/main/java/com/corebanking/api/client/TransferClient.java:32-50`(`listTransfers`, 질의 문자열을 그대로 넘김, `@CircuitBreaker(transferClient)`, `@Retry(transferClient)`, 실패 로그 :41 'Transfer service list call failed', 서킷 로그 :53)
2. api `application.yml:13-16`(transfer read-timeout 10s), `:30-38`(서킷 transferClient: 창 10, 최소 5, 50%, 열림 5s, 반열림 3), `:48-54`(재시도 3, 200ms 지수) → transfer 가 500 이면 같은 호출을 최대 3번 보내고, 실패가 쌓이면 서킷이 5초 열려 폴링을 끊었다가 반열림 뒤 다시 흘림
3. transfer `core-banking/transfer-service/src/main/java/com/corebanking/transfer/controller/TransferController.java:51-64`(`list`, page/size 가 있으면 `PageRequest.of(page, size)`) → `TransferService.java:120-127`(`search`, `Page` 를 받아 `getContent()`) → `TransferRepository.java:17-28`(`Page<Transfer> search`, OR-null 술어 JPQL) → Spring Data 가 첫 쪽이 꽉 차면(1행 = size 1) 건수 질의 `select count(t1_0.id) from transfers t1_0 where (:1 is null or t1_0.from_account=:2 ) and (:3 is null or t1_0.to_account=:4 ) and ...` 를 따로 돌림(sql_id byb2y2hv7p4ag, plan_hash 2603328921)
4. BANKING.TRANSFERS 6,531,859행(2026-10-12 109 실측). 인덱스 IDX_TRANSFERS_TO(to_account), IDX_TRANSFERS_STATUS, IDX_TRANSFERS_CREATED 가 있지만 OR-null 술어라 건수 질의는 전수 스캔이다. 109 Oracle 읽기 전용 실측(2026-10-12, ACC-1003, 오늘 완료 입금 4,907건): 한 건 찾기(`fetch first 1 rows only`) 논리 읽기 89, CPU 0.01초 미만 / 건수 질의 논리 읽기 102,923, CPU 0.53초, 경과 0.62초. DPM Top SQL 24시간: byb2y2hv7p4ag 실행당 논리 읽기 101,500~103,063, CPU 중앙값 0.25~0.56초, 실행 수 초당 0.16~1.25(기준선 거래 내역)
5. Oracle `core-banking/k8s/10-oracle.yaml:43-49`(limits cpu 2000m), cpu_count 2(v$parameter 실측). 넘치는 세션은 `resmgr:cpu quantum` 을 기다린다(DPM 7일 232건, byb2y2hv7p4ag 163건, 모두 다른 시나리오 실행 구간)
6. transfer `application.yml:12-17`(Hikari 최대 15, connection-timeout 3000) → 건수 질의가 느려지며 15개가 묶이면 이체 POST(`TransferService.execute`)가 연결을 3초 안에 못 얻어 'HikariPool-1 - Connection is not available, request timed out after 3000ms' → 500. readiness `/actuator/health`(DB 포함)도 시간 초과로 잠깐 빠질 수 있음(`core-banking/k8s/22-transfer-service.yaml:87-93`), liveness 는 livenessState 만 봐 재시작 없음(`:97-103`)
7. account `TransferClient`(read-timeout 15s, 재시도 3, 서킷) → api `AccountClient`(10s, 재시도 3, 서킷) → 이체 502. account 는 `AccountService.validateAndForward`(`AccountService.java:57-80`, 읽기 전용 트랜잭션) 안에서 transfer 를 기다리며 Hikari 연결을 쥐어 잔액 조회 일부도 실패(F42-R, F70-R 과 같은 예상 전파)
8. commerce payment → transfer FQDN(`commerce/payment-service/src/main/resources/application.yml:47-48`) 정산 이체 실패 → checkout 502(F42-R 선례)

### 109 실제 선례 (원칙 9 근거)

F47-R 실행 3d446438(2026-10-12 06:4x~07:0x UTC, `~/dev/testbed-ops/verifications/2026-10-12-0741-F47-R-run-3d446438.md`)에서 동반 부하의 거래 내역 단계(초당 약 3건, api 경유 `page=0&size=20`)가 이 건수 질의를 평시 분당 50~68회에서 분당 약 190회로 늘린 것만으로: Oracle `dpm.oracle.instance.cpu_time` 0.39~0.60 → **1.87~1.93**(상한 2), db_time 0.4 → 12.6~14.9, byb2y2hv7p4ag 실행당 CPU 0.38~0.44 → 0.55~0.61초, DPM 세션 testbed-transfer + byb2y2hv7p4ag `resmgr:cpu quantum` 53건(평시 2시간 0건), transfer 'HikariPool-N - Connection is not available' 726건(평시 0), transfer readiness 'context deadline exceeded' 11회, transfer p95 0.5 → 6.9~9.0초, api 'transferClient is OPEN' 1,899건, commerce checkout 결제 실패 69건(평시 0). 같은 문장, 같은 계획이 이 테스트베드에서 초당 약 3건 늘어나는 것만으로 이체 경로가 실패한다는 직접 증거다. F74-R 은 같은 문장을 초당 8건 더한다.

## 5. 원인 규정 (원칙 5, 6)

- `root_cause.target_id`: `core-banking-transfer`. 결함은 거래 내역 목록 API 가 "한 건이라도 있나" 확인(size=1)에도 Page 건수 질의를 붙이고, 그 질의가 OR-null 술어로 표 전체를 훑는 로직이다. 폴링은 공개 계약(page/size 선택) 안의 정당한 호출이고 빈도도 계약상 제한이 없다. 원칙 6 의 "A 가 정당하고 B 의 로직이 잘못" 경우이며, 원본 Fly.io 도 질의를 첫 일치에서 멈추게 고치고 엔드포인트에 한도를 더했다(같은 층위).
- `trigger_target_id`: 비움. 계기(제휴 연동의 폴링 주기 단축)는 이 테스트베드 부품 밖의 바깥 호출자다(F70-R 과 같은 규정).
- `scoring.partial`: `banking-oracle`(CPU 를 다 쓴 DB, 증상 위치), `core-banking-api`(폴링을 넘기고 재시도로 늘림), `core-banking-account`(이체 실패의 중간 증상).
- 원칙 5: 정답은 "기존 거래 내역 건수 질의의 실행 수가 바깥 목록 호출 급증을 따라 늘어 Oracle CPU 를 다 썼고, 그 질의를 돌리는 transfer 의 풀이 묶여 이체가 실패했다"이며, DPM Top SQL(실행 수, 실행당 읽기), DPM 세션(machine, event), Oracle CPU 지표, transfer Hikari 로그, api 목록 실패 로그로 바로 낼 수 있다. 코드 설계 추론(비멱등, 경쟁 조건)이 필요하지 않다. '건수가 필요 없는 확인에 건수를 센다'는 사실은 피해 설명이다.

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 |
|---|---|
| 감지 | transfer ERROR 'Connection is not available' 급증, api 'Transfer service list call failed', 'transferClient is OPEN', account 'Transfer service call failed', 이체 502 와 commerce checkout 실패, core-banking-transfer·api·account 서버 스팬 오류율과 지연, Oracle CPU(dpm.oracle.instance.cpu_time)·db_time 이상, KCM testbed-transfer Unhealthy(readiness). 별도 업무 규칙 없이 이벤트와 인시던트가 생기는 겉 증상 |
| 피해 판정(러너) | 동반 부하 이체(step transfer) 5xx 비율 ≥ 0.3(평시 0), 3틱. 배제는 부하 미전달과 Oracle NotReady 뿐. 잔액 조회 실패율은 예상 전파라 기록만 한다(F42-R, F70-R 과 같음) |
| RCA | §7 의 순서: 바깥 목록 호출 급증(url.query toAccount, size=1) → byb2y2hv7p4ag 실행 수 급증과 실행당 약 10만 읽기 → Oracle CPU 상한과 resmgr:cpu quantum(machine=transfer 파드) → transfer Hikari 고갈 → 이체·정산 실패. 롤아웃, 설정, DDL, 잠금 대기 없음 |

## 7. 관측 근거 표 (119 실조회, 2026-10-12, 평시)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 결정 증거는 DPM Top SQL(수집기 주기 전수, 분 단위 delta), DPM 세션, VM Oracle CPU 지표, 로그(전수), KCM 이벤트다.

| 증거 | 119 위치 | 실제 조회와 결과 |
|---|---|---|
| 근본: 건수 질의의 실행 수와 실행당 읽기 | CH `dpm_topsql_local`(engine oracle, sql_id, body 의 executions, avgLogicalReads, avgCpuTime, planHashValue) | `SELECT toStartOfHour(timestamp) h, sum(JSONExtractFloat(body,'executions'))/3600, avg(JSONExtractFloat(body,'avgLogicalReads')), quantile(0.5)(JSONExtractFloat(body,'avgCpuTime')) FROM lucida.dpm_topsql_local WHERE engine='oracle' AND sql_id='byb2y2hv7p4ag' AND timestamp > now() - INTERVAL 24 HOUR GROUP BY h` → 초당 0.16~1.25회, 실행당 논리 읽기 101,500~103,063, CPU 중앙값 252~561ms, planHashValue 2603328921 하나. 최근 1시간 Oracle CPU 1위 문장(905 CPU초, 2위 outbox 폴링 128초). 문장 본문은 Top SQL 에 없고 109 v$sql 로 확인: `select count(t1_0.id) from transfers t1_0 where (:1 is null or t1_0.from_account=:2 ) and (:3 is null or t1_0.to_account=:4 ) and (:5 is null or t1_0.status=:6 ) and ...`. 장애 시 같은 sql_id, 같은 계획의 실행 수가 분당 수백 회로 뛴다 |
| 근본: Oracle CPU 포화 | VM `dpm.oracle.instance.cpu_time`(target_id 00149c2f-…, 표본 아님) | `max_over_time(dpm.oracle.instance.cpu_time[1h])` 24시간 → 조용한 시간 0.14~0.65, 다른 시나리오 실행 구간 최대 2.0. 상한 2(cpu_count 2). F47-R 실행에서 같은 문장 증가로 1.87~1.93 |
| 근본: 누가 돌리나 | CH `dpm_session_local`(body 의 machine, sqlId, event) | 7일 sqlId byb2y2hv7p4ag 세션 표본: direct path read 905, On CPU 510, resmgr:cpu quantum 163, machine 모두 testbed-transfer-…(다른 시나리오 구간). 7일 전체 resmgr:cpu quantum 232건. 장애 시 machine=transfer 파드, sqlId=byb2y2hv7p4ag, event resmgr:cpu quantum·On CPU 가 몰린다 |
| 계기: 바깥 목록 호출 모양 | CH `otel_traces_local` span_attributes['url.query'] (표본 10%, 보조) | `SELECT service_name, multiIf(... 'size=1' ..., 'toAccount=' ..., 'page=' ...) k, count() FROM lucida.otel_traces_local WHERE timestamp > now() - INTERVAL 7 DAY AND service_name IN ('core-banking-transfer','core-banking-api') AND span_name='GET /api/transfers' GROUP BY 1,2` → api 18,439건, transfer 18,038건이 모두 `fromAccount=ACC-100x&page=0&size=20`(toAccount 0건, size=1 0건). 장애 시 초당 8건의 10% 인 약 0.8건/초가 `toAccount=…&status=COMPLETED&from=…&page=0&size=1` 로 잡힌다(보조 증거) |
| 계기: 목록 호출 실패 | CH `lucida_logs_local`(전수) | api ERROR 'Transfer service list call failed: …' 1,416건, 'Transfer service circuit open/exhausted: CircuitBreaker 'transferClient' is OPEN …' 16,864건(보존 2026-10-05~, 모두 다른 시나리오 실행 구간). 장애 시 transfer 500('Connection is not available')을 받은 목록 호출 실패와 서킷 열림이 이체 실패와 같은 시각에 남는다 |
| 전파: transfer 풀 고갈 | CH `lucida_logs_local` core-banking-transfer ERROR | 'HikariPool-1 - Connection is not available, request timed out after 3000ms (total=15, active=15, idle=0, waiting=…)' 961건(2026-10-11 17:04~, 모두 다른 시나리오 구간) |
| 전파: readiness | CH `kcm_events_local`(rca-testbed-banking, testbed-transfer, Unhealthy) | 다른 시나리오 구간에 readiness 'context deadline exceeded' 수집 확인(F47-R 실행 11회). 장애 시 Killing 은 없어야 한다(liveness 는 DB 를 보지 않음), ScalingReplicaSet·Pulled 도 없어야 한다 |
| 피해 | CH 로그(account 'Transfer service call failed', api 이체 502, commerce payment 'core-banking transfer call failed'), APM 서버 스팬 오류율 | 같은 경로 오류 형식은 F42-R, F47-R, F70-R 실행에서 수집 확인 |
| 배제: 배포·설정·DB 변경 | CH `kcm_events_local`(ScalingReplicaSet, Pulled), PG kcm_resources_history(ReplicaSet image), DPM 세션의 DDL·enq: TX | 이 주입은 아무것도 바꾸지 않는다. 장애 구간에 testbed-transfer 의 ReplicaSet, 이미지(core-banking-transfer:latest) 그대로, DDL·잠금 대기 없음 |

## 8. 감별

- must_support: 정답지 `must_support` 5개(목록 호출 모양 변화와 건수 질의 실행 수 급증, Oracle CPU 상한과 resmgr:cpu quantum, transfer 풀 고갈과 이체 실패, 롤아웃·DDL·잠금 없음, 폴링이 멈추면 몇 초 안에 회복).
- must_rule_out: 결함 릴리스 질의(F42-R: 새 sql_id, 롤아웃), 바깥 호출의 힙 고갈(F70-R: OOM, liveness 재시작), 내부 백필 Job(F59-P: Job 이벤트, ledger 파드), DB 잠금(F01-P, F08-G), Oracle 계정·자격 증명(F35-R, F54-R), account 릴리스 폭주(F47-R), 진입 트래픽 폭주 일반(F07-H: 입구 전체가 아니라 목록 한 경로만 늘어남).
- contrast_with: F42-R, F70-R, F59-P, F47-R, F33-R.
- F42-R 과 가르는 관측: ① KCM ScalingReplicaSet·새 이미지 유무 ② Top SQL 의 sql_id(F42-R 은 롤아웃 직후 처음 나타난 문장, F74-R 은 평시부터 있던 byb2y2hv7p4ag 의 실행 수만 증가, 계획 같음) ③ 실행 수가 무엇을 따라가나(F42-R 은 이체 수, F74-R 은 목록 호출 수) ④ 표본 트레이스의 url.query.
- F59-P 와 가르는 관측: Job 생성·파드 이벤트 유무, 목록 호출이 api 경유(입구)인지 클러스터 안 Job 파드인지, 정답 위치(내부 배치 작업 대 은행 이체 서비스).

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

- 주입: `load.north_south` primary, `core-banking/loadgen/settlement-arrival-poll.js`, entry 30082, target_rps 5(동반 여정: 이체 50 / 잔액 40 / 계좌 목록 10, 거래 내역 단계 없음), 경사 2m, 유지 13m, 내림 1m(부하 960초 = 레벨 timeout 16m, primary 부하 계약), seed 7474. 폴링: 경사 뒤 constant-arrival-rate 초당 8건(가맹점 계좌 8개 × 1초 주기), 클라이언트 시간 제한 30초, 13분. 스크립트 파라미터 POLL_INTERVAL_SEC 기본 1(예전 버전 60).
- **부하 계산**(Oracle CPU 초/초):
  - 평시: 기준선 거래 내역 건수 질의 0.16~1.25건/초 × 0.25~0.56초 = 0.04~0.7, 그 밖의 Oracle 사용 약 0.1~0.3 → 0.2~1.0(VM 조용한 시간 0.14~0.65).
  - 동반 여정 5rps: 이체 2.5건/초(PK 잠금, 행 쓰기), 잔액 2건/초(PK) → 약 0.1 미만. 거래 내역 단계를 빼서 건수 질의를 보태지 않는다.
  - 폴링 8건/초 × 건수 질의 0.25~0.6초(109 실측 0.53) = **2.0~4.8 CPU**. 합 2.2~5.9 로 상한 2 의 110~295%.
  - api 서킷: transfer 가 500 을 주기 시작하면 api 재시도(최대 3배)가 잠깐 더 얹고, 실패율 50% 에서 서킷이 5초 열려 폴링이 끊긴다. 열린 동안 Oracle 의 줄(풀 15개 × 0.5초 / 2 CPU ≈ 4초)이 빠지고 반열림 3건이 통과하면 닫혀 다시 초당 8건. 닫힌 구간 안에서 풀이 수 초 만에 다시 찬다(처리 능력 2 CPU / 0.55초 ≈ 3.6건/초 < 도착 8건/초). 그래서 피해는 끊기지 않고 들쭉날쭉하다(원본 '간헐적 500').
  - 선례: F47-R 실행에서 같은 문장 +3건/초만으로 Oracle 1.87~1.93, transfer 풀 고갈 726건, commerce 결제 실패 69건(§4). F74-R 은 +8건/초.
- 성공: 동반 부하 이체(step transfer) 5xx ≥ 0.3, 3틱(45초). F42-R 과 같은 문턱(서킷 반열림 주기로 실패율이 30초 창마다 흔들려 0.5 는 과함).
- min_hold 4m: 경사 2분 뒤 폴링 시작, 포화는 수십 초 안에 자리 잡는다. 4분이면 서킷 주기 여러 번을 지난 뒤 판정.
- 배제: achieved_rps < 1.25(동반 5rps 의 1/4), Oracle NotReady. 잔액 조회 실패율은 예상 전파라 배제에 넣지 않는다. 중단: entry_status 0(nginx, api, account 는 살아 502 로 답한다).
- 회복: transfer Ready, available 1, 기준선 이체 5xx < 0.05(2틱), 상한 8m. 폴링이 멈추면 쌓인 건수 질의가 몇 초 안에 끝난다.
- 테스트베드 한계: 입구 요청률은 평시 + 5rps + 8rps. 필수 중단 조건(입구 0, 노드, Oracle 파드)에 걸리지 않는다. Oracle 은 CPU 만 포화하고 메모리, 세션 수(평시 transfer 15 + account 10 + ledger 10 안팎)는 그대로다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Fly.io 2026-04-23, 공식 인프라 기록, 링크)와 요소별 대응표가 있고 기전(외부 폴링 빈도 증가 → 필요 이상 행을 훑는 질의 → 공유 DB CPU 포화 → 같은 DB 쓰는 요청의 간헐적 실패)이 같다 (원칙 1)
- [x] 분류 장부를 갱신했다. 묶음 F 3 → 4(3.6%), 정답 위치 은행 이체 서비스 9 → 10(9.1%), 결제 경로 13/110(11.8%), 서비스 은행 36 → 37(가장 적은 서비스 동률). 어느 축도 20% 아래 (원칙 2)
- [x] 근본 원인 위치: `TransferController.java:51-64`, `TransferService.java:120-127`, `TransferRepository.java:17-28`(Page + OR-null 건수 질의), Oracle `10-oracle.yaml:43-49`(cpu 2000m), 109 v$sql byb2y2hv7p4ag (G1)
- [x] 근본 원인의 흔적: DPM Top SQL(byb2y2hv7p4ag 실행 수, 실행당 읽기), DPM 세션(machine, resmgr:cpu quantum), VM dpm.oracle.instance.cpu_time (원칙 3)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: DPM(수집기 주기 전수), VM DB 지표, 로그 전수, KCM. url.query 트레이스는 보조 (원칙 3)
- [x] 계기의 흔적: 같은 sql_id 실행 수가 목록 호출 수를 따라 급증(평시 초당 0.2~1.2 → 8 이상), 표본 트레이스 url.query 'toAccount=…&size=1'(평시 0건), api 목록 호출 실패·서킷 로그. 인공 지연 없음 (원칙 4)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다(목록 건수 질의 실행 급증 → Oracle CPU 포화 → transfer 풀 고갈) (원칙 5)
- [x] 정답지 세 칸: target_id core-banking-transfer, trigger_target_id 비움(바깥 호출자), partial banking-oracle·api·account (원칙 6)
- [x] 감지, 피해 판정, RCA 증거가 6절에 따로 있다 (원칙 7)
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: 요청 경로와 질의 문자열에 id 없음, k6 태그는 러너 안의 지표에만, User-Agent 는 상주 기준선과 같은 k6 (원칙 8)
- [x] 부하 상한(동반 5rps + 폴링 8rps)과 서킷(api transferClient, account transferClient)을 넣어 피해 계산, 109 실측 질의 비용과 F47-R 실행 선례 (원칙 9)

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- Oracle cpu_time 이 1.8 이상에 붙는지, byb2y2hv7p4ag 실행 수가 분당 수백 회로 뛰는지(서킷 열림 구간에는 줄어든다).
- 동반 부하 이체 5xx 비율이 30초 창마다 0.3 을 넘는지. 서킷 주기로 모자라면 운영 하네스가 POLL_INTERVAL_SEC 를 줄이는 대신 폴링 수를 계약 안에서 올린다(스크립트 기본값 변경).
- transfer readiness 가 빠지는지, liveness 재시작(Killing)이 없는지.
- 표본 트레이스에 url.query 'toAccount=…&page=0&size=1' 이 잡히는지.
