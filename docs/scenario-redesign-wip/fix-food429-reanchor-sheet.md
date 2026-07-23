# FIX 설계 시트 — food-429 착각 3 + 앵커 얕음 재앵커 5

근거: 헌장 §2 골든4조건·§2-A env위장금지 / fault-surface-{commerce,food-delivery,core-banking}.md / 실코드 file:line grep 실측(2026-07-23).
공통 사실(재확인): **food는 429 없음** — 용량초과=503(biz-reject, DispatchService.java:60-64), PG 4xx=ClientErrorException 전파(CB 무시), PG 5xx/timeout=502(PgApiClient.java:54). 429는 food 코드 어디에도 없음.

---

## A. food-429 착각 3종

### F06-P — food-partial-429  → **재정의 (429→503 dispatch capacity)**
현행: manifest는 thin stub(metadata 없음, 429는 slug/description에만), readiness=blocked. injection=food external-pg-mock + load.north_south.
문제: "food partial 429" 전제가 코드와 불일치. food엔 429 경로 자체가 없음.
- ① 앵커 🟡 : 429는 근거 0. 그러나 food **진짜 부분장애=503 배차거절**은 실코드 존재(DispatchService.java:60-64 countByStatus('ASSIGNED')>=maxCapacity→503, OrderService.java:96,145 fan-out이 503 수신→주문거절). 재정의하면 ✅ 가능.
- ② 정답 ❌ : metadata 자체가 없음(root_cause 미작성).
- ③ 감별 🟡 : 재정의 후 F19-Q와 동일한 "biz-503 ↔ fault-503" 2차감별 필요 — 상태코드로 불가, ASSIGNED 추세·DELIVERED 전이율 관측 미배선.
- ④ 주입 ❌→🟡 : injection이 external-pg-mock인데 이는 **PG 502(F19-S)** 표면이지 배차503 표면이 아님 — 현행 주입면과 주장증상 불일치. 배차503 재현엔 배차 스케줄러 정지 injector 필요(F19-Q 능력갭)= 미존재. PG-502로 재정의하면 mock.expectation 1줄 일반화로 가능.
- **처분: 재정의.** 두 갈래 — (a) 현행 external-pg-mock 주입면을 살려 **PG 지연→payment 502→order 연쇄(F19-S 메커니즘)** 로 재정의(배선만, 즉시 가능), (b) 슬러그가 말하는 "partial/배차부족"을 살리려면 **503 dispatch(F19-Q)** 로 재정의하되 배치정지 injector 신설 필요(능력갭). **권고=(a) PG-502 재정의** — 주입면이 이미 그것이라 정직·저비용.
- 수정 root_cause: (a안) food-payment의 외부 PG hop 지연/다운 → payment 502 → order fan-out(:154) 502 전파. target_kind=service, id=food-payment(외부 PG), mechanism=P4 CB/timeout.
- gap: mock.expectation food 일반화(profiles.json guard 1줄) + `loadgen.food_create_status_rate`·`prometheus.circuitbreaker_open{cb=pg}` 신설(F19-S와 공유).

### F15-H — pg-lock-food-429-simultaneous (동시복합)  → **food arm 재정의 (429→PG 502)**
현행: timeline.compose, blocked. commerce PG락 arm + food arm(429 가정) 동시.
- ① 앵커 🟡 : commerce PG락 arm은 유지 가능(C1/C2 실근거). food arm의 429만 근거단절.
- ② 정답 ❌ : metadata 없음(multi-root answer-key 미작성).
- ③ 감별 🟡 : 복합이므로 두 root 각각 must_support 필요 — food arm이 429면 관측 자체 불가.
- ④ 주입 🟡 : timeline.compose는 실재, 각 arm injector에 종속. food-429 arm은 실현불가 injector.
- **처분: food arm 재정의(multi-root 유지).** food arm 429 → **F19-S PG 502**(권고, 주입=mock.expectation food, commerce arm과 동종 PG-mock 계열이라 대칭적) 또는 F19-Q 503(injector 갭). commerce PG락 arm 유지. → "PG락(commerce) + PG지연 502(food) 동시" = 두 도메인 동시 결제경로 장애의 정당한 multi-root.
- 수정 root_cause(2개): {commerce-inventory/order PG row-lock 경합} + {food-payment 외부PG 502}. 동시발생, 감별=도메인별 증상표면 분리.
- gap: food arm은 F06-P(a)와 동일 배선. commerce PG락 arm answer-key 작성.

