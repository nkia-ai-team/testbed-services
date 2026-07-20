#!/usr/bin/env python3
"""Exact-simultaneous (offset 0) two-root orchestrator foundation for F15-T1.

One orchestration script owns both sub-injections and their reverse-order
cleanup (R6-1): a commerce inventory PostgreSQL row lock (driven from tb-runner
against NodePort 30432, mirroring F01-R) and a food-delivery payment pod OOMKill
loop (kubectl memory-limit reduction, mirroring the F05-R adaptive ladder plus
restart-budget stop-loss). The two roots differ in cause and topology, so the
expected incident judgement is anti-merge: two separate incidents, not one.

This module is a fail-closed *foundation* only. It is not wired to a live
`timeline.compose` executor mapping and every non-F15-T1 timeline stays blocked.
"""
from __future__ import annotations

import json
from typing import Any

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance

PROFILE_ID = "timeline.compose"

# Timelines whose simultaneous/nested contracts remain unresolved. Kept aligned
# with timeline_flap_executor.BLOCKED_TIMELINES minus F15-T1, which this module
# details. Every other composite timeline must still fail closed.
BLOCKED_TIMELINES = {
    "F08-G": "Oracle lock row, credential, and inverse transaction are unresolved",
    "F14-R": "response-loss proxy and duplicate-row cleanup do not exist",
    "F15-H": "food dispatch baseline is not healthy enough for a simultaneous fault",
    "F15-G": "Oracle and PostgreSQL lock rows and acquisition order are unresolved",
    "F15-T2": "food dispatch recovery is a prerequisite",
    "F15-T3": "worker placement and consumer stall SLA are unresolved",
    "F15-T4": "handoff close interval and consumer drain SLA are unresolved",
}

# Food OOM strength is calibrated on the F05-R memory ladder (1Gi baseline). The
# PostgreSQL lock is deterministic and never laddered — only one strength axis
# moves per calibration (controller R7).
FOOD_BASELINE = {
    "limits": {"cpu": "500m", "memory": "1Gi"},
    "requests": {"cpu": "200m", "memory": "512Mi"},
}
FOOD_FAULT_MEMORY_LADDER = ("768Mi", "640Mi", "576Mi")


def _food_fault(memory: str) -> dict[str, Any]:
    return {
        "limits": {"cpu": "500m", "memory": memory},
        "requests": {"cpu": "200m", "memory": "512Mi"},
    }


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    if scenario_id != "F15-T1":
        reason = BLOCKED_TIMELINES.get(scenario_id, "timeline is not allowlisted")
        raise ExecutorError(reason)
    approved = profile.get("scenario_parameters", {}).get(scenario_id)
    if approved is None or params != approved:
        raise ExecutorError("parameters must exactly match an approved F15-T1 timeline")
    required = {
        "commerce_namespace", "pg_runner_host", "pg_db_host", "pg_db_port",
        "pg_db_name", "pg_db_user", "pg_application_name", "pg_product_id",
        "pg_hold_seconds", "food_namespace", "food_deployment", "food_container",
        "food_baseline", "food_fault", "start_offset_seconds",
    }
    if set(params) != required:
        raise ExecutorError("dual-fault timeline parameters have an invalid shape")
    if params["start_offset_seconds"] != 0:
        raise ExecutorError("F15-T1 is an exact-simultaneous (offset 0) timeline")
    if params["commerce_namespace"] != "rca-testbed-commerce":
        raise ExecutorError("commerce lock namespace is not allowlisted")
    if params["pg_runner_host"] != "192.168.122.206" or params["pg_db_host"] != "192.168.122.77":
        raise ExecutorError("PostgreSQL lock must run from tb-runner against the NodePort host")
    if params["pg_db_port"] != 30432 or params["pg_db_name"] != "commerce":
        raise ExecutorError("PostgreSQL lock target is outside the approved NodePort contract")
    if params["pg_application_name"] != f"rca-{scenario_id}-inventory-lock":
        raise ExecutorError("database session tag does not bind the scenario")
    if not 60 <= params["pg_hold_seconds"] <= 900:
        raise ExecutorError("PostgreSQL hold is outside the bounded window")
    if params["food_namespace"] != "rca-testbed-food" or params["food_deployment"] != "testbed-payment":
        raise ExecutorError("food OOM target is not the allowlisted food payment deployment")
    if params["food_baseline"] != FOOD_BASELINE:
        raise ExecutorError("food baseline resources do not match the deployed manifest")
    fault_memory = params["food_fault"].get("limits", {}).get("memory")
    if params["food_fault"] != _food_fault(fault_memory) or fault_memory not in FOOD_FAULT_MEMORY_LADDER:
        raise ExecutorError("food fault memory is not on the measured F05-R ladder")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    p = profile_instance(plan, PROFILE_ID)["parameters"]
    return kubectl_bash_argv([
        action, plan["scenario"]["id"],
        p["pg_runner_host"], p["pg_db_host"], str(p["pg_db_port"]), p["pg_db_name"],
        p["pg_db_user"], p["pg_application_name"], str(p["pg_product_id"]), str(p["pg_hold_seconds"]),
        p["food_namespace"], p["food_deployment"], p["food_container"],
        json.dumps(p["food_baseline"], sort_keys=True, separators=(",", ":")),
        json.dumps(p["food_fault"], sort_keys=True, separators=(",", ":")),
        str(p["start_offset_seconds"]),
    ]), SCRIPT


