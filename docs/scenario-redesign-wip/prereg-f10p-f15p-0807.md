# 사전 등록 — F10-P·F15-P 판독 절차 (2026-08-07)

**런이 나오기 전에 확정한다.** 데이터를 보고 기준을 고르면 예측이 무의미해진다.
이 파일의 커밋 시각이 두 런의 `t1`보다 앞서는 것이 이 문서의 구속력이다.

각 항목에 **확인 / 반증 / 판정 불가** 조건을 셋 다 미리 적는다.

---

## 공통 규약

**출처**: `/root/rca-scenario-runner/runs/<RUN_ID>/` — `ticks.jsonl`(신호 시계열), `result.json`(t1/t2/level_changes),
`decisions.json`(reason·streaks), `plan.json`(gates·approved_levels).

**창 규칙**
- **BEFORE** = 직전 런의 `t2` 이후 ~ 이 런의 `t1`. 직전 런은 전 런 `t1` 오름차순의 바로 앞.
  **`overlapping_run_ids`는 쓰지 않는다** — clean-window 목록이지 동시 실행이 아니다(F03-H에서 확인).
- 원인 판단은 **`phase == "evaluating"` 틱만**. settling·aborted·recovery·dirty는 결과이지 원인이 아니다.
- 사다리 시나리오이므로 **레벨별로 나눠 읽는다**(`result.json.level_changes[].applied_at`/`effect_ended_at`).
- **시각은 UTC 통일.** 디렉터리 mtime은 KST이므로 쓰지 않는다.

**사다리 파라미터 버전 병기 (제약 (1) 이행)**
`plan.json → profile_instances[profile_id=="host.stress"].approved_levels[].parameters` 전문을 결과에 병기.
`git log -S` 로 사다리 변경 커밋을 찾아, 변경이 이 런 `t1`보다 앞이면 **그 이전 실측은 비교 불가**로 표기.
(`32c4308`이 F05-P·F25-H·F03-H 사다리를 08-07 07:52에 바꿨다.)

**`usable` 처리**: `usable == false` 틱은 **그 신호에 한해** 제외(틱 전체를 버리지 않음). 제외 비율 병기.

---

## A. F10-P

> **예측**: `must_rule_out` 중단이 아니라 **`succeeded`로 판정되며, 그 성공이 주입이 아니라
> 부하(`core-banking/surge.js:82` 무제한 조회) 때문인 거짓 양성**이다.

```
success:        transfer_2xx_rate  lt 0.5   (all)
must_rule_out:  achieved_rps       gt 60    (any)
사다리: io-6000 / io-12000 / io-20000  (host.stress, fio, host=192.168.122.11=tb-w2,
        target_dir=…oracledata-testbed-oracle-0, size_mib=2048, rate_iops=6000/12000/20000)
```

### A-1. "급등"의 정의 (확정)
`disk_io_util` 기준, 두 잣대를 **함께**:

| 잣대 | 정의 |
|---|---|
| **절대** | `>= 80` 인 evaluating 틱이 **3틱 이상** |
| **배수** | `median(DURING) / median(BEFORE) >= 3.0` |

- **BEFORE 표본 5틱 미만이면 배수 잣대를 쓰지 않는다**(F18-P·F03-H에서 n=3~13이었다). 절대 잣대만으로 판정하고 "BEFORE 표본 부족" 명기.
- 둘 다 참 → 급등 확정 / 하나만 → 부분 급등(불확실성 표기) / 둘 다 거짓 → 급등 없음

⚠ `disk_io_util`의 단위가 백분율이 아니면 임계의 **의미**("장치가 사실상 포화")를 고정하고 숫자를 환산한다.
**환산을 데이터 본 뒤에 하는 것이므로 사전 등록의 강도가 그만큼 약해진다 — 정직하게 표기할 것.**

### A-2. 선후 판정 (확정)
- `T_disk` = `disk_io_util`이 **처음** 80을 넘은 evaluating 틱의 `elapsed_sec`
- `T_hikari` = `transfer_hikari_pending`이 **처음** `max(1, 2 × median(BEFORE))`를 넘은 틱의 `elapsed_sec`
- 틱 간격 15~16초 → **`|T_disk − T_hikari| <= 16`이면 "동시"**(한 틱 차는 순서로 읽지 않는다)

