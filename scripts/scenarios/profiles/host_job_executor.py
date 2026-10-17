#!/usr/bin/env python3
"""Routine host maintenance job executor (a scheduled job whose memory grows with its target).

F80-R reconstructs GitHub 2026-09-20 (githubstatus.com/incidents/f6yrxnz5f7bs): "a routine
repository maintenance job for an unusually large repository consuming nearly all of the
memory" on one Git storage server left that server unable to serve the Git operations that
create merge commits; the server was removed from service at 22:18 UTC and the follow-up is
to cap the memory one maintenance job can take.

Here the server is the banking worker tb-w2 and the routine job is the Oracle datafile backup.
It runs two compression streams side by side, the usual split for a backup with one datafile far
larger than the rest: the largest datafile (FREEPDB1/users01.dbf, 5.4 GiB) on its own, and the
rest of the Oracle volume directory (about 3.7 GiB) as one tar stream. Both use `zstd --long=31`,
whose long-distance window (2 GiB) fills as the input passes it, so each stream's resident size
follows its target up to the window: 2,395,520 kB measured on 2.6 GiB of the real users01.dbf
(66 tb-ops, zstd 1.5.5 -12 --long=31 -T1, 12.7 MB/s) and 2,404,096 kB on the real rest of the
directory (21.5 MB/s), about 4.6 GiB for the two between about 170 and 183 seconds. That is more
than all the memory tb-w2 has left (2,257-2,904 MiB since the move to 66). The job runs as a
transient systemd unit outside every pod cgroup, so the kubelet cannot evict it.

The unit carries its own memory bound (MemoryHigh, MemoryMax), sized at start from the node's
available memory so that a fixed reserve stays outside the job. Without it the job took the node
past the point where the kernel can still make progress: the page cache that holds every running
binary was reclaimed, the node thrashed (load 227, NotReady, ssh unreachable for minutes) and the
kernel OOM killer only fired about nine minutes later. A node in that state also takes the node
agents with it, so the evidence the scenario exists for is lost. With the bound the job grows to
its soft limit and is throttled there, holding the memory; the workloads beside it lose their page
cache and slow down, while the node keeps enough to stay reachable and collectable.

Every step is one plain command on the node. The node's auth log (forwarded to 119) records the
`sudo systemd-run` that starts the unit, systemd logs the unit start, and the process list shows
tar and zstd. Nothing carries the scenario id: the unit is named after the job, the output file
after the date, and no state file is written (the unit itself is the state).
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance

PROFILE_ID = "host.job"

# 2026-10-16. F80-R: tb-w2 Oracle 볼륨(PVC oracledata-testbed-oracle-0)의 백업. 가장 큰 데이터 파일 users01.dbf
# (5,369,765,888 바이트)와 나머지 FREE 디렉터리(약 3.7 GiB, du -sb 9,303,835,648 바이트 합계)를 두 압축 흐름으로
# 나란히 zstd -12 --long=31 한다. 실측(2026-10-16, 실제 파일): users01.dbf 앞 2,600 MiB 에서 최대 RSS 2,395,520 kB,
# 12.7 MB/s(창이 차는 데 약 170초, 흐름 끝 약 420초), 나머지 디렉터리 tar 3,934,085,120 바이트에서 최대 RSS
# 2,404,096 kB, 21.5 MB/s(창이 차는 데 약 100초, 흐름 끝 약 183초). 두 흐름 약 4,678 MiB 대 tb-w2 여유(66 이전 뒤 1분 표본 2,004개: 최소 2,257, p05 2,631, p50 2,700,
# p95 2,780, 최대 2,904 MiB). preflight 는 여유가 작업보다 1 GiB 이상 작을 때(≤ 3,584 MiB)만, 그리고 노드가 이미
# 압박 중이 아닐 때(≥ 1,536 MiB)만 주입한다. RuntimeMaxSec 600 이 작업을 스스로 끝낸다.
# 2026-10-17: 상한 없는 작업이 노드를 무너뜨렸다(F80-R-run-bad63a76: 주입 뒤 약 100초에 이체 5xx 0.8 로 피해는 났지만
# 곧 파일 캐시가 바닥나 노드가 헛돌았고 load 227, NotReady, SSH 불통, 커널 전역 OOM 은 약 9분 뒤에야 났다). 그래서 유닛에
# 메모리 상한을 단다: MemoryHigh = 시작 때 MemAvailable − 768 MiB(노드 몫으로 남김), MemoryMax = MemoryHigh + 256 MiB
# (넘으면 이 유닛 안에서만 커널이 죽인다). 작업은 MemoryHigh 에서 멈칫거리며 메모리를 쥐고 있고, 노드는 최소 512 MiB 를 지킨다.
CONTRACTS = {
    "F80-R": {
        "host": "192.168.122.11",
        "node": "tb-w2",
        "namespace": "rca-testbed-banking",
        "pvc": "oracledata-testbed-oracle-0",
        "source": "FREE",
        "largest_file": "FREE/FREEPDB1/users01.dbf",
        "largest_min_mib": 2048,
        "unit": "oradata-backup",
        "description": "Oracle datafile backup",
        "output_dir": "/var/backups/oradata",
        "level": 12,
        "long_window_log": 31,
        "nice": 10,
        "runtime_seconds": 600,
        "min_free_mib": 20480,
        "min_available_mib": 1536,
        "max_available_mib": 3584,
        "memory_reserve_mib": 768,
        "memory_hard_margin_mib": 256,
        "deployments": ["testbed-nginx", "testbed-api", "testbed-account", "testbed-transfer", "testbed-ledger"],
        "statefulsets": ["testbed-oracle"],
    },
}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    contract = CONTRACTS.get(scenario_id)
    if contract is None:
        raise ExecutorError("scenario has no verified host maintenance job contract")
    allowed = profile.get("parameter_contract", {}).get("allowed_scenarios")
    if allowed is not None and scenario_id not in allowed:
        raise ExecutorError("scenario is not enabled by the profile registry")
    if params != contract:
        raise ExecutorError("parameters must exactly match the approved host maintenance job contract")
    if not params["output_dir"].startswith("/var/backups/") or params["output_dir"].count("/") != 3:
        raise ExecutorError("output_dir must be one directory directly under /var/backups")
    if any("," in name for name in params["deployments"] + params["statefulsets"]):
        raise ExecutorError("workload names must not contain commas")
    # The soft limit is what was available minus the reserve, so the reserve must be smaller than the
    # least memory preflight accepts, and the hard margin must not eat the reserve.
    if not 0 < params["memory_hard_margin_mib"] < params["memory_reserve_mib"] < params["min_available_mib"]:
        raise ExecutorError("memory bound must satisfy 0 < hard margin < reserve < min available")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    validate(plan["scenario"]["id"], p, {})
    location = instance["location"]
    if location.get("transport") != "ssh" or location.get("host") != p["host"] or location.get("node") != p["node"]:
        raise ExecutorError("host maintenance job location must be the measured worker")
    return kubectl_bash_argv([
        action, p["host"], p["node"], p["namespace"], p["pvc"], p["source"], p["largest_file"],
        str(p["largest_min_mib"]), p["unit"], p["description"], p["output_dir"], str(p["level"]),
        str(p["long_window_log"]), str(p["nice"]), str(p["runtime_seconds"]), str(p["min_free_mib"]),
        str(p["min_available_mib"]), str(p["max_available_mib"]),
        str(p["memory_reserve_mib"]), str(p["memory_hard_margin_mib"]),
        ",".join(p["deployments"]), ",".join(p["statefulsets"]),
    ]), SCRIPT


SCRIPT = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; host="$2"; node="$3"; ns="$4"; pvc="$5"; source="$6"; largest="$7"; largest_min_mib="$8"
unit="$9"; desc="${10}"; outdir="${11}"; level="${12}"; wlog="${13}"; nice="${14}"; runtime="${15}"
min_free_mib="${16}"; min_avail_mib="${17}"; max_avail_mib="${18}"; reserve_mib="${19}"; margin_mib="${20}"
deploys="${21}"; sets="${22}"
# How long cleanup waits for the banking workloads after stopping the job. Measured on 66: app pods
# become Ready in 95-130s and can take about 5 minutes under load (host.image note); Oracle restarts
# with crash recovery when it was evicted or killed. 420s matches the other node executors.
cleanup_wait=420s
k=(kubectl --kubeconfig=/root/tb-kubeconfig)
IFS=, read -r -a dep_list <<<"$deploys"
IFS=, read -r -a sts_list <<<"$sets"
node_sh() {
  ssh -i /root/.ssh/tb_key -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 "nkia@$host" "$@"
}
# One remote command line with every argument shell-quoted: ssh joins argv with spaces.
node_q() { node_sh "$(printf '%q ' "$@")"; }
# A remote test whose exit status is the answer. ssh exits 255 when it cannot reach the node, and
# that must never read as "no": an unreachable node once read as an idle unit and an absent output.
node_test() {
  local rc=0
  node_q "$@" || rc=$?
  if (( rc == 255 )); then echo "ssh to $host failed during: $*" >&2; exit 1; fi
  return "$rc"
}
# Cleanup can start while a memory-starved node is still not answering ssh (it comes back once the
# pressure ends). Retry with a bounded backoff instead of failing on the first banner timeout. The
# delays add up to 230s and each attempt is bounded by ConnectTimeout, so the wait stays under about
# 6 minutes and wait + stop + the workload wait below fit the executor's 900s limit.
ssh_backoff=(5 10 15 20 30 30 30 30 30 30)
wait_node() {
  local delay last
  last=$(node_sh true 2>&1) && return 0
  for delay in "${ssh_backoff[@]}"; do
    sleep "$delay"
    last=$(node_sh true 2>&1) && return 0
  done
  echo "node $host unreachable over ssh after ${#ssh_backoff[@]} retries: $last" >&2
  return 1
}
# The node directory behind the Oracle volume, resolved every time: the PV was re-provisioned once
# (2c369013 -> 899a2f44) and a pinned path would then point at nothing.
datadir() {
  local pv; pv=$("${k[@]}" -n "$ns" get pvc "$pvc" -o jsonpath='{.spec.volumeName}')
  [[ -n "$pv" ]]
  "${k[@]}" get pv "$pv" -o json | jq -er --arg n "$node" '
    select([.spec.nodeAffinity.required.nodeSelectorTerms[]?.matchExpressions[]?.values[]?] | index($n))
    | (.spec.hostPath.path // .spec.local.path)'
}
unit_active() { node_test systemctl is-active --quiet "$unit.service"; }
# A function, not `! unit_active`: set -e ignores a negated command, so a bare `! unit_active`
# in the middle of a branch would never stop the script.
unit_idle() { if unit_active; then return 1; fi; }
out_absent() { node_test test ! -e "$outdir"; }
mem_available_kib() { node_sh "awk '/^MemAvailable:/{print \$2}' /proc/meminfo"; }
# Status fields rather than `kubectl rollout status` (same reason as k8s.env and host.image).
healthy() {
  local deadline=$((SECONDS + ${1%s})) ok d s
  while :; do
    ok=1
    for d in "${dep_list[@]}"; do
      "${k[@]}" -n "$ns" get deploy "$d" -o json | jq -e '
        .status.observedGeneration >= .metadata.generation
        and ((.status.updatedReplicas // 0) == .spec.replicas)
        and ((.status.availableReplicas // 0) == .spec.replicas)' >/dev/null || ok=0
    done
    for s in "${sts_list[@]}"; do
      "${k[@]}" -n "$ns" get sts "$s" -o json | jq -e '((.status.readyReplicas // 0) == .spec.replicas)' >/dev/null || ok=0
    done
    (( ok == 1 )) && return 0
    (( SECONDS < deadline )) || return 1
    sleep 5
  done
}
check() {
  command -v kubectl >/dev/null; command -v jq >/dev/null; command -v ssh >/dev/null
  # The job's tools are the node's own (Ubuntu 24.04: tar, zstd 1.5.5, systemd 255).
  node_q sh -c 'command -v tar && command -v zstd && command -v systemd-run' >/dev/null
  # The volume lives on this node and the pod that uses it runs here.
  [[ "$("${k[@]}" -n "$ns" get pod "${sts_list[0]}-0" -o jsonpath='{.spec.nodeName}')" == "$node" ]]
  dir=$(datadir); [[ "$dir" == /* ]]
  # The target is large enough to fill the long-distance window (the job's memory follows it).
  size=$(node_q sudo -n stat -c %s "$dir/$largest")
  (( size >= largest_min_mib * 1024 * 1024 ))
  # Room for the compressed output on the root file system, far from the kubelet disk thresholds.
  free_mib=$(node_sh "df -Pm /var/backups | awk 'NR==2{print \$4}'")
  (( free_mib >= min_free_mib ))
  # The job's two streams hold about 4.6 GiB. Inject only while the node's available memory is at
  # least 1 GiB below that (so the job certainly exhausts it) and the node is not already pressed.
  avail_mib=$(( $(mem_available_kib) / 1024 ))
  (( avail_mib <= max_avail_mib && avail_mib >= min_avail_mib ))
  healthy 1s
}
case "$action" in
  preflight) check; unit_idle; out_absent ;;
  # The job is started the way a scheduled backup runs: a systemd unit outside every pod, with a
  # lowered CPU and IO priority and its own runtime bound, so it ends even if cleanup never comes.
  # run does not wait for the job (waiting while the coordinator lock is held expires the lease).
  # Its memory is bounded from what the node has available now: MemoryHigh leaves the reserve to the
  # node, so the job takes the page cache and squeezes the workloads beside it without starving the
  # node itself; MemoryMax a little above is the backstop (the kernel then kills inside this unit,
  # never node-wide).
  run) check; unit_idle; out_absent
    high_mib=$(( avail_mib - reserve_mib )); max_mib=$(( high_mib + margin_mib ))
    (( high_mib > 0 ))
    stamp=$(date -u +%Y%m%d); big=$(basename "$largest" .dbf)
    # Two streams side by side: the largest datafile alone, the rest of the directory as one tar.
    inner=$(printf 'mkdir -p %q && cd %q && { tar -cf - --exclude=%q %q | zstd -q -%d --long=%d -T1 -o %q & zstd -q -%d --long=%d -T1 -o %q %q; wait; }' \
      "$outdir" "$dir" "$largest" "$source" "$level" "$wlog" "$outdir/oradata-$stamp.tar.zst" \
      "$level" "$wlog" "$outdir/$big-$stamp.dbf.zst" "$largest")
    node_q sudo -n systemd-run --unit="$unit" --description="$desc" --collect \
      --property=Nice="$nice" --property=IOSchedulingClass=idle --property=RuntimeMaxSec="$runtime" \
      --property=MemoryHigh="${high_mib}M" --property=MemoryMax="${max_mib}M" \
      /bin/sh -c "$inner" >/dev/null
    for _ in {1..10}; do unit_active && break; sleep 1; done
    unit_active ;;
  # Stop the job, remove only what it wrote (preflight required the directory to be absent), then
  # wait for the banking workloads: pods the node evicted or the kernel killed come back on their own.
  cleanup)
    wait_node
    if unit_active; then node_q sudo -n systemctl stop "$unit.service"; fi
    for _ in {1..30}; do unit_active || break; sleep 1; done
    unit_idle
    if ! out_absent; then
      node_q sudo -n find "$outdir" -maxdepth 1 -type f -name '*.zst' -delete
      node_q sudo -n rmdir "$outdir"
    fi
    healthy "$cleanup_wait" ;;
  # Restored means: no job, no output, every banking workload ready, and the node has memory again.
  recovery) wait_node; unit_idle; out_absent; healthy 1s
    (( $(mem_available_kib) >= 1048576 )) ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
