---
title: 부하 시나리오 규칙
status: Draft
owner: project
last_reviewed: 2026-07-15
tags:
  - scenario
  - testbed
  - load
  - anomaly-detection
  - evaluation
summary: baseline 위에 얹는 시나리오성 부하(surge)의 주입 규칙, 시나리오 간 간격, RCA·이상감지 양축 커버리지, golden 이상감지 기대값 기입과 사후 검증 패스를 정의한다.
---

# 부하 시나리오 규칙

이 문서는 부하가 개입하는 시나리오(트래픽 폭주가 원인인 시나리오, 부하로 증상을
발현시키는 시나리오)에 추가로 적용되는 규칙을 정의한다. 시나리오 일반 규율
(스키마, 4구간 타임라인, golden 승격)은 [시나리오 설계](spec-scenario-design.md)와
[시나리오 작성 규칙](spec-scenario-authoring.md)을 그대로 승계하고, 이 문서는
그 위에 부하 특유의 규칙만 얹는다. baseline 부하 생성기 자체의 설계는
[테스트베드 확장 §8](spec-testbed-expansion.md)이 정본이다.

## 1. 용어

| 용어 | 뜻 |
| --- | --- |
| **baseline** | loadgen이 24시간 흘리는 평상시 부하. tb-runner systemd 상주, diurnal 프로파일, 항상 on. |
| **surge** | 시나리오가 의도적으로 일으키는 폭주 부하. 시나리오 시간 안에만 존재하는 별도 k6 실행. |

부하 시나리오에서 surge의 역할은 둘 중 하나다.

1. **원인으로서의 surge** — 트래픽 급증 자체가 root cause (예: 이벤트 폭주 →
   커넥션 풀 고갈 → latency 급등).
2. **증폭기로서의 surge** — root cause는 다른 결함(예: slow query)이고, 평시
   부하에선 무증상인 그 결함의 증상을 발현시키는 보조 수단.

같은 도구(surge 스크립트)를 쓰되 golden 표기가 달라진다(R5).

## 2. 규칙

### 원칙

- **R0. 인시던트 격상 가능성 (최상위 필터).** 모든 부하 시나리오는 실제
  운영이라면 **운영자가 인시던트로 선언할 만한 상황**이어야 한다. 메트릭
  몇 개가 흔들리는 것으론 부족하다 — 사용자 체감(에러율·응답 지연·기능
  불능)으로 이어지는 인과가 있어야 한다. 이 필터를 통과하지 못하는
  시나리오는 카탈로그에 넣지 않는다.
- **R1. baseline 불가침.** 부하 시나리오는 baseline loadgen을 절대 건드리지
  않는다 — env 수정·재시작·중단 금지. surge는 항상 별도 k6 프로세스로
  baseline 위에 얹는다. baseline이 흔들리면 정상 분포가 오염되고 시나리오 간
  독립성이 깨진다.

### 주입

- **R2. 주입 위치는 부하 유형이 결정한다.**

  | 부하 유형 | 주입 위치 | 진입 경로 |
  | --- | --- | --- |
  | 사용자 트래픽 surge (north-south) | tb-runner | baseline과 동일 — tb-cp NodePort |
  | 내부 호출 폭주 (east-west, 재시도 폭풍 등) | 클러스터 내부 (일회성 Job 등) | 서비스 내부 DNS |
  | DB 직접 부하 (무거운 쿼리·커넥션 점유) | tb-runner | DB NodePort |
  | 네트워크 경로 부하 (NMS 계열 — 대역 포화·flow·trap) | **외부 서버 192.168.200.57** (2026-07-15 추가) | 물리 NIC·브리지 경유 — 109 호스트 밖에서 진입 |

  유형과 위치가 어긋나면 관측되는 topology가 비현실적이 된다. 사용자 폭주가
  클러스터 안에서 발생하거나, 내부 폭주가 정문으로 들어오면 안 된다.
  **네트워크 계층 시나리오(G19·G20)는 반드시 외부 서버발이어야 한다** —
  tb-runner도 109 호스트 안의 VM이라 그 트래픽은 물리 인터페이스를 타지
  않으므로, 호스트 내부발 부하로는 NMS가 보는 인터페이스·flow에 실상관이
  생기지 않는다. .57은 SNMP 폴링 대상·trap 송신·NetFlow exporter 역할
  겸용([시나리오 설계 §9](spec-scenario-design.md) NMS 전제 작업). 접속:
  `root@192.168.200.57` / `Cloud!!25` (2026-07-15 확보·실증 — Rocky 9.7,
  x86_64, hostname `dev-svr-200-57`).
