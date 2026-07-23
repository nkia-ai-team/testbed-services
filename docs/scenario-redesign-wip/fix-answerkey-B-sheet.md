# FIX 배치 B — answer-key 미작성 5개 설계 시트

담당: **F14-P, F14-R, F15-P, F15-T3, F15-T4**
근거: spec-scenario-design-charter.md §2(4조건)·§2-B(P1~P7)·§5(injector gap), spec-scenario-design.md §3(split/merge/multi-root),
fault-surface-{commerce,food-delivery,core-banking}.md, 코드 실측(2026-07-23).
산출: 각 시나리오별 4조건(①코드앵커 ②answer-key ③감별 ④주입수단) 판정 + answer-key(fix-<id>-metadata.json) + 배관 gap.

전 5건 공통: `readiness=blocked|partial`(현행 유지), `prerequisite_gate.state=unresolved→blocked`, live 금지. answer-key만 신규 작성.

---

## F14-P — selective-ledger-loss (Class A / P5, banking)

### 인과 사슬 (코드 실측)
```
inject: ledger recordTransfer 경로에 transient 실패 주입(예: ledger_entries 행 락 / Oracle 순간 blip)
  이체 트래픽은 계속 200 COMPLETED (동기 경로 무손상)
  → banking.transfers 이벤트가 ledger consumer로 정상 유입
  → recordTransfer() 가 실패 예외 throw
  → TransferEventConsumer.onTransferEvent catch(Exception){ log.error } 로 삼킴 (line 40-42)
  → 예외를 되던지지 않으므로 Kafka offset 커밋됨 → 해당 이벤트 재처리 안 됨
  → ledger_entries 에 그 이체가 영구 누락 (at-most-once 실질)
  → ReconciliationBatch(10분) 만이 사후 imbalance WARN 로 관측
```
정답 = **ledger consumer 에러처리(catch-swallow)** 가 유실의 root. 5xx 없음. 이체 API·transfer·account 전부 정상 200.

### 핵심 코드 앵커 (직접 검증)
- `core-banking/ledger-service/.../consumer/TransferEventConsumer.java:38-42` — `catch (Exception ex) { log.error(...); }` **되던지지 않음** → offset 커밋 → 유실. **최강 앵커(FS-10)**.
- `TransferEventConsumer.java:28`(=@KafkaListener groupId=ledger-service) + `ledger application.yml:27-31`(concurrency 미설정=단일스레드) — 유입 경로.
- `LedgerService.java:35` `findByTransferRef` 멱등조회 — **주의: 멱등은 중복을 막을 뿐, 삼켜져 아예 도달 안 한 이벤트의 유실은 못 막음**(감별 포인트).
- `ReconciliationBatch.java:30-31`(LedgerService.java:50-57 sumImbalance) — 유일한 사후 관측면.
- 동기 200 독립 근거: `TransferService.execute()` 단일 @Transactional(TransferService.java:51-107), outbox→relay→Kafka 정상 발행.

### 4조건 판정
| 조건 | 판정 | 근거 |
|---|---|---|
| ① 코드/인프라 앵커 | ✅ | TransferEventConsumer.java:40-42 catch-swallow 실측. P5(silent async 유실)에 정확 매핑. |
| ② answer-key 작성 | ✅ | fix-F14-P-metadata.json root_cause{service, banking-ledger-consumer, P5, code_anchor}. 코드 거동(삼킴→offset커밋→유실)과 일치. |
| ③ 감별 가능 | 🟡 | 감별 *설계* 완결(§ 아래). 결정지표 `database.ledger_imbalance_count`(또는 outbox 발행분 대비 ledger 반영분 diff) query_id 미존재 → DB selector 신설 필요. 설계 ✅ / 관측 배선 ❌. |
| ④ 주입 수단 실재 | ❌ | **injector 미구현.** consumer 처리경로에 *선별적 transient 실패*를 주입할 수단 없음. recordTransfer 실패를 만들려면 ledger_entries 락/Oracle blip을 이벤트 도착과 겹치게 넣어야 하는데 그 executor 부재. → gate=blocked. |

### 감별 설계
- must_support: 이체 성공률 정상(transfer_2xx_rate≥0.95) · ledger_entries 최신 이체 **선별적** 누락(전량 아님) · ReconciliationBatch imbalance WARN↑.
- must_rule_out: 이체 5xx 급증(있으면 lock/502 계열) · **outbox published_at=null 적체↑(있으면 F18-P relay 정지)** · **Kafka consumer lag↑(있으면 F04-R consumer 정지 — F14-P는 consumer 살아서 소비는 하되 삼킴)** · entry_status≠429.
- 오답 유도: 이체 API 전부 200이라 trace상 무장애 → naive RCA "정상" 오판. 정답은 데이터 정합(imbalance) 검사로만 드러남.

