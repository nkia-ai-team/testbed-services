#!/usr/bin/env python3
"""판정이 났는지가 아니라 **판정이 주입 때문인지**를 묻는 감사기.

2026-08-07, F19-P·F19-S가 통과했는데 그 통과가 가짜였다. 성공조건은
`order_create_5xx_rate >= 0.1`이었고 실제로 0.95였지만, 그 5xx는 주입한 mock `/pay`
지연이 만든 것이 아니라 dispatch-service 고장이 만든 것이었다 — `dispatches`에
status 인덱스가 없어 137만 행을 훑다가 죽었고, OrderService가 그 실패를 payment 호출
**이전에** 503으로 바꿨다. 사람이 손으로 확인한 방법은 이랬다:

    성공을 확정한 창에서 실패한 사용자 요청 트레이스를 모으고,
    그 트레이스 안에 **주입 대상 서비스의 스팬이 있는지** 센다.
    → order 503 트레이스 482건 중 payment 스팬 0건. 인과 미입증.

이 도구는 그 한 번의 포렌식을 41종에 대해 자동으로 돌린다. 러너의 판정을 뒤집지는
않는다 — 판정 옆에 **"그 판정을 만든 신호가 주입과 연결되는가"**를 나란히 적는다.

## 왜 실패율 계열만 보는가

`checkout_5xx_rate`·`order_create_5xx_rate` 같은 신호는 "요청이 **어떤 이유로든**
실패했다"를 센다. 원인을 묻지 않으므로 상류 고장을 그대로 흡수한다. 지연(p95) 계열은
같은 함정이 훨씬 약하다(느려지려면 대개 그 경로를 실제로 지나야 한다). 그래서 검사는
실패율 계열이 판정을 만들었을 때만 돌리고, 아니면 `not_applicable`로 둔다.

## 모르면 모른다고 한다

트레이스가 없거나 표본이 임계 미만이면 `undetermined`다. 오늘 반복해서 배운 것 —
없는 것을 지어내면 그 자체가 다음 사람을 속인다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
METADATA_PATH = SCRIPT_DIR / "registry" / "scenario-metadata.json"

CLICKHOUSE_URL = os.environ.get("CLICKHOUSE_URL", "http://192.168.230.119:18123/")
CLICKHOUSE_USER = os.environ.get("CLICKHOUSE_USER", "lucida")
CLICKHOUSE_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "lucida123")
TRACE_TABLE = "lucida.otel_traces_local"

# 실패율 계열 — "요청이 어떤 이유로든 실패했다"를 세는 신호. 원인을 안 묻기 때문에
# 상류 고장을 흡수한다. 이 계열이 판정을 만들었을 때만 인과 검사를 돈다.
FAILURE_RATE_MARKERS = ("5xx_rate", "_error_rate", "_fail", "error_rate")
# 2xx 성공률은 반대 방향이지만 같은 함정이다(상류가 죽어도 2xx가 떨어진다).
SUCCESS_RATE_MARKERS = ("_2xx_rate", "_success_rate")

# 도메인별 사용자 진입 서비스. 부하 생성기가 때리는 지점이고, 피해가 사용자에게
# 보이는 곳이다. profiles.json의 domain_profiles(business_step)와 같은 계약을 가리킨다.
DOMAIN_ENTRY_SERVICE = {
    "commerce": "commerce-order",
    "food-delivery": "food-delivery-order",
    "core-banking": "core-banking-transfer",
}

# 진입 서비스는 **신호**에서 고른다. 시나리오의 domain 으로 고르면 교차 도메인에서
# 틀린다 — F17-R 은 domain=core-banking(주입은 banking transfer)인데 판정 신호는
# commerce 의 `checkout_5xx_rate` 다. domain 으로 골랐더니 core-banking-transfer 의
# 5xx 를 찾다가 0건이 나와 "실패 트레이스 없음"으로 조용히 빠졌다(실제로는 같은 창에
# commerce-order 502 가 169건 있었다). 신호가 어느 사용자 여정을 재는지가 정답이다.
SIGNAL_ENTRY_SERVICE = {
    "checkout_5xx_rate": "commerce-order",
    "order_error_rate": "commerce-order",
    "order_create_5xx_rate": "food-delivery-order",
    "food_create_5xx_rate": "food-delivery-order",
    "transfer_2xx_rate": "core-banking-transfer",
}


def entry_service_for(signals: Iterable[str], domain: str | None) -> str | None:
    for signal in signals:
        if signal in SIGNAL_ENTRY_SERVICE:
            return SIGNAL_ENTRY_SERVICE[signal]
    return DOMAIN_ENTRY_SERVICE.get(domain or "")

MIN_TRACE_SAMPLE = 10          # 이보다 적으면 비율을 아예 말하지 않는다
CAUSAL_PRESENT_RATIO = 0.50    # 절반 이상이면 인과 있음
CAUSAL_ABSENT_RATIO = 0.05     # 5% 이하면 인과 없음(F19는 0.00이었다)
# "0건 관측"에서 진짜 비율의 상한은 rule of three 로 3/n 이다. 5% 를 주장하려면
# n >= 60 이 필요하다. 판정 창은 대개 1분 남짓이라 그만큼 안 모이는 일이 흔하므로,
# 말을 못 하게 막는 대신 **신뢰도를 함께 적는다** — 읽는 사람이 과대 해석하지 않도록.
CONFIDENT_SAMPLE = int(3 / CAUSAL_ABSENT_RATIO)

# --- 중단 시계열(ITS) 검사 -------------------------------------------------
# 위 비율 검사는 "실패한 요청이 대상을 지나갔는가"를 묻는다. 그 질문은 **대상이
# 응답을 멈추는 형태에서 구조적으로 틀린다** — 지나갈 스팬 자체가 없기 때문이다.
# 2026-08-13 F06-H 실측(payments 테이블 EXCLUSIVE 잠금):
#     주입 전 commerce-payment 103.7 spans/분, 오류 0
#     주입 중                   0.6 spans/분   ← 얼어붙음
#     주입 후                 132.7 spans/분, 오류 0   ← 회복
# 러너 판정은 succeeded, 비율 검사는 `causality_absent`(confidence high, 149건 중
# 0건). 인과는 완벽히 맞는데 도구가 틀렸다. 차단기 예외 조항도 `target_errors > 0`
# 을 요구해서 이 형태를 못 잡는다 — 얼어붙은 서비스는 오류도 0이다.
#
# 그래서 "지나갔는가" 대신 **t1 에서 꺾이고 t2 에서 돌아오는가**를 함께 본다.
# 되돌아오는 것까지 봐야 인과다 — 꺾이기만 하면 추세일 수 있다.
ITS_PRE_MIN = 5            # 주입 직전 관측 창(분)
ITS_POST_MIN = 5           # 주입 종료 후 관측 창(분)
ITS_POST_SETTLE_SEC = 30   # t2 직후는 정리 작업이 섞이므로 건너뛴다
ITS_MIN_BASELINE_RPM = 20  # 이보다 조용한 서비스는 붕괴를 말할 수 없다
ITS_COLLAPSE_RATIO = 0.20  # 주입 전 대비 20% 이하로 떨어지면 붕괴
ITS_RECOVERY_RATIO = 0.50  # 주입 후 전 수준의 50% 이상 돌아오면 회복


class AuditError(RuntimeError):
    pass


# --------------------------------------------------------------------------- run 읽기

def load_run(run_dir: Path) -> dict[str, Any]:
    def _json(name: str) -> Any:
        path = run_dir / name
        if not path.is_file():
            raise AuditError(f"{run_dir.name}: {name} 없음")
        return json.loads(path.read_text())

    def _result_or_state(state: dict[str, Any]) -> dict[str, Any]:
        """result.json 이 없으면 state.json 에서 같은 값을 만든다.

        큐(live-queue)로 돈 런은 result.json 을 남기지만 수동 실행
        (`POST /api/scenarios/{id}/run`)은 안 남긴다 — 2026-08-13 실측.
        수리 검증·파일럿은 대부분 수동 실행이라, 이걸 못 읽으면 정작 감사해야 할
        런을 감사하지 못한다. t1/t2 는 첫 레벨 적용 ~ 마지막 레벨 종료다.
        """
        path = run_dir / "result.json"
        if path.is_file():
            return json.loads(path.read_text())
        changes = state.get("level_changes") or []
        if not changes:
            raise AuditError(f"{run_dir.name}: result.json 도 level_changes 도 없다")
        return {
            "scenario_id": state.get("scenario_id"),
            "outcome": (state.get("controller_state") or {}).get("phase"),
            "t1": changes[0].get("applied_at"),
            "t2": changes[-1].get("effect_ended_at"),
            "_source": "state.json (result.json 부재)",
        }

    ticks_path = run_dir / "ticks.jsonl"
    if not ticks_path.is_file():
        raise AuditError(f"{run_dir.name}: ticks.jsonl 없음")
    ticks = [json.loads(line) for line in ticks_path.read_text().splitlines() if line.strip()]
    state = _json("state.json")
    return {
        "run_id": run_dir.name,
        "result": _result_or_state(state),
        "state": state,
        "decisions": _json("decisions.json"),
        "ticks": ticks,
    }


def deciding_gate(run: dict[str, Any]) -> str | None:
    """어떤 게이트가 이 run의 최종 판정을 만들었는가."""
    reason = run["decisions"].get("controller_state", {}).get("reason", "")
    outcome = run["result"].get("outcome")
    if outcome == "succeeded":
        return "success"
    if reason == "must_rule_out_detected":
        return "must_rule_out"
    if reason in ("abort_condition", "abort"):
        return "abort"
    return None


def gate_spec(run: dict[str, Any], gate: str) -> dict[str, Any] | None:
    """게이트 정의는 **그 run의 state.json**에서 읽는다.

    레지스트리는 그 뒤로 바뀌었을 수 있고(오늘만 임계가 여러 번 움직였다), 우리가
    묻는 것은 "그때 무엇이 판정을 만들었나"이지 "지금 계약이 무엇인가"가 아니다.
    """
    spec = (run["state"].get("spec") or {}).get("adaptive") or {}
    return spec.get(gate)


def signal_names(spec: dict[str, Any] | None) -> list[str]:
    if not spec:
        return []
    return [c["signal"] for c in spec.get("conditions", []) if "signal" in c]


def is_failure_rate(signal: str) -> bool:
    low = signal.lower()
    return any(m in low for m in FAILURE_RATE_MARKERS) or any(m in low for m in SUCCESS_RATE_MARKERS)


def verdict_window(run: dict[str, Any], gate: str, consecutive: int) -> tuple[str, str, int]:
    """판정을 확정한 연속 틱 구간의 [시작, 끝] 시각.

    streak이 마지막으로 0에서 올라가 confirm에 닿기까지가 그 구간이다. 그 앞뒤는
    판정과 무관하므로 트레이스를 넓게 보면 엉뚱한 구간을 인과로 오인한다.
    """
    ticks = run["ticks"]
    end_idx = None
    for i, tick in enumerate(ticks):
        if (tick.get("streaks") or {}).get(gate, 0) >= consecutive:
            end_idx = i
            break
    if end_idx is None:
        raise AuditError(f"{gate} streak이 {consecutive}에 닿은 틱이 없다")
    start_idx = end_idx
    while start_idx > 0 and (ticks[start_idx - 1].get("streaks") or {}).get(gate, 0) > 0:
        start_idx -= 1
    start_at = ticks[start_idx].get("at")
    end_at = ticks[end_idx].get("at")
    if not start_at or not end_at:
        raise AuditError("틱에 시각(at)이 없다")
    return start_at, end_at, end_idx - start_idx + 1


# --------------------------------------------------------------- 주입 대상 / 도메인

def load_metadata() -> dict[str, Any]:
    return json.loads(METADATA_PATH.read_text())["scenarios"]


def apm_services() -> set[str]:
    """APM 스팬이 실제로 존재하는 서비스 집합.

    러너의 APPROVED_APM_SERVICES를 베끼지 않고 **우리 레지스트리에서 유도**한다 —
    controllers.json이 apm 질의에 실어 보내는 service_name이 곧 관측 가능한 집합이고,
    베껴 두면 어느 한쪽이 바뀔 때 조용히 어긋난다(오늘 F23-R이 그렇게 물렸다).
    """
    controllers = json.loads((SCRIPT_DIR / "registry" / "controllers.json").read_text())
    found: set[str] = set()
    for controller in controllers.get("controllers", {}).values():
        for obs in controller.get("observations", []):
            # apm 질의뿐 아니라 clickhouse 오류율 질의도 같은 이름 공간을 쓴다. 앞서
            # apm 질의로만 좁혔더니 F19-P의 food-delivery-payment 가 빠져 대상 집합이
            # 통째로 비었다 — 관측 경로가 아니라 **이름 공간**을 모으는 게 맞다.
            name = (obs.get("parameters") or {}).get("service_name")
            if isinstance(name, str):
                found.add(name)
    return found


# 정답지는 짧은 이름(`food-order`)을 쓰고 APM은 긴 이름(`food-delivery-order`)을 쓴다.
# 둘을 잇되, 최종 판단은 반드시 실제 APM 집합 안에 있는지로 한다.
_NAME_PREFIXES = ("food-delivery-", "core-banking-", "commerce-", "food-", "banking-")
DOMAIN_APM_PREFIX = {"commerce": "commerce-", "food-delivery": "food-delivery-",
                     "core-banking": "core-banking-"}


def to_apm_service(name: str, domain: str | None, known: set[str]) -> str | None:
    """정답지 이름을 APM service_name으로 정규화한다. 모르면 None(=관측 불가)."""
    base = name.split(":", 1)[0]          # commerce-postgres:schema.table → commerce-postgres
    if base in known:
        return base
    prefix = DOMAIN_APM_PREFIX.get(domain or "")
    if not prefix:
        return None
    for p in _NAME_PREFIXES:
        if base.startswith(p):
            candidate = prefix + base[len(p):]
            return candidate if candidate in known else None
    return None


def injection_targets(scenario_id: str, metadata: dict[str, Any],
                      known: set[str], entry_service: str | None) -> tuple[set[str], str | None]:
    """정답지에서 주입 대상 서비스 집합과 도메인을 얻는다.

    `root_cause.target_id`가 결함의 위치, `trigger_target_id`가 피해가 드러나는 곳,
    `scoring.accept/partial`이 채점상 정답으로 인정되는 자원이다. 셋을 합치면
    "이 시나리오가 건드린다고 주장하는 것"이 된다 — 그 스팬이 실패 트레이스에
    하나도 없으면 주장과 실측이 어긋난 것이다.

    **진입 서비스는 반드시 뺀다.** 실패를 그 서비스에서 세고 있으므로, 그것을 대상에
    넣으면 모든 실패 트레이스가 자동으로 대상을 포함해 비율이 1.0이 되고 검사가
    공허해진다. 우리가 묻는 것은 "실패가 주입된 곳까지 닿았는가"이다.

    APM 스팬이 없는 대상(mock·DB·노드)은 조용히 빠진다 — 그 경우 대상이 비면
    `undetermined`가 되지, 없는 근거를 지어내지 않는다.
    """
    meta = metadata.get(scenario_id)
    if meta is None:
        raise AuditError(f"{scenario_id}: 정답지 없음")
    root = meta.get("root_cause") or {}
    scoring = meta.get("scoring") or {}
    candidates: list[str] = []
    for key in ("target_id", "trigger_target_id"):
        value = root.get(key)
        if isinstance(value, str):
            candidates.append(value)
    for key in ("accept", "partial"):
        candidates.extend(v for v in (scoring.get(key) or []) if isinstance(v, str))
    domain = meta.get("domain")
    targets = {s for s in (to_apm_service(c, domain, known) for c in candidates) if s}
    targets.discard(entry_service)
    return targets, domain


# --------------------------------------------------------------------- 트레이스 조회

def clickhouse_query(sql: str, *, opener: Callable[..., Any] | None = None) -> list[dict[str, Any]]:
    request = urllib.request.Request(
        CLICKHOUSE_URL,
        data=(sql + "\nFORMAT JSONEachRow").encode("utf-8"),
        headers={
            "X-ClickHouse-User": CLICKHOUSE_USER,
            "X-ClickHouse-Key": CLICKHOUSE_PASSWORD,
            "Content-Type": "text/plain; charset=utf-8",
        },
    )
    open_fn = opener or urllib.request.urlopen
    with open_fn(request, timeout=60) as response:  # noqa: S310 (고정 내부 호스트)
        body = response.read().decode("utf-8")
    return [json.loads(line) for line in body.splitlines() if line.strip()]


def _quote(values: Iterable[str]) -> str:
    # 값은 정답지에서만 오고 서비스 이름은 [a-z-]이지만, SQL에 문자열을 붙이는 이상
    # 따옴표는 반드시 이스케이프한다.
    return ", ".join("'" + v.replace("'", "''") + "'" for v in sorted(values))


def activity_shift(
    *,
    injection_from: str,
    injection_to: str,
    targets: set[str],
    entry_service: str | None,
    query: Callable[[str], list[dict[str, Any]]],
) -> dict[str, Any]:
    """주입 대상의 거동이 t1 에서 꺾이고 t2 에서 돌아오는가.

    비율 검사와 달리 트레이스의 **모양**이 아니라 **양**을 본다. 그래서 대상이
    얼어붙어 스팬이 사라지는 형태도 잡힌다(모듈 주석의 F06-H 참조).

    ⚠ **진입 서비스의 급증은 인과 근거가 아니다.** 다수 시나리오가 companion 으로
    부하 프로파일(load.north_south 등)을 함께 흘리므로, 진입 급증은 부하 생성기가
    만든 것일 수 있다. 근거로 쓰는 것은 **대상 쪽** 붕괴/오류 급증뿐이고, 진입은
    맥락으로만 싣는다.
    """
    services = sorted(set(targets) | ({entry_service} if entry_service else set()))
    if not services:
        return {"status": "undetermined", "reason": "관측할 서비스가 없다"}

    t1, t2 = _parse(injection_from), _parse(injection_to)
    windows = {
        "pre": (t1 - timedelta(minutes=ITS_PRE_MIN), t1 - timedelta(seconds=5)),
        "during": (t1, t2),
        "post": (t2 + timedelta(seconds=ITS_POST_SETTLE_SEC),
                 t2 + timedelta(seconds=ITS_POST_SETTLE_SEC) + timedelta(minutes=ITS_POST_MIN)),
    }

    measured: dict[str, dict[str, dict[str, float]]] = {}
    for name, (lo, hi) in windows.items():
        minutes = max((hi - lo).total_seconds() / 60.0, 1e-9)
        sql = f"""
