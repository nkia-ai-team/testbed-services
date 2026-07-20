---
title: 평가용 데이터 캡처·재생 설계
status: Draft
owner: project
last_reviewed: 2026-07-20
tags:
  - evaluation
  - testbed
  - data
  - replay
  - rca
summary: 평가 입력 데이터를 재현 가능하게 고정하는 방법을 정의한다. 사건 시간창을 3개 저장소(VM/ClickHouse/PostgreSQL)에서 파일로 떠서 케이스로 저장하고, 그 파일을 각 평가 담당자에게 전달하는 캡처 파이프라인과 케이스 계약(디렉터리 구조·저장 경로)을 다룬다. 재생 방법은 소비자 소유이며 §8에 알려진 함정만 참고로 남긴다.
---

# 평가용 데이터 캡처·재생 설계

이 문서는 [평가·실험 설계](spec-evaluation.md)가 정의한 채점 방법론을 **재현
가능한 입력** 위에서 돌리기 위한 데이터 계층을 고정한다. 평가가 공정하려면
"같은 입력 → 대상 버전만 바꿔 점수 비교"가 성립해야 하는데, 테스트베드는
살아있는 서비스·부하·장애로 데이터가 매번 새로 생겨 그대로는 재현이 안 된다.
이 문서는 그 입력을 **한 번 녹화(캡처)해 두고 반복 재생(평가)** 하는 구조를
정의한다.

관련: [테스트베드 환경 설계](spec-testbed-design.md),
[시나리오 작성 규칙](spec-scenario-authoring.md),
[Lucida Next 데이터 수집 참조](ref-lucida-next-data-collection.md),
[Lucida Next AI 기능 참조](ref-lucida-next-ai-features.md).

## 1. 문제와 원칙

- **재현성.** 스트리밍 이상감지·클러스터·인시던트·RCA를 평가하려면
  입력이 매 실행 동일해야 한다. 라이브 재실행은 타이밍 지터·부하 난수·GC로
  bit 단위 재현이 불가하므로 **점수용 평가에는 부적합**하다(통합·스모크용).
- **캡처는 데이터 계층에서.** 소비자가 여럿이고 각자 쿼리 패턴이 다르며 계속
  진화하므로 "응답 녹화/재생"은 깨진다. 대신 **저장소(DB)의 시간창 스냅샷**을
  얼리고, 평가 때 실제 쿼리를 날린다. 이러면 어떤 쿼리가 와도 일관되게 답한다.
- **오염 격리.** 파이프라인 각 단계는 공유 저장소에 되쓰기(write-back)를 한다
  (§3). 평가 중 단계를 재실행하면 원본이 오염되므로, 반드시 **격리된 일회용
  사본**에 복원해 돌리고 폐기한다.
- **책임 경계 — 캡처까지가 이 설계의 몫이다(2026-07-14 확정).** 이 파이프라인의
  산출물은 **케이스 파일**(§4)이고, 거기서 끝난다. 그 파일을 어떻게 먹을지
  (DB 복원 조회, 인프로세스 스트리밍 하니스, Kafka 재발행 등)는 **각 평가
  담당자(소비자)가 소유**한다. 기존 원칙 "데이터는 공유, 골든만 소비자별"이
  "**재생 방법도 소비자별**"로 확장된 것이다. 소비자가 알아야 할 함정은 §8에
  참고로 남긴다.

## 2. 두 단계 — 캡처와 평가

평가는 두 단계로 분리한다. 이 둘을 섞으면 혼란해진다.

**Phase A · 캡처 (녹화)** — 라이브 테스트베드, 일일 사이클(2026-07-20 개정, §2.1).

1. 테스트베드에 에이전트·collector 설치(최초 1회).
2. **일일 정상 세그먼트** 확보 — 매일 00:00~02:00(보호 구간, 주입 금지)을
   정상 부하로 유지하고, 그 2시간을 그날의 **공유 정상 prefix**로 1회 덤프한다.
   이상감지가 "평상시"를 학습하고 RCA가 비교할 기준을 만든다.
3. 02:00부터 시나리오 큐를 연속 실행: 장애 주입 → 인시던트 발생 → 회복 →
   다음 시나리오(간격 규칙은 [부하 시나리오 R6](spec-scenario-load.md)).
4. 시나리오마다 벽시계가 `t2+20m`에 도달한 뒤 `[t1-10m, t2+20m]` 창을 3개
   저장소에서 떠서 **시나리오 세그먼트**로 저장하고, 같은 시점의
   stream-anomaly 모델 artifact와 **골든 정답**을 기록한다.
5. 케이스 = 그날의 정상 세그먼트(참조) + 시나리오 세그먼트. 캡처 파이프라인이
   정상 prefix 사본을 시나리오 직전으로 시간 이동(shift)해 이어붙인
   **연속 시간축 완성본(`assembled/`)까지 생성해 동봉**하고, 레시피를
   `meta.json`에 기록한다(§2.1).

