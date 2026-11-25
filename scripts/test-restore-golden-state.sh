#!/usr/bin/env bash
# Side-effect-free tests for restore-golden-state.sh. docker and curl are fakes on
# PATH (RUNTIME=docker): docker answers inspect/stop/start and swallows the psql and
# pg_restore sidecar runs; curl answers the observer health check. A temporary golden
# root holds one golden (meta.json + data/golden-pg-state.dump).
#
# What is checked: the restore result JSON carries golden_stream_generation and
# golden_stream_boot_load_until read after the data restore (null when unreadable,
# never failing the restore), and golden_sha256 = sha256 of the dump
# that was restored (the runner threads it into phases.json trainer_reset, the capture
# carries it into the recording meta), in both the real and the dry-run output, and a
# dump that does not match meta.pg_ai_state.sha256 is still refused.

set -Eeuo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
RESTORE="$SCRIPT_DIR/restore-golden-state.sh"
TMP_ROOT=$(mktemp -d)
trap 'rm -rf "$TMP_ROOT"' EXIT

fail() { printf '[FAIL] %s\n' "$*" >&2; exit 1; }

mkdir -p "$TMP_ROOT/bin"
cat >"$TMP_ROOT/bin/docker" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$FAKE_LOG"
case "$1" in
  inspect)
    # -f '{{.State.Running}}' <name>: false once stopped (v4 GOLDFIX keeps the state so the
    # band restore can check that the observer really is down).
    if [[ "${2:-}" == -f ]]; then
      [[ -e "$FAKE_STATE/${4:-}.stopped" ]] && printf 'false\n' || printf 'true\n'
    fi
    exit 0 ;;
  stop)
    [[ "${2:-}" == lucida-ai-observer && -n "${FAKE_OBSERVER_STOP_NOOP:-}" ]] || : >"$FAKE_STATE/${2:-}.stopped"
    exit 0 ;;
  start) rm -f "$FAKE_STATE/${2:-}.stopped"; exit 0 ;;
  exec)
    [[ -n "${FAKE_ARTIFACT_FILE:-}" && "$*" == *"/var/lib/lucida/ai-models/stream-anomaly/global/v1/model.json"* ]] || exit 1
    cat "$FAKE_ARTIFACT_FILE" ;;
  run)
    case "$*" in
      *--version*) [[ "$*" == *fake-pg16* ]] && echo 'pg_dump (PostgreSQL) 16.15' || echo 'pg_dump (PostgreSQL) 17.11' ;;
      *'SHOW server_version_num'*) echo "${FAKE_PG_SERVER_NUM:-170011}" ;;
      *trainer_stream_generations*)
        [[ "${FAKE_STREAM_FAIL:-}" == 1 ]] && exit 2
        printf '%s\n' "${FAKE_STREAM_ROW-1985|0|1795105440999}" ;;
      *'UPDATE public.band_state'*|*band_forecast_seq*)
        [[ "${FAKE_BAND_RESET_FAIL:-}" == 1 ]] && exit 2
        printf '%s\n' "${FAKE_BAND_RESET_ROW-43|11400}" ;;
      *json_build_object*)
        [[ "${FAKE_BAND_FAIL:-}" == 1 ]] && exit 2
        default_band='{"detection_policy":null,"model_binding":false,"artifact_uri":null,"artifact_checksum":null,"coverage_overrides":0}'
        printf '%s\n' "${FAKE_BAND_ROW-$default_band}" ;;
    esac
    exit 0 ;;
  *) exit 9 ;;
esac
SH
cat >"$TMP_ROOT/bin/curl" <<'SH'
#!/usr/bin/env bash
exit 0
SH
cat >"$TMP_ROOT/bin/kubectl" <<'SH'
#!/usr/bin/env bash
exit 1
SH
chmod +x "$TMP_ROOT/bin/docker" "$TMP_ROOT/bin/curl" "$TMP_ROOT/bin/kubectl"

export PATH="$TMP_ROOT/bin:$PATH" FAKE_LOG="$TMP_ROOT/docker.log" FAKE_STATE="$TMP_ROOT/state"
mkdir -p "$FAKE_STATE"
export RUNTIME=docker KUBECTL=false PG_PASSWORD=unused-test-value

