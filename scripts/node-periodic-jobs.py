#!/usr/bin/env python3
"""List a node's periodic jobs and say which of them a measurement window never saw.

Why (testbed v4 ORAFIX, questions.md Q25 answer 1 ③): the E7 write limit of the bank Oracle disk
was set from a 24-hour measurement on a Wednesday and Thursday. The weekly fstrim of tb-w2 runs on
Monday mornings, so the measurement never saw it. On 11-16 its first discard of the new disk took
the whole limit for 336 seconds and stopped Oracle. A measurement that sets a limit or describes the
normal period of a node must be compared with the node's periodic jobs: a job that does not fire
inside the window is a load the measurement does not contain, and it is judged separately.

What it reads (read-only, nothing is written or started on the node):
  - systemd timers: every timer unit (systemctl list-units --all --type=timer), its OnCalendar
    and monotonic triggers, RandomizedDelaySec, AccuracySec, Persistent, last trigger and next run
    (systemctl show), the triggered unit and its ExecStart. Calendar firings inside the window come
    from systemd-analyze calendar --base-time (systemd 248 and later).
  - cron: /etc/crontab, /etc/cron.d/*, user crontabs (/var/spool/cron/crontabs/*), and the
    run-parts directories /etc/cron.{hourly,daily,weekly,monthly} with their scripts. Five-field
    schedules and @hourly, @daily, @weekly, @monthly, @yearly are matched minute by minute in the
    node's local time; @reboot is listed and never counted inside a window.
  - fstrim exposure: file systems in /etc/fstab that fstrim --listed-in /etc/fstab would trim (not
    X-fstrim.notrim, a device that accepts discard). Shown with the fstrim timer because a first trim
    of a large free area is one long write burst.

Run on the node as root (the user crontabs need it). Standard library only, so it can be streamed:
  ssh <node> "sudo -n python3 - --window-start 2026-11-11T08:37:00Z --window-end 2026-11-12T08:37:00Z" \\
      < node-periodic-jobs.py
  python3 node-periodic-jobs.py [--window-start ISO --window-end ISO] [--json]

A job is "outside" the window when none of its runs falls in [start, end]. For a calendar timer the
run may come up to RandomizedDelaySec + AccuracySec after the nominal time; a nominal time inside
the window counts as inside even if the delay could push it out, and the report shows the delay so
a reader can judge the edge. Monotonic timers (OnBootSec, OnUnitActiveSec) have no wall-clock
schedule: the report shows their interval and last trigger, and marks them "interval" instead of
guessing.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

Runner = Callable[[list], "subprocess.CompletedProcess[str]"]

CRON_FILES = (Path("/etc/crontab"),)
CRON_DIRS = (Path("/etc/cron.d"),)
USER_CRON_DIR = Path("/var/spool/cron/crontabs")
RUN_PARTS_DIRS = tuple(Path(f"/etc/cron.{name}") for name in ("hourly", "daily", "weekly", "monthly"))
FSTAB = Path("/etc/fstab")
TRIM_FS = {"ext4", "ext3", "ext2", "xfs", "btrfs", "f2fs", "vfat"}
MAX_ITERATIONS = 2000
MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
DAYS = {d: i for i, d in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))}
MACROS = {
    "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *", "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0", "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@hourly": "0 * * * *",
}
ENV_LINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*=")


def run(cmd: list) -> "subprocess.CompletedProcess[str]":
    env = {**os.environ, "TZ": "UTC", "LC_ALL": "C", "SYSTEMD_PAGER": "", "SYSTEMD_COLORS": "0"}
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)


def parse_iso(value: str) -> datetime:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError(f"{value}: give a time zone (Z or +09:00)")
    return moment.astimezone(timezone.utc)


def fmt(moment: datetime | None) -> str | None:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if moment else None


# ------------------------------------------------------------------- cron


def _field_values(text: str, low: int, high: int, names: dict[str, int] | None = None) -> set[int]:
    values: set[int] = set()
    for part in text.split(","):
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            step = int(step_text)
            if step < 1:
                raise ValueError(f"step {step_text}")
        if part == "*":
            start, end = low, high
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = _value(a, names), _value(b, names)
        else:
            start = _value(part, names)
            end = high if step > 1 else start
        if start < low or end > high or start > end:
            raise ValueError(f"{part} outside {low}-{high}")
        values.update(range(start, end + 1, step))
    return values


def _value(text: str, names: dict[str, int] | None) -> int:
    key = text.lower()
    if names and key in names:
        return names[key]
    return int(text)


class CronSchedule:
    """Five-field cron schedule with vixie-cron day semantics: when both day of month and day of
    week are restricted, either one matching is enough."""

    def __init__(self, spec: str) -> None:
        spec = MACROS.get(spec.strip().lower(), spec.strip())
        fields = spec.split()
        if len(fields) != 5:
            raise ValueError(f"not a five-field schedule: {spec!r}")
        self.spec = spec
        self.minutes = _field_values(fields[0], 0, 59)
        self.hours = _field_values(fields[1], 0, 23)
        self.dom = _field_values(fields[2], 1, 31)
        self.months = _field_values(fields[3], 1, 12, MONTHS)
        dow = _field_values(fields[4], 0, 7, DAYS)
        self.dow = {d % 7 for d in dow}
        self.dom_any = fields[2] == "*"
        self.dow_any = fields[4] == "*"

    def matches(self, local: datetime) -> bool:
        if local.minute not in self.minutes or local.hour not in self.hours or local.month not in self.months:
            return False
        dom_ok = local.day in self.dom
        dow_ok = ((local.weekday() + 1) % 7) in self.dow
        if self.dom_any or self.dow_any:
            return dom_ok and dow_ok
        return dom_ok or dow_ok

    def runs(self, start: datetime, end: datetime, tz: timezone | Any) -> list[datetime]:
        """Run times (UTC) in [start, end], read in the node's local zone tz."""
        out = []
        moment = start.astimezone(timezone.utc).replace(second=0, microsecond=0)
        if moment < start:
            moment += timedelta(minutes=1)
        while moment <= end:
            if self.matches(moment.astimezone(tz)):
                out.append(moment)
            moment += timedelta(minutes=1)
        return out


