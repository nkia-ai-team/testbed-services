---
title: F53-R 설계 시트 (운영을 가리킨 로컬 마이그레이션이 food 주문 표를 지워 주문 생성이 전량 실패)
status: Draft
owner: project
last_reviewed: 2026-10-10
tags:
  - scenario
  - design
  - schema
  - mysql
  - operation
summary: 주문 표를 바꾸는 기능을 만들던 개발자가 자기 PC 에서 돌린 마이그레이션의 연결이 로컬 DB 대신 운영 food MySQL 을 가리켜, 마이그레이션의 표 지우기 단계가 살아 있는 fooddelivery.orders 를 지운다. order-service 의 주문 생성이 첫 쓰기(주문 INSERT)에서 MySQL 1146 Table doesn't exist 로 실패해 배차, 결제 호출 전에 전량 500 이 되고 주문 조회도 실패한다. 둘러보기, 배달 추적, dispatch, payment 는 정상이다. 원본은 Resend 2024-02-21 로컬에서 돌린 마이그레이션 명령이 운영을 가리켜 운영의 모든 표를 지운 장애.
---

# F53-R 설계 시트

## 1. 요약

food 의 order-service 는 주문을 `fooddelivery.orders`(약 173만 행, 데이터 114MB, 인덱스 250MB)에 저장하고, 품목 `order_items` 가 외래 키로 이 표를 참조한다. 주문 표를 바꾸는 기능을 만들던 개발자가 자기 PC 에서 마이그레이션을 돌리는데, 명령이 쓰는 연결 설정이 로컬 DB 가 아니라 운영 food MySQL 을 가리키고 있었다. 마이그레이션의 표 지우기 단계(마이그레이션 도구가 표를 지울 때처럼 외래 키 검사를 끈 채)가 운영의 살아 있는 `orders` 를 지운다. 그 순간부터 `createOrder` 는 가게, 메뉴, 배달원 용량을 확인한 뒤 첫 쓰기인 주문 INSERT 에서 MySQL 1146 으로 실패해 트랜잭션이 되돌려지고, 배차도 결제도 일어나지 않은 채 500 으로 답한다. 주문 조회와 6초마다 도는 주문 보존 정리 배치도 같은 오류를 낸다. 가게 둘러보기, 배달 추적, dispatch, payment 는 그대로다.

비유: 본사 직원이 새 장부 양식을 시험하려고 "옛 장부를 치우고 새로 만든다" 는 순서를 실행했는데, 연습용 사무실이 아니라 실제 가게 계산대의 주문 장부를 치웠다. 점원은 손님마다 메뉴와 배달 기사 여유까지 확인하고 주문을 적으려는 순간 "장부가 없다" 며 주문을 받지 못한다. 메뉴판과 배달 조회 창구는 멀쩡하다.

## 2. 원본 사례

