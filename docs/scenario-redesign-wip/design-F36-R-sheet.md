---
title: F36-R 설계 시트 (food restaurants 열 이름 변경 마이그레이션이 코드보다 먼저 적용되어 가게 조회와 주문이 실패)
status: Draft
owner: project
last_reviewed: 2026-10-08
tags:
  - scenario
  - design
  - schema
  - mysql
summary: 스키마 마이그레이션이 food MySQL restaurants 테이블의 region 열 이름을 delivery_region 으로 바꿨는데, 다시 배포되지 않은 restaurant-service 가 옛 이름을 계속 골라 가게 조회와 검색이 MySQL 1054 Unknown column 으로 실패하고, 가게 조회가 첫 단계인 order 가 모든 주문에 502 로 답하는 시나리오. 원본은 Onfido 2024-10-18 스키마 마이그레이션과 ORM 모델 불일치 장애.
---

# F36-R 설계 시트

## 1. 요약

food 의 restaurant-service 는 restaurants 테이블(23행: id, name, region, status)을 JPA 엔티티 `Restaurant` 로 읽고, 필드 `region` 은 열 `region` 에 매핑돼 있다. 가게 조회(`findById`)와 가게 검색은 이 열을 고른다. 스키마 마이그레이션이 `ALTER TABLE restaurants RENAME COLUMN region TO delivery_region` 을 먼저 적용하고, 새 이름을 읽는 restaurant 코드는 아직 배포되지 않았다. 그 순간부터 MySQL 이 가게 조회와 검색 문장마다 `ERROR 1054 (42S22): Unknown column 'r1_0.region' in 'field list'` 로 거절한다. restaurant 는 500 으로 답하고, createOrder 의 첫 단계가 가게 조회인 order 는 3회 재시도 뒤 restaurant 서킷브레이커를 열어 모든 주문에 502 로 답한다. 같은 파드, 같은 DB 에서 region 을 고르지 않는 메뉴와 인기 메뉴는 200 그대로다. 롤아웃, 재시작, 설정 변경은 없고 MySQL 은 빠르고 건강하다.

비유: 창고 선반 이름표를 "지역"에서 "배송 지역"으로 바꿔 붙였는데, 창고 직원 지시서는 아직 "지역 선반에서 꺼내라"고 적혀 있다. 직원은 바쁘지도 아프지도 않은데 그 선반을 찾지 못해 매번 빈손으로 돌아오고, 다른 선반(메뉴) 일은 멀쩡히 한다.

## 2. 원본 사례

