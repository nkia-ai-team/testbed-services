# Audit Batch2 — F04~F06 계열 (12개)

대조: fault-surface-{commerce,food-delivery,core-banking}.md / P1~P7 앵커

| id | readiness | primary 결함(주입) | answer-key(golden root cause) | 앵커 | answer-key 정합성 | 코드 앵커 | 판정 | 한 줄 근거 |
|---|---|---|---|---|---|---|---|---|
| F04-R commerce-shipping-consumer-stop | ready/live | shipping deploy scale=0 (Kafka consumer 중단) | shipping consumer outage → 주문·결제 성공하나 배송 생성 SLA 위반, outbox publish 정상·lag 증가·shipment row 미생성 | **P5** (silent async, 5xx 없음) | 정합 — success가 shipping_replicas=0 & lag>0만 요구, 5xx 기대 안 함(옳음) | shipping OrderEventConsumer.java:32 (PAID만 shipment 생성) | ✅ | P5 정확: 5xx 없는 배송 드리프트가 정답. 주입이 scale=0(env)이나 "consumer outage"의 충실한 재현 |
| F04-H order-outbox-relay-stop | **blocked** | order outbox relay 정지 | (metadata 없음) 의도상 outbox→Kafka 발행 정지 → 동기 200인데 다운스트림 드리프트 | **P5** (이상적 표본) | N/A(답키 없음). live_enabled=false, prereq "outbox relay control" unresolved | OutboxRelay.java:33-47 (@Scheduled send().get()) | 🟡 | P5 교과서 케이스지만 **relay 정지 주입수단 부재**(앱 내 @Scheduled, injection endpoint 없음)로 blocked=능력갭 |
| F04-P banking-ledger-rate-limit | **blocked** | ledger consumer rate 제한 | (metadata 없음) ledger 단일스레드 consumer lag → 이체 200 성공, 원장만 지연 | **P5** (FS-3) | N/A. prereq "ledger consumer rate control" unresolved | ledger TransferEventConsumer.java:28 (concurrency 미설정=1스레드) | 🟡 | FS-3와 일치하나 consumer rate 제어 주입수단 없음 → blocked 능력갭 |
| F04-G short-broker-outage | **partial** | Kafka pod lifecycle 단기 중단+catch-up | (metadata 없음) 짧은 브로커 중단은 outbox at-least-once로 흡수·복구(가드레일) | **P5** (흡수형) | N/A. prereq "safe outage/catch-up duration" unresolved | OutboxRelay.java(재시도), OrderEventConsumer(catch-up) | 🟡 | 가드레일 의도(무영구영향)는 타당하나 안전 중단시간 미확정 → partial |
| F05-R payment-oom-loop | ready/live | payment mem limit 1Gi→768/640/576Mi 축소(ladder)+checkout 부하 | Payment container memory limit exhaustion → OOMKilled 직접관측·restart↑, host mem pressure 정상 | **패턴외** (인프라조작) | 자기정합 O — success가 termination=OOMKilled & restart>=1 & liveness_unchanged로 대안 배제. 답키가 거동과 일치 | k8s/23-payment-service.yaml resources; JVM heap | 🟡 | **코드결함 아닌 순수 limit 축소(인위조작)**. 답키는 정합하고 F05-H/F05-P와 표면 분리 잘 됨 |
| F05-H payment-liveness-loop | ready/live | livenessProbe path→/actuator/health/f05-h-fail(항상 404) | Misconfigured liveness probe → termination=Error·restart loop, OOMKilled·mem pressure 없음 | **패턴외** (config조작) | 정합 O — success가 termination=Error & restart>=2 & liveness_matches_baseline=false & resources_unchanged. OOM 대안 배제 | k8s/23-payment-service.yaml:80 (baseline probe 확인됨) | 🟡 | 순수 probe 오설정 주입(코드결함 아님). 답키 정합, F05-R(OOM)과 termination_reason으로 감별 |
| F05-P worker-memory-eviction | ready/live | tb-w2 node python memhog(5500→8500MiB ladder) | Worker node memory exhaustion → kubelet eviction, 다도메인 동시 5xx, 앱 heap 정상 | **패턴외** (P7 인접: node blast) | 정합 O — success가 node_mem>=92 & 5xx>=0.1 & node_ready & rps<=60. cohort 검증·F09-R(CPU)/F09-H(heap) 감별 명시 | 없음(인프라) — cohort placement 검증 로직 | 🟡 | 순수 노드 인프라 조작이나 계측·감별 매우 견고. 답키 정합 |
| F05-G invalid-image-no-impact | ready/live | payment invalid image rollout | **No incident** — 기존 ready replica가 서비스 유지, ImagePullBackOff 있으나 사용자 경로 정상 | **가드레일(G)** | 정합 O — success가 image_pull_failed=true & replicas>=1 & business_ok & user-impact(entry>=500) 배제. "장애 아님"이 정답 | k8s rollingUpdate maxUnavailable(기존 replica 유지) | ✅ | 가드레일 정답 명확: 실패 rollout이 기존 replica로 봉쇄됨 |
| F06-R commerce-external-hang | ready/live | external-pg-mock /v1/payments 30s delay | External PG hang → checkout timeout/재시도 실패, 외부 span 장기대기, DB lock·banking 정상 | **P4**(외부 PG hop CB 없음)+P2(txn timeout 없음) | 정합 O — success가 entry>=500 & tagged_db_sessions=0(DB lock 배제). F06-H 감별짝 | PgApiClient(CB/Retry 없음,10s), order→payment read15s×retry2 | ✅ | P4/P2 정확. 단 success의 entry_status>=500이 게이트 health면 미발화 위험 → checkout_5xx 미사용은 소캐비엇 |
| F06-H payment-db-lock | ready/live | payments 테이블 EXCLUSIVE LOCK(tagged 세션, pg_sleep 600) | Internal PG table lock on payments → INSERT 블록·order read-timeout 초과 5xx, 외부 PG 정상 | **P3 인접**(무제한 lock, 주입형) | 정합 O — success가 tagged_session>=1 & 5xx>=0.05 & order_error>=0.1 & rps>=15, must_rule_out tagged=0. 근거 상세(payments는 매번 INSERT라 테이블락이 유일표면) | metadata 코드근거 상세(07-20 실측) | ✅ | F06-R 내부DB 감별짝. 주입형 락이나 설계의도·근거 견고 |
| F06-P food-partial-429 | **blocked** | food external-pg-mock 부분 429 | (metadata 없음) 부분 429 degradation | **패턴외/의심** | N/A. prereq "food courier pool recovery" unresolved | food fault-surface: 429=ClientErrorException(4xx)→CB ignore(정상거절) | 🟡 | blocked 능력갭 + **429는 4xx biz-reject라 CB가 안 여는 정상거절** → 장애 의미 모호(답키 부재로 검증불가) |
| F06-G transient-5xx-absorbed | ready/live | mock /v1/payments 짧은 500 pulse(remaining=1,ttl10s) | **No incident** — retry/CB가 흡수, 최종 주문 성공·결제 정합 유지, 중복결제 없음 | **가드레일(G)** | 정합 O — success가 5xx=0 & business_ok & transient_consumed>=1 & no-dup-payment 등 광범위 불변식. 매우 견고 | payment PgApiClient Retry(pg max=2), CB | ✅ | 가드레일 정답 명확, 중복결제/expectation 소비까지 검증하는 최강 계측 |

