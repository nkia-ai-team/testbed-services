---
title: F63-P 설계 시트 (food dispatch-service 를 배달 조회마다 스레드 하나짜리 풀을 만들고 닫지 않는 릴리스로 롤아웃)
status: Draft
owner: project
last_reviewed: 2026-10-11
tags:
  - scenario
  - design
  - release
summary: food dispatch-service 를 결함 있는 새 릴리스(food-delivery-dispatch:1.9.0, fault-images/f63-p 패치로 만든 별도 태그)로 롤아웃하면, 배달 목록과 상세 조회에 새로 더한 처리 마감 시간이 조회마다 스레드 하나짜리 풀을 만들고 닫지 않아 멈춘 스레드가 조회 수만큼 쌓이고, 저녁 배달 상태 확인 부하에서 몇 분 만에 dispatch 컨테이너가 메모리 한도 1GiB 를 채워 OOM 으로 죽고 재시작을 되풀이하며, 그 구간마다 order 의 배달원 용량 확인이 실패해 모든 새 음식 주문이 503 으로 거절되는 시나리오. 원본은 Intercom 2025-04-24 의 새 카운터 구현이 요청마다 스레드를 만들고 치우지 않은 스레드 누수(F63-R 과 같은 원본의 food 하류 판).
---

# F63-P 설계 시트

## 1. 요약

food dispatch-service 의 새 릴리스 1.9.0 은 손님이 부르는 두 배달 조회, 배달 목록(GET /api/deliveries)과 배달 상세(GET /api/deliveries/{id})에 처리 마감 시간 2초를 더한다. 저녁 피크에 손님들이 배달 상태를 되풀이해 확인할 때 DB 가 느리면 조회가 dispatch 의 요청 스레드를 붙잡아 같은 서버의 배차 요청까지 밀리지 않게 하려는 것이다. 마감 처리는 요청마다 `Executors.newFixedThreadPool(1)` 로 풀을 만들어 조회를 맡기고 `Future.get(2초)` 로 기다리는데, 풀을 닫지 않는다. 고정 크기 풀의 작업 스레드는 다음 작업을 기다리며 멈춰 있는 비데몬 스레드이고 자기 풀을 붙잡고 있어서 스레드도 풀도 회수되지 않는다. 조회 하나마다 멈춘 스레드가 하나씩 남는다. order 가 부르는 배달원 용량 확인과 배차 요청은 그대로다.

스레드마다 접근한 스택 페이지와 커널 스택(16KiB)이 컨테이너 메모리로 잡혀 쌓인다. 동반 부하(배달 조회만, 초당 10건)와 기준선 배달 추적으로 스레드가 초당 약 10개 늘어, dispatch 컨테이너 메모리가 기동 직후 약 350MiB 에서 한도 1GiB 까지 오른다. 힙은 그대로다. 한도에 닿으면 커널의 memory cgroup OOM 이 java 를 죽이고 kubelet 이 같은 파드에서 다시 띄운다(109 실측 기동에서 Ready 까지 41~45초). 멈춤, 재시작, 기동 동안 order 의 배달원 용량 확인이 시간 초과와 연결 거부 끝에 실패하고 서킷이 열려, 모든 새 음식 주문이 주문 행을 저장하기 전에 503 'Dispatch service unavailable' 로 거절된다. 배달 추적도 실패한다. 새 컨테이너도 같은 릴리스라 되풀이한다. order, restaurant, payment, MySQL 은 멀쩡하다.

비유: 배차 사무실(dispatch)에 새 규칙이 왔다. "배달 상태를 묻는 손님마다 전담 직원 한 명을 새로 불러 조회를 맡기고 결과만 받는다." 그런데 일이 끝난 전담 직원을 돌려보내지 않는다. 사무실(컨테이너 메모리)이 손님 수만큼 차서 몇 분 뒤 사무실이 문을 닫고(OOM) 다시 연다. 사무실이 닫힌 동안 주문 창구(order)는 배달원이 남았는지 물을 곳이 없어 주문을 받지 못한다. 고칠 곳은 사무실 크기가 아니라 새 규칙(dispatch 릴리스)이다.

## 2. 원본 사례