### F15-T2 — pg-lock-then-food-429 (순차복합)  → **food arm 재정의 (429→PG 502), 동일**
현행: timeline.compose 순차(commerce PG락 → 이후 food 429).
- 4조건 판정 F15-H와 동형(①🟡 commerce arm 유지/food arm 근거단절, ②❌ metadata없음, ③🟡, ④🟡 food-429 injector 실현불가).
- **처분: food arm 재정의(순차 유지).** food 2단계를 **F19-S PG 502**로 교체(권고). 순차 서사="commerce PG락 발생 → 회복 국면에 food PG hop도 502" = 시차 multi-root.
- 수정 root_cause: F15-H와 동일 2root, 단 시간축 분리(t0 commerce, t1 food).
- gap: F15-H와 동일.

**food-429 3종 공통 결론**: 셋 다 429 전제 폐기. 주입면이 이미 external-pg-mock(=PG표면)인 F06-P는 PG-502(F19-S)로 재정의가 가장 정직. F15-H/T2는 commerce PG-arm 유지 + food arm을 PG-502로 교체해 multi-root 재구성. 배차503(F19-Q) 재정의는 injector 능력갭 때문에 비권고. 세 개 모두 metadata answer-key가 아예 없어 ②는 전면 신작 필요.

---

## B. 앵커 얕음 재앵커 5종

### F02-R — pg-product-index-drop  → **CUT 권고 (드롭 무효과, 테이블 과소)**
실측: `idx_products_name`=products(name) **plain btree**. 유일 사용처 `findByNameContainingIgnoreCase`=`UPPER(name) LIKE UPPER('%q%')` **leading-wildcard**라 btree 원천 미사용 → 드롭해도 실행계획 불변. (ProductRepository.java 확인.)
추가조사: 인덱스가 실제 효과내는 쿼리는 존재함 — `findByCategoryId`가 `idx_products_category`(category_id) equality 사용. **그러나 products 테이블 ~2016행**(fault-surface 실측). PG seq scan 2016행 = sub-ms → **어느 컬럼 인덱스를 드롭해도 측정가능 지연 0**. 인덱스 인과가 성립하려면 대형 테이블(orders 50k / payments 50k) 필요한데 그건 F02-R 정체성(product 검색)이 아님.
- ① 앵커 ❌ : 드롭 대상 컬럼이 원천 미사용 + 테이블 과소로 효과 물리적 부재.
- ② 정답 🟡 : metadata는 있으나(distinguishing_evidence="rows scanned 변함") 실제로 rows scanned/plan 불변이라 정답이 거짓.
- ③ 감별 ❌ : "TopSQL plan 변화"가 감별근거인데 변화가 안 남.
- ④ 주입 ✅ : db.ddl DROP INDEX executor는 실재(readiness=ready). 단 주입은 되나 무증상.
- **처분: CUT 권고.** 재앵커 가능성=대형테이블 슬로우쿼리로 정체성 변경(C8 order stats/search: orders 50k, created_at 인덱스 부재 full scan)해야 하나 이는 별 시나리오(인덱스 "부재"지 "드롭"아님)라 F02-R을 살리는 게 아니라 대체하는 것. product-index-drop 자체는 실현불가 → **CUT**.
- gap(참고): 살리려면 products 시드를 수십만행으로 확대 + 검색을 category-equality나 prefix-LIKE(인덱스 타는)로 교체 = 앱·데이터 대공사. 비권고.

### F03-H — order-thread-saturation  → **재앵커 (합성 sleep 엔드포인트 → 실 다운스트림 블로킹)**
실측: 트리거가 `OrderController.java:66 /api/orders/reports/render` = `Thread.sleep(delayMs)` 순수 합성. **주석이 자백**: "F03-H 주입 표면: DB에 닿지 않고 서블릿 워커 스레드만 점유". 앱에 시나리오 전용 인위 엔드포인트를 심은 것 = §2-A 정신 위배(코드 동작 인위 왜곡).
- ① 앵커 🟡 : Tomcat 기본 200 스레드 포화는 실 메커니즘(C9). 그러나 트리거가 목적성 sleep이라 "자연 발생 결함"이 아님.
- ② 정답 ✅ : metadata root_cause 명확(스레드풀 고갈, DB 무접촉 감별).
- ③ 감별 ✅ : "DB blocking 0·풀 정상·하류 p95 정상"이 결정적(F03-P와 구분). 설계 우수.
- ④ 주입 ✅ : k6 Job이 render 호출 = 실재. 단 대상이 합성 엔드포인트.
- **처분: 재앵커.** 자연 스레드포화 경로 = **실 느린 다운스트림**. checkout 5-hop은 동기 blocking(SimpleClientHttpRequestFactory, 풀 없음)이라 하류 1개가 read-timeout 미만으로 느려지면 Tomcat 워커가 그 hop에 블로킹 누적(C9/C12). 재앵커: **mock.expectation로 PG mock 지연 주입(예 8s < payment read-timeout 15s)** → order/gateway Tomcat 스레드가 checkout hop 대기로 자연 포화. render 합성 엔드포인트 제거.
  - 주의: 이 경로는 DB 커넥션풀(order Hikari 10)도 함께 고갈(C12)돼 "순수 스레드풀 격리"라는 F03-H의 감별 정체성이 흐려짐. 순수 스레드-only 격리를 유지하려면 render 합성이 유일 — 즉 **정체성(스레드풀 단독 격리) vs 자연성(§2-A)** 이 상충. 권고: **자연성 우선 재앵커**(하류지연 기반), 감별은 "스레드+풀 동반 고갈이나 하류 p95만 원인"으로 재기술. 순수 격리를 고집하면 합성 엔드포인트가 불가피하므로 그 경우 Class를 "테스트 전용 합성 부하"로 명시하고 golden 강등.
