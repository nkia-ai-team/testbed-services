# 골든 4조건 전수 재감사 (2026-07-27)

대상: 카탈로그 60종 전건. 방법: 6그룹 병렬 정적 감사(매니페스트·registry·executor·앱 소스·러너 `live_probes.py` 직접 대조).
기준: `docs/spec-scenario-design-charter.md` §2 골든 4조건 + §2-A Class A/B 분리.
원칙: **통과 아니면 CUT. 중간 등급 없음.**

> 부하·클러스터 변경 없이 수행(당시 109에서 v3 캡처 진행 중). 라이브 실측이 필요한 항목은 §5 미확인 목록에 분리.

## 1. 집계

| 판정 | 수 | 의미 |
| --- | ---: | --- |
| KEEP | 15 | 4조건 실질 충족. 단 **전건 `root_cause` 구조 백필 필요** |
| 조건부 | 19 | 명시된 수리를 하면 KEEP |
| CUT | 26 | 폐기 |

CUT 26의 사유 분류:

| 사유 | 수 | 해당 |
| --- | ---: | --- |
| 음성(no-incident) | 4 | F01-G, F03-G, F05-G, F11-G |
| 전제가 사실이 아님 | 4 | F02-P(인덱스 사용 쿼리 부재), F06-P·F15-H(food 429 부재), F13-R(commerce Ingress 부재) |
| 합성 장애 | 1 | F03-H(`Thread.sleep`) |
| injector가 거부 stub / allowlist 밖 | 6 | F13-H, F13-P, F14-P, F14-R, F21-P, F21-Q |
| 정답·컨트롤러 통째 부재 | 8 | F10-H, F10-P, F10-R, F15-P, F15-T2, F15-T3, F15-T4, F04-P |
| 감별 불가(신호 부재) | 2 | F09-H, F09-P |
| cleanup 복구 불가(데이터 오염) | 1 | F17-P |

## 2. 판정표

### KEEP (15)

| ID | 근거 |
| --- | --- |
| F01-H | P4+P1. payment-service에 resilience4j 0건, `PgApiClient.java:31-40`→`PaymentClient.java:43,53` 502 사슬 코드 검증 |
| F01-R | P3 `InventoryRepository.java:17-19`. 유일하게 정상 배선된 tagged-session probe. **단 성공 게이트 수정 필수**(락 1행 vs 부하 16행) |
| F04-R | Class B + P5. kafka lag 실 probe, `OrderEventConsumer.java:28-38` 예외 삼킴이 silent failure 뒷받침 |
| F05-H | Class B(probe). F05-R과 Error↔OOMKilled 거울쌍 감별 대칭 |
| F05-R | Class B(limit). must_rule_out 3종이 F05-H·일반 과부하까지 배제 |
| F12-H | `kcm.pod.cpu_throttled_time`이 독립 결정지표(주입 되읽기 아님) |
| F15-R | P4 앵커 + MockServer 실주입 + 스냅샷 정확복원 |
| F18-P | `OutboxRelay.java:19-20` `@ConditionalOnProperty` 실제 제어점 + outbox 적체/lag 평탄 감별 |
| F19-P | P1 `OrderService.java:57/:128/:154` + hikari_pending 양쪽 등록 + mock food 승인 |
| F19-S | P4 `PgApiClient.java:32-33`. **단 "pg CB open"을 결정 증거로 서술했으나 관측 불가 — 문서 정정 필요** |
| F20-P | P8. `init.sql:46` 인덱스 존재 + `TransferRepository.java:33-36` trunc() 무력화 |
| F20-Q | unpaged + `GROUP BY DATE()`. **`dispatch-capacity-503` ruleout은 무효 → 제거** |
| F20-R | `idx_orders_user` 유일. "5xx 없이 타 스키마 동반 지연"이 신호 조합으로 구현 |
| F23-R | `ReconciliationBatch.java:46` env 제어점 + 관측 3종 양쪽 실구현. **class 라벨 필요** |
| F25-H | StatefulSet 일반화 실구현, OOMKilled는 자기충족 아님. **blast-radius 관측 보강 과제** |

