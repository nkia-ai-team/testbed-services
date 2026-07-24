# 설계 시트 — F20-R / F20-P / F20-Q: 슬로우쿼리 패밀리 3종 (전부 Class A, 신규 패턴 축)

- **백로그 근거**: 헌장 §3.2 "앵커 얕음: F02-R(인덱스 미사용)" 계열을 슬로우쿼리라는 **독립 패턴 축**으로 승격. 무거운 조회 엔드포인트가 코드/스키마 결함(인덱스 부재·함수 래핑·무제한 반환)으로 자원(공유 DB CPU / 자체 커넥션풀 / 자체 힙)을 잠식하는 계열이다. 헌장 §2-B의 P1~P7 결함 사전에는 슬로우쿼리 패턴이 없다 — 이 3종이 **신규 패턴 P8(슬로우쿼리/인덱스 설계 결함)** 후보다.
- **코드 근거**: fault-surface-commerce.md §C8, fault-surface-core-banking.md §FS-7, fault-surface-food-delivery.md §C6 + 3도메인 실코드 file:line 실측(아래 앵커 전부 grep 확인, 드리프트 정정 반영).
- **공통 정체성(감별 축)**: 세 시나리오 모두 "무거운 읽기 쿼리"지만 **잠식하는 자원이 다르다** — R=공유 PG CPU(교차 스키마 오염), P=자체 커넥션풀 점유(이체 경합 악화), Q=자체 JVM 힙(무제한 반환). 이 자원 분기가 패밀리 내부 3자 감별의 핵심이며, 세 개 모두 증상이 "느려지는 조회"로 수렴해 naive RCA를 오도한다.
- **공통 주입 성격(정직 평가)**: 이 계열의 주입은 별도 executor(mock/db-lock/k8s-env)가 아니라 **무거운 조회 엔드포인트를 반복 호출하는 부하 그 자체**다. 주입 메커니즘(k6 GET)은 100% 실재하나, **현재 어떤 loadgen 스크립트도 이 엔드포인트들을 치지 않는다**(아래 §4-④ 실측). 따라서 세 개 모두 진짜 injector 능력갭이 아니라 **loadgen 스크립트 신설 + 계약 등록이라는 배선 갭**에 걸린다 — F19-Q(스케줄러 정지 제어점 부재)와 근본적으로 다른, 더 가벼운 부류.

---

## 1. 인과 사슬 (3개, 코드 실측)

### F20-R (commerce C8) — order 통계/검색 슬로우쿼리 → 공유 PG CPU 오염 → 타 스키마 교차 지연
```
loadgen: GET /api/orders/stats/daily?days=90  (+ GET /api/orders?status=&from=&to= 무제한 검색) 반복 호출
  → OrderRepository.dailyStats(:32-35): JPQL FUNCTION('DATE', o.createdAt) GROUP BY → orders(50k) 풀스캔
     (orders 인덱스는 idx_orders_user(user_id) 하나뿐 — created_at/status 인덱스 없음, init-schemas.sql:215)
  → OrderRepository.search(:19-28): (:x IS NULL OR ...) OR-null 술어 → 인덱스 미선택 풀스캔
  → order-service 응답 지연 상승 + 단일 PostgreSQL 인스턴스 CPU 상승
  → 같은 PG 인스턴스의 payment_schema/inventory_schema 쿼리까지 동반 지연  ← 공유자원 오염 = 정체성
  → payment/inventory 서비스는 5xx 없이 '느려짐'만 — 원인이 order로 오인
```
앵커: `OrderRepository.java:19-28`(search OR-null 풀스캔), `:32-35`(dailyStats FUNCTION('DATE') GROUP BY, created_at 인덱스 없음), `OrderController.java:45-61`(무제한 search, `:59` Pageable.unpaged()), `:75-79`(stats/daily), `db/init-schemas.sql:215`(idx_orders_user 유일 — created_at/status 인덱스 부재), `:256`(payment_schema.payments 동일 PG 인스턴스). **핵심: 인덱스가 아예 없는(드롭이 아니라 설계상 부재) 풀스캔이 단일 공유 PG의 CPU를 잠식해 타 스키마를 오염 — F02-R(인덱스 드롭)과 달리 상시 부재 구조.**

