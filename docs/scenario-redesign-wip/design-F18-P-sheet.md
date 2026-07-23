# 설계 시트 — F18-P: outbox relay 정지 → 원장/알림 silent lag

- **제안 id**: `F18-P` (신규)
- **slug**: `f18-p-banking-outbox-relay-halt`
- **Class / 패턴**: Class A (코드) / **P5 Silent async (outbox→Kafka send().get())** (헌장 §2-B). 백로그 #4.
- **도메인**: core-banking(주) / food-delivery(부) — 관측표면 APM + DPM(outbox row count) + KCM(consumer lag)
- **감별짝**: KEEP **F04-R**(shipping *consumer* stop) — F18-P는 *relay(producer)* stop. 정반대 측.

---

## 1. 인과 사슬 (코드 실측 근거)

```
inject: k8s.env patch testbed-transfer/transfer-service  env OUTBOX_RELAY_ENABLED=false → rollout
  → @ConditionalOnProperty(outbox.relay.enabled=true) 재평가 실패 → OutboxRelay 빈 미생성
  → @Scheduled relay() 폴링 정지 (2s 주기 중단)
동기 이체 경로는 무영향:
  api → account(검증) → transfer.execute() 단일 @Transactional
     → Oracle 잔액변경 + outbox_events INSERT (같은 로컬 트랜잭션) → COMMIT → 200 COMPLETED
  ↑ outbox INSERT는 DB 로컬이라 relay 유무와 무관하게 성공 → 이체 API 계속 200
비동기만 조용히 밀림:
  outbox_events.published_at = NULL 무한 적체 (relay가 안 드레인)
  → banking.transfers 토픽에 신규 메시지 0건 → ledger TransferEventConsumer 유입 정지
  → ledger_entries 최신 이체 미반영 (원장 지연) + banking.ledger 알림 정지
  → Kafka consumer lag 은 오히려 0 수렴 (생산이 멈췄으므로) ← 핵심 감별점
```

핵심 코드 앵커 (직접 검증):
- `core-banking/shop-common/.../outbox/OutboxRelay.java:21-22` `@ConditionalOnProperty(prefix="outbox.relay", name="enabled", havingValue="true")` — **정확한 외부 제어점**. env `OUTBOX_RELAY_ENABLED=false`가 Spring relaxed-binding으로 이 property를 override → 빈 미생성.
- `OutboxRelay.java:33` `@Scheduled(fixedDelayString="${outbox.relay.poll-interval-ms:2000}")` — 2s 폴링.
- `OutboxRelay.java:35` `findTop100ByPublishedAtIsNullOrderByCreatedAtAsc()` — null 행만 폴링(적체 지표의 근원).
- `OutboxRelay.java:39` `kafkaTemplate.send(...).get()` — 동기 blocking (P5 시그니처).
- `OutboxRelay.java:40` 성공 시에만 `setPublishedAt(now)` → relay 정지 시 null 영속.
- `transfer-service/src/main/resources/application.yml:36-38` `outbox.relay.enabled: true` (평문 YAML — env override 가능, 현재 env 미노출).
- 동기 200 근거: `TransferService.execute()` 단일 `@Transactional` (fault-surface-core-banking §2, TransferService.java:51-107). outbox INSERT가 이체 트랜잭션 내부 → relay와 독립.
- ledger 정지 근거: `TransferEventConsumer.onTransferEvent` (concurrency 미설정=단일스레드, ledger yml:27-31) — 유입 자체가 끊김.
- 근거 문서: fault-surface-core-banking **FS-4**(outbox relay 적체, line 108-112) + **FS-3**(consumer lag, 대비군).

**food(부) 등가**: `food-delivery/{order,payment,dispatch}-service/.../OutboxRelay.java:12` 전부 동일 `@ConditionalOnProperty(outbox.relay.enabled=true)`. 동일 env-disable로 food 변형(F18-P-food) 실현 가능 — 동기 createOrder 200, notify/정산 알림 무음(fault-surface-food-delivery **C5**).

---

## 2. 감별 설계 (골든 조건 ③) — F04-R와의 정반대 대칭

