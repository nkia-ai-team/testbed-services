---
title: F35-P 설계 시트 (운영을 가리킨 로컬 마이그레이션이 banking 계좌 표를 지워 잔액 조회, 이체, commerce 정산이 실패)
status: Draft
owner: project
last_reviewed: 2026-10-11
tags:
  - scenario
  - design
  - schema
  - oracle
  - operation
summary: 계좌 표를 바꾸는 기능을 만들던 개발자가 자기 PC 에서 돌린 마이그레이션의 연결이 로컬 DB 대신 운영 banking Oracle 을 가리켜, 마이그레이션의 표 지우기 단계가 살아 있는 BANKING.ACCOUNTS 를 지운다. account 의 잔액 조회, 계좌 목록, 고객 이체 확인과 transfer 의 계좌 잠금(commerce 정산)이 모두 ORA-00942 로 실패하고, 이체 표만 읽는 거래 내역은 그대로 된다.
---

# F35-P 설계 시트

## 1. 요약

banking 의 계좌는 Oracle `BANKING.ACCOUNTS`(2026-10-11 1,001 행: ACTIVE 976, FROZEN 24, CLOSED 1, commerce 정산 계좌 둘 포함)에 있다. 이 표를 읽는 서비스는 둘이다. account-service 는 잔액 조회(`GET /api/accounts/{id}`, nginx → account), 계좌 목록, 고객 이체 앞의 계좌 확인(`POST /api/accounts/transfer`, api → account)에서 읽고, transfer-service 는 모든 이체의 첫 문장으로 두 계좌를 `FOR UPDATE` 로 잠근다(commerce-payment 가 checkout 마다 transfer 로 바로 보내는 정산 이체 포함).

계좌 표를 바꾸는 기능을 만들던 개발자가 자기 PC 에서 마이그레이션을 돌리는데, 명령의 연결이 로컬 DB 가 아니라 운영 banking Oracle 을 가리켜 마이그레이션의 표 지우기 단계가 살아 있는 ACCOUNTS 를 지운다. 그 순간부터 이 표를 부르는 모든 문장이 `ORA-00942: table or view "BANKING"."ACCOUNTS" does not exist` 로 실패한다. 잔액 조회와 계좌 목록은 500, 고객 이체는 account 의 첫 계좌 조회에서 500(api 재시도 뒤 502), commerce 정산은 transfer 의 첫 계좌 잠금에서 500 이라 commerce checkout 이 502 다. 이체 표만 읽는 거래 내역은 같은 Oracle 에서 200 으로 계속 답하고, 파드와 풀은 멀쩡하다.

비유: 은행 지점에서 누군가 연습용 장부철을 버리려다 실제 고객 원장철을 버렸다. 창구 직원(account, transfer)은 그대로 출근해 있고 전산(Oracle)도 켜져 있지만, 잔액을 물어도, 송금을 하려 해도 "그 장부가 없습니다" 라는 답만 나온다. 송금 내역철(TRANSFERS)은 남아 있어 내역 조회만 된다.

## 2. 원본 사례

