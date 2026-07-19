#!/usr/bin/env python3
"""Snapshot-and-restore MockServer executor for approved commerce failures."""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance

PROFILE_ID = "mock.expectation"


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    if scenario_id not in profile["parameter_contract"]["allowed_scenarios"]:
        raise ExecutorError("scenario is not allowlisted")
    approved = [profile["scenario_parameters"][scenario_id]]
    approved.extend(
        level["parameters"]
        for level in profile.get("scenario_levels", {}).get(scenario_id, [])
    )
    if params not in approved:
        raise ExecutorError("parameters do not match an approved scenario profile or level")
    if params.get("path") != "/v1/payments":
        raise ExecutorError("mock target is not allowlisted")
    mode = params.get("mode")
    if mode in {"status", "delay"}:
        if set(params) != {"path", "mode", "status_code", "delay_seconds"}:
            raise ExecutorError("status/delay parameters have an invalid shape")
        return
    if mode == "transient_status":
        expected = {
            "path", "mode", "status_code", "remaining_times", "ttl_seconds",
            "pulse_interval_seconds", "max_pulses",
        }
        if set(params) != expected:
            raise ExecutorError("transient_status parameters have an invalid shape")
        if params != {
            "path": "/v1/payments",
            "mode": "transient_status",
            "status_code": 500,
            "remaining_times": 1,
            "ttl_seconds": 10,
            "pulse_interval_seconds": 15,
            "max_pulses": 32,
        }:
            raise ExecutorError("transient_status contract must remain exact")
        return
    raise ExecutorError("mock mode is not allowlisted")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    location = instance["location"]
    if location.get("namespace") != "rca-testbed-commerce" or location.get("resource") != "deployment/testbed-external-pg-mock":
        raise ExecutorError("mock executor requires the canonical commerce MockServer")
    argv = kubectl_bash_argv([
        action, plan["scenario"]["id"], location["namespace"], location["resource"],
        p["path"], p["mode"], str(p["status_code"]), str(p.get("delay_seconds", 0)),
        str(p.get("remaining_times", 0)), str(p.get("ttl_seconds", 0)),
        str(p.get("pulse_interval_seconds", 0)), str(p.get("max_pulses", 0)),
    ])
    return argv, SCRIPT


