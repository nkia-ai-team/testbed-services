---
title: F60-R 설계 시트 (배차 이력 표에 외래 키를 더하는 마이그레이션의 표 복사 잠금이 배차 쓰기를 막아 dispatch 풀 고갈, 주문 503)
status: Draft
owner: project
last_reviewed: 2026-10-10
tags:
  - scenario
  - design
  - mysql
  - migration
  - lock
summary: 정합성 보강 마이그레이션이 food 배차 상태 이력 표 dispatch_events 에 빠져 있던 dispatches 로의 외래 키를 ALGORITHM, LOCK 없이 더해, foreign_key_checks 가 켜진 MySQL 이 표를 통째로 복사하는 몇 분 동안 자식과 부모 표의 모든 쓰기가 메타데이터 잠금을 기다리고, 막힌 배차 쓰기가 dispatch 연결 풀을 채워 모든 새 주문이 503 이 되는 시나리오. 원본은 Pulumi 2023-10-06(외래 키를 바꾸는 마이그레이션의 표 복사가 오래 잠금을 쥐어 DB 연결이 말라 24분 동안 요청 74.7% 실패).
---

# F60-R 설계 시트

## 1. 요약

food 의 dispatch-service 는 배달원을 배정하는 트랜잭션에서 `dispatches` 에 배차 한 행, `dispatch_events` 에 상태 이력 한 행을 쓴다. 이력 표는 `dispatch_id` 로 배차를 가리키지만 외래 키가 없다(보존 정리 코드 주석이 그렇게 적는다). 정합성 보강 마이그레이션이 그 외래 키를 더한다:

```sql
ALTER TABLE dispatch_events ADD CONSTRAINT fk_dispatch_events_dispatch FOREIGN KEY (dispatch_id) REFERENCES dispatches (id)
```

외래 키는 제자리(in place)로 더할 수 있다는 가정 아래 ALGORITHM 도 LOCK 도 적지 않았다. 그런데 MySQL 은 `foreign_key_checks` 가 켜져 있으면(서버 기본값) 외래 키를 제자리에 더하지 못하고 표를 통째로 복사한다(세션 상태 `copy to tmp table`). 복사하는 동안 자식 표와 **부모 dispatches** 의 모든 쓰기가 `Waiting for table metadata lock` 으로 기다리고 읽기는 통과한다. 배달원 배정 INSERT 가 멈춰 dispatch 의 연결을 쥐고, order 의 배차 호출이 시간 초과되며, 막힌 쓰기가 dispatch Hikari 10개를 모두 묶어 주문 저장 전 용량 확인까지 연결을 못 얻는다. 모든 새 주문이 503 이 되고, dispatch 는 health 확인 실패로 Service 에서 빠지고 재시작된다. 복사가 끝나면(약 1분 반~5분) 쓰기가 풀리고 스스로 회복한다.

비유: 도서관 대출 장부(배차 표) 옆에 반납 이력철(이력 표)이 있다. 사서장이 "이력철 한 장 한 장이 실제 대출 기록을 가리키는지 확인하는 규칙을 붙이자"며 이력철을 새 바인더로 옮겨 적기 시작했다. 옮겨 적는 동안 장부와 이력철 둘 다 새로 쓸 수 없게 봉해 두었다(읽기는 된다). 창구의 사서 열 명이 모두 "장부에 쓰려고 대기" 중이 되자, 책을 빌리러 온 손님은 대출 가능 여부조차 물어볼 사서가 없어 돌아간다. 장부도 이력철도 멀쩡하고, 옮겨 적기가 끝나면 다시 굴러간다.

## 2. 원본 사례

