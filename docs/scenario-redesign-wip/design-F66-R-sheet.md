---
title: F66-R 설계 시트 (commerce 회원 표 열 추가 마이그레이션이 느린 내보내기 질의 뒤에서 잠금을 기다려 회원 표 질의가 모두 줄을 섬)
status: Draft
owner: project
last_reviewed: 2026-10-11
tags:
  - scenario
  - design
  - database
summary: 운영자가 commerce PostgreSQL user_schema.users 에 기본값 없는 null 허용 열 하나를 더하는 마이그레이션(카탈로그만 바꾸는 안전한 변경)을 돌렸는데, 몇 초 앞서 같은 표를 읽는 느린 내보내기 질의(회원과 로그인 토큰을 이은 COPY, 약 7분 활성)가 ACCESS SHARE 를 쥐고 있어 마이그레이션이 ACCESS EXCLUSIVE 를 기다리고, 그 뒤의 회원 표 질의가 단순 읽기까지 모두 줄을 서는 시나리오. 게이트웨이가 쓰기마다 부르는 토큰 확인이 회원 표를 읽어 user-service 풀이 차고 인가된 commerce 쓰기가 약 7분 동안 500 이 되며, 내보내기가 끝나면 스스로 풀린다. 원본은 Honeycomb 2024-08-06(teams 표 INSTANT 열 추가가 느린 SELECT 뒤 메타데이터 잠금 대기로 인증 포함 그 표 질의를 막아 API 요청 75% 거절), PostgreSQL 기전 보조는 GoCardless 의 잠금 대기열 사후 설명.
---

# F66-R 설계 시트

## 1. 요약

운영자가 마케팅 수신 동의 기능을 준비하며 commerce PostgreSQL 의 회원 표 `user_schema.users` 에 열 하나를 더하는 마이그레이션 `ALTER TABLE user_schema.users ADD COLUMN marketing_opt_in boolean` 을 돌린다. 기본값도 NOT NULL 도 없는 열 추가는 표를 다시 쓰지 않고 카탈로그만 바꾸는 "안전한" 변경이다. 그래도 그 순간에는 표의 ACCESS EXCLUSIVE 잠금이 필요하다.

몇 초 앞서 내보내기 작업이 같은 표를 읽는 느린 질의를 시작했다. 회원과 로그인 토큰을 이어 내보내는 `COPY (SELECT u.id, u.email, u.name, t.created_at, t.expires_at FROM user_schema.users u JOIN user_schema.auth_tokens t ON t.user_id = u.id) TO STDOUT` 가 약 229만 행을 받는 쪽 속도에 맞춰 흘려 약 7분 동안 활성이고, 그동안 users 의 ACCESS SHARE 잠금을 쥔다. 마이그레이션은 잠금을 얻지 못하고 기다린다. 기다리는 ACCESS EXCLUSIVE 요청은 모든 잠금과 충돌하므로, 그 뒤에 오는 users 질의는 id 로 회원 하나를 읽는 단순 읽기까지 모두 마이그레이션 뒤에 줄을 선다. 역할과 DB 에 lock_timeout, statement_timeout 이 없어(0) 아무도 포기하지 않는다.

게이트웨이는 쓰기(장바구니 변경, checkout)마다 user-service `verify-token` 으로 토큰을 확인하고, user 는 auth_tokens 에서 토큰을 찾은 다음 users 에서 회원을 읽는다. 로그인도 users 를 읽는다. 이 읽기가 끝나지 않아 user 의 Hikari 풀(10)이 몇 초 만에 차고, 뒤의 확인은 연결 대기 3초 끝에 500 이다. 게이트웨이는 확인을 같은 클래스 안에서 직접 불러 재시도, 서킷, 401 fallback 이 적용되지 않으므로 인가된 쓰기가 모두 500 이다. user 의 health 에 DB 확인이 있어 readiness 가 빠지고 liveness 가 재시작하며, 다시 뜬 파드는 기동 때 회원 시드 INSERT 도 같은 줄에 서서 기동 프로브에 다시 시작된다. 상품 둘러보기와 다른 표는 평시대로다. 내보내기가 끝나면 마이그레이션이 잠금을 얻어 열을 즉시 더하고 줄이 한꺼번에 풀린다.

비유: 회원 명부(users) 앞에 "명부 양식에 칸 하나 추가, 잠깐이면 됨" 이라는 공사 팻말(마이그레이션)이 섰다. 공사는 명부를 잠깐 혼자 써야 하는데, 마침 누가 명부를 통째로 베끼는 중(내보내기)이라 공사가 기다린다. 공사가 줄 맨 앞에 서 있으니, 손님 명찰을 확인하러 온 직원들(토큰 확인)도 그 뒤에 줄을 선다. 명부 자체도 공사 내용도 멀쩡한데 문 앞이 7분 동안 막힌다. 고칠 곳은 명부 앞 줄(그 표의 잠금 대기열)이고, 계기는 베끼는 중에 팻말을 세운 마이그레이션이다.

## 2. 원본 사례

- **Honeycomb, 2024-08-06** (공식 상태 페이지 사고 "UI and API unavailable" 과 사후 보고, 2024-08-07 게시): https://status.honeycomb.io/incidents/ycgvhvs201q1
  - 12:59:39~13:20:05 PDT. API 가 요청의 75% 를 거절(대부분 /1/auth 인증 끝점), UI 는 적어도 19분 쓸 수 없었고 트리거 평가가 멈춤.
  - "The schema migration was technically safe—a column addition to the `teams` table using an `INSTANT` algorithm". 마이그레이션 몇 초 앞서 같은 표를 읽는 질의가 시작됐고 "this specific slow `SELECT` query run could easily take more than 5 minutes to complete for some customer organizations". 마이그레이션이 메타데이터 잠금을 얻으려다 기다리는 동안("the `ALTER` statement itself waits for already running queries and transactions using this table to terminate") 그 요청이 인증을 포함한 그 표의 다른 질의를 막음.
  - 탐지: 13:00 경 저하를 알아채고 13:04 경보. 장애가 배포 직후라 진단이 어려웠고 DB 성능과 스키마 마이그레이션의 상관을 좁히는 데 몇 분. 후속: 느린 질의 점검, 클라이언트 시간 제한과 맞는 DB 쪽 시간 제한.