### 조건부 (19)

| ID | 살리는 조건 |
| --- | --- |
| F01-P | 관측을 `database.oracle_tagged_session_count`로 교체 + `queries.json` 등록. 현재는 Oracle을 PG로 조회해 **실행 성공 불가** |
| F02-H | observations/success 신설 + 디스크 IO query 신설 + PVC 경로 실측 + `live_enabled` 승격 |
| F03-P | `class:"B"`(설정 오배포) 재라벨 + hikari_pending 배선. Class A 유지 시 §2-A 위반으로 CUT |
| F04-H | injector를 `kafka.control`→`k8s.env`로 교체(현재 의미 반대) + outbox selector 신설 + metadata/controller 신작 |
| F05-P | tb-w2 실배치 cohort 실측 → metadata·`required_cohort` 불일치 정정 |
| F06-H | **controllers.json에 `"parameters":{"scenario_tag":"rca-F06-H-payment-lock"}` 1줄 추가.** 없으면 성공 판정 영구 불가 |
| F06-R | success를 `checkout_5xx_rate`로 교체 + mock 적용 확인 관측 추가(현 2조건이 단일샘플·동어반복) |
| F07-H | `APPROVED_HIKARI_SERVICES`에 commerce-order 추가 + 메트릭 방출 실측. 실패 시 CUT |
| F07-P | `root_cause` 구조 작성 + P-패턴 사전 공백 처리(bulkhead는 P1~P7 미매핑) |
| F08-G | 관측을 oracle 어댑터로 교체 + `ORACLE_TAG_CONTRACT` 일반화. **mode=evaluation·live인 채 실행 불가 상태** |
| F08-H | `entry_status>=400`을 `checkout_5xx_rate`로 교체(현재 사용자 영향 신호가 단일샘플 1개뿐) |
| F08-P | Class B(설정 오배포) 재라벨 + 250ms가 실제로 무는지 사전 실측 |
| F09-R | cpu 모드에 `required_cohort` 배치 게이트 이식(정답 전제가 미보장) + Class B 라벨 |
| F11-R | `cause` 재작성(fallback 과부하 ✗ → tx 내 Redis timeout 점유 ○) + commerce-cart를 APM/Hikari allowlist에 추가 |
| F15-G | 태그 allowlist 2건 추가 + `tagged_oracle_sessions`를 oracle 어댑터로 교체. 현재 첫 tick LiveProbeError |
| F15-T1 | 러너 정규식 `F[0-9]{2}-[A-Z]` → `F[0-9]{2}-[A-Z][0-9]?` 확장 + entry_status 게이트 교체 |
| F16-H | 롤링업데이트 의미론 해소(maxUnavailable 등) + live 1회 실증. 현재 구조상 장애 미발생 |
| F17-R | 동일 롤링업데이트 문제 + `pod_ready==false` 오탐 조건 교체 |
| F24-Q | 매니페스트 runtime 블록 작성 + controllers 등록 + 캘리브레이션 1회. **러너 배선은 이미 완료 — 투자 대비 회수 최고** |

### CUT (26)

