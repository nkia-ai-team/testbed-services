# 설계 시트 — F19-P / F19-Q / F19-S: food-delivery 실코드 결함 3종 (Class A)

- **백로그 근거**: 헌장 §4 #5 "food 실코드 결함 3종 (롱tx 풀고갈·배차정지·PG연쇄) / food / A / P1·P3·P4 / food 도메인 code-anchor 빈약".
- **코드 근거**: fault-surface-food-delivery.md C1/C2/C3 + food-delivery 실코드 file:line 실측(아래 앵커 전부 grep 확인).
- **공통 함정(반영됨)**: food는 429 없음 → 용량초과=**503**. biz-reject 503과 장애 503 감별이 F19-Q의 정체성. 증상은 항상 orchestrator **order**로 수렴하나 원인은 downstream — must_rule_out으로 order 자체 정상을 지목.

---

## 1. 인과 사슬 (3개, 코드 실측)

### F19-P (P1) — order 롱-트랜잭션 커넥션풀 고갈
```
mock.expectation: food PG mock POST /pay → delay 8s (payment read-timeout 10s '미만')
  → payment.processPayment이 지연된 200 반환(502 아님)
  → order.createOrder(@Transactional, OrderService.java:57)가 DB 커넥션 잡은 채
     restaurant(:60,73)·dispatch(:96,145)·payment(:154) 순차 호출 → 커넥션 보유 = Σlatency
  → order Hikari pool(15) 고갈 → 신규 createOrder가 connection-timeout 3000ms 초과
  → order-service 5xx/timeout   ← 증상=order, 원인=downstream 지연
```
앵커: `OrderService.java:57`(@Transactional, timeout 미설정), `:60/:73/:96/:145/:154`(트랜잭션 내부 순차 원격호출), order `application.yml`(Hikari 15 / connection-timeout 3000ms / payment read-timeout 10s), `k8s/30-external-pg-mock.yaml`(주입면). **핵심: delay를 read-timeout 미만으로 둬 payment는 200을 내고, order 자신의 커넥션 고갈만으로 5xx** — 하류가 성공인데 상류가 죽는 오진 구조.

### F19-Q (P3) — 배차 만료 배치 정지 → capacity 소진 → 503
```
[배치 정지: deliverExpiredDispatches @Scheduled 30s 멈춤]  ← 주입수단 미존재(§4)
  → ETA 지난 ASSIGNED가 DELIVERED로 회수되지 않고 단조 누적
  → countByStatus('ASSIGNED') >= maxCapacity(2000) (DispatchService.java:60-63)
  → dispatchCourier 신규 배차 503 'Courier pool exhausted'
  → order.createOrder fan-out(:96,145)이 503 수신 → 'No courier available' 주문 거절
```
앵커: `DispatchService.java:119-127`(deliverExpiredDispatches @Scheduled fixedDelay=30000, findExpiredAssigned→DELIVERED = capacity 회수), `:60-63`(countByStatus>=maxCapacity→503), `application.yml:83`(max-capacity ${DISPATCH_MAX_CAPACITY:2000}), `OrderService.java:96,145`. **정체성: 이 503은 정상 용량 포화(biz-reject)와 코드가 동일** — 배치정지(장애)인지 정상포화인지 감별이 핵심.

### F19-S (P4) — PG 지연/다운 → payment 502 → pg CB open → order 502 연쇄
```
mock.expectation: food PG mock POST /pay → delay 30s (read-timeout 10s '초과') 또는 status 500
  → PgApiClient.pay timeout/에러(PgApiClient.java:34,42)
  → @Retry(pg) max2 백오프 재시도 증폭(:33) → ServiceException(502 BAD_GATEWAY, :54)
  → 실패율 50%↑ → @CircuitBreaker(pg) open(:32) → payFallback 즉시 502(:65)
  → order.paymentClient.processPayment(:154)이 502 수신 → 주문 실패 전파
```
앵커: `PgApiClient.java:32-34`(@CircuitBreaker(pg)+@Retry(pg) pay), `:42`(/pay), `:54/:65`(502 BAD_GATEWAY / fallback), payment `application.yml`(resilience4j pg window10/min5/50%/open5s; retry pg max2·300ms; read-timeout 10s), `OrderService.java:154`. **F19-P와 같은 주입면이나 delay가 read-timeout을 '초과'** → 흡수(P) 대신 timeout→502→CB open(S).

---

## 2. 감별 설계 (골든 조건 ③)

| | must_support | must_rule_out (감별점) |
|---|---|---|
| **F19-P** | order create 5xx/timeout↑; order Hikari pending>0(active≈15); payment 502 낮음 | payment 502 아님(<0.05) → S와 구분 · 5xx가 4xx/503 아님(커넥션풀발) → Q와 구분 · 429 아님(food=503) |
| **F19-Q** | order create **503**↑; ASSIGNED 카운트 maxCapacity에서 정체(단조증가); DELIVERED 전이율=0 | 정상 용량포화 아님(배치회수 살아있으면 ASSIGNED 톱니파) · dispatch down/unavailable 아님(pod ready, capacity=200) · 429 아님 |
| **F19-S** | payment **502**↑; pg CB **open**; order create 502↑ + retry 증폭 | order 풀고갈발 5xx 아님(payment가 명시적 502, Hikari pending 정상) → P와 구분 · 502이지 503 아님 → Q와 구분 · 429 아님 |