### 2.1 캡처 시간창 계약 (2026-07-20 개정 — 일일 사이클·세그먼트 분리)

> 2026-07-15 계약(시나리오당 단일 창 `[t1-2h, t2+45m]`)을 대체한다. 시나리오마다
> 정상 2시간을 실시간으로 기다리던 구조를 "일일 공유 정상 세그먼트 + 짧은
> 시나리오 창"으로 바꿔 대기 시간을 없애고 하루 처리량을 높인다. settle window
> 45m→20m 단축은 인시던트 담당자 확인(2026-07-20).

- `t1`은 **부하·장애 주입 시작 시각**, `t2`는 **주입 종료 시각**이며 둘 다
  UTC(Z)로 기록한다. 자동 캡처 입력은 모호한 offset·현지시각 변환을 허용하지
  않고 초 단위 `YYYY-MM-DDTHH:MM:SSZ` 형식만 받는다. (구 계약과 동일)
- **정상 세그먼트**: 매일 자정부터 2시간(**00:00~02:00 KST**, 2026-07-20
  확정 — meta에는 UTC로 기록, KST 병기)을 그날의 공유 정상 prefix로 1회
  덤프한다. 이 창은 **보호 구간**이다 — 주입 금지이며, 전날 마지막
  시나리오의 잔여 효과·회복이 자정 전에 끝나 있어야 한다. 덤프 전 검증은
  **경량**으로 한다(2026-07-20 결정): 수집 스택 가용성 확인 — 관측
  에이전트·collector·119 observer(AI 계층) 기동 + 각 저장소에 최신 데이터
  유입 — 까지만 게이트로 걸고, 무알람·무인시던트 전수 검증은 요구하지
  않는다. 저장 위치는 케이스 디렉터리와 분리된
  `normal-segments/<domain>/<날짜>/`.
- **시나리오 세그먼트**: 고정 조회 범위는 `capture_start = t1-10m`부터
  `capture_end = t2+20m`까지. 실제 export·dump·파일 복사는 벽시계가
  `capture_end`에 도달한 뒤 시작한다. `+20m`는 부하 지속 시간이 아니라
  수집·이상감지·클러스터·인시던트 판정의 **pipeline settle window**다.
- **조립은 캡처 파이프라인이 하고, 방향은 정상 prefix를 앞으로 민다
  (2026-07-20 확정)**: 시나리오 세그먼트는 **실제 벽시계 그대로** 둔다 —
  `t1`/`t2`·인시던트 시각·로그 본문 속 시각 문자열·PG dump·모델
  artifact·토폴로지 스냅샷 전부 재작성 불필요. 대신 정상 세그먼트의
  **사본**(VM export + CH parquet)에 Δ = `(t1−10m) − 정상 세그먼트 끝`을
  더해 시나리오 직전 `[t1−10m−2h, t1−10m]`에 붙인다. 결과물이
  `assembled/`(연속 시간축 완성본)이며 소비자는 이것만 쓰면 된다. 정상
  구간에는 인시던트·클러스터가 없어야 하므로(보호 구간) PG 쪽 shift는
  발생하지 않는다.
- **원본 불변은 유지된다**: shift는 `assembled/` 사본에만 적용한다. 원시
  세그먼트(`data/` + `normal-segments/`)의 event-time은 바꾸지 않는다(구
  계약 승계) — 실제 벽시계로의 역추적·검증은 원본으로 한다. 조립 레시피
  (`rebase`)는 meta에 남겨 재현·역산 가능하게 한다.
- **이음새(seam)는 소비자가 처리한다**: 밀려온 정상 prefix(자정 저점
  위상)와 시나리오 구간(실제 시각 위상)의 diurnal 수준 차이로 이음새에
  인위적 level shift가 생기고, prefix 구간은 벽시계 표기와 부하 위상이
  어긋난다(원위상은 `segments[].tod_phase`로 전달). 이음새는 항상
  **`t1−10m` 지점**이며 `rebase.seam_at`으로 전달한다. 채점 제외 등 처리는
  이상감지 담당(소비자) 몫이다(2026-07-20 합의).
