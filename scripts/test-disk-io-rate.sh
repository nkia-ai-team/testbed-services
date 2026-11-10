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

# Normal reconciliation (review 3's r1): 24 h, every 600 s 15 s at 400 MiB/s and 1000 IOPS. Rule 2
# asks 801 MiB/s and 2001 IOPS; there no second reads more than a limit, so rule 1 is 1.00x and the
# smallest limits meeting both rules are those same numbers.
make_ev "$TMP_ROOT/r1.raw" 86400 "R:600:100:15:400:1000"
out=$(python3 "$TOOL" report "$TMP_ROOT/r1.raw" --read-mbps 801 --read-iops 2001 $W) || fail "normal reconciliation must pass: $out"
[[ "$(verdict "$out")" == "read queue at the limits: no second reads more than a limit, 1.00x; yes" ]] || fail "$out"
grep -q 'smallest read MiB/s limit meeting both rules (other read limit open): 801$' <<<"$out" || fail "$out"
grep -q 'smallest read IOPS limit meeting both rules (other read limit open): 2001$' <<<"$out" || fail "$out"
if out=$(python3 "$TOOL" report "$TMP_ROOT/r1.raw" --read-mbps 800 --read-iops 2001 $W); then fail "800 must fail rule 2: $out"; fi
grep -Eq '^read_bps .* 800 +NO$' <<<"$out" || fail "$out"
echo "[PASS] queue: an even reconciliation is never slowed; p99.9 must be strictly below half the limit"

# A gap inside a burst splits it (bursts line); the queue too starts again after a gap.
make_raw "$TMP_ROOT/g.raw" "100:500,101:500,102:500,103:500" 1302
out=$(python3 "$TOOL" report "$TMP_ROOT/g.raw")
grep -q 'read bursts above 20.0 MiB/s or 200 IOPS (for reading only): 7,' <<<"$out" || fail "a gap must end a burst: $out"
echo "[PASS] report: a dropped interval ends a burst"

# The three holes found in reviews 3 and 4. Each must FAIL under a 300 MiB/s (or 300 IOPS) limit.
# Shoulders: 97 s of 25 MiB/s around a 3-second 3000 MiB/s spike (review 3 r2).
make_ev "$TMP_ROOT/sh.raw" 3600 "1000:49:25:100;1049:3:3000:1000;1052:48:25:100"
if out=$(python3 "$TOOL" report "$TMP_ROOT/sh.raw" --read-mbps 300 $W); then fail "shoulders must not hide a spike: $out"; fi
grep -q 'worst 10.83x (3 s over the limits now -> 32.5 s until the queue empties), over 2.0x: 1; NO' <<<"$out" || fail "$out"
# Inside: two such spikes joined by 94 s of light reads, one burst (review 4 B1d), and the same with
# 300 IOPS in the joining seconds (B1f).
for join in "25:100" "15:300"; do
  make_ev "$TMP_ROOT/in.raw" 3600 "1000:3:3000:1000;1003:94:$join;1097:3:3000:1000"
  if out=$(python3 "$TOOL" report "$TMP_ROOT/in.raw" --read-mbps 300 $W); then fail "light reads between spikes must not hide them ($join): $out"; fi
  grep -Eq 'read queue at the limits: 2 episodes, worst ([0-9.]+)x .*over 2.0x: 2; NO' <<<"$out" || fail "$join: $out"
done
# IOPS: a 3-second burst of 1000 reads/s in small blocks (8 MiB/s, under the 20 MiB/s burst line;
# review 4 K1) under a 300 IOPS limit, with a generous byte limit and with the IOPS limit alone.
make_ev "$TMP_ROOT/k1.raw" 3600 "1500:3:8:1000"
for limits in "--read-mbps 2000 --read-iops 300" "--read-iops 300"; do
  if out=$(python3 "$TOOL" report "$TMP_ROOT/k1.raw" $limits $W); then fail "an IOPS-only burst must fail ($limits): $out"; fi
  grep -q 'worst 3.42x (3 s over the limits now -> 10.3 s until the queue empties)' <<<"$out" || fail "$limits: $out"