**3자 감별 축**: 상태코드/신호로 갈림 — P=order 5xx/timeout+Hikari pending, Q=order **503**+ASSIGNED 단조증가+DELIVERED전이=0, S=payment **502**+pg CB open. P↔S는 **PG mock delay가 read-timeout(10s) 미만이냐 초과냐**로 갈리는 near-twin(주입면 동일). Q는 유일하게 503이며 "biz-reject 503 ↔ fault 503"의 2차 감별을 요구(상태코드로 불가, ASSIGNED 추세·DELIVERED 전이율·응답 메시지로만).

**오답 유도**: 세 시나리오 모두 증상이 order로 수렴 → naive RCA가 order를 root로 오판. 정답은 각각 downstream 지연(P)/dispatch 배치(Q)/외부 PG(S). Q는 한 단계 더 — '정상 용량 포화'로 2차 오판 유도.

---

## 3. 골든 4조건 자체점검표

| 조건 | F19-P | F19-Q | F19-S |
|---|---|---|---|
| ① 코드/인프라 앵커 | ✅ OrderService.java:57 롱-tx + fan-out :60~154, Hikari 15/timeout 3s 실측. P1 정확 매핑 | ✅ DispatchService.java:60-63,119-127 + max-capacity:83 실측. P3(capacity) 매핑 | ✅ PgApiClient.java:32-65 CB/Retry/502 실측 + resilience4j pg. P4 매핑 |
| ② 정답(answer-key) | ✅ metadata root_cause{service=food-order, mechanism, code_anchor} 코드거동 일치 | ✅ metadata root_cause{service=food-dispatch scheduler} 작성. infra_anchor="없음(순수 스케줄러)" 정직 표기 | ✅ metadata root_cause{service=food-payment PG hop} 작성, CB거동 일치 |
| ③ 감별 가능 | 🟡 감별 *설계* 완결(P vs S vs Q). 그러나 관측 query 미존재: food create-status rate·Hikari pending 미배선 | 🟡→❌ 감별 설계는 명확(ASSIGNED 추세·DELIVERED 전이율). 그러나 **biz-503↔fault-503 2차 감별은 상태코드로 불가**·surge.js가 503을 pass로 접음·해당 query 전무 → 관측 사실상 불가 | 🟡 감별 설계 완결(payment 502·pg CB open이 P와 결정적 구분). 그러나 pg CB state query·food create-status rate 미존재 |
| ④ 주입 수단 실재 | 🟡 mock.expectation 메커니즘 실재·commerce서 live검증. 단 executor가 commerce ns 하드코딩(:54) → food 허용 1줄 일반화 + 계약 allowlist | ❌ **배치 정지 injector 미존재**. @Scheduled 외부 제어점 없음(P5 갭 동종). env 하향은 §2-A 위장·자기모순, pod교란은 메커니즘 불일치. 제어 훅 신설 필요 | 🟡 F19-P와 동일 injector(mock.expectation food) → 같은 1줄 일반화 + 계약. delay 파라미터만 상이(30s) |
| **종합** | ①②✅ ③④🟡 → **draft/blocked** (배선만) | ①②✅ ③🟡→❌ ④❌ → **draft/blocked** (능력갭, 배선 아님) | ①②✅ ③④🟡 → **draft/blocked** (배선만) |

세 개 모두 `readiness=draft`, `prerequisite_gate.state=blocked`, `live_allowed=false`. **P·S는 배선(wiring)만으로 승격 가능**, **Q는 진짜 능력갭(injector 신설)** 이 선행돼야 하는 근본적으로 다른 부류.

---

## 4. 능력 갭 (승격 전 선행 — prerequisite_gate)

### 4-A. 공통 (P·S — 배선, 신규 executor/코드 없음)
1. **mock.expectation executor의 commerce 하드코딩** (`profiles/mock_expectation_executor.py:54`):
   `location.namespace != "rca-testbed-commerce"` 이면 reject. **그러나 SCRIPT 본문(:76 `kubectl -n "$ns"`)은 이미 `$ns` 파라미터화** — food PG mock도 동일 mockserver라 **guard 1줄을 `{commerce, food}` 허용으로 일반화**하면 됨. 신규 executor 아님. food mock 확인: `food-delivery/k8s/30-external-pg-mock.yaml`(rca-testbed-food, testbed-external-pg-mock, mockserver:1080, POST /pay, failure_surface: external-timeout).
