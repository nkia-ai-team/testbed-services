"""Bash side of the PostgreSQL lock injection held from the runner container (v4 C6b).

db.lock (F01-R, F06-H)와 timeline 실행기 셋(timeline.multi 의 pg_lock 단계 F15-G,
timeline_dual_fault F15-T1, timeline_lock_mock F15-H, F15-T2)이 같은 잠금을 건다. 이 모듈이
함수 묶음 하나를 만들고 넷이 끼워 쓴다(Oracle 쪽 ``oracle_lock_remote`` 와 같은 구성).

옛 방식은 commerce 네임스페이스에 단명 클라이언트 파드(postgres:16-alpine)를 띄워 그 안의
psql 이 잠금을 쥐었다. 2026-08-23 F01-R 녹화에 남은 흔적: KCM 이벤트(파드 생성, 배정),
kubelet 과 containerd syslog, ``SQL_PRE`` 가 든 명령줄, ``sleep 600`` 프로세스. 앞의 정리도
DB 파드에 exec 해 psql 로 확인했다.

이제 잠금 세션은 러너 컨테이너의 psql 이 리스너(NodePort)로 붙어 쥔다. 세션 신원은 계약의
``client_identity`` (실 앱과 같은 application_name)이고, 잠금 유지는 클라이언트가 트랜잭션을
연 채 쉬는 것(서버에는 idle in transaction, ClientRead)이다. 클러스터에는 파드도 exec 도 생기지
않는다. 바뀌는 칸은 하나다: 서버가 보는 client_addr 이 앱 파드 주소가 아니라 NodePort 로
들어온 연결이 받는 노드 주소다(DPM 과 러너 관측 세션과 같은 주소, 결과 문서 확인 필요).

러너가 FIN 없이 사라지면(tb-ops 정지, 망 단절) 서버 쪽 끊김 감지가 세션을 끝내 롤백한다.
세션 시작 옵션으로 그 세션에만 건다(서버 설정 변경 없음): TCP keepalive 60초 + 10초 x 6,
그리고 보유 시간 + 120초의 idle_in_transaction_session_timeout. 사전검사는 서버가 이 옵션을
받아들이는지(세션의 tcp_keepalives_idle 이 60 인지) 확인한다.
"""
from __future__ import annotations

HEREDOC_END = "PG_LOCK_SESSION_SH"
START_MARKER = "# ---- PostgreSQL lock session from the runner (pg_lock_remote.py) ----"
END_MARKER = "# ---- end of PostgreSQL lock session ----"
LEGACY_CLIENT_SELECTOR = "lucida.io/db-client=session"
# 잠금을 얻으려고 기다리는 상한(초). 앱 트랜잭션이 행을 잠깐 쥐고 있어도 기다렸다 잡는다.
ACQUIRE_TIMEOUT_SECONDS = 60
# 보유 시간이 끝나도 롤백하지 못한(러너가 사라진) 세션을 서버가 끝내는 여유(초).
IDLE_TIMEOUT_MARGIN_SECONDS = 120

# The session itself: a local bash that drives one psql through a coprocess. It runs on the
# runner (never in a pod), in its own process group (setsid), so pgl_hold_stop can end the
# psql child even if the script is killed.
SESSION_SCRIPT = r'''#!/usr/bin/env bash
# PostgreSQL lock session (pg_lock_remote.py): open a transaction, take LOCK_SQL, report the
# backend pid in READY_FILE, idle for HOLD seconds, roll back. SIGTERM rolls back at once.
# The server sees what the old in-cluster client sent: BEGIN, SELECT pg_backend_pid(), the lock
# statement (which stays as the session's last query). "locked" is psql's own \echo, never sent.
set -u
stop=0
trap 'stop=1' TERM INT
coproc PSQL { exec psql -X -q -At -v ON_ERROR_STOP=1 2>&1; }
to="${PSQL[1]}"; from="${PSQL[0]}"; psql_pid="$PSQL_PID"
printf '%s\n' 'BEGIN;' 'SELECT pg_backend_pid();' "$LOCK_SQL" '\echo locked' >&"$to"
backend=""; line=""
while IFS= read -r -t 120 line <&"$from"; do
  # The first all-digits line is the pid; a row lock then prints its key, which is not.
  if [[ -z "$backend" && "$line" =~ ^[0-9]+$ ]]; then backend="$line"; continue; fi
  [[ "$line" == locked ]] && break
  [[ -n "$backend" ]] || printf '%s\n' "$line" >&2
done
if [[ "$line" != locked || -z "$backend" ]]; then
  echo "lock was not taken" >&2
  kill "$psql_pid" 2>/dev/null
  exit 1
fi
printf '%s\n' "$backend" >"$READY_FILE"
end=$((SECONDS + HOLD))
while (( !stop && SECONDS < end )); do sleep 1 & wait $!; done
printf '%s\n' 'ROLLBACK;' '\q' >&"$to"
for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$psql_pid" 2>/dev/null || exit 0; sleep 1; done
kill "$psql_pid" 2>/dev/null
exit 0
'''