- `meta.json` 필수 필드 — 이 계약을 포함한 `schema_version`은 **`1.3`**:
  - 기존 유지: `t1`, `t2`, `capture_start`, `capture_end`, `dump_started_at`,
    `dump_completed_at`, `scenario_metadata` 6개 필드,
    `scenario_metadata_sha256` (캡처 스크립트는 설명을 생성하지 않고 정본을
    운반, 누락·해시 불일치 시 staging 승격 거부).
  - 신규 `segments[]`: `{role: normal|scenario, ref(정상 세그먼트 경로),
    original_start, original_end, tod_phase}` — 원본 벽시계 범위 보존.
  - 신규 `rebase`: `{policy: shift_normal_forward, delta_sec, seam_at}` —
    연속 시간축 조립 레시피. `t1`/`t2`는 실시각 그대로라 별도 virtual
    필드가 없다.
  - 신규 `normal_provenance`: `{captured_at, loadgen_seed, baseline_rps,
    testbed_commit}` — 정상 세그먼트의 신선도·재현성 판단 근거.
  - 신규 `preflight`: `{window: [t1-10m, t1], verdict: clean|clean_after_wait|
    ai_judged_clean, checked_at, checks[]: {name, value, threshold, pass},
    waited_sec, ai_judgement}` — 선행 10분 창이 정상이었다는 증명. 게이트
    정의(2층 preflight)는 [부하 시나리오 R6](spec-scenario-load.md).
  - 신규 `topology_snapshot`: `{snapshot_at, endpoints[], graph_params}` —
    `data/topology/` API 응답 스냅샷의 수집 시각·요청 파라미터(§4 케이스
    구조의 토폴로지 스냅샷 규칙 참조).
- **구현 전파 대상(아직 구 계약으로 동작)**: controller export 범위
  ([spec-scenario-controller](spec-scenario-controller.md)),
  runbook-scenario-load-execution·runbook-eval-case-restore 절차, 캡처
  스크립트, 정상 세그먼트 일일 덤프 자동화(신규), rca-scenario-runner의
  preflight 게이트(신규 — [부하 시나리오 R6](spec-scenario-load.md)),
  토폴로지 API 스냅샷 수집(신규 — §4).
- **전환은 전면 재시작이다(2026-07-20 결정)**: 신계약 구현이 준비되면 구
  계약 케이스 라이브러리(schema ≤1.2 — L1 수동 캡처 + 자동 캡처분)는
  **전부 제거**하고, 시나리오를 신계약 하에서 처음부터 재실행해 라이브러리를
  새로 쌓는다. 신·구 포맷 혼재를 만들지 않으며, 구 케이스 소급 변환도 하지
  않는다. 종전의 "큐 완주 후 AI 계층 결손 4건만 재실행" 계획은 이 결정으로
  대체된다. 구현이 준비되기 전까지는 실측 캡처를 생성하지 않는다.

케이스는 `meta.json.case_label`로 용도를 구분한다. 허용값은 다음 세 개뿐이다.

| 라벨 | 의미 | 점수용 사용 |
|---|---|---|
| `calibration` | 강도·재현성 조정 중 생성한 캡처 | 아니오 |
| `evaluation` | 정책 검증을 통과한 불변 평가 캡처 | 예 |
| `failed` | 시나리오 실행 실패를 조사하기 위한 진단 캡처 | 아니오 |

라벨은 캡처 완전성 규칙을 완화하지 않는다. 세 라벨 모두 동일한 시간창과 필수
artifact를 가져야 한다. `evaluation_eligible`은 evaluation fixed run 성공,
cleanup·recovery 성공, `dirty=false`, script/catalog SHA-256을 담은 controller
`result.json` 검증, 그리고 `preflight.verdict`가 clean 계열
(`clean`/`clean_after_wait`/`ai_judged_clean`)임을 모두 통과해야 참이다 —
선행 10분 창이 더러운 캡처는 점수용으로 승격되지 않는다. result는 controller-owned trusted runs
root 아래에 있어야 하며 case/scenario/t1/t2/profile과 plan/script/catalog의 실제
SHA-256에 결속된다. 자동 캡처의 기본 라벨은 기존 호출과의
호환을 위해 `calibration`이다.

주입과 수집은 동시가 아니다. ingest router는 기본 5초 배치 flush를 사용하고
네트워크·브로커 큐 지연이 더해질 수 있다. collector 폴링형 신호는 각 수집 주기만큼
늦을 수 있으며, 하류에는 클러스터 닫기 3분·judge 보류 10분·LLM 유예 최대 30분의
벽시계 단계가 있다(§8.2). settle window 20m은 이 지연 합계의 이론 최대(§8.2
기준 최대 ~43m)보다 짧지만 실운영 지연 기준으로 충분하다는 인시던트 담당자
확인(2026-07-20)에 근거한다. 실제 지연은 환경 부하에 따라 달라지므로 각 저장소의
최대 event-time과 dump 시각의 차이를 `meta.json` 또는 캡처 로그에 기록하고,
인시던트가 창 밖에서 생성된 정황이 보이면 settle window를 재협의한다.

**Phase B · 평가 (재생)** — 라이브 없이 무한 반복. **소비자 소유.**

1. 케이스 파일을 받아 자기 방식으로 입력을 구성한다 — 배치형은 격리 DB에
   복원해 조회, 스트리밍형은 얼린 원시를 자기 하니스에 흘리는 식.