### F20-P (banking FS-7) — transfer stats/daily trunc 풀스캔 → transfer 커넥션 점유 → 이체 경합 악화
```
loadgen: GET /api/transfers/stats/daily?days=90 반복 호출
  → TransferService.dailyStats(:129-131) → TransferRepository.dailyStatsSince(:33-36):
     native "select trunc(created_at) ... group by trunc(created_at)"
  → transfers에 idx_transfers_created(created_at)가 존재(init.sql:46)하나 trunc(created_at) 함수 래핑으로 인덱스 무력화 → 풀스캔
  → 풀스캔이 transfer Hikari(max=15) 커넥션을 스캔 시간만큼 점유
  → 동시 이체 execute()의 FOR UPDATE(무한 대기, AccountRepository.java:18-20)와 커넥션/CPU 경합 악화
  → transfer 지연 → account read-timeout(15s) 초과 → account 502, retry x3 부하 증폭 → api 502
```
앵커: `TransferRepository.java:33-36`(native trunc(created_at) group by 풀스캔), `TransferService.java:129-131`(dailyStats), `TransferController.java:66-69`(GET /stats/daily), `db/init.sql:46`(idx_transfers_created **존재** — 그러나 trunc()로 무력화), transfer `application.yml`(Hikari max=15, connection-timeout 3000ms). **정체성: R과 달리 인덱스가 존재하나 함수 래핑으로 무력화 — "인덱스가 있는데 왜 느린가"라는 한 단계 깊은 오진 유도. 잠식 자원은 공유DB가 아니라 transfer 자체의 커넥션풀(이체 경합).**

### F20-Q (food C6) — 무제한 조회 + stats filesort → order 힙·DB 압박
```
loadgen: GET /api/orders (page/size 없이) + GET /api/orders/stats/daily?days=90 반복 호출
  → OrderController.getAllOrders(:41-43): page/size 미지정 시 Pageable.unpaged() → 무제한 반환
  → OrderRepository.search(:22-33): OR-null 술어 풀스캔 + 전 행을 힙에 적재
  → OrderRepository.aggregateDailyStats(:36-40): native "GROUP BY DATE(created_at)" → 컬럼 함수로 인덱스 무력화 + filesort
  → order 힙 사용량 증가(limit 1Gi, k8s/20-order-deploy.yaml:91) → GC 압박/OOM 위험 + MySQL slow query
  → order 지연 상승; C1(createOrder 롱-tx 커넥션 압박)과 상승작용 가능
```
앵커: `OrderController.java:41-43`(page/size 없으면 Pageable.unpaged() 무제한), `OrderRepository.java:22-33`(search OR-null 풀스캔), `:36-40`(aggregateDailyStats native GROUP BY DATE(created_at) filesort), `k8s/20-order-deploy.yaml:88,91`(order request 512Mi / limit **1Gi**). **정체성: 잠식 자원은 order JVM 힙(무제한 결과셋 적재) — R(공유DB)·P(커넥션풀)과 다른 세 번째 자원. 단일 MySQL이라 교차 스키마 오염 없음(R과의 결정적 구분).**

---

## 2. 감별 설계 (골든 조건 ③)

| | must_support | must_rule_out (감별점) |
|---|---|---|
| **F20-R** | order p95 지연↑; **payment/inventory p95도 동반↑(교차 스키마)**; 공유 PG CPU↑; payment/inventory **에러율은 낮음**(<0.05, 느려질 뿐 5xx 아님) | F02-R 인덱스 드롭 아님(인덱스가 상시 부재 — index_present는 원래 false) · 특정 서비스 장애 아님(payment/inventory pod ready, 5xx 없음) · order 커넥션풀 고갈발 5xx 아님(지연이지 5xx 아님) |
| **F20-P** | transfer p95 지연↑; transfer Hikari pending>0(커넥션 점유); account/api **502**↑ + retry 증폭; 이체 성공률↓ | 인덱스 드롭 아님(idx_transfers_created 존재 — **함수 래핑**이 원인) · FS-1 hot-account row-lock 아님(특정 계좌 편중 없이 전체 지연) · Oracle 전체 다운 아님(account/ledger 조회는 상대적으로 견딤, transfer만 집중 열화) |
| **F20-Q** | order p95 지연↑; **order 컨테이너 메모리가 1Gi limit로 상승**(무제한 반환 적재); order restart/OOMKilled 위험; MySQL slow query | F19-P 롱-tx 커넥션풀 고갈 아님(PG mock 지연 없음, payment 502<0.05 — 힙 압박이지 커넥션 timeout 아님) · F19-Q 배차 503 아님(상태코드 지연/200이지 503 아님) · 단일 MySQL이라 교차 스키마 오염 없음(R과 구분) · 429 아님 |