| ID | 사유 |
| --- | --- |
| F01-G | `cause="No incident..."` 음성 |
| F02-P | `idx_menus_category`를 타는 쿼리가 food 전체에 부재. 성공조건도 `entry_status ne 0` 하나뿐 |
| F03-G | 결함 주입 없음(부하 단독), 음성 |
| F03-H | `OrderController.java:70` `Thread.sleep` — 주석이 "F03-H 주입 표면"이라 자백. §2-A 위배 |
| F04-P | "rate limit" 코드 부재(실체는 consumer concurrency 미설정). injector·metadata·controller 전무 |
| F05-G | `cause="No incident..."` 음성 |
| F06-P | food 429 코드 0건. metadata·controller 없음, mock allowlist 밖 |
| F09-H | 정답이 GC 압박인데 `jvm.gc.*` 관측 0건. OOM 배제 지표는 러너가 testbed-order를 거부 |
| F09-P | success 3건 전부 자기충족/비특이. throttling 지표 부재 |
| F10-H | metadata·controller 부재 + host `.14` vs manifest `worker-w2(.11)` 좌표 모순 |
| F10-P | metadata·controller 부재 + 좌표가 도메인-워커 맵과 정반대 |
| F10-R | 좌표는 정합하나 정답·컨트롤러·allowlist 전무 (재설계 시 F10 중 1순위) |
| F11-G | `cause="No incident..."` 음성 + success 3조건 전부 구조적 참 |
| F13-H | `network_fault_executor.py:21` BLOCKED stub. connect-phase는 APM 대체 불가 |
| F13-P | `business_fault_executor.py:13` BLOCKED stub. WPM download-phase 계약 미구성 |
| F13-R | commerce에 Ingress 리소스 자체가 없음(nginx NodePort뿐) + k8s.patch allowlist 밖 |
| F14-P | 앵커 최상급(`TransferEventConsumer.java:40-42` catch-swallow)이나 injector가 BLOCKED stub + imbalance 쿼리 부재 |
| F14-R | 앵커 최상급(`OrderService.java:59-60` 멱등키 0건)이나 fault-proxy 미구현 + allowlist 밖 |
| F15-H | food 429 전제 오류 + metadata·controller·allowlist 전부 부재 |
| F15-P | metadata 부재 + allowlist 밖 + stress-ng 미설치 + co-residency 보장 코드 없음 |
| F15-T2 | metadata 부재 + dispatcher fail-closed + food 429 전제 오류 |
| F15-T3 | metadata 부재 + dispatcher fail-closed — merge 결속을 만들 코드 0 |
| F15-T4 | metadata 부재 + dispatcher fail-closed — split 인계 구간 미구현 |
| F17-P | cleanup 원장 역분개 미구현 + 공유 계정 ACC-1001 차감 = 다음 케이스 오염 확정 |
| F21-P | executor CONTRACTS 미등록(실행 즉시 ExecutorError) + **F09-R과 동일 주입에 상충 정답** |
| F21-Q | 동일. host.stress 계약 부재 + busy-thread 신호 부재 + §2-A 위장 |

## 3. 구조적 결함 (개별 시나리오보다 우선)

### 3-1. answer-key 구조가 사실상 전건 미작성
`registry/scenario-metadata.json` 44개 항목 중 헌장 구조(`class`/`fault_pattern`/`root_cause{target_kind,target_id,mechanism,code_anchor[]}`/`must_support`/`must_rule_out`)를 가진 것은 **F21-P·F21-Q·F24-Q 3건뿐**. 나머지는 자유텍스트 6필드.
**그 3건 중 2건(F21-P/Q)이 CUT 판정** → 백필 템플릿 정본은 **F24-Q**로 삼는다.

템플릿 채택 전 수정할 3가지:
1. `target_id`가 문단이라 기계 채점 불가 → 단일 식별자로 축소 + `trigger_target_id` 분리
2. `injected_fault` 필드 신설(무엇을 실제 조작했는가). 있었다면 F21-P의 정답 충돌이 작성 시점에 드러났다
3. `code_anchor`에 라인 + 심볼명 병기 + CI grep 검증(실측에서 라인 오차 3건 발견)

### 3-2. `entry_status`는 rate가 아니다
`load_north_south_executor.py:174-175` — 마지막 checkout 샘플 **1건**. `live_probes.py:598-604`가 그대로 반환.
`entry_status >= 500` 류 게이트는 영향반경 100%가 아니면 발화하지 않는다. 전 그룹에서 발견.
→ rate 지표(`loadgen.checkout_5xx_rate`, `business_nonok_rate`)로 치환. `entry_status`는 abort의 `==0`(도달 불가) 용도로만 존치.

