---
title: F81-R 설계 시트, SMS 전용 민원 포털 DB 서버에서 운영자가 접수번호 유일 제약을 지워 조회가 표 전체를 읽고 DB CPU 100%
status: Draft
owner: project
last_reviewed: 2026-11-27
tags:
  - scenario
  - design
  - cpu
  - civil-petition-sms
summary: SMS 전용 민원 포털(civil-petition-sms) DB 서버 dbsvr01 에서 운영자가 sudo -u postgres psql 로 민원 표의 접수번호 유일 제약(그 색인이 접수번호 조회의 유일한 길)을 지워, 조회마다 약 200만 행을 읽고 바쁜 시간에 코어 4개가 차며 두 WAS 풀이 표 읽기로 차 포털이 503, 500 을 내는 시나리오(Chargebee 2018-03-02 재구성)의 설계 근거. 사용자와 정한 카드 C9.
---

# F81-R 설계 시트

- id `F81-R`, slug `f81-r-petition-receipt-key-drop-db-cpu`, 도메인 `civil-petition-sms`(SMS 테스트베드)
- 사용자와 정한 카드: C9 "운영자가 인덱스를 잘못 지워 앱 질의가 표 전체를 읽고 DB CPU 100%"(`~/dev/testbed-ops/v4/scenario-design/cards.md`)
- 상태: 후보(ready + `stage: candidate`), 설계 강도 하나로 고정한 evaluation 모드, 녹화 대기 큐에 추가(note: 새 테스트베드 첫 시나리오, 흔적 표 실측과 119 보강 뒤, 3초 설정 적용 뒤, KST 13~19시 시작)
- 주입 수단: 기존 실행기 `vmdb.lock` 에 새 모드 `drop-index`(운영자 경로의 DDL, preflight 부하 검사, 되돌리기는 카탈로그에서 읽은 같은 정의로 다시 만들기)와 첫 계약. 동반 부하 없음(상주 부하가 피해를 낸다)

## 1. 요약과 비유

SMS 전용 민원 포털은 웹(websvr01), WAS 두 대(wassvr01, wassvr02), DB(dbsvr01, 4 vCPU) VM 네 대가 한 업무망에 있고 SMS 로만 관제된다. 접수번호 조회(여정 30%, 로그인 없는 공개 경로)는 `petition.receipt_no` 로 한 건을 찾는데, 그 열의 색인은 UNIQUE 제약 `petition_receipt_no_key` 가 만든 B-tree 하나뿐이다.
운영자가 dbsvr01 에 로그인해 postgres 계정으로 그 제약을 지운다. 다음 요청부터 조회마다 민원 표 약 200만 행(약 1.5GB)을 처음부터 끝까지 읽는다. 한 번에 코어 하나로 약 0.8초다. 바쁜 시간(상주 부하 21 rps 이상, 조회 초당 약 6건 이상)에는 조회가 요구하는 CPU 가 코어 4개를 넘어, 표 읽기가 쌓이고 하나에 10~20초가 걸린다. 두 WAS 의 풀(각 40)이 표 읽기를 쥔 연결로 차면 나머지 요청은 연결을 3초 기다리다 실패한다(503, 신청과 처리는 500). 같은 정의로 제약을 다시 만들 때까지 이어진다.

비유: 도서관 대출 창구가 책 번호 색인 카드함 하나로 책을 찾아 주는데, 정리하던 직원이 그 카드함을 치웠다. 사서가 책 한 권 찾을 때마다 서가 전체를 훑느라 창구 줄이 끝없이 늘고, 다른 업무(반납, 회원 가입) 손님도 같은 창구 줄에 막힌다.

## 2. 원본 사례

