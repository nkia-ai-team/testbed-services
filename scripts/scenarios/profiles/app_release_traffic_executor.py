#!/usr/bin/env python3
"""Reversible pricing fault-release via a namespace-local shadow service target."""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance

PROFILE_ID = "app.release"

CONTRACTS = {
    scenario_id: {
        "namespace": "rca-testbed-commerce",
        "service": "testbed-pricing",
        "baseline_selector": {"app": "testbed-pricing"},
        "fault_selector": {"app": f"scenario-pricing-fault-{scenario_id.lower()}"},
        "fault_deployment": f"scenario-pricing-fault-{scenario_id.lower()}",
        "fault_configmap": f"scenario-pricing-fault-{scenario_id.lower()}",
        "node_name": "tb-w3",
        "image": "nginx:alpine",
        "wrong_unit_price": "1.00",
    }
    for scenario_id in ("F08-R", "F14-H")
}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    del profile
    expected = CONTRACTS.get(scenario_id)
    if expected is None:
        raise ExecutorError("pricing traffic release is not allowlisted")
    if params != expected:
        raise ExecutorError("parameters must exactly match the measured pricing shadow contract")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    location = instance.get("location", {})
    if location.get("transport") != "kubectl" or location.get("namespace") != p["namespace"]:
        raise ExecutorError("pricing shadow release requires the commerce Kubernetes namespace")
    return kubectl_bash_argv([
        action, plan["scenario"]["id"], p["namespace"], p["service"],
        p["baseline_selector"]["app"], p["fault_selector"]["app"],
        p["fault_deployment"], p["fault_configmap"], p["node_name"],
        p["image"], p["wrong_unit_price"],
    ]), SCRIPT


SCRIPT = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; scenario_id="$2"; ns="$3"; service="$4"; baseline_app="$5"; fault_app="$6"; deployment="$7"; configmap="$8"; node="$9"; image="${10}"; wrong_price="${11}"
state_root="${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}"
state="$state_root/${scenario_id}-pricing-service-selector.json"
k=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$ns")
selector() { "${k[@]}" get svc "$service" -o json | jq -cS '.spec.selector'; }
baseline_json="$(jq -cn --arg app "$baseline_app" '{app:$app}')"
fault_json="$(jq -cn --arg app "$fault_app" '{app:$app}')"
resources_absent() { ! "${k[@]}" get deploy "$deployment" >/dev/null 2>&1 && ! "${k[@]}" get configmap "$configmap" >/dev/null 2>&1; }
original_ready() { "${k[@]}" rollout status deploy/testbed-pricing --timeout=180s >/dev/null; }
apply_fault_release() {
  "${k[@]}" apply -f - >/dev/null <<YAML
apiVersion: v1
kind: ConfigMap
metadata:
  name: $configmap
data:
  default.conf: |
    server {
      listen 8087;
      location = /actuator/health { default_type application/json; return 200 '{"status":"UP"}'; }
      location = /api/pricing/quote {
        default_type application/json;
        return 200 '{"items":[{"productId":1,"quantity":1,"unitPrice":$wrong_price,"subtotal":$wrong_price}],"subtotal":$wrong_price,"promotionDiscount":0,"couponDiscount":0,"total":$wrong_price,"appliedPromotion":null,"appliedCoupon":null}';
      }
      location / { default_type application/json; return 503 '{"error":"scenario pricing release only serves quote"}'; }
    }
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: $deployment
  labels: {app: $fault_app, rca-scenario: $scenario_id}
spec:
  replicas: 1
  selector: {matchLabels: {app: $fault_app}}
  template:
    metadata: {labels: {app: $fault_app, rca-scenario: $scenario_id}}
    spec:
      nodeName: $node
      containers:
      - name: pricing-fault
        image: $image
        imagePullPolicy: Never
        ports: [{containerPort: 8087}]
        readinessProbe: {httpGet: {path: /actuator/health, port: 8087}, periodSeconds: 2, timeoutSeconds: 1, failureThreshold: 5}
        resources:
          requests: {cpu: 25m, memory: 16Mi}
          limits: {cpu: 100m, memory: 64Mi}
        volumeMounts: [{name: config, mountPath: /etc/nginx/conf.d}]
      volumes: [{name: config, configMap: {name: $configmap}}]
YAML
  "${k[@]}" rollout status deploy/"$deployment" --timeout=120s >/dev/null
}
restore_selector() {
  original="$(cat "$state")"
  "${k[@]}" patch svc "$service" --type=merge -p "{\"spec\":{\"selector\":$original}}" >/dev/null
  [[ "$(selector)" == "$original" ]]
}
case "$action" in
  preflight)
    command -v kubectl >/dev/null; command -v jq >/dev/null
    "${k[@]}" auth can-i patch services | grep -qx yes
    "${k[@]}" auth can-i create deployments | grep -qx yes
    [[ "$(selector)" == "$baseline_json" && ! -e "$state" ]]; resources_absent; original_ready
    "${k[@]}" get node "$node" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' | grep -qx True
    ;;
  run)
    [[ "$(selector)" == "$baseline_json" && ! -e "$state" ]]; resources_absent
    mkdir -p "$state_root"; selector >"$state.tmp"; mv -T "$state.tmp" "$state"
    apply_fault_release
    "${k[@]}" patch svc "$service" --type=merge -p "{\"spec\":{\"selector\":$fault_json}}" >/dev/null
    [[ "$(selector)" == "$fault_json" ]]
    ;;
  cleanup)
    [[ -e "$state" ]] || exit 0
    restore_selector; original_ready
    "${k[@]}" delete deploy "$deployment" --ignore-not-found --wait=true >/dev/null
    "${k[@]}" delete configmap "$configmap" --ignore-not-found --wait=true >/dev/null
    resources_absent; rm -f "$state"
    ;;
  recovery)
    [[ ! -e "$state" && "$(selector)" == "$baseline_json" ]]; resources_absent; original_ready
    ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