- 수정 root_cause: 느린 하류(PG mock 지연) → checkout 동기 hop 블로킹 → order Tomcat(200)+Hikari(10) 포화. target=downstream 지연, 증상=order.
- gap: mock.expectation food/commerce 일반화(이미 commerce는 실재). render 엔드포인트 제거(소스 변경).

### F03-P — small-hikari-checkout  → **재라벨 Class B(설정오배포) 또는 자연화**
실측: payment yml에 hikari 블록 없음 = **기본 10**. F03-P는 `SPRING_DATASOURCE_HIKARI_MAXIMUM_POOL_SIZE` env로 5→3→2 강제 축소(k8s.env). = §2-A "풀 크기 강제 축소" 정면 위배(Class A 위장).
- ① 앵커 🟡 : 커넥션풀 고갈 메커니즘은 실재(C1/C12). 그러나 고갈을 env 인위축소로 만듦.
- ② 정답 ✅ : metadata가 이미 정직 — cause="Undersized HikariCP pool", "surge는 증폭기(R5)", "원인은 설정". 사실상 이미 설정결함으로 서술.
- ③ 감별 ✅ : "payment pod·DB·호스트 정상 + 풀설정 rollout 이력"이 F07-H/F06과 구분. 설계 양호.
- ④ 주입 ✅ : k8s.env executor 실재(readiness=ready).
- **처분: Class B 재라벨(설정 오배포).** metadata가 이미 "설정오배포" 논지라 정직화 비용 낮음. F08-P(read-timeout 오설정)와 동일 부류로 **"undersized pool 설정 오배포"** Class B/config로 명시. §2-A는 "자연고갈 아니면 Class B로 명시하거나 폐기"를 허용 → 재라벨이 정답.
- 대안(자연화): env축소 제거하고 기본풀10을 **순수 부하(북남 surge 고강도)만으로** C12 자연고갈 유도 → 그러나 order/payment 기본10은 상당 부하 필요하고 F07-H(surge=root)와 정체성 충돌. 권고=**Class B 재라벨**(자연화보다 감별이 깨끗).
- 수정 root_cause: payment Hikari maximum-pool-size 설정 오배포(10→2). Class B/config. surge=증폭기.
- gap: 없음(executor·metadata 이미 정합). 라벨/문서만 정정.

### F07-H — north-south-surge  → **재앵커 (부하=root 정당화, 용량무릎=코드한계 명시)**
실측: 부하 자체가 원인. 현행 metadata는 코드앵커 0("진입량↑ 내부호출량↑"만).
- ① 앵커 🟡→✅ : 부하가 root인 시나리오는 정당(용량 무릎). 단 "무릎이 코드의 어느 한계냐"를 앵커해야 함. 코드 한계 실측: **order/payment/product/inventory Hikari 기본 10 + @Transactional 안 5-hop 동기호출(OrderService.java:59/144) + @Transactional(timeout) 부재 + Tomcat 200**. surge가 동시 checkout>10을 만들면 order Hikari(10)가 무릎(C12) — 이게 코드 한계 앵커.
- ② 정답 🟡 : metadata root_cause가 "traffic surge"로 추상적. "surge → order Hikari(10) 고갈 무릎"으로 구체화 필요.
- ③ 감별 ✅ : "진입량·내부호출량 동반증가, 특정 retry반복 아님"이 F08-P(timeout)·F03-P(풀축소)와 구분. 유지.
- ④ 주입 ✅ : load.north_south adaptive k6 실재(readiness=ready).
- **처분: 재앵커(정당화).** cut 아님 — 부하=root는 정당한 용량 시나리오. root_cause를 "surge가 order Hikari pool(10)·Tomcat(200) 용량 무릎을 초과"로 코드앵커. Class A(코드 한계) 유지 가능하나 엄밀히는 "용량/부하" 성격이라 **A(코드용량한계)로 앵커**하되 감별에서 F03-P(설정풀축소)·F08-P(설정timeout)와 "설정변경 이력 부재 + 순수 진입량 초과"로 분리.
- 수정 root_cause: north-south 진입 폭주가 order createOrder/checkout의 tx내 동기 5-hop + Hikari 10 구조를 무릎 초과로 몰아 checkout 5xx. target=capacity(order 서비스 구조한계), mechanism=P1/P2 용량초과.
- gap: `load.north_south` 실재. adaptive 무릎 탐색 calibration으로 "무릎 rps" 정의 필요(관측=achieved_rps·checkout_5xx_rate 실재).