2. 대상 기능을 실행하고 결과를 골든 정답과 비교해 채점한다.
3. 격리 자원을 폐기하고 다음 케이스로 반복한다.

대상 버전(에이전트·모델)이 바뀌면 **Phase B만** 케이스 라이브러리에 다시
돌린다. 라이브 테스트베드를 재현할 필요 없이 전체 회귀 평가가 끝난다.

## 3. 파이프라인 저장 위치 (코드 기준)

`../lucida-next/backend` 확인 결과(2026-07-14 재검증), 파이프라인 각 단계의
출력이 모두 durable하게 저장된다. 캡처는 이 지점들을 시간창으로 뜬다.

| 경계 | 저장소 | 테이블/타깃 | 쓰는 곳 (file:line) |
|---|---|---|---|
| 원시 메트릭 | VictoriaMetrics | remote-write 시계열(`target_id`) | `ingest/writer/victoria_metrics.go:87` |
| 원시 트레이스 | ClickHouse | `lucida.otel_traces_local` | `ingest/writer/clickhouse.go:104` |
| 원시 로그 | ClickHouse | `lucida.lucida_logs_local` ⚠ | `ingest/writer/clickhouse_lucida_logs.go:31` |
| 이상감지 이벤트 | ClickHouse | `lucida.lucida_events_local` | `store/events.go:56` (+검출기 7종) |
| 클러스터 | PostgreSQL | `event_clusters` (+CH `cluster_id` 태깅) | `store/clusters.go:82` |
| 인시던트 격상 | PostgreSQL | `incidents`/`incident_members`/`incident_timeline` | `store/incidents_judge.go:515` |
| 토폴로지 스냅샷 | PostgreSQL jsonb | `event_clusters.correlation_meta.topology` → `incidents.topology` | `topology_snapshot.go:22` |
| 토폴로지(라이브)·HostKin | 라이브 쿼리 | PG `targets`(`signalgateway.go:131`) + CH `host_connections`(`rca_signals.go:454` `HostTopPeers`) | 두 파일로 분산 |
| stream-anomaly 모델 artifact | 파일 model store | `/var/lib/lucida/ai-models/stream-anomaly/global/v1/model.json` | `modelstore/modelstore.go:25`, `streamanomaly/runner/model.go:26` |

- ⚠ `lucida_logs_local`은 **ADR-016 이중쓰기의 shadow 경로**(best-effort,
  DPM 필터링)다. 원시 로그의 정본 싱크는 별도(`WriteLogsBatch`)이므로, 로그
  재현 충실도가 중요한 케이스는 정본 싱크를 캡처 대상에 포함해야 한다.
- RCA의 `IncidentSeed`(`rca/model/signals.go:19`)는 읽기 전용이며 PG `incidents`를
  backing store로 쓴다(`signalgateway.go:71`).
- **forecast 워커는 평가 대상이 아니다(2026-07-14 확정).** forecast는 Kafka가
  아닌 VM `query_range` 5분 폴링으로 돌고 자체 durable 상태(PG
  `trainer_forecast_conformal`, champion 라우팅)를 가지나, 평가 범위 제외로
  캡처하지 않는다. 단, **평가 모드에서는 forecast 워커를 미기동**해야 한다 —
  켜두면 복원된 VM을 폴링해 격리 CH에 이벤트를 새로 써서 클러스터·인시던트
  평가에 비결정 노이즈가 흘러든다(§8).

## 4. 케이스 계약 — 파일 구조와 저장 경로

케이스는 DB를 계속 띄워 두는 것이 아니라 **파일**로 저장한다. 이 파일
디렉터리가 캡처 파이프라인과 소비자 사이의 **유일한 인터페이스(계약)** 다.
각 저장소의 native 덤프/복원 포맷을 쓰므로, 복원 방식의 소비자는 **기존
모듈·API 코드 수정 없이** 복원된 DB를 그대로 조회한다. 소비자용 복원 절차는
[평가 케이스 복원 runbook](runbook-eval-case-restore.md) 참조.

- Parquet은 파일 하나 = 테이블 하나이므로 ClickHouse 몫은 단일 파일이 아니라
  `clickhouse/` 디렉터리에 **테이블별 파일(파일명 = 테이블명)** 로 담는다.
- `postgres.dump`는 custom format(`pg_dump -Fc`, 스키마 포함) — 소비자가 빈
  PG에 `pg_restore`만으로 복원 가능해야 한다.
- stream-anomaly 모델 artifact는 `capture_end`(=`t2+20m`) 대기가 끝나자마자 느린 저장소 export보다
  먼저 복사한다. 원본 JSON과 SHA-256을 함께 보존하며 checksum 파일의 대상명은 케이스
  내부 상대 파일명 `model.json`으로 기록한다.

### 저장 경로

케이스 라이브러리의 정본 위치는 AI 개발 서버의 공유 데이터 볼륨이다:

