---
title: F68-R 설계 시트 (banking transfer 릴리스의 계좌별 속도 제한이 이름 하나의 제한기라 모든 이체가 초당 2건을 나눠 써 두 도메인 이체가 함께 실패)
status: Draft
owner: project
last_reviewed: 2026-10-12
tags:
  - scenario
  - design
  - release
  - rate-limit
summary: banking transfer-service 를 '출금 계좌당 초당 2건' 이상 거래 방지 한도를 더한 릴리스 2.4.0 으로 롤아웃하자, 한도를 resilience4j @RateLimiter(name = "transferVelocity") 하나로 걸어 모든 계좌의 이체와 commerce 정산이 초당 2건 한 몫을 나눠 쓰고, 넘친 요청은 처리되지 않은 RequestNotPermitted 로 500 이 되어, account·api 서킷이 대부분 열린 banking 이체는 약 90% 502, 한도를 거의 혼자 쓰게 된 commerce 정산은 약 19% 실패한다. 원본 GitHub 2026-04-09.
---

# F68-R 설계 시트

## 1. 요약

banking transfer-service 는 이체를 실제로 실행하는 서비스다. 부르는 쪽은 셋이다: banking 손님 이체(loadgen → nginx → api → account → transfer), checkout 마다 하나씩 오는 commerce 정산 이체(commerce payment → transfer, 재시도 없음), 매시 정각의 commerce 정산 배치. 새 릴리스 2.4.0 은 이상 거래 방지를 더했다. Javadoc 은 "한 출금 계좌에서 1초에 2건을 넘게 들어오는 이체는 자동화된 남용으로 보고 받지 않는다, 가장 바쁜 commerce 정산 계좌도 평시 초당 1건 안쪽" 이라고 적는다. 구현은 `@RateLimiter(name = "transferVelocity")` 와 application.yml 의 `limit-for-period: 2`, `limit-refresh-period: 1s`, `timeout-duration: 0s` 다.

그런데 이름 붙은 resilience4j 속도 제한기는 계좌마다 따로 생기지 않는다. 이름 하나에 인스턴스 하나라서, "계좌당 초당 2건" 이 "은행 전체 초당 2건" 이 된다. 넘친 요청은 트랜잭션 전에 `RequestNotPermitted` 로 거절되는데 릴리스가 이 예외를 매핑하지 않아 Tomcat 이 SEVERE 를 남기고 500 으로 답한다. 상주 부하(합쳐 초당 약 0.8건)에서는 1초에 3건 이상 몰린 순간만 거절되지만, banking 동반 부하(손님 계좌 7개 사이 이체 초당 약 4건, 계좌당 0.6건 미만이라 의도한 계좌별 한도라면 하나도 걸리지 않음)가 더해지면 합친 수요 초당 약 4.8건이 한도 2건을 넘는다. 그 실패율에서 account 와 api 의 서킷브레이커가 대부분의 시간 열려 banking 이체는 열에 아홉이 api 에서 곧바로 502 이고, 서킷이 열린 동안 transfer 로 오는 호출이 한도 아래로 줄어 commerce 정산은 다섯에 하나꼴로만 거절되어 checkout 502 가 된다(서킷까지 넣은 로컬 전체 사슬 실측 0.90 대 0.19). transfer 는 Ready 이고 밀리초 안에 답하며 Oracle 은 한가하다.

비유: 은행 창구 직원이 "한 손님이 1초에 두 건 넘게 송금하면 막아라" 는 새 규칙을 받았는데, 손님마다 세는 계수기 대신 창구에 계수기 하나만 달았다. 손님이 몇 명이든 창구 전체에서 1초에 두 건만 받고 나머지는 "처리할 수 없습니다" 로 돌려보낸다. 가맹점 정산 직원도, 개인 손님도 같은 계수기에 걸린다. 창구는 한가해 보이는데 줄 선 사람 절반 넘게 빈손으로 돌아간다.

## 2. 원본 사례

