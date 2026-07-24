# 설계 시트 — F23-R: commerce 재입고 배치 정지 → 재고 소진 → checkout 409 조용한 기능마비 (Class A)

- **백로그 근거**: 헌장 §4(신규 설계 백로그) 계열 + fault-surface-commerce.md §C10 "inventory 재고 소진 → checkout 영구 실패 (배치 의존 자립성)". commerce 도메인의 Class A "조용한 기능마비"(5xx 없는 4xx 마비) 표본.
- **코드 근거**: fault-surface-commerce.md §C10 + commerce/inventory-service·order-service·product-service 실코드 file:line(아래 앵커 전부 grep/read 확인).
- **동종성 선언**: F23-R은 **F19-Q(food 배차 만료 배치 정지)와 동종 구조** — `@Scheduled` 배치가 멈추면 자원이 회수/보충되지 않고 단조 고갈되어, 정상 업무거절과 **상태코드가 동일한** 조용한 기능마비를 일으킨다. F19-Q의 감별 정체성이 "biz-503 ↔ fault-503"이라면 F23-R은 "정상 재고소진(biz-409) ↔ 배치정지(fault-409)"의 동형 2차 감별이다. 두 시나리오는 **같은 injector 능력갭(스케줄러 정지 훅)을 공유**하며, 훅 하나로 둘 다 해제된다(§4-B).
- **공통 함정(반영됨)**: reserve 재고부족은 **409 CONFLICT**(4xx)라 order의 productClient CB가 실패로 집계하지 않는다(ProductClient.java:37 명시). 5xx도 CB open도 없다 → "조용한 기능마비". loadgen(script.js·surge.js)은 409를 pass로 접어 지표에 드러내지 않는다 — F19-Q의 503-fold와 동형.

---

## 1. 인과 사슬 (코드 실측)

```
[배치 정지: ReconciliationBatch.run @Scheduled 10분 멈춤]  ← 주입수단: §4 능력갭
  → 상주 부하(loadgen checkout 5-hop)가 재고를 계속 소비(reserve = stock -= qty)
  → 재입고(RESTOCK) 스텝 미실행 → inventory.stock이 회수 없이 단조 감소 → 0 도달
  → InventoryService.reserve: stock < quantity → 409 CONFLICT 'Insufficient stock'
  → product InventoryClient가 409를 status 그대로 전파(:33,48)
  → order productClient: 4xx는 CB/Retry 실패집계 제외(:37) → ClientErrorException으로 그대로 전파(:39), 5xx·CB open 없음
  → checkout이 409로 실패 급증   ← 증상=order/gateway 409, 원인=inventory 재입고 배치 정지
```

앵커(전부 확인):
- `commerce/inventory-service/.../service/ReconciliationBatch.java:46` — `@Scheduled(fixedDelayString="${inventory.reconciliation.interval-ms:600000}")` 10분 주기. **주기가 env로 바인딩됨**(F19-Q의 하드코딩 `fixedDelay=30000`과 결정적 차이 — §4-B).
- `:47` — `@Transactional` (대사+재입고 단일 트랜잭션).
- `:74-91` — RESTOCK 루프: `stock < restockThreshold(20)`이면 `stock = restockTarget(200)`로 보충하고 `RESTOCK` movement 원장 기록. **이 루프가 안 돌면 재고가 영구 소진.**
- `:16-25` — 파일 주석이 자립성 버그를 실측 기록: "보충이 없으면 장기 실행 시 전 상품이 소진돼 checkout 여정이 영구 실패한다(2026-07-14 실측으로 확인된 버그)".
- `InventoryService.java:79-83` — `if (stock < quantity) throw new ServiceException(HttpStatus.CONFLICT, "Insufficient stock...")` = **409의 발원지**. `:75` `findByProductIdForUpdate`(PESSIMISTIC_WRITE).
- `product-service InventoryClient.java:33,48` — 하류 상태코드를 `HttpStatus.valueOf(...)`로 그대로 전파(409 보존).
- `order-service ProductClient.java:25` `@CircuitBreaker(productClient)`; `:35-42` 4xx → `ClientErrorException`(CB 실패 미집계); `:37` 주석 "4xx는 하류의 정상 업무 거절(재고 부족 409 등) — CB/Retry 실패 집계 대상에서 제외"; `:46-54` fallback도 4xx는 502로 안 바꾸고 그대로 rethrow. → **409는 CB를 열지 않고 5xx를 만들지 않는다(코드 확증).**
- loadgen 소비 확증: `commerce/loadgen/script.js:220-228`(checkout journey가 `/api/orders/checkout` 호출, 409를 pass로 접음), `surge.js:110-132`(surge-checkout이 동일 엔드포인트, `not 5xx`로 409를 pass로 접음). checkout 5-hop이 reserve를 실제 호출하므로 **부하가 진짜로 재고를 소비**.
- 주입면 후보: `commerce/k8s/22-inventory-service.yaml`(testbed-inventory, 재입고 관련 env 없음 → 기본값; k8s.env로 SPRING_APPLICATION_JSON 주입 가능 — F08-P 선례).