> **해소됨 (2026-07-28).** success에서 `entry_status`를 쓰던 6건 중 4건(F01-H·F01-R·F06-R·F15-T1)을 `loadgen.checkout_5xx_rate >= 0.05`로 교체했다. F07-H의 `entry-still-reachable != 0`은 abort의 `== 0`과 같은 말이라 삭제했다.
> **F08-H만 남았다** — 주입이 mock 429(4xx)라 5xx 비율로는 안 잡히는데 **4xx 비율 지표가 없다**(§3-7과 같은 뿌리). 피해 증거는 `business.checkout_invariant == false`가 이미 지고 있어 조건을 삭제했고, 4xx rate 신설은 §3-7 step×status 버킷 작업에 묶는다.

### 3-3. 동어반복·자기충족 성공조건
주입 파라미터 되읽기(`achieved_rps >= 주입rps`, `replicas == 0`, `image_pull_failed == true`, `env == 주입값`)와 구조적 항상-참(`pod_ready == true`, 태그를 안 심는 시나리오의 `tagged_db_sessions == 0`).
→ success에서 제거하고 preflight/effect-gate 또는 must_rule_out으로 강등. **success는 "장애가 실제로 났는가"만 물어야 한다.**

> **해소됨 (2026-07-28).** 31개 컨트롤러 전수 재분류. `effect-gate`는 신설하지 않았다 — 컨트롤러 스키마에 그런 구획이 없고, `must_rule_out`의 정의가 *"참이면 런을 무효화(abort)"*이며 **min_hold 이후에만 평가**되므로 가드·감별자를 **반전해서** 넣기에 이미 정확한 자리다. 스키마 변경 없이 끝났다.
>
> | 유형 | 처리 | 예 |
> | --- | --- | --- |
> | 주입 되읽기 | must_rule_out 반전 | `shipping_replicas == 0`(우리가 0으로 줄였다) → `> 0`이면 주입 실패 |
> | 부하 가드 | must_rule_out 반전 | `achieved_rps >= 15` → `< 15`면 부하가 안 흘렀으므로 무효 |
> | 감별자 | must_rule_out 반전 | `payment 5xx < 0.05`(무관 서비스 정상) → `>= 0.05`면 다른 원인 |
> | abort와 동어 | 삭제 | F07-H `entry_status != 0` |
>
> success 조건 총 106 → 60. 시나리오당 최소 1건은 남으며 전건이 피해 증거다.
>
> **경계**: *결과*는 강등하지 않았다. F19-P/F20-P의 커넥션 풀 대기, F12-H의 CPU throttling은 주입의 되읽기가 아니라 주입이 일으킨 성능 저하이므로 G3의 피해에 해당한다. 되읽기와 결과를 가르는 기준은 *"부하 없이도 참인가"* — 참이면 되읽기다.
>
> 재발 방지: `test_success_conditions_ask_only_whether_damage_occurred`가 success에서 `http.entry_health`·`loadgen.achieved_rps`·`kubernetes.pod_ready` 사용을 구조적으로 금지한다.

### 3-4. 실행 불가 배선 6건
| 시나리오 | 증상 |
| --- | --- |
| F06-H | `scenario_tag` 파라미터 누락 → 태그가 `lucida:<run_id>`로 폴백 → 항상 0 |
| F08-G | Oracle 세션을 PG `pg_stat_activity`로 조회 + 태그 allowlist 미등록 |
| F15-G | 동일(태그 2건 미등록 + oracle 어댑터 오배정) |
| F01-P | 동일 계열(`database.oracle_tagged_session_count`가 `queries.json`에 미등록) |
| F07-H | `APPROVED_HIKARI_SERVICES`에 commerce-order 부재 → LiveProbeError |
| F15-T1 | 러너 `scenario_id` 정규식 `F[0-9]{2}-[A-Z]`가 `F15-T1`을 배제 |

