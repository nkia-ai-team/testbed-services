#!/usr/bin/env bash
# Side-effect-free policy tests for capture-eval-case.sh. Only --dry-run is used.

set -Eeuo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CAPTURE="$SCRIPT_DIR/capture-eval-case.sh"
TMP_ROOT=$(mktemp -d)
trap 'rm -rf "$TMP_ROOT"' EXIT

fail() {
  printf '[FAIL] %s\n' "$*" >&2
  exit 1
}

expect_rejected() {
  local name=$1
  shift
  if "$CAPTURE" "$@" --output-root "$TMP_ROOT/cases" --dry-run >"$TMP_ROOT/out" 2>"$TMP_ROOT/err"; then
    fail "$name was accepted"
  fi
}

scenario_metadata=$(jq -cn '{
  title:"Checkout rate-limit propagation",
  description:"External payment 429 propagates to checkout failure.",
  cause:"External payment gateway rate limiting",
  injection_summary:"Return 429 from the payment mock while checkout load is active.",
  user_impact:"Checkout requests fail with 502.",
  distinguishing_evidence:"External 429 is present while database blocking sessions remain zero."
}')
scenario_metadata_sha256=$(printf '%s' "$scenario_metadata" | sha256sum | awk '{print $1}')

base_args=(
  --case-id case-policy-test
  --scenario-id commerce/scenario-policy-test
  --scenario-title "$(jq -r .title <<<"$scenario_metadata")"
  --scenario-description "$(jq -r .description <<<"$scenario_metadata")"
  --scenario-cause "$(jq -r .cause <<<"$scenario_metadata")"
  --scenario-injection-summary "$(jq -r .injection_summary <<<"$scenario_metadata")"
  --scenario-user-impact "$(jq -r .user_impact <<<"$scenario_metadata")"
  --scenario-distinguishing-evidence "$(jq -r .distinguishing_evidence <<<"$scenario_metadata")"
  --scenario-metadata-sha256 "$scenario_metadata_sha256"
  --t1 2026-07-15T01:00:00Z
  --t2 2026-07-15T01:10:00Z
)

mkdir -p "$TMP_ROOT/runs/run-test"
printf '%s\n' '{"plan":"fixed"}' >"$TMP_ROOT/runs/run-test/plan.json"
printf '%s\n' '#!/usr/bin/env bash' >"$TMP_ROOT/scenario.sh"
printf '%s\n' '{"catalog":"test"}' >"$TMP_ROOT/catalog.json"
plan_sha=$(sha256sum "$TMP_ROOT/runs/run-test/plan.json" | awk '{print $1}')
script_sha=$(sha256sum "$TMP_ROOT/scenario.sh" | awk '{print $1}')
catalog_sha=$(sha256sum "$TMP_ROOT/catalog.json" | awk '{print $1}')
jq -n \
  --arg plan_sha "$plan_sha" \
  --arg script_sha "$script_sha" \
  --arg catalog_sha "$catalog_sha" \
  --argjson scenario_metadata "$scenario_metadata" \
  --arg scenario_metadata_sha256 "$scenario_metadata_sha256" \
  --arg script_path "$TMP_ROOT/scenario.sh" \
  --arg catalog_path "$TMP_ROOT/catalog.json" '{
  mode:"evaluation", outcome:"succeeded", dirty:false,
  case_id:"case-policy-test", scenario_id:"commerce/scenario-policy-test",
  scenario_metadata:$scenario_metadata,
  scenario_metadata_sha256:$scenario_metadata_sha256,
  t1:"2026-07-15T01:00:00Z", t2:"2026-07-15T01:10:00Z",
  profile:{kind:"fixed", id:"approved-l1"}, approved_profile_id:"approved-l1",
  cleanup:{status:"succeeded"}, recovery:{status:"succeeded"},
  plan_sha256:$plan_sha, script_sha256:$script_sha, catalog_sha256:$catalog_sha,
  script_path:$script_path, catalog_path:$catalog_path
}' >"$TMP_ROOT/runs/run-test/result.json"
chmod 600 "$TMP_ROOT/runs/run-test/result.json"
export CAPTURE_DRY_RUN_TRUSTED_RUNS_ROOT="$TMP_ROOT/runs"

