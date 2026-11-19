#!/usr/bin/env python3
"""관제 서버(119)에 이 테스트베드의 SMS 서버, NMS 장비, trap 수신기, 로그 감시를 제품 API 로 등록한다.

설계 정본: docs/spec-testbed-vm-3tier.md §4(119 등록 대상), §4-1(ICMP 는 기본값 그대로), §10 T3.
적용 순서와 되돌리기: docs/runbook-civil-petition-apply.md §12.

화면(관제추가, 등록 대기 승인, 사용자 정의 감시, 관리 상태)이 부르는 것과 같은 경로만 쓴다(lucida-next develop-ai
d0186576d2 의 frontend registerTarget.ts, ServerPendingRegistrations.tsx, CustomMonitorsView.tsx, api/authz.ts).
수집 정책은 건드리지 않는다(장비 개별 덮어쓰기 없음, 서버 ICMP 는 기본 켬 그대로, 설계 §4-1).

단계(--phase, 순서대로):
  approve       에이전트가 처음 보고해 생긴 등록 대기 서버 대상(이름 = 업무 주소)을 승인한다
  nic           서버마다 관리 NIC 하위 대상이 생겼는지 본다(쓰기 없음). 관리 NIC 수집은 «받아들임»(설계 §10 T3, Q7d)
                이고 «관리 안 함» PUT 을 하지 않는다. 제품 승격(collector-sms)이 관리 안 함 NIC 행을 지우고 managed 로
                다시 만들며 그 NIC 가용성 이력도 지운다(11-19 119 실기, v4 MNIC, 제품 결함 후보 Q34)
  log-monitors  역할별 로그 감시(monitoring/log-monitors.json)를 서버마다 단다. 같은 파일 감시가 있으면 건너뛴다
  nms           라우터를 SNMP v2c 장비로 등록한다(커뮤니티는 인벤토리의 비밀값 파일에서 읽고 화면에 내지 않는다)
  trap          trap 수신기(udp 162)를 등록한다. 같은 (프로토콜, 포트) 수신기가 있으면 건너뛴다
  record        대상 번호 표(JSON)를 쓴다(--out). 쓰기 없음
  rollback      위에서 만든 것을 거꾸로 지운다(--record 의 표를 읽음. 서버 대상은 에이전트를 먼저 멈춘 뒤에만)

--yes 가 없으면 로그인(POST /api/v1/login, 관제 서버에 세션 행이 생긴다)과 읽기(GET)만 하고 쓸 호출은 찍기만 한다. 로그인 계정은 환경변수 LUCIDA_LOGIN_USER,
LUCIDA_LOGIN_PASSWORD(러너 컨테이너와 같은 이름)에서만 읽고, 주소는 LUCIDA_QUERY_URL(없으면 인벤토리 관제 서버
실주소의 13080). 비밀값(비밀번호, 커뮤니티)은 출력하지 않는다.

모든 호출에 화면과 같은 Origin 머리를 싣는다. query 의 corsMiddleware(lucida-next backend/services/query/main.go)는
쿠키가 있는 GET 아닌 요청에 허용 목록(LUCIDA_ALLOWED_ORIGINS, 설치기가 PUBLIC_URL 과 게이트웨이 포트에서 파생)과
글자로 같은 Origin 을 요구하고, 없거나 다르면 403 forbidden origin 이다. 값은 관제 주소의 scheme://host[:port]
(브라우저처럼 http 80, https 443 은 생략)이고 LUCIDA_WEB_ORIGIN 으로 덮어쓴다.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEPLOY = HERE.parent
LOG_MONITORS = DEPLOY / "monitoring" / "log-monitors.json"
SERVER_ROLES = ("web", "was", "db")
# 화면 기본 수집 정책(NMS). registerTarget.ts, targets.ts 의 기본 정책 id.
NMS_DEFAULT_POLICY = "00000000-0000-4000-8009-000000000001"
TRAP_PORT = 162
TRAP_PROTOCOL = "udp"
PHASES = ("approve", "nic", "log-monitors", "nms", "trap", "record", "rollback")
LOG_PATTERN_KEYS = ("debug", "info", "warn", "error", "fatal")
# 관제 주소에서 파생한 Origin 대신 쓸 값(scheme://host[:port]). 허용 목록이 다른 주소일 때만 둔다.
ORIGIN_ENV = "LUCIDA_WEB_ORIGIN"


class RegisterError(RuntimeError):
    pass


# ---------------------------------------------------------------- 인벤토리에서 읽는 값

def load_inventory(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _management(inv: dict) -> str:
    return next(name for name, net in inv["networks"].items() if net.get("management"))


def servers(inv: dict) -> list[dict]:
    """서버 역할 호스트마다 이름, 역할, 업무 주소(= 에이전트 이름 LUCIDA_HOST), 관리 NIC 이름."""
    mgmt = _management(inv)
    out = []
    for host in inv["hosts"]:
        if host["role"] not in SERVER_ROLES:
            continue
        biz = next(nic for nic in host["nics"] if nic.get("gateway") and nic["net"] != mgmt)
        mnic = next(nic for nic in host["nics"] if nic["net"] == mgmt)
        out.append({"name": host["name"], "role": host["role"], "agent_host": biz["addr"],
                    "mgmt_if": mnic["ifname"], "biz_if": biz["ifname"]})
    return out


def router(inv: dict) -> dict | None:
    host = next((h for h in inv["hosts"] if h["role"] == "router"), None)
    if not host:
        return None
    up = next(nic for nic in host["nics"] if nic.get("gateway"))
    return {"name": host["name"], "address": up["addr"],
            "downlinks": [nic["ifname"] for nic in host["nics"] if nic.get("downlink")]}


def log_monitor_specs(role: str, path: Path = LOG_MONITORS) -> list[dict]:
    """역할의 로그 감시 정의(공통 + 역할). 각 항목은 이름과 화면이 보내는 params 전체."""
    data = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for item in data["common"] + data["roles"].get(role, []):
        params = dict(data["defaults"])
        params["fileName"] = item["fileName"]
        for key in LOG_PATTERN_KEYS:
            params[f"{key}Pattern"] = item.get(f"{key}Pattern", "")
            params[f"{key}ExcludePattern"] = ""
        out.append({"name": item["name"], "params": params,
                    "unified_log_enabled": bool(data.get("monitor_defaults", {}).get("unified_log_enabled", False)),
                    "log_type_tag": item.get("logTypeTag", "")})
    return out


def expand(path: str) -> Path:
    return Path(os.path.expanduser(path))


# ---------------------------------------------------------------- HTTP

def web_origin(base: str, override: str | None = None) -> str:
    """화면이 싣는 Origin. 브라우저처럼 scheme://host[:port] 이고 스킴 기본 포트는 생략한다.

    lucida-next deploy/src/ap/install-layout.sh _polestar_origin 과 같은 규칙이다(허용 목록이 이 꼴로 만들어지고
    query 는 글자로 같은지만 본다). override 는 그대로 쓰되 경로, 질의, 사용자 정보가 붙은 값은 거절한다
    (붙으면 허용 목록과 영영 맞지 않는다).
    """
    if override:
        parts = urllib.parse.urlsplit(override)
        if (parts.scheme not in ("http", "https") or not parts.hostname or parts.path or parts.query
                or parts.fragment or "@" in parts.netloc):
            raise RegisterError(f"{ORIGIN_ENV} 는 scheme://host[:port] 꼴이어야 함: {override!r}")
        return override
    parts = urllib.parse.urlsplit(base)
    scheme = parts.scheme.lower()
    host = parts.hostname
    if scheme not in ("http", "https") or not host:
        raise RegisterError(f"관제 주소에서 Origin 을 만들 수 없음: {base!r}")
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    if port is None or (scheme, port) in (("http", 80), ("https", 443)):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


class Client:
    """세션 쿠키 하나로 query(/api/v1)와 SMS 수집기(/sm/api, 웹 게이트웨이 경유)를 부른다.

    모든 호출에 Origin 을 싣는다(모듈 머리말). origin 을 주지 않으면 LUCIDA_WEB_ORIGIN, 없으면 base 에서 파생한다.
    """

    def __init__(self, base: str, timeout: float = 30.0, origin: str | None = None) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.origin = web_origin(self.base, origin if origin is not None else os.environ.get(ORIGIN_ENV))
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def login(self, user: str, password: str) -> None:
        status, body = self.request("POST", "/api/v1/login", {"username": user, "password": password})
        if status != 200:
            raise RegisterError(f"로그인 실패 {status}")

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, object]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", "Accept": "application/json",
                                              "Origin": self.origin})
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as error:
            raw, status = error.read(), error.code
        try:
            return status, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return status, raw.decode("utf-8", "replace")[:300]


def _id(obj: object) -> str | None:
    if not isinstance(obj, dict):
        return None
    for key in ("id", "target_id"):
        if isinstance(obj.get(key), str):
            return obj[key]
    for key in ("target", "collector", "custom_monitor", "monitor"):
        if isinstance(obj.get(key), dict):
            return _id(obj[key])
    return None


class Session:
    """GET 은 늘 하고, 쓰기(POST, PUT, DELETE)는 yes 일 때만 한다. 쓸 호출은 모두 찍는다(비밀값은 가림)."""

    def __init__(self, client, yes: bool, out=sys.stdout) -> None:
        self.client, self.yes, self.out = client, yes, out
        self.writes: list[tuple[str, str]] = []

    def get(self, path: str) -> object:
        status, body = self.client.request("GET", path)
        if status != 200:
            raise RegisterError(f"GET {path} -> {status} {str(body)[:200]}")
        return body

    def write(self, method: str, path: str, body: dict | None = None, shown: dict | None = None,
              ok: tuple[int, ...] = (200, 201, 204)) -> tuple[int, object]:
        shown_text = json.dumps(shown if shown is not None else body, ensure_ascii=False) if body is not None else ""
        self.writes.append((method, path))
        if not self.yes:
            print(f"(찍기만) {method} {path} {shown_text}".rstrip(), file=self.out)
            return 0, None
        status, resp = self.client.request(method, path, body)
        print(f"{method} {path} -> {status}", file=self.out)
        if status not in ok:
            raise RegisterError(f"{method} {path} -> {status} {str(resp)[:300]}")
        return status, resp


def _targets(session: Session, **query) -> list[dict]:
    body = session.get("/api/v1/targets?" + urllib.parse.urlencode(query))
    return list((body or {}).get("targets") or []) if isinstance(body, dict) else []


def find_server(session: Session, agent_host: str, pending: bool) -> list[dict]:
    query = {"type": "server", "search": agent_host, "limit": 500}
    if pending:
        query["registration_status"] = "pending"
    return [t for t in _targets(session, **query) if t.get("name") == agent_host]


def subtree(session: Session, target_id: str) -> list[dict]:
    body = session.get(f"/api/v1/targets/{target_id}/subtree")
    return list((body or {}).get("nodes") or []) if isinstance(body, dict) else []


# ---------------------------------------------------------------- 단계

def phase_approve(session: Session, inv: dict) -> list[str]:
    """등록 대기 서버를 이름(업무 주소)으로 찾아 한 번에 승인한다. 이미 승인된 서버는 건너뛴다."""
    ids, problems = [], []
    for host in servers(inv):
        pending = find_server(session, host["agent_host"], pending=True)
        approved = [t for t in find_server(session, host["agent_host"], pending=False)
                    if t.get("registration_status") in (None, "", "approved")]
        if approved and not pending:
            print(f"{host['name']}: 이미 승인됨 {approved[0].get('id')}", file=session.out)
            continue
        if len(pending) != 1:
            problems.append(f"{host['name']}: 이름 {host['agent_host']} 의 등록 대기 서버가 {len(pending)}개(1개여야 함."
                            " 에이전트가 아직 보고하지 않았거나 이름이 다름)")
            continue
        if pending[0].get("address") not in (None, "", host["agent_host"]):
            problems.append(f"{host['name']}: 등록 대기 대상 주소 {pending[0].get('address')} 가 업무 주소가 아님"
                            "(관리 주소면 ICMP 가 닿지 않는다. 승인하지 않음)")
            continue
        ids.append(pending[0]["id"])
    if problems:
        raise RegisterError("; ".join(problems))
    if ids:
        session.write("POST", "/api/v1/targets/approve", {"ids": ids})
    return ids


def server_id(session: Session, host: dict) -> str:
    found = [t for t in find_server(session, host["agent_host"], pending=False)
             if t.get("registration_status") in (None, "", "approved")]
    if len(found) != 1:
        raise RegisterError(f"{host['name']}: 승인된 서버 대상(이름 {host['agent_host']})이 {len(found)}개")
    return found[0]["id"]


MGMT_NIC_DECISION = "accepted"


def phase_nic(session: Session, inv: dict) -> list[str]:
    """관리 NIC 받아들임 확인(쓰기 없음). 하위 대상이 아직 없으면(첫 지표 전) 멈추고 다시 돌리라고 알린다.

    «관리 안 함» 으로 두지 않는다(모듈 머리말 nic). 누가 이미 관리 안 함으로 두었어도 되돌리거나 다시 쓰지 않고 찍기만 한다.
    """
    done, problems = [], []
    for host in servers(inv):
        sid = server_id(session, host)
        nics = [n for n in subtree(session, sid)
                if n.get("resource_type") == "sms.network_interface" and n.get("resource_key") == host["mgmt_if"]]
        if len(nics) != 1:
            problems.append(f"{host['name']}: 관리 NIC {host['mgmt_if']} 하위 대상이 {len(nics)}개(승인 뒤 첫 지표를 기다려 다시)")
            continue
        node = nics[0]
        state = node.get("management_status") or "managed"
        note = "" if state == "managed" else "(관리 안 함이어도 손대지 않는다. 제품 승격이 지우고 managed 로 다시 만든다)"
        print(f"{host['name']}: {host['mgmt_if']} 받아들임 {node['id']} {state}{note}", file=session.out)
        done.append(node["id"])
    if problems:
        raise RegisterError("; ".join(problems))
    return done


def _monitors(session: Session, parent: str) -> list[dict]:
    body = session.get("/sm/api/custom-monitors?" + urllib.parse.urlencode({"kind": "log", "parent": parent, "owner": "sms"}))
    return list((body or {}).get("custom_monitors") or []) if isinstance(body, dict) else []


def _monitor_file(monitor: dict) -> str | None:
    params = monitor.get("params")
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except json.JSONDecodeError:
            params = {}
    return (params or {}).get("fileName")


def phase_log_monitors(session: Session, inv: dict, specs_path: Path = LOG_MONITORS) -> int:
    """화면 사용자 정의 감시(로그)와 같은 몸통. 같은 파일을 보는 감시가 이미 있으면 건너뛴다(다시 돌려도 같은 상태)."""
    added = 0
    for host in servers(inv):
        sid = server_id(session, host)
        have = {_monitor_file(m) for m in _monitors(session, sid)}
        for spec in log_monitor_specs(host["role"], specs_path):
            if spec["params"]["fileName"] in have:
                continue
            # 감시 꼴(unified_log_enabled, log_type_tag)은 기존 세 도메인 감시와 같게(11-18 지휘 세션 결정, 정의 파일).
            body = {"parent_target_id": sid, "kind": "log", "name": f"{spec['name']}-{host['name']}",
                    "description": "", "enabled": True, "unified_log_enabled": spec["unified_log_enabled"],
                    "params": spec["params"]}
            if spec["log_type_tag"]:
                body["log_type_tag"] = spec["log_type_tag"]
            session.write("POST", "/sm/api/custom-monitors", body)
            added += 1
    return added


def _network_targets(session: Session, **query) -> list[tuple[dict, list[dict]]]:
    """network 대상과 그 수집기의 쌍. 실제 응답은 수집기를 대상 안에 넣지 않고 최상위 target_collectors 에
    {"target": 대상, "collectors": [수집기]} 로 따로 준다(lucida-next d0186576d2 query/handler/targets.go 목록,
    store.Target 에는 수집기 칸이 없다). 대상 하나에 쌍 하나다."""
    body = session.get("/api/v1/targets?" + urllib.parse.urlencode(dict(type="network", limit=5000, include_collectors=1, **query)))
    pairs = (body or {}).get("target_collectors") if isinstance(body, dict) else None
    if not isinstance(pairs, list):
        raise RegisterError("대상 목록 응답에 target_collectors 가 없음(include_collectors=1 응답 꼴이 다름)")
    return [(pair.get("target") or {}, list(pair.get("collectors") or [])) for pair in pairs]


def _collector_config(col: dict) -> dict:
    config = col.get("config") or {}
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except json.JSONDecodeError:
            config = {}
    return config if isinstance(config, dict) else {}


def _nms_devices(session: Session, address: str) -> list[tuple[dict, list[dict]]]:
    return [(t, cols) for t, cols in _network_targets(session, search=address) if t.get("address") == address]


def phase_nms(session: Session, inv: dict) -> str | None:
    rt = router(inv)
    if not rt:
        print("라우터 없음(이 인벤토리는 NMS 장비를 등록하지 않는다)", file=session.out)
        return None
    found = _nms_devices(session, rt["address"])
    if found:
        target, cols = found[0]
        if len(found) > 1 or not any(c.get("kind") == "polestar_nms" for c in cols):
            # 대상만 있고 수집기가 없으면(앞 실행에서 수집기 생성과 보상 삭제가 함께 실패) 폴링이 없다. 성공으로 찍지 않는다.
            raise RegisterError(f"{rt['name']}: 주소 {rt['address']} 의 network 대상 {len(found)}개 가운데 polestar_nms 수집기가 있는 "
                                f"하나가 아님({[t.get('id') for t, _ in found]}). 화면에서 확인해 지우고 다시")
        print(f"{rt['name']}: 이미 등록됨 {target.get('id')}", file=session.out)
        return target.get("id")
    community_path = expand(inv["monitoring"]["snmp_community_file"])
    community = community_path.read_text(encoding="utf-8").strip()
    if not community:
        raise RegisterError(f"커뮤니티 파일이 비었음: {community_path}")
    target = {"type": "network", "name": rt["name"], "address": rt["address"], "discovery": "manual",
              "meta": {"collector": "polestar-nms", "group": "Polestar NMS", "endpoint_port": "161", "name_sync": False}}
    _, resp = session.write("POST", "/api/v1/targets", target)
    tid = _id(resp) or "<새 대상 id>"
    collector = {"kind": "polestar_nms", "enabled": True, "config": {"port": 161, "version": "2c"},
                 "config_encrypted": {"community": community}, "collection_policy_id": NMS_DEFAULT_POLICY}
    shown = dict(collector, config_encrypted={"community": f"<{inv['monitoring']['snmp_community_file']} 의 값>"})
    _write_collector_or_undo(session, tid, collector, shown)
    return tid


def _write_collector_or_undo(session: Session, tid: str, collector: dict, shown: dict | None = None) -> None:
    """화면과 같다: 수집기를 못 만들면 방금 만든 대상을 지운다. 지우기도 실패하면 두 사유를 함께 낸다."""
    try:
        session.write("POST", f"/api/v1/targets/{tid}/collectors", collector, shown=shown)
    except RegisterError as error:
        try:
            session.write("DELETE", f"/api/v1/targets/{tid}")
        except RegisterError as undo:
            raise RegisterError(f"{error}; 그리고 방금 만든 대상 {tid} 를 지우지 못함({undo}). 수집기 없는 대상이 남았다, 화면에서 지운다") from error
        raise


def _trap_receivers(session: Session) -> list[dict]:
    out = []
    for target, cols in _network_targets(session):
        for col in cols:
            config = _collector_config(col)
            if col.get("kind") == "polestar_trap" and str(config.get("port")) == str(TRAP_PORT) \
                    and str(config.get("protocol", TRAP_PROTOCOL)).lower() == TRAP_PROTOCOL:
                out.append(target)
                break
    return out


def phase_trap(session: Session) -> str | None:
    found = _trap_receivers(session)
    if found:
        print(f"trap 수신기 이미 있음 {found[0].get('id')}", file=session.out)
        return found[0].get("id")
    name = f"trap-{TRAP_PROTOCOL}-{TRAP_PORT}"
    target = {"type": "network", "name": name, "discovery": "manual",
              "meta": {"collector": "polestar-trap", "group": "Polestar TRAP", "endpoint_port": str(TRAP_PORT),
                       "endpoint_protocol": TRAP_PROTOCOL}}
    _, resp = session.write("POST", "/api/v1/targets", target)
    tid = _id(resp) or "<새 대상 id>"
    collector = {"kind": "polestar_trap", "enabled": True,
                 "config": {"port": TRAP_PORT, "protocol": TRAP_PROTOCOL, "v3_username": "", "v3_auth_protocol": "",
                            "v3_priv_protocol": "", "allow_unregistered": False}}
    _write_collector_or_undo(session, tid, collector)
    return tid


def phase_record(session: Session, inv: dict, specs_path: Path = LOG_MONITORS) -> dict:
    """대상 번호 표(runbook §12-4 의 꼴). 쓰기 없음."""
    table = {"schema": "petition-portal-targets/1", "testbed": inv["testbed"], "domain": inv["domain"],
             "servers": [], "network_devices": [], "trap_receivers": []}
    for host in servers(inv):
        sid = server_id(session, host)
        nodes = subtree(session, sid)
        children: dict[str, dict[str, str]] = {}
        for node in nodes:
            rtype = node.get("resource_type") or ""
            if node.get("id") == sid or not rtype:
                continue
            key = node.get("resource_key") or ""
            children.setdefault(rtype, {})[key] = node["id"]
        monitors = {(_monitor_file(m) or ""): _id(m) for m in _monitors(session, sid)}
        mgmt = children.get("sms.network_interface", {}).get(host["mgmt_if"])
        table["servers"].append({"name": host["name"], "role": host["role"], "target_id": sid,
                                 "agent_host": host["agent_host"], "children": children,
                                 "management_nic": {"ifname": host["mgmt_if"], "target_id": mgmt,
                                                    "decision": MGMT_NIC_DECISION},
                                 "log_monitors": {f: monitors.get(f) for f in sorted(monitors)}})
    rt = router(inv)
    if rt:
        for target, _ in _nms_devices(session, rt["address"]):
            nodes = subtree(session, target["id"])
            interfaces = {(n.get("resource_key") or n.get("display_name") or ""): n["id"] for n in nodes
                          if n.get("id") != target["id"] and (n.get("resource_kind") == "network_interface"
                                                              or "interface" in (n.get("resource_type") or ""))}
            table["network_devices"].append({"name": rt["name"], "target_id": target["id"], "address": rt["address"],
                                             "interfaces": interfaces})
    for target in _trap_receivers(session):
        table["trap_receivers"].append({"name": target.get("name"), "target_id": target.get("id"),
                                        "protocol": TRAP_PROTOCOL, "port": TRAP_PORT})
    return table


def phase_rollback(session: Session, table: dict) -> int:
    """대상 번호 표에 적힌 것을 거꾸로 지운다: trap 수신기, NMS 장비, 로그 감시, 서버 대상.

    서버 대상은 에이전트가 멈춘 뒤에만 지운다(멈추지 않으면 다음 보고에 등록 대기로 다시 생긴다, runbook §12-5).
    관리 NIC 는 관리 안 함으로 두지 않으므로(받아들임) 지울 관리 상태 선언이 없다. 옛 표의 "unmanaged" 목록도 지우지 않는다:
    그 선언은 제품 승격이 행과 함께 이미 지웠고(MNIC), 남았더라도 서버 대상 삭제가 하위 대상과 선언을 함께 지운다.
    """
    n = 0
    for rec in table.get("trap_receivers", []):
        session.write("DELETE", f"/api/v1/targets/{rec['target_id']}"); n += 1
    for rec in table.get("network_devices", []):
        session.write("DELETE", f"/api/v1/targets/{rec['target_id']}"); n += 1
    for rec in table.get("servers", []):
        for monitor_id in (rec.get("log_monitors") or {}).values():
            if monitor_id:
                session.write("DELETE", f"/sm/api/custom-monitors/{monitor_id}"); n += 1
    for rec in table.get("servers", []):
        session.write("DELETE", f"/api/v1/targets/{rec['target_id']}"); n += 1
    return n


# ---------------------------------------------------------------- 진입

def main(argv: list[str] | None = None, client_factory=Client) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--phase", required=True, choices=PHASES)
    parser.add_argument("--yes", action="store_true", help="쓰기 호출을 실제로 한다(없으면 찍기만)")
    parser.add_argument("--out", type=Path, help="record: 대상 번호 표를 쓸 파일")
    parser.add_argument("--record", type=Path, help="rollback: 지울 대상의 번호 표")
    parser.add_argument("--log-monitors", type=Path, default=LOG_MONITORS)
    args = parser.parse_args(argv)
    inv = load_inventory(args.inventory)
    base = os.environ.get("LUCIDA_QUERY_URL") or f"http://{inv['hypervisor']['monitor_real_addr']}:13080"
    user, password = os.environ.get("LUCIDA_LOGIN_USER"), os.environ.get("LUCIDA_LOGIN_PASSWORD")
    if not user or not password:
        print("t3_register: LUCIDA_LOGIN_USER, LUCIDA_LOGIN_PASSWORD 가 없음(러너 계정, 값은 출력하지 않는다)", file=sys.stderr)
        return 2
    if args.phase == "rollback" and not args.record:
        print("t3_register: rollback 은 --record <대상 번호 표> 필요", file=sys.stderr)
        return 2
    try:
        origin = web_origin(base, os.environ.get(ORIGIN_ENV))
    except RegisterError as error:
        print(f"t3_register: {error}", file=sys.stderr)
        return 2
    print(f"관제 주소 {base}, Origin {origin}")
    client = client_factory(base)
    try:
        client.login(user, password)
        session = Session(client, args.yes)
        if args.phase == "approve":
            print(f"승인 {len(phase_approve(session, inv))}대")
        elif args.phase == "nic":
            print(f"관리 NIC 받아들임 {len(phase_nic(session, inv))}개(쓰기 없음)")
        elif args.phase == "log-monitors":
            print(f"로그 감시 새로 {phase_log_monitors(session, inv, args.log_monitors)}개")
        elif args.phase == "nms":
            print(f"NMS 장비 {phase_nms(session, inv)}")
        elif args.phase == "trap":
            print(f"trap 수신기 {phase_trap(session)}")
        elif args.phase == "record":
            table = phase_record(session, inv, args.log_monitors)
            text = json.dumps(table, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            if args.out:
                args.out.write_text(text, encoding="utf-8")
                print(f"대상 번호 표: {args.out}")
            else:
                print(text, end="")
        elif args.phase == "rollback":
            table = json.loads(args.record.read_text(encoding="utf-8"))
            print(f"지울 호출 {phase_rollback(session, table)}개")
    except RegisterError as error:
        print(f"t3_register: {error}", file=sys.stderr)
        return 1
    if not args.yes and args.phase not in ("record", "nic"):
        print("--yes 없음: 쓰기 호출은 찍기만 했다")
    return 0


if __name__ == "__main__":
    sys.exit(main())
