---
title: F64-P 설계 시트 (banking api 설정 배포가 거래 내역 서킷브레이커의 느린 호출 임계를 거래 내역 정상 지연보다 낮게 넣어 멀쩡한 거래 내역 조회를 끊음)
status: Draft
owner: project
last_reviewed: 2026-10-11
tags:
  - scenario
  - design
  - config-deploy
  - circuit-breaker
summary: banking api-service 설정 롤아웃이 거래 내역 서킷브레이커(transferClient)에 느린 호출 차단(slow-call-duration-threshold 100ms, slow-call-rate-threshold 50%)을 켜는데 임계가 거래 내역 호출의 정상 처리 시간(api 에서 본 transfer 호출 p5 약 240ms)보다 낮아, transfer 가 멀쩡히 200 으로 답하는데도 서킷이 계속 열려 거래 내역 대부분이 api 에서 502 가 되는 시나리오. 이체는 다른 서킷이라 정상이다. 원본은 F64-R 과 같은 Spreedly 2022-03-28 서킷브레이커의 잘못된 설정이 의도보다 낮은 임계에서 작동해 멀쩡한 하류로 가는 요청을 503 으로 돌려보낸 장애.
---

# F64-P 설계 시트

## 1. 요약

banking 의 사용자 요청은 nginx 를 지나 api-service 로 들어간다. api 는 두 종류의 요청을 서로 다른 Resilience4j 서킷브레이커로 넘긴다: 이체(`POST /api/transfers` → account-service, 서킷 `accountClient`)와 거래 내역(`GET /api/transfers` → transfer-service, 서킷 `transferClient`). 두 서킷의 평시 설정은 실패율만 본다: 최근 10건 중 최소 5건이 모였을 때 **예외로 끝난 호출**이 50% 이상이면 5초 열린다(`application.yml:19-38`). 느린 호출 기준은 라이브러리 기본값(60초, 100%)이라 사실상 꺼져 있다.

설정 롤아웃 하나가 api 컨테이너에 `SPRING_APPLICATION_JSON` 한 줄을 더해 거래 내역 서킷에 느린 호출 차단을 켠다: `slow-call-duration-threshold: 100ms`, `slow-call-rate-threshold: 50`. 의도는 거래 내역 조회가 비정상으로 늘어질 때 끊는 것이었지만 들어간 임계(100ms)가 거래 내역의 정상 처리 시간보다 낮다. transfer 의 페이지 거래 내역 조회(조건이 모두 선택인 질의와 COUNT)는 평시 약 240~250ms 걸린다(119 트레이스, §7). 그래서 새 api 파드에서는 거래 내역 호출이 **전부 느린 호출**로 기록되고 5건이 모이자마자 서킷이 열린다. 열린 동안 거래 내역은 transfer 에 닿지도 못하고 api fallback 이 502 를 낸다. 5초 뒤 half-open 시험 호출 3건이 transfer 로 가서 200 으로 끝나지만 역시 100ms 를 넘으므로 서킷이 다시 열린다. 이 주기가 계속되어 거래 내역의 약 85% 가 502 다. 이체, 잔액 조회, transfer, Oracle, commerce 정산은 아무 잘못이 없다.

비유: 은행 창구 안내원이 "통장 정리 창구가 0.1초 안에 응답하지 않으면 막힌 것으로 보고 5초 동안 손님을 돌려보내라"는 새 지침을 받았다. 통장 정리는 원래 한 건에 0.25초 걸리는 일이라 안내원은 멀쩡한 창구를 계속 "막힘"으로 판정하고 통장 정리 손님 대부분을 돌려보낸다. 송금 창구는 다른 지침을 따르므로 평소처럼 손님을 받는다.

## 2. 원본 사례