- 기업: Resend (메일 발송 API)
- 날짜: 2024-02-21 (UTC 04:56 마이그레이션 시작, 17:05 해결)
- 링크: [Incident report for February 21, 2024 (공식 블로그)](https://resend.com/blog/incident-report-for-february-21-2024) (자료 문서 `ref-real-world-incidents.md` M22 에 이번에 추가)
- 요약(출처가 말한 것만): 기능을 만들던 엔지니어가 로컬 환경에서 데이터베이스 마이그레이션을 돌렸는데 그 명령이 운영 환경을 가리켜 운영의 모든 표를 지웠다("incorrectly pointed to the production environment instead, which dropped all tables in production"). 04:57 운영에서 표가 지워지는 것을 알아챘고 05:01 백업 복원을 시작했다. 첫 복원(약 6시간)은 잘못된 백업 시각 선택으로 실패했고, 더 오래된 백업에서 다시 복원해(약 5시간) 17:02 API 요청 수락을 재개했다. 05:01~17:05(약 12시간) 모든 사용자가 메일 발송, API, 대시보드를 쓰지 못했고("no API requests were being accepted and no data was being stored"), 마이그레이션 직전 5분의 기록을 잃었다. 재발 방지로 운영 쓰기 권한 제한, 로컬 개발 개선, DB 장애 중 발송을 이어 갈 중복, 재해 복구 시험 주기 강화를 들었다.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 기능을 만들던 엔지니어가 로컬 환경에서 데이터베이스 마이그레이션 실행 | 주문 표를 바꾸는 기능을 만들던 개발자가 자기 PC 에서 마이그레이션 실행 |
| 원인이 된 결함 | 명령이 로컬이 아니라 운영 환경을 가리켜 운영의 모든 표를 지움 | 명령의 연결이 로컬 DB 가 아니라 운영 food MySQL 을 가리켜 마이그레이션의 표 지우기 단계가 운영 `fooddelivery.orders` 를 지움(그 기능이 바꾸는 표 하나로 규모를 줄임) |
| 전파 경로 | 표가 없어 API 가 요청을 받지도 저장하지도 못함 | order 의 `Order` 엔티티가 여전히 `orders` 에 씀 → 주문 INSERT 1146 → 트랜잭션 되돌림 → POST /api/orders 500. 주문 조회, 보존 정리 배치도 같은 오류 |
| 사용자 증상 | 메일 발송, API, 대시보드 약 12시간 전면 불가 | 주문 생성과 주문 조회 100% 500. 가게 둘러보기, 메뉴, 배달 추적, 배차, 결제 서비스 자체는 정상 |
| 탐지된 경로 | 1분 뒤 운영에서 표가 지워지는 것을 알아챔 | order 오류율과 새 오류 로그(1146 WARN, 'doesn't exist' ERROR, Servlet ERROR, 보존 정리 WARN) 급증 → 119 이상 탐지 → 인시던트. DPM 표 수 16→15 |
| 완화와 복구 | 백업에서 복원(두 번째 시도에 성공), 5분 기록 손실 | cleanup 이 표를 원래 자리로 되돌림(행 그대로, 즉시 회복). 원본의 백업 복원 몇 시간은 재현하지 않는다 |

기전은 원본과 같다: "개발 중 로컬에서 돌린 마이그레이션 명령이 운영을 가리켜 운영의 표를 지움 → 앱이 그 표를 찾지 못해 그 표를 쓰는 요청이 모두 실패 → 표를 되살리면 회복". 우리 스택에 맞춘 것은 규모다. 원본은 모든 표였고 여기서는 그 기능이 바꾸던 표 하나(`orders`)다. 모든 표를 지우면 가게 표까지 사라져 loadgen 의 메뉴 조회 단계에서 여정이 끝나고(주문 표본이 사라짐) 원인 신호가 흐려진다. 재구성의 안전 장치 하나: 진짜 DROP 은 173만 행을 잃으므로, 실행기는 표를 앱 스키마 밖의 시스템 스키마 `mysql` 로 옮긴다(`mysql.fooddelivery_orders`). DPM 수집기는 `mysql` 스키마를 보고하지 않아(databases.count 4: fooddelivery, information_schema, performance_schema, sys) 앱과 DB 목록 지표에는 DROP 그 자체로 보인다(표, 행, 인덱스가 fooddelivery 에서 빠짐). 행, 인덱스, 외래 키는 표를 따라가고 cleanup 의 같은 RENAME 으로 그대로 돌아온다(로컬 MySQL 8.0 실측, §4).

## 3. 숫자 근거 (2026-10-10, `scenario-stats.py`, 정식 + 후보 합계 63, 이 후보 전)

| 축 | 값 | 판단 |
|---|---|---|
| 서비스 | 쇼핑몰 26, 은행 20, 음식배달 17 | 음식배달이 가장 적다 → 음식배달(17→18) |
| 묶음 | A 7, B 7, D 7, G 7, J 7(각 11%), C 6, L 6(각 9.5%), F 3, P 4, E 2, H 2, K 2, I 1, M 1, O 1, N 0 | P(운영 작업의 대상 착오) 4→5(7.8%). 결함이 앱이 기대하는 표가 없는 꼴로 드러나 F48-R, F48-P 처럼 P+L. L 로 세도 6→7(10.9%) |
| 정답 위치 | DB 테이블(주문) 0 | §2-1 'DB 테이블(<무엇>)' 꼴, 0→1(1.6%). 주문 서비스(7, 11%)가 아니다 |
| 결제 경로 합계 | 10(15.9%) | 결제가 정답이 아니므로 그대로(10/64, 15.6%) |
| 어느 축이든 | 최대는 A, B, D, G, J 7/64(10.9%), 주문 서비스 7/64(10.9%) | 20% 미만 |

묶음 해석: 바뀐 것은 표 하나의 존재다. 설정값이 아니라 G 가 아니고, 코드나 이미지 배포가 없어 J 가 아니며, 계획된 스키마 변경이 코드보다 먼저 운영에 적용된 F36-R(L)과도 다르다: 이 마이그레이션은 운영에 적용할 변경이 아니었고, 의도한 변경 내용(기능 개발 중 주문 표 바꾸기)은 맞았는데 적용 대상(환경)이 틀렸다. 그래서 P 다. DB 프로세스, 자원, 잠금, 질의 비용은 그대로라 A, B, D, F 가 아니다.

관계와 id: 원본(Resend, 다른 환경을 가리킨 마이그레이션)과 도구(표 지우기)가 F48(GitHub, 취소된 백필의 정리 단계)과 달라 새 사례군 F53 의 첫 시나리오 R 로 둔다. 같은 수단(db.ddl)이지만 새 모드(그림자 표 없이 표를 스키마 밖으로)이고 표, 파라미터가 달라 같은 주입 중복(같은 수단과 같은 파라미터)이 아니다.

겉 증상(food 주문 생성 500)은 F38-R(정식), F48-P(후보)와 같고 정답이 다르다(카탈로그 관계 H). 관제 AI 가 "food 주문 500 이면 MySQL 인스턴스" 나 "1146 이면 백필 표" 를 외워 찍지 못하게 하는 짝이다. 가르는 관측 근거는 §8 에 둔다.

### 3단계 후보 목록 (실제 기전 × 부품, 숫자 순으로 줄 세움, 버린 이유 포함)

| 순위 | 후보 | 원본 사례 | 부품(정답 위치 수) | 결과 |
|---|---|---|---|---|
| 1 | 운영을 가리킨 로컬 마이그레이션이 food orders 표를 지움 | Resend 2024-02-21 공식 블로그 | food MySQL, DB 테이블(주문) 0, 음식배달 17 | **채택(F53-R)** |
| 2 | 운영을 가리킨 로컬 마이그레이션이 banking Oracle transfers 를 지움 | Resend 2024-02-21 | banking Oracle, DB 테이블(이체) 0, 은행 20 | 뒤로 미룸: 정답 위치는 같은 0 이지만 은행이 음식배달보다 3 많고, Oracle 은 스키마 사이 RENAME 이 없어 진짜 DROP 과 휴지통(FLASHBACK) 복원이 필요하다(identity, 인덱스 이름이 BIN$ 로 바뀌어 되돌리기 검증이 더 크다). 다음 은행 차례의 후보로 남긴다 |
| 3 | 새 버전의 중첩 트랜잭션이 연결을 쥔 채 두 번째 연결을 기다림(hold-and-wait)으로 풀 고갈 | WorkOS 2026-07-16 공식 블로그(분석 이벤트 쓰기가 중첩 트랜잭션으로 두 번째 연결을 요구, 높은 동시성에서 풀이 서로 기다리는 요청으로 가득) | food payment(결제 경로 10) 또는 order(7), J | 버림(원칙 9, 컨트롤러 필수 중단 조건): 교착은 동시 요청이 풀 크기(15)에 닿아야 난다. 기준선 최대 주문 초당 약 1.1건 + 동반 1.4건 × payment p95 62ms ≈ 동시 0.2 라 일어나지 않는다. order 판은 order 풀이 묶이면 DB 를 보는 readiness 가 빠져 entry 0 |
| 4 | 정렬 규칙(collation) 정렬 마이그레이션이 부모, 자식 표의 맞춤을 깨 인덱스를 못 씀 | Autumn 2026-03-15 공식 블로그(KSUID 열 COLLATE "C" 와 외래 키 열 기본 정렬이 달라 인덱스를 못 씀, 표 넷을 차례로 바꾸다 핵심 질의가 인덱스를 잃고 DB CPU 급증) | DB 테이블(각 표 0~1), F 3 | 버림(원칙 1): 세 도메인의 사용자 경로 질의에 문자열 열끼리 비교하는 조인이 없다(grep: JOIN 은 인기 메뉴 집계의 bigint 조인 하나). PostgreSQL 은 열과 바인드 값 비교에서 열의 정렬이 이겨 인덱스를 쓰고, MySQL 도 같은 문자 집합이면 열 정렬로 맞춘다. 원본의 '두 표 사이 정렬 불일치' 고리를 만들 수 없다 |
| 5 | 백엔드 DB 작업(마이그레이션 잡)이 DB CPU 를 넘겨 여러 기능 시간 초과 | Slack 2025-11-01 공식 상태 페이지 | food MySQL(DB 인스턴스 2), A | 버림(원칙 9): rejected 의 같은 원본 두 행(banking ledger 보존 삭제, food order 정리)과 Onfido 실측의 벽 그대로. 바깥 세션이 CPU 를 다 써도 ms 단위 OLTP 는 거의 굶지 않아 3초 풀 대기나 시간 초과로 이어질 계산이 서지 않는다 |
| 6 | 제공자 실시간 이전으로 디스크 지연 급증 → 잠금 경합 → 전면 장애 | Clerk 2026-03-10 공식 블로그 | food MySQL 디스크(노드, 디스크 6), A | 버림(같은 주입, 원칙 1): 테스트베드 재현 수단이 host.stress io 로 F10-H(MySQL 디스크 IO 포화)와 같은 주입이고, 원본 계기(클라우드 VM 실시간 이전)는 재현할 수 없다 |
| 7 | 배포가 앱이 쓰는 DB 사용자를 바꿔 재시작 때 옛, 새 사용자 연결이 겹쳐 풀러의 파일 기술자 한도 초과 | Adapty 2025-12-05 공식 상태 페이지 | DB 계정(1), O 1 | 버림(원칙 1, 컨트롤러 필수 중단 조건): 테스트베드에 연결 풀러(PgBouncer, ProxySQL 등)가 없고, food 는 네 서비스가 한 계정을 써 order readiness 가 빠지면 entry 0 |
| 8 | 배포 때 새 서비스들이 함께 연결을 열어 DB 연결 프록시 용량을 넘김 | Tracktile 2026-03-03 공식 상태 페이지 | DB 인스턴스(2), E 2 | 버림(원칙 1, 9): 연결 프록시가 없고 서비스별 Hikari 15 × 다섯이 MySQL max_connections 아래다. rejected 의 Resend 2026-02-15(풀 크기 확대가 Oracle 접속 상한 독점) 행과 같은 벽 |
| 9 | 서킷브레이커 임계 오설정이 멀쩡한 하류 호출을 차단 | 공식 사후 보고 없음(웹 검색 1회: dev.to 개인 글, 확인 안 됨) | food order(7), M23 | 버림(원칙 1): rejected 2026-10-08 행과 같은 결론 |
| 10 | 승인 웹훅(failurePolicy Fail)의 백엔드가 없어 새 파드 생성이 모두 거절, 일상 재배포에서 파드 소실 | UK Ministry of Justice Cloud Platform 2020-09-07 공식 런북(검증 웹훅이 새 Ingress 규칙을 막음) | 쿠버네티스 승인(0), banking transfer(6) | 버림(원칙 1): 원본은 Ingress 규칙 생성을 막았는데 테스트베드에 Ingress 가 없다. 파드 승인으로 옮기면 F50-R(할당량 승인 거절 + transfer 재배포)과 계기, 증상, 피해 경로가 같고, 피해가 나는 곳이 maxSurge 0 인 transfer 뿐이다 |
| 11 | 취소된 백필이 food payments 표를 치움 | GitHub 2026-07-24 | DB 테이블(결제) 1, 결제 경로 | 버림(컨트롤러 필수 중단 조건, 원칙 9): payments 164만 행 318MB 라 그림자 복사(INSERT … SELECT, REPEATABLE READ)가 수십 초이고 원본 표 끝의 공유 잠금이 결제 INSERT 를 막아 payment 풀이 묶이면 order 가 결제 대기(최대 10초) 동안 자기 연결을 쥔다(rejected 의 order_items 재검 행과 같은 벽) |

rejected 기록, 장부 §4-1, §5, 이번 실행에서 막힌 목록에 같은 원본(Resend 2024-02-21)이나 같은 주입(표를 스키마 밖으로 옮기는 지우기)이 없다. rejected 의 'food order_items 를 취소된 백필이 치움'(GitHub 원본, 그림자 복사가 큰 표에서 수십 초)과는 원본, 도구, 주입 꼴이 다르고(그림자 복사 없이 메타데이터 한 문장), '시험 작업 환경 변수가 운영 DB 를 가리켜 메뉴, 가게 표를 비움'(Travis CI 원본)과는 피해 꼴이 다르다(그쪽은 빈 표라 400 단일 신호, 이 후보는 표가 없어 SQL 오류 500 과 ERROR 로그).

## 4. 인과 사슬 (코드와 인프라 위치)

1. 엔티티: `food-delivery/order-service/src/main/java/com/fooddelivery/order/entity/Order.java:7-12` `@Entity @Table(name = "orders")`, `@GeneratedValue(strategy = GenerationType.IDENTITY)` 라 `save` 가 곧바로 INSERT 를 보낸다.
2. 주문 생성: `OrderService.java:57-171` `createOrder`(@Transactional). 가게 조회(:60), 메뉴 조회(:73), 배달원 용량 확인(:96) 뒤 첫 쓰기 `orderRepository.save(order)`(:128). 'Created order' 로그(:141), 배차 호출(:145), 결제 호출(:154)은 그 뒤라, 표가 없으면 하나도 실행되지 않는다.
3. 예외 처리: `GlobalExceptionHandler.java:10-20` 는 `ServiceException` 만 다룬다. `InvalidDataAccessResourceUsageException` 은 Spring 기본 처리로 500 이 되고 Tomcat 이 ERROR 'Servlet.service() … threw exception [Request processing failed: … could not execute statement [Table 'fooddelivery.orders' doesn't exist] [insert into orders …]]' 를 남긴다. Hibernate `SqlExceptionHelper` 가 WARN 'SQL Error: 1146, SQLState: 42S02' 와 ERROR "Table 'fooddelivery.orders' doesn't exist" 를 먼저 남긴다.
4. 보존 정리: `OrderRetentionBatch.java:41-57` 가 6초마다 orders 를 읽는다(`select o1_0.id, o1_0.status, o1_0.created_at from orders …`). 실패를 WARN 'Order retention purge failed, will retry next cycle: …' 로 남기고 다음 주기에 다시 한다. 요청량과 무관한 시각 고정 신호다.
5. 그 밖에 orders 를 읽는 곳: 주문 조회 GET /api/orders/{id}, 정리 배치 `OrderCleanupBatch`(1시간), restaurant 인기 메뉴 집계 배치 `MenuRepository.java:15-20`(orders 조인, 1시간). 창 안에 돌면 같은 오류를 낸다.
6. 재시작해도 표를 만들지 않는다: `order-service/src/main/resources/application.yml:20` `ddl-auto: none`.
7. health: order 의 readiness, liveness 는 `/actuator/health`(`20-order-deploy.yaml`)이고 DB 확인은 질의 없는 `isValid()` 라 표가 없어도 UP 이다(로컬 실측).
8. 스키마: `food-delivery/db/init.sql:74-95` orders(PRIMARY, idx_orders_restaurant, idx_orders_customer, idx_orders_created_at, fk_orders_restaurant), order_items 의 `fk_order_items_order` 가 orders 를 참조.
9. 인프라: rca-testbed-food StatefulSet testbed-mysql(파드 testbed-mysql-0, MySQL 8.0.46, tb-w3). 109 실측(2026-10-10): fooddelivery 표 16개, 인덱스 31개(orders 4), orders 약 173만 행, 시스템 스키마 mysql 에 fooddelivery_ 로 시작하는 표 0개, @@lock_wait_timeout 기본 31536000.
10. 로컬 실측(MySQL 8.0, init.sql 시드 2만 행, order, restaurant, dispatch 1.0.0 jar):
    - `RENAME TABLE fooddelivery.orders TO mysql.orders_x` 성공, fooddelivery 표 수 16→15, 옮긴 동안 order_items 의 외래 키가 mysql.orders_x 를 가리키고, 되돌리면 fooddelivery.orders 와 외래 키가 원래대로, 행 2만 그대로, 이어서 INSERT 성공.
    - 표가 없는 동안 POST /api/orders 3/3 이 500(0.06~0.3초), 응답 본문은 Spring 기본 오류. dispatch 'Dispatched courier' 0건(배차 전 실패). order health 내내 UP. 6초 뒤 보존 정리 WARN.
    - 실행기 스크립트(kubectl 대신 docker exec 로 바꾼 시험 껍데기): preflight 0 → run 0 → preflight 1('Table … doesn't exist') → cleanup 0 → cleanup 0(두 번째는 할 일 없음) → recovery 0. cleanup 뒤 주문이 INSERT 와 배차를 지나감.

## 5. 원인 규정 (원칙 5, 6)

| 칸 | 값 | 근거 |
|---|---|---|
| `root_cause.target_id` | `food-mysql:fooddelivery.orders` | 결함은 표 쪽이다(운영을 가리킨 마이그레이션이 살아 있는 표를 지움). 원본 보고의 원인과 복구도 표 지우기와 그 복원이다. 표기는 F48-P, F48-R, F36-R, F33-R 과 같은 '인스턴스:스키마.테이블' 꼴 |
| `trigger_target_id` | null | 근본과 계기가 같은 곳(이 표에 대한 잘못된 대상의 마이그레이션) |
| `scoring.partial` | `food-delivery-order`, `food-order`, `food-mysql` | 오류가 찍히는 서비스와 DB 인스턴스는 원인 근처다 |

원칙 5: 정답은 로그의 오류 문장("없는 표 orders"), 실패한 문장이 주문 INSERT 라는 사실, 같은 DB 의 다른 읽기, 쓰기 성공, 같은 분 DPM 표 수 감소, 그 시각에 배포나 재시작이 없다는 사실로 낼 수 있는 결론이다. 코드 설계 결함 추론이 필요 없다. "order 가 주문 생성에서 SQL 오류를 낸다" 고 답하면 order(부분), "orders 표가 사라졌다(DDL, 운영 작업)" 고 답하면 정답이다. 누가 어느 PC 에서 왜 마이그레이션을 돌렸는지는 관제 데이터에 없으므로 요구하지 않는다.

원칙 6: order 와 배치는 늘 하던 정당한 요청을 보냈고 결함 있는 곳(사라진 표)에서 실패했다. 표를 지운 잘못된 대상의 마이그레이션이 원인이다.

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 | 기대 |
|---|---|---|
| 감지 | order 서버 스팬 오류(골든 시그널), 새 오류 로그 템플릿(1146 WARN, 'doesn't exist' ERROR, Servlet ERROR, 보존 정리 WARN)과 로그 급증 | 주문 생성 100% 500. 보존 정리 WARN 이 부하와 무관하게 6초마다, 여기에 주문마다 세 줄 |
| 피해 판정 | 러너: 동반 부하 주문 생성 5xx 비율 ≥ 0.5 와 2xx 비율 < 0.2(3틱) | 평시 주문 생성 5xx 는 0 근처(배차 한도 503 이 드물게) |
| 원인 설명 | 로그 전수(1146 오류 문장, 실패 문장 'insert into orders', 'Created order' 멈춤, 배차와 결제 로그 없음), DPM 표 수, 인덱스 수, 행 수, dispatch 와 restaurant 의 200, KCM 무변화 | §7 |

인시던트 근거와 위험:

- 같은 order 주문 생성 500 꼴인 F38-R 이 정식(녹화 2026-10-09)이다. F38-R 은 order 외에 dispatch, payment 에도 오류 로그가 났지만, 이 후보는 order 하나에만 오류가 난다. 대신 order 의 새 템플릿이 넷(1146 WARN, 표 없음 ERROR, Servlet ERROR, 보존 정리 WARN)이고 보존 정리 WARN 이 부하와 무관하게 6초마다 이어져 로그 급증 이벤트가 날 것으로 본다.
- 동반 부하(주문 생성 초당 약 1.4건)가 가장 한가한 시간에도 order POST 표본 스팬을 분당 약 8건(10%) 만든다. F48-P 와 달리 실패가 배차 전이라 동반 부하가 배달원 한도를 채우지 않는다.

## 7. 관측 근거 표 (119 실조회, 2026-10-10 05:40~06:20 UTC)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 결정 증거는 전수 로그와 DPM, KCM 이다.

| 증거 | 119 표와 칸 | 조회 | 평시 결과 |
|---|---|---|---|
| 근본: 없는 표 오류 | CH `lucida_logs_local` (`service_name`, `body`) | `SELECT countIf(position(body,'SQL Error: 1146')>0), countIf(position(body,'42S02')>0), countIf(position(body,'Order retention purge failed')>0) FROM lucida.lucida_logs_local WHERE service_name LIKE 'food-delivery-%'` | 1146 0건, 42S02 0건, 보존 정리 실패 1건(2026-10-08 20:33 'Unable to rollback against JDBC Connection', 다른 문장) |
| 근본: 그 표 이름 | 같은 표 | `countIf(position(body,'Table ''fooddelivery.orders'' doesn''t exist')>0)`, `countIf(position(body,'SQLSyntaxErrorException')>0)` (food 전체) | 둘 다 0건. 같은 예외 종류 'InvalidDataAccessResourceUsageException' 은 restaurant 에만 3,952건(2026-10-09 10:19~10:34 UTC, F36-R 실행 창의 1054), order 0건 |
| 주문 로그 범위 | 같은 표 | `min(timestamp), countIf(position(body,'Created order id=')>0), count() WHERE service_name='food-delivery-order'` | 가장 이른 로그 2026-10-03 04:00 UTC, 'Created order' 414,149건, 전체 855,151건 |
| 기준선 주문량 | 같은 표 | 'Created order id=' 를 KST 시각별 7일 평균 | 분당 11.8(06시)~68.4(17시)건 |
| 계기: 표 지우기 | VM `dpm.mysql.database.table_count{db_name="fooddelivery"}`, `index_count`, `row_count` (target c8c558e5-…, 60초 간격) | 현재값, `min_over_time`, `max_over_time` [7d] | table_count 7일 16 고정, index_count 30~31(F33-R 의 인덱스 제거 실행), 현재 31. row_count 현재 11,398,931. 109 실측으로 orders 인덱스 4개, 약 173만 행이라 지우면 15, 27, 약 170만 감소 |
| 계기: 시스템 스키마는 보고 안 함 | VM `dpm.mysql.database.*` 의 `db_name` 값, `dpm.mysql.databases.count` | 시리즈 목록 | db_name 은 fooddelivery, information_schema, performance_schema, sys 넷뿐이고 databases.count 4. mysql 스키마로 옮긴 표는 어느 지표에도 새로 나타나지 않는다 |
| 계기: DDL 문장 자체 | CH `dpm_topsql_local` | F48-R, F48-P 시트와 같음 | DDL 은 Top SQL 에 남지 않는다 |
| 다른 변경 없음 | CH `kcm_events_local` | `namespace='rca-testbed-food' AND position(object_name,'testbed-order')=1` 사유별 | Unhealthy 196건(마지막 2026-10-09 03:43), ScalingReplicaSet 8건(마지막 2026-10-08 18:53), Killing, Pulled, Created, Started 각 10건. 롤아웃은 이 표에 남는다 |
| 피해: 주문 생성 스팬 | CH `otel_traces_local` (`span_kind`, `span_name`, `span_attributes['http.response.status_code']`) | 7일 order 서버 스팬 | POST /api/orders 20,516(5xx 530, 대부분 F38-R, F49-H 등 시나리오 실행 창), health 5,145(5xx 2) |
| 배제: 롤아웃이 아님 | 같은 KCM 표 | 위와 같음 | 주입은 쿠버네티스 객체를 건드리지 않는다 |

## 8. 감별

- must_support: 1146 과 "Table 'fooddelivery.orders' doesn't exist" 오류 로그(평시 0), 실패 문장이 주문 INSERT 이고 'Created order' 가 멈추며 그 시도에 배차, 결제 로그가 없음, 6초마다 보존 정리 WARN, 같은 분 DPM 표 수 16→15, 인덱스 수 31→27, 행 수 약 170만 감소, 둘러보기와 배달 추적 200, 롤아웃과 재시작 없음, MySQL 과 order 풀 정상.
- must_rule_out(정답지에 문장으로): order 새 버전, 취소된 백필이 다른 표를 치움(F48-P), MySQL 인스턴스 읽기 전용(F38-R), MySQL 다운이나 재시작, 하류(가게, 배차, 결제) 고장, 부하 증가나 용량 부족.
- contrast_with: F48-P, F38-R, F48-R, F36-R.
- F48-P 와 가르는 관측 근거(같은 주문 500, 같은 1146):
  - 오류가 가리키는 표: F48-P 는 order_outbox_events, F53-R 은 orders.
  - 표 수 방향: F48-P 는 그림자 표로 16→17(인덱스 31→34), F53-R 은 16→15(인덱스 31→27)이고 row_count 가 약 170만 줄어든다.
  - 실패 지점: F48-P 는 마지막 outbox 기록이라 'Created order' 가 찍히고 배차, 결제가 이미 끝나 있다. F53-R 은 첫 주문 INSERT 라 'Created order' 가 멈추고 배차, 결제 로그가 없다.
  - 시각 고정 신호: F48-P 는 2초마다 릴레이 스케줄러 ERROR, F53-R 은 6초마다 보존 정리 WARN.
- F38-R 과 가르는 관측 근거: F38-R 은 1290 '--read-only', dispatch, payment 쓰기도 실패(녹화 창 로그 order 3,874, dispatch 1,298, payment 966건). F53-R 은 1146, orders 를 부르는 문장만 실패한다.
- 판별력: 서비스 입도 채점에서는 F38-R, F48-P, F49-H 와 같은 'order' 근처 답이 부분 점수다. 정답 입도(database-relation)에서는 표 이름이 정답이고, 인스턴스(food-mysql)는 부분 점수다.

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

진행 대본: 설계 강도 하나로 고정한 평가 모드(`approved-fixed-f53-r`). 강도라는 축이 없다: 표가 없으면 그 표를 부르는 문장은 요청량과 무관하게 100% 실패한다.

| 항목 | 값 | 근거 |
|---|---|---|
| primary | db.ddl(표 지우기 모드), 계약 `{engine mysql, rca-testbed-food, testbed-mysql-0, fooddelivery, orders, hold_schema mysql, minimum_rows 1000}` | 실행기 CONTRACTS 와 profiles.json 같은 값(G2). 새 모드 MYSQL_DROP_REMOTE(테스트로 고정) |
| companion | load.north_south, food order-surge.js 2rps, entry 30181, seed 5353 | F38-R, F49-H 와 같은 값. 실패가 배차 전이라 배달원 한도를 채우지 않는다 |
| entry_status | 동반 부하 k6 의 order, restaurant NodePort | 고장 중 500(0 아님). 0 은 노드나 order 가 죽었을 때만 |
| min_hold / settle / timeout | 15m / 30s / 20m, max_injection 25m | F38-R, F48-P 와 같음 |
| success(3틱) | 동반 부하 주문 생성 5xx 비율 ≥ 0.5, 2xx 비율 < 0.2 | loadgen 집계(표본 아님), 30초 창에 동반만 약 42건 |
| must_rule_out(2틱) | achieved_rps < 0.5, MySQL NotReady, order NotReady | 부하 끊김, DB 다운, order 다운이면 다른 시나리오다 |
| abort | entry_status == 0 (필수) | |
| recovery(2틱, 10m) | target_health 200, MySQL Ready, order Ready, 기준선 주문 2xx ≥ 0.7 | 되돌리면 다음 주문부터 즉시 INSERT 와 배차를 지남(로컬 실측) |

부하 계산:

- 동반 부하: order-surge.js 2rps 중 주문 생성이 초당 약 1.4건, 30초 창 약 42건. 기준선 주문 생성 분당 약 12~68건이 더해진다. 고장 중 모두 500 → 5xx 비율 1.0, 2xx 비율 0. 문턱(0.5, 0.2)을 어느 시간대에도 넘는다.
- 배달원 한도: 실패가 용량 확인 뒤, 배차 요청 전이라 ASSIGNED 가 늘지 않는다(로컬 실측 'Dispatched courier' 0건). F48-P 가 동반 부하를 두지 못한 이유(되돌려진 주문의 배차가 한도를 채움)가 여기에는 없다.
- 강도와 피해: 주입은 메타데이터 문장 하나라 표 크기(173만 행)와 무관하게 즉시 끝난다. RENAME 은 orders 의 배타적 메타데이터 잠금이 필요해 진행 중인 주문 트랜잭션(평소 수백 ms)을 기다리고, 그동안 새 주문 문장이 그 뒤에 줄 선다. lock_wait_timeout 10초가 상한이다. 1시간 주기 인기 메뉴 집계(약 5초 조인)와 겹치면 최대 약 5초 기다릴 수 있다: 그동안 order 가 쥐는 연결은 (기준선 최대 약 1.1 + 동반 1.4) 건/초 × 5초 ≈ 13개로 풀 15 아래이고, readiness 는 10초 주기 3회 실패가 필요해 빠지지 않는다. 10초 안에 잠금을 못 얻으면 아무것도 바뀌지 않은 채 run 이 실패하고 MySQL 오류 첫 줄이 러너 로그에 남는다.
- 실패 뒤: order 는 가게, 메뉴, 용량 확인(평소 수십 ms) 뒤 즉시 SQL 오류로 끝나 풀을 오래 쥐지 않는다(로컬 실측 0.06초). health 는 isValid() 라 Ready 다.
- 서킷브레이커: order 의 restaurant, dispatch 서킷은 하류가 200 이라 열리지 않고, payment 는 부르지 않는다. order 앞에는 서킷이 없어 손님이 500 을 그대로 받는다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Resend 2024-02-21, 공식 블로그 사고 보고)와 요소별 대응표가 있고 기전이 같다(규모만 모든 표 → 그 기능의 표 하나) (§2)
- [x] 분류 장부 §4-1, §6 갱신. 묶음 P 4→5(7.8%, L 로 세도 10.9%), 정답 위치 DB 테이블(주문) 0→1(1.6%), 결제 경로 10/64(15.6%), 음식배달 17→18. 어느 축도 20% 미만 (§3)
- [x] 근본 원인 위치: `Order.java:7-12`, `OrderService.java:128`, `OrderRetentionBatch.java:41-57`, 인프라 testbed-mysql-0 fooddelivery.orders (§4)
- [x] 근본 원인 흔적: CH `lucida_logs_local` 1146 과 표 이름 오류 문장(전수, 평시 0건). 같은 로거의 문장 꼴은 로컬 실측과 F48-P 시트의 수집 경로로 확인 (§4, §7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: 로그 전수, DPM 표 수, 인덱스 수, 행 수, KCM. 스팬은 보조 (§7)
- [x] 계기 흔적: DPM table_count 16→15, index_count 31→27, row_count 약 170만 감소(1분 간격, 표 수 7일 동안 16 고정), 오류 문장의 표 이름, 그 시각에 롤아웃과 재시작 없음. 인공 지연 없음 (§7)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다 (§5)
- [x] 정답지 세 칸이 원칙 6대로 (§5)
- [x] 감지, 피해 판정, RCA 증거 구분과 인시던트 위험 (§6)
- [x] 주입 도구가 시나리오 id 를 남기지 않는다: 옮긴 표 이름은 `fooddelivery_orders` 이고 인자와 스크립트에 id 없음(테스트로 고정). 동반 부하 k6 태그는 기존 시나리오들과 같은 경로
- [x] 부하 상한과 서킷브레이커를 고려한 피해 계산, 메타데이터 잠금 대기의 최악 경우 (§9)

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- order 로그에 'SQL Error: 1146, SQLState: 42S02' 와 "Table 'fooddelivery.orders' doesn't exist", 'Servlet.service() … [insert into orders …]' 가 주문마다, 'Order retention purge failed' 가 6초마다 남는지. POST /api/orders 스팬이 500 인지.
- 'Created order id=' 가 멈추고 그 시도에 dispatch 'Dispatched courier', payment 'Processed payment' 가 나오지 않는지, 둘러보기와 배달 추적은 200 인지.
- VM 의 `dpm.mysql.database.table_count{db_name="fooddelivery"}` 가 주입 분에 15, `index_count` 가 27 이 되고 `row_count` 가 약 170만 줄었다가 cleanup 뒤 16, 31 로 돌아오는지. `dpm.mysql.databases.count` 가 4 그대로인지(시스템 스키마가 보고되지 않는다는 가정 확인).
- 인시던트: 묶음 멤버 템플릿과 판정 사유, promoted_at, 실행 시각대.
- 주입이 메타데이터 잠금 대기로 실패(lock_wait_timeout 10초)하면 아무것도 바뀌지 않은 채 run 이 실패하고 MySQL 오류 첫 줄이 러너 로그에 남는다.
- cleanup 뒤 fooddelivery.orders 가 행 그대로이고(약 173만) `mysql.fooddelivery_orders` 가 없는지(recovery 가 확인), order_items 의 외래 키가 fooddelivery.orders 를 가리키는지.
- 녹화 창에 food MySQL OOM 재시작이 끼면 녹화로 쓰지 않는다. 재시작이 끼어도 표는 mysql 스키마에 남아 있고 cleanup 이 되돌린다.