- **GoCardless** (공식 엔지니어링 블로그 "Zero-downtime Postgres migrations - the hard parts"): https://gocardless.com/blog/zero-downtime-postgres-migrations-the-hard-parts/ — 같은 기전의 PostgreSQL 설명(얻지 못한 잠금은 대기열에 들어가고 그와 충돌하는 뒤 잠금이 모두 뒤에 줄을 섬, AccessExclusive 는 모든 잠금과 충돌, 느린 SELECT 가 AccessShare 를 쥔 동안 ALTER 가 기다리고 뒤의 SELECT 가 멈춤)과 그 기전으로 약 15초 API 가 멈춘 사고. 재구성 엔진(PostgreSQL)에서 고리가 원본(MySQL 메타데이터 잠금)과 같음을 보이는 보조 원본이다.
- 둘 다 자료 문서 M21 에 이번에 추가했다(출처가 말한 사실만).

### 요소별 대응표

| 요소 | 원본 사례 (Honeycomb 2024-08-06) | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | teams 표에 열을 더하는 스키마 마이그레이션(INSTANT, 기술적으로 안전) | users 표에 기본값 없는 null 허용 열을 더하는 마이그레이션(카탈로그만, 표를 다시 쓰지 않음) |
| 원인이 된 결함 | 몇 초 앞서 시작된 같은 표의 느린 SELECT(5분 넘게 걸릴 수 있음)가 끝나기를 마이그레이션이 기다리며 잠금 요청을 대기열에 세움. DB 쪽 시간 제한 없음 | 몇 초 앞서 시작된 같은 표의 느린 내보내기 질의(COPY, 약 7분 활성)가 ACCESS SHARE 를 쥐고, 마이그레이션의 ACCESS EXCLUSIVE 요청이 대기열에 섬. lock_timeout, statement_timeout 0 |
| 전파 경로 | 기다리는 잠금 요청 뒤로 그 표의 다른 질의(인증 포함)가 막힘 | 기다리는 ACCESS EXCLUSIVE 뒤로 users 질의(토큰 확인의 회원 읽기, 로그인, 기동 시드)가 막힘 → user 풀 고갈 → 게이트웨이 쓰기 500 |
| 사용자 증상 | API 요청 75% 거절(대부분 /1/auth), UI 사용 불가 | 인가된 commerce 쓰기(장바구니 변경, checkout), 로그인, 회원가입 500. 상품 둘러보기는 정상 |
| 원본의 탐지 경로 | 저하 인지와 경보, DB 성능과 스키마 마이그레이션의 상관 | lucida-next 의 게이트웨이 오류율, commerce-user 오류 로그, DPM 세션 잠금 대기 사슬, KCM testbed-user Unhealthy |
| 완화와 복구 | UI, API 회복(느린 질의가 끝나 마이그레이션이 지나감). 후속으로 느린 질의 점검, DB 쪽 시간 제한 | 내보내기가 끝나 마이그레이션이 지나가며 스스로 회복. cleanup 이 더한 열을 지움 |

기전 일치: 원인("그 자체로는 안전한 열 추가가 같은 표의 느린 읽기 뒤에서 잠금을 기다리며 대기열 맨 앞에 섬")에서 증상("그 표를 읽는 인증 경로가 막혀 API 가 요청을 거절")까지 고리가 같다.

바꾼 것(기전 고리 밖):
- 엔진: MySQL 메타데이터 잠금 → PostgreSQL 표 잠금(ACCESS SHARE / ACCESS EXCLUSIVE). 두 엔진 모두 "기다리는 배타 요청이 뒤의 공유 요청을 막는" 대기열 규칙이 같고(GoCardless 가 PostgreSQL 판을 설명), 열 추가가 표를 다시 쓰지 않는 점(INSTANT ↔ 기본값 없는 null 허용 열)도 같다.
- 느린 읽기의 정체: 원본은 일부 고객 조직에서 오래 걸리는 앱의 SELECT. 재구성은 내보내기 작업의 COPY(SELECT ...) 로, 받는 쪽이 일정 속도로 받아 약 7분 활성이다. 서버 쪽에서는 둘 다 같은 표의 공유 잠금을 쥔 채 오래 도는 읽기 문장이다. DB CPU 를 다 쓰는 무거운 질의로 재구성하지 않은 것은 commerce PostgreSQL(0.5 CPU)을 포화시켜 F20-R, F33-P 꼴의 다른 신호가 섞이기 때문이다.
- 막히는 인증 경로: 원본은 teams 표를 읽는 /1/auth. 재구성은 게이트웨이의 쓰기 인가가 부르는 user verify-token(회원 읽기)과 로그인. 읽기(GET)는 인가를 거치지 않아 정상이다.
- 길이: 원본 약 20분, 재구성 약 7분(대기 세션이 연결 한도를 넘지 않게, §9).

## 3. 숫자 근거 (2026-10-11, `scenario-stats.py`, 정식 45 + 후보 40 = 85, 이 후보 전)