def parse_cron_text(text: str, source: str, *, has_user: bool) -> list[dict[str, Any]]:
    """Jobs of one crontab. System crontabs (/etc/crontab, /etc/cron.d) carry a user field."""
    jobs = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or ENV_LINE.match(line):
            continue
        if line.startswith("@"):
            head = line.split(None, 1)
            spec, rest = head[0], (head[1] if len(head) > 1 else "")
        else:
            parts = line.split(None, 5)
            if len(parts) < 6:
                jobs.append({"source": f"{source}:{number}", "schedule": line, "error": "unreadable line"})
                continue
            spec, rest = " ".join(parts[:5]), parts[5]
        user = None
        if has_user:
            head = rest.split(None, 1)
            user, rest = (head[0], head[1].strip()) if len(head) > 1 else ((head[0] if head else None), "")
        jobs.append({"source": f"{source}:{number}", "schedule": spec, "user": user, "command": rest})
    return jobs


def read_cron(reader: Callable[[Path], str | None], lister: Callable[[Path], list[Path]]) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for path in CRON_FILES:
        text = reader(path)
        if text is not None:
            jobs += parse_cron_text(text, str(path), has_user=True)
    for directory in CRON_DIRS:
        for path in lister(directory):
            # cron ignores names with dots (run-parts naming rule), as Debian's cron does.
            if "." in path.name or path.name.startswith("."):
                continue
            text = reader(path)
            if text is not None:
                jobs += parse_cron_text(text, str(path), has_user=True)
    for path in lister(USER_CRON_DIR):
        text = reader(path)
        if text is not None:
            for job in parse_cron_text(text, str(path), has_user=False):
                job["user"] = path.name
                jobs.append(job)
    return jobs


