---
title: F65-P 설계 시트 (commerce user-service 일상 롤아웃이 회전 전 DB 계정을 다시 넣어 교체된 파드가 PostgreSQL 인증에 실패함)
status: Draft
owner: project
last_reviewed: 2026-10-12
tags:
  - scenario
  - design
  - credential-rotation
  - config-drift
summary: commerce user-service 의 일상 롤아웃이 인프라 코드에 남아 있던 회전 전 DB 계정 이름(DB_USER=commerce_app)을 명시 값으로 다시 넣어 Secret 참조를 덮어쓰고, maxSurge 0 이라 하나뿐인 파드가 먼저 내려간 뒤 새 파드가 기동 중 'password authentication failed for user "commerce_app"' 으로 죽기를 되풀이해 user 가 비고, 게이트웨이의 토큰 확인이 user 에 닿지 못해 모든 쓰기(장바구니 담기, checkout)를 500, 로그인을 502 로 만드는 시나리오(E2B 2026-06-25 재구성, F65-R 과 같은 원본의 자격 증명 판)의 설계 근거.
---

# F65-P 설계 시트

## 1. 요약

commerce 의 아홉 서비스는 PostgreSQL 에 같은 앱 계정으로 붙는다. user-service Deployment(`testbed-user`)는 그 계정을 Secret `postgres-secret` 에서 읽는다: env `DB_USER` 는 `secretKeyRef POSTGRES_USER`, `DB_PASS` 는 `secretKeyRef POSTGRES_PASSWORD` 다.

예전에는 앱 계정이 `commerce_app` 이었고 배포 정의에 그 이름이 값으로 박혀 있었다. 운영에서 자격 증명을 회전하면서 계정을 지금의 것으로 바꾸고 배포를 Secret 참조로 고쳤는데, 인프라 코드의 user 배포 정의에는 그 변경이 들어가지 않았다(드리프트). 일상 인프라 갱신 하나가 그 낡은 정의를 다시 적용해 user 컨테이너 env 의 `DB_USER` 가 Secret 참조 대신 값 `commerce_app` 이 된다. 비밀번호는 Secret 의 현재 값 그대로다.

user Deployment 의 정본 전략은 maxSurge 0 / maxUnavailable 1 이다. 롤아웃은 하나뿐인 옛 파드를 먼저 내린다. 새 컨테이너는 Spring Boot 를 띄우다가 기동 때 스키마 초기화(`spring.sql.init.mode: always`)가 연결을 얻는 순간 PostgreSQL 이 `FATAL: password authentication failed for user "commerce_app"` 으로 거절해 'Application run failed' 로 죽는다(종료 코드 1). kubelet 이 다시 띄워도 같은 env 라 같은 곳에서 죽고 CrashLoopBackOff 가 된다. testbed-user 엔드포인트가 비어, 게이트웨이의 AuthGuard 가 쓰기 요청마다 부르는 토큰 확인(`/api/users/verify-token`)이 연결 거부로 실패한다. `verifyIfWrite` 가 `verifyToken` 을 Spring 프록시를 거치지 않고 자기 안에서 부르므로 그 메서드의 user 서킷, 재시도, 401 폴백이 적용되지 않고, 예외가 그대로 나가 500 이 된다(게이트웨이는 쓰기마다 SEVERE 'Servlet.service() ... ResourceAccessException: I/O error on POST request for "http://testbed-user:8085/api/users/verify-token": Connection refused' 를 남긴다). 장바구니 담기, 비우기, checkout 같은 모든 쓰기가 500, 새 로그인은 502 다. 상품 조회와 검색은 정상이다. PostgreSQL 과 앱 계정, 다른 commerce 서비스는 그대로다.

비유: 사옥 출입증을 새 카드로 바꾸면서 직원들 카드는 다 교체했는데, 경비실 교대 근무표 원본에는 옛 카드 번호가 그대로 적혀 있었다. 정기 교대 날 그 근무표대로 들어온 새 경비원은 옛 카드로 문을 열려다 계속 막힌다. 앞 근무자는 이미 퇴근했다. 경비실이 비자 방문증 확인을 받아야 하는 모든 출입(쓰기)이 막히고, 확인이 필요 없는 로비 구경(조회)만 된다.

## 2. 원본 사례

