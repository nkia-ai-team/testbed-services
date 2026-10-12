---
title: F71-R 설계 시트 — commerce PostgreSQL 앱 역할의 비밀번호 기한이 지나 여덟 서비스의 풀이 수명대로 빔
status: Draft
owner: project
last_reviewed: 2026-10-12
tags:
  - scenario
  - design
  - database
  - credential
summary: commerce PostgreSQL 애플리케이션 역할 commerce 의 비밀번호 유효 기한(VALID UNTIL)이 갱신 없이 지나, 열린 연결은 그대로 쓰이지만 새 로그인이 모두 'password authentication failed' 로 거절되고, 이 역할을 함께 쓰는 여덟 commerce 서비스의 Hikari 풀이 수명(10분, 30분)대로 비어 readiness 에서 빠지고 재시작을 되풀이해 commerce 로그인, 쓰기, 상품 조회가 실패하는 시나리오(GitHub 2025-03 자격 증명 만료 재구성)의 설계 근거.
---

# F71-R 설계 시트

- id `F71-R`, slug `f71-r-commerce-postgres-app-account-password-expiry`, 새 사례군 F71(원본 GitHub 2025-03, 앞선 시나리오에 없던 원본)
- 상태: 후보(ready + `stage: candidate`), 설계 강도 하나로 고정한 evaluation 모드, 녹화 대기 큐에 추가
- 묶음 O(자격 증명 만료, 회전 실수), 정답 위치 DB 계정, 서비스 쇼핑몰

## 1. 요약과 비유

commerce 의 여덟 서비스(user, cart, pricing, shipping, product, order, payment, inventory)는 PostgreSQL 에 역할 하나 `commerce` 로
네트워크 비밀번호 로그인을 한다(Secret `postgres-secret` 의 `POSTGRES_USER`, pg_hba `host all all all scram-sha-256`).
이 역할의 비밀번호에 유효 기한(VALID UNTIL)이 걸려 있었고, 만료 임박 경보가 제 역할을 못 해 아무도 갱신하지 않은 채 그 시각이 지난다.
PostgreSQL 은 기한을 비밀번호 로그인 때만 보므로 열린 연결은 그대로 쓰이고, 새 로그인만 `FATAL: password authentication failed for user "commerce"` 로 거절된다.
Hikari 풀은 연결을 max-lifetime 까지만 쓰고 새로 만들지 못한다. user, cart, pricing, shipping(600000ms)은 10분 안에,
product, order, payment, inventory(Hikari 기본 30분)는 30분 안에 풀이 빈다. 풀이 빈 서비스는 `/actuator/health` 의 DB 확인이 실패해
readiness 에서 빠지고, 같은 경로를 보는 liveness 가 컨테이너를 재시작하며, 새 컨테이너는 기동 중 풀을 열지 못해 재시작을 되풀이한다.
게이트웨이는 DB 가 없어 살아 있다. user 가 빠지면 쓰기마다 하는 토큰 확인이 실패해 쓰기 500, 로그인 502 가 되고, product 가 빠지면 상품 조회도 502 다.

비유: 사무실 여덟 곳이 출입증 하나를 함께 쓰는데 출입증에 유효 기간이 찍혀 있었고 갱신 알림이 고장 났다.
이미 안에 들어와 있는 사람은 계속 일하지만, 나갔다 다시 들어오려는 사람은 "출입증이 유효하지 않습니다"에 막힌다.
교대 주기가 짧은 사무실(10분)부터 비고, 긴 사무실(30분)이 나중에 빈다. 건물(DB)은 멀쩡하고 출입증의 날짜만 지났다.

## 2. 원본 사례

