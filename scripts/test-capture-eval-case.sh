#!/usr/bin/env bash
# Side-effect-free policy tests for capture-eval-case.sh. Mostly --dry-run; the v4
# discovery section at the end runs the live path against fake curl and docker on
# PATH (nothing leaves the machine).

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
  (.phases[] | select(.phase == "trainer_reset") | .golden_id == "golden-01" and .golden_sha256 == ("a"*64)) and
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

# golden_sha256 may be null (a restore script or runner that predates the
# digest); this must still be accepted, not rejected.
null_golden_sha="$TMP_ROOT/phases-null-golden-sha.json"
jq '(.phases[] | select(.phase == "trainer_reset") | .golden_sha256) = null' "$phases_json" \
  >"$null_golden_sha"
output=$("$CAPTURE" "${base_args[@]}" --case-label calibration --phases-json "$null_golden_sha" \
  --output-root "$TMP_ROOT/cases" --dry-run)
jq -e '
  .mode == "dry-run" and .schema_version == "2.0" and
  (.phases[] | select(.phase == "trainer_reset") | .golden_sha256 == null)' \
  <<<"$output" >/dev/null || fail 'phases-json with golden_sha256=null should be accepted'

# A golden_sha256 that is a string but not a sha256 hex digest is rejected: the
# recording meta must not carry something that cannot be checked against the dump.
bad_golden_sha="$TMP_ROOT/phases-bad-golden-sha.json"
jq '(.phases[] | select(.phase == "trainer_reset") | .golden_sha256) = "not-a-digest"' "$phases_json" \
  >"$bad_golden_sha"
expect_rejected phases-json-golden-sha256-not-hex \
  "${base_args[@]}" --case-label calibration --phases-json "$bad_golden_sha"

# Evaluation-label integrity guard (spec §2.2, 2026-07-25): a shortened normal
# lead-in (< contract 2h) must downgrade evaluation -> calibration, not mint a
# scoring-grade case. The 2026-07-24 overnight incident regression guard.
short_normal="$TMP_ROOT/phases-short-normal.json"
jq '(.phases[] | select(.phase == "normal") | .start) = "2026-07-15T00:49:00Z"' "$phases_json" \
  >"$short_normal"  # normal now 60s instead of 2h; injection/t1/t2 unchanged
output=$("$CAPTURE" "${base_args[@]}" --case-label evaluation --phases-json "$short_normal" \
  --output-root "$TMP_ROOT/cases" --dry-run 2>/dev/null)
jq -e '.case_label == "calibration" and .evaluation_eligible == false' \
  <<<"$output" >/dev/null || fail 'short normal lead-in must downgrade evaluation -> calibration'

# The full 2h normal lead-in must NOT be downgraded — it stays evaluation
# (and then fails for the missing run-result, proving the label survived).
expect_rejected full-normal-evaluation-needs-result \
  "${base_args[@]}" --case-label evaluation --phases-json "$phases_json"

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

# Scenario notes (정답 칸 밖 참고 메모): carried as meta.json scenario_notes, never folded
# into scenario_metadata or its hash; absent when not supplied; malformed notes rejected.
notes_file="$TMP_ROOT/scenario-notes.json"
jq -n '{beyond_data:{detail:"Allocator arenas retain freed memory.",
  missing_data:"MySQL internal memory total is not collected.",
  confirmed_by:"sys.memory_global_total against process smaps."},
  beyond_data_ko:{detail:"할당자 arena 가 반납된 메모리를 쥐고 있다.",
  missing_data:"MySQL 자체 메모리 합계가 수집되지 않는다.",
  confirmed_by:"sys.memory_global_total 과 프로세스 smaps 대조."}}' >"$notes_file"
for mode_args in "" "--phases-json $phases_json"; do
  # shellcheck disable=SC2086
  output=$("$CAPTURE" "${base_args[@]}" --case-label calibration $mode_args \
    --scenario-notes-file "$notes_file" --output-root "$TMP_ROOT/cases" --dry-run)
  jq -e --slurpfile notes "$notes_file" --argjson scenario_metadata "$scenario_metadata" \
    --arg sha "$scenario_metadata_sha256" '
    .scenario_notes == $notes[0] and .scenario_metadata == $scenario_metadata and
    .scenario_metadata_sha256 == $sha' \
    <<<"$output" >/dev/null || fail "scenario notes not carried as scenario_notes (${mode_args:-v2})"
  # shellcheck disable=SC2086
  output=$("$CAPTURE" "${base_args[@]}" --case-label calibration $mode_args \
    --output-root "$TMP_ROOT/cases" --dry-run)
  jq -e 'has("scenario_notes") | not' <<<"$output" >/dev/null ||
    fail "scenario_notes present without --scenario-notes-file (${mode_args:-v2})"
done
jq '.beyond_data |= del(.confirmed_by)' "$notes_file" >"$TMP_ROOT/notes-missing.json"
expect_rejected scenario-notes-missing-field \
  "${base_args[@]}" --scenario-notes-file "$TMP_ROOT/notes-missing.json"
