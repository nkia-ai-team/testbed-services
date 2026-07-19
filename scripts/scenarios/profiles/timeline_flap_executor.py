#!/usr/bin/env python3
"""Bounded MockServer flapping timeline foundation for F15-R."""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance

PROFILE_ID = "timeline.compose"

BLOCKED_TIMELINES = {
    "F08-G": "Oracle lock row, credential, and inverse transaction are unresolved",
    "F14-R": "response-loss proxy and duplicate-row cleanup do not exist",
    "F15-H": "food dispatch baseline is not healthy enough for a simultaneous fault",
    "F15-G": "Oracle and PostgreSQL lock rows and acquisition order are unresolved",
    "F15-T1": "food OOM limit and exact simultaneous trigger are unresolved",
    "F15-T2": "food dispatch recovery is a prerequisite",
    "F15-T3": "worker placement and consumer stall SLA are unresolved",
    "F15-T4": "handoff close interval and consumer drain SLA are unresolved",
}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    if scenario_id != "F15-R":
        raise ExecutorError("flapping timeline is allowlisted only for F15-R")
    approved = profile.get("scenario_parameters", {}).get(scenario_id)
    if approved is None or params != approved:
        raise ExecutorError("parameters must exactly match an approved F15-R timeline")
    required = {
        "namespace", "mock_resource", "mock_path", "fault_status",
        "episode_count", "fault_hold_seconds", "recovery_gap_seconds",
    }
    if set(params) != required:
        raise ExecutorError("flapping timeline parameters have an invalid shape")
    if (
        params["namespace"] != "rca-testbed-commerce"
        or params["mock_resource"] != "deployment/testbed-external-pg-mock"
        or params["mock_path"] != "/v1/payments"
        or params["fault_status"] != 429
        or params["episode_count"] != 2
        or not 30 <= params["fault_hold_seconds"] <= 120
        or not 60 <= params["recovery_gap_seconds"] <= 300
    ):
        raise ExecutorError("flapping timeline is outside bounded canonical targets")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    p = profile_instance(plan, PROFILE_ID)["parameters"]
    return kubectl_bash_argv([
        action, plan["scenario"]["id"], p["namespace"], p["mock_resource"],
        p["mock_path"], str(p["fault_status"]), str(p["episode_count"]),
        str(p["fault_hold_seconds"]), str(p["recovery_gap_seconds"]),
    ]), SCRIPT


SCRIPT = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; scenario_id="$2"; ns="$3"; resource="$4"; path="$5"; fault_status="$6"; episodes="$7"; fault_hold="$8"; recovery_gap="$9"
state_root="${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}"
state_dir="$state_root/${scenario_id}-mock-flap"
snapshot="$state_dir/expectations.json"; worker="$state_dir/worker.sh"; worker_pid="$state_dir/worker.pid"
port=19215; pf_pid=""; k=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$ns")
stop_pf() { [[ -z "$pf_pid" ]] || { kill "$pf_pid" 2>/dev/null || true; wait "$pf_pid" 2>/dev/null || true; pf_pid=""; }; }
trap stop_pf EXIT
start_pf() { "${k[@]}" port-forward "$resource" "$port:1080" >"/tmp/${scenario_id}-flap-pf.log" 2>&1 & pf_pid=$!; for _ in {1..40}; do curl -fsS --max-time 1 "http://127.0.0.1:$port/liveness/probe" >/dev/null 2>&1 && return; sleep .25; done; return 1; }
retrieve() { curl -fsS --max-time 5 -X PUT "http://127.0.0.1:$port/mockserver/retrieve?type=ACTIVE_EXPECTATIONS" -H 'Content-Type: application/json' -d '{}'; }
reset() { curl -fsS --max-time 5 -X PUT "http://127.0.0.1:$port/mockserver/reset" >/dev/null; }
restore() { reset; curl -fsS --max-time 5 -X PUT "http://127.0.0.1:$port/mockserver/expectation" -H 'Content-Type: application/json' --data-binary "@$snapshot" >/dev/null; }
canonical() { python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin),sort_keys=True,separators=(",",":")))'; }
stop_worker() { if [[ -r "$worker_pid" ]]; then pid="$(cat "$worker_pid")"; kill -TERM "$pid" 2>/dev/null || true; for _ in {1..40}; do kill -0 "$pid" 2>/dev/null || break; sleep .25; done; kill -KILL "$pid" 2>/dev/null || true; rm -f "$worker_pid"; fi; }
write_worker() {
  cat >"$worker" <<'WORKER'
#!/usr/bin/env bash
set -euo pipefail
ns="$1"; resource="$2"; path="$3"; status="$4"; episodes="$5"; fault_hold="$6"; recovery_gap="$7"; port="$8"; snapshot="$9"
pf=""; k=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$ns")
stop() { [[ -z "$pf" ]] || { kill "$pf" 2>/dev/null || true; wait "$pf" 2>/dev/null || true; }; }
trap stop EXIT INT TERM
"${k[@]}" port-forward "$resource" "$port:1080" >/tmp/F15-R-flap-worker-pf.log 2>&1 & pf=$!
for _ in {1..40}; do curl -fsS --max-time 1 "http://127.0.0.1:$port/liveness/probe" >/dev/null 2>&1 && break; sleep .25; done
reset() { curl -fsS --max-time 5 -X PUT "http://127.0.0.1:$port/mockserver/reset" >/dev/null; }
restore() { reset; curl -fsS --max-time 5 -X PUT "http://127.0.0.1:$port/mockserver/expectation" -H 'Content-Type: application/json' --data-binary "@$snapshot" >/dev/null; }
for ((episode=1; episode<=episodes; episode++)); do
  reset
  curl -fsS --max-time 5 -X PUT "http://127.0.0.1:$port/mockserver/expectation" -H 'Content-Type: application/json' \
    -d "{\"id\":\"rca-F15-R-episode-$episode\",\"priority\":1000,\"httpRequest\":{\"method\":\"POST\",\"path\":\"$path\"},\"httpResponse\":{\"statusCode\":$status,\"body\":\"{\\\"status\\\":\\\"RATE_LIMITED\\\"}\"}}" >/dev/null
  sleep "$fault_hold"; restore
  (( episode == episodes )) || sleep "$recovery_gap"
done
WORKER
  chmod 700 "$worker"
}
case "$action" in
  preflight)
    command -v kubectl >/dev/null; command -v curl >/dev/null; command -v python3 >/dev/null
    "${k[@]}" auth can-i get pods | grep -qx yes; "${k[@]}" rollout status "$resource" --timeout=1s >/dev/null
    [[ ! -e "$state_dir" ]]; start_pf; retrieve >/dev/null
    ;;
  run)
    [[ ! -e "$state_dir" ]]; mkdir -p "$state_dir"; start_pf; retrieve >"$snapshot.tmp"; mv -T "$snapshot.tmp" "$snapshot"; stop_pf
    write_worker; nohup bash "$worker" "$ns" "$resource" "$path" "$fault_status" "$episodes" "$fault_hold" "$recovery_gap" "$port" "$snapshot" \
      >/tmp/F15-R-flap-worker.log 2>&1 & echo $! >"$worker_pid"
    ;;
  cleanup)
    [[ -e "$state_dir" ]] || exit 0; stop_worker; start_pf; restore
    expected="$(canonical <"$snapshot")"; actual="$(retrieve | canonical)"; [[ "$actual" == "$expected" ]]
    rm -f "$snapshot" "$worker"; rmdir "$state_dir"
    ;;
  recovery)
    [[ ! -e "$state_dir" ]]; "${k[@]}" rollout status "$resource" --timeout=1s >/dev/null
    ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
