---
title: F41-R 설계 시트 (banking account-service 를 메모리가 새는 릴리스로 롤아웃)
status: Draft
owner: project
last_reviewed: 2026-10-09
tags:
  - scenario
  - design
  - release
summary: banking account-service 를 결함 있는 새 릴리스(core-banking-account:1.3.0, fault-images/f41-r 패치로 만든 별도 태그)로 롤아웃하면, 새로 더한 잔액 스냅샷이 0.5초마다 전체 계좌를 다시 읽어 계좌별 이력에 쌓기만 해 힙이 몇 분 만에 차고, 전체 GC 연속, OutOfMemoryError, liveness 재시작이 주기적으로 되풀이되어 잔액 조회와 이체가 그때마다 실패하는 시나리오. 원본은 Honeycomb 2019-11-06 잘못된 커밋의 메모리 누수로 모든 백엔드가 같은 속도로 차서 되풀이해 죽은 장애.
---

# F41-R 설계 시트

## 1. 요약

banking account-service 의 새 릴리스 1.3.0 은 잔액 조회를 Oracle 대신 메모리 스냅샷으로 답하는 기능을 더했다. 스케줄 작업이 0.5초마다 전체 계좌(약 1,000행)를 다시 읽어 계좌별 이력 끝에 붙이고, 조회는 가장 최근 값을 쓴다. 이력을 줄이는 곳이 없어 힙이 분당 약 30MB 씩 찬다. 메모리 한도, JVM 옵션, 프로브는 그대로라 기본 힙(약 246MiB)이 기동 약 6분 뒤 차고, 전체 GC 가 잇따르다(지연) OutOfMemoryError 로 Tomcat 요청 처리가 멈추고, readiness 와 liveness 가 실패해 kubelet 이 컨테이너를 재시작한다. 다시 뜬 컨테이너도 같은 릴리스라 같은 일을 되풀이한다.

비유: 계산대 옆에 영수증 보관함을 새로 달았는데 버리는 사람이 없다. 몇 분마다 보관함이 넘쳐 계산대가 멈추고, 점장이 계산대를 껐다 켜면 빈 보관함으로 다시 시작해 같은 일이 되풀이된다. 고칠 곳은 계산대(재시작)가 아니라 보관함을 단 새 설비(릴리스)다.

## 2. 원본 사례

- **Honeycomb, 2019-11-06** (공식 사후 보고): https://www.honeycomb.io/blog/incident-report-running-dry-on-memory-without-noticing
- 수집(ingest) 워커의 메모리 누수로 약 20분짜리 오류 구간이 네 번 생겨 고객 텔레메트리의 1~3% 가 간헐적으로 거절됨. 사후 보고: "a slow memory leak that manifested over hours, which leaked at the same rate on each ingest backend". 백엔드가 같아 몇 분 차이로 함께 죽었고 진행 중 요청이 실패, 새 요청은 건강한 백엔드를 찾지 못함. ALB 로그 'backend unreachable', 'backend timed out while processing'. 최근 배포가 없었다는 이유로 ALB 를 의심했다가, 다른 엔지니어가 재시작과 메모리 누수를 찾아 잘못된 커밋을 되돌린 릴리스를 배포해 복구. 재발 방지로 프로세스 크래시(panic, OOM) 비율을 진단 신호로 쓰기로 함.
- 자료 문서 M12 행에 세부를 더했다(출처가 말한 사실만).

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 잘못된 커밋이 든 릴리스 배포 | account-service 를 릴리스 core-banking-account:1.3.0 으로 롤아웃(KCM ScalingReplicaSet, 새 파드 Pulled 이벤트에 태그가 남음) |
| 원인이 된 결함 | 느린 메모리 누수(백엔드마다 같은 속도) | 새 잔액 스냅샷이 0.5초마다 전체 계좌를 이력에 붙이기만 하고 비우지 않음(분당 약 30MB, 요청이 아니라 시간에 비례하므로 인스턴스마다 같은 속도) |
| 전파 경로 | 메모리 고갈 → 프로세스 크래시 → 진행 중 요청 실패, 새 요청이 건강한 백엔드를 못 찾음 → 다시 뜬 뒤 같은 누수로 되풀이 | 힙 고갈 → 전체 GC 연속, OutOfMemoryError(acceptor 스레드까지 죽어 요청이 걸림) → readiness 실패로 엔드포인트에서 빠짐, liveness 실패로 kubelet 재시작 → 같은 릴리스가 다시 채움 |
| 사용자 증상 | 간헐적 오류 구간(약 20분씩 네 번), 일부 요청 거절 | 약 11~13분 주기로 잔액 조회 시간 초과, 500, 502, account 를 거치는 이체 502. 구간 사이에는 정상 |
| 원본의 탐지 경로 | SLO 소진 경보, 백엔드 재시작과 메모리 그래프 | lucida-next 오류율, 지연 이상, JVM 메모리(jvm.memory.used.bands 이상 대역이 account 에 있음), KCM Unhealthy, Killing |
| 완화와 복구 | 커밋 되돌림, 고친 릴리스 배포 | cleanup 이 이미지를 매니페스트 이미지로 되돌림(롤백) |

