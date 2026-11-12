#!/usr/bin/env bash
# Side-effect-free tests for capture-golden-state.sh. curl and docker are fakes on PATH:
# curl writes placeholder export files, docker answers the PG tool and server versions,
# the row-count psql and the pg_dump sidecar (writing a placeholder dump into the
# mounted staging dir). Nothing leaves the machine.
#
# What is checked (v4 R1): the golden pg_dump refuses a PG tool image whose major is
# older than the server's (pg_dump 16 aborts against a PG 17 server with "server version
# mismatch") before it dumps anything or publishes a golden, accepts an equal major, and
# the default image is postgres:17-alpine. Also: the PG tool major check is the same
# function in the three scripts that copy it (capture, golden capture, golden restore);
# they cannot share one file because the runner bind-mounts capture-eval-case.sh alone
# and restore-golden-state.sh runs from a separate copy on the AI-stack host.

set -Eeuo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
GOLDEN="$SCRIPT_DIR/capture-golden-state.sh"
TMP_ROOT=$(mktemp -d)
trap 'rm -rf "$TMP_ROOT"' EXIT

fail() { printf '[FAIL] %s\n' "$*" >&2; exit 1; }

mkdir -p "$TMP_ROOT/bin"
cat >"$TMP_ROOT/bin/curl" <<'SH'
#!/usr/bin/env bash
printf 'curl %s\n' "$*" >>"$FAKE_LOG"
[[ "$*" == *'--config -'* ]] && cat >>"$FAKE_LOG.config"
all="$*"
out=''
while (( $# > 0 )); do
  case "$1" in
    --output) out=$2; shift 2 ;;
    *) shift ;;
  esac
done
[[ -n "$out" ]] && printf 'placeholder\n' >"$out"
# freshness gate: VictoriaMetrics newest sample, ClickHouse newest trace (both "now")
[[ -z "$out" && "$all" == */api/v1/query* ]] && printf '{"data":{"result":[{"value":[0,"%s"]}]}}\n' "$(date +%s)"
[[ -z "$out" && "$all" == *'max(timestamp)'* ]] && date +%s
exit 0
SH
cat >"$TMP_ROOT/bin/docker" <<'SH'
#!/usr/bin/env bash
printf 'docker %s\n' "$*" >>"$FAKE_LOG"
case "$*" in
  *--version*)
    [[ "$*" == *fake-pg16* ]] && echo 'pg_dump (PostgreSQL) 16.15' || echo 'pg_dump (PostgreSQL) 17.11'
    exit 0 ;;
  *'SHOW server_version_num'*)
    [[ -n "${FAKE_PG_HANG:-}" ]] && exec sleep 60   # 주소가 막혀 접속이 걸린 경우
    echo "${FAKE_PG_SERVER_NUM:-170011}"; exit 0 ;;
  *pg_stat_user_tables*) exit 0 ;;
  *"-tAc SELECT 1"*) exit 0 ;;
  *'pg_dump -Fc'*)
    host=''
    prev=''
    for a in "$@"; do
      [[ "$prev" == --volume ]] && host=${a%:/out}
      prev=$a
    done
    printf 'fake dump\n' >"$host/golden-pg-state.dump"
    exit 0 ;;
esac
exit 9
SH
# kubectl fake: FAKE_K8S=1 이면 trainer Deployment, 서비스 ClusterIP, 시크릿이 있는 k8s 평면.
# 없으면 kubectl 이 안 되는 호스트. sudo fake 는 늘 실패한다(시험 호스트의 진짜 kubectl 을
# sudo -n 경로로 집어 실제 클러스터를 읽는 일을 막는다).
cat >"$TMP_ROOT/bin/kubectl" <<'SH'
#!/usr/bin/env bash
printf 'kubectl %s\n' "$*" >>"$FAKE_LOG"
[[ -n "${FAKE_K8S:-}" ]] || exit 1
case "$*" in
  version*) exit 0 ;;
  *'get deployment/ai-trainer'*) exit 0 ;;
  *'get svc victoriametrics '*) printf '10.43.0.1' ;;
  *'get svc clickhouse '*) printf '10.43.0.2' ;;
  *'get svc ai-observer '*) printf '10.43.0.3' ;;
  *'get svc pg-rw '*) printf '10.43.0.4' ;;
  *'get secret pg-superuser'*'.data.username'*) printf 'fakeadmin' | base64 ;;
  *'get secret pg-superuser'*'.data.password'*) printf 'fake-admin-secret' | base64 ;;
  *'get secret polestar-secret'*'.data.CLICKHOUSE_PASSWORD'*) printf 'fake-ch-secret' | base64 ;;
  *) exit 1 ;;