golden_root="$TMP_ROOT/goldens"
golden="$golden_root/golden-0900"
mkdir -p "$golden/data"
printf 'fake pg custom dump bytes\n' >"$golden/data/golden-pg-state.dump"
dump_sha=$(sha256sum "$golden/data/golden-pg-state.dump" | cut -d' ' -f1)
jq -n --arg sha "$dump_sha" \
  '{kind:"golden", tod_phase_kst:"09:00",
    pg_ai_state:{sha256:$sha, tables:["detector_seen_signatures","coverage_signal_state"]}}' \
  >"$golden/meta.json"

# 1. Real restore: result JSON keeps its existing keys and adds golden_sha256.
out=$("$RESTORE" --golden-root "$golden_root" --for-time now --ready-timeout-sec 5 2>"$TMP_ROOT/err") ||
  fail "restore failed: $(cat "$TMP_ROOT/err")"
jq -e --arg sha "$dump_sha" --arg dir "$golden" '
  .action == "restore-golden" and .status == "ok" and
  .golden_dir == $dir and .golden_tod_kst == "09:00" and
  .pg_tables_restored == 2 and
  .golden_sha256 == $sha and (.golden_sha256 | test("^[0-9a-f]{64}$"))
' <<<"$out" >/dev/null || fail "restore result must carry golden_sha256 of the restored dump: $out"
grep -q '^run ' "$FAKE_LOG" || fail 'restore did not run the pg sidecar'
# A golden without band tables (older product) leaves the band follow-up out and its fields null.
jq -e '.band_open_episodes_cleared == null and .band_forecast_seq_set == null' <<<"$out" >/dev/null ||
  fail "a golden without band tables must not report a band follow-up: $out"
! grep -q -e 'UPDATE public.band_state' -e 'band_forecast_seq' "$FAKE_LOG" || fail 'band follow-up ran for a golden without band tables'

# 1b. G2META (Q19 B): the result names the restored golden's active stream generation
#     and the earliest valid_until of its artifacts (ms floored to the second), read
#     after the data restore and before the observer starts.
jq -e '.golden_stream_generation == 1985 and .golden_stream_boot_load_until == "2026-11-19T16:24:00Z" and
       .golden_stream_periodic_count == 0 and
       .observer_band_sources == {detection_policy:null, model_binding:false, artifact_uri:null,
                                  artifact_checksum:null, coverage_overrides:0, artifact_read:false,
                                  artifact_checksum_ok:null, artifact_band_strategy:null}' \
  <<<"$out" >/dev/null || fail "restore result must carry the golden stream generation, deadline and band sources: $out"
band_line=$(grep -n 'json_build_object' "$FAKE_LOG" | head -1 | cut -d: -f1)
[[ -n "$band_line" ]] || fail 'band strategy sources were not read'
query_line=$(grep -n 'trainer_stream_generations' "$FAKE_LOG" | head -1 | cut -d: -f1)
restore_line=$(grep -n 'pg_restore --data-only' "$FAKE_LOG" | head -1 | cut -d: -f1)
start_line=$(grep -n '^start ' "$FAKE_LOG" | head -1 | cut -d: -f1)
[[ -n "$query_line" && -n "$restore_line" && -n "$start_line" ]] || fail "missing sidecar or start lines: $(cat "$FAKE_LOG")"
(( restore_line < query_line && query_line < start_line && restore_line < band_line && band_line < start_line )) ||
  fail "stream generation and band sources must be read after pg_restore and before the observer starts: $(cat "$FAKE_LOG")"
grep 'trainer_stream_generations' "$FAKE_LOG" | grep -q 'm.active' ||
  fail 'stream generation read must pick the active generation'
# valid_until_ms 0 = never expires (observer trainer_load.go): only positive values set the deadline,
# across all three tables the observer checks at boot (G2META 2회차, 리뷰 하 1).
for tbl in 't.valid_until_ms) FILTER (WHERE t.valid_until_ms > 0) FROM public.trainer_stream_threshold' \
           'd.valid_until_ms) FILTER (WHERE d.valid_until_ms > 0) FROM public.trainer_stream_distribution_profile' \
           'p.valid_until_ms) FILTER (WHERE p.valid_until_ms > 0) FROM public.trainer_stream_periodic_profile'; do
  grep -qF "min($tbl" "$FAKE_LOG" || fail "deadline must take min(valid_until_ms > 0) of: $tbl"
