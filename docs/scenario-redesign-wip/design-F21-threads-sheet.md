# 설계 시트 — F21-Q / F21-P: Tomcat 스레드풀(기본 200) 포화 2종 (Class A, P2)

- **백로그 근거**: 헌장 §2-B P2 "`@Transactional(timeout)` 부재 + 기본 풀(Hikari 10~15 / **Tomcat 200**)". P2의 "Tomcat 200" 축이 commerce(F03-H)에만 있고 food/banking 빈칸 → 두 칸 채움.
- **코드 근거**: fault-surface-food-delivery.md §2 order-service + fault-surface-core-banking.md §0·§1 api/account + FS-1/FS-2/FS-9. 아래 앵커 전부 실소스 grep 확인(2026-07-24).
- **F03-H와의 관계(중요)**: 기존 commerce F03-H(`f03-h-order-thread-saturation`)는 헌장 감사에서 "합성 sleep 엔드포인트 의존"이 문제라 **재앵커 대상**(order 풀10+5hop tx 한계로 재정의)이었다. F21은 그 교훈을 반영해 **합성 엔드포인트 0** — 실코드 호출 경로(createOrder fan-out / api→account→transfer 동기체인)의 자연 스레드 점유만으로 포화를 만든다.
- **핵심 감별 정체성**: 병목 자원이 **Tomcat busy threads(200)** 임을 증명해야 한다. 커넥션풀(Hikari) 고갈(F19-P)보다 **한 층 위 자원**. 그래서 두 시나리오 모두 "풀이 먼저 터지지 않고 스레드가 먼저 200에 닿는" 경로를 골라야 하는데, 이게 설계의 전부다(§1 참조).

---

## 0. 왜 대부분의 경로는 Tomcat 200에 닿기 전에 Hikari가 먼저 터지는가 (설계의 핵심 제약)

`@Transactional` 메서드가 하류 원격호출을 **DB 커넥션을 잡은 채** 기다리면, 그 서비스는 Tomcat 200이 아니라 Hikari(10~15)가 **먼저** 고갈된다(= F19-P 계열). Tomcat 스레드가 200까지 차오르려면, 스레드를 오래 점유하는 요청이 **그 시간 동안 DB 커넥션을 쥐고 있지 않아야** 한다. 이 조건을 만족하는 실코드 지점을 찾는 것이 F21 설계의 본질이며, 두 도메인에서 각각 다른 방식으로 성립한다:

- **food F21-Q — Hibernate 지연 커넥션 획득(delayed acquisition) 이용**: `createOrder`(@Transactional, `OrderService.java:57`)의 첫 3개 원격호출 `getRestaurant`(:60)·`getMenu`(:73)·`checkCapacity`(:96)은 **전부 `orderRepository.save`(:128)보다 앞**에 있다. Spring Boot 3.4/Hibernate 6 기본값 `DELAYED_ACQUISITION_AND_RELEASE_AFTER_TRANSACTION`에서는 **첫 SQL(:128 save)이 실행되기 전까지 Hikari 커넥션을 획득하지 않는다.** 따라서 **restaurant를 지연**시키면 각 createOrder가 :60에서 최대 read-timeout(5s)만큼 **Tomcat 스레드만** 점유하고 **Hikari는 잡지 않는다**(active≈0, pending≈0). 지연 지점을 save 앞(restaurant)에 두는 것이 F19-P(save 뒤 payment 지연=Hikari 보유)와의 결정적 분기다. **앵커 가정: 지연 커넥션 획득이 기본으로 켜져 있어야 성립** — 실환경에서 `hikaricp_connections_active`가 restaurant 지연 중 낮게 유지되는지로 검증(§4).
- **banking F21-P — api-service에 DataSource가 아예 없음**: `ApiServiceApplication.java:12` `@SpringBootApplication(exclude={DataSourceAutoConfiguration.class, HibernateJpaAutoConfiguration.class})` → **api는 Hikari 풀이 0개.** 하류(account→transfer)가 느리면 api→account 요청이 **DB 커넥션이라는 개념 자체가 없는** 채로 Tomcat 스레드만 점유한다 → 테스트베드에서 **유일하게 "Tomcat busy threads가 곧 병목"임이 구조적으로 자명한 서비스.** (반대로 account-service는 Hikari 10을 잡으므로 §1-B에서 별도로 다룬다 — account는 오히려 F21-P의 감별 조력자.)

---

## 1. 인과 사슬 (2개, 코드 실측)

