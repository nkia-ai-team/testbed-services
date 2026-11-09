#!/usr/bin/env python3
"""Operator-shaped database client run from the runner container (v4 C6b).

DB 변경 실행기(db.account, db.ddl, db.row_delete, db.table_readonly, db.cursor_reset,
app.control)가 DB 파드에 exec 해 ``sqlplus / as sysdba``, ``mysql -uroot``, ``psql`` 을
돌리던 자리를 대신한다. 옛 방식은 DB 노드에 runc exec, 클라이언트 프로세스, 그리고 Oracle
에는 DB 서버 자신이 SYS 로 붙은 짧은 세션(machine=testbed-oracle-0)을 남겼다. 현실의 운영자는
작업 호스트에서 리스너로 붙는다. 이 클라이언트는 그 모양만 만든다.

* Oracle: python-oracledb thin. 신원은 ``oracle_lock_remote`` 의 운영 도구 상수(SQLcl,
  ops-bastion, dba)를 env 로 받는다. 입력은 지금까지 sqlplus 에 넣던 스크립트 그대로다
  (``set``, ``whenever sqlerror``, ``alter session set container``, ``exit`` 를 sqlplus 처럼
  해석). 출력도 ``set pages 0 heading off feedback off`` 의 모양(행마다 한 줄, 칸은 공백 하나).
* MySQL: PyMySQL. ``mysql -N -B -e`` 와 같은 출력(칸은 탭, NULL 은 NULL)과 오류 첫 줄.
  접속 속성은 mysql 명령줄 도구 모양이다.

PostgreSQL 은 러너 이미지에 이미 있는 psql 을 쓰므로 이 파일이 없다(db_operator_remote.py).

값은 전부 환경 변수로 받는다. 비밀번호는 명령줄, 출력, 오류 문구에 싣지 않는다.
"""
from __future__ import annotations

import os
import re
import socket
import sys

ORACLE_ERROR_EXIT = 1
# Receive buffer and window for a paced export's connection. A paced reader holds the statement open only
# while the rows it has not taken yet cannot all sit in buffers between it and the server. Inside the pod
# the client used a Unix socket; over TCP the kernel grows the buffers to MBs and the export finishes on
# the server long before the reader is done, dropping its lock early. 2026-11-10, 104 docker, 3.4 MB COPY
# read at about 70 KB/s: in the pod the statement stayed active 49 s; psql over TCP 4 s; through a relay
# with a 64 KB buffer 34 s; with 8 KB and the window clamped to it 49 s. MySQL with the same socket: the
# server stops after 70 KB unread (229 KB with the old in-pod Unix socket). 8 KB still carries over
# 10 MB/s at a millisecond's round trip, well above the fastest paced export (about 1 MB/s).
PACED_RCVBUF = 8192


def _env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"missing {name}")
    return value


# ---------------------------------------------------------------- Oracle

_CONTAINER_SWITCH = re.compile(r"(?is)^\s*alter\s+session\s+set\s+container\s*=")
_PLSQL_START = re.compile(
    r"(?is)^\s*(begin|declare)\b|^\s*create\s+(or\s+replace\s+)?(procedure|function|package|trigger|type)\b"
)
_SQLPLUS_COMMAND = re.compile(r"(?i)^(set|whenever|prompt|spool|rem|remark|exit|quit)(\s|;|$)")


def oracle_statements(script: str):
    """Split a SQL*Plus script into ('cmd', text) and ('sql', text) items.

    A SQL statement ends with ';' at the end of a line; a PL/SQL block (begin, declare,
    create procedure ...) ends with a line holding only '/'. SQL*Plus commands are
    recognised only at the start of a statement, as SQL*Plus does.
    """
    buf: list[str] = []
    plsql = False
    for raw in script.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not buf:
            if not stripped or stripped.startswith("--"):
                continue
            if _SQLPLUS_COMMAND.match(stripped):
                yield ("cmd", stripped.rstrip(";").strip())
                continue
            if stripped == "/":
                continue
            plsql = bool(_PLSQL_START.match(stripped))
        if stripped == "/":
            yield ("sql", "\n".join(buf))
            buf, plsql = [], False
            continue
        buf.append(line)
        if not plsql and stripped.endswith(";"):
            text = "\n".join(buf).rstrip()
            yield ("sql", text[:-1])
            buf = []
    # An unterminated statement is not run, as in SQL*Plus.


