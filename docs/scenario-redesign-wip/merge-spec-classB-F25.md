# 머지 스펙 — Class B 공유 배선 + F25-H 1차 승격

- **정본 근거**: design-F25-middleware-sheet.md, fault-surface-infra-middleware.md, design-F25-{H,S,P,R}-{manifest,metadata}.json.
- **범위**: F25-H(commerce PG OOMKill)만 승격. F25-S/P/R은 배선 스펙만(승격 안 함). F26/F27/F25-R bare 커넥션은 손대지 않음.
- **실측 방법**: 109(GB10)에 이번 세션에서 ssh 권한이 없어(`Permission denied (publickey,password)`) 라이브 kubectl 확인은 못했다. 대신 레포 매니페스트(`commerce/k8s/10-postgres.yaml`)를 직접 읽어 재검증했고, executor·live_probes.py 소스를 전량 읽어 대조했다 — 아래 판정은 전부 이 소스 실측 기반이다. **라이브 kubectl 확인은 잔여 TODO**(적용자가 접근 권한으로 1회 재검증 권장: `kubectl -n rca-testbed-commerce get statefulset testbed-postgres`).

---

## 0. 핵심 수정 사항 — 설계 시트의 갭 판정을 좁힌다

design-F25-middleware-sheet.md §0은 "공통 갭 A(executor kind 일반화)"를 세 executor(k8s.resource·k8s.patch·kafka.control) 전체에 걸친 문제로 기술했다. **소스를 직접 읽은 결과, 이 갭은 실제로는 두 층위로 나뉜다:**

1. **주입(mutation) 쪽은 정말로 Deployment 하드코딩** — `k8s_resource_executor.py`의 bash SCRIPT가 `get deploy`, `patch deploy --type=strategic`, `rollout status deploy/`, `auth can-i patch deployments`를 리터럴로 쓴다. StatefulSet(`testbed-postgres`)엔 이 커맨드가 그대로 실패한다(리소스 이름은 같아도 kind가 다름). **여기는 진짜 kind 일반화가 필요하다.**
2. **관측(observation) 쪽은 대부분 이미 kind-agnostic이다** — `rca-scenario-runner/backend/app/live_probes.py`의 `_kubernetes_observation`을 읽어보면, `container_restart_count`/`container_last_termination_reason`/`container_oom_killed`/`container_memory_current_bytes`/`pod_ready` 계열은 워크로드 객체(`kubectl get deploy`)가 아니라 **파드 라벨 셀렉터**(`kubectl get pods --selector app=<name>`)로 조회한다(live_probes.py:681-730, :852-856). StatefulSet의 파드도 동일 라벨(`app: testbed-postgres`, `10-postgres.yaml:29`)을 갖고 있으므로 **이 5개 query는 코드 변경 없이 StatefulSet 파드에도 그대로 동작한다.** 필요한 건 kind 일반화가 아니라 **하드코딩된 타깃 allowlist에 postgres 항목을 추가하는 것**뿐이다.
   - 더 나아가 `kubernetes.pod_ready`용 `APPROVED_K8S_TARGETS`에는 **이미** `("rca-testbed-commerce", "testbed-postgres"): "app=testbed-postgres"`가 등록돼 있다(live_probes.py:112, F02-H가 먼저 써서 남긴 항목). `pg_pod_ready` 관측은 **오늘 당장 추가 배선 없이 동작한다.**
   - 예외 하나: `kubernetes.deployment_container_memory_limit`(F25-H manifest의 `pg_mem_limit` observation이 참조)은 진짜 `kubectl get deployment <name>` 하드코딩이라(live_probes.py:744-767) StatefulSet엔 안 통한다. 이건 success/abort/recovery 게이트에 쓰이지 않는 정보성 관측이라 **승격을 막지 않지만**, 완전성을 위해 신규 query id로 고치는 걸 권고한다(§3).