### 배관 gap (승격 전)
1. **[진짜 병목/injector ❌]** ledger recordTransfer 선별 실패 주입 executor 신설(예: ledger_entries hot row 락을 이벤트 유입과 동기화, 또는 ledger Oracle 순간 차단). 현행 executor 중 이 표면 없음.
2. **[관측 ③]** `database.ledger_imbalance_count`(banking selector `SELECT ABS(SUM(debit)-SUM(credit)) ...` 또는 발행-반영 diff) query_id + DB adapter selector 서버측 구현.
3. **[계약]** business.fault / load.north_south parameter_contract 에 F14-P 등록(banking surge 30rps).

**처분: FIX 유지 — answer-key ✅ 작성. injector(❌) + 관측(🟡)이 승격 전 배선. gate=blocked.**

---

## F14-R — non-idempotent-response-loss (Class A / P5·P7, commerce)

### 인과 사슬 (코드 실측)
```
inject: order 진입 앞단 fault-proxy 로 (a) 응답 드롭(response loss) 또는 (b) 중복요청 주입
  order createOrder 는 멱등키(Idempotency-Key/requestId) 를 전혀 안 봄 (검증: grep 0건)
  경로 A(응답유실): createOrder 가 DB 커밋+재고예약 성공 → 응답 패킷 유실 → 클라 타임아웃 재시도
     → 멱등키 없어 두 번째 요청이 새 주문 재생성 → 중복 주문/이중 재고예약 (데이터 드리프트)
  경로 B(중복주입): 동일 논리주문이 2건으로 실체화, 5xx 없이 200 두 번
  → 표면 지표(성공률/지연) 정상, 오직 order 행수·재고 movement 정합으로만 드러남
```
정답 = **order createOrder 멱등키 부재** 가 root (P5 silent 데이터드리프트 + P7 무결성). 5xx 없음.

### 핵심 코드 앵커 (직접 검증)
- `commerce/order-service/.../OrderService.java:60 createOrder(OrderRequest)` — **멱등키 파라미터/검사 전무**(grep `idempoten|requestId|dedupe|clientRef` = 0건). checkout(:144)도 동일.
- OrderRequest DTO 에 dedup 필드 없음, orders 테이블 UNIQUE(business key) 없음(init-schemas.sql user_id 인덱스만).
- `OrderService.java:59/144` 전체 @Transactional — 커밋 성공 후 응답유실 시 rollback 불가(이미 커밋) → 재시도가 중복 실체화.
- 대비군: banking ledger 는 `findByTransferRef` 로 멱등(LedgerService.java:35) — commerce order 는 그런 가드가 **없다**는 것이 정체성.

### 4조건 판정
| 조건 | 판정 | 근거 |
|---|---|---|
| ① 코드/인프라 앵커 | ✅ | OrderService.java:60 멱등키 부재 실측(grep 0건). P5/P7 매핑. |
| ② answer-key 작성 | ✅ | fix-F14-R-metadata.json root_cause{service, commerce-order, P5, code_anchor}. |
| ③ 감별 가능 | 🟡 | 설계 완결. 결정지표 `database.duplicate_order_count`(동일 user_id+cart+±window 중복 orders) selector 미존재 → 신설 필요. |
| ④ 주입 수단 실재 | ❌ | **proxy 부재.** 응답 드롭/중복요청을 주입하는 fault-proxy(중간자) 미구현. timeline.compose 는 sub-injection 조합만, response-loss proxy 이미지 없음(manifest prerequisite_gate="response-loss proxy/fault image"와 일치). gate=blocked. |

### 감별 설계
- must_support: 주문 성공률 정상(2xx) · **중복 orders 행 증가(동일 논리주문 N>1)** · 재고 movement 이중 차감.
- must_rule_out: 주문 5xx 급증(있으면 하류지연/풀고갈 C1/C12) · order Hikari 고갈 signal · entry_status≠429 · user 401(있으면 F16-H fail-close).
- 오답 유도: 200만 보고 정상 판정 or "재고 부족"을 inventory 탓으로 오판. 정답=order 멱등설계 결함.

### 배관 gap
1. **[injector ❌]** response-loss / duplicate-request fault-proxy(중간자 컨테이너 or ingress 규칙) 신설. 현행 timeline.compose 로는 불가.
2. **[관측 ③]** `database.duplicate_order_count` selector(commerce order_schema) query_id 신설.
3. **[계약]** load.north_south commerce surge 등록.

