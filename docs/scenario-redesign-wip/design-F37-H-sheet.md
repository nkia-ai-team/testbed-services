---
title: F37-H 설계 시트 (banking api-service 를 운영에 없는 이체 한도 설정을 읽는 릴리스로 롤아웃해 이체 접수 전량 500)
status: Draft
owner: project
last_reviewed: 2026-10-11
tags:
  - scenario
  - design
  - release
summary: banking api-service 를 결함 있는 새 릴리스(core-banking-api:1.6.0, fault-images/f37-h 패치로 만든 별도 태그)로 롤아웃하면, 새로 더한 1회 이체 한도 확인이 이체마다 읽는 정책값이 릴리스의 개발 프로필 파일에만 있고 운영 설정에는 없어, Ready 인 api 가 banking 진입점으로 들어온 모든 이체를 account 를 부르기 전에 500 으로 끝내는 시나리오. 거래 내역, 잔액 조회, commerce 정산은 정상이다. F37-R(api 파드 dnsPolicy 배포로 이체와 내역 502)의 같은 증상 다른 원인.
---

# F37-H 설계 시트

## 1. 요약

banking api-service 의 새 릴리스 1.6.0 은 이체 접수(`POST /api/transfers`, `ApiService.submitTransfer`)의 요청 검증 바로 뒤에 "1회 이체 한도" 확인을 더한다. 운영 정책값이라 재배포 없이 바꿀 수 있게 하겠다며 한도를 기동 때 묶지 않고 **이체마다** `Environment.getRequiredProperty("api.policy.single-transfer-limit", BigDecimal.class)` 로 읽는다. 값은 릴리스에 함께 든 개발 프로필 파일(`application-dev.yml`, 3000000)에만 있고, 운영의 기본 `application.yml`, ConfigMap `service-config`, 파드 env, 활성 프로필 어디에도 없다.

기동과 `/actuator/health` 는 이 값을 읽지 않는다. 그래서 새 파드는 startup, readiness, liveness 프로브를 통과해 Ready 가 되고, 기본 롤링 전략(maxSurge 25%)으로 옛 파드를 대신한다. 그 순간부터 banking nginx 가 api 로 보내는 모든 이체가 'Submitting transfer' 로그와 account 호출 앞에서 `IllegalStateException: Required key 'api.policy.single-transfer-limit' not found` 를 던진다. api 의 `GlobalExceptionHandler` 는 `ServiceException` 만 다루므로 수 ms 만에 500 이 되고 Tomcat 이 SEVERE 'Servlet.service() ... threw exception' 을 남긴다. 거래 내역 조회(api → transfer), 잔액 조회와 계좌 목록(nginx → account 직행), commerce 정산(commerce payment → transfer 직행)은 정상이다.

비유: 은행 창구(api)에 "1회 이체 한도를 넘으면 받지 마라" 는 새 규정이 내려왔는데, 규정집 본문에는 한도 금액이 없고 금액은 연습용 사본(dev 프로필)에만 적혀 있다. 창구는 이체 신청이 올 때마다 본문에서 금액을 찾다 못 찾아 신청을 돌려보낸다. 잔액 조회 창구와 거래 내역 창구, 가맹점 정산 전용 창구는 평소대로 일한다.

## 2. 원본 사례

- **ServiceChannel, 2025-05-08** (공식 상태 페이지 사고와 사후 보고, 게시 2025-05-15): https://status.servicechannel.com/incidents/445h94td476q
  - 사고 제목 "Code Release Causes US Environment Outage". 예정된 미국 운영 코드 릴리스 뒤 2:29 AM EDT 부터 로그인 어려움(3:07 AM 까지)과 핵심 대시보드 기능 중단(4:12 AM 까지). 영향 Major, 사용자 Many, 빈도 Continuous.
  - 대시보드 원인: 개발 환경에는 올바르게 설정된 설정 하나가 "had not been fully synchronized to the production environment", 새 코드가 그 설정에 접근하려다 실패. 이 틈은 새 코드가 배포 중 그 설정에 접근할 때까지 드러나지 않았다.
  - 탐지: SRE 경보. 완화: 대시보드는 필요한 설정을 모두 운영에 적용해 회복. 후속: 배포 절차에 설정 검증.
  - 릴리스 내용과 설정 이름은 적혀 있지 않다. 로그인 쪽은 재구성하지 않는다.
