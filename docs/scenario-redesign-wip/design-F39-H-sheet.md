---
title: F39-H 설계 시트 (banking account-service 를 이체 접수 응답 구조를 바꾼 릴리스로 롤아웃)
status: Draft
owner: project
last_reviewed: 2026-10-11
tags:
  - scenario
  - design
  - release
summary: banking account-service 를 결함 있는 새 릴리스(core-banking-account:1.6.0, fault-images/f39-h 패치로 만든 별도 태그)로 롤아웃하면, 공개 API 정비로 이체 접수 응답의 status 가 문자열에서 {code, label} 객체로 바뀌어 account 와 transfer 는 이체를 실행하고 200 으로 답하는데 그 응답을 읽는 api 가 해석에 실패해 api 를 거치는 모든 이체가 502 로 실패하는 시나리오. 원본은 GitHub 2021-10-08 Codespaces API 응답 구조 변경으로 기존 클라이언트가 깨진 장애. F39-R(account 하류 주소 오설정)과 사용자 증상이 같고 원인이 다른 H.
---

# F39-H 설계 시트

## 1. 요약

banking account-service 의 새 릴리스 1.6.0 은 공개 API 를 정비하면서 이체 접수 `POST /api/accounts/transfer` 의 응답을 새 `TransferReceiptResponse` 로 바꾼다. 앱 화면이 처리 결과를 바로 보여 주도록 `status` 를 문자열 `"COMPLETED"` 대신 `{"code":"COMPLETED","label":"이체 완료"}` 객체로 내려준다. 계좌 조회, 계좌 목록, health 끝점, transfer 호출과 DB 는 그대로라 새 파드는 프로브를 통과하고 기본 롤링 전략(maxSurge 25%)으로 옛 파드를 대신하며, 이체마다 계좌를 검증해 transfer 에 넘기고(transfer 가 실행해 'Transfer COMPLETED' 를 남김) 200 으로 답한다. 그런데 이 끝점의 유일한 호출자인 api-service(nginx `/api/transfers` → api → account)는 응답을 공용 DTO `TransferResponse`(status 는 `String`)로 읽는다. Jackson 이 객체를 문자열로 바꾸지 못해 RestClient 가 `Error while extracting response for type [com.corebanking.common.dto.TransferResponse] and content type [application/json]` 을 던지고, api 는 `Account service call failed for order null: ...` ERROR 를 남기고 502 를 돌려준다. api 의 재시도 3회는 이미 실행된 이체를 account 에 다시 보내 다시 실행되게 하고, accountClient 서킷브레이커가 열리면 반열림 시도 말고는 account 를 부르지 않고 502 로 답한다. 사용자에게는 api 를 거치는 모든 이체가 약 0.6초 만에 실패하는데, account 에 닿은 시도는 돈이 실제로 옮겨진다.

비유: 은행 창구(account)가 새 양식의 영수증을 쓰기 시작했다. 예전 영수증은 "처리 결과: 완료" 한 칸이었는데, 새 영수증은 "처리 결과: [코드] COMPLETED / [표시] 이체 완료" 라는 표다. 송금은 금고(transfer)에서 이미 끝났고 영수증도 나왔지만, 예전 양식만 읽도록 만들어진 접수 기계(api)는 영수증을 읽지 못해 손님에게 "실패" 를 알린다. 손님이 다시 누르면 금고는 또 송금한다. 창구도 금고도 멀쩡하고, 고칠 곳은 영수증 양식을 바꾼 창구의 새 릴리스다.

## 2. 원본 사례