| 결과 | 해석 |
|---|---|
| `T_disk < T_hikari − 16` | 디스크가 먼저 → **주입이 원인** |
| `T_hikari < T_disk − 16` | 풀 경합이 먼저 → **부하가 원인** |
| `|차| <= 16` | **선후 판정 불가** |

### A-3. 보조 판별식 — `account` 대조
주입은 **Oracle PVC 디스크**를 때리고 `banking` 스키마의 transfer·account가 **같은 인스턴스**를 공유한다.
- 주입이 원인이면 **account도 함께 느려진다**
- 부하가 원인이면 **account는 멀쩡하다** — F18-P 실측: transfer가 502를 쏟는 동안 `/api/accounts*`는 거의 전량 200

F10-P에 `account_p95` 관측이 없으므로 트레이스로:
`service_name IN ('core-banking-transfer','core-banking-account'), span_kind='SERVER'` → 3구간 p95.

### A-4. 판정 규칙 (확정)

| # | 조건 | 판정 |
|---|---|---|
| 1 | 급등 확정 AND `T_disk < T_hikari − 16` AND account 동반 상승 | **주입이 원인. 예측 반증.** 그대로 보고 |
| 2 | 급등 없음 AND `transfer_2xx_rate < 0.5` 성립 | **거짓 양성 확정. 예측 확인** |
| 3 | 급등 없음 AND transfer만 상승·account 평온 | **거짓 양성 확정(강)** — 두 독립 근거 |
| 4 | 부분 급등 또는 선후 "동시" | **판정 유보.** "주입과 부하가 함께 기여, 분리 불가". 단정 금지 |
| 5 | `disk_io_util` unusable ≥ 50% | **판정 불가 — 게이지 문제.** §M에 추가 |
| 6 | `transfer_hikari_pending` 전 틱 0 | 풀 경합 없음 → 제3의 원인. **미규명으로 남기고 별도 조사 제안** |
| 7 | `succeeded`가 아니라 중단 | **예측 빗나감.** 일곱 계열로 분류하고 왜 틀렸는지 적는다 |

### A-5. 명시적 반증 조건
> **디스크가 먼저 급등하고 hikari가 뒤따르면(규칙 1) 주입이 진짜 원인이고 거짓 양성 가설은 틀린 것이다.**
> 그 경우 "예측 반증"이라 분명히 쓰고, §B(부하) 시급성은 F18-P·F15-P 쪽으로만 남는다고 보고한다.

### A-6. 참조 기준선
F18-P에서 **부하만으로** `transfer_2xx_rate` 0.30~0.49 실측. F10-P의 2xx가 이 범위면 부하로 설명 가능하다는
**보조** 근거로만 쓴다(단독 근거 금지).

---

## B. F15-P

> **예측**: `node_mem_util`이 blind라 실제 압박이 게이지에 안 잡혀 success `>= 75`가 서지 않는다.
> **단, F05-P(임계 92)보다 임계가 낮으므로 통과할 수도 있다** — 이 갈림길을 미리 명시한다.

```
success:        node_mem_util gte 75 / transfer_p95 gte 4000 / account_p95 gte 1000
must_rule_out:  node_mem_util lt 60 ; achieved_rps gt 60 ; node_ready eq false
escalate:       node_mem_util lt 75
사다리: pressure-cpu2-vm1x1500m / cpu3-vm1x3000m / cpu4-vm2x2500m (host.stress, pressure, tb-w2)
```
※ success의 `all`/`any` 조합은 판독 시 `plan.json`에서 **원문 그대로 인용**해 재확인.

### B-1. 게이지 결함 — 확인·반증 조건 (확정)
같은 창(레벨별 evaluating)에서:
```
A(blind)  max without(grade) (kcm.node.mem_utilization{node="tb-w2"})
B(참값)   max without(grade) (kcm.node.system_mem_utilization{node="tb-w2"})
검증      mem_usage / mem_capacity * 100
```
(F05-P에서 B와 검증식이 소수점까지 일치했다. 이번에도 일치하는지 먼저 본다 — 불일치면 B의 신뢰성부터 재검토.)

| 결과 | 판정 |
|---|---|
| `max(A) < 75` AND `max(B) >= 75` | **예측 확인.** 게이지가 blind라 판정이 막혔다 |
| `max(A) >= 75` | **예측 반증.** 게이지가 임계를 넘겼다 |
| `max(A) < 75` AND `max(B) < 75` | **예측 반증(다른 방향).** 게이지가 아니라 **주입 강도 부족** → 사다리 재설계 항목으로 이동 |
| A ≈ B | **예측 반증.** tb-w2에서는 blind가 발현 안 함 → 노드별 차이를 조사 |