**결론**: F25-H 승격에 필요한 배선은 design 시트가 말한 만큼 크지 않다. (a) k8s.resource 실행기의 kind 일반화(진짜 필요), (b) live_probes.py의 postgres 타깃 allowlist 추가(작음), (c) mem_limit query 신설(선택, 비게이팅). F25-S/P가 필요로 하는 `statefulset_available_replicas`는 F25-H엔 불필요(§6에 배선 스펙만 기술).

---

## 1. F25-H 승격 경로

### 1-A. injector: `k8s_resource_executor.py` kind 일반화

**현재 (scripts/scenarios/profiles/k8s_resource_executor.py)**:
```python
APPROVED_TARGETS = {
    "F05-R": ("rca-testbed-commerce", "testbed-payment", "payment-service", "memory"),
    "F09-P": ("rca-testbed-commerce", "testbed-inventory", "inventory-service", "cpu"),
}
```
```bash
k=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$ns")
current() { "${k[@]}" get deploy "$deploy" -o json | jq -Sc --arg c "$container" '...' ; }
patch() { jq -cn ... | "${k[@]}" patch deploy "$deploy" --type=strategic --patch-file=/dev/stdin >/dev/null; }
healthy() { "${k[@]}" rollout status deploy/"$deploy" --timeout="$1" >/dev/null; }
check() { ...; "${k[@]}" auth can-i patch deployments | grep -qx yes; ...; }
```

**diff 제안** (StatefulSet의 `.spec.template.spec.containers` 경로는 Deployment와 동일하므로 JSON patch 페이로드는 손대지 않는다 — kind 리터럴만 파라미터화):

```diff
 APPROVED_TARGETS = {
     "F05-R": ("rca-testbed-commerce", "testbed-payment", "payment-service", "memory"),
     "F09-P": ("rca-testbed-commerce", "testbed-inventory", "inventory-service", "cpu"),
+    "F25-H": ("rca-testbed-commerce", "testbed-postgres", "postgres", "memory"),
 }
+APPROVED_KINDS = {
+    "F05-R": "deploy", "F09-P": "deploy",
+    "F25-H": "statefulset",
+}
```
```python
def validate(scenario_id, params, profile):
    required = {"namespace", "deployment", "container", "resource", "baseline", "fault"}
    ...
    # F25_H_MEM_LEVELS 형태로 512Mi→320Mi 고정 1단(design manifest와 일치)를 F05_R_LEVELS처럼 선언하고
    # scenario_id == "F25-H" 분기에서 members 검증 추가 (F05-R 패턴 그대로 재사용).
```
```python
def build_invocation(plan, action):
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    validate(plan["scenario"]["id"], p, {})
    kind = APPROVED_KINDS[plan["scenario"]["id"]]
    return kubectl_bash_argv([
        action, plan["scenario"]["id"], kind, p["namespace"], p["deployment"], p["container"],
        json.dumps(p["baseline"], ...), json.dumps(p["fault"], ...),
    ]), SCRIPT
```
```diff
 SCRIPT = br'''#!/usr/bin/env bash
 set -euo pipefail
-action="$1"; scenario_id="$2"; ns="$3"; deploy="$4"; container="$5"; baseline="$6"; fault="$7"
+action="$1"; scenario_id="$2"; kind="$3"; ns="$4"; deploy="$5"; container="$6"; baseline="$7"; fault="$8"
 state_root="${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}"
 state="$state_root/${scenario_id}-container-resources.json"
 k=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$ns")
-current() { "${k[@]}" get deploy "$deploy" -o json | jq -Sc --arg c "$container" '...'; }
-patch() { jq -cn ... | "${k[@]}" patch deploy "$deploy" --type=strategic --patch-file=/dev/stdin >/dev/null; }
-healthy() { "${k[@]}" rollout status deploy/"$deploy" --timeout="$1" >/dev/null; }
-check() { command -v kubectl >/dev/null; command -v jq >/dev/null; "${k[@]}" auth can-i patch deployments | grep -qx yes; [[ "$(current)" == "$baseline" ]]; healthy 1s; }
+current() { "${k[@]}" get "$kind" "$deploy" -o json | jq -Sc --arg c "$container" '...'; }
+patch() { jq -cn ... | "${k[@]}" patch "$kind" "$deploy" --type=strategic --patch-file=/dev/stdin >/dev/null; }
+healthy() { "${k[@]}" rollout status "$kind"/"$deploy" --timeout="$1" >/dev/null; }
+check() { command -v kubectl >/dev/null; command -v jq >/dev/null; "${k[@]}" auth can-i patch "${kind}s" | grep -qx yes; [[ "$(current)" == "$baseline" ]]; healthy 1s; }
```
- `auth can-i patch deployments` → `auth can-i patch "${kind}s"` (kubectl RBAC resource plural: `deployments`/`statefulsets`, both regular plurals — safe).
- `kubectl rollout status statefulset/<name>` is a supported kubectl subcommand (StatefulSet rollout status is real, unlike some other kinds) — no substitute needed.
- **주의**: PG StatefulSet은 `updateStrategy` 기본이 `RollingUpdate`이므로 `patch` 후 `rollout status`가 파드 재기동을 기다린다 — 이건 F25-H가 원하는 정확한 동작(메모리 조임 → 재기동 유발)과 일치한다. `run` 액션에서 patch 직후 pod가 OOMKill되면 rollout status가 timeout날 수 있으므로 F25-H의 `run` 단계는 `healthy` 호출 없이(F05-R과 동일하게 `patch`만 호출) 끝내야 한다 — 현재 SCRIPT의 `run)` 분기가 이미 `healthy`를 호출하지 않으므로 **수정 불필요**, 그대로 맞다.