### F08-P — low-read-timeout-config  → **재라벨 Class B(설정오배포) 유지 / 또는 실 latent(P1)로 재앵커**
실측: 현행이 order→payment read-timeout을 15s(order yml:54)→250ms로 env 오버라이드. 250ms는 임의 합성. **실 결함은 정반대**: read-timeout 15s가 **과대**하고 `@Transactional(timeout)` 부재라 느린 payment가 order 커넥션을 최장 30s(retry 2회) 점유 = P1/P2 latent.
- ① 앵커 🟡 : 250ms는 실코드 리터럴이 아님(합성). 단 metadata는 이미 "설정 오배포"로 정직 서술.
- ② 정답 ✅ : metadata cause="Misconfigured read-timeout (config misdeployment)" — 정직.
- ③ 감별 ✅ : "span이 250ms에서 client-side 절단, 외부 payment 정상, rollout 이력"이 F06-R(외부hang)과 구분. 양호.
- ④ 주입 ✅ : k8s.env SPRING_APPLICATION_JSON 실재.
- **처분: 두 갈래.** (a) **현행 Class B 유지 정직화** — "read-timeout 250ms 오배포"는 실제 운영에서 나오는 설정사고이고 metadata가 이미 config-misdeploy로 라벨. §2-A는 "설정오배포 Class로 명시"를 허용 → **그대로 Class B/config로 확정**(저비용, 권고). (b) **실 latent로 재앵커** — 정반대인 "timeout 부재/과대 + tx내 동기호출"(P1)로 바꾸면 F06-R/C1과 메커니즘 중복(외부 payment 지연→order 풀고갈)이라 신규가치 낮고 F03-H 재앵커안과도 겹침. → **권고=(a) Class B 설정오배포로 확정**. 방향(짧게)이 실 결함(길게)의 반대인 점만 문서에 명시해 "인위값"이 아니라 "오배포된 잘못된 값"으로 성격 고정.
- 수정 root_cause: order→payment read-timeout 설정 오배포(15s→250ms). Class B/config. 조기 timeout으로 checkout 결제확정 5xx.
- gap: 없음(executor·metadata 정합). 라벨 확정만.

---

## C. 처분 요약

| id | 처분 | ① 앵커 | ② 정답 | ③ 감별 | ④ 주입 |
|---|---|---|---|---|---|
| F06-P | 재정의(429→PG502/F19-S) | 🟡→✅ | ❌(신작) | 🟡 | 🟡(mock 일반화) |
| F15-H | food arm 재정의(→PG502), multi-root | 🟡 | ❌(신작) | 🟡 | 🟡 |
| F15-T2 | food arm 재정의(→PG502), 순차 | 🟡 | ❌(신작) | 🟡 | 🟡 |
| F02-R | **CUT 권고** | ❌ | 🟡(거짓) | ❌ | ✅(무증상) |
| F03-H | 재앵커(합성sleep→실하류지연) | 🟡 | ✅ | ✅ | ✅ |
| F03-P | Class B 재라벨(설정오배포) | 🟡 | ✅ | ✅ | ✅ |
| F07-H | 재앵커(부하=root, Hikari10 무릎 앵커) | 🟡→✅ | 🟡 | ✅ | ✅ |
| F08-P | Class B 확정(설정오배포) | 🟡 | ✅ | ✅ | ✅ |

**CUT 권고: F02-R** (인덱스 드롭이 leading-wildcard 미사용 + 테이블 2016행 과소로 물리적 무효과).

**배관 gap 집계**:
- mock.expectation food 일반화(profiles.json guard 1줄) — F06-P·F15-H·F15-T2 food arm 공유.
- 신규 query_id: `loadgen.food_create_status_rate`, `prometheus.circuitbreaker_open{cb=pg}`(food-payment) — food-429 3종 공유(F19-S와 동일).
- answer-key 전면 신작: F06-P·F15-H·F15-T2 metadata 부재(3개 모두 scenario-metadata에 없음).
- F03-H: order-service 소스에서 합성 render 엔드포인트 제거 + mock.expectation 지연 주입으로 트리거 교체.
- F03-P/F08-P: 코드·executor 무변경, Class B/config 라벨·문서 정정만.
- F07-H: calibration으로 "무릎 rps" 정의, root_cause를 order Hikari(10) 용량한계로 구체화.
