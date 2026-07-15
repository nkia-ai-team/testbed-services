---
title: 시나리오 작성 규칙과 ground truth 형식
status: Draft
owner: project
last_reviewed: 2026-07-08
tags:
  - testbed
  - scenario
  - evaluation
  - ground-truth
summary: testbed 시나리오(service-spec.yaml)를 채점 가능한 정밀 정답(golden) 레코드로 끌어올리는 작성 규칙과, 6종 계열별 원인 시나리오·음성 시나리오 작성 패턴을 정의한다.
---

# 시나리오 작성 규칙과 ground truth 형식

이 문서는 [평가·실험 설계](spec-evaluation.md)의 정답 레코드 스키마(§4)와
testbed `service-spec.yaml` 시나리오 스키마 사이의 **변환 규칙**을 정의한다.
testbed의 `service-spec.yaml`은 장애를 *설계·주입*하기 위한 형식이고, 평가는
*채점 가능한* 정밀 정답을 요구한다. 둘은 같은 시나리오의 두 얼굴이며, 이
문서는 전자를 후자로 끌어올리는 규칙을 고정한다.

이 문서가 담당하는 것과 담당하지 않는 것:

- **담당:** 시나리오 하나를 어떤 정밀도로 적어야 채점이 되는가, 계열별로
  원인을 어디에 심어야 하는가, 정답 레코드 각 필드를 어떻게 채우는가.
- **비담당:** 채점 지표·채점식([spec-evaluation.md](spec-evaluation.md)),
  testbed 실행 절차(별도 runbook), 개별 시나리오의 실제 목록·현황.

## 1. 한 시나리오, 두 소비자

시나리오 하나는 두 곳에서 소비된다. 둘을 같은 설계 의도에 고정하되 형식은
분리한다.

| 소비자 | 형식 | 위치 | 용도 |
| --- | --- | --- | --- |
| testbed 실행 | `service-spec.yaml` 항목 | 컨테이너 `/app/scenarios/<domain>/` | 장애 주입·알람 검증 |
| 평가 채점 | golden 레코드 | golden set (평가 하네스) | RCA 리포트 채점 |

핵심 규율은 [spec-evaluation.md §1](spec-evaluation.md)의 **양방향 과적합
금지**를 그대로 승계한다. 정답은 시나리오 *설계 의도*(우리가 무슨 장애를
어디에 심었는가)에 고정하며, agent 출력에 맞춰 조정하지 않는다. 우리가
장애를 심은 주체이므로 `target_id`·차원 값은 이미 알고 있다 — 발굴 노동이
아니라 **정밀하게 받아쓰는** 작업이다.

## 2. service-spec.yaml → golden 레코드 매핑

`service-spec.yaml` 필드가 golden 레코드([spec-evaluation.md §4](spec-evaluation.md))의
어느 필드로 가는지, 그 과정에서 무엇을 정밀화해야 하는지를 고정한다.

| service-spec 필드 | golden 레코드 필드 | 정밀화 작업 |
| --- | --- | --- |
| `id`, `title` | `incident_ref` | `<domain>/<id>`로 그대로 |
| `root_cause` (자연어) | `cause.mechanism` + `cause.entity` | **자연어 원인을 정본 식별자로 분해** (§3) |
| `expected_rca_root_cause` | `accept_if` / `reject_if` | 채점 경계 문장으로 다듬기 (§4) |
| `propagation` | `propagation` | root→현상 인과사슬 그대로, 단계 명확화. **golden에서는 단계별 구조체** `{step, description, targets:[{target_id, name}]}` — 각 단계의 대상도 정본 target_id로 기입(2026-07-15 확정, 첫 케이스 적용). 사용자 체감 등 대상 없는 단계는 `targets: []` |
| `expected_alarms` | (매핑 안 함) | **알람 검증용, RCA 정답 아님** — 분리 유지 |
| `difficulty` | `difficulty` | 없으면 1~5로 부여 |
| (신규) | `cause.domain` | 원인이 속한 계열 명시 (§5, **필수**) |
| (신규) | `expected_depth` | `entity` / `dimension` 결정 (§3.2) |
| (신규) | `has_single_cause`, `expected_status` | 정직성 게이트값 (§4) |
| (신규) | `must_support`, `must_rule_out`, `expected_missing` | 근거 요건 (§4) |

`(신규)` 표기 필드는 `service-spec.yaml`에 대응 원천이 없어 golden 레코드를
쓸 때 **추가로 결정**해야 하는 값이다. 이 결정들이 "채점 가능한 정밀도"의
실체다.

`expected_alarms`는 어떤 경우에도 RCA 채점 정답으로 흘려보내지 않는다.
알람은 "발화했어야 하는 신호"를 검증하고, RCA 정답은 "원인으로 짚어야 하는
엔티티"를 검증한다 — 다른 축이다([spec-evaluation.md §4](spec-evaluation.md)).

