---
title: 대조 쌍 레지스트리 — 무엇이 무엇과 일치해야 하는가
status: Draft
owner: project
last_reviewed: 2026-08-07
tags:
  - scenario
  - contract
  - drift
summary: 신호가 생산자→저장소→소비자→판정→정답지→배포 사본으로 흐르는 사슬의 각 마디에서 일치해야 하는 쌍을 전수로 적고, 각각에 가드가 있는지·기계화 가능한지·불가능하면 사람이 무엇을 봐야 하는지 표시한다. 이 목록은 완전하지 않다.
---

# 대조 쌍 레지스트리 — 무엇이 무엇과 일치해야 하는가

## 왜 이 문서가 있는가

2026-08-07 하루에 드리프트를 **여섯 겹** 찾았는데, 매번 **앞의 것을 고친 뒤에야 다음이
드러났다**:

```
정답지 산문(bd10144) → 구조화 필드(59c484c) → 같은 계약 안의 정규식(59c484c)
  → 러너 allowlist(오늘 두 번 물림) → 109 배포 사본(F23-R window·loadgen 접기)
  → 생산자(4e07550)
```

이건 **운에 기댄 발견**이다. 드리프트 가드가 어제까지 여섯 종(levels·companions·실행기명·
산문 사다리·code_anchor·tag_pattern), 이번에 둘을 더해 여덟 종인데 — 그건 **"우리가 물린 곳"**
이지 **"물릴 수 있는 곳 전부"**가 아니다. 발견을 열거로 바꾸려고 이 목록을 만든다.

> **이 목록은 완전하지 않다.** 오늘 배운 것이 정확히 "목록이 불완전했다"는 사실이다.
> **다음에 새 드리프트가 나오면 그건 여기 없던 쌍이라는 뜻**이므로, 고치면서 이 표에
> 줄을 추가하라. 그것이 이 문서의 사용법이다.

## 신호가 흐르는 사슬

```
생산자          저장소             소비자            판정           정답지
loadgen    →  VM · ClickHouse  →  러너 어댑터  →  게이트/컨트롤러  →  metadata
   └──────────────────── 배포 사본(109) ─────────────────────────┘
```

**드리프트는 이 사슬 어디에서든 난다.** 오늘 여섯 겹이 정확히 서로 다른 마디였다.

## 범례

- **가드** — 자동 검사가 있는가. 있으면 어디에.
- **기계화** — 없다면 자동화 가능한가. ✅ 가능 / ⚠ 부분 / ❌ 원리적으로 불가
- **사람이 볼 것** — 기계가 못 잡는 부분에서 무엇을 확인해야 하는가.

---

## A. 생산자 (loadgen)

| # | 쌍 | 가드 | 기계화 | 사람이 볼 것 |
|---|---|---|---|---|
| A1 | loadgen 발행 필드 ↔ 러너가 읽는 selector | `test_published_steps_match_the_runner_observation_contract` | — | — |
| A2 | baseline 스크립트 태그 ↔ 발행 약속 | `test_baseline_scripts_tag_the_steps_they_promise_to_publish` | — | — |
| A3 | 빈 창은 비율을 안 싣는다 ↔ 러너가 필드 부재를 에러로 | `test_a_request_free_window_publishes_no_rate_at_all` · `test_rates_and_their_denominator_are_published_together` (`4e07550`) | — | 새 비율 지표를 추가할 때 **분모도 함께 발행**하는지 |
| A4 | baseline entrypoint ↔ domain 문서 | `test_every_baseline_entrypoint_publishes_a_domain_document` | — | — |
| A5 | "0이 아티팩트인가 측정값인가" | **없음** | ❌ | `achieved_rps`처럼 **분모가 시간**인 지표는 0이 진짜 값이다. 새 지표마다 사람이 판단해야 한다(§1c) |
| A6 | **한 문서 안 필드들의 분모** ↔ 그 필드들을 AND로 묶는 게이트 | **없음** | ⚠ | 같은 문서에서 와도 분모가 다르다. `checkout_5xx_rate`는 **30초 창 비율**, `business_ok`는 **마지막 한 건**(`entry_status in {200,400,409}`, 창이 바뀌어도 안 지워짐)이다. 성공 조건이 둘을 AND로 묶어 분모 1짜리 신호가 streak을 리셋했다(§23-c, F11-R). **새 필드를 발행할 때 그 값의 분모가 무엇인지 문서에 남길 것** |