### F21-Q (P2) — food order-service Tomcat 200 포화 (restaurant 상류지연, Hikari 미보유)
```
[trigger: restaurant-service getRestaurant/getMenu 응답을 baseline보다 지연 (목표 ~4s, read-timeout 5s '미만')]
  → createOrder(@Transactional, OrderService.java:57)가 :60 getRestaurant에서 ~4s 대기
     (아직 save 前 → 지연 커넥션 획득으로 order Hikari 커넥션 미보유)
  → 각 createOrder가 order Tomcat 스레드를 ~4s 점유. 동시 createOrder ≥200이면 Tomcat 200 포화
  → order-service acceptor 큐 적체 → 신규 요청이 스레드를 못 받음
  → 무관한 읽기 GET /api/orders(@Transactional(readOnly), OrderService.java:173)도 지연/타임아웃
     ← 이게 Tomcat-포화의 지문: '느린 하류를 안 타는' 엔드포인트까지 전염
```
앵커: `OrderService.java:57`(@Transactional, timeout 미설정), `:60/:73/:96`(save 前 순차 원격호출), `:128`(orderRepository.save = 첫 SQL = Hikari 획득 시점), `:173`(무관 읽기 GET), order `application.yml:1-2`(**server: tomcat 블록 부재 → 기본 200**, grep `server.tomcat` 전무 확인), `:53-54`(restaurant read-timeout **5s**). **핵심: 지연을 restaurant(save 前 hop)에 둬 order Hikari를 비운 채 Tomcat만 채운다 — F19-P는 payment(save 後 hop) 지연이라 Hikari 15가 먼저 고갈.**

### F21-P (P2) — banking api-service Tomcat 200 포화 (transfer 하류지연, 풀 자체가 없음)
```
[trigger: transfer-service 이체 처리를 baseline보다 지연 (목표 ~7-8s, account read-timeout 15s·api read-timeout 10s '미만' → 느린 200 성공, CB 미개방)]
  → account.validateAndForward(@Transactional(readOnly), AccountService.java:57)가 :80 transferClient.executeTransfer에서 대기
  → api→account 요청(api application.yml:11-12, read-timeout 10s)이 account 응답을 기다리며 api Tomcat 스레드 점유
  → api-service는 DataSource 제외(ApiServiceApplication.java:12) → Hikari 0 → 오직 Tomcat 200만 유한 자원
  → 동시 이체 ≥200이면 api Tomcat 200 포화 → acceptor 큐 적체
  → 무관한 조회프록시 GET /api/transfers(api application.yml:15-16)도 스레드 굶주림으로 지연
```
앵커: api `application.yml:1-2`(**server: tomcat 블록 부재 → 기본 200**), `:11-12`(account read-timeout **10s**), `:15-16`(transfer 조회 read-timeout 10s), `:18-49`(resilience4j CB window10/min5/50%/open5s + retry max-attempts=3), `ApiServiceApplication.java:12`(**DataSourceAutoConfiguration 제외 = Hikari 0**), account `application.yml:29-30`(transfer read-timeout **15s**), `AccountService.java:57,80`(@Transactional(readOnly) + 말미 executeTransfer), transfer `AccountRepository.java:18-20`(FOR UPDATE NOWAIT 없음), `TransferService.java:51`(@Transactional timeout 없음). fault-surface FS-9(retry x3 증폭), FS-1/FS-2(lock/CB 축).

---

## 2. 감별 설계 (골든 조건 ③)

| | must_support | must_rule_out (감별점) |
|---|---|---|
| **F21-Q** | order **busy-threads → 200 근접**(`http.server.active_requests`); order Hikari **pending≈0·active 낮음**; **무관 GET /api/orders 지연 상승**; order create 지연↑(5xx는 낮거나 timeout성) | order Hikari 고갈발 아님(pending≈0) → **F19-P와 결정 구분** · payment 502 아님(지연은 restaurant hop, save 前) → F19-S 아님 · 배차 503 아님(dispatch 정상, capacity>0) → F19-Q 아님 · 429 아님(food=503) |
| **F21-P** | api **busy-threads → 200 근접**; api **502 rate 낮음**(느린 200 성공, CB 미개방); api Hikari **N/A(풀 없음)**; **무관 GET /api/transfers 조회프록시 지연**; transfer는 지연만(5xx 아님) | api 풀고갈 아님(**풀이 존재하지 않음**) → FS-6 아님 · api CB **open 아님**(개방되면 즉시 502·스레드 해제 = FS-2로 전이) → FS-2 아님 · 502 폭증 아님(폭증이면 lock 무한대기 FS-1) → FS-1 아님 · account는 Hikari 10이 먼저 참(§1-B) → account가 아니라 **api**가 순수 스레드 지표 |

