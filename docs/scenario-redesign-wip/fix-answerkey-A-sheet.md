# FIX answer-key A그룹 — F02-H / F03-R / F04-H / F04-P / F14-H

기준: spec-scenario-design-charter.md §2 골든4조건 / §5 능력갭. 본보기: pilot(F16-H), design-F18-P.
근거는 전부 실측 file:line. 처분 = {정답작성완료 | 재라벨 | cut권고}.

---

## F02-H — commerce-storage-io  → **정답작성완료 (Class B / SMS·disk)**

감사 소견("blocked, PVC 전제, injector gap")을 **정정**: injector는 이미 실재한다.
`host_stress_executor.py:12`에 F02-H 계약이 등록돼 있음:
`{"mode":"fio","host":"192.168.122.184","target_dir":"/opt/local-path-provisioner/pvc-5d71e22a…_rca-testbed-commerce_pgdata-testbed-postgres-0","size_mib":2048,"runtime_seconds":600,"rate_iops":3000}`
즉 PG PVC는 tb-w1(192.168.122.184)의 local-path-provisioner **hostPath 디렉토리**이고, fio가 그 디바이스에 IO 포화를 건다. F10-R/H/P가 동일 방식(fio/watermark, MySQL·Oracle PVC)으로 이미 검증됨 → PVC IO 주입은 입증된 능력.

| 조건 | 판정 | 근거 |
|---|---|---|
| ① 앵커 | ✅ | Class B 인프라. root=PG PVC backing device(tb-w1 로컬디스크) IO 포화. 코드 아님. 공유 단일 PostgreSQL(8스키마, README:119)이 느려져 commerce 전반 지연. 조작수단=fio. |
| ② 정답 | ✅ | root=스토리지 IO 포화(호스트 디스크), 증상=commerce 전 서비스 지연(공유 PG 경유). 코드 결함 아님을 명시. |
| ③ 감별 | 🟡 | 설계는 가능(디스크 await/iops↑ = SMS 디스크지표 vs F09 CPU/F05 mem vs 코드 DB락 F01). 그러나 **호스트 디스크 IO 지표 query_id 배선 필요**(현 default_queries=http.entry_health뿐). CPU/mem host-stress와 감별하려면 disk metric 필수. |
| ④ 주입 | ✅ | host_stress_executor CONTRACTS['F02-H'] fio 모드 실재 + bounded recovery(파일/프로세스 정리). 신규 코드 불요. |

**배관 gap**: (a) `profiles.json` host.stress.parameter_contract.allowed_scenarios에 F02-H 없음(F09-R/F05-P만) → F02-H + scenario_parameters 등록 필요. (b) load.north_south.allowed_scenarios에 F02-H 없음 → 등록(commerce surge 30080/checkout). (c) 디스크 IO 관측 query_id 신설(SMS disk await/util).
answer-key JSON: `fix-F02-H-metadata.json`.

---

## F03-R — food-payment-connection-leak  → **CUT권고 (실코드 근거 없음)**

실코드 조사 결과 "connection leak"을 만드는 코드 경로가 **없다**:
- food payment의 RestClient는 `SimpleClientHttpRequestFactory`(RestClientConfig.java:19,30) = **HTTP 커넥션 풀 자체가 없음** → HTTP 커넥션 릭 표면 부재.
- DB는 Hikari(15) + `@Transactional`(PaymentService.java:51) 관리 → 커넥션 릭 경로 없음(정상 close).
- 의존 injector = app.release 기반 **미빌드 fault 이미지**. `profiles.json` app.release `live_supported=false`.

| 조건 | 판정 | 근거 |
|---|---|---|
| ① 앵커 | ❌ | leak을 내는 코드 없음(풀 없음+tx관리). P2-인접이라던 소견의 실체 = 롱tx 풀고갈(C1)이지 leak 아님. |
| ② 정답 | ❌ | 코드 거동과 일치하는 "leak" 정답 작성 불가(억지 금지). |
| ③ 감별 | — | 해당 없음. |
| ④ 주입 | ❌ | fault 이미지·app.release(live=false) 부재. leak 주입수단 없음. |