def _oracle_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if hasattr(value, "read"):
        return str(value.read())
    return str(value)


def oracle_connect():
    import oracledb  # 러너 이미지 시스템 파이썬에만 있다. 시험은 가짜 모듈을 넣는다.

    program = _env("CLIENT_PROGRAM")
    kwargs = dict(
        user=_env("ODB_USER"),
        password=_env("ODB_PASSWORD"),
        dsn=_env("ODB_DSN"),
        program=program,
        machine=_env("CLIENT_MACHINE"),
        osuser=_env("CLIENT_OSUSER"),
        driver_name=_env("CLIENT_DRIVER"),
        terminal="unknown",
        # NodePort 경유 연결이 중간 장비에서 끊기지 않게(oracle_lock_session 과 같은 값).
        expire_time=1,
    )
    conn = oracledb.connect(**kwargs)
    conn.module = program
    return conn


def run_oracle_script(script: str, out=sys.stdout) -> int:
    """Run a SQL*Plus-style script and return the exit status SQL*Plus would give.

    * ``whenever sqlerror exit ...`` makes the first error end the script with 1
      (``rollback`` rolls back first; otherwise SQL*Plus commits, and so does this).
    * Without it an error is printed and the script goes on, exit status 0.
    * ``exit`` and the end of the script commit, as SQL*Plus does by default.
    """
    conn = oracle_connect()
    on_error = None  # None = continue, else "commit" or "rollback" before exiting 1
    status = 0
    try:
        cursor = conn.cursor()
        for kind, text in oracle_statements(script):
            if kind == "cmd":
                low = text.lower()
                if low.startswith(("exit", "quit")):
                    break
                if low.startswith("whenever"):
                    words = low.split()
                    if "sqlerror" in words and "exit" in words:
                        on_error = "rollback" if "rollback" in words else "commit"
                    elif "sqlerror" in words and "continue" in words:
                        on_error = None
                continue
            if _CONTAINER_SWITCH.match(text):
                # The session is already in the PDB: the listener service names it.
                continue
            try:
                cursor.execute(text)
                if cursor.description is not None:
                    for row in cursor.fetchall():
                        print(" ".join(_oracle_value(v) for v in row), file=out)
            except Exception as exc:  # oracledb.DatabaseError
                message = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
                print(message, file=out)
                print(message, file=sys.stderr)
                if on_error is not None:
                    if on_error == "rollback":
                        conn.rollback()
                    else:
                        conn.commit()
                    status = ORACLE_ERROR_EXIT
                    return status
        conn.commit()
        return status
    finally:
        out.flush()
        conn.close()


# ---------------------------------------------------------------- MySQL

# What the mysql command-line client sends (MySQL 8.0 libmysqlclient). DPM does not read
# connect attributes today; they still match the tool an operator would use.
MYSQL_CONNECT_ATTRS = {
    "_client_name": "libmysql",
    "_client_version": "8.0.40",
    "_os": "Linux",
    "_platform": "x86_64",
    "program_name": "mysql",
}


def _small_buffer_connection(address, timeout=None, source_address=None, **_kwargs):
    """socket.create_connection with the receive buffer fixed before the handshake."""
    host, port = address
    last = None
    for family, kind, proto, _name, sockaddr in socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM):
        sock = socket.socket(family, kind, proto)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, PACED_RCVBUF)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_WINDOW_CLAMP, PACED_RCVBUF)
            if timeout is not None:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last = exc
            sock.close()
    raise last or OSError(f"cannot connect to {host}:{port}")