**3자 감별 축(패밀리 정체성 — 잠식 자원)**:
- **F20-R = 공유 PG CPU 오염**: order 슬로우쿼리가 단일 PG 인스턴스 CPU를 먹어 **다른 스키마(payment/inventory) 조회까지 동반 지연**. 결정 신호 = "건강한(5xx 없는) 타 서비스가 함께 느려짐". 인덱스는 **아예 없음**(idx_orders_user 유일).
- **F20-P = 자체 커넥션풀 점유**: transfer 풀스캔이 transfer Hikari(15)를 붙잡아 **자기 도메인 이체 경합**만 악화, 증상은 banking 체인(account/api 502)에 국한. 인덱스는 **존재하나 trunc()로 무력화**.
- **F20-Q = 자체 JVM 힙**: 무제한 반환이 order 힙을 밀어올려 **메모리/OOM 압박**. 단일 MySQL이라 교차 오염 없음. 인덱스는 GROUP BY DATE() 함수로 **무력화**.

**오답 유도**: 세 개 모두 "조회가 느려짐"으로 수렴 → naive RCA가 느린 서비스(order/transfer)를 root로 지목하기 쉬우나, 정답은 각각 (R) 공유 PG를 오염시킨 order 쿼리설계·(P) 인덱스를 무력화한 함수 래핑·(Q) 페이지네이션 부재다. R은 한 단계 더 — "왜 관계없는 payment까지 느린가"(공유 인스턴스)를 풀어야 하고, P는 "인덱스가 있는데 왜 느린가"(함수 래핑)를 풀어야 한다.

---

## 3. 골든 4조건 자체점검표

| 조건 | F20-R | F20-P | F20-Q |
|---|---|---|---|
| ① 코드/인프라 앵커 | ✅ OrderRepository.java:19-35 + init-schemas.sql:215(인덱스 부재) + :256(공유 PG) 실측. 신규 패턴 P8(슬로우쿼리) | ✅ TransferRepository.java:33-36(trunc 풀스캔) + init.sql:46(인덱스 존재/무력화) 실측. P8 | ✅ OrderController.java:41-43(unpaged) + OrderRepository.java:22-40 + k8s/20:91(1Gi) 실측. P8 |
| ② 정답(answer-key) | ✅ metadata root_cause{service=commerce-order 쿼리/인덱스, infra=공유 PG 인스턴스} 코드거동 일치 | ✅ metadata root_cause{service=core-banking-transfer 쿼리, mechanism=trunc 인덱스 무력화} 작성 | ✅ metadata root_cause{service=food-order 쿼리, mechanism=unpaged+DATE() filesort} 작성 |
| ③ 감별 가능 | 🟡 감별 설계 완결(교차 스키마 오염). 결정 ground-truth인 **공유 PG CPU/DB-side 쿼리시간 query 미존재**(node_cpu는 노드레벨 프록시뿐). 교차지연은 apm_service_p95(order+payment) + error_rate로 **간접** 관측 가능 | 🟡 감별 설계 완결(커넥션 점유·502). **transfer Hikari pending query 미존재**(F19-P와 동일 갭) + DB-side 쿼리시간 미존재. account/api 502는 apm_service_error_rate로 관측 가능 | 🟡 감별 설계 완결(힙 압박). **힙/메모리는 kubernetes.container_memory_current_bytes + limit로 관측 가능(3종 중 최선)**. 단 쿼리 귀속(어느 쿼리)·DB-side filesort query 미존재 |
| ④ 주입 수단 실재 | 🟡 메커니즘(k6 GET stats/unpaged) 실재. **그러나 commerce surge.js는 products/cart/checkout만 침 — stats/daily·무제한 search를 치는 스크립트 없음.** NEW loadgen 스크립트 + load.north_south 계약 등록 필요(배선) | 🟡 동일 — banking surge.js는 balance/history/list/transfer만 침. /transfers/stats/daily 스크립트 없음. NEW 스크립트 + 계약(배선) | 🟡 동일 — food surge.js/order-surge.js는 restaurants·order create·deliveries만 침. GET /api/orders unpaged·stats/daily 스크립트 없음. NEW 스크립트 + 계약(배선) |
| **종합** | ①②✅ ③④🟡 → **draft/blocked** (배선: 스크립트+DB CPU query) | ①②✅ ③④🟡 → **draft/blocked** (배선: 스크립트+Hikari/DB query) | ①②✅ ③④🟡 → **draft/blocked** (배선: 스크립트+DB query; 힙은 기존 query로 관측 가능) |