jq '. + {cause:"leak"}' "$notes_file" >"$TMP_ROOT/notes-extra.json"
expect_rejected scenario-notes-unknown-key \
  "${base_args[@]}" --scenario-notes-file "$TMP_ROOT/notes-extra.json"
jq '.beyond_data.detail = ""' "$notes_file" >"$TMP_ROOT/notes-empty.json"
expect_rejected scenario-notes-empty-text \
  "${base_args[@]}" --scenario-notes-file "$TMP_ROOT/notes-empty.json"

# Decoy deployments (testbed v4 U7): carried verbatim as meta.decoy_deployments,
# outside scenario_metadata and its hash; every other meta key is unchanged; the
# key is absent without the flag; a malformed record is rejected.
decoys_file="$TMP_ROOT/decoys.json"
jq -n '{schema_version:1, scenario_id:"F01-R", seed:42, policy_sha256:null,
  cycle_start:"2026-07-15T00:00:00Z",
  decoys:[{id:"d1", window:"normal", planned_at:"2026-07-15T00:20:00Z", kind:"restart",
    namespace:"rca-testbed-commerce", deployment:"testbed-order", relation:"off_domain",
    value:"x", status:"applied"}]}' >"$decoys_file"
for mode_args in "" "--phases-json $phases_json"; do
  # shellcheck disable=SC2086
  with_decoys=$("$CAPTURE" "${base_args[@]}" --case-label calibration $mode_args \
    --decoys-json "$decoys_file" --output-root "$TMP_ROOT/cases" --dry-run 2>/dev/null)
  # shellcheck disable=SC2086
  without=$("$CAPTURE" "${base_args[@]}" --case-label calibration $mode_args \
    --output-root "$TMP_ROOT/cases" --dry-run 2>/dev/null)
  jq -e --slurpfile decoys "$decoys_file" --argjson without "$without" '
    .decoy_deployments == $decoys[0] and del(.decoy_deployments) == $without' \
    <<<"$with_decoys" >/dev/null || fail "decoys not carried as decoy_deployments (${mode_args:-v2})"
  jq -e 'has("decoy_deployments") | not' <<<"$without" >/dev/null ||
    fail "decoy_deployments present without --decoys-json (${mode_args:-v2})"
done
jq '.decoys[0] |= del(.status)' "$decoys_file" >"$TMP_ROOT/decoys-missing.json"
expect_rejected decoys-missing-field \
  "${base_args[@]}" --decoys-json "$TMP_ROOT/decoys-missing.json"
grep -q -- '--decoys-json must be' "$TMP_ROOT/err" || fail 'decoys-missing-field rejected for another reason'
jq '.schema_version = 2' "$decoys_file" >"$TMP_ROOT/decoys-version.json"
expect_rejected decoys-unknown-version \
  "${base_args[@]}" --decoys-json "$TMP_ROOT/decoys-version.json"
grep -q -- '--decoys-json must be' "$TMP_ROOT/err" || fail 'decoys-unknown-version rejected for another reason'

# ------------------------------------------------------------
# v4 자동 발견 (2026-11-10 R1, spec §2.3). 라이브 경로를 가짜 curl, docker 로 돌린다.
# 확인하는 것: v3 13개 CH 표의 이름, 파일 자리, 형식, meta 항목 불변(실제 v3 녹화본
# case-f72-r-v3-bf50d2b7 의 meta 와 같은 모양), 13개 밖 표 자동 발견과 뒤에 붙이기, 제외 목록,
# 크기 상한, PG 카탈로그 계획(fk_closure, history, over_cap, 없는 자기참조 열), 제품 버전 칸.
# ------------------------------------------------------------
live_root="$TMP_ROOT/live"
fakebin="$live_root/bin"
fix="$live_root/fixtures"
mkdir -p "$fakebin" "$fix/pg" "$fix/ch" "$live_root/cases"
export FAKE_LOG="$live_root/fake.log"
: >"$FAKE_LOG"

# 원천 PG 카탈로그. incidents 에 superseded_by 가 없다(develop-ai DDL 367).
printf '%s\n' \
  $'public\tincidents\tr\t81920\t10\t10\t0' \
  $'public\tevent_clusters\tr\t81920\t10\t10\t0' \
  $'public\tincident_members\tr\t40960\t10\t10\t0' \
  $'public\talert_history\tr\t40960\t10\t10\t0' \
  $'public\talert_active\tr\t40960\t10\t10\t0' \
  $'public\tincident_event_cluster_successors\tr\t40960\t10\t10\t0' \
  $'public\ttargets\tr\t40960\t10\t10\t0' \
  $'public\tincident_snapshots\tr\t40960\t10\t10\t0' \
  $'public\ttarget_config_history\tp\t40960\t0\t\\N\t\\N' \
  $'public\ttarget_config_latest\tr\t40960\t10\t10\t0' \
  $'public\tbig_unknown_table\tr\t314572800\t10\t10\t0' \
  $'public\ttrainer_trace_window_threshold\tr\t2983190528\t3400571\t3400571\t0' \
  $'public\tcollector_sms_process_snapshot\tr\t2500000000\t10\t10\t0' \
  $'public\tautomation_dist_artifacts\tr\t2600755200\t20\t20\t0' \
  $'security\taudit_marks\tr\t24576\t0\t0\t0' >"$fix/pg/tables.tsv"