## 3. 원인 엔티티 정밀화 규칙

`root_cause` 자연어를 채점 가능한 `cause.entity`로 분해하는 것이 이 문서의
핵심이다. lucida-next 엔티티 모델에서 실행 단위는 `target_id`(덩어리) +
차원 라벨/값(그 안의 세부 단위)이다([spec-evaluation.md §3.2](spec-evaluation.md)).

### 3.1 `target_id` — 정본 식별자

- testbed가 장애를 심은 **실제 리소스의 정본 식별자**를 적는다. 표시 이름이
  아니라 수집 스택이 부여하는 식별자다.
- 우리가 배포·주입 주체이므로 이 값은 이미 결정돼 있다. 시나리오 작성 시
  배포 산출물(리소스 등록 결과)에서 그대로 옮긴다.
- 추정하지 않는다. 식별자를 아직 모르면 그 시나리오는 **미완성**이며 golden
  set에 넣지 않는다.

### 3.2 `dimension` — 세부 단위와 `expected_depth`

원인이 `target_id` 덩어리 자체인지, 그 안의 세부 단위인지에 따라
`expected_depth`를 정한다.

- 원인이 **리소스 레벨**(예: 호스트 전체 CPU 포화) → `expected_depth: entity`.
  `dimension`을 비우고, agent가 `target_id`만 맞혀도 정답.
- 원인이 **세부 단위**(예: 점유 트랜잭션 SQL `sql_id`, 특정 인터페이스 포화) →
  `expected_depth: dimension`. `dimension.label`/`value`를 채우고, agent가
  거기까지 내려가야 완전 정답(덩어리까지만 = 부분정답, 얕음).

**차원은 임의 라벨이 아니라 census의 실제 식별자에 바인딩한다.** 세부 단위는
lucida-next에서 두 형태로만 표현된다
([ref-lucida-next-data-collection.md](ref-lucida-next-data-collection.md)):

- PG resource inventory의 `resource_kind` + `resource_key` 쌍 (예:
  `network_interface` / `ifIndex:12`, `pod` / `default/checkout-...`).
- 시계열 allowlist 라벨 또는 ClickHouse 컬럼 (예: `pod_name`, `sql_id`,
  `process_pid`, `url`).

`dimension.label`/`value`에는 이 정본 식별자를 적는다 — 추상 라벨(`snmp_index`
같은)을 지어내지 않는다. 그래야 채점 시 agent 출력과 결정론적으로 대조된다.
계열별 정본 차원은 [spec-evaluation.md §5](spec-evaluation.md) 표를 따르되
최종 확정 전까지 고정이 아니며, 새 식별자가 필요하면 §5 확장을 함께 제안한다.

### 3.3 계열(`cause.domain`) 지정

`cause.domain`은 **필수**다. 계열 층화([spec-evaluation.md §3.6](spec-evaluation.md))의
전제이기 때문이다. 원인이 실제로 작동하는 계열을 적는다 — 증상이 뜬 계열이
아니라. 증상 계열과 원인 계열이 다르면 cross-domain-chain 시나리오다(§5).

## 4. 채점 경계와 근거 요건

### 4.1 `accept_if` / `reject_if`

LLM 채점관은 이 두 문장을 판정 경계로 쓴다([spec-evaluation.md §7](spec-evaluation.md)).
정답 레코드가 정밀할수록 채점관 재량이 줄어 재현성이 오른다.

- `accept_if`: 무엇을 짚으면 정답인가. **엔티티 + 메커니즘**을 함께 조건으로
  건다. 예: "target_id=DB를 짚고 lock wait를 유발한 점유 트랜잭션(`sql_id`)을
  원인으로 지목하면 정답."
- `reject_if`: 흔한 오답을 명시적으로 배제한다. 특히 **그럴듯한 증상 계열
  오답**을 적는다. 예: "호스트 CPU나 다른 서비스를 원인으로 지목하면 오답."
- 표현은 박지 않는다. 같은 원인을 다르게 서술해도 의미가 맞으면 정답 —
  정규식 매칭이 아니라 의미 등가 판정이다.

### 4.2 근거 요건 3종

| 필드 | 채운다 | 예 |
| --- | --- | --- |
| `must_support` | 채택 원인을 지지하는 **양성 측정 신호** | "DPM `lock_wait` 상승 (db 단위) + 점유 SQL(`sql_id`)" |
| `must_rule_out` | 배제해야 하는 **반증/정상 신호** | "호스트/서비스 CPU 정상 → 리소스 원인 배제" |
| `expected_missing` | 결론을 강화하지만 수집 안 된 데이터 (없으면 `[]`) | — |