## B. 저장소 (VictoriaMetrics · ClickHouse)

| # | 쌍 | 가드 | 기계화 | 사람이 볼 것 |
|---|---|---|---|---|
| B1 | `queries.json` `update_interval_sec` ↔ 실제 수집 주기 | ⚠ p95만(`test_p95_freshness_matches_measured_ingestion_cadence`, 상수 하드코딩) | ⚠ | 새 질의를 추가할 때 실측 케이던스. 틀리면 streak이 같은 표본을 여러 번 센다 |
| B2 | 질의 템플릿의 빈 창 처리 ↔ "없음"의 의미 | **없음** — clickhouse `if(count()=0,0,…)` 미수리(§1b) | ✅ 러너 레포 테스트로 | 새 집계 질의가 빈 창을 0으로 접지 않는지 |
| B3 | 질의가 참조하는 메트릭 이름 ↔ 실재 여부 | **없음**(라이브에서만 확인 가능) | ⚠ 배포 시 스모크 | 신설 질의는 배포 후 실제 값이 나오는지 |
| B4 | **프로브 질의 ↔ 대상 표의 인덱스** | **없음**(전수 점검 1회 완료, 2026-08-07) | ⚠ EXPLAIN 검사 가능 | **프로브도 워크로드다.** 전 프로브를 훑어 🔴 셋을 찾았다(§20): F23-R `inventory_movements`(2.47M·15s), F15-R `payments`(1.19M·15s, 신규), preflight `orders`(1.19M·41종 런당 1회, 신규). 대조는 `outbox_events`로 같은 1.12M인데 부분 인덱스 술어가 정확히 맞아 **cost 4.31**이다. 새 프로브는 **`EXPLAIN`으로 계획을 보고** 붙일 것 — **`ANALYZE`는 금지**(질의를 실제로 돌려 대상을 교란한다) |

## C. 소비자 (러너 어댑터) — **레포 경계**

| # | 쌍 | 가드 | 기계화 | 사람이 볼 것 |
|---|---|---|---|---|
| C1 | `queries.json` `allowed_parameters` ↔ **러너의 실제 allowlist** | **없음(크로스 레포)** | ⚠ 교차 검사 스크립트 | 아래 §G 참조. 기존 테스트 docstring이 이미 인정한다 — "testbed 사본이 더 넓으면 여기선 통과하고 라이브에서 거부된다" |
| C2 | controllers observations params ⊆ `allowed_parameters` | `test_observation_parameters_stay_inside_what_the_query_allows` | — | — |
| C3 | 러너 `Condition` 문법 ↔ 레지스트리가 표현하려는 것 | **없음** | ⚠ | 오늘 물렸다 — 차분 게이트를 쓰려다 `Condition`이 `StrictModel`이라 불가임을 **소스를 읽어** 알았다(§11b). 새 판정 형태를 설계하기 전에 러너 문법을 먼저 확인할 것 |

## D. 판정 (게이트 · 컨트롤러)