### 1-B. 관측: allowlist 추가만 (kind 일반화 없음)

**`rca-scenario-runner/backend/app/live_probes.py`** — F05 계열 타깃 상수에 병렬 추가 (F20_FOOD_ORDER_TARGET 추가 때 쓴 패턴 그대로):
```diff
+F25_H_POSTGRES_TARGET = {
+    "namespace": "rca-testbed-commerce",
+    "deployment": "testbed-postgres",
+    "container": "postgres",
+}
```
```diff
             elif parameters == F20_FOOD_ORDER_TARGET and query.query_id in {
                 "kubernetes.container_memory_current_bytes",
                 "kubernetes.container_memory_limit_bytes",
                 "kubernetes.container_restart_count",
                 "kubernetes.container_last_termination_reason",
                 "kubernetes.container_oom_killed",
             }:
                 target = F20_FOOD_ORDER_TARGET
+            elif parameters == F25_H_POSTGRES_TARGET and query.query_id in {
+                "kubernetes.container_memory_current_bytes",
+                "kubernetes.container_memory_limit_bytes",
+                "kubernetes.container_restart_count",
+                "kubernetes.container_last_termination_reason",
+                "kubernetes.container_oom_killed",
+            }:
+                target = F25_H_POSTGRES_TARGET
             else:
                 raise LiveProbeError("payment container target is not allowlisted")
```
- `param name`은 여전히 `"deployment"` 키를 쓰지만 값은 `testbed-postgres`(StatefulSet 이름) — 이건 이름뿐인 레거시 파라미터 명이라 기능에 영향 없음(파드 라벨 셀렉터 조회 로직만 탄다). 명명 정합성은 선택 리팩터(§7)로 미룬다.
- `pg_pod_ready`(F25-H manifest observation)는 **이미 동작**(`APPROVED_K8S_TARGETS`에 기존 등록). 추가 배선 불필요.
- `pg_mem_current`/`pg_mem_limit`: mem_current는 위 elif 추가로 해결. **mem_limit**은 `kubernetes.deployment_container_memory_limit` query를 쓰는데 이건 `kubectl get deployment` 리터럴(§0 예외). 두 가지 선택지:
  - (권장) F25-H manifest의 `pg_mem_limit` observation을 **제거**한다 — success/abort/recovery 어디에도 안 쓰이는 정보성 필드이고, baseline(512Mi)/fault(320Mi)는 controller profile에 이미 고정값으로 박혀있어 라이브에서 재확인할 필요가 약하다. 승격을 지금 막지 않는 게 우선.
  - (완전성 원하면) 신규 query id `kubernetes.statefulset_container_memory_limit`을 만들어(§3) 사용 — 기존 `deployment_container_memory_limit` 핸들러를 복제하고 `kubectl get statefulset`으로 바꾼 버전. F25-S/P 배선(§6)과 함께 갈 수 있는 작업이라 이번 배치엔 필수 아님.
  - **본 스펙은 권장안(제거)을 F25-H 승격 매니페스트에 반영**했다(§4).