- 묶음(정식 + 후보): J 14(16.5%), G 10, P 10(각 11.8%), D 8(9.4%), A 7, B 7, L 7, C 6, E 4, F 3, H 2, K 2, Q 2, I 1, M 1, O 1, N 0. 금지(20%, 17 이상) 없음. 이 후보 D 8→9(86 중 10.5%).
- 정답 위치: 주문 서비스 8(9.4%)이 가장 크고, **DB 테이블(사용자) 0 → 1**(1.2%). 결제 경로 합계 12(14.1% → 86 중 14.0%, 이 후보와 무관).
- 서비스: 쇼핑몰 29, 은행 29, 음식배달 27. 쇼핑몰 29 → 30. 쇼핑몰은 은행과 최다 동률이라 원칙 2 완화 규칙(최다 서비스는 정답 위치가 0 인 부품만)을 따른다. 회원 표는 0 인 정답 위치다.
- 부품 지도: commerce postgres 8(표마다 정답 위치가 갈림: 재고 3, 결제 2, 인증 토큰 1, 상품 1, 재고 이동 이력 1, 사용자 0). 0 인 commerce 부품은 kafka, notification, nginx 인데 막힌 목록의 벽(비동기 소비자라 사용자 증상 없음, nginx OTel 없음)에 걸린다.
- 왜 이 후보인가: 가장 적은 서비스(음식배달 27)에서 먼저 찾았지만 이번 반복의 음식배달 후보가 원칙 9, 1 로 막혔고(아래 2, 3번), 다음으로 쇼핑몰, 은행 동률 중 정답 위치가 0 인 부품을 정답으로 하는 후보가 이것이다. 기전(이미 안전한 DDL 이 긴 읽기 뒤에서 잠금 대기열을 만듦)이 테스트베드에 처음이고 원본이 공식 사후 보고다.
- 겉 증상(인가된 commerce 쓰기 500, user 풀 고갈과 재시작)은 F33-P 와 같다. 2026-10-09 사용자 정정대로 증상 겹침은 버릴 사유가 아니라 H 관계로 판별력을 더한다: F33-P 는 DB 가 포화되고 Top SQL 호출당 블록이 뛰며, F66-R 은 DB 가 한가하고 세션이 잠금을 기다린다.

### 같은 꼴로 막힌 후보와의 관계

`$OPS/rejected-candidates.md` 62행(2026-10-09, commerce cart_schema 판 잠금 대기열, GoCardless/Honeycomb)은 "원칙 2(묶음 D 14%, DB 계열 정답 위치 합계, 장부 권고 'DB 밖 부품 먼저'), 카탈로그 §1(F06-H 와 같은 commerce 표 단위 잠금 꼴)" 로 버렸다. 지금은 다르다:
- 원칙 2: D 는 이제 8/85(9.4%) → 9/86(10.5%)이고, 2026-10-09 사용자 정정이 "정답이 데이터베이스다", "피하는 게 좋다 같은 권고" 를 버릴 사유에서 뺐다. 정답 위치(DB 테이블(사용자))는 0 이다.
- 카탈로그 §1: 같은 주입이 아니다. F06-H 는 db.lock 이 payments 를 EXCLUSIVE 로 잡고 노는 세션 하나(쓰기만 막고 읽기와 health 는 통과)이고, F66-R 은 db.ddl 의 새 모드로 오래 도는 읽기 하나와 그 뒤에서 기다리는 DDL 하나가 만드는 대기열(읽기까지 막음)이다. 표, 서비스, 피해 경로(쓰기 인가 대 결제 쓰기)도 다르다. 관제 데이터에서도 사슬 모양(한 단 대 두 단)과 막힌 문장(쓰기 대 읽기)이 다르다.
- 22행(food MySQL 판, Honeycomb)은 원칙 7, 컨트롤러 필수 중단 조건(food 입구 서비스가 그 표를 써서 입구가 끊김)으로 막혔다. commerce 입구는 게이트웨이이고 게이트웨이는 DB 를 쓰지 않아 users 줄에 걸리지 않는다.

### 후보 목록 (3단계, "실제 기전 × 부품", 숫자 순)