**정체성**: 이 409는 "정상 재고소진(biz-reject)"과 코드가 동일하다 — 배치정지(장애)인지 정상 소진인지 감별이 이 시나리오의 핵심. 게다가 4xx라 5xx·CB open 신호가 전무 → "조용한 기능마비".

---

## 2. 감별 설계 (골든 조건 ③)

| | must_support | must_rule_out (감별점) |
|---|---|---|
| **F23-R** | checkout **409** 비율↑; inventory stock이 회수 없이 **단조 감소→0**; **RESTOCK movement 유입=0**(배치 미동작 직접 신호); available=0 지속 | ① **정상 재고소진(biz-409) 아님** — 정상이면 10분마다 RESTOCK으로 stock이 **톱니파**(20 밑돌면 200으로 복원); 배치정지면 단조감소 flatline + RESTOCK row 0 · ② inventory **pod down/unavailable 아님** — pod ready, GET /api/inventory 200, reserve는 409(insufficient)이지 503/timeout 아님 · ③ **row-lock 경합(C2) 아님** — 그건 지연/timeout 증상, 409 아님(락은 stock>0에서 발생) · ④ **5xx·CB open 아님** — 409는 4xx, order productClient CB closed·checkout_5xx_rate 정상 |

**감별 축(2차 감별)**: 상태코드(409)와 dispatch 유무만으로는 정상 소진과 장애를 구분 불가. 갈림은 **stock 시계열 추세(톱니파 vs 단조감소) + RESTOCK 원장 유입율(>0 vs =0)**. 이는 F19-Q의 "ASSIGNED 추세 + DELIVERED 전이율"과 정확히 동형이다(회수/보충 배치의 동작 신호).

**오답 유도**: naive RCA는 checkout 실패를 order/checkout 결함으로 오판. 한 단계 나아가도 "정상 재고 소진(수요 급증)"으로 2차 오판. 정답 = inventory **재입고 배치 정지**. 5xx·CB open이 없어 표준 알람이 조용한 것이 오진을 심화.

---

## 3. 골든 4조건 자체점검표

| 조건 | F23-R |
|---|---|
| ① 코드/인프라 앵커 | ✅ ReconciliationBatch.java:46,74-91 + InventoryService.java:79-83(409) + ProductClient.java:25,37(4xx CB무시) 전부 read 실측. "배치정지→자원 미회수→4xx 마비" 메커니즘 코드에 박힘. (P1~P7 중 전용 슬롯 부재 — F19-Q와 같이 "스케줄러 정지→자원 미회수" 계열; 편의상 P3 자원고갈 계열로 라벨) |
| ② 정답(answer-key) | ✅ metadata root_cause{service=commerce-inventory, mechanism=ReconciliationBatch 재입고 스텝 정지, code_anchor} 코드 거동과 일치. infra_anchor에 interval-ms env 실재를 정직 표기(F19-Q와의 비대칭) |
| ③ 감별 가능 | 🟡→❌ 감별 **설계**는 완결(stock 톱니파 vs 단조감소, RESTOCK 원장 유입율, 5xx=0). 그러나 **결정 지표 query 전무**: (a) inventory stock 시계열 query 없음, (b) RESTOCK movement 유입율 query 없음, (c) checkout **409** rate 없음 — `checkout_5xx_rate`는 5xx 전용이고 script.js:228·surge.js:132가 409를 pass로 접음. biz-409↔fault-409 2차 감별은 상태코드로 불가 → 관측 사실상 불가 |
| ④ 주입 수단 실재 | 🟡 **F19-Q보다 나음**: 재입고 배치 주기가 `fixedDelayString="${inventory.reconciliation.interval-ms}"`로 **env 바인딩** → k8s.env로 interval-ms를 큰 값 주입 + rollout이면 배치가 사실상 정지(고갈은 loadgen의 **자연 소비** → §2-A 위장 아님). 단 **stopgap**: (a) fixedDelayString은 bean init 바인딩이라 **pod rollout 재시작 필요**(재시작 이벤트가 창을 오염, "돌던 배치 일시정지"가 아님), (b) 임의 극단 타이밍값은 헌장이 물러서는 F08-P류 env-왜곡 optics, (c) restockThreshold/Target 하향은 **진짜 §2-A 위장**(고갈을 인위 제조)이라 금지. 깨끗한 길 = 런타임 스케줄러 정지 훅(§4-B, F19-Q 공유) |
| **종합** | ①②✅ ③❌ ④🟡 → **draft / blocked** (주 사유=③ 관측 배선 부재; 부 사유=④ 깨끗한 런타임 훅 미구축) |