### 1-C. `k8s.resource` profile 계약 (`scripts/scenarios/registry/profiles.json`)

```diff
     "k8s.resource": {
       "executor": "profiles/k8s_resource_executor.py",
       "location_strategy": "scenario",
       "allowed_locations": [
         "commerce-namespace"
       ],
       "live_supported": true,
       "default_queries": [
         "kubernetes.container_restart_count",
         "kubernetes.container_last_termination_reason",
         "kubernetes.deployment_container_memory_limit",
         "kubernetes.deployment_resources_match_baseline",
         "kubernetes.pod_ready",
         "http.entry_health"
       ],
       "parameter_contract": {
         "allowed_scenarios": [
-          "F05-R"
+          "F05-R",
+          "F25-H"
         ]
       },
       "scenario_parameters": {
         "F05-R": { ... },
+        "F25-H": {
+          "namespace": "rca-testbed-commerce",
+          "deployment": "testbed-postgres",
+          "container": "postgres",
+          "resource": "memory",
+          "baseline": {"limits": {"memory": "512Mi"}, "requests": {"memory": "256Mi"}},
+          "fault": {"limits": {"memory": "320Mi"}, "requests": {"memory": "256Mi"}}
+        }
```
- `scenario_levels`에도 F05-R처럼 단일 레벨을 등록(design manifest §profile.levels와 동일 — F25-H는 fixed 1단, F05-R처럼 ladder 아님).
- `10-postgres.yaml:67-70` 실측 baseline은 `requests: {cpu:200m, memory:256Mi}, limits:{cpu:500m, memory:512Mi}` — **cpu 필드도 baseline/fault 양쪽에 포함해야** `set(value) - {"requests","limits"}` 검증과 무관하게 실제 patch가 cpu를 건드리지 않도록, `resource=memory` 선택이라 validate()의 `key not in value.get("limits",{})` 체크는 memory만 확인하지만 patch 페이로드 전체(cpu 포함)가 그대로 나가므로 **cpu 값도 baseline과 동일하게 채워 넣어야 한다**(design manifest json이 cpu를 생략한 채 memory만 넣었는데, 이러면 patch가 cpu limit을 지워버릴 위험 — F05_R_BASELINE 패턴처럼 cpu를 명시). **승격 시 반드시 baseline/fault 양쪽에 `cpu: "500m"`를 포함**하도록 정정.

### 1-D. `load.north_south` companion allowlist

```diff
       "parameter_contract": {
         "allowed_scenarios": [
           "F07-H", "F01-R", "F01-H", "F06-R", "F08-H", "F09-P", ...
+          , "F25-H"
         ]
       },
```
- companion 파라미터는 F02-P 선례(commerce entry 30080, surge.js checkout)를 그대로 재사용 — 신규 tag_pattern 불필요, 기존 checkout surge로 충분(design sheet §4-A item 6과 일치).

### 1-E. `registry/scenario-metadata.json` + `registry/controllers.json` + `catalog.json`

- **scenario-metadata.json**: design-F25-H-metadata.json 내용을 그대로 `"F25-H": {...}` 키로 삽입(title/description/cause/root_cause/injection_summary/user_impact/must_support/must_rule_out/distinguishing_evidence/difficulty/related_scenarios 전부 이미 완성된 텍스트).
- **controllers.json**: design-F25-H-manifest.json의 `execution.controller` 블록을 F05-R 항목과 나란히 삽입하되, `live_enabled: true`로 바꾸고(design draft는 `false`), `dispatcher_mode: "trusted"` 유지. `live_scenario_ids` 배열(controllers.json 최상단, 현재 33개)에 `"F25-H"` append.
- **catalog.json**: 새 엔트리
  ```json
  {
    "id": "F25-H",
    "slug": "f25-h-commerce-postgres-oomkill",
    "profiles": ["k8s.resource", "load.north_south"],
    "readiness": "ready",
    "load_mode": "fixed",
    "injection_location": "commerce-namespace",
    "prerequisite": "k8s.resource StatefulSet kind 일반화 적용 완료, postgres 관측 allowlist 추가 완료"
  }
  ```
  (`load_mode: "fixed"` — F25-H는 단일 fixed level, F05-R 같은 adaptive ladder 아님.)
