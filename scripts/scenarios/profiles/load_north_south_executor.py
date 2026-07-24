#!/usr/bin/env python3
"""Trusted executor for the allowlisted commerce north-south k6 profile."""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from executor_common import ExecutorError, bind_level_parameters

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
COMPILER_SPEC = importlib.util.spec_from_file_location("scenario_compile_plan", ROOT / "compile-plan.py")
assert COMPILER_SPEC and COMPILER_SPEC.loader
compiler = importlib.util.module_from_spec(COMPILER_SPEC)
COMPILER_SPEC.loader.exec_module(compiler)


def validate_parameters(scenario_id: str, parameters: dict[str, Any], profile: dict[str, Any]) -> None:
    contract = profile["parameter_contract"]
    required = {
        "target_rps", "ramp_up", "hold", "ramp_down", "entry_url",
        "script_path", "scenario_tag", "seed", "baseline_unit",
    }
    if set(parameters) != required:
        raise ExecutorError(f"parameter keys must be exactly {sorted(required)}")
    if scenario_id not in contract["allowed_scenarios"]:
        raise ExecutorError(f"scenario is not allowlisted: {scenario_id}")
    levels = profile.get("scenario_levels", {}).get(scenario_id)
    if levels is not None and parameters not in [level["parameters"] for level in levels]:
        raise ExecutorError("parameters must exactly match a predeclared scenario level")
    target_rps = parameters["target_rps"]
    if isinstance(target_rps, bool) or not isinstance(target_rps, int):
        raise ExecutorError("target_rps must be an integer")
    bounds = contract["target_rps"]
    if not bounds["minimum"] <= target_rps <= bounds["maximum"]:
        raise ExecutorError("target_rps is outside the approved range")
    for name in ("ramp_up", "hold", "ramp_down"):
        if not re.fullmatch(contract["duration_pattern"], parameters[name]):
            raise ExecutorError(f"invalid duration: {name}")
    if parameters["entry_url"] not in contract["allowed_entry_urls"]:
        raise ExecutorError("entry_url is not allowlisted")
    if parameters["script_path"] not in contract["allowed_script_paths"]:
        raise ExecutorError("script_path is not allowlisted")
    if not re.fullmatch(contract["tag_pattern"], parameters["scenario_tag"]):
        raise ExecutorError("scenario_tag is not allowlisted")
    if parameters["scenario_tag"] != f"scenario_id={scenario_id}":
        raise ExecutorError("scenario_tag must exactly bind the scenario id")
    if parameters["baseline_unit"] not in contract["allowed_baseline_units"]:
        raise ExecutorError("baseline unit is not allowlisted")
    domain_profile = contract["domain_profiles"].get(parameters["entry_url"])
    if domain_profile is None or parameters["baseline_unit"] != domain_profile["baseline_unit"]:
        raise ExecutorError("baseline_unit does not match the entry_url domain profile")
    if isinstance(parameters["seed"], bool) or not isinstance(parameters["seed"], int):
        raise ExecutorError("seed must be an integer")


def confirmation_for(plan: dict[str, Any]) -> str:
    return f"LIVE:{plan['scenario']['id']}:{plan['plan_digest']}"


def load_instance(plan: dict[str, Any]) -> dict[str, Any]:
    matches = [row for row in plan["profile_instances"] if row["profile_id"] == "load.north_south"]
    if len(matches) != 1:
        raise ExecutorError("plan must contain exactly one load.north_south profile")
    return matches[0]


def build_ssh_argv(location: dict[str, Any]) -> list[str]:
    if location.get("transport") != "ssh" or location.get("host") != "192.168.122.206":
        raise ExecutorError("north-south executor requires canonical tb-runner SSH location")
    destination = f"{location.get('user', 'nkia')}@{location['host']}"
    return [
        "/usr/bin/ssh",
        "-i", "/root/.ssh/tb_key",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=10",
        destination,
        "bash", "-s", "--",
    ]


