#!/usr/bin/env python3
"""Two-fault F08-H timeline with reverse-order recovery."""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance

PROFILE_ID = "timeline.compose"


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    if scenario_id not in profile["parameter_contract"]["allowed_scenarios"]:
        raise ExecutorError("scenario is not allowlisted")
    if params != profile["scenario_parameters"][scenario_id]:
        raise ExecutorError("parameters do not match the approved scenario profile")
    if params["mock_path"] != "/v1/payments" or not 1 <= params["second_fault_offset_seconds"] <= 60:
        raise ExecutorError("timeline is outside the approved bounds")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    p = profile_instance(plan, PROFILE_ID)["parameters"]
    return kubectl_bash_argv([
        action, plan["scenario"]["id"], p["namespace"], p["deployment"], p["annotation_key"],
        p["mock_resource"], p["mock_path"], str(p["mock_status"]), str(p["second_fault_offset_seconds"]),
    ]), SCRIPT


SCRIPT = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; scenario_id="$2"; ns="$3"; deploy="$4"; annotation="$5"; mock_resource="$6"; mock_path="$7"; mock_status="$8"; offset="$9"
state_root="${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}"
state_dir="$state_root/${scenario_id}-timeline"
expectations="$state_dir/mock-expectations.json"; annotation_state="$state_dir/annotation"
port=19188; pf_pid=""; k=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$ns")
stop_pf() { [[ -z "$pf_pid" ]] || { kill "$pf_pid" 2>/dev/null || true; wait "$pf_pid" 2>/dev/null || true; }; }
trap stop_pf EXIT
start_pf() { "${k[@]}" port-forward "$mock_resource" "$port:1080" >"/tmp/${scenario_id}-timeline-pf.log" 2>&1 & pf_pid=$!; for _ in {1..20}; do curl -fsS --max-time 1 "http://127.0.0.1:$port/liveness/probe" >/dev/null 2>&1 && return; sleep .25; done; return 1; }
retrieve() { curl -fsS --max-time 5 -X PUT "http://127.0.0.1:$port/mockserver/retrieve?type=ACTIVE_EXPECTATIONS" -H 'Content-Type: application/json' -d '{}'; }
reset_mock() { curl -fsS --max-time 5 -X PUT "http://127.0.0.1:$port/mockserver/reset" >/dev/null; }
annotation_value() { "${k[@]}" get deploy "$deploy" -o "jsonpath={.spec.template.metadata.annotations['$annotation']}"; }
restore_rollout() {
  original=$(cat "$annotation_state")
  if [[ "$original" == __ABSENT__ ]]; then
    "${k[@]}" patch deploy "$deploy" --type=merge -p "{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{\"$annotation\":null}}}}}" >/dev/null
  else
    "${k[@]}" patch deploy "$deploy" --type=merge -p "{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{\"$annotation\":\"$original\"}}}}}" >/dev/null
  fi
  "${k[@]}" rollout status deploy/"$deploy" --timeout=180s >/dev/null
}
check() { command -v kubectl >/dev/null; command -v curl >/dev/null; "${k[@]}" auth can-i patch deployments | grep -qx yes; "${k[@]}" rollout status deploy/"$deploy" --timeout=1s >/dev/null; "${k[@]}" rollout status "$mock_resource" --timeout=1s >/dev/null; start_pf; retrieve >/dev/null; stop_pf; pf_pid=""; }
case "$action" in
  preflight) check; [[ ! -e "$state_dir" ]] ;;
  run)
    check; mkdir -p "$state_dir"; current=$(annotation_value); [[ -n "$current" ]] && printf '%s' "$current" >"$annotation_state" || printf '__ABSENT__' >"$annotation_state"
    start_pf; retrieve >"$expectations.tmp"; mv -T "$expectations.tmp" "$expectations"; stop_pf; pf_pid=""
    stamp=$(date -u +%Y%m%dT%H%M%SZ); "${k[@]}" patch deploy "$deploy" --type=merge -p "{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{\"$annotation\":\"$stamp\"}}}}}" >/dev/null
    sleep "$offset"; start_pf; reset_mock
    curl -fsS --max-time 5 -X PUT "http://127.0.0.1:$port/mockserver/expectation" -H 'Content-Type: application/json' -d "{\"id\":\"rca-$scenario_id\",\"priority\":100,\"httpRequest\":{\"method\":\"POST\",\"path\":\"$mock_path\"},\"httpResponse\":{\"statusCode\":$mock_status,\"body\":\"{\\\"status\\\":\\\"RATE_LIMITED\\\"}\"}}" >/dev/null
    ;;
  cleanup)
    [[ -e "$state_dir" ]] || exit 0
    # Reverse sub-injection order is mandatory: external mock first, rollout second.
    start_pf; reset_mock; curl -fsS --max-time 5 -X PUT "http://127.0.0.1:$port/mockserver/expectation" -H 'Content-Type: application/json' --data-binary "@$expectations" >/dev/null; stop_pf; pf_pid=""
    restore_rollout; rm -f -- "$expectations" "$annotation_state"; rmdir -- "$state_dir"
    ;;
  recovery) [[ ! -e "$state_dir" ]]; "${k[@]}" rollout status deploy/"$deploy" --timeout=1s >/dev/null; start_pf; retrieve | grep -q '"statusCode"[[:space:]]*:[[:space:]]*200' ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
