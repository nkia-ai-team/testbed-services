#!/usr/bin/env python3
"""실행기 배관 줄을 가르는 꼴의 정의 한 곳(설계 docs/spec-testbed-vm-3tier.md §6-2, Q7a).

VM 첫 rsyslog 규칙(templates/common/rsyslog-00-opsjobs.conf, render.py 가 채움), 66 거르기(hypervisor/rsyslog-05-hvjobs.conf,
hypervisor/build.py 가 채움), runbook 의 확인 명령(이 파일의 CLI), 흔적 표 도구의 Q7a(tools/trace_table.py)가 모두 여기서 꼴을
가져온다. 거르기와 확인이 서로 다른 꼴을 쓰면 같은 틈으로 0 을 세게 된다(11-20 APT4 차례 8: `user-runtime-dir@1460.service`
의 `dir@1460` 을 거르기와 확인 꼴이 함께 놓쳤다).

꼴은 POSIX ERE 이다(rsyslog re_match, grep -E, ClickHouse match 의 re2 가 모두 [[:alnum:]] 을 받는다).

uid 꼴의 근거: systemd 255(Ubuntu 24.04 VM), 257(Debian 13, 66)이 로그인 세션을 열고 닫을 때 내는 줄(아래 SYSTEMD_SESSION_LINES)에서
uid 가 놓이는 자리는 다섯 가지다.
  user-<uid>          slice 이름(user-1450.slice), 마운트 유닛(run-user-1450.mount)
  <유닛>@<uid>        템플릿 유닛 인스턴스(user@1450.service, user-runtime-dir@1450.service)
  UID <uid>           설명(User Manager for UID 1450, User Slice of UID 1450)
  uid=<uid>           pam_unix(... uid=1450)
  /user/<uid>         경로(/run/user/1450)
uid 뒤에는 숫자가 오면 안 된다(14500 을 1450 으로 보지 않게). 앞은 위 다섯 자리말로만 열린다(session-1450.scope 의 세션 번호는
uid 가 아니다).
"""
from __future__ import annotations

import argparse
import re
import sys

UID_PREFIXES = ("user-", "@", "UID ", "uid=", "/user/")
RUN_R = "run-r[0-9a-f]{32}"
NAME = re.compile(r"^[a-z][a-z0-9]{1,30}$")


def name_ere(name: str) -> str:
    """계정 이름이 낱말로 든 줄."""
    if not NAME.fullmatch(name):
        raise ValueError(f"계정 이름 꼴이 아님: {name!r}")
    return f"(^|[^[:alnum:]_.-]){name}([^[:alnum:]_-]|$)"


def uid_ere(uid: int | str) -> str:
    """systemd, logind, pam 이 uid 를 놓는 다섯 자리(모듈 머리) 뒤의 그 uid."""
    uid = str(uid)
    if not uid.isdigit():
        raise ValueError(f"uid 꼴이 아님: {uid!r}")
    return "(" + "|".join(UID_PREFIXES) + ")" + uid + "([^0-9]|$)"


def rainer(ere: str) -> str:
    """rsyslog RainerScript 큰따옴표 문자열 안에 넣을 꼴(\\, ", $ 를 탈출. 맨 $ 는 8.2312 가 설정을 거절한다)."""
    return ere.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")


def check_ere(accounts: list[tuple[str, int | str]], extra: list[str] = ()) -> str:
    """확인 명령이 셀 꼴 하나: 계정마다 이름과 uid, 가드 유닛 이름, 덧붙인 낱말(도우미 이름 등)."""
    parts = []
    for name, uid in accounts:
        parts += [name_ere(name), uid_ere(uid)]
    parts.append(RUN_R)
    for word in extra:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,63}", word):
            raise ValueError(f"덧붙일 낱말 꼴이 아님: {word!r}")
        parts.append(word.replace(".", "[.]"))
    return "|".join(parts)


def systemd_session_lines(name: str, uid: int | str, session: int = 12) -> list[tuple[str, str]]:
    """systemd 255 와 257 이 ssh 로그인 세션 하나를 열고 닫을 때(첫 세션이라 사용자 관리자도 뜨고 짐) 내는 줄의 꼴.
    (프로그램 이름, 본문). 사용자 관리자 자신(systemd --user, 그 uid 로 돎)의 줄은 VM 에서는 소켓 uid 갈래가 잡고
    66 에서는 잡지 못한다(모듈 밖, 결과 문서 확인 필요)."""
    u, n, s = str(uid), name, session
    return [
        ("systemd", f"Created slice user-{u}.slice - User Slice of UID {u}."),
        ("systemd", f"Starting user-runtime-dir@{u}.service - User Runtime Directory /run/user/{u}..."),
        ("systemd", f"Finished user-runtime-dir@{u}.service - User Runtime Directory /run/user/{u}."),
        ("systemd", f"Starting user@{u}.service - User Manager for UID {u}..."),
        ("systemd", f"Started user@{u}.service - User Manager for UID {u}."),
        ("systemd-logind", f"New session {s} of user {n}."),
        ("systemd", f"Started session-{s}.scope - Session {s} of User {n}."),
        ("systemd", f"Stopping user@{u}.service - User Manager for UID {u}..."),
        ("systemd", f"user@{u}.service: Deactivated successfully."),
        ("systemd", f"Stopped user@{u}.service - User Manager for UID {u}."),
        ("systemd", f"user@{u}.service: Consumed 1.234s CPU time, 4.5M memory peak."),
        ("systemd", f"Stopping user-runtime-dir@{u}.service - User Runtime Directory /run/user/{u}..."),
        ("systemd", f"run-user-{u}.mount: Deactivated successfully."),
        ("systemd", f"user-runtime-dir@{u}.service: Deactivated successfully."),
        ("systemd", f"Stopped user-runtime-dir@{u}.service - User Runtime Directory /run/user/{u}."),
        ("systemd", f"Removed slice user-{u}.slice - User Slice of UID {u}."),
        ("systemd", f"user-{u}.slice: Consumed 2.345s CPU time, 9.8M memory peak."),
    ]


# 같은 세션의 줄 가운데 계정 이름도 uid 도 없어 꼴로 가를 수 없는 것(평시에도 다른 사용자의 세션이 같은 꼴을 남긴다).
UNATTRIBUTABLE = ("session-{s}.scope: Deactivated successfully.", "Session {s} logged out. Waiting for processes to exit.",
                  "Removed session {s}.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", action="append", required=True, help="<이름>:<uid>, 여러 번")
    parser.add_argument("--extra", action="append", default=[], help="덧붙여 셀 낱말(도우미 이름 등)")
    parser.add_argument("--format", choices=["ere", "clickhouse"], default="ere")
    args = parser.parse_args(argv)
    accounts = []
    for item in args.account:
        name, _, uid = item.partition(":")
        accounts.append((name, uid))
    ere = check_ere(accounts, args.extra)
    # ere: grep -E -c "$(... --format ere)" <파일>. clickhouse: match(message, '<꼴>') 의 따옴표 안에 그대로 넣는다.
    print(ere if args.format == "ere" else ere.replace("\\", "\\\\").replace("'", "\\'"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