- **Intercom(현 Fin), 2025-04-24, 2025-04-25** (공식 상태 페이지 사후 보고, "Intercom teammate web app is down", 미국 호스팅 지역): https://www.finstatus.com/us-hosting/incidents/01JSPDFV117N6Z8VPG2W98BMBQ/write-up
- 2025-04-24 20:09~20:17 UTC 와 04-25 11:57~12:07 UTC 에 Inbox 를 포함한 팀원용 웹 앱이 내려갔다. 고객은 "general slowness" 와 오류 페이지를 봤다. 4월 22일부터 Inbox 카운터를 갱신하는 새 방식을 롤아웃했고 24일 모든 고객에게 켰다. 원인은 스레드 누수다: "The new counters implementation was unintentionally creating a new set of threads on each request", "memory usage of the Rails processes on the web serving fleet started to increase due to a thread leak bug", "Most web serving hosts ran out of memory in a very short period of time." 정기 배포가 누수를 초기화했는데 마지막 배포 뒤 2시간이 넘게 지나 있었다. 첫 장애는 증설과 헬스 체크 실패에 따른 호스트 교체로 회복됐고, 배포가 막히자 다시 장애가 났다. 메모리를 두 배로 늘리고 누수 수정을 내보내 끝났다.
- 자료 문서 M12 에 이미 있다(F63-R 이 더함). 같은 원본의 banking api 판이 F63-R 이다.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | Inbox 카운터 새 구현의 롤아웃 | dispatch-service 를 릴리스 food-delivery-dispatch:1.9.0 으로 롤아웃(KCM ScalingReplicaSet, 새 파드 Pulled 이벤트에 태그) |
| 원인이 된 결함 | 요청마다 스레드 묶음을 새로 만들고 치우지 않음 | 배달 조회마다 스레드 하나짜리 풀을 새로 만들고 닫지 않음(멈춘 비데몬 스레드가 조회마다 하나씩 남음) |
| 전파 경로 | 요청 수에 비례해 웹 서버 메모리 상승 → 메모리 고갈 | 조회 수에 비례해 스레드와 컨테이너 메모리 상승 → 한도 → 멈춤, OOM, 재시작 → 같은 릴리스라 되풀이 → 그 구간에 dispatch 에 기대는 주문 생성이 거절됨 |
| 사용자 증상 | 전반적인 느림과 오류 페이지, 앱 다운(각 8~10분) | 배달 추적 실패, 새 음식 주문 503(구간마다 약 1분), 약 11~14분마다 되풀이 |
| 원본의 탐지 경로 | 메모리 사용률, 헬스 체크 실패, 지연 | lucida-next 의 dispatch JVM 비데몬 스레드 수 이상(평시 최대 10 → 수천), tb-w3 커널 memcg OOM 로그, KCM 재시작과 Unhealthy, order 의 용량 확인 실패 ERROR 와 주문 5xx |
| 완화와 복구 | 증설, 호스트 교체, 정기 재배포(누수 초기화), 누수 수정 배포 | 재시작이 잠시씩 살림(원본의 호스트 교체와 같은 효과), cleanup 이 매니페스트 이미지로 되돌림(롤백) |

기전 일치: 원인(새 버전이 요청마다 스레드를 만들고 치우지 않음)에서 증상(요청 수에 비례한 메모리 상승 → 고갈 → 느림과 오류, 재시작이 잠깐 살리고 다시 참)까지 고리가 같다. 바꾼 것(고리 밖): Rails 대신 Spring Boot JVM, 웹 서버 여러 대 대신 dispatch 파드 하나, 호스트 메모리 대신 컨테이너 한도. 사용자 증상이 누수 서비스의 하류 의존(order 의 주문 생성)에서 나는 점이 F63-R(누수 서비스가 곧 진입 경로)과 다르다.

## 3. 숫자 근거 (2026-10-11, `scenario-stats.py`, 정식 + 후보 합계 92, 이 후보 전)

- 묶음: J 14(15.2%), G 12, D 11, P 11, D·F 외 0~8. N 만 0 이다(재시도 증폭은 rejected 45, 151, 262 행처럼 하류 실패라는 두 번째 계기가 필요해 이번에도 후보가 없었다). 어느 묶음도 20%(18.6) 에 닿지 않는다. 이 후보 J 14 → 15(93 중 16.1%), 피해 꼴로 A 를 덧붙여 J+A(A 로 세도 7 → 8, 8.6%).
- 정답 위치: 주문 서비스 8(8.7%)이 가장 많다. **배달 서비스 5 → 6(6.5%)**. 결제 경로 12/93(12.9%, 무관).
- 서비스: 쇼핑몰 31, 은행 31, 음식배달 30 → 31. 음식배달이 가장 적어 정답 위치에 제한이 없다. 쇼핑몰과 은행은 최다 동률이라 정답 위치 0 인 부품만 낼 수 있다.
- 왜 이 후보인가: 현실 트리거 1위(Google 바이너리 배포 37%)인 결함 있는 새 버전이고, 가장 적은 음식배달에서 정답 위치가 적은 서비스다. 결함 꼴(스레드 누수 → 컨테이너 OOM 재시작 고리)은 음식배달에 없고, 누수 서비스가 다른 서비스의 동기 의존이라 증상(order 503)과 원인(dispatch)이 갈리는 전파는 F63-R 에 없다. 관제 AI 는 order 503 이 같은 F43-P(연결 누수), F55-R(429), F33-H(MySQL), F60-H(잠금 대기), F51-P·F32-R(용량)과 이 후보를 스레드, OOM, 재시작으로 갈라야 한다.

