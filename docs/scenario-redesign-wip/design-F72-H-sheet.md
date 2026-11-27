---
title: F72-H 설계 시트, 민원 포털 라우터의 DB 쪽 포트 링크가 끊겨 WAS 가 DB 에 닿지 못함
status: Draft
owner: project
last_reviewed: 2026-11-27
tags:
  - scenario
  - design
  - network
  - civil-petition
summary: 민원 포털(SMS, NMS 전용 전통 3계층 테스트베드) 라우터 rt-core01 의 DB 쪽 포트 ens21 링크가 끊겨(변경 아닌 원인) DB 망이 라우터에서 빠지고, was01, was02 의 DB 연결이 대답 없이 버려져 앱 DB 기다림 상한 3초 뒤 실패하며 포털 거의 모든 요청이 503, 500 이 되고 db01 이 관제에서 함께 사라지는 시나리오(Magic Pages 2026-10-20 재구성)의 설계 근거. 사용자와 정한 카드 C1, F72-P 와 증상이 같고 원인이 다른 H.
---

# F72-H 설계 시트

- id `F72-H`, slug `f72-h-petition-router-db-link-down`, 도메인 `civil-petition`(SMS + NMS 테스트베드)
- 사용자와 정한 카드: C1 "라우터 DB 망 포트 링크 끊김"(`~/dev/testbed-ops/v4/scenario-design/cards.md`). 11-25 첫 설계는 앱 DB 기다림(10초)이 부하기 시간 제한(10초)과 같아 러너 필수 중단에 걸려 막혔고, 11-26 사용자 결정으로 앱 DB 기다림 상한 3초(main 28ab39a)가 들어와 다시 설계한다.
- 상태: 후보(ready + `stage: candidate`), 설계 강도 하나로 고정한 evaluation 모드, 녹화 대기 큐에 추가(note: 새 테스트베드 첫 시나리오, 흔적 표 실측과 119 보강 뒤 녹화, 3초 설정 적용 확인)
- 주입 수단: 기존 실행기 `vmlink.state`(66 하이퍼바이저, QMP set_link)에 첫 계약만 더함. 실행기 동작은 바꾸지 않는다. 동반 부하 없음(상주 부하가 피해를 낸다)
- ID: F72 사례군(F72-R, F72-P: "WAS 에서 DB 로 가는 길이 막힘")과 사용자 증상이 같고 원인이 다르므로 관계 H(카탈로그 §1). 새 사례군 번호(F81)는 다른 설계 세션과 겹칠 수 있어 쓰지 않았다.

## 1. 요약과 비유

민원 포털은 웹(web01), WAS 두 대(was01, was02), DB(db01) 가 라우터(rt-core01) 하나로 이어진 전통 3계층이다. DB 망(192.168.126.0/24)으로 가는 길은 라우터 NIC ens21 하나뿐이고, db01 이 관제 서버로 보내는 보고도 이 길로 나간다.
그 포트의 링크가 끊긴다. 케이블이 빠지거나 맞은편 스위치 포트가 죽은 것과 같다. 아무도 로그인하지 않았고 설정도 바뀌지 않았다.
라우터는 캐리어를 잃고 DB 망 주소와 경로를 내린다. WAS 가 DB 로 보내는 패킷은 엉뚱한 길로 나갔다 되돌아와 대답 없이 버려진다. 앱은 3초를 기다린 뒤 포기하고, 포털의 로그인, 신청, 조회, 검색, 담당자 처리가 모두 약 3초 뒤 503 이나 500 으로 끝난다. db01 은 멀쩡히 돌지만 관제 화면에서 보고와 핑이 함께 끊겨 죽은 것처럼 보인다.

비유: 금고실로 가는 복도 하나가 무너졌다. 금고와 경리팀은 그대로인데 경리 업무가 전부 서고, 금고실 안 사람과 연락도 끊겨 금고실이 불탄 것처럼 보인다. 건물 안내판(라우터)은 "3번 복도 통행 불가"를 알린다.

## 2. 원본 사례

### 2-1. 고른 사례: Magic Pages, 2026-10-20

