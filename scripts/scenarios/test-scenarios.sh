#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd -- "$script_dir/../.." && pwd)"
catalog="$script_dir/catalog.json"
manifest_dir="$script_dir/manifests"

total="$(jq '.scenarios | length' "$catalog")"
[[ "$total" -eq 60 ]]
# Internal consistency: ids, slugs and manifests track the catalog exactly, so
# these are derived rather than pinned — a pinned copy is what went stale here
# (the suite asserted 64 long after the catalog moved to 60, and failed silently
# in anything that did not check its exit code).
[[ "$(find "$manifest_dir" -maxdepth 1 -type f -name '*.yaml' | wc -l)" -eq "$total" ]]
[[ "$(jq '[.scenarios[].id] | unique | length' "$catalog")" -eq "$total" ]]
[[ "$(jq '[.scenarios[].slug] | unique | length' "$catalog")" -eq "$total" ]]
# bin/ predates the profile-executor architecture and has drifted: 18 scripts
# have no catalog entry and 14 catalog entries have no script. Pinned so new
# drift trips, not as an endorsement — cleanup is a separate backlog item.
[[ "$(find "$script_dir/bin" -maxdepth 1 -type f -name '*.sh' | wc -l)" -eq 64 ]]
# 2026-07-29: ready 41→43, parked 14→12. F15-H·F15-T2 승격(c83fcaf)이 이 핀을 두고 갔다.
# 이 스위트가 deploy-live-promotions.sh의 첫 관문이라, 핀이 어긋난 동안 배포 자체가
# 막혀 있었다 — 승격 커밋과 핀 갱신은 같은 커밋에 있어야 한다.
# The readiness split IS a governed decision, so it stays pinned.
# parked = 2026-07-27 골든 감사 CUT 26종. 설계 자산은 남기고 실행에서만 뺀다.
# 2026-07-28: ready 31→32, parked 26→25. F03-H가 복귀했다 — 주입 표면을 자백하던
# OrderController의 Thread.sleep(delayMs)을 실제 결함(직렬화된 O(n^2) 리포트 렌더러)으로
# 교체해 G6 누설을 없앴다.
# 2026-07-28: readiness에 cut을 신설하고 음성 시나리오 4종(F01-G·F03-G·F05-G·F11-G)을
# 옮겼다(parked 24→20). parked는 "회생 후보"라는 뜻이므로 회생 조건이 코드가 아니라
# 헌장 개정인 것을 같은 칸에 두면 영원히 열릴 것처럼 읽힌다. cut은 종착역이다.
# 2026-07-28: ready 33→37. 스토리지 포화 3종(F02-H·F10-H·F10-P)과 복합 자원 고갈
# F15-P가 들어왔다. 마지막까지 이들을 막고 있던 것은 도구도 계약도 아니라
# "장치가 바쁘다"를 셀 수 없다는 것이었다 — host.disk_io_utilization 신설로 풀렸다.
# 2026-07-28: ready 37→39. Tomcat 스레드풀 포화 짝(F21-P·F21-Q). 좌표는 배치 고정으로
# 이미 갈렸고(F09-R과 다른 노드), 마지막 차단은 busy-thread 신호가 non-daemon 스레드를
# 세고 있었다는 것이다 — Tomcat 워커는 daemon이라 그 합은 4~5에 붙박이였다.
# 2026-07-28: ready 39→41. F09-H·F09-P가 07-27 parked에서 복귀했다. 감별 신호가
# 없다던 판정이 둘 다 틀렸다 — GC는 재는 방법(used_after_last_gc/limit 비율)이 없었을
# 뿐이고, 스로틀은 신호가 있었으나 러너 템플릿이 testbed-product로 하드코딩돼 있어
# F12-H 말고는 아무도 쓸 수 없었다.
# 2026-07-28: ready 41→42. F17-P. 카탈로그가 적어둔 배선 4항 중 둘은 이미 완료돼
# 있었고, 원장 역분개는 주입 금액을 1~5로 낮춰 오염 자체를 없애는 것으로 대체했다.
# 2026-07-28: ready 42→43, blocked 1→0. F04-H. 유일한 blocked였고, 필요하던
# 제어 표면(@ConditionalOnProperty)은 앱에 이미 있었다 — 없던 것은 commerce PG의
# 미발행 outbox를 세는 관측이며 러너에 신설했다.
# 2026-07-28: ready 43→41. 부하 도달 가능성 전수 점검에서 F07-P·F20-P가 반증됐다.
# 둘 다 "부하를 부으면 뻗는다"를 서비스 시간 측정 없이 가정하고 있었다.
# 2026-07-29: ready 43→44. F14-P. 필요하던 "catch-swallow injector"는 앱에 심을 필요가
# 없었다 — 결함(삼킴 + 자동 ack = 영구 유실)은 이미 코드에 있고, 없던 것은 그것을
# 발화시킬 쓰기 실패다. ledger_entries만 READ ONLY로 뒤집는다(db.table_readonly 신설).
# 짝인 F14-R은 같은 심사에서 반증돼 parked로 남았다: 중첩 타임아웃이 안쪽(10s) <
# 바깥쪽(15s)이라 "커밋됐는데 응답만 유실"이 구조적으로 생기지 않는다.
# 2026-08-06: ready 44→41, parked 11→14. c887e20 이 F02-H·F21-P·F21-Q 를 파킹할 때
# 카탈로그만 옮기고 이 게이트를 안 고쳐서, 그날부터 clean tree 에서도 이 스크립트가
# exit 1 이었다(0806 배치 수리 중 발견). 셋 다 라이브 두 번이 인과 부재·성공↔감별자
# 상호배타를 실증한 건이고, 재설계는 별도 트랙(F21 은 app.control 지연 표면 구현 완료,
# 승격은 사다리 실측 후).
[[ "$(jq '[.scenarios[] | select(.readiness=="ready")] | length' "$catalog")" -eq 41 ]]
[[ "$(jq '[.scenarios[] | select(.readiness=="parked")] | length' "$catalog")" -eq 14 ]]
[[ "$(jq '[.scenarios[] | select(.readiness=="cut")] | length' "$catalog")" -eq 4 ]]
[[ "$(jq '[.scenarios[] | select(.readiness=="blocked")] | length' "$catalog")" -eq 0 ]]
[[ "$(jq '[.scenarios[] | select(.readiness=="draft")] | length' "$catalog")" -eq 1 ]]
[[ "$(jq '[.scenarios[] | select(.readiness=="partial")] | length' "$catalog")" -eq 0 ]]
# 2026-07-28: 13 → 17. 스토리지 IO 3종(F02-H·F10-H·F10-P)과 F15-P를 고정 계약에서
# 캘리브레이션 사다리로 전환했다. 고정 rate_iops가 장치 능력의 16~21%뿐이라 피해를
# 낼 수 없었고, 무릎은 정적으로 알 수 없어 런타임이 찾아야 한다.
# 2026-08-06: 18 → 19. 9e23a9a 가 F25-H 를 320Mi 고정에서 실측 사다리로 돌리며
# load_mode 를 adaptive 로 바꿨는데 이 개수를 안 고쳤다. 게이트가 첫 실패에서 멈추는
# 탓에 위쪽 ready 개수(8d1ef6b)를 고치고 나서야 드러났다 — 같은 계열의 두 번째 누락.
# 2026-08-07: 19 → 20. F20-R(d1df8a5). rps 30 고정은 정체성을 위반하고 있었다 —
# 그 부하에서 order 는 느려진 게 아니라 30초 타임아웃 벽에 처박혔고 헬스체크마저 503 이었다.
# 쓸 수 있는 띠는 실측상 1 rps 부근이라(단일 풀스캔이 1 rps 에서 이미 p95 1826ms,
# 2~3 rps 에서 곧장 5xx) 사다리 1·2·3 으로 강등해 런타임이 그 좁은 띠를 찾게 한다.
[[ "$(jq '[.scenarios[] | select(.load_mode=="adaptive")] | length' "$catalog")" -eq 20 ]]
# 2026-08-06: 42 → 41. adaptive 의 짝 — F25-H 가 fixed 에서 빠져나갔으므로 함께 움직인다.
# 2026-08-07: 41 → 40. adaptive 의 짝 — F20-R 이 fixed 에서 빠져나갔다.
[[ "$(jq '[.scenarios[] | select(.load_mode=="fixed")] | length' "$catalog")" -eq 40 ]]
[[ "$(jq '[.scenarios[] | select(.load_mode=="no-load")] | length' "$catalog")" -eq 0 ]]
# 2026-07-29: 알려진 profile 목록을 손으로 적어두던 것을 레지스트리에서 유도하도록
# 바꿨다. 손으로 적힌 목록은 profile을 신설할 때마다 조용히 낡고, 그 결과가 0f40dd7의
# 배포 정지였다 — 검사하는 쪽과 실제 계약이 다른 것을 보고 있으면 안 된다.
jq -e --argjson known_profiles "$(jq '[.profiles | keys[]]' "$script_dir/registry/profiles.json")" '
  all(.scenarios[];
    (.id | test("^F[0-9]{2}-(R|H|P|G|Q|S|T[1-4])$")) and
    (.slug | test("^[a-z0-9][a-z0-9-]+$")) and
    (.readiness | IN("ready", "partial", "blocked", "draft", "parked", "cut")) and
    (.load_mode | IN("adaptive", "fixed", "no-load")) and
    (.injection_location | type == "string" and length > 0) and
    (.profiles | type == "array" and length > 0 and all(.[]; IN($known_profiles[]))) and
    (if .readiness == "ready" then true else (.prerequisite | length > 0) end)
  )