| # | 후보 | 층, 부품 | 원본 사례 | 결과 |
|---|---|---|---|---|
| 1 | commerce users 열 추가 마이그레이션이 느린 내보내기 질의 뒤에서 잠금을 기다려 회원 표 질의가 줄을 섬, 인가된 쓰기 500 | DB, commerce users(D, DB 테이블(사용자) 0) | Honeycomb 2024-08-06 공식, GoCardless 공식 | **채택** |
| 2 | food dispatch 릴리스의 새 갱신 작업이 요약 읽기 모델로 배차를 되써 eta_minutes 를 비움 → 만료 배치가 끝내지 못해 배달원 용량이 새고 주문 503 | 앱, food dispatch(H+J, 배달 서비스 5) | Cloudflare 2023-01-24 공식('last seen' 갱신이 숨겨진 칸을 빈 값으로 덮어 토큰 무효) | 버림: 원칙 9. 용량이 차는 시간은 (2000 - 현재 배정)/주문률인데 기준선 주문률이 하루 주기로 초당 0.13~1.15 라(배정 1,677, 10분 700건, 2026-10-11 읽기 전용 실측) 피크에는 약 4분, 새벽에는 약 4시간이다. 동반 주문 부하로 당기면 피크에는 주입 없이도 한도가 찬다. 러너에 시각 조건이 없다 |
| 3 | food dispatch 회수 작업(만료 배치)만 접근이 끊겨 배정이 새고 용량이 참 | 앱, food dispatch(B, 배달 서비스 5) | Travis CI 2015-08-04 공식(회전한 비밀번호가 정리 서비스 설정에 반영 안 돼 VM 이 새고 용량 소진), CircleCI 2021-05-21 공식(업그레이드 기본 시간 제한이 VM 정리 소비자를 끊어 할당량 소진) | 버림: 원칙 1. 회수 작업이 dispatch 안에서 요청 경로와 같은 계정, 같은 풀을 써 회수만 끊는 계기(정리 주체의 자격 증명, 기본 시간 제한)를 원본대로 만들 수 없다. 권한 회수 판은 공식 사례 없음(막힌 목록 75행) |
| 4 | food order → dispatch 서킷브레이커 느린 호출 임계 설정 배포 | 앱, food order(G, 주문 서비스 8) | Spreedly 2022-03-28 공식 | 버림: 지휘 세션 목록(F64-R 원본, food order→restaurant 판은 막힌 목록 21행과 같은 주입) |
| 5 | commerce 표 이전 뒤 통계(ANALYZE) 미이전으로 나쁜 실행 계획 | DB, commerce PostgreSQL 표(Q) | RevenueCat 2022-11-23 공식(주 원인) | 버림: 원칙 9. 사용자 경로 질의가 단일 등가 조건이라 통계 없이도 인덱스를 고르고, 자동 분석이 곧 통계를 되살린다 |
| 6 | food MySQL 앱 계정 자격 증명 회전이 다른 환경에 적용되고 옛 자격 증명 삭제 | DB 계정, food MySQL(O) | Cloudflare 2025-03-21 공식(R2 자격 증명이 운영 대신 기본 환경에 배포되고 옛 자격 증명 삭제) | 버림: 컨트롤러 필수 중단 조건. food 네 서비스가 한 계정을 쓰고 health 가 DB 를 봐 order, restaurant 입구가 끊긴다(막힌 목록 11행과 같은 벽) |
| 7 | food dispatch liveness 시간 제한 과소 설정 배포 | 인프라, 프로브(B, 배달 서비스) | Chef 2014-07-10 공식(아주 낮은 헬스체크 시간 제한) | 버림: 원칙 9(health 가 ms 라 부하에서도 시간 제한에 닿지 않음), k8s.probe 수단이 F05-H 꼴(막힌 목록 230행) |
| 8 | JVM 판 갱신의 컨테이너 인식으로 CPU 수 기준 풀이 줄어 처리량 저하 | 런타임, food JVM(E) | CircleCI 2019-03-26~04-10 공식 | 버림: 원칙 1. 원본이 그 변경을 주 원인이 아니라 기여 요인으로 적고, 테스트베드에 CPU 수로 크기를 정하는 풀이 없다 |
| 9 | 보안 정책 변경이 의도 밖 대상까지 막음(tb-w3 방화벽) | 인프라, 노드(K) | Salesforce 2023-09-20 공식 | 버림: 카탈로그 §1(host.firewall 수단과 노드가 F44-R 과 같음) |
| 10 | 배포 도구가 운영 대신 스테이징 빌드를 가리킴(food payment 이미지) | 인프라, 이미지(J) | Cloudflare 2023-10-30 공식 | 버림: F17-H(없는 이미지 롤아웃) 꼴이고 food 는 기본 롤링이라 옛 파드가 남아 피해 없음(원칙 9) |

DB 가 정답인 후보 3개(1, 5, 6), 부품 9종(commerce 회원 표, food dispatch, food order, commerce 표 통계, food MySQL 계정, 프로브, JVM, tb-w3 노드, food payment 이미지), 앱, DB, 인프라, 런타임 층.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 주입: `scripts/scenarios/profiles/db_ddl_executor.py` `PG_QUEUE_REMOTE` `run` — testbed-postgres-0 파드 안에서 백그라운드로 내보내기 `COPY (SELECT u.id, u.email, u.name, t.created_at, t.expires_at FROM user_schema.users u JOIN user_schema.auth_tokens t ON t.user_id = u.id) TO STDOUT` 를 N 행마다 1초 쉬는 읽기 쪽(awk)에 흘리고(N = auth_tokens 행 추정 / 420 + 1, 109 실측 행 추정 2,288,642 → N 5,450), 그 세션이 users 의 공유 잠금을 쥔 것을 본 뒤 `ALTER TABLE user_schema.users ADD COLUMN marketing_opt_in boolean` 을 백그라운드로 띄우고, 서버가 ALTER 를 내보내기 뒤에서 기다리는 것으로 보이면 돌아온다.
2. 회원 표: `commerce/user-service/src/main/resources/schema.sql:3-11`(users, 기본 키 id, email UNIQUE). 더하는 열은 엔티티(`User.java`)에 없어 앱 읽기, 쓰기에 영향이 없다.
3. 토큰 확인: `commerce/user-service/src/main/java/com/commerce/user/service/UserService.java:87-95` verifyToken — `authTokenRepository.findByToken`(auth_tokens) 다음 `userRepository.findById`(users). users 가 줄을 서면 이 읽기가 끝나지 않는다. 로그인 `:69-70`(findByEmail)도 같다.
4. user 풀: `commerce/user-service/src/main/resources/application.yml:11-17`(maximum-pool-size 10, connection-timeout 3000). 줄 선 읽기 10건이 연결을 쥐고, 뒤의 요청은 3초 뒤 'Connection is not available' 500.
5. 기동 시드: `application.yml:25-29`(spring.sql.init.mode always), `data.sql:2-23`(INSERT INTO user_schema.users ... ON CONFLICT (id) DO NOTHING). 재시작한 user 는 이 INSERT 가 같은 줄에 서서 뜨지 못한다.
6. 게이트웨이: `commerce/api-gateway/src/main/java/com/commerce/gateway/service/AuthGuard.java:31-63` verifyIfWrite 가 쓰기마다 `verifyToken`(44)을 같은 클래스 안에서 직접 부른다. 자기 호출이라 @Retry, @CircuitBreaker(user), 401 fallback 이 적용되지 않고, user 의 500 이나 연결 거부가 처리되지 않은 예외로 올라가 쓰기 500 이 된다(`GatewayProxyController.java:28-71` 의 쓰기 경로들, `api-gateway/src/main/resources/application.yml:18-21` user read-timeout 10s).
7. user 프로브: `commerce/k8s/25-user-service.yaml:84-103` startupProbe 5초 x 30, readinessProbe 10초 x 3(시간 제한 3초), livenessProbe 15초 x 5, 모두 `/actuator/health`(DB 확인 포함). 전략 maxSurge 0 은 롤아웃이 아니라 무관하다.
8. DB: `commerce/k8s/10-postgres.yaml` testbed-postgres(PostgreSQL 16.14 alpine, aarch64, tb-w1). 109 읽기 전용 실측(2026-10-11): max_connections 100, 열린 세션 62(앱 JDBC 57), lock_timeout 0, statement_timeout 0, 역할, DB 설정 없음, users 3,020행, auth_tokens 653MB.

