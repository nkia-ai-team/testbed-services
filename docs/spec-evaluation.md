---
title: 평가·실험 설계
status: Draft
owner: project
last_reviewed: 2026-07-08
tags:
  - evaluation
  - experiment
  - rca
  - research
summary: RCA agent의 평가 축을 정량 지표·채점식으로 구체화하고, 정밀 정답(golden) 레코드 스키마, 채점 방식(하이브리드), cause-domain 층화, baseline, ablation, 실험 절차를 정의한다.
---

# 평가·실험 설계

이 문서는 [프로젝트 목적](project-purpose.md)이 정의한 다섯 개 평가 축을
재현 가능한 실험으로 만든다. 목적 문서는 "무엇을 평가하는가"(축)를 고정했고,
이 문서는 "어떻게 점수화하고 비교하는가"(지표·정답·채점 방식·절차)를 고정한다.

이 문서는 **평가 방법론**을 정의한다. testbed의 현재 구성·시나리오·커버리지
현황은 휘발성이 크므로 별도 실사 문서나 runbook에서 관리하고, 여기서는
방법론만 고정한다.

목적 문서의 비목표를 그대로 승계한다. 특히 이 문서는 채점식을 확정하되,
채점식이 agent를 과확신하게 만들거나 골든 정답에 과적합시키는 방향으로
설계되지 않도록 안전 장치를 함께 정의한다.

## 1. 설계 원칙

- **축 분리.** 정확도, 깊이, 근거 품질, 과확신 억제, 사례 활용 효과는 서로
  다른 실패 양상이다. 하나의 종합 점수로 뭉치지 않고 축별로 따로 보고한다.
- **정직성 우선.** 근거가 부족할 때 확정하지 않는 것을, 정답을 맞히는 것보다
  먼저 채점한다. 과확신은 정확도 이득으로 상쇄되지 않는 하드 실패로 다룬다.
- **신호 비편향.** 목적 문서가 요구한 "특정 신호나 도메인에 미리 편향되지
  않음"을 평가에서 검증한다. 원인이 어느 신호 계열(§5)에 있든 골고루 측정하고
  계열 편향을 지표로 드러낸다. 계열 층화는 비편향을 *검증하는 도구*이지,
  agent가 그 계열 구획으로 사고해야 한다는 뜻이 아니다.
- **채점은 하이브리드.** 구조화 필드의 동일성(예: `status` enum 비교,
  단일원인 게이트의 불리언)은 **결정론**으로 채점한다 — 더 재현 가능하고
  저렴하며 채점관 drift가 없다. 표현이 달라지는 **의미 등가**(같은 원인을
  다르게 서술, 근거가 실제로 주장을 지지하는가)만 **LLM 채점관**이 정답
  레코드(§4)와 대조한다. 정규식으로 원인·근거를 매칭하지는 않는다.
- **양방향 과적합 금지.** agent 코드를 특정 golden 케이스에 1:1로 맞추지
  않는다. 동시에 golden 정답을 agent의 약점에 맞춰 하향 조정하지 않는다.
  정답은 시나리오 설계 의도에 고정한다.
- **평가 하네스 우선.** agent 내부 엔진보다 평가 하네스와 golden set을 먼저
  세운다. 지표가 없는 상태의 성능 개선은 측정 불가능한 개선이다.

## 2. 평가 대상 단위

한 번의 평가 실행(run)은 다음을 입력·출력으로 한다.

- **입력:** 하나의 인시던트 seed (목적 문서 입력 스키마).
- **출력:** 하나의 RCA 리포트 (목적 문서 출력 스키마 — `status`, `cause`,
  `causal_chain`, `evidence`, `alternatives`, `missing_evidence` 등).
- **정답:** 해당 인시던트에 대응하는 정밀 정답 레코드 (§4).

채점은 리포트와 정답 레코드를 축별로 대조해 점수를 산출한다. 구조 필드는
결정론, 의미 등가는 LLM 채점관이 담당한다(§7).

## 3. 평가 축별 지표

