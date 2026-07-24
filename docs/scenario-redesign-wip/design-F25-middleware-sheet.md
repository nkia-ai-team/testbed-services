# 설계 시트 — F25-{R,H,S,P}: 미들웨어 인프라 결함 4종 (Class B)

- **백로그 근거**: fault-surface-infra-middleware.md B1(PG 연결고갈)·B2(PG OOM)·B3(Kafka SPOF)·B4(Oracle 공유포화) 신규 채택 권고 4건.
- **클래스**: 전부 **Class B** — 미들웨어 인스턴스 자체의 인프라 결함(자원 상한·SPOF·영속성/설정). 헌장 §2 골든 4조건에서 Class B는 ①이 **인프라 앵커**(자원·노드·인스턴스와 그 조작 수단).
- **관측 도메인**: DPM(DB) 중심 + KCM(컨테이너/OOM·replicas) 보조. F25-S는 commerce 앵커지만 3도메인 브로커 공통 표면.
- **실측 재검증(2026-07-24, 레포 매니페스트 직접 grep)**:
  - PG: `commerce/k8s/10-postgres.yaml:23` **kind: StatefulSet** `testbed-postgres`(container `postgres`), mem limit **512Mi**/req 256Mi(:67-70). `max_connections=100` 기본·`shared_buffers=128MB` 기본(fault-surface §0). ns `rca-testbed-commerce`.
  - Kafka: `commerce/k8s/12-kafka.yaml:10` **kind: StatefulSet** `testbed-kafka`(container `kafka`, apache/kafka:3.9.0), **replicas: 1**(:18). broker+controller 겸임(KRaft). ns `rca-testbed-commerce`.
  - Oracle: `core-banking/k8s/10-oracle.yaml:5` **kind: StatefulSet** `testbed-oracle`(container `oracle`), cpu limit **2000m**/req 500m·mem 4Gi/req 2Gi(:37-41). SGA 1.5G·단일 PDB FREEPDB1 4서비스 공유(fault-surface §1.3). ns `rca-testbed-banking`.
  - 앱 풀 합 vs DB 상한: commerce PG usable **97**(100−3) vs Hikari 합 **95**, 라이브 baseline 62접속(fault-surface §0). food MySQL·banking Oracle는 연결 여유 큼 → 연결고갈은 commerce PG 고유.

---

## 0. 공통 근본 발견 — 실측으로 갱신된 능력 판정 (가장 중요)

fault-surface 문서는 B2/B3/B4를 "executor 재사용 + allowlist 확장(신규 코드 아님)"으로 낙관했으나, **executor 소스를 직접 읽어 정정**한다:

| executor | 실측 (source) | 실제 대상 | 미들웨어 실제 kind | 판정 정정 |
|---|---|---|---|---|
| `k8s_resource_executor.py` | `auth can-i patch **deployments**`, `get deploy`(:82) | Deployment `testbed-payment` | PG=**StatefulSet** | allowlist만으론 불가 — **StatefulSet kind 일반화** 필요 |
| `kafka_control_executor.py` | `scale **deploy** "$deployment"`(:99), `get deploy`(:60-62) | **consumer** Deployment `testbed-shipping`를 0으로 | broker=**StatefulSet** `testbed-kafka` | 브로커 down 아님 — **브로커 StatefulSet scale + statefulset available_replicas** 신설 |
| `k8s_patch_executor.py` | `set resources **deploy**`(:52), `get deploy`(:43) | Deployment(inventory/product) cpu | Oracle=**StatefulSet** | **StatefulSet kind 일반화** 필요 |

→ **공통 갭 A (executor kind 일반화)**: 세 executor 모두 `kubectl ... deploy`에 하드코딩. 미들웨어 4종(PG/MySQL/Oracle/Kafka)은 전부 StatefulSet이라 `deploy`→`statefulset` 파라미터화가 선행. 신규 injector는 아니나 "1줄 allowlist"보다 크다(kind 스위치 + `.status` 셀렉터 대응).

→ **공통 갭 B (available_replicas가 Deployment 전용)**: `queries.json:199` `kubernetes.deployment_available_replicas` 셀렉터=`deployment.status.available_replicas`. 브로커/DB(StatefulSet)의 가용 replica를 못 읽음 → **`kubernetes.statefulset_available_replicas` 신설** 필요(F25-S·F25-H 감별의 핵심 지표).

