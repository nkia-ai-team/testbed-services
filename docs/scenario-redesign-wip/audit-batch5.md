# Batch5 감사 — F13~F15 계열 (16개)

## 핵심 구조 발견
- **16개 중 answer-key(scenario-metadata.json)가 실제 존재하는 건 3개뿐**: F15-G, F15-R, F15-T1 (모두 readiness=ready, live 등재됨). 나머지 13개는 `catalog.json` 스텁(prerequisite 텍스트)만 있고 root cause/distinguishing_evidence가 **미작성**. → 13개는 "answer-key 정합성"을 검증할 대상 자체가 없음(설계 스텁 단계). 이들은 원리적으로 ✅ 불가.
- 13개는 memory가 지목한 진짜 갭과 일치: **네트워크(F13-H)/WPM 프로브(F13-G/R)/fault 이미지(F14-H/R)/selective-drop injector(F14-P)/composite dispatch 배선(F15-H/T2/T3/T4)** — 능력 부재로 blocked.

## id별 판정표

| id | 앵커 패턴 | answer-key | 코드 앵커 | 판정 | 한 줄 근거 |
|---|---|---|---|---|---|
| F13-G single-probe-failure | 패턴외(관측성) | 없음 | 없음(앱 무관) | ❌ | WPM 프로브 1곳 다운=관측 사각 아티팩트, 서비스 영향 0, transport unresolved. RCA 케이스로서 실체 미약 |
| F13-H connect-phase-network-delay | 패턴외(네트워크 인프라) | 없음 | 없음(tc netem 인프라) | 🟡 | connect 단계 지연=인프라 fault. 정직하게 blocked, 앱코드 무관·answer-key 미작성 |
| F13-P large-history-download | 패턴외(응답 페이로드) | 없음 | 약(무제한 조회/대형 응답) | 🟡 | banking 대형 이력 조회→대형 직렬화/슬로우쿼리(FS-7 근접). 데이터 시딩 필요, answer-key 없음 |
| F13-R checkout-edge-ttfb-delay | 패턴외(엣지 지연 인프라) | 없음 | 없음(ingress patch) | 🟡 | 엣지 TTFB 지연=ingress/인프라. 앱코드 아님, WPM TTFB 계약 필요, answer-key 없음 |
| F14-G compensation-final-consistency | **P7**(무결성/보상) | 없음 | 강(commerce saga 롤백+inventory release, C4/C1) | 🟡 | PG mock 실패→결제 롤백→재고 보상 최종정합 검증. 코드 앵커 견고하나 business-invariant probe 미비·answer-key 없음 |
| F14-H pricing-correctness-bug | 패턴외/P5근접(무증상 오류) | 없음 | 없음(합성 버그 이미지 필요) | 🟡 | 잘못된 가격 산출=5xx 없는 정합성 위반(P5류 취지 맞음). 그러나 root=주입한 버그이지 잠재 구조결함 아님. fault 이미지 부재 |
| F14-P selective-ledger-loss | **P5**(silent async/유실) | 없음 | **강**(banking FS-10: TransferEventConsumer catch-swallow+offset commit, ledger imbalance) | 🟡 | 원장 이벤트 선택적 유실→DEBIT≠CREDIT, 이체는 200. P5 정통 앵커. selective-drop injector·answer-key 미구현일 뿐 근거 최강 |
| F14-R non-idempotent-response-loss | **P5/P7**(중복/드리프트) | 없음 | 강(order createOrder 멱등키 부재) | 🟡 | 응답 유실→재시도→비멱등 중복 생성. 코드상 멱등키 없음 앵커됨(F15-R의 must_rule_out 대칭). response-loss proxy 부재 |
| **F15-G dual-db-lock-multiroot** | **P3∧P3**(독립 이중 락) | **있음** | **강**(commerce inventory PESSIMISTIC C2 + banking Oracle accounts FOR UPDATE FS-1, 동일 checkout trace) | ✅ | offset0 동시 주입 두 독립 락, 상호 인과 없음→has_single_cause:false 정당. 두 root 모두 checkout 5xx 독립 유발. 코드 지지 |
| F15-H pg-lock-food-429-simultaneous | P3+? multi-root | 없음 | **불일치**(food 429 앵커 없음) | 🟡 | commerce PG 락은 앵커되나 **"food-429"는 코드에 없음**: food 용량소진=503 "Courier pool exhausted"(DispatchService.java:62), 429 아님. PG mock 429 주입해도 food는 4xx 전파(비장애). 상태코드 명명 결함 |
| F15-P common-node-pressure | P7근접(공유자원 단일root) | 없음 | 약(공유 worker 노드) | 🟡 | 공통 노드 CPU/mem 압박→동거 서비스 상관증상=하나로 merge돼야 함(F15-T1 anti-merge의 정반대). 인프라 fault, placement topology 필요 |
| **F15-R pg-429-flapping** | **P4/외부 rate-limit 재발** | **있음** | **강**(commerce PgApiClient.java: RestClientException→502, CB/Retry 없음) | ✅ | 429는 commerce에서 HttpClientErrorException→catch(RestClientException)→**502**로 확정 매핑→checkout 5xx. 검증함. episode 재발 인지+order중복0으로 F14-R와 분리. answer-key 코드 지지 |
| **F15-T1 pg-lock-food-oom-exact** | **P3(락)+인프라OOM** 독립 2root | **있음** | 강(commerce inventory 락 C2 + food payment memory-limit OOMKill) | ✅ | 이종 2 root(commerce PG vs food k8s mem), 공유 인프라 없음→anti-merge 인시던트2개 정당. F05-R ladder 재사용. 지지됨 |
| F15-T2 pg-lock-then-food-429 | P3→? 순차 multi-root | 없음 | **불일치**(food 429 앵커 없음) | 🟡 | F15-H의 순차판. 동일 "food-429" 결함(food=503). PG락 앵커되나 handoff 대상 상태코드 미근거 |
| F15-T3 host-cpu-nested-consumer-stall | **P5**(async lag)+인프라 | 없음 | 중(consumer 단일스레드 FS-3/notify) | 🟡 | host CPU 압박→중첩 consumer stall→lag. partial. consumer SLA/placement 미확정, answer-key 없음 |
| F15-T4 pg-lock-handoff-kafka-lag | **P3→P5** 순차 2root | 없음 | 중(PG락 C2 + outbox/Kafka lag C5/FS-4) | 🟡 | 락 인시던트→해소→Kafka lag 인시던트 handoff. 인시던트 경계 테스트. partial, hand-off interval 미확정 |

