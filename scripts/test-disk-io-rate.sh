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
grep -q 'read bursts above 20.0 MiB/s or 200 IOPS (for reading only): 6, median 4 s' <<<"$out" || fail "bursts: $out"
grep -Eq '^read_bps +0\.0 +0\.0 +500\.0 +500\.0 +1000\.0MiB/s' <<<"$out" || fail "read percentiles and smallest limit in MiB/s: $out"
grep -Eq '^write_iops +30\.0 +30\.0 +30\.0 +30\.0 +60\.0 IOPS' <<<"$out" || fail "write iops: $out"
echo "[PASS] report: one-second percentiles in MiB/s, smallest limit, bursts; gaps and resets dropped"

# Rare events on a quiet span, given as "start:length:MiB/s:read IOPS;..." (absolute seconds) or
# "R:every:start:length:MiB/s:read IOPS[:count];..." (recurring). Same form as the review's gen2.py.
make_ev() {  # make_ev <file> <seconds> <events>
  python3 - "$@" <<'PY'
import sys
out, total, spec = sys.argv[1], int(sys.argv[2]), sys.argv[3]
MIB = 1024 * 1024
ev = {}
for item in filter(None, spec.split(";")):
    p = item.split(":")
    if p[0] == "R":
        every, start, ln, mib, iops = int(p[1]), int(p[2]), int(p[3]), float(p[4]), int(p[5])
        count = int(p[6]) if len(p) > 6 else total // every
        for k in range(count):
            for d in range(ln):
                ev[k * every + start + d] = (mib, iops)
    else:
        start, ln, mib, iops = int(p[0]), int(p[1]), float(p[2]), int(p[3])
        for d in range(ln):
            ev[start + d] = (mib, iops)
rio = rsec = wio = wsec = 0
t = 1794290000.0
with open(out, "w") as f:
    for s in range(total):
        mib, iops = ev.get(s, (40 / 1024, 10))
        rio += iops
        rsec += int(mib * MIB) // 512
        wio += 30
        wsec += 200 * 1024 // 512
        f.write(f"{t:.6f} {rio} 0 {rsec} 0 {wio} 0 {wsec} 0 0 0 0\n")
        t += 1.0
PY
}
verdict() { grep -E '^read queue at the limits' <<<"$1"; }
slowest() { sed -n 's/.*the slowest second.s reads finish \([0-9.]*\) s.*/\1/p' <<<"$1"; }
passes() { python3 "$TOOL" report "$@" $W >/dev/null; }   # passes <raw> <limits...>
smallest() { python3 "$TOOL" report "$1" | sed -n "s|^smallest read $2 limit meeting both rules (other read limit open): ||p"; }

# Normal reconciliation (review 3's r1): 24 h, every 600 s 15 s at 400 MiB/s and 1000 IOPS. Rule 2
# asks 801 MiB/s and 2001 IOPS; there every second's reads fit in the second, and the smallest
# limits meeting both rules are those same numbers.
make_ev "$TMP_ROOT/r1.raw" 86400 "R:600:100:15:400:1000"
out=$(python3 "$TOOL" report "$TMP_ROOT/r1.raw" --read-mbps 801 --read-iops 2001 $W) || fail "normal reconciliation must pass: $out"
[[ "$(slowest "$out")" == 0.500 ]] || fail "1000 reads under 2001 IOPS are done in half a second: $out"
[[ "$(smallest "$TMP_ROOT/r1.raw" MiB/s)" == 801 && "$(smallest "$TMP_ROOT/r1.raw" IOPS)" == 2001 ]] || fail "smallest limits for r1"
passes "$TMP_ROOT/r1.raw" --read-mbps 800 --read-iops 2001 && fail "800 must fail rule 2"
echo "[PASS] queue: an even reconciliation is not slowed; p99.9 must be strictly below half the limit"

# A gap inside a burst splits it (bursts line).
make_raw "$TMP_ROOT/g.raw" "100:500,101:500,102:500,103:500" 1302
out=$(python3 "$TOOL" report "$TMP_ROOT/g.raw")
grep -q 'read bursts above 20.0 MiB/s or 200 IOPS (for reading only): 7,' <<<"$out" || fail "a gap must end a burst: $out"
echo "[PASS] report: a dropped interval ends a burst"

