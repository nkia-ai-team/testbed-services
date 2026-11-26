---
title: F80-P 설계 시트, SMS 전용 민원 포털 DB 서버에서 작업이 메모리를 다 써서 커널이 PostgreSQL 을 죽임
status: Draft
owner: project
last_reviewed: 2026-11-25
tags:
  - scenario
  - design
  - memory
  - civil-petition-sms
summary: SMS 전용 민원 포털(civil-petition-sms) DB 서버 dbsvr01 에서 root 의 python3 작업 프로세스 열둘(각 512 MiB)이 서버 여유보다 많은 메모리를 잡아, 커널 OOM 킬러가 공유 버퍼를 RSS 로 세어 가장 크게 보이는 PostgreSQL 프로세스를 죽이고 systemd 가 postgresql@16-main 을 멈춘 채 두어 포털이 503, 500 을 내는 시나리오(Wikimedia 2017-07-28 재구성)의 설계 근거. 사용자와 정한 카드 C7.
---

# F80-P 설계 시트

- id `F80-P`, slug `f80-p-petition-db-job-memory-oom-stops-postgres`, 도메인 `civil-petition-sms`(SMS 테스트베드)
- 사용자와 정한 카드: C7 "작업이 DB 서버 메모리를 다 써서 커널이 PostgreSQL 을 죽임"(`~/dev/testbed-ops/v4/scenario-design/cards.md`)
- 상태: 후보(ready + `stage: candidate`), 설계 강도 하나로 고정한 evaluation 모드, 녹화 대기 큐에 추가(note: 새 테스트베드 첫 시나리오, 흔적 표 실측과 119 보강 뒤 녹화)
- 주입 수단: 기존 실행기 `vmhost.stress` 에 새 모드 `memprocs`(여러 작은 프로세스로 나눈 메모리 작업, 되돌리기에 서비스 유닛 다시 띄우기)와 첫 계약. 동반 부하 없음(상주 부하가 피해를 낸다)

## 1. 요약과 비유

SMS 전용 민원 포털은 웹(websvr01), WAS 두 대(wassvr01, wassvr02), DB(dbsvr01) VM 네 대가 한 업무망에 있고 SMS 로만 관제된다. dbsvr01 은 메모리 8GB 에 스왑이 없고 PostgreSQL 16 의 shared_buffers 가 2GB 다.
dbsvr01 에서 root 의 작업이 python3 작업 프로세스 열둘로 돌며 하나에 512 MiB, 합 6 GiB 를 몇 초 안에 잡는다. 서버 여유는 약 5.07 GiB 라 커널은 파일 캐시를 다 비운 뒤 메모리가 바닥나고, OOM 킬러가 RSS 가 가장 큰 프로세스를 죽인다. PostgreSQL 프로세스는 자기가 만진 공유 버퍼가 RSS 로 잡혀 각 약 2.1 GiB 로 보이므로 512 MiB 작업 프로세스보다 훨씬 커서, 죽는 것은 작업이 아니라 PostgreSQL 이다. postmaster 는 복구 재시작을 시작하지만 systemd 가 유닛 안의 OOM 종료를 보고 기본 정책대로 유닛을 멈추고, 유닛에는 자동 재시작이 없다. 작업을 멈추고 PostgreSQL 을 다시 띄울 때까지 포털의 거의 모든 요청이 몇 ms 안에 503, 500 이 된다.

비유: 금고실 옆 창고에 짐을 잔뜩 들여놓자 관리인이 자리를 비우려고 방에서 가장 커 보이는 짐을 내다 버리는데, 그게 금고였다. 금고는 큰 공용 서랍(공유 버퍼)을 제 몸처럼 달고 있어 가장 커 보였다.

## 2. 원본 사례

