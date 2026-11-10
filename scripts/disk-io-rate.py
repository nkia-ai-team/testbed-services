#!/usr/bin/env python3
"""Per-second I/O of one virtual disk, read from the hypervisor (testbed v4 E7).

Why: the limit for the banking Oracle disk (Proxmox throttle) must leave the normal workload alone.
The one-minute averages that SMS and KCM keep hide what matters: the ledger reconciliation reads the
whole ledger once every ten minutes in a few seconds. So the limit is chosen from one-second rates.

Where it reads: on the Proxmox host, the block statistics of the disk's logical volume
(/sys/block/dm-N/stat). That is the disk's own traffic after QEMU, read-only, with nothing started in
the guest and no QMP connection (the VM's QMP socket serves one client at a time and Proxmox itself
polls it). One ssh session runs the sampling loop on the host; this side only stores the lines.

  disk-io-rate.py collect --host root@192.168.200.66 --volume vmdata/vm-103-disk-1 \\
      --seconds 86400 --out sdb-24h.raw
  disk-io-rate.py report sdb-24h.raw --read-mbps 160 --write-mbps 40 --read-iops 1600 --write-iops 800

report prints, for read and write bytes and operations per second: p50, p99, p99.9 and max, the
smallest limit that keeps p99.9 below half of it, and the read bursts (consecutive seconds above
--burst-mbps): how long they take now and how long the same bytes would take at the read limit.
With limits given it says PASS only when every p99.9 is below half its limit and the median burst
keeps its order of magnitude (same number of digits in seconds).
"""
from __future__ import annotations

import argparse
import math
import re
import shlex
import subprocess
import sys
from pathlib import Path

SECTOR = 512
VOLUME = re.compile(r"^[A-Za-z0-9+_.-]+/[A-Za-z0-9+_.-]+$")

REMOTE_LOOP = r"""
set -eu
dev=$(readlink -f /dev/{volume})
stat=/sys/block/${{dev##*/}}/stat
[ -r "$stat" ] || {{ echo "no block statistics for {volume}" >&2; exit 3; }}
end=$((SECONDS + {seconds}))
while [ "$SECONDS" -lt "$end" ]; do
  read -r line <"$stat"
  printf '%s %s\n' "$EPOCHREALTIME" "$line"
  sleep 1
done
"""


def collect(args: argparse.Namespace) -> int:
    if not VOLUME.match(args.volume):
        print(f"volume must be <vg>/<lv>: {args.volume}", file=sys.stderr)
        return 2
    script = REMOTE_LOOP.format(volume=args.volume, seconds=int(args.seconds))
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30", args.host, "bash -c " + shlex.quote(script)]
    with open(args.out, "a", encoding="utf-8") as out:
        proc = subprocess.run(command, stdout=out, check=False)
    return proc.returncode


def samples(path: Path) -> list[tuple[float, int, int, int, int]]:
    """(time, read ios, read sectors, write ios, write sectors) per line, in time order."""
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) < 8:
            continue
        rows.append((float(fields[0]), int(fields[1]), int(fields[3]), int(fields[5]), int(fields[7])))
    rows.sort()
    return rows


def rates(rows: list[tuple[float, int, int, int, int]]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {"read_bps": [], "write_bps": [], "read_iops": [], "write_iops": []}
    for a, b in zip(rows, rows[1:]):
        dt = b[0] - a[0]
        # A gap (ssh reconnect, a restart of collect) or a counter reset (device re-created) is not a rate.
        if dt <= 0 or dt > 5 or any(b[i] < a[i] for i in range(1, 5)):
            continue
        out["read_iops"].append((b[1] - a[1]) / dt)
        out["read_bps"].append((b[2] - a[2]) * SECTOR / dt)
        out["write_iops"].append((b[3] - a[3]) / dt)
        out["write_bps"].append((b[4] - a[4]) * SECTOR / dt)
    return out


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return ordered[index]


def bursts(read_bps: list[float], threshold: float) -> list[tuple[int, float]]:
    """(seconds, bytes) of each run of consecutive seconds above threshold."""
    runs, length, total = [], 0, 0.0
    for value in read_bps + [0.0]:
        if value > threshold:
            length += 1
            total += value
        elif length:
            runs.append((length, total))
            length, total = 0, 0.0
    return runs


def digits(seconds: float) -> int:
    return len(str(max(1, int(math.ceil(seconds)))))


def report(args: argparse.Namespace) -> int:
    series = rates(samples(Path(args.raw)))
    count = len(series["read_bps"])
    if count == 0:
        print("no usable one-second samples")
        return 2
    limits = {
        "read_bps": args.read_mbps * 1e6 if args.read_mbps else None,
        "write_bps": args.write_mbps * 1e6 if args.write_mbps else None,
        "read_iops": args.read_iops,
        "write_iops": args.write_iops,
    }
    print(f"one-second samples: {count} ({count / 3600:.1f} h)")
    print("series      p50        p99        p99.9      max        smallest limit (p99.9 < limit/2)  limit given  ok")
    ok = True
    for name, values in series.items():
        unit = 1e6 if name.endswith("bps") else 1
        p999 = percentile(values, 0.999)
        row = [percentile(values, 0.5), percentile(values, 0.99), p999, max(values)]
        given = limits[name]
        verdict = ""
        if given:
            passed = p999 < given / 2
            ok &= passed
            verdict = "yes" if passed else "NO"
        shown = " ".join(f"{v / unit:10.1f}" for v in row)
        print(f"{name:10s} {shown}  {2 * p999 / unit:10.1f}{'MB/s' if unit > 1 else ' IOPS':5s}               "
              f"{'' if not given else f'{given / unit:.0f}':>11s}  {verdict}")
    runs = bursts(series["read_bps"], args.burst_mbps * 1e6)
    if runs:
        lengths = sorted(r[0] for r in runs)
        sizes = sorted(r[1] for r in runs)
        median_len, median_bytes = lengths[len(lengths) // 2], sizes[len(sizes) // 2]
        print(f"read bursts above {args.burst_mbps} MB/s: {len(runs)}, median {median_len} s and "
              f"{median_bytes / 1e9:.2f} GB, longest {lengths[-1]} s")
        if limits["read_bps"]:
            at_limit = median_bytes / limits["read_bps"]
            same = digits(at_limit) == digits(median_len)
            ok &= same
            print(f"median burst at the read limit: {at_limit:.1f} s (now {median_len} s), "
                  f"same order of magnitude: {'yes' if same else 'NO'}")
    else:
        print(f"read bursts above {args.burst_mbps} MB/s: none")
    if any(limits.values()):
        print("PASS" if ok else "FAIL")
        return 0 if ok else 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--host", required=True)
    c.add_argument("--volume", required=True, help="<vg>/<lv> of the disk on the Proxmox host")
    c.add_argument("--seconds", type=int, default=86400)
    c.add_argument("--out", required=True, help="raw lines are appended")
    r = sub.add_parser("report")
    r.add_argument("raw")
    r.add_argument("--read-mbps", type=float)
    r.add_argument("--write-mbps", type=float)
    r.add_argument("--read-iops", type=float)
    r.add_argument("--write-iops", type=float)
    r.add_argument("--burst-mbps", type=float, default=20.0)
    args = parser.parse_args(argv)
    return collect(args) if args.command == "collect" else report(args)


if __name__ == "__main__":
    sys.exit(main())
