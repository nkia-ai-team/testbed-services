# 설계 시트 — F24-Q: restaurant 최상류 차단 → order 전량 502 (food, Class A)

- **백로그 근거**: 헌장 §4 #5 "food 실코드 결함 3종 / food / A / P1·P3·P4 / food 도메인 code-anchor 빈약"의 확장. F19가 order 자체(P1)·dispatch(P3)·PG(P4)를 다뤘고, F24-Q는 남은 **최상류 리프(restaurant)** 를 채운다.
- **코드 근거**: fault-surface-food-delivery.md **C4**(+§2 restaurant-service, §3 PopularMenuBatch, §4 에러경로표) + food-delivery 실코드 file:line 실측(아래 앵커 전부 grep 확인).
- **패턴 매핑**: 순수 P1~P7 하나에 딱 떨어지지 않고 **P4 변형(불균일 resilience의 반대 극단 — restaurant엔 CB/Retry가 *있으나* retry가 오히려 증폭기)** + **fan-in 취약(모든 주문의 단일 상류 의존)**. 정체성은 "상류 리프 지연이 retry 증폭·hard-dependency로 order 전량 502가 되는 코드 구조".
- **정체성 한 줄**: 증상은 order 502 전량이나 root는 **restaurant**. F19-P(order 풀고갈)·F19-S(PG 502)·F19-Q(dispatch 503)와 함께 "증상=order, 원인=제각각"의 **4자 감별**을 완성한다.

---

## 1. 인과 사슬 (코드 실측)

### F24-Q — restaurant 지연/포화 → order retry 증폭 → 전량 502
```
load.north_south: restaurant NodePort(30181)에 조회 부하 폭주
  → restaurant Hikari pool(10, 최소) + Tomcat 스레드 포화, restaurant p95 급등
  → order.createOrder(@Transactional, OrderService.java:57)의 최상류 2콜:
       restaurantClient.getRestaurant(:60) → GET /api/restaurants/{id}
       restaurantClient.getMenu(:73)      → GET /api/restaurants/{id}/menu
     이 read-timeout 5s 초과
  → @Retry(name="restaurant") max-attempts=3 지수백오프(200ms×2) 재시도 증폭
     (RestaurantClient.java:30,59) — 실패 주문 1건당 restaurant 호출 최대 6회
     = restaurant에 양성(+) 피드백 부하 가중
  → 실패율 50%↑ → @CircuitBreaker(name="restaurant") open 5s(RestaurantClient.java:29,58)
  → getRestaurantFallback/getMenuFallback이 즉시 502 BAD_GATEWAY(:44,:55)
  → createOrder가 DB 쓰기(:128) '이전' 단계(:60/:73)에서 502 던짐
     → GlobalExceptionHandler가 status 그대로 반환 → order 전량 502
```
**앵커**:
- `OrderService.java:57`(@Transactional createOrder), `:60`(getRestaurant), `:73`(getMenu) — 모든 주문이 restaurant에 **2회 hard-dependency**. 두 콜은 order DB 쓰기(:128)보다 **앞**이라 502가 커넥션풀 고갈 없이도(F19-P와 달리) 곧장 발생.
- `RestaurantClient.java:29-30,58-59`(@CircuitBreaker(restaurant)+@Retry(restaurant)), `:44,:55`(502 BAD_GATEWAY / fallback), `:40-42,:69-71`(4xx는 ClientErrorException로 회로 안 엶 — 진짜 장애만 502).
- order `application.yml:54`(restaurant read-timeout 5s), `:69-77`(CB restaurant window10/min5/50%/open5s), `:98-104`(retry restaurant max-attempts=3·200ms·exp×2).
- restaurant `application.yml:13`(Hikari **10**, 최소 풀), restaurant NodePort `k8s/21-restaurant-deploy.yaml:118`(30181).
- 부하원 보조: `MenuRepository.java:17`(order_items⋈orders⋈menus 네이티브 크로스테이블 조인, orders 20k 풀스캔), `PopularMenuBatch.java:37,45`(@Scheduled 1h, deleteAll 후 재삽입) — restaurant DB/CPU 자연 압박원이나 **1h 주기라 주입 타이밍 제어 불가**(④ 쟁점, 아래 §4).

**핵심 오진 구조**: restaurant는 downstream 없는 **읽기 전용 리프**라 평소엔 무해해 보이나, 모든 주문의 단일 상류라 여기 지연이 order 전량으로 직결된다. retry(3x)가 restaurant를 더 밀어붙이는 양성 피드백이고, 502는 order에서 관측되므로 naive RCA가 order를 root로 오판한다.

---

## 2. 감별 설계 (골든 조건 ③) — 4자 감별표

증상이 모두 **order 주문 실패**로 수렴하는 food 4시나리오의 결정 감별. 축 = (a) order가 받는 상태코드, (b) 어느 downstream이 신호를 내는가, (c) order 자신의 풀 상태.

