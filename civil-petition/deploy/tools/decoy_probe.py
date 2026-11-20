#!/usr/bin/env python3
"""U7 decoy harm check for runbook §15-4 차례 9 (설계 §6-2 운영자 경로, T4U 리뷰 rv-t4u R3): samples the load
generator's live document during a decoy window and judges it against fixed numbers, so the judgement rests on a
record that stays (a JSON lines file) and counts the 404s a Tomcat redeploy gives (check_failed_rate).

  decoy_probe.py sample --inventory inventory/t.json --seconds 900 --out decoy-was01.jsonl [--interval 10]
  decoy_probe.py judge decoy-was01.jsonl [--from 2026-11-21T03:00:00+09:00 --to 2026-11-21T03:15:00+09:00]

sample runs on 104: one ssh session to the load generator by the install path (operator account, through 66,
`-J root@192.168.200.66`, the same as apply.sh), which prints the live document every --interval seconds. The load
generator does not forward syslog to 119 (설계 §2-3), so the session leaves nothing in the recording. Each line of the
output is {"sampled_at": <UTC>, "doc": <live document>}.

judge reads such a file. The live document is a 30-second window republished every second, so samples every 10 seconds
cover the whole span. The decoy is harmless when, in every sample inside the span:
  check_failed_rate   <= 0.01   (k6 checks: 404 and any other wrong answer, not only 5xx)
  http_abnormal_rate  <= 0.01   (5xx and no response; 설계 §6-3 평시 상한 0.5% 의 2배)
  http_p95_ms         <= 800    (all requests, the 30-second window; 설계 §6-3 진입 p95 평시 피크)
and the samples are enough and fresh: at least 80% of span / interval samples, each document observed within 60 s of
its sampling. Exit 0 harmless, 1 harmful (the lines over the numbers are printed), 2 not enough to judge.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

LIVE_DOC = "/tmp/k6-{domain}-live.json"
LIMITS = {"check_failed_rate": 0.01, "http_abnormal_rate": 0.01, "http_p95_ms": 800.0}
FRESH_SECONDS = 60
COVERAGE = 0.8
JUMP = "root@192.168.200.66"


def parse_time(value: str) -> dt.datetime:
    moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError(f"time without a zone: {value}")
    return moment


def loadgen(inventory: dict) -> tuple[str, str, str]:
    """(operator account, load generator management address, domain)."""
    mgmt = next(name for name, net in inventory["networks"].items() if net.get("management"))
    host = next(h for h in inventory["hosts"] if h["role"] == "loadgen")
    addr = next(n["addr"] for n in host["nics"] if n["net"] == mgmt)
    return inventory["accounts"]["operator"]["name"], addr, inventory["domain"]


def sample_argv(inventory: dict, seconds: int, interval: int) -> list[str]:
    operator, addr, domain = loadgen(inventory)
    count = max(seconds // interval, 1)
    remote = (f"for i in $(seq {count}); do printf '%s\\t' \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"; "
              f"tr -d '\\n' < {LIVE_DOC.format(domain=domain)} 2>/dev/null || printf '{{}}'; echo; sleep {interval}; done")
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-J", JUMP, f"{operator}@{addr}", remote]


def sample(inventory: dict, seconds: int, interval: int, out: Path) -> int:
    proc = subprocess.Popen(sample_argv(inventory, seconds, interval), stdout=subprocess.PIPE, text=True)
    assert proc.stdout
    with out.open("a") as sink:
        for line in proc.stdout:
            stamp, _, body = line.rstrip("\n").partition("\t")
            try:
                doc = json.loads(body) if body else {}
            except json.JSONDecodeError:
                doc = {}
            sink.write(json.dumps({"sampled_at": stamp, "doc": doc}, ensure_ascii=False) + "\n")
            sink.flush()
    return proc.wait()


def judge(lines: list[str], start: dt.datetime | None, end: dt.datetime | None, interval: int) -> tuple[int, list[str]]:
    samples = []
    for line in lines:
        if not line.strip():
            continue
        record = json.loads(line)
        at = parse_time(record["sampled_at"])
        if (start and at < start) or (end and at > end):
            continue
        samples.append((at, record.get("doc") or {}))
    report: list[str] = []
    if not samples:
        return 2, ["no sample in the span"]
    first, last = start or samples[0][0], end or samples[-1][0]
    wanted = max(int((last - first).total_seconds() // interval), 1)
    fresh = []
    for at, doc in samples:
        observed = doc.get("observed_at")
        if observed and abs((at - parse_time(observed)).total_seconds()) <= FRESH_SECONDS:
            fresh.append((at, doc))
    if len(fresh) < COVERAGE * wanted:
        return 2, [f"{len(fresh)} fresh samples, {wanted} expected (>= {COVERAGE:.0%})"]
    worst = {key: 0.0 for key in LIMITS}
    for at, doc in fresh:
        for key, limit in LIMITS.items():
            value = doc.get(key)
            if value is None:
                # The window had no request of that kind: not a harm, but say so.
                continue
            worst[key] = max(worst[key], float(value))
            if float(value) > limit:
                report.append(f"{at.isoformat()} {key} {value} > {limit}")
    summary = ", ".join(f"{key} max {worst[key]:.4g} (<= {LIMITS[key]:g})" for key in LIMITS)
    return (1 if report else 0), [f"{len(fresh)} samples: {summary}"] + report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--inventory", required=True, type=Path)
    s.add_argument("--seconds", required=True, type=int)
    s.add_argument("--interval", type=int, default=10)
    s.add_argument("--out", required=True, type=Path)
    j = sub.add_parser("judge")
    j.add_argument("file", type=Path)
    j.add_argument("--from", dest="start")
    j.add_argument("--to", dest="end")
    j.add_argument("--interval", type=int, default=10)
    args = parser.parse_args(argv)
    if args.mode == "sample":
        if not 30 <= args.seconds <= 7200 or not 2 <= args.interval <= 30:
            parser.error("--seconds 30 ~ 7200, --interval 2 ~ 30")
        return sample(json.loads(args.inventory.read_text()), args.seconds, args.interval, args.out)
    start = parse_time(args.start) if args.start else None
    end = parse_time(args.end) if args.end else None
    code, lines = judge(args.file.read_text().splitlines(), start, end, args.interval)
    print("\n".join(lines))
    print({0: "harmless", 1: "harmful", 2: "not enough to judge"}[code])
    return code


if __name__ == "__main__":
    raise SystemExit(main())