근본 대책: `live_probes.py:1121-1127` 태그 allowlist를 하드코딩 집합이 아니라 매니페스트/queries 기반 검증으로 일반화. `ORACLE_TAG_CONTRACT` 단일 태그 하드코딩 파라미터화.

> **해소됨 (F06-H는 2026-07-27, 나머지 5건은 07-28).** 실제 원인은 표에 적힌 것과 일부 달랐다 — 재확인해 고친 내용:
>
> | 시나리오 | 실제 원인 | 조치 |
> | --- | --- | --- |
> | F06-H | 태그 관측 자체가 L1 누설 | `database.blocked_session_count`로 교체(07-27) |
> | F01-P·F08-G | Oracle 세션을 **PG** `tagged_session_count`로 조회 | `database.oracle_tagged_session_count`로 전환 + repo `queries.json`에 등록 |
> | F15-G | 위 + PG 태그 `rca-F15-G-inventory-lock`이 러너 allowlist 밖 | Oracle 축 전환 + PG 태그 등록 |
> | F15-T1 | 러너 loadgen 정규식 `F[0-9]{2}-[A-Z]`가 `F15-T1` 배제 | `T[1-4]` 분기 추가 |
> | F07-H | **표의 hikari 진단은 현행과 불일치** — 현 매니페스트에 hikari 관측이 없다. 실제 결함은 `tagged_db_sessions`에 파라미터가 없어 `lucida:<run_id>`로 폴백, 아무도 그 이름을 쓰지 않으므로 항상 0 → must_rule_out이 **발화 불가**(§3-3 동어반복 계열) | inventory·payments 두 릴레이션의 `blocked_session_count`로 교체 |
>
> `ORACLE_TAG_CONTRACT`(단일 태그 dict)는 `APPROVED_ORACLE_TAGS` 3종 집합으로 바뀌었다. 태그가 SQL에 `chr(114)||chr(99)||...`로 **손으로 인코딩**돼 있어 F01-P 외에는 관측 자체가 불가능했다 — 인코딩을 `_oracle_string_literal()`로 런타임 생성한다(중첩 printf/sqlplus 파이프라인에 따옴표를 넣지 않으려는 원래 의도는 유지).

### 3-5. 파드 교란이 장애를 만들지 못한다 (신규 발견)
1-replica Deployment + strategy 미지정(maxUnavailable 0) 상태에서 probe/template patch를 가하면 **신규 파드만 NotReady가 되고 구 Ready 파드가 살아남아** Service 엔드포인트가 유지된다.
영향: **F16-H, F17-R**(및 F15-T1의 food OOM 축, F05-H 재확인 필요).
→ `strategy.rollingUpdate.maxUnavailable: 1` 명시 / 파드 레벨 조작 / replicas 2+ 전 파드 교란 중 택1. **결정 전 golden 불가.**

