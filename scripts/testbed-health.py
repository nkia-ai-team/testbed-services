#!/usr/bin/env python3
"""Testbed health in one line, every 10 minutes, recording or not (testbed v4 ORAFIX, Q25 answer 2).

Why: 11-16 10:20 KST the bank Oracle stopped and stayed down 30 hours. The observation server made an
incident at 13:01 and the Oracle housekeeping log filled with failures, but nothing that a person or
the command session reads every turn said so. This check is that line.

What it reads: one Kubernetes API list of the pods (kubectl get pods -A -o json with the tb-ops
kubeconfig) and the runner's /api/active on tb-ops itself. It never logs in to a testbed node or the
observation server, so it leaves no ssh line in any node's syslog. The cluster's audit policy records
no get, list or watch (tb-cp /etc/kubernetes/audit/policy.yaml: level None for those verbs), and tb-ops
does not forward its syslog to the observation server (scripts/setup-syslog-forwarding.sh), so the timer
and its journal lines stay on tb-ops. It runs on a fixed 10-minute timer from normal operation on, so
whatever it costs is part of every baseline and never lines up with an injection.

What it says, for the pods of the testbed namespaces (name prefix, default rca-testbed-):
  - not Ready: pods that are not Succeeded and whose Ready condition is not True, with the waiting
    reason of a container (CrashLoopBackOff, ImagePullBackOff, ...) when there is one;
  - restarts: the increase of container restart counts since the previous run, per pod (by uid);
  - runner: idle or active <scenario> (a scenario may take pods down on purpose while active).

Files ($STATE_DIR, default /var/lib/lucida/testbed-health):
  status.line   the one line, replaced each run (the command session's board reads this)
  last.json     the same in full, and the restart counts the next run compares with
  history.log   every line, the newest 4320 kept (30 days at 10 minutes)

One line: "<KST time> testbed-health <OK|ALERT|UNKNOWN> pods <n> notready <n> restarts+<n> runner <state>"
followed, for ALERT, by " | notready <ns>/<pod>(<reason>) ..." and " | restarts <ns>/<pod>+<n> ...", and
for UNKNOWN by " | <why>". Exit 0 for OK, 3 for ALERT, 2 for UNKNOWN. The first run has no previous counts
and reports restarts+0 with "(first run)".

Install on tb-ops: scripts/README.md "테스트베드 건강 점검".
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

KST = timezone(timedelta(hours=9))
HISTORY_KEEP = 4320
OK, ALERT, UNKNOWN = 0, 3, 2


def pod_rows(document: dict[str, Any], prefix: str) -> list[dict[str, Any]]:
    rows = []
    for item in document.get("items") or []:
        meta = item.get("metadata") or {}
        namespace = meta.get("namespace") or ""
        if not namespace.startswith(prefix):
            continue
        status = item.get("status") or {}
        phase = status.get("phase")
        if phase == "Succeeded":
            continue
        ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions") or [])
        containers = (status.get("initContainerStatuses") or []) + (status.get("containerStatuses") or [])
        restarts = sum(int(c.get("restartCount") or 0) for c in containers)
        reasons = sorted({((c.get("state") or {}).get("waiting") or {}).get("reason") for c in containers} - {None})
        if not reasons and not ready:
            reasons = [phase or "NotReady"]
        rows.append({
            "pod": f"{namespace}/{meta.get('name')}",
            "uid": meta.get("uid"),
            "ready": ready,
            "restarts": restarts,
            "reason": ",".join(reasons) if reasons else None,
        })
    return sorted(rows, key=lambda r: r["pod"])


def restart_increases(rows: list[dict[str, Any]], previous: dict[str, int] | None) -> list[dict[str, Any]]:
    """Per pod increase of the restart count since the previous run. A pod the previous run did not see
    (new uid) counts from 0. No previous run: nothing to compare with, no increase."""
    if previous is None:
        return []
    out = []
    for row in rows:
        before = previous.get(row["uid"], 0)
        if row["restarts"] > before:
            out.append({"pod": row["pod"], "increase": row["restarts"] - before})
    return out


def runner_state(fetch: Callable[[str], bytes], url: str) -> str:
    try:
        body = json.loads(fetch(url.rstrip("/") + "/api/active"))
    except Exception:
        return "unknown"
    if body.get("is_active"):
        return f"active {body.get('scenario_id') or '?'}"
    return "idle"


def line_of(now: datetime, verdict: str, rows: list[dict[str, Any]], increases: list[dict[str, Any]] | None,
            runner: str, *, first: bool = False, why: str | None = None) -> str:
    stamp = now.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")
    if verdict == "UNKNOWN":
        return f"{stamp} testbed-health UNKNOWN runner {runner} | {why}"
    notready = [r for r in rows if not r["ready"]]
    total = sum(i["increase"] for i in increases or [])
    text = (f"{stamp} testbed-health {verdict} pods {len(rows)} notready {len(notready)} "
            f"restarts+{total}{' (first run)' if first else ''} runner {runner}")
    if notready:
        text += " | notready " + " ".join(f"{r['pod']}({r['reason']})" for r in notready)
    if increases:
        text += " | restarts " + " ".join(f"{i['pod']}+{i['increase']}" for i in increases)
    return text


def run_kubectl(kubeconfig: str) -> dict[str, Any]:
    result = subprocess.run(
        ["kubectl", "--kubeconfig", kubeconfig, "--request-timeout=20s", "get", "pods", "-A", "-o", "json"],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        tail = [line for line in (result.stderr or result.stdout or "").splitlines() if line.strip()]
        raise RuntimeError(f"kube API: {tail[-1][:200] if tail else 'kubectl rc ' + str(result.returncode)}")
    return json.loads(result.stdout)


def http_get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - fixed local runner URL
        return response.read()


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def check(state_dir: Path, *, now: datetime, get_pods: Callable[[], dict[str, Any]], fetch: Callable[[str], bytes],
          runner_url: str, prefix: str) -> tuple[int, str]:
    state_dir.mkdir(parents=True, exist_ok=True)
    last_path = state_dir / "last.json"
    try:
        previous_doc = json.loads(last_path.read_text(encoding="utf-8"))
        previous = previous_doc.get("restarts_by_uid")
        previous = previous if isinstance(previous, dict) else None
    except (OSError, ValueError):
        previous_doc, previous = None, None
    runner = runner_state(fetch, runner_url)
    try:
        rows = pod_rows(get_pods(), prefix)
    except Exception as error:  # the API is the only source: say UNKNOWN, keep the old counts
        why = str(error) if isinstance(error, RuntimeError) else f"{type(error).__name__}: {error}"
        line = line_of(now, "UNKNOWN", [], None, runner, why=why)
        code = UNKNOWN
        document = {**(previous_doc or {}), "checked_at": now.astimezone(timezone.utc).isoformat(),
                    "verdict": "UNKNOWN", "why": why, "line": line}
    else:
        if not rows:
            line = line_of(now, "UNKNOWN", [], None, runner, why=f"no pods in namespaces {prefix}*")
            code = UNKNOWN
            verdict = "UNKNOWN"
        else:
            increases = restart_increases(rows, previous)
            notready = [r for r in rows if not r["ready"]]
            verdict = "ALERT" if notready or increases else "OK"
            code = ALERT if verdict == "ALERT" else OK
            line = line_of(now, verdict, rows, increases, runner, first=previous is None)
        document = {
            "checked_at": now.astimezone(timezone.utc).isoformat(),
            "verdict": verdict,
            "line": line,
            "runner": runner,
            "namespace_prefix": prefix,
            "pods": rows,
            "restart_increases": restart_increases(rows, previous) if rows else [],
            "restarts_by_uid": {r["uid"]: r["restarts"] for r in rows} if rows else (previous or {}),
        }
    write_atomic(last_path, json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    write_atomic(state_dir / "status.line", line + "\n")
    history = state_dir / "history.log"
    try:
        lines = history.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    lines = (lines + [line])[-HISTORY_KEEP:]
    write_atomic(history, "\n".join(lines) + "\n")
    return code, line


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="testbed-health")
    parser.add_argument("--state-dir", default=os.environ.get("STATE_DIR", "/var/lib/lucida/testbed-health"))
    parser.add_argument("--kubeconfig", default=os.environ.get("KUBECONFIG_PATH", "/root/tb-kubeconfig"))
    parser.add_argument("--runner-url", default=os.environ.get("RUNNER_URL", "http://127.0.0.1:8091"))
    parser.add_argument("--namespace-prefix", default=os.environ.get("NAMESPACE_PREFIX", "rca-testbed-"))
    args = parser.parse_args(argv)
    code, line = check(Path(args.state_dir), now=datetime.now(timezone.utc),
                       get_pods=lambda: run_kubectl(args.kubeconfig), fetch=http_get,
                       runner_url=args.runner_url, prefix=args.namespace_prefix)
    print(line)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
