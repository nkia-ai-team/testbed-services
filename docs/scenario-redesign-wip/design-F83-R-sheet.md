---
title: F83-R 설계 시트, 민원 포털 DB 서버에서 작업이 메모리를 거의 다 써 서버가 먹통이 되고 하이퍼바이저로 되살림
status: Draft
owner: project
last_reviewed: 2026-11-28
tags:
  - scenario
  - design
  - memory
  - civil-petition
summary: 민원 포털(civil-petition) DB 서버 db01(8GB, 스왑 없음)에서 일회성 데이터 작업이 메모리를 천천히 거의 다 쥐고 파일 캐시를 32~64 MiB 만 남겨, 커널이 프로그램 코드와 데이터를 디스크에서 거듭 다시 읽으며(page cache thrashing) OOM killer 가 끼어들지 않은 채 서버 전체가 대답을 멈추고, 약 10분 뒤 사람이 66 하이퍼바이저에서 리셋해 되살리는 시나리오(Wikimedia 2014-06-19 pc1002 재구성)의 설계 근거. 사용자와 정한 카드 C11(C8 대체).
---

# F83-R 설계 시트

- id `F83-R`, slug `f83-r-petition-db-job-memory-thrash-hypervisor-reset`, 도메인 `civil-petition`(SMS + NMS 테스트베드)
- 사용자와 정한 카드: C11 "SMS + NMS db01 이 메모리 바닥으로 먹통, 하이퍼바이저로 되살림"(C8 대체, 지시 `~/dev/testbed-ops/v4/scenario-design/brief-C11-db-freeze.md`)
- 상태: 후보(ready + `stage: candidate`), 설계 강도 하나로 고정한 evaluation 모드, 녹화 대기 큐에 추가(note: 새 테스트베드 첫 시나리오, 흔적 표 실측과 119 보강 뒤 녹화)
- 주입 수단: 기존 실행기 `vmhost.job` 에 새 모드 `memload`(이름 붙은 유닛으로 도는 python3 작업, 파일 캐시를 한 띠에 묶음)와 첫 계약, 66 도우미 `t4-hv-exec` 에 새 동작 `vm-check`, `vm-state`, `vm-hold`, `vm-reset`, `vm-release`(QMP system_reset). 동반 부하 없음(상주 부하가 피해를 낸다)

## 1. 요약과 비유

민원 포털은 웹(web01), WAS 두 대(was01, was02), DB(db01) VM 이 라우터(rt-core01)로 이어진 전통 3계층이고 SMS 와 NMS 로만 관제된다. db01 은 메모리 8GB 에 스왑이 없고 PostgreSQL 16 의 shared_buffers 가 2GB 다. 평시에는 데이터가 OS 파일 캐시에 약 5GB 더 들어 있다.

db01 에서 일회성 데이터 작업(이름 붙은 유닛 `petition-export`, root 의 python3)이 24 MiB/s 로 약 4분 동안 메모리를 늘려 약 5.5 GiB 를 쥐고, 파일 캐시를 32 MiB 와 약 64 MiB 사이로 묶는다(그 아래로 내려가면 16 MiB 를 돌려준다). 스왑이 없으면 커널은 익명 메모리(작업 메모리, PostgreSQL 공유 버퍼)를 내보내지 못하고 파일 쪽(모든 프로세스의 프로그램 코드, 라이브러리, 로그, 공유 버퍼 밖 데이터 파일)만 내쫓는다. 그 쪽은 곧바로 다시 필요해 디스크에서 다시 읽히고 또 내쫓긴다. 내쫓기가 늘 "회수 진전"으로 세어지므로 커널은 메모리가 바닥났다고 판단하지 않고 OOM killer 가 돌지 않는다. PostgreSQL, sshd, SMS 에이전트가 디스크 읽기에 묶여 서버 전체가 대답하지 못하고(커널은 TCP 연결 맺기와 핑에 답할 수 있다), 두 WAS 의 새 DB 연결이 PgJDBC 3초 상한에 끊겨 포털 거의 모든 요청이 503, 500 이 된다. 약 10분 뒤 사람이 66 하이퍼바이저에서 db01 을 리셋하면 부팅과 PostgreSQL 복구 재시작으로 되살아난다.

비유: 책상 위를 짐으로 거의 다 덮고 손바닥만 한 자리만 남기면, 일하는 사람은 서류 한 장을 볼 때마다 다른 서류를 서랍에 넣고 꺼내기를 끝없이 되풀이한다. 일은 멈추지 않은 것처럼 보이지만 아무것도 끝나지 않는다. 관리인(OOM killer)은 책상이 "아직 정리되고 있다"고 보아 짐을 치우지 않고, 결국 사람이 전원을 내렸다 올린다.

## 2. 원본 사례

