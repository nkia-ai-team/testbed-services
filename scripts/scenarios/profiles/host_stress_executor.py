#!/usr/bin/env python3
"""Bounded host/PVC stress foundations with exact process and file recovery."""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, profile_instance

PROFILE_ID = "host.stress"

CONTRACTS: dict[str, dict[str, Any]] = {
    "F02-H": {"mode": "fio", "host": "192.168.122.184", "target_dir": "/opt/local-path-provisioner/pvc-5d71e22a-1225-4505-a7cc-5cf29dad4cf5_rca-testbed-commerce_pgdata-testbed-postgres-0", "size_mib": 2048, "runtime_seconds": 600, "rate_iops": 3000},
    "F10-R": {"mode": "watermark", "host": "192.168.122.184", "target_dir": "/opt/local-path-provisioner/pvc-5d71e22a-1225-4505-a7cc-5cf29dad4cf5_rca-testbed-commerce_pgdata-testbed-postgres-0", "watermark_percent": 85, "reserve_mib": 10240, "maximum_fill_mib": 51200},
    "F10-H": {"mode": "fio", "host": "192.168.122.14", "target_dir": "/opt/local-path-provisioner/pvc-3439d85f-f921-4b19-8808-c679506a31dd_rca-testbed-food_mysqldata-testbed-mysql-0", "size_mib": 2048, "runtime_seconds": 600, "rate_iops": 4000},
    "F10-P": {"mode": "fio", "host": "192.168.122.184", "target_dir": "/opt/local-path-provisioner/pvc-01f9e717-727a-4227-a09b-584ec371c99f_rca-testbed-banking_oracledata-testbed-oracle-0", "size_mib": 2048, "runtime_seconds": 600, "rate_iops": 3000},
    "F15-P": {"mode": "pressure", "host": "192.168.122.11", "cpu_workers": 2, "vm_workers": 1, "vm_bytes": "512M", "runtime_seconds": 600},
}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    del profile
    expected = CONTRACTS.get(scenario_id)
    if expected is None:
        raise ExecutorError("scenario has no verified bounded host-stress contract")
    if params != expected:
        raise ExecutorError("parameters do not exactly match the verified host-stress contract")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    validate(plan["scenario"]["id"], p, {})
    if instance["location"].get("transport") != "ssh" or instance["location"].get("host") != p["host"]:
        raise ExecutorError("host.stress location must match the measured worker")
    args = [action, plan["scenario"]["id"], p["mode"], p["host"]]
    if p["mode"] == "fio":
        args += [p["target_dir"], str(p["size_mib"]), str(p["runtime_seconds"]), str(p["rate_iops"])]
    elif p["mode"] == "watermark":
        args += [p["target_dir"], str(p["watermark_percent"]), str(p["reserve_mib"]), str(p["maximum_fill_mib"])]
    else:
        args += [str(p["cpu_workers"]), str(p["vm_workers"]), p["vm_bytes"], str(p["runtime_seconds"])]
    return ["/usr/bin/ssh", "-i", "/root/.ssh/tb_key", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10", f"nkia@{p['host']}", "sudo", "bash", "-s", "--", *args], REMOTE


REMOTE = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; scenario="$2"; mode="$3"; expected_host="$4"; shift 4
state_root=/var/lib/lucida/scenario-profile-state; mkdir -p "$state_root"; chmod 700 "$state_root"
pidfile="$state_root/${scenario}.pid"; artifact=""
alive() { [[ -s "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; }
stop() { if alive; then kill "$(cat "$pidfile")"; for _ in {1..30}; do alive || break; sleep 1; done; alive && kill -9 "$(cat "$pidfile")"; fi; rm -f "$pidfile"; }
case "$mode" in
 fio)
  target="$1"; size="$2"; runtime="$3"; rate="$4"; artifact="$target/.lucida-${scenario}-fio"
  check() { command -v fio >/dev/null; [[ -d "$target" && ! -L "$target" ]]; [[ "$(df -P "$target" | awk 'NR==2{print $4}')" -gt $((size*1024+1048576)) ]]; }
  case "$action" in preflight) check; [[ ! -e "$artifact" ]] && ! alive;; run) check; [[ ! -e "$artifact" ]] && ! alive; nohup fio --name="lucida-$scenario" --filename="$artifact" --size="${size}M" --rw=randwrite --bs=4k --iodepth=16 --direct=1 --time_based --runtime="$runtime" --rate_iops="$rate" --end_fsync=1 >"$state_root/${scenario}.log" 2>&1 </dev/null & echo $! >"$pidfile";; cleanup) stop; rm -f -- "$artifact" "$state_root/${scenario}.log";; recovery) ! alive; [[ ! -e "$artifact" ]];; *) exit 2;; esac ;;
 watermark)
  target="$1"; watermark="$2"; reserve_mib="$3"; max_mib="$4"; artifact="$target/.lucida-${scenario}-watermark"
  calculate() { read -r blocks used avail < <(df -Pk "$target" | awk 'NR==2{print $2,$3,$4}'); desired=$((blocks*watermark/100-used)); reserve_kib=$((reserve_mib*1024)); (( desired > avail-reserve_kib )) && desired=$((avail-reserve_kib)); (( desired > max_mib*1024 )) && desired=$((max_mib*1024)); (( desired > 0 )); }
  case "$action" in preflight) [[ -d "$target" && ! -L "$target" && ! -e "$artifact" ]]; calculate;; run) [[ ! -e "$artifact" ]]; calculate; fallocate -l "${desired}K" "$artifact"; sync -f "$artifact";; cleanup) rm -f -- "$artifact"; sync -f "$target";; recovery) [[ ! -e "$artifact" ]];; *) exit 2;; esac ;;
 pressure)
  cpu="$1"; vm="$2"; bytes="$3"; runtime="$4"
  case "$action" in preflight) command -v stress-ng >/dev/null; ! alive;; run) ! alive; nohup stress-ng --cpu "$cpu" --vm "$vm" --vm-bytes "$bytes" --timeout "${runtime}s" --metrics-brief >"$state_root/${scenario}.log" 2>&1 </dev/null & echo $! >"$pidfile";; cleanup) stop; rm -f "$state_root/${scenario}.log";; recovery) ! alive;; *) exit 2;; esac ;;
 *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