- **Chargebee, 2018-03-02**, 상태 페이지 사후 보고 "Resolved" 사고, [공식](https://status.chargebee.com/incidents/pffpjnwyr92p). 자료 문서 `ref-real-world-incidents.md` 에 이미 있다(237 줄 근처 표, 이 시나리오에서 바꾸지 않음). 2026-11-27 원문을 다시 읽어 확인한 사실만 적는다.
  - 배포 중 할 일 하나가 "remove a unique constraint in a table whose column we are deprecating" 였고, 그 제약이 그 표의 색인 구실도 했다.
  - "the database CPU hit 100% causing the requests to pile up and timeout". 첫 오류 05:07 UTC, 05:17~05:26 UTC 앱 전체 9분 다운, 첫 오류부터 정상까지 19분.
  - 되돌리기: 색인을 되살리려면 메타 잠금이 필요했으나 질의가 도는 동안 얻지 못해, 백엔드 작업을 멈춰도 안 되자 서비스를 내리고 색인을 다시 더함.
  - 원인: "we failed to measure the impact of the queries on this table". DB 엔진, 실행 주체(사람인지 마이그레이션 도구인지), 탐지 경로는 적혀 있지 않다.
- 보조 근거(같은 '사람 손 DDL' 꼴, 원본으로 쓰지 않음): Vapi 2024-10-02(자료 문서에 있음, F33-P 원본) "Human error on our end led us to being index-less on our biggest table", DB CPU 100%, API 시간 초과.

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 배포 작업으로 폐기 예정 열의 유일 제약을 지움(누가 어떤 도구로 쳤는지 불명) | 운영자 sysop 이 dbsvr01 에 로그인해 `sudo -u postgres psql -c 'ALTER TABLE petition DROP CONSTRAINT petition_receipt_no_key'` |
| 원인이 된 결함 | 그 유일 제약이 그 표의 색인 구실을 하고 있었음(질의 영향을 재지 않음) | 같음. 접수번호 열의 색인은 그 제약의 것 하나뿐 |
| 전파 경로 | 질의가 색인을 잃음 → DB CPU 100% → 요청이 쌓여 시간 초과 | 조회가 표 전체 읽기 → DB CPU 100% → 표 읽기가 쌓여 두 WAS 풀 고갈 → 나머지 요청 3초 대기 실패 |
| 사용자 증상 | 앱 전체 다운 9분, 오류 19분 | 접수번호 조회 거의 전부 실패(10초 시간 초과나 503), 그 밖 기능 대부분 503, 500 |
| 원인 쪽 상태 | DB 는 살아 있고 CPU 100% | 같음. dbsvr01 은 보고와 핑을 이어 가고 CPU 를 postgres 백엔드가 씀 |
| 탐지된 경로 | 적혀 있지 않음 | 웹, WAS 접근 로그 5xx, WAS 풀 고갈 ERROR 와 조회 경로 느린 요청 WARN, dbsvr01 CPU 와 부하 평균, 연결 수, auth.log 의 운영자 psql 줄 |
| 완화와 복구 | 색인을 다시 더함(잠금을 못 얻어 서비스를 내림) | 운영자가 같은 정의로 제약을 다시 만듦. ACCESS EXCLUSIVE 를 얻기까지 진행 중 표 읽기를 기다려 수 초~십수 초 더 막힘(104 docker 포화 중 6초) |

기전 확인: "색인 구실을 하던 유일 제약을 지움 → 고빈도 질의가 표 전체를 읽음 → DB CPU 100% → 요청이 쌓여 시간 초과" 고리가 같다. 다른 점은 둘이고 기전을 바꾸지 않는다. ① 계기의 주체: 원본은 '배포 중 할 일' 로만 적혀 사람 손인지 도구인지 모른다. 카드는 운영자 손 변경을 요구하고, 사람 손 DDL 은 보조 근거(Vapi)가 있다. 재구성은 운영자 경로(설계 §6-2 Q35)로 쳐서 auth.log 에 원인 흔적을 남긴다. ② 열이 폐기 예정이라는 사정: 재구성의 접수번호 열은 폐기 예정이 아니다. 왜 지웠는지는 원본에도 관제 데이터에도 없는 의도라 정답에 요구하지 않는다(beyond_data).

## 3. 숫자 근거 (2026-11-27, `scenario-stats.py`, 정식 + 후보)

- 이번 실행에서 다른 점(공통 지시): 후보는 사용자와 정한 카드라 넓게 뽑기를 하지 않는다. 숫자는 상한 확인용이다.
- 서비스: 설계 GT0 결정 3 (다)와 S 결정 4(T 와 S 한 몫, v4 시작 뒤 생성분만 세어 1:1:1:1). 민원 3(F72-P, F80-P, F72-H) → 4. `scenario-stats.py` 는 civil-petition 2, civil-petition-sms 1 → 2 로 따로 센다(결정 4 의 한 몫과 'v4 시작 뒤' 구간은 모른다). 사용자가 이 두 테스트베드 카드 10장을 정했으므로 서비스 균형으로 막지 않는다.
- 묶음(합계 121 → 122, 20% = 24.4): **F 4 → 5(4%)**. 가장 많은 J 21(17%), D 17(14%). 주 묶음 F(느린 쿼리), 보조 없음. 계기는 스키마 변경(DDL)이지만 설정값 배포가 아니라 G 가 아니고(F33-R, F33-P 와 같은 판정), CPU 가 찬 것은 같은 질의의 읽는 행 수가 늘어서라 A 가 아니며, 잠금 대기가 아니라 D 가 아니고, 의도한 DB 의 의도한 제약을 지웠으니 P 가 아니다.
- 정답 위치: **DB 테이블(민원) 0 → 1**(§2-1 'DB 테이블(<무엇>)' 꼴). 가장 많은 노드, 디스크 11(9%), 은행 이체 서비스 11(9%). 결제 경로 14/122(11.5%, 이 후보는 무관).
- 단순 규칙 적중률(`--candidate F81-R`): 판정 **보류**(v4 세트 9건 < 20건, 통과로 본다). 변경 원인 몫 3/8 → 3/9(33.3%, 하한 30% 위, F 는 장부 '변경 계기' 아니오라 몫이 준다). 규칙 1, 2, 6 적중률 37.5% → 33.3%, 정밀 100% 그대로(미끼와 함께 75%). 규칙 5 적중률 37.5% → 44.4%.
- ID: 기전은 F33-R(food MySQL dispatches 색인을 마이그레이션이 지움)의 재발형(R)이지만 F33 의 R, P, H 가 다 쓰였다. 그래서 새 사례군 F81 의 첫 R 로 둔다(F80-P 는 F80 에 P 가 비어 있어 그 변형으로 둔 선례). F33-R 과 다른 축: DB 엔진(VM 위 PostgreSQL), 계기(운영자 손 DDL, 유일 제약), 관측(SMS 데이터뿐, 질의는 안 보임). 병렬로 설계 중인 다른 카드가 같은 F81 을 고르면 지휘 세션이 하나를 다음 번호로 옮긴다(사람이 정할 것).
- 왜 이 후보인가: 사용자와 정한 카드. 앞서 막힌 "DB 서버의 별도 작업이 CPU 를 다 씀" 은 리눅스가 CPU 를 고르게 나눠 앱 질의가 몇 ms 만 느려졌다. 여기서는 **앱 자신의 질의가 CPU 를 쓰므로** 공정 분배가 피해를 막지 않는다(§9-1). 겉 증상(포털 전면 5xx, DB 서버가 원인)이 F80-P 와 같아 관제 AI 가 CPU 와 메모리, 오류 꼴(풀 대기 대 연결 거부), 운영자 줄을 읽어야 가른다.

## 4. 인과 사슬 (코드와 인프라 위치)

### 4-1. 사슬

1. 계기(dbsvr01): 실행기(opsjob, 상시 관리 연결 192.168.133.31)가 preflight 부하 검사(§9-2)를 통과시키고 제약 정의를 카탈로그에서 읽어 가드를 먼저 건다. 이어 러너가 운영자 단말(192.168.122.21, S 관리망에서 192.168.133.5)에서 sysop 으로 새 ssh 로그인(pty)을 열고 `sudo -u postgres psql -X -d petition -c 'ALTER TABLE petition DROP CONSTRAINT petition_receipt_no_key'` 를 친다(`scripts/scenarios/profiles/vmdb_lock_executor.py:34-43` CONTRACTS["F81-R"], `vmdb_executor_common.py` DDL_BODY).
2. 스키마: `civil-petition/db/schema.sql:27` `receipt_no varchar(20) NOT NULL UNIQUE` 가 유일 제약 `petition_receipt_no_key` 와 그 색인을 만든다. 같은 열의 다른 색인은 없다(보관 표의 `petition_archive_receipt_idx` 는 다른 표).
3. 질의: `PetitionRepository.java:48-53` findByReceipt `... FROM petition p JOIN department d ON d.dept_id = p.dept_id WHERE p.receipt_no = ?`. 경로 `PortalController.java:74-81` GET /api/petitions/{receiptNo}(로그인 없음). 부하기 `loadgen/script.js:166-172` lookup(시간 제한 10초), 여정 30%(`script.js:227-235`).
4. 계획 바뀜(104 docker 실측, §4-2): 색인 조회 0.6ms → 병렬 순차 읽기(작업자 2) 344~364ms, 직렬 802ms, 버퍼 188,903 쪽(표 전체).
5. CPU: 조회 수요 = 초당 조회 × 한 번의 CPU. 상주 부하(`loadgen/entrypoint.sh:20-35` KST 시각별 rps: 0시 11, 4시 5, 10시 18, 11시 21, 12시 24, 14시 28, 15~17시 30, 21시 21, 22시 18)의 30%. 21 rps 이상이면 초당 약 6.3건 × 약 0.77~1.3 CPU초 = 4.9~8.2 코어 > 4(§9-1).
6. 풀: `civil-petition/deploy/templates/was/petition-context.xml:9-15` minIdle 5, maxActive 40, maxWait 3초, PgJDBC connectTimeout, loginTimeout 3초, socketTimeout 없음. 표 읽기가 쌓이면 연결을 10~20초 쥐고(부하기가 10초에 포기해도 질의는 끝까지 돈다) 풀이 찬다. 나머지 요청은 3초 뒤 'Timeout: Pool empty. Unable to fetch a connection in 3 seconds' → `ApiExceptionHandler.java:23-26` 503(신청과 처리는 @Transactional 이 연결을 못 열어 500).
7. 느린 요청 줄: `SlowRequestFilter.java:17-30` 3초 넘은 요청은 끝날 때 WARN `slow request method=GET uri=/petition/api/petitions/CP... status=200 took_ms=...`. 풀 대기 실패는 약 3초라 경계에 걸쳐 WARN 이 될 수도 있다(11-26 S 풀 고갈 때 실제로 WARN 1,033 줄).
8. 웹: websvr01 Apache(event MPM, Ubuntu 기본 작업자 150, 프록시 시간 제한 30초)가 WAS 응답을 그대로 돌려준다. 접근 로그 감시 패턴 `" 5[0-9][0-9] ` 이 5xx 를 잡는다.
9. 영향받지 않는 것: dbsvr01 자체(보고, 핑), 메모리 사용량, PostgreSQL 프로세스(죽지 않음). 표 heap(약 1,476MB)이 shared_buffers 2GB 의 1/4 보다 커서 순차 읽기는 링 버퍼(256KB)를 쓰고 페이지는 대부분 OS 페이지 캐시(dbsvr01 cached 약 7GB)에서 온다. 그래서 읽기마다 커널 복사 비용이 더해져 한 번 비용이 공유 버퍼 적중보다 크다(피해가 커지는 쪽). 디스크 읽기는 거의 없을 것으로 보고 첫 실행에서 디스크 IO 계열로 본다.

### 4-2. 로컬 측정 (2026-11-27, 104 docker, 원칙 3 의 실측이 아니라 비용 계산 근거)

`postgres:16`(16.14) 컨테이너 `--cpus=4 --memory=8g`, 설정은 `civil-petition/deploy/templates/db/tuning.conf` 와 같은 값, 저장소 `schema.sql` 과 `seed.sql`(200만) 그대로, VACUUM ANALYZE, pg_prewarm. 기록 `/home/sjbang/tmp/claude-1008/-home-sjbang-dev-lucida-next/ee261603-595a-4ca8-a253-3a4bd5d2bb18/scratchpad/c9/`(bench-restore.log, lookup.sql). 측정 때 104 는 1분 부하 평균 47(64 스레드)로 바빴다.

| 측정 | 값 |
|---|---|
| 표 크기 | petition heap 1,476 MB(188,903 쪽, 2,000,155 행), petition_receipt_no_key 108 MB |
| 조회(색인 있음) | 0.612 ms |
| `ALTER TABLE petition DROP CONSTRAINT petition_receipt_no_key` | 0.18초 |
| 조회(색인 없음) 병렬(작업자 2) | 344~364 ms, 버퍼 188,903 쪽 |
| 조회(색인 없음) 직렬 | 802 ms |
| pgbench 16 연결 포화 처리량 | 초당 5.24건(평균 지연 3,055 ms) → 한 번 약 0.76 CPU초 |
| 포화(40 연결) 중 `ALTER TABLE ... ADD CONSTRAINT ... UNIQUE (receipt_no)` | 6.0초, 그동안 처리량 초당 3~8건, 끝난 뒤 초당 14,600건 |
| 야간 통계 배치 SQL(BatchService.refreshStats, 표 전체를 읽는 같은 꼴) | 로컬 3.18~3.34초 대 dbsvr01 3.85, 5.43초(119 WAS 느린 요청 WARN 의 took_ms, 11-25, 11-26 01:10 KST), T db01 3.6~4.9초 |

dbsvr01 의 한 번 비용 추정: 통계 배치의 비(1.15~1.65)를 곱해 약 0.9~1.3 CPU초(로컬 0.77 보다 크다). 배치 시각에 디스크 읽기가 섞였다면 비가 부풀었을 수 있어 아래 모의는 0.55~1.8 을 모두 돌렸다. 실기 값은 실행기 preflight 가 실행마다 잰다(§9-2).

## 5. 원인 규정 (원칙 5, 6)

- 근본(`root_cause.target_id`): dbsvr01 SMS 서버 대상 번호 `ef2e9ef9-936c-4a8e-afc8-b4e1a19b715d`(target_kind `node`, `civil-petition/deploy/inventory/s.targets.json`). 결함은 PostgreSQL 민원 표의 사라진 유일 제약(색인)이고, S 에는 DB 대상이 없어 그 표를 품은 서버가 대상이다. 장부 정답 위치는 DB 테이블(민원).
- 계기(`trigger_target_id`): 비움. 계기(운영자 DDL)와 결함이 같은 곳(dbsvr01 의 DB)이다.
- 원칙 6: CPU 를 쓴 PostgreSQL 백엔드, 오류를 낸 WAS 와 웹은 피해자다. 앱 질의와 부하는 평시 그대로이고 바뀐 것은 질의가 기대던 색인이다(C 를 낸 B 가 아니라 B 를 바꾼 A). 원본도 질의가 아니라 제약 제거를 원인으로 적었다(같은 층위).
- 채점: 입도 `node`, accept `dbsvr01`, 서버 대상 번호, dbsvr01 CPU 대상 `1b3e4ab0-cf06-4ec6-838d-6a874d37764f`, auth.log 감시 `fb2b26aa-3056-4c45-a0c9-eea0d766271e`(원인 흔적 자리), PostgreSQL 로그 감시 `0fb375aa-e758-4101-a680-6cfb43b719a1`(DB 를 짚은 답). partial: wassvr01, wassvr02, websvr01 의 이름과 대상 번호(증상 서버만 본 답).
- 채점 판별력 한계: F80-P 와 같이 정답과 CPU 가 찬 곳이 같은 서버라, "DB 서버 과부하" 로만 짚은 답도 node 입도에서 만점이다. 원인 설명(운영자 DDL, 색인 상실)까지 가르려면 근거 설명을 보는 채점이 필요하다.
- 원칙 5: 정답 "dbsvr01 에서 운영자가 DB 스키마(유일 제약)를 바꾼 뒤 DB CPU 가 차 포털이 실패" 는 관제 데이터(auth.log 감시의 psql 줄, CPU, 부하, postgres 프로세스, 연결 수, 조회 경로만 느린 요청 줄)로 낼 수 있다. 어느 질의가 왜 표 전체를 읽는지(SQL, 계획)는 DPM 이 없고 PostgreSQL 의 DDL, 느린 문장 줄이 LOG 수준이라 119 에 없어 `beyond_data` 로 옮겼다.

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 | 기대(합격 조건 아님) |
|---|---|---|
| 감지(lucida-next) | 지금 이 도메인에서 이벤트가 되는 것은 지표 이상(stream-anomaly)뿐이다(공통 지시의 119 사실). 후보: dbsvr01 `sms.cpu.utilization`(평시 0.3~1.3% → 약 100%), `sms.cpu.load_average_1m`(0.0x → 수십), `sms.netstat.established_count`(16~20 → 약 95). 셋 다 dbsvr01 에 `.bands` 계열이 있다(11-27 조회). 켜진 알람 정책 'CPU 사용률' 의 문턱을 넘는지는 정책 값에 달림 | 이벤트, 이벤트 클러스터, 인시던트는 기대로만 적는다. 로그 감시 줄(auth.log WARN, 앱 ERROR, WARN)과 프로세스별 CPU 는 이벤트가 되지 않는다(§11) |
| 피해 판정(러너) | 상주 부하(loadgen VM, domain civil-petition-sms) 요약의 접수번호 조회 비정상 비율 ≥ 0.5, 3틱(§9-3) | 평시 조회 약 0.01(없는 번호 1% 고정, 30초 창 최대 약 0.055). 장애 중 모의 1.00 |
| RCA(합격 기준) | §7 의 SRE 길: 사용자 피해 줄 → WAS 의 조회 경로 느린 요청과 풀 고갈 → dbsvr01 CPU 100% 와 부하 → postgres 백엔드와 연결 수 → auth.log 의 운영자 psql DDL 줄 | 각 단계 평시 데이터가 119 에 있음을 조회로 확인(§7) |

## 7. SRE 가 데이터로 원인을 찾는 길 (합격 기준)

알람과 AI 설정과 상관없이 119 에 쌓인 데이터만으로 간다. 장애 때 모양은 모두 "시험 실행에서 확인"(흔적 표 실측 전)이다. 평시 값은 2026-11-27 KST 조회. 로그 감시 7일은 S 시험 구간 둘(11-26 09:00~12:30 KST 풀 고갈, 11-27 20:55 KST 이후 F80-P 실행)을 뺀 값이다.

| 단계 | 질문 | 어느 표, 칸, 대상 번호 | 평시 값(119 실조회) | 장애 때 모양 |
|---|---|---|---|---|
| 1 | 사용자가 실패하나 | CH `lucida.lucida_logs_local`(`event_source='log_monitor'`), websvr01 접근 로그 감시 `d4394f56-01ae-48fe-a0e5-25d6c308db36`(패턴 `" 5[0-9][0-9] `), Tomcat 접근 로그 `86fc8dcd-5b23-4ece-8c0c-8bfcada414e0`, `0c4c7c2b-506a-4c1a-9368-23dc40e18120` | 7일 websvr01 5xx 6줄, Tomcat 5xx 0줄 | 시험 실행에서 확인. 기대(모의): 30초에 503, 500 약 100~400줄 |
| 2 | 어디서, 어떻게 실패하나 | wassvr01 앱 로그 `d2063720-6a36-4452-b3d5-64a2d2167207`, wassvr02 앱 로그 `c97d51e4-d004-407c-bc55-31b3aaf45c92`(ERROR ` ERROR |Exception`, WARN ` WARN `) | 7일 두 서버 ERROR 0줄, 'Pool empty' 0줄, 느린 요청 WARN 5줄(모두 야간 배치 `/petition/admin/batch/stats`, `/archive`, 01:10, 02:22 KST) | 시험 실행에서 확인. 기대: 'Timeout: Pool empty. Unable to fetch a connection in 3 seconds' ERROR(로그인, 목록, 검색 등), 'Could not open JDBC Connection for transaction'(신청, 처리), 그리고 `uri=/petition/api/petitions/CP...` 만 `status=200 took_ms=10000` 이상인 느린 요청 WARN(조회 경로만 오래 걸려 끝남). 11-26 09:29~11:51 KST S 풀 고갈(원인 미확인, 10초 설정)에서 같은 꼴 줄이 실제로 왔다(ERROR 2,889 / WARN 1,033, 1시간) |
| 3 | DB 서버가 죽었나, 바쁜가 | VM `sms.cpu.utilization`, `sms.cpu.user_utilization`, `sms.cpu.load_average_1m`(dbsvr01 CPU 대상 `1b3e4ab0-cf06-4ec6-838d-6a874d37764f`), `sms.server.icmp_status`(서버 `ef2e9ef9-...`), `sms.cpu.steal_utilization` | 24시간(11-26 20:30 ~ 11-27 20:30 KST): CPU 0.31~1.32%(중앙 0.71), user 0.18~0.95%, 1분 부하 0.00~0.36, steal 최대 0.02%, 논리 코어 4, ICMP 1 | 시험 실행에서 확인. 기대: CPU 약 100%(user 가 대부분), 부하 평균 수십, ICMP 1 과 보고 이어짐 |
| 4 | 무엇이 CPU 를 쓰나, 누가 DB 에 붙나 | CH `lucida.process_snapshot`(host_name 192.168.131.31, name, user, cpu_pct, state), VM `sms.process.cpu_utilization`; CH `lucida.host_connections`(local_port 5432, remote_port 5432), VM `sms.netstat.established_count`(`cde238a9-e0dd-46c0-970e-4cac780eba04`) | 1시간(11-27 19:30~20:30 KST, 약 25 rps): postgres 16개(60 표본 960행), cpu_pct 최대 1.88, 평균 0.20, 그 밖 최대 lucida-agent 0.48. 연결: dbsvr01 쪽 direction='in' postgres 5432 ESTABLISHED 행을 remote_addr(WAS)별로 세면 표본마다 5(WAS 쪽 java 행은 표본당 1행으로만 보여 연결 수를 세는 자리로 쓰지 않는다). ESTABLISHED 16~20 | 시험 실행에서 확인. 기대: postgres 행이 WAS 연결만큼 늘고(최대 약 16 + 80) 다수가 state R, cpu_pct 큼. 그 밖 프로세스 CPU 평시 그대로. dbsvr01 쪽 in 행의 WAS 주소별 수 5 → 약 40 |
| 5 | 무엇이 바뀌었나 | dbsvr01 auth.log 감시 `fb2b26aa-3056-4c45-a0c9-eea0d766271e`(파일 /var/log/auth.log, WARN 패턴 `sudo:.*COMMAND`) | 7일 WARN 674줄(CRON root 세션 약 600, 운영자 세션 sudo: `systemctl status postgresql@16-main`, `tail -n 20 .../postgresql-16-main.log` 각 12 등). 30일 psql sudo 줄은 11-27 09:23~13:26 KST 유지보수의 줄(읽기 전용 count, `ANALYZE petition;` 등)이고, DDL(ALTER, DROP, CREATE) 줄은 0 | 시험 실행에서 확인. 기대: 장애 시작 수십 초 앞에 운영자 sysop 이 postgres 계정으로 친 ALTER TABLE petition DROP CONSTRAINT petition_receipt_no_key 를 담은 sudo COMMAND 줄(WARN). 실제 sudo 는 공백 든 인자를 따옴표로 감싸 적을 수 있고 TTY 칸도 달라질 수 있어 줄의 정확한 꼴은 첫 실행에서 확인한다. 운영자 세션 열림 줄은 `USER=postgres` 라 'session opened for user root' 패턴에 안 걸린다 |
| (닿지 않음) | DB 안에서 무엇이 느린가 | PostgreSQL 로그 감시 `0fb375aa-e758-4101-a680-6cfb43b719a1`(ERROR, FATAL|PANIC, WARNING) | 시험 구간 밖 7일 0줄 | `log_statement='ddl'` 의 'LOG: statement: ALTER TABLE ...' 와 `log_min_duration_statement=1000` 의 'LOG: duration: ... statement: SELECT ... receipt_no ...' 는 LOG 수준이라 감시 패턴에 안 맞아 119 에 안 간다(§11 보강 후보). 잠금 대기 줄도 LOG 다 |

조회 근거(모두 분할 칸과 시각 범위를 건 읽기 전용, 104 의 readonly=2 사본 `q.sh` 과 VictoriaMetrics query_range):

```
-- 단계 1, 2, 5: S 로그 감시 줄 수(7일, 시험 구간 둘 뺌)
SELECT target_id, severity_text, count() c, countIf(body LIKE '%slow request%') slow, countIf(body LIKE '%Pool empty%') pool
FROM lucida.lucida_logs_local WHERE timestamp_date >= today()-7 AND timestamp > now() - INTERVAL 7 DAY AND event_source='log_monitor'
  AND host_name LIKE '192.168.131.%'
  AND NOT (timestamp + INTERVAL 9 HOUR BETWEEN toDateTime('2026-11-26 09:00:00') AND toDateTime('2026-11-26 12:30:00'))
  AND NOT (timestamp + INTERVAL 9 HOUR >= toDateTime('2026-11-27 20:55:00')) GROUP BY 1,2
-- 단계 2: 느린 요청 WARN 의 경로와 시간(10일)
SELECT host_name, timestamp + INTERVAL 9 HOUR kst, extract(body,'uri=([^ ]+)') u, extract(body,'took_ms=([0-9]+)') ms FROM lucida.lucida_logs_local
WHERE timestamp_date >= today()-10 AND timestamp > now() - INTERVAL 10 DAY AND event_source='log_monitor' AND body LIKE '%slow request%admin/batch%'
-- 단계 5: auth.log 감시 줄의 꼴(7일), psql sudo 줄(30일)
SELECT severity_text, replaceRegexpAll(substring(body,17,200),'[0-9]+','N') b, count() c FROM lucida.lucida_logs_local
WHERE timestamp_date >= today()-7 AND timestamp > now() - INTERVAL 7 DAY AND event_source='log_monitor' AND target_id='fb2b26aa-3056-4c45-a0c9-eea0d766271e' GROUP BY 1,2
SELECT host_name, timestamp + INTERVAL 9 HOUR kst, substring(body, position(body,'COMMAND='), 140) FROM lucida.lucida_logs_local
WHERE timestamp_date >= today()-30 AND timestamp > now() - INTERVAL 30 DAY AND event_source='log_monitor' AND body LIKE '%COMMAND=%psql%'
-- 단계 4: 프로세스 표본, 연결 표본(1시간, 11-27 10:30~11:30 UTC)
SELECT name, user, count(), uniqExact(ts), max(cpu_pct), avg(cpu_pct) FROM lucida.process_snapshot
WHERE ts > toDateTime('2026-11-27 10:30:00') AND ts < toDateTime('2026-11-27 11:30:00') AND host_name='192.168.131.31' GROUP BY 1,2
SELECT host_name, direction, process, state, remote_addr, count(), uniqExact(timestamp) FROM lucida.host_connections
WHERE timestamp > toDateTime('2026-11-27 10:30:00') AND timestamp < toDateTime('2026-11-27 11:30:00') AND host_name LIKE '192.168.131.%'
  AND (local_port=5432 OR remote_port=5432) GROUP BY 1,2,3,4,5
-- 단계 3, 4: VictoriaMetrics query_range {host_name="192.168.131.31"} sms.cpu.utilization, sms.cpu.user_utilization, sms.cpu.load_average_1m,
--   sms.cpu.steal_utilization, sms.cpu.logical_core, sms.netstat.established_count, 끝 2026-11-27T11:30Z, 24시간, step 300
```

표본 데이터 여부: 1~5 단계는 전수 원천(로그 감시 줄, 60초 프로세스 표본의 전체 목록, 30초 연결 표본, SMS 지표)이다. 트레이스, APM, DPM, NMS, syslog 전달은 S 에 없다.

## 8. 관측 근거 요약과 감별

- must_support(정답지 6개): auth.log 감시의 운영자 psql DDL 줄, CPU 약 100% 와 부하 평균, postgres 백엔드 CPU 와 dbsvr01 이 받은 5432 연결(WAS 주소별) 증가, 조회 경로만 status 200 으로 오래 걸린 느린 요청 WARN, 풀 고갈 ERROR, 접근 로그 5xx.
- must_rule_out(정답지 5개): DB 서버의 다른 작업이 CPU 를 씀(앞서 막힌 카드의 꼴, 프로세스 표본에 다른 프로세스 없음), 부하 급증(조회 경로만 느림, 웹 NIC 와 WAS CPU 평시 대역), DB 다운이나 망 단절(ICMP 1, 오류가 풀 대기 시간 초과), 잠금 대기(CPU 가 차 있음), WAS 배포나 설정 변경(java pid 그대로, 운영자 줄은 dbsvr01 psql).
- contrast_with: F33-R(같은 기전, MySQL, 마이그레이션, 질의가 보임), F33-P(색인 교체 실수, commerce PostgreSQL), F80-P(같은 서버가 원인, 메모리, 즉시 연결 거부), F72-P(같은 포털 전면 5xx 와 운영자 sudo 줄, 라우터, 즉시 거부).
- F80-P 와 가르는 관측: F80-P 는 메모리 급등과 python3 작업 프로세스, 커널 OOM 줄, postgres 사라짐, 오류가 몇 ms 안의 연결 거부다. F81-R 은 메모리 평시 그대로, CPU 100%, postgres 가 늘어 일하고, 오류가 3초 풀 대기와 10초 넘는 조회다.
- 러너 배제 조건: 상주 부하가 끊김(achieved_rps < 2.0).
- 미끼와의 관계: S 에도 U7 미끼(운영자 경로의 WAR 재배포, 웹 설정 재적용)와 평시 운영자 세션(30~90분마다, db 는 `systemctl status`, `tail` 로그)이 섞인다. 미끼와 평시 세션은 dbsvr01 에서 psql 을 치지 않으므로 "운영자 로그인 = 원인" 지름길은 막히고, 원인은 줄 내용(psql DDL)으로만 선다.
- 단순 규칙 쪽 메모: 이 시나리오는 장부상 변경 원인 묶음이 아니다(F, 변경 계기 아니오). 실제 계기는 운영자 변경이라 "장애 앞 운영자 줄 = 원인" 규칙에는 맞는다(F72-P 와 같은 쪽).

## 9. 러너 판정과 강도, 피해 계산 (원칙 9)

### 9-1. 피해가 나는 이유: 서비스 자신이 CPU 를 쓴다

- 앞서 막힌 카드("DB 서버의 별도 작업이 CPU 를 다 씀")는 별도 프로세스 몇 개가 CPU 를 먹어도 CFS 가 앱 백엔드에 공정한 몫을 주어 몇 ms ~ 수십 ms 만 느려졌다. 여기서는 CPU 를 쓰는 것이 앱의 조회 질의 그 자체다. 조회가 요구하는 총 CPU(초당 조회 λ × 한 번 C)가 코어 4개를 넘으면 처리량은 초당 4/C 건에 묶이고 남는 조회가 쌓인다. 공정 분배는 쌓인 조회를 고르게 느리게 할 뿐 줄여 주지 않는다.
- 한도가 찬다: 쌓인 조회가 연결을 쥐어 두 풀(각 40)이 찬다. 읽기 상한(socketTimeout)이 없어 부하기가 10초에 포기해도 질의는 끝까지 돈다. 풀이 찬 뒤 다른 요청은 maxWait 3초 뒤 실패한다.
- 계산(λ = 0.3 × R, 처리 가능 4/C):

| KST 시각 | 상주 rps R | 초당 조회 λ | C=0.77 수요(코어) | C=1.0 | C=1.3 |
|---|---|---|---|---|---|
| 4시 | 5 | 1.5 | 1.2 | 1.5 | 2.0 |
| 9시, 23시 | 14 | 4.2 | 3.2 | 4.2 | 5.5 |
| 10시, 22시 | 18 | 5.4 | 4.2 | 5.4 | 7.0 |
| 11시, 21시 | 21 | 6.3 | 4.9 | 6.3 | 8.2 |
| 13시, 19시 | 26 | 7.8 | 6.0 | 7.8 | 10.1 |
| 15~17시 | 30 | 9.0 | 6.9 | 9.0 | 11.7 |

- 모의(`/home/sjbang/tmp/claude-1008/-home-sjbang-dev-lucida-next/ee261603-595a-4ca8-a253-3a4bd5d2bb18/scratchpad/c9sim/sim.py`, `sweep.py`): 10ms 간격, DB 4코어 처리기 공유, WAS 두 대 풀 각 40(FIFO, maxWait 3초), 웹 서버 작업자 150(WAS 응답까지 쥠, 부하기가 포기해도 쥠), 부하기 constant-arrival(시간 제한 10초, maxVUs 150, 넘치면 반복을 버림), 여정 비중 그대로, 주입 60초부터 900초까지, 씨앗 둘. 결과(주입 120초 뒤부터 30초 창):

| R | C | 조회 비정상 비율 최소 | 30초 창 5xx 평균 | 30초 창 10초 시간 초과(0) 평균 | 30초 창 3초 이상 요청 평균 | 신청 0 | 신청 최대 시간 |
|---|---|---|---|---|---|---|---|
| 14 | 0.77 | 0.00 | 0 | 0 | 1~2 | 0 | - |
| 14 | 1.0 | 0.94~1.00 | 17~34 | 119~120 | 20~37 | 0 | 3.0초 |
| 18 | 0.77 | 0.00~1.00(씨앗에 따라) | 20~35 | 143~155 | 34~42 | 0 | 3.0초 |
| 18 | 1.0 | 1.00 | 142~153 | 119~120 | 157~171 | 0 | 3.0초 |
| 21 | 0.55 | 0.00 | 0 | 0 | 2~4 | 0 | - |
| 21 | 0.77 | 1.00 | 110~124 | 154~155 | 126~143 | 0 | 3.0초 |
| 21 | 1.3 | 1.00 | 327~330 | 91~92 | 348~352 | 0 | 3.0초 |
| 24 | 0.77 | 1.00 | 201~208 | 155 | 237~242 | 0 | 3.2초 |
| 30 | 0.55 | 1.00 | 149~160 | 216 | 348~372 | 0 | 4.4초 |
| 30 | 0.77 | 1.00 | 287~294 | 155 | 613~618 | 0 | 5.3초 |
| 30 | 1.8 | 1.00 | 509~518 | 67 | 664~666 | 0 | 7.0초 |

  - 문턱은 날카롭다: λC 가 코어 4개의 약 1.05배면 씨앗에 따라 갈리고(R 18, C 0.77), 1.2배 이상이면 모든 씨앗에서 조회 비정상 비율이 1.00 이다. 그래서 실행기 preflight 문턱을 120% 로 둔다(§9-2).
  - 30 rps 에서 부하기 반복 버림이 30초 창에 약 30~170 생긴다(VU 150 이 시간 초과 요청으로 참). 그래도 30초 창 요청이 약 770~820(약 26 rps)이라 배제 조건(< 2.0)에 닿지 않는다.

### 9-2. 강도 조건: 한가한 시간에는 주입하지 않는다

- 저점(4시 5 rps)에는 수요가 코어 1.2~2.0개라 표 읽기가 쌓이지 않고 조회가 0.35~0.8초 걸릴 뿐이다(5xx 0, 3초 WARN 0). 그대로 녹화하면 피해가 없다(원칙 9 위반).
- 동반 부하로 rps 를 올리는 길은 쓰지 않았다: 진입 트래픽이 평시의 두 배가 되면 "부하 급증" 이 두 번째 원인 후보가 되고(웹 NIC, WAS CPU 에 보임), 원본(평소 트래픽에서 CPU 100%)과도 다르다.
- 대신 실행기 preflight 가 실행마다 dbsvr01 에서 잰다: ① 그 색인을 탄 조회 수(`pg_stat_user_indexes.idx_scan`)의 약 6초 증가분 → 초당 조회 ② 직렬 표 전체 읽기 한 번(`max_parallel_workers_per_gather=0`, 색인 계획 끔, 색인 첫 열 비교)의 시간 → C ③ 둘의 곱이 `nproc × load_pct(120)%` 이상일 때만 통과. 결과는 stderr 한 줄('reads N in M ms, one scan S ms, demand D of E milli-cores')로 러너 기록에 남는다. 104 docker 로 실제 PostgreSQL 에 돌려 확인: 초당 약 5건에서 3,851 대 4,800 으로 거절, 초당 약 15건에서 12,094 대 4,800 으로 통과.
- 이 검사가 남기는 것: 실행기 psql 한 번(postgres 계정, 0.8~1.3초 CPU 한 코어)과 6초 대기. 실행기 호출을 10초 안에 끝내는 규칙(설계 §6-2 표본 줄)을 지킨다. 느린 문장 줄(LOG)이 dbsvr01 로컬 서버 로그에 남을 수 있으나 119 에 가지 않는다.
- 결과적으로 preflight 가 통과시키는 시각은 상주 부하 약 21 rps 이상, KST 약 11~21시다(C 가 1.3 이면 9시부터). 다만 측정 창 6초와 PostgreSQL 통계 반영 지연(유휴 백엔드는 최대 약 10초 뒤 반영)으로 측정값이 약 ±20% 흔들리고, 120% 문턱이 모의의 갈림 경계(약 1.05배)에 가깝다. 그래서 문턱은 그대로 두고 녹화 대기 큐 note 의 집어 갈 시각을 KST 13~19시(26 rps 이상, 수요 1.5배 이상)로 좁혔다. 피크 30 rps 에서도 신청 최대 7초라 필수 중단에 닿지 않는다(§9-4).

### 9-3. 판정과 강도

- 강도: 하나로 고정(`approved-fixed-f81-r`): mode drop-index, table petition, index petition_receipt_no_key, load_pct 120. 사다리가 없다: 제약을 지우거나 안 지우거나다. 세기는 시각(상주 부하)이 정하고 preflight 가 문턱을 지킨다.
- success: 상주 부하(domain civil-petition-sms) 접수번호 조회 비정상 비율 ≥ 0.5, 3틱(45초). min_hold 10m, settle 60s, level timeout 15m, max_injection_duration 20m.
- 신청 단계와 전체 비율을 쓰지 않는 이유: F72-P 시트 §9-4 와 같다. 조회(30%)는 쿠키 단지 없이 부르는 공개 경로라 장애 중 로그인이 503 으로 풀려도 사라지지 않는다. 모의에서 30초 창 조회 수 최소 109(R 14)~255, 저점이면 preflight 가 막아 해당 없음.
- must_rule_out: achieved_rps < 2.0.
- abort: entry_status = 0, 2틱(§9-4).
- recovery: target_health 200(전역), entry_status 200, 조회 비정상 비율 < 0.1, 2틱, 10분. VM 도메인 회복 규칙(F72-P 가 compile-plan 에 더한 규칙)을 따른다.
- preflight: coordinator-clean, clean-window, baseline-traffic, target-health, vm-sms-entry-health(F80-P 와 같음). 실행기 preflight: psql 연결, 색인이 표에 있음, 운영자 홈, 부하 검사(§9-2).
- 시각(예상): t0 운영자 로그인과 DROP(0.2초) → t0 + 수 초 조회가 표 전체 읽기 → 수십 초 안에 CPU 포화와 풀 고갈(모의: 주입 뒤 첫 30초 창 비정상 비율 0.66~0.89, 다음 창부터 1.00) → 3틱 뒤 success → t0 + 10분 cleanup(운영자가 같은 정의로 ADD CONSTRAINT, 진행 중 표 읽기를 기다리며 수 초~십수 초 막힘) → 색인이 생기면 다음 요청부터 회복. 풀 연결은 유휴 60초 뒤 5 로 줄어든다.
- 안전: 실행기 가드(U8)가 변경보다 먼저 걸리고, 기한(max_injection_duration + 600초 = 30분)이 되면 root 가 같은 정의로 만든다(104 docker 실제 PostgreSQL 로 가드 되돌리기를 돌려 확인). 정의는 변경 직전 카탈로그에서 읽어 `/var/lib/opsjobs/ddl-petition_receipt_no_key.sql` 에 두고 다음 부팅에 지운다.

### 9-4. 러너 필수 중단 조건(entry_status 0)에 걸리지 않는 이유

- entry_status 는 상주 부하의 마지막 신청 응답 상태다. 0 은 10초 안에 응답이 없을 때다.
- 신청은 지운 색인과 무관한 INSERT 다(nextval, petition 과 history 쓰기). 풀에서 연결을 얻으면 CPU 가 찬 서버에서도 짧게 끝나고(1ms 남짓의 CPU 가 수십 개 백엔드와 공정 분배로 수십~수백 ms), 3초 안에 못 얻으면 500 이다. 웹 서버 작업자 150 이 차 접수 줄에서 기다리는 시간을 더해도 모의 최대 7.0초(R 30, C 1.8)다. 모든 격자에서 신청 0 은 0건이다(§9-1 표).
- 3초 설정이 전제다: 옛 10초(maxWait 10000)로 같은 모의를 돌리면 신청이 10초를 넘겨 30초 창 25개 가운데 16~17개에서 마지막 신청이 0 이다(R 21, 30, C 0.77, `old10.py`). 3초 설정이 S 의 wassvr01, wassvr02 에 적용됐는지 첫 실행 전에 본다(11-26 S 풀 고갈 줄은 'in 10 seconds' 였다).
- 장애 중 로그인이 실패해 신청 요청이 사라져도 모니터는 마지막 값(200 이나 500)을 싣는다.
- 남는 위험: 웹 서버 작업자가 150 보다 적게 설정돼 있거나(Ubuntu 기본 150 가정, 저장소 템플릿에 MPM 설정 없음) dbsvr01 이 모의보다 훨씬 느리면 신청이 웹 접수 줄에서 더 기다린다. 첫 실행 확인 항목에 둔다.
- C 가 1.8 을 넘으면 조회 하나가 DB 에서 30초(웹 프록시 시간 제한)를 넘길 수 있다. 그러면 Apache 가 502, 504 와 오류 로그 줄을 더 낸다(피해 흔적이 느는 쪽). 첫 실행에서 본다.

### 9-5. 되돌리기 시간이 녹화 창에 주는 영향

- cleanup 의 `ALTER TABLE ... ADD CONSTRAINT` 는 ACCESS EXCLUSIVE 잠금을 기다리는 동안 새 질의를 그 뒤에 세운다. 진행 중 표 읽기(최대 80개)는 새 표 읽기가 더 시작되지 않아 CPU 를 나눠 빨리 끝나고(남은 CPU 합 / 4 코어, 약 수 초~10초), 그 뒤 색인 만들기 약 3초(104 docker 포화 중 전체 6.0초). 그동안 포털은 계속 실패한다(장애 끝이 수 초~십수 초 늦어짐). 원본의 "메타 잠금을 못 얻어 서비스를 내림" 과 같은 꼴의 작은 판이다.
- 녹화 창(평시 2시간 + 장애 + 식힘 30분)에 비하면 짧아 창을 늘릴 필요가 없다. 되돌리기가 실패하면(잠금 대기 중 세션 끊김 등) 실행기 root 가 다시 하고, 가드가 마지막으로 한다. 되돌리기 줄(운영자 sudo psql ADD CONSTRAINT)도 auth.log 감시에 남는다(복구 흔적).

### 9-6. 장애 중 로그인 상태 소실과 판정 신호

F72-P 시트 §9-4 와 같다. 이 시나리오는 조회 자체가 피해 경로라 조회 비정상 비율이 피해를 그대로 잰다. 평시 창별 최대 비정상 비율(없는 번호 1%)은 5, 10, 19, 30 rps 에서 0.055, 0.044, 0.031, 0.023(F80-P 시트 §9-4 모의)이라 문턱 0.5 와 회복 문턱 0.1 이 넉넉히 가른다.

- 119 부담: 실패 요청마다 감시 패턴에 걸리는 줄이 약 6줄(웹 접근 1, Tomcat 접근 1, 앱 ERROR 1, 스택의 Exception 줄 약 3)과 느린 요청 WARN 이라, 10분에 약 1.3만~6만 줄(모의 5xx 30초에 110~520). F72-P, F80-P 와 같은 자릿수다.

### 9-7. 첫 실행 확인 항목 (아직 실측 없음)

- 앱 DB 기다림 3초 설정 적용 여부('Pool empty ... in 3 seconds' 줄).
- 실행기 preflight 가 적는 dbsvr01 실측(초당 조회, 한 번 읽기 ms, 수요): C 가 §4-2 추정(0.9~1.3초) 안인지. 병렬 작업자가 붙는 낮은 동시 구간의 CPU 몫.
- 조회 비정상 비율이 3틱 안에 0.5 를 넘는지, 포화까지 걸린 시간, 달성 rps, 부하기 반복 버림.
- 신청 응답 최대 시간과 entry_status 0 틱(웹 서버 작업자 수 포함).
- 느린 요청 WARN 이 조회 경로에 status 200 으로 오는지, 풀 대기 실패가 WARN 경계(3초)에 몇 줄 걸리는지.
- auth.log 감시에 운영자 psql 줄이 WARN 으로 오는지(`USER=postgres` 줄 꼴, psql 의 'could not change directory to "/home/sysop"' 경고는 운영자 터미널에만 남는다).
- dbsvr01 CPU, 부하 평균, ESTABLISHED, 프로세스 표본의 postgres 행 수와 state, cpu_pct.
- cleanup 의 ADD CONSTRAINT 걸린 시간과 그동안의 실패, 회복 시각.
- dbsvr01 의 다른 평시 일(30분 `pg-table-stats` 타이머의 count, lock_timeout 200ms)이 DROP, ADD 순간과 겹쳐 실패 줄을 남기는지(ARCH 수정 뒤로 조용히 끝나야 함).
- 웹 프록시 시간 제한(30초)을 넘는 조회가 있는지(502, 504, Apache 오류 로그).
- CPU 가 찬 동안 30분 주기 `pg-table-stats` 첫 읽기(statement_timeout 1초)가 실패해 dbsvr01 syslog 감시에 'Failed' 줄(서비스 실패)이 남아 주입과 짝을 이룰 수 있는지. 남으면 배관 흔적에 가까운 원인 쪽 줄이 되므로 흔적 표에 적고 지휘 세션이 판단한다.
- auth.log 감시에 들어온 운영자 sudo 줄의 정확한 꼴(인자 따옴표, TTY 칸).

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Chargebee 2018-03-02, 링크, 11-27 원문 재확인)와 요소별 대응표가 있고 기전이 같다. 계기 주체와 열 사정의 차이를 적었다(§2) (원칙 1)
- [x] 분류 장부를 갱신했다(§4-1 행, §6 기록). 묶음 F 4 → 5(4%), 정답 위치 DB 테이블(민원) 0 → 1, 결제 경로 14/122(11.5%, 무관), 민원 3 → 4. 어느 축도 20% 에 닿지 않고 단순 규칙 판정은 보류(통과)다(§3) (원칙 2)
- [x] 근본 원인 위치가 코드(schema.sql:27, PetitionRepository.java:48-53)와 인프라 지점(dbsvr01 PostgreSQL 제약)으로 확인됐다(§4) (G1)
- [x] 근본 원인의 흔적 자리가 119 실데이터에 있다: dbsvr01 auth.log 감시(평시 WARN 674줄/7일 수집, sudo COMMAND 패턴), CPU 계열, 프로세스와 연결 표본. 장애 때 줄은 시험 실행에서 확인(§7) (원칙 3)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: 로그 감시, SMS 지표, 60초 전체 프로세스 목록, 30초 연결 표본(§7) (원칙 3)
- [x] 계기의 흔적이 있다: 운영자 로그인과 sudo psql DDL 줄(평시 30일 DDL 0). 인공 지연 없음, 질의는 앱 그대로 (원칙 4)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다. 질의 수준 사실은 beyond_data(§5) (원칙 5)
- [x] 정답지 세 칸이 원칙 6대로 채워졌다(§5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다(§6) (원칙 7)
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: 운영자 명령은 실제 객체 이름(petition, petition_receipt_no_key), 가드 작업 이름 ddl-petition_receipt_no_key 와 정의 파일은 /var/lib/opsjobs(119 로 안 감), 실행기 계정 줄은 rsyslog 첫 규칙이 거른다. build_invocation 출력에 F81, drop-index 낱말 없음을 확인 (원칙 8)
- [x] 부하 상한과 타임아웃 사슬(풀 3초, 부하기 10초, 웹 작업자 150, 프록시 30초)을 넣은 피해 계산과 모의, 강도 조건(preflight)이 있다(§9) (원칙 9)

## 11. 119 보강 후보 (고치지 않는다, 지휘 세션 몫)

공통 지시의 119 사실(11-25): 이 도메인에서 이벤트는 지표 이상(stream-anomaly)만 생기고, 로그 감시 줄은 이벤트가 되지 않으며, 한 서버 안 여러 시리즈 지표(프로세스별)는 이상 판정하지 않는다.

| 근거 | 지금 | 보강하면 |
|---|---|---|
| PostgreSQL 서버 로그의 DDL 줄 'LOG: statement: ALTER TABLE ...'(log_statement ddl) | LOG 수준이라 감시 패턴(ERROR, FATAL|PANIC, WARNING)에 안 맞아 119 에 안 감 | DB 안 스키마 변경이 DB 서버 로그로 직접 선다. 감시 패턴은 시나리오별로 고치지 않는 평시 속성이라(설계 §2-3, log-monitors.json 머리) 고객의 흔한 감시 패턴으로 'statement:' 나 DDL 낱말을 넣을지는 테스트베드 평시 속성 결정 |
| 느린 문장 줄 'LOG: duration: N ms statement: SELECT ... WHERE p.receipt_no = ...'(log_min_duration_statement 1000) | 같은 이유로 안 감 | 어느 질의가 느린지가 관제 데이터에 남아 beyond_data 의 일부가 정답 칸으로 온다 |
| auth.log 감시 WARN(운영자 sudo psql DDL) | 이벤트 안 됨 | 운영자 변경이 이벤트가 되면 원인 시각과 서버가 이벤트로 선다(F72-P 시트 §11 과 같은 거리) |
| 두 WAS 앱 로그 ERROR 급증(풀 고갈), 느린 요청 WARN 급증, 접근 로그 5xx 급증 | 이벤트 안 됨 | F72-P, F80-P 시트 §11 과 같음 |
| dbsvr01 CPU 사용률, 1분 부하 평균, ESTABLISHED 수(롤업) | 판정 대상. dbsvr01 에 `sms.cpu.utilization.bands`, `sms.cpu.load_average_1m.bands`, `sms.netstat.established_count.bands` 계열이 있다(11-27 조회) | 이 시나리오에서 이벤트가 생길 길. 켜진 'CPU 사용률' 알람 정책 문턱도 넘을 수 있다 |
| postgres 프로세스별 CPU | 프로세스별 지표는 이상 판정 안 함 | 프로세스 모니터에 postgres 를 등록하면 그 프로세스 묶음 CPU 가 선다(고객 설정, 평시 속성) |

## 12. 이번에 바꾼 공용 부분 (지휘 세션 검토)

1. `vmdb_lock_executor.py`: 새 모드 `drop-index`(자기 키 집합 host, node, mode, table, index, load_pct. 기존 잠금 모드의 키와 인자는 그대로, 시험이 같은 인자를 단언). 운영자 경로 디스패치(`vm.operator_dispatch()`)를 쓴다. 첫 계약 F81-R.
2. `vmdb_executor_common.py`: `DDL_BODY`(present, definition, owned_by_constraint, state, 부하 검사 check, prepare 가 정의를 파일에, undo_body 는 같은 정의를 root 로, remove, op_apply_script 는 `sudo -u postgres psql -X -d petition -c 'ALTER TABLE ... DROP CONSTRAINT ...'`(제약이 아니면 DROP INDEX), op_remove_script 는 같은 정의). 이름 검사 `index_name`(소문자 식별자, 금지 낱말). 코어 수는 `nproc`, 시험용 덮어쓰기 `DB_CORES`.
3. 시험(`tests/test_ready_profile_executors.py`): `test_s_index_drop_goes_the_operator_way_and_is_built_again`(한가할 때 preflight 거절과 로그인 없음, 바쁠 때 통과와 수요 줄, 운영자 로그인 출발 주소, auth.log 꼴 sudo 줄 `USER=postgres ; COMMAND=psql -X -d petition -c ALTER TABLE ... DROP CONSTRAINT ...`, 가드 되돌리기에 정의가 있고 운영자 이름이 없음, 멱등, cleanup 의 운영자 ADD CONSTRAINT 줄, 운영자 없이 root 되돌리기, 연결이 끊긴 뒤 가드 되돌리기), `test_s_index_drop_parameters`(인자, 기존 잠금 모드 인자 그대로, 거절 여섯). 가드 되돌리기에서 정의를 뺀 변이로 첫 시험이 실패함을 확인. `tests/vm_sandbox.py`: 가짜 psql 에 색인 있음, 정의, 첫 열, 읽기 수, 표 읽기, DROP 과 ADD 응답, 가짜 sudo 에 `-u <계정>`(USER=<계정> 으로 기록). 노드 실행기 목록 시험의 사용 중 집합에 vmdb.lock.
4. 104 docker 의 실제 PostgreSQL 16 에 노드 프로그램을 돌려 확인(가짜 아님): preflight 거절과 통과, arm 의 운영자 명령, 운영자 DROP 뒤 state ours, hold 의 ADD CONSTRAINT, finish 뒤 default, 가드 되돌리기 파일을 직접 돌려 제약이 다시 생김.
5. `registry/profiles.json` vmdb.lock 을 live_supported true, 같은 계약으로. `generate-manifests.py` 에 vmdb.lock preflight 꼬리표(db-inverse-ddl-ready).
6. 러너 저장소: 바꾸지 않음.

## 13. 배포 전 확인 (사람이 할 일)

- S 의 wassvr01, wassvr02 에 앱 DB 기다림 3초 설정이 적용되고 재기준선이 끝났는지(§9-4).
- 운영 러너(tb-ops)의 `CIVIL_PETITION_SMS_OPS` 가 켜져 있는지(F80-P 시트 §13 과 같음).
- 한가한 시간에 집히면 흐름은 이렇다: 주입 전 실행기 preflight 가 실패하고(변경 없음, 운영자 로그인 없음), 운영 하네스가 resume 한 번이나 재실행 세 번 뒤 그 항목을 hold 로 둔다. 무한 대기나 깨진 녹화는 아니다. **사람이 정할 것**: 지휘 세션이 큐 순서를 시각(KST 13~19시)에 맞출지, 운영 하네스가 큐 note 의 시각 조건을 읽게 할지.
- 운영자 sysop 이 dbsvr01 에서 `sudo -u postgres psql` 을 비밀번호 없이 칠 수 있는지(11-27 유지보수 줄에 `USER=postgres ; COMMAND=/usr/bin/psql` 이 있어 될 것으로 봄).