## 집계
- ✅ 탄탄: **3** (F15-G, F15-R, F15-T1)
- 🟡 의심: **12**
- ❌ 근거없음: **1** (F13-G)

## 가장 문제되는 Top 3
1. **F15-H / F15-T2 "food-429" 상태코드 결함**: food-delivery 코드 어디에도 429 없음. 용량소진은 503(DispatchService.java:62 SERVICE_UNAVAILABLE). PG mock에 429 주입 시에도 food PgApiClient는 4xx를 ClientErrorException으로 **전파(CB 무시, 비장애 취급)** → composite가 노리는 5xx 영향 미발생. 두 시나리오의 명명·의도 root가 코드 거동과 불일치. (answer-key 미작성이라 아직 오답 고착은 아니나 승격 전 반드시 재정의 필요.)
2. **F13-G single-probe-failure**: 순수 관측성 아티팩트(프로브 1곳 다운). 서비스 영향 0, P1~P7 어디에도 앵커 안 됨, transport unresolved, answer-key 없음 → RCA 평가 케이스로서 실체가 가장 빈약. 배치 내 유일 ❌.
3. **F14-H pricing-correctness-bug**: root cause가 "주입한 버그"(존재하지 않는 fault 이미지)이지 실코드의 잠재 구조결함이 아님. 같은 F14의 F14-P(catch-swallow 유실)·F14-R(멱등키 부재)가 실코드 결함에 앵커되는 것과 대조적으로 "조작된 root" 위험 최고.

## 부가 관찰
- F15-G/T1의 "multi-root"는 한 전파사슬을 쪼갠 게 아니라 **물리적으로 분리된 2개 락/장애를 offset0 동시(또는 이종 도메인) 주입**한 합성 multi-root로, 상호 인과 없음이 코드/토폴로지로 지지됨(정당). F15-P는 그 반대(공유 노드→merge). 설계 축은 일관됨.
- F15-R의 429→502 매핑은 commerce vs food 도메인 차이가 결정적: **commerce PgApiClient는 모든 RestClientException(4xx 포함)을 502로 변환**(안전), food PgApiClient는 4xx 전파. F15-R이 commerce라서 성립. 도메인 혼동 시 재발 위험(F15-H가 그 함정에 빠짐).