### B-2. 두 결함 분리 — `transfer` 대 `account` (핵심)
근거: F15-P 자신의 `must_support`("같은 노드의 앱이 **함께** 지연 — 한 서비스가 아니다") + F18-P 실측
(무제한 조회는 transfer만 때리고 account는 안 건드린다).

| `transfer_p95` | `account_p95` | 해석 |
|---|---|---|
| 상승 | **상승** | **노드 자원 고갈(주입)** — 설계 의도대로 |
| 상승 | **평온** | **무제한 조회(부하)** — `surge.js:82` 오염 |
| 상승 | 상승하되 transfer가 훨씬 큼 | 둘 다 기여. **분리 불가** 표기 + 배수 병기 |

**정량 기준(확정)**: `account_p95`의 `median(DURING)/median(BEFORE) >= 2.0`이면 "상승", `< 1.3`이면 "평온", 사이는 "모호".
보조: `node_cpu_util`·참값 메모리 동반 여부. 노드 자원이 평온한데 transfer만 느리면 부하 원인이 굳어진다.

### B-3. 판정 규칙 (확정)

| # | 조건 | 판정 |
|---|---|---|
| 1 | B-1 확인 AND B-2 "주입 원인" | **예측 확인.** 게이지 수리(§M-1)만으로 살아날 가능성 큼 |
| 2 | B-1 확인 AND B-2 "부하 원인" | **두 결함 동시 작동.** §M-1 **과** §B 둘 다 선행 필요 |
| 3 | B-1 "주입 강도 부족" | **예측 반증.** §K에서 사다리 재설계로 이동 |
| 4 | `succeeded` | **예측 반증.** blind가 임계 75를 막지 못했다 — F05-P(92)와 임계 차이가 갈랐다 |
| 5 | `must_rule_out: node_mem_util < 60` 발화로 중단 | **blind의 반대 방향 피해** — 낮게 보여 "압박 없음"으로 실격. 예측의 변형 |
| 6 | 그 외 | 일곱 계열 분류부터. 예측은 **판정 보류** |

### B-4. 명시적 반증 조건
> **blind 값이 75를 넘겨 success가 서면 예측은 틀린 것이다.** F05-P가 막힌 것은 blind 때문이 아니라
> 임계 92가 blind 값의 도달 범위 밖이었기 때문이고 75는 그 안이었다는 뜻이 된다.
> 그 경우 §M-1의 시급성은 F05-P 하나로 줄어든다.

---

## C. 한 장 요약

| | F10-P | F15-P |
|---|---|---|
| **예측** | 거짓 양성 `succeeded` | blind 게이지로 실패 |
| **1차 판별식** | `disk_io_util` 급등(절대 ≥80 3틱 / 배수 ≥3.0) | `mem_utilization` 대 `system_mem_utilization` 괴리 |
| **2차 판별식** | `T_disk` 대 `T_hikari` 선후(±16s 동시) | `transfer_p95` 대 `account_p95`(≥2.0 / <1.3) |
| **보조** | account 트레이스 p95 동반 상승 | `node_cpu_util`·참값 메모리 동반 |
| **반증 조건** | 디스크 먼저 급등 + account 동반 | blind 값이 75 초과 |
| **판정 불가** | 부분 급등 / 선후 동시 / unusable ≥50% | A≈B / 배수 1.3~2.0 모호 |

**모든 판독에 병기**: 런 id, `t1`/`t2`(UTC), 레벨별 구간, **사다리 파라미터 전문**, 사다리 변경 커밋 유무,
신호별 unusable 비율, BEFORE 표본 크기.

---

## D. 이 절차의 한계 (미리 밝힌다)

- **`disk_io_util`의 단위와 정상 범위를 모른다.** 절대 임계 80은 백분율 가정이다.
  다르면 A-1 환산 규칙을 따르되 **환산을 데이터 본 뒤에 하므로 사전 등록 강도가 약해진다.**
- F15-P success의 `all`/`any` 조합을 원문 확정하지 않았다. 판독 시 `plan.json`에서 인용한다.
- 두 시나리오 모두 **calibration 모드일 가능성**이 있다. 그러면 사다리를 끝까지 올리므로 레벨별 판독이 필수이고,
  `evaluation` 모드와 "성공"의 의미가 다르다는 점을 결론에 반영한다.
