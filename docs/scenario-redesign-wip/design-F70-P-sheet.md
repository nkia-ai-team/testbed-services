---
title: F70-P 설계 시트 (배달원 정산 시스템의 page 없는 배달 완료 조회가 dispatch 힙을 다 써 멈추고 재시작을 되풀이, 새 주문 503)
status: Draft
owner: project
last_reviewed: 2026-10-12
tags:
  - scenario
  - design
  - memory
  - query-of-death
summary: 배달원 정산 시스템이 food 배달 목록 API 에서 배달 완료 내역을 page/size 없이(API 계약상 허용) 약 2분 반마다 받아 가려 하자, 상한 없이 결과 전부를 엔티티로 만드는 dispatch-service 가 약 174만 행을 기본 힙(약 247MiB)에 올려 연속 GC 로 멈추고 liveness 로 재시작되기를 되풀이해, order 의 배달원 용량 확인이 실패하고 모든 새 주문이 503 이 된다. 배포, 설정, DB 변경은 없다. 원본 Cronofy 2025-04-15, F70-R 의 음식배달 판.
---

# F70-P 설계 시트

## 1. 요약

food 배달 목록 API(`GET /api/deliveries`)는 page/size 를 둘 다 빼면 조건에 맞는 배차를 **전부** 돌려준다(`Pageable.unpaged()`, 코드 주석이 "둘 다 없으면 무제한"이라고 적는다). 평시 트래픽은 늘 `status=DELIVERED&page=0&size=20` 으로 부르고, 배차 표에는 배달 완료가 약 174만 행 있다. 배달원 정산 시스템 하나가 계약대로 `status=DELIVERED` 를 page 없이 부르자, dispatch 는 약 174만 행을 MySQL 에서 통째로 받아 관리 엔티티로 만들려다 기본 힙(-Xmx 없이 1GiB 컨테이너의 25%, 약 247MiB)이 차서 약 10~15초 만에 연속 전체 GC 로 멈춘다. 멈춘 동안 order 가 주문 저장 전에 부르는 배달원 용량 확인이 시간 초과와 서킷 열림으로 끝나 모든 새 주문이 503 이고, readiness 로 빠진 dispatch 는 liveness 로 재시작된 뒤 다음 정산 호출에 다시 멈춘다.

비유: 배달 대행사 사무실에 "지난달 배달 완료 내역 전부 주세요"라는 정당한 요청이 들어왔는데, 배차 담당자가 몇 장씩 끊어 주지 않고 수백만 장을 한꺼번에 책상에 쌓으려다 책상이 무너져 새 배달 접수(배달원 남았나요?)까지 멈춘다. 고칠 곳은 "한 번에 몇 장까지"라는 상한이 없는 창구 절차다.

## 2. 원본 사례