done
# Band sources follow the observer's precedence inputs for the stream-anomaly agent.
for frag in "FROM public.ai_detection_policies p" "WHERE p.agent_id = 'stream-anomaly' ORDER BY p.is_builtin DESC, p.created_at, p.id" \
            "b.agent_id = 'stream-anomaly' AND b.enabled = true AND b.rollout_status = 'active'" \
            "OR (b.scope = 'target' AND b.target_id IS NULL) OR b.scope = 'global')" \
            "JOIN public.ai_model_registry r ON r.id = v.model_id" \
            "WHERE c.agent_id = 'stream-anomaly' AND c.enabled AND (c.params->>'band_strategy') IS NOT NULL"; do
  grep -qF "$frag" "$FAKE_LOG" || fail "band source read is missing: $frag"
done
for row_case in 'none:' 'fail:' 'nodeadline:1985|3|' 'noperiodic:1985||1795105440999' 'garbage:1985|abc' 'oldform:1985|1795105440999'; do
  label=${row_case%%:*} row=${row_case#*:}
  : >"$FAKE_LOG"
  if [[ "$label" == fail ]]; then
    out=$(FAKE_STREAM_FAIL=1 "$RESTORE" --golden-root "$golden_root" --for-time now --ready-timeout-sec 5 2>"$TMP_ROOT/err") ||
      fail "restore must not fail when the stream read fails: $(cat "$TMP_ROOT/err")"
  else
    out=$(FAKE_STREAM_ROW="$row" "$RESTORE" --golden-root "$golden_root" --for-time now --ready-timeout-sec 5 2>"$TMP_ROOT/err") ||
      fail "restore must not fail on stream row '$row': $(cat "$TMP_ROOT/err")"
  fi
  case "$label" in
    nodeadline) want='.golden_stream_generation == 1985 and .golden_stream_periodic_count == 3 and .golden_stream_boot_load_until == null' ;;
    noperiodic) want='.golden_stream_generation == 1985 and .golden_stream_periodic_count == null and .golden_stream_boot_load_until == "2026-11-19T16:24:00Z"' ;;
    *) want='.golden_stream_generation == null and .golden_stream_boot_load_until == null' ;;
  esac
  jq -e ".status == \"ok\" and .golden_sha256 == \"$dump_sha\" and $want" <<<"$out" >/dev/null ||
    fail "stream row case $label: $out"
  grep -q '^start ' "$FAKE_LOG" || fail "stream row case $label must still start the observer"
done
for band_case in 'fail:' 'empty:' 'notjson:x|y' 'noncount:{"model_binding":"false","coverage_overrides":0}' \
                 'policy:{"detection_policy":"legacy_envelope","model_binding":false,"artifact_uri":null,"artifact_checksum":null,"coverage_overrides":2}'; do
  label=${band_case%%:*} row=${band_case#*:}
  : >"$FAKE_LOG"
  if [[ "$label" == fail ]]; then
    out=$(FAKE_BAND_FAIL=1 "$RESTORE" --golden-root "$golden_root" --for-time now --ready-timeout-sec 5 2>"$TMP_ROOT/err") ||
      fail "restore must not fail when the band read fails: $(cat "$TMP_ROOT/err")"
  else
    out=$(FAKE_BAND_ROW="$row" "$RESTORE" --golden-root "$golden_root" --for-time now --ready-timeout-sec 5 2>"$TMP_ROOT/err") ||
      fail "restore must not fail on band row '$row': $(cat "$TMP_ROOT/err")"
  fi
  case "$label" in
    policy) want='.observer_band_sources.detection_policy == "legacy_envelope" and .observer_band_sources.coverage_overrides == 2' ;;
    *) want='.observer_band_sources == null' ;;
  esac
  jq -e ".status == \"ok\" and .golden_stream_generation == 1985 and $want" <<<"$out" >/dev/null ||
    fail "band row case $label: $out"
done