- `docs/ref-real-world-incidents.md` M2 에 이미 있다(F58-R 때 추가). 같은 원본의 food 판이 F58-R(order 주문 생성), F36-H(restaurant 가게 상세)이고, 이 시트는 그 은행 판이다.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 예정된 운영 코드 릴리스 | api-service 를 릴리스 core-banking-api:1.6.0 으로 롤아웃(KCM ScalingReplicaSet, 새 파드 Pulled 이벤트에 태그) |
| 원인이 된 결함 | 새 코드가 접근하는 설정이 개발 환경에만 있고 운영에 동기화되지 않음 | 새 1회 이체 한도 확인이 이체마다 읽는 `api.policy.single-transfer-limit` 이 릴리스의 `application-dev.yml` 에만 있고 운영 설정(application.yml, ConfigMap, 파드 env)에는 없음 |
| 전파 경로 | 새 코드가 설정에 접근할 때 실패 → 그 기능(대시보드) 불가 | 이체 접수에서 `getRequiredProperty` 실패 → 처리되지 않은 예외로 500 → banking 공개 이체 기능 불가(다른 기능은 정상) |
| 사용자 증상 | 핵심 대시보드 기능 사용 불가 | banking 진입점으로 들어온 모든 이체가 500. 잔액, 계좌 목록, 거래 내역, commerce 정산은 정상 |
| 탐지된 경로 | SRE 경보 | api 오류율, Tomcat SEVERE 로그 급증으로 lucida 이상 탐지와 인시던트(원칙 7) |
| 완화와 복구 | 필요한 설정을 운영에 적용 | cleanup 이 매니페스트 이미지로 되돌림(설정을 운영에 더해도 회복) |

기전은 원본과 같다: "새 코드가 운영에 없는(개발 환경에만 있는) 설정을 읽는다 → 배포 뒤 그 기능을 처음 쓸 때 실패한다 → 설정을 맞추거나 되돌릴 때까지 그 기능만 계속 실패한다". 바꾼 것은 대상(대시보드 → banking 이체 접수)과 규모다.

## 3. 숫자 근거 (2026-10-11, `scenario-stats.py`, 정식 + 후보 94 → 95)

| 축 | 이 후보 | 정식 + 후보 (전 → 후) | 상한 20% |
|---|---|---|---|
| 묶음 | J(결함 있는 새 버전 배포) + G | J 15 → 16 (16.8%) | 아래 |
| 정답 위치 | 은행 API | 4 → 5 (5.3%) | 아래 |
| 결제 경로 | 해당 없음 | 12/95 (12.6%) | 아래 |
| 서비스 | 은행 | 31 → 32 (쇼핑몰 32, 음식배달 31) | 균형 |

- 은행과 음식배달이 31 로 가장 적고 쇼핑몰이 32 라, 은행에서는 상한 아래인 어느 정답 위치도 낼 수 있다. 은행 부품 중 정답 위치 수가 가장 적은 요청 경로 서비스가 api(4)다(원장 1 은 Kafka 소비자 뒤 비동기라 사용자 증상이 없다).
- 0 인 묶음(N)과 적은 묶음(M, I, O, K, H, Q, F, E) 후보는 아래 목록처럼 모두 관문에서 막혔다.

### 후보 목록 (실제 기전 × 부품, 순서는 원칙 2 숫자 순)

