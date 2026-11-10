"""Bash side of the PostgreSQL sessions that tell an application or batch story (v4 C6b).

db.lock (F01-R, F06-H)와 timeline 실행기 셋(timeline.multi 의 pg_lock 단계 F15-G,
timeline_dual_fault F15-T1, timeline_lock_mock F15-H, F15-T2)의 잠금이 이 함수 묶음을 쓴다. db.ddl 내보내기
대기열(F66-R, F66-P, F67-H, F73-R)의 내보내기는 배치 Job(db_operator_remote 의 pdb_export_job)이고 마이그레이션은
운영자 네트워크 세션(pdb_spawn)이다(수정 2회차).

세션의 출처(사용자 결정 Q8, 2026-11-10): 정답지가 앱이나 배치 세션으로 그리는 PostgreSQL 세션은
클러스터 안 파드에서 붙는다(파드 대역 주소, 앱 세션과 같은 대역). 러너 컨테이너에서 NodePort 로
붙으면 DB 가 보는 주소가 DPM 관제 세션(10.244.3.1)이나 러너 부하와 같아진다(C6b 리뷰 2절).

클라이언트 파드(2026-07-28 의 F01-R 방식)를 다음처럼 줄였다.

* 파드 안 프로세스는 psql 하나다. 옛 파드는 ``sh -c '{ printf "$SQL_PRE"; sleep HOLD; ... } | psql'``
  이라 노드 프로세스 표본에 sh, sleep 600 과 SQL 이 든 명령줄이 남았다. 이제 psql 은 stdin 을 기다리고
  러너가 ``kubectl attach`` 로 문장을 보낸다. 파드 정의에는 SQL 이 없다(잠금 파드). 보유 시간도
  러너가 센다.
* 파드는 DB 파드의 nodeSelector 로 도메인 노드에 둔다(리뷰 2회차 하). 다른 노드면 앱 이름 세션이 다른
  도메인 파드 대역에서 온다.
* 러너가 사라지면: attach 가 끊겨 stdin 이 닫히고(stdinOnce) psql 이 끝나 롤백한다. 끊김을 늦게
  알아채도 파드의 activeDeadlineSeconds(보유 + 여유)가 파드를 끝낸다.

정리는 러너의 attach 구동기를 먼저 멈추고(ROLLBACK, \\q), 파드를 이름과 라벨로 지운 뒤, 서버에서 그
세션(backend pid 와 시작 시각)이 사라졌는지 운영자 세션(db_operator_remote 의 pdb)으로 확인한다.
이름으로 세션을 죽이지 않는다(신원이 실 앱과 같다).
"""
from __future__ import annotations

import shlex

HEREDOC_END = "PG_POD_DRIVER_SH"
START_MARKER = "# ---- PostgreSQL client pod sessions (pg_lock_remote.py) ----"
END_MARKER = "# ---- end of PostgreSQL client pod sessions ----"
CLIENT_SELECTOR = "lucida.io/db-client=session"
CLIENT_CONTAINER = "client"
# 잠금을 얻었다는 표시를 기다리는 상한(초). 넘으면 파드를 지워 연결을 끊는다(서버가 대기를 취소한다).
ACQUIRE_TIMEOUT_SECONDS = 60
# 러너가 사라졌을 때 파드가 스스로 끝나기까지 보유 시간 위로 더 두는 여유(초).
DEADLINE_MARGIN_SECONDS = 60
# The label the batch export Jobs carry (pdb_export_job), the way such jobs are labelled; nothing names the runner.
PG_EXPORT_LABEL = "app.kubernetes.io/component=data-export"
# The export pod's TCP receive buffer (min, default, max), set in the pod's own network namespace by a privileged init
# container. Without it the kernel grows the buffer to megabytes and the server finishes early: 104 k3s, 11 MB at 40 s
# nominal ended after 22 s without it and after 39 s with it (C6b 수정 2회차).
PG_EXPORT_RMEM = "4096 8192 8192"
# The export pod's reader: a second's pause every r rows (the old in-pod pacing).
PG_EXPORT_PACE = 'NR % r == 0 { system("sleep 1") }'

