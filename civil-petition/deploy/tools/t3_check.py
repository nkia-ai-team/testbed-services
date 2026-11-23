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
# NMS 행의 되돌아봄 창. 폴링 간격을 표본에서 재고 그 두 배(최소 15분)를 본다. collector-nms 는 첫 폴링에서만 장비 대상
# (target_id = 장비)으로 싣고, 그때 자원 하위 대상(targets.type network_resource, resource_kind cpu, memory, network_interface 등)을
# 만든 뒤 다음 폴링부터
# target_id = 하위 대상으로 싣는다. host_target_id = 장비 라벨은 처음부터 늘 있다(119 VM 실측 11-19, 폴링 60초).
# 그래서 장비 단위 질의는 target_id 가 아니라 host_target_id 로 고른다. 폴링 간격은 같은 계열의 이웃 표본 간격
# (VictoriaMetrics scrape_interval)을 1시간 안에서 재고, 계열마다 표본이 2개 미만이면 알 수 없음으로 둔다.
NMS_MIN_WINDOW_SEC = 900
NMS_POLL_LOOKBACK_SEC = 3600
LINK_DOWN = "1.3.6.1.6.3.1.1.5.3"
LINK_UP = "1.3.6.1.6.3.1.1.5.4"
# Q8 예산(설계 §1 Q8, §3-6). 기준값은 monitoring/t3-budget.json 한 곳에 둔다(검토 세션 Q31 답 반영, 11-19).
# 녹화 캡처 구간의 정의는 이 도구의 capture_intervals 한 곳이다.
BUDGET_FILE = DEPLOY / "monitoring" / "t3-budget.json"


def load_budget(path: Path = BUDGET_FILE) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("clickhouse_memory_resident_max_gib", "victoriametrics_memory_share_of_limit_max", "disk_used_pct_max",
                "steal_increase_max_pp", "cpu_busy_avg_max_pct", "tunnel_avg_mbps_reference",
                "capture_increment_estimate_gib", "capture_peak_share_of_limit_max"):
        if not isinstance(data.get(key), (int, float)):
            raise ValueError(f"{path}: {key} 가 수가 아님")
    return data
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
            router = {"name": host["name"], "addr": biz["addr"], "mgmt_addr": mnic["addr"], "up_if": biz["ifname"],
                      "downlinks": [n["ifname"] for n in host["nics"] if n.get("downlink")]}
        elif host["role"] == "loadgen":
            loadgen = {"name": host["name"], "addr": biz["addr"]}
    features = inv.get("features", {})
    return {"servers": servers, "router": router, "loadgen": loadgen, "operator": inv["accounts"]["operator"]["name"],
            "monitor_real": inv["hypervisor"]["monitor_real_addr"], "domain": inv["domain"],
            # The VM host keys live where apply.sh keeps them for this testbed (ST3 G3).
            "known_hosts": os.path.expanduser(f"~/.ssh/known_hosts_{inv['domain']}"),
            # S does not forward syslog (S 문서 결정 2): its servers must send none (ST3 G2).
            "syslog_forward": bool(features.get("syslog_forward", True)),
            # An SMS-only testbed (no router: S) has the ST3 rows "범위 밖 0" and "변경 감지" (ST3 G4).
            "sms_only": not features.get("router", True)}


def log_monitor_files(role: str, path: Path = LOG_MONITORS) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [dict(item) for item in data["common"] + data["roles"].get(role, [])]


# ---------------------------------------------------------------- 읽기 통로

class Remote:
    """119(ssh 로 파드 안 클라이언트), VictoriaMetrics(HTTP), VM(66 경유 관리망) 읽기."""

    def __init__(self, monitor_real: str, operator: str, known_hosts: str | None = None) -> None:
        secret = os.path.expanduser(f"~/.config/testbed/ssh-{monitor_real}")
        self.ssh119 = ["sshpass", "-f", secret, "ssh", "-o", "ConnectTimeout=15", f"root@{monitor_real}"]
        self.vm_url = f"http://{monitor_real}:{VM_PORT}"
        self.operator = operator
        self.known_hosts = known_hosts or os.path.expanduser("~/.ssh/known_hosts_civil-petition")

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
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "UserKnownHostsFile=" + self.known_hosts,
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