- **R3. 생명주기는 시나리오에 종속.** surge는 `injection.script`가 시작하고
  cleanup 단계가 종료를 보증한다: k6는 duration 만료로 스스로 죽게 하되,
  cleanup에서 잔존 프로세스 강제 종료 + 사망 확인까지 한다. 잔존 surge는
  다음 시나리오를 오염시키고, 제때 꺼져야 회복 곡선이 데이터로 남는다.
- **R4. 재현성.** surge 스크립트도 시드 고정(`LOADGEN_SEED` 규약 재사용),
  여정은 loadgen 여정 카탈로그에서 선택, 강도·지속시간·ramp 형태는 전부
  `injection.parameters`에 기록한다. 같은 파라미터 = 같은 부하.
- **R6. 시나리오 간 간격.** 시나리오 사이에 **최소 1시간의 정상 구간**을
  보장한다(정상 1h → 주입 → 정상 1h → 주입 …). 이상감지가
  정상→이상→회복→정상 사이클을 온전히 보고, 인접 시나리오의 증상이 섞이는
  것을 구조적으로 막는다. 4구간 타임라인이 시나리오 *안*의 규칙이라면 이것은
  시나리오 *사이*의 규칙이다. 의도적 동시 발생(음성 패턴
  `multiple_concurrent_anomalies`)은 한 시나리오 안에서 명시적으로만 설계한다.

### 커버리지

- **R7. RCA 축 커버리지.** 카탈로그는 RCA가 지목할 수 있는 low-level 원인의
  다양성을 커버해야 한다. 관측 4계층(SMS/APM/DPM/KCM) × 원인
  유형(CPU·메모리·커넥션 풀·slow query·lock·스레드 고갈·pod 재시작·네트워크
  등) 매트릭스로 빈 칸을 관리한다(§4).
- **R8. 이상감지 축 커버리지.** 원인뿐 아니라 이상감지가 잡아야 할 **현상
  유형**도 커버 축이다: 메트릭 스파이크, 로그 신규 패턴 출현, 에러 로그 급증,
  latency 분포 변화, 호출량 급변 등. 전제: **로그 이상은 부하만으로 나오지
  않는다** — 해당 상황에서 앱이 실제로 그 로그를 찍도록 결함 표면이 설계돼
  있어야 하므로, 이 축은 surge 설계가 아니라 앱 결함 표면 설계와 짝이다.

### golden과 검증

- **R5. 역할에 따른 golden 표기.** surge가 원인인 시나리오는
  `root_cause.mechanism`에 트래픽 급증을 쓴다. surge가 증폭기인 시나리오의
  root cause는 원 결함이고 surge는 `propagation`에만 기록한다 — 증폭기
  surge를 원인으로 답하면 오답으로 채점돼야 한다.
- **R9. 이상감지 기대값 기입 + 사후 검증.** golden 레코드에 RCA 원인 외에
  **어느 대상(target)의 어느 메트릭/로그가 주입 후 몇 분 내에 이상으로
  잡혀야 하는지**를 적는다(§3 `expected_anomalies`). 예상만으로 승격하지
  않는다 — 시나리오를 실제 1회 실행해 예상 지표가 유의미하게 움직였는지
  확인하는 **검증 패스**(§6)를 승격 조건으로 둔다. 검증 실패 시 golden을
  관측에 맞춰 고치는 게 아니라 주입(강도·지속시간·결함 표면)을 설계 의도에
  맞게 고친다 — [spec-evaluation.md](spec-evaluation.md) 양방향 과적합 금지
  승계.

## 3. 스키마 확장