| # | 후보 | 원본 사례 | 부품 | 묶음 / 정답 위치 | 판정 |
|---|---|---|---|---|---|
| 1 | 운영자가 공간을 되찾으려고 TRANSFERS 에 ALTER TABLE MOVE(오프라인 재구성) | Coveralls 2025-04-06(공식) | banking Oracle 이체 표 | D / DB 테이블(이체) 2 | 버림: 원칙 1, 9. 로컬 Oracle 26ai Free(109 와 같은 23-slim, 2 CPU, 같은 크기 645만 행 768MB 사본) 실측: MOVE UPDATE INDEXES 54.8~59.5초 동안 INSERT 가 줄 서지 않고 처음 약 15초만 ORA-01502(인덱스 unusable)로 즉시 실패, 그 뒤는 성공. 원본의 '잠금 뒤에 질의가 쌓여 연결 고갈' 고리가 아니고 피해 창도 짧다. F67-R 시트 6행의 막힘 사유(세 도메인 동률)는 이제 풀렸지만 실측이 막는다 |
| 2 | 배포 마이그레이션이 TRANSFERS 에 비온라인 CREATE INDEX | Harness 2026-01-26(공식, 이번에 M21 에 추가) | banking Oracle 이체 표 | D / DB 테이블(이체) 2 | 버림: 원칙 9. 같은 로컬 사본에서 (from_account, created_at) 인덱스 생성이 9.8초, 그동안 INSERT 대기. 원본 표는 7천만 행 이상(13분), 109 는 645만 행이라 판정 창이 서지 않음 |
| 3 | 취소된 인덱스 백필이 commerce order_items 를 치움 | GitHub 2026-07-24(공식) | commerce PostgreSQL 주문 항목 표 | P / DB 테이블(주문 항목) 0 | 버림: 원칙 1. 원본은 Vitess(MySQL) 백필 워크플로의 취소 코드 경로(F48-R, F48-P 가 MySQL 로 재구성)이고, PostgreSQL 은 인덱스를 그림자 표 없이 CREATE INDEX CONCURRENTLY 로 만들어 같은 고리가 일어날 도구 경로가 없다 |
| 4 | 마이그레이션 인덱스 생성이 높은 동시성 표에 차단 잠금 | Jamf School 2025-09-24(공식, 이번에 M21 에 추가) | food MySQL | D | 버림: 같은 주입. 온라인 인덱스 생성의 메타데이터 잠금 대기열은 F60-H(db.ddl MYSQL_QUEUE_REMOTE) 꼴이고, 원본이 엔진과 문장을 밝히지 않음 |
| 5 | commerce 주문 스키마 표들(orders, order_items) VACUUM FULL | Coveralls 2025-04-06 | commerce PostgreSQL | D / DB 테이블(주문) 1 | 버림: 원칙 2. 쇼핑몰이 최다(32)라 정답 위치 0 이어야 하는데 주문 표는 1(F53-R). order_items 단독은 막힌 목록(34초) |
| 6 | 원장 보존 삭제 설정 오배포로 원장 행 삭제(H) | 공식 사례 없음 | banking ledger | H / 은행 원장 서비스 1 | 버림: 원칙 1, 7. 대사 배치 WARN 이 10분에 한 번뿐이고 원장은 비동기 경로 |
| 7 | 이체 접수 정리 배치(StaleTransferCleanup) 주기 설정 오배포 | Slack 2025-11-01(공식) | banking transfer | A | 버림: 원칙 9. fixedDelay 단일 스레드라 Oracle 세션 하나(막힌 목록 53행과 같은 벽) |
| 8 | 운영 도구의 무거운 질의가 food MySQL CPU 독점 | Onfido 2025-03-13(공식) | food MySQL | A | 버림: 원칙 9. 짧은 OLTP 가 굶지 않는다는 banking 실측(막힌 목록 115행)과 같은 계산이고, 피해는 자기 풀을 쥔 비싼 질의(F33-H, F42-P)에서만 났다 |
| 9 | 메시지마다 프로듀서를 만드는 릴리스, 소비자 독 메시지 | PagerDuty 2023-08-28 등 | banking/food Kafka | B / 메시지 브로커 0 | 버림: 원칙 7. 세 도메인 Kafka 발행이 outbox 비동기뿐(막힌 목록 188, 258행) |
| 10 | 개발 프로필에만 있는 정책값을 읽는 릴리스 | ServiceChannel 2025-05-08(공식) | banking api | J+G / 은행 API 4 | **채택** |