- **manifests/f25-h-commerce-postgres-oomkill.yaml**: design-F25-H-manifest.json을 매니페스트 컴파일러(`generate-manifests.py`) 입력 형식(yaml)으로 변환. `prerequisite_gate.state`를 `blocked`→`ready`, `live_allowed`을 `false`→`true`로, controller의 `live_enabled: false`→`true`로, `pg_mem_limit` observation 항목을 제거(§1-B).

### 1-F. 테스트 델타

- `scripts/scenarios/tests/test_registry_contracts.py`:
  - `test_registry_closure_covers_64_scenarios_and_20_profiles`: `len(catalog["scenarios"])` 기대값 `50→51`.
  - `test_live_scenarios_match_matrix`(라인 129 부근): `live_ids` 집합과 `controllers["live_scenario_ids"]` 리스트 양쪽에 `"F25-H"` 추가. F25-H는 `expected_live=True`이므로 manifest의 `binding.primary_ref == "k8s.resource"`, `companion_refs == ["load.north_south"]`, `runtime.baseline.clean_window == "30m"`, `runtime.capture.post_window == "20m"` 제약을 그대로 만족해야 한다(design manifest가 이미 이 값들로 맞춰져 있음 — 확인됨).
- `scripts/scenarios/tests/test_k8s_generic_executors.py`:
  - `test_resource_executor_supports_only_named_payment_memory_and_inventory_cpu`에 F25-H 케이스 추가: `resource.validate("F25-H", pg_params, {})`가 통과하고, `namespace`나 `container`를 바꾼 tampered 버전은 `not allowlisted`로 거부되는지 확인.
  - kind 파라미터화 후 `test_resource_script_snapshots_then_restores_exact_original`이 여전히 통과하는지(스크립트 텍스트 어서션이 `deploy` 리터럴 대신 `"$kind"` 변수 사용을 반영하도록 갱신) 확인 — 현재 테스트는 `--type=strategic`만 어서트하므로 큰 수정은 아니나, `get "$kind"` 형태로 바뀐 걸 검증하는 어서션 한 줄 추가 권고.
- `rca-scenario-runner/backend/tests/test_external_live_manifests.py`: F25_H_POSTGRES_TARGET을 참조하는 새 관측 케이스(모의 kubectl stdout으로 pod 라벨 `app=testbed-postgres` 응답 픽스처) 추가 권고 — 기존 F20 테스트 패턴 복제.

---

## 2. F25-H 감별/성공 게이트 재확인

design manifest의 success/abort/recovery는 그대로 유효하다(§0에서 관측 배선만 좁혔을 뿐 게이트 로직은 안 바꿨다):
- **success**: `pg_termination_reason == OOMKilled` AND `pg_restart_count >= 1` AND `commerce_5xx_rate >= 0.1` (2틱 연속)
- **abort**: `entry_status == 0` OR `pg_restart_count >= 6`(재시작 폭주 — catastrophic 재정의 규칙과 정합, pod_ready abort 없음 — test_abort_gates_are_user_impact_only 통과)
- **recovery**: `pg_pod_ready == true` AND `entry_status == 200` AND `commerce_5xx_rate < 0.05`
- **must_rule_out**: `pg_pod_ready == false`가 2틱 연속이면 "connection-refusal 아님"을 배제하는 신호로 씀(design manifest 그대로 유지 — 이미 실재하는 query라 변경 없음).

---

## 3. 공유 배선 명세 (F25-S/P/R — 이번 승격 안 함, 배선만)

