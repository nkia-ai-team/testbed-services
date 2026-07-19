---
title: 시나리오 부하 주입·모니터링·캡처 runbook
status: Active
owner: project
last_reviewed: 2026-07-18
tags:
  - scenario
  - load
  - operations
  - capture
summary: 시나리오 설명 작성부터 순차 부하 주입, adaptive 강화, cleanup, 데이터·모델 저장과 실패 재개까지의 운영 절차.
---

# 시나리오 부하 주입·모니터링·캡처 runbook

## 1. 범위와 완료 조건

이 runbook은 `rca-scenario-runner`가 109에서 시나리오를 순차 실행하고 평가
케이스를 저장하는 전 과정을 다룬다. 챗봇과 프론트엔드는 범위 밖이다. 사용자
지시에 따라 104 Mongo 검증과 `golden.anomaly.json` 생성도 수행하지 않는다.

시나리오 하나의 완료 조건은 다음을 모두 만족하는 것이다.

1. AI가 작성한 시나리오 설명 정본이 존재한다.
2. 사전검사와 2시간 clean window가 통과한다.
3. 승인된 위치에서만 부하·장애가 주입된다.
4. 기대 사용자 영향과 원인 구분 증거가 연속 관측된다.
5. cleanup과 recovery가 성공하고 `dirty=false`다.
6. controller 성공이 확인된 실행만 `[t1-2h,t2+45m]` 데이터와 `t2+45m`
   시점 모델을 원자적으로 저장한다.
7. 저장본 `meta.json`의 시나리오 설명과 정본 SHA-256이 일치한다.

## 2. 설명 정본 — AI 작성, 코드는 운반·검증만

정본은
[`scripts/scenarios/registry/scenario-metadata.json`](../scripts/scenarios/registry/scenario-metadata.json)에
둔다. 자동 생성기나 실행 스크립트가 ID·slug로 설명을 만들어서는 안 된다.
시나리오 설계자가 다음 여섯 필드를 직접 작성한다.

| 필드 | 의미 |
| --- | --- |
| `title` | 사람이 빠르게 식별할 수 있는 사건 이름 |
| `description` | 원인과 전파를 포함한 시나리오 개요 |
| `cause` | 평가에서 구분할 근본원인 메커니즘 |
| `injection_summary` | 어디에 무엇을 어떻게 주입하는지 |
| `user_impact` | 실제 운영 인시던트로 볼 사용자 영향 |
| `distinguishing_evidence` | 유사 장애·대안 원인과 구분하는 결정적 증거 |

runner는 live 실행 전에 해당 ID의 여섯 필드가 모두 있는지 검사한다. 캡처
스크립트는 내용을 생성하거나 수정하지 않고 그대로 `meta.json`에 복사한다.
정본 객체의 canonical JSON SHA-256도 함께 저장하며 불일치하면 케이스 승격을
거부한다.

## 3. 실행 전 검사

다음 검사가 하나라도 실패하면 주입을 시작하지 않는다.

- runner readiness: kubeconfig, SSH key, dispatcher, catalog, capture script,
  Docker socket, 저장소 자격증명, 모델 snapshot target.
- metadata readiness: 현재 live queue의 모든 ID가 설명 정본에 존재한다.
- coordinator: active lease와 dirty run이 없다.
- baseline: 상주 loadgen이 정상이고 이전 실제 효과 종료 뒤 2시간이 지났다.
- target: 대상 pod·서비스·DB와 cleanup 역연산이 확인된다.
- placement: north-south=`tb-runner`, east-west=클러스터 내부,
  DB 직접=`tb-runner`, 실제 네트워크 경로=`.57` 규칙을 지킨다.

## 4. 자동 순차 실행과 adaptive 모니터링

큐는 한 번에 한 시나리오만 실행한다. 신규 live 승격은 현재 큐의 뒤에만 자동
추가한다. 각 단계는 다음 상태 전이를 따른다.

```text
waiting_clean_window
  → running/preflight
  → apply level
  → settle
  → observe
  → succeed | escalate | abort
  → reverse cleanup
  → recovery verification
  → waiting_capture
  → waiting_clean_window
```

- 기대 지표가 충분히 움직이지 않으면 controller가 사전에 승인된 다음 강도로
  올린다. 임의 배수나 런타임 shell 조작은 금지한다.
- 성공은 사용자 영향과 원인 구분 증거를 `consecutive_ticks`만큼 연속 확인해야
  한다.
- pod 전체 불능, 대안 원인 검출, 관측 stale, 최대 주입 시간 초과는 abort 조건이다.
- 의미 오류, cleanup·recovery 실패, 캡처 실패는 다음 시나리오로 건너뛰지 않고
  queue를 pause한다.
- `safety_observation_unavailable` 또는 `decision_observation_unavailable`처럼
  일시적인 관측 불능으로 정상 cleanup된 실행은 같은 시나리오를 최대 2회 자동
  재시도한다. 실패 실행은 `state.json`, `result.json`, timeline 등 진단 기록만
  남기고 평가 데이터 덤프는 만들지 않는다. cleanup의 `t2`부터 2시간 clean
  window가 지난 뒤에만 다시 주입하며, 재시도 한도를 소진하거나 다른 사유이면
  fail-closed로 pause한다.