2. **mock.expectation parameter_contract** (`registry/profiles.json`): allowed_scenarios에 F19-P/F19-S 추가 + scenario_parameters(path=`/pay`, F19-P: mode=delay/8s, F19-S: mode=delay/30s 또는 status/500) + allowed_locations에 `food-mock`→rca-testbed-food 매핑.
3. **load.north_south contract**: allowed_scenarios·tag_pattern에 F19-P/F19-S 추가 + scenario_parameters(entry `http://192.168.122.77:30181`, script `/opt/loadgen/food-delivery/surge.js`, baseline_unit `loadgen-food` — F02-P 선례 그대로).
4. **NEW query_id: `loadgen.food_create_status_rate`** (진짜 병목): 현재 loadgen query는 `loadgen.achieved_rps`·`loadgen.checkout_5xx_rate`(commerce 전용) **2개뿐**. food surge.js `orderJourneyBounded`의 create 스텝(`surge.js:132-139`)은 **200/400/503을 전부 pass로 접어** 상태별 비율을 emit하지 않음. surge.js 요약에 create 스텝 status-class(2xx/4xx/502/503) 비율 emit + query 등록 필요. (pilot F16-H가 commerce에서 발견한 "checkout_5xx_rate가 401을 못 봄"과 동형 — food엔 아예 create query가 0.)
5. **NEW query_id: `prometheus.hikari_pending_connections`** (F19-P): food-order Hikari active/pending 게이지. OTel javaagent가 `hikaricp_connections_pending` 노출하는지 확인 후 등록.
6. **NEW query_id: `prometheus.circuitbreaker_open`** (F19-S): resilience4j cb=pg state 게이지(food-payment). pilot의 `gateway_circuitbreaker_open` 갭과 동일 계열.

### 4-B. F19-Q 전용 (진짜 능력갭 — injector 신설)
7. **배차 만료 스케줄러 정지 제어점 부재** (근본): `deliverExpiredDispatches`가 앱 내부 `@Scheduled`(DispatchService.java:119)라 외부 제어 훅이 없음 — **헌장 §5 P5(outbox relay @Scheduled) 갭과 동종**. 재현 옵션 전부 부적격:
   - `DISPATCH_MAX_CAPACITY` 하향(k8s.env): 정상 용량 축소 = 이 시나리오가 감별해야 할 **biz-reject 자체를 만드는 자기모순** + 헌장 §2-A "Class A 위장 금지" 위배.
   - pod 교란/GC stall: dispatch 전체 다운 → 'unavailable' 503(성격 다름), ASSIGNED 누적 메커니즘 미재현.
   - → **스케줄러 정지 전용 injector**(business.fault 실장화 또는 앱에 `EXPIRY_BATCH_ENABLED` 류 제어 플래그 신설) 필요.
8. **NEW query: `database.dispatch_status_count`** (ASSIGNED 시계열, 단조증가 vs 톱니파) + **`database.dispatch_delivered_rate`**(DELIVERED 전이율=배치 동작 신호). database adapter에 등록 필요.
9. **503 biz vs fault 2차 감별 관측** (가장 깊은 갭): 상태코드(503)만으론 정상포화/장애 구분 불가. 응답 메시지('Courier pool exhausted' vs dispatch 'unavailable')·ASSIGNED 추세·DELIVERED 전이율의 결합 관측이 필요. 현 관측 표면엔 없음.

---

## 5. 헌장 부합성 평가 (한 문단)

세 시나리오는 헌장 §4 #5의 "food code-anchor 빈약"을 실코드 file:line으로 메꿨고(①② 3개 모두 ✅, PgApiClient/DispatchService/OrderService 전부 grep 실측), 헌장이 요구한 "깊이"를 P1·P3·P4 세 패턴에 정확히 매핑했다. 가장 값진 발견은 **동일 백로그 항목 안에 성격이 근본적으로 다른 두 부류가 섞여 있었다는 것**이다: F19-P·F19-S는 injector(mock.expectation)가 commerce에서 live-검증된 실물이라 executor guard 1줄 + 계약 allowlist라는 순수 **배선**으로 승격되지만, F19-Q는 배차 만료 배치가 앱 내부 `@Scheduled`라 외부 제어점이 없어 헌장 §5 P5 갭과 동종의 **진짜 능력갭**이며, 유일한 손쉬운 대안(MAX_CAPACITY 하향)은 헌장 §2-A "Class A 위장 금지"에 정면으로 걸리고 이 시나리오의 정체성인 biz-503↔fault-503 감별 자체를 무너뜨리는 자기모순이다 — 4조건 루브릭이 없었다면 F19-Q는 "env 낮춰 503 재현"이라는 그럴듯한 얕은 manifest로 승격됐을 것이다. 또한 food 관측 표면이 commerce보다 더 척박하다는 것도 드러났다: loadgen query가 사실상 commerce checkout 전용 1개뿐이고 food surge.js는 create의 503을 pass로 접어버려, 세 시나리오 모두 ③ 감별이 "설계는 완결이나 관측 배선 부재"로 🟡에 걸린다(Q는 2차 감별까지 필요해 사실상 ❌). 결론: P·S는 배선 백로그로 즉시 올릴 값어치가 있고, Q는 스케줄러 제어 훅이라는 인프라 갭을 별도 트랙(헌장 §5)으로 올려야 하는, 정직하게 blocked인 설계다.