**F21-Q ↔ F19-P 결정축(가장 중요)**: 둘 다 증상이 order로 수렴하나 **병목 자원이 다르다.** F19-P=payment(save 後) 지연 → `hikaricp_connections_pending>0`, active≈15(풀 고갈). F21-Q=restaurant(save 前) 지연 → `hikaricp_connections_pending≈0`, `http.server.active_requests→200`(스레드 고갈), 그리고 **무관 GET 전염**은 F21-Q에만 있다(Hikari 고갈은 DB 만지는 엔드포인트에 국한, 스레드 고갈은 전 엔드포인트 전염). 이 세 지표(pending / active_requests / 무관GET지연)가 P2↔P1을 가른다.

**F21-P ↔ FS-1/FS-2 결정축**: 셋 다 근본은 transfer 지연이나 **api의 응답 형태가 다르다.** FS-1(lock 무한대기)→api 502 폭증. FS-2(CB open)→api 즉시 502·이체 0%. F21-P→api **느린 200 성공**(CB 미개방, 502 낮음) + **busy-threads 200**. 즉 F21-P는 "지연 주입이 timeout 미만이라 실패가 아니라 지연으로만 나타나는" 구간에 산다 — 주입이 과하면(transfer 응답>10s) api read-timeout→502→CB open으로 **FS-2로 전이**한다. 이 전이 경계가 F21-P의 감별선이자 주입 캘리브레이션의 핵심(§4).

**오답 유도**: 두 시나리오 모두 증상이 진입 오케스트레이터(order / api)로 수렴 + 무관 엔드포인트까지 지연 → naive RCA가 order/api를 root로 오판. 정답은 각각 restaurant 상류지연(Q) / transfer 하류지연(P)이며, order/api는 "튜닝 안 된 Tomcat 200 + 풀 없음/미보유"라는 **구조적 증폭기**일 뿐.

---

## 3. 골든 4조건 자체점검표

| 조건 | F21-Q (food) | F21-P (banking) |
|---|---|---|
| ① 코드/인프라 앵커 | ✅ `OrderService.java:57,60,73,96,128,173` + `application.yml:1-2`(tomcat 블록 부재=200), `:53-54`(rt 5s). 지연 커넥션 획득으로 save 前 hop이 Hikari 미보유임을 앵커. P2 매핑 | ✅ `ApiServiceApplication.java:12`(**DataSource 제외=풀 0**), api `application.yml:1-2`(tomcat 200), `:11-12`(rt 10s), account `:29-30`(rt 15s), `AccountService.java:57,80`, transfer `AccountRepository.java:18-20`. P2 매핑. **api=풀 없는 유일 서비스**가 최강 앵커 |
| ② 정답(answer-key) | ✅ metadata root_cause{service=food-restaurant 상류지연, mechanism=order Tomcat 200 포화·Hikari 미보유, code_anchor}. "왜 order가 아니라 restaurant인가"·"왜 Hikari 아니라 스레드인가" 명시 | ✅ metadata root_cause{service=banking-transfer 하류지연, mechanism=api Tomcat 200 포화·풀 부재, code_anchor}. "api엔 풀이 없어 스레드가 유일 자원" 정직 표기 |
| ③ 감별 가능 | 🟡 감별 *설계* 완결(vs F19-P/F19-S/F19-Q). 그러나 결정 지표 미배선: `http.server.active_requests`(busy-thread proxy)·`hikaricp_connections_pending/active`·무관 GET 지연·`loadgen.food_create_status_rate` 전부 미등록 query. §4 | 🟡 감별 설계 완결(vs FS-1/FS-2/FS-6). 그러나 `http.server.active_requests`·`loadgen.transfer_2xx_rate`(느린200 관측)·api CB state query 미배선. §4 |
| ④ 주입 수단 실재 | 🟡 **정밀 지연 injector 부재가 근본 난점.** restaurant hop엔 mock이 없어(food mock=PG 전용) delay_seconds 정밀주입 불가 → host.stress(worker-w2 CPU) 또는 db.workload(restaurant MySQL)로 **간접·조립식** 지연. 실재하나 거칠고 read-timeout 5s 미만 유지에 캘리브레이션 필수. inert stub은 아님 | 🟡 동일 난점. transfer hop엔 mock 없음 → host.stress(worker-w3) 또는 db.workload(transfer Oracle)로 지연. **10s(api rt) 미만 유지 실패 시 FS-2로 오염**되므로 캘리브레이션이 감별 정체성과 직결. db.lock은 부적격(무한대기→502→CB=FS-1/2) |
| **종합** | ①②✅ ③④🟡 → **draft/blocked** (배선 + 지연주입 캘리브레이션) | ①②✅ ③④🟡 → **draft/blocked** (배선 + 지연주입 캘리브레이션) |