# The driver runs on the runner (never in a pod), in its own process group (setsid). It holds
# `kubectl attach -i` to the pod's psql: lines written to DIR/in go to psql's stdin, psql's output is
# appended to DIR/out. After DEADLINE seconds, or on SIGTERM, it sends ROLLBACK and \q and ends.
DRIVER_SCRIPT = r'''#!/usr/bin/env bash
set -u
stop=0
trap 'stop=1' TERM INT
dir="$DRIVER_DIR"
rm -f -- "$dir/to"; mkfifo -m 600 "$dir/to"
kubectl --kubeconfig "$DRIVER_KUBECONFIG" -n "$DRIVER_NS" attach -i -q "$DRIVER_POD" -c "$DRIVER_CONTAINER" \
  <"$dir/to" >>"$dir/out" 2>>"$dir/attach.err" &
attach=$!
exec 5>"$dir/to"
exec 4<>"$dir/in"
end=$((SECONDS + DEADLINE))
while (( !stop && SECONDS < end )) && kill -0 "$attach" 2>/dev/null; do
  if IFS= read -r -t 1 line <&4; then printf '%s\n' "$line" >&5 2>/dev/null || break; fi
done
printf '%s\n' 'ROLLBACK;' '\q' >&5 2>/dev/null
exec 5>&-
for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$attach" 2>/dev/null || exit 0; sleep 1; done
kill "$attach" 2>/dev/null
exit 0
'''


def bash_functions() -> bytes:
    """pgl_* functions. Needs db_operator_remote's functions (db_target, dbo_k, pdb_env, pdb) first."""
    if any(line.strip() == HEREDOC_END for line in DRIVER_SCRIPT.splitlines()):
        raise ValueError("driver script collides with the heredoc terminator")
    header = "\n".join(
        [
            START_MARKER,
            f"PGL_SELECTOR='{CLIENT_SELECTOR}'",
            f"PGL_CONTAINER='{CLIENT_CONTAINER}'",
            f"PGL_ACQUIRE_TIMEOUT={ACQUIRE_TIMEOUT_SECONDS}",
            f"PGL_DEADLINE_MARGIN={DEADLINE_MARGIN_SECONDS}",
            f"PDB_EXPORT_LABEL={shlex.quote(PG_EXPORT_LABEL)}",
            f"PDB_EXPORT_RMEM={shlex.quote(PG_EXPORT_RMEM)}",
            f"PDB_EXPORT_PACE={shlex.quote(PG_EXPORT_PACE)}",
            f"PGL_DRIVER_SRC=$(cat <<'{HEREDOC_END}'",
        ]
    )
    return (header + "\n" + DRIVER_SCRIPT + HEREDOC_END + "\n)\n" + _FUNCTIONS + END_MARKER + "\n").encode()