def remote_script() -> bytes:
    return br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; scenario_id="$2"; target_rps="$3"; ramp_up="$4"; hold="$5"; ramp_down="$6"
entry_url="$7"; script_path="$8"; scenario_tag="$9"; seed="${10}"; baseline_unit="${11}"
health_path="${12}"; gateway_env="${13}"; business_step="${14}"; read_step="${15:-}"
safe_id="${scenario_id//[^A-Za-z0-9_-]/_}"
summary="/tmp/rca-scenario-${safe_id}-summary.json"
samples="/tmp/rca-scenario-${safe_id}-samples.json"
live="/tmp/rca-scenario-${safe_id}-live.json"
monitor="/tmp/rca-scenario-${safe_id}-monitor.py"
monitor_pid="/tmp/rca-scenario-${safe_id}-monitor.pid"
log_file="/tmp/rca-scenario-${safe_id}.log"

tagged_pids() {
  local proc arg prev has_tag has_script is_k6 first
  for proc in /proc/[0-9]*; do
    [[ -r "$proc/cmdline" ]] || continue
    has_tag=false; has_script=false; is_k6=false; prev=""; first=true
    while IFS= read -r -d '' arg; do
      if [[ "$first" == true ]]; then [[ "${arg##*/}" == "k6" ]] && is_k6=true; first=false; fi
      [[ "$prev" == "--tag" && "$arg" == "$scenario_tag" ]] && has_tag=true
      [[ "$arg" == "$script_path" ]] && has_script=true
      prev="$arg"
    done < "$proc/cmdline" || true
    [[ "$is_k6" == true && "$has_tag" == true && "$has_script" == true ]] && printf '%s\n' "${proc##*/}"
  done
}

check_read_only() {
  command -v k6 >/dev/null
  command -v curl >/dev/null
  systemctl is-active --quiet "$baseline_unit"
  [[ -r "$script_path" ]]
  [[ "$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "$entry_url$health_path")" == "200" ]]
}

case "$action" in
  preflight)
    check_read_only
    [[ -z "$(tagged_pids)" ]]
    ;;
  run)
    check_read_only
    [[ -z "$(tagged_pids)" ]] || { echo "tagged k6 already running" >&2; exit 4; }
    cat >"$monitor" <<'PY'
