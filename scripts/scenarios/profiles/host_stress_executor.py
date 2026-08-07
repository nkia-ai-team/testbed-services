#!/usr/bin/env python3
"""Bounded host/PVC stress foundations with exact process and file recovery."""
from __future__ import annotations

import shlex
from typing import Any

from executor_common import ExecutorError, cli, profile_instance

PROFILE_ID = "host.stress"

CONTRACTS: dict[str, dict[str, Any]] = {
    "F10-R": {"mode": "watermark", "host": "192.168.122.184", "target_dir": "/opt/local-path-provisioner/pvc-5d71e22a-1225-4505-a7cc-5cf29dad4cf5_rca-testbed-commerce_pgdata-testbed-postgres-0", "watermark_percent": 85, "reserve_mib": 10240, "maximum_fill_mib": 51200},
    # F21-P·F21-Q (Tomcat worker saturation) — registered 2026-07-30. Both were in
    # registry/profiles.json but not here, so dispatch raised "scenario has no
    # verified bounded host-stress contract"; the quality charter's G2 flagged this
    # on 07-27 and it was still open. These are `fixed` profiles, one level each, so
    # they belong in CONTRACTS rather than in a calibration ladder.
    #
    # The envelope is not new: both ask for exactly F09R_LEVELS' middle rung
    # (cpu, 3 workers, 480s), pointed at a different worker. tb-w1/w2/w3 are
    # identical 4-core VMs on the same GB10 host, so 3-of-4 cores for 8 minutes is
    # already measured behaviour — F09-R has run this shape on tb-w1. CPU only:
    # memory stays free, which is what keeps these distinct from F05-P and F15-P
    # (F15-P shares tb-w2 but drives CPU *and* memory together).
    "F21-P": {"mode": "cpu", "host": "192.168.122.11", "cpu_workers": 3, "runtime_seconds": 480},
    "F21-Q": {"mode": "cpu", "host": "192.168.122.14", "cpu_workers": 3, "runtime_seconds": 480},
}

# 스토리지 IO 시나리오(F02-H·F10-H·F10-P)는 단일 계약이 아니라 캘리브레이션 사다리다.
#
# 2026-07-28 실측: tb-w3 /dev/vda1이 randwrite 4k direct에서 약 18,600 IOPS
# (min 13,516 / max 26,090, 72.9 MiB/s, util 85%). 세 워커는 같은 GB10 호스트의
# 동종 VM이라 능력이 유사하다고 보되, 무릎은 노드별로 라이브에서 확정한다.
#
# 이전 계약의 rate_iops 3000~4000은 그 능력의 16~21%에 불과했다. rate_iops는
# **상한**이므로 이 값으로는 DB를 굶길 수 없다 — F10-R에서 배운 것과 같은 함정
# ("안전한 구간에는 피해가 없다"). 사다리는 경합 없음(~32%)부터 사실상 무제한까지
# 걸쳐, 어느 지점에서 DB 지연이 사용자 피해로 번지는지를 런타임이 찾게 한다.
# 20000은 측정 능력(18.6k)을 넘으므로 실질적으로 무제한이다.
_STORAGE_LADDER_IOPS = (6000, 12000, 20000)

_STORAGE_TARGETS: dict[str, dict[str, Any]] = {
    # commerce PG — tb-w1 단독 장치(2026-07-28 배치 고정 후 Oracle과 분리됨)
    "F02-H": {"host": "192.168.122.184", "target_dir": "/opt/local-path-provisioner/pvc-5d71e22a-1225-4505-a7cc-5cf29dad4cf5_rca-testbed-commerce_pgdata-testbed-postgres-0"},
    # food MySQL — tb-w3 단독 장치
    "F10-H": {"host": "192.168.122.14", "target_dir": "/opt/local-path-provisioner/pvc-3439d85f-f921-4b19-8808-c679506a31dd_rca-testbed-food_mysqldata-testbed-mysql-0"},
    # banking Oracle — tb-w2 단독 장치(07-28 이사 + PVC 재생성)
    "F10-P": {"host": "192.168.122.11", "target_dir": "/opt/local-path-provisioner/pvc-2c369013-b180-417a-9eda-da922c78b6ee_rca-testbed-banking_oracledata-testbed-oracle-0"},
}

STORAGE_LEVELS: dict[str, list[dict[str, Any]]] = {
    sid: [
        {"mode": "fio", "host": t["host"], "target_dir": t["target_dir"],
         "size_mib": 2048, "runtime_seconds": 600, "rate_iops": iops}
        for iops in _STORAGE_LADDER_IOPS
    ]
    for sid, t in _STORAGE_TARGETS.items()
}