- 기업: Resend (메일 발송 API)
- 날짜: 2024-02-21 (UTC 04:56 마이그레이션 시작, 17:05 해결)
- 링크: [Incident report for February 21, 2024 (공식 블로그)](https://resend.com/blog/incident-report-for-february-21-2024) (자료 문서 `ref-real-world-incidents.md` M22 에 있음)
- 요약(출처가 말한 것만): 기능을 만들던 엔지니어가 로컬 환경에서 데이터베이스 마이그레이션을 돌렸는데 그 명령이 운영 환경을 가리켜 운영의 모든 표를 지웠다("incorrectly pointed to the production environment instead, which dropped all tables in production"). 04:57 운영에서 표가 지워지는 것을 알아챘고 백업 복원으로 17:02 API 요청 수락을 재개했다. 약 12시간 메일 발송, API, 대시보드를 쓰지 못했다.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 기능을 만들던 엔지니어가 로컬 환경에서 데이터베이스 마이그레이션 실행 | 계좌 표를 바꾸는 기능을 만들던 개발자가 자기 PC 에서 마이그레이션 실행 |
| 원인이 된 결함 | 명령이 로컬이 아니라 운영 환경을 가리켜 운영의 모든 표를 지움 | 명령의 연결이 로컬 DB 가 아니라 운영 banking Oracle 을 가리켜 표 지우기 단계가 운영 `BANKING.ACCOUNTS` 를 지움(그 기능이 바꾸는 표 하나로 규모를 줄임) |
| 전파 경로 | 표가 없어 API 가 요청을 받지도 저장하지도 못함 | account 의 `Account` 엔티티와 transfer 의 계좌 잠금이 여전히 `accounts` 를 부름 → ORA-00942 → 잔액 조회, 계좌 목록 500, 고객 이체 account 500 → api 502, commerce 정산 transfer 500 → commerce-payment 502 → commerce checkout 502 |
| 사용자 증상 | 메일 발송, API, 대시보드 약 12시간 전면 불가 | banking 잔액 조회, 계좌 목록, 송금 100% 실패, commerce 구매 100% 실패. 거래 내역은 정상 |
| 탐지된 경로 | 1분 뒤 운영에서 표가 지워지는 것을 알아챔 | banking account, api, transfer 와 commerce order, payment 오류율, 새 오류 로그(ORA-00942 ERROR, Servlet ERROR, 서킷 열림, 'Checkout payment failed') 급증 → 119 이상 탐지 → 인시던트 |
| 완화와 복구 | 백업에서 복원(두 번째 시도에 성공), 5분 기록 손실 | cleanup 이 `FLASHBACK TABLE … TO BEFORE DROP` 으로 되돌림(행 그대로, 즉시 회복). 원본의 백업 복원 몇 시간은 재현하지 않는다 |

기전은 원본과 같다: "개발 중 로컬에서 돌린 마이그레이션 명령이 운영을 가리켜 운영의 표를 지움 → 앱이 그 표를 찾지 못해 그 표를 쓰는 요청이 모두 실패 → 표를 되살리면 회복". 우리 스택에 맞춘 것은 규모(표 하나)와 엔진(Oracle)이다.

재구성은 F35-H 와 같은 실제 DROP 이다. 109 는 `recyclebin=on` 이라 `DROP TABLE BANKING.ACCOUNTS`(PURGE 없음)는 표, 기본 키 인덱스, 행 트리거를 휴지통으로 옮기고 그 공간을 빈 공간으로 센다. cleanup 은 `FLASHBACK TABLE … TO BEFORE DROP` 으로 셋을 되돌린 뒤 BIN$ 이름으로 돌아온 인덱스, 제약, 트리거에 원래 이름을 다시 붙인다. F35-H 의 표에는 트리거가 없어 기존 모드는 트리거를 다루지 않았다. 그래서 db.ddl Oracle 표 지우기 모드에 선택 계약 `triggers`(사건:시점:상태=이름)를 더했다. F35-H 는 이 인자를 넘기지 않아 스크립트와 인자가 그대로다(기존 테스트와 새 테스트 `test_oracle_dropped_accounts_table_brings_its_row_trigger_back` 가 확인).

관계와 id: 겉 증상(banking 이체와 commerce 정산 실패, banking 로그의 Oracle 오류)이 F35 사례군(F35-R 계정 잠금, F35-H 이체 표 지우기)과 일부 같고, 실패하는 경로와 성공하는 경로가 다르므로 F35 의 P 로 둔다. F35-H 와 원본, 도구가 같지만 사라진 표, 실패하는 서비스(account 대 transfer), 성공하는 경로(거래 내역 대 잔액 조회), 오류 문장(ORA-00942 ACCOUNTS 대 ORA-04043/ORA-00942 TRANSFERS)이 서로 반대라 카탈로그 §1 의 "서비스 이름만 바꾼 복제" 가 아니다. 같은 원본의 다른 표 변형(F53-R orders, F53-P payments, F32-P dispatches, F23-P products)과 같은 방식이다. 같은 수단(db.ddl)이지만 파라미터(표, 인덱스, 제약, 트리거)가 달라 같은 주입 중복이 아니다.

## 3. 숫자 근거 (2026-10-11, `scenario-stats.py`, 정식 + 후보 합계 98, 이 후보 전)

- 서비스(정식 + 후보): commerce 33, core-banking 32, food-delivery 33. **은행이 가장 적다**(원칙 2). commerce 와 food 는 동률 최다라 정답 위치 0 부품만 낼 수 있다.
- 묶음(정식 + 후보, 20% = 19.6): J 16, D 14, G 12, P 12, L 8, A 7, B 7, C 6, E 4, F 3, H 2, K 2, Q 2, I 1, M 1, O 1, N 0. 이 후보의 주 묶음 P 는 12 → 13(99 중 13.1%), 상한 아래.
- 정답 위치(정식 + 후보): DB 테이블(은행 계좌) 4(F01-P, F08-G, F15-G 잠금, F56-R 행 삭제) → 5(5.1%), 상한 아래. 표가 사라지는 고장 꼴은 이 위치에 처음이다.
- 결제 경로: 13/98(13.3%) → 13/99, 이 후보와 무관(정답이 banking 계좌 표).
- 장부와 catalog 대조 어긋남 없음.

### 3단계 후보 목록 (실제 기전 × 부품, 숫자 순으로 줄 세움, 버린 이유 포함)

banking(가장 적은 서비스)을 먼저 보고, 은행 쪽 부품(api, account, transfer, ledger, Oracle, Kafka, nginx)과 인프라 층을 고루 짚었다.

| # | 기전 × 부품 | 원본 사례 | 결과 |
|---|---|---|---|
| 1 | M22 운영 명령 대상 착오 × banking Oracle ACCOUNTS 표 | Resend 2024-02-21 공식 | **채택**(F35-P) |
| 2 | M2 결함 릴리스(풀 크기 계산 결함) × banking transfer Hikari 풀 | GitLab 2021-07-27 공식 production#5238(리팩터가 동적 풀 크기를 덮어 기본값 5) | 버림, 원칙 9: banking 사용자 경로의 동시 연결 수요가 1 안팎(POST 5ms, 잔액 1ms, 119 트레이스)이라 5 로도 마르지 않고, 거래 내역(255ms 전수 스캔) 급증은 Oracle CPU 2코어가 먼저 찬다. rejected 13 의 벽과 같다 |
| 3 | M8 엄격 스키마 릴리스 × banking transfer(commerce 정산) | Buttondown 2026-06-14 공식 | 버림, 원칙 1·9: commerce 정산 요청(BankingTransferClient)이 선언된 필드만 보내 거절이 생기지 않고, 고객 이체는 4xx 단일 신호(F34-R 교훈) |
| 4 | M11 비싼 헬스체크를 더한 릴리스 × banking transfer readiness | GitLab 2020-10-28 production#2918(헬스체크가 /metrics 를 불러 긴 질의가 쌓임) | 버림, 원칙 1: 스테이징 사고이고, 기본 롤링 서비스는 옛 파드가 남아 피해가 없음(rejected 79 벽) |
| 5 | M13 승인 정책 × banking 네임스페이스(PodSecurity enforce + 롤아웃) | 공식 사후 보고 없음(웹 검색 1회: Kyverno 블로그, 교육 자료뿐) | 버림, 원칙 1, F50-R(ResourceQuota + 롤아웃) 꼴 |
| 6 | M11 헬스체크가 의존을 반영해 용량 제거 × banking account readiness | Demandbase 상태 페이지(배포의 DB 갱신이 잠금 + 헬스체크 반영) | 버림, 원칙 1: 배포 + DB 잠금 복합 계기 |
| 7 | M22 행 단위 보안 정책(VPD) 오적용 × banking ACCOUNTS | 공식 사후 보고 없음 | 버림, 원칙 1, 고객 경로는 4xx 'Account not found' 단일 신호 |
| 8 | 무효 트리거(ORA-04098) × banking ACCOUNTS 갱신 | 공식 사후 보고 없음(rejected 236 의 GoCardless 판은 다른 벽) | 버림, 원칙 1 |
| 9 | 제약 추가 마이그레이션(order_id NOT NULL) × banking TRANSFERS | Onfido 2024-10-18 꼴 | 버림, 지휘 세션 지시: "스키마-코드 순서" 꼴은 사람 결정 전까지 내지 않음 |
| 10 | M10 소비자 그룹 이름 변경으로 재처리 × banking ledger | 공식 사후 보고 없음(LinkedIn 2016 은 rejected 140) | 버림, 원칙 7(사용자 경로 밖), 원칙 9 |
| 11 | M22 Cassandra 새 노드가 빈 읽기 × 은행 원장 | Monzo 2019-07-29 | 버림, 원칙 1: 복제 토폴로지 없음 |

## 4. 인과 사슬 (코드와 인프라 위치)

1. 계기: `DROP TABLE BANKING.ACCOUNTS`(PURGE 없음) — 표, PK 인덱스 `SYS_C008651`, 트리거 `TRG_ACCOUNTS_UPDATED_AT` 가 휴지통으로(infra: testbed-oracle-0, PDB FREEPDB1, USERS).
2. 잔액 조회: `core-banking/account-service/src/main/java/com/corebanking/account/controller/AccountController.java:24-27 (getAccount)` → `AccountService.java:34-44 (getAccount, findById)` → ORA-00942 → `GlobalExceptionHandler.java:9-20`(ServiceException 만 처리) → Spring 기본 500, `Servlet.service() ... InvalidDataAccessResourceUsageException` ERROR.
3. 계좌 목록: `AccountService.java:46-51 (search)` 같은 500.
4. 고객 이체: api `ApiService.submitTransfer` → `api/client/AccountClient.java:28-58 (requestTransfer, 재시도 3, 서킷)` → account `AccountService.java:57-81 (validateAndForward)` 첫 `findById` 에서 ORA-00942 → 500 → api 502 'Account service call failed for order null: 500', 서킷 열림. transfer 는 불리지 않는다.
5. commerce 정산: `commerce/payment-service/.../client/BankingTransferClient.java:37-59` → transfer `TransferService.java:52-71 (execute, findByIdForUpdate :69)` → `transfer/repository/AccountRepository.java:18-20 (PESSIMISTIC_WRITE)` → ORA-00942(`... for update`) → 500 → commerce-payment 502 → commerce-order 'Checkout payment failed, releasing reserved stock'.
6. 무영향: 거래 내역 `api → transfer GET /api/transfers`(TRANSFERS 만), ledger(LEDGER_ENTRIES), outbox 릴레이. health 는 연결 `isValid()` 라 account, transfer Ready(`core-banking/k8s/21-account-service.yaml:67-87`).

### 로컬 실측 (Oracle 23.26.2, gvenzl/oracle-free:23-slim = 109 와 같은 버전, 2026-10-11)

- `core-banking/db/init.sql` 로 스키마를 만들고 계좌를 1,004 행으로 채운 뒤, 현재 main 의 account-service, transfer-service jar 를 로컬에서 띄움.
- executor 원격 스크립트(kubectl 을 docker exec 로 바꾼 shim)로 run: 1.7초(확인 포함). 이후 잔액 조회 500 ×3(0.012~0.057초), account 이체 500(0.017초), transfer 직접(정산 경로) 500(0.039초), 계좌 목록 500, **거래 내역 200**, account, transfer `/actuator/health` UP.
- 로그(그대로): account `WARN SqlExceptionHelper : SQL Error: 942, SQLState: 42000`, `ERROR SqlExceptionHelper : ORA-00942: table or view "BANKING"."ACCOUNTS" does not exist` 와 `https://docs.oracle.com/error-help/db/ora-00942/`, `ERROR ... Servlet.service() ... InvalidDataAccessResourceUsageException: JDBC exception executing SQL [select a1_0.id,a1_0.balance,a1_0.holder,a1_0.status from accounts a1_0 where a1_0.id=?]`. transfer 는 같은 오류에 `[... where a1_0.id=? for update]`.
- USERS used(DBA_TABLESPACE_USAGE_METRICS × 8192): 1,638,400 → 1,376,256(−262,144) → cleanup 뒤 1,638,400.
- cleanup: FLASHBACK 뒤 BIN$ 이름의 인덱스, 제약 넷, 트리거를 원래 이름으로, 두 번 돌려도 0(멱등), recovery 0. 트리거 `TRG_ACCOUNTS_UPDATED_AT` ENABLED, VALID, 다음 이체에서 두 계좌 updated_at 이 그 시각으로 바뀜. 잔액 200, 이체 200.
- preflight 는 로컬 USERS 빈 공간이 1024MB 미만이라 실패(의도대로). 109 USERS 빈 공간 5482MB.

## 5. 원인 규정

- `root_cause.target_id`: `banking-oracle:BANKING.accounts` (F01-P, F35-H 와 같은 '인스턴스:스키마.테이블' 표기). 결함을 가진 곳은 잘못된 환경을 가리킨 마이그레이션이 지운 표다. 고치려면 표를 되살려야 한다.
- `trigger_target_id`: 비움. 근본과 계기가 같은 곳(이 표에 대한 잘못된 대상의 마이그레이션)이다.
- `scoring.partial`: `banking-account`, `core-banking-account`(오류를 가장 많이 내는 증상 서비스), `banking-oracle`(표는 맞히지 못하고 DB 만), `banking-transfer`, `commerce-payment`(정산 실패를 본 쪽).
- 원칙 6: account, transfer, api, commerce-payment 는 늘 하던 요청을 보냈고(정당한 요청) 결함 있는 곳(지워진 표)에서 실패했다. 원본의 포스트모템도 "마이그레이션이 운영을 가리켜 표를 지움" 을 원인으로 지목한다.
- 원칙 5: 정답은 오류 문장(표 이름)과 같은 분 테이블스페이스 감소, 다른 변경 부재로 관제 데이터에서 바로 낼 수 있다. 누가 어느 PC 에서 돌렸는지는 요구하지 않는다.

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 |
|---|---|
| 감지(lucida-next) | core-banking-account, api, transfer 의 오류율과 ERROR 로그 급증(평시 account Servlet 오류 0), commerce order, payment 오류율, checkout 실패 로그 |
| 피해 판정(러너) | 동반 부하 잔액 조회(step get) 실패율 ≥ 0.5, 이체(step transfer) 5xx ≥ 0.5, 3틱 |
| RCA | ORA-00942 가 ACCOUNTS 를 지목(account, transfer 로그 전수), 같은 분 USERS used −262,144 바이트(DPM, 평시 감소 0), 거래 내역 200 계속, KCM 에 롤아웃·스케일·재시작 없음, 파드 Ready, 풀 대기 없음 |

## 7. 관측 근거 표 (119 실조회, 평시)

트레이스(10% 표본)와 APM 지표는 보조로만 쓰고, 핵심 증거는 로그 전수와 DPM 지표다.

| 증거 | 119 위치 | 조회와 결과(2026-10-11) |
|---|---|---|
| ORA-00942 평시 0 | CH `lucida.lucida_logs_local` body | `SELECT service_name, count() FROM lucida_logs_local WHERE service_name LIKE 'core-banking-%' AND body LIKE '%ORA-00942%' GROUP BY 1` → 0행(로그 보존 시작 2026-10-04 20:00 UTC, banking 로그 298만 행) |
| SqlExceptionHelper 문장이 수집됨 | 같은 표 | 2026-10-09 F35-R 실행 때 account 'SQL Error: 28000, SQLState: 99999' 443건, 'ORA-28000: The account is locked; login denied.\n\nhttps://docs.oracle.com/error-help/db/ora-28000/' 443건(같은 형식으로 ORA-00942 가 남는다) |
| account Servlet 오류 평시 0 | 같은 표 | 일별 `countIf(body LIKE 'Servlet.service()%')` service_name='core-banking-account': 10-04~10-08, 10-10 모두 0, 장애 실행이 있던 10-09(443), 10-11(2,504)만 0 아님 |
| 잔액 조회 경로 존재와 양 | CH `otel_traces_local` 표본 | 최근 1시간 account SERVER `GET /api/accounts/{id}` 194건(10% 표본, 실제 약 1,940/시), `POST /api/accounts/transfer` 48, `GET /api/accounts` 34 |
| 고객 이체 흐름 | 로그 전수 | 최근 1시간 api 'Submitting transfer ... orderId=null' 383, account 'Account validation passed' 379, transfer 'Transfer COMPLETED' 계속(commerce-settlement 포함) |
| 거래 내역 경로(성공해야 할 것) | 트레이스 표본 | transfer SERVER `GET /api/transfers` 99건/시(표본), p50 255ms. 이 경로의 SQL 은 `SELECT freepdb1.transfers` 만 |
| 계기 흔적: 테이블스페이스 | VM `dpm.oracle.tablespace.used{tablespace="USERS"}` (1분) | 현재 5,535,301,632B(109 `used_space×8192` 와 같음). 2026-10-04 21:43 ~ 2026-10-11 의 10,080분 동안 분 단위 감소 0번, 증가 계단은 8MB 이상(2일 창 11번). 지우기는 −262,144B(로컬 실측). 2026-10-15 정정: 위 10,080분(7일) 기록과 현재값은 109 당시 측정 기록이고 기준이 아니다. 66 이전(앱 DB 논리 덤프)으로 테이블스페이스 물리 크기와 이력이 이어지지 않는다. 기준은 녹화 평시 구간(주입 전 2시간) 동안 이 계열이 줄지 않는다(할당으로 늘어나는 계단만)는 것이다(근거 `~/dev/testbed-ops/verifications/2026-10-15-0037-F35-H-run-a1206bfd.md`: 같은 Oracle USERS 의 주입 직전 값이 설계 당시 절대값과 달랐다) |
| cross-domain 문장 | 로그 전수 | commerce-order 'Checkout payment failed, releasing reserved stock: ...' 가 수집됨(3일 수천 건, 다른 실행) |
| 풀 상태(정상이어야 할 것) | VM `db.client.connections.usage/max` | transfer max 15, account max 10, 최근 1시간 used 최대 2 |

계기 자체(DDL 문장)는 Top SQL 에 남지 않고 표 목록 지표가 없다(F35-H 시트와 같은 확인). 그래서 계기의 흔적은 표 이름을 가리키는 오류 문장, 그 분의 유일한 테이블스페이스 감소, 같은 시각 KCM 의 변경 부재다.

## 8. 감별

- must_support: account, transfer 의 ORA-00942(ACCOUNTS), 같은 분 USERS used −262,144B, 잔액 조회 500과 이체 5xx, api 서킷, commerce checkout 실패, 거래 내역 200, KCM 변경 없음과 파드 Ready.
- must_rule_out(정답지): F35-H(이체 표: 거래 내역 실패, ORA-04043, 잔액 정상), F35-R(계정 잠금: ORA-28000, 풀 비고 readiness 빠짐, 거래 내역도 실패), F51-R(account replicas 0: 연결 거절, commerce 정산 성공), F56-R(정산 계좌 두 행 삭제: 정산만 400), account 새 버전(F41-R, F51-H, F41-H, F39-H: 이미지 변경), Oracle 다운·포화(F25-H 꼴)나 행 잠금(F01-P: 기다림과 차단 세션).
- contrast_with: F35-H, F35-R, F51-R, F56-R.
- 같은 증상(이체, commerce 정산 502)에서 정답이 다른 이웃이 여럿이라 관제 AI 는 "어느 표가 사라졌나"(오류 문장의 표 이름)와 "무엇이 아직 되나"(거래 내역 대 잔액)를 봐야 맞힌다.

## 9. 러너 판정 조건과 강도, 부하

- 강도: 축이 없다. 표가 없으면 그 표를 부르는 문장은 요청량과 무관하게 100% 실패한다(원칙 9 계산이 자명). 설계 강도 1단 고정 evaluation(`approved-fixed-f35-p`, 파라미터는 profiles.json `scenario_parameters["F35-P"]` 와 같음).
- 동반 부하: `load.north_south` banking `transfer-heavy-surge.js`, target_rps 5(초당 잔액 조회 약 1.75, 이체 2, 거래 내역 0.75, 목록 0.5), entry 30082, hold 21m, seed 3538. 시간대와 무관하게 실패 표본을 보장하려는 것. 상주 commerce 기준선이 checkout(정산) 실패를 만든다.
- success(all, 3틱): `balance_read_nonok_rate ≥ 0.5`, `transfer_5xx_rate ≥ 0.5`. 기대값 약 1.0.
- must_rule_out(any, 2틱): `achieved_rps < 1.25`, Oracle 파드 NotReady, transfer 파드 NotReady. account 파드 Ready 와 재시작 수는 러너 관측 허용 목록(`live_probes.py`)에 banking account 대상이 없어 러너 조건으로 두지 않고(러너 코드 변경 없이), 검증에서 KCM 으로 본다.
- abort: `entry_status == 0`(nginx 나 노드가 죽었을 때만. api 는 502 로 답해 0 이 아님).
- recovery(all, 2틱, 10m): target_health 200, Oracle·transfer Ready, 기준선 잔액 조회 실패율 < 0.05, 기준선 이체 5xx < 0.05.
- min_hold 15m, timeout 20m, max_injection_duration 25m, cleanup 필수.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Resend 2024-02-21 공식 블로그)와 요소별 대응표가 있고 기전이 같다(원칙 1) — §2
- [x] 분류 장부를 갱신했고 묶음 P 12→13(13.1%), 정답 위치 DB 테이블(은행 계좌) 4→5(5.1%), 결제 경로 13(13.1%), 서비스 은행 32(가장 적음)→33. 어느 축도 20% 아래(원칙 2) — §3, 장부 §4-1, §6
- [x] 근본 원인 위치가 인프라 지점(testbed-oracle-0 BANKING.ACCOUNTS, SYS_C008651, TRG_ACCOUNTS_UPDATED_AT)과 코드 `file:line` 으로 확인됨(G1) — §4
- [x] 근본 원인의 흔적이 119 실데이터에서 조회됨: 로그 표의 SqlExceptionHelper 문장 수집, ORA-00942 평시 0, DPM 테이블스페이스 계열(원칙 3) — §7
- [x] 핵심 증거가 표본 데이터에만 있지 않다: 로그 전수와 DPM 1분 지표(원칙 3)
- [x] 계기의 흔적이 있다: 표 이름을 가리키는 오류 문장, 같은 분의 유일한 테이블스페이스 감소. 인공 지연 없음(원칙 4)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다(원칙 5) — §5
- [x] 정답지 세 칸이 원칙 6대로(근본 = 표, 계기 비움, 부분 점수 = 증상 서비스와 DB) — §5
- [x] 감지, 피해 판정, RCA 증거를 각각 적음(원칙 7) — §6
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: sqlplus 는 DB 파드 안 SYSDBA, 문장에 id 없음, 테스트가 argv 와 스크립트에 id 가 없음을 확인(원칙 8)
- [x] 부하 상한과 서킷을 고려해 피해가 실제로 난다: 실패가 결정적(100%)이라 서킷이 열려도 502 로 실패가 이어지고, 동반 부하 5rps 는 상한 180 안(원칙 9) — §9