# Clean preflight verdict for [t1-10m, t1] (spec §2.1) — required for evaluation.
clean_preflight="$TMP_ROOT/preflight-clean.json"
jq -n '{window:["2026-07-15T00:50:00Z","2026-07-15T01:00:00Z"], verdict:"clean",
  checked_at:"2026-07-15T01:00:05Z", waited_sec:0, ai_judgement:null,
  checks:[{name:"baseline_loadgen_alive", value:1, threshold:1, pass:true}]}' >"$clean_preflight"

for label in calibration evaluation failed; do
  label_args=()
  [[ "$label" != evaluation ]] ||
    label_args=(--run-result "$TMP_ROOT/runs/run-test/result.json" --preflight-json "$clean_preflight")
  output=$("$CAPTURE" "${base_args[@]}" --case-label "$label" "${label_args[@]}" \
    --output-root "$TMP_ROOT/cases" --dry-run)
  jq -e \
    --arg case_label "$label" \
    '.mode == "dry-run" and .schema_version == "1.3" and .case_label == $case_label and
     .capture_start == "2026-07-15T00:50:00Z" and
     .capture_end == "2026-07-15T01:30:00Z" and
     .capture_start_kst == "2026-07-15T09:50:00+09:00" and
     .model_snapshot_not_before == .capture_end and
     (.segments | type == "array" and .[0].role == "scenario") and
     .golden_anomaly_file == false and
     .evaluation_eligible == ($case_label == "evaluation")' \
    <<<"$output" >/dev/null || fail "unexpected dry-run policy for label=$label"
done

# Evaluation requires a preflight verdict, and it must be clean.
expect_rejected evaluation-without-preflight \
  "${base_args[@]}" --case-label evaluation --run-result "$TMP_ROOT/runs/run-test/result.json"
dirty_preflight="$TMP_ROOT/preflight-dirty.json"
jq '.verdict = "dirty"' "$clean_preflight" >"$dirty_preflight"
expect_rejected evaluation-with-dirty-preflight \
  "${base_args[@]}" --case-label evaluation --run-result "$TMP_ROOT/runs/run-test/result.json" \
  --preflight-json "$dirty_preflight"

[[ ! -e "$TMP_ROOT/cases" ]] || fail 'dry-run created the output root'

expect_rejected offset-time \
  --case-id case-policy-test --scenario-id commerce/test \
  --t1 2026-07-15T01:00:00+00:00 --t2 2026-07-15T01:10:00Z
expect_rejected invalid-calendar-date \
  --case-id case-policy-test --scenario-id commerce/test \
  --t1 2026-02-30T01:00:00Z --t2 2026-03-01T01:10:00Z
expect_rejected reversed-window \
  --case-id case-policy-test --scenario-id commerce/test \
  --t1 2026-07-15T01:10:00Z --t2 2026-07-15T01:00:00Z
expect_rejected unknown-label \
  "${base_args[@]}" --case-label production
expect_rejected evaluation-without-result \
  "${base_args[@]}" --case-label evaluation

mkdir -p "$TMP_ROOT/runs/run-failed"
cp "$TMP_ROOT/runs/run-test/plan.json" "$TMP_ROOT/runs/run-failed/plan.json"
jq '.cleanup.status = "failed"' "$TMP_ROOT/runs/run-test/result.json" >"$TMP_ROOT/runs/run-failed/result.json"
chmod 600 "$TMP_ROOT/runs/run-failed/result.json"
expect_rejected evaluation-with-failed-cleanup \
  "${base_args[@]}" --case-label evaluation --run-result "$TMP_ROOT/runs/run-failed/result.json"