# F09-R (worker CPU noisy neighbor) is a calibration ladder, not a single contract.
# tb-w1 (192.168.122.184) has 4 cores, so intensity is CPU-only busy loops (yes) at
# 50/75/100% of the node — memory is untouched to stay clear of the eviction/OOM
# surface owned by F05-P.
#
# 2026-07-28: 대상이 tb-w3에서 tb-w1으로 바뀌었다. nodeSelector 도입 전에는 commerce
# 서비스가 세 워커에 흩어져 있었고 이 사다리는 우연히 commerce 다수가 앉아 있던 tb-w3를
# 때리고 있었다. 이제 commerce 전체가 tb-w1에 고정되므로 "노드 CPU 포화가 같은 노드
# 서비스들의 p95를 함께 올린다"는 이 시나리오의 정체성이 배치로 보장된다.
F09R_LEVELS = [
    {"mode": "cpu", "host": "192.168.122.184", "cpu_workers": 2, "runtime_seconds": 480},
    {"mode": "cpu", "host": "192.168.122.184", "cpu_workers": 3, "runtime_seconds": 480},
    {"mode": "cpu", "host": "192.168.122.184", "cpu_workers": 4, "runtime_seconds": 480},
]

# F05-P (worker node memory pressure) is a calibration ladder that drives tb-w1
# (192.168.122.184) toward the kubelet hard-eviction threshold (memory.available<100Mi)
# so several co-located commerce pods are evicted and their APIs fail during the
# reschedule gap. tb-w1 has 4 cores and ~11.9G total, so the burner is a stress-ng-free
# anonymous-memory hog (python touches every page) held for the window — CPU stays free
# to keep this distinct from F09-R's CPU surface.
#
# 2026-07-28: nodeSelector 도입으로 대상이 tb-w2에서 tb-w1으로 바뀌었다. 이전에는
# commerce cohort가 세 워커에 흩어져 있어 tb-w2를 때리는 것이 우연히 일부만 맞았고,
# 특히 `testbed-payment` 접두사는 tb-w2에 있던 **food**-payment에 매칭됐다(commerce
# payment는 다른 노드). 이제 commerce 전체가 tb-w1에 고정되어 cohort가 설계대로다.
#
# 사다리 MiB는 유지한다. 이전 대상(tb-w2)의 available ~7985 MiB 기준이었고 새 대상
# tb-w1은 commerce 집결 후 available ~6865 MiB(2026-07-28 실측)라, 5500은 압박만,
# 7000이 eviction 무릎을 넘고 8500은 확실히 넘는다 — 무릎을 더 좁게 감싼다.
# 무릎의 정확한 위치는 라이브 캘리브레이션으로만 확정하며 정적 측정으로 단정하지 않는다.
#
# `required_cohort`는 fail-closed 런타임 배치 게이트다. nodeSelector로 배치가 고정된
# 뒤에도 이 게이트는 남긴다 — 매니페스트가 되돌려지거나 노드가 빠지면 엉뚱한 노드를
# 때리는 대신 멈춰야 하기 때문이다. 게이트는 주입 전에 node-local crictl로 현재 파드를
# 다시 세어 예상 cohort가 실제로 공존하는지 확인하고, 아니면 한 바이트도 할당하지 않는다.
_F05P_COHORT = [
    "testbed-gateway", "testbed-cart", "testbed-inventory",
    "testbed-redis", "testbed-kafka", "testbed-payment",
]
#
# 2026-08-07(run 3ce101d5): 5500과 7000 사이가 비어 있었다. 5500은 node_mem 47~56%로
# 무효과라 4분 뒤 escalate했고, 다음 단 7000은 67초 만에 노드를 NotReady로 보내
# abort로 끝냈다 — 성공 구간(node_mem >= 92%이면서 노드는 살아 있는 상태)을 사다리가
# 통째로 건너뛴 것이다. 그 사이에 6250을 넣어 무릎을 더 좁게 감싼다.
F05P_LEVELS = [
    {"mode": "memhog", "host": "192.168.122.184", "mib": 5500, "runtime_seconds": 480, "required_cohort": _F05P_COHORT},
    {"mode": "memhog", "host": "192.168.122.184", "mib": 6250, "runtime_seconds": 480, "required_cohort": _F05P_COHORT},
    {"mode": "memhog", "host": "192.168.122.184", "mib": 7000, "runtime_seconds": 480, "required_cohort": _F05P_COHORT},
    {"mode": "memhog", "host": "192.168.122.184", "mib": 8500, "runtime_seconds": 480, "required_cohort": _F05P_COHORT},
]