각 지표는 "정답 레코드의 어느 필드"와 "agent 출력의 어느 부분"을 대조하는지,
그리고 채점 방식(결정론 / LLM)이 무엇인지로 정의된다. LLM 대조는 두 모드로
나뉜다 — *reference 모드*(정답 노출, 매칭용), *blind 모드*(정답 미노출, 내부
정합성용). §7 참조.

### 3.1 원인 식별 정확도

정답 `cause.entity`(= `target_id` + 차원, §4)와 agent가 지목한 원인을 대조한다.
표현이 다를 수 있어 의미 등가는 LLM(reference)이 판정한다. 단, agent가 원인을
구조화된 `target_id`로 함께 방출하면 그 부분은 결정론으로 대조한다.

기본값: **`k = 3`** (`cause_hit@k`, `alternatives` 상위 포함). scenario별
후보 수 상한은 §10에서 확정.

| 지표 | 정의 | 채점 |
| --- | --- | --- |
| `cause_hit@1` | 1순위 원인이 정답 엔티티와 같은 것을 가리키면 1 | LLM(ref) + 구조 필드 결정론 |
| `cause_hit@k` | 상위 k개 후보(1순위 + `alternatives`) 안에 정답이 있으면 1 (k=3) | 동일 |
| `cause_mrr` | 정답이 후보 목록에서 차지한 순위의 역수 (MRR) | 동일 |

LLM 채점관은 정답의 `accept_if`/`reject_if` 가이드(§4)를 판정 경계로 쓴다.

### 3.2 실행 단위 지목 깊이

깊이는 별도 분류 체계가 아니라 **식별자의 차원(dimension) 도달 여부**로
정의된다. lucida-next 엔티티 모델에서 실행 단위는 `target_id`(덩어리) +
차원 라벨/값(그 안의 세부 단위)이다. 원인이 세부 수준인데 agent가 덩어리까지만
짚으면 얕은 것이다.

| 지표 | 정의 | 채점 |
| --- | --- | --- |
| `depth_match` | `expected_depth=dimension`인 시나리오에서 agent가 차원까지 맞히면 1 | LLM(ref) |
| `over_shallow_rate` | `target_id`는 맞았으나 요구된 차원에 도달 못 한 비율 | LLM(ref) |

깊이는 §3.1 정확도의 부분점수로 흡수된다: `target_id` 틀림 = 오답,
`target_id` 맞고 차원 미달 = 부분정답(얕음), `target_id` + 차원 맞음 = 정답.
원인이 리소스 레벨인 시나리오는 `expected_depth: entity`로 두어 차원 없이도
정답으로 인정한다.

### 3.3 근거 품질

정답의 `must_support`·`must_rule_out`·`expected_missing`과 agent 리포트를
대조한다.

| 지표 | 정의 | 채점 |
| --- | --- | --- |
| `evidence_grounded` | 채택 원인의 각 주장이 evidence 참조를 동반하는가 | 결정론(참조 존재) |
| `support_recall` | 정답 `must_support`를 지지 근거로 담았는가 | LLM(ref) |
| `ruleout_recall` | 정답 `must_rule_out`(예: DB-normal negative evidence)을 반증으로 들었는가 | LLM(ref) |
| `evidence_consistency` | 인용한 근거가 실제로 그 원인을 지지·반증하는가 | LLM(**blind**) |
| `missing_recall` | 정답 `expected_missing`을 `missing_evidence`에 담았는가 | LLM(ref) |

`evidence_consistency`만 blind 모드다 — 정답 미노출로, 인용 근거만으로 주장이
서는지 본다. 정답을 흘리면 채점관이 역산한다(§7).

### 3.4 과확신 억제 (hard gate, 결정론)

정답의 `has_single_cause`·`expected_status`와 agent의 `status`(구조화 enum
필드)를 대조한다. **모두 결정론으로 채점한다** — 구조 필드 비교라 LLM이
필요 없고, 이로써 채점관의 verbosity/confidence bias가 원천 차단된다.

| 지표 | 정의 |
| --- | --- |
| `false_confident_on_null` | `has_single_cause=false`인데 `status=confirmed`를 낸 비율. **목표 = 0 (하드 게이트)** |
| `status_match` | agent `status`가 정답 `expected_status`와 정합한가 |
| `abstain_correctness` | 데이터 부족 케이스에서 `insufficient`/`provisional`로 낮췄는가 |

