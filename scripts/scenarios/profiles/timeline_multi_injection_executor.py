#!/usr/bin/env python3
"""Composed multi-injection timeline executor for distractor/multi-root scenarios.

Unlike ``timeline_compose_executor`` (which is bound to the single F08-H
rollout+mock pattern), this profile composes N heterogeneous sub-injections that
each carry their own tagged, idempotent, death-confirmed lifecycle:

* ``rollout``      — annotation-driven Deployment restart (topology distractor)
* ``oracle_lock``  — tagged Oracle ``SELECT ... FOR UPDATE`` on FREEPDB1 via
                     ``kubectl exec`` into the Oracle pod (v$session client
                     identifier tag; death confirmed by v$session absence)
* ``pg_lock``      — PostgreSQL ``SELECT ... FOR UPDATE`` held by a short-lived
                     in-cluster client pod that impersonates the real app's
                     session identity (death confirmed by pod absence)

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
state_dir="$state_root/${scenario_id}-multi"
kube=(kubectl --kubeconfig=/root/tb-kubeconfig)

# ---- oracle_lock primitive (kubectl exec into Oracle pod; FREEPDB1) ----------
oracle() {  # $1=verb $2=step_index
  local verb="$1" i="$2"
  local ns pod schema table keycol key tag hold
  ns=$(field "$i" namespace); pod=$(field "$i" pod); schema=$(field "$i" schema)
  table=$(field "$i" table); keycol=$(field "$i" key_column); key=$(field "$i" key_value)
  tag=$(field "$i" client_identifier); hold=$(field "$i" hold_seconds)
  local k=("${kube[@]}" -n "$ns")
  local pidf="/tmp/${tag}.pid"
  o_rowok() { printf 'alter session set container=FREEPDB1;\nset pages 0 feedback off heading off\nselect count(*) from %s.%s where %s=%s;\nexit;\n' \
      "$schema" "$table" "$keycol" "'$key'" | ${k[@]} exec -i "$pod" -- sqlplus -s / as sysdba | tr -d '[:space:]' | grep -qx 1; }
  o_sessions() { printf 'alter session set container=FREEPDB1;\nset pages 0 feedback off heading off\nselect count(*) from v$session where client_identifier=%s;\nexit;\n' \
      "'$tag'" | ${k[@]} exec -i "$pod" -- sqlplus -s / as sysdba | tr -d '[:space:]'; }
  o_kill() { printf "alter session set container=FREEPDB1;\nset pages 0 feedback off heading off\nbegin for s in (select sid,serial# from v\$session where client_identifier='%s') loop execute immediate 'alter system kill session '''||s.sid||','||s.serial#||''' immediate'; end loop; end;\n/\nexit;\n" \
      "$tag" | ${k[@]} exec -i "$pod" -- sqlplus -s / as sysdba >/dev/null 2>&1 || true; }
  case "$verb" in
    check)  o_rowok; [[ "$(o_sessions)" == 0 ]] ;;
    apply)  ${k[@]} exec "$pod" -- sh -lc 'cat > /tmp/'"$tag"'.sql <<SQL
alter session set container=FREEPDB1;
alter session set current_schema='"$schema"';
begin dbms_session.set_identifier('"'"''"$tag"''"'"'); end;
/
select '"$keycol"' from '"$table"' where '"$keycol"'='"'"''"$key"''"'"' for update;
host sleep '"$hold"';
rollback;
exit;
SQL
nohup sqlplus -s / as sysdba @/tmp/'"$tag"'.sql >/tmp/'"$tag"'.log 2>&1 </dev/null & echo $! >'"$pidf"'' ;;
    release)  # death-confirm: kill server session, verify v$session tag gone
      o_kill
      ${k[@]} exec "$pod" -- sh -lc 'test -s '"$pidf"' && kill "$(cat '"$pidf"')" 2>/dev/null; rm -f '"$pidf"' /tmp/'"$tag"'.sql /tmp/'"$tag"'.log' 2>/dev/null || true
      for _ in $(seq 1 20); do [[ "$(o_sessions)" == 0 ]] && return 0; sleep 1; done
      return 1 ;;
    recover)  [[ "$(o_sessions)" == 0 ]]; o_rowok ;;
  esac
}

# ---- pg_lock primitive (in-cluster client pod; app identity impersonation) ----
# Same recipe as the db.lock executor; see the Korean note above ORCHESTRATOR.
pg() {  # $1=verb $2=step_index
  local verb="$1" i="$2"
  local ns db_pod svc secret image schema table keycol keyval identity hold
  ns=$(field "$i" namespace); db_pod=$(field "$i" db_pod); svc=$(field "$i" service)
  secret=$(field "$i" secret); image=$(field "$i" image); schema=$(field "$i" schema)
  table=$(field "$i" table); keycol=$(field "$i" key_column); keyval=$(field "$i" key_value)
  identity=$(field "$i" client_identity); hold=$(field "$i" hold_seconds)
  local k=("${kube[@]}" -n "$ns")
  local sel="lucida.io/db-client=session"
  local saved="$state_dir/pg-$i.pod"
  p_admin() { "${k[@]}" exec "$db_pod" -- sh -lc "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -Atc \"$1\""; }
  p_rowok() { p_admin "SELECT count(*) FROM ${schema}.${table} WHERE ${keycol}='${keyval}';" | tr -d '[:space:]' | grep -qx 1; }
  p_clients() { "${k[@]}" get pods -l "$sel" -o name 2>/dev/null | tr -d '\r'; }
  case "$verb" in
    check)  p_rowok; [[ -z "$(p_clients)" ]] ;;
    apply)
      local token pod pid
      token="$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"; pod="commerce-db-client-${token}"
      "${k[@]}" apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: ${pod}
  namespace: ${ns}
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
      "${k[@]}" wait --for=jsonpath='{.status.phase}'=Running "pod/$pod" --timeout=90s >/dev/null
      pid=""
      for _ in $(seq 1 30); do
        pid="$("${k[@]}" logs "$pod" 2>/dev/null | sed -n '1p' | tr -d '[:space:]')"
        [[ "$pid" =~ ^[0-9]+$ ]] && break
        pid=""; sleep 1
      done
      [[ -n "$pid" ]] || { "${k[@]}" delete pod "$pod" --now --ignore-not-found >/dev/null; echo "lock session did not report a backend pid" >&2; exit 1; }
      p_admin "SELECT count(*) FROM pg_stat_activity WHERE pid=${pid} AND state='idle in transaction';" | tr -d '[:space:]' | grep -qx 1
      printf '%s %s\n' "$pod" "$pid" >"$saved" ;;
    release)
      # Never kill by session name: the identity now equals the real app's.
      # Deleting the client pod drops the connection and rolls the txn back.
      if [[ -s "$saved" ]]; then local pod pid; read -r pod pid <"$saved" || true
        [[ -n "${pod:-}" ]] && "${k[@]}" delete pod "$pod" --now --ignore-not-found >/dev/null; fi
      local p; for p in $(p_clients); do "${k[@]}" delete "$p" --now --ignore-not-found >/dev/null; done
      rm -f -- "$saved"
      for _ in $(seq 1 20); do [[ -z "$(p_clients)" ]] && return 0; sleep 1; done
      return 1 ;;
    recover)  [[ -z "$(p_clients)" ]]; p_rowok ;;
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
      "${k[@]}" rollout status deploy/"$deploy" --timeout=180s >/dev/null; rm -f "$saved" ;;
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