- **Wikimedia, 2017-07-28**, 사고 기록 "s5 (WikiData and dewiki) read-only",
  [공식](https://wikitech.wikimedia.org/wiki/Incident_documentation/20170728-s5_(WikiData_and_dewiki)_read-only). 자료 문서 `ref-real-world-incidents.md` M12 에 이 시나리오에서 더했다(출처가 말한 사실만).
  - "Jul 28 05:48:07 db1063 kernel: ... Free swap = 0kB", "Out of memory: Kill process 4744 (mysqld) score 757 or sacrifice child": s5 샤드 마스터 DB 서버에서 커널 OOM 킬러가 mysqld 를 죽임.
  - 원인: 2017-07-05 14:00 부터 돌던 pt-kill 프로세스의 메모리 누수("The reason for the leak turned it to be a pt-kill process running since that exact date", "pt-kill leaks memory (albeit slowly)"). pt-kill 은 위키데이터의 큰 장기 실행 관리자 질의를 끊으려고 돌던 도구. 누수가 db1063 의 메모리를 채운 원인으로 들었으니 같은 서버의 프로세스로 읽힌다(서버를 명시한 문장은 없음).
  - 영향: 05:48:14 mysqld_safe 가 MySQL 을 다시 띄웠고 복구 뒤 일부러 읽기 전용으로 떠 Wikidata 와 독일어 위키백과가 5:48~7:04 편집 불가. 탐지: IRC 사용자 보고, 한 시간 넘게 늦음. 후속: 메모리와 스왑 감시, pt-kill 대체, 전용 MySQL 프로세스의 oom_adj 조정.

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | DB 서버에서 함께 돌던 관리 도구(pt-kill)가 메모리를 계속 늘림(23일 누수) | DB 서버 dbsvr01 에서 root 의 작업(python3 작업 프로세스 열둘)이 6 GiB 를 몇 초 안에 잡음 |
| 원인이 된 결함 | DB 가 아닌 프로세스가 서버 메모리를 다 씀, 스왑 0 | 같음(작업 6 GiB 대 여유 약 5.07 GiB, 스왑 0) |
| 전파 경로 | 커널 OOM 킬러가 가장 큰 프로세스 mysqld 를 죽임 → MySQL 재시작 → 읽기 전용으로 떠 쓰기 불가 | 커널 OOM 킬러가 RSS 가 가장 큰 PostgreSQL 프로세스를 죽임 → postmaster 복구 재시작 → systemd 가 유닛을 멈춤(OOMPolicy=stop, Restart= 없음) → DB 연결 거부 |
| 사용자 증상 | 두 위키 편집, 계정 생성 불가(읽기는 됨) | 포털 로그인, 신청, 조회, 검색, 담당자 처리 503, 500(정적 첫 화면만 뜸) |
| 원인 쪽 상태 | 서버는 살아 있고 MySQL 은 다시 떴으나 읽기 전용 | 서버는 살아 있고(ICMP, 보고) PostgreSQL 유닛만 멈춤 |
| 탐지된 경로 | 사용자 보고, 커널 로그의 OOM 줄과 메모리, 스왑 그래프 | 웹, WAS 접근 로그 5xx 와 앱 연결 거부 ERROR, dbsvr01 syslog 감시의 커널 OOM 줄과 유닛 실패 줄, 메모리 사용률, 프로세스 표본 |
| 완화와 복구 | read_only=0 으로 되돌림, pt-kill 대체 | 작업을 멈추고 PostgreSQL 을 다시 띄움(cleanup) |

기전 확인: "DB 서버의 다른 프로세스가 메모리를 다 씀 → 커널 OOM 이 DB 를 죽임 → DB 가 쓰기를 못 받음" 고리가 같다. 다른 점은 셋이고 기전을 바꾸지 않는다. ① 시간 축: 원본은 23일 느린 누수, 재구성은 몇 초에 잡는 작업이다. 느리게 차오르면 OOM 직전 회수 압박 구간이 길어져 서버가 헛돌 수 있고(F80-R 실행 F80-R-run-bad63a76 에서 노드가 헛돌아 NotReady, SSH 불통) 그 구간의 요청이 10초를 넘겨 러너 필수 중단 조건에 걸린다(§9-2). ② DB 엔진과 재시작: 원본은 mysqld_safe 가 다시 띄우고 읽기 전용으로 두었고, 재구성은 Ubuntu systemd 가 유닛을 멈춘 채 둔다. 둘 다 "죽은 뒤 쓰기가 사람 손을 기다린다". ③ 희생자 고르기: 원본 mysqld 는 버퍼 풀이 익명 메모리라 그 자체로 가장 컸고, PostgreSQL 은 공유 메모리를 쓰지만 커널이 만진 공유 페이지를 프로세스 RSS 에 세어 같은 결과가 난다(§4-2).

## 3. 숫자 근거 (2026-11-25, `scenario-stats.py`, 정식 + 후보)

- 이번 실행에서 다른 점(공통 지시): 후보는 사용자와 정한 카드라 넓게 뽑기를 하지 않는다. 숫자는 상한 확인용이다.
- 서비스: 설계 GT0 결정 3 (다)와 S 결정 4(T 와 S 한 몫, v4 시작 뒤 생성분만 세어 1:1:1:1). 민원 1(F72-P) → 2. `scenario-stats.py` 는 정답지 domain 으로 civil-petition-sms 를 따로 1 로 센다(결정 4 의 한 몫과 'v4 시작 뒤' 구간은 모른다). 사용자가 이 두 테스트베드 카드 10장을 정했으므로 서비스 균형으로 막지 않는다.
- 묶음(합계 120, 20% = 24): **A 10 → 11(9%)**, B 7, D 17(14%), J 21(17.5%) 등. 주 묶음 A(서버 메모리 고갈), 보조 B(DB 프로세스가 죽고 유닛이 멈춘 채 남음).
- 정답 위치: **노드, 디스크 9 → 10(8%)**(§2-1 '인프라', 작업이 돈 서버). 가장 많은 은행 이체 서비스 11(9%). 결제 경로 14/120(11.7%, 이 후보는 무관).
- ID(1회차 평가 권고): 장부 정의상 F80-R(쿠버네티스 워커 tb-w2 의 백업 작업이 노드 메모리를 다 써 DB 파드가 빠짐)과 묶음(A), 기전(서버 위 작업이 메모리를 다 써 DB 가 빠짐), 정답 위치(작업이 돈 서버)가 같아 F44-P 선례대로 그 P 변형으로 둔다(처음 F81-R). 원본은 다르다(F80-R 은 GitHub 2026-09-20). F80-R 과 가르는 근거: ① 근거가 SMS 데이터뿐(KCM 축출 사건 없음) ② 희생자를 kubelet 축출이 아니라 커널 전역 OOM 이 공유 버퍼 RSS 로 고름 ③ 죽은 뒤 systemd 가 유닛을 멈춘 채 둠(파드처럼 다시 뜨지 않음).
- 단순 규칙 적중률(`--candidate F80-P`): 판정 **보류**(v4 세트 7건 < 20건, 통과로 본다). 변경 원인 몫 3/6 → 3/7(42.9%, 하한 30% 위). 규칙 1, 2, 6 적중률 50% → 42.9%, 정밀 100% 그대로(미끼와 함께 75%).
- 왜 이 후보인가: 사용자와 정한 카드. S 의 첫 시나리오이고 원인이 서버 위(작업 프로세스, 메모리)에 있어 SMS 데이터만으로 선다(S 문서 §8). 겉 증상(포털 전면 5xx, DB 연결 거부)이 F72-P(라우터 규칙, DB 살아 있음)와 같아 관제 AI 가 DB 서버의 메모리, 프로세스, syslog 감시 줄을 읽어야만 가른다.

## 4. 인과 사슬 (코드와 인프라 위치)

### 4-1. 사슬

1. 계기(dbsvr01): 실행기(opsjob, 상시 관리 연결 192.168.133.31)가 가드를 먼저 건 뒤 root 로 프로세스 묶음 하나를 띄운다. 묶음 안에 python3 작업 프로세스 열둘이 각자 512 MiB 를 64 MiB 조각으로 잡고(bytearray, 0 채움이라 페이지가 실제로 잡힘) 최대 1500초 쥔다(`scripts/scenarios/profiles/vmhost_stress_executor.py` CONTRACTS["F80-P"], `memprocs` 모드).
2. 메모리: dbsvr01 MemTotal 7,941 MiB(VM `sms.memory.total_size` 8,326,938,624), 스왑 0(`sms.virtual_memory.swap_total_size` 0), 가용 5,184~5,221 MiB, 약 5.07 GiB(`sms.other_memory.available_pages` 24시간 1,327,116~1,336,500 쪽, 4 KiB). 회수할 수 없는 것: PostgreSQL 공유 메모리 약 2.1GB(`civil-petition/deploy/templates/db/tuning.conf:3` shared_buffers = 2GB, 데이터가 캐시에 있어 다 참), 프로세스 익명 메모리(사용 약 137MB), 커널. 작업 6,144 MiB 는 여유보다 약 0.9 GiB(923~960 MiB) 많아 파일 캐시(약 5GB)를 다 비운 뒤 OOM 이 난다.
3. 희생자: 커널 OOM 킬러는 점수(RSS + 스왑 + 페이지 표, oom_score_adj 보정)가 가장 큰 프로세스를 죽인다(§4-2). PostgreSQL 백엔드 약 2.1~2.2GB, 체크포인터 약 2.1GB 가 작업 프로세스(512 MiB + 인터프리터)보다 커서 postgres 가 죽는다. postmaster 는 -900 이라 고르지 않는다.
4. PostgreSQL: postmaster 가 "server process (PID N) was terminated by signal 9: Killed" 를 남기고 다른 서버 프로세스를 모두 끝낸 뒤 복구 재시작을 시작한다. 그 사이 새 연결은 "FATAL: the database system is in recovery mode" 나 "not yet accepting connections" 로 거절된다(로컬 실험 §4-3).
5. systemd: 유닛 cgroup 의 memory.events oom_kill 을 보고 "postgresql@16-main.service: A process of this unit has been killed by the OOM killer." 를 남기고, 유닛에 OOMPolicy 가 없어 기본 `DefaultOOMPolicy=stop` 으로 유닛을 멈춘다(ExecStop `pg_ctlcluster ... -m fast stop`, "Failed with result 'oom-kill'"). 유닛에 `Restart=` 가 없어(`#Restart=on-failure` 주석) 다시 뜨지 않는다. 근거: Ubuntu snapshot 20261117T224105Z 의 postgresql-common 257build1.1 `postgresql@.service`, systemd 255.4-1ubuntu8.18 `system.conf` 의 `#DefaultOOMPolicy=stop` 과 `systemd.service(5)` OOMPolicy 절("If set to stop the event is logged but the unit is terminated cleanly by the service manager"). 설치 코드는 이 유닛을 그대로 쓴다(`civil-petition/deploy/steps/db.sh:32`, 덮어쓰기 없음).
6. 앱 쪽: JDBC 주소 dbsvr01 업무 주소 192.168.131.31:5432. 듣는 쪽이 없으니 연결이 바로 거부된다(RST). 풀 `civil-petition/deploy/templates/was/petition-context.xml:9-15`(minIdle 5, maxActive 40, maxWait 3초(2026-11-26 전에는 10초), testOnBorrow, validationInterval 30초). 읽기 경로 `ApiExceptionHandler.java:23-26`(DataAccessException → ERROR 'database access failed' 와 503), 신청과 담당자 처리 `PetitionRepository.java:29, 79`(@Transactional, 트랜잭션을 열 때 연결을 못 얻으면 500).
7. 웹: websvr01 Apache 균형기가 WAS 가 몇 ms 안에 돌려준 503, 500 을 그대로 사용자에게. 응답 상태로 WAS 를 빼지 않는다(F72-P 시트 §4 와 같은 설정).
8. 영향받지 않는 것: dbsvr01 자체(ICMP, SMS 에이전트 보고). lucida-agent RSS 14 MiB, sshd 8 MiB 라 희생자로 골라지지 않는다. PostgreSQL 이 빠진 뒤 메모리는 작업 6 GiB + 인터프리터 약 0.1 GiB + 나머지 약 0.6 GiB 로 약 1.1 GiB 가 남아 서버가 헛돌지 않는다.

### 4-2. 누가 죽는가 (계산)

- 커널 점수(Ubuntu 24.04 커널 6.8 `oom_badness`): RSS(익명 + 파일 + 공유 메모리 페이지) + 스왑 + 페이지 표, 거기에 `oom_score_adj × 전체 쪽 수 / 1000`. 공유 메모리는 그 프로세스가 만진 쪽만큼 RSS 에 들어간다.
- oom_score_adj: postgresql-common 257build1.1 `postgresql@.service` 에 `OOMScoreAdjust=-900`("prevent OOM killer from choosing the postmaster (individual backends will reset the score to 0)"), `pg_ctlcluster` 가 시작 때 `PG_OOM_ADJUST_FILE=/proc/self/oom_score_adj` 를 걸어 PostgreSQL 이 자식을 만들 때마다 0 으로 되돌린다(같은 패키지에서 읽음). S 설치 코드와 클러스터 `environment` 파일은 이를 바꾸지 않는다(저장소 grep 0).
- 실측 RSS(119 `lucida.process_snapshot`, host_name 192.168.131.31, 2026-11-25 11:53 UTC 표본): 백엔드 12610~12613 2,148~2,171 MiB, 체크포인터로 보이는 11890 2,103, 11891 1,528, 11-24 새 풀 연결 백엔드 1,936, 1,649, 45, 45, 20, postmaster 11889 83(-900). 그 밖의 가장 큰 프로세스 multipathd 27, unattended-upgr 22, lucida-agent 14.
- 작업 프로세스 하나: 512 MiB + python3 인터프리터 약 9 MiB. 점수 = 약 13.3만 쪽. postgres 백엔드는 약 55만 쪽. 그래서 첫 희생자는 postgres 다(약 네 배 차이). 커널은 가장 큰 하나만 고르므로 원리상으로는 '작업 프로세스보다 크다' 면 되지만, 공유 버퍼 RSS 는 프로세스마다 만진 만큼만 잡히고 시점에 따라 출렁이므로(아래 재현성) 여유를 두려고 프로세스를 512 MiB 로 나눴다(1회차 평가 권고 반영, 처음 설계는 1 GiB 여섯). 작업을 프로세스 하나(6 GiB)로 두면 그 프로세스가 가장 커서 작업 자신이 죽는다(C6 막힘의 확인 사실과 같은 이유).
- 재현성 조건: postgres 의 가장 큰 프로세스가 작업 프로세스보다 커야 하고, preflight 는 그 두 배(1,024 MiB) 이상을 요구한다. 두 배로 둔 까닭: 표본은 60초 전 값이고 첫 kill 뒤 연쇄 kill 사이에도 postgres 쪽이 계속 커 보여야 해서다. PostgreSQL 이 막 다시 뜬 직후에는 프로세스들이 작아(새 백엔드 20~45 MiB) 작업 프로세스가 먼저 죽는다. 그래서 실행기 preflight 가 유닛 cgroup 의 가장 큰 프로세스 RSS 가 1,024 MiB(작업 프로세스 둘) 이상인지 본다. 처음 설계(1 GiB 여섯, 문턱 2,048 MiB)는 지금 최댓값 2,171 MiB 와 여유가 123 MiB 뿐이라 한 번 다시 뜨면 며칠 문턱이 안 찰 수 있었다(1회차 평가 지적). 실측으로 11-24 에 새로 맺은 풀 백엔드가 1분에 158 MiB, 12분에 451, 40분에 약 720, 33시간 뒤 1,936 MiB 였다(같은 표에서 pid 51596 의 시간별 RSS). 체크포인터(11-19 시작)는 2,103 에 머문다. 이 꼴이면 다시 띄운 뒤 1,024 MiB 는 약 1시간 안팎에 다시 찬다(백엔드 하나가 40분에 약 720, 그 뒤 1~2시간에 1,000 을 넘김). 재녹화 간격은 첫 실행 뒤 실측으로 정한다(§9-5).

### 4-3. 로컬 실험 (2026-11-25, 104 docker, 축소, 원칙 3 의 실측이 아니라 기전 확인)

`postgres:16`(16.14) 컨테이너를 메모리 2,560 MiB, 스왑 0 cgroup 에 두고 shared_buffers 768MB, pgbench 80배 자료, postmaster 만 -900, 자식은 `PG_OOM_ADJUST_FILE` 로 0, pgbench 10 연결 지속(초당 20~60건)에 작은 perl 작업 프로세스를 띄웠다. 기록 `/home/sjbang/tmp/claude-1008/-home-sjbang-dev-lucida-next/ee261603-595a-4ca8-a253-3a4bd5d2bb18/scratchpad/c7/`(mon.log, pg-exp2.log, cl/client.log).

- 작업 프로세스(RSS 32 MiB)가 postgres 프로세스(최대 21 MiB, postmaster 제외)보다 클 때: OOM 63번이 모두 작업 프로세스를 죽였고 PostgreSQL 은 살았다. 지연은 OOM 동안 p99 241ms, 최대 360ms 로 잠깐 올랐다. 서버가 헛돌지 않았다.
- 작업 프로세스(RSS 13 MiB)가 postgres 프로세스보다 작을 때: 커널이 "WAL writer process (PID 64) was terminated by signal 9: Killed", postmaster 가 "terminating any other active server processes", "all server processes terminated; reinitializing", 복구 시작. 복구 중 연결 시도는 "FATAL: the database system is not yet accepting connections" 10줄(서버 로그), 클라이언트에는 "WARNING: terminating connection because of crash of another server process"(PostgreSQL 14 부터 클라이언트에만 가고 서버 로그에 안 남음). 메모리가 계속 차 있자 복구 중인 startup 프로세스도 죽어 "shutting down due to startup process failure" 로 postmaster 가 끝났고, 그 뒤 연결은 바로 "Connection refused"(1,180회, 기다림 없음).
- 결론: 희생자는 크기로 갈린다(§4-2 계산과 같음). PostgreSQL 이 죽은 뒤 연결 실패는 빨리 난다. systemd 의 OOMPolicy 는 컨테이너에 없어 실험하지 않았다(실기 dbsvr01 에서는 systemd 가 멈춤을 확정하고, 그것이 없어도 startup 프로세스가 죽으면 postmaster 가 스스로 끝난다).

## 5. 원인 규정 (원칙 5, 6)

- 근본(`root_cause.target_id`): dbsvr01 SMS 서버 대상 번호 `ef2e9ef9-936c-4a8e-afc8-b4e1a19b715d`(target_kind `node`, `civil-petition/deploy/inventory/s.targets.json`). 결함은 서버 메모리를 다 쓴 작업이고 고쳐야(작업을 멈추거나 상한을 두어야) 재발이 막힌다.
- 계기(`trigger_target_id`): 비움. 계기도 같은 서버 위 작업이다.
- 원칙 6: PostgreSQL 은 늘 하던 대로 돌다 커널에 죽은 피해자다(C 를 낸 B 가 아니라 B 를 죽게 한 A). 원본도 DB 가 아니라 메모리를 늘린 도구를 원인으로 적었다(같은 층위).
- 채점: 입도 `node`, accept `dbsvr01`, 서버 대상 번호, dbsvr01 메모리 대상 `610ae17d-ae99-4bb0-8153-66eb0645253f`. partial: PostgreSQL 로그 감시 대상 `0fb375aa-e758-4101-a680-6cfb43b719a1`(DB 자체를 원인으로 본 답), wassvr01, wassvr02, websvr01 의 이름과 대상 번호(증상 서버만 본 답).
- 채점 판별력 한계(1회차 평가 권고): 정답과 희생자가 같은 서버라, 'PostgreSQL 이 죽었다' 처럼 원인을 DB 자체로 본 답도 대상으로 dbsvr01 을 짚으면 입도 node 채점에서 만점을 받는다. partial 칸의 PostgreSQL 로그 감시 대상 번호는 그 답이 하위 대상을 짚을 때만 가른다. 원인 설명(작업 프로세스와 메모리)까지 가르려면 대상 번호가 아니라 근거 설명을 보는 채점이 필요하다.
- 원칙 5: 정답 "dbsvr01 에서 돈 작업이 메모리를 다 써서 커널이 PostgreSQL 을 죽였다" 는 관제 데이터(syslog 감시의 커널 OOM 줄과 유닛 실패 줄, 메모리 사용률, 프로세스 표본의 python3 작업 프로세스들과 postgres 사라짐, WAS 연결 거부)로 낼 수 있다. 작업의 명령줄과 목적, 커널이 PostgreSQL 을 고른 까닭(공유 버퍼 RSS, -900)은 119 에 없어 `beyond_data` 로 옮겼다.

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 | 기대(합격 조건 아님) |
|---|---|---|
| 감지(lucida-next) | 지금 이 도메인에서 이벤트가 되는 것은 지표 이상(stream-anomaly)뿐이다(공통 지시의 119 사실). 후보: dbsvr01 메모리 사용률(평시 1.5~1.9% → 약 79%, 롤업이라 판정 대상), 가용 페이지, cached 급감. 켜진 알람 정책 '메모리 사용률'의 문턱을 넘는지는 정책 값에 달림 | 이벤트, 이벤트 클러스터, 인시던트는 기대로만 적는다. 로그 감시 FATAL 과 ERROR 줄, 프로세스 사라짐은 이벤트가 되지 않는다(§11) |
| 피해 판정(러너) | 상주 부하(loadgen VM, domain civil-petition-sms) 요약의 접수번호 조회 비정상 비율 ≥ 0.5, 3틱(§9-3) | 평시 조회 약 0.01(없는 번호 1% 고정, 30초 창 최대 약 0.055) |
| RCA(합격 기준) | §7 의 SRE 길: 사용자 피해 줄 → WAS 연결 거부 줄 → dbsvr01 은 살았고 PostgreSQL 만 없음 → 커널 OOM 줄과 유닛 실패 줄 → 메모리 급등과 python3 작업 프로세스들 | 각 단계 평시 데이터가 119 에 있음을 조회로 확인(§7) |

## 7. SRE 가 데이터로 원인을 찾는 길 (합격 기준)

알람과 AI 설정과 상관없이 119 에 쌓인 데이터만으로 간다. 장애 때 모양은 모두 "시험 실행에서 확인"(흔적 표 실측 전)이다. 평시 값은 2026-11-25 KST 조회(로그 감시 7일, 메모리 24시간, 프로세스와 연결 1~3시간, ICMP 3시간).

| 단계 | 질문 | 어느 표, 칸, 대상 번호 | 평시 값(119 실조회) | 장애 때 모양 |
|---|---|---|---|---|
| 1 | 사용자가 실패하나 | CH `lucida.lucida_logs_local`(`event_source='log_monitor'`), websvr01 접근 로그 감시 `d4394f56-01ae-48fe-a0e5-25d6c308db36`(패턴 `" 5[0-9][0-9] `), 오류 로그 `b5d1b809-bd0d-4217-b463-8a79092a5ce6` | 7일 접근 로그 ERROR 2줄, 오류 로그 ERROR 6줄(11-24 11:51 KST tomcat 재시작 때 502) | 시험 실행에서 확인. 기대: /petition/api 요청마다 503, 500 줄(초당 5~30) |
| 2 | 어디서 실패하나 | wassvr01 앱 로그 `d2063720-6a36-4452-b3d5-64a2d2167207`, wassvr02 앱 로그 `c97d51e4-d004-407c-bc55-31b3aaf45c92`, Tomcat 접근 로그 `86fc8dcd-5b23-4ece-8c0c-8bfcada414e0`, `0c4c7c2b-506a-4c1a-9368-23dc40e18120`; CH `lucida.host_connections`(remote_port 5432) | 7일 앱 로그 ERROR 0줄(wassvr02 WARN 2), Tomcat 접근 로그 5xx 0줄. 연결 표본: WAS 마다 java → 192.168.131.31 ESTABLISHED 가 매 표본(1시간 120표본), dbsvr01 쪽 postgres ← WAS 마다 5 | 시험 실행에서 확인. 기대: 두 WAS 같은 때 'database access failed: ... Connection to 192.168.131.31:5432 refused' 와 Exception 줄, WAS → 5432 연결이 표본에서 사라짐 |
| 3 | DB 서버가 죽었나, DB 만 없나 | VM `sms.server.icmp_status`, `sms.server.rtt`(dbsvr01 `ef2e9ef9-...`), `sms.cpu.utilization`(`1b3e4ab0-cf06-4ec6-838d-6a874d37764f`); CH `lucida.process_snapshot`(host_name 192.168.131.31, name postgres); PostgreSQL 로그 감시 `0fb375aa-e758-4101-a680-6cfb43b719a1`(패턴 ERROR, FATAL|PANIC, WARNING) | ICMP 3시간 내내 1, RTT 3.81~13.1ms(중앙 4.26), CPU 0.73~1.94%, postgres 프로세스 16개(시간마다 pid 16, 7일 2,050 표본), PostgreSQL 로그 감시 줄 7일 0(S 수집 특성 표: 24시간 0, 파일에도 맞는 줄 없음) | 시험 실행에서 확인. 기대: ICMP 1 과 보고는 이어지고, postgres 가 표본에서 모두 사라지며, PostgreSQL 로그 감시에 FATAL('in recovery mode', 'not yet accepting connections', 'shutting down' 중 하나 이상)이 처음으로 들어옴 |
| 4 | 왜 DB 가 없나 | dbsvr01 syslog 감시 `4127c038-fb5f-424d-8749-43b15cf6125c`(파일 /var/log/syslog, fatal 패턴 `Out of memory|oom-kill`, error 패턴 `[Ff]ailed`, 'oom-kill' 을 담은 줄은 fatal 이 먼저) | 7일 ERROR 70줄(fwupd 'failed to add device' 66, update-notifier 'failed' 4), FATAL 0줄(S 네 서버 30일 FATAL 0). 30일 모든 호스트에서 커널 OOM 줄 0(감시 패턴 문자열을 담은 운영자 grep 줄만 있음) | 시험 실행에서 확인. 기대: 커널 'python3 invoked oom-killer'(할당하던 쪽), 'oom-kill:constraint=CONSTRAINT_NONE,... task=postgres,pid=N', 'Out of memory: Killed process N (postgres) total-vm:..., shmem-rss:...kB ... oom_score_adj:0' 줄이 FATAL 로, systemd 'postgresql@16-main.service: Failed with result 'oom-kill'.' 도 'oom-kill' 을 담아 FATAL 로 |
| 5 | 무엇이 메모리를 먹었나 | VM `sms.memory.utilization`, `sms.memory.used`, `sms.memory.cached`, `sms.other_memory.available_pages`(dbsvr01 메모리 대상 `610ae17d-ae99-4bb0-8153-66eb0645253f`); CH `lucida.process_snapshot`(name, user, mem_rss) | 24시간 사용률 1.47~1.88%, used 122~156MB, cached 7.07~7.37GB, 가용 쪽 1,327,710~1,336,338, 스왑 0. dbsvr01 python 프로세스 7일 0, postgres RSS 최대 2,171 MiB | 시험 실행에서 확인. 기대: 사용률 약 79%(작업 6,144 MiB + 나머지 / 7,941), cached 급감, 가용 쪽 약 28만 쪽(약 1.1 GiB)으로. 프로세스 표본에 root 의 python3 작업 프로세스 여러 개(살아 있는 동안 각 mem_rss 약 520 MiB, 시작 시각 같음, 연쇄 kill 로 몇이 함께 죽을 수 있음) |

조회 근거(모두 분할 칸과 시각 범위를 건 읽기 전용, 104 의 readonly=2 사본 `q.sh`):

```
-- 단계 1, 2, 3, 4: S 로그 감시 줄 수(7일)
SELECT host_name, target_id, severity_text, count() FROM lucida.lucida_logs_local
WHERE timestamp_date >= today()-7 AND timestamp > now() - INTERVAL 7 DAY AND event_source='log_monitor'
  AND host_name LIKE '192.168.131.%' GROUP BY 1,2,3
-- 단계 4: dbsvr01 syslog 감시 ERROR 줄의 꼴
SELECT replaceRegexpAll(substring(body,34,110),'[0-9]+','N') b, count() FROM lucida.lucida_logs_local
WHERE timestamp_date >= today()-7 AND timestamp > now() - INTERVAL 7 DAY AND event_source='log_monitor'
  AND target_id='4127c038-fb5f-424d-8749-43b15cf6125c' GROUP BY b
-- 단계 4: 커널 OOM 줄이 감시로 온 적이 있나(30일, 모든 호스트)
SELECT host_name, severity_text, count() FROM lucida.lucida_logs_local WHERE timestamp_date >= today()-30
  AND timestamp > now() - INTERVAL 30 DAY AND event_source='log_monitor' AND (body LIKE '%Out of memory%' OR body LIKE '%oom-kill%') GROUP BY 1,2
-- 단계 3, 5: 프로세스 표본
SELECT ts, pid, ppid, user, name, round(mem_rss/1048576,1) FROM lucida.process_snapshot
WHERE ts > now() - INTERVAL 3 MINUTE AND host_name='192.168.131.31' ORDER BY ts DESC, mem_rss DESC
SELECT name, user, uniqExact(pid), uniqExact(ts), round(max(mem_rss)/1048576) FROM lucida.process_snapshot
WHERE ts > now() - INTERVAL 7 DAY AND host_name='192.168.131.31' AND (name LIKE 'python%' OR name='postgres') GROUP BY 1,2
-- 단계 2: 연결 표본
SELECT host_name, direction, process, state, remote_addr, count(), uniqExact(timestamp) FROM lucida.host_connections
WHERE timestamp > now() - INTERVAL 1 HOUR AND host_name LIKE '192.168.131.%' AND (local_port=5432 OR remote_port=5432) GROUP BY 1,2,3,4,5
-- 단계 3, 5: VictoriaMetrics query_range sms.memory.*, sms.other_memory.available_pages, sms.server.icmp_status 등
--   {host_name="192.168.131.31"}, 24시간 step 300 / 3시간 step 60
```

표본 데이터 여부: 1~4 단계는 전수 원천(로그 감시 줄, 60초 프로세스 표본의 전체 목록, 30초 연결 표본, SMS 지표)이다. 트레이스, APM, NMS, syslog 전달은 S 에 없다.

## 8. 관측 근거 요약과 감별

- must_support(정답지 7개): 커널 OOM 줄(postgres 희생), 유닛 'Failed with result 'oom-kill'' 과 postgres 사라짐, 메모리 사용률 급등과 python3 작업 프로세스들, PostgreSQL 로그 FATAL, 두 WAS 연결 거부 ERROR 와 연결 표본의 5432 연결 사라짐, 접근 로그 5xx, dbsvr01 생존.
- must_rule_out(정답지 5개): PostgreSQL 자체의 메모리 폭주나 무거운 질의(메모리를 늘린 것은 python3, postgres RSS 는 평시 그대로), 운영자가 PostgreSQL 을 멈춤(auth.log 감시에 운영자 sudo 없음, 멈춤 앞에 커널 OOM 과 'oom-kill'), DB 서버 다운이나 망 단절(ICMP, 보고 이어짐, 연결 거부는 즉시), WAS 배포나 설정 변경(java pid 그대로, 두 대 동시), DB 과부하나 연결 고갈(시간 초과가 아니라 즉시 거부).
- contrast_with: F80-R(쿠버네티스 워커 백업 작업이 Oracle 파드를 밀어냄), F05-P(노드 메모리 고갈과 축출), F25-H(DB 컨테이너 자신의 한도 OOM), F72-P(같은 포털 전면 5xx 지만 DB 는 살아 있음).
- 러너 배제 조건: 상주 부하가 끊김(achieved_rps < 2.0).
- 미끼와의 관계: S 에도 U7 미끼(운영자 경로의 WAR 재배포, 웹 설정 재적용)가 섞인다. 미끼는 WAS 와 웹에서만 나고 dbsvr01 의 프로세스와 메모리를 바꾸지 않는다.
- 단순 규칙 쪽 메모: 이 시나리오는 변경 원인이 아니다. "장애 근처 메모리 급등 서버 = 원인" 이 지름길이 되지 않는지는 C6, C8 같은 메모리 카드가 쌓인 뒤 다시 본다(사람이 정할 것).

## 9. 러너 판정과 강도, 피해 계산 (원칙 9)

### 9-1. 피해가 나는 이유: 프로세스가 죽고 다시 뜨지 않는다

- 평시(근거 `~/dev/testbed-ops/v4/agent-out/APT2-facts/` k6 창, T 와 같은 스크립트와 하루 모양): 평균 약 19 rps, 설계 피크 30, 저점 5, 피크 p95 약 8ms. dbsvr01 CPU 약 1%.
- 부하기의 모든 요청은 `/petition/api/...` 이고 모든 경로가 DB 를 쓴다. PostgreSQL 이 내려가면 정적 첫 화면을 뺀 요청이 100% 실패한다. CPU 계산(앞선 카드의 함정)과 무관하다: 피해는 "자원 경쟁으로 느려짐"이 아니라 "DB 프로세스가 죽고 유닛이 멈춤"에서 난다.
- 지속: 한 번 죽으면 cleanup(min_hold 10분 뒤)까지 PostgreSQL 이 내려가 있다(§4-1 5). 되풀이 죽음에 기대지 않는다. 계산 갈래: systemd 가 유닛을 멈추지 않는 경우(§9-5 확인 항목)라도 메모리가 여전히 차 있으면 복구 중인 startup 프로세스가 죽어 postmaster 가 스스로 끝날 수 있고(§4-3 실험), 그렇지 않으면 PostgreSQL 이 복구한 뒤 공유 버퍼가 다시 차며 작업 6 GiB 가 남긴 약 1.1 GiB 를 넘을 때 다시 OOM 이 난다. 이때 희생자는 다시 큰 postgres 프로세스와 512 MiB 작업 프로세스의 크기 비교로 갈리고, 다시 차는 데 실측 꼴로 10분 넘게 걸려(§4-2: 새 백엔드 12분에 451 MiB) 피해가 짧아진다. 이 갈래면 첫 실행의 성공 조건이 서지 않으니 보강을 낸다.
- 실패 시간: 5432 에 듣는 쪽이 없으면 TCP 연결이 바로 거부되어(RST) 앱이 몇 ms 안에 503(신청과 처리는 500)을 낸다. 풀의 열린 연결은 PostgreSQL 이 끝날 때 서버 쪽에서 닫혀 다음 사용 때 바로 오류다. 풀은 maxActive 40 에 닿지 않고 maxWait(10초) 대기도 생기지 않는다(F72-P 시트 §9-1 과 같은 꼴).
- 문턱: 설계 §6-3 피해 판정 "사용자 비정상 비율 5% 이상 2분 지속" 에 대해 약 100% 가 약 10분. 감시 패턴에 걸리는 피해 줄은 웹 접근 로그 5xx, Tomcat 접근 로그 5xx, 앱 로그 ERROR 와 `Exception` 줄이다. 3초 이상 느린 요청 WARN 은 거의 생기지 않는다(빨리 실패한다).

### 9-2. 러너 필수 중단 조건(entry_status 0)에 걸리지 않는 이유 (C1 막힘 대응)

- entry_status 는 상주 부하의 마지막 신청 응답 상태다. 0 은 10초 안에 응답이 없을 때다.
- PostgreSQL 이 내려간 뒤: 연결 거부라 신청이 몇 ms 안에 500 이다. 장애 중 로그인이 실패해 신청 요청이 사라져도 모니터는 마지막 값(500)을 그대로 싣는다.
- 죽기 직전(회수 압박) 구간을 짧게 둔다: 작업 프로세스 열둘이 함께 512 MiB 씩 0 으로 채워 잡아 몇 초 안에 파일 캐시 약 5GB 를 밀어내고 OOM 에 닿는다. 이 동안 PostgreSQL 의 읽기는 공유 버퍼(2GB, 평시 다 참)에서 나와 파일 캐시가 비어도 거의 디스크로 가지 않는다. 로컬 실험(§4-3)에서 작업이 몰아 잡은 OOM 동안 지연은 최대 360ms 였다. 반대로 메모리가 천천히 차오르면 회수 압박이 길어 서버가 헛돌 수 있다(F80-R 실행 F80-R-run-bad63a76: 유닛 메모리 상한에 걸린 백업 작업이 노드를 헛돌게 해 NotReady, SSH 불통). 그래서 작업을 천천히 키우지 않는다.
- PostgreSQL 이 빠진 뒤 남는 메모리 약 1.1 GiB(7,941 − 6,144 − 108(인터프리터 12개) − 약 500(커널) − 약 40(나머지 프로세스)): 서버가 헛돌지 않고 관리 ssh 와 가드가 닿는다.
- 남는 위험: OOM 순간 진행 중이던 요청 몇 개가 postmaster 반응(실험에서 5초)만큼 늦을 수 있다. 10초를 넘겨도 다음 신청(500)이 값을 덮으므로 2틱(30초) 연속 0 이 되기 어렵다. 첫 실행 확인 항목에 둔다(§9-5).

### 9-3. 판정과 강도

- 강도: 하나로 고정(`approved-fixed-f80-p`): processes 12, mib 6144(작업 프로세스 하나 512 MiB), runtime_seconds 1500, revive_unit postgresql@16-main. 사다리가 없다: 작업 합이 여유보다 크면 OOM, 작업 프로세스가 postgres 의 절반 이하면 postgres 가 희생자다. 동반 부하 없음: 저점(5 rps)에서도 접수번호 조회가 초당 1.5건(30초 창 약 45건) 모두 실패해 판정이 선다.
- success: 상주 부하(domain civil-petition-sms) 접수번호 조회 비정상 비율 ≥ 0.5, 3틱(45초). min_hold 10m, settle 60s, level timeout 15m, max_injection_duration 20m. 신청 단계 비율은 쓰지 않는다(§9-4).
- must_rule_out: achieved_rps < 2.0(저점 목표의 40%, 부하가 끊겼을 때만).
- abort: entry_status = 0, 2틱(§9-2).
- recovery: target_health 200(전역), entry_status 200(로그인이 풀렸던 가상 사용자가 다시 로그인한 뒤의 첫 신청), 조회 비정상 비율 < 0.1, 2틱, 10분. VM 도메인 회복 규칙(pod_ready 대신 진입 상태, F72-P 가 compile-plan 에 더한 규칙)을 따른다.
- preflight: coordinator-clean, clean-window, baseline-traffic, target-health, vm-sms-entry-health(S 진입, 1회차 평가 지적으로 T 의 vm-entry-health 에서 바꿈). 실행기 preflight: python3, 작업 + 1 GiB ≤ MemTotal, postgresql@16-main 떠 있음, 그 cgroup 의 가장 큰 프로세스 RSS ≥ 1,024 MiB(작업 프로세스 둘), 작업 없음.
- 시각(예상, §9 계산): t0 작업 시작 → t0 + 수 초 파일 캐시 비움과 OOM, postgres 희생 → 수 초 안에 postmaster 복구 재시작과 systemd 유닛 멈춤, 첫 실패 → 3틱 뒤 success → t0 + 10분 cleanup(작업 멈춤, PostgreSQL 시작) → PostgreSQL 이 듣기 시작하면 다음 요청부터 회복(첫 몇 분은 디스크 읽기).
- 안전: 실행기 가드(U8)가 작업보다 먼저 걸리고, 기한(max_injection_duration + 600초 = 30분)이 되면 묶음을 멈추고 PostgreSQL 을 띄운다. 작업 자신도 1500초 뒤 끝난다. 작업 합 6 GiB 는 PostgreSQL 이 빠진 뒤 서버에 들어가 관리 ssh, 에이전트, 가드가 희생자로 골라지지 않는다.

### 9-4. 장애 중 로그인 상태 소실과 판정 신호

F72-P 시트 §9-4 와 같다(같은 앱, 같은 부하 스크립트, 같은 즉시 실패 꼴). 신청(8%), 내 민원(25%), 담당자 처리(11%)는 로그인 상태가 필요하고 장애 중 재로그인이 실패해 몇 분 안에 신청 요청이 사라진다. 접수번호 조회(30%)는 쿠키 단지 없이 부르는 공개 경로라 장애 내내 값이 실린다. 같은 모의를 이 카드에서 다시 돌린 결과(`/home/sjbang/tmp/claude-1008/-home-sjbang-dev-lucida-next/ee261603-595a-4ca8-a253-3a4bd5d2bb18/scratchpad/f72p_lookup_sim.py`, 장애 0~15분, 30초 창 30개 x 20회): 조회 0건 창은 5, 10, 19, 30 rps 모두 0개, 창당 조회 최소 27, 70, 143, 230건, 장애 중 비정상 비율 최소 1.00, 평시 창별 최대 0.055, 0.044, 0.031, 0.023. 문턱 0.5 는 넉넉히 가르고 회복 문턱은 0.1 이다.

- 119 부담: 실패 요청마다 감시 패턴에 걸리는 줄이 약 6줄(웹 접근 1, Tomcat 접근 1, 앱 ERROR 1, 스택의 Exception 줄 약 3)이라 10분에 약 1.8만~10.8만 줄(저점~피크). F72-P 와 같은 양이다. 처음 실행에서 로그 감시 수집 지연과 119 ClickHouse 메모리 예산을 함께 본다.

### 9-5. 첫 실행 확인 항목 (아직 실측 없음)

- 커널 OOM 이 실제로 postgres 를 고르는지(syslog 감시의 'Killed process N (postgres)'), 'invoked oom-killer' 줄이 python3 을 가리키는지, 몇 번 죽이는지.
- systemd 가 postgresql@16-main 을 멈추는지('Failed with result 'oom-kill'' 줄이 syslog 감시로 오는지)와 cleanup 의 systemctl start 로 다시 뜨는지. 멈추지 않으면 §9-1 계산 갈래대로 피해가 짧아질 수 있다.
- PostgreSQL 로그 감시에 FATAL 줄이 실제로 오는지(복구, 종료 창의 연결 시도 수에 달림. 저점 5 rps 에서도 몇 초 창에 몇 건 예상).
- OOM 순간 요청 지연(10초 넘는 것이 있는지, entry_status 0 틱).
- SMS 에이전트(lucida-agent) 보고가 OOM 순간 끊기는지, 프로세스 표본이 python3 작업 프로세스들과 postgres 사라짐을 담는지(60초 표본).
- 메모리 사용률 계열(롤업)이 이상 이벤트가 되는지, 켜진 '메모리 사용률' 알람 정책의 문턱을 넘는지(기대로만).
- 연쇄 kill(1회차 평가 권고): 공유 메모리는 postmaster 가 재초기화할 때까지 매핑이 남아 첫 kill 로는 거의 풀리지 않는다(docker 기록에서 첫 kill 뒤 postmaster 반응까지 5.7초). 그 사이 작업이 계속 잡으면 커널이 postgres 프로세스를 더 죽이거나, 큰 postgres 가 다 끝나 가는 중이면 python3 작업 프로세스를 죽일 수 있다. 몇 번, 무엇을 죽였는지 syslog 감시의 'Killed process' 줄 수와 이름으로 센다. 그래서 정답지에는 python3 개수를 적지 않았다.
- 전역 OOM 의 헛돎: docker 실험은 cgroup OOM 이라 전역 OOM 앞의 회수 압박(서버 전체가 헛도는지)을 보여 주지 않는다. 기전 확인으로만 쓴다. 첫 실행에서 OOM 앞뒤 dbsvr01 보고 공백, 관리 ssh 응답, 요청 지연을 본다.
- 수집 통로 확인: 7일 0줄이라 수집 통로가 실데이터로 확인되지 않은 감시 넷(PostgreSQL 로그 `0fb375aa-e758-4101-a680-6cfb43b719a1`, wassvr01 앱 로그 `d2063720-6a36-4452-b3d5-64a2d2167207`, Tomcat 접근 로그 `86fc8dcd-5b23-4ece-8c0c-8bfcada414e0`, `0c4c7c2b-506a-4c1a-9368-23dc40e18120`)에 장애 줄이 실제로 오는지.
- cleanup 뒤 PostgreSQL 의 가장 큰 프로세스 RSS 가 다시 1,024 MiB 넘게 차는 데 걸리는 시간(§4-2 실측 꼴로 수십 분). 실행기 preflight 가 그 전에는 다음 실행을 막는다(재녹화 간격을 정하는 근거).

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Wikimedia 2017-07-28, 링크)와 요소별 대응표가 있고 기전이 같다. 시간 축, 재시작 꼴, 희생자 고르기의 차이를 적었다(§2) (원칙 1)
- [x] 분류 장부를 갱신했다(§4-1 행, §6 기록). 묶음 A 10 → 11(9%), 정답 위치 노드, 디스크 9 → 10(8%), 결제 경로 14/120(11.7%, 무관), 민원 1 → 2. 어느 축도 20% 에 닿지 않고 단순 규칙 판정은 보류(통과)다(§3) (원칙 2)
- [x] 근본 원인 위치가 인프라 지점(dbsvr01 메모리, 스왑 0, PostgreSQL 유닛 OOMScoreAdjust와 OOMPolicy)과 코드 위치로 확인됐다(§4) (G1)
- [x] 근본 원인의 흔적 자리가 119 실데이터에 있다: dbsvr01 syslog 감시(평시 ERROR 70줄 수집, fatal 패턴에 OOM 문자열), 메모리 계열, 프로세스 표본(이름, 사용자, RSS). 장애 때 줄은 시험 실행에서 확인(§7) (원칙 3)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: 로그 감시, SMS 지표, 60초 전체 프로세스 목록(§7) (원칙 3)
- [x] 계기의 흔적이 있다: 프로세스 표본의 root python3 작업 프로세스들(평시 7일 0)과 메모리 사용률 급등, 커널 OOM 줄의 'invoked oom-killer'. 인공 지연 없음 (원칙 4)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다(§5) (원칙 5)
- [x] 정답지 세 칸이 원칙 6대로 채워졌다(§5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다(§6) (원칙 7)
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: 프로세스 이름 python3, 사용자 root, 상태 파일 /var/lib/opsjobs/extract.pgid(119 로 안 감), 가드 작업 extract. 실행기 계정 줄은 rsyslog 첫 규칙이 거른다. 명령줄은 표본에 없다. compile-plan 으로 만든 실행기 입력에 F80 이 없음을 확인 (원칙 8)
- [x] 부하 상한과 타임아웃 사슬(연결 거부 즉시, 풀 10초, 부하기 10초, 웹 30초)을 넣은 피해 계산이 있다(§9) (원칙 9)

## 11. 119 보강 후보 (고치지 않는다, 지휘 세션 몫)

공통 지시의 119 사실(11-25): 이 도메인에서 이벤트는 지표 이상(stream-anomaly)만 생기고, 로그 감시 줄(ERROR 포함), 가용성 DOWN 은 이벤트가 되지 않으며, 한 서버 안 여러 시리즈 지표(프로세스별)는 이상 판정하지 않는다.

| 근거 | 지금 | 보강하면 |
|---|---|---|
| syslog 감시의 커널 OOM 줄(FATAL, 'Killed process N (postgres)') | 이벤트 안 됨 | 로그 감시 FATAL 을 이벤트로 세우면 원인 서버와 희생 프로세스 이름이 시각과 함께 선다 |
| syslog 감시의 systemd 'Failed with result 'oom-kill'' (FATAL, 'oom-kill' 패턴) | 이벤트 안 됨 | 서비스 유닛 실패가 이벤트가 되면 "DB 가 멈춘 채 남음"이 선다 |
| PostgreSQL 로그 감시 FATAL(평시 0) | 이벤트 안 됨 | DB 쪽 사건이 인시던트 재료가 된다 |
| postgres 프로세스 16개 사라짐 | 프로세스별 지표는 이상 판정 안 함, postgres 프로세스 감시 대상 없음 | 프로세스 모니터(켜진 알람 정책 '프로세스 모니터 상태')에 postgres 를 등록하면 DOWN 이 선다. 등록은 고객 설정이라 테스트베드 평시 속성으로 정할 일 |
| 두 WAS 앱 로그 ERROR 급증, 접근 로그 5xx 급증 | 이벤트 안 됨 | F72-P 시트 §11 과 같음 |
| dbsvr01 메모리 사용률 급등(롤업) | 판정 대상. VM 에 `sms.memory.utilization.bands{host_name="192.168.131.31"}` 계열 3개가 있다(11-25 조회, 원칙 3 S 표의 '0 계열'은 그 뒤 바뀜) | 이 시나리오에서 이벤트가 생길 거의 유일한 길 |

## 12. 이번에 바꾼 공용 부분 (지휘 세션 검토)

1. `vmhost_stress_executor.py`: 새 모드 `memprocs`(자기 키 집합 `processes`, `revive_unit`, 기존 모드의 키와 위치 인자는 그대로). 작업 프로세스 P 개가 합 N MiB 를 64 MiB 조각으로 잡고 쥔다. preflight: python3, N + 1024 ≤ MemTotal, 되살릴 유닛이 떠 있음, 그 cgroup 의 가장 큰 프로세스 RSS ≥ 작업 프로세스 둘. state: 유닛이 내려가 있으면 기본이 아님. cleanup 과 가드의 되돌리기: 묶음 멈춤 뒤 유닛이 내려가 있으면 `systemctl start`. 되살릴 유닛은 서버 역할의 서비스만(db postgresql@16-main, was tomcat10, web apache2). 첫 계약 F80-P.
2. 시험(`tests/test_ready_profile_executors.py`): `test_memprocs_job_splits_into_workers_and_brings_the_service_back`(preflight 거절 둘, 작업 프로세스 둘이 실제로 RSS 를 잡음, 유닛이 내려간 뒤 cleanup 두 번과 유닛 시작, 연결이 끊긴 뒤 가드의 되돌리기가 묶음을 멈추고 유닛을 시작), `test_memprocs_parameters_name_the_role_service_and_even_workers`(역할과 유닛, 고른 나눔, 범위, 키 섞임 거절, 기존 모드 인자 그대로). 되살리기를 지운 변이에서 첫 시험이 실패함을 확인. `tests/vm_sandbox.py` 가짜 systemctl 에 `show -P ControlGroup` 응답을 더함.
3. `registry/profiles.json` vmhost.stress 를 live_supported true, 같은 계약으로. `generate-manifests.py` 에 vmhost.stress preflight 꼬리표(host-placement-and-oob-recovery).
4. 러너 저장소(커밋 안 함) `backend/tests/test_external_live_manifests.py` `test_every_live_controller_observation_passes_the_probe_allowlists`: S 프로브는 S 운영이 켜졌을 때(`CIVIL_PETITION_SMS_OPS`, tb-ops 처럼)만 답해, 첫 S controller 의 관측(lookup_nonok_rate, achieved_rps, domain civil-petition-sms)이 "operations off" 로 거절됐다. 시험 안에서만 S 운영을 켜고 옆 저장소의 S 인벤토리를 읽게 고침(러너 앱 코드 변경 없음, 재빌드 불필요). 운영 러너의 `CIVIL_PETITION_SMS_OPS` 가 켜져 있는지는 배포 때 확인할 일이다.
5. 미끼 누설 시험(러너 `test_decoys.py`)이 정답지 화면 이야기의 'Oracle' 낱말로 은행 도메인을 이름 부른 것으로 읽어 '/petition/api' 의 api 를 은행 api 서비스로 잡았다. 대조 줄의 'Oracle 파드' 를 'DB 파드' 로 바꿈.

## 13. 배포 전 확인 (사람이 할 일)

- 운영 러너(tb-ops)의 `CIVIL_PETITION_SMS_OPS` 가 켜져 있는지. 꺼져 있으면 S 관측(lookup_nonok_rate, achieved_rps)과 `vm-sms-entry-health` preflight 가 거절된다.
- preflight `baseline-traffic` 이 S 부하기를 보지 않는 러너 한계(F72-P 와 같음). S 상주 부하가 살아 있는지는 `vm-sms-entry-health` 와 must_rule_out(achieved_rps < 2.0)이 대신 본다.
- 재녹화 간격: cleanup 으로 PostgreSQL 을 다시 띄운 뒤 가장 큰 프로세스 RSS 가 1,024 MiB 를 넘기 전에는 실행기 preflight 가 막는다.