- **GitHub, 2021-10-08** (공식 월간 가용성 보고, 2021년 10월): https://github.blog/2021-11-03-github-availability-report-october-2021/
- 17:16 UTC 부터 1시간 36분. 공개 API 출시 과정에서 Codespaces 의 핵심 API 응답 하나가 의도치 않게 구조가 바뀌었고("inadvertently restructured"), 안정된 스키마("stable schema")에 기대던 기존 API 클라이언트가 깨졌다. VS Code 데스크톱 클라이언트에서 새 Codespace 를 시작할 수 없었고, 웹 편집기와 기존 데스크톱 세션은 저하됐다. 모니터링이 처음에는 영향을 잡지 못했고, 사고 중 시작된 무관한 배포가 되돌림을 늦췄다. 회귀를 되돌려 모든 클라이언트가 다시 연결됐다.
- 자료 문서 `ref-real-world-incidents.md` M8 표에 이미 있다(F49-R 때 추가). F49-R(food restaurant 가게 상세), F49-P(food payment 결제 응답)와 같은 원본이다.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 공개 API 출시를 위한 Codespaces 서비스 변경 배포 | account-service 를 릴리스 core-banking-account:1.6.0(공개 API 정비)으로 롤아웃(KCM ScalingReplicaSet, 새 파드 Pulled 이벤트에 태그가 남음) |
| 원인이 된 결함 | 핵심 API 응답 구조가 의도치 않게 바뀜(기존 클라이언트가 기대던 안정 스키마를 깸) | 이체 접수 응답의 status 가 문자열에서 {code, label} 객체로 바뀜(호출자가 기대던 `TransferResponse.status: String` 을 깸) |
| 전파 경로 | 응답을 읽는 기존 클라이언트(VS Code 확장)가 해석 실패 → 새 Codespace 시작 불가 | 응답을 읽는 api 의 AccountClient 가 해석 실패 → 재시도, 서킷 → 이체 접수 502 |
| 사용자 증상 | 데스크톱에서 새 Codespace 시작 불가, 기존 세션과 웹 편집기 저하. 서비스 자체는 응답함 | api 를 거치는 모든 이체가 502. 잔액 조회, 계좌 목록, 거래 내역, commerce 정산은 정상이고 account 와 transfer 는 200 으로 답함 |
| 원본의 탐지 경로 | 모니터링이 처음엔 못 잡음(클라이언트 쪽 실패), 내부 서비스 경계 모니터링을 늘리기로 함 | lucida-next 의 api 오류율, api ERROR 로그 급증. account, transfer 서버 스팬은 200 이라 서비스 경계(호출자 쪽)에서만 보인다 |
| 완화와 복구 | 회귀 되돌림 | cleanup 이 account 이미지를 매니페스트 이미지로 되돌림(롤백) |

기전 동일성: 원본과 재구성 모두 "API 제공자의 배포가 응답 구조를 바꿨고, 제공자는 정상 응답하는데 그 구조에 기대던 기존 클라이언트가 해석에 실패해 기능이 멈춘다" 는 고리다. 대상(Codespaces API → 이체 접수 API)과 클라이언트(VS Code 확장 → api-service)만 우리 스택에 맞게 바꿨다. 결함은 원본처럼 한 응답, 한 필드 꼴로 작게 두었다. 이 테스트베드에서는 그 응답이 쓰기 호출의 결과라, 응답만 읽히지 않고 쓰기(이체)는 실행된다는 것이 피해 설명에 더해진다(원칙 5: 정답이 아니라 user_impact).

## 3. 숫자 근거 (2026-10-11, `scenario-stats.py`, 정식 47 + 후보 44 = 91, 이 후보 전)

