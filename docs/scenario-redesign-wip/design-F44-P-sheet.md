---
title: F44-P 설계 시트 — banking 워커 방화벽 허용 목록이 commerce 정산 호출자를 빠뜨려 checkout 이 실패함
status: Draft
owner: project
last_reviewed: 2026-10-09
tags:
  - scenario
  - design
  - network
summary: 운영자가 은행 워커 tb-w2 의 호스트 방화벽에서 transfer API 포트(8082)를 banking 파드와 진입 망에서만 받도록 좁히며 다른 노드 tb-w1 의 commerce payment(정산 이체 호출자)를 빠뜨려, payment 의 keep-alive 연결이 닫힌 뒤 commerce checkout 이 전량 502 가 되고 banking 자신은 멀쩡한 시나리오(Central 1 2025-04-09 재구성)의 설계 근거.
---

# F44-P 설계 시트

- id `F44-P`, slug `f44-p-banking-node-firewall-omits-commerce-settlement`, F44 사례군의 P(일부 비슷)
- 상태: 후보(ready + `stage: candidate`), 설계 강도 하나로 고정한 evaluation 모드, 녹화 대기 큐에 추가
- 주입 수단: 기존 실행기 `host.firewall`(`scripts/scenarios/profiles/host_firewall_executor.py`)에 다른 노드 호출자 계약을 더함 + `load.north_south` 동반 부하

## 1. 요약과 비유

commerce 의 checkout 은 결제마다 core-banking 이체로 정산을 동기 처리한다. 정산을 부르는 commerce payment 는 워커 tb-w1 에,
받는 banking transfer 는 워커 tb-w2 에 있어 요청은 flannel overlay 를 건너 payment 자신의 파드 주소(10.244.3.x)를 단 채 tb-w2 에 닿는다.
운영자가 "그동안 아무 데서나 받던" transfer API 포트 8082 를 좁혀, banking 자신의 파드 대역(10.244.1.0/24)과 손님 요청이 들어오는 진입 망(제어 노드의
NodePort SNAT 주소, 호스트 망)만 받게 한다. 다른 노드에서 들어오는 상대 기관(commerce)의 정산 연결은 목록에 없다.
이미 맺어진 연결은 그대로 두는 규칙이라 payment 가 열어 둔 keep-alive 연결이 닫힐 때까지는 checkout 이 되고, 그 뒤로는 정산 이체의 새 연결 SYN 이
버려져 모든 checkout 이 502 다. banking 자신은 멀쩡하고 자기 손님의 잔액, 내역, 이체와 NodePort 직행 이체에 계속 답한다.

비유: 은행이 금고실 출입 통제를 강화하며 "본점 직원과 정문으로 들어온 손님만" 받게 했는데, 매일 정산하러 오던 거래처 직원(commerce)이 명단에서 빠졌다.
은행 창구는 평소처럼 돌아가고, 거래처 쪽에서만 결제가 끝나지 않는다.

## 2. 원본 사례

