---
title: F54-P 설계 시트 — food MySQL 앱 계정에 건 시간당 쓰기 한도가 주문 저장을 막음
status: Draft
owner: project
last_reviewed: 2026-10-11
tags:
  - scenario
  - design
  - database
summary: DB 자원 한도 정비가 food MySQL 애플리케이션 계정 fooddelivery 에 시간당 쓰기 한도(MAX_UPDATES_PER_HOUR 500)를 걸었는데 한도가 계정의 평시 쓰기(분당 약 130~980 문장)보다 한참 낮아, 풀이 연결을 바꾸는 대로 공유 카운터가 다 쓰이고 그 뒤 모든 주문 저장이 MySQL 1226 으로 거절되어 음식 주문이 500 이 되는 시나리오(Google 2020-12-14 재구성)의 설계 근거.
---

# F54-P 설계 시트

- id `F54-P`, slug `f54-p-food-mysql-app-account-hourly-write-cap-breaks-orders`, 사례군 F54(F54-R 과 같은 원본, 같은 '계정 자원 한도' 꼴의 다른 엔진, 다른 한도 종류, 다른 피해 모양이라 P)
- 상태: 후보(ready + `stage: candidate`), 설계 강도 하나로 고정한 evaluation 모드, 녹화 대기 큐에 추가
- 주입 수단: 기존 실행기 `db.account`(`scripts/scenarios/profiles/db_account_executor.py`)에 MySQL 시간당 쓰기 한도 모드를 더함. 동반 부하 없음

## 1. 요약과 비유

food 의 order, restaurant, dispatch, payment 는 MySQL 인스턴스 하나에 애플리케이션 계정 `'fooddelivery'@'%'` 하나로 접속하고, 이 계정에는 자원 한도가 없다.
DB 자원 한도 정비가 "폭주하는 배치 쓰기를 막자"며 `ALTER USER 'fooddelivery'@'%' WITH MAX_UPDATES_PER_HOUR 500` 을 건다.
그런데 이 계정은 평시에도 분당 수백에서 천 건 가까운 쓰기 문장을 낸다(시간당 수만 건). MySQL 은 시간당 카운터를 로그인 때 연결에 붙이므로 이미 열린 연결은 그대로이고,
Hikari 풀이 연결을 바꾸는 대로(최대 10분) 새 연결들이 계정 카운터 하나를 함께 쓴다. 카운터가 몇 분 만에 500 을 넘으면 그 연결들의 INSERT, UPDATE, DELETE 가 모두
`1226 User 'fooddelivery' has exceeded the 'max_updates' resource (current value: 500)` 로 거절된다. 약 10분 뒤에는 모든 주문 저장이 실패해 POST /api/orders 가 500 이다.
읽기, 연결 확인, readiness 는 그대로라 가게와 메뉴 둘러보기, 배달 조회는 정상이고 파드는 하나도 재시작하지 않는다.

비유: 회사가 "법인카드 한 장으로 한 시간에 500 건까지만 결제"라는 규칙을 새로 걸었는데, 그 카드는 매장 네 곳이 함께 쓰고 평소에 한 시간에 수만 건을 긁는다.
이미 단말기에 꽂혀 있던 카드는 옛 규칙대로 결제되다가, 단말기가 카드를 새로 읽을 때마다 새 규칙이 붙고, 공유 한도 500 건이 몇 분 만에 차면 그때부터 모든 결제가 "한도 초과"다.
잔액 조회(읽기)는 되고 카드도 정지되지 않았다. 문제는 카드가 아니라 카드에 걸린 한도 값이다.

## 2. 원본 사례