후보 10 은 F58-R, F36-H 와 원본과 결함 꼴이 같다. 카탈로그 §1 의 R 조건(DB 엔진, 진입 경로, 영향 범위 중 하나 이상 다름)을 넘는다: 도메인(food → banking), 진입 경로(NodePort 직행 → nginx → api), 영향 범위(기능 하나만, 같은 api 의 다른 기능은 정상)가 다르다. 사례군은 증상이 같은 F37(api 롤아웃 뒤 banking 이체 실패)의 H 로 둔다: F37-R 과 겉 증상이 겹치고 원인이 다르므로, 관제 AI 는 롤아웃 객체의 바뀐 칸(이미지 대 dnsPolicy)과 오류 문장(설정 조회 대 이름 해석), 거래 내역의 성패로 갈라야 한다.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 롤아웃: `core-banking/k8s/20-api-service.yaml:9`(replicas 1), `:27-28`(image core-banking-api:latest, imagePullPolicy Never), `:31-33`(envFrom service-config), `:57-76`(세 프로브 모두 /actuator/health). 기본 전략 maxSurge 25% 라 새 파드 Ready 뒤 옛 파드가 내려간다(109 KCM 2026-10-09: Started 14:07:41 → 옛 파드 Killing 14:08:07, 26초).
2. 결함: `scripts/scenarios/fault-images/f37-h/api-service.patch:31-38`(`submitTransfer` 의 요청 검증 뒤 `environment.getRequiredProperty("api.policy.single-transfer-limit", BigDecimal.class)`), `:46-52`(값은 `application-dev.yml` 에만).
3. 운영 설정에 값 없음: `core-banking/api-service/src/main/resources/application.yml:1-66`(server, services, resilience4j, management 뿐), `core-banking/k8s/02-configmaps.yaml`(service-config 에 정책 키와 SPRING_PROFILES_ACTIVE 없음, 테스트로 고정).
4. 예외 처리: `core-banking/api-service/src/main/java/com/corebanking/api/config/GlobalExceptionHandler.java:9-21`(ServiceException 만) → IllegalStateException 은 DispatcherServlet 을 거쳐 Tomcat SEVERE 와 500.
5. 영향 범위: `ApiService.java:28-37`(submitTransfer, 확인은 'Submitting transfer' 로그와 `accountClient.requestTransfer` 앞), `:40-42`(listTransfers 는 그대로), banking nginx `02-configmaps.yaml:38-46`(/api/transfers → api, /api/accounts → account), commerce `02-configmaps.yaml:46`(BANKING_TRANSFER_URL 로 transfer 직행).
6. 로컬 실측(2026-10-11): origin/main core-banking 에 패치를 얹어 `./mvnw -o -pl api-service -am package` 로 빌드한 jar 를 띄우니 health UP, `POST /api/transfers` 500 과 로그 'Servlet.service() for servlet [dispatcherServlet] ... threw exception [Request processing failed: java.lang.IllegalStateException: Required key 'api.policy.single-transfer-limit' not found] with root cause'. 같은 jar 를 `--spring.profiles.active=dev` 로 띄우면 금액 1000 은 account 호출로 넘어가고(하류가 없어 502), 5000000 은 400 'amount exceeds the single transfer limit 3000000'.

## 5. 원인 규정 (원칙 6)