def run_parts(lister: Callable[[Path], list[Path]]) -> dict[str, list[str]]:
    return {str(d): sorted(p.name for p in lister(d) if not p.name.startswith(".") and p.name != "placeholder")
            for d in RUN_PARTS_DIRS}


# ---------------------------------------------------------------- systemd


SHOW_PROPS = ("Id", "Unit", "TimersCalendar", "TimersMonotonic", "RandomizedDelayUSec", "AccuracyUSec",
              "Persistent", "LastTriggerUSec", "NextElapseUSecRealtime", "UnitFileState", "ActiveState")
CAL_RE = re.compile(r"OnCalendar=([^;]+?)\s*;")
MONO_RE = re.compile(r"\{\s*(On[A-Za-z]+Sec)=([^;]+?)\s*;")
ELAPSE_RE = re.compile(r"(?:Next elapse|Iteration #\d+):\s+\w{3} (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC")
SPAN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(us|ms|s|min|h|d|w|month|y)?")
SPAN_UNIT = {"us": 1e-6, "ms": 1e-3, "s": 1, None: 1, "min": 60, "h": 3600, "d": 86400, "w": 604800,
             "month": 2629800, "y": 31557600}


def parse_span(text: str) -> float:
    """systemd time span ("1h 40min", "100ms", "0") in seconds."""
    total = 0.0
    for number, unit in SPAN_RE.findall(text or ""):
        total += float(number) * SPAN_UNIT[unit or None]
    return total


def parse_show(text: str) -> list[dict[str, str]]:
    """systemctl show output for several units: blocks separated by blank lines."""
    units, current = [], {}
    for line in text.splitlines():
        if not line.strip():
            if current:
                units.append(current)
                current = {}
            continue
        key, _, value = line.partition("=")
        # Repeated keys (several OnCalendar) arrive as separate TimersCalendar lines.
        current[key] = f"{current[key]}\n{value}" if key in current else value
    if current:
        units.append(current)
    return units


def parse_stamp(value: str | None) -> datetime | None:
    """A systemctl show timestamp. The command runs with TZ=UTC, so it reads
    "Mon 2026-11-16 01:20:14 UTC"; "@<seconds>" is accepted as well; empty or "n/a" is None."""
    value = (value or "").strip()
    if value.startswith("@"):
        try:
            seconds = int(value[1:])
        except ValueError:
            return None
        return datetime.fromtimestamp(seconds, timezone.utc) if seconds > 0 else None
    match = re.fullmatch(r"\w{3} (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC", value)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def calendar_runs(spec: str, start: datetime, end: datetime, runner: Runner) -> tuple[list[datetime], str | None]:
    """Nominal firings of an OnCalendar spec in [start, end] (systemd-analyze calendar)."""
    base = start - timedelta(seconds=1)
    cmd = ["systemd-analyze", "calendar", f"--iterations={MAX_ITERATIONS}",
           f"--base-time=@{int(base.timestamp())}", spec]
    result = runner(cmd)
    if result.returncode != 0:
        return [], (result.stderr or result.stdout or "systemd-analyze failed").strip()[:200]
    runs = []
    for stamp in ELAPSE_RE.findall(result.stdout):
        moment = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if moment > end:
            break
        if moment >= start:
            runs.append(moment)
    return runs, None