→ **F25-R 전용 갭 C (진짜 injector 부재)**: bare 커넥션 슬롯을 잡는 injector가 아예 없음(`db.lock`은 row-lock만). + PG 총 접속수/`too many clients` 관측 query도 없음(`database.tagged_session_count`은 태그된 세션만 셈). **B군 중 가장 깊은 blocked.**

---

## 1. 시나리오별 인과 사슬 + 인프라 앵커

### F25-R (DPM) — commerce PG 연결 슬롯 고갈 → 8스키마 전면 신규요청 거부
```
[슬롯 소비 injector로 유휴 커넥션 N개 홀드]  ← 주입수단 미존재(갭 C)
  또는 지속 부하로 cart(5→20)·pricing(5→15)·user(3→10)·shipping(3→10) 풀 동시 max 램프
  → PG usable 97 소진 (라이브 baseline 62 + Δ)
  → 신규 getConnection 전부 FATAL: sorry, too many clients already
  → order/payment/inventory/… 8스키마 전부 커넥션 획득 실패 → commerce 전 서비스 5xx
```
- **인프라 앵커**: `commerce/k8s/10-postgres.yaml` 무튜닝 → `max_connections=100` 기본, reserved 3 → usable **97**. 앱 Hikari 풀 합 **95**(order10+product10+inventory10+payment10+cart20+pricing15+user10+shipping10, fault-surface §0). 헤드룸 사실상 0.
- **root vs 증상**: root=**PG max_connections 인프라 상한(단일 인스턴스, 스키마 격리 없음)**. 증상=commerce **전 서비스 동시** "DB 안 붙음"(오진 유발).

### F25-H (DPM·KCM) — commerce PG OOMKill → commerce DB 전면 순단
```
[k8s.resource로 testbed-postgres mem limit 512Mi → 조임(예 320Mi)]
  + 슬로우쿼리/정렬 부하로 work_mem×동시성 + shared_buffers(128MB)가 cgroup 상한 초과
  → postgres-0 OOMKill → 재기동 수십초(PVC로 데이터 보존)
  → 재기동 동안 8스키마 전부 접속 불가 → commerce 전면 순단 → 회복
```
- **인프라 앵커**: `10-postgres.yaml:70` mem limit **512Mi**(req 256Mi). shared_buffers 128MB.
- **root vs 증상**: root=**PG 메모리 인프라 상한**. 증상=전 서비스 순간 5xx 후 자동 회복. KCM `container_last_termination_reason=OOMKilled`·`restart_count↑`.

### F25-S (DPM·KCM, commerce 앵커) — Kafka 브로커 SPOF → 이벤트 백본 전면 중단
```
[kafka.control(확장)로 testbed-kafka StatefulSet replicas 1→0]
  → RF=1/ISR=1이라 failover 없음, KRaft combined라 controller 쿼럼도 상실
  → OutboxRelay send().get() 블로킹/실패, 소비 중단
  → 동기 결제 경로는 정상(200), 비동기만 전멸: 배송 미생성·알림 정지·상태 사후보정 정지
  → 재기동 시 PVC로 복구·리밸런싱
```
- **인프라 앵커**: `12-kafka.yaml:18` **replicas: 1** + broker/controller 겸임 + 전 토픽 RF=1/ISR=1(fault-surface 라이브 확인).
- **root vs 증상**: root=**Kafka 인스턴스 SPOF**. 증상=shipping/notification 무행동 + outbox 백로그, **5xx 없는 조용한 고장**.

### F25-P (DPM) — banking Oracle 단일 PDB 공유 포화 → banking 전 서비스 저하
```
[k8s.patch(확장)로 testbed-oracle cpu limit 2000m → 조임]  (§2-A 논증 아래)
  → 단일 PDB FREEPDB1가 cpu/SGA 상한에 걸려 쿼리 큐잉
  → api/account/transfer/ledger 4서비스 전부 지연(공유 자원 오염)
  → 재시작 무거워(startup 수분) 회복 지연
```
- **인프라 앵커**: `10-oracle.yaml:37-41` cpu limit **2000m**·mem 4Gi. SGA 1.5G(Free 하드캡)·단일 PDB FREEPDB1 4서비스 공유.
- **root vs 증상**: root=**Oracle Free 인프라 상한 + 단일 PDB 공유**. 증상=banking 도메인 전반 동반 저하(특정 서비스 원인 안 보임).