- 기업: Spreedly(결제 오케스트레이션, Core Transactional API 를 통해 여러 결제대행사로 거래를 보냄)
- 날짜: 2022-03-28 18:45~19:22 UTC(상태 페이지 첫 공지 14:45~15:22 EDT, 약 37분). 사후 보고 게시 2022-04-08
- 링크: [공식 상태 페이지 사고와 사후 보고 "Degradation of service with Adyen gateway"](https://status.spreedly.com/incidents/yplj1mlfrvsl)
- 요약(출처가 말한 것만, 자료 문서 M23 행과 같음): 사후 보고 제목은 "Circuit Breaker Triggered at Lower than Intended Threshold for Adyen Gateway". Adyen 게이트웨이용 서킷브레이커가 "erroneously activated" 했고 일부 고객이 요청에 HTTP 503 을 받았다. 서킷브레이커는 "to manage traffic flow when a downstream gateway is producing too many failures, or consuming too many resources" 하려고 둔 것이다. 조사에서 "an errant configuration in the Circuit Breaker" 를 찾아 설정을 고쳐 정상으로 되돌렸다. 어떤 설정값이 얼마였는지, 어떻게 탐지했는지는 보고에 없다.
- 같은 원본의 앞선 재구성: F64-R(commerce api-gateway 의 order 라우트 서킷, checkout 502). 이 시트는 같은 기전을 은행 도메인 api 의 거래 내역 서킷에 옮긴 P 변형이다. 원본은 결제대행사 하나로 가는 경로만 끊고 다른 경로는 살렸는데, 이 재구성도 한 경로(거래 내역)만 끊고 같은 api 의 다른 경로(이체)는 살린다.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 서킷브레이커의 잘못된 설정(errant configuration) | banking api-service 설정 롤아웃: 컨테이너 env 에 `SPRING_APPLICATION_JSON` 으로 거래 내역 서킷(transferClient)의 느린 호출 임계 100ms, 느린 호출 비율 50% 를 더함 |
| 원인이 된 결함 | 서킷브레이커가 의도보다 낮은 임계에서 작동(Triggered at Lower than Intended Threshold) | 임계 100ms 가 거래 내역 정상 처리 시간(api 에서 본 transfer 호출 p5 239.6ms)보다 낮아 멀쩡한 호출이 모두 느린 호출로 세어져 서킷이 열림 |
| 전파 경로 | 서킷이 하류 결제대행사(Adyen)로 가는 요청을 막음. 하류는 실패를 내지 않았는데 오작동 | 서킷이 열린 동안 api 가 transfer 를 부르지 않고 fallback 이 502. half-open 시험 호출 3건은 transfer 에 닿아 200 이지만 다시 100ms 를 넘어 서킷이 또 열림. transfer, Oracle, account 는 정상 |
| 사용자 증상 | 그 결제대행사로 가는 고객 요청 일부가 HTTP 503, 다른 결제대행사는 정상 | 거래 내역(`GET /api/transfers`)의 약 85% 가 502 'Transfer service unavailable: CircuitBreaker 'transferClient' is OPEN ...'. 이체, 잔액 조회, 계좌 목록, commerce 정산은 정상(이 api 는 fallback 을 BAD_GATEWAY 로 만든다) |
| 탐지 경로 | 보고에 없음. 조사에서 서킷브레이커 설정 오류를 찾음 | api 5xx 와 ERROR 로그 'Transfer service circuit open/exhausted: CircuitBreaker 'transferClient' is OPEN' 급증. 그 앞에 하류 실패 로그('Transfer service list call failed')가 없고 transfer 는 200 으로 정상 처리. 롤아웃 직후 시작되고 새 ReplicaSet 스펙에 그 설정이 있음 |
| 완화와 복구 | 설정을 고쳐 정상 운영으로 되돌림 | cleanup 이 env 를 원래 배열로 되돌리고 api 롤아웃(새 파드는 기본 설정, 서킷 상태도 새로 시작)이 끝나면 거래 내역이 회복 |

기전 대조: 원본의 고리는 "서킷브레이커 설정 오류 → 의도보다 낮은 임계에서 작동 → 멀쩡한 하류로 가는 요청을 차단 → 그 경로 요청만 5xx → 설정을 고쳐 복구"다. 재구성도 같은 고리다. 원본이 어떤 설정 항목인지 밝히지 않아, 이 api 에서 평시에 실제로 작동할 수 있는 항목을 골랐다: api 는 하류의 업무 4xx 를 `ClientErrorException` 으로 무시하고(`application.yml:37-38`) transfer 가 살아 있으면 거래 내역은 200 이라 평시 실패율은 0 이다. 실패율 임계를 낮춰도 서킷이 열리지 않으므로, 멀쩡한 하류에서 서킷이 열리려면 느린 호출 기준이어야 한다. 503 이 502 로 바뀐 것은 이 api fallback 의 상태 코드(`TransferClient.java:52-60`)다.

## 3. 숫자 근거 (2026-10-11, `scenario-stats.py`, 정식 + 후보 합계 86, 이 후보 전)

- 묶음(정식 + 후보): J 14(16.3%), G 10(11.6%), P 10(11.6%), D 9, A 7, B 7, L 7, C 6, E 4, F 3, H 2, K 2, Q 2, I 1, M 1, O 1, N 0. 20%(86 기준 17.2)에 닿은 묶음은 없다. 이 후보는 **G**(10 → 11, 87 중 12.6%). 현실에서 두 번째로 흔한 트리거(설정 배포 31%)다.
- 정답 위치(정식 + 후보): 주문 서비스 8(9.3%)이 가장 많고, 이 후보의 **은행 API** 는 3(F47-R, F63-R, 정식 1) → 4(4.6%). 결제 경로 합계 12/86(14.0%)는 이 후보와 무관하다(정답이 결제 쪽이 아니다).
- 서비스(정식 + 후보, 문제가 시작된 서비스): commerce 30, core-banking 29, food-delivery 27. 이 후보로 은행 29 → 30.
- 왜 이 후보인가(3단계 후보 목록 전체는 §11):
  - 가장 적은 음식배달에서 같은 기전을 먼저 봤지만 막혔다. food order → dispatch 서킷은 fallback(`DispatchClient.java:47-50`)과 `OrderService` 가 로그를 남기지 않아 503 의 증거가 표본 트레이스뿐이다(원칙 3). order → payment 서킷은 배차를 마친 뒤의 단계라 실패한 주문마다 배달원 배정이 남아 배차 용량(2000, 2026-10-11 배정 1,677)이 차고 F32-R 증상(용량 소진 503)과 섞인다(원칙 9, 7). order → restaurant 판은 rejected 21 과 같은 주입이다.
  - 은행은 쇼핑몰과 같은 수가 되지만, 정답 위치 은행 API(4.6%)와 묶음 G(12.6%)가 모두 상한과 멀다.
  - 겉 증상(거래 내역이 api 에서 502)이 F54-R(Oracle 앱 계정 호출당 읽기 한도, transfer 500), F59-P(원장 백필이 transfer 풀을 묶어 시간 초과), F63-R(api 릴리스 스레드 누수로 OOM 재시작)과 같아 "증상 경로의 하류(transfer)나 DB 가 범인" 또는 "api 이미지가 범인"이라는 지름길로는 틀린다. 하류의 건강과 api ReplicaSet 스펙의 설정 한 줄, 'list call failed' 없이 시작된 서킷 열림 로그를 함께 봐야 맞히는 H 성격의 데이터다.
  - F64-R 과는 계층(commerce BFF 게이트웨이 대 은행 도메인 api)과 피해 모양(checkout 쓰기 경로 전체 대 같은 서비스 안 한 조회 경로만, 이체는 삶)이 다르다. 서비스 이름만 바꾼 복제가 아니다: 끊기는 서킷의 하류(transfer 조회)가 F54-R, F59-P 의 범인 후보와 겹치는 점이 이 변형의 감별 가치다.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 계기: Deployment `testbed-api`(rca-testbed-banking, replicas 1, RollingUpdate maxSurge 25%(올림 1) / maxUnavailable 25%(내림 0), `core-banking/k8s/20-api-service.yaml`) 컨테이너 `api-service` 의 env 배열에 `SPRING_APPLICATION_JSON={"resilience4j":{"circuitbreaker":{"instances":{"transferClient":{"slow-call-duration-threshold":"100ms","slow-call-rate-threshold":50}}}}}` 한 줄이 더해진 롤아웃. 평시 env 는 OTel 관련 9줄뿐이고(매니페스트 `20-api-service.yaml:38-56`, 운영 값은 OTLP 주소가 192.168.200.109:14318 로 다름) 공용 ConfigMap `service-config` 에도 이 키가 없다(109 kubectl 2026-10-11).
2. 설정 적용: Spring Boot 3.4.5 는 `SPRING_APPLICATION_JSON` 을 `application.yml` 보다 앞선 속성 원천으로 읽어 `resilience4j.circuitbreaker.instances.transferClient.slow-call-duration-threshold=100ms`, `...slow-call-rate-threshold=50` 을 더한다. 나머지 transferClient 설정(창 10, 최소 5, 실패율 50, 열림 5s, half-open 3, 자동 전이)은 `core-banking/api-service/src/main/resources/application.yml:30-38` 그대로다. accountClient 서킷은 바뀌지 않는다.
3. 기록: `TransferClient.listTransfers`(`TransferClient.java:32-50`, `@CircuitBreaker(name="transferClient", fallbackMethod="listTransfersFallback")` + `@Retry(name="transferClient")`)가 transfer `GET /api/transfers` 를 부른다. 호출은 200 으로 끝나도 100ms 를 넘기면 느린 호출로 기록된다. transfer 는 조건이 모두 선택인 JPQL 페이지 조회와 COUNT(`TransferRepository.java:16-28`)로 평시 약 240~250ms 에 답한다(§7).
4. 열림: 최소 호출 5건이 모이면 느린 호출 비율 100% ≥ 50% 라 서킷이 OPEN. 열린 동안 호출은 `CallNotPermittedException` 으로 막히고 fallback `listTransfersFallback`(`TransferClient.java:52-60`)이 ERROR 'Transfer service circuit open/exhausted: CircuitBreaker 'transferClient' is OPEN and does not permit further calls' 를 남기고 `ServiceException(BAD_GATEWAY)` 를 던진다. `@Retry(transferClient)`(최대 3회, 200ms, 지수 2배, `application.yml:48-54`)가 두 번 더 시도하지만 역시 막혀 같은 ERROR 가 요청 하나에 세 줄 남고 `GlobalExceptionHandler`(`GlobalExceptionHandler.java:12-20`)가 약 0.6초 만에 502 로 답한다.
5. 되풀이: 5초 뒤 자동으로 HALF_OPEN, 시험 호출 3건이 transfer 로 가서 200 으로 끝나지만 모두 100ms 를 넘어 느린 호출 비율 100% 로 다시 OPEN. 시험 호출 자리가 찬 동안 들어온 호출은 'is HALF_OPEN and does not permit further calls' 로 막힌다.
6. 하류는 정상: transfer 는 들어온 거래 내역 조회를 평소처럼 처리하고, 받는 수만 서킷이 허용한 만큼으로 줄어든다. 이체 처리와 commerce 정산(commerce-payment → transfer 직접)도 그대로다.
7. 다른 경로: 이체(`ApiService.submitTransfer` → `AccountClient.requestTransfer`, 서킷 accountClient, `ApiService.java:28-38`)는 설정이 바뀌지 않았다. 잔액 조회와 계좌 목록은 nginx 가 account 로 바로 보내 api 를 지나지 않는다.

로컬 실측(2026-10-11, 104): 저장소 `core-banking` 을 임시 폴더에 복사해 api-service jar 를 빌드하고(Boot 3.4.5, Resilience4j 2.2.0. 109 이미지 `core-banking-api:latest` 2026-08-07 빌드, api-service 소스는 2026-07-16 이후 변경 없음), 250ms 뒤 200 을 주는 가짜 transfer 와 10ms 뒤 200 을 주는 가짜 account 를 붙였다. 같은 `SPRING_APPLICATION_JSON` 으로 띄우고 거래 내역 40건을 0.1초 간격으로 순차 호출한 결과 `200×5, 502×6, 200×3, 502×6, 200×3, ...`: 5건 뒤 열리고 half-open 3건이 통과한 뒤 다시 열렸다. 로그에 'CircuitBreaker 'transferClient' is OPEN and does not permit further calls' ERROR 가 80줄(막힌 요청 하나에 세 줄) 남았다. 같은 파드의 이체 POST 10건은 모두 200. env 없이 띄우면 거래 내역 20건 모두 200, ERROR 0.

## 5. 원인 규정 (원칙 5, 6)

| 칸 | 값 | 근거 |
|---|---|---|
| 근본(`root_cause.target_id`) | `core-banking-api`(target_kind config) | 고쳐야 재발이 막히는 곳은 api 의 서킷브레이커 설정이다. transfer 는 정당한 요청을 평소 시간 안에 처리했고, 서킷을 연 판단은 api 설정이 내렸다. 원본도 "errant configuration in the Circuit Breaker" 를 원인으로 지목했다 |
| 계기(`trigger_target_id`) | 비움 | 계기(api 설정 롤아웃)가 근본과 같은 곳에서 일어났다 |
| 부분 점수(`scoring.partial`) | `core-banking-transfer` | 증상 경로의 하류. api 로그가 'CircuitBreaker 'transferClient' is OPEN' 이라 transfer 를 지목하는 답이 가장 흔할 것이다. 원인 근처(그 서킷이 지키는 하류)지만 정답은 아니다 |

원칙 5: 정답은 관제 데이터로 낼 수 있다. ① 고장 시작 직전 api 롤아웃(KCM 이벤트)과 새 ReplicaSet 스펙의 env 한 줄(`kcm_resources_history`), ② api ERROR 가 'circuit open/exhausted: CircuitBreaker ... is OPEN' 뿐이고 그 앞에 'Transfer service list call failed'(I/O error, Read timed out, 5xx)가 없음, ③ 같은 시각 transfer 가 Ready 이고 오류가 없으며 이체가 계속 성공. 셋을 이으면 "하류는 멀쩡한데 api 가 스스로 끊는다, 바뀐 것은 api 설정"이 된다. 코드 설계 결함을 추론할 필요가 없다. 임계 100ms 와 거래 내역 처리 시간 240~250ms 의 비교도 스펙과 지연 지표만으로 된다.

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 | 이 시나리오에서 |
|---|---|---|
| 감지 | api 골든 시그널과 로그 | core-banking-api 서버 스팬 GET /api/transfers 가 502 로 끝나 api 오류율(VM `apm.agent.otel.java.error_rate{service_name="core-banking-api"}`, CH 트레이스 5xx)이 오른다. api ERROR 로그가 평시 하루 약 700줄(잔액 부족 400 업무 거절, 'Account service ...')에서 실패한 거래 내역 하나에 세 줄씩, 동반 부하 기준 초당 약 8줄로 는다. 인시던트가 생겨야 하는 근거: 고장 약 14분 연속, 거래 내역 초당 약 3건의 약 85% 가 502 |
| 피해 판정 | 러너 | api 서버 스팬 5xx 비율(표본 60초 창, 평시 0) ≥ 30%, 3틱(계산 약 60%). 이체 5xx 와 잔액 조회 실패율은 0 그대로 |
| RCA | 녹화된 데이터 | api 롤아웃 KCM 이벤트, 새 ReplicaSet 스펙의 `SPRING_APPLICATION_JSON`(resilience4j transferClient slow-call 100ms), 하류 실패 없이 시작되는 'is OPEN' ERROR, transfer 의 Ready 와 오류 0, 이체 성공('Submitting transfer' 뒤 'Account service call failed' 없음) |

## 7. 관측 근거 표 (119 실조회, 2026-10-11, 평시)

| 증거 | 119 표와 칸 | 조회 | 결과(평시) |
|---|---|---|---|
| 근본: 설정이 스펙에 잡히는가 | PG `kcm_resources_history`(kind, namespace, name, yaml, captured_at) | `kind ILIKE 'replicaset' AND namespace='rca-testbed-banking'` | banking ReplicaSet 303건(2026-07-13~10-11), 그중 `SPRING_APPLICATION_JSON` 0건, `resilience4j` 0건. testbed-api ReplicaSet 36건(마지막 2026-10-09 14:08) 모두 `SPRING_APPLICATION_JSON` 없음. yaml 에 컨테이너 env 배열 전체가 담김(예 testbed-api-bdd59894b: JAVA_TOOL_OPTIONS, OTEL_* 9줄, envFrom service-config) |
| 계기: api 롤아웃 | CH `kcm_events_local`(namespace, object_name, reason, timestamp) | `namespace='rca-testbed-banking' AND object_name LIKE 'testbed-api%'` | banking 이벤트 보존 2026-08-24~. testbed-api 이벤트 48건, 모두 2026-10-09 13:52~14:08(ScalingReplicaSet 8, SuccessfulCreate 4, Killing 4, Unhealthy 12 등, 그날 api 롤아웃). 주입의 롤아웃도 같은 꼴(ScalingReplicaSet, 새 ReplicaSet, 파드 Started, 옛 ReplicaSet 축소)로 남는다 |
| 전파: 서킷 열림 로그 | CH `lucida_logs_local`(service_name, severity_text, body) | `service_name='core-banking-api'` 8일, 시간별 `body LIKE 'Transfer service circuit open/exhausted: CircuitBreaker%'` 와 `body LIKE 'Transfer service list call failed%'` | 서킷 차단 로그가 있던 시간은 2026-10-09 06시(2,085줄, list call failed 151 = I/O 129 + 5xx 22), 07시(4,423, I/O 399), 13시(2,397, I/O 133), 14시(2,807, I/O 141), 2026-10-11 03시(4,390, I/O 499)뿐이고 모두 transfer 가 내려가 I/O error 가 함께 있었다. 그 밖의 시간은 0줄. 이 시나리오에서는 서킷 차단 로그가 'list call failed' 없이 나와야 한다 |
| 하류 정상: 이체 경로 | CH `lucida_logs_local` | `service_name='core-banking-api' AND body LIKE 'Submitting transfer%'` 최근 1일 | 24,352줄(이체 요청마다 한 줄, 전수). 고장 중에도 이어지고 그 뒤에 'Account service call failed' 가 없어야 한다 |
| 결함의 크기: 임계 대 정상 처리 시간 | CH `otel_traces_local`(service_name, span_kind, span_name, duration_ns) | api CLIENT 스팬 `GET`(transfer 호출) 24시간과 7일, transfer SERVER 스팬 `GET /api/transfers` 24시간 | api → transfer 24시간 p5 239.6ms, p50 248.9, p90 273.3, p99 289.4ms(5,306건). 7일 16,547건 중 50ms 미만 107건(0.6%), 50~200ms 0건. 일별 p5 217~422ms. transfer 서버 스팬 p1 237.8, p50 248.7ms(5,243건). 표본(10%)이지만 분포의 하한을 보이는 보조 증거이고 핵심 증거로 쓰지 않는다 |
| 다른 경로 | 같은 표 | api SERVER 스팬 이름별 24시간 | POST /api/transfers p50 10.9ms(2,366건, accountClient 서킷), GET /api/transfers p50 249.9ms(5,405건). api CLIENT POST(account 호출) p50 10ms |
| 감지 신호 | VM `apm.agent.otel.java.error_rate{service_name="core-banking-api"}`, CH api 서버 스팬 5xx | 최근 24시간 | 거래 내역 서버 스팬 5,405건 중 5xx 0. 표본 기반이라 추세 신호로만 쓴다 |

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다: 근본은 ReplicaSet 스펙(전수)과 KCM 이벤트(전수), 전파와 하류 정상은 로그(전수)로 보인다. 러너의 성공 판정만 F54-R 과 같은 api 서버 스팬 5xx 비율(표본)을 쓴다: banking 동반 부하의 loadgen 요약에 거래 내역만 따로 세는 선택자가 없기 때문이다(거래 내역은 계좌 목록과 같은 step list).

## 8. 감별

- must_support: ① 고장 시작 직전 testbed-api 롤아웃(KCM)과 새 ReplicaSet 스펙의 `SPRING_APPLICATION_JSON`(transferClient slow-call 100ms, 50%). ② api ERROR 'Transfer service circuit open/exhausted: CircuitBreaker 'transferClient' is OPEN/HALF_OPEN and does not permit further calls' 가 롤아웃 뒤 급증하고, 같은 구간에 'Transfer service list call failed' 가 없다. ③ transfer 는 같은 시각 Ready, 오류 없음, 처리 시간 평소 240~250ms. ④ 이체(POST)와 잔액 조회, commerce 정산은 정상. ⑤ api 서버 스팬 5xx 비율 평시 0 → 약 60%.
- must_rule_out:
  - transfer 자체 장애가 아님: transfer 파드 Ready, 재시작과 오류가 없다. transfer 가 죽거나 비었다면(F17-R, F17-H, F65-R, 2026-10-09, 10-11 구간) api 에 'list call failed: I/O error' 가 먼저 쌓여 실패율로 서킷이 열리고 이체와 commerce 정산도 실패한다.
  - DB 계정 한도나 Oracle 장애가 아님: 그 경우 transfer 가 거래 내역을 처리하다 실패해 transfer 500 과 api 'list call failed: 500' 이 생긴다(F54-R ORA-02395). 여기서는 transfer 에 닿은 호출이 모두 200 이다.
  - transfer 풀 고갈이나 과부하가 아님: 그 경우 거래 내역이 느려지다 시간 초과로 끊긴다(F59-P). 여기서는 transfer 지연이 평소와 같고 받는 호출은 오히려 준다.
  - api 자체 장애가 아님: 롤아웃 한 번 뒤 재시작이 없고 이체 경로와 health 가 정상이다(F63-R 은 api 가 OOM 재시작하며 이체와 거래 내역이 함께 502).
  - 코드나 이미지 배포가 아님: 새 ReplicaSet 의 이미지(core-banking-api:latest), 프로브, 자원이 옛 ReplicaSet 과 같고 env 한 줄만 다르다(F47-R, F63-R 은 이미지 태그가 바뀐 릴리스).
- contrast_with: F64-R(같은 사례군, commerce 게이트웨이 order 라우트), F54-R, F59-P, F63-R, F65-R(정답지 `related_scenarios.contrast_with` 에 각각의 갈림 근거).

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

설계 강도 하나로 고정한 평가 모드(`approved-fixed-f64-p`). 강도라 할 값은 임계 하나(100ms)이고, 거래 내역 호출의 평시 하한(일별 p5 217ms 이상, 7일 동안 50~200ms 0건)보다 두 배 넘게 낮게 골라 모든 정상 호출이 느린 호출이 된다. 임계를 정상 분포 안(예 240ms)에 두면 열림 여부가 지연 변동에 따라 들쭉날쭉해지므로 피한다. 50ms 미만 0.6% 는 빈 결과 같은 빠른 응답이라 열림을 막지 못한다(창 10건 중 느린 호출 50% 이상이면 열림).

- 열림 주기: 창 10, 최소 5, 느린 비율 약 100% ≥ 50% → OPEN 5초 → HALF_OPEN 시험 3건(모두 느림) → OPEN. 한 주기 ≈ 5초 + 시험 3건이 모이고 끝나는 시간(약 1초).
- 동반 부하: core-banking surge.js(잔액 55%, 거래 내역 25%, 계좌 목록 10%, 이체 10%), target_rps 10 → 거래 내역 초당 2.5건 + 기준선 약 0.6건(119 api 서버 스팬 하루 5,405건 × 10 표본 역산) = 약 3.1건, 이체 1 + 0.27 = 약 1.3건. 한 주기 약 6초에 막힘 약 16건 대 통과 3건 → 거래 내역 5xx 약 0.84. api 서버 스팬 5xx 비율 ≈ 3.1 × 0.84 / (3.1 + 1.3) ≈ 60%(성공 문턱 30%). 60초 창의 표본 스팬 약 26건이라 문턱과의 거리가 표본 잡음보다 크다.
- 10rps 를 고른 이유: F54-R 과 같은 부하로, 거래 내역이 표본 창마다 충분히 지나가게 하면서 이체는 accountClient 를 지나 영향이 없다. 막힌 거래 내역은 약 0.6초 만에 502 라 스레드나 연결을 오래 쥐지 않는다(api Tomcat 기본 200).
- 기준선만 있을 때(동반 부하 끝난 뒤): 거래 내역 초당 약 0.6건이라 5건이 모이는 데 약 8초, 열림 5초, 시험 3건 약 5초. 실패 비율이 약 0.5 안팎으로 낮아지지만 고장은 이어진다. 판정은 동반 부하 구간에서 끝난다.
- 서킷브레이커가 피해를 숨기지 않는다: 이 시나리오에서 서킷 자체가 피해를 만든다. 재시도는 막힌 호출을 두 번 더 막힐 뿐이다.
- api health 는 서킷 상태와 무관하다(서킷 health indicator 미등록). 세 프로브가 /actuator/health 이고 새 파드는 정상 기동해 Ready 가 된다. 진입점 entry_status(이체 단계)는 accountClient 를 지나 0 이 되지 않는다.

| 항목 | 값 |
|---|---|
| level | `approved-fixed-f64-p`, min_hold 10m, settle 60s, timeout 16m |
| max_injection_duration | 25m |
| companion | `load.north_south` core-banking surge.js, target_rps 10, entry 30082, ramp 2m + hold 17m + ramp_down 15s, seed 6465 |
| success | api 서버 스팬 5xx 비율(`clickhouse.service_error_rate` core-banking-api) ≥ 30, 3틱 연속 |
| must_rule_out | 동반 부하 achieved_rps < 2.5(부하 끊김), transfer 파드 NotReady(transfer 자체 장애), transfer 서버 스팬 5xx 비율 ≥ 10(하류가 실제로 실패: F54-R), 동반 부하 이체 5xx 비율(`loadgen.food_create_status_rate`, business.5xx) ≥ 0.2(이체까지 실패: F35-R, F42-R, F63-R, F65-R), 잔액 조회 실패율(`loadgen.read_step_status_rate`) ≥ 0.2(account, DB, nginx 고장), 2틱 |
| abort | entry_status(banking 진입점) == 0, 2틱. api 는 살아서 이체에 답하므로 이 시나리오로는 0 이 나오지 않는다 |
| recovery | api 서버 스팬 5xx 비율 < 10 이 판정력을 가진 조건, target_health 200, transfer, api 파드 Ready, 기준선 잔액 조회 실패율 < 0.05 는 형식 조건. 2틱, 10m |
| cleanup | env 를 원래 배열로 패치한 뒤 롤아웃 available 확인(180초), 동반 부하 종료, 10m |

판정 근거(평시 값과 장애 값):

| 조건 | 관측 | 평시 | 장애 중 | 판정력 |
|---|---|---|---|---|
| success | api 서버 스팬 5xx 비율 | 0 | 약 60% | 있음(문턱 30) |
| must_rule_out transfer-erroring | transfer 서버 스팬 5xx 비율 | 0 | 0(transfer 에 닿은 호출은 200) | 교란(F54-R 꼴)일 때만 오른다 |
| must_rule_out transfers-also-failing | 동반 부하 이체 5xx 비율 | 0 | 0(accountClient 무관) | 교란(이체 경로 장애)일 때만 0.2 이상 |
| must_rule_out balance-reads-failing | 잔액 조회 실패율 | 0 | 0(api 를 지나지 않음) | 교란일 때만 0.2 이상 |
| recovery | api 서버 스팬 5xx 비율 | 0 | 약 60%(기준선만이면 약 35~50%) | 있음(문턱 10) |

새 관측 쿼리, 새 실행기, 러너 변경은 없다. 기존 `k8s.env` 실행기의 허용 표(APPROVED_TARGETS, APPROVED_KEYS)에 F64-P(testbed-api, api-service, 키 SPRING_APPLICATION_JSON)만 더했다. 러너 kubernetes 프로브 허용 목록(live_probes APPROVED_K8S_TARGETS)에 testbed-api 와 testbed-transfer 가 이미 있다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Spreedly 2022-03-28, 공식 상태 페이지 사후 보고)와 요소별 대응표가 있고, 기전("서킷브레이커 설정 오류로 의도보다 낮은 임계에서 작동해 멀쩡한 하류로 가는 한 경로의 요청을 막고, 설정을 고쳐 복구")이 같다 (원칙 1, §2)
- [x] 장부를 갱신했다: 묶음 G(10 → 11, 87 중 12.6%), 정답 위치 은행 API(3 → 4, 4.6%), 결제 경로 12/87(13.8%, 무관), 서비스 은행 29 → 30, 어느 축도 20% 에 닿지 않는다 (원칙 2, §3)
- [x] 근본 원인 위치: api Deployment env(`20-api-service.yaml:38-56`), 서킷 설정(`application.yml:19-38, 48-54`), 서킷과 fallback(`TransferClient.java:32-60`) 확인 (G1, §4)
- [x] 근본 원인의 흔적: PG `kcm_resources_history` ReplicaSet env(평시 banking 303건 중 resilience4j, SPRING_APPLICATION_JSON 0건, testbed-api 36건에 env 배열 전체) (원칙 3, §7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: ReplicaSet 스펙과 KCM 이벤트(전수), api 로그(전수). 트레이스 지연 분포는 보조. 러너 성공 판정만 표본 스팬 5xx 비율을 쓴다(F54-R 과 같음) (원칙 3)
- [x] 계기의 흔적: api 롤아웃 KCM 이벤트와 새 ReplicaSet 스펙. 인공 지연을 쓰지 않는다. transfer 의 처리 시간은 평소 그대로다 (원칙 4)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다: 롤아웃 직후 하류 실패 없이 시작된 서킷 열림 로그, 정상인 하류와 이체, 스펙의 env 한 줄 (원칙 5)
- [x] 정답지 세 칸: 근본 `core-banking-api`(config), 계기 비움, 부분 점수 core-banking-transfer (원칙 6, §5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다 (원칙 7, §6)
- [x] 주입 도구가 시나리오 id 를 남기지 않는다: k8s.env 실행기는 네임스페이스, Deployment, 컨테이너, env 배열만 kubectl 로 넘기고, 클러스터에 남는 것은 env 한 줄(값에 id 없음)과 롤아웃뿐이다. 동반 부하 태그는 tb-runner 의 k6 쪽에만 남는다(기존 시나리오와 같음) (원칙 8)
- [x] 피해 계산: 임계가 정상 분포 하한의 절반 아래라 모든 호출이 느린 호출, 열림 주기와 동반 부하로 거래 내역 5xx 약 0.84, api 5xx 약 60%, 진입점과 이체 생존 (원칙 9, §9)

## 11. 3단계 후보 목록 (실제 기전 × 부품, 버린 이유)

| # | 후보 | 부품 | 원본 사례 | 결과 |
|---|---|---|---|---|
| 1 | api 거래 내역 서킷 느린 호출 임계 설정 롤아웃 | banking api | Spreedly 2022-03-28(M23, 공식) | **채택** |
| 2 | order → dispatch 서킷 느린 호출 임계 설정 롤아웃 | food order | Spreedly 2022-03-28 | 버림(원칙 3): `checkCapacityFallback` 과 `OrderService` 가 로그를 남기지 않아 503 증거가 표본 트레이스뿐 |
| 3 | order → payment 서킷 느린 호출 임계 설정 롤아웃 | food order | Spreedly 2022-03-28 | 버림(원칙 9, 7): 배차 뒤 결제 단계라 실패 주문마다 배달원 배정이 남아 용량이 차 F32-R 증상과 섞이고 cleanup 뒤에도 남음 |
| 4 | order → restaurant 서킷 느린 호출 임계 | food order | Spreedly 2022-03-28 | 버림: rejected 21 과 같은 주입(지휘 세션 목록) |
| 5 | DB 활동 감사(verbose) 로그 켜기로 DB I/O 과부하와 재시작 고리 | food MySQL, banking Oracle | Firstup 2025-10-10(공식 상태 페이지) | 버림(원칙 9): 평시 문장 수(초당 수십~수백)로는 로그 쓰기가 DB I/O 를 묶을 계산이 서지 않고, 원본의 재시작 고리는 food 진입 서비스 liveness 로 entry 0(필수 중단 조건) |
| 6 | DB 연결 시간 제한 1s → 300ms 축소 배포 | food 서비스 Hikari | PostHog 2025-09-29(공식) | 버림(원칙 1): 원본이 쓰기 DB 부하 + 설정 + 백오프 없는 재시도 + 프로브 오설정 네 요인의 복합 |
| 7 | 용량 한계 근처 서비스의 일상 배포가 파드를 잠깐 줄임 | K8s 롤아웃 | GitHub 2026-08-06(월간 가용성 보고) | 버림(원칙 1): 모든 Deployment 가 replicas 1, maxUnavailable 0 이라 줄어들 여유 구조가 없음 |
| 8 | 트래픽 정점이 LB 흐름 한도를 넘고 재시도 버그가 증폭 | nginx, 게이트웨이 | GitHub 2026-08-17(월간 가용성 보고) | 버림(원칙 1, 3): 복합 계기, nginx 는 OTel 이 없어 119 에 흔적이 없음 |
| 9 | DB 전역 질의 실행 시간 제한(max_execution_time) 설정 | food MySQL | 웹 검색 1회에 공식 사후 보고 없음(GitLab 이슈는 계획 악화) | 버림(원칙 1) |
| 10 | 노드 MTU 불일치로 큰 응답 유실 | tb-w2 네트워크 | Grebedoc 2026-05-26 상태 페이지 한 줄, UC Berkeley 2018 공지 | 버림(원칙 1, 컨트롤러 필수 중단 조건): 출처가 한 줄 요약뿐이고, 교차 노드 경로가 입구(NodePort)뿐이라 입구가 먼저 끊김 |

## 12. 첫 실행(곧 녹화 실행)에서 확인할 것

- 새 api 파드가 Ready 가 되어 옛 파드를 대체하는 시간과 고장 시작 시각.
- api ERROR 가 실제로 'Transfer service circuit open/exhausted: CircuitBreaker 'transferClient' is OPEN and does not permit further calls' 와 'is HALF_OPEN ...' 꼴이고 'Transfer service list call failed' 가 없는지. 다르면 정답지의 결정 증거 문구를 녹화본에 맞게 고친다.
- transfer 오류 0, 이체 5xx 0, 'Submitting transfer' 뒤 'Account service call failed' 없음.
- KCM 이 새 ReplicaSet 을 `SPRING_APPLICATION_JSON`(resilience4j transferClient slow-call 100ms) env 로 잡는지.
- api 서버 스팬 5xx 비율이 계산(약 60%) 근처인지.