세 개 모두 `readiness=draft`, `prerequisite_gate.state=blocked`, `live_allowed=false`. **세 개 전부 배선(wiring)만으로 승격 가능** — F19-Q 같은 진짜 injector 능력갭(제어점 부재)이 없다. 잔여 갭은 (1) 슬로우쿼리 endpoint를 치는 loadgen 스크립트, (2) DB-side CPU/쿼리시간 query, (3) Hikari pending query(P) 뿐이며 전부 신규 executor/제어점 신설이 아니라 스크립트 작성 + registry 등록이다.

---

## 4. 능력 갭 (승격 전 선행 — prerequisite_gate)

### 4-A. 공통 ④ — loadgen 스크립트 배선 (신규 executor 아님, 스크립트 신설)
현재 어떤 surge 스크립트도 슬로우쿼리 endpoint를 치지 않는다(실측):
- commerce surge.js: `/api/products`·`/api/carts`·`/api/orders/checkout`·`/api/users/login`만. `stats/daily`·무제한 search **없음**.
- banking surge.js: `/api/accounts/{id}`·`/api/transfers?fromAccount=`·`/api/accounts?status`·`POST /api/transfers`만. `/api/transfers/stats/daily` **없음**.
- food surge.js/order-surge.js: `/api/restaurants*`·`POST /api/orders`(create)·`/api/deliveries*`만. `GET /api/orders`(unpaged)·`/api/orders/stats/daily` **없음**.

필요 배선:
1. **NEW k6 스크립트** 도메인별 `slowquery.js`(또는 기존 surge.js에 slowquery 여정 추가): 주 여정 = `GET /api/orders/stats/daily?days=90`(R/Q)·`GET /api/transfers/stats/daily?days=90`(P)·`GET /api/orders`(page/size 없이, Q)·무제한 search(R). status·latency를 read-step으로 버킷팅 emit.
2. **load.north_south parameter_contract 등록**: `allowed_scenarios`에 F20-R/F20-P/F20-Q 추가, `tag_pattern` 정규식 확장, `allowed_script_paths`에 신규 스크립트 3종, `scenario_parameters`(F20-R entry 30080/loadgen-commerce, F20-P entry 30082/loadgen-banking, F20-Q entry 30181/loadgen-food; F02-P·F07-H 선례 그대로).
   - banking·food는 domain_profiles/allowed_entry_urls/baseline_unit이 이미 등록돼 있어(30082·30181) entry 재사용 가능.
3. **NEW query_id: read-step status/latency rate** — surge.js가 slowquery 스텝의 상태/지연을 emit해야 성공/회복 판정 가능. 기존 loadgen query(`achieved_rps`·`checkout_5xx_rate`·`read_step_status_rate` 등)에 slowquery 스텝 매핑 추가.

