#!/usr/bin/env python3
"""규모 축 판정: 피크 2배(60 rps) 30분(설계 docs/spec-testbed-vm-3tier.md §10 T2 "규모 축", §3-6).

입력:
  --summary   k6 --summary-export 결과(JSON). 부하 스크립트가 http_5xx, http_no_response 카운터를 낸다.
  --cpu       cpu-sample.sh 의 JSON 줄 파일들(이름=경로). 이름은 router, was 로 시작하는 것, hypervisor,
              steal 을 볼 VM 들(어느 이름이든 steal_pct 를 본다).
  --inventory 이 테스트베드 인벤토리(선택). 라우터가 없는 인벤토리(features.router 거짓, S 문서 결정 3)면 라우터
              CPU 표본을 요구하지 않는다. 주지 않으면 라우터 표본을 요구한다(T 의 종전 동작).

기준(설계 값): 비정상 비율 1% 이하, 라우터 CPU 50% 이하와 softirq 20% 이하, WAS CPU 70% 이하,
66 CPU 70% 이하, 이 도메인 VM steal 5% 이하. CPU 값은 10초 표본의 95 백분위로 판정한다(평균은 짧은 포화를
숨기고 최댓값은 한 표본의 잡음에 흔들린다). 2배 부하가 실제로 들어갔는지 보려고 달성 요청률이 목표의 95%
이상인지도 본다(못 미치면 판정할 수 없는 시험이라 FAIL).

판정 이름: PASS_2X_60RPS_30M 또는 FAIL_2X_60RPS_30M.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

TARGET_RPS = 60.0
DELIVERED_MIN_SHARE = 0.95
ABNORMAL_MAX = 0.01
ROUTER_CPU_MAX = 50.0
ROUTER_SOFTIRQ_MAX = 20.0
WAS_CPU_MAX = 70.0
HYPERVISOR_CPU_MAX = 70.0
STEAL_MAX = 5.0


def p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(1, math.ceil(0.95 * len(ordered))) - 1]


def _count(metrics: dict, name: str) -> float:
    metric = metrics.get(name) or {}
    if "count" in metric:
        return float(metric["count"])
    return float((metric.get("values") or {}).get("count", 0))


def _rate(metrics: dict, name: str) -> float:
    metric = metrics.get(name) or {}
    if "rate" in metric:
        return float(metric["rate"])
    return float((metric.get("values") or {}).get("rate", 0))


def needed_hosts(inventory: dict | None) -> tuple[str, ...]:
    """표본 파일이 꼭 있어야 하는 이름(앞자리). 라우터는 인벤토리가 라우터를 켰거나 인벤토리를 주지 않았을 때만."""
    if inventory is not None and not inventory["features"]["router"]:
        return ("was", "hypervisor")
    return ("router", "was", "hypervisor")


def judge(summary: dict, cpu: dict[str, list[dict]], needed: tuple[str, ...] = ("router", "was", "hypervisor")) -> dict:
    metrics = summary.get("metrics", {})
    requests = _count(metrics, "http_reqs")
    abnormal = _count(metrics, "http_5xx") + _count(metrics, "http_no_response")
    rate = _rate(metrics, "iterations")
    reasons: list[str] = []
    abnormal_rate = abnormal / requests if requests else None
    if abnormal_rate is None or abnormal_rate > ABNORMAL_MAX:
        reasons.append(f"비정상 비율 {abnormal_rate} > {ABNORMAL_MAX}")
    if rate < TARGET_RPS * DELIVERED_MIN_SHARE:
        reasons.append(f"달성 요청률 {rate:.1f} < {TARGET_RPS * DELIVERED_MIN_SHARE:.1f}(2배 부하가 들어가지 않음)")
    hosts = {}
    for name, samples in sorted(cpu.items()):
        if not samples:
            reasons.append(f"{name} CPU 표본 없음")
            continue
        busy = p95([float(s["busy_pct"]) for s in samples])
        softirq = p95([float(s["softirq_pct"]) for s in samples])
        steal = p95([float(s["steal_pct"]) for s in samples])
        hosts[name] = {"busy_p95": busy, "softirq_p95": softirq, "steal_p95": steal, "samples": len(samples)}
        if name.startswith("router"):
            if busy > ROUTER_CPU_MAX:
                reasons.append(f"{name} CPU {busy} > {ROUTER_CPU_MAX}")
            if softirq > ROUTER_SOFTIRQ_MAX:
                reasons.append(f"{name} softirq {softirq} > {ROUTER_SOFTIRQ_MAX}")
        if name.startswith("was") and busy > WAS_CPU_MAX:
            reasons.append(f"{name} CPU {busy} > {WAS_CPU_MAX}")
        if name == "hypervisor":
            if busy > HYPERVISOR_CPU_MAX:
                reasons.append(f"{name} CPU {busy} > {HYPERVISOR_CPU_MAX}")
        elif steal > STEAL_MAX:
            reasons.append(f"{name} steal {steal} > {STEAL_MAX}")
    for prefix in needed:
        if not any(name == prefix or name.startswith(prefix) for name in cpu):
            reasons.append(f"{prefix} CPU 표본 파일이 없음")
    return {
        "verdict": "PASS_2X_60RPS_30M" if not reasons else "FAIL_2X_60RPS_30M",
        "requests": requests,
        "abnormal": abnormal,
        "abnormal_rate": abnormal_rate,
        "achieved_rps": rate,
        "http_p95_ms": ((metrics.get("http_req_duration") or {}).get("p(95)")),
        "hosts": hosts,
        "reasons": reasons,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--summary", required=True, type=Path)
    parser.add_argument("--cpu", action="append", default=[], help="이름=JSON 줄 파일")
    parser.add_argument("--inventory", type=Path, help="이 테스트베드 인벤토리. 라우터가 없으면 라우터 표본을 요구하지 않는다")
    args = parser.parse_args(argv)
    inventory = json.loads(args.inventory.read_text()) if args.inventory else None
    cpu: dict[str, list[dict]] = {}
    for item in args.cpu:
        name, _, path = item.partition("=")
        cpu[name] = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    result = judge(json.loads(args.summary.read_text()), cpu, needed_hosts(inventory))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["verdict"].startswith("PASS") else 1


if __name__ == "__main__":
    sys.exit(main())
