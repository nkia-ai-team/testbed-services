---
title: F63-R 설계 시트 (banking api-service 를 요청마다 스레드 하나짜리 풀을 만들고 닫지 않는 릴리스로 롤아웃)
status: Draft
owner: project
last_reviewed: 2026-10-11
tags:
  - scenario
  - design
  - release
summary: banking api-service 를 결함 있는 새 릴리스(core-banking-api:1.5.0, fault-images/f63-r 패치로 만든 별도 태그)로 롤아웃하면, 이체 접수와 거래 내역 조회에 새로 더한 처리 마감 시간이 요청마다 스레드 하나짜리 풀을 만들고 닫지 않아 멈춘 스레드가 요청 수만큼 쌓이고, 부하에서 약 8분이면 api 컨테이너가 메모리 한도 1GiB 를 채워 멈췄다 OOM 으로 죽고 재시작을 되풀이하며, 그 구간마다 banking 게이트웨이의 이체와 거래 내역이 502 로 실패하는 시나리오. 원본은 Intercom 2025-04-24 의 새 카운터 구현이 요청마다 스레드를 만들고 치우지 않은 스레드 누수.
---

# F63-R 설계 시트

## 1. 요약

banking api-service 의 새 릴리스 1.5.0 은 이체 접수(POST /api/transfers → account)와 거래 내역 조회(GET /api/transfers → transfer)에 전체 처리 마감 시간 5초를 더한다. 하류 호출은 읽기 10초에 재시도 3회라 30초 넘게 요청을 붙잡을 수 있어서다. 마감 처리는 요청마다 `Executors.newFixedThreadPool(1)` 로 풀을 만들어 하류 호출을 맡기고 `Future.get(5초)` 로 기다리는데, 풀을 닫지 않는다. 고정 크기 풀의 작업 스레드는 다음 작업을 기다리며 멈춰 있는 비데몬 스레드이고 자기 풀을 붙잡고 있어서, 스레드도 풀도 회수되지 않는다. 요청 하나마다 멈춘 스레드가 하나씩 남는다.

스레드마다 접근한 스택 페이지와 커널 스택(16KiB)이 컨테이너 메모리로 잡혀 약 130KiB 씩 쌓인다. 동반 부하에서 api 를 거치는 요청이 초당 약 11건이면 컨테이너 메모리가 평시 약 290MiB 에서 한도 1GiB 까지 약 8분에 오른다. 힙은 그대로다. 한도에 닿으면 회수할 수 없는 메모리 압박으로 요청과 헬스 확인이 잠깐(로컬 약 30초) 느려지다 커널의 memory cgroup OOM 이 java 를 죽이고, kubelet 이 같은 파드에서 컨테이너를 바로 다시 띄운다(앞 크래시가 10분보다 앞이면 재시작 대기 없음, 10분 안이면 10초, 109 실측 기동에서 Ready 까지 26~29초). 멈춤, 재시작, 기동 동안 banking nginx 가 api 로 보내는 이체와 거래 내역은 502 다. 새 컨테이너도 같은 릴리스라 약 10분마다 되풀이한다. 잔액 조회(nginx → account 직행), commerce 정산(transfer 직행), account, transfer, Oracle 은 멀쩡하다.

비유: 창구(api)에 새로 온 규칙이 "손님마다 전담 직원 한 명을 새로 불러 일을 맡기고 결과만 받는다"인데, 일이 끝난 전담 직원을 돌려보내지 않는다. 직원 대기실(컨테이너 메모리)이 손님 수만큼 차서, 몇 분 뒤 창구 건물이 꽉 차 문을 닫고(OOM) 다시 연다. 다시 열어도 같은 규칙이라 또 찬다. 고칠 곳은 대기실 크기가 아니라 새 규칙(api 릴리스)이다.

## 2. 원본 사례