**처분: FIX 유지 — answer-key ✅ 작성. proxy(❌)가 최대 gap. gate=blocked.**

---

## F15-P — common-node-pressure (Class B / 인프라, merge)

### 판정 핵심: T1(anti-merge)의 정반대 = **MERGE(단일 공통 root)**
spec-scenario-design.md §3: merge = 서로 다른 증상이어도 **하나의 공통 root** 입증되면 한 사건. F15-P 는 공유 worker 노드 자원압박이 그 위에 co-locate 된 여러 서비스를 동시 열화시켜 증상이 여러 서비스로 퍼지지만 root 는 **노드 1개**. → merge, `has_single_cause=true`.

### 인과 사슬 (인프라 앵커 + 배치 전제)
```
전제(배치 topology): 한 도메인의 서비스들은 같은 worker 노드에 co-locate
   (project CLAUDE.md: tb-w1=commerce / tb-w2=food-delivery / tb-w3=core-banking)
inject: host.stress(ssh) 로 대상 worker 노드 CPU/mem 포화
  → 그 노드의 모든 pod 가 CPU steal / 스케줄 지연
  → 여러 서비스가 동시에 지연·간헐 5xx (증상 분산)
  → naive RCA 는 각 서비스를 개별 root 로 오판(증상만 보고)
  → 정답 = 공유 노드 자원압박(공통 root, merge)
```
Class B(인프라). 앵커 = 노드 자원 + host.stress executor(ssh). **판정은 co-residency 전제 성립이 선결**(서비스들이 실제 같은 노드에 있어야 merge 성립).

### 4조건 판정
| 조건 | 판정 | 근거 |
|---|---|---|
| ① 코드/인프라 앵커 | 🟡 | 인프라 지점(worker 노드 자원)+조작수단(host.stress ssh) 실재. 단 "여러 서비스 co-residency"가 앵커 성립 전제 → 배치 검증 필요(preflight host-placement-and-oob-recovery 존재). |
| ② answer-key 작성 | ✅ | fix-F15-P-metadata.json root_cause{host, shared-worker-node, merge, has_single_cause=true}. |
| ③ 감별 가능 | 🟡 | 설계 완결(공통 노드 metric↑ + 다서비스 동시). node-level SMS metric(host CPU/mem)은 관측되나, "동시성=공통 root" 판정 지표(공동 상관) 배선 필요. |
| ④ 주입 수단 실재 | ✅ | host.stress executor(ssh)는 실재·검증(F09-* SMS 계열 사용). 계약에 F15-P 노드 target 등록만. |

### 감별 설계
- must_support: 대상 노드 host CPU/mem 포화 · **동일 노드 co-locate 서비스 다수 동시 지연** · pod throttling.
- must_rule_out: 단일 서비스만 지연(있으면 그 서비스 코드결함/split) · 타 노드 서비스 정상 · DB 락 signal 없음 · entry_status≠429.
- **판정 규칙**: 증상 서비스들이 모두 한 노드 위 → merge(공통 root=노드). 증상 서비스가 서로 다른 노드/원인 → **split/multi-root(F15-T1/F15-G 쪽)**. 이 갈림이 F15-P 정체성.
- 오답 유도: 여러 서비스 동시 5xx → 다중 장애로 오판. 정답=공통 노드 압박 1건.

### 배관 gap
1. **[전제 검증]** 대상 서비스들의 **동일 노드 co-residency 보장**(nodeSelector/affinity 또는 배치 확인). 미보장 시 merge 전제 붕괴 → 시나리오 무효. gate 핵심.
2. **[관측 🟡]** node-level 다서비스 동시상관을 결정지표로 노출(공통 SMS metric + 증상 서비스 목록 태깅).
3. **[계약]** host.stress parameter_contract 에 F15-P(노드 id, cpu/mem 강도) 등록.

**처분: FIX 유지 — answer-key ✅(merge/has_single_cause=true). injector ✅, 배치 전제 검증이 gate. gate=blocked(placement 미확정 시).**

---

## F15-T3 — host-cpu-nested-consumer-stall (Class B 인프라 + P5 취약성)

