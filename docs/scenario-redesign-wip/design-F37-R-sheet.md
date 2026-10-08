---
title: F37-R 설계 시트 (banking api-service 파드 DNS 정책을 노드 resolver 로 바꾸는 배포로 클러스터 이름 해석 실패)
status: Draft
owner: project
last_reviewed: 2026-10-08
tags:
  - scenario
  - design
  - dns
  - config-deploy
summary: banking api-service 롤아웃이 파드 dnsPolicy 를 ClusterFirst 에서 Default(노드 resolver)로 바꿔, 새 api 파드가 testbed-account, testbed-transfer 를 해석하지 못하고(UnknownHostException) 이체와 거래 내역이 502 로 실패하는 시나리오. 원본은 Intercom 2024-11-05 서버 이미지에 더한 캐싱 resolver 가 사설 DNS 레코드를 해석하지 못한 장애.
---

# F37-R 설계 시트

## 1. 요약

banking api-service(Deployment testbed-api)는 nginx 뒤에서 이체(POST /api/transfers)와 거래 내역(GET /api/transfers)을 받아 account(`http://testbed-account:8081`)와 transfer(`http://testbed-transfer:8082`)를 부른다. 두 주소는 클러스터 Service 의 짧은 이름이라 클러스터 DNS(CoreDNS)와 그 검색 도메인이 있어야 해석된다. 롤아웃 하나가 api 파드 템플릿의 `dnsPolicy` 한 칸을 ClusterFirst(기본값)에서 Default 로 바꾼다. Default 파드는 노드의 resolver(tb-w2 kubelet 이 주는 `nameserver 192.168.122.1`)를 쓰는데, 이 resolver 는 공개 이름에는 답하지만 클러스터 이름은 해석하지 못한다. 노드에서 물으면 SERVFAIL, 파드 네트워크에서 물으면 응답 없이 시간 초과다(109 실측, §4). api 는 DB 를 쓰지 않고 `/actuator/health` 도 이름을 해석하지 않으므로 새 파드는 프로브를 통과해 Ready 가 되고, 패치 약 1분 뒤 옛 파드를 대체한다. 그 뒤 api 의 하류 호출은 모두 연결 전 이름 해석에서 실패하고(java.net.UnknownHostException, 캐시되지 않은 해석마다 약 10초 = glibc 시간 초과 5초 x 2회), 재시도 3회와 서킷브레이커 끝에 502 가 된다. 잔액 조회와 계좌 목록(nginx 가 /api/accounts 를 account 로 바로 보냄), commerce 정산 이체(commerce-payment 가 transfer 를 바로 부름)는 정상이다. banking 동반 부하 6rps 를 붙여 가장 한가한 시간에도 core-banking-api 표본 스팬과 러너 판정 표본을 확보한다(§6, §9).

비유: 사내 내선 번호부 대신 시내 전화번호부를 쓰도록 한 직원의 전화기 설정만 바꿨다. 바깥 가게 번호는 다 나오는데 옆 부서 번호는 없어서, 그 직원이 거는 사내 전화만 전부 안 걸린다. 옆 부서는 멀쩡히 다른 사람 전화를 받고 있다.

## 2. 원본 사례