esac
SH
printf '#!/usr/bin/env bash\nexit 1\n' >"$TMP_ROOT/bin/sudo"
chmod +x "$TMP_ROOT/bin/curl" "$TMP_ROOT/bin/docker" "$TMP_ROOT/bin/kubectl" "$TMP_ROOT/bin/sudo"
export PATH="$TMP_ROOT/bin:$PATH" FAKE_LOG="$TMP_ROOT/fake.log"
export PG_PASSWORD=unused-test-value CH_PASSWORD=unused-test-value

grep -q 'PG_DUMP_IMAGE="${PG_DUMP_IMAGE:-postgres:17-alpine}"' "$GOLDEN" || fail 'default PG tool image is not 17'

# 1. Same major (17 tool, 17 server): the golden is captured with its dump.
: >"$FAKE_LOG"
PG_DUMP_IMAGE=fake-pg17 "$GOLDEN" --skip-freshness --output-root "$TMP_ROOT/g1" >"$TMP_ROOT/out" 2>"$TMP_ROOT/err" ||
  fail "golden capture failed: $(cat "$TMP_ROOT/err")"
dump=$(find "$TMP_ROOT/g1" -name golden-pg-state.dump)
[[ -s "$dump" ]] || fail 'golden dump missing'
grep -q 'pg_dump -Fc' "$FAKE_LOG" || fail 'pg_dump did not run'

# 2. Older tool than the server (16 tool, 17 server): refused before pg_dump, nothing published.
: >"$FAKE_LOG"
if PG_DUMP_IMAGE=fake-pg16 "$GOLDEN" --skip-freshness --output-root "$TMP_ROOT/g2" >"$TMP_ROOT/out" 2>"$TMP_ROOT/err"; then
  fail 'golden capture ran a PG 16 tool against a PG 17 server'
fi
grep -q 'PG 도구 16 은 서버 17 보다 오래돼' "$TMP_ROOT/err" || fail "unexpected refusal: $(cat "$TMP_ROOT/err")"
! grep -q 'pg_dump -Fc' "$FAKE_LOG" || fail 'pg_dump ran despite the major mismatch'
[[ -z "$(find "$TMP_ROOT/g2" -mindepth 1 -maxdepth 1 ! -name '.*' 2>/dev/null)" ]] ||
  fail 'a golden was published despite the major mismatch'

# 3. Same older major (16 tool, 16 server): allowed.
FAKE_PG_SERVER_NUM=160015 PG_DUMP_IMAGE=fake-pg16 "$GOLDEN" --skip-freshness --output-root "$TMP_ROOT/g3" \
  >/dev/null 2>"$TMP_ROOT/err" || fail "refused a PG 16 tool against a PG 16 server: $(cat "$TMP_ROOT/err")"

# 4. The server version query runs under a time limit and a connect timeout, like the other PG
#    steps: a hung connection fails the capture within the limit instead of stalling it.
: >"$FAKE_LOG"
start=$(date +%s)
if FAKE_PG_HANG=1 PG_TOOL_CHECK_TIMEOUT_SEC=2 PG_DUMP_IMAGE=fake-pg17 "$GOLDEN" --skip-freshness \
     --output-root "$TMP_ROOT/g4" >/dev/null 2>"$TMP_ROOT/err"; then
  fail 'golden capture went on with a hung PG connection'
fi
(( $(date +%s) - start < 20 )) || fail 'the PG tool check did not stop within its time limit'
grep -q 'PG 도구, 서버 메이저를 읽지 못했다' "$TMP_ROOT/err" || fail "unexpected failure: $(cat "$TMP_ROOT/err")"
grep -q 'PGCONNECT_TIMEOUT=10.*SHOW server_version_num' "$FAKE_LOG" || fail 'the server version psql has no connect timeout'
! grep -q 'pg_dump -Fc' "$FAKE_LOG" || fail 'pg_dump ran after a failed PG tool check'