# F15-P — 2026-07-28 재설계.
#
# 원 설계는 "공용 노드 압박"이었다. 세 도메인이 한 워커에 뒤섞여 있으니 노드를 누르면
# 여러 도메인이 동시에 아프다는 것이 정체성이었는데, nodeSelector로 도메인을 갈라놓은
# 순간 그 전제가 **소멸**했다. 이제 어떤 워커도 한 도메인만 담는다.
#
# 새 정체성은 **복합 자원 고갈**이다. F09-R은 CPU만, F05-P는 메모리만 건드리도록
# 일부러 격리돼 있다(각 주석 참조). F15-P는 둘을 동시에 밀어 "자원 하나로는 설명되지
# 않는" 서명을 만든다 — 이것이 감별선이다:
#   - F09-R 배제: 메모리 압박 신호가 함께 있다(F09-R은 메모리를 안 건드린다)
#   - F05-P 배제: CPU 포화가 함께 있다(F05-P는 CPU를 비워 둔다)
#   - 서비스 단위 결함 배제: 같은 노드 서비스가 **전부** 함께 나빠진다
#
# 대상은 tb-w2(banking)다. commerce 노드(tb-w1)에는 이미 F09-R·F05-P가 있어 쌓이는
# 것을 피했고, 무엇보다 banking은 **Oracle이 같은 노드에 있어** 앱과 DB가 함께
# 무너지는 인과 그림이 나온다 — 노드가 근본임을 가리키는 더 강한 증거다.
#
# 메모리는 eviction 무릎 아래로 묶는다. tb-w2 available ~7103 MiB(2026-07-28 실측)에서
# 최대 5000 MiB만 잡아 파드 축출은 일으키지 않는다 — 축출은 F05-P의 표면이고,
# F15-P는 "축출 없이 전반적으로 느려지는" 상태를 노린다.
#
# 2026-08-07(run 8f317c57): 두 가지가 함께 고쳐졌다.
#
# ① 메모리 축이 아예 주입되지 않고 있었다 — stress-ng에 `--vm-keep`이 빠져 있었다.
#    이 플래그가 없으면 vm stressor가 매 반복 munmap/mmap을 되풀이해 페이지가 상주하지
#    않는다.
#
#    ⚠️ 2026-08-07 정정: 아래 "node_mem_util이 안 올랐다"는 **증거는 무효다.**
#    러너가 읽던 `kcm.node.mem_utilization`이 파드/cgroup 회계라, ssh로 띄운 파드 밖
#    프로세스(stress-ng·memhog)를 아예 보지 못했다. F05-P run 1f444bc5에서 같은 노드·
#    같은 창의 두 계열이 93.74% vs 46.85%로 갈라지는 것이 확인됐고, 러너는 f1f0f81로
#    `system_mem_utilization`으로 교체했다. 즉 8f317c57에서 메모리가 실제로는 올라가
#    있었는데 장님 값을 보고 있었을 수 있다.
#
#    `--vm-keep`이 필요하다는 것 자체는 stress-ng 문서·`--help` 실측("redirty memory
#    instead of reallocating")으로 독립적으로 서므로 이 수리는 유지한다. 다만 아래
#    실측 수치를 근거로 인용하지 말 것 — 메트릭 수리 후 재실행으로 다시 세워야 한다.
#    같은 이유로 must_rule_out `cpu-only-pressure(mem<60)` 발화도 오발화였을 수 있다.
#
#    (원 기록) 8f317c57: node_cpu_util이 99.36%까지 올라가는 동안
#    node_mem_util은 settling 값 61.62%를 한 번도 넘지 못하고 오히려 57.83~57.91%로
#    **내려갔다**. 이 한 플래그가 F15-P의 세 게이트를 동시에 깨고 있었다 — success는
#    mem >= 75가 필요한데 도달 불가, escalate는 mem < 75로 영구 발화해 사다리가 CPU
#    축만으로 올라가고, must_rule_out `cpu-only-pressure`(mem < 60)가 elapsed 370s에
#    confirm되어 run을 abort시켰다. 복합 자원 고갈이라는 정체성 중 CPU 절반만 주입되고
#    있었던 것이다. `--vm-keep`("redirty memory instead of reallocating", tb-w2의
#    stress-ng 0.17.06 --help 실측)이 vm-bytes 사다리를 비로소 '상주 RSS'로 만든다 —
#    F05-P의 memhog가 touch-and-hold bytearray로 이미 의도적으로 하는 일과 같다.
#    사다리 수치는 그대로 둔다: 이 사다리는 메모리가 실제로 상주한다는 전제로
#    설계됐고(tb-w2 available ~7103 MiB 대비 최대 5000 MiB), 지금까지 그 전제가
#    코드에서 깨져 있었을 뿐이다. 무릎의 위치는 캘리브레이션이 라이브에서 찾는다.
#
# ② 시나리오 전용 부하가 없었다 — host.stress 계열 5종(F05-P·F09-R·F10-P·F10-H·F15-P)
#    중 F15-P만 companion_refs가 비어 있었다. 동종은 전부 load.north_south를 달고
#    achieved_rps 관측 + `user-load-overshot` 감별자를 함께 갖는다. F15-P에도 같은
#    3종을 붙였다. 파라미터는 F10-P를 그대로 따른다 — 같은 노드(tb-w2)·같은 도메인
#    (banking, entry 30082 / core-banking/surge.js / loadgen-banking)이고 역할도 같다
#    (노드 압박을 드러내는 배경 부하이지 원인이 아니다). target_rps 20은 F09-R·F10-P와
#    동일하고, overshoot 감별자 임계 60도 F09-R과 동일한 3배 여유다.
#
# 피해 임계(transfer_p95 >= 4000 / account_p95 >= 1000)는 **의도적으로 건드리지 않았다**.
# 8f317c57에서 account_p95가 3~33ms에 머문 것은 사실이지만, 그 run은 압박의 절반
# (CPU)만 주입된 상태였으므로 "account는 압박을 안 받는다"는 결론의 근거가 되지 못한다.
# account 컨테이너의 cpu_throttled_time이 0이었다는 관측도 배제 근거가 아니다 — 이
# 시나리오는 정답지(scenario-metadata.json root_cause.mechanism)가 명시하듯 "cgroup
# 한도를 넘지 않으므로 컨테이너 지표는 정상"인 상태를 노린다. 즉 throttle 0은 설계된
# 결과이지 피해 부재의 증거가 아니다. 또한 must_support가 "앱(transfer·account)이 함께
# 지연 — 한 서비스가 아니다"를 감별선으로 삼으므로 account를 판정에서 빼면 시나리오
# 정체성이 무너진다. 임계 재조정은 ①②가 적용된 재실행 실측을 보고 판단한다.
F15P_LEVELS = [
    {"mode": "pressure", "host": "192.168.122.11", "cpu_workers": 2, "vm_workers": 1, "vm_bytes": "1500M", "runtime_seconds": 600},
    {"mode": "pressure", "host": "192.168.122.11", "cpu_workers": 3, "vm_workers": 1, "vm_bytes": "3000M", "runtime_seconds": 600},
    {"mode": "pressure", "host": "192.168.122.11", "cpu_workers": 4, "vm_workers": 2, "vm_bytes": "2500M", "runtime_seconds": 600},
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
    if scenario_id == "F15-P":
        if params not in F15P_LEVELS:
            raise ExecutorError("parameters do not match a measured F15-P compound-pressure level")
        return
    if scenario_id in STORAGE_LEVELS:
        if params not in STORAGE_LEVELS[scenario_id]:
            raise ExecutorError("parameters do not match a measured storage-IO ladder level")
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
    # ssh joins argv with spaces into one remote command line, so remote arguments
    # must be shell-quoted (db_ddl_executor and load_north_south_executor do the
    # same). Nothing here carries a metacharacter today — these are paths, numbers
    # and cohort names — but that is a property of the current parameters, not of
    # the transport, and load.north_south lost five scenarios to exactly that
    # assumption when one health_path grew an `&`.
    return ["/usr/bin/ssh", "-i", "/root/.ssh/tb_key", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10", f"nkia@{p['host']}", "sudo", "bash", "-s", "--", *(shlex.quote(arg) for arg in args)], REMOTE


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
  # --vm-keep is load-bearing, not a tuning knob: without it the vm stressor munmaps
  # and re-mmaps every iteration, so pages are never held resident and node memory
  # utilisation does not move at all. See the F15P_LEVELS note for run 8f317c57.
  case "$action" in preflight) command -v stress-ng >/dev/null; ! alive;; run) ! alive; nohup stress-ng --cpu "$cpu" --vm "$vm" --vm-bytes "$bytes" --vm-keep --timeout "${runtime}s" --metrics-brief >"$state_root/${scenario}.log" 2>&1 </dev/null & echo $! >"$pidfile";; cleanup) stop; rm -f "$state_root/${scenario}.log";; recovery) ! alive;; *) exit 2;; esac ;;
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