expect_rejected evaluation-scenario-mismatch \
  --case-id case-policy-test --scenario-id commerce/other \
  --t1 2026-07-15T01:00:00Z --t2 2026-07-15T01:10:00Z \
  --case-label evaluation --run-result "$TMP_ROOT/runs/run-test/result.json"

mkdir -p "$TMP_ROOT/runs/run-badhash"
cp "$TMP_ROOT/runs/run-test/plan.json" "$TMP_ROOT/runs/run-badhash/plan.json"
jq '.plan_sha256 = ("0" * 64)' "$TMP_ROOT/runs/run-test/result.json" >"$TMP_ROOT/runs/run-badhash/result.json"
chmod 600 "$TMP_ROOT/runs/run-badhash/result.json"
expect_rejected evaluation-plan-hash-mismatch \
  "${base_args[@]}" --case-label evaluation --run-result "$TMP_ROOT/runs/run-badhash/result.json"

# ------------------------------------------------------------
# v3 (continuous-cycle, schema 2.0, spec §2.2) — --phases-json.
# Timeline: normal.start=T-2h10m, buffer=[T-10m,T), injection=[T,T+10m)=t1/t2,
# cooldown=[T+10m,T+40m). capture_start=normal.start, capture_end=t2+30m.
# ------------------------------------------------------------
phases_json="$TMP_ROOT/phases.json"
jq -n '{
  schema_version:"2.0", run_id:"run-cycle-test", scenario_id:"commerce/scenario-policy-test",
  timeline:"continuous",
  capture_start:"2026-07-14T22:50:00Z", capture_end:"2026-07-15T01:40:00Z",
  phases: [
    {phase:"trainer_reset", at:"2026-07-14T22:50:00Z", golden_id:"golden-01", golden_sha256:("a"*64)},
    {phase:"normal", start:"2026-07-14T22:50:00Z", end:"2026-07-15T00:50:00Z"},
    {phase:"buffer", start:"2026-07-15T00:50:00Z", end:"2026-07-15T01:00:00Z"},
    {phase:"injection", start:"2026-07-15T01:00:00Z", end:"2026-07-15T01:10:00Z"},
    {phase:"cooldown", start:"2026-07-15T01:10:00Z", end:"2026-07-15T01:40:00Z"}
  ]
}' >"$phases_json"

# (a) phases-json input reflected: capture_start/capture_end/schema 2.0/phases[].
output=$("$CAPTURE" "${base_args[@]}" --case-label calibration --phases-json "$phases_json" \
  --output-root "$TMP_ROOT/cases" --dry-run)
jq -e '
  .mode == "dry-run" and .schema_version == "2.0" and .timeline == "continuous" and
  .capture_start == "2026-07-14T22:50:00Z" and
  .capture_end == "2026-07-15T01:40:00Z" and
  .capture_start_kst == "2026-07-15T07:50:00+09:00" and
  (.phases | type == "array" and length == 5) and
  (.phases[] | select(.phase == "trainer_reset") | .at_kst | length > 0) and
  (.phases[] | select(.phase == "injection") | .start == "2026-07-15T01:00:00Z" and .end == "2026-07-15T01:10:00Z") and
  .evaluation_eligible == false' \
  <<<"$output" >/dev/null || fail 'unexpected dry-run output for --phases-json (v3)'

# (b) --phases-json and --normal-segment are mutually exclusive.
normal_segment_dir="$TMP_ROOT/normal-segments/commerce/2026-07-14"
mkdir -p "$normal_segment_dir"
jq -n '{segment_start:"2026-07-14T00:00:00Z", segment_end:"2026-07-14T02:00:00Z"}' \
  >"$normal_segment_dir/meta.json"
expect_rejected phases-json-with-normal-segment \
  "${base_args[@]}" --case-label calibration --phases-json "$phases_json" \
  --normal-segment "$normal_segment_dir"