def item_mgmt_nic(remote, ctx, ids, until, table: dict | None = None) -> Item:
    """관리 NIC(Q7d)는 «받아들임»(설계 §10 T3 «빼거나 받아들인다고 기록», 11-19 지휘 세션 결정, v4 MNIC).

    «관리 안 함» 으로 빼는 길은 쓸 수 없다: 에이전트가 그 NIC 를 보고에서 빼면 collector-sms 승격이 행을 지우고 managed 로
    다시 만들며 가용성 이력도 지운다(제품 결함 후보 Q34). 그래서 충족 조건은 서버마다 ① 대상 번호 표에 관리 NIC 의
    받아들임 표시(management_nic.decision = accepted, ifname 이 인벤토리 관리 NIC) ② 119 하위 대상이 있고 표의 번호와 같음
    ③ 관리 상태 managed(관리 안 함이면 누가 다시 PUT 한 것) ④ 최근 10분 계열이 들어옴(받아들였으니 수집되는 것이 정상)이다.
    """
    it = Item("관리 NIC(Q7d)")
    bad = []
    if table is None:
        return it.set(False, "대상 번호 표(--table) 가 없어 받아들임 표시를 볼 수 없음")
    records = {rec.get("name"): rec for rec in table.get("servers", [])}
    for s in ctx["servers"]:
        target = ids["servers"].get(s["name"])
        if not target:
            bad.append(f"{s['name']} 대상 없음")
            continue
        mark = (records.get(s["name"]) or {}).get("management_nic") or {}
        nic = [c for c in ids["children"].get(target["id"], [])
               if c["resource_type"] == "sms.network_interface" and c["resource_key"] == s["mgmt_if"]]
        state = nic[0]["management_status"] if nic else "하위 대상 없음"
        recent = vm_scalar(remote, f'count(last_over_time({{__name__=~"sms\\\\.network_interface\\\\..+",'
                                   f'host_target_id="{target["id"]}",device="{s["mgmt_if"]}"}}[10m]))', until) or 0
        it.numbers.append(f"{s['name']} {s['mgmt_if']} 표 {mark.get('decision') or '표시 없음'}, {state}, 최근 10분 계열 {int(recent)}")
        if mark.get("decision") != "accepted" or mark.get("ifname") != s["mgmt_if"]:
            bad.append(f"{s['name']} 대상 번호 표에 관리 NIC {s['mgmt_if']} 받아들임 표시 없음")
        if not nic:
            bad.append(f"{s['name']} 관리 NIC {s['mgmt_if']} 하위 대상 없음")
            continue
        if mark.get("target_id") and mark["target_id"] != nic[0]["id"]:
            bad.append(f"{s['name']} 표의 관리 NIC 번호 {mark['target_id']} 가 119 하위 대상 {nic[0]['id']} 와 다름")
        if state != "managed":
            bad.append(f"{s['name']} 관리 NIC 가 {state}(받아들임이면 managed, 관리 안 함 PUT 을 하지 않는다)")
        if not recent:
            bad.append(f"{s['name']} 관리 NIC 최근 10분 계열 0(받아들였으면 수집돼야 함)")
    return it.set(not bad, "; ".join(bad) or "관리 NIC 수집을 받아들였다(대상 번호 표 표시, managed, 계열 들어옴)")


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
    # 설계 행은 «기록» 이다. 계열이 있으면 라벨을 적고 충족, 계열이 없으면 기록할 것이 없어 미충족(리뷰 L9).
    if "pid" not in keys or "name" not in keys:
        it.detail.append("단위 라벨이 이 설명(pid, name)과 다르다. 위 라벨을 그대로 기록한다")
    return it.set(bool(series), "" if series else "프로세스 계열 없음")


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


def nms_poll_seconds(remote, device_id: str, until) -> float | None:
    """장비 CPU 계열의 이웃 표본 간격(초). 하위 대상으로 옮겨 실린 계열까지 host_target_id 로 고른다(NMS_MIN_WINDOW_SEC 위 설명)."""
    value = vm_scalar(remote, f'max(scrape_interval({{__name__="nms.cpu.usage",host_target_id="{device_id}"}}'
                              f'[{NMS_POLL_LOOKBACK_SEC}s]))', until)
    return value if value and value > 0 else None