### 후보 목록 (3단계, 실제 기전 × 부품)

| # | 후보 | 층, 부품 | 원본 사례 | 결과 |
|---|---|---|---|---|
| 1 | food dispatch 새 릴리스가 배달 조회마다 스레드 풀을 만들고 닫지 않아 컨테이너 OOM 재시작 반복, 그 구간에 주문 503 | 앱, food dispatch(J+A, 배달 서비스 5) | Intercom 2025-04-24 공식 사후 보고 | **채택**. rejected 274행(같은 원본을 food 네 서비스에)이 dispatch 를 entry_status 0 으로 막았으나 사실과 다르다: entry_status 는 기준선 주문 생성 단계(30180 → order)의 마지막 상태(`loadgen_monitor.py` business_step create)이고 dispatch 가 죽으면 order 는 503 으로 답한다. 같은 dispatch NotReady·재시작을 F43-P, F60-H 가 이미 이 근거로 설계했다(controller `_note`). 그 행의 restaurant, order, payment 판은 그대로 막힌다(아래 2, 11) |
| 2 | 같은 스레드 누수 릴리스를 food restaurant 에 | 앱, food restaurant(가게 3) | 같은 원본 | 버림: 컨트롤러 필수 중단 조건에 준함. 기준선 주문 여정이 restaurant 메뉴 조회가 200 일 때만 order 를 불러(`food-delivery/loadgen/script.js` orderJourney) restaurant 가 죽은 동안 주문 표본이 사라지고 판정, 배제 관측이 함께 끊김(rejected 274행) |
| 3 | food restaurant 새 릴리스의 힙 메모리 누수 | 앱, food restaurant(가게 3) | Honeycomb 2019-11-06 공식 | 버림: 2 와 같은 벽에 GC 로 느려지는 구간이 길어 order 가 트랜잭션 안에서 기다림(rejected 178행 꼴) |
| 4 | food dispatch 또는 restaurant 릴리스가 health 끝점을 없애 kubelet 재시작 반복 | 앱, food dispatch·restaurant | Spreedly 2023-01-25 공식 | 버림: 원칙 9. food 는 startup, readiness, liveness 가 모두 같은 /actuator/health 라(22-dispatch-deploy.yaml:67-86) 새 파드가 Ready 가 못 되고 기본 롤링이 옛 파드를 둬 피해가 없음(F41-H 는 banking 의 하위 경로) |
| 5 | food dispatch 릴리스의 엄격한 입력 검증이 정상 배차 요청을 400 | 앱, food dispatch | Flagsmith 2024-01-18 공식 | 버림: F34-R 교훈. order 가 하류 4xx 를 ClientErrorException 으로 그대로 400 전파해 4xx 단일 신호 |
| 6 | banking outbox_events 를 ALTER TABLE MOVE 로 공간 회수하다 PK 인덱스 UNUSABLE, 이체 INSERT ORA-01502 | DB 표, banking 이체 이벤트 outbox(0) | 공식 사후 보고 없음 | 버림: 원칙 1(웹 검색 1회, rejected 209행과 같은 벽) |
| 7 | 최소 권한 정리 마이그레이션이 표 하나의 INSERT 권한을 빠뜨림 | DB 계정(3) | 공식 사후 보고 없음 | 버림: 원칙 1(웹 검색 1회 더: 벤더 문서와 포럼뿐, rejected 75행) |
| 8 | 정렬 규칙(collation) 마이그레이션으로 조인이 인덱스를 못 탐 | DB 표, commerce PG·food MySQL | Autumn 2026-03-15 공식 블로그 | 버림: 원칙 1 재현 불가. 세 도메인 사용자 경로에 문자열 열끼리 조인하는 질의가 없음(rejected 200행과 같은 벽) |
| 9 | 감사 트리거의 로그 표 32비트 id 가 차서 본 쓰기까지 실패 | DB 표, food order_items(0) | GoCardless 2018-04-06 공식 블로그 | 버림: 원칙 1. 테스트베드에 트리거와 32비트 열이 없어 둘을 함께 주입해야 해 인공적(rejected 236행과 같은 벽) |
| 10 | 잘못된 스키마 제약(int32 외래 키)으로 쓰기 실패 | DB 표 | Sentry 2021-01-12 공식 상태 페이지 | 버림: 원칙 1. 포럼 설명상 int32 넘침이고 세 도메인 id 가 모두 BIGINT, NUMBER(rejected 174행) |
| 11 | 같은 스레드 누수 릴리스를 food payment 에 | 앱, food payment(결제 경로) | Intercom 2025-04-24 | 버림: 컨트롤러 필수 중단 조건, 원칙 9. order 가 트랜잭션 안에서 payment 를 기다리고, 배차 뒤 실패라 주문마다 배달원 배정이 남아 용량이 참(rejected 274, 293행) |

