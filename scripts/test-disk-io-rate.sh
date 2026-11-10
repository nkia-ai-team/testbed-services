#!/usr/bin/env bash
# Tests for disk-io-rate.py: report on a synthetic hour, collect against a fake ssh.
set -Eeuo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
TOOL="$SCRIPT_DIR/disk-io-rate.py"
TMP_ROOT=$(mktemp -d)
trap 'rm -rf "$TMP_ROOT"' EXIT
fail() { printf '[FAIL] %s\n' "$*" >&2; exit 1; }

# One hour at one line per second. Reads 10 IOPS and 40 KB/s, writes 30 IOPS and 200 KB/s, and every
# 600 s a 4-second read burst of 500 MB/s (the ledger reconciliation shape). One 30-second gap and one
# counter reset must not turn into rates.
python3 - "$TMP_ROOT/hour.raw" <<'PY'
import sys
rio = rsec = wio = wsec = 0
t = 1794290000.0
with open(sys.argv[1], "w") as out:
    for s in range(3600):
        if s == 1800:
            t += 30                       # collector restarted: a gap
        if s == 2400:
            rio = rsec = wio = wsec = 0   # device re-created: counters start again
        burst = (s % 600) in (100, 101, 102, 103)
        rio += 1000 if burst else 10
        rsec += (500_000_000 if burst else 40_000) // 512
        wio += 30
        wsec += 200_000 // 512
        out.write(f"{t:.6f} {rio} 0 {rsec} 0 {wio} 0 {wsec} 0 0 0 0\n")
        t += 1.0
PY

out=$(python3 "$TOOL" report "$TMP_ROOT/hour.raw")
grep -q '^one-second samples: 3597 ' <<<"$out" || fail "gap and reset must be dropped: $out"
grep -q 'read bursts above 20.0 MB/s: 6, median 4 s' <<<"$out" || fail "bursts: $out"
grep -Eq '^read_bps +0\.0 +0\.0 +500\.0 +500\.0 +1000\.0MB/s' <<<"$out" || fail "read percentiles and smallest limit: $out"
grep -Eq '^write_iops +30\.0 +30\.0 +30\.0 +30\.0 +60\.0 IOPS' <<<"$out" || fail "write iops: $out"
echo "[PASS] report: one-second percentiles, smallest limit, bursts; gaps and resets dropped"

# The 160 MB/s read limit stretches a 4 s reconciliation to about 12.5 s and sits under twice p99.9.
if out=$(python3 "$TOOL" report "$TMP_ROOT/hour.raw" --read-mbps 160 --write-mbps 40 --read-iops 1600 --write-iops 800); then
  fail "a read limit the bursts saturate must fail: $out"
fi
grep -q 'median burst at the read limit: 12.5 s (now 4 s), same order of magnitude: NO' <<<"$out" || fail "$out"
grep -Eq '^read_bps .* 160 +NO$' <<<"$out" || fail "read limit verdict: $out"
grep -q '^FAIL$' <<<"$out" || fail "$out"
# A limit above twice p99.9 keeps the burst in the same order of magnitude.
out=$(python3 "$TOOL" report "$TMP_ROOT/hour.raw" --read-mbps 1200 --write-mbps 40 --read-iops 2400 --write-iops 800) \
  || fail "limits above twice p99.9 must pass: $out"
grep -q '^PASS$' <<<"$out" || fail "$out"
echo "[PASS] report judges limits: p99.9 under half the limit and the burst keeps its order of magnitude"

# collect: one ssh to the host, the loop reads the volume's block statistics, lines are appended.
mkdir -p "$TMP_ROOT/bin"
cat >"$TMP_ROOT/bin/ssh" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$@" >"$FAKE_ARGS"
printf '1794290000.000000 1 0 8 0 1 0 8 0 0 0 0\n1794290001.000000 2 0 16 0 2 0 16 0 0 0 0\n'
SH
chmod +x "$TMP_ROOT/bin/ssh"
export FAKE_ARGS="$TMP_ROOT/ssh.args"
printf 'old line\n' >"$TMP_ROOT/c.raw"
PATH="$TMP_ROOT/bin:$PATH" python3 "$TOOL" collect --host root@66.invalid --volume vmdata/vm-103-disk-1 --seconds 5 --out "$TMP_ROOT/c.raw"
[[ $(wc -l <"$TMP_ROOT/c.raw") -eq 3 && "$(head -1 "$TMP_ROOT/c.raw")" == "old line" ]] || fail "collect must append"
grep -qx 'root@66.invalid' "$FAKE_ARGS" || fail "ssh target"
grep -q 'readlink -f /dev/vmdata/vm-103-disk-1' "$FAKE_ARGS" || fail "the loop must resolve the volume"
grep -q '/sys/block/' "$FAKE_ARGS" && grep -q 'end=$((SECONDS + 5))' "$FAKE_ARGS" || fail "the loop must read sysfs for the given seconds"
! grep -Eq 'qm |qmp|socat' "$FAKE_ARGS" || fail "the loop must not talk to the VM's monitor"
if PATH="$TMP_ROOT/bin:$PATH" python3 "$TOOL" collect --host h --volume 'vmdata/x;rm -rf /' --out "$TMP_ROOT/x.raw" 2>/dev/null; then
  fail "a volume name with shell characters must be refused"
fi
echo "[PASS] collect: one ssh, sysfs only, appends, refuses odd volume names"
