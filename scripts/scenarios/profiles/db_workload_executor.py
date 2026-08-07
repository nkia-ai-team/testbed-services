#!/usr/bin/env python3
"""Bounded, read-only, periodic wide-scan workload against a shared database.

F02-H(캐시 무력화 재설계, docs/scenario-redesign-wip/f02-h-cache-defeat-redesign-0806.md)의
companion 주입 표면이다. io 사다리만으로는 인과가 없었다 — 핫 워킹셋이 캐시 안에
살아 디스크가 81%여도 업무 질의는 41.9ms였다(0804 #24). 캐시(shared_buffers 128MB +
파드 limit 512Mi)의 8배인 DB(4,272MB)에서 캐시 밖 대역을 주기적으로 훑어 상주 페이지를
밀어내면, 업무 질의가 콜드 페이지를 "바쁜 장치"에서 읽게 된다. 재기동이 필요 없다는
것이 이 설계의 핵심이다(재기동은 F18-P 계보에서 자백이자 자기 실격).

## 왜 이 형태인가 — db.lock이 실측으로 확정한 L2/G6 규칙을 그대로 따른다

2026-07-27 캡처 실측(db_lock_executor 참조)은 주입 세션이 남기는 세 축이 전부
유일값이면 그 자체로 정답을 자백한다는 것을 보였다. 같은 세 축을 여기서도 막는다.

  · `application_name` — 기존 db.workload 스텁은 `rca-F02-G-batch-heavy-sql`을 썼다.
    이것이 정확히 실측에서 14,167행 중 유일값으로 적발된 그 패턴이다(당시
    `rca-F01-R-inventory-lock`). 실제 앱과 같은 `PostgreSQL JDBC Driver`로 사칭한다.
  · `client_addr` — tb-runner에서 붙으면 클러스터 밖 NAT 주소(10.244.0.0)가 찍혀
    출처가 드러난다. db.lock과 동일하게 **클러스터 내부 단명 클라이언트 파드**에서
    붙어 실제 앱 파드와 같은 대역(10.244.x.y)을 받는다.
  · `wait_event` — 서버측 `pg_sleep`은 `PgSleep` 대기를 남기고, 이는 실운영 분포에
    존재하지 않는 유일값이었다. 주기 대기는 **클라이언트측 `sleep`**으로 한다.
    패스 사이 세션 상태는 자연 발생하는 `idle`이다.

SQL 본문은 명령줄이 아니라 **env**로 넘긴다 — 노드의 프로세스 수집기(KCM)가 cmdline을
캡처하므로, 자기가 훑는 테이블 이름을 argv에 적는 주입은 이미 자백한 것이다
(app_control_executor의 같은 판단). 식별자는 따옴표로 감싸지 않는다(따옴표 친
테이블명은 테이블이 아니라 문자열을 읽는다).

## 읽기 전용은 선언이 아니라 서버측 강제다

세션이 `default_transaction_read_only = on`으로 열리므로 실수로 쓰기 문장이 들어가면
주입이 조용히 성공하는 대신 실패한다. 업무 테이블 잠금·쓰기 금지는 F02-H 설계 문서의
명시 요구이고, 잠금은 db.lock의 표면이라 여기서 겹치면 두 시나리오의 원인이 갈리지
않는다. `statement_timeout`은 한 패스가 창을 넘겨 눌러앉는 것을 막는다.

## 사다리 knob은 주기 하나다

설계 문서는 주기(10s/5s/2s)와 대역(1/4·1/2·전체)을 함께 적었지만, 대역을 파라미터로
열려면 테이블마다 단조 컬럼의 의미를 알아야 하고 그 지식이 계약에 새는 순간
allowlist가 임의 SQL 표면으로 번진다. 강도 축은 주기만으로도 같은 범위를 덮으므로
`period_seconds`만 범위 검증 대상으로 두고 나머지는 동일성으로 고정한다(사다리 단이
기본값과 다르다는 이유로 통째로 거부되던 배치 #12 재발 방지 — app_control과 같은 형태).

대상 테이블을 하나로 고정한 것도 실측 근거가 있다. `payment_schema.payment_logs`는
610MB로 파드 메모리 limit(512Mi)보다 크다 — 한 번 훑는 것만으로 상주 페이지가 전부
교체된다. 대역을 쪼갤 이유가 없다.

## 구현하지 않은 모드 — F03-P 커넥션 점유 지연 (조건부 보류)

"pg_sleep으로 트랜잭션 점유 시간을 늘린다"는 두 번째 용도는 **의도적으로 넣지 않았다**.
왜 반쪽만 만들었는지가 나중에 반드시 물어질 것이므로 근거를 남긴다.

먼저, 이 모드를 요청하게 만든 원래 전제가 **2026-08-07 실측으로 반증됐다**.

  · 전제였던 것: "pool=2면 이론 처리량 ~400rps인데 부하는 60rps라 풀이 물리적으로
    포화될 수 없다 → 인위적 DB 지연 없이는 F03-P가 원리적으로 성립하지 않는다."
  · 실측: run 4a1cbd57에서 `payment_p95>=500`이 **연속 11틱** 달성됐다(요구 3틱).
    풀은 실제로 막힌다 — 유효 서비스 시간이 DB 작업 몇 ms가 아니라 @Transactional
    안의 외부 HTTP 두 번(PG mock·banking transfer, read-timeout 각 10s)이기 때문이다.
  · 막힌 쪽은 p95가 아니라 success의 다른 항이다. F03-P의 success는 AND이고
    (`payment_p95>=500` AND `checkout_5xx_rate>=0.05`), `checkout_5xx_rate`가 전 구간
    0.00이었다.
  · 그 0.00은 F05-R을 막았던 것과 같은 기전으로 보인다 — commerce 재고 고갈로
    checkout이 `reserveStock`에서 409로 조기 종료되면 payment에 **도달조차 못 하므로**
    payment가 아무리 느려도 checkout은 5xx를 낼 수 없다. 호출 순서는 코드로 확인했다:
    OrderService.createOrder가 재고 예약을 먼저 하고(:67) 결제는 그 뒤다(:102).
    재고 보충 재설계(52f23b6)가 그 409 지배를 없앤다.

즉 F03-P는 **db.workload 없이 재배치에서 풀릴 수 있다**. 그래서 이 모드는 폐기가
아니라 조건부 보류다 — 재배치 후에도 F03-P의 checkout_5xx_rate가 여전히 안 움직이면
그때 다시 꺼낸다.

그리고 다시 꺼내더라도 **pg_sleep 형태로는 안 된다**. 위 반증과 무관하게 두 가지가
각각 독립으로 성립하지 않기 때문이다.

  1. 기전이 닿지 않는다. F03-P가 늘리려는 것은 payment-service **자신의** Hikari
     커넥션 점유 시간인데(PaymentService.processPayment의 @Transactional), 외부 세션의
     pg_sleep은 그 트랜잭션을 전혀 건드리지 못한다. 점유 시간은 정답지가 적은 대로
     "두 원격 왕복의 합"이고, `payment_schema.payments`는 orders로의 FK가 없어
     INSERT가 외부 읽기 세션과 충돌할 잠금 자체가 없다. 밖에서 막으려면 테이블
     잠금이어야 하는데 그건 db.lock의 표면이고 큐잉이 아니라 전면 정지를 만든다.
  2. 설령 닿아도 자백한다. 위의 `PgSleep` 대기 실측이 그대로 적용된다.

이건 실행기 문제가 아니라 F03-P의 설계 결정이므로 별건으로 올렸다.
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, profile_instance

PROFILE_ID = "db.workload"

# 주입 세션이 사칭할 신원. db.lock의 APP_IDENTITY와 같은 값이어야 한다 — 실제
# Spring Boot + HikariCP + PostgreSQL JDBC 세션이 남기는 application_name이다.
APP_IDENTITY = "PostgreSQL JDBC Driver"

# 시나리오 이름이 새면 그 자체가 정답이다. 신원 문자열에 이 조각이 들어오면 거부한다.
FORBIDDEN_IDENTITY_FRAGMENTS = ("rca", "scenario", "lucida", "f02", "f03", "inject")

# 주기는 사다리가 단마다 바꾸는 유일한 knob이다.
MIN_PERIOD_SECONDS = 1
MAX_PERIOD_SECONDS = 60

CONTRACTS: dict[str, dict[str, Any]] = {
    # readiness=parked. 사다리 실측 전의 잠정 단이고, 판정 계약은 실측 후에 붙는다
    # (app_control의 F21-P와 같은 상태). 이 표가 프로파일을 라이브로 열어 두는 유일한
    # 권한이므로, 카탈로그에 없는 시나리오는 여기에 남기지 않는다 — 계획될 수 없는
    # 시나리오를 라이브 allowlist에 두는 것은 fail-open이다(구 F02-G 항목 제거).
    "F02-H": {
        "engine": "postgresql",
        "access": "in-cluster-pod",
        "mode": "scan",
        "namespace": "rca-testbed-commerce",
        "db_pod": "testbed-postgres-0",
        "service": "testbed-postgres",
        "secret": "postgres-secret",
        "image": "postgres:16-alpine",
        "schema": "payment_schema",
        "table": "payment_logs",
        "client_identity": APP_IDENTITY,
        "period_seconds": 10,
        "runtime_seconds": 600,
        "statement_timeout_seconds": 120,
    },
}


def _without_period(params: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in params.items() if k != "period_seconds"}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    contract = CONTRACTS.get(scenario_id)
    if contract is None:
        raise ExecutorError("scenario is not allowlisted for db workload")
    period = params.get("period_seconds")
    if (isinstance(period, bool) or not isinstance(period, int)
            or not MIN_PERIOD_SECONDS <= period <= MAX_PERIOD_SECONDS):
        raise ExecutorError(
            f"period_seconds must be an integer within {MIN_PERIOD_SECONDS}..{MAX_PERIOD_SECONDS}"
        )
    if _without_period(params) != _without_period(contract):
        raise ExecutorError("parameters do not exactly match the verified workload contract")
    if params.get("mode") != "scan":
        raise ExecutorError("db.workload supports only the read-only scan mode")
    if params.get("engine") != "postgresql":
        raise ExecutorError("db.workload requires an explicit postgresql engine")
    # 클러스터 밖에서 붙으면 client_addr이 tb-runner의 NAT 주소로 찍혀 출처가 드러난다.
    if params.get("access") != "in-cluster-pod":
        raise ExecutorError("db workload must originate inside the cluster")
    identity = str(params.get("client_identity", ""))
    lowered = identity.lower()
    if not identity or any(bad in lowered for bad in FORBIDDEN_IDENTITY_FRAGMENTS):
        raise ExecutorError("client_identity must impersonate a real application session")
    allowed = profile.get("parameter_contract", {}).get("allowed_scenarios")
    if allowed is not None and scenario_id not in allowed:
        raise ExecutorError("scenario is not enabled by the profile registry")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    validate(plan["scenario"]["id"], p, {})
    # 질의문은 여기서 조립하지 않는다 — 스키마·테이블은 이미 인자로 넘어가므로
    # 완성된 SELECT까지 argv에 실으면 같은 사실을 한 겹 더 노출할 뿐이고, 조립처가
    # 둘이 되면 서로 어긋난다. 원격 스크립트가 유일한 조립처다(db.lock과 같은 형태).
    return [
        "/usr/bin/bash", "-s", "--", action, plan["scenario"]["id"],
        p["namespace"], p["db_pod"], p["service"], p["secret"], p["image"],
        p["schema"], p["table"], p["client_identity"],
        str(p["period_seconds"]), str(p["runtime_seconds"]),
        str(p["statement_timeout_seconds"]),
    ], POSTGRES_CLIENT_POD


# The scan runs from a short-lived in-cluster pod so its client_addr sits in the same
# 10.244.x.y range as the application pods. The SQL text rides in the pod's env, never
# in argv: the node process collector captures command lines, and a scan that names its
# own table there has already confessed. The pass interval is a client-side `sleep`, not
# a server-side pg_sleep -- the 2026-07-27 capture found PgSleep to be a wait event that
# never occurs in real traffic, so it identifies the injection on its own.
POSTGRES_CLIENT_POD = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; scenario="$2"; ns="$3"; db_pod="$4"; svc="$5"; secret="$6"; image="$7"
schema="$8"; table="$9"; identity="${10}"; period="${11}"; runtime="${12}"; stmt_timeout="${13}"

# Read-only is enforced by the server for the whole transaction, so a write that ever
# reaches this session fails instead of quietly succeeding. Identifiers are never
# quoted: a quoted table name reads a string, not the table.
scan_sql="SET default_transaction_read_only = on; SET statement_timeout = '${stmt_timeout}s'; SELECT count(*) FROM ${schema}.${table};"

k=(kubectl --kubeconfig /root/tb-kubeconfig -n "$ns")
sel="lucida.io/db-workload=scan"
state="/tmp/db-workload-${scenario}.state"

admin() { "${k[@]}" exec "$db_pod" -- sh -lc "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -Atc \"$1\""; }
table_ok() { admin "SELECT to_regclass('${schema}.${table}') IS NOT NULL;" | tr -d '[:space:]' | grep -qx t; }
client_pods() { "${k[@]}" get pods -l "$sel" -o name 2>/dev/null | tr -d '\r'; }
no_clients() { [[ -z "$(client_pods)" ]]; }

create_pod() {
  local pod token
  token="$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"
  pod="commerce-db-report-${token}"
  "${k[@]}" apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: ${pod}
  namespace: ${ns}
  labels:
    lucida.io/db-workload: scan
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
        - name: PERIOD
          value: "${period}"
        - name: RUNTIME
          value: "${runtime}"
        - name: SCAN_SQL
          value: "${scan_sql}"
      command: ["sh", "-c"]
      args:
        - 'end=\$(( \$(date +%s) + \$RUNTIME )); while [ "\$(date +%s)" -lt "\$end" ]; do printf "%s\n" "\$SCAN_SQL" | psql -X -At -U "\$POSTGRES_USER" -d "\$POSTGRES_DB" >/dev/null || true; sleep "\$PERIOD"; done'
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
    printf '%s\n' "$pod" >"$state"
    ;;
  # The pod is the injection state: deleting it drops the connection, so cleanup never
  # terminates backends server-side. Killing by application_name would be actively
  # unsafe here -- the session impersonates the JDBC driver, so that name also matches
  # every real pooled connection (db.lock learned this first).
  cleanup)
    if [[ -s "$state" ]]; then
      read -r pod <"$state" || true
      [[ -n "${pod:-}" ]] && "${k[@]}" delete pod "$pod" --now --ignore-not-found >/dev/null
    fi
    for p in $(client_pods); do "${k[@]}" delete "$p" --now --ignore-not-found >/dev/null; done
    rm -f -- "$state"
    ;;
  recovery)
    no_clients
    [[ ! -s "$state" ]]
    ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