def read_timers(runner: Runner) -> tuple[list[dict[str, Any]], str | None]:
    listed = runner(["systemctl", "list-units", "--all", "--type=timer", "--plain", "--no-legend", "--no-pager"])
    if listed.returncode != 0:
        return [], (listed.stderr or "systemctl list-units failed").strip()[:200]
    names = [line.split()[0] for line in listed.stdout.splitlines() if line.split()]
    names = [n for n in names if n.endswith(".timer")]
    if not names:
        return [], None
    shown = runner(["systemctl", "show", *[f"-p{p}" for p in SHOW_PROPS], *names])
    if shown.returncode != 0:
        return [], (shown.stderr or "systemctl show failed").strip()[:200]
    timers = []
    for unit in parse_show(shown.stdout):
        target = unit.get("Unit", "")
        exec_start = None
        if target:
            service = runner(["systemctl", "show", "-pExecStart", "--value", target])
            if service.returncode == 0:
                match = re.search(r"argv\[\]=([^;]+?)\s*;", service.stdout)
                exec_start = match.group(1) if match else (service.stdout.strip()[:200] or None)
        timers.append({
            "unit": unit.get("Id"),
            "triggers": target,
            "exec": exec_start,
            "calendar": CAL_RE.findall(unit.get("TimersCalendar", "")),
            "monotonic": [f"{k}={v}" for k, v in MONO_RE.findall(unit.get("TimersMonotonic", ""))],
            "randomized_delay_sec": parse_span(unit.get("RandomizedDelayUSec", "")),
            "accuracy_sec": parse_span(unit.get("AccuracyUSec", "")),
            "persistent": unit.get("Persistent") == "yes",
            "last": fmt(parse_stamp(unit.get("LastTriggerUSec"))),
            "next": fmt(parse_stamp(unit.get("NextElapseUSecRealtime"))),
            "state": unit.get("ActiveState"),
        })
    return timers, None


# ------------------------------------------------------------------ fstrim