SELECT service_name,
       count() AS spans,
       countIf(toUInt16OrZero(span_attributes['http.response.status_code']) >= 500) AS errors
FROM {TRACE_TABLE}
WHERE timestamp BETWEEN '{lo.strftime("%Y-%m-%d %H:%M:%S")}' AND '{hi.strftime("%Y-%m-%d %H:%M:%S")}'
  AND service_name IN ({_quote(set(services))})
  AND NOT startsWith(span_name, 'GET /actuator')
GROUP BY service_name
"""
        for row in query(sql):
            svc = row["service_name"]
            measured.setdefault(svc, {})[name] = {
                "rpm": round(int(row.get("spans") or 0) / minutes, 1),
                "err_rpm": round(int(row.get("errors") or 0) / minutes, 1),
            }

    def at(svc: str, win: str, field: str) -> float:
        return float(((measured.get(svc) or {}).get(win) or {}).get(field, 0.0))

    # 대상의 절대 spans/분 은 주변 부하에 따라 통째로 오르내린다 — 다수 시나리오가
    # companion 부하 프로파일을 함께 흘리기 때문이다. **진입 서비스로 정규화한 점유율**
    # 을 쓰면 그 흔들림이 상쇄된다. 2026-08-14 실측(전 → 중 → 후):
    #   F06-H  절대 76.5→3.9→1536.4   점유율 1.01→0.00→0.91
    #   F15-H  절대 205.8→18.3→84.4   점유율 0.99→0.47→0.96  ← 절대값으로는 회복 미달
    #   F15-T2 절대 84.2→28.7→32.0    점유율 1.05→1.06→1.06  ← 실제로는 무변화
    # F15-H 는 절대값으로 보면 "회복 안 됨"이지만 주변 부하가 같이 내려간 것뿐이었다.
    entry_rpm = {w: 0.0 for w in ("pre", "during", "post")}
    if entry_service:
        entry_rpm = {w: at(entry_service, w, "rpm") for w in entry_rpm}

    per_service: dict[str, dict[str, Any]] = {}
    for svc in services:
        pre, during, post = (at(svc, w, "rpm") for w in ("pre", "during", "post"))
        is_target = svc in targets
        normalize = is_target and all(entry_rpm[w] > 0 for w in entry_rpm)
        if normalize:
            share = {w: v / entry_rpm[w] for w, v in
                     (("pre", pre), ("during", during), ("post", post))}
            collapsed = (pre >= ITS_MIN_BASELINE_RPM
                         and share["during"] <= share["pre"] * ITS_COLLAPSE_RATIO)
            recovered = share["pre"] > 0 and share["post"] >= share["pre"] * ITS_RECOVERY_RATIO
        else:
            share = None
            collapsed = pre >= ITS_MIN_BASELINE_RPM and during <= pre * ITS_COLLAPSE_RATIO
            recovered = pre > 0 and post >= pre * ITS_RECOVERY_RATIO
        per_service[svc] = {
            "role": "target" if svc in targets else "entry",
            "pre_rpm": pre, "during_rpm": during, "post_rpm": post,
            "pre_err_rpm": at(svc, "pre", "err_rpm"),
            "during_err_rpm": at(svc, "during", "err_rpm"),
            "share_of_entry": ({k: round(v, 3) for k, v in share.items()} if share else None),
            "collapsed": collapsed,
            "recovered": recovered,
            "surged": pre > 0 and during >= pre * 2,
            "error_spike": at(svc, "during", "err_rpm") > max(at(svc, "pre", "err_rpm") * 3, 1.0),
        }

    tgt = {s: v for s, v in per_service.items() if v["role"] == "target"}

    # 주입 후 창에 부하가 없으면 "회복"을 판정할 수 없다. cleanup 이 부하 프로파일을
    # 내리므로, 런을 연달아 돌리면 이 창이 무부하 구간에 떨어진다 — 2026-08-14 실측:
    #   F03-H entry 837.4 → 1522.9 → 51.4   F19-P entry 95.4 → 158.0 → 4.6
    # 대상이 분명히 붕괴한 F15-H(205.8 → 18.3)까지 "회복 안 됨"으로 접혀 no_shift 가
    # 됐다. v3 캡처 사이클은 쿨다운 30분 동안 부하가 계속 돌아 이 문제가 없지만,
    # 수동 런에서는 매번 오판한다. 판정을 접지 말고 **판정 불가라고 말한다.**
    entry_v = next((v for v in per_service.values() if v["role"] == "entry"), None)
    post_has_load = entry_v is None or entry_v["post_rpm"] >= entry_v["pre_rpm"] * 0.2
    recovery_readable = post_has_load

    silent = [s for s, v in tgt.items() if v["collapsed"] and v["recovered"]]
    collapsed_only = [s for s, v in tgt.items() if v["collapsed"]]
    erring = [s for s, v in tgt.items() if v["error_spike"]]
    if silent:
        status, reason = "target_silenced", f"주입 구간에 {', '.join(silent)} 이(가) 멎었다가 회복했다"
    elif collapsed_only and not recovery_readable:
        status = "target_collapsed_recovery_unknown"
        reason = (f"주입 구간에 {', '.join(collapsed_only)} 이(가) 멎었다. 다만 주입 후 창에"
                  " 부하가 없어(진입 트래픽이 주입 전의 20% 미만) 회복은 판정할 수 없다")
    elif erring:
        status, reason = "target_erring", f"주입 구간에 {', '.join(erring)} 의 오류가 뛰었다"
    elif not tgt:
        status, reason = "undetermined", "APM 스팬을 내는 주입 대상이 없다"
    elif all(v["pre_rpm"] < ITS_MIN_BASELINE_RPM for v in tgt.values()):
        status, reason = "undetermined", "주입 전 대상 활동이 판정 최소치 미만이다"
    elif not recovery_readable:
        status = "undetermined"
        reason = "주입 후 창에 부하가 없어 거동 변화를 판정할 수 없다"
    else:
        status, reason = "no_shift", "대상 거동이 주입 구간에 유의미하게 변하지 않았다"

    return {"status": status, "reason": reason, "services": per_service,
            "post_window_has_load": post_has_load,
            "windows": {k: [v[0].isoformat(), v[1].isoformat()] for k, v in windows.items()}}


def causal_check(
    *,
    start_at: str,
    end_at: str,
    entry_service: str,
    targets: set[str],
    query: Callable[[str], list[dict[str, Any]]],
    injection_from: str | None = None,
    injection_to: str | None = None,
) -> dict[str, Any]:
    """실패한 사용자 요청 트레이스 중 주입 대상 스팬을 포함한 비율."""
    if not targets:
        return {"status": "undetermined", "reason": "정답지에 APM 서비스 대상이 없다"}
    # 트레이스는 창 경계에 걸칠 수 있으므로 조인 쪽만 앞뒤로 조금 넓힌다.
    lo = (_parse(start_at) - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
    hi = (_parse(end_at) + timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
    win_lo = _parse(start_at).strftime("%Y-%m-%d %H:%M:%S")
    win_hi = _parse(end_at).strftime("%Y-%m-%d %H:%M:%S")
    sql = f"""