printf '%s\n' \
  $'incidents\tid\tuuid' $'incidents\tcreated_at\ttimestamp with time zone' \
  $'event_clusters\tid\tuuid' $'event_clusters\tcreated_at\ttimestamp with time zone' $'event_clusters\tmerged_into\tuuid' \
  $'incident_members\tincident_id\tuuid' \
  $'alert_history\tid\tbigint' $'alert_history\tstarted_at\ttimestamp with time zone' \
  $'targets\tid\tuuid' $'targets\tcreated_at\ttimestamp with time zone' \
  $'incident_snapshots\tincident_id\tuuid' $'incident_snapshots\tcaptured_at\ttimestamp with time zone' \
  $'target_config_history\tchanged_at\ttimestamp with time zone' \
  $'target_config_latest\tchanged_at\ttimestamp with time zone' \
  $'big_unknown_table\tpayload\tbytea' \
  $'trainer_trace_window_threshold\tagent_id\ttext' \
  $'trainer_trace_window_threshold\tversion\tinteger' \
  $'trainer_trace_window_threshold\tvalid_from\ttimestamp with time zone' \
  $'trainer_trace_window_threshold\tactive\tboolean' \
  $'alert_active\talert_history_id\tbigint' \
  $'incident_event_cluster_successors\tpredecessor_incident_id\tuuid' \
  $'incident_event_cluster_successors\tsuccessor_incident_id\tuuid' \
  $'incident_event_cluster_successors\tsuccessor_cluster_id\tuuid' >"$fix/pg/columns.tsv"
printf '%s\n' \
  $'incident_members\tincident_id\tincidents\tid' \
  $'incident_snapshots\tincident_id\tincidents\tid' \
  $'incident_snapshots\ttarget_id\ttargets\tid' \
  $'alert_active\talert_history_id\talert_history\tid' \
  $'incident_event_cluster_successors\tpredecessor_incident_id\tincidents\tid' \
  $'incident_event_cluster_successors\tsuccessor_incident_id\tincidents\tid' \
  $'incident_event_cluster_successors\tsuccessor_cluster_id\tevent_clusters\tid' >"$fix/pg/fks.tsv"

