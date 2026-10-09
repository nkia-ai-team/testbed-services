---
title: F44-R 설계 시트 — food 워커 방화벽 허용 목록이 order 를 빠뜨려 restaurant 로의 새 연결이 버려짐
status: Draft
owner: project
last_reviewed: 2026-10-09
tags:
  - scenario
  - design
  - network
summary: 운영자가 워커 tb-w3 의 호스트 방화벽에서 restaurant API 포트(8081)를 노드와 진입 망에서만 받도록 좁히며 같은 노드의 order-service 파드 대역을 빠뜨려, order 의 keep-alive 연결이 닫힌 뒤 주문이 전량 502 가 되는 시나리오(Harness 2024-09-01 재구성)의 설계 근거.
---

# F44-R 설계 시트

- id `F44-R`, slug `f44-r-food-node-firewall-allowlist-omits-order`, 새 사례군(F44)의 첫 시나리오
- 상태: 후보(ready + `stage: candidate`), 설계 강도 하나로 고정한 evaluation 모드, 녹화 대기 큐에 추가
- 주입 수단: 새 실행기 `host.firewall`(`scripts/scenarios/profiles/host_firewall_executor.py`) + `load.north_south` 동반 부하

## 1. 요약과 비유

food 서비스는 모두 워커 tb-w3 한 대에서 돌고, 같은 노드 파드끼리의 통신도 노드의 iptables FORWARD 를 지난다.
운영자가 "그동안 아무 데서나 받던" restaurant API 포트 8081 을 좁혀, 손님 요청이 들어오는 진입 망(제어 노드의 NodePort
SNAT 주소, 호스트 망)과 노드 자신만 받게 한다. 그런데 restaurant 를 부르는 클러스터 안의 호출자 order-service 는
같은 노드의 파드 대역(10.244.2.0/24)에 있고, 그 대역이 허용 목록에 없다. 이미 맺어진 연결은 그대로 두는 규칙이라
order 가 열어 둔 keep-alive 연결이 닫힐 때까지는 주문이 되고, 그 뒤로는 새 연결의 SYN 이 버려져 모든 주문이 502 다.
restaurant 자신은 멀쩡하고 손님의 가게, 메뉴 조회에 계속 답한다.

비유: 건물 출입 통제를 강화하며 "정문으로 들어온 방문객과 건물 관리인만 3층 주방에 들어갈 수 있다"고 바꿨는데,
2층 홀에서 일하는 직원(order)이 명단에서 빠졌다. 이미 주방 문을 열어 둔 직원은 문이 닫힐 때까지 드나들지만,
한 번 닫히고 나면 아무도 주문을 주방에 넣지 못한다. 주방(restaurant)은 정문 손님에게는 계속 음식을 낸다.

## 2. 원본 사례