- 기업: E2B(코드 실행 샌드박스 클라우드)
- 날짜: 2026-06-24 20:26~20:55 PDT(2026-06-25 03:26~03:55 UTC, 약 29분). 사후 보고 "Service Disruption Postmortem" 2026-06-25, 상태 페이지 사고 "Investigating 503 status code on API"(전면 중단)
- 링크: [공식 사후 보고](https://status.e2b.dev/incidents/01KVYDD98RNPD48XQ9RREZFPN3/write-up) (`ref-real-world-incidents.md` M7 에 이미 있음, F65-R 이 더함)
- 요약(출처가 말한 것만): 근본 원인은 운영 환경과 인프라 코드(IaC) 사이 설정 드리프트다. 6월 4일 일상 운영 중 운영 DB 자격 증명을 회전했는데 운영에 직접 적용하고 IaC 에는 반영하지 않았다. 6월 25일 일상 인프라 갱신이 낡은 IaC 설정을 다시 적용해 "published a new version of the database secret with an outdated connection string" 했고, API 인스턴스가 새 비밀값을 읽어 DB 에 연결하지 못했다. 비밀값 갱신이 모든 API 인스턴스의 동시 재시작을 일으켜 "every instance reloaded the bad configuration at once, leaving no healthy instances and taking the service down". API, 샌드박스 생성, 대시보드가 모두 사용 불가였다. 당직이 잘못된 DB 비밀값을 찾아 올바른 연결 문자열을 되돌리고 연결을 확인한 뒤 복구했다. 탐지 경로, 비밀값 이름, 낡은 연결 문자열의 내용은 적혀 있지 않다. 후속: 설정, 비밀값 변경의 단계적 롤아웃과 health 확인, 운영 변경의 즉시 IaC 반영, 실제 DB 연결을 보는 health check, 비밀값 갱신과 재시작 분리.
- 현실 비중: Microsoft Teams 연구에서 배포 오류의 55% 가 인증서 만료나 회전 실수(전체의 약 11%, M7), 설정 배포는 Google 포스트모템 트리거의 31%(M1)다.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 일상 인프라 갱신이 낡은 IaC 설정을 다시 적용 | user Deployment 의 일상 롤아웃이 인프라 코드의 낡은 배포 정의를 다시 적용: 컨테이너 env `DB_USER` 가 Secret 참조 대신 값 `commerce_app` |
| 원인이 된 결함 | 운영에서 직접 회전한 DB 자격 증명이 IaC 에 반영되지 않은 드리프트. 다시 적용된 연결 정보가 회전 전 것 | 운영에서 회전한 DB 계정(지금은 Secret 의 계정)이 인프라 코드의 user 정의에 반영되지 않은 드리프트. 다시 적용된 계정 이름이 회전 전 `commerce_app` |
| 전파 경로 | 비밀값 갱신이 모든 인스턴스를 한꺼번에 재시작 → 모두 낡은 자격 증명으로 DB 연결 실패 → 건강한 인스턴스 없음 → API 503 | maxSurge 0 / maxUnavailable 1 이 하나뿐인 인스턴스를 먼저 내림 → 새 인스턴스가 낡은 계정으로 DB 인증 실패(기동 중 종료, CrashLoopBackOff) → 엔드포인트 없음 → 게이트웨이 토큰 확인 연결 거부(자기 호출이라 폴백 없음) → 쓰기 500, 로그인 502 |
| 사용자 증상 | API, 샌드박스 생성, 대시보드 전면 불가(503) | commerce 의 모든 쓰기(장바구니 담기, 비우기, checkout) 500, 새 로그인 502. 상품 목록, 상세, 검색은 정상 |
| 탐지 경로 | 보고에 없음. 당직이 잘못된 DB 비밀값을 찾음 | user 의 ERROR 'Application run failed'(원인 'password authentication failed for user "commerce_app"'), KCM BackOff 와 롤아웃 이벤트, 새 ReplicaSet 스펙의 값 DB_USER, 게이트웨이 SEVERE 'Servlet.service() ... ResourceAccessException: I/O error on POST request for "http://testbed-user:8085/api/users/verify-token": Connection refused'(쓰기마다)와 'Downstream unavailable ... /api/users/login', 쓰기 실패(500) 급증 |
| 완화와 복구 | 올바른 연결 문자열을 되돌리고 연결 확인 뒤 복구 | cleanup 이 env 배열을 원래대로(Secret 참조) 되돌리고 롤아웃 완료(새 파드가 현재 계정으로 붙어 Ready)까지 기다림 |

기전 대조: 원본의 고리는 "자격 증명을 운영에서 직접 회전 → IaC 드리프트 → 일상 갱신이 회전 전 자격 증명을 다시 적용 → 모든 인스턴스가 한꺼번에 그 설정으로 재시작 → DB 에 붙지 못해 건강한 인스턴스가 없음 → 그 서비스에 기대는 요청 전부 실패 → 올바른 연결 정보를 되돌려 복구"다. 재구성도 같은 고리다. 대상이 E2B API 에서 commerce user-service 로, 비밀값 객체에서 배포 env 로 바뀐 것은 스택에 맞춘 운반 수단의 차이다. 사용자 증상의 꼴(503 대신 쓰기 500)은 테스트베드에서 user 가 게이트웨이 인증 가드의 의존이라 생기는 전파 모양이다: 원본도 "그 서비스가 비면 그것에 기대는 모든 요청이 실패"이고, 재구성에서 user 에 기대는 요청이 쓰기와 로그인이다.

F65-R(같은 원본) 과의 관계: F65-R 은 연결 문자열 가운데 Oracle 서비스 이름을 낡게 두어 리스너가 인증 전에 거절하고(ORA-12514), 묶음을 G(설정 오배포)로 셌다. Oracle 은 실패 로그인 지연이 같은 계정의 다른 서비스로 번질 수 있어 자격 증명 쪽을 낡게 둘 수 없었다(F65-R 시트 §2). PostgreSQL 은 실패 인증에 잠금이나 지연이 없어(`pg_hba` scram-sha-256, 잠금 기능 없음) 원본의 기원 그대로 **자격 증명**을 낡게 둘 수 있다. 그래서 이 판은 원본의 "회전 전 자격 증명이 다시 적용됨"을 문자 그대로 재현하고 묶음 O(자격 증명 회전 실수)로 센다. 전파도 다르다: F65-R 은 이체 경로의 호출자들이 Connection refused 와 502 를 내고, F65-P 는 user 를 부르는 게이트웨이의 토큰 확인이 실패해 쓰기가 500 이 되고 조회는 멀쩡하다.

## 3. 숫자 근거 (2026-10-12 반복 65 재계산, `scenario-stats.py`, main 172e5a1, 정식 + 후보 합계 103, 이 후보 전)

- 묶음(정식 + 후보): J 17(16.5%), D 16(15.5%), P 13(12.6%), G 12(11.7%), L 8, A 7, B 7, C 6, E 4, F 3, H 2, I 2, K 2, Q 2, M 1, O 1, N 0. 20%(103 기준 20.6)에 닿은 묶음은 없다. 이 후보는 주 묶음 **O**(자격 증명 만료, 회전 실수: 1 → 2, 104 중 1.9%), 피해가 인증 의존 실패로 번져 **I** 를 덧붙여 O+I 로 적는다. O 는 현실 비중(배포 오류의 55%, 전체 약 11%)에 비해 우리에게 가장 적은 묶음 가운데 하나다(N 0 은 이번 실행에서 단일 계기로 막힘, rejected 45·317·EXHAUSTED 목록).
- 정답 위치(정식 + 후보): 가장 많은 곳은 은행 이체 서비스, 주문 서비스 각 8(7.8%). 이 후보는 **사용자 서비스**(1 → 2, 104 중 1.9%). 결제 경로 합계 13/103(12.6%, 이 후보 뒤 13/104 12.5%)는 이 후보와 무관하다(정답이 결제 쪽이 아니다).
- 서비스(정식 + 후보, 문제가 시작된 서비스): commerce 34, core-banking 34, food-delivery 35. 이 후보는 commerce(34 → 35).
  - 균형 규칙(원칙 2, 2026-10-12 사용자 결정, main 4a10ac0): 합계가 가장 적은 서비스 하나에서 만들고, 동률이면 그중 어느 것이든 된다. 그 서비스 안의 정답 위치는 따로 제한하지 않고 묶음, 정답 위치, 결제 경로 20% 상한만 본다. commerce 는 core-banking 과 함께 34 로 가장 적으므로 이 규칙 그대로 commerce 에서 만든다. 정답 위치 사용자 서비스는 1 → 2(1.9%)로 상한(20.8)과 거리가 멀다.
  - 경위: 반복 64 에서 설계와 검사 5개를 통과했으나, 그때 규칙("최다 서비스는 정답 위치 0 인 부품만")에서 세 서비스가 34 동률이라 셋 다 최다로 묶여 균형 규칙 예외로 보류됐다(rejected 328행). 그 전 막힘(반복 47 EXHAUSTED, rejected 304행)도 사유가 "commerce 최다인데 사용자 서비스 정답 1" 하나였다. 두 사유 모두 위 규칙 변경으로 없어졌다. 반복 65 는 보류 패치를 지금 main 위에 다시 얹어 숫자와 관문을 다시 확인했다.
  - 은행과 음식배달에 같은 기전을 두지 않는 이유: maxSurge 0 인 Deployment 는 banking transfer(F65-R 이 씀)와 commerce user 뿐이다. 다른 서비스는 기본 롤링(maxSurge 1)이라 Ready 가 못 된 새 파드 옆에 옛 파드가 남아 피해가 없다(rejected 286행, F65-R 시트 §10 2번).
- 왜 이 후보인가: 현실에 흔한 자격 증명 회전 실수(O)가 우리에게 1개뿐이고, 같은 user 부재 증상(쓰기만 실패, 읽기 정상)을 내는 정식 F16-H(readiness 프로브 오설정)와 **증상은 같고 원인이 다른** 관계라, 관제 AI 가 "user 가 없다"에서 멈추지 않고 왜 없는지(새 ReplicaSet 의 값 DB_USER, user 자신의 인증 실패 로그, 컨테이너 재시작 꼴)로 갈라야 한다.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 계기: Deployment `testbed-user`(rca-testbed-commerce, replicas 1, `commerce/k8s/25-user-service.yaml:20-24` strategy maxUnavailable 1 / maxSurge 0) 컨테이너 `user-service` 의 env 배열에서 `DB_USER` 가 `valueFrom.secretKeyRef(postgres-secret, POSTGRES_USER)`(`25-user-service.yaml:56-60`) 대신 `{"name":"DB_USER","value":"commerce_app"}` 인 롤아웃. 나머지 env(DB_PASS 의 Secret 참조, OTel 설정)는 그대로다. 109 실배치(2026-10-12 kubectl)는 매니페스트와 같은 전략, revision 46, env 11줄.
2. 옛 파드 먼저 내림: maxSurge 0 이라 컨트롤러가 옛 ReplicaSet 을 0, 새 ReplicaSet 을 1 로 같은 초에 바꾸고 옛 파드가 엔드포인트에서 빠진다(F16-H, F17-H 와 같은 꼴).
3. 기동 중 인증 실패: user 의 `spring.datasource.username`(`commerce/user-service/src/main/resources/application.yml:9-11`, `${DB_USER}`)이 `commerce_app` 이 된다. `spring.sql.init.mode: always`(`application.yml:25-29`)라 기동 때 `dataSourceScriptDatabaseInitializer` 가 연결을 얻으려 하고, PostgreSQL 16(`pg_hba` 의 원격 규칙 scram-sha-256, 109 조회)이 없는 역할 `commerce_app` 을 `FATAL: password authentication failed for user "commerce_app"` 으로 거절한다. 104 실측(2026-10-12, 109 의 `commerce-user:latest` 이미지, OTel javaagent 2.9.0, 로컬 postgres:16 에 같은 원격 규칙): 'Starting UserServiceApplication' → 'HikariPool-1 - Starting...' → WARN 'Exception encountered during context initialization - cancelling refresh attempt' → ERROR 'Application run failed'(예외 사슬 끝 `org.postgresql.util.PSQLException: FATAL: password authentication failed for user "commerce_app"`) → 종료 코드 1. 같은 실측에서 OTLP 로 받은 로그 레코드에 이 다섯 줄이 모두 있었다(예외 문장은 스택 트레이스 속성). 109 의 평시 user 기동은 'Starting' 뒤 약 14초에 Hikari 시작, 약 36초에 'Started'(119 로그 2026-10-11 10:10:47~10:11:23)라, 새 컨테이너는 뜬 뒤 약 15~20초에 죽는다.
4. 재시작 반복: kubelet 이 같은 env 로 다시 띄우고 같은 곳에서 죽어 CrashLoopBackOff(10초, 20초, 40초 … 5분) 가 된다. startupProbe(`25-user-service.yaml:84-89`, 5초 × 30)는 앱이 포트를 열기 전에 죽어 연결 거부로 실패한다. 파드는 Ready 가 되지 않는다.
5. 엔드포인트 빔: testbed-user Service 에 엔드포인트가 없어 kube-proxy 가 testbed-user:8085 로의 연결을 거절한다.
6. 게이트웨이: 쓰기 요청(POST/PUT/DELETE/PATCH, 회원가입과 로그인 제외)마다 `AuthGuard.verifyIfWrite`(`commerce/api-gateway/src/main/java/com/commerce/gateway/service/AuthGuard.java:31-48`)가 `:44` 에서 `verifyToken`(`:51-58`, @CircuitBreaker user + @Retry user)을 같은 객체 안에서 바로 부른다. Spring AOP 프록시를 거치지 않는 자기 호출이라 서킷, 재시도, `verifyTokenFallback`(`:60-63`, 401) 이 모두 적용되지 않는다. 연결 거부가 `ResourceAccessException` 그대로 컨트롤러 밖으로 나가고, `GlobalExceptionHandler`(`commerce/api-gateway/src/main/java/com/commerce/gateway/config/GlobalExceptionHandler.java:12-20`)는 `ServiceException` 만 잡으므로 서블릿이 SEVERE 'Servlet.service() ... ResourceAccessException: I/O error on POST request for "http://testbed-user:8085/api/users/verify-token": Connection refused' 를 남기고 500 을 낸다. 119 의 2026-10-11 10:06~10:11 user 장애 구간(다른 시나리오)에서 이 꼴이 실제로 남았다: 스택 `AuthGuard.verifyToken(AuthGuard.java:57)` ← `verifyIfWrite(AuthGuard.java:44)` 이고 resilience4j 프레임이 없으며, 보존 기간 전체에 'Token verification unavailable' 은 0건이다. 로그인(`/api/users/login`)은 컨트롤러가 프록시를 거쳐 부르는 `forwardToUser` 가 user 서킷으로 실패해 fallback `unavailable`(`commerce/api-gateway/src/main/java/com/commerce/gateway/service/ProxyService.java:102-106`)이 WARN 'Downstream unavailable for POST /api/users/login: ...' 을 남기고 502 를 낸다.
7. 정상인 것: 조회(GET)는 AuthGuard 를 지나지 않아 상품 목록, 상세, 검색, 장바구니 조회가 200 이다. user 를 부르는 곳은 게이트웨이뿐이다(`services.user.url` 은 `commerce/api-gateway/.../RestClientConfig.java` 에만 있음). PostgreSQL, 앱 계정, 나머지 여덟 서비스의 풀은 그대로다.

## 5. 원인 규정 (원칙 5, 6)

- 근본(`root_cause.target_id`): `commerce-user`(target_kind config). 결함을 가진 곳은 user 의 배포 설정(env DB_USER)이고 고쳐야 재발이 막히는 곳도 그곳이다.
- 계기(`trigger_target_id`): 비움. 결함을 드러낸 사건(롤아웃)도 user 배포 자신이다.
- 부분 점수(`scoring.partial`): `commerce-gateway`(쓰기 500 과 로그인 502 를 내는 곳), `commerce-postgres`(인증을 거절한 곳. 거절은 정당하다). 원인 근처지만 결함이 없는 곳이다.
- 원칙 6: 게이트웨이의 토큰 확인 요청은 정당하고, 확인할 곳이 없을 때 쓰기를 통과시키지 않는 것도 정당하다. 폴백이 걸리지 않아 401 대신 500 이 되는 것은 이 장애 전부터 있던 게이트웨이의 성질이고 user 가 비지 않으면 드러나지 않으므로 결함 보유 대상이 아니다. PostgreSQL 은 없는 계정을 거절했을 뿐이고 같은 계정의 다른 여덟 서비스는 정상으로 붙는다. 인증을 망가뜨린 것은 user 자신의 배포 설정이다.
- 원칙 5: 정답은 KCM 의 롤아웃 이벤트와 새 ReplicaSet 스펙의 env 한 줄 차이(`DB_USER` 가 Secret 참조에서 값 `commerce_app` 으로), 그 직후 user 로그의 'password authentication failed for user "commerce_app"' 라는 전수 근거 쌍으로 낸다. 코드 설계 결함 추론이 필요 없다. 왜 인프라 코드에 옛 값이 남았는지(드리프트)는 원본처럼 요구하지 않는다.

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 누가 | 무엇으로 | 이 시나리오에서 |
|---|---|---|---|
| 감지 | lucida-next | user ERROR 로그('Application run failed') 반복, KCM BackOff, 게이트웨이 SEVERE 'Servlet.service() ... ResourceAccessException: I/O error on POST request for "http://testbed-user:8085/api/users/verify-token": Connection refused'(쓰기마다)와 WARN 'Downstream unavailable', 쓰기 응답 500 급증 | 같은 user 부재 증상의 F16-H 가 정식 녹화에서 인시던트를 만들었다. 이 판은 그에 더해 user 의 ERROR 로그와 재시작 이벤트가 생긴다 |
| 피해 판정 | 러너 | 동반 부하와 기준선의 쓰기 단계 비정상 비율(loadgen.write_step_status_rate), 읽기 단계 비정상 비율 | 평시 0 근처, 고장 중 쓰기 약 1.0, 읽기 0 그대로(F16-H 실행 aea47b5c: 31초부터 쓰기 비정상 비율 1.0. 이 비율은 4xx+5xx 업무 실패 비율(`business_nonok_rate`)이라 상태 코드를 가리지 않는다) |
| 원인 설명 | RCA | §7 의 KCM 이벤트, ReplicaSet 스펙(PG), user 오류 로그(전수), 게이트웨이 로그 | 근본(값 DB_USER=commerce_app)과 그 결과(그 이름을 담은 인증 실패)가 모두 전수 데이터에 남는다 |

## 7. 관측 근거 표 (119 실조회, 2026-10-12 05:40~06:20 UTC)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 결정 증거는 KCM(이벤트 전수, ReplicaSet 스펙 전수)과 로그(전수)다.

| 증거 | 119 표와 칸 | 조회 | 평시 결과 |
|---|---|---|---|
| 근본: 계정 설정 | PG lucida `kcm_resources_history` (kind, namespace, name, yaml, captured_at) | `kind ILIKE 'replicaset' AND namespace='rca-testbed-commerce'` 수, `name LIKE 'testbed-user-%'` 수, `yaml ILIKE '%commerce_app%'` 수 | commerce ReplicaSet 1,524건(마지막 2026-10-11 20:32), 그 가운데 testbed-user 79건(2026-10-12 반복 65 재조회. 반복 64 조회 때는 1,565건과 83건이었고 보존 기간이 지난 기록이 빠졌다). 전 네임스페이스 ReplicaSet 에서 `commerce_app` 0건 |
| 근본: 스펙이 실제로 잡히는가 | 같은 표 | testbed-user 최근 5건의 yaml 에서 DB_USER 앞뒤 | testbed-user-7fd46bb8d8 등이 롤아웃마다 잡히고 yaml 에 `{"name":"DB_USER","valueFrom":{"secretKeyRef":{"name":"postgres-secret","key":"POSTGRES_USER"}}}` 가 그대로 담김 → 주입 뒤에는 `{"name":"DB_USER","value":"commerce_app"}` 로 잡혀야 한다 |
| 계기: 롤아웃 | CH `kcm_events_local` (namespace, object_name, reason, body) | `namespace='rca-testbed-commerce' AND object_name LIKE 'testbed-user%'` reason 별 수 | ScalingReplicaSet 20, SuccessfulCreate 10, Scheduled 10, Started 12, Killing 8, Unhealthy 185(대부분 다른 시나리오 실행 구간). 롤아웃 꼴이 수집된다(마지막 2026-10-01 04:14) |
| 전파: 재시작 반복 | 같은 표 | `reason='BackOff'` 전체 | 보존 기간 25건, 모두 food payment 와 commerce PostgreSQL(2026-08-25~10-01). testbed-user 의 BackOff 는 0건 → 고장 중 처음 나타나야 한다 |
| 근본 결과: user 오류 로그 | CH `lucida_logs_local` body, `log_attributes['exception.stacktrace']` | 7일(보존 시작 2026-10-05), 전 서비스 `body` 또는 스택 트레이스에 'password authentication failed', 'Application run failed', 'Error starting ApplicationContext' | 모두 0건 |
| 오류 로그가 수집되는 꼴 | 같은 표 | 3일, `mapContains(log_attributes,'exception.stacktrace')` 서비스별, 'Caused by' 포함 수 | commerce-user 683건(46건이 Caused by 사슬 포함), 다른 서비스도 예외 사슬이 스택 트레이스 속성에 통째로 담김(예: food dispatch 'Caused by: ... --read-only option'). 104 실측에서 'Application run failed' 레코드의 스택 트레이스에 PSQLException 문장이 있음(§4) |
| 기동 로그가 수집되는가 | 같은 표 | commerce-user 2026-10-11 10:10~10:12 | 'Starting UserServiceApplication', 'HikariPool-1 - Starting...', 'HikariPool-1 - Start completed.', 'Started UserServiceApplication in 39.683 seconds' 가 들어온다 |
| 평시 user 오류 | 같은 표 | commerce-user 7일 WARN/ERROR | 2026-10-11 10:06~10:11 한 구간(다른 시나리오의 users 표 잠금: 'Connection is not available', 'DataSource health check failed')뿐. 인증 실패는 없다 |
| 전파: 게이트웨이 쓰기 실패 | CH `lucida_logs_local` | commerce-gateway, body 에 'verify-token', 날짜별 수와 꼴; 'Token verification unavailable' 수 | 보존 기간(2026-10-06~) 'verify-token' 로그는 2026-10-11 10:06~10:11 한 구간(다른 시나리오의 user 장애) 209건뿐이고 그 가운데 SEVERE 'Servlet.service() ... ResourceAccessException: I/O error on POST request for "http://testbed-user:8085/api/users/verify-token": Connection refused' 이 있다(스택 `AuthGuard.verifyToken:57` ← `verifyIfWrite:44`, resilience4j 프레임 없음). 'Token verification unavailable' 은 0건. 같은 구간 로그인은 WARN 'Downstream unavailable for POST /api/users/login' (보존 전체 574건, 모두 그 구간) |
| 배제: DB 와 계정 | 109 읽기 조회(psql) | `pg_roles`, `pg_hba_file_rules`, `pg_stat_activity` | 역할 commerce, lucida_mon 만 있음(commerce_app 없음), 원격 규칙 `host all all all scram-sha-256`, PostgreSQL 16.14, 앱 계정 세션 56(JDBC). 실패 인증에 잠금이나 지연이 없다 |

## 8. 감별

- must_support(정답지 5항목): 롤아웃 이벤트와 새 ReplicaSet 스펙의 값 DB_USER=commerce_app, user 자신의 'Application run failed'(원인 'password authentication failed for user "commerce_app"') 반복, user 컨테이너 재시작과 BackOff, 게이트웨이의 쓰기마다 SEVERE 'Servlet.service() ... ResourceAccessException: I/O error on POST request for "http://testbed-user:8085/api/users/verify-token": Connection refused' 와 로그인 'Downstream unavailable', 쓰기 실패(500) 급증, 읽기 정상.
- must_rule_out(정답지 6항목): readiness 프로브 오설정(F16-H), PostgreSQL 이나 앱 계정 장애(F25-H, F66-R 꼴), user 의 느린 응답이나 자원 고갈(F16-P 꼴), 결함 있는 새 코드, 게이트웨이 자체 장애나 설정(F64-R), 부하 증가.
- contrast_with: F16-H(같은 증상, readiness 경로 404 로 컨테이너는 Running, DB 오류 없음), F16-P(같은 증상, notification 릴리스의 회원 목록 홍수로 user 가 CPU 와 풀에 묻힘, user 는 Ready 이고 느림), F65-R(같은 원본, banking transfer 의 연결 서비스 이름, ORA-12514 와 이체 502), F35-R(DB 쪽 계정 잠금 ORA-28000 으로 같은 계정의 모든 서비스가 실패).
- 러너 must_rule_out: user 가용 레플리카가 0 이 아님, 읽기 단계 비정상 ≥0.1, 진입점 429.

가르는 근거 정리: 겉 증상(쓰기만 실패, user 가용 0)은 F16-H 와 같다. 401 은 가르는 신호로 쓰지 않는다: surge.js `cart()` 의 토큰 없는 장바구니 담기가 평시에도 401 'Missing Authorization token' 을 받는다. F16-H 는 프로브 정의(readinessProbe path)가 바뀐 새 ReplicaSet 이고 컨테이너가 Running 으로 남아 'Readiness probe failed: HTTP probe failed with statuscode: 404' 가 반복되며 user 에 DB 오류가 없다. F65-P 는 바뀐 것이 env DB_USER 한 줄이고, 컨테이너가 기동 중에 죽어(재시작 수 증가, BackOff) user 로그에 그 계정 이름을 담은 인증 실패가 있다. 같은 PostgreSQL 에 같은 Secret 계정으로 붙는 여덟 서비스는 정상이라 DB 와 계정은 멀쩡하다.

## 9. 러너 판정 조건, 강도, 부하 계산 (원칙 9)

- 강도는 하나다: env 한 줄(DB_USER 를 값 commerce_app 으로). 계정 이름이 맞느냐 틀리느냐의 문제라 사다리가 없다. 설계 강도 1단 고정 evaluation(`approved-fixed-f65-p`).
- 피해가 실제로 나는 계산: maxSurge 0 이라 패치 순간 옛 파드가 엔드포인트에서 빠지고, 새 파드는 같은 env 로 다시 떠도 같은 곳에서 죽으니 Ready 가 될 길이 없다. 게이트웨이의 토큰 확인은 자기 호출이라 서킷, 재시도, 폴백이 걸리지 않고 쓰기마다 연결 거부로 곧바로 500 이 된다(흡수할 곳이 없다). 같은 user 부재 경로의 F16-H 실행(aea47b5c)에서 쓰기 비정상 비율(4xx+5xx)이 31초부터 1.0 이었다.
- 신호 생존 창: F16-H 실행에서 약 267초 뒤 부하 쪽이 새 로그인을 못 해 쓰기 자체를 보내지 않게 되며 쓰기 비율이 0.0 으로 꺼졌다(F16-H controller 주석). 그래서 F16-H 와 같은 판정 틀을 쓴다: success 쓰기 비정상 ≥0.3 3틱, min_hold 150s, timeout 13m, max_injection_duration 15m. 신호가 꺼진 뒤에도 user 는 계속 비어 있고 로그인 502 와 user 재시작이 녹화 구간에 남는다.
- 부하: 동반 north-south 35rps(commerce surge.js: 조회 35%, 장바구니 15%, checkout 50%, ramp 2m, hold 11m, ramp down 1m, F16-H 와 같은 값, seed 6565), 상주 기준선(loadgen-commerce) 위에 더한다. 상한 180 의 19%. 쓰기 단계가 비율의 분모이고, 기준선의 토큰 캐시(24시간)가 남아 있는 동안 기준선 쓰기도 같은 경로로 500 이 된다.
- abort: commerce 진입점(게이트웨이 30080) 0(연결 불가) 2틱. 게이트웨이는 user 와 무관하게 살아 있다.
- recovery: target_health 200, user 파드 Ready, 가용 1, 쓰기 비정상 <0.05 2틱(timeout 10m).
- cleanup: k8s.env 가 원래 env 배열(Secret 참조)을 되돌리면 다시 롤아웃되어(maxSurge 0) 재시작 중인 파드가 내려가고 새 파드가 현재 계정으로 붙는다(평시 기동 약 36초, 실행기 대기 상한 180초).
- PostgreSQL 부담: 기동마다 연결 시도 한 번이 인증 단계에서 거절된다(백오프로 몇 분에 한 번). 세션이 생기지 않고 PostgreSQL 에는 잠금 기능이 없어 다른 서비스의 같은 계정 로그인에 영향이 없다.

## 10. 후보 목록과 고른 이유 (3단계, 버린 이유 포함)

| # | 실제 기전 × 부품 | 원본 사례 | 결과 |
|---|---|---|---|
| 1 | 회전 전 DB 자격 증명 재적용 × commerce user(maxSurge 0, 인증 의존) | E2B 2026-06-25 공식 | **채택**(O 1, 사용자 서비스 1, commerce 34 로 banking 과 함께 가장 적음. 반복 64 설계, 반복 65 에 새 균형 규칙으로 재평가) |
| 2 | 동시성 한도(bulkhead) 설정 롤아웃이 한도를 0 으로 × commerce pricing 견적 | GitHub 2025-09-15 공식(리미터 경계 조건이 모든 요청 거절) | 원칙 3: pricing 의 BulkheadFullException 처리기, order 의 pricing 실패 경로, 게이트웨이의 5xx 전달이 모두 로그를 남기지 않아(코드 확인, 119 7일 'Pricing' WARN/ERROR 0건) 핵심 증거가 10% 트레이스뿐 |
| 3 | ENUM 수정이 표 전체 다시 쓰기 × food MySQL 쓰기 잦은 표 | Honeycomb 6월 3일(연도 미확인, 상태 페이지 z1ptbq6mz65y) | 원칙 2·카탈로그 §1: food 에 ENUM 열이 없고, 쓰기 잦은 큰 표(dispatches, payments, dispatch_events)는 이미 정답 위치로 쓰였고 같은 '표 복사 잠금' 꼴(F60-R)이며 orders 는 필수 중단 조건 |
| 4 | 새 릴리스의 느린 메모리 누수 × commerce product | Honeycomb 2019-11-06 공식 | 카탈로그 §1: F41-R 과 원본, 기전이 같은 서비스 교체(rejected 178행과 같은 벽) |
| 5 | 이동한 표의 인덱스가 UNUSABLE 로 남아 INSERT 실패 × banking OUTBOX_EVENTS | 공식 사후 보고 없음(Coveralls 2025-04-06 은 PG VACUUM FULL) | 원칙 1 |
| 6 | 프로모션 데이터의 빈 필드가 견적 코드의 null 처리 누락을 밟음 × commerce pricing | Google Cloud 2025-06-12 공식 | 카탈로그 §1: F40-R 과 같은 표(promotions), 같은 수단(db.config_row), 같은 정답 위치(가격 서비스) |
| 7 | Kafka 브로커 쪽 변경 × 세 도메인 메시지 브로커(정답 위치 0) | (여러 원본) | 원칙 7: 세 도메인 발행이 모두 outbox 비동기라 사용자 경로 증상이 없음(rejected 27·146·188행) |
| 8 | 장바구니 표 잠금 × commerce cart_items | Sendcloud 2025-09-10(자료 문서 밖) | 카탈로그 §1: F06-H(commerce payments 표 잠금)와 같은 꼴(rejected 62행), 표가 3행이라 원칙 9 |
| 9 | 외부 결제 제공자의 응답 형식 변경 × food 외부 PG | Flagsmith 2026-03-03 공식 | 순위가 1번 아래: 정답 위치 외부 결제 의존(6)과 결제 경로(13)로 지름길을 키우고, 같은 원본의 형식 변경 꼴이 이미 있다(F32-H). 설계하지 않음 |

## 11. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(E2B 2026-06-25, 링크)와 요소별 대응표가 있고 기전이 같다. F65-R 과 달리 낡은 값이 자격 증명(계정 이름)이라 원본의 회전 기원을 그대로 재현한다(§2) (원칙 1)
- [x] 분류 장부를 갱신했고 묶음 O(1 → 2, 1.9%), 정답 위치 사용자 서비스(1 → 2, 1.9%), 결제 경로 13/104(12.5%, 무관), 서비스 commerce 34 → 35(commerce 와 core-banking 34 가 가장 적어 새 균형 규칙대로 commerce 에서 만듦)를 적었다. 어느 축도 20% 에 닿지 않는다(§3) (원칙 2)
- [x] 근본 원인 위치: `commerce/k8s/25-user-service.yaml:56-60`(DB_USER 의 Secret 참조), `:20-24`(전략), `commerce/user-service/src/main/resources/application.yml:9-11`(datasource username), `:25-29`(sql.init always)(§4) (G1)
- [x] 근본 원인의 흔적이 119 에서 조회됐다: PG `kcm_resources_history` ReplicaSet yaml(testbed-user 79건, env 배열이 DB_USER 참조까지 잡힘, commerce_app 0건), CH `lucida_logs_local`(인증 실패, 'Application run failed' 평시 0건, 예외 사슬이 스택 트레이스 속성에 담김)(§7) (원칙 3)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: KCM 이벤트, ReplicaSet 스펙, 로그 모두 전수다 (원칙 3)
- [x] 계기의 흔적: testbed-user 롤아웃 KCM 이벤트와 새 ReplicaSet 스펙, 재시작과 BackOff 이벤트. 인공 지연을 쓰지 않는다 (원칙 4)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다: 롤아웃 + env 한 줄 + 그 계정 이름을 그대로 담은 인증 실패 (원칙 5)
- [x] 정답지 세 칸이 원칙 6대로 채워졌다(§5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다(§6) (원칙 7)
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: env 값 commerce_app 은 앱 계정 이름 꼴이고 비밀번호 값은 넣지 않는다(DB_PASS 는 Secret 참조 그대로). 부하 태그는 기존 관례(k6 안에만)다 (원칙 8)
- [x] 부하 상한과 서킷브레이커를 고려해 피해가 실제로 나는 계산이 있다: 엔드포인트가 비고 토큰 확인이 자기 호출이라 흡수할 폴백이 없으며(쓰기 500), 같은 경로의 F16-H 실행에서 쓰기 비정상 1.0(§9) (원칙 9)

## 12. 첫 실행에서 확인할 것

- KCM 이 새 ReplicaSet 을 `{"name":"DB_USER","value":"commerce_app"}` 로 잡는지.
- user 의 ERROR 'Application run failed' 가 스택 트레이스 속성에 'password authentication failed for user "commerce_app"' 을 담아 재시작마다 들어오는지(104 실측은 받음, 109 의 OTel 중계 경로는 첫 실행에서 확인).
- KCM 에 testbed-user 파드 BackOff 'Back-off restarting failed container user-service' 가 남는지.
- 쓰기 비정상 비율이 F16-H 처럼 30초 안에 1.0 으로 서고(쓰기 500), 게이트웨이가 쓰기마다 SEVERE 'Servlet.service() ... ResourceAccessException: I/O error on POST request for "http://testbed-user:8085/api/users/verify-token": Connection refused' 를 남기며, 읽기 비정상이 0 인지.
