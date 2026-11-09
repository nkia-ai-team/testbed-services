#!/usr/bin/env python3
"""Oracle row-lock session held from outside the database host (v4 C6).

Oracle 잠금 주입의 정본 클라이언트다. ``oracle_lock_remote.py`` 가 이 소스를 원격 bash
본문에 실어 보내고, 러너 컨테이너가 리스너(NodePort)로 접속해 실행한다. 옛 방식과 왜
바꿨는지는 ``oracle_lock_remote.py`` 머리 주석에 있다.

운영자나 앱이 잠금을 잡을 때의 모양은 "다른 호스트에서 네트워크로 붙은 세션이
트랜잭션을 열어 둔 채 놀고 있는 것" 이다. 이 클라이언트는 그 모양만 만든다.

* 접속은 리스너로, 앱 스키마 계정으로 한다.
* 잠금 유지는 클라이언트 쪽 대기다. 서버 세션은 INACTIVE, ``SQL*Net message from
  client`` 로 남고 피해 쪽은 ``enq: TX - row lock contention`` 으로 기다린다.
* 세션 신원(program, module, machine, osuser)은 호출자가 정한 운영 도구 모양이다.
  시나리오를 가리키는 값은 넣지 않는다.
* ``client_identifier`` 는 잠금 세션에만 붙인다. 러너의 감별 관측
  (``database.oracle_tagged_session_count``)이 이 값을 센다.
* 해제는 롤백이다. SIGTERM 이면 바로, 아니면 보유 시간이 끝나면.

값은 전부 환경 변수로 받는다(명령줄에 남기지 않는다). 식별자는 정규식으로 검사한 뒤
따옴표 없이 쓰고, 키 값은 바인드 변수로만 넘긴다.
"""
from __future__ import annotations

import os
import re
import signal
import sys
import time

IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]{0,127}$")
# FOR UPDATE WAIT 의 상한(초). 앱 트랜잭션이 행을 잠깐 잡고 있어도 기다렸다 잡는다.
ACQUIRE_WAIT_SECONDS = 60
# 해제 확인 때 행을 잠글 수 있는지 볼 대기(초). 앱의 짧은 잠금은 이 안에 풀린다.
RELEASE_PROBE_WAIT_SECONDS = 10
# ORA-30006: resource busy; acquire with WAIT timeout expired
ORA_WAIT_TIMEOUT = 30006

_stop = False


def _env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"missing {name}")
    return value


def _identifier(name: str) -> str:
    value = _env(name)
    if not IDENTIFIER.match(value):
        raise SystemExit(f"{name} is not a plain identifier")
    return value


def _target() -> tuple[str, str]:
    schema = _identifier("LOCK_SCHEMA")
    table = _identifier("LOCK_TABLE")
    keycol = _identifier("LOCK_KEYCOL")
    return f"{schema}.{table}", keycol


def _connect():
    import oracledb  # 러너 이미지에만 있다. 시험은 가짜 모듈을 넣는다.

    return oracledb.connect(
        user=_env("ORA_USER"),
        password=_env("ORA_PASSWORD"),
        dsn=_env("ORA_DSN"),
        program=_env("CLIENT_PROGRAM"),
        machine=_env("CLIENT_MACHINE"),
        osuser=_env("CLIENT_OSUSER"),
        driver_name=_env("CLIENT_DRIVER"),
        terminal="unknown",
        # NodePort 경유 유휴 연결이 중간 장비에서 끊기지 않게 TCP keepalive 를 켠다.
        expire_time=1,
    )


def _error_code(exc: BaseException) -> int:
    args = getattr(exc, "args", ())
    if args and hasattr(args[0], "code"):
        return int(args[0].code)
    return 0


def row_count() -> int:
    table, keycol = _target()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(f"select count(*) from {table} where {keycol} = :k", k=_env("LOCK_KEY"))
        (count,) = cur.fetchone()
        return int(count)
    finally:
        conn.close()


def lockable() -> int:
    """1 if the row can be locked within RELEASE_PROBE_WAIT_SECONDS, else 0."""
    table, keycol = _target()
    conn = _connect()
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                f"select {keycol} from {table} where {keycol} = :k "
                f"for update wait {RELEASE_PROBE_WAIT_SECONDS}",
                k=_env("LOCK_KEY"),
            )
            cur.fetchall()
        except Exception as exc:  # oracledb.DatabaseError
            if _error_code(exc) == ORA_WAIT_TIMEOUT:
                return 0
            raise
        conn.rollback()
        return 1
    finally:
        conn.close()


def _request_stop(_signum, _frame) -> None:
    global _stop
    _stop = True


def hold() -> int:
    table, keycol = _target()
    seconds = int(_env("LOCK_HOLD"))
    if seconds <= 0:
        raise SystemExit("LOCK_HOLD must be positive")
    ready_file = _env("READY_FILE")
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    conn = _connect()
    try:
        conn.client_identifier = _env("LOCK_TAG")
        conn.module = _env("CLIENT_PROGRAM")
        # action 은 건드리지 않는다. 빈 문자열을 넣으면 23ai 서버가 "Bad TTC Packet" 으로
        # 연결을 끊고 경고 로그에 접속 정보를 남긴다(11-09 Oracle Free 실측).
        cur = conn.cursor()
        cur.execute(
            f"select {keycol} from {table} where {keycol} = :k for update wait {ACQUIRE_WAIT_SECONDS}",
            k=_env("LOCK_KEY"),
        )
        if len(cur.fetchall()) != 1:
            conn.rollback()
            raise SystemExit("lock target row is missing")
        with open(ready_file, "w", encoding="ascii") as handle:
            handle.write("locked\n")
        deadline = time.monotonic() + seconds
        while not _stop:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(remaining, 1.0))
        conn.rollback()
        return 0
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        raise SystemExit("usage: oracle_lock_session.py row-count|lockable|hold")
    verb = argv[1]
    if verb == "row-count":
        print(row_count())
        return 0
    if verb == "lockable":
        print(lockable())
        return 0
    if verb == "hold":
        return hold()
    raise SystemExit(f"unknown verb: {verb}")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