- 시작 전 검증 실패로 실제 효과가 없었다면 문제를 수정한 뒤 같은 시나리오를
  즉시 재개할 수 있다. 실제 효과가 있었다면 cleanup 시각 기준 clean window를
  다시 적용한다.

## 5. cleanup과 복구

복합 시나리오는 하위 주입을 역순으로 정리한다. 각 executor는 최초 상태 snapshot,
마지막 적용 상태, run-scoped PID·Job·expectation을 소유해야 한다. 단순히
"정상값으로 추정"해 덮어쓰지 않는다.

cleanup 성공 뒤에도 다음을 연속 확인한다.

- 대상 service/pod ready와 원래 replica·resource·probe·env 복구
- MockServer expectation·pulse 제거와 snapshot 복원
- DB lock/session 종료와 index 정의 복구
- Kafka backlog drain 또는 시나리오별 최종 업무 invariant
- coordinator `active_lease=null`, `dirty_run=null`

한 항목이라도 복구되지 않으면 `DIRTY`로 남기고 자동 진행을 중단한다.

## 6. 캡처와 저장 계약

- `t1`: 가장 이른 실제 장애·부하 효과 시작 시각
- `t2`: 가장 늦은 부하 종료 또는 장애 해제 시각
- 데이터 범위: `[t1-2h,t2+45m]`
- export 시작: 벽시계가 `t2+45m`에 도달한 뒤
- 모델: 같은 시점의
  `/var/lib/lucida/ai-models/stream-anomaly/global/v1/model.json`
- `golden.anomaly.json`: 생성 금지

controller 성공이 확인된 케이스만
`/data/eval-cases/case-<scenario-id>-<run-suffix>/`에 staging 후 원자적 rename으로
저장한다. 실패·중단 실행은 이 경로에 생성하지 않는다. 필수 산출물은 VM export,
ClickHouse table별 Parquet, PostgreSQL custom dump, 모델 JSON·checksum,
`meta.json`이다.

`meta.json`에는 시간·해시와 함께 다음 객체가 반드시 들어간다.

```json
{
  "scenario_metadata": {
    "title": "...",
    "description": "...",
    "cause": "...",
    "injection_summary": "...",
    "user_impact": "...",
    "distinguishing_evidence": "..."
  },
  "scenario_metadata_sha256": "<64-hex>"
}
```

## 7. 저장 후 검증

다음 read-back을 통과하기 전에는 저장 완료로 보고하지 않는다.

```bash
CASE=/root/rca-scenario-runner/eval-cases/<case-id>

jq -e '
  (.scenario_metadata.title | length > 0) and
  (.scenario_metadata.description | length > 0) and
  (.scenario_metadata.cause | length > 0) and
  (.scenario_metadata.injection_summary | length > 0) and
  (.scenario_metadata.user_impact | length > 0) and
  (.scenario_metadata.distinguishing_evidence | length > 0) and
  (.scenario_metadata_sha256 | test("^[0-9a-f]{64}$")) and
  .golden_anomaly_file == false
' "$CASE/meta.json"

(cd "$CASE/models/stream-anomaly/global/v1" && sha256sum -c model.json.sha256)
```

정본 객체를 canonical compact JSON으로 다시 직렬화한 SHA-256과
`meta.json.scenario_metadata_sha256`도 대조한다.

## 8. 운영 중 확인할 상태

- `GET /api/live-queue`: 현재 phase, index, run id, pause reason
- `GET /api/live-queue/readiness`: 실행·저장 전제조건
- `GET /api/active`: 현재 mutation lease
- `state/capture-jobs.json`: capture window, retry, failure, metadata
- `runs/<run-id>/state.json`: adaptive 결정, cleanup, recovery
- `runs/<run-id>/capture-error.log`: 저장 실패 상세

캡처는 최대 3회 재시도하며 30초·60초 backoff를 사용한다. 최종 실패 시 queue는
pause되고 다음 시나리오를 시작하지 않는다.

## 9. 재시작·일시 정지 복구

- `state/capture-jobs.json`은 runner의 엄격한 스키마가 소유한다. 운영자가 계산값이나
  설명 해시를 최상위 job 필드에 임의로 추가하지 않는다. 설명 해시는 캡처 결과와
  `meta.json`에 기록하며 job 내부에서는 `scenario_metadata`로부터 계산한다.
- runner가 `waiting_capture` 중 재시작되면 미완료 capture worker를 자동 재기동한다.
- queue 상태의 `auto_retry_counts`, `last_auto_retry_reason`은 일시 관측 실패의
  자동 복구 횟수와 최근 사유를 보존한다. 실패 실행에는 캡처 job을 만들지 않으며,
  이 상태는 컨테이너를 재시작해도 유지된다.
- queue가 일시 정지됐더라도 현재 job 상태가 `pending`, `model_requested`,
  `completed`이면 `POST /api/live-queue/resume`은 같은 run의 `waiting_capture`를
  보존한다. 동일 장애를 다시 주입하거나 clean window로 건너뛰지 않는다.