기전 일치: 원인(배포된 코드의 누수)에서 증상(같은 속도로 차서 주기적으로 죽고 다시 시작)까지 고리가 같다. 원본은 누수 코드를 밝히지 않아, 자바 서비스에서 가장 흔한 꼴(비우지 않는 메모리 캐시)을 한 기능 크기로 둔다.

바꾼 것(기전 고리 밖):
- 스택: Go 수집 워커 → Spring Boot JVM(Serial GC).
- 속도: 원본은 몇 시간에 걸친 느린 누수라 배포 뒤 시간이 지나 '최근 배포 없음'으로 의심받지 않았다. 테스트베드는 녹화 구간 안에 들어오도록 분 단위로 새고, 롤아웃 몇 분 뒤 첫 실패 구간이 온다. 그래서 '배포와 증상 사이가 멀어 배포를 의심하지 않는' 함정은 재현하지 않는다.
- 규모: 원본은 같은 백엔드 여러 대가 몇 분 차이로 함께 죽었다. 테스트베드 account 는 replicas 1 이라 한 인스턴스가 주기적으로 죽는다. '모든 인스턴스가 같은 속도로 찬다'는 성질은 누수가 요청이 아니라 시간에 비례하는 것으로 남는다.
- 크래시 방식: 원본은 프로세스 크래시, 테스트베드는 JVM 이 힙 고갈로 멈추고 liveness 가 재시작한다.

## 3. 숫자 근거 (2026-10-09, `scenario-stats.py`, 정식 + 후보 합계 47, 이 후보 전)

- 묶음: A 7, B 7, D 7(각 14%), C 6, G 6, F 3, L 3, E 2, H 2, I 1, **J 1**, M 1, O 1, K 0, N 0. J(결함 있는 새 버전 배포)는 현실 트리거 1위(Google 바이너리 배포 37%)인데 1개(F17-H, 산출물 결함)뿐이고 코드 결함 버전은 0이다. K, N 은 0 이지만 막힌 시도가 많다(rejected 목록, 장부 §5).
- 정답 위치: 외부 결제 의존 6, 주문 서비스 6(각 12%), 결제 경로 합계 10(21%, 금지). **은행 계좌 서비스 1**(F39-R 후보) → 2(4%).
- 서비스: 음식배달 10, 은행 12, 쇼핑몰 25. 은행 12 → 13.
- 왜 이 후보인가: J 가 현실 대비 가장 모자라고, 2026-10-09 결함 버전 이미지 규칙으로 처음 만들 수 있게 됐다. 음식배달이 1 적지만 누수 재시작 주기를 걸 서비스가 막힌다: order 는 loadgen 생성 입구라 재시작 중 entry_status 0 이 abort, restaurant 는 주문 여정의 메뉴 조회 입구라 표본이 사라짐(rejected 의 restaurant OOM 행과 같은 벽), dispatch 는 배달 서비스 2개와 order 503 증상이 겹침, payment 는 결제 경로 금지. banking account 는 진입점(nginx → api)이 따로 있어 account 가 죽어도 entry_status 는 502 다.