**권고**: CUT. 굳이 살리면 **C1(롱tx Hikari-15 고갈)**로 재앵커해야 하나 이는 (a) leak이 아니라 P1 풀고갈이고 (b) PG mock 지연 injector(mock.expectation/latency) 필요 — food 지연계열과 중복. answer-key 미작성(blocked 사유=실코드 leak 부재 + injector 부재).

---

## F04-H — order-outbox-relay-stop (commerce, P5)  → **정답작성완료 (unblock via k8s.env)**

F18-P(banking)에서 밝혀진 메커니즘의 **commerce 정합판**. 실측으로 동일 제어점 확인:
`commerce/commerce-common/.../outbox/OutboxRelay.java:20` `@ConditionalOnProperty(prefix="outbox.relay",name="enabled",havingValue="true")`, `:33-40` `@Scheduled(2s)`+`kafkaTemplate.send().get()`. order-service `application.yml:38-40` `outbox.relay.enabled: true`(평문). env `OUTBOX_RELAY_ENABLED=false` + rollout로 빈 미생성 → relay 정지.

| 조건 | 판정 | 근거 |
|---|---|---|
| ① 앵커 | ✅ | OutboxRelay.java:20,33-40 + order yml:38-40. P5(silent async). checkout은 별 tx라 동기 200 독립. |
| ② 정답 | ✅ | root=commerce order relay 정지 → commerce.orders 미발행 → shipping 배송생성/notification 정지 + order_schema.outbox_events published_at=null 적체. 동기 checkout 200. |
| ③ 감별 | 🟡 | 설계 완결(vs **F04-R**: consumer(shipping) 정지=lag↑; F04-H=relay 정지→생산중단→lag 평탄/0 + outbox null↑). **lag는 must_rule_out**(증가 아님). 배선 gap: `database.outbox_unpublished_count`(order_schema) selector 미존재(F18-P와 공유). shipment row 미생성은 관측 가능. |
| ④ 주입 | 🟡 | k8s.env executor 실재·검증(F08-P가 동일 testbed-order/order-service 타깃·baseline 사용). 신규코드 불요. **현 manifest injector=kafka.control은 오배선**(kafka/consumer 정지지 relay 정지 아님) → k8s.env로 교체 필수. |

**배관 gap**: (a) manifest profile_refs kafka.control→**k8s.env** 교체. (b) `k8s_env_executor.py` APPROVED_TARGETS/KEYS + `profiles.json` k8s.env.parameter_contract에 F04-H 등록: namespace=rca-testbed-commerce, deployment=testbed-order, container=order-service, baseline=order OTEL env 배열(20-order-service.yaml:53-81 실측: DB_USER/DB_PASS/JAVA_TOOL_OPTIONS/OTEL_SERVICE_NAME=commerce-order/OTEL_* 11개), fault=baseline+OUTBOX_RELAY_ENABLED=false. (c) load.north_south allowed_scenarios에 F04-H(commerce surge). (d) outbox_unpublished_count DB selector(order_schema).
answer-key JSON: `fix-F04-H-metadata.json`.

---

## F04-P — banking-ledger-rate-limit (P5/FS-3)  → **정답작성완료 (injector 🟡)**

단일스레드 consumer lag. 실측: `ledger-service TransferEventConsumer.java:28` `@KafkaListener(groupId="ledger-service")`, ledger `application.yml:25-31` **concurrency 미설정=기본 1스레드**. `LedgerService.recordTransfer`가 매 건 2 insert+멱등조회 → 처리속도<유입속도면 lag. transfer/account/api는 200(이체 성공).