# 원천 CH system 표. v3 13개 + 13개 밖 표.
ch_row() { printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$@"; }
{
  for t in otel_traces_local lucida_logs_local dpm_session_local dpm_topsql_local kcm_events_local \
           host_connections syslog_local; do
    ch_row lucida "$t" MergeTree 'toYYYYMMDD(timestamp)' timestamp 10 1000
  done
  ch_row lucida lucida_events_local ReplacingMergeTree 'modulo(cityHash64(event_id), 16)' occurred_at 10 1000
  ch_row lucida event_cluster_projection_local ReplacingMergeTree 'modulo(cityHash64(event_id), 16)' assigned_at 10 1000
  ch_row lucida process_snapshot MergeTree 'toDate(ts)' ts 10 1000
  ch_row lucida process_meta ReplacingMergeTree '' 'target_id, proc_key' 10 1000
  ch_row lucida trace_error_chains_local ReplacingMergeTree 'toYYYYMMDD(window_start)' window_start 10 1000
  ch_row lucida trace_path_signatures_local ReplacingMergeTree 'toYYYYMMDD(window_start)' window_start 10 1000
  ch_row lucida kcm_pod_logs_local MergeTree 'toYYYYMMDD(timestamp)' 'cluster, timestamp' 10 1000
  ch_row lucida agg_service_live_5s AggregatingMergeTree 'toStartOfHour(minute)' 'service, minute' 10 1000
  ch_row lucida agg_event_occurrence_1d MergeTree day day 0 0
  ch_row lucida wpm_text_local ReplacingMergeTree xtype 'xtype, hash' 0 1000
  ch_row lucida huge_state_table ReplacingMergeTree '' key 10 104857600
  ch_row lucida fails_parquet_local MergeTree 'toYYYYMMDD(timestamp)' timestamp 10 1000
  ch_row lucida fails_always_local MergeTree 'toYYYYMMDD(timestamp)' timestamp 10 1000
  ch_row lucida fails_timeout_local MergeTree 'toYYYYMMDD(timestamp)' timestamp 10 1000
  ch_row lucida apm_sql_texts_local ReplacingMergeTree 'toYYYYMMDD(timestamp)' 'service_name, timestamp_date, sql_dictionary_ref' 10 1000
  ch_row lucida events_by_hash_local MergeTree 'modulo(cityHash64(id), 16)' 'ts, id' 10 1000
  ch_row lucida mv_traces_to_live_5s MaterializedView '' '' '\N' '\N'
  ch_row lucida otel_traces_ingest 'Null' '' '' '\N' '\N'
  ch_row lucida '.inner_id.63da7c0d' AggregatingMergeTree 'toYYYYMM(minute)' minute 10 1000
  ch_row lucida schema_migrations ReplacingMergeTree '' version 10 1000
  ch_row lucida_selfsystem otel_traces_local MergeTree 'toYYYYMMDD(timestamp)' timestamp 10 1000
} >"$fix/ch/tables.tsv"
{
  for t in otel_traces_local lucida_logs_local dpm_session_local dpm_topsql_local kcm_events_local \
           host_connections syslog_local kcm_pod_logs_local fails_parquet_local fails_always_local \
           fails_timeout_local; do
    printf 'lucida\t%s\ttimestamp\tDateTime64(9)\n' "$t"
  done
  printf 'lucida\tlucida_events_local\tevent_id\tUUID\n'
  printf 'lucida\tlucida_events_local\toccurred_at\tDateTime64(3)\n'
  printf 'lucida\tagg_service_live_5s\tminute\tDateTime\n'
  printf 'lucida\tagg_service_live_5s\tlatency\tAggregateFunction(quantilesTDigest(0.5, 0.95, 0.99), Float64)\n'
  printf 'lucida\tagg_event_occurrence_1d\tday\tDate\n'
  printf 'lucida\twpm_text_local\treceived_at\tDateTime\n'
  printf 'lucida\thuge_state_table\tvalue\tDateTime\n'
  printf 'lucida_selfsystem\totel_traces_local\ttimestamp\tDateTime64(9)\n'
  printf 'lucida_selfsystem\totel_traces_local\ttrace_uuid\tNullable(UUID)\n'
  printf 'lucida\tapm_sql_texts_local\ttimestamp\tDateTime64(9)\t\t\n'
  printf 'lucida\tapm_sql_texts_local\ttimestamp_date\tDate\tMATERIALIZED\ttoDate(timestamp)\n'
  printf 'lucida\tevents_by_hash_local\tts\tDateTime\n'
} >"$fix/ch/columns.tsv"
printf '%s\n' \
  $'lucida\tmv_traces_to_live_5s\tlucida.agg_service_live_5s\t' \
  $'lucida\totel_traces_ingest\t\tlucida.mv_traces_to_live_5s' >"$fix/ch/mviews.tsv"

cat >"$fakebin/curl" <<'PYEOF'
#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
out, data, url, jar = None, None, None, None
i = 0
stdin_used = False
writeout = None
while i < len(args):
    a = args[i]
    if a == "--output":
        out = args[i + 1]; i += 2; continue
    if a == "--cookie-jar":
        jar = args[i + 1]; i += 2; continue
    if a == "--write-out":
        writeout = args[i + 1]; i += 2; continue
    if a in ("--data-binary", "--data-urlencode", "-H", "--cookie", "--max-time", "-X"):
        if a == "--data-binary":
            data = args[i + 1]
        i += 2; continue
    if a == "--config":
        sys.stdin.read(); stdin_used = True; i += 2; continue
    if a.startswith("http"):
        url = a
    i += 1
if data == "@-":
    data = sys.stdin.read()
log = open(os.environ["FAKE_LOG"], "a")
log.write(f"CURL {url}\t{(data or '').replace(chr(10), ' ')}\n")
fix = os.environ["FAKE_FIX"]
def emit(body):
    if out and out != "-":
        open(out, "wb").write(body if isinstance(body, bytes) else body.encode())
    else:
        sys.stdout.write(body if isinstance(body, str) else body.decode())
if jar:
    open(jar, "w").write("cookie")
if "fake-vm" in url:
    emit("".join('{"metric":{"__name__":"m%d"},"values":[1],"timestamps":[1]}\n' % n for n in range(3)))
elif url.endswith("/api/v1/login"):
    emit("{}")
elif url.endswith("/api/v1/product/version"):
    if os.environ.get("FAKE_PV_FAIL"):
        sys.stderr.write("curl: (22) The requested URL returned error: 404\n"); sys.exit(22)
    emit('{"version":"2026.11.09-01","branch":"develop-ai"}')
elif "/api/v1/topology/graph" in url:
    emit('{"nodes":[],"edges":[]}')
elif "/api/v1/asset-tree/service/unified" in url:
    emit('{"tree":[]}')
elif "fake-ch" in url:
    q = data or ""
    if "FROM system.tables" in q and "partition_key" in q:
        emit(open(f"{fix}/ch/tables.tsv").read())
    elif "FROM system.columns" in q:
        emit(open(f"{fix}/ch/columns.tsv").read())
    elif "create_table_query" in q:
        emit(open(f"{fix}/ch/mviews.tsv").read())
    elif "FROM system.parts" in q:
        emit("otel_traces_local\nkcm_pod_logs_local\n")
    elif "fails_timeout_local" in q and "FORMAT Parquet" in q:
        emit("Code: 159. DB::Exception: Timeout exceeded: elapsed 600 seconds: While executing ParquetBlockOutputFormat. (TIMEOUT_EXCEEDED)\n")
        sys.stdout.write("500"); sys.exit(0)
    elif "fails_always_local" in q or ("fails_parquet_local" in q and "FORMAT Parquet" in q):
        emit("Code: 50. DB::Exception: Internal type of column is not supported for conversion into Parquet data format. (UNKNOWN_TYPE)\n")
        sys.stdout.write("500"); sys.exit(0)
    elif "FORMAT Parquet" in q:
        emit(b"PAR1fake")
    elif "FORMAT Native" in q:
        emit(b"NATIVEfake")
    elif "FORMAT TSV" in q:
        emit("5\t2026-07-15 01:00:00\t2026-07-15 01:05:00\n")
    else:
        sys.stderr.write("fake curl: unknown ch query\n"); sys.exit(2)
else:
    sys.stderr.write(f"fake curl: unknown url {url}\n"); sys.exit(2)
if writeout:
    sys.stdout.write("200")
PYEOF
cat >"$fakebin/docker" <<'PYEOF'
#!/usr/bin/env python3
import os, re, sys
args = sys.argv[1:]
log = open(os.environ["FAKE_LOG"], "a")
log.write("DOCKER " + " ".join(args) + "\n")
host = None
for i, a in enumerate(args):
    if a == "--volume":
        host = args[i + 1].rsplit(":/out", 1)[0]
def real(p):
    return p.replace("/out", host, 1)
fix = os.environ["FAKE_FIX"]
if "pg_dump" in args:
    f = args[args.index("--file") + 1]
    open(real(f), "wb").write(b"PGDMPfake")
    sys.exit(0)
if "psql" in args:
    sqlf = real(args[args.index("--file") + 1])
    sql = open(sqlf).read()
    log.write("PSQLFILE " + os.path.basename(sqlf) + "\n" + sql + "\nENDPSQL\n")
    for target in re.findall(r"^\\o (\S+)$", sql, re.M):
        name = os.path.basename(target)
        if name.endswith(".tsv"):
            data = open(f"{fix}/pg/{name}").read()
        else:
            data = "id\n1\n2\n"
        open(real(target), "w").write(data)
    sys.exit(0)
sys.stderr.write("fake docker: unexpected call\n"); sys.exit(2)
PYEOF
chmod +x "$fakebin/curl" "$fakebin/docker"
printf '{"model":"fake"}\n' >"$live_root/model.json"

run_live() {
  local case_id=$1; shift
  env PATH="$fakebin:$PATH" FAKE_FIX="$fix" FAKE_LOG="$FAKE_LOG" "$@" \
    VM_URL=http://fake-vm CH_URL=http://fake-ch CH_USER=lucida CH_PASSWORD=pw \
    PG_PASSWORD=pw PG_DUMP_IMAGE=fake-pg QUERY_API_URL=http://fake-api QUERY_API_PASSWORD=pw \
    MODEL_SOURCE="$live_root/model.json" \
    "$CAPTURE" "${base_args[@]/case-policy-test/$case_id}" --case-label calibration \
    --phases-json "$phases_json" --output-root "$live_root/cases" \
    >"$live_root/$case_id.out" 2>"$live_root/$case_id.err"
}
run_live case-live-v4 || { cat "$live_root/case-live-v4.err" >&2; fail 'live capture with fakes failed'; }
meta="$live_root/cases/case-live-v4/meta.json"
[[ -r "$meta" ]] || fail 'live capture produced no meta.json'

# (a) v3 13개: 이름, 순서, 파일 자리, 시각 열, scope, 항목 이름이 실제 v3 녹화본(case-f72-r-v3-bf50d2b7)과 같다.
v3_tables='[["otel_traces_local","data/clickhouse/otel_traces_local.parquet","timestamp","slice"],
["lucida_logs_local","data/clickhouse/lucida_logs_local.parquet","timestamp","slice"],
["lucida_events_local","data/clickhouse/lucida_events_local.parquet","occurred_at","slice"],
["dpm_session_local","data/clickhouse/dpm_session_local.parquet","timestamp","slice"],
["dpm_topsql_local","data/clickhouse/dpm_topsql_local.parquet","timestamp","slice"],
["kcm_events_local","data/clickhouse/kcm_events_local.parquet","timestamp","slice"],
["process_snapshot","data/clickhouse/process_snapshot.parquet","ts","slice"],
["trace_error_chains_local","data/clickhouse/trace_error_chains_local.parquet","window_start","slice"],
["trace_path_signatures_local","data/clickhouse/trace_path_signatures_local.parquet","window_start","slice"],
["syslog_local","data/clickhouse/syslog_local.parquet","received_at","slice"],
["host_connections","data/clickhouse/host_connections.parquet","timestamp","slice"],
["process_meta","data/clickhouse/process_meta.parquet","seen_at","snap"],
["event_cluster_projection_local","data/clickhouse/event_cluster_projection_local.parquet","assigned_at","slice"]]'
v3_meta_keys='["capture_end","capture_end_kst","capture_start","capture_start_kst","case_id","case_label",
"clickhouse_tables","dump_completed_at","dump_completed_at_kst","dump_started_at","dump_started_at_kst",
"evaluation_eligible","golden_anomaly_file","model_sha256","model_snapshot_at","model_snapshot_at_kst",
"model_snapshot_lag_sec","model_source_path","phases","postgres_tables","preflight","run_catalog_sha256",
"run_plan_sha256","run_script_sha256","scenario_id","scenario_metadata","scenario_metadata_sha256",
"schema_version","t1","t1_kst","t2","t2_kst","time_basis","timeline","topology_bundle","topology_snapshot"]'
jq -e --argjson v3 "$v3_tables" '
  (.clickhouse_tables[:13] | map([.table, .file, .time_column, .scope])) == $v3 and
  (.clickhouse_tables[:13] | all(keys == ["file","rows","scope","table","time_column","time_max","time_min"]))' \
  "$meta" >/dev/null || fail 'v3 13 ClickHouse entries changed (name, order, file, time column, scope or keys)'