- 기업: Pulumi
- 날짜: 2023-10-06 약 17:15 UTC(사후 보고 게시 2023-10-18, Casey Huang)
- 링크: [공식 블로그 사후 보고 "Details of the Pulumi Outage on October 6, 2023"](https://www.pulumi.com/blog/post-mortem-2023-10-06/)
- 요약(원문 인용 중심, 2026-10-10 WebFetch 로 확인): "shipped a database migration modifying foreign keys on a table to our production environment". "The resulting table copy operation caused by the bad migration held a lock for a significant amount of time" 이고 이것이 "caused a query pileup that starved our database of all available connections". 24분 장애 동안 "we failed to process 74.7% of received requests". 배포 3분 안에 경보, 5분 안에 사고 회의. 대응자가 막힌 쓰기를 끊으려 준비하는 사이 마이그레이션이 성공적으로 끝나("While we were preparing to execute mitigation actions, the migration completed successfully") DB 와 API 가 곧바로 완전히 회복. 대상 표는 가장 바쁜 표 트래픽의 0.2% 미만이라 리뷰가 위험이 낮다고 봤음. 원인 설명: "Adding foreign key constraints to a table can be done “in place” with an asterisk", 시험과 리뷰가 그 단서를 놓침. 재발 방지: "migrations must always explicitly specify safe ALGORITHM and LOCK constraints"(SQL 린트로 강제), 운영 규모 사전 시험, 덜 중요한 표의 잠금이 핵심 작업의 연결을 뺏지 않게 장애 영역 분리.
- `docs/ref-real-world-incidents.md` M21 에 이 사례를 추가했다(출처가 말한 사실만).
- 원본이 적지 않은 것: DB 엔진 이름, 표 이름, 정확한 문장. 다만 "ALGORITHM 과 LOCK 을 명시" 라는 재발 방지 문구와 "외래 키는 제자리로 된다, 단서가 붙는다" 는 설명은 MySQL 의 ALTER TABLE 문법과 그 규칙(`foreign_key_checks` 가 꺼져 있을 때만 INPLACE)을 가리킨다. 재구성은 그 규칙을 우리 MySQL 8.0.46 에서 로컬 실측으로 확인했다(§4).

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 표의 외래 키를 바꾸는 마이그레이션 배포 | 배차 이력 표 dispatch_events 에 dispatches 로의 외래 키를 더하는 마이그레이션(ALGORITHM, LOCK 없음) |
| 원인이 된 결함 | 외래 키 추가가 "제자리" 로 된다는 가정의 단서를 놓쳐, 마이그레이션이 표 복사를 일으키고 그 복사가 오래 잠금을 쥠 | `foreign_key_checks=1` 이라 MySQL 이 INPLACE 대신 COPY 를 골라 이력 표 348만 행을 복사, 그동안 자식, 부모 표 쓰기에 메타데이터 잠금 |
| 전파 경로 | 그 표에 대한 쓰기가 막혀 질의가 쌓이고 연결 풀이 말라 DB 와 API 전체가 연결을 못 얻음 | 배차 INSERT 가 막혀 dispatch Hikari 10개가 기다리는 쓰기에 묶임 → 같은 풀을 쓰는 용량 확인과 health 확인도 연결을 못 얻음 → order 배차, 용량 확인 실패와 서킷 열림 → 모든 새 주문 503, dispatch NotReady 와 재시작 |
| 사용자 증상 | 24분 동안 받은 요청의 74.7% 실패 | 복사하는 동안(약 1분 반~5분) 모든 새 주문 503 'Dispatch service unavailable', 배달 추적 실패. 둘러보기, 주문 조회는 정상 |
| 탐지된 경로 | 배포 3분 안에 경보 | lucida 이상 탐지(order 오류 로그와 503, dispatch 풀 고갈 로그, KCM Unhealthy, DPM 세션 수 급증), 인시던트 |
| 완화와 복구 | 막힌 쓰기를 끊으려 준비하는 사이 마이그레이션이 스스로 끝나 곧바로 회복 | 복사가 스스로 끝나 쓰기가 풀리고 회복. cleanup 은 복사가 끝나기를 기다린 뒤(남으면 멈춤) 외래 키를 지워 원래 스키마로 되돌림 |

기전 확인: 원인(외래 키를 더하는 마이그레이션이 일으킨 표 복사와 그 잠금)에서 증상(그 표에 쓰는 서비스의 연결 풀 고갈, 넓은 요청 실패)까지 고리가 원본과 같다. 원본처럼 대상은 "한가한 보조 표"(이력 표)이고, 피해는 그 표를 쓰는 서비스의 연결 풀 전체로 번진다. 회복 꼴도 같다: 원본은 막힌 쓰기를 끊으려 준비하는 사이 마이그레이션이 스스로 끝나 회복했고, 재구성도 복사가 끝나면 스스로 회복한다. 차이 하나: 재구성에서는 dispatch 의 liveness 재시작이 클라이언트 쪽 연결을 끊지만, 죽은 파드의 서버 쪽 대기 세션은 복사가 끝날 때까지 남는다.

## 3. 숫자 근거 (2026-10-10, `scenario-stats.py`, 정식 + 후보 합계 80, 이 후보 전)

| 축 | 값 | 판단 |
|---|---|---|
| 서비스 | commerce 27, core-banking 27, food-delivery 26 | 음식배달이 가장 적어 우선. 쇼핑몰, 은행은 정답 위치 0 부품만 낼 수 있음 |
| 묶음 | A 7, B 7, C 6, D 7, E 4, F 3, G 8, H 2, I 1, J 13(16%), K 2, L 7, M 1, O 1, P 10, Q 1, N 0 | 주 묶음 D 7→8(81 중 9.9%). J 가 16% 로 가장 많아 새 버전 배포가 아닌 후보를 앞에 둠 |
| 정답 위치 | 주문 서비스 8(10%)이 최다. DB 테이블(배차 이력) 0 | 새 정답 위치 0→1(1.2%). 배차로 묶어 세도 2→3(3.7%) |
| 결제 경로 합계 | 12(15%) | 이 후보는 결제 경로가 아니라 12/81(14.8%) |
| 부품 지도 | food: mysql 8, dispatch 7, order 4, restaurant 4, payment 3, notify 1, kafka 0 | 증상 부품(dispatch)은 많지만 정답은 DB 의 다른 표에 가한 스키마 변경 |

왜 이 후보인가: 서비스 최소(음식배달), 묶음 D 는 정식만 있고 후보가 0이며(9.9%), 정답 위치가 처음 나오는 표이고, 현실 근거가 공식 사후 보고다. 겉 증상(주문 503 'Dispatch service unavailable', dispatch 풀 고갈)이 F32-R, F32-H, F32-P, F33-R, F33-H, F43-P, F55-R, F59-R 과 같다. 다만 정답지가 잠긴 부모 dispatches 도 받으므로(§5) 채점 위치는 F33-R, F32-P 의 'food-mysql:fooddelivery.dispatches' 와 겹친다: 세 시나리오 모두 'dispatch 풀 고갈이면 dispatches 표' 로 만점이 난다. 다른 것은 기전(DDL 세션의 표 복사 잠금 대 인덱스 제거, 표 소멸)과 그것을 가르는 관측 근거(§8)이고, dispatch 릴리스나 설정 배포(F32-R, F32-H, F33-H, F43-P, F55-R), 백필 Job(F59-R)을 외워 찍는 지름길은 막는다. 장부 정답 위치는 마이그레이션이 바꾼 표로 '배차 이력' 이지만 배차로 묶어 세면 2→3(3.7%)이다.

### 3단계 후보 목록 (실제 기전 × 부품, 숫자 순으로 줄 세움)

| # | 후보 | 원본 | 부품, 정답 위치(현재 수) | 판단 |
|---|---|---|---|---|
| 1 | 외래 키 추가 마이그레이션의 표 복사 잠금이 배차 쓰기를 막음 | Pulumi 2023-10-06 공식 블로그 | food MySQL dispatch_events, DB 테이블(배차 이력) 0 | **채택(F60-R)**. 음식배달 최소, D 후보 0, 정답 위치 0 |
| 2 | 외래 키(와 그 인덱스)를 지운 마이그레이션 뒤 보존 정리 DELETE 가 order_items 전수 스캔, 반복 읽기의 다음 키 잠금이 주문 품목 INSERT 를 막음 | Chargebee 2018-03-02 공식 상태 페이지 | food MySQL order_items, DB 테이블(주문 항목) 0 | 버림(원칙 9): 109 읽기 전용 실측으로 order_items 전수 스캔 1.06초, 보존 정리는 6초 간격이라 잠금 창이 주기의 15~33%, 주문은 1~3초 늦어질 뿐 5xx 가 나지 않는다 |
| 3 | 알림 서비스 릴리스가 이벤트마다 dispatch 를 반복 호출(렌더마다 다시 부르는 결함 꼴) | Cloudflare 2025-09-12 공식 블로그(+ Close 2024-09 상태 페이지) | food notify, 알림 서비스 0 | 버림(원칙 9): 119 로그로 알림 이벤트가 초당 약 0.9건(주문 약 0.23건/초)이라 호출 배수를 기전과 무관하게 크게 잡아야 dispatch 가 포화된다. 끝없이 늘어나는 폴러를 넣으면 원본과 결함 꼴이 바뀜 |
| 4 | 외래 키 추가 표 복사를 order 가 쓰는 표(order_items 를 가리키는 키 바꾸기, dispatches.order_id → orders, payments.order_id → orders)에 | Pulumi 2023-10-06 | food orders 계열 | 버림(컨트롤러 필수 중단 조건): 로컬 실측에서 부모 표 INSERT 도 메타데이터 잠금을 기다려, orders 가 부모면 order 의 주문 INSERT 가 막혀 order 풀과 readiness 가 빠지고 entry_status 0 |
| 5 | banking transfer 릴리스가 헬스 하위 그룹을 꺼 liveness 404 재시작 반복 | Spreedly 2023-01-25 공식 상태 페이지(F41-H 시트 보류 2번) | banking transfer, 은행 이체 서비스 6 | 버림(원칙 2): 은행이 쇼핑몰과 함께 27 로 가장 많고 정답 위치가 0 이 아님 |
| 6 | food dispatch 릴리스가 order 의 배차 요청을 엄격 검증으로 400 → order 503 / 운영에 없는 개발 전용 설정을 읽는 dispatch 릴리스 | Flagsmith 2024-01-18, ServiceChannel 2025-05-08 (F41-H, F36-H 시트 보류) | food dispatch, 배달 서비스 5 | 보류(버리지 않음): J 13(16%), 같은 증상의 정답이 이미 배달 서비스 다섯이라 1번보다 뒤 |
| 7 | 외부 PG 가 응답 필드 형식을 바꿔 payment 가 해석 실패 | Flagsmith 2026-03-03(F30-R, F32-H 원본) | food 외부 결제 mock, 외부 결제 의존 6 | 버림(원칙 4): 공급자 쪽 변경은 mock 에 관측이 없어 계기 흔적이 119 에 남지 않고, 결제 경로 13/81 로 상한에 다가감 |
| 8 | commerce gateway 텔레메트리 라이브러리 성능 회귀 + 몰림으로 gateway OOM | Canva 2024-11-12 공식 블로그 | commerce gateway, 게이트웨이 0 | 버림(컨트롤러 필수 중단 조건): commerce 부하가 gateway 로 바로 들어가 gateway 가 죽으면 entry_status 0(rejected 의 gateway 누수 행과 같은 벽) |
| 9 | 마이그레이션이 order_items 에 유일 제약이나 CHECK 제약, 트리거를 더해 정상 쓰기가 거절 | (공식 사후 보고 없음, 웹 검색 2회: 기술 문서와 포럼뿐) | food order_items | 버림(원칙 1) |
| 10 | food MySQL 전역 설정(max_allowed_packet, offline_mode) 변경 | (공식 사후 보고 없음) | food MySQL, DB 인스턴스 2 | 버림(원칙 1) |

서로 다른 부품: dispatch_events, order_items, notify, orders 계열 표, banking transfer, dispatch, 외부 결제 mock, commerce gateway, MySQL 인스턴스(9개).
rejected, 장부 §4-1, §5, 지휘 세션의 막힌 목록과 원본, 주입이 겹치는 것은 없다(rejected 의 Honeycomb 2024-08-06 메타데이터 잠금 대기열 후보는 원본과 주입이 다르다: 그쪽은 열린 트랜잭션 뒤에서 기다리는 즉시 ADD COLUMN, 이쪽은 DDL 자체가 표를 복사하며 잠금을 쥔다. Modern Treasury 외래 키 후보는 원문 미확인과 dispatches 대상이라 order 풀 위험으로 막혔다).

## 4. 인과 사슬 (코드와 인프라 위치)

1. 계기: 마이그레이션 `ALTER TABLE dispatch_events ADD CONSTRAINT fk_dispatch_events_dispatch FOREIGN KEY (dispatch_id) REFERENCES dispatches (id)` (ALGORITHM, LOCK 없음). 109 읽기 전용 실측(2026-10-10): `@@global.foreign_key_checks`=1, `lock_wait_timeout` 31536000, fooddelivery 외래 키 5개 중 dispatch_events 0개, 고아 이력 0행(전체 조인 8초), dispatch_events 약 348만 행(데이터 178MB, 인덱스 197MB, `idx_dispatch_events_dispatch` 하나), 데이터 볼륨 여유 19GB.
2. 서버 동작(로컬 MySQL 8.0.46, `--cpus 0.5`, 버퍼 풀 128MB, 109 와 같은 판, 같은 표 정의와 크기로 실측): 마이그레이션 세션이 `copy to tmp table` 상태로 한가할 때 76~97초, 다른 세션 넷이 두 표를 계속 읽는 동안 300초. 그동안 `INSERT INTO dispatches`, `INSERT INTO dispatch_events` 가 처음부터 끝까지 `Waiting for table metadata lock`(각 약 92초와 끝까지), `SELECT COUNT(*) FROM dispatches WHERE status='ASSIGNED'` 는 0.27초. 같은 표 전수 읽기 속도는 109 와 로컬이 비슷하다(1.27~1.47초 대 1.40~1.67초).
3. `food-delivery/dispatch-service/src/main/java/com/fooddelivery/dispatch/service/DispatchService.java:50-77` (dispatchCourier): 용량 COUNT(읽기, 통과) 뒤 `:71` `dispatchRepository.save(d)` 의 dispatches INSERT(IDENTITY 라 즉시 실행)에서 메타데이터 잠금을 기다리며 Hikari 연결을 쥔다. `:72` recordEvent 의 이력 INSERT 도 같은 잠금.
4. `DispatchService.java:119-129` (deliverExpiredDispatches, 30초마다): dispatches UPDATE 와 이력 INSERT 도 기다린다. `DispatchRetentionService.java:37-66` (purgeOlderThan, 6초마다): 이력 DELETE 가 기다린다. 둘 다 연결 하나씩을 쥔다.
5. `food-delivery/order-service/src/main/java/com/fooddelivery/order/client/DispatchClient.java:55-75` (dispatchCourier): 읽기 시간 제한 5초(`order-service/src/main/resources/application.yml:55-58`) 뒤 ERROR 'Failed to dispatch courier for order {}: I/O error on POST request ... Read timed out', 재시도 3회(`application.yml:105-111`, 200ms 지수 백오프) 끝에 fallback 503 'Dispatch service unavailable'. 시간 초과된 호출마다 dispatch 쪽에는 막힌 쓰기와 연결이 남는다.
6. `food-delivery/dispatch-service/src/main/resources/application.yml:13-15` (Hikari 10, connection-timeout 3000): 막힌 쓰기 약 10개면 풀이 빈다. 그 뒤 order 가 주문 저장 전에 부르는 용량 확인(`OrderService.java:94-112`, `DispatchService.java:106` getCapacity)은 연결을 3초 기다리다 'HikariPool-1 - Connection is not available' 로 500 → order 'Failed to check dispatch capacity' → 서킷 열림 → 곧바로 503.
7. `food-delivery/k8s/22-dispatch-deploy.yaml:73-86`: readiness(/actuator/health, 10초 × 3)와 liveness(15초 × 5)가 health 의 DataSource 확인에서 연결을 못 얻어 실패 → 약 30초 뒤 NotReady(연결 거부로 order 가 곧바로 503), 약 75초 뒤 kubelet 재시작. 죽은 파드의 서버 쪽 대기 세션은 복사가 끝날 때까지 남는다(rejected Honeycomb 행의 로컬 실측과 같음). 새 파드는 기동(약 40초) 뒤 첫 배정에서 다시 막힌다.
8. 복사가 끝나면 기다리던 쓰기가 지나가고(살아 있는 연결의 배정은 커밋, 끊긴 연결의 것은 되돌려짐) dispatch 와 order 가 스스로 회복한다(order 서킷 5초).
9. 그대로인 것: 가게, 메뉴, 주문 조회(읽기와 다른 표), orders, order_items, payments 쓰기(복사 대상이 아님). 결제는 주문이 배차 단계에서 끝나 불리지 않는다.

## 5. 원인 규정 (원칙 5, 6)

- 근본(`root_cause.target_id`): `food-mysql:fooddelivery.dispatch_events`. 잘못된 것은 이 표에 가한 스키마 변경(방식 지정 없는 외래 키 추가)이다. dispatch 와 order 는 늘 하던 정당한 요청을 했고 실패한 곳(풀 고갈)은 증상이다(원칙 6 의 "정당한 요청이 결함 있는 곳에서 실패" 꼴은 아니고 "잘못된 명령 A 가 B 를 막아 C 증상" 꼴: 원인은 A).
- 계기(`trigger_target_id`): 비움. 근본과 계기가 같은 곳(이 표에 대한 마이그레이션)이다.
- 정답으로 받는 것: `food-mysql:fooddelivery.dispatch_events` 와 `food-mysql:fooddelivery.dispatches`. DPM 세션 기록에는 DDL 문장 대신 sqlHash 만 남아 복사 중인 표 이름이 직접 보이지 않고, 대기 세션은 두 표 모두에 쓰고 있다. 잠긴 부모 표를 지목한 답은 관제 데이터로 낼 수 있는 합리적 결론이라 오답 처리하지 않는다(원칙 5). 외래 키라는 세부, 누가 돌렸는지는 요구하지 않는다.
- 부분 점수(`scoring.partial`): food-delivery-dispatch, food-dispatch(증상 서비스), food-mysql(인스턴스만 지목).
- 장부 정답 위치는 마이그레이션이 바꾼 표로 "DB 테이블(배차 이력)" 로 센다.

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 |
|---|---|
| 감지(lucida) | order ERROR 로그 급증('Failed to dispatch courier ... Read timed out', 'Failed to check dispatch capacity'), order POST /api/orders 503, dispatch 'Connection is not available' 급증, KCM testbed-dispatch Unhealthy, Killing, DPM 세션 수 급증 |
| 피해 판정(러너) | 기준선 loadgen-food 의 주문 생성 5xx 비율 ≥ 0.5, 2xx 비율 < 0.2 (2틱, 판정 관측 갱신 30초) |
| RCA 증거 | DPM 세션 스냅숏의 'copy to tmp table'(root@localhost, fooddelivery) 과 dispatch 파드 IP 의 'Waiting for table metadata lock', 롤아웃 없음, 표 수와 인덱스 수 불변, 1146 없음 |

## 7. 관측 근거 (119 실조회, 2026-10-10 21:30~23:00 UTC)

| 증거 | 표, 칸 | 조회와 결과(평시) | 표본 여부 |
|---|---|---|---|
| 계기: DDL 세션 | CH `lucida.dpm_session_local`(body JSON `user`, `hostname`, `state`, `queryTime`, `dbName`) | `SELECT JSONExtractString(body,'state') st, count() FROM lucida.dpm_session_local WHERE engine='mysql' AND timestamp > now() - INTERVAL 60 DAY GROUP BY 1` → 'copy to tmp table' **0건**(상태 22종 중 없음). 스냅숏은 1분마다(`count(DISTINCT timestamp)` 1시간 59). root@localhost 세션도 기록된다(60일 1건, 2026-10-09 03:43 'Waiting for table metadata lock') | 전수(DPM 1분 폴링) |
| 근본: 메타데이터 잠금 대기 | 위 표 state | 'Waiting for table metadata lock' 60일 8건, 모두 2026-10-09 03:43 한 스냅숏. 장애 시 dispatch 파드 IP(10.244.2.x, user fooddelivery)의 세션이 분마다 쌓인다 | 전수 |
| 전파: 세션 수 | VM `dpm.mysql.session.active_session`, `current_session`{target_id="c8c558e5-…"} | `quantile_over_time(0.5, …[7d])` active 1, `max_over_time` 12, current 중앙 57 최대 75, `max_session` 151. 장애 시 active 가 대기 세션 수만큼 오른다 | 전수 |
| 보조: 표 다시 쓰기 | VM `dpm.mysql.instance.data_file_write_bytes` | 7일 중앙 0.41MB/s, p99 2.27MB/s, 최대 2.55MB/s. 복사는 약 375MB 를 1분 반~5분에 다시 쓴다 | 전수 |
| 감별: 표, 인덱스 수 불변 | VM `dpm.mysql.database.table_count`, `index_count`(fooddelivery) | 표 16, 인덱스 31(F32-P, F33-R 시트 7일 고정 확인). 외래 키 추가는 기존 인덱스를 다시 써 두 값이 그대로다 | 전수 |
| 전파: order 배차 시간 초과 | CH `lucida.lucida_logs_local` `service_name='food-delivery-order' AND body LIKE 'Failed to dispatch courier%Read timed out%'` | 30일 **0건** | 전수 |
| 전파: order 용량 확인 실패 | 위 표 `body LIKE 'Failed to check dispatch capacity%'` | 7일: '...: 500 : "<html>...Whitelabel' 310건, '...: Read timed out' 292건(다른 시나리오 실행 창). 장애 시 오른다 | 전수 |
| 전파: dispatch 풀 고갈 | 위 표 `service_name='food-delivery-dispatch' AND body LIKE '%Connection is not available%'` | 7일 798건, 5분 창 최대 260(2026-10-08 09:15, F30-R 실행), 그 밖은 다른 시나리오 실행 창 | 전수 |
| 전파: dispatch 재시작 | CH `lucida.kcm_events_local` `object_name LIKE 'testbed-dispatch%'` | 7일 Unhealthy 124, Killing 10, ScalingReplicaSet 20(모두 시나리오 실행 창). 장애 시 Unhealthy, Killing 이 ScalingReplicaSet 없이 나온다 | 전수 |
| 피해: 주문, 배차 로그 | 위 표 'Created order id=%'(order), 'Dispatched courier=%'(dispatch) | 평시 주문 약 0.23~1건/초(알림 로그 1시간 836건 기준). 장애 시 멈춘다 | 전수 |
| 보조: 주문 503, 배차 스팬 | CH `lucida.otel_traces_local` | 장애 시 order POST /api/orders 503, dispatch POST /api/deliveries/dispatch 긴 지속 시간 | **10% 표본, 보조로만** |

핵심 증거(DDL 세션 상태, 메타데이터 잠금 대기 세션, 세션 수, order 와 dispatch 로그, KCM 이벤트)는 모두 전수 수집이다. 트레이스와 APM 지표는 보조로만 쓴다. DDL 문장 자체는 DPM Top SQL 에 남지 않는다(일회성 문장은 앞선 폴링에 본 적이 없어 델타로 나오지 않음, F23-P 실행기 설명과 같음). 장애 시 형태는 첫 실행에서 다시 확인한다.

## 8. 감별

- must_support: 'copy to tmp table' 세션과 메타데이터 잠금 대기 세션, order 'Failed to dispatch courier ... Read timed out' 뒤 503, dispatch 풀 고갈과 KCM Unhealthy, Killing(롤아웃 없음), active_session 상승과 표, 인덱스 수 불변, 둘러보기와 주문 조회 정상, 스스로 회복.
- must_rule_out: F32-P(1146, 표 수 감소), F33-R/F59-R(풀을 쥔 것이 긴 읽기, Top SQL 검사 행 수 수백만), dispatch 릴리스나 설정 배포(롤아웃, 새 ReplicaSet), MySQL 다운이나 읽기 전용(다른 표 쓰기 성공), 부하나 배달원 한도(요청량 그대로, 'courier pool exhausted' 없음).
- contrast_with: F32-P, F33-R, F59-R, F48-R, F43-P(정답지 related_scenarios 에 차이를 적음).
- 같은 증상(주문 503 'Dispatch service unavailable')의 기존 정답: 배달 서비스 5, DB 테이블(배차) 2, 내부 배치 작업 1. 이 후보의 채점 위치는 DB 테이블(배차)과 겹친다(dispatches 도 정답으로 받음). 기전만 다르고, 그 기전을 가르는 근거는 DPM 세션 상태('copy to tmp table', 메타데이터 잠금 대기), 1146 없음, 표, 인덱스 수 불변, Top SQL 검사 행 수 평시다.

## 9. 러너 판정과 강도, 부하 계산

- 강도 축이 없다: 표 하나를 복사하는 길이는 표 크기와 인스턴스 바쁨으로 정해진다. 설계 강도 하나(이 표, 이 문장)로 고정한다.
- 복사 길이(원칙 9): 109 와 같은 판, 같은 크기의 로컬 실측은 한가할 때 76~97초, 읽기 세션 넷과 함께 300초. 109 testbed-mysql-0 은 평시 거의 한가하다(cgroup CPU 약 2%, active_session 7일 중앙 1, 평가자 109 읽기 전용 실측). 로컬에서 80만 행 복사가 약 14초였으니(평가자 실측) 348만 행은 약 60초까지 짧아질 수 있다. 그래서 복사는 약 60~100초로 보고 판정을 복사 앞부분에 둔다: min_hold 60s, settle 30s, success 2틱, timeout 6m.
- 시간표 계산(기준선 주문 r ≈ 0.2~1.1건/초): 0초 복사 시작 → 첫 배정이 막힘. 주문 하나가 배차 시간 초과 3번(5초 + 0.2초 + 5초 + 0.4초 + 5초 ≈ 16초)에 dispatch 연결 셋을 남기므로 보존 정리, 만료 배치와 함께 약 5~15초에 dispatch 풀 10개가 빈다. 그 뒤 용량 확인 3초 대기 실패, 5번 실패면 서킷이 열려 곧바로 503. dispatch NotReady(약 30~40초) 뒤에는 연결 거부로 곧바로 503. 판정 관측 둘(loadgen.food_create_status_rate, loadgen.transfer_2xx_rate)은 러너 observation_queries.json 의 update_interval 이 30초이고, adaptive.py 의 _independence_sec 가 all 조건에 가장 느린 간격 30초를 써 _updated_streaks 가 표본 사이가 30초 이상일 때만 연속 틱을 올린다. success 연속은 min_hold 전에 지워지지 않는다(_drop_pending_streaks 는 must_rule_out, escalate 만 지움). 그래서 30초 창이 처음 조건을 넘는 표본(약 30~45초) 다음 표본이 약 60~75초에 찍히고, min_hold 60s 직후 2틱으로 성공한다. 둘째 창(약 30~75초)은 통째로 복사(약 60~100초) 안에 든다. 3틱이면 셋째 창(약 60~105초)이 복사 끝과 회복(readiness 10초, 서킷 5초)에 걸려 깨질 수 있고, 깨지면 evaluation_level_timeout 으로 첫 녹화를 잃는다(평가 1차 지적). 같은 2틱 선례가 controller 15개 있다. 복사가 2틱보다 짧게 끝나는 경우(약 60초 미만)가 남는 위험이고 §11 에 적었다.
- order 풀(15) 압박: 처음 수십 초 동안 주문 트랜잭션이 배차 응답을 기다리며 연결을 쥔다. r × 16초 ≈ 3~18 이라 가장 바쁜 시간에 잠깐 15 에 닿을 수 있으나, 서킷이 열리는 약 10~20초 뒤에는 주문이 1초 안에 끝난다. order readiness 는 10초 × 3번 연속 실패가 필요해 빠지지 않는다고 본다(F33-R 녹화: dispatch 풀 고갈 중 order liveness 실패 8회, 재시작 없음, entry_status 503/500/200). 그래서 동반 부하를 두지 않는다(동반 주문은 이 압박만 키움). must_rule_out 의 order NotReady 가 이 가정을 지킨다. 그래도 createOrder 의 @Transactional(OrderService.java:57)이 용량 확인 재시도와 배차 호출 동안 order 연결을 쥐므로, 운영 하네스에 기준선 주문이 적은 시간대(KST 새벽, 주문 분당 약 12건) 실행을 권한다(§11).
- MySQL 세션 상한: 평시 현재 세션 중앙 57, 최대 75. dispatch 재시작마다 서버 쪽 대기 세션이 약 10~13개 남고 새 파드가 3~10개를 연다. 복사 5분 안에 재시작 두세 번이면 약 100 이하로 max_connections 151 아래. preflight 가 열린 세션 90 이하를 요구한다.
- cleanup: 복사가 끝나기를 최대 300초 기다리고(원본처럼 마이그레이션을 끝내게 둠, 녹화에 복사 전체가 담김. 300초는 로컬에서 가장 길었던 복사와 같은 상한), 넘으면 KILL QUERY(로컬 실측 되감기 0.4초). 설계 결정: cleanup 은 성공, abort, must_rule_out 어느 경로로 불려도 같은 동작이라, 안전 중단 뒤에도 복사가 남아 있으면 장애가 최대 300초 더 이어진다. 복사는 어차피 그 안에 스스로 끝나고 쓰기만 막을 뿐 데이터를 바꾸지 않으므로 이렇게 둔다. 외래 키 지우기는 메타데이터만(로컬 0.07초). 러너 cleanup 시간 제한 10m, 실행기 하위 프로세스 제한 900초 안.
- run 은 러너 coordinator lock 을 쥔 채 돈다(profile-control.py): 마이그레이션을 DB 파드 안에서 백그라운드(nohup, 표준 입출력 리디렉션)로 띄우고 세션이 보이면 곧 돌아온다(로컬 1.5초). db.lock 의 Oracle 잠금 세션(nohup sqlplus)과 같은 방식이다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Pulumi, 2023-10-06, 공식 블로그)와 요소별 대응표, 기전 동일(§2). 원문 인용은 2026-10-10 WebFetch 로 확인.
- [x] 분류 장부를 갱신했고 묶음 D(7→8, 9.9%), 정답 위치 DB 테이블(배차 이력)(0→1), 결제 경로 12/81(14.8%), 서비스(음식배달 26→27), 이 후보를 고른 이유를 적었다. 어느 축도 20% 미만(§3).
- [x] 근본 원인 위치: food MySQL fooddelivery.dispatch_events 와 그 스키마(`food-delivery/db/init.sql:134-140`), 막히는 쓰기 `DispatchService.java:71-72`, 109 실측 값(§4).
- [x] 근본 원인 흔적이 119 에 조회됐다: dpm_session_local 의 state(평시 'copy to tmp table' 0건, 메타데이터 잠금 대기 한 스냅숏), root@localhost 세션 기록 가능(§7).
- [x] 핵심 증거가 표본 데이터에만 있지 않다: DPM 세션, 로그, KCM 이 전수다(§7).
- [x] 계기 흔적: DDL 세션이 분마다 'copy to tmp table' 로 남는다. 인공 지연이 아니라 현실의 스키마 변경 문장 그 자체다(원칙 4).
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다: "DB 안의 DDL 세션이 표를 복사하며 배차 쓰기를 막음". 외래 키라는 세부나 코드 결함 추론을 요구하지 않고, 잠긴 부모 표를 지목한 답도 받는다(§5).
- [x] 정답지 세 칸이 원칙 6대로다(§5).
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다(§6).
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: 문장, 외래 키 이름, 로그 파일(/tmp/schema-migration.log, cleanup 이 지움)에 시나리오 id 가 없고 테스트로 고정했다. DB 세션은 다른 db.ddl MySQL 모드와 같은 root@localhost 다.
- [x] 부하 상한과 서킷브레이커를 고려한 피해 계산(§9). 강도는 한 단, 동반 부하 없음.

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- 109 에서 복사 길이(2틱 성공에 약 75초 이상 필요). 그보다 짧게 끝나면 판정이 서지 않아 실패로 끝난다: 그때는 운영 하네스가 동반 부하로 판정 표본을 늘리는 보강을 낸다.
- 운영 하네스 권고: 기준선 주문이 적은 시간대(KST 03~06시)에 돌린다. order 가 Ready 를 지키는지(must_rule_out)가 가장 먼저 볼 항목이다.
- dpm_session_local 에 'copy to tmp table' 과 'Waiting for table metadata lock' 이 실제로 담기는지.
- order 가 Ready 를 지키는지(must_rule_out), MySQL 현재 세션 최대값.
- cleanup 뒤 외래 키 0, /tmp 로그 없음, dispatch 회복.