| # | 쌍 | 가드 | 기계화 | 사람이 볼 것 |
|---|---|---|---|---|
| D1 | controllers levels ↔ profiles `scenario_levels` | `compile-plan.py` ContractError | — | — |
| D2 | profiles `scenario_parameters` ∈ `scenario_levels` | `compile-plan.py` | — | — |
| D3 | 실행기 `CONTRACTS` ↔ profiles | `test_ready_profile_executors.py` 다수 | — | 새 실행기를 만들 때 대조 테스트를 함께 |
| D4 | `tag_pattern` ↔ `allowed_scenarios` (**양방향**) | `test_every_allowlisted_scenario_also_matches_its_tag_pattern` (`59c484c`에서 양방향으로) | — | — |
| D5 | 레벨 timeout ↔ 부하 길이 ↔ min_hold ↔ 연속 요건 | `test_timing_contracts.py` 4종 | — | — |
| D6 | 임계 단위 ↔ 지표 단위 | `test_latency_thresholds_…` · `test_rate_thresholds_…` | — | — |
| D7 | 감별자 바닥 ↔ 평시 분포 | ⚠ `test_discriminator_floors_sit_at_rest_not_inside_the_ladder` | ⚠ | 평시 분포는 **라이브 실측**이라 코드가 모른다. 임계를 정할 때 run 아티팩트를 열 것 |
| D8 | 성공 신호 ↔ **대조 팔 오염** | ⚠ 5종만(`7c90e08`·`5862289`) | ⚠ 러너 `compare_to` 필요 | 25종 미적용(§11c/§11d). **주입면이 baseline 경로 밖일 때만** 대조 팔을 쓸 수 있다 |
| D9 | **부하 `target_rps`** ↔ 그 부하를 하한으로 재는 감별자 | `test_load_floor_discriminators_sit_below_the_load_they_judge` (**이번 추가**, F06-P·F15-H는 열거된 기지 결함) | — | **부하를 바꾸는 커밋은 그 부하를 재는 감별자를 함께 연다.** `765e99f`가 40→5로 내리며 `lt 15`를 남겨 두 시나리오가 상시 실격이 됐다(§24-c) |
| D10 | 성공 신호 ↔ **그 성공의 부수 효과** | **없음** | ❌ | 자동 대조 불가 — "무엇이 성공에 뒤따르는가"는 앱 거동 지식이다. 실패율이 성공 신호인 9종은 완전 실패 → 타임아웃 → 처리량 붕괴가 **정상 경로**라 `achieved_rps` 하한에 스스로 걸린다(§24-b). **감별자를 쓸 때 "성공했을 때 이 값이 어디로 가는가"를 물을 것** |
| D12 | 성공 신호의 **transient / latch** 성질 ↔ `consecutive_ticks`·`min_hold` | **없음** | ⚠ 형태 분류는 가능 | 사다리 17종이 전부 transient를 쓰고 13종은 transient만 쓴다. `match: all`이라 **하나만 깜빡여도 streak이 0**이다(§25-b). latch 신호는 연속 요건이 사실상 무의미하고 transient 신호는 그 요건이 전부다 — **성공 조건을 설계할 때 두 성질을 섞었는지 볼 것.** 타이밍 값은 17종이 사실상 한 벌이라(settle 전부 30s, min_hold 10종이 4m) **복사된 값인지 고른 값인지** 함께 물어야 한다(§25-d) |
| D11 | **판정 로직 수리** ↔ 그 수리가 바꾸는 **노출 시간** | **없음** | ❌ | `e723d9b`(escalate streak 리셋)는 레벨 체류를 3.3분 → 8분으로 늘렸고, 길어진 창에서 F11-R이 전에 못 만나던 플랩을 두 번 만나 abort했다(§23-d). 수리 커밋 자신의 "판정 안 바뀜" 예측이 빗나갔다(§22-b) — **타이밍을 바꾸는 수리는 통과 이력 전체를 재검증 대상으로 만든다** |

## E. 정답지 (metadata)

| # | 쌍 | 가드 | 기계화 | 사람이 볼 것 |
|---|---|---|---|---|
| E1 | `injected_fault.levels` ↔ controllers levels | `test_stated_injection_matches_the_live_controller` | — | — |
| E2 | `companions` ↔ `companion_refs` ↔ `catalog.profiles` | `test_stated_companions_agree_with_controller_and_catalog` (`59c484c`) | — | — |
| E3 | `injection_summary`의 실행기명 ↔ 바인딩 | `test_injection_summary_names_a_profile_the_scenario_actually_binds` (`bd10144`) | — | — |
| E4 | 산문의 **나열형** 사다리 수치 ↔ 계약 | `test_prose_ladders_match_the_contract_ladder` (`f833e50`) | — | — |
| E5 | 산문의 **비나열형** 수치(주기·행수·용량·임계) ↔ 실제 | **없음** | ❌ 사실상 불가 | "10분마다"·"137만 행"·"실사용 124MiB" 같은 서술. 계약 밖 실측을 정당하게 인용할 수도 있어 자동 대조가 위험하다. **실제 사례**: F20-R "orders(5만 행)"이 실측 1,185,713행이었다(§19). **사람이 볼 때**: 자라는 데이터를 인용할 때, 그리고 **인과의 전제가 되는 사실을 바꾸는 커밋**을 낼 때 그 사실을 인용한 정답지를 함께 열 것 |
| E6 | `code_anchor` ↔ 실제 줄번호·심볼 | `test_code_anchors_still_name_a_symbol_that_lives_there` | — | — |
| E7 | 산문이 가리키는 설계 ↔ **현재 설계** | **없음** | ❌ | **원리적으로 불가.** 값이 아니라 접근이 낡는다 — F18-P가 폐기된 k8s.env 설계를 정답으로 가리키고 있었다(§19 유형 1). 재설계 커밋마다 사람이 산문을 다시 읽어야 한다 |