`false_confident_on_null`이 0이 아니면 그 run은 다른 축 점수와 무관하게
정직성 실패로 별도 표기한다. 음성 시나리오는 noisy-logs-only 같은 guardrail
패턴을 포함해야 한다.

### 3.5 과거 사례 활용 효과 (comparative, paired)

유사 사례가 원인 후보 생성이나 판단 품질을 개선하는지, 사례 활용 on/off를
**같은 시나리오에서 켜고 끈 paired 비교**로 측정한다(§8, §9). paired 설계는
차이의 분산을 줄여 적은 N으로도 lift를 강하게 말할 수 있게 한다.

| 지표 | 정의 |
| --- | --- |
| `case_lift_accuracy` | 사례 on 조건의 `cause_hit@k` − off 조건의 값 (paired) |
| `case_lift_depth` | 사례 on/off 간 `depth_match` 차이 (paired) |
| `case_harm_rate` | 사례 주입이 오히려 틀린 원인으로 유도한 비율 |

### 3.6 신호 계열 커버리지·편향 (stratified, 방향성)

§3.1~3.3 지표를 원인이 속한 신호 계열(§5)별로 층화해 재집계한다. 계열별 셀은
표본이 작으므로 **방향성 지표로 보고한다**(유의성 검정은 pooled 결과에서, §9).

| 지표 | 정의 |
| --- | --- |
| `accuracy_by_domain` | 계열별 `cause_hit@k` |
| `cross_domain_bias` | 계열 간 정확도 분산 — 특정 계열에서만 잘하는 편향의 크기 |
| `cross_domain_chain` | 원인 계열 ≠ 증상 계열인 시나리오에서의 정확도 |

`cross_domain_chain`이 RCA의 실질 난도다 — 증상이 뜬 계열과 원인이 있는
계열이 다를 때 계열을 넘어 추적하는 능력을 본다.

## 4. 정답(golden) 레코드 스키마

각 시나리오는 하나의 정답 레코드를 가진다. 원천은 testbed `service-spec.yaml`
시나리오 필드이며, 채점 가능하도록 정밀화한다.
채점관이 대조할 수 있을 만큼 **의미에 대해 정밀**하되, 표현은 박지 않는다.

```yaml
reference:
  incident_ref: food-delivery/scenario-XX

  # ── 정직성·단정 수준 (§3.4, 결정론 채점) ─────────
  has_single_cause: true             # 음성 시나리오면 false
  expected_status: confirmed         # confirmed | provisional | insufficient

  # ── 원인 (§3.1 정확도 · §3.2 깊이 · §3.6 계열) ────
  cause:
    domain: DPM                      # APM|DPM|SMS|NMS|KCM|WPM (원인이 속한 계열)
    entity:
      target_id: "018f...-db-0001"   # 정본 식별자 (덩어리)
      dimension:                     # 그 안의 세부 단위 (해당 시). 정본 식별자는 §5
        label: sql_id                # 점유 트랜잭션 SQL (TopSQL). "table"은 lucida-next DPM 미수집 차원
        value: "pg:...."
    mechanism: "장기 트랜잭션(해당 sql_id)이 inventory row lock 점유 → lock wait 누적"
    expected_depth: dimension        # entity(target_id만) | dimension(차원까지 요구)
  accept_if: >                       # 정답 인정 경계 (LLM 채점관 가이드)
    target_id=DB를 짚고, lock wait를 유발한 점유 트랜잭션(sql_id / 해당 SQL)을
    원인으로 지목하면 정답. inventory 테이블은 SQL 본문으로 확인되면 정합.
  reject_if: >
    호스트 CPU나 다른 서비스를 원인으로 지목하면 오답.

  # ── 근거 품질 (§3.3) ─────────────────────────────
  must_support:
    - "DPM lock_wait 상승 (postgresql.db.lock_wait, db 단위)"
    - "점유/대기 세션 및 TopSQL(sql_id) 식별"
  must_rule_out:
    - "호스트/서비스 CPU 정상 → 리소스 원인 배제"
  expected_missing: []

  # ── 인과사슬 (§3.2 관련) ─────────────────────────
  propagation:
    - "background tx가 inventory row lock 점유"
    - "동시 요청 lock 대기 누적 → 스레드풀 고갈"
    - "상위 서비스 timeout/5xx"

  difficulty: 5                      # 1~5 (층화 보고용)
```