부품 8종(food dispatch, restaurant, payment, banking outbox 표, DB 계정, commerce·food 조인 표, food order_items, 표 제약), DB 가 정답인 후보 5개. 웹 검색 6회(유일 제약 마이그레이션, 제약 위반 쓰기 실패, Sentry 2021-01-12, 길이 초과, 트리거, 권한 회수 사후 보고).

## 4. 인과 사슬 (코드와 인프라 위치)

1. 롤아웃: `scripts/scenarios/profiles/k8s_image_executor.py` RELEASE_SCRIPT `run` — 109 docker 의 food-delivery-dispatch:1.9.0 을 tb-w3 containerd 에 올리고 `kubectl set image deploy testbed-dispatch dispatch-service=food-delivery-dispatch:1.9.0`(F33-H, F43-P, F55-R 과 같은 스크립트, 계약 `CONTRACTS["F63-P"]` 만 더함).
2. 배포: `food-delivery/k8s/22-dispatch-deploy.yaml:27-28`(image, imagePullPolicy Never), `:49-50`(JAVA_TOOL_OPTIONS 는 OTel 에이전트뿐), `:67-86`(startup, readiness, liveness 모두 /actuator/health), `:87-93`(requests 512Mi, limits cpu 500m memory 1Gi). 전략은 기본값(maxSurge 25%, 109 kubectl 2026-10-11). 운영 파드 컨테이너 pids.max 14229, tb-w3 aarch64, threads-max 94,860.
3. 결함: `scripts/scenarios/fault-images/f63-p/dispatch-service.patch:66-84`(DispatchController.withinDeadline — 요청마다 `Executors.newFixedThreadPool(1)`, `submit`, `Future.get(2000ms)`, 시간 초과면 cancel 과 504, 하류 예외는 그대로 다시 던짐(상세 404 유지), 풀은 닫지 않음), `:50-57`(배달 목록, 상세가 withinDeadline 을 씀). 매니페스트 버전은 `food-delivery/dispatch-service/src/main/java/com/fooddelivery/dispatch/controller/DispatchController.java:31-45`(요청 스레드에서 바로 DispatchService 호출). 용량 확인(`:62-64` getCapacity)과 배차 요청은 그대로다.
4. 누수: 고정 크기 풀의 작업 스레드는 비데몬(pool-N-thread-1)이고 작업 뒤 큐에서 기다리며 멈춘다(WAITING). 조회 트랜잭션은 작업 안에서 끝나 연결은 돌려준다(Hikari, MySQL 은 그대로). 테스트 `test_k8s_image_release_mode_rolls_food_dispatch_thread_leak_release` 가 이 꼴과 감싸지 않는 두 경로를 고정한다.
5. 피해 쪽: order 의 주문 생성은 restaurant 확인 뒤 `DispatchClient.checkCapacity()`(`food-delivery/order-service/src/main/java/com/fooddelivery/order/client/DispatchClient.java:31-50`, 읽기 5초, 재시도 3회, 서킷: 10건 창 50%, 5초 열림)로 용량을 묻고, 실패하면 `OrderService.java:94-112` 에서 503(서킷 fallback 'Dispatch service unavailable') 으로 끝낸다. 주문 행 저장은 `:128` 로 그 뒤라 실패한 주문은 주문 행도 배차도 남기지 않는다(배차 한도를 채우지 않음). 실패마다 order 는 ERROR 'Failed to check dispatch capacity: ...' 를 남긴다(서킷이 열린 뒤에는 로그 없이 fallback).
6. 영향 밖: restaurant(가게 둘러보기, 메뉴), payment, MySQL, notify. dispatch 의 만료 배치는 재기동 동안 멈췄다가 다음 주기(30초)에 따라잡는다.

### 로컬 실측 (2026-10-11, 원칙 9 근거)

origin/main food-delivery 에 패치를 얹어 `./mvnw -o -pl dispatch-service -am package` 로 jar 를 만들고, eclipse-temurin 17-jre(x86_64) 컨테이너에 OTel 자바 에이전트(내보내기 끔)를 붙여 `--cpus 0.5 -m 1g --memory-swap 1g`(운영 한도와 같음)로 띄웠다. DB 는 mysql:8.0 에 `food-delivery/db/init.sql` 과 끝난 배차 1만 행을 더했다. 열린 고리 부하 초당 10건(배달 목록 75%, 상세 25%, 동반 부하와 같은 비율), 30초마다 cgroup memory.current, 스레드 수, /actuator/health(3초 제한)를 쟀다.