## F. 카탈로그 · 게이트 메타

| # | 쌍 | 가드 | 기계화 | 사람이 볼 것 |
|---|---|---|---|---|
| F1 | `catalog.readiness`·`load_mode` ↔ `test-scenarios.sh` 핀 | `test_shell_gate_catalog_pins_match_the_catalog` (**이번 추가**) | — | 핀은 거버넌스 결정이라 유지한다. 승격·강등·파킹 커밋과 핀 갱신은 **같은 커밋**에 |
| F2 | `catalog.profiles` ↔ `[primary]+companions` | `test_stated_companions_agree_with_controller_and_catalog` | — | — |
| F3 | `controllers.json` ↔ `controllers-parked.json` **서로소** | `test_parked_controllers_stay_out_of_the_live_registry` (**이번 추가**) | — | 승격 시 파킹 사본을 지우고 **산문도 함께** 갱신(F21-Q가 대기 중, §19) |
| F4 | 매니페스트 ↔ 레지스트리 | `generate-manifests.py --check` | — | 레지스트리를 고치면 재생성 |
| F5 | `bin/` 스크립트 ↔ catalog | ⚠ 개수 핀만 | ⚠ | 18개는 카탈로그 항목이 없고 14개는 스크립트가 없다(알려진 표류, 별도 백로그) |

## G. 배포 경계 — **레포 ↔ 109 ↔ 클러스터**

**오늘 두 번 물린 곳이고, 한쪽 레포의 테스트로는 원리적으로 못 잡는다.**

| # | 쌍 | 가드 | 기계화 | 사람이 볼 것 |
|---|---|---|---|---|
| G1 | 레포 `scripts/scenarios` ↔ 109 `/root/testbed-services/…` | **없음**(임시방편만) | ✅ **가능** | 오늘 두 번: F23-R `window_minutes` 12↔5, loadgen 접기(mtime 아흐레 전). 배포 스크립트에 F23-R 가드가 들어간 게 그 임시방편이다 |
| G2 | 레포 앱 코드 ↔ 클러스터 이미지 | **없음** | ⚠ | 이미지 태그가 전부 `:latest`라 무엇이 도는지 알 수 없다. `rollout restart` 없이 레지스트리만 바꾸면 조용히 어긋난다 |
| G3 | 러너 레포 HEAD ↔ 109 컨테이너 | **없음** | ✅ **가능** | 오늘 `c294d4a`가 109에 없음을 수동 확인했다(git log 대조) |

### 이 셋을 어떻게 잡을 것인가 — 제안

한 레포의 pytest로는 불가능하므로 **배포 시점 검증**이 자리다. 셋 다 같은 형태다:

1. **G1**: 배포 직전 `scripts/scenarios`의 파일 해시 목록을 레포와 109에서 각각 뽑아
   비교한다. 다르면 rsync가 선행돼야 한다는 뜻이다. 지금 배포 스크립트가 F23-R
   `window_minutes` 하나만 보는 것을 **디렉터리 전체 해시 비교**로 일반화하면 된다.
2. **G3**: 러너 컨테이너 안의 `git rev-parse HEAD`(또는 파일 해시)를 러너 레포 HEAD와
   비교. 오늘 수동으로 한 것을 스크립트로 옮기는 것뿐이다.
3. **C1**: `queries.json`의 `allowed_parameters`를 러너의 실제 allowlist와 비교하는
   **교차 검사 스크립트**. 두 레포를 동시에 볼 수 있는 곳(배포 호스트)에서 돌린다.

셋 다 이번에 만들지 않았다 — 배치·배포가 도는 중이라 범위를 통제했다. **G1이 비용
대비 효과가 가장 크다**(오늘 두 번 물린 곳이고 구현이 해시 비교뿐이다).

