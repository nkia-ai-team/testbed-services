#!/usr/bin/env bash
# Tests for disk-io-rate.py: report on a synthetic hour, collect against a fake ssh.
set -Eeuo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
TOOL="$SCRIPT_DIR/disk-io-rate.py"
TMP_ROOT=$(mktemp -d)
trap 'rm -rf "$TMP_ROOT"' EXIT
fail() { printf '[FAIL] %s\n' "$*" >&2; exit 1; }

# Synthetic raw files at one line per second: a background of reads 10 IOPS and 40 KiB/s, writes
# 30 IOPS and 200 KiB/s, plus read bursts given as "second-in-600:MiB/s,...". Optional gap (30 s
# missing before a second) and counter reset (at a second).
make_raw() {  # make_raw <file> <burst seconds and rates> [gap_at] [reset_at]
  python3 - "$@" <<'PY'
import sys
path, spec = sys.argv[1], sys.argv[2]
gap_at = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] else -1
reset_at = int(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4] else -1
burst = {}
for item in filter(None, spec.split(",")):
    second, rate = item.split(":")
    burst[int(second)] = float(rate)
MIB = 1024 * 1024
rio = rsec = wio = wsec = 0
t = 1794290000.0
with open(path, "w") as out:
    for s in range(3600):
        if s == gap_at:
            t += 30
        if s == reset_at:
            rio = rsec = wio = wsec = 0
        rate = burst.get(s % 600) if (s % 600) in burst else None
        rio += 1000 if rate else 10
        rsec += int((rate * MIB) if rate else 40 * 1024) // 512
        wio += 30
        wsec += 200 * 1024 // 512
        out.write(f"{t:.6f} {rio} 0 {rsec} 0 {wio} 0 {wsec} 0 0 0 0\n")
        t += 1.0
PY
}
W='--write-mbps 40 --write-iops 800'

# A: 4-second bursts at 500 MiB/s every 600 s, one gap and one counter reset away from the bursts.
make_raw "$TMP_ROOT/a.raw" "100:500,101:500,102:500,103:500" 1800 2400
out=$(python3 "$TOOL" report "$TMP_ROOT/a.raw")
grep -q '^one-second samples: 3597 (1.0 h), dropped intervals: 2$' <<<"$out" || fail "gap and reset must be dropped: $out"
grep -q 'read bursts above 20.0 MiB/s or 200 IOPS: 6, median 4 s' <<<"$out" || fail "bursts: $out"
grep -Eq '^read_bps +0\.0 +0\.0 +500\.0 +500\.0 +1000\.0MiB/s' <<<"$out" || fail "read percentiles and smallest limit in MiB/s: $out"
grep -Eq '^write_iops +30\.0 +30\.0 +30\.0 +30\.0 +60\.0 IOPS' <<<"$out" || fail "write iops: $out"
echo "[PASS] report: one-second percentiles in MiB/s, smallest limit, bursts; gaps and resets dropped"

# A gap inside a burst splits it: what lies on both sides of a gap is not one burst.
make_raw "$TMP_ROOT/g.raw" "100:500,101:500,102:500,103:500" 1302
out=$(python3 "$TOOL" report "$TMP_ROOT/g.raw")
grep -q 'read bursts above 20.0 MiB/s or 200 IOPS: 7,' <<<"$out" || fail "a gap must end a burst: $out"
echo "[PASS] report: a dropped interval ends a burst"

# B: 12-second bursts at 400 MiB/s and a limit well above them: no burst gets longer, PASS.
make_raw "$TMP_ROOT/b.raw" "$(seq -s, 100 111 | sed 's/\([0-9]*\)/\1:400/g')"
out=$(python3 "$TOOL" report "$TMP_ROOT/b.raw" --read-mbps 900 --read-iops 2400 $W) || fail "a limit above the bursts must pass: $out"
grep -q 'bursts at the read limit: worst 1.00x (12 s now -> 12.0 s)' <<<"$out" || fail "$out"
echo "[PASS] report: a 12-second burst under a high limit passes (time at the limit is never below now)"

# Boundary of p99.9 < limit/2 (p99.9 is 400 MiB/s here): 800 fails, 801 passes.
if out=$(python3 "$TOOL" report "$TMP_ROOT/b.raw" --read-mbps 800 --read-iops 2400 $W); then fail "limit = 2 x p99.9 must fail: $out"; fi
grep -Eq '^read_bps .* 800 +NO$' <<<"$out" || fail "$out"
out=$(python3 "$TOOL" report "$TMP_ROOT/b.raw" --read-mbps 801 --read-iops 2400 $W) || fail "limit just above 2 x p99.9 must pass: $out"
echo "[PASS] report: p99.9 must be strictly below half the limit"

# C: rare 3-second spikes at 3000 MiB/s (one hour has one: 0.08% of seconds, under p99.9) on top of
# 100 MiB/s bursts. A 300 MiB/s limit keeps p99.9 under half of it but turns 3 s into 30 s: FAIL on
# the burst rule alone.
python3 - "$TMP_ROOT/c.raw" <<'PY'
import sys
MIB = 1024 * 1024
rio = rsec = wio = wsec = 0
t = 1794290000.0
with open(sys.argv[1], "w") as out:
    for s in range(3600):
        rate = 3000 if s in (1500, 1501, 1502) else (100 if s % 600 in range(100, 110) else None)
        rio += 1000 if rate else 10
        rsec += int((rate * MIB) if rate else 40 * 1024) // 512
        wio += 30
        wsec += 200 * 1024 // 512
        out.write(f"{t:.6f} {rio} 0 {rsec} 0 {wio} 0 {wsec} 0 0 0 0\n")
        t += 1.0