- 서비스(정식 + 후보): commerce 31, **core-banking 30**, **food-delivery 30** → core-banking 31. 쇼핑몰이 최다라 쇼핑몰은 정답 위치 0 인 부품만 낼 수 있고, 은행과 음식배달이 같은 최소다.
- 묶음: J 14(15.4%), G 12, D 11, P 11, **L 7(7.7%)** → 8(92 중 8.7%), A 7, B 7, C 6, E 4, F 3, H 2, K 2, Q 2, I 1, M 1, O 1, N 0. J 로 세도 14 → 15(16.3%). 20% 상한(18.4) 아래
- 정답 위치: **은행 계좌 서비스 5(5.5%)** → 6(6.5%). 최다는 주문 서비스 8(8.8%)
- 결제 경로 합계 12(13.2%) → 12/92(13.0%), 이 후보와 무관
- 왜 이 후보인가: 3단계 후보 목록(§11)을 숫자 순으로 줄 세운 뒤 관문을 통과한 첫 후보다. 0 인 묶음(N)과 0 인 부품(kafka, notify, notification, nginx)은 이번 실행에서 같은 벽(사용자 경로 밖의 비동기 소비, nginx 관측 없음, 서킷브레이커와 낮은 부하)에 막혀 있다. 그다음 기준인 최소 서비스(은행, 음식배달 30)에서 L 묶음(7.7%)과 은행 계좌 서비스 정답 위치(5.5%)가 모두 낮다. 같은 원본의 account 판은 2026-10-09 반복 6 에서 원칙 2(그때 은행 15 로 최다)로만 반려됐는데 지금 숫자로 그 사유가 풀렸다. 음식배달 dispatch 판(같은 원본, POST 배차 응답)도 같은 순위였으나 dispatch 정답 위치와 order 503 증상 시나리오가 이미 여섯이라 은행 판을 먼저 냈다.
- 증상이 F39-R(account 하류 주소 오설정)과 같다는 것은 카탈로그 관계 H 로서 추론을 요구하는 가치이고, 둘을 가르는 관측 근거는 §8 에 적는다.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 계기: `rca-testbed-banking` Deployment `testbed-account` 이미지 `core-banking-account:latest` → `core-banking-account:1.6.0`(`core-banking/k8s/21-account-service.yaml:27-28`, imagePullPolicy Never, nodeSelector tb-w2 `:18-19`, startupProbe `/actuator/health` `:67-69`, readinessProbe `/actuator/health` `:73-75`, livenessProbe `/actuator/health/liveness` `:81-83`, 기본 전략 maxSurge 25%).
2. 결함(릴리스 1.6.0): `scripts/scenarios/fault-images/f39-h/account-service.patch:16-22`(AccountController.transfer 가 TransferReceiptResponse 를 돌려줌), `:37-44`(TransferReceiptResponse, status 가 Status(code, label) 객체, of() 가 코드와 표시 이름을 채움). 매니페스트 버전은 `core-banking/account-service/src/main/java/com/corebanking/account/controller/AccountController.java:44-47` 에서 공용 `TransferResponse` 를 그대로 돌려준다.
3. 실행은 그대로: `account-service/.../service/AccountService.java:58-81`(validateAndForward: 두 계좌 조회와 검증, INFO 'Account validation passed ... forwarding to transfer-service', transferClient.executeTransfer) → transfer 의 `TransferService.execute`(계좌 잠금, 잔액 이동, 'Transfer COMPLETED', transfers 행과 outbox 기록).
4. 계약: `core-banking/shop-common/src/main/java/com/corebanking/common/dto/TransferResponse.java:7-10`(status 는 String).
5. 호출자: `core-banking/api-service/src/main/java/com/corebanking/api/service/ApiService.java:28-38`(submitTransfer) → `api/client/AccountClient.java:28-47`(`body(TransferResponse.class)` 해석 실패 → RestClientException → `log.error("Account service call failed for order {}: {}")` → ServiceException 502), `:50-58`(fallback 'Account service circuit open/exhausted' → 502). 재시도 3회 200ms 지수, 서킷 10건 창 50% 5초 열림 반열림 3건, 읽기 10초(`api-service/src/main/resources/application.yml:8-52`). Resilience4j 기본 순서는 Retry 가 바깥이라 재시도 한 번 한 번이 account 호출(곧 이체 실행)이고 서킷에 실패로 세어진다.
6. 경로: `core-banking/k8s/02-configmaps.yaml:38-50` nginx `/api/transfers` → api, `/api/accounts` → account 직행. 이 끝점(`/api/accounts/transfer`)의 호출자는 저장소 전체에서 api 하나다(grep). commerce-payment 는 transfer 를 FQDN 으로 직접 부르고, commerce 기준선 부하는 transfer NodePort 로 직접 이체한다.
7. 영향 밖: 잔액 조회(`GET /api/accounts/{id}`), 계좌 목록은 공용 DTO 그대로이고, 거래 내역은 api 가 transfer 응답을 문자열로 그대로 넘긴다(`TransferController.list`). 그래서 동반 부하의 잔액 조회, 거래 내역, 목록은 200 이다.

## 5. 원인 규정 (원칙 6)