```
/data/eval-cases/                  # 케이스 라이브러리 루트
  case-01-inventory-lock/
  case-02-.../
  ...
```

- `/data`는 3.5T 공유 볼륨(sudo 그룹 setgid)로, 기존 평가 데이터셋
  (`bank_rca_eval_2025`, `chatai-eval-dataset`)과 같은 관례를 따른다.
- 케이스 디렉터리는 캡처 완료 후 **불변**으로 취급한다(원본 수정 금지,
  소비자는 읽기만). 수정이 필요하면 새 케이스로 파생한다.

### 케이스 디렉터리 구조

```
case-01-inventory-lock/
  data/                     # 세계 스냅샷 (모든 소비자 공유)
    victoriametrics.export  # 메트릭 (VM JSON lines export)
    clickhouse/             # 테이블별 Parquet — 파일명 = 테이블명
      otel_traces_local.parquet
      lucida_logs_local.parquet
      lucida_events_local.parquet
      host_connections.parquet
    postgres.dump           # 클러스터·인시던트·targets·정책·토폴로지 (pg_dump -Fc)
    topology/               # 토폴로지 API 응답 스냅샷 (2026-07-20 추가)
      topology-graph.json                # GET /api/v1/topology/graph 응답
      asset-tree-service-unified.json    # GET /api/v1/asset-tree/service/unified 응답
  assembled/                # 연속 시간축 완성본 (2026-07-20 추가) — 소비자 기본 입력
    victoriametrics.export  # shift된 정상 prefix + 실시각 시나리오 창 병합본
    clickhouse/             # 동일 병합본 (테이블별 Parquet)
  models/
    stream-anomaly/global/v1/
      model.json            # capture_end(t2+20m) 시점의 모델 artifact
      model.json.sha256     # 원본 무결성 검증값
  golden.rca.json           # RCA 정답(근본원인) — RCA 담당 소유
  meta.json                 # 시나리오 설명·t1·t2·캡처 시간창·덤프/모델 스냅샷 시각 등
  scenario.md               # 시나리오 동반 문서(사람 판독용) — 개요·주입 방법·
                            # 실행 기록·관측 결과·재실행 캘리브레이션 주의·정본 포인터
```

케이스 부속 규칙(2026-07-15, 첫 캡처에서 확정):

- **시간대**: meta·골든의 시간 필드 **정본은 UTC(Z)** — 케이스 내 CH/VM
  데이터가 전부 UTC라 기계 대조에 변환이 없어야 한다. 사람 판독용으로
  `*_kst` 병기 필드를 둔다(예: `incident_onset` + `incident_onset_kst`).
- **assembled 조립본 (2026-07-20 추가)**: 캡처 파이프라인이 정상 prefix
  사본(VM export + CH parquet)의 시각에 `rebase.delta_sec`을 더해 시나리오
  창과 병합한 연속 시간축 완성본. **소비자의 기본 입력은 `assembled/`**이고,
  `data/`·`normal-segments/` 원본은 실벽시계 역추적·검증용이다. PG dump·모델
  artifact·토폴로지 스냅샷은 시나리오 실시각 기준이라 assembled 대상이
  아니다(§2.1 조립 방향 참조). 한계: shift된 prefix의 로그 **본문 텍스트**
  속 시각 문자열은 재작성하지 않는다 — event-time 칼럼만 이동하므로 사람
  판독 시 어긋나 보일 수 있다(기계 채점은 칼럼 기준이라 무해). 용량은
  케이스당 약 2배가 되며 3.5T 볼륨 기준 허용.
- **토폴로지 스냅샷 (2026-07-20 추가, 소비자 요구)**: query 서비스
  (119:18080, 로그인 세션 필요 — ai-coverages 등록과 동일 절차)의 두
  엔드포인트 응답을 **API 응답 JSON 그대로** `data/topology/`에 저장한다.
  ①`GET /api/v1/topology/graph` — 자동탐색 cross-domain 그래프
  `{nodes[], edges[], sources[]}`, `range`는 시나리오 창 `[t1-10m, t2+20m]`
  기준. ②`GET /api/v1/asset-tree/service/unified` — 운영자 관리 서비스 트리.
  스냅샷 시점은 모델 artifact와 동일하게 `capture_end` 이후이며, 사용한
  요청 파라미터 전체와 스냅샷 시각을 `meta.json.topology_snapshot`에
  기록한다. PG dump에도 토폴로지 원천이 들어 있지만, 소비자가 dump 복원
  없이 RCA 전파 채점·토폴로지 컨텍스트 입력으로 바로 쓰도록 API 응답
  형식으로 동봉하는 것이 목적이다.