- 기업: Onfido (Studio)
- 날짜: 2024-10-18 08:18~08:33 (페이지에 시간대 표기 없음). 사후 보고 게시 2024-11-14
- 링크: [공식 사후 보고](https://status.onfido.com/incidents/fvl7r5k973p2)
- 요약(출처가 말한 것만): 릴리스 중 Studio 테이블 하나에 데이터베이스 스키마 마이그레이션이 배포됐는데 갱신된 코드가 아직 모든 인스턴스에 퍼지지 않았다. 마이그레이션이 데이터베이스와 애플리케이션 쪽 ORM 모델 사이에 일시적 불일치를 만들어("caused a temporary mismatch"), 옛 코드를 돌던 인스턴스가 더는 없는 열에 접근하려 했다. 여러 Studio 엔드포인트에서 5xx 가 났고 15분 구간 트래픽의 약 23%(하루 전체 0.52%)였으며 워크플로 실행이 오류로 끝났다. 08:20 자동 알람, 08:31 원인 식별, 08:33 해결. 재발 방지는 두 단계로 나눠야 하는 스키마 변경을 자동으로 찾아 강제하도록 마이그레이션 검토와 출시 절차를 고치는 것이다. 표, 열 이름, 열을 지웠는지 바꿨는지는 적혀 있지 않다.
- 현실 비중: Liu 외 HotOS 2019(Azure 버그 장애 112건)에서 데이터 형식 불일치가 21%, Ghosh 외 SoCC 2022 에서 버그 가운데 하위 호환성 문제 14.6%(`ref-real-world-incidents.md` §1, M8).

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 릴리스 중 Studio 테이블 하나에 스키마 마이그레이션 배포, 새 코드는 아직 일부 인스턴스에만 | restaurants 테이블에 열 이름 변경 마이그레이션(`RENAME COLUMN region TO delivery_region`) 적용, restaurant-service 는 옛 코드 그대로(롤아웃 없음) |
| 원인이 된 결함 | 두 단계(확장 뒤 수축)로 나눠야 할 스키마 변경을 코드보다 먼저 적용해 DB 와 ORM 모델이 불일치 | 같음: 테이블에는 `delivery_region`, 엔티티 `Restaurant` 는 `@Column(name = "region")` |
| 전파 경로 | 옛 인스턴스가 없는 열을 조회 → 그 테이블을 쓰는 엔드포인트 5xx → 워크플로 실행 실패 | restaurant 의 가게 조회와 검색이 1054 로 실패 → 500 → order createOrder 첫 단계 실패, 재시도, 서킷브레이커 → 주문 502 |
| 사용자 증상 | 여러 Studio 엔드포인트 5xx(15분 구간 약 23%), 워크플로 실행 오류 | 주문 전량 502, 가게 상세와 검색 500, 메뉴와 인기 메뉴는 정상(부분 장애) |
| 탐지된 경로 | 배포 2분 뒤 자동 알람 | restaurant, order 오류율과 오류 로그 급증(119 이상 탐지 → 인시던트) |
| 완화와 복구 | 15분 만에 해결(방법 기재 없음), 마이그레이션 절차 보강 | cleanup 이 열 이름을 되돌림(같은 문장 꼴), 서킷브레이커가 5초 뒤 반열림에서 닫히며 즉시 회복 |

기전은 원본과 같다: "코드보다 먼저 적용된 스키마 변경 → 옛 ORM 이 없는 열을 조회 → 그 테이블을 쓰는 엔드포인트 5xx". 우리 스택에 맞춘 것은 대상(MySQL 8.0 의 restaurants 테이블)과 규모(서비스 한 개, 열 한 개)다. 원본은 일부 인스턴스만 옛 코드였고 우리는 restaurant 파드가 하나라 해당 엔드포인트가 100% 실패한다. 그래서 전체 오류율은 원본(23%)보다 높고, 같은 서비스 안에서도 그 열을 고르는 엔드포인트만 실패하는 부분 장애라는 점은 같다.

## 3. 숫자 근거 (2026-10-08, `scenario-stats.py`, 정식 + 후보 합계 39, 이 후보 전)

| 축 | 값 | 판단 |
|---|---|---|
| 서비스 | 쇼핑몰 23, 은행 9, 음식배달 7 | 음식배달이 가장 적다 → 음식배달 |
| 묶음 0개 | J, K, M, N | 아래 이유로 이번에도 막힘 |
| 묶음 L(데이터 형식 불일치) | 1(F30-R, 3%) → 2(5%) | 현실 21%(Liu) 대비 크게 적다 |
| 상한 근처 | A 7, B 7, D 7(각 17%), C 6(15%) | 피함 |
| 정답 위치 DB 테이블(가게) | 0 → 1 | §2-1 의 "DB 테이블(<무엇>)" 꼴, 새 줄 불필요 |
| 결제 경로 합계 | 10(25%) | 결제가 정답이 아니므로 변동 없음 |

0인 묶음을 고르지 못한 이유:

- J(결함 있는 새 버전 배포): 결함 이미지가 없다. `app.release` 는 `live_supported: false` 이고 고정 digest 의 결함 이미지가 노드에 없다. 결함 이미지를 만들려면 앱 코드 변경이 필요한데 이는 사람 검토 대상(원칙 문서 §3)이다.
- K(네트워크): `network.fault` 실행기가 안전한 주입 지점과 대역 외 복구가 없어 의도적으로 막혀 있다(`network_fault_executor.py`). CNI 가 flannel 이라 NetworkPolicy 도 집행되지 않는다(109 실측).
- M(DNS): 같은 날 폐기된 F31(Service 삭제로 DNS 레코드 소멸, 로컬 기록)이 사람이 되살릴 후보로 남아 있다. 다른 기전(파드 resolver 설정 오배포)은 새 파드가 시작하지 못해 롤링 업데이트가 옛 파드를 남기므로 피해가 나지 않는다.
- N(재시도가 장애를 키움): food, banking 의 모든 동기 호출에 서킷브레이커가 있어 재시도 증폭이 유지되지 않는다(F14-R 막힘과 같은 이유).

같은 날 설계 단계에서 함께 검토하고 버린 것(로컬 기록): food MySQL 연결 한도 축소(Buttondown 2026-03-31), 연결 풀 크기 축소 배포(GitLab 2021-07-27). 둘 다 order 의 DB 풀을 함께 말려 order readiness 가 빠지면 필수 중단 조건(entry_status 0)이 증상으로 발화하거나, 테스트베드 요청량(질의 수 ms)으로는 풀 축소가 피해를 내지 못한다.

왜 이 후보인가: 음식배달이 가장 적고, L 은 현실 대비 가장 덜 채워진 묶음 가운데 하나이며, 정답 위치(가게 테이블)가 처음이다. order 는 자기 DB 풀이 멀쩡해 입구가 끊기지 않으므로(502 로 답함) 음식배달의 구조적 제약(게이트웨이 없음, liveness 가 DB 를 봄)에 걸리지 않는다.

## 4. 인과 사슬 (코드와 인프라 위치)

```
db.ddl(mysql, 열 이름 바꾸기): testbed-mysql-0 안 MySQL 클라이언트
  SET SESSION lock_wait_timeout=10; ALTER TABLE restaurants RENAME COLUMN region TO delivery_region, ALGORITHM=INSTANT
  → 메타데이터만 바뀜(행 재작성 없음, 인덱스는 기본 키뿐이라 변화 없음)
  → restaurant GET /api/restaurants/{id}: RestaurantService.getRestaurant → findById
       'select r1_0.id,r1_0.name,r1_0.region,r1_0.status from restaurants r1_0 where r1_0.id=?'
       → MySQL 1054 Unknown column 'r1_0.region' → Hibernate WARN 'SQL Error: 1054, SQLState: 42S22', ERROR 'Unknown column ...'
       → GlobalExceptionHandler 가 ServiceException 만 매핑 → 처리되지 않은 예외로 500
  → restaurant GET /api/restaurants?region=&status=: RestaurantRepository.search 도 같은 열을 고르고 걸러 500
  → order POST /api/orders: createOrder 첫 단계 restaurantClient.getRestaurant
       → 5xx → ERROR 'Failed to fetch restaurant N: 500 ...' → ServiceException 502, @Retry 3회(200ms, 400ms)
       → 서킷브레이커 restaurant(10건 창, 50%) 열림 → fallback 502 'Restaurant service unavailable'(응답 본문만, 로그 없음. 5초 뒤 반열림 3건이 다시 restaurant 로 가서 'Failed to fetch restaurant' 를 남기고 다시 열림)
       → 주문 행을 쓰기 전에 끝남('Created order' 없음)
  ↔ restaurant GET /{id}/menu, /{id}/popular-menu: existsById 는 'SELECT COUNT(*) FROM restaurants r1_0 WHERE r1_0.id=?'(region 안 고름),
       menus 와 인기 메뉴 요약 테이블 조회 → 200 그대로
  ↔ restaurant /actuator/health: DataSource 확인은 SELECT 1 → UP, Ready 유지
```

앵커(정답지 `code_anchor` 와 같음):

- `food-delivery/restaurant-service/src/main/java/com/fooddelivery/restaurant/entity/Restaurant.java:16-17` (`@Column(name = "region")`)
- `food-delivery/restaurant-service/src/main/java/com/fooddelivery/restaurant/service/RestaurantService.java:54-59` (getRestaurant, findById), `:61-69` (getMenu, existsById 와 menus)
- `food-delivery/restaurant-service/src/main/java/com/fooddelivery/restaurant/repository/RestaurantRepository.java:12-16` (search JPQL)
- `food-delivery/restaurant-service/src/main/java/com/fooddelivery/restaurant/config/GlobalExceptionHandler.java:9-20`
- `food-delivery/order-service/src/main/java/com/fooddelivery/order/client/RestaurantClient.java:29-56`, `OrderService.java:57-61`
- `food-delivery/order-service/src/main/resources/application.yml:51-54, 69-77, 98-104`
- `food-delivery/db/init.sql:29-34` (restaurants 정의)
- 인프라: rca-testbed-food `testbed-mysql-0`(MySQL 8.0.46, tb-w3), restaurants 23행, `menus.restaurant_id`, `orders.restaurant_id` 외래 키(이름이 바뀌는 열이 아니어서 영향 없음)

로컬 실측(mysql:8.0.46 컨테이너, 같은 표 정의): 이름 변경 직후 같은 SELECT 가 `ERROR 1054 (42S22) at line 1: Unknown column 'r1_0.region' in 'field list'`, id COUNT 와 자식 표 INSERT(외래 키 확인)는 정상, 되돌리면 즉시 원래 정의. 다른 세션이 트랜잭션 안에서 restaurants 를 읽고 있으면 ALTER 가 메타데이터 잠금을 기다리다 10.6초 뒤 실패하고 아무것도 바뀌지 않았다. 실행기 스크립트 전체(preflight, run, 두 번째 run 거부, cleanup 두 번, recovery)를 가짜 kubectl 로 같은 컨테이너에 돌려 확인했다.

## 5. 원인 규정 (원칙 5, 6)

| 칸 | 값 | 근거 |
|---|---|---|
| `root_cause.target_id` | `food-mysql:fooddelivery.restaurants` | 결함은 테이블 스키마 쪽 변경(코드보다 먼저 적용된 열 이름 변경)이다. 원본 사후 보고의 원인과 재발 방지도 "스키마 마이그레이션과 그 출시 절차"를 가리킨다. 표기는 F33-R, F06-H 와 같은 '인스턴스:스키마.테이블' 꼴이고 같은 이름의 다른 개체가 없다 |
| `trigger_target_id` | null | 근본과 계기가 같은 곳(restaurants 에 대한 DDL) |
| `scoring.partial` | `food-delivery-restaurant`, `food-restaurant`, `food-mysql` | 오류가 찍히는 서비스(restaurant)와 DB 인스턴스는 원인 근처다. order 는 증상이라 부분 점수도 주지 않는다 |

원칙 5: 정답은 로그의 오류 문장("없는 열 region")과 엔드포인트별 갈림, 그리고 그 시각에 배포나 재시작이 없다는 사실로 낼 수 있는 결론이다. 코드 설계 결함 추론이 필요 없다. "restaurant 가 DB 스키마와 안 맞는 열을 읽는다"고 답하면 restaurant 서비스(부분)로, "restaurants 테이블의 region 열이 사라졌다(이름 변경, 마이그레이션)"고 답하면 정답이다.

원칙 6: restaurant 와 order 는 늘 하던 정당한 요청을 보냈고, 결함 있는 곳(바뀐 스키마)에서 실패했다. 열 이름을 바꾼 명령이 원인이다.

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 | 기대 |
|---|---|---|
| 감지 | restaurant, order 서버 스팬 오류(골든 시그널), 새 오류 로그 템플릿과 로그 급증(log-anomaly 이벤트) | 이벤트는 고장 시작 직후에 나고, 묶음은 마지막 이벤트 뒤 약 20분에 닫힌다. promote 는 대개 그 열린 묶음의 첫 판정(고장 2~4분째)에서 난다(아래). 그래서 1차 근거는 고장 중 골든 시그널이다: restaurant 업무 요청 약 40~50% 500, order 주문 생성 약 100% 502. 다만 그 판정의 최근 15분 창에는 고장이 2~4분만 들어가 오류율이 엔드포인트 비율보다 희석된다(대략 그 비율의 1/7~1/4, 평시 0 대비로는 여전히 큼). 로그 연쇄(restaurant 1054 → order 'Failed to fetch restaurant')는 회복 뒤 재판정의 보조 근거다 |
| 피해 판정 | 러너: 동반 부하의 주문 생성 5xx 비율 ≥ 0.5, 2xx 비율 < 0.2(3틱) | 평시 5xx 0, 2xx 0.92~1.0 |
| 원인 설명 | 로그 전수(1054 오류 문장), 스팬 엔드포인트별 갈림, KCM 무변화, DPM 정상 | §7 |

인시던트가 실제로 생길지에 대한 근거(평시 조회, 원칙 7, 2026-10-08 16:40 UTC 조회):

- **promote 는 대개 고장 중에 난다.** lucida-next 판정의 "최근 15분"은 판정 시각(운영에서는 벽시계)에 묶인다(judge_repo.go asOf, service_signals.go, store/incidents_judge.go). 실제 promote 시각은 PG `incidents.promoted_at` 이다(`incident_judge_decisions.last_reviewed_at` 은 promote 건에서 대개 나중 재검토 시각이라 쓰지 않는다). 최근 48시간 application, order, database, payment, web 묶음의 promote 89건: 마지막 이벤트와 거의 같은 시각(중앙 +0.2분, 약 13초. 44건은 마지막 이벤트 전), 80건이 마지막 이벤트 뒤 15분 안, 77건(closed_at 이 있는 것만 세면 76건)이 묶음이 닫히기 전에 promote 됐다(2회차 평가의 7일 실측도 344건 중 282건이 15분 안). promote 는 후보 전송(candidate_sent) 뒤 중앙 2.3분, 첫 이벤트 뒤 중앙 3.8분이고, 묶음은 마지막 이벤트 뒤 중앙 20분에 닫힌다(3회차 평가 실측). 반대로 drop 은 대개 묶음이 닫힌 뒤 판정되어 회복된 값을 본다(아래 OOM 반례들은 07:58~08:01 에 닫히고 08:20~08:24 에 drop, 037b0414 는 09:38 에 닫히고 10:02 에 drop). 열린 묶음은 처음 판정된 뒤에는 drop 되고 나서 candidate_published_version 이 오를 때만 다시 판정된다(store/incidents_judge.go:291). 또 이벤트는 고장 시작 직후에만 난다: 15분 가까운 food 고장 두 번(2026-10-08 09:10~09:25, 14:50~15:00)에서 이벤트는 시작 직후에만 났고, 7일 새 템플릿이 열린 52건 중 같은 template_id 의 surge, unknown_anomaly 가 30분 안에 다시 난 것은 3건(food 0건)이다(3회차 평가 실측). 그래서 F36-R 이 판정될 기회는 "고장 중 여러 번"이 아니라 대개 고장 2~4분째의 첫 판정 한 번이고, 그 판정은 고장 중이다(00f2b1c7 09:21:57, d1d43f83 14:56:43 이 같은 꼴로 고장 중 promote). min_hold 15분은 그 첫 판정이 고장 중에 나도록(묶음 닫힘 전, 실패가 이어지는 중) 하고 러너 판정과 녹화에 충분한 실패 구간을 주기 위한 길이다.
- **선례(promoted_at 기준).** 아래 묶음들은 모두 동반 부하가 없던 창이다(골든 시그널 30분 창의 요청량이 같은 UTC 시각 7일 평균과 같은 수준: 10-05 05:40 창 food order 7.9/분 대 평균 7.5, restaurant 71.5 대 66.2. 10-06 08:45 창 order 7.9 대 8.1. 10-06 09:40 창 order 8.3 대 8.3).
  - 33ddac71(2026-10-05 05:52~05:56, application, 멤버 12, dispatch 와 order 의 log-anomaly): 06:01:16 promote(마지막 이벤트 뒤 5분, 묶음이 닫히기 전). 그때 판정 문장은 "dispatch p95 2499→5308ms, 오류율 0.4% … 현재 이상 징후가 미회복 상태"로, 고장 중 골든 시그널이 근거였다. 07:10 은 뒤의 재검토 시각이다.
  - 2dc30be8(2026-10-06 09:54:42~09:54:50, 멤버 4, dispatch 새 템플릿과 surge, order surge): 09:56:23 promote, 판정 "현재 이상 상태이며 미회복". 12:04 는 마지막 재검토 시각이고 그 재검토 문장은 "운영상 무시할 수 있는 수준의 노이즈"였다.
  - 8634a30a(2026-10-06 08:56, 멤버 3, dispatch SQL Error 와 order 배차 용량 확인 실패): 11:39:08 promote(마지막 이벤트 뒤 2시간 43분, 묶음이 10:56 에 닫힌 뒤). 판정 문장 "골든 시그널 상의 수치는 아직 유의미한 임계치를 넘지 않았으나 서비스 간 의존 관계를 따라 오류가 전파되는 양상이 명확하다". 8634a30a 외에 a928e8e3(2026-10-02, 묶음 09:08 닫힘, 09:12:35 promote, "골든 시그널 유의미한 변동 없음 … dispatch DB 커넥션 이슈가 order 'Failed to check dispatch capacity' 로 전파")와 e5eefb74(2026-10-06, 05:44 닫힘, 06:19:50 promote, "골든 시그널 오류율 0.0% … SQL Error 와 Whitelabel Error 연쇄")도 회복 뒤 로그 연쇄로 promote 됐다(3회차 평가 조회).
  - d1d43f83(2026-10-08 14:54, order 오류율 5.6%, order 서킷브레이커 OPEN)은 F30-R 실행(14:52~15:00, surge.js 5rps 동반 부하) 중 promote 된 묶음이라 동반 부하 없는 선례로 쓰지 않는다.
- **restaurant 와 order 가 한 묶음이 될지.** 코드 규칙상 가능하다: merge_policy.go decideCoOnset(같은 시간 창 W 180초 안의 동시 발병 + 직접 토폴로지 관계 + 원인 쪽이 먼저)이고, apm_call order → restaurant(33ddac71 스냅샷 관측 11,974, 037b0414 스냅샷 11,840)의 원인 쪽(cause_side=target)인 restaurant 의 1054 가 order 의 실패보다 먼저거나 같다. 실측은 반반보다 조금 낫다: 2회차 평가 조회로 2026-09-25 이후 restaurant 와 order 로그 이상이 180초 안에 함께 난 시간대 19개 중 같은 묶음이 12개(약 60%)이고, 최근 두 번(10-06 09:55, 10-08 07:38, MySQL OOM 재시작)은 쪼개졌으며 마지막으로 묶인 것은 10-04 384118bc 다. 10-08 07:38 에는 restaurant surge(묶음 d1538919)와 order surge(a6930cd1)가 둘 다 07:41 까지 열려 있었는데 따로 묶여 drop 됐고, order 새 템플릿 'Failed to process payment for order <*>'(9b65903e)와 payment Hikari 연결 실패(ad5246f2)는 같은 시각에 났는데도 따로 묶여 LLM 판정 전에 "cheap-gate: all_anomaly_below_warning" 으로 suppress 됐다. 쪼개진 이유는 코드로 확인하지 못했다. 쪼개지면 각 묶음의 판정 입력이 그 서비스 하나라서, 고장 중 판정이면 restaurant 묶음은 restaurant 오류율로, order 묶음은 order 오류율로 promote 될 수 있지만 회복 뒤 판정이면 OOM 때처럼 "건수 적고 회복됨"으로 drop 될 위험이 있다. 또 food new_template 이벤트 43건 중 30건이 caution 등급이라, 쪼개진 order 쪽 묶음에 caution 새 템플릿만 남으면 LLM 판정 전에 cheap-gate 로 suppress 될 위험이 있다(10-08 09:14 고장 때 order 단독 묶음 e4e2704f, f709f258 이 같은 사유로 걸러졌다, 3회차 평가 조회).
- **이벤트 꼴.** 2회차 평가의 CH `lucida_events_local` 7일 food log-anomaly 조회로, new_template 에피소드 39개는 모두 열리자마자 닫혔고 unknown_anomaly 와 surge 는 중앙 약 3.5분 이어졌다. F36-R 의 첫 1054 와 'Failed to fetch restaurant' 이벤트는 새 템플릿이라 곧 닫히고, 같은 템플릿이 반복돼도 이벤트가 다시 나는 경우는 드물다(위). 그래서 묶음과 판정은 고장 시작 직후의 이벤트 몇 개(새 템플릿, 첫 surge)로 정해지고, 묶음은 마지막 이벤트 뒤 약 20분에 닫힌다.
- **토폴로지 로드 실패.** 2회차 평가가 본 ai-operator 로그(2026-10-07 23:27 이후)에 "topology load failed … context deadline exceeded (continuing without topology)" 가 1,336회, "topology loaded" 가 319회이고, 성공이 시간당 5~6회까지 떨어진 시간대(01시, 14시)가 있다. topology_provider 는 마지막 성공본을 10분까지만 쓰므로 토폴로지 없이 처리된 이벤트는 직접 관계 병합을 못 해 쪼개질 위험이 더 크다.
- 인시던트가 안 생기면 첫 실행 검증에서 ① 묶음이 어떻게 쪼개졌는지(멤버 서비스, 템플릿) ② 그 시각 토폴로지 로드 성공 여부 ③ promote 가 났다면 promoted_at 과 그때 판정 문장, drop 이면 판정 시각이 묶음이 닫힌 뒤였는지를 본다. 검증 하네스는 promote 가 대개 고장 중이나 직후(마지막 이벤트 15분 안)에 나지만, 회복 뒤 재판정(8634a30a 처럼 몇 시간 뒤)도 있으니 실행 뒤 몇 시간은 지켜본다.

## 7. 관측 근거 표 (119 실조회, 2026-10-08 15:30~16:45 UTC, 로그 행은 16:43 재조회)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 결정 증거는 전수 로그와 KCM, DPM 이다.

| 증거 | 119 표와 칸 | 조회 | 평시 결과 |
|---|---|---|---|
| 근본: 없는 열 오류 | CH `lucida_logs_local` (`service_name`, `severity_text`, `body`) | `SELECT countIf(service_name='food-delivery-restaurant' AND (position(body,'Unknown column')>0 OR position(body,'SQL Error: 1054')>0)) FROM lucida.lucida_logs_local WHERE timestamp > now() - INTERVAL 8 DAY` | 0건(2026-10-08 16:43 UTC 조회, 보존 창이 굴러가 그때 가장 이른 로그는 2026-10-01 16:00, 약 7일) |
| 근본 경로의 평시 기록(같은 로거가 CH 에 남는가) | 같은 표 | restaurant 8일 로그 템플릿 집계 | 2026-10-08 16:43 UTC 조회(가장 이른 로그 10-01 16:00): WARN 'SQL Error: 0, SQLState: 08S01' 145건, ERROR 'Communications link failure' 145건, SEVERE 'Servlet.service() ... threw exception' 148건(MySQL 재시작 때. 15:40 조회 때는 168, 168, 173건이었고 보존 창이 굴러가며 줄었다). SqlExceptionHelper 의 WARN/ERROR 와 처리되지 않은 예외 로그가 전수로 남는다 |
| 계기: 열 이름 변경 자체 | 없음 | DPM Top SQL(`dpm_topsql_local`, `body.sqlText`)에서 food MySQL 의 ALTER 검색 | 2026-08-13 이후 125만 행 중 ALTER 0건. DDL 은 Top SQL 에 남지 않는다. VM 의 DPM MySQL 지표에도 열 수준 값이 없다(`dpm.mysql.database.*` 는 표 수, 인덱스 수, 행 수, 크기뿐이라 열 이름 변경으로 바뀌는 값이 없다). 곧 DDL 자체는 119 에 남지 않고 계기는 오류 문장으로만 간접 확인된다. 계기의 흔적은 오류 문장(없는 열 이름)과 시작 시각, 그 시각에 다른 변경이 없다는 사실이다 |
| 다른 변경 없음(롤아웃) | CH `kcm_events_local` (`namespace`, `object_name`, `reason`) | `... WHERE namespace='rca-testbed-food' AND reason='ScalingReplicaSet'`, `... object_name LIKE 'testbed-restaurant%'` | testbed-restaurant 의 ScalingReplicaSet 0건(KCM 보존 약 45일, 2026-08-24 이후), 파드 `testbed-restaurant-549cc6cc64-8b2kz` 그대로(109 kubectl: 나이 58일, 재시작 0회). 그 밖에는 프로브 Unhealthy 이벤트가 하루 2~8건씩 간헐적으로 있을 뿐이다 |
| 피해: restaurant 엔드포인트별 | CH `otel_traces_local` (`span_kind='SERVER'`, `span_name`, `status_code`) | 최근 1시간 restaurant, order 서버 스팬 이름별 건수 | 표본 1시간: menu 921, 상세 740, 검색 188, 인기 메뉴 149, POST /api/orders 199(오류 5). 상세와 검색(region 을 고름)이 restaurant 업무 스팬의 약 46% |
| 피해: 골든 시그널 평시 | CH `agg_service_golden_signals` (`req_count`, `error_count`, `duration_p50_state`) | 7일 합계와 분위 | restaurant 436,163건 중 오류 12, p50 1.8ms, p95 28.8ms. order 53,369건 중 오류 58, p95 233ms. KST 시간대별 restaurant 분당 13~76건, order 2.3~8.5건(표본) |
| 하류: order 의 가게 조회 실패 로그 | `lucida_logs_local` | 8일 'Failed to fetch restaurant'(16:43 UTC 조회) | 0건('Restaurant service unavailable' 은 fallback 응답 본문이라 로그로 남지 않는다, order GlobalExceptionHandler 는 로그를 쓰지 않음). 평시 order 의 가게 관련 로그는 INFO 'Order rejected: restaurant 24 status=CLOSED'(가게 24 가 원래 CLOSED, 400) 뿐 |
| 배제: DB 정상 | VM `dpm.mysql.session.active_session`, `dpm.mysql.instance.avg_query_response_time`, `dpm.mysql.session.current_session` (target_id c8c558e5-…) | `quantile_over_time(0.5, ...[8d])` | 중앙 1, 7.28ms, 58 |
| 배제: 풀 대기 없음 | VM `db.client.connections.pending_requests{service_name="food-delivery-restaurant"}` | `quantile_over_time(0.95, ...[8d])` | 0 |
| 질의 모양(무엇이 깨지고 무엇이 남나) | `dpm_topsql_local` `sqlText` | restaurants 를 포함한 질의 24시간 | `SELECT COUNT(*) FROM restaurants r1_0 WHERE r1_0.id = ?`(existsById, region 없음), `SELECT r1_0.id, r1_0.name, r1_0.region, r1_0.status FROM restaurants r1_0 WHERE r1_0.id = ?`(findById), 검색 3종(region 포함). Top SQL 은 상위 항목만 남아 호출 수는 증거로 쓰지 않는다 |

## 8. 감별

- must_support: 1054 오류 로그(평시 0), 엔드포인트별 갈림(상세, 검색 500 / 메뉴, 인기 메뉴 200), order 의 가게 조회 실패와 502, 롤아웃과 재시작 없음, MySQL 과 restaurant 풀 정상.
- must_rule_out(정답지에 문장으로): restaurant 새 버전(롤아웃 없음), MySQL 다운이나 재시작(KCM testbed-mysql 재시작 2026-10-01 06시와 14시, 10-04 03시, 10-06 09시, 10-08 07시 UTC 창과 달리 연결 실패 없음, 메뉴 정상), 부하 증가나 용량 부족(아래), 가게 데이터 변경(그러면 200 과 400 'status=CLOSED'), 느린 쿼리(F33-R), 서비스 간 형식 불일치(F30-R).
- 동반 부하 경쟁 가설 배제: 동반 부하는 주문 유입을 시간대에 따라 약 2~8배로 늘린다. 그러나 실패는 시간 초과가 아니라 즉시 나는 SQL 오류이고 오류 문장이 없는 열 이름을 가리킨다. restaurant p95 는 밀리초대, Hikari 대기 0, MySQL 세션과 응답 시간은 평시다. 같은 부하에서 같은 파드, 같은 DB 의 메뉴 엔드포인트는 200 이다. 부하만으로는 이 오류도 이 갈림도 생기지 않는다. 정답지에 "부하 그대로"라고 쓰지 않았다.
- contrast_with: F33-R(같은 food MySQL DDL 계기, 느린 쿼리로 포화), F30-R(같은 L 묶음, 서비스 간 JSON 형식과 설정 배포), F25-H(DB 프로세스가 죽어 모든 질의가 끊김).

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

진행 대본: 설계 강도 하나로 고정한 평가 모드(`approved-fixed-f36-r`). 강도라는 축이 없다: 열 이름이 바뀌면 그 열을 고르는 질의는 요청량과 무관하게 100% 실패한다.

| 항목 | 값 | 근거 |
|---|---|---|
| primary | db.ddl(열 이름 바꾸기 모드), 계약 `{engine mysql, rca-testbed-food, testbed-mysql-0, fooddelivery, restaurants, region → delivery_region, minimum_rows 20}` | 실행기와 profiles.json 양쪽 같은 값(G2) |
| companion | load.north_south, order-surge.js, target_rps 2, ramp 2m, hold 21m, food 진입점 30181, 기준선 loadgen-food | F33-R 과 같은 스크립트와 rps |
| min_hold / settle / timeout | 15m / 30s / 20m, max_injection 25m | 성공 판정은 min_hold 뒤에 시작하므로 실패가 최소 15분 이어진다. 이벤트는 고장 직후에 나고 묶음은 약 20분 뒤 닫히며, promote 는 대개 그 열린 묶음의 첫 판정(고장 2~4분째)에서 나므로(promoted_at 기준 48시간 89건 중 80건이 마지막 이벤트 뒤 15분 안) 고장 중 골든 시그널이 판정 입력이 된다. 그 판정의 15분 창에는 고장이 2~4분만 들어가 오류율이 희석된다(§6) |
| success(3틱) | 동반 부하 주문 생성 5xx ≥ 0.5, 2xx < 0.2 | 서킷브레이커가 열리면 502 가 곧바로 나서 5xx 비율이 1 근처 |
| must_rule_out(2틱) | achieved_rps < 0.5, MySQL 파드 NotReady, restaurant 파드 NotReady | restaurant 가 빠지면 연결 실패라 다른 시나리오다. 열 이름 변경은 health 의 SELECT 1 을 건드리지 않는다 |
| abort | entry_status == 0 (필수) | entry_status 는 동반 부하의 주문 생성 응답 코드다. order 는 자기 DB 풀이 멀쩡해 502 로 답하므로 0 이 나오지 않는다 |
| recovery | target_health 200, MySQL Ready, restaurant Ready, 기준선 주문 2xx ≥ 0.7 (2틱, 10m) | target_health 는 러너 고정값 commerce :30080/health(live_probes.py TARGET_HEALTH_URL)라 food 회복을 보지 않는다. food 회복은 restaurant-ready 와 create-recovered(기준선 주문 생성 2xx)가 맡는다. 되돌린 뒤 서킷브레이커가 5초 안에 반열림, 성공으로 닫힘 |

부하 계산(가장 한가한 시간대 기준, KST 02~06시 기준선 1 iter/s):

- 기준선 restaurant 실패 원천: 둘러보기 55%(상세 1회) + 검색 15% → 초당 약 0.70건, 분당 약 42건(표본 약 4건).
- 동반 부하 2rps: 주문 여정 75% 에서 메뉴(성공) 뒤 주문 생성 초당 약 1.4건(F33-R 실측 기준) → order 502 분당 약 84건(표본 약 8건). order 의 가게 조회는 서킷브레이커가 열린 뒤 5초마다 반열림 3건씩 restaurant 로 가서 분당 약 36건 500. 동반 부하의 둘러보기 10%, 검색 5% 가 분당 약 18건 더한다.
- 합: restaurant 실패 분당 약 96건(표본 약 10건), 업무 요청의 약 40~50%. order 주문 생성은 거의 100% 실패(기준선 주문 초당 약 0.2건 포함). 15분이면 restaurant, order 각각 표본 오류 스팬 약 140건 이상. 바쁜 시간(KST 14~18시, 6 iter/s)에는 기준선 몫이 6배다.
- 동반 부하 상한: 2rps 는 load.north_south 계약(1~180) 안. order 가 실패로 빨리 끝나 VU 가 쌓이지 않는다.
- 서킷브레이커: 열리면 오류가 사라지는 것이 아니라 fallback 이 502 로 바뀔 뿐이라 피해 판정과 감지에 영향이 없다. restaurant 로 가는 호출은 줄지만 기준선 둘러보기와 검색이 restaurant 오류를 계속 만든다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Onfido 2024-10-18, 공식 링크)와 요소별 대응표가 있고 기전이 같다 (§2)
- [x] 분류 장부 §4-1, §6 갱신. 묶음 L 1→2(5%), 정답 위치 DB 테이블(가게) 0→1, 결제 경로 합계 10 그대로, 음식배달 7→8. 어느 축도 20% 미만 (§3)
- [x] 근본 원인 위치: `Restaurant.java:16-17`, `RestaurantService.java:54-59`, `RestaurantRepository.java:12-16`, 인프라 testbed-mysql-0 restaurants (§4)
- [x] 근본 원인 흔적: CH `lucida_logs_local` restaurant 1054 오류 문장(전수), 같은 로거의 평시 기록 확인 (§7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: 로그 전수, KCM, DPM. 스팬은 엔드포인트별 갈림의 보조 (§7)
- [x] 계기 흔적: DDL 문장은 남지 않지만 오류 문장이 없는 열 이름을 가리키고, 그 시각에 롤아웃, 재시작, 설정 변경이 없다(KCM). 인공 지연 없음 (§7)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다 (§5)
- [x] 정답지 세 칸이 원칙 6대로 (§5)
- [x] 감지, 피해 판정, RCA 증거 구분과 인시던트 근거(평시 drop 사유, promote 선례의 동반 부하 여부, promote 시점(promoted_at), 묶음 쪼개짐 실측) (§6)
- [x] 주입 도구가 시나리오 id 를 남기지 않는다: MySQL 클라이언트를 파드 안 root 로 열어 ALTER 한 문장, 인자와 스크립트에 id 없음(테스트로 고정). k6 태그는 tb-runner 의 k6 출력에만 남는다
- [x] 부하 상한과 서킷브레이커를 고려한 피해 계산, 가장 한가한 시간대 기준 (§9)

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- restaurant 로그에 'SQL Error: 1054, SQLState: 42S22' 와 "Unknown column 'r1_0.region' in 'field list'" 가 실제로 남는지. 메뉴 엔드포인트 스팬이 200 으로 남는지. Tomcat SEVERE 'Servlet.service() ... threw exception' 본문에 실패한 SQL(`from restaurants r1_0`)이 실리는지도 본다(실리면 판정과 RCA 가 표 이름까지 로그에서 읽는다).
- 119 묶음이 restaurant 와 order 를 한 묶음으로 묶는지(§6, 실측 약 60%, 최근 두 번은 쪼개짐). 쪼개졌으면 각 묶음의 멤버 템플릿과 판정 사유를 검증 보고서에 남긴다. 첫 1054 와 'Failed to fetch restaurant' 는 새 템플릿이라 그 에피소드는 곧 닫히고 이후 이벤트가 다시 나지 않을 수 있다. 첫 판정이 고장 2~4분째에 났는지 본다. order 쪽 묶음에 caution 새 템플릿만 남아 "cheap-gate: all_anomaly_below_warning" 으로 suppress 되지 않았는지도 본다.
- 고장 구간에 ai-operator 의 토폴로지 로드가 성공했는지('topology loaded' 대 'topology load failed … continuing without topology'). 마지막 성공본은 10분까지만 쓰이므로 그 구간에 성공이 없으면 쪼개짐의 원인으로 적는다.
- promote 가 났다면 `incidents.promoted_at` 과 그 시각의 판정 문장(고장 중 골든 시그널인지, 회복 뒤 로그 연쇄인지)을 적는다. `last_reviewed_at` 은 나중 재검토 시각일 수 있다.
- 주입이 메타데이터 잠금 대기로 실패(lock_wait_timeout 10초)하면 아무것도 바뀌지 않은 채 run 이 실패하고, 실행기가 MySQL 오류 첫 줄('ERROR 1205 (HY000) ... Lock wait timeout exceeded')을 stderr 로 남겨 러너 로그에서 구분된다. 그때는 진행 중인 긴 트랜잭션(order 의 createOrder 는 외래 키 확인으로 restaurants 에 공유 메타데이터 잠금을 잡는다)이 있었는지 보고 다시 돌린다.
- 이름 변경을 기다리는 몇 초 동안 restaurants 를 읽는 새 질의가 잠금 뒤에 줄을 선다. 녹화에서 주입 직전 몇 초의 지연으로 보일 수 있다(원본의 마이그레이션 적용 순간과 같은 꼴).
- cleanup 뒤 `SELECT COUNT(*) FROM information_schema.columns WHERE table_schema='fooddelivery' AND table_name='restaurants' AND column_name='region'` 이 1 인지(실행기 recovery 가 확인한다). 아니면 다음 무인 실행 전에 수동으로 되돌린다.
- promote 는 대개 고장 중이나 직후에 나지만 회복 뒤 재판정으로 몇 시간 뒤에 날 수도 있다(§6). promote 된 묶음의 판정 입력 서비스가 restaurant, order 인지 본다.
- 녹화 창에 food MySQL OOM 재시작(약 2일 주기)이 끼면 녹화로 쓰지 않는다.