필드별 채점 연결:

| 필드 | 의미 | 채점 연결 |
| --- | --- | --- |
| `has_single_cause` | 명확한 단일 원인 존재 여부 | §3.4 게이트 (결정론) |
| `expected_status` | 기대 결론 단정 수준 | §3.4 (결정론) |
| `cause.domain` | 원인이 속한 신호 계열 (**필수** — §3.6 층화 전제) | §3.6 |
| `cause.entity.target_id` | 원인 엔티티 정본 식별자 (**필수**) | §3.1 |
| `cause.entity.dimension` | 그 안의 세부 단위 (원인이 세부 수준일 때) | §3.2 |
| `cause.mechanism` | 원인 작동 방식 | §3.1 판정 보조 |
| `expected_depth` | `entity` 또는 `dimension` | §3.2 |
| `accept_if`/`reject_if` | 정답 인정·불인정 경계 | §3.1 LLM 가이드 |
| `must_support`/`must_rule_out`/`expected_missing` | 근거 요건 | §3.3 |
| `propagation` | 원인→현상 기대 인과사슬 | 인과사슬 평가 |
| `difficulty` | 난이도 1~5 | 층화 보고 |

음성(null-cause) 시나리오는 같은 스키마로 `has_single_cause: false`,
`expected_status: insufficient`, `cause: {domain: null, entity: null}`,
`accept_if: "측정 양성신호 없음을 인지하고 uncertainty 유지"`로 채운다.

`expected_alarms`는 **알람 발화 검증용이며 RCA 채점 정답이 아니다.** 정답
레코드와 명시적으로 분리한다.

정답 레코드는 시나리오 설계 의도에 고정하며 agent 출력에 맞춰 조정하지
않는다(§1 양방향 과적합 금지). `target_id`·차원 값은 testbed가 이미 아는
정보(우리가 장애를 심으므로)라 별도 발굴 노동이 없다 — 시나리오 작성 시
정밀하게 적기만 하면 된다.

## 5. Cause-domain 층화 축 (v1)

RCA 평가의 원인 계열은 **v1 측정 커버리지 축**으로 아래 6종을 쓴다. 이는
"RCA가 근본적으로 이 6개로 나뉜다"는 연구적 고정 진리가 아니라, **현재
lucida-next 수집과 testbed가 산출할 수 있는 범위에서 편향을 측정하기 위한
층화 축**이다. 계열·차원 라벨은 확장 가능하며 최종 schema 전까지 고정이 아니다.

차원은 임의 라벨이 아니라 lucida-next census
([ref-lucida-next-data-collection.md](ref-lucida-next-data-collection.md))의
실제 식별자에 바인딩한다 — PG resource inventory의 `resource_kind`+`resource_key`
쌍, 또는 시계열 allowlist 라벨/ClickHouse 컬럼. 아래 표는 그 정본 식별자를
계열별로 옮긴 것이다(고정 아님, §10에서 정본 목록 확정).

| 계열 | 의미 | 정본 차원 (census 근거) |
| --- | --- | --- |
| APM | 애플리케이션 — trace/log/metric | `service_name`, span/endpoint (OTLP 속성·CH 컬럼) |
| DPM | 데이터베이스 — session·TopSQL | `sql_id`(예 `pg:9fd1`); CH 엔진은 database/table; custom SQL 라벨(`database`,`lock_mode`) |
| SMS | 서버/호스트 — CPU·메모리·디스크·프로세스 | resource `filesystem`(key=마운트), `interface`(key `eth0`); 프로세스 라벨 `process_pid`/`process_executable_name` |
| NMS | 네트워크 — SNMP·trap·NetFlow | resource `network_interface`(key `ifIndex:N`, 표시 `Gi1/0/12`); NetFlow `in_iface`/`out_iface` |
| KCM | 컨테이너 — node·pod·container·workload | `resource_kind`∈{pod,container,node,…} + `resource_key`(`ns/name`); 라벨 `pod_name`/`container_name` |
| WPM | 웹 — xlog·probe | `url`; profile 세부 단계(phase) |

