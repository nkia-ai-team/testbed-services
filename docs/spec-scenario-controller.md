---
title: Adaptive Scenario Controller 설계
status: Ready for controlled live validation
owner: project
last_reviewed: 2026-07-16
tags: [scenario, controller, adaptive-load, safety, capture]
summary: 시나리오의 사전검사, 단계별 주입, 실시간 판정, 자동 중단·복구, 캡처와 평가 승격을 결정론적으로 수행하는 공통 controller 계약을 정의한다.
---

# Adaptive Scenario Controller 설계

## 1. 목적과 비목표

controller의 목적은 시나리오마다 사람이 지표를 보며 강도를 조정하는 방식을
없애고, 같은 입력과 같은 관측값에 대해 같은 결정을 내리는 폐루프 실행을
보장하는 것이다. 부하·장애 주입 script는 원자적인 `run`, `cleanup`, `dry-run`
동작만 제공하고, 단계 상승·유지·중단·캡처 판단은 controller가 공통으로 소유한다.

controller는 golden에 맞춰 결과를 만들거나 기대값을 관측에 맞춰 낮추지 않는다.
목표 신호가 나오지 않으면 calibration 실패로 보존하고, 대체 원인이 관측되면
성공 조건이 충족돼도 평가 케이스로 승격하지 않는다.

## 2. 실행 모드

| mode | 의미 | 강도 변경 | 평가 데이터 승격 |
| --- | --- | --- | --- |
| `dry_run` | schema·위치·script hash·시간·cleanup 계획만 검증 | 없음 | 불가 |
| `calibration` | 사전 선언된 ladder 안에서 목표 증상을 찾음 | 자동 | 불가 |
| `evaluation` | calibration에서 확정한 단일 profile을 재현 | 금지 | 검증 통과 시 가능 |
| `cleanup` | 이전 실행의 잔존 상태를 멱등 복구 | 없음 | 해당 없음 |

순수 판정 core는 부작용을 만들지 않으므로 `calibration|evaluation`만 해석한다.
`dry_run|cleanup`은 dispatcher lifecycle 모드다. evaluation은 level 하나인 fixed
profile만 허용하며, 두 계층 사이의 mode 변환은 dispatcher가 명시적으로 수행한다.

`calibration`의 이전 약한 단계까지 포함한 전체 데이터는 진단용이다. 정식 평가는
확정 강도를 `evaluation`에서 고정 재실행한 데이터만 사용한다.

## 3. 상태 머신

```text
IDLE
  → PREFLIGHT
  → BASELINE_CHECK
  → ARMING
  → INJECTING(level N)
  → SETTLING
  → EVALUATING
      ├─ insufficient + safe → ESCALATING → INJECTING(level N+1)
      ├─ success → HOLDING
      ├─ ruled_out → STOPPING
      └─ unsafe → ABORTING
  → STOPPING
  → CLEANING
  → RECOVERY_CHECK
  → CAPTURE_WAIT
  → CAPTURING
  → VALIDATING
  → SUCCEEDED | CALIBRATION_FAILED | FAILED | DIRTY
```

`DIRTY`는 cleanup 또는 회복 확인 실패다. `DIRTY`가 존재하면 다른 시나리오를
시작하지 않는다. scenario별 state 외에 전역 coordinator store가 run lease,
fencing token, active run, dirty run을 원자적으로 보존한다. watchdog과 controller는
같은 fencing token의 cleanup claim을 경쟁하며 한쪽만 cleanup을 수행한다. 재시작
뒤에도 lease 만료와 recovery 검증 전에는 새 실행을 허용하지 않는다.

## 4. 시간 계약과 캡처 정책

- 모든 시각은 UTC RFC3339로 저장한다.
- `t1`은 첫 실제 주입이 효력을 내기 시작한 시각이다.
- `t2`는 마지막 부하가 종료되고 마지막 장애가 실제로 해제된 시각이다.
- 복합·adaptive 실행은 `t1=min(step.started_at)`,
  `t2=max(step.effect_ended_at)`으로 계산한다.
- export query 범위는 폐구간 `[t1-2h, t2+45m]`이다.
- export는 `t2+45m` 이후 시작한다. t1 이전 2시간은 저장소의 과거 데이터를
  조회하므로 t1 전에 별도 dump 프로세스를 시작하지 않는다.
- `/var/lib/lucida/ai-models/stream-anomaly/global/v1/model.json`은
  `t2+45m` 시점에 별도 snapshot으로 저장한다.
- `golden.anomaly.json`은 만들지 않는다.
- calibration·abort·목표 미달 실행도 진단용으로 캡처할 수 있지만
  `run_class=calibration|failed`로 기록하며 evaluation dataset에 포함하지 않는다.
- t1 이전 2시간에 다른 시나리오, 잔존 장애 또는 baseline 중단이 있으면
  `BASELINE_CHECK`에서 시작을 거부한다.

## 5. YAML 계약