- **모델 스냅샷**: 원본 경로는
  `/var/lib/lucida/ai-models/stream-anomaly/global/v1/model.json`, 케이스 경로는
  `models/stream-anomaly/global/v1/model.json`으로 고정한다. `capture_end` 이후 복사하고
  JSON 파싱·비어 있지 않음·SHA-256 일치를 검증한다. `meta.json`에
  `model_snapshot_at`, `model_source_path`, `model_sha256`을 기록한다.
- **캡처 정책 검증**: 자동 캡처는 `model_snapshot_at >= capture_end`를 검사하고,
  checksum을 다시 검증한 뒤에만 staging 디렉터리를 승격한다. 케이스 staging에
  `golden.anomaly.json`이 존재하면 승격을 거부한다.
- **골든 propagation 구조**: 산문 나열이 아니라 단계별 구조체
  `{step, description, targets:[{target_id, name}]}` — 전파 사슬의 각 단계도
  cause.entity와 같은 정본 target_id 체계로 적어야 "어느 단계까지 제대로
  짚었나"를 기계 대조할 수 있다.
- **라이브러리 루트**: `/data/eval-cases/` 루트에 복원 runbook 사본
  (`runbook-eval-case-restore.md`)을 둔다 — 정본은 본 저장소 docs이며 사본
  갱신은 케이스 추가 시점에 확인.

**데이터는 공유, 골든만 따로.** 데이터(`data/`)와 모델 스냅샷(`models/`)은
한 벌을 모든 소비자가 공유한다. 소비자별로 다른 것은 데이터가 아니라
"문제+정답지"(골든)이며, 각 평가 담당자가 소유한다. **재생 방법도 마찬가지로
소비자 소유다**(§1 책임 경계).

케이스 내 별도 골든 파일은 `golden.rca.json`만 둔다. 이상감지 기대값은 시나리오
YAML의 `expected_anomalies`를 정본으로 사용하며 **`golden.anomaly.json`은 만들지
않는다**. 실제 이상감지 출력은 `data/clickhouse/lucida_events_local.parquet`에
포함된다. **챗봇은 이 평가·캡처 설계의 적용 범위에서 제외한다**(2026-07-15 범위
정정).

## 5. 두 종류의 데이터 — 슬라이스 vs 전체

"시간창 안의 것만 저장"은 대체로 맞지만 한 가지가 어긋난다.

| 종류 | 예 | 뜨는 방식 |
|---|---|---|
| 시간 데이터 | 메트릭·트레이스·로그·이벤트·알람·인시던트 | 시나리오 창 `[t1-10m,t2+20m]`로 슬라이스 + 일일 정상 세그먼트(00:00~02:00) 참조 |
| 기준/인벤토리 데이터 | `targets`(자원 대장)·토폴로지·알람정책·서비스 카탈로그 | 시간창과 무관하게 그 시점 전체 스냅샷 |
| 모델 artifact | stream-anomaly global v1 `model.json` | `capture_end` 이후 파일+SHA-256 스냅샷 |

인벤토리는 시간축이 없다. RCA가 사건 당시 대상과 토폴로지를 재구성하려면
시간창 안의 시계열만으로는 부족하다. 라이브로 계산되는 토폴로지·HostKin도
결국 PG `targets` + CH `host_connections`를 읽으므로(§3), 이 참조 테이블은
반드시 전체를 뜬다.

## 6. 캡처 두 종류와 소비자

소비자는 여럿이고(RCA·이상감지·클러스터·인시던트), 필요한 창의 성격이
다르다. 구분 축은 **"장애 유무"가 아니라 "창 길이·목적"** 이다. 장애는 양쪽 다
포함될 수 있다.

| 캡처 종류 | 길이 | 내용 | 주 소비자 |
|---|---|---|---|
| 짧은 집중 캡처 | 짧음(정상 리드인 + 장애 + 회복) | 장애 하나, 깨끗한 라벨 | RCA·이상감지·클러스터·인시던트 |
| 긴 운영 캡처 | 긺(수일~수주) | 정상 + 장애·노이즈 섞임 | 이상감지 장기 기준선, 클러스터·인시던트 현실 노이즈, RCA 유사 사례 corpus 구성 |

- **짧은 집중 캡처의 리드인은 2시간으로 고정**하고 `meta`에 명시한다. 긴 과거는
  긴 운영 캡처가 담당하며, 모든 짧은 케이스에 수일치 리드인을 붙이지 않는다.
- **재활용.** 긴 운영 타임라인을 한 번 캡처하고, 그 안의 각 장애 구간을 잘라
  짧은 컴포넌트 케이스로 파생할 수 있다. 종결된 과거 인시던트는 고정 KEDB
  corpus 후보로도 사용한다.
- **트레이드오프.** 컴포넌트 평가(RCA 등)는 장애 하나·노이즈 적은 깨끗한
  조건에서 귀속이 명확하다. 긴 운영 캡처는 노이즈가 섞여 귀속이 어려워질 수
  있으므로, 보통 **깨끗한 짧은 케이스와 현실적인 긴 캡처를 둘 다** 둔다.

