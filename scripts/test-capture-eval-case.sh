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

for label in calibration evaluation failed; do
  label_args=()
  [[ "$label" != evaluation ]] || label_args=(--run-result "$TMP_ROOT/runs/run-test/result.json")
  output=$("$CAPTURE" "${base_args[@]}" --case-label "$label" "${label_args[@]}" \
    --output-root "$TMP_ROOT/cases" --dry-run)
  jq -e \
    --arg case_label "$label" \
    '.mode == "dry-run" and .case_label == $case_label and
     .capture_start == "2026-07-14T23:00:00Z" and
     .capture_end == "2026-07-15T01:55:00Z" and
     .model_snapshot_not_before == .capture_end and
     .golden_anomaly_file == false and
     .evaluation_eligible == ($case_label == "evaluation")' \
    <<<"$output" >/dev/null || fail "unexpected dry-run policy for label=$label"
done

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

printf '[PASS] capture policy dry-run tests\n'