- **Cronofy, 2025-04-15**(상태 페이지 사고 "Small number of 5XX errors in US", 사후 보고 게시 2025-04-17 11:21 UTC): <https://status.cronofy.com/incidents/ntmxwcyhpyvq>
- 요약(출처가 말한 것만, 자료 문서 M9 에 이미 있음): 한 고객의 Availability Query 요청 일부가 Availability 계산의 버그를 건드려 "some requests to the Availability Engine attempted to return values several orders of magnitude larger than intended", 그 요청을 맡은 서버들이 메모리가 바닥나 죽었고(17:59 UTC), 처리 중 요청이 HTTP 500 을 받았다. 트래픽과 부하 모양은 평소와 같아 보였고, OOM 종료가 잘 드러나지 않아 진단이 늦었다. 메모리 한도를 올려도 효과가 없었고, 고객이 호출을 멈추고 수정을 배포해 해결. 재발 방지는 OOM 추적·경보, 처리 규모 제한, 허용 값 제한.
- 같은 원본의 앞선 재구성: F70-R(banking transfer 거래 내역 API). F70-P 는 같은 결함 꼴을 음식배달 배차 서비스에서 재구성한다. 다른 점은 DB(MySQL, Connector/J 가 결과 집합을 통째로 받음), 힙 크기(-Xmx 없이 기본 25%), 피해 경로(order 가 주문 저장 전에 부르는 용량 확인 실패로 새 주문 503, 결제 단계까지 가지 않음, 게이트웨이 없음)다.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 한 고객의 Availability Query 호출(정상 사용) | 배달원 정산 시스템(바깥 파트너)이 API 계약대로 page 없이 배달 완료 내역 전부를 약 2분 반마다 요청(시간 제한 90초, 실패 뒤 60초 쉼) |
| 원인이 된 결함 | 특정 입력에서 의도보다 몇 자릿수 큰 값을 만들어 돌려주려는 엔진(처리 규모 상한 없음) | page/size 가 없으면 상한 없이 결과 전부를 엔티티로 만들어 응답 목록으로 바꾸는 dispatch 목록 로직(평시 응답 20행 대비 약 174만 행) |
| 전파 경로 | 그 요청을 맡은 서버가 메모리 고갈로 죽음 → 그 서버의 처리 중 요청 500 | dispatch 힙 고갈 → 연속 전체 GC 로 요청 처리 정지 → order 의 용량 확인 시간 초과·서킷 열림 → 새 주문 503. readiness 제외, liveness 재시작, 재시작 동안 연결 거부 |
| 사용자 증상 | 미국 요청 일부 HTTP 500 | 새 음식 주문 전부 503(한 번에 약 2분씩 되풀이), 배달 추적 시간 초과. 가게, 메뉴 조회 정상 |
| 탐지된 경로 | 5XX 증가로 호출, 몇 서버의 이례적 메모리 사용 → 특정 고객 질의 | order 'Failed to check dispatch capacity ... Read timed out', dispatch JVM 힙 지표가 한도까지, KCM liveness 재시작, DPM 의 LIMIT 없는 배차 조회, 주문 오류율 |
| 완화와 복구 | 고객이 호출 중단, 수정(처리 규모 제한) 배포 | 호출이 멈추면 liveness 재시작 뒤 정상. 근본 수리는 목록 API 상한(최대 page size, 스트리밍) — 시나리오는 cleanup 으로 호출을 멈춘다 |

기전 고리(특정 정상 요청 → 의도보다 몇 자릿수 큰 결과 처리 → 서버 메모리 고갈 → 처리 중 요청 실패)는 원본과 같다. 원본은 고객 단위 다중 서버였고 여기서는 dispatch 단일 파드라 피해가 그 서비스를 거치는 모든 요청(주문의 용량 확인, 배달 추적)으로 넓다.

## 3. 숫자 근거 (2026-10-12, `scenario-stats.py`, 정식 + 후보 합계 107, 이 후보 전)

- 서비스: 쇼핑몰 36, 은행 36, 음식배달 **35** → 합계가 가장 적은 음식배달에서 만든다(원칙 2, 2026-10-12 규칙).
- 묶음: J 17(15.9%), D 16(15.0%), P 13, G 12, A 8, L 8, B 7, C 6, E 4, F·K·O 3, H·I·Q 2, M 1, N 0. 20% 에 닿은 묶음 없음. 이 후보는 **A**(8 → 9, 108 중 8.3%).
- 정답 위치: 배달 서비스 6(5.6%) → 7(6.5%). 20% 에 닿은 위치 없음. 결제 경로 13/108(12.0%), 이 후보와 무관.
- 음식배달 부품 지도: mysql 15, kafka 0, order 4, restaurant 4, dispatch 12, payment 5, notify 1, external-pg-mock 3. kafka 와 notify 는 outbox 비동기 경로라 사용자 증상이 서지 않아(아래 후보 3, 11) 이번에도 막혔다.
- 왜 이 후보인가: 음식배달은 주문, 가게 입구가 DB 를 보는 health 로 묶여 DB 전체 장애나 입구 서비스의 느려짐이 곧 입구 0(필수 중단)이 되고, 결제 쪽은 결제 경로 상한과 배차 한도 누수에 걸린다. 그 안에서 공식 원본이 있고 피해가 빠르고 분명한 비입구 서비스 후보를 찾았다. 이 후보는 아무것도 바꾸지 않고 "기존 API 를 누가 어떻게 부르는가"가 계기라 J, G, P 어느 것도 아니며, 같은 dispatch 가 메모리로 죽고 주문이 503 인 F63-P(릴리스 스레드 누수)와 원인이 달라 관제 AI 가 롤아웃 유무, 힙 증가 모양, 차오르는 메모리 종류로 가려야 한다(카탈로그 관계 H 와 같은 가치).

