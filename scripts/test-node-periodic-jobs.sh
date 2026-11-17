#!/usr/bin/env bash
# Tests for node-periodic-jobs.py: the cron matcher, the systemd readers against recorded command
# output, the fstrim exposure, and the window verdicts. No command runs on this host.
set -Eeuo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
python3 - "$SCRIPT_DIR/node-periodic-jobs.py" <<'PY'
import importlib.util, subprocess, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

spec = importlib.util.spec_from_file_location("npj", sys.argv[1])
npj = importlib.util.module_from_spec(spec)
spec.loader.exec_module(npj)
UTC = timezone.utc
KST = timezone(timedelta(hours=9))


def fail(message):
    print(f"[FAIL] {message}", file=sys.stderr)
    sys.exit(1)


def check(condition, message):
    if not condition:
        fail(message)


# The E7 measurement window (Wed 11-11 17:42 KST to Thu 11-12 17:42 KST).
start = datetime(2026, 11, 11, 8, 42, 26, tzinfo=UTC)
end = datetime(2026, 11, 12, 8, 42, 25, tzinfo=UTC)

# --- cron schedules, matched minute by minute in the node's zone ---
runs = lambda spec, tz=UTC: npj.CronSchedule(spec).runs(start, end, tz)
check(len(runs("23 * * * *")) == 24, "hourly")
check([r.strftime("%H:%M") for r in runs("7 */6 * * *")] == ["12:07", "18:07", "00:07", "06:07"], "every 6 hours")
check(runs("30 3 * * 0") == [] and runs("47 6 * * 7") == [], "Sunday (0 and 7) is outside a Wed-Thu window")
check(len(runs("5-55/10 * * * *")) == 144, "range with step")
check(len(runs("0 9 * * mon-fri")) == 1, "day names and ranges")
check(runs("@weekly") == [] and len(runs("@daily")) == 1 and len(runs("@hourly")) == 24, "macros")
# Both day fields restricted: either matching is enough (vixie cron). Day 12 is a Thursday.
check(len(runs("24 0 12 * 1")) == 1 and len(runs("24 0 8-14 * *")) == 1, "day of month or day of week")
# Local zone: 00:24 KST on 11-12 is 15:24 UTC on 11-11.
check([r.isoformat() for r in runs("24 0 12 * *", KST)] == ["2026-11-11T15:24:00+00:00"], "local time zone")
for bad in ("61 * * * *", "* * *", "*/0 * * * *", "5-1 * * * *"):
    try:
        npj.CronSchedule(bad)
    except ValueError:
        continue
    fail(f"accepted {bad!r}")
print("[PASS] cron schedules match minute by minute in the node's zone")

# --- crontab text: user field after a tab, environment lines, @reboot ---
jobs = npj.parse_cron_text("SHELL=/bin/sh\n# c\n17 *\t* * *\troot\tcd / && run-parts --report /etc/cron.hourly\n"
                           "@reboot root /usr/bin/true\nbroken line\n", "/etc/crontab", has_user=True)
check(jobs[0]["schedule"] == "17 * * * *" and jobs[0]["user"] == "root"
      and jobs[0]["command"] == "cd / && run-parts --report /etc/cron.hourly", f"tab separated: {jobs[0]}")
check(jobs[1]["schedule"] == "@reboot" and jobs[1]["user"] == "root" and jobs[1]["command"] == "/usr/bin/true", jobs[1])
check(jobs[2].get("error") == "unreadable line", jobs[2])
user = npj.parse_cron_text("23 * * * * /opt/testbed/oracle-maintenance.sh purge\n", "/var/spool/cron/crontabs/root",
                           has_user=False)
check(user[0]["user"] is None and user[0]["command"].startswith("/opt/testbed/oracle"), user)
print("[PASS] crontab lines are read with and without a user field")

# --- systemd: recorded outputs of tb-w2 (11-17) ---
LIST = "fstrim.timer loaded active waiting Discard unused filesystem blocks once a week\n" \
       "sysstat-collect.timer loaded active waiting Run system activity accounting tool every 10 minutes\n" \
       "systemd-tmpfiles-clean.timer loaded active waiting Daily Cleanup of Temporary Directories\n"