### 4-B. 관측 갭 ③ — DB-side / 자원 query (신규 registry 등록)
4. **NEW query: DB CPU / DB-side 슬로우쿼리 시간**(가장 깊은 갭, R의 결정 ground-truth): 단일 PG(commerce)·Oracle(banking)·MySQL(food)의 인스턴스 CPU 또는 active-query 시간. 현 registry에는 `prometheus.node_cpu_utilization`(노드레벨 프록시 — PG pod가 단독 노드면 근사 가능하나 노이즈)과 `database.index_present`(boolean)뿐. **교차 스키마 오염(R)을 직접 증명할 DB-process 지표가 없다** — apm_service_p95(order+payment 동반 상승) + payment error_rate 낮음으로 **간접** 감별해야 함.
5. **NEW query: `prometheus.hikari_pending_connections`**(F20-P transfer, F20-R order 보조) — transfer/order Hikari active/pending 게이지. F19-P가 이미 올린 동일 갭. OTel javaagent `hikaricp_connections_pending` 노출 확인 후 등록.
6. **관측 가능(기존 query로 충족)**: 
   - `prometheus.apm_service_p95`(service_name) — order/transfer 지연 + R의 교차 스키마(payment/inventory) 지연 동반 상승. **R의 간접 감별 핵심.**
   - `prometheus.apm_service_error_rate`(service_name) — P의 account/api 502, R의 payment "5xx 아님" 확인.
   - `kubernetes.container_memory_current_bytes` + `kubernetes.deployment_container_memory_limit` — **Q의 힙 압박을 직접 관측**(1Gi limit 대비 상승). 3종 중 Q의 ③이 가장 잘 관측됨.
   - `kubernetes.container_restart_count` / `container_last_termination_reason` — Q의 OOMKilled 신호.

### 4-C. 진짜 능력갭 여부 판정
**셋 다 진짜 injector 능력갭 아님.** F19-Q(앱 내부 @Scheduled 정지 제어점 부재)와 달리, 이 계열의 결함은 "무거운 쿼리를 반복 호출"로 100% 재현되고 그 수단(k6 GET)은 실재한다. 유일한 장애물은 (a) 그 endpoint를 치는 스크립트가 아직 없음, (b) 결정 지표(DB CPU·Hikari pending) query가 아직 등록 안 됨 — **둘 다 배선**이다. §2-A "Class A 위장 금지" 위배 없음: env로 코드동작을 왜곡하지 않고, 실제 결함 있는 쿼리 경로를 실제 부하로 자연 발화시킨다.

---

## 5. 헌장 부합성 평가 (한 문단)

세 시나리오는 헌장 §3.2가 "앵커 얕음(F02-R 인덱스 미사용)"으로 분류했던 것을 **슬로우쿼리라는 독립 패턴 축(P8 후보)**으로 끌어올려 3도메인에 걸쳐 실코드 file:line으로 앵커링했고(①② 3개 모두 ✅), 헌장이 요구한 "깊이"를 각기 다른 잠식 자원(공유 PG CPU / 커넥션풀 / JVM 힙)에 매핑해 패밀리 내부 감별을 성립시켰다. 가장 값진 발견은 **동일 계열 안에서 인덱스 결함의 성격이 세 갈래로 갈린다는 것**이다: R은 인덱스가 아예 없고(설계상 부재, F02-R의 드롭과 다름), P는 인덱스가 있는데 trunc() 함수 래핑으로 무력화되며, Q는 GROUP BY DATE() 함수 무력화에 더해 페이지네이션 부재까지 겹친다 — "인덱스가 없다/있는데 안 쓴다"의 구분은 슬로우쿼리 RCA의 실무 핵심이며 4조건 루브릭이 이를 서로 다른 답으로 강제했다. 정직하게 남는 한계는 두 가지다: ④ 주입은 F19-Q 같은 진짜 제어점 갭이 아니라 **슬로우쿼리 endpoint를 치는 loadgen 스크립트가 아직 없다는 순수 배선 갭**이고(그래서 셋 다 F19-Q보다 가벼운 blocked), ③ 관측은 이 계열의 결정 ground-truth인 **DB-process CPU/쿼리시간 지표가 registry에 없어**(R의 교차 스키마 오염은 apm_service_p95 두 서비스 동반 상승으로 간접 감별) 🟡에 걸린다 — 다만 Q의 힙 압박만은 기존 container_memory query로 직접 관측돼 3종 중 ③이 가장 튼튼하다. 결론: 셋 다 배선 백로그(스크립트 신설 + DB CPU/Hikari query 등록)로 즉시 올릴 값어치가 있고, 진짜 인프라 능력갭이 없다는 점에서 F19-P·F19-S와 같은 부류(즉시 승격 가능)이되 관측 배선이 한 겹 더 필요한, 정직하게 blocked인 설계다.