- **Intercom(현 Fin), 2025-04-24, 2025-04-25** (공식 상태 페이지 사후 보고, "Intercom teammate web app is down", 미국 호스팅 지역): https://www.finstatus.com/us-hosting/incidents/01JSPDFV117N6Z8VPG2W98BMBQ/write-up (옛 주소 intercomstatus.com 에서 옮겨짐)
- 2025-04-24 20:09~20:17 UTC 와 04-25 11:57~12:07 UTC 에 Inbox 를 포함한 팀원용 웹 앱이 내려갔다. 고객은 "general slowness" 와 오류 페이지를 봤다. 4월 22일부터 Inbox 카운터(맞춤 보기, 폴더의 대화 수를 실시간으로 보여 줌)를 갱신하는 새 방식을 롤아웃했고 24일 모든 고객에게 켰다. 원인은 스레드 누수다: "The new counters implementation was unintentionally creating a new set of threads on each request", 그 스레드가 치워지지 않아 "memory usage of the Rails processes on the web serving fleet started to increase due to a thread leak bug", "Most web serving hosts ran out of memory in a very short period of time." 평소에는 정기 배포가 스레드 누수와 메모리 증가를 초기화했는데 마지막 배포 뒤 2시간이 넘게 지나 있었다. 첫 장애는 지연 기반 증설과 헬스 체크 실패에 따른 호스트 교체로 회복됐다. 30분 자동 배포를 걸었다가 끈 뒤, 배포 시스템의 다른 문제로 1시간 넘게 배포가 막히자 메모리가 100% 에 닿아 다시 장애가 났다. 메모리를 두 배로 늘리고 자동 배포를 다시 켠 뒤 스레드 누수 수정을 내보내 끝났다.
- 자료 문서 M12 표에 이 사례를 더했다(출처가 말한 사실만).

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | Inbox 카운터 새 구현의 롤아웃(4/22~, 4/24 전면) | api-service 를 릴리스 core-banking-api:1.5.0 으로 롤아웃(KCM ScalingReplicaSet, 새 파드 Pulled 이벤트에 태그가 남음) |
| 원인이 된 결함 | 요청마다 스레드 묶음을 새로 만들고 치우지 않음(스레드 누수) | 요청마다 스레드 하나짜리 풀을 새로 만들고 닫지 않음(멈춘 비데몬 스레드가 요청마다 하나씩 남음) |
| 전파 경로 | 요청 수에 비례해 Rails 프로세스 메모리 상승 → 웹 서버 메모리 고갈 | 요청 수에 비례해 스레드와 컨테이너 메모리 상승 → 컨테이너 메모리 한도 → 멈춤, OOM 종료, 재시작 → 같은 릴리스라 되풀이 |
| 사용자 증상 | 전반적인 느림과 오류 페이지, 앱 다운(각 8~10분) | 이체와 거래 내역이 약 30초 느려지다 약 30~45초 502, 약 10분마다 되풀이 |
| 원본의 탐지 경로 | 메모리 사용률(사후 조치로 메모리 경보 추가), 헬스 체크 실패, 지연 | lucida-next 의 api JVM 스레드 수 이상(비데몬 대기 스레드 평시 최대 8 → 수천), tb-w2 커널 memcg OOM 로그, KCM 재시작과 Unhealthy, 게이트웨이 뒤 이체 실패 |
| 완화와 복구 | 증설, 호스트 교체, 정기 재배포(누수 초기화), 메모리 두 배, 누수 수정 배포 | 재시작이 잠시씩 살림(원본의 호스트 교체, 재배포와 같은 효과), cleanup 이 매니페스트 이미지로 되돌림(롤백) |

기전 일치: 원인(새 버전이 요청마다 스레드를 만들고 치우지 않음)에서 증상(요청 수에 비례한 메모리 상승 → 메모리 고갈 → 느림과 오류, 재시작이 잠깐 살리고 다시 참)까지 고리가 같다. 바꾼 것(고리 밖): Rails 프로세스 대신 Spring Boot JVM, 웹 서버 여러 대 대신 api 파드 하나, 호스트 메모리 대신 컨테이너 메모리 한도. 원본의 정기 재배포 대신 kubelet 의 컨테이너 재시작이 누수를 초기화한다. 원본 후반부(배포 시스템의 다른 문제)는 재현하지 않는다.

## 3. 숫자 근거 (2026-10-11, `scenario-stats.py`, 정식 + 후보 합계 82, 이 후보 전)

- 묶음: J 13(15.9%) 이 가장 많고 P 10(12.2%), D 8, G 8, A 7, B 7, L 7, C 6, E 4, F 3, H 2, K 2, Q 2, I 1, M 1, O 1, N 0. 어느 묶음도 20% 에 닿지 않는다. 이 후보 J 13 → 14(83 중 16.9%). 피해 꼴로 A(컨테이너 메모리 한도)를 덧붙여 J+A(A 로 세도 7 → 8, 9.6%).
- 정답 위치: 주문 서비스 8(9.8%)이 가장 많다. **은행 API 2 → 3(3.6%)**, §2-1 에 이미 있는 말. 결제 경로 12/83(14.5%, 이 후보와 무관).
- 서비스: 쇼핑몰 28, 은행 27, 음식배달 27. 은행 27 → 28. 쇼핑몰이 가장 많아 쇼핑몰은 정답 위치 0 인 부품(게이트웨이, 알림, Kafka, nginx)만 낼 수 있다.
- 왜 이 후보인가: 현실 트리거 1위(Google 바이너리 배포 37%)인 결함 있는 새 버전이고, 앞선 시나리오에 없던 원본과 결함 꼴(스레드 누수)이다. 같은 기전을 음식배달(27)에 먼저 대 봤으나 막혔다(아래 후보 2, 3). 은행은 진입점이 nginx 라 api 가 멈추거나 죽어도 nginx 가 502 로 답해 entry_status 가 0 이 되지 않는다. 정답 위치 은행 API 는 3.6% 로 적다.

### 후보 목록 (3단계, 기존 목록이 아니라 실제 기전 × 부품, 인프라 층에서 시작)

