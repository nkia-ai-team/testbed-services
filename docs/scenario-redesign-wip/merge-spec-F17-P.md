# 머지 스펙 — F17-P (commerce→transfer 직행 무결성 우회)

이 문서는 `docs/scenario-redesign-wip/design-F17-P-{sheet,manifest,metadata}.json`(정본)을
레지스트리(`scripts/scenarios/registry/*.json`)·러너(`rca-scenario-runner`)에 반입하기 위해
쓰기 권한을 가진 담당자가 적용해야 할 변경 조각이다. 이 브랜치의 격리 규칙상
`registry/*.json`·`profiles/*.py`·러너 `live_probes.py`/`observation_queries.json`은
읽기만 했으며, 실제 편집은 여기 없음 — 아래는 적용 스펙이다.

## 0. dual-arm 실현 방식 확정 (핵심 결론)

**executor 코드 변경 불필요.** `load_north_south_executor.py`(profiles/*.py, 읽기 전용 확인)를
읽어 검증한 결과, 이미 존재하는 두 스트림 집계 메커니즘을 그대로 재사용하면 dual-arm이 된다:

- `validate_parameters`는 파라미터 키가 정확히 9개(`target_rps, ramp_up, hold, ramp_down,
  entry_url, script_path, scenario_tag, seed, baseline_unit`)여야 한다(46-31행, exact-match).
  → 설계 매니페스트 draft의 `control_entry_url`/`frozen_from_account`/`closed_to_account`/
  `active_counterparty`/`amount_min`/`amount_max`는 executor 계약에 **넣을 수 없다**
  (넣으면 `set(parameters) != required`로 즉시 거부). 이 값들은 `surge.js`의
  `ACTIVE_ACCOUNTS` 관례와 동일하게 **스크립트 내부 상수**로 고정했다
  (`core-banking/loadgen/frozen-bypass.js` 참조 — FROZEN_FROM_ACCOUNT/CLOSED_TO_ACCOUNT/
  ACTIVE_COUNTERPARTY/AMOUNT_MIN/AMOUNT_MAX).
- remote_script(89-270행)는 k6를 **한 번만** 실행하고 `gateway_env=entry_url` 하나만
  주입한다 — 두 번째 URL을 계약으로 넘길 여지가 없다. 그래서 control arm URL(30082)은
  스크립트 내부 상수 `NORMAL_URL`(env override 가능하지만 executor가 넘기지 않음, surge.js의
  `GATEWAY_URL` 기본값 패턴과 동일)로 고정했다.
- **결정적 재사용 포인트**: remote_script에 내장된 monitor.py(134-245행)는 이미
  `business_step`/`read_step` 두 태그 스트림을 각각 독립 집계한다 —
  `business_2xx_rate/business_4xx_rate/business_5xx_rate`(business_step 태그) +
  `read_2xx_rate/read_nonok_rate`(read_step 태그). 이는 정확히 우리가 필요한 "두 경로,
  두 결과 분포"와 동형이다. 그래서:
  - direct arm(30282) → tag `step="frozen-direct"` = domain_profile.business_step
  - control arm(30082) → tag `step="frozen-control"` = domain_profile.read_step
  이렇게 태깅하면 **모니터 코드 수정 없이** 기존 필드로 바로 관측된다. 실제로 동일 패턴이
  이미 라이브: `loadgen.transfer_2xx_rate`(selector `business.2xx.rate`)가 banking
  entry_url 30082의 business_step=`transfer`를 그대로 쓴다(`queries.json` 확인, 읽기).

이 재사용 덕분에 설계시트 §4가 "신규 query_id 3종은 관측 배선이 전무하다"고 표시한 것 중
**2개(`frozen_bypass_completed_rate`, `normal_path_reject_rate`)는 신규 모니터 로직이 아니라
기존 selector를 새 query_id 이름으로 재노출하는 것으로 충분**하다. 진짜 신규는
`database.integrity_violation_count` 하나(§3).

*대안 검토*: 설계시트가 제시한 "전용 one-shot HTTP injector 프로파일 신설"은 필요 없다 —
위 재사용으로 기존 north_south 계약 확장만으로 충분히 표현 가능함을 확인했다.

**경로 불일치 발견**: 팀리드 지시문은 스크립트 경로를 `commerce/loadgen/frozen-bypass.js`로
표기했으나, 실제 인과사슬(§1)·k8s NodePort(30282)·기존 banking 스크립트 배치
(`core-banking/loadgen/surge.js`, `core-banking/loadgen/slowquery.js`)는 모두 core-banking
쪽이고, 매니페스트 `profile.levels[0].parameters.script_path`도
`/opt/loadgen/core-banking/frozen-bypass.js`로 명시돼 있다. 정본(매니페스트)을 따라
**`core-banking/loadgen/frozen-bypass.js`**로 생성했다(지시문 표기는 오기로 판단).

## 1. `scripts/scenarios/registry/profiles.json` — `profiles["load.north_south"].parameter_contract`

```jsonc
{
  "allowed_scenarios": [/* ...기존... */, "F17-P"],
  "allowed_entry_urls": [
    "http://192.168.122.77:30080",
    "http://192.168.122.77:30181",
    "http://192.168.122.77:30082",
    "http://192.168.122.77:30282"        // 신규 — direct 우회 진입점
  ],
  "allowed_script_paths": [
    /* ...기존... */,
    "/opt/loadgen/core-banking/frozen-bypass.js"   // 신규
  ],
  "allowed_baseline_units": [/* 기존 그대로 — loadgen-banking 재사용 */],
  "domain_profiles": {
    /* ...기존 3개 그대로... */,
    "http://192.168.122.77:30282": {
      "baseline_unit": "loadgen-banking",
      "health_path": "/actuator/health",
      "gateway_env": "TRANSFER_DIRECT_URL",
      "business_step": "frozen-direct",
      "read_step": "frozen-control"
    }
  },
  "target_rps": { "minimum": 1, "maximum": 180 },
  "duration_pattern": "^[1-9][0-9]*[sm]$",
  "tag_pattern": "^scenario_id=F(...|17-P)$"   // 기존 alternation에 17-P 추가
}
```

`profile.scenario_levels["F17-P"]`(또는 controllers.json의 fixed profile 조각, §4 참조)에
바인딩되는 파라미터는 **정확히 9키**여야 한다:

```json
{
  "target_rps": 20,
  "ramp_up": "1m",
  "hold": "8m",
  "ramp_down": "1m",
  "entry_url": "http://192.168.122.77:30282",
  "script_path": "/opt/loadgen/core-banking/frozen-bypass.js",
  "scenario_tag": "scenario_id=F17-P",
  "seed": 4242,
  "baseline_unit": "loadgen-banking"
}
```

design-F17-P-manifest.json 초안의 `control_entry_url`/`frozen_from_account`/
`closed_to_account`/`active_counterparty`/`amount_min`/`amount_max` 필드는 **executor
파라미터에서 제거**하고 metadata 문서용 참고 정보로만 남긴다(실값은 frozen-bypass.js 상수와
반드시 일치 — AMOUNT_MIN=1000/AMOUNT_MAX=5000/ACC-9001/ACC-9002/ACC-1001).

## 2. `scripts/scenarios/registry/queries.json` (+ 러너 `observation_queries.json` 동일 반영)

두 파일의 스키마가 다르다는 점에 주의 — `queries.json`은 `value_type`을 갖고
`allowed_parameters`를 생략 가능한 항목도 있으나(`database.tagged_session_count` 등은
있음), 러너 `observation_queries.json`은 `value_type`이 없고 `allowed_parameters`가
없으면 빈 배열 `[]`로 채워야 한다(`loadgen.transfer_2xx_rate`/`database.outbox_unpublished_count`
실측 확인). 양쪽에 각각 맞는 형태로 반입:

`scripts/scenarios/registry/queries.json`:
```jsonc
"loadgen.frozen_bypass_completed_rate": {
  "adapter": "loadgen_summary",
  "selector": "business.2xx.rate",     // 기존 필드 재사용 — direct arm(step=frozen-direct)의 2xx율
  "value_type": "number",
  "freshness_sec": 30
},
"loadgen.normal_path_reject_rate": {
  "adapter": "loadgen_summary",
  "selector": "read.nonok.rate",       // 기존 필드 재사용 — control arm(step=frozen-control)의 4xx+율
  "value_type": "number",
  "freshness_sec": 30
},
"database.integrity_violation_count": {
  "adapter": "database",
  "selector": "banking.integrity_violation_count",   // 신규
  "allowed_parameters": ["since"],
  "value_type": "integer",
  "freshness_sec": 30
}
```

`rca-scenario-runner/backend/app/observation_queries.json` (동일 3종, `value_type` 없이):
```jsonc
"loadgen.frozen_bypass_completed_rate": {
  "adapter": "loadgen_summary",
  "selector": "business.2xx.rate",
  "freshness_sec": 30,
  "allowed_parameters": []
},
"loadgen.normal_path_reject_rate": {
  "adapter": "loadgen_summary",
  "selector": "read.nonok.rate",
  "freshness_sec": 30,
  "allowed_parameters": []
},
"database.integrity_violation_count": {
  "adapter": "database",
  "selector": "banking.integrity_violation_count",
  "freshness_sec": 30,
  "allowed_parameters": ["since"]
}
```

주의: `business_2xx_rate`는 순수 HTTP 200 비율이라 body의 `status=="COMPLETED"` 자체를
구분하지 않는다. 이 시나리오에서는 AMOUNT_MAX(5000)가 FROZEN 계좌 잔액(100000)보다 항상
작아 balance 검사(TransferService.java:77)로 인한 `FAILED`(200 응답 내부값)가 발생할 수
없으므로 실질적으로 안전한 근사다 — 하지만 엄밀히는 "200 ⇒ COMPLETED"를 코드가 보장하는
것이지 모니터가 body를 파싱하는 것은 아니라는 점을 승격 문서에 명시해야 한다(must_rule_out
근거 목록에 "AMOUNT_MAX < FROZEN 잔액이므로 FAILED-on-200 불가능" 한 줄 추가 권고).

## 3. `database.integrity_violation_count` 구현 (러너 `live_probes.py`, `_database_observation`)

`database.outbox_unpublished_count`(905-918행)와 동일한 kubectl exec + sqlplus 패턴을 따른다
(테이블/컬럼명은 `core-banking/db/init.sql:15-40` 실측 확정: `accounts.status`,
`transfers.from_account/to_account/status/created_at`, 스키마 접두 `banking.`):

```python
if query.query_id == "database.integrity_violation_count":
    since = query.parameters.get("since")  # ISO8601, F06 패턴(_f06_pulse_state)처럼 run 시작시각 바인딩
    if not since:
        raise LiveProbeError("integrity violation query requires 'since'")
    result = self._kubectl(
        "exec", "testbed-oracle-0", "--namespace", "rca-testbed-banking", "--",
        "sh", "-lc",
        "printf 'alter session set container=FREEPDB1;\\n"
        "set pages 0 feedback off heading off\\n"
        "select count(*) from banking.transfers t join banking.accounts a "
        "on a.id in (t.from_account, t.to_account) "
        "where a.status in (''FROZEN'',''CLOSED'') and t.status=''COMPLETED'' "
        f"and t.created_at >= to_timestamp(''{since}'', ''YYYY-MM-DD HH24:MI:SS'');\\n"
        "exit;\\n' | sqlplus -s / as sysdba",
    )
    raw = result.stdout.strip()
    if not re.fullmatch(r"[0-9]+", raw):
        raise LiveProbeError("integrity violation count is invalid")
    return int(raw), _aware(self.clock()), "database:integrity-violation-count"