# The live document must be rewritten at most once per drain cycle, not per
# parsed line: a per-line fsync cannot keep up with k6's json output at high
# arrival rates, the parser falls minutes behind, and observed_at then trips
# the 30s staleness contract (F07-H run 158b449c, 80rps, 07-19).
import collections, datetime, json, os, sys, time
source, output, scenario_id, business_step, read_step = (sys.argv[1:] + [""])[:5]
iterations = collections.deque()
checkout_results = collections.deque()
read_results = collections.deque()
entry_status = None
last_stamp = None
position = 0
while True:
    parsed_any = False
    try:
        with open(source, encoding="utf-8") as stream:
            stream.seek(position)
            while True:
                line = stream.readline()
                if not line:
                    break
                position = stream.tell()
                if '"iterations"' not in line and '"http_reqs"' not in line:
                    continue
                try:
                    point = json.loads(line)
                    if point.get("type") != "Point":
                        continue
                    data = point.get("data", {})
                    observed = data.get("time")
                    if not observed:
                        continue
                    stamp = datetime.datetime.fromisoformat(observed.replace("Z", "+00:00"))
                    metric = point.get("metric")
                    tags = data.get("tags", {})
                    if metric == "iterations":
                        iterations.append(stamp)
                    elif metric == "http_reqs" and tags.get("step") == business_step:
                        raw = tags.get("status")
                        entry_status = int(raw) if raw and str(raw).isdigit() else 0
                        checkout_results.append((stamp, entry_status))
                    elif metric == "http_reqs" and read_step and tags.get("step") == read_step:
                        raw = tags.get("status")
                        read_results.append((stamp, int(raw) if raw and str(raw).isdigit() else 0))
                    else:
                        continue
                    last_stamp = stamp
                    parsed_any = True
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
    except FileNotFoundError:
        pass
    if parsed_any and last_stamp is not None:
        cutoff = last_stamp - datetime.timedelta(seconds=30)
        while iterations and iterations[0] < cutoff:
            iterations.popleft()
        while checkout_results and checkout_results[0][0] < cutoff:
            checkout_results.popleft()
        while read_results and read_results[0][0] < cutoff:
            read_results.popleft()
        span = max(1.0, min(30.0, (iterations[-1] - iterations[0]).total_seconds())) if len(iterations) > 1 else 1.0
        checkout_count = len(checkout_results)
        business_2xx_rate = (
            sum(200 <= status <= 299 for _, status in checkout_results) / checkout_count
            if checkout_count else 0.0
        )
        business_4xx_rate = (
            sum(400 <= status <= 499 for _, status in checkout_results) / checkout_count
            if checkout_count else 0.0
        )
        business_5xx_rate = (
            sum(status >= 500 for _, status in checkout_results) / checkout_count
            if checkout_count else 0.0
        )
        business_nonok_rate = business_4xx_rate + business_5xx_rate
        # checkout_5xx_rate is kept for backward compatibility (pre-existing
        # query_id/consumer contract); business_5xx_rate is its replacement value.
        checkout_5xx_rate = business_5xx_rate
        document = {
            "scenario_id": scenario_id,
            "scenario_tag": f"scenario_id={scenario_id}",
            "achieved_rps": len(iterations) / span,
            "entry_status": entry_status,
            "checkout_5xx_rate": checkout_5xx_rate,
            "business_2xx_rate": business_2xx_rate,
            "business_4xx_rate": business_4xx_rate,
            "business_5xx_rate": business_5xx_rate,
            "business_nonok_rate": business_nonok_rate,
            "business_ok": entry_status in {200, 400, 409},
            "observed_at": last_stamp.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        if read_step:
            read_count = len(read_results)
            read_2xx_rate = (
                sum(200 <= status <= 299 for _, status in read_results) / read_count
                if read_count else 0.0
            )
            document["read_2xx_rate"] = read_2xx_rate
            document["read_nonok_rate"] = (
                sum(status >= 400 or status == 0 for _, status in read_results) / read_count
                if read_count else 0.0
            )
        temporary = output + ".tmp"
        with open(temporary, "w", encoding="utf-8") as target:
            json.dump(document, target, sort_keys=True)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, output)
    time.sleep(1)