- **Harness, 2024-09-01**(사후 보고 게시 2024-09-17), 상태 페이지 사고 "Harness cloud builds failing at initialise step for MAC users",
  [공식](https://status.harness.io/incidents/bs6qp18g8l21). 2026-10-09 WebFetch 로 원문 확인.
  - "We tightened a firewall rule for our Mac VM registry that was previously too permissive."
  - "the new rule did not account for the NAT IP address of one of these components."
  - "The issue didn't surface immediately as the affected component maintains a persistent socket connection", 연결이 다시 맺어지거나 재시작할 때까지 방화벽의 영향을 받지 않음.
  - 시각: 9월 1일 17:00 UTC 규칙 제한, 9월 4일 06:03 UTC 고객 보고, 08:39 UTC 규칙을 다시 만들고 검증.
  - 후속: 필요한 NAT IP 를 넣어 다시 좁히기, 방화벽 제한 적용 시 관련 서비스 재시작, 변경 때 연결을 비우고 다시 맺게 하기.
- `docs/ref-real-world-incidents.md` M15(네트워크 손실, 노드 네트워크 단절)에 이 행을 추가했다(출처가 말한 사실만).

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 지나치게 열려 있던 Mac VM 레지스트리 방화벽 규칙을 좁힘 | 지금까지 모두 받던 restaurant API 포트(8081)에 tb-w3 호스트 방화벽 허용 목록을 넣음(운영자가 노드에서 iptables 명령) |
| 원인이 된 결함 | 새 규칙이 레지스트리를 쓰는 한 구성 요소의 NAT IP 를 빠뜨림 | 허용 목록(노드 자신 10.244.2.1, 진입 노드 대역 10.244.0.0/24, 호스트 망 192.168.122.0/24, 192.168.200.0/24)이 클러스터 안 호출자 order 의 파드 대역(10.244.2.0/24)을 빠뜨림 |
| 전파 경로 | 그 구성 요소는 지속 소켓 연결을 유지해 다시 맺어질 때까지 무사, 다시 맺는 순간부터 막힘 | 규칙은 conntrack NEW 만 거른다. order 의 keep-alive 연결(평시 1개 유지)이 닫힐 때까지 주문 성공, 그 뒤 새 연결 SYN 이 버려져 connect-timeout 3초, 재시도 3회, 서킷브레이커 열림 |
| 사용자 증상 | 일부 고객의 macOS CI 파이프라인이 초기화 단계에서 실패 | 모든 음식 주문이 502. 가게와 메뉴 둘러보기는 정상(손님은 허용된 진입 망으로 들어옴) |
| 탐지된 경로 | 고객 보고 | order 오류율과 오류 로그 급증(관제), 러너의 주문 5xx 비율 |
| 완화와 복구 | 방화벽 규칙을 다시 만듦 | cleanup 이 점프 규칙과 체인을 지움. 다음 새 연결부터 통하고 서킷이 반열림 성공으로 닫힘 |

기전 확인: 원인(허용 목록 강화가 한 호출자를 빠뜨림)에서 증상(그 호출자만 새 연결 실패, 지속 연결은 다시 맺어질 때까지 무사)까지의
고리가 원본과 같다. 바뀐 것은 대상(레지스트리 → restaurant API)과 주소 종류(NAT IP → 파드 대역)뿐이다.

## 3. 숫자 근거 (2026-10-09, `scenario-stats.py`, 정식 + 후보)

- 서비스: commerce 26, core-banking 14, **food-delivery 10 → 11**(가장 적음). 이 후보는 food.
- 묶음: A 7, B 7, C 6, D 7, E 2, F 3, G 6, H 2, I 1, J 4, L 3, M 1, O 1, **K 0 → 1**, N 0. 합계 0 인 K 가 최우선 묶음이다. 20% 상한(51 중 10.2)에 닿는 묶음 없음.
  주 묶음 K(서비스 사이 통신이 끊김), 계기가 운영자의 방화벽 설정 변경이라 K+G 로 적는다. G 는 6 → 집계는 주 묶음 K 로만.
- 정답 위치: **노드, 디스크 3 → 4**(51 중 7.8%). 결제 경로 합계 10/51(19.6%)이고 이 후보는 결제 쪽이 아니다.
- 부품 지도: food 에서 0 인 부품은 kafka, order, notify. 인프라 층 "네트워크와 DNS" 중 네트워크(방화벽)는 정답으로 쓰인 적이 없다(F37-R 은 DNS).
- 왜 이 후보인가: K 는 묶음 중 유일하게 계속 0 이었다(§5 F13-H, F13-P, F13-R 은 주입기 부재, rejected 의 NetworkPolicy 는 flannel 단독이라 수단 없음,
  tc 와 MTU, conntrack, 경로 삭제는 원본 사례 불일치나 계기 흔적 부재). 노드 방화벽은 계기가 노드 auth 로그로 119 에 남고(§7), 원본(Harness 2024-09-01)의 기전과 같다.
  서비스도 가장 적은 food 다.

후보 목록과 버린 이유는 §11 에 있다.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 계기: tb-w3(192.168.122.14)에서 `sudo iptables -N RESTAURANT-API-IN`, `-A RESTAURANT-API-IN -s <허용 출발지> -m comment --comment "restaurant-api: allow node and entry networks" -j RETURN` 4개,
   `-A RESTAURANT-API-IN -m limit --limit 6/min -j LOG --log-prefix "[FW BLOCK] "`, `-A RESTAURANT-API-IN -j DROP`,
   `-I FORWARD 1 -d 10.244.2.0/24 -p tcp --dport 8081 -m conntrack --ctstate NEW -m comment --comment ... -j RESTAURANT-API-IN`.
2. 노드 망: tb-w3 는 br_netfilter 적재, `net.bridge.bridge-nf-call-iptables=1`(2026-10-09 실측)이라 cni0 브리지를 지나는 파드 사이 트래픽도 FORWARD 를 지난다.
   FLANNEL-FWD(-s 10.244.0.0/16 ACCEPT) 계수기가 10초에 약 2,400 패킷 늘어 파드 출발 새 연결이 FORWARD 를 지나는 것을 확인했다(기존 연결은 KUBE-FORWARD 의 ESTABLISHED 규칙이 받는다).
3. 호출자 배치: `food-delivery/k8s/20-order-deploy.yaml:18-19`, `21-restaurant-deploy.yaml:18-19`(nodeSelector tb-w3). 2026-10-09 restaurant 파드 10.244.2.218, order 10.244.2.77.
   restaurant 의 8081 상대(파드 /proc/net/tcp): 10.244.2.77(order, ESTABLISHED 1개 유지), 10.244.2.1(kubelet 프로브), 10.244.0.0(tb-cp NodePort 로 들어온 손님 요청, flannel 주소로 SNAT).
   다른 호출자는 없다(food 안에서 restaurant 를 부르는 것은 order 뿐: `food-delivery/order-service/src/main/resources/application.yml:51-54`).
4. order 의 호출: `OrderService.java:58-60`(createOrder 첫 단계 `restaurantClient.getRestaurant`), `RestaurantClient.java:29-56`(@CircuitBreaker/@Retry restaurant, `log.error("Failed to fetch restaurant {}: {}")`, 502, fallback 'Restaurant service unavailable'),
   `RestClientConfig.java:15-43`(SimpleClientHttpRequestFactory = HttpURLConnection, keep-alive 재사용, `setConnectTimeout`), `application.yml:51-54`(connect-timeout 3s, read-timeout 5s), `69-77`(서킷 sliding window 10, 50%, 열림 5초), `98-104`(재시도 3회 200ms 부터 두 배).
5. 결과: SYN 무응답 → 3초 뒤 `I/O error on GET request for "http://testbed-restaurant:8081/api/restaurants/N": Connect timed out` → 재시도 → 서킷 열림 → `POST /api/orders` 502, 주문 행 없음.
6. 진입 경로: loadgen-food(tb-runner)와 동반 부하는 `http://192.168.122.77:30180/30181`(tb-cp NodePort)로 들어오고 tb-cp 의 flannel 주소 10.244.0.0 으로 SNAT 되어 허용 목록 안이다. order NodePort(8080)는 규칙의 포트가 아니다.

## 5. 원인 규정 (원칙 5, 6)

- 근본(`root_cause.target_id`): `tb-w3`(target_kind `node`). 결함은 노드의 방화벽 설정이다. 고쳐야 재발이 막히는 곳이 그 규칙이다.
- 계기(`trigger_target_id`): 비움. 계기도 같은 곳(tb-w3 방화벽 규칙 변경)이다.
- 원칙 6: order 의 요청(가게 확인)과 restaurant 의 처리는 늘 하던 그대로 정당하다. 잘못된 것은 그 사이를 막은 규칙이다. 원본도 "방화벽 규칙 변경이 한 구성 요소를 빠뜨림"을 원인으로 적었다(같은 층위).
- 부분 점수(`scoring.partial`): `food-delivery-restaurant`(연결이 닿지 않는 목적지만 본 답), `food-delivery-order`(증상 서비스만 본 답).
- 채점 입도 `node`, accept `tb-w3`(F10-H, F05-P 와 같은 표기). target_id 는 partial 에 없다.
- 원칙 5: 정답은 "tb-w3 의 방화벽 규칙 변경이 order → restaurant 새 연결을 막았다"이고, 관제 데이터(노드 syslog 의 iptables 명령과 커널 차단 로그, order 의 연결 시간 초과 로그, restaurant 정상)로 낼 수 있다.
  왜 그 대역을 빠뜨렸는지(변경 검토 절차)나 keep-alive 동작 세부는 요구하지 않는다.

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 | 기대 |
|---|---|---|
| 감지(lucida-next) | order 오류율(APM error_rate), order ERROR 로그 새 템플릿('Failed to fetch restaurant N: I/O error ... Connect timed out') 급증, 'Created order' 소멸 | 같은 경로의 F36-R(order 502, restaurant 500)이 정식 녹화에 성공한 겉모양과 같다. 별도 업무 규칙 없이 인시던트가 나야 한다 |
| 피해 판정(러너) | 동반 부하 k6 의 주문 생성 5xx 비율 ≥ 0.5, 2xx 비율 < 0.2 (3틱) | 평시 주문 5xx 는 0 근처(F36-R 과 같은 관측) |
| RCA | §7 의 노드 syslog(sudo iptables, kernel '[FW BLOCK]'), order 로그, restaurant 정상, KCM 무변화 | 근본 위치 tb-w3 까지 간다 |

## 7. 관측 근거 (119 실조회, 2026-10-09 12:00~12:20 UTC)

| 증거 | 표, 칸 | 조회와 결과(평시) | 표본 여부 |
|---|---|---|---|
| 계기: 노드의 iptables 명령 | CH `lucida.syslog_local` (`hostname`, `app_name`, `message`) | `SELECT received_at, hostname, app_name, substring(message,1,200) FROM lucida.syslog_local WHERE received_at > now() - INTERVAL 7 DAY AND hostname='tb-w3' AND app_name='sudo' ORDER BY received_at DESC LIMIT 15` → 설계 중 실행한 `nkia : PWD=/home/nkia ; USER=root ; COMMAND=/usr/sbin/iptables -S FORWARD` 등이 몇 초 안에 보임(sudo 가 인자 전체를 남긴다). `rsyslog.d/60-lucida-forward.conf` 가 `*.*` 를 119:514/udp 로 넘긴다. 워커(tb-w1~3)의 `COMMAND=/usr/sbin/iptables` 는 30일 동안 2026-10-09 설계 조회 6건뿐 | 전수 |
| 계기와 전파: 버려진 연결 | 같은 표 `app_name='kernel'` | 커널 로그는 수집된다(tb-w3 kernel 하루 약 175건, 예 `cni0: port 10(veth84194e99) entered disabled state`). `SRC=`, `DPT=` 를 담은 줄은 워커 30일 0건. 장애 시 `[FW BLOCK] IN=cni0 OUT=cni0 ... SRC=10.244.2.x DST=<restaurant> ... DPT=8081 ... SYN` 이 분당 최대 6건 | 전수 |
| 전파: order 연결 시간 초과 | CH `lucida.lucida_logs_local` | `SELECT countIf(body LIKE '%Connect timed out%'), countIf(body LIKE '%Read timed out%'), countIf(body LIKE 'Failed to fetch restaurant%I/O error%'), countIf(body LIKE 'Created order id=%') FROM lucida.lucida_logs_local WHERE service_name='food-delivery-order' AND timestamp > now() - INTERVAL 10 DAY` → **0**, 292(dispatch 쪽 F19, F32 계열), **0**, 406,510(보존 2026-10-02 12:00~). 'Failed to fetch restaurant N: 500 ...' 는 F36-R 실행 구간에만 있다 | 전수 |
| 피해: 주문 생성 로그 | 위 표 `body LIKE 'Created order id=%'` 5분 창 | F32-H 시트 §7: 최근 6시간 5분 창 중앙 239, p10 165. 장애 시 0 근처 | 전수 |
| 감별: restaurant 는 멀쩡 | VM `apm.agent.otel.java.rps{service_name="food-delivery-restaurant"}` | 최근 1시간 최대 1.37rps(손님 조회). 장애 중에도 유지되어야 한다. restaurant 는 요청마다 로그를 남기지 않아(1시간 로그 2줄, 인기 메뉴 배치) 이 축은 APM 과 KCM 으로 본다 | APM 은 보조 |
| 감별: 쿠버네티스 무변화 | CH `lucida.kcm_events_local` | 주입은 쿠버네티스 객체를 바꾸지 않는다. restaurant 파드는 2026-08-11 이후 같은 파드(59일), order 는 2026-10-08 부터 같은 파드. 장애 구간에 rca-testbed-food 의 ScalingReplicaSet, Killing, Unhealthy 가 없어야 한다 | 전수 |
| 보조: order 클라이언트 스팬 | CH `lucida.otel_traces_local` | order → restaurant CLIENT 스팬 오류, POST /api/orders 502. 10% 표본이라 보조로만 | 10% 표본 |

핵심 증거(노드 auth 로그의 iptables 명령, 커널 차단 로그, order 연결 시간 초과 로그, 주문 로그 소멸, 쿠버네티스 무변화)는 모두 전수 수집 데이터다.
트레이스와 APM 지표는 보조다. 장애 시 형태(커널 LOG 줄의 실제 수집, 오류 문장)는 첫 실행에서 다시 확인한다.
119 에는 파드 IP 와 파드를 잇는 표가 없다(PG `kcm_resources_history` 의 파드 yaml 은 생성 시점이라 podIP 가 비어 있음). 그래서 커널 줄의 SRC 가 order 라는 것은
"order 만 연결 시간 초과를 남긴다"와 시각으로 잇는다. 정답 위치(tb-w3)와 막힌 포트(8081 = Service testbed-restaurant 의 targetPort)는 명령 자체에 있다.

## 8. 감별

- must_support: 정답지 `must_support` 5개(노드 auth 로그 iptables 명령, 커널 '[FW BLOCK] ... DPT=8081', order 'Connect timed out' 과 502, restaurant Ready 와 rps 유지, 쿠버네티스와 DB 무변화).
- must_rule_out: restaurant 다운, restaurant 오류나 지연(F36-R, F24-Q), order 하류 주소 오설정(F39-R), DNS(F37-R), DB와 용량, 노드 자원 고갈(F10-H, F21-Q).
- contrast_with: F36-R(같은 경로, restaurant 가 SQL 오류 500), F39-R(호출자 설정 배포로 다른 호스트), F37-R(DNS 정책), F10-H(같은 tb-w3 정답, 디스크 IO 포화).
- 러너 배제 조건: 동반 부하 미전달(achieved_rps < 0.5), MySQL 파드 NotReady, restaurant 파드 NotReady.

## 9. 러너 판정과 강도, 부하 계산 (원칙 9)

- 강도: 하나로 고정(approved-fixed-f44-r). 규칙은 켜거나 끄는 것뿐이라 사다리가 없다.
- 피해 계산: 규칙 뒤 order 의 기존 연결은 restaurant Tomcat 이 연결당 100 요청 뒤 닫거나 JDK 가 5초 쉰 연결을 닫을 때까지 통한다. 기준선 주문 초당 0.2~1.2건 + 동반 부하 약 1.4건이면
  restaurant 호출(주문당 2회)이 초당 3~5건이라 길어야 1분 안팎이다. 그 뒤 새 연결은 모두 3초 connect-timeout. 서킷은 호출 5건 이상에서 실패율 50% 면 열리므로 주문 두 건 만에 열리고,
  5초 열림, 반열림 3회(각 약 10초) 실패를 되풀이한다. 열린 동안의 주문은 CallNotPermitted 재시도 백오프(약 0.6초) 뒤 fallback 502, 반열림 호출은 약 10초 뒤 502 다.
  따라서 연결이 닫힌 뒤 주문 성공은 0 이다(F36-R 과 같은 서킷 구조, 같은 판정 임계).
- 입구 보호: order DB 연결은 가게 확인 단계에서 잡지 않는다(F21-Q 정답지의 Hibernate 지연 연결 획득). 반열림 3건이 약 10초씩 Tomcat 스레드를 쥐어도 200 스레드, Hikari 15 에 한참 못 미쳐
  order readiness 는 유지된다. 진입점(tb-cp NodePort 30180, 30181)은 규칙과 무관하다.
- 부하: `load.north_south` order-surge.js 2rps(F36-R 과 같은 값, 상한 180 의 1%), ramp 2m, hold 21m. 한가한 시간에도 주문 표본을 만든다.
- 판정: success = order_create_5xx_rate ≥ 0.5 이고 order_create_2xx_rate < 0.2 가 3틱. min_hold 15m, level timeout 20m, max_injection_duration 25m.
- cleanup: FORWARD 점프 삭제, 체인 비우고 삭제, `iptables -S` 로 둘 다 없음 확인 후 상태 파일 삭제. recovery: 상태 파일과 규칙이 없고 restaurant, order 파드 Ready, 기준선 주문 2xx ≥ 0.7.
- 안전: 규칙은 tb-w3 파드 대역의 8081 로 가는 **전달 트래픽의 새 연결**만 맞춘다. 정책(-P), INPUT, OUTPUT, 다른 체인은 건드리지 않아 호스트 망 SSH(복구 경로), kubelet, 다른 포트는 영향이 없다.
  실행기는 상태 파일을 규칙보다 먼저 써서 중간에 실패한 run 도 cleanup 이 치운다. 목록 조회가 실패하면 '없음'으로 읽지 않고 멈춘다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Harness 2024-09-01, 링크)와 요소별 대응표가 있고 기전이 같다(§2) (원칙 1)