- `root_cause.target_id`: `core-banking-account`(target_kind container). 결함을 가진 곳이자 고쳐야(되돌려야) 재발이 막히는 곳은 응답 계약을 깬 account 새 릴리스다. api 의 요청 A(이체 접수)는 정당했고, 그것을 받은 B(account 1.6.0)의 응답이 계약을 어겼으므로 원칙 6 의 "B 의 로직이 잘못" 경우다. 원본 포스트모템도 응답을 바꾼 API 쪽 회귀를 되돌렸다(같은 층위). 결함 이미지 규칙 7(근본은 그 서비스의 새 버전)과도 맞는다.
- `trigger_target_id`: 없음(계기인 롤아웃과 근본이 같은 곳).
- `scoring.partial`: `core-banking-api`, `banking-api`(증상을 낸 곳, 해석 실패 로그가 나는 곳).
- 코드 줄(패치의 어느 줄)을 맞히라고 요구하지 않는다(원칙 5). "account 롤아웃 직후 api 가 account 응답을 해석 못 함, account 와 transfer 는 일을 끝냄 → account 릴리스" 는 관제 데이터(KCM 이벤트, api 로그, 스팬 상태, account, transfer 로그)로 낼 수 있는 결론이다. 재시도가 이체를 다시 실행한다는 것은 정답이 아니라 피해 설명(user_impact)에 적는다.

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 | 기대 |
|---|---|---|
| 감지(lucida-next) | api 서버 스팬 오류율(POST /api/transfers 502), api ERROR 로그 급증(해석 실패 문장, 평시 0) | 별도 규칙 없이 이벤트, 인시던트 생성. 같은 꼴(api 502 + api 오류 로그)의 F39-R 이 정식 녹화됐다 |
| 피해 판정(러너) | 동반 부하 k6 문서의 이체 step 5xx 비율 ≥ 0.5 (3틱) | 평시 0(업무 거절은 400), 장애 시 약 1.0 |
| 원인 설명(RCA) | KCM 롤아웃 이벤트와 이미지 태그, api 로그 문장(대상 DTO 이름), account, transfer 서버 스팬과 클라이언트 스팬 200, account 'Account validation passed' 와 transfer 'Transfer COMPLETED' 지속 | §7 |

인시던트가 생길 조건: F39-R 시트 §6 의 판정 기전 요약을 따른다. 고장 중 api 서버 스팬은 이체 요청 거의 전부가 502 다(서킷이 열리면 잔액 부족 400 몫도 502). 동반 부하 5rps 중 이체 40% 는 초당 2.0건이고 기준선(1~4 iter/s, 이체 10%)을 더해 api 이체는 초당 약 2.1~2.4건, 10% 표본으로 분당 약 13개 ERROR 서버 스팬이다. 평시 api 의 5xx 는 다른 시나리오 구간 말고는 0 이다(§7). banking 선례는 promote 지연이 18~20분이라 고장 약 14분 뒤 회복 후에 promote 될 수 있다(F39-R 과 같다).

## 7. 관측 근거 (119 실조회, 평시, 2026-10-11 11:00~12:30 UTC)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 핵심 증거는 KCM 이벤트, 자원 이력, 전수 로그다.