- **Google, 2020-12-14**, Google Cloud 상태 페이지 사고 보고 [공식](https://status.cloud.google.com/incident/zall/20013). `docs/ref-real-world-incidents.md` M1 에 이미 있는 행이고 F54-R 과 같은 원본이다. 2026-10-11 WebFetch 로 원문 다시 확인:
  - "parts of the previous quota system were left in place which incorrectly reported the usage for the User ID Service as 0."
  - "An existing grace period on enforcing quota restrictions delayed the impact, which eventually expired,"
  - "As a result, the quota for the account database was reduced, which prevented the Paxos leader from writing. Shortly after, the majority of read operations became outdated which resulted in errors on authentication lookups."
  - "At 04:08 the root cause and a potential fix were identified, ... disabling the quota enforcement in one datacenter at 04:22", "at 04:27 the same mitigation was applied to all datacenters, which returned error rates to normal levels by 04:33."
- 원본의 쿼터는 **계정 DB 의 쓰기**에 걸렸다. F54-R 은 같은 원본을 Oracle 의 호출당 읽기 한도로 옮겼고, F54-P 는 원본에 더 가깝게 계정의 쓰기 양 한도로 옮긴다.

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 자동 쿼터 관리가 한 서비스의 사용량을 0 으로 잘못 보고받아 쿼터를 줄임(유예 기간 뒤 적용) | DB 자원 한도 정비가 앱 계정 fooddelivery 에 시간당 쓰기 한도 500 을 검(한도를 계정의 실제 쓰기 양으로 잡지 않음) |
| 원인이 된 결함 | 계정 DB 쿼터가 그 서비스의 실제 쓰기 양 아래로 내려감, 안전 검사가 막지 못함 | 한도 500/시간이 계정의 평시 쓰기(분당 약 130~980, 시간당 수만)보다 한참 낮음 |
| 전파 경로 | 쿼터 때문에 Paxos 리더가 쓰기를 못 함 → 읽기 대부분이 낡음 → 인증 조회 오류 → 인증이 필요한 API 전반 5xx | 공유 카운터가 차면 계정의 모든 데이터 변경 문장이 1226 으로 거절 → order 주문 저장 실패 → POST /api/orders 500. 배치와 outbox 쓰기도 같은 오류 |
| 지연 | 기존 유예 기간이 영향을 늦춤 | MySQL 이 한도를 로그인 때 붙여 기존 연결은 그대로, 풀이 연결을 바꾸는 대로(최대 10분) 번짐 |
| 사용자 증상 | Google Cloud, Workspace 의 인증이 필요한 API 5xx | 음식 주문이 모두 500. 둘러보기, 배달 조회는 정상 |
| 원인 쪽 상태 | DB 는 떠 있고 쿼터만 깎임 | MySQL 과 모든 파드는 Ready, 로그인과 읽기 정상, 한도 값만 바뀜 |
| 탐지된 경로 | 용량 경보(03:43), User ID Service 오류 경보(03:46) | order 오류율(POST /api/orders 500), food 세 서비스의 새 로그 문구 1226 'max_updates', DPM 쓰기 지표 급감, rollback 증가 |
| 완화와 복구 | 쿼터 적용을 꺼서 회복(04:22~04:27, 04:33 정상) | cleanup 이 한도를 0(없음)으로 되돌리면 한도를 지닌 연결도 즉시 쓰기 회복 |

기전 확인: 원인(계정 DB 의 쓰기 한도를 실제 사용량 아래로 잡은 변경) → 정상 쓰기 거절 → 그 쓰기에 기대는 사용자 요청 5xx 라는 고리가 원본과 같다.
원본은 쓰기 거절 뒤 읽기가 낡아 인증 조회가 실패하는 한 단계를 더 거쳤고, 테스트베드는 쓰기 거절이 바로 주문 실패가 된다. 원인과 원인 쪽 상태(DB 는 멀쩡, 한도만 바뀜), 완화(한도 해제)는 같다.
원본의 "유예 기간"은 테스트베드에서 "로그인 때 붙는 한도"라는 MySQL 성질로 나타난다. 둘 다 변경 시각과 피해 시각을 벌리는 성질이고 증상의 꼴을 바꾸지 않는다.

## 3. 숫자 근거 (2026-10-11, `scenario-stats.py`, 정식 + 후보 89 종, 이 후보 전)

- 서비스: commerce 31, core-banking 30, **food-delivery 28 → 29**. 음식배달이 가장 적다.
- 묶음: A 7, B 7, C 6, D 11, E 4, F 3, **G 11 → 12**, H 2, I 1, J 14, K 2, L 7, M 1, O 1, P 10, Q 2. 20% 상한(18)에 닿는 묶음 없음(가장 큰 J 15.7%).
  주 묶음 G(잘못된 설정값, 여기서는 DB 계정의 한도 값을 배포), 피해 모양이 한도에 걸린 정상 요청이라 G+A(F54-R 과 같은 해석). G 는 12/90 = 13.3%.
- 정답 위치: **DB 계정 2 → 3**(90 중 3.3%). 가장 큰 위치는 주문 서비스 8(9.0%). 결제 경로 12/89(13.5%, 이 후보는 무관).
- 부품 지도: food 의 0 부품은 kafka 뿐(세 도메인 모두 발행이 비동기 outbox 라 사용자 증상이 없어 이번 실행에서 계속 막힘). food MySQL 10 중 계정은 0(F35-R, F54-R 은 banking Oracle 계정).
- 왜 이 후보인가: 음식배달이 가장 적은 서비스 축이고, 정답 위치 DB 계정이 3% 로 적다. 겉 증상(food 모든 쓰기 거절, 주문 500, 읽기와 파드 정상)이 정식 F38-R(인스턴스 read_only)과 같고 정답이 다른 H 성격이라,
  관제 AI 는 오류 문구(1226 'max_updates' 대 1290 '--read-only')와 번지는 모양(연결이 바뀌는 대로 몇 분에 걸쳐 대 한순간)으로 계정과 인스턴스를 갈라야 한다(지시서: 증상이 같고 정답이 다른 후보는 가치가 크다).
- 이전 반려와의 관계: 2026-10-08 의 같은 수단 반려 행(rejected 24행 'food MySQL 앱 계정 쓰기 한도(MAX_UPDATES_PER_HOUR)')의 사유는 "증상 중복(F38-R)"과 "원칙 1(PostHog 리미터 원본과 기전 고리가 멂)"이었다.
  증상 겹침은 2026-10-09 사용자 정정으로 버릴 사유가 아니고, 원본을 계정 DB 쓰기 쿼터가 깎인 Google 2020-12-14 로 바꿔 원칙 1 을 맞췄다. 같은 행의 다른 관찰("MAX_QUERIES_PER_HOUR 는 접속 초기화까지 막아 풀이 마름")은 그대로 받아들여 쓰기 한도만 쓴다.

## 4. 인과 사슬 (코드와 인프라 위치)

```
db.account(MySQL 모드): testbed-mysql-0 안 mysql 클라이언트(root)
  preflight: mysql.user 의 'fooddelivery'@'%' 네 한도(max_updates|max_questions|max_connections|max_user_connections) = 0|0|0|0
  run: ALTER USER 'fooddelivery'@'%' WITH MAX_UPDATES_PER_HOUR 500 → 500|0|0|0 확인
  → 이미 열린 연결: 카운터 없음(세지도 막지도 않음)
  → Hikari 가 새로 연 연결: 계정 카운터 하나를 함께 씀(INSERT, UPDATE, DELETE 만 셈)
       order, restaurant, payment(minimum-idle 5), dispatch(3): minimum-idle 위는 idle-timeout 60초, 모두 max-lifetime 10분 안에 교체
  → 공유 카운터 500 초과(평시 쓰기 분당 약 130~980 의 일부만 세어도 몇 분)
  → 한도 있는 연결의 데이터 변경 문장 = 1226, SELECT/SET/COMMIT/ping = 정상
  → order POST /api/orders: OrderService.createOrder(@Transactional)
       restaurantClient.getRestaurant, getMenu, dispatchClient.checkCapacity → 200(읽기)
       orderRepository.save(order) → IDENTITY 라 INSERT INTO orders 즉시 실행 → 1226
       → Hibernate WARN 'SQL Error: 1226, SQLState: 42000', ERROR 'User 'fooddelivery' has exceeded the 'max_updates' resource (current value: 500)'
       → GlobalExceptionHandler 가 ServiceException 만 매핑 → 처리되지 않은 예외로 500(Tomcat SEVERE), ROLLBACK
  → dispatch deliverExpiredDispatches(30초), order/dispatch/payment outbox 발행(UPDATE)과 정리(DELETE), 보존 정리 → 같은 오류
  ↔ 가게, 메뉴, 인기 메뉴, 배차 용량, 주문 조회, 배달 조회 → 200 그대로
  ↔ /actuator/health(DataSource 연결 확인) → UP, 모든 파드 Ready
  cleanup: ALTER USER ... WITH MAX_UPDATES_PER_HOUR 0 → 0|0|0|0, 한도 있던 연결도 즉시 쓰기 회복
```

앵커(정답지 `code_anchor` 와 같음):

- `food-delivery/order-service/src/main/java/com/fooddelivery/order/service/OrderService.java:57-170` (createOrder, 128행 save 에서 첫 쓰기)
- `food-delivery/order-service/src/main/java/com/fooddelivery/order/entity/Order.java:12` (`@GeneratedValue(strategy = GenerationType.IDENTITY)`)
- `food-delivery/order-service/src/main/java/com/fooddelivery/order/config/GlobalExceptionHandler.java:9-21`
- `food-delivery/dispatch-service/src/main/java/com/fooddelivery/dispatch/service/DispatchService.java:119-130` (deliverExpiredDispatches)
- `food-delivery/order-service/src/main/resources/application.yml:7-17` (계정 fooddelivery, minimum-idle 5, idle-timeout 60000, max-lifetime 600000). restaurant, payment 도 같은 값, dispatch 는 minimum-idle 3
- `food-delivery/k8s/01-secrets.yaml:10-11` (MYSQL_USER fooddelivery, 네 Deployment 의 DB_USER 가 이 값. `20-order-deploy.yaml:39-43`)
- `food-delivery/k8s/10-mysql.yaml:1-23` (StatefulSet testbed-mysql, mysql:8.0)

인프라 실측(109, 2026-10-11 읽기 전용):

- MySQL 8.0.46. `mysql.user` 의 fooddelivery@%, lucida_mon@%, root 모두 네 한도 0. 전역 `max_user_connections` 0, `max_connections` 151.
- `performance_schema.accounts`: fooddelivery 가 파드 IP 별로 현재 5, 5, 5, 3 연결, 누적 약 1,900~2,060 연결(풀이 꾸준히 교체한다는 뜻).
- `performance_schema.events_statements_summary_by_user_by_event_name` 1분 차(10:15:23 → 10:16:23 UTC): insert +597, update +360, delete_multi +26, delete 0, select +1,481, set_option +3,594, commit +1,089, rollback +24, Ping +272.
  곧 이 시각 쓰기 문장은 분당 약 983(시간당 약 5만 9천).
- 실행기 preflight 를 109 에서 읽기 전용으로 돌려 통과(rc 0).

로컬 실측(mysql:8.0 컨테이너, `SELECT VERSION()` 8.0.46, 2026-10-11, pymysql):

- 한도 0 에서 연 연결 A, `MAX_UPDATES_PER_HOUR 5` 를 건 뒤 INSERT 8번 → 모두 성공(옛 연결은 세지도 막지도 않음).
- 그 뒤 연 연결 B: INSERT 5번 성공, 6번째부터 `(1226, "User 'fooddelivery' has exceeded the 'max_updates' resource (current value: 5)")`. 같은 B 에서 SELECT, `SET SESSION TRANSACTION READ ONLY`, `READ WRITE`, `SET autocommit=0/1`, `SET NAMES`, COMMIT, ROLLBACK 은 성공, UPDATE, DELETE 는 1226.
- 또 연 연결 C: SELECT 성공, 첫 INSERT 부터 1226(카운터를 B 와 함께 씀). 옛 연결 A 는 그때도 INSERT 성공.
- `MAX_UPDATES_PER_HOUR 0` 으로 되돌리자 B, C 모두 곧바로 INSERT 성공.
- 거절된 INSERT 는 `Com_insert` 에 세지 않는다(한도 3, 시도 6 → Com_insert +3).
- 실행기 원격 스크립트를 가짜 kubectl(docker exec)로 같은 컨테이너에 돌림: preflight 0, run 0, 두 번째 run 1(이미 한도), cleanup 0, cleanup 0(멱등), recovery 0, preflight 0. 다른 한도(MAX_USER_CONNECTIONS 3)가 남아 있으면 preflight 1, 없는 계정 1, 계약 밖 자원(MAX_QUERIES_PER_HOUR) 2.

## 5. 원인 규정 (원칙 5, 6)

| 칸 | 값 | 근거 |
|---|---|---|
| `root_cause.target_id` | `food-mysql:fooddelivery` | 결함은 DB 계정에 걸린 한도 값이다. 고쳐야 재발이 막히는 곳이 그 계정의 한도다. 표기는 F54-R `banking-oracle:BANKING`, F35-R 과 같은 '인스턴스:계정' 꼴. 이 DB 는 계정과 스키마 이름이 같다 |
| `trigger_target_id` | null | 근본과 계기가 같은 곳(계정의 한도 변경) |
| `scoring.partial` | `food-mysql`, order, dispatch, payment 서비스 이름 | 인스턴스까지만 지목한 답, 오류를 낸 서비스는 원인 근처다 |

- 채점 입도 `database-relation`, accept `food-mysql:fooddelivery`.
- 원칙 6: order, dispatch, payment 는 늘 하던 정당한 쓰기를 보냈다. 잘못된 것은 그것을 거절하는 한도다. 원본도 쿼터 변경을 원인으로 적었다(같은 층위).
- 원칙 5: 정답은 "fooddelivery 계정에 새로 걸린 시간당 쓰기 한도가 모든 쓰기를 막았다"이고, 관제 데이터(1226 문구가 계정 이름과 'max_updates' 자원을 말함, 네 서비스의 여러 표 쓰기가 같은 문구로 실패, 읽기 정상, DPM 쓰기 지표만 급감, 배포 없음)로 낼 수 있다.
  한도 값이 어떻게 정해졌는지, 왜 공유 계정인지는 요구하지 않는다. "food MySQL 이 쓰기를 거절한다"까지만 답하면 인스턴스(partial)다.

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 | 기대 |
|---|---|---|
| 감지 | order 서버 스팬 오류(POST /api/orders 500, 골든 시그널), 새 로그 템플릿 ERROR '... has exceeded the 'max_updates' resource ...'(보존 로그 0건)와 그것을 담은 Tomcat SEVERE 본문, order 로그 surge, DPM rollback_count 상승과 insert_count 급감 | F38-R(같은 꼴의 인스턴스 read_only, 정식)이 같은 경로로 녹화되었다 |
| 피해 판정 | 러너: 기준선의 주문 생성 5xx 비율 ≥ 0.5, 2xx 비율 < 0.2(3틱) | 평시 5xx 0 근처(배차 한도 503 이 드물게), 2xx 0.9 이상 |
| 원인 설명 | 로그 전수(1226 문구, 네 서비스), DPM 쓰기 지표, KCM 무변화, 읽기 정상 | §7 |

- 인시던트 근거: order 가 입구 서비스로서 POST /api/orders 에 바로 500(ERROR 서버 스팬)을 낸다. 표본 10% 에서 가장 한가한 시간(2026-10-10 20~21시 UTC)에도 POST /api/orders 표본이 시간당 70~77건(분당 약 1.2), 최근 1시간 419건이다.
  전면 실패 구간(5분 넘게)에 order 표본 오류가 분당 1~7건 이어져 평시(F38-R 시트의 7일 53,351 중 58, 0.11%)와 크게 다르다.
- F38-R 과 다른 점: 실패가 한순간이 아니라 몇 분에 걸쳐 늘어난다. 그래서 이벤트가 F38-R 보다 늦게(공유 카운터가 차고 옛 연결이 줄어드는 2~10분째) 생기고, min_hold 15분이 전면 실패 구간을 5분 넘게 판정과 녹화에 담는다.

## 7. 관측 근거 표 (119 실조회, 2026-10-11 10:10~10:35 UTC)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 결정 증거는 전수 로그와 DPM, KCM 이다.

| 증거 | 119 표와 칸 | 조회 | 평시 결과 | 표본 여부 |
|---|---|---|---|---|
| 근본: 계정 한도 초과 문구 | CH `lucida.lucida_logs_local` (`service_name`, `severity_text`, `body`) | `SELECT countIf(position(body,'max_updates')>0), countIf(position(body,'SQL Error: 1226')>0), countIf(position(body,'has exceeded the')>0), countIf(position(body,'resource (current value')>0) FROM lucida.lucida_logs_local WHERE timestamp > now() - INTERVAL 10 DAY` | 모든 서비스 0, 0, 0, 0 (보존 시작 2026-10-04 08:02, 1,375만 행) | 전수 |
| 같은 로거가 CH 에 남는 경로 | 같은 표 | food 서비스의 'SQL Error:', 'read-only' 집계(9일) | F38-R 실행(2026-10-09 17:14~17:29): order WARN 'SQL Error: 1290, SQLState: HY000' 1,937, ERROR '--read-only option ...' 1,937, SEVERE 'Servlet.service() ... threw exception' 1,454, dispatch 664, payment 483, 세 서비스 'Failed to publish outbox event ... will retry next cycle' 각 약 453, 'Outbox purge failed' 각 30. Hibernate SqlExceptionHelper 의 WARN(코드, 상태)과 ERROR(드라이버 문장)가 전수로 남는다. 1226 도 같은 경로 | 전수 |
| 근본: DB 쪽 쓰기 정지 | VM `dpm.mysql.sql.insert_count`, `update_count`, `select_count`, `rollback_count` (target_id c8c558e5-…, 분 단위) | `quantile_over_time(q, ...[7d])` | insert 중앙 443, 1백분위 83 / update 중앙 258, 1백분위 49 / select 중앙 1,252, 99백분위 1,930 / rollback 중앙 2.9, 99백분위 29.5. 장애 중 insert, update 는 0 근처로 떨어지고 select 는 이어진다(거절 문장은 Com_* 에 안 셈, 로컬 실측) | 1분 수집 |
| 근본: 계정의 쓰기 양(한도가 사용량 아래) | MySQL performance_schema(109 읽기 전용) | §4 1분 차 | 쓰기 문장 분당 약 983. DPM 1백분위(insert+update 약 132)로도 시간당 약 7,900 이라 500 은 어느 시간대든 몇 분 분량 | 직접 조회 |
| 계기: ALTER USER 자체 | CH `lucida.dpm_topsql_local` (`engine='mysql'`, `body`) | `countIf(position(upper(body),'ALTER USER')>0)`, `'MAX_UPDATES'` | 0, 0(2026-08-13 부터 131만 행). Top SQL 은 자주 도는 다이제스트만 남기고 오류 수 칸이 없다. 명령 자체는 119 에 남지 않고, 계기는 그 변경이 만든 오류 문구(계정 이름과 자원 이름이 들어 있음)로 확인된다(F54-R, F38-R 과 같은 기준) | 1분 수집 |
| 다른 변경 없음 | CH `lucida.kcm_events_local` (`namespace`, `reason`) | `namespace='rca-testbed-food'` 7일 reason 별 | Unhealthy 253, ScalingReplicaSet 59, Killing 30 등은 모두 다른 시나리오 실행 창(마지막 2026-10-11 06:53). 장애 구간에 ScalingReplicaSet, Killing, Unhealthy 가 없어야 한다 | 전수 |
| 피해: order 엔드포인트 | CH `lucida.otel_traces_local` (`service_name`, `span_name`, `status_code`) | 최근 1시간 order 서버 스팬 | POST /api/orders 419(오류 0), GET /actuator/health 59. 가장 한가한 시간대 POST 표본 시간당 70~77. 장애 중 POST 만 ERROR | 10% 표본(보조) |
| 피해 보조: 주문 생성 로그 | 같은 로그 표 | 최근 1시간 order 'Created order' | 3,923건(분당 약 65). 장애 중 0 으로 끊김 | 전수 |
| 배제: DB 정상 | VM `dpm.mysql.session.active_session`, `dpm.mysql.instance.avg_query_response_time` | `quantile_over_time(0.5/0.99, ...[7d])` | 활성 세션 중앙 1, 99백분위 7 / 평균 응답 시간 중앙 7.01ms, 99백분위 13.26ms. 장애 중에도 평시 범위(쓰기는 기다리지 않고 즉시 거절) | 1분 수집 |

## 8. 감별

- must_support(정답지 5개): 세 서비스의 1226 'max_updates' 문구(평시 0), POST /api/orders 500 과 읽기 200 의 갈림(몇 분에 걸쳐 증가), 서로 다른 표 쓰기(만료 배치, outbox 발행과 정리)가 같은 문구로 실패, DPM 쓰기 지표 0 근처와 select 유지, rollback 상승, KCM 무변화와 파드 Ready.
- must_rule_out(정답지 6개): 인스턴스 read_only(F38-R, 1290), 계정 잠금(F35-R), 권한 회수(1142), 표 삭제, 스키마, 인덱스(F53-R, F32-P, F36-R, F33-R), MySQL 다운, IO, 잠금 대기(F25-H, F10-H, D), 앱 배포.
- contrast_with: F38-R(같은 겉 증상, 인스턴스 read_only), F54-R(같은 원본, Oracle 호출당 읽기 한도), F35-R(같은 계정 정답 꼴, 로그인 거절), F53-R(주문 저장 실패, 표 하나 삭제).
- 부하 가설 배제: 동반 부하가 없고, 실패는 시간 초과가 아니라 밀리초 안에 나는 SQL 오류이며 그 문장이 계정의 시간당 자원을 말한다. 30초마다 도는 배치도 같은 문장으로 실패한다.

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

- 강도: 하나로 고정(`approved-fixed-f54-p`, MAX_UPDATES_PER_HOUR 500). 사다리 없음.
- 피해 계산: 한도를 지닌 연결의 비중은 변경 뒤 0 에서 약 10분에 걸쳐 1 로 오른다(옛 연결이 max-lifetime 10분 안에 모두 교체, 고르게 퍼져 있다고 보면 선형).
  한도 연결이 센 쓰기 누계 ≈ R × t² / (2 × 600초). R(계정 쓰기 문장/초)은 평시 약 2.2(DPM 1백분위 insert+update 분당 약 132)~16(10:15 UTC 표본 분당 983).
  카운터 500 을 넘는 시각 t = √(1200 × 500 / R) ≈ 약 3.2분(R=16)~8.7분(R=2.2)이다. 그 뒤 한도 연결은 모든 쓰기가 거절되고, 약 10분째에는 남은 옛 연결이 없어 모든 주문 저장이 1226 으로 500 이다(결정적, 시간대와 무관).
  한 시간 창은 첫 한도 연결이 생길 때 시작돼 실행(최대 25분) 안에는 초기화되지 않는다.
- 판정: success = 기준선 주문 생성 5xx 비율 ≥ 0.5 이고 2xx 비율 < 0.2 가 3틱. min_hold 15m(최악 약 10분째 전면 실패, 그 뒤 5분 넘게 유지), level timeout 22m, max_injection_duration 25m.
  가장 한가한 시간(주문 초당 약 0.13~0.2)에는 30초 창 주문 수가 몇 건뿐이지만 전면 실패 구간에서는 모두 5xx 라 비율 판정이 흔들리지 않는다(F60-H 와 같은 기준선 판정).
- 부하: 동반 부하 없음. 상주 기준선(loadgen-food, 초당 1~6 반복, 주문 20%)만 쓴다. 이유: 주문 동반 부하(order-surge.js 2rps, 주문 초당 약 1.4건 추가)를 붙이면 한도가 다 쓰이기 전 몇 분 동안의 주문이 배달원 풀(2026-10-11 10:30 UTC 배정 1,587/2,000, 만료 15~34분)을 채워 'No courier available' 503 이 섞인다.
  F38-R 은 한순간에 쓰기가 끊겨 이 문제가 없었다.
- 배제: food MySQL 파드 NotReady, order 파드 NotReady(2틱). abort: 입구 상태 0(2틱). order 의 health 는 연결 확인(ping)이라 Ready 로 남는다(F38-R 녹화에서 확인된 같은 꼴).
- cleanup: `ALTER USER 'fooddelivery'@'%' WITH MAX_UPDATES_PER_HOUR 0` 뒤 `0|0|0|0` 확인(멱등). recovery: 사전 상태와 같고 MySQL, order Ready, 기준선 주문 2xx ≥ 0.7(2틱, 10분). 한도를 지닌 연결도 즉시 풀린다.
- 안전: 바꾸는 것은 한 계정의 한 한도 칸뿐이다. 비밀번호, 계정 상태, 권한, 전역 변수, lucida_mon(DPM), root 는 건드리지 않고 연결도 끊지 않는다. 한도는 mysql.user 에 영속하므로 MySQL 이 실행 중 재시작해도 남는다(그때는 DB 다운 배제 조건이 먼저 발화하고 cleanup 이 지운다).
- 실행기 변경: F35-R, F54-R 계약과 Oracle 스크립트는 그대로 두고, 계약의 engine 이 mysql 이면 MySQL 한도 스크립트를 쓰는 모드를 더했다. 스크립트는 MAX_UPDATES_PER_HOUR 밖의 자원을 거부한다(exit 2).

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Google 2020-12-14, 링크, 원문 인용)와 요소별 대응표가 있고 기전이 같다(§2) (원칙 1)
- [x] 분류 장부를 갱신했다(§4-1 행, §6 기록). 묶음 G 11→12(13.3%), 정답 위치 DB 계정 2→3(3.3%), 결제 경로 12(13.5%, 무관), 음식배달 28→29. 어느 축도 20% 에 닿지 않는다(§3) (원칙 2)
- [x] 근본 원인 위치가 인프라 지점(testbed-mysql-0 의 'fooddelivery'@'%' 한도 칸, performance_schema 쓰기 양)과 코드 위치로 확인됐다(§4) (G1)
- [x] 근본 원인의 흔적이 119 실데이터에서 조회됐다: 같은 로거 경로(F38-R 실행의 1290 기록), 1226 문구 평시 0건, DPM 쓰기 지표(§7) (원칙 3)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: 로그와 KCM 은 전수, DPM 은 1분 수집(§7) (원칙 3)
- [x] 계기의 흔적이 조회됐다: 변경이 만드는 오류 문구에 계정 이름과 자원 이름이 들어 있고 평시 0건. 인공 지연 없음(§7) (원칙 4)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다(§5) (원칙 5)
- [x] 정답지 세 칸이 원칙 6대로 채워졌다(§5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다(§6) (원칙 7)
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: 바뀌는 것은 한도 숫자 하나이고 실행기 인자와 스크립트에 id 가 없다(실행기 테스트가 확인) (원칙 8)
- [x] 피해가 실제로 날 계산이 있다: 한도와 계정 쓰기 양, 연결 교체 시간, 판정 시각, 동반 부하를 빼는 이유(§9) (원칙 9)

## 11. 후보 목록 (반복 48, 2026-10-11)

기존 목록이 아니라 "실제 기전 × 부품"에서 출발했다. 0 묶음은 N(재시도 증폭), 0 부품은 세 도메인 kafka, commerce notification, nginx 다.
막는 사유는 원칙 1~9, 20% 상한, 같은 주입, 필수 중단 조건뿐으로 했다.

| 순위 | 후보 | 원본 사례 | 부품, 층 | 판정 |
|---|---|---|---|---|
| 1 | **food MySQL 앱 계정 fooddelivery 에 시간당 쓰기 한도(MAX_UPDATES_PER_HOUR 500)** | Google 2020-12-14 공식 | food MySQL 계정, DB(G+A) | **채택**(음식배달 최소, DB 계정 2, F38-R 과 같은 증상 다른 정답) |
| 2 | food MySQL 앱 계정 시간당 연결 수 한도(MAX_CONNECTIONS_PER_HOUR) | Google 2020-12-14 | food MySQL 계정 | 버림: 컨트롤러 필수 중단 조건. 새 연결만 막아 풀이 마르면 order readiness 가 빠져 입구 0(rejected 11행 계정 잠금과 같은 벽) |
| 3 | food MySQL 앱 계정 시간당 질의 수 한도(MAX_QUERIES_PER_HOUR) | Google 2020-12-14 | food MySQL 계정 | 버림: 필수 중단 조건. 드라이버 접속 초기화와 health 질의까지 세어 풀이 마름(rejected 24행 관찰) |
| 4 | banking Oracle 앱 계정 프로필 CONNECT_TIME 한도(세션이 몇 분 뒤 ORA-02399 로 끊김) | Google 2020-12-14 | banking Oracle 계정 | 버림: 카탈로그 §1. F54-R 과 같은 계정, 같은 프로필 수단(db.account Oracle 프로필 모드)의 자원 이름만 바꾼 꼴 |
| 5 | commerce PostgreSQL 앱 계정 연결 수 한도(ALTER ROLE commerce CONNECTION LIMIT)를 풀 합 아래로 | Google 2020-12-14 | commerce PG 계정 | 버림: 원칙 2 서비스 균형. commerce 가 최다(31)이고 정답 위치 DB 계정이 0 이 아님(2). 아홉 서비스가 한 계정이라 굶는 풀도 정해지지 않음(rejected 115행) |
| 6 | food 워커 tb-w3 호스트 방화벽 정책이 큰 규칙 집합으로 불어나 노드 불안정 | Square 2023-09-07 공식(https://developer.squareup.com/blog/incident-summary-2023-09-07/ , 2026-10-11 WebFetch) | tb-w3 노드, 인프라(K) | 버림: 원칙 1. 원본은 "combined with a DNS service upgrade" 와 DNS 트래픽 패턴이 겹친 복합이고 규칙 수, CPU 효과를 밝히지 않음. host.firewall 은 F44-R 과 같은 노드 |
| 7 | food 워커 tb-w3 정적 경로 접두사 오타로 파드 대역 일부를 엉뚱한 다음 홉으로 | UC Berkeley EECS 2015-07-07, GitHub 2025-06-17(F54-R 시트 후보 2) | tb-w3 노드 경로, 인프라(K) | 버림: 원칙 9, 증상 중복 이상의 벽. food 파드 사이 통신은 같은 노드 브리지라 노드 밖으로 나가는 것은 tb-cp CoreDNS 조회뿐(rejected 38, 152행과 같은 벽) |
| 8 | food order 설정 배포가 Jackson WRAP_ROOT_VALUE 를 켜 restaurant 응답 해석이 비어 주문 거절 | (없음) | food order 설정, 앱(G+L) | 버림: 원칙 1(공식 사후 보고 없음), 주문이 400 으로 끝나 4xx 단일 신호(F34-R 교훈) |
| 9 | banking transfer 롤아웃이 잘못된 JVM 옵션(JAVA_TOOL_OPTIONS)으로 기동 실패(maxSurge 0) | (없음) | banking transfer 배포, 인프라(G) | 버림: 원칙 1(공식 사후 보고 없음), 꼴이 F65-R, F17-H(transfer 롤아웃이 새 파드를 못 띄움)와 같음 |
| 10 | N: food order → payment 재시도 설정 확대가 외부 결제 지연을 증폭 | GitHub 2026-05-05(이번 실행에서 막힘) | food order 설정 | 버림: 이번 실행의 막힌 원본, 재시도는 하류 실패라는 두 번째 계기가 필요(복합) |

서로 다른 부품 7개(food MySQL 계정, banking Oracle 계정, commerce PG 계정, tb-w3 방화벽, tb-w3 경로, food order 설정, banking transfer 배포), DB, 인프라, 앱 세 층.