- **Central 1, 2025-04-09**(사후 보고 게시 2025-05-06), 상태 페이지 사고 INC198965 "Brief Interac e-Transfer outage",
  [공식](https://status.central1.com/incidents/nr0l5bvnmwpn). 2026-10-09 WebFetch 로 원문 확인.
  - 12:50~12:54 PT, e-Transfer 서비스 중단. MemberDirect/Forge 디지털 뱅킹과 API 서비스 사용자가 이체를 보내거나 받을 때 오류.
  - 원인: 방화벽 오류가 UCP 시스템과 Interac 사이 연결을 잠깐 끊음. UCP 는 밴쿠버 망 연결로 Interac 과 통신하지 못했지만 응답은 유지했고, Interac 쪽에는 문제가 없었음.
  - 완화: 방화벽 규칙을 바로잡음, 서비스는 자동 회복. 후속: 방화벽 규칙 변경 안전장치와 네트워크 거버넌스 강화, 네트워크 계층 로깅 강화.
- 보조(같은 꼴, 원본 아님): Snowflake 2025-11-20 INC0147071(Azure Australia East), 방화벽 규칙 설정 변경이 Cloud Services 와 Storage 계층 사이 통신을 끊어 요청 처리 불가
  ([공식](https://status.snowflake.com/incidents/z78t3ckmgrc6), 예비 원인만 확인).
- `docs/ref-real-world-incidents.md` M15 에 Central 1 행을 추가했다(출처가 말한 사실만).

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 방화벽 규칙 오류(후속 조치가 '방화벽 규칙 변경 안전장치') | 지금까지 모두 받던 transfer API 포트(8082)에 tb-w2 호스트 방화벽 허용 목록을 넣음(운영자가 노드에서 iptables 명령) |
| 원인이 된 결함 | 규칙이 결제 플랫폼 UCP 와 상대 망 Interac 사이 연결을 막음 | 허용 목록(banking 파드 대역 10.244.1.0/24, 진입 노드 대역 10.244.0.0/24, 호스트 망 둘)이 상대 도메인 호출자 commerce payment 의 파드 대역(10.244.3.0/24)을 빠뜨림 |
| 전파 경로 | UCP 가 Interac 과 통신하지 못함 | 규칙은 conntrack NEW 만 거른다. payment 의 keep-alive 연결이 닫힐 때까지 정산 성공, 그 뒤 새 연결 SYN 이 버려져 connect-timeout 3초, payment 502, order 결제 재시도와 서킷 열림 |
| 사용자 증상 | e-Transfer 를 보내거나 받을 때 오류 | 모든 commerce checkout 이 502(정산 이체 실패). 둘러보기, 장바구니는 정상 |
| 원인 쪽 상태 | UCP 는 응답을 유지, Interac 쪽 문제 없음 | transfer 는 Ready, banking 자신의 이체, 조회와 NodePort 직행 이체는 정상 |
| 탐지된 경로 | 기재 없음 | commerce order, gateway 오류율과 order ERROR 로그 급증(관제), 러너의 checkout 5xx 비율 |
| 완화와 복구 | 방화벽 규칙을 바로잡음, 자동 회복 | cleanup 이 점프 규칙과 체인을 지움. 다음 새 연결부터 통하고 order 결제 서킷이 반열림 성공으로 닫힘 |

기전 확인: 원인(방화벽 규칙이 상대 기관과의 연결을 막음)에서 증상(그 연결을 쓰는 결제만 실패, 원인 쪽 플랫폼은 응답 유지)까지의 고리가 원본과 같다.
원본은 규칙 오류의 세부(어느 쪽 출발지가 빠졌는지, 연결 방향)를 적지 않았다. 재구성의 '허용 목록이 상대 기관 대역을 빠뜨림'은 원본이 말한 범위(방화벽 규칙 오류, 후속 조치가 규칙 변경 안전장치) 안의 구체화다.
keep-alive 연결이 닫힐 때까지 무사한 지연(약 1분)은 원본에 없는 테스트베드 쪽 성질이고, 원본의 4분 중단과 견주어 증상의 꼴을 바꾸지 않는다.

## 3. 숫자 근거 (2026-10-09, `scenario-stats.py`, 정식 + 후보, 이 후보 전 51 종)

- 서비스: commerce 26, **core-banking 14 → 15**, food-delivery 11(가장 적음). food 를 고르지 못한 이유는 §11(0 부품 kafka, notify 는 사용자 증상 없음, order 는 생성 입구라 abort, tb-w3 인프라 층은 쓰였거나 막힘).
- 묶음: A 7, B 7, C 6, D 7, E 2, F 3, G 6, H 2, I 1, J 4, **K 1 → 2**, L 3, M 1, O 1, N 0. 20% 상한(52 중 10.4)에 닿는 묶음 없음. N 은 0 이지만 모든 동기 호출의 서킷브레이커로 막혀 있다(F14-R, rejected 45).
  주 묶음 K(서비스 사이 통신이 끊김), 계기가 운영자의 방화벽 설정 변경이라 K+G. J 는 같은 날 넷(F41-R, F42-R, F43-R 과 F17-H)이 'k8s.image 롤아웃한 그 서비스가 정답'인 꼴로 몰려 이번에는 피했다(F43-R 평가 권고).
- 정답 위치: **노드, 디스크 4 → 5**(52 중 9.6%). 결제 경로 합계 10/52(19.2%)이고 이 후보의 정답은 결제 쪽이 아니다(commerce payment 는 증상, 부분 점수).
- 부품 지도: banking 에서 0 인 부품은 kafka, nginx(OTel 이 없어 119 에 흔적 없음). 인프라 층 "네트워크" 정답은 F44-R 하나다.
- 왜 이 후보인가: 은행은 두 번째로 적은 서비스이고 앱 부품(account 5, transfer 5)이 몰려 있어 인프라 층 정답이 맞다. 도메인 횡단 경로(commerce → banking)의
  연결 단계 장애는 아직 없다: 지금 있는 commerce 정산 실패(F17-R, F17-H, F01-P, F35-R, F42-R)는 모두 banking 자신도 함께 실패한다.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 계기: tb-w2(192.168.122.11)에서 `sudo iptables -N TRANSFER-API-IN`, `-A TRANSFER-API-IN -s <허용 출발지> -m comment --comment "transfer-api: allow banking pods and entry networks" -j RETURN` 4개,
   `-A TRANSFER-API-IN -m limit --limit 6/min -j LOG --log-prefix "[FW BLOCK] "`, `-A TRANSFER-API-IN -j DROP`,
   `-I FORWARD 1 -d 10.244.1.0/24 -p tcp --dport 8082 -m conntrack --ctstate NEW -m comment --comment ... -j TRANSFER-API-IN`.
2. 노드 망: tb-w2 는 br_netfilter 적재, `net.bridge.bridge-nf-call-iptables=1`(2026-10-09 실측). tb-w1 에서 온 VXLAN 패킷은 flannel.1 에서 풀려 cni0 로 라우팅되며 FORWARD 를 지난다.
   kube-proxy 의 ClusterIP DNAT 는 출발 노드 tb-w1 에서 이미 끝나 tb-w2 의 FORWARD 에서는 목적지가 transfer 파드 IP 이고 출발지는 payment 파드 IP 그대로다(SNAT 없음).
3. 배치: `commerce/k8s/23-payment-service.yaml:18-19`(nodeSelector tb-w1), `core-banking/k8s/22-transfer-service.yaml:27-28`(nodeSelector tb-w2). 노드 podCIDR: tb-cp 10.244.0.0/24, tb-w1 10.244.3.0/24, tb-w2 10.244.1.0/24, tb-w3 10.244.2.0/24.
   2026-10-09 transfer 파드 10.244.1.220, payment 10.244.3.81. tb-w2 conntrack 의 8082 상대: 10.244.0.0(tb-cp NodePort 30282 로 들어온 요청, 13), 10.244.1.1(kubelet 프로브, 20), 10.244.3.81(commerce payment, 2).
   같은 노드의 api, account 는 ClusterIP 로 와서 conntrack 원래 목적지가 Service 주소로 남지만 FORWARD 에서는 DNAT 뒤라 10.244.1.x → 10.244.1.220 이다(허용).
4. commerce 의 호출: `commerce/k8s/02-configmaps.yaml:43-46`(BANKING_TRANSFER_URL 클러스터 Service FQDN), `payment-service/.../PaymentService.java:53-90`(processPayment, @Transactional, 외부 결제 뒤 정산 이체 동기 호출),
   `BankingTransferClient.java:37-59`(재시도, 서킷 없음, RestClientException → 502 'core-banking transfer call failed: ...'), `RestClientConfig.java:35-44`(HttpURLConnection, connect-timeout 3s),
   `order-service/.../OrderService.java:189-199`(결제 실패 시 ERROR 'Checkout payment failed, releasing reserved stock', 재고 해제, 502), `order-service/src/main/resources/application.yml:91-97, 134-139`(paymentClient 서킷 5초 열림, 재시도 2회, read-timeout 15s > 3s 라 payment 의 오류 문장이 그대로 전달).
5. 결과: SYN 무응답 → 3초 뒤 payment 502 `core-banking transfer call failed: I/O error on POST request for "http://testbed-transfer.rca-testbed-banking.svc.cluster.local:8082/api/transfers": Connect timed out` → order 재시도 → 서킷 열림 → checkout 502.
6. 영향받지 않는 경로: banking 진입점(tb-cp NodePort 30082 → nginx → api, account → transfer)은 tb-w2 안 파드끼리라 허용, commerce 기준선의 2% 직행 이체 여정(`commerce/loadgen/script.js:235-249`, `BANKING_TRANSFER_URL=http://192.168.122.77:30282`)은 tb-cp SNAT 주소라 허용.

## 5. 원인 규정 (원칙 5, 6)

- 근본(`root_cause.target_id`): `tb-w2`(target_kind `node`). 결함은 은행 노드의 방화벽 설정이다. 고쳐야 재발이 막히는 곳이 그 규칙이다.
- 계기(`trigger_target_id`): 비움. 계기도 같은 곳(tb-w2 방화벽 규칙 변경)이다.
- 원칙 6: payment 의 정산 요청과 transfer 의 처리는 늘 하던 그대로 정당하다. 잘못된 것은 그 사이를 막은 규칙이다. 원본도 방화벽 오류를 원인으로 적었다(같은 층위).
- 부분 점수(`scoring.partial`): `core-banking-transfer`(연결이 닿지 않는 목적지만 본 답), `commerce-payment`(증상 서비스만 본 답).
- 채점 입도 `node`, accept `tb-w2`(F44-R, F10-H, F15-P 와 같은 표기). target_id 는 partial 에 없다.
- 원칙 5: 정답은 "tb-w2 의 방화벽 규칙 변경이 commerce 에서 transfer 로 가는 새 연결을 막았다"이고, 관제 데이터(노드 syslog 의 iptables 명령과 커널 차단 로그, commerce 의 연결 시간 초과 로그, banking 정상)로 낼 수 있다.
  왜 그 대역을 빠뜨렸는지(변경 검토 절차)나 keep-alive 세부는 요구하지 않는다.

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 | 기대 |
|---|---|---|
| 감지(lucida-next) | commerce order, gateway 오류율(APM error_rate), order ERROR 로그 새 꼴('Checkout payment failed ... Connect timed out') 급증, checkout 502 | commerce 정산 실패로 checkout 이 502 인 겉모양은 F17-R(정식, 정상 녹화 있음)과 같다. 별도 업무 규칙 없이 인시던트가 나야 한다 |
| 피해 판정(러너) | 동반 부하 k6 의 checkout 5xx 비율 ≥ 0.5, 2xx 비율 < 0.3 (3틱) | 평시 checkout 5xx 는 0 근처(F43-R 과 같은 관측과 임계) |
| RCA | §7 의 노드 syslog(sudo iptables, kernel '[FW BLOCK]'), commerce order 로그, banking 정상, KCM 무변화 | 근본 위치 tb-w2 까지 간다 |

## 7. 관측 근거 (119 실조회, 2026-10-09 12:40~12:55 UTC)

| 증거 | 표, 칸 | 조회와 결과(평시) | 표본 여부 |
|---|---|---|---|
| 계기: 노드의 iptables 명령 | CH `lucida.syslog_local` (`hostname`, `app_name`, `message`) | `SELECT hostname, min(received_at), max(received_at), count() FROM lucida.syslog_local WHERE app_name='sudo' AND message LIKE '%iptables%' GROUP BY hostname` → tb-w2 는 설계 중 실행한 `COMMAND=/usr/sbin/iptables -S` 4건(2026-10-09 12:44~12:49)뿐, 보존 시작(2026-08-13) 뒤 규칙 변경 명령 0건. 같은 조회로 설계 중의 `conntrack -L` 이 몇 초 안에 `hostname tb-w2, app_name sudo, source_ip 192.168.200.137` 로 보임(sudo 가 인자 전체를 남김). registered=0 이라 자원 귀속은 hostname 글자로 한다(F44-R 과 같음) | 전수 |
| 계기와 전파: 버려진 연결 | 같은 표 `app_name='kernel'` | tb-w2 커널 로그는 수집된다(30일 586건, 예 `cni0: port 9(...) entered disabled state`). `DPT=` 를 담은 줄은 0건. 장애 시 `[FW BLOCK] IN=flannel.1 OUT=cni0 ... SRC=10.244.3.x DST=10.244.1.x ... DPT=8082 ... SYN` 이 분당 최대 6건. xt_LOG 는 아직 적재되지 않아(lsmod) 규칙 삽입 때 자동 적재되는지 첫 실행에서 확인한다(F44-R 과 같은 미확인 항목, 없으면 must_support 2번을 뺀다) | 전수 |
| 전파: commerce 정산 연결 시간 초과 | CH `lucida.lucida_logs_local` | `SELECT service_name, countIf(body LIKE '%Connect timed out%'), countIf(body LIKE '%core-banking transfer call failed%') FROM lucida.lucida_logs_local WHERE timestamp > now() - INTERVAL 30 DAY AND service_name LIKE 'commerce-%' GROUP BY 1` → commerce-order 0 / 170, commerce-payment 0 / 4(보존 2026-10-02~). 170 건은 앞선 banking 쪽 장애의 `... Connection refused`. gateway 와 product 의 'Connect timed out' 은 2026-10-06 하루의 다른 경로(testbed-order, product 하류) | 전수 |
| 피해와 감별: 정산 이체 로그 | 같은 표 `service_name='core-banking-transfer'` | 최근 1시간 `Transfer COMPLETED ... from=commerce-settlement` 2,152건, `from=ACC-` 1,345건. 장애 시 앞의 것만 0 으로, 뒤의 것(banking 자신의 이체와 commerce 기준선 직행 이체)은 유지 | 전수 |
| 피해: checkout 로그 | 같은 표 `service_name IN ('commerce-payment','commerce-order')` | 최근 1시간 `Banking transfer for order N: COMPLETED` 2,157, `Cleared cart after checkout` 2,156. 장애 시 0 근처 | 전수 |
| 감별: banking 자신은 멀쩡 | 러너 `loadgen.read_step_status_rate`(domain core-banking), CH 로그 | 상주 기준선의 잔액 조회 실패율 평시 0. transfer, api, account 의 오류 로그가 늘지 않아야 한다 | 전수(로그) |
| 감별: 쿠버네티스 무변화 | CH `lucida.kcm_events_local` | 주입은 쿠버네티스 객체를 바꾸지 않는다. 장애 구간에 rca-testbed-banking, rca-testbed-commerce 의 ScalingReplicaSet, Killing, Unhealthy 가 없어야 한다 | 전수 |
| 보조: 스팬 | CH `lucida.otel_traces_local` | payment → transfer CLIENT 스팬 오류, POST /api/orders/checkout 502. 10% 표본이라 보조로만 | 10% 표본 |

핵심 증거(노드 auth 로그의 iptables 명령, 커널 차단 로그, commerce order 의 연결 시간 초과 로그, 정산 이체 로그 소멸과 banking 자신의 이체 지속, 쿠버네티스 무변화)는 모두 전수 수집 데이터다.
트레이스와 APM 지표는 보조다. 커널 줄의 SRC 대역(10.244.3.0/24)이 tb-w1 이라는 것은 119 에 노드 podCIDR 표가 없어, "commerce 만 연결 시간 초과를 남긴다"와 시각으로 잇는다.
정답 위치(tb-w2)와 막힌 포트(8082 = Service testbed-transfer 의 targetPort)는 명령 자체에 있다.

## 8. 감별

- must_support: 정답지 `must_support` 5개(노드 auth 로그 iptables 명령, 커널 '[FW BLOCK] ... DPT=8082', commerce order 'Connect timed out' 과 checkout 502, banking 정상과 'from=commerce-settlement' 소멸, 쿠버네티스와 DB 무변화).
- must_rule_out: transfer 다운(F17-R, F17-H), banking DB 장애(F01-P, F35-R, F42-R), payment 정산 주소 오설정(F39-R 꼴), DNS(F37-R 꼴), commerce 자체 장애(F05-R, F05-H, F25-H, 외부 결제), 노드 자원 고갈(F15-P).
- contrast_with: F44-R(같은 원인 꼴, food 노드 안), F17-R·F17-H(transfer 엔드포인트 비움), F39-R(호출자 설정 배포로 다른 호스트), F35-R·F42-R(banking DB 쪽 원인).
- 러너 배제 조건: 동반 부하 미전달(achieved_rps < 1), transfer 파드 NotReady, payment 파드 NotReady, PostgreSQL NotReady, banking 기준선 잔액 조회 실패율 ≥ 0.2.

## 9. 러너 판정과 강도, 부하 계산 (원칙 9)

- 강도: 하나로 고정(approved-fixed-f44-p). 규칙은 켜거나 끄는 것뿐이라 사다리가 없다.
- 피해 계산: 규칙 뒤 payment 의 기존 연결은 transfer Tomcat 이 연결당 100 요청 뒤 닫거나 JDK keep-alive 캐시가 쉬는 연결을 닫을 때까지 통한다. 기준선 checkout 초당 약 0.6건(1시간 2,157건) + 동반 부하
  3rps × 50% = 1.5건이면 정산 호출이 초당 약 2건이라 길어야 1분 안팎이다. 그 뒤 새 연결은 모두 3초 connect-timeout. payment 의 banking 호출에는 재시도, 서킷이 없어 시도마다 3초 뒤 502 이고,
  order paymentClient(재시도 2회, 서킷 10회 중 50% 면 5초 열림)는 checkout 몇 건 만에 서킷을 열어 열린 동안은 fallback 502, 반열림 3건은 약 6초 뒤 502 다. 따라서 연결이 닫힌 뒤 checkout 성공은 0 이다.
- 입구 보호: commerce 진입점(nginx → gateway)은 규칙과 무관하다. payment 의 @Transactional 이 시도마다 PostgreSQL 연결을 약 3초 쥐지만 서킷이 열린 동안에는 호출이 없어 동시 보유는 몇 개다.
  banking 진입점과 transfer 는 허용 목록 안이다.
- 부하: `load.north_south` commerce surge.js 3rps(F43-R, F33-P 와 같은 값, 상한 180 의 2%), ramp 2m, hold 21m. 한가한 시간에도 checkout 표본을 만든다.
- 판정: success = checkout_5xx_rate ≥ 0.5 이고 checkout_2xx_rate < 0.3 이 3틱. min_hold 15m, level timeout 20m, max_injection_duration 25m.
- cleanup: 상태 파일이 없어도 규칙을 읽어 FORWARD 점프 삭제, 체인 비우고 삭제, `iptables -S` 로 둘 다 없음 확인 후 상태 파일 삭제. recovery: 상태 파일과 규칙이 없고 transfer, payment 파드 Ready, 기준선 checkout 실패율 < 0.1.
- 안전: 규칙은 tb-w2 파드 대역의 8082 로 가는 **전달 트래픽의 새 연결**만 맞춘다. 정책(-P), INPUT, OUTPUT, 다른 체인은 건드리지 않아 호스트 망 SSH(복구 경로), kubelet, Oracle 1521, 다른 포트는 영향이 없다.
- 실행기 변경: F44-R 계약은 그대로 두고, 호출자 네임스페이스, 노드, 파드 대역을 계약에서 따로 받을 수 있게 했다(빠지면 지켜지는 쪽과 같아 F44-R 동작 그대로).
  cleanup 이 상태 파일 없이도 노드의 규칙을 읽어 지우게 고쳤다(F44-R 실행 보고서의 권고: 러너 컨테이너가 주입 중 다시 만들어지면 상태 파일이 사라져 규칙이 남던 문제).
  2026-10-09 109 에서 두 계약의 preflight 를 읽기 전용으로 돌려 둘 다 통과, 허용 목록에 10.244.3.0/24 를 넣은 변형은 실패함을 확인했다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Central 1 2025-04-09, 링크)와 요소별 대응표가 있고 기전이 같다(§2) (원칙 1)
- [x] 분류 장부를 갱신했다(§4-1 행, §6 기록). 묶음 K 1→2(4%), 정답 위치 노드, 디스크 4→5(9.6%), 결제 경로 10/52(19.2%, 이 후보는 무관), banking 14→15. 어느 축도 20% 에 닿지 않는다(§3) (원칙 2)
- [x] 근본 원인 위치가 인프라 지점(tb-w2 iptables FORWARD, br_netfilter, podCIDR, conntrack 상대)과 코드 위치로 확인됐다(§4) (G1)
- [x] 근본 원인의 흔적이 119 실데이터에서 조회됐다: syslog_local 의 tb-w2 sudo 명령(hostname, app_name, message)과 kernel 줄 수집(§7) (원칙 3)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: syslog, 로그, KCM 이벤트는 전수(§7) (원칙 3)
- [x] 계기의 흔적이 조회됐다: 노드 auth 로그에 iptables 명령이 인자째 남는다. 인공 지연 없음(§7) (원칙 4)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다(§5) (원칙 5)
- [x] 정답지 세 칸이 원칙 6대로 채워졌다(§5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다(§6) (원칙 7)
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: 체인 이름 TRANSFER-API-IN, 주석 'transfer-api: allow banking pods and entry networks', 로그 접두어 '[FW BLOCK] ', 상태 파일 `tb-w2-TRANSFER-API-IN-firewall`. 실행기 테스트가 인자에 id 가 없음을 확인한다. k6 동반 부하 태그는 기존 시나리오와 같은 방식 (원칙 8)
- [x] 부하 상한과 서킷브레이커를 넣은 피해 계산이 있다(§9) (원칙 9)

## 11. 후보 목록 (반복 5, 2026-10-09)

부품 지도의 0 인 부품(commerce kafka, notification, gateway, nginx / banking kafka, nginx / food kafka, order, notify)과 인프라 층에서 출발했다.
기전은 `ref-real-world-incidents.md` 의 M 번호 중 아직 시나리오가 없는 것(M5 고정 한도, M9 독 메시지, M17 시간)과 원인 분포 상위(설정 배포)를 먼저 봤다. 숫자 순(0 묶음, 0 정답 위치, 적은 서비스)으로 세웠다.

| 순위 | 후보 | 원본 사례 | 부품, 층 | 판정 |
|---|---|---|---|---|
| 1 | commerce Kafka 토픽 max.message.bytes 하향으로 outbox 발행이 RecordTooLarge 로 멈춤 (메시지 브로커 0) | 공식 사후 보고 미확인(M5) | commerce kafka, 앱 | 버림: 원칙 1, 세 도메인 모두 outbox 비동기라 사용자 증상 없음(원칙 7, rejected 27, 64 와 같은 벽) |
| 2 | food notify, commerce notification 소비자 독 메시지 크래시 (알림 서비스 0) | incident.io 2022 블로그(미확인), PostHog 2026-07-23(2차) | notify, 앱 | 버림: 소비자가 사용자 경로 밖이라 조용한 장애(원칙 7), 원본 2차 출처(원칙 1) |
| 3 | commerce order 의 PaymentEventConsumer 독 메시지 | 위와 같음 | commerce order, 앱 | 버림: 원인 위치가 결제 이벤트 생산자(결제 경로 19.6%), 상태 보정만 늦어 사용자 증상 없음(원칙 7) |
| 4 | food restaurant 검색 경로만 깨지는 릴리스 (가게 서비스 0) | 공식 사례 없음 | restaurant, 앱(J) | 버림: 원칙 1, restaurants 23행이라 질의 비용으로는 피해가 안 나고, J 가 같은 날 넷(롤아웃한 그 서비스가 정답인 꼴)이라 지름길 위험(F43-R 평가 권고) |
| 5 | **banking tb-w2 방화벽 허용 목록이 commerce 파드 대역을 빠뜨려 정산 이체 새 연결 드롭** | Central 1 2025-04-09 공식(보조 Snowflake 2025-11-20) | tb-w2 노드 네트워크, 인프라 | **채택**(K 1, 노드 4, banking 14, 도메인 횡단 연결 단계 장애 0) |
| 6 | banking Oracle 프로파일 호출당 자원 한도(LOGICAL_READS_PER_CALL)를 앱 계정에 걸어 이체 내역만 ORA-02395 | 공식 사례 없음(웹 검색 3회: GitLab 이슈 #2216 은 제안일 뿐, 나머지는 블로그와 원인이 다른 사고) | Oracle, DB | 버림: 원칙 1. 기전은 성립(내역 count 약 9.8만 LR, 변경은 새 세션부터라 풀 수명 10분에 걸쳐 번짐) |
| 7 | food MySQL 전역 sql_mode 변경으로 일부 질의 거절 | 공식 사례 없음 | food MySQL, DB | 버림: 원칙 1, DB 정답 쏠림(DB 계열 27.7%) |
| 8 | food order 설정 배포 spring.transaction.default-timeout 하향 (food order 부품 0) | 공식 사례 없음 | food order, 앱(G) | 버림: 원칙 9, createOrder 가 1초(최소 단위) 안에 끝나 끊길 트랜잭션이 없음, 원칙 1 |
| 9 | tb-w2 kubelet 정지로 노드 NotReady, 엔드포인트 제거 | Datadog 2023-03-08(rejected 38 원본) | tb-w2 노드, 인프라 | 버림: 컨트롤러 필수 중단 조건(banking nginx 도 tb-w2 라 입구 0), 원본 중복 |
| 10 | food order 가상 스레드 설정 배포로 캐리어 스레드 고정(pinning) 교착 | Netflix 2024 기술 블로그 | food order, 앱(G) | 버림: 앱이 Java 17 이라 가상 스레드가 없음 |

서로 다른 부품 9개(commerce kafka, notify, commerce order, restaurant, tb-w2 노드 네트워크, Oracle, food MySQL, food order, kubelet), 앱과 인프라 두 층, DB 정답 후보 2개(6, 7).