jq -e --argjson keys "$v3_meta_keys" '(keys - $keys) as $added | ($keys - keys) == [] and .schema_version == "2.0"' \
  "$meta" >/dev/null || fail 'a v3 meta key disappeared'
for f in $(jq -r --argjson v3 "$v3_tables" '$v3[][1]' <<<'{}'); do
  [[ -s "$live_root/cases/case-live-v4/$f" ]] || fail "v3 file missing: $f"
done

# (b) 13개 밖 표가 뒤에 붙는다(자동 발견). 시계열은 분할 키 시각 열로 자르고, 그런 열이 없는 표는 통째로.
jq -e '
  (.clickhouse_tables[13:] | all(.discovered == true)) and
  (.clickhouse_tables[13:] | map(.database + "." + .table) | sort) ==
    ["lucida.agg_event_occurrence_1d","lucida.agg_service_live_5s","lucida.apm_sql_texts_local",
     "lucida.events_by_hash_local","lucida.fails_always_local",
     "lucida.fails_parquet_local","lucida.fails_timeout_local","lucida.kcm_pod_logs_local","lucida.wpm_text_local",
     "lucida_selfsystem.otel_traces_local"] and
  (.clickhouse_tables[] | select(.table == "kcm_pod_logs_local") |
     .file == "data/clickhouse/kcm_pod_logs_local.parquet" and .scope == "slice" and
     .time_column == "timestamp" and .rows == 5 and .time_min == "2026-07-15 01:00:00") and
  (.clickhouse_tables[] | select(.table == "agg_service_live_5s") |
     .format == "Native" and .file == "data/clickhouse/agg_service_live_5s.native" and
     .mv_target_of == ["lucida.mv_traces_to_live_5s"]) and
  (.clickhouse_tables[] | select(.table == "agg_event_occurrence_1d") | .time_type == "Date" and .scope == "slice") and
  (.clickhouse_tables[] | select(.table == "wpm_text_local") | .scope == "snap" and .time_column == null) and
  (.clickhouse_tables[] | select(.database == "lucida_selfsystem") |
     .file == "data/clickhouse/lucida_selfsystem/otel_traces_local.parquet") and
  (.clickhouse_tables[] | select(.table == "fails_parquet_local") |
     .format == "Native" and .file == "data/clickhouse/fails_parquet_local.native" and (has("error") | not)) and
  (.clickhouse_tables[] | select(.table == "fails_always_local") | (.error | test("UNKNOWN_TYPE")) and .rows == null and .file == null) and
  (.clickhouse_tables[] | select(.table == "fails_timeout_local") |
     (.error | test("TIMEOUT_EXCEEDED")) and .format == "Parquet" and .file == null) and
  (.clickhouse_tables[] | select(.table == "apm_sql_texts_local") |
     .scope == "slice" and .time_column == "timestamp_date" and .slice_basis == "sorting_key_time") and
  (.clickhouse_tables[] | select(.table == "events_by_hash_local") | .slice_basis == "sorting_key_first")' \
  "$meta" >/dev/null || fail 'discovered ClickHouse tables not appended as expected'
