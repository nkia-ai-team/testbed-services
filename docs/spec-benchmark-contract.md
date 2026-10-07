---
title: 벤치마크 계약 — 정답 분리와 채점 결속
status: Draft
owner: project
last_reviewed: 2026-08-14
tags:
  - evaluation
  - benchmark
  - data
  - contract
summary: 케이스를 벤치마크로 성립시키기 위한 두 계약 — (A) 정답과 입력의 물리적 분리, (B) 정답 레코드를 실제 RCA 산출물 계약에 결속한 채점 규칙. spec-evaluation.md §3·§4가 정의한 축을 캡처 산출물에 실제로 연결한다.
---

# 벤치마크 계약 — 정답 분리와 채점 결속

## 0. 왜 이 문서가 필요한가 — 빠진 이음새

[평가·실험 설계](spec-evaluation.md) §4가 골든 레코드 스키마를
(`has_single_cause`·`cause.entity.target_id`·`accept_if` …) **이미 정의했다.**
문제는 설계가 아니라 **결속**이다.

2026-08-14 실측:

| 확인 | 결과 |
| --- | --- |
| 골든 스키마가 채워진 케이스 | **0 / 25** |
| 골든 스키마가 채워진 레지스트리 항목 | **0 / 52** |
| 골든 스키마의 실제 인스턴스 | `scripts/scenarios/.fragments/F15-G.md` **1건**(그것도 산문) |
| 케이스가 실제로 싣는 정답 | `meta.json`의 `scenario_metadata` **한국어 산문 6필드** |

이음새는 조각 문서에 명시적으로 적혀 있다 — `F15-G.md` §5:

> `golden` 레코드: `has_single_cause:false`, `expected_status: insufficient` …
> **golden 파일 작성은 evaluation-capture 단계 몫**(이 fragment는 시나리오
> 정적 상세화까지).

시나리오 설계는 골든 작성을 캡처 단계로 넘겼고, **캡처 단계는 그것을 구현하지
않았다.** 그 결과 산출물에는 산문만 남았고, 예측은 구조화 JSON이라 둘을
기계로 대조할 수 없다. 35종을 지금 상태로 캡처하면 **채점 불가능한 케이스
35개**가 나온다.

이 문서는 그 이음새를 계약으로 메운다. 두 가지다.

- **A. 정답과 입력의 분리** — 지금은 같은 파일(`meta.json`)에 섞여 있다.
- **B. 정답 레코드를 예측 계약에 결속** — 채점 가능한 어휘로 정답을 쓴다.

---

## 1. 원칙

1. **정답은 예측과 같은 어휘로 쓴다.** 산문으로 쓰면 LLM 채점관에게 전부
   떠넘기게 되고, 재현성이 판정관 변덕에 종속된다. 기계로 맞출 수 있는 것은
   기계로 맞춘다.
2. **정답은 입력과 물리적으로 분리한다.** "하네스가 안 읽겠지"는 계약이 아니다.
   소비자에게 주는 묶음에 정답이 물리적으로 없어야 한다.
3. **LLM 채점은 좁게 가둔다.** 결정론으로 못 재는 부분(작동 기전 서술)만
   맡기고, 판정 근거는 `accept_if`/`reject_if`로 사전에 고정한다
   ([spec-evaluation §7](spec-evaluation.md) 규율 준수).
4. **정답은 시나리오 설계 의도에 고정한다.** agent 출력에 맞춰 조정하지 않는다
   (양방향 과적합 금지 — spec-evaluation §1).

---

## 2. 계약 A — 케이스 레이아웃 v3.1 (schema 3.0)

### 2.1 현재 무엇이 새는가

`meta.json` 하나에 아래가 **함께** 들어 있다.

| 필드 | 성격 |
| --- | --- |
| `t1`·`t2`·`capture_start`·`capture_end`·`time_basis` | **입력** — 소비자가 창을 잡으려면 필요 |
| `model_sha256`·`postgres_tables[]`·`topology_bundle` | **입력** — 복원 계약 |
| `scenario_metadata`(title·description·cause·injection_summary·user_impact·distinguishing_evidence) | **정답** |
| `scenario_id`(`F03-G`) · `case_id`(`case-f03-g-v3-…`) | **정답 누설** |

마지막 줄이 특히 나쁘다. **디렉터리 이름 자체가 정답이다.**
`case-f03-g-v3-3ec8892e`에서 `-g-`는 음성(흡수) 시나리오를 뜻한다. 파일을 하나도
안 열어도 "이건 장애가 아니다"를 알 수 있다. `scenario.md`는 아예 정답 해설서다.

### 2.2 새 레이아웃

