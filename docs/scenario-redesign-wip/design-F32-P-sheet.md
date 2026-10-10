---
title: F32-P 설계 시트 (운영을 가리킨 로컬 마이그레이션이 food 배차 표를 지워 주문이 배달원 용량 확인에서 전량 503)
status: Draft
owner: project
last_reviewed: 2026-10-10
tags:
  - scenario
  - design
  - schema
  - mysql
  - operation
summary: 배차 표를 바꾸는 기능을 만들던 개발자가 자기 PC 에서 돌린 마이그레이션의 연결이 로컬 DB 대신 운영 food MySQL 을 가리켜, 마이그레이션의 표 지우기 단계가 살아 있는 fooddelivery.dispatches 를 지운다. dispatch-service 의 용량 확인, 배달 추적, 배치가 MySQL 1146 으로 실패하고, order-service 는 주문을 저장하기 전에 부르는 배달원 용량 확인이 500 이 되어 재시도와 서킷 끝에 모든 주문을 503 으로 거절한다. 겉 증상은 F32-R(배차 한도 설정), F32-H(숫자 형식 설정), F33-R(같은 표 인덱스 제거)과 같고 원인이 다르다. 원본은 Resend 2024-02-21(F53-R 과 같은 원본).
---

# F32-P 설계 시트

## 1. 요약

food 의 dispatch-service 는 배달원 배차를 `fooddelivery.dispatches`(109 실측 약 174만 행, ASSIGNED 약 1,745, 데이터 126MB, 인덱스 95MB, 인덱스 3개)에 둔다. 이 표는 dispatch 만 읽고 쓰며, 외래 키로 이 표를 참조하는 표도 없다(init.sql 의 주석대로 order_id 에 외래 키를 두지 않음). 배차 표를 바꾸는 기능을 만들던 개발자가 자기 PC 에서 마이그레이션을 돌리는데, 명령이 쓰는 연결 설정이 로컬 DB 가 아니라 운영 food MySQL 을 가리키고 있었다. 마이그레이션의 표 지우기 단계가 운영의 살아 있는 `dispatches` 를 지운다.

그 순간부터 dispatches 를 부르는 dispatch 의 모든 문장이 MySQL 1146 으로 실패한다. order-service 의 `createOrder` 는 가게와 메뉴를 확인(200)한 뒤, 아무것도 쓰기 전에 dispatch 에 배달원 용량을 묻는다(GET /api/deliveries/capacity, dispatches 를 세는 질의). dispatch 가 500 을 주고, order 는 재시도하다 dispatch 서킷이 열려 POST /api/orders 를 약 1초 안에 503 'Dispatch service unavailable' 로 거절한다. 주문 행도 배차도 결제도 생기지 않는다. 배달 추적도 500 이고, dispatch 의 보존 정리 배치(6초)와 배차 만료 배치(30초)가 같은 오류를 낸다. 가게 둘러보기, 주문 조회, 결제는 그대로다.

비유: 배달 대행 사무실의 기사 배정 장부를 본사 직원이 연습용 사무실 것으로 착각하고 치웠다. 가게 점원은 주문을 적기 전에 늘 "지금 기사 여유가 있나요?" 를 묻는데, 사무실이 "장부가 없어 모르겠다" 고만 답하니 점원은 주문을 받지 못하고 손님을 돌려보낸다. 손님 눈에는 "기사가 없다" 고 거절당한 것과 똑같지만, 기사는 그대로 있고 장부만 사라졌다.

F53-R(같은 원본, 같은 수단으로 주문 표를 지움)과 무엇이 다른가: 표를 쓰는 서비스가 order 가 아니라 하류 dispatch 이고, 오류 문장(1146)이 order 가 아니라 dispatch 로그에 나며, order 는 SQL 오류 없이 하류 실패를 503 으로 바꾼다. 관제 AI 에게는 "주문 503 이 배달원 용량 확인에서 나면 dispatch 설정(F32-R, F32-H)이나 dispatch 릴리스(F33-H, F43-P)" 라는 지름길을 막는 짝이 된다(§8). 같은 표가 정답인 F33-R(인덱스 제거)과는 같은 정답에 다른 기전이라, "배차 표가 원인" 까지 맞혀도 무엇이 바뀌었는지는 데이터로 갈라야 한다.

## 2. 원본 사례