' "$catalog" >/dev/null

python3 "$script_dir/generate-manifests.py" --check >/dev/null

while IFS= read -r row; do
  slug="$(jq -r '.slug' <<<"$row")"
  manifest="$manifest_dir/$slug.yaml"
  jq -e --argjson row "$row" '
    ["ssh", "kubectl", "api-via-kubectl", "local-orchestrator", "unresolved"] as $known_transports |
    ["catalog-integrity", "canonical-kubeconfig", "global-dirty-lease",
     "baseline-clean-window", "target-health", "profile-prerequisites",
     "transport-ssh-access", "transport-kubernetes-rbac", "transport-api-contract",
     "transport-local-runner-state", "transport-resolution-blocker",
     "db-session-tag-clean", "db-inverse-ddl-ready", "mock-restore-contract",
     "baseline-loadgen-active", "east-west-job-contract",
     "kubernetes-original-spec-snapshot", "kubernetes-recovery-capacity",
     "kubernetes-original-resource-snapshot", "kubernetes-original-probe-snapshot",
     "kubernetes-original-env-snapshot",
     "kafka-drain-capacity", "host-placement-and-oob-recovery",
     "cache-warmup-contract", "network-oob-recovery", "rollback-artifact",
     "wpm-probe-contract", "business-invariant-probe", "subinjection-timeline",
     "app-control-flag-armed"] as $known_preflights |
    ($row.profiles | map("injector-profiles/" + .)) as $profile_refs |
    .id == $row.id and
    .slug == $row.slug and
    .readiness == $row.readiness and
    .injection.location == $row.injection_location and
    (.injection.matrix_location_transport | type == "string" and length > 0) and
    (.injection.transport | IN($known_transports[])) and
    .injection.profile_refs == $profile_refs and
    (.execution.preflight_ids | length >= 8 and all(.[]; IN($known_preflights[]))) and
    (if .execution.controller.live_enabled
     then .execution.controller.dispatcher_mode == "trusted" and
          .execution.controller.binding.primary_ref == $row.profiles[0] and
          .execution.controller.binding.companion_refs == $row.profiles[1:] and
          (.execution.controller.runtime.mode | IN("calibration", "evaluation")) and
          (if $row.load_mode == "adaptive"
           then .execution.controller.runtime.profile.kind == "adaptive_ladder"
           else .execution.controller.runtime.profile.kind == "fixed" end)
     else .execution.controller.dispatcher_mode == "dry-run" and
          (.execution.controller.decision_mode | IN("calibration", "evaluation", "none")) and
          .execution.controller.profile.kind == $row.load_mode end) and
    all(.actions[]; .mode == "dry-run" and .mutation == false) and
    .actions.run.requires_preflight == true and
    .actions.cleanup.required == true and
    .actions.cleanup.order == "reverse" and
    .actions.cleanup.recovery_gate == true and
    .prerequisite_gate.live_allowed == .execution.controller.live_enabled and
    (if $row.readiness == "ready"
     then .prerequisite_gate.state == "satisfied" and .prerequisite_gate.required == false
     else .prerequisite_gate.state == "unresolved" and .prerequisite_gate.required == true
     end) and
    .capture_policy.policy_ref == "focused-window-v1" and
    .capture_policy.time_basis == "UTC" and
    .capture_policy.query_window == "[t1-10m,t2+20m]" and
    .capture_policy.export_not_before == "t2+20m" and
    .capture_policy.create_golden_anomaly == false
  ' "$manifest" >/dev/null