| # | 후보 | 층, 부품 | 원본 사례 | 결과 |
|---|---|---|---|---|
| 1 | banking api 새 릴리스가 요청마다 스레드 풀을 만들고 닫지 않아 스레드가 쌓여 컨테이너 메모리 한도에서 OOM 재시작 반복 | 앱, banking api(J+A, 은행 API 2) | Intercom 2025-04-24 공식 사후 보고 | **채택** |
| 2 | 같은 스레드 누수 릴리스를 food order, restaurant, dispatch 에 | 앱, food 입구 서비스(주문 8, 가게 3, 배달 5) | 같은 원본 | 버림: 컨트롤러 필수 중단 조건. 세 서비스는 loadgen 이 NodePort 로 바로 부르는 입구라 한도에서 멈추는 약 30초와 재시작 동안 연결 불가(entry_status 0). rejected 의 'food restaurant 메모리 누수 OOM 재시작' 행과 같은 벽 |
| 3 | 같은 릴리스를 food payment 에 | 앱, food payment(결제 경로 14.5%) | 같은 원본 | 버림: 컨트롤러 필수 중단 조건. order 가 트랜잭션 안에서 payment 를 기다려(F19-P 꼴) payment 가 멈추면 order 풀이 묶여 entry 0. rejected 의 'food payment 메모리 누수' 행과 같은 벽 |
| 4 | 같은 릴리스를 banking account 에 | 앱, banking account(은행 계좌 5) | 같은 원본 | 버림: 카탈로그 §1. account 는 이미 릴리스 재시작 고리가 둘(F41-R 힙 누수, F41-H liveness 404)이고 증상(잔액 조회 502)과 대상이 같아 차오르는 자원만 다른 복제에 가깝다. 정답 위치도 은행 API(2)보다 많다 |
| 5 | 같은 릴리스를 banking transfer 에 | 앱, banking transfer(은행 이체 6) | 같은 원본 | 순위 뒤: 정답 위치가 은행 API 의 두 배이고, commerce 정산이 transfer 를 바로 불러 transfer 가 멈추면 commerce checkout 이 묶임(commerce 입구 지연, 증상이 F17-R, F17-H 와 겹침) |
| 6 | 배포 중 전체 캐시 비우기로 재적재 폭주, 스레드 굶주림 | 앱, commerce cart Redis(캐시 1) | Shepherd 2026-02-26 공식 | 버림: rejected 197행과 같은 원본(원칙 2, 9: 쇼핑몰 최다인데 캐시 1, cart DB 풀이 폴백 흡수) |
| 7 | 스레드 풀 자동 설정이 최소값으로 줄여 처리 지연 | 앱, commerce gateway(게이트웨이 0) | Nebius S3 2025-05-05 공식 | 버림: rejected 의 Nebius 행과 같은 원본(원칙 9: 180rps 상한, ms 지연이라 동시성이 스레드 수 미만) |
| 8 | 결함 배포의 중복 메시지 처리로 DB 연결 과부하 | 앱, banking ledger(원장 1) | Signhost 2025-09-02 공식 | 버림: rejected 의 Signhost 행과 같은 원본(원칙 1, 7: 원장은 사용자 경로 밖 비동기) |
| 9 | 배포가 앱 DB 사용자를 바꿔 풀러 파일 기술자 한도 초과 | DB 연결 계층 | Adapty 2025-12-05 공식 | 버림: rejected 의 Adapty 행과 같은 원본(연결 풀러 없음) |
| 10 | 같은 스레드 누수 릴리스를 commerce gateway 에 | 앱, commerce gateway(게이트웨이 0) | Intercom 2025-04-24 | 버림: 컨트롤러 필수 중단 조건. commerce 부하가 testbed-gateway 로 바로 들어가 gateway 가 멈추거나 재시작하면 k6 상태 0(rejected 263행의 gateway 메모리 누수와 같은 벽) |

부품 8종(banking api, food order·restaurant·dispatch, food payment, banking account, banking transfer, commerce cart Redis, commerce gateway, banking ledger, DB 연결 계층), DB 가 정답인 후보 1개(9). 지휘 세션이 넘긴 이번 실행의 막힌 후보, rejected, 장부 §4-1, §5 와 같은 원본, 같은 주입은 1 에 없다(Intercom 2025-04-24 는 처음 쓰는 원본이고, 스레드 누수는 처음 쓰는 결함 꼴이다. api 릴리스 태그 1.5.0 은 F47-R 1.4.0 과 다르다). 웹 검색 2회(스레드 고갈 배포 사후 보고, 파일 기술자 누수 사후 보고)로 원본을 찾았다.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 롤아웃: `scripts/scenarios/profiles/k8s_image_executor.py` RELEASE_SCRIPT `run` — 109 docker 의 core-banking-api:1.5.0 을 tb-w2 containerd 에 올리고 `kubectl set image deploy testbed-api api-service=core-banking-api:1.5.0`. 노드는 Deployment 의 nodeSelector 에서 읽는다(F41-R, F47-R 과 같은 스크립트, 계약 `CONTRACTS["F63-R"]` 만 더함).
2. 배포: `core-banking/k8s/20-api-service.yaml:27-28`(image, imagePullPolicy Never), `:57-76`(startupProbe /actuator/health 5초 간격 30회, readinessProbe 10초 간격 시간 제한 3초 3회, livenessProbe 15초 간격 시간 제한 3초 5회), `:77-83`(requests 512Mi, limits cpu 500m memory 1Gi). JVM 옵션은 OTel 에이전트뿐(`JAVA_TOOL_OPTIONS`), 힙 상한은 기본값(컨테이너 한도의 1/4, 256MiB)이다(119 VM jvm.memory.limit heap 풀 합 259,522,560 바이트). 전략은 기본값(maxSurge 25%)이라 새 파드가 Ready 가 된 뒤 옛 파드가 내려간다(109 kubectl 2026-10-11).
3. 결함: `scripts/scenarios/fault-images/f63-r/api-service.patch:45-63`(ApiService.withinDeadline — 요청마다 `Executors.newFixedThreadPool(1)`, `submit`, `Future.get(5000ms)`, 시간 초과면 cancel 과 504, 풀은 닫지 않음), `:37`(submitTransfer 가 account 로의 이체 요청을 감쌈), `:42`(listTransfers 가 transfer 로의 거래 내역 조회를 감쌈). 매니페스트 버전은 `core-banking/api-service/src/main/java/com/corebanking/api/service/ApiService.java:28-42`(요청 스레드에서 바로 하류 호출).
4. 누수: `ThreadPoolExecutor` 의 작업 스레드는 기본 스레드 팩토리로 만든 비데몬 스레드(이름 pool-N-thread-1)이고 작업 뒤 큐에서 다음 작업을 기다리며 멈춘다(WAITING). 스레드가 풀을 붙잡아 풀도 회수되지 않는다. `newSingleThreadExecutor` 는 감싼 객체가 회수될 때 스스로 닫혀 누수가 되지 않으므로(로컬 실측: 16,000번 만들어도 스레드 약 1,960 에서 멈춤) 고정 크기 풀을 쓴다. 테스트(`test_k8s_image_release_mode_rolls_banking_api_thread_leak_release`)가 이 꼴을 고정한다.
5. 메모리: 스레드마다 접근한 사용자 스택 페이지와 커널 스택 16KiB(cgroup memory.stat kernel_stack: 운영 파드 48 스레드에 784KiB)가 컨테이너 메모리로 잡힌다. 로컬 실측에서 스레드당 약 130KiB(아래).
6. 피해 쪽: banking nginx 가 `/api/transfers` 를 api 로, `/api/accounts` 를 account 로 보낸다(`core-banking/k8s/02-configmaps.yaml` nginx-conf). api 가 멈추면 readiness 시간 초과로 엔드포인트에서 빠지고, OOM 으로 죽으면 걸려 있던 연결이 끊겨 nginx 가 502, 재시작 대기와 기동 동안 엔드포인트가 비어 502 다.
7. 영향 밖: 잔액 조회와 계좌 목록(nginx → account), commerce 정산(commerce payment → `BANKING_TRANSFER_URL` testbed-transfer 직행, `commerce/k8s/02-configmaps.yaml:46`), account, transfer, Oracle.