둘 다 `readiness=draft`, `prerequisite_gate.state=blocked`, `live_allowed=false`.

---

## 4. 능력 갭 (승격 전 선행 — prerequisite_gate)

### 4-A. 관측 배선 (wiring — 신규 executor/코드 아님, 공유 갭)
1. **NEW query_id `prometheus.http_server_active_requests`** (두 시나리오의 결정 지표): OTel javaagent가 servlet 계측으로 emit하는 `http.server.active_requests`(semconv UpDownCounter=동시 in-flight 서버요청 = **Tomcat busy-thread 근사**)를 서비스별(food-order / banking-api)로 등록. **검증 필요**: javaagent 기본 emit 여부 + `tomcat.threads.busy`(Micrometer 바인딩) 병행 노출 여부. fault-surface들은 "앱 커스텀 메트릭 0, actuator만"이라 active_requests(javaagent 자동)가 유일한 스레드 지표 후보.
2. **NEW query_id `prometheus.hikari_pending_connections`** (F21-Q 감별, F19-P 공유): food-order Hikari active/pending. F21-Q에선 **pending≈0을 증명하는 용도**(스레드지 풀 아님). api-service는 풀이 없어 이 query가 **null/부재 → 그 부재 자체가 F21-P의 감별 증거**.
3. **NEW query_id `loadgen.food_create_status_rate`** (F21-Q, F19 공유): food surge.js create 스텝 per-status emit(현재 200/400/503 pass로 접힘). **추가로 read-step(GET /api/orders) 지연/성공률** emit 필요 — 무관 GET 전염이 F21-Q 핵심 지문인데 현 surge는 create만 침.
4. **NEW query_id `loadgen.transfer_2xx_rate`** (F21-P, 헌장 §5/F18-P 공유): banking surge.js transfer 스텝의 **느린-200 비율**(F21-P는 502가 아니라 느린 200이 정답신호). 현 surge는 not-5xx만 check.
5. **NEW query_id `prometheus.circuitbreaker_open`** (F21-P 감별): api accountClient CB state. **open이면 FS-2로 전이**했다는 신호 = must_rule_out.

### 4-B. 부하 형상 (load shaping — 산술 근거)
6. **고동시성 부하 필요 — 기존 surge 기본값으론 200 스레드 못 채움.**
   - **F21-Q 산술**: Tomcat 200 포화 = 동시 in-flight 200. 각 createOrder Phase-A 점유 ≈ restaurant 지연 d≈4s. Little's law L=λ·W → 200 = λ·4 → **λ ≈ 50 create-req/s** 지속 필요. food surge는 create가 소수 믹스 → **create-heavy 변형(order-surge.js 존재) + target_rps 대폭 상향** 필요. (지연 d를 5s(rt)에 가깝게 올리면 λ 요구는 40으로 하강하나 retry/502 위험 상승 트레이드오프.)
   - **F21-P 산술**: api 점유 ≈ transfer 지연 ≈ 8s. 200 = λ·8 → **λ ≈ 25 transfer-req/s**. banking surge transfer 믹스 **10%**(§surge.js) → 총 250rps 필요 = `load.north_south target_rps.maximum=180` 및 건강무릎(20~40rps) 초과. → **transfer-가중 surge 변형** 필요(현 믹스로는 불가). MAX_VUS 250은 동시 200 스레드 커버 가능하나 arrival-rate×믹스가 병목.
   - load.north_south는 banking 진입(30082, loadgen-banking)·food 진입(30181, loadgen-food) **이미 계약됨** → allowed_scenarios에 F21-Q/F21-P 추가 + scenario_parameters(전용 script_path, 상향 target_rps) 필요. entry/baseline_unit는 기존 재사용.