### 모듈별 입력·채점 컨셉

| 모듈 | 입력 (구성은 소비자 몫) | 출력 | 채점 컨셉 |
|---|---|---|---|
| 이상감지 | 원시 스트림 | 이상 이벤트 | 정답 이상과 비교 — 제때 맞게 잡았나 + 오탐 안 냈나 |
| 클러스터 | 이벤트 집합 | 이벤트 그룹 | 같은 사건 이벤트를 하나로 잘 묶었나 |
| 인시던트 격상 | 클러스터·이벤트 | 인시던트(시각·범위·심각도) | 제때 격상 / 과·미격상 여부 |
| RCA | 인시던트+토폴로지+원시 | 근본원인 | 원인 자원·도메인을 맞췄나 |

**격리 vs 종단.** 각 단계에 앞 경계의 *골든* 입력을 주면 그 단계만 격리 채점해
귀속이 명확하다. 앞 단계의 *실제* 출력을 주면 종단(누적) 성능을 본다. 케이스
데이터는 두 방식 모두를 지원한다(각 경계의 실제 출력이 §3대로 전부 캡처되므로).

## 7. 유사 장애 사례

RCA 유사 장애 기능을 고려한 사례군의 의미 관계와 시간 순서는
[시나리오 설계 §9.1](spec-scenario-design.md)에서 정의한다. 검색·활용 지표와
평가 하네스 계약은 시나리오 포트폴리오가 확정된 뒤 별도로 설계한다.

## 8. 소비자 참고 — 재생 시 알려진 함정

재생 방법은 소비자 소유(§1)이나, 케이스를 설계·검증하며 확인한 함정을
기록해 둔다(2026-07-14, `../lucida-next/backend` 코드 재검증 기준). 자기
하니스를 설계할 때 참고하라.

### 8.1 스트리밍 이상감지

- 감지기는 Kafka `lucida.metrics`를 소비한다(`streamanomaly/runner/main.go:77,342,469`).
  단, **평가에 Kafka 재발행이 필수는 아니다** — 감지 판정의 핵심(기준선·밴드·
  z-score·계절 버킷·에피소드 시작)은 데이터 타임스탬프(event-time) 기준이라
  (`detector.go:~900,~966,~1082`), 얼린 원시를 시간순으로 감지기에 직접 먹이는
  **인프로세스 하니스**가 더 단순하고 재현성도 높다.
- **벽시계 예외:** 에피소드 **종료(close)** 는 30초 벽시계 스윕이 KeepFiring
  8분을 실제 시각으로 센다(`main.go:409` → `runtime.go:449`). 가상 시계 훅
  `WithClock`(`detector.go:303`)이 있으나 **러너에는 미연결**(테스트에서만 사용,
  `evict_test.go:32`). 인프로세스 하니스는 이 훅을 데이터 시각으로 연결하면
  종료까지 완전 재현된다.
- **트레이너·시드 고정:** 감지 결과는 데이터만으로 정해지지 않는다. `ai-trainer`가
  보정값 4종을 PG `trainer_stream_*` 4개 테이블에 쓰고 감지기가 reload한다.
  고정 방법 — `STREAM_ANOMALY_TRAINER_RELOAD_SEC=0` (`main.go:534`),
  `STREAM_ANOMALY_MODEL_RELOAD_SEC=0` (`model.go:141`), `ai-trainer` 미기동,
  시드/백필 off(`STREAM_ANOMALY_SEED_FROM_VM`, `_SEASONAL_BACKFILL`, `_SPIKE_SEED` —
  기본값 모두 true 주의).
- **coverage/정책 60초 재읽기:** `coverageRefreshLoop`(60초, env로 못 끔)가
  coverage뿐 아니라 **detection-policy 파라미터도 매번 재읽는다**(`main.go:508,521`).
  재생 중 정책 데이터가 불변이어야 한다.
- **train/test 분리.** 모델은 해당 케이스의 장애 데이터로 학습되면 안 된다(누수).
  골든 이상 라벨은 감지기 출력이 아니라 시나리오에서 나온 진짜 정답이어야
  한다(순환논리 방지).
- **콜드스타트 vs 웜스타트:** 콜드스타트(시드·reload·트레이너 전부 off, 빈
  상태에서 리드인으로 평상시 학습)가 기본이며 완전 재현된다. 계절성(요일/시간대
  장기 패턴)에 의존하는 시나리오만 모델·상태 스냅샷을 케이스에 함께 얼려
  웜스타트한다 — 콜드스타트는 21일치 계절 패턴을 만들지 못한다.

### 8.2 클러스터·인시던트를 실워커로 돌릴 때

클러스터·인시던트 채점은 캡처된 실제 출력을 조회(배치형)하는 것이 기본이다.
실워커를 재생 입력 위에서 돌리려는 소비자는 다음을 알아야 한다.