def fstrim_exposure(fstab_text: str | None, discard_of: Callable[[str], int | None]) -> list[dict[str, Any]]:
    """fstab file systems and whether fstrim --listed-in /etc/fstab trims them."""
    out = []
    for raw in (fstab_text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 4 or fields[2] not in TRIM_FS:
            continue
        source, target, fstype, options = fields[:4]
        opts = options.split(",")
        notrim = "X-fstrim.notrim" in opts
        granularity = discard_of(target)
        out.append({
            "source": source, "target": target, "fstype": fstype, "options": options,
            "notrim": notrim, "discard_granularity": granularity,
            "trimmed": (not notrim) and bool(granularity),
            "mount_discard": "discard" in opts,
        })
    return out


def _discard_granularity(target: str, runner: Runner) -> int | None:
    found = runner(["findmnt", "-n", "-o", "SOURCE", "--target", target])
    source = found.stdout.strip().splitlines()[0] if found.returncode == 0 and found.stdout.strip() else ""
    if not source.startswith("/dev/"):
        return None
    shown = runner(["lsblk", "-bdno", "DISC-GRAN", source])
    try:
        return int(shown.stdout.split()[0]) if shown.returncode == 0 and shown.stdout.split() else None
    except ValueError:
        return None


# ------------------------------------------------------------------ report


def classify(jobs: list[dict[str, Any]], start: datetime | None, end: datetime | None) -> None:
    for job in jobs:
        if start is None or end is None:
            job["window"] = None
        elif job.get("monotonic") and not job.get("calendar"):
            job["window"] = "interval"
        elif job.get("error"):
            job["window"] = "unknown"
        else:
            job["window"] = "inside" if job.get("runs_in_window") else "outside"


def collect(start: datetime | None, end: datetime | None, *, runner: Runner = run,
            reader: Callable[[Path], str | None] | None = None,
            lister: Callable[[Path], list[Path]] | None = None, tz: Any = None) -> dict[str, Any]:
    reader = reader or _read_file
    lister = lister or _list_dir
    tz = tz or datetime.now().astimezone().tzinfo
    timers, timer_error = read_timers(runner)
    for timer in timers:
        runs: list[datetime] = []
        for spec in timer["calendar"]:
            if start is None or end is None:
                break
            found, error = calendar_runs(spec, start, end, runner)
            if error:
                timer["error"] = error
            runs += found
        timer["runs_in_window"] = [fmt(r) for r in sorted(set(runs))]
    cron = read_cron(reader, lister)
    for job in cron:
        if "error" in job:
            continue
        try:
            schedule = None if job["schedule"].strip().lower() == "@reboot" else CronSchedule(job["schedule"])
        except ValueError as error:
            job["error"] = str(error)
            continue
        if schedule is None:
            job["runs_in_window"] = []
            job["reboot"] = True
        elif start is not None and end is not None:
            job["runs_in_window"] = [fmt(r) for r in schedule.runs(start, end, tz)]
    classify(timers, start, end)
    classify(cron, start, end)
    for job in cron:
        if job.get("reboot") and start is not None:
            job["window"] = "reboot"
    trims = fstrim_exposure(reader(FSTAB), lambda target: _discard_granularity(target, runner))
    return {
        "window": {"start": fmt(start), "end": fmt(end)},
        "local_zone": str(tz),
        "timers": timers,
        "timer_error": timer_error,
        "cron": cron,
        "run_parts": run_parts(lister),
        "fstrim": trims,
    }


def _read_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _list_dir(path: Path) -> list[Path]:
    try:
        return sorted(p for p in path.iterdir() if p.is_file())
    except OSError:
        return []


def render(report: dict[str, Any]) -> str:
    lines = []
    window = report["window"]
    if window["start"]:
        lines.append(f"# window {window['start']} ~ {window['end']} (UTC); cron read in {report['local_zone']}")
    else:
        lines.append(f"# no window given: listing only; cron read in {report['local_zone']}")
    if report.get("timer_error"):
        lines.append(f"# timers unreadable: {report['timer_error']}")
    lines.append("kind | job | schedule | delay | last | next | window | runs in window")
    for t in report["timers"]:
        schedule = "; ".join(t["calendar"] + t["monotonic"]) or "-"
        delay = f"+{int(t['randomized_delay_sec'] + t['accuracy_sec'])}s"
        runs = ", ".join(t.get("runs_in_window") or []) or "-"
        lines.append(f"timer | {t['unit']} -> {t['triggers']} ({t['exec'] or '-'}) | {schedule} | {delay} | "
                     f"{t['last'] or '-'} | {t['next'] or '-'} | {t['window'] or '-'} | {runs}")
    for c in report["cron"]:
        runs = c.get("runs_in_window") or []
        shown = f"{len(runs)} ({runs[0]} .. {runs[-1]})" if runs else "-"
        what = c.get("command") or c.get("error") or "-"
        lines.append(f"cron | {c['source']} {c.get('user') or ''} {what} | {c['schedule']} | - | - | - | "
                     f"{c.get('window') or '-'} | {shown}")
    for directory, names in report["run_parts"].items():
        if names:
            lines.append(f"run-parts | {directory} | {', '.join(names)} | - | - | - | see /etc/crontab or anacron | -")
    for f in report["fstrim"]:
        state = "excluded (X-fstrim.notrim)" if f["notrim"] else (
            "TRIMMED by fstrim" if f["trimmed"] else "not trimmed (device gives no discard)")
        lines.append(f"fstrim | {f['target']} ({f['source']}, {f['fstype']}) | {state} | "
                     f"discard granularity {f['discard_granularity']} | - | - | - | -")
    outside = [t["unit"] for t in report["timers"] if t["window"] == "outside"]
    outside += [f"{c['source']}" for c in report["cron"] if c.get("window") == "outside"]
    if window["start"]:
        lines.append(f"# outside the window ({len(outside)}): {', '.join(outside) or 'none'}")
    return "\n".join(lines)


def main(argv: Iterable[str]) -> int:
    parser = argparse.ArgumentParser(prog="node-periodic-jobs", description=__doc__.split("\n")[0])
    parser.add_argument("--window-start", default=None, help="measurement window start, ISO with zone")
    parser.add_argument("--window-end", default=None, help="measurement window end, ISO with zone")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(list(argv))
    if (args.window_start is None) != (args.window_end is None):
        parser.error("give both --window-start and --window-end, or neither")
    start = parse_iso(args.window_start) if args.window_start else None
    end = parse_iso(args.window_end) if args.window_end else None
    if start is not None and end is not None and end < start:
        parser.error("--window-end is before --window-start")
    report = collect(start, end)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