_FUNCTIONS = r'''
# dbo_node_selector: the database pod's nodeSelector as JSON (its domain's pods share it), or its node by hostname.
dbo_node_selector() {
  dbo_k get pod "$DBO_POD" -o json | jq -ce \
    'if (.spec.nodeSelector // {}) != {} then .spec.nodeSelector elif .spec.nodeName then {"kubernetes.io/hostname": .spec.nodeName} else empty end' \
    || { echo "database pod $DBO_POD has no node to place a client next to" >&2; return 1; }
}
# pdb_export_job NAME SQL RATE DEADLINE APPNAME: the export as a batch Job in the database's namespace (v4 C6b 수정 2회차,
# the answer keys tell an export job or a data job): one pod on the node its domain runs on (dbo_node_selector), so
# the database sees a pod address like the services' and no export bytes cross another node. The pod runs psql and a
# reader that takes the rows at a fixed pace (a second's pause every RATE rows), the old in-pod pacing. A privileged
# init container first shrinks the pod's TCP receive buffer (PDB_EXPORT_RMEM, its own network namespace only), so the
# reader's pace holds the server back (104 k3s: 11 MB at 40 s nominal and 195 MB at 60 s ended on time; without the
# init container the 11 MB ended after 22 s). The Job ends itself: activeDeadlineSeconds DEADLINE, then the pod's psql closes
# its session; finished Jobs are deleted after ttlSecondsAfterFinished. The password reaches psql from the Secret.
pdb_export_job() {
  local image sel
  image="$(dbo_k get pod "$DBO_POD" -o 'jsonpath={.spec.containers[0].image}')" || return 1
  [[ -n "$image" ]] || { echo "database pod image is unknown" >&2; return 1; }
  sel="$(dbo_node_selector)" || return 1
  [[ "$3" =~ ^[1-9][0-9]*$ && "$4" =~ ^[1-9][0-9]*$ ]] || { echo "export rate and deadline must be numbers" >&2; return 1; }
  jq -n --arg name "$1" --arg sql "$2" --arg rate "$3" --argjson deadline "$4" --arg app "$5" --arg image "$image" \
    --argjson sel "$sel" --arg selector "$PDB_EXPORT_LABEL" --arg host "$DBO_PG_CLUSTER_SERVICE" --arg secret "$DBO_PG_SECRET" \
    --arg pace "$PDB_EXPORT_PACE" --arg rmem "$PDB_EXPORT_RMEM" '
    ($selector | split("=")) as [$lk, $lv] |
    def key($k): {name: $k, valueFrom: {secretKeyRef: {name: $secret, key: $k}}};
    {apiVersion: "batch/v1", kind: "Job", metadata: {name: $name, labels: {($lk): $lv}},
     spec: {backoffLimit: 0, activeDeadlineSeconds: $deadline, ttlSecondsAfterFinished: 300,
      template: {metadata: {labels: {($lk): $lv}},
       spec: {restartPolicy: "Never", terminationGracePeriodSeconds: 5, nodeSelector: $sel,
        initContainers: [{name: "net-tuning", image: $image, command: ["sysctl", "-w", ("net.ipv4.tcp_rmem=" + $rmem)],
                          securityContext: {privileged: true}}],
        containers: [{name: "export", image: $image,
         command: ["sh", "-c", "psql -X -q -c \"$SQL\" | awk -v r=\"$RATE\" \"$PACE\""],
         env: [{name: "PGHOST", value: $host}, {name: "PGAPPNAME", value: $app},
               {name: "SQL", value: $sql}, {name: "RATE", value: $rate}, {name: "PACE", value: $pace},
               (key("POSTGRES_USER") | .name = "PGUSER"), (key("POSTGRES_DB") | .name = "PGDATABASE"),
               (key("POSTGRES_PASSWORD") | .name = "PGPASSWORD")]}]}}}}' \
    | dbo_k apply -f - >/dev/null
}
pdb_export_name() { printf '%s-data-export-%s' "${DBO_NS##*-}" "$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"; }
pdb_export_left() { { dbo_k get jobs -l "$PDB_EXPORT_LABEL" -o name; dbo_k get pods -l "$PDB_EXPORT_LABEL" -o name; } 2>/dev/null | tr -d '\r'; }
# pdb_export_gone: no export Job or pod is left (one an earlier run lost waits for its deadline and TTL).
pdb_export_gone() { [[ -z "$(pdb_export_left)" ]] || { echo "an export job is still there: $(pdb_export_left | tr '\n' ' ')" >&2; return 1; }; }
# pdb_delete_exports: delete every export Job (and its pod) and wait until they are gone.
pdb_delete_exports() {
  local j
  for j in $(dbo_k get jobs -l "$PDB_EXPORT_LABEL" -o name 2>/dev/null); do
    dbo_k delete "$j" --cascade=foreground --ignore-not-found --wait=false >/dev/null || return 1
  done
  for _ in $(seq 1 60); do [[ -z "$(pdb_export_left)" ]] && return 0; sleep 1; done
  pdb_export_gone
}
# pgl_lock_sql SCHEMA TABLE SCOPE MODE KEYCOL KEYVAL: the statement that takes the lock.
pgl_lock_sql() {
  case "$3" in
    row) [[ -n "$5" && -n "$6" ]] || { echo "row scope needs key" >&2; return 2; }
         printf "SELECT %s FROM %s.%s WHERE %s='%s' FOR UPDATE;" "$5" "$1" "$2" "$5" "$6" ;;
    table) case "$4" in EXCLUSIVE|"ACCESS EXCLUSIVE"|"SHARE ROW EXCLUSIVE") : ;;
             *) echo "unsupported lock mode: $4" >&2; return 2 ;; esac
         printf 'LOCK TABLE %s.%s IN %s MODE;' "$1" "$2" "$4" ;;
    *) echo "unsupported lock scope: $3" >&2; return 2 ;;
  esac
}
# Probes are an operator's session (db_operator_remote) and are not recorded by pg_stat_statements.
pgl_q() { pdb "$1" "-c pg_stat_statements.track=none"; }
# pgl_target_ok SCHEMA TABLE SCOPE KEYCOL KEYVAL: the table exists (and the row, for row scope).
pgl_target_ok() {
  [[ "$(pgl_q "SELECT to_regclass('$1.$2') IS NOT NULL;")" == t ]] || { echo "lock target table $1.$2 is missing" >&2; return 1; }
  [[ "$3" != row ]] && return 0
  [[ "$(pgl_q "SELECT count(*) FROM $1.$2 WHERE $4='$5';")" == 1 ]] || { echo "lock target row is missing" >&2; return 1; }
}
# Client pods of this kind in the namespace (ours, or one an older run left).
pgl_pods() { dbo_k get pods -l "$PGL_SELECTOR" -o name 2>/dev/null | tr -d '\r'; }
pgl_no_pods() { [[ -z "$(pgl_pods)" ]] || { echo "a database client pod is still there: $(pgl_pods | tr '\n' ' ')" >&2; return 1; }; }
# pgl_pids DIR: pids of this host's attach driver for DIR, found by its command line.
pgl_pids() {
  local dir="$1" proc args=()
  for proc in /proc/[0-9]*; do
    mapfile -d '' -t args 2>/dev/null <"$proc/cmdline" || continue
    [[ "${args[*]}" == *"$dir/pg_pod_driver.sh"* ]] && printf '%s\n' "${proc##*/}"
  done
  return 0
}
pgl_alive() { [[ -n "$(pgl_pids "$1")" ]]; }
pgl_idle() { if pgl_alive "$1"; then echo "a PostgreSQL client session from $1 is still running" >&2; return 1; fi; }

# pgl_pod DIR NAME IDENTITY DEADLINE [ARG...]: create a client pod running psql (and ARG, if any) as its only
# process, as the image's postgres role through the in-cluster Service, and wait until it runs. Without ARG psql
# waits for statements on stdin (stdinOnce: closing the attach ends it). PGL_IMAGE, PGL_SERVICE, PGL_SECRET (a
# lock contract's image, service, secret) override the defaults: the database pod's own image, the application
# Service, postgres-secret.
pgl_pod() {
  local dir="$1" name="$2" identity="$3" deadline="$4"; shift 4
  local image="${PGL_IMAGE:-}" service="${PGL_SERVICE:-$DBO_PG_CLUSTER_SERVICE}" secret="${PGL_SECRET:-$DBO_PG_SECRET}"
  local args="" a sel
  [[ -n "$image" ]] || image="$(dbo_k get pod "$DBO_POD" -o 'jsonpath={.spec.containers[0].image}')" || return 1
  [[ -n "$image" ]] || { echo "database pod image is unknown" >&2; return 1; }
  # On the node the domain's pods run on (C6b review 2): elsewhere the application's name would come from another
  # domain's pod range.
  sel="$(dbo_node_selector)" || return 1
  for a in -X -q -At "$@"; do args+="${args:+, }$(jq -cn --arg v "$a" '$v')"; done
  mkdir -p -- "$dir"
  printf '%s\n' "$name" >>"$dir/pods"
  dbo_k apply -f - >/dev/null <<YAML || return 1
apiVersion: v1
kind: Pod
metadata:
  name: ${name}
  namespace: ${DBO_NS}
  labels:
    lucida.io/db-client: session
spec:
  restartPolicy: Never
  terminationGracePeriodSeconds: 1
  nodeSelector: ${sel}
  activeDeadlineSeconds: $(( deadline + PGL_DEADLINE_MARGIN ))
  containers:
    - name: ${PGL_CONTAINER}
      image: ${image}
      command: ["psql", ${args}]
      stdin: true
      stdinOnce: true
      env:
        - name: PGHOST
          value: "${service}"
        - name: PGAPPNAME
          value: "${identity}"
        - name: PGUSER
          valueFrom: {secretKeyRef: {name: ${secret}, key: POSTGRES_USER}}
        - name: PGDATABASE
          valueFrom: {secretKeyRef: {name: ${secret}, key: POSTGRES_DB}}
        - name: PGPASSWORD
          valueFrom: {secretKeyRef: {name: ${secret}, key: POSTGRES_PASSWORD}}
YAML
  dbo_k wait --for=jsonpath='{.status.phase}'=Running "pod/$name" --timeout=90s >/dev/null \
    || { echo "client pod $name did not start" >&2; return 1; }
}
pgl_name() { printf '%s-db-client-%s' "${DBO_NS##*-}" "$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"; }
# pgl_drive DIR POD DEADLINE: start the attach driver for the pod's psql.
pgl_drive() {
  local dir="$1"
  : >"$dir/out"; rm -f -- "$dir/in"; mkfifo -m 600 "$dir/in"
  printf '%s' "$PGL_DRIVER_SRC" >"$dir/pg_pod_driver.sh"
  DRIVER_DIR="$dir" DRIVER_NS="$DBO_NS" DRIVER_POD="$2" DRIVER_CONTAINER="$PGL_CONTAINER" \
    DRIVER_KUBECONFIG="$DBO_KUBECONFIG" DEADLINE="$3" \
    setsid nohup bash "$dir/pg_pod_driver.sh" >>"$dir/driver.log" 2>&1 </dev/null &
}
# pgl_send DIR LINE...: statements (or psql backslash commands) to the driven psql.
pgl_send() { local dir="$1"; shift; printf '%s\n' "$@" >"$dir/in"; }
# pgl_wait_line DIR TEXT SECONDS: wait until psql printed the line TEXT.
pgl_wait_line() {
  local i
  for ((i = 0; i < $3 * 2; i++)); do grep -qx -- "$2" "$1/out" 2>/dev/null && return 0; pgl_alive "$1" || break; sleep 0.5; done
  return 1
}
# pgl_stop DIR: stop this host's driver (it rolls back and quits psql). No database call; the pods are deleted
# by pgl_release. After 15 s the whole process group goes.
pgl_stop() {
  local dir="$1" pids p groups=""
  pids="$(pgl_pids "$dir")"
  [[ -z "$pids" ]] && return 0
  for p in $pids; do groups+=" $(awk '{print $5}' "/proc/$p/stat" 2>/dev/null)"; done
  kill -TERM $pids 2>/dev/null || true
  for _ in $(seq 1 15); do pgl_alive "$dir" || return 0; sleep 1; done
  for p in $groups; do [[ "$p" =~ ^[0-9]+$ ]] && kill -KILL -- "-$p" 2>/dev/null; done
  sleep 1
  if pgl_alive "$dir"; then echo "PostgreSQL client driver in $dir did not stop" >&2; return 1; fi
}
# pgl_released DIR: the backend the lock session reported is gone on the server (its locks with it).
pgl_released() {
  local bpid bstart
  [[ -s "$1/backend" ]] || return 0
  read -r bpid bstart <"$1/backend"
  [[ "$bpid" =~ ^[0-9]+$ ]] || { echo "unreadable lock session record in $1" >&2; return 1; }
  [[ "$(pgl_q "SELECT count(*) FROM pg_stat_activity WHERE pid = $bpid AND backend_start = '$bstart'::timestamptz;")" == 0 ]] \
    || { echo "PostgreSQL lock session backend $bpid is still on the server" >&2; return 1; }
}
# pgl_delete_pods DIR: our pods by name, then any client pod of this kind by label.
pgl_delete_pods() {
  local rc=0 p
  if [[ -s "$1/pods" ]]; then
    while IFS= read -r p; do [[ -n "$p" ]] && { dbo_k delete pod "$p" --now --ignore-not-found >/dev/null || rc=1; }; done <"$1/pods"
  fi
  for p in $(pgl_pods); do dbo_k delete "$p" --now --ignore-not-found >/dev/null || rc=1; done
  for _ in $(seq 1 30); do [[ -z "$(pgl_pods)" ]] && return "$rc"; sleep 1; done
  pgl_no_pods
}

# ---- the row or table lock (db.lock, timeline steps) ----
# pgl_check DIR SCHEMA TABLE SCOPE KEYCOL KEYVAL
pgl_check() { pgl_idle "$1" && [[ ! -s "$1/backend" ]] && pgl_no_pods && pgl_target_ok "$2" "$3" "$4" "$5" "$6"; }
# pgl_apply DIR SCHEMA TABLE SCOPE MODE KEYCOL KEYVAL HOLD IDENTITY: a client pod's psql opens a transaction,
# takes the lock and idles; the driver rolls it back after HOLD seconds.
pgl_apply() {
  local dir="$1" sql name bpid bstart
  sql="$(pgl_lock_sql "$2" "$3" "$4" "$5" "$6" "$7")" || return 1
  pdb_env || return 1
  name="$(pgl_name)"
  pgl_pod "$dir" "$name" "$9" "$8" || { pgl_delete_pods "$dir" || true; return 1; }
  pgl_drive "$dir" "$name" "$8"
  # The server sees BEGIN, SELECT pg_backend_pid(), the lock statement (the session's last query, as the old
  # client sent); "locked" is psql's own \echo, never sent.
  pgl_send "$dir" 'BEGIN;' 'SELECT pg_backend_pid();' "$sql" '\echo locked'
  if ! pgl_wait_line "$dir" locked $((PGL_ACQUIRE_TIMEOUT + 30)); then
    echo "PostgreSQL lock session did not report the lock: $(tail -c 400 "$dir/out" 2>/dev/null | tr '\n' ' ')" >&2
    pgl_stop "$dir" || true; pgl_delete_pods "$dir" || true
    return 1
  fi
  # The first all-digits line is the pid; a row lock then prints its key, which is not. Confirm on the server by
  # that pid (never by session name, which is the app's) and record its start so a reused pid cannot pass.
  bpid="$(grep -m1 -xE '[0-9]+' "$dir/out")"
  bstart="$(pgl_q "SELECT backend_start FROM pg_stat_activity WHERE pid = ${bpid:-0} AND state = 'idle in transaction';")"
  if [[ -z "$bstart" ]]; then
    echo "the lock session is not idle in transaction on the server" >&2
    pgl_stop "$dir" || true; pgl_delete_pods "$dir" || true
    return 1
  fi
  printf '%s %s\n' "$bpid" "$bstart" >"$dir/backend"
}
# pgl_release DIR: driver first (no API or database), then the pods, then the server-side check. The record
# stays until all three pass.
pgl_release() {
  local rc=0
  pgl_stop "$1" || rc=1
  pgl_delete_pods "$1" || rc=1
  pdb_env || return 1
  pgl_released "$1" || rc=1
  if [[ "$rc" == 0 ]]; then rm -rf -- "$1"; fi
  return "$rc"
}
# pgl_recover DIR SCHEMA TABLE SCOPE KEYCOL KEYVAL
pgl_recover() { pgl_idle "$1" && pgl_released "$1" && pgl_no_pods && pgl_target_ok "$2" "$3" "$4" "$5" "$6"; }
'''