---

## 이번에 추가한 가드 (2개)

비용이 낮고 오늘 실제로 물린 것부터 골랐다.

- **F1** `test_shell_gate_catalog_pins_match_the_catalog` — `test-scenarios.sh`는
  `set -e`라 **첫 실패에서 죽어서** 낡은 핀이 08-06부터 세 번에 걸쳐 하나씩만 드러났다
  (`8d1ef6b`가 둘, `2b02a98`이 하나). pytest는 안 죽으므로 **어긋난 핀을 한 번에 전부**
  보고한다. 역검증했다 — 핀 둘을 되돌리면 둘 다 한 번에 잡힌다.
- **D9** `test_load_floor_discriminators_sit_below_the_load_they_judge` — `achieved_rps`
  하한 감별자가 그 시나리오 사다리 최저 단의 `target_rps`보다 아래인지 본다. **이미
  깨진 F06-P·F15-H는 `known_broken`에 열거**했다 — 침묵이 아니라 목록이고, 고치면
  집합에서 빼는 순간부터 재발을 막는다. 역검증했다(집합을 비우면 red).
- **F3** `test_parked_controllers_stay_out_of_the_live_registry` — 파킹 사본이 live
  레지스트리와 겹치면 두 벌이 따로 낡고 승격 때 낡은 쪽이 되살아난다. F21-Q가 이미 그
  위험을 안고 대기 중이다(파킹 사본은 `host.stress`인데 재설계는 `app.control`).

## 남은 것 — 우선순위

1. **G1 배포 사본 해시 비교** — 오늘 두 번 물렸고 구현이 가장 싸다.
1-b. ~~**B4 프로브 질의 실행 계획** 훑기~~ — **완료**(2026-08-07). 🔴 셋의 인덱스 DDL 제안은 §20에 있고 **배포 묶음** 대상이다. 이 점검은 표가 자라면 판정이 바뀌므로(오늘 orders 가 5만→119만) **주기적으로 다시 돌려야 한다.**
2. **B2 clickhouse 빈 창** — 미수리 결함이 남아 있다(§1b). 러너 레포 건.
3. **D8 대조 팔** — 25종 미적용. 러너 `compare_to`가 선행돼야 제대로 풀린다.
4. **D9 기지 결함 2종 해소** — F06-P·F15-H의 `load-not-delivered` 임계를 설계 부하에
   맞춰 내리고 `known_broken`에서 뺀다. 러너 변경 없이 되는 유일한 §24 수리지만
   **배치 완주 후에 한다** — 도는 배치의 계약을 중간에 가르지 않기 위해서이고, 또
   **이번 배치의 스킵 사유가 진단의 실증**이라 지금 고치면 확인 기회를 잃는다.
5. **D10 `achieved_rps` 하한 일반 수리** — 13종 노출. 분모(`checkout_count`)를 함께
   보거나 5xx가 높을 때 면제하는 형태인데, **두 신호를 한 조건에 묶으려면 러너 문법이
   필요**하다(D8의 `compare_to`와 같은 대기줄).
6. **E5 비나열형 산문 수치** — 자동 대조가 위험해서 설계가 필요하다.

## 이 문서 쓰는 법

- 새 드리프트를 고칠 때 **여기 줄을 추가한다.** 없던 쌍이었다는 뜻이다.
- 새 시나리오·프로파일·질의를 만들 때 **해당 마디의 줄을 훑는다.** "사람이 볼 것" 칸이
  체크리스트다.
- 가드를 추가하면 **기계화 칸을 갱신한다.**
- **형태로 후보를 만들되 형태로 판정하지 않는다.** `(observation, op)` 훑기는 노출을
  빠짐없이 세는 좋은 방법이지만 거기까지다. "주입이 성공하면 참이 되는 조건" 40건을
  분류했더니 **13건이 정당한 감별자**였다 — 일괄 수리했으면 F18-P의
  `transfer-pod-failure`(app.control 주입은 파드를 안 죽인다는 보호)를 없앴을 것이다.
  **한 줄씩 왜 거기 있는지 읽고 나서 고친다**(0807 §26).
- **완전성을 주장하지 않는다.** 이 목록이 다 찼다고 느끼는 순간이 가장 위험하다.