### 로컬 실측 (2026-10-11, 원칙 9 근거)

origin/main core-banking 에 패치를 얹어 `./mvnw -o -pl api-service -am package` 로 jar 를 만들고, eclipse-temurin 17-jre(x86_64) 컨테이너에 OTel 자바 에이전트(내보내기 끔)를 붙여 `--cpus 0.5 -m 1g --memory-swap 1g`(운영 한도와 같음)로 띄웠다. 하류(account, transfer)는 5~30ms 지연으로 200 을 주는 흉내 서버. 열린 고리 부하 초당 11건(이체 POST 73%, 거래 내역 GET 27%, 운영 동반 부하의 api 몫과 같은 비율), 60초 제한, 5초마다 /actuator/health(3초 제한).

| 시각(부하 시작 뒤) | 스레드 | 컨테이너 메모리 | 응답 |
|---|---|---|---|
| 0 | 32 | 287MiB | 전부 200, 최장 1.2초(첫 요청 JIT), 그 뒤 0.2초 이하 |
| 4분 18초 | 2,875 | 733MiB | 전부 200 |
| 8분 20초 | 5,544 | 1,016MiB | 전부 200 |
| 9분~9분 40초 | 약 6,000 | 1,019MiB(한도) | 응답 최장 22.9초, health 3초 시간 초과 4회 |
| 9분 40초 | - | - | OOMKilled(종료 137, memory.events oom_kill 1). 걸려 있던 요청은 연결 끊김 |

- 스레드 증가는 초당 약 11개(요청 수와 같음), 메모리는 스레드당 약 130KiB(초반 JIT 구간 포함 평균). 부하 시작부터 한도 도달까지 8분 20초, OOM 종료까지 9분 40초. 그 전 약 9분 동안 실패 0건(5,940건 전부 200).
- 처음 시험(같은 jar, 초당 약 22건)도 한도에서 약 2분 느려진 뒤 OOMKilled(137) 로 끝났다. 한도에서 바로 죽지 않고 파일 페이지를 회수하며 버티는 구간이 30초~2분이다.
- 운영과의 차이: 운영은 eclipse-temurin 17-jre, aarch64(tb-w2, 페이지 4KiB 확인), 에이전트가 119 로 내보낸다. 운영 api 파드 cgroup 실측(2026-10-11): memory.current 289MiB, anon 286MiB, kernel_stack 784KiB, 스레드 48. 커널 스택은 arm64 도 16KiB 다. 운영에서는 멈춘 구간에 readiness(3회 × 10초)가 실패해 엔드포인트에서 빠지고 liveness(5회 × 15초)보다 OOM 이 먼저 올 가능성이 크다. 어느 쪽이든 재시작이다.

## 5. 원인 규정 (원칙 5, 6)

- 근본(`root_cause.target_id`): `core-banking-api`(api-service 의 새 릴리스). 결함을 가진 곳이자 되돌려야 재발이 막히는 곳.
- 계기(`trigger_target_id`): 비움. 계기(릴리스 롤아웃)와 결함이 같은 곳이다. 사용자와 loadgen 의 요청은 평시와 같다(원칙 6). 컨테이너 한도와 JVM 옵션도 그대로다.
- 부분 점수: `testbed-nginx`, `banking-nginx`(502 를 낸 증상 위치). 노드(tb-w2)는 부분 점수에 넣지 않는다: 노드 메모리는 여유가 있고 한도에 닿은 것은 api 컨테이너 cgroup 하나다.
- 층위: 원본 사후 보고의 결론("thread leak bug" 가 든 새 카운터 구현, 누수 수정 배포로 끝남)과 같은 층위. 패치 속 코드 줄을 맞히라고 요구하지 않는다(원칙 5, 결함 버전 이미지 규칙 7). 관제 데이터로 낼 수 있는 결론: "api 를 1.5.0 으로 롤아웃한 뒤 api 의 비데몬 대기 스레드가 요청 수에 비례해 수천으로 늘고(힙은 그대로) api 컨테이너가 메모리 한도에서 OOM 으로 죽기를 되풀이한다 → api 새 버전의 스레드 누수, 롤백".

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 |
|---|---|
| 감지 | core-banking-api JVM 스레드 수 이상(jvm.thread.count.bands 가 있음), api 서버 스팬 지연(멈춘 구간), api 스팬과 로그가 끊겼다 다시 생김(service.instance.id 바뀜), tb-w2 커널 OOM 로그, KCM Unhealthy(readiness, liveness 시간 초과)와 재시작 |
| 피해 판정(러너) | 동반 부하 이체 5xx 비율 ≥ 0.5 가 1틱(구간이 최대 틱 간격보다 김, 30초 창 하나가 이체 약 240건). 평시 0. 기록: 잔액 조회 비정상 비율(0 근처여야 함) |
| RCA | 롤아웃 이벤트와 새 이미지 태그, api 비데몬 대기 스레드의 요청 비례 직선 상승과 재시작마다 초기화, 힙과 데몬 스레드는 그대로, api cgroup OOM 커널 보고(kernel_stack 수십 MB), 바뀌지 않은 한도와 JVM 옵션, account·transfer·Oracle 정상 |