| 증거 | 119 표와 칸 | 조회 | 결과 |
|---|---|---|---|
| 계기: 롤아웃과 이미지 태그 | CH `lucida.kcm_events_local`(namespace, object_name, reason, body) | `namespace='rca-testbed-banking' AND timestamp BETWEEN '2026-10-11 03:20' AND '03:26'` 롤아웃 이벤트 | banking transfer 릴리스 실행(F17-H)의 'ScalingReplicaSet: Scaled up replica set testbed-transfer-598d85f5f4 to 1', 'ErrImageNeverPull: Container image "core-banking-transfer:2.1.0" ...' 가 수집됨(이벤트 본문에 태그가 담김). testbed-account 는 30일 동안 2026-10-09 20:34, 20:49 의 롤아웃(ScalingReplicaSet 8건, Pulled 2건) 뿐 |
| 계기: ReplicaSet, 파드 스펙의 이미지 | PG lucida `kcm_resources_history`(kind, name, yaml, captured_at) | `namespace='rca-testbed-banking' AND name LIKE 'testbed-account%'` 종류별 수와 `position('core-banking-account:latest' in yaml)>0` 수 | replicaset 32건, pod 27건 전부 :latest(마지막 2026-10-09 20:50). 같은 표에 2026-10-11 03:23 testbed-transfer-598d85f5f4 의 image `core-banking-transfer:2.1.0` 이 잡혀 있어, 장애 때 1.6.0 ReplicaSet 이 남아야 한다 |
| 전파: api 해석 실패 로그 | CH `lucida.lucida_logs_local`(service_name, severity_text, body) | `service_name LIKE 'core-banking%' AND (body LIKE '%Error while extracting response%' OR body LIKE '%Cannot deserialize%')` 30일(banking 로그 보존 시작 2026-10-04 12:00) | 0건(같은 문구는 commerce-gateway, commerce-product, food-delivery-order 의 다른 시나리오 구간뿐) |
| 전파: api 평시 ERROR 꼴 | 같은 표 | core-banking-api `body LIKE 'Account service%'` 3일, 숫자를 지운 문장별 | 'Insufficient balance' 400 전달(쌍으로 2,012건씩), 'Connection refused', 'Transfer service unavailable', 'CircuitBreaker ... is OPEN' 등 다른 시나리오 구간 문장뿐. 'Error while extracting response' 0건 |
| 감별: 하류는 일을 끝냄 | 같은 표 | 시간별 api 'Submitting transfer', account 'Account validation passed', transfer 'Transfer COMPLETED ... from=ACC-', 'from=commerce-settlement' (최근 26시간) | 시간당 api 380~1,490, account 372~1,453, transfer ACC 525~2,109, commerce 정산 584~2,899. 장애 때 api 접수는 그대로인데 account 검증과 transfer ACC 완료가 이어지고(재시도와 반열림 시도만큼), commerce 정산은 그대로여야 한다 |
| 감별: 스팬 상태 | CH `otel_traces_local`(service_name, span_kind, span_name, span_attributes['server.address'], ['http.response.status_code']) | api, account 의 POST 스팬 최근 하루 | api SERVER POST /api/transfers 200 2,090(502 211, 400 69 는 다른 구간), api CLIENT → testbed-account 200 2,090, account SERVER POST /api/accounts/transfer 200 2,090, account CLIENT → testbed-transfer 200 2,090. 장애 때는 api SERVER 만 502 이고 나머지 셋은 200 이어야 한다 |

로컬 실측(2026-10-11, 104, gvenzl/oracle-free:23-slim + core-banking init.sql, origin/main 2b5ad8c 로 빌드한 api, transfer jar 와 패치를 얹은 account jar, Kafka 없음, OTel 에이전트 없음):
- 릴리스 account: `POST /api/accounts/transfer` → 200 `{"transferId":"727baf07-...","status":{"code":"COMPLETED","label":"이체 완료"}}`, 잔액 조회 200, api 경유 거래 내역 200, health UP.
- api 경유 이체 1건: 502 `Account service unavailable: Account service failed: Error while extracting response for type [com.corebanking.common.dto.TransferResponse] and content type [application/json]`(0.85초), 그 사이 transfer 'Transfer COMPLETED' 3건(재시도 3회가 모두 실행됨).
- api 경유 이체 2rps 240건: 502 240건, p50 0.61초, p95 0.69초, 최대 0.70초. 2분 동안 transfer 가 실제로 실행한 이체 65건(서킷 반열림 시도마다), account 'Account validation passed' 69줄, api 'Error while extracting' 136줄.
- 같은 api 에 매니페스트 account jar: 2rps 60건 모두 200(p50 0.04초) — 502 는 릴리스 때문임을 확인.

## 8. 감별