# Sub-injection order is fixed: [0] PostgreSQL inventory lock, [1] food OOM.
# Both are started at offset 0 (exact simultaneous). Cleanup is the strict
# reverse: food memory restored first, PostgreSQL lock terminated second. A
# failure on either side fails the whole scenario and blocks the next one.
SCRIPT = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; scenario_id="$2"
runner="$3"; db_host="$4"; db_port="$5"; db_name="$6"; db_user="$7"; tag="$8"; product_id="$9"; hold="${10}"
food_ns="${11}"; food_deploy="${12}"; food_container="${13}"; food_baseline="${14}"; food_fault="${15}"; offset="${16}"
state_root="${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}"
state_dir="$state_root/${scenario_id}-dual"
food_state="$state_dir/food-resources.json"
k=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$food_ns")
ssh_runner=(ssh -i /root/.ssh/tb_key -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 "nkia@$runner")

# --- PostgreSQL inventory lock, executed on tb-runner against the NodePort ---
PG_REMOTE='set -euo pipefail
sub="$1"; db_host="$2"; db_port="$3"; db_name="$4"; db_user="$5"; tag="$6"; product_id="$7"; hold="$8"
[[ -r "$HOME/.pgpass" ]] || { echo "trusted PostgreSQL credential file is unavailable" >&2; exit 3; }
base=(psql -X -v ON_ERROR_STOP=1 -h "$db_host" -p "$db_port" -U "$db_user" -d "$db_name")
tag_count() { "${base[@]}" -tAc "SELECT count(*) FROM pg_stat_activity WHERE application_name='"'"'$tag'"'"';" | tr -d "[:space:]"; }
case "$sub" in
  check) command -v psql >/dev/null; "${base[@]}" -tAc "SELECT 1" | grep -qx 1; [[ "$(tag_count)" == 0 ]];
         "${base[@]}" -tAc "SELECT count(*) FROM inventory_schema.inventory WHERE product_id=$product_id;" | grep -qx 1 ;;
  run) nohup env PGAPPNAME="$tag" psql -X -v ON_ERROR_STOP=1 -h "$db_host" -p "$db_port" -U "$db_user" -d "$db_name" \
         -c "BEGIN; SELECT product_id FROM inventory_schema.inventory WHERE product_id=$product_id FOR UPDATE; SELECT pg_sleep($hold); ROLLBACK;" >"/tmp/${tag}.log" 2>&1 </dev/null & ;;
  cleanup) "${base[@]}" -tAc "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name='"'"'$tag'"'"' AND pid <> pg_backend_pid();" >/dev/null; rm -f "/tmp/${tag}.log" ;;
  recovery) "${base[@]}" -tAc "SELECT 1" | grep -qx 1; [[ "$(tag_count)" == 0 ]] ;;
  *) exit 2 ;;
esac'
pg() { "${ssh_runner[@]}" bash -s -- "$1" "$db_host" "$db_port" "$db_name" "$db_user" "$tag" "$product_id" "$hold" <<<"$PG_REMOTE"; }

# --- Food payment OOM via exact-snapshot memory-limit reduction ---
food_current() { "${k[@]}" get deploy "$food_deploy" -o json | jq -Sc --arg c "$food_container" '.spec.template.spec.containers[] | select(.name==$c) | (.resources // {})'; }
food_patch() { jq -cn --arg c "$food_container" --argjson r "$1" '{spec:{template:{spec:{containers:[{name:$c,resources:$r}]}}}}' | "${k[@]}" patch deploy "$food_deploy" --type=strategic --patch-file=/dev/stdin >/dev/null; }
food_check() { command -v jq >/dev/null; "${k[@]}" auth can-i patch deployments >/dev/null; [[ "$(food_current)" == "$food_baseline" ]]; "${k[@]}" rollout status deploy/"$food_deploy" --timeout=1s >/dev/null; }

case "$action" in
  preflight)
    command -v kubectl >/dev/null; command -v jq >/dev/null; command -v ssh >/dev/null
    [[ ! -e "$state_dir" ]]
    pg check
    food_check
    ;;
  run)
    [[ ! -e "$state_dir" ]]; mkdir -p "$state_dir"; umask 077
    printf '%s' "$(food_current)" >"$food_state"
    # offset 0: both roots start together. PG lock first (async on tb-runner),
    # food OOM immediately after with no intervening wait.
    [[ "$offset" -eq 0 ]] || sleep "$offset"
    pg run
    food_patch "$food_fault"
    ;;
  cleanup)
    [[ -e "$state_dir" ]] || exit 0
    # Reverse sub-injection order: food memory restored first, PG lock second.
    # Either failure aborts with a non-zero status so the run is marked DIRTY.
    rc=0
    original=$(cat "$food_state"); [[ "$original" == "$food_baseline" ]]
    food_patch "$original"; "${k[@]}" rollout status deploy/"$food_deploy" --timeout=180s >/dev/null || rc=1
    pg cleanup || rc=1
    [[ $rc -eq 0 ]]
    rm -f "$food_state"; rmdir "$state_dir"
    ;;
  recovery)
    [[ ! -e "$state_dir" ]]
    [[ "$(food_current)" == "$food_baseline" ]]; "${k[@]}" rollout status deploy/"$food_deploy" --timeout=1s >/dev/null
    pg recovery
    ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