### 판정 핵심: **MERGE / 단일 인과사슬** (multi-root 아님)
charter가 요구한 "독립 근본 여럿 vs 전파사슬 억지 분할" 판정 → **단일 사슬**. host CPU 포화가 root 이고, consumer stall 은 그 *하류 전파*다. consumer 의 단일스레드(P5 코드 속성)는 **취약성(amplifier)** 이지 독립 root 가 아니다. host CPU 를 걷어내면 consumer 도 회복 → 인과 종속 입증. → merge, `has_single_cause=true`.

### 인과 사슬 (코드+인프라 실측)
```
inject: host.stress(ssh) 로 consumer 가 뜬 worker 노드 CPU 포화  [ROOT: 호스트 CPU, Class B]
  → 그 노드의 단일스레드 Kafka consumer 가 CPU 기아
     (banking ledger TransferEventConsumer concurrency 미설정=1 thread, ledger yml:27-31)
  → 건당 처리속도 급락 → banking.transfers consumer lag 단조 증가  [NESTED 증상: P5 취약성]
  → ledger_entries 반영 지연(원장 silent lag), 이체 API 는 200 정상
  → 단일 root(호스트 CPU) → consumer stall → 데이터 지연  ... 하나의 전파사슬
```
"nested" = 인프라 root 위에 코드 취약성(단일스레드)이 겹쳐 증폭. **두 개의 독립 사건이 아님.**

### 핵심 앵커
- 인프라 root: worker 노드 CPU(host.stress ssh) — Class B.
- 코드 취약성(왜 stall 이 큰가): `TransferEventConsumer.java:28`(단일 @KafkaListener) + `ledger application.yml:27-31`(concurrency 미설정=1 thread). LedgerService.recordTransfer 매건 DB write.
- 동기 200 독립: TransferService 단일 @Transactional(정상).

### 4조건 판정
| 조건 | 판정 | 근거 |
|---|---|---|
| ① 코드/인프라 앵커 | ✅ | host CPU(host.stress) + 단일스레드 consumer(ledger yml:27-31). 인프라 root + P5 취약성 결합, 실측. |
| ② answer-key 작성 | ✅ | fix-F15-T3-metadata.json root_cause{host, worker-node-cpu, merge, has_single_cause=true, nested consumer stall}. |
| ③ 감별 가능 | 🟡 | 설계 완결(host CPU↑ ∧ consumer lag↑ 동반, 인과종속). `kafka.consumer_lag{group=ledger-service}` query_id 존재여부 확인 필요 — 없으면 신설. |
| ④ 주입 수단 실재 | 🟡(partial) | host.stress ✅(실재). 단 timeline.compose 로 host.stress+대상 consumer 노드를 결속하는 계약 등록 필요. manifest readiness=partial 과 일치. |

### 감별 설계
- must_support: 대상 노드 host CPU 포화 · ledger consumer lag↑ · 원장 반영 지연 · 이체 2xx 정상.
- must_rule_out: **consumer replica down/scale=0(있으면 F04-R)** · **outbox published_at=null 적체(있으면 F18-P relay 정지)** · 타 노드 정상 · 이체 5xx(있으면 lock 계열) · entry_status≠429.
- **판정 규칙(핵심)**: host CPU 를 제거하면 consumer lag 회복 → **인과종속=단일사슬(merge)**. 만약 consumer 가 host 와 무관히 독립 정지면 그건 F04-R(split). 이 종속성 입증이 T3 를 multi-root 아닌 merge 로 고정.

### 배관 gap
1. **[관측 🟡]** `kafka.consumer_lag{group=ledger-service}` + node host CPU query_id 동시 노출(둘의 동반상승이 사슬 증거).
2. **[계약 partial]** timeline.compose 에 host.stress(노드)+대상 consumer 결속 등록.

**처분: FIX 유지 — answer-key ✅(merge/단일사슬, root=호스트 CPU, consumer stall=nested 전파). partial. gate=blocked.**

---

## F15-T4 — pg-lock-handoff-kafka-lag (P3 → P5, 순차)

### 판정 핵심: **SPLIT (시간 순차 · 독립 2 root)** — 단일사슬 아님, multi-root(동시) 아님
charter 요구 판정. **결정 근거**: commerce inventory row-lock(P3)과 Kafka consumer lag(P5) 사이에 **코드상 인과경로 없음**. inventory product 행 락은 shipping/notification Kafka consumer 를 stall 시키지 않는다(서로 다른 서비스·topology·자원). 따라서 "PG락이 lag를 유발"하는 단일 전파사슬은 **거짓**. → 두 root 는 독립. 그리고 이름·transport(timeline.compose, subinjection-timeline)·"handoff/순차"가 가리키듯 **시간상 순차**(A 종료→B 시작)이므로 동시(F15-T1/F15-G)가 아닌 **temporal split**. → `has_single_cause=false`, expected=**두 개의 분리된 인시던트/RCA(순차)**.

