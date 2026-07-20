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

# F09-R (worker CPU noisy neighbor) is a calibration ladder, not a single contract.
# tb-w3 (192.168.122.14) has 4 cores and no stress-ng, so intensity is CPU-only busy
# loops (yes) at 50/75/100% of the node — memory is untouched to stay clear of the
# eviction/OOM surface owned by F05-P.
F09R_LEVELS = [
    {"mode": "cpu", "host": "192.168.122.14", "cpu_workers": 2, "runtime_seconds": 480},
    {"mode": "cpu", "host": "192.168.122.14", "cpu_workers": 3, "runtime_seconds": 480},
    {"mode": "cpu", "host": "192.168.122.14", "cpu_workers": 4, "runtime_seconds": 480},
]

# F05-P (worker node memory pressure) is a calibration ladder that drives tb-w2
# (192.168.122.11) toward the kubelet hard-eviction threshold (memory.available<100Mi)
# so several co-located commerce/food pods are evicted and their APIs fail during the
# reschedule gap. tb-w2 has 4 cores, ~11.9G total / ~8G reclaimable-available memory and
# no stress-ng, so the burner is a stress-ng-free anonymous-memory hog (python touches
# every page) held for the window — CPU stays free to keep this distinct from F09-R's
# CPU surface. Ladder MiB are static estimates from the 07-20 read-only headroom probe
# (free -m available ~7985 MiB above 3944 MiB used); the eviction knee (between the
# 7000 and 8500 steps) is confirmed by live calibration, never by static measurement.
#
# `required_cohort` is the fail-closed runtime placement gate: commerce has no
# nodeSelector/affinity so the cohort on tb-w2 drifts across reschedules (pod name
# suffixes already changed between probes). The gate re-verifies the *current* pods on
# the node by deployment-name prefix (crictl pods, node-local, no kubeconfig) before any
# allocation and refuses if the expected multi-service cohort is not actually co-located.
_F05P_COHORT = [
    "testbed-gateway", "testbed-cart", "testbed-inventory",
    "testbed-redis", "testbed-kafka", "testbed-payment",
]
F05P_LEVELS = [
    {"mode": "memhog", "host": "192.168.122.11", "mib": 5500, "runtime_seconds": 480, "required_cohort": _F05P_COHORT},
    {"mode": "memhog", "host": "192.168.122.11", "mib": 7000, "runtime_seconds": 480, "required_cohort": _F05P_COHORT},
    {"mode": "memhog", "host": "192.168.122.11", "mib": 8500, "runtime_seconds": 480, "required_cohort": _F05P_COHORT},
]


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    del profile
    if scenario_id == "F09-R":
        if params not in F09R_LEVELS:
            raise ExecutorError("parameters do not match a measured F09-R CPU noisy-neighbor level")
        return
    if scenario_id == "F05-P":
        if params not in F05P_LEVELS:
            raise ExecutorError("parameters do not match a measured F05-P memory-pressure level")
        return
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
    elif p["mode"] == "cpu":
        args += [str(p["cpu_workers"]), str(p["runtime_seconds"])]
    elif p["mode"] == "memhog":
        args += [str(p["mib"]), str(p["runtime_seconds"]), ",".join(p["required_cohort"])]
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
 cpu)
  # stress-ng-free CPU noisy neighbor: N `yes` busy loops at normal priority (no nice),
  # confined to one session so cleanup can reap the whole group by negative PGID. The
  # burner writes its own leader pid ($$ == pgid under setsid) so we never guess it, and a
  # self-bounded sleep + trap kill 0 guarantees teardown even if the controller never calls cleanup.
  cpu="$1"; runtime="$2"; pgidfile="$state_root/${scenario}.pgid"
  grouplive() { [[ -s "$pgidfile" ]] && kill -0 -"$(cat "$pgidfile")" 2>/dev/null; }
  case "$action" in
   preflight) command -v yes >/dev/null; command -v setsid >/dev/null; [[ ! -e "$pgidfile" ]]; ! grouplive ;;
   run) [[ ! -e "$pgidfile" ]]; ! grouplive
     setsid bash -c 'echo $$ >"'"$pgidfile"'.tmp"; mv -T "'"$pgidfile"'.tmp" "'"$pgidfile"'"; trap "kill 0" EXIT; for _ in $(seq 1 '"$cpu"'); do yes >/dev/null 2>&1 & done; sleep '"$runtime"'' >"$state_root/${scenario}.log" 2>&1 &
     for _ in {1..15}; do [[ -s "$pgidfile" ]] && break; sleep 0.2; done; [[ -s "$pgidfile" ]] ;;
   cleanup)
     if grouplive; then kill -TERM -"$(cat "$pgidfile")" 2>/dev/null || true; for _ in {1..30}; do grouplive || break; sleep 1; done; grouplive && kill -KILL -"$(cat "$pgidfile")" 2>/dev/null || true; fi
     rm -f "$pgidfile" "$pgidfile.tmp" "$state_root/${scenario}.log" ;;
   recovery) ! grouplive; [[ ! -e "$pgidfile" ]] ;;
   *) exit 2 ;;
  esac ;;
 memhog)
  # stress-ng-free node memory-pressure burner: a single python process anonymously
  # allocates $mib MiB and touches every page (bytearray, 1 MiB stride) so the pages are
  # resident RSS, then holds for $runtime. Confined to a setsid session with a self-bound
  # sleep + trap kill 0 so cleanup reaps the whole group by negative PGID (identical
  # lifecycle contract to the cpu mode). CPU stays idle to keep this distinct from F09-R.
  #
  # Placement gate (fail-closed): before allocating we re-list the pods actually resident
  # on THIS node via node-local crictl and require every deployment prefix in $cohort to
  # be present. commerce has no nodeSelector/affinity so the cohort drifts; a stale target
  # map must not silently pressure the wrong node. Missing crictl, a crictl error, or any
  # absent cohort member aborts before a single byte is allocated.
  mib="$1"; runtime="$2"; cohort="$3"; pgidfile="$state_root/${scenario}.pgid"
  grouplive() { [[ -s "$pgidfile" ]] && kill -0 -"$(cat "$pgidfile")" 2>/dev/null; }
  placement_ok() {
    command -v crictl >/dev/null || return 1
    local pods; pods="$(crictl pods --state Ready -o json 2>/dev/null)" || return 1
    [[ -n "$pods" ]] || return 1
    local names; names="$(printf '%s' "$pods" | python3 -c 'import sys,json
d=json.load(sys.stdin)
print("\n".join(p["metadata"]["name"] for p in d.get("items",[])))' 2>/dev/null)" || return 1
    local prefix
    for prefix in ${cohort//,/ }; do
      printf '%s\n' "$names" | grep -q "^${prefix}-\?" || return 1
    done
  }
  case "$action" in
   preflight) command -v python3 >/dev/null; command -v setsid >/dev/null; placement_ok; [[ ! -e "$pgidfile" ]]; ! grouplive ;;
   run) [[ ! -e "$pgidfile" ]]; ! grouplive; placement_ok
     setsid bash -c 'echo $$ >"'"$pgidfile"'.tmp"; mv -T "'"$pgidfile"'.tmp" "'"$pgidfile"'"; trap "kill 0" EXIT; python3 -c "import sys,time
mib=int(sys.argv[1]); hold=int(sys.argv[2])
buf=bytearray(mib*1024*1024)
for off in range(0, len(buf), 1024*1024):
    buf[off]=1
time.sleep(hold)" '"$mib"' '"$runtime"'' >"$state_root/${scenario}.log" 2>&1 &
     for _ in {1..15}; do [[ -s "$pgidfile" ]] && break; sleep 0.2; done; [[ -s "$pgidfile" ]] ;;
   cleanup)
     if grouplive; then kill -TERM -"$(cat "$pgidfile")" 2>/dev/null || true; for _ in {1..30}; do grouplive || break; sleep 1; done; grouplive && kill -KILL -"$(cat "$pgidfile")" 2>/dev/null || true; fi
     rm -f "$pgidfile" "$pgidfile.tmp" "$state_root/${scenario}.log" ;;
   recovery) ! grouplive; [[ ! -e "$pgidfile" ]] ;;
   *) exit 2 ;;
  esac ;;
 *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