[시나리오 설계 §4](spec-scenario-design.md)의 양성 시나리오 스키마에 부하
시나리오가 추가로 채우는 필드.

```yaml
injection:
  script: <상대 경로>
  parameters:
    load:
      journeys: [browsing, checkout]     # loadgen 여정 카탈로그에서 선택
      intensity_multiplier: 5            # baseline 대비 배수 (확정 — 실제 인시던트는
                                         # "평소의 N배"처럼 상대적으로 서술되므로(R0),
                                         # 절대 RPS가 아니라 배수로 표현한다)
      ramp: { up: 2m, hold: 8m, down: 1m }
      seed: 42
expected_anomalies:                      # R9 — 이상감지 기대값
  - target: <정본 target_id 또는 target_id_source>
    signal: <metric|log|trace>
    name: <예: apm.http.server.duration p95 / HikariCP timeout 로그 패턴>
    direction: <up|down|new-pattern>
    within: <주입 시작 후 감지 기한, 예: 5m>
    precondition: <이 기대의 전제가 되는 관측 설정 — 알람 정책·이상감지 대상 등>
```

`precondition`은 이상감지 설정과의 정합을 위한 것이다. 기대가 성립하려면 관측
쪽에 해당 메트릭이 실제로 걸려 있어야 하며, 이를 기록해 두지 않으면 설정
변경 후 "왜 안 잡히는지"를 추적할 수 없다.

## 4. 커버리지 매트릭스와 카탈로그 후보

### 4.1 관측 파이프라인 제약 (lucida-next 코드 확인, 2026-07-14)

시나리오 기대값은 관측 쪽이 실제로 잡을 수 있는 것에만 걸어야 한다. 코드
조사(`../lucida-next` observer/RCA)로 확인된 제약:

1. **coverage gate** — stream anomaly는 `ai_coverages`에 (target×metric)으로
   등록된 대상만 처리한다. 미등록 메트릭은 아무리 튀어도 침묵 —
   `expected_anomalies.precondition`에 coverage 등록을 반드시 적고, 검증
   패스에서 선행 확인한다.
2. **로그 신규 패턴은 ERROR 이상만 즉시 발화** — log anomaly의
   `novel_template`은 severity ERROR(≥17) 이상만 바로 이벤트가 된다. WARN
   신규 템플릿은 즉발 아님. 로그축 기대값을 거는 시나리오는 해당 상황에서
   앱이 **ERROR 레벨 로그**를 찍는지 먼저 확인한다.
3. **forecast는 기대값에서 제외** — class=prediction이라 인시던트 승격 대상이
   아니다.
4. **인시던트 승격에 judge 층이 있다** — 이벤트(class=anomaly) → event
   cluster → incident judge의 promote/drop 판정을 거쳐야 인시던트다. 음성
   시나리오의 "이상감지 O / 장애 X" 경계는 이 judge 층에 있다(§5).
5. **RCA 원인 단위는 target_id** — 프로세스/pod/SQL/엔드포인트는 후보로
   승격되지 않고 dimension 라벨로만 보존된다. golden의 `cause.entity`는
   target 단위, low-level 디테일은 `dimension`으로 적는다
   ([작성 규칙](spec-scenario-authoring.md) expected_depth와 정합).

탐지기별 기대 가능한 reason: stream=`metric_anomaly(level_shift)`,
trace=`distribution_shift(느려진 방향만)·error_chain·novel_path`,
log=`novel_template·rate_spike·freq(ERROR 빈도)·rate_drop`.

### 4.2 매트릭스 (기존 시나리오 + 신규 후보)

행 = 원인 유형(RCA 축), 열 = 기대 현상(이상감지 축·탐지기 reason 기준).
`기존`은 rca-scenario-runner의 commerce(구 plopvape-shop) 카탈로그, `L*`/`N*`은
§4.3 신규 후보. 빈 칸이 백로그다.