### 로컬 실측 (2026-10-11, 원칙 9 근거)

origin/main(9390b61)의 commerce 를 풀어 user-service jar 를 빌드하고(`mvnw -o -pl user-service -am package`), PostgreSQL 16.14 컨테이너(같은 판, pg_stat_statements)에 붙여 띄웠다(Kafka 없음: outbox 릴레이 오류는 무관). verify-token 을 초당 5건 보냈다.

| 시험 | 결과 |
|---|---|
| 손으로 만든 대기열(REPEATABLE READ 읽기 트랜잭션 120초 + ALTER) | pg_stat_activity: ALTER 가 Lock/relation 으로 읽기 세션에 막히고, user 세션 10개가 Lock/relation 으로 ALTER 에 막힘. verify-token 15초 창마다 75건 중 75건 500(평균 3.01초), 처음 10건은 끝나지 않음(클라이언트 20초 시간 초과). user health DOWN. user 로그 'HikariPool-1 - Connection is not available, request timed out after 3000ms (total=10, active=10, idle=0, waiting=15)' 1,180줄. 읽기가 끝나자 ALTER 가 지나가고 바로 200 |
| user 프로세스를 죽였다 다시 띄움(대기열 유지) | 줄 선 백엔드 10개가 그대로 남음(대기 중인 백엔드는 클라이언트가 사라진 것을 모름). 다시 띄운 user 는 data.sql 의 회원 시드 INSERT 가 Lock/relation 으로 줄에 서서 health 응답 없음(유휴 연결 3개 + 줄 선 1개). 잠금이 풀린 뒤 3초 만에 'Started UserServiceApplication in 131.542 seconds' |
| 실행기 스크립트 그대로(가짜 kubectl 로 docker exec, auth_tokens 20만 행, export_seconds 60) | preflight 0, run 2.2초 만에 0(내보내기 Client/ClientWrite 활성, ALTER Lock/relation 이 내보내기에 막힘, user 세션이 ALTER 에 막힘). 내보내기 61초에 스스로 끝, ALTER 대기 58초 뒤 완료. verify-token 0~60초 500, 60초 창부터 200. cleanup 2.3초(열 지움, 파드 안 파일 삭제), recovery 0. drain 5초로 일찍 cleanup 해도(내보내기 끝냄, 마이그레이션 지나감 또는 취소) 열이 지워지고 recovery 0. 탐침 문장은 pg_stat_statements 에 남지 않고 내보내기와 ALTER 는 남음 |

운영과의 차이: 109 는 aarch64 alpine(busybox awk, 109 에서 `awk ... system()` 확인), user 는 쿠버네티스 프로브로 재시작된다(로컬은 손으로). 대기열은 잠금 규칙으로만 정해져 CPU 속도와 무관하다.

## 5. 원인 규정 (원칙 5, 6)

- 근본(`root_cause.target_id`): `commerce-postgres:user_schema.users` — 그 표에 생긴 잠금 대기열(이미 안전한 DDL 이 같은 표의 긴 읽기 뒤에서 ACCESS EXCLUSIVE 를 기다림). F06-H, F01-R, F33-P, F60-R 과 같은 '인스턴스:스키마.테이블' 꼴.
- 계기(`trigger_target_id`): 비움. 계기(그 표에 대한 마이그레이션)와 결함 위치가 같다. user-service, 게이트웨이, 부하는 평시 그대로 정당하다(원칙 6: 정당한 요청 A, 잘못된 곳 B → B).
- 부분 점수: `commerce-user`(증상이 드러나는 서비스), `commerce-postgres`(인스턴스 단위 답), `commerce-postgres:user_schema.auth_tokens`(내보내기가 함께 읽는 표, 토큰 확인 경로의 앞 표).
- 층위: 원본 사후 보고의 결론("안전한 열 추가가 같은 표의 느린 SELECT 뒤에서 잠금을 기다리며 인증을 포함한 그 표 질의를 막음")과 같다. 관제 데이터로 낼 수 있는 결론: "users 표에서 잠금 대기 사슬(user 세션 → 기다리는 세션 → 오래 도는 읽기)이 생겨 user 풀이 마르고 게이트웨이 쓰기가 실패했다". 마이그레이션이 무슨 열을 더했는지, 내보내기가 무엇이었는지는 요구하지 않는다(세션 스냅숏에 문장 원문이 없다, §7).

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 |
|---|---|
| 감지 | commerce-gateway 오류율(쓰기 500, 서버 스팬 ERROR), commerce-user 오류 로그 급증(평시 0줄), KCM testbed-user Unhealthy, Killing |
| 피해 판정(러너) | 상주 기준선(domain commerce) checkout 5xx 비율 ≥ 0.5 와 2xx 비율 < 0.3 이 3틱. 표본은 주입 전 VU 토큰 캐시가 있는 checkout 여정뿐이다(§9). 평시 기준선 쓰기 nonok 은 0.1 미만(F33-P, F16-H 복구 조건과 같은 관측) |
| RCA | DPM 세션 스냅숏의 잠금 대기 사슬과 relNames, DB CPU 와 Top SQL 평시, 롤아웃 없음, 내보내기 세션이 사라지는 분에 함께 회복 |