[[ -s "$live_root/cases/case-live-v4/data/clickhouse/lucida_selfsystem/otel_traces_local.parquet" ]] ||
  fail 'selfsystem table file missing'
[[ ! -e "$live_root/cases/case-live-v4/data/clickhouse/fails_always_local.native" ]] ||
  fail 'failed table left a partial file'
[[ -s "$live_root/cases/case-live-v4/data/catalog/clickhouse/tables.tsv" && -s "$live_root/cases/case-live-v4/data/catalog/postgres/fks.tsv" ]] ||
  fail 'source catalogs not kept under data/catalog/'
extra_pg=$(cd "$live_root/cases/case-live-v4/data/postgres" && ls -A | grep -vE '\.csv$|^restore\.sql$' || true)
[[ -z "$extra_pg" ]] || fail "data/postgres holds files other than CSV and restore.sql: $extra_pg"

# (c) 제외 목록: 저장소 없는 엔진, 내부 저장소, 설정 제외, 크기 상한 넘는 기준 표.
jq -e '(.clickhouse_excluded | map(.table) | sort) ==
  [".inner_id.63da7c0d","huge_state_table","mv_traces_to_live_5s","otel_traces_ingest","schema_migrations"]' \
  "$meta" >/dev/null || fail 'clickhouse_excluded is not the expected list'