WITH failed AS (
  SELECT DISTINCT trace_id FROM {TRACE_TABLE}
  WHERE span_kind = 'SERVER'
    AND service_name = '{entry_service}'
    AND toUInt16OrZero(span_attributes['http.response.status_code']) >= 500
    AND timestamp BETWEEN '{win_lo}' AND '{win_hi}'
)
SELECT
  (SELECT count() FROM failed) AS failed_traces,
  uniqExactIf(trace_id, service_name IN ({_quote(targets)})) AS traces_touching_target
FROM {TRACE_TABLE}
WHERE trace_id IN (SELECT trace_id FROM failed)
  AND timestamp BETWEEN '{lo}' AND '{hi}'
"""
    rows = query(sql)
    if not rows:
        return {"status": "undetermined", "reason": "트레이스 질의가 행을 반환하지 않았다"}
    total = int(rows[0].get("failed_traces") or 0)
    touching = int(rows[0].get("traces_touching_target") or 0)
    if total == 0:
        return {"status": "undetermined", "reason": "창 안에 실패한 사용자 요청 트레이스가 없다",
                "failed_traces": 0}
    if total < MIN_TRACE_SAMPLE:
        return {"status": "undetermined", "reason": f"표본 부족({total} < {MIN_TRACE_SAMPLE})",
                "failed_traces": total, "traces_touching_target": touching}
    ratio = touching / total
    extra: dict[str, Any] = {}
    if ratio <= CAUSAL_ABSENT_RATIO:
        # 스팬이 없다고 인과가 없는 것은 아니다. **차단기가 열리면 하류 호출 자체가
        # 사라진다** — F01-H 실측(2026-08-07): mock /v1/payments 429 → payment 실패 →
        # @CircuitBreaker(paymentClient) open → 이후 checkout 은 requestPaymentFallback
        # 의 502 를 받고 payment 를 **부르지 않는다**. 그래서 order 502 가 734건인데
        # payment 스팬은 30초당 1~5건(half-open 탐침)만 남았다. 이걸 그대로 "인과 없음"
        # 이라고 적으면 차단기를 가진 시나리오 전부가 거짓 음성이 된다.
        #
        # 가르는 기준은 **대상이 그 창에서 실제로 실패하고 있었는가**다. F19-P 는
        # 대상(food payment)의 스팬이 0건이고 오류도 0건이었다 — 그건 진짜 인과 없음이다.
        # 두 질문은 창이 다르다. "무엇이 판정을 만들었나"는 판정 창(위 비율)이고,
        # "대상이 애초에 연루돼 있었나"는 **주입 구간 전체**의 질문이다. 판정 창은
        # 대개 30초~1분이라(신호가 이미 포화돼 일찍 confirm 된다) 차단기의 half-open
        # 탐침(30초당 1~5건)이 잡히지 않는다 — F01-H 가 정확히 그랬다.
        act_lo = _parse(injection_from).strftime("%Y-%m-%d %H:%M:%S") if injection_from else win_lo
        act_hi = _parse(injection_to).strftime("%Y-%m-%d %H:%M:%S") if injection_to else win_hi
        target_health = _target_activity(targets, act_lo, act_hi, entry_service, query)
        # 대상 오류가 진입 실패를 설명할 만한 규모여야 한다. 스팬 두어 건이 실패한
        # 것만으로 차단기를 주장하면, 실제 원인이 딴 데 있는 run 이 전부 "인과 가능"으로
        # 흐려진다 — F19-S 는 payment 업무 오류가 2건인데 order 실패는 258건이었고,
        # 진짜 원인은 dispatch 였다(독립 증거: dispatch 500·헬스 503·재시작 10회).
        tgt_err = target_health.get("errors", 0)
        entry_fail = target_health.get("entry_failures", 0)
        proportionate = tgt_err > 0 and (entry_fail == 0 or tgt_err / entry_fail >= CAUSAL_ABSENT_RATIO)
        if proportionate:
            return {
                "status": "causality_via_breaker",
                "failed_traces": total, "traces_touching_target": touching,
                "ratio": round(ratio, 4),
                "confidence": "low",
                "confidence_note": (
                    "대상 스팬은 거의 없지만 대상 자체가 실패 중이다 — 차단기가 열려 하류 "
                    "호출이 사라진 형태와 일치한다. 스팬 부재만으로 인과를 부정할 수 없다"),
                "target_spans": target_health.get("spans"),
                "target_errors": target_health.get("errors"),
                "target_window": f"{act_lo} ~ {act_hi} (주입 구간 전체)",
                "entry_failures": entry_fail,
                "targets": sorted(targets), "entry_service": entry_service,
            }
        status = "causality_absent"
        extra = {"target_spans": target_health.get("spans"),
                 "target_errors": tgt_err, "entry_failures": entry_fail}
    elif ratio >= CAUSAL_PRESENT_RATIO:
        status = "causality_present"
    else:
        status = "causality_partial"
    return {
        **extra,
        "status": status,
        "failed_traces": total,
        "traces_touching_target": touching,
        "ratio": round(ratio, 4),
        "confidence": "high" if total >= CONFIDENT_SAMPLE else "low",
        "confidence_note": (
            None if total >= CONFIDENT_SAMPLE
            else f"표본 {total} < {CONFIDENT_SAMPLE}(rule of three) — 방향은 읽되 단정하지 말 것"
        ),
        "targets": sorted(targets),
        "entry_service": entry_service,
    }


def _target_activity(targets: set[str], lo: str, hi: str, entry_service: str,
                     query: Callable[[str], list[dict[str, Any]]]) -> dict[str, Any]:
    """대상 서비스가 그 창에서 **업무** 스팬을 남겼는지, 그중 실패가 있었는지.

    `/actuator/**` 는 뺀다 — 헬스체크는 주입과 무관하게 항상 돌므로, 세면 "대상이
    관여했다"가 언제나 참이 된다. F19-S 가 그 함정에 걸려 payment 업무 호출이 0건인데
    헬스 스팬 16건 때문에 차단기로 오분류됐다.

    "실패 트레이스에 대상 스팬이 없다"를 인과 부정으로 읽으려면, 대상이 아예
    조용했는지(진짜 무관) 아니면 실패하고 있었는지(차단기로 호출이 끊긴 것)를
    구분해야 한다.
    """
    sql = f"""
