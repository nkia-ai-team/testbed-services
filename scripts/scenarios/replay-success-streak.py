#!/usr/bin/env python3
"""러너의 success 판정을 런 아티팩트로 재생한다 — 배치를 돌리지 않고 후보를 검증한다.

임계·min_hold·consecutive_ticks 를 바꾸면 그 런이 통과했을지 답한다. 읽기 전용이고
`runs/<run-id>/ticks.jsonl` 만 본다. 2026-08-12 사이클의 수리 7종이 전부 이 도구로
결정됐다(F15-H·F20-R·F15-T2 의 min_hold, F03-H 의 게이트 교체, F09-P 임계 되돌림).

    # 먼저 검증 — 현행 계약으로 재생해 기록된 스트릭과 일치하는지 본다
    python3 scripts/scenarios/replay-success-streak.py --validate \
        --run F15-H-run-f2829049 --scenario F15-H

    # 그 다음 후보 평가
    python3 scripts/scenarios/replay-success-streak.py \
        --run F15-H-run-f2829049 --scenario F15-H --min-hold 240 --ct 2

`--runs-root` 기본값은 109 의 /root/rca-scenario-runner/runs 다.

──────────────────────────────────────────────────────────────────────────────
검증 없이 쓰지 말 것. 0812 에 첫 두 판본이 실측과 어긋났고, `--validate` 를 붙이지
않았으면 "현행 설정에서도 발화한다"는 거짓 결론을 그대로 냈을 것이다. 재생이
어려운 이유는 규칙 넷이 서로 얽혀 있기 때문이다 — 하나라도 빠뜨리면 조용히 틀린다:

1. **독립창 기반 증가**(adaptive.py `_updated_streaks`). 조건이 참이어도
   `elapsed - mark >= independence` 일 때만 오르고, 아니면 **유지**다. independence 는
   `match=all` 이면 조건 신호들의 `update_interval_sec` 최대값이다. 60s 메트릭이면
   15s 틱에도 스트릭은 분당 한 번만 오른다. (첫 판본이 매 틱 증가시켜 6까지 갔다.)
2. **hold-on-unknown (D0)**. unusable 은 success 스트릭을 리셋하지 않고 유지하며,
   상한은 `2 x independence` 만큼의 staleness 다. abort·must_rule_out 은 계속 리셋한다.
3. **settling 틱도 누적된다.** `_drop_pending_streaks` 는 must_rule_out·escalate 만
   0으로 만들고 success 는 건드리지 않는다. evaluating 틱만 재생하면 어긋난다.
   (두 번째 판본이 F20-R 에서 6틱 어긋난 원인.)
4. **사다리는 단마다 elapsed 가 되감긴다.** 되감기는 지점에서 끊고 스트릭을 0으로
   되돌린다. 안 하면 여러 단이 한 시퀀스로 붙는다.

`--validate` 는 **현행 레지스트리 계약**으로 재생해 기록과 대조한다. 그러므로 그
시나리오의 success 조건을 이미 바꾼 뒤에는 불일치가 나는 것이 **정상**이다 — 옛 런의
기록은 옛 계약이 만든 것이기 때문이다(예: F03-H 는 0812 에 `order_p95` 를 뺐으므로
그 이전 런에 대해 29/113 불일치가 난다). 검증은 계약을 바꾸기 **전에** 하거나,
바꾼 뒤라면 손대지 않은 시나리오로 하라.

이 도구가 답하지 **않는** 것: 새 설정에서 주입이 같은 피해를 낼지. 재생은 기록된
신호 위에서만 성립하므로 주입 파라미터를 바꾸는 수리(사다리 재구성 등)에는 쓸 수
없다 — 그건 배치가 답한다.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

DEFAULT_RUNS_ROOT = Path("/root/rca-scenario-runner/runs")
REGISTRY = Path(__file__).resolve().parent / "registry" / "controllers.json"
QUERIES = Path(__file__).resolve().parent / "registry" / "queries.json"

OPS = {
    "gte": lambda v, t: v >= t,
    "gt": lambda v, t: v > t,
    "lt": lambda v, t: v < t,
    "lte": lambda v, t: v <= t,
    "eq": lambda v, t: v == t,
    "ne": lambda v, t: v != t,
}
HOLD_FACTOR = 2  # adaptive.py `_UNKNOWN_HOLD_FACTOR`


def _seconds(value: str) -> int:
    if value.endswith("m"):
        return int(value[:-1]) * 60
    if value.endswith("s"):
        return int(value[:-1])
    return int(value)


def _condition(cond: dict, signals: dict):
    observed = signals.get(cond["observation"])
    if observed is None or not observed.get("usable"):
        return None
    return OPS[cond["op"]](observed.get("value"), cond["value"])


def _independence(conds: list[dict], signals: dict, match: str) -> int:
    intervals = [
        signals[c["observation"]].get("update_interval_sec") or 0
        for c in conds
        if c["observation"] in signals
        and (match == "all" or _condition(c, signals) is True)
    ]
    if not intervals:
        return 0
    return max(intervals) if match == "all" else min(intervals)


def replay(ticks: list[dict], conds: list[dict], match: str, min_hold: int, ct: int) -> dict:
    # 사다리는 단마다 elapsed 가 되감긴다. settling 도 함께 재생하되 판정은
    # evaluating 틱에서만 한다(규칙 3·4).
    usable_ticks = [t for t in ticks if t.get("phase") in ("settling", "evaluating")]
    segments: list[list[dict]] = []
    current: list[dict] = []
    previous = None
    for tick in usable_ticks:
        elapsed = tick.get("elapsed_sec")
        if previous is not None and elapsed < previous:
            segments.append(current)
            current = []
        current.append(tick)
        previous = elapsed
    segments.append(current)

    trace, best_any, best_post, fired = [], 0, 0, None
    for segment in segments:
        streak, mark = 0, None
        for tick in segment:
            signals = tick.get("signals") or {}
            elapsed = tick.get("elapsed_sec")
            results = [_condition(c, signals) for c in conds]
            independence = _independence(conds, signals, match)
            if match == "all":
                verdict = (
                    False if any(r is False for r in results)
                    else None if any(r is None for r in results)
                    else True
                )
            else:
                verdict = (
                    True if any(r is True for r in results)
                    else None if any(r is None for r in results)
                    else False
                )
            if verdict is None:  # D0: hold, bounded by 2x independence (규칙 2)
                if mark is not None and streak and elapsed - mark > HOLD_FACTOR * independence:
                    streak, mark = 0, None
            elif verdict is not True:
                streak, mark = 0, None
            elif streak == 0 or mark is None:
                streak, mark = 1, elapsed
            elif elapsed - mark >= independence:  # 규칙 1
                streak, mark = streak + 1, elapsed
            trace.append((elapsed, streak, (tick.get("streaks") or {}).get("success")))
            best_any = max(best_any, streak)
            if elapsed >= min_hold and tick.get("phase") == "evaluating":
                best_post = max(best_post, streak)
                if streak >= ct and fired is None:
                    fired = elapsed
    return {"trace": trace, "max_any": best_any, "max_post": best_post, "fires_at": fired}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", required=True, help="run 디렉터리 이름 (예: F15-H-run-f2829049)")
    parser.add_argument("--scenario", required=True, help="시나리오 id (예: F15-H)")
    parser.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    parser.add_argument("--min-hold", type=int, help="초. 생략하면 레지스트리의 첫 레벨 값")
    parser.add_argument("--ct", type=int, help="생략하면 레지스트리 값")
    parser.add_argument("--validate", action="store_true",
                        help="현행 계약으로 재생해 기록된 스트릭과 대조한다")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    controller = json.loads(REGISTRY.read_text())["controllers"][args.scenario]
    success = controller["success"]
    match = "all" if "all" in success else "any"
    conds = success.get("all") or success.get("any")
    ct = args.ct or int(success.get("consecutive_ticks", 1))
    min_hold = (args.min_hold if args.min_hold is not None
                else _seconds(controller["profile"]["levels"][0]["min_hold"]))

    path = args.runs_root / args.run / "ticks.jsonl"
    ticks = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    result = replay(ticks, conds, match, min_hold, ct)

    if args.validate:
        mismatched = [(el, sim, rec) for el, sim, rec in result["trace"]
                      if rec is not None and sim != rec]
        result["validation"] = {
            "ticks": len(result["trace"]),
            "mismatched": len(mismatched),
            "ok": not mismatched,
            "first_mismatches": mismatched[:8],
        }

    if args.json:
        print(json.dumps({k: v for k, v in result.items() if k != "trace"},
                         ensure_ascii=False, indent=2))
        return 0

    if args.validate:
        validation = result["validation"]
        status = "OK" if validation["ok"] else "MISMATCH"
        print("VALIDATE %s — %d/%d 틱 불일치" % (status, validation["mismatched"], validation["ticks"]))
        for el, sim, rec in validation["first_mismatches"]:
            print("    el=%s 재생=%s 기록=%s" % (el, sim, rec))
        if not validation["ok"]:
            print("불일치의 원인은 둘 중 하나다:")
            print("  (a) 이 시나리오의 success 계약을 런 이후에 바꿨다 -> 정상이다")
            print("  (b) 재생이 러너 규칙과 다르다 -> 모듈 상단 규칙 넷을 확인하라")
            print("먼저 `git log -p registry/controllers.json` 으로 (a) 를 배제할 것.")
    print("%s / %s  min_hold=%ss ct=%s -> max_any=%s max_post_min_hold=%s 발화=%s"
          % (args.scenario, args.run, min_hold, ct,
             result["max_any"], result["max_post"],
             ("el=%s" % result["fires_at"]) if result["fires_at"] is not None else "없음"))
    return 0 if not args.validate or result["validation"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
