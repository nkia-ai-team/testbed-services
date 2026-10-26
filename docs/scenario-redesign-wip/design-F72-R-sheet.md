---
title: F72-R 설계 시트 — banking 워커 방화벽이 Oracle 리스너 앞에서 같은 노드 앱 파드를 빠뜨려 풀이 수명대로 빔
status: Draft
owner: project
last_reviewed: 2026-10-12
tags:
  - scenario
  - design
  - network
summary: 운영자가 은행 워커 tb-w2 의 호스트 방화벽에서 Oracle 리스너 포트(1521)를 노드 주소와 호스트, 관리망에서만 받도록 좁히며 같은 노드의 banking 앱 파드(account, transfer, ledger)를 빠뜨려, 열린 Hikari 세션이 수명(10분)대로 물러난 뒤 다시 맺지 못하고(ORA-12170 TCP connect timeout) 세 서비스가 readiness 에서 빠져 banking 과 commerce 정산이 502 가 되는 시나리오(Harness 2024-09-01 재구성)의 설계 근거.
---

# F72-R 설계 시트

- id `F72-R`, slug `f72-r-banking-node-firewall-omits-oracle-app-clients`, 새 사례군 F72 의 첫 시나리오
- 상태: 후보(ready + `stage: candidate`), 설계 강도 하나로 고정한 evaluation 모드, 녹화 대기 큐에 추가
- 주입 수단: 기존 실행기 `host.firewall`(`scripts/scenarios/profiles/host_firewall_executor.py`)에 Oracle 리스너 계약 하나를 더함(동작 변경 없음) + `load.north_south` 동반 부하

## 1. 요약과 비유

banking 의 account, transfer, ledger 는 Oracle 파드와 같은 워커 tb-w2 에서 돌며 자기 파드 주소(10.244.1.x)로 Oracle 리스너(1521)에 붙는다.
DB 감시(119 DPM)와 DBA 도구는 NodePort 30308 로 들어와 노드 주소로 SNAT 되거나 호스트 망 주소로 온다.
운영자가 "그동안 아무 데서나 받던" Oracle 리스너 포트를 좁혀 노드 주소와 호스트, 관리망만 받게 한다. 리스너 로그에 보이던 접속(감시, 도구)만 생각하고,
같은 노드에서 파드 주소로 바로 붙는 앱 파드 대역을 빠뜨렸다. 이미 맺어진 세션은 그대로 두는 규칙이라 아무 일도 없어 보이다가, Hikari 가 세션을 수명(10분)대로
물리고 새로 맺으려는 순간부터 SYN 이 버려진다. 약 10분 안에 세 서비스의 풀이 모두 비고 readiness 에서 빠져 banking 의 조회, 이체와 commerce 정산이 502 다.
Oracle 은 멀쩡하고 감시는 계속 붙어 있다.

비유: 금고실 출입 명단을 "경비실과 관리 사무소에서 오는 사람만"으로 바꿨는데, 매일 금고실 옆방에서 바로 드나들던 직원들이 명단에 없었다.
이미 금고실 안에 있던 직원들은 일을 계속하다가, 교대하러 나갔다 들어오려는 순간 하나씩 막힌다.

## 2. 원본 사례