> **결정·실증 완료 (2026-07-28).** 라이브 클러스터에서 before/after를 직접 측정했다(`testbed-user`, readiness 고장 주입):
>
> | | 파드 | endpoints | availableReplicas |
> | --- | --- | --- | ---: |
> | 현재 전략(25%/25%) | 헌 파드 `1/1 Running` + 새 파드 `0/1 Running` | `10.244.2.59` | 1 |
> | `maxUnavailable:1`/`maxSurge:0` | `0/1 Running` 하나 | **없음** | 없음 |
>
> replicas 1에서 쿠버네티스는 maxUnavailable을 **내림**(25% → 0), maxSurge를 **올림**(25% → 1)한다. 그래서 새 파드가 먼저 뜨고 헌 파드는 새 파드가 Ready가 될 때까지 서비스한다 — readiness가 영원히 실패하면 롤아웃만 멈추고 **아무도 다치지 않는다.** 결함은 진짜인데 사건이 없다(G3 불충족).
>
> **채택: `maxUnavailable: 1` / `maxSurge: 0`을 `testbed-user`·`testbed-transfer` **두 디플로이먼트에만** 명시.** 매니페스트(`commerce/k8s/25-user-service.yaml`, `core-banking/k8s/22-transfer-service.yaml`)에 넣고 라이브에도 적용했다.
> - 전 함대 적용하지 않은 이유: **F08-H는 무해한 롤아웃을 distractor로 쓴다.** 전역 적용은 그 distractor를 진짜 장애로 만들어 시나리오를 파괴한다.
> - replicas 2+ 안을 택하지 않은 이유: 용량·로드밸런싱 전제가 바뀌어 다른 시나리오의 임계치를 흔든다.
> - 파드 레벨 조작 안을 택하지 않은 이유: readinessProbe는 러닝 파드에서 불변 필드다.
>
> **영향 범위는 readiness 2건뿐이다.** `k8s.probe` 3건 중 F05-H는 **liveness**라 새 파드가 Ready가 된 뒤 헌 파드가 제거되고 그 다음 크래시 루프에 빠지므로 원래부터 장애가 난다. `k8s.resource`(F05-R·F25-H)도 같은 이유로 정상 동작한다. 즉 §3-5의 영향 목록에서 F15-T1·F05-H는 제외된다.
>
> 부수 확인: `availableReplicas`는 0일 때 필드가 사라지지만 러너가 `.get("availableReplicas", 0)`으로 받으므로 관측은 정상이다.

### 3-6. 정답 충돌 1건
F21-P와 F09-R이 **동일 주입**(`{"mode":"cpu","host":"192.168.122.14","cpu_workers":3,"runtime_seconds":480}`)에 서로 다른 answer-key를 갖는다. 데이터셋 최대 취약점.

### 3-7. 상태코드 해상도 부족
`loadgen.*_status_rate` = `business.5xx.rate`라 **502와 503을 구분하지 못한다**. F19-S("502 전파")·F20-Q("503 배제")·F24-Q("502 vs 503")가 모두 이 신호에 감별을 의존.
→ step×status 버킷 신설이 3건 공통 선행조건.

### 3-8. 미등록 query_id 확정
`prometheus.circuitbreaker_open`, `prometheus.http_server_active_requests` — repo `registry/queries.json`·러너 `observation_queries.json` **양쪽 모두 부재**. 대체로 넣은 `jvm_nondaemon_thread_count`는 어느 매니페스트도 참조하지 않고 busy-thread 의미도 아니다.

### 3-9. executor CONTRACTS ↔ registry allowlist 불일치
`profiles.json`의 `allowed_scenarios`에 있어도 executor 내부 `CONTRACTS`에 없으면 실행 불가(F21-P/Q에서 확인). 역으로 `host.stress`는 `build_invocation`이 빈 profile을 넘겨 registry allowlist를 아예 검증하지 않는다.
→ 양방향 정합성을 `test-scenarios.sh`에서 강제.

## 4. v3 캡처 큐 오염 (즉시 조치 대상)

`rca-scenario-runner/backend/app/live_queue.py` CYCLE_SCENARIO_ORDER에 **음성 4종(F01-G, F03-G, F05-G, F11-G)** 이 포함돼 있다. 헌장 §1(음성 폐지)과 정면 충돌.

- F03-G, F05-G, F11-G: **이미 캡처 완료되어 evaluation 8건에 포함** → 데이터셋 구성 재검토 필요
- F01-G: 2026-07-27 재캡처 진행 중 → CUT 대상이므로 산출물 활용 불가

## 5. 미확인 항목 (라이브 실측 필요)