| 시각(부하 시작 뒤) | 스레드 | 컨테이너 메모리 | 응답 |
|---|---|---|---|
| 0 | 40 | 353MiB | 전부 200/404, 최장 0.1초 |
| 2분 | 1,248 | 551MiB | 오류 0 |
| 5분 | 3,061 | 746MiB | 오류 0 |
| 8분 3초 | 4,874 | 938MiB | 오류 0, 최장 0.6초 |
| 9분 4초 | 5,478 | 1,023MiB(한도) | 오류 0 |
| 9분 34초 | 5,781 | 1,023MiB(한도) | 오류 0, health 200 |
| 9분~10분 25초 | 약 6,000 | 한도 | 조회 최장 21.2초, 마지막 구간 오류 115건(연결 끊김) |
| 10분 25초 | - | - | OOMKilled(종료 137, docker State.OOMKilled true) |

- 스레드 증가는 초당 약 10개(요청 수와 같음), 메모리는 데워진 뒤 스레드당 약 100~110KiB, 초반 JIT 구간 포함 평균 약 125KiB(커널 스택 16KiB 포함, memory.stat kernel_stack 이 스레드 5,781개에 94.8MB). 부하 시작부터 한도 도달까지 약 9분, OOM 종료까지 10분 25초. 그 전 약 9분 30초 동안 실패 0건(5,745건 전부 200/404). 힙은 누수 경로 밖이다.
- 한도에서 바로 죽지 않고 약 1분 버틴 것은 로컬 컨테이너가 회수할 페이지를 가진 동안이다(F63-R 로컬 실측도 30초~2분). 운영(aarch64, 내보내기 켬, 기동 직후 메모리가 로컬과 다를 수 있음)의 주기와 멈춤 길이는 첫 실행에서 기록한다.

## 5. 원인 규정 (원칙 5, 6)

- 근본(`root_cause.target_id`): `food-delivery-dispatch`(dispatch-service 의 새 릴리스). 결함을 가진 곳이자 되돌려야 재발이 막히는 곳.
- 계기(`trigger_target_id`): 비움. 계기(릴리스 롤아웃)와 결함이 같은 곳이다. 손님과 loadgen 의 배달 조회는 정당한 요청이다(동반 부하는 저녁 피크의 배달 상태 확인 꼴, 매니페스트 버전은 같은 부하에서 스레드가 일정). 컨테이너 한도와 JVM 옵션도 그대로다(원칙 6).
- 부분 점수: `food-delivery-order`, `testbed-order`(503 을 낸 증상 위치). 노드(tb-w3)는 넣지 않는다: 노드 메모리는 여유(가용 약 6.3GiB)이고 한도에 닿은 것은 dispatch 컨테이너 cgroup 하나다.
- 층위: 원본 사후 보고의 결론("thread leak bug" 가 든 새 카운터 구현, 누수 수정 배포로 끝남)과 같다. 패치 속 코드 줄을 맞히라고 요구하지 않는다(원칙 5, 결함 버전 이미지 규칙 7). 관제 데이터로 낼 수 있는 결론: "dispatch 를 1.9.0 으로 롤아웃한 뒤 dispatch 의 비데몬 대기 스레드가 조회 수에 비례해 수천으로 늘고(힙은 그대로) dispatch 컨테이너가 메모리 한도에서 OOM 으로 죽기를 되풀이한다 → 그동안 order 가 용량 확인에 실패해 주문 503 → dispatch 새 버전의 스레드 누수, 롤백".

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 |
|---|---|
| 감지 | food order 의 주문 생성 5xx 와 'Failed to check dispatch capacity' ERROR 로그 급증, dispatch JVM 스레드 수 이상, dispatch 스팬과 로그가 끊겼다 다시 생김(service.instance.id 바뀜), tb-w3 커널 OOM 로그, KCM Unhealthy 와 재시작 |
| 피해 판정(러너) | 기준선 주문 생성 5xx 비율 ≥ 0.5, 1틱(평시 0 근처). 배제: 동반 부하 전달, MySQL·order 파드 Ready |
| RCA | 롤아웃 이벤트와 새 이미지 태그, dispatch 비데몬 대기 스레드의 조회 비례 직선 상승과 재시작마다 초기화, 힙과 데몬 스레드는 그대로, dispatch cgroup OOM 커널 보고, 바뀌지 않은 한도와 JVM 옵션, MySQL·restaurant·payment 정상 |

## 7. 관측 근거 표 (119 실조회, 2026-10-11 평시)

