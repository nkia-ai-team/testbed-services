---
title: F72-P 설계 시트, 민원 포털 라우터에서 운영자가 DB 포트 허용 목록을 좁히다 WAS 망을 빠뜨림
status: Draft
owner: project
last_reviewed: 2026-11-25
tags:
  - scenario
  - design
  - network
  - civil-petition
summary: 민원 포털(SMS, NMS 전용 전통 3계층 테스트베드) 라우터 rt-core01 에서 운영자가 DB 서버 PostgreSQL 포트(192.168.126.11:5432)로 가는 길을 관제 터널, 관리망, 라우터 주소만 남기는 nftables 표로 좁히면서 그 포트의 유일한 클라이언트인 WAS 망을 빠뜨려, was01, was02 가 DB 에 붙지 못하고 포털 거의 모든 요청이 503, 500 이 되는 시나리오(Harness 2024-09-01 재구성)의 설계 근거. 사용자와 정한 카드 C2.
---

# F72-P 설계 시트

- id `F72-P`, slug `f72-p-petition-router-acl-resets-was-to-db`, 도메인 `civil-petition`(SMS + NMS 테스트베드)
- 사용자와 정한 카드: C2 "운영자가 라우터 접근 규칙을 잘못 고쳐 WAS → DB 길이 막힘"(`~/dev/testbed-ops/v4/scenario-design/cards.md`)
- 상태: 후보(ready + `stage: candidate`), 설계 강도 하나로 고정한 evaluation 모드, 녹화 대기 큐에 추가(note: 새 테스트베드 첫 시나리오, 흔적 표 실측과 119 보강 뒤 녹화)
- 주입 수단: 기존 실행기 `router.config`(운영자 경로) 에 첫 계약과 새 판정 `reset`(`reject with tcp reset`)을 더함. 동반 부하 없음(상주 부하가 피해를 낸다)

## 1. 요약과 비유

민원 포털은 웹(web01), WAS 두 대(was01, was02), DB(db01) 가 라우터(rt-core01) 하나로 이어진 전통 3계층이다. WAS 의 Spring 앱은 Tomcat JDBC 풀로 db01 의 PostgreSQL(192.168.126.11:5432)에 붙고, 이 연결은 늘 라우터를 지난다.
운영자가 보안 점검을 이유로 "DB 포트는 관제와 관리 쪽에서만 닿게" 라우터에 표를 하나 더한다. 표는 관제 터널, 관리망, 라우터 자기 주소를 먼저 받고 나머지 5432 연결은 기록한 뒤 거절한다. 그런데 그 포트를 실제로 쓰는 유일한 클라이언트, WAS 망을 목록에 넣지 않았다.
다음 패킷부터 WAS 가 DB 에 붙지 못해 포털의 로그인, 신청, 조회, 검색, 담당자 처리가 모두 몇 ms 안에 503 이나 500 이 된다. db01 과 PostgreSQL 은 멀쩡하고 핑에도 답한다.

비유: 건물 금고실 앞 출입문에 "경비실과 관리실 사람만" 이라는 새 명단을 붙였는데, 매일 금고를 여닫는 경리팀이 명단에 없었다. 금고는 멀쩡하지만 경리 업무가 전부 선다.

## 2. 원본 사례