| 구분 | 관측 | 논리 |
|---|---|---|
| **must_support** | transfer_2xx_rate ≥ 0.95 (동기 이체 성공) | 동기 경로 무손상 — 5xx 없음 |
| | outbox_unpublished_count 단조 증가 (published_at=null) | relay 정지의 직접 증거 |
| | ledger 최신 이체 미반영 / 반영 지연 | 원장 silent lag |
| **must_rule_out** | transfer_5xx_rate 낮음(< 0.05) | 증상이 **비동기에만** 국한 (F01/FS-1 lock의 502와 구분) |
| | **kafka_consumer_lag 평탄/0 수렴** | **결정적 감별점 vs F04-R** — consumer 정지면 lag↑, relay 정지면 생산 중단→lag↓ |
| | pod_ready(testbed-transfer)=true, entry_status≠0 | 파드/브로커 다운 아님(광역 아웃티지 배제) |
| | entry_status ≠ 429 | 외부 rate-limit 오인 차단 |

**⚠️ 과업 must_support 정정**: 과업 명세는 "Kafka consumer lag 증가"를 must_support로 들었으나, **relay(producer) 정지에서는 신규 메시지 생산이 멈춰 consumer lag가 오히려 0으로 수렴**한다. lag 증가는 F04-R(consumer 정지)의 시그니처다. 따라서 F18-P의 진짜 감별 축은 **"outbox published_at=null 적체↑ AND consumer lag 평탄"** 이며, lag는 must_support가 아니라 must_rule_out(증가 아님)으로 배치해야 F04-R과 정확히 갈린다. 이 정정이 이 시나리오 정체성의 핵심.

**오답 유도 구조**: 동기 이체는 200이라 APM trace·에러율은 완전 정상 → naive RCA가 "장애 없음"으로 판정하거나 ledger를 범인으로 오판. 정답=transfer relay 정지. 데이터 정합(outbox null count)/Kafka lag 외부 관측 없이는 5xx-free라 안 보임(fault-surface §7 관측 사각).

---

## 3. 골든 4조건 자체점검표

| 조건 | 판정 | 근거 |
|---|---|---|
| ① 코드/인프라 앵커 | ✅ | OutboxRelay.java:21-40 (제어점=`@ConditionalOnProperty` line21, P5 send().get() line39) + application.yml:36. TransferService 단일 tx로 200 독립 확인. P5 정확 매핑. |
| ② 정답(answer-key) 작성 | ✅ | design-F18-P-metadata.json에 root_cause{target_kind=service, id=banking-transfer-relay, mechanism, code_anchor}. 코드 거동과 일치(relay 빈 미생성→null 적체). |
| ③ 감별 가능 | 🟡 | 감별 *설계* 완결(§2: 비동기 국한, lag 평탄 vs F04-R). 그러나 결정 지표 **`database.outbox_unpublished_count` query_id 미존재** — DB adapter는 selector 기반(raw SQL 아님)이라 신규 selector(`banking.outbox_unpublished_count` → `SELECT COUNT(*) FROM banking.outbox_events WHERE published_at IS NULL`) 서버측 구현 + queries.json 등록 필요. 설계 ✅ / 관측 배선 ❌. |
| ④ 주입 수단 실재 | 🟡 | **실 제어점 존재**: k8s.env executor(검증됨, F03-P/F08-P/F09-H 사용)가 env patch+rollout 수행 → OUTBOX_RELAY_ENABLED=false로 relay 무력화. 신규 executor 불요. 단 (a) k8s.env allowlist(APPROVED_TARGETS/APPROVED_KEYS/parameter_contract)에 F18-P 등록 필요, (b) rollout 재기동에 동기 경로 순간 blip 수반. 계약 확장 + caveat. |

**종합**: ✅✅🟡🟡 — 신규 executor/앱코드 없이 **관측 selector 1종 신설 + 계약 확장 2건**으로 승격 가능. manifest `readiness=draft`, `prerequisite_gate.state=blocked`.

---

## 4. injector gap 상세 (헌장 §5 정밀 판정)

헌장 §5는 "outbox relay가 앱 내 @Scheduled라 외부 제어점 없음 → blocked"라 단정했다. **이 판정을 정정한다**: 제어점은 실재한다.

