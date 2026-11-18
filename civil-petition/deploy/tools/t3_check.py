#!/usr/bin/env python3
"""T3(119 등록과 수집) 완료 조건 확인. 읽기 전용.

설계 정본: docs/spec-testbed-vm-3tier.md §10 T3 표(행마다 아래 항목 하나), Q8(119 자원 예산). 적용 순서:
docs/runbook-civil-petition-apply.md §12, §13.

읽는 곳(쓰기 없음):
  - 119 ClickHouse: 파드 안 clickhouse-client --readonly=1 --max_threads=2 --max_execution_time=60, 모든 질의에 시각 범위
  - 119 PostgreSQL: 파드 안 psql, read only 트랜잭션, statement_timeout 20초, 큰 열(본문, yaml, raw) 읽지 않음
  - 119 VictoriaMetrics: 104 에서 평가용 LB(:18428) 질의만
  - 119 호스트: df, sadf(sysstat), wg0 바이트 카운터, kubectl get(읽기)
  - (--vm-reads 일 때만) 서버 VM: 66 을 거쳐 관리망 운영자 계정으로 /proc 와 로그 파일 grep -c(읽기)
119 접속은 비밀번호 파일 ~/.config/testbed/ssh-<관제 서버 실주소> 를 sshpass -f 로만 쓴다(값을 인자에 두지 않는다).

쓰는 법:
  t3_check.py --inventory inventory/t.json --since <등록 시각> [--until <시각>] [--trap-since <시각>]
              [--table <대상 번호 표>] [--vm-reads] [--json <결과 파일>]
  t3_check.py --inventory inventory/t.json --budget --start <시각> --end <시각> --out <예산 사실 파일>
  t3_check.py --inventory inventory/t.json --budget-verdict --before <파일> --after <파일>
시각은 ISO(시간대 포함, 예 2026-11-19T15:00:00+09:00). Q8 은 등록 직전에 등록 전 24시간을, 등록 24시간 뒤에 등록 뒤
24시간을 같은 --budget 으로 재고 --budget-verdict 로 견준다(설계 §10 T3 "부담").
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEPLOY = HERE.parent
LOG_MONITORS = DEPLOY / "monitoring" / "log-monitors.json"
SERVER_ROLES = ("web", "was", "db")
KST = dt.timezone(dt.timedelta(hours=9))
UTC = dt.timezone.utc
VM_PORT = 18428
GUEST_GATEWAY = "root@192.168.200.66"

# 설계 §10 T3 "SMS": 4대 각각 이 이름군이 모두 있어야 한다(lucida-next collector-sms agent_ingest.go 의 발행 이름).
SMS_FAMILIES = {
    "CPU": "sms.cpu.",
    "메모리": "sms.memory.",
    "디스크": "sms.disk.",
    "파일 시스템": "sms.file_system.",
    "NIC": "sms.network_interface.",
    "프로세스": "sms.process.",
}
# 설계 §10 T3 "NMS": 라우터 인터페이스 상태, 옥텟, 오류, 버림, CPU(collector-nms 발행 이름).
NMS_FAMILIES = {
    "상태": ("nms.interface.if_oper_status",),
    "옥텟": ("nms.interface.if_in_octets", "nms.interface.if_out_octets"),
    "오류": ("nms.interface.if_in_errors", "nms.interface.if_out_errors"),
    "버림": ("nms.interface.if_in_discards", "nms.interface.if_out_discards"),
    "CPU": ("nms.cpu.usage",),
}
LINK_DOWN = "1.3.6.1.6.3.1.1.5.3"
LINK_UP = "1.3.6.1.6.3.1.1.5.4"
# Q8 예산(설계 §1 Q8, §3-6).
CH_MEM_BUDGET_GIB = 3.6
VM_MEM_SHARE_MAX = 0.80
DISK_PCT_MAX = 80.0
STEAL_INCREASE_MAX_PP = 3.0
CPU_BUSY_MAX_PCT = 70.0
TUNNEL_AVG_MBPS_MAX = 5.0
DPM_PREFIX = "dpm."


class QueryError(RuntimeError):
    pass


@dataclasses.dataclass
class Item:
    row: str
    verdict: str = "미판정"
    numbers: list[str] = dataclasses.field(default_factory=list)
    detail: list[str] = dataclasses.field(default_factory=list)

    def set(self, ok: bool, reason: str = "") -> "Item":
        self.verdict = "충족" if ok else "미충족"
        if reason:
            self.detail.append(reason)
        return self


# ---------------------------------------------------------------- 인벤토리

def load_inventory(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def context(inv: dict) -> dict:
    mgmt = next(name for name, net in inv["networks"].items() if net.get("management"))
    servers, router, loadgen = [], None, None
    for host in inv["hosts"]:
        biz = next((nic for nic in host["nics"] if nic.get("gateway") and nic["net"] != mgmt), None)
        mnic = next(nic for nic in host["nics"] if nic["net"] == mgmt)
        if host["role"] in SERVER_ROLES:
            mount = next((d["mount"] for d in host.get("disks", []) if d.get("mount")), None)
            servers.append({"name": host["name"], "role": host["role"], "addr": biz["addr"], "mgmt_if": mnic["ifname"],
                            "mgmt_addr": mnic["addr"], "data_mount": mount})
        elif host["role"] == "router":
            router = {"name": host["name"], "addr": biz["addr"], "mgmt_addr": mnic["addr"],
                      "downlinks": [n["ifname"] for n in host["nics"] if n.get("downlink")]}
        elif host["role"] == "loadgen":
            loadgen = {"name": host["name"], "addr": biz["addr"]}
    return {"servers": servers, "router": router, "loadgen": loadgen, "operator": inv["accounts"]["operator"]["name"],
            "monitor_real": inv["hypervisor"]["monitor_real_addr"], "domain": inv["domain"]}


def log_monitor_files(role: str, path: Path = LOG_MONITORS) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [dict(item) for item in data["common"] + data["roles"].get(role, [])]


# ---------------------------------------------------------------- 읽기 통로

class Remote:
    """119(ssh 로 파드 안 클라이언트), VictoriaMetrics(HTTP), VM(66 경유 관리망) 읽기."""

    def __init__(self, monitor_real: str, operator: str) -> None:
        secret = os.path.expanduser(f"~/.config/testbed/ssh-{monitor_real}")
        self.ssh119 = ["sshpass", "-f", secret, "ssh", "-o", "ConnectTimeout=15", f"root@{monitor_real}"]
        self.vm_url = f"http://{monitor_real}:{VM_PORT}"
        self.operator = operator

    @staticmethod
    def _run(cmd: list[str], stdin: str | None = None, timeout: int = 150) -> str:
        done = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
        if done.returncode != 0:
            raise QueryError(f"rc={done.returncode}: {done.stderr.strip()[:300]}")
        return done.stdout

    def ch(self, sql: str) -> list[list[str]]:
        remote = ("k3s kubectl -n polestar exec -i chi-clickhouse-main-0-0-0 -- clickhouse-client "
                  "--readonly=1 --max_threads=2 --max_execution_time=60 --multiquery")
        out = self._run(self.ssh119 + [remote], stdin=sql)
        return [line.split("\t") for line in out.splitlines() if line.strip()]

    def pg(self, sql: str) -> list[list[str]]:
        body = "set statement_timeout=20000;\nbegin read only;\n" + sql + "\ncommit;\n"
        remote = ("k3s kubectl -n polestar exec -i pg-1 -c postgres -- psql -U postgres -d lucida "
                  "-v ON_ERROR_STOP=1 -At -F '|'")
        out = self._run(self.ssh119 + [remote], stdin=body, timeout=90)
        return [line.split("|") for line in out.splitlines() if line.strip() and line not in ("SET", "BEGIN", "COMMIT")]

    def host(self, command: str) -> str:
        return self._run(self.ssh119 + [command], timeout=120)

    def vm(self, path: str, params: dict) -> dict:
        url = self.vm_url + path + "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=90) as resp:
            data = json.load(resp)
        if data.get("status") != "success":
            raise QueryError(f"vm {path}: {str(data)[:200]}")
        return data["data"]

    def guest(self, mgmt_addr: str, command: str) -> str:
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o",
               "UserKnownHostsFile=" + os.path.expanduser("~/.ssh/known_hosts_civil-petition"),
               "-J", GUEST_GATEWAY, f"{self.operator}@{mgmt_addr}", command]
        return self._run(cmd, timeout=60)


def vm_scalar(remote, query: str, at: dt.datetime | None = None) -> float | None:
    params = {"query": query}
    if at:
        params["time"] = int(at.timestamp())
    result = remote.vm("/api/v1/query", params)["result"]
    return float(result[0]["value"][1]) if result else None


def vm_vector(remote, query: str, at: dt.datetime | None = None) -> list[dict]:
    params = {"query": query}
    if at:
        params["time"] = int(at.timestamp())
    return remote.vm("/api/v1/query", params)["result"]


def ch_ts(t: dt.datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def ch_date(t: dt.datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%d")


def quote(values) -> str:
    return ", ".join("'" + str(v).replace("'", "") + "'" for v in values)


UUID = re.compile(r"^[0-9a-f-]{36}$")


def regex_ids(ids) -> str:
    """대상 번호(UUID)만 받아 PromQL 정규식 선택자 값으로 잇는다(글자가 이스케이프 없이 안전한 꼴만)."""
    ids = [str(i) for i in ids]
    bad = [i for i in ids if not UUID.match(i)]
    if bad:
        raise QueryError(f"대상 번호 꼴이 아님: {bad[:3]}")
    return "|".join(ids)


def promql_regex(names) -> str:
    """지표 이름 목록을 PromQL 문자열 안에 쓸 정규식으로 잇는다. 점 앞 역슬래시를 PromQL 문자열이 한 번 풀므로 두 겹으로 쓴다."""
    return "|".join(re.escape(n).replace("\\", "\\\\") for n in names)


# ---------------------------------------------------------------- 대상 번호

def resolve_ids(remote, ctx: dict) -> dict:
    """119 PG 에서 서버(이름 = 업무 주소), 하위 대상, 라우터 장비 대상 번호를 읽는다."""
    names = [s["addr"] for s in ctx["servers"]]
    rows = remote.pg("select id, name, coalesce(address,''), coalesce(registration_status,''), management_status "
                     f"from targets where type='server' and name in ({quote(names)}) and deleting_at is null;")
    by_name = {}
    for tid, name, address, reg, mstat in rows:
        by_name.setdefault(name, []).append({"id": tid, "address": address, "registration_status": reg,
                                             "management_status": mstat})
    servers = {}
    for s in ctx["servers"]:
        found = by_name.get(s["addr"], [])
        servers[s["name"]] = found[0] if len(found) == 1 else None
    host_ids = [v["id"] for v in servers.values() if v]
    children: dict[str, list[dict]] = {}
    if host_ids:
        for tid, host_id, rtype, rkey, mstat in remote.pg(
                "select id, meta->>'host_target_id', coalesce(meta->>'resource_type',''), coalesce(meta->>'resource_key',''), "
                f"management_status from targets where meta->>'host_target_id' in ({quote(host_ids)}) and id <> "
                "(meta->>'host_target_id')::uuid and deleting_at is null;"):
            children.setdefault(host_id, []).append({"id": tid, "resource_type": rtype, "resource_key": rkey,
                                                     "management_status": mstat})
    router = None
    if ctx["router"]:
        rows = remote.pg("select id, coalesce(registration_status,'') from targets where type='network' "
                         f"and address = '{ctx['router']['addr']}' and deleting_at is null;")
        router = {"id": rows[0][0], "registration_status": rows[0][1]} if len(rows) == 1 else None
    return {"servers": servers, "children": children, "router": router}


# ---------------------------------------------------------------- 항목

def item_sms(remote, ctx, ids, until) -> Item:
    it = Item("SMS")
    bad = []
    for s in ctx["servers"]:
        target = ids["servers"].get(s["name"])
        if not target:
            bad.append(f"{s['name']} 대상 없음(이름 {s['addr']} 인 승인된 server 대상이 1개가 아님)")
            continue
        names = {r["metric"].get("__name__", "") for r in vm_vector(
            remote, f'count by (__name__) (last_over_time({{host_target_id="{target["id"]}",__name__=~"sms\\\\..+"}}[15m]) '
                    f'or last_over_time({{target_id="{target["id"]}",__name__=~"sms\\\\..+"}}[15m]))', until)}
        missing = [label for label, prefix in SMS_FAMILIES.items() if not any(n.startswith(prefix) for n in names)]
        active = vm_scalar(remote, f'count(last_over_time({{host_target_id="{target["id"]}"}}[5m]) '
                                   f'or last_over_time({{target_id="{target["id"]}"}}[5m]))', until) or 0
        samples = vm_scalar(remote, f'max(count_over_time({{__name__="sms.cpu.utilization",host_target_id="{target["id"]}"}}[10m]))',
                            until) or 0
        interval = round(600 / samples, 1) if samples else None
        it.numbers.append(f"{s['name']} 활성 {int(active)}계열, 표본 간격 {interval}초")
        if missing:
            bad.append(f"{s['name']} 이름군 없음: {', '.join(missing)}")
    return it.set(not bad, "; ".join(bad))


def item_mgmt_nic(remote, ctx, ids, until) -> Item:
    it = Item("관리 NIC(Q7d)")
    bad = []
    for s in ctx["servers"]:
        target = ids["servers"].get(s["name"])
        if not target:
            bad.append(f"{s['name']} 대상 없음")
            continue
        nic = [c for c in ids["children"].get(target["id"], [])
               if c["resource_type"] == "sms.network_interface" and c["resource_key"] == s["mgmt_if"]]
        state = nic[0]["management_status"] if nic else "하위 대상 없음"
        recent = vm_scalar(remote, f'count(last_over_time({{__name__=~"sms\\\\.network_interface\\\\..+",'
                                   f'host_target_id="{target["id"]}",device="{s["mgmt_if"]}"}}[10m]))', until) or 0
        it.numbers.append(f"{s['name']} {s['mgmt_if']} {state}, 최근 10분 계열 {int(recent)}")
        if state != "unmanaged" or recent:
            bad.append(f"{s['name']} 관리 NIC 가 수집에서 빠지지 않음({state}, 계열 {int(recent)})")
    return it.set(not bad, "; ".join(bad) or "관리 NIC 를 관리 안 함으로 두어 에이전트 수집에서 뺐다")


def item_journal(remote, ctx, vm_reads: bool) -> Item:
    it = Item("journal")
    it.detail.append("코드(lucida-next d0186576d2 agent/sms/goagent): journal, sd_journal 을 읽는 곳 없음. 로그 감시는 서버가 "
                     "내려 준 파일만 tail")
    if not vm_reads:
        it.verdict = "미판정"
        it.detail.append("--vm-reads 없이는 VM 의 열린 파일을 보지 않는다")
        return it
    bad = []
    for s in ctx["servers"]:
        out = remote.guest(s["mgmt_addr"], "p=$(pgrep -x -u 0 lucida-agent) && sudo -n ls -l /proc/$p/fd | awk '{print $NF}'")
        journal = [line for line in out.splitlines() if "journal" in line]
        it.numbers.append(f"{s['name']} 열린 파일 {len(out.splitlines())}, journal {len(journal)}")
        if journal:
            bad.append(f"{s['name']} 에이전트가 journal 을 엶")
    return it.set(not bad, "; ".join(bad))


def item_process_unit(remote, ctx, ids, until) -> Item:
    it = Item("프로세스 계열 단위")
    target = next((ids["servers"][s["name"]] for s in ctx["servers"] if ids["servers"].get(s["name"])), None)
    if not target:
        return it.set(False, "서버 대상 없음")
    series = remote.vm("/api/v1/series", {"match[]": f'{{__name__="sms.process.cpu_utilization",host_target_id="{target["id"]}"}}',
                                           "start": int((until - dt.timedelta(minutes=10)).timestamp()),
                                           "end": int(until.timestamp())})
    keys = sorted({k for s in series for k in s if k not in ("__name__",)})
    it.numbers.append(f"라벨 {', '.join(keys)}")
    it.detail.append("프로세스 계열은 (호스트, 프로세스 이름, pid) 마다 하나다. 에이전트는 15초마다 CPU 상위 50개만 보낸다"
                     "(goagent main.go collectProcesses(cfg, 50)). process_snapshot 은 60초마다 전체 목록")
    return it.set("pid" in keys and "name" in keys, "" if "pid" in keys else "pid 라벨 없음")


def item_icmp(remote, ctx, ids, until) -> Item:
    it = Item("ICMP")
    bad = []
    host_ids = []
    for s in ctx["servers"]:
        target = ids["servers"].get(s["name"])
        if not target:
            bad.append(f"{s['name']} 대상 없음")
            continue
        host_ids.append(target["id"])
        status = vm_scalar(remote, f'last_over_time({{__name__="sms.server.icmp_status",target_id="{target["id"]}"}}[10m])', until)
        rtt = vm_scalar(remote, f'count(last_over_time({{__name__="sms.server.rtt",target_id="{target["id"]}"}}[10m]))', until)
        avail = remote.pg("select status from target_availability_latest where target_id = "
                          f"'{target['id']}' and resource_kind = 'server';")
        avail = avail[0][0] if avail else "없음"
        it.numbers.append(f"{s['name']} icmp {status}, rtt 계열 {int(rtt or 0)}, 주소 {target['address']}, 가용성 {avail}")
        if status != 1 or not rtt:
            bad.append(f"{s['name']} 서버 ICMP 상태 1 과 RTT 계열이 아님")
        if target["address"] != s["addr"]:
            bad.append(f"{s['name']} 대상 주소 {target['address']} 가 업무 주소 {s['addr']} 가 아님")
        if avail != "UP":
            bad.append(f"{s['name']} 가용성 {avail}")
    if host_ids:
        overrides = remote.pg("select count(*) from policy_device_overrides where kind = 'collection' and target_id in "
                              f"(select id from targets where id in ({quote(host_ids)}) or meta->>'host_target_id' in ({quote(host_ids)}));")
        count = int(overrides[0][0]) if overrides else -1
        it.numbers.append(f"수집 정책 장비 개별 덮어쓰기 {count}")
        if count != 0:
            bad.append(f"수집 정책 장비 개별 덮어쓰기 {count}건(0 이어야 함, 설계 §4-1)")
    router = ids.get("router")
    if ctx["router"]:
        if not router:
            bad.append("라우터 장비 대상 없음")
        else:
            status = vm_scalar(remote, f'last_over_time({{__name__="nms.network.icmp_status",target_id="{router["id"]}"}}[10m])', until)
            rtt = vm_scalar(remote, f'count(last_over_time({{__name__="nms.network.rtt",target_id="{router["id"]}"}}[10m]))', until)
            it.numbers.append(f"라우터 icmp {status}, rtt 계열 {int(rtt or 0)}")
            if status != 1 or not rtt:
                bad.append("라우터 NMS ICMP 상태 1 과 RTT 계열이 아님")
    return it.set(not bad, "; ".join(bad))


def item_nms(remote, ctx, ids, until) -> Item:
    it = Item("NMS")
    router = ids.get("router")
    if not ctx["router"]:
        it.verdict = "해당 없음"
        return it
    if not router:
        return it.set(False, "라우터 장비 대상 없음")
    bad = []
    for label, names in NMS_FAMILIES.items():
        regex = promql_regex(names)
        found = {r["metric"]["__name__"] for r in vm_vector(
            remote, f'count by (__name__) (last_over_time({{__name__=~"{regex}",target_id="{router["id"]}"}}[15m]))', until)}
        if set(names) - found:
            bad.append(f"{label} 없음({', '.join(sorted(set(names) - found))})")
    ifaces = vm_scalar(remote, f'count(count by (snmp_index) (last_over_time({{__name__="nms.interface.if_oper_status",'
                               f'target_id="{router["id"]}"}}[15m])))', until) or 0
    samples = vm_scalar(remote, f'count_over_time({{__name__="nms.cpu.usage",target_id="{router["id"]}"}}[10m])', until) or 0
    want = len(ctx["router"]["downlinks"]) + 1
    it.numbers.append(f"인터페이스 {int(ifaces)}개(하위 {len(ctx['router']['downlinks'])} + 상향 1 이상), "
                      f"폴링 간격 {round(600 / samples, 1) if samples else None}초")
    if ifaces < want:
        bad.append(f"인터페이스 {int(ifaces)} < {want}")
    return it.set(not bad, "; ".join(bad))


def item_trap(remote, ctx, since: dt.datetime | None, until) -> Item:
    it = Item("trap")
    if not ctx["router"]:
        it.verdict = "해당 없음"
        return it
    if not since:
        it.detail.append("--trap-since(라우터 하위 인터페이스를 내린 시각 직전) 가 없어 보지 않음")
        return it
    rows = remote.ch(
        "select trap_oid, count(), max(registered) from lucida.snmp_traps_local "
        f"where received_at >= '{ch_ts(since)}' and received_at <= '{ch_ts(until)}' and source_ip = '{ctx['router']['addr']}' "
        "group by trap_oid format TSV;")
    counts = {oid.lstrip("."): int(n) for oid, n, _ in rows}
    down = sum(n for oid, n in counts.items() if oid.startswith(LINK_DOWN))
    up = sum(n for oid, n in counts.items() if oid.startswith(LINK_UP))
    it.numbers.append(f"linkDown {down}, linkUp {up}(출발 {ctx['router']['addr']})")
    return it.set(down >= 1 and up >= 1, "" if down and up else "linkDown, linkUp 각 1행 이상이 아님")


def item_no_dpm(remote, ctx, ids, since, until) -> Item:
    it = Item("DPM 없음")
    all_ids = [t["id"] for t in ids["servers"].values() if t]
    for host_id in list(all_ids):
        all_ids += [c["id"] for c in ids["children"].get(host_id, [])]
    if ids.get("router"):
        all_ids.append(ids["router"]["id"])
    secs = max(int((until - since).total_seconds()), 60)
    dpm = 0.0
    if all_ids:
        dpm = vm_scalar(remote, f'count(last_over_time({{__name__=~"dpm\\\\..+",target_id=~"{regex_ids(all_ids)}"}}[{secs}s]) '
                                f'or last_over_time({{__name__=~"dpm\\\\..+",host_target_id=~"{regex_ids(all_ids)}"}}[{secs}s]))',
                        until) or 0
    db_addr = next(s["addr"] for s in ctx["servers"] if s["role"] == "db")
    rows = remote.pg(f"select count(*) from targets where type = 'database' and address = '{db_addr}' and deleting_at is null;")
    db_targets = int(rows[0][0]) if rows else -1
    it.numbers.append(f"dpm 계열 {int(dpm)}, DB 대상 {db_targets}")
    return it.set(dpm == 0 and db_targets == 0)


def item_db_server(remote, ctx, ids, since, until) -> Item:
    it = Item("DB 서버 관제")
    db = next(s for s in ctx["servers"] if s["role"] == "db")
    target = ids["servers"].get(db["name"])
    if not target:
        return it.set(False, "DB 서버 대상 없음")
    bad = []
    fs = vm_scalar(remote, f'count(last_over_time({{__name__=~"sms\\\\.file_system\\\\..+",host_target_id="{target["id"]}",'
                           f'mountpoint="{db["data_mount"]}"}}[15m]))', until) or 0
    disk = vm_scalar(remote, f'count(last_over_time({{__name__=~"sms\\\\.disk\\\\..+",host_target_id="{target["id"]}"}}[15m]) '
                             f'or last_over_time({{__name__=~"sms\\\\.disk\\\\..+",target_id="{target["id"]}"}}[15m]))', until) or 0
    procs = remote.ch("select count() from lucida.process_snapshot where ts >= "
                      f"'{ch_ts(until - dt.timedelta(minutes=10))}' and ts <= '{ch_ts(until)}' "
                      f"and target_id = '{target['id']}' and name like 'postgres%' format TSV;")
    procs = int(procs[0][0]) if procs else 0
    db_log = next(m["fileName"] for m in log_monitor_files("db") if "postgresql" in m["fileName"])
    lines = remote.ch("select count() from lucida.lucida_logs_local where timestamp_date >= "
                      f"'{ch_date(since)}' and timestamp >= '{ch_ts(since)}' and timestamp <= '{ch_ts(until)}' "
                      f"and event_source = 'log_monitor' and host_target_id = '{target['id']}' "
                      f"and log_attributes['file_path'] = '{db_log}' format TSV;")
    lines = int(lines[0][0]) if lines else 0
    it.numbers.append(f"데이터 디스크 {db['data_mount']} 계열 {int(fs)}, 디스크 IO 계열 {int(disk)}, "
                      f"PostgreSQL 프로세스 표본 행(10분) {procs}, 서버 로그 감시 줄 {lines}")
    if not fs:
        bad.append("데이터 디스크 파일 시스템 계열 없음")
    if not disk:
        bad.append("디스크 IO 계열 없음")
    if not procs:
        bad.append("PostgreSQL 프로세스 표본 없음")
    if not lines:
        bad.append("서버 로그 감시 줄 없음(로그 감시 항목의 줄 없음 판정을 본다)")
    return it.set(not bad, "; ".join(bad))


def syslog_verdict(rows: list[list[str]], expected: dict[str, str], forbidden: list[str], slots: int) -> tuple[bool, list[str], list[str]]:
    """rows: (출발 주소, 10분 칸 번호, 행 수). expected: 출발 주소 -> 이름. 칸마다 1행 이상, 금지 주소 0행."""
    per: dict[str, set[int]] = {addr: set() for addr in expected}
    banned = 0
    for addr, slot, count in rows:
        if addr in forbidden:
            banned += int(count)
        elif addr in per and int(count) > 0:
            per[addr].add(int(slot))
    numbers, bad = [], []
    for addr, name in expected.items():
        missing = slots - len(per[addr])
        numbers.append(f"{name}({addr}) 빈 10분 칸 {missing}/{slots}")
        if missing:
            bad.append(f"{name} 10분 칸 {missing}개에 행 없음")
    numbers.append(f"부하기 출발 {banned}행")
    if banned:
        bad.append(f"부하기 출발 행 {banned}")
    return not bad, numbers, bad


def item_syslog(remote, ctx, since, until) -> Item:
    it = Item("syslog")
    expected = {s["addr"]: s["name"] for s in ctx["servers"]}
    if ctx["router"]:
        expected[ctx["router"]["addr"]] = ctx["router"]["name"]
    forbidden = [ctx["loadgen"]["addr"]] if ctx["loadgen"] else []
    start = since.astimezone(UTC).replace(second=0, microsecond=0)
    start = start + dt.timedelta(minutes=(10 - start.minute % 10) % 10)
    slots = max(int((until - start).total_seconds() // 600), 0)
    if not slots:
        it.detail.append("판정 구간이 10분보다 짧음")
        return it
    addrs = list(expected) + forbidden
    rows = remote.ch(
        "select source_ip, intDiv(toUnixTimestamp(received_at) - toUnixTimestamp(toDateTime('"
        f"{ch_ts(start)}')), 600) slot, count() from lucida.syslog_local where received_at >= '{ch_ts(start)}' "
        f"and received_at < '{ch_ts(start + dt.timedelta(seconds=600 * slots))}' and source_ip in ({quote(addrs)}) "
        "group by 1, 2 format TSV;")
    ok, numbers, bad = syslog_verdict(rows, expected, forbidden, slots)
    it.numbers += numbers
    return it.set(ok, "; ".join(bad))


def monitor_verdict(rows: dict[tuple[str, str], int], grep: dict[tuple[str, str], int] | None,
                    expected: list[tuple[str, str]]) -> tuple[str, list[str], list[str]]:
    """감시 항목(서버 이름, 파일)마다 줄 수로 판정한다.

    줄이 있으면 통과. 줄이 없을 때 VM 파일에 패턴에 맞는 줄도 없으면 «줄 없음(파일에도 없음)» 으로 따로 세고(감시가
    못 읽는다는 증거가 아님), 파일에는 있는데 줄이 없으면 실패다. VM 을 읽지 않았으면(grep None) 줄 없음은 실패로 둔다.
    """
    numbers, bad, quiet = [], [], []
    for key in expected:
        count = rows.get(key, 0)
        if count:
            numbers.append(f"{key[0]} {key[1]} {count}")
            continue
        file_hits = None if grep is None else grep.get(key)
        if file_hits == 0:
            quiet.append(f"{key[0]} {key[1]}")
        else:
            bad.append(f"{key[0]} {key[1]} 줄 0(파일 맞는 줄 {file_hits if file_hits is not None else '안 봄'})")
    if quiet:
        numbers.append(f"줄 없음(파일에도 맞는 줄 없음) {len(quiet)}: {', '.join(quiet)}")
    return ("충족" if not bad else "미충족"), numbers, bad


def item_log_monitors(remote, ctx, ids, since, until, vm_reads: bool) -> Item:
    it = Item("로그 감시")
    expected, rows, grep = [], {}, {} if vm_reads else None
    for s in ctx["servers"]:
        target = ids["servers"].get(s["name"])
        files = log_monitor_files(s["role"])
        expected += [(s["name"], m["fileName"]) for m in files]
        if not target:
            continue
        registered = {r[0] for r in remote.pg(
            "select params->>'fileName' from collector_sms_custom_monitors where kind = 'log' and enabled "
            f"and parent_target_id = '{target['id']}';")}
        for m in files:
            if m["fileName"] not in registered:
                it.detail.append(f"{s['name']} {m['fileName']} 감시 등록 없음")
        for path, count in remote.ch(
                "select log_attributes['file_path'], count() from lucida.lucida_logs_local where timestamp_date >= "
                f"'{ch_date(since)}' and timestamp >= '{ch_ts(since)}' and timestamp <= '{ch_ts(until)}' and "
                f"event_source = 'log_monitor' and host_target_id = '{target['id']}' group by 1 format TSV;"):
            rows[(s["name"], path)] = int(count)
        if vm_reads:
            for m in files:
                pattern = "|".join(m[k] for k in ("fatalPattern", "errorPattern", "warnPattern", "infoPattern") if m.get(k))
                path = m["fileName"].replace("[YYYY]", "$(date -u +%Y)").replace("[MM]", "$(date -u +%m)").replace("[DD]", "$(date -u +%d)")
                out = remote.guest(s["mgmt_addr"], f"sudo -n grep -cE {shlex.quote(pattern)} \"{path}\" || true")
                grep[(s["name"], m["fileName"])] = int((out.strip().splitlines() or ["0"])[-1] or 0)
    # 날짜 자리가 든 파일은 줄의 file_path 가 그날 이름이다. 정의 이름으로 모은다.
    merged: dict[tuple[str, str], int] = {}
    for (name, path), count in rows.items():
        key = next(((n, f) for n, f in expected if n == name and _same_file(f, path)), (name, path))
        merged[key] = merged.get(key, 0) + count
    verdict, numbers, bad = monitor_verdict(merged, grep, expected)
    it.numbers += numbers
    it.verdict = verdict if not it.detail else "미충족"
    it.detail += bad
    return it


def _same_file(pattern: str, path: str) -> bool:
    regex = re.escape(pattern).replace(re.escape("[YYYY]"), r"\d{4}").replace(re.escape("[MM]"), r"\d{2}").replace(
        re.escape("[DD]"), r"\d{2}")
    return re.fullmatch(regex, path) is not None


def item_bands(remote, ctx, ids, since, until) -> Item:
    it = Item("탐지 밴드")
    if until - since < dt.timedelta(hours=24):
        it.detail.append("등록 뒤 24시간이 지나야 본다")
        return it
    bad = []
    targets = [(s["name"], ids["servers"].get(s["name"])) for s in ctx["servers"]]
    if ctx["router"]:
        targets.append((ctx["router"]["name"], ids.get("router")))
    for name, target in targets:
        if not target:
            bad.append(f"{name} 대상 없음")
            continue
        sel = f'__name__=~"(sms|nms)\\\\..+\\\\.bands"'
        count = vm_scalar(remote, f'count(last_over_time({{{sel},host_target_id="{target["id"]}"}}[1h]) '
                                  f'or last_over_time({{{sel},target_id="{target["id"]}"}}[1h]))', until) or 0
        inf = vm_scalar(remote, f'count(max_over_time({{{sel},host_target_id="{target["id"]}"}}[24h]) == +Inf '
                                f'or max_over_time({{{sel},target_id="{target["id"]}"}}[24h]) == +Inf)', until) or 0
        it.numbers.append(f"{name} 밴드 {int(count)}계열, +Inf {int(inf)}")
        if not count or inf:
            bad.append(f"{name} 밴드 {int(count)}, +Inf {int(inf)}")
    return it.set(not bad, "; ".join(bad))


def item_table(remote, table_path: Path | None) -> Item:
    it = Item("대상 번호 표")
    if not table_path:
        it.detail.append("--table 없음")
        return it
    table = json.loads(table_path.read_text(encoding="utf-8"))
    wanted = []
    for rec in table.get("servers", []):
        wanted.append(rec["target_id"])
        for kids in (rec.get("children") or {}).values():
            wanted += list(kids.values())
    for rec in table.get("network_devices", []):
        wanted.append(rec["target_id"])
        wanted += list((rec.get("interfaces") or {}).values())
    wanted += [rec["target_id"] for rec in table.get("trap_receivers", [])]
    rows = remote.pg(f"select id from targets where id in ({quote(wanted)}) and deleting_at is null;") if wanted else []
    missing = sorted(set(wanted) - {r[0] for r in rows})
    it.numbers.append(f"표의 대상 {len(wanted)}개, 119 에 없음 {len(missing)}")
    kinds = {k for rec in table.get("servers", []) for k in (rec.get("children") or {})}
    for want in ("sms.network_interface", "sms.file_system", "sms.process"):
        if want not in kinds:
            it.detail.append(f"서버 하위 대상 {want} 이 표에 없음")
    return it.set(not missing and not it.detail, f"119 에 없는 번호 {missing[:5]}" if missing else "")


# ---------------------------------------------------------------- Q8 예산

def _sadf_days(start: dt.datetime, end: dt.datetime) -> list[str]:
    days, day = [], start.astimezone(UTC).date()
    while day <= end.astimezone(UTC).date():
        days.append(day.strftime("%d"))
        day += dt.timedelta(days=1)
    return days


def parse_sadf(text: str, start: dt.datetime, end: dt.datetime) -> dict:
    """sadf -d -- -u 출력(hostname;interval;timestamp;CPU;%user;%nice;%system;%iowait;%steal;%idle)의 구간 평균."""
    rows = []
    for line in text.splitlines():
        parts = line.split(";")
        if len(parts) < 10 or line.startswith("#"):
            continue
        ts = dt.datetime.strptime(parts[2].replace(" UTC", ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        if not (start <= ts <= end):
            continue
        user, nice, system, iowait, steal, idle = (float(x) for x in parts[4:10])
        rows.append((user + nice + system, iowait, steal))
    if not rows:
        return {"samples": 0, "cpu_busy_avg": None, "iowait_avg": None, "steal_avg": None}
    n = len(rows)
    return {"samples": n, "cpu_busy_avg": round(sum(r[0] for r in rows) / n, 2),
            "iowait_avg": round(sum(r[1] for r in rows) / n, 2), "steal_avg": round(sum(r[2] for r in rows) / n, 2)}


def budget_snapshot(remote, ctx, ids, start: dt.datetime, end: dt.datetime) -> dict:
    secs = int((end - start).total_seconds())
    ch_mem = remote.ch("select max(value) from system.asynchronous_metric_log where event_date >= "
                       f"'{ch_date(start)}' and event_time >= '{ch_ts(start)}' and event_time <= '{ch_ts(end)}' "
                       "and metric = 'MemoryResident' format TSV;")
    vm_mem = vm_scalar(remote, f'max_over_time(process_resident_memory_bytes{{service_name="victoriametrics"}}[{secs}s])', end)
    limit = remote.host("k3s kubectl -n polestar get pods -o jsonpath='{range .items[*]}{.metadata.name} "
                        "{.spec.containers[0].resources.limits.memory}{\"\\n\"}{end}' | grep '^vmsingle-' | head -n 1")
    disk = remote.host("df -P / | awk 'NR==2 {print $5}'").strip().rstrip("%")
    sadf = remote.host(" ; ".join(f"LC_ALL=C sadf -d /var/log/sysstat/sa{d} -- -u 2>/dev/null" for d in _sadf_days(start, end)))
    wg = remote.host("cat /sys/class/net/wg0/statistics/rx_bytes /sys/class/net/wg0/statistics/tx_bytes 2>/dev/null; date +%s")
    wg_lines = wg.split()
    snap = {"start": start.astimezone(KST).isoformat(), "end": end.astimezone(KST).isoformat(),
            "ch_memory_resident_max_gib": round(float(ch_mem[0][0]) / 1024 ** 3, 3) if ch_mem and ch_mem[0][0] not in ("", "\\N") else None,
            "vm_resident_max_gib": round(vm_mem / 1024 ** 3, 3) if vm_mem else None,
            "vm_limit_gib": _gib(limit.split()[-1]) if limit.strip() else None,
            "disk_pct_now": float(disk) if disk else None,
            "cpu": parse_sadf(sadf, start, end),
            "wg0": {"rx_bytes": int(wg_lines[0]), "tx_bytes": int(wg_lines[1]), "at": int(wg_lines[2])} if len(wg_lines) == 3 else None}
    all_ids = [t["id"] for t in ids["servers"].values() if t]
    if ids.get("router"):
        all_ids.append(ids["router"]["id"])
    if all_ids:
        regex = regex_ids(all_ids)
        snap["reference"] = {
            "active_series": int(vm_scalar(remote, f'count(last_over_time({{host_target_id=~"{regex}"}}[5m]) '
                                                   f'or last_over_time({{target_id=~"{regex}"}}[5m]))', end) or 0),
            "unique_series": int(vm_scalar(remote, f'count(last_over_time({{host_target_id=~"{regex}"}}[{secs}s]) '
                                                   f'or last_over_time({{target_id=~"{regex}"}}[{secs}s]))', end) or 0),
            "rows": source_rows(remote, ctx, ids, start, end)}
    return snap


def _gib(text: str) -> float | None:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti)?", text.strip())
    if not match:
        return None
    scale = {"Ki": 1 / 1024 ** 2, "Mi": 1 / 1024, "Gi": 1.0, "Ti": 1024.0, None: 1 / 1024 ** 3}[match.group(2)]
    return round(float(match.group(1)) * scale, 3)


def source_rows(remote, ctx, ids, start, end) -> dict:
    host_ids = [t["id"] for t in ids["servers"].values() if t]
    addrs = [s["addr"] for s in ctx["servers"]] + ([ctx["router"]["addr"]] if ctx["router"] else [])
    span = f"'{ch_ts(start)}' and '{ch_ts(end)}'"
    queries = {
        "process_snapshot": f"select count() from lucida.process_snapshot where ts between {span} and target_id in ({quote(host_ids)})",
        "host_connections": f"select count() from lucida.host_connections where timestamp between {span} and target_id in ({quote(host_ids)})",
        "syslog_local": f"select count() from lucida.syslog_local where received_at between {span} and source_ip in ({quote(addrs)})",
        "lucida_logs_local": ("select count() from lucida.lucida_logs_local where timestamp_date >= "
                              f"'{ch_date(start)}' and timestamp between {span} and host_target_id in ({quote(host_ids)})"),
    }
    if ctx["router"]:
        queries["snmp_traps_local"] = (f"select count() from lucida.snmp_traps_local where received_at between {span} "
                                       f"and source_ip = '{ctx['router']['addr']}'")
    out = {}
    for name, sql in queries.items():
        rows = remote.ch(sql + " format TSV;") if host_ids or name in ("syslog_local", "snmp_traps_local") else [["0"]]
        out[name] = int(rows[0][0]) if rows else None
    return out


def budget_verdict(before: dict, after: dict) -> Item:
    it = Item("부담(Q8)")
    bad = []
    mem = after.get("ch_memory_resident_max_gib")
    it.numbers.append(f"ClickHouse MemoryResident 최댓값 전 {before.get('ch_memory_resident_max_gib')} 뒤 {mem} GiB(예산 {CH_MEM_BUDGET_GIB})")
    if mem is None or mem >= CH_MEM_BUDGET_GIB:
        bad.append("ClickHouse 메모리 예산")
    vm, limit = after.get("vm_resident_max_gib"), after.get("vm_limit_gib")
    it.numbers.append(f"VictoriaMetrics 상주 최댓값 {vm} / 한도 {limit} GiB(80% 미만)")
    if vm is None or not limit or vm >= VM_MEM_SHARE_MAX * limit:
        bad.append("VictoriaMetrics 메모리")
    disk = after.get("disk_pct_now")
    it.numbers.append(f"디스크 {disk}%(80% 미만)")
    if disk is None or disk >= DISK_PCT_MAX:
        bad.append("디스크")
    steal_b, steal_a = (before.get("cpu") or {}).get("steal_avg"), (after.get("cpu") or {}).get("steal_avg")
    busy = (after.get("cpu") or {}).get("cpu_busy_avg")
    it.numbers.append(f"steal 평균 전 {steal_b} 뒤 {steal_a}(증가 3%p 이하), CPU(user+nice+system) 평균 {busy}%(70% 이하)")
    if steal_a is None or steal_b is None or steal_a - steal_b > STEAL_INCREASE_MAX_PP:
        bad.append("steal 증가")
    if busy is None or busy > CPU_BUSY_MAX_PCT:
        bad.append("CPU")
    wb, wa = before.get("wg0"), after.get("wg0")
    if wb and wa and wa["at"] > wb["at"] and wa["rx_bytes"] >= wb["rx_bytes"] and wa["tx_bytes"] >= wb["tx_bytes"]:
        mbps = round((wa["rx_bytes"] - wb["rx_bytes"] + wa["tx_bytes"] - wb["tx_bytes"]) * 8 / (wa["at"] - wb["at"]) / 1e6, 3)
        it.numbers.append(f"터널 평균 {mbps} Mbps(두 사실 사이, 직렬 지점 기준 {TUNNEL_AVG_MBPS_MAX} 이하, 판정 아닌 별도 기준)")
        if mbps > TUNNEL_AVG_MBPS_MAX:
            it.detail.append("터널 평균이 직렬 지점 기준을 넘음(§3-6)")
    else:
        it.numbers.append("터널 평균: wg0 카운터가 두 사실에 다 있지 않거나 그 사이 줄어 계산 안 함")
    ref = after.get("reference")
    if ref:
        it.numbers.append(f"참고: 활성 계열 {ref['active_series']}, 구간 고유 계열 {ref['unique_series']}, 원천 행 {ref['rows']}")
    return it.set(not bad, "; ".join(bad))


# ---------------------------------------------------------------- 진입

def parse_time(text: str) -> dt.datetime:
    value = dt.datetime.fromisoformat(text)
    if value.tzinfo is None:
        raise SystemExit(f"시간대 없는 시각: {text}")
    return value


def render(items: list[Item]) -> str:
    lines = ["| 행(설계 §10 T3) | 판정 | 수치 |", "|---|---|---|"]
    for it in items:
        lines.append(f"| {it.row} | {it.verdict} | {'; '.join(it.numbers)} |")
    for it in items:
        if it.detail:
            lines.append(f"\n{it.row}: " + " / ".join(it.detail))
    return "\n".join(lines)


def main(argv: list[str] | None = None, remote_factory=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--since", help="등록 시각(창 시작)")
    parser.add_argument("--until", help="창 끝(없으면 지금)")
    parser.add_argument("--trap-since", help="라우터 하위 인터페이스를 내리기 직전 시각")
    parser.add_argument("--table", type=Path, help="대상 번호 표(t3_register --phase record 의 출력)")
    parser.add_argument("--vm-reads", action="store_true", help="VM 의 에이전트 열린 파일과 감시 파일 grep -c 를 읽는다")
    parser.add_argument("--json", type=Path, help="항목 결과를 JSON 으로 쓴다")
    parser.add_argument("--budget", action="store_true", help="Q8 예산 사실(--start, --end) 을 --out 에 쓴다")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--budget-verdict", action="store_true")
    parser.add_argument("--before", type=Path)
    parser.add_argument("--after", type=Path)
    args = parser.parse_args(argv)
    inv = load_inventory(args.inventory)
    ctx = context(inv)
    if args.budget_verdict:
        it = budget_verdict(json.loads(args.before.read_text()), json.loads(args.after.read_text()))
        print(render([it]))
        return 0 if it.verdict == "충족" else 1
    remote = (remote_factory or Remote)(ctx["monitor_real"], ctx["operator"])
    ids = resolve_ids(remote, ctx)
    if args.budget:
        if not (args.start and args.end and args.out):
            parser.error("--budget 은 --start, --end, --out 필요")
        snap = budget_snapshot(remote, ctx, ids, parse_time(args.start), parse_time(args.end))
        args.out.write_text(json.dumps(snap, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(snap, ensure_ascii=False))
        return 0
    if not args.since:
        parser.error("--since(등록 시각) 필요")
    since = parse_time(args.since)
    until = parse_time(args.until) if args.until else dt.datetime.now(UTC)
    trap_since = parse_time(args.trap_since) if args.trap_since else None
    items = [
        item_sms(remote, ctx, ids, until),
        item_mgmt_nic(remote, ctx, ids, until),
        item_journal(remote, ctx, args.vm_reads),
        item_process_unit(remote, ctx, ids, until),
        item_icmp(remote, ctx, ids, until),
        item_nms(remote, ctx, ids, until),
        item_trap(remote, ctx, trap_since, until),
        item_no_dpm(remote, ctx, ids, since, until),
        item_db_server(remote, ctx, ids, since, until),
        item_syslog(remote, ctx, since, until),
        item_log_monitors(remote, ctx, ids, since, until, args.vm_reads),
        item_bands(remote, ctx, ids, since, until),
        item_table(remote, args.table),
    ]
    print(f"창 {since.astimezone(KST).isoformat()} ~ {until.astimezone(KST).isoformat()}")
    print(render(items))
    if args.json:
        args.json.write_text(json.dumps([dataclasses.asdict(i) for i in items], ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    return 0 if all(i.verdict in ("충족", "해당 없음") for i in items) else 1


if __name__ == "__main__":
    sys.exit(main())