- 기업: Resend (메일 발송 API)
- 날짜: 2024-02-21 (UTC 04:56 마이그레이션 시작, 17:05 해결)
- 링크: [Incident report for February 21, 2024 (공식 블로그)](https://resend.com/blog/incident-report-for-february-21-2024) (자료 문서 `ref-real-world-incidents.md` M22 에 이미 있음, F53-R 이 추가)
- 요약(출처가 말한 것만): 기능을 만들던 엔지니어가 로컬 환경에서 데이터베이스 마이그레이션을 돌렸는데 그 명령이 운영 환경을 가리켜 운영의 모든 표를 지웠다("incorrectly pointed to the production environment instead, which dropped all tables in production"). 04:57 운영에서 표가 지워지는 것을 알아챘고 05:01 백업 복원을 시작했다. 첫 복원은 잘못된 백업 시각 선택으로 실패했고, 더 오래된 백업에서 다시 복원해 17:02 API 요청 수락을 재개했다. 약 12시간 모든 사용자가 메일 발송, API, 대시보드를 쓰지 못했고("no API requests were being accepted and no data was being stored"), 마이그레이션 직전 5분의 기록을 잃었다.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 기능을 만들던 엔지니어가 로컬 환경에서 데이터베이스 마이그레이션 실행 | 배차 표를 바꾸는 기능을 만들던 개발자가 자기 PC 에서 마이그레이션 실행 |
| 원인이 된 결함 | 명령이 로컬이 아니라 운영 환경을 가리켜 운영의 모든 표를 지움 | 명령의 연결이 로컬 DB 가 아니라 운영 food MySQL 을 가리켜 마이그레이션의 표 지우기 단계가 운영 `fooddelivery.dispatches` 를 지움(그 기능이 바꾸는 표 하나로 규모를 줄임) |
| 전파 경로 | 표가 없어 API 가 요청을 받지도 저장하지도 못함 | dispatch 의 `Dispatch` 엔티티 질의 1146 → 용량 확인 500 → order 의 용량 확인 실패, 재시도, 서킷 열림 → POST /api/orders 503. 배달 추적, 보존 정리, 만료 배치도 같은 오류 |
| 사용자 증상 | 메일 발송, API, 대시보드 약 12시간 전면 불가 | 주문 생성 100% 503, 배달 추적 500. 가게 둘러보기, 메뉴, 주문 조회, 결제는 정상 |
| 탐지된 경로 | 1분 뒤 운영에서 표가 지워지는 것을 알아챔 | order 오류율과 오류 로그('Failed to check dispatch capacity: 500'), dispatch 의 새 오류 템플릿(1146 WARN, 'doesn't exist' ERROR, Servlet ERROR, 보존 정리 WARN, 스케줄 ERROR) 급증 → 119 이상 탐지 → 인시던트. DPM 표 수 16→15 |
| 완화와 복구 | 백업에서 복원(두 번째 시도에 성공), 5분 기록 손실 | cleanup 이 표를 원래 자리로 되돌림(행 그대로, order 서킷이 다음 호출을 통과시키는 대로 약 5초 안 회복). 원본의 백업 복원 몇 시간은 재현하지 않는다 |

기전은 원본과 같다: "개발 중 로컬에서 돌린 마이그레이션 명령이 운영을 가리켜 운영의 표를 지움 → 앱이 그 표를 찾지 못해 그 표를 쓰는 요청이 모두 실패 → 표를 되살리면 회복". 우리 스택에 맞춘 것은 규모(모든 표 → 그 기능의 표 하나)다. 재구성의 안전 장치는 F53-R 과 같다: 진짜 DROP 은 174만 행을 잃으므로, 실행기는 표를 앱 스키마 밖의 시스템 스키마 `mysql` 로 옮긴다(`mysql.fooddelivery_dispatches`). DPM 수집기는 `mysql` 스키마를 보고하지 않아 앱과 DB 목록 지표에는 DROP 그 자체로 보인다. 행과 인덱스는 표를 따라가고 cleanup 의 같은 RENAME 으로 그대로 돌아온다(로컬 MySQL 8.0 실측, §4).

## 3. 숫자 근거 (2026-10-10, `scenario-stats.py`, 정식 + 후보 합계 67, 이 후보 전)

| 축 | 값 | 판단 |
|---|---|---|
| 서비스 | 쇼핑몰 26, 은행 21, 음식배달 20 | 음식배달이 가장 적다 → 음식배달(20→21) |
| 묶음 | J 8(12%), A 7, B 7, D 7, G 7, P 7(각 10%), C 6, L 6(9%), F 3, E 2, H 2, K 2, I 1, M 1, O 1, N 0 | P(운영 작업의 대상 착오) 7→8(11.8%). 결함이 앱이 기대하는 표가 없는 꼴로 드러나 F53-R, F53-P, F35-H 처럼 P+L. L 로 세도 6→7(10.3%) |
| 정답 위치 | DB 테이블(배차) 1(F33-R) | §2-1 'DB 테이블(<무엇>)' 꼴, 1→2(2.9%). 배달 서비스(4)가 아니다 |
| 결제 경로 합계 | 11(16.4%) | 결제가 정답이 아니므로 그대로(11/68, 16.2%) |
| 어느 축이든 | 최대는 J 8/68(11.8%), P 8/68(11.8%), 주문 서비스 7/68(10.3%) | 20%(13.6) 미만 |

묶음 해석: 바뀐 것은 표 하나의 존재다. 설정값이 아니라 G 가 아니고, 코드나 이미지 배포가 없어 J 가 아니며, 계획된 스키마 변경이 코드보다 먼저 적용된 F36-R(L)과도 다르다: 이 마이그레이션은 운영에 적용할 변경이 아니었고, 의도한 변경 내용은 맞았는데 적용 대상(환경)이 틀렸다. 그래서 P 다. 인덱스, 잠금, 자원, 질의 비용은 그대로라 F, D, A 가 아니다(F33-R 은 같은 표의 인덱스 제거라 F).

관계와 id: 같은 원본과 수단의 F53 사례군은 R(orders), P(payments)가 이미 쓰였다. 이 후보의 겉 증상(주문 생성이 배달원 용량 확인에서 전량 503)은 F32 사례군(F32-R 배차 한도 설정, F32-H 숫자 형식 설정)과 같고 최초 원인과 고장 난 층(서비스 설정 → DB 표)이 달라 F32 의 P 로 둔다(F35-H 가 F35-R 의 증상 짝으로 Resend 원본을 쓴 것과 같은 방식). 같은 수단(db.ddl 표 지우기 모드)이지만 계약의 표가 달라(dispatches) 같은 주입 중복(같은 수단과 같은 파라미터)이 아니다. 카탈로그 §1 의 "서비스 이름만 바꾼 복제" 도 아니다: F53-R, F53-P 는 표를 쓰는 서비스가 곧 오류를 내는 서비스였지만 여기서는 오류 문장이 dispatch 에, 증상이 order 에 나뉘고, 실패 지점이 주문 저장 전 하류 확인이며, 영향 범위(배달 추적, 두 배치)도 다르다.

겉 증상(food 주문 503, 'Dispatch service unavailable')은 F32-R(정식), F32-H, F33-R(정식), F33-H, F43-P 와 같고 정답이 다르다(카탈로그 관계 H). 관제 AI 가 "주문 503 이 dispatch 에서 오면 dispatch 서비스" 를 외워 찍지 못하게 하는 짝이다. 가르는 관측 근거는 §8 에 둔다.

### 3단계 후보 목록 (실제 기전 × 부품, 숫자 순으로 줄 세움, 버린 이유 포함)

출발점은 기존 목록이 아니라 `ref-real-world-incidents.md` 의 기전(M2 결함 배포, M8 형식 불일치, M12 누수, M22 운영 명령 실수 등)과 부품 지도다. 0 인 부품(food, banking Kafka, notify, nginx, gateway)은 rejected 와 이번 실행의 막힌 목록에 원칙 7(사용자 경로 밖 비동기), 원칙 3(nginx OTel 없음), 원칙 1(게이트웨이 사례 없음)로 막힌 것이 쌓여 있어 이번에는 새 기전 × 그 부품 조합을 찾지 못했다.

| 순위 | 후보 | 원본 사례 | 부품(정답 위치 수) | 결과 |
|---|---|---|---|---|
| 1 | 운영을 가리킨 로컬 마이그레이션이 food dispatches 표를 지움 | Resend 2024-02-21 공식 블로그 | food MySQL dispatches 표, DB 테이블(배차) 1, 음식배달 20 | **채택(F32-P)** |
| 2 | 운영을 가리킨 로컬 마이그레이션이 banking Oracle ACCOUNTS 를 지움 | Resend 2024-02-21 | banking Oracle ACCOUNTS 표, DB 테이블(은행 계좌) 3, 은행 21 | 뒤로 미룸(설계 안 함): 1순위가 관문을 통과해 멈춤. 정답 위치가 3(1순위 1보다 많음)이고 은행이 1 많다. 다음 은행 차례 후보로 남긴다(init.sql 에 REFERENCES 없음 확인, ACCOUNTS 크기와 Oracle drop 모드 계약은 미확인) |
| 3 | banking account 릴리스가 이체 응답(TransferResponse) 구조를 바꿔 api 가 해석 실패 | GitHub 2021-10-08 월간 가용성 보고(F49-R 원본) | banking account(은행 계좌 서비스 3), L+J | 뒤로 미룸: api 의 AccountClient 가 해석 실패(502)를 @Retry 3회로 다시 POST 하고 이체가 멱등이 아니라(TransferService 가 요청마다 새 transferRef) 실패 한 건마다 이체가 세 번 커밋된다. 피해가 비멱등 재시도 설계에서 커져 원칙 5 위험이 있어 따로 따져야 한다 |
| 4 | banking account 릴리스의 DB 연결 누수 | Octopus Deploy 2025-11-25 공식(F43-R, F43-P 원본) | banking account(3), J+E | 뒤로 미룸(설계 안 함): 같은 원본의 세 번째 서비스 판. 1순위보다 정답 위치 수가 많다 |
| 5 | banking account 릴리스가 검증마다 무거운 질의를 더해 Oracle 포화 | GitHub 2025-01-09 월간 가용성 보고(F42-R, F42-P, F33-H 원본) | banking account(3), J+F | 뒤로 미룸(설계 안 함): 같은 원본의 네 번째 판이고 공유 Oracle 포화 증상이 F42-R 과 같은 DB 에서 겹친다 |
| 6 | food dispatch 릴리스의 메모리 누수 | Honeycomb 2019-11-06 공식(F41-R 원본) | food dispatch(배달 서비스 4), J+A | 뒤로 미룸: order 가 @Transactional 안에서 dispatch 용량 확인을 기다려(OrderService.java:57, :96, read-timeout 5초) GC 로 느려진 dispatch 가 order 풀을 묶으면 readiness 이탈(entry 0) 위험. food payment 판이 같은 벽으로 rejected |
| 7 | food payment 릴리스가 결제 응답 구조를 바꿔 order 가 해석 실패 | GitHub 2021-10-08 | food payment(결제 서비스 3, 결제 경로 11→12, 17.6%) | 뒤로 미룸: order 의 PaymentClient @Retry(2회)가 해석 실패 뒤 결제를 다시 POST 해 외부 PG 가 두 번 청구된다(3번과 같은 원칙 5 위험). 결제 경로가 상한에 가까워진다 |
| 8 | food restaurant 릴리스의 메모리 누수 | Honeycomb 2019-11-06 | food restaurant(가게 서비스 2), J+A | 버림(컨트롤러 필수 중단 조건): restaurant 가 진입 NodePort(30181)라 OOM 재시작 동안 entry_status 0 |
| 9 | banking ACCOUNTS 를 취소된 백필이 보류 이름으로 치움 | GitHub 2026-07-24 월간 가용성 보고(F48-R 원본) | banking Oracle ACCOUNTS(3) | 버림(원칙 1): 원본 고리는 Vitess 워크플로 취소가 표를 `_vt_hld_` 보류 상태로 옮기는 동작인데 Oracle 에는 그 도구와 이름 규칙이 없어 대응이 약하다(같은 표 지우기는 2번이 Resend 로 더 맞다) |

rejected 기록, 장부 §4-1, §5, 이번 실행에서 막힌 목록에 같은 원본과 같은 주입(Resend + dispatches 표 지우기)이 없다. 막힌 목록의 Resend 행은 orders(F53-R), payments(F53-P), TRANSFERS(F35-H)이고, rejected 의 menus 행(원칙 7: loadgen 이 메뉴 200 뒤에만 주문해 주문 표본이 사라짐)은 이 후보에 해당하지 않는다: 메뉴와 가게 조회는 200 이라 loadgen 이 주문을 계속 보내고 주문 503 이 표본으로 남는다.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 엔티티: `food-delivery/dispatch-service/src/main/java/com/fooddelivery/dispatch/entity/Dispatch.java:6-8` `@Entity @Table(name = "dispatches")`.
2. 용량 확인: `DispatchController.java:63-66` GET /api/deliveries/capacity → `DispatchService.java:106-115` `getCapacity` 가 `countByStatus("ASSIGNED")`. 표가 없으면 Hibernate `SqlExceptionHelper` WARN 'SQL Error: 1146, SQLState: 42S02' 와 ERROR "Table 'fooddelivery.dispatches' doesn't exist", Tomcat ERROR 'Servlet.service() … InvalidDataAccessResourceUsageException …' 와 500(`GlobalExceptionHandler.java:10-20` 는 ServiceException 만 다룬다).
3. order 쪽: `OrderService.java:94-112` 도메인 검증 3(배달원 용량 확인)이 첫 쓰기 `orderRepository.save(order)`(:128)보다 앞이다. `DispatchClient.java:32-50` `checkCapacity` 가 500 을 ERROR 'Failed to check dispatch capacity: 500 : …' 과 ServiceException(502)로 바꾸고, @Retry(최대 3회, 200ms 지수 백오프)와 @CircuitBreaker(창 10, 최소 5, 50%) 끝에 fallback 이 503 'Dispatch service unavailable: …' 을 던진다. 'Created order' 로그(:141), 배차(:145), 결제(:154)는 실행되지 않는다.
4. 배치: `DispatchRetentionBatch.java:41-57` 6초마다 dispatches 를 읽어 실패를 WARN 'Dispatch retention purge failed, will retry next cycle: …' 로 남긴다. `DispatchService.java:119-129` `deliverExpiredDispatches` 가 30초마다 `findExpiredAssigned` 로 실패해 스케줄러가 ERROR 'Unexpected error occurred in scheduled task' 를 남긴다. 둘 다 요청량과 무관한 시각 고정 신호다.
5. 그 밖에 dispatches 를 쓰는 곳: 배달 추적 GET /api/deliveries, /{id}, 배차 POST /dispatch(실패가 그 앞이라 이번에는 불리지 않음). 배차 이력 `dispatch_events` 와 outbox `dispatch_outbox_events` 는 다른 표라 그대로다.
6. 재시작해도 표를 만들지 않는다: `dispatch-service/src/main/resources/application.yml:20` `ddl-auto: none`.
7. health: dispatch, order 의 readiness, liveness 는 `/actuator/health`(`22-dispatch-deploy.yaml:67-86`)이고 DB 확인은 질의 없는 `isValid()` 라 표가 없어도 UP 이다(로컬 실측).
8. 스키마: `food-delivery/db/init.sql:110-131` dispatches(PRIMARY, idx_dispatches_order, idx_dispatches_status_assigned). 109 실측: 이 표를 참조하는 외래 키 0개.
9. 인프라: rca-testbed-food StatefulSet testbed-mysql(파드 testbed-mysql-0, MySQL 8.0.46, tb-w3). 109 실측(2026-10-10 09:2x UTC, information_schema 읽기 전용): fooddelivery 표 16개, 인덱스 31개(dispatches 3), dispatches 1,744,644 행(ASSIGNED 1,745), 시스템 스키마 mysql 에 fooddelivery_ 로 시작하는 표 0개, @@lock_wait_timeout 기본 31536000.
10. 로컬 실측(2026-10-10, MySQL 8.0 컨테이너 + init.sql 시드 2만 행, origin/main 과 같은 소스로 만든 order, restaurant, dispatch, payment 1.0.0 jar, PG 는 파이썬 흉내 서버, 실행기 원격 스크립트는 `db_ddl_executor.MYSQL_DROP_REMOTE` 를 그대로 꺼내 kubectl 을 docker exec 로 바꾼 껍데기로 실행):
    - 평시 주문 2/2 200, 배달 추적 200, 용량 `{"currentAssigned":2,"maxCapacity":2000,"available":1998}`.
    - preflight 0 → run 0 → 주문 8/8 503(0.64~0.78초). 첫 건 본문 'Dispatch service unavailable: Dispatch capacity check failed: 500 …', 이후 'CircuitBreaker 'dispatch' is OPEN …'. 배달 추적 500, 용량 500. order, dispatch health 내내 UP. dispatch 로그: 요청마다 'SQL Error: 1146, SQLState: 42S02', "Table 'fooddelivery.dispatches' doesn't exist", 'Servlet.service() … InvalidDataAccessResourceUsageException', 6초 주기 'Dispatch retention purge failed … [select d1_0.id,d1_0.status,d1_0.assigned_at from dispatches d1_0 …]', 30초 주기 'Unexpected error occurred in scheduled task'. order 로그: 'Failed to check dispatch capacity: 500 : …' 세 번(재시도), 주문 행 증가 0.
    - 주입 중 preflight 는 1('ERROR 1146 … doesn't exist' 가 stderr 첫 줄) → cleanup 0 → cleanup 0(두 번째는 할 일 없음) → recovery 0. 표 수 15→16, 인덱스 3개와 행 그대로. cleanup 뒤 1초 안에 주문 200(서킷 반열림 통과), 이어서 5/5 200.

## 5. 원인 규정 (원칙 5, 6)

| 칸 | 값 | 근거 |
|---|---|---|
| `root_cause.target_id` | `food-mysql:fooddelivery.dispatches` | 결함은 표 쪽이다(운영을 가리킨 마이그레이션이 살아 있는 표를 지움). 원본 보고의 원인과 복구도 표 지우기와 그 복원이다. 표기는 F33-R, F53-R 과 같은 '인스턴스:스키마.테이블' 꼴 |
| `trigger_target_id` | null | 근본과 계기가 같은 곳(이 표에 대한 잘못된 대상의 마이그레이션) |
| `scoring.partial` | `food-delivery-dispatch`, `food-dispatch`, `food-mysql` | 오류 문장이 찍히는 서비스와 DB 인스턴스는 원인 근처다. 증상만 보이는 order 는 부분 점수에도 넣지 않는다(F33-R 과 같은 선택: order 는 하류 실패를 전할 뿐 오류 문장이 order 에 없다) |

원칙 5: 정답은 dispatch 로그의 오류 문장("없는 표 dispatches"), 그 오류가 용량 확인과 두 배치에서 동시에 시작됐다는 사실, 같은 DB 의 다른 표 읽기, 쓰기 성공, 같은 분 DPM 표 수와 인덱스 수 감소, 그 시각에 배포나 재시작이 없다는 사실로 낼 수 있는 결론이다. 코드 설계 결함 추론이 필요 없다. "dispatch 가 SQL 오류를 낸다" 고 답하면 dispatch(부분), "dispatches 표가 사라졌다(DDL, 운영 작업)" 고 답하면 정답이다. 누가 어느 PC 에서 왜 마이그레이션을 돌렸는지는 관제 데이터에 없으므로 요구하지 않는다.

원칙 6: order 와 dispatch 의 요청과 배치는 늘 하던 정당한 것이고 결함 있는 곳(사라진 표)에서 실패했다. 표를 지운 잘못된 대상의 마이그레이션이 원인이다.

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 | 기대 |
|---|---|---|
| 감지 | order 서버 스팬 오류(POST /api/orders 503), order ERROR 'Failed to check dispatch capacity: 500' 급증, dispatch 새 오류 템플릿(1146 WARN, 'doesn't exist' ERROR, Servlet ERROR, 보존 정리 WARN, 스케줄 ERROR)과 로그 급증 | 주문 생성 100% 503. 보존 정리 WARN 이 부하와 무관하게 6초마다, 스케줄 ERROR 가 30초마다 |
| 피해 판정 | 러너: 동반 부하 주문 생성 5xx 비율 ≥ 0.5 와 2xx 비율 < 0.2(3틱) | 평시 주문 생성 5xx 는 0 근처 |
| 원인 설명 | 로그 전수(dispatch 1146 과 표 이름, order 전파 문장, 'Created order', 'Dispatched courier' 멈춤), DPM 표 수, 인덱스 수, 행 수, KCM 무변화, MySQL 세션 평시 | §7 |

인시던트 근거와 위험:

- 같은 order 503 꼴의 F32-R, F33-R 이 정식(녹화 있음)이다. 이 후보는 order 오류 로그('Failed to check dispatch capacity: 500')가 평시에도 다른 시나리오 실행 창에 있던 템플릿(119 에 313건, 마지막 2026-10-09)이라 새 템플릿은 dispatch 쪽에서 나온다. dispatch 의 1146 계열 넷과 'Dispatch retention purge failed' 는 평시 0건(retention WARN 은 F38-R 창 151건 뿐)이라 로그 급증 이벤트가 날 것으로 본다.
- 동반 부하(주문 생성 초당 약 1.4건)가 가장 한가한 시간에도 order POST 표본 스팬을 분당 약 8건(10%) 만든다. dispatch 서버 스팬은 서킷이 열리면 줄어든다(반열림 때 3건씩). 결정 증거는 스팬이 아니라 로그다.

## 7. 관측 근거 표 (119 실조회, 2026-10-10 09:15~09:30 UTC)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 결정 증거는 전수 로그와 DPM, KCM 이다.

| 증거 | 119 표와 칸 | 조회 | 평시 결과 |
|---|---|---|---|
| 근본: 없는 표 오류 | CH `lucida_logs_local` (`service_name`, `body`) | `SELECT service_name, countIf(position(body,'SQL Error: 1146')>0), countIf(position(body,'fooddelivery.dispatches')>0), countIf(position(body,'Unexpected error occurred in scheduled task')>0) … FROM lucida.lucida_logs_local WHERE service_name LIKE 'food-delivery-%' GROUP BY 1` | food 다섯 서비스 모두 1146 0건, 'fooddelivery.dispatches' 0건. 스케줄 ERROR 는 dispatch 104건, order 14건, payment 6건(다른 원인: 2026-10-09 시나리오 창의 연결 실패, read_only) |
| dispatch 로그 수집 | 같은 표 | `severity_text, substring(body,1,90), count() … service_name='food-delivery-dispatch' AND timestamp > now() - INTERVAL 1 DAY` | 'Dispatch expiry batch started/finished' 가 30초마다, 'Dispatched courier' 하루 약 5.8만 건(2026-10-04~09). 가장 이른 로그 2026-10-03 08:00 UTC |
| dispatch 보존 정리 실패 | 같은 표 | `countIf(position(body,'Dispatch retention purge failed')>0)` 일별 | 2026-10-08 1건, 2026-10-09 154건(F38-R 등 시나리오 창), 그 밖 0 |
| dispatch Tomcat 오류 수집 | 같은 표 | `position(body,'Servlet.service')>0 OR position(body,'SQL Error')>0` (dispatch) | 'SQL Error: 0'(778), 'Servlet.service() … CannotCreateTransactionException'(686), 'SQL Error: 1290'(664) 등 시나리오 창 기록. 같은 로거 경로로 1146 문장이 수집된다 |
| 전파: order 문장 | 같은 표 | order WARN/ERROR 중 'dispatch' 포함 템플릿 | 'Failed to check dispatch capacity: 500 : …' 313건, '… Read timed out' 292건(마지막 2026-10-09 03:44, F33-R 계열 실행 창), 'Dispatch service unavailable' 355건 |
| 기준선 주문량 | 같은 표 | 'Created order id=' 시각별(최근 24시간) | 시간당 3,466~4,145건(04~08시 UTC) |
| 계기: 표 지우기 | VM `dpm.mysql.database.table_count{db_name="fooddelivery"}`, `index_count`, `row_count` (target c8c558e5-…, 60초 간격) | 현재값, `min_over_time`, `max_over_time` [7d] | table_count 7일 16 고정, index_count 7일 30~31(F33-R 실행), 현재 31, row_count 현재 11,487,400. dispatches 는 인덱스 3개, 약 174만 행이라 지우면 15, 28, 약 174만 감소 |
| 계기: 시스템 스키마는 보고 안 함 | VM `dpm.mysql.database.*` 의 `db_name` | F53-R 시트 §7 과 같은 조회 | db_name 은 fooddelivery, information_schema, performance_schema, sys 넷. mysql 로 옮긴 표는 어느 지표에도 새로 나타나지 않는다 |
| 다른 변경 없음 | CH `kcm_events_local` | `namespace='rca-testbed-food' AND position(object_name,'testbed-dispatch')=1` 사유별 | Unhealthy 1,258(마지막 2026-10-09 03:44), ScalingReplicaSet 24, Pulled 49, Killing 48(마지막 2026-10-09 00:27). 롤아웃은 이 표에 남는다 |
| 피해: 스팬 | CH `otel_traces_local` (dispatch) | 최근 1일 span_kind, span_name | CLIENT 'SELECT fooddelivery.dispatches' 16,602, INTERNAL 'DispatchRepository.countByStatus' 11,812 등. 표가 없는 동안 이 CLIENT 스팬이 오류가 된다(보조 증거) |

## 8. 감별

- must_support: dispatch 1146 과 "Table 'fooddelivery.dispatches' doesn't exist" 오류 로그(평시 0), 6초마다 보존 정리 WARN, 30초마다 스케줄 ERROR, order 'Failed to check dispatch capacity: 500' 과 503, 'Created order' 와 'Dispatched courier' 멈춤, 같은 분 DPM 표 수 16→15, 인덱스 수 31→28, 행 수 약 174만 감소, 둘러보기, 주문 조회, 결제 200, 롤아웃과 재시작 없음, MySQL 세션과 응답 시간 평시.
- must_rule_out(정답지에 문장으로): dispatch 새 버전이나 설정 배포, 배차 인덱스 제거(F33-R), 주문 표 지우기(F53-R), MySQL 인스턴스 읽기 전용이나 다운, dispatch 파드 다운이나 풀 고갈, 부하 증가나 배달원 한도 소진.
- contrast_with: F33-R, F32-R, F32-H, F53-R, F43-P.
- F33-R 과 가르는 관측 근거(같은 표가 정답, 같은 order 503):
  - 오류: F33-R 은 'Read timed out', 'Connection is not available'(풀 고갈)이고 1146 이 없다. F32-P 는 1146 과 표 이름이 즉시 나온다.
  - DB: F33-R 은 MySQL 활성 세션과 응답 시간이 치솟고(전수 스캔), F32-P 는 평시다.
  - DPM: F33-R 은 index_count 31→30, table_count 16 그대로, F32-P 는 16→15, 31→28 이고 row_count 가 약 174만 줄어든다.
- F32-R, F32-H, F33-H, F43-P 와 가르는 관측 근거: 그쪽은 testbed-dispatch 롤아웃(KCM ScalingReplicaSet, 새 파드, 이미지나 env 변경)이 계기다. F32-R 은 dispatch 가 용량 확인에 200(available 0)으로 답하고 order 가 'Order rejected: courier pool exhausted' 뒤 503 'No courier available (capacity full)' 로 거절('Failed to check dispatch capacity' 없음), F32-H 는 200 본문을 order 가 못 읽음, F43-P 는 dispatch 풀 고갈과 readiness 이탈이다. F32-P 는 쿠버네티스 객체가 하나도 바뀌지 않고 dispatch 가 Ready 인 채 SQL 오류 500 을 즉시 준다.
- F53-R 과 가르는 관측 근거: F53-R 은 용량 확인이 200 이고 order 자신이 첫 INSERT 에서 1146(orders)을 내며 index_count 가 27 이 된다. F32-P 는 order 에 SQL 오류가 없고 dispatch 의 1146(dispatches)이 order 503 으로 번진다.
- 판별력: 서비스 입도 채점에서는 F32-R, F32-H, F33-H, F43-P 와 같은 'dispatch' 근처 답이 부분 점수다. 정답 입도(database-relation)에서는 표 이름이 정답이고, 인스턴스(food-mysql)와 dispatch 는 부분 점수다.

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

진행 대본: 설계 강도 하나로 고정한 평가 모드(`approved-fixed-f32-p`). 강도라는 축이 없다: 표가 없으면 그 표를 부르는 문장은 요청량과 무관하게 100% 실패한다.

| 항목 | 값 | 근거 |
|---|---|---|
| primary | db.ddl(표 지우기 모드), 계약 `{engine mysql, rca-testbed-food, testbed-mysql-0, fooddelivery, dispatches, hold_schema mysql, minimum_rows 1000}` | 실행기 CONTRACTS 와 profiles.json 같은 값(G2). 기존 MYSQL_DROP_REMOTE 를 그대로 쓴다(테스트로 F53-R 과 같은 스크립트임을 고정) |
| companion | load.north_south, food order-surge.js 2rps, entry 30181, seed 3216 | F53-R, F38-R 과 같은 값. 실패가 주문 저장과 배차 전이라 배달원 한도를 채우지 않는다 |
| entry_status | 동반 부하 k6 의 order, restaurant NodePort | 고장 중 503(0 아님). 0 은 노드나 order 가 죽었을 때만 |
| min_hold / settle / timeout | 15m / 30s / 20m, max_injection 25m | F53-R 과 같음 |
| success(3틱) | 동반 부하 주문 생성 5xx 비율 ≥ 0.5, 2xx 비율 < 0.2 | loadgen 집계(표본 아님), 30초 창에 동반만 약 42건 |
| must_rule_out(2틱) | achieved_rps < 0.5, MySQL NotReady, order NotReady | 부하 끊김, DB 다운, order 다운이면 다른 시나리오다. dispatch 파드 Ready 는 러너 관측 허용 목록(APPROVED_K8S_TARGETS)에 testbed-dispatch 가 없어 러너 판정에 넣지 않고 RCA 증거(must_support 5번, KCM)로만 둔다(러너 변경 없음) |
| abort | entry_status == 0 (필수) | |
| recovery(2틱, 10m) | target_health 200, MySQL Ready, order Ready, 기준선 주문 2xx ≥ 0.7 | 되돌리면 order 서킷이 다음 호출을 통과시키는 대로 회복(로컬 실측 1초 안) |

부하 계산:

- 동반 부하: order-surge.js 2rps 중 주문 생성이 초당 약 1.4건, 30초 창 약 42건. 기준선 주문 생성 분당 약 12~68건이 더해진다. 고장 중 모두 503 → 5xx 비율 1.0, 2xx 비율 0. 문턱(0.5, 0.2)을 어느 시간대에도 넘는다.
- 연결 보유: createOrder 는 @Transactional 이라 용량 확인을 기다리는 동안 order 연결 하나를 쥔다. 서킷이 열린 뒤에도 재시도 백오프(200ms, 400ms)로 주문 하나가 약 0.65~0.8초(로컬 실측)다. 동시 보유 = (기준선 최대 약 1.1 + 동반 1.4)건/초 × 0.8초 ≈ 2개로 order 풀 15 아래이고 readiness 는 isValid() 라 빠지지 않는다. dispatch 쪽은 실패가 즉시라(수 ms) 풀 10 을 묶지 않는다.
- 배달원 한도: 주문이 배차 요청까지 가지 않고, 표가 없는 동안 ASSIGNED 는 늘지 않는다. 되돌린 뒤 만료 배치가 그사이 ETA 가 지난 배차를 한 번에 DELIVERED 로 바꾼다(평시 한 번에 5~30건, 고장 15분이면 약 1,000건 안팎의 한 트랜잭션, 수 초).
- 강도와 피해: 주입은 메타데이터 문장 하나라 표 크기(174만 행)와 무관하게 즉시 끝난다. RENAME 은 dispatches 의 배타적 메타데이터 잠금이 필요해 진행 중인 dispatch 트랜잭션(배차 INSERT 수 ms, 보존 정리 2000건 삭제 묶음 1초 안팎, 만료 배치 수십 ms)을 기다리고, 그동안 새 dispatch 문장이 그 뒤에 줄 선다. lock_wait_timeout 10초가 상한이다. 그 사이 order 의 용량 확인이 기다려도 read-timeout 5초 안이고 동시 보유는 위 계산과 같은 수준이다. 10초 안에 잠금을 못 얻으면 아무것도 바뀌지 않은 채 run 이 실패하고 MySQL 오류 첫 줄이 러너 로그에 남는다.
- 서킷브레이커: order 의 dispatch 서킷이 열려 주문 대부분이 dispatch 를 부르지 않고 곧바로 503 이다(5초마다 반열림 3건만 dispatch 에 닿음). 그래서 dispatch 의 요청 오류 로그는 반열림 주기와 배달 추적(기준선 loadgen 의 DISPATCH_URL 30182)에서 오고, 두 배치의 시각 고정 오류가 그 바닥을 받친다. restaurant, payment 서킷은 열리지 않는다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Resend 2024-02-21, 공식 블로그 사고 보고)와 요소별 대응표가 있고 기전이 같다(규모만 모든 표 → 그 기능의 표 하나) (§2)
- [x] 분류 장부 §4-1, §6 갱신. 묶음 P 7→8(11.8%, L 로 세도 10.3%), 정답 위치 DB 테이블(배차) 1→2(2.9%), 결제 경로 11/68(16.2%), 음식배달 20→21. 어느 축도 20% 미만 (§3)
- [x] 근본 원인 위치: `Dispatch.java:6-8`, `DispatchService.java:106-115`, `:119-129`, `DispatchRetentionBatch.java:41-57`, 인프라 testbed-mysql-0 fooddelivery.dispatches (§4)
- [x] 근본 원인 흔적: CH `lucida_logs_local` 의 dispatch 1146 과 표 이름 오류 문장(전수, 평시 0건). 같은 로거의 문장 꼴은 로컬 실측과 119 의 기존 dispatch Hibernate, Tomcat 오류 수집으로 확인 (§4, §7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: 로그 전수, DPM 표 수, 인덱스 수, 행 수, KCM. 스팬은 보조 (§7)
- [x] 계기 흔적: DPM table_count 16→15, index_count 31→28, row_count 약 174만 감소(1분 간격, 표 수 7일 동안 16 고정), 오류 문장의 표 이름, 그 시각에 롤아웃과 재시작 없음. 인공 지연 없음 (§7)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다 (§5)
- [x] 정답지 세 칸이 원칙 6대로 (§5)
- [x] 감지, 피해 판정, RCA 증거 구분과 인시던트 위험 (§6)
- [x] 주입 도구가 시나리오 id 를 남기지 않는다: 옮긴 표 이름은 `fooddelivery_dispatches` 이고 인자와 스크립트에 id 없음(테스트로 고정). 동반 부하 k6 태그는 기존 시나리오들과 같은 경로
- [x] 부하 상한과 서킷브레이커를 고려한 피해 계산, 메타데이터 잠금 대기의 최악 경우 (§9)

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- dispatch 로그에 'SQL Error: 1146, SQLState: 42S02' 와 "Table 'fooddelivery.dispatches' doesn't exist", 'Servlet.service() … InvalidDataAccessResourceUsageException' 이 반열림 호출과 배달 추적마다, 'Dispatch retention purge failed' 가 6초마다, 'Unexpected error occurred in scheduled task' 가 30초마다 남는지.
- order 로그에 'Failed to check dispatch capacity: 500' 이 나오고 POST /api/orders 스팬이 503 인지, 'Created order id=' 와 dispatch 'Dispatched courier' 가 멈추는지, 둘러보기, 주문 조회, 결제(새 결제는 없음)가 200 인지.
- VM 의 `dpm.mysql.database.table_count{db_name="fooddelivery"}` 가 주입 분에 15, `index_count` 가 28 이 되고 `row_count` 가 약 174만 줄었다가 cleanup 뒤 16, 31 로 돌아오는지.
- dispatch 파드가 내내 Ready 인지(KCM, 러너 판정에는 없음).
- 인시던트: 묶음 멤버 템플릿과 판정 사유, promoted_at, 실행 시각대.
- cleanup 뒤 fooddelivery.dispatches 가 행 그대로이고 `mysql.fooddelivery_dispatches` 가 없는지(recovery 가 확인), 만료 배치가 밀린 배차를 정상 처리하는지.
- 녹화 창에 food MySQL OOM 재시작이 끼면 녹화로 쓰지 않는다. 재시작이 끼어도 표는 mysql 스키마에 남아 있고 cleanup 이 되돌린다.
