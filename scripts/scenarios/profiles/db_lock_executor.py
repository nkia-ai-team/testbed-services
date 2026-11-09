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

import db_operator_remote
import oracle_lock_remote
import pg_lock_remote
from executor_common import ExecutorError, cli, profile_instance

PROFILE_ID = "db.lock"

# 주입 세션이 사칭할 신원. 실제 앱(Spring Boot + HikariCP + PostgreSQL JDBC)이
# 남기는 application_name과 정확히 일치해야 한다.
APP_IDENTITY = "PostgreSQL JDBC Driver"

# 신원이 앱과 같아지므로 세션 이름으로는 우리 것을 골라낼 수 없다. 정리는
# **잠금 세션 프로세스를 끝내는 것**으로 한다(연결이 끊기면 트랜잭션이 롤백된다).
# pg_terminate_backend를 이름으로 쏘던 기존 방식은 실 서비스 세션을 죽일 수 있어 금지한다.
# 2026-07-28 부터 11-10 까지는 클러스터 안 전용 클라이언트 파드가 세션을 쥐었고 정리는 그 파드를
# 지우는 것이었다. 그 파드가 KCM 이벤트, kubelet/containerd syslog, 명령줄에 흔적을 남겨(2026-08-23
# F01-R 녹화) v4 C6b 에서 러너 컨테이너의 네트워크 세션으로 바꿨다(pg_lock_remote.py).
# 옛 파드의 라벨은 남아 있는 옛 실행을 정리하는 데만 쓴다.
CLIENT_LABEL_KEY = "lucida.io/db-client"
CLIENT_LABEL_VALUE = "session"
# 세션이 어디서 붙는지(계약의 access). 러너 컨테이너에서 리스너(NodePort)로.
PG_ACCESS = "runner-network"