SHOW = ("Id=fstrim.timer\nUnit=fstrim.service\n"
        "TimersCalendar={ OnCalendar=Mon *-*-* 00:00:00 ; next_elapse=Mon 2026-11-23 00:00:00 UTC }\n"
        "RandomizedDelayUSec=1h 40min\nAccuracyUSec=1h\nPersistent=yes\n"
        "LastTriggerUSec=Mon 2026-11-16 01:20:14 UTC\nNextElapseUSecRealtime=Mon 2026-11-23 00:23:56 UTC\n"
        "ActiveState=active\n\n"
        "Id=sysstat-collect.timer\nUnit=sysstat-collect.service\n"
        "TimersCalendar={ OnCalendar=*-*-* *:00/10:00 ; next_elapse=n/a }\nRandomizedDelayUSec=0\nAccuracyUSec=1min\n"
        "Persistent=no\nLastTriggerUSec=n/a\nNextElapseUSecRealtime=\nActiveState=active\n\n"
        "Id=systemd-tmpfiles-clean.timer\nUnit=systemd-tmpfiles-clean.service\n"
        "TimersMonotonic={ OnUnitActiveUSec=1d ; next_elapse=1d 15min }\n"
        "TimersMonotonic={ OnBootUSec=15min ; next_elapse=15min }\nRandomizedDelayUSec=0\nAccuracyUSec=1min\n"
        "Persistent=no\nActiveState=active\n")


def calendar_output(spec, base):
    # What systemd-analyze calendar prints, for the two specs above.
    base = datetime.fromtimestamp(base, UTC)
    step = timedelta(weeks=1) if spec.startswith("Mon") else timedelta(minutes=10)
    first = (base + timedelta(days=(7 - base.weekday()) % 7 or 7)).replace(hour=0, minute=0, second=0) \
        if spec.startswith("Mon") else (base.replace(second=0, microsecond=0) + timedelta(minutes=10 - base.minute % 10))
    lines = [f"  Original form: {spec}", f"    Next elapse: {first:%a %Y-%m-%d %H:%M:%S} UTC", "       From now: x"]
    for i in range(2, 200):
        lines += [f"   Iteration #{i}: {first + (i - 1) * step:%a %Y-%m-%d %H:%M:%S} UTC", "       From now: x"]
    return "\n".join(lines) + "\n"


calls = []


def runner(cmd):
    calls.append(cmd)
    ok = lambda out: subprocess.CompletedProcess(cmd, 0, out, "")
    if cmd[:2] == ["systemctl", "list-units"]:
        return ok(LIST)
    if cmd[:2] == ["systemctl", "show"] and "-pExecStart" in cmd:
        exe = {"fstrim.service": "/sbin/fstrim --listed-in /etc/fstab:/proc/self/mountinfo"}.get(cmd[-1], "/usr/bin/x")
        return ok(f"{{ path={exe.split()[0]} ; argv[]={exe} ; ignore_errors=no }}\n")
    if cmd[:2] == ["systemctl", "show"]:
        return ok(SHOW)
    if cmd[:2] == ["systemd-analyze", "calendar"]:
        check(cmd[2] == f"--iterations={npj.MAX_ITERATIONS}", cmd)
        return ok(calendar_output(cmd[-1], int(cmd[3].split("@")[1])))
    if cmd[0] == "findmnt":
        return ok({"/": "/dev/sda1\n", "/data": "/dev/sdb\n"}.get(cmd[-1], "tmpfs\n"))
    if cmd[0] == "lsblk":
        return ok("4096\n" if cmd[-1] in ("/dev/sda1", "/dev/sdb") else "0\n")
    return subprocess.CompletedProcess(cmd, 1, "", "unexpected")


files = {
    "/etc/crontab": "17 *\t* * *\troot\tcd / && run-parts --report /etc/cron.hourly\n47 6\t* * 7\troot\ttest -x /usr/sbin/anacron\n",
    "/etc/cron.d/sysstat": "5-55/10 * * * * root command -v debian-sa1\n",
    "/etc/cron.d/skip.dpkg-old": "* * * * * root never\n",
    "/var/spool/cron/crontabs/root": "@reboot /usr/bin/true\n",
    "/etc/fstab": "LABEL=cloudimg-rootfs / ext4 discard,commit=30 0 1\n"
                  "UUID=a /data ext4 defaults,nofail 0 2\nUUID=b /kept ext4 defaults,nofail,X-fstrim.notrim 0 2\n"
                  "tmpfs /run/x tmpfs defaults 0 0\nUUID=c /nodiscard xfs defaults 0 0\n",
}
reader = lambda path: files.get(str(path))
lister = lambda directory: sorted(Path(p) for p in files if str(Path(p).parent) == str(directory)) + \
    ([Path("/etc/cron.weekly/man-db")] if str(directory) == "/etc/cron.weekly" else [])