## 7. 관측 근거 표 (119 실조회, 2026-10-11 02:00~03:30 UTC)

| 증거 | 119 표와 칸 | 조회 | 평시 결과 |
|---|---|---|---|
| 계기: 롤아웃과 이미지 태그 | CH `kcm_events_local`(reason, object_name, body) | `namespace='rca-testbed-banking' AND object_name LIKE 'testbed-api%'` reason 별 | Unhealthy 12(startup 'connection refused'), ScalingReplicaSet 8, Created·Killing·Pulled·Scheduled·Started·SuccessfulCreate·SuccessfulDelete 각 4(2026-10-09 13:52~14:08). Pulled 본문에 태그가 담김('Container image "core-banking-api:latest" already present on machine'). api 롤아웃과 시작 프로브 실패가 수집된다 |
| 근본: 스레드 누수 | VM `apm.agent.otel.java.jvm.thread.count`(service_name, jvm_thread_daemon, jvm_thread_state) | `max_over_time(sum(...{service_name="core-banking-api",jvm_thread_daemon="false"})[7d:5m])` | 8(7일 최대). 같은 질의를 모든 서비스에 하면 최대 12(food notify). 지금 값: 비데몬 runnable 1, timed_waiting 2, waiting 1, 데몬 36(전체 7일 중앙값 47, 데몬 중앙값 43). 에이전트가 10초마다 내보내는 런타임 지표라 트레이스 표본과 무관. 고장 때 비데몬 waiting 이 요청 수만큼(분당 약 660) 직선으로 오른다 |
| 감별: 힙은 그대로 | VM `apm.agent.otel.java.jvm.memory.used{jvm_memory_type="heap"}`, `jvm.memory.limit` | api 7일 | heap 중앙값 74MB, 최대 127MB, 한도(풀 합) 259,522,560 바이트(기본값, 컨테이너 한도의 1/4). 로컬 실측에서 스레드 5,500 개일 때도 힙은 이 범위(누수는 스택, 커널 스택이라 힙 밖) |
| 근본: 컨테이너 OOM | CH `syslog_local`(hostname, app_name, message) | `hostname='tb-w2' AND app_name='kernel'` | tb-w2 커널 메시지 858건(2026-08-16~). memcg OOM 보고가 통째로 수집됨: 2026-09-28 02:31 Oracle 파드 cgroup 'memory: usage 4194304kB, limit 4194304kB', 'Memory cgroup stats for /kubepods.slice/...pod<UID>.slice', 'kernel_stack 1851392', 'Tasks state' 프로세스 표. tb-w3 은 'Memory cgroup out of memory: Killed process 433032 (mysqld) ...' 까지 수집(2026-10-08). 고장 때는 api 파드 cgroup 에 limit 1048576kB, kernel_stack 수십 MB(스레드 수 × 16KiB), Killed process (java) |
| 전파: 재시작 | CH `kcm_events_local` | 같은 파드 이름에 Scheduled 없이 Pulled, Created, Started 반복, 앞서 Unhealthy('Readiness probe failed', 'Liveness probe failed' 시간 초과) | api 는 평시 재시작 0. 컨테이너 재시작 이벤트는 다른 시나리오(food order, dispatch liveness)에서 수집됨 |
| 전파: 재시작 흔적(로그) | CH `lucida_logs_local`(host_name, resource_attributes['service.instance.id']) | core-banking-api 7일 | 195,627행, service.instance.id 3개, host_name(파드) 3개. JVM 이 다시 뜰 때마다 같은 파드 이름에 새 service.instance.id 가 생긴다 |
| 참고: 이체 로그 | CH `lucida_logs_local` | core-banking-api 1시간 body 별 | 'Submitting transfer: ...'(분당 약 20), 'Account service call failed ... 400 ... Insufficient balance' 와 서킷 열림 로그(평시 잔액 부족 거절). 새 릴리스는 정상 경로에 로그를 더하지 않는다(시간 초과 때만 'transfer submit did not finish within 5000ms' WARN, 로컬 실측 0건) |
| 피해: 영향 밖 | VM `apm.agent.otel.java.percentile95`, KCM | account, transfer | 고장 때도 평시와 같아야 함(account p95 평시 7.7ms) |

표본 데이터(트레이스 10%, APM 스팬 지표)만으로 증명하지 않는다. 결정 증거는 KCM 이벤트(전수), JVM 런타임 지표(스레드, 힙), syslog 커널 메시지(전수), 로그 자원 속성이다.