# 4b. The boot-loaded model artifact is read through the running observer after it is
#     healthy, checked against the version checksum like the observer does, and its
#     params.BandStrategy recorded. Unreadable or unsafe paths leave artifact_read false.
printf '%s' '{"params":{"BandStrategy":"legacy_envelope","SeasonalBuckets":true},"note":"x"}' >"$TMP_ROOT/artifact.json"
art_sha=$(sha256sum "$TMP_ROOT/artifact.json" | cut -d' ' -f1)
bound_row() { printf '{"detection_policy":null,"model_binding":true,"artifact_uri":"%s","artifact_checksum":%s,"coverage_overrides":0}' "$1" "$2"; }
for art_case in "match:stream-anomaly/global/v1/model.json:\"$art_sha\":1" \
                "mismatch:stream-anomaly/global/v1/model.json:\"$(printf 'b%.0s' {1..64})\":1" \
                "nochecksum:stream-anomaly/global/v1/model.json:null:1" \
                "unreadable:stream-anomaly/global/v1/model.json:\"$art_sha\":0" \
                "traversal:../../etc/passwd:\"$art_sha\":1"; do
  IFS=: read -r label uri sum readable <<<"$art_case"
  : >"$FAKE_LOG"
  artifact_env=''
  [[ "$readable" == 1 ]] && artifact_env="$TMP_ROOT/artifact.json"
  out=$(FAKE_BAND_ROW="$(bound_row "$uri" "$sum")" FAKE_ARTIFACT_FILE="$artifact_env" \
        "$RESTORE" --golden-root "$golden_root" --for-time now --ready-timeout-sec 5 2>"$TMP_ROOT/err") ||
    fail "restore must not fail on artifact case $label: $(cat "$TMP_ROOT/err")"
  case "$label" in
    match|nochecksum) want='.artifact_read == true and .artifact_checksum_ok == true and .artifact_band_strategy == "legacy_envelope"' ;;
    mismatch) want='.artifact_read == true and .artifact_checksum_ok == false and .artifact_band_strategy == "legacy_envelope"' ;;
    *) want='.artifact_read == false and .artifact_checksum_ok == null and .artifact_band_strategy == null' ;;
  esac
  jq -e ".status == \"ok\" and (.observer_band_sources | .model_binding == true and $want)" <<<"$out" >/dev/null ||
    fail "artifact case $label: $out"
  if [[ "$label" == traversal ]]; then
    ! grep -q '^exec ' "$FAKE_LOG" || fail 'an unsafe artifact path must not be read'
  else
    exec_line=$(grep -n '^exec ' "$FAKE_LOG" | head -1 | cut -d: -f1)
    healthy_after=$(grep -n '^start ' "$FAKE_LOG" | head -1 | cut -d: -f1)
    [[ -n "$exec_line" && "$exec_line" -gt "$healthy_after" ]] ||
      fail "artifact must be read from the started observer: $(cat "$FAKE_LOG")"
    grep -q '^exec lucida-ai-observer cat /var/lib/lucida/ai-models/stream-anomaly/global/v1/model.json$' "$FAKE_LOG" ||
      fail "artifact read path: $(grep '^exec ' "$FAKE_LOG")"
  fi
done

