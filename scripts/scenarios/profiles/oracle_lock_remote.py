"""Bash side of the Oracle row-lock injection, shared by two executors (v4 C6).

``db_lock_executor`` (F01-P) 와 ``timeline_multi_injection_executor`` (F08-G, F15-G 의
``oracle_lock`` 단계)가 같은 잠금을 건다. 두 벌이던 sqlplus 본문이 서로 다르게 고쳐진
일이 있어(2026-08-03, 08-04) 이제는 이 모듈 하나가 함수 묶음을 만들고 둘 다 끼워 쓴다.
잠금 세션 자체는 ``oracle_lock_session.py`` 가 정본이며 여기서 그 소스를 heredoc 으로
싣는다.

옛 방식은 DB 파드 안에서 ``sqlplus / as sysdba`` 로 잠금을 잡고 ``host sleep`` 으로
버텼다. 119 에 남은 흔적이 셋이었다(2026-09-01 F01-P 녹화 실측).

    dpm_session  schema=SYS, program=sqlplus@testbed-oracle-0, machine=testbed-oracle-0
    process_meta sqlplus -s / as sysdba @/tmp/dba-maintenance.sql
    process_meta sleep 900 (DB 파드 컨테이너의 자식 프로세스)

DB 서버 자신이 SYS 로 자기 행을 잠그는 모양은 현실에 없다.

이 함수들은 러너 컨테이너에서 돈다. DB 파드에는 exec 하지 않는다. kubectl 로 읽는 것은
DB 파드가 있는 노드 주소, NodePort 번호, 앱 계정 비밀(Secret)뿐이다.
"""
from __future__ import annotations

import shlex
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLIENT_SOURCE_PATH = HERE / "oracle_lock_session.py"
HEREDOC_END = "ORACLE_LOCK_SESSION_PY"

# 러너 이미지(python:3.12-slim)의 시스템 파이썬. oracledb 는 이 파이썬에 설치한다.
# ORACLE_LOCK_PYTHON 은 시험이 가짜 oracledb 를 쓰는 파이썬으로 바꿀 때만 쓴다.
PYTHON = "/usr/local/bin/python3"
# 은행 도메인 Oracle 의 리스너를 밖으로 내는 Service 와 PDB.
NODEPORT_SERVICE = "testbed-oracle-external"
LISTENER_PORT = 1521
SERVICE_NAME = "FREEPDB1"
# 앱이 쓰는 계정 비밀. 키 이름은 core-banking/k8s/01-secrets.yaml 과 같다.
SECRET = "oracle-secret"
SECRET_USER_KEY = "APP_USER"
SECRET_PASSWORD_KEY = "APP_USER_PASSWORD"

# 잠금 세션의 신원: 운영자가 작업 호스트에서 SQLcl(오라클 명령줄 도구, JDBC 기반)로 붙은
# 모양. 전 시나리오 공통 상수라 어느 시나리오도 가리키지 않는다(기준서 G6 판정 규칙,
# client_identifier `dba-maintenance` 와 같은 취급). 값 선택은 결과 문서의 "확인 필요" 에
# 올렸다. python-oracledb 는 program, machine, osuser 에 공백과 괄호를 받지 않는다
# (DPY-3029, 접속 서술자에 실리기 때문). 그래서 "SQL Developer" 는 쓸 수 없었다.
CLIENT_PROGRAM = "SQLcl"
CLIENT_MACHINE = "ops-bastion"
CLIENT_OSUSER = "dba"


def client_source() -> str:
    source = CLIENT_SOURCE_PATH.read_text(encoding="utf-8")
    if any(line.strip() == HEREDOC_END for line in source.splitlines()):
        raise ValueError("client source collides with the heredoc terminator")
    return source


def bash_functions() -> bytes:
    """Bash function definitions (ora_*) with the client source embedded."""
    q = shlex.quote
    header = "\n".join(
        [
            "# ---- Oracle row-lock client (oracle_lock_remote.py) ----",
            f'ora_py="${{ORACLE_LOCK_PYTHON:-{PYTHON}}}"',
            f"ORA_NODEPORT_SERVICE={q(NODEPORT_SERVICE)}",
            f"ORA_LISTENER_PORT={LISTENER_PORT}",
            f"ORA_SERVICE_NAME={q(SERVICE_NAME)}",
            f"ORA_SECRET={q(SECRET)}",
            f"ORA_SECRET_USER_KEY={q(SECRET_USER_KEY)}",
            f"ORA_SECRET_PASSWORD_KEY={q(SECRET_PASSWORD_KEY)}",
            f"export CLIENT_PROGRAM={q(CLIENT_PROGRAM)}",
            f"export CLIENT_MACHINE={q(CLIENT_MACHINE)}",
            f"export CLIENT_OSUSER={q(CLIENT_OSUSER)}",
            f"ORA_SRC=$(cat <<'{HEREDOC_END}'",
        ]
    )
    return (header + "\n" + client_source() + HEREDOC_END + "\n)\n" + _FUNCTIONS).encode()


