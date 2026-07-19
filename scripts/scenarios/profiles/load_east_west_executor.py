#!/usr/bin/env python3
"""Bounded namespace-local k6 Job executor for pricing bulkhead saturation."""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance

PROFILE_ID = "load.east_west"

LEVELS = {
    rps: {
        "namespace": "rca-testbed-commerce",
        "job": "scenario-f07-p-pricing-bulkhead",
        "configmap": "scenario-f07-p-pricing-bulkhead",
        "node_name": "tb-w1",
        "image": "grafana/k6:latest",
        "target_url": "http://testbed-pricing:8087/api/pricing/quote",
        "target_rps": rps,
        "duration_seconds": 480,
        "preallocated_vus": 32,
        "max_vus": 64,
    }
    for rps in (20, 35, 50)
}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    del profile
    if scenario_id != "F07-P":
        if scenario_id == "F03-H":
            raise ExecutorError("F03-H blocked: no slow read-only order endpoint isolates servlet threads from DB")
        raise ExecutorError("east-west load scenario is not allowlisted")
    if params not in LEVELS.values():
        raise ExecutorError("parameters must exactly match a measured F07-P bulkhead level")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    location = instance.get("location", {})
    if location.get("transport") != "kubectl" or location.get("namespace") != p["namespace"]:
        raise ExecutorError("east-west executor requires the commerce namespace")
    return kubectl_bash_argv([
        action, plan["scenario"]["id"], p["namespace"], p["job"], p["configmap"],
        p["node_name"], p["image"], p["target_url"], str(p["target_rps"]),
        str(p["duration_seconds"]), str(p["preallocated_vus"]), str(p["max_vus"]),
    ]), SCRIPT


SCRIPT = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; scenario_id="$2"; ns="$3"; job="$4"; configmap="$5"; node="$6"; image="$7"; target="$8"; rps="$9"; duration="${10}"; pre_vus="${11}"; max_vus="${12}"
state_root="${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}"
state="$state_root/${scenario_id}-east-west-job"
k=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$ns")
absent() { ! "${k[@]}" get job "$job" >/dev/null 2>&1 && ! "${k[@]}" get configmap "$configmap" >/dev/null 2>&1; }
pricing_ready() { "${k[@]}" rollout status deploy/testbed-pricing --timeout=180s >/dev/null; "${k[@]}" get endpoints testbed-pricing -o jsonpath='{.subsets[0].addresses[0].ip}' | grep -q .; }
case "$action" in
  preflight)
    command -v kubectl >/dev/null
    "${k[@]}" auth can-i create jobs.batch | grep -qx yes
    "${k[@]}" auth can-i delete jobs.batch | grep -qx yes
    [[ ! -e "$state" ]]; absent; pricing_ready
    "${k[@]}" get node "$node" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' | grep -qx True
    ;;
  run)
    [[ ! -e "$state" ]]; absent; pricing_ready; mkdir -p "$state_root"
    "${k[@]}" apply -f - >/dev/null <<YAML
apiVersion: v1
kind: ConfigMap
metadata: {name: $configmap}
data:
  scenario.js: |
    import http from 'k6/http';
    import { Counter } from 'k6/metrics';
    const rejected = new Counter('pricing_bulkhead_rejected');
    export const options = { scenarios: { quote: { executor: 'constant-arrival-rate', rate: $rps, timeUnit: '1s', duration: '${duration}s', preAllocatedVUs: $pre_vus, maxVUs: $max_vus } }, thresholds: {} };
    const body = JSON.stringify({items: Array.from({length:16}, (_, i) => ({productId:i+1, quantity:1})), couponCode:null});
    export default function () { const response = http.post('$target', body, {headers:{'Content-Type':'application/json'}, tags:{scenario_id:'F07-P', step:'pricing-quote'}}); if (response.status >= 429) rejected.add(1); }
---
apiVersion: batch/v1
kind: Job
metadata: {name: $job, labels: {rca-scenario: $scenario_id}}
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 3600
  template:
    metadata: {labels: {rca-scenario: $scenario_id}}
    spec:
      restartPolicy: Never
      nodeName: $node
      containers:
      - name: k6
        image: $image
        imagePullPolicy: Never
        args: ['run', '--out', 'json=/tmp/f07-p.json', '/scripts/scenario.js']
        resources:
          requests: {cpu: 100m, memory: 64Mi}
          limits: {cpu: '1', memory: 256Mi}
        volumeMounts: [{name: script, mountPath: /scripts}]
      volumes: [{name: script, configMap: {name: $configmap}}]
YAML
    printf '%s\n' "$job" >"$state"
    ;;
  cleanup)
    [[ -e "$state" ]] || exit 0
    [[ "$(cat "$state")" == "$job" ]]
    "${k[@]}" delete job "$job" --ignore-not-found --wait=true >/dev/null
    "${k[@]}" delete configmap "$configmap" --ignore-not-found --wait=true >/dev/null
    absent; rm -f "$state"
    ;;
  recovery)
    [[ ! -e "$state" ]]; absent; pricing_ready
    ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