report = npj.collect(start, end, runner=runner, reader=reader, lister=lister, tz=UTC)
timers = {t["unit"]: t for t in report["timers"]}
fstrim = timers["fstrim.timer"]
check(fstrim["window"] == "outside" and fstrim["runs_in_window"] == [], f"weekly fstrim: {fstrim}")
check(fstrim["last"] == "2026-11-16T01:20:14Z" and fstrim["next"] == "2026-11-23T00:23:56Z", fstrim)
check(fstrim["randomized_delay_sec"] == 6000 and fstrim["accuracy_sec"] == 3600, fstrim)
check(fstrim["exec"] == "/sbin/fstrim --listed-in /etc/fstab:/proc/self/mountinfo", fstrim)
check(timers["sysstat-collect.timer"]["window"] == "inside" and len(timers["sysstat-collect.timer"]["runs_in_window"]) == 144,
      timers["sysstat-collect.timer"])
check(timers["sysstat-collect.timer"]["last"] is None, "n/a is no time")
tmp = timers["systemd-tmpfiles-clean.timer"]
check(tmp["window"] == "interval" and tmp["monotonic"] == ["OnUnitActiveUSec=1d", "OnBootUSec=15min"], tmp)
check(not any(c[:2] == ["systemd-analyze", "calendar"] and "OnUnitActive" in c[-1] for c in calls), "no calendar for monotonic")
cron = {c["source"]: c for c in report["cron"]}
check(cron["/etc/crontab:1"]["window"] == "inside" and cron["/etc/crontab:2"]["window"] == "outside", cron)
check(cron["/etc/cron.d/sysstat:1"]["window"] == "inside", cron)
check(not any(s.startswith("/etc/cron.d/skip.dpkg-old") for s in cron), "cron ignores names with dots")
check(cron["/var/spool/cron/crontabs/root:1"]["window"] == "reboot" and cron["/var/spool/cron/crontabs/root:1"]["user"] == "root", cron)
check(report["run_parts"]["/etc/cron.weekly"] == ["man-db"], report["run_parts"])
trims = {f["target"]: f for f in report["fstrim"]}
check(trims["/data"]["trimmed"] and not trims["/data"]["notrim"], trims["/data"])
check(trims["/kept"]["notrim"] and not trims["/kept"]["trimmed"], trims["/kept"])
check(not trims["/nodiscard"]["trimmed"] and "/run/x" not in trims, trims)
text = npj.render(report)
check("# outside the window or only at its edge (2): fstrim.timer, /etc/crontab:2" in text, text)
check("fstrim | /data (UUID=a, ext4) | TRIMMED by fstrim" in text and "excluded (X-fstrim.notrim)" in text, text)
listing = npj.render(npj.collect(None, None, runner=runner, reader=reader, lister=lister, tz=UTC))
check("# no window given" in listing and "# outside the window" not in listing, listing)
# Edge (review rv-orafix L6): a weekly run named 30 minutes before the window with a delay of 2h40
# may start inside it, so it is neither inside nor plainly outside; the same run inside with the whole
# delay inside the window is inside; at the window end, a run that could start after it is edge.
monday = datetime(2026, 11, 16, 0, 0, tzinfo=UTC)
edge = npj.collect(monday + timedelta(minutes=30), monday + timedelta(days=1), runner=runner, reader=reader,
                   lister=lister, tz=UTC)
fs = {t["unit"]: t for t in edge["timers"]}["fstrim.timer"]
check(fs["window"] == "edge" and fs["runs_at_edge"] == ["2026-11-16T00:00:00Z"] and fs["runs_in_window"] == [], fs)
check("(edge: 2026-11-16T00:00:00Z)" in npj.render(edge), npj.render(edge))
inside = npj.collect(monday - timedelta(minutes=1), monday + timedelta(days=1), runner=runner, reader=reader,
                     lister=lister, tz=UTC)
check({t["unit"]: t for t in inside["timers"]}["fstrim.timer"]["window"] == "inside", "nominal in, delay in")
late = npj.collect(monday - timedelta(minutes=1), monday + timedelta(hours=1), runner=runner, reader=reader,
                   lister=lister, tz=UTC)
check({t["unit"]: t for t in late["timers"]}["fstrim.timer"]["window"] == "edge", "delay may cross the end")
print("[PASS] timers and cron jobs outside the window are named, monotonic timers are not guessed")
print("[PASS] fstrim exposure follows fstab options and the device's discard support")

# --- spans and stamps ---
check(npj.parse_span("1h 40min") == 6000 and npj.parse_span("0") == 0 and npj.parse_span("1w 1min") == 604860, "spans")
check(npj.parse_stamp("@1794900000") == datetime.fromtimestamp(1794900000, UTC) and npj.parse_stamp("n/a") is None
      and npj.parse_stamp("") is None and npj.parse_stamp("@0") is None, "stamps")
print("[PASS] node periodic jobs tests")
PY