# (d) 13개 밖 표 질의는 모두 창 조건(분할 키 시각 열)과 실행 시간 상한을 단다. 통째 표만 창 조건이 없다.
grep -F 'kcm_pod_logs_local' "$FAKE_LOG" | grep -F 'FORMAT Parquet' |
  grep -F "\`timestamp\` >= parseDateTime64BestEffort('2026-07-14T22:50:00Z')" | grep -qF 'max_execution_time=600' ||
  fail 'discovered slice export lacks the window filter or the execution cap'
grep -F 'agg_event_occurrence_1d' "$FAKE_LOG" | grep -F 'FORMAT Parquet' |
  grep -qF "\`day\` >= toDate(parseDateTime64BestEffort('2026-07-14T22:50:00Z'))" ||
  fail 'Date-partitioned table is not filtered by day'
grep -F 'lucida_selfsystem' "$FAKE_LOG" | grep -F 'FORMAT Parquet' | grep -qF 'toString(`trace_uuid`) AS `trace_uuid`' ||
  fail 'UUID columns of discovered tables are not exported as strings'
if grep -F 'FROM `lucida`.`huge_state_table`' "$FAKE_LOG" >/dev/null; then fail 'over-cap table was exported'; fi
# 13개 밖 표에 나간 모든 질의(내보내기, 통계)가 실행 설정(max_threads=2, 실행 시간 상한)을 달고, slice 표는
# 창 조건을, snap 표는 조건 없이 나갔는지 meta 의 표 목록과 대조한다. 시간 초과 표는 Native 로 다시 내지 않는다.
python3 - "$FAKE_LOG" "$meta" <<'PYCHK' || fail 'a discovered ClickHouse query lacks max_threads=2, the execution cap or the window'
import json, sys
log, meta = sys.argv[1], json.load(open(sys.argv[2]))
lines = [l.split("\t", 1) for l in open(log) if l.startswith("CURL http://fake-ch")]
start, end = "'2026-07-14T22:50:00Z'", "'2026-07-15T01:40:00Z'"
bad, seen = [], 0
for e in meta["clickhouse_tables"][13:]:
    src = f"FROM `{e['database']}`.`{e['table']}`"
    qs = [(u, q) for u, q in lines if src in q]
    if len(qs) < (1 if e.get("error") else 2):
        bad.append(f"{e['table']}: {len(qs)} queries")
    for u, q in qs:
        seen += 1
        if "max_threads=2" not in u or "max_execution_time=600" not in u:
            bad.append(f"{e['table']}: settings {u}")
        if e["scope"] == "slice":
            if f"`{e['time_column']}` >=" not in q or start not in q or end not in q:
                bad.append(f"{e['table']}: no window {q[:160]}")
        elif " WHERE " in q:
            bad.append(f"{e['table']}: snap with WHERE")
    if e["table"] == "fails_timeout_local" and any("FORMAT Native" in q for _, q in qs):
        bad.append("timeout retried as Native")
    if e["table"] == "apm_sql_texts_local" and not all("toDate(`timestamp`) >=" in q for _, q in qs):
        bad.append("dictionary table lacks the partition day filter")
print("\n".join(bad), file=sys.stderr)
sys.exit(1 if bad or seen < 15 else 0)
PYCHK

# (e) PG 계획: 외래 키 자식, 이력 표, 상한 넘는 표, 없는 자기참조 열, 명시 snap.
jq -e '
  (.postgres_scope.auto_slices == [{table:"incident_event_cluster_successors",rule:"fk_closure"},
                                   {table:"incident_snapshots",rule:"fk_closure"},
                                   {table:"target_config_history",rule:"history"}]) and
  (.postgres_scope.over_cap == [{table:"big_unknown_table",bytes:314572800}]) and
  (.postgres_scope.missing_columns == ["incidents.superseded_by"]) and
  (.postgres_scope.lineage == ["incident_event_cluster_successors"]) and
  (.postgres_scope.snap_fk_to_slice == [{table:"alert_active",parents:["alert_history"]}]) and
  (.postgres_scope.absent_slice | index("coverage_events") != null) and
  (.postgres_scope.plan_file == "data/catalog/postgres-scope-plan.json") and
  (.postgres_scope.over_budget == true and .postgres_scope.largest_snap[0].table == "collector_sms_process_snapshot") and
  ([.postgres_scope.largest_snap[].table] | index("trainer_trace_window_threshold") == null) and
  ([.postgres_tables[].table] | index("trainer_trace_window_threshold") != null) and
  ([.postgres_tables[].table] | index("incident_snapshots") != null and index("target_config_history") != null) and
  ([.postgres_tables[].table] | index("incidents") < index("incident_snapshots"))' \
  "$meta" >/dev/null || fail 'postgres scope plan not reflected in meta'