### 후보 목록 (3단계, 기존 목록이 아니라 기전 × 0 부품, 인프라 층에서 시작)

| # | 후보 | 층, 부품 | 원본 사례 | 결과 |
|---|---|---|---|---|
| 1 | account 새 릴리스의 메모리 누수, 주기적 OOM 재시작 | 앱, banking account(J) | Honeycomb 2019-11-06 공식 | **채택** |
| 2 | 새 릴리스의 정규식 역추적으로 CPU 포화 | 앱, banking api 또는 commerce cart(J) | Cloudflare 2019-07-02 공식 | 버림: rejected 에 같은 원본(막힌 사유 '앱 코드 변경'은 결함 이미지 규칙으로 풀렸으므로 다음 후보 재료) |
| 3 | 새 릴리스의 배칭이 깨져 요청당 질의가 수십 배(N+1) | 앱, food restaurant(J) | Discord 2020-06-17 공식 상태 페이지 | 버림: 원칙 9. restaurant 메뉴 질의가 PK 조회 수십 개가 되어도 180rps 이하에서 MySQL CPU 몇 % 라 피해 계산이 서지 않음 |
| 4 | 잠재 결함이 정당한 데이터 변경으로 발화 | 앱, banking transfer(J) | Fastly 2021-06-08 | 버림: 계기 두 개(배포와 데이터 변경)의 복합이고 원본 확인 전, 단일 계기 후보 1이 앞섬 |
| 5 | 새 버전 기동 지연으로 startupProbe 크래시 루프 | 인프라, 프로브와 재시작 정책 | PostHog 2025-10-28 공식 | 버림: rejected(롤아웃 실패류, maxSurge 25% 가 옛 파드를 남김) |
| 6 | 노드 이미지 GC 가 쓰는 이미지를 지워 새 파드 기동 실패 | 인프라, 이미지와 배포 | Harness 2025-10-28 | 버림: F17-H 의 원본과 같음 |
| 7 | 일부 레플리카만 새 버전(혼재 배포) | 인프라, 이미지와 배포 | Knight Capital 2012 SEC | 버림: 모든 서비스 replicas 1 이라 재현 불가 |
| 8 | 새 버전 Kafka 소비자가 특정 메시지에서 예외, 무한 재시도 | 앱, food notify(J) | PostHog 2026-07-23(2차 출처) | 버림: 원칙 1(2차 출처), 원칙 7(비동기 경로라 사용자 증상 없음) |
| 9 | 롤백한 옛 버전이 바뀐 설정과 충돌 | 인프라, 롤백 | Stripe 2019-07-10 공식 | 버림: rejected(잘못된 산출물, 옛 이미지 롤백) |

DB 가 정답인 후보는 0개, 부품 7종(account, api/cart, restaurant, transfer, 프로브, 이미지, notify), 앱과 인프라 두 층을 모두 냈다.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 롤아웃: `scripts/scenarios/profiles/k8s_image_executor.py` RELEASE_SCRIPT `run` — 109 docker 의 core-banking-account:1.3.0 을 tb-w2 containerd 에 올리고(`docker save | ssh nkia@<tb-w2> sudo ctr -n k8s.io images import -`) `kubectl set image deploy testbed-account account-service=core-banking-account:1.3.0`.
2. 배포: `core-banking/k8s/21-account-service.yaml:9`(replicas 1), `:18-19`(nodeSelector tb-w2), `:27-28`(image, imagePullPolicy Never), 전략은 기본값(maxSurge 25%, maxUnavailable 25%)이라 새 파드가 Ready 가 된 뒤 옛 파드가 내려간다(109 kubectl 2026-10-09: replicas 1, generation 18, RollingUpdate 25%/25%).
3. 결함: `scripts/scenarios/fault-images/f41-r/account-service.patch:68-74`(BalanceSnapshotCache.refresh — @Scheduled fixedDelay 500ms, `accountRepository.findAll()` 결과를 계좌별 ConcurrentLinkedDeque 에 addLast, 지우는 곳 없음), `:20-30`(AccountService.getAccount 가 latest(id) 로 답함). 매니페스트 버전은 `core-banking/account-service/src/main/java/com/corebanking/account/service/AccountService.java:34-39`(Oracle findById).
4. 힙: JVM 옵션은 OTel javaagent 뿐이라 힙은 컨테이너 한도(1Gi, `:88-94`)의 기본 25%. 119 VM `apm.agent.otel.java.jvm.memory.limit`: Eden 68MiB, Survivor 8MiB, Tenured 170MiB(Serial GC 풀 이름).
5. 프로브: readiness `/actuator/health`(DB 포함, timeout 3s, `:73-79`), liveness `/actuator/health/liveness`(15초, timeout 3s, 5회, `:81-87`).
6. 하류: api `AccountClient.requestTransfer`(`core-banking/api-service/src/main/java/com/corebanking/api/client/AccountClient.java:28-58`) 가 account 연결 실패를 재시도, 서킷 끝에 502. 잔액 조회는 nginx `location /api/accounts` → account 직행(`core-banking/k8s/02-configmaps.yaml:45-46`).