SCRIPT = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; scenario_id="$2"; ns="$3"; resource="$4"; path="$5"; mode="$6"; status="$7"; delay="$8"
remaining_times="$9"; ttl_seconds="${10}"; pulse_interval_seconds="${11}"; max_pulses="${12}"
state_root="${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}"
state="$state_root/${scenario_id}-mock-expectations.json"
pulse_state="$state_root/${scenario_id}-mock-pulse-state.json"
worker="$state_root/${scenario_id}-mock-pulse-worker.py"
worker_pid="$state_root/${scenario_id}-mock-pulse-worker.pid"
port=$((19100 + 10#${scenario_id:1:2}))
pf_pid=""
k=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$ns")
stop_pf() { [[ -z "$pf_pid" ]] || { kill "$pf_pid" 2>/dev/null || true; wait "$pf_pid" 2>/dev/null || true; pf_pid=""; }; }
trap stop_pf EXIT
start_pf() {
  "${k[@]}" port-forward "$resource" "$port:1080" >/tmp/"${scenario_id}"-mock-pf.log 2>&1 & pf_pid=$!
  for _ in {1..20}; do curl -fsS --max-time 1 "http://127.0.0.1:$port/liveness/probe" >/dev/null 2>&1 && return; sleep .25; done
  return 1
}
retrieve() { curl -fsS --max-time 5 -X PUT "http://127.0.0.1:$port/mockserver/retrieve?type=ACTIVE_EXPECTATIONS" -H 'Content-Type: application/json' -d '{}'; }
reset() { curl -fsS --max-time 5 -X PUT "http://127.0.0.1:$port/mockserver/reset" >/dev/null; }
canonical() { python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin),sort_keys=True,separators=(",",":")))'; }
write_status() {
  local absent="$1" restored="$2"
  PULSE_STATE="$pulse_state" ABSENT="$absent" RESTORED="$restored" python3 - <<'PY'
import datetime, json, os
path = os.environ["PULSE_STATE"]
try:
    with open(path, encoding="utf-8") as stream:
        state = json.load(stream)
except (FileNotFoundError, json.JSONDecodeError):
    state = {}
state.update({
    "observed_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
    "transient_expectation_absent": os.environ["ABSENT"] == "true",
    "snapshot_restored": os.environ["RESTORED"] == "true",
    "pending": False,
})
temporary = path + ".tmp"
with open(temporary, "w", encoding="utf-8") as stream:
    json.dump(state, stream, sort_keys=True)
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
os.replace(temporary, path)
PY
}
inject() {
  local response
  if [[ "$mode" == status ]]; then
    response="{\"statusCode\":$status,\"headers\":{\"content-type\":[\"application/json\"]},\"body\":\"{\\\"status\\\":\\\"RATE_LIMITED\\\"}\"}"
  else
    response="{\"statusCode\":200,\"delay\":{\"timeUnit\":\"SECONDS\",\"value\":$delay},\"headers\":{\"content-type\":[\"application/json\"]},\"body\":\"{\\\"status\\\":\\\"APPROVED\\\",\\\"transaction_id\\\":\\\"scenario-delay\\\"}\"}"
  fi
  reset
  curl -fsS --max-time 5 -X PUT "http://127.0.0.1:$port/mockserver/expectation" -H 'Content-Type: application/json' \
    -d "{\"id\":\"rca-$scenario_id\",\"priority\":100,\"httpRequest\":{\"method\":\"POST\",\"path\":\"$path\"},\"httpResponse\":$response}" >/dev/null
}
write_worker() {
  cat >"$worker" <<'PY'
#!/usr/bin/env python3
import datetime, json, os, signal, subprocess, sys, time, urllib.request

scenario_id, namespace, resource, path, status, remaining, ttl, interval, maximum, port, output = sys.argv[1:]
status, remaining, ttl, interval, maximum, port = map(int, (status, remaining, ttl, interval, maximum, port))
expectation_id = f"rca-{scenario_id}-transient"
stopping = False

def stop(*_args):
    global stopping
    stopping = True

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)

def now():
    return datetime.datetime.now(datetime.timezone.utc)

def atomic(state):
    state["observed_at"] = now().isoformat().replace("+00:00", "Z")
    temporary = output + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(state, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, output)