- [x] 분류 장부를 갱신했다(§4-1 행, §6 기록). 묶음 K 0→1(2%), 정답 위치 노드, 디스크 3→4(7.8%), 결제 경로 10/51(19.6%, 이 후보는 무관), food 10→11(가장 적음). 어느 축도 20% 에 닿지 않는다(§3) (원칙 2)
- [x] 근본 원인 위치가 인프라 지점(tb-w3 iptables FORWARD, br_netfilter 설정)과 코드 위치로 확인됐다(§4) (G1)
- [x] 근본 원인의 흔적이 119 실데이터에서 조회됐다: syslog_local 의 sudo iptables 명령(hostname, app_name, message)과 kernel 줄 수집(§7) (원칙 3)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: syslog, 로그, KCM 이벤트는 전수(§7) (원칙 3)
- [x] 계기의 흔적이 조회됐다: 노드 auth 로그에 iptables 명령이 인자째 남는다. 인공 지연 없음(§7) (원칙 4)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다(§5) (원칙 5)
- [x] 정답지 세 칸이 원칙 6대로 채워졌다(§5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다(§6) (원칙 7)
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: 체인 이름 RESTAURANT-API-IN, 주석 'restaurant-api: allow node and entry networks', 로그 접두어 '[FW BLOCK] ', 상태 파일 `tb-w3-RESTAURANT-API-IN-firewall`. 실행기 테스트가 인자와 스크립트에 id 가 없음을 확인한다. k6 동반 부하 태그는 기존 시나리오와 같은 방식 (원칙 8)
- [x] 부하 상한과 서킷브레이커를 넣은 피해 계산이 있다(§9) (원칙 9)

## 11. 후보 목록 (반복 4, 2026-10-09)

부품 지도의 0 인 부품(commerce kafka, notification, gateway, nginx / banking kafka, nginx / food kafka, order, notify)과 인프라 층(네트워크, 인증서, 배치, 프로브, 노드 자원)에서 출발했다.

| 순위 | 후보 | 원본 사례 | 부품 | 판정 |
|---|---|---|---|---|
| 1 | food tb-w3 호스트 방화벽 허용 목록이 order 파드 대역을 빠뜨려 restaurant 8081 새 연결 SYN 드롭 | Harness 2024-09-01 공식 | 노드 네트워크(방화벽), food | **채택**(K 0, 노드 3, food 최소) |
| 2 | banking tb-w2 호스트 방화벽 허용 목록이 Oracle 1521 에서 transfer 를 빠뜨림 | Harness 2024-09-01 | 노드 네트워크, banking | 보류: 1과 같은 원본과 주입이고 증상이 F35-R(재접속 실패, NotReady, 502)과 겹침 |
| 3 | food payment → 외부 결제 mock 송신 방화벽 차단 | Ex Libris Alma 2019-07-09 RCA(방화벽 설정 변경이 외부 송신 차단) | 외부 결제 경로 | 버림: 결제 경로 정답 금지(19.6%) |
| 4 | commerce WAF 규칙을 시험 모드 대신 차단 모드로 배포 | Zendesk 2022-06-29 공식(CDN WAF 규칙 차단 모드, 440만 요청 차단) | commerce nginx, gateway | 버림: nginx 에 OTel 이 없어 119 에 흔적이 안 남고(원칙 3, 기존 반려), 게이트웨이 차단은 4xx 단일 신호(F34-R 교훈) |
| 5 | commerce gateway 레이트 리미터 키 오설정 | PostHog 2025-10-24 | gateway | 버림: 같은 원본이 nginx, MySQL 판으로 이미 두 번 막힘, 429 단일 신호 |
| 6 | food notify 새 릴리스의 독 메시지 크래시 루프 | PostHog 2026-07-23(2차) | notify | 버림: notify 는 로그만 남기는 소비자라 사용자 증상 없음(원칙 7), 원본이 2차 출처(원칙 1) |
| 7 | banking Kafka 브로커 디스크 가득 | 공식 사후 보고 없음(M18) | banking kafka | 버림: 원칙 1, outbox 비동기라 사용자 증상 없음(기존 Kafka 반려와 같은 벽) |
| 8 | banking ledger 새 릴리스 결함 | 공식 사례 미확인 | ledger | 버림: 소비자 경로라 사용자 5xx 없음(원칙 7) |
| 9 | food order 새 릴리스가 restaurant 호출마다 새 HTTP 클라이언트를 만들어 소켓, 스레드 누수 | 공식 사후 보고 없음 | food order | 버림: 원칙 1 |
| 10 | 대학 방화벽 변경이 이름 해석 실패로 규칙 일부를 끔 | MacEwan 2024-11 상태 페이지 | DNS | 버림: 공신력 약함, DNS 는 F37-R 과 겹침 |

서로 다른 부품 7개(노드 네트워크, 외부 결제, nginx/gateway, notify, kafka, ledger, order), 앱과 인프라 두 층, DB 정답 후보 0개.
