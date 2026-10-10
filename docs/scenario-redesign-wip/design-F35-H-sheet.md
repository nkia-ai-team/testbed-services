---
title: F35-H 설계 시트 (운영을 가리킨 로컬 마이그레이션이 banking 이체 표를 지워 이체, 거래 내역, commerce 정산이 실패)
status: Draft
owner: project
last_reviewed: 2026-10-10
tags:
  - scenario
  - design
  - schema
  - oracle
  - operation
summary: 이체 표를 바꾸는 기능을 만들던 개발자가 자기 PC 에서 돌린 마이그레이션의 연결이 로컬 DB 대신 운영 banking Oracle 을 가리켜, 마이그레이션의 표 지우기 단계가 살아 있는 BANKING.TRANSFERS 를 지운다. transfer-service 의 이체가 transfers INSERT 에서 ORA-04043, 거래 내역이 ORA-00942 로 실패해 account, api 가 502, commerce checkout 의 정산 이체도 502 가 된다. 잔액 조회, 계좌 목록, 원장, Oracle, 파드는 정상이다. 원본은 Resend 2024-02-21 로컬에서 돌린 마이그레이션 명령이 운영을 가리켜 운영의 모든 표를 지운 장애(F53-R 과 같은 원본의 Oracle 판). 겉 증상은 F35-R(계정 잠금), F01-P(정산 계좌 잠금)와 같고 정답이 다르다.
---

# F35-H 설계 시트

## 1. 요약

banking 의 transfer-service 는 모든 이체를 Oracle `BANKING.TRANSFERS`(2026-10-10 통계 6,190,576 행, identity 키, 유일 `transfer_ref`, 인덱스 다섯)에 둔다. 이 표를 읽고 쓰는 서비스는 transfer 하나다(저장소 grep, `dba_dependencies` 0건). 이체 표를 바꾸는 기능을 만들던 개발자가 자기 PC 에서 마이그레이션을 돌리는데, 명령이 쓰는 연결이 로컬 DB 가 아니라 운영 banking Oracle 을 가리키고 있었다. 마이그레이션의 표 지우기 단계가 운영의 살아 있는 `TRANSFERS` 를 지운다.

그 순간부터 이 표를 부르는 모든 문장이 실패한다. 이체(`POST /api/transfers`)는 banking 고객 쪽(nginx → api → account → transfer)이든 commerce checkout 의 정산(commerce-payment → transfer)이든 두 계좌를 `FOR UPDATE` 로 잠근 뒤 transfers INSERT 에서 `ORA-04043: Object transfers does not exist.` 로 실패한다(Oracle 드라이버가 생성 키를 돌려받으려고 표를 확인하는 단계). 트랜잭션이 되돌려져 잔액은 움직이지 않고, transfer 는 수십 ms 안에 500 으로 답한다. account 는 재시도 뒤 서킷을 열고 502, api 도 502, commerce-payment 는 정산 실패를 502 로 바꿔 commerce checkout 이 실패하고 예약 재고를 풀어 준다. 거래 내역(`GET /api/transfers`)은 `ORA-00942: table or view "BANKING"."TRANSFERS" does not exist` 로 실패해 api 가 502 다. 1분마다 도는 transfer 보존 정리 배치도 같은 오류로 WARN 을 남긴다.

이 표를 부르지 않는 것은 같은 Oracle 에서 그대로다: 잔액 조회와 계좌 목록(nginx → account), 원장, outbox 릴레이. 롤아웃도 재시작도 설정 변경도 없고, Oracle 은 떠 있으며, transfer 는 Ready 다(health 확인이 연결 `isValid()`). Hikari 풀도 비지 않는다.

비유: 은행 지점의 "송금 장부" 캐비닛을, 본사 직원이 연습용 사무실 것을 치운다는 게 실제 지점 것을 치웠다. 창구 직원은 손님 두 계좌를 확인하고(잠금) 장부에 적으려는 순간 "장부가 없다" 며 송금을 거절한다. 잔액 조회 창구는 계좌 원장만 보므로 멀쩡하다. 이 지점에 송금을 맡기던 쇼핑몰도 결제 정산이 막혀 판매를 못 한다.

## 2. 원본 사례