```
case-<opaque-id>/
  MANIFEST.json          # 규격 버전 + 두 묶음의 sha256 + 데이터 평면 식별
  input/                 # 소비자에게 주는 전부
    meta.json            # 시간창·복원 계약만. 정답 필드 없음
    data/
      victoriametrics.export
      clickhouse/*.parquet
      postgres.dump  postgres/*.csv  restore.sql
    models/stream-anomaly/global/v1/{model.json,model.json.sha256}
    topology/
  answer/                # 하네스만 연다. 소비자 배포본에서 제외
    golden.json          # §3 기계 채점용 정답 레코드
    scenario.md          # 사람 판독용 서술(현행 문서를 여기로)
    causality-audit.json # ITS 인과 감사 결과(정답 타당성 근거)
```

**`<opaque-id>`는 시나리오를 유추할 수 없어야 한다.** 캡처 시 난수로 배정하고
`answer/golden.json`에만 `scenario_id`를 적는다.

### 2.3 `input/meta.json`에서 제거되는 것

- `scenario_id`, `scenario_metadata`, `scenario_metadata_sha256`
- `case_label`, `evaluation_eligible` (운영 라벨이지 소비자 입력이 아니다)
- `run_*_sha256` 계열 — 주입 스크립트 해시는 주입 방식의 지문이다

남는 것은 **창·평면·복원 계약**뿐이다.

### 2.4 파생 누설도 같이 막는다

레이아웃만 갈라도 데이터 안에 정답이 남는다. 아래는 **프라이버시가 아니라
정답 누설**이므로 스크러빙 백로그가 아니라 **캡처 게이트**로 올린다.

**2026-08-14 실측** (v3 케이스 2건 전수 스캔 — F03-G(순수 부하) · F01-R(PG 잠금),
ClickHouse 12표 약 250만 행):

| 경로 | 무엇이 새는가 | 실측 |
| --- | --- | --- |
| DPM `dpm_session_local.body` 의 `program` | `rca-F01-R-inventory-lock` — **시나리오 id + 기전**이 통째로 | ✅ **확정** F01-R 8행 |
| `process_meta.cmdline` | `/var/lib/lucida/scenario-profile-state/F09-R.pgid` — 주입 기구 경로 + **다른 시나리오의 id** | ✅ **확정** 두 케이스 모두 6행 |
| PG `incidents.title`(창 밖) | AI 생성 한국어 제목에 근본원인이 적혀 있다 | ✅ 확정([[capture-pg-scope-leak-0813]], pg-scope로 수리됨) |
| 부하 태그 `scenario_tag` | 트래픽에 시나리오 id | ❌ **관측 데이터에서 미발견** — k6 인자에만 있고 스팬·로그로 전파되지 않는다 |
| 케이스 자신의 id (`F03-G`) | 관측 데이터 내 등장 | ❌ 미발견 (잠금 태그 경로 제외) |

첫 줄이 가장 나쁘다. **RCA가 "점유 세션"을 찾을 때 보는 바로 그 데이터**에
정답이 적혀 있다. `program` 하나만 읽으면 시나리오도 기전도 끝난다.

둘째 줄은 **교차 누설**이다 — F03-G 케이스와 F01-R 케이스 양쪽에서 무관한
`F09-R`이 나왔다. 케이스별 사고가 아니라 구조적이다.

→ **캡처 게이트**: `input/` 전체에 대해 시나리오 id 패턴(`F\d\d-[A-Z]\d?`)과
주입 태그(`rca-F\d\d-`)·주입 기구 경로(`scenario-profile-state`·`scenario-runs`)를
전수 검색해 **0건**이어야 케이스가 유효하다. 오탐 주의: `rca-testbed-*`는 k8s
네임스페이스(정상 인프라 명명)이므로 `rca-` 단독 패턴은 쓰지 않는다.

→ **주입 계약 수정이 선행돼야 한다.** 게이트만 걸면 잠금 계열 시나리오가 전부
불합격한다. `application_name`/`client_identifier`를 시나리오와 무관한 중립
문자열로 바꾸되(예: 임의 세션 토큰), **러너는 그 토큰으로 태그를 관측해야 하므로**
토큰↔시나리오 대응은 `answer/`에만 남긴다. 스캐너는
`scripts/scenarios/leakscan.py`로 넣는다.

---

## 3. 계약 B — 골든 레코드를 예측 계약에 결속

### 3.1 예측 계약(실측)

평가 대상의 산출물은 `incident_rca.report_json`이고 구조는 확정돼 있다
(`lucida-next/backend/services/ai/features/operator/rca/model/report.go` ↔
`frontend/web/src/components/ai/rca/rcaReportTypes.ts`, 1:1).