DPM의 "table lock" 같은 파생 개념은 native 차원이 아니다 — lucida-next는
PostgreSQL lock을 `postgresql.db.lock_wait`(db.name 단위 카운트)로, 그 외 엔진은
세션 blocking 분류(`blocking`/`blocked`, `blocking_pid`)로 수집하고, 점유 SQL은
`sql_id`(TopSQL)로 도달한다(테이블 단위 lock 차원은 없음). 시나리오 작성 시 이
실제 식별자로 바인딩한다([spec-scenario-authoring.md](spec-scenario-authoring.md) §3.2).

lucida-next 수집은 이 6종에 더해 VMM·FMS·BMC까지 9계열을 수집하지만
([ref-lucida-next-data-collection.md](ref-lucida-next-data-collection.md)),
평가 v1 층화 축은 6종으로 시작한다. 확장 여부는 §10.

**golden set은 이 6종을 필수 층화 축으로 삼는다** — 계열마다 원인이 그 계열에
있는 시나리오를 확보한다. 현 testbed의 계열 커버리지와 갭은 실제 환경 실사로
별도 확정한다.

### 5.1 Golden set 규모

목표 규모는 **약 60개**로 확정한다. 서비스·연구 양쪽 모두 계열 편향을 방향성
이상으로 보고 pooled 메인 주장을 검정하려면 이 정도가 최소선이다.

| 구성 | 수 |
| --- | --- |
| 양성 시나리오 (계열당 ~8) | 6종 × 8 = ~48 |
| 음성(null-cause) 쿼터 (계열 횡단) | ~10 |
| **합계** | **~58~60** |

- 난이도(1~5)는 각 계열 안에 섞는다 — 별도 층화 축으로 쪼개지 않는다.
- cross-domain-chain(원인 계열 ≠ 증상 계열, §3.6)을 계열당 1~2개 지정한다.

**단계적 구축.** 목표는 60으로 고정하되 전량 완성 후 착수하지 않는다.

- **Phase 1 (~30):** 계열당 4~5 + 음성 ~6. 평가 하네스 가동 + pooled 메인
  주장 + 계열 방향성 최소 확보. 계열당 1~2개로 정답 스키마(§4)를 현실
  검증하는 얇은 수직 슬라이스를 포함한다.
- **Phase 2 (~60):** 논문 제출·서비스 검증 전 목표치까지 확장.

실제 신규 작성 부담은 현 testbed 실사 후 확정한다. 평가 방법론상으로는
NMS/KCM/WPM을 포함한 6종 계열이 모두 필요하다.

## 6. Testbed

평가 golden set의 원천인 RCA testbed의 구성·시나리오 스키마·계열 커버리지는
실제 환경 실사 후 별도 문서나 runbook에서 관리한다. 이 문서는 testbed 현황을
확정하지 않고, golden set이 만족해야 하는 평가 요건만 정의한다.

## 7. LLM 채점관 활용 규율

구조 필드는 결정론으로 채점하고(§3.4 전체, §3.1의 구조 식별자, §3.3의
`evidence_grounded`), 의미 등가만 LLM 채점관에 맡긴다. LLM 채점관은 **정밀
정답에 대조하는 비교기**이지 혼자 품질을 평가하는 심사위원이 아니다.

- **항상 정답 대조.** 채점관은 스스로 "무엇이 정답인가"를 판단하지 않고, 정답
  레코드(§4)의 해당 필드와 agent 출력을 대조만 한다. 정답이 정밀할수록 재량이
  줄어 재현성이 오른다.
- **과확신은 LLM에 맡기지 않는다.** 단정 수준 채점(§3.4)은 구조 필드 결정론
  비교라, 채점관의 confidence/verbosity bias가 개입할 여지가 없다.
