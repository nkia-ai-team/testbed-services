---
title: F33-P 설계 시트 — commerce auth_tokens 인덱스 교체 마이그레이션 실패로 토큰 확인 전수 스캔
status: Draft
owner: project
last_reviewed: 2026-10-09
tags:
  - scenario
  - design
summary: 겹친 token 인덱스 두 개를 합치는 마이그레이션이 새 유일 인덱스의 동시 생성을 statement_timeout 으로 끊겨 INVALID 로 남긴 채 옛 둘을 지우고 이름을 바꿔, 게이트웨이가 쓰기마다 부르는 토큰 확인이 240만 행 전수 스캔이 되고 commerce PostgreSQL 이 포화되는 시나리오(원본 Vapi 2024-10-02).
---

# F33-P 설계 시트

## 1. 요약과 비유

commerce 의 모든 쓰기(장바구니 변경, checkout)는 api-gateway 가 user-service `POST /api/users/verify-token` 으로 토큰을 확인한 뒤에야 하류로 간다. 이 확인은 `user_schema.auth_tokens`(약 244만 행)에서 토큰 하나를 찾는 질의이고, token 열에 겹쳐 있는 두 인덱스(UNIQUE 제약 `auth_tokens_token_key`, `idx_auth_tokens_token`) 덕에 호출당 0.13ms 다.
운영자가 "같은 인덱스 두 개는 낭비"라며 하나로 합치는 마이그레이션을 돌린다: 새 유일 인덱스를 동시 생성(`CREATE UNIQUE INDEX CONCURRENTLY`)하고, 옛 둘을 지우고, 새 것의 이름을 옛 이름으로 바꾼다. 동시 생성은 마이그레이션 세션의 statement_timeout(2초)에 끊겨 INVALID 로 남는데 마이그레이션은 멈추지 않는다. 결과는 "이름은 있는데 아무도 못 쓰는 인덱스" 하나다. 토큰 확인이 매번 표 전체를 읽고, CPU 0.5 코어인 PostgreSQL 이 포화된다.

비유: 도서관이 색인 카드 두 벌을 한 벌로 합치려고 새 카드함을 만들다 중간에 멈췄는데, 옛 카드함 둘을 버리고 빈 새 카드함에 옛 이름표를 붙였다. 이름표는 그대로라 아무도 이상한 줄 모르고, 책을 찾을 때마다 서가 전체를 뒤진다.

## 2. 원본 사례