### 3단계 후보 목록 (실제 기전 × 부품, 버린 이유 포함)

| # | 부품 | 기전 × 사례 | 결과 |
|---|---|---|---|
| 1 | dispatch(배달 서비스) | M9 query of death: 정상 호출이 상한 없는 배달 목록을 불러 힙 고갈 (Cronofy 2025-04-15 공식) | **채택**(A, 배달 서비스 6 → 7) |
| 2 | payment(결제 서비스) | 같은 기전으로 `GET /api/payments` page 없이 | 버림: 결제 경로 합계(12%)에 더하고, payment 가 GC 로 멈추면 order 가 결제 호출(읽기 10초 x 2)을 기다리며 주문 트랜잭션의 연결을 쥐며, 결제는 배차 뒤 단계라 실패 주문마다 배차가 남아 한도 소진(503)이 섞인다(rejected 293행과 같은 벽, 원칙 9) |
| 3 | food Kafka(메시지 브로커 0) | 브로커 멈춤 → dispatch outbox 릴레이가 공용 스케줄러 스레드를 붙잡아 만료 배치 정지 → 배차 한도 소진 (Honeycomb 2025-12-05 공식, F23-H 폐기 원본) | 버림: 한도 도달 시간이 그때 주문률에 따라 몇 분~두 시간 넘게 흔들리고(반복 62 의 E2B 판과 같은 벽), 동반 주문 부하를 더하면 그 부하만으로 한도가 찬다(원칙 9) |
| 4 | payment → 외부 PG 서킷 | 느린 호출 임계 오설정 (Spreedly 2022-03-28 공식, 원본이 Adyen 게이트웨이 앞 서킷) | 버림: 평시 PG 호출 p50 2.2ms, p99 4.2ms(119 7일 1일 표본)라 임계를 1~2ms 로 지어내야 하고, 기준선 주문률(초당 0.2~1.2)에서는 반열림 3회가 통과해 실패 비율이 약 25~67% 로 흔들림(원칙 9). 같은 원본, 같은 수단(k8s.env 서킷)이 F64-R, F64-P 에 있고 결제 경로에 더함 |
| 5 | DB 테이블(배차 이력) dispatch_events | 인덱스 제거 마이그레이션 (Buildkite 2025-11-10 공식) | 버림: dispatch_id 로 읽는 경로가 배달 이력 API(평시 부하 없음)와 6초 주기 보존 정리 DELETE 뿐이라 피해가 잠금 창의 1~3초 지연에 그침(rejected 264행 order_items 판과 같은 벽, 원칙 9) |
| 6 | DB 테이블(가게, 메뉴) 부모 행 | 일괄 정정 작업의 잠금이 프레임워크 때문에 관련 행까지 번져 운영 쓰기가 막힘 (Sendcloud 2025-09-10 공식 상태 페이지 https://status.sendcloud.com/incidents/7j75dwr15nrq ) | 버림: food 에서 부모 행 잠금이 막는 것은 orders, order_items INSERT(외래 키 공유 잠금)라 order 가 주문 트랜잭션 안에서 잠금 대기(최대 50초)로 연결을 쥐어 order 풀과 readiness 가 빠지고 입구가 0 이 된다(컨트롤러 필수 중단 조건) |
| 7 | DB 인스턴스 | 한 앱의 풀 상한이 너무 커서 DB 연결 한도를 다 씀 (Resend 2026-02-15 공식 https://www.resend.com/blog/incident-report-for-february-15-2026 ) | 버림: 연결 한도 고갈은 어느 풀이 굶을지 무작위이고 order 가 굶으면 입구 0(rejected 12행 max_connections 판과 같은 벽, 필수 중단) |
| 8 | 노드 네트워크(tb-w3) | 경로 MTU 블랙홀로 큰 패킷만 사라져 DB 연결 시간 초과 (Linode 2026-07 상태 페이지, 잠정 보고) | 버림: 원본이 "investigation is ongoing" 인 잠정 보고(원칙 1), 파드 veth MTU 변경은 119 에 흔적이 없고(원칙 4), DB 응답이 큰 restaurant 는 소켓이 멈춰 풀이 차 입구 0 |
| 9 | food MySQL(내부 배치 작업) | 이전 Job 이 DB CPU 를 다 씀 (Slack 2025-11-01 공식 https://slack-status.com/2025-11-01 ) → MySQL 에 직접 무거운 집계를 도는 이전 Job | 버림: 0.5 CPU MySQL 이 Job 질의로 포화돼도 짧은 OLTP 질의는 CFS 몫으로 ms~수십 ms 느려질 뿐 시간 제한(3~5초)에 닿지 않음(원칙 9. 이번 로컬 실측에서도 MySQL CPU 한도 구간 동안 다른 질의는 끝남). F59-R 은 dispatch 풀을 통해서만 피해가 났다 |
| 10 | order(주문 서비스) | 요청마다 불필요한 트랜잭션을 열어 연결 풀 고갈 (incident.io 2023-04-20 공식 블로그 https://incident.io/blog/database-performance ) | 버림: 원본은 짧은 트랜잭션이 많이 쌓인 꼴이고 무엇을 감쌌는지 밝히지 않음(원칙 1). food order, payment 는 평시 설계부터 트랜잭션 안에서 하류를 불러 '새로 생긴 불필요한 트랜잭션'으로 재구성할 자리가 없고, order 풀이 차면 입구 0 |
| 11 | notify(알림 서비스) | 소비자 결함, 독 메시지 | 버림: 사용자 경로 밖 비동기 소비자(원칙 7, rejected 98·153행) |

같은 꼴의 이전 시도: F20-Q(food order 무제한 조회 힙 압박, 원본 사례 없음)는 2026-10-08 "관리 제외"로 폐기됐다(원칙 위반이 아니라 사용자가 모르는 옛 시나리오 정리). F70-P 는 공식 원본(Cronofy)이 있고, 계기가 릴리스가 아니라 정상 호출이며, 대상이 order(입구)가 아니라 dispatch(비입구)라 입구 0 필수 중단에 걸리지 않는다.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 정산 시스템(k6 VU 1) → dispatch NodePort 30182 → `food-delivery/dispatch-service/src/main/java/com/fooddelivery/dispatch/controller/DispatchController.java:30-41` (`getDeliveries`, :30 주석 "둘 다 없으면 무제한", :38 `Pageable.unpaged()`)
2. `DispatchService.java:86-92` (`searchDispatches`, 상한 없이 `getContent()` 를 응답 목록으로) → `DispatchRepository.java:16-17` (`findByStatus` Slice 파생 질의, unpaged 면 `LIMIT` 이 없다) → MySQL `SELECT ... FROM dispatches d1_0 WHERE d1_0.status = ?`(평시 페이지 문장은 끝에 `LIMIT ?`)
3. fooddelivery.dispatches 약 174만 행, 그중 DELIVERED 1,743,131 행, ASSIGNED 1,407 행(109 MySQL 읽기 전용 실측, 2026-10-12 12:05 UTC). MySQL Connector/J 는 기본 설정에서 결과 집합 전체를 클라이언트 메모리에 받는다.
4. `food-delivery/k8s/22-dispatch-deploy.yaml:49-50` (JAVA_TOOL_OPTIONS 는 OTel 에이전트뿐, -Xmx 없음), `:87-93` (limits cpu 500m, memory 1Gi) → JVM 기본값으로 힙 최대 약 247MiB(119 VM jvm.memory.limit: Eden 68.3, Survivor 8.5, Tenured Gen 170.7MiB), CPU 2개 미만이라 Serial GC.
5. `:67-86` startup, readiness(10초 x 3, 시간 제한 3초), liveness(15초 x 5, 시간 제한 3초) 모두 `/actuator/health` → Service 제외, kubelet 재시작. 재기동에서 Ready 까지 41~45초(119 KCM, F63-P 측정).
6. 그동안 order `OrderService.java:94-112` (createOrder 의 용량 확인, 주문 행 저장 전) → `DispatchClient.java:32-50` (`checkCapacity`, `@Retry(dispatch)`, `@CircuitBreaker(dispatch)`, 실패 로그 :41) → `order-service/src/main/resources/application.yml:55-58` (dispatch read-timeout 5s), `:78-86` (서킷 최소 5회, 50%, 열림 5초), `:105-111` (재시도 3회, 200ms 지수) → 'Failed to check dispatch capacity: I/O error on GET request for "http://testbed-dispatch:8082/api/deliveries/capacity": Read timed out' 뒤 서킷 열림 → 503 'Dispatch service unreachable'/'Dispatch service unavailable'. 재시작 동안은 'Connection refused'.
7. order 는 주문 트랜잭션(`@Transactional`, :57) 안에서 용량 확인을 기다려 주문마다 연결 하나를 최대 약 15.6초 쥐지만, 기준선 주문은 초당 0.2~1.15건이고 서킷이 다섯 번째 실패에서 열려 order 풀(15)은 차지 않는다(F63-P, F60-H 와 같은 구간). 주문 행과 배차는 용량 확인 뒤에 쓰이므로 실패 주문이 아무것도 남기지 않는다.

### 로컬 실측 (2026-10-12, 원칙 9 근거)

- 구성: origin/main 의 dispatch-service jar 그대로, `eclipse-temurin:17-jre` 컨테이너 `--cpus 0.5 -m 1g`, -Xmx 없음(운영과 같음: `MaxHeapSize 268435456`, `UseSerialGC true` 확인), `outbox.relay.enabled=false`(Kafka 없음), OTel 에이전트 없음(운영은 에이전트만큼 힙 여유가 더 적다). `mysql:8.0` `--cpus 0.5 -m 1g`(buffer pool 128MB, 운영과 같음)에 food `init.sql` 과 dispatches DELIVERED 1,743,000 행, ASSIGNED 1,400 행.
- 시험 1(배경: 용량 확인 초당 2건 시간 제한 5초 + 페이지 목록 초당 1건, 60초 뒤 page 없는 조회 한 건, 클라이언트 시간 제한 90초): 조회 12:12:33 → 12:12:45 구간부터 용량 확인 대부분 5초 시간 초과, 12:13:00 부터 전부 시간 초과, 12:13:30 부터는 연결도 받지 않음(k6 'dial: i/o timeout'). `/actuator/health` 는 12:12:51 부터 3초 시간 초과로 실험 끝(12:21)까지 돌아오지 않음. dispatch CPU 는 0.5 한도에서 5분 넘게 GC. 로그: stderr 'Exception in thread "http-nio-8082-Acceptor" java.lang.OutOfMemoryError: Java heap space', 'Exception in thread "http-nio-8082-Poller" ...', 요청 스레드 ERROR 'Failed to complete processing of a request'(java.lang.OutOfMemoryError: Java heap space), SqlExceptionHelper 'SQL Error: 0, SQLState: S1000' / 'Java heap space', 'Failed to release JPA EntityManager' — OOM 줄들은 조회 약 3분 뒤(12:15:30~)에야 찍혔다. 쿠버네티스에서는 그 전에 liveness 가 재시작하므로(멈춘 뒤 약 75초) 운영에서 OOM 로그가 119 에 남지 않을 수 있다. 근거는 JVM 지표, KCM, order 로그, DPM 이다.
- 시험 2(배경 부하 없이 조회 한 건, MySQL 영향 측정): MySQL CPU 는 행을 보내는 처음 약 10초 동안 한도(0.5)에 닿고, 그 뒤 dispatch 가 GC 로 읽기를 멈추자 그 세션은 'Sending to client' 로 약 75초 기다렸다(그동안 MySQL CPU 약 0.005). 다른 질의를 막는 잠금은 없다.
- 실측 경로: 104 scratchpad `f70p/tmp.wxdQ2L48mB/`(setup.sh, setup2.sh, run.sh, run2.sh, load.js, out/). 컨테이너는 멈춰 두었다.

## 5. 원인 규정 (원칙 5, 6)

- `root_cause.target_id`: `food-delivery-dispatch`. 결함은 상한 없는 목록 로직이다. 정산 호출은 공개 계약(page/size 선택, 빼면 전부) 안의 정당한 요청이므로 원칙 6 의 "A 가 정당하고 B 의 로직이 잘못" 경우다. 원본도 고객 질의가 아니라 엔진의 처리 규모를 고쳤다.
- `trigger_target_id`: 비움. 계기는 바깥 호출자(정산 시스템)라 이 테스트베드 부품 이름이 없다.
- `scoring.partial`: `food-delivery-order`, `testbed-order`(증상 서비스), `food-mysql`(무거운 조회를 받은 DB).
- 원칙 5: 정답은 "dispatch 가 배달 목록 호출을 처리하다 힙이 바닥나 멈췄다"이고 관제 데이터(JVM 힙 지표, DPM 의 LIMIT 없는 조회, liveness 재시작, order 의 용량 확인 시간 초과 로그)로 바로 낼 수 있다. 코드 설계 추론(비멱등, 경쟁 조건)이 필요하지 않다. 상한이 없다는 사실은 피해 설명(왜 한 호출로 무너지는가)이다.

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 |
|---|---|
| 감지 | order ERROR 로그 급증('Failed to check dispatch capacity'), order 서버 스팬 오류율(주문 503), dispatch 서버 스팬 오류·지연, KCM Unhealthy·Killing. 별도 업무 규칙 없이 이벤트와 인시던트가 생기는 겉 증상 |
| 피해 판정(러너) | 기준선 주문 생성 5xx 비율 ≥ 0.5(평시 0 근처), 배제는 부하 미전달, MySQL NotReady, order NotReady |
| RCA | 표 7 의 순서: dispatch 힙 지표 한도와 CPU 한도 → DPM 의 LIMIT 없는 배차 조회 → KCM liveness 재시작(롤아웃 없음) → order 용량 확인 시간 초과 |

## 7. 관측 근거 표 (119 실조회, 2026-10-12 11:50~12:35 UTC, 평시)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 핵심 증거는 JVM 지표(10초 내보내기, 표본 아님), DPM Top SQL, KCM 이벤트, 전수 로그다.

| 증거 | 119 위치 | 실제 조회와 결과 |
|---|---|---|
| 근본: dispatch 힙 | VM `apm.agent.otel.java.jvm.memory.used`, `jvm.memory.used_after_last_gc`, `jvm.memory.limit` (service_name food-delivery-dispatch) | `{__name__="apm.agent.otel.java.jvm.memory.limit",service_name="food-delivery-dispatch"}` → Eden 68.3, Survivor 8.5, Tenured Gen 170.7MiB. `max_over_time(...jvm.memory.used{jvm_memory_type="heap"}[6h])` → Tenured 84.7, Eden 34.1MiB. used_after_last_gc 6시간 최대 Tenured 49.1MiB. 장애 시 Tenured 가 한도까지 |
| 근본: GC 로 CPU 한도 | VM `apm.agent.otel.java.jvm.cpu.recent_utilization` | 지표 수집 확인(이름 목록). 장애 시 0.5 CPU 한도에 붙음(로컬 dispatch CPU 50%) |
| 계기: LIMIT 없는 배차 조회 | CH `dpm_topsql_local`(engine mysql, body JSON 의 sqlText, rowExamined, rowSent) | 1일: 페이지 질의 sql_id adf27719… `SELECT d1_0.id, ... FROM dispatches d1_0 WHERE d1_0.status = ? LIMIT ?` 825회, rowExamined 최대 21. LIMIT 없는 같은 꼴 문장 0건. 장애 시 새 sql_id(LIMIT 없음)가 약 174만 행 검사 |
| 계기: page 없는 목록 요청 | CH `otel_traces_local` span_attributes['url.query'] (표본 10%) | `SELECT span_attributes['url.query'], count(), quantile(0.5)(duration_ns)/1e6, max(duration_ns)/1e6 FROM lucida.otel_traces_local WHERE timestamp > now() - INTERVAL 7 DAY AND service_name='food-delivery-dispatch' AND span_name='GET /api/deliveries' AND span_kind='SERVER' GROUP BY 1` → `status=DELIVERED&page=0&size=20` 5,104건(p50 2.6ms, 최대 81ms), `status=DELIVERED&page=2&size=5` 1건, page 없음 0건. 보조 증거(표본, 정산 호출은 약 2분 반에 한 건) |
| 전파: 재시작 | CH `kcm_events_local` (namespace rca-testbed-food, object_name testbed-dispatch%) | 7일: Unhealthy 'Readiness probe failed: ... context deadline exceeded' 29건, 'Liveness probe failed: ... context deadline exceeded' 27건(다른 시나리오), Started/Killing/Created 각 14건, Startup probe 'connection refused' 기동마다 8건, ScalingReplicaSet 6건(롤아웃). 이 시나리오는 ScalingReplicaSet 없이 Unhealthy·Killing·Started 만 되풀이돼야 한다 |
| 피해: order 용량 확인 실패 | CH `lucida_logs_local` (전수) | 7일 food-delivery-order ERROR 'Failed to check dispatch capacity: I/O error on GET request for "http://testbed-dispatch:8082/api/deliveries/capacity": Read timed out' 292건, '... Connection refused' 2건(다른 시나리오) — 같은 로그 형식이 수집된다 |
| 보조: OOM 로그 | CH `lucida_logs_local` severity, log_attributes['exception.type'] | 30일: OOM 로그는 core-banking-account 의 SEVERE 'Servlet.service() ... OutOfMemoryError' 3건, 'Failed to complete processing of a request'(exception.type java.lang.OutOfMemoryError) 3건뿐, food 0건. 로컬에서 OOM 줄은 조회 약 3분 뒤에야 찍혀 liveness 재시작이 먼저 올 수 있다. 남으면 보조, 없으면 JVM 지표와 KCM 이 근거 |
| 배제: MySQL, order | 러너 kubernetes.pod_ready(testbed-mysql, testbed-order), KCM | MySQL 은 처음 약 10초만 CPU 한도, 잠금 없음. order 는 재시작 없음 |

## 8. 감별

- must_support: 정답지 `must_support` 4개(dispatch Tenured Gen 이 호출 몇 초 안에 한도·CPU 한도 + DPM LIMIT 없는 조회, KCM Unhealthy·Killing·Started 반복과 롤아웃 없음, order 'Read timed out' 뒤 'Connection refused' 와 기준선 주문 5xx, 표본 url.query 'status=DELIVERED' page 없음과 남으면 OOM 로그).
- must_rule_out: 릴리스 누수(F63-P, F43-P, F33-H: 롤아웃, 몇 분에 걸친 증가), 설정 롤아웃(F32-R, F32-H), MySQL·배차 표(F33-R, F60-R, F60-H, F38-R), 백필 Job·부하 폭주(F59-R, F07-H), order·restaurant 자체 장애.
- contrast_with: F70-R, F63-P, F43-P, F59-R, F33-H, F32-R.
- F63-P 와 가르는 관측: ① KCM ScalingReplicaSet·새 이미지 Pulled 유무(F63-P 는 1.9.0 롤아웃) ② 차오르는 메모리(F63-P 는 스레드 스택이라 힙은 평탄하고 jvm.thread.count 가 오름, F70-P 는 Tenured Gen 이 한도) ③ 속도(F63-P 는 10분 넘게, F70-P 는 호출 몇 초 뒤) ④ DPM 의 LIMIT 없는 배차 조회 유무.
- F59-R 과 가르는 관측: Job 파드와 KCM Job 이벤트 유무, 깊은 offset 페이지 질의 수천 건 대 LIMIT 없는 한 건, dispatch 풀 고갈(Hikari) 대 힙 고갈.

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

- 주입: `load.north_south` primary, `rider-settlement-export.js`, target_rps 5(배달 추적 여정, 주문 없음), entry 30181(food 도메인 프로파일, 스크립트는 DISPATCH_URL 기본값 30182), 경사 2m, 유지 13m, 내림 1m(부하 960초 = 레벨 timeout 16m, primary 부하 계약), seed 7016.
- 정산 VU 1: 호출(시간 제한 90초) → 실패 → 60초 쉼 → 다음 호출, 약 150초 주기. 한 주기: 호출 → 약 10~15초 뒤 멈춤 → liveness 15초 x 5 로 약 75초 뒤 재시작 → 기동 41~45초 → Ready(호출 뒤 약 130~135초) → 다음 호출(약 150초)에 다시 멈춤. 주기 약 150초 가운데 약 120초 동안 dispatch 가 답하지 않는다.
- 성공: 기준선 주문 생성 5xx ≥ 0.5, 2틱. 기준선 주문은 초당 0.2~1.15건이라 30초 창에 6~34건이고, 멈춤 구간 약 120초가 2틱(최대 50초)보다 길어 어느 위상이든 성립한다. 주문은 용량 확인에서 끝나 배차 한도를 쓰지 않는다(동반 부하에 주문을 넣지 않는 이유, delivery-status-surge.js 주석).
- min_hold 10m: 경사 2분 뒤 첫 호출, 10분이면 네 번째 안팎의 실패 구간 안에서 판정.
- 배제: achieved_rps < 1.25, MySQL NotReady, order NotReady. 중단: entry_status 0(order, restaurant 는 살아 503 으로 답한다).
- 회복: MySQL·order Ready, 기준선 주문 2xx ≥ 0.7(2틱), 상한 12m. cleanup 뒤에도 이미 들어간 조회가 dispatch 를 멈춰 있을 수 있어 liveness 재시작(약 3분)을 기다린다.
- 테스트베드 한계: 정산 호출은 약 150초에 1건이라 입구 요청률이 평시 + 5rps 뿐이다. MySQL 은 호출마다 약 10초 CPU 한도를 쓰고 기다린다. 필수 중단 조건(입구 0, 노드, MySQL)에 걸리지 않는다. dispatch 는 비입구라 재시작해도 entry_status 는 0 이 되지 않는다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Cronofy 2025-04-15, 공식 상태 페이지 사후 보고, 링크)와 요소별 대응표가 있고 기전(정상 호출 → 의도보다 몇 자릿수 큰 결과 처리 → 메모리 고갈 → 처리 중 요청 실패)이 같다 (원칙 1)
- [x] 분류 장부를 갱신했다. 묶음 A 8 → 9(8.3%), 정답 위치 배달 서비스 6 → 7(6.5%), 결제 경로 13/108(12.0%), 서비스 음식배달 35 → 36(가장 적은 서비스). 어느 축도 20% 아래 (원칙 2)
- [x] 근본 원인 위치: `DispatchController.java:30-41`(:38 unpaged), `DispatchService.java:86-92`, `DispatchRepository.java:16-17`, `22-dispatch-deploy.yaml:49-50`(-Xmx 없음), `:87-93` (G1)
- [x] 근본 원인의 흔적: VM dispatch JVM 힙 지표(평시 값 조회), DPM MySQL Top SQL(평시 페이지 질의 조회, LIMIT 없는 문장은 장애 시 새 sql_id) (원칙 3)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: JVM 지표(표본 아님), DPM, KCM 이벤트, 로그(전수). url.query 트레이스는 보조 (원칙 3)
- [x] 계기의 흔적: DPM Top SQL 의 LIMIT 없는 배차 조회, 표본 트레이스 url.query. 인공 지연 없음 (원칙 4)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다(목록 호출 처리 중 힙 고갈로 dispatch 멈춤) (원칙 5)
- [x] 정답지 세 칸: target_id food-delivery-dispatch, trigger_target_id 비움(바깥 호출자), partial order·mysql (원칙 6)
- [x] 감지, 피해 판정, RCA 증거가 6절에 따로 있다 (원칙 7)
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: 요청 경로와 질의 문자열에 id 없음, k6 태그는 러너 안의 지표에만 붙고, User-Agent 는 상주 기준선과 같은 k6 (원칙 8)
- [x] 부하 상한(동반 5rps, 정산 약 150초에 1건)과 서킷(order → dispatch, 최소 5회 50% 5초)을 넣어 피해 계산, 로컬 실측으로 한 건만으로도 멈춤을 확인 (원칙 9)

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- dispatch Tenured Gen 이 호출 뒤 한도에 닿는지, OTel 지표가 멈추기 직전까지 내보내지는지(멈춘 JVM 은 지표 내보내기가 끊길 수 있다. 끊기면 그 끊김과 KCM 이 남는다).
- DPM Top SQL 에 LIMIT 없는 dispatches 조회 sql_id 가 잡히는지(MySQL 이 'Sending to client' 로 기다리다 연결이 끊긴 문장도 다이제스트에 남는지).
- OOM 로그가 liveness 재시작 전에 119 에 들어오는지.
- 재기동 Ready 와 다음 호출 사이 위상(약 15~20초 정상 구간)이 운영에서도 그런지, 주기마다 기준선 주문 5xx 0.5 이상 구간이 서는지.