## 8. 감별

- must_support: api 롤아웃과 1.5.0 태그, api 비데몬 대기 스레드의 요청 비례 상승(평시 최대 8 → 수천)과 재시작마다 초기화, 힙과 데몬 스레드는 평시 그대로, tb-w2 커널의 api cgroup OOM 보고, KCM 재시작과 시간 초과 Unhealthy, 이체와 거래 내역 실패가 멈춤과 재시작 구간에 몰림, account·transfer·Oracle 정상, 한도와 JVM 옵션 불변.
- must_rule_out: account 결함(F41-R, F41-H: 잔액 조회 정상), transfer 나 DB(F17-H, F42-R, F01-P, F35-R: transfer, Oracle Ready, commerce 정산 정상), 하류 요청 폭증(F47-R: account 평시 그대로, 늘어난 것은 비데몬 대기 스레드), 한도나 힙 설정 축소(F05-R, F09-H), 노드 메모리 압박(F05-P), 이름 해석, 주소 오설정(F37-R, F39-R).
- contrast_with: F41-R, F41-H, F47-R, F05-R, F05-P.
- F41-R, F41-H 와는 '릴리스 뒤 재시작 고리' 겉 증상이 같다(카탈로그 관계 H 에 가까움). 가르는 관측 근거: 재시작하는 파드(account 대 api), 실패하는 사용자 경로(잔액 조회 대 이체·거래 내역), 차오르는 자원(Tenured 힙 대 비데몬 스레드와 컨테이너 메모리), 죽는 방식(JVM OutOfMemoryError 뒤 liveness, liveness 404 대 커널 memcg OOM).
- F47-R 과는 같은 api 릴리스 계열이고 api 스레드가 는다는 점이 같다. 가르는 근거: F47-R 은 데몬 스레드(runAsync 작업 스레드)가 늘고 account CPU 가 한도에 붙으며 api 는 살아 있다. F63-R 은 비데몬 대기 스레드가 늘고 하류는 평시 그대로이며 api 자신이 OOM 으로 죽는다.

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