# An artifact whose shape the observer would refuse (params not an object, BandStrategy not a
# string) is recorded as unread; it must never end the restore, which by then has started
# the observer and left the trainer frozen (G2META 3회차, 재리뷰 하 1). A missing params is
# the observer's defaults (it merges over them): read, strategy null.
for shape_case in 'pstring:{"params":"x"}:0' 'pnumber:{"params":5}:0' 'bnumber:{"params":{"BandStrategy":3}}:0' \
                  'pnull:{"params":null}:1' 'pmissing:{"note":"x"}:1'; do
  label=${shape_case%%:*} rest=${shape_case#*:} readable=${shape_case##*:}
  body=${rest%:*}
  printf '%s' "$body" >"$TMP_ROOT/odd.json"
  odd_sha=$(sha256sum "$TMP_ROOT/odd.json" | cut -d' ' -f1)
  : >"$FAKE_LOG"
  out=$(FAKE_BAND_ROW="$(bound_row stream-anomaly/global/v1/model.json "\"$odd_sha\"")" FAKE_ARTIFACT_FILE="$TMP_ROOT/odd.json" \
        "$RESTORE" --golden-root "$golden_root" --for-time now --ready-timeout-sec 5 2>"$TMP_ROOT/err") ||
    fail "restore must not end on artifact shape $label: $(tail -3 "$TMP_ROOT/err")"
  if [[ "$readable" == 1 ]]; then
    want='.artifact_read == true and .artifact_checksum_ok == true and .artifact_band_strategy == null'
  else
    want='.artifact_read == false and .artifact_checksum_ok == null and .artifact_band_strategy == null'
  fi
  jq -e ".status == \"ok\" and .trainer_frozen == true and (.observer_band_sources | $want)" <<<"$out" >/dev/null ||
    fail "artifact shape $label: $out"
  grep -q 'golden restore complete' "$TMP_ROOT/err" || fail "artifact shape $label did not finish the restore"
done

# 5. v4 GOLDFIX (Q43 답 1, 2): a golden with the band engine tables.
band_golden="$TMP_ROOT/band-goldens/golden-band"
mkdir -p "$band_golden/data"
printf 'fake band dump bytes\n' >"$band_golden/data/golden-pg-state.dump"
band_dump_sha=$(sha256sum "$band_golden/data/golden-pg-state.dump" | cut -d' ' -f1)
jq -n --arg sha "$band_dump_sha" \
  '{kind:"golden", tod_phase_kst:"02:00",
    pg_ai_state:{sha256:$sha, tables:["detector_seen_signatures","band_forecast","band_state"],
                 sequences:{band_forecast_seq:11372}}}' >"$band_golden/meta.json"