| 원인 유형 (계열) | 메트릭 level shift | 에러 로그 급증 (rate_spike/freq) | 로그 신규 패턴 (novel_template) | latency 분포 변화 | 에러 전파 (error_chain) | 신규 경로 (novel_path) |
| --- | --- | --- | --- | --- | --- | --- |
| 트래픽 폭주 (APM) | 기존 Black Friday→**L1 개정** | | | L1 | | |
| 커넥션 풀 고갈 (APM/DPM) | 기존 PG Pool Exhaustion | **L2** | **L2** (HikariCP timeout ERROR) | L2 | | |
| 재시도 폭풍/CB (APM) | **L3** (내부 호출량) | L3 | L3 (CB open 로그 — 레벨 확인 필요) | | L3 | |
| 캐시 장애+부하 (APM/DPM) | **L4** (DB 세션 급증) | | | L4 | | **L4** (cart→DB 직행) |
| slow query (DPM) | 기존 Payment Row Lock | | | | | |
| lock 경합 (DPM) | 기존 Inventory Row Lock | | | | | |
| 배치-온라인 경합 (DPM/혼합) | **L5** | | | L5 | | |
| 이벤트 백본 적체 (Kafka/outbox) | **L6** ⚠️ 관측 gap 확인 선행 | | | | | |
| cross-domain 전파 (APM) | | | | **L7** | **L7** | |
| CPU 고갈 (SMS/KCM) | 기존 Noisy-Neighbor, PG CPU Throttle | | | | | |
| 메모리 (SMS) | 기존 Memory Leak | | | | | |
| 변경 (change) | 기존 Deployment Change 양성/음성 | | | | | |
| 음성: 흡수된 surge | **N1** (탐지 O·승격 X) | | | | | |
| 음성: 계절성 피크 | N2 — **연기** (계절성 신뢰까지 데이터 필요) | | | | | |

### 4.3 신규 후보 목록

| id | 이름 | surge 역할 | 주입 (R2) | 핵심 기대 탐지 | RCA 근거 (수집 데이터) | 우선순위 |
| --- | --- | --- | --- | --- | --- | --- |
| L1 | Black Friday 개정판 — 배수 기반 north-south 폭주 | 원인 | tb-runner | metric level shift(호출량·CPU) + latency 분포 이동 | 메트릭 시계열 + trace + cohort(전 서비스 공통) | **1차** |
| L2 | surge × 축소된 HikariCP pool — 평시 무증상 결함 발현 | 증폭기 | tb-runner + pool 축소 배포 | novel_template(HikariCP timeout ERROR) + rate_spike + DPM 세션 급증 | dpm_session + 로그 + 변경 이력(pool 설정) | **1차** |
| L3 | 재시도 폭풍 — 하류 지연 + retry 증폭 | 원인(east-west) | 클러스터 내부 | 내부 호출량 level shift + error_chain (novel_path 아님 — 기존 경로 반복) | trace error chain + 메트릭 | 2차 |
| L4 | 캐시 스탬피드 — Redis 장애 + surge → DB fallback | 증폭기 | tb-runner + Redis 장애 | novel_path(cart→DB 직행) + DPM 세션 급증 | trace novel path + dpm_session | 2차 |
| L5 | 배치-온라인 경합 — 정산 배치 시간대 surge 중첩 | 증폭기 | tb-runner (배치 스케줄 정렬) | DPM wait/lock + 시간대 latency | dpm_topsql/session + 배치 주기 상관 | 2차 |
| L6 | Kafka/outbox 적체 — surge로 발행량 급증 | 증폭기 | tb-runner | ⚠️ outbox lag 노출 메트릭이 수집에 있는지 **선행 확인** — 없으면 관측 gap부터 | (확인 후 확정) | 보류 |
| L7 | cross-domain 전파 — 체크아웃 surge → banking transfer 폭주 | 원인 | tb-runner | 도메인 경계 넘는 error_chain/latency | trace + 토폴로지 Ring2(상류 추적) | 2차 |
| N1 | 흡수된 surge (음성) | — | tb-runner (낮은 배수) | §5 — 3층 비대칭 | cohort·메트릭 (에러율 정상 근거) | **1차** |
| N2 | 계절성 피크 (음성) | — | — | **연기** — stream 계절성 버킷 신뢰까지 축적 필요 (§7) | — | 연기 |

## 5. 음성 부하 시나리오