#### §2-A 정직성 논증 (F25-P cpu 하향이 "위장"인가?)
헌장 §2-A는 "env로 **코드 동작**을 인위 왜곡(풀 크기 강제 축소·timeout 임의값)하는 것을 **Class A로 위장 금지**"라 규정한다. F25-P의 cpu limit 하향은 이에 **해당하지 않는다**:
- (1) **코드가 아니라 인프라 자원을 조인다.** 앱의 로직/설정(Hikari·timeout)은 손대지 않는다. 조이는 대상은 Oracle 컨테이너의 cgroup cpu — 미들웨어 인스턴스의 물리 자원.
- (2) **정답이 바로 그 자원 상한이다.** Class A 위장은 "앱 코드 때문"이라 오답을 심는 것이 문제인데, F25-P의 root_cause는 정직하게 "Oracle 인프라 cpu 상한 + 단일 PDB 공유"이며 주입면과 정답이 일치한다(헌장 ②).
- (3) **단, 완전한 충실성은 아니다 → 🟡.** 가장 충실한 재현은 "4서비스가 고정 Free 캡을 자연 포화"이나 그런 부하 드라이버 injector가 없다. cpu-patch는 그 천장에 **결정적으로 도달시키는 proxy**다. 이는 §2-A의 "자연 고갈이 아니면 **Class B(설정/운영 결함)로 명시**"라는 허용 경로에 정확히 부합한다 — 우리는 Class A로 위장하지 않고 Class B 인프라 자원 축소로 **명시**한다. 그래서 판정은 정직하게 🟡(proxy 주입)로 남긴다.

---

## 2. 감별표 (골든 조건 ③ — must_support / must_rule_out)

| | must_support | must_rule_out (감별점) |
|---|---|---|
| **F25-R** | PG 총 접속수 ≈97에서 flat(상한 도달); commerce 전 서비스 5xx 동시(단일 서비스 아님); PG pod ready·restart=0 | **앱 풀 고갈(F03/F22) 아님** — 그건 한 서비스 Hikari pending만, PG 접속수는 상한 미달 · **PG OOM(F25-H) 아님** — restart=0·termination≠OOMKilled(PG 건강, 접속만 거부) · 429 아님 |
| **F25-H** | `container_last_termination_reason=OOMKilled`; `restart_count` 증가; 재기동 구간 전 서비스 순간 5xx 후 회복; PG mem_current가 limit 근접 후 kill | **앱 pod OOM(F05-R) 아님** — 대상이 공유 DB(testbed-postgres)라 blast=commerce 전체(payment 한 서비스 아님) · **연결 고갈(F25-R) 아님** — F25-R은 restart 없음 |
| **F25-S** | `statefulset_available_replicas(testbed-kafka)=0`; 전 토픽 unavailable; outbox 미발행 누적; **동기 200 정상·5xx 없음** | **consumer 정지(F04-R) 아님** — F04-R은 **브로커 UP**(kafka replicas=1)이고 소비만 멈춰 **lag↑**; F25-S는 **브로커 down**(available=0)이라 발행까지 중단. 감별축=`available_replicas(kafka)=0` vs 브로커생존+lag(§헌장 F04 정정) |
| **F25-P** | banking 4서비스(account/transfer/ledger/api) apm p95 **동시** 상승; Oracle cpu throttled 상승; transfer_2xx_rate 저하 | **Oracle row-lock(F01-P) 아님** — F01-P은 특정 행 FOR UPDATE로 Oracle 건강·`tagged_session` 상주·한 경로만 블록; F25-P는 락 없이 **인스턴스 자원 상한**이라 4서비스 전부 저하 · 단일 서비스 CPU/노드포화(F09) 아님 |