### 4-C. 지연 주입 (injection — 진짜 난점, inert stub 아님이나 정밀 부재)
7. **정밀 sub-timeout 지연 injector 부재가 F21의 근본 갭.** 정밀 `delay_seconds` 주입은 **mock 경계에만** 존재(mock.expectation=PG mock). F21의 지연 지점(restaurant getRestaurant / transfer execute)엔 **mock이 없다** → 정밀 주입 불가. 실재 대안과 부적격 사유:
   - **host.stress**(실재, live_supported): worker 노드 CPU 스트레스로 해당 노드 전 pod 지연. F21-Q=worker-w2(food), F21-P=worker-w3(banking). **거칠다**(노드 내 타 서비스 동반 지연 → 교란) + read-timeout(5s/10s) 미만 유지에 캘리브레이션 필수. allowed_scenarios(현 F09-R/F05-P)에 F21 추가 + 도메인 워커 매핑 필요.
   - **db.workload**(실재, 그러나 `live_supported=false`): restaurant MySQL / transfer Oracle에 경쟁 쿼리 부하 → 타깃 지연. host.stress보다 국소적이나 **live 미지원 = 승격 전 live-enable 선행**.
   - **db.lock 부적격**: 고정 hold(600s) row-lock → transfer FOR UPDATE **무한대기 > timeout** → 502 → CB open. = **FS-1/FS-2를 만들어 F21 감별대상 자체를 오염**(헌장 §2-A 정신). sub-timeout 지연엔 못 씀.
   - → 결론: **"완료되는(성공) 느린 응답을 timeout 미만으로 정밀 조절"** 하는 injector가 없다. host.stress/db.workload로 실현 가능하나 **캘리브레이션 게이트**(지연이 5s/10s 창을 넘으면 시나리오 정체성이 F19/FS-2로 붕괴)가 승격의 전제. F19-Q(스케줄러 정지 injector 부재)와 **동종의 진짜 능력 결이나, 완전 부재는 아니고 "정밀도 부재"**라는 점이 다르다.

---

## 5. 헌장 부합성 평가 (한 문단)

F21-Q/F21-P는 헌장 §2-B P2의 "Tomcat 200" 축이 commerce(F03-H) 한 칸에만 있던 빈자리를 food·banking 실코드로 메꿨고, F03-H가 감사에서 지적받은 "합성 sleep 엔드포인트"를 배제해 **실 호출경로의 자연 스레드 점유**만으로 포화를 설계했다(①② 두 개 모두 ✅, 앵커 전부 file:line grep 실측). 이 설계의 가장 값진 실측 발견은 "Tomcat 200이 병목이 되려면 스레드를 점유하는 동안 DB 커넥션을 쥐지 않아야 한다"는 제약을 두 도메인이 **서로 다른 코드 사실로** 통과한다는 것이다: food는 Hibernate 지연 커넥션 획득 덕에 `save`(:128) 前 restaurant hop이 Hikari를 비운 채 스레드만 점유하고(그래서 지연 지점을 save 前에 두는 것이 F19-P와의 결정적 분기), banking은 api-service가 아예 DataSource를 제외(`ApiServiceApplication.java:12`)해 **풀이라는 개념이 없는 유일한 서비스**라 스레드가 자명하게 유일 병목이다 — 4조건 루브릭이 없었다면 "그냥 부하 많이 주면 스레드 찬다"는 얕은 manifest로 승격됐을 것이고, 실제로는 account(Hikari 10)나 order-createOrder-payment(Hikari 15)가 스레드보다 먼저 터져 정체성이 F19/FS-6으로 붕괴했을 것이다. 정직한 약점은 ④다: 정밀 sub-timeout 지연 주입기가 mock 경계에만 있어(PG) restaurant/transfer hop엔 host.stress/db.workload라는 거친 대체 수단 + 캘리브레이션 게이트에 의존하며, 이 게이트를 넘기면(지연이 read-timeout 초과) 시나리오가 F19/FS-2로 전이하는 구조라 "주입의 정밀도"가 곧 감별의 전제다. 결론: 두 시나리오는 코드 앵커·정답·감별 설계가 탄탄한(①②✅ ③🟡) 진짜 장애이며, 승격은 (a) busy-thread/status-rate query 배선, (b) 고동시성 부하 형상(transfer/create-heavy 변형), (c) sub-timeout 지연 주입 캘리브레이션 — 셋을 선행 조건으로 하는 정직하게 blocked인 P2 설계다.