이로써 T-계열이 깔끔히 분화: **T1=동시 독립(anti-merge/multi-root), T3=단일사슬(merge, 인프라 root), T4=순차 독립(temporal split)**.

### 인과 (두 독립 하위장애, 시간 순차)
```
[구간1: t0~t1]  PG inventory 행 FOR UPDATE 장기 락(P3, DPM)
   → commerce reserve 직렬화 대기 → order checkout 지연/502 (F02/C2 계열)
   → cleanup 후 락 해제, 구간1 종료
[handoff]
[구간2: t1~t2]  commerce shipping Kafka consumer 정지(P5, KCM) — 구간1과 독립 주입
   → PAID 이벤트 미소비 → shipping lag↑ → 배송 생성 지연 (F04-R 계열)
두 root 는 자원·topology·시간창이 분리 → split. 겹치는 시각 없음(순차).
```

### 핵심 앵커
- 구간1 root: `InventoryRepository.java:17-19` PESSIMISTIC_WRITE(NOWAIT 없음) + InventoryService.java:73-96. P3.
- 구간2 root: `OrderEventConsumer.java:28-38`(shipping groupId), F04-R 동일 표면(consumer scale=0). P5.
- 인과 부재 증거: inventory 행 락과 shipping consumer 는 공유 자원/코드경로 없음(서로 다른 schema·서비스·노드 가능).

### 4조건 판정
| 조건 | 판정 | 근거 |
|---|---|---|
| ① 코드/인프라 앵커 | ✅ | 구간1 InventoryRepository.java:17-19(P3) + 구간2 OrderEventConsumer.java:28-38(P5). 둘 다 실측, KEEP 앵커(F04-R/C2) 재사용. |
| ② answer-key 작성 | ✅ | fix-F15-T4-metadata.json root_cause **배열 2개**(disjoint 시간창) + has_single_cause=false + expected=split. |
| ③ 감별 가능 | 🟡 | 설계 완결(구간별 지표 분리). 두 지표(PG blocking session, shipping lag) 존재하나 **시간창 분리 태깅**이 split 판정 결정지표 → sub-injection timeline 이 각 구간 t창 기록. |
| ④ 주입 수단 실재 | 🟡(partial) | db lock(F02/C2 실재) + kafka consumer stop(F04-R 실재) 둘 다 존재. timeline.compose 로 **순차 결속**(offset≠0, 구간 분리) 계약 등록 필요. manifest partial 과 일치. |

### 감별 설계
- must_support: 구간1 PG blocking session(태그)+checkout 502(t0~t1) · 구간2 shipping consumer lag↑+배송 지연(t1~t2) · **두 구간 시간창 비중첩**.
- must_rule_out: **두 장애 동시 시작(있으면 multi-root T1/G)** · 한 root 가 다른 root 유발하는 인과경로(있으면 단일사슬 merge) · 공유 노드/DB 원인(있으면 F15-P merge) · entry_status≠429.
- **판정 규칙**: 시간창 분리 ∧ 인과경로 부재 → **split(2 인시던트 순차)**. 이 시나리오는 RCA 가 "하나의 근본으로 억지 축약"하지 않고 순차 2건으로 분리 보고해야 정답.

### 배관 gap
1. **[계약/injector 🟡]** timeline.compose 에 db.lock(inventory)→[gap]→kafka.control(shipping) 순차 스케줄 등록(offset/duration 분리). 두 injector 는 실재.
2. **[관측 ③]** 구간별 시간창을 캡처 메타에 분리 기록(sub-injection timeline) — split 판정 근거.

**처분: FIX 유지 — answer-key ✅(SPLIT/temporal, has_single_cause=false, 2 root 순차). 단일사슬·multi-root 아님 확정. partial. gate=blocked.**

---

## 요약: T-계열 구조 분화 (charter §3 고정)
| id | 구조 | root 수 | 시간 | has_single_cause |
|---|---|---|---|---|
| F15-T1 | anti-merge / multi-root | 2 독립 | 동시(offset 0) | false |
| F15-G | multi-root | 2 독립 | 동시 | false |
| **F15-P** | **merge** | **1(공유 노드)** | — | **true** |
| **F15-T3** | **merge / 단일사슬** | **1(호스트 CPU), consumer=nested 전파** | — | **true** |
| **F15-T4** | **split / temporal** | **2 독립(P3 then P5)** | **순차(비중첩)** | **false** |
