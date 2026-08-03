#!/usr/bin/env python3
"""Exact-simultaneous (offset 0) two-root orchestrator foundation for F15-T1.

One orchestration script owns both sub-injections and their reverse-order
cleanup (R6-1): a commerce inventory PostgreSQL row lock (held by a short-lived
in-cluster client pod that impersonates the real application session identity,
mirroring F01-R after the 2026-07-28 leak fix) and a food-delivery payment OOMKill
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
    "F15-G": "Oracle and PostgreSQL lock rows and acquisition order are unresolved",
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
        "commerce_namespace", "pg_access", "pg_db_pod", "pg_service", "pg_secret",
        "pg_image", "pg_schema", "pg_table", "pg_key_column", "pg_key_value",
        "pg_client_identity", "pg_hold_seconds", "food_namespace",
        "food_deployment", "food_container", "food_baseline", "food_fault",
        "start_offset_seconds",
    }
    if set(params) != required:
        raise ExecutorError("dual-fault timeline parameters have an invalid shape")
    if params["start_offset_seconds"] != 0:
        raise ExecutorError("F15-T1 is an exact-simultaneous (offset 0) timeline")
    if params["commerce_namespace"] != "rca-testbed-commerce":
        raise ExecutorError("commerce lock namespace is not allowlisted")
    if params["pg_access"] != "in-cluster-pod":
        raise ExecutorError("postgresql lock injection must originate inside the cluster")
    if params["pg_db_pod"] != "testbed-postgres-0" or params["pg_service"] != "testbed-postgres":
        raise ExecutorError("PostgreSQL lock target is outside the approved contract")
    if params["pg_schema"] != "inventory_schema" or params["pg_table"] != "inventory":
        raise ExecutorError("PostgreSQL lock relation is not allowlisted")
    # G6/L1: the session must be indistinguishable from a real application
    # session. Encoding the scenario in the identity puts the answer in the
    # capture (charter appendix A-1/A-2); F01-R impersonates the JDBC driver.
    if params["pg_client_identity"] != "PostgreSQL JDBC Driver":
        raise ExecutorError("client identity must impersonate the real application session")
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
        p["commerce_namespace"], p["pg_db_pod"], p["pg_service"], p["pg_secret"], p["pg_image"],
        p["pg_schema"], p["pg_table"], p["pg_key_column"], str(p["pg_key_value"]),
        p["pg_client_identity"], str(p["pg_hold_seconds"]),
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
pg_ns="$3"; db_pod="$4"; svc="$5"; secret="$6"; image="$7"; schema="$8"; table="$9"; keycol="${10}"; keyval="${11}"
identity="${12}"; hold="${13}"
food_ns="${14}"; food_deploy="${15}"; food_container="${16}"; food_baseline="${17}"; food_fault="${18}"; offset="${19}"
state_root="${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}"
state_dir="$state_root/${scenario_id}-dual"
food_state="$state_dir/food-resources.json"
pg_state="$state_dir/pg-client.pod"
k=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$food_ns")
kc=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$pg_ns")
sel="lucida.io/db-client=session"

# --- PostgreSQL inventory lock, held by an in-cluster client pod (G6/L2) -----
# Identity impersonates the real app; the lock is held by an open transaction
# (idle in transaction), not by a server-side sleep; the source IP is a pod IP.
p_admin() { "${kc[@]}" exec "$db_pod" -- sh -lc "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -Atc \"$1\""; }
p_rowok() { p_admin "SELECT count(*) FROM ${schema}.${table} WHERE ${keycol}='${keyval}';" | tr -d '[:space:]' | grep -qx 1; }
p_clients() { "${kc[@]}" get pods -l "$sel" -o name 2>/dev/null | tr -d '\r'; }
pg() {
  case "$1" in
    check) p_rowok; [[ -z "$(p_clients)" ]] ;;
    run)
      local token pod pid
      token="$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"; pod="commerce-db-client-${token}"
      "${kc[@]}" apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: ${pod}
  namespace: ${pg_ns}
  labels:
    lucida.io/db-client: session
spec:
  restartPolicy: Never
  terminationGracePeriodSeconds: 1
  containers:
    - name: client
      image: ${image}
      envFrom:
        - secretRef:
            name: ${secret}
      env:
        - name: PGHOST
          value: "${svc}"
        - name: PGAPPNAME
          value: "${identity}"
        - name: PGPASSWORD
          valueFrom:
            secretKeyRef:
              name: ${secret}
              key: POSTGRES_PASSWORD
        - name: HOLD
          value: "${hold}"
        - name: SQL_PRE
          value: "BEGIN; SELECT pg_backend_pid(); SELECT ${keycol} FROM ${schema}.${table} WHERE ${keycol}='${keyval}' FOR UPDATE;"
        - name: SQL_POST
          value: "ROLLBACK;"
      command: ["sh", "-c"]
      args:
        - '{ printf "%s\n" "\$SQL_PRE"; sleep "\$HOLD"; printf "%s\n" "\$SQL_POST"; } | psql -X -At -U "\$POSTGRES_USER" -d "\$POSTGRES_DB"'
YAML
      "${kc[@]}" wait --for=jsonpath='{.status.phase}'=Running "pod/$pod" --timeout=90s >/dev/null
      pid=""
      for _ in $(seq 1 30); do
        # psql prints the BEGIN command tag before any result, so line 1 is never
        # the pid. Take the first all-digits line. Same fix as db_lock_executor
        # (2026-07-31) and timeline_multi_injection_executor (2026-08-03).
        pid="$("${kc[@]}" logs "$pod" 2>/dev/null | tr -d '\r' | sed -n '/^[0-9][0-9]*$/{p;q;}')"
        [[ "$pid" =~ ^[0-9]+$ ]] && break
        pid=""; sleep 1
      done
      [[ -n "$pid" ]] || { "${kc[@]}" delete pod "$pod" --now --ignore-not-found >/dev/null; echo "lock session did not report a backend pid" >&2; exit 1; }
      p_admin "SELECT count(*) FROM pg_stat_activity WHERE pid=${pid} AND state='idle in transaction';" | tr -d '[:space:]' | grep -qx 1
      printf '%s %s\n' "$pod" "$pid" >"$pg_state" ;;
    cleanup)
      if [[ -s "$pg_state" ]]; then local pod pid; read -r pod pid <"$pg_state" || true
        [[ -n "${pod:-}" ]] && "${kc[@]}" delete pod "$pod" --now --ignore-not-found >/dev/null; fi
      local q; for q in $(p_clients); do "${kc[@]}" delete "$q" --now --ignore-not-found >/dev/null; done
      rm -f -- "$pg_state"
      [[ -z "$(p_clients)" ]] ;;
    recovery) [[ -z "$(p_clients)" ]]; p_rowok ;;
    *) exit 2 ;;
  esac
}

# --- Food payment OOM via exact-snapshot memory-limit reduction ---
food_current() { "${k[@]}" get deploy "$food_deploy" -o json | jq -Sc --arg c "$food_container" '.spec.template.spec.containers[] | select(.name==$c) | (.resources // {})'; }
food_patch() { jq -cn --arg c "$food_container" --argjson r "$1" '{spec:{template:{spec:{containers:[{name:$c,resources:$r}]}}}}' | "${k[@]}" patch deploy "$food_deploy" --type=strategic --patch-file=/dev/stdin >/dev/null; }
food_check() { command -v jq >/dev/null; "${k[@]}" auth can-i patch deployments >/dev/null; [[ "$(food_current)" == "$food_baseline" ]]; "${k[@]}" rollout status deploy/"$food_deploy" --timeout=1s >/dev/null; }

case "$action" in
  preflight)
    command -v kubectl >/dev/null; command -v jq >/dev/null
    [[ ! -e "$state_dir" ]]
    pg check
    food_check
    ;;
  run)
    [[ ! -e "$state_dir" ]]; mkdir -p "$state_dir"; umask 077
    printf '%s' "$(food_current)" >"$food_state"
    # offset 0: both roots start together. PG lock first (in-cluster client pod),
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
