#!/usr/bin/env python3
"""Database lock executor.

주입 세션은 **실제 애플리케이션 세션과 구별되지 않아야 한다**(품질 기준서 G6/L2,
`docs/spec-scenario-quality-charter.md` 부록 A). 2026-07-27 캡처 실측에서 기존 방식은
한 레코드에 세 개의 유일값을 남겼다.

    program: rca-F01-R-inventory-lock   (14,167행 중 유일)
    waitEvent: PgSleep                  (유일 — 실운영에 존재하지 않는 대기)
    ip: 10.244.0.0                      (클러스터 밖 tb-runner의 NAT 주소)

세 축을 모두 자연 분포 안으로 넣는다.

    program → "PostgreSQL JDBC Driver"  (실 앱 세션 319행과 동일)
    대기     → idle in transaction / ClientRead (자연 발생 173행)
    ip       → 클러스터 내부 파드 IP    (10.244.x.y)

보유 시간은 자연 분포(max 20.3s)에 맞추지 않는다. 맞추면 장애가 발생하지 않아
G3(서비스 피해 실재)와 충돌한다. L2의 기준은 "정상처럼 보이는가"가 아니라
"실제 버그가 이 흔적을 만들 수 있는가"이며, 수백 초짜리 idle-in-transaction은
커넥션 풀 트랜잭션 누수로 흔히 발생한다.
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, profile_instance

PROFILE_ID = "db.lock"

# 주입 세션이 사칭할 신원. 실제 앱(Spring Boot + HikariCP + PostgreSQL JDBC)이
# 남기는 application_name과 정확히 일치해야 한다.
APP_IDENTITY = "PostgreSQL JDBC Driver"

# 신원이 앱과 같아지므로 세션 이름으로는 우리 것을 골라낼 수 없다. 정리는
# **전용 클라이언트 파드를 지우는 것**으로 한다(파드가 사라지면 연결이 끊겨
# 트랜잭션이 롤백된다). pg_terminate_backend를 이름으로 쏘던 기존 방식은
# 실 서비스 세션을 죽일 수 있어 금지한다.
CLIENT_LABEL_KEY = "lucida.io/db-client"
CLIENT_LABEL_VALUE = "session"

CONTRACTS: dict[str, dict[str, Any]] = {
    "F01-P": {"engine": "oracle", "namespace": "rca-testbed-banking", "pod": "testbed-oracle-0", "schema": "BANKING", "table": "accounts", "key_column": "id", "key_value": "commerce-settlement", "client_identifier": "dba-maintenance", "hold_seconds": 600},
    "F01-R": {
        "engine": "postgresql", "access": "in-cluster-pod",
        "namespace": "rca-testbed-commerce", "db_pod": "testbed-postgres-0",
        "service": "testbed-postgres", "secret": "postgres-secret", "image": "postgres:16-alpine",
        "schema": "inventory_schema", "table": "inventory",
        "lock_scope": "row", "lock_mode": "", "key_column": "product_id", "key_value": "1",
        "client_identity": APP_IDENTITY, "hold_seconds": 600,
    },
    # F06-H: payment 경로는 결제마다 신규 INSERT라 특정 row FOR UPDATE로는 쓰기를 막지
    # 못한다(07-20 실측, payments 357k행 상주). 세션이 payments 테이블을 EXCLUSIVE
    # MODE로 잡아 INSERT(ROW EXCLUSIVE)를 블록하되 평문 SELECT/actuator health
    # (SELECT 1·ACCESS SHARE)는 통과시켜 pod readiness 자기-abort를 피한다.
    "F06-H": {
        "engine": "postgresql", "access": "in-cluster-pod",
        "namespace": "rca-testbed-commerce", "db_pod": "testbed-postgres-0",
        "service": "testbed-postgres", "secret": "postgres-secret", "image": "postgres:16-alpine",
        "schema": "payment_schema", "table": "payments",
        "lock_scope": "table", "lock_mode": "EXCLUSIVE", "key_column": "", "key_value": "",
        "client_identity": APP_IDENTITY, "hold_seconds": 600,
    },
}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    if scenario_id in CONTRACTS:
        if params != CONTRACTS[scenario_id]:
            raise ExecutorError("parameters do not exactly match the verified lock contract")
    else:
        if scenario_id not in profile["parameter_contract"]["allowed_scenarios"]:
            raise ExecutorError("scenario is not allowlisted")
        expected = profile["scenario_parameters"][scenario_id]
        if params != expected:
            raise ExecutorError("parameters do not match the approved scenario profile")

    # G6/L1: 세션 신원에 시나리오를 인코딩하면 정답이 캡처 데이터에 그대로 노출된다.
    # 과거 계약(`rca-<scenario>-<surface>-lock`)은 금지되며, 신원은 실제 앱과
    # 동일해야 한다. Oracle 경로는 별도 신원 체계라 아직 대상이 아니다(TODO 참조).
    if params.get("engine") == "postgresql":
        identity = params.get("client_identity", "")
        if identity != APP_IDENTITY:
            raise ExecutorError("client identity must impersonate the real application session")
        if params.get("access") != "in-cluster-pod":
            raise ExecutorError("postgresql lock injection must originate inside the cluster")
        scope = params.get("lock_scope")
        if scope == "row":
            if not params.get("key_column") or not params.get("key_value"):
                raise ExecutorError("row-scope lock requires key_column and key_value")
        elif scope == "table":
            if params.get("lock_mode") not in {"EXCLUSIVE", "ACCESS EXCLUSIVE", "SHARE ROW EXCLUSIVE"}:
                raise ExecutorError("unsupported table lock mode")
        else:
            raise ExecutorError("lock_scope must be row or table")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    if p.get("engine") == "oracle":
        return ["/usr/bin/bash", "-s", "--", action, plan["scenario"]["id"], p["namespace"], p["pod"], p["schema"], p["table"], p["key_column"], p["key_value"], p["client_identifier"], str(p["hold_seconds"])], ORACLE_REMOTE
    if p.get("engine") == "postgresql":
        if p.get("access") != "in-cluster-pod":
            raise ExecutorError("postgresql lock injection must originate inside the cluster")
        argv = [
            "/usr/bin/bash", "-s", "--", action, plan["scenario"]["id"],
            p["namespace"], p["db_pod"], p["service"], p["secret"], p["image"],
            p["schema"], p["table"], p["lock_scope"], p.get("lock_mode", ""),
            p.get("key_column", ""), p.get("key_value", ""),
            str(p["hold_seconds"]), p["client_identity"],
        ]
        return argv, POSTGRES_CLIENT_POD
    raise ExecutorError("db.lock requires an explicit engine")


# 클러스터 내부에 단명 클라이언트 파드를 띄워 락을 잡는다. 파드가 받는 IP는
# 실제 애플리케이션 파드와 같은 대역(10.244.x.y)이라 출처로 구별되지 않는다.
#
# 락 유지 = 서버측 pg_sleep이 아니라 **클라이언트가 트랜잭션을 열어둔 채 대기**.
# 서버에는 state='idle in transaction', wait_event='ClientRead'로 남는다.
#
# SQL 본문은 env로 전달한다. 노드의 프로세스 수집기가 cmdline을 캡처하므로
# 명령줄에 시나리오 특이 문자열을 남기지 않기 위함이다.
POSTGRES_CLIENT_POD = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; scenario="$2"; ns="$3"; db_pod="$4"; svc="$5"; secret="$6"; image="$7"
schema="$8"; table="$9"; scope="${10}"; mode="${11}"; keycol="${12}"; keyval="${13}"
hold="${14}"; identity="${15}"

k=(kubectl --kubeconfig /root/tb-kubeconfig -n "$ns")
sel="lucida.io/db-client=session"
state="/tmp/db-lock-${scenario}.state"

case "$scope" in
  row)   [[ -n "$keycol" && -n "$keyval" ]] || { echo "row scope needs key" >&2; exit 2; }
         lock_sql="SELECT ${keycol} FROM ${schema}.${table} WHERE ${keycol}='${keyval}' FOR UPDATE;" ;;
  table) case "$mode" in EXCLUSIVE|"ACCESS EXCLUSIVE"|"SHARE ROW EXCLUSIVE") : ;;
           *) echo "unsupported lock mode: $mode" >&2; exit 2 ;; esac
         lock_sql="LOCK TABLE ${schema}.${table} IN ${mode} MODE;" ;;
  *) echo "unsupported lock scope: $scope" >&2; exit 2 ;;
esac

admin() { "${k[@]}" exec "$db_pod" -- sh -lc "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -Atc \"$1\""; }
table_ok() { admin "SELECT to_regclass('${schema}.${table}') IS NOT NULL;" | tr -d '[:space:]' | grep -qx t; }
client_pods() { "${k[@]}" get pods -l "$sel" -o name 2>/dev/null | tr -d '\r'; }
no_clients() { [[ -z "$(client_pods)" ]]; }

create_pod() {
  local pod token
  token="$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"
  pod="commerce-db-client-${token}"
  "${k[@]}" apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: ${pod}
  namespace: ${ns}
  labels:
    lucida.io/db-client: session
spec:
  restartPolicy: Never
  terminationGracePeriodSeconds: 1
  containers:
    - name: client
      image: ${image}
      envFrom:
        - secretRef:
            name: ${secret}
      env:
        - name: PGHOST
          value: "${svc}"
        - name: PGAPPNAME
          value: "${identity}"
        - name: PGPASSWORD
          valueFrom:
            secretKeyRef:
              name: ${secret}
              key: POSTGRES_PASSWORD
        - name: HOLD
          value: "${hold}"
        - name: SQL_PRE
          value: "BEGIN; SELECT pg_backend_pid(); ${lock_sql}"
        - name: SQL_POST
          value: "ROLLBACK;"
      command: ["sh", "-c"]
      args:
        - '{ printf "%s\n" "\$SQL_PRE"; sleep "\$HOLD"; printf "%s\n" "\$SQL_POST"; } | psql -X -At -U "\$POSTGRES_USER" -d "\$POSTGRES_DB"'
YAML
  printf '%s' "$pod"
}

case "$action" in
  preflight)
    table_ok
    no_clients
    [[ ! -s "$state" ]]
    ;;
  run)
    table_ok; no_clients
    pod="$(create_pod)"
    "${k[@]}" wait --for=jsonpath='{.status.phase}'=Running "pod/$pod" --timeout=90s >/dev/null
    # Backend pid is kept in runner-local state only; it never reaches capture output.
    # It is NOT on the first log line: psql prints the BEGIN command tag before any
    # result, so the session logs "BEGIN", then the pid, then the locked key. Reading
    # line 1 therefore never matched and every PostgreSQL db.lock injection died with
    # "did not report a backend pid" (F01-R, 2026-07-31). Take the first all-digits
    # line instead: SQL_PRE always issues pg_backend_pid() before the lock query, so
    # that line is the pid whether or not psql prints tags.
    backend_pid=""
    for _ in $(seq 1 30); do
      backend_pid="$("${k[@]}" logs "$pod" 2>/dev/null | tr -d '\r' | sed -n '/^[0-9][0-9]*$/{p;q;}')"
      [[ "$backend_pid" =~ ^[0-9]+$ ]] && break
      backend_pid=""; sleep 1
    done
    [[ -n "$backend_pid" ]] || { "${k[@]}" delete pod "$pod" --now --ignore-not-found >/dev/null; echo "lock session did not report a backend pid" >&2; exit 1; }
    # Confirm the lock is actually held, keyed by pid (not by session name).
    admin "SELECT count(*) FROM pg_stat_activity WHERE pid=${backend_pid} AND state='idle in transaction';" \
      | tr -d '[:space:]' | grep -qx 1
    printf '%s %s\n' "$pod" "$backend_pid" >"$state"
    ;;
  cleanup)
    if [[ -s "$state" ]]; then
      read -r pod backend_pid <"$state" || true
      [[ -n "${pod:-}" ]] && "${k[@]}" delete pod "$pod" --now --ignore-not-found >/dev/null
    fi
    # Reclaim any client left behind by label, in case the state file was lost.
    for p in $(client_pods); do "${k[@]}" delete "$p" --now --ignore-not-found >/dev/null; done
    rm -f -- "$state"
    ;;
  recovery)
    no_clients
    table_ok
    ;;
  *) exit 2 ;;
esac
'''

ORACLE_REMOTE = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; scenario="$2"; ns="$3"; pod="$4"; schema="$5"; table="$6"; keycol="$7"; key="$8"; tag="$9"; hold="${10}"
k=(kubectl --kubeconfig /root/tb-kubeconfig -n "$ns"); state="/tmp/${tag}.pid"
alive() { "${k[@]}" exec "$pod" -- env STATE="$state" sh -lc 'test -s "$STATE" && kill -0 "$(cat "$STATE")" 2>/dev/null'; }
stop() { "${k[@]}" exec "$pod" -- env STATE="$state" TAG="$tag" sh -lc 'if test -s "$STATE"; then kill "$(cat "$STATE")" 2>/dev/null || true; rm -f "$STATE" "/tmp/$TAG.sql" "/tmp/$TAG.log"; fi'; }
check_row() { printf 'alter session set container=FREEPDB1;\nalter session set current_schema=%s;\nset pages 0 feedback off heading off\nselect count(*) from %s where %s='"'"'%s'"'"';\nexit;\n' "$schema" "$table" "$keycol" "$key" | "${k[@]}" exec -i "$pod" -- sqlplus -s / as sysdba | tr -d '[:space:]' | grep -qx 1; }
case "$action" in preflight) check_row; ! alive;; run) check_row; ! alive; "${k[@]}" exec "$pod" -- sh -lc 'cat > /tmp/'"'"'$tag'"'"'.sql <<EOF
alter session set container=FREEPDB1;
alter session set current_schema='"'"'$schema'"'"';
begin dbms_session.set_identifier('"'"'$tag'"'"'); end;
/
select '"'"'$keycol'"'"' from '"'"'$table'"'"' where '"'"'$keycol'"'"'='"'"''"'"'$key'"'"''"'"' for update;
host sleep '"'"'$hold'"'"'
rollback;
exit;
EOF
nohup sqlplus -s / as sysdba @/tmp/'"'"'$tag'"'"'.sql >/tmp/'"'"'$tag'"'"'.log 2>&1 </dev/null & echo $! >/tmp/'"'"'$tag'"'"'.pid' ;; cleanup) stop;; recovery) ! alive; check_row;; *) exit 2;; esac
'''
# L1 해소(2026-07-28): client_identifier가 시나리오 ID를 인코딩하던 것을
#   `dba-maintenance` 상수로 통일했다(F01-P·F08-G·F15-G 공통). 케이스마다 다르면
#   태그 하나가 정답을 지목하지만(L1), 전 케이스 균일하면 변별정보가 0이라
#   공개 가능한 환경 서명(L4)이 된다 — 기준서 G6의 판정 규칙 그대로다.
#   값 선택 근거: 실 앱은 set_identifier를 호출하지 않으므로 무엇을 넣어도
#   앱과 같아지지는 않는다. 그렇다면 특정 서비스를 지목하지 않는 운영 주체
#   이름이 안전하다 — `banking-interest-batch` 같은 값은 무고한 서비스를
#   가리키는 **틀린 단서**가 된다.
#
# TODO(G6/L2 — Oracle): 남은 두 축은 그대로다.
#   · `host sleep`으로 유지 → 대기 이벤트가 인위적(L2)
#   · DB 파드 내부 실행이라 출처가 앱과 다름(L2)
#   PostgreSQL과 동일하게 (1) 실 앱 신원 사칭 (2) idle-in-transaction 유지
#   (3) 클러스터 내 클라이언트 파드로 전환해야 한다. Oracle 실 앱 세션의
#   program/module 값을 캡처에서 실측한 뒤 착수할 것.


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