CONTRACTS: dict[str, dict[str, Any]] = {
    # hold 600->900 (2026-08-06, 0804 #9): min_hold 8m 뒤 성공 창이 2분뿐이라
    # 요동치는 payment_error_rate로 3연속을 다시 만들 시간이 없었다.
    "F01-P": {"engine": "oracle", "namespace": "rca-testbed-banking", "pod": "testbed-oracle-0", "schema": "BANKING", "table": "accounts", "key_column": "id", "key_value": "commerce-settlement", "client_identifier": "dba-maintenance", "hold_seconds": 900},
    "F01-R": {
        "engine": "postgresql", "access": PG_ACCESS,
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
        "engine": "postgresql", "access": PG_ACCESS,
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
    # 동일해야 한다. Oracle 경로는 운영 도구 신원을 쓴다(아래 ORACLE_CLIENT 주석).
    if params.get("engine") == "postgresql":
        identity = params.get("client_identity", "")
        if identity != APP_IDENTITY:
            raise ExecutorError("client identity must impersonate the real application session")
        if params.get("access") != PG_ACCESS:
            raise ExecutorError("postgresql lock injection must be a network session from the runner")
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
        return ["/usr/bin/bash", "-s", "--", action, plan["scenario"]["id"], p["namespace"], p["pod"], p["schema"], p["table"], p["key_column"], p["key_value"], p["client_identifier"], str(p["hold_seconds"])], ORACLE_CLIENT
    if p.get("engine") == "postgresql":
        if p.get("access") != PG_ACCESS:
            raise ExecutorError("postgresql lock injection must be a network session from the runner")
        argv = [
            "/usr/bin/bash", "-s", "--", action, plan["scenario"]["id"],
            p["namespace"], p["db_pod"], p["service"], p["secret"], p["image"],
            p["schema"], p["table"], p["lock_scope"], p.get("lock_mode", ""),
            p.get("key_column", ""), p.get("key_value", ""),
            str(p["hold_seconds"]), p["client_identity"],
        ]
        return argv, POSTGRES_CLIENT
    raise ExecutorError("db.lock requires an explicit engine")


# PostgreSQL 잠금(v4 C6b, 2026-11-10). 러너 컨테이너의 psql 이 리스너(NodePort)로 붙어, 계약의
# client_identity(실 앱의 application_name)로 트랜잭션을 연 채 쉰다. 서버에는 idle in transaction,
# ClientRead 로 남는다(서버측 pg_sleep 이 아니다). 클러스터에는 파드도 exec 도 생기지 않는다.
# 정리는 이 호스트의 세션 프로세스를 먼저 멈추고(kube API, DB 가 필요 없다) 그다음 서버에서 그
# 세션(backend pid 와 시작 시각으로 고른다, 이름이 아니다)이 사라졌는지 확인한다. 함수는
# pg_lock_remote.py, 접속은 db_operator_remote.py. 계약의 service, image 는 더 쓰지 않는다.
_POSTGRES_HEAD = br"""#!/usr/bin/env bash
set -euo pipefail
action="$1"; scenario="$2"; ns="$3"; db_pod="$4"; svc="$5"; secret="$6"; image="$7"
schema="$8"; table="$9"; scope="${10}"; mode="${11}"; keycol="${12}"; keyval="${13}"
hold="${14}"; identity="${15}"
run_dir="${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}/db-lock-postgres-${scenario}"
"""
_POSTGRES_BODY = br"""
db_target "$ns" "$db_pod"
case "$action" in
  preflight)
    pgl_check "$run_dir" "$schema" "$table" "$scope" "$keycol" "$keyval"
    ;;
  run)
    pgl_check "$run_dir" "$schema" "$table" "$scope" "$keycol" "$keyval"
    pgl_apply "$run_dir" "$schema" "$table" "$scope" "$mode" "$keycol" "$keyval" "$hold" "$identity"
    ;;
  cleanup)
    pgl_release "$run_dir"
    ;;
  recovery)
    pgl_recover "$run_dir" "$schema" "$table" "$scope" "$keycol" "$keyval"
    ;;
  *) exit 2 ;;
esac
"""
POSTGRES_CLIENT = (_POSTGRES_HEAD + db_operator_remote.bash_functions() + pg_lock_remote.bash_functions()
                   + _POSTGRES_BODY)

# Oracle 잠금(v4 C6, 2026-11-09). 옛 방식은 DB 파드에 exec 해 `sqlplus / as sysdba` 로
# 잠그고 `host sleep` 으로 버텼다. 119 녹화(2026-09-01 F01-P)에 세 흔적이 남았다:
#   · dpm_session 차단 세션이 schema=SYS, program=sqlplus@testbed-oracle-0,
#     machine=testbed-oracle-0 (DB 서버가 자기 행을 SYS 로 잠그는 모양은 현실에 없다)
#   · process_meta 에 `sqlplus -s / as sysdba @/tmp/dba-maintenance.sql`
#   · process_meta 에 DB 파드 컨테이너의 `sleep 900`
# 이제 잠금 세션은 러너 컨테이너에서 리스너(NodePort)로 앱 스키마 계정으로 붙고, 운영
# 도구 모양의 신원으로 트랜잭션을 연 채 논다(oracle_lock_session.py, oracle_lock_remote.py).
# DB 파드와 노드에는 프로세스도 파일도 남지 않는다. 피해 쪽 기전(행 잠금, 서버 세션
# INACTIVE `SQL*Net message from client`, 대기자 `enq: TX - row lock contention`)은 같다.
#
# L1 해소(2026-07-28): client_identifier가 시나리오 ID를 인코딩하던 것을
#   `dba-maintenance` 상수로 통일했다(F01-P·F08-G·F15-G 공통). 케이스마다 다르면
#   태그 하나가 정답을 지목하지만(L1), 전 케이스 균일하면 변별정보가 0이라
#   공개 가능한 환경 서명(L4)이 된다 — 기준서 G6의 판정 규칙 그대로다.
#   러너의 감별 관측(database.oracle_tagged_session_count)이 이 값을 세므로 잠금
#   세션에만 그대로 붙인다. 119 dpm_session 에는 이 칸이 실리지 않는다.
#
# 정리는 잠금 세션 프로세스 종료(SIGTERM 이면 롤백, 안 되면 SIGKILL 로 소켓을 닫아
# 서버가 롤백)이고, 서버 쪽 확인은 그 행을 FOR UPDATE WAIT 로 잡아 보는 것이다.
# 세션 이름으로 다른 세션을 죽이지 않는다. 정리는 kube API, DB 가 필요 없는 로컬 정지를
# 먼저 하고 그다음 확인한다(API 가 죽어도 잠금 세션은 멈춘다, 실패는 보고한다).
#
# 러너가 FIN 없이 사라지면(tb-ops 정지, 망 단절) 이 호스트는 아무것도 못 한다. 그때는
# Oracle 서버의 dead connection detection(SQLNET.EXPIRE_TIME. 10-oracle.yaml 의
# oracle-startdb-scripts 가 기동마다 건다)이 약 2분 안에 세션을 정리해 롤백한다(U8).
# 사전검사가 DB 파드에 그 설정이 걸려 있는지 확인하고 없으면 주입하지 않는다(ora_dcd_ok).
_ORACLE_HEAD = br"""#!/usr/bin/env bash
set -euo pipefail
action="$1"; scenario="$2"; ns="$3"; pod="$4"; schema="$5"; table="$6"; keycol="$7"; key="$8"; tag="$9"; hold="${10}"
run_dir="${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}/db-lock-oracle-${scenario}"
"""
_ORACLE_BODY = br"""
case "$action" in
  preflight|run)
    ora_idle "$run_dir"
    ora_prepare "$ns" "$pod"
    ora_dcd_ok "$ns" "$pod"
    ora_target "$schema" "$table" "$keycol" "$key"
    ora_row_ok
    # Released before starting = no session (ours or a leftover) holds the row now.
    ora_released
    if [[ "$action" == run ]]; then ora_hold_start "$run_dir" "$tag" "$hold"; fi
    ;;
  cleanup)
    # Local first: the holder on this host is stopped whatever the API or the database
    # says. Only then the server-side check, and any failure is reported.
    stop_rc=0
    ora_hold_stop "$run_dir" || stop_rc=1
    if [[ "$stop_rc" == 0 && ! -d "$run_dir" ]]; then exit 0; fi
    ora_prepare "$ns" "$pod"
    ora_target "$schema" "$table" "$keycol" "$key"
    ora_released
    [[ "$stop_rc" == 0 ]]
    rm -rf -- "$run_dir"
    ;;
  recovery)
    ora_idle "$run_dir"
    ora_prepare "$ns" "$pod"
    ora_target "$schema" "$table" "$keycol" "$key"
    ora_row_ok
    ora_released
    ;;
  *) exit 2 ;;
esac
"""
ORACLE_CLIENT = _ORACLE_HEAD + oracle_lock_remote.bash_functions() + _ORACLE_BODY


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