# (c) injection.start/.end must equal --t1/--t2.
bad_injection_start="$TMP_ROOT/phases-bad-injection-start.json"
jq '(.phases[] | select(.phase == "injection") | .start) = "2026-07-15T00:59:00Z"' "$phases_json" \
  >"$bad_injection_start"
expect_rejected phases-json-injection-start-mismatch \
  "${base_args[@]}" --case-label calibration --phases-json "$bad_injection_start"

bad_injection_end="$TMP_ROOT/phases-bad-injection-end.json"
jq '(.phases[] | select(.phase == "injection") | .end) = "2026-07-15T01:11:00Z"' "$phases_json" \
  >"$bad_injection_end"
expect_rejected phases-json-injection-end-mismatch \
  "${base_args[@]}" --case-label calibration --phases-json "$bad_injection_end"

# golden_sha256 may be null (restore has not emitted a digest yet — known
# TODO on the runner side); this must still be accepted, not rejected.
null_golden_sha="$TMP_ROOT/phases-null-golden-sha.json"
jq '(.phases[] | select(.phase == "trainer_reset") | .golden_sha256) = null' "$phases_json" \
  >"$null_golden_sha"
output=$("$CAPTURE" "${base_args[@]}" --case-label calibration --phases-json "$null_golden_sha" \
  --output-root "$TMP_ROOT/cases" --dry-run)
jq -e '
  .mode == "dry-run" and .schema_version == "2.0" and
  (.phases[] | select(.phase == "trainer_reset") | .golden_sha256 == null)' \
  <<<"$output" >/dev/null || fail 'phases-json with golden_sha256=null should be accepted'

# ------------------------------------------------------------
# Topology periodic bundle — --topology-bundle (spec §2.2, EventCluster
# contract). Fixture manifest matches the spec's field names exactly:
# schema_version, capture_interval_seconds, capture_failures[],
# snapshots[].{captured_at, graph[]{path,request_url,http_status,sha256},
# service_tree{path,request_url,http_status,sha256}}.
# ------------------------------------------------------------
make_topology_bundle() {
  local dir=$1
  shift
  local -a captured_ats=("$@")
  mkdir -p "$dir/graph" "$dir/service-tree"
  local idx=0 snapshots_json='[]' ts graph_file tree_file graph_sha tree_sha
  for ts in "${captured_ats[@]}"; do
    idx=$((idx + 1))
    graph_file="graph/${ts}-part-001.json"
    tree_file="service-tree/${ts}.json"
    printf '{"nodes":[],"edges":[],"sources":[],"snapshot":%d}\n' "$idx" >"$dir/$graph_file"
    printf '{"tree":[],"snapshot":%d}\n' "$idx" >"$dir/$tree_file"
    graph_sha=$(sha256sum "$dir/$graph_file" | awk '{print $1}')
    tree_sha=$(sha256sum "$dir/$tree_file" | awk '{print $1}')
    snapshots_json=$(jq -c \
      --arg ts "$ts" \
      --arg gpath "$graph_file" --arg gsha "$graph_sha" \
      --arg tpath "$tree_file" --arg tsha "$tree_sha" \
      '. + [{captured_at:$ts,
             graph:[{path:$gpath, request_url:"http://query-api/api/v1/topology/graph", http_status:200, sha256:$gsha}],
             service_tree:{path:$tpath, request_url:"http://query-api/api/v1/asset-tree/service/unified", http_status:200, sha256:$tsha}}]' \
      <<<"$snapshots_json")
  done
  jq -n --argjson snapshots "$snapshots_json" \
    '{schema_version:"1", case_id:null, capture_interval_seconds:30, capture_failures:[], snapshots:$snapshots}' \
    >"$dir/manifest.json"
}

good_topology_bundle="$TMP_ROOT/topology-bundle-good"
make_topology_bundle "$good_topology_bundle" \
  "2026-07-15T00-55-00Z" "2026-07-15T01-05-00Z" "2026-07-15T01-15-00Z"