- 기업: Resend (메일 발송 API)
- 날짜: 2024-02-21 (UTC 04:56 마이그레이션 시작, 17:05 해결)
- 링크: [Incident report for February 21, 2024 (공식 블로그)](https://resend.com/blog/incident-report-for-february-21-2024) (자료 문서 `ref-real-world-incidents.md` M22 에 있음, F53-R 이 추가)
- 요약(출처가 말한 것만): 기능을 만들던 엔지니어가 로컬 환경에서 데이터베이스 마이그레이션을 돌렸는데 그 명령이 운영 환경을 가리켜 운영의 모든 표를 지웠다("incorrectly pointed to the production environment instead, which dropped all tables in production"). 04:57 운영에서 표가 지워지는 것을 알아챘고 05:01 백업 복원을 시작했다. 첫 복원은 잘못된 백업 시각 선택으로 실패했고, 더 오래된 백업에서 다시 복원해 17:02 API 요청 수락을 재개했다. 약 12시간 모든 사용자가 메일 발송, API, 대시보드를 쓰지 못했고("no API requests were being accepted and no data was being stored"), 마이그레이션 직전 5분의 기록을 잃었다.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 기능을 만들던 엔지니어가 로컬 환경에서 데이터베이스 마이그레이션 실행 | 이체 표를 바꾸는 기능을 만들던 개발자가 자기 PC 에서 마이그레이션 실행 |
| 원인이 된 결함 | 명령이 로컬이 아니라 운영 환경을 가리켜 운영의 모든 표를 지움 | 명령의 연결이 로컬 DB 가 아니라 운영 banking Oracle 을 가리켜 표 지우기 단계가 운영 `BANKING.TRANSFERS` 를 지움(그 기능이 바꾸는 표 하나로 규모를 줄임) |
| 전파 경로 | 표가 없어 API 가 요청을 받지도 저장하지도 못함 | transfer 의 `Transfer` 엔티티가 여전히 `transfers` 에 씀 → INSERT ORA-04043, 조회 ORA-00942 → 이체 500 되돌림 → account, api 502 → commerce-payment 정산 502 → commerce checkout 502. 거래 내역 502, 보존 정리 배치 실패 |
| 사용자 증상 | 메일 발송, API, 대시보드 약 12시간 전면 불가 | banking 송금과 거래 내역 100% 실패, commerce 구매 100% 실패. 잔액 조회와 계좌 목록은 정상 |
| 탐지된 경로 | 1분 뒤 운영에서 표가 지워지는 것을 알아챔 | banking api, account, transfer 와 commerce order, payment 오류율, 새 오류 로그(ORA-04043, ORA-00942 ERROR, Servlet ERROR, 서킷 열림, 'Checkout payment failed') 급증 → 119 이상 탐지 → 인시던트 |
| 완화와 복구 | 백업에서 복원(두 번째 시도에 성공), 5분 기록 손실 | cleanup 이 표 이름을 되돌림(행 그대로, 즉시 회복). 원본의 백업 복원 몇 시간은 재현하지 않는다 |

기전은 원본과 같다: "개발 중 로컬에서 돌린 마이그레이션 명령이 운영을 가리켜 운영의 표를 지움 → 앱이 그 표를 찾지 못해 그 표를 쓰는 요청이 모두 실패 → 표를 되살리면 회복". 우리 스택에 맞춘 것은 규모(표 하나)와 엔진(Oracle)이다.

재구성은 실제 DROP 이다. 109 는 `recyclebin=on` 이라 `DROP TABLE BANKING.TRANSFERS`(PURGE 없음)는 표를 스키마에서 빼 휴지통으로 옮기고, 공간을 바로 풀지 않되 빈 공간으로 센다([Oracle 관리 안내서 "Using Flashback Drop and Managing the Recycle Bin"](https://docs.oracle.com/cd/B28359_01/server.111/b28310/tables011.htm), [RECYCLEBIN 매개변수](https://docs.oracle.com/en/database/oracle/oracle-database/19/refrn/RECYCLEBIN.html)). 그래서 원본처럼 표가 사라진 흔적이 DB 쪽 지표에도 남는다: DPM 이 1분마다 보고하는 `dpm.oracle.tablespace.used{tablespace="USERS"}`(DBA_TABLESPACE_USAGE_METRICS)가 표와 인덱스 크기(784+1600MB)만큼 떨어진다(§7). 원본의 백업 복원 대신 cleanup 은 `FLASHBACK TABLE … TO BEFORE DROP` 으로 행, 인덱스, 제약, identity 시퀀스를 되돌리고, 휴지통 이름(`BIN$…`)으로 돌아온 인덱스 일곱과 제약 여덟에 계약에 고정한 109 실측 이름(덮는 열과 제약 종류로 짝지음)을 다시 붙인다. 상태 파일이 필요 없다. preflight 는 휴지통 켜짐, 계약과 같은 인덱스와 제약 이름, 휴지통에 이 표의 사본 없음, USERS 빈 공간 2048MB 이상(창 동안 DB 가 공간 압박으로 휴지통의 표를 거둬 가지 않게, 109 실측 5602MB)을 확인한다.

(1차 평가 FAIL 반영, 2026-10-10: 처음 설계는 같은 스키마 안 RENAME 이었다. 평가가 실제 DROP 은 USERS used 를 크게 떨어뜨리는데 RENAME 은 그러지 않아 원본의 계기 흔적을 지운다는 것을 로컬 실측으로 보였고, 맞다. 로컬 재실측: DROP 뒤 USERS used 86MB → 0, FLASHBACK 뒤 86MB.)

관계와 id: 원본과 도구(표 지우기)는 F53-R, F53-P 와 같은 F53 사례군이지만 그 사례군의 R, P 는 이미 쓰였다. 겉 증상(banking 이체와 거래 내역 실패, commerce 정산 실패, banking 로그의 Oracle 오류)이 F35-R(애플리케이션 계정 잠금)과 같고 정답이 다르므로 F35 사례군의 H 로 둔다. F53-R 과는 DB 엔진(MySQL → Oracle), 진입 경로(food 주문 → banking 이체와 commerce checkout), 영향 범위(도메인을 넘음)가 달라 카탈로그 §1 의 "서비스 이름만 바꾼 복제" 가 아니다. 같은 수단(db.ddl)이지만 새 모드(Oracle 휴지통 DROP 과 FLASHBACK)와 새 파라미터라 같은 주입 중복도 아니다.

## 3. 숫자 근거 (2026-10-10, `scenario-stats.py`, 정식 + 후보 합계 66, 이 후보 전)

| 축 | 값 | 판단 |
|---|---|---|
| 서비스 | 쇼핑몰 26, 은행 20, 음식배달 20 | 은행과 음식배달이 함께 가장 적다 → 은행(20→21) |
| 묶음 | J 8(12%), A, B, D, G 7(각 10.6%), C, L, P 6(각 9.1%), F 3, E, H, K 2, I, M, O 1, N 0 | P(운영 작업의 대상 착오) 6→7(10.4%). 결함이 앱이 기대하는 표가 없는 꼴로 드러나 F53-R 처럼 P+L. L 로 세도 6→7 |
| 정답 위치 | DB 테이블(이체) 0 | §2-1 'DB 테이블(<무엇>)' 꼴, 0→1(1.5%). 은행 이체 서비스(6)가 아니다 |
| 결제 경로 합계 | 11(16.7%) | 결제가 정답이 아니므로 그대로(11/67, 16.4%) |
| 어느 축이든 | 최대는 J 8/67(11.9%), 주문 서비스 7/67(10.4%) | 20% 미만 |

묶음 해석: 바뀐 것은 표 하나의 존재다. 설정값이 아니라 G 가 아니고, 코드나 이미지 배포가 없어 J 가 아니며, Oracle 계정(O), 잠금(D), 자원(A), 질의 비용(F)은 그대로다. 마이그레이션이 의도한 변경 내용은 맞았는데 적용 대상(환경)이 틀렸으니 P 다.

### 3단계 후보 목록 (실제 기전 × 부품, 숫자 순으로 줄 세움, 버린 이유 포함)

장부와 기존 목록은 피할 것 확인에만 썼다. 부품 다섯(banking Oracle, food MySQL, food payment 와 외부 PG, banking transfer API, food restaurant)에 걸쳐 실제 사례로 후보를 뽑았다.

| 순위 | 후보 | 원본 사례 | 부품(정답 위치 수), 묶음 | 결과 |
|---|---|---|---|---|
| 1 | 운영을 가리킨 로컬 마이그레이션이 banking Oracle TRANSFERS 를 지움 | Resend 2024-02-21 공식 블로그 | banking Oracle, DB 테이블(이체) 0, P 6, 은행 20 | **채택(F35-H)**. F53-R 시트가 "Oracle 은 진짜 DROP 과 휴지통 복원이 필요" 로 미뤘던 후보다. 실제 DROP 을 휴지통 FLASHBACK 과 계약 이름 복원으로 되돌릴 수 있고 진행 중 이체를 기다려 즉시 끝난다는 것을 로컬에서 실측해 풀었다(§4) |
| 2 | 같은 꼴을 food MySQL dispatches 에 | Resend 2024-02-21 | food MySQL, DB 테이블(배차) 1, P, 음식배달 20 | 뒤로 미룸: 숫자는 1번과 같지만 food 에는 이미 같은 엔진, 같은 수단, 같은 hold_schema 모드가 두 번(F53-R, F53-P) 있어 영향 범위만 다르다. 엔진과 진입 경로가 다른 1번이 먼저다. 다음 음식배달 차례 후보로 남긴다 |
| 3 | 응답 코드 탓에 백오프가 안 걸린 재시도 폭주(food payment → 외부 PG) | GitHub 2026-05-05 월간 가용성 보고 | 외부 결제 의존(6), 결제 경로 11, N 0 | 버림(이번 실행에서 이미 막힌 원본과 같은 주입, 원칙 9): payment 의 재시도는 4xx 를 ClientErrorException 으로 빼서(`application.yml` ignore-exceptions) 429 에 재시도가 일어나지 않고, 리미터를 따로 걸면 두 번째 주입(복합)이다 |
| 4 | 내부 백필 작업이 거래 내역 API 를 깊은 OFFSET 으로 훑어 DB 과부하 | Bitbucket 2025-05-08 공식 상태 페이지 | banking transfer(6), E+N | 버림(원칙 9, 6): 거래 내역 질의가 이미 전수 스캔(rejected 의 banking 인덱스 행 실측 약 224ms, 98k LR)이라 OFFSET 깊이로 비용이 거의 늘지 않고, 정답이 부하를 낸 내부 작업이라 주입이 load.north_south 진입 폭주(F07-H 꼴)와 같아진다 |
| 5 | DB 연결 시간 제한 1s→300ms 축소와 쓰기 DB 부하가 겹쳐 크래시 루프 | PostHog 2025-09-29 공식 사후 보고 | food restaurant(2), G | 버림(원칙 1): 원본 계기가 설정 축소와 별도 부하 상승 두 가지가 겹친 것이고, 하나만 재현하면 연결 획득이 ms 단위라 실패가 나지 않는다 |
| 6 | 열린 트랜잭션 뒤에서 DDL 이 기다리며 이체 DML 을 줄 세움 | Honeycomb 2024-08-06 공식 상태 페이지 | banking Oracle TRANSFERS, D | 버림(원칙 1, 9): Oracle 은 원본의 보유자(느린 SELECT)가 DDL 을 막지 않고, DML 보유자를 따로 주입하면 복합이다(rejected 의 Jamf School 행과 같은 벽) |
| 7 | 빈 필드 데이터가 널 검사 없는 새 코드에 닿아 NPE | Google Cloud 2025-06-12 공식 | food restaurant(2), J | 버림(원칙 1): 원본은 결함 코드 배포 뒤 데이터 변경이라는 두 계기(rejected 의 Fastly 복합, 'food 메뉴 일괄 갱신 빈 값' 행과 같은 벽) |
| 8 | 운영 명령 입력 실수로 food payment Deployment 를 replicas 0 | AWS S3 2017-02-28 공식 | 결제 서비스(3), 결제 경로 11, P+B | 버림(카탈로그 §1): F51-R 과 같은 수단(k8s.scale replicas 0), 같은 원본의 서비스 교체라 엔진, 진입 경로, 노이즈가 그대로다 |
| 9 | 마이그레이션이 코드보다 먼저 TRANSFERS 열 이름을 바꿈 | Onfido 2024-10-18 공식 | banking Oracle TRANSFERS, L | 버림(지휘 세션 지시): '스키마-코드 순서' 꼴은 사람 결정 전까지 내지 않는다 |

rejected 기록, 장부 §4-1, §5, 이번 실행에서 막힌 목록에 같은 원본(Resend 2024-02-21)과 같은 주입(Oracle 표 지우기)의 짝이 없다. Resend 는 F53-R, F53-P(food MySQL)에서 쓰였고 이번 후보는 엔진, 수단 모드, 대상이 다르다.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 엔티티: `core-banking/transfer-service/src/main/java/com/corebanking/transfer/entity/Transfer.java:7-12` `@Entity @Table(name = "transfers")`, `@GeneratedValue(strategy = GenerationType.IDENTITY)`. save 가 곧바로 INSERT 를 보내고 생성 키를 돌려받는다.
2. 이체: `TransferService.java:53-108` `execute`(@Transactional). 두 계좌 `findByIdForUpdate`(:69, :71) 뒤 `transferRepository.save(transfer)`(:99)에서 실패, 트랜잭션 되돌림(잔액 그대로).
3. 거래 내역, 통계, 정리: `TransferRepository.java:17-43` `search`, `dailyStatsSince`, `findIdsCreatedBefore` 모두 transfers 를 부른다. `TransferRetentionBatch.java:42-54` 1분마다 purge, 실패를 WARN 'Transfer retention purge failed, will retry next cycle' 로.
4. 오류 처리: `transfer-service/.../config/GlobalExceptionHandler.java:9-20` 은 ServiceException 만 다뤄 DataAccessException 은 Spring 기본 처리로 500 과 'Servlet.service() ... threw exception' ERROR. `application.yml:20` ddl-auto none(재시작해도 표를 만들지 않음).
5. 호출자: `account-service/.../client/TransferClient.java:28-58`(5xx 를 재시도 3회 뒤 502, 서킷 열림), `api-service/.../client/TransferClient.java:32-58`(거래 내역 500 을 502 로), `commerce/payment-service/.../client/BankingTransferClient.java:41-58`(정산 이체 실패 → 502), `commerce/order-service/.../OrderService.java:193`('Checkout payment failed, releasing reserved stock').
6. 프로브: `core-banking/k8s/22-transfer-service.yaml:81-100` startup, readiness 는 `/actuator/health`(DB 연결 isValid), liveness 는 `/actuator/health/liveness`. 표가 없어도 transfer 는 Ready.
7. 인프라: rca-testbed-banking StatefulSet testbed-oracle(파드 testbed-oracle-0, Oracle AI Database 26ai Free 23.26.2, tb-w2, PDB FREEPDB1). 109 읽기 전용 조회(2026-10-10): BANKING 표 여섯(ACCOUNTS, LEDGER_ENTRIES, OUTBOX_EVENTS, OUTBOX_RELAY_CONTROL, RESPONSE_DELAY_CONTROL, TRANSFERS), TRANSFERS 를 참조하는 의존 개체 0, 외래 키 0, 인덱스 SYS_C008658(PK), SYS_C008659(UQ), IDX_TRANSFERS_FROM, _TO, _ORDER, _STATUS, _CREATED, identity `ISEQ$$_73040`, `recyclebin=on`, `ddl_lock_timeout=0`, 휴지통 비어 있음, 표 784MB + 인덱스 1600MB(USERS used 5159MB 의 46%), USERS 빈 공간 5602MB. ORA-00942 문장은 표 이름을 담는다(`ORA-00942: table or view "BANKING"."NO_SUCH_TBL_X" does not exist`, 109 실측).

### 로컬 실측 (Oracle Free 23-slim, 109 와 같은 이미지 계열, 2026-10-10)

- 저장소 init.sql 로 만든 BANKING 스키마(TRANSFERS 30만 행) + 저장소 transfer-service 1.0.0 jar(이 커밋 기준 main 소스 그대로 빌드): 정상 POST 3/3 200, 거래 내역 200.
- `DROP TABLE BANKING.TRANSFERS`(휴지통 on): USERS used 86MB → 0, `dba_recyclebin` 에 표(can_undrop YES)와 인덱스 일곱. POST 3/3 500('SQL Error: 4043, SQLState: 42000', 'ORA-04043: Object transfers does not exist.', 'Servlet.service() ... InvalidDataAccessResourceUsageException: could not execute statement [ORA-04043 ...]'), 거래 내역 500('ORA-00942: table or view "BANKING"."TRANSFERS" does not exist'), `/actuator/health` UP.
- `FLASHBACK TABLE … TO BEFORE DROP`: USERS used 86MB, 행 수 그대로, identity 시퀀스 그대로(뒤이은 INSERT 성공), 인덱스와 제약(NOT NULL 검사 포함)은 `BIN$…` 이름. 계약 이름으로 다시 붙이면 원래 목록과 같다(SYS_C 꼴 이름도 붙일 수 있음, generated 칸만 USER NAME 으로 바뀜).
- 실행기 원격 스크립트(kubectl 을 docker exec 로 바꾼 시험 껍데기, 로컬 이름 표): preflight 0, run 0, 고장 중 preflight 1, cleanup 0(두 번째도 0, 멱등), recovery 0. 이미 FLASHBACK 만 된 상태(BIN$ 이름 13개)에서 cleanup 이 이름을 마저 붙이고 0. 초당 약 30건 이체 중 run 1.5초, cleanup 0.9초(진행 중 트랜잭션을 기다림), 그 사이 요청 300건 중 121건 500, 되돌린 뒤 POST 10/10 200, 거래 내역 200, 정산 계좌 잔액은 실패분이 되돌려짐. 처음 원격 스크립트는 이름 다시 붙이기에 ddl_lock_timeout 이 없어 부하 중 ORA-00054 로 cleanup 이 1 을 냈다(다시 돌리면 끝남). 세 DDL 모두 ddl_lock_timeout 10 아래에서 돌게 고쳤다.
- 109 에서 같은 원격 스크립트의 preflight(읽기 전용 조회만)를 계약 그대로 돌려 0: 휴지통 on, 인덱스와 제약 이름 일치, 사본 없음, 빈 공간 2048MB 이상.

## 5. 원인 규정

| 칸 | 값 | 근거 |
|---|---|---|
| `root_cause.target_id` | `banking-oracle:BANKING.transfers` | 결함을 가진 곳은 사라진 표다. transfer, account, api, commerce-payment 의 코드와 요청은 늘 하던 그대로라 정당하다(원칙 6: 정당한 요청이 결함 있는 곳에서 실패). 표기는 F01-P(`banking-oracle:BANKING.accounts`)와 같은 '인스턴스:스키마.테이블' |
| `trigger_target_id` | null | 근본과 계기가 같은 곳(이 표에 대한 잘못된 대상의 마이그레이션) |
| `scoring.partial` | banking-transfer, core-banking-transfer, banking-oracle, commerce-payment | 오류를 낸 서비스(transfer), 인스턴스까지만 짚은 답(Oracle), 도메인 밖 증상만 본 답(commerce-payment) |

원본 포스트모템도 "마이그레이션 명령이 운영을 가리켜 표를 지웠다" 를 원인으로 들었다. 같은 층위(사라진 표)로 지목한다. 누가 왜 어느 PC 에서 돌렸는지는 요구하지 않는다(원칙 5).

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 | 기대 |
|---|---|---|
| 감지 | core-banking-api, account, transfer 와 commerce-order, payment 의 오류율(APM 표본), 새 ERROR 로그(ORA-04043, ORA-00942, Servlet ERROR, 서킷 열림, 'Checkout payment failed') 급증 | 이상 탐지 이벤트와 인시던트. 같은 경로(api, account 502, commerce checkout 502)의 F35-R 은 정식(정상 녹화 있음)이다 |
| 피해 판정 | 러너: 동반 부하 이체(step transfer) 5xx 비율 ≥ 0.5, 3틱 | 평시 0, 고장 중 약 1.0(잔액 부족 400 만 예외) |
| RCA | transfer 오류 문장이 사라진 표 이름을 가리킴, 다른 표 질의는 성공, 파드와 풀과 Oracle 정상, KCM 변경 없음 | §7, §8 |

## 7. 관측 근거 표 (119 실조회, 평시)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 핵심 증거는 전수 수집되는 로그다.

| 증거 | 표, 칸 | 조회 | 결과 |
|---|---|---|---|
| 근본: 표가 없다는 DB 오류 | CH `lucida_logs_local`(service_name, severity_text, body) | `countIf(body LIKE '%ORA-00942%')`, `countIf(body LIKE '%ORA-04043%')`, `countIf(body LIKE '%does not exist%')` by service_name, 60일(남아 있는 것은 2026-10-03 08:00 UTC 부터) | core-banking-api, account, transfer, ledger 모두 0, 0, 0 |
| 근본 문장이 로그로 남는 경로 | 같은 표, core-banking-transfer WARN/ERROR 30일 | 문장 묶음 | 'SQL Error: 28000, SQLState: 99999'(WARN) + 'ORA-28000: ...'(ERROR) 각 342건(F35-R 시험 2026-10-09 06:55~07:12), 'ORA-00060' 2건, 'Transfer retention purge failed' 15건(같은 F35-R 시험). Hibernate SqlExceptionHelper 의 'SQL Error' WARN + ORA ERROR 짝과 배치 WARN 이 수집된다 |
| 전파: 이체 완료가 멈춤 | 같은 표, INFO | 1일 'Transfer COMPLETED%' | 73,169건(분당 약 51), commerce-payment 'Banking transfer for order%' 41,735건(분당 약 29), api 'Submitting transfer%' 24,966건 |
| 전파: commerce checkout 실패 | 같은 표, commerce-order ERROR 8일 | 'Checkout payment failed%' | F35-R 시험 구간(2026-10-09 06:55~07:12)에만 있고 그 밖 0 |
| 계기: DDL 문장 자체 | CH `dpm_topsql_local`(body JSON) | 30일 oracle 행에서 'alter table', 'rename' | 0건. Top SQL 본문은 sqlId, planHashValue, schema, module, 수치뿐이고 SQL 문장이 없다 |
| 계기: 지운 표의 공간 | VM `dpm.oracle.tablespace.used{tablespace="USERS"}`(DPM 이 DBA_TABLESPACE_USAGE_METRICS 사용량을 1분마다, lucida-next dbpoll oracle_dbgauge) | 현재값, `min_over_time`, `max_over_time` [7d], `count_over_time` [1h] | 현재 5,409,472,512B(109 used 5159MB 와 같음), 7일 4,998,430,720~5,409,472,512B, 시간당 58점. TRANSFERS 표 784MB + 인덱스 1600MB 라 DROP 하면 약 2.9GB 로 7일 최솟값보다 2GB 넘게 낮아진다(로컬 실측 DROP 뒤 used 86MB → 0) |
| 계기: Oracle 표 목록 지표 | VM 지표 이름 | `dpm.oracle.*` 목록 | 표 단위 지표 없음(MySQL 과 달리 table_count 가 없다). TRANSFERS 에 의존 개체가 없어 invalid_object_count 도 바뀌지 않는다 |
| 배제: 롤아웃, 재시작 | CH `kcm_events_local`(namespace, object_kind, object_name, reason) | 7일 rca-testbed-banking | 수집됨(2026-10-09 testbed-account ScalingReplicaSet, Killing, Unhealthy 등). 주입 구간에는 없어야 한다 |
| 배제: 잠금(F01-P) | VM `dpm.oracle.session.blocked_session` | max_over_time 7d | 최대 2(평시 순간값). F01-P 녹화는 장시간 차단으로 보인다. 이번 주입은 차단을 만들지 않는다 |
| 배제: Oracle 부하 | VM `dpm.oracle.session.active_session` | avg_over_time 1d | 약 0.19 |
| 보조: 표본 오류율 | VM `apm.agent.otel.java.error_rate{service_name="core-banking-transfer"}` | 1h | 평시 0 |

계기의 흔적은 둘이다: 같은 분의 USERS 사용량 급락(지운 표와 인덱스 크기, 장애, 잠금, 롤아웃, 부하로는 생기지 않음)과, 사라진 표 이름을 직접 가리키는 오류 문장. DDL 문장 자체는 119 에 남지 않는다. 그 시작 시각에 다른 변경(롤아웃, 재시작, 설정)이 없다. 인공 지연은 없다.

원칙 8 참고(평가 권고): 실행기는 기존 Oracle 실행기(F14-P, F35-R)처럼 파드 안 SYSDBA sqlplus 로 접속해, DPM 세션 표본에 'sqlplus@testbed-oracle-0' SYS 세션이 잡힐 수 있다. 시나리오 id, 보류 이름, SQL 은 남지 않는다. 원본의 '개발자 PC 의 마이그레이션 도구' 접속과 모양이 다른 점은 기존 Oracle 실행기 공통 한계다.

## 8. 감별

- must_support: 같은 분 DPM USERS 사용량 약 2.4GB 급락, transfer WARN 'SQL Error: 4043, SQLState: 42000' + ERROR 'ORA-04043: Object transfers does not exist.'(이체마다), ERROR 'ORA-00942: table or view "BANKING"."TRANSFERS" does not exist'(거래 내역마다), WARN 'Transfer retention purge failed'(1분마다), 'Transfer COMPLETED' 멈춤, account 'Transfer service call failed ...: 500' 과 서킷 열림, api 502, commerce-payment 'core-banking transfer call failed: 500', commerce-order 'Checkout payment failed', 잔액 조회 200, KCM 변경 없음, transfer 와 Oracle Ready.
- must_rule_out: F35-R(ORA-28000, 풀 비움, 파드 이탈, 잔액 조회도 실패), F01-P(대기 뒤 시간 초과, 차단 세션), transfer 파드 소실(F17-H, F50-R, F51-R, F52-R: Connection refused, KCM 변경), transfer 새 버전(F40-H, F42-R: 롤아웃, 앱 검증 오류나 느린 질의), Oracle 다운이나 포화(F25-H 꼴), 부하.
- contrast_with: F35-R, F01-P, F53-R, F52-R(정답지 `related_scenarios`).

가르는 관측 근거 한 줄: 오류가 Oracle 이 낸 ORA-04043/00942 이고 그 문장이 표 하나를 가리키며, 같은 분에 USERS 사용량이 그 표 크기만큼 떨어지고, 같은 Oracle 의 다른 표 질의와 모든 파드, 풀, 세션이 정상이다.

## 9. 러너 판정 조건과 강도, 부하

- 주입: db.ddl(Oracle 모드) 1단 고정(`approved-fixed-f35-h`). 강도 축이 없다: 표가 없으면 그 표를 부르는 문장은 요청량과 무관하게 100% 실패한다. min_hold 15m, settle 30s, timeout 20m, max_injection_duration 25m.
- 동반 부하: load.north_south `core-banking transfer-heavy-surge.js` 5rps(이체 40%, 잔액 35%, 거래 내역 15%, 목록 10%), ramp 2m, hold 21m, entry 30082(F51-R, F52-R 과 같은 스크립트와 세기). 초당 이체 약 2건, 거래 내역 0.75건이 실패 표본을 시간대와 무관하게 보장한다. 평시 거래 내역 1건이 약 0.22초 CPU 의 전수 스캔이라 램프 동안 Oracle CPU 를 약 0.17 코어 더 쓰고, 고장 중에는 즉시 실패해 오히려 가볍다. 부하 상한 180 의 3%.
- 피해 계산(원칙 9): 실패가 시간 초과가 아니라 즉시 나는 SQL 오류라 서킷브레이커는 실패를 줄이지 않고 빠르게 만들 뿐이다(account, api 서킷 열림 → 502). 상주 commerce 부하의 checkout 이 정산마다 실패한다(평시 분당 약 29건).
- success: 동반 부하 이체 5xx 비율 ≥ 0.5, 3틱.
- must_rule_out: achieved_rps < 1.25, 잔액 조회(step get) 실패율 ≥ 0.2(account 나 Oracle 전체 장애), Oracle 파드 NotReady, transfer 파드 NotReady, transfer 재시작 ≥ 2.
- abort: entry_status == 0(nginx 나 노드가 죽음). 표 지우기는 파드를 건드리지 않아 api 가 502 로 답한다.
- recovery: target_health 200, Oracle Ready, transfer Ready, 기준선 이체 5xx < 0.05, 2틱, 10m.
- cleanup: `FLASHBACK TABLE BANKING.TRANSFERS TO BEFORE DROP`, 인덱스와 제약 이름을 계약대로 다시 붙임, 전체 목록과 휴지통 빈 것 확인, 부하 종료. 되돌린 표의 공간은 다시 사용량으로 세어진다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Resend 2024-02-21, 공식 블로그)와 요소별 대응표, 기전이 원본과 같다 (§2). 실제 DROP TABLE 이고 되돌림만 휴지통 FLASHBACK 이다
- [x] 분류 장부를 갱신했고 묶음 P(6→7, 10.4%), 정답 위치 DB 테이블(이체)(0→1), 결제 경로 11/67(16.4%), 서비스 은행 20→21 과 고른 이유를 적었다. 어느 축도 20% 미만 (§3)
- [x] 근본 원인 위치가 인프라 지점(BANKING.TRANSFERS, 109 사전 조회)과 `file:line`(Transfer.java:7-12, TransferService.java:99)으로 확인됐다 (§4)
- [x] 근본 원인의 흔적이 119 에 남는 경로를 조회했다: VM dpm.oracle.tablespace.used{USERS}(1분, 7일 4.998~5.409GB), lucida_logs_local 의 SqlExceptionHelper WARN + ORA ERROR 짝(같은 경로의 ORA-28000 342건), ORA-04043/00942 평시 0건 (§7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: 로그 전수 (§7)
- [x] 계기 흔적: 같은 분 USERS 사용량이 지운 표 크기만큼 급락하고, 오류 문장이 사라진 표 이름을 가리키며, 그 시각에 롤아웃, 재시작, 설정 변경이 없다(KCM). 인공 지연 없음 (§7)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다: "banking Oracle 의 TRANSFERS 표가 없다" 는 오류 문장에서 바로 나온다 (§5)
- [x] 정답지 세 칸이 원칙 6대로 채워졌다 (§5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다 (§6)
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: 원격 스크립트와 argv 에 id 없음(테스트). 휴지통 이름은 Oracle 이 만든다. SYS sqlplus 세션은 기존 Oracle 실행기 공통 한계(§7) (§4, §7)
- [x] 부하 상한과 서킷브레이커를 고려해 피해가 실제로 날 계산이 있다: 즉시 실패라 요청량과 무관하게 100%, 동반 5rps (§9)
