#!/usr/bin/env python3
"""civil-petition VM 실행기 시험 실행 도구(설계 docs/spec-testbed-vm-3tier.md §10 T4 "시험 실행", "자가 복구 실측").

실행기에는 시나리오 계약이 없다(시나리오는 사용자와 같이 설계, 설계 §7). 시험 실행은 녹화가 아니므로 catalog 와 registry 를
바꾸지 않고, 이 도구가 계약 하나를 그 자리에서만 실행기에 묶어 같은 build_invocation 으로 부른다. 허용 목록, 내용 가드,
U8 가드, 늘 열린 연결 검사는 실행기 그대로다. 러너 컨테이너 안에서 돈다(키, known_hosts, ControlPath 가 거기 있다).

  vm-executor-trial.py --profile <실행기> --params <계약 JSON 파일> --action preflight|run|cleanup|recovery
                       [--max-injection 10m] [--live --confirm TRIAL:<실행기>:<계약 sha256 앞 12자>]
--live 없이는 부르지 않고 무엇을 부를지만 낸다(argv, stdin sha256, 가드 기한, 확인 문자열).
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
import unittest.mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "profiles"))
from executor_common import ExecutorError, execute, guard_expiry_seconds, with_failure_trace  # noqa: E402

EXECUTORS = {
    "vmlink.state": "vmlink_state_executor", "vmlink.qdisc": "vmlink_qdisc_executor",
    "router.qdisc": "router_qdisc_executor", "router.config": "router_config_executor",
    "router.resource": "router_resource_executor", "vmhost.stress": "vmhost_stress_executor",
    "vmhost.job": "vmhost_job_executor", "vmhost.firewall": "vmhost_firewall_executor",
    "vm.service": "vm_service_executor", "vmdb.lock": "vmdb_lock_executor",
    "vmdb.workload": "vmdb_workload_executor", "vmload.north_south": "vmload_north_south_executor",
}
TRIAL_ID = "TRIAL"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, HERE / "profiles" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def plan_for(profile_id: str, params: dict, max_injection: str) -> dict:
    locations = json.loads((HERE / "registry" / "locations.json").read_text())["locations"]
    allowed = json.loads((HERE / "registry" / "profiles.json").read_text())["profiles"][profile_id]["allowed_locations"]
    match = [lid for lid in allowed if locations[lid].get("host") == params.get("host")
             and locations[lid].get("node") == params.get("node")]
    if len(match) != 1:
        raise ExecutorError("host and node must be one of the executor's registry locations")
    return {"scenario": {"id": TRIAL_ID}, "controller": {"runtime": {"max_injection_duration": max_injection}},
            "profile_instances": [{"profile_id": profile_id, "parameters": params, "location_id": match[0],
                                   "location": locations[match[0]]}]}


def confirmation(profile_id: str, params: dict) -> str:
    digest = hashlib.sha256(json.dumps(params, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f"TRIAL:{profile_id}:{digest[:12]}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", required=True, choices=sorted(EXECUTORS))
    parser.add_argument("--params", required=True, type=Path)
    parser.add_argument("--action", required=True, choices=["preflight", "run", "cleanup", "recovery"])
    parser.add_argument("--max-injection", default="10m")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--confirm")
    args = parser.parse_args(argv)
    try:
        params = json.loads(args.params.read_text())
        module = load(EXECUTORS[args.profile])
        plan = plan_for(args.profile, params, args.max_injection)
        with unittest.mock.patch.dict(module.CONTRACTS, {TRIAL_ID: params}):
            command, stdin = module.build_invocation(plan, args.action)
        stdin = with_failure_trace(stdin)
        expected = confirmation(args.profile, params)
        print(json.dumps({"profile_id": args.profile, "action": args.action, "argv": command,
                          "stdin_sha256": hashlib.sha256(stdin).hexdigest(), "guard_expiry_seconds": guard_expiry_seconds(plan),
                          "confirmation": expected, "live": args.live}, ensure_ascii=False, sort_keys=True))
        if not args.live:
            return 0
        if args.confirm != expected:
            raise ExecutorError("--confirm must be the confirmation printed by a dry run of the same contract")
        return execute(command, stdin)
    except (ExecutorError, OSError, json.JSONDecodeError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