def mysql_connect(database: str | None, paced: bool = False):
    import pymysql  # 러너 이미지 시스템 파이썬에만 있다.
    from pymysql.constants import CLIENT

    if paced:
        # PyMySQL opens its socket with socket.create_connection; this process runs one export only.
        import pymysql.connections

        pymysql.connections.socket.create_connection = _small_buffer_connection

    conn = pymysql.connect(
        host=_env("MDB_HOST"),
        port=int(_env("MDB_PORT")),
        user=_env("MDB_USER"),
        password=_env("MDB_PASSWORD"),
        database=database or None,
        charset="utf8mb4",
        autocommit=True,
        client_flag=CLIENT.MULTI_STATEMENTS,
        connect_timeout=10,
        defer_connect=True,
    )
    attrs = dict(MYSQL_CONNECT_ATTRS)
    attrs["_pid"] = str(os.getpid())
    conn._connect_attrs = attrs
    conn.connect()
    return conn


def _mysql_value(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", "replace")
    text = str(value)
    # mysql -B escapes these so a row stays one line.
    return (text.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\0", "\\0"))


def _mysql_error(exc: BaseException) -> str:
    args = getattr(exc, "args", ())
    if len(args) >= 2 and isinstance(args[0], int):
        return f"ERROR {args[0]}: {args[1]}"
    return f"ERROR: {exc}"


def run_mysql(sql: str, database: str | None, stream: bool, out=sys.stdout) -> int:
    """Run one ``-e`` string (one or more statements) the way ``mysql -N -B`` does."""
    import pymysql
    import pymysql.cursors

    try:
        conn = mysql_connect(database, paced=stream)
    except pymysql.MySQLError as exc:
        print(_mysql_error(exc), file=sys.stderr)
        return 1
    try:
        cursor = conn.cursor(pymysql.cursors.SSCursor if stream else pymysql.cursors.Cursor)
        try:
            cursor.execute(sql)
            while True:
                if cursor.description is not None:
                    for row in cursor:
                        out.write("\t".join(_mysql_value(v) for v in row) + "\n")
                if not cursor.nextset():
                    break
        except pymysql.MySQLError as exc:
            out.flush()
            print(_mysql_error(exc), file=sys.stderr)
            return 1
        except BrokenPipeError:
            # The reader is gone (an export ended by its caller); closing the connection ends the statement.
            return 1
        return 0
    finally:
        try:
            out.flush()
        except BrokenPipeError:
            pass
        conn.close()


# ---------------------------------------------------------------- PostgreSQL paced export relay

def pg_relay(listen_dir: str, port: str) -> int:
    """Carry one psql connection to the server through a TCP socket with a small receive buffer.

    psql (libpq) cannot set its socket's buffer, so a paced export connects to a Unix socket here
    (``PGHOST=<listen_dir>``) and this process forwards the bytes both ways. It serves one connection
    and ends with it. Reading upstream only as fast as psql takes the rows keeps the server's COPY
    waiting on the client, as it did with psql inside the pod.
    """
    import threading

    path = os.path.join(listen_dir, f".s.PGSQL.{port}")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    os.chmod(path, 0o600)
    server.listen(1)
    server.settimeout(60)
    try:
        down, _ = server.accept()
    finally:
        server.close()
        os.unlink(path)
    down.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, PACED_RCVBUF)
    up = _small_buffer_connection((_env("PG_UPSTREAM_HOST"), int(_env("PG_UPSTREAM_PORT"))), timeout=10)
    up.settimeout(None)

    def pump(src, dst) -> None:
        try:
            while True:
                data = src.recv(16384)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for sock, how in ((dst, socket.SHUT_WR), (src, socket.SHUT_RD)):
                try:
                    sock.shutdown(how)
                except OSError:
                    pass

    outbound = threading.Thread(target=pump, args=(down, up), daemon=True)
    outbound.start()
    pump(up, down)
    outbound.join(timeout=5)
    up.close()
    down.close()
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        raise SystemExit("usage: db_operator_session.py oracle|mysql|mysql-stream|pg-relay DIR PORT")
    verb = argv[1]
    if verb == "oracle":
        return run_oracle_script(sys.stdin.read())
    if verb in ("mysql", "mysql-stream"):
        return run_mysql(_env("SQL"), os.environ.get("DB", ""), verb == "mysql-stream")
    if verb == "pg-relay" and len(argv) == 4:
        return pg_relay(argv[2], argv[3])
    raise SystemExit(f"unknown verb: {verb}")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
