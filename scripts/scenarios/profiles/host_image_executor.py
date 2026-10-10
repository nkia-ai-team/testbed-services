#!/usr/bin/env python3
"""Node image retention executor (a cleanup rule that also matches the images in use).

F52-R reconstructs Logto 2023-12-17 (blog.logto.io/postmortem-docker-image-not-found):
an automated image retention workflow, meant to delete "untagged legacy images older
than 3 days", deleted the production images by mistake, because the tags of those
images sat on a manifest list while the images themselves carried no tag. The services
then "failed to fetch the image" and were unavailable until a new deployment brought a
fresh production image.

Here the image store the services start from is the worker's own containerd store: every
banking app image is imagePullPolicy Never and was loaded onto tb-w2 with `ctr import`,
so none of them carries a registry digest. The cleanup removes the banking app images
that carry no registry digest (the shape a superseded local build has), which on this
node is every banking app image in use. Running containers keep their snapshots and go on
serving. The failure appears at the next pod the node has to start: the routine restart
of transfer-service, whose strategy (maxSurge 0, maxUnavailable 1) removes the old pod
first. The new pod cannot start (ErrImageNeverPull) and transfer has no pod. This
executor refuses a Deployment without that strategy, because under the default strategy
the old pod keeps serving.

Every removal is one `sudo ctr` command on the node, so the node's auth log (forwarded to
119 as syslog) records each one, and containerd logs the ImageDelete events. run writes the
image ID of every image to its state file before removing anything. cleanup loads an image
back from 109 docker only when 109 docker still holds that very ID, and fails otherwise:
109's :latest is rebuilt by ordinary deploys, and loading a newer build would quietly change
the app version. Without the state file (a recreated runner container) the expected ID comes
from the containers that still run from the image on the node (crictl imageRef); an image no
running container uses is then not loaded at all, and cleanup fails for a person to finish.
recovery checks that every running app container on the node uses exactly the image its name
now points to. Nothing carries the scenario id: the state file is named after the node and
the namespace.
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance

PROFILE_ID = "host.image"

# 2026-10-10. F52-R: tb-w2 containerd 에서 레지스트리 다이제스트가 없는 banking 앱 이미지 넷(api, account,
# transfer, ledger 의 :latest)을 지운다. 2026-10-10 실측: 넷 모두 ctr import 로 적재되어 repoDigests 가 비어 있고
# (nginx, Kafka, Oracle 은 레지스트리에서 받아 다이제스트가 있음), 109 docker 의 같은 이름과 이미지 ID 가 같다.
# 그 뒤 transfer 를 일상 재배포하면 maxSurge 0 이라 옛 파드가 먼저 내려가고 새 파드는 ErrImageNeverPull 이다.
CONTRACTS = {
    "F52-R": {
        "host": "192.168.122.11",
        "node": "tb-w2",
        "namespace": "rca-testbed-banking",
        "images": [
            "core-banking-api:latest",
            "core-banking-account:latest",
            "core-banking-transfer:latest",
            "core-banking-ledger:latest",
        ],
        # The container that runs from each image, in the same order (recovery and the
        # state-less cleanup read the image ID a running container uses through it).
        "containers": ["api-service", "account-service", "transfer-service", "ledger-service"],
        "deployment": "testbed-transfer",
        "container": "transfer-service",
    },
}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    contract = CONTRACTS.get(scenario_id)
    if contract is None:
        raise ExecutorError("scenario has no verified node image retention contract")
    allowed = profile.get("parameter_contract", {}).get("allowed_scenarios")
    if allowed is not None and scenario_id not in allowed:
        raise ExecutorError("scenario is not enabled by the profile registry")
    if params != contract:
        raise ExecutorError("parameters must exactly match the approved node image retention contract")
    if any("," in image or ":" not in image for image in params["images"]):
        raise ExecutorError("images must be name:tag references")
    if len(params["containers"]) != len(params["images"]) or params["container"] not in params["containers"]:
        raise ExecutorError("every image needs the container that runs from it")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    validate(plan["scenario"]["id"], p, {})
    location = instance["location"]
    if location.get("transport") != "ssh" or location.get("host") != p["host"] or location.get("node") != p["node"]:
        raise ExecutorError("node image retention location must be the measured worker")
    return kubectl_bash_argv([
        action, p["host"], p["node"], p["namespace"], ",".join(p["images"]), ",".join(p["containers"]),
        p["deployment"], p["container"],
    ]), SCRIPT


SCRIPT = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; host="$2"; node="$3"; ns="$4"; images="$5"; containers="$6"; deploy="$7"; container="$8"
state_root="${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}"
# One line per image: "<name:tag> <sha256 image ID>", written before anything is removed.
state="$state_root/${node}-${ns}-images"
k=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$ns")
IFS=, read -r -a imgs <<<"$images"
IFS=, read -r -a ctrs <<<"$containers"
(( ${#imgs[@]} == ${#ctrs[@]} ))
node_sh() {
  ssh -i /root/.ssh/tb_key -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 "nkia@$host" "$@"
}
ref() { printf 'docker.io/library/%s\n' "$1"; }
# containerd's own list. A failed listing must stop the script under set -e instead of
# reading as "absent", so callers capture it with `list=$(listing)` on a line of its own.
listing() { node_sh sudo -n ctr -n k8s.io images ls -q; }
present() { grep -Fx "$(ref "$1")" <<<"$2" >/dev/null; }
# What CRI reports for the image: its ID (the docker config digest) and its registry digests.
inspecti() { node_sh sudo -n crictl inspecti -o json "$(ref "$1")"; }
node_id() { inspecti "$1" | jq -r '.status.id'; }
local_build() { inspecti "$1" | jq -e '(.status.repoDigests // []) | length == 0' >/dev/null; }
docker_id() { docker image inspect --format '{{.Id}}' "$1"; }
# The node copy and the 109 docker copy are the same image (cleanup restores from 109).
same_image() {
  local nid did; nid=$(node_id "$1"); did=$(docker_id "$1")
  [[ "$nid" == sha256:* && "$nid" == "$did" ]]
}
# "<container> <imageRef>" for every running container of the namespace on the node.
# imageRef is the image ID the container was created from, kept after the image is removed.
running_refs() {
  node_sh sudo -n crictl ps -o json | jq -r --arg ns "$ns" '.containers[]
    | select(.labels["io.kubernetes.pod.namespace"] == $ns and .state == "CONTAINER_RUNNING")
    | "\(.metadata.name) \(.imageRef)"'
}
container_of() { local i; for i in "${!imgs[@]}"; do [[ "${imgs[$i]}" == "$1" ]] && { echo "${ctrs[$i]}"; return 0; }; done; return 1; }
recorded_id() { awk -v i="$1" '$1 == i { print $2 }' "$state"; }
# The ID cleanup must restore: the recorded one, or, without the state file, the one the
# image's running container uses. Empty when neither is known.
expected_id() {
  if [[ -e "$state" ]]; then recorded_id "$1"; return 0; fi
  local c; c=$(container_of "$1")
  awk -v c="$c" '$1 == c && $2 ~ /^sha256:/ { print $2; exit }' <<<"$running"
}
spec() { "${k[@]}" get deploy "$deploy" -o json; }
image() { spec | jq -r --arg c "$container" '.spec.template.spec.containers[] | select(.name==$c) | .image'; }
never_pull() { [[ "$(spec | jq -r --arg c "$container" '.spec.template.spec.containers[] | select(.name==$c) | .imagePullPolicy')" == "Never" ]]; }
replace_first() { spec | jq -e '.spec.replicas == 1 and .spec.strategy.type == "RollingUpdate"
    and (.spec.strategy.rollingUpdate.maxSurge | tostring) == "0"' >/dev/null; }
on_node() { [[ "$(spec | jq -r '.spec.template.spec.nodeSelector["kubernetes.io/hostname"] // empty')" == "$node" ]]; }
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
# Load one image back from 109 docker, but only the build the node held: 109's :latest is
# rebuilt by ordinary deploys and a newer build would change the app version unnoticed.
# Called directly, not in $(...), so that set -e still applies.
restore_one() {
  local img=$1 exp did
  exp=$(expected_id "$img")
  if [[ "$exp" != sha256:* ]]; then
    echo "no known image ID for $img (no state file, no running container from it): not loading 109's" \
      "current build; load the build the node held by hand: docker save <that build> |" \
      "ssh -i /root/.ssh/tb_key nkia@$host 'sudo ctr -n k8s.io images import -'" >&2
    return 1
  fi
  did=$(docker_id "$img")
  if [[ "$did" != "$exp" ]]; then
    echo "109 docker $img is $did but the node held $exp: not loading a different build" >&2
    return 1
  fi
  docker save "$img" | node_sh sudo -n ctr -n k8s.io images import - >/dev/null
  [[ "$(node_id "$img")" == "$exp" ]]
}
check() {
  command -v kubectl >/dev/null; command -v jq >/dev/null; command -v docker >/dev/null; command -v ssh >/dev/null
  "${k[@]}" auth can-i patch deployments | grep -qx yes
  never_pull; replace_first; on_node
  # The restarted Deployment must start from one of the removed images.
  local img list current found=0
  current=$(image); list=$(listing)
  for img in "${imgs[@]}"; do if [[ "$current" == "$img" ]]; then found=1; fi; done
  (( found == 1 ))
  # Each image is on the node, carries no registry digest (the cleanup rule's selection),
  # and is the image 109 docker holds, so cleanup can load back exactly what was there.
  for img in "${imgs[@]}"; do present "$img" "$list"; local_build "$img"; same_image "$img"; done
  healthy 1s
}
running=""
case "$action" in
  preflight) check; [[ ! -e "$state" ]] ;;
  # The image IDs go to the state file first, so a run that fails halfway is still cleaned
  # up with the right builds. Each image goes by its name and by the sha256:<image ID>
  # reference CRI keeps beside it, and every one is gone before the routine restart starts.
  # run does not wait for the rollout (waiting while the coordinator lock is held expires the
  # runner lease).
  run) check; [[ ! -e "$state" ]]
    mkdir -p "$state_root"; : >"$state.tmp"
    for img in "${imgs[@]}"; do
      id=$(node_id "$img"); [[ "$id" == sha256:* ]]
      printf '%s %s\n' "$img" "$id" >>"$state.tmp"
    done
    mv -T "$state.tmp" "$state"
    list=$(listing)
    for img in "${imgs[@]}"; do
      id=$(recorded_id "$img"); [[ "$id" == sha256:* ]]
      refs=$(grep -Fx -e "$(ref "$img")" -e "$id" <<<"$list" || true)
      [[ -n "$refs" ]]
      # shellcheck disable=SC2086 # one argument per reference
      node_sh sudo -n ctr -n k8s.io images rm $refs >/dev/null
    done
    list=$(listing)
    for img in "${imgs[@]}"; do if present "$img" "$list"; then exit 1; fi; done
    "${k[@]}" rollout restart deploy "$deploy" >/dev/null ;;
  # Images are restored even without the state file: a runner container recreated while the
  # images were gone loses its state but the node still lacks them. A node that holds every
  # image and no state means there is nothing to undo. kubelet retries the pod that could not
  # start on its own; a redeploy follows when it does not come back within a minute.
  cleanup) list=$(listing); missing=()
    for img in "${imgs[@]}"; do if ! present "$img" "$list"; then missing+=("$img"); fi; done
    (( ${#missing[@]} > 0 )) || [[ -e "$state" ]] || exit 0
    [[ -e "$state" ]] || running=$(running_refs)
    for img in "${missing[@]}"; do restore_one "$img"; done
    list=$(listing)
    for img in "${imgs[@]}"; do
      present "$img" "$list"
      if [[ -e "$state" ]]; then [[ "$(node_id "$img")" == "$(recorded_id "$img")" ]]; fi
    done
    healthy 60s || { "${k[@]}" rollout restart deploy "$deploy" >/dev/null; healthy 180s; }
    rm -f "$state" ;;
  # Restored means: every image is back under its name, and every running app container on
  # the node uses exactly the image that name now points to (a newer build loaded in place
  # of the old one would differ from the containers that kept running).
  recovery) [[ ! -e "$state" ]]; healthy 1s; list=$(listing); running=$(running_refs)
    for img in "${imgs[@]}"; do
      present "$img" "$list"
      nid=$(node_id "$img"); rid=$(expected_id "$img")
      [[ "$nid" == sha256:* && "$nid" == "$rid" ]]
    done ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