def nms_window_seconds(poll: float | None) -> int:
    """NMS 행 되돌아봄 창: 폴링 간격의 두 배(올림), 최소 NMS_MIN_WINDOW_SEC. 간격을 모르면 최소값."""
    if not poll:
        return NMS_MIN_WINDOW_SEC
    return max(NMS_MIN_WINDOW_SEC, -int(-2 * poll // 1))


def item_nms(remote, ctx, ids, until) -> Item:
    """라우터 상향과 하위 인터페이스마다 상태, 옥텟, 오류, 버림 계열과 장비 CPU(설계 §10 T3 NMS).

    인터페이스 이름(인벤토리 ifname)은 장비 하위 대상(network_resource, resource_kind network_interface)의 resource_key
    (ifDescr)이고, 그 meta.snmp_index 가 VM 계열의 snmp_index 라벨이다(119 조회 11-18: 기존 장비 하위 대상의 meta 칸,
    계열 라벨 target_id = 장비 또는 하위 대상, snmp_index). lo, 관리 NIC 처럼 설계 밖 인터페이스는 세지 않는다.
    CPU 는 host_target_id = 장비로 고르고, 창은 잰 폴링 간격에서 파생한다(NMS_MIN_WINDOW_SEC 위 설명). 계열이 창 안에
    있어야 충족이고 폴링 간격을 모르는 것만으로는 미충족이 아니다(등록 직후 첫 폴링만 있을 때).
    """
    it = Item("NMS")
    router = ids.get("router")
    if not ctx["router"]:
        it.verdict = "해당 없음"
        return it
    if not router:
        return it.set(False, "라우터 장비 대상 없음")
    bad = []
    poll = nms_poll_seconds(remote, router["id"], until)
    window = nms_window_seconds(poll)
    it.numbers.append(f"폴링 간격 {round(poll, 1)}초" if poll else
                      f"폴링 간격 알 수 없음(최근 {NMS_POLL_LOOKBACK_SEC // 60}분 안 같은 CPU 계열 표본 2개 미만)")
    it.numbers.append(f"창 {window}초(폴링 간격 2배, 최소 {NMS_MIN_WINDOW_SEC}초)")
    # 첫 폴링(장비 대상)과 그 뒤(하위 대상)가 한 창에 함께 들면 같은 CPU 가 두 계열이라 resource_key(CPU 번호)로 센다.
    cpu = len(vm_vector(remote, f'count by (resource_key) (last_over_time({{__name__="nms.cpu.usage",'
                                f'host_target_id="{router["id"]}"}}[{window}s]))', until))
    it.numbers.append(f"CPU {cpu}개")
    if not cpu:
        bad.append("CPU 없음(nms.cpu.usage)")
    rows = remote.pg("select coalesce(meta->>'resource_key',''), coalesce(meta->>'snmp_index',''), id from targets "
                     f"where meta->>'host_target_id' = '{router['id']}' and meta->>'resource_kind' = 'network_interface' "
                     "and deleting_at is null;")
    by_name = {key.strip(): (index, tid) for key, index, tid in rows}
    families = {label: names for label, names in NMS_FAMILIES.items() if label != "CPU"}
    regex = promql_regex([n for names in families.values() for n in names])
    for name in [ctx["router"]["up_if"]] + ctx["router"]["downlinks"]:
        if name not in by_name or not by_name[name][0]:
            bad.append(f"{name} 인터페이스 대상(snmp_index) 없음")
            it.numbers.append(f"{name} 대상 없음")
            continue
        index, child = by_name[name]
        found = {r["metric"]["__name__"] for r in vm_vector(
            remote, f'count by (__name__) (last_over_time({{__name__=~"{regex}",target_id=~"{regex_ids([router["id"], child])}",'
                    f'snmp_index="{index}"}}[{window}s]))', until)}
        missing = [label for label, names in families.items() if set(names) - found]
        it.numbers.append(f"{name}(snmp_index {index}) {'모두' if not missing else '빠짐 ' + ', '.join(missing)}")
        if missing:
            bad.append(f"{name} {', '.join(missing)} 없음")
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
    # 수신기는 관제 서버의 모든 주소에서 UDP 162 를 연다. 다른 장비가 보내기 시작한 trap 을 기록한다(판정 아님, 리뷰 L6).
    others = remote.ch("select source_ip, count() from lucida.snmp_traps_local "
                       f"where received_at >= '{ch_ts(since)}' and received_at <= '{ch_ts(until)}' "
                       f"and source_ip != '{ctx['router']['addr']}' group by source_ip format TSV;")
    it.numbers.append(f"참고: 라우터 아닌 출발 {sum(int(r[1]) for r in others)}행"
                      + (f"({', '.join(r[0] for r in others[:5])})" if others else ""))
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


def item_db_server(remote, ctx, ids, since, until, vm_reads: bool = False) -> Item:
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
    db_monitor = next(m for m in log_monitor_files("db") if "postgresql" in m["fileName"])
    db_log = db_monitor["fileName"]
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
    # 서버 로그 감시 줄은 로그 감시 행과 같은 규칙(11-18 지휘 세션 결정 8): 줄이 없을 때 파일에도 맞는 줄이 없으면 실패가
    # 아니라 따로 센다. VM 을 읽지 않았으면 실패.
    grep = {(db["name"], db_log): file_hits(remote, db, db_monitor)} if (vm_reads and not lines) else (None if not vm_reads else {})
    verdict, numbers, monitor_bad = monitor_verdict({(db["name"], db_log): lines} if lines else {}, grep, [(db["name"], db_log)])
    if lines == 0 and verdict == "충족":
        it.numbers.append("서버 로그 감시 줄 없음(파일에도 맞는 줄 없음)")
    bad += [f"서버 로그 감시: {b}" for b in monitor_bad]
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
    if not ctx.get("syslog_forward", True):
        return item_no_syslog(remote, ctx, since, until, it)
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


def item_no_syslog(remote, ctx, since, until, it: Item) -> Item:
    """A testbed that does not forward syslog (S): no row from its servers, router or load generator in the window
    (S 문서 ST3 "범위 밖 0", ST3 G2)."""
    addrs = [s["addr"] for s in ctx["servers"]] + ([ctx["router"]["addr"]] if ctx["router"] else []) \
        + ([ctx["loadgen"]["addr"]] if ctx["loadgen"] else [])
    rows = remote.ch(f"select source_ip, count() from lucida.syslog_local where received_at >= '{ch_ts(since)}' "
                     f"and received_at <= '{ch_ts(until)}' and source_ip in ({quote(addrs)}) group by 1 format TSV;")
    total = sum(int(r[1]) for r in rows if len(r) == 2)
    it.numbers.append(f"syslog 전달 끔: 서버, 부하기 출발 {total}행(0 이어야 함)")
    return it.set(total == 0, "; ".join(f"{r[0]} 출발 {r[1]}행" for r in rows if len(r) == 2 and int(r[1])))


OUT_OF_SCOPE_PREFIXES = ("nms", "dpm", "apm", "kcm")


def item_out_of_scope(remote, ctx, ids, since, until) -> Item:
    """S 문서 ST3 "범위 밖 0": S 대상(서버와 하위 대상)의 nms.*, dpm.*, apm.*, kcm.* 계열 0(ST3 G4). syslog 0 은 syslog 행."""
    it = Item("범위 밖 0")
    all_ids = [t["id"] for t in ids["servers"].values() if t]
    for host_id in list(all_ids):
        all_ids += [c["id"] for c in ids["children"].get(host_id, [])]
    if not all_ids:
        return it.set(False, "S 대상 없음")
    secs = max(int((until - since).total_seconds()), 60)
    bad = []
    for prefix in OUT_OF_SCOPE_PREFIXES:
        n = vm_scalar(remote, f'count(last_over_time({{__name__=~"{prefix}\\..+",target_id=~"{regex_ids(all_ids)}"}}[{secs}s]) '
                              f'or last_over_time({{__name__=~"{prefix}\\..+",host_target_id=~"{regex_ids(all_ids)}"}}[{secs}s]))',
                      until) or 0
        it.numbers.append(f"{prefix}.* 계열 {int(n)}")
        if n:
            bad.append(f"{prefix}.* {int(n)}")
    return it.set(not bad, "; ".join(bad))


def item_change_detect(remote, ctx, ids, since, until) -> Item:
    """S 문서 ST3 "변경 감지": 서버마다 change-detect 감시 대상(ai_coverages, 켜짐)에 들었는지(판정). 창 안
    change-detect 이벤트 수는 적어만 둔다(재시작 1회가 들어오는지 기록, 안 들어와도 실패가 아니다, ST3 G4)."""
    it = Item("변경 감지")
    host_ids = {name: t["id"] for name, t in ids["servers"].items() if t}
    missing = [s["name"] for s in ctx["servers"] if s["name"] not in host_ids]
    covered: set[str] = set()
    if host_ids:
        rows = remote.pg("select distinct target_id::text from ai_coverages where agent_id = 'change-detect' and enabled "
                         f"and target_id::text in ({quote(host_ids.values())});")
        covered = {r[0] for r in rows if r}
    events = []
    if host_ids:
        events = remote.ch("select target_id, count() from lucida.lucida_events_local where occurred_at >= "
                           f"'{ch_ts(since)}' and occurred_at <= '{ch_ts(until)}' and detector = 'change-detect' "
                           f"and target_id in ({quote(host_ids.values())}) group by 1 format TSV;")
    counts = {r[0]: int(r[1]) for r in events if len(r) == 2}
    bad = [f"{name} 대상 없음" for name in missing]
    for name, tid in host_ids.items():
        it.numbers.append(f"{name} 감시 대상 {'예' if tid in covered else '아니오'}, change-detect 이벤트 {counts.get(tid, 0)}건(기록만)")
        if tid not in covered:
            bad.append(f"{name} change-detect 감시 대상 아님")
    return it.set(not bad, "; ".join(bad))


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


def file_hits(remote, server: dict, monitor: dict) -> int:
    """VM 감시 파일에서 감시 패턴(레벨 모두)에 맞는 줄 수(읽기, 운영자 sudo, runbook §12-0 순서 11 의 녹화 정지 아래)."""
    pattern = "|".join(monitor[k] for k in ("fatalPattern", "errorPattern", "warnPattern", "infoPattern") if monitor.get(k))
    path = monitor["fileName"].replace("[YYYY]", "$(date -u +%Y)").replace("[MM]", "$(date -u +%m)").replace("[DD]", "$(date -u +%d)")
    out = remote.guest(server["mgmt_addr"], f"sudo -n grep -cE {shlex.quote(pattern)} \"{path}\" || true")
    return int((out.strip().splitlines() or ["0"])[-1] or 0)


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
                grep[(s["name"], m["fileName"])] = file_hits(remote, s, m)
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


def item_table(remote, table_path: Path | None, ctx: dict) -> Item:
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
    if len(table.get("servers", [])) != len(ctx["servers"]):
        it.detail.append(f"서버 {len(table.get('servers', []))}대(인벤토리 {len(ctx['servers'])}대)")
    if ctx["router"]:
        # 라우터가 있는 테스트베드는 장비, 그 인터페이스, trap 수신기 번호도 표에 있어야 한다(설계 §10 T3, 리뷰 L8).
        devices = table.get("network_devices", [])
        if len(devices) != 1:
            it.detail.append(f"장비 {len(devices)}개(1개여야 함)")
        elif not devices[0].get("interfaces"):
            it.detail.append("장비 인터페이스 번호가 비었음")
        if not table.get("trap_receivers"):
            it.detail.append("trap 수신기 번호가 비었음")
    return it.set(not missing and not it.detail, f"119 에 없는 번호 {missing[:5]}" if missing else "")


# ---------------------------------------------------------------- 녹화 캡처 구간(정의는 여기 한 곳)

CAPTURE_GAP_SEC = 600      # 이보다 가까운 내보내기는 한 캡처로 묶는다(캡처 한 번이 표 여럿을 잇달아 내보낸다)
CAPTURE_PAD_SEC = 60       # 구간 앞뒤 여유(MemoryResident 는 1초 표본이고 내보내기 뒤 메모리를 돌려주는 데 시간이 걸린다)
DUMP_WINDOW_KST = ("00:00:00", "02:00:00")   # scripts/dump-normal-segment.sh 의 평시 보호 구간(하루 한 번 덤프)


def capture_export_sql(start: dt.datetime, end: dt.datetime) -> str:
    """녹화 캡처 구간의 출처: 119 ClickHouse system.query_log 의 Parquet 내보내기(lucida 표, 끝났거나 실패한 것).

    캡처(scripts/capture-eval-case.sh)와 평시 구간 덤프(scripts/dump-normal-segment.sh)는 같은 꼴의
    `SELECT ... FROM lucida.<표> ... FORMAT Parquet` 를 같은 계정으로 보낸다. 시작, 끝 시각은 질의의 query_start_time
    과 event_time(끝난 시각)이다. 큰 열(질의 본문)은 범위 칸 두 개만 뽑아 읽는다.
    """
    return ("select toUnixTimestamp(query_start_time), toUnixTimestamp(event_time), "
            "extract(query, 'FROM (lucida[.][a-z_]+)'), "
            "extract(query, '>= parseDateTime64BestEffort[(].([^\\x27]+).[)]'), "
            "extract(query, '<= parseDateTime64BestEffort[(].([^\\x27]+).[)]') "
            "from system.query_log where event_date >= "
            f"'{ch_date(start - dt.timedelta(hours=1))}' and event_time >= '{ch_ts(start - dt.timedelta(hours=1))}' "
            f"and query_start_time <= '{ch_ts(end)}' and type in ('QueryFinish', 'ExceptionWhileProcessing') "
            "and query like '%FORMAT Parquet%' and query like '%FROM lucida.%' order by 1 format TSV;")


def is_normal_segment_dump(range_start: str, range_end: str) -> bool:
    """평시 구간 덤프인가: 내보낸 시각 범위가 정확히 하루의 00:00 ~ 02:00 KST(덤프 스크립트의 보호 구간)."""
    try:
        a = dt.datetime.fromisoformat(range_start.replace("Z", "+00:00")).astimezone(KST)
        b = dt.datetime.fromisoformat(range_end.replace("Z", "+00:00")).astimezone(KST)
    except ValueError:
        return False
    return (a.date() == b.date() and a.strftime("%H:%M:%S") == DUMP_WINDOW_KST[0]
            and b.strftime("%H:%M:%S") == DUMP_WINDOW_KST[1])


def capture_intervals_from_exports(rows: list[list[str]]) -> list[dict]:
    """내보내기 행(시작 epoch, 끝 epoch, 표, 범위 시작, 범위 끝)에서 녹화 캡처 구간을 만든다.

    평시 구간 덤프는 빼고(녹화가 아니라 날마다 도는 평시 작업이라 하루 최댓값에 남긴다), 남은 내보내기를 CAPTURE_GAP_SEC
    안이면 한 구간으로 묶어 앞뒤로 CAPTURE_PAD_SEC 씩 늘린다. 범위를 못 읽은 내보내기(시각 조건 없는 표)도 캡처로 센다.
    """
    exports = []
    for row in rows:
        if len(row) < 5:
            continue
        start, end, table, range_start, range_end = int(row[0]), int(row[1]), row[2], row[3], row[4]
        if is_normal_segment_dump(range_start, range_end):
            continue
        exports.append((start, max(end, start), table))
    intervals: list[dict] = []
    for start, end, table in sorted(exports):
        if intervals and start - intervals[-1]["_end"] <= CAPTURE_GAP_SEC:
            intervals[-1]["_end"] = max(intervals[-1]["_end"], end)
            intervals[-1]["tables"].add(table)
            intervals[-1]["exports"] += 1
        else:
            intervals.append({"_start": start, "_end": end, "tables": {table}, "exports": 1})
    out = []
    for item in intervals:
        a = item["_start"] - CAPTURE_PAD_SEC
        b = item["_end"] + CAPTURE_PAD_SEC
        out.append({"start": dt.datetime.fromtimestamp(a, KST).isoformat(), "end": dt.datetime.fromtimestamp(b, KST).isoformat(),
                    "start_epoch": a, "end_epoch": b, "exports": item["exports"], "tables": sorted(t for t in item["tables"] if t)})
    return out


def capture_intervals(remote, start: dt.datetime, end: dt.datetime) -> list[dict]:
    """판정 구간 [start, end] 와 겹치는 녹화 캡처 구간 목록."""
    rows = remote.ch(capture_export_sql(start, end))
    return [i for i in capture_intervals_from_exports(rows)
            if i["end_epoch"] >= int(start.timestamp()) and i["start_epoch"] <= int(end.timestamp())]


def _not_in(intervals: list[dict]) -> str:
    if not intervals:
        return "1"
    return "not (" + " or ".join(f"toUnixTimestamp(event_time) between {i['start_epoch']} and {i['end_epoch']}"
                                 for i in intervals) + ")"


def _in(intervals: list[dict]) -> str:
    if not intervals:
        return "0"
    return "(" + " or ".join(f"toUnixTimestamp(event_time) between {i['start_epoch']} and {i['end_epoch']}"
                             for i in intervals) + ")"


def parse_sadf_mem(text: str, start: dt.datetime, end: dt.datetime) -> float | None:
    """sadf -d -- -r 출력(hostname;interval;timestamp;kbmemfree;kbavail;...)의 구간 안 kbavail 최솟값(GiB)."""
    values = []
    for line in text.splitlines():
        parts = line.split(";")
        if len(parts) < 5 or line.startswith("#"):
            continue
        ts = dt.datetime.strptime(parts[2].replace(" UTC", ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        if start <= ts <= end:
            values.append(int(parts[4]))
    return round(min(values) / 1024 ** 2, 3) if values else None


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
    captures = capture_intervals(remote, start, end)
    span = (f"event_date >= '{ch_date(start)}' and event_time >= '{ch_ts(start)}' and event_time <= '{ch_ts(end)}' "
            "and metric = 'MemoryResident'")
    ch_mem = remote.ch(f"select max(value), quantile(0.99)(value), maxIf(value, {_not_in(captures)}), "
                       f"quantileIf(0.99)(value, {_not_in(captures)}), maxIf(value, {_in(captures)}), "
                       f"countIf({_in(captures)}) from system.asynchronous_metric_log where {span} format TSV;")
    ch_limit = remote.ch("select (select toUInt64(value) from system.server_settings where name = 'max_server_memory_usage'), "
                         "(select toUInt64(value) from system.asynchronous_metrics where metric = 'CGroupMemoryTotal') format TSV;")
    sadf_mem = remote.host(" ; ".join(f"LC_ALL=C sadf -d /var/log/sysstat/sa{d} -- -r 2>/dev/null" for d in _sadf_days(start, end)))
    vm_mem = vm_scalar(remote, f'max_over_time(process_resident_memory_bytes{{service_name="victoriametrics"}}[{secs}s])', end)
    limit = remote.host("k3s kubectl -n polestar get pods -o jsonpath='{range .items[*]}{.metadata.name} "
                        "{.spec.containers[0].resources.limits.memory}{\"\\n\"}{end}' | grep '^vmsingle-' | head -n 1")
    disk = remote.host("df -P / | awk 'NR==2 {print $5}'").strip().rstrip("%")
    sadf = remote.host(" ; ".join(f"LC_ALL=C sadf -d /var/log/sysstat/sa{d} -- -u 2>/dev/null" for d in _sadf_days(start, end)))
    wg = remote.host("cat /sys/class/net/wg0/statistics/rx_bytes /sys/class/net/wg0/statistics/tx_bytes 2>/dev/null; date +%s")
    wg_lines = wg.split()
    snap = {"start": start.astimezone(KST).isoformat(), "end": end.astimezone(KST).isoformat(),
            # ClickHouse 상주 메모리: 판정은 녹화 캡처 구간을 뺀 최댓값과 p99(검토 세션 Q31 답). 캡처 순간 최댓값은 따로 둔다.
            "ch_memory_resident_max_gib": _gib_of(ch_mem, 2),
            "ch_memory_resident_p99_gib": _gib_of(ch_mem, 3),
            "ch_capture_peak_gib": _gib_of(ch_mem, 4) if ch_mem and _num(ch_mem[0][5]) else None,
            "ch_memory_resident_max_gib_all": _gib_of(ch_mem, 0),
            "ch_memory_resident_p99_gib_all": _gib_of(ch_mem, 1),
            "capture_intervals": [{k: i[k] for k in ("start", "end", "exports", "tables")} for i in captures],
            "capture_definition": "system.query_log 의 lucida 표 Parquet 내보내기 가운데 평시 구간 덤프(00:00~02:00 KST 범위)를 뺀 것, "
                                  f"{CAPTURE_GAP_SEC}초 안은 묶고 앞뒤 {CAPTURE_PAD_SEC}초(t3_check.py capture_intervals)",
            "ch_memory_limit_gib": _limit_gib(ch_limit),
            "node_mem_available_min_gib": parse_sadf_mem(sadf_mem, start, end),
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


def _num(text) -> float | None:
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if value == value else None  # NaN 은 없음으로


def _gib_of(rows, index: int) -> float | None:
    value = _num(rows[0][index]) if rows and len(rows[0]) > index else None
    return round(value / 1024 ** 3, 3) if value else None


def _limit_gib(rows) -> float | None:
    """ClickHouse 메모리 상한: max_server_memory_usage 와 컨테이너(cgroup) 한도 중 낮은 것."""
    values = [v for v in (_num(x) for x in (rows[0] if rows else [])) if v]
    return round(min(values) / 1024 ** 3, 3) if values else None


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


def capture_margin_verdict(before: dict, after: dict, budget: dict | None = None) -> Item:
    """캡처 순간 최댓값 + T, S 추정 증가분이 ClickHouse 메모리 상한의 85% 아래인가(검토 세션 Q31 답 ①, 판정은 Q8 과 따로).

    넘을 것 같으면 T3 는 진행하고 ST3 전에 검토 세션에 다시 묻는다. 두 사실 가운데 큰 캡처 순간값을 쓴다.
    """
    budget = budget or load_budget()
    it = Item("캡처 순간 여유")
    peaks = [x for x in (before.get("ch_capture_peak_gib"), after.get("ch_capture_peak_gib")) if x is not None]
    limit_ch = after.get("ch_memory_limit_gib") or before.get("ch_memory_limit_gib")
    share = budget["capture_peak_share_of_limit_max"]
    plus = budget["capture_increment_estimate_gib"]
    if not peaks or not limit_ch:
        it.detail.append("두 사실 구간 어디에도 녹화 캡처가 없거나 상한을 못 읽어 보지 않음")
        return it
    peak = max(peaks)
    total = round(peak + plus, 3)
    line = round(limit_ch * share, 3)
    it.numbers.append(f"캡처 순간 최댓값 {peak} + T, S 추정 증가분 {plus} = {total} GiB, 상한 {limit_ch} 의 {int(share * 100)}% = {line}")
    return it.set(total < line, "" if total < line else "넘음: T3 는 진행하고 ST3 전에 검토 세션에 다시 묻는다")


def budget_verdict(before: dict, after: dict, budget: dict | None = None) -> Item:
    it = Item("부담(Q8)")
    budget = budget or load_budget()
    CH_MEM_BUDGET_GIB = budget["clickhouse_memory_resident_max_gib"]
    VM_MEM_SHARE_MAX = budget["victoriametrics_memory_share_of_limit_max"]
    DISK_PCT_MAX = budget["disk_used_pct_max"]
    STEAL_INCREASE_MAX_PP = budget["steal_increase_max_pp"]
    CPU_BUSY_MAX_PCT = budget["cpu_busy_avg_max_pct"]
    TUNNEL_AVG_MBPS_MAX = budget["tunnel_avg_mbps_reference"]
    bad = []
    mem, p99 = after.get("ch_memory_resident_max_gib"), after.get("ch_memory_resident_p99_gib")
    it.numbers.append(f"ClickHouse MemoryResident(녹화 캡처 구간 뺌) 최댓값 전 {before.get('ch_memory_resident_max_gib')} 뒤 {mem}, "
                      f"p99 전 {before.get('ch_memory_resident_p99_gib')} 뒤 {p99} GiB(예산 {CH_MEM_BUDGET_GIB} 미만)")
    for label, snap in (("전", before), ("뒤", after)):
        spans = snap.get("capture_intervals") or []
        it.numbers.append(f"뺀 캡처 구간({label}) {len(spans)}개" + (": " + ", ".join(f"{x['start']}~{x['end']}" for x in spans) if spans else ""))
    if mem is None or mem >= CH_MEM_BUDGET_GIB:
        bad.append("ClickHouse 메모리 예산(최댓값)")
    if p99 is None or p99 >= CH_MEM_BUDGET_GIB:
        bad.append("ClickHouse 메모리 예산(p99)")
    node, limit_ch = after.get("node_mem_available_min_gib"), after.get("ch_memory_limit_gib")
    # 노드 가용 메모리: ClickHouse 가 평소(p99)에서 자기 상한까지 자라도 노드에 들어갈 만큼 남아 있어야 한다(정의 t3-budget.json).
    need = round(limit_ch - p99, 3) if (limit_ch and p99) else None
    it.numbers.append(f"노드 가용 메모리 최솟값 전 {before.get('node_mem_available_min_gib')} 뒤 {node} GiB(ClickHouse 상한 {limit_ch} - p99 = {need} 이상)")
    if node is None or need is None or node < need:
        bad.append("노드 가용 메모리")
    vm, limit = after.get("vm_resident_max_gib"), after.get("vm_limit_gib")
    it.numbers.append(f"VictoriaMetrics 상주 최댓값 {vm} / 한도 {limit} GiB({int(VM_MEM_SHARE_MAX * 100)}% 미만)")
    if vm is None or not limit or vm >= VM_MEM_SHARE_MAX * limit:
        bad.append("VictoriaMetrics 메모리")
    disk = after.get("disk_pct_now")
    it.numbers.append(f"디스크 {disk}%({DISK_PCT_MAX}% 미만)")
    if disk is None or disk >= DISK_PCT_MAX:
        bad.append("디스크")
    steal_b, steal_a = (before.get("cpu") or {}).get("steal_avg"), (after.get("cpu") or {}).get("steal_avg")
    busy = (after.get("cpu") or {}).get("cpu_busy_avg")
    it.numbers.append(f"steal 평균 전 {steal_b} 뒤 {steal_a}(증가 {STEAL_INCREASE_MAX_PP}%p 이하), "
                      f"CPU({budget.get('cpu_busy_definition', 'user+nice+system')}) 평균 {busy}%({CPU_BUSY_MAX_PCT}% 이하)")
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
    if after.get("ch_memory_resident_max_gib_all") is not None:
        it.numbers.append(f"참고(판정 아님): 캡처 포함 최댓값 {after['ch_memory_resident_max_gib_all']}, 캡처 순간 최댓값 {after.get('ch_capture_peak_gib')}")
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
        before, after = json.loads(args.before.read_text()), json.loads(args.after.read_text())
        it = budget_verdict(before, after)
        margin = capture_margin_verdict(before, after)
        print(render([it, margin]))
        return 0 if it.verdict == "충족" else 1
    remote = remote_factory(ctx["monitor_real"], ctx["operator"]) if remote_factory \
        else Remote(ctx["monitor_real"], ctx["operator"], ctx["known_hosts"])
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
    table = json.loads(args.table.read_text(encoding="utf-8")) if args.table else None
    items = [
        item_sms(remote, ctx, ids, until),
        item_mgmt_nic(remote, ctx, ids, until, table),
        item_journal(remote, ctx, args.vm_reads),
        item_process_unit(remote, ctx, ids, until),
        item_icmp(remote, ctx, ids, until),
        item_nms(remote, ctx, ids, until),
        item_trap(remote, ctx, trap_since, until),
        item_no_dpm(remote, ctx, ids, since, until),
        item_db_server(remote, ctx, ids, since, until, args.vm_reads),
        item_syslog(remote, ctx, since, until),
        item_log_monitors(remote, ctx, ids, since, until, args.vm_reads),
        item_bands(remote, ctx, ids, since, until),
        item_table(remote, args.table, ctx),
    ]
    if ctx["sms_only"]:
        items += [item_out_of_scope(remote, ctx, ids, since, until), item_change_detect(remote, ctx, ids, since, until)]
    print(f"창 {since.astimezone(KST).isoformat()} ~ {until.astimezone(KST).isoformat()}")
    print(render(items))
    if args.json:
        args.json.write_text(json.dumps([dataclasses.asdict(i) for i in items], ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    return 0 if all(i.verdict in ("충족", "해당 없음") for i in items) else 1


if __name__ == "__main__":
    sys.exit(main())