PY
if out=$(python3 "$TOOL" report "$TMP_ROOT/c.raw" --read-mbps 300 --read-iops 2400 $W); then fail "a limit that stretches a burst tenfold must fail: $out"; fi
grep -Eq '^read_bps .* 300 +yes$' <<<"$out" || fail "the p99.9 rule should pass here: $out"
grep -q 'bursts at the read limit: worst 10.00x (3 s now -> 30.0 s).*over 2.0x: 1; NO' <<<"$out" || fail "$out"
grep -q '^FAIL$' <<<"$out" || fail "$out"
echo "[PASS] report: a limit that passes p99.9 but stretches a burst beyond 2x fails"

# Rare events on a quiet hour, given as "absolute second:MiB/s:read IOPS,...".
make_abs() {  # make_abs <file> <events>
  python3 - "$@" <<'PY'
import sys
path, spec = sys.argv[1], sys.argv[2]
events = {}
for item in filter(None, spec.split(",")):
    second, mib, iops = item.split(":")
    events[int(second)] = (float(mib), int(iops))
MIB = 1024 * 1024
rio = rsec = wio = wsec = 0
t = 1794290000.0
with open(path, "w") as out:
    for s in range(3600):
        mib, iops = events.get(s, (40 / 1024, 10))
        rio += iops
        rsec += int(mib * MIB) // 512
        wio += 30
        wsec += 200 * 1024 // 512
        out.write(f"{t:.6f} {rio} 0 {rsec} 0 {wio} 0 {wsec} 0 0 0 0\n")
        t += 1.0
PY
}
events() { local from=$1 to=$2 mib=$3 iops=$4 s out=""; for s in $(seq "$from" "$to"); do out+="$s:$mib:$iops,"; done; printf '%s' "$out"; }

# D: a rare 3-second spike at 3000 MiB/s inside 97 seconds of 25 MiB/s shoulders. The shoulders are
# not slowed by a 300 MiB/s limit and must not dilute the spike's tenfold stretch.
make_abs "$TMP_ROOT/d.raw" "$(events 1000 1048 25 100)$(events 1049 1051 3000 1000)$(events 1052 1099 25 100)"
if out=$(python3 "$TOOL" report "$TMP_ROOT/d.raw" --read-mbps 300 --read-iops 5000 $W); then fail "shoulders must not hide a tenfold stretch: $out"; fi
grep -q 'read bursts above 20.0 MiB/s or 200 IOPS: 1, median 100 s' <<<"$out" || fail "one burst of 100 s expected: $out"
grep -q 'bursts at the read limit: worst 10.00x (3 s now -> 30.0 s)' <<<"$out" || fail "$out"
echo "[PASS] report: shoulders of a burst do not dilute the stretch of its spike"

# E: the byte limit is generous but a 300 IOPS limit slows a rare 3-second burst of 1000 reads/s
# about 3.3 times.
make_abs "$TMP_ROOT/e.raw" "$(events 1500 1502 100 1000)"
if out=$(python3 "$TOOL" report "$TMP_ROOT/e.raw" --read-mbps 2000 --read-iops 300 $W); then fail "an IOPS limit that stretches a burst must fail: $out"; fi
grep -Eq '^read_iops .* 300 +yes$' <<<"$out" || fail "the p99.9 rule should pass for IOPS here: $out"
grep -q 'bursts at the read limit: worst 3.33x (3 s now -> 10.0 s)' <<<"$out" || fail "$out"
out=$(python3 "$TOOL" report "$TMP_ROOT/e.raw" --read-mbps 2000 --read-iops 500 $W) || fail "500 IOPS stretches it 2x and must pass: $out"
echo "[PASS] report: the IOPS limit counts in the time a burst takes"

# F: the burst rule is 2x. A rare 3-second spike at 900 MiB/s under a 300 MiB/s limit takes 3x: FAIL.
make_abs "$TMP_ROOT/f.raw" "$(events 2000 2002 900 1000)"
if out=$(python3 "$TOOL" report "$TMP_ROOT/f.raw" --read-mbps 300 --read-iops 5000 $W); then fail "a threefold stretch must fail: $out"; fi
grep -q 'worst 3.00x (3 s now -> 9.0 s).*over 2.0x: 1; NO' <<<"$out" || fail "$out"
echo "[PASS] report: a threefold stretch of a rare burst fails"

# G: the rule is on p99.9, not p99. Data A has its bursts in 0.67% of seconds: p99 is the quiet
# background, p99.9 is 500 MiB/s. A 600 MiB/s limit keeps every burst second under it (1.00x) but
# p99.9 is above half of it: FAIL.
if out=$(python3 "$TOOL" report "$TMP_ROOT/a.raw" --read-mbps 600 --read-iops 5000 $W); then fail "p99.9 above half the limit must fail: $out"; fi
grep -Eq '^read_bps +0\.0 +0\.0 +500\.0 .* 600 +NO$' <<<"$out" || fail "$out"
grep -q 'worst 1.00x' <<<"$out" || fail "$out"
echo "[PASS] report: the limit rule reads p99.9 (p99 below half the limit is not enough)"

# H: a second under the limit between two slowed seconds still takes a full second.
make_abs "$TMP_ROOT/h.raw" "2500:3000:1000,2501:100:1000,2502:3000:1000"
if out=$(python3 "$TOOL" report "$TMP_ROOT/h.raw" --read-mbps 300 --read-iops 5000 $W); then fail "$out"; fi
grep -q 'worst 7.00x (3 s now -> 21.0 s)' <<<"$out" || fail "a second inside the slowed part needs at least one second: $out"
echo "[PASS] report: every second inside the slowed part of a burst takes at least one second"

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