rm -f "$FAKE_STATE"/*; : >"$FAKE_LOG"
out=$("$RESTORE" --golden-dir "$band_golden" --ready-timeout-sec 5 2>"$TMP_ROOT/err") ||
  fail "band golden restore failed: $(cat "$TMP_ROOT/err")"
jq -e '.status == "ok" and .pg_tables_restored == 3 and .band_open_episodes_cleared == 43 and .band_forecast_seq_set == 11400' \
  <<<"$out" >/dev/null || fail "band restore result: $out"
# Both band tables go through the same reset as every golden table.
grep -q 'DELETE FROM public."band_forecast"; DELETE FROM public."band_state";' "$FAKE_LOG" ||
  fail "band tables must be reset with the other golden tables: $(grep DELETE "$FAKE_LOG")"
reset_line=$(grep -n 'UPDATE public.band_state' "$FAKE_LOG" | head -1 | cut -d: -f1)
restore_line=$(grep -n 'pg_restore --data-only' "$FAKE_LOG" | head -1 | cut -d: -f1)
delete_line=$(grep -n 'DELETE FROM public."band_forecast"' "$FAKE_LOG" | head -1 | cut -d: -f1)
stop_line=$(grep -n '^stop lucida-ai-observer$' "$FAKE_LOG" | head -1 | cut -d: -f1)
start_line=$(grep -n '^start lucida-ai-observer$' "$FAKE_LOG" | head -1 | cut -d: -f1)
[[ -n "$reset_line" && -n "$restore_line" && -n "$delete_line" && -n "$stop_line" && -n "$start_line" ]] ||
  fail "band restore lines missing: $(cat "$FAKE_LOG")"
(( stop_line < delete_line && restore_line < reset_line && reset_line < start_line )) ||
  fail "band tables must be reset with the observer down and closed before it starts: $(cat "$FAKE_LOG")"
grep -q 'observer confirmed stopped for the band tables' "$TMP_ROOT/err" || fail 'band restore did not confirm the observer stop'
reset_sql=$(sed -n "${reset_line}p" "$FAKE_LOG"; sed -n "$((reset_line + 1)),$((reset_line + 3))p" "$FAKE_LOG")
# Open episodes are closed by nulling the columns the observer nulls on close, rows stay.
grep -qF 'UPDATE public.band_state SET episode_id = NULL, episode_opened_at = NULL,' <<<"$reset_sql" ||
  fail "open episodes must be closed by nulling the episode columns: $reset_sql"
grep -qF 'orig_valid_from = NULL, orig_upper = NULL, orig_lower = NULL' <<<"$reset_sql" ||
  fail "closing an episode nulls the original band too: $reset_sql"
grep -qF 'WHERE episode_id IS NOT NULL OR episode_opened_at IS NOT NULL RETURNING 1' <<<"$reset_sql" ||
  fail "only rows with an open episode are touched: $reset_sql"
! grep -qE 'DELETE FROM public\.band_state|TRUNCATE' "$FAKE_LOG" || fail 'band_state rows must not be deleted after the restore'
set_cols=$(grep -oE '(shift|base|flat_center|flat_half|retrain_due_at|retrain_count|buffer_due_at) = ' <<<"$reset_sql" || true)
[[ -z "$set_cols" ]] || fail "temporary band and retrain columns must be kept: $set_cols"
# band_forecast_seq is set to at least the restored max(seq), the meta value and its live value.
grep -qF "setval('public.band_forecast_seq', GREATEST((SELECT COALESCE(max(seq), 0) FROM public.band_forecast)," <<<"$reset_sql" ||
  fail "band_forecast_seq must be set to at least the restored max(seq): $reset_sql"
grep -qF '11372, (SELECT last_value FROM public.band_forecast_seq), 1), true)' <<<"$reset_sql" ||
  fail "setval must also take the capture value and never lower the live value: $reset_sql"

# 5b. The observer still running (stop did not take): the band tables are refused before
#     anything is emptied, and the observer is not restarted over a half restore.
rm -f "$FAKE_STATE"/*; : >"$FAKE_LOG"
if FAKE_OBSERVER_STOP_NOOP=1 OBSERVER_STOP_WAIT_SEC=0 "$RESTORE" --golden-dir "$band_golden" --ready-timeout-sec 5 \
     >"$TMP_ROOT/out" 2>"$TMP_ROOT/err"; then
  fail 'band restore must be refused while the observer runs'
fi
grep -q 'observer is still running; band_forecast and band_state are not restored' "$TMP_ROOT/err" ||
  fail "unexpected failure: $(cat "$TMP_ROOT/err")"
! grep -qE 'DELETE FROM|pg_restore|UPDATE public.band_state|^start ' "$FAKE_LOG" ||
  fail "nothing may be emptied or restored while the observer runs: $(cat "$FAKE_LOG")"
# A golden without band tables keeps the old behaviour even then.
rm -f "$FAKE_STATE"/*; : >"$FAKE_LOG"
FAKE_OBSERVER_STOP_NOOP=1 OBSERVER_STOP_WAIT_SEC=0 "$RESTORE" --golden-dir "$golden" --ready-timeout-sec 5 \
  >/dev/null 2>"$TMP_ROOT/err" || fail "a golden without band tables must not need the band stop check: $(cat "$TMP_ROOT/err")"

# 5c. A failing band follow-up fails the restore and leaves the observer down.
rm -f "$FAKE_STATE"/*; : >"$FAKE_LOG"
if FAKE_BAND_RESET_FAIL=1 "$RESTORE" --golden-dir "$band_golden" --ready-timeout-sec 5 >/dev/null 2>"$TMP_ROOT/err"; then
  fail 'a failed band follow-up must fail the restore'
fi
grep -q 'band follow-up (close open episodes, band_forecast_seq) failed' "$TMP_ROOT/err" || fail "unexpected failure: $(cat "$TMP_ROOT/err")"
! grep -q '^start lucida-ai-observer$' "$FAKE_LOG" || fail 'the observer must not start after a failed band follow-up'
for bad in '' 'x|y' '43'; do
  rm -f "$FAKE_STATE"/*
  if FAKE_BAND_RESET_ROW="$bad" "$RESTORE" --golden-dir "$band_golden" --ready-timeout-sec 5 >/dev/null 2>"$TMP_ROOT/err"; then
    fail "band follow-up row '$bad' must fail the restore"
  fi
done

# 5d. RUNTIME=k8s: the observer counts as stopped only when its Deployment has no pods left.
cat >"$TMP_ROOT/bin/kubectl-k8s" <<'SH'
#!/usr/bin/env bash
printf 'kubectl %s\n' "$*" >>"$FAKE_LOG"
case "$*" in
  version*) exit 0 ;;
  *'get deployment/ai-trainer -o jsonpath={.spec.replicas}'*) echo 1 ;;
  *'get deployment/ai-observer -o jsonpath={.spec.replicas} {.status.replicas}'*)
    # pods linger for FAKE_POD_POLLS reads after scale 0
    left=$(cat "$FAKE_STATE/k8s-polls" 2>/dev/null || echo "${FAKE_POD_POLLS:-0}")
    if [[ -e "$FAKE_STATE/k8s-observer-scaled0" ]]; then
      if (( left > 0 )); then echo "0 1"; echo $(( left - 1 )) >"$FAKE_STATE/k8s-polls"; else echo "0 "; fi
    else echo "1 1"; fi ;;
  *'get deployment/ai-observer -o jsonpath={.spec.replicas}'*)
    [[ -e "$FAKE_STATE/k8s-observer-scaled0" ]] && echo 0 || echo 1 ;;
  *'scale deployment/ai-observer --replicas=0'*) : >"$FAKE_STATE/k8s-observer-scaled0" ;;
  *'scale deployment/ai-observer --replicas=1'*) rm -f "$FAKE_STATE/k8s-observer-scaled0" ;;
  *get\ deployment/*|*scale*|*rollout*) exit 0 ;;
  *'get svc pg-rw'*) printf '10.43.0.4' ;;
  *'get svc ai-observer'*) printf '10.43.0.3' ;;
  *'get secret pg-superuser'*) [[ -n "${FAKE_NO_ADMIN_SECRET:-}" ]] && exit 1
    case "$*" in *'.data.username'*) printf 'fakeadmin' | base64 ;; *'.data.password'*) printf 'fake-admin-secret' | base64 ;; esac ;;
  *) exit 1 ;;
esac
SH
chmod +x "$TMP_ROOT/bin/kubectl-k8s"
rm -f "$FAKE_STATE"/*; : >"$FAKE_LOG"
RUNTIME=k8s KUBECTL="$TMP_ROOT/bin/kubectl-k8s" FAKE_POD_POLLS=1 OBSERVER_STOP_WAIT_SEC=30 \
  "$RESTORE" --golden-dir "$band_golden" --ready-timeout-sec 5 >"$TMP_ROOT/out" 2>"$TMP_ROOT/err" ||
  fail "k8s band restore failed: $(cat "$TMP_ROOT/err")"
polls=$(grep -c 'get deployment/ai-observer -o jsonpath={.spec.replicas} {.status.replicas}' "$FAKE_LOG")
(( polls >= 2 )) || fail "k8s band restore must wait until the observer pods are gone: $polls reads"
poll_last=$(grep -n 'jsonpath={.spec.replicas} {.status.replicas}' "$FAKE_LOG" | tail -1 | cut -d: -f1)
k8s_delete=$(grep -n 'DELETE FROM public."band_forecast"' "$FAKE_LOG" | head -1 | cut -d: -f1)
[[ -n "$k8s_delete" ]] && (( poll_last < k8s_delete )) || fail "k8s band reset must follow the pod check: $(cat "$FAKE_LOG")"
rm -f "$FAKE_STATE"/*; : >"$FAKE_LOG"
if RUNTIME=k8s KUBECTL="$TMP_ROOT/bin/kubectl-k8s" FAKE_POD_POLLS=99 OBSERVER_STOP_WAIT_SEC=0 \
     "$RESTORE" --golden-dir "$band_golden" --ready-timeout-sec 5 >/dev/null 2>"$TMP_ROOT/err"; then
  fail 'k8s band restore must be refused while observer pods remain'
fi
grep -q 'observer is still running' "$TMP_ROOT/err" || fail "unexpected k8s failure: $(cat "$TMP_ROOT/err")"
! grep -q 'DELETE FROM' "$FAKE_LOG" || fail 'k8s: nothing may be emptied while observer pods remain'

# 5e. k8s: an unreadable pg-superuser secret is not a silent exit (pipefail on the read used to end the
#     script with no message). The WARN fallback to PG_USER runs, the admin username is PG_USER, and the
#     restore goes on (here through the band restore).
rm -f "$FAKE_STATE"/*; : >"$FAKE_LOG"
RUNTIME=k8s KUBECTL="$TMP_ROOT/bin/kubectl-k8s" FAKE_NO_ADMIN_SECRET=1 PG_USER=fallbackuser \
  "$RESTORE" --golden-dir "$band_golden" --ready-timeout-sec 5 >"$TMP_ROOT/out" 2>"$TMP_ROOT/err" ||
  fail "restore must go on without the admin secret: $(tail -3 "$TMP_ROOT/err")"
grep -q 'WARN: k8s secret polestar/pg-superuser not readable' "$TMP_ROOT/err" ||
  fail "an unreadable admin secret must log the WARN fallback: $(cat "$TMP_ROOT/err")"
grep -q 'PG admin privilege check passed (user=fallbackuser)' "$TMP_ROOT/err" ||
  fail "the admin role must fall back to PG_USER: $(grep 'admin' "$TMP_ROOT/err")"
jq -e '.status == "ok"' "$TMP_ROOT/out" >/dev/null || fail "restore without the admin secret: $(cat "$TMP_ROOT/out")"

# 2. Dry run reports the same digest without touching anything.
: >"$FAKE_LOG"
out=$("$RESTORE" --golden-dir "$golden" --dry-run 2>"$TMP_ROOT/err") ||
  fail "dry-run failed: $(cat "$TMP_ROOT/err")"
jq -e --arg sha "$dump_sha" '.mode == "dry-run" and .golden_sha256 == $sha' <<<"$out" >/dev/null ||
  fail "dry-run result must carry golden_sha256: $out"
! grep -qE '^(run|stop|start) ' "$FAKE_LOG" || fail 'dry-run must not stop, start or run anything'

# 3. A dump that no longer matches meta is refused before anything is stopped.
: >"$FAKE_LOG"
printf 'tampered\n' >>"$golden/data/golden-pg-state.dump"
if "$RESTORE" --golden-dir "$golden" --ready-timeout-sec 5 >"$TMP_ROOT/out" 2>"$TMP_ROOT/err"; then
  fail 'restore must refuse a dump whose sha256 differs from meta'
fi
grep -q 'golden dump sha256 mismatch' "$TMP_ROOT/err" || fail "unexpected refusal: $(cat "$TMP_ROOT/err")"
! grep -qE '^(run|stop|start) ' "$FAKE_LOG" || fail 'a refused restore must not touch the stack'

# 4. PG 도구 이미지의 메이저가 서버보다 낮으면 아무것도 멈추기 전에 거절한다(119 PG 17, 도구 16).
#    기본 이미지는 postgres:17-alpine 이다.
printf 'fake pg custom dump bytes\n' >"$golden/data/golden-pg-state.dump"
grep -q 'PG_DUMP_IMAGE="${PG_DUMP_IMAGE:-postgres:17-alpine}"' "$RESTORE" || fail 'default PG tool image is not 17'
: >"$FAKE_LOG"
if PG_DUMP_IMAGE=fake-pg16 "$RESTORE" --golden-dir "$golden" --ready-timeout-sec 5 >"$TMP_ROOT/out" 2>"$TMP_ROOT/err"; then
  fail 'restore must refuse a PG 16 tool against a PG 17 server'
fi
grep -q 'PG 도구 16 은 서버 17 보다 오래돼' "$TMP_ROOT/err" || fail "unexpected refusal: $(cat "$TMP_ROOT/err")"
! grep -qE '^(stop|start) ' "$FAKE_LOG" || fail 'a refused restore must not stop or start anything'
! grep -q 'pg_restore' "$FAKE_LOG" || fail 'pg_restore ran despite the major mismatch'
PG_DUMP_IMAGE=fake-pg16 FAKE_PG_SERVER_NUM=160015 "$RESTORE" --golden-dir "$golden" --ready-timeout-sec 5 \
  >/dev/null 2>"$TMP_ROOT/err" || fail "restore refused a PG 16 tool against a PG 16 server: $(cat "$TMP_ROOT/err")"

printf '[PASS] restore-golden-state.sh golden_sha256 and band tests\n'
