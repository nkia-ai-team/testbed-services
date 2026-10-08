---
title: F38-R 설계 시트 (운영자 세션이 food MySQL 인스턴스 전체를 읽기 전용으로 바꿔 모든 쓰기가 거절되고 주문이 500 으로 실패)
status: Draft
owner: project
last_reviewed: 2026-10-08
tags:
  - scenario
  - design
  - config
  - mysql
summary: 운영자의 root SQL 세션이 SET GLOBAL read_only=ON 을 실행해 food MySQL 인스턴스 전체가 읽기 전용이 되고, 앱 계정의 모든 쓰기가 1290 '--read-only option' 으로 거절되어 주문이 전량 500 으로 실패하는 시나리오. 읽기, ping, readiness 는 정상. 원본은 Vapi 2025-01-21 운영 DB 읽기 전용 전환 장애.
---

# F38-R 설계 시트

## 1. 요약

food 의 네 서비스(order, restaurant, dispatch, payment)는 MySQL 8.0 인스턴스 하나(testbed-mysql-0)를 일반 앱 계정 하나(fooddelivery)로 함께 쓴다. 이 계정에는 `fooddelivery.*` 에 대한 ALL PRIVILEGES 만 있고 SUPER 나 CONNECTION_ADMIN 이 없다. 운영자의 root SQL 클라이언트 세션이 `SET GLOBAL read_only = ON` 을 실행한다(재구성 서사: 왜 실행했는지는 원본에도 없고 시나리오도 정하지 않는다). 그 순간부터 MySQL 은 앱 계정의 모든 INSERT, UPDATE, DELETE 를 `ERROR 1290 (HY000): The MySQL server is running with the --read-only option so it cannot execute this statement` 로 거절한다. SELECT, 연결 확인(ping), `/actuator/health` 는 그대로 성공한다.

order-service 의 createOrder 는 트랜잭션 하나 안에서 가게, 메뉴, 배차 용량을 HTTP 로 읽고(모두 200) 주문 행을 INSERT 하다가 1290 을 받는다. 예외가 매핑되지 않아 POST /api/orders 가 밀리초 안에 모두 500 으로 끝나고 트랜잭션은 롤백된다. dispatch-service 의 30초 만료 배치도 매 주기 같은 문장으로 실패한다. 롤아웃, 재시작, Ready 이탈은 없고 MySQL 은 빠르고 건강하다.

비유: 은행 지점장이 금고 서랍 하나를 "열람만" 으로 잠그려다가 지점 전체 전산을 "조회 전용" 으로 돌려 놓았다. 창구 직원은 잔액을 보여 줄 수는 있지만 입금, 출금, 개설은 한 건도 처리하지 못하고, 모든 직원이 같은 안내 문구("시스템이 조회 전용 모드입니다")를 받는다.

## 2. 원본 사례

