#!/usr/bin/env python3
"""One-off batch Job executor (an internal backfill that calls another service's API at scale).

F59-R reconstructs the Atlassian Bitbucket incident of 2025-05-08: an internal high-scale
backfill job from an internal service sent an excessive volume of expensive queries through
internal API endpoints, the primary database came under pressure, the website and API saw
latency and intermittent failures, and retries from downstream services kept pressing on the
database after the job was stopped. Here the job is a Kubernetes Job in the food namespace,
run from the notify-service image as a notification-archive backfill: parallel workers walk the
dispatch-service delivery list (GET /api/deliveries?status=DELIVERED) page by page. The list
has no sort key, so a page deep in the history makes MySQL step over every earlier finished
delivery (LIMIT offset, size), and each such call holds one dispatch Hikari connection for
seconds. Nothing about dispatch, order, MySQL or their configuration changes; the only new
object is the Job.

The scenario id never reaches kubectl: the remote script gets the namespace, the Job name and
the Job manifest built from the contract, and its state file is keyed by namespace and Job.
"""
from __future__ import annotations

import json
from typing import Any

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance

PROFILE_ID = "k8s.job"

# 2026-10-10. F59-R: 알림 보관소 백필 Job 이 dispatch 목록 API 를 병렬 12개로 페이지마다 읽는다.
# 끝난 배차(DELIVERED) 약 173만 행 / 페이지 500 = 약 3,470 페이지를 12 × 290 으로 나눠, 워커마다
# 자기 구간의 앞에서부터 읽는다. 109 실측(2026-10-10): 같은 목록 질의가 offset 20만에서 0.53초,
# offset 100만에서 6.3초(버퍼 풀 128MB 를 넘는 읽기). dispatch Hikari 풀은 10 이라 워커 12 개면
# 풀이 늘 이 질의에 묶인다.
CONTRACTS = {
    "F59-R": {
        "namespace": "rca-testbed-food",
        "job": "notify-delivery-backfill",
        "image": "food-delivery-notify:latest",
        "image_owner": "testbed-notify",
        "node": "tb-w3",
        "target_deployment": "testbed-dispatch",
        "target_url": "http://testbed-dispatch:8082",
        "status": "DELIVERED",
        "page_size": 500,
        "workers": 12,
        "pages_per_worker": 290,
        "request_timeout_seconds": 60,
        "active_deadline_seconds": 1800,
    },
}

# The backfill itself. Each worker owns a contiguous page range, saves each page to the
# work directory for the archive loader and retries a failed page after two seconds.
BACKFILL_SCRIPT = r'''set -u
trap 'kill 0' TERM INT
fetch_range() {
  local worker="$1" page=$(( $1 * PAGES_PER_WORKER )) last=$(( ($1 + 1) * PAGES_PER_WORKER )) code
  while [ "$page" -lt "$last" ]; do
    code=$(curl -s -o "/work/deliveries-$page.json" -w '%{http_code}' --max-time "$REQUEST_TIMEOUT" \
      "$DISPATCH_URL/api/deliveries?status=$DELIVERY_STATUS&page=$page&size=$PAGE_SIZE")
    if [ "$code" = 200 ]; then
      page=$((page + 1))
      [ $((page % 10)) -eq 0 ] && echo "worker $worker: page $page of $last"
    else
      echo "worker $worker: page $page returned $code, retrying"
      sleep 2
    fi
  done
}
for worker in $(seq 0 $((WORKERS - 1))); do fetch_range "$worker" & done
wait
'''