# A gap also starts the queue again (it lasted at least five seconds): two seconds of 500 MiB under
# 300 MiB/s, one after the other, leave 200 MiB queued and the second one ends at 2.333 s (FAIL).
# With a gap between them each is done at 1.667 s (PASS).
make_ev "$TMP_ROOT/nogap.raw" 3600 "1000:2:500:100"
passes "$TMP_ROOT/nogap.raw" --read-mbps 300 && fail "back-to-back seconds must carry the queue"
[[ "$(slowest "$(python3 "$TOOL" report "$TMP_ROOT/nogap.raw" --read-mbps 300 || true)")" == 2.333 ]] || fail "carried queue"
python3 - "$TMP_ROOT/nogap.raw" "$TMP_ROOT/gap.raw" <<'PY'
import sys
lines = open(sys.argv[1]).read().splitlines()
out = []
for k, line in enumerate(lines):
    t, rest = line.split(" ", 1)
    shift = 30 if k > 1000 else 0
    out.append(f"{float(t) + shift:.6f} {rest}")
    if k == 1000:   # after the first heavy second: a row 30 s later with the same counters (a dropped interval)
        out.append(f"{float(t) + 30:.6f} {rest}")
open(sys.argv[2], "w").write("\n".join(out) + "\n")
PY
out=$(python3 "$TOOL" report "$TMP_ROOT/gap.raw" --read-mbps 300 $W) || fail "a gap must start the queue again: $out"
[[ "$(slowest "$out")" == 1.667 ]] || fail "$out"
echo "[PASS] queue: back-to-back seconds carry the queue, a dropped interval starts it again"

# The three holes found in reviews 3 and 4 all FAIL under 300 MiB/s (or 300 IOPS).
make_ev "$TMP_ROOT/sh.raw" 3600 "1000:49:25:100;1049:3:3000:1000;1052:48:25:100"     # shoulders (r2)
passes "$TMP_ROOT/sh.raw" --read-mbps 300 && fail "shoulders must not hide a spike"
for join in "25:100" "15:300"; do                                                    # inside (B1d, B1f)
  make_ev "$TMP_ROOT/in.raw" 3600 "1000:3:3000:1000;1003:94:$join;1097:3:3000:1000"
  passes "$TMP_ROOT/in.raw" --read-mbps 300 && fail "light reads between spikes must not hide them ($join)"
done
make_ev "$TMP_ROOT/k1.raw" 3600 "1500:3:8:1000"                                      # small-block IOPS (K1)
passes "$TMP_ROOT/k1.raw" --read-mbps 2000 --read-iops 300 && fail "an IOPS-only burst must fail with both limits"
passes "$TMP_ROOT/k1.raw" --read-iops 300 && fail "an IOPS-only burst must fail with the IOPS limit alone"
[[ "$(smallest "$TMP_ROOT/k1.raw" IOPS)" == 750 ]] || fail "three seconds of 1000 reads need 750 IOPS"
passes "$TMP_ROOT/k1.raw" --read-iops 750 || fail "750 IOPS must pass"
passes "$TMP_ROOT/k1.raw" --read-iops 749 && fail "749 IOPS must fail"
echo "[PASS] queue: shoulders, light reads between spikes and small-block IOPS bursts all fail at 300"

# Adding reads never makes a second finish earlier: the spikes alone, with light reads between them,
# and (review 5's 2% case) one spike followed by 20 s just under or just over the limit.
make_ev "$TMP_ROOT/alone.raw" 3600 "1000:3:3000:1000;1097:3:3000:1000"
make_ev "$TMP_ROOT/in.raw" 3600 "1000:3:3000:1000;1003:94:25:100;1097:3:3000:1000"
a=$(slowest "$(python3 "$TOOL" report "$TMP_ROOT/alone.raw" --read-mbps 300 $W || true)")
b=$(slowest "$(python3 "$TOOL" report "$TMP_ROOT/in.raw" --read-mbps 300 $W || true)")
[[ -n "$a" && -n "$b" ]] && python3 -c "import sys; sys.exit(0 if float('$b') >= float('$a') else 1)" || fail "light reads made a second finish earlier: $a -> $b"
for after in "" ";1001:20:297:100" ";1001:20:303:100"; do
  make_ev "$TMP_ROOT/x2.raw" 3600 "1000:1:3000:1000$after"
  passes "$TMP_ROOT/x2.raw" --read-mbps 300 && fail "a spike stays a FAIL whatever follows it ($after)"
done
echo "[PASS] queue: added reads never help ($a s alone, $b s with light reads between)"

