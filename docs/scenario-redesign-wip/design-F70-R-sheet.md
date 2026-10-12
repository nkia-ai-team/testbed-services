---
title: F70-R 설계 시트 (정산 대사 시스템의 page 없는 30일 거래 내역 조회가 transfer 힙을 다 써 멈추고 재시작을 되풀이)
status: Draft
owner: project
last_reviewed: 2026-10-12
tags:
  - scenario
  - design
  - memory
  - query-of-death
summary: 가맹 정산 대사 시스템이 banking 거래 내역 API 에서 한 달치 완료 이체를 page/size 없이(API 계약상 허용) 1분마다 받아 가려 하자, 상한 없이 결과 전부를 엔티티로 만드는 transfer-service 가 약 210만 행을 512MiB 힙에 올려 전체 GC 연속과 OutOfMemoryError 로 멈추고 liveness 로 재시작되기를 되풀이해, banking 이체와 commerce checkout 이 502 가 된다. 배포, 설정, DB 변경은 없다. 원본 Cronofy 2025-04-15.
---

# F70-R 설계 시트

## 1. 요약

banking 거래 내역 API(`GET /api/transfers`)는 page/size 를 빼면 조건에 맞는 이체를 **전부** 돌려준다(`Pageable.unpaged()`, 코드 주석이 "무제한 조회 관례"라고 적는다). 평시 트래픽은 늘 `page=0&size=20` 으로 부르고, 이체 표는 약 650만 행까지 자랐다. 가맹 정산 대사 시스템 하나가 계약대로 `status=COMPLETED&from=<30일 전 0시>` 를 page 없이 부르자, transfer 는 최근 30일 완료 이체 약 210만 행을 관리 엔티티로 만들어 512MiB 힙에 올리다 전체 GC 가 잇따르고 OutOfMemoryError 로 멈춘다. api 는 10초 시간 초과 뒤 두 번 재시도해 같은 조회를 세 번까지 보낸다. 멈춘 동안 모든 이체(banking 고객, commerce 정산)가 502 로 실패하고, readiness 로 빠진 transfer 는 liveness 로 재시작된 뒤 다음 대사 호출에 다시 멈춘다.

비유: 은행 창구에 "지난달 거래 내역 전부 주세요"라는 정당한 요청이 들어왔는데, 창구 직원이 몇 장씩 끊어 주지 않고 수백만 장을 한꺼번에 책상에 쌓으려다 책상이 무너져 다른 손님 업무까지 멈춘다. 손님은 규정대로 요청했고, 고칠 곳은 "한 번에 몇 장까지"라는 상한이 없는 창구 절차다.

## 2. 원본 사례