- 사후 보고 "Post-mortem on 20 October 2026 outage"(Ghost 호스팅, 창업자 Jannis Fedoruk-Betschki, 2026-10-21 게시), [공식](https://www.magicpages.co/blog/post-mortem-on-20-october-2026-outage/). 자료 문서 `ref-real-world-incidents.md` M15 에 이번에 더함(출처가 말한 사실만).
  - "At 19:04 UTC, that private network started dropping connections between some of our servers." "A database server got lost first." 서버 사이 사설망은 Hetzner 의 vSwitch 이고, Hetzner 쪽 사고 제목은 "Technical issues with vSwitch MAC learning". 이 회사 쪽 변경은 적혀 있지 않다.
  - 사설망이 연결을 버린 결과 "On one worker about 260 sites couldn't reach the database from 19:37 to 19:56 UTC"(한 작업 서버의 약 260개 사이트가 데이터베이스에 닿지 못함), 캐시된 페이지는 떴지만 "Everything else failed with an error after about 10 seconds."(관리 화면 로그인, 발행, 회원 가입, 캐시 안 된 페이지).
  - "The database cluster and the storage cluster recovered on their own." 저장된 데이터 손실 없음. 탐지 경로는 적혀 있지 않다. 후속은 보조 망(암호화) 추가.

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 서버 사이 사설망(vSwitch)이 연결을 버리기 시작함, 이 회사 쪽 변경 없음 | 라우터 rt-core01 의 DB 쪽 포트 ens21 링크가 끊김(캐리어 잃음), 누구의 변경도 없음 |
| 원인이 된 결함 | 망 장비(vSwitch MAC 학습) 쪽 장애로 서버 사이 길이 끊김 | 앱 서버와 DB 서버 사이 유일한 길(라우터 포트 ens21)의 물리 링크 끊김 |
| 전파 경로 | 데이터베이스 서버가 먼저 끊기고 작업 서버들이 데이터베이스에 닿지 못함 | 라우터가 DB 망 주소와 경로를 내려 WAS 출발 패킷이 대답 없이 버려짐 → was01, was02 의 DB 연결 시간 초과 |
| 사용자 증상 | 캐시 안 된 요청이 약 10초 뒤 오류, 로그인과 발행 실패 | 포털 거의 모든 요청이 약 3초 뒤 503, 신청과 처리 500, 첫 몇 요청은 응답 없음 |
| 원인 쪽 상태 | 데이터베이스 클러스터 자체는 정상, 스스로 회복 | db01 과 PostgreSQL 은 계속 돎. 단, 관제 쪽에서는 보고와 핑이 끊겨 닿지 않음 |
| 탐지된 경로 | 적혀 있지 않음 | 라우터 NMS ens21 down 과 linkDown trap, 가용성 ens21 DOWN, 라우터 syslog Lost carrier, db01 보고 공백과 ICMP 0, WAS 앱 로그의 연결 시간 초과와 느린 요청, 웹과 Tomcat 접근 로그 5xx |
| 원본과 다른 점(찾기 쉬움) | 링크 신호 없이 사설망이 연결을 조용히, 간헐적으로(여러 차례, 서버마다 다르게) 버림. 사업자 쪽 장애라 이 회사 장비에는 계기 흔적이 없었음 | 링크가 한 번 뚜렷이 끊겨 라우터에 캐리어 잃음, linkDown trap, 'Lost carrier' 라는 계기 흔적이 남는다. 그래서 원본보다 원인 자리를 찾기 쉽다(관계 H 로 F72-P 와 가르는 데 이 흔적을 쓴다) |
| 완화와 복구 | 망이 돌아오자 클러스터가 스스로 회복, 보조 망 추가 | 링크가 돌아오면(cleanup) 라우터가 DB 망을 되살리고 풀이 다시 연결해 1분 안에 회복 |

기전 확인: 원인(앱 서버와 DB 서버 사이 망 경로가 변경 없이 끊김)에서 증상(앱이 DB 연결을 기다리다 시간 초과로 오류, DB 자체는 멀쩡)까지의 고리가 원본과 같다. 원본의 시간 초과 약 10초는 그 앱의 기다림 상한이고, 이 테스트베드는 앱 설정 3초를 쓴다(규모를 바꾼 것이지 기전은 같다). 원본의 장애 장비는 클라우드 사업자의 가상 스위치이고 여기서는 계층 사이 라우터의 포트다. 둘 다 서버 바깥 망 장비 쪽 끊김이라 관제 AI 가 서버가 아니라 길을 짚어야 한다.

### 2-2. 카드의 처음 원본(GitHub 2018-10-21)을 쓰지 않은 까닭

- GitHub 사후 분석(2018-10-30, M15 에 있음): 계기는 고장 난 100G 광 장비를 바꾸는 정기 정비였고 동서 해안 사이 43초 연결 끊김이 났으며, 그 결과는 리전 간 MySQL 자동 페일오버와 데이터 불일치, 24시간 넘는 성능 저하였다. 계기가 정비 작업이고(변경 계기에 가깝다) 피해 고리가 "앱이 DB 에 못 붙음"이 아니라 "DB 페일오버가 엉뚱한 쪽을 주로 뽑음"이라 이 카드와 맞음새가 약하다(11-25 지적).
- 웹에서 더 찾은 것(쓰지 않음): GitHub 2012-11-30 "Network problems last Friday"(스위치 이전 정비 중 잘못 설정된 부분 링크 감시 기능과 업체 결함, 계기가 정비), Cloudflare 2020-11-02 "A Byzantine failure in the real world"(스위치 일부 고장이 etcd 와 DB 클러스터 페일오버 연쇄로 번짐, 피해 고리가 페일오버). 둘 다 계기나 고리가 다르다.
- Magic Pages 2026-10-20 은 계기(변경 없는 망 끊김), 고리(앱 서버가 DB 에 못 붙음), 증상(시간 초과 뒤 오류), 원인 쪽 상태(DB 는 멀쩡)가 카드와 모두 맞는다. 공식 사후 보고지만 회사 규모가 작은 점은 한계로 적는다.

## 3. 숫자 근거 (2026-11-27, `scenario-stats.py`, 정식 + 후보)

- 이번 실행에서 다른 점(공통 지시): 후보는 사용자와 정한 카드 10장이라 후보 넓게 뽑기를 하지 않는다. 숫자는 상한 확인용이다.
- 서비스: 설계 GT0 결정 3 (다)와 S 결정 4(T 와 S 한 몫, v4 시작 뒤 생성분만). 민원 2(F72-P, F80-P) → 3. `scenario-stats.py` 는 civil-petition 과 civil-petition-sms 를 따로 세고 "v4 시작 뒤 생성분만" 구간을 모르므로 손으로 셌다. 사용자가 두 테스트베드 카드 10장을 정해 서비스 균형으로 막지 않는다.
- 묶음(합계 120 → 121, 20% = 24.2): A 11, B 7, C 6, D 17, E 4, F 4, G 13, H 2, I 2, J 21, **K 4 → 5(4%)**, L 9, M 1, O 3, P 14, Q 2. 주 묶음 K 단독: 계기가 변경이 아니다(운영자 로그인, 설정, 배포 없음). 같은 K 의 넷(F44-R, F44-P, F72-R, F72-P)은 모두 운영자 방화벽 변경(K+G)이라 F72-H 가 첫 비변경 K 다. 현실 비중(네트워크 15%)에 비해 K 가 적다.
- 정답 위치: **노드, 디스크 10 → 11(9%)**(§2-1 '네트워크 등 인프라'). 결제 경로 14/121(11.6%, 이 후보는 무관).
- 단순 규칙 적중률(`--candidate F72-H`): 판정 **보류**(v4 세트 8건 < 20건, 통과로 본다). 변경 원인 몫 3/7 → 3/8(37.5%, 하한 30% 위). 규칙 1, 2, 6 적중률 42.9% → 37.5%, 정밀 100% 그대로(미끼와 함께 75%), 규칙 5 적중률 14.3% → 25.0%, 정밀 16.7% → 28.6%. 장애 근처 변경을 찍는 규칙이 이 후보에서 빗나가므로 변경 원인 쏠림을 누그러뜨린다.
- 왜 이 후보인가: 사용자와 정한 카드. F72-P 와 사용자 증상(WAS 두 대가 라우터를 지나 DB 에 못 붙어 포털 전면 5xx)이 같고 원인이 달라, 관제 AI 가 라우터 포트 상태, trap, db01 의 관제 공백, 오류 꼴(거부 대 시간 초과), 운영자 줄 유무를 읽어야만 둘을 가른다(관계 H 의 가치). 이 테스트베드의 결정 5(관제가 라우터를 거침)가 노린 "서버가 죽었다와 서버로 가는 길이 끊겼다를 데이터로 가르기"를 처음 쓰는 시나리오다.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 계기(66 하이퍼바이저): 실행기 `vmlink.state`(`scripts/scenarios/profiles/vmlink_state_executor.py:34-36` CONTRACTS["F72-H"] = host 192.168.122.1, node pve66, vmid 201, net net3)가 강제 명령 계정으로 66 도우미에게 `link-down 201 net3` 을 보낸다. root 도우미(`civil-petition/deploy/hypervisor/t4-hv-exec-root.in:146-150`)가 자기 되돌림 가드를 먼저 걸고 VM 의 QMP 소켓에 `set_link` off 를 보낸다(qm 을 거치지 않아 Proxmox 작업 줄이 없다). VM 201 의 net3 은 라우터의 ens21(DB 쪽, 192.168.126.1)이다(`vm_executor_common.py:88-93` ROUTER_DOWNLINKS, HV_LINKS, 인벤토리 `t.json` rt-core01 nics 순서: ens18 net0 상향, ens19 net1 웹, ens20 net2 WAS, ens21 net3 DB, ens22 net4 관리. 11-26 T4 시험 r21 이 net2 를 내려 ens20 이 내려간 것으로 대응이 확인됐다).
2. 라우터: 게스트는 캐리어를 잃는다. systemd-networkd 가 'ens21: Lost carrier' 를 남기고, 망 설정에 캐리어를 잃어도 설정을 지키는 지시가 없어(`civil-petition/deploy/render.py:327-358` `_netplan`, 주소와 경로만) DB 망 주소와 연결 경로를 내린다. snmpd 는 `linkUpDownNotifications yes`(`templates/router/snmpd.conf:13`)라 linkDown trap 을 10.250.0.1 로 보내고 IF-MIB ifOperStatus 를 down 으로 보고한다. 11-26 T4 시험 r21(ens20 같은 꼴)에서 Lost carrier, linkDown trap, if_oper_status 2, 가용성 인터페이스 DOWN 이 모두 119 에 닿았다.
3. 버려지는 길(소리 없이 버림): 라우터에 192.168.126.0/24 경로가 사라지면 WAS 출발 패킷(192.168.125.x → 192.168.126.11)은 라우터 기본 경로(상향 ens18 → 66 호스트 192.168.123.1)로 나간다. 평시 ACL 은 주소로만 허용하므로(`render.py:370-389` `_acl_rules`: WAS 망 → DB 망 tcp 5432, 업무망 → 상향 허용) 그대로 나간다. 66 은 그 대역을 192.168.123.254 로 보내므로(설계 §3-4 경로) 같은 브리지로 라우터에 되돌려 보낸다. 라우터는 상향 NIC 로 들어온 출발지 192.168.125.x 가 역경로(ens20)와 맞지 않아 rp_filter=1(`templates/router/sysctl-router.conf:3-4`)로 대답 없이 버린다. 66 은 출발지가 자기 망에 있지 않아 ICMP redirect 도 보내지 않는다. 그래서 WAS 는 SYN 에 아무 답도 받지 못한다.
   - 실측 근거: 11-26 T4 시험 r21(라우터 WAS 쪽 ens20 180초 내림, 같은 라우터, 같은 꼴의 길)에서 웹 → WAS 연결이 Apache `connectiontimeout=3`(`render.py:464`) 두 멤버 몫인 약 6.0초씩 걸렸다(부하기 p95 6,009ms, 200초). ICMP 로 빠르게 거부됐다면 몇 ms 였을 것이다. 라우터가 캐리어를 잃은 직후 networkd 가 주소를 내리기 전 잠깐은 이웃 해석 실패로 ICMP host unreachable 이 날 수 있지만, 커널이 원천마다 초당 약 1개로 제한하고 그것도 연결을 3초보다 빨리 끝낼 뿐이라 계산이 바뀌지 않는다(첫 실행 확인 항목).
   - 반대 방향: db01 은 기본 게이트웨이 192.168.126.1 이 사라져(ARP 실패) SMS 보고, syslog, ICMP 응답을 내보내지 못한다. 119 의 가용성 스위퍼 핑과 RTT 핑도 라우터에서 같은 꼴로 사라진다.
4. 앱 쪽(3초 상한, `civil-petition/deploy/templates/was/petition-context.xml:9-15`, 인벤토리 `app.db_wait_s` 3): JDBC 주소 `render.py:450`(192.168.126.11:5432). 새 연결은 PgJDBC connectTimeout 3초와 loginTimeout 3초가 거의 같은 순간에 닿아, 어느 쪽이 먼저냐에 따라 'SocketTimeoutException: Connect timed out' 이나 'PSQLException: Connection attempt timed out.'(loginTimeout 쪽, PgJDBC 42.3.3 에서는 이쪽이 먼저 날 가능성이 높다는 1회차 평가 지적)로 끝나고, 풀이 maxActive 에 닿은 동안 빌리기는 maxWait 3초에 'PoolExhaustedException: ... Timeout: Pool empty. Unable to fetch a connection in 3 seconds'. 읽기 경로는 `ApiExceptionHandler.java:23-26`(DataAccessException → ERROR 'database access failed: <원인>' 와 503 'database unavailable'), 신청과 담당자 처리는 `PetitionRepository.java:29, 79`(@Transactional: 트랜잭션을 열 때 연결을 못 얻으면 CannotCreateTransactionException 이 오류 페이지 필터로 가 500). 모두 3초를 넘겨 `SlowRequestFilter.java:17-29` 가 'slow request ... status= took_ms=' WARN 을 남긴다. 열린 연결의 읽기 상한(socketTimeout)은 없어(야간 배치 때문, 3초 결정 문단) 끊기는 순간 열려 있던 풀 연결(평시 WAS 마다 5)은 빌린 요청을 붙잡는다.
   - 실측 근거(옛 10초 설정): 11-25 T4 시험 r13(라우터에서 was01 → db01 5432 를 drop 한 300초)에서 was01 앱 로그 감시에 'database access failed: java.net.SocketTimeoutException: Connect timed out', 'Caused by: org.postgresql.util.PSQLException: The connection attempt failed.', 'PoolExhaustedException ... Unable to fetch a connection in N seconds', 'slow request ... took_ms=' 줄이 실제로 119 에 닿았다(§7 단계 2 조회). 그때는 loginTimeout 이 없어 연결 시간 초과 문구만 났다. 3초 설정에서는 loginTimeout 도 3초라 'Connection attempt timed out.' 이 섞이거나 앞설 수 있다(정답지는 둘 다 받는다).
5. 웹: `templates/web/petition-site.conf:16`(ProxyPass balancer, timeout 30). WAS 가 약 3초에 돌려준 503, 500 을 그대로 사용자에게. 붙잡힌 요청은 웹 30초보다 먼저 부하기(10초)가 포기한다.
6. 영향받지 않는 것: 웹 망, WAS 망, 라우터 상향과 관리 NIC. web01, was01, was02, 라우터의 SMS 보고, syslog, ICMP, NMS 폴링(라우터 상향 주소)은 그대로다. 66 의 러너 관리 연결은 관리망이라 db01 에도 닿는다.

## 5. 원인 규정 (원칙 5, 6)

- 근본(`root_cause.target_id`): 라우터 rt-core01 의 인터페이스 ens21 NMS 대상 번호 `79a3ac72-fde1-400a-89ca-bcbf414a4b92`(target_kind `node`, 네트워크 장비, `civil-petition/deploy/inventory/t.targets.json`). 결함은 그 포트의 물리 링크이고 고쳐야(케이블, 포트 교체) 재발이 막히는 곳이다.
- 계기(`trigger_target_id`): 비움. 계기도 같은 곳(ens21 링크)이다.
- 원칙 6: 앱의 연결 시도와 PostgreSQL 은 늘 하던 그대로 정당하다. 끊긴 것은 그 사이의 길이다. 원본도 서버 사이 망을 원인으로 적었다(같은 층위).
- 채점: 입도 `node`, accept `rt-core01 ens21`, ens21 대상 번호, `rt-core01`, 라우터 대상 번호(장비로 짚은 답도 받는다. F72-P 와 같은 장비지만 이 시나리오는 위치 채점이고 원인 꼴은 설명 칸에서 가른다). partial: db01 과 대상 번호, db01 업무 NIC ens18 대상 번호(사라진 서버만 본 답), was01, was02, web01 과 대상 번호(증상 서버만 본 답).
- 원칙 5: 정답은 "rt-core01 의 DB 쪽 포트 ens21 링크가 끊겨 DB 망으로 가는 길이 사라졌다" 이고 관제 데이터(NMS ens21 상태와 카운터, linkDown trap, 가용성, 라우터 syslog, db01 공백, WAS 시간 초과 줄)로 낼 수 있다. 링크가 왜 끊겼는지(케이블, 맞은편 포트, NIC)는 119 에 없어 `beyond_data` 로 옮겼다.

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 | 기대(합격 조건 아님) |
|---|---|---|
| 감지(lucida-next) | 지금 이 도메인에서 이벤트가 되는 것은 지표 이상(stream-anomaly)뿐이다(공통 지시의 119 사실). 후보: 라우터 ens21 트래픽(NMS 인터페이스 트래픽은 밴드가 있다) 0 으로 떨어짐, was01, was02 네트워크 롤업과 netstat 연결 수 변화 | 이벤트, 이벤트 클러스터, 인시던트는 기대로만 적는다. 포트 상태, trap, 가용성 DOWN, 로그 감시 ERROR 줄, syslog 는 이벤트가 되지 않는다(§11) |
| 피해 판정(러너) | 상주 부하(loadgen VM) 요약의 접수번호 조회 비정상 비율 ≥ 0.5, 3틱(§9-3, 로그인 없는 경로라 장애 내내 값이 실림) | 평시 조회 약 0.01(없는 번호 1% 고정, 30초 창 최대 약 0.055) |
| RCA(합격 기준) | §7 의 SRE 길: 사용자 피해 줄 → WAS 연결 시간 초과 줄 → db01 이 관제에서 사라짐 → 라우터 ens21 down, trap, Lost carrier → 운영자 변경 없음 | 각 단계 평시 데이터가 119 에 있음을 조회로 확인(§7) |

## 7. SRE 가 데이터로 원인을 찾는 길 (합격 기준)

알람과 AI 설정과 상관없이 119 에 쌓인 데이터만으로 간다. 장애 때 모양은 모두 "시험 실행에서 확인"(흔적 표의 이 계약 줄은 아직 실측 전. 같은 실행기의 r21(ens20) 줄을 참고로 적는다). 평시 값은 2026-11-27 KST 조회(11-25 ~ 11-26 은 T4 시험 구간이라 평시에서 뺀다).

| 단계 | 질문 | 어느 표, 칸, 대상 번호 | 평시 값(119 실조회) | 장애 때 모양 |
|---|---|---|---|---|
| 1 | 사용자가 실패하나 | CH `lucida.lucida_logs_local`(`event_source='log_monitor'`), web01 접근 로그 감시 `d510390b-d2d3-4da4-9f1b-64af8cd16bdd`(패턴 `" 5[0-9][0-9] `), Tomcat 접근 로그 `69ebf84f-81c2-42f0-b45d-a49f93804e1f`, `63de5bcc-f7cf-478b-ba1e-d9f044a70730` | 11-21 ~ 11-24, 11-27: 세 감시 5xx 줄 0(11-23 웹 오류 로그 6줄은 미끼 재배포) | 시험 실행에서 확인. 기대: /petition/api 요청마다 503, 500 줄(초당 5~30) |
| 2 | 어디서 실패하나, 어떤 꼴인가 | was01 앱 로그 `5aad9fbb-ec04-44b0-ba02-f81ced10af73`, was02 앱 로그 `e466fa3e-c26a-41a6-9c04-90e840b6df21`(ERROR 패턴 ` ERROR ` 또는 `Exception`, WARN 패턴 ` WARN `) | 11-27: 두 서버 ERROR 0줄. WARN 은 was02 에만 야간 배치 느린 요청 하루 2줄(배치는 was02 에서만 돈다, 11-21 ~ 11-24 같음), was01 은 0줄. 같은 꼴의 줄('Connect timed out', 'The connection attempt failed.', 'Unable to fetch a connection in N seconds', 'slow request ... took_ms=')이 11-25 T4 시험 r13(라우터 drop)에서 119 에 닿은 것을 확인 | 시험 실행에서 확인. 기대: 두 WAS 같은 초에 'database access failed' 와 원인 'Connect timed out' 이나 'Connection attempt timed out.', 피크에서는 'Unable to fetch a connection in 3 seconds' 도, 요청마다 took_ms 약 3000 의 'slow request' WARN. 'Connection refused' 는 없음 |
| 3 | DB 가 죽었나, 사라졌나 | VM `sms.cpu.utilization`(db01 `60579e7c-ac4f-41a0-a479-3cf25654fa28`) 표본 수, `sms.server.icmp_status`, `sms.server.rtt`(db01 `73343434-0302-468f-b738-643aec7ab84b`), PG `target_availability_history`(db01), CH `lucida.process_snapshot`(192.168.126.11, postgres), PostgreSQL 로그 감시 `17a5d386-4f83-42c2-9e05-e7eecbafeb74` | db01 CPU 표본 1시간 60개(1분마다), 최대 1.94%. ICMP 24시간 내내 1, RTT 3시간 중앙 4.97ms 최대 12ms. db01 가용성 DOWN 7일 0. postgres 프로세스 16개 pid 그대로(1시간 60표본). PostgreSQL 로그 감시 줄 11-27 0(7일 FATAL 20줄은 11-25 시험) | 시험 실행에서 확인. 기대: 주입 시각부터 db01 표본이 끊기고 ICMP 0, 약 1분 뒤 가용성 DOWN(r21 에서 두 WAS 가 약 1분 뒤 DOWN). 같은 때 web01, was01, was02 는 표본과 ICMP 가 이어짐. 라우터 NMS ICMP(`nms.network.icmp_status`)는 평시에도 7일 21번 짧은 거짓 DOWN(가용성 network_device_icmp, 번마다 1분 안, 119 부하)이 있어 라우터 핑 한 번 빠짐은 근거로 쓰지 않는다. 링크가 돌아온 뒤 postgres pid 그대로, PostgreSQL 로그에 종료, 시작 줄 없음 |
| 4 | 길이 어디서 끊겼나 | VM `nms.interface.if_oper_status`, `if_out_octets`, `if_in_octets`(rt-core01 ens21 `79a3ac72-fde1-400a-89ca-bcbf414a4b92`), CH `lucida.snmp_traps_local`(source_ip 192.168.123.254), PG `target_availability_history`(라우터 `65b4ab9c-d1b4-42f6-be5c-7efb5c34cb3c`, resource_kind network_interface), CH `lucida.syslog_local`(hostname rt-core01, app_name systemd-networkd) | ens21 if_oper_status 24시간 최소, 최대 모두 1. if_out_octets 3시간 중앙 약 104,896, 최소 약 91,883, if_in_octets 중앙 약 258,234. 7일 trap 3행(11-25 T4: ens20 linkDown 1, 라우터 재시작 뒤 linkUp 과 coldStart 계열 2). 가용성 network_interface 줄 7일 2(11-25 ens20 DOWN, UP). Lost carrier 줄 7일 1(11-25 ens20). 라우터 network_device, network_snmp 의 짧은 DOWN, UNKNOWN 은 7일 수십 번(119 부하로 폴링이 늦은 거짓 DOWN, APT4B 판단) | 시험 실행에서 확인. 기대: if_oper_status 2, 옥텟 카운터 멈춤(늘지 않음), linkDown trap(1.3.6.1.6.3.1.1.5.3) 1행, 가용성 network_interface ens21 DOWN 과 cleanup 뒤 UP, syslog 'systemd-networkd: ens21: Lost carrier' 와 'Gained carrier'. linkUp trap 은 r21 처럼 빠질 수 있다(§9-5) |
| 5 | 누가 무엇을 바꿨나 | CH `lucida.syslog_local`(source_ip 192.168.123.254), app_name sshd, sudo, kernel | 라우터 sudo 명령 하루 2~8번: 평시 운영자 세션의 `nft list ruleset`, `tail -n` 뿐(11-21 ~ 11-24, 11-27). `nft -f` 는 11-25 T4 시험 1번뿐 | 시험 실행에서 확인. 기대: 주입 시각 앞뒤에 설정 변경 명령(nft -f, 배포)과 kernel 차단 줄 없음(평시 조회 세션은 겹칠 수 있고 그때는 조회 명령만). 66 출발 syslog 에도 주입 흔적 없음(r21 실측: 평시에 없던 꼴 0) |

조회 근거(모두 분할 칸과 시각 범위를 건 읽기 전용, 104 의 readonly=2 사본과 VictoriaMetrics LB):

```
-- 단계 1, 2: 날짜별 피해 줄 수
SELECT toDate(timestamp,'Asia/Seoul') d, host_name, target_id, severity_text, count() FROM lucida.lucida_logs_local
WHERE timestamp_date >= today()-7 AND timestamp > now() - INTERVAL 7 DAY AND event_source='log_monitor'
  AND target_id IN ('66b554a1-...','d510390b-...','5aad9fbb-...','e466fa3e-...','69ebf84f-...','63de5bcc-...','17a5d386-...') GROUP BY 1,2,3,4
-- 단계 2: 줄 꼴(숫자 지움)
SELECT host_name, severity_text, replaceRegexpAll(substring(body,1,170),'[0-9]+','N') b, count() FROM lucida.lucida_logs_local
WHERE timestamp_date >= today()-3 AND timestamp > now() - INTERVAL 3 DAY AND event_source='log_monitor'
  AND target_id IN ('5aad9fbb-...','e466fa3e-...') GROUP BY 1,2,3 ORDER BY 4 DESC
-- 단계 3: 프로세스, 연결 표본
SELECT host_name, name, user, uniqExact(pid), uniqExact(ts) FROM lucida.process_snapshot
WHERE ts > now() - INTERVAL 1 HOUR AND host_name IN ('192.168.126.11','192.168.125.11','192.168.125.12') GROUP BY 1,2,3
SELECT host_name, direction, process, state, remote_addr, count(), uniqExact(timestamp) FROM lucida.host_connections
WHERE timestamp > now() - INTERVAL 1 HOUR AND (local_port=5432 OR remote_port=5432) GROUP BY 1,2,3,4,5
-- 단계 3, 4: 가용성 이력(PG, begin read only)
select target_id, resource_kind, coalesce(resource_key,''), status, started_at from target_availability_history
where started_at > now() - interval '7 days' and target_id in ('73343434-...','79a3ac72-...','65b4ab9c-...')
-- 단계 4: trap, 링크 syslog
SELECT source_ip, trap_oid, count(), min(received_at) FROM lucida.snmp_traps_local
WHERE toYYYYMM(received_at) >= 202611 AND received_at > now() - INTERVAL 7 DAY GROUP BY 1,2
SELECT hostname, app_name, message, count() FROM lucida.syslog_local WHERE toYYYYMM(received_at) >= 202611
  AND received_at > now() - INTERVAL 7 DAY AND source_ip='192.168.123.254' AND message ILIKE '%carrier%' GROUP BY 1,2,3
-- 단계 4: VictoriaMetrics min_over_time / max_over_time(nms.interface.if_oper_status{target_id="79a3ac72-..."}[24h]),
--         quantile_over_time(0.5, nms.interface.if_out_octets{...}[3h]), min_over_time(sms.server.icmp_status{host_name="192.168.126.11"}[24h])
-- 단계 5: 라우터 sudo 명령 꼴
SELECT toDate(received_at,'Asia/Seoul'), extract(message, 'COMMAND=(\S+ ?\S*)'), count() FROM lucida.syslog_local
WHERE toYYYYMM(received_at) >= 202611 AND received_at > now() - INTERVAL 7 DAY AND source_ip='192.168.123.254' AND app_name='sudo' GROUP BY 1,2
```

표본 데이터 여부: 1, 2, 5 단계는 전수 원천(로그 감시 줄, syslog), 3 단계는 1분 SMS 표본과 60초 프로세스 표본의 전체 목록, 4 단계는 60초 NMS 폴링과 trap, 가용성 이력이다. 트레이스, APM 은 이 도메인에 없다.

## 8. 관측 근거 요약과 감별

- must_support(정답지 6개): ens21 if_oper_status down 과 카운터 멈춤, linkDown trap 과 가용성 ens21 DOWN, 라우터 syslog Lost carrier 와 설정 변경 명령(nft -f, 배포) 없음, db01 만 관제에서 사라짐, 두 WAS 의 연결 시간 초과 ERROR 와 3초 느린 요청 WARN, 웹과 Tomcat 접근 로그 5xx.
- must_rule_out(정답지 6개): 라우터 접근 규칙 변경(F72-P 꼴), db01 서버 다운이나 재부팅과 PostgreSQL 다운, WAS 배포나 설정 변경, DB 과부하나 느린 질의, 라우터 전체 장애, 웹 서버 장애.
- **F72-P 와 가르는 근거**(공통 지시, 1회차 평가 대비, 정답지 must_rule_out 첫째와 distinguishing_evidence 에 적음):

| 근거 | F72-P(운영자 접근 규칙) | F72-H(링크 끊김) |
|---|---|---|
| 라우터 ens21 if_oper_status | up 그대로 | down |
| linkDown trap, 가용성 network_interface | 없음 | ens21 linkDown, ens21 DOWN |
| 라우터 syslog | 운영자 sshd 로그인, sudo `nft -f`, kernel '[DB ACL] ... DPT=5432' | 'systemd-networkd: ens21: Lost carrier', 설정 변경 명령 없음(평시 운영자 조회 세션이 우연히 겹칠 수는 있다) |
| ens21 트래픽 | 송신만 거의 0(수신은 db01 보고 몫이 남음), 링크는 올라감 | 송수신 카운터 모두 멈춤 |
| db01 관제 | 보고, ICMP 1, 가용성 그대로 | 보고 끊김, ICMP 0, 가용성 DOWN |
| 앱 오류 꼴과 시간 | 몇 ms 안의 'Connection refused', 느린 요청 WARN 없음 | 3초 기다린 'Connect timed out' 이나 'Connection attempt timed out.', 'Unable to fetch a connection in 3 seconds', took_ms 약 3000 의 WARN, 첫 몇 요청은 응답 없음 |

- contrast_with: F72-P(위 표), F72-R(새 연결만 버려 풀 수명만큼 늦게, 쿠버네티스, Oracle), F80-P(DB 프로세스가 실제로 죽어 거부, 서버 보고는 이어짐, S 도메인).
- 채점: 위치 채점은 F72-P 와 같다(둘 다 rt-core01 을 accept 로 받는다, 1회차 평가 권고로 accept 유지). 원인 꼴(링크 끊김 대 접근 규칙 변경)은 설명 칸(cause, distinguishing_evidence)에서 가른다. 장부 §6 기록에 같은 말을 적었다.
- 장부 유사도(§4-1 행, §6 기록): F72-P 와 주 묶음(K), 정답 위치(노드, 디스크, 같은 라우터)가 같다. 묶음이 K+G 대 K 로 다르고(계기가 변경 대 비변경), 기전(접근 규칙의 TCP 재설정 대 물리 링크 끊김과 경로 소멸), 흔적 원천(운영자 syslog 와 kernel 대 포트 상태, trap, 가용성, 서버 관제 공백)이 다르다. 겉 증상이 같은 것은 관계 H 의 뜻이다.
- 러너 배제 조건: 상주 부하가 끊김(achieved_rps < 2.0).
- 미끼와의 관계: 이 도메인의 U7 미끼(WAS 재배포, 웹 설정 재적용)는 WAS 와 웹에서만 나고 링크 상태를 바꾸지 않는다. 평시 운영자 세션이 우연히 주입 근처 라우터에 열리면 조회 명령(`nft list ruleset`, `tail`)만 남는다. "장애 근처 라우터 운영자 변경 = 원인" 규칙은 이 시나리오에서 빗나간다(§3 규칙 적중률).

## 9. 러너 판정과 강도, 피해 계산 (원칙 9)

### 9-1. 피해가 나는 이유: 길이 막힌다

- 평시(근거 `~/dev/testbed-ops/v4/agent-out/APT2-facts/` k6 창): 평균 약 19 rps, 설계 피크 30, 저점 5, 피크 p95 약 8ms. db01 CPU 약 1%, WAS CPU 약 1.6%.
- 부하기의 모든 요청은 `/petition/api/...` 이고 모든 경로가 DB 를 쓴다. DB 망으로 가는 유일한 길(ens21)이 끊기면 정적 첫 화면을 뺀 요청이 100% 실패한다. CPU 계산(앞선 카드의 함정)과 무관하다: 피해는 자원 경쟁이 아니라 "연결이 맺어지지 않음"에서 난다.
- 문턱: 설계 §6-3 피해 판정 "사용자 비정상 비율 5% 이상 2분 지속" 에 대해 약 100% 가 10분. 감시 패턴에 걸리는 피해 줄은 웹 접근 로그 5xx, Tomcat 접근 로그 5xx, 앱 로그 ERROR 와 `Exception` 줄, 그리고 3초 이상 요청 WARN 이다.

### 9-2. 3초 상한에서 요청 하나가 어떻게 끝나나

| 요청이 만난 것 | 앱 안 동작 | 끝나는 시각과 상태 | 수 |
|---|---|---|---|
| 끊기는 순간 열려 있던 풀 연결(평시 WAS 마다 5, 연결 표본 실측) | 빌린 뒤 검증 질의나 업무 질의를 쓰고 대답을 기다림. 읽기 상한이 없고 TCP 재전송 포기(tcp_retries2 기본 15, 약 15분)가 min_hold 10분보다 길다 | 부하기 10초에 상태 0(웹 30초, 앱은 링크가 돌아올 때 풀림) | WAS 마다 첫 약 5개, 합 약 10개. 그 뒤 이 연결은 돌아오지 않는다 |
| 빈 풀에서 새 연결(풀 크기 < 40) | PgJDBC connectTimeout 3초(loginTimeout 3초) | 약 3.0~3.3초에 503, 신청과 처리는 500 | 나머지 거의 모두 |
| 풀 크기가 40 에 닿은 동안 | maxWait 3초 기다림 뒤 PoolExhaustedException | 약 3초에 503, 500 | 피크에서만: WAS 마다 동시 연결 시도 = 초당 15 × 3초 = 45 > 40 − 5(붙잡힘) = 35, 넘친 약 10개 |

- 어느 경우도 부하기 10초 시간 제한 안에 답한다(붙잡힌 첫 약 10개 빼고). 동시 사용 자원: Tomcat 스레드 WAS 마다 약 50(최대 200, `server.xml` maxThreads), 웹 동시 프록시 약 90, 부하기 VU 피크 약 110~120(최대 150, §9-3 모의에서 버린 반복 0).
- 옛 10초 설정(11-25 막힘)과 비교: 11-25 T4 시험 r13(라우터에서 was01 → db01 drop, 10초 설정)에서 부하기 시간 초과 비율이 0.49 로 붙고 entry_status 가 30초 넘게 0 이었다(`~/dev/testbed-ops/v4/agent-out/APT4B-facts/r13-rtcfg.samples.jsonl`, 14:22:11 ~ 14:22:41 UTC 네 표본 연속 0). 3초 설정은 새 연결과 풀 기다림이 모두 3초에 끝나 이 꼴을 붙잡힌 첫 몇 개로 줄인다.

### 9-3. 러너 필수 중단(entry_status 0, 2틱)을 피하는가: 모의

- entry_status 는 상주 부하의 마지막 신청 응답 상태다(`scripts/scenarios/profiles/loadgen_monitor.py:114-117`, 완료 순서로 마지막 submit 의 상태, 응답 없음 0). 러너는 15초마다 그 값을 읽고 두 번 연속 0 이면 끊는다.
- 0 이 될 길은 붙잡힌 첫 약 10개 가운데 신청이 있고, 그 신청의 시간 초과(주입 + 10초) 뒤 15초 넘게 다른 신청 응답이 없을 때뿐이다. 장애 중 신청은 3초 남짓에 500 으로 답하고, 로그인 상태가 풀려 신청이 사라지는 것은 몇 분 뒤다(아래 모의 약 7~8분).
- 모의(`/home/sjbang/tmp/claude-1008/-home-sjbang-dev-lucida-next/ee261603-595a-4ca8-a253-3a4bd5d2bb18/scratchpad/c1/c1_abort_sim.py`, 결과 `sim1000.txt`): k6 constant-arrival-rate, VU 40 ~ 150, 스크립트의 누적 비중표와 로그인 규칙 그대로, 세션 요청은 고정 WAS, 세션 없는 요청과 로그인은 번갈아. t0 에 WAS 마다 열린 연결 K 개, 그 뒤 첫 K 개 요청은 10초 시간 초과(0), 나머지는 3.0~3.3초 뒤 503/500. 문서 10초마다, 틱 15초마다(위상 무작위). 장애 10분, 조건마다 1,000번.

| rps | K | 2틱 연속 0(중단) | 0 틱 하나 이상 | 성공(조회 ≥ 0.5, 3틱) 도달 | 장애 중 창당 조회 최소 | 조회 비정상 비율 최소 | 신청이 0건인 창이 처음 나오는 시각(중앙) | VU 최대 |
|---|---|---|---|---|---|---|---|---|
| 5 | 5 | 0 | 10.2% | 1000/1000, 최대 74초 | 24 | 1.00 | 약 482초 | 40 |
| 10 | 5 | 0 | 5.5% | 1000/1000, 최대 73초 | 59 | 1.00 | 약 445초 | 43 |
| 19 | 5 | 0 | 3.3% | 1000/1000, 최대 71초 | 126 | 1.00 | 약 419초 | 73 |
| 30 | 5 | 0 | 3.1% | 1000/1000, 최대 72초 | 215 | 1.00 | 약 410초 | 109 |
| 5 | 10 | 2(0.2%) | 16.3% | 1000/1000, 최대 72초 | 22 | 1.00 | 약 484초 | 40 |
| 10 | 10 | 0 | 13.5% | 1000/1000, 최대 73초 | 57 | 1.00 | 약 464초 | 53 |
| 19 | 10 | 0 | 8.0% | 1000/1000, 최대 72초 | 122 | 1.00 | 약 445초 | 83 |
| 30 | 10 | 0 | 9.1% | 1000/1000, 최대 71초 | 216 | 1.00 | 약 426초 | 119 |

- 판단: 평시 열린 연결 5개(연결 표본 실측)에서 4,000번 가운데 중단 0번. 열린 연결을 두 배(10개)로 잡아도 저점에서만 0.2%. 0 이 한 틱 나오는 것은 몇 %(첫 시간 초과 신청이 15초 안에 다음 신청 500 에 덮이지 않은 경우)라 첫 실행 확인 항목에 둔다.
- 남는 위험(모의 밖): 열린 연결이 평시보다 많을 때(야간 배치 02:20 창, 피크의 순간 동시성), 링크가 끊기기 전 진행 중이던 요청, 3초를 크게 넘는 실패 시간(예: 주소를 내리기 전 이웃 해석 대기). 녹화 시각이 야간 배치 창(02:00 ~ 05:00 KST)과 겹치지 않는지는 운영 하네스가 본다.
- **전제: 3초 설정이 테스트베드에 적용돼 있어야 한다.** 아직 적용 전이다(지휘 세션이 적용과 재기준선 예정). 옛 10초 설정이면 r13 꼴로 30~45초 안에 중단된다. 녹화 대기 큐 note 와 첫 실행 확인 항목에 넣었다.

### 9-4. 판정과 강도

- 강도: 하나로 고정(`approved-fixed-f72-h`): host 192.168.122.1, node pve66, vmid 201, net net3. 링크는 끊거나 잇는 것뿐이라 사다리가 없다. 동반 부하 없음: 저점(5 rps)에서도 장애 중 30초 창마다 조회가 최소 24건(모의) 모두 실패해 판정이 선다.
- success: 상주 부하(domain civil-petition) 접수번호 조회 비정상 비율 ≥ 0.5, 3틱(45초). 모의에서 모두 74초 안에 선다(첫 창이 평시 몫과 섞이는 동안 늦어짐). min_hold 10m, settle 60s, level timeout 15m, max_injection_duration 20m. 신청 단계 비율은 쓰지 않는다(장애 중 로그인 상태 소실로 몇 분 뒤 신청이 사라짐, F72-P 평가 지적과 같음).
- must_rule_out: achieved_rps < 2.0(저점 목표의 40%, 부하가 끊겼을 때만. 3초 실패는 도착률을 바꾸지 않는다).
- abort: entry_status = 0, 2틱(§9-3).
- recovery: target_health 200(전역), entry_status 200(로그인이 풀렸던 가상 사용자가 다시 로그인한 뒤의 첫 신청), 조회 비정상 비율 < 0.1, 2틱, 10분. VM 도메인 회복 규칙(F72-P 가 compile-plan 에 더한 규칙)을 따른다.
- preflight: coordinator-clean, clean-window, baseline-traffic, target-health, vm-entry-health. 실행기 preflight: VM 201 의 QMP 소켓이 있고 net3 이 올라가 있음(도우미 link-check). 매니페스트 preflight 는 network-oob-recovery(이번에 vmlink.state 를 매핑에 더함).
- 시각(예상): t0 링크 내림(1초 안) → t0 + 1초 안에 첫 실패 시작(새 연결 3초), 첫 약 10개는 t0 + 10초에 시간 초과 → t0 + 약 1분 안에 라우터 linkDown trap 과 가용성 ens21 DOWN(r21 실측: trap 약 36초, 가용성 약 43초 뒤)과 db01 가용성 DOWN → 약 1분 15초 안에 success → t0 + 10분 cleanup 링크 올림 → 라우터가 DB 망을 되살리고 다음 요청부터 회복(r21 에서 링크 올림 뒤 약 30초 안에 비정상 0). 붙잡힌 연결은 다음 재전송(최대 약 2분 간격)에서 풀린다.
- 안전: 실행기 가드(U8, 66 도우미 안)가 링크를 내리기 전에 걸리고 기한(max_injection_duration + 600초 = 30분)이 되면 링크를 올린다. 66 의 관리망과 터널, 라우터 상향과 관리 NIC 는 허용 목록 밖이다(설계 §3-5 규칙 2). QMP 링크 상태는 VM 이 다시 켜지면 사라진다.

### 9-5. 첫 실행 확인 항목 (아직 실측 없음)

- **3초 설정 적용 여부**: 두 WAS 의 petition.xml 에 connectTimeout=3, loginTimeout=3, maxWait=3000 이 들어갔는가(지휘 세션 몫). 안 됐으면 돌리지 않는다.
- entry_status 0 틱이 몇 번 나오는지(모의 3~10%가 한 틱), 2틱 연속이 없는지.
- 앱 오류 줄 꼴: 'Connect timed out' 과 'Connection attempt timed out.'(loginTimeout 쪽) 가운데 어느 것이 나는지, 'Unable to fetch a connection in 3 seconds' 가 피크에서 섞이는지, 'slow request' WARN 이 요청마다 나는지(took_ms 가 3000 근처라 문턱 아래로 떨어지는 것이 있을 수 있음).
- 붙잡히는 요청 수(평시 열린 연결 5 x 2)와 그 요청이 링크가 돌아온 뒤 풀리는지.
- 소리 없이 버림: 실패 시간이 3초 근처인지(ICMP 로 빨리 끝나는 몫이 있는지), 라우터 networkd 가 주소와 경로를 실제로 내리는지(내리지 않으면 이웃 해석 실패로 ICMP host unreachable 이 원천마다 초당 약 1개 올 수 있다. 피해 계산은 같다).
- NMS: ens21 if_oper_status 2 와 옥텟 멈춤(60초 폴링), linkDown trap 1행, 가용성 network_interface ens21 DOWN 과 UP. linkUp trap 은 r21 에서 빠졌다(라우터 snmpd 'empty variable list in _query'). 이번에도 빠지는지 적는다.
- db01: 보고 공백 길이, 가용성 DOWN 까지의 시간(r21 약 1분), SMS 에이전트가 끊긴 동안의 값을 나중에 보내는지(설계 §13-3), 링크가 돌아온 뒤 db01 syslog 의 rsyslogd 'omfwd ... resumed' 같은 줄(r20 에서 was01 에 났음), postgres pid 그대로인지.
- 장애 중 상주 부하 조회 비정상 비율이 1.0 근처로 실리는지, 신청 단계가 실제로 사라지는지.
- 회복: 링크 올림 뒤 회복 시간, cleanup 뒤 db01 쪽 옛 백엔드(붙잡혔던 연결)가 정리되는지(연결 표본과 postgres 프로세스 수가 평시 10, 16 으로 돌아오는지).
- 66 출발 syslog 에 주입 흔적이 없는지(r21 실측: 평시에 없던 꼴 0).

- 119 부담: 실패 요청마다 감시 패턴에 걸리는 줄이 약 7줄(웹 접근 1, Tomcat 접근 1, 앱 ERROR 1, 스택의 Exception 줄 약 3, slow request WARN 1)이라 10분에 약 2.1만~12.6만 줄(저점~피크). F72-P 보다 약 1/6 많다. 처음 실행에서 로그 감시 수집 지연과 119 ClickHouse 메모리 예산(설계 §3-6)을 함께 본다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Magic Pages 2026-10-20, 링크)와 요소별 대응표가 있고 기전이 같다. 처음 원본 GitHub 2018-10-21 을 바꾼 까닭을 적었다(§2) (원칙 1)
- [x] 분류 장부를 갱신했다(§4-1 행, §6 기록). 묶음 K 4 → 5(4%), 정답 위치 노드, 디스크 10 → 11(9%), 결제 경로 14/121(무관), 민원 2 → 3. 어느 축도 20% 에 닿지 않고 단순 규칙 판정은 보류(통과)다(§3) (원칙 2)
- [x] 근본 원인 위치가 인프라 지점(rt-core01 ens21 = VM 201 net3, 망 설정, rp_filter, 평시 ACL)과 코드 위치로 확인됐다(§4) (G1)
- [x] 근본 원인의 흔적 자리가 119 실데이터에 있다: NMS ens21 if_oper_status 와 옥텟(평시 24시간 1, 중앙 약 10만), trap 표(11-25 같은 꼴 linkDown 1행 수집됨), 가용성 이력(ens20 같은 꼴 DOWN 수집됨), 라우터 syslog Lost carrier(수집됨). 장애 때 값은 시험 실행에서 확인(§7) (원칙 3)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: trap, syslog, 로그 감시, 가용성 이력, SMS 지표, NMS 폴링(§7) (원칙 3)
- [x] 계기의 흔적이 있다: 포트 상태 down, linkDown trap, Lost carrier, 가용성 인터페이스 DOWN, 그 뒤 서버의 관제 공백. 인공 지연 없음(지연은 앱 자신의 연결 시간 제한) (원칙 4)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다(§5). 링크가 끊긴 물리 까닭은 beyond_data (원칙 5)
- [x] 정답지 세 칸이 원칙 6대로 채워졌다(§5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다(§6) (원칙 7)
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: 게스트 안에서 아무것도 돌지 않고, 66 의 도우미, 가드, hvjob 계정 줄은 66 거르기가 막는다(r21 실측 평시에 없던 꼴 0). compile-plan 으로 만든 실행기 입력에 시나리오 id 가 없다(인자는 vmid, net 뿐) (원칙 8)
- [x] 부하 상한과 타임아웃 사슬(연결 3초, 풀 3초, 읽기 상한 없음, 부하기 10초, 웹 30초, Apache 연결 3초)을 넣은 피해 계산과 중단 조건 모의가 있다(§9) (원칙 9)

## 11. 119 보강 후보 (고치지 않는다, 지휘 세션 몫)

공통 지시의 119 사실(11-25): 이 도메인에서 이벤트는 지표 이상(stream-anomaly)만 생기고, 로그 감시 줄(ERROR 포함), syslog, trap, 가용성 DOWN, 인터페이스 상태는 이벤트가 되지 않으며, 한 서버 안 여러 시리즈 지표는 이상 판정하지 않는다.

| 근거 | 지금 | 보강하면 |
|---|---|---|
| 라우터 ens21 if_oper_status down, linkDown trap | 이벤트 안 됨 | 장비 포트 상태 바뀜 이벤트가 서면 원인 위치가 시각과 함께 이벤트에 남는다(이 시나리오의 근본 원인 그 자체) |
| 가용성 network_interface ens21 DOWN, db01 서버 DOWN | 이벤트 안 됨 | 가용성 DOWN 이벤트가 서면 "포트 DOWN 과 그 뒤 서버 DOWN" 이 한 묶음 재료가 된다 |
| 라우터 syslog 'Lost carrier' | 이벤트 안 됨 | syslog 링크 줄이 이벤트가 되면 같은 근거가 하나 더 선다 |
| 두 WAS 앱 로그 ERROR, WARN 급증(평시 0, 2) | 로그 감시 줄은 이벤트 안 됨 | 로그 감시 원천의 오류 급증 이벤트가 서면 사용자 피해가 인시던트 재료가 된다 |
| 웹, Tomcat 접근 로그 5xx 급증 | 같음 | 같음 |
| NMS ens21 트래픽 0 | 밴드가 있어 지표 이상 후보(감소 방향을 잡는지, 카운터가 멈춘 경우 값이 어떻게 실리는지는 모름) | 이 시나리오에서 이벤트가 생길 거의 유일한 길. 시험 실행에서 이벤트 여부를 본다 |
| db01 SMS 지표 끊김 | 값이 없으면 지표 이상도 서지 않음 | 수집 공백(데이터 끊김) 이벤트가 서면 "서버가 사라짐" 이 재료가 된다 |

## 12. 이번에 바꾼 부분 (지휘 세션 검토)

1. `scripts/scenarios/profiles/vmlink_state_executor.py`: 첫 계약 F72-H(VM 201 net3)와 설명. 동작, 파라미터, 도우미는 그대로(새 모드 없음). `registry/profiles.json` vmlink.state 를 live_supported true, 같은 계약으로.
2. `scripts/scenarios/generate-manifests.py`: vmlink.state 의 매니페스트 preflight 를 network-oob-recovery 로 매핑(이 매핑이 없으면 생성기가 멈춘다).
3. 시험 손질(F72-P, F80-P 와 같은 꼴): 개수 핀(카탈로그 146, ready 121, fixed 140, 후보 7), live 와 ready 고정 목록 끝에 F72-H, 노드 실행기 가드 시험에서 vmlink.state 를 "아직 안 쓰는 실행기" 견본에서 "쓰는 실행기" 견본으로 옮기고 카탈로그 쪽 점검 집합에 더함, 라이브 프로파일 목록에 vmlink.state.
4. 앱 코드, 앱 설정, 러너 저장소는 바꾸지 않았다.

## 13. 사람이 정할 것

- 3초 상한 적용 확인을 사람(지휘 세션)의 순서로만 둘지, 러너 preflight 에 "WAS 의 petition.xml 에 connectTimeout=3, loginTimeout=3, maxWait=3000 이 들어 있음" 확인을 더할지(이 도메인 VM preflight 의 버전 표류 검사, 설계 §6-4 와 같은 자리. 더하면 러너나 실행기 preflight 변경이라 이 카드 범위 밖). 지금은 녹화 대기 큐 note 에 "적용과 재기준선 확인 뒤에만 집어 간다" 순서만 적었다.
- 채점 accept 에 라우터 장비(rt-core01)를 둔 채로 둔다(1회차 평가 권고로 유지). F72-P 와 위치 채점이 같고 원인 꼴은 설명 칸에서만 가른다.
- 녹화 시각이 야간 배치 창(02:00 ~ 05:00 KST)과 겹치면 열린 풀 연결이 늘어 중단 위험이 커진다(§9-3). 이 창을 피할지.