def job_manifest(p: dict[str, Any]) -> dict[str, Any]:
    labels = {"app": p["job"]}
    env = {
        "DISPATCH_URL": p["target_url"],
        "DELIVERY_STATUS": p["status"],
        "PAGE_SIZE": str(p["page_size"]),
        "WORKERS": str(p["workers"]),
        "PAGES_PER_WORKER": str(p["pages_per_worker"]),
        "REQUEST_TIMEOUT": str(p["request_timeout_seconds"]),
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": p["job"], "namespace": p["namespace"], "labels": labels},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": p["active_deadline_seconds"],
            "ttlSecondsAfterFinished": 600,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "restartPolicy": "Never",
                    "terminationGracePeriodSeconds": 5,
                    "nodeSelector": {"kubernetes.io/hostname": p["node"]},
                    "containers": [{
                        "name": "backfill",
                        "image": p["image"],
                        "imagePullPolicy": "Never",
                        "command": ["bash", "-c"],
                        "args": [BACKFILL_SCRIPT],
                        "env": [{"name": k, "value": v} for k, v in env.items()],
                        "resources": {
                            "requests": {"cpu": "50m", "memory": "64Mi"},
                            "limits": {"cpu": "500m", "memory": "256Mi"},
                        },
                        "volumeMounts": [{"name": "work", "mountPath": "/work"}],
                    }],
                    "volumes": [{"name": "work", "emptyDir": {"sizeLimit": "512Mi"}}],
                },
            },
        },
    }


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    contract = CONTRACTS.get(scenario_id)
    if contract is None:
        raise ExecutorError("scenario has no verified batch job contract")
    allowed = profile.get("parameter_contract", {}).get("allowed_scenarios")
    if allowed is not None and scenario_id not in allowed:
        raise ExecutorError("scenario is not enabled by the profile registry")
    if params != contract:
        raise ExecutorError("parameters must exactly match the approved batch job contract")
    if params["workers"] * params["pages_per_worker"] <= 0:
        raise ExecutorError("batch job contract must read at least one page")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    validate(plan["scenario"]["id"], p, {})
    location = instance["location"]
    if location.get("transport") != "kubectl" or location.get("namespace") != p["namespace"]:
        raise ExecutorError("batch job executor requires its canonical Kubernetes namespace")
    manifest = json.dumps(job_manifest(p), separators=(",", ":"), sort_keys=True)
    return kubectl_bash_argv([
        action, p["namespace"], p["job"], p["target_deployment"], p["image_owner"],
        p["image"], p["node"], manifest,
    ]), SCRIPT


SCRIPT = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; ns="$2"; job="$3"; target="$4"; owner="$5"; image="$6"; node="$7"; manifest="$8"
state_root="${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}"
state="$state_root/${ns}-${job}-job"
k=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$ns")
job_absent() {
  ! "${k[@]}" get job "$job" >/dev/null 2>&1 && [[ -z "$("${k[@]}" get pods -l job-name="$job" -o name)" ]]
}
# Status fields rather than `kubectl rollout status`, for the same reason as k8s.scale.
available() {
  local deadline=$((SECONDS + ${1%s}))
  while :; do
    if "${k[@]}" get deploy "$target" -o json | jq -e '
        .status.observedGeneration >= .metadata.generation
        and ((.status.updatedReplicas // 0) == .spec.replicas)
        and ((.status.availableReplicas // 0) == .spec.replicas)' >/dev/null; then return 0; fi
    (( SECONDS < deadline )) || return 1
    sleep 2
  done
}
# The Job runs an image that is already on the node (imagePullPolicy Never): the Deployment
# that owns that image must use exactly it and have a running pod on the same node.
image_on_node() {
  [[ "$("${k[@]}" get deploy "$owner" -o jsonpath='{.spec.template.spec.containers[0].image}')" == "$image" ]]
  "${k[@]}" get pods -o json | jq -e --arg node "$node" --arg image "$image" '
      [.items[] | select(.spec.nodeName == $node and .status.phase == "Running")
                | select(any(.spec.containers[]; .image == $image))] | length > 0' >/dev/null
}
check() {
  command -v kubectl >/dev/null; command -v jq >/dev/null
  "${k[@]}" auth can-i create jobs.batch | grep -qx yes
  "${k[@]}" auth can-i delete jobs.batch | grep -qx yes
  kubectl --kubeconfig=/root/tb-kubeconfig get node "$node" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' | grep -qx True
  image_on_node; available 1s
}
case "$action" in
  preflight) check; [[ ! -e "$state" ]]; job_absent ;;
  # run records the Job name and does not wait for the pod: waiting while the coordinator
  # lock is held expires the runner lease.
  run)
    check; job_absent; mkdir -p "$state_root"; printf '%s\n' "$job" >"$state.tmp"; mv -T "$state.tmp" "$state"
    printf '%s' "$manifest" | "${k[@]}" apply -f - >/dev/null ;;
  cleanup)
    [[ -e "$state" ]] || exit 0
    [[ "$(cat "$state")" == "$job" ]]
    "${k[@]}" delete job "$job" --ignore-not-found --cascade=foreground --wait=true --timeout=180s >/dev/null
    deadline=$((SECONDS + 180))
    until job_absent; do (( SECONDS < deadline )) || exit 1; sleep 2; done
    available 300s
    rm -f "$state" ;;
  recovery) [[ ! -e "$state" ]]; job_absent; available 1s ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
