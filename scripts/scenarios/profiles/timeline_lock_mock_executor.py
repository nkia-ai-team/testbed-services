#!/usr/bin/env python3
"""Two-domain, two-root timeline for F15-H (simultaneous) and F15-T2 (sequential).

Both compose the same pair of already-proven sub-injections:

  [0] commerce PostgreSQL inventory table lock (EXCLUSIVE) — held by a short-lived
      in-cluster client pod that impersonates the real application session identity
      (G6/L1: the scenario must not sign itself into the capture).

      Table scope, not the F01-R/F15-T1 single-row scope, and the reason is
      reachability rather than blast-radius appetite. This timeline spends its k6
      on the food arm — food's 429 has no signal outside the load summary — so the
      commerce arm is observed against baseline traffic alone. A product_id=1 row
      lock reaches only the 9.18% of checkouts whose cart contains that product
      (baseline adds 1~2 items uniformly from 16), which at the diurnal trough is
      0.15 concurrent blocked sessions — the gate would almost never stand up.
      An EXCLUSIVE table lock blocks InventoryService.reserve() for every checkout,
      so 1.6 (trough) to 8 (peak) sessions sit blocked and order p95 pins at the
      10s order→inventory read-timeout (RestClientConfig.java:40). Same reasoning
      as F06-H, which went table scope because per-payment INSERTs cannot be
      blocked by a row lock at all.
  [1] food external PG MockServer returning 429 on /pay — the F06-P arm. food
      does not mint 429 itself; PgApiClient.java:50 converts the downstream 4xx to
      ClientErrorException and the order fan-out propagates it unchanged, so the
      429 an operator sees at the food entry point is the external PG's.

F15-H starts both at offset 0. F15-T2 starts the food arm after the commerce lock
has been held, making the two roots separable in time as well as in topology.

The two domains are pinned to different workers and different databases since the
2026-07-28 placement pinning (commerce=tb-w1/PostgreSQL, food=tb-w3/MySQL), so
neither arm can leak into the other's symptom surface. That separation is what
makes the expected judgement anti-merge: two incidents, not one.
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance

PROFILE_ID = "timeline.compose"

ROUTED = {"F15-H": 0, "F15-T2": None}
REQUIRED = {
    "commerce_namespace", "pg_access", "pg_db_pod", "pg_service", "pg_secret",
    "pg_image", "pg_schema", "pg_table", "pg_lock_scope", "pg_lock_mode",
    "pg_client_identity", "pg_hold_seconds", "food_namespace",
    "food_mock_resource", "food_mock_path", "food_mock_status",
    "start_offset_seconds",
}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    if scenario_id not in ROUTED:
        raise ExecutorError("timeline is not allowlisted")
    approved = profile.get("scenario_parameters", {}).get(scenario_id)
    if approved is None or params != approved:
        raise ExecutorError(f"parameters must exactly match an approved {scenario_id} timeline")
    if set(params) != REQUIRED:
        raise ExecutorError("lock/mock timeline parameters have an invalid shape")

    offset = params["start_offset_seconds"]
    if scenario_id == "F15-H":
        if offset != 0:
            raise ExecutorError("F15-H is an exact-simultaneous (offset 0) timeline")
    elif not 60 <= offset <= 600:
        # F15-T2 must separate the two roots by enough time that the commerce
        # lock is already visible when the food arm lands; a few seconds would
        # be indistinguishable from F15-H in the capture.
        raise ExecutorError("F15-T2 must stage the food arm on a bounded delay")

    # Commerce lock arm — identical contract to F15-T1 (timeline_dual_fault_executor).
    if params["commerce_namespace"] != "rca-testbed-commerce":
        raise ExecutorError("commerce lock namespace is not allowlisted")
    if params["pg_access"] != "in-cluster-pod":
        raise ExecutorError("postgresql lock injection must originate inside the cluster")
    if params["pg_db_pod"] != "testbed-postgres-0" or params["pg_service"] != "testbed-postgres":
        raise ExecutorError("PostgreSQL lock target is outside the approved contract")
    if params["pg_schema"] != "inventory_schema" or params["pg_table"] != "inventory":
        raise ExecutorError("PostgreSQL lock relation is not allowlisted")
    # Row scope is deliberately refused here: against baseline-only commerce
    # traffic a single-row lock reaches too little of the checkout stream to be
    # measurable (see module docstring). EXCLUSIVE blocks reserve()'s writes while
    # leaving plain SELECT and actuator health alone, matching F06-H.
    if params["pg_lock_scope"] != "table" or params["pg_lock_mode"] != "EXCLUSIVE":
        raise ExecutorError("commerce arm must hold an EXCLUSIVE table lock")
    if params["pg_client_identity"] != "PostgreSQL JDBC Driver":
        raise ExecutorError("client identity must impersonate the real application session")
    if not 60 <= params["pg_hold_seconds"] <= 900:
        raise ExecutorError("PostgreSQL hold is outside the bounded window")

    # Food mock arm — identical target to F06-P (mock_expectation_executor).
    if params["food_namespace"] != "rca-testbed-food":
        raise ExecutorError("food mock namespace is not allowlisted")
    if params["food_mock_resource"] != "deployment/testbed-external-pg-mock":
        raise ExecutorError("food mock executor requires the canonical testbed MockServer")
    if params["food_mock_path"] != "/pay" or params["food_mock_status"] != 429:
        raise ExecutorError("food mock fault is outside the approved F06-P surface")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    p = profile_instance(plan, PROFILE_ID)["parameters"]
    return kubectl_bash_argv([
        action, plan["scenario"]["id"],
        p["commerce_namespace"], p["pg_db_pod"], p["pg_service"], p["pg_secret"], p["pg_image"],
        p["pg_schema"], p["pg_table"], p["pg_lock_scope"], p["pg_lock_mode"],
        p["pg_client_identity"], str(p["pg_hold_seconds"]),
        p["food_namespace"], p["food_mock_resource"], p["food_mock_path"],
        str(p["food_mock_status"]), str(p["start_offset_seconds"]),
    ]), SCRIPT


# Sub-injection order is fixed: [0] PostgreSQL inventory lock, [1] food mock 429.
# Cleanup is the strict reverse — the mock's original expectations are restored
# first, the lock session is terminated second. A failure on either side fails the
# whole scenario so the run is marked DIRTY rather than silently half-cleaned.
SCRIPT = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; scenario_id="$2"
pg_ns="$3"; db_pod="$4"; svc="$5"; secret="$6"; image="$7"; schema="$8"; table="$9"; scope="${10}"; mode="${11}"
identity="${12}"; hold="${13}"
food_ns="${14}"; mock_resource="${15}"; mock_path="${16}"; mock_status="${17}"; offset="${18}"
state_root="${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}"
state_dir="$state_root/${scenario_id}-lockmock"
expectations="$state_dir/mock-expectations.json"
pg_state="$state_dir/pg-client.pod"
kc=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$pg_ns")
kf=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$food_ns")
sel="lucida.io/db-client=session"
port=19189; pf_pid=""
stop_pf() { [[ -z "$pf_pid" ]] || { kill "$pf_pid" 2>/dev/null || true; wait "$pf_pid" 2>/dev/null || true; }; pf_pid=""; }
trap stop_pf EXIT

# --- [0] PostgreSQL inventory lock, held by an in-cluster client pod (G6/L2) ---
# Identity impersonates the real app; the lock is held by an open transaction
# (idle in transaction), not by a server-side sleep; the source IP is a pod IP.
[[ "$scope" == table ]] || { echo "commerce arm is table scope only" >&2; exit 2; }
case "$mode" in EXCLUSIVE) : ;; *) echo "unsupported lock mode: $mode" >&2; exit 2 ;; esac
lock_sql="LOCK TABLE ${schema}.${table} IN ${mode} MODE;"
p_admin() { "${kc[@]}" exec "$db_pod" -- sh -lc "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -Atc \"$1\""; }
p_tableok() { p_admin "SELECT to_regclass('${schema}.${table}') IS NOT NULL;" | tr -d '[:space:]' | grep -qx t; }
p_clients() { "${kc[@]}" get pods -l "$sel" -o name 2>/dev/null | tr -d '\r'; }
pg() {
  case "$1" in
    check) p_tableok; [[ -z "$(p_clients)" ]] ;;
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
          value: "BEGIN; SELECT pg_backend_pid(); ${lock_sql}"
        - name: SQL_POST
          value: "ROLLBACK;"
      command: ["sh", "-c"]
      args:
        - '{ printf "%s\n" "\$SQL_PRE"; sleep "\$HOLD"; printf "%s\n" "\$SQL_POST"; } | psql -X -At -U "\$POSTGRES_USER" -d "\$POSTGRES_DB"'
YAML
      "${kc[@]}" wait --for=jsonpath='{.status.phase}'=Running "pod/$pod" --timeout=90s >/dev/null
      pid=""
      for _ in $(seq 1 30); do
        pid="$("${kc[@]}" logs "$pod" 2>/dev/null | sed -n '1p' | tr -d '[:space:]')"
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
    recovery) [[ -z "$(p_clients)" ]]; p_tableok ;;
    *) exit 2 ;;
  esac
}

# --- [1] food external PG MockServer: 429 on /pay (F06-P surface) -------------
start_pf() { "${kf[@]}" port-forward "$mock_resource" "$port:1080" >"/tmp/${scenario_id}-lockmock-pf.log" 2>&1 & pf_pid=$!; for _ in {1..20}; do curl -fsS --max-time 1 "http://127.0.0.1:$port/liveness/probe" >/dev/null 2>&1 && return; sleep .25; done; return 1; }
retrieve() { curl -fsS --max-time 5 -X PUT "http://127.0.0.1:$port/mockserver/retrieve?type=ACTIVE_EXPECTATIONS" -H 'Content-Type: application/json' -d '{}'; }
reset_mock() { curl -fsS --max-time 5 -X PUT "http://127.0.0.1:$port/mockserver/reset" >/dev/null; }
mock_check() { command -v curl >/dev/null; "${kf[@]}" rollout status "$mock_resource" --timeout=1s >/dev/null; start_pf; retrieve >/dev/null; stop_pf; }

case "$action" in
  preflight)
    command -v kubectl >/dev/null
    [[ ! -e "$state_dir" ]]
    pg check
    mock_check
    ;;
  run)
    [[ ! -e "$state_dir" ]]; mkdir -p "$state_dir"; umask 077
    start_pf; retrieve >"$expectations.tmp"; mv -T "$expectations.tmp" "$expectations"; stop_pf
    # [0] first, always. F15-H then lands the food arm immediately (offset 0);
    # F15-T2 holds the lock alone for the offset so the roots are separable in time.
    pg run
    [[ "$offset" -eq 0 ]] || sleep "$offset"
    start_pf; reset_mock
    curl -fsS --max-time 5 -X PUT "http://127.0.0.1:$port/mockserver/expectation" -H 'Content-Type: application/json' -d "{\"id\":\"rca-$scenario_id\",\"priority\":100,\"httpRequest\":{\"method\":\"POST\",\"path\":\"$mock_path\"},\"httpResponse\":{\"statusCode\":$mock_status,\"body\":\"{\\\"status\\\":\\\"RATE_LIMITED\\\"}\"}}" >/dev/null
    stop_pf
    ;;
  cleanup)
    [[ -e "$state_dir" ]] || exit 0
    rc=0
    start_pf; reset_mock
    curl -fsS --max-time 5 -X PUT "http://127.0.0.1:$port/mockserver/expectation" -H 'Content-Type: application/json' --data-binary "@$expectations" >/dev/null || rc=1
    stop_pf
    pg cleanup || rc=1
    [[ $rc -eq 0 ]]
    rm -f -- "$expectations"; rmdir -- "$state_dir"
    ;;
  recovery)
    [[ ! -e "$state_dir" ]]
    pg recovery
    start_pf; ! retrieve | grep -q "\"id\"[[:space:]]*:[[:space:]]*\"rca-$scenario_id\""
    ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
