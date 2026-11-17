#!/usr/bin/env bash
# Tests for testbed-health.py: kubectl and the runner API are fakes on PATH and in a local HTTP-less
# fetch (the runner URL points at a closed port, or the fake answers through RUNNER_FILE).
set -Eeuo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
TOOL="$SCRIPT_DIR/testbed-health.py"
TMP_ROOT=$(mktemp -d)
trap 'rm -rf "$TMP_ROOT"' EXIT
fail() { printf '[FAIL] %s\n' "$*" >&2; exit 1; }

mkdir -p "$TMP_ROOT/bin"
cat >"$TMP_ROOT/bin/kubectl" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$FAKE_LOG"
[[ -n "${FAKE_API_DOWN:-}" ]] && { echo 'The connection to the server 192.168.122.77:6443 was refused' >&2; exit 1; }
cat "$FAKE_PODS"
SH
chmod +x "$TMP_ROOT/bin/kubectl"
export PATH="$TMP_ROOT/bin:$PATH" FAKE_LOG="$TMP_ROOT/kubectl.log" FAKE_PODS="$TMP_ROOT/pods.json"
STATE="$TMP_ROOT/state"

# pods <spec>...: "<ns>/<name>/<uid>/<ready>/<restarts>[/<waiting reason>[/<phase>]]"
pods() {
  python3 - "$@" >"$FAKE_PODS" <<'PY'
import json, sys
items = []
for spec in sys.argv[1:]:
    ns, name, uid, ready, restarts, *rest = spec.split("/")
    reason = rest[0] if rest and rest[0] else None
    phase = rest[1] if len(rest) > 1 else "Running"
    state = {"waiting": {"reason": reason}} if reason else {"running": {}}
    items.append({"metadata": {"namespace": ns, "name": name, "uid": uid},
                  "status": {"phase": phase, "conditions": [{"type": "Ready", "status": "True" if ready == "1" else "False"}],
                             "containerStatuses": [{"name": "c", "restartCount": int(restarts), "state": state}]}})
print(json.dumps({"items": items}))
PY
}
run() { python3 "$TOOL" --state-dir "$STATE" --runner-url "http://127.0.0.1:9" "$@"; }

# First run: every pod Ready, no previous counts (restarts are the baseline, not an increase).
pods rca-testbed-banking/testbed-oracle-0/u1/1/2 rca-testbed-commerce/testbed-order-x/u2/1/17 kube-system/coredns-a/u9/0/5
rc=0; out=$(run) || rc=$?
[[ $rc -eq 0 ]] || fail "all Ready must exit 0, got $rc: $out"
[[ "$out" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}\ [0-9]{2}:[0-9]{2}\ KST\ testbed-health\ OK\ pods\ 2\ notready\ 0\ restarts\+0\ \(first\ run\)\ runner\ unknown$ ]] \
  || fail "first line: $out"
[[ "$(cat "$STATE/status.line")" == "$out" ]] || fail "status.line must hold the line"
grep -q -- '--request-timeout=20s get pods -A -o json' "$FAKE_LOG" || fail "one pod list call: $(cat "$FAKE_LOG")"
[[ $(wc -l <"$FAKE_LOG") -eq 1 ]] || fail "exactly one API call per run"
echo "[PASS] the first run lists the testbed pods once and reports OK without increases"

# Oracle restarts and goes CrashLoopBackOff: ALERT names the pod, the reason and the increase.
pods rca-testbed-banking/testbed-oracle-0/u1/0/7/CrashLoopBackOff rca-testbed-commerce/testbed-order-x/u2/1/17
rc=0; out=$(run) || rc=$?
[[ $rc -eq 3 ]] || fail "a not Ready pod must exit 3, got $rc"
[[ "$out" == *" testbed-health ALERT pods 2 notready 1 restarts+5 runner unknown | notready rca-testbed-banking/testbed-oracle-0(CrashLoopBackOff) | restarts rca-testbed-banking/testbed-oracle-0+5" ]] \
  || fail "alert line: $out"
echo "[PASS] a crash-looping pod is reported with its reason and its restart increase"

# Ready again but restarted once more since the last run: still ALERT (restarts), then OK.
pods rca-testbed-banking/testbed-oracle-0/u1/1/8 rca-testbed-commerce/testbed-order-x/u2/1/17
rc=0; out=$(run) || rc=$?
[[ $rc -eq 3 && "$out" == *"notready 0 restarts+1 runner unknown | restarts rca-testbed-banking/testbed-oracle-0+1" ]] || fail "restart only: $rc $out"
rc=0; out=$(run) || rc=$?
[[ $rc -eq 0 && "$out" == *" OK pods 2 notready 0 restarts+0 runner unknown" ]] || fail "steady state: $rc $out"
echo "[PASS] a restart between runs alerts once, a steady state is OK"