### 로컬 실측 (2026-10-09, 원칙 9 근거)

같은 패치를 얹은 account jar 를 104 에서 빌드해(`mvn -pl account-service -am package`) 컨테이너(eclipse-temurin 21-jre-alpine, x86_64, `--cpus 0.5 --memory 1g`, Serial GC 자동 선택)로 띄우고, 로컬 Oracle Free(gvenzl/oracle-free 23-slim)에 accounts 1,000행(시드와 같은 꼴, 한글 예금주 987행)을 넣어 잔액 조회 약 5rps 와 15초마다 liveness 를 걸었다.

| JVM 기동 후 | 관찰 |
|---|---|
| 52초 | 기동 완료 |
| 1.8분 | 전체 GC 뒤 살아 있는 힙 69MB |
| 3.4분 | 122MB(힙 247MB 로 확장) |
| 6분 | 247MB 에 닿아 전체 GC 가 1.1~1.5초씩 몇 초 간격으로 이어짐(조회는 성공, 느림) |
| 8.2분 | `java.lang.OutOfMemoryError: Java heap space`, Tomcat acceptor 스레드가 OOM 으로 죽음, 로그 'Servlet.service() for servlet [dispatcherServlet] ... threw exception [Request processing failed: java.lang.OutOfMemoryError: Java heap space]' |
| 8.2분 뒤 | 잔액 조회와 liveness 가 응답 없이 5초 시간 초과(조회 75건 중 0건 성공) |

실측 조건의 차이: 운영은 eclipse-temurin 17-jre, aarch64(Dockerfile, 109 노드)이고 로컬은 JDK 21, x86_64 다. 두 판 모두 이 크기(1 CPU 미만, 2GB 미만)에서 Serial GC 와 MaxRAMPercentage 25% 기본값을 고르므로 힙 크기(약 246MiB, 운영 jvm.memory.limit 과 같음)와 누수량(같은 객체를 쌓음)은 같고, 객체 헤더 크기와 GC 속도 차이로 주기가 몇 분 달라질 수 있다. 첫 실행에서 주기를 확인한다(§11).

누수 속도는 약 0.53MB/s(분당 약 32MB), 요청률과 무관하다(조회 5rps 를 걸었지만 스냅샷 갱신이 지배). 운영 account 는 OTel 에이전트를 달고 평시 Tenured 가 약 55MB(로컬 35~44MB)라 조금 더 빨리 찬다. kubelet 은 liveness 5회 실패(약 75초) 뒤 컨테이너를 재시작하고 JVM 기동에 약 50초가 걸린다(로컬 52초, 0.5 CPU). 한 주기: 기동 약 1분 + 차기까지 약 6분 + 전체 GC 연속 약 2~3분 + 멈춤, 재시작, 기동 약 2~3분 ≈ 11~13분.

## 5. 원인 규정 (원칙 5, 6)