- **Harness, 2024-09-01**(사후 보고 게시 2024-09-17), 상태 페이지 사고 "Harness cloud builds failing at initialise step for MAC users",
  [공식](https://status.harness.io/incidents/bs6qp18g8l21). 자료 문서 M15 에 이미 있다(F44-R 원본).
  - 9월 1일 17:00 UTC "We tightened a firewall rule for our Mac VM registry that was previously too permissive."
  - 새 규칙이 레지스트리를 쓰는 구성 요소 하나의 NAT IP 를 빠뜨림. 그 구성 요소는 지속 소켓 연결을 유지해, 연결을 다시 맺거나 재시작할 때까지 방화벽의 영향을 받지 않아 문제가 바로 드러나지 않음.
  - 영향: 일부 고객의 macOS 호스팅 CI 파이프라인이 초기화 단계에서 실패. 탐지: 9월 4일 고객 보고. 완화: 방화벽 규칙을 다시 만들고 검증.
  - 후속: 필요한 NAT IP 를 넣어 다시 좁히기, 방화벽 제한을 적용할 때 관련 서비스 재시작, 변경 시 연결을 비우고 다시 맺게 하기.

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 지나치게 느슨하던 레지스트리 앞 방화벽 규칙을 좁힘 | 지금까지 모두 받던 Oracle 리스너 포트(1521)에 tb-w2 호스트 방화벽 허용 목록을 넣음(운영자가 노드에서 iptables 명령) |
| 원인이 된 결함 | 새 규칙이 레지스트리를 쓰는 구성 요소 하나의 주소를 빠뜨림 | 허용 목록(노드 주소 /32 넷, 호스트 망 둘, 관리망)이 같은 노드에서 파드 주소로 붙는 banking 앱 파드 대역(10.244.1.x)을 빠뜨림 |
| 전파 경로 | 지속 소켓 연결이라 다시 맺을 때까지 영향 없음, 다시 맺을 때 실패 | 규칙은 conntrack NEW 만 거른다. Hikari 풀 세션(account 3, transfer 5, ledger 3)이 수명 10분대로 물러나며 다시 맺지 못함(ORA-12170), 풀이 비고 readiness 이탈 |
| 사용자 증상 | 파이프라인 초기화 단계 실패 | banking 잔액 조회, 계좌 목록, 이체, 거래 내역 502, commerce checkout 502(정산 실패) |
| 원인 쪽 상태 | 레지스트리 자체는 정상 | Oracle 파드 Ready, DPM 수집 계속, 앱 세션만 사라짐 |
| 탐지된 경로 | 고객 보고(사흘 뒤) | banking api, commerce 오류율, ERROR 로그(ORA-12170), KCM readiness 실패, 러너의 잔액 조회 실패율과 이체 5xx 비율 |
| 완화와 복구 | 방화벽 규칙을 다시 만들고 검증 | cleanup 이 점프 규칙과 체인을 지움. Hikari 가 다음 시도에서 다시 맺고 readiness 가 돌아옴 |

기전 확인: 원인(정당한 클라이언트를 빠뜨린 방화벽 규칙 강화)에서 증상(그 클라이언트가 지속 연결을 다시 맺는 순간부터 실패, 지켜진 쪽은 정상)까지의 고리가 원본과 같다.
원본의 "지속 소켓 연결"이 여기서는 DB 연결 풀의 세션이고, 원본이 실패를 늦게 알아챈 이유(연결이 다시 맺어질 때까지 무사)가 그대로 재현된다. F44-R 은 같은 원본을 서비스 API 앞에서
재구성했고 지속 연결이 keep-alive(약 1분)였다. F72-R 은 DB 리스너 앞이라 지연이 풀 수명만큼(최대 10분) 길고, 막힌 쪽이 호출자 하나가 아니라 같은 노드 앱 셋이다.

## 3. 숫자 근거 (2026-10-12, `scenario-stats.py`, 정식 + 후보, 이 후보 전 106 종)

- 서비스: commerce 36, **core-banking 35 → 36**, food-delivery 35. 합계가 가장 적은 둘(은행, 음식배달) 가운데 은행에서 만든다(원칙 2, 2026-10-12 결정).
  음식배달 판을 고르지 않은 이유: food 네 서비스는 DB 를 보는 liveness 를 함께 써서 같은 고장에서 order, restaurant 입구가 연결 불가(필수 중단 조건 entry_status 0)가 된다(장부 F35-R 기록).
- 묶음(합계 106, 20% = 21.2): A 8, B 7, C 6, **D 16**, E 4, F 3, **G 12**, H 2, I 2, **J 17**, **K 2 → 3**, L 8, M 1, O 3, P 13, Q 2.
  주 묶음 K(네트워크 단절), 계기가 운영자의 방화벽 설정 변경이라 K+G(F44-R, F44-P 와 같은 분류). 현실 비중(네트워크 15%, Gunawi)에 비해 K 가 2.8% 로 적다.
- 정답 위치: **노드, 디스크 6 → 7**(107 중 6.5%). 가장 많은 은행 이체 서비스 9(8%). 결제 경로 13/107(12.1%, 이 후보의 정답은 결제 쪽이 아니다).
- 부품 지도: banking oracle 9, kafka 0, api 5, account 11, transfer 11, ledger 2, nginx 0. 0 인 kafka, nginx 는 §11 에서 막혔다(outbox 비동기, 119 미수집).
  인프라 층 "네트워크와 DNS" 정답은 F44-R, F44-P, F37-R 셋뿐이다.
- 왜 이 후보인가: 은행 앱 서비스(account, transfer)에 정답이 몰려 있어 인프라 층 정답이 맞고, 겉 증상이 F35-R(정식, 앱 계정 잠금)과 같은데 정답이 다른 H 관계라
  관제 AI 가 오류 문구(ORA-12170 대 ORA-28000)와 노드 계기(iptables 명령, 커널 차단 줄)를 읽어야만 맞힌다.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 계기: tb-w2(192.168.122.11)에서 `sudo iptables -N ORACLE-LSNR-IN`, `-A ORACLE-LSNR-IN -s <허용 출발지> -m comment --comment "oracle-listener: allow nodes and management networks" -j RETURN` 7개
   (10.244.1.1/32, 10.244.0.0/32, 10.244.2.0/32, 10.244.3.0/32, 192.168.122.0/24, 192.168.213.0/24, 192.168.230.0/24),
   `-A ORACLE-LSNR-IN -m limit --limit 6/min -j LOG --log-prefix "[FW BLOCK] "`, `-A ORACLE-LSNR-IN -j DROP`,
   `-I FORWARD 1 -d 10.244.1.0/24 -p tcp --dport 1521 -m conntrack --ctstate NEW -m comment --comment ... -j ORACLE-LSNR-IN`.
2. 노드 망: tb-w2 는 br_netfilter 적재, `net.bridge.bridge-nf-call-iptables=1`(2026-10-12 실측). 앱 파드는 ClusterIP testbed-oracle(10.99.55.102):1521 로 붙고,
   cni0 게이트웨이에서 DNAT 된 뒤 다시 cni0 로 라우팅되어 FORWARD 를 지난다. FORWARD 에서 출발지는 앱 파드 IP, 목적지는 Oracle 파드 IP(10.244.1.137)다(F44-P 에서 같은 노드 ClusterIP 경로 확인).
3. 실측 상대(2026-10-12 tb-w2 conntrack, 1521): 10.244.1.230(transfer, ESTABLISHED 5), 10.244.1.228(account, 3), 10.244.1.218(ledger, 3),
   10.244.2.0(119 DPM 이 tb-w3 192.168.200.138:30308 로 들어와 tb-w3 flannel 주소로 SNAT). tb-w2 NodePort 30308 로 직접 오는 192.168.122.1(109), 192.168.230.128 은
   FORWARD 에서 원래 출발지로 보인다(SNAT 는 POSTROUTING). 노드 podCIDR: tb-cp 10.244.0.0/24, tb-w1 10.244.3.0/24, tb-w2 10.244.1.0/24, tb-w3 10.244.2.0/24.
   허용 목록은 이 앱 외 상대를 모두 남기고 앱 파드 대역만 뺀다.
4. 앱 쪽: `core-banking/k8s/02-configmaps.yaml:8-10`(DB_HOST testbed-oracle, DB_PORT 1521), `account-service/src/main/resources/application.yml:12-17`(minimum-idle 3, connection-timeout 3000, max-lifetime 600000),
   `transfer-service/...:12-17`(minimum-idle 5), `ledger-service/...:12-17`(minimum-idle 3). `core-banking/k8s/21-account-service.yaml:73-87`, `22-transfer-service.yaml:87-103`, `23-ledger-service.yaml:73-87`
   (readiness 는 DB 를 보는 /actuator/health, liveness 는 /actuator/health/liveness 라 재시작 없이 트래픽에서만 빠진다).
5. 결과: 세션이 수명대로 물러남 → 새 연결 SYN 무응답 → ojdbc11 23.5 가 3초 뒤 `ORA-12170: Cannot connect. TCP connect timeout of 3000ms for host testbed-oracle port 1521.`
   (로컬 실측: HikariCP 5.1.0 + ojdbc11 23.5.0.24.07, 응답 없는 주소의 1521 에 대해 DriverManager 직접 연결은 위 문구로 실패하고, Hikari getConnection 은 3초 뒤
   `HikariPool-1 - Connection is not available, request timed out after 3000ms (total=0, ...)` 이며 두 번째 시도부터 원인에 ORA-12170 이 붙는다) → 풀이 빔 →
   요청 3초 뒤 실패, readiness 실패(3회 × 10초) → nginx 502(잔액, 계좌 목록), `api-service/.../AccountClient.java:28-58`(account 실패와 서킷 열림 502),
   `commerce/payment-service/.../BankingTransferClient.java:37-58`(정산 실패 502) → commerce checkout 502.
6. 영향받지 않는 경로: Oracle 프로브는 exec(healthcheck.sh, `core-banking/k8s/10-oracle.yaml:50-67`)라 방화벽과 무관하다. nginx, api, Kafka, banking NodePort 진입(30082)은 1521 이 아니다.
   119 DPM(10.244.2.0)과 109, 관리망의 NodePort 접속은 허용 목록 안이다.

## 5. 원인 규정 (원칙 5, 6)

- 근본(`root_cause.target_id`): `tb-w2`(target_kind `node`). 결함은 은행 노드의 방화벽 설정이다. 고쳐야 재발이 막히는 곳이 그 규칙이다.
- 계기(`trigger_target_id`): 비움. 계기도 같은 곳(tb-w2 방화벽 규칙 변경)이다.
- 원칙 6: 앱의 재연결과 Oracle 의 처리는 늘 하던 그대로 정당하다. 잘못된 것은 그 사이를 막은 규칙이다. 원본도 방화벽 규칙을 원인으로 적었다(같은 층위).
- 부분 점수(`scoring.partial`): `banking-oracle`(닿지 않는 목적지만 본 답), `banking-account`, `banking-transfer`, `banking-ledger`, `banking-api`, `commerce-payment`(증상 서비스만 본 답).
- 채점 입도 `node`, accept `tb-w2`(F44-R, F44-P, F10-H, F15-P 와 같은 표기). target_id 는 partial 에 없다.
- 원칙 5: 정답은 "tb-w2 의 방화벽 규칙 변경이 같은 노드 앱 파드에서 Oracle 로 가는 새 연결을 막아 풀이 비었다"이고, 관제 데이터(노드 syslog 의 커널 차단 줄, 수집되면 iptables 명령,
  앱의 ORA-12170, DPM 세션 감소와 Oracle 정상, KCM readiness 실패)로 낼 수 있다. 계기 위치는 iptables 명령 줄이 없어도 커널 차단 줄(hostname tb-w2, DPT=1521, 같은 노드 출발지)만으로 tb-w2 방화벽까지 간다. 왜 그 대역을 빠뜨렸는지나 Hikari 수명 세부는 요구하지 않는다.

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 | 기대 |
|---|---|---|
| 감지(lucida-next) | core-banking-api, commerce-payment, commerce-order 오류율(APM error_rate), banking 세 서비스 ERROR 로그 새 꼴(ORA-12170, 'Connection is not available'), KCM readiness 실패 | 같은 겉모양의 F35-R(정식, 정상 녹화 있음)이 인시던트를 만들었다. 별도 업무 규칙 없이 인시던트가 나야 한다 |
| 피해 판정(러너) | 동반 부하 k6 의 잔액 조회 실패율 ≥ 0.5, 이체 5xx 비율 ≥ 0.5 (3틱) | 평시 둘 다 0(F35-R 과 같은 관측과 임계) |
| RCA | §7 의 노드 kernel syslog '[FW BLOCK] ... DPT=1521'(핵심), 앱 ORA-12170, DPM 세션, KCM. sudo iptables 줄은 수집되면 보조 | 근본 위치 tb-w2 까지 간다 |

## 7. 관측 근거 (119 실조회, 2026-10-12 10:40~11:00 UTC)

| 증거 | 표, 칸 | 조회와 결과(평시) | 표본 여부 |
|---|---|---|---|
| 계기와 전파(핵심): 버려진 연결 | CH `lucida.syslog_local` (`hostname`, `app_name='kernel'`, `message`) | 같은 날 F44-P 의 `[FW BLOCK] IN=flannel.1 OUT=cni0 ... DPT=8082 ... SYN` 이 tb-w2 에서 95건, F44-R 의 줄이 tb-w3 에서 91건 수집됨(평가 실조회: 커널 LOG 줄은 안정적으로 들어옴). 1521 을 담은 '[FW BLOCK]' 줄은 0건. 장애 시 `[FW BLOCK] IN=cni0 OUT=cni0 ... SRC=10.244.1.x DST=10.244.1.137 ... DPT=1521 ... SYN` 이 첫 재연결부터 분당 최대 6건. 노드(hostname), 리스너 포트, 같은 노드 앱 파드 출발지가 한 줄에 있다 | 전수 |
| 보조(수집되면): 노드의 iptables 명령 | 같은 표 `app_name='sudo'` | tb-w2 auth.log 에는 F44-P 주입의 `-N`, `-A` 4개, LOG, DROP, `-I FORWARD 1 … --dport 8082 …` 와 정리의 `-D FORWARD` 가 모두 있지만, 119 에는 정리의 `-F`, `-X` 두 줄만 들어왔다(주입 8줄 중 0줄). F44-R 도 주입 8줄 중 3줄. `/etc/rsyslog.d/60-lucida-forward.conf` 의 omfwd 가 udp 514 로 보내며 몇 초 안에 몰린 sudo 줄을 잃는다. 그래서 이 줄은 판정 근거로 쓰지 않는다 | 전수 의도, 실제 일부 손실 |
| 전파: 앱의 연결 실패 | CH `lucida.lucida_logs_local` | `SELECT service_name, count() FROM lucida.lucida_logs_local WHERE timestamp > now() - INTERVAL 30 DAY AND (body LIKE '%ORA-12170%' OR body LIKE '%TCP connect timeout%' OR body LIKE '%Network Adapter%') GROUP BY 1` → 0행(보존 시작 2026-10-05). F35-R 실행(2026-10-09 06:55~)에서는 같은 경로가 `ORA-28000 ...` ERROR(account 443, transfer 342)와 `HikariPool-1 - Connection is not available ... (total=0, ...)` 로 남았다: 원인 예외 문구가 로그에 그대로 실린다 | 전수 |
| 전파: 세션 감소, Oracle 정상 | VM `dpm.oracle.session.idle_session`, `dpm.oracle.session.current_session` | 최근 10분 idle_session 10~12, current_session 89~91. 장애 시 idle 이 10분 안에 0 으로, current 는 그만큼 준다. DPM 수집(CH `dpm_session_local` 의 `collector-dpm-...` 세션)은 계속된다 | 전수(1분 수집) |
| 전파: readiness 이탈 | CH `lucida.kcm_events_local` | F35-R 실행 창(2026-10-09 06:50~07:40) rca-testbed-banking: testbed-account, testbed-transfer, testbed-ledger 파드 `Unhealthy` 29, 29, 28건. 같은 모양이 나와야 하고 ScalingReplicaSet, Killing 은 없어야 한다 | 전수 |
| 피해: banking, commerce 오류 | 같은 로그 표, 러너 loadgen 관측 | api `Account service call failed`, commerce-payment `core-banking transfer call failed` 502. 동반 부하 잔액 조회 실패율, 이체 5xx 비율 평시 0 | 전수(로그) |
| 보조: 스팬, APM 지표 | CH `lucida.otel_traces_local`, VM `apm.agent.otel.java.error_rate` | core-banking-api 502 스팬, 오류율 상승. 10% 표본이라 보조로만 | 10% 표본 |

핵심 증거(커널 차단 줄, 앱의 ORA-12170, DPM 세션 감소와 Oracle 정상, KCM readiness 실패)는 모두 전수 수집 데이터다. 노드 auth 로그의 iptables 명령은 UDP 전달에서 잃을 수 있어 보조로만 둔다.
정답 위치(tb-w2)와 막힌 포트(1521 = Service testbed-oracle 의 targetPort)는 커널 차단 줄 자체(hostname, DPT)에 있다. 커널 줄의 SRC(10.244.1.x)가 banking 앱 파드라는 것은 119 에 파드 IP 표가 없어,
"Oracle 을 쓰는 세 서비스만 ORA-12170 을 남긴다"와 시각으로 잇는다.

## 8. 감별

- must_support: 정답지 `must_support` 6개(iptables 명령(보조, 수집되면), 커널 '[FW BLOCK] ... DPT=1521'(계기의 핵심), 세 서비스의 ORA-12170, DPM idle_session 감소와 Oracle 정상, KCM readiness 실패와 502, 풀 수명만큼 늦은 시작).
- must_rule_out: 계정 잠금, 만료(F35-R, F71-R), Oracle 다운(F25-H), 행 잠금과 느린 질의(F01-P, F33-R, F42-R), 앱 배포와 연결 설정(F65-R), DNS(F37-R), 노드 자원 고갈(F15-P).
- contrast_with: F35-R(같은 증상, ORA-28000), F44-R·F44-P(같은 원인 꼴, 서비스 API 앞), F25-H(DB 다운), F65-R(한 서비스 롤아웃의 접속 설정).
- 러너 배제 조건: 동반 부하 미전달(achieved_rps < 2.5), Oracle 파드 NotReady.

## 9. 러너 판정과 강도, 부하 계산 (원칙 9)

- 강도: 하나로 고정(approved-fixed-f72-r). 규칙은 켜거나 끄는 것뿐이라 사다리가 없다.
- 피해 계산: 세 풀의 세션은 만들어진 시각이 흩어져 있어 규칙 뒤 0~10분 사이에 하나씩 물러난다(Hikari 는 수명의 2.5% 안에서 흩뿌림). 물러난 자리를 채우려는 연결은 모두 3초 뒤 ORA-12170 이다.
  banking 사용자 경로의 동시 연결 수요는 1 안팎(이체 POST 5ms, 잔액 1ms)이라 세션이 하나라도 남으면 버티고, 마지막 세션이 물러나는 순간 전면 실패가 된다. 최악 시각은 수명 10분 + readiness 30초 ≈ 10.5분.
  Hikari 의 수명 만료는 강제라 부하나 시간대와 무관하게 반드시 온다(F35-R 과 같은 시간 모양, F35-R 은 정식으로 녹화됨).
- 입구 보호: banking 진입점(tb-cp NodePort 30082 → nginx → api)은 DB 를 쓰지 않아 502 로 답한다(entry_status 0 은 nginx 나 노드가 죽었을 때만). liveness 가 DB 를 보지 않아 재시작이 없다.
- 부하: `load.north_south` core-banking transfer-balance-surge.js 10rps(F35-R 과 같은 값, 상한 180 의 6%), ramp 2m, hold 27m. 이체 55%(30초에 약 165건), 잔액 조회 35%(약 105건, account 직행), 목록 10%, 거래 내역 단계 없음(2026-10-26 운영 보강 1회차에 surge.js 에서 바꿈: surge.js 의 거래 내역 25% 가 Oracle 을 먼저 채웠다).
- 판정: success = 잔액 조회 실패율 ≥ 0.5 이고 이체 5xx 비율 ≥ 0.5 가 3틱. min_hold 21m, settle 60s, level timeout 27m, max_injection_duration 30m.
- cleanup: 상태 파일이 없어도 규칙을 읽어 FORWARD 점프 삭제, 체인 비우고 삭제, `iptables -S` 로 둘 다 없음 확인 후 상태 파일 삭제. recovery: 규칙 없음, Oracle, transfer 파드 Ready, 기준선 잔액 조회 실패율 < 0.05(10분).
- 안전: 규칙은 tb-w2 파드 대역의 1521 로 가는 **전달 트래픽의 새 연결**만 맞춘다. 정책(-P), INPUT, OUTPUT, 다른 체인은 건드리지 않아 호스트 망 SSH(복구 경로), kubelet, 다른 포트는 영향이 없다.
  119 DPM, 109 러너의 DB 확인, 관리망 접속은 허용 목록 안이라 관측과 판정이 끊기지 않는다.
- 실행기 변경: 계약(`CONTRACTS["F72-R"]`)과 설명 한 단락만 더했다. 같은 노드 호출자라 호출자 네임스페이스, 노드, 대역은 지켜지는 쪽 기본값을 쓴다(F44-R 동작 그대로).
  preflight 의 배치 확인은 Oracle 파드(app=testbed-oracle, tb-w2, 10.244.1.0/24 안)와 대표 호출자 transfer(같은 대역, 허용 목록 밖), Service testbed-oracle 의 targetPort 1521 이다(2026-10-12 kubectl 로 각 조건 확인).
  첫 실행 확인 항목: 주입 시각 tb-w2 sudo iptables 줄의 119 수집 여부(F44-P, F44-R 실행에서 대부분 잃음, 없어도 판정은 커널 줄로 한다). 커널 LOG 수집은 F44-P, F44-R 실행에서 확인됨.
- **2026-10-26 운영 보강 1회차: 동반 부하에서 거래 내역 단계를 뺐다**(profiles.json `scenario_parameters.F72-R.script_path` `/opt/loadgen/core-banking/surge.js` → `/opt/loadgen/core-banking/transfer-balance-surge.js`, target_rps 10·ramp 2m·hold 27m 그대로, 정답지 description, injection_summary(영, 한)의 동반 부하 스크립트 이름, controller 노트, 실행 행렬). 근거 `~/dev/testbed-ops/verifications/2026-10-26-2143-F72-R-run-92d33300.md` §4-4 ③, §11: 주입(tb-w2 '[FW BLOCK] … DPT=1521' 129줄), 피해(세 파드 readiness 실패 85건, 잔액·이체 전면 실패), 원인 근거(ORA-12170 1,346줄, DPM idle_session 11→0)가 모두 남고 겹침도 없었으나, surge.js 의 거래 내역 단계(25%, 초당 2.5건)가 transfer 건수 질의 byb2y2hv7p4ag 실행을 분당 17회에서 139~167회로 늘려 20:19~20:27 에 Oracle cpu_time 1.51~1.79(상한 2), db_time 3.01, active_session 5 가 되어 must_rule_out 3 의 'Oracle 활성 세션과 CPU 가 오르지 않으며'를 어겼다(계획 2603328921 그대로, 10-23 두 실행도 cpu 1.95, 1.94). F47-R·F63-R(1ad6679, 272ccd2), F37-H(f3f71c4), F68-R(94386c5)과 같은 방법이다. 주입 수단, 강도(approved-fixed-f72-r), success, must_rule_out, must_support 는 그대로다. 재실행에서 확인할 것: Oracle cpu_time < 0.5, active ≤ 1, Hikari 첫 시간 초과가 total 2 이하에서 나는지.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Harness 2024-09-01, 링크)와 요소별 대응표가 있고 기전이 같다(§2) (원칙 1)
- [x] 분류 장부를 갱신했다(§4-1 행, §6 기록). 묶음 K 2→3(2.8%), 정답 위치 노드, 디스크 6→7(6.5%), 결제 경로 13/107(12.1%, 무관), banking 35→36. 어느 축도 20% 에 닿지 않는다(§3) (원칙 2)
- [x] 근본 원인 위치가 인프라 지점(tb-w2 iptables FORWARD, br_netfilter, podCIDR, conntrack 상대)과 코드 위치로 확인됐다(§4) (G1)
- [x] 근본 원인의 흔적이 119 실데이터에서 조회됐다: syslog_local 의 tb-w2 kernel '[FW BLOCK]' 줄 수집(F44-P, F44-R 실행분). sudo iptables 줄은 UDP 전달에서 대부분 잃어 보조로만 둔다(§7) (원칙 3)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: syslog, 로그, DPM, KCM 이벤트는 전수(§7) (원칙 3)
- [x] 계기의 흔적이 조회됐다: 규칙이 버린 연결마다 커널 차단 줄(노드, 포트, 출발지)이 남는다. 명령 줄은 노드 auth 로그에는 인자째 남지만 119 수집은 일부라 첫 실행에서 확인한다. 인공 지연 없음(§7) (원칙 4)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다(§5) (원칙 5)
- [x] 정답지 세 칸이 원칙 6대로 채워졌다(§5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다(§6) (원칙 7)
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: 체인 이름 ORACLE-LSNR-IN, 주석 'oracle-listener: allow nodes and management networks', 로그 접두어 '[FW BLOCK] ', 상태 파일 `tb-w2-ORACLE-LSNR-IN-firewall`. 실행기 테스트가 인자와 스크립트에 id 가 없음을 확인한다. k6 동반 부하 태그는 기존 시나리오와 같은 방식 (원칙 8)
- [x] 부하 상한과 서킷브레이커를 넣은 피해 계산이 있다(§9) (원칙 9)

## 11. 후보 목록 (반복 69, 2026-10-12)

부품 지도의 0 인 부품(banking kafka, nginx / food kafka)과 인프라 층(네트워크, 시계, 비밀값, 스케줄링, 배치)에서 출발했다. 기전은 원인 분포 상위(설정 배포, 운영 변경)와
아직 적은 M 번호(M5 고정 한도, M15 네트워크)를 먼저 봤다. 서비스는 합계가 가장 적은 은행(35), 음식배달(35)만 봤다. 숫자 순(0 묶음, 0 정답 위치, 적은 서비스)으로 세웠다.

| 순위 | 후보 | 원본 사례 | 부품, 층 | 판정 |
|---|---|---|---|---|
| 1 | banking nginx 에 원격 주소 기준 요청 한도(limit_req)를 넣는 설정 배포가 모든 요청을 한 주소로 보고 막음 (nginx 0) | PostHog 2025-10-24 공식 | banking nginx, 게이트웨이 층 | 버림: 원칙 3. nginx 로그와 설정이 119 에 없고, commerce 정산은 nginx 를 거치지 않고 transfer Service 로 바로 가서 피해가 119 밖의 부하 발생기에만 남는다 |
| 2 | banking Kafka 보존 설정으로 브로커 디스크가 차 발행 실패 (메시지 브로커 0) | Inngest 2025-10-16 공식 | banking kafka | 버림: 원칙 7. 발행이 outbox 비동기라 사용자 증상이 없다(rejected 307 과 같은 벽) |
| 3 | food MySQL 문장 시간 제한(max_execution_time) 전역 하향 (DB 인스턴스 2) | 공식 사후 보고 없음(웹 검색 1회) | food MySQL | 버림: 원칙 1, 9. food 사용자 경로 SELECT 가 모두 ms 단위(rejected 40 과 같은 벽) |
| 4 | banking Oracle 앱 계정에 SESSIONS_PER_USER 프로필 (DB 계정 4) | Google 2020-12-14 공식 | banking Oracle 계정 | 버림: 같은 수단(db.account 프로필 모드, F54-R), 원칙 9(열린 세션은 유지되고 동시 수요가 1 안팎이라 한도 안에서 돈다) |
| 5 | banking transfer 스레드 풀을 키우는 설정 배포가 OS 스레드 한도를 넘음 (은행 이체 9) | AWS Kinesis 2020-11-25 공식 | transfer, 런타임 한도 | 버림: 원칙 9. tb-w2 파드 cgroup pids.max 가 max, kernel.threads-max 94,860(실측)이라 한도 전에 메모리 한도 OOM 이 되어 기전이 F63-R 꼴로 바뀐다 |
| 6 | banking 세 풀 수명을 짧게 하는 설정 배포로 재연결이 몰림 (DB 인스턴스 2) | Clerk 2025-09-18 공식 | banking Hikari, Oracle | 버림: 원칙 1(원본 계기는 제공자의 엔진 마이너 업그레이드), 원칙 9(세 풀 합 11 세션의 로그온으로 Oracle 이 포화되지 않음), rejected 314 |
| 7 | food payment 의 외부 PG 자격 증명 만료 (결제 경로 13) | GitHub 2025-03 공식 | food payment, 외부 PG | 버림: PG mock 에 인증이 없고, payment 가 PG 의 4xx 를 그대로 넘겨 4xx 단일 신호(rejected 302 와 같은 벽) |
| 8 | banking transfer 리소스 요청 상향 롤아웃이 maxSurge 0 이라 Pending | 공식 사후 보고 없음(웹 검색 1회) | 스케줄링 층 | 버림: 원칙 1 |
| 9 | food notify 소비자 결함 (알림 서비스 1) | (사례 찾기 전 버림) | food notify | 버림: NotifyService 가 로그만 남기는 무동작 소비자라 사용자 경로가 없다(원칙 7) |
| 10 | **banking tb-w2 방화벽이 Oracle 리스너 앞에서 같은 노드 앱 파드를 빠뜨림** | Harness 2024-09-01 공식 | tb-w2 노드 네트워크, DB 앞 | **채택**(K 2, 노드, 디스크 6, banking 35, F35-R 과 같은 증상 다른 정답) |

서로 다른 부품 9개(banking nginx, banking kafka, food MySQL, Oracle 계정, transfer 런타임, banking Hikari, food 외부 PG, 스케줄링, food notify) + 채택 후보의 tb-w2 노드 네트워크.