# The pod was recreated (new uid, e.g. deleted by a person): counts from 0, not a negative increase.
pods rca-testbed-banking/testbed-oracle-0/u3/1/0 rca-testbed-commerce/testbed-order-x/u2/1/17
rc=0; out=$(run) || rc=$?
[[ $rc -eq 0 && "$out" == *" OK pods 2 notready 0 restarts+0"* ]] || fail "recreated pod: $rc $out"
pods rca-testbed-banking/testbed-oracle-0/u3/1/2 rca-testbed-commerce/testbed-order-x/u2/1/17 rca-testbed-food/job-a/u7/0/0//Succeeded
rc=0; out=$(run) || rc=$?
[[ $rc -eq 3 && "$out" == *"pods 2 notready 0 restarts+2"* ]] || fail "new uid counts from 0, Succeeded pods are skipped: $rc $out"
echo "[PASS] a recreated pod counts from zero and finished job pods are not counted"

# The API cannot be read: UNKNOWN with kubectl's reason, and the previous counts are kept.
FAKE_API_DOWN=1 run >"$TMP_ROOT/out" || rc=$?
[[ $rc -eq 2 ]] || fail "API down must exit 2, got $rc"
grep -q 'testbed-health UNKNOWN runner unknown | kube API: The connection to the server 192.168.122.77:6443 was refused' "$TMP_ROOT/out" || fail "unknown line: $(cat "$TMP_ROOT/out")"
pods rca-testbed-banking/testbed-oracle-0/u3/1/3 rca-testbed-commerce/testbed-order-x/u2/1/17
rc=0; out=$(run) || rc=$?
[[ "$out" == *"restarts+1 runner unknown | restarts rca-testbed-banking/testbed-oracle-0+1" ]] || fail "counts across an API outage: $out"
# No testbed pod at all is not "healthy".
pods kube-system/coredns-a/u9/1/0
rc=0; out=$(run) || rc=$?
[[ $rc -eq 2 && "$out" == *"UNKNOWN runner unknown | no pods in namespaces rca-testbed-*" ]] || fail "no pods: $rc $out"
echo "[PASS] an unreadable API or an empty namespace set is UNKNOWN, never OK"

# The runner state comes from its local API: active names the scenario.
python3 - "$TOOL" "$STATE" <<'PY' || fail "runner state"
import importlib.util, json, sys
from datetime import datetime, timezone
from pathlib import Path
spec = importlib.util.spec_from_file_location("h", sys.argv[1]); h = importlib.util.module_from_spec(spec); spec.loader.exec_module(h)
pods = {"items": [{"metadata": {"namespace": "rca-testbed-food", "name": "p", "uid": "u"},
                   "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}], "containerStatuses": []}}]}
seen = []
def fetch(url):
    seen.append(url)
    return json.dumps({"is_active": True, "scenario_id": "S1"}).encode()
code, line = h.check(Path(sys.argv[2]) / "x", now=datetime(2026, 11, 17, 9, 3, tzinfo=timezone.utc), get_pods=lambda: pods,
                     fetch=fetch, runner_url="http://127.0.0.1:8091/", prefix="rca-testbed-")
assert seen == ["http://127.0.0.1:8091/api/active"], seen
assert line == "2026-11-17 18:03 KST testbed-health OK pods 1 notready 0 restarts+0 (first run) runner active S1", line
code, line = h.check(Path(sys.argv[2]) / "x", now=datetime(2026, 11, 17, 9, 13, tzinfo=timezone.utc), get_pods=lambda: pods,
                     fetch=lambda url: json.dumps({"is_active": False}).encode(), runner_url="u", prefix="rca-testbed-")
assert line.endswith("runner idle"), line
PY
echo "[PASS] the line carries the runner state (idle, active and its scenario)"

# history keeps every line, bounded.
[[ $(wc -l <"$STATE/history.log") -eq 9 ]] || fail "history lines: $(wc -l <"$STATE/history.log")"
python3 - "$TOOL" "$STATE" <<'PY' || fail "history bound"
import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("h", sys.argv[1]); h = importlib.util.module_from_spec(spec); spec.loader.exec_module(h)
assert h.HISTORY_KEEP == 4320
PY
python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert d['verdict']=='UNKNOWN' and 'restarts_by_uid' in d" "$STATE/last.json" || fail "last.json"
echo "[PASS] history.log and last.json are kept"

# The timer runs at fixed minutes with no randomized delay, and the service treats 2 and 3 as results.
grep -qx 'OnCalendar=\*-\*-\* \*:03/10:00' "$SCRIPT_DIR/systemd/testbed-health.timer" || fail "timer schedule"
! grep -q 'RandomizedDelaySec' "$SCRIPT_DIR/systemd/testbed-health.timer" || fail "no randomized delay"
grep -qx 'SuccessExitStatus=2 3' "$SCRIPT_DIR/systemd/testbed-health.service" || fail "exit statuses"
grep -qx 'ExecStart=/usr/bin/python3 /opt/testbed/testbed-health.py' "$SCRIPT_DIR/systemd/testbed-health.service" || fail "ExecStart"
python3 - "$TOOL" <<'PY' || fail "no ssh in the code"
import ast, sys
tree = ast.parse(open(sys.argv[1]).read())
strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
assert not any(s == "ssh" or s.startswith("ssh ") for s in strings), "the check must not ssh anywhere"
PY
echo "[PASS] the systemd units match the contract and the check never uses ssh"
echo "[PASS] testbed health tests"