**감별 축 요약**: F25-R=PG 접속수 상한 flat + 전 스키마 동시(restart=0) / F25-H=OOMKilled + restart↑(공유 DB) / F25-S=kafka available=0 + 5xx 없는 백본 정지 / F25-P=4서비스 동반 저하 + cpu throttle(락 없음). 네 개 모두 "증상이 도메인 전체로 퍼짐"이 공통이라 naive RCA가 개별 앱을 root로 오판 → 정답은 공유 미들웨어 인스턴스.

---

## 3. 골든 4조건 자체점검표

| 조건 | F25-R | F25-H | F25-S | F25-P |
|---|---|---|---|---|
| ① 인프라 앵커 | ✅ max_conn=100 vs 풀합 95, baseline 62 실측 | ✅ 10-postgres.yaml:70 mem 512Mi 실측 | ✅ 12-kafka.yaml:18 replicas1·RF1 | ✅ 10-oracle.yaml:37-41 cpu2000m·SGA1.5G·단일PDB |
| ② 정답(answer-key) | ✅ root_cause{db=commerce-postgres, mechanism=connection ceiling} 작성 | ✅ root_cause{db=commerce-postgres, mechanism=cgroup OOM} 작성 | ✅ root_cause{broker=commerce-kafka, mechanism=SPOF} 작성 | ✅ root_cause{db=banking-oracle, mechanism=shared PDB resource ceiling} 작성 |
| ③ 감별 가능 | 🟡→❌ 설계 명확하나 **PG 총 접속수·too-many-clients query 부재**(tagged_session은 태그세션만); 전 스키마 5xx는 checkout_5xx 1개로만 근사 | 🟢 termination_reason/restart_count/mem_current/mem_limit **전부 실재**(queries.json:221-264). 감별 관측 가능 | 🟡 lag/pod_ready 실재하나 **available_replicas가 Deployment 전용**(갭 B) → StatefulSet 지표 신설해야 F04-R 감별 결정화 | 🟡 apm_service_p95(서비스별)·cpu_throttled_time 실재하나 후자가 Deployment 파라미터(StatefulSet 대응 필요); banking 서비스별 5xx query 없음 |
| ④ 주입 수단 실재 | ❌ **bare 커넥션 홀드 injector 부재**(갭 C, 진짜). config-fault(max_conn 하향+재시작)는 pod 교란 성격 다름 | 🟡 k8s.resource 실재하나 **Deployment 하드코딩**(갭 A) → StatefulSet 일반화 + allowlist + PG mem 값 | 🟡 kafka.control 실재하나 **consumer Deployment scale**이라 브로커 down 아님 → StatefulSet scale + 지표 신설(갭 A·B) | 🟡 k8s.patch 실재하나 Deployment 하드코딩(갭 A) + §2-A proxy 주입(자연 포화 아님) |
| **종합** | ①②✅ ③❌ ④❌ → **draft/blocked (진짜 능력갭: injector+query 신설)** | ①②✅ ③✅ ④🟡 → **draft/blocked (배선: executor kind 일반화)** | ①②✅ ③🟡 ④🟡 → **draft/blocked (배선+지표 신설)** | ①②✅ ③🟡 ④🟡 → **draft/blocked (배선+§2-A proxy 한계)** |

네 개 모두 `readiness=draft`, `prerequisite_gate.state=blocked`, `live_allowed=false`.
- **F25-H가 가장 승격에 가깝다**(관측 ③ 완비, ④는 executor kind 일반화만).
- **F25-R이 가장 깊다**(injector 자체가 없음 + 관측도 없음 — 순수 배선으로 안 됨).

---

## 4. 능력 갭 (승격 전 선행 — prerequisite_gate)