- 기업: GitHub
- 날짜: 2026-04-09 (보고 머리글 "April 09 09:50 UTC (lasting 25 minutes) and 16:20 UTC (lasting 4 hours and 16 minutes)", 본문 시간표 09:05~19:05 UTC 와 16:05~20:36 UTC)
- 링크: [GitHub availability report: April 2026](https://github.blog/news-insights/company-news/github-availability-report-april-2026/) (공식 월간 가용성 보고)
- 요약(출처가 말한 것): Copilot coding agent 의 새 세션 요청 약 84% 가 지연되고 평균 오류율 83.9%(최대 97.5%), 워크플로 생성 약 22,700건이 지연되거나 실패했다. 원인은 "a bug in our rate limiting logic that incorrectly applied a rate limit globally across all users" 로, 한도가 그것을 일으킨 설치 하나가 아니라 모든 사용자에게 걸렸다. 내부 엔드포인트 호출을 3~4배로 늘린 클라이언트 갱신이 한도 소진을 앞당겼다. 두 번째 사고에서는 캐시 버그가 속도 제한 상태를 실제 창보다 오래 남겨 장애가 되풀이됐다. 15분 안에 탐지했고, 기능 플래그로 잘못된 캐시를 끄고 서비스를 설치별 자격 증명으로 옮겨 20:36 UTC 에 완전 복구했다. 후속은 이 실패 꼴의 자동 감시와 경보, 캐시 개선, 클라이언트 종류별 속도 제한 범위 분리.
- 자료 문서 `docs/ref-real-world-incidents.md` M19(레이트 리밋 오설정)에 이번에 추가했다(출처가 말한 사실만, 본문 시각과 수치는 공식 페이지에서 확인).

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 속도 제한 로직이 든 변경의 배포 | transfer-service 릴리스 2.4.0 롤아웃(이상 거래 방지 속도 제한 추가) |
| 원인이 된 결함 | 한도를 설치별로 걸어야 하는데 로직 버그로 모든 사용자에게 전역 적용 | 계좌별로 걸어야 하는 한도를 이름 하나의 resilience4j 제한기로 걸어 모든 계좌, 모든 호출자에게 전역 적용 |
| 전파 경로 | 공유된 한도가 바닥나 모든 사용자의 새 세션이 지연, 실패. 호출을 3~4배로 늘린 클라이언트 갱신이 소진을 앞당김 | 공유된 초당 2건이 바닥나 banking 손님 이체와 commerce 정산이 함께 500 → account, api 502, commerce payment, order 502. banking 동반 부하(초당 약 4건)가 소진을 앞당김 |
| 사용자 증상 | 새 에이전트 세션 약 84% 지연, 평균 오류율 83.9% | banking 이체 약 90% 502(서킷 열림 포함), commerce checkout 약 19% 502. 잔액 조회, 거래 내역, 상품 둘러보기는 정상 |
| 원본의 탐지 경로 | 15분 안에 탐지(경보) | lucida 이상 탐지(banking api, account 오류율과 오류 로그, commerce order, payment 오류), 인시던트. 원인은 transfer 의 RequestNotPermitted 와 롤아웃을 이어야 나온다 |
| 완화와 복구 | 잘못된 캐시를 기능 플래그로 끄고 설치별 자격 증명으로 옮김 | cleanup 이 transfer 이미지를 매니페스트 이미지로 되돌림(롤백). 매니페스트 버전에는 제한이 없다 |

기전 확인: 정당한 개별 사용자에게 걸려야 할 한도가 구현 결함으로 전역이 되어, 각자는 한도 안인데 합친 트래픽이 공유 한도를 넘어 모두가 실패한다. 원본처럼 트래픽을 늘린 다른 요인(원본: 클라이언트 갱신, 재구성: banking 동반 부하)이 소진을 앞당긴다. 원인에서 증상까지 고리가 같다. 우리 스택(Spring Boot, resilience4j)에서 이 결함이 가장 자연스럽게 생기는 꼴이 "주석 하나에 이름 하나" 다.

## 3. 숫자 근거 (2026-10-12, `scenario-stats.py`, 정식 + 후보 합계 100, 이 후보 전)

| 축 | 값 | 판단 |
|---|---|---|
| 서비스 | commerce 34, core-banking 33, food-delivery 33 | 은행과 음식배달이 가장 적다(동률). 은행 33 → 34 |
| 묶음(20% = 20.2) | J 16, D 14, P 13, G 12, L 8, A 7, B 7, C 6, E 4, F 3, H 2, I 2, K 2, Q 2, M 1, O 1, N 0 | 주 묶음 J(결함 있는 새 버전 배포) 16 → 17(101 중 16.8%), 상한 아래. 한도 값은 의도대로면 맞고 결함은 제한기 범위를 정한 코드라 G 가 아니다 |
| 정답 위치 | 은행 이체 서비스 7(최다는 주문 서비스 8) | 7 → 8(7.9%), 상한 아래 |
| 결제 경로 합계 | 13(13%) | 이 후보와 무관(13/101) |
| 부품 지도 | core-banking: transfer 9, account 11, oracle 9, api 5, ledger 2, kafka 0, nginx 0 | kafka, nginx 는 0 이지만 앞선 반복들에서 막힘(비동기, 119 미수집). transfer 는 상한 아래 |
| 장부와 catalog 대조 | 어긋남 없음 | 장부 정정 없음 |

왜 이 후보인가: 세 축 모두 상한 아래이고, 원본(GitHub 2026-04-09)과 결함 꼴(계좌별 한도가 전역 한도가 됨)이 앞선 시나리오에 없다. 같은 "요청 한도 릴리스" 인 F55-R(food dispatch, 원격 주소별 한도가 order 하나를 한 클라이언트로 봄, 429)과는 원본, 도메인, 응답(처리되지 않은 500), 피해 범위(두 도메인의 모든 호출자, 실패 비율이 합친 부하를 따라감)가 다르다. 은행과 음식배달 동률 가운데 은행을 고른 이유: 같은 결함을 food payment 에 두면(주문당 결제 한도) order 가 하류 4xx 를 그대로 넘기는 구조라 처리된 429 는 4xx 단일 신호가 되고, food 에는 이미 F55-R 이 있다. transfer 는 commerce 와 banking 두 도메인의 호출을 함께 받아, 공유 한도가 "서로 다른 호출자를 함께 깨는" 원본의 모습을 그대로 보여 준다.

### 3단계 후보 목록 (실제 기전 × 부품, 숫자 순, 버린 이유 포함)

| # | 기전 × 부품 | 원본 사례 | 결과 |
|---|---|---|---|
| 1 | M19 속도 제한 범위 버그 × banking transfer(공유 호출 대상) | GitHub 2026-04-09 공식 | **채택** |
| 2 | 같은 기전 × food payment(주문당 결제 한도) | GitHub 2026-04-09 공식 | 버림(원칙 7): order PaymentClient 가 하류 4xx 를 그대로 넘겨 처리된 429 는 4xx 단일 신호, 같은 도메인에 F55-R |
| 3 | M21 통계 자동 분석이 계획 뒤집음 × commerce PostgreSQL 표(0 위치) | Clerk 2026-02-19 공식 | 버림(원칙 1): 세 DB 에 거의 전부 NULL 인 열을 거르는 뜨거운 질의가 없어 통계를 지어내야(재현이 아니라 조작) 함 |
| 4 | M2 릴리스 질의가 임시 공간을 채움 × banking Oracle TEMP | MIT Open Learning 2026-03-24 공식 | 버림(원칙 9, 1): TEMP 자동 확장이 노드 디스크를 채워 F10-R 벽, 막으려면 TEMP 최대 크기 변경이라는 두 번째 계기 |
| 5 | M5 저장 용량 상한 × banking Oracle Free 12GB | Instapaper 2017-02, CircleCI 2026-10-06(공식 페이지 원문 미확보) | 버림(원칙 1, 9): 109 USERS 할당 10.7GB 중 BANKING 사용 5.3GB 라 상한에 닿으려면 수 GB 를 억지로 채워야 함 |
| 6 | M22 잘못된 대상 DB 명령 × banking FREEPDB1 닫기 | GitLab 2017-01-31 공식 | 버림(원칙 1): 원본은 데이터 디렉터리 삭제라 되돌릴 수 있는 닫기는 다른 고리, 남는 꼴은 F25-H 의 DB 중단 |
| 7 | M1 설정 변경이 DB 연결 급증 × banking Oracle processes | Atlassian Confluence 2026-01-08 공식 | 버림(원칙 1, 9): 원본이 설정 내용을 밝히지 않고, 피해 쪽 Hikari 가 쥔 연결을 유지해 굶는지가 경합 운(막힌 목록 OpenAI 2026-09-29 행) |
| 8 | M4 중첩 트랜잭션 hold-and-wait × banking transfer 정산 경로 | WorkOS 2026-07-16 공식 | 버림(원칙 9): 동시 요청이 풀(15)에 닿아야 하는데 정산 이체 10ms 대라 수천 rps 필요, 거래 내역으로 동시성을 올리면 Oracle 이 먼저 참(막힌 목록 WorkOS 행과 같은 벽) |
| 9 | M5 TIME_WAIT 포트 고갈 × banking api → account | Jim's Pckt 2026-04 운영자 블로그 | 버림(원칙 1, 9): 개인 운영 블로그라 공신력이 약하고 180rps × 60초가 포트 범위에 못 미침 |
| 10 | M7 보존 표시를 무시한 자동화가 쓰이는 키를 지움 × banking DB Secret | Azure AD 2021-03-15 공식 RCA 사본 | 버림(원칙 1): Secret 이 사라지면 인증 실패가 아니라 컨테이너 생성 실패라 고리가 다르고 F17-H, F52-R 꼴, 공식 Secret 사례 없음 |
| 11 | M10 소비자 그룹 재조정 실패 × banking ledger Kafka | Socure 2026-06-25 공식 상태 페이지 | 버림(원칙 7): 원장 적재가 비동기라 사용자 증상 없음 |
| 12 | M21 즉시 못 하는 열 변경이 표 재구성 × food MySQL 큰 표 | Astalty 2026-06-29 공식 사고 기록 | 버림(원칙 1, 카탈로그 §1): 엔진과 표 미공개, 남는 꼴이 F60-R(표 복사 잠금) |
| 13 | M11 잘못된 네트워크 설정 배포로 헬스 체크 실패 × banking 서비스 | GitHub 2026-02-12 공식 | 버림(카탈로그 §1, 원칙 9): transfer 에 두면 F17-H, F65-R 꼴(새 파드 미준비), 나머지는 기본 롤링이 옛 파드를 남김 |

부품 9종(banking transfer, food payment, commerce PostgreSQL, banking Oracle TEMP·용량·PDB·processes, banking api, banking Secret, banking Kafka, food MySQL), 앱, DB, 인프라를 함께 냈다. 지휘 세션이 넘긴 막힌 후보와 같은 원본·같은 주입은 없다(GitHub 2026-04-09 는 처음 쓰는 원본. PostHog 2025-10-24 의 F55-R 과는 원본과 수단 파라미터가 다르다).

## 4. 인과 사슬 (코드와 인프라 위치)

1. 롤아웃: `core-banking/k8s/22-transfer-service.yaml:17-18`(maxUnavailable 1, maxSurge 0) 이라 옛 파드가 먼저 내려가고 약 1분 모든 이체 실패, 새 파드(2.4.0) Ready.
2. 결함: `scripts/scenarios/fault-images/f68-r/transfer-service.patch:17-24`(TransferService.execute 위 Javadoc 과 `@RateLimiter(name = "transferVelocity")`), `:34-41`(application.yml transferVelocity: 초당 2건, 대기 0). resilience4j 의 RateLimiterRegistry 는 이름마다 인스턴스 하나를 두므로 계좌와 무관하게 모든 호출이 한 몫을 나눠 쓴다. 제한기 측면(aspect)이 `@Transactional` 보다 바깥이라 거절은 트랜잭션, Hikari, Oracle 에 닿기 전에 끝난다(로컬 스택 추적에서 TransactionInterceptor 가 거절 지점 위에 없음).
3. 거절의 모양: `core-banking/transfer-service/src/main/java/com/corebanking/transfer/config/GlobalExceptionHandler.java:10-21` 은 ServiceException 만 다뤄 RequestNotPermitted 는 처리되지 않고 Tomcat 이 SEVERE `Servlet.service() for servlet [dispatcherServlet] in context with path [] threw exception [Request processing failed: io.github.resilience4j.ratelimiter.RequestNotPermitted: RateLimiter 'transferVelocity' does not permit further calls] with root cause` 를 남기며 500(Whitelabel 페이지).
4. banking 손님 경로: `core-banking/account-service/src/main/java/com/corebanking/account/client/TransferClient.java:28-58` 이 5xx 를 `Transfer service call failed for order null: 500 : "<html>...Whitelabel..."` 로 남기고 502, 재시도 3회(200ms 지수), transferClient 서킷(10건 창 50%, 5초 열림) → `core-banking/api-service/src/main/java/com/corebanking/api/client/AccountClient.java:28-58` 이 재시도와 서킷 끝에 502.
5. commerce 경로: `commerce/payment-service/src/main/java/com/commerce/payment/client/BankingTransferClient.java:37-58` 이 'core-banking transfer call failed: 500 ...' 502(재시도 없음), 결제 트랜잭션 롤백 → `commerce/order-service/src/main/java/com/commerce/order/service/OrderService.java:189-196` 'Checkout payment failed, releasing reserved stock' 와 502, 매시 `SettlementBatch` 'Settlement banking transfer failed'.
6. 제한 밖: 잔액 조회와 계좌 목록(nginx → account), 거래 내역(api → transfer GET)은 execute 를 지나지 않는다.

### 로컬 실측 (2026-10-12, 원칙 9 근거)

로컬 gvenzl/oracle-free:23-slim(2 CPU, core-banking/db/init.sql 의 스키마와 계좌 시드)에 매니페스트 jar(origin/main transfer-service)와 2.4.0 jar(같은 소스 + fault-images/f68-r 패치)를 따로 띄우고, 손님 이체(ACC-1001~1007 무작위 쌍, 정수 금액)와 정산 이체(commerce-settlement → commerce-merchant, 소수 둘째 자리)를 지수 간격(개방 루프)으로 60초씩 보냈다.

| 버전 | 손님 초당 / 정산 초당 | 손님 결과 | 정산 결과 |
|---|---|---|---|
| 매니페스트 | 0.35 / 0.45 (기준선 수준) | 25건 모두 200 | 34건 모두 200 |
| 매니페스트 | 4.35 / 0.45 (동반 부하 수준) | 261건 모두 200 | 20건 모두 200 |
| 2.4.0 | 0.35 / 0.45 | 16건 중 1건 500(6%) | 30건 중 2건 500(7%) |
| 2.4.0 | 4.35 / 0.45 | 265건 중 158건 500(60%) | 26건 중 14건 500(54%) |

2.4.0 의 동반 부하 수준에서 500 은 일곱 손님 계좌 모두(계좌마다 17~29건)와 commerce-settlement 에 고르게 떨어졌다. ACC-1003 에 연달아 세 번 보낸 직후 다른 계좌 ACC-1004 의 첫 이체도 500 이었다(계좌별이 아님). 거절 응답은 수 ms, 로그는 위 3번의 SEVERE 와 스택(`RateLimiterAspect.handleJoinPoint` 아래 `TransferService$$SpringCGLIB$$0.execute`, TransactionInterceptor 없음). 매니페스트 jar 는 같은 부하에서 500 이 0 이었다.

### 서킷까지 넣은 전체 사슬 실측 (2026-10-12, 1차 평가 반영)

위 표는 transfer 를 직접 불러 사이의 재시도와 서킷브레이커가 빠져 있다(평가 지적). api(accountClient 서킷, 재시도 3) → account(transferClient 서킷 10건 창 50% 5초 열림 반열림 3, 재시도 3) → transfer 2.4.0 을 실제 설정 그대로 로컬에 띄우고, 손님 이체는 api 로(nginx 자리), 정산 이체는 transfer 로 직접(commerce payment 자리, 재시도·서킷 없음) 보냈다.

| 부하(손님 / 정산 초당) | 시간 | 손님 이체 5xx(api 응답) | 정산 5xx(transfer 응답) |
|---|---|---|---|
| 4.35 / 0.45 (동반 부하 수준) | 6분 | 1,577건 중 1,417건 502(0.90), 30초 창 0.84~0.94 | 168건 중 32건 500(0.19), 30초 창 0~0.44 |
| 0.35 / 0.45 (기준선 수준) | 3분 | 62건 중 4건(0.06) | 77건 중 7건(0.09) |

동반 부하 6분 동안 account 로그의 'transferClient' is OPEN 978건, api 로그의 'accountClient' is OPEN 3,752건. 서킷이 열려 있는 동안 api 가 곧바로 502 를 내고 transfer 로 오는 호출이 줄어, 재시도도 서킷도 없는 commerce 정산이 한도를 거의 혼자 쓴다. 그래서 commerce 쪽 실패는 해석 계산(58%)보다 훨씬 낮고 30초 창마다 크게 흔들린다. 지휘 세션의 이산 사건 모의(banking 0.89, commerce 0.10~0.18)와 같다. commerce checkout 은 판정 조건에서 뺐다(§9).

## 5. 원인 규정 (원칙 5, 6)

- `root_cause.target_id`: core-banking-transfer(transfer-service 의 새 릴리스 2.4.0). 결함(계좌별로 의도한 한도를 이름 하나의 제한기로 건 것)이 여기 있고, 되돌리면 재발이 막힌다.
- `trigger_target_id`: 비움. 계기도 같은 롤아웃이다. 동반 부하는 원본의 "트래픽을 늘린 클라이언트 갱신" 에 해당하는 정당한 요청이고 계좌당 속도는 의도한 한도 안이다(계좌당 초당 0.6건 미만).
- `scoring.partial`: core-banking-account, core-banking-api(502 를 낸 banking 증상 위치), commerce-payment(정산 실패를 502 로 바꾼 commerce 증상 위치).
- 원본이 지목한 층위: "rate limiting logic 의 버그" 곧 한도를 건 쪽의 코드. 같은 층위로 transfer 의 새 버전을 지목한다. 패치의 어느 줄인지는 요구하지 않는다(원칙 5). 관제 데이터로 낼 수 있는 결론: "transfer 2.4.0 롤아웃 뒤 transfer 가 RateLimiter 'transferVelocity' 로 모든 계좌의 이체 일부를 거절" — 예외 이름과 롤아웃 시각, 거절이 계좌와 무관하게 퍼진 모습이 모두 녹화된다.

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 |
|---|---|
| 감지(lucida) | core-banking-api, core-banking-account, core-banking-transfer 오류율과 오류 로그(ERROR, SEVERE) 급증, commerce-order, commerce-payment 오류 로그와 checkout 502, APM error_rate. 별도 업무 규칙 없이 오류율과 오류 로그로 인시던트가 생긴다 |
| 피해 판정(러너) | 동반 부하 이체 5xx 비율 ≥ 0.6 (3틱). commerce checkout 실패(약 0.19)는 판정에 넣지 않고 녹화 증거로 본다 |
| RCA | KCM 롤아웃과 2.4.0 태그, transfer SEVERE RequestNotPermitted, 완료가 초당 약 2건에 묶이고 거절이 계좌와 무관, transfer 건강(Ready, Hikari·Oracle 평시) |

## 7. 관측 근거 표 (119 실조회, 2026-10-12 01:00~01:35 UTC, 평시)

| 증거 | 119 표, 칸 | 실제 조회 | 결과(평시) |
|---|---|---|---|
| 롤아웃과 이미지 태그 | CH `kcm_events_local`(namespace, object_name, reason, body) | `reason='Pulled' AND object_name LIKE 'testbed-transfer%'`, 30일 | 'Container image "core-banking-transfer:latest" already present on machine' 52건(마지막 2026-10-11 17:12), F42-R 후보 실행의 2.2.0 2건. ScalingReplicaSet 'Scaled up replica set testbed-transfer-... to 1 from 0' 도 보임 |
| 제한기 거절 로그 | CH `lucida_logs_local`(service_name, severity_text, body) | `body LIKE '%RateLimiter%' OR body LIKE '%RequestNotPermitted%'`, 30일 | 0건. 같은 꼴의 Tomcat 'Servlet.service() ... threw exception' SEVERE 는 transfer 에서 258건(CannotCreateTransactionException, F42-R 등 실행 때) 수집되어 이 문장이 전수 로그로 들어옴을 확인 |
| 완료 속도와 호출자 | CH `lucida_logs_local` 'Transfer COMPLETED ... from=...' | 10분 단위, 7일 분 단위 분위수 | 평시 분당 정산 중앙값 28(p99 58), 전체 49(p99 98), 손님 계좌별 분당 중앙값 3(p99 10). 정산 계좌도 의도한 계좌별 한도(분당 120) 안 |
| 하류 실패 로그 | CH `lucida_logs_local` core-banking-account 'Transfer service call failed ... 500 : "<html>...Whitelabel..."', commerce-order 'Checkout payment failed ... core-banking transfer call failed: 500 ...' | 7일 | account 500 Whitelabel 76건, commerce 'transfer call failed: 500' 은 F42-R 실행 구간(2026-10-11 17:0x)과 정각 정산 배치에서만. 평시에는 없다 |
| 서버 스팬 | CH `otel_traces_local`(service_name, span_name, span_attributes['http.response.status_code']), 10% 표본 | transfer POST /api/transfers, 7일 | 200 22,611, 400 81(F40-H 실행), 500 15(F42-R 실행). 표본이라 보조 증거로만 씀 |
| APM 지표 | VM `apm.agent.otel.java.error_rate`, `rps`, `percentile95`{service_name="core-banking-transfer"} | 시계열 존재 확인 | 있음(표본 기반 서비스 단위, 추세 신호로만) |
| transfer 건강 | KCM 파드 상태, 로그 'Connection is not available'(평시 0, F42-R 때만), DPM Oracle 세션·CPU | 롤아웃 뒤 녹화에서 확인 | 이 고장에서 늘지 않아야 한다(감별) |

핵심 증거(제한기 SEVERE, 하류 오류 로그, KCM 롤아웃)는 전수 로그와 KCM 이벤트에 있고, 표본 데이터(트레이스, APM 지표)에만 기대지 않는다.

## 8. 감별

- must_support: 롤아웃과 2.4.0 태그 / transfer SEVERE RequestNotPermitted 와 DB 스팬 없는 500 / 완료 초당 약 2건 상한과 계좌 무관 거절 / account, api, commerce 의 502 와 판정 비율 / transfer 건강(재시작, Hikari 대기, ORA, 잠금 없음, Oracle 평시, 잔액 조회와 거래 내역 200).
- must_rule_out: F42-R(같은 하류 500 Whitelabel 이지만 'Connection is not available' 과 Oracle 포화) / F40-H(400 'whole won', commerce 만) / F17-R, F17-H, F65-R, F44-P(도달 불가, 연결 거부나 시간 초과) / DB 쪽(F01-P, F35-R, F54-R, F57-R: ORA 와 대기) / 한 계좌의 정당한 차단(거절 계좌들의 속도가 계좌당 1건에 한참 못 미침) / commerce 쪽 변경.
- contrast_with: F42-R, F40-H, F55-R, F44-P, F17-R, F64-P.
- 같은 증상 다른 원인(H 관계)의 가치: 하류 로그만 보면 F42-R 과 같아 관제 AI 가 transfer 쪽 근거(예외 이름, Oracle, Hikari)를 따로 봐야 갈린다. F55-R 과는 "요청 한도가 사실상 전역" 이라는 꼴이 같아 계좌와 무관한 거절, 처리되지 않은 500 을 근거로 써야 한다.

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

- 강도: 설계 강도 하나로 고정(approved-fixed-f68-r, release 2.4.0). 강도 사다리 없음. min_hold 12m, settle 60s, timeout 20m, max_injection_duration 25m.
- 동반 부하: load.north_south, core-banking transfer-balance-surge.js, target_rps 7, ramp 2m, hold 21m, entry 30082(banking nginx), seed 6868, 기준선 단위 loadgen-banking. 이체 55%(초당 약 3.9건, 손님 계좌 7개, 계좌당 0.6건 미만), 잔액 조회 35%(account 직행), 목록 10%, 거래 내역 단계 없음(2026-10-22 운영 보강 1회차에 transfer-heavy-surge.js 10rps 에서 바꿈: 거래 내역 15% 의 Oracle 전수 COUNT 는 실측 실행당 CPU 0.48초(평시)~0.68초(부하)로 초당 1.5건이면 1.0~1.5 CPU 라 Oracle 이 먼저 찼다).
- 계산: 합친 수요 = 동반 이체 4 + 기준선 손님 이체 약 0.35 + commerce 정산 약 0.45 = 초당 약 4.8건, 제한기 통과 초당 2건 → 서킷이 없다면 논리적 실패율 하한 1 - 2/4.8 = 58%(transfer 직접 실측 60%, 54%). 실제 사슬에는 서킷이 있다: 실패율 50% 이상에서 account transferClient 와 api accountClient 서킷이 열려(5초, 반열림 3건) 열린 동안 banking 이체는 transfer 에 닿지 않고 곧바로 502 다. 그래서 banking 동반 이체 5xx 는 0.90 으로 오르고(30초 창 0.84~0.94), transfer 로 오는 호출은 한도 아래로 줄어 재시도도 서킷도 없는 commerce 정산은 약 0.19 만 거절된다(§4 전체 사슬 실측).
- success: 동반 부하 이체 5xx ≥ 0.6, 3틱 연속(실측 0.90 의 아래 여유). commerce checkout 5xx 는 30초 창 0~0.44 로 흔들려 판정에 넣지 않고 must_support(녹화의 commerce 'Checkout payment failed ... core-banking transfer call failed: 500')로 옮겼다.
- must_rule_out: achieved_rps < 2.5, transfer 재시작 ≥ 2, Oracle NotReady (2틱).
- abort: banking 진입점(nginx) entry_status 0 (2틱). 거절은 nginx, api, account 를 살려 둔 채 502 로 답하므로 이 고장으로는 0 이 되지 않는다.
- recovery: target_health 200, transfer Ready, available 1, 기준선 banking 이체 5xx < 0.05 (2틱, 10분).
- 롤아웃 공백: maxSurge 0 이라 약 1분 연결 거부(F17-H 꼴)지만 min_hold 12m 뒤 판정이라 공백만으로 판정되지 않는다.
- 데이터 부작용: 거절된 이체는 트랜잭션 전에 끝나 transfers 행이 없고, commerce 결제는 되돌려진다. 고칠 데이터 없음. 거절된 commerce checkout 의 재고는 order 가 돌려준다.
- **2026-10-22 운영 보강 1회차: 동반 부하에서 거래 내역 단계를 뺐다**(profiles.json `scenario_parameters.F68-R.script_path` → `/opt/loadgen/core-banking/transfer-balance-surge.js`, target_rps 10 → 7, 정답지 description, injection_summary, mechanism(영, 한), controller 노트, 실행 행렬). 근거 `~/dev/testbed-ops/verifications/2026-10-22-1632-F68-R-run-8b3f61c5.md` §3-1, §6: 주입(15:25:12 testbed-transfer 2.4.0 롤아웃), 피해(동반 이체 5xx 0.82~0.94), 원인 근거(RequestNotPermitted 352줄, 30일 0, 일곱 계좌와 정산 계좌에 고른 거절, Hikari, ORA, 재시작 0)는 섰으나, transfer-heavy-surge.js 10rps 의 거래 내역 단계(15%, 초당 약 1.5건)가 건수 질의 byb2y2hv7p4ag 를 분당 22~41회에서 117~140회로 늘려 Oracle CPU 0.29 → 최고 1.92/2, db_time 5.16, active_session 11, `resmgr:cpu quantum` 대기가 나 must_rule_out ①(F42-R 아님: Oracle CPU, 활성 세션, Top SQL 평시)과 must_support ⑤ 에 걸렸다(겹친 실행 없음). F37-H(f3f71c4), F47-R·F63-R(1ad6679) 과 같은 방법이다. 7rps 는 이체 55% 로 초당 약 3.9건이라 설계 계산의 손님 이체 초당 4건과 같은 수준이고(합친 수요와 제한기 통과 2건/초 계산, success 0.6, 서킷 사슬은 그대로), F37-H-run-a6cd30bd 는 같은 교체 뒤 Oracle CPU 0.25~0.42 였다. 주입, success, must_rule_out(achieved_rps < 2.5 는 7rps 의 약 36%), must_support 는 그대로다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(GitHub 2026-04-09, 공식 월간 가용성 보고)와 요소별 대응표가 있고 기전이 같다(원칙 1) — §2
- [x] 분류 장부 갱신, 묶음 J 16 → 17(16.8%), 정답 위치 은행 이체 서비스 7 → 8, 결제 경로 13/101, 서비스 은행 33 → 34, 이 후보를 고른 이유를 적었다. 어느 축도 20% 아래(원칙 2) — §3, 장부 §4-1·§6
- [x] 근본 원인 위치를 `file:line` 으로 확인(G1) — §4
- [x] 근본 원인 흔적이 119 실데이터에 남는 경로를 조회로 확인(KCM Pulled 이미지 태그, Tomcat SEVERE 전수 로그)(원칙 3) — §7
- [x] 핵심 증거가 표본 데이터에만 있지 않다(로그 전수, KCM)(원칙 3) — §7
- [x] 계기 흔적(롤아웃, 이미지 태그)이 조회됐고 인공 지연을 쓰지 않는다(원칙 4)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다(예외 이름, 롤아웃, 계좌 무관 거절). 코드 설계 결함을 추론할 필요는 없다(원칙 5)
- [x] 정답지 세 칸이 원칙 6대로(근본 transfer 릴리스, 계기 비움, 부분 점수 증상 위치) — §5
- [x] 감지, 피해 판정, RCA 증거를 따로 적었다(원칙 7) — §6
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다. 패치가 더한 문자열에 F68, fault, bug, chaos, scenario 없음(원칙 8)
- [x] 부하 상한(180 중 7rps, 2026-10-22 보강 전 10rps)과 서킷브레이커를 고려해 피해가 날 계산과 로컬 실측(서킷까지 넣은 전체 사슬)이 있다(원칙 9) — §4, §9

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- 새 파드 'Pulled ... core-banking-transfer:2.4.0' 과 SEVERE RequestNotPermitted 가 녹화에 실제로 있는지.
- 동반 부하 이체 5xx 가 0.6 을 넘는지(로컬 0.90), commerce checkout 실패가 로컬처럼 0.1~0.2 대인지.
- transfer 가 재시작 없이 Ready 이고 Oracle CPU, 세션이 평시인지(F42-R 과 가르는 근거).
- 인시던트가 banking 과 commerce 중 어디에 몇 건 생기는지.