def request(endpoint, payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{endpoint}", data=body,
        headers={"Content-Type": "application/json"}, method="PUT",
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        raw = response.read()
    return json.loads(raw) if raw else None

pf = subprocess.Popen([
    "kubectl", "--kubeconfig=/root/tb-kubeconfig", "-n", namespace,
    "port-forward", resource, f"{port}:1080",
], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
state = {
    "scenario_id": scenario_id, "expectation_id": expectation_id,
    "started_at": now().isoformat().replace("+00:00", "Z"),
    "last_pulse_at": None, "pulses_inserted": 0, "transient_consumed_count": 0,
    "expired_unconsumed_count": 0, "duplicate_expectation_count": 0, "pending": False,
    "transient_expectation_absent": False, "snapshot_restored": False,
    "worker_active": True,
}
try:
    for _ in range(40):
        if pf.poll() is not None:
            raise RuntimeError("MockServer port-forward stopped")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/liveness/probe", timeout=1).read()
            break
        except Exception:
            time.sleep(.25)
    else:
        raise RuntimeError("MockServer port-forward not ready")
    atomic(state)
    while not stopping and state["pulses_inserted"] < maximum:
        active = request("/mockserver/retrieve?type=ACTIVE_EXPECTATIONS", {}) or []
        matches = [item for item in active if item.get("id") == expectation_id]
        state["duplicate_expectation_count"] += max(0, len(matches) - 1)
        state["pending"] = bool(matches)
        if not matches:
            recorded_before = request("/mockserver/retrieve?type=REQUESTS", {"method": "POST", "path": path}) or []
            created = now()
            request("/mockserver/expectation", {
                "id": expectation_id,
                "priority": 1000,
                "httpRequest": {"method": "POST", "path": path},
                "httpResponse": {
                    "statusCode": status,
                    "headers": {"content-type": ["application/json"]},
                    "body": '{"status":"TRANSIENT_FAILURE"}',
                },
                "times": {"remainingTimes": remaining, "unlimited": False},
                "timeToLive": {"timeUnit": "SECONDS", "timeToLive": ttl, "unlimited": False},
            })
            state["pulses_inserted"] += 1
            state["last_pulse_at"] = created.isoformat().replace("+00:00", "Z")
            state["pending"] = True
            atomic(state)
            resolved = False
            deadline = time.monotonic() + ttl + 2
            while not stopping and time.monotonic() < deadline:
                time.sleep(.5)
                active = request("/mockserver/retrieve?type=ACTIVE_EXPECTATIONS", {}) or []
                matches = [item for item in active if item.get("id") == expectation_id]
                state["duplicate_expectation_count"] += max(0, len(matches) - 1)
                state["pending"] = bool(matches)
                if not matches:
                    recorded_after = request("/mockserver/retrieve?type=REQUESTS", {"method": "POST", "path": path}) or []
                    if len(recorded_after) > len(recorded_before):
                        state["transient_consumed_count"] += 1
                    else:
                        state["expired_unconsumed_count"] += 1
                    resolved = True
                    atomic(state)
                    break
            if not stopping and not resolved:
                active = request("/mockserver/retrieve?type=ACTIVE_EXPECTATIONS", {}) or []
                matches = [item for item in active if item.get("id") == expectation_id]
                recorded_after = request("/mockserver/retrieve?type=REQUESTS", {"method": "POST", "path": path}) or []
                state["pending"] = bool(matches)
                if not matches and len(recorded_after) > len(recorded_before):
                    state["transient_consumed_count"] += 1
                else:
                    state["expired_unconsumed_count"] += 1
                atomic(state)
        atomic(state)
        end = time.monotonic() + interval
        while not stopping and time.monotonic() < end:
            time.sleep(min(.5, end - time.monotonic()))
except Exception as error:
    state["worker_error"] = f"{type(error).__name__}: {error}"
finally:
    state["worker_active"] = False
    atomic(state)
    pf.terminate()
    try:
        pf.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pf.kill()
PY
  chmod 700 "$worker"
}
stop_worker() {
  if [[ -r "$worker_pid" ]]; then
    local pid; pid="$(cat "$worker_pid")"
    kill -TERM "$pid" 2>/dev/null || true
    for _ in {1..40}; do kill -0 "$pid" 2>/dev/null || break; sleep .25; done
    kill -KILL "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    rm -f "$worker_pid"
  fi
}
case "$action" in
  preflight)
    command -v kubectl >/dev/null; command -v curl >/dev/null; command -v python3 >/dev/null
    "${k[@]}" auth can-i get pods | grep -qx yes
    "${k[@]}" rollout status "$resource" --timeout=1s >/dev/null
    [[ ! -e "$state" && ! -e "$worker_pid" ]]
    start_pf; retrieve >/dev/null
    ;;
  run)
    [[ ! -e "$state" && ! -e "$worker_pid" ]]
    mkdir -p "$state_root"; start_pf; retrieve >"$state.tmp"; mv -T "$state.tmp" "$state"
    if [[ "$mode" == transient_status ]]; then
      stop_pf
      write_worker
      nohup python3 "$worker" "$scenario_id" "$ns" "$resource" "$path" "$status" "$remaining_times" "$ttl_seconds" "$pulse_interval_seconds" "$max_pulses" "$port" "$pulse_state" \
        >/tmp/"${scenario_id}"-mock-pulse.log 2>&1 & echo $! >"$worker_pid"
    else
      inject
    fi
    ;;
  cleanup)
    [[ -e "$state" ]] || exit 0
    stop_worker
    start_pf; reset; write_status true false
    curl -fsS --max-time 5 -X PUT "http://127.0.0.1:$port/mockserver/expectation" -H 'Content-Type: application/json' --data-binary "@$state" >/dev/null
    expected="$(canonical <"$state")"; actual="$(retrieve | canonical)"; [[ "$actual" == "$expected" ]]
    write_status true true
    ;;
  recovery)
    "${k[@]}" rollout status "$resource" --timeout=1s >/dev/null
    [[ ! -e "$worker_pid" && -e "$state" ]]
    start_pf; expected="$(canonical <"$state")"; actual="$(retrieve | canonical)"; [[ "$actual" == "$expected" ]]
    write_status true true
    rm -f "$state" "$worker"
    ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