- job이 실제 `failed`이면 원인을 수정한 뒤 재시도 정책에 따라 같은 시나리오를
  다시 실행한다. 효과가 발생했던 run은 cleanup 기준 clean window를 적용한다.
- 복구 뒤에는 `GET /api/live-queue`, job 상태, `capture-complete.json`, 최종
  `meta.json`을 함께 읽어 queue index가 정확히 한 번만 증가했는지 확인한다.

## 10. 관측 계약 사전 검증

live 승격 전에는 manifest에 지표 이름이 존재하는지만 보지 않고 실제 운영 저장소에
동일 label 조합의 시계열이 있으며 값 단위가 임계값과 일치하는지 확인한다.

- APM p95 정본: `apm.agent.otel.java.percentile95`
- gateway 조회: `prometheus.apm_service_p95`와
  `parameters.service_name=commerce-gateway`
- APM percentile 임계값 단위: milliseconds. 예를 들어 500ms는 `500`으로 쓴다.
- live probe 서비스 allowlist에 manifest의 `service_name`이 포함돼야 한다.
- dry-run 이후 production probe를 직접 한 번 호출해 `quality=good`, 유한한 값,
  freshness 충족을 확인한다.
- `safety_observation_pending`이 최소 유지시간 뒤에도 계속되면 timeout까지 기다리지
  말고 abort·must-rule-out에 쓰인 각 signal의 quality를 개별 점검한다.
- SSH·kubectl·Prometheus처럼 일시 실패 가능한 read-only probe는 한 번의 error를
  곧바로 controller streak 초기화에 사용하지 않는다. 같은 tick에서 즉시 1회
  재시도하고 두 번 모두 unusable일 때만 fail-closed 판정에 반영한다.

2026-07-17 F03-G에서는 존재하지 않는
`http_server_request_duration_seconds_bucket`과 초 단위 임계값 `0.5`를 사용했고,
gateway가 APM allowlist에도 빠져 있었다. 실제 수집 지표와 millisecond 단위 계약으로
교체하고 현재 큐의 동일 구형 관측식(F01-G, F07-H, F09-P)을 함께 수정했다.

같은 날 후속 실행에서는 checkout 정상 업무 결과가 `200`, `400`, `409`를 포함하는데
성공 조건이 HTTP `200`만 허용해 evaluation timeout이 발생했다. 영향 없음·fallback
성공 시나리오는 단일 HTTP 코드가 아니라 `business.checkout_invariant=true`를 정본으로
사용한다. 현재 큐의 동일 패턴인 F05-G와 F11-G도 함께 정정했다.

2026-07-19 F03-G 6차 실행(`F03-G-run-c296f50e`, 07-18) 사후 분석. 관측 계약
수리 이후에도 `evaluation_level_timeout`으로 실패했는데, 종료 시점 streak이
`success=1`이었고 잔존 live 스냅샷은 `achieved_rps=32.5`, `business_ok=true`,
`checkout_5xx_rate=0.0`, gateway p95 10~61ms로 전부 건강했다. 즉 장애 주입도
테스트베드도 아니라 **성공 판정이 hold 구간 내내 확정되지 못하다가 timeout
직전에야 처음 충족**된 것이다. 확인된 구조 결함 4건:

1. **판정 시간 예산 부족** — level `timeout=10m`이 부하 프로파일 총 길이
   11m(ramp 2m + hold 8m + down 1m)보다 짧다. settle 30s와 ramp를 빼면
   성공 3연속 tick을 만들 유효 창이 실질 7분대다.
2. **freshness 계약과 실제 지표 케이던스 불일치** — `user_p95`는 분당 2샘플
   (`:00`·`:15`)이 배치로 적재돼 최신 샘플 age가 주기적으로 60초에 근접·초과
   한다. `freshness=60s`에서는 tick 위상에 따라 stale 판정이 반복된다.
3. **unusable 신호의 streak 리셋** — 성공 조건에 필요한 신호 하나가 한 tick
   이라도 stale/error면 success streak이 0으로 돌아간다. 15s tick × 3연속
   요건과 결합하면 일시적 관측 요동만으로 성공이 무기한 지연된다. 앞선
   4개 run의 `safety_observation_unavailable` abort도 같은 뿌리다.
4. **tick 단위 기록 부재** — `runs/<run-id>/`에 최종 상태만 남고 tick별
   관측값·판정이 없으며 `logs/<run-id>.log`도 0바이트다. 컨테이너 재시작
   후에는 어떤 조건이 며칠 동안 흔들렸는지 재구성할 수 없다. 본 분석도
   tb-runner 잔존 `-live.json.tmp`와 VM 재질의로 우회했다.

수리 방향: F03-G manifest의 level timeout을 프로파일 길이 이상으로 확장,
`user_p95` freshness를 실측 케이던스에 맞게 완화(120s), controller의 tick별
관측·판정 로그를 `runs/<run-id>/ticks.jsonl`로 영속화, run 로그 캡처 수리.
성공·중단 조건의 의미(임계값·조건식)는 이 수리에서 변경하지 않는다.