`readiness=draft`, `prerequisite_gate.state=blocked`, `live_allowed=false`. **③(관측 3종)이 하드 블로커** — ④에 restart 기반 우회로가 있어도 stock 추세·RESTOCK 원장·409 rate 없이는 biz-409와 fault-409를 분리할 수 없다.

---

## 4. 능력 갭 (승격 전 선행 — prerequisite_gate)

### 4-A. F23-R 관측 배선 (③ 하드 블로커 — 신규 query 3종)
1. **NEW `database.inventory_stock_level`**: commerce PG `inventory_schema.inventory`의 stock 분포(min/avg 또는 재고=0 상품 수). 감별 1차 신호(단조감소 vs 톱니파). `database.index_present`(selector `postgres.index.present`, db_host/port/name/schema/table 파라미터)와 동일 어댑터 패턴으로 신설. commerce PG는 30432로 접근 가능(F15-T1 선례).
2. **NEW `database.restock_movement_rate`**: `inventory_schema.inventory_movements WHERE movement_type='RESTOCK'`의 최근 창 유입 건수. **배치 동작 여부의 직접 신호**(배치정지=0). F19-Q의 `database.dispatch_delivered_rate`와 동형.
3. **NEW `loadgen.checkout_status_rate`** (409 버킷): 현 `loadgen.checkout_5xx_rate`(selector `checkout.5xx.rate`)는 **5xx 전용**이라 409를 구조적으로 못 봄. 게다가 `script.js:228`(`checkout 200/400/409` → pass)·`surge.js:132`(`not 5xx` → pass)가 409를 pass로 접어 emit 자체가 없음. → surge.js/script.js가 checkout 스텝 status-class(2xx/4xx/409/5xx) 비율을 emit하도록 보강 + query 등록. 이는 F16-H가 commerce에서 발견한 "checkout_5xx_rate가 401을 못 봄"(→ `write_step_status_rate` 신설) 및 F19-Q의 food-503 fold와 **동형**.

### 4-B. 스케줄러 정지 훅 — **F19-Q와 공유(훅 하나로 둘 다 해제)**
> 이 시나리오의 투자 효율의 핵심. F19-Q(dispatch 배차 만료 배치)와 F23-R(inventory 재입고 배치)은 **둘 다 `@Scheduled` 배치의 "정지"가 결함**이고, 손쉬운 env 대안이 전부 §2-A 경계에 걸린다. **하나의 훅 요구사항으로 통합**한다.

**공유 갭의 성격**:
| | F19-Q (food dispatch) | F23-R (commerce inventory) |
|---|---|---|
| 대상 스케줄러 | `DispatchService.deliverExpiredDispatches` `@Scheduled(fixedDelay=30000)` **하드코딩** | `ReconciliationBatch.run` `@Scheduled(fixedDelayString="${inventory.reconciliation.interval-ms:600000}")` **env 바인딩** |
| env 우회로 | **없음**(주기 하드코딩) → ④ ❌ | interval-ms 큰 값 + rollout → ④ 🟡 (restart 기반 stopgap) |
| 금지된 §2-A 위장 | `DISPATCH_MAX_CAPACITY` 하향(biz-reject 임계 제조) | `restock.threshold/target` 하향(고갈 인위 제조) |
| 결함의 참값 | 배치 정지 = ASSIGNED 미회수 | 배치 정지 = stock 미보충 |