_FUNCTIONS = r'''
# ora_prepare NS DB_POD: 리스너 주소와 앱 계정을 읽어 env 로 내보낸다. DB 파드는 조회만 한다.
ora_prepare() {
  local ns="$1" db_pod="$2" host port
  local k=(kubectl --kubeconfig /root/tb-kubeconfig -n "$ns")
  "$ora_py" -c 'import oracledb' 2>/dev/null || { echo "runner image lacks python-oracledb" >&2; return 1; }
  host="$("${k[@]}" get pod "$db_pod" -o 'jsonpath={.status.hostIP}')"
  port="$("${k[@]}" get svc "$ORA_NODEPORT_SERVICE" -o "jsonpath={.spec.ports[?(@.port==$ORA_LISTENER_PORT)].nodePort}")"
  [[ -n "$host" && "$port" =~ ^[0-9]+$ ]] || { echo "oracle listener endpoint not found" >&2; return 1; }
  ORA_USER="$("${k[@]}" get secret "$ORA_SECRET" -o "jsonpath={.data.$ORA_SECRET_USER_KEY}" | base64 -d)"
  ORA_PASSWORD="$("${k[@]}" get secret "$ORA_SECRET" -o "jsonpath={.data.$ORA_SECRET_PASSWORD_KEY}" | base64 -d)"
  [[ -n "$ORA_USER" && -n "$ORA_PASSWORD" ]] || { echo "oracle app account secret is empty" >&2; return 1; }
  ORA_DSN="${host}:${port}/${ORA_SERVICE_NAME}"
  export ORA_USER ORA_PASSWORD ORA_DSN
}
# ora_target SCHEMA TABLE KEYCOL KEY
ora_target() { export LOCK_SCHEMA="$1" LOCK_TABLE="$2" LOCK_KEYCOL="$3" LOCK_KEY="$4"; }
# ora_call row-count|lockable: 짧은 세션 하나로 묻고 숫자 하나를 찍는다.
ora_call() { "$ora_py" -c "$ORA_SRC" "$1"; }
ora_row_ok() { [[ "$(ora_call row-count)" == 1 ]]; }
ora_released() { [[ "$(ora_call lockable)" == 1 ]]; }
# ora_alive RUN_DIR: 그 디렉터리의 잠금 세션 프로세스가 살아 있는가(pid 재사용은 cmdline 으로 거른다).
ora_alive() {
  local dir="$1" pid
  [[ -s "$dir/pid" ]] || return 1
  read -r pid <"$dir/pid" || return 1
  [[ "$pid" =~ ^[0-9]+$ && -r "/proc/$pid/cmdline" ]] || return 1
  tr '\0' ' ' <"/proc/$pid/cmdline" | grep -qF "$dir/oracle_lock_session.py hold"
}
# ora_hold_start RUN_DIR TAG HOLD: 잠금 세션을 띄우고 잠금을 잡았다고 알릴 때까지 기다린다.
ora_hold_start() {
  local dir="$1" tag="$2" hold="$3"
  mkdir -p "$dir"
  printf '%s' "$ORA_SRC" >"$dir/oracle_lock_session.py"
  rm -f -- "$dir/ready"
  LOCK_TAG="$tag" LOCK_HOLD="$hold" READY_FILE="$dir/ready" \
    nohup "$ora_py" "$dir/oracle_lock_session.py" hold >"$dir/log" 2>&1 </dev/null &
  local pid="$!"
  printf '%s\n' "$pid" >"$dir/pid"
  # Liveness here is kill -0, not ora_alive: until nohup and python have exec'd, the
  # child's cmdline is still this shell's, and ora_alive would call a starting session dead.
  for _ in $(seq 1 90); do
    [[ -s "$dir/ready" ]] && return 0
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  echo "oracle lock session did not report the lock: $(tail -c 400 "$dir/log" 2>/dev/null | tr '\n' ' ')" >&2
  ora_hold_stop "$dir" || true
  return 1
}
# ora_hold_stop RUN_DIR: SIGTERM 이면 세션이 롤백하고 닫는다. 15초 안에 안 끝나면 SIGKILL
# (소켓이 닫히면 서버가 롤백한다). 서버 쪽 해제 확인은 호출자가 ora_released 로 한다.
ora_hold_stop() {
  local dir="$1" pid
  ora_alive "$dir" || return 0
  read -r pid <"$dir/pid"
  kill -TERM "$pid" 2>/dev/null || true
  for _ in $(seq 1 15); do ora_alive "$dir" || return 0; sleep 1; done
  kill -KILL "$pid" 2>/dev/null || true
  sleep 1
  ! ora_alive "$dir"
}
'''
