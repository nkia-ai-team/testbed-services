# 머지 스펙 — F23-R: commerce 재입고 배치 정지 → 재고 소진 → checkout 409 조용한 기능마비

정본: `design-F23-R-sheet.md` + `design-F23-R-manifest.json` + `design-F23-R-metadata.json` (draft/blocked). 본 문서는 ③ 관측 3종 배선과 ④ env stopgap 주입을 **실물 배관 계약**으로 확정하고, manifest의 1개 편차를 교정한다. 격리 규칙에 따라 registry/*.json·tests/*·profiles/*.py·러너 live_probes.py·observation_queries.json·execution-matrix.md는 이 문서에만 조각을 기술하고 직접 편집하지 않았다.

---

## 0. 앵커 재검증 결과 (전부 read 확인, 시트와 합치)

- `ReconciliationBatch.java:46` `@Scheduled(fixedDelayString = "${inventory.reconciliation.interval-ms:600000}")`, `:73-91` RESTOCK 루프(threshold=20 생성자 주입 기본값, target=200), `:16-25` 파일 주석의 자립성 버그 기술 — 시트와 100% 일치(시트의 `:74-91`은 실제 `:73-91`, 사소한 오프바이원, 내용 동일).
- `InventoryService.java:79-83` `stock < quantity → 409 CONFLICT`, `:75` `findByProductIdForUpdate` — 정확 일치.
- `ProductClient.java`(order-service) `:25` `@CircuitBreaker`, `:36-41` 4xx→`ClientErrorException`(CB 미집계, 주석 `:37`), fallback `:49-51`도 4xx는 그대로 rethrow — 정확 일치.
- `InventoryClient.java`(product-service) `:23-36` getStock, `:38-51` reserve — 409를 `HttpStatus.valueOf`로 그대로 전파. 시트의 `:33,48`은 getStock의 catch(:32-35)·reserve의 catch(:47-50) 라인과 근사 일치(±1행, 내용 동일).
- `schema.sql`(inventory-service) 확인: `inventory_schema.inventory(product_id, stock, reserved, updated_at)`, `inventory_schema.inventory_movements(product_id, movement_type, quantity, resulting_stock, created_at)`. **manifest에 명시된 테이블·컬럼명이 실제 스키마와 일치** — 추가 확인 불필요.
- loadgen: `script.js:227-230`(`checkout 200/400/409` pass), `surge.js:131-132`(`checkout not 5xx` pass) — 409가 실패로 집계되지 않음을 확인. 즉 **현재 어떤 k6 메트릭도 409만 따로 분리해 카운트하지 않는다**(체크 자체가 409를 성공 조건에 포함).
- `testbed-inventory` 배포는 `commerce/k8s/22-inventory-service.yaml:9` `replicas: 1`, 전략 명시 없음(기본 RollingUpdate, maxSurge=1/maxUnavailable=0 상당) — §4 참고.

결론: 코드 근거는 시트 그대로 유효. 아래는 순수 배관 설계.

---

## 1. 관측 query 3종 — 실현 방식

### 1-A. `database.inventory_stock_level` — **신규 필요** (기존 재사용 불가)

기존 `database.index_present`와 동일한 "고정 SQL + 파라미터 완전일치 계약" 패턴을 그대로 복제한다(신규 selector 문자열만 다름 — index_present처럼 스키마/테이블을 세션변수로 파라미터화할 필요 없음, 이 selector는 F23-R 전용 단일 소비자라 SQL에 테이블명을 직접 박아도 안전).

**`scripts/scenarios/registry/queries.json` 추가 조각** (읽기전용 파일이므로 이 스펙에만 기술 — 반입 시 team-lead가 편집):
```json
"database.inventory_stock_level": {
  "adapter": "database",
  "selector": "commerce.inventory.zero_stock_count",
  "allowed_parameters": ["db_host", "db_port", "db_name", "db_user", "schema", "table"],
  "value_type": "integer",
  "freshness_sec": 30
}
```

**러너 `backend/app/live_probes.py` `_database_observation` 추가 분기** (신규 상수 + 분기, `database.index_present` 분기 바로 아래):
```python
INVENTORY_STOCK_CONTRACT = {
    "db_host": "192.168.122.77", "db_port": 30432, "db_name": "commerce",
    "db_user": "commerce", "schema": "inventory_schema", "table": "inventory",
}
INVENTORY_ZERO_STOCK_SQL = (
    "SELECT count(*) AS zero_stock_count FROM inventory_schema.inventory WHERE stock = 0"
)
# ...
if query.query_id == "database.inventory_stock_level":
    if dict(query.parameters) != INVENTORY_STOCK_CONTRACT:
        raise LiveProbeError("inventory stock target is not allowlisted")
    response = self.database_client(INVENTORY_ZERO_STOCK_SQL, (), credentials=self.database_credentials)
    count = response.get("zero_stock_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise LiveProbeError("inventory zero-stock count is invalid")
    return count, _response_time(response, self.clock), "database:inventory-zero-stock-count"
```
값 의미론: **재고=0인 상품 수**(manifest의 `agg: count_zero_stock`과 일치). 정상 소진(톱니파)이면 배치 10분 주기로 0으로 복귀, 배치정지면 단조 증가 후 정체. `success.stock-depleted: >= 1`, `recovery.stock-replenished: == 0` — manifest 그대로 유효, 수정 불필요.

같은 PG 인스턴스(192.168.122.77:30432, db=commerce)를 쓰므로 **신규 자격증명 불필요**(`database.index_present`가 이미 쓰는 `database_credentials` 재사용).

### 1-B. `database.restock_movement_rate` — **신규 필요**

```json
"database.restock_movement_rate": {
  "adapter": "database",
  "selector": "commerce.inventory.restock_movement_count",
  "allowed_parameters": ["db_host", "db_port", "db_name", "db_user", "schema", "table", "movement_type", "window_minutes"],
  "value_type": "integer",
  "freshness_sec": 60
}
```
```python
RESTOCK_MOVEMENT_CONTRACT = {
    "db_host": "192.168.122.77", "db_port": 30432, "db_name": "commerce",
    "db_user": "commerce", "schema": "inventory_schema", "table": "inventory_movements",
    "movement_type": "RESTOCK", "window_minutes": 12,
}
RESTOCK_MOVEMENT_SQL = (
    "SELECT count(*) AS restock_count FROM inventory_schema.inventory_movements "
    "WHERE movement_type = 'RESTOCK' AND created_at >= now() - interval '12 minutes'"
)
if query.query_id == "database.restock_movement_rate":
    if dict(query.parameters) != RESTOCK_MOVEMENT_CONTRACT:
        raise LiveProbeError("restock movement target is not allowlisted")
    response = self.database_client(RESTOCK_MOVEMENT_SQL, (), credentials=self.database_credentials)
    count = response.get("restock_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise LiveProbeError("restock movement count is invalid")
    return count, _response_time(response, self.clock), "database:restock-movement-count"
```
window 고정 12분(배치 주기 10분보다 여유 있게) — 배치가 살아있으면 매 tick마다 최근 12분 창에 RESTOCK row가 최소 1건 이상 잡힌다. `success.restock-halted: == 0`, `recovery.restock-resumed: > 0` — manifest 그대로 유효.

**F19-Q와의 관계 정정**: 시트가 "F19-Q `database.dispatch_delivered_rate`와 동형"이라 했지만, 실측 결과 **그 query는 registry/queries.json에도 아직 존재하지 않는다**(F19-Q 자체가 draft/blocked, 레지스트리 미반입). 즉 F23-R의 이 두 query는 선례를 잇는 게 아니라 **F23-R이 이 패턴의 최초 반입자**다. 순서상 문제는 없으나(둘 다 독립적으로 유효한 설계), "동형 재사용"이 아니라 "동형 신설"로 정정한다.

### 1-C. checkout 409 rate — **manifest 편차 발견, 정정 필요** (신규 query, 파라미터 없음)

**발견한 편차**: manifest(`design-F23-R-manifest.json:117-118`)는 `loadgen.checkout_status_rate`를 `parameters: {step: "checkout", status_class: "409"}`로 정의했으나, 러너의 `_loadgen_observation` 실제 계약(`live_probes.py:451-456` 이하 방금 재확인)은:
```python
if query.parameters or query.query_id not in {...}:
    raise LiveProbeError("unsupported loadgen query")
```
— **loadgen_summary 어댑터는 파라미터를 아예 받지 않는다.** 기존 5개 query_id(achieved_rps·checkout_5xx_rate·write/read_step_status_rate·food_create_status_rate·transfer_2xx_rate) 전부 **1:1 고정 필드 매핑**이고, `step`/`status_class` 같은 파라미터화는 어댑터에 존재하지 않는다. **team-lead 질의에 대한 답**: `business_nonok_rate`(write_step_status_rate)로 409를 볼 수 있는지 검토한 결과 — **재사용 불가, 최소화 아닌 신규 query 필요**. 이유:
1. `business_nonok_rate`는 checkout 스텝에 한정되지 않고 write 계열 전체(주석상 400 쿠폰 오류 포함)를 묶어, 재고소진(409)과 쿠폰오류(400)를 구분 못 한다 — F23-R 감별에 필수인 "409만" 신호가 아니다.
2. 따라서 기존 `business_4xx_rate` 계열 재사용은 기각 — **신규 query_id 신설**로 최소화 원칙을 지키되, 파라미터화 없이 어댑터 계약에 맞춰 설계한다.

**정정된 설계** — `loadgen.checkout_409_rate`(파라미터 없음, 이미 머지된 2026-07-23 status-class 일반화 patch의 `field.endswith("_rate")` 검증을 그대로 탄다):

```json
"loadgen.checkout_409_rate": {
  "adapter": "loadgen_summary",
  "selector": "checkout.409.rate",
  "allowed_parameters": [],
  "value_type": "number",
  "freshness_sec": 30
}
```
```python
# _loadgen_observation의 allowlist set에 추가:
"loadgen.checkout_409_rate",
# field map에 추가:
"loadgen.checkout_409_rate": "checkout_409_rate",
```
range check는 기존 `field.endswith("_rate")` 일반화(07-23 patch, 이미 러너 main에 병합됨 — README 확인: status-class=d70effc)가 그대로 커버 — **코드 추가 없이 allowlist 2줄만 추가**.

**k6 쪽 신규 작업(미해결 — 이 스펙의 범위 밖이지만 명시)**: `checkout_5xx_rate`가 어디서 계산돼 `/app/state/loadgen/latest-summary.json`(또는 tb-runner의 `/tmp/rca-scenario-{id}-live.json`)에 채워지는지의 **실제 변환기(exporter) 위치를 이 저장소·러너 저장소 어디에서도 찾지 못했다** — k6 스크립트(`script.js`/`surge.js`) 자체에는 `handleSummary()`가 없고, 두 레포 전체 grep으로도 `checkout_5xx_rate`/`business_nonok_rate` 필드를 실제로 계산하는 코드가 나오지 않는다(선언은 `live_probes.py`·`observation_queries.json`에만 있음 — 소비자 쪽). 이는 tb-runner의 git 미추적 wrapper(systemd `loadgen-commerce` 유닛 주변)에 있을 가능성이 높다(메모리: "loadgen은 tb-runner systemd로 이전"). **필요 작업**: (1) k6 checkout 스텝에 409 전용 커스텀 메트릭(`Rate('checkout_409_rate')`, 기존 pass 판정과 별개로 병행 기록) 추가, (2) 그 값을 `checkout_409_rate` 필드로 변환기에 실어보내는 배선 — **이 위치를 tb-runner 실기에서 확인하는 것이 배포단계 선행 작업**(team-lead 실측 필요, k8s.env 대상 실측과 같은 성격).

---

## 2. env stopgap 주입 — k8s.env allowlist 확장분

`scripts/scenarios/profiles/k8s_env_executor.py`(읽기전용, 이 스펙에만 조각 기술) 현재 4개 시나리오 등록(F03-P·F09-H·F08-P·F18-P). F23-R 추가:

```python
APPROVED_TARGETS = {
    # ...기존 4개...
    "F23-R": ("rca-testbed-commerce", "testbed-inventory", "inventory-service"),
}
APPROVED_KEYS = {
    # ...기존 4개...
    "F23-R": {"SPRING_APPLICATION_JSON"},
}
```
`SPRING_APPLICATION_JSON`을 쓰는 이유는 F08-P와 동일(중첩 Spring 프로퍼티 `inventory.reconciliation.interval-ms`는 단일 평면 env 키로 못 씀). fault 값:
```json
{"inventory":{"reconciliation":{"interval-ms":999999999}}}
```
**baseline 배열은 배포단계에서 실측 필요**(team-lead 담당, 본인 명시) — `k8s_env_executor.validate`가 `current() == baseline` 사전조건을 강제하므로(`SCRIPT` 내 `check()` 함수, `k8s_env_executor.py:66`), testbed-inventory의 현재 컨테이너 env 전체 스냅샷을 `kubectl get deploy testbed-inventory -o json`으로 떠서 baseline 배열에 정확히 반영해야 한다(추측 금지 — 다른 시나리오의 baseline도 전부 실측값).

**§2-A(합성 결함 주입 금지) 위배 아님 근거** — 시트 §4-B 인용: "고갈은 loadgen의 자연 소비라 §2-A 위장이 아니다"(design-F23-R-sheet.md:85, §5 결론 문단 재확인: "k8s.env로 interval-ms를 크게 + rollout, 고갈은 loadgen의 자연 소비라 §2-A 위장이 아니다"). 이 env 변경은 **재고를 직접 조작하지 않는다** — 유일하게 건드리는 것은 배치의 재실행 주기이고, stock=0 도달은 오로지 상주 loadgen이 실제 reserve를 호출해 소비한 결과다. 반대로 시트가 명시적으로 금지하는 것은 `restock.threshold`/`restock.target`을 낮춰 고갈을 인위 제조하는 것(진짜 위장) — 이 스펙은 그 키를 건드리지 않는다(`APPROVED_KEYS["F23-R"]`에 `inventory.restock.*`는 없음, 오직 `inventory.reconciliation.interval-ms`만).

**stopgap 한정 명시** — 시트 §4 자체점검표(design-F23-R-sheet.md:56)와 §5 결론이 이 경로를 "golden 승격로가 아니라 훅 개발 전 검증용"으로 못박았다. 이 머지 스펙도 동일하게 **manifest의 `execution.controller.runtime.mode: "evaluation"`·`dispatcher_mode: "trusted"`·`live_enabled: false`, `actions.*.mode: "dry-run"`을 그대로 유지**한다 — 이번 배관으로 F23-R을 "golden 라이브"로 승격하는 것이 아니라 "관측+주입 배선이 갖춰진 evaluation-모드 draft"로 한 단계 전진시키는 것이다.

### 2-A. 재시작 오염 대응 — controller 타이밍 조정

`k8s_env_executor.py`의 `run` action은 `patch` 후 `healthy` 대기 없이 종료한다(cleanup에서만 `healthy 180s` 대기 — `k8s_env_executor.py:69-70`). **manifest의 `settle_after_change: "60s"`(design-F23-R-manifest.json:46)만으로는 rolling update 완료를 보장 못 함** — `testbed-inventory`는 `replicas: 1`, 전략 미지정(기본 RollingUpdate, maxSurge=1/maxUnavailable=0 상당, `commerce/k8s/22-inventory-service.yaml:9`)이라 이론상 신규 파드가 Ready 된 후 구파드가 종료되어 `kubernetes.pod_ready`가 끊기지 않아야 하지만, **이는 배포단계에서 실측 확인 필요**(readinessProbe 타이밍에 따라 짧은 창에서 selector가 신구 파드를 동시에 볼 수 있음). 권고: `settle_after_change`를 rollout 완료를 명시적으로 기다리는 스텝으로 바꾸거나(예: `kubernetes.deployment_available_replicas == 1` 게이트를 t1 확정 전에 추가), 최소한 `must_rule_out.consecutive_ticks`를 2→3으로 늘려 rollout 중 일시적 tick 하나를 흡수. **t1(golden 시간 기준점)은 patch 적용 시각이 아니라 rollout-healthy 확정 시각으로 앵커**해야 시트 §4-B(a)가 지적한 "재시작 이벤트의 창 오염"을 피할 수 있다.

---

## 3. profiles.json — load.north_south companion

`load.north_south`(profiles.json:147)의 `parameter_contract.allowed_scenarios` 배열에 `"F23-R"` 추가(현재 F07-H·F01-R·...·F02-P·F17-R·F18-P 등 목록에 이미 F02-P·F03-P 등 commerce 시나리오가 다수 등록돼 있어 선례 그대로 따름). companion 자체의 실행 파라미터(namespace/target 등)는 F23-R 전용 신규가 아니라 기존 commerce north-south 부하 실행 방식 그대로(체크아웃 저널 자연 소비가 곧 신호원이므로 별도 파라미터 불필요) — **scenario_parameters에 F23-R 전용 엔트리 불필요**, `allowed_scenarios`에 이름만 추가하면 된다(F01-R 등 다른 다수 시나리오도 같은 방식으로 이름만 등록돼 있음, profiles.json:161-187 확인).

---

## 4. business.fault 참조 처리

manifest의 `binding.primary_ref: "business.fault"`(design-F23-R-manifest.json:38)는 **미실현 스케줄러-정지 injector의 자리표시자**(F19-Q와 공유 예정, Wave 2)로 유지한다 — 이번 배관에서 `business.fault`의 `parameter_contract`/`scenario_parameters`를 F23-R용으로 채우지 않는다(team-lead 지시: "이번엔 env stopgap 경로로만 승격 설계", 스케줄러 정지 훅은 3등급 Wave2). 대신 실제 주입 경로는 §2의 `k8s.env`로 대체한다. **controller 조각 편차**: manifest를 그대로 반입하면 `primary_ref: business.fault`가 여전히 stub을 가리켜 실주입 불가 상태가 유지되므로, 반입 시 controller의 `binding.primary_ref`를 `"k8s.env"`로, `approved_profile_id`를 `"k8s.env"`로, `profile.levels[0].parameters`를 §2의 `{namespace, deployment, container, baseline, fault}` 스키마로 교체해야 한다. `business.fault`는 `related_scenarios`/주석에만 "향후 golden 훅 대상"으로 남겨 F19-Q와의 연결고리를 보존.

---

## 5. catalog/metadata/controllers 승격 조각 — 계약 필수사항

- **catalog.json**: `id: F23-R`, `class: A`, `fault_pattern: P3`, `domain: commerce`, `readiness: draft`(§1-C 미해결 항목 due — k6 exporter 배선 확인 전까지는 draft 유지 권고, §6 참조), `prerequisite_gate` 유지(§1-C 해소 전까지 `state: blocked`).
- **scenario-metadata.json**: `design-F23-R-metadata.json`의 `F23-R` 블록을 그대로 반입(root_cause/must_support/must_rule_out/distinguishing_evidence 전부 코드 재검증 통과, 수정 불필요). `code_anchor` 배열의 `ReconciliationBatch.java:74-91`은 실제로 `:73-91`이나 의미 변화 없어 사소한 오프바이원 그대로 두거나 정정, 둘 다 무방(권고: 정정).
- **controllers.json**: manifest의 `execution` 블록 반입 시 §2/§4 편차(`primary_ref`→`k8s.env`, observations의 3개 query_id는 그대로 §1의 신규 3종과 일치) 반영.
- success 게이트는 manifest 그대로(§1에서 필드 의미 확정): `checkout-409-up`(≥0.3)·`stock-depleted`(≥1)·`restock-halted`(==0), consecutive_ticks=3. **5xx가 아니라 409·409 게이트임을 controller 주석에 명시**(조용한 기능마비 성격 — must_rule_out의 `checkout-5xx-elevated`가 오히려 "낮아야 정상"이라는 반전 논리를 컨트롤러 코멘트에 남길 것).

---

## 6. test 기대값 delta

`scripts/scenarios/tests/*.py`(읽기전용, 조각만 기술) 반입 시 필요한 신규/변경 기대값:
- catalog 카운트 +1 (F23-R 신규 진입, 현재 66→67 또는 등록 시점의 최신 카운트+1).
- `k8s_env_executor` 테스트: `APPROVED_TARGETS`/`APPROVED_KEYS`에 F23-R 케이스 추가 시 기존 4-시나리오 파라미터화 테스트 패턴을 그대로 복제(F08-P 테스트가 `SPRING_APPLICATION_JSON` 단일 키 변경을 검증하는 방식과 동일 — F23-R도 동일 키라 테스트 골격 재사용 가능).
- `load.north_south` allowed_scenarios 테스트: F23-R 추가 확인.
- 신규 query_id 3종에 대한 러너 쪽 계약 테스트(`test_observations.py`/`test_live_probes.py` 상당) — `INVENTORY_STOCK_CONTRACT`/`RESTOCK_MOVEMENT_CONTRACT` 완전일치 검증 + 오염 파라미터 거부 테스트, `loadgen.checkout_409_rate`의 무파라미터 거부 테스트(파라미터 있으면 `LiveProbeError`).
- prerequisite_gate 테스트: F23-R이 `state: blocked`으로 등록되는 draft 스냅샷 테스트(§1-C 미해소 상태 반영, F19-Q와 마찬가지로 즉시 live 승격 테스트는 작성하지 않음).

---

## 7. 남는 미해결 항목 (blocked 유지 사유)

1. **k6 exporter 위치 미확인**(§1-C) — `checkout_409_rate` 필드를 실제로 채우는 코드가 두 레포 어디에도 없음. 배포단계 tb-runner 실기 확인 필요(team-lead).
2. **k6 커스텀 메트릭 신설**(§1-C) — script.js/surge.js checkout 스텝에 409 전용 Rate 메트릭 추가. 이 스펙 범위 밖(러너 실행 스크립트가 아니라 loadgen 소스 — testbed-services 쪽 커밋 대상이나 이번 작업 지침상 신규 loadgen 스크립트 불필요 판단이 뒤집힘, team-lead 재확인 요망).
3. **baseline env 실측**(§2) — testbed-inventory 컨테이너 현재 env 전체 스냅샷.
4. **rollout 타이밍 실측**(§2-A) — replicas=1 배포에서 patch 이후 pod_ready 연속성 확인.
5. 상기 1·2가 해소돼야 success 게이트의 `checkout-409-up` 신호가 실제로 채워진다 — **③ 하드 블로커는 이 머지 스펙 반입만으로 완전히 해소되지 않으며, 관측 3종 중 2종(stock/restock)은 이 스펙으로 완결, 1종(409 rate)은 러너 어댑터 배선은 완결되나 k6 발신원이 미완결**로 남는다. `prerequisite_gate.state`는 이 잔여를 이유로 `blocked` 유지를 권고.

---

## 8. 반환 요약

- 관측 3종: (1) `database.inventory_stock_level`·(2) `database.restock_movement_rate`는 **완전 신규**(F19-Q 대응 query도 미존재라 "동형 재사용"이 아닌 "동형 신설"), (3) 409 rate는 **기존 `business_nonok_rate` 재사용 불가로 판정**(체크아웃 한정도 409 특정도 안 됨) → `loadgen.checkout_409_rate` 신규지만 **어댑터 코드 변경은 allowlist 2줄**(range-check 일반화는 이미 병합됨); **k6 발신원 배선은 미해결**로 남음(§7-1,2).
- env 대상 실측 필요 항목: testbed-inventory/inventory-service 컨테이너 baseline env 전체(§2), rollout 중 pod_ready 연속성(§2-A).
- 앵커 검증: 시트의 전체 code_anchor 7건 모두 read로 재확인, 오프바이원 1건(사소, 의미 불변) 외 전부 정확.
- 확정 편차: (a) manifest의 `loadgen.checkout_status_rate{step,status_class}` 파라미터화는 어댑터 계약과 불일치 — `loadgen.checkout_409_rate`(무파라미터)로 정정. (b) manifest의 `primary_ref: business.fault`는 이번 승격에서 `k8s.env`로 교체(§4). (c) `settle_after_change`만으로 rollout 완료를 보장 못 함 — t1 앵커를 rollout-healthy 확정 시점으로 이동 권고(§2-A).
- 머지 스펙 경로: `docs/scenario-redesign-wip/merge-spec-F23-R.md`(본 문서).