# Monotone in the limit (review 5 x1, x3): once a limit passes, every higher one passes, and the
# smallest limit report prints is that boundary.
make_ev "$TMP_ROOT/x3.raw" 86400 "30000:1:360:1000;30001:60:285:1000"
make_ev "$TMP_ROOT/x1.raw" 86400 "30000:1:3000:1000;30001:60:400:1000"
for f in x3 x1; do
  m=$(smallest "$TMP_ROOT/$f.raw" MiB/s)
  passes "$TMP_ROOT/$f.raw" --read-mbps "$m" || fail "$f: the printed smallest limit $m must pass"
  passes "$TMP_ROOT/$f.raw" --read-mbps "$((m - 1))" && fail "$f: $((m - 1)) must fail"
  seen_pass=0
  for L in 144 250 281 282 284 285 300 320 361 399 400 1000 1499 1500 1700 3000; do
    if passes "$TMP_ROOT/$f.raw" --read-mbps "$L"; then seen_pass=1
    elif (( seen_pass )); then fail "$f: $L fails after a lower limit passed"; fi
    if (( L >= m )); then (( seen_pass )) || fail "$f: $L >= smallest $m must pass"; fi
  done
done
[[ "$(smallest "$TMP_ROOT/x3.raw" MiB/s)" == 282 && "$(smallest "$TMP_ROOT/x1.raw" MiB/s)" == 1500 ]] || fail "x3 282, x1 1500"
echo "[PASS] queue: the rule is monotone in the limit and report's smallest limit is the boundary (x3 282, x1 1500)"

# Examples in the documentation: one second at 3000 MiB/s needs 1500, three in a row need 2250.
make_ev "$TMP_ROOT/b4.raw" 3600 "2000:1:3000:1000"
make_ev "$TMP_ROOT/r2b.raw" 3600 "2000:3:3000:1000"
[[ "$(smallest "$TMP_ROOT/b4.raw" MiB/s)" == 1500 && "$(smallest "$TMP_ROOT/r2b.raw" MiB/s)" == 2250 ]] || fail "1500 and 2250"
# The printed time and the verdict agree at the boundary (the background trickle does not tip it).
# The second also carries the 40 KiB/s background: 3000.04 MiB under 1500 is 2.00003 s, read in
# milliseconds as 2.000 (review 5 R4).
make_ev "$TMP_ROOT/b4t.raw" 3600 "2000:1:3000.0390625:1000"
out=$(python3 "$TOOL" report "$TMP_ROOT/b4t.raw" --read-mbps 1500 $W) || fail "$out"
grep -q 'finish 2.000 s after it starts (at most 2 s), seconds later than that: 0; yes' <<<"$out" || fail "$out"
out=$(python3 "$TOOL" report "$TMP_ROOT/b4.raw" --read-mbps 1499 $W || true)
grep -q 'finish 2.001 s after it starts (at most 2 s), seconds later than that: 1; NO' <<<"$out" || fail "$out"
echo "[PASS] queue: 1500 for one second at 3000 MiB/s, 2250 for three; printed time and verdict agree"

# A light second between two heavy ones still waits behind them (review 3's H): 3000, 100, 3000.
make_ev "$TMP_ROOT/h.raw" 3600 "2500:1:3000:1000;2501:1:100:1000;2502:1:3000:1000"
out=$(python3 "$TOOL" report "$TMP_ROOT/h.raw" --read-mbps 300 $W || true)
[[ "$(slowest "$out")" == 18.333 ]] || fail "the third second waits for 5200 MiB at 300 MiB/s: $out"
echo "[PASS] queue: a light second between heavy ones keeps the backlog"

# Rare peaks inside a reconciliation (review 4 B2): 40 of the 144 daily reconciliations start with one
# second at 2000 MiB/s. Rule 2 alone says 801; the peak needs 1000.
make_ev "$TMP_ROOT/b2.raw" 86400 "R:600:100:12:400:1000;R:600:100:1:2000:1000:40"
out=$(python3 "$TOOL" report "$TMP_ROOT/b2.raw")
grep -Eq '^read_bps .* 800\.0MiB/s' <<<"$out" || fail "rule 2 alone: $out"
[[ "$(smallest "$TMP_ROOT/b2.raw" MiB/s)" == 1000 ]] || fail "$out"
passes "$TMP_ROOT/b2.raw" --read-mbps 999 && fail "999 must fail"
passes "$TMP_ROOT/b2.raw" --read-mbps 1000 || fail "1000 must pass"
echo "[PASS] report: the smallest limit meeting both rules follows rare peaks (1000, rule 2 alone 801)"

# The limit rule reads p99.9, not p99 (data A: bursts in 0.67% of seconds).
if out=$(python3 "$TOOL" report "$TMP_ROOT/a.raw" --read-mbps 600 --read-iops 5000 $W); then fail "p99.9 above half the limit must fail: $out"; fi
grep -Eq '^read_bps +0\.0 +0\.0 +500\.0 .* 600 +NO$' <<<"$out" || fail "$out"
echo "[PASS] report: the limit rule reads p99.9 (p99 below half the limit is not enough)"

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