SELECT
  countIf(service_name IN ({_quote(targets)})) AS spans,
  countIf(service_name IN ({_quote(targets)})
          AND toUInt16OrZero(span_attributes['http.response.status_code']) >= 500) AS errors,
  countIf(service_name = '{entry_service}'
          AND toUInt16OrZero(span_attributes['http.response.status_code']) >= 500) AS entry_failures
FROM {TRACE_TABLE}
WHERE span_kind = 'SERVER'
  AND service_name IN ({_quote(targets | {entry_service})})
  AND NOT startsWith(span_attributes['http.route'], '/actuator')
  AND timestamp BETWEEN '{lo}' AND '{hi}'
"""
    rows = query(sql)
    if not rows:
        return {"spans": 0, "errors": 0, "entry_failures": 0}
    return {"spans": int(rows[0].get("spans") or 0),
            "errors": int(rows[0].get("errors") or 0),
            "entry_failures": int(rows[0].get("entry_failures") or 0)}


def _parse(value: str) -> datetime:
    """틱의 `at` 은 초 단위지만 `level_changes` 의 시각은 마이크로초를 달고 온다
    (2026-08-13: `...T23:28:32.340568Z`). 둘 다 받는다 — 한쪽만 받으면 result.json
    이 없는 수동 실행에서 t1/t2 를 못 읽어 인과 검사가 통째로 건너뛰어진다."""
    text = value.replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise AuditError(f"시각을 해석할 수 없다: {value}")


# ------------------------------------------------------------------------ 대조 팔

def control_arm(run: dict[str, Any], window_end_index: int | None) -> dict[str, Any]:
    """무주입 대조 팔 신호를 그대로 낸다 — 해석 가능 여부를 함께 표시한다.

    2026-08-07 배치 시점의 대조군 생산자는 분모를 싣지 않아, 0.0이 "오류가 없었다"인지
    "요청 자체가 없었다"인지 구분되지 않는다(수리는 레포에만 있고 배포 전이다).
    분모 신호(`*_count`)가 틱에 실리기 시작하면 이 함수는 자동으로 해석 가능해진다 —
    조건이 데이터에 있지 코드에 박혀 있지 않기 때문이다.
    """
    ticks = run["ticks"]
    if not ticks:
        return {"status": "undetermined", "reason": "틱 없음"}
    idx = window_end_index if window_end_index is not None else len(ticks) - 1
    idx = min(idx, len(ticks) - 1)
    signals = ticks[idx].get("signals") or {}
    baseline = {k: (v or {}).get("value") for k, v in signals.items() if k.endswith("_baseline")}
    if not baseline:
        return {"status": "absent", "reason": "이 run에는 대조 팔 신호가 없다"}
    # 분모는 아무 `*_count`나 되는 게 아니라 **그 대조 신호의 분모**여야 한다.
    # 접미사만 보던 초기 판정은 F05-R에서 `restart_count`(파드 재시작 횟수)를 분모로
    # 오인해 해석 불가를 해석 가능으로 뒤집었다. 이름 줄기가 대조 신호의 접두여야 한다:
    #   checkout_5xx_rate_baseline ← checkout_count   (checkout_5xx_rate 가 checkout 로 시작)
    #   checkout_5xx_rate_baseline ← restart_count    (restart 로 시작하지 않음 → 거부)
    stems = {k[: -len("_baseline")] for k in baseline}
    denominators = {
        k: (v or {}).get("value")
        for k, v in signals.items()
        if k.endswith("_count") and any(stem.startswith(k[: -len("_count")]) for stem in stems)
    }
    if denominators:
        return {"status": "interpretable", "values": baseline, "denominators": denominators}
    return {
        "status": "uninterpretable",
        "values": baseline,
        "reason": "분모(*_count)가 없어 0.0이 '오류 없음'인지 '요청 없음'인지 구분 불가",
    }


# --------------------------------------------------------------------------- 감사

def audit_run(run_dir: Path, metadata: dict[str, Any],
              query: Callable[[str], list[dict[str, Any]]] | None,
              known_services: set[str]) -> dict[str, Any]:
    run = load_run(run_dir)
    scenario_id = run["result"].get("scenario_id") or run["state"].get("scenario_id")
    report: dict[str, Any] = {
        "run_id": run["run_id"],
        "scenario_id": scenario_id,
        "outcome": run["result"].get("outcome"),
        "reason": run["decisions"].get("controller_state", {}).get("reason"),
    }
    gate = deciding_gate(run)
    report["deciding_gate"] = gate
    if gate is None:
        report["causal_evidence"] = {"status": "not_applicable",
                                     "reason": "게이트가 만든 판정이 아니다(타임아웃 등)"}
        report["control_arm"] = control_arm(run, None)
        return report

    spec = gate_spec(run, gate)
    signals = signal_names(spec)
    report["deciding_signals"] = signals
    failure_signals = [s for s in signals if is_failure_rate(s)]
    report["failure_rate_signals"] = failure_signals

    try:
        start_at, end_at, span = verdict_window(run, gate, int((spec or {}).get("consecutive_ticks", 1)))
        report["verdict_window"] = {"from": start_at, "to": end_at, "ticks": span}
        end_index = next(i for i, t in enumerate(run["ticks"]) if t.get("at") == end_at)
    except (AuditError, StopIteration) as exc:
        report["verdict_window"] = {"error": str(exc)}
        end_index = None

    report["control_arm"] = control_arm(run, end_index)

    # ITS 검사는 게이트 종류와 무관하게 돈다. 비율 검사가 `not_applicable` 인
    # 시나리오(2026-08-13 기준 35종 중 15종)도 "주입이 효과를 냈는가"는 물어야 한다 —
    # 그 15종이 통째로 미검증으로 남아 있었다.
    domain = (metadata.get(scenario_id) or {}).get("domain")
    entry = entry_service_for(signals, domain)
    try:
        targets, _ = injection_targets(scenario_id, metadata, known_services, entry)
    except AuditError as exc:
        targets = set()
        report["activity_shift"] = {"status": "undetermined", "reason": str(exc)}
    if "activity_shift" not in report:
        t1, t2 = run["result"].get("t1"), run["result"].get("t2")
        if query is None:
            report["activity_shift"] = {"status": "undetermined",
                                        "reason": "트레이스 백엔드에 접근할 수 없다"}
        elif not (t1 and t2):
            report["activity_shift"] = {"status": "undetermined", "reason": "주입 구간 미상"}
        else:
            try:
                report["activity_shift"] = activity_shift(
                    injection_from=t1, injection_to=t2, targets=targets,
                    entry_service=entry, query=query)
            except Exception as exc:  # 백엔드 장애를 판정으로 둔갑시키지 않는다
                report["activity_shift"] = {"status": "undetermined",
                                            "reason": f"트레이스 질의 실패: {exc}"}

    if not failure_signals:
        report["causal_evidence"] = {
            "status": "not_applicable",
            "reason": "판정을 만든 신호가 실패율 계열이 아니다(지연·자원 계열은 이 함정이 약하다)"
                      " — 인과는 activity_shift 로 본다",
        }
        return report
    if "verdict_window" in report and "error" in report["verdict_window"]:
        report["causal_evidence"] = {"status": "undetermined", "reason": "판정 구간을 특정할 수 없다"}
        return report
    if query is None:
        report["causal_evidence"] = {"status": "undetermined", "reason": "트레이스 백엔드에 접근할 수 없다"}
        return report

    # 진입 서비스는 신호에서 고른다 — 실패율 신호가 있으면 그쪽이 더 정확하다.
    entry_from_failure = entry_service_for(failure_signals, domain) or entry
    if entry_from_failure is None:
        report["causal_evidence"] = {"status": "undetermined",
                                     "reason": f"진입 서비스 미상(신호={failure_signals}, domain={domain})"}
        return report
    ratio_targets, _ = injection_targets(scenario_id, metadata, known_services, entry_from_failure)
    try:
        report["causal_evidence"] = causal_check(
            start_at=report["verdict_window"]["from"], end_at=report["verdict_window"]["to"],
            entry_service=entry_from_failure, targets=ratio_targets, query=query,
            injection_from=run["result"].get("t1"), injection_to=run["result"].get("t2"),
        )
    except Exception as exc:  # 트레이스 백엔드 장애를 판정으로 둔갑시키지 않는다
        report["causal_evidence"] = {"status": "undetermined", "reason": f"트레이스 질의 실패: {exc}"}

    # 비율 검사가 "미입증"이라도 대상이 멎었다가 회복했다면 그것이 인과다.
    # 비율 검사의 구조적 사각지대이므로 ITS 쪽 결론이 이긴다(모듈 주석 F06-H).
    shift = report.get("activity_shift") or {}
    if (report["causal_evidence"].get("status") in {"causality_absent", "undetermined"}
            and shift.get("status") == "target_silenced"):
        report["causal_evidence"] = {
            **report["causal_evidence"],
            "status": "causality_via_silence",
            "superseded_status": report["causal_evidence"].get("status"),
            "reason": shift.get("reason"),
        }
    return report


VERDICT_LABEL = {
    "causality_present": "인과 입증",
    "causality_via_breaker": "인과 가능 — 차단기로 하류 호출이 사라진 형태",
    "causality_via_silence": "인과 입증 — 대상이 주입 구간에 멎었다가 회복(중단시계열)",
    "causality_absent": "인과 미입증 — 판정은 났으나 주입과 연결되지 않는다",
    "causality_partial": "부분 인과",
    "undetermined": "판정 불가",
    "not_applicable": "해당 없음",
}


def render(reports: list[dict[str, Any]]) -> str:
    lines = []
    for r in reports:
        ce = r.get("causal_evidence") or {}
        label = VERDICT_LABEL.get(ce.get("status", ""), ce.get("status", "?"))
        lines.append(f"{r['scenario_id']:8} {r['run_id']:26} outcome={r['outcome']:<10} → {label}")
        if ce.get("failed_traces") is not None:
            conf = f" 신뢰도={ce['confidence']}" if ce.get("confidence") else ""
            lines.append(f"{'':9}실패 트레이스 {ce.get('failed_traces')}건 중 주입 대상 스팬 포함 "
                         f"{ce.get('traces_touching_target')}건 (ratio={ce.get('ratio')}){conf}")
            if ce.get("confidence_note"):
                lines.append(f"{'':9}⚠ {ce['confidence_note']}")
            if ce.get("targets"):
                lines.append(f"{'':9}대상={','.join(ce['targets'])} 진입={ce.get('entry_service')}")
        if ce.get("reason"):
            lines.append(f"{'':9}사유: {ce['reason']}")
        ca = r.get("control_arm") or {}
        if ca.get("status") == "uninterpretable":
            lines.append(f"{'':9}대조 팔 {ca.get('values')} — ⚠ {ca.get('reason')}")
        elif ca.get("status") == "interpretable":
            lines.append(f"{'':9}대조 팔 {ca.get('values')} 분모 {ca.get('denominators')}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="run 판정의 인과 근거를 감사한다(읽기 전용)")
    parser.add_argument("--runs-root", default="/root/rca-scenario-runner/runs")
    parser.add_argument("--run", action="append", default=[], help="run 디렉터리 이름(반복 가능)")
    parser.add_argument("--scenario", action="append", default=[], help="시나리오 id로 최신 run 선택")
    parser.add_argument("--json", action="store_true", help="JSON으로 출력")
    parser.add_argument("--no-traces", action="store_true", help="트레이스 조회 없이 구조만 감사")
    args = parser.parse_args(argv)

    root = Path(args.runs_root)
    dirs: list[Path] = [root / name for name in args.run]
    for scenario in args.scenario:
        matches = sorted(root.glob(f"{scenario}-run-*"), key=lambda p: p.stat().st_mtime, reverse=True)
        if matches:
            dirs.append(matches[0])
    if not dirs:
        dirs = sorted((p for p in root.iterdir() if p.is_dir() and "-run-" in p.name),
                      key=lambda p: p.stat().st_mtime, reverse=True)

    metadata = load_metadata()
    known = apm_services()
    query = None if args.no_traces else (lambda sql: clickhouse_query(sql))
    reports = []
    for d in dirs:
        try:
            reports.append(audit_run(d, metadata, query, known))
        except AuditError as exc:
            reports.append({"run_id": d.name, "scenario_id": "?", "outcome": "?",
                            "causal_evidence": {"status": "undetermined", "reason": str(exc)}})
    print(json.dumps(reports, indent=2, ensure_ascii=False) if args.json else render(reports))
    return 0


if __name__ == "__main__":
    sys.exit(main())