- 근본(`root_cause.target_id`): `core-banking-account`(account-service 의 새 릴리스). 결함을 가진 곳이자 되돌려야 재발이 막히는 곳.
- 계기(`trigger_target_id`): 비움. 계기(릴리스 롤아웃)와 결함이 같은 곳이다. 호출자(api, nginx, loadgen)의 요청은 평시 그대로 정당하다(원칙 6).
- 부분 점수: `core-banking-api`, `banking-api`(증상이 드러나는 곳).
- 층위: 원본 사후 보고의 결론("배포된 커밋이 메모리를 새게 했고 되돌려 복구")과 같은 층위. 패치 속 코드 줄을 맞히라고 요구하지 않는다(원칙 5, 결함 버전 이미지 규칙 7). 관제 데이터로 낼 수 있는 결론: "account 를 1.3.0 으로 롤아웃한 직후부터 account 힙이 요청과 무관하게 직선으로 차고, 찰 때마다 OOM 과 재시작이 되풀이되며, 한도와 설정은 그대로다 → 새 버전의 메모리 누수, 롤백".

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 |
|---|---|
| 감지 | core-banking-account 서버 스팬 오류율과 지연(전체 GC 구간), api 오류율, account JVM 메모리 이상 대역(119 VM `jvm.memory.used.bands` 가 account 풀마다 있음), KCM Unhealthy(liveness 는 banking 에서 보존 기간 내 0건) |
| 피해 판정(러너) | 동반 부하 잔액 조회(step get) 실패율 ≥ 0.5 가 3틱. 평시 0 |
| RCA | 롤아웃 이벤트와 새 이미지 태그, JVM 힙 직선 상승과 한도 도달, OOM 로그, Killing, 재시작 뒤 같은 상승의 반복, 바뀌지 않은 한도와 설정 |

## 7. 관측 근거 표 (119 실조회, 2026-10-09 09:20~10:10 UTC)

| 증거 | 119 표와 칸 | 조회 | 평시 결과 |
|---|---|---|---|
| 계기: 롤아웃과 이미지 태그 | CH `kcm_events_local`(reason, object_name, body) | `namespace='rca-testbed-banking' AND reason IN ('Pulled','ScalingReplicaSet','Killing')` | Pulled 본문에 태그가 담긴다: 'Container image "core-banking-transfer:latest" already present on machine' 64건, ledger 2건. testbed-account 는 banking 이벤트 보존(2026-08-24~) 안에 롤아웃 이벤트가 없고 Unhealthy readiness 29건(오늘 06:56~07:12)뿐 |
| 계기: 새 ReplicaSet 스펙 | PG `kcm_resources_history`(kind, name, yaml, captured_at) | `namespace='rca-testbed-banking' AND name LIKE 'testbed-account%'` 종류별 집계와 "image":"core-banking-account:latest" 수 | ReplicaSet 33건, 파드 24건 전부 :latest(마지막 2026-08-11). ReplicaSet 수집은 오늘도 된다(food dispatch 2026-10-09 00:27). 앱 저장소의 버전 태그 ReplicaSet 은 0건(redis:7-alpine 만) |
| 근본: 힙 상승 | VM `apm.agent.otel.java.jvm.memory.used`, `jvm.memory.used_after_last_gc`, `jvm.memory.limit`(service_name, host_name=파드, jvm_memory_pool_name) | Tenured Gen 7일 min/max | used 75~82MB(직선 상승 증거는 이 지표), used_after_last_gc 55MB(7일 최소=최대: 전체 GC 때만 계단식으로 갱신되므로 '전체 GC 뒤에도 내려오지 않음' 증거로만 쓴다), limit 170MiB. 지표는 에이전트가 10초마다 내보내는 것이라 트레이스 표본과 무관 |
| 참고: 버전 표시 | VM, CH 의 `service_version` | account 지표, 로그 | 1.0.0. 릴리스는 소스만 바꾸고 pom 버전(core-banking/account-service/pom.xml)은 그대로라 1.3.0 에서도 1.0.0 이다. 버전 변화는 이미지 태그(KCM)로만 보인다 |
| 근본: OOM | CH `lucida_logs_local`(body, log_attributes exception.*) | `body ILIKE '%OutOfMemoryError%'` 로그 보존 전체(최소 시각 2026-10-02 08:00, 7일) | 0건. 같은 꼴의 Spring 오류 로그는 수집된다(transfer 'Unexpected error occurred in scheduled task' 182건, exception.type/message/stacktrace 가 log_attributes 에) |
| 전파: 재시작 | CH `kcm_events_local` | banking `body ILIKE '%Liveness probe failed%'` | 0건(banking 이벤트 보존 2026-08-24 03:46~). food order, dispatch 의 liveness 실패 이벤트는 수집됨 |
| 전파: api 실패 | CH `lucida_logs_local` | core-banking-api ERROR 7일 | 'Account service call failed for order null: 400 ...'(잔액 부족, 평시 4,827건)는 평시에도 있다. 고장 때는 같은 문장 뒤가 'I/O error ... Connection refused' 나 'Read timed out' 이 된다 |
| 보조: 새 질의 | CH `dpm_topsql_local`(engine oracle, sql_id, body 의 executions) | oracle 하루 20,005행 | 문장 본문은 없고 sql_id 와 수치만. 새 버전의 findAll 은 평시 없던 sql_id 로 분당 약 110회 실행될 것(첫 실행에서 확인) |