부하가 개입하지만 장애는 아닌 상황. 핵심은 **이상감지 층과 인시던트/RCA 층의
정답이 비대칭**이라는 것이다 — 부하 증가는 이상으로 잡는 것이 맞지만, 그것이
곧 장애 판정·원인 확정을 정당화하지는 않는다.

| 패턴 | 상황 | 이상감지 정답 | RCA/인시던트 정답 |
| --- | --- | --- | --- |
| **흡수된 surge** (N1) | 일회성 폭주가 왔지만 시스템이 버텨 에러율·지연 미발생 | **탐지 O** (트래픽 메트릭 이상 — 못 잡으면 미탐) | **장애 아님** — "트래픽 폭주로 장애 발생" 식 원인 확정은 과확신 오답 |
| **계절성 피크** (N2, 연기) | 매일 반복되는 diurnal 피크 수준의 완만한 증가 | **탐지 X** (반복 계절성 — 울리면 오탐, 계절성 학습 테스트) | 해당 없음 (인시던트 아님) |

N1의 기대값은 관측 파이프라인 구조(§4.1-4)에 따라 **3층으로** 적는다:
①탐지 층 — 트래픽 메트릭 `metric_anomaly` 이벤트 생성 O, ②승격 층 —
incident judge가 promote하지 않거나(drop/hold), promote되더라도 사용자 영향
근거 없음, ③RCA 층 — 단일 원인 확정 없이 "부하 증가는 있었으나 영향
없음"으로 보존. N2는 stream anomaly의 계절성 버킷(일간 24버킷 3주기, 주간
168버킷 3주)이 신뢰 상태에 도달할 만큼 baseline이 축적된 뒤에 설계한다 —
현재 연기(§7).

golden 표기는 [시나리오 설계 §5](spec-scenario-design.md) 음성 규율을 따르되
(`has_single_cause: false`, `expected_status: insufficient` 등), 흡수된 surge
패턴은 `expected_anomalies`에 트래픽 이상 탐지를 **양성 기대값으로** 함께
기입한다 — 같은 시나리오가 이상감지에는 true positive, RCA에는 guardrail로
작동한다.

## 6. 검증 패스 (승격 조건)

시나리오를 카탈로그/golden에 승격하기 전에 1회 이상 실제 실행하고 다음을
확인한다.

1. **증상 발현**: `expected_anomalies`의 각 항목이 `within` 안에 실제
   관측되었다 (수집 데이터 또는 알람 history 기준).
2. **강도 적정성 (캘리브레이션)**: 증상은 나오되 시스템은 살아있다. 4 vCPU
   worker 규모에서 과한 강도는 전면 장애(모든 것이 죽어 원인 특정이 불가능한
   상태)를 만들고, 약한 강도는 무증상이다 — 두 극단 모두 승격 불가이며 강도를
   조정해 재실행한다.
3. **회복 확인**: surge 종료 후 시스템이 정상 지표로 복귀했다 (R3의 회복
   곡선).
4. **간섭 없음**: baseline loadgen이 실행 내내 중단·재시작 없이 유지되었다
   (R1).

## 7. 미결

- R9 검증 패스의 자동화 범위: 수동 확인으로 시작하되, 알람 history 매칭은
  기존 검증 도구(rca-scenario-runner 검증 루프)에 얹을 수 있는지.
- **L6 관측 gap**: Kafka consumer lag/outbox 적체를 노출하는 메트릭이 현재
  수집 계약에 있는지 확인 — 없으면 시나리오 전에 메트릭 노출 작업이 선행.
- **N2 연기**: 계절성 평가는 stream anomaly 계절성 버킷이 신뢰 상태가 될
  만큼 baseline 축적 후 재개(일간 3일+, 주간 3주+ — loadgen 상주 시작
  2026-07-13 기준).
- L3의 Resilience4j circuit breaker open 로그가 ERROR 레벨인지 확인(§4.1-2
  전제) — WARN이면 로그축 기대값 제외.
- 기존 Black Friday 시나리오(절대 동시성 5→500 정의)의 L1 개정 —
  rca-scenario-runner 레포 수정 필요(배수 정의·tb-runner 주입·
  expected_anomalies 추가).
