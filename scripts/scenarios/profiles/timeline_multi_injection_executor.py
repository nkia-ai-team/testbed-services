#!/usr/bin/env python3
"""Composed multi-injection timeline executor for distractor/multi-root scenarios.

Unlike ``timeline_compose_executor`` (which is bound to the single F08-H
rollout+mock pattern), this profile composes N heterogeneous sub-injections that
each carry their own tagged, idempotent, death-confirmed lifecycle:

* ``rollout``      — annotation-driven Deployment restart (topology distractor)
* ``oracle_lock``  — tagged Oracle ``SELECT ... FOR UPDATE`` on FREEPDB1 held by
                     a runner-side network session as the app schema account
                     (``oracle_lock_session.py``; no exec into the Oracle pod;
                     death confirmed by the row becoming lockable again)
* ``pg_lock``      — PostgreSQL ``SELECT ... FOR UPDATE`` held by a short-lived
                     in-cluster client pod (one psql, driven over attach) under the
                     real app's session identity (``pg_lock_remote.py``; death
                     confirmed by the session's backend being gone on the server)

Sub-injections apply in declared order (with per-step ``offset_seconds``) and
clean up in strict reverse order. Each lock's cleanup terminates the *server*
session (not merely the client process) and verifies the tag is gone, satisfying
the R3 death-confirmation rule at the same standard as ``db_lock_executor``.

Live side effects only ever happen through ``executor_common`` after the plan
digest + confirmation gate; ``build_invocation`` is pure and returns argv+stdin.
"""
from __future__ import annotations

import base64
import json
from typing import Any

import db_operator_remote
import oracle_lock_remote
import pg_lock_remote
from executor_common import ExecutorError, cli, profile_instance

PROFILE_ID = "timeline.multi"

# Fully-resolved, verified contracts. Any live parameter must match exactly.
# Oracle row BANKING.accounts id='commerce-settlement' verified present (PK id).
# PG row commerce inventory product_id=1 shares the F01-R verified lock row.
CONTRACTS: dict[str, dict[str, Any]] = {
    "F08-G": {
        "steps": [
            {
                "name": "distractor-rollout",
                "kind": "rollout",
                "offset_seconds": 0,
                "namespace": "rca-testbed-commerce",
                "deployment": "testbed-notification",
                "annotation_key": "rca.lucida.ai/f08-g-distractor",
            },
            {
                "name": "banking-oracle-lock",
                "kind": "oracle_lock",
                "offset_seconds": 15,
                "namespace": "rca-testbed-banking",
                "pod": "testbed-oracle-0",
                "schema": "BANKING",
                "table": "accounts",
                "key_column": "id",
                "key_value": "commerce-settlement",
                "client_identifier": "dba-maintenance",
                "hold_seconds": 600,
            },
        ],
    },
    "F15-G": {
        "steps": [
            {
                "name": "commerce-inventory-lock",
                "kind": "pg_lock",
                "offset_seconds": 0,
                "access": "in-cluster-pod",
                "namespace": "rca-testbed-commerce",
                "db_pod": "testbed-postgres-0",
                "service": "testbed-postgres",
                "secret": "postgres-secret",
                "image": "postgres:16-alpine",
                "schema": "inventory_schema",
                "table": "inventory",
                "key_column": "product_id",
                "key_value": "1",
                "client_identity": "PostgreSQL JDBC Driver",
                "hold_seconds": 600,
            },
            {
                "name": "banking-settlement-lock",
                "kind": "oracle_lock",
                "offset_seconds": 0,
                "namespace": "rca-testbed-banking",
                "pod": "testbed-oracle-0",
                "schema": "BANKING",
                "table": "accounts",
                "key_column": "id",
                "key_value": "commerce-settlement",
                "client_identifier": "dba-maintenance",
                "hold_seconds": 600,
            },
        ],
    },
}

_KINDS = {"rollout", "oracle_lock", "pg_lock"}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    if scenario_id in CONTRACTS:
        if params != CONTRACTS[scenario_id]:
            raise ExecutorError("parameters do not exactly match the verified multi-injection contract")
    else:
        if scenario_id not in profile["parameter_contract"]["allowed_scenarios"]:
            raise ExecutorError("scenario is not allowlisted")
        if params != profile["scenario_parameters"][scenario_id]:
            raise ExecutorError("parameters do not match the approved scenario profile")
    steps = params.get("steps")
    if not isinstance(steps, list) or not (2 <= len(steps) <= 4):
        raise ExecutorError("multi-injection requires between two and four ordered steps")
    seen: set[str] = set()
    for step in steps:
        if step.get("kind") not in _KINDS:
            raise ExecutorError("unknown sub-injection kind")
        if step["name"] in seen:
            raise ExecutorError("sub-injection names must be unique")
        seen.add(step["name"])
        if not 0 <= int(step.get("offset_seconds", 0)) <= 120:
            raise ExecutorError("sub-injection offset is outside the approved bounds")
        if step["kind"] in ("oracle_lock", "pg_lock") and int(step["hold_seconds"]) <= 0:
            raise ExecutorError("lock hold must be positive")
        if step["kind"] == "pg_lock" and step.get("access") != "in-cluster-pod":
            raise ExecutorError("postgresql lock injection must originate inside the cluster")
    if not any(s["kind"] in ("oracle_lock", "pg_lock") for s in steps):
        raise ExecutorError("a multi-injection must contain at least one tagged DB lock root")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    p = profile_instance(plan, PROFILE_ID)["parameters"]
    spec = json.dumps(
        {"scenario_id": plan["scenario"]["id"], "steps": p["steps"]},
        sort_keys=True,
        separators=(",", ":"),
    )
    spec_b64 = base64.b64encode(spec.encode()).decode()
    return ["/usr/bin/bash", "-s", "--", action, spec_b64], ORCHESTRATOR