- **Vapi, 2024-10-02** ([공식 상태 페이지 사후 보고](https://status.vapi.ai/incident/438296)). 상태 페이지 시각(UTC) 16:15 API 저하와 통화 타임아웃, 16:38 DB 자원 확장(약 2분 완전 중단), 16:41 확장 뒤에도 CPU 최대, 16:59 병목 확인, 17:00 API 복구. 사후 보고: 늘어나는 부하에 `call` 테이블 질의를 빠르게 하려고 복합 인덱스를 더했는데, 생성이 UI 에서는 성공한 것처럼 보였지만 실패해 `INVALID` 로 남았고, 이어 옛 단순 인덱스를 지워 가장 큰 테이블에 쓸 수 있는 인덱스가 없어졌다("Human error on our end led us to being index-less on our biggest table `call`s"). DB CPU 100%, API 타임아웃, 쿠버네티스가 건강하지 않은 파드를 재시작해 부하가 더 나빠졌다. 집계 질의를 끄는 것으로는 풀리지 않았고, 인덱스가 `INVALID` 임을 찾아 다시 만들어 복구했다. `ref-real-world-incidents.md` M21 에 추가.

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 질의 성능을 높이려는 인덱스 변경(새 복합 인덱스 추가 후 옛 단순 인덱스 제거) | 겹친 token 인덱스 두 개를 하나로 합치는 마이그레이션(새 유일 인덱스 동시 생성 후 옛 제약과 인덱스 제거, 이름 바꾸기) |
| 원인이 된 결함 | 새 인덱스 생성이 실패해 `INVALID` 로 남았는데 성공으로 보고 옛 인덱스를 지움 | 동시 생성이 statement_timeout 2초에 끊겨 `INVALID` 로 남았는데 마이그레이션이 멈추지 않고 옛 둘을 지움. 테이블에 쓸 수 있는 token 인덱스가 없음 |
| 전파 경로 | 가장 큰 테이블의 고빈도 질의가 인덱스 없이 돎 → DB CPU 100% → API 타임아웃, 파드 재시작이 악화 | 쓰기마다 도는 토큰 조회가 약 244만 행 전수 스캔 → PostgreSQL(0.5 CPU) 포화 → user Hikari 고갈 → verify 500(또는 read timeout, user NotReady 때 연결 거부) → 게이트웨이가 확인을 자기 호출해 재시도, 서킷, fallback 없이 쓰기 500, user 가 Unhealthy 로 빠지고 재시작될 수 있음(원본의 '재시작이 악화'와 같은 꼴) |
| 사용자 증상 | API 요청 타임아웃, 통화 타임아웃 | commerce 인가된 쓰기(장바구니, checkout) 500, 조회 지연 |
| 탐지 경로 | 페이징(성능 저하), DB CPU 급증, 나중에 인덱스 `INVALID` 확인 | APM 오류와 지연, DPM Top SQL 의 토큰 조회 블록 급증과 세션, KCM user Unhealthy |
| 완화와 복구 | 인덱스 재생성 | cleanup: INVALID 인덱스 제거, 일반 인덱스 먼저 재생성(유효해지면 스캔이 멈춤), 유일 인덱스 재생성과 제약 부착 |

기전이 같다: "인덱스 변경 중 새 인덱스가 INVALID 로 남은 줄 모르고 옛 인덱스를 지워, 고빈도 질의가 인덱스 없이 돌아 DB 포화". 대상과 규모(PostgreSQL 16, 244만 행, 0.5 CPU)만 우리 스택에 맞췄다. 동시 생성을 끊는 수단으로 statement_timeout 을 쓴 것은 원본이 실패 이유를 밝히지 않아 PostgreSQL 이 문서로 보장하는 실패 경로 하나를 고른 것이다(취소된 `CREATE INDEX CONCURRENTLY` 는 INVALID 항목을 남긴다).

로컬 실측(postgres:16-alpine, 같은 스키마, 240만 행, 2026-10-09): statement_timeout 2s 에서 CIC 가 'canceling statement due to statement timeout' 으로 끊기고 `idx_auth_tokens_token_new` 가 `indisvalid=f, indisready=f, 0 bytes` 로 남았다. 제약과 인덱스를 지우고 이름을 바꾼 뒤 `EXPLAIN` 은 Parallel Seq Scan, `CREATE INDEX IF NOT EXISTS idx_auth_tokens_token` 은 'already exists, skipping'. cleanup 순서(DROP INDEX → CREATE UNIQUE INDEX → ADD CONSTRAINT ... USING INDEX → CREATE INDEX)로 `pg_get_indexdef` 와 제약이 원래와 같아졌다. 실행기 스크립트도 kubectl 대역(shim)으로 같은 컨테이너에 preflight, run, cleanup, recovery, 재 cleanup 을 돌려 모두 통과했고, 생성이 끝나 버린 경우(statement_timeout 120s)는 새 인덱스를 지우고 rc=4 로 멈추며 원래 상태가 남았다.

## 3. 숫자 근거 (2026-10-09, `scenario-stats.py`, 정식 + 후보)

- 서비스: commerce 24, core-banking 12, food-delivery 10. commerce 가 가장 많아 **정답 위치가 0 인 부품만** 낸다(원칙 2, 2026-10-08 완화).
- 묶음 F(느린 쿼리): 2(정식 2, 후보 0) → 3 (47 중 6%). 0 인 묶음은 K, N 이고 둘 다 이번 회차 후보가 막혔다(아래).
- 정답 위치: DB 테이블(인증 토큰) 0 → 1. 결제 경로 21%(금지), 주문 서비스 13%, 외부 결제 13% 와 겹치지 않는다. DB 정답 후보는 목록에서 2개(이 후보와 은행 시퀀스).
- 쏠림 기록(적대적 평가 권고): 이 후보로 DB 계열 정답 위치(DB 테이블 + DB 인스턴스 + DB 계정) 합계가 47 중 13(27.7%), commerce PostgreSQL 이 정답이거나 정답 테이블을 가진 시나리오가 6개(F01-R, F06-H, F15-T1, F15-H, F25-H, F33-P)가 된다. 위치별 20% 상한은 지키지만 쏠림이 크다. 장부 §6 에 적었고 다음 후보는 DB 밖 부품을 먼저 본다.
- F33-R 과의 관계(적대적 평가 권고): 인덱스 상실 → 전수 스캔 → 0.5 CPU DB 포화 → Hikari(10) 고갈 → 호출자 실패까지 같고, INVALID 와 이름 바꾸기 차이는 119 에 직접 보이지 않으며 정답도 요구하지 않는다. 그래서 '같은 원인, 다른 대상'에 가깝다. 그런데 F33 사례군의 R 은 이미 F33-R 이 쓰고 있어 P(일부만 비슷)로 둔다. 서비스 이름만 바꾼 복제는 아니다: DB 엔진(PostgreSQL), 테이블과 문장, 그리고 질의가 불리는 경로가 다르다. 이 시나리오는 모든 인가된 commerce 쓰기의 토큰 확인이고, F33-R 은 food 주문 용량 확인이다. 실패하는 진입 경로도 장바구니와 checkout 의 게이트웨이 500 대 food 주문 503 으로 다르다. 정답지 contrast_with 와 distinguishing_evidence 에 녹화로 가르는 근거(Top SQL 문장, DB, 실패 진입 경로)를 적었다.
- 부품 지도: commerce 의 user 는 정답 1(F16-H)이지만 이 후보의 정답은 user 서비스가 아니라 PostgreSQL 의 auth_tokens 테이블이다. 장부 §2-1 에 이 테이블을 정답으로 한 시나리오는 없다.

### 후보 목록 (3단계, "실제 기전 × 0 인 부품 또는 인프라 층", 숫자 순)

| # | 후보 | 원본 사례 | 묶음/정답 위치 | 결과 |
|---|---|---|---|---|
| 1 | food 워커 tb-w2/tb-w3 VXLAN 대역 제한(tc)으로 교차 노드 파드 통신 손실 | Slack 2021-01-04(TGW 포화로 패킷 손실) | K/노드 | 버림(원칙 1, 4): 원본은 부하 급증에 따른 용량 포화, 재구성은 수동 tc 설정이라 기전이 다르고, banking 파드의 DNS 조회도 tb-cp CoreDNS 로 VXLAN 을 타서 F37-R(이름 해석 실패) 증상과 섞인다. network.fault 껍데기 수단(F13-H)과 같은 벽 |
| 2 | commerce gateway 재시도 정책 오배포(백오프 없이 재시도 확대)로 작은 지연이 폭주로 | Huang 2022 메타스테이블 | N/게이트웨이 | 버림(원칙 9, 5): 재시도는 실패가 있어야 늘어 계기가 따로 하나 더 필요하고(복합 장애), 하류가 ms 단위라 3배 증폭으로도 포화 계산이 서지 않는다 |
| 3 | **commerce auth_tokens 인덱스 교체 마이그레이션 실패(INVALID)로 토큰 확인 전수 스캔** | **Vapi 2024-10-02** | **F/DB 테이블(인증 토큰) 0** | **선택** |
| 4 | commerce cart Redis maxmemory noeviction 으로 캐시 쓰기 거절, 묵은 장바구니 읽기 | OutSmart 2026-05-19(Redis 메모리 가득) | B·H/캐시(1) | 버림(원칙 7): CartService 가 쓰기 실패를 삼키고(writeThroughCache catch) 읽기는 CB 와 DB 폴백이 막아 5xx 가 없고, 남는 것은 묵은 장바구니로 인한 4xx(400/409)나 조용한 데이터 오류다(F34-R 교훈) |
| 5 | commerce user 토큰 수명 설정(auth.token-ttl-hours) 0 배포로 모든 쓰기 401 | GitHub 2026-06-10(인증 조회 실패로 401) | G/사용자 서비스(1) | 버림(F34-R 교훈, 원칙 1): 5xx, 지연 변화 없는 401 단일 신호이고, 원본은 잘못된 호스트 설정이라 기전도 다르다 |
| 6 | banking Oracle 시퀀스, IDENTITY MAXVALUE 를 낮춰 이체 INSERT 실패 | GitHub 2026-05-06(32비트 키 최대값) | M5/DB 테이블 | 버림(원칙 1): id 가 NUMBER 라 한계가 없고 MAXVALUE 를 낮추는 것은 인공적이다(Basecamp INT 소진 반려와 같은 벽) |
| 7 | commerce user-service(maxSurge 0) 를 다른 CPU 아키텍처 이미지로 롤아웃 | Harness 2025-10-28, Pipefy 2024-05-22 | J/사용자 서비스(1) | 버림(카탈로그 §1, 원칙 2): F17-H(이미지 부재 롤아웃, 엔드포인트 비움)와 같은 증상 계열이고 commerce 는 정답 위치 0 인 부품만 낸다 |
| 8 | banking transfer 보존 기간 설정 0 배포로 끝난 이체 삭제 | Azure DevOps 2023-05-24(정리 작업이 운영 DB 삭제) | M22/은행 이체 | 버림(원칙 7): 보존 배치는 끝난 이체만 지워 사용자 경로에 실패가 없다(조용한 데이터 손실) |
| 9 | food order JVM 힙 비율(MaxRAMPercentage) 오배포로 OOMKill | (공식 사례 없음) | A/주문 서비스 | 버림(원칙 1, 카탈로그 §1): F05-R, F09-H 와 같은 주입 꼴의 서비스 교체이고 원본 사례가 없다 |
| 10 | tb-w2 시계 어긋남(chrony 정지 후 시각 이동) | Cloudflare 2017-01-01(윤초) | 시계/노드 | 버림(원칙 1, 9): 노드 사이에 시각을 검증하는 경로(TLS, 서명 토큰)가 없어 피해 계산이 서지 않는다 |

이번 실행에서 이미 막힌 후보(파드 우선순위 선점, kubelet 인증서, CNI MTU, gateway ORDER/USER URL, 스레드·연결 풀 최소화, cart Redis 비활성, food 비동기 계열)와 rejected 목록의 원본, 주입은 목록에서 뺐다. 음식배달과 은행 후보는 위 표의 1, 6, 8, 9 와 rejected 의 2026-10-08~09 항목들(연결 한도, 계정 잠금, 잠금 대기열, 스케줄러, Kafka 등)로 막혀 있어 commerce 의 0 인 정답 위치로 갔다.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 계기와 근본(인프라 지점): commerce `testbed-postgres-0`(PostgreSQL 16.14, CPU limit 500m, memory 512Mi, `commerce/k8s/10-postgres.yaml:68-72`, shared_buffers 128MB) 의 `user_schema.auth_tokens`. 2026-10-09 기준 약 244만 행, 테이블 238MB, `auth_tokens_pkey` 52MB, `auth_tokens_token_key` 178MB, `idx_auth_tokens_token` 178MB. 마이그레이션 DDL:
   - `CREATE UNIQUE INDEX CONCURRENTLY idx_auth_tokens_token_new ON user_schema.auth_tokens (token);` (세션 statement_timeout 2s → 취소, INVALID)
   - `ALTER TABLE user_schema.auth_tokens DROP CONSTRAINT auth_tokens_token_key; DROP INDEX user_schema.idx_auth_tokens_token; ALTER INDEX user_schema.idx_auth_tokens_token_new RENAME TO idx_auth_tokens_token;`
2. `commerce/api-gateway/src/main/java/com/commerce/gateway/controller/GatewayProxyController.java:28-52` (authGuard.verifyIfWrite) → `AuthGuard.java:31-63` (verifyIfWrite 가 44행에서 같은 클래스의 verifyToken 을 직접 부른다. 자기 호출이라 Spring AOP 프록시를 지나지 않아 @Retry/@CircuitBreaker user 와 verifyTokenFallback(401)이 적용되지 않는다. 적대적 평가에서 짚었고 F16-H 녹화 case-f16-h-v3-2f0555c7 의 gateway SEVERE 'Servlet.service() ... ResourceAccessException ... verify-token' 24건으로 확인됨) → `GlobalExceptionHandler.java:12-20` (ServiceException 만 처리) → 확인 실패는 처리되지 않은 예외로 쓰기 500, gateway SEVERE 로그
3. `commerce/user-service/src/main/java/com/commerce/user/controller/UserController.java:31-32` (verify-token) → `UserService.java:88-94` (verifyToken) → `AuthTokenRepository.java:10` (findByToken: `where at1_0.token=$1`)
4. `commerce/user-service/src/main/resources/application.yml:11-15` (hikari maximum-pool-size 10, connection-timeout 3000) → 풀이 스캔에 묶이면 3초 뒤 500
5. `commerce/api-gateway/src/main/resources/application.yml` 의 user 서킷과 재시도 설정(51-57, 102-106)은 토큰 확인 경로에 적용되지 않는다(위 4). 재시도로 부하가 늘지도, 서킷으로 줄지도 않는다. 게이트웨이 user read-timeout 은 10초(RestClientConfig)
6. `commerce/k8s/25-user-service.yaml:90-103` (readiness, liveness 가 /actuator/health, DB 확인 포함) → user Unhealthy, 재시작 가능
7. `commerce/user-service/src/main/resources/application.yml:26-28` + `schema.sql:29-37` (기동마다 `CREATE INDEX IF NOT EXISTS idx_auth_tokens_token` → 같은 이름의 INVALID 인덱스가 있어 건너뜀, 스스로 낫지 않음)

## 5. 원인 규정

- 근본(`root_cause.target_id`): `commerce-postgres:user_schema.auth_tokens` (database). 결함은 이 테이블에 token 질의를 받칠 유효한 인덱스가 없는 상태다. 고쳐야 재발이 막히는 곳도 여기(인덱스 재생성)다.
- 계기(`trigger_target_id`): 비움. 계기(DDL)가 근본과 같은 테이블에서 일어났다(F33-R, F06-H 와 같은 규칙).
- 부분 점수: `commerce-user`(증상과 오류 로그가 나는 서비스), `commerce-postgres`(인스턴스까지는 맞음). 게이트웨이는 쓰기 500 과 SEVERE 로그를 내지만 증상 전달자라 부분 점수에도 넣지 않는다.
- 원칙 6: 게이트웨이와 user-service 의 요청은 정당하다(코드, 설정, 요청량 그대로). 잘못된 것은 테이블 쪽 변경이다. 원본 사후 보고도 "인덱스 없는 가장 큰 테이블"을 원인으로 지목한다.
- 원칙 5: "어느 질의가 왜 느려졌나"는 DPM Top SQL(호출당 블록), 세션, DB 크기 변화로 관제 데이터에서 바로 나온다. 동시 생성이 왜 끊겼는지(statement_timeout)는 정답에 요구하지 않는다.

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 |
|---|---|
| 감지 | commerce APM 오류율과 지연(user 500, gateway 쓰기 500), commerce-gateway SEVERE verify-token 과 commerce-user ERROR 로그 급증(보존 7일 안 0), KCM testbed-user Unhealthy, DPM 활성 세션과 seq_scans 급증. 401 은 평시에도 토큰 없는 장바구니 쓰기의 약 25%(gateway SERVER 스팬, 평가 실측)라 증상 신호로 쓰지 않는다 |
| 피해 판정(러너) | 동반 부하 checkout 5xx 비율 ≥ 0.5 와 2xx 비율 < 0.3 (3틱 연속) |
| RCA | DPM Top SQL 토큰 조회의 호출당 블록 5 → 약 3만, 같은 분 used_size 약 350MB 감소, 롤아웃 없음, PostgreSQL 재시작 없음 |

## 7. 관측 근거 표 (119 실조회, 평시)

| 증거 | 119 표와 칸 | 조회와 결과 | 수집 |
|---|---|---|---|
| 근본: 토큰 조회의 호출당 비용 | CH `lucida.dpm_topsql_local`(`sql_hash`, `body` 의 `calls`, `avgExecTime`, `sharedHit`, `sharedRead`, `sqlText`) | `SELECT sql_hash, count(), avg(JSONExtractFloat(body,'avgExecTime')), avg((sharedHit+sharedRead)/calls) FROM lucida.dpm_topsql_local WHERE engine='postgresql' AND timestamp > now() - INTERVAL 8 DAY AND positionCaseInsensitive(body,'auth_tokens')>0 GROUP BY sql_hash` → 토큰 조회 `select at1_0.id,...,at1_0.user_id from user_schema.auth_tokens at1_0 where at1_0.token=$1` sql_hash 618727859340325129: 8일 618 스냅샷, avgExecTime 평균 0.129ms, 호출당 블록 5, 스냅샷당 calls 최대 3,593. 평시에는 상위 문장에 간헐적으로만 잡힌다(마지막 2026-10-07 09:22, 최근 1일 0행): 빠른 문장이라 상위 N 밖에 있는 것이 정상이고, 주입 뒤 상위로 올라오는 것 자체가 신호다. 같은 표의 토큰 INSERT 는 8,290 스냅샷 1.47ms. 주입 뒤 같은 sql_hash 가 호출당 약 3만 블록, 수백 ms~수 초가 돼야 한다(읽기 전용 실측: 인덱스를 끈 세션의 EXPLAIN ANALYZE 970ms, shared hit 119 + read 30,354) | 전수(DPM 폴링, 상위 문장) |
| 근본: 활성 세션 | CH `lucida.dpm_session_local`(engine postgresql, `body` 의 `sqlHash`, `queryTime`, `waitType`) | 최근 1시간 postgresql 행 64개(분당 1회 활성 세션 표본, 대부분 모니터링 계정). 주입 뒤 user-service 세션이 토큰 조회를 수 초씩 도는 표본이 늘어야 한다 | 표본(분당) |
| 계기: DB 크기 변화 | VM `dpm.postgresql.database.used_size{db_name="commerce"}` | 8일 60초 표본 11,520 개, 분당 변화 대부분 -57~+0.9MB, 250MB 넘게 줄어든 분이 4번(10-04 03:09, 03:31, 10-07 09:05, 10:05 — 운영 정리 시각). PostgreSQL 서버 로그는 수집 경로가 없다(kcm_pod_logs_local 7일 0행). 그래서 계기는 '같은 분의 호출당 블록 급증 + 약 350MB 감소'로만 남는다. 인덱스 두 개(178MB×2) 제거면 약 350MB 준다. 단독 결정 증거가 아니라 Top SQL 의 변화 시각과 맞춰 보는 보조 증거 | 전수(DPM 폴링) |
| 계기/근본: 순차 스캔 수 | VM `dpm.postgresql.sql.seq_scans{db_name="commerce"}` | 24시간 p50 16.4, p99 29.5, 최대 31.0. 주입 뒤 토큰 확인 초당 수 건이 모두 순차 스캔이 되어 크게 오른다(DB 단위 지표라 어느 표인지는 Top SQL 로 가른다) | 전수(DPM 폴링) |
| 전파: gateway 와 user-service 오류 | CH `lucida.lucida_logs_local`(service_name `commerce-gateway`, `commerce-user`) | 로그 보존은 약 7일(최소 timestamp 2026-10-02 07:17)이고 그 안에 commerce-user 로그 0건(파드 기동 2026-10-01 이 보존 이전이라 기동 로그도 없음, user 는 정상 경로에서 로그를 남기지 않는다), commerce-gateway 의 'verify-token' 로그 0건. F16-H 녹화(case-f16-h-v3-2f0555c7)에서는 user 가 빠진 동안 gateway SEVERE 'Servlet.service() ... ResourceAccessException ... verify-token' 24건이 남았다. 같은 배포 형태의 commerce 서비스는 DB 풀 고갈 때 'HikariPool-1 - Connection is not available, request timed out after ...ms' ERROR 와 'Servlet.service() ... threw exception' SEVERE 를 남겼다(inventory, product, cart, 20일). user 도 OTEL_LOGS_EXPORTER=otlp 로 같은 설정이다(109 kubectl) | 전수(로그) |
| 전파: user 프로브 | CH `lucida.kcm_events_local`(namespace, object_name, reason) | 8일 rca-testbed-commerce 이벤트 중 testbed-user 0건(Unhealthy 236건은 모두 다른 파드, 10-06 다른 실행). 주입 뒤 testbed-user Unhealthy, Killing 이 생길 수 있다 | 전수(KCM) |
| 피해: 쓰기 경로 | CH `lucida.otel_traces_local`(service_name, span_name, duration_ns) | 1시간: commerce-user `POST /api/users/verify-token` 추정 분당 160건, p50 1.7ms, p95 2.3ms(DB 스팬 `SELECT user_schema.auth_tokens` p50 0.3ms). gateway `POST /api/carts/**` p50 8.8ms, `POST /api/orders/**` p50 49.8ms | 표본 10%(보조) |
| 롤아웃 없음 | CH `lucida.kcm_events_local` reason ScalingReplicaSet | 8일 commerce ScalingReplicaSet 16건, 마지막 10-02. 주입 구간에 없어야 한다 | 전수(KCM) |

핵심 증거(Top SQL, DB 크기, 로그, KCM)는 표본 데이터가 아니다. 트레이스와 APM 지표는 보조로만 쓴다.

## 8. 감별

- must_support: 정답지 `must_support` 6개(Top SQL 블록, used_size·seq_scans, user 로그, KCM user, 러너 피해, 롤아웃·재시작 없음).
- must_rule_out: F16-H(user 프로브 오설정, DB 한가), F25-H(PostgreSQL 다운), F20-R 꼴 무거운 질의 부하, 부하·자원 한도 변경, 게이트웨이 설정 오배포.
- contrast_with: F33-R(같은 원인 꼴, MySQL 단순 제거, 주문 503), F16-H(같은 commerce 쓰기 실패와 user 경로, 원인 user readiness, DB 한가), F20-R(같은 PostgreSQL 포화, 원인 부하), F25-H(같은 공유 DB, 원인 프로세스 사망).

## 9. 러너 판정과 강도, 부하 계산

- 강도 하나 고정(`approved-fixed-f33-p`, min_hold 5m, settle 60s, timeout 10m), max_injection_duration 20m, 동반 부하 surge.js 3rps(ramp 2m, hold 16m).
- 비용: 인덱스 없는 토큰 조회 한 번 = 약 3만 블록, 0.5 CPU 한도에서 970ms(병렬 작업자 2, CPU 시간 약 0.5초로 본다).
- 수요: 동반 부하 3rps × checkout 50% × 인가 쓰기 3(장바구니 비우기, 담기, checkout) = 초당 4.5건. 기준선은 KST 15시 실측 분당 160건(초당 2.7), 새벽(2rps)에는 그 약 1/5. 합계 초당 5~7건 × 0.5 CPU초 = 2.5~3.6 CPU초/초 > 0.5 코어. 시간대와 무관하게 5배 이상 넘친다. 기준선만으로도 피크에는 1.35 > 0.5 다.
- 동시성 상한: user Hikari 10 이 동시 스캔을 10개로 묶고(병렬 작업자는 max_parallel_workers 8 안), 나머지 확인은 3초 뒤 500. 게이트웨이는 확인을 재시도하지도 서킷으로 끊지도 않는다(자기 호출, §4). 그래서 부하가 증폭되지도 줄지도 않고 들어오는 확인이 모두 user 풀 앞에 줄 선다. user 가 NotReady 인 동안은 연결 거부로 즉시 500 이라 그 사이 PostgreSQL 부하가 잠깐 준다.
- 진입점: commerce 진입(nginx → gateway)은 DB 를 쓰지 않아 entry_status 0 이 나지 않는다. 가장 긴 쓰기는 verify read-timeout 10초에 order read-timeout(15초 × 재시도 2)을 더해도 k6 기본 60초 안이다. 게이트웨이 Tomcat 스레드는 쓰기 초당 약 7건 × 최대 10초 ≈ 70개로 200 안이다.
- 재고: 실패한 checkout 은 재고를 쓰지 않아 F40-R 의 재고 소진 걱정이 없다. 3rps 는 F40-R(6rps)보다 작다.
- 성공 신호 지속: surge.js 는 VU 별로 토큰을 캐시해(만료 24시간) 로그인이 막혀도 캐시된 토큰으로 checkout 표본이 이어진다. 새 VU 의 로그인이 실패하면 그 여정은 checkout 을 내지 않고 끝난다. 동반 부하가 램프 2분 동안 주입과 함께 시작되므로(러너는 companion 을 먼저 적용하고 곧바로 primary) 캐시된 토큰을 가진 VU 가 적을 수 있고, 로그인까지 막히는 틱에는 checkout 표본이 0 이 되어 비율이 비고(usable 아님) 성공 판정이 늦어질 위험이 있다. 로그인은 user 풀(10)에서 스캔 사이 빈 연결을 얻으면 지나가므로 0 이 계속되지는 않는다고 보지만, 첫 실행에서 확인할 항목이다(운영 메모).
- cleanup 소요 추정: 러너는 primary cleanup 을 먼저 하고 동반 부하를 나중에 멈춘다(production_runtime.py cleanup_order). 그래서 cleanup 은 포화 중에 돌고, 이를 줄이려고 실행기 cleanup 을 **일반 인덱스 먼저** 만들게 했다(유효해지는 순간 토큰 조회가 인덱스를 타 스캔이 멈춘다). 로컬(제한 없는 CPU)에서 재생성 각 4.3~4.8초. 109 의 arm64 0.5 CPU 에서 단독이면 그 2~4배(약 10~20초), 스캔 10개와 CPU 를 나누면 약 10배 더해 첫 인덱스 최악 약 3~4분으로 본다. 첫 인덱스 뒤에는 포화가 풀려 유일 인덱스는 단독 시간(수십 초)에 끝난다. 합계 5분 안팎으로 cleanup timeout 15m, 실행기 상한 900초 안이다. 재생성 동안 로그인(토큰 INSERT)이 기다린다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 사례(Vapi 2024-10-02, 공식 상태 페이지 사후 보고)와 요소별 대응표, 기전 동일(§2)
- [x] 장부 갱신, 묶음 F(정식 + 후보 2→3, 6%), 정답 위치 DB 테이블(인증 토큰)(0), 결제 경로 21%(무관), 서비스 commerce 24(정답 위치 0 이라 허용), 고른 이유(§3). 어느 축도 20% 미만
- [x] 근본 위치: 인프라 지점 testbed-postgres-0 user_schema.auth_tokens 와 코드 anchor(§4)
- [x] 근본 흔적 119 조회: dpm_topsql_local sql_hash 618727859340325129(호출당 5블록, 0.129ms), VM used_size, seq_scans(§7)
- [x] 핵심 증거가 표본 데이터에만 있지 않음: DPM 폴링, 로그 전수, KCM(§7)
- [x] 계기 흔적: 같은 분의 Top SQL 블록 급증과 used_size 감소, seq_scans 급증. DDL 자체가 Top SQL 에 남으면 직접 증거가 하나 더 생긴다. 인공 지연 없음
- [x] 정답이 관제 데이터로 낼 수 있는 결론(어느 테이블의 어느 질의가 인덱스를 잃었나). 코드 설계 결함 추론 불필요
- [x] 정답지 세 칸이 원칙 6대로(§5)
- [x] 감지, 피해 판정, RCA 증거 구분(§6)
- [x] 주입 도구가 시나리오 id 를 남기지 않음: psql 은 kubectl exec 로 파드 안에서 기본 application_name(psql)으로 돌고, 인덱스 이름은 실제 마이그레이션이 쓸 이름이다. 행 수 확인은 LIMIT 10만으로 묶었다. k6 `--tag scenario_id=F33-P` 는 tb-runner 로컬 지표 태그(기존 시나리오와 같음)
- [x] 부하 상한과 서킷을 고려한 피해 계산(§9, 토큰 확인 경로에는 재시도와 서킷이 적용되지 않음을 반영)

## 11. 운영 메모 (녹화 검증 때 확인)

- 러너 success 는 "토큰 확인 전수 스캔"과 "다른 이유의 commerce 쓰기 실패"를 가르지 못한다. 녹화본에서 (1) Top SQL sql_hash 618727859340325129 의 호출당 블록이 수만으로 올랐는지 (2) used_size 가 약 350MB 줄어든 분이 (1)이 처음 나타난 분과 같은지(둘이 같은 분이어야 계기 흔적이 선다) (2-1) gateway SEVERE verify-token 이 있는지 (3) 롤아웃과 PostgreSQL 재시작이 없는지 (4) cleanup 뒤 실행기 recovery 가 통과했는지(두 인덱스 정의와 제약 복원)를 본다.
- 첫 실행에서 checkout 표본이 0 인 틱이 이어지는지(§9 성공 신호 지속), cleanup 소요 시간(§9 추정 5분 안팎)을 기록한다.
- cleanup 이 끊겨 INVALID 인덱스나 빠진 제약이 남으면 다음 실행의 preflight 가 막힌다. 그때는 db.ddl cleanup 을 다시 돌린다(멱등: INVALID 인덱스만 지우고 없는 것만 만든다).
