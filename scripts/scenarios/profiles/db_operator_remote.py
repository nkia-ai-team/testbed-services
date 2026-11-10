"""Bash side of the operator-shaped database sessions (v4 C6b).

DB 를 바꾸는 실행기들이 DB 파드에 exec 하지 않고 러너 컨테이너에서 리스너(NodePort)로 붙게
하는 함수 묶음이다. ``oracle_lock_remote`` (v4 C6, Oracle 행 잠금)와 같은 방식을 Oracle,
MySQL, PostgreSQL 의 일회성 변경과 확인에 넓힌다.

옛 방식이 119 로 보낸 흔적(2026-09 녹화 실측, C3-6 결과 문서 확인 필요 5, 6):

    process_meta  DB 노드의 runc exec 와 sqlplus, mysql, psql, sh, df 프로세스
    dpm_session   Oracle: schema=SYS, program=sqlplus@testbed-oracle-0, machine=testbed-oracle-0
                  (DB 서버 자신이 SYS 로 붙는 짧은 세션. 운영자는 작업 호스트에서 붙는다)

함수가 kubectl 로 하는 일은 조회뿐이다: DB 파드가 있는 노드 주소, 외부 Service 의
NodePort, 계정 비밀(Secret), 그리고 디스크 여유는 kubelet 요약 API(노드 파일시스템). DB
파드에는 exec 하지 않는다.

세션의 신원
  Oracle      운영 도구(SQLcl, ops-bastion, dba, ``oracle_lock_remote`` 의 상수). 계정은 역할로 고른다:
              dba = SYSTEM(관리 작업, 사전 조회), app = 앱 스키마 계정, mon = DPM 조회 계정
  MySQL       root, 접속 속성은 mysql 명령줄 도구 모양. app = 앱 계정(내보내기 작업)
  PostgreSQL  러너 이미지의 psql(application_name 기본값 psql), 앱 역할(이미지가 만든 관리자 역할)
어느 값도 시나리오를 가리키지 않는다(기준서 G6).

러너가 FIN 없이 사라질 때: 일회성 문장은 서버가 끝까지 하거나 되돌린다. 오래 사는 세션
(백그라운드 내보내기, 마이그레이션, 잠금)은 서버 쪽 끊김 감지가 정리한다. PostgreSQL 은
세션 시작 옵션(``PDB_KEEPALIVE``: TCP keepalive 와 tcp_user_timeout)으로 그 세션에만 걸고, Oracle
은 서버 DCD(C6), MySQL 은 서버 기본 net_write_timeout(60초)이 쓰는 중인 세션을 끊는다.
"""
from __future__ import annotations

import shlex
from pathlib import Path

import oracle_lock_remote

HERE = Path(__file__).resolve().parent
CLIENT_SOURCE_PATH = HERE / "db_operator_session.py"
HEREDOC_END = "DB_OPERATOR_SESSION_PY"
START_MARKER = "# ---- operator database sessions (db_operator_remote.py) ----"
END_MARKER = "# ---- end of operator database sessions ----"

KUBECONFIG = "/root/tb-kubeconfig"
PYTHON = oracle_lock_remote.PYTHON

ORACLE_SERVICE = oracle_lock_remote.NODEPORT_SERVICE
ORACLE_PORT = oracle_lock_remote.LISTENER_PORT
ORACLE_SERVICE_NAME = oracle_lock_remote.SERVICE_NAME
ORACLE_SECRET = oracle_lock_remote.SECRET
# 역할 -> (사용자 키 또는 고정 이름, 비밀번호 키). gvenzl 이미지는 ORACLE_PASSWORD 를 SYS, SYSTEM
# 의 비밀번호로 쓴다. SYSTEM 은 DBA 역할로 계정, 프로파일, 남의 스키마 DDL, v$ 조회,
# DBMS_SHARED_POOL 을 다 할 수 있다(11-10 Oracle Free 23 실측). 원격 SYSDBA 는 쓰지 않는다.
ORACLE_ROLES = {
    "dba": ("=SYSTEM", "ORACLE_PASSWORD"),
    "app": ("APP_USER", "APP_USER_PASSWORD"),
    "mon": ("MON_USER", "MON_PASSWORD"),
}