# pg_lock 경로는 2026-07-28에 db.lock 실행기와 같은 방식으로 교체했다(기준서 G6/L2).
# 이전의 ssh+NodePort 방식은 한 세션에 세 개의 유일값을 남겼다 — application_name에
# 인코딩된 시나리오 ID(L1), 실운영에 없는 대기 이벤트 PgSleep(L2), 클러스터 밖
# tb-runner의 NAT 주소(L2). 셋 다 캡처에서 정답을 지목한다.
#
# The orchestrator decodes the spec with a stdlib python3 (present on the runner)
# and drives each sub-injection. Reverse-order cleanup is mandatory: the last
# root applied is released first so no dependent effect outlives its cause.
ORCHESTRATOR = br"""#!/usr/bin/env bash
set -euo pipefail
action="$1"; spec_b64="$2"
scenario_id=$(printf '%s' "$spec_b64" | base64 -d | python3 -c 'import json,sys;print(json.load(sys.stdin)["scenario_id"])')
step_count=$(printf '%s' "$spec_b64" | base64 -d | python3 -c 'import json,sys;print(len(json.load(sys.stdin)["steps"]))')
field() { printf '%s' "$spec_b64" | base64 -d | python3 -c 'import json,sys;print(json.load(sys.stdin)["steps"]['"$1"'].get("'"$2"'",""))'; }

state_root="${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}"
# How long cleanup waits for Ready after restoring. Measured on 66: app pods become Ready in
# 55-145s at idle. The startupProbe allows 5s x 60 = 300s (at 5s x 30 = 150s, loaded startups
# restarted and took about 5 minutes). 420s covers the full 300s with margin. Waits in run are
# left alone (they set the scenario strength and the judging time).
cleanup_wait=420s
state_dir="$state_root/${scenario_id}-multi"
kube=(kubectl --kubeconfig=/root/tb-kubeconfig)

""" + oracle_lock_remote.bash_functions() + db_operator_remote.bash_functions() + pg_lock_remote.bash_functions() + br"""
# ---- oracle_lock primitive (runner-side network session; oracle_lock_session.py) ----
# v4 C6: the lock session connects from the runner container through the listener
# as the app schema account and idles with its transaction open. Nothing runs in the
# Oracle pod (the old way logged in there as SYS and slept in a child process,
# which 119 recorded; see oracle_lock_remote.py). Release ends the session process
# (rollback); the server-side check is that the row can be locked again. Same
# functions as db.lock.
oracle() {  # $1=verb $2=step_index
  local verb="$1" i="$2"
  local ns pod schema table keycol key tag hold
  ns=$(field "$i" namespace); pod=$(field "$i" pod); schema=$(field "$i" schema)
  table=$(field "$i" table); keycol=$(field "$i" key_column); key=$(field "$i" key_value)
  tag=$(field "$i" client_identifier); hold=$(field "$i" hold_seconds)
  local run_dir="$state_dir/oracle-$i"
  case "$verb" in
    check)
      ora_idle "$run_dir"; ora_prepare "$ns" "$pod"; ora_dcd_ok "$ns" "$pod"
      ora_target "$schema" "$table" "$keycol" "$key"; ora_row_ok; ora_released ;;
    apply)
      ora_prepare "$ns" "$pod"; ora_target "$schema" "$table" "$keycol" "$key"
      ora_hold_start "$run_dir" "$tag" "$hold" ;;
    release)  # errexit is off here (dispatch release ... || rc=1), so every step is checked.
      # Local first: stop this host's session whatever the API or the database says,
      # then confirm server-side that the row is lockable again.
      local rc=0
      ora_hold_stop "$run_dir" || rc=1
      if ora_prepare "$ns" "$pod"; then
        ora_target "$schema" "$table" "$keycol" "$key"
        ora_released || rc=1
      else
        rc=1
      fi
      if [[ "$rc" == 0 ]]; then rm -rf -- "$run_dir"; fi
      return "$rc" ;;
    recover)
      ora_idle "$run_dir"; ora_prepare "$ns" "$pod"
      ora_target "$schema" "$table" "$keycol" "$key"; ora_row_ok; ora_released ;;
  esac
}

# ---- pg_lock primitive (in-cluster client pod, one psql driven over attach; pg_lock_remote.py) ----
# The lock session is a client pod's psql under the real app's application_name, idling with its
# transaction open (pod address, like the app's own sessions). v4 C6b: the pod runs psql alone (no
# sh, no sleep, no SQL in its spec); the runner sends the statements over attach and counts the hold.
# Release stops the runner's driver, deletes the pod, then confirms on the server that its backend
# is gone. Same functions as db.lock.
pg() {  # $1=verb $2=step_index
  local verb="$1" i="$2"
  local ns db_pod schema table keycol keyval identity hold
  ns=$(field "$i" namespace); db_pod=$(field "$i" db_pod); schema=$(field "$i" schema)
  PGL_IMAGE=$(field "$i" image); PGL_SERVICE=$(field "$i" service); PGL_SECRET=$(field "$i" secret)
  table=$(field "$i" table); keycol=$(field "$i" key_column); keyval=$(field "$i" key_value)
  identity=$(field "$i" client_identity); hold=$(field "$i" hold_seconds)
  local run_dir="$state_dir/pg-$i"
  db_target "$ns" "$db_pod"
  case "$verb" in
    check)   pgl_check "$run_dir" "$schema" "$table" row "$keycol" "$keyval" ;;
    apply)   pgl_apply "$run_dir" "$schema" "$table" row "" "$keycol" "$keyval" "$hold" "$identity" ;;
    release) pgl_release "$run_dir" ;;  # errexit is off here; pgl_release returns its result.
    recover) pgl_recover "$run_dir" "$schema" "$table" row "$keycol" "$keyval" ;;
  esac
}

# ---- rollout primitive (annotation restart of a topology distractor) ---------
rollout() {  # $1=verb $2=step_index
  local verb="$1" i="$2"
  local ns deploy ann; ns=$(field "$i" namespace); deploy=$(field "$i" deployment); ann=$(field "$i" annotation_key)
  local k=("${kube[@]}" -n "$ns"); local saved="$state_dir/rollout-$i.annotation"
  case "$verb" in
    check)  "${k[@]}" auth can-i patch deployments | grep -qx yes; "${k[@]}" rollout status deploy/"$deploy" --timeout=1s >/dev/null ;;
    apply)  local cur; cur=$("${k[@]}" get deploy "$deploy" -o "jsonpath={.spec.template.metadata.annotations['$ann']}")
      [[ -n "$cur" ]] && printf '%s' "$cur" >"$saved" || printf '__ABSENT__' >"$saved"
      "${k[@]}" patch deploy "$deploy" --type=merge -p "{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{\"$ann\":\"$(date -u +%Y%m%dT%H%M%SZ)\"}}}}}" >/dev/null ;;
    release)  local orig; orig=$(cat "$saved" 2>/dev/null || echo __ABSENT__)
      if [[ "$orig" == __ABSENT__ ]]; then
        "${k[@]}" patch deploy "$deploy" --type=merge -p "{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{\"$ann\":null}}}}}" >/dev/null
      else
        "${k[@]}" patch deploy "$deploy" --type=merge -p "{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{\"$ann\":\"$orig\"}}}}}" >/dev/null
      fi
      "${k[@]}" rollout status deploy/"$deploy" --timeout="$cleanup_wait" >/dev/null; rm -f "$saved" ;;
    recover)  "${k[@]}" rollout status deploy/"$deploy" --timeout=1s >/dev/null ;;
  esac
}

dispatch() { case "$(field "$2" kind)" in oracle_lock) oracle "$1" "$2";; pg_lock) pg "$1" "$2";; rollout) rollout "$1" "$2";; *) exit 2;; esac; }

case "$action" in
  preflight)
    [[ ! -e "$state_dir" ]]
    for ((i=0; i<step_count; i++)); do dispatch check "$i"; done ;;
  run)
    for ((i=0; i<step_count; i++)); do dispatch check "$i"; done
    mkdir -p "$state_dir"
    for ((i=0; i<step_count; i++)); do
      off=$(field "$i" offset_seconds); [[ "${off:-0}" -gt 0 ]] && sleep "$off"
      dispatch apply "$i"
    done ;;
  cleanup)
    [[ -e "$state_dir" ]] || exit 0
    rc=0
    for ((i=step_count-1; i>=0; i--)); do dispatch release "$i" || rc=1; done
    rmdir -- "$state_dir" 2>/dev/null || true
    exit "$rc" ;;
  recovery)
    [[ ! -e "$state_dir" ]]
    for ((i=0; i<step_count; i++)); do dispatch recover "$i"; done ;;
  *) exit 2 ;;
esac
"""


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