## 7. 관측 근거 표 (119 실조회, 2026-10-11 05:40~06:20 UTC)

표본 데이터(트레이스 10%, APM 스팬 지표)만으로 증명하지 않는다. 결정 증거는 DPM 세션 스냅숏(분마다), 전수 로그, KCM 이벤트다.

| 증거 | 119 표와 칸 | 조회 | 평시 결과 |
|---|---|---|---|
| 근본: users 잠금 대기 사슬 | CH `lucida.dpm_session_local`(engine, body 의 pid, user, program, ip, waitType, waitEvent, sessionType, blockingPid, relNames, queryTime) | 60일 `engine='postgresql' AND JSONExtractString(body,'relNames') LIKE '%users%'` 수, 30일 waitType/waitEvent 별 수, 7일 sessionType 별 수 | relNames 에 users 0건. 30일 Lock/relation 745(F06-H 등 실행 때, relNames inventory, payments 따위), Lock/tuple 2,626. 7일 active 10,057, blocking 300, blocked 16, idle in transaction 432. 스냅숏은 분마다 한 번(최근 20분 분당 1). 다른 실행의 blocked 행 예: `{"sessionType":"blocking","waitType":"Lock","waitEvent":"tuple","blockingPid":767946,"relNames":"inventory, inventory_pkey, ..."}` — 대기 행에 표 이름과 막는 pid 가 남는다는 확인. 장애 때 user 파드 IP 세션의 blocked + relNames users 와 그 사슬이 처음 나타나야 한다 |
| 계기: 마이그레이션과 긴 읽기 | 같은 표 | 같은 행의 blockingPid 를 따라감 | 사슬 둘째 단(ALTER, Lock/relation, relNames users)과 맨 앞(내보내기, Client/ClientWrite, queryTime 이 분마다 늘어 약 420초, relNames users, auth_tokens). 두 세션은 program 'PostgreSQL JDBC Driver', DB 파드 안 연결이다. 세션 행에는 문장 원문이 없고 sqlHash 는 Top SQL 과 다른 32비트 값이다. 원문은 끝난 문장만 pg_stat_statements 에 남고, DPM Top SQL 은 앞선 수집에서 본 문장의 증분만 내보내 한 번 도는 ALTER, COPY 는 닿지 않는다(F23-P 시트와 같은 확인) |
| 전파: user 풀 고갈 | CH `lucida.lucida_logs_local` | 8일 `service_name='commerce-user'` 수, `body LIKE '%verify-token%'` 수 | commerce-user 로그 0줄(평시 로그를 남기지 않음, OTEL_LOGS_EXPORTER otlp 는 켜져 있음: `25-user-service.yaml:80-81`), verify-token 이 든 로그 0줄. 장애 때 'HikariPool-1 - Connection is not available, request timed out after 3000ms (total=10, active=10, idle=0, ...)' 와 게이트웨이 'Servlet.service() ... threw exception' 이 처음 나타나야 한다(로컬 같은 jar 1,180줄) |
| 전파: 게이트웨이 쓰기 실패 | CH `lucida.lucida_logs_local` | 8일 `service_name='commerce-gateway'` 템플릿별 | 평시 0줄, 다른 실행(2026-10-06) 때 'Downstream unavailable for POST /api/orders/checkout ...' WARN 들뿐. verify-token 꼴 0 |
| 전파: user 재시작 | CH `lucida.kcm_events_local`(reason, object_name) | 30일 `object_name LIKE 'testbed-user%'` reason 별 | Unhealthy 14, ScalingReplicaSet 4, Killing 2 등, 마지막 2026-10-01(F16-H 실행 때). 장애 때 롤아웃 없이 Unhealthy, Killing 이 생겨야 한다 |
| 감별: DB 는 한가 | CH `lucida.dpm_topsql_local`(sql_hash, body 의 calls, sharedHit, sharedRead, avgExecTime) | 1일 토큰 조회 sql_hash 618727859340325129 와 회원 이메일 조회 8716608724904561152 | 토큰 조회 14,428호출, 호출당 블록 5, 평균 0.062ms. 회원 조회 277호출, 3블록, 0.061ms. 장애 때 호출당 블록은 그대로여야 한다(F33-P 와 가르는 근거) |
| 감별: 부하 | CH `lucida.otel_traces_local`(10% 표본, 보조) | 1시간 commerce-user SERVER 스팬 이름별 | verify-token 923, health 53, login 37(표본, 전수 약 10배 = 초당 약 2.6건의 토큰 확인). 동반 부하는 없다 |
| 참고: DB 설정 | 109 PostgreSQL 읽기 전용 조회 | `SHOW lock_timeout`, `statement_timeout`, `max_connections`, pg_stat_activity 수, pg_db_role_setting | 0, 0, 100, 62, 설정 없음(2026-10-11) |

## 8. 감별