```yaml
controller:
  mode: calibration                 # dry_run|calibration|evaluation
  state_store: /app/state/<scenario-id>.json
  tick_interval: 15s
  settle_after_change: 2m
  max_injection_duration: 20m       # capture wait/export 시간은 제외

  baseline:
    clean_window: 2h
    required:
      - check: baseline_process_active
      - check: no_recent_scenario_overlap
      - check: target_health

  profile:
    kind: adaptive_ladder
    levels:
      - id: l1
        parameters: {target_rps: 60}
        min_hold: 3m
      - id: l2
        parameters: {target_rps: 70}
        min_hold: 3m
      - id: l3
        parameters: {target_rps: 80}
        min_hold: 5m

  observations:
    - id: achieved_rps
      adapter: loadgen_summary
      query: iterations.rate
      freshness: 30s
    - id: user_p95
      adapter: prometheus
      query: <approved query id>
      freshness: 1m
    - id: pool_pending
      adapter: prometheus
      query: <approved query id>
      freshness: 1m

  success:
    all:
      - {observation: achieved_rps_ratio, op: gte, value: 0.90}
      - {observation: user_p95_sec, op: gte, value: 1.0}
      - {observation: pool_pending, op: gte, value: 5}
    consecutive_ticks: 2

  must_rule_out:
    any:
      - {observation: inventory_fast_fail_rate, op: gt, value: 0}
      - {observation: unrelated_db_lock_count, op: gt, value: 0}

  abort:
    any:
      - {observation: entry_health, op: ne, value: 200}
      - {observation: user_p95_sec, op: gt, value: 10}
      - {observation: user_5xx_rate, op: gt, value: 0.30}
    consecutive_ticks: 3

  recovery:
    all:
      - {observation: scenario_process_count, op: eq, value: 0}
      - {observation: entry_health, op: eq, value: 200}
    timeout: 10m

  capture:
    enabled: true
    pre_window: 2h
    post_window: 45m
    model_snapshot: /var/lib/lucida/ai-models/stream-anomaly/global/v1/model.json
    create_golden_anomaly: false
```

`evaluation` mode에서는 `profile.kind=fixed`와 level 하나만 허용한다. controller는
calibration 결과를 자동으로 golden으로 승격하지 않고, 승인된 profile id와
script SHA를 evaluation YAML에 명시해야 한다.

캡처의 evaluation 승격은 라벨만으로 얻을 수 없다. capture 도구는 controller
`result.json`의 evaluation mode, fixed profile, succeeded outcome, cleanup/recovery
성공, `dirty=false`, script/catalog SHA-256을 검증해야 한다.

## 6. 관측 어댑터와 판정

초 단위 안전 판정은 이상감지 결과가 아니라 직접 시스템 지표를 사용한다.
이상감지·클러스터·인시던트 결과는 post-run validation 대상이다.

필수 adapter는 다음과 같다.

- `loadgen_summary`: achieved RPS, dropped iteration, request result.
- `http_probe`: entry health, latency, 사용자 상태 코드.
- `prometheus`: 승인된 query id로만 metric 조회. YAML 임의 PromQL은 금지한다.
- `kubernetes`: Pod Ready, restart, resource, placement.
- `database`: scenario tag가 붙은 session·lock과 정상 대안 신호.
- `business_probe`: 재고, 계좌, 배차, 주문·결제 최종 상태.
- `capture_status`: export와 model snapshot의 완료·checksum.

모든 관측값은 `value`, `observed_at`, `source`, `freshness`, `quality`를 갖는다.
stale 또는 error 품질은 성공으로 계산하지 않는다. 안전 지표가 stale이면 다음
단계로 상승하지 않고, 연속 stale이 한계를 넘으면 안전 중단한다.

판정 우선순위는 `abort → must_rule_out → success → escalate`다. 동일 tick에서
성공과 중단이 함께 참이면 중단이 우선한다.

## 7. 단계 상승 규칙

- ladder와 최대 강도는 실행 전에 YAML로 고정한다.
- controller는 사전에 없는 level을 생성하지 않는다.
- level 변경 후 `settle_after_change` 동안 성공·상승 판정을 보류하되 abort는
  계속 평가한다.
- 한 번에 하나의 강도 축만 변경한다. RPS와 resource limit을 함께 바꾸는
  시나리오는 복합 step으로 명시하고 각각의 원인을 구분할 수 있어야 한다.
- 목표 미달이면서 안전하고 최소 hold가 끝났을 때만 다음 level로 이동한다.
- 마지막 level에서도 목표 미달이면 `CALIBRATION_FAILED`다.

## 8. 안전·cleanup·watchdog

- live mode는 `feasibility=ready`인 모든 injection point만 허용한다.
- `prerequisite`, `defer`, 실행 위치 정책 위반, script hash 불일치는 subprocess
  생성 전에 거부한다.