# 5. The copied check is the same function in all three scripts.
extract() { sed -n '/^pg_tool_major_check() {$/,/^}$/p' "$1"; }
ref=$(extract "$SCRIPT_DIR/capture-eval-case.sh")
[[ -n "$ref" ]] || fail 'pg_tool_major_check not found in capture-eval-case.sh'
for f in capture-golden-state.sh restore-golden-state.sh; do
  [[ "$(extract "$SCRIPT_DIR/$f")" == "$ref" ]] || fail "pg_tool_major_check in $f differs from capture-eval-case.sh"
done

# 6. 예전 평면(kubectl 안 됨): 호스트 포트 기본값 그대로, PG 사이드카는 기본 네트워크.
: >"$FAKE_LOG"
PG_DUMP_IMAGE=fake-pg17 "$GOLDEN" --skip-freshness --output-root "$TMP_ROOT/g6" >"$TMP_ROOT/out" 2>"$TMP_ROOT/err" ||
  fail "docker-plane capture failed: $(cat "$TMP_ROOT/err")"
grep -q 'runtime=docker' "$TMP_ROOT/out" || fail 'auto runtime without kubectl is not docker'
grep -q 'pg_dump -Fc --host 192.168.230.119 --port 15432 --username lucida' "$FAKE_LOG" ||
  fail 'docker plane did not keep the host-port PG defaults'
grep -q 'http://192.168.230.119:18428/api/v1/export' "$FAKE_LOG" || fail 'docker plane did not keep the VM default'
! grep -q -- '--network host' "$FAKE_LOG" || fail 'docker plane put a PG sidecar on the host network'

# 7. k8s 평면(v4 E11): 주지 않은 주소, 비밀번호를 클러스터에서 채운다. freshness gate 도 ClusterIP 로 간다.
: >"$FAKE_LOG"; : >"$FAKE_LOG.config"
FAKE_K8S=1 PG_PASSWORD='' CH_PASSWORD='' PG_DUMP_IMAGE=fake-pg17 "$GOLDEN" --output-root "$TMP_ROOT/g7" \
  >"$TMP_ROOT/out" 2>"$TMP_ROOT/err" || fail "k8s capture failed: $(cat "$TMP_ROOT/err")"
grep -q 'runtime=k8s' "$TMP_ROOT/out" || fail 'auto runtime with a trainer Deployment is not k8s'
grep -q 'curl .*http://10.43.0.3:8087/api/v1/health' "$FAKE_LOG" || fail 'observer health did not use the ClusterIP'
grep -q 'curl .*http://10.43.0.1:8428/api/v1/query' "$FAKE_LOG" || fail 'VM freshness did not use the ClusterIP'
grep -q 'curl .*http://10.43.0.1:8428/api/v1/export' "$FAKE_LOG" || fail 'VM export did not use the ClusterIP'
grep -q 'curl .*http://10.43.0.2:8123/' "$FAKE_LOG" || fail 'ClickHouse did not use the ClusterIP'
grep -q 'user = "lucida:fake-ch-secret"' "$FAKE_LOG.config" || fail 'ClickHouse password was not read from the secret'
grep -q -- '--network host .*pg_dump -Fc --host 10.43.0.4 --port 5432 --username fakeadmin' "$FAKE_LOG" ||
  fail 'pg_dump did not run on the host network against pg-rw with the superuser secret'
grep -q -- '--network host .*-h 10.43.0.4 -p 5432 -U fakeadmin .*SHOW server_version_num' "$FAKE_LOG" ||
  fail 'the PG major check did not use the host network and pg-rw'
grep -q -- '--network host .*-h 10.43.0.4 -p 5432 -U fakeadmin .*-tAc SELECT 1' "$FAKE_LOG" ||
  fail 'the freshness PG check did not use the host network and pg-rw'