- **Wikimedia, 2014-06-19**, 사고 기록 "Incidents/20140619-parsercache",
  [공식](https://wikitech.wikimedia.org/wiki/Incident_documentation/20140619-parsercache), [IRC 기록](https://wikitech.wikimedia.org/wiki/Incident_documentation/20140619-parsercache/IRC_log). 자료 문서 `ref-real-world-incidents.md` M20 에 이 시나리오에서 더했다(출처가 말한 사실만).
  - "There was a site outage and 5xx spike between 2014-06-19 19:07:32 and ~19:20 caused by pc1002 going down": parser cache MariaDB 서버 셋 가운데 pc1002 가 감당하지 못하자 사이트가 무너졌다.
  - "mutante observed an OOM error on the pc1002 console", "RobH observed no ssh/ping and so powercycled pc1002"(IRC: "checking mgmt", "powercycled pc1002"). "mysqld failed to restart after powercycle due to a pid file", "After restart and MariaDB recovery the site came back up."
  - "pc1002 evidently didn't die cleanly and was still responding to at least some mysqld tcp connections for at least 1min", MediaWiki 부하 분산은 pc1002 를 빼지 않고 재부팅될 때까지 기다리며 멈췄다.
  - 계기: 세 서버 모두 트래픽 급증, pc1002 의 bytes_in 과 wait_io 급증, "puppet ran at that time and took longer than normal, likely due to the already heavy mysqld load"(puppet 이 느려진 것은 mysqld 부하 때문이라고 봄), "puppet running was the final trigger but also *not* the whole problem", 스왑은 "unknown considering oom/panic". 콘솔에 보인 것도 "No exact timestamp available for the OOM and/or kernel panic mutante saw" 라 OOM 인지 커널 패닉인지 단정하지 않는다. **무엇이 메모리를 다 썼는지는 적혀 있지 않다.** 후속: puppet 을 nice 로, 부하가 비정상이면 puppet 을 시작하지 않기, 들쭉날쭉한 DB 호스트 다루기 개선.

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 트래픽 급증(공동 원인)에 그 부하로 평소보다 오래 걸린 DB 서버 위 운영 작업(puppet 실행, 마지막 계기)이 겹침 | DB 서버 db01 에서 돈 일회성 데이터 작업(유닛 petition-export, root python3)이 메모리를 거의 다 쥠 |
| 원인이 된 결함 | 콘솔에 OOM 이나 커널 패닉, 메모리를 쓴 주체와 스왑 기여는 출처에 없음 | 서버 메모리 부족, 스왑 0, 파일 캐시만 32~64 MiB 남음 |
| 전파 경로 | 서버가 깨끗하게 죽지 않고 일부 TCP 연결에만 답함 → 앱이 그 서버를 빼지 못하고 기다림 → 사이트 5xx | 서버가 디스크 다시 읽기에 묶여 죽지도 일하지도 않음(커널은 TCP 연결은 맺음) → WAS 의 연결 시도가 3초 상한에 끊김 → 포털 5xx |
| 사용자 증상 | 사이트 장애와 5xx 급증(약 13분) | 포털 로그인, 신청, 조회, 검색, 담당자 처리 503, 500(약 12분), 정적 첫 화면만 뜸 |
| 원인 쪽 상태 | ssh, 핑 무응답, 콘솔 OOM | SMS 보고와 ssh 무응답, 핑은 답할 수 있음, 커널 OOM 줄은 기대하지 않음 |
| 탐지된 경로 | dberror.log 'Too many connections', Icinga critical, ganglia bytes_in, wait_io | 웹, WAS 접근 로그 5xx 와 앱 시간 초과 ERROR, 3초 느린 요청 WARN, 공백 직전 db01 메모리와 페이지 읽기, db01 보고 공백 |
| 완화와 복구 | 관리 콘솔로 전원 재투입, mysqld 손 시작, InnoDB 복구 | 66 하이퍼바이저에서 리셋(QMP system_reset), PostgreSQL 은 부팅 때 저절로 떠 복구 재시작(WAL 재생) |

기전 확인: "DB 서버에서 운영 작업이 돈 뒤 서버가 깨끗이 죽지 않고 응답 불능 → 앱이 그 서버를 기다리다 5xx → 사람이 전원(리셋)으로 되살리고 DB 복구" 고리가 같다. 다른 점은 넷이고 적어 둔다. ① **메모리를 쓴 주체는 출처에 없고, 재구성이 마지막 계기(DB 서버 위 운영 작업)를 주 원인으로 골랐다. 트래픽 급증 공동 원인은 뺐다**(원본에서 puppet 이 느려진 것도 mysqld 부하 때문이라고 적었다). ② **원본 콘솔에 보인 것은 OOM 이거나 커널 패닉일 수 있다**("OOM and/or kernel panic"). 재구성은 작업이 파일 캐시를 하한 위로 지켜 OOM 이 오지 않게 한다. 원본도 무엇이든 서버를 되살리지 못했으므로 "제때 끝나지 않아 사람이 전원을 내렸다"는 점이 같다. ③ 계기 꼴: 원본은 자동 운영 도구(puppet), 재구성은 일회성 데이터 작업 하나(둘 다 DB 서버 위 운영 작업). ⑤ 원본은 핑도 끊겼다(IRC 19:11:41 'PING CRITICAL - Packet loss = 100%'), 재구성은 커널이 답하면 핑이 이어질 수 있다. ④ 원본 MySQL 은 PID 파일 때문에 손으로 시작했고, 재구성 PostgreSQL 은 Ubuntu 유닛이 부팅 때 저절로 띄운다.

## 3. 숫자 근거 (2026-11-28, `scenario-stats.py --candidate F83-R`, 정식 + 후보)

- 이번 실행에서 다른 점(공통 지시): 후보는 사용자와 정한 카드라 넓게 뽑기를 하지 않는다. 숫자는 상한 확인용이다.
- 서비스: 설계 GT0 결정 3 (다)와 S 결정 4(T 와 S 한 몫, v4 시작 뒤 생성분만 세어 1:1:1:1)로 민원 5 → 6(F72-P, F80-P, F72-H, F81-R, F82-R 다음). `scenario-stats.py` 는 정답지 domain 으로 civil-petition 을 4, civil-petition-sms 를 2 로 센다(결정 4 의 한 몫과 'v4 시작 뒤' 구간은 모른다). 사용자가 이 두 테스트베드 카드 10장을 정했으므로 서비스 균형으로 막지 않는다.
- 묶음(합계 124, 20% = 24.8): **A 11 → 12(9.7%)**, J 21(16.9%), D 17(13.7%) 등. 주 묶음 A(서버 메모리 고갈). 보조 묶음은 두지 않는다(부품 하나가 멈춘 것이 아니라 서버 전체가 대답하지 못한다).
- 정답 위치: **노드, 디스크 12 → 13(10.5%)**(§2-1 '인프라', 작업이 돈 서버 db01). 결제 경로 14/124(11.3%, 무관).
- 단순 규칙 적중률(`--candidate F83-R`): 판정 **보류**(v4 세트 11건 < 20건, 통과로 본다). 변경 원인 몫 3/10 → 3/11(27.3%)로 하한 30% 아래로 내려가지만 세트가 20건 미만이라 판정하지 않는다(사람이 정할 것 §13). 규칙 1, 2, 6 적중률 30.0% → 27.3%, 정밀 100% 그대로(미끼와 함께 75.0%), 규칙 5 적중률과 정밀 30.0% → 27.3%.
- **F80-P 와 묶음, 정답 위치가 같다**(카드 지시 1): 둘 다 A 이고 정답이 "작업이 돈 DB 서버(노드)"다. 다른 것은 전파(커널 OOM 이 PostgreSQL 만 죽임 대 서버 전체 먹통과 하이퍼바이저 리셋), 관측(§8 표), 도메인(S 대 T), 원본 사례다. 민원 두 테스트베드에서 'DB 서버 메모리' 가 원인인 것이 둘이 되어 관제 AI 가 "민원 DB 서버 메모리가 오르면 원인" 이라는 지름길을 배울 수 있다. 사람이 정할 것으로 올린다(§13).
- ID: 새 사례군 F83 의 R. F80(서버 위 작업이 메모리를 다 씀)의 R, P 는 이미 쓰였고, 원본과 전파가 달라 F80 의 다른 관계 접미사로 두지 않았다.
- 왜 이 후보인가: 사용자와 정한 카드. 서버 완전 다운을 입구(web01)가 아니라 서버(db01)로 하자는 사용자 결정(11-26)이고, 겉 증상(포털 전면 5xx, WAS 3초 시간 초과)이 F72-H(라우터 링크 끊김)와 같아 관제 AI 가 db01 의 공백 직전 메모리, 재부팅 흔적, 라우터 상태를 읽어야만 가른다.

## 4. 인과 사슬 (코드와 인프라 위치)

### 4-1. 사슬

1. 계기(db01): 실행기(opsjob, 상시 관리 연결 192.168.127.41)가 66 도우미에 VM 205 되살림 가드를 먼저 걸고(`vm-hold`), db01 가드(유닛 멈춤)를 건 뒤 이름 붙은 일시 유닛 `petition-export`("Yearly petition statistics export")로 root python3 작업을 띄운다(`scripts/scenarios/profiles/vmhost_job_executor.py` CONTRACTS["F83-R"], `memload` 모드). systemd 가 "Started petition-export.service - Yearly petition statistics export." 를 남기고 rsyslog 가 119 로 보낸다(실행기 계정 줄만 첫 규칙이 거른다, 설계 §6-2).
2. 작업: 제 쪽을 잠그고(mlockall, root) 파일 캐시(/proc/meminfo 의 Active(file) + Inactive(file))가 64 MiB 를 넘는 동안 16 MiB 씩 24 MiB/s 로 늘리고, 32 MiB 아래면 16 MiB 를 돌려주며, 1500초 뒤 스스로 끝난다(유닛 RuntimeMaxSec 1560초). 잠금은 작업 자신이 thrashing 에 멈추지 않고 띠를 지키게 하려는 것이다(다른 프로세스가 커지면 그만큼 돌려줘 OOM 을 막는다, §9-6 모의).
3. 메모리: db01 MemTotal 7,941 MiB, 스왑 0(`sms.virtual_memory.swap_total_size` 0), 평시 사용률 0.97~1.84%, cached 7.08~7.33GB(공유 버퍼 약 2.1GB 포함), buffers 약 0.6GB, 가용 1,334,378~1,389,392 쪽(약 5.1~5.3 GiB). 작업은 파일 캐시 약 5.5GB 를 거의 다 가져가 약 5.5 GiB 를 쥔다(사용률 약 70%, F80-P 의 6 GiB 작업이 73.7% 였다).
4. 커널: 스왑이 없으면 익명 쪽(작업, 공유 버퍼)은 회수 대상이 아니다. 파일 쪽 32~64 MiB 는 모든 프로세스의 코드와 라이브러리, journald 와 rsyslog 가 쓰는 로그, PostgreSQL 의 공유 버퍼 밖 데이터(민원 표만 heap 1.6GB, 색인 포함 1.9GB, `templates/db/pg-table-stats` 머리 주석)와 WAL 쓰기가 다투는 자리다. 내쫓은 쪽을 다시 읽으면 그것도 회수 진전이라 커널의 메모리 할당 길은 OOM 을 부르지 않는다(§9-6 모의: 파일 쪽 22 MiB 에서도 150초 동안 OOM 없음, 다 써야 1초 안에 OOM).
5. 서버 먹통: 디스크 다시 읽기가 IO 대기열을 채워(평시 db01 디스크 읽기 시간 최대 17ms, sdb 읽기 최대 594/s) 프로세스마다 코드 한 쪽을 실행할 때마다 디스크를 기다린다. SMS 에이전트(lucida-agent)가 보고를 못 보내 60초 표본이 끊기고, sshd 가 배너를 못 보내며, postmaster 가 새 연결을 받아 인증하는 데 수 초 넘게 걸린다. 커널의 TCP 처리와 ICMP 응답은 사용자 공간을 거치지 않아 이어질 수 있다(원본 'still responding to at least some mysqld tcp connections').
6. 앱 쪽: JDBC 주소 db01 업무 주소 192.168.126.11:5432(`civil-petition/deploy/render.py:450`). 풀 `civil-petition/deploy/templates/was/petition-context.xml:9-15`(minIdle 5, maxActive 40, maxWait 3초, testOnBorrow, validationQueryTimeout 3초, PgJDBC connectTimeout 3초와 loginTimeout 3초, socketTimeout 없음). PgJDBC 42.3.3(§9-2)에서 새 연결은 3초에 'Connection attempt timed out.' 이나 'Connect timed out', 풀이 차면 maxWait 3초 뒤 'Unable to fetch a connection in 3 seconds'. 읽기 경로 `ApiExceptionHandler.java:23-26`(DataAccessException → ERROR 'database access failed' 와 503), 신청과 담당자 처리 `PetitionRepository.java:29, 79`(@Transactional, 500), 3초 넘는 요청 `SlowRequestFilter.java:17-29`(WARN 'slow request ... took_ms=').
7. 웹: web01 Apache 균형기가 WAS 가 3초 남짓에 돌려준 503, 500 을 그대로 돌려준다(ProxyPass timeout 30, `templates/web/petition-site.conf:16`). 응답 상태로 WAS 를 빼지 않는다(F72-P 시트 §4 와 같은 설정).
8. 회복(사람): 66 하이퍼바이저에서 VM 205 리셋(QMP system_reset, Proxmox 의 Reset 과 같음, qm 을 거치지 않아 Proxmox 작업 줄 없음). db01 이 부팅하고 postgresql@16-main 이 저절로 뜬다(`civil-petition/deploy/steps/db.sh:32` restart_enabled). 갑작스런 종료라 PostgreSQL 은 'database system was interrupted ... automatic recovery in progress' 로 WAL 을 재생한다(LOG 수준이라 로그 감시에는 안 간다). 복구 동안 연결 시도는 'FATAL: the database system is starting up'(감시 fatal 패턴에 맞음, 에이전트가 그때 이미 tail 중이면 119 에 닿음, §9-5).

### 4-2. 먹통을 정말 만들 수 있는가 (카드 지시 2)

| 따질 것 | 사실 | 근거 |
|---|---|---|
| 메모리, 스왑 | MemTotal 7,941 MiB, 스왑 0 | 인벤토리 `t.json` db01 memory_mb 8192, VM `sms.memory.total_size` 8,326,946,816, `sms.virtual_memory.swap_total_size` 0(11-28 조회) |
| systemd-oomd, earlyoom | 없음 | 119 `process_snapshot` db01 전체 목록(11-28 02:15 KST)에 그런 프로세스 없음. psimon 커널 스레드는 있어 PSI 는 켜져 있다(누가 거는지는 119 로 알 수 없다) |
| vm.overcommit | 저장소 sysctl 은 ip_forward 하나만 바꾼다(`templates/common/sysctl-server.conf`). Ubuntu 기본(0)으로 본다. 작업이 쪽을 실제로 만지므로(0 채움, 잠금) overcommit 값과 무관 | 저장소 |
| OOM killer | 켜져 있다(F80-P 가 같은 꼴 S 서버에서 커널 OOM 을 냈다, 11-27 21:04 KST 실행: 메모리 사용률 1.4% → 73.7%, ICMP 1, 보고 이어짐) | 119 VM `sms.memory.utilization{host_name="192.168.131.31"}` |
| 커널 | Ubuntu 24.04(apt snapshot 20261117), 정확한 판은 119 에 없다. 같은 66 의 쿠버네티스 VM 부팅 줄이 6.8.0-149-generic(11-20). Ubuntu 커널은 MGLRU 를 켜 둔다(104 의 Ubuntu 6.5 HWE `CONFIG_LRU_GEN_ENABLED=y`, lru_gen enabled 0x0007, min_ttl_ms 0) | 119 `syslog_local`, 104 /boot/config |
| 스왑 없이 먹통이 되는가 | 된다. 104 실제 커널(6.5, MGLRU 켬)의 스왑 없는 cgroup 모의(§9-6)에서 파일 쪽을 작업 집합 아래로 묶어 두면 OOM 없이 다시 읽기만 끝없이 이어졌다. 104 디스크는 빨라 다시 읽기 한 번이 약 0.03ms 라 '느려짐' 으로 끝나지만, 66 디스크는 db01 읽기 시간이 ms 단위(평시 최대 17ms)라 같은 다시 읽기 수가 수백 배 긴 멈춤이 된다 | §9-6 |
| 66 위 실제 선례 | F80-R 실행 F80-R-run-bad63a76(2026-10-17, 66 의 tb-w2, 7.76 GiB 스왑 없음): 백업 작업이 메모리를 천천히 채우자 load 227, SSH 배너 교환 시간 초과, 노드 NotReady(+137초), 커널 OOM 은 9분 뒤(09:42:26 UTC)에야 java 하나를 죽였고 작업이 RuntimeMaxSec 로 끝난 뒤에야 돌아왔다 | `~/dev/testbed-ops/operator/log.md` 2026-10-17T09:52:25Z 줄 |
| 빨리 잡으면 먹통이 아니다 | F80-P(같은 8GB, 스왑 없음, shared_buffers 2GB S 서버)는 작업이 몇 초 안에 6 GiB 를 잡아 OOM 이 바로 PostgreSQL 을 죽였고 보고가 끊기지 않았다(위). 그래서 F83-R 은 천천히(24 MiB/s) 채우고 파일 캐시 하한을 지킨다 | 위 |

## 5. 원인 규정 (원칙 5, 6)

- 근본(`root_cause.target_id`): db01 SMS 서버 대상 번호 `73343434-0302-468f-b738-643aec7ab84b`(target_kind `node`, `civil-petition/deploy/inventory/t.targets.json`). 결함은 서버 메모리를 거의 다 쓴 작업이고, 고쳐야(멈추거나, 상한을 두거나, 다른 서버로 옮기거나) 재발이 막힌다.
- 계기(`trigger_target_id`): 비움. 계기도 같은 서버 위 작업이다.
- 원칙 6: PostgreSQL 은 디스크 읽기에 묶인 피해자이고, WAS 와 웹 서버는 오류를 크게 남기는 피해자다. 원본도 DB 엔진이 아니라 같은 서버의 작업과 부하를 계기로 적었다.
- 채점: 입도 `node`, accept `db01`, 서버 대상 번호, db01 메모리 대상 `1acbd536-3e3c-4425-a9cb-a8fbd91c0141`(F80-P 와 같은 범위, 2회차 평가 권고 4. 기타 메모리 대상 `ebe3b100-...` 은 더 받을 이유가 없어 뺐다). partial: PostgreSQL 로그 감시 대상 `17a5d386-4f83-42c2-9e05-e7eecbafeb74`(DB 자체를 원인으로 본 답), was01, was02, web01 의 이름과 대상 번호(증상 서버만 본 답). 라우터와 66 은 넣지 않는다(F72-H 와 가르는 핵심이 '라우터가 아님' 이다).
- 원칙 5: 정답 "db01 에서 돈 작업이 메모리를 거의 다 써서 서버가 응답 불능이 됐다" 는 관제 데이터(작업 유닛 시작 줄, 공백 직전 메모리와 페이지 읽기 표본, 프로세스 표본의 python3, 보고 공백, 재부팅 흔적, WAS 시간 초과 줄)로 낼 수 있다. 왜 OOM 이 안 왔는지(회수 진전), 작업이 메모리를 어떻게 정했는지, 누가 리셋했는지는 119 에 없어 `beyond_data` 로 옮겼다.

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 | 기대(합격 조건 아님) |
|---|---|---|
| 감지(lucida-next) | 이 도메인에서 이벤트가 되는 것은 지표 이상(stream-anomaly)뿐이다(공통 지시의 119 사실). 후보: db01 메모리 사용률(롤업, 평시 약 1.3% → 약 70%), cached 와 가용 페이지 급감, 페이지 읽기 급증, 부하 평균. 켜진 알람 정책 '메모리 사용률' 의 문턱을 넘는지는 정책 값에 달림 | 이벤트, 이벤트 클러스터, 인시던트는 기대로만 적는다. SMS 보고 공백, 가용성 UNKNOWN, 재부팅, 로그 감시 ERROR 줄은 이벤트가 되지 않는다(§11) |
| 피해 판정(러너) | 상주 부하(loadgen VM, domain civil-petition) 요약의 접수번호 조회 비정상 비율 ≥ 0.5, min_hold 14분 뒤 3틱(§9-3) | 평시 약 0.01, 먹통 중 약 1.0 |
| RCA(합격 기준) | §7 의 SRE 길 | 각 단계 평시 데이터가 119 에 있음을 조회로 확인(§7) |

## 7. SRE 가 데이터로 원인을 찾는 길 (합격 기준)

알람과 AI 설정과 상관없이 119 에 쌓인 데이터만으로 간다. 장애 때 모양은 모두 "시험 실행에서 확인"(흔적 표 실측 전)이다. 평시 값은 2026-11-28 01:30~02:40 KST 조회(로그 감시는 날짜별 7일, 지표 24시간, 표본 1시간). 11-25, 11-26 은 T4 시험 실행이 있어 로그 감시 평시는 11-27 하루로 적는다.

| 단계 | 질문 | 어느 표, 칸, 대상 번호 | 평시 값(119 실조회) | 장애 때 모양 |
|---|---|---|---|---|
| 1 | 사용자가 실패하나 | CH `lucida.lucida_logs_local`(`event_source='log_monitor'`), web01 접근 로그 감시 `d510390b-d2d3-4da4-9f1b-64af8cd16bdd`(패턴 `" 5[0-9][0-9] `), Tomcat 접근 로그 `69ebf84f-81c2-42f0-b45d-a49f93804e1f`(was01), `63de5bcc-f7cf-478b-ba1e-d9f044a70730`(was02) | 11-27 하루 세 감시 모두 0줄(11-25, 11-26 T4 시험 날만 수천 줄) | 시험 실행에서 확인. 기대: /petition/api 요청마다 503, 500(초당 5~30) |
| 2 | 어디서 실패하나 | was01 앱 로그 `5aad9fbb-ec04-44b0-ba02-f81ced10af73`, was02 앱 로그 `e466fa3e-c26a-41a6-9c04-90e840b6df21` | 11-27 하루 ERROR 0줄, WARN 은 was02 2줄(밤 배치 느린 요청). T4 시험 날에는 'database access failed: java.net.SocketTimeoutException: Connect timed out' 1,418줄과 Caused by 줄 1,499줄로 같은 감시 통로가 이 꼴의 줄을 실어 나름을 확인. 느린 요청 took_ms 는 11-25, 11-26 에 10초 근처가 대부분이라 3초 설정은 아직 적용 전 | 시험 실행에서 확인. 기대: 두 WAS 같은 때 'database access failed' 와 'Connection attempt timed out.', 'Connect timed out', 'Unable to fetch a connection in 3 seconds' 가운데 하나 이상, 'slow request ... took_ms=' 약 3000 |
| 3 | DB 서버가 죽었나, 길이 끊겼나 | VM `sms.cpu.utilization`(db01 `60579e7c-ac4f-41a0-a479-3cf25654fa28`) 표본 수, `sms.server.icmp_status`, `sms.server.rtt`(db01 `73343434-0302-468f-b738-643aec7ab84b`); CH `lucida.process_snapshot`(host_name 192.168.126.11); PG `target_availability_history`(db01); VM `nms.interface.if_oper_status`, `if_in_octets`(rt-core01 ens21 `79a3ac72-fde1-400a-89ca-bcbf414a4b92`) | CPU 표본 1시간 60개(1분마다), 프로세스 표본 1시간 60회(약 140행씩), ICMP 24시간 내내 1(RTT 4.14~16.85ms, 중앙 5.0), db01 가용성 11-19 06:15 UTC 부터 UP(그 앞은 에이전트 재시작 때 UNKNOWN 27초), ens21 if_oper_status 24시간 최소 1, if_in_octets 3시간 중앙 약 103,295 | 시험 실행에서 확인. 기대: db01 의 SMS 보고와 프로세스 표본이 몇 분 끊긴다. ICMP 는 커널이 답하면 1 이 이어질 수 있다(F72-H 는 0). 가용성은 UNKNOWN 이 될 수 있다(에이전트 공백). 라우터 ens21 은 올라간 채, 트래픽은 WAS 의 연결 시도로 이어짐 |
| 4 | 끊기기 직전 db01 에 무슨 일이 | VM `sms.memory.utilization`, `sms.memory.cached`, `sms.memory.used`(db01 메모리 `1acbd536-3e3c-4425-a9cb-a8fbd91c0141`), `sms.other_memory.available_pages`, `page_in_bytes`, `page_faults`, `page_scans`(`ebe3b100-cde7-4382-ae0b-725ba4c46d08`), `sms.cpu.load_average_1m`, `wait_utilization`, `sms.cpu.waiting_processes`; CH `lucida.process_snapshot`(name, user, mem_rss) | 24시간: 사용률 0.97~1.84%(중앙 1.27), cached 7.08~7.33GB, used 81~153MB, 가용 쪽 1,334,378~1,389,392, page_in_bytes 중앙 0 최대 약 4.8 MB/s(밤 백업), page_faults 중앙 62 최대 4,840/s, page_scans 중앙 0 최대 1,690, 부하 평균 1분 중앙 0.01 최대 0.61, CPU 대기 최대 0.9%, 대기 프로세스 최대 5. python3 프로세스 7일 0(root 의 unattended-upgr 만, RSS 22 MiB), postgres 는 스냅숏마다 약 16개(백엔드 pid 는 풀 연결이 바뀔 때 바뀜, 7일 고유 pid 345개) | 시험 실행에서 확인. 기대: 약 4분 동안 사용률이 1분마다 약 18%p 씩 올라 약 70%, cached 와 가용 쪽 급감, 마지막 표본들에서 페이지 읽기, 페이지 폴트, 부하 평균이 녹화 앞 구간(pre_window)의 같은 계열 최댓값을 넘음(평시 24시간 최댓값은 날마다 흔들린다: 11-28 첫 조회 page_in 약 4.8 MB/s, 부하 0.61, 재조회 6.16 MB/s, 0.36). 프로세스 표본에 root python3(RSS 가 표본마다 약 1.4 GiB 씩 자람) |
| 5 | 무엇이 시작됐나(계기) | CH `lucida.syslog_local`(hostname db01, app_name systemd, 'Started ...') | 7일 'Started' 줄: fwupd 163, sysop 세션과 user@ 각 40여(평시 운영자 세션), launchpadlib 타이머 43, T4 시험 날 유닛 셋(reindexjob 2, nightlyarchive 1, auditreport 1). 'petition-export' 0 | 시험 실행에서 확인. 기대: 'Started petition-export.service - Yearly petition statistics export.' 한 줄이 메모리 경사 시작과 같은 분 |
| 6 | 어떻게 되살렸나 | VM `sms.server.last_boot_time`, `sms.server.master_agent_uptime`(db01); CH `lucida.syslog_local`(hostname db01, 커널 'Linux version' 과 시작 유닛 줄); CH `lucida.process_snapshot`(postgres pid); PostgreSQL 로그 감시 `17a5d386-4f83-42c2-9e05-e7eecbafeb74` | last_boot_time 2026-11-17 16:35:58 UTC 그대로, 에이전트 시작 11-19 06:14:19 UTC. db01 syslog 하루 약 1,900줄, 7일 부팅 줄 0, 커널 OOM 줄 0. 66 의 쿠버네티스 VM 재부팅(11-20 14:47 UTC)에서 부팅 줄 약 1,000줄이 'Linux version 6.8.0-149-generic' 부터 119 에 들어온 것을 실측. PostgreSQL 로그 감시 FATAL 은 11-25 PG 재시작 때 20줄('terminating connection due to administrator command', 'the database system is shutting down')뿐, 11-27 0 | 시험 실행에서 확인. 기대: last_boot_time 이 리셋 시각으로, 부팅 줄이 종료 줄('Stopping', 'Reached target Shutdown') 없이, 늘 있던 postmaster 를 포함해 모든 postgres pid 바뀜. 'the database system is starting up' FATAL 은 에이전트가 PostgreSQL 복구보다 먼저 tail 을 시작했을 때만 남는다(로그 감시는 파일 끝부터 읽음) |

조회 근거(모두 분할 칸과 시각 범위를 건 읽기 전용, 104 의 readonly=2 사본 `q.sh`, PG 는 `begin read only`):

```
-- 단계 1, 2, 6: T 로그 감시 줄 수(날짜별 7일)
SELECT toDate(timestamp,'Asia/Seoul') d, target_id, severity_text, count() FROM lucida.lucida_logs_local
WHERE timestamp_date >= today()-7 AND timestamp > now() - INTERVAL 7 DAY AND event_source='log_monitor'
  AND host_name IN ('192.168.124.11','192.168.125.11','192.168.125.12','192.168.126.11') GROUP BY 1,2,3
-- 단계 2: 시간 초과 줄 꼴, 느린 요청 시간 분포
SELECT severity_text, replaceRegexpAll(substring(body,1,200),'[0-9]+','N') b, count() FROM lucida.lucida_logs_local
WHERE timestamp_date >= today()-7 AND timestamp > now() - INTERVAL 7 DAY AND event_source='log_monitor'
  AND target_id IN ('5aad9fbb-...','e466fa3e-...') AND body LIKE '%timed out%' GROUP BY 1,2
SELECT toDate(timestamp,'Asia/Seoul'), host_name, intDiv(toUInt32OrZero(extract(body,'took_ms=([0-9]+)')),1000), count()
FROM lucida.lucida_logs_local WHERE timestamp_date >= today()-4 AND timestamp > now() - INTERVAL 4 DAY
  AND event_source='log_monitor' AND target_id IN (...) AND body LIKE '%slow request%' GROUP BY 1,2,3
-- 단계 3, 4: 표본
SELECT name, user, uniqExact(pid), uniqExact(ts), round(max(mem_rss)/1048576) FROM lucida.process_snapshot
WHERE ts > now() - INTERVAL 7 DAY AND host_name='192.168.126.11' AND (name LIKE 'python%' OR name LIKE 'unattended%') GROUP BY 1,2
-- 단계 5, 6: syslog
SELECT replaceRegexpAll(substring(message,1,140),'[0-9]+','N') m, count() FROM lucida.syslog_local
WHERE received_at > now() - INTERVAL 7 DAY AND hostname='db01' AND app_name='systemd' AND message LIKE 'Started %' GROUP BY m
SELECT toDate(received_at,'Asia/Seoul'), count(), countIf(message LIKE '%Linux version%'), countIf(message LIKE '%Out of memory%' OR message LIKE '%oom-kill%')
FROM lucida.syslog_local WHERE received_at > now() - INTERVAL 7 DAY AND hostname='db01' GROUP BY 1
SELECT received_at, app_name, substring(message,1,120) FROM lucida.syslog_local
WHERE received_at > toDateTime64('2026-11-20 14:46:40',3) AND received_at < toDateTime64('2026-11-20 14:48:30',3) AND source_ip='192.168.213.143'
-- 단계 3, 6: 가용성(PG)
select target_id, status, started_at from target_availability_history where started_at > now() - interval '14 days'
  and target_id in ('73343434-...','bda0dfa8-...','ebdf15e2-...','c5f7161d-...')
-- VictoriaMetrics: min_over_time / max_over_time / quantile_over_time(0.5, <지표>{host_name="192.168.126.11"}[24h]),
--   count_over_time(sms.cpu.utilization{host_name="192.168.126.11"}[1h]), sms.server.last_boot_time, 원시 표본 간격 [5m]
```

표본 데이터 여부: 1, 2, 5 단계는 전수 원천(로그 감시 줄, syslog), 3, 4 단계는 1분 SMS 표본과 60초 프로세스 표본의 전체 목록, 6 단계는 SMS 표본과 syslog 다. 트레이스, APM, DPM 은 이 도메인에 없다.

공백이 증거인 까닭: 에이전트는 60초마다 보내므로 마지막 표본은 먹통 시작 60초 안의 것이다. 경사는 약 4분이라 약 4개의 오르는 표본이 공백 앞에 남는다(F80-P 실행에서 몇 초짜리 경사도 1분 표본 하나로 잡혔다). 먹통 동안 에이전트가 쌓아 둔 것은 리셋으로 사라지므로(메모리 안), 공백은 "서버가 보고하지 못했다" 는 사실 그대로 남는다. 다만 thrashing 이 얕아 에이전트가 느리게라도 보내면 표본이 드문드문 이어질 수 있다(첫 실행 확인).

## 8. 관측 근거 요약과 감별

### 8-1. F80-P 와 가르기 (카드 지시 1)

| 관측 | F80-P(S dbsvr01, 커널 OOM 이 PostgreSQL 을 죽임) | F83-R(T db01, thrashing 먹통, 하이퍼바이저 리셋) |
|---|---|---|
| DB 서버 SMS 보고, 프로세스 표본 | 이어짐(11-27 21:04 실행 실측: 1분 표본 빠짐 없음) | 몇 분 끊김 |
| 메모리 사용률 | 표본 하나 사이에 1.4% → 73.7%(몇 초) | 약 4분 동안 1분마다 오르는 경사 뒤 공백 |
| ICMP | 1 | 1 이 이어질 수 있음(커널 응답), 첫 실행 확인 |
| 커널 OOM 줄(syslog 감시 FATAL) | 있음, PostgreSQL 이 희생 | 기대하지 않음(파일 캐시 하한) |
| postgres 프로세스 | 사라져 cleanup 까지 없음 | 공백 뒤 늘 있던 postmaster 를 포함해 모든 pid 가 바뀌어 다시 있음(재부팅) |
| WAS 오류 | 'Connection ... refused', 몇 ms | 3초 시간 초과, 느린 요청 WARN |
| 재부팅 흔적 | 없음 | last_boot_time 바뀜, 종료 줄 없는 부팅 줄 |
| 회복 | 작업 멈춤과 PostgreSQL 시작 | 하이퍼바이저 리셋 |

### 8-2. 정답지 감별

- must_support(정답지 6개): 작업 유닛 시작 줄과 root python3, 메모리 경사와 cached, 가용 쪽 감소, 공백 직전 페이지 읽기와 부하 평균, db01 보고와 표본 공백(라우터 포트 그대로), 두 WAS 3초 시간 초과 ERROR 와 느린 요청 WARN, 접근 로그 5xx, 재부팅 흔적(last_boot_time, 종료 줄 없는 부팅 줄, 늘 있던 postmaster 를 포함해 모든 postgres pid 바뀜).
- must_rule_out(정답지 5개): PostgreSQL 만 죽은 OOM(서버 보고까지 끊기고 재부팅, 3초 시간 초과), 망 경로 장애(라우터 포트, trap, 설정 명령 없음, 끊기기 직전 메모리 상승), PostgreSQL 자신의 질의, 잠금, 메모리(작업 유닛 시작 줄, postgres RSS 평시 꼴), WAS 배포나 설정 변경(java pid 그대로, 두 대 동시), 계획된 재시작(종료 줄 없음, 실패가 부팅보다 몇 분 앞).
- contrast_with: F80-P(§8-1), F72-H(같은 'db01 이 관제에서 사라지고 WAS 3초 시간 초과', 라우터 포트 down 과 trap, ICMP 0, 재부팅 없음), F80-R(쿠버네티스 워커의 백업 작업이 노드를 헛돌게 함).
- 절대 문장 피하기(공통 일반 규칙 ③): '운영자 줄이 없다' 대신 '라우터에 설정 변경 명령(nft -f, tc)이 없다', '핑에 답한다' 는 '이어질 수 있다' 로만 썼다. 평시 운영자 세션(sysop, 30~90분마다)은 db01 에도 로그인하므로 '로그인이 없다' 는 쓰지 않았다.
- 미끼와의 관계: U7 미끼(운영자 경로의 WAR 재배포, 웹 설정 재적용)는 WAS 와 웹에서만 나고 db01 의 메모리와 보고를 바꾸지 않는다.
- 단순 규칙 쪽 메모: 변경 원인이 아니다. 'DB 서버 메모리 급등 = 원인' 이 F80-P 와 함께 지름길이 되는지 사람이 정한다(§13).

## 9. 러너 판정과 강도, 피해 계산 (원칙 9)

### 9-1. 피해가 나는 이유: 서버가 멈추고, 앱 상한 3초에 연결 시도가 끝난다

- 평시(근거 `~/dev/testbed-ops/v4/agent-out/APT2-facts/`): 평균 약 28 rps(한가할 때 5, 바쁠 때 30), 피크 p95 약 8ms, db01 CPU 약 1%.
- 부하기의 모든 요청은 `/petition/api/...` 이고 모든 경로가 DB 를 쓴다. 피해는 "CPU 를 나눠 써서 몇 ms 느려짐"(앞선 막힌 카드의 함정)이 아니라 **DB 서버의 프로세스가 디스크 다시 읽기에 묶여 대답하지 못함**에서 난다. PostgreSQL 이 새 연결의 시작 패킷에 3초 안에 답하지 못하면 PgJDBC 가 끊어(§9-2) 정적 첫 화면을 뺀 요청이 거의 100% 실패한다.
- 지속: 경사 약 4분(파일 캐시 약 5.5GB 를 24 MiB/s 로, 약 235초) 뒤 먹통, min_hold 14분(주입부터 셈)과 성공 3틱(45초)으로 리셋까지 약 10.7분, 부팅과 PostgreSQL 복구 약 1~1.5분. 사용자 피해 약 12분. 원본은 약 13분.
- 문턱: 설계 §6-3 피해 판정 "사용자 비정상 비율 5% 이상 2분 지속" 에 대해 약 100% 가 약 12분. 감시 패턴에 걸리는 피해 줄은 웹 접근 로그 5xx, Tomcat 접근 로그 5xx, 앱 ERROR 와 `Exception` 줄, 3초 느린 요청 WARN 이다. 실패 요청마다 약 6줄이라 약 12분에 저점 약 2만, 피크 약 13만 줄(F72-H, F80-P 와 같은 양).
- 한가한 시간에도 피해가 난다: 먹통은 부하와 상관없이 작업이 만든다(서버 쪽 다시 읽기는 에이전트, sshd, journald, PostgreSQL 백그라운드 프로세스가 늘 일으킨다). 다만 먹통의 깊이는 PostgreSQL 이 하는 일의 양에 따라 달라 바쁜 시간이 확실하다. 녹화 시각은 KST 09:00~21:00(밤 보관 배치 02:20~05:00, 논리 백업 03:30 회피).

### 9-2. 3초 상한이 막는가 (카드 지시 3, PgJDBC 42.3.3 소스)

Maven Central `postgresql-42.3.3-sources.jar` 를 읽었다(WAS 의 `libpostgresql-jdbc-java`, `steps/os-base.sh:7`).

| 경로 | 소스 | 먹통 db01 에서 |
|---|---|---|
| 새 연결 | `org/postgresql/Driver.java` 249~266: loginTimeout 이 있으면 `ConnectThread` 를 띄우고 `ct.getResult(timeout)`. 342~375: 기한이 지나면 `abandoned = true` 후 `PSQLException("Connection attempt timed out.")` | 커널이 SYN-ACK 를 돌려줘 TCP 는 맺어져도 PostgreSQL 이 시작 패킷에 답하지 못하면 **요청 스레드는 3초에 풀려난다**. 503(신청과 처리는 500) |
| 연결 맺기 | `core/v3/ConnectionFactoryImpl.java` 99~115: `new PGStream(socketFactory, hostSpec, connectTimeout)`, socketTimeout 은 0 보다 클 때만 건다 | 듣기 대기열(PostgreSQL 16 은 MaxConnections x 2)이 차 커널이 SYN 을 버리면 connectTimeout 3초에 'Connect timed out'. 둘 다 3초라 어느 문구든 날 수 있다(공통 일반 규칙 ②) |
| 버려진 연결 시도 | 같은 파일: socketTimeout 이 없어 백그라운드 스레드의 시작 응답 읽기에 상한이 없다. `Driver.java` 316~327: 나중에 성공하면 닫는다 | 먹통 동안은 백그라운드 스레드와 소켓이 남는다. 리셋 뒤에는 db01 이 그 연결을 모르므로 끝내 답이 오지 않는다(tcpKeepAlive 기본 false, 같은 파일 121~123). 그래서 cleanup 이 두 WAS 의 db01:5432 연결을 끊는다(§9-4) |
| 열린 풀 연결 | 질의 읽기에 상한이 없다(socketTimeout 없음). validationQueryTimeout 은 `PgStatement.setQueryTimeout` 의 타이머가 **따로 맺는 연결로** 취소 요청을 보낼 뿐(`core/QueryExecutorBase.java` 181~183, cancelSignalTimeout) 원래 소켓의 읽기를 끊지 않는다 | 먹통 순간 WAS 마다 쉬던 풀 연결(평시 minIdle 5)을 빌린 요청은 붙잡혀 부하기 10초 시간 초과(상태 0)가 된다. 리셋 뒤에도 풀리지 않아 cleanup 이 끊는다 |
| 풀 빌리기 | Tomcat JDBC: 쉬는 연결이 없고 크기가 40 아래면 새 연결(위 3초), 40 이면 maxWait 3초 | 붙잡힌 5개를 뺀 35 자리를 3초짜리 연결 시도가 채우면(WAS 당 초당 약 14 요청 x 3초 = 약 42) 나머지는 'Unable to fetch a connection in 3 seconds' 로 3초 뒤 503 |

러너 필수 중단(entry_status 0, 2틱 연속): §9-3 모의.

### 9-3. 판정, 강도, 중단 모의

- 강도: 하나로 고정(`approved-fixed-f83-r`): mode memload, unit petition-export, file_floor_mib 32, file_ceiling_mib 48, ramp_mib_s 24, runtime_seconds 1500, vmid 205. 사다리가 없다: 작업이 파일 캐시를 하한 근처로 묶으면 먹통이 서버 작업 집합(코드, 로그, WAL, 공유 버퍼 밖 데이터)에 따라 깊어지고, 하한이 OOM 을 막는다. 부족하면 검증 하네스가 하한을 낮추는 보강을 낸다. 실행기는 하한을 8 MiB 까지 받으므로(1회차 평가 권고 4, 시험 포함) 보강은 계약(profiles.json, controllers.json, 실행기 CONTRACTS)만 바꾸면 된다. 하한 근거: db01 의 postgres 밖 프로세스 RSS 는 multipathd 26, journald 16, lucida-agent 15 MiB 수준이라 뜨거운 파일 쪽이 작을 수 있다(그러면 32~64 MiB 띠에서는 먹통이 얕아 PostgreSQL 이 3초 안에 답할 수 있다). 104 모의는 22 MiB 까지 OOM 이 없었다.
- success: 상주 부하(domain civil-petition) 접수번호 조회 비정상 비율 ≥ 0.5, 3틱(45초). min_hold 14m(주입부터), settle 60s, level timeout 17m, max_injection_duration 20m(가드 기한 30분). 신청 단계 비율은 쓰지 않는다(F72-P 평가 지적: 장애 중 로그인 상태가 풀려 신청이 사라진다).
- must_rule_out: achieved_rps < 2.0(부하가 끊겼을 때만).
- abort: entry_status = 0, 2틱(compile-plan 필수).
- recovery: target_health 200, entry_status 200, 조회 비정상 비율 < 0.1, 2틱, 10분.
- cleanup timeout 15m(executor_common cleanup 상한 900초, 디스패처 960초): 리셋 몇 초 + WAS 연결 끊기 최대 80초 + db01 복귀 기다림 최대 420초 + 서비스 기다림 최대 240초.
- preflight: coordinator-clean, clean-window, baseline-traffic, target-health, vm-entry-health. 실행기 preflight: python3, 유닛이 돌지 않음, 스왑 0, 파일 캐시 > 1,072 MiB, postgresql@16-main 떠 있음, `/sys/kernel/mm/lru_gen/min_ttl_ms` 가 0 이거나 없음(0 이 아니면 MGLRU 가 thrashing 대신 OOM 을 불러 F80-P 꼴이 되므로 거절, 1회차 평가 권고 5), min_free_kbytes 를 preflight 출력에 남김, 66 도우미가 VM 205 의 QMP 소켓 있음과 붙잡혀 있지 않음.

중단 모의(`/home/sjbang/tmp/claude-1008/-home-sjbang-dev-lucida-next/ee261603-595a-4ca8-a253-3a4bd5d2bb18/scratchpad/c11/abort/c11_abort_sim.py`, F72-H 시트 §9-3 모의를 고침): k6 도착률 고정, VU 40~150, 여정 비중표 그대로, 세션 고정. 넘어가는 구간 TR 초 동안 대답 비율이 1 에서 0 으로, 대답 지연이 0.5초에서 최대 12초로 커지고(10초 넘으면 상태 0), 먹통 시작에 WAS 마다 쉬던 풀 연결 K 개가 붙잡히며(10초 시간 초과), 그 뒤 연결 시도는 3.0~3.3초에 503, 리셋 뒤 90초는 거부로 즉시 503. 400회씩:

| TR(초) | K | 5 rps | 10 rps | 19 rps | 30 rps |
|---|---|---|---|---|---|
| 30 | 5 | 0 | 0 | 0 | 0 |
| 30 | 10 | 0 | 0 | 0 | 0 |
| 120 | 5 | 0 | 0 | 0 | 0 |
| 120 | 10 | 2(0.5%) | 0 | 0 | 0 |
| 300 | 5 | 2(0.5%) | 0 | 0 | 0 |
| 300 | 10 | 6(1.5%) | 1(0.25%) | 0 | 2(0.5%) |

(표 값은 2틱 연속 0 이 난 횟수.) 0 틱 하나라도 나는 비율은 2.5~21% 이지만 다음 틱의 500 이 덮는다. 조회는 장애 중 30초 창마다 저점에서도 18건 넘게 실려 성공 조건이 늘 선다. 모의는 평시 데이터의 튐(1분 몰림)을 넣지 않아 중단 확률은 모의보다 클 수 있다(공통 일반 규칙 ⑦). 그래서 녹화는 상주 부하가 있는 KST 09:00~21:00 로 둔다(저점 5 rps 근처의 새벽을 피함).

### 9-4. 되살리기 길 (카드 지시 4)

- 서버가 먹통이면 실행기의 정리(ssh)가 닿지 않는다(F80-R 실행에서 cleanup 이 'Connection timed out during banner exchange' 로 실패했다). 그래서 cleanup 은 서버를 거치지 않는다:
  1. 66 도우미 `vm-reset 205`: 가드를 미루고, VM 이 붙잡혀 있으면 QMP `system_reset`(F72-H 의 `set_link` 와 같은 통로, qm 을 거치지 않아 Proxmox 작업 기록과 그 syslog 줄이 없다), 표지를 지우고 가드를 푼다. 출력 `reset`(두 번째 cleanup 은 `none`, 다시 리셋하지 않음).
  2. (러너 쪽 표지 `$ctl/memload-drop-205` 는 run 이 66 붙잡음 **앞에** 실행 시작 시각을 적어 만든다.) 곧바로 was01, was02 에서 `ss -K -tn dst 192.168.126.11 dport = :5432`(실행기 계정, 상시 연결 위, 그 sudo 줄은 첫 규칙이 거름). 리셋한 서버가 영영 답하지 않을 연결(붙잡힌 풀 연결, 버려진 연결 시도 스레드의 소켓)만 그때 있다. 끊으면 그 스레드가 IO 오류로 끝나고 풀이 새 연결을 맺는다. 그대로 두면 Tomcat 이 다시 뜰 때까지 남는다(다음 녹화의 평시를 바꾼다). 끊은 뒤 남은 ESTABLISHED 연결 수를 읽어 0 이 아니거나 WAS 에 닿지 못하면 cleanup 을 실패로 낸다. 표지가 끊기를 빚으로 남긴다. 66 이 none 을 돌려주는데 표지가 있으면(리셋 답이 ssh 에서 사라짐, 66 가드가 기한에 스스로 리셋, 또는 리셋이 아예 없음) db01 복귀를 기다린 뒤 db01 의 부팅 시각(/proc/stat btime)을 읽어 **실행 시작보다 뒤에 부팅했을 때만** 두 WAS 에서 끊는다(loose, 새 연결도 잘리지만 풀이 다시 맺는다, 남은 수는 보지 않음). 부팅하지 않았으면 그 연결은 살아 있는 것이라 끊지 않는다. 끊을 때 WAS 커널의 소켓 파괴 지원(CONFIG_INET_DIAG_DESTROY)을 읽어 없거나 모르면 출력에 남긴다(1, 2회차 평가 권고, 시험 포함).
  3. db01 이 관리망으로 다시 답할 때까지(실행기 `state` 가 `default`, 최대 420초, 5초마다) 기다린다.
  4. db01 에서 유닛이 없음을 확인하고 postgresql@16-main 이 active 이고 `pg_isready` 에 답할 때까지(최대 240초) 기다린다. 서버 쪽 가드는 부팅 때 tmpfiles 규칙이 지운 상태라 해제만 한다.
- 정리가 오지 않으면: 66 가드가 기한(max_injection_duration + 600초 = 30분)에 VM 이 붙잡혀 있으면 리셋한다(`vm-restore`). db01 쪽 작업도 1500초 뒤 스스로 끝나고 유닛 RuntimeMaxSec 1560초가 있다.
- 실패 길: 작업이 시작되지 못하면 run 이 서버 쪽을 정리하고 66 표지만 리셋 없이 거둔다(`vm-release`).
- 회복 조건: 러너 recovery(진입 200, 조회 비정상 < 0.1, 2틱)가 PostgreSQL 복구와 WAS 풀 회복을 함께 본다(풀이 새 연결을 맺어야 조회가 성공한다). 실행기 recovery 는 db01 유닛 없음과 66 표지 없음.
- 사람(운영자)이 하이퍼바이저로 되살린 행동이 119 에 남기는 흔적: db01 쪽은 `sms.server.last_boot_time` 이 리셋 시각으로, `master_agent_uptime` 새 값, syslog 의 부팅 줄(커널 'Linux version', 시작 유닛)이 종료 줄 없이, postmaster 를 포함한 모든 postgres 와 lucida-agent 의 pid 바뀜, 가용성 UNKNOWN 뒤 UP(11-19 에이전트 재시작 때 27초 UNKNOWN 선례). 66 쪽은 남지 않는다(QMP 에 바로 써 Proxmox 작업 줄이 없다). 사람이 Proxmox 화면의 Reset 을 눌렀다면 66 syslog(119 에 hostname bon04-13 으로 들어옴)에 `qmreset` 작업 줄이 남았을 것이다(11-20 `qmreboot`, `qmstart` 줄 실측). 남길지는 66 거르기 규칙과 함께 사람이 정한다(§13).
- 러너 쪽 영향: 리셋으로 러너의 db01 상시 연결이 끊기고 감시가 다시 연다. preflight 의 '다시 연 뒤 30분' 이 다음 녹화를 30분 늦춘다(식힘 30분과 겹친다). 먹통 동안 다시 열기가 배너에서 멈추면 15초 안에 포기하고 300초 뒤 다시 한다(러너 `vm_ops.py` MasterSupervisor). 인증이 끝난 뒤 끊기는 드문 경우에는 'given_up' 으로 러너 재시작 전까지 preflight 가 막힐 수 있다(첫 실행 확인).

### 9-5. 첫 실행 확인 항목 (아직 실측 없음)

**지휘 세션 절차(먼저):** 이 커밋은 66 도우미의 판(`t4-hv-exec-root` 틀과 `t4-hv-exec.sha256.json`)을 바꾼다. 러너 preflight 의 버전 표류 검사는 66 에 설치된 도우미의 체크섬을 러너 쪽 civil-petition deploy 사본의 `t4-hv-exec.sha256.json` 과 견주므로, 둘이 어긋나면 F83-R 만이 아니라 civil-petition 큐 전체(F72-P, F72-H, F82-R)가 preflight 에서 막힌다. 그래서 **커밋을 main 에 반영한 뒤 66 도우미 재설치(runbook §15-1 차례 2)와 러너 쪽 deploy 사본 갱신을 한 창에서 하고, 그 전에는 civil-petition 녹화를 보류한다.** 재설치 뒤 러너에서 도우미 `version` 답이 사본의 체크섬과 같은지 본다.

- **3초 설정 적용 여부**: 두 WAS 의 petition-context.xml 이 3초 판인지(11-25, 11-26 느린 요청은 10초 근처라 아직 옛 판). 옛 10초면 이 시나리오도 필수 중단에 걸린다.
- **66 도우미 판**: 이 커밋의 `vm-*` 동작이 든 판으로 66 에 다시 설치됐는지, 러너 쪽 civil-petition deploy 사본의 `t4-hv-exec.sha256.json` 이 같은 판인지(어긋나면 버전 표류 preflight 가 도메인 전체를 막는다).
- 먹통이 실제로 나는지: db01 SMS 보고와 프로세스 표본 공백 길이, 관리 ssh 무응답, PostgreSQL 이 3초 안에 로그인을 못 받는지(WAS 시간 초과 줄). 먹통이 얕아 PostgreSQL 이 3초 안에 답하면 피해가 적다(성공 조건이 안 서면 보강: 하한을 낮춤).
- 커널 OOM 이 끼어드는지(syslog 감시 FATAL 'Out of memory'). 끼어들면 희생은 RSS 가 가장 큰 작업(약 5.5 GiB)일 가능성이 크고 서버가 스스로 돌아온다. **그래도 cleanup 은 무조건 리셋하므로 '리셋으로 회복' 흔적(재부팅 줄, last_boot_time)이 장애와 상관없이 인공적으로 생긴다**(1회차 평가 권고 3). **녹화 판정: 녹화 구간 db01 syslog 에 커널 OOM 줄('Out of memory', 'oom-kill')이 있으면 그 실행은 버린다**(F80-P 와 같은 데이터에 인공 리셋 흔적만 더한 것, 큐 note 에도 같은 조건). 보강(하한, 경사 속도)을 낸다.
- db01 의 `lru_gen/min_ttl_ms`(preflight 가 0 이 아니면 거절)와 preflight 출력의 min_free_kbytes 값.
- ICMP 가 이어지는지, 가용성이 UNKNOWN 이 되는지(설계는 둘 다 '가능' 으로만 적었다).
- 공백 직전 표본에 페이지 읽기, 부하 평균 급증이 실제로 남는지.
- 부팅 줄과 'the database system is starting up' FATAL 이 119 에 닿는지(로그 감시는 파일 끝부터 읽으므로 에이전트가 늦게 뜨면 FATAL 은 빠진다).
- cleanup 시간(리셋에서 pg_isready 까지), `ss -K` 가 WAS 커널에서 되는지(Ubuntu 커널 `CONFIG_INET_DIAG_DESTROY=y`, 104 의 Ubuntu 6.5 설정에서 확인), 끊은 뒤 두 WAS 의 java 스레드 수가 평시로 돌아오는지.
- 러너 상시 연결이 'given_up' 에 빠지지 않는지.
- WAL 재생 시간과 부팅 직후 조회 지연(캐시가 비어 디스크에서 읽음).

### 9-6. 104 커널 모의 (카드 지시 2, 기전 확인이지 원칙 3 의 실측이 아님)

104(커널 6.5.0-44-generic, MGLRU 켬, min_ttl_ms 0)에서 `systemd-run --user --scope -p MemoryMax=1024M -p MemorySwapMax=0` 안에 파일 기반 작업 집합 128 MiB(무작위 파일을 mmap 해 0.1초마다 512쪽을 무작위로 읽는 probe, 프로그램 코드 흉내, 읽기 앞당김 끔)와 익명 메모리를 늘려 쥐는 프로세스를 함께 두었다. 하나씩, 150초씩. 기록 `/home/sjbang/tmp/claude-1008/-home-sjbang-dev-lucida-next/ee261603-595a-4ca8-a253-3a4bd5d2bb18/scratchpad/c11/sim/`(thrash.py, ctl.py, job.py, s1-*.jsonl, c2.jsonl).

| 익명 쥠(MiB) | 남은 파일 쪽(중앙, MiB) | probe 한 바퀴(중앙) | 다시 읽기(초당) | OOM |
|---|---|---|---|---|
| 752 | 128(작업 집합 다 들어감) | 0.5ms | 0 | 없음 |
| 912 | 101 | 12.1ms | 416 | 없음 |
| 960 | 53 | 32.7ms | 1,103 | 없음 |
| 992 | 22 | 46.9ms | 1,479 | 없음 |
| 1,100(한도보다 큼) | 8 까지 줄다 끝 | - | - | 파일 쪽이 바닥나자 1초 안에 OOM kill |

- 읽은 것: 파일 쪽이 작업 집합보다 작으면 다시 읽기가 끝없이 이어지고(22 MiB, 작업 집합의 17% 에서도) OOM 은 오지 않는다. OOM 은 파일 쪽이 거의 0 이 되는 순간에만 났다. 이것이 카드의 "죽지도 살지도 않는" 상태의 기전이다.
- 104 디스크는 다시 읽기 한 번이 약 0.03ms 라 probe 가 몇십 ms 느려지는 데 그쳤다. db01 디스크 읽기는 ms 단위(평시 최대 17ms)라 같은 다시 읽기 수가 수백 배 긴 멈춤이 된다. 66 위 F80-R 선례(§4-2)가 그 꼴(SSH 배너 시간 초과, 9분 뒤에야 OOM)이다.
- 실행기 작업 코드(`job.py`, 실행기 `MEM_JOB` 와 같은 띠 논리, 파일 쪽을 cgroup memory.stat 에서 읽게만 바꿈)를 하한 24, 상한 40 으로 돌리고 100초에 경쟁 프로세스가 240 MiB 를 60초에 걸쳐 잡게 했다(먹통 동안 PostgreSQL 백엔드가 늘어나는 꼴 흉내): 작업이 976 MiB 에서 751 MiB 로 돌려줘 파일 쪽이 23~35 MiB 로 지켜지고, 240초 내내 OOM 0, 다시 읽기 초당 약 1,500 이 이어졌다(사용자 scope 라 mlockall 은 못 했다. db01 에서는 root 라 잠긴다).
- 실제 db01 의 작업 집합(코드, 로그, WAL, 공유 버퍼 밖 데이터)은 모르므로 하한 32 MiB 는 "작업 집합보다 충분히 작고 OOM 경계보다는 큰" 값으로 골랐다. 먹통 깊이는 첫 실행에서 잰다. 얕으면 하한을 8~24 MiB 로 낮추는 보강을 계약만으로 낼 수 있다(실행기 하한 8 MiB).
- 실행기 작업 코드의 결함 하나를 시험이 잡아 고쳤다: 늘린 조각을 가리키던 변수가 남아 돌려줄 때 한 조각이 늦게 풀렸다(`del b`).

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Wikimedia 2014-06-19, 링크)와 요소별 대응표가 있고 기전이 같다. 계기 꼴, OOM 표시, 재시작 꼴의 차이를 적었다(§2) (원칙 1)
- [x] 분류 장부를 갱신했다(§4-1 행, §6 기록). 묶음 A 11 → 12(9.7%), 정답 위치 노드, 디스크 12 → 13(10.5%), 결제 경로 14/124(무관), 민원 5 → 6. 어느 축도 20% 에 닿지 않고 단순 규칙 판정은 보류(통과)다. F80-P 와 묶음, 정답 위치가 같은 점을 사람이 정할 것으로 올렸다(§3) (원칙 2)
- [x] 근본 원인 위치가 인프라 지점(db01 메모리, 스왑 0, 공유 버퍼 2GB)과 코드 위치로 확인됐다(§4) (G1)
- [x] 근본 원인의 흔적 자리가 119 실데이터에 있다: db01 SMS 메모리, 페이지, 부하 계열(1분, 빠짐 없음), 프로세스 표본, syslog(유닛 시작 줄, 부팅 줄 실측 선례), last_boot_time. 장애 때 모양은 시험 실행에서 확인(§7) (원칙 3)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: syslog, 로그 감시, SMS 지표, 60초 전체 프로세스 목록(§7) (원칙 3)
- [x] 계기의 흔적이 있다: 'Started petition-export.service' 줄, 프로세스 표본의 root python3(평시 7일 0), 메모리 경사. 인공 지연 없음 (원칙 4)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다(§5) (원칙 5)
- [x] 정답지 세 칸이 원칙 6대로 채워졌다(§5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다(§6) (원칙 7)
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: 유닛 이름 petition-export, 설명 'Yearly petition statistics export', 프로세스 이름 python3, 사용자 root. 실행기 계정 줄은 rsyslog 첫 규칙이 거르고, 66 은 QMP 에 바로 써 작업 줄이 없다. 명령줄은 표본에 없다. compile-plan 으로 만든 실행기 입력에 F83 이 없음을 확인 (원칙 8)
- [x] 부하 상한과 타임아웃 사슬(PgJDBC 3초, 풀 3초, 부하기 10초, 웹 30초)을 넣은 피해 계산과 중단 모의가 있다(§9) (원칙 9)

## 11. 119 보강 후보 (고치지 않는다, 지휘 세션 몫)

공통 지시의 119 사실(11-25): 이 도메인에서 이벤트는 지표 이상(stream-anomaly)만 생기고, 로그 감시 줄, syslog, 가용성 DOWN 은 이벤트가 되지 않으며, 한 서버 안 여러 시리즈 지표(프로세스별)는 이상 판정하지 않는다.

| 근거 | 지금 | 보강하면 |
|---|---|---|
| db01 SMS 보고 공백(에이전트 무응답) | 이벤트 안 됨 | '보고가 끊겼는데 핑은 됨' 을 사건으로 세우면 서버 먹통이 선다 |
| 가용성 UNKNOWN | 이벤트 안 됨 | 가용성 변화가 이벤트가 되면 먹통 시작이 시각과 함께 선다 |
| last_boot_time 바뀜, syslog 부팅 줄 | 이벤트 안 됨(변경 감지 원천에 재부팅이 있는지 모름) | 예정 없는 재부팅을 변경 사건으로 세우면 회복 행동이 선다 |
| syslog 의 작업 유닛 시작 줄 | 이벤트 안 됨 | 새 이름의 유닛 시작을 변경 사건으로 세우면 계기가 선다 |
| 두 WAS 앱 로그 ERROR 급증, 접근 로그 5xx 급증 | 이벤트 안 됨 | F72-P 시트 §11 과 같음 |
| db01 메모리 사용률(롤업), 페이지 읽기 | 판정 대상(밴드 있음, F80-P 시트 §11) | 이 시나리오에서 이벤트가 생길 거의 유일한 길. 경사가 공백 앞에 몇 표본뿐이라 판정에 쓸 점이 적다 |

## 12. 이번에 바꾼 공용 부분 (지휘 세션 검토)

1. `vmhost_job_executor.py`: 새 모드 `memload`(자기 키 집합 `mode`, `file_floor_mib`(8~1024), `file_ceiling_mib`, `ramp_mib_s`, `vmid`, 기존 압축 작업의 키와 인자는 그대로). preflight 에 lru_gen min_ttl_ms 확인과 min_free_kbytes 기록, cleanup 의 WAS 연결 끊기는 확인하고 실패하면 빚으로 남김(표지는 run 이 66 붙잡음 앞에 만들고, none 일 때는 db01 부팅 시각이 실행 뒤일 때만 끊음). 서버 쪽 프로그램(`MEM_NODE_BODY`)과 러너 쪽 진행(`MEM_DISPATCH`)이 따로다: preflight 에 66 `vm-check`, run 에 66 `vm-hold` 뒤 서버 가드와 유닛 시작(실패하면 `vm-release`), cleanup 에 66 `vm-reset` 뒤 WAS 연결 끊기와 db01 복귀, 서비스 기다림, recovery 에 66 `vm-state`. T 테스트베드의 DB 서버만 받는다(66 도우미가 T 에만 있음).
2. 66 도우미(`civil-petition/deploy/hypervisor/t4-hv-exec-root.in`, `t4-hv-exec`, `build.py`): `RESETS` 표(서버 웹, WAS, DB VMID, 인벤토리에서 채움, 라우터와 부하기는 없음)와 동작 `vm-check`, `vm-state`, `vm-hold`(확인, 가드, 표지 순), `vm-reset`(가드 미룸, 표지 있으면 QMP system_reset), `vm-release`, 가드 되돌림 `vm-restore`(앞 도우미가 넘기지 않음). `t4-hv-exec.sha256.json` 다시 만듦. **66 에 다시 설치해야 한다**(runbook §15-1 차례 2, 러너 쪽 deploy 사본과 함께).
3. `registry/profiles.json` vmhost.job 을 live_supported true, 같은 계약으로. `generate-manifests.py` 에 vmhost.job preflight 꼬리표(host-placement-and-oob-recovery).
4. 시험: 66 도우미의 vm 동작(가드가 변경보다 먼저, 리셋 한 번만, 거절), 실행기 memload 의 run, cleanup, recovery 와 실패 길(가짜 QMP 와 가짜 서버), 키 섞임 거절, 노드 실행기 목록과 개수 핀.

## 13. 사람이 정할 것

- F80-P 와 묶음(A), 정답 위치(DB 서버 노드)가 같다. 민원 두 테스트베드에서 'DB 서버 메모리' 정답이 둘이 된다. 그대로 둘지, 다음 카드를 다른 쪽으로 할지.
- v4 세트 변경 원인 몫이 3/11(27.3%)로 하한 30% 아래(세트 20건 미만이라 판정 보류). 다음 카드는 변경 원인 쪽.
- 66 도우미 재설치와 러너 쪽 deploy 사본 갱신을 같은 창에서 하는 것(어긋나면 도메인 전체 preflight 가 막힌다).
- 리셋을 qm 이나 Proxmox 화면처럼 66 작업 줄을 남기게 할지(사람의 회복 행동이 66 syslog 에 남음, 지금은 vmlink 와 같이 남기지 않음).
- cleanup 의 WAS 연결 끊기(ss -K): 실제로는 Tomcat 재시작이나 붙잡힌 채 두기가 흔하지만, 다음 녹화의 평시를 지키려고 넣었다. 그 순간 두 WAS 앱 로그에 IO 오류 줄 몇 개가 남는다(식힘 앞쪽).
- 녹화 시각 KST 09:00~21:00 을 운영 하네스가 지키게 할지.
