---
title: F82-R 설계 시트, 민원 포털 라우터에서 운영자가 WAS 쪽 포트에 속도 제한을 너무 낮게 걸어 큐가 넘침
status: Draft
owner: project
last_reviewed: 2026-11-27
tags:
  - scenario
  - design
  - network
  - civil-petition
summary: 민원 포털(SMS, NMS 전용 전통 3계층 테스트베드) 라우터 rt-core01 에서 운영자가 WAS 쪽 포트 ens20 송신에 400 kbit/s 속도 제한(tbf)을 거는데, 바쁜 시간 그 포트 트래픽(60초 평균 약 424 kbit/s)이 그보다 조금 많아 큐가 넘치고 패킷이 버려져, 웹의 요청과 DB 응답이 was01, was02 에 늦게 닿고 두 연결 풀이 차 포털 요청 대부분이 3초 넘게 걸리며 일부는 503, 500 이 되는 시나리오(Google Compute Engine 2015-03-07 재구성)의 설계 근거. 사용자와 정한 카드 C3.
---

# F82-R 설계 시트

- id `F82-R`, slug `f82-r-petition-router-was-rate-limit-queue-overflow`, 도메인 `civil-petition`(SMS + NMS 테스트베드)
- 사용자와 정한 카드: C3 "운영자가 라우터 WAS 망 포트에 속도 제한을 너무 낮게 걸어 큐 넘침"(`~/dev/testbed-ops/v4/scenario-design/cards.md`)
- 상태: 후보(ready + `stage: candidate`), 설계 강도 하나로 고정한 evaluation 모드, 녹화 대기 큐에 추가(note: 새 테스트베드 첫 시나리오, 흔적 표 실측과 119 보강 뒤 녹화, 3초 설정 적용과 러너 새 관측 배포 확인, KST 16~18시)
- 주입 수단: 기존 실행기 `router.qdisc` 에 새 모드 `operator-rate`(운영자 경로, 부하 창 preflight)와 첫 계약. 기존 모드(rate, netem)의 동작과 파라미터는 그대로. 동반 부하 없음(상주 부하가 피해를 낸다)
- ID: 같은 라우터의 F72 사례군(F72-R, F72-P, F72-H)은 "WAS 에서 DB 로 가는 길이 막힘"이고 R, P, H 가 다 찼다. 이 카드는 원본(Google 2015-03-07)과 기전(큐 넘침)이 새로워 새 사례군 F82 의 R 로 둔다. 이 실행의 다른 설계 세션과 번호가 겹칠 수 있다(지휘 세션이 rebase 때 확인).
- 러너 변경: 새 관측 `loadgen.http_p95_ms`(러너 `LOADGEN_FIELDS` 한 줄, `observation_queries.json` 한 항목, 커밋 안 함, 러너 재빌드 필요, §12)

## 1. 요약과 비유

민원 포털은 웹(web01), WAS 두 대(was01, was02), DB(db01) 가 라우터(rt-core01) 하나로 이어진 전통 3계층이다. 라우터의 WAS 쪽 포트 ens20 은 라우터가 WAS 망으로 보내는 것 전부, 곧 웹 서버가 넘기는 요청과 DB 서버가 돌려주는 질의 응답을 나른다. 바쁜 시간(KST 16~18시) 이 포트는 60초 평균 약 424 kbit/s 를 보낸다.
운영자가 라우터에 로그인해 이 포트 송신에 토큰 버킷 속도 제한을 건다: `sudo tc qdisc add dev ens20 root handle 1: tbf rate 400kbit burst 32kb latency 400ms`. 400 Mbit 상한을 뜻했다면 단위 한 글자(m 대신 k)를 잘못 친 꼴이고, 그 값이 하필 바쁜 시간 트래픽보다 조금 작다.
큐가 넘쳐 패킷이 버려지고 나머지는 큐에서 기다린다. 요청과 DB 응답이 WAS 에 늦게 닿아 요청마다 DB 연결을 몇 초씩 쥐고, 두 연결 풀(각 40)이 차서 나머지 요청은 3초 기다린 뒤 실패한다. 포털은 느려지고(요청 대부분 3~10초) 일부는 503, 500 이다. DB 서버와 웹 서버는 한가하고 핑도 빠르다. WAS 두 대의 핑만 약 1초로 늦다.

비유: 건물 안쪽 사무실(WAS)로 들어가는 복도에 "한 번에 몇 사람만" 이라는 통제선을 쳤는데, 숫자를 잘못 적어 점심시간 인파보다 조금 적다. 복도 앞에 줄이 늘고 몇은 돌아가며, 안에서는 서류(DB 응답)를 기다리는 직원이 창구를 붙잡고 있어 창구도 꽉 찬다. 금고실(DB)과 정문(웹)은 한가하다.

## 2. 원본 사례

### 2-1. 고른 사례: Google Compute Engine, 2015-03-07

