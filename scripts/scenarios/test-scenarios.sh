#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd -- "$script_dir/../.." && pwd)"
catalog="$script_dir/catalog.json"
manifest_dir="$script_dir/manifests"

[[ "$(jq '.scenarios | length' "$catalog")" -eq 64 ]]
[[ "$(find "$script_dir/bin" -maxdepth 1 -type f -name '*.sh' | wc -l)" -eq 64 ]]
[[ "$(find "$manifest_dir" -maxdepth 1 -type f -name '*.yaml' | wc -l)" -eq 64 ]]
[[ "$(jq '[.scenarios[].id] | unique | length' "$catalog")" -eq 64 ]]
[[ "$(jq '[.scenarios[].slug] | unique | length' "$catalog")" -eq 64 ]]
[[ "$(jq '[.scenarios[] | select(.readiness=="ready")] | length' "$catalog")" -eq 25 ]]
[[ "$(jq '[.scenarios[] | select(.readiness=="partial")] | length' "$catalog")" -eq 11 ]]
[[ "$(jq '[.scenarios[] | select(.readiness=="blocked")] | length' "$catalog")" -eq 28 ]]
[[ "$(jq '[.scenarios[] | select(.load_mode=="adaptive")] | length' "$catalog")" -eq 13 ]]
[[ "$(jq '[.scenarios[] | select(.load_mode=="fixed")] | length' "$catalog")" -eq 47 ]]
[[ "$(jq '[.scenarios[] | select(.load_mode=="no-load")] | length' "$catalog")" -eq 4 ]]
jq -e '
  ["db.lock","db.ddl","db.workload","mock.expectation","load.north_south",
   "load.east_west","k8s.patch","k8s.lifecycle","k8s.resource","k8s.probe",
   "k8s.env","kafka.control","host.stress",
   "cache.control","network.fault","app.release","wpm.probe","business.fault",
   "timeline.compose","timeline.multi"] as $known_profiles |
  all(.scenarios[];
    (.id | test("^F[0-9]{2}-(R|H|P|G|T[1-4])$")) and
    (.slug | test("^[a-z0-9][a-z0-9-]+$")) and
    (.readiness | IN("ready", "partial", "blocked")) and
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
     "wpm-probe-contract", "business-invariant-probe", "subinjection-timeline"] as $known_preflights |
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

ready_live_false=0
ready_live_true=0
while IFS= read -r slug; do
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
[[ $((ready_live_false + ready_live_true)) -eq 25 ]]
[[ "$ready_live_true" -eq 25 ]]
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