- 강도는 릴리스 하나로 고정(approved-fixed-f63-r, 실행기 파라미터 = 이미지 태그). 누수 속도는 api 를 거치는 요청 수에 비례한다(요청 1건 = 스레드 1개).
- 부하 계산: 동반 부하 load.north_south core-banking transfer-heavy-surge.js 20rps(이체 40% 약 8건/초, 거래 내역 15% 약 3건/초 → api 약 11건/초, 잔액 35%와 계좌 목록 10%는 account 직행). 상주 기준선(1~4 iter/s 의 35%)이 api 에 약 0.4~1.4건/초를 더한다. 한도까지 필요한 스레드 = (1024 − 290)MiB ÷ 약 130KiB ≈ 5,800개. ramp 2분(평균 약 5.5건/초, 약 660개) 뒤 초당 약 11.5개면 롤아웃 뒤 약 9~10분에 한도, 짧은 멈춤(로컬 30초~2분, 운영 파드 cgroup 은 회수할 파일 페이지가 160KiB 뿐이라 더 짧기 쉬움) 뒤 OOM. kubelet 은 앞 크래시 뒤 10분(2 × 최대 대기 5분)을 넘겨야 재시작 대기를 초기화하므로 첫 크래시와 주기가 10분보다 긴 뒤 크래시는 바로 재시작, 주기가 10분보다 짧으면 두 번째 크래시는 10초 대기(실패 구간이 늘어 판정엔 유리)이고, 기동에서 Ready 까지 26~29초(109 실측: testbed-api-bdd59894b-ghfxw Started 14:07:41 → Ready 14:08:07, 앞선 롤아웃 Started 13:52:27 → 옛 파드 Killing 13:52:56) 뒤 다시 약 8분에 한도. 한 주기 약 10분. 운영 api 의 스레드 스택 실제 RSS 는 12~176KiB(평균 약 97KiB, 평가 실측 smaps)라 스레드 RSS 97~130KiB 면 OOM 은 롤아웃 뒤 약 9.7~12.8분, 실패 구간은 약 10~13분과 20~25.5분 근처라 min_hold 9m 과 timeout 28m 사이에 둘 다 든다. 주기가 8분이면 첫 구간은 min_hold 앞이지만 둘째, 셋째가 timeout 안이고, 14분이면 둘째가 28m 을 넘는다(첫 실행에서 실제 주기 기록).
- 거래 내역 3건/초는 Oracle 전수 스캔 약 0.64 CPU(F42-R, F47-R 과 같은 동반 부하)라 Oracle(2 CPU)은 여유가 있다. 이체 8건/초는 F47-R 로컬 실측에서 account, transfer 가 여유였다.
- success: transfer_5xx_rate ≥ 0.5, 1틱(선례 F20-R). 러너는 틱을 끝낸 뒤 15초를 쉬므로(rca-scenario-runner `backend/app/runner.py`) 실제 틱 간격은 15~25초(109 ticks.jsonl F40-R, F17-R, F14-P 실행 평균 약 18초)다. 이 관측은 갱신 간격이 30초라 연속 수는 참 표본이 앞선 표시보다 30초 이상 뒤일 때만 오른다(`adaptive.py` `_updated_streaks`). 그래서 2틱은 첫 참 틱과 약 38초 뒤 참 틱이 모두 구간 안에 있어야 하고, 평가 시뮬레이션으로 참 구간 43초면 33%, 30초면 0% 다. 비율은 마지막 k6 포인트 기준 30초 슬라이딩 창(`loadgen_monitor.py`)이라 0.5 이상인 구간은 [OOM 순간 − (S − 15초), 엔드포인트 복귀 + 15초](S 는 멈춤 중 걸렸다 OOM 순간 한꺼번에 502 가 되는 요청의 길이)이고, 운영 cgroup 은 회수할 파일 페이지가 160KiB 뿐이라 S 가 짧기 쉬워 약 30~45초다. 1틱 근거: ① 엔드포인트 복귀까지 약 28~31초(109 KCM Started → 옛 파드 Killing 26~29초 + 재기동 1~2초)만으로 L ≥ 28초라 최대 틱 간격 25초보다 길어 어느 위상이든 한 틱은 든다. ② 30초 창 하나가 동반 부하 이체 약 240건(8건/초 × 30초)의 집계라 표본 한 개짜리 잡음이 아니고, 평시 이 비율은 0 이다. ③ min_hold 9m 이 롤아웃 직후 과도 구간(새 파드 기동, 동반 부하 ramp)을 막는다. 실패 구간 사이에는 0 근처다.
- min_hold 9m, timeout 28m, max_injection_duration 30m: 첫 실패 구간(약 10~11분)과 두 번째(약 20~22분)가 모두 판정 기회다. 첫 구간에서 판정되면 녹화 구간의 되풀이는 한 번(누수 → OOM → 재시작 → 다시 상승)이고, 원본의 되풀이를 두 번 담는 것보다 판정이 서는 쪽을 골랐다(세 번째 구간은 약 31분 뒤라 timeout 과 동반 부하를 늘려야 함). 운영에서 스레드당 메모리나 요청률이 달라 주기가 늘어도 두 번째 구간을 timeout 이 받는다.
- 동반 부하: ramp 2m + hold 27m + ramp_down 15s(29m15s)를 max_injection_duration 30m 이 덮는다. seed 6363.
- must_rule_out: achieved_rps < 5, transfer 파드 NotReady, Oracle 파드 NotReady(2틱). transfer-heavy-surge.js 의 maxVUs 는 400 이라 멈춤이 30초를 크게 넘으면 VU 가 바닥나 achieved_rps 가 내려갈 수 있다(첫 실행에서 최저값 확인).
- abort: entry_status == 0(2틱). entry_status 는 기준선 이체 단계(nginx → api)다. api 가 멈춘 구간에는 이체가 끝나지 않고, OOM 으로 죽는 순간 연결이 끊겨 nginx 가 502 로 끝내며, 그 뒤 엔드포인트가 빈 동안 nginx 가 바로 502 다. 0 은 k6 60초 제한을 넘어야 나오는데(nginx-conf 에 proxy_*_timeout 이 없어 nginx 도 기본 60초라 비슷한 때 504), 로컬 실측의 멈춤 구간 최장 응답이 22.9초였다. 상주 기준선 이체는 약 0.47건/초이고 abort 는 독립 간격 없이 2틱이라, JVM 이 엔드포인트에 남은 채 60초 넘게 멈추면 약 75초 안에 abort 될 수 있다. readiness(3회 × 10초)가 그 전에 엔드포인트에서 떼어 내고 liveness(5회 × 15초)가 상한이라 위험은 크지 않다(22.9초는 로컬 11건/초 한 번의 값이라 첫 실행 확인 항목). nginx 와 노드는 건드리지 않는다.
- recovery: target_health 200, api 파드 Ready, 기준선 이체 5xx 비율 < 0.05(2틱).
- cleanup: 이미지 원복, available(180초), 1.5.0 을 쓰는 파드가 없어진 뒤 tb-w2 containerd 에서 1.5.0 의 이름 참조와 ID 참조를 모두 지우고 둘 다 없는지 확인. 옛 api 프로세스가 내려가면 쌓인 스레드도 사라진다. 데이터 부작용 없음(새 코드는 하류 호출을 다른 스레드에서 할 뿐 요청과 응답은 같다).
- 노드: tb-w2 이미지 파일시스템 74%(2026-10-11, preflight 는 80% 미만 요구). 노드 threads-max 와 kubepods pids.max 94,860, 노드 전체 스레드 약 990(2026-10-11)이라 api 스레드 약 6,000 개는 노드 한도에 닿지 않는다. 노드 메모리 11.6GiB 중 약 6GiB 가용이고 api 는 cgroup 1GiB 안에서만 찬다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Intercom 2025-04-24, 공식 상태 페이지 사후 보고 링크)와 요소별 대응표, 기전 같음 (§2)
- [x] 장부 갱신(§4-1, §6), 묶음 J 14/83(16.9%), A 로 세도 8/83(9.6%), 정답 위치 은행 API 3/83(3.6%), 결제 경로 12/83(14.5%, 무관), 은행 28(쇼핑몰 28, 음식배달 27. 음식배달은 같은 기전이 필수 중단 조건에 걸림). 어느 축도 20% 에 닿지 않는다 (§3)
- [x] 근본 원인 위치: 패치 `api-service.patch:45-63`, `:37`, `:42`, 매니페스트 `ApiService.java:28-42`, `20-api-service.yaml:27-28, :57-76, :77-83`, nginx-conf (§4)
- [x] 근본 원인의 흔적 조회: VM api jvm.thread.count(비데몬 7일 최대 8, 모든 서비스 최대 12), jvm.memory.used heap, CH syslog_local tb-w2 커널 memcg OOM 보고 수집, CH kcm_events_local api 롤아웃·Pulled 태그·Unhealthy, CH lucida_logs_local service.instance.id (§7)
- [x] 핵심 증거가 표본 데이터만이 아님: JVM 런타임 지표, syslog 커널 메시지, KCM 이벤트, 로그 자원 속성 (§7)
- [x] 계기 흔적: 롤아웃 이벤트와 새 태그(인공 지연 없음, 지연과 실패는 실제 메모리 고갈과 OOM 에서 남) (§7)
- [x] 정답이 관제 데이터로 낼 수 있는 결론: "api 롤아웃 뒤 api 비데몬 스레드가 요청에 비례해 쌓이고 힙은 그대로인데 api 컨테이너가 OOM 으로 죽기를 되풀이 → api 새 버전의 스레드 누수, 롤백". 코드 줄 추론 불필요 (§5)
- [x] 정답지 세 칸(근본 core-banking-api, 계기 비움, 부분 testbed-nginx, banking-nginx) (§5). class A(코드 결함). fault_pattern 은 P1~P8 어느 것도 '요청마다 스레드 누수'에 맞지 않아 생략(새 P 번호를 만들지 않는 규칙)
- [x] 감지, 피해 판정, RCA 증거 구분 (§6)
- [x] 시나리오 id 흔적 없음: 패치 문자열(withinDeadline, DEADLINE_MS, 'transfer submit', 'transfer list', 'did not finish within', 'timed out', 'interrupted')에 id, fault, bug, chaos, scenario, leak 없음(테스트로 확인), 태그 1.5.0, 실행기 인자와 상태 파일에 id 없음
- [x] 피해 계산: 스레드 약 11.5개/초 × 약 130KiB ≈ 약 1.5MiB/초, 여유 약 734MiB 를 약 8분에 채움(로컬 실측 8분 20초, OOM 9분 40초). 실패 구간은 짧은 멈춤 뒤 OOM, 바로 재시작, Ready 까지 26~29초라 이체 5xx 비율 0.5 이상이 약 30~45초이고(최대 틱 간격 25초보다 길어 1틱 판정에 충분) 약 10분마다 되풀이. 서킷브레이커는 api 안(하류 쪽)이라 api 자신의 메모리 고갈과 무관하다 (§9)

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- 배포 단계에서 `bash scripts/scenarios/fault-images/build.sh f63-r` 로 109 docker 에 core-banking-api:1.5.0 이 있어야 preflight 가 통과한다(빌드 전후 기본 이미지 ID 비교는 스크립트가 한다).
- KCM 이 새 파드의 'Pulled ... core-banking-api:1.5.0' 을 잡는지, api 비데몬 waiting 스레드가 분당 수백씩 직선으로 오르는지, 힙이 그대로인지.
- 한도 도달 시각과 죽는 방식(커널 OOM 인지 liveness 인지), tb-w2 syslog 에 api cgroup 의 OOM 보고와 Killed process (java) 가 남는지, KCM 이 같은 파드의 재시작과 Unhealthy 를 남기는지.
- 이체 5xx 비율이 실패 구간마다 0.5 를 넘는 길이(참 구간 L)와 실제 틱 간격, 실제 누수 주기(롤아웃에서 OOM 까지), achieved_rps 최저값이 5 위인지, 잔액 조회는 0 근처인지, 인시던트가 생기는지(원칙 7).
- entry_status 가 0 이 되지 않는지(멈춤 구간 이체가 k6, nginx 의 60초를 넘지 않는지, 운영의 멈춤 길이).
- 녹화 검증: F41-R, F41-H 와 겉 증상이 비슷하므로 재시작 대상이 api 이고 비데몬 스레드 상승과 api cgroup OOM 이 있어야 F63-R 녹화로 인정.
- cleanup 뒤 tb-w2 containerd 에 1.5.0 의 이름 참조와 ID 참조가 모두 남지 않았는지(recovery 가 확인).