- must_support: DPM 세션의 users 잠금 대기 사슬(user 세션 blocked → 같은 blockingPid 의 대기 세션 → 오래 도는 ClientWrite 활성 세션), commerce-user Hikari 'Connection is not available'(평시 0줄), 게이트웨이 verify-token Servlet 오류(평시 0), KCM testbed-user Unhealthy, Killing(롤아웃 없음), checkout nonok ≥ 0.5 와 2xx < 0.3, 상품 둘러보기 정상, PostgreSQL Ready 와 Top SQL 호출당 블록 평시, 내보내기 세션이 사라지는 분에 함께 회복.
- must_rule_out: 인덱스를 잃은 전수 스캔(F33-P), user 프로브 오설정(F16-H), 쓰기 잠금을 쥔 채 노는 트랜잭션(F06-H, F01-R), PostgreSQL 다운(F25-H), 게이트웨이 설정이나 부하.
- contrast_with: F33-P, F16-H, F06-H, F60-R, F01-R.
- 관계: F33-P 와 겉 증상(인가된 쓰기 500, user 풀 고갈과 재시작)이 같고 정답 표가 다르다(H). 판별력은 DPM 세션(실행 대 잠금 대기)과 Top SQL 호출당 블록(급증 대 평시)에 있다. F06-H 와는 '표 잠금' 꼴이 같고, 사슬 모양(한 단 대 두 단)과 막힌 문장(쓰기 대 단순 읽기)이 다르다. F60-R 과는 '마이그레이션이 표 잠금으로 앱을 막음' 꼴이 같고, DDL 이 오래 도는지(표 복사) 아니면 즉시 끝날 DDL 이 긴 읽기 때문에 기다리는지가 다르다.

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

- 강도는 하나로 고정(approved-fixed-f66-r): 내보내기 약 420초(export_seconds), cleanup 대기 상한 300초(drain_seconds), 시작 조건 열린 세션 70 이하(maximum_sessions). 대기열은 잠금 규칙으로 정해져 결정적이다: 내보내기가 도는 동안 users 질의는 하나도 끝나지 않는다.
- 부하: 동반 부하를 두지 않는다. 판정은 상주 기준선(loadgen-commerce, `commerce/loadgen/entrypoint.sh:11-17` 하루 주기 2~10rps, 사전 할당 VU 20, 최대 50)에서 한다.
  - 1차 평가 차단 지적(2026-10-11): 처음에는 F33-P 의 판정(동반 surge.js 의 checkout)을 그대로 옮겼다. 그러나 러너는 동반 부하를 띄운 직후 같은 루프에서 주입하고(rca-scenario-runner `production_runtime.py:817-853`), 동반 k6 는 막 떠 토큰 캐시가 비어 있다. surge 의 checkout 여정은 login 부터 하는데(`surge.js:65-82, 110-115`) 이 주입은 F33-P 와 달리 로그인(findByEmail)까지 users 줄에 세우므로 여정이 login 에서 끝나 checkout 을 보내지 않는다. 표본이 0 인 창은 필드가 빠져 unusable 이 되고(`loadgen_monitor.py`), 성공 streak 이 쌓이지 않아 timeout 으로 끝났을 것이다. 로컬 실측은 고정 토큰으로 verify-token 을 직접 불러 이 경로를 확인하지 못했다.
  - 기준선 checkout 여정(여정의 8%)은 `login(userId)` 로 시작하되 VU 마다 사용자 20명의 토큰을 24시간 캐시한다(`commerce/loadgen/script.js:93-113`). 캐시가 있으면 로그인 요청 없이 장바구니 비우기, 담기, checkout 을 보내고(`:194-230`), 셋 모두 게이트웨이 인가의 users 읽기에서 막혀 500 이 된다. 캐시가 없는 여정은 로그인이 3초 뒤 500 으로 끝나 checkout 을 보내지 않는다. 그래서 표본이 있는 창의 checkout 5xx 비율은 1 에 가깝고 2xx 는 0 에 가깝다. F16-H(정식)가 같은 기준선 캐시로 쓰기 실패율을 판정한다.
  - 표본 수: 기준선 k6 는 매시 정각 다시 시작해 캐시가 빈다. (VU, 사용자) 쌍 400 개가 시간당 checkout 576~2,880 번(2~10rps x 8%)으로 채워져, 정각 뒤 t 분의 캐시 비율은 대략 1 - e^(-checkout 수/400) 이다. 한가한 시간(2rps)에도 정각 뒤 30분이면 약 50% 라 30초 창마다 checkout 여정 약 4.8건 중 약 2.4건이 표본이고(표본 0 인 창 약 9%), 피크(10rps)에는 정각 뒤 10분이면 약 70% 로 창마다 약 17건이다. k6 는 낮은 동시성에서 VU 일부만 돌리는 경향이 있어 실제 캐시 비율은 이보다 높다.
  - 남는 위험: 주입이 정각 직후(캐시가 거의 빔)에 시작하거나 판정 전에 정각이 지나면 표본이 줄어 판정이 늦어지거나 timeout 으로 끝날 수 있다. F16-H 와 같은 성질이라 min_hold 를 150s 로 짧게 두어 정각 재시작 전에 판정을 끝내게 한다(F16-H 가 min_hold 8m → 150s 로 바꾼 이유와 같음). 러너에 시각 조건이 없어 이 위험은 운영 하네스의 재시도(attempts)로 흡수한다.