| 증거 | 119 표와 칸 | 조회 | 평시 결과 |
|---|---|---|---|
| 계기: 롤아웃과 이미지 태그 | CH `kcm_events_local`(reason, object_name, body) | `namespace='rca-testbed-food' AND object_name LIKE 'testbed-dispatch%'` 최근 3일 | 2026-10-11 06:46~06:53, 10-08~09 의 dispatch 롤아웃 다섯 번이 ScalingReplicaSet, SuccessfulCreate, Scheduled, Pulled('Container image "food-delivery-dispatch:latest" already present on machine'), Created, Started, Unhealthy(startup 'connection refused', 'context deadline exceeded'), Killing 으로 수집됨. Started → 옛 파드 Killing 41초(18:51:33→18:52:14), 45초(21:13:23→21:14:08), 41초, 45초, 41초, 45초 |
| 근본: 스레드 누수 | VM `apm.agent.otel.java.jvm.thread.count`(service_name, jvm_thread_daemon) | `max_over_time(sum(...{service_name="food-delivery-dispatch",jvm_thread_daemon="false"})[7d:5m])` | 10(7일 최대). 지금 비데몬 5, 데몬 37. 에이전트가 주기적으로 내보내는 런타임 지표라 트레이스 표본과 무관 |
| 감별: 힙은 그대로 | VM `apm.agent.otel.java.jvm.memory.used` | dispatch, jvm_memory_type 별 | heap 약 87MB, non_heap 약 207MB(2026-10-11). 로컬 실측에서 스레드 수천 개일 때도 누수는 스택, 커널 스택이라 힙 밖 |
| 근본: 컨테이너 OOM | CH `syslog_local`(hostname, app_name, message, received_at) | `hostname='tb-w3' AND app_name='kernel' AND (message LIKE '%Killed process%' OR message LIKE '%oom-kill%')` | tb-w3 커널의 memcg OOM 보고가 통째로 수집됨: 2026-10-08 20:33 'oom-kill:constraint=CONSTRAINT_MEMCG,...cpuset=cri-containerd-...scope', 'Memory cgroup out of memory: Killed process 433032 (mysqld) ... anon-rss:1033516kB', 'ib_log_writer invoked oom-killer'. 고장 때는 dispatch 파드 cgroup 에 limit 1048576kB, Killed process (java) |
| 전파: 재시작 흔적(로그) | CH `lucida_logs_local`(resource_attributes['service.instance.id']) | food-delivery-dispatch 7일 | 451,317행, service.instance.id 8개. JVM 이 다시 뜰 때마다 새 id 가 생긴다 |
| 전파: 주문 쪽 | CH `lucida_logs_local` | food-delivery-order 7일, body 에 'ispatch' | ERROR 'Failed to check dispatch capacity: I/O error on GET request for "http://testbed-dispatch:8...' 295건(다른 시나리오 실행 때), WARN 'Dispatch capacity check failed, rejecting order' 517건. order 가 dispatch 연결 실패를 전수 로그로 남긴다 |
| 참고: dispatch 평시 로그 | CH `lucida_logs_local` | food-delivery-dispatch 1시간 | 'Dispatch expiry batch started/finished' INFO 뿐. 새 릴리스는 정상 경로에 로그를 더하지 않는다(시간 초과 때만 WARN '... did not finish within 2000ms') |

표본 데이터(트레이스 10%, APM 스팬 지표)만으로 증명하지 않는다. 결정 증거는 KCM 이벤트(전수), JVM 런타임 지표(스레드, 힙), syslog 커널 메시지(전수), 로그(전수)다.

## 8. 감별

- must_support: dispatch 롤아웃과 1.9.0 태그, dispatch 비데몬 대기 스레드의 조회 비례 상승(평시 최대 10 → 수천)과 재시작마다 초기화, 힙과 데몬 스레드는 그대로, tb-w3 커널의 dispatch cgroup OOM 보고, KCM 재시작과 Unhealthy, order 용량 확인 실패와 주문 503 이 멈춤과 재시작 구간에 몰림, MySQL·order·restaurant·payment 정상, 한도와 JVM 옵션 불변.
- must_rule_out: dispatch 연결 누수(F43-P), 호출자별 요청 한도(F55-R), MySQL 포화(F33-H, F33-R, F42-P), 잠금 대기열과 데이터로 찬 용량(F60-H, F51-P, F32-R), 한도나 힙 설정 축소(F05-R, F09-H), 노드 메모리 압박(F05-P), 배달 조회 부하 급증 자체(F07-H).
- contrast_with: F63-R, F43-P, F55-R, F33-H, F41-R, F05-R.
- F43-P 와는 '같은 dispatch 릴리스 뒤 재시작 고리와 주문 503' 겉 증상이 같다. 가르는 근거: 차오르는 자원(Hikari 사용 연결 10 고정 대 비데몬 스레드와 컨테이너 메모리), 죽는 방식(liveness 5회 실패 대 커널 memcg OOM), dispatch 로그('Connection is not available' 대 없음).
- F63-R 과는 원본과 결함 꼴이 같다(P 관계). 가르는 근거: 도메인, 누수 서비스(banking api 대 food dispatch), 증상이 나는 곳(nginx 502 대 하류를 부르는 order 의 503).

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