- `root_cause.target_id`: `core-banking-api`(container). 결함을 가진 곳이자 고쳐야(되돌리거나 운영 설정을 더해야) 재발이 막히는 곳이다.
- `trigger_target_id`: 비움(근본과 같은 곳의 롤아웃).
- `scoring.accept`: core-banking-api, banking-api, testbed-api. `partial`: testbed-nginx, banking-nginx(진입점에서 500 을 본 답).
- 원본 포스트모템도 "새 코드 릴리스가 운영에 동기화되지 않은 설정을 읽음" 을 원인으로 지목했다. 같은 층위다. 관제 데이터만으로 "api 의 새 릴리스가 설정 키를 못 찾는다" 까지 바로 간다(오류 문장이 키 이름을 담는다). 코드 설계 추론은 필요 없다(원칙 5).

## 6. 감지, 피해 판정, RCA 증거

- 감지(lucida): api 서버 오류율과 Tomcat SEVERE 로그 급증, banking 이체 실패.
- 피해 판정(러너): 동반 부하 이체 5xx 비율(business.5xx.rate, 평시 0) ≥ 0.5, 3틱.
- RCA: KCM 롤아웃과 Pulled 이벤트의 새 태그, api SEVERE 'Required key ...' 로그, api 'Submitting transfer' 소멸, account 로 가는 자식 스팬 없는 500 서버 스팬, 하류와 DB 무오류.

## 7. 관측 근거 (119 실조회, 평시, 2026-10-11 15:00~15:40 UTC)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 결정 증거는 전수 로그와 KCM 이벤트다.

| 증거 | 119 표와 칸 | 조회 | 평시 결과 |
|---|---|---|---|
| 근본: 설정 조회 예외 | CH `lucida_logs_local`(body) | 전 서비스 `countIf(position(body,'Required key')>0)`, `'IllegalStateException'` | 0, 0(보존 창 시작 2026-10-04 12:00 UTC) |
| 근본 경로가 CH 에 남는가 | 같은 표, `position(body,'Servlet.service')>0` banking·food | 서비스별 집계 | core-banking-account SEVERE 443, core-banking-transfer 53, food 여러 서비스. 같은 OTel 에이전트 설정의 api 도 같은 경로(api SEVERE 1주 1건) |
| 계기: 롤아웃과 이미지 태그 | CH `kcm_events_local`(namespace, object_name, reason, body) | `reason='Pulled'` banking, `object_name LIKE 'testbed-api%'` | Pulled 'Container image "core-banking-account:1.3.0" already present on machine'(F41-R 실행 2026-10-11 13:30)처럼 태그가 남는다. testbed-api 롤아웃은 ScalingReplicaSet, Started, Killing 으로 남음(2026-10-09 14:07~14:08) |
| 전파: 이체 접수 로그 | `lucida_logs_local` api 'Submitting transfer' | 시간별 | 시간당 357~2,670건. 고장 중 0 이 된다(확인이 이 로그 앞) |
| 전파: api 스팬 | CH `otel_traces_local` api | 1일 span_name 별 | POST /api/transfers SERVER 2,627(표본), POST CLIENT 2,421, GET /api/transfers 5,501. 고장 중 POST 서버 스팬만 500, POST 클라이언트 스팬 소멸 |
| 배제: transfer 정상 | `lucida_logs_local` transfer 'Transfer COMPLETED' | 1시간 | 2,386 중 commerce-settlement 1,308. 고장 중에도 정산 몫은 그대로 |
| 배제: 다른 변경 | `kcm_events_local` banking | 주입 구간 | testbed-api 외 롤아웃, 재시작이 없어야 한다 |

계기의 흔적: 롤아웃(ScalingReplicaSet)과 새 파드 Pulled 이벤트가 새 태그를 담고, 같은 시각부터 오류 문장이 시작된다. 인공 지연은 없다(원칙 4).

원칙 8: 패치가 더한 문자열에 시나리오 id, fault, bug, chaos, scenario 가 없다(테스트). 실행기 argv 에 id 가 없다(테스트).

## 8. 감별