- **두 모드 분리:**
  - *reference 모드* — 정답 노출. 매칭·부분점수·recall용(§3.1, §3.2,
    §3.3 대부분).
  - *blind 모드* — 정답 미노출. 내부 정합성용(§3.3 `evidence_consistency`).
- **채점 재현성:** agent와 다른 모델 계열 사용(self-preference bias 완화),
  temperature 0, 축별 점수+근거의 구조화 출력, 복수 샘플 median.
- **채점관 검증(필수):** LLM이 맡는 의미 채점은 사람이 채점한 서브셋으로
  채점관-사람 일치도를 측정·보고한다. 이 검증 없이는 "agent 성능"이 아니라
  "채점관의 취향"을 측정하는 것이다.

## 8. Baseline과 Ablation

### 8.1 Baseline

| Baseline | 목적 |
| --- | --- |
| Naive LLM | seed만 주고 조회·검증 없이 바로 원인 답변. 조회·반증 파이프라인 기여의 하한선 |
| No-retrieval agent | 과거 사례 활용을 끈 agent. §3.5 대조군 |
| 기존 파이프라인(있다면) | 현행 RCA 방식과의 비교. 실체·접근성 별도 확인 필요 |

**성공은 절대 숫자가 아니라 baseline 대비 상대 개선 + 유의성 검정으로
정의한다.** "얼마나 이겨야 의미 있는가"의 실제 임계값은 지금 지어내지 않고
Phase 1 baseline 실측 후 확정한다(data-dependent). 지금 고정하는 것은 성공의
*정의 방식*과 baseline 라인업까지다.

### 8.2 Ablation (paired)

각 컴포넌트를 **같은 golden set에서 켜고 끈 paired 비교**로 축별 점수를
비교한다. 각 시나리오가 자기 자신의 대조군이 되어 차이의 분산이 줄어든다.

- 데이터 조회(신호 수집) on/off
- 반증·배제 단계 on/off
- 인과사슬 구성 on/off
- 신뢰도·calibration 표기 on/off
- 과거 사례 활용 on/off (§3.5는 이 조건의 특수 케이스)

각 컴포넌트를 끌 때 어느 축이 얼마나 떨어지는지가 그 컴포넌트의 존재
근거이자 논문 기여의 실험 증거다.

## 9. 실험 절차와 재현

- **주장 계층화.** 메인 주장("작동한다 / baseline을 이긴다")은 전 시나리오
  pooled로 유의성 검정. 계열별(§3.6)은 방향성으로 보고. lift·ablation(§3.5,
  §8.2)은 paired 비교로 검정.
- **고정 요소:** golden set 버전, agent 버전, 사용 모델, 채점관 모델,
  ablation 조건을 run마다 기록한다. 모델은 교체 가능한 변수로 취급하고 코드에
  하드코딩하지 않는다.
- **다중 시행:** LLM 비결정성을 고려해 조건당 복수 회 실행하고 평균과
  분산을 함께 보고한다. 채점관도 복수 샘플 median(§7).
- **보고 형식:** 축별 지표를 조건(baseline·ablation)·cause-domain별로 나열한
  표를 산출한다. 종합 단일 점수는 부가 정보로만 둔다.
- **재현:** golden set과 실행 설정으로부터 결과 표를 다시 만들 수 있어야
  한다.

## 10. 열린 결정

- **성공 임계값(data-dependent).** 성공의 정의 방식(baseline 대비 상대 개선 +
  유의성)은 확정. 실제 임계값은 Phase 1 baseline 실측 후 정한다(§8.1).
- **기존 파이프라인 baseline 가용성.** 옆 `lucida-rca-agent`가 비교군으로
  쓸 수 있는 상태인지 사실 확인(실측 아님, §8.1).
- **채점관 검증 규모.** 채점관-사람 일치도를 몇 개 샘플로, 어떤 지표로
  보고할지(§7).
- **차원 라벨 세트 확정.** 6종 계열별 차원 라벨의 정본 목록(§5 표는 초안).
- **`cause_hit@k`의 후보 수 상한.** k=3 기본이나 scenario별 후보 상한 정책(§3.1).
- VMM 이상 계열로의 평가 범위 확장 여부(§5).
- 종합 순위가 필요할 때의 축 가중치.