MYSQL_SERVICE = "testbed-mysql-external"
MYSQL_PORT = 3306
MYSQL_SECRET = "mysql-secret"
MYSQL_ROLES = {
    "root": ("=root", "MYSQL_ROOT_PASSWORD"),
    "app": ("MYSQL_USER", "MYSQL_PASSWORD"),
}

POSTGRES_SERVICE = "testbed-postgres-external"
POSTGRES_PORT = 5432
POSTGRES_SECRET = "postgres-secret"
# The Service the applications use inside the cluster (client pods connect through it, pg_lock_remote).
POSTGRES_CLUSTER_SERVICE = "testbed-postgres"

# 오래 사는 PostgreSQL 세션에만 거는 서버 쪽 끊김 감지(세션 시작 옵션이라 문장이 아니다).
# 쉬는 연결은 keepalive 60초 + 10초 x 6 이면 끊고, 보낸 데이터가 120초 동안 확인되지 않으면 끊는다.
PG_KEEPALIVE = (
    "-c tcp_keepalives_idle=60 -c tcp_keepalives_interval=10 -c tcp_keepalives_count=6 "
    "-c tcp_user_timeout=120000"
)


def client_source() -> str:
    source = CLIENT_SOURCE_PATH.read_text(encoding="utf-8")
    if any(line.strip() == HEREDOC_END for line in source.splitlines()):
        raise ValueError("client source collides with the heredoc terminator")
    return source


def _role_table(name: str, roles: dict[str, tuple[str, str]]) -> str:
    q = shlex.quote
    cases = "\n".join(f"    {role}) printf '%s %s' {q(user)} {q(pw)} ;;" for role, (user, pw) in roles.items())
    return f"{name}() {{\n  case \"$1\" in\n{cases}\n    *) return 1 ;;\n  esac\n}}\n"


def bash_functions() -> bytes:
    """Bash function definitions (db_*, odb, mdb, pdb, node_fs) with the client embedded."""
    q = shlex.quote
    header = "\n".join(
        [
            START_MARKER,
            f'dbo_py="${{DB_OPERATOR_PYTHON:-{PYTHON}}}"',
            f"DBO_KUBECONFIG={q(KUBECONFIG)}",
            f"DBO_ORACLE_SERVICE={q(ORACLE_SERVICE)} DBO_ORACLE_PORT={ORACLE_PORT} DBO_ORACLE_PDB={q(ORACLE_SERVICE_NAME)}",
            f"DBO_ORACLE_SECRET={q(ORACLE_SECRET)}",
            f"DBO_MYSQL_SERVICE={q(MYSQL_SERVICE)} DBO_MYSQL_PORT={MYSQL_PORT} DBO_MYSQL_SECRET={q(MYSQL_SECRET)}",
            f"DBO_PG_SERVICE={q(POSTGRES_SERVICE)} DBO_PG_PORT={POSTGRES_PORT} DBO_PG_SECRET={q(POSTGRES_SECRET)}",
            f"DBO_PG_CLUSTER_SERVICE={q(POSTGRES_CLUSTER_SERVICE)}",
            f"PDB_KEEPALIVE={q(PG_KEEPALIVE)}",
            f"ORA_ENTRY_NODE_JQ={q(oracle_lock_remote.ENTRY_NODE_JQ)}",
            f"export CLIENT_PROGRAM={q(oracle_lock_remote.CLIENT_PROGRAM)}",
            f"export CLIENT_MACHINE={q(oracle_lock_remote.CLIENT_MACHINE)}",
            f"export CLIENT_OSUSER={q(oracle_lock_remote.CLIENT_OSUSER)}",
            f"export CLIENT_DRIVER={q(oracle_lock_remote.CLIENT_DRIVER)}",
            f"DBO_SRC=$(cat <<'{HEREDOC_END}'",
        ]
    )
    roles = _role_table("dbo_oracle_role", ORACLE_ROLES) + _role_table("dbo_mysql_role", MYSQL_ROLES)
    return (header + "\n" + client_source() + HEREDOC_END + "\n)\n" + roles + _FUNCTIONS + END_MARKER + "\n").encode()