- must_support: 정답지 `must_support` 4항.
- must_rule_out: F37-R(이름 해석), F47-R·F63-R(api 다른 릴리스), account 고장(F39-R, F39-H, F41-R, F41-H, F51-H, F51-R), transfer·Oracle 고장(F17-H, F42-R, F50-R, F52-R, F65-R, F35-R, F57-R, F01-P), api 자원 고갈.
- contrast_with: F37-R, F58-R, F47-R, F63-R, F39-H.
- 가르는 근거 한 줄: 새 ReplicaSet 이 이미지 태그를 바꾸고, 오류가 api 자신의 'Required key ... not found' 이며, 같은 api 의 거래 내역과 다른 진입 경로(잔액, commerce 정산)는 정상이다. F39-H(같은 '이체 접수만 실패')는 정답이 account 릴리스이고 api 로그가 account 응답 해석 실패(502)다.

## 9. 러너 판정 조건과 강도, 부하 계산

- 주입: k8s.image release 모드 1단 고정(`approved-fixed-f37-h`). 강도 축이 없다: 설정이 없으면 이체마다 실패한다.
- 동반 부하: load.north_south `core-banking transfer-heavy-surge.js` 20rps(이체 40% = 초당 8건), ramp 2m, hold 22m, entry 30082(F63-R 과 같은 스크립트와 세기). 부하 상한 180 의 11%.
- 피해 계산(원칙 9): 실패가 시간 초과가 아니라 api 안의 즉시 예외라 서킷브레이커(accountClient 는 부르기 전)와 무관하게 이체의 100% 가 5xx 다. 30초 창 하나에 이체 약 240건.
- success: transfer_5xx_rate ≥ 0.5, 3틱 연속. min_hold 5m(새 파드 기동 약 30초와 ramp 2m 를 넘김), settle 60s, timeout 20m, max_injection_duration 25m.
- must_rule_out: achieved_rps < 5, transfer 파드 NotReady, Oracle 파드 NotReady(2틱). account 파드는 러너 관측 허용 목록(`live_probes.APPROVED_K8S_TARGETS`)에 없어 러너 조건으로 두지 않고 녹화로 감별한다(잔액 조회 비정상 비율을 기록).
- abort: entry_status == 0(nginx 나 노드가 죽음). api 는 Ready 로 500 을 답하므로 0 이 나지 않는다.
- recovery: target_health 200, api Ready, 기준선 이체 5xx < 0.05, 2틱, 10m.
- cleanup: 이미지를 core-banking-api:latest 로 되돌리고 available 확인, 노드에서 1.6.0 참조 제거, 부하 종료. 고장 동안 쓴 데이터가 없다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(ServiceChannel 2025-05-08, 공식)와 요소별 대응표, 기전이 원본과 같다 (§2)
- [x] 분류 장부를 갱신했고 묶음 J(15→16, 16.8%), 정답 위치 은행 API(4→5), 결제 경로 12/95, 서비스 은행 31→32 와 고른 이유를 적었다. 어느 축도 20% 미만 (§3)
- [x] 근본 원인 위치가 `file:line`(api-service.patch:31-38, :46-52, GlobalExceptionHandler.java:9-21)과 인프라 지점(20-api-service.yaml, 02-configmaps.yaml)으로 확인됐다 (§4)
- [x] 근본 원인의 흔적이 119 에 남는 경로를 조회했다: lucida_logs_local 의 Tomcat SEVERE 경로(banking 수집 확인), 'Required key' 평시 0, kcm_events_local Pulled 이벤트의 태그 (§7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: 로그 전수, KCM 이벤트 (§7)
- [x] 계기 흔적: 롤아웃과 Pulled 이벤트의 새 태그, 같은 시각부터 오류 문장. 인공 지연 없음 (§7)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다: 오류 문장이 키 이름을 담고 롤아웃 직후 시작 (§5)
- [x] 정답지 세 칸이 원칙 6대로 채워졌다 (§5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다 (§6)
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: 패치 문자열과 argv 에 id 없음(테스트) (§7)
- [x] 부하 상한과 서킷브레이커를 고려해 피해가 실제로 날 계산이 있다: 즉시 예외라 이체 100% 실패, 동반 20rps (§9)