PY
    rm -f -- "$samples" "$live"
    nohup k6 run --tag "$scenario_tag" \
      --env "$gateway_env=$entry_url" --env "TARGET_RPS=$target_rps" \
      --env "RAMP_UP=$ramp_up" --env "HOLD=$hold" --env "RAMP_DOWN=$ramp_down" \
      --env "SURGE_SEED=$seed" --out "json=$samples" --summary-export "$summary" "$script_path" \
      >"$log_file" 2>&1 &
    nohup python3 "$monitor" "$samples" "$live" "$scenario_id" "$business_step" "$read_step" \
      >>"$log_file" 2>&1 & echo $! >"$monitor_pid"
    ;;
  cleanup)
    mapfile -t pids < <(tagged_pids)
    ((${#pids[@]} == 0)) || kill -TERM "${pids[@]}" 2>/dev/null || true
    for _ in {1..20}; do [[ -z "$(tagged_pids)" ]] && break; sleep 0.25; done
    mapfile -t pids < <(tagged_pids)
    ((${#pids[@]} == 0)) || kill -KILL "${pids[@]}" 2>/dev/null || true
    if [[ -r "$monitor_pid" ]]; then kill -TERM "$(cat "$monitor_pid")" 2>/dev/null || true; fi
    rm -f -- "$summary" "$samples" "$live" "$monitor" "$monitor_pid" "$log_file"
    ;;
  recovery)
    check_read_only
    [[ -z "$(tagged_pids)" ]]
    ;;
  *) echo "unsupported remote action: $action" >&2; exit 2 ;;
esac
'''


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = load_instance(plan)
    parameters = instance["parameters"]
    profiles = json.loads((ROOT / "registry" / "profiles.json").read_text())
    contract = profiles["profiles"]["load.north_south"]["parameter_contract"]
    domain_profile = contract["domain_profiles"].get(parameters["entry_url"])
    if domain_profile is None:
        raise ExecutorError("entry_url has no domain profile")
    argv = build_ssh_argv(instance["location"])
    argv.extend([
        action,
        plan["scenario"]["id"],
        str(parameters["target_rps"]),
        parameters["ramp_up"],
        parameters["hold"],
        parameters["ramp_down"],
        parameters["entry_url"],
        parameters["script_path"],
        parameters["scenario_tag"],
        str(parameters["seed"]),
        parameters["baseline_unit"],
        domain_profile["health_path"],
        domain_profile["gateway_env"],
        domain_profile["business_step"],
        domain_profile.get("read_step", ""),
    ])
    return argv, remote_script()


def execute(argv: Sequence[str], stdin: bytes) -> int:
    completed = subprocess.run(argv, input=stdin, timeout=900, check=False)
    return completed.returncode


def authorize_live(plan: dict[str, Any], plan_digest: str | None, confirmation: str | None) -> None:
    if not plan["live_allowed"]:
        raise ExecutorError("normalized plan does not allow live execution")
    if plan_digest != plan["plan_digest"]:
        raise ExecutorError("--plan-digest must exactly match the normalized plan")
    if confirmation != confirmation_for(plan):
        raise ExecutorError("--confirm must exactly match the normalized live confirmation")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", nargs="?", default="dry-run", choices=["preflight", "run", "cleanup", "recovery", "dry-run"])
    parser.add_argument("--scenario")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--plan-digest")
    parser.add_argument("--confirm")
    parser.add_argument("--level-index", type=int)
    parser.add_argument("--parameters-json")
    args = parser.parse_args()

    if not args.scenario:
        if args.live:
            raise ExecutorError("--scenario is required for live execution")
        print(json.dumps({"schema_version": "1.0", "side_effects": False, "profile_id": "load.north_south", "action": args.action, "live_requested": False, "live_supported": True}, sort_keys=True))
        return 0

    plan = compiler.compile_plan(args.scenario)
    selected_plan, selected_level_id = bind_level_parameters(
        plan, "load.north_south", args.level_index, args.parameters_json
    )
    instance = load_instance(selected_plan)
    profiles = json.loads((ROOT / "registry" / "profiles.json").read_text())
    profile = profiles["profiles"]["load.north_south"]
    validate_parameters(plan["scenario"]["id"], instance["parameters"], profile)
    argv, stdin = build_invocation(selected_plan, "preflight" if args.action == "dry-run" else args.action)
    output = {
        "schema_version": "1.0",
        "side_effects": False,
        "profile_id": "load.north_south",
        "action": args.action,
        "scenario_id": plan["scenario"]["id"],
        "plan_digest": plan["plan_digest"],
        "confirmation": confirmation_for(plan),
        "parameters": instance["parameters"],
        "selected_level_id": selected_level_id,
        "invocation": {"argv": argv, "stdin_sha256": __import__("hashlib").sha256(stdin).hexdigest()},
        "live_allowed": plan["live_allowed"],
    }
    if not args.live:
        print(json.dumps(output, sort_keys=True))
        return 0
    authorize_live(plan, args.plan_digest, args.confirm)
    return execute(argv, stdin)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ExecutorError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        raise SystemExit(3)
