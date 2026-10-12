---
title: F69-R 설계 시트 (결제 감사 자료 내보내기 Job 의 mysqldump 기본 표 잠금이 food 결제 쓰기를 막아 모든 새 주문이 결제 단계에서 실패)
status: Draft
owner: project
last_reviewed: 2026-10-12
tags:
  - scenario
  - design
  - batch-job
  - lock
summary: 재무 쪽 결제 감사 자료 내보내기 Job 이 food MySQL 결제 표를 mysqldump 기본값(--lock-tables)으로 떠서 덤프 내내 LOCK TABLES ... READ LOCAL 을 쥐자, payments 의 쓰기가 모두 메타데이터 잠금을 기다려 payment 연결 풀이 차고 모든 새 음식 주문이 결제 단계에서 502 로 실패한다. 원본 KnowBe4 2025-10-13.
---

# F69-R 설계 시트

## 1. 요약

재무 쪽이 결제 감사 자료를 보관소로 올리려고 일회성 쿠버네티스 Job `payment-audit-export` 를 food 네임스페이스에 띄운다. Job 은 노드에 이미 있는 MySQL 이미지(mysql:8.0, 클라이언트 도구 포함)에서 food 서비스들이 쓰는 앱 계정(mysql-secret)으로 `mysqldump --no-tablespaces fooddelivery payments settlement_summary | xz -6 -T1` 을 돌린다. CPU 한도는 150m 다.

mysqldump 의 기본값(`--opt`)에는 `--lock-tables` 가 들어 있다. `--single-transaction` 을 주지 않으면 덤프를 시작할 때 `LOCK TABLES payments READ LOCAL, settlement_summary READ LOCAL` 을 걸고 마지막 행을 쓸 때까지 쥔다. InnoDB 에서 이 표 읽기 잠금은 읽기는 통과시키지만 그 표의 INSERT, UPDATE, DELETE 를 모두 'Waiting for table metadata lock' 에 세운다. 덤프는 받는 쪽(xz)이 압축하는 속도로만 나아가므로 payments 약 173만 행(SQL 약 190MB)을 내보내는 데 10분이 넘게 걸리고, 그동안 결제 쓰기가 막힌다.

payment-service 는 결제마다 외부 PG 를 부르기 전에 PENDING 결제 행부터 INSERT 한다. 결제 약 15건이면 Hikari 연결 15개가 모두 기다리는 INSERT 에 묶이고, 뒤의 결제 요청은 연결을 3초 기다리다 500 이다. order 는 주문 저장과 배차까지 끝낸 뒤 결제를 부르므로 모든 새 주문이 그 마지막 단계에서 502 로 롤백된다. payment 의 health(DB 확인)가 같은 풀을 기다려 payment 가 NotReady 가 되고 재시작한다. Job 을 치우면 덤프 연결이 끊기며 잠금이 풀리고 기다리던 쓰기가 한꺼번에 지나간다.

비유: 회계팀이 감사용으로 금고 장부를 통째로 복사하겠다며 "복사하는 동안 장부에 아무도 쓰지 마시오" 팻말을 걸어 두고, 복사기는 아주 느린 것을 골랐다. 장부를 읽는 사람은 지나가지만, 새 결제를 적으러 온 직원들은 팻말 앞에 줄을 서고, 손님(주문)은 계산대에서 결제가 안 된다며 돌아간다. 팻말을 건 사람이 회계팀이라는 사실은 복사기 옆에 서 있는 낯선 사람(새 Job 파드)으로 드러난다.

## 2. 원본 사례