```
ReportJson
  diagnosis.verdict        CONCLUSIVE | INCONCLUSIVE | CONTRADICTED
  diagnosis.confidence     0.0~1.0
  hypotheses[]             rank, is_adopted, title, description,
                           grounds[], culprits[{label,value,detail}],
                           evaluation.verdict  PROVEN|WEAKENED|CONTRADICTED
    propagation.nodes[]    level  service|app|db|host
                           status root_cause|affected
                           entity  ← target_id (Polestar10 등록 자산 id)
  conclusion_summary, next_steps[], similar_cases[]
```

핵심은 `PropagationNode.entity`가 **`target_id`로 채워진다**는 점이다
(`service/golden.go`에서 `Entity: st.TargetID`). 정답도 같은 식별자를 쓰면
**지목 정확도는 문자열 일치로 결정론 채점된다.**

### 3.2 매핑 — 골든 필드 ↔ 예측 경로

| 골든 필드 (spec-evaluation §4) | 예측 경로 | 채점 |
| --- | --- | --- |
| `has_single_cause` | `diagnosis.verdict` | 결정론 |
| `expected_status` | `diagnosis.verdict` (+`confidence`) | 결정론 |
| `cause.entity.target_id` | `hypotheses[is_adopted].propagation.nodes[status=root_cause].entity` | **결정론(정확 일치)** |
| `cause.entity.dimension` | 같은 가설의 `culprits[].label`/`value` | 결정론(정규화 후) |
| `cause.domain` | root_cause 노드의 `level` + 자산 도메인 | 결정론 |
| `must_support` | `hypotheses[is_adopted].grounds[]` | LLM 판정(좁게) |
| `must_rule_out` | `evaluation.ruled_out[]` | LLM 판정(좁게) |
| `cause.mechanism` + `accept_if`/`reject_if` | `title`+`description` | LLM 판정 |
| `propagation[]` | `propagation.nodes[]` 순서 | LLM 판정(보조) |

### 3.3 `target_id`는 평면 의존이다 — 자연키를 같이 박는다

k3s 이관으로 자산이 재등록되며 `targets`가 1,442행 → 1,581행으로 바뀌었다.
**옛 평면에서 적어 둔 `target_id`는 새 평면에서 안 맞는다.**

→ 골든은 둘 다 싣는다.

```json
"entity": {
  "target_id": "018f...-db-0001",
  "natural_key": {"kind": "k8s_deployment",
                  "namespace": "rca-testbed-commerce",
                  "name": "inventory-service"},
  "resolved_on_plane": "k3s-20260813",
  "resolved_at": "2026-08-20T04:11:02Z"
}
```

채점은 `target_id` 우선, 불일치 시 자연키로 재해석한다. 평면이 또 바뀌어도
정답이 살아남는다.

---

## 4. 채점 — 두 층

### 4.1 Tier 1 · 결정론 (재현율 100%)

| 지표 | 정의 |
| --- | --- |
| **T1 탐지** | 주입 구간에 인시던트가 격상됐는가. 음성 케이스는 **격상 없음이 정답** |
| **T2 지목** | 채택 가설의 root_cause `entity` == 골든 `target_id` (자연키 폴백) |
| **T3 깊이** | `expected_depth=dimension`이면 `culprits`의 차원까지 일치 |
| **T4 단정** | `diagnosis.verdict`가 `expected_status`와 정합. `has_single_cause=false`인데 `CONCLUSIVE`면 **과확신 감점**(hard gate, spec-evaluation §3.4) |

T2는 **부분점수를 주지 않는다.** "같은 호스트의 다른 서비스"는 오답이다.
단, `level`만 맞은 경우를 **별도 열**로 보고해 실패 양상을 보이게 한다.

### 4.2 Tier 2 · LLM 판정 (좁게)

- 입력: 채택 가설의 `title`·`description`·`grounds[]`, 골든의 `cause.mechanism`·
  `accept_if`·`reject_if`·`must_support`·`must_rule_out`. **그 밖의 정보는 주지 않는다.**
- 판정관 **2인 독립** → 불일치 시 3인째로 다수결. **일치도(κ)를 항상 보고한다.**
  κ가 낮으면 그 지표는 그 회차에서 무효로 처리한다.
- 판정관에게 케이스 id·시나리오 id·`scenario.md`를 **주지 않는다**(정답 해설서를
  보면 채점이 아니라 대조가 된다).

### 4.3 집계

- 케이스별 T1~T4 + Tier 2를 각각 보고하고 **단일 총점으로 합치지 않는다.**
  합치면 탐지 실패와 지목 실패가 섞여 진단력을 잃는다.
- 35종 기준 **점수 해상도는 약 2.9%p**다. 모든 비율은 **Wilson 95% 신뢰구간과
  함께** 보고한다. 구간이 겹치는 두 수치를 "개선"이라 부르지 않는다.