- controller heartbeat를 별도 watchdog이 감시한다. heartbeat timeout 또는 runner
  종료 시 전역 coordinator의 fencing token 기반 cleanup claim을 획득한 경우에만
  동일 script의 `cleanup`을 한 번 호출한다.
- cleanup은 역순이며 멱등이어야 한다. DB는 scenario-tagged session만, k6는
  scenario script tag만, Kubernetes는 실행 직전 저장한 원 spec만 복원한다.
- cleanup 성공 뒤에도 recovery 조건이 충족되지 않으면 `DIRTY`다.
- `DIRTY` 해제는 명시적 cleanup과 recovery validation이 모두 성공해야 한다.

## 9. 실행 산출물

각 실행은 다음 파일을 남긴다.

```text
runs/<run-id>/
  plan.json                 # 정규화된 controller·execution 계획
  state.json                # 마지막 영속 상태와 heartbeat
  decisions.jsonl           # tick별 관측·판정·상승/중단 근거
  script.sha256
  timeline.json             # t1/t2, level·step 구간
  cleanup.json
  recovery.json
  capture.json              # query window, export, model snapshot, checksums
  result.json               # calibration/evaluation/failed/dirty 판정
```

비밀값, Secret 원문, 전체 환경변수, 인증 header 값은 산출물과 프로세스 argv에
기록하지 않는다.

## 10. 64개 시나리오 적용 규칙

64개 파일은 서로 다른 controller를 구현하지 않는다. 다음 script family를
재사용하고 YAML profile·target·판정 조건만 달리한다.

- north-south load
- east-west Job load
- DB session/lock/DDL
- Kubernetes resource/lifecycle
- external MockServer
- Kafka/outbox/consumer
- host resource
- network path
- business fault/probe
- composite timeline

각 시나리오는 `run`, `cleanup`, `dry-run`을 제공하고, live 준비가 안 된 family는
명확한 prerequisite와 함께 `dry-run`만 성공해야 한다. 64개 전체 dry-run은
원격 접속이나 mutation 없이 script 존재·hash·schema·위치·schedule·cleanup·capture
계획을 검증해야 한다.

64개 entrypoint는 모두 동일한 catalog/manifest/location/profile/query closure와
plan/run/cleanup dry-run 계약을 사용한다. 이 가운데 정본 위치·파라미터·복구 계약과
완전한 controller runtime이 함께 확정된 F01-R, F01-H, F01-G, F02-R, F03-G,
F04-R, F05-G, F06-R, F07-H, F08-H, F09-P, F11-R, F11-G, F12-H만 normalized
plan에서 live가 허용된다. 이 14개도 현재 plan
digest, 정확한 confirmation, executor hash, 전역 lease/fencing, 2시간 baseline
적격성, profile-control preflight를 모두 통과해야 실제 apply가 가능하다.

64개 외부 manifest는 `/api/scenario-manifests`와
`/api/scenario-manifests/{id}`에서 조회한다.

Live controller는 lease 획득 전에 current normalized plan과 contract tree를 run별
immutable recovery capsule로 원자 발행한다. apply/cleanup/watchdog/manual recovery는
이 capsule만 사용하므로 mounted source가 실행 도중 바뀌어도 원래 target과 payload를
복구한다. 각 profile mutation은 원격 호출 전에 fsync된 intent로 기록하고 성공 후
complete로 전환한다. crash 시 intent-only 항목도 possibly-applied로 간주해 역순
cleanup한다.

나머지 50개는 plan-only다. F12-H는 `rca-testbed-commerce/testbed-product/product-service`
정본 target과 CPU `500m→250m→100m→50m` ladder, 35 RPS 부하, exact snapshot/restore를
사용한다. product APM p95/error와 CPU throttle 상승, control service 안정, network error 0을
동시에 확인하고 cleanup 뒤 CPU limit `500m`와 pod Ready를 복구 gate로 확인한다. F01-G와 F11-R은 각각
delay와 fallback overload의 미실측 경계를 adaptive ladder 안에서만 탐색하고,
F05-G는 commerce payment의 정본 target과 exact image rollback을 사용한다. 파일
존재나 개념적 feasibility만으로 다른 시나리오를 라이브 실행 준비 완료로 해석하지 않는다.

## 11. 검증 계약

- 상태 전이, 조건 우선순위, consecutive tick, stale observation, max duration.
- calibration ladder 상승과 fixed evaluation에서 상승 금지.
- timeout·controller crash·cleanup 실패·recovery 실패.
- dirty state에서 후속 실행 차단.
- t1/t2와 `[t1-2h,t2+45m]` 계산, model snapshot 시각.
- calibration/failed 캡처가 evaluation으로 승격되지 않음.
- 64개 manifest와 script의 side-effect-free dry-run.
- live dispatcher가 dry-run과 같은 정규화 plan을 사용함.

실제 부하·장애 주입은 이 정적·단위·통합 dry-run 검증이 모두 통과한 뒤 별도
승인된 실행 단계에서만 수행한다.