- **GitHub, 2025-03-29~31**, 월간 가용성 보고(2025년 3월) [공식](https://github.blog/news-insights/company-news/github-availability-report-march-2025/). 2026-10-12 WebFetch 로 원문 확인, `docs/ref-real-world-incidents.md` M7 에 이번에 추가.
  - "Between March 29 7:00 UTC and March 31 17:00 UTC, GitHub experienced service degradation due to two separate, but related incidents."
  - 3월 29일: 마케팅 이메일 구독 해지 불가, 원인 "expired credentials used for an internal service".
  - 3월 31일 7:00~16:40 UTC: 전자책, 행사 등록 양식 제출 불가, "a similar credential expiry".
  - 근본 원인: "an issue in the automated alerting for monitoring upcoming credential expirations" — 만료가 지난 뒤에야 알았고 영구 수정 전에 두 번 났다.
  - 완화: 자격 증명 갱신과 해당 서비스 재배포. 재발 방지: "enhanced our credential expiry detection, alerting, and rotation processes".
- 원본은 자격 증명의 종류(DB 인지)와 영향 비율을 밝히지 않는다. 재구성은 대상을 "내부 서비스가 쓰는 자격 증명" 중 이 테스트베드에 실제로 있는 것, DB 역할의 비밀번호로 둔다.

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 내부 서비스가 쓰던 자격 증명의 만료 시각이 지남(사람의 조작 없음) | commerce 역할 비밀번호의 VALID UNTIL 시각이 지남(실행기가 기한을 지금으로 둔다 = 기한 도달) |
| 원인이 된 결함 | 만료 임박 경보의 문제로 갱신이 안 된 자격 증명 | 갱신되지 않은 채 기한이 지난 비밀번호. 서비스는 그 비밀번호를 그대로 씀 |
| 전파 경로 | 그 자격 증명으로 하는 인증 실패 → 그 서비스에 기대는 기능(구독 해지, 등록 양식) 실패 | 새 로그인 거절 → 풀이 수명대로 빔 → health DOWN, readiness 이탈, liveness 재시작, 기동 실패 반복 → 게이트웨이 쓰기 500, 로그인 502, 상품 조회 502 |
| 사용자 증상 | 구독 해지, 양식 제출 불가 | 로그인, 장바구니, checkout 불가(약 10분 뒤), 상품 조회와 검색 불가(약 30분 뒤) |
| 원인 쪽 상태 | 인증 주체는 멀쩡, 자격 증명만 무효 | PostgreSQL 인스턴스, DPM 수집, local 관리 접속 정상. commerce 역할의 비밀번호 로그인만 거절 |
| 탐지된 경로 | 만료가 지난 뒤 기능 실패로 앎(경보 실패) | 여덟 서비스 로그의 같은 인증 거절 문구, 풀 지표 0, DPM 세션 감소, KCM 재시작 |
| 완화와 복구 | 자격 증명 갱신, 서비스 재배포 | cleanup 이 기한을 없애고(VALID UNTIL 'infinity') 재시작 반복 중인 파드가 다음 재시작에서 붙음 |

기전 확인: "서비스가 쓰던 자격 증명이 만료되어 그 자격 증명으로 하는 인증이 실패하고, 그 인증에 기대는 기능이 멈춘다"가 원본과 같다.
원인 쪽 상태(인증 주체는 정상, 자격 증명만 무효)와 완화(자격 증명 갱신)도 같다. 피해가 만료 직후가 아니라 풀이 비는 순간부터 나는 것은
원본이 말하지 않은 부분이고 우리 스택(Hikari 연결 풀, PostgreSQL 이 기한을 로그인 때만 봄)의 성질이다. 로컬 postgres:16-alpine 실측(2026-10-12):
슈퍼유저 역할도 VALID UNTIL 이 지나면 열린 연결은 질의를 계속 처리하고, 새 네트워크 로그인은 클라이언트에 `FATAL: password authentication failed for user "commerce"`,
서버 로그에만 `DETAIL: User "commerce" has an expired password.` 로 거절되며, `VALID UNTIL 'infinity'` 직후 로그인이 된다. local 소켓(trust)은 기한과 무관하다.

## 3. 숫자 근거 (2026-10-12, `scenario-stats.py`, 정식 + 후보 105 종, 이 후보 전)

- 서비스: commerce 35, core-banking 35, food-delivery 35. 셋이 동률이라 어느 것이든 된다(원칙 2, 2026-10-12 결정). **commerce 35 → 36**.
- 묶음: A 8, B 7, C 6, D 16, E 4, F 3, G 12, H 2, I 2, J 17, K 2, L 8, M 1, **O 2 → 3**, P 13, Q 2. 20% 상한(21)에 닿는 묶음 없음(가장 큰 J 16%).
  O 는 3/106 = 2.8%. 현실 근거: Ghosh 2022 에서 배포 오류의 55%(전체의 약 11%)가 인증서 만료나 회전 실수인데 테스트베드에는 2개뿐이다.
- 정답 위치: **DB 계정 3 → 4**(4/106 = 3.8%). 가장 많은 은행 이체 서비스 9(8.5%). 결제 경로 13/106 = 12.3%(무관).
- 왜 이 후보인가: O 묶음과 DB 계정 정답이 적고, commerce 에는 O 가 F65-P(배포 쪽 실수) 하나뿐이다. 겉 증상이 F65-P(user 의 'password authentication failed')와 겹치고
  정답이 달라(배포 정의가 아니라 DB 쪽 자격 증명) 관제 AI 가 롤아웃 유무, 거절된 역할, 영향 서비스 수로 추론해야 맞힌다(카탈로그 관계 H 의 가치).
  같은 "새 로그인만 거절, 풀이 수명대로 빈다" 고리의 F35-R 과는 원본, 엔진, 계기(잠금 명령이 아니라 기한 경과), 피해 모양(liveness 재시작 반복, 30분에 걸친 두 물결)이 다르다.
- 이번 반복(67)의 후보 목록과 버린 이유는 §11.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 역할과 접속: `commerce/k8s/01-secrets.yaml:9-10`(POSTGRES_USER commerce, POSTGRES_PASSWORD), 여덟 서비스 매니페스트의 `DB_USER`/`DB_PASS` 가 같은 키
   (`commerce/k8s/25-user-service.yaml:56-65`, 20, 21, 22, 23, 26, 27, 28 의 DB_USER 줄). notification 은 매니페스트에 DB env 가 있지만 datasource 설정이 없어 풀 지표가 없다(대상 아님).
2. 109 읽기 전용 조회(2026-10-12 09:42 UTC, `BEGIN READ ONLY`): PostgreSQL 16.14, 로그인 역할 `commerce`(슈퍼유저, rolvaliduntil NULL), `lucida_mon`.
   pg_hba: local, 127.0.0.1, ::1 trust, 그 밖의 host `scram-sha-256`. commerce 연결은 파드 IP 8개에서 56개(10, 10, 10, 10, 5, 5, 3, 3), backend_start 가 09:13~09:42 로 흩어져 있다(30분 수명 풀의 연결 생성 시각이 퍼져 있음).
3. 풀 설정: `commerce/user-service/src/main/resources/application.yml:7-17`(10, minimum-idle 3, connection-timeout 3000, max-lifetime 600000),
   cart `:7-17`(20, 5), pricing `:7-16`(15, 5), shipping `:7-16`(10, 3) 모두 max-lifetime 600000. product, order, payment, inventory `:7-20` 은 hikari 설정이 없어 Hikari 기본값(maximumPoolSize 10, minimumIdle 10, maxLifetime 1800000, connectionTimeout 30000).
4. Hikari 동작(HikariCP 5.1, Spring Boot 3.4.5, commerce 와 banking 같은 판): 연결은 max-lifetime(최대 2.5% 앞당김)에 은퇴하고 채우려는 새 연결 시도의 실패는 debug 로만 남는다.
   풀이 0 이 되면 요청은 connection-timeout 뒤 `HikariPool-1 - Connection is not available, request timed out after …ms (total=0, active=0, idle=0, …)` 를 받고, 이 예외는 마지막 연결 실패를 다음 예외로 달아
   Hibernate SqlExceptionHelper 가 ERROR 로 남긴다. 같은 경로가 banking F35-R 실행(2026-10-09)에서 'ORA-28000: The account is locked' 본문으로 119 에 실제 남았다(account 443건, transfer 342건, ledger 15건).
5. 프로브: `commerce/k8s/21-product-service.yaml:79-97` 등 여덟 매니페스트 모두 startup, readiness, liveness 가 `/actuator/health`(DB 확인 포함), timeoutSeconds 3, readiness 10초 x 3, liveness 15초 x 5.
   풀이 비면 readiness 30초 안에 빠지고 liveness 75초 뒤 재시작, 새 컨테이너는 `spring.sql.init.mode: always`(user `:27`, product `:20` 등)의 기동 초기화에서 거절되어 'Application run failed' 로 죽는다(F65-P 설계에서 확인한 user 기동 실패 꼴) → CrashLoopBackOff.
6. 게이트웨이: `commerce/api-gateway/src/main/java/com/commerce/gateway/service/AuthGuard.java:31-61` 쓰기마다 user 의 verify-token(F65-P: user 가 비면 쓰기 500, 로그인 프록시 502).
   `ProxyService.java:90-105` product 라우트 실패는 WARN 'Downstream unavailable for GET /api/products …' 와 502.
7. 실행기: `scripts/scenarios/profiles/db_account_executor.py` `POSTGRES_EXPIRY_REMOTE`(이번에 더한 PostgreSQL 비밀번호 기한 모드). 러너 컨테이너에서 kubectl exec 로 파드 local 소켓 psql(trust, pg_stat_statements 기록 끔).
   preflight: 기한 없음, 로그인 가능, 첫 네트워크 규칙 scram-sha-256, 파드 안 네트워크 로그인 성공. run: `ALTER ROLE "commerce" VALID UNTIL '<지금 UTC>'` 뒤 기한 지남과 네트워크 로그인 거절 확인.
   cleanup: `VALID UNTIL 'infinity'` 뒤 네트워크 로그인 성공 확인. 109 에서 preflight 실행(읽기와 로그인 한 번) 통과, 로컬 postgres:16-alpine 에 가짜 kubectl 로 preflight → run → cleanup → recovery 전 과정 통과.

## 5. 원인 규정 (원칙 5, 6)

- `root_cause.target_id`: `commerce-postgres:commerce`(콜론 뒤는 역할 이름). 결함을 가진 곳은 기한이 지난 자격 증명이고, 고쳐야(갱신해야) 재발이 막힌다.
- `trigger_target_id`: 비움. 계기(기한 도달)가 결함과 같은 곳이다. 서비스의 로그인은 정당한 요청이다(원칙 6 의 "A 가 정당했고 B 가 잘못" 중 B = 자격 증명).
- `scoring.partial`: `commerce-postgres`(인스턴스만 지목, 덜 정밀), `commerce-user`, `commerce-product`, `commerce-order`, `commerce-payment`, `commerce-cart`(증상 서비스), `commerce-gateway`(피해 전달).
- 원본 층위: "서비스가 쓰던 자격 증명이 만료됐다"까지. 경보가 왜 안 울렸는지, 기한 정책이 무엇이었는지는 요구하지 않는다(원칙 5).
- 관제 데이터로 낼 수 있는가: 거절 문구가 역할 이름을 말하고 여덟 서비스가 같은 문구를 내며, 롤아웃이 없고 DB 는 살아 있다. 합리적인 관제 AI 는 "commerce DB 계정 인증 실패"에 닿는다.
  기한이 지났다는 사실 자체(서버 로그의 'expired password')는 수집되지 않으므로, 계정 잠금, 비밀번호 불일치, 기한 경과를 가르라고 요구하지 않는다. 정답은 계정(자격 증명)까지다.

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 | 이 시나리오에서 |
|---|---|---|
| 감지 | 겉 증상 | 약 10분부터 user, cart, pricing, shipping 의 ERROR 급증과 readiness, liveness 실패, BackOff, 게이트웨이 쓰기 500·로그인 502. 약 30분부터 product, order, payment, inventory 도 같은 꼴, 상품 조회 502. commerce 전체가 멈춰 인시던트가 생기지 않을 수 없는 크기 |
| 피해 판정 | 러너 | 기준선(loadgen-commerce) 조회 단계 비정상 비율 ≥ 0.5(평시 0 근처). 쓰기 비율은 부하 쪽 로그인 불능으로 꺼지므로 판정에 쓰지 않고 회복에만 쓴다 |
| RCA | 녹화 데이터 | §7 의 로그 문구, 풀 지표, DPM 세션, KCM 이벤트 |

## 7. 관측 근거 표 (119 실조회, 2026-10-12 09:40~10:10 UTC)

표본 데이터(트레이스 10%, APM 골든 시그널)로 증명하지 않는다. 아래는 모두 전수 로그, OTel 런타임 지표(10초 내보내기, 표본 아님), DPM, KCM 이다.

| 증거 | 119 표와 칸 | 조회 | 평시 결과 | 장애 때 기대 |
|---|---|---|---|---|
| 인증 거절 문구 | CH `lucida.lucida_logs_local.body`, `service_name` | `SELECT count() FROM lucida.lucida_logs_local WHERE timestamp > now() - INTERVAL 30 DAY AND body ILIKE '%password authentication failed%'` | 0(보존 시작 2026-10-05 이후 모든 서비스) | 여덟 서비스에서 Hikari 시간 초과에 이어진 예외와 'Application run failed' 스택으로 다수 |
| 빈 풀 시간 초과 | 같은 표 | `… service_name LIKE 'commerce-%' AND body ILIKE '%Connection is not available%total=0,%'` (8일) | 0. 앞선 실행의 풀 고갈은 모두 total=10/20, active=10/20(user 2026-10-11 10:06~10:11, cart 20:23~20:32) | total=0, active=0, idle=0 |
| 서비스별 풀 크기 | VM `db.client.connections.usage{service_name=~"commerce-.*"}` (state idle/used 합) | `min_over_time((sum by (service_name) (db.client.connections.usage{service_name=~"commerce-.*"}))[7d:5m])` | 7일 최저 cart 4, inventory 10, order 10, payment 10, pricing 5, product 10, shipping 3, user 3(지금 값과 같음) | 10분 풀 넷이 10분 안에, 30분 풀 넷이 30분 안에 0 |
| DB 세션 | VM `dpm.postgresql.session.current_session`, `idle_session` | `quantile_over_time(0.5, …[8d])`, `min_over_time(…[8d])` | current 중앙 62, 1백분위 62, 최저 57. idle 중앙 61, 1백분위 59, 최저 45 | 계단식으로 0 근처(lucida_mon 등 몇 개만) |
| 파드 상태 | CH `lucida.kcm_events_local.reason`, `body`, `object_name` | `… namespace='rca-testbed-commerce' AND (body ILIKE 'Readiness probe failed%' OR body ILIKE 'Liveness probe failed%' OR reason='BackOff')` | 수집됨(이벤트 표 시작 2026-08-13, commerce Unhealthy 3,010건은 모두 앞선 시나리오 실행 창, BackOff 는 2026-08-25 postgres 가 마지막) | 여덟 파드의 readiness, liveness 실패와 BackOff, 두 물결 |
| 게이트웨이 | CH `lucida_logs_local`, `service_name='commerce-gateway'` | `… body ILIKE 'Downstream unavailable for%'` (8일) | 'GET /api/products' 5,593건 등 모두 앞선 실행 창(마지막 2026-10-06 11:29, 로그인은 2026-10-11 10:11) | 'POST /api/users/login', 'GET /api/products', 'GET /api/categories' 다수 |
| 계기(기한 도달) | 없음 | pg_roles, PostgreSQL 서버 로그는 수집되지 않음. DPM Top SQL 은 실행기 세션을 기록하지 않음(pg_stat_statements.track=none) | — | 첫 흔적은 풀 지표와 DPM 세션의 감소, 이어서 거절 문구(원칙 4: 계기의 흔적은 "새 로그인이 같은 문구로 거절되기 시작"이다. 인공 지연이 아니라 실제 인증 거절) |

원칙 4 보충: 원본에서도 계기는 사람의 조작이 아니라 시간의 경과였고 흔적은 실패 자체였다. 이 재구성도 계기 흔적은 거절 문구, 풀 감소, DPM 세션 감소다.
실행기의 ALTER ROLE 은 기한 도달을 재현하는 수단이라 데이터에 남지 않게 한다(원칙 8).

## 8. 감별

- must_support: 정답지 `must_support` 6항(거절 문구와 그 경로, 풀 지표 두 물결, DPM 세션 감소, KCM 두 물결, 조회와 쓰기 실패, 요청량 불변).
- must_rule_out: DB 다운(F25-H, 파드 Ready·DPM 계속·연결 거부 없음), 잠금이나 느린 질의로 꽉 찬 풀(F66-R, F67-H, F33-P, total=0 과 원인 예외), user 배포의 역할 이름 오설정(F65-P, 롤아웃 없음·지금 역할·여덟 서비스),
  user readiness 오설정(F16-H), 네트워크나 노드 장애(DB 가 거절을 답함, 같은 노드 게이트웨이 정상), 부하 증가(동반 부하 없음).
- contrast_with: F35-R, F65-P, F25-H, F16-H(정답지 문장).

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

- 강도라 할 값이 없다: 기한은 지났거나 안 지났거나다. 피해 크기는 요청량이 아니라 풀 연결의 수명으로 정해진다. 평가 모드 `approved-fixed-f71-r` 하나.
- 시간 계산: 만료 시각에 있던 연결은 모두 그 전에 만들어졌으므로 user, cart, pricing, shipping 은 10분, product, order, payment, inventory 는 30분 안에 마지막 연결이 은퇴한다.
  그 뒤 readiness 3회(30초)로 빠진다. 성공 판정 `min_hold 31m` 뒤 3틱(45초)이라 약 31.75분에 난다. 그때 쓰기와 로그인은 약 20분, 상품 조회는 1분 넘게 실패 중이다.
  `timeout 37m`, `max_injection_duration 40m`(기존 40m 시나리오 3개와 같은 상한).
- 부하: 동반 부하 없음. 기준선 loadgen-commerce 는 KST 하루 주기 2~10 요청/초이고 그중 브라우징 65%, 검색 15% 는 로그인이 필요 없어, 가장 한가한 시간(2 요청/초)에도 30초 창에 조회 약 48건이 흐른다.
  서킷브레이커는 피해를 숨기지 않는다(게이트웨이 product 서킷 폴백도 502). 풀이 비기 전의 부분 실패(남은 연결이 바빠 대기 초과)는 판정에 쓰지 않는다.
- 배제: 진입점 429, commerce PostgreSQL 파드 NotReady. 중단: 진입점 연결 불가(게이트웨이는 DB 를 안 써 살아 있어야 한다).
- 회복: cleanup 뒤 재시작 반복 중인 파드는 CrashLoopBackOff 대기(최대 5분)와 기동, startupProbe(30회 x 5초 안)를 지나 붙는다. 약 7.5분이라 recovery 15m.
- 러너 조회 주의: 러너의 commerce DB 조회(preflight 업무 성공 확인)는 commerce 로 비밀번호 로그인을 하므로 고장 구간 관측에는 넣지 않았다(preflight 는 주입 전이라 정상).

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(GitHub 2025-03, 공식 월간 보고 링크)와 요소별 대응표, 기전이 원본과 같다(자격 증명 만료 → 인증 실패 → 기능 중단) (원칙 1, §2)
- [x] 분류 장부 갱신(§4-1 행, §6 기록), 묶음 O 3/106, 정답 위치 DB 계정 4/106, 결제 경로 13/106, 서비스 commerce 36·은행 35·음식배달 35, 고른 이유(§3). 어느 축도 20% 아래 (원칙 2)
- [x] 근본 원인 위치: commerce/k8s/01-secrets.yaml:9-10, 여덟 매니페스트 DB_USER, 풀 설정 줄, 109 pg_roles·pg_hba 조회 (G1, §4)
- [x] 근본 원인 흔적: CH lucida_logs_local.body 의 거절 문구(평시 0), VM db.client.connections.usage, DPM session 지표 (원칙 3, §7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: 전수 로그, OTel 런타임 지표, DPM, KCM (원칙 3)
- [x] 계기 흔적: 새 로그인의 같은 거절 문구, 풀과 세션 감소. 인공 지연 없음 (원칙 4)
- [x] 정답이 관제 데이터로 낼 수 있는 결론(공통 역할의 인증 거절)이다. 코드 설계 결함 추론 불필요 (원칙 5)
- [x] 정답지 세 칸이 원칙 6대로(§5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다 (원칙 7, §6)
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: 실행기 스크립트와 인자에 id 없음(테스트로 고정), psql 앱 이름 기본값, pg_stat_statements 기록 끔 (원칙 8)
- [x] 부하와 서킷브레이커를 고려한 피해 계산(§9) (원칙 9)

## 11. 후보 목록 (반복 67, 2026-10-12)

숫자(원칙 2): 세 서비스 동률 35, 0 인 묶음 N, 적은 묶음 M 1, H·I·K·O·Q 2, F 3. 0 인 부품 Kafka(세 도메인), nginx(commerce, banking), 정답 위치 0: 메시지 브로커, DNS.

| 순 | 후보(실제 기전 × 부품) | 원본 | 결과 |
|---|---|---|---|
| 1 | commerce PostgreSQL 앱 역할 비밀번호 기한 경과(O, DB 계정) | GitHub 2025-03 공식 | **채택(F71-R)**. 반복 57 에서 commerce 판은 균형 규칙으로만 막혔고 2026-10-12 규칙 변경으로 풀림 |
| 2 | banking Oracle TRANSFERS 의 idx_transfers_from 삭제 마이그레이션(F, DB 테이블(이체)) | Chargebee 2018-03-02, Buildkite 2025-11-10 공식 | 버림(원칙 9): 거래 내역 조회가 약 1/s 이고 건수 질의 byb2y2hv7p4ag 는 이미 평균 318ms, 62,038 buffer gets(전수에 가까움), 인덱스를 타는 것은 목록 질의(3.3ms)뿐이라 지워도 Oracle 추가 부하가 약 0.3코어로 피해가 정해지지 않음 |
| 3 | food Kafka 브로커: 요청마다 새 프로듀서를 만드는 릴리스(B, 메시지 브로커) | PagerDuty 2025-08-28 공식 | 버림(원칙 7, 9): rejected 188, 312 와 같은 원본·주입. 발행이 outbox 비동기라 사용자 경로 증상 없음, 180rps 상한에서 PID 상태로 힙 1G 를 채우는 데 수 시간 |
| 4 | commerce cart Redis 캐시 전체 비우기 몰림(캐시) | Shepherd 2026-02-26 상태 페이지 | 버림(원칙 9): carts 20행, cart 로그 시간당 27건이라 DB 몰림이 피해가 안 됨(반복 46 의 균형 사유는 풀렸지만 강도 벽이 남음) |
| 5 | commerce Service 셀렉터에 라벨 추가(게이트웨이와 로드밸런서 설정) | Grafana Labs 2022 공식 블로그 | 버림(원칙 1, 9): 원본 기전은 롤아웃 중 일부 파드로 트래픽이 몰려 OOM 인데 모든 Deployment 가 replicas 1 이라 몰림이 없고, 엔드포인트 0 으로 바꾸면 기전이 달라짐 |
| 6 | 같은 꼴, 라벨 누락으로 엔드포인트 미등록(DNS·서비스 디스커버리) | Upbound 상태 페이지 | 버림(원칙 1): 원본이 계기(무엇이 라벨을 빠뜨렸나)를 밝히지 않음 |
| 7 | commerce 게이트웨이 파드 DNS 장애(M, DNS) | Square 2023-09-07 공식 | 버림(원칙 1, 3): 원본은 호스트 방화벽 변경과 DNS 서버 업그레이드의 복합으로 클러스터 DNS 서버가 무너진 것인데 kube-system 은 미수집, 파드 단위로 옮기면 F37-R 꼴(rejected 108) |
| 8 | DB 연결 시간 제한 축소 배포 + 재시도 증폭(N, 재시도) | PostHog 2025-09-29 공식 | 버림(원칙 1): DB 포화, 시간 제한 축소, 백오프 없는 재시도, 헬스체크 오설정 네 요인의 복합 |
| 9 | tb-w1 시계 점프로 토큰 만료 판정 어긋남(시계) | Cloudflare 2017-01-01 공식 | 버림(원칙 3, 8): 노드 시계가 뛰면 그 노드 모든 관측 데이터의 시각이 함께 어긋나 녹화 자체가 깨지고 chrony 가 곧 되돌림 |