- 피해 계산: user 풀 10 이 기준선 토큰 확인(초당 약 2.6건, 트레이스 10% 표본에서 추정)과 로그인 앞에서 약 4초 만에 찬다. 그 뒤 확인은 3초 대기 끝에 500(로컬 15초 창마다 75건 중 75건), user 가 NotReady 이거나 재시작 중이면 게이트웨이 호출이 연결 거부로 곧바로 500. 게이트웨이에 fallback 이 없어 캐시된 토큰의 checkout 은 모두 5xx 다.
- 연결 한도 계산: 줄 선 백엔드는 user 파드가 죽어도 잠금이 풀릴 때까지 남는다(로컬 확인). 첫 파드의 10개, liveness 재시작(약 75초 + 기동) 뒤 다시 뜬 파드마다 시드 INSERT 1개(유휴 연결 3개는 파드가 죽으면 닫힘), 기동 프로브 실패(150초)와 백오프로 7분 동안 재시작 약 2~3번 → 약 13개. 평시 62 + 주입 세션 2 + 13 ≈ 77 < 100. 시작 조건 70 이하가 여유를 지킨다. 길이를 7분으로 둔 이유다(원본 20분).
- success: 기준선 checkout_5xx_rate ≥ 0.5 와 기준선 checkout 2xx 비율 < 0.3, 3틱(틱 15초, 관측 갱신 30초라 약 90초). min_hold 150s(위 정각 재시작 위험), settle 30s, timeout 7m(내보내기가 끝나는 시각).
- must_rule_out: 기준선 achieved_rps < 1(기준선 2~10rps), 기준선 상품 목록 실패율 ≥ 0.1(읽기는 인가와 users 를 거치지 않아 그대로여야 함, F16-H 와 같은 감별), commerce PostgreSQL 파드 NotReady(2틱).
- abort: 기준선 entry_status == 0(2틱, 기준선의 마지막 checkout 응답 상태라 창에 표본이 없어도 값이 남는다). commerce 입구는 게이트웨이(30080)이고 게이트웨이는 DB 를 쓰지 않으며 읽기는 인가를 거치지 않아 users 줄에 걸리지 않는다. 쓰기 스레드는 user 응답(3초) 또는 read-timeout(10초)까지만 묶이고 Tomcat 200 에 비해 적다(기준선 쓰기 초당 수 건 x 최대 10초).
- recovery: target_health 200, user Ready, PostgreSQL Ready, 기준선 쓰기 nonok < 0.1(2틱, 10분). 내보내기가 끝나면 줄이 한꺼번에 풀리고, 재시작 중이던 user 는 시드 INSERT 가 끝나 곧 뜬다(로컬 3초). CrashLoopBackOff 지연이 남을 수 있어 10분이다.
- cleanup: 내보내기가 스스로 끝나기를 최대 300초 기다리고(넘으면 pg_terminate_backend, 남은 마이그레이션은 pg_cancel_backend), 두 세션이 없어진 뒤 `SET lock_timeout = '5s'; ALTER TABLE user_schema.users DROP COLUMN IF EXISTS marketing_opt_in`(카탈로그만, 최대 5번), 파드 안 스크립트, 로그 파일 삭제. 행, 인덱스, 다른 표는 손대지 않는다. 내보내기 출력은 파드 안 awk 가 버린다(파일로 남기지 않음).
- run 시간: 탐침 약 8번 + kubectl exec 3번 + 확인 대기 각 1~2초, 로컬 2.2초. 러너 coordinator lease(30초) 안이다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Honeycomb 2024-08-06 공식 사후 보고, PostgreSQL 기전 GoCardless 공식 블로그)와 요소별 대응표, 기전 동일(§2)
- [x] 분류 장부 갱신(§4-1 행, §6 기록), 묶음 D(8→9, 86 중 10.5%), 정답 위치 DB 테이블(사용자)(0→1), 결제 경로 12(14.0%), 서비스 쇼핑몰 29→30(최다 동률이지만 정답 위치 0 부품), 고른 이유(§3). 어느 축도 20% 미만
- [x] 근본 원인 위치 `file:line`(user 서비스 질의, 풀, 시드, 게이트웨이 인가, 프로브)과 인프라 지점(testbed-postgres-0, 설정 실측)(§4)
- [x] 근본 원인 흔적 119 실조회: dpm_session_local 이 대기 행에 relNames 와 blockingPid 를 남김(다른 실행 행으로 확인), 60일 relNames users 0건(§7)
- [x] 핵심 증거가 표본에만 있지 않음: DPM 세션 스냅숏(분마다), 전수 로그, KCM 이벤트. 트레이스는 보조
- [x] 계기 흔적: 사슬 둘째 단의 대기 세션(마이그레이션)과 맨 앞의 오래 도는 읽기 세션(내보내기)이 세션 스냅숏에 남음. 인공 지연 없음(대기는 잠금 규칙, 내보내기의 속도는 받는 쪽이 정하고 pg_sleep 없음)
- [x] 정답이 관제 데이터로 낼 수 있는 결론(users 표의 잠금 대기 사슬이 user 풀을 마르게 함), 코드 설계 결함 추론 불필요. 게이트웨이 fallback 없음은 피해 설명(§5, user_impact)
- [x] 정답지 세 칸이 원칙 6 대로(근본 users 표, 계기 같음, 부분 점수 commerce-user, commerce-postgres, auth_tokens)
- [x] 감지, 피해 판정, RCA 증거 구분(§6)
- [x] 주입 도구가 시나리오 id 를 남기지 않음: 실행기 인자는 네임스페이스, 파드, 스키마, 표, 열, 숫자뿐이고, 두 세션은 드라이버 기본 이름 'PostgreSQL JDBC Driver', 파드 안 파일 이름은 /tmp/user-export.sh, /tmp/user-export.log, /tmp/schema-migration.log, 탐침은 pg_stat_statements 에 남지 않음(테스트 `test_postgres_column_migration_queues_behind_a_slow_export_and_the_column_is_dropped_after`)
- [x] 부하 상한과 서킷브레이커를 고려한 피해 계산(§9: user 풀 약 4초에 고갈, 게이트웨이 fallback 없음으로 캐시된 토큰의 쓰기 5xx, 판정 표본이 기준선 토큰 캐시에서 나오는 계산과 정각 재시작 위험, 연결 한도 여유 계산, 로컬 실측)