done < <(jq -c '.scenarios[]' "$catalog")

# Parked and cut scenarios keep their design assets (manifest, executors) but
# must never compile to an executable plan. compile-plan gates live_allowed on
# readiness == "ready", so this is the guard that keeps a withdrawn scenario
# from re-entering the capture queue by accident. cut is covered by the same
# loop deliberately: a terminal decision needs the same mechanical proof as a
# temporary one, or the enum value becomes documentation instead of a gate.
while IFS= read -r slug; do
  [[ "$(python3 "$script_dir/compile-plan.py" --scenario "$slug" | jq -r '.live_allowed')" == "false" ]]
done < <(jq -r '.scenarios[] | select(.readiness=="parked" or .readiness=="cut") | .slug' "$catalog")

ready_live_false=0
ready_live_true=0
# bin/ only covers the pre-profile-executor generation of scenarios: 46 of the
# 60 catalog slugs have a script. The newer ones are driven through
# profile-control/trusted_dispatcher instead, so the round-trip below runs over
# the intersection rather than the whole catalog.
while IFS= read -r slug; do
  [[ -f "$script_dir/bin/$slug.sh" ]] || continue
  expected_plan="$(python3 "$script_dir/compile-plan.py" --scenario "$slug")"
  shared_contract=""
  for action in plan run cleanup; do
    output="$("$script_dir/bin/$slug.sh" "--$action")"
    jq -e --arg slug "$slug" --arg action "$action" --argjson expected_plan "$expected_plan" \
      '.side_effects == false and .action == $action and .scenario.slug == $slug and
       .manifest.slug == $slug and .selected_action.mode == "dry-run" and
       .selected_action.mutation == false and .cleanup.required == true and
       .selected_action == .manifest.actions[$action] and
       .normalized_plan == $expected_plan and
       .digests == {
         scenario: $expected_plan.scenario_digest,
         manifest: $expected_plan.manifest_digest,
         registry: $expected_plan.registry_digest,
         plan: $expected_plan.plan_digest
       } and
       .profile_instances == $expected_plan.profile_instances and
       .profile_executor_hashes == ($expected_plan.profile_instances | map({profile_id, executor, executor_sha256})) and
       .observation_query_ids == $expected_plan.observation_query_ids and
       .location_ids == ($expected_plan.profile_instances | map(.location_id)) and
       all(.profile_executor_hashes[]; .executor_sha256 | test("^[0-9a-f]{64}$")) and
       all(.profile_instances[];
         if .location.transport == "kubectl"
         then .location.kubeconfig == "/root/tb-kubeconfig"
         else true end) and
       .normalized_plan.live_allowed == $expected_plan.live_allowed and
       .prerequisite_gate.live_allowed == $expected_plan.live_allowed and
       .capture.create_golden_anomaly == false' \
      <<<"$output" >/dev/null

    current_shared="$(jq -Sc '{normalized_plan,digests,profile_executor_hashes,observation_query_ids,location_ids}' <<<"$output")"
    if [[ -z "$shared_contract" ]]; then
      shared_contract="$current_shared"
    else
      [[ "$current_shared" == "$shared_contract" ]]
    fi
    while IFS= read -r executor; do
      [[ -f "$script_dir/$executor" ]]
    done < <(jq -r '.profile_executor_hashes[].executor' <<<"$output")
  done
  readiness="$(jq -r '.scenario.readiness' <<<"$expected_plan")"
  live_allowed="$(jq -r '.live_allowed' <<<"$expected_plan")"
  if [[ "$readiness" == "ready" && "$live_allowed" == "false" ]]; then
    ready_live_false=$((ready_live_false + 1))
  elif [[ "$readiness" == "ready" && "$live_allowed" == "true" ]]; then
    ready_live_true=$((ready_live_true + 1))
  fi