- 기업: Intercom(현 Fin, 미국 호스팅 지역)
- 날짜: 2024-11-05 03:30~06:00 UTC(약 2.5시간)
- 링크: [공식 사후 보고](https://www.intercomstatus.com/us-hosting/incidents/01JBX9TND1N4J6PM95X6X5ZP0K/write-up) (현재 같은 경로의 [finstatus.com](https://www.finstatus.com/us-hosting/incidents/01JBX9TND1N4J6PM95X6X5ZP0K/write-up) 으로 넘겨짐)
- 요약(출처가 말한 것만): 앞선 장애의 후속 조치이자 가끔 생기던 DNS 지연을 고치려고 Linux 서버 이미지에 캐싱 resolver(unbound)를 더했다. 이 이미지는 이미 많은 애플리케이션에 문제없이 나가 있었다. 미국 애플리케이션은 MySQL 앞의 ProxySQL 클러스터를 사설 DNS 영역으로 찾는데, unbound 는 호스트가 인터넷 DNS 에 닿을 수 있으면 사설 레코드를 해석하지 못했다("the private DNS records were not resolvable"). 인터넷 DNS 질의가 막힌 스테이징과 사전 운영에서는 드러나지 않았다. 서버 이미지는 자동으로 다시 빌드되고 애플리케이션 서버가 매주 교체되는데, 03:27 운영에 닿은 뒤 교체된 백그라운드 워커가 ProxySQL 엔드포인트를 해석하지 못해 Rails 프로세스를 띄우지 못하고 작업이 쌓였다. 웹 서버는 교체 호스트가 health check 를 통과해야 넘겨받으므로 영향이 없었다. 03:44 경보, 04:48 이미지 롤아웃을 원인으로 식별, 롤백이 배포 잠금에 걸려 05:46 재시도, 06:00 정상. 재발 방지는 이미지 롤아웃 강화와 사설 DNS 레코드 사용 제거.
- 함께 본 사례: Let's Encrypt 2025-07-21(내부 resolver 클러스터의 설정 스크립트가 잘못된 전달자를 설정해 모든 머신의 resolver 설정이 무효가 되고 SERVFAIL 또는 시간 초과로 DB 서버 이름 해석이 깨짐, 임시로 /etc/hosts 에 DB 주소를 적어 복구, [공식](https://community.letsencrypt.org/t/2025-07-21-complete-api-outage/240985)). 둘 다 `ref-real-world-incidents.md` M16 에 더했다.
- 현실 비중: 설정 배포는 Google 포스트모템 트리거의 31%(M1), DNS 는 M16 에 공식 사례가 여럿이다. 이 장부에서 M(DNS)은 정식 + 후보 0 이다.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 서버 이미지 롤아웃(캐싱 resolver unbound 추가, DNS 지연 개선 목적) | testbed-api Deployment 롤아웃(파드 dnsPolicy ClusterFirst → Default, 클러스터 DNS 를 거치지 않고 노드 resolver 를 쓰게 함) |
| 원인이 된 결함 | 새 resolver 가 공개 이름은 해석하지만 사설 DNS 영역(ProxySQL 엔드포인트)은 해석하지 못함 | 노드 resolver 192.168.122.1 이 공개 이름은 해석하지만 클러스터 Service 이름(testbed-account, testbed-transfer)은 해석하지 못함(노드에서는 SERVFAIL, 파드 네트워크에서는 응답 없이 시간 초과) |
| 전파 경로 | 교체된 워커가 내부 DB 엔드포인트를 해석하지 못해 프로세스를 띄우지 못함 → 작업 적체 | 새 api 파드가 하류 이름을 해석하지 못해 모든 하류 호출이 UnknownHostException → 재시도, 서킷 열림 → 502 |
| 사용자 증상 | 대화 갱신, Fin 응답, 웹훅 전달, 데이터 갱신 지연(웹 요청은 정상) | 이체와 거래 내역 502(일부는 약 10초 기다린 뒤), 잔액 조회와 계좌 목록, commerce 정산은 정상 |
| health check 의 역할 | 웹 서버는 교체 호스트가 health check 를 통과해야 넘겨받아 무사했음 | account, transfer, ledger 처럼 DB 를 보는 health 를 가진 파드였다면 Ready 가 되지 못해 롤아웃이 옛 파드를 남긴 채 멈춘다(피해 없음). api 는 health 가 이름과 무관해 대체가 끝난다(워커 쪽 모양) |
| 원본의 탐지 경로 | 03:44 경보(운영 도달 17분 뒤), 04:48 롤아웃을 원인으로 식별 | api 오류율, 지연, ERROR 로그 급증, KCM 롤아웃 이벤트와 새 ReplicaSet 스펙 |
| 완화와 복구 | 이미지 롤백 | dnsPolicy 를 ClusterFirst 로 되돌리는 롤아웃 |

기전은 원본과 같다: "배포된 resolver 설정이 공개 이름만 알고 내부 이름을 모르게 되어, 그 설정을 받은 호스트가 내부 의존 대상에 닿지 못한다. health check 가 그 이름을 보지 않는 호스트만 서비스에 들어가 피해가 난다". 바꾼 것은 규모와 층이다. 원본은 서버 이미지(호스트 OS)였고 여기서는 파드 스펙이며, 원본은 워커 묶음 전체였고 여기서는 서비스 하나다. 원본의 워커는 시작 시 해석이 필요해 프로세스가 뜨지 못했고, api 는 요청마다 해석해 프로세스는 뜨고 요청이 실패한다(우리 코드의 성질, 증상 층의 차이일 뿐 원인에서 증상까지의 고리는 같다).

## 3. 숫자 근거 (2026-10-08, `scenario-stats.py`, 정식 + 후보 합계 40, 이 후보 전)

| 축 | 값 |
|---|---|
| 묶음 | A 7, B 7, D 7(각 17%), C 6, G 3, E 2, F 2, H 2, L 2, I 1, O 1, J, K, M, N 0. 이 후보는 **M DNS(0)**, 계기가 배포 설정이라 M+G |
| 정답 위치 | 외부 결제 의존 6, 주문 서비스 6(각 15%), 결제 경로 합계 10(25%, 금지). 이 후보는 **은행 API(0)**, §2-1 에 이미 있는 말 |
| 서비스 | 쇼핑몰 23, 은행 9, 음식배달 8 |

이 후보를 고른 이유:

- 합계 0 인 묶음(J, K, M, N)이 최우선이다. J 는 결함 이미지가 없고(app.release live_supported false, 앱 코드 변경은 사람 검토 대상), K 는 이번에도 막혔다: Pinpoint 2026-03-19(DB 방화벽 규칙 범위가 너무 좁아 앱 서버가 DB 에 닿지 못함, [공식](https://status.pinpoint.support/incidents/dqtssjdqkd87))를 NetworkPolicy 로 재구성하려 했으나 tb 클러스터 CNI 가 flannel 단독이라 NetworkPolicy 가 집행되지 않는다(109 kubectl 2026-10-08: kube-flannel DaemonSet 만 있고 정책 컨트롤러 없음, 설계 단계에서 버림, rejected 기록). N 은 F14-R 막힘 그대로다. M 은 같은 날 폐기된 F31(로컬 기록, AWS 2025-10-19~20 빈 DNS 레코드, Service 삭제 주입)이 있었으나, 이 후보는 원본 사례, 주입(파드 dnsPolicy, Service 와 CoreDNS 는 그대로), 정답 위치(은행 API, F31 은 가게 서비스 Service)가 모두 다르다.
- 정답 위치 은행 API 는 0 이고 결제 쪽이 아니다.
- 서비스: 음식배달(8)이 은행(9)보다 1 적지만 음식배달로는 피해가 나지 않는다. 사용자 요청을 받는 order, restaurant, dispatch, payment 는 모두 DB 를 보는 `/actuator/health` 를 readiness 로 써서, 같은 배포를 하면 새 파드가 `testbed-mysql` 을 해석하지 못해 Ready 가 되지 않고 롤아웃이 옛 파드를 남긴 채 멈춘다(원본의 웹 서버 쪽 모양). DB 를 쓰지 않는 notify 는 Kafka 소비자라 사용자 요청 경로에 없어 5xx 가 나지 않는다(원칙 7). 은행에서도 account, transfer, ledger 는 같은 이유로 롤아웃이 멈추고, DB 를 쓰지 않으면서 사용자 요청을 받는 서비스는 api 하나다. 쇼핑몰 23 과의 차이 규칙(원칙 2)에는 둘 다 걸리지 않는다.
- rejected 기록과 장부 §4-1, §5 에 같은 원본 사례나 같은 주입이 없다. 최근 F34-R 폐기 교훈("4xx 업무 거절 단일 신호")과 달리 이 후보의 피해는 서버 스팬 502 와 지연, 여러 템플릿의 ERROR 로그다.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 배포 정본: `core-banking/k8s/20-api-service.yaml:17-25` 파드 템플릿에 dnsPolicy 가 없어 기본값 ClusterFirst. 109 실배치(2026-10-08 kubectl): dnsPolicy ClusterFirst, strategy RollingUpdate maxSurge 25% / maxUnavailable 25%(replicas 1 이라 각각 1, 0), OTLP 내보내기 `http://192.168.200.109:14318`(IP 라 이름 해석과 무관, 관측 데이터는 끊기지 않음).
2. 하류 주소: `core-banking/k8s/02-configmaps.yaml:14-15` (ACCOUNT_SERVICE_URL `http://testbed-account:8081`, TRANSFER_SERVICE_URL `http://testbed-transfer:8082`) → `api-service/src/main/resources/application.yml:8-16` (services.account.url, services.transfer.url).
3. 해석이 실패하는 자리: `api-service/.../config/RestClientConfig.java:14-34` (SimpleClientHttpRequestFactory, HttpURLConnection). JDK 는 `InetSocketAddress` 생성 때 이름을 해석하고, 실패하면 미해석 주소로 두었다가 `Socket.connect` 에서 `UnknownHostException(호스트 이름)` 을 던진다. Spring RestClient 는 이것을 `ResourceAccessException("I/O error on POST request for \"<url>\": <원인 메시지>")` 로 감싼다. 로컬 실측(2026-10-08, spring-web 6.2.6 + SimpleClientHttpRequestFactory, 해석 안 되는 이름): 메시지 `I/O error on POST request for "http://testbed-account.invalid:8081/api/accounts/transfer": testbed-account.invalid`, 원인 `java.net.UnknownHostException: testbed-account.invalid`. 즉 로그의 I/O 오류는 **호스트 이름 하나로 끝난다**(연결 거부는 'Connection refused', 시간 초과는 'Read timed out').
4. 로그와 응답: `api-service/.../service/ApiService.java:28-38` (submitTransfer, INFO 'Submitting transfer' 뒤 호출) → `client/AccountClient.java:28-48` (requestTransfer, ERROR 'Account service call failed for order {}: {}' 후 502) → `:50-58` (requestTransferFallback, ERROR 'Account service circuit open/exhausted' 후 502 'Account service unavailable'). 거래 내역은 `client/TransferClient.java:32-60` (listTransfers, ERROR 'Transfer service list call failed: {}', fallback 502). 재시도와 서킷: `application.yml:39-54` (max-attempts 3, 200ms 지수 백오프), `:19-37` (sliding window 10, 최소 5회, 실패율 50%, 열림 5초).
5. 경로: `02-configmaps.yaml:38-50` nginx `/api/transfers` → api, `/api/accounts` → account 직행. commerce-payment 는 transfer 를 FQDN 으로 직접 부른다(README 호출 그래프).
6. 노드 resolver: tb-w2 kubelet `resolvConf: /run/systemd/resolve/resolv.conf` = `nameserver 192.168.122.1`(libvirt dnsmasq). dnsPolicy Default 파드는 이 파일을 받는다.

109 실측(2026-10-08 17:00~17:20 UTC):

- tb-w2 노드에서 `dig @192.168.122.1 testbed-account`, `testbed-account.rca-testbed-banking.svc.cluster.local` → SERVFAIL(1ms 안팎). `github.com` → NOERROR.
- 파드 네트워크(banking nginx 파드)에서 `nslookup testbed-account 192.168.122.1` → 'connection timed out; no servers could be reached'(5.0초, 1회차 평가 재측정). 즉 파드에서는 SERVFAIL 이 아니라 응답 없이 시간 초과이고, glibc 기본(시간 초과 5초, 시도 2회)으로 아래 약 10초가 설명된다.
- tb-w2 에서 resolv.conf 를 `nameserver 192.168.122.1` 로 둔 격리 마운트 이름공간(`unshare -m`, 노드 설정 무변경)으로 glibc 해석: `getaddrinfo('testbed-account')` 와 `'testbed-transfer'` 가 10.01초 뒤 `EAI_AGAIN`(Temporary failure in name resolution), `getent ahostsv4` 도 10.0초 뒤 실패, `github.com` 은 즉시 성공. api 이미지(eclipse-temurin:17-jre, glibc)도 같은 경로다. JDK 기본 음성 캐시 10초(`networkaddress.cache.negative.ttl`)라 해석은 약 10초 막혔다가 10초 동안 즉시 실패하기를 되풀이한다.
- banking nginx 파드(클러스터 DNS)에서 `nslookup github.com 192.168.122.1` 성공: 파드 네트워크에서 그 resolver 에 닿는다. 같은 파드의 클러스터 DNS 로 testbed-account 해석도 정상.
- 실행기 preflight(읽기 전용: 현재 dnsPolicy ClusterFirst, dnsConfig 없음, 롤아웃 상태 정상, patch 권한)를 109 에서 직접 돌려 rc 0.

## 5. 원인 규정 (원칙 5, 6)

| 칸 | 값 | 근거 |
|---|---|---|
| 근본(`target_id`) | `core-banking-api` | 결함을 가진 곳은 api 의 배포 설정(파드 dnsPolicy)이다. 고쳐야 재발이 막히는 곳도 같다. APM 서비스 이름(OTEL_SERVICE_NAME)으로 적는다. target_kind config |
| 계기(`trigger_target_id`) | 비움 | 계기(롤아웃)가 근본과 같은 곳에서 일어났다 |
| 부분 점수 | coredns, kube-dns | 이름 해석 실패라는 기전은 맞혔으나 클러스터 DNS 를 범인으로 지목한 답. 클러스터 DNS 는 멀쩡하다 |
| 입도 | service | |

- 원칙 6: api 의 요청은 늘 하던 그대로 정당하다. 그 요청이 해석 단계에서 실패하게 만든 것은 api 자신의 배포 설정이다. 하류 account, transfer 는 증상의 대상일 뿐 결함이 없어 부분 점수도 주지 않는다(오답). 원본 사후 보고가 지목한 층위("서버 이미지에 더한 resolver 가 사설 레코드를 해석하지 못함")와 같은 층위다.
- 원칙 5: 정답은 KCM 의 롤아웃 이벤트와 새 ReplicaSet 스펙의 dnsPolicy 한 칸 차이, 그 직후 api 로그의 호스트 이름뿐인 I/O 오류(UnknownHostException 의 모양, 로그에는 예외 형식이 남지 않음)라는 전수 근거 쌍으로 낸다. 예외 형식은 10% 표본 CLIENT 스팬에만 있어 보조다. 코드 설계 결함 추론이 필요 없다. 누가 왜 정책을 바꿨는지는 요구하지 않는다.
- class B(설정, 코드 결함 아님), fault_pattern 없음.

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 | 이 시나리오 |
|---|---|---|
| 감지 | lucida-next 이상 탐지 | core-banking-api ERROR 로그 급증(새 템플릿: 호스트 이름으로 끝나는 I/O 오류), api 서버 스팬 502(오류율), 해석 대기로 오른 p95. 업무 규칙 없이 겉 증상으로 이벤트가 생긴다 |
| 피해 판정 | 러너 | 동반 부하 이체(step transfer) 5xx 비율, 평시 0 에서 0.5 이상 |
| 원인 설명 | 녹화 데이터 | §7 의 KCM 이벤트와 ReplicaSet, 파드 스펙(PG), 로그(전수), 하류의 정상 응답 로그 |

인시던트가 생길 조건(지휘 세션이 넘긴 119 판정 기전 재측정 요약과 이번 조회):

- 판정 입력은 CH MV `agg_service_golden_signals`(표본 SERVER, CONSUMER 스팬 status_code='ERROR'), 판정 시각 기준 최근 15분 대 그 전 3시간, 묶음 멤버 상위 3개 서비스. promote 는 대개 고장 2~4분째의 첫 판정(promoted_at 기준)에서 난다. 첫 판정의 15분 창에는 고장이 2~4분만 들어가므로 **고장 즉시 5xx 서버 스팬이 크게 나야 유리**하다.
- 이 시나리오는 그 모양이다: 새 파드가 Ready 가 되는 순간부터 api 를 거치는 요청이 전부 502 다(부분 실패 단계가 없다). api 의 서버 스팬은 POST, GET /api/transfers 두 엔드포인트이고, OTel HTTP 서버 스팬은 5xx 면 ERROR 다. 평시 api 업무 거절은 400(잔액 부족)이라 오류율에 들어가지 않는다(VM `apm.agent.otel.java.error_rate{service_name="core-banking-api"}` 7일 99백분위 0, 2026-10-08 17:20 UTC 조회).
- 표본량: 평시 api 표본 요청은 가장 한가한 KST 2~6시에 분당 약 3.1~3.3개(CH MV 7일 시간대별 평균, 같은 조회 시각), 표본 rps 7일 평균 0.079/s. 동반 부하 6rps 중 api 몫(거래 내역 25% + 이체 10%)은 초당 약 2.1건, 기준선 몫까지 합치면 가장 한가한 시간에 초당 약 2.45건, 10% 표본으로 분당 약 15개 ERROR 서버 스팬이다. 첫 판정(고장 2~3분)의 15분 창에서도 api 오류율은 대략 40~55%가 된다(동반 부하 램프업 2분을 감안한 추정, 첫 실행에서 확인).
- 로그: 요청 하나가 재시도 3회와 fallback 으로 ERROR 2~4줄을 남긴다. 가장 한가한 시간에도 api ERROR 로그가 평시 분당 약 0.1~3줄(CH MV `log_error_count` 시간대 평균)에서 분당 수백 줄로 뛴다.
- 묶음 구성 위험: 같은 banking 네임스페이스에서 ledger 대사 불일치 로그 인시던트가 평시 약 30분마다 난다(PG `incidents` 2026-10-08, main_service core-banking-ledger). 이 잡음과 같은 묶음이 되면 판정 입력의 상위 3개 서비스에 api 와 ledger 가 함께 들어갈 수 있으나, api 의 오류율과 로그 급증이 판정의 1차 근거가 된다. 하류 account, transfer 는 고장 중 오히려 api 발 요청이 줄어 오류율이 오르지 않는다.
- 선례: 동반 부하가 붙은 banking 실행은 아직 녹화가 없다(F35-R 후보 대기). 2026-10-01~02 core-banking-api 단독 묶음이 여러 번 promote 되었으나(예 2026-10-02 04:31 첫 이벤트, 04:49 promote, 판정 문장 "오류율 1.9%에서 23.4%") 그날은 판정 적체 시기였고 신호가 400 업무 거절 위주라 이 설계의 근거로 쓰지 않는다. 그래서 동반 부하와 고장 길이(약 14분)로 조건을 확보한다.

## 7. 관측 근거 표 (119 실조회, 2026-10-08 17:05~17:35 UTC)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 결정 증거는 KCM(이벤트 전수, 리소스 스펙 전수)과 로그(전수)다. 트레이스는 보조다.

| 증거 | 119 표와 칸 | 조회 | 평시 결과 |
|---|---|---|---|
| 근본: 파드 DNS 정책 | PG `kcm_resources_history` (kind replicaset, pod, yaml 의 `"dnsPolicy"`, captured_at) | namespace 별 dnsPolicy 값 집계, 전 기간(최소 2026-07-13) | rca-testbed-banking 540건 전부 ClusterFirst, commerce 3,659건과 food 500건도 전부 ClusterFirst. Default 는 kube-system 6건뿐 |
| 근본: 스펙이 실제로 잡히는가 | 같은 표 | F30-R 실행(2026-10-08 14:52 food payment env 롤아웃) | 새 ReplicaSet 을 14:52:30 에 잡았고 yaml 에 바뀐 env 와 `"dnsPolicy":"ClusterFirst"` 가 있음. 새 파드도 dnsPolicy 를 담음. 녹화 범위 `pg-scope.json` 에 kcm_resources_history(captured_at) 포함 |
| 계기: 롤아웃 | CH `kcm_events_local` (object_kind, object_name, reason, body) | namespace='rca-testbed-banking', object_name='testbed-api', 표 보존 시작 2026-08-13 05:28(banking 네임스페이스 첫 이벤트 2026-08-24 03:46) 이후 | testbed-api ScalingReplicaSet 0건, api 파드 Unhealthy 0건. 이벤트 보존 범위 안에서는 롤아웃이 처음 보는 사건이다. 그 전 롤아웃(2026-08-07, 08-11)은 PG kcm_resources_history 에 옛 ReplicaSet 으로 남아 있어, '처음'은 이벤트 보존 범위 안에서만 맞다. 롤아웃 이벤트 모양은 F30-R 실행(ScalingReplicaSet, SuccessfulCreate, Scheduled, Pulled, Created, Started, 46초 뒤 옛 ReplicaSet 축소)으로 확인 |
| 전파: 해석 실패 로그 | CH `lucida_logs_local` body | `match(body, ': testbed-[a-z-]+$')`(호스트 이름으로 끝나는 I/O 오류), 8일 | 0건 |
| 같은 | 같은 표, `log_attributes['exception.type']`, `['exception.stacktrace']` | ILIKE '%UnknownHost%', '%name resolution%', '%Name or service not known%', 8일, 전 서비스 | 0건. AccountClient.java:39, TransferClient.java:41 은 log.error 에 예외 객체 없이 ex.getMessage() 만 넘겨, 고장 중에도 전수 로그에는 'UnknownHost' 가 남지 않는다 |
| 전파: api 평시 ERROR | CH 로그 | core-banking-api ERROR 템플릿, 7일 | 'Insufficient balance' 400 계열 4,798건씩, 다른 시나리오 창의 'Connection refused' 717건, CircuitBreaker OPEN 등. 이번 템플릿(호스트 이름으로 끝남)은 없음 |
| 전파: 트레이스(보조) | CH `otel_traces_local` events_attributes['exception.type'] | 예외 이벤트가 있는 스팬, 보존(2026-10-05 16:00~) | CLIENT 스팬에 java.net.ConnectException, java.net.SocketTimeoutException 이 기록됨(commerce-product 등). api 의 하류 호출 CLIENT 스팬에는 java.net.UnknownHostException 이 붙을 것이다. 트레이스 보존(2026-10-05~) 동안 어느 서비스 스팬에도 이 형식은 0건이고, 예외 형식은 이 10% 표본에만 남는다 |
| 하류 정상(대조) | CH 로그 | account 'Account validation passed'(이체가 account 에 닿을 때마다), transfer 'Transfer COMPLETED ... from=commerce-settlement' | account 하루 약 21,900건(10-06, 10-07), transfer commerce 정산 최근 1일 41,219건. 고장 중 앞의 것만 끊기고 뒤의 것은 이어져야 한다 |
| 골든 시그널(감지) | VM `apm.agent.otel.java.error_rate`, `apm.agent.otel.java.rps` {service_name="core-banking-api"}, CH MV `agg_service_golden_signals` | `quantile_over_time(0.99, …[7d])`, `avg_over_time(…[7d])`, MV 시간대별 평균 | 오류율 99백분위 0, 표본 rps 평균 0.079/s, MV 표본 요청 KST 2~6시 분당 3.1~3.3, 오류 분당 0~0.04, ERROR 로그 분당 0.1~2.8. account 오류율 99백분위 0 |
| 피해 | 동반 부하 k6 live 문서 | 이체 step 5xx 비율 | 평시 0(업무 거절은 400) |

## 8. 감별

- must_support: 정답지 5항목(롤아웃 이벤트와 새 ReplicaSet, 파드 스펙의 dnsPolicy Default, 호스트 이름으로 끝나는 api I/O 오류 로그(전수)와 표본 CLIENT 스팬의 UnknownHostException(보조), api 오류율과 지연 상승, 하류가 다른 호출자에게 정상 응답하고 account 'Account validation passed' 만 끊김, 동반 부하 이체 5xx 와 잔액 조회 정상).
- must_rule_out(정답지): account 나 transfer 장애(F17-R 꼴은 'Connection refused' 와 transfer NotReady, F35-R 꼴은 ORA-28000 과 잔액 조회 실패), 클러스터 DNS 장애(다른 파드의 Service 호출 정상, 해석 실패가 api 에서만), 부하 증가, 코드나 이미지 배포(새 ReplicaSet 이 이미지, env, 프로브 그대로이고 dnsPolicy 한 칸만 다름).
- 부하 증가 경쟁 가설: 동반 부하 6rps 는 유입을 늘린다. 그러나 ① 오류가 시간 초과나 풀 고갈이 아니라 연결 전 이름 해석 실패(호스트 이름뿐인 I/O 오류)이고 ② 같은 부하의 55%를 받는 잔액 조회 경로(nginx → account)는 실패하지 않으며(러너 배제 조건 `account-path-failing`) ③ 하류 account 는 오히려 api 발 요청을 받지 못해 'Account validation passed' 가 끊기고 ④ 실패는 롤아웃 직후 계단식으로 시작한다. banking surge 의 실측 건강 상한은 20rps(surge.js 머리말, 2026-07-15)라 6rps(기준선 최고 4 iter/s 를 더해도 10rps)는 그 아래다.
- contrast_with: F17-R(transfer 프로브 오설정, api 'Connection refused', transfer NotReady), F35-R(Oracle 계정 잠금, 하류 세 서비스 NotReady, 잔액 조회도 실패), F08-P(호출 쪽 설정 배포가 정상 하류 호출을 스스로 끊는 같은 꼴, 바뀐 값이 read-timeout 이라 요청이 하류에 닿은 뒤 끊김).

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

설계 강도 하나로 고정한 평가 모드(`approved-fixed-f37-r`). 강도라 할 만한 값은 없다: 정책은 바꾸거나 되돌리는 것뿐이고, 새 파드가 Ready 가 된 뒤 api 를 거치는 요청은 요청량과 무관하게 전부 실패한다.

- 한 요청의 대기: 재시도는 200ms, 400ms 지수 백오프(`application.yml:39-54`)이고 JDK 음성 캐시가 10초라, 첫 해석이 약 10초 걸려 실패한 뒤의 재시도는 즉시 실패한다. 한 요청은 최대 약 10.6초(약 11초) 기다린다.
- 시간: 패치 → 새 파드 생성, JVM 기동, startupProbe(5초 주기) 통과, readiness(10초 주기) Ready → 옛 파드 종료. F30-R 의 같은 꼴 롤아웃이 46초였으므로 약 1분 뒤 고장이 시작된다(settle 60s). min_hold 15m 이라 판정(성공 3틱)은 패치 약 15.75분 뒤이고 전면 실패는 약 14분 이어진다. cleanup 은 정책을 되돌리고 새 파드가 available 이 될 때까지(보통 약 1분, 상한 180초) 기다린다. 그 1분 동안에도 실패가 이어진다.
- 시간대: 피해는 요청량과 무관하다. 가장 한가한 KST 2~6시(기준선 1 iter/s)에도 동반 부하가 api 몫 초당 약 2.1건을 더해, 러너 판정 표본(이체 step 30초 창 약 18건)과 119 표본 스팬(분당 약 15개)이 시간대에 좌우되지 않는다.
- 스레드: 해석이 막히는 동안 요청이 최대 약 10초 기다린다. 동시 요청은 초당 약 2.45건 × 10초 ≈ 25개로 Tomcat 기본 200 스레드에 한참 못 미친다. 서킷이 열리면 대부분 즉시 502 다.
- 서킷브레이커: 열리면 api 는 바로 502 로 답하고, 반열림 시도도 해석이 실패해 다시 열린다. 피해를 숨기지 않는다.
- liveness, readiness 는 `/actuator/health`(DB 없음)라 고장 파드는 재시작되지 않는다.

| 항목 | 값 |
|---|---|
| level | `approved-fixed-f37-r`, min_hold 15m, settle 60s, timeout 20m |
| max_injection_duration | 25m |
| companion | `load.north_south` core-banking surge.js, target_rps 6, entry 30082, ramp 2m + hold 21m + ramp_down 15s |
| success | 동반 부하 이체 5xx 비율(`loadgen.food_create_status_rate`, 선택자 business.5xx.rate) ≥ 0.5, 3틱 연속 |
| must_rule_out | 동반 부하 achieved_rps < 1.5(부하 끊김), 동반 부하 잔액 조회 실패율 ≥ 0.2(account 쪽 장애), transfer 파드 NotReady(하류 장애), 2틱 |
| abort | entry_status(domain core-banking, 이체 단계 nginx → api) == 0, 2틱. api 와 nginx 는 살아서 502 로 답하므로 이 시나리오로는 0 이 나오지 않는다 |
| recovery | target_health 200, api 파드 Ready, transfer 파드 Ready, 기준선 잔액 조회 실패율 < 0.05, 2틱, 10m |
| cleanup | dnsPolicy ClusterFirst 로 패치 뒤 롤아웃 available 확인(180초), 동반 부하 종료, 10m |

새 관측 쿼리와 러너 변경은 없다(`kubernetes.pod_ready` 의 testbed-api, testbed-transfer 는 러너 허용 목록에 이미 있다). 새 실행기 `k8s.dns`(profiles/k8s_dns_executor.py)만 더했다. 실행기는 파드 템플릿의 dnsPolicy 한 칸만 JSON patch 로 바꾸고, dnsConfig 가 붙은 Deployment 는 건드리지 않으며, 상태 파일을 Deployment 이름으로 짓는다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Intercom 2024-11-05, 공식 링크)와 요소별 대응표가 있고, 기전("배포된 resolver 설정이 공개 이름만 알고 내부 이름을 몰라 그 설정을 받은 호스트가 내부 의존 대상에 닿지 못함, health 가 그 이름을 보지 않는 호스트만 서비스에 들어감")이 같다 (원칙 1, §2)
- [x] 장부를 갱신했다: 묶음 M(0), 정답 위치 은행 API(0), 결제 경로 25%와 무관, 서비스 은행 9(음식배달 8 을 고르지 않은 이유는 §3), 어느 축도 20%에 닿지 않는다 (원칙 2, §3)
- [x] 근본 원인 위치: Deployment 파드 템플릿(`20-api-service.yaml:17-25`), 하류 주소(`02-configmaps.yaml:14-15`), 해석 실패가 로그가 되는 자리(`AccountClient.java:28-48`, `TransferClient.java:32-60`), 노드 resolver(tb-w2 kubelet resolvConf)가 확인됐다 (G1, §4)
- [x] 근본 원인의 흔적: PG `kcm_resources_history` 의 dnsPolicy(평시 banking 540건 전부 ClusterFirst, 롤아웃 때 새 ReplicaSet 스펙을 실제로 잡음) (원칙 3, §7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: KCM 이벤트와 리소스 스펙(전수), 로그(전수). 로그는 예외 형식 없이 호스트 이름뿐인 I/O 오류를 남기고, 트레이스의 UnknownHostException 은 보조 (원칙 3)
- [x] 계기의 흔적: testbed-api 롤아웃 KCM 이벤트(2026-08-13 에 시작된 이벤트 보존 안에서 처음)와 새 ReplicaSet 스펙. 인공 지연을 쓰지 않는다. 지연이 있다면 실제 resolver 가 응답하지 않아 glibc 가 시간 초과로 포기하는 시간이다 (원칙 4)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다: 롤아웃 직후 api 만의 이름 해석 실패와 스펙의 한 칸 차이 (원칙 5)
- [x] 정답지 세 칸: 근본 `core-banking-api`(config), 계기 비움, 부분 점수 coredns, kube-dns (원칙 6, §5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다. 골든 시그널이 오를 근거(고장 즉시 api 서버 스팬 전부 502, 동반 부하로 표본 분당 약 15개, 고장 약 14분)를 설계로 확보했고 판정 입력의 한계(벽시계 최근 15분 대 3시간, 멤버 상위 3개 서비스, ledger 잡음 인시던트)를 적었다 (원칙 7, §6)
- [x] 주입 도구가 시나리오 id 를 남기지 않는다: 실행기는 네임스페이스, Deployment, 정책 값만 kubectl 로 넘기고 상태 파일도 Deployment 이름이다. 클러스터에 남는 것은 dnsPolicy 값과 롤아웃뿐이다(단위 테스트로 고정) (원칙 8)
- [x] 피해 계산: 요청량과 무관한 전면 실패, 서킷브레이커는 피해를 숨기지 않음, 스레드 여유, 동반 부하 6rps 는 실측 건강 상한 20rps 아래, 부하 증가 경쟁 가설은 관측으로 배제 (원칙 9, §8, §9)

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- 새 api 파드가 Ready 가 되어 옛 파드를 대체하는지(롤아웃이 멈추면 피해가 없다), 패치에서 고장 시작까지의 시간.
- api 로그가 실제로 `I/O error on POST request for "http://testbed-account:8081/api/accounts/transfer": testbed-account` 꼴로 남는지, 해석 대기가 약 10초이고 한 요청이 최대 약 11초인지(api p95). 다르면 정답지의 결정 증거 문구를 녹화본에 맞게 고친다.
- 표본 api CLIENT 스팬의 exception.type 이 java.net.UnknownHostException 인지(로그에는 예외 형식이 남지 않으므로 형식 확인은 스팬으로만 한다).
- KCM 이 새 ReplicaSet 과 파드를 `"dnsPolicy":"Default"` 로 잡는지.
- 판정이 promote 해 인시던트가 생기는지. 동반 부하가 붙은 banking 녹화 선례가 아직 없으므로 `incidents.promoted_at`(last_reviewed_at 아님)과 promote 된 묶음의 멤버 상위 3개 서비스(판정 입력)에 core-banking-api 가 들어갔는지, ledger 잡음 인시던트와 섞이는지 반드시 기록한다. 인시던트가 안 생기면 먼저 판정 지연(promoted_at 또는 판정 updated_at − 묶음 first_event_at)을 본다.
- 잔액 조회가 고장 내내 정상인지(배제 조건이 잘못 발화하지 않는지), cleanup 뒤 1분 안에 회복되는지.