| | must_support (양성 신호) | must_rule_out (감별점) |
|---|---|---|
| **F24-Q**<br>(restaurant 최상류) | order create **502**↑; **restaurant p95 급등**(`food-delivery-restaurant`); restaurant error_rate↑; (관측 가능 시)restaurant CB open | payment 502 낮음(<0.05) → S 아님 · order create 503 낮음 → Q(dispatch) 아님 · order Hikari pending 정상~경미(502가 풀고갈보다 **먼저**, DB쓰기 전 :60/:73에서 발생) → P 아님 |
| **F19-P**<br>(order 롱tx 풀고갈) | order create 5xx/**timeout**↑; order Hikari **pending>0**(active≈15); payment 502 낮음 | 하류 502 없음(payment 200 지연) → S·Q·**본건 아님** · restaurant p95는 정상 → **F24-Q와 여기서 갈림** |
| **F19-Q**<br>(dispatch 배치정지) | order create **503**↑; dispatch ASSIGNED 단조증가; DELIVERED 전이=0 | 502 아님(503) → P·S·본건 아님 · restaurant/payment p95 정상 |
| **F19-S**<br>(PG 502→CB) | payment **502**↑; **pg CB open**; order create 502↑ | restaurant p95 정상(payment가 root) → **F24-Q와 여기서 갈림** · order 풀 정상 → P 아님 |

**F24-Q ↔ F19-S 결정 감별(둘 다 order 502)**: 어느 downstream이 아픈가로 갈린다. F24-Q=**restaurant** p95/error_rate 급등, payment 정상. F19-S=**payment** 502·pg CB open, restaurant 정상. 상태코드(502)는 동일하므로 **downstream별 p95/error_rate**가 유일한 결정 증거.

**F24-Q ↔ F19-P 결정 감별(둘 다 order 5xx)**: F24-Q는 **하류(restaurant)가 명시적 502**를 내고 restaurant p95가 뜬다. F19-P는 **하류가 502를 안 내고**(payment 200 지연) order 자신의 Hikari pending만 뜬다. 또 F24-Q의 502는 DB 쓰기 전(:60/:73)에서 나므로 order 커넥션풀 고갈 없이도 발생 = 풀 신호가 약하다.

**오답 유도**: 네 개 모두 증상이 order로 수렴 → naive RCA가 order를 root로 오판. F24-Q는 한 겹 더 — restaurant가 downstream 없는 "무해한 리프"라 후보에서 배제되기 쉬우나, 실은 모든 주문의 단일 상류 SPOF다.

---

## 3. 골든 4조건 자체점검표

| 조건 | F24-Q 판정 | 근거 |
|---|---|---|
| ① 코드/인프라 앵커 | ✅ | OrderService.java:57,60,73(모든 주문 restaurant 2회 hard-dep) + RestaurantClient.java:29-59(CB/Retry/502 fallback) + order application.yml:54,69-104(timeout5s/CB/retry3) + restaurant application.yml:13(Hikari10) + k8s/21:118(NodePort30181). 전부 grep 실측. |
| ② 정답(answer-key) | ✅ | metadata root_cause{target=food-restaurant(포화/지연), mechanism=order retry-증폭 hard-dependency fan-in} 작성. code_anchor 코드거동 일치. infra_anchor="restaurant 자연 포화(Hikari10 최소풀), 순수 코드/부하 — 별도 인프라 조작 없음" 정직 표기. |
| ③ 감별 가능 | 🟡 | 감별 **설계 완결**(4자). 관측: restaurant p95(`food-delivery-restaurant`)·apm_service_error_rate·loadgen.food_create_status_rate는 **이미 등록됨**(F02-P가 restaurant_p95 실사용) → F19보다 유리. 단 (a) **restaurant CB open query 부재**(circuitbreaker_open query 자체가 registry에 없음, F19-S cb=pg 갭과 동일), (b) surge.js food create 스텝이 business.5xx를 실제 emit하는지 미검(F19 지적) → 🟡. |
| ④ 주입 수단 실재 | 🟡 | load.north_south로 restaurant NodePort(30181) 조회 폭주 = **실재·계약준비완료**(F02-P가 동일 entry/script/baseline 사용, 신규 executor 0). 계약에 F24-Q allowlist+tag_pattern+scenario_parameters 추가만 필요(순수 배선). **단 2가지 정직한 유보**: (1) restaurant 포화가 order를 502까지 확실히 밀어내는 **효과 크기는 첫 calibration 필요**(rps 튜닝), (2) **Class A/B 경계 긴장**(아래 §5). |
| **종합** | | ①②✅ ③④🟡 → **readiness=draft, prerequisite_gate.state=blocked, live_allowed=false** (배선+calibration, 능력갭 아님). |

---

## 4. 주입 수단 후보 저울질 (④ — 정직한 선택 근거)

| 후보 | 실재성 | 헌장 부합 | 판정 |
|---|---|---|---|
| **A. load.north_south restaurant flood(30181)** ← 채택 | ✅ 계약준비완료(F02-P 선례, executor·entry·script·baseline 전부 실재) | 자연 포화(Hikari10) — §2-A "인위 왜곡" 아님. 다만 부하=트리거라 Class A/B 경계 긴장(§5) | **채택**. 실재성·저비용(배선만)·정체성 부합 최고. |
| B. mock.expectation로 restaurant 지연 | ❌ **부적용**. mock.expectation은 mockserver(PG mock) 전용. restaurant는 **실 Spring 서비스**라 mock이 없음. | — | 기각(주입면 부재). |
| C. PopularMenuBatch 무거운 조인(자연 부하원) | 🟡 코드 실재(MenuRepository.java:17, 1h 배치)이나 **@Scheduled 1h·수동 트리거 훅 없음** → 주입 타이밍 제어 불가 | ④ 위배(온디맨드 재현 불가). 헌장 §5 P5(스케줄러 제어점 부재) 갭과 동종 | 기각(타이밍 제어 불가). 앱에 배치 수동트리거 엔드포인트 신설 시 이상적. |
| D. restaurant 노드/파드 CPU stress(SMS executor) | 🟡 실현 가능하나 restaurant를 직접 stress | §2-A "Class B(인프라) 위장 위험". root가 "restaurant host CPU"가 되어 코드 fan-in이 아닌 **인프라 알람**으로 귀결 | 기각(Class A 의도와 불일치). 별개 Class B 시나리오로는 정당. |
| E. db.lock/db.ddl로 MySQL `menus` 지연(F02-P류) | 🟡 db.ddl mysql은 실재(F02-P index drop). db.lock mysql 지원은 미검 | getMenu만 차단(getRestaurant는 정상). root=DB인덱스/락이 되어 **F02-P와 정체성 충돌** | 기각(F02와 중복). load 불안정 시 DPM-flavored 대체 fallback으로만 보류. |

**선택 근거(한 문단)**: restaurant를 지연시킬 수단 중 **mock.expectation은 restaurant가 실 서비스라 원천 부적용**(mock은 PG 전용), PopularMenuBatch는 **@Scheduled 1h 타이밍 제어 불가**로 ④ 탈락, CPU stress는 **§2-A Class B 위장**으로 Class A 의도와 충돌, DB 조작은 **F02-P(인덱스)와 정체성 중복**이다. 남는 유일한 정직한 선택은 **load.north_south로 restaurant NodePort(30181)에 조회 부하를 흘려 Hikari 10(최소풀)·Tomcat 스레드를 자연 포화**시키는 것이며, 이는 F02-P가 동일 entry·script·baseline으로 이미 라이브 검증한 계약준비완료 경로라 신규 executor가 필요 없다(순수 배선). 부하가 트리거라는 점에서 Class A/B 경계에 긴장이 있으나(§5), 주입은 env로 코드동작을 왜곡하지 않는 자연 포화이고, 테스트 대상 결함은 "부하 그 자체"가 아니라 **restaurant 지연을 order 전량 502로 증폭시키는 order측 코드(retry 3x·bulkhead 부재·단일 상류 hard-dependency)** 이므로 정직하게 Class A로 유지한다.

---

## 5. 헌장 부합성 평가 (한 문단)

F24-Q는 F19 3종이 채운 order/dispatch/PG를 넘어 food 호출그래프의 **최상류 리프(restaurant)** 를 code-anchor로 메꿔(①② ✅, OrderService·RestaurantClient·양측 application.yml·restaurant Hikari·NodePort 전부 grep 실측) fault-surface C4를 시나리오화한다. F19 대비 뚜렷한 강점은 **감별 관측이 이미 상당수 배선돼 있다는 것**이다: restaurant p95(`food-delivery-restaurant`)는 F02-P가 실사용 중이고 apm_service_error_rate·loadgen.food_create_status_rate도 등록돼 있어 ③의 결정축(restaurant가 아픈가 vs payment가 아픈가)을 관측 가능하다 — 남은 갭은 restaurant CB open query(circuitbreaker_open 자체가 registry 부재, F19-S cb=pg와 공유) 하나뿐이다. 가장 정직해야 할 지점은 ④의 **Class A/B 경계**다: 주입이 부하이므로 헌장 §3.2가 F07-H를 "부하=원인, 코드앵커 0"으로 강등한 함정에 걸릴 위험이 있다. 그러나 F07-H는 부하받는 서비스 자신이 root(증상=원인)인 순수 용량 시나리오인 반면, F24-Q는 **부하받는 곳(restaurant)과 증상 나는 곳(order 502)이 분리**되고 그 분리를 만드는 것이 order측 코드(retry 3x 증폭·bulkhead 부재·모든 주문의 단일 상류 hard-dependency)라는 점에서 근본이 다르다 — 고칠 대상이 "트래픽 줄이기"가 아니라 "order의 restaurant 의존을 캐시화/bulkhead화/retry 감축"인 **코드 결함**이다. 남는 두 유보는 정직하게 blocked로 남긴다: (1) restaurant 포화가 order를 502까지 확실히 밀어내는 효과 크기는 첫 calibration에서 rps 튜닝으로 확정해야 하고, (2) restaurant CB open 가시화는 F19-S와 공유하는 신규 query에 종속된다. 결론: F24-Q는 **배선 백로그(load 계약 allowlist + circuitbreaker_open query 신설)로 즉시 올릴 값어치가 있는, 정직하게 draft/blocked인 Class A 설계**다.
</invoke>