- must_support: 정답지 `must_support` 5개(롤아웃 이벤트와 태그, api 해석 실패 로그, api 502 와 account, transfer 의 200 대비, account 검증과 transfer 완료 지속, 동반 부하 이체 5xx 와 잔액 조회 정상).
- must_rule_out: F39-R(account 하류 주소 오설정), transfer 장애(F17-R, F17-H, F42-R, F40-H), account 자체 장애나 재시작(F41-R, F41-H, F51-R, F51-H), Oracle 이나 DB 계정 장애(F35-R, F54-R, F01-P), api 자체 결함이나 설정, 이름 해석(F37-R, F47-R, F63-R, F64-P).
- contrast_with: F39-R, F49-R, F49-P, F41-H, F40-H.
- **같은 증상, 다른 정답 원인(관계 H)을 가르는 관측 근거**: F39-R 과 F39-H 는 모두 "api 를 거친 이체만 502, 잔액 조회, 거래 내역, commerce 정산 정상, 정답 서비스 account" 다. 가르는 것은 (1) 이체가 transfer 에 닿는가: F39-R 은 account 경로의 transfer 'Transfer COMPLETED ... from=ACC-' 가 끊기고 F39-H 는 이어진다 (2) 로그 문장: F39-R account 'Transfer service call failed ... "http://testbed-ledger:8082/api/transfers": Connect timed out' 와 api 'Account service call failed ... 502', F39-H account 오류 없음과 api 'Error while extracting response for type [com.corebanking.common.dto.TransferResponse]' (3) 스팬: F39-R account 서버 스팬 502, F39-H account 서버 스팬 200 (4) 계기: F39-R 새 ReplicaSet 의 env 한 줄(TRANSFER_SERVICE_URL), F39-H 이미지 태그 1.6.0(env 그대로) (5) 지연: F39-R 은 연결 시간 초과로 수 초, F39-H 는 약 0.6초.
- 같은 account 릴리스 수단의 F41-H 와는 재시작 유무(KCM Unhealthy 404, Killing), 잔액 조회 실패 유무로 갈린다. 같은 원본의 F49-R, F49-P 와는 도메인과 호출자가 다르다.

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