| 조건 | 판정 | 근거 |
|---|---|---|
| ① 앵커 | ✅ | ledger yml:25-31(concurrency 미설정→단일스레드) + TransferEventConsumer.java:28-39 + LedgerService recordTransfer 2insert/event. FS-3. Class A(코드=동시성 상한 부재). |
| ② 정답 | ✅ | root=ledger 단일스레드 consumer 처리량 병목 → banking.transfers lag → ledger_entries 최신 이체 미반영. 동기 이체 200. 5xx 없음(silent). |
| ③ 감별 | 🟡 | vs **F04-R**(consumer 정지=lag 총량까지↑, 재시작 전 drain 없음) & **F18-P**(relay 정지=lag 평탄/0). F04-P=surge 중 lag↑, surge 후 drain. `kubernetes.kafka_consumer_lag` query 실재 ✅. ledger 반영지연 DB selector(ledger_entries freshness) 미존재. |
| ④ 주입 | 🟡 | **진짜 rate-control injector 부재.** 현 manifest kafka.control은 replica scale(0/n)만 = consumer *정지*(=F04-R 의미) → 오배선. 실 구동경로=load.north_south **banking surge 고rps**로 concurrency=1 병목 착취(신규 injector 불요)이나 (a) 지속 lag 재현 신뢰성 미검증 (b) banking이 load.north_south allowed_scenarios·domain_profiles(30082/transfer)에는 있으나 F04-P 미등록. 대안=consumer-throttle 훅 신설(없음). |

**배관 gap**: (a) manifest injector kafka.control→load.north_south(banking surge) 재정의, 또는 consumer-throttle 훅 확보 전 blocked 유지. (b) load.north_south allowed_scenarios에 F04-P + banking surge(/opt/loadgen/core-banking/surge.js, 30082, transfer, 고rps). (c) ledger 반영지연 DB selector.
answer-key JSON: `fix-F04-P-metadata.json`. (root=ledger lag, 동기 200 — 과업 명세와 일치.)

---

## F14-H — pricing-correctness-bug  → **CUT권고 (실코드 잠재 correctness 결함 부재)**

실코드 재조사(PricingService.java 전체) 결과 **주입 가능한 잠재 correctness 결함이 없다**:
- 유일한 실코드 quirk = **stale promotion 캐시**: `refreshPromotionCache` cron `0 0 * * * *`(매시) + PostConstruct(:48-60), quote는 `activePromotionsCache`(:38,90,126)만 읽음. 만료 프로모션이 최대 1h 계속 적용/신규 미적용 = 가격 드리프트. 그러나 (a) 코드 주석(:36-37)이 명시한 **설계상 tradeoff**이지 결함 아님, (b) 시간기반 수동(분 단위 시나리오 창에서 발현 불가) = **주입수단 없음**, (c) 가격정합 관측 지표 부재 = 관측 불가.
- 쿠폰 `discountAmount + percentPart` 동시적용(:104-106)은 쿠폰이 두 필드를 갖는 **의도된 동작**.

| 조건 | 판정 | 근거 |
|---|---|---|
| ① 앵커 | ❌ | 잠재 결함 없음. 원 설계의 root=주입한 버그(존재않는 fault 이미지). stale-cache는 설계 tradeoff. |
| ② 정답 | ❌ | 억지 answer-key 금지(과업 지시). |
| ③ 감별 | — | 해당 없음. |
| ④ 주입 | ❌ | app.release(live=false)+미빌드 fault 이미지. 인위 버그 이미지 주입은 §2-A(인위왜곡 Class A 위장 금지) 위배. |

**권고**: CUT 또는 재설계(백로그). 재설계 시 = "무결성/가격드리프트" Class A로 재출발하려면 (i) 관측배선(`database.*` 가격정합 selector) + (ii) 데이터 주입수단이 선행 — 현 능력 밖. answer-key 미작성.

---

## 5개 배관 gap 종합
1. **profiles.json 계약 등록 누락**: F02-H(host.stress+load.north_south), F04-H(k8s.env+load.north_south), F04-P(load.north_south banking). executor 코드는 F02-H 이미 지원.
2. **manifest injector 오배선 2건**: F04-H·F04-P의 kafka.control → 각각 k8s.env / load.north_south로 교체.
3. **관측 selector 신설**: `database.outbox_unpublished_count`(F04-H, order_schema; F18-P와 공유), ledger 반영지연 selector(F04-P), 호스트 disk IO 지표(F02-H SMS).
4. **k8s_env_executor APPROVED_TARGETS/KEYS 확장**: F04-H(testbed-order/OUTBOX_RELAY_ENABLED).
5. **능력 부재(cut 사유)**: F03-R(실코드 leak 경로 없음+fault이미지 부재), F14-H(잠재 correctness 결함 부재+주입수단 부재).