- 상태 페이지 사고 "Google Compute Engine Incident #15046"(GCE connection issue, 상세 보고 2015-03-08 게시), [공식](https://status.cloud.google.com/incident/compute/15046). 자료 문서 `ref-real-world-incidents.md` M19(레이트 리밋 오설정)에 이번에 더함(출처가 말한 사실만).
  - 09:55 ~ 10:38 PST(43분) VM 의 바깥으로 나가는 패킷이 간헐적으로 버려짐. 인터넷과 다른 Google 서비스로 가는 송신이 영향, "VM execution and VM-to-VM network traffic was unaffected during this interval."
  - 원인 문장: "The root cause of the packet loss was a configuration change introduced to the network stack designed to provide greater isolation between VMs and projects by capping the traffic volume allowed by an individual VM." 시험은 통과했으나 운영에서 "it affected some VMs in an unexpected manner".
  - 사용자 영향은 VM, 영역, 사용자 망에 따라 눈에 띄는 영향 없음부터 응답이 느려지거나 시간 초과까지. 탐지: 자동 망 감시가 18분 뒤 알림. 완화: 설정 변경 되돌리기(10:35 시작, 10:38 회복). 재발 방지: 운영과 같게 시험하고 망 변경은 작은 몫부터.

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | VM 마다 트래픽 양을 묶는(cap) 망 스택 설정 변경 | 운영자가 라우터 rt-core01 WAS 쪽 포트 ens20 송신에 tbf 속도 제한을 넣음(운영자 ssh 로그인, `sudo tc qdisc add ... tbf rate 400kbit ...`) |
| 원인이 된 결함 | 상한이 일부 VM 의 실제 트래픽에 예상과 다르게 걸림 | 상한 400 kbit/s 가 그 포트의 바쁜 시간 트래픽(약 424 kbit/s)보다 조금 작음(단위 실수로 보이는 값, 의도는 데이터 밖) |
| 전파 경로 | 상한을 넘는 송신 패킷이 버려짐 | 상한을 넘는 패킷이 큐에서 기다리다 넘치면 버려짐 → TCP 재전송과 느려짐 → 요청과 DB 응답이 WAS 에 늦게 닿음 → 풀 연결이 오래 묶여 두 풀이 참 |
| 사용자 증상 | 영향 없음부터 응답 지연, 시간 초과까지 | 요청 대부분 3~10초, 부하 창 안 모의에서 약 1.3~7.5% 실패(풀 기다림 3초 뒤 503, 500, 또는 사용자 10초 시간 초과). 한가한 시간이면 영향 없음(실행기 preflight 가 그 시각을 거절) |
| 원인 쪽 상태 | VM 실행과 VM 사이 트래픽은 정상 | 라우터, 포트(up), DB 서버, 웹 서버, WAS CPU 정상. 버림은 그 포트에만 |
| 탐지된 경로 | 자동 망 감시(패킷 손실) | 라우터 NMS ens20 송신 버림(ifOutDiscards)과 송신량이 제한에 붙음, WAS 핑 지연, 앱 느린 요청 WARN 과 풀 고갈 ERROR, 접근 로그 5xx, 라우터 syslog 의 운영자 tc 명령 |
| 완화와 복구 | 설정 변경을 되돌림 | 운영자가 큐 규칙을 지움(`sudo tc qdisc del dev ens20 root`), 밀린 것이 몇 초 안에 빠짐 |

기전 확인: 원인(트래픽 양을 묶는 망 설정 변경이 실제 트래픽보다 작게 걸림)에서 증상(송신 패킷이 버려져 응답이 느려지거나 시간 초과, 장비와 서버는 멀쩡)까지의 고리가 원본과 같다. 원본은 자동화된 설정 배포(VM 전체)이고 여기서는 운영자 한 명이 장비 하나에 친 명령이다(규모와 경로가 다르고 기전은 같다). 원본의 "VM, 영역, 사용자 망에 따라 영향이 달랐다"는 여기서 "부하가 상한보다 많을 때만 피해"로 재현된다.

### 2-2. 찾았지만 쓰지 않은 것

- Kolab Now 2026-11-06 "Incident report: Misconfigured traffic shaping"([블로그](https://blogs.kolabnow.com/2026/11/06/incident-report-misconfigured-traffic-shaping)): 회선 증설 뒤 트래픽 제어 정책을 고치다 실수, 약 29분 장애. 보고서가 값, 단위, 증상(손실, 지연)과 원인 확정을 적지 않고("remains to be determined"), 어긋남이 남용자를 막지 못한 쪽이라 이 카드(정상 트래픽을 너무 조임)와 맞음새가 약하다.
- Palo Alto 지식 문서(10G 포트의 QoS 송신 최대값이 100Mbps 로 걸려 장애)는 제품 안내라 공신력 있는 사후 보고로 쓰지 않았다.

## 3. 숫자 근거 (2026-11-27, `scenario-stats.py`, 정식 + 후보)

- 이번 실행에서 다른 점(공통 지시): 후보는 사용자와 정한 카드 10장이라 후보 넓게 뽑기를 하지 않는다. 숫자는 상한 확인용이다.
- 서비스: 설계 GT0 결정 3 (다)와 S 결정 4(T 와 S 한 몫, v4 시작 뒤 생성분만). 민원 4(F72-P, F80-P, F72-H, F81-R) → 5. `scenario-stats.py` 는 civil-petition 과 civil-petition-sms 를 따로 세고 v4 구간을 모르므로 손으로 셌다. 사용자가 두 테스트베드 카드 10장을 정해 서비스 균형으로 막지 않는다.
- 묶음(합계 122 → 123, 20% = 24.6): A 11, B 7, C 6, D 17, E 4, F 5, G 13, H 2, I 2, J 21, **K 5 → 6(5%)**, L 9, M 1, O 3, P 14, Q 2. 주 묶음 K(피해가 망 큐의 버림과 지연에서 난다), 계기가 운영자의 라우터 설정 변경이라 K+G(F44-R, F44-P, F72-R, F72-P 다음 다섯째). 현실 비중(네트워크 15%)에 비해 K 가 적다.
- 정답 위치: **노드, 디스크 11 → 12(9.8%)**(§2-1 '네트워크 등 인프라'). 결제 경로 14/123(11.4%, 무관).
- 같은 노드 쏠림(브리프 요구): rt-core01 이 정답인 것이 F72-P(K+G, 운영자 접근 규칙, 거부), F72-H(K, 링크 끊김, 변경 없음), F82-R(K+G, 운영자 큐 규칙, 버림과 지연)로 셋. 정답 위치 '노드, 디스크' 12 안에서 라우터 셋은 25%, 전체 123 의 2.4%. 기전 셋이 서로 다르고(거부, 끊김, 큐 넘침) 흔적도 다르다(§8). 단 라우터 운영자 변경이 원인인 것이 둘이고 라우터에는 U7 미끼가 없어 "라우터 운영자 변경 줄 = 정답" 지름길 위험이 있다(§13 사람이 정할 것).
- 단순 규칙 적중률(`--candidate F82-R`): 판정 보류(통과로 본다), 변경 원인 몫 33.3% → 30.0%(하한에 닿음, §10-1).
- 왜 이 후보인가: 사용자와 정한 카드. 결정 4(큐 넘침 버림만 라우터 ifOutDiscards 에 남김)를 처음 쓰는 시나리오이고, 같은 라우터의 F72-P(거부)와 F72-H(끊김)와 겉 증상이 겹치면서 "느림이 먼저, 실패는 일부"라 관제 AI 가 NMS 버림 카운터, 송신량 모양, 서버별 핑 지연을 읽어야 맞힌다.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 계기(라우터): 운영자 sysop 이 운영자 단말 주소 192.168.127.5 에서 rt-core01(관리 192.168.127.2)에 ssh 로그인(키, pty)해 `sudo tc qdisc add dev ens20 root handle 1: tbf rate 400kbit burst 32kb latency 400ms` 를 친다(`scripts/scenarios/profiles/router_qdisc_executor.py:51-63` CONTRACTS["F82-R"], `:203-205` op_apply_script). 평시 하위 NIC 의 root qdisc 는 기본값(noqueue, 설계 결정 9)이다.
2. 큐: tbf 는 ens20 송신을 400 kbit/s(초당 50 KB)로 내보내고, 그보다 많이 들어오면 큐에 담는다. 큐 크기는 rate x latency + burst 다. 커널 값으로 rate 50,000 B/s, burst 32,768 B 라 상한 약 52,768 B 이고, 가득 찬 큐의 대기는 약 1.06초다(latency 400ms 만이 아니라 burst 분량이 더해진다). 그래서 글에서는 "최대 약 1초"로 적는다. 들어오는 양이 계속 많으면 큐가 차고 넘친 패킷을 버린다. 라우터 snmpd 의 ifOutDiscards 도우미(`civil-petition/deploy/router/ifdiscards.py:62` qdisc_shares, 대상 NIC 는 `civil-petition/deploy/render.py:492` 의 하위 NIC)가 이 버림을 ens20 의 송신 버림 카운터에 더한다(설계 결정 4).
3. 무엇이 ens20 을 지나나: 라우터가 WAS 망(192.168.125.0/24)으로 보내는 것 전부. 웹의 프록시 요청(web01 → was01, was02 :8080), DB 응답(db01:5432 → WAS), WAS 로 가는 ACK, 관제 쪽 응답과 핑(10.250.0.1 → WAS). 바쁜 시간 60초 평균 약 424 kbit/s, 밤 약 100 kbit/s(§7 단계 4).
4. 앱 쪽: 풀 `civil-petition/deploy/templates/was/petition-context.xml:9-15`(minIdle 5, maxActive 40, maxWait 3초, connectTimeout, loginTimeout 3초, socketTimeout 없음). 요청 하나는 ens20 을 몇 번 지난다(요청 도착, 질의 응답마다). 늦게 닿는 만큼 요청이 풀 연결을 오래 쥐고, 두 풀이 차면 빌리기가 3초 뒤 실패한다: 읽기 경로는 `ApiExceptionHandler.java:23-26`(DataAccessException → ERROR 'database access failed: ... Unable to fetch a connection in 3 seconds ...' 와 503), 신청과 처리는 트랜잭션 시작 실패라 Spring Boot 오류 페이지로 500. 3초 넘은 요청마다 `SlowRequestFilter.java:17-30` 이 'slow request method= uri= status= took_ms=' WARN 을 남긴다(풀 3초 실패도 took_ms 3000 넘어 WARN).
5. 웹: `civil-petition/deploy/templates/web/petition-site.conf:16`(ProxyPass timeout 30), 균형기 멤버 `civil-petition/deploy/render.py:463-464`(connectiontimeout 3, retry 30). 웹 → WAS 새 연결의 SYN 도 ens20 큐를 지나 버려질 수 있어, 드물게 연결 3초 실패(AH00957, AH01114)와 503 이 난다(모의 18만 요청 가운데 2번, §9-3).
6. 부하기: `civil-petition/loadgen/script.js:52` TIMEOUT 10초, 가상 사용자 최대 150(constant-arrival-rate). 10초 넘은 요청은 응답 없음(0)으로 끝난다.
7. 영향받지 않는 것: db01, web01 으로 가는 길(ens21, ens19)과 그쪽 핑. 라우터 자기 CPU. 관리 NIC(ens22)라 운영자, 러너 ssh 와 cleanup 은 막히지 않는다.

## 5. 원인 규정 (원칙 5, 6)

- 근본(`root_cause.target_id`): 라우터 rt-core01 의 NMS 대상 번호 `65b4ab9c-d1b4-42f6-be5c-7efb5c34cb3c`(target_kind `node`). 결함은 라우터 ens20 의 큐 설정값이고 고쳐야 재발이 막히는 곳이다.
- 계기(`trigger_target_id`): 비움(같은 곳의 설정 변경).
- 원칙 6: 앱의 요청, DB 응답, 웹의 프록시는 늘 하던 그대로 정당하다. 잘못된 것은 그 길을 너무 좁게 묶은 설정값이다. 원본도 "트래픽 양을 묶는 설정 변경"을 원인으로 적었다(같은 층위).
- 채점: 입도 `node`, accept `rt-core01 ens20` 과 그 인터페이스 대상 번호 `8dd90421-b8fa-435d-b3fd-5fd5be103296`, `rt-core01` 과 라우터 대상 번호. 결함이 그 포트의 송신 큐 설정이라 포트와 장비 둘 다 정답이다(F72-H 가 원인 인터페이스 ens21 을 accept 에 둔 것과 같은 꼴, 1회차 평가 차단 1). partial: was01, was02(증상과 핑 지연이 보이는 서버), db01(풀 고갈을 DB 탓으로 읽은 답), web01(5xx 를 낸 앞단).
- 원칙 5: 정답은 "rt-core01 의 큐 규칙(속도 제한)이 WAS 쪽 포트를 바쁜 시간 트래픽 아래로 묶어 패킷이 버려지고 늦어졌다" 이고 관제 데이터(운영자 tc 명령 줄, ens20 버림과 평평한 송신량, WAS 쪽만 핑 지연, 앱 느린 요청과 풀 고갈 줄)로 낼 수 있다. 값을 왜 그렇게 넣었는지(단위 실수)와 큐 통계는 119 에 없어 `beyond_data` 로 옮겼다.

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 | 기대(합격 조건 아님) |
|---|---|---|
| 감지(lucida-next) | 이 도메인에서 이벤트가 되는 것은 지표 이상(stream-anomaly)뿐(공통 지시의 119 사실). 후보: 라우터 롤업 `nms.interfaces.if_out_discards`(평시 7일 0, 밴드가 있다), 인터페이스 ens20 `if_out_discards`, 송신량 모양, `sms.server.rtt`(was01, was02) | 이벤트, 클러스터, 인시던트는 기대로만. 로그 감시 WARN, ERROR 줄과 syslog 는 이벤트가 되지 않는다(§11) |
| 피해 판정(러너) | 상주 부하 모든 요청의 30초 창 p95 ≥ 3000ms, 3틱(새 관측 `loadgen.http_p95_ms`) | 평시 p95 약 8~10ms. 모의 장애 중 창별 2.9~10초 |
| RCA(합격 기준) | §7 의 SRE 길 | 각 단계 평시 데이터가 119 에 있음을 조회로 확인 |

## 7. SRE 가 데이터로 원인을 찾는 길 (합격 기준)

알람과 AI 설정과 상관없이 119 에 쌓인 데이터만으로 간다. 장애 때 모양은 모두 "시험 실행에서 확인"(흔적 표의 router.qdisc 큐 계열 줄 r14 는 ens19 512kbit 가 넘치지 않아 버림, 지연이 안 남은 실측뿐). 평시 값은 2026-11-27 KST 조회(11-25 ~ 11-26 은 T4 시험 구간이라 평시에서 뺀다).

| 단계 | 질문 | 어느 표, 칸, 대상 번호 | 평시 값(119 실조회) | 장애 때 모양 |
|---|---|---|---|---|
| 1 | 사용자가 느리거나 실패하나 | CH `lucida.lucida_logs_local`(`event_source='log_monitor'`): was01 앱 로그 `5aad9fbb-ec04-44b0-ba02-f81ced10af73`, was02 앱 로그 `e466fa3e-c26a-41a6-9c04-90e840b6df21`(WARN, ERROR 패턴), web01 접근 로그 `d510390b-d2d3-4da4-9f1b-64af8cd16bdd`(`" 5[0-9][0-9] `), Tomcat 접근 로그 `69ebf84f-81c2-42f0-b45d-a49f93804e1f`, `63de5bcc-f7cf-478b-ba1e-d9f044a70730` | 11-21 ~ 11-24, 11-27: WAS 앱 로그 WARN 은 was02 야간 배치 느린 요청 하루 2줄뿐, ERROR 0줄. 접근 로그 5xx 0줄(11-23 웹 오류 로그 6줄은 미끼 재배포). 11-25 ~ 26 의 많은 줄은 T4 시험 | 시험 실행에서 확인. 기대(모의 비율): 두 WAS 에서 'slow request ... took_ms=' WARN 이 요청의 약 55~90%(초당 15~25줄), 'database access failed ... Unable to fetch a connection in 3 seconds' ERROR(요청의 약 1~6%), 접근 로그 503, 500 |
| 2 | 어디가 느린가: 서버인가 길인가 | VM `sms.cpu.utilization`(db01 `60579e7c-ac4f-41a0-a479-3cf25654fa28`, was01 `1d8696cc-896e-4c6f-90a0-c1aac1cfd4ef`, was02 `874c55e3-9b0a-4c5d-b1ac-9470b6012a4a`), `sms.server.rtt`(was01 `bda0dfa8-d90c-455f-b4ae-d20fcb0ae7bd`, was02 `ebdf15e2-477d-4c8f-9afc-f4c4b1baf69b`, web01 `c5f7161d-74ea-431d-90a7-09a9d534098f`, db01 `73343434-0302-468f-b738-643aec7ab84b`) | CPU 7일 중앙: db01 0.71%, was01 1.15%, was02 1.15%. RTT 7일 중앙: was01 4.62ms(p95 9.92), was02 4.59ms(p95 9.88), 24시간: web01 4.55ms, db01 5.0ms | 시험 실행에서 확인. 기대: CPU 셋 모두 평시 근처. was01, was02 RTT 만 약 1초(모의 평균 0.77~1.0초), web01, db01 RTT 평시. ICMP 상태는 1 유지가 기대(모의 핑 손실 수 %). 지연이 WAS 망으로 들어가는 길에 있다 |
| 3 | 라우터에서 무슨 일이 있나 | VM `nms.interface.if_out_discards`, `if_out_octets`, `if_oper_status`(rt-core01 ens20 `8dd90421-b8fa-435d-b3fd-5fd5be103296`, 비교 ens19 `917e4af7-5879-4bb5-97a0-1b935894d75b`, ens21 `79a3ac72-fde1-400a-89ca-bcbf414a4b92`), 롤업 `nms.interfaces.if_out_discards`(라우터 `65b4ab9c-d1b4-42f6-be5c-7efb5c34cb3c`), `nms.cpus.avg_usage`, CH `snmp_traps_local` | ens19, ens20, ens21 if_out_discards 와 라우터 롤업 7일 내내 0. ens20 if_out_octets 48시간 중앙 약 264k, p95 약 427k, KST 시간대 중앙 15시 401k, 16~18시 약 424k(72시간). if_oper_status 24시간 1. 라우터 CPU 7일 중앙 1% | 시험 실행에서 확인. 기대: ens20 if_out_discards 0 → 초당 약 10~15(모의 송신 패킷의 약 8%), if_out_octets 가 부하를 따르지 않고 약 400k 근처에 평평하게 붙음, if_oper_status 1 그대로, trap 없음, ens19, ens21 버림 0, 라우터 CPU 평시 |
| 4 | 누가 무엇을 바꿨나 | CH `lucida.syslog_local`(source_ip 192.168.123.254, hostname rt-core01), app_name sshd, sudo | 라우터 sudo 명령(11-21 ~ 11-24, 11-27): 평시 운영자 세션의 `nft list ruleset`, `tail -n 20` 하루 4~8번씩. tc 명령 0. (11-25 nft -f 와 nft delete 는 T4 시험, 11-26 설치 작업 줄은 배포) | 시험 실행에서 확인. 기대: 느려지기 시작하기 직전 'Accepted publickey for sysop from 192.168.127.5', 'COMMAND=/usr/sbin/tc qdisc add dev ens20 root handle 1: tbf rate 400kbit burst 32kb latency 400ms', cleanup 의 'COMMAND=/usr/sbin/tc qdisc del dev ens20 root' |
| 5 | DB 와 WAS 는 멀쩡한가 | CH `lucida.process_snapshot`(192.168.126.11 postgres, 192.168.125.11/12 java), PostgreSQL 로그 감시 `17a5d386-4f83-42c2-9e05-e7eecbafeb74`, CH `host_connections`(5432) | postgres 16개, java 각 1개 pid 그대로(1시간 60표본, F72-H 시트 조회), PostgreSQL 로그 감시 줄 11-27 0 | 시험 실행에서 확인. 기대: pid 그대로, PostgreSQL 로그 줄 없음. db01 쪽 WAS 출발 5432 연결이 WAS 마다 5 → 최대 40 으로 늚(풀이 참, 연결 표본) |

조회 근거(모두 분할 칸과 시각 범위를 건 읽기 전용, 104 의 readonly=2 사본과 VictoriaMetrics LB):

```
-- 단계 1: 날짜별 피해 줄 수
SELECT toDate(timestamp,'Asia/Seoul') d, host_name, target_id, severity_text, count() FROM lucida.lucida_logs_local
WHERE timestamp_date >= today()-7 AND timestamp > now() - INTERVAL 7 DAY AND event_source='log_monitor'
  AND target_id IN ('d510390b-...','66b554a1-...','5aad9fbb-...','e466fa3e-...','69ebf84f-...','63de5bcc-...') GROUP BY 1,2,3,4
-- 단계 2: VictoriaMetrics query_range sms.server.rtt{host_name=~"192.168.125.1[12]"} 168h step 300, sms.cpu.utilization{host_name=~"..."}
-- 단계 3: max_over_time(nms.interface.if_out_discards{target_id="8dd90421-..."}[1h]) 168h step 3600 (ens19, ens21 같음),
--         nms.interfaces.if_out_discards{target_id="65b4ab9c-..."}, nms.interface.if_out_octets{target_id="8dd90421-..."} 72h step 60
--         (KST 시간대별 중앙), nms.cpus.avg_usage{target_id="65b4ab9c-..."}
-- 단계 4: 라우터 sudo 명령 꼴
SELECT toDate(received_at,'Asia/Seoul'), extract(message, 'COMMAND=(\S+ ?\S* ?\S*)'), count() FROM lucida.syslog_local
WHERE toYYYYMM(received_at) >= 202611 AND received_at > now() - INTERVAL 7 DAY AND source_ip='192.168.123.254' AND app_name='sudo'
  AND message LIKE '%COMMAND=%' GROUP BY 1,2
```

표본 데이터 여부: 1, 4 단계는 전수 원천(로그 감시 줄, syslog), 2 단계는 1분 SMS 표본, 3 단계는 60초 NMS 폴링, 5 단계는 60초 프로세스 표본과 30초 연결 표본의 전체 목록이다. 트레이스, APM 은 이 도메인에 없다.

## 8. 관측 근거 요약과 감별

- must_support(정답지 6개): 라우터 운영자 로그인과 `sudo tc qdisc add ... tbf rate 400kbit`, ens20 송신 버림(평시 7일 0), ens20 송신량이 제한 근처에 평평하게 붙고 포트는 up(trap 없음), WAS 두 대만 핑 지연, 두 WAS 느린 요청 WARN 과 풀 고갈 ERROR, 접근 로그 5xx.
- must_rule_out(정답지 6개): 링크 장애(F72-H 꼴: 포트 up, 송신이 0 이 아님, 핑 응답), 접근 규칙 거부(F72-P 꼴: 명령이 tc, 차단 줄과 'Connection refused' 없음, 대부분 느리게 성공), DB 과부하나 느린 질의(db01 CPU, 핑 평시), WAS 자체 문제(CPU, java pid, 두 대 동시, 두 대 핑만), 웹 서버 문제, 라우터 자원 고갈(CPU 평시, 버림이 ens20 에만).
- 같은 라우터 세 카드 가르기:

| 근거 | F72-P(접근 규칙) | F72-H(링크 끊김) | F82-R(큐 넘침) |
|---|---|---|---|
| 운영자 줄 | `sudo nft -f /home/sysop/db-acl.nft` | 설정 변경 명령 없음 | `sudo tc qdisc add dev ens20 ... tbf rate 400kbit` |
| 포트 상태 | ens21 up | ens21 down, linkDown trap | ens20 up, trap 없음 |
| 버림 카운터(ifOutDiscards) | 0(거부는 nftables, 큐가 아님) | 0 | ens20 0 → 초당 약 10~15 |
| 송신량 | ens21 거의 0 | ens21 멈춤 | ens20 약 400k 에 평평 |
| 핑 | db01 정상 | db01 0(사라짐) | was01, was02 만 약 1초 |
| 앱 오류 꼴 | 즉시 'Connection refused', 거의 전부 5xx | 3초 뒤 연결 시간 초과, 거의 전부 5xx | 대부분 3초 넘게 성공(WARN), 일부 풀 고갈 5xx |
| 라우터 kernel 줄 | '[DB ACL] ... DPT=5432' | 'Lost carrier' | 없음(tbf 는 줄을 남기지 않는다) |

- contrast_with: F72-P, F72-H(위 표), F44-R, F44-P, F72-R(운영자 방화벽 K+G).
- 러너 배제 조건: 상주 부하가 끊김(achieved_rps < 2.0).
- 미끼와의 관계와 지름길 위험: U7 미끼(운영자 경로의 WAR 재배포, 웹 설정 재적용)는 WAS 와 웹에서만 나고 라우터의 평시 운영자 세션은 조회뿐이다. 그래서 라우터 운영자 변경 줄은 F72-P 와 F82-R 에만 있고, 둘 다 정답이 rt-core01 이다. 장애 근처 라우터 운영자 변경을 정답으로 찍는 규칙이 이 도메인에서 정밀 100% 가 될 수 있다(F72-H 는 변경이 없어 그 규칙에서 빗나간다). 라우터에 무해한 설정 변경 미끼(예: 평시에도 같은 꼴의 `tc qdisc` 나 `nft -f` 를 넣었다 빼는 일)를 둘지는 사람이 정할 것(§13).

## 9. 러너 판정과 강도, 피해 계산 (원칙 9)

### 9-1. 피해가 나는 이유: 길의 한도가 찬다

- 이 테스트베드는 한가하다(평시 약 28 rps 이하, p95 약 8ms, 서버 CPU 1~2%). 그래서 CPU 를 다투는 카드는 피해가 안 났다(사전 막힘). 이 카드의 피해는 "서버 자원"이 아니라 "길의 한도"에서 난다: ens20 이 보내야 하는 양이 제한보다 많으면 넘는 몫은 기다리거나 버려질 수밖에 없다.
- 수요(119 실측, 60초 평균): ens20 송신 ≈ 30.6k + 12.9k x 반복률(bit/s, 시간대 중앙 회귀, 72시간). 상주 부하는 시각별 목표 반복률로 돈다(`civil-petition/loadgen/entrypoint.sh`, 피크 16시 30, 저점 4시 5). 시간대 중앙: 13시 346k, 14시 373k, 15시 401k, 16~18시 약 424k, 19시 394k, 20시 367k, 밤 약 100k. 값의 단위는 bit/s 다(119 traffic_tx_usage 가 1 Gbps 대비 같은 수의 % 이고, 흔적 표 r14 가 ens19 512 kbit 제한에서 14 rps 일 때 넘치지 않은 것과 맞는다).
- 제한 400 kbit/s 와의 비(수요/제한): 13시 0.87, 14시 0.93, 15시 1.00, 16~18시 1.06, 19시 0.99, 20시 0.92. 비가 1 아래면 큐가 거의 비고 피해가 없다(모의 0.95, 0.96 에서 p95 7ms 그대로). 1 을 넘으면 넘는 몫이 쌓인다.
- 왜 "조금"이 크게 번지나: 부하기는 도착률이 정해진(constant-arrival-rate) 열린 부하라 길이 느려져도 요청이 줄지 않는다. 넘는 몫이 큐와 TCP 송신 버퍼에 쌓여 요청 하나가 끝나는 데 걸리는 시간이 길어지고, 동시에 떠 있는 요청이 부하기 가상 사용자 상한(150)에 닿는다. 그 뒤로는 "150 / 길이 처리하는 반복률" 만큼 요청이 걸리는 평형이 된다(제한 400 kbit 에서 처리 가능한 반복률은 약 (400 - 31) / 12.9 ≈ 29 반복/초, 요청 하나 평균 약 5초). 그사이 풀 연결은 늦은 DB 응답을 기다리며 오래 묶여 두 풀(80)이 차고, 연결을 못 얻은 요청이 3초 뒤 503, 500 으로 빠진다. 이것이 "느리게 성공하는 다수 + 3초 뒤 실패하는 일부 + 10초를 넘는 몇"의 모양이다.

### 9-2. 3초 설정에서 요청 하나가 어떻게 끝나나

| 경우 | 끝 | 흔적 |
|---|---|---|
| 풀 연결을 얻고 질의 응답이 늦게 닿음(대부분) | 3~10초 뒤 200 | 'slow request ... status=200 took_ms=' WARN |
| 두 풀(각 40)이 차서 3초 안에 못 빌림 | 3초 남짓 뒤 읽기 503, 신청과 처리 500 | ERROR 'database access failed: ... Unable to fetch a connection in 3 seconds ...'(읽기), 오류 페이지 ERROR(신청, 처리), WARN status=503/500 took_ms 약 3000, 접근 로그 5xx |
| 요청 도착이나 응답이 10초 넘게 걸림 | 부하기 응답 없음(0). Apache 는 30초까지 기다리고 앱은 끝까지 처리 | 앱 WARN(took_ms 10000 넘음) |
| 웹 → WAS 새 연결 SYN 이 두 번 버려짐 | Apache connectiontimeout 3초 뒤 503(그 멤버 30초 오류 상태, 다른 멤버로 넘김) | 웹 오류 로그 AH00957, AH01114(드묾) |

열린 풀 연결의 읽기 상한이 없어도 이 카드에서는 문제가 되지 않는다: 길이 끊긴 것이 아니라 늦을 뿐이라 묶인 연결도 결국 응답을 받고 풀로 돌아온다.

### 9-3. 모의: 실제 커널 TCP 와 tbf 위에서 잰 강도별 결과

방법(`/home/sjbang/tmp/claude-1008/-home-sjbang-dev-lucida-next/ee261603-595a-4ca8-a253-3a4bd5d2bb18/scratchpad/c3/emu.py`, `run.sh`): 104 에서 사용자 이름공간 넷(web, rt, was, db)을 veth 로 잇고 rt 의 WAS 쪽 veth 송신에 같은 tbf(burst 32kb, latency 400ms)를 건다. was 에 Tomcat 풀 흉내(WAS 둘, 풀 각 40, minIdle 5, maxWait 3초, connect, login 3초, 검증 30초, 읽기 상한 없음, 스레드 200, keepalive 20초와 100요청), db 에 질의마다 정한 바이트를 돌려주는 서버, web 에 Apache 흉내(byrequests, 연결 재사용, connectiontimeout 3, 오류 상태 30초, 다른 멤버 한 번, timeout 30)와 부하기 흉내(constant-arrival-rate, 가상 사용자 150, 10초, 같은 여정 비중표)를 둔다. 여정별 요청, 질의, 응답 바이트는 앱 SQL 을 읽고 정한 뒤 평시 수요가 119 실측과 맞게 0.78 배로 맞췄다(모의 무제한 19 반복/초 ≈ 285k, 30 ≈ 430k, 119 회귀 276k, 418k). 관제 흐름은 30 kbit/s 하나로 넣었다. 측정 구간은 워밍업 뒤 90 ~ 180초. 10분 2틱 연속 0 확률은 틱 위상을 0.1초씩 150번 밀어 센 연속 쌍 비율 p 로 1 - (1 - p)^39 를 낸 것이고, 짧은 측정이라 1.10~1.13 에서 2~7% 로 흔들린다.

| 반복/초, 제한(kbit) | 수요/제한 | 비정상(5xx+0) | 3초 넘는 요청 | 30초 창 p95 | 신청 0 비율 | 15초 틱에 마지막 신청 0 | 10분 2틱 연속 0 확률(추정식 P: 틱 위상 150개에서 센 연속 쌍 비율 p 로 1-(1-p)^39, T: 틱 0 비율의 제곱 x 39. 괄호는 측정 구간의 신청 0 건수/신청 수와 측정 초. 표본이 적어 크기 순서만 본다) | 큐 버림(송신 패킷 대비) | WAS 핑 평균 |
|---|---|---|---|---|---|---|---|---|---|
| 30, 500 / 30, 450 | 0.86 / 0.96 | 0 | 0 | 0.008초 | 0 | 0 | 0 | 0 | 0.06ms |
| 19, 300 | 0.95 | 0 | 0 | 0.008초 | 0 | 0 | 0 | 0 | 0.06ms |
| 30, 420 | 1.02 | 1.3%(모두 응답 없음, 5xx 0) | 40% | 2.9 → 7.7초(밀린 것이 쌓이며 오름) | 0.7% | 0.5% | P 0(3/405, 180초) | 5.1% | 0.74초 |
| 30, 400 | 1.08 | 3.5% | 56% | 5.7~10초 | 2.0% | 0.4% | P 0(4/201, 90초) | 6.2% | 0.77초 |
| 28, 365 | 1.10 | 5.5% | 79% | 8.4~10초 | 4.5% | 3.6% | P 약 6.8%(17/376, 180초) | 7.6% | 1.0초 |
| 30, 380 | 1.13 | 7.5% | 76% | 8.8~10초 | 1.6% | 1.6% | P 약 2.3%(6/380, 180초) | 8.5% | 0.99초 |
| 30, 360 | 1.19 | 9.8% | 91% | 8.3~10초 | 3.7% | 5.2% | T 약 10%(13/348, 180초. P 는 0 이었으나 표본이 적어 T 로 적음) | 7.8% | 1.0초 |
| 30, 340 | 1.26 | 9.6% | 92% | 9.2~10초 | 6.3% | 5.5% | P 약 11%(22/347, 180초) | 9.3% | 1.08초 |
| 30, 300 | 1.43 | 14% | 97% | 10초 | 13% | 14% | P 약 49%(22/166, 90초) | 9.5% | 1.17초 |
| 19, 256 | 1.11 | 21% | 97% | 10초 | 18% | 17% | P 약 10%(24/135, 90초) | 8.6% | 1.18초 |

읽는 법: 수요가 제한 아래면 아무 일도 없고(급경사), 넘으면 넘는 정도와 상관없이 요청 대부분이 수 초가 된다. 응답 없음(0)은 넘는 정도가 클수록, 그리고 같은 비라도 낮은 반복률(처리 가능한 반복률이 작아 평형 지연이 큼)일수록 는다(19, 256 이 30, 380 보다 나쁨). 그래서 강도는 "바쁜 시간에, 수요가 제한을 조금만 넘을 때"로 고른다.

- 단계별 비정상(30, 380): 접수번호 조회 4.3%, 내 민원 10%, 검색 9.8%, 통계 13%, 신청 6.1%, 처리 9.7%, 로그인 4.6%. 느린 요청 WARN 3,694건(그중 status 503 204, 500 51), 풀 기다림 실패 237건.
- 한계: Apache MPM 작업자, JVM, 실제 PostgreSQL 프로토콜과 바이트 분포, virtio 와 66 브리지 지연은 흉내 내지 않았다. 피해가 "나는가"와 응답 없음 비율의 크기 순서를 보는 도구이고 실측(첫 실행)을 대신하지 않는다.

### 9-3-1. 모의에 없는 몰림: WAS 의 fwupd 메타데이터 내려받기 (1회차 평가 권고 4)

- 119 의 16~18시 ens20 송신(60초 값)에 1분짜리 튐이 있다: 11-27 16:40 약 700k, 17:01 약 705k, 11-26 16:40 579k, 17:00 633k, 11-24 15:22 691k. 같은 분에 라우터 상향 포트 ens18 수신이 평소 약 150k 에서 약 425k 로 오르고 db01, web01 쪽 포트(ens21 송신, ens19 송신)는 평평하다. 곧 바깥(상향)에서 WAS 망으로 들어온 몫이다.
- 출처(119 조회): 같은 분 SMS `sms.network_interface.rx_traffic`(device ens18)가 was02(11-27 16:38 236k, 11-26 16:38 241k, 평소 약 27k) 하나에서만 튀고, 같은 시각 was02 syslog 에 `fwupd-refresh.service` 시작, `fwupdmgr: Updating lvfs`, `Successfully downloaded new metadata` 가 있다. 17:00 튐은 was01 의 같은 줄(11-27 17:00:26 ~ 28)이다. Ubuntu 기본 `fwupd-refresh.timer` 가 펌웨어 메타데이터(1분 평균으로 약 2 MB)를 인터넷에서 받는 것이다.
- 빈도와 시각(7일 syslog): WAS 마다 하루 1~5번, 최근 며칠은 하루 한 번이고, 11-24 ~ 26 사이에는 늦어지다가 최근 이틀은 16:37~17:01 에 머문다(11-26 과 11-27 차이 1분)(was02: 11-24 15:21, 11-25 15:52, 11-26 16:37, 11-27 16:38. was01: 11-24 15:27, 11-25 16:25, 11-26 16:59, 11-27 17:00). 녹화 시각 16:00~18:30 과 겹칠 수 있다. 내려받기 두 번(16:38 무렵, 17:00 무렵) 각각 약 1분이라, 녹화 창 안에서 고르게 고른 시각에 10분 유지를 시작하면 겹칠 확률은 약 15%(겹치는 시작 구간 약 11분 x 2 / 150분, 15분 유지면 약 20%)다. 그래서 큐 note 에 "그날 119 syslog 에서 was01, was02 의 fwupd 내려받기 줄을 확인한 뒤 시작(최근 모양이면 17:05~18:30)"을 시작 조건으로 더했다.
- 제한 중 영향: 약 2 MB 가 400 kbit/s 큐에 더해지면 약 40초 동안 수요가 제한을 크게 넘어(1분 평균 비 약 1.75) 그동안 응답 없음이 늘고 2틱 연속 0 위험이 오른다. 모의는 이 몰림을 넣지 않았으므로 중단 위험 "0~수 %, 2~7%" 는 몰림을 뺀 값이다. 또한 비 1.02 모의는 측정 창 안에서 p95 가 계속 오르는 중이라 정상 상태가 아니었다(긴 유지에서 더 나빠질 수 있다).
- 현실성: 운영 중 서버의 정기 내려받기가 막힌 길에 겹치는 것은 실제 장애에서도 흔하다(원본도 송신 트래픽 양이 영향을 갈랐다). 몰림을 없애지 않고 첫 실행 확인 항목에 둔다(§9-5). 실행 중 몰림이 있었는지는 was01, was02 syslog 의 fwupd 줄과 ens18 수신으로 사후에 가를 수 있다.

### 9-4. 강도와 판정

- 강도: 하나로 고정(`approved-fixed-f82-r`): rate 400 kbit, burst 32 kb, latency 400 ms, ens20. 부하 창은 실행기 preflight 가 지킨다: 주입 직전 ens20 송신을 6초 재어 제한의 102~112% 일 때만 넣는다(`min_load_pct` 102, `max_load_pct` 112). 아래(한가한 시간)면 피해가 없고, 위면 응답 없음이 빠르게 늘어 필수 중단 위험이 커진다(§9-3 1.19 이상). 119 시간대 중앙으로는 KST 16~18시(비 약 1.06)가 들고, 6초 측정의 흔들림과 날마다의 차이로 15시나 19시(비 약 1.0)에 드는 날도 있다(그러면 피해가 늦게 서거나 약해 success 를 못 채우고 끝난다, 안전 쪽). 제한값을 400 으로 둔 까닭: 가장 바쁜 시간의 비를 모의에서 피해는 확실하고 응답 없음은 거의 없는 1.02~1.08 한가운데에 두려고. 384 로 두면 15~19시가 들지만 16~18시 비가 1.10 으로 창 위쪽 끝(중단 위험 약 2~7%)에 붙는다.
- 시간대별 통과율(평가 권고, 2회차 갱신): 119 ens20 60초 값이 창(408~448 kbit/s) 안에 드는 비율, 깨끗한 6일(11-20 ~ 24, 11-27. 11-25 ~ 26 은 T4 시험이라 뺌)의 날별 최소 ~ 최대. 15:00~15:30 0~13%, 15:30~16:00 34~48%, 16:00~16:30 72~97%, 16:30~17:00 83~93%, 17:00~17:30 69~100%, 17:30~18:00 90~100%, 18:00~18:30 77~93%, 18:30~19:00 43~57%, 19:00~19:30 0~7%. 곧 창 16:00~18:30 안은 69~100% 다. 상주 부하 목표(entrypoint.sh, PEAK 30, TROUGH 5, 정수 산술 5 + (25 x pct + 50) / 100)는 14시 28, 15시 30, 16시 30, 17시 30, 18시 28, 19시 26 이고, k6 1시간 반복이 시작 시각을 매시 :48 무렵으로 밀려 바뀌므로 30 rps 구간은 약 15:48~18:48 이다(그 시각에 시작한 반복이 그 시각의 목표를 1시간 쓴다). 녹화 시각은 KST 16:00~18:30(큐 note). preflight 는 6초 값이라 60초 값보다 더 흔들린다.
- 동반 부하 없음: 동반 부하를 더하면 수요가 시각과 무관하게 정해지지만, 이 테스트베드의 동반 부하 실행기는 별도 k6 라 상주 부하 관측(판정, 중단)과 분리되고 열린 부하를 더 얹어 평형 지연을 키운다. 바쁜 시간 상주 부하만으로 강도가 맞으므로 쓰지 않는다.
- success: `request_p95_ms`(새 관측 `loadgen.http_p95_ms`, 상주 부하 civil-petition, 모든 요청의 30초 창 p95) ≥ 3000, 3틱. 로그인 상태와 무관하게 모든 여정이 들어가 장애 내내 값이 실린다(F72-P 평가 지적의 일반 규칙). 비정상 비율(창 안 모의 1.3~7.5%)과 접수번호 조회 비율(1~10%)은 문턱으로 약해 쓰지 않는다.
- must_rule_out: achieved_rps < 2.0(부하가 끊겼을 때만). 장애 중 가상 사용자 상한으로 반복 일부(창 안 모의 0~7%)가 버려져도 목표의 90% 넘게 남는다.
- abort: entry_status = 0, 2틱(필수). 위험은 §9-3 의 마지막 열: 제한 400 에서 16~18시 비 약 1.06 이면 모의 30, 420(1.02) 과 30, 400(1.08) 사이로 10분에 0~수 %(두 실행 모두 연속 쌍 0 이지만 신청 0 표본이 3, 4건뿐이라 0 이라고 단정하지 않는다). 창 위쪽 끝 1.10~1.13 은 모의 28, 365 와 30, 380 으로 약 2~7%(짧은 측정이라 흔들림 큼). preflight 상한 112% 가 1.19 이상(약 10%)을 막는다. 단 이 수치는 관제 흐름을 30 kbit/s 고정 하나로만 넣은 모의 값이라, 아래 §9-3-1 의 1분짜리 몰림이 주입과 겹치면 중단 위험이 모의보다 클 수 있다.
- recovery: target_health 200, entry_status 200, request_p95_ms < 1000, 조회 비정상 비율 < 0.1, 2틱, 10분.
- min_hold 10m, settle 60s, level timeout 15m, max_injection_duration 20m.
- 시각(예상, §9 계산): t0 운영자 로그인과 tc(몇 초) → 몇십 초 안에 큐 넘침과 풀 참 → 3틱 뒤 success → t0 + 10분 cleanup 운영자 로그인과 `tc qdisc del` → 몇 초 안에 밀린 것이 빠지고 p95 회복.
- 안전: 바꾸는 것은 ens20 root qdisc 하나(실행기가 하위 NIC 만 허용, 상향 ens18 과 관리 ens22 거절). 관리 NIC 로 하는 cleanup 은 막히지 않는다. 라우터 자기 되돌림 가드(U8)가 운영자 명령보다 먼저 걸린다(되돌림은 ens20 의 handle 1: tbf root 만 지움, 남의 root 는 안 건드림).

### 9-5. 첫 실행 확인 항목 (아직 실측 없음)

- 3초 설정이 두 WAS 에 적용됐는지(옛 10초면 풀 기다림이 부하기 시간 제한과 같아져 응답 없음이 늘고 중단 위험).
- 러너에 새 관측 `loadgen.http_p95_ms` 가 배포됐는지(없으면 success 가 unusable).
- preflight 측정값(ens20 kbit/s, %)과 실제 피해의 관계: 16~18시 비 약 1.06 에서 모의대로 p95 가 3초를 넘는지. 6초 측정이 60초 평균과 얼마나 다른지.
- ens20 ifOutDiscards 가 119 에 오르는지(도우미 pass_persist, 60초 폴링), if_out_octets 가 400k 근처에 평평한지.
- WAS fwupd 내려받기(§9-3-1)가 주입 10분과 겹쳤는지, 겹쳤다면 그 동안 응답 없음과 entry_status 가 어땠는지.
- was01, was02 의 SMS 보고: 관제 응답이 ens20 을 지나 늦어져 SMS 보고나 표본이 비는지(가용성 UNKNOWN 이 생기면 흔적이 바뀐다). ICMP 상태가 1 로 남는지.
- 두 WAS 앱 로그 WARN, ERROR 줄 수와 로그 감시 수집 지연(초당 15~25줄 WARN, 10분에 약 1만~1.5만 줄).
- 라우터 운영자 sudo tc 줄이 UDP syslog 로 119 에 닿는지.
- 응답 없음 비율과 entry_status 0 의 실제 빈도(모의 신청 0 1~2%).

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Google Compute Engine 2015-03-07, 링크)와 요소별 대응표가 있고 기전이 같다(§2) (원칙 1)
- [x] 분류 장부를 갱신했다(§4-1 행, §6 기록). 묶음 K 5 → 6(5%), 정답 위치 노드, 디스크 11 → 12(9.8%), 결제 경로 14/123(11.4%, 무관), 민원 4 → 5. 단순 규칙 적중률 판정은 보류(v4 세트 20건 미만, 통과로 본다), 아래 숫자 (원칙 2)
- [x] 근본 원인 위치가 인프라 지점(rt-core01 ens20 root qdisc, 결정 4 의 ifOutDiscards 도우미)과 코드 위치로 확인됐다(§4) (G1)
- [x] 근본 원인의 흔적 자리가 119 실데이터에 있다: 라우터 syslog 운영자 sudo 줄(평시 세션 줄 수집), NMS ens20 if_out_discards 와 if_out_octets(평시 계열 수집, 7일 0), WAS 핑(SMS rtt). 장애 때 모양은 시험 실행에서 확인(§7) (원칙 3)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: syslog, 로그 감시, NMS 폴링, SMS 지표(§7) (원칙 3)
- [x] 계기의 흔적이 있다: 운영자 로그인과 `sudo tc qdisc add ... tbf rate 400kbit`(평시 라우터에 없는 명령), 그 결과의 송신 버림 카운터. 인공 지연이 아니라 현실 운영자 명령이 만든 큐 넘침이다 (원칙 4)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다(§5) (원칙 5)
- [x] 정답지 세 칸이 원칙 6대로 채워졌다(§5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다(§6) (원칙 7)
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: 운영자 명령에 이름이 없고(tc 와 장치, 값뿐), 가드 작업 이름 queue-ens20, 실행기 계정 줄은 라우터 rsyslog 첫 규칙이 거른다 (원칙 8)
- [x] 부하 상한과 타임아웃 사슬(풀 3초, 부하기 10초, 웹 30초, 가상 사용자 150)을 넣은 피해 계산과 실제 커널 모의가 있다(§9) (원칙 9)

단순 규칙 적중률(`scenario-stats.py --candidate F82-R`): 아래 §10-1.

### 10-1. 단순 규칙 적중률

판정 **보류**(v4 세트 10건 < 20건, 통과로 본다). 세트 구성 변경 원인 3/9(33.3%) → 3/10(30.0%): 장부의 주 묶음 K 는 변경 계기 칸이 "아니오" 라 K+G 인 이 후보가 변경 원인으로 세지지 않는다(F72-P 와 같음). 하한 30% 에 딱 닿았다(아래로 내려가지 않음). 다음 카드는 변경 원인 몫을 올리는 쪽이어야 한다(권고). 규칙 1, 2, 6 적중률 33.3% → 30.0%, 정밀 100% 그대로(미끼와 함께 75%), 규칙 5 적중률과 정밀 44.4% → 50.0%(2회차 다시 잼).

## 11. 119 보강 후보 (고치지 않는다, 지휘 세션 몫)

| 근거 | 지금 | 보강하면 |
|---|---|---|
| 라우터 syslog 의 운영자 설정 변경(`sudo ... tc qdisc add`, `nft -f`) | 이벤트 안 됨 | 장비 설정 변경 이벤트가 서면 "변경 = 원인" 후보가 시각과 함께 선다 |
| ens20 if_out_discards, 라우터 롤업 if_out_discards | 밴드가 있다(평시 0, 밴드 폭 약 1e-12). 인터페이스 단위가 이상 판정되는지, 롤업만 되는지는 모름 | 이 시나리오에서 이벤트가 생길 가장 유력한 길. 시험 실행에서 이벤트 여부를 본다 |
| was01, was02 sms.server.rtt 급등 | 밴드 여부 모름 | RTT 지표 이상 이벤트가 서면 "WAS 쪽 길" 위치가 이벤트에 남는다 |
| 두 WAS 앱 로그 WARN(느린 요청) 급증과 ERROR | 로그 감시 줄은 이벤트 안 됨 | 로그 감시 원천의 급증 이벤트가 서면 사용자 피해가 인시던트 재료가 된다 |
| 웹, Tomcat 접근 로그 5xx | 같음 | 같음 |

## 12. 이번에 바꾼 부분 (지휘 세션 검토)

1. `router_qdisc_executor.py`: 새 모드 `operator-rate`(파라미터 host, node, mode, device, rate_kbit, burst_kb, latency_ms, min_load_pct, max_load_pct). 운영자 경로(`vm.operator_dispatch`, OPERATOR_FUNCTIONS), preflight 의 송신량 창 검사(`/proc/net/dev` tx 바이트 6초), 가드 되돌림은 handle 1: tbf root 만, cleanup 은 운영자 `tc qdisc del` 뒤 root 가 남은 것. 기존 모드(rate, netem)는 그대로(기존 시험 통과). 첫 계약 F82-R. `registry/profiles.json` router.qdisc 를 live_supported true, 같은 계약으로.
2. 시험(`tests/test_ready_profile_executors.py`): `test_router_rate_limit_goes_the_operator_way_inside_its_load_window`(실제 커널 이름공간의 veth 와 UDP 흐름으로 창 아래, 위 거절과 통과, 운영자 로그인과 sudo 줄, 멱등, 운영자 cleanup, 운영자 없는 cleanup, 가드 되돌림, 남의 root 보존), `test_router_rate_limit_parameters`. NodeGuardTests 의 router.qdisc 견본을 USED_NODE_SAMPLES 로 옮김, 시험 도구 시험의 "계약 없음" 단정을 "계약은 F82-R 뿐"으로. 라이브 프로파일 목록, 개수 핀, 끝 순서 고정 목록.
3. `generate-manifests.py`: router.qdisc 의 preflight 를 network-oob-recovery 로.
4. 새 관측 `loadgen.http_p95_ms`: testbed-services `registry/queries.json` 와 러너 `backend/app/observation_queries.json`(selector `http.p95.ms`), 러너 `backend/app/live_probes.py` LOADGEN_FIELDS 에 `http_p95_ms`. 부하 모니터는 이미 civil-petition 에서 `--http-summary` 로 `http_p95_ms` 를 싣는다(`civil-petition/deploy/templates/loadgen/loadgen.service`). 러너 저장소는 커밋하지 않았고 러너 재빌드가 필요하다.

## 13. 사람이 정할 것

- 러너 새 관측과 재빌드(§12-4)를 받아들일지. 받아들이지 않으면 success 를 기존 관측으로 둘 수 없다(조회 비정상 비율은 모의 1~10% 라 문턱으로 약하다).
- 녹화 시각: 큐 note 의 KST 16:00~18:30(§9-4 시간대별 통과율). 운영 하네스가 이 시각을 강제할지(시각 조건을 읽게 할지, 큐 순서로 맞출지), F81-R 과 같은 결정 거리.
- 라우터 운영자 변경 미끼: F72-P 와 F82-R 둘 다 라우터 운영자 변경이 원인이라 "라우터 운영자 변경 = 정답" 지름길이 생길 수 있다(§8). 라우터에 U7 같은 무해한 변경 미끼를 둘지.
- v4 세트 변경 원인 몫: 이 후보로 33.3% → 30.0%, 하한 30% 에 닿았다(§10-1). 다음 카드를 변경 원인 쪽으로 할지.
- accept 의 rt-core01 이 F72-P, F72-H 와 같다(같은 장비 셋, 포트는 F72-H ens21, F82-R ens20 으로 다름). 위치 채점만으로는 세 카드가 같은 점수다. 기전 서술 채점(must_support)이 가른다.