- 주입: k8s.image release 모드, 설계 강도 1단 고정 evaluation(`approved-fixed-f39-h`, min_hold 15m, settle 60s, timeout 20m, max_injection_duration 25m). 강도 사다리 없음, 첫 실행이 곧 녹화 실행. 강도라 할 값이 없다: 새 파드가 옛 파드를 대체한 뒤 api 를 거치는 이체는 요청량과 무관하게 전부 실패한다.
- success(3틱): 동반 부하 이체(step transfer) 5xx 비율(`loadgen.food_create_status_rate`, 선택자 business.5xx.rate) ≥ 0.5.
- must_rule_out(2틱): achieved_rps < 1.25(부하 끊김), 동반 부하 잔액 조회 실패율 ≥ 0.2(account 자체나 Oracle 장애), transfer NotReady(하류 장애), Oracle NotReady.
- abort: entry_status == 0(2틱). api 와 nginx 는 살아서 502 로 답하고 account 는 200 으로 답하므로 이 시나리오로는 0 이 나오지 않는다.
- recovery(2틱, 10m): 기준선 이체 5xx 비율 < 0.05 가 판정력을 가진 조건(F39-R 과 같은 근거와 표본 계산: 가장 한가한 시간 30초 창 이체 약 3건). target_health 200, transfer Ready 는 compile-plan 이 요구하는 형식 조건.
- 부하: load.north_south `transfer-heavy-surge.js` 5rps(이체 40, 잔액 35, 거래 내역 15, 목록 10), entry 30082, ramp 2m + hold 21m + ramp_down 15s(F39-R, F41-H 와 같은 값). 이체 초당 2.0건이 시간대와 상관없이 러너 표본(30초 창 약 60건)과 api 표본 스팬을 채운다. banking surge 실측 건강 상한 20rps 아래, 180rps 상한과 무관.
- 요청 하나의 시간: api 재시도 200ms, 400ms 와 account 왕복(수십 ms)으로 약 0.6초(로컬 p50 0.61초). api 스레드 동시 사용은 초당 약 2.4건 × 0.6초 ≈ 1.5개라 Tomcat 기본 200 에 한참 못 미친다. account, transfer, Oracle 쪽 부하는 평시보다 적다(서킷이 열린 동안 account 호출이 반열림 시도뿐).
- 업무 거절: 잔액 부족은 account 가 넘기기 전에 400 으로 거절한다. 서킷이 닫혀 있으면 400 은 그대로 전달되지만 서킷이 열린 동안은 502 가 된다. 실패 비율은 0.9 를 넘을 것으로 계산한다(성공 문턱 0.5).
- 데이터: 고장 동안 실행된 이체는 정상 이체와 같은 잔액 이동이고 api 접수 수보다 적다(로컬 2분 240 접수 중 65 실행). 되돌리지 않는다(cleanup 대상 아님). 기준선 부하가 쓰는 ACC 계좌 잔액은 평시와 같은 범위에서 오르내린다.
- 롤아웃 시간: tb-w2 에서 새 account JVM 기동과 readiness 통과까지 약 1분(옛 파드는 그동안 서비스). 그 뒤 즉시 전량 실패.
- 배포 전제: 109 docker 에 `core-banking-account:1.6.0` 이 있어야 preflight 가 통과한다(`bash scripts/scenarios/fault-images/build.sh f39-h`, 배포 스크립트가 없으면 빌드). tb-w2 이미지 파일시스템 75%(2026-10-11, 상한 80%), 109 docker 의 account 태그는 latest, 1.3.0, 1.4.0, 1.5.0 이고 1.6.0 은 없다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(GitHub 2021-10-08, 공식 월간 가용성 보고 링크)와 요소별 대응표, 기전 동일(§2)
- [x] 분류 장부 갱신(§4-1 행, §6 기록), 묶음 L+J(L 7→8, 8.7%, J 로 세도 15, 16.3%), 정답 위치 은행 계좌 서비스(5→6, 6.5%), 결제 경로 12/92(13.0%), 서비스 은행 30→31, 고른 이유(§3). 어느 축도 20% 미만
- [x] 근본 원인 위치 `file:line` 확인(패치 줄, 매니페스트 코드, 공용 DTO, api 클라이언트)과 인프라 지점(21-account-service.yaml, 02-configmaps.yaml)(§4)
- [x] 근본 원인 흔적 119 실조회: kcm_events_local 의 롤아웃, 이미지 태그를 담은 kubelet 이벤트 수집, kcm_resources_history 의 account ReplicaSet, 파드 이미지와 같은 날 transfer 2.1.0 ReplicaSet 이 잡힌 선례(§7)
- [x] 핵심 증거가 표본에만 있지 않음: KCM 이벤트, 자원 이력, 전수 로그(api 해석 실패 문장, account 검증, transfer 완료). 스팬은 보조
- [x] 계기 흔적: 롤아웃 이벤트와 새 이미지 태그. 인공 지연 없음(응답은 빠르고 결함은 응답 구조)
- [x] 정답이 관제 데이터로 낼 수 있는 결론(롤아웃 직후 호출자 해석 실패, 피호출자와 그 하류는 정상), 코드 설계 결함 추론 불필요. 재시도가 이체를 다시 실행하는 것은 피해 설명(§5)
- [x] 정답지 세 칸이 원칙 6 대로(근본 account 릴리스, 계기 같음, 부분 점수 api)
- [x] 감지, 피해 판정, RCA 증거 구분(§6)
- [x] 주입 도구가 시나리오 id 를 남기지 않음: 실행기 인자는 네임스페이스, Deployment, 컨테이너, 이미지 둘뿐이고, 패치가 더한 문자열에 시나리오 id, fault, bug, chaos 같은 말이 없음(테스트 `test_k8s_image_release_mode_rolls_banking_account_transfer_receipt_release` 가 확인)
- [x] 부하 상한과 서킷브레이커를 고려한 피해 계산(§9: 서킷이 열려도 fallback 502, 결정적 전량 실패, 로컬 240/240)

## 11. 후보 목록과 버린 후보 (이번 반복, 3단계)

실제 기전 × 부품으로 먼저 적은 뒤(기존 시나리오는 피할 것 확인에만 씀) 숫자 순(0 인 묶음, 0 인 정답 위치, 적은 서비스)으로 줄 세웠다. 부품은 account, transfer, dispatch, payment, Oracle, MySQL, Kafka, 워커 노드 네트워크.