def bash_functions() -> bytes:
    """pgl_* functions. Needs db_operator_remote's functions (db_target, pdb_env, pdb) first."""
    if any(line.strip() == HEREDOC_END for line in SESSION_SCRIPT.splitlines()):
        raise ValueError("session script collides with the heredoc terminator")
    header = "\n".join(
        [
            START_MARKER,
            f"PGL_LEGACY_SELECTOR='{LEGACY_CLIENT_SELECTOR}'",
            f"PGL_ACQUIRE_TIMEOUT={ACQUIRE_TIMEOUT_SECONDS}",
            f"PGL_IDLE_MARGIN={IDLE_TIMEOUT_MARGIN_SECONDS}",
            f"PGL_SESSION_SRC=$(cat <<'{HEREDOC_END}'",
        ]
    )
    return (header + "\n" + SESSION_SCRIPT + HEREDOC_END + "\n)\n" + _FUNCTIONS + END_MARKER + "\n").encode()


_FUNCTIONS = r'''
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
pgl_q() { pdb "$1" "-c pg_stat_statements.track=none"; }
# pgl_target_ok SCHEMA TABLE SCOPE KEYCOL KEYVAL: the table exists (and the row, for row scope).
pgl_target_ok() {
  [[ "$(pgl_q "SELECT to_regclass('$1.$2') IS NOT NULL;")" == t ]] || { echo "lock target table $1.$2 is missing" >&2; return 1; }
  [[ "$3" != row ]] && return 0
  [[ "$(pgl_q "SELECT count(*) FROM $1.$2 WHERE $4='$5';")" == 1 ]] || { echo "lock target row is missing" >&2; return 1; }
}
# pgl_keepalive_ok: the server applies the per-session dead-peer settings this injection relies on.
pgl_keepalive_ok() {
  [[ "$(pdb "SHOW tcp_keepalives_idle;" "-c pg_stat_statements.track=none $PDB_KEEPALIVE")" == 60 ]] \
    || { echo "server does not apply per-session TCP keepalive; refusing a lock that could outlive the runner" >&2; return 1; }
}
# The client pods of the old method (label selector), from a run that predates this code.
pgl_legacy() { dbo_k get pods -l "$PGL_LEGACY_SELECTOR" -o name 2>/dev/null | tr -d '\r'; }
pgl_legacy_none() { [[ -z "$(pgl_legacy)" ]] || { echo "an old in-cluster lock client pod is still there" >&2; return 1; }; }
# pgl_pids DIR: pids of this host's lock session for DIR, found by its command line.
pgl_pids() {
  local dir="$1" proc args=()
  for proc in /proc/[0-9]*; do
    mapfile -d '' -t args 2>/dev/null <"$proc/cmdline" || continue
    [[ "${args[*]}" == *"$dir/pg_lock_session.sh"* ]] && printf '%s\n' "${proc##*/}"
  done
  return 0
}
pgl_alive() { [[ -n "$(pgl_pids "$1")" ]]; }
pgl_idle() { if pgl_alive "$1"; then echo "a PostgreSQL lock session from $1 is still running" >&2; return 1; fi; }
# pgl_hold_start DIR LOCK_SQL HOLD IDENTITY: start the session and wait until it holds the lock.
pgl_hold_start() {
  local dir="$1" lock_sql="$2" hold="$3" identity="$4" pid
  pdb_env || return 1
  mkdir -p -- "$dir"
  printf '%s' "$PGL_SESSION_SRC" >"$dir/pg_lock_session.sh"
  rm -f -- "$dir/ready" "$dir/backend"
  PGAPPNAME="$identity" \
    PGOPTIONS="$PDB_KEEPALIVE -c lock_timeout=${PGL_ACQUIRE_TIMEOUT}s -c idle_in_transaction_session_timeout=$(( (hold + PGL_IDLE_MARGIN) * 1000 ))" \
    LOCK_SQL="$lock_sql" HOLD="$hold" READY_FILE="$dir/ready" \
    setsid nohup bash "$dir/pg_lock_session.sh" >"$dir/log" 2>&1 </dev/null &
  pid="$!"
  for _ in $(seq 1 $((PGL_ACQUIRE_TIMEOUT + 30))); do
    [[ -s "$dir/ready" ]] && break
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  if [[ ! -s "$dir/ready" ]]; then
    echo "PostgreSQL lock session did not report the lock: $(tail -c 400 "$dir/log" 2>/dev/null | tr '\n' ' ')" >&2
    pgl_hold_stop "$dir" || true
    return 1
  fi
  # Confirm on the server, keyed by backend pid (never by session name, which is the app's), and
  # record the backend's start so a later check cannot mistake a reused pid for it.
  local bpid bstart
  read -r bpid <"$dir/ready"
  [[ "$bpid" =~ ^[0-9]+$ ]] || { echo "unreadable backend pid" >&2; pgl_hold_stop "$dir" || true; return 1; }
  bstart="$(pgl_q "SELECT backend_start FROM pg_stat_activity WHERE pid = $bpid AND state = 'idle in transaction';")"
  [[ -n "$bstart" ]] || { echo "the lock session is not idle in transaction on the server" >&2; pgl_hold_stop "$dir" || true; return 1; }
  printf '%s %s\n' "$bpid" "$bstart" >"$dir/backend"
}
# pgl_hold_stop DIR: stop this host's session. No API or database call, so every path calls it
# first. SIGTERM rolls back; after 15 s the whole process group (psql included) is killed and the
# server rolls back when the socket closes.
pgl_hold_stop() {
  local dir="$1" pids p groups=""
  pids="$(pgl_pids "$dir")"
  [[ -z "$pids" ]] && return 0
  for p in $pids; do
    groups+=" $(awk '{print $5}' "/proc/$p/stat" 2>/dev/null)"
  done
  kill -TERM $pids 2>/dev/null || true
  for _ in $(seq 1 15); do pgl_alive "$dir" || return 0; sleep 1; done
  for p in $groups; do [[ "$p" =~ ^[0-9]+$ ]] && kill -KILL -- "-$p" 2>/dev/null; done
  sleep 1
  if pgl_alive "$dir"; then echo "PostgreSQL lock session in $dir did not stop" >&2; return 1; fi
}
# pgl_released DIR: the backend the session reported is gone on the server (its locks with it).
pgl_released() {
  local bpid bstart
  [[ -s "$1/backend" ]] || return 0
  read -r bpid bstart <"$1/backend"
  [[ "$bpid" =~ ^[0-9]+$ ]] || { echo "unreadable lock session record in $1" >&2; return 1; }
  [[ "$(pgl_q "SELECT count(*) FROM pg_stat_activity WHERE pid = $bpid AND backend_start = '$bstart'::timestamptz;")" == 0 ]] \
    || { echo "PostgreSQL lock session backend $bpid is still on the server" >&2; return 1; }
}

# High-level verbs shared by the executors. DIR is the runner-local state for one lock.
# pgl_check DIR SCHEMA TABLE SCOPE KEYCOL KEYVAL
pgl_check() { pgl_idle "$1" && [[ ! -s "$1/backend" ]] && pgl_legacy_none && pgl_target_ok "$2" "$3" "$4" "$5" "$6" && pgl_keepalive_ok; }
# pgl_apply DIR SCHEMA TABLE SCOPE MODE KEYCOL KEYVAL HOLD IDENTITY
pgl_apply() {
  local sql
  sql="$(pgl_lock_sql "$2" "$3" "$4" "$5" "$6" "$7")" || return 1
  pgl_hold_start "$1" "$sql" "$8" "$9"
}
# pgl_release DIR: local stop first, then the server-side check; old client pods (from a run that
# predates this code) are deleted by label as the old cleanup did. The record stays until released.
pgl_release() {
  local rc=0 p
  pgl_hold_stop "$1" || rc=1
  pdb_env || return 1
  pgl_released "$1" || rc=1
  for p in $(pgl_legacy); do dbo_k delete "$p" --now --ignore-not-found >/dev/null || rc=1; done
  pgl_legacy_none || rc=1
  if [[ "$rc" == 0 ]]; then rm -rf -- "$1"; fi
  return "$rc"
}
# pgl_recover DIR SCHEMA TABLE SCOPE KEYCOL KEYVAL
pgl_recover() { pgl_idle "$1" && pgl_released "$1" && pgl_legacy_none && pgl_target_ok "$2" "$3" "$4" "$5" "$6"; }
'''