**통합 훅 요구사항(권고 = Option B)**:
- **Option A (앱측 런타임 플래그)**: 각 배치 진입부에 런타임 토글 가드 신설 — `inventory.reconciliation.enabled` / `dispatch.expiry-batch.enabled`(예: `@Scheduled` 메서드 첫 줄에서 `AtomicBoolean`/`@ConditionalOnProperty`·actuator refresh로 검사). 장점: 의미가 정확("이 배치만 정지"), threshold/target 불간섭. 단점: 앱 코드 변경 + actuator 미노출 시 여전히 env→restart.
- **Option B (전용 injector, 권고)**: 현재 `business.fault`(profiles.json:2523, **`live_supported=false` stub**, allowed_locations에 commerce/food-namespace 이미 포함)를 **실 스케줄러 정지 injector로 승격**. 계약: `parameter_contract.allowed_scenarios += [F19-Q, F23-R]`, `scenario_parameters`를 `{namespace, deployment, container, scheduler_method}`로 키잉(F19-Q: rca-testbed-food/testbed-dispatch/deliverExpiredDispatches, F23-R: rca-testbed-commerce/testbed-inventory/ReconciliationBatch.run), `live_supported`는 런타임 훅이 실재할 때만 true로 전환. **하나의 injector 계약이 두 시나리오를 서비스** = 요구된 투자 효율.
- **공통 요건**: (1) **pod 재시작 없이** 지정 `@Scheduled`만 정지(F23-R의 interval-ms restart 오염 제거, F19-Q는 애초에 대안 없음), (2) 업무-용량/임계 knob 불간섭(§2-A 위장 회피), (3) cleanup 시 배치 재개 → recovery gate(RESTOCK/DELIVERED 유입 재개)로 검증.
- **F23-R 임시 대안(승격 전 실험용, 비권고)**: k8s.env로 `SPRING_APPLICATION_JSON={"inventory":{"reconciliation":{"interval-ms":999999999}}}` + rollout. 고갈은 자연 소비라 §2-A 위장은 아니나 restart 이벤트·극단값 optics 때문에 golden 승격로가 아니라 훅 개발 전 메커니즘 검증용으로만. (F19-Q에는 이 대안조차 없음.)

---

## 5. 헌장 부합성 평가 (한 문단)

F23-R은 fault-surface §C10과 ReconciliationBatch 파일 주석에 실측 기록된 "재입고 배치 자립성 버그"를 실코드 file:line으로 앵커링해 ①②를 채웠고(ReconciliationBatch:46/74-91, InventoryService:79-83, ProductClient:25/37 전부 read 확인), 특히 **5xx도 CB open도 없는 4xx 조용한 기능마비**를 order productClient의 "4xx는 CB 실패 미집계"(ProductClient.java:37) 코드로 확증해 감별의 뼈대를 코드에서 끌어냈다 — 이는 F19-Q(biz-503↔fault-503)의 commerce 동형(biz-409↔fault-409)이다. 가장 정직해야 할 지점은 ④인데, F19-Q sheet가 "env 하향은 전부 부적격"이라 결론지은 것과 달리 **F23-R은 재입고 배치 주기가 `fixedDelayString`으로 env 바인딩되어 있어 F19-Q에 없는 우회로가 실재**한다(k8s.env로 interval-ms를 크게 + rollout, 고갈은 loadgen의 자연 소비라 §2-A 위장이 아님) — 이 비대칭을 감추지 않고 ④를 🟡(❌ 아님)으로 표기한다. 다만 이 우회로는 (a) fixedDelayString이 bean-init 바인딩이라 pod rollout 재시작을 강요해 "돌던 배치의 일시정지"가 아닌 "재시작 후 사실상 미동작"이고 재시작 이벤트가 창을 오염시키며, (b) 임의 극단 타이밍값은 헌장이 §3.2에서 물러서기로 한 F08-P류 env-왜곡 optics에 걸리고, (c) restock threshold/target을 낮추는 진짜 §2-A 위장과 반드시 구분돼야 하므로, golden 승격로가 아니라 훅 개발 전 검증용에 머문다. 결국 F23-R을 blocked로 묶는 하드 사유는 ④가 아니라 ③이다: inventory stock 시계열·RESTOCK 원장 유입율·checkout 409 rate 세 관측이 전부 부재하고(commerce 관측 표면도 checkout_5xx_rate가 5xx 전용이라 409에 구조적으로 눈멀어 있으며 두 loadgen 스크립트가 409를 pass로 접는다), 이것 없이는 정상 재고소진의 톱니파와 배치정지의 단조감소를 분리할 수 없다. 결론: F23-R은 관측 배선 3종(§4-A)을 신설하면 감별이 서고, 스케줄러 정지 훅(§4-B)은 **F19-Q와 단일 훅으로 통합**해 별도 인프라 트랙(헌장 §5)으로 올리는 것이 정직하고 효율적이다 — 4조건 루브릭이 없었다면 F23-R은 "interval-ms 크게 줘서 409 재현"이라는 그럴듯한 얕은 manifest로 승격됐을 것이다.