1. **(채택)** GitHub 2021-10-08 × banking account 이체 접수 응답 구조 변경 릴리스 → api 해석 실패(L+J, 은행 계좌 서비스 5, 은행 30).
2. (다음 순위, 막히지 않음) 같은 원본 × food dispatch POST 배차 응답 구조 변경 → order 해석 실패, 재시도마다 배차가 남아 용량이 빨리 참(L+J, 배달 서비스 5, 음식배달 30). 채택 후보와 숫자가 같고 dispatch 정답, order 503 증상 시나리오가 이미 여섯이라 뒤로 둠.
3. (다음 순위, 막히지 않음) 같은 원본 × banking transfer 이체 응답 구조 변경 → account 와 commerce 정산이 함께 해석 실패(L+J, 은행 이체 서비스 7). 정답 위치 수가 더 많아 뒤로 둠.
4. 방화벽, NAT 의 유휴 연결 시간 초과가 풀의 유휴 DB 연결을 조용히 끊음(K, 노드) — 원칙 1: 공식 사후 보고를 웹 검색 1회로 못 찾음(벤더 KB, 이슈 추적기뿐).
5. 운영자가 food orders 에 감사 트리거를 더해 트리거 오류로 주문 INSERT 실패(D/H, DB 테이블(주문)) — 원칙 1: 트리거가 원인인 공식 사후 보고를 웹 검색 1회로 못 찾음.
6. 질의 강제 종료 도구(pt-kill) 오설정이 앱 질의를 죽임(G, DB 인스턴스) — 원칙 1: 웹 검색 1회에 버그 보고와 Wikimedia 작업 티켓뿐.
7. 최소 권한 정리의 DB 권한 회수(O/G, DB 계정) — 원칙 1: 웹 검색 1회 더 해도 공식 사후 보고 없음(rejected 75행과 같음).
8. Redocly 2026-01-14(정리 큐 작업이 오류 처리 누락으로 백오프 없이 20초마다 재전달, 큰 표를 두드려 API OOM, 동적 DB 자격 증명 역할 한도) — 원칙 1: 세 계기의 복합(OOM, 역할 한도, 오케스트레이터)이고 Payplug 반려(208행)와 같은 벽.
9. Inngest 2025-10-16(Kafka 디스크 가득으로 생산자 쪽 스팬 적체, 파일 기술자 고갈) × banking/food Kafka(메시지 브로커 0) — 원칙 7: 세 도메인 Kafka 발행이 outbox 릴레이 비동기뿐이라 사용자 경로 증상 없음.
10. food payment 정산 표시를 운영자가 되돌려 정산 배치가 결제 수십만 건을 한 트랜잭션에 올림(P, DB 테이블(결제)) — 원칙 1(공식 사례 없음), 컨트롤러 필수 중단 조건(payment GC 지연이 order 풀을 묶음).
11. Heroku #2105 × banking transfer 내부 인증 헤더 요구 릴리스 — 원칙 7(F34-R 교훈): banking 쪽은 401 전달 단일 신호이고 commerce 502 는 F40-H 와 같은 꼴.
12. RevenueCat 2022-11-23 × banking OUTBOX_EVENTS identity 되감기(Q, DB 테이블(이체 outbox) 0) — 카탈로그 §1: F57-R 과 엔진, 진입 경로(이체 트랜잭션), 수단이 같고 표만 다름(ORA-00001 같은 증상).

## 12. 첫 실행(곧 녹화 실행)에서 확인할 것

- 새 account 파드가 Ready 가 되어 옛 파드를 대체하는지, 패치에서 고장 시작까지의 시간.
- api 로그가 실제로 `Account service call failed for order null: Error while extracting response for type [com.corebanking.common.dto.TransferResponse] and content type [application/json]` 꼴로 남는지. 다르면 정답지의 결정 증거 문구를 녹화본에 맞게 고친다.
- transfer 의 ACC 계좌 'Transfer COMPLETED' 가 고장 중 얼마나 이어지는지(반열림 시도 주기), account 'Account validation passed' 수.
- KCM 이 새 ReplicaSet 을 image core-banking-account:1.6.0 으로 잡는지, Pulled 본문에 태그가 담기는지.
- 판정이 promote 해 인시던트가 생기는지(`incidents.promoted_at`), 묶음의 상위 서비스에 core-banking-api 가 들어가는지, ledger 잡음 인시던트와 섞이는지.
- 잔액 조회와 거래 내역이 고장 내내 정상인지(배제 조건이 잘못 발화하지 않는지), cleanup 뒤 기준선 이체 5xx 비율이 0 으로 돌아오는지.