### 4-A. 공통 배선 (신규 injector 아님 — executor/query 일반화)
1. **executor kind 일반화 (갭 A)**: `k8s_resource_executor`·`k8s_patch_executor`·`kafka_control_executor`의 `kubectl deploy`/`auth can-i patch deployments`를 `--kind {deploy|statefulset}` 파라미터로 일반화. 미들웨어는 전부 StatefulSet. F25-H(PG resource)·F25-P(Oracle patch)·F25-S(kafka scale) 공유.
2. **`kubernetes.statefulset_available_replicas` 신설 (갭 B)**: 셀렉터 `statefulset.status.available_replicas`. F25-S 브로커 down 감별(=0)·F25-H PG readiness의 결정 지표. 현 `deployment_available_replicas`는 Deployment 전용.
3. **k8s.resource parameter_contract**: allowed_scenarios에 F25-H + allowed_locations(commerce-namespace 이미 포함) + PG mem baseline(512Mi)/fault(예 320Mi) + `kind=statefulset`·`workload=testbed-postgres`·`container=postgres`.
4. **k8s.patch parameter_contract**: allowed_scenarios에 F25-P + allowed_locations(banking-namespace 이미 포함) + Oracle cpu baseline(2000m)/fault + `kind=statefulset`·`workload=testbed-oracle`·`container=oracle`. baseline!=500m 가드(k8s_patch_executor.py:21)는 2000m 허용하도록 완화 필요.
5. **kafka.control parameter_contract**: allowed_scenarios에 F25-S + 브로커 대상 파라미터(`mode=broker-down`, `workload=testbed-kafka`, `kind=statefulset`) — 현 F04-R은 consumer scale이므로 mode 분기 신설.
6. **load.north_south allowlist**: F25-R·F25-H·F25-S(commerce surge, entry 30080)·F25-P(banking surge, entry 30082) tag_pattern·allowed_scenarios 추가(companion 부하 — F02-P 선례).

### 4-B. F25-R 전용 (진짜 능력갭 — 신규 injector + query)
7. **bare 커넥션 슬롯 소비 injector 신설 (갭 C, 근본)**: 유휴 커넥션 N개를 잡아 usable 97을 결정적으로 소진하는 executor(예 `db.connection-saturate`). `db.lock`은 row-lock만이라 부적격. 대안(max_connections 하향+재시작)은 pod 교란이라 "자연 도달" 성격과 다름.
8. **`database.pg_connection_count` 신설**: `pg_stat_activity` 총 접속수(vs max_connections). 현 `database.tagged_session_count`는 특정 application_name 태그 세션만 세어 전체 포화를 못 봄.
9. **`database.pg_connection_error_rate` (또는 too-many-clients 카운터) 신설**: `FATAL: too many clients` 발생을 관측. 없으면 loadgen checkout_5xx로만 근사 → 전 스키마 동시성 증명 불가.

---

## 5. 헌장 부합성 평가 (한 문단)
네 시나리오는 fault-surface B1~B4의 인프라 앵커를 레포 매니페스트로 재검증(PG/Kafka/Oracle 전부 StatefulSet·자원 상한 실측)하고 헌장 Class B "인프라 앵커" 요건을 채웠다. 가장 값진 발견은 fault-surface가 "executor 재사용 + allowlist 확장(신규 코드 아님)"으로 낙관한 B2/B3/B4가 **executor 소스 실측 결과 전부 `kubectl deploy` 하드코딩**이라, 미들웨어가 StatefulSet인 이상 순수 allowlist로는 불가하고 **kind 일반화**라는 공통 배선이 선행한다는 점이다 — 특히 kafka.control은 fault-surface의 "브로커 스케일0 지원" 주장과 달리 실제로는 **consumer Deployment를 0으로** 만드는 코드라(브로커는 살아있음) F25-S=브로커 down의 정체성을 그대로 재현하지 못한다. 4조건 루브릭이 이 격차를 드러냈다: F25-H는 관측(③)이 완비돼 executor kind 일반화만으로 승격 가능한 반면, F25-R은 bare 커넥션 injector도 PG 접속수 query도 아예 없어 순수 배선으로 안 되는 진짜 능력갭이고, F25-P는 cpu-patch가 §2-A "Class A 위장"에는 걸리지 않으나(코드가 아니라 인프라 자원을 조이고 정답이 그 자원 상한임) 자연 포화가 아닌 proxy 주입이라 정직하게 🟡로 남는다. 결론: F25-H·F25-S·F25-P는 executor kind 일반화 + StatefulSet available_replicas 지표라는 **공유 배선 백로그**로 묶어 올릴 값어치가 있고(H가 선두), F25-R은 커넥션 슬롯 injector라는 별도 능력 트랙으로 분리해야 하는, 정직하게 blocked인 설계다.