grep -q -- '--network host .*pg_stat_user_tables' "$FAKE_LOG" || fail 'the row-count psql did not use the host network'
! grep -q -e fake-admin-secret -e fake-ch-secret "$TMP_ROOT/out" "$TMP_ROOT/err" || fail 'a secret value reached the output'
meta=$(find "$TMP_ROOT/g7" -name meta.json)
[[ -s "$meta" ]] || fail 'k8s capture published no meta.json'
for t in trainer_stream_generations trainer_stream_drift trainer_log_generations trainer_seed_verdicts trainer_prompts \
         trainer_stream_threshold trainer_stream_periodic_profile trainer_stream_distribution_profile \
         trainer_masking_rules trainer_thresholds trainer_periodic_templates trainer_template_profiles; do
  jq -e --arg t "$t" '.pg_ai_state.tables | index($t) != null' "$meta" >/dev/null || fail "golden table list lacks $t"
  grep -q -- "--table public.$t " "$FAKE_LOG" || fail "pg_dump did not dump $t"
done
[[ "$(jq '.pg_ai_state.tables | length' "$meta")" == 28 ]] || fail 'golden table list is not 28 tables'
! jq -e '.pg_ai_state.tables | index("trainer_trace_window_threshold") != null' "$meta" >/dev/null ||
  fail 'trainer_trace_window_threshold must stay out of the golden'

# 8. k8s 에서도 준 값은 그대로 쓴다(주소, PG 자격). 그때 PG 시크릿은 읽지 않는다.
: >"$FAKE_LOG"
FAKE_K8S=1 VM_URL=http://vm.explicit:1 PG_HOST=pg.explicit PG_PORT=6543 PG_USER=given PG_PASSWORD=given-pw \
  PG_DUMP_IMAGE=fake-pg17 "$GOLDEN" --skip-freshness --output-root "$TMP_ROOT/g8" >/dev/null 2>"$TMP_ROOT/err" ||
  fail "k8s capture with explicit values failed: $(cat "$TMP_ROOT/err")"
grep -q 'curl .*http://vm.explicit:1/api/v1/export' "$FAKE_LOG" || fail 'explicit VM_URL was overridden'
grep -q 'pg_dump -Fc --host pg.explicit --port 6543 --username given' "$FAKE_LOG" || fail 'explicit PG settings were overridden'
! grep -q 'get secret pg-superuser' "$FAKE_LOG" || fail 'the PG secret was read although PG_PASSWORD was given'

# 9. RUNTIME=k8s 인데 kubectl 이 안 되면 아무것도 만들지 않고 죽는다(호스트 포트로 조용히 떨어지지 않는다).
: >"$FAKE_LOG"
if RUNTIME=k8s PG_DUMP_IMAGE=fake-pg17 "$GOLDEN" --skip-freshness --output-root "$TMP_ROOT/g9" >/dev/null 2>"$TMP_ROOT/err"; then
  fail 'RUNTIME=k8s without kubectl went on'
fi
grep -q 'RUNTIME=k8s but no usable kubectl' "$TMP_ROOT/err" || fail "unexpected failure: $(cat "$TMP_ROOT/err")"
! grep -q 'pg_dump -Fc' "$FAKE_LOG" || fail 'pg_dump ran without kubectl in k8s mode'

# 10. k8s 에서 시크릿을 못 읽으면 덤프 전에 죽는다.
: >"$FAKE_LOG"
cat >"$TMP_ROOT/bin/kubectl.nosecret" <<'SH'
#!/usr/bin/env bash
case "$*" in *'get secret'*) exit 1 ;; esac
exec "$(dirname "$0")/kubectl" "$@"
SH
chmod +x "$TMP_ROOT/bin/kubectl.nosecret"
if FAKE_K8S=1 KUBECTL="$TMP_ROOT/bin/kubectl.nosecret" PG_PASSWORD='' PG_DUMP_IMAGE=fake-pg17 "$GOLDEN" --skip-freshness \
     --output-root "$TMP_ROOT/g10" >/dev/null 2>"$TMP_ROOT/err"; then
  fail 'k8s capture went on without PG credentials'
fi
grep -q 'k8s secret polestar/pg-superuser is not readable' "$TMP_ROOT/err" || fail "unexpected failure: $(cat "$TMP_ROOT/err")"
! grep -q 'pg_dump -Fc' "$FAKE_LOG" || fail 'pg_dump ran without PG credentials'

printf '[PASS] capture-golden-state.sh PG tool major and runtime tests\n\'