_FUNCTIONS = r'''
# db_target NS POD: the database pod every later call addresses (namespace and pod name).
db_target() { DBO_NS="$1"; DBO_POD="$2"; DBO_HOST=""; }
dbo_k() { kubectl --kubeconfig "$DBO_KUBECONFIG" -n "$DBO_NS" "$@"; }
# dbo_endpoint SERVICE PORT: "<node address> <node port>" of the database pod's listener. Cached.
dbo_endpoint() {
  local port
  [[ -n "${DBO_NS:-}" && -n "${DBO_POD:-}" ]] || { echo "db_target was not called" >&2; return 1; }
  if [[ -z "${DBO_HOST:-}" ]]; then
    # Through a worker that does not run the database pod and is not the control plane (ENTRY_NODE_JQ), so the
    # database sees neither the DPM collector's address nor the runner load's.
    DBO_HOST="$(ora_entry_node "$DBO_NS" "$DBO_POD")" || { DBO_HOST=""; return 1; }
  fi
  port="$(dbo_k get svc "$1" -o "jsonpath={.spec.ports[?(@.port==$2)].nodePort}")" || return 1
  [[ "$port" =~ ^[0-9]+$ ]] || { echo "service $1 has no node port for $2" >&2; return 1; }
  printf '%s %s' "$DBO_HOST" "$port"
}
# ora_entry_node NS DB_POD: the InternalIP of the node network sessions enter through (same function as in
# oracle_lock_remote; defined here too so a body that carries only these functions has it).
ora_entry_node() {
  local db_node
  db_node="$(kubectl --kubeconfig "$DBO_KUBECONFIG" -n "$1" get pod "$2" -o 'jsonpath={.spec.nodeName}')" || return 1
  [[ -n "$db_node" ]] || { echo "database pod $2 has no node" >&2; return 1; }
  kubectl --kubeconfig "$DBO_KUBECONFIG" get nodes -o json | jq -r --arg db "$db_node" "$ORA_ENTRY_NODE_JQ" \
    | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' || { echo "no worker node without $db_node to enter through" >&2; return 1; }
}
# dbo_secret SECRET KEY: one decoded value. A key spelled "=name" is that literal name.
dbo_secret() {
  if [[ "$2" == =* ]]; then printf '%s' "${2#=}"; return 0; fi
  local v
  v="$(dbo_k get secret "$1" -o "jsonpath={.data.$2}" | base64 -d)" || return 1
  [[ -n "$v" ]] || { echo "secret $1 has no $2" >&2; return 1; }
  printf '%s' "$v"
}
dbo_driver() { "$dbo_py" -c "import $1" 2>/dev/null </dev/null || { echo "runner image lacks python $1" >&2; return 1; }; }

# The *_env functions read the address and the account once and keep them in shell variables (not
# exported, except PostgreSQL's PG* which a background psql needs). Call them at the top of the body:
# a call made inside $( ) cannot keep what it read, and every SQL call would ask the API again.

# odb_env ROLE / odb ROLE: run the SQL*Plus-style script on stdin as ROLE (dba|app|mon) through the
# Oracle listener.
odb_env() {
  local role="$1" keys ep
  keys="$(dbo_oracle_role "$role")" || { echo "unknown oracle role $role" >&2; return 2; }
  [[ "${ODB_ROLE:-}" == "$role" ]] && return 0
  dbo_driver oracledb || return 1
  ep="$(dbo_endpoint "$DBO_ORACLE_SERVICE" "$DBO_ORACLE_PORT")" || return 1
  ODB_DSN="${ep% *}:${ep#* }/${DBO_ORACLE_PDB}"
  ODB_USER="$(dbo_secret "$DBO_ORACLE_SECRET" "${keys% *}")" || return 1
  ODB_PASSWORD="$(dbo_secret "$DBO_ORACLE_SECRET" "${keys#* }")" || return 1
  ODB_ROLE="$role"
}
odb() {
  odb_env "$1" || return 1
  ODB_DSN="$ODB_DSN" ODB_USER="$ODB_USER" ODB_PASSWORD="$ODB_PASSWORD" "$dbo_py" -c "$DBO_SRC" oracle
}

# mdb_as ROLE DB SQL: one `mysql -N -B -e SQL` through the MySQL listener as ROLE (root|app).
# mdb DB SQL: the same as root. mdb_stream_as ROLE DB SQL: rows as they arrive (mysql --quick).
# Each role keeps its own variables (MDB_<ROLE>_*), so root probes and an app-account export can
# both be read once at the top.
mdb_env() {
  local role="$1" keys ep
  keys="$(dbo_mysql_role "$role")" || { echo "unknown mysql role $role" >&2; return 2; }
  local v="MDB_${role^^}_USER"
  [[ -n "${!v:-}" ]] && return 0
  dbo_driver pymysql || return 1
  ep="$(dbo_endpoint "$DBO_MYSQL_SERVICE" "$DBO_MYSQL_PORT")" || return 1
  MDB_HOST="${ep% *}"; MDB_PORT="${ep#* }"
  local user password
  user="$(dbo_secret "$DBO_MYSQL_SECRET" "${keys% *}")" || return 1
  password="$(dbo_secret "$DBO_MYSQL_SECRET" "${keys#* }")" || return 1
  printf -v "MDB_${role^^}_USER" '%s' "$user"
  printf -v "MDB_${role^^}_PASSWORD" '%s' "$password"
}
# mdb_run ROLE VERB DB SQL: the client with one role's account in its environment only.
mdb_run() {
  mdb_env "$1" || return 1
  local u="MDB_${1^^}_USER" p="MDB_${1^^}_PASSWORD"
  MDB_HOST="$MDB_HOST" MDB_PORT="$MDB_PORT" MDB_USER="${!u}" MDB_PASSWORD="${!p}" DB="$3" SQL="$4" \
    "$dbo_py" -c "$DBO_SRC" "$2" </dev/null
}
mdb_as() { mdb_run "$1" mysql "$2" "$3"; }
mdb() { mdb_run root mysql "$1" "$2"; }
mdb_stream_as() { mdb_run "$1" mysql-stream "$2" "$3"; }

# pdb SQL [PGOPTIONS]: one `psql -At -c SQL` through the PostgreSQL listener; first stderr line to stderr.
# The connection settings are exported once (PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE), so a
# background psql started by the caller connects the same way.
pdb_env() {
  local ep
  [[ -n "${PGHOST:-}" && "${PDB_FOR:-}" == "$DBO_NS/$DBO_POD" ]] && return 0
  command -v psql >/dev/null || { echo "runner image lacks psql" >&2; return 1; }
  ep="$(dbo_endpoint "$DBO_PG_SERVICE" "$DBO_PG_PORT")" || return 1
  PGHOST="${ep% *}"; PGPORT="${ep#* }"
  PGUSER="$(dbo_secret "$DBO_PG_SECRET" POSTGRES_USER)" || return 1
  PGPASSWORD="$(dbo_secret "$DBO_PG_SECRET" POSTGRES_PASSWORD)" || return 1
  PGDATABASE="$(dbo_secret "$DBO_PG_SECRET" POSTGRES_DB)" || return 1
  PGCONNECT_TIMEOUT=10
  export PGHOST PGPORT PGUSER PGPASSWORD PGDATABASE PGCONNECT_TIMEOUT
  PDB_FOR="$DBO_NS/$DBO_POD"
}
pdb() {
  pdb_env || return 1
  local e rc
  e="$(mktemp)"
  PGOPTIONS="${2:-}" psql -X -At -v ON_ERROR_STOP=1 -c "$1" </dev/null 2>"$e"; rc=$?
  head -n 1 "$e" >&2; rm -f -- "$e"
  return "$rc"
}
# pdb_pod SQL: the statement through psql inside the database pod over its local socket (trust, not a password
# login), the one administrator path an expired password or a narrowed host rule cannot lock out (the image's
# only administrator role is the role the services log in with). The exec starts psql alone, as a database
# administrator on the host would; the statement and the setting that keeps it out of pg_stat_statements travel
# on its stdin, and the user and database names are the Secret's. The first error line goes to stderr.
pdb_pod() {
  local u d e rc
  u="$(dbo_secret "$DBO_PG_SECRET" POSTGRES_USER)" || return 1
  d="$(dbo_secret "$DBO_PG_SECRET" POSTGRES_DB)" || return 1
  e="$(mktemp)"
  printf 'SET pg_stat_statements.track = none;\n%s\n' "$1" \
    | dbo_k exec -i "$DBO_POD" -- psql -X -q -At -v ON_ERROR_STOP=1 -U "$u" -d "$d" 2>"$e"; rc=$?
  head -n 1 "$e" >&2; rm -f -- "$e"
  return "$rc"
}

# Background sessions: a statement that must outlive the body (run holds the runner's coordinator lock
# under a 30 s lease) runs in a client process of its own on the runner, in a new session (setsid) so a
# timeout that kills the body's process group does not end it, with its output in LOG. The old way ran
# the same client inside the database pod with nohup.
# dbo_run_dir NAME: the directory for one body's background logs, on the runner (never in the pod). The
# spawn functions create it; the body's cleanup removes it.
dbo_run_dir() { printf '%s' "${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}/$1"; }
DBO_PACE='NR % r == 0 { system("sleep 1") }'
# pdb_spawn SQL LOG APPNAME: one PostgreSQL statement in the background, with server-side dead-peer
# detection on its connection (PDB_KEEPALIVE) so it cannot outlive a runner that vanished.
pdb_spawn() {
  mkdir -p -- "$(dirname -- "$2")" || return 1
  pdb_env || return 1
  PGAPPNAME="$3" PGOPTIONS="$PDB_KEEPALIVE" SQL="$1" \
    setsid nohup bash -c 'psql -X -q -c "$SQL"' </dev/null >"$2" 2>&1 &
  printf '%s\n' "$!" >"$2.pgid"
}
# pdb_spawn_paced SQL RATE LOG APPNAME: an export (COPY ... TO STDOUT) taken by a reader that pauses a
# second every RATE rows, in the background. psql reaches the server through the client's relay (pg-relay:
# a Unix socket in the log's directory, then TCP with a small receive buffer), so the server keeps waiting
# on the reader as it did with psql inside the pod; see PACED_RCVBUF in db_operator_session.py.
pdb_spawn_paced() {
  local dir; dir="$(dirname -- "$3")"
  mkdir -p -- "$dir" || return 1
  pdb_env || return 1
  PG_UPSTREAM_HOST="$PGHOST" PG_UPSTREAM_PORT="$PGPORT" RELAY_DIR="$dir" DBO_PY="$dbo_py" DBO_SRC="$DBO_SRC" \
    PGAPPNAME="$4" PGOPTIONS="$PDB_KEEPALIVE" SQL="$1" RATE="$2" DBO_PACE="$DBO_PACE" \
    setsid nohup bash -c '
      "$DBO_PY" -c "$DBO_SRC" pg-relay "$RELAY_DIR" "$PGPORT" &
      for _ in $(seq 1 50); do [[ -S "$RELAY_DIR/.s.PGSQL.$PGPORT" ]] && break; sleep 0.1; done
      PGHOST="$RELAY_DIR" psql -X -q -c "$SQL" | awk -v r="$RATE" "$DBO_PACE"
      wait' </dev/null >"$3" 2>&1 &
  printf '%s\n' "$!" >"$3.pgid"
}

# mdb_pod_spawn ROLE DB SQL LOG [RATE]: one mysql client inside the database pod, the process an operator runs on
# the database host (v4 C6b 수정 1회차, user decision Q8, for the answer keys that tell root@localhost and
# fooddelivery@localhost sessions). The exec starts `mysql` itself, no shell: the pod shows that
# one process, over the local socket (user@localhost). The password reaches it on stdin as an option file (never on
# a command line). The runner side runs in its own process group, recorded in LOG.pgid for dbo_reap.
#   Without PACED (a migration): `kubectl exec -i`; its output is a line or two.
#   With PACED (an export): the rows come back to the runner through the client's pod-exec, which reads only as
#   fast as it is told (kubectl exec would take the whole stream into memory at once). The stream path between
#   the pod and the runner still holds about 34 MB (k3s 1.31), so mdb_pod_pace sets a byte rate that makes the
#   server finish when the export should; until then the reader waits. The old pacing was an `| awk` inside the
#   pod; nothing but the client runs there now.
mdb_pod_spawn() {
  mdb_env "$1" || return 1
  local u="MDB_${1^^}_USER" p="MDB_${1^^}_PASSWORD" cmd quoted pw kconf
  mkdir -p -- "$(dirname -- "$4")" || return 1
  cmd=(mysql --defaults-extra-file=/dev/stdin "-u${!u}")
  [[ -z "${5:-}" ]] || cmd+=(--quick -N -B)
  cmd+=("$2" -e "$3")
  pw="${!p//\\/\\\\}"; pw="${pw//\"/\\\"}"
  if [[ -z "${5:-}" ]]; then
    quoted="$(printf '%q ' "${cmd[@]}")"
    DBO_OPTFILE="$(printf '[client]\npassword="%s"' "$pw")" DBO_EXEC="$quoted" \
      KCFG="$DBO_KUBECONFIG" NS="$DBO_NS" POD="$DBO_POD" \
      setsid nohup bash -c 'printf "%s\n" "$DBO_OPTFILE" | eval "kubectl --kubeconfig \"\$KCFG\" -n \"\$NS\" exec -i \"\$POD\" -- $DBO_EXEC"' \
      </dev/null >"$4" 2>&1 &
  else
    mkdir -p -- "$4.pace" && printf '0\n' >"$4.pace/rate" || return 1
    kconf="$(kubectl --kubeconfig "$DBO_KUBECONFIG" config view --raw --minify -o json)" || return 1
    DBO_OPTFILE="$(printf '[client]\npassword="%s"' "$pw")" KEXEC_CONFIG="$kconf" KEXEC_NS="$DBO_NS" KEXEC_POD="$DBO_POD" \
      KEXEC_PACE_DIR="$4.pace" DBO_PY="$dbo_py" DBO_SRC="$DBO_SRC" \
      setsid nohup bash -c 'printf "%s\n" "$DBO_OPTFILE" | "$DBO_PY" -c "$DBO_SRC" pod-exec -- "$@" >/dev/null' \
      _ "${cmd[@]}" </dev/null >"$4" 2>&1 &
  fi
  printf '%s\n' "$!" >"$4.pgid"
}
# mdb_pod_pace LOG ID ROWS SECONDS: once the export's first rows have been sampled and the stream path between the
# pod and the runner is full (the server's Bytes_sent for processlist ID stops moving), read the rest at the byte
# rate that ends the server's work in SECONDS: about ROWS rows of the sampled size, less what the path holds.
mdb_pod_pace() {
  local dir="$1.pace" i sb=0 sr=0 b prev="" total remaining
  for ((i = 0; i < 40; i++)); do [[ -s "$dir/sample" ]] && break; sleep 0.5; done
  read -r sb sr <"$dir/sample" 2>/dev/null || true
  [[ "$sb" =~ ^[0-9]+$ && "$sr" =~ ^[1-9][0-9]*$ ]] || { echo "the export sent no sample of its rows" >&2; return 1; }
  for ((i = 0; i < 20; i++)); do
    b="$(mdb "" "SELECT s.variable_value FROM performance_schema.status_by_thread s JOIN performance_schema.threads t ON t.thread_id = s.thread_id WHERE t.processlist_id = $2 AND s.variable_name = 'Bytes_sent';")"
    [[ "$b" =~ ^[0-9]+$ ]] || { echo "the export's bytes sent are unreadable" >&2; return 1; }
    [[ "$b" == "$prev" ]] && break
    prev="$b"; sleep 1
  done
  total=$(( $3 * sb / sr )); remaining=$(( total - b ))
  (( remaining > 0 )) || { echo "the export (about $total bytes) fits in what the stream path holds ($b bytes)" >&2; return 1; }
  # The reader keeps the rate on plan itself (rows, bytes held in the path, deadline), see _Pace.
  printf '%s %s %s\n' "$3" "$(( b - sb ))" "$(( $(date +%s) + ($4 > 0 ? $4 : 1) ))" >"$dir/plan.tmp" && mv -f -- "$dir/plan.tmp" "$dir/plan"
}
# mdb_pod_drain LOG: let an export started by mdb_pod_spawn take the rest of its stream at full speed and wait for
# it to end, so the client in the pod can finish (an exec process whose stream closes stays blocked on its output,
# k3s 1.31 / containerd 1.7).
mdb_pod_drain() {
  local g i
  [[ -d "$1.pace" ]] || return 0
  printf 'max\n' >"$1.pace/rate"
  read -r g <"$1.pgid" 2>/dev/null || return 0
  [[ "$g" =~ ^[0-9]+$ ]] || return 0
  for ((i = 0; i < 60; i++)); do dbo_group_alive "$g" || return 0; sleep 1; done
  echo "the export client's stream did not end" >&2
  return 1
}
# dbo_group_alive PGID: a process of that group that has not ended (the runner's PID 1 does not reap, so ended
# children stay as zombies and `kill -0 -PGID` would still succeed).
dbo_group_alive() {
  local proc stat rest
  for proc in /proc/[0-9]*; do
    read -r stat <"$proc/stat" 2>/dev/null || continue
    rest="${stat##*) }"
    read -r -a f <<<"$rest"
    [[ "${f[2]}" == "$1" && "${f[0]}" != Z ]] && return 0
  done
  return 1
}
# dbo_reap DIR: end the runner-side process groups recorded under DIR (*.pgid). The server side has already been
# dealt with by the caller; this only stops readers and kubectl streams that would otherwise wait on a dead
# connection (C6b review: they outlived the cleanup after a cut).
dbo_reap() {
  local f g
  for f in "$1"/*.pgid; do
    [[ -e "$f" ]] || continue
    read -r g <"$f" || continue
    [[ "$g" =~ ^[0-9]+$ ]] || continue
    kill -TERM -- "-$g" 2>/dev/null || true
  done
  sleep 1
  for f in "$1"/*.pgid; do
    [[ -e "$f" ]] || continue
    read -r g <"$f" || continue
    [[ "$g" =~ ^[0-9]+$ ]] && kill -KILL -- "-$g" 2>/dev/null
  done
  return 0
}

# node_fs [unit]: "<size> <used> <available>" of the filesystem that holds the database pod's
# volumes, in MB (default) or KB. The data volumes are local-path PVs (hostPath on the node's root
# filesystem, 66 2026-11-10), so the kubelet's node filesystem figures are theirs; any other kind of
# PV is refused rather than measured on the wrong filesystem. No exec, no df in the pod.
node_fs() {
  local unit="${1:-MB}" node pod_json pv summary
  pod_json="$(dbo_k get pod "$DBO_POD" -o json)" || return 1
  node="$(jq -r '.spec.nodeName // empty' <<<"$pod_json")"
  [[ -n "$node" ]] || { echo "database pod $DBO_POD has no node" >&2; return 1; }
  local claim
  for claim in $(jq -r '.spec.volumes[]? | .persistentVolumeClaim.claimName // empty' <<<"$pod_json"); do
    pv="$(dbo_k get pvc "$claim" -o 'jsonpath={.spec.volumeName}')" || return 1
    [[ -n "$pv" ]] || { echo "claim $claim is not bound" >&2; return 1; }
    kubectl --kubeconfig "$DBO_KUBECONFIG" get pv "$pv" -o json \
      | jq -e '.spec.hostPath != null or .spec.local != null' >/dev/null \
      || { echo "volume $pv is not on the node's filesystem; refusing to read its free space from the node" >&2; return 1; }
  done
  summary="$(kubectl --kubeconfig "$DBO_KUBECONFIG" get --raw "/api/v1/nodes/$node/proxy/stats/summary")" || return 1
  jq -r --arg unit "$unit" '.node.fs | (if $unit == "KB" then 1024 else 1048576 end) as $d
    | [(.capacityBytes / $d | floor), (.usedBytes / $d | floor), (.availableBytes / $d | floor)] | map(tostring) | join(" ")' <<<"$summary"
}
'''


def with_sessions(script: bytes, *extra: bytes) -> bytes:
    """Put the session functions (and any EXTRA function blocks that build on them, such as
    pg_lock_remote's) right after the body's `set -euo pipefail` line."""
    marker = b"set -euo pipefail\n"
    head, sep, rest = script.partition(marker)
    if not sep:
        raise ValueError("remote body has no `set -euo pipefail` line")
    return head + sep + bash_functions() + b"".join(extra) + rest


def without_library(script: str) -> str:
    """The body with the shared function blocks (this module's and pg_lock_remote's) cut out, so a
    check on what a body itself does is not tripped by a word in the shared client."""
    import pg_lock_remote

    for start, end in ((START_MARKER, END_MARKER), (pg_lock_remote.START_MARKER, pg_lock_remote.END_MARKER)):
        while start in script:
            i = script.index(start)
            j = script.index(end, i) + len(end)
            script = script[:i] + script[j:]
    return script