표본 데이터(트레이스 10%, APM 스팬 지표)만으로 증명하지 않는다. 결정 증거는 KCM 이벤트(전수), JVM 지표(에이전트 지표, 전수), 로그(전수)다.

## 8. 감별

- must_support: 롤아웃과 1.3.0 태그, Tenured 직선 상승과 한도 도달(재시작 뒤 반복), OOM 로그, Unhealthy 와 Killing, 실패 구간마다의 잔액 조회 실패와 api 로그, 거래 내역과 정산 정상.
- must_rule_out: 한도나 힙 설정 축소(F05-R, F09-H: resources, JAVA_TOOL_OPTIONS 와 jvm.memory.limit 그대로), 컨테이너 OOMKill(재시작 사유가 liveness), 부하 증가(요청률 평탄, 힙이 시간에 비례), Oracle, DB 계정(F35-R, F01-P), transfer(F17-R, F17-H), account 하류 주소(F39-R).
- contrast_with: F09-H, F05-R, F39-R, F17-H.

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

- 강도는 릴리스 하나로 고정(approved-fixed-f41-r, 실행기 파라미터 = 이미지 태그). 누수 속도는 패치(0.5초 주기, 계좌 수)로 정해져 요청률, 시각과 무관하다.
- 동반 부하: load.north_south core-banking transfer-heavy-surge.js 5rps(잔액 35% → 초당 약 1.75건, 30초 창 약 50건), ramp 2m + hold 27m + ramp_down 15s. 피해를 만드는 데 필요한 것은 아니고 판정 표본용이다.
- success: balance_read_nonok_rate ≥ 0.5, 3틱(30초 갱신 → 75초). 실패 구간(멈춤, 재시작, 기동 약 2~3분)에서만 선다.
- min_hold 14m, timeout 28m, max_injection_duration 30m: 첫 실패 구간이 약 9~13분, 두 번째가 약 20~25분이라 두 번째 구간에서 판정하고 녹화 구간에 실패 구간 두 번이 들어간다(원본의 되풀이되는 오류 구간). 주기가 14분까지 길어져도 받는다.
- must_rule_out: achieved_rps < 1.25, transfer 파드 NotReady, Oracle 파드 NotReady(2틱).
- abort: entry_status == 0(2틱). account 가 멈춰도 nginx, api 는 살아 502 로 답한다.
- recovery: target_health 200, transfer Ready, 기준선 잔액 조회 실패율 < 0.05(2틱).
- cleanup: 이미지 원복, available(180초), 1.3.0 을 쓰는 파드가 없어진 뒤 tb-w2 containerd 에서 1.3.0 의 이름 참조와 ID 참조(run 이 109 docker 의 이미지 ID 를 상태 파일에 남김)를 모두 지우고 둘 다 없는지 확인. 목록 조회가 실패하면 '없음'으로 읽지 않고 실패한다.
- max_injection_duration 30m: 동반 부하 ramp 2m + hold 27m + ramp_down 15s(29m15s)를 덮는다.
- 노드 디스크: tb-w2 이미지 파일시스템 73.1%(2026-10-09). 릴리스는 기본 이미지와 같은 eclipse-temurin 기반이라 새로 쓰는 것은 앱 층뿐이다. preflight 가 80% 미만을 요구한다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Honeycomb 2019-11-06, 공식 링크)와 요소별 대응표, 기전 같음 (§2)
- [x] 장부 갱신(§4-1, §6), 묶음 J 2/48(4%), 정답 위치 은행 계좌 서비스 2/48(4%), 결제 경로 10/48(21%, 이 후보와 무관), 은행 13. 어느 축도 20% 미만 (§3)
- [x] 근본 원인 위치: 패치 `account-service.patch:68-74`, `:20-30`, 매니페스트 `21-account-service.yaml` 각 줄 (§4)
- [x] 근본 원인의 흔적 조회: VM jvm.memory.* (Tenured used 평시 7일 75~82MB, used_after_last_gc 55MB, limit 170MiB), CH kcm_events_local Pulled 본문의 이미지 태그, PG kcm_resources_history ReplicaSet image (§7)
- [x] 핵심 증거가 표본 데이터만이 아님: KCM 이벤트, JVM 에이전트 지표, 로그 전수 (§7)
- [x] 계기 흔적: 롤아웃 이벤트와 새 태그(인공 지연 없음, 누수는 실제 코드) (§7)
- [x] 정답이 관제 데이터로 낼 수 있는 결론: "새 버전 롤아웃 뒤 힙 직선 상승, 주기적 OOM 재시작, 한도 불변 → 새 버전 누수". 코드 줄 추론 불필요 (§5)
- [x] 정답지 세 칸(근본 core-banking-account, 계기 비움, 부분 api) (§5). class A(코드 결함). fault_pattern 은 P1~P7 어느 것도 메모리 누수에 맞지 않아 생략(새 P 번호를 만들지 않는 규칙)
- [x] 감지, 피해 판정, RCA 증거 구분 (§6)
- [x] 시나리오 id 흔적 없음: 패치 문자열(새 클래스 BalanceSnapshotCache, 설정 키 account.balance-snapshot.refresh-ms)에 id, fault, bug, chaos 없음, 태그 1.3.0, 실행기 인자와 상태 파일에 id 없음(테스트로 확인)
- [x] 피해 계산: 로컬 실측(§4) 누수 분당 약 32MB, 6분 뒤 전체 GC 연속, 8분 뒤 OOM 과 요청 멈춤. 서킷브레이커는 api 쪽이라 피해를 줄이지 않고 502 로 바꿀 뿐이다 (§9)

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- 배포 단계에서 `bash scripts/scenarios/fault-images/build.sh f41-r` 로 109 docker 에 core-banking-account:1.3.0 이 있어야 preflight 가 통과한다(빌드 전후 기본 이미지 ID 비교는 스크립트가 한다).
- KCM 이 새 파드의 'Pulled ... core-banking-account:1.3.0' 와 Killing 을 잡는지, kcm_resources_history 가 새 ReplicaSet 을 잡는지.
- OOM 순간 OTel 내보내기가 살아 있어 OOM 로그가 119 에 남는지. 없으면 must_support 3 을 JVM 지표와 KCM 재시작으로 좁힌다.
- 실제 주기(운영 account 는 에이전트와 평시 힙이 커서 로컬보다 빠를 수 있고, JDK 17 aarch64 라 GC 속도가 다를 수 있다)와 success 가 두 번째 실패 구간에서 섰는지. 주기가 크게 다르면 운영 하네스가 min_hold, timeout 을 조정한다.
- 인시던트가 실패 구간마다 생기는지(원칙 7).
- cleanup 뒤 tb-w2 containerd 에 1.3.0 의 이름 참조와 ID 참조(`sha256:<1.3.0 이미지 ID>`)가 모두 남지 않았는지(recovery 가 확인). CRI 는 이미지마다 이름과 별도로 ID 참조를 두고, 이름만 지우면 내용이 남는다(평가 지적, tb-w2 에 이름 없는 ID 참조 3개가 이미 있다: core-banking-ledger:pre-bcf3524, core-banking-transfer:pre-outbox-purge-20260807 의 ID 등, 이 시나리오와 무관한 옛 잔여물).