- `cause.domain`·`difficulty`로 층화 보고한다(spec-evaluation §3.6·§5).

---

## 5. 평가 모드 두 갈래 — 음성 케이스의 제약

`rca-runonce <incident_id>`는 **인시던트가 이미 있어야** 돈다
(`cmd/rca-runonce/main.go`). 여기서 갈린다.

| 모드 | 입력 | 잴 수 있는 것 |
| --- | --- | --- |
| **격리(RCA-only)** | 케이스에 복원된 `incident_id` | T2·T3·T4, Tier 2 |
| **종단** | 원시부터 탐지·클러스터·격상 재생 | T1 포함 전부 |

**음성 4종(F01-G·F03-G·F05-G·F11-G)은 격리 모드에서 평가할 수 없다.**
인시던트가 없으므로 실행할 대상이 없다. 이들은 "격상하지 않았는가"를 묻는
케이스이므로 **종단 모드 전용**이다.

→ 결과 보고에 **모드를 반드시 병기**한다. 격리 모드 점수를 "35종 성적"이라
부르면 음성 4종이 조용히 빠진 31종 성적이 된다.

---

## 6. 베이스라인 (필수)

베이스라인 없는 점수는 해석 불가다. 최소 셋을 같은 하네스로 돌려 함께 보고한다.

| 베이스라인 | 정의 | 무엇을 드러내나 |
| --- | --- | --- |
| **최빈 상수** | 항상 가장 흔한 정답 target을 찍는다 | 정답 편중이 만드는 하한 |
| **최다 알람** | 그 창에서 알람이 가장 많이 뜬 target | "제일 시끄러운 놈" 휴리스틱의 강도 |
| **진입 서비스 고정** | 항상 `commerce-order` 등 진입점 | 증상↔원인 혼동의 하한 |

모델 점수가 이 셋을 **신뢰구간 밖으로** 못 넘으면 그 지표는 아직 측정력이 없다.

---

## 7. 캡처 단계가 해야 할 일 — 빠진 구현

`capture-eval-case.sh`에 아래를 추가한다. 이것이 §0의 이음새다.

1. **`answer/golden.json` 생성.** 레지스트리 `controllers.json`의 확정 level
   파라미터에서 주입 대상을 끌어온다 — 우리가 심었으므로 발굴 노동이 없다
   (예: `schema=inventory_schema`, `table=inventory`, `key_value=1`).
2. **`target_id` 해석.** 캡처 시점 `targets` 테이블에서 자연키 → `target_id`를
   조회해 박고, 자연키와 평면 식별자를 함께 남긴다(§3.3).
3. **인과 감사 첨부.** `audit-run-causality.py`의 ITS 판정을
   `answer/causality-audit.json`에 싣는다. 정답이 실제로 그 주입 때문이라는
   근거가 케이스 안에 있어야 한다.
4. **누설 게이트.** §2.4 전수 검색이 0건이어야 한다.
5. **무효 처리.** 위 넷 중 하나라도 실패하면 케이스를 `evaluation_eligible=false`로
   내리고 캡처를 실패로 기록한다. **조용히 통과시키지 않는다.**

케이스 디렉터리는 불변이다. 캡처·검증 도구가 케이스 안에 상태 파일을 쓰지
않도록 하고(2026-08-14에 실제로 오염 1건 발생·정리), 배포본은 읽기 전용으로
마운트한다.

---

## 8. 미결 — 결정이 필요하다

| # | 항목 | 왜 지금인가 |
| --- | --- | --- |
| 1 | 정답 라벨 영문 병기 | 캡처 후에 하면 `scenario_metadata_sha256` 35개가 깨진다 |
| 2 | `expected_depth`를 종별로 확정 | `entity`까지만 요구할지 `dimension`까지 요구할지가 난이도를 바꾼다 |
| 3 | 음성 비율 | 현재 4/35(11%). 오탐 저항을 재려면 더 필요할 수 있다 |
| 4 | 기전 중복쌍 | F04-H/F18-P(둘 다 outbox relay 정지), F02-H/F10-H/F10-P(둘 다 PVC fio) — 실효 표본이 35보다 작다. 층화 보고로 드러낼지, 종을 갈지 |
| 5 | 배포본에서 `answer/` 제외 방식 | 별도 tarball인지, 해시만 공개하는지 |

---

## 관련 문서

- [평가·실험 설계](spec-evaluation.md) — 채점 축·골든 스키마 정본(§3·§4·§7·§8)
- [평가용 데이터 캡처·재생 설계](spec-eval-data-capture.md) — 캡처 계약(§4 케이스 구조, §5 스코프)
- [평가 케이스 복원 runbook](runbook-eval-case-restore.md) — 소비자 절차
- [시나리오 품질 헌장](spec-scenario-quality-charter.md)