## 12. 평가 반영 기록

- 2026-10-11 1차 평가 FAIL(원칙 9, 차단): 이체 5xx 비율은 갱신 30초라 3틱이 60~75초를 요구하는데 실패 구간은 약 43~46초라 success 가 서지 않음. consecutive_ticks 3 → 2, min_hold 13m → 9m. 재시작 대기 10초, 20초와 기동 약 40초(account 값)를 109 실측(첫 크래시 바로 재시작, Ready 까지 26~29초)으로 바로잡음. 권고 반영: 힙 한도(풀 합 259,522,560, 기본 1/4), nginx 기본 60초 시간 제한, VU 상한과 achieved_rps 확인, 스택 실제 RSS 로 본 주기 범위.
- 2026-10-11 재평가 1회차 FAIL(원칙 9, 차단): 러너 실제 틱 간격이 15~25초(틱 뒤 15초 쉼)라 갱신 30초 관측의 2틱은 약 38초 폭이 필요하고, 참 구간 약 30~45초에서 확률적(43초 33%, 30초 0%)이었다. consecutive_ticks 2 → 1(선례 F20-R)로 내리고 근거 세 가지(구간 ≥28초 > 최대 틱 간격 25초, 30초 창 하나가 이체 약 240건, min_hold 9m 이 과도 구간 차단)를 §9 와 controller `_note` 에 적었다. '30초 폭', '위상과 무관' 문구를 실제 틱 간격 기준으로 고쳐 씀. 권고 반영: 사용자 증상 502 구간을 약 30~45초로 통일, back-off 를 '앞 크래시 뒤 10분 안이면 10초'로 조건부로, 주기 범위(OOM 롤아웃 뒤 약 9.7~12.8분)와 첫 실행의 주기 기록 항목.