done
grep -q 'smallest read IOPS limit meeting both rules (other read limit open): 505$' <<<"$out" || fail "$out"
out=$(python3 "$TOOL" report "$TMP_ROOT/k1.raw" --read-iops 505 $W) || fail "505 IOPS must pass: $out"
echo "[PASS] queue: shoulders, light reads between spikes and small-block IOPS bursts all fail at 300"

# Adding light reads never turns a FAIL into a PASS: the same spikes alone and with the join.
make_ev "$TMP_ROOT/alone.raw" 3600 "1000:3:3000:1000;1097:3:3000:1000"
a=$({ python3 "$TOOL" report "$TMP_ROOT/alone.raw" --read-mbps 300 $W || true; } | sed -n 's/.*worst \([0-9.]*\)x.*/\1/p')
make_ev "$TMP_ROOT/in.raw" 3600 "1000:3:3000:1000;1003:94:25:100;1097:3:3000:1000"
b=$({ python3 "$TOOL" report "$TMP_ROOT/in.raw" --read-mbps 300 $W || true; } | sed -n 's/.*worst \([0-9.]*\)x.*/\1/p')
[[ -n "$a" && -n "$b" ]] && python3 -c "import sys; sys.exit(0 if float('$b') >= float('$a') else 1)" || fail "light reads lowered the ratio: $a -> $b"
echo "[PASS] queue: light reads only lengthen the queue ($a x alone, $b x with them)"

# Ratio 2: a 3-second spike at 900 MiB/s under 300 takes 9 s (3.00x): FAIL. A single second at
# 3000 MiB/s needs at least 1500 (half its rate).
make_ev "$TMP_ROOT/f.raw" 3600 "2000:3:900:1000"
if out=$(python3 "$TOOL" report "$TMP_ROOT/f.raw" --read-mbps 300 $W); then fail "a threefold stretch must fail: $out"; fi
grep -q 'worst 3.00x (3 s over the limits now -> 9.0 s until the queue empties), over 2.0x: 1; NO' <<<"$out" || fail "$out"
make_ev "$TMP_ROOT/b4.raw" 3600 "2000:1:3000:1000"
grep -q 'smallest read MiB/s limit meeting both rules (other read limit open): 1501$' <<<"$(python3 "$TOOL" report "$TMP_ROOT/b4.raw")" \
  || fail "one second at 3000 MiB/s needs a limit above half of it"
echo "[PASS] queue: twice as long is the most a heavy run may take"

# A light second between two heavy ones still waits in the queue (review 3's H): 3000, 100, 3000.
make_ev "$TMP_ROOT/h.raw" 3600 "2500:1:3000:1000;2501:1:100:1000;2502:1:3000:1000"
if out=$(python3 "$TOOL" report "$TMP_ROOT/h.raw" --read-mbps 300 $W); then fail "$out"; fi
grep -q 'worst 10.17x (2 s over the limits now -> 20.3 s until the queue empties)' <<<"$out" || fail "$out"
echo "[PASS] queue: the light second between two heavy ones is not counted as work"

# The smallest limit follows rare peaks inside a reconciliation, not only p99.9 (review 4 B2): 24 h
# of reconciliations at 400 MiB/s, 40 of them starting with one second at 2000 MiB/s. Rule 2 alone
# says 801; what is queued at 2000 drains while the reconciliation still reads 400, so it takes 1200.
make_ev "$TMP_ROOT/b2.raw" 86400 "R:600:100:12:400:1000;R:600:100:1:2000:1000:40"
out=$(python3 "$TOOL" report "$TMP_ROOT/b2.raw")
grep -Eq '^read_bps .* 800\.0MiB/s' <<<"$out" || fail "rule 2 alone: $out"
grep -q 'smallest read MiB/s limit meeting both rules (other read limit open): 1200$' <<<"$out" || fail "$out"
if python3 "$TOOL" report "$TMP_ROOT/b2.raw" --read-mbps 1199 $W >/dev/null; then fail "1199 must fail"; fi
python3 "$TOOL" report "$TMP_ROOT/b2.raw" --read-mbps 1200 $W >/dev/null || fail "1200 must pass"
echo "[PASS] report: the smallest limit meeting both rules is printed and follows rare peaks"

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