```

`since`는 F06 계열이 쓰는 `_f06_pulse_state`/run-start-timestamp 바인딩 관례를 그대로 따라
"이 실행 윈도우 이후에 생긴 위반만" 센다 — 과거 calibration/실주입 잔재가 count를
오염시키지 않게 하기 위함(§4 cleanup과도 연결).

## 4. cleanup — 정정 방법

direct arm이 만든 FROZEN/CLOSED 계좌 관여 `COMPLETED` transfers는 원장에 실제로 남는다
(부정합 mutation). cleanup은 다음을 반드시 수행:

```sql
-- direct arm이 생성한 위반 transfer의 잔액을 원복(역이체)하고 상태를 REVERSED로 마킹.
-- scenario_tag로 이 실행에서 생긴 행만 특정한다 — orderId는 null이라 사용 불가,
-- created_at >= :run_started_at AND (from_account/to_account IN (ACC-9001, ACC-9002))로 특정.
UPDATE banking.accounts SET balance = balance + :amount WHERE id = :from_account_of_violation;
UPDATE banking.accounts SET balance = balance - :amount WHERE id = :to_account_of_violation;
UPDATE banking.transfers SET status = 'REVERSED' WHERE id IN (:violation_transfer_ids);
```

이 SQL은 애플리케이션 트랜잭션 밖에서 직접 원장을 건드리므로 **cleanup 스크립트가 반드시
run 시작 시각 이후·ACC-9001/ACC-9002 관여 행만 정확히 특정**해야 한다(범위를 넓히면 다른
동시 실행 시나리오 데이터까지 건드릴 위험). controller의 `cleanup.required=true`는 이미
manifest에 있음 — 여기에 위 SQL을 tb-runner cleanup 단계(north_south executor의
`cleanup` action 이후, database 어댑터 쪽 별도 스텝)로 연결해야 한다. **live 승격은 이
cleanup이 실제로 구현·검증된 뒤로 미룬다**(manifest `live_enabled=false`가 정직한 이유).

## 5. catalog/metadata/controllers 승격 조각

계약 필수사항은 매니페스트에 이미 확정돼 있으므로 그대로 반입:

- `controllers.json`: `dispatcher_mode="trusted"`, `live_enabled=false`,
  `runtime.tick_interval="15s"`, `runtime.settle_after_change="30s"`,
  `baseline.required` 4종(coordinator-clean/clean-window/baseline-traffic/target-health),
  `capture` 정본 정책(`focused-window-v1`, pre_window 10m/post_window 20m),
  `abort.any=[{entry_status eq 0}]` consecutive_ticks 2,
  `recovery.all=[{target_health eq 200},{frozen_bypass_completed_rate eq 0}]` consecutive_ticks 2.
  fixed level 파라미터는 §1의 9키와 정확히 일치해야 한다(그 외 필드는 controller
  `profile.levels[].parameters`가 아니라 scenario metadata 참고용 주석으로만 보관).
- `catalog.json`: id=F17-P, class=A, fault_pattern=P7, domain=cross, readiness=draft
  (§6 gate 해소 전까지 유지), injection.profile_refs=["load.north_south"].
- `scenario-metadata.json`: design-F17-P-metadata.json 본문 그대로 반입(문구 변경 없음).

## 6. 승격 게이트 (readiness → live 전환 조건)

1. §1~§3 레지스트리/러너 반영 + dry-run으로 `frozen_bypass_completed_rate`,
   `normal_path_reject_rate`, `integrity_violation_count` 세 관측이 실제로 값을 내는지 검증.
2. §4 cleanup SQL 구현·검증(원장 정정이 실제로 동작 — 별도 스테이징 실행으로 확인).
3. 위 2개 통과 후에만 `live_enabled=true`/`prerequisite_gate.state=cleared`로 전환.

## 7. test 기대값 delta

`tests/test_registry_contracts.py` 등 registry 계약 테스트(읽기 전용 확인)에 다음 델타가
필요하다 — 실제 편집은 쓰기 권한 보유자가 수행:
- `allowed_scenarios`/`tag_pattern`에 F17-P가 포함됐는지 확인하는 테스트 케이스 추가.
- `allowed_entry_urls`에 30282, `domain_profiles`에 그 항목이 존재하는지 확인.
- F17-P의 fixed level 파라미터 키 집합이 정확히 9개(초과 키 없음)인지 검증하는 회귀
  테스트 — 이 시나리오가 §0에서 지적한 "매니페스트 draft 초과 필드" 실수를 반복하지
  않도록 하는 방지책.
- `database.integrity_violation_count`/`loadgen.frozen_bypass_completed_rate`/
  `loadgen.normal_path_reject_rate`가 `queries.json`과 러너 `observation_queries.json`
  양쪽에 동일하게 등록됐는지 대조하는 테스트(기존 파일 간 동기 테스트 패턴이 있다면 재사용).

## 8. 생성 파일

- `core-banking/loadgen/frozen-bypass.js` (신규 — dual-arm k6 스크립트)
- `docs/scenario-redesign-wip/merge-spec-F17-P.md` (본 문서)