| 항목 | 확인 방법 |
| --- | --- |
| 롤링업데이트 실거동(최우선) | `kubectl get deploy testbed-user -n rca-testbed-commerce -o jsonpath='{.spec.strategy}'` + probe patch 후 `kubectl get endpoints` |
| F10×3 PVC UUID 최신성 | `kubectl get pvc -A -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,VOL:.spec.volumeName` |
| mysql-0 / oracle-0 실제 배치 노드 | `kubectl get pod -A -o wide \| grep -E "mysql-0\|oracle-0"` |
| F05-P / F09-R cohort 실재 | `kubectl get pods -A -o wide --field-selector spec.nodeName=tb-w2` (tb-w3 동일) |
| F07-H hikari 메트릭 방출 | VictoriaMetrics(119:18428)에서 `db_client_connections_pending_requests{service_name="commerce-order"}` |
| F08-P 250ms가 실제로 무는지 | 기존 캡처 baseline parquet의 `commerce-order` p50/p95 (신규 실행 불필요) |
| F05-R 576Mi OOM 실효성 | 과거 calibration 캡처 확인 |
| 109 러너 사본과 로컬 clone 동일성 | 캡처 종료 후 `docker exec <runner> grep -n "rca-F15-T1-inventory-lock" /app/app/live_probes.py` |

## 6. 권고 작업 순서

1. **v3 큐에서 음성 4종 제거** + 이미 캡처된 3건의 데이터셋 처분 결정
2. **구조적 결함 우선 처리** — 3-2(entry_status 치환), 3-3(동어반복 제거), 3-4(배선 6건), 3-6(정답 충돌)은 개별 시나리오 작업보다 앞선다
3. **3-5 롤링업데이트 의미론 결정** — 파드 교란 계열 전체가 여기 걸려 있다
4. **answer-key 백필** — F24-Q 기반 템플릿 확정(§3-1 수정 3건 반영) 후 KEEP 15 + 조건부 19에 적용
5. **조건부 19 수리** — 회수 순: F24-Q > F06-H(1줄) > F01-P·F08-G·F15-G(배선) > 나머지
6. **CUT 26 제거** — catalog.json·manifests에서 정리. 단 F14-P/F14-R의 코드 앵커(`TransferEventConsumer.java:40-42`, `OrderService.java:59-60`)는 카탈로그 최강급 P5/무결성 재료이므로 **신규 시나리오 백로그로 승계**(injector 신설 트랙)

   > **실행됨 (2026-07-28) — 단, 삭제가 아니라 보류(park).** 26종을 지우려다 보니 CUT 사유의 대부분이 "발상이 틀렸다"가 아니라 **"미완성"**이었다. 정답·컨트롤러 부재 8 + injector 부재 6 = 14종은 쓰기만 하면 회생하고, 전제 오류 4 + 합성장애 1 + cleanup 1도 앱·정리절차를 고치면 살아난다. 회생 불가는 음성 4와 관측 소스가 없는 2종뿐이다. 지우면 목표(60종)에 도달할 때 같은 설계를 다시 해야 한다.
   >
   > 구현: `catalog.json`에 `readiness: "parked"` + `prerequisite`에 **회생 조건**을 명시. 컨트롤러 8종은 `registry/controllers-parked.json`으로 이관했다 — `compile-plan`이 *"controllers.json에 있으면 곧 실행 가능"*을 불변식으로 강제하므로(`must bind a ready catalog scenario`) 보류분이 거기 남아 있을 수 없다. 실행 차단은 `live_allowed = readiness == "ready" and ...`가 담당하며, 26종 전건이 `live_allowed == false`임을 `test-scenarios.sh`가 검사한다.
   >
   > 러너 큐에서도 제거: `LIVE_SCENARIO_ORDER`에서 F03-G·F09-P·F11-G, `CYCLE_SCENARIO_ORDER`에서 F01-G·F03-G·F05-G·F11-G·F09-P.
   >
   > 부수 발견 — `test-scenarios.sh`는 **이미 실패 상태였다**(카탈로그가 60으로 줄어든 뒤에도 64를 단언, `Q`·`S` 접미사가 ID 정규식에 미등록). 종료코드를 확인하지 않은 호출 경로에서 조용히 통과한 것으로 보인다. 고정 숫자 중 파생 가능한 것은 파생시키고, 통치 대상인 readiness 분포만 고정으로 남겼다.
7. **전수 스모크** — 위가 끝난 뒤
