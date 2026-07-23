# 배치3 감사 — F07~F09 계열 (12개)

기준: manifests/*.yaml, catalog.json, registry/{controllers,scenario-metadata}.json + 3도메인 fault-surface 실측 지도.
주: answer-key(root cause 서술)는 registry/scenario-metadata.json에만 존재. blocked/partial 4종(F07-R·F07-G·F08-R·F09-G)은 metadata 항목 자체가 **NOT FOUND** = answer-key 미작성.

| id | readiness | 앵커 패턴 | answer-key(cause) | 실코드 정합 | 코드앵커 | 판정 | 한 줄 근거 |
|---|---|---|---|---|---|---|---|
| **F07-R** cross-domain-retry-storm | blocked | P4+P7 (retry 불균일·크로스도메인) | 없음(metadata NOT FOUND) | 검증불가 | 없음(transport unresolved, "safe delay injection point" 미해결) | ❌ | answer-key 부재 + 주입경로 미정의. 개념(retry storm)은 실재(api/account 3×3=9 hop)하나 아무것도 정의 안 됨 |
| **F07-H** north-south-surge | ready(live) | 패턴외(순수 부하) | North-south traffic surge | 정합(부하=원인 자체) | 없음(코드 결함 아님) | 🟡 | 부하가 증폭기가 아니라 **원인 그 자체**. adaptive ladder로 rps knee까지 밀어 포화. 정직한 capacity 인시던트지만 구조 결함 앵커 0 |
| **F07-P** pricing-bulkhead-saturation | ready(live) | 패턴외(bulkhead 동시성 상한) | Pricing bulkhead concurrency saturation | ✅정합 | ✅ PricingService.java:77, pricing yml:32-36 (@Bulkhead 20/wait0) | ✅ | east-west k6가 /api/pricing/quote 포화→503 BulkheadFullException 즉시거절. 실제 bulkhead·reject surface 코드일치. 부하구동이나 신호 특정적 |
| **F07-G** cb-contained-downstream | partial | 가드레일(CB 격리) | 없음(NOT FOUND) | **불일치** | 부분(대상 잘못) | 🟡 | commerce payment→PG는 **CB 없음**(PgApiClient CB/Retry 부재). "CB-contained"는 commerce PG hop엔 거짓. 진짜 CB-격리는 cart-Redis fallback인데 external-pg-mock을 겨냥. prereq "fallback" 미해결 |
| **F08-R** faulty-pricing-release | blocked | (배포결함) | 없음(NOT FOUND) | 검증불가 | 없음("pricing fault image" 미존재) | ❌ | 고의로 깨진 pricing 이미지가 존재해야 하는데 없음. answer-key도 없음. 정직히 blocked |
| **F08-H** rollout-plus-external-fault | ready(live) | P1(외부 hop) + distractor | External payment failure coincident w/ deploy | ✅정합 | ✅ 외부 PG timeout 경로(payment yml, PgApiClient) | ✅ | 무영향 rollout(distractor)+외부 PG fault 합성. old/new pod 모두 동일 실패=배포 무죄 판별. cross-check 타당 |
| **F08-P** low-read-timeout-config | ready(live) | 패턴외(설정 오배포) | Misconfigured read-timeout 250ms | 정합하나 **실결함의 정반대** | ✅ 15s 리터럴 존재(order yml:54), 250ms env override | 🟡 | **핵심 의심 확인**: 실제 구조결함은 read 15s + @Transactional timeout 부재 = **무한/과대 timeout**. 250ms 합성은 그 정반대(조기 컷). 실재 latent defect 아닌 인위 config. 다만 config-misdeploy는 유효 원인클래스라 ❌ 아닌 🟡. **ready+live로 출고된 게 문제** |
| **F08-G** distractor-rollout-oracle-lock | ready(live) | P3(row-lock)+P7(크로스도메인)+distractor | banking Oracle settlement row lock (notification 배포=무관) | ✅정합 | ✅ transfer FOR UPDATE NOWAIT없음(AccountRepository.java:18-20), 크로스도메인 30282 직행 | ✅ | Oracle blocking session이 transfer 대기→commerce payment timeout→checkout 5xx. banking FS-1 + commerce C4 결합. distractor(notification) 무죄 판별. lock은 주입세션이나 실 lock-wait 거동에 앵커 |
| **F09-R** worker-cpu-noisy-neighbor | ready(live) | 패턴외(host CPU 경합) | Worker node CPU contention (co-located noisy proc) | 정합(노드 cohort 신호) | 없음(yes busy-loop 주입, 코드결함 아님) | 🟡 | tb-w3에 yes 루프로 노드 CPU 포화→같은 노드 pod cohort p95 동반상승. 유효 infra 클래스지만 순수 host stress, 코드 앵커 0 |
| **F09-H** order-gc-pressure | ready(live) | 패턴외(JVM heap 축소) | Undersized JVM heap → STW GC | 정합(GC pause 실거동) | 없음(-Xmx env override, 코드결함 아님) | 🟡 | JAVA_TOOL_OPTIONS -Xmx 256→176 ladder로 heap 좁혀 GC pause. restart_count 0 pre-OOM 유지. env 인위조작, 구조결함 아님 |
| **F09-P** inventory-cpu-throttle | ready(live) | 패턴외(k8s CPU limit 축소) | Inventory container CPU throttling (undersized limit) | 정합(throttle 실거동) | 없음(k8s.patch limit 인하) | 🟡 | inventory CPU limit 인하→throttle→재고예약 지연. inventory 락/배치는 실 CPU작업이나 fault는 limit 조작. live 검증됨이나 env 인위 |
| **F09-G** batch-cpu-no-user-impact | blocked | 가드레일(무영향 확인) | 없음(NOT FOUND) | **전제 의심** | 없음("independent batch trigger" 미해결) | 🟡 | 토폴로지에 **독립 batch host 없음**(Reconciliation/Settlement 배치는 서비스 pod 내 @Scheduled). "batch host" 주입지점 전제가 실제와 불일치. 정직히 blocked이나 개념 근거 약함 |

## 집계
- ✅ 탄탄: **3** — F07-P, F08-H, F08-G
- 🟡 의심: **7** — F07-H, F07-G, F08-P, F09-R, F09-H, F09-P, F09-G
- ❌ 근거없음: **2** — F07-R, F08-R (둘 다 blocked + answer-key 미작성)

## 가장 문제되는 Top 3 (출고된 것 우선)
1. **F08-P** — 250ms read-timeout 합성이 실제 latent 결함(read 15s+@Transactional timeout 부재=무한/과대 timeout)의 **정확히 정반대**. 진짜 구조결함을 재현하는 대신 반대 방향 config를 인위 주입했는데 **ready+live_scenario_ids로 출고**됨. config-misdeploy 자체는 유효 클래스라 폐기까진 아니나, "실결함 반영"이라 오해되면 answer-key가 오도적.
2. **F09 계열(R/H/P) 전부** — 모두 ready/live인데 host CPU busy-loop · JVM -Xmx · k8s CPU limit **순수 환경 조작**. 코드 결함 앵커 0. noisy-neighbor/throttle/GC는 유효 infra 인시던트지만 7개 구조패턴 어디에도 안 걸림 = "코드 결함 재현" 서사와 어긋남. 3개가 한꺼번에 env-stress로 채워짐.
3. **F07-G** — "CB-contained downstream" 가드레일이 겨냥한 commerce payment→PG hop엔 **CB가 존재하지 않음**(PgApiClient CB/Retry 부재). 전제가 코드와 불일치. 진짜 CB-격리 사례는 cart-Redis fallback인데 external-pg-mock을 지목. partial·prereq 미해결로 남아있으나 answer-key 작성 시 오도 위험.
