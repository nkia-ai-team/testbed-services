#!/usr/bin/env python3
"""One-off batch Job executor (an internal backfill that calls another service's API at scale,
or a data export that reads a service's tables straight from its database).

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
object is the Job. F59-P is the banking statement backfill and F69-R the food payment audit
export (see their contracts below).

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


# F59-P is the same incident on core-banking: the ledger team's statement backfill, run from the
# ledger-service image, takes the account directory from account-service once and then, with
# many parallel workers, reads every account's transfer history page by page from
# transfer-service (GET /api/transfers?fromAccount=...&page=N&size=500). That list filters with
# optional parameters, so Oracle answers each page and its total count with a full scan of the
# transfers table, and every call holds one transfer Hikari connection while it runs. Nothing
# about transfer, account, api, Oracle or their configuration changes; the only new object is
# the Job.
# 2026-10-10. F59-P: 원장 쪽 계좌 명세서 백필 Job 이 계좌 목록(1,001개)을 한 번 받고 워커 48개로
# 계좌마다 이체 내역을 페이지 500 으로 끝까지 읽는다. 워커들이 계좌를 같은 순서로 돌며 계좌마다 쪽을
# 나눠(워커 w 는 w, w+48, ... 쪽) 읽으므로, 이체가 몰린 계좌 8개(settlement 401만, ACC-1001~1007 각
# 약 33만)에서도 작은 계좌에서도 동시 호출이 늘 48 이다(작은 계좌는 대부분 워커가 빈 쪽을 받는데,
# offset 이 0 이 아닌 빈 쪽은 페이지 질의와 건수 질의를 둘 다 전수 스캔한다). 109 실측(2026-10-10): 거래 내역 질의는 선택
# 조건(:x is null or ...) 꼴이라 transfers 약 638만 행 전수 스캔이고, 건수 질의(DPM byb2y2hv7p4ag)
# 호출당 약 0.23~0.25초, 페이지 질의도 끝까지 훑으면 0.22초 이상. transfer Hikari 풀은 15, 연결 대기
# 3초라 워커 48 개면 풀 15 를 늘 스캔이 쥐고 나머지가 대기열을 채운다.
CONTRACTS["F59-P"] = {
    "namespace": "rca-testbed-banking",
    "job": "ledger-statement-backfill",
    "image": "core-banking-ledger:latest",
    "image_owner": "testbed-ledger",
    "node": "tb-w2",
    "target_deployment": "testbed-transfer",
    "target_url": "http://testbed-transfer:8082",
    "directory_url": "http://testbed-account:8081",
    "page_size": 500,
    "workers": 48,
    "request_timeout_seconds": 60,
    "active_deadline_seconds": 1800,
}

# The statement backfill. The account directory is read once; every worker then walks the
# accounts in the same order and, for each account, reads its share of the history pages
# (worker w reads pages w, w+WORKERS, ...) until a short page, keeps the last page for the
# statement loader and appends one line per account. A failed page is retried after two
# seconds. Splitting each account's pages rather than the accounts keeps all workers busy on
# the few accounts that hold almost all transfers.
STATEMENT_SCRIPT = r'''set -u
trap 'kill 0' TERM INT
until code=$(curl -s -o /work/accounts.json -w '%{http_code}' --max-time "$REQUEST_TIMEOUT" "$ACCOUNT_URL/api/accounts") \
    && [ "$code" = 200 ]; do
  echo "account directory unavailable, retrying"
  sleep 2
done
mapfile -t accounts < <(grep -o '"id": *"[^"]*"' /work/accounts.json | cut -d'"' -f4)
echo "accounts: ${#accounts[@]}"
read_history() {
  local worker="$1" account="$2" page="$1" code rows
  while :; do
    code=$(curl -s -o "/work/page-$worker.json" -w '%{http_code}' --max-time "$REQUEST_TIMEOUT" \
      "$TRANSFER_URL/api/transfers?fromAccount=$account&page=$page&size=$PAGE_SIZE")
    if [ "$code" != 200 ]; then
      echo "worker $worker: $account page $page returned $code, retrying"
      sleep 2
      continue
    fi
    rows=$(grep -o '"transferRef"' "/work/page-$worker.json" | wc -l)
    [ "$rows" -lt "$PAGE_SIZE" ] && break
    page=$((page + WORKERS))
  done
  echo "$account $page" >>"/work/statements-$worker.txt"
}
run_worker() {
  local worker="$1" account
  for account in "${accounts[@]}"; do read_history "$worker" "$account"; done
  echo "worker $worker: done"
}
for worker in $(seq 0 $((WORKERS - 1))); do run_worker "$worker" & done
wait
'''


# F69-R reconstructs the KnowBe4 incident of 2025-10-13: a newly deployed data export workflow
# ran its extraction queries against shared transactional tables and took shared locks that
# concurrent updates could not pass, so logins and enrollment updates timed out until the
# workflow was removed. Here the finance team's payment audit export is a Kubernetes Job in the
# food namespace, run from the MySQL image already on the node (the client tools ship with it):
# it dumps the payment tables with mysqldump's defaults and compresses the dump with xz inside a
# small CPU budget. Without --single-transaction mysqldump takes LOCK TABLES ... READ LOCAL on
# every table it dumps and keeps the locks until the last row is written, so the dump runs at
# the pace of the compressor and every INSERT, UPDATE and DELETE on payments waits for a
# metadata lock the whole time, while reads go on. Nothing about payment, order, MySQL or their
# configuration changes; the only new object is the Job.
# 2026-10-12. F69-R: 결제 감사 자료 내보내기 Job 이 payments(약 173만 행, 덤프 약 190MB),
# settlement_summary 를 mysqldump 기본값(--lock-tables)으로 떠서 xz -6 으로 압축한다. 로컬 mysql:8.0
# 같은 크기 사본 실측(2026-10-12): 잠금 중 INSERT 는 'Waiting for table metadata lock', SELECT 는 통과,
# 덤프 세션은 'Sending to client'. x86(Xeon Silver 4514Y) CPU 0.5 에서 302초, 0.2 에서 829초(압축이
# 속도를 정한다). 한도 150m 이면 109(GB10 코어가 x86 보다 빨라도) 판정 시각(약 5분 30초)까지 덤프가 끝나지 않는다.
CONTRACTS["F69-R"] = {
    "namespace": "rca-testbed-food",
    "job": "payment-audit-export",
    "image": "mysql:8.0",
    "image_owner": "statefulset/testbed-mysql",
    "node": "tb-w3",
    "target_deployment": "testbed-payment",
    "db_host": "testbed-mysql",
    "db_secret": "mysql-secret",
    "database": "fooddelivery",
    "tables": "payments settlement_summary",
    "cpu_limit": "150m",
    "memory_limit": "256Mi",
    "active_deadline_seconds": 1800,
}

# The export. The account comes from the same Secret the food services use; the dump goes to
# the work directory for the archive upload.
EXPORT_SCRIPT = r'''set -euo pipefail
out="/work/$DB_NAME-$(date -u +%Y%m%dT%H%M%SZ).sql.xz"
echo "export $DB_NAME ($EXPORT_TABLES) to $out"
mysqldump --host="$DB_HOST" --user="$DB_USER" --no-tablespaces "$DB_NAME" $EXPORT_TABLES | xz -6 -T1 >"$out"
echo "export finished: $(stat -c %s "$out") bytes"
'''


def export_container(p: dict[str, Any]) -> dict[str, Any]:
    def secret(key: str) -> dict[str, Any]:
        return {"valueFrom": {"secretKeyRef": {"name": p["db_secret"], "key": key}}}

    return {
        "name": "export",
        "image": p["image"],
        "imagePullPolicy": "Never",
        "command": ["bash", "-c"],
        "args": [EXPORT_SCRIPT],
        "env": [
            {"name": "DB_HOST", "value": p["db_host"]},
            {"name": "DB_NAME", "value": p["database"]},
            {"name": "EXPORT_TABLES", "value": p["tables"]},
            {"name": "DB_USER", **secret("MYSQL_USER")},
            {"name": "MYSQL_PWD", **secret("MYSQL_PASSWORD")},
        ],
        "resources": {
            "requests": {"cpu": "50m", "memory": "64Mi"},
            "limits": {"cpu": p["cpu_limit"], "memory": p["memory_limit"]},
        },
        "volumeMounts": [{"name": "work", "mountPath": "/work"}],
    }


def api_container(p: dict[str, Any]) -> dict[str, Any]:
    if "directory_url" in p:
        script = STATEMENT_SCRIPT
        env = {
            "ACCOUNT_URL": p["directory_url"],
            "TRANSFER_URL": p["target_url"],
            "PAGE_SIZE": str(p["page_size"]),
            "WORKERS": str(p["workers"]),
            "REQUEST_TIMEOUT": str(p["request_timeout_seconds"]),
        }
    else:
        script = BACKFILL_SCRIPT
        env = {
            "DISPATCH_URL": p["target_url"],
            "DELIVERY_STATUS": p["status"],
            "PAGE_SIZE": str(p["page_size"]),
            "WORKERS": str(p["workers"]),
            "PAGES_PER_WORKER": str(p["pages_per_worker"]),
            "REQUEST_TIMEOUT": str(p["request_timeout_seconds"]),
        }
    return {
        "name": "backfill",
        "image": p["image"],
        "imagePullPolicy": "Never",
        "command": ["bash", "-c"],
        "args": [script],
        "env": [{"name": k, "value": v} for k, v in env.items()],
        "resources": {
            "requests": {"cpu": "50m", "memory": "64Mi"},
            "limits": {"cpu": "500m", "memory": "256Mi"},
        },
        "volumeMounts": [{"name": "work", "mountPath": "/work"}],
    }


def job_manifest(p: dict[str, Any]) -> dict[str, Any]:
    labels = {"app": p["job"]}
    container = export_container(p) if "tables" in p else api_container(p)
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
                    "containers": [container],
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
    if "tables" in params:
        if not params["tables"].split():
            raise ExecutorError("export job contract must name at least one table")
    elif params["workers"] * params.get("pages_per_worker", params["page_size"]) <= 0:
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
# The Job runs an image that is already on the node (imagePullPolicy Never): the workload
# that owns that image (a Deployment, or kind/name such as statefulset/testbed-mysql) must use
# exactly it and have a running pod on the same node.
image_on_node() {
  local ref="$owner"
  [[ "$ref" == */* ]] || ref="deploy/$ref"
  [[ "$("${k[@]}" get "$ref" -o jsonpath='{.spec.template.spec.containers[0].image}')" == "$image" ]]
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