jq -e '.tables.trainer_trace_window_threshold.scope == "slice" and .tables.trainer_trace_window_threshold.rule == "config" and
  .tables.collector_sms_process_snapshot.scope == "snap" and .tables.collector_sms_process_snapshot.rule == "config" and
  .tables.target_config_latest.scope == "snap" and .tables.target_config_latest.rule == "default" and
  .tables.big_unknown_table.scope == "drop" and .tables["security.audit_marks"].scope == "snap"' \
  "$live_root/cases/case-live-v4/data/catalog/postgres-scope-plan.json" >/dev/null || fail 'scope-plan.json decisions unexpected'
dump_line=$(grep '^DOCKER .*pg_dump' "$FAKE_LOG")
for want in --exclude-table-data=public.incident_snapshots \
            --exclude-table-data-and-children=public.target_config_history \
            --exclude-table-data=public.big_unknown_table --exclude-table-data=public.incidents; do
  grep -qF -- "$want" <<<"$dump_line" || fail "pg_dump lacks $want"
done
if grep -qF -- 'public.collector_sms_process_snapshot' <<<"$dump_line"; then fail 'explicit snap table excluded from pg_dump'; fi
grep -qF -- '--exclude-table-data=public.trainer_trace_window_threshold' <<<"$dump_line" ||
  fail 'trainer_trace_window_threshold still dumped whole'
export_sql=$(sed -n '/^PSQLFILE .export.sql$/,/^ENDPSQL$/p' "$FAKE_LOG")
grep -qF 'SELECT "agent_id", max("version") FROM public."trainer_trace_window_threshold"' <<<"$export_sql" ||
  fail 'threshold validity slice missing from export SQL'
grep -qF 'merged_into' <<<"$export_sql" || fail 'event_clusters recursion lost'
grep -qF 'SELECT l."successor_incident_id" AS id FROM public."incident_event_cluster_successors" l' <<<"$export_sql" ||
  fail 'incidents lineage recursion missing'
grep -q 'slice table in pg-scope.json is absent at source, not captured: coverage_events' "$live_root/case-live-v4.err" ||
  fail 'no warning for a configured slice table absent at source'
if grep -q 'absent at source, not captured: kedb_pending' "$live_root/case-live-v4.err"; then fail 'warned about an absent drop table'; fi
if grep -qF -- 'public.alert_active' <<<"$dump_line"; then fail 'state table alert_active excluded from pg_dump'; fi
if grep -qF 'superseded_by' <<<"$export_sql"; then fail 'export SQL still reads incidents.superseded_by'; fi
grep -q 'whole-table dump .* exceeds budget' "$live_root/case-live-v4.err" || fail 'no warning for snap budget'
grep -qF "\\copy incident_snapshots FROM 'incident_snapshots.csv'" "$live_root/cases/case-live-v4/data/postgres/restore.sql" ||
  fail 'restore.sql lacks the discovered child table'

# (f) 제품 버전 칸과 지표 내보내기 요약.
jq -e '.product_version == "2026.11.09-01" and .product_branch == "develop-ai" and
  .product_version_source == "query-api:/api/v1/product/version" and
  .victoriametrics_export.series == 3 and .victoriametrics_export.file == "data/victoriametrics.export"' \
  "$meta" >/dev/null || fail 'product version or victoriametrics_export missing'

# (g) 제품 버전 API 가 없으면 null 로 두고 녹화는 끝낸다. 환경변수가 있으면 그 값을 쓴다.
run_live case-live-v4-nopv FAKE_PV_FAIL=1 || fail 'capture failed when product version API is missing'
jq -e '.product_version == null and .product_branch == null' "$live_root/cases/case-live-v4-nopv/meta.json" >/dev/null ||
  fail 'product_version not null when API missing'
grep -q 'product version unavailable' "$live_root/case-live-v4-nopv.err" || fail 'no warning for missing product version'
run_live case-live-v4-envpv FAKE_PV_FAIL=1 PRODUCT_VERSION=manual-1 || fail 'capture failed with PRODUCT_VERSION'
jq -e '.product_version == "manual-1" and .product_version_source == "env:PRODUCT_VERSION"' \
  "$live_root/cases/case-live-v4-envpv/meta.json" >/dev/null || fail 'PRODUCT_VERSION env not used'

printf '[PASS] capture policy dry-run tests\n'
