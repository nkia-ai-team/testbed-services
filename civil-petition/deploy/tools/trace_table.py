#!/usr/bin/env python3
"""주입 수단별로 119 에 실제로 남은 흔적을 읽어 흔적 표 한 줄과 배관 흔적 판정(Q7)을 낸다. 읽기 전용.

설계 정본: docs/spec-testbed-vm-3tier.md §6-7(표의 칸, 줄, 칸을 채우는 규칙), §6-2(Q7a ~ Q7d), §10 T4 "흔적 실측",
"배관 흔적". 표 자리: docs/ref-civil-petition-injection-traces.md. 시나리오 후보를 만들지 않는다(설계 §7).

읽는 곳(쓰기 없음, t3_check.Remote 와 같은 통로):
  - 119 ClickHouse: 파드 안 clickhouse-client --readonly=1 --max_threads=2 --max_execution_time=60.
    모든 질의에 시각 범위와 그 표의 분할 키 조건을 함께 건다(process_snapshot toDate(ts), host_connections
    toYYYYMMDD(timestamp), lucida_logs_local timestamp_date, syslog_local 과 snmp_traps_local toYYYYMM(received_at)).
    lucida_events_local 은 분할 키가 날짜가 아니라(cityHash64(event_id) 의 나머지) 시각 범위와 대상 번호로만 좁힌다.
  - 119 PostgreSQL: read only 트랜잭션, statement_timeout 20초(가용성 이력, 구성 이력)
  - 119 VictoriaMetrics: 평가용 LB(:18428) 질의만
창: 주입 창(--start, --end)과 그 앞의 같은 길이 평시 창 N 개(--baseline-windows, 기본 6). 평시 창은 주입 창 바로 앞부터
거꾸로 잇고, --baseline-gap 만큼 띄운다(기본 30분, 주입 직전 준비 동작을 평시에 넣지 않게).

쓰는 법:
  trace_table.py --inventory inventory/t.json --start <주입 시작> --end <회복 끝> --executor <실행기> --variant <변형>
                 --run-id <시험 실행 id> --words <가릴 낱말,...> [--injector-host hypervisor|router|<VM 이름>] [--json <파일>]
시각은 ISO(시간대 포함). 출력: 표 한 줄(마크다운)과 Q7 판정. --json 은 질의, 수치, 판정 전부.
--words 에는 그 시험 실행의 시나리오 id, 실행기 작업 이름(가드 job 이름), 가드 파일 이름을 넣는다. 공통 낱말(test, testbed,
tb, t4)과 실행기 계정 이름, uid, run-r 꼴은 늘 본다.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import t3_check  # noqa: E402
import pipeline_patterns  # noqa: E402  (배관 줄 꼴의 정의 한 곳, 거르기와 같은 꼴)

UTC = dt.timezone.utc
KST = dt.timezone(dt.timedelta(hours=9))
HV_SOURCE = "192.168.200.66"
COMMON_WORDS = ("test", "testbed", "tb", "t4")
RUN_R = pipeline_patterns.RUN_R
COLUMNS = ("SMS 지표", "프로세스 표본", "연결 표본", "로그 감시", "syslog", "66 출발 syslog", "가용성", "NMS 지표", "trap",
           "ICMP", "SMS 구성 변경", "변경 감지 이벤트")


@dataclasses.dataclass
class Window:
    start: dt.datetime
    end: dt.datetime

    @property
    def seconds(self) -> int:
        return max(int((self.end - self.start).total_seconds()), 60)


def windows(start: dt.datetime, end: dt.datetime, count: int, gap: dt.timedelta) -> tuple[Window, list[Window]]:
    span = end - start
    base, cursor = [], start - gap
    for _ in range(count):
        base.append(Window(cursor - span, cursor))
        cursor -= span
    return Window(start, end), base


# ---------------------------------------------------------------- 질의 꼴(분할 키 + 시각 범위)

def ts(t: dt.datetime) -> str:
    return t3_check.ch_ts(t)


def day(t: dt.datetime) -> str:
    return t3_check.ch_date(t)


def span_process(w: Window) -> str:
    return f"toDate(ts) between '{day(w.start)}' and '{day(w.end)}' and ts between '{ts(w.start)}' and '{ts(w.end)}'"


def span_connections(w: Window) -> str:
    return (f"toYYYYMMDD(timestamp) between {w.start.astimezone(UTC):%Y%m%d} and {w.end.astimezone(UTC):%Y%m%d} "
            f"and timestamp between '{ts(w.start)}' and '{ts(w.end)}'")


def span_logs(w: Window) -> str:
    return f"timestamp_date between '{day(w.start)}' and '{day(w.end)}' and timestamp between '{ts(w.start)}' and '{ts(w.end)}'"


def span_received(w: Window) -> str:
    return (f"toYYYYMM(received_at) between {w.start.astimezone(UTC):%Y%m} and {w.end.astimezone(UTC):%Y%m} "
            f"and received_at between '{ts(w.start)}' and '{ts(w.end)}'")


def span_events(w: Window) -> str:
    return f"occurred_at between '{ts(w.start)}' and '{ts(w.end)}'"


def quote(values) -> str:
    return t3_check.quote(values)


def pattern(message: str) -> str:
    """줄 꼴: 숫자, 16진 덩어리, 주소를 자리표로 바꾼 앞 80자."""
    text = re.sub(r"\b[0-9a-f]{8,}\b", "<hex>", message)
    text = re.sub(r"\d+", "<n>", text)
    return text[:80]


# ---------------------------------------------------------------- 칸

@dataclasses.dataclass
class Cell:
    column: str
    text: str = ""
    numbers: dict = dataclasses.field(default_factory=dict)


def guarded(column: str, fn: Callable[[], Cell]) -> Cell:
    """재지 못한 칸은 비우지 않고 "미측정, 이유" 로 둔다(설계 §6-7 칸을 채우는 규칙)."""
    try:
        return fn()
    except (t3_check.QueryError, OSError, ValueError, KeyError, IndexError) as exc:
        return Cell(column, f"미측정, {type(exc).__name__}: {str(exc)[:120]}")


def above_baseline(window_value: float, baseline: list[float]) -> bool:
    return window_value > (max(baseline) if baseline else 0)


def cell_series(remote, column: str, ids: dict[str, str], prefix: str, w: Window, base: list[Window]) -> Cell:
    """계열마다 주입 창 최댓값이 평시 창 최댓값을 넘는 것(대상, 계열 이름, 창 최댓값, 평시 최댓값)."""
    found = []
    for name, target in ids.items():
        def series(win: Window) -> dict[str, float]:
            q = (f'max by (__name__) (max_over_time({{host_target_id="{target}",__name__=~"{prefix}"}}[{win.seconds}s]) '
                 f'or max_over_time({{target_id="{target}",__name__=~"{prefix}"}}[{win.seconds}s]))')
            return {r["metric"].get("__name__", ""): float(r["value"][1]) for r in t3_check.vm_vector(remote, q, win.end)}
        now = series(w)
        before = [series(b) for b in base]
        for metric, value in sorted(now.items()):
            ref = [b[metric] for b in before if metric in b]
            if ref and above_baseline(value, ref) and value > 1.2 * max(ref):
                found.append(f"{name} {metric} 창 최댓값 {value:g}, 평시 최댓값 {max(ref):g}")
    return Cell(column, "; ".join(found[:12]) or "없음(실측)", {"rows": found})


def cell_process(remote, ids: dict[str, str], w: Window, base: list[Window]) -> Cell:
    def names(win: Window) -> dict[tuple[str, str, str], float]:
        rows = remote.ch(f"select target_id, name, user, max(cpu_pct) from lucida.process_snapshot where {span_process(win)} "
                         f"and target_id in ({quote(ids.values())}) group by 1, 2, 3 format TSV;")
        return {(r[0], r[1], r[2]): float(r[3]) for r in rows}
    now = names(w)
    seen = set().union(*(names(b).keys() for b in base)) if base else set()
    by_id = {v: k for k, v in ids.items()}
    new = [f"{by_id.get(t, t)} {n}({u}) CPU 최대 {c:g}%" for (t, n, u), c in sorted(now.items()) if (t, n, u) not in seen]
    return Cell("프로세스 표본", "; ".join(new[:12]) or "없음(실측, 평시 창에 없던 이름 0)", {"new": new})


def cell_connections(remote, ids: dict[str, str], w: Window, base: list[Window]) -> Cell:
    def tuples(win: Window) -> set[tuple[str, str, str]]:
        rows = remote.ch(f"select target_id, remote_addr, toString(local_port), process from lucida.host_connections "
                         f"where {span_connections(win)} and target_id in ({quote(ids.values())}) group by 1, 2, 3, 4 format TSV;")
        return {(r[0], r[1], f"{r[2]}/{r[3]}") for r in rows}
    now = tuples(w)
    seen = set().union(*(tuples(b) for b in base)) if base else set()
    by_id = {v: k for k, v in ids.items()}
    new = sorted(now - seen)
    text = "; ".join(f"{by_id.get(t, t)} {a} {p}" for t, a, p in new[:12])
    return Cell("연결 표본", text or "없음(실측, 새 연결 0)", {"new": [list(x) for x in new]})


def cell_logs(remote, ids: dict[str, str], w: Window, base: list[Window]) -> Cell:
    def counts(win: Window) -> dict[tuple[str, str], int]:
        rows = remote.ch("select host_target_id, log_attributes['file_path'], count() from lucida.lucida_logs_local where "
                         f"{span_logs(win)} and event_source = 'log_monitor' and host_target_id in ({quote(ids.values())}) "
                         "group by 1, 2 format TSV;")
        return {(r[0], r[1]): int(r[2]) for r in rows}
    now = counts(w)
    before = [counts(b) for b in base]
    by_id = {v: k for k, v in ids.items()}
    rows = []
    for key, n in sorted(now.items()):
        ref = [b.get(key, 0) for b in before]
        if above_baseline(n, ref):
            sample = remote.ch("select substring(body, 1, 160) from lucida.lucida_logs_local where "
                               f"{span_logs(w)} and event_source = 'log_monitor' and host_target_id in ({quote([key[0]])}) "
                               f"and log_attributes['file_path'] in ({quote([key[1]])}) order by timestamp limit 1 format TSV;")
            rows.append(f"{by_id.get(key[0], key[0])} {key[1]} {n}줄(평시 최대 {max(ref) if ref else 0}), 첫 줄 꼴 "
                        f"'{pattern(sample[0][0]) if sample else ''}'")
    return Cell("로그 감시", "; ".join(rows) or "없음(실측)", {"rows": rows})


PATTERN_SQL = ("substring(replaceRegexpAll(replaceRegexpAll(message, '[0-9a-f]{8,}', '<hex>'), '[0-9]+', '<n>'), 1, 80)")


def syslog_patterns(remote, sources: list[str], win: Window) -> dict[tuple[str, str], int]:
    """줄 꼴별 수. 꼴은 관제 서버에서 묶어 센다(숫자, 16진 덩어리를 자리표로 바꾼 앞 80자)."""
    rows = remote.ch(f"select source_ip, app_name, {PATTERN_SQL} p, count() from lucida.syslog_local where {span_received(win)} "
                     f"and source_ip in ({quote(sources)}) group by 1, 2, 3 format TSV;")
    return {(r[0], f"{r[1]}: {r[2]}"): int(r[3]) for r in rows}


def cell_syslog(remote, column: str, sources: list[str], names: dict[str, str], w: Window, base: list[Window]) -> Cell:
    now = syslog_patterns(remote, sources, w)
    before = [syslog_patterns(remote, sources, b) for b in base]
    fresh, grown = [], []
    for key, n in sorted(now.items()):
        ref = [b.get(key, 0) for b in before]
        if not any(ref):
            fresh.append(f"{names.get(key[0], key[0])} {key[1]} {n}줄")
        elif n > max(ref):
            grown.append(f"{names.get(key[0], key[0])} {key[1]} {n}줄(평시 최대 {max(ref)})")
    total = sum(now.values())
    base_max = max((sum(b.values()) for b in before), default=0)
    text = f"창 {total}줄(평시 창 최대 {base_max}). 평시에 없던 꼴: " + ("; ".join(fresh[:10]) or "0")
    if grown:
        text += ". 평시보다 는 꼴: " + "; ".join(grown[:6])
    return Cell(column, text, {"total": total, "baseline_max": base_max, "fresh": fresh, "grown": grown})


def cell_availability(remote, ids: dict[str, str], w: Window) -> Cell:
    rows = remote.pg("select target_id, resource_kind, coalesce(resource_key,''), status, started_at from "
                     "target_availability_history where started_at between "
                     f"'{w.start.isoformat()}' and '{w.end.isoformat()}' and target_id in ({quote(ids.values())}) order by started_at;")
    by_id = {v: k for k, v in ids.items()}
    text = "; ".join(f"{by_id.get(r[0], r[0])} {r[1]} {r[2]} {r[3]} {r[4]}" for r in rows[:12])
    return Cell("가용성", text or "없음(실측, 상태 바뀜 0)", {"rows": rows})


def cell_traps(remote, router: str, w: Window) -> Cell:
    rows = remote.ch(f"select trap_oid, count(), min(received_at) from lucida.snmp_traps_local where {span_received(w)} "
                     f"and source_ip = '{router}' group by 1 format TSV;")
    text = "; ".join(f"{r[0]} {r[1]}행(첫 {r[2]})" for r in rows)
    return Cell("trap", text or "없음(실측)", {"rows": rows})


def cell_icmp(remote, ids: dict[str, str], router_id: str | None, w: Window, base: list[Window]) -> Cell:
    rows = []
    for name, target in ids.items():
        low = t3_check.vm_scalar(remote, f'min_over_time({{__name__="sms.server.icmp_status",target_id="{target}"}}[{w.seconds}s])', w.end)
        rtt = t3_check.vm_scalar(remote, f'max_over_time({{__name__="sms.server.rtt",target_id="{target}"}}[{w.seconds}s])', w.end)
        ref = [t3_check.vm_scalar(remote, f'max_over_time({{__name__="sms.server.rtt",target_id="{target}"}}[{b.seconds}s])', b.end)
               for b in base]
        ref = [r for r in ref if r is not None]
        if low == 0 or (rtt is not None and ref and rtt > 1.5 * max(ref)):
            rows.append(f"{name} icmp 최소 {low}, rtt 최대 {rtt}(평시 최대 {max(ref) if ref else None})")
    if router_id:
        low = t3_check.vm_scalar(remote, f'min_over_time({{__name__="nms.network.icmp_status",target_id="{router_id}"}}[{w.seconds}s])', w.end)
        if low == 0:
            rows.append(f"라우터 icmp 최소 {low}")
    return Cell("ICMP", "; ".join(rows) or "없음(실측)", {"rows": rows})


def cell_config(remote, ids: dict[str, str], w: Window) -> Cell:
    rows = remote.pg("select target_id, resource_kind, prop_key, change_type, count(*) from target_config_history where "
                     f"changed_at between '{w.start.isoformat()}' and '{w.end.isoformat()}' and target_id in ({quote(ids.values())}) "
                     "group by 1, 2, 3, 4 order by 5 desc;")
    by_id = {v: k for k, v in ids.items()}
    text = "; ".join(f"{by_id.get(r[0], r[0])} {r[1]} {r[2]} {r[3]} {r[4]}건" for r in rows[:12])
    return Cell("SMS 구성 변경", text or "없음(실측)", {"rows": rows})


def cell_change_events(remote, ids: dict[str, str], w: Window) -> Cell:
    rows = remote.ch("select target_id, event_kind, count(), min(occurred_at) from lucida.lucida_events_local where "
                     f"{span_events(w)} and detector = 'change-detect' and target_id in ({quote(ids.values())}) group by 1, 2 format TSV;")
    by_id = {v: k for k, v in ids.items()}
    text = "; ".join(f"{by_id.get(r[0], r[0])} {r[1]} {r[2]}건(첫 {r[3]})" for r in rows)
    return Cell("변경 감지 이벤트", text or "없음(실측)", {"rows": rows})


# ---------------------------------------------------------------- Q7

def q7(remote, ctx: dict, ids: dict[str, str], account: str, uid: int, hv_account: str, hv_uid: int, words: list[str],
       inject: Window, w: Window, base: list[Window]) -> dict:
    """Q7 공통 낱말, Q7a(실행기 계정, uid, 가드 유닛 줄), Q7b(실행기 계정 표본 행, 주입 앞뒤 10분), Q7c(관리망 새 연결)."""
    sources = [s["addr"] for s in ctx["servers"]] + ([ctx["router"]["addr"]] if ctx["router"] else []) + [HV_SOURCE]
    for x in words:
        if x and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}", x):
            raise ValueError(f"가릴 낱말은 영숫자와 - 만: {x!r}")
    all_words = sorted(set(COMMON_WORDS) | {x.lower() for x in words if x})
    word_re = "|".join(all_words)
    word_hits = {
        "syslog_local": remote.ch(f"select count() from lucida.syslog_local where {span_received(w)} and source_ip in "
                                  f"({quote(sources)}) and match(lower(message), '(^|[^a-z0-9])({word_re})([^a-z0-9]|$)') format TSV;"),
        "lucida_logs_local": remote.ch(f"select count() from lucida.lucida_logs_local where {span_logs(w)} and host_target_id in "
                                       f"({quote(ids.values())}) and match(lower(body), '(^|[^a-z0-9])({word_re})([^a-z0-9]|$)') format TSV;"),
        "process_snapshot": remote.ch(f"select count() from lucida.process_snapshot where {span_process(w)} and target_id in "
                                      f"({quote(ids.values())}) and match(lower(concat(name, ' ', user)), '(^|[^a-z0-9])({word_re})([^a-z0-9]|$)') format TSV;"),
    }
    words_total = sum(int(v[0][0]) if v else 0 for v in word_hits.values())
    for name in (account, hv_account):
        if not re.fullmatch(r"[a-z][a-z0-9]{1,30}", name):
            raise ValueError(f"계정 이름 꼴이 아님: {name!r}")
    pipe = pipeline_patterns.check_ere([(account, uid), (hv_account, hv_uid)], ["t4-hv-exec"])
    q7a = remote.ch(f"select source_ip, count() from lucida.syslog_local where {span_received(w)} and source_ip in ({quote(sources)}) "
                    f"and match(message, '{pipe}') group by 1 format TSV;")
    q7a += remote.ch(f"select host_target_id, count() from lucida.lucida_logs_local where {span_logs(w)} and event_source = 'log_monitor' "
                     f"and host_target_id in ({quote(ids.values())}) and match(body, '{pipe}') group by 1 format TSV;")
    around = Window(inject.start - dt.timedelta(minutes=10), inject.start + dt.timedelta(minutes=10))
    operator = ctx["operator"]
    if not re.fullmatch(r"[a-z][a-z0-9]{1,30}", operator):
        raise ValueError(f"운영자 계정 이름 꼴이 아님: {operator!r}")
    vm_sources_all = [s["addr"] for s in ctx["servers"]] + ([ctx["router"]["addr"]] if ctx["router"] else [])
    vm_names_by_addr = ([(ctx["router"]["name"], ctx["router"]["addr"])] if ctx["router"] else []) + \
        [(s["name"], s["addr"]) for s in ctx["servers"]]

    def executor_rows(win: Window) -> int:
        rows = remote.ch(f"select count() from lucida.process_snapshot where {span_process(win)} and target_id in "
                         f"({quote(ids.values())}) and user = '{account}' format TSV;")
        return int(rows[0][0]) if rows else 0
    base20 = [Window(b.start, b.start + dt.timedelta(minutes=20)) for b in base]
    q7b_now, q7b_base = executor_rows(around), [executor_rows(b) for b in base20]
    # Q7c 는 계정이 아니라 출발 주소로 가른다(Q35 조건 ①, host_connections 에는 계정이 없다). 실행기 늘 열린 연결의 출발
    # 주소(관리망 host_addr)에서 온 새 연결만 배관 흔적이다. 운영자 단말 주소에서 온 연결은 원인 흔적이라 판정에서 빼고 따로 센다.
    executor_src, operator_src = ctx["executor_source"], ctx.get("operator_source")

    # 열쇠에 출발 포트를 넣는다(리뷰 rv-t4o M1): VM 으로 들어오는 ssh 는 늘 22 번이라 (대상, 출발 주소, 22) 만으로는 늘 열린
    # 연결과 새 연결이 같은 열쇠가 된다. 늘 열린 연결의 출발 포트는 그 수명 동안 같다.
    def mgmt_conns(win: Window, source: str) -> set[tuple[str, ...]]:
        return {tuple(r) for r in remote.ch(
            f"select target_id, remote_addr, toString(local_port), toString(remote_port) from lucida.host_connections "
            f"where {span_connections(win)} and target_id in ({quote(ids.values())}) and remote_addr = '{source}' "
            "group by 1, 2, 3, 4 format TSV;")}
    # "Seen" runs from the earliest baseline window up to the injection window (rv-t4o2 N1): a master reopened after
    # the baseline windows (redeploy, reboot, a network cut) has a new source port, and is not new at the injection.
    seen = mgmt_conns(Window(min(b.start for b in base20), around.start), executor_src) if base20 else set()
    new_conns = [list(r) for r in sorted(mgmt_conns(around, executor_src) - seen)]
    # 운영자 출발 연결 수(리뷰 L2): 주입 창과 같은 길이 평시 창들의 최댓값. 원인 흔적이라 판정은 없다.
    operator_conns_now = len(mgmt_conns(w, operator_src)) if operator_src else None
    operator_conns_base = [len(mgmt_conns(b, operator_src)) for b in base] if operator_src else []
    # 운영자 경로(Q35 조건 ②, ④): 운영자 단말 주소에서 온 로그인 수를 주입 창과 같은 길이 평시 창들의 최댓값으로 견주고,
    # 경로 줄(sshd 수락, 세션 열림, sudo TTY=pts, 세션 닫힘)이 119 에 닿았는지 센다. 판정은 사람이 한다(평시 세션이 있어야
    # 로그인이 주입 표지가 되지 않는다).
    # VM 별로도 낸다(지휘 세션 결정 M3): 한 VM 의 짧은 창에 로그인이 몰리는지 보려는 것이고 판정 기준은 두지 않는다.
    operator_paths = {}
    login_now, login_base = None, []
    by_vm: dict[str, dict[str, int]] = {}
    if operator_src:
        def logins(win: Window) -> dict[str, int]:
            rows = remote.ch(f"select source_ip, count() from lucida.syslog_local where {span_received(win)} "
                             f"and source_ip in ({quote(vm_sources_all)}) and app_name in ('sshd', 'sshd-session') "
                             f"and startsWith(message, 'Accepted publickey for {operator} from {operator_src} ') group by 1 format TSV;")
            return {r[0]: int(r[1]) for r in rows if len(r) == 2}
        now, base_counts = logins(w), [logins(b) for b in base]
        login_now, login_base = sum(now.values()), [sum(c.values()) for c in base_counts]
        for name, addr in vm_names_by_addr:
            by_vm[name] = {"window": now.get(addr, 0),
                           "baseline_max": max((c.get(addr, 0) for c in base_counts), default=0)}
        rows = remote.ch(
            "select countIf(startsWith(message, 'Accepted publickey for " + operator + " from " + operator_src + " ')), "
            "countIf(position(message, 'session opened for user " + operator + "(') > 0 and app_name in ('sshd', 'sshd-session')), "
            "countIf(app_name = 'sudo' and startsWith(message, '" + operator + " : TTY=pts/')), "
            "countIf(position(message, 'session closed for user " + operator + "') > 0 and app_name in ('sshd', 'sshd-session')) "
            f"from lucida.syslog_local where {span_received(w)} and source_ip in ({quote(vm_sources_all)}) format TSV;")
        values = [int(v) for v in rows[0]] if rows and len(rows[0]) == 4 else [0, 0, 0, 0]
        operator_paths = dict(zip(("sshd_accepted", "session_opened", "sudo_tty_pts", "session_closed"), values))
    # 실행기 자기 sudo 의 세션 닫힘 줄(pam_unix(sudo:session): session closed for user root)에는 계정 이름이 없어 VM 첫 규칙의
    # 이름 갈래로는 안 걸린다(리뷰 rv-t4s M3). 평시 점검 읽기도 같은 줄을 남기므로 0 이 아니라 평시 창 최댓값과 견준다.
    # 운영자 경로(router.config, vm.service)의 sudo 도 같은 닫힘 줄을 남긴다. 그것은 원인 흔적이라 배관 계수에서 뺀다(리뷰 L5).
    # sudo 줄에는 pid 가 없어(119 proc_id 빈칸, 11-19 읽기) 짝을 지을 수 없으므로, sudo 한 번이 명령 줄 하나와 닫힘 줄 하나를
    # 남기는 것을 써서 운영자 명령 줄(<운영자> : ...) 수만큼 닫힘 줄에서 뺀다. 실행기 명령 줄은 첫 규칙이 돌려 119 에 없다.
    vm_sources = vm_sources_all

    def sudo_closes(win: Window) -> tuple[int, int]:
        """(실행기 몫 닫힘 줄, 운영자 명령 줄)."""
        rows = remote.ch(f"select countIf(position(message, 'session closed for user root') > 0), "
                         f"countIf(startsWith(message, '{operator} : ')) from lucida.syslog_local where {span_received(win)} "
                         f"and source_ip in ({quote(vm_sources)}) and app_name = 'sudo' format TSV;")
        closes, commands = (int(rows[0][0]), int(rows[0][1])) if rows and len(rows[0]) >= 2 else (0, 0)
        return max(closes - commands, 0), commands
    (close_now, operator_now), close_base = sudo_closes(w), [sudo_closes(b)[0] for b in base]
    return {
        "words": all_words,
        "sudo_close_window": close_now, "sudo_close_baseline_max": max(close_base) if close_base else None,
        "operator_sudo_commands_window": operator_now,
        "q7_sudo_close": "충족" if close_base and close_now <= max(close_base) else "미충족", "word_hits": {k: int(v[0][0]) if v else 0 for k, v in word_hits.items()},
        "q7_common_words": "충족" if words_total == 0 else "미충족",
        "q7a_rows": q7a, "q7a": "충족" if not q7a else "미충족",
        "q7b_window": q7b_now, "q7b_baseline_max": max(q7b_base) if q7b_base else None,
        "q7b": "충족" if q7b_base and q7b_now <= max(q7b_base) else "미충족",
        "q7c_new": new_conns, "q7c": "충족" if not new_conns else "미충족", "q7c_source": executor_src,
        "operator_source": operator_src, "operator_logins_window": login_now,
        "operator_logins_baseline_max": max(login_base) if login_base else None, "operator_path_lines": operator_paths,
        "operator_logins_by_vm": by_vm, "operator_conns_window": operator_conns_now,
        "operator_conns_baseline_max": max(operator_conns_base) if operator_conns_base else None,
        "q7d": "표의 SMS 지표 칸의 관리 NIC 줄로 사람이 본다(관리 NIC 는 받아들임, v4 MNIC)",
    }


# ---------------------------------------------------------------- 표 한 줄

def row(executor: str, variant: str, cells: list[Cell], run_id: str, w: Window) -> str:
    by = {c.column: c.text for c in cells}
    when = f"{run_id}, {w.start.astimezone(KST):%m-%d %H:%M} ~ {w.end.astimezone(KST):%H:%M} KST"
    values = [executor, variant] + [by.get(c, "미측정, 질의 없음") for c in COLUMNS] + ["실측 뒤 사람이 적음", when]
    return "| " + " | ".join(v.replace("|", "/").replace("\n", " ") for v in values) + " |"


def collect(remote, inv: dict, start: dt.datetime, end: dt.datetime, executor: str, variant: str, run_id: str,
            words: list[str], baseline_windows: int, gap: dt.timedelta) -> dict:
    ctx = t3_check.context(inv)
    mgmt = next(net for net in inv["networks"].values() if net.get("management"))
    ctx["executor_source"] = mgmt["host_addr"]
    ctx["operator_source"] = mgmt.get("operator_host_addr")
    ids_all = t3_check.resolve_ids(remote, ctx)
    ids = {name: t["id"] for name, t in ids_all["servers"].items() if t}
    router_id = ids_all["router"]["id"] if ids_all.get("router") else None
    w, base = windows(start, end, baseline_windows, gap)
    names = {s["addr"]: s["name"] for s in ctx["servers"]}
    if ctx["router"]:
        names[ctx["router"]["addr"]] = ctx["router"]["name"]
    domain_sources = list(names)
    router_name = ctx["router"]["name"] if ctx["router"] else ""
    every = {**ids, **({router_name: router_id} if router_id else {})}
    cells = [
        guarded("SMS 지표", lambda: cell_series(remote, "SMS 지표", ids, "sms\\\\..+", w, base)),
        guarded("프로세스 표본", lambda: cell_process(remote, ids, w, base)),
        guarded("연결 표본", lambda: cell_connections(remote, ids, w, base)),
        guarded("로그 감시", lambda: cell_logs(remote, ids, w, base)),
        guarded("syslog", lambda: cell_syslog(remote, "syslog", domain_sources, names, w, base)),
        guarded("66 출발 syslog", lambda: cell_syslog(remote, "66 출발 syslog", [HV_SOURCE], {HV_SOURCE: "66"}, w, base)),
        guarded("가용성", lambda: cell_availability(remote, every, w)),
        guarded("NMS 지표", lambda: cell_series(remote, "NMS 지표", {router_name: router_id}, "nms\\\\..+", w, base)
                if router_id else Cell("NMS 지표", "미측정, 라우터 대상 번호 없음")),
        guarded("trap", lambda: cell_traps(remote, ctx["router"]["addr"], w) if ctx["router"] else Cell("trap", "미측정, 라우터 없음")),
        guarded("ICMP", lambda: cell_icmp(remote, ids, router_id, w, base)),
        guarded("SMS 구성 변경", lambda: cell_config(remote, ids, w)),
        guarded("변경 감지 이벤트", lambda: cell_change_events(remote, ids, w)),
    ]
    accounts = inv["accounts"]
    checks = q7(remote, ctx, ids, accounts["executor"]["name"], accounts["executor"]["uid"],
                inv.get("hypervisor", {}).get("executor_account", "hvjob"), int(inv.get("hypervisor", {}).get("executor_uid", 1460)),
                words, w, w, base)
    return {"row": row(executor, variant, cells, run_id, w), "cells": [dataclasses.asdict(c) for c in cells], "q7": checks,
            "window": [w.start.isoformat(), w.end.isoformat()],
            "baseline": [[b.start.isoformat(), b.end.isoformat()] for b in base]}


def main(argv: list[str] | None = None, remote_factory=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--executor", required=True)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--words", default="", help="가릴 낱말(시나리오 id, 작업 이름, 가드 파일 이름), 쉼표로")
    parser.add_argument("--baseline-windows", type=int, default=6)
    parser.add_argument("--baseline-gap-min", type=int, default=30)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    start, end = dt.datetime.fromisoformat(args.start), dt.datetime.fromisoformat(args.end)
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        parser.error("--start, --end 는 시간대가 있는 ISO 시각이고 end 가 start 뒤")
    if end - start > dt.timedelta(hours=3):
        parser.error("주입 창은 3시간 이하(질의 범위를 좁게 둔다)")
    inv = t3_check.load_inventory(args.inventory)
    ctx = t3_check.context(inv)
    remote = (remote_factory or (lambda: t3_check.Remote(ctx["monitor_real"], ctx["operator"])))()
    result = collect(remote, inv, start, end, args.executor, args.variant, args.run_id,
                     [w.strip() for w in args.words.split(",")], args.baseline_windows,
                     dt.timedelta(minutes=args.baseline_gap_min))
    print(result["row"])
    q = result["q7"]
    print(f"Q7 공통 낱말 {q['q7_common_words']} {q['word_hits']}, Q7a {q['q7a']} {q['q7a_rows']}, "
          f"Q7b {q['q7b']} 창 {q['q7b_window']} 평시 최대 {q['q7b_baseline_max']}, Q7c(출발 {q['q7c_source']}) {q['q7c']} {q['q7c_new']}, "
          f"운영자 로그인(출발 {q['operator_source']}) 창 {q['operator_logins_window']} 평시 최대 {q['operator_logins_baseline_max']}, "
          f"운영자 경로 줄 {q['operator_path_lines']}, VM 별 운영자 로그인(창, 평시 최대) "
          f"{ {n: (v['window'], v['baseline_max']) for n, v in q['operator_logins_by_vm'].items()} }, "
          f"운영자 출발 연결 창 {q['operator_conns_window']} 평시 최대 {q['operator_conns_baseline_max']}, "
          f"VM sudo 세션 닫힘 줄(운영자 몫 뺌) {q['q7_sudo_close']} 창 {q['sudo_close_window']} 평시 최대 {q['sudo_close_baseline_max']}, "
          f"운영자 sudo 명령 줄 {q['operator_sudo_commands_window']}")
    if args.json:
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