### 3-A. `kubernetes.statefulset_available_replicas` 신설

**`scripts/scenarios/registry/queries.json`** (= `rca-scenario-runner/backend/app/observation_queries.json`, 두 파일은 현재 byte-identical — 양쪽 동시 수정 필요):
```diff
+    "kubernetes.statefulset_available_replicas": {
+      "adapter": "kubernetes",
+      "selector": "statefulset.status.available_replicas",
+      "freshness_sec": 30,
+      "allowed_parameters": ["namespace", "statefulset"]
+    },
```
**`live_probes.py`**: `kubernetes.deployment_available_replicas` 블록(line 790-808)을 그대로 복제, `kubectl get deployment` → `kubectl get statefulset`, `document["status"]["availableReplicas"]`는 StatefulSet에서도 동일 필드명(`status.availableReplicas`, k8s API 공통 스키마)이라 селector 문자열만 다르면 됨:
```diff
+        if query.query_id == "kubernetes.statefulset_available_replicas":
+            target = (
+                str(query.parameters.get("namespace")),
+                str(query.parameters.get("statefulset")),
+            )
+            if set(query.parameters) != {"namespace", "statefulset"} or target not in APPROVED_STATEFULSET_REPLICA_TARGETS:
+                raise LiveProbeError("statefulset replica target is not allowlisted")
+            namespace = str(query.parameters["namespace"])
+            statefulset = str(query.parameters["statefulset"])
+            result = self._kubectl(
+                "get", "statefulset", statefulset, "--namespace", namespace, "-o", "json",
+            )
+            document = json.loads(result.stdout)
+            value = document.get("status", {}).get("availableReplicas", 0)
+            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
+                raise LiveProbeError("statefulset available replicas is invalid")
+            return value, _aware(self.clock()), (
+                f"kubernetes:{namespace}:{statefulset}:available-replicas"
+            )
```
```diff
+APPROVED_STATEFULSET_REPLICA_TARGETS = frozenset(
+    {
+        ("rca-testbed-commerce", "testbed-kafka"),   # F25-S 감별 핵심
+    }
+)
```
- **F25-S 감별점 그대로 성립**: F04-R(consumer 정지, 브로커 생존)은 `kubernetes.kafka_consumer_lag`↑로만 보이고 `statefulset_available_replicas(kafka)`는 baseline(1) 유지. F25-S(브로커 down)는 `available_replicas=0` + lag 관측 자체가 실패(브로커가 없으니 exec 불가) — 이 비대칭이 헌장이 요구하는 감별축이다.

### 3-B. executor kind 일반화 — F25-P(`k8s.patch`), F25-S(`kafka.control`)

- **`k8s_patch_executor.py`**: 현재 `baseline_cpu_limit != "500m"`이면 거부(line 21-22) — 이건 commerce 서비스(500m 고정) 전용 가드다. Oracle의 baseline cpu는 `2000m`(`10-oracle.yaml:37-41` 실측)이라 **이 가드를 완화**해야 F25-P가 통과한다: `allowed_baseline_cpu_limits = {"500m", "2000m"}` 식으로 스칼라 하나 대신 scenario별 허용 집합으로 바꾸는 게 가장 좁은 변경. 동시에 SCRIPT의 `get deploy`/`set resources deploy`도 §1-A와 동일한 kind 파라미터화가 필요(Oracle=StatefulSet).
- **`kafka_control_executor.py`**: `CONTRACTS`가 `scenario_id → params` 단일 매핑이라 F25-S를 추가하려면 새 키 `"F25-S"`를 등록하되, **현재 스크립트는 `deployment`를 scale 대상으로 스케일하는 로직**(consumer)이라 브로커(StatefulSet, replicas 자체를 0으로) 시나리오엔 다른 동작이 필요하다: `available_replicas()`/`current_replicas()`가 `kubectl get deploy`를 쓰는데 브로커는 StatefulSet이므로 여기도 kind 파라미터화 + `scale statefulset` 커맨드로 교체해야 한다. 또한 F25-S는 소비자가 아니라 **브로커 자체**를 죽이므로 `wait_lag_zero()`(kafka-consumer-groups.sh exec)가 브로커 죽은 상태에서 실패하는 게 정상 — cleanup 단계에서 브로커 복구 후에만 lag 체크가 유효하다는 순서 가정이 스크립트에 이미 있음(scale 복구 → wait_available → wait_lag_zero 순서, line 101-107)이라 **로직 순서는 그대로 재사용 가능**, kind만 바꾸면 됨.
- 두 executor 모두 §1-A와 동일한 패턴(kind 변수화)이므로 F25-H 적용 후 이 두 곳은 기계적으로 반복 적용 가능 — **F25-H가 먼저 착지하면 F25-S/P 배선 비용이 실측으로 검증된 템플릿을 얻는다.**

