#!/usr/bin/env bash
# Render representative injection placement without contacting any target.
set -euo pipefail

profile="${1:-}"

usage() {
  echo "usage: $0 <north-south|east-west|db-direct|container-resource|external-mock|nms-external|composite>" >&2
}

emit() {
  printf '%s=%s\n' "$1" "$2"
}

[[ -n "$profile" ]] || { usage; exit 2; }
emit profile "$profile"
emit side_effects false
emit orchestrator scenario-runner@109

case "$profile" in
  north-south)
    emit status ready
    emit injection_location tb-runner@192.168.122.206
    emit transport ssh
    emit entry_path 'tb-runner -> tb-cp NodePort -> gateway'
    emit representative commerce/scenario-01
    emit cleanup 'terminate scenario-tagged k6; preserve baseline unit'
    ;;
  east-west)
    emit status partial
    emit injection_location 'target namespace one-shot Job'
    emit transport kubectl
    emit entry_path 'Job -> service DNS'
    emit blocker 'reusable Job image/manifest and saturation strength not finalized'
    emit cleanup 'delete scenario Job and verify service recovery'
    ;;
  db-direct)
    emit status partial
    emit injection_location tb-runner@192.168.122.206
    emit transport ssh
    emit entry_path 'tb-runner -> DB NodePort -> tagged DB session'
    emit representative 'PG ready; MySQL/Oracle scripts pending'
    emit cleanup 'rollback or terminate only scenario-tagged session'
    ;;
  container-resource)
    emit status ready
    emit injection_location 'target Kubernetes Deployment/StatefulSet'
    emit transport kubectl
    emit entry_path 'resource patch -> rollout -> real service traffic'
    emit representative 'existing CPU limit/scale scripts'
    emit cleanup 'restore runtime snapshot, rollout, Ready and health check'
    ;;
  external-mock)
    emit status ready-control-plane
    emit injection_location 'commerce or food external-pg-mock'
    emit transport kubectl
    emit entry_path 'port-forward/control API -> actual payment outbound path'
    emit representative cross-domain/f15-t2
    emit blocker 'food order-impact scenarios remain blocked until courier pool recovery'
    emit cleanup 'reset and reinstall domain-specific default 200 expectation'
    ;;
  nms-external)
    emit status blocked
    emit injection_location external-server@192.168.200.57
    emit transport ssh
    emit entry_path '.57 -> actual physical interface -> tb-cp NodePort'
    emit blocker 'approval, interface mapping, collector registration and out-of-band recovery required'
    emit cleanup 'restore interface/qdisc/firewall and stop scenario exporter'
    ;;
  composite)
    emit status blocked-live
    emit injection_location 'one location per sub-injection'
    emit transport 'local orchestrator + kubectl/ssh/api sub-injections'
    emit entry_path 'offset schedule under one cleanup boundary'
    emit representative cross-domain/f15-t2
    emit blocker 'F15-T2 is dry-run only until food courier pool recovery'
    emit cleanup 'reverse-order cleanup; any failure blocks the next scenario'
    ;;
  *)
    usage
    exit 2
    ;;
esac
