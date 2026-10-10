#!/usr/bin/env python3
"""Release image rollout executor (a Deployment rolled to an image the node does not hold).

F17-H reconstructs Harness 2025-10-28 (with Pipefy 2024-05-22): a routine deployment
needed a container image that was no longer in the registry the nodes pull from, so
the new pods never started and the endpoint behind them answered 502. Here the node's
own containerd store is that registry (every app image is imagePullPolicy Never and
loaded per node), and the change is one field of a Deployment's pod template: the
container image moves from the loaded tag to a release tag that was never loaded.
The Deployment's own strategy (maxSurge 0, maxUnavailable 1) removes the old pod
before the new one has to start; this executor refuses a Deployment without it,
because under the default strategy the old pod keeps serving (cut F05-G).

The scenario id never reaches kubectl: the remote script gets the namespace, the
Deployment, the container and the two image references, and its state file is keyed
by Deployment.

A second mode ("release") rolls a Deployment to a defective new release instead
(F41-R reconstructs Honeycomb 2019-11-06: a bad commit leaked memory at the same rate
on every backend until they crashed). The defective image is built from a patch kept
under scripts/scenarios/fault-images/ and lives only in 109 docker; this mode loads it
into the target node's containerd right before the rollout, and cleanup rolls back to
the manifest image and removes the release from the node again (its name and the
sha256:<image ID> reference CRI keeps beside it). App sources,
manifests and the manifest image are never touched. F17-H keeps its own script.
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance

PROFILE_ID = "k8s.image"

# 2026-10-09. F17-H: banking transfer-service 를 노드에 적재된 적 없는 릴리스 태그로 롤아웃한다.
# transfer 는 매니페스트 정본이 maxSurge 0 / maxUnavailable 1 이라 옛 파드가 먼저 내려가고,
# 새 파드는 ErrImageNeverPull 로 컨테이너를 띄우지 못한다.
CONTRACTS = {
    "F17-H": {
        "namespace": "rca-testbed-banking",
        "deployment": "testbed-transfer",
        "container": "transfer-service",
        "baseline_image": "core-banking-transfer:latest",
        "fault_image": "core-banking-transfer:2.1.0",
    },
    # 2026-10-09. F41-R: banking account-service 를 메모리 누수가 든 릴리스 1.3.0 으로 롤아웃한다.
    # 결함 이미지는 fault-images/f41-r(패치 + image.json)로 109 docker 에만 빌드해 두고, 실행기가
    # 주입 직전에 tb-w2 에 올리며 cleanup 이 매니페스트 이미지로 되돌린 뒤 노드에서 지운다.
    "F41-R": {
        "mode": "release",
        "namespace": "rca-testbed-banking",
        "deployment": "testbed-account",
        "container": "account-service",
        "baseline_image": "core-banking-account:latest",
        "fault_image": "core-banking-account:1.3.0",
    },
    # 2026-10-09. F42-R: banking transfer-service 를 일일 이체 한도 확인을 더한 릴리스 2.2.0 으로 롤아웃한다.
    # 새 질의가 이체마다 transfers 약 630만 행을 전수 스캔해 Oracle(2 CPU)이 포화된다. 결함 이미지는
    # fault-images/f42-r 로 109 docker 에만 빌드한다. F17-H 의 2.1.0(노드에 없어야 하는 태그)과 겹치지 않는다.
    "F42-R": {
        "mode": "release",
        "namespace": "rca-testbed-banking",
        "deployment": "testbed-transfer",
        "container": "transfer-service",
        "baseline_image": "core-banking-transfer:latest",
        "fault_image": "core-banking-transfer:2.2.0",
    },
    # 2026-10-09. F43-R: commerce cart-service 를 장바구니 품목 수 상한 확인을 더한 릴리스 1.2.0 으로 롤아웃한다.
    # 새 확인이 담기마다 풀에서 연결을 빌려 돌려주지 않아 Hikari 풀(20)이 몇 초 만에 마른다. 결함 이미지는
    # fault-images/f43-r 로 109 docker 에만 빌드하고, 실행기가 tb-w1 에 올렸다가 cleanup 이 지운다.
    "F43-R": {
        "mode": "release",
        "namespace": "rca-testbed-commerce",
        "deployment": "testbed-cart",
        "container": "cart-service",
        "baseline_image": "commerce-cart:latest",
        "fault_image": "commerce-cart:1.2.0",
    },
    # 2026-10-09. F47-R: banking api-service 를 이체 전 활성 계좌 확인을 더한 릴리스 1.4.0 으로 롤아웃한다.
    # 새 확인이 이체마다 account 의 계좌 목록 API 를 끝 페이지까지(약 49번) 뒤에서 다시 읽어 account 가
    # 요청에 묻힌다. 결함 이미지는 fault-images/f47-r 로 109 docker 에만 빌드하고, 실행기가 tb-w2 에 올렸다가
    # cleanup 이 지운다. 다른 시나리오의 릴리스 태그와 겹치지 않는다(api 는 이 태그 하나뿐).
    "F47-R": {
        "mode": "release",
        "namespace": "rca-testbed-banking",
        "deployment": "testbed-api",
        "container": "api-service",
        "baseline_image": "core-banking-api:latest",
        "fault_image": "core-banking-api:1.4.0",
    },
    # 2026-10-09. F49-R: food restaurant-service 를 가게 상세 응답의 status 를 문자열에서 {code, label} 객체로 바꾼
    # 릴리스 1.4.0 으로 롤아웃한다. restaurant 는 200 으로 답하고, 그 응답을 RestaurantResponse(status 문자열)로 읽는
    # order 가 해석에 실패해 주문이 502 다. 결함 이미지는 fault-images/f49-r 로 109 docker 에만 빌드하고, 실행기가
    # tb-w3 에 올렸다가 cleanup 이 지운다. 다른 시나리오의 릴리스 태그와 겹치지 않는다(restaurant 는 이 태그 하나뿐).
    "F49-R": {
        "mode": "release",
        "namespace": "rca-testbed-food",
        "deployment": "testbed-restaurant",
        "container": "restaurant-service",
        "baseline_image": "food-delivery-restaurant:latest",
        "fault_image": "food-delivery-restaurant:1.4.0",
    },
    # 2026-10-09. F33-H: food dispatch-service 를 배달 목록에 전체 건수(X-Total-Count)를 더한 릴리스 1.6.0 으로
    # 롤아웃한다. 목록 조회가 Slice 에서 Page 로 바뀌어 요청마다 끝난 배차 약 174만 행을 세는 COUNT 가 붙고,
    # food MySQL(CPU 0.5)이 포화되며 dispatch 풀이 마른다. 결함 이미지는 fault-images/f33-h 로 109 docker 에만
    # 빌드하고, 실행기가 tb-w3 에 올렸다가 cleanup 이 지운다. 다른 시나리오의 릴리스 태그와 겹치지 않는다
    # (dispatch 는 이 태그 하나뿐).
    "F33-H": {
        "mode": "release",
        "namespace": "rca-testbed-food",
        "deployment": "testbed-dispatch",
        "container": "dispatch-service",
        "baseline_image": "food-delivery-dispatch:latest",
        "fault_image": "food-delivery-dispatch:1.6.0",
    },
    # 2026-10-09. F49-H: food order-service 를 시각 표기를 앱 화면 표준(yyyy-MM-dd HH:mm:ss)으로 맞춘 릴리스 2.3.0 으로
    # 롤아웃한다. 같은 ObjectMapper 를 하류 호출에도 써서 dispatch 가 그대로 보내는 ISO 시각(assignedAt)을 읽지 못하고,
    # 배차는 dispatch 에 기록되는데 order 는 DispatchResponse 해석 실패로 주문이 503 이다. 결함 이미지는 fault-images/f49-h
    # 로 109 docker 에만 빌드하고, 실행기가 tb-w3 에 올렸다가 cleanup 이 지운다. 다른 시나리오의 릴리스 태그와 겹치지 않는다
    # (order 는 이 태그 하나뿐).
    "F49-H": {
        "mode": "release",
        "namespace": "rca-testbed-food",
        "deployment": "testbed-order",
        "container": "order-service",
        "baseline_image": "food-delivery-order:latest",
        "fault_image": "food-delivery-order:2.3.0",
    },
    # 2026-10-10. F43-P: food dispatch-service 를 같은 주문의 중복 배차 확인을 더한 릴리스 1.7.0 으로 롤아웃한다.
    # 새 확인이 배차 요청마다 풀에서 연결을 빌려 중복이 없을 때(거의 모든 요청) 돌려주지 않아 Hikari 풀(10)이 배차 약
    # 10건 만에 마르고, 용량 확인과 배차가 연결 대기 3초 끝에 500, order 가 주문을 503 으로 거절한다. 결함 이미지는
    # fault-images/f43-p 로 109 docker 에만 빌드하고, 실행기가 tb-w3 에 올렸다가 cleanup 이 지운다. dispatch 의 다른
    # 릴리스 태그(F33-H 1.6.0)와 겹치지 않는다.
    "F43-P": {
        "mode": "release",
        "namespace": "rca-testbed-food",
        "deployment": "testbed-dispatch",
        "container": "dispatch-service",
        "baseline_image": "food-delivery-dispatch:latest",
        "fault_image": "food-delivery-dispatch:1.7.0",
    },
}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    contract = CONTRACTS.get(scenario_id)
    if contract is None:
        raise ExecutorError("scenario has no verified release image contract")
    allowed = profile.get("parameter_contract", {}).get("allowed_scenarios")
    if allowed is not None and scenario_id not in allowed:
        raise ExecutorError("scenario is not enabled by the profile registry")
    if params != contract:
        raise ExecutorError("parameters must exactly match the approved release image contract")
    if params["baseline_image"] == params["fault_image"]:
        raise ExecutorError("fault image must differ from the baseline")
    if params["baseline_image"].split(":")[0] != params["fault_image"].split(":")[0]:
        raise ExecutorError("fault image must be another tag of the same repository")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    validate(plan["scenario"]["id"], p, {})
    location = instance["location"]
    if location.get("transport") != "kubectl" or location.get("namespace") != p["namespace"]:
        raise ExecutorError("release image executor requires its canonical Kubernetes namespace")
    return kubectl_bash_argv([
        action, p["namespace"], p["deployment"], p["container"], p["baseline_image"], p["fault_image"],
    ]), RELEASE_SCRIPT if p.get("mode") == "release" else SCRIPT


SCRIPT = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; ns="$2"; deploy="$3"; container="$4"; baseline="$5"; fault="$6"
state_root="${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}"
state="$state_root/${ns}-${deploy}-image"
k=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$ns")
spec() { "${k[@]}" get deploy "$deploy" -o json; }
image() { spec | jq -r --arg c "$container" '.spec.template.spec.containers[] | select(.name==$c) | .image'; }
# The failure needs the image to be absent from the node and the old pod to go first.
# Check both on the live objects instead of trusting the contract.
never_pull() { [[ "$(spec | jq -r --arg c "$container" '.spec.template.spec.containers[] | select(.name==$c) | .imagePullPolicy')" == "Never" ]]; }
replace_first() { spec | jq -e '.spec.replicas == 1 and .spec.strategy.type == "RollingUpdate"
    and (.spec.strategy.rollingUpdate.maxSurge | tostring) == "0"' >/dev/null; }
node() { spec | jq -r '.spec.template.spec.nodeSelector["kubernetes.io/hostname"] // empty'; }
on_node() {
  local n; n=$(node); [[ -n "$n" ]]
  kubectl --kubeconfig=/root/tb-kubeconfig get node "$n" -o json \
    | jq -e --arg i "$1" --arg d "docker.io/library/$1" '[.status.images[] | (.names // [])[]] | any(. == $i or . == $d)' >/dev/null
}
# While the fault holds, no container uses the baseline image, and with no registry behind
# imagePullPolicy Never the node copy is the only one. kubelet image GC (tb-w2: high 85%,
# low 80%, minimum age 2m) would reclaim it once the image filesystem passes 85%, and cleanup
# could not bring transfer back. Start only below the GC low mark, which leaves about 2 GB of
# writes before GC can begin (tb-w2 was 79.7% on 2026-10-09).
disk_ok() {
  local n; n=$(node); [[ -n "$n" ]]
  kubectl --kubeconfig=/root/tb-kubeconfig get --raw "/api/v1/nodes/$n/proxy/stats/summary" \
    | jq -e '.node.runtime.imageFs | (1 - .availableBytes / .capacityBytes) < 0.80' >/dev/null
}
set_image() { "${k[@]}" set image deploy "$deploy" "$container=$1" >/dev/null; }
# Status fields rather than `kubectl rollout status`, for the same reason as k8s.env
# (a stale ProgressDeadlineExceeded verdict survives a successful restore).
healthy() {
  local deadline=$((SECONDS + ${1%s}))
  while :; do
    if spec | jq -e '
        .status.observedGeneration >= .metadata.generation
        and ((.status.updatedReplicas // 0) == .spec.replicas)
        and ((.status.availableReplicas // 0) == .spec.replicas)
        and ((.status.replicas // 0) == .spec.replicas)' >/dev/null; then return 0; fi
    (( SECONDS < deadline )) || return 1
    sleep 2
  done
}
check() {
  command -v kubectl >/dev/null; command -v jq >/dev/null
  "${k[@]}" auth can-i patch deployments | grep -qx yes
  [[ "$(image)" == "$baseline" ]]; never_pull; replace_first
  # `! on_node` would not stop the script under set -e (a negated status never does).
  on_node "$baseline"; if on_node "$fault"; then return 1; fi
  disk_ok
  healthy 1s
}
case "$action" in
  preflight) check; [[ ! -e "$state" ]] ;;
  # Like k8s.env, run does not wait for the rollout it starts: waiting while the
  # coordinator lock is held expires the runner lease. This rollout never completes.
  run) check; mkdir -p "$state_root"; image >"$state.tmp"; mv -T "$state.tmp" "$state"; set_image "$fault" ;;
  cleanup) [[ -e "$state" ]] || exit 0; original=$(cat "$state"); [[ "$original" == "$baseline" ]]
    # If image GC removed the baseline anyway, rolling back cannot start transfer. Say how to
    # reload it (109 docker holds the same image) instead of timing out silently.
    if ! on_node "$original"; then
      echo "baseline image $original is gone from node $(node): reload it on 109 with" \
        "docker save $original | ssh -i /root/.ssh/tb_key nkia@<node ip> 'sudo ctr -n k8s.io images import -'" >&2
      exit 1
    fi
    set_image "$original"; healthy 180s; rm -f "$state" ;;
  recovery) [[ ! -e "$state" ]]; [[ "$(image)" == "$baseline" ]]; healthy 1s ;;
  *) exit 2 ;;
esac
'''


# Defective release (mode "release"). The node holds only manifest images (imagePullPolicy
# Never, no registry), so the release tag is copied from 109 docker into the node's
# containerd just before the rollout and removed again by cleanup. The Deployment keeps its
# own strategy: the new pod starts and passes its probes, then the release misbehaves.
RELEASE_SCRIPT = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; ns="$2"; deploy="$3"; container="$4"; baseline="$5"; fault="$6"
state_root="${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}"
state="$state_root/${ns}-${deploy}-image"
# The release's image ID (docker config digest), kept from run until cleanup has removed every
# node reference to it: CRI keeps its own "sha256:<ID>" reference beside the name, and removing
# only the name would leave the release's content on the node.
release_ref="$state_root/${ns}-${deploy}-release-id"
k=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$ns")
spec() { "${k[@]}" get deploy "$deploy" -o json; }
image() { spec | jq -r --arg c "$container" '.spec.template.spec.containers[] | select(.name==$c) | .image'; }
never_pull() { [[ "$(spec | jq -r --arg c "$container" '.spec.template.spec.containers[] | select(.name==$c) | .imagePullPolicy')" == "Never" ]]; }
node() { spec | jq -r '.spec.template.spec.nodeSelector["kubernetes.io/hostname"] // empty'; }
node_ip() {
  local n; n=$(node); [[ -n "$n" ]]
  kubectl --kubeconfig=/root/tb-kubeconfig get node "$n" -o json \
    | jq -r '.status.addresses[] | select(.type=="InternalIP") | .address' | head -n1
}
on_node_status() {
  local n; n=$(node); [[ -n "$n" ]]
  kubectl --kubeconfig=/root/tb-kubeconfig get node "$n" -o json \
    | jq -e --arg i "$1" --arg d "docker.io/library/$1" '[.status.images[] | (.names // [])[]] | any(. == $i or . == $d)' >/dev/null
}
node_sh() {
  ssh -i /root/.ssh/tb_key -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 "nkia@$(node_ip)" "$@"
}
# containerd's own list is current; node status.images lags the import by a status update.
# grep reads the whole list (no -q): an early exit would SIGPIPE ssh and fail the pipeline.
in_containerd() { node_sh sudo -n ctr -n k8s.io images ls -q | grep -Fx "docker.io/library/$1" >/dev/null; }
# The pulled-in release only adds its app layer (same eclipse-temurin base as the manifest
# image), but while it runs the manifest image is unused and kubelet image GC (high 85%,
# low 80%) could reclaim it. Start only below the GC low mark, as F17-H does.
disk_ok() {
  local n; n=$(node); [[ -n "$n" ]]
  kubectl --kubeconfig=/root/tb-kubeconfig get --raw "/api/v1/nodes/$n/proxy/stats/summary" \
    | jq -e '.node.runtime.imageFs | (1 - .availableBytes / .capacityBytes) < 0.80' >/dev/null
}
set_image() { "${k[@]}" set image deploy "$deploy" "$container=$1" >/dev/null; }
healthy() {
  local deadline=$((SECONDS + ${1%s}))
  while :; do
    if spec | jq -e '
        .status.observedGeneration >= .metadata.generation
        and ((.status.updatedReplicas // 0) == .spec.replicas)
        and ((.status.availableReplicas // 0) == .spec.replicas)
        and ((.status.replicas // 0) == .spec.replicas)' >/dev/null; then return 0; fi
    (( SECONDS < deadline )) || return 1
    sleep 2
  done
}
# No pod of the namespace may still run the release when its tag leaves the node.
release_unused() {
  local deadline=$((SECONDS + 90))
  while "${k[@]}" get pods -o json | jq -e --arg f "$fault" \
      '[.items[] | .spec.containers[].image] | any(. == $f)' >/dev/null; do
    (( SECONDS < deadline )) || return 1
    sleep 3
  done
}
release_id() { docker image inspect --format '{{.Id}}' "$fault" 2>/dev/null || true; }
# Every node reference to the release: its name and its ID reference ($1, may be empty).
# A failed listing must not read as "absent": capture it before filtering.
release_refs() {
  local list; list=$(node_sh sudo -n ctr -n k8s.io images ls -q) || return 1
  grep -Fx -e "docker.io/library/$fault" ${1:+-e "$1"} <<<"$list" || true
}
release_absent() { local refs; refs=$(release_refs "$1") || return 1; [[ -z "$refs" ]]; }
unload() {
  local id=$1 ref
  for ref in $(release_refs "$id"); do node_sh sudo -n ctr -n k8s.io images rm "$ref" >/dev/null; done
  release_absent "$id"
}
check() {
  command -v kubectl >/dev/null; command -v jq >/dev/null; command -v docker >/dev/null; command -v ssh >/dev/null
  "${k[@]}" auth can-i patch deployments | grep -qx yes
  [[ "$(image)" == "$baseline" ]]; never_pull
  [[ "$(spec | jq -r '.spec.replicas')" == "1" ]]
  docker image inspect "$fault" >/dev/null
  on_node_status "$baseline"; in_containerd "$baseline"
  local id; id=$(release_id); [[ "$id" == sha256:* ]]
  release_absent "$id"
  disk_ok
  healthy 1s
}
case "$action" in
  preflight) check; [[ ! -e "$state" ]] ;;
  # Loading takes seconds (only the app layer is new); the rollout itself is not awaited,
  # like k8s.env, so the coordinator lock is not held for the JVM start.
  run) check; mkdir -p "$state_root"; image >"$state.tmp"; mv -T "$state.tmp" "$state"
    release_id >"$release_ref.tmp"; mv -T "$release_ref.tmp" "$release_ref"
    docker save "$fault" | node_sh sudo -n ctr -n k8s.io images import - >/dev/null
    in_containerd "$fault"
    set_image "$fault" ;;
  cleanup) id=$(cat "$release_ref" 2>/dev/null || release_id)
    [[ -e "$state" ]] || { unload "$id"; rm -f "$release_ref"; exit 0; }
    original=$(cat "$state"); [[ "$original" == "$baseline" ]]
    if ! in_containerd "$original"; then
      echo "baseline image $original is gone from node $(node): reload it on 109 with" \
        "docker save $original | ssh -i /root/.ssh/tb_key nkia@$(node_ip) 'sudo ctr -n k8s.io images import -'" >&2
      exit 1
    fi
    set_image "$original"; healthy 180s; release_unused; unload "$id"; rm -f "$release_ref" "$state" ;;
  recovery) [[ ! -e "$state" ]]; [[ ! -e "$release_ref" ]]; [[ "$(image)" == "$baseline" ]]; healthy 1s
    release_absent "$(release_id)" ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