### 3-C. F25-R (별도 능력 트랙, 이번 배선에 없음)

design 시트 판정 그대로 유지: bare 커넥션 슬롯 injector(`db.connection-saturate` 같은 신규 executor)와 `database.pg_connection_count`/`pg_connection_error_rate` 신규 query가 필요한 **진짜 능력 갭**이라 이번 공유 배선 범위 밖. 별도 트랙으로 분리 권고(design sheet 결론과 동일).

---

## 4. 요약 — 이번 배치 산출물 체크리스트

| 파일 | 변경 | 승격 관련 |
|---|---|---|
| `scripts/scenarios/profiles/k8s_resource_executor.py` | APPROVED_TARGETS/APPROVED_KINDS에 F25-H 추가, SCRIPT kind 파라미터화 | F25-H 필수 |
| `rca-scenario-runner/backend/app/live_probes.py` | F25_H_POSTGRES_TARGET 추가 + elif 분기 | F25-H 필수 |
| `scripts/scenarios/registry/profiles.json` | k8s.resource·load.north_south allowed_scenarios/scenario_parameters += F25-H (cpu 필드 포함 필수) | F25-H 필수 |
| `scripts/scenarios/registry/scenario-metadata.json` | F25-H 메타데이터 삽입 | F25-H 필수 |
| `scripts/scenarios/registry/controllers.json` | F25-H 컨트롤러(live_enabled=true) + live_scenario_ids append | F25-H 필수 |
| `scripts/scenarios/catalog.json` | F25-H 엔트리(readiness=ready) | F25-H 필수 |
| `scripts/scenarios/manifests/f25-h-commerce-postgres-oomkill.yaml` | 신규(pg_mem_limit observation 제거) | F25-H 필수 |
| `scripts/scenarios/tests/test_registry_contracts.py` | 51 scenarios, live_ids/live_scenario_ids += F25-H | F25-H 필수 |
| `scripts/scenarios/tests/test_k8s_generic_executors.py` | F25-H validate 케이스 + kind 리터럴 어서션 갱신 | F25-H 필수 |
| `rca-scenario-runner/backend/tests/test_external_live_manifests.py` | F25-H 관측 픽스처 | 권고 |
| `queries.json`/`observation_queries.json` (양쪽) | `kubernetes.statefulset_available_replicas` 신설 | F25-S 선행 배선(이번 승격 아님) |
| `k8s_patch_executor.py` | baseline cpu 가드 완화 + kind 파라미터화 | F25-P 선행 배선(이번 승격 아님) |
| `kafka_control_executor.py` | F25-S 계약 추가 + kind 파라미터화(scale statefulset) | F25-S 선행 배선(이번 승격 아님) |

**F25-H 승격 실현성**: 관측(③)은 이미 완비돼 있었고(pod_ready는 즉시 사용 가능, restart/termination/oom/mem_current는 allowlist 추가 1건), 주입(④)은 k8s.resource의 kind 일반화 1건(위 diff)으로 닫힌다 — design 시트의 "배선만 남음" 판정은 맞고, 실제 배선 범위는 시트가 우려한 것보다 **더 좁다**(관측은 대부분 이미 동작). 유일한 리스크는 F05_R_BASELINE 패턴을 안 따르고 cpu 필드를 생략한 design manifest의 파라미터 초안 — 이건 §1-C에서 정정했다.