- 기업: Vapi (음성 AI API)
- 날짜: 2025-01-21 05:03~05:33 (타임라인에 시간대 표기 없음. 상태 페이지의 사고 등록 1:20pm UTC, 해결 1:23pm UTC)
- 링크: [공식 상태 페이지 사고 기록](https://status.vapi.ai/incident/499408) (사고 목록 [2024-12~2025-02](https://status.vapi.ai/incidents/2024-12/2025-02))
- 요약(출처가 말한 것만): 사고 제목 "Updates to DB are failing". 05:03:04 운영 데이터베이스에 커넥션 풀러를 거쳐 붙은 SQL 클라이언트가 의도치 않게 데이터베이스를 읽기 전용으로 만들었다. 근본 원인은 읽기 전용 모드로 설정된 직접 SQL 클라이언트 연결이 커넥션 풀러를 통해 그 설정을 모든 세션에 퍼뜨려 갱신, 삽입, 삭제가 막힌 것이다("A configuration error caused the production database to switch to read-only mode"). 05:05 쓰기 실패 시작, 05:18 오류가 쌓여 API 중단, 약 05:23 데이터베이스 재시작 시작, 05:25 재시작, 05:33 완전 복구. 쓰기 30분 차단, API 15분 중단. 재발 방지는 원인으로 의심되는 복제 작업 중지, DDL 과 역할 작업의 상세 감사 로그, 운영 데이터베이스 직접 접속 제거다. 데이터베이스 종류, 풀러 제품, 읽기 전용이 설정된 정확한 방식은 적혀 있지 않다.
- 현실 비중: 설정 배포가 트리거의 31%(Google SRE Workbook), 설정이 장애의 19%(Ghosh 외 SoCC 2022)(`ref-real-world-incidents.md` §1, M1).

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 운영 DB 에 붙은 SQL 클라이언트 세션의 읽기 전용 설정 | 운영자의 root SQL 세션이 `SET GLOBAL read_only = ON` 실행(의도는 원본에도 재구성에도 두지 않는다) |
| 원인이 된 결함 | 한 세션의 읽기 전용 설정이 풀러를 거쳐 모든 세션에 퍼져 DB 전체가 읽기 전용 상태 | 같은 결과: 인스턴스 전역 변수 read_only=1, 앱 계정(SUPER 없음)의 모든 세션이 쓰기 불가. 퍼진 경로가 풀러가 아니라 전역 변수다 |
| 전파 경로 | 갱신, 삽입, 삭제 실패 → 오류 누적 → API 중단 | 주문 INSERT 1290 → order 500, dispatch 만료 배치 UPDATE 실패 → 모든 주문 실패 |
| 사용자 증상 | 쓰기 30분 차단, API 15분 중단 | 주문 전량 500, 조회(가게, 메뉴, 주문 내역, 배달 조회)는 정상 |
| 탐지된 경로 | 쓰기 실패와 API 오류(시각만 기재) | order POST /api/orders 500(ERROR 서버 스팬), order 와 dispatch 의 새 ERROR 템플릿 'The MySQL server is running with the --read-only option ...', DPM 쓰기 지표 급감과 롤백 급증(119 이상 탐지 → 인시던트) |
| 완화와 복구 | 데이터베이스 재시작으로 복구 | cleanup 이 `SET GLOBAL read_only = OFF`. 영속하지 않는 설정이라 MySQL 재시작으로도 풀린다(원본의 재시작과 같은 효과) |

기전은 원본과 같다: "DB 가 읽기 전용 상태로 바뀜 → 읽기는 되고 모든 쓰기가 거절됨 → 쓰기가 필요한 API 가 실패". 우리 스택에 맞춘 것은 대상(MySQL 8.0 인스턴스)과 읽기 전용이 퍼지는 경로(풀러를 거친 세션 설정 대신 인스턴스 전역 변수)다. 원본은 DB 종류와 정확한 설정 방식을 밝히지 않았고, MySQL 에서 한 세션의 명령으로 모든 앱 세션이 읽기 전용이 되는 표준 방식이 전역 read_only 다. 원본에서 API 가 쓰기 실패 13분 뒤에 중단된 것은 오류가 쌓인 결과이고, 우리는 주문 생성 자체가 쓰기라 즉시 500 이다.

## 3. 숫자 근거 (2026-10-08, `scenario-stats.py`, 정식 + 후보 합계 41, 이 후보 전)

| 축 | 값 | 판단 |
|---|---|---|
| 서비스 | 쇼핑몰 23, 은행 10, 음식배달 8 | 음식배달이 가장 적다 → 음식배달 |
| 묶음 0개 | J, K, N | 아래 이유로 이번에도 막힘 |
| 묶음 G(설정 오배포) | 3(7%) → 4(10%) | 현실 트리거 31%(Google) 대비 크게 적다 |
| 상한 근처 | A 7, B 7, D 7(각 17%), C 6(14%) | 피함 |
| 정답 위치 DB 인스턴스 | 1(F25-H) → 2 | §2-1 에 이미 있는 말 |
| 결제 경로 합계 | 10(24%) | 결제가 정답이 아니므로 변동 없음 |

0인 묶음을 고르지 못한 이유(2026-10-08 실측):

- J(결함 있는 새 버전 배포): tb-w3, tb-w2 의 이미지가 서비스마다 `latest` 하나뿐이다(crictl). 결함 이미지를 만들려면 앱 코드 변경이 필요하고 이는 사람 검토 대상이다.
- K(네트워크): CNI 가 flannel 단독이고 conflist 에 bandwidth 플러그인도 없다(tb-w3 `/etc/cni/net.d/10-flannel.conflist`: flannel, portmap). NetworkPolicy, 파드 대역폭 주석 모두 집행되지 않는다. 노드 iptables, conntrack 조작은 막힌 network.fault 와 같은 수단이고 kubelet 프로브까지 끊어 입구가 연결 불가(필수 중단 조건)가 된다.
- N(재시도가 장애를 키움): food, banking 의 모든 동기 호출에 서킷브레이커가 있어 재시도 증폭이 유지되지 않는다(F14-R 막힘과 같은 이유).

같은 반복에서 먼저 검토하고 버린 것(로컬 기록): banking Oracle 인덱스 제거(F33-R 의 은행판). 원장 멱등 확인 질의(`89y14ktt0px76`, idx_ledger_ref)는 Kafka 소비자 경로라 사용자 5xx 로 이어질 계산이 서지 않고, 이체 내역 질의(`99nsgbv90qrn7`)와 건수 질의(`byb2y2hv7p4ag`)는 OR-null 꼴이라 지금도 TABLE ACCESS FULL(실행 계획 실측)이어서 인덱스를 지워도 달라지는 것이 없다.

왜 이 후보인가: 음식배달이 가장 적고, G 는 현실에서 가장 흔한 트리거인데 정식 + 후보 4개뿐이며, 정답 위치(DB 인스턴스)가 1개뿐이다. order 는 자기 DB 연결과 health 가 멀쩡해 입구가 끊기지 않으므로(500 으로 답함) 음식배달의 구조적 제약(게이트웨이 없음, readiness 가 DB 를 봄)에 걸리지 않는다. 같은 "읽기 전용" 발상의 F14-P(정식, H)는 Oracle 테이블 하나를 읽기 전용으로 바꿔 원장 쓰기가 조용히 사라지는 장애이고, F38-R 은 인스턴스 전체가 읽기 전용이라 여러 서비스의 쓰기가 같은 오류로 드러난다. 주입 수단, 대상, 증상이 모두 다르다.

## 4. 인과 사슬 (코드와 인프라 위치)

```
db.instance_readonly: testbed-mysql-0 안 MySQL 클라이언트(root)
  SET SESSION lock_wait_timeout=10; SET GLOBAL read_only=ON
  → @@global.read_only=1, super_read_only=0. 영속하지 않음(persisted_variables 비어 있음)
  → 앱 계정 fooddelivery(SUPER, CONNECTION_ADMIN 없음)의 쓰기 문장만 거절, root 와 읽기는 그대로
  → order POST /api/orders: OrderService.createOrder(@Transactional)
       restaurantClient.getRestaurant, getMenu, dispatchClient.checkCapacity → 200(읽기)
       orderRepository.save(order) → IDENTITY 라 INSERT INTO orders 즉시 실행 → MySQL 1290
       → Hibernate WARN 'SQL Error: 1290, SQLState: HY000', ERROR 'The MySQL server is running with the --read-only option so it cannot execute this statement'
       → GlobalExceptionHandler 가 ServiceException 만 매핑 → 처리되지 않은 예외로 500, 트랜잭션 ROLLBACK
       → 배차(dispatchCourier)와 결제(processPayment)를 부르기 전에 끝남('Created order' 없음)
  → dispatch deliverExpiredDispatches(30초마다): ETA(15~34분)가 지난 ASSIGNED 를 DELIVERED 로 UPDATE, dispatch_events INSERT
       → 1290 → 스케줄러 ERROR 'Unexpected error occurred in scheduled task' + 같은 SQL 오류 로그, 트랜잭션 롤백, 다음 주기 반복
  → outbox 정리(30초마다): 지울 행이 있으면 WARN 'Outbox purge failed, will retry next cycle: ...' (삼킴)
  ↔ 가게, 메뉴, 인기 메뉴, 배차 용량, 주문 조회, 배달 조회 → 200 그대로
  ↔ /actuator/health: DataSource 확인(연결 유효성 확인) → UP, 모든 파드 Ready 유지
```

앵커(정답지 `code_anchor` 와 같음):

- `food-delivery/order-service/src/main/java/com/fooddelivery/order/service/OrderService.java:57-128` (createOrder, 읽기 셋 뒤 save 에서 첫 쓰기)
- `food-delivery/order-service/src/main/java/com/fooddelivery/order/entity/Order.java:12-13` (`@GeneratedValue(strategy = GenerationType.IDENTITY)`)
- `food-delivery/order-service/src/main/java/com/fooddelivery/order/config/GlobalExceptionHandler.java:9-21`
- `food-delivery/dispatch-service/src/main/java/com/fooddelivery/dispatch/service/DispatchService.java:116-130` (deliverExpiredDispatches)
- `food-delivery/order-service/src/main/resources/application.yml:7-17` (datasource, 계정 fooddelivery)
- `food-delivery/k8s/10-mysql.yaml:1-23` (StatefulSet testbed-mysql, image mysql:8.0)
- 인프라: rca-testbed-food `testbed-mysql-0`(MySQL 8.0.46, tb-w3). 계정 fooddelivery: `GRANT USAGE ON *.*`, `GRANT ALL PRIVILEGES ON fooddelivery.*`, mysql.user super_priv N, mysql.global_grants 0건. lucida_mon(DPM 관측): PROCESS, REPLICATION CLIENT, SELECT 뿐. `performance_schema.persisted_variables` 0행, events 0개(2026-10-08 18:0x UTC 조회)

로컬 실측(mysql:8.0.46 컨테이너, food `init.sql` 적용, 일반 계정): read_only=ON 뒤 INSERT 와 UPDATE 가 `ERROR 1290 (HY000)`, SELECT 와 `mysqladmin ping` 은 정상, `SET SESSION TRANSACTION READ WRITE` 와 `SET autocommit=0` 은 허용. 거절된 INSERT, UPDATE 는 Com_insert, Com_update 를 올리지 않고 이어진 ROLLBACK 은 Com_rollback 을 올린다. 다른 세션이 쓰기 트랜잭션을 열어 둔 상태에서도 `SET GLOBAL read_only=ON` 이 0.11초에 끝났다. 실행기 스크립트 전체(preflight, run, 두 번째 run 거부, cleanup 두 번, recovery, 앱 계정에 CONNECTION_ADMIN 을 준 상태의 preflight 거부)를 가짜 kubectl 로 같은 컨테이너에 돌려 확인했다.

## 5. 원인 규정 (원칙 5, 6)

| 칸 | 값 | 근거 |
|---|---|---|
| `root_cause.target_id` | `food-mysql` | 결함은 DB 인스턴스의 설정 상태(전역 read_only)다. 원본 기록도 "configuration error 로 운영 DB 가 읽기 전용으로 바뀜"을 원인으로 적고 복구도 DB 쪽(재시작)이다. 표기는 F25-H 의 `commerce-postgres` 와 같은 'DB 인스턴스' 꼴이다 |
| `trigger_target_id` | null | 근본과 계기가 같은 곳(인스턴스 전역 변수) |
| `scoring.partial` | `food-delivery-order`, `food-order`, `food-delivery-dispatch`, `food-dispatch` | 오류가 찍히는 서비스는 원인 근처다 |

원칙 5: 정답은 로그의 오류 문장("--read-only option"), 여러 서비스의 서로 다른 테이블 쓰기가 같은 문장으로 실패한다는 사실, DPM 의 쓰기 지표만 끊긴 모양, 그 시각에 배포나 재시작이 없다는 사실로 낼 수 있는 결론이다. 코드 설계 결함 추론이 필요 없다. "order 가 DB 쓰기에 실패한다"고 답하면 order(부분)로, "food MySQL 이 읽기 전용 상태가 되어 모든 쓰기를 거절한다"고 답하면 정답이다. 누가 왜 명령을 실행했는지는 요구하지 않는다.

원칙 6: order 와 dispatch 는 늘 하던 정당한 쓰기를 보냈고, 결함 있는 곳(읽기 전용으로 바뀐 인스턴스)에서 거절됐다. 인스턴스를 읽기 전용으로 바꾼 명령이 원인이다.

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 | 기대 |
|---|---|---|
| 감지 | order 서버 스팬 오류(POST /api/orders 500, 골든 시그널), 새 로그 템플릿은 ERROR 'The MySQL server is running with the --read-only option ...'(남은 로그 약 7일 0건)과 그것을 담은 Tomcat SEVERE 본문뿐(Hibernate WARN 'SQL Error: N, SQLState: X' 와 스케줄러 ERROR 'Unexpected error occurred in scheduled task' 는 기존 템플릿일 가능성이 크다), order 로그 surge, DPM rollback_count 급증과 insert_count 급감 | 이벤트는 고장 시작 직후에 나고 promote 는 대개 열린 묶음의 첫 판정(고장 2~4분째)에서 난다. 1차 근거는 고장 중 order 오류율이다 |
| 피해 판정 | 러너: 동반 부하의 주문 생성 5xx 비율 ≥ 0.5, 2xx 비율 < 0.2(3틱) | 평시 5xx 0, 2xx 0.92~1.0 |
| 원인 설명 | 로그 전수(1290 문장, 여러 서비스), DPM 쓰기 지표, KCM 무변화, 읽기 정상 | §7 |

인시던트가 실제로 생길지에 대한 근거(원칙 7, 2026-10-08 18:00~18:20 UTC 조회):

- **order 자신이 즉시 5xx 서버 스팬을 낸다.** F36-R 처럼 하류 실패가 재시도, 서킷브레이커를 거쳐 502 로 번지는 것이 아니라, 입구 서비스 order 의 POST /api/orders 가 첫 요청부터 500(ERROR 서버 스팬)이다. 판정 입력(CH MV `agg_service_golden_signals`, 표본 SERVER/CONSUMER 스팬 status_code='ERROR', 판정 시각 기준 최근 15분 대 그 전 3시간, 묶음 멤버 상위 3개 서비스)에 order 가 바로 들어간다. order 평시 7일 표본 53,351건 중 오류 58건(0.11%), KST 시간대별 분당 2.0~8.3건이다.
- **첫 판정 창의 희석 계산(가장 한가한 시간대).** 동반 부하 2rps 가 주문 생성을 초당 약 1.4건(계산: 2rps × 주문 여정 75%, order-surge.js 를 쓰는 F33-R, F36-R 은 아직 실행된 적이 없어 실측 아님) 더하고 기준선이 약 0.2건이라, 고장 중 order 표본 오류 스팬은 분당 약 10건(계산값)이다. 참고 실측: F30-R 18:00 실행(surge.js 2rps, KST 03시)에서 order 'Created order' 는 평시 분당 10~16 → 15~27, POST /api/orders 표본 스팬은 5분당 9~12건이었다. 동반 부하가 계산보다 적게 들어와도 기준선만으로 첫 판정 창 오류율이 약 12% 로 평시 0.11% 를 크게 넘는다. promote 는 대개 고장 2~4분째의 첫 판정에서 나므로 15분 창에 고장이 2~4분만 들어가 창 전체 오류율은 대략 12~24%다. 평시 0.11% 대비 두 자릿수 배다.
- **판정 시점.** 실제 promote 시각은 PG `incidents.promoted_at` 이다(`last_reviewed_at` 은 대개 나중 재검토 시각). 이전 반복의 평가 실측(48시간 promote 89건 중 80건이 마지막 이벤트 뒤 15분 안, 묶음은 마지막 이벤트 뒤 약 20분에 닫힘, 열린 묶음은 drop 뒤 candidate_published_version 이 오를 때만 재판정(store/incidents_judge.go:291))대로, F38-R 의 판정 기회도 대개 고장 2~4분째의 첫 판정 한 번이다. min_hold 15분은 그 판정이 고장 중에 나도록 하고 러너 판정과 녹화에 충분한 실패 구간을 주는 길이다.
- **선례(promoted_at 기준, 동반 부하 여부 포함).** 2026-10-08 의 food 인시던트: 90900391(main food-delivery-order, 첫 이벤트 14:53:19, promote 14:55:15, 약 2분)과 6cfd1613(묶음 d1d43f83, 첫 이벤트 14:54:17, promote 14:56:43)은 F30-R 실행(14:52~15:00, surge.js 5rps 동반 부하) 중이고, 5ae9bf6a(묶음 00f2b1c7, 09:12:53~09:21:14, promote 09:21:57)도 F30-R 실행(09:13~09:21) 중이다. 셋 다 동반 부하가 붙은 창이라 "동반 부하 없이도 난다"는 근거로 쓰지 않고, F38-R 처럼 동반 부하를 붙인 food 주문 실패가 고장 중 promote 된 선례로만 쓴다. DPM 쪽: a6cb1410(main food-delivery-dispatch, 첫 이벤트 08:36:12, promote 08:40:17)의 제목이 "MySQL-fooddelivery 의 ROLLBACK 급증이 food-delivery-dispatch 오류로 이어지고 있어요"로, DPM rollback 지표 이상이 묶음에 들어가 MySQL 이 인시던트 문장에 오른 선례다(그 창의 러너 기록은 남아 있지 않아 동반 부하 여부는 확인하지 못했다).
- **반대 위험.** 같은 날 18:00~18:08 F30-R 실행에서는 order log surge(warning, 18:01:23)와 DPM `dpm.mysql.sql.rollback_count` critical(18:01:50)이 났지만 묶음 4fd5d522, 42493bec, eca3bdf4 가 18:22~18:23 `hold / self_resolved_burst`(trigger gate)로 끝나 인시던트가 없었다. 그 창의 order 골든 시그널 error_count 는 0 이었다. F38-R 이 다른 점은 POST /api/orders 500(ERROR 서버 스팬)이 고장 내내 판정 입력(골든 시그널)에 들어간다는 것이다. 7일 order 이벤트 종류는 trace distribution_shift/error_chain, log surge/new_template/unknown_anomaly, metric_anomaly 이고 'order 오류율' 전용 탐지기는 없다. 그래서 cheap-gate 를 넘기는 것은 order log surge(warning)와 DPM rollback critical 이고, 판정에서 order 오류율이 근거가 된다. 첫 실행 검증에서 promote 가 없으면 묶음 멤버, 판정 시각, gate 사유(self_resolved_burst 여부), 판정 문장을 확인한다(4xx 업무 거절이 아니라 5xx 서버 스팬이다).
- **묶음.** order 와 MySQL(DPM) 이벤트, dispatch 로그 이벤트가 한 묶음이 될지는 토폴로지와 decideCoOnset(W 180초) 에 달렸고, 호출자, 피호출자 한 묶음은 실측 약 60%다. 쪼개져도 order 묶음은 order 오류율만으로 판정 입력이 선다.

## 7. 관측 근거 표 (119 실조회, 2026-10-08 18:00~18:20 UTC)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 결정 증거는 전수 로그와 DPM, KCM 이다.

| 증거 | 119 표와 칸 | 조회 | 평시 결과 |
|---|---|---|---|
| 근본: 읽기 전용 거절 | CH `lucida_logs_local` (`service_name`, `severity_text`, `body`) | `SELECT service_name, countIf(position(body,'read-only')>0 OR position(body,'read_only')>0), countIf(position(body,'SQL Error: 1290')>0) FROM lucida.lucida_logs_local WHERE timestamp > now() - INTERVAL 8 DAY AND service_name LIKE 'food-delivery-%' GROUP BY 1` 와 모든 서비스 대상 'read-only' 검색 | food 다섯 서비스, 다른 도메인 모두 0건(18:08 UTC 조회, 가장 이른 로그 2026-10-01 16:00, 약 7일) |
| 근본 경로의 평시 기록(같은 로거가 CH 에 남는가) | 같은 표 | food 'SQL Error:' 8일 집계, 2026-10-08 07:37:50~07:38:30 경고 이상 로그 | 'SQL Error:' dispatch 771, restaurant 145, payment 8, order 7건. 07:38 MySQL 재시작 때 WARN 'SQL Error: 0, SQLState: 08S01' 와 ERROR 'Communications link failure ...' 가 짝으로(restaurant 85, 85건), SEVERE 'Servlet.service() ... threw exception' 이 함께 남았다. Hibernate SqlExceptionHelper 의 WARN(코드, 상태)과 ERROR(드라이버 문장)가 전수로 남는다. 8일 'Unexpected error occurred in scheduled task' 는 order 5, dispatch 79건, 'SQL Error: N, SQLState: X' WARN 은 order 7, dispatch 771건이라 이 둘은 기존 템플릿일 가능성이 크고, 새 템플릿은 ERROR 'The MySQL server is running with the --read-only option ...'(남은 로그 약 7일 0건)과 그것을 담은 Tomcat SEVERE 본문뿐이다 |
| 근본: DB 쪽 쓰기 정지 | VM `dpm.mysql.sql.insert_count`, `update_count`, `select_count`, `rollback_count` (target_id c8c558e5-…, 분 단위) | 8일 query_range step 60s, 11,168점 | insert 중앙 456, 하위 1% 84 / update 중앙 266, 하위 1% 50 / select 중앙 1,260 / rollback 중앙 1, 95분위 4.9, 99분위 6.9, 최대 392. select ≥ 200 인데 insert < 5 인 점 17개(대개 1분 단발, 가장 긴 연속 3분, 10-01 14:36~14:38 은 MySQL 재시작 전후). 15분 넘게 이어진 적 없음 |
| 근본: 지표의 뜻 | MySQL `SHOW GLOBAL STATUS`(109 읽기 전용) | 18:05:50 과 18:06:51 UTC 두 번 | Com_select +880/분, Com_insert +197/분, Com_commit +466/분, Com_rollback +63/분. 같은 시각 DPM select_count 769~853, insert_count 120~145, commit_count 390~444, rollback_count 36~48 로 같은 크기 → DPM 지표는 Com_* 문장 수다. 로컬 실측대로 거절된 쓰기는 Com_insert, Com_update 에 세지 않는다 |
| 계기: SET GLOBAL 자체 | DPM Top SQL(`dpm_topsql_local`, `body.sqlText`) | food MySQL 의 `SET %`, `ALTER %`, `CREATE %`, `DROP %` 10일 검색 | `SET autocommit`, `SET SESSION TRANSACTION READ WRITE/READ ONLY`(Spring 트랜잭션, 각 약 14,000건), `SET character_set_results` 뿐이고 SET GLOBAL 0건. Top SQL 은 자주 도는 다이제스트만 남긴다. 곧 명령 자체는 119 에 남지 않고 계기는 오류 문장과 시작 시각, 쓰기만 끊긴 DB 쪽 모양으로 간접 확인된다. 세션 단위 READ ONLY 트랜잭션(평시)은 1290 이 아니라 1792 를 내므로 혼동되지 않는다 |
| 다른 변경 없음 | CH `kcm_events_local` | `namespace='rca-testbed-food' AND (object_name LIKE 'testbed-order%' OR object_name LIKE 'testbed-mysql%')`, 60일 | testbed-order 마지막 ScalingReplicaSet 2026-10-01 04:05. testbed-mysql-0 Started 56회(마지막 2026-10-08 07:38, OOM 재시작 약 2일 주기). MySQL 재시작 때는 order Unhealthy 20건(07:38)이 함께 난다. F38-R 은 재시작도 Unhealthy 도 없어야 한다 |
| 피해: order 엔드포인트 | CH `otel_traces_local` (`span_kind='SERVER'`, `span_name`, `status_code`) | 최근 1시간 order 서버 스팬 이름별 | POST /api/orders 102건(오류 0), GET /actuator/health 60건. 고장 중에는 POST 만 ERROR |
| 피해: 골든 시그널 평시 | CH `agg_service_golden_signals` (`req_count`, `error_count`) | 7일 합계, KST 시간대별 | order 53,351건 중 오류 58, 분당 2.0(KST 03시)~8.3(18시). restaurant 436,373 중 12, dispatch 114,269 중 64 |
| 배제: DB 정상 | VM `dpm.mysql.session.active_session`, `dpm.mysql.instance.avg_query_response_time`, `dpm.mysql.session.blocked_session` | `quantile_over_time(0.5, ...[8d])` | 중앙 1, 7.28ms, 0 |
| 배제: 풀 대기 없음 | VM `db.client.connections.pending_requests{service_name="food-delivery-order"}` | `quantile_over_time(0.95, ...[8d])` | 0 |

## 8. 감별

- must_support: order, dispatch 의 1290 '--read-only option' 로그(평시 0), POST /api/orders 500 과 읽기 200 의 갈림, DPM insert/update 0 근처와 select 유지, rollback 급증, 롤아웃과 재시작 없음, MySQL 과 order Ready, 세션과 응답 시간 평시.
- must_rule_out(정답지에 문장으로): MySQL 다운이나 재시작(F25-H 꼴, OOM 재시작. 그러면 08S01 연결 실패와 order Unhealthy 가 나고 읽기도 끊김), 디스크나 IO(F10-H), 테이블 하나의 문제(F14-P 읽기 전용 테이블, F36-R 스키마, F33-R 인덱스), DB 계정 문제(F35-R 잠금, 권한 회수면 1142), 잠금 대기(D), order 새 버전이나 설정 배포, 부하 증가나 용량 부족(아래).
- 동반 부하 경쟁 가설 배제: 동반 부하는 주문 유입을 시간대에 따라 약 2~8배로 늘린다. 그러나 실패는 시간 초과가 아니라 밀리초 안에 나는 SQL 오류이고, 그 문장이 서버의 읽기 전용 상태를 가리킨다. 부하와 무관하게 30초마다 도는 dispatch 만료 배치도 같은 문장으로 실패한다. 같은 부하에서 같은 DB 의 읽기는 200 이고 세션, 응답 시간, Hikari 대기는 평시다. DB 가 받아들이는 쓰기 수(insert_count)는 시도가 늘어도 오르지 않고 0 근처로 떨어진다. 부하만으로는 이 오류도 이 모양도 생기지 않는다. 정답지에 "부하 그대로"라고 쓰지 않았다.
- contrast_with: F14-P(테이블 하나 읽기 전용, 조용한 원장 유실), F36-R(같은 food MySQL, 스키마 변경으로 읽기 1054), F25-H(DB 프로세스 죽음, 읽기와 쓰기 함께 끊김), F35-R(DB 쪽 한 칸으로 새 로그인 거절).

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

진행 대본: 설계 강도 하나로 고정한 평가 모드(`approved-fixed-f38-r`). 강도라는 축이 없다: read_only 가 켜지면 쓰기는 요청량과 무관하게 100% 거절된다.

| 항목 | 값 | 근거 |
|---|---|---|
| primary | db.instance_readonly, 계약 `{engine mysql, rca-testbed-food, testbed-mysql-0, app_account fooddelivery}` | 실행기와 profiles.json 양쪽 같은 값(G2) |
| companion | load.north_south, order-surge.js, target_rps 2, ramp 2m, hold 21m, food 진입점 30181, 기준선 loadgen-food | F33-R, F36-R 과 같은 스크립트와 rps |
| min_hold / settle / timeout | 15m / 30s / 20m, max_injection 25m | 성공 판정은 min_hold 뒤에 시작하므로 실패가 최소 15분 이어진다. promote 는 대개 고장 2~4분째 첫 판정에서 난다(§6) |
| success(3틱) | 동반 부하 주문 생성 5xx ≥ 0.5, 2xx < 0.2 | 모든 주문이 저장 단계에서 500 이라 5xx 비율이 1 근처 |
| must_rule_out(2틱) | achieved_rps < 0.5, MySQL 파드 NotReady, order 파드 NotReady | MySQL 이 빠지면 DB 다운이고 재시작은 read_only 도 지운다. order 가 빠지면 연결 실패다. read_only 는 health 의 연결 확인을 건드리지 않아 둘 다 Ready 로 남아야 한다 |
| abort | entry_status == 0 (필수) | entry_status 는 동반 부하의 주문 생성 응답 코드다. order 는 DB 연결과 health 가 멀쩡해 500 으로 답하므로 0 이 나오지 않는다 |
| recovery | target_health 200, MySQL Ready, order Ready, 기준선 주문 2xx ≥ 0.7 (2틱, 10m) | target_health 는 러너 고정값 commerce :30080/health 라 food 회복은 order-ready 와 create-recovered 가 맡는다. read_only=OFF 직후 쓰기가 성공한다 |

부하 계산(가장 한가한 시간대 기준, KST 02~06시 기준선 1 iter/s):

- 기준선 주문 생성 초당 약 0.2건 + 동반 부하 약 1.4건(계산: 2rps × 주문 여정 75%, 실측 아님) = 초당 약 1.6건, 분당 약 96건이 모두 500(표본 약 10건, 계산값). 15분이면 order 표본 오류 스팬 약 140건 이상. 바쁜 시간(KST 14~18시, 6 iter/s)에는 기준선 몫이 6배다.
- 주문 한 건은 가게, 메뉴, 용량 조회(읽기) 뒤 INSERT 에서 끝나 배차, 결제, Kafka 를 부르지 않는다. 실패가 빨라 VU 가 쌓이지 않는다. 동반 부하 2rps 는 load.north_south 계약(1~180) 안.
- 서킷브레이커: order 가 직접 실패하므로 order 의 하류 서킷브레이커(restaurant, dispatch, payment)는 열리지 않는다. 열려도 오류는 그대로 500 이다.
- DPM: 고장 중 rollback_count 는 분당 약 96 + 만료 배치 2(평시 중앙 1, 99분위 6.9), insert_count 는 0 근처(평시 하위 1% 84).

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Vapi 2025-01-21, 공식 상태 페이지)와 요소별 대응표가 있고 기전이 같다 (§2)
- [x] 분류 장부 §4-1, §6 갱신. 묶음 G 3→4(10%), 정답 위치 DB 인스턴스 1→2, 결제 경로 합계 10 그대로, 음식배달 8→9. 어느 축도 20% 미만 (§3)
- [x] 근본 원인 위치: `OrderService.java:57-128`, `Order.java:12-13`, `GlobalExceptionHandler.java:9-21`, `DispatchService.java:116-130`, 인프라 testbed-mysql-0 전역 read_only, 계정 권한 (§4)
- [x] 근본 원인 흔적: CH `lucida_logs_local` 1290 오류 문장(전수, 평시 0, 같은 로거 평시 기록 확인), VM DPM `dpm.mysql.sql.*_count` (§7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: 로그 전수, DPM(문장 수 지표), KCM. 스팬은 엔드포인트별 갈림의 보조 (§7)
- [x] 계기 흔적: SET GLOBAL 문장은 남지 않지만 오류 문장이 서버의 읽기 전용 상태를 가리키고, DB 쪽 쓰기만 끊긴 모양과 시작 시각이 남으며, 그 시각에 롤아웃, 재시작이 없다(KCM). 인공 지연 없음 (§7)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다 (§5)
- [x] 정답지 세 칸이 원칙 6대로 (§5)
- [x] 감지, 피해 판정, RCA 증거 구분과 인시던트 근거(promoted_at, 선례의 동반 부하 여부, 첫 판정 창 희석 계산(계산값), 18:00 F30-R 실행의 self_resolved_burst 반례) (§6)
- [x] 주입 도구가 시나리오 id 를 남기지 않는다: MySQL 클라이언트를 파드 안 root 로 열어 전역 변수 한 칸, 인자와 스크립트에 id 없음(테스트로 고정). 실행기의 상태 확인 SELECT 가 DPM 표에 남는지는 첫 녹화 누설 검사에서 본다(§11). k6 태그는 tb-runner 의 k6 출력에만 남는다
- [x] 부하 상한과 서킷브레이커를 고려한 피해 계산, 가장 한가한 시간대 기준 (§9)

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- order 로그에 'SQL Error: 1290, SQLState: HY000' 와 'The MySQL server is running with the --read-only option so it cannot execute this statement' 가 실제로 남는지, Tomcat SEVERE 'Servlet.service() ... threw exception' 본문에 실패한 SQL(`insert into orders`)이 실리는지. dispatch 만료 배치의 'Unexpected error occurred in scheduled task' 와 같은 1290 이 30초마다 남는지.
- 동반 부하의 실제 주문 생성률(k6 create step 수, order 'Created order' 평시 대비)을 기록한다. §6, §9 의 초당 약 1.4건은 계산값이다.
- 누설 검사: 실행기가 run 전후로 root 로 `SELECT CONCAT(@@global.read_only, @@global.super_read_only)` 와 `mysql.user`, `mysql.global_grants` 조회를 실행한다. 녹화본의 `dpm_topsql_local`(sqlText)과 `dpm_session_local` 에 이 문장들이 잡혔는지 확인한다(2026-10-08 기준 두 표 모두 0건). 잡혔으면 주입 도구의 인공 흔적이므로 녹화에서 빼는 방법을 검증 보고서에 적는다.
- order POST /api/orders 가 500 이고 order 의 하류(restaurant, dispatch, payment) 호출에 오류가 없는지. order, MySQL 파드가 Ready 로 남았는지.
- DPM insert_count, update_count 가 0 근처로 떨어지고 rollback_count 가 오르는지(실제 값과 이벤트 생성 여부).
- promote 가 났다면 `incidents.promoted_at`, 묶음 멤버(상위 3개 서비스), 그 시각의 판정 문장. 안 났다면 묶음 쪼개짐, 토폴로지 로드 성공 여부, 판정 시각이 묶음이 닫힌 뒤였는지. 회복 뒤 재판정 promote 도 있으니 실행 뒤 몇 시간은 지켜본다.
- cleanup 뒤 `@@global.read_only` 가 0 인지(실행기 recovery 가 확인한다). 아니면 다음 무인 실행 전에 수동으로 `SET GLOBAL read_only=OFF`.
- 녹화 창에 food MySQL OOM 재시작(약 2일 주기)이 끼면 read_only 도 함께 풀리므로 녹화로 쓰지 않는다(must_rule_out food-db-down 이 잡는다).
- 고장 중 시작된 order 트랜잭션은 롤백되어 주문 행이 남지 않는다. 회복 뒤 만료 배치가 밀린 배달을 한 번에 DELIVERED 로 옮기므로 회복 직후 dispatch 로그 'delivered=N' 이 평소보다 클 수 있다.