- 기업: KnowBe4 (KSAT, PhishER)
- 날짜: 2025-10-13 (사후 보고 게시 2025-10-27)
- 링크: [KSAT & PhishER - Login Issues](https://status.knowbe4.com/incidents/mm0hdhwtjzrz) (공식 상태 페이지 사고와 사후 보고)
- 요약(출처가 말한 것): 19:30~21:58 UTC 캐나다(CA) 지역 KSAT, PhishER 콘솔 로그인 실패와 시간 초과. 그날 앞서 배포한 새 데이터 내보내기("data processing") 작업이 공유 인증 표에 무거운 질의를 돌렸고("intensive queries against shared authentication tables"), 그 질의가 동시 갱신과 맞지 않는 공유 잠금을 만들어 로그인과 등록 갱신이 끝나지 못했다. 제외 필터가 운영 스키마 이름 불일치로 듣지 않아 큰 이진 열까지 처리하다 작업이 죽고 다시 뜨기를 되풀이하며 핵심 표의 잠금을 다시 잡았다(2차 원인). DB 층에서 시간 초과하는 인증 요청 감시로 탐지했지만 처음엔 캠페인 갱신 적체와 DB 포화를 의심했고, "The lack of targeted database lock monitoring delayed the identification of the blocking component". CA 에서 작업을 치우자 잠금이 풀리고 로그인이 곧바로 회복됐다. 후속은 고트래픽 인증 표를 피하는 내보내기 커넥터 수정, 핵심 표 잠금 경합 감시, 트랜잭션 DB 에 잠금을 걸지 않는 다른 추출 방식. 표 이름과 DB 엔진은 밝히지 않았다.
- 자료 문서 `docs/ref-real-world-incidents.md` M20(배치 잡과 온라인 트래픽 충돌)에 이번에 추가했다.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 새로 배포한 데이터 내보내기 작업이 돌기 시작 | 결제 감사 자료 내보내기 Job payment-audit-export 가 시작 |
| 원인이 된 결함 | 내보내기 질의가 공유 트랜잭션 표에 동시 갱신과 맞지 않는 공유 잠금을 만듦 | mysqldump 기본값(--lock-tables, --single-transaction 없음)이 payments, settlement_summary 에 LOCK TABLES ... READ LOCAL 을 걸어 덤프 내내 쥠(읽기는 통과, 쓰기는 대기) |
| 전파 경로 | 잠금 때문에 로그인 세션과 등록 갱신 쓰기가 끝나지 못하고 인증 요청이 DB 층에서 시간 초과 | 결제 INSERT(결제마다 첫 문장), 보존 정리 DELETE, 정산 UPDATE 가 메타데이터 잠금 대기 → payment Hikari 고갈 → order 의 결제 호출 실패 |
| 사용자 증상 | CA 지역 로그인 실패, 콘솔 접근 불가, 간헐적 인증 시간 초과 | 모든 새 음식 주문이 결제 단계에서 502. 둘러보기, 배달 조회는 정상 |
| 원본의 탐지 경로 | DB 층 인증 요청 시간 초과 감시, 처음엔 DB 포화로 오인, 잠금 감시가 없어 차단 주체 식별이 늦음 | lucida 이상 탐지(order 오류율, payment 오류 로그와 재시작), 인시던트. 차단 주체는 DPM 세션 스냅숏의 작업 파드 세션과 KCM 의 새 Job 으로 이어야 나온다 |
| 완화와 복구 | 작업을 치우자 잠금이 풀리고 로그인 곧바로 회복, 서비스 재배포로 연결 재초기화 | cleanup 이 Job 을 지우면 덤프 연결이 닫혀 잠금이 풀리고 결제 쓰기가 지나감. payment 는 재시작 고리에서 몇 분 안에 Ready |

원본과 다른 점: 원본의 2차 원인(작업이 죽고 다시 뜨며 잠금을 다시 잡음)은 재현하지 않는다(backoffLimit 0). 1차 원인과 그 고리(내보내기의 공유 잠금 → 갱신 차단 → 시간 초과 → 작업 제거로 회복)가 같다. 원본이 엔진과 질의를 밝히지 않아, 우리 스택에서 같은 고리를 내는 가장 흔한 꼴인 mysqldump 기본 잠금으로 옮겼다(대상과 규모만 바꿈, 원칙 1).

## 3. 숫자 근거 (2026-10-12, `scenario-stats.py`, 정식 + 후보)

- 서비스: 쇼핑몰 34, 은행 34, 음식배달 33(합 101). 가장 적은 음식배달을 고름. 추가 뒤 34/34/34.
- 묶음: 주 묶음 D(DB 잠금) 14(13.9%) → 15/102(14.7%). 20% 상한 아래. 대안으로 본 J 는 17(16.8%).
- 정답 위치: 내부 배치 작업 2(F59-R, F59-P) → 3(2.9%). 결제 경로 합계 13/101(12.9%) 그대로(정답이 결제 표나 결제 서비스가 아니라 작업).
- 왜 이 후보인가: 아래 후보 목록 중 원칙 1~9 관문을 모두 넘은 첫 후보다. 원본 KnowBe4 2025-10-13 은 어느 시나리오에도 쓰이지 않았고, 'DB 에 바로 붙은 내보내기 작업의 표 잠금' 은 기존 D 묶음(행, 표 잠금은 F01-R, F01-P, F06-H 처럼 주인이 드러나지 않는 잠금 세션이거나 F53-H, F60-H, F66-R 처럼 스키마 변경 대기열)에 없는 계기다.

### 후보 목록 (실제 기전 × 부품, 숫자 순으로 줄 세움)

| # | 후보 | 원본 | 부품 | 결과 |
|---|---|---|---|---|
| 1 | 결제 감사 내보내기 Job 의 mysqldump 기본 표 잠금 | KnowBe4 2025-10-13 공식 | food MySQL 결제 표 + 내부 배치 작업(위치 2) | **채택** |
| 2 | 로그 수준 설정 배포의 과다 로깅이 최대 부하에서 자원 포화 | Asana 2023-12-20 공식 상태 페이지 | food restaurant(가게 서비스 3) | 버림(원칙 9): 로컬 실측(restaurant 1.0.0 + OTel 2.9.0 + MySQL, CPU 0.5) root DEBUG 요청당 43줄인데 50~150rps 에서 p95, CPU 가 INFO 와 같음 |
| 3 | 제약 제거 마이그레이션의 표 잠금이 연결 고갈 | Buttondown 2026-06-11 공식 블로그 | food MySQL 표 | 버림(원칙 1, 카탈로그 §1): 어떤 제약인지 미공개, 제약 제거는 잠금을 잡는 순간 끝나 오래 쥐려면 앞선 긴 질의가 필요해 F60-H, F66-R 대기열 꼴 |
| 4 | 뜨거운 행 시각 갱신이 동시성에서 직렬화, 연결 고갈 | Nango 2025-10-06 공식 상태 페이지 | food restaurants 행 | 버림(원칙 1): 계기가 작업량 급증이고 앱에 그런 쓰기가 없어 릴리스와 급증 두 계기가 필요 |
| 5 | 릴리스의 비효율 질의로 DB 연결 한도 | Trybe 2026-08-27, Svix 2023-06 공식 | food 서비스 + MySQL | 버림(카탈로그 §1): F42-R, F42-P, F33-H 의 '릴리스 질의가 DB 포화' 꼴 |
| 6 | 자동 확장이 연결 폭주와 재시도를 일으킴 | AWS us-east-1 2021-12-07 공식 | food MySQL 연결 | 버림(원칙 1): 원본은 내부 네트워크 장비 포화라 DB 연결로 옮기면 기전이 바뀜 |
| 7 | 리팩터가 풀 크기를 기본값으로 덮음 | GitLab 2021-07-27 production#5238 | food order(주문 서비스 8) | 버림(원칙 9): Hikari 기본 10 이 order 동시 수요보다 커 피해 계산이 서지 않음(dispatch 판은 반복 62 에서 같은 벽) |
| 8 | 실패에 백오프 없는 폴링이 API 를 누름 | Buttondown 2025-11-06 공식 블로그 | food 내부 호출자 | 버림(원칙 1): 테스트베드 안에 폴링하는 내부 호출자가 없음 |
| 9 | 엣지 경로 패킷 손실로 간헐 시간 초과 | GitHub 2025-12-18 월간 보고 | tb-w3 파드 경로(노드, 디스크 6) | 버림(원칙 4, 8): 파드 네임스페이스의 재전송은 노드 지표에 남지 않고, 주입 명령(sudo tc)이 syslog 에 인공 흔적을 남김 |
| 10 | 검색 끝점 트래픽 급증으로 지연 | GitHub 2025-12-22 월간 보고 | food restaurant 검색 | 버림(원칙 9): food 검색 경로는 100rps 넘어도 무릎이 없음(spec-scenario-load §8.1-2) |
| 11 | 마이그레이션이 스키마 어긋남을 만들어 쓰기 실패 | GitHub 2025-12-18 월간 보고 | food 표 | 버림: '스키마-코드 순서' 꼴은 사람 결정 대기(Tuist 2026-08-19 줄) |

서로 다른 부품: MySQL 결제 표, restaurant, MySQL 표, restaurants 행, order, MySQL 연결, 노드 네트워크, 검색 끝점(5개 이상).

## 4. 인과 사슬과 코드, 인프라 위치

1. Job `payment-audit-export`(rca-testbed-food, tb-w3, mysql:8.0, CPU 150m)가 `scripts/scenarios/profiles/k8s_job_executor.py:182-187`(EXPORT_SCRIPT) 의 `mysqldump --host="$DB_HOST" --user="$DB_USER" --no-tablespaces "$DB_NAME" $EXPORT_TABLES | xz -6 -T1` 을 돈다. 계정 DB_USER, MYSQL_PWD 는 mysql-secret 에서(앱 계정 fooddelivery, `fooddelivery.*` 전체 권한, LOCK TABLES 포함).
2. mysqldump 8.0.46 기본값 `--lock-tables` → `LOCK TABLES payments READ /*!32311 LOCAL */, settlement_summary READ /*!32311 LOCAL */` → payments 를 `SELECT /*!40001 SQL_NO_CACHE */ * FROM payments` 로 한 행씩 흘림(--quick). 받는 쪽 xz 가 느려 서버 세션은 'Sending to client' 로 머무른다.
3. `food-delivery/payment-service/src/main/java/com/fooddelivery/payment/service/PaymentService.java:50-66` processPayment 의 첫 쓰기 `paymentRepository.save(payment)`(66) 가 SHARED_WRITE 메타데이터 잠금 대기. 서버 `@@lock_wait_timeout` 31536000.
4. `food-delivery/payment-service/src/main/resources/application.yml:12-15` Hikari 15, 연결 대기 3000ms → 결제 약 15건 뒤 'Connection is not available, request timed out after 3000ms'.
5. `PaymentRetentionService.java:48-64`(6초마다 DELETE), `SettlementBatch.java:36-62`(saveAll) 도 대기.
6. `food-delivery/order-service/src/main/java/com/fooddelivery/order/service/OrderService.java:152-160` 결제 호출 실패 → `PaymentClient.java:30-59`(재시도 2회, 서킷, fallback 502) → 주문 트랜잭션 롤백, POST /api/orders 502.
7. `food-delivery/k8s/23-payment-deploy.yaml:67-86` 세 프로브 모두 /actuator/health(DB 확인) → payment NotReady, liveness 재시작. 다시 뜬 파드도 첫 결제에서 막힘.
8. 다른 표(orders, order_items, dispatches, outbox 표)는 잠기지 않아 order 와 dispatch 는 평시대로 쓴다.

## 5. 원인 규정 (원칙 5, 6)

- 근본(`root_cause.target_id`): `rca-testbed-food/job/payment-audit-export`. 정당한 결제 쓰기를 막는 잠금을 건 작업이 원인이다. payment, order, MySQL 은 코드, 설정, 스키마, 요청량이 그대로이고 정당한 요청을 처리했을 뿐이다. 원본 사후 보고가 원인을 내보내기 작업으로 지목하고 그 작업을 치워 완화한 층위와 같다. 정답 위치는 장부 §2-1 '내부 배치 작업'(F59-R, F59-P 와 같은 말).
- 계기(`trigger_target_id`): 비움. 작업 시작이 곧 근본이다.
- 부분 점수(`scoring.partial`): `food-mysql:fooddelivery.payments`(잠긴 표, 증상이 모이는 곳), `food-delivery-payment`, `food-payment`, `testbed-payment`(풀이 마른 서비스), `food-mysql`, `food-delivery-order`(주문 502 를 낸 곳). 합리적인 관제 답(잠긴 표를 지목)을 오답으로 두지 않는다.
- 관제 데이터로 낼 수 있는 결론인가: 예. "새 Job 이 뜬 직후, 그 파드의 DB 세션 하나가 오래 'Sending to client' 이고 결제 세션들이 메타데이터 잠금을 기다린다" 는 KCM 과 DPM 만으로 이어진다. 코드 설계 결함 추론이 필요 없다.

## 6. 감지, 피해 판정, RCA 증거 (원칙 7)

| 층 | 무엇으로 | 내용 |
|---|---|---|
| 감지 | lucida 이상 탐지 | food-delivery-order 오류율 급증(POST /api/orders 502), order 오류 로그 'Failed to process payment for order', payment 의 Hikari 오류 로그와 KCM Unhealthy, Killing. 별도 업무 규칙 없이 인시던트가 생길 겉 증상 |
| 피해 판정 | 러너(loadgen-food 기준선 문서) | 주문 생성 5xx 비율 ≥ 0.5, 2xx 비율 < 0.2 를 3틱. 평시 5xx 는 배차 한도 거절이 드물게 섞이는 0 근처 |
| RCA | 녹화 데이터 | KCM 의 새 Job 과 파드, DPM 세션 스냅숏(작업 파드 IP 의 긴 'Sending to client', payment 파드의 'Waiting for table metadata lock'), 덤프 뒤 DPM Top SQL 의 mysqldump 문장, 롤아웃·스키마 변경 없음 |

## 7. 관측 근거 표 (119 실조회, 평시. 장애 시 모양은 첫 실행에서 다시 확인)

표본 데이터(트레이스 10%, APM 지표)로 증명하지 않는다. 결정 증거는 전수인 KCM 이벤트, DPM 세션 스냅숏(1분), DPM Top SQL, 로그다.

| 증거 | 119 표, 칸 | 조회와 결과(2026-10-12) |
|---|---|---|
| 새 Job 과 파드(계기) | `lucida.kcm_events_local` object_kind, object_name, reason, namespace | `SELECT object_kind, reason, count() FROM lucida.kcm_events_local WHERE namespace='rca-testbed-food' AND timestamp > now() - INTERVAL 30 DAY GROUP BY 1,2` → Pod(Unhealthy 1654, Pulled 177, Created 174, Started 172, Killing 104, Scheduled 84, BackOff 8), Deployment ScalingReplicaSet 167, ReplicaSet 84/84. **Job 0건**. Job 종류 이벤트가 수집되는지는 kcm-128 네임스페이스의 `Job kcm-agent-enroll SuccessfulCreate 'Created pod: ...'`(2026-09-17)로 확인 |
| 작업 파드의 긴 DB 세션(근본) | `lucida.dpm_session_local` body JSON: user, hostname, state, queryTime, sqlHash | `SELECT JSONExtractString(body,'state'), count(), max(JSONExtractFloat(body,'queryTime')), groupUniqArray(3)(JSONExtractString(body,'hostname')) FROM lucida.dpm_session_local WHERE engine='mysql' AND timestamp > now() - INTERVAL 60 DAY AND JSONExtractString(body,'user')='fooddelivery' GROUP BY 1` → executing 29196, waiting for handler commit 2641, ... **'Sending to client' 0건**, hostname 은 모두 서비스 파드 IP(10.244.2.x). 수집 시작 2026-08-13, 전체 289,201행 |
| 결제 세션의 메타데이터 잠금 대기(전파) | 같은 표 state | 같은 조회에서 'Waiting for table metadata lock' 7행, 한 스냅숏(2026-10-09 03:43, 10.244.2.82)뿐 |
| 덤프 문장(근본, 사후) | `lucida.dpm_topsql_local` body JSON sqlText, calls, avgExecTime, rowSent | `SELECT count() FROM lucida.dpm_topsql_local WHERE engine='mysql' AND timestamp > now() - INTERVAL 60 DAY AND (position(body,'SQL_NO_CACHE')>0 OR positionCaseInsensitive(body,'LOCK TABLES')>0)` → **0**. 분마다 상위 15~17 문장을 남기고 1회 호출 문장도 들어온다(`SELECT timestamp, count(), min(calls) ...` → 15~17, mincalls 1) |
| payment 풀 고갈(전파) | `lucida.lucida_logs_local` service_name, body | 'Connection is not available' 30일: food-delivery-payment 10건(평시 드묾). 평시 1시간 payment 는 'Processed payment id=...' 가 결제마다, order 는 'Created order id=', 'Recorded outbox event' |
| payment 재시작(전파) | `kcm_events_local` Pod Unhealthy, Killing | 평시에도 Unhealthy 가 있어(30일 1654) 단독 근거로 쓰지 않는다. 롤아웃(ScalingReplicaSet) 없이 testbed-payment Killing 이 몰리는 모양으로 쓴다 |
| MySQL 은 한가함(감별) | VM `dpm.mysql.*`, 호스트 지표 | 덤프는 xz 속도로만 나아가 서버 쪽 SELECT 는 대부분 클라이언트를 기다린다(로컬 실측 CPU 평시) |

109 읽기 전용 실측(2026-10-12): `information_schema.tables` payments 1,726,617 행(데이터 298MB, 인덱스 99MB), settlement_summary 1,288 행, `@@lock_wait_timeout` 31536000, `@@innodb_lock_wait_timeout` 50, `@@max_connections` 151, `@@net_write_timeout` 60, Threads_connected 21, Max_used_connections 38. mysql 파드에 mysqldump 8.0.46, xz, gzip 있음. `SHOW GRANTS` fooddelivery: `ALL PRIVILEGES ON fooddelivery.*`.

로컬 실측(2026-10-12, mysql:8.0 = 8.0.46, 서버 CPU 0.5 메모리 1Gi, 같은 payments 정의 1,726,617 행):
- 이 시나리오의 EXPORT_SCRIPT 그대로(MYSQL_PWD 환경 변수, --no-tablespaces) 덤프가 오류 없이 시작, 세션 'Sending to client' + `SELECT /*!40001 SQL_NO_CACHE */ * FROM payments`.
- `performance_schema.metadata_locks`: payments, settlement_summary SHARED_READ_ONLY GRANTED(덤프 세션), 다른 세션 INSERT 의 payments SHARED_WRITE PENDING, processlist 'Waiting for table metadata lock'. 같은 표 SELECT 는 즉시 끝남.
- 덤프 컨테이너를 죽이자 덤프 세션이 사라지고 기다리던 INSERT 가 곧바로 끝남.
- 덤프 길이: SQL 193,995,997 바이트, xz -6 뒤 5,332,108 바이트. x86 Xeon Silver 4514Y 에서 받는 쪽 CPU 0.5 일 때 302초, 0.2 일 때 829초(압축 없이는 4초, gzip -6 CPU 0.2 는 32초).

## 8. 감별

- must_support, must_rule_out: 정답지(`scenario-metadata.json` F69-R)와 같다.
- contrast_with:
  - F53-H: 같은 결제 표 메타데이터 잠금 대기와 결제 단계 502. 가르는 근거는 기다리는 스키마 변경 세션(ALTER, root@localhost)의 유무, 긴 세션의 주인(DB 파드 안 localhost 대 새 Job 파드 IP), 그 문장(열 목록 SELECT 대 mysqldump 의 SELECT /*!40001 SQL_NO_CACHE */ *), KCM 의 Job 이벤트, 읽기도 줄을 서는지(F53-H) 아닌지(F69-R). 정답도 다르다(표 대 작업).
  - F59-R: 같은 '새 내부 Job 이 원인' 정답 꼴. F59-R 은 API 를 깊은 페이지로 몰아쳐 dispatch 풀과 MySQL 을 포화시키고 주문이 503, F69-R 은 DB 에 바로 붙은 덤프의 표 잠금이고 MySQL 은 한가하며 주문이 결제 단계 502.
  - F60-H: 배차 표 메타데이터 잠금 대기열, 주문이 저장 전에 503.
  - F06-H: commerce 결제 표 배타 잠금. 잠금 주인이 드러나지 않고 정답이 표.

## 9. 러너 판정 조건, 강도, 부하 계산 (원칙 9)

- 실행기: k8s.job 에 내보내기 계약(F69-R)을 더함. 이미지 주인을 `statefulset/testbed-mysql` 처럼 kind/name 으로 적을 수 있게 했고(백필 계약은 그대로 `deploy/` 로 찾음), 컨테이너 환경에 Secret 참조를 쓴다. 기존 F59-R, F59-P 테스트가 그대로 통과한다.
- 강도(승인 고정값 하나): CPU 한도 150m, xz -6. 로컬 x86 CPU 0.2 에서 829초이므로 150m 이면 그보다 길다. 109 노드 CPU 는 GB10(Cortex-X925 / A725)이라 코어가 x86 보다 2배 빠르더라도 덤프가 약 9분 넘게 걸려 판정 시각(약 5분 30초)에 아직 잠금이 살아 있다. 느린 코어면 더 길어지지만 cleanup 이 Job 을 지워 끝내므로 상한(activeDeadlineSeconds 1800, max_injection_duration 20m)에 닿지 않는다.
- 피해 계산: 기준선 주문 초당 0.2~1.15건(하루 주기), 결제 재시도 2회 → payment 풀 15 는 결제 시도 약 15번, 13~75초에 찬다. 그 뒤 모든 주문이 결제 단계 502. order 는 결제 서킷이 열리면 곧바로 실패해 order 풀이 마르지 않는다(F53-H 와 같은 계산).
- 세션 수: payment 재시작마다 기다리는 서버 세션이 최대 15씩 남는다(옛 파드 세션은 잠금이 풀릴 때까지 기다림). liveness 15초 × 5 와 기동 약 25초로 재시작 고리가 약 2분이라 5분 30초면 재시작 2~3번, 기다리는 세션 최대 약 60 + 평시 21 ≈ 80 < max_connections 151.
- 판정: success 기준선 5xx ≥ 0.5 + 2xx < 0.2 3틱(min_hold 4m), must_rule_out MySQL 파드 NotReady 와 order 파드 NotReady, abort entry_status 0. 동반 부하 없음(F53-H 와 같음).
- recovery: target 200, MySQL·order Ready, 기준선 2xx ≥ 0.7 2틱, timeout 10m(payment CrashLoopBackOff 지연).

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(KnowBe4, 2025-10-13, 공식 상태 페이지 링크)와 요소별 대응표가 있고, 기전(내보내기 작업의 공유 잠금 → 갱신 차단 → 시간 초과 → 작업 제거로 회복)이 원본과 같다 (원칙 1, §2)
- [x] 분류 장부를 갱신했고(§4-1 행, §6 기록), 묶음 D 15/102(14.7%), 정답 위치 내부 배치 작업 3(2.9%), 결제 경로 13/102, 서비스 34/34/34, 고른 이유를 적었다. 어느 축도 20% 에 닿지 않는다 (원칙 2, §3)
- [x] 근본 원인 위치가 인프라 지점(Job payment-audit-export, EXPORT_SCRIPT `k8s_job_executor.py:182-187`)과 코드 위치(`PaymentService.java:50-66` 등)로 확인됐다 (G1, §4)
- [x] 근본 원인의 흔적이 119 실데이터에서 조회됐다: kcm_events_local 의 Job 이벤트 수집(kcm-128 사례), dpm_session_local 의 hostname, state, queryTime, dpm_topsql_local 의 sqlText (원칙 3, §7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: KCM, DPM 세션, Top SQL, 로그(전수) (원칙 3)
- [x] 계기의 흔적이 조회된다: 새 Job SuccessfulCreate 와 파드 Scheduled, Pulled, Started. 이유 없는 인공 지연이 없다: 덤프가 느린 것은 받는 쪽 압축(현실의 내보내기 작업 꼴)이고 잠금은 mysqldump 기본값에서 나온다 (원칙 4)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다: 새 Job 파드의 긴 세션과 결제 세션의 메타데이터 잠금 대기가 같은 분에 시작하고 Job 이 사라지는 분에 함께 끝난다 (원칙 5)
- [x] 정답지 세 칸이 원칙 6대로다: 근본 = 작업, 계기 비움, 부분 = 잠긴 표, payment, MySQL, order (§5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다 (원칙 7, §6)
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: Job 이름, 레이블, 스크립트, 환경 변수에 id, fault, chaos, inject 없음(테스트 `test_k8s_job_payment_export_dumps_with_default_table_locks_and_leaves_the_backfills`). DB 세션의 계정은 앱 계정이고 프로그램은 실제 내보내기 도구 mysqldump 다 (원칙 8)
- [x] 부하 상한과 서킷브레이커를 고려해 피해가 실제로 날 계산이 있다: 기준선만으로 payment 풀 15 가 13~75초에 차고, order 결제 서킷이 열려도 결제 단계에서 502 다. 덤프 길이는 판정 시각보다 길다 (원칙 9, §9)
