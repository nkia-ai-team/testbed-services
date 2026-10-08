---
title: F39-R 설계 시트 (banking account-service 설정 롤아웃이 이체 하류 주소를 다른 내부 호스트로 바꿔 연결 실패)
status: Draft
owner: project
last_reviewed: 2026-10-08
tags:
  - scenario
  - design
  - config-deploy
summary: banking account-service 롤아웃이 이체 하류 주소 TRANSFER_SERVICE_URL 을 다른 내부 호스트(testbed-ledger:8082)로 덮어써, account 가 검증한 이체를 넘길 때마다 연결 시간 초과로 실패하고 이체 요청이 502 가 되는 시나리오. 원본은 GitHub 2026-03-05 롤아웃이 로드밸런서에 넣은 잘못된 설정이 내부 트래픽을 잘못된 호스트로 보낸 장애.
---

# F39-R 설계 시트

## 1. 요약

banking 이체는 nginx → api(`POST /api/transfers`) → account(`POST /api/accounts/transfer`, 계좌 검증) → transfer(`POST /api/transfers`, 실행) 순서로 간다. account 가 transfer 를 찾는 주소는 공용 ConfigMap `service-config` 의 `TRANSFER_SERVICE_URL=http://testbed-transfer:8082` 다. 롤아웃 하나가 account 컨테이너에 같은 이름의 명시 env `TRANSFER_SERVICE_URL=http://testbed-ledger:8082` 를 더한다. 명시 env 가 ConfigMap 보다 앞서므로 새 account 파드는 이체를 ledger 의 Service 이름에 transfer 포트를 붙인 곳으로 보낸다. testbed-ledger 는 해석되지만 그 Service 는 8083 만 열어, 8082 로 가는 연결은 어느 파드에도 닿지 않고 응답 없이 버려진다(109 실측, §4). account 의 health 는 자기 DB 만 보므로 새 파드는 Ready 가 되어 패치 약 1분 뒤 옛 파드를 대체한다. 그 뒤 account 는 계좌 검증까지는 정상으로 하고, 넘기는 단계에서 connect-timeout 3초마다 `Connect timed out` 으로 실패하며, 재시도와 서킷브레이커 끝에 502 를 낸다. api 도 재시도와 서킷 끝에 502 로 답한다. 잔액 조회, 계좌 목록, 거래 내역, commerce 정산 이체, transfer NodePort 직행 이체는 정상이다. banking 동반 부하 5rps(이체 비중 40%)를 붙여 가장 한가한 시간에도 account, api 표본 스팬과 러너 판정 표본을 확보한다(§6, §9).

비유: 지점 직원이 고객 확인을 마친 송금 서류를 본점 송금부로 보내는데, 새로 배포된 업무 설정표에 송금부 주소 칸만 같은 건물 장부부 주소로 잘못 적혔다. 장부부에는 그 창구가 없어 서류가 아무 데도 닿지 않는다. 송금부와 장부부는 멀쩡히 다른 일을 하고 있고, 지점 직원도 고객 확인은 잘 한다.

## 2. 원본 사례