- 강도는 릴리스 하나로 고정(approved-fixed-f63-p, 실행기 파라미터 = 이미지 태그). 누수 속도는 dispatch 의 배달 조회 수에 비례한다(조회 1건 = 스레드 1개).
- 동반 부하가 주문을 만들지 않는 이유: 배차 한도 max-capacity 2000 은 동시에 배정 중인 배차 수다. 2026-10-11 실측으로 기준선 주문이 시간당 720~4,133건(초당 0.2~1.15건)이고 배정 중 배차가 1,132(저녁 저부하 시각)~1,677(rejected 289행)이라, delivery-tracking-surge.js 처럼 주문을 섞으면(목표 10rps 의 10% = 초당 1건) 피크에 몇 분 안에 'Courier pool exhausted' 503 이 섞여 F32-R 증상과 겹친다. 새 스크립트 delivery-status-surge.js 는 기준선 deliveryTrackingJourney 의 두 엔드포인트만 75/25 로 부른다.
- 부하 계산: 동반 10rps(ramp 2m 평균 약 5건/초, 그 뒤 10건/초) + 기준선 배달 추적(1~6 iter/s 의 10%) ≈ 초당 10~10.6개. 한도까지 필요한 스레드 = (1024 − 기동 직후 약 350)MiB ÷ 스레드당 메모리. 스레드당 메모리는 로컬 x86_64 실측 꾸준한 구간 약 100~110KiB(초반 JIT 포함 평균 약 125KiB, 로컬은 스레드 약 6,000개에서 OOM), F63-R 운영 aarch64 smaps 평균 약 97KiB 라 스레드 약 5,500~7,100개, 첫 OOM 은 롤아웃 뒤 약 11~14분(ramp 2분 약 600개 포함), 그 뒤 재기동 약 45초 + 다시 차는 약 9~12분과 한도에서 버티는 약 1분이 한 주기다. 컨테이너 pids.max 14229 보다 훨씬 적어 스레드 생성 실패보다 OOM 이 먼저다.
- MySQL 부하: 배달 목록(idx_dispatches_status_assigned, 20행 Slice)과 상세(PK)가 초당 10건이라 F33-H 동반 부하(12rps)보다 가볍다. 스레드는 조회가 끝난 뒤 연결을 돌려줘 Hikari 는 그대로다.
- success: 기준선 주문 생성 5xx 비율 ≥ 0.5, 1틱(선례 F63-R, F20-R). 0.5 이상인 구간은 [dispatch 가 엔드포인트에서 빠지거나 죽은 뒤 + 약 15초, 엔드포인트 복귀 + 15초]이고, 재기동에서 Ready 까지 41~45초(109 KCM 실측)만으로 최대 틱 간격 25초보다 길다. 그 앞 멈춤 구간에도 order 는 용량 확인 시간 초과(5초 × 3)와 서킷 끝에 503 이라 구간이 늘어난다. 30초 창 하나가 기준선 주문 6~34건이다.
- order 풀 위험: 멈춤 초반 서킷이 열리기 전 약 10~15초 동안 주문 트랜잭션이 용량 확인을 기다리며 order 연결을 쥔다(기준선 최대 초당 1.15건 × 약 15초 ≈ 17 대 풀 15). order 의 readiness 는 3회 × 10초 실패가 필요해 서킷이 열리면 풀린다(F60-H 와 같은 구간). 첫 실행에서 order 가 Ready 를 지켰는지 확인한다.
- min_hold 8m, timeout 28m, max_injection_duration 30m: 첫 OOM(약 11~14분)이 판정 기회이고 두 번째(약 22~28분)는 대개 timeout 안에 든다. 동반 부하 ramp 2m + hold 27m + ramp_down 15s 를 max_injection_duration 이 덮는다. seed 6316.
- must_rule_out: achieved_rps < 2.5, food MySQL 파드 NotReady, order 파드 NotReady(2틱). dispatch 파드 준비 상태와 JVM 스레드는 러너 허용 목록 밖이라 RCA 쪽 감별로 둔다(F43-P 와 같음).
- abort: entry_status(기준선 주문 생성 단계) == 0, 2틱. order 는 dispatch 가 죽어도 503 으로 답하고 응답이 k6 60초를 넘지 않는다(용량 확인 최장 약 15.6초).
- recovery: target_health 200, MySQL·order Ready, 기준선 주문 2xx ≥ 0.7(2틱).
- cleanup: 이미지 원복, available(180초), 1.9.0 을 쓰는 파드가 없어진 뒤 tb-w3 containerd 에서 이름 참조와 ID 참조를 지우고 확인. 데이터 부작용 없음(실패한 주문은 주문 행과 배차를 남기지 않고, 새 코드는 조회를 다른 스레드에서 할 뿐 응답은 같다).
- 노드: tb-w3 이미지 파일시스템 54%(2026-10-11, preflight 80% 미만), 메모리 가용 약 6.3GiB.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Intercom 2025-04-24, 공식 상태 페이지 사후 보고 링크)와 요소별 대응표, 기전 같음 (§2)
- [x] 장부 갱신(§4-1, §6), 묶음 J 15/93(16.1%), A 로 세도 8/93, 정답 위치 배달 서비스 6/93(6.5%), 결제 경로 12/93(12.9%, 무관), 음식배달 31(쇼핑몰 31, 은행 31). 어느 축도 20% 에 닿지 않는다 (§3)
- [x] 근본 원인 위치: 패치 `dispatch-service.patch:66-84`, `:50-57`, 매니페스트 `DispatchController.java:31-45`, `22-dispatch-deploy.yaml`, order `DispatchClient.java:31-50`, `OrderService.java:94-128` (§4)
- [x] 근본 원인의 흔적 조회: VM dispatch jvm.thread.count(비데몬 7일 최대 10), jvm.memory.used heap, CH syslog_local tb-w3 커널 memcg OOM 보고 수집, CH kcm_events_local dispatch 롤아웃·Pulled 태그·Unhealthy, CH lucida_logs_local service.instance.id 와 order 용량 확인 ERROR (§7)
- [x] 핵심 증거가 표본 데이터만이 아님: JVM 런타임 지표, syslog 커널 메시지, KCM 이벤트, 전수 로그 (§7)
- [x] 계기 흔적: 롤아웃 이벤트와 새 태그(인공 지연 없음, 지연과 실패는 실제 메모리 고갈과 OOM 에서 남) (§7)
- [x] 정답이 관제 데이터로 낼 수 있는 결론(§5). 코드 줄 추론 불필요
- [x] 정답지 세 칸(근본 food-delivery-dispatch, 계기 비움, 부분 food-delivery-order, testbed-order). class A. fault_pattern 은 P1~P8 어느 것도 맞지 않아 생략
- [x] 감지, 피해 판정, RCA 증거 구분 (§6)
- [x] 시나리오 id 흔적 없음: 패치 문자열(withinDeadline, LOOKUP_DEADLINE_MS, 'delivery list', 'delivery lookup', 'did not finish within', 'timed out', 'failed', 'interrupted')에 id, fault, bug, chaos, scenario, leak 없음(테스트로 확인), 태그 1.9.0, 새 부하 스크립트에 id 없음(태그는 러너가 붙이는 scenario_id 뿐, 기존 동반 부하와 같음)
- [x] 피해 계산: 스레드 초당 약 10개 × 약 100~125KiB, 여유 약 670MiB 를 약 9~12분에 채움(로컬 실측 §4), 재기동 41~45초 동안 기준선 주문 503 → 5xx 비율 0.5 이상 구간이 최대 틱 간격보다 김. 서킷브레이커(order 안)는 실패를 빠르게 할 뿐 dispatch 의 메모리 고갈과 무관 (§9)

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- 배포 단계에서 `bash scripts/scenarios/fault-images/build.sh f63-p` 로 109 docker 에 food-delivery-dispatch:1.9.0 이 있어야 preflight 가 통과한다(배포 스크립트가 없으면 만든다). 새 부하 스크립트 delivery-status-surge.js 가 tb-runner /opt/loadgen/food-delivery 에 올라갔는지.
- KCM 이 새 파드의 'Pulled ... food-delivery-dispatch:1.9.0' 을 잡는지, dispatch 비데몬 waiting 스레드가 분당 수백씩 직선으로 오르는지, 힙이 그대로인지.
- 한도 도달 시각과 죽는 방식(커널 OOM 인지 liveness 인지), tb-w3 syslog 에 dispatch cgroup 의 OOM 보고와 Killed process (java) 가 남는지, KCM 이 같은 파드의 재시작과 Unhealthy 를 남기는지, 운영(aarch64)의 스레드당 메모리와 실제 주기.
- 기준선 주문 5xx 비율이 구간마다 0.5 를 넘는지, order 가 Ready 를 지키고 entry_status 가 0 이 되지 않는지, 배정 중 배차가 한도 아래인지, 인시던트가 생기는지(원칙 7).
- cleanup 뒤 tb-w3 containerd 에 1.9.0 의 이름 참조와 ID 참조가 모두 남지 않았는지.