done < <(jq -r '.scenarios[].slug' "$catalog")
# 21 of the 31 ready scenarios have a bin/ script; every one of them must
# compile to live_allowed == true. The remaining 10 are covered by the
# catalog-level checks above and by tests/test_registry_contracts.py.
# 2026-07-28: 21→22. F03-H 복귀분이 bin/ 스크립트를 가진 ready 집합에 더해졌다.
# 2026-07-28: 23→27. 스토리지 포화 3종과 F15-P가 더해졌고 넷 다 bin/ 스크립트를 갖고 있다.
# 2026-07-28: 27→29. F09-H·F09-P 복귀분.
# 2026-07-28: 29→30. F04-H 승격분(bin/ 스크립트를 이미 갖고 있었다).
# 2026-07-28: 30→29. F07-P 강등분(F20-P는 bin/ 스크립트가 없어 이 카운트에
# 애초에 들어 있지 않았다).
# 2026-07-29: 29→31. F15-H·F15-T2 승격분(둘 다 bin/ 스크립트를 갖고 있다).
# 2026-07-29: 31→32. F14-P 승격분(bin/ 스크립트를 이미 갖고 있었다).
# 2026-08-07: 32→31. c887e20 의 파킹 3종 중 bin/ 스크립트를 가진 것은 F02-H 하나뿐이라
# (F21-P·F21-Q 는 애초에 이 집합 밖) 하나만 빠진다. 8d1ef6b 가 위쪽 ready 개수를 고치자
# 드러난 같은 계열의 세 번째 누락이다 — 게이트가 첫 실패에서 멈추므로 낡은 핀은
# 한 번에 하나씩만 보인다.
[[ $((ready_live_false + ready_live_true)) -eq 31 ]]
[[ "$ready_live_true" -eq 31 ]]
[[ "$ready_live_false" -eq 0 ]]

if "$script_dir/bin/f15-t2-pg-lock-then-food-429.sh" --live 2>/dev/null; then
  echo "blocked scenario unexpectedly passed live fail-closed gate" >&2
  exit 1
fi
if RUNNER_SCRIPT=/bin/true SCENARIO_CATALOG=/tmp/forged.json \
  "$script_dir/bin/f07-h-north-south-surge.sh" --live 2>/dev/null; then
  echo "ready scenario unexpectedly accepted caller-controlled live inputs" >&2
  exit 1
fi

echo "[PASS] 64 scenario entrypoints"