- **체인은 자체 연쇄한다:** 이벤트 CH 쓰기 → `event.saved` 발행(`store/events.go:119`,
  `event_saved.go:45`) → 클러스터 워커 소비·즉시 클러스터링(`event_saved_consumer.go`)
  → 인시던트 judge가 30초 PG 폴링으로 닫힌 클러스터 심사(`judge_runner.go:101`).
  Kafka 없이도 클러스터 워커는 30초 CH 폴링 폴백으로 동작한다(`runner.go:112`).
- **벽시계 가드가 과거 타임스탬프 재생을 막는다:** 소비자 신선도 가드
  (`EVENTCLUSTER_EVENT_SAVED_MAX_LAG_MIN`)가 오래된 이벤트를 버리고, 클러스터
  닫기(3분 무활동)·judge 보류(10분)·LLM 유예(30분)가 전부 `time.Now()` 기준이다.
  과거 시각 그대로는 재생 불가 — 타임스탬프를 현재로 평행이동(타임시프트)하고
  1배속 실시간 재생해야 하며, VM·CH·PG 세 저장소에 일관 적용해야 한다(judge가
  심사 중 VM을 재조회함, `judge_repo.go:226`).
- **인시던트 judge는 LLM을 포함한다**(`judge_repo.go`, `cfg.AIModel`) — 입력을
  고정해도 출력이 흔들릴 수 있다. 완전 재현이 필요하면 룰 기반 폴백 모드
  사용 또는 의미 등가 채점을 병행한다.
- **다른 Kafka 소비자:** `lucida.logs`(loganomaly), `lucida.traces`(traceanomaly),
  `lucida.changes`(changedetect), `lucida.alerts`(alarmbridge)도 각자 토픽을
  소비한다. metrics만 재발행하면 이들은 침묵한다.

### 8.3 공통

- **forecast 워커 미기동**(§3) — 켜두면 복원된 VM을 폴링해 격리 CH에 이벤트를
  써서 하류 평가에 노이즈가 된다.
- 격리 DB는 일회용이다 — 워커가 되쓰기해도 원본 케이스 파일은 불변(§4).
- **ClickHouse 24.3의 Parquet 출력은 UUID 컬럼 미지원** — `lucida_events_local`의
  `event_id`/`episode_id`는 캡처 시 `toString()` 캐스팅으로 뜬다(첫 캡처에서
  실측). 복원/조회 시 이 두 컬럼이 String임을 감안할 것.
- 원격 `pg_dump`는 AI 개발 서버의 lucida-postgres 컨테이너로 가능:
  `docker exec -e PGPASSWORD=... lucida-postgres pg_dump -Fc -h <AP> -p 15432 -U lucida -d lucida`.

## 9. 미결 / 다음 단계

- ~~실제 export 메커니즘~~ → **수동 절차로 실증 완료(2026-07-15, 첫 케이스
  `case-l1-blackfriday-surge`)**: VM `/api/v1/export`(전 시리즈 시간창), CH
  HTTP `FORMAT Parquet`(테이블별 시간창 슬라이스 + host_connections 전체),
  원격 `pg_dump -Fc`(lucida 전체). **자동화 초안도 구현 완료** —
  `scripts/capture-eval-case.sh`가 시간 가드·3개 저장소 덤프·모델 스냅샷·checksum·
  meta·원자적 승격을 수행한다. `--dry-run`과 저장소별 읽기 전용 연결/쿼리 검증은
  통과했으며, 실제 케이스 end-to-end 캡처 검증과 로그 정본 싱크(§3 ⚠) 포함 여부는
  남아 있다.
- **케이스 계약 확정.** `meta.json` 표준 필드(t1·t2·시간창·덤프/모델 스냅샷 시각·시나리오
  ID·캡처 종류), 파일 포맷 버전 표기. 첫 케이스의 meta.json이 사실상의 원형 —
  스키마로 고정하는 작업이 남음.
- ~~`/data/eval-cases/` 부트스트랩~~ → **완료(2026-07-15)** — 루트 생성, 복원
  runbook 사본 배치, 첫 케이스 수록. 명명은 `case-<slug>`로 시작했고
  `case-NN-slug` 번호 규칙 채택 여부는 케이스가 늘 때 결정.
- **골든 스키마.** 케이스 내장 `golden.rca.json` 필드 정의(§6)와
  [평가·실험 설계](spec-evaluation.md)의 골든 레코드와의 정합. 이상감지 기대값은
  시나리오 YAML `expected_anomalies`를 사용한다.
- **복원 헬퍼(선택).** 격리 DB 세트 기동·복원·폐기 자동화(docker 등)는 소비자
  몫이나, 공용 스크립트를 한 벌 제공하면 중복을 줄인다.