- **Harness, 2024-09-01**(사후 보고 게시 2024-09-17), 상태 페이지 사고 "Harness cloud builds failing at initialise step for MAC users",
  [공식](https://status.harness.io/incidents/bs6qp18g8l21). 자료 문서 `ref-real-world-incidents.md` M15 에 이미 있다(F44-R, F72-R, F71-H 원본).
  - 9월 1일 17:00 UTC "We tightened a firewall rule for our Mac VM registry that was previously too permissive."
  - 새 규칙이 레지스트리를 쓰는 구성 요소 하나의 NAT IP 를 빠뜨림("the new rule did not account for the NAT IP address of one of these components").
  - 그 구성 요소는 지속 소켓 연결을 유지해 다시 맺을 때까지 영향이 드러나지 않음. 영향: 일부 고객의 macOS CI 파이프라인이 초기화 단계에서 실패. 탐지: 9월 4일 고객 보고. 완화: 방화벽 규칙을 다시 만들고 검증.

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 지나치게 느슨하던 레지스트리 앞 방화벽 규칙을 좁힘(운영 변경) | 운영자가 라우터 rt-core01 에 DB 포트(192.168.126.11:5432) 허용 목록 표 db-acl 을 넣음(운영자 ssh 로그인, 홈에 규칙 파일, `sudo nft -f`) |
| 원인이 된 결함 | 새 규칙이 레지스트리를 쓰는 구성 요소 하나의 주소를 빠뜨림 | 허용 목록(관제 터널 10.250.0.0/30, 관리망 192.168.127.0/24, 라우터 192.168.123.254)이 그 포트의 유일한 클라이언트인 WAS 망 192.168.125.0/24 를 빠뜨림 |
| 전파 경로 | 그 구성 요소의 레지스트리 접근 실패 | WAS 두 대의 DB 연결이 라우터에서 TCP 재설정으로 거절됨(열린 풀 연결은 다음 사용 때 재설정, 새 연결은 바로 거부) → 앱 DB 접근 실패 |
| 사용자 증상 | 파이프라인 초기화 단계 실패 | 포털 거의 모든 요청 503 'database unavailable', 신청과 담당자 처리 500 또는 503 |
| 원인 쪽 상태 | 레지스트리 자체는 정상 | db01 과 PostgreSQL 정상, ICMP 와 SMS 보고 정상, 5432 TCP 만 거절 |
| 탐지된 경로 | 고객 보고(사흘 뒤) | 웹, WAS 접근 로그 5xx 와 앱 ERROR 줄, 라우터 syslog 의 운영자 명령과 커널 차단 줄, NMS 의 DB 쪽 인터페이스 송신 감소 |
| 완화와 복구 | 방화벽 규칙을 다시 만들고 검증 | 운영자가 표를 지움(`sudo nft delete table inet db-acl`), 풀이 다음 요청에서 다시 연결 |

기전 확인: 원인(정당한 클라이언트를 빠뜨린 방화벽 규칙 강화)에서 증상(그 클라이언트의 접근이 실패하고 지켜진 쪽은 정상)까지의 고리가 원본과 같다.
원본의 "지속 연결이라 늦게 드러남"은 재현하지 않는다(카드와 달라진 점 1, §9): 운영자의 표가 연결 상태를 보지 않는 거절이라 열린 연결도 끊긴다. 이것이 F72-R(같은 원본, 쿠버네티스 워커 호스트 방화벽이 새 연결만 버림)과 갈리는 점이고 접미사를 P 로 둔 이유다.

## 3. 숫자 근거 (2026-11-25, `scenario-stats.py`, 정식 + 후보)

- 이번 실행에서 다른 점(공통 지시): 후보는 사용자와 정한 카드 10장이라 후보 넓게 뽑기를 하지 않는다. 숫자는 상한 확인용이다.
- 서비스: 설계 GT0 결정 3 (다)(v4 시작 뒤 생성분만 세어 1:1:1:1). 민원(civil-petition) 0 → 1. `scenario-stats.py` 는 정답지 domain 으로 civil-petition 을 세지만 "v4 시작 뒤 생성분만" 구간을 모른다. 사용자가 이 두 테스트베드 카드 10장을 정했으므로 서비스 균형으로 막지 않는다.
- 묶음(합계 119, 20% = 23.8): A 10, B 7, C 6, D 17, E 4, F 4, G 13, H 2, I 2, J 21, **K 3 → 4(3%)**, L 9, M 1, O 3, P 14, Q 2. 주 묶음 K(네트워크 단절), 계기가 운영자의 방화벽 설정 변경이라 K+G(F44-R, F44-P, F72-R 과 같은 분류). 현실 비중(네트워크 15%)에 비해 K 가 적다.
- 정답 위치: **노드, 디스크 8 → 9(7.6%)**(§2-1 '쿠버네티스 노드, 디스크, 네트워크 등 인프라'). 가장 많은 은행 이체 서비스 11(9%). 결제 경로 14/119(11.8%, 이 후보는 무관).
- 단순 규칙 적중률(`--candidate F72-P`): 판정 **보류**(v4 세트 6건 < 20건, 통과로 본다). 변경 원인 몫 3/5 → 3/6(50%, 하한 30% 위). 규칙 1, 2, 6 적중률 60% → 50%, 정밀 100% 그대로(미끼와 함께 75%).
- 왜 이 후보인가: 사용자와 정한 카드. 새 도메인에서 장비(라우터) 변경이 원인이고, 겉 증상(포털 전면 5xx, DB 연결 실패)이 앞으로 올 "DB 다운", "링크 끊김"(C1), "DB 서버 방화벽"(C10, S 도메인) 과 같아 관제 AI 가 라우터 syslog 와 NMS, db01 생존 근거를 읽어야만 맞힌다.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 계기(라우터): 운영자 sysop 이 운영자 단말 주소 192.168.127.5 에서 rt-core01(관리 192.168.127.2)에 ssh 로그인(키, pty)해 `/home/sysop/db-acl.nft` 를 쓰고 `sudo nft -f /home/sysop/db-acl.nft` 를 친다. 표 글(실행기가 만듦, `scripts/scenarios/profiles/router_config_executor.py:42-52` CONTRACTS["F72-P"], `vm_executor_common.py` `nft_table`):

   ```
   table inet db-acl {
       chain forward {
           type filter hook forward priority -5; policy accept;
           ip saddr 10.250.0.0/30 accept            (관제 터널, 양방향)
           ip daddr 10.250.0.0/30 accept
           iifname "ens22" accept                   (관리 NIC, 양방향)
           oifname "ens22" accept
           ip saddr 192.168.127.0/24 accept         (관리망, 양방향)
           ip daddr 192.168.127.0/24 accept
           ip saddr 192.168.123.254 accept          (라우터 상향 주소, 양방향)
           ip daddr 192.168.123.254 accept
           ip daddr 192.168.126.11/32 meta l4proto tcp th dport 5432 limit rate 6/minute log prefix "[DB ACL] "
           ip daddr 192.168.126.11/32 meta l4proto tcp th dport 5432 counter reject with tcp reset
       }
   }
   ```

   앞의 여덟 줄은 실행기가 모든 체인 첫머리에 넣는 보호 줄(설계 §3-5 규칙 6)인데, 운영자 이야기에서는 그대로 "관제와 관리 쪽만 남긴 허용 목록"이다. WAS 망(192.168.125.0/24)은 없다.
2. 라우터 망: 평시 상태 기반 ACL `civil-petition/deploy/templates/router/nft-acl.nft:3-7`(표 fwacl, forward 우선순위 0, policy drop, established 수용, 허용 흐름은 `civil-petition/deploy/inventory/t.json:107-125` 의 acl: 상향 → 웹, 웹 → WAS, WAS → DB 5432). 새 표는 우선순위 -5 라 fwacl 보다 먼저 보고, nftables 의 거절은 다른 표의 수용과 무관하게 끝이다. 연결 상태를 보지 않으므로 열린 연결의 패킷도 걸린다.
3. 실측(2026-11-25, 104 의 사용자 네임스페이스와 실제 커널, 시험 `test_f72p_router_table_refuses_forwarded_db_connects_at_once_and_cuts_open_ones`): 라우터 네임스페이스가 WAS 와 DB 네임스페이스 사이를 전달할 때 이 표를 넣으면 열린 연결은 다음 송신에서 `ConnectionResetError`(0.0초), 새 연결 열 번은 모두 `ConnectionRefusedError`(0.0초), ICMP 핑은 통과.
4. 앱 쪽: JDBC 주소 `civil-petition/deploy/render.py:450`(jdbc_url = db01 업무 주소 192.168.126.11:5432), 풀 `civil-petition/deploy/templates/was/petition-context.xml:9-15`(initialSize, minIdle 5, maxActive 40, maxWait 3000, testOnBorrow, validationInterval 30000. 2026-11-26 전에는 maxWait 10000). 읽기 경로는 `ApiExceptionHandler.java:23-26`(DataAccessException → ERROR 'database access failed: <원인>' 와 503 'database unavailable'), 신청과 담당자 처리는 `PetitionRepository.java:29, 79`(@Transactional: 트랜잭션을 열 때 연결을 못 얻으면 CannotCreateTransactionException 이 처리기 밖으로 나가 Spring Boot 오류 페이지 필터가 ERROR 와 500. 열린 풀 연결을 얻어 트랜잭션 첫 질의에서 재설정되면 DataAccessException 이라 503. 그래서 신청과 처리는 500 또는 503).
5. 웹: `civil-petition/deploy/templates/web/petition-site.conf:16`(ProxyPass balancer, timeout 30): WAS 가 몇 ms 안에 돌려준 503, 500 을 그대로 사용자에게. 균형기는 응답 상태로 WAS 를 빼지 않는다(failonstatus 없음).
6. 영향받지 않는 경로: 관제 흐름(SMS 보고 18090, syslog 514, ICMP 가 10.250.0.1 과 오감)은 보호 줄이 먼저 받는다. db01 의 SMS 보고, ICMP, 가용성은 그대로다. 운영자와 러너의 관리 ssh 는 관리 NIC 로 다닌다. 라우터의 NMS 폴링은 상향 주소로 받는다.

## 5. 원인 규정 (원칙 5, 6)

- 근본(`root_cause.target_id`): 라우터 rt-core01 의 NMS 대상 번호 `65b4ab9c-d1b4-42f6-be5c-7efb5c34cb3c`(target_kind `node`, `civil-petition/deploy/inventory/t.targets.json`). 결함은 라우터의 접근 규칙 설정이고 고쳐야 재발이 막히는 곳이다.
- 계기(`trigger_target_id`): 비움. 계기도 같은 곳(라우터 규칙 변경)이다.
- 원칙 6: 앱의 연결 시도와 PostgreSQL 의 처리는 늘 하던 그대로 정당하다. 잘못된 것은 그 사이를 막은 규칙이다. 원본도 방화벽 규칙을 원인으로 적었다(같은 층위).
- 채점: 입도 `node`, accept `rt-core01` 과 라우터 대상 번호. partial: db01(목적지만 본 답), was01, was02, web01(증상 서버만 본 답)의 이름과 대상 번호, 라우터 인터페이스 ens20, ens21 대상 번호(장비는 맞지만 링크 장애로 읽은 답).
- 원칙 5: 정답은 "rt-core01 의 접근 규칙 변경이 WAS 에서 DB 5432 로 가는 연결을 막았다" 이고 관제 데이터(라우터 syslog 의 운영자 명령과 커널 차단 줄, WAS 앱 로그의 연결 거부, db01 생존 근거, NMS ens21 송신 감소)로 낼 수 있다. 규칙 파일의 글과 운영자 의도는 119 에 없어 `beyond_data` 로 옮겼다.

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 | 기대(합격 조건 아님) |
|---|---|---|
| 감지(lucida-next) | 지금 이 도메인에서 이벤트가 되는 것은 지표 이상(stream-anomaly)뿐이다(공통 지시의 119 사실). 후보: 라우터 ens21 송신, 수신 트래픽(NMS 인터페이스 트래픽은 밴드가 있다) 급감, db01 네트워크 롤업 감소 | 이벤트, 이벤트 클러스터, 인시던트는 기대로만 적는다. 로그 감시 ERROR 줄, syslog, 연결 거부는 이벤트가 되지 않는다(§11) |
| 피해 판정(러너) | 상주 부하(loadgen VM) 요약의 접수번호 조회 비정상 비율 ≥ 0.5, 3틱(§9-3, 로그인 없는 경로라 장애 내내 값이 실림) | 평시 조회 약 0.01(없는 번호 1% 고정, 30초 창 최대 약 0.055) |
| RCA(합격 기준) | §7 의 SRE 길: 사용자 피해 줄 → WAS 연결 거부 줄 → db01 생존 → 라우터 운영자 명령과 커널 차단 줄 → NMS ens21 송신 감소 | 각 단계 평시 데이터가 119 에 있음을 조회로 확인(§7, §8) |

## 7. SRE 가 데이터로 원인을 찾는 길 (합격 기준)

알람과 AI 설정과 상관없이 119 에 쌓인 데이터만으로 간다. 장애 때 모양은 모두 "시험 실행에서 확인"(흔적 표 실측 전)이다. 평시 값은 2026-11-25 KST 조회.

| 단계 | 질문 | 어느 표, 칸, 대상 번호 | 평시 값(119 실조회) | 장애 때 모양 |
|---|---|---|---|---|
| 1 | 사용자가 실패하나 | CH `lucida.lucida_logs_local`(`event_source='log_monitor'`), web01 접근 로그 감시 `d510390b-d2d3-4da4-9f1b-64af8cd16bdd`(패턴 `" 5[0-9][0-9] `), 웹 오류 로그 `66b554a1-5bdd-4452-bbbf-ed2a24877701` | 7일 접근 로그 5xx 6줄, 오류 로그 18줄(11-23 WAS 재배포 미끼 때 AH00957, AH01114) | 시험 실행에서 확인. 기대: /petition/api 요청마다 503, 500 줄(초당 5~30) |
| 2 | 어디서 실패하나 | was01 앱 로그 `5aad9fbb-ec04-44b0-ba02-f81ced10af73`, was02 앱 로그 `e466fa3e-c26a-41a6-9c04-90e840b6df21`(패턴 ` ERROR ` 또는 `Exception`), Tomcat 접근 로그 `69ebf84f-81c2-42f0-b45d-a49f93804e1f`, `63de5bcc-f7cf-478b-ba1e-d9f044a70730` | 7일 앱 로그 ERROR 0줄(WARN 은 야간 배치 느린 요청 25, 35줄), Tomcat 접근 로그 5xx 0줄 | 시험 실행에서 확인. 기대: 두 WAS 같은 초에 'database access failed: java.net.ConnectException: Connection refused', 'Caused by: org.postgresql.util.PSQLException: Connection to 192.168.126.11:5432 refused', 신청 경로는 'Forwarding to error page ... Could not open JDBC Connection for transaction' |
| 3 | DB 가 죽었나 | VM `sms.server.icmp_status{host_name="192.168.126.11"}`, `sms.server.rtt`, `sms.cpu.utilization`(db01 대상 `73343434-0302-468f-b738-643aec7ab84b`), CH `lucida.process_snapshot`(host_name 192.168.126.11, name postgres), PostgreSQL 로그 감시 `17a5d386-4f83-42c2-9e05-e7eecbafeb74`, CH `syslog_local`(db01) | ICMP 3시간 내내 1, RTT 4.31~13.3ms(중앙 5.38), CPU 0.98~1.38%, postgres 프로세스 16개 pid 그대로(1시간 60표본), PostgreSQL 로그 감시 줄 0 | 시험 실행에서 확인. 기대: 모두 평시 그대로(살아 있음). 연결 표본에서 db01 쪽 postgres ESTABLISHED 는 WAS 마다 5(평시)에서 그대로 남고(재설정은 WAS 쪽만 받는다) WAS 쪽 java → 192.168.126.11:5432 는 사라질 것 |
| 4 | 무엇이 바뀌었나 | CH `lucida.syslog_local`(source_ip 192.168.123.254, hostname rt-core01), app_name sshd, sudo, kernel | 7일 라우터 sudo 명령: `nft list ruleset` 30, `tail -n` 27(평시 운영자 세션), 설치 작업 줄 몇 개. `nft -f` 0. 라우터 kernel 줄 7일 3줄(conntrack 가득 참 시험 1, apparmor 2), DPT 를 담은 줄 0 | 시험 실행에서 확인. 기대: 실패 시작 직전 'Accepted publickey for sysop from 192.168.127.5', 'COMMAND=/usr/sbin/nft -f /home/sysop/db-acl.nft', 그 뒤 '[DB ACL] IN=ens20 OUT=ens21 ... SRC=192.168.125.11 DST=192.168.126.11 ... PROTO=TCP ... DPT=5432' 분당 최대 6줄, cleanup 의 'nft delete table inet db-acl' |
| 5 | 길이 어디서 끊겼나 | VM `nms.interface.if_out_octets`, `if_out_ucast_pkts`, `if_in_octets`, `if_oper_status`(rt-core01 ens21 `79a3ac72-fde1-400a-89ca-bcbf414a4b92`, ens20 `8dd90421-b8fa-435d-b3fd-5fd5be103296`), CH `snmp_traps_local` | 3시간 5분 표본: ens21 송신 옥텟 90K~131K(중앙 109K), 송신 유니캐스트 80~111(중앙 92), 수신 옥텟 215K~332K, ens20 수신 옥텟 451K~685K. 라우터 NMS ICMP 1 | 시험 실행에서 확인. 기대: ens21 이 올라간 채(trap 없음) 송신이 거의 0, 수신은 db01 의 SMS 보고와 syslog 몫만 남음. ens20 수신은 거의 그대로(WAS 가 보낸 시도가 라우터에 들어와서 버려짐) |

조회 근거(모두 분할 칸과 시각 범위를 건 읽기 전용, 104 의 readonly=2 사본):

```
-- 단계 1, 2: 7일 로그 감시 줄 수
SELECT host_name, target_id, severity_text, count() FROM lucida.lucida_logs_local
WHERE timestamp_date >= today()-7 AND timestamp > now() - INTERVAL 7 DAY AND event_source='log_monitor'
  AND host_target_id IN ('c5f7161d-...','bda0dfa8-...','ebdf15e2-...','73343434-...') GROUP BY 1,2,3
-- 단계 3: 프로세스 표본
SELECT host_name, name, user, uniqExact(pid), uniqExact(ts) FROM lucida.process_snapshot
WHERE ts > now() - INTERVAL 1 HOUR AND host_name IN ('192.168.126.11','192.168.125.11') AND name IN ('postgres','java') GROUP BY 1,2,3
-- 단계 3: 연결 표본
SELECT host_name, direction, process, state, remote_addr, count(), uniqExact(timestamp) FROM lucida.host_connections
WHERE timestamp > now() - INTERVAL 1 HOUR AND (local_port=5432 OR remote_port=5432) GROUP BY 1,2,3,4,5
-- 단계 4: 라우터 sudo 명령 꼴
SELECT hostname, extract(message, 'COMMAND=(\S+ ?\S*)'), count() FROM lucida.syslog_local
WHERE received_at > now() - INTERVAL 7 DAY AND source_ip LIKE '192.168.12%' AND app_name='sudo' GROUP BY 1,2
-- 단계 5: VictoriaMetrics query_range nms.interface.if_out_octets{target_id="79a3ac72-..."} 3시간, step 300
```

표본 데이터 여부: 1~4 단계는 전수 원천(로그 감시 줄, syslog, 60초 프로세스 표본의 전체 목록, SMS 지표)이고 5 단계는 60초 NMS 폴링이다. 트레이스, APM 은 이 도메인에 없다.

## 8. 관측 근거 요약과 감별

- must_support(정답지 6개): 라우터의 운영자 로그인과 `sudo nft -f`, 커널 '[DB ACL] ... DPT=5432', 두 WAS 의 연결 거부 ERROR, 웹과 Tomcat 접근 로그 5xx, db01 생존(ICMP, postgres pid, CPU, PostgreSQL 로그), ens21 송신 감소와 링크 올라감.
- must_rule_out(정답지 5개): PostgreSQL 다운이나 재시작(오류 문구 'refused' 가 그렇게 보이지만 pid 와 로그가 아니라고 함), 링크나 인터페이스 장애(C1 꼴, 링크 올라감, trap 없음, db01 ICMP 정상), WAS 배포나 설정 변경(미끼 war_redeploy 꼴, java pid 그대로, 두 대가 같은 초), DB 과부하나 연결 고갈(바로 난 거부, 기다림 없음), 웹 서버 설정 변경이나 장애(미끼 config_reapply 꼴).
- contrast_with: F72-R(같은 원본, 새 연결만 버려 풀 수명만큼 늦게, ORA-12170), F44-R과 F44-P(서비스 API 포트), F71-H(pg_hba, DB 가 받아서 FATAL).
- 러너 배제 조건: 상주 부하가 끊김(achieved_rps < 2.0).
- 장부에서의 자리(1회차 평가 권고, 기록): 같은 묶음, 기전, 정답 위치(K+G, 노드, 디스크)의 넷째다(F44-R, F44-P, F72-R 다음). 숫자 상한은 통과한다. 앞의 셋과 가르는 근거는 §2 대로 장치(계층 사이 라우터), 전파(연결 상태를 보지 않는 재설정이라 바로 전면), 흔적(라우터 syslog 와 NMS, 'Connection refused')이다.
- 미끼와의 관계: 이 도메인에는 U7 미끼(운영자 경로의 WAR 재배포, 웹 설정 재적용)가 평시에 섞인다. 미끼는 WAS 와 웹에서만 나고 라우터에서는 나지 않으며, 라우터의 평시 운영자 세션은 `nft list ruleset` 같은 조회뿐이라 `nft -f` 는 이 주입에만 있다. 단 "장애 근처 라우터 운영자 변경 = 원인" 이 단순 규칙이 되지 않는지는 C1, C3 같은 라우터 카드가 쌓인 뒤 다시 본다(사람이 정할 것).

## 9. 러너 판정과 강도, 피해 계산 (원칙 9)

### 9-1. 피해가 나는 이유: 길이 막힌다

- 평시(근거 `~/dev/testbed-ops/v4/agent-out/APT2-facts/` 의 q3-no-db.txt, k6 창): 24시간 1,644,767 요청(평균 약 19 rps, 설계 피크 30, 저점 5), 비정상 0, 피크 p95 최대 8.6ms. db01 CPU 약 1%, WAS CPU 약 1.6%.
- 부하기의 모든 요청은 `/petition/api/...` 이고 모든 경로가 DB 를 쓴다(로그인 app_user 조회, 신청과 처리는 쓰기 트랜잭션, 조회, 목록, 검색, 담당자 함, 통계는 읽기). 그래서 길이 막히면 정적 첫 화면을 뺀 사용자 요청이 100% 실패한다. CPU 계산(앞선 카드의 함정)과 무관하다: 피해는 "자원 경쟁"이 아니라 "연결이 맺어지지 않음" 에서 난다.
- 실패 시간: TCP 재설정이라 연결 시도가 라우터 왕복(1ms 안팎) 만에 'Connection refused' 로 끝나고 앱이 몇 ms 안에 503(신청과 처리는 500 또는 503)을 낸다(§4 의 커널 실측 0.0초).
- 풀: WAS 마다 유휴 5개(minIdle 5, 평시 db01 쪽 ESTABLISHED 가 WAS 마다 5 로 고정)는 다음 사용 때 검증 질의(`SELECT 1`, 30초마다)나 업무 질의에서 재설정되어 버려지고, 새로 맺기는 바로 거부된다. 실패한 생성은 풀 크기에서 빠지므로 maxActive 40 에 닿지 않고 maxWait(10초) 대기도 생기지 않는다(WAS 당 동시 연결 시도 = 초당 2.5~15건 × 몇 ms 로 1 미만).
- 문턱: 설계 §6-3 피해 판정 "사용자 비정상 비율 5% 이상 2분 지속" 에 대해 약 100% 가 10분. 감시 패턴에 걸리는 피해 줄은 웹 접근 로그 5xx(`" 5[0-9][0-9] `), Tomcat 접근 로그 5xx, 앱 로그 ERROR 와 `Exception` 줄이다. 3초 이상 느린 요청 WARN 은 생기지 않는다(빨리 실패한다).

### 9-2. drop 이 아니라 reset 인 이유 (카드와 달라진 점 1)

카드는 "WAS DB 연결 시간 초과 줄" 을 적었다. 그대로 drop 으로 버리면:
- PgJDBC 연결 시간 제한 기본 10초, 풀 maxWait 10초가 부하기 요청 시간 제한 10초(`civil-petition/loadgen/script.js` TIMEOUT)와 같아, 사용자 쪽은 대부분 503 이 아니라 "응답 없음(0)" 으로 끝난다. 그러면 러너의 필수 중단 조건(entry_status 0, compile-plan 이 모든 controller 에 요구)이 2틱 만에 run 을 끊는다.
- (2026-11-26 갱신) 사용자 결정으로 앱 평시 설정에 DB 기다림 상한 3초(connectTimeout, loginTimeout, maxWait, validationQueryTimeout)가 들어갔다. 위 문장은 그 전 설정 기준이다. 이 시나리오는 재설정(RST)이라 바로 거부되어 결과가 바뀌지 않는다.
- 일반 reject(ICMP port unreachable)는 커널이 목적지마다 초당 약 1개로 제한해(net.ipv4.icmp_ratelimit 1000ms, icmp_ratemask 에 dest unreachable 포함, nftables 의 일반 reject 기본이 ICMP 임을 104 커널에서 확인) WAS 당 초당 2.5~15건 시도 대부분이 다시 시간 초과가 된다.
- 그래서 `reject with tcp reset`(새 판정 `reset`, tcp 만)을 쓴다. 실제 운영자가 TCP 포트를 막을 때 흔히 쓰는 꼴이고, 흔적은 'Connection refused' 로 바뀐다. 카드의 뜻(운영자가 라우터 접근 규칙을 잘못 고쳐 WAS → DB 길이 막힘)은 그대로다.

### 9-3. 판정과 강도

- 강도: 하나로 고정(`approved-fixed-f72-p`). 규칙은 켜거나 끄는 것뿐이라 사다리가 없다. 동반 부하 없음: 저점(5 rps)에서도 접수번호 조회가 초당 1.5건(30초 창 약 45건) 모두 실패해 판정이 선다.
- success: 상주 부하(domain civil-petition) 접수번호 조회 비정상 비율 ≥ 0.5, 3틱(45초). min_hold 10m, settle 60s, level timeout 15m, max_injection_duration 20m. 신청 단계 비율은 쓰지 않는다(§9-4).
- must_rule_out: achieved_rps < 2.0(저점 목표의 40%, 부하가 끊겼을 때만).
- abort: entry_status(같은 도메인 상주 부하의 마지막 신청 상태) = 0, 2틱. reset 이라 신청은 500 이나 503 으로 바로 답해 0 이 되지 않는다. 장애 중 신청이 사라져도(§9-4) 모니터는 마지막 신청 상태를 그대로 싣는다(창으로 자르지 않는 값).
- recovery: target_health 200(전역), entry_status 200(로그인이 풀렸던 가상 사용자가 다시 로그인한 뒤의 첫 신청), 조회 비정상 비율 < 0.1(평시 30초 창 최대 약 0.055 라 0.05 는 저점에서 걸릴 수 있다), 2틱, 10분. 쿠버네티스 시나리오의 pod_ready 대신 진입 상태를 본다(compile-plan 의 VM 도메인 회복 규칙, 이번에 더함, §12).
- preflight: coordinator-clean, clean-window, baseline-traffic, target-health, vm-entry-health. 이 도메인의 VM preflight(설계 §6-4: 터널, 신선도, 라우터 부하, 관리 연결, 보관 배치 상태)는 러너가 도메인으로 고른다.
- 시각(예상, §9 계산): t0 운영자 로그인과 `nft -f`(몇 초) → t0 + 1초 안에 첫 실패 → 3틱 뒤 success → t0 + 10분 cleanup 운영자 로그인과 `nft delete table` → 몇 초 안에 회복.
- 안전: 표는 forward 의 192.168.126.11:5432 TCP 만 거절한다. 내용 가드(설계 §3-5 규칙 6)가 관제 터널, 관리망, 라우터 주소를 먼저 받고 적용 직후 66 의 터널 핑과 표 확인을 한다. 라우터 자기 되돌림 가드(U8)가 적용보다 먼저 걸린다.
### 9-4. 장애 중 로그인 상태 소실과 판정 신호 (1회차 평가 차단 1)

- 부하기(`civil-petition/loadgen/script.js`)는 가상 사용자마다 시민과 담당자 세션을 하나씩 들고 있다. 신청(8%), 내 민원(25%)은 `ensureCitizen()`, 담당자 처리(11%)는 `ensureStaff()` 가 로그인 상태를 먼저 보고, 풀렸으면 로그인 요청만 보내고 끝난다. 재로그인 여정(5%, 반은 시민 반은 담당자)은 `login()` 결과로 상태를 덮어쓴다.
- 장애 중 로그인도 DB(app_user 조회)를 거쳐 503 이라, 재로그인 여정을 만난 가상 사용자는 로그인 상태가 풀리고 장애가 끝날 때까지 다시 서지 않는다. 그래서 신청 단계 요청이 몇 분 안에 사라진다(평가자 모의: 10~15분 30초 창 10개 중 신청이 있는 창이 19 rps 에서 20회 중 19회 0개, 30 rps 에서 20회 모두 0개). 모니터는 신청이 0건인 창에 그 비율을 싣지 않고 러너는 unusable 로 본다. 1회차 설계의 성공 조건(신청 비율 ≥ 0.5)은 그래서 버렸다.
- 접수번호 조회(30%)와 공개 검색(15%), 통계(6%)는 쿠키 단지 없이 부르는 공개 경로라 로그인 상태와 무관하게 계속 나간다. 성공 조건은 조회 비율 하나다.
- 같은 꼴 모의(`/home/sjbang/tmp/claude-1008/-home-sjbang-dev-lucida-next/ee261603-595a-4ca8-a253-3a4bd5d2bb18/scratchpad/f72p_lookup_sim.py`, 평가자 모의와 같은 도착 꼴, 같은 누적 비중표, 장애 0~15분, 30초 창 30개 x 20회): 조회 0건 창은 5, 10, 19, 30 rps 모두 0개, 창당 조회 최소 27, 70, 143, 230건, 장애 중 비정상 비율 최소 1.00. 평시 같은 모의의 창별 비정상 비율 최대 0.055, 0.044, 0.031, 0.023(없는 번호 404). 그래서 문턱 0.5 는 장애와 평시를 넉넉히 가르고 회복 문턱은 0.1 로 둔다.
- achieved_rps(배제 조건)는 반복 수라 로그인 소실과 무관하다(로그인이 풀린 신청 여정도 로그인 요청 하나를 보내는 반복이다).

- 119 부담: 실패 요청마다 감시 패턴에 걸리는 줄이 약 6줄(웹 접근 1, Tomcat 접근 1, 앱 ERROR 1, 스택의 Exception 줄 약 3)이라 10분에 약 1.8만~10.8만 줄(저점~피크). 처음 실행에서 로그 감시 수집 지연과 119 ClickHouse 메모리 예산(설계 §3-6)을 함께 본다.

### 9-5. 첫 실행 확인 항목 (아직 실측 없음)

- 라우터 커널 '[DB ACL] ... DPT=5432' 줄(nf_log)이 119 `syslog_local` 에 닿는지. 라우터 kernel 줄은 conntrack 가득 참 줄로 닿는 것만 확인됐다.
- NMS 라우터 ens21 송신 옥텟과 패킷이 실제로 거의 0 으로 떨어지는지(60초 폴링), 수신은 db01 SMS 보고 몫만 남는지.
- 라우터 운영자 sudo `nft -f` 줄이 UDP syslog 에서 빠지지 않는지(F44-P 에서 몰린 sudo 줄 유실 선례).
- 두 WAS 앱 로그 ERROR 와 Exception 줄 꼴과 수(설계 예상 약 6줄 x 요청 수), 로그 감시 수집 지연.
- 장애 중 상주 부하 조회 비정상 비율이 1.0 근처로 실리고(§9-4), 신청 단계가 실제로 사라지는지.
- (2회차 평가 권고) 장애 구간 중 k6 재시작(1시간 주기, 확률 약 20%)이 나면 setup() 이 가장 최근 번호를 SEED_PETITIONS(200만)로 잡는다. 가장 최근 번호가 약 400만을 넘은 뒤에는 회복 직후 조회가 가상 사용자마다 자기 신청이 성공할 때까지 404 가 되어 lookups-recovered(< 0.1)가 4~5분 늦어질 수 있다(회복 제한 10분 안).
- (2회차 평가 권고) cleanup 뒤 db01 쪽 옛 백엔드 10개(재설정은 WAS 쪽만 받음)가 TCP keepalive 기본값(약 2시간)까지 남아, 연결 표본과 postgres 프로세스 수가 평시 10, 16 이 아니라 20, 26 으로 보일 수 있다.
- (2회차 평가 권고) nf_log '[DB ACL]' 줄과 운영자 sudo 줄의 119 도달(UDP 유실 여부)을 실측한다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Harness 2024-09-01, 링크)와 요소별 대응표가 있고 기전이 같다. 지연 발현만 재현하지 않으며 그 차이가 P 의 근거다(§2) (원칙 1)
- [x] 분류 장부를 갱신했다(§4-1 행, §6 기록). 묶음 K 3 → 4(3%), 정답 위치 노드, 디스크 8 → 9(7.6%), 결제 경로 14/119(11.8%, 무관), 민원 0 → 1. 어느 축도 20% 에 닿지 않고 단순 규칙 판정은 보류(통과)다(§3) (원칙 2)
- [x] 근본 원인 위치가 인프라 지점(rt-core01 nftables forward, 표 fwacl 앞 우선순위, 인벤토리 acl)과 코드 위치로 확인됐다(§4) (G1)
- [x] 근본 원인의 흔적 자리가 119 실데이터에 있다: 라우터 syslog 의 운영자 sshd, sudo 줄(평시 세션 7일 30+27줄 수집)과 kernel 줄(conntrack 가득 참 줄 수집됨), NMS ens21 지표. 장애 때 줄은 시험 실행에서 확인(§7) (원칙 3)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: syslog, 로그 감시, SMS 지표, NMS 폴링(§7) (원칙 3)
- [x] 계기의 흔적이 있다: 운영자 로그인과 `sudo nft -f`(평시 라우터에 없는 명령), 차단된 패킷마다 커널 '[DB ACL]' 줄. 인공 지연 없음 (원칙 4)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다(§5) (원칙 5)
- [x] 정답지 세 칸이 원칙 6대로 채워졌다(§5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다(§6) (원칙 7)
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: 표 이름 db-acl, 파일 /home/sysop/db-acl.nft, 로그 접두어 '[DB ACL] ', 가드 작업 cfg-db-acl. 실행기 계정 줄은 라우터 rsyslog 첫 규칙이 거르고 운영자 줄만 간다. compile-plan 으로 만든 실행기 입력에 F72 가 없음을 확인 (원칙 8)
- [x] 부하 상한과 타임아웃 사슬(연결 10초, 풀 10초, 부하기 10초, 웹 30초)을 넣은 피해 계산이 있다(§9) (원칙 9)

## 11. 119 보강 후보 (고치지 않는다, 지휘 세션 몫)

공통 지시의 119 사실(11-25): 이 도메인에서 이벤트는 지표 이상(stream-anomaly)만 생기고, 로그 감시 줄(ERROR 포함), syslog, trap, 가용성 DOWN, 인터페이스 상태는 이벤트가 되지 않으며, 한 서버 안 여러 시리즈 지표는 이상 판정하지 않는다.

| 근거 | 지금 | 보강하면 |
|---|---|---|
| 라우터 syslog 의 운영자 설정 적용(`sudo ... nft -f`) | 이벤트 안 됨 | 장비 설정 변경 이벤트(변경 감지)로 서면 "변경 = 원인" 후보가 시각과 함께 선다 |
| 라우터 kernel 차단 줄('[DB ACL] ... DPT=5432') | 이벤트 안 됨 | syslog 줄 급증(log-anomaly)으로 서면 길이 끊긴 장비가 이벤트에 남는다 |
| 두 WAS 앱 로그 ERROR 급증(평시 0) | 로그 감시 ERROR 줄은 이벤트 안 됨 | 로그 감시 원천의 오류 급증 이벤트가 서면 사용자 피해가 인시던트 재료가 된다 |
| 웹, Tomcat 접근 로그 5xx 급증 | 같음 | 같음 |
| NMS ens21 송신 급감 | 밴드가 있어 지표 이상 후보(감소 방향을 잡는지는 모름) | 이 시나리오에서 이벤트가 생길 거의 유일한 길. 시험 실행에서 이벤트 여부를 본다 |

## 12. 이번에 바꾼 공용 부분 (지휘 세션 검토)

1. `vm_executor_common.py`: 판정 `reset`(`reject with tcp reset`, tcp 만)을 더함. 기존 drop, reject 는 그대로. 시험: `test_a_reset_verdict_is_tcp_only_and_renders_a_tcp_reset`(tcp 가 아니면 거절, 글 모양, 실제 nft 적재), `test_f72p_router_table_refuses_forwarded_db_connects_at_once_and_cuts_open_ones`(실제 커널 네임스페이스 전달 시험).
2. `router_config_executor.py`: 첫 계약 F72-P 와 설명. `registry/profiles.json` router.config 를 live_supported true, 같은 계약으로.
3. `tests/test_ready_profile_executors.py` `test_registry_places_every_executor_on_its_vms`: "모든 VM 실행기의 계약은 비어 있다" 를 "실행기 계약 = 레지스트리 계약(양방향), 계약이 있을 때만 live_supported" 로 바꿈(첫 계약이 들어온 뒤의 같은 뜻, 계약 없는 시나리오 거절은 그대로 시험). 같은 파일 `NodeGuardTests`: router.config 가 이제 카탈로그 시나리오를 가져 "아직 안 쓰는 노드 실행기" 견본 목록에서 빼고(시험이 그렇게 요구한다) 카탈로그 쪽 가드 점검 집합에 더함, VM 실행기 시험이 쓰는 견본은 `USED_NODE_SAMPLES` 로 옮김. `tests/test_load_north_south_executor.py` 라이브 프로파일 목록에 router.config 추가.
4. `compile-plan.py`: VM 도메인(civil-petition, civil-petition-sms) controller 는 회복 조건에 pod_ready 대신 그 도메인 상주 부하의 entry_status = 200(http.entry_health, 같은 domain)을 요구하고 kubernetes 관측을 금지. 쿠버네티스 시나리오 규칙은 그대로. 시험 `test_vm_domain_recovery_reads_the_domain_entry_instead_of_a_pod`.
5. `generate-manifests.py`: 주입 위치 `civil-petition <VM>` 을 ssh 전송으로, router.config 의 preflight 를 network-oob-recovery 로.
6. 러너 저장소(커밋 안 함) `backend/tests/test_external_live_manifests.py` `test_every_live_controller_observation_passes_the_probe_allowlists`: 가짜 전송이 VM 읽기 앞의 로컬 ControlMaster 확인(`ssh -O check`)까지 막아 허용 목록을 통과한 관측을 "연결이 끊김" 문제로 셌다. 그 로컬 확인만 성공으로 답하게 해 가짜 전송이 실제 읽기에서 닿게 고침(러너 앱 코드 변경 없음, 재빌드 불필요).