- 기업: GitHub(GitHub Actions)
- 날짜: 2026-03-05 16:24~19:30 UTC(보고 머리글은 16:35 시작, 2시간 55분)
- 링크: [공식 월간 가용성 보고 2026년 3월](https://github.blog/news-insights/company-news/github-availability-report-march-2026/)
- 요약(출처가 말한 것만): 복원력을 높이려고 Redis 인프라 갱신을 운영에 롤아웃했는데, 이 갱신이 Redis 로드밸런서에 잘못된 설정 변경을 넣었고 그 설정이 내부 트래픽을 잘못된 호스트로 보냈다. 워크플로 실행의 95% 가 5분 안에 시작하지 못했고(평균 지연 30분) 10% 는 인프라 오류로 실패했다. 로드밸런서 설정을 바로잡아 17:24 UTC 부터 작업이 정상 실행됐고 나머지 시간은 밀린 큐를 비웠다. 재발 방지는 갱신 즉시 롤백과 그 영역 변경 동결, 잘못된 설정이 인프라로 퍼지지 못하게 하는 자동화, 잘못 설정된 로드밸런서 경보, Actions Redis 클라이언트가 짧은 캐시 중단을 견디게 하는 조정. 잘못된 호스트가 무엇이었는지, 어떻게 탐지했는지는 보고에 없다.
- 같은 기전의 두 번째 사례: GitHub 2026-06-10 15:05~16:25 UTC, 내부 API 인프라로의 memcached 프록시 서비스 롤아웃이 인증 서비스가 잘못된 호스트 설정을 집어 들게 해 인증 조회가 간헐적으로 실패했고 API 요청의 약 9% 가 잘못된 401 을 받았다. memcached 서비스가 올바른 호스트를 가리키게 설정을 바꿔 완화했다([공식](https://github.blog/news-insights/company-news/github-availability-report-june-2026/)). 둘 다 `ref-real-world-incidents.md` M1 에 더했다.
- 현실 비중: 설정 배포는 Google 포스트모템 트리거의 31%(M1)인데 이 장부의 G 는 정식 + 후보 4(42 중 10%)다.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | Redis 인프라 갱신의 운영 롤아웃 | testbed-account Deployment 롤아웃(컨테이너 env 한 줄 추가) |
| 원인이 된 결함 | 롤아웃이 로드밸런서에 넣은 잘못된 설정이 내부 트래픽을 잘못된 호스트로 보냄 | 롤아웃된 env 가 account 의 이체 하류 주소를 다른 내부 호스트(testbed-ledger:8082)로 바꿈 |
| 전파 경로 | Redis 에 의존하는 Actions 작업 배정과 실행이 막힘 | account 의 transfer 호출이 연결 시간 초과 → 재시도, 서킷 열림 → account 502 → api 재시도, 서킷 열림 → api 502 |
| 사용자 증상 | 작업 시작 지연(95% 5분 넘게), 10% 인프라 오류 실패 | 게이트웨이를 거치는 이체 502(처음 몇 건은 약 10초 기다린 뒤), 잔액 조회, 거래 내역, commerce 정산은 정상 |
| 원본의 탐지 경로 | 보고에 없음 | account, api 오류율과 ERROR 로그 급증, KCM 롤아웃 이벤트와 새 ReplicaSet 스펙 |
| 완화와 복구 | 로드밸런서 설정을 바로잡고 갱신을 롤백 | env 를 원래 배열로 되돌리는 롤아웃 |

기전은 원본과 같다: "롤아웃된 설정이 내부 트래픽을 잘못된 호스트로 보내 의존하는 기능이 실패하고, 설정을 바로잡아 복구한다". 바꾼 것은 층과 규모다. 원본은 공유 로드밸런서 설정이라 Redis 를 쓰는 모든 클라이언트가 영향을 받았고, 여기서는 호출하는 서비스 하나의 하류 주소 설정이라 그 서비스를 거치는 경로만 영향을 받는다. 06-10 사례가 바로 이 모양(한 서비스가 잘못된 호스트 설정을 집어 듦)이다. 잘못된 호스트로 같은 네임스페이스의 다른 내부 서비스 이름을 쓴 것은 재구성 선택이다(원본은 호스트를 밝히지 않았다). 그 값이 왜 들어갔는지는 원본에도 없고 지어내지 않는다.

## 3. 숫자 근거 (2026-10-08, `scenario-stats.py`, 정식 + 후보 합계 42, 이 후보 전)

| 축 | 값 |
|---|---|
| 묶음 | A 7, B 7, D 7(각 16%), C 6, G 4, E 2, F 2, H 2, L 2, I 1, M 1, O 1, J, K, N 0. 이 후보는 **G 설정 오배포(4→5, 43 중 12%)** |
| 정답 위치 | 외부 결제 의존 6, 주문 서비스 6(각 14%), 결제 경로 합계 10(23%, 금지). 이 후보는 **은행 계좌 서비스(0)**, §2-1 에 이미 있는 말 |
| 서비스 | 쇼핑몰 23, 은행 10, 음식배달 9 → 은행 11 |

이 후보를 고른 이유:

- 합계 0 인 묶음(J, K, N)은 이번에도 막혔다. J 는 노드에 결함 이미지가 없다(app.release live_supported false, 앱 코드 변경은 사람 검토 대상). K 는 CNI 가 flannel 단독이라 쿠버네티스 쪽 네트워크 주입 수단이 없고, Service 설정을 바꾸는 꼴(셀렉터, 포트)은 계기 흔적이 남지 않는다: 119 PG `kcm_resources_history` 는 Service, Deployment, ConfigMap 레코드가 2026-09-17 06:34 뒤로 하나도 쌓이지 않았다(ReplicaSet, 파드는 2026-10-08 까지 쌓임, 2026-10-08 19:00 UTC 조회). N 은 모든 동기 호출의 서킷브레이커 때문에 F14-R 막힘 그대로다. 1 인 묶음 I, M, O 는 은행, 음식배달에 인증 서비스가 없고(I), M 과 O 는 바로 앞 후보(F37-R, F35-R)가 같은 은행 표면을 썼다.
- G 는 현실에서 가장 흔한 계기(31%)인데 정식 + 후보 비율이 10% 라 현실 대비 적다. 상한 20% 에 멀다.
- 정답 위치 은행 계좌 서비스는 0 이고 결제 쪽이 아니다.
- 서비스: 음식배달(9)이 은행(10)보다 1 적지만 고르지 않았다. 음식배달에서 같은 기전(호출하는 쪽 배포의 하류 주소 오설정)을 걸 곳은 order 의 `SERVICES_DISPATCH_URL`, `SERVICES_RESTAURANT_URL` 인데 정답 위치가 주문 서비스(6, 두 번째로 많음)가 되고, dispatch 쪽은 order 의 용량 확인이 503 으로 끝나는 F32-R 의 증상을 되풀이한다. restaurant 는 하류 호출이 없고 payment 쪽은 결제 경로(금지)다. 음식배달의 남은 표면은 MySQL 위주라 최근 후보(F33-R, F36-R, F38-R)와 겹친다. 쇼핑몰 23 과의 차이 규칙(원칙 2)에는 둘 다 걸리지 않는다.
- rejected 기록과 장부 §4-1, §5 에 같은 원본 사례나 같은 주입이 없다. 최근 F34-R 폐기 교훈("4xx 업무 거절 단일 신호")과 달리 이 후보의 피해는 account, api 서버 스팬 502 와 여러 템플릿의 ERROR 로그다.
- 같은 계열과의 거리: F08-P(commerce order read-timeout 오배포)와 F37-R(banking api dnsPolicy)도 "호출하는 쪽 설정 배포가 정상 하류 호출을 끊는다". 이 후보는 바뀐 칸(하류 주소 값), 실패 단계(이름은 해석되고 잘못된 호스트로의 연결이 시간 초과), 범위(한 하류 경로만), 정답 위치(account)가 다르고, 로그가 잘못된 URL 을 그대로 담는다(§8).

## 4. 인과 사슬 (코드와 인프라 위치)

1. 배포 정본: `core-banking/k8s/21-account-service.yaml:31-33` envFrom ConfigMap service-config, `:38~` 명시 env(DB 계정, OTel). 평시 값 `core-banking/k8s/02-configmaps.yaml:15` `TRANSFER_SERVICE_URL: http://testbed-transfer:8082`. 109 실배치(2026-10-08 kubectl): replicas 1, RollingUpdate maxSurge 25% / maxUnavailable 25%(각각 1, 0), readiness `/actuator/health`(DB), liveness `/actuator/health/liveness`, startup `/actuator/health` 5초 주기, 명시 env 11개에 TRANSFER_SERVICE_URL 없음, OTLP 내보내기 `http://192.168.200.109:14318`(IP 라 관측 데이터는 끊기지 않음).
2. 주소를 받는 자리: `account-service/src/main/resources/application.yml:26-30` (services.transfer.url = `${TRANSFER_SERVICE_URL:...}`, connect-timeout 3s, read-timeout 15s) → `account-service/.../config/RestClientConfig.java:14-24` (transferRestClient, SimpleClientHttpRequestFactory).
3. 실패가 로그가 되는 자리: `account-service/.../service/AccountService.java:58-80` (validateAndForward: Oracle 에서 두 계좌 조회와 검증, INFO 'Account validation passed ... forwarding to transfer-service', 그다음 transferClient.executeTransfer) → `client/TransferClient.java:28-48` (executeTransfer, RestClientException 을 ERROR 'Transfer service call failed for order {}: {}' 로 남기고 BAD_GATEWAY) → `:50-58` (executeTransferFallback, ERROR 'Transfer service circuit open/exhausted for order {}: {}' 후 502 'Transfer service unavailable'). 재시도와 서킷: `application.yml:32-52` (transferClient 슬라이딩 창 10, 최소 5회, 실패율 50%, 열림 5초, half-open 3회 / 재시도 3회 200ms 지수 백오프). Resilience4j 기본 순서는 Retry 가 바깥이라 재시도 한 번 한 번이 서킷에 실패로 세어진다.
4. api: `api-service/.../client/AccountClient.java:28-58` (requestTransfer, account 의 502 를 ERROR 'Account service call failed for order {}: {}' 로 남기고 BAD_GATEWAY, fallback 'Account service circuit open/exhausted'), read-timeout 10초(`api-service/src/main/resources/application.yml:8-12`).
5. 경로: `02-configmaps.yaml:38-50` nginx `/api/transfers` → api, `/api/accounts` → account 직행. commerce-payment 는 transfer 를 FQDN 으로 직접 부르고, commerce 기준선 부하는 transfer NodePort 30282 로 직접 이체한다(tb-runner k6 인자 `BANKING_TRANSFER_URL=http://192.168.122.77:30282`, 2026-10-08 확인).
6. 잘못된 호스트: `core-banking/k8s/23-ledger-service.yaml:97-105` Service testbed-ledger 는 8083 만 연다.

로그 문장 모양: RestClient 는 I/O 예외를 `ResourceAccessException("I/O error on POST request for \"<url>\": <원인 메시지>")` 로 감싸고 TransferClient 는 `ex.getMessage()` 만 남기므로, 로그에는 요청 URL 이 그대로 들어간다. 같은 클러스터의 다른 서비스가 연결 시간 초과를 남긴 실제 모양(119 CH, 8일): commerce-product `I/O error on GET request for "http://testbed-inventory:8082/api/inventory/14": Connect timed out`. 그래서 고장 중 account 로그는 `Transfer service call failed for order null: I/O error on POST request for "http://testbed-ledger:8082/api/transfers": Connect timed out` 이 된다(api 경유 이체의 orderId 는 null, 평시 로그 'Account service call failed for order null' 에서 확인).

109 실측(2026-10-08 19:00~19:10 UTC, testbed-account 파드 안, 읽기 전용 요청):

- `getent hosts testbed-ledger` → 10.101.6.9 (testbed-ledger.rca-testbed-banking.svc.cluster.local). 이름은 해석된다.
- `curl --connect-timeout 10 -X POST http://testbed-ledger:8082/api/transfers` → 10.0초 뒤 rc=28(연결 시간 초과, 응답 없음), 2회 같음. `--connect-timeout 3` 으로는 3초 뒤 rc=28. 같은 결과가 testbed-transfer:8081, testbed-transfer:8080 에서도 나온다: Service 에 없는 포트로 가는 ClusterIP 연결은 kube-proxy 가 바꾸지 않아 어느 파드에도 닿지 않는다.
- `curl http://testbed-ledger:8083/actuator/health` → 200(4.5ms), `http://testbed-transfer:8082/actuator/health` → 200. 두 서비스는 정상이다.
- 따라서 Java 클라이언트(connect-timeout 3s)는 시도마다 3초 뒤 `java.net.SocketTimeoutException: Connect timed out` 으로 실패한다.
- 실행기 preflight 와 같은 읽기 전용 비교: 109 의 현재 testbed-account 컨테이너 env 배열(`jq -Sc`)이 profiles.json `scenario_parameters[F39-R].baseline` 과 글자 그대로 같고, `auth can-i patch deployments` 는 yes.

## 5. 원인 규정 (원칙 5, 6)

| 칸 | 값 | 근거 |
|---|---|---|
| 근본(`target_id`) | `core-banking-account` | 결함을 가진 곳은 account 의 배포 설정(컨테이너 env 의 이체 하류 주소)이다. 고쳐야 재발이 막히는 곳도 같다. APM 서비스 이름(OTEL_SERVICE_NAME)으로 적는다. target_kind config |
| 계기(`trigger_target_id`) | 비움 | 계기(롤아웃)가 근본과 같은 곳에서 일어났다 |
| 부분 점수 | core-banking-api, banking-api | 사용자에게 502 를 내는 증상 서비스. api 는 결함이 없고 account 의 502 를 전할 뿐이다 |
| 입도 | service | |

- 원칙 6: api 의 이체 요청과 account 의 계좌 검증은 늘 하던 그대로 정당하다. 그 요청을 엉뚱한 호스트로 보내게 만든 것은 account 자신의 배포 설정이다. transfer 는 원래 받아야 할 호출을 못 받을 뿐 결함이 없고, ledger 는 잘못 적힌 이름일 뿐 연결이 닿지도 않는다. 둘 다 오답이다. 원본 보고가 지목한 층위("롤아웃이 넣은 잘못된 설정이 트래픽을 잘못된 호스트로 보냄")와 같은 층위다.
- 원칙 5: 정답은 KCM 의 롤아웃 이벤트와 새 ReplicaSet 스펙의 env 한 줄 차이, 그 직후 account 로그에 그대로 찍힌 잘못된 URL 이라는 전수 근거 쌍으로 낸다. 코드 설계 결함 추론이 필요 없다. 누가 왜 그 값을 넣었는지는 요구하지 않는다.
- class B(설정, 코드 결함 아님), fault_pattern 없음.

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 | 이 시나리오 |
|---|---|---|
| 감지 | lucida-next 이상 탐지 | core-banking-account, core-banking-api 서버 스팬 502(오류율), 두 서비스의 ERROR 로그 급증. 업무 규칙 없이 겉 증상으로 이벤트가 생긴다 |
| 피해 판정 | 러너 | 동반 부하 이체(step transfer) 5xx 비율, 평시 0 에서 0.5 이상 |
| 원인 설명 | 녹화 데이터 | §7 의 KCM 이벤트와 ReplicaSet 스펙(PG), 로그(전수), 하류의 정상 응답 로그 |

인시던트가 생길 조건(지휘 세션이 넘긴 119 판정 기전 요약과 이번 조회):

- 판정 입력은 CH MV `agg_service_golden_signals`(표본 SERVER, CONSUMER 스팬 status_code='ERROR'), 판정 시각 기준 최근 15분 대 그 전 3시간, 묶음 멤버 상위 3개 서비스. 지휘 세션 요약으로는 promote 가 대개 고장 2~4분째의 첫 판정(`incidents.promoted_at` 기준)에서 나므로 고장 즉시 5xx 서버 스팬이 크게 나야 유리하다. 단 banking 실측은 다르다: 2026-10-02 08:00~08:10 UTC banking account → transfer 실패로 api 502 가 났을 때(CH MV 5분 오류 32, 38건) core-banking-api 묶음 4건은 first_event_at 08:00:46, 08:03:26, promoted_at 08:19:08~08:23:06 으로 promote 지연이 약 18~20분이었고 고장이 끝난 약 10분 뒤에 promote 됐다(PG incidents 51b7de7d, e600191a, 8b506068, 0d0e0174, 2026-10-08 19:42 UTC 재조회). 즉 banking 에서는 회복 뒤에도 promote 될 수 있고, 동반 부하 없는 약 10분 고장으로도 인시던트가 생겼다. 이 시나리오의 고장 약 14분은 그 길이보다 길다.
- 이 시나리오는 그 모양이다: 새 account 파드가 Ready 가 되는 순간부터 account 를 거치는 이체가 전부 502 다(부분 실패 단계가 없다). OTel HTTP 서버 스팬은 5xx 면 ERROR 다. 평시 두 서비스의 업무 거절은 400(잔액 부족)이라 오류율에 들어가지 않는다: VM `apm.agent.otel.java.error_rate` 7일 99백분위 core-banking-account 0, core-banking-api 0, CH MV 최근 하루 오류 0건(2026-10-08 19:04 UTC 조회).
- 서킷이 열린 뒤의 모양(계산): api 의 accountClient 서킷이 열리면 api 는 account 를 부르지 않고 바로 502 를 낸다. 그래서 고장 중 api 는 이체 요청 거의 전부가 오류이고, account 는 api 의 half-open 시도(5초 열림 뒤 3회)만 받아 그 호출이 모두 502 다. account 쪽 잘못된 URL 의 연결 시간 초과 로그는 account 서킷의 half-open 시도마다(약 5초 열림 + 최대 약 9.6초 시도) 되풀이되어 고장 내내 이어진다.
- 표본량(계산값, 동반 부하 스크립트는 2026-07 F21-P 에서 다른 강도로 실행된 적 있고 이 강도로는 실행 전): 평시 가장 한가한 KST 2~6시 표본 요청은 api 분당 2.8~3.3, account 분당 4.8~5.6(CH MV 7일 시간대별 평균, 2026-10-08 19:16 UTC 조회). 동반 부하 5rps 중 api 몫(이체 40% + 거래 내역 15%)은 초당 2.75건, 그중 이체 2.0건이 502 다. 기준선(1 iter/s, 이체 10%) 몫을 더해 api 는 초당 약 3.1건 중 약 2.1건이 오류(약 68%)이고 10% 표본으로 분당 약 13개 ERROR 서버 스팬이다. account 는 잔액, 계좌 목록 요청(초당 약 2.9건)을 정상 처리하면서 이체 호출(api half-open 시도, 초당 약 0.5건 미만)이 모두 502 라 오류율이 약 15% 안팎으로 오른다. 동반 부하는 패치와 같은 시각에 1rps 에서 5rps 로 120초 동안 오르므로(러너 production_runtime 의 램프), 고장(패치 약 1분 뒤 시작) 3분째에 도는 첫 판정의 15분 창은 평시 약 11분 + 경사 중 고장 전 약 1분 + 고장 3분으로 채워진다. KST 2~6시 기준 그 창의 api 오류율은 약 35% 안팎, 표본 ERROR 서버 스팬은 약 35개로 계산한다(평가 1회차 재계산 반영, 첫 실행에서 확인).
- 로그: 고장 중 api ERROR 'Account service call failed', 'Account service circuit open/exhausted' 가 요청마다 1~2줄씩, 평시 분당 0.1~0.8줄(CH MV `log_error_count`, KST 2~6시)에서 분당 100줄 넘게 뛴다. account ERROR 는 평시 0 에서 half-open 시도마다 생긴다.
- 묶음 구성 위험: 같은 banking 네임스페이스에서 ledger 대사 불일치 로그 인시던트가 8일 271건(하루 약 34건, 약 42분마다, 마지막 first_event_at 2026-10-08 19:21, PG `incidents` main_service core-banking-ledger, 2026-10-08 19:42 UTC 조회) 난다. 고장 약 14분과 banking promote 지연 약 18~20분을 합친 30분 남짓 동안 ledger 잡음 묶음이 거의 확실히 하나는 겹치므로 같은 묶음으로 섞일 위험이 크다. 섞이면 판정 입력의 상위 3개 서비스에 ledger 가 들어갈 수 있으나 account, api 의 오류율이 1차 근거다. transfer 는 고장 중 오히려 account 발 요청이 줄어 오류율이 오르지 않는다.
- 참고(설계 근거로 쓰지 않음): 2026-10-01 17:20 UTC 창에 account 'Transfer service call failed ... "http://testbed-transfer:8082/api/transfers": Connection refused' 450건과 함께 core-banking-api 묶음이 첫 이벤트 약 9분 뒤 promote 됐다(PG incidents d611f714, b9d947d0). 그 창은 transfer 롤아웃 두 번(KCM 이벤트)이 있었고 어떤 실행이었는지, 동반 부하가 있었는지는 확인하지 못했다. 그래서 이 설계는 동반 부하와 고장 길이(약 14분)로 조건을 따로 확보한다.

## 7. 관측 근거 표 (119 실조회, 2026-10-08 18:50~19:20 UTC)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 결정 증거는 KCM(이벤트 전수, ReplicaSet 스펙 전수)과 로그(전수)다. 트레이스는 보조다.

| 증거 | 119 표와 칸 | 조회 | 평시 결과 |
|---|---|---|---|
| 근본: 하류 주소 설정 | PG `kcm_resources_history` (kind replicaset, namespace rca-testbed-banking, yaml, captured_at) | yaml 에 'TRANSFER_SERVICE_URL' 이 든 레코드 수, 전 기간 | banking ReplicaSet 290건 중 0건(값은 늘 ConfigMap envFrom 에서 옴). testbed-account ReplicaSet 33건, 마지막 2026-08-11 00:04 |
| 근본: 스펙이 실제로 잡히는가 | 같은 표 | banking testbed-transfer-74b498b67b(2026-10-01 17:31 잡음) | yaml 에 컨테이너 env 배열 전체(DB_USER, JAVA_TOOL_OPTIONS, OTEL_*)가 담김. 같은 날 food payment env 롤아웃(F30-R 실행)의 새 ReplicaSet 도 잡힘. 녹화 범위에 kcm_resources_history(captured_at) 포함 |
| 계기: 롤아웃 | CH `kcm_events_local` (namespace, object_kind, object_name, reason, body) | namespace='rca-testbed-banking', banking 첫 이벤트 2026-08-24 03:46 (표 보존 시작 2026-08-13 05:28) 이후 | banking 이벤트 2,029건 중 testbed-account ScalingReplicaSet 0건. 롤아웃 이벤트 모양은 2026-10-01 01:06 testbed-transfer 롤아웃(ScalingReplicaSet, Scheduled, Pulled, Created, Started, 옛 ReplicaSet 축소, Killing)으로 확인 |
| 전파: 잘못된 URL 로그 | CH `lucida_logs_local` body | `body LIKE '%testbed-ledger%'`, 8일(보존 2026-10-01 16:00~), 전 서비스 | 0건 |
| 같은 | 같은 표 | core-banking-* `body LIKE '%Connect timed out%'`, 8일 | 0건(같은 문구는 commerce-product 10,637건, commerce-gateway 323건뿐) |
| 전파: account 평시 ERROR | 같은 표 | core-banking-account 'Transfer service call failed%' 날짜별, 8일 | 2026-10-01 178건, 10-02 306건(전부 testbed-transfer 로의 'Connection refused' 또는 500), 10-03~10-08 0건. 'Transfer service circuit open%' 도 10-03 뒤 0건 |
| 하류 정상(대조) | 같은 표 | account 'Account validation passed', transfer 'Transfer COMPLETED ... from=ACC-' 와 '... from=commerce-settlement', 2026-10-06, 10-07 | account 검증 하루 21,914 / 21,891건, transfer ACC 계좌 이체 31,715 / 31,658건(account 경로 + NodePort 직행), commerce 정산 41,584 / 41,760건. 고장 중 account 경로 몫만 끊기고 직행과 정산은 이어져야 한다 |
| 호출 경로(보조) | CH `otel_traces_local` CLIENT 스팬 span_attributes['server.address'], ['url.full'] | 2026-10-08 17:00~18:00 UTC, transfer 를 향한 CLIENT 스팬 | commerce-payment POST 82, account POST 41(`http://testbed-transfer:8082/api/transfers`), api GET(거래 내역). transfer SERVER POST 의 k6 직행 28. 고장 중 account CLIENT 스팬의 server.address 가 testbed-ledger, server.port 8082 가 된다(표본 10%) |
| 골든 시그널(감지) | VM `apm.agent.otel.java.error_rate` {service_name=~"core-banking-(account\|api)"}, CH MV `agg_service_golden_signals` | `quantile_over_time(0.99, …[7d])`, MV KST 2~6시 시간대별 평균 7일 | 오류율 99백분위 둘 다 0. 표본 요청 api 분당 2.8~3.3, account 4.8~5.6, 오류 분당 0~0.03, ERROR 로그 api 분당 0.1~0.8, account 0~0.4 |
| 하류 DB 부담(배제용) | CH `dpm_topsql_local` (sql_id, body 의 executions, avgCpuTime, avgElapsedTime) | BANKING 스키마 최근 1시간 | 거래 내역 COUNT(byb2y2hv7p4ag) 1회 평균 437ms(CPU 394ms), 시간당 911회. 페이지 조회(470as8m1vuxbs) 46ms. 이체 쪽 질의는 1ms 미만 |
| 피해 | 동반 부하 k6 live 문서 | 이체 step 5xx 비율 | 평시 0(업무 거절은 400) |

## 8. 감별

- must_support: 정답지 5항목(롤아웃 이벤트와 새 ReplicaSet 스펙의 명시 TRANSFER_SERVICE_URL, account ERROR 로그의 잘못된 URL 과 Connect timed out, account 와 api 서버 스팬 502, account 검증과 잔액 조회는 계속되고 transfer 는 다른 호출자에게 답하며 account 경로 이체만 끊김, 동반 부하 이체 5xx 와 잔액 조회 정상).
- must_rule_out(정답지): transfer 장애(F17-R 꼴은 testbed-transfer 로 'Connection refused' 와 transfer NotReady), ledger 장애(ledger 는 원래 호출 대상이 아니고 8082 연결은 ledger 파드에 닿지 않음), account 나 Oracle 장애(F35-R 꼴은 잔액 조회도 실패하고 파드 NotReady), 이름 해석 실패(F37-R 꼴은 호스트 이름뿐인 I/O 오류), 부하 증가, 코드나 이미지 배포(새 ReplicaSet 은 env 한 줄만 다름).
- 부하 증가 경쟁 가설: 동반 부하 5rps 는 유입을 늘린다. 그러나 ① 오류가 풀 고갈, DB 지연, 읽기 시간 초과가 아니라 로그에 적힌 잘못된 호스트로의 연결 시간 초과이고 ② 같은 부하의 35% 를 받는 잔액 조회(nginx → account, 같은 account 파드와 Oracle)는 실패하지 않으며(러너 배제 조건 `account-path-failing`), 거래 내역(api → transfer)도 정상이고 ③ transfer 는 오히려 account 발 요청을 받지 못하며 ④ 동반 부하는 패치와 함께 오르기 시작하지만 오류는 새 account 파드가 Ready 가 된 뒤에만 나고(그 전 약 1분은 같은 부하에서 이체가 성공), 부하 곡선이 아니라 롤아웃 시각에 맞춰 0 에서 거의 1 로 뛴다. 하류 DB 부담: 동반 부하의 거래 내역 초당 0.75건 × 약 0.48초(COUNT 437ms + 페이지 46ms, 대부분 CPU) ≈ Oracle CPU 0.36개 분량이 기준선(초당 0.25건 ≈ 0.12개) 위에 더해진다. Oracle Free 의 CPU 상한 2개 아래이고, 고장 중에는 이체 쓰기가 오히려 줄어든다. banking surge 의 실측 건강 상한은 20rps(surge.js 머리말 §8.1-1, 2026-07-15)라 5rps(기준선 최고 4 iter/s 를 더해도 9rps)는 그 아래다.
- contrast_with: F37-R(api dnsPolicy, 모든 하류 이름 해석 실패, 이체와 거래 내역 함께 실패), F17-R(같은 증상, 원인은 transfer readiness 오설정, Connection refused, commerce 정산도 실패), F08-P(호출 쪽 read-timeout 오배포, 요청이 하류에 닿은 뒤 끊김), F35-R(Oracle 계정 잠금, 잔액 조회도 실패).

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

설계 강도 하나로 고정한 평가 모드(`approved-fixed-f39-r`). 강도라 할 만한 값은 없다: 주소는 바뀌거나 되돌려지는 것뿐이고, 새 파드가 Ready 가 된 뒤 account 를 거치는 이체는 요청량과 무관하게 전부 실패한다.

- 한 요청의 대기: account 서킷이 닫혀 있을 때 시도마다 연결 시간 초과 3초, 재시도 200ms, 400ms 라 account 안에서 약 9.6초 뒤 502 다. api 의 read-timeout 10초에 가까워 일부는 api 쪽 'Read timed out' 으로 먼저 끝날 수 있다(둘 다 502). 서킷이 열린 뒤에는 바로 502 다. 고장 직후 account 서킷은 실패 시도 5회(초당 이체 약 2.1건이면 처음 약 3초 안에 시작된 시도들)가 끝나는 약 3~6초 뒤 열린다.
- 시간: 패치 → 새 파드 생성, JVM 기동(banking 앱 실측 프로세스 기동 약 45초, 2026-10-01 transfer 'Started ... in 39.7 seconds (process running for 46.7)'), startupProbe(5초 주기) 통과, readiness Ready → 옛 파드 종료. 약 1분 뒤 고장이 시작된다(settle 60s). min_hold 15m 이라 판정(성공 3틱)은 패치 약 15.75분 뒤이고 전면 실패는 약 14분 이어진다. cleanup 은 env 를 되돌리고 롤아웃이 available 이 될 때까지(보통 약 1분, 상한 180초) 기다린다. 그 1분 동안에도 실패가 이어진다.
- 시간대: 피해는 요청량과 무관하다. 가장 한가한 KST 2~6시(기준선 1 iter/s)에도 동반 부하가 이체 초당 2.0건을 더해, 러너 판정 표본(이체 step 30초 창 약 60건)과 119 표본 스팬(api 분당 약 13개 ERROR)이 시간대에 좌우되지 않는다.
- 업무 거절: 잔액이 금액보다 적으면 account 가 넘기기 전에 400 으로 거절한다(5xx 아님). 2026-10-08 19:05 UTC 잔액(GET /api/accounts/ACC-100x): 1001 119,295, 1002 457,321, 1003 6,110,610, 1004 164,977, 1005 18,530,258, 1006 36,592, 1007 339,137. 금액이 1,000~50,000 이라 400 은 ACC-1006 출금의 일부(이체의 약 4%)뿐이고, api 서킷이 열리면 그것도 502 가 된다. 이체 5xx 비율은 0.9 를 넘을 것으로 계산한다(성공 문턱 0.5).
- 스레드: 동시 대기 요청은 서킷이 닫힌 처음 몇 초와 half-open 시도 때만 생기고 초당 약 2.1건 × 약 10초 ≈ 21개로 Tomcat 기본 200 스레드에 한참 못 미친다.
- 서킷브레이커: 열리면 account, api 가 바로 502 로 답하고, half-open 시도도 같은 호스트로 가서 다시 열린다. 피해를 숨기지 않는다.
- liveness 는 livenessState, readiness 는 자기 DB 라 고장 파드는 재시작되지 않는다.

| 항목 | 값 |
|---|---|
| level | `approved-fixed-f39-r`, min_hold 15m, settle 60s, timeout 20m |
| max_injection_duration | 25m |
| companion | `load.north_south` core-banking transfer-heavy-surge.js(이체 40, 잔액 35, 거래 내역 15, 계좌 목록 10), target_rps 5, entry 30082, ramp 2m + hold 21m + ramp_down 15s |
| success | 동반 부하 이체 5xx 비율(`loadgen.food_create_status_rate`, 선택자 business.5xx.rate) ≥ 0.5, 3틱 연속 |
| must_rule_out | 동반 부하 achieved_rps < 1.25(부하 끊김), 동반 부하 잔액 조회 실패율 ≥ 0.2(account 자체나 Oracle 장애), transfer 파드 NotReady(하류 장애), 2틱 |
| abort | entry_status(domain core-banking, 이체 단계 nginx → api) == 0, 2틱. api 와 nginx 는 살아서 502 로 답하므로 이 시나리오로는 0 이 나오지 않는다 |
| recovery | 기준선 이체 5xx 비율(`loadgen.food_create_status_rate` {domain: core-banking}) < 0.05 가 판정력을 가진 조건이고, target_health 200(러너 고정 대상이 commerce 진입점이라 banking 과 무관)과 transfer 파드 Ready(고장 중에도 참)는 compile-plan 이 요구하는 형식 조건이다. 2틱, 10m |
| cleanup | env 를 원래 배열로 패치한 뒤 롤아웃 available 확인(180초), 동반 부하 종료, 10m |

판정 근거(헌장 기록 형식, 평시 값과 장애 값):

| 조건 | 관측 | 평시 | 장애 중 | 판정력 |
|---|---|---|---|---|
| success | 동반 부하 이체 5xx 비율 | 0(업무 거절은 400) | 약 0.9~1.0(account 경로 이체 전부 502, api 서킷이 열리면 400 몫도 502) | 있음(문턱 0.5) |
| must_rule_out account-path-failing | 동반 부하 잔액 조회 실패율 | 0 | 0(고장 난 account 파드도 잔액 조회에 200) | 교란(account 자체나 Oracle 장애)이 있을 때만 0.2 이상. 이 시나리오의 고장으로는 오르지 않는 것이 맞다 |
| must_rule_out transfer-down | transfer 파드 Ready | true | true | 교란(F17-R 꼴)이 있을 때만 false |
| recovery | 기준선 이체 5xx 비율 | 0.0(2026-10-08 19:33 UTC, KST 04:33 가장 한가한 시간, 109 tb-runner `/tmp/rca-baseline-core-banking-live.json`: checkout_count 4, business_5xx_rate 0.0, entry_status 200) | 약 1.0(기준선 이체도 nginx → api → account) | 있음(문턱 0.05) |

회복 표본 계산: 가장 한가한 시간 기준선 1 iter/s × 이체 10% × 30초 창 = 이체 약 3건(실측 4건). 5xx 가 한 건만 섞여도 약 0.25~0.33 이라 문턱 0.05 를 넘고, 2틱 연속이 필요하다. 창에 이체가 0건이면(포아송 기대 3건에서 약 5%) loadgen_monitor 가 비율 필드를 싣지 않아 그 틱은 unusable 이 되고 통과로 읽히지 않는다. 회복 timeout 10m 안에 2틱 연속 판정이 설 확률은 충분하다. 잔액 조회는 고장 난 account 파드도 답하므로 회복 근거로 쓰지 않는다(1회차 평가 지적, 틀린 `_note` 를 고침).

새 관측 쿼리, 새 실행기, 러너 변경은 없다. 기존 `k8s.env` 실행기의 허용 표(APPROVED_TARGETS, APPROVED_KEYS)에 F39-R(testbed-account, account-service, 키 TRANSFER_SERVICE_URL)만 더했다. 러너의 kubernetes 프로브 허용 목록(live_probes APPROVED_K8S_TARGETS)에 testbed-account 가 없어 account 파드 Ready 는 관측하지 않는다. account 롤아웃 회복은 cleanup 이 롤아웃 available 을 기다리는 것과 기준선 이체 5xx 비율(`transfer_5xx_rate_baseline`) 회복으로 확인한다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(GitHub 2026-03-05, 공식 링크, 같은 기전의 2026-06-10)와 요소별 대응표가 있고, 기전("롤아웃된 설정이 내부 트래픽을 잘못된 호스트로 보내 의존 기능이 실패하고 설정을 바로잡아 복구")이 같다 (원칙 1, §2)
- [x] 장부를 갱신했다: 묶음 G(4→5, 12%), 정답 위치 은행 계좌 서비스(0), 결제 경로 23% 와 무관, 서비스 은행 10(음식배달 9 를 고르지 않은 이유는 §3), 어느 축도 20% 에 닿지 않는다 (원칙 2, §3)
- [x] 근본 원인 위치: Deployment 컨테이너 env 와 ConfigMap(`21-account-service.yaml:31-33`, `02-configmaps.yaml:15`), 주소를 받는 자리(`application.yml:26-30`, `RestClientConfig.java:14-24`), 실패가 로그가 되는 자리(`TransferClient.java:28-58`), 잘못된 호스트의 Service(`23-ledger-service.yaml:97-105`)가 확인됐다 (G1, §4)
- [x] 근본 원인의 흔적: PG `kcm_resources_history` 의 ReplicaSet env(평시 banking 290건에 명시 TRANSFER_SERVICE_URL 0건, ReplicaSet 스펙에 env 배열 전체가 실제로 잡힘)와 account 로그의 URL (원칙 3, §7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: KCM 이벤트와 ReplicaSet 스펙(전수), 로그(전수). 트레이스의 server.address 는 보조 (원칙 3)
- [x] 계기의 흔적: testbed-account 롤아웃 KCM 이벤트(banking 이벤트 보존 안에서 처음)와 새 ReplicaSet 스펙. 인공 지연을 쓰지 않는다. 대기가 있다면 실제로 응답 없는 주소로의 연결이 시간 초과되는 시간이다 (원칙 4)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다: 롤아웃 직후 account 로그에 찍힌 잘못된 URL 과 스펙의 env 한 줄 차이 (원칙 5)
- [x] 정답지 세 칸: 근본 `core-banking-account`(config), 계기 비움, 부분 점수 core-banking-api (원칙 6, §5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다. 골든 시그널이 오를 근거(고장 즉시 account 경로 이체 전부 502, 동반 부하로 api 표본 분당 약 13개 ERROR, 고장 약 14분)를 설계로 확보했고 판정 입력의 한계(최근 15분 대 3시간, 멤버 상위 3개 서비스, ledger 잡음 인시던트)를 적었다 (원칙 7, §6)
- [x] 주입 도구가 시나리오 id 를 남기지 않는다: k8s.env 실행기는 네임스페이스, Deployment, 컨테이너, env 배열만 kubectl 로 넘긴다. 클러스터에 남는 것은 env 한 줄과 롤아웃뿐이고 그 값에 시나리오 id 가 없다. 동반 부하 태그는 tb-runner 의 k6 쪽에만 남는다(기존 시나리오와 같음) (원칙 8)
- [x] 피해 계산: 요청량과 무관한 전면 실패, 서킷브레이커는 피해를 숨기지 않음, 업무 거절 비율, 스레드 여유, 동반 부하 5rps 는 실측 건강 상한 20rps 아래, 부하 증가 경쟁 가설은 관측으로 배제 (원칙 9, §8, §9)

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- 새 account 파드가 Ready 가 되어 옛 파드를 대체하는지(롤아웃이 멈추면 피해가 없다), 패치에서 고장 시작까지의 시간.
- account 로그가 실제로 `Transfer service call failed for order null: I/O error on POST request for "http://testbed-ledger:8082/api/transfers": Connect timed out` 꼴로 남는지, 서킷이 열린 뒤에도 half-open 시도마다 되풀이되는지. 다르면 정답지의 결정 증거 문구를 녹화본에 맞게 고친다.
- api 로그가 'Account service call failed ... 502' 와 'Read timed out' 중 어느 쪽으로 남는지(account 안 약 9.6초와 api read-timeout 10초가 가까움), 이체 p95.
- KCM 이 새 ReplicaSet 을 명시 env `TRANSFER_SERVICE_URL=http://testbed-ledger:8082` 로 잡는지.
- 판정이 promote 해 인시던트가 생기는지. `incidents.promoted_at`(last_reviewed_at 아님)과 promote 된 묶음의 멤버 상위 3개 서비스에 core-banking-api, core-banking-account 가 들어갔는지, ledger 잡음 인시던트와 섞이는지 기록한다. 인시던트가 안 생기면 먼저 판정 지연(promoted_at − 묶음 first_event_at)을 본다.
- 잔액 조회와 거래 내역이 고장 내내 정상인지(배제 조건이 잘못 발화하지 않는지), transfer 의 commerce-settlement 'Transfer COMPLETED' 가 이어지는지, cleanup 뒤 기준선 이체 5xx 비율이 0 으로 돌아와 회복 조건이 서는지와 그 시각.
- promote 된 묶음의 main_service 와 멤버 상위 3개 서비스. core-banking-ledger 잡음 인시던트가 8일 271건이라 같은 묶음이 될 수 있다.
- `incidents.promoted_at` 과 first_event_at 의 차이. banking 선례는 약 18~20분이라 고장이 끝난 뒤 promote 될 수 있으므로, 회복 직후가 아니라 cleanup 뒤 30분 이상 지나서 확인한다.
