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
[[ "$*" == *'--config -'* ]] && cat >/dev/null
out=''
while (( $# > 0 )); do
  case "$1" in
    --output) out=$2; shift 2 ;;
    *) shift ;;
  esac
done
[[ -n "$out" ]] && printf 'placeholder\n' >"$out"
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
chmod +x "$TMP_ROOT/bin/curl" "$TMP_ROOT/bin/docker"
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

printf '[PASS] capture-golden-state.sh PG tool major tests\n'