- **Cronofy, 2025-04-15**(상태 페이지 사고 "Small number of 5XX errors in US", 사후 보고 게시 2025-04-17 11:21 UTC): <https://status.cronofy.com/incidents/ntmxwcyhpyvq>
- 요약(출처가 말한 것만): 한 고객의 Availability Query 요청 일부가 Availability 계산의 버그를 건드려 "some requests to the Availability Engine attempted to return values several orders of magnitude larger than intended", 처리에 약 20분 동안 큰 자원이 들었다. 그 요청을 맡은 서버들이 메모리가 바닥나 죽었고(17:59 UTC), 처리 중 요청이 HTTP 500(미국 요청의 1% 미만)을 받았다. 트래픽과 부하 모양은 평소와 같아 보였고, OOM 종료가 잘 드러나지 않아 진단이 늦었다. 메모리 한도를 올려도 효과가 없었고, 고객이 호출을 멈추고 수정을 배포해 해결. 재발 방지는 OOM 추적·경보, 처리 규모 제한, 허용 값 제한.
- 보조 사례(같은 기전, 참고만): Zulip Cloud 2025-01-23(<https://status.zulip.com/incidents/2bxd4xdqp99c>, 극소수 요청을 처리하다 메모리가 바닥나 서비스가 자동 재시작, 최악 9분 전면 중단).
- 자료 문서 `ref-real-world-incidents.md` M9 에 Cronofy 를 이번에 추가했다.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 한 고객의 Availability Query 호출(정상 사용) | 가맹 정산 대사 시스템이 API 계약대로 page 없이 30일 완료 이체 목록을 1분마다 요청 |
| 원인이 된 결함 | 특정 입력에서 의도보다 몇 자릿수 큰 값을 만들어 돌려주려는 엔진(처리 규모 상한 없음) | page/size 가 없으면 상한 없이 결과 전부를 엔티티로 만들어 응답 목록으로 바꾸는 transfer 목록 로직(평시 응답 20행 대비 약 210만 행) |
| 전파 경로 | 그 요청을 맡은 서버가 메모리 고갈로 죽음 → 그 서버의 처리 중 요청 500 | transfer 힙 고갈 → 전체 GC 연속과 OutOfMemoryError 로 요청 처리 정지 → account·api 를 거친 이체와 commerce 정산이 시간 초과·거절 → 502. readiness 제외, liveness 재시작 |
| 사용자 증상 | 미국 요청 일부 HTTP 500 | banking 이체 502, commerce checkout 502(한 번에 2~3분씩 되풀이). account 풀이 멈춘 이체 호출에 묶여 잔액 조회 일부도 실패. 상품 조회 정상 |
| 탐지된 경로 | 5XX 증가로 호출, 몇 서버의 이례적 메모리 사용 → 특정 고객 질의 | api 'Read timed out'(목록 호출), transfer JVM 힙 지표가 한도까지, transfer OutOfMemoryError 로그, KCM liveness 재시작, 이체 오류율 |
| 완화와 복구 | 고객이 호출 중단, 수정(처리 규모 제한) 배포 | 호출이 멈추면 liveness 재시작 뒤 정상. 근본 수리는 목록 API 상한(최대 page size, 스트리밍) — 시나리오는 cleanup 으로 호출을 멈춘다 |

기전 고리(특정 정상 요청 → 의도보다 몇 자릿수 큰 결과 처리 → 서버 메모리 고갈 → 처리 중 요청 실패)는 원본과 같다. 원본은 고객 단위 다중 서버였고 여기서는 transfer 단일 파드라 피해가 그 서비스를 거치는 모든 요청으로 넓다.

## 3. 숫자 근거 (2026-10-12, `scenario-stats.py`, 정식 + 후보 합계 104, 이 후보 전)

- 서비스: 쇼핑몰 35, 은행 **34**, 음식배달 35 → 합계가 가장 적은 은행에서 만든다(원칙 2, 2026-10-12 규칙).
- 묶음: D 16(15%), J 17(16%), P 13(12%), G 12(11%), L 8, A 7, B 7, C 6, E 4, F 3, H·I·K·O·Q 2, M 1, N 0. 20% 에 닿은 묶음 없음. 이 후보는 **A**(7 → 8, 105 중 7.6%).
- 정답 위치: 은행 이체 서비스 8(7.7%) → 9(8.6%). 20% 에 닿은 위치 없음. 결제 경로 13/105(12.4%), 이 후보와 무관.
- 은행 부품 지도: oracle 9, kafka 0, api 5, account 11, transfer 10, ledger 2, nginx 0. kafka(outbox 비동기라 사용자 증상 없음, rejected 다수)와 nginx(119 미수집, 원칙 3)는 이번에도 막혀 있다.
- 왜 이 후보인가: 은행 J(결함 릴리스)가 이미 11개라 배포가 원인이 아닌 꼴을 찾았다. 이 후보는 아무것도 바꾸지 않고 "기존 API 를 누가 어떻게 부르는가"가 계기라 J, G, P 어느 것도 아니며, 같은 증상(힙 고갈과 재시작 반복)의 F41-R(릴리스 누수)과 원인이 달라 관제 AI 가 롤아웃 유무와 힙 증가 모양으로 가려야 한다(카탈로그 관계 H 와 같은 가치).

### 3단계 후보 목록 (실제 기전 × 부품, 숫자 순, 버린 이유 포함)

| # | 부품 | 기전 × 사례 | 결과 |
|---|---|---|---|
| 1 | transfer | M9 query of death: 정상 호출이 상한 없는 목록을 불러 힙 고갈 (Cronofy 2025-04-15 공식, Zulip 2025-01-23 보조) | **채택**(A, 은행 이체 서비스 8 → 9) |
| 2 | account | 같은 기전으로 `GET /api/accounts` page 없이 | 버림: 계좌 1,001행이라 힙을 채울 수 없음(원칙 9) |
| 3 | ledger | 같은 기전으로 `GET /api/ledger-entries` page 없이 | 버림: ledger 는 사용자 경로 밖(rejected 253 행과 같은 벽, 원칙 7) |
| 4 | Oracle(DB 인스턴스) | DB 최적화 설정 변경이 역효과 (RevolutionEHR 2025-03 상태 페이지) | 버림: 무엇을 바꿨는지 미공개(원칙 1) |
| 5 | api | 고정 메모리 한도 + 이례적으로 큰 데이터 동기화로 OOMKill 반복 (Harness STO 2025-11-23 공식) | 버림: banking 에 큰 데이터 동기화 경로가 없어 계기를 지어내야 함(원칙 1) |
| 6 | transfer | readiness 프로브가 의존성을 포함해 대량 제외 (DoorDash 엔지니어링 페이지) | 버림: 공식 사후 보고 아님, 단일 replica 라 '남은 파드 과부하' 고리가 없음(원칙 1, 9) |
| 7 | api → account 서킷 | 서킷 설정 오류(4xx 를 실패로 셈) (Spreedly 2022-03-28) | 버림: 원본이 설정값 미공개, 같은 원본·같은 수단(k8s.env 서킷)이 F64-R·F64-P 에 있고 api→account 판은 반복 47 에 §1 로 막힘 |
| 8 | Oracle | 엔진 결함으로 질의 메모리 미계산 (Bitmovin 2026-07-27 RCA) | 버림: DB 엔진 결함 재현 불가(원칙 1) |
| 9 | Oracle | 메모리보다 큰 질의로 DB 프로세스 종료 (Pipefy 상태 페이지) | 버림: 엔진·설정 미공개(원칙 1) |
| 10 | nginx | 프록시 본문·시간 제한 설정 | 버림: nginx 가 119 에 남지 않음(원칙 3, rejected 59·238 행) |
| 11 | kafka | 브로커·토픽 설정 | 버림: banking 발행이 outbox 비동기라 사용자 증상 없음(원칙 7, rejected 258 행) |

같은 꼴의 이전 시도: F20-Q(food order 무제한 조회 힙 압박, 원본 사례 없음)는 2026-10-08 "관리 제외"로 폐기됐다(원칙 위반이 아니라 사용자가 모르는 옛 시나리오 정리). 이번 실행의 막힌 목록(반복 57: transfer 거래 내역 페이지 처리 제거 릴리스, 반복 58: api 거래 내역 페이지 인자 제거 릴리스)은 같은 힙 고갈이지만 계기가 릴리스였고 공식 사례가 없어 막혔다. 이번 후보는 계기가 릴리스가 아니라 정상 호출이고 공식 원본(Cronofy)이 있다.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 대사 시스템(k6 VU 1) → `nginx` `/api/transfers` → api `TransferController.list` → `ApiService.listTransfers` → `core-banking/api-service/src/main/java/com/corebanking/api/client/TransferClient.java:32-50` (`listTransfers`, 질의 문자열을 그대로 넘김, `@Retry(transferClient)`, 실패 로그 :41)
2. api `application.yml:13-16`(transfer read-timeout 10s), `:48-49`(retry max-attempts 3) → 10초 뒤 'Transfer service list call failed: I/O error on GET request for "http://testbed-transfer:8082/api/transfers": Read timed out' 과 재시도 2회. 서버 쪽 transfer 는 앞선 조회를 계속 처리하므로 같은 조회가 최대 3건 겹친다.
3. transfer `core-banking/transfer-service/src/main/java/com/corebanking/transfer/controller/TransferController.java:46-66` (`list`, :62 `Pageable.unpaged()`) → `TransferService.java:120-127` (`search`, 상한 없이 `getContent()` 를 응답 목록으로) → `TransferRepository.java:17-28` (OR-null JPQL, unpaged 면 `fetch first` 와 count 질의가 없다)
4. BANKING.TRANSFERS 약 650만 행, 최근 30일 완료 약 220만 행(109 Oracle 읽기 전용 실측, 2026-10-12) → 관리 엔티티 약 210만 개
5. `core-banking/k8s/22-transfer-service.yaml:63-64` (`-Xmx512m`), `:105-110` (limits cpu 500m, memory 1Gi, Serial GC) → 힙 고갈, 전체 GC 연속, OutOfMemoryError
6. `:87-104` readiness `/actuator/health`(10초 x 3), liveness `/actuator/health/liveness`(15초 x 5) → Service 제외, kubelet 재시작
7. 그동안 account `TransferClient`(read-timeout 15s, 재시도 3, 서킷) → api `AccountClient`(10s, 재시도 3, 서킷) → 이체 502. commerce payment → transfer 정산 실패 → checkout 502.
8. account `AccountService.java:57-80` (`validateAndForward`, `@Transactional(readOnly = true)` 안에서 transfer 호출) → 멈춘 호출마다 account Hikari 연결(최대 10, `application.yml:13-15` connection-timeout 3000)을 최대 15초(재시도까지 약 45초) 쥔다. 이체가 초당 2건 넘게 들어오므로 서킷(최소 5회)이 열리기 전에 풀이 말라 account 'Connection is not available, request timed out after 3000ms', Servlet.service CannotCreateTransactionException, 잔액 조회 일부 실패, readiness(`/actuator/health`, DB 포함) 잠깐 실패가 따라온다. liveness(`/actuator/health/liveness`)는 DB 를 보지 않아 account 는 재시작되지 않는다. 선례: F42-R 실행 26143861(transfer 가 느리기만 했던 경우)에서 잔액 조회 실패율 0.16~0.39, account CannotCreateTransactionException 2,501건, readiness 'context deadline exceeded' 10회.

### 로컬 실측 (2026-10-12, 원칙 9 근거)

- 구성: origin/main 의 transfer-service 를 그대로 빌드한 jar, `eclipse-temurin:17-jre` 컨테이너 `--cpus 0.5 -m 1g`, `JAVA_TOOL_OPTIONS=-Xmx512m`(운영과 같은 한도, OTel 에이전트 없음 — 운영은 에이전트만큼 힙 여유가 더 적다), `gvenzl/oracle-free:23-slim` 에 banking `init.sql` 과 이체 260만 행(최근 30일 223만, 운영 220만과 같은 규모).
- 배경 부하: 이체 POST 초당 5건 + 페이지 조회 초당 약 1.7건.
- 시험 1(api 재시도처럼 10초 시간 초과 뒤 2회 재시도): 첫 조회 뒤 20초에 이체 p95 1.8초, 30초에 4.4초, 40초에 이체 실패 시작, 60초부터 이체 전부 시간 초과. transfer 로그 'Exception in thread "http-nio-8082-Acceptor" java.lang.OutOfMemoryError: Java heap space', 'Servlet.service() ... [Handler dispatch failed: java.lang.OutOfMemoryError: Java heap space]', Hikari 'Connection is not available ... (total=15, active=15)'. 조회가 끝난 뒤에도 `/actuator/health/liveness` 가 응답 없음(5초 시간 초과), CPU 0.5 를 GC 가 계속 씀 → 쿠버네티스에서는 liveness 재시작이 회복 경로.
- 시험 2(재시도 없이 한 건, 시간 제한 150초): 첫 조회 뒤 약 50초에 이체 응답이 끊기고 같은 상태로 멈춤. **한 건으로도 충분**하다.
- 시험 3(새 부하 스크립트 `statement-export.js` 를 k6 0.54 로 그대로 실행): 경사 20초, 유지 150초, 재조회 30초로 같은 멈춤과 OutOfMemoryError 29줄.

## 5. 원인 규정 (원칙 5, 6)

- `root_cause.target_id`: `core-banking-transfer`. 결함은 상한 없는 목록 로직이다. 대사 호출은 공개 계약(page/size 선택, 빼면 전부) 안의 정당한 요청이므로 원칙 6 의 "A 가 정당하고 B 의 로직이 잘못" 경우다. 원본도 고객 질의가 아니라 엔진의 처리 규모를 고쳤다.
- `trigger_target_id`: 비움. 계기는 바깥 호출자(대사 시스템)라 이 테스트베드 부품 이름이 없다.
- `scoring.partial`: `core-banking-api`(질의를 넘기고 재시도로 세 배로 늘림), `core-banking-account`(증상 서비스), `banking-oracle`(무거운 조회를 받은 DB).
- 원칙 5: 정답은 "transfer 가 거래 내역 목록 호출을 처리하다 힙이 바닥나 멈췄다"이고 관제 데이터(OOM 로그, JVM 힙 지표, 목록 호출 시간 초과 로그, liveness 재시작)로 바로 낼 수 있다. 코드 설계 추론(비멱등, 경쟁 조건)이 필요하지 않다. 상한이 없다는 사실은 피해 설명(왜 한 호출로 무너지는가)이다.

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 |
|---|---|
| 감지 | api, account 의 ERROR 로그 급증(이체 502, 서킷 열림), transfer SEVERE OutOfMemoryError, account·api 서버 스팬 오류율, KCM Unhealthy·Killing. 별도 업무 규칙 없이 이벤트와 인시던트가 생기는 겉 증상 |
| 피해 판정(러너) | 동반 부하 이체(step transfer) 5xx 비율 ≥ 0.5(평시 0), 배제는 부하 미전달과 Oracle NotReady 뿐. 잔액 조회 실패율은 예상 전파(위 8)라 기록만 한다(F42-R 과 같음) |
| RCA | 표 7 의 순서: api 목록 호출 'Read timed out' → transfer 힙 지표 한도 → transfer OOM 로그 → liveness 재시작, 롤아웃·설정 변화 없음 |

## 7. 관측 근거 표 (119 실조회, 2026-10-12 08:00~08:30 UTC, 평시)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 핵심 증거는 전수 로그, JVM 지표, KCM 이벤트다.

| 증거 | 119 위치 | 실제 조회와 결과 |
|---|---|---|
| 계기: 목록 호출 시간 초과 | CH `lucida_logs_local` body (전수) | `SELECT service_name, count() FROM lucida.lucida_logs_local WHERE timestamp > now() - INTERVAL 30 DAY AND service_name LIKE 'core-banking%' AND body LIKE '%Read timed out%' GROUP BY 1` → 0행(로그 보존 2026-10-05~). 같은 로그 형식은 있음: 'Transfer service list call failed: I/O error on GET request for "http://testbed-transfer:8082/api/transfers": Connection refused' 1,080건(다른 시나리오). URL 에 질의 문자열은 실리지 않는다 |
| 계기: page 없는 목록 요청 | CH `otel_traces_local` span_attributes['url.query'] (표본 10%) | `SELECT position(span_attributes['url.query'],'page=')>0, count(), max(duration_ns)/1e9 FROM lucida.otel_traces_local WHERE timestamp > now() - INTERVAL 7 DAY AND service_name='core-banking-transfer' AND span_name='GET /api/transfers' GROUP BY 1` → page 있음 16,301건, page 없음 0건, 가장 긴 9.3초. 보조 증거(표본) |
| 계기: DB 쪽 상한 없는 조회 | CH `dpm_topsql_local`(분 단위 delta, sql_id 별 실행·논리 읽기), `dpm_session_local`(machine=파드 이름) | 평시 페이지 조회 문장은 `... fetch first ? rows only`(트레이스 db.statement). 평시 sql 99nsgbv90qrn7 avgLogicalReads 약 10. 무제한 조회는 `fetch first` 없는 다른 sql_id 로 Top SQL 에 잡힌다(장애 시 확인) |
| 근본: transfer 힙 | VM `apm.agent.otel.java.jvm.memory.used`, `jvm.memory.used_after_last_gc`, `jvm.memory.limit` (service_name core-banking-transfer, 10초 내보내기, 표본 아님) | Tenured Gen 최근 1시간 최대 97.0MiB, 6시간 GC 뒤 최대 58.2MiB, 한도 341.4MiB(Eden 136.5 + Survivor 17.1). 장애 시 한도까지 |
| 근본: OOM | CH `lucida_logs_local` severity SEVERE, log_attributes['exception.type'] | `SELECT service_name, severity_text, substring(body,1,120), log_attributes['exception.type'], count() FROM lucida.lucida_logs_local WHERE timestamp > now() - INTERVAL 7 DAY AND service_name LIKE 'core-banking%' AND (body LIKE '%OutOfMemoryError%' OR log_attributes['exception.type'] LIKE '%OutOfMemory%') GROUP BY 1,2,3,4` → 수집되는 형식은 둘: SEVERE 'Servlet.service() for servlet [dispatcherServlet] in context with path [] threw exception [Handler dispatch failed: java.lang.OutOfMemoryError: Java heap space] with root cause' 3건, SEVERE 'Failed to complete processing of a request'(exception.type java.lang.OutOfMemoryError) 3건, 모두 core-banking-account(2026-10-11 F41-R 실행). transfer 0건. 로컬에서 본 'Exception in thread "http-nio-8082-Acceptor" java.lang.OutOfMemoryError' 는 JVM 기본 처리기가 stderr 에만 쓰고 119 는 컨테이너 stdout/stderr 를 모으지 않아(보존 구간 'Exception in thread' 0건) 근거로 쓰지 않는다 |
| 전파: 재시작 | CH `kcm_events_local` (namespace rca-testbed-banking, reason Unhealthy/Killing) | 7일 Unhealthy 242건, Killing 22건 수집(다른 시나리오). 예: testbed-account 'Readiness probe failed: ... context deadline exceeded'. 롤아웃이 없으면 ScalingReplicaSet 이 없어야 한다 |
| 피해 | CH 로그(account 'Transfer service call failed', api 서킷, commerce 'core-banking transfer call failed'), APM 서버 스팬 오류율 | 같은 경로의 오류 로그 형식은 F65-R 등에서 수집 확인 |
| 배제: Oracle, account 자체 장애 | Oracle 파드 Ready(러너 kubernetes.pod_ready), account 재시작 없음(KCM), account 실패 로그의 대상 URL testbed-transfer | account 풀 고갈은 예상 전파라 배제 근거가 아니다 |

## 8. 감별

- must_support: 정답지 `must_support` 5개(목록 호출 'Read timed out' 이 먼저, transfer Tenured Gen 이 호출 1분 안에 한도, transfer OOM 로그, KCM liveness 재시작 반복과 롤아웃 없음, 이체 5xx 와 account 풀 고갈의 예상 전파(대상 URL testbed-transfer, account 재시작 없음)).
- must_rule_out: 릴리스 누수(F41-R, F63-R, F51-H: 롤아웃, 몇 분에 걸친 증가), 설정 롤아웃(F65-R, F64-P), Oracle·잠금(F35-R, F01-P, F57-R), 부하 폭주·배치(F07-H, F59-P), account·api 자체 장애.
- contrast_with: F41-R, F63-R, F59-P, F20-Q, F65-R.
- F41-R 과 가르는 관측: ① KCM ScalingReplicaSet·새 이미지 Pulled 유무 ② 힙 증가 기울기(F41-R 은 분당 약 30MB 로 몇 분, F70-R 은 호출 뒤 1분 안에 한도) ③ 첫 오류 전 api 'Transfer service list call failed ... Read timed out' 유무 ④ 피해 서비스(account 대 transfer).

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

- 주입: `load.north_south` primary, `statement-export.js`, target_rps 5(동반 여정), entry 30082, 경사 2m, 유지 13m, 내림 1m(부하 960초 = 레벨 timeout 16m, primary 부하 계약), seed 7070.
- 대사 VU 1, 60초 쉼. 한 주기: 호출 → 약 50초에 멈춤 → api 는 31초에 502 를 돌려주고 클라이언트는 60초 쉼 → liveness 15초 x 5 → 재기동 약 60~90초 → 다음 호출. 주기 약 4분 중 실패 2~3분.
- 성공: 동반 부하 이체 5xx ≥ 0.5, 3틱(45초). 멈춘 동안 account → transfer 호출이 15초 시간 초과·재시도·서킷 열림, api 가 502 를 31초 안에 돌려줘 k6 기본 시간 제한(60초) 안에 5xx 로 셈한다.
- min_hold 10m: 경사 2분 뒤 첫 호출, 10분이면 세 번째 안팎의 실패 구간에서 판정.
- 배제: achieved_rps < 1.25, Oracle NotReady. 잔액 조회 실패율 배제는 두지 않는다(account 풀이 멈춘 transfer 호출에 묶이는 예상 전파, F42-R 선례). 중단: entry_status 0(nginx, api, account 는 살아 502 로 답한다).
- 회복: transfer Ready, available 1, 기준선 이체 5xx < 0.05(2틱), 상한 12m. cleanup 뒤에도 이미 들어간 조회가 transfer 를 멈춰 있을 수 있어 liveness 재시작(약 3분)을 기다린다.
- 테스트베드 한계: 대사 호출은 분당 1건(재시도 포함 3건)이라 입구 요청률이 평시 + 5rps 뿐이다. Oracle 은 full scan 몇 건을 받는다(평시 페이지 조회도 같은 OR-null 문장). 필수 중단 조건(입구 0, 노드, Oracle)에 걸리지 않는다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Cronofy 2025-04-15, 공식 상태 페이지 사후 보고, 링크)와 요소별 대응표가 있고 기전(정상 호출 → 의도보다 몇 자릿수 큰 결과 처리 → 메모리 고갈 → 처리 중 요청 실패)이 같다 (원칙 1)
- [x] 분류 장부를 갱신했다. 묶음 A 7 → 8(7.6%), 정답 위치 은행 이체 서비스 8 → 9(8.6%), 결제 경로 13/105(12.4%), 서비스 은행 34 → 35(가장 적은 서비스). 어느 축도 20% 아래 (원칙 2)
- [x] 근본 원인 위치: `TransferController.java:46-66`(:62 unpaged), `TransferService.java:120-127`, `TransferRepository.java:17-28`, `22-transfer-service.yaml:63-64`(-Xmx512m) (G1)
- [x] 근본 원인의 흔적: VM transfer JVM 힙 지표(평시 값 조회), CH 로그 OOM 수집 경로(SEVERE Servlet.service 와 'Failed to complete processing of a request'(exception.type java.lang.OutOfMemoryError) 수집 확인, stderr 의 acceptor 스레드 줄은 수집 안 됨) (원칙 3)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: 로그(전수), JVM 지표(표본 아님), KCM 이벤트. url.query 트레이스는 보조 (원칙 3)
- [x] 계기의 흔적: api 'Transfer service list call failed ... Read timed out'(평시 0), DPM Top SQL 의 fetch first 없는 조회. 인공 지연 없음 (원칙 4)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다(목록 호출 처리 중 힙 고갈로 transfer 멈춤) (원칙 5)
- [x] 정답지 세 칸: target_id core-banking-transfer, trigger_target_id 비움(바깥 호출자), partial api·account·oracle (원칙 6)
- [x] 감지, 피해 판정, RCA 증거가 6절에 따로 있다 (원칙 7)
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: 요청 경로와 질의 문자열에 id 없음, k6 태그는 러너 안의 지표에만 붙고, User-Agent 는 상주 기준선과 같은 k6 (원칙 8)
- [x] 부하 상한(동반 5rps, 대사 분당 1건)과 서킷(account·api, 최소 5회 50% 5초)을 넣어 피해 계산, 로컬 실측으로 한 건만으로도 멈춤을 확인 (원칙 9)

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- transfer Tenured Gen 이 호출 뒤 한도에 닿는지, OTel 지표가 멈추기 직전까지 내보내지는지.
- OutOfMemoryError 가 SEVERE 'Servlet.service() ...' 나 'Failed to complete processing of a request' 로 119 에 들어오는지(멈춘 JVM 의 로그 내보내기가 늦거나 빠질 수 있다. 빠지면 JVM 지표와 KCM 이 남는다).
- account 풀 고갈로 잔액 조회 실패율이 얼마나 오르는지(배제 조건이 아니라 기록용).
- DPM Top SQL 에 `fetch first` 없는 transfers 조회 sql_id 가 잡히는지.
- api 재시도 3건이 transfer 에 실제로 겹치는지(서킷이 열리면 줄어든다), 주기마다 실패 구간이 서는지.