- **@Scheduled 자체엔 런타임 토글 없음**은 맞음. 그러나 relay 빈 전체가 **`@ConditionalOnProperty(outbox.relay.enabled)`로 게이팅**되어 있고, 이 property는 평문 YAML(application.yml:36)이라 **env `OUTBOX_RELAY_ENABLED=false`로 Spring relaxed-binding override 가능**. 부팅 시 조건 재평가 → 빈 미생성 → relay 완전 정지. → **k8s.env(container env patch + rollout)가 정확히 이 일을 한다.** 즉 갭은 "제어점 부재"가 아니라 "런타임 무중단 토글 부재(rollout 필요) + 계약 미등록 + 관측 미배선".

- **왜 kafka.control(F04-R용)은 안 되나**: kafka_control_executor는 `kubectl scale deploy <consumer> --replicas=0` — *consumer 정지*다. transfer(producer/relay)에 쓰면 서비스 전체가 죽어 동기 200 경로까지 붕괴 → P5 정체성 파괴. 잘못된 표면.

- **대안 injector(Kafka 브로커 차단) 기각**: relay `send().get()` 실패로 null 적체는 재현되나, kafka-0 파드/네트워크 차단은 (1) 파드/브로커 다운으로 관측되어 "silent" 아님, (2) consumer·타 도메인까지 광역 blast, (3) F04-R must_rule_out(kafka-pod-failure)와 충돌. 기각.

**승격 전 배선 필요 (prerequisite_gate)**:
1. **관측 selector 신설 (조건 ③, 진짜 병목)**: `registry/queries.json`에 `database.outbox_unpublished_count`(adapter=database, selector=`banking.outbox_unpublished_count`, allowed_params=[namespace]) + **DB adapter 서버측 selector 구현**(`SELECT COUNT(*) FROM banking.outbox_events WHERE published_at IS NULL`). food 변형 시 MySQL 등가 selector.
2. **주입 계약 확장 (조건 ④)**:
   - `k8s_env_executor.py` APPROVED_TARGETS/APPROVED_KEYS에 `F18-P: (rca-testbed-banking, testbed-transfer, transfer-service)` / key `{OUTBOX_RELAY_ENABLED}` 추가 + `profiles.json` k8s.env.parameter_contract.allowed_scenarios·scenario_parameters(baseline=라이브 env 스냅샷 정확일치, fault=+OUTBOX_RELAY_ENABLED=false) 등록.
   - `profiles.json` load.north_south.allowed_scenarios·tag_pattern·scenario_parameters(F18-P)에 banking surge(`/opt/loadgen/core-banking/surge.js`, entry 30082, business_step=transfer, ~30rps) 추가. (계약은 이미 banking surge 지원 — 등록만.)
3. **caveat**: baseline env는 22-transfer-service.yaml의 현행 env 배열과 **정확일치**해야 executor `check()` 통과(현재 env: DB_USER/DB_PASS/JAVA_TOOL_OPTIONS/OTEL_*). rollout 중 동기 경로 순간 blip → settle 창(30s)으로 흡수.

---

## 5. 헌장 부합성 평가 (한 문단)

F18-P는 헌장의 4조건 루브릭이 §5의 능력갭 단정마저 검증 대상으로 되돌린 사례다. ①②는 relay 빈의 게이팅 지점(`@ConditionalOnProperty` OutboxRelay.java:21)과 동기 200 독립성(단일 @Transactional 내 outbox INSERT)을 코드로 확정해 완전 충족했고, ③④는 "제어점은 있으나 무중단 토글·관측이 없다"를 정직하게 🟡로 분리했다. 가장 값진 발견 둘: (a) 헌장 §5가 "외부 제어점 없음"이라 한 것은 부정확 — `@ConditionalOnProperty`+env override라는 실 제어점이 있어 k8s.env로 relay를 rollout-disable할 수 있고, 갭은 "런타임 토글 부재+계약 미등록+관측 미배선"으로 좁혀진다. (b) 과업 명세가 must_support로 든 "consumer lag 증가"는 실제로 F04-R(consumer 정지)의 시그니처이며, relay(producer) 정지에서는 생산 중단으로 lag가 0 수렴한다 — 즉 "outbox null 적체↑ AND lag 평탄"이 F04-R과 F18-P를 가르는 정체성 축이다. 4조건 게이트가 없었다면 이 시나리오는 F04-R과 사실상 동일한 lag-증가 manifest로 얕게 복제됐을 것이다. 결론: readiness=draft/gate=blocked로 정직 표기하되, 갭은 executor 신설이 아닌 selector 1종+계약 2건이라 승격 경로가 짧다.