# manifest captured_at must be the strict YYYY-MM-DDTHH:MM:SSZ form the
# capture script requires; filenames above avoid ':' for portability, so
# rewrite captured_at (not the paths/hashes) to the colonized timestamps.
jq '.snapshots |= [
  (.[0] | .captured_at = "2026-07-15T00:55:00Z"),
  (.[1] | .captured_at = "2026-07-15T01:05:00Z"),
  (.[2] | .captured_at = "2026-07-15T01:15:00Z")
]' "$good_topology_bundle/manifest.json" >"$good_topology_bundle/manifest.json.tmp"
mv "$good_topology_bundle/manifest.json.tmp" "$good_topology_bundle/manifest.json"

# (a) normal ingestion: dry-run reflects topology_bundle summary in meta.
output=$("$CAPTURE" "${base_args[@]}" --case-label calibration --phases-json "$phases_json" \
  --topology-bundle "$good_topology_bundle" --output-root "$TMP_ROOT/cases" --dry-run)
jq -e '
  .mode == "dry-run" and .schema_version == "2.0" and
  .topology_bundle.path == "topology/" and
  .topology_bundle.snapshot_count == 3 and
  .topology_bundle.capture_failure_count == 0 and
  .topology_bundle.interval_seconds == 30' \
  <<<"$output" >/dev/null || fail 'unexpected dry-run output for --topology-bundle (good bundle)'

# (b) sha256 mismatch is rejected.
bad_sha_bundle="$TMP_ROOT/topology-bundle-bad-sha"
cp -r "$good_topology_bundle" "$bad_sha_bundle"
jq '(.snapshots[0].graph[0].sha256) = ("0" * 64)' "$bad_sha_bundle/manifest.json" \
  >"$bad_sha_bundle/manifest.json.tmp"
mv "$bad_sha_bundle/manifest.json.tmp" "$bad_sha_bundle/manifest.json"
expect_rejected topology-bundle-sha-mismatch \
  "${base_args[@]}" --case-label calibration --phases-json "$phases_json" \
  --topology-bundle "$bad_sha_bundle"

# (c) no snapshot covering the injection window [t1, t2] is rejected.
no_coverage_bundle="$TMP_ROOT/topology-bundle-no-coverage"
make_topology_bundle "$no_coverage_bundle" "2026-07-15T00-30-00Z" "2026-07-15T00-40-00Z"
jq '.snapshots |= [
  (.[0] | .captured_at = "2026-07-15T00:30:00Z"),
  (.[1] | .captured_at = "2026-07-15T00:40:00Z")
]' "$no_coverage_bundle/manifest.json" >"$no_coverage_bundle/manifest.json.tmp"
mv "$no_coverage_bundle/manifest.json.tmp" "$no_coverage_bundle/manifest.json"
expect_rejected topology-bundle-no-coverage \
  "${base_args[@]}" --case-label calibration --phases-json "$phases_json" \
  --topology-bundle "$no_coverage_bundle"

# (d) --topology-bundle omitted: fail-open (warns on stderr, still succeeds,
# meta.topology_bundle is null).
output=$("$CAPTURE" "${base_args[@]}" --case-label calibration --phases-json "$phases_json" \
  --output-root "$TMP_ROOT/cases" --dry-run 2>"$TMP_ROOT/topology-bundle-missing.err")
jq -e '.topology_bundle == null' <<<"$output" >/dev/null ||
  fail 'expected topology_bundle == null when --topology-bundle is omitted'
grep -q 'topology-bundle not supplied' "$TMP_ROOT/topology-bundle-missing.err" ||
  fail 'expected a fail-open warning when --topology-bundle is omitted'

# --topology-bundle requires v3 (--phases-json); v2 calls must reject it.
expect_rejected topology-bundle-without-phases-json \
  --case-id case-policy-test --scenario-id commerce/test \
  --t1 2026-07-15T01:00:00Z --t2 2026-07-15T01:10:00Z \
  --topology-bundle "$good_topology_bundle"

printf '[PASS] capture policy dry-run tests\n'