`must_rule_out`은 RCA 품질의 핵심이다 — 그럴듯한 대안을 **정상 근거로**
배제할 수 있는지를 채점한다. 원인이 세부 단위인 시나리오에는 "인접 단위는
정상" 형태의 반증을 최소 하나 넣는다.

## 5. 계열별 원인 시나리오 패턴

6종 계열([spec-evaluation.md §5](spec-evaluation.md)) 각각에서 원인이 되는
전형과 대표 차원을 정리한다. golden set은 계열마다 원인이 그 계열에 있는
시나리오를 확보해야 한다.

대표 `dimension`은 census 정본 식별자다([spec-evaluation.md §5](spec-evaluation.md)).

| 계열 | 전형적 원인 | 대표 `dimension` (census 정본) | 흔한 증상 계열 |
| --- | --- | --- | --- |
| APM | 코드 경로 지연, 의존 호출 폭주, 예외 급증 | `service_name`, span/endpoint | APM (동일) |
| DPM | lock 경합, 느린 SQL, 커넥션 고갈 | `sql_id`(TopSQL); lock은 `lock_wait`(db 단위)·세션 blocking 분류 (table 단위 미수집) | APM (상위 서비스) |
| SMS | 호스트 CPU/메모리/디스크 포화, 프로세스 이상 | `filesystem`/`interface` resource, `process_pid` | APM/DPM |
| NMS | 인터페이스 포화·에러·flap, 패킷 손실 | `network_interface`/`ifIndex:N` (표시 `Gi1/0/12`) | APM/WPM |
| KCM | 노드 압박, pod OOM/eviction, 스케줄 실패 | `pod`/`container` resource, 라벨 `pod_name` | APM |
| WPM | synthetic probe 실패, 특정 phase 지연 | `url`, profile phase | WPM (동일) |

작성 지침:

- **계열당 원인 심기.** 원인이 확실히 그 계열에서 발생하도록 장애를
  설계한다. 증상은 다른 계열로 번져도 좋다(오히려 cross-domain).
- **cross-domain-chain을 계열당 1~2개.** 원인 계열 ≠ 증상 계열인 시나리오를
  넣는다([spec-evaluation.md §3.6](spec-evaluation.md)). 예: NMS 인터페이스
  포화(원인) → APM 상위 서비스 latency(증상). 이게 RCA의 실질 난도다.
- **난이도 혼합.** 난이도 1~5를 계열 안에 섞는다 — 별도 층화 축으로 쪼개지
  않는다([spec-evaluation.md §5.1](spec-evaluation.md)).

현 testbed의 실제 계열 커버리지는 별도 실사로 확정한다. 이 문서의 규칙은
기존 시나리오 정밀화와 신규 시나리오 작성 모두에 적용되는 형식 계약이다.

## 6. 음성(guardrail) 시나리오 패턴

명확한 단일 원인이 없는(또는 측정 양성신호가 없는) 시나리오를 계열 횡단으로
~10개 확보한다([spec-evaluation.md §5.1](spec-evaluation.md)). 과확신 억제
게이트([spec-evaluation.md §3.4](spec-evaluation.md))를 검증한다.

같은 golden 스키마를 이렇게 채운다:

```yaml
has_single_cause: false
expected_status: insufficient
cause: {domain: null, entity: null}
accept_if: >
  측정 양성신호가 없음을 인지하고 uncertainty를 유지하면 정답.
reject_if: >
  로그 키워드만으로 특정 원인을 confirmed로 확정하면 오답.
must_support: []
must_rule_out: []
```

전형 패턴:

- **잡음만 있는 로그.** 측정 신호는 정상인데 로그 키워드가 원인을 암시 —
  키워드만으로 확정하면 실패. 예: `noisy-logs-only` guardrail.
- **다중 동시 이상.** 여러 계열이 동시에 흔들려 단일 원인 확정이 부당한 경우.
- **수집 공백.** 원인 계열의 데이터가 수집되지 않아 `expected_missing`으로
  누락을 짚어야 하는 경우 (`has_single_cause: true`이되 `expected_status:
  insufficient` 가능).

## 7. 적용 예시 — NMS 수직 슬라이스 (초안)

NMS 계열이 현 testbed에 0개이므로, 신규 작성의 형태를 end-to-end로 보인다.
아래는 **형식 예시**이며 실제 시나리오·식별자는 배포 시 확정한다.

원인 설계: social-feed 도메인의 상위 스위치 업링크 인터페이스가 포화·에러
증가 → 패킷 손실·재전송 → 상위 서비스 latency/timeout(증상은 APM 계열).
전형적 cross-domain-chain. 인터페이스는 census 정본대로
`resource_kind=network_interface` / `resource_key=ifIndex:N`(표시명
`Gi1/0/12`)로 식별한다 — 아래 `ifIndex:12`는 예시 값이다.

`service-spec.yaml` 쪽(발췌):