## 요약
- ✅ 탄탄: 5 (F04-R, F05-G, F06-R, F06-H, F06-G)
- 🟡 의심: 7 (F04-H, F04-P, F04-G blocked/partial 능력갭 · F05-R/F05-H/F05-P 순수 인프라조작 · F06-P blocked+429 의미모호)
- ❌ 근거없음: 0

## 핵심 관찰
- **F04 계열은 P5(silent async)에 정확히 정렬**. F04-R은 답키가 올바르게 "5xx 없는 배송 드리프트"를 정답으로 잡음(5xx 잘못 기대 안 함). F04-H/P/G는 P5의 이상적 표본이나 **주입수단 부재로 blocked/partial**(relay·consumer가 앱 내 @Scheduled/consumer라 외부 제어점 없음) = 능력갭이지 답키 오류 아님.
- **F05 계열은 전부 순수 인프라/config 조작**(limit 축소·probe 파손·node memhog)이며 P1~P7 코드결함 아님. 그러나 답키는 termination_reason·mem pressure·cohort로 서로 감별되게 정합. F05-G는 가드레일 정답.
- **F06-R↔F06-H는 의도된 감별짝**(외부 hang vs 내부 DB락, 동일 checkout 증상), 둘 다 근거 견고. F06-G는 가드레일 최강 계측.