```yaml
- id: scenario-11-nms-uplink-saturation
  title: 업링크 인터페이스 포화로 인한 상위 서비스 지연
  root_cause: social-feed 상위 스위치 업링크 인터페이스(ifIndex:12, Gi1/0/12) 포화·에러 증가
  propagation:
    - 업링크 인터페이스 이용률 포화 + 입력 에러 증가
    - 패킷 손실·재전송 누적 → 유효 대역 저하
    - 상위 API 호출 latency 상승 → timeout/5xx
  expected_alarms:
    - NMS: interface 이용률 임계 초과 (ifIndex:12)
    - APM: social-feed API latency 임계 초과
  difficulty: 4
```

golden 레코드 쪽:

```yaml
reference:
  incident_ref: social-feed/scenario-11-nms-uplink-saturation

  has_single_cause: true
  expected_status: confirmed

  cause:
    domain: NMS
    entity:
      target_id: "<배포 시 확정: 스위치 target 정본 UUID>"
      dimension:
        label: resource_key         # resource_kind=network_interface
        value: "ifIndex:12"         # 표시명 Gi1/0/12
    mechanism: "업링크 인터페이스 포화·입력 에러 → 패킷 손실·재전송 → 상위 서비스 지연"
    expected_depth: dimension

  accept_if: >
    NMS 인터페이스(ifIndex:12, Gi1/0/12) 포화·에러를 짚고, 그로 인한 패킷
    손실이 상위 서비스 지연을 유발했다고 지목하면 정답.
  reject_if: >
    social-feed 애플리케이션 코드나 DB, 호스트 CPU를 원인으로 확정하면 오답.

  must_support:
    - "NMS 인터페이스(ifIndex:12) 이용률/입력 에러 상승"
    - "패킷 손실·재전송 증가"
  must_rule_out:
    - "social-feed 호스트 CPU/메모리 정상 → 리소스 원인 배제"
    - "DB session/lock 정상 → 데이터 계층 원인 배제"
  expected_missing: []

  propagation:
    - "업링크 인터페이스 포화 + 입력 에러 증가"
    - "패킷 손실·재전송 누적 → 유효 대역 저하"
    - "상위 API latency 상승 → timeout/5xx"

  difficulty: 4
```

이 예시가 보이는 것: 증상은 APM(latency/5xx)에서 뜨지만 `cause.domain`은
NMS이고, `must_rule_out`이 그럴듯한 증상 계열 오답(APP/DB/호스트)을 정상
근거로 배제한다. `expected_depth: dimension`이라 agent가 인터페이스(ifIndex:12)
차원까지 내려가야 완전 정답이다.

## 8. 작성 체크리스트

시나리오 하나를 golden set에 넣기 전 확인한다.

- [ ] `cause.domain`이 원인 발생 계열로 지정됐다 (증상 계열 아님).
- [ ] `target_id`가 추정이 아닌 배포 산출물의 정본 식별자다.
- [ ] `expected_depth`가 `entity`/`dimension`으로 결정됐고, dimension이면
      `label`/`value`가 채워졌다.
- [ ] `accept_if`가 엔티티+메커니즘을 함께 조건으로 건다.
- [ ] `reject_if`가 그럴듯한 증상 계열 오답을 최소 하나 배제한다.
- [ ] `must_rule_out`에 정상/반증 근거가 최소 하나 있다.
- [ ] `has_single_cause`·`expected_status`가 정직성 게이트에 맞게 설정됐다.
- [ ] `expected_alarms`를 RCA 정답으로 흘리지 않았다.
- [ ] 계열별 cross-domain-chain·음성 쿼터를 계획 대비 추적한다.

## 9. 열린 항목

- 차원 라벨 세트 확정([spec-evaluation.md §10](spec-evaluation.md)과 연동) —
  특히 NMS `network_interface`/`ifIndex:N`, KCM `pod`/`container` resource_key의
  정본 값 형식.
- ~~DPM lock 차원 바인딩~~ **확정.** lucida-next는 table 단위 lock을 수집하지
  않는다 — PostgreSQL은 `postgresql.db.lock_wait`(db.name 단위 카운트,
  `collector-dpm/dbpoll/poll.go`), 그 외 엔진은 세션 blocking 분류
  (`blocking_pid`, `*_session.go`)로 수집하고 점유 SQL은 `sql_id`(TopSQL)로
  도달한다. 골든 lock 시나리오의 `dimension`은 `sql_id`에 바인딩한다
  ([spec-evaluation.md §4](spec-evaluation.md) 예시 반영 완료).
- `target_id` 정본 식별자를 배포 산출물에서 자동 추출하는 절차(수작업 전사
  오류 방지).
- golden 레코드 저장 위치·파일 형식(평가 하네스와의 계약).
- 신규 NMS/KCM/WPM 시나리오 작성 우선순위(실제 testbed 실사 결과와 연동).
