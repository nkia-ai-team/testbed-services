"""Fakes around the operator database sessions (profiles/db_operator_remote.py, v4 C6b).

The executors no longer exec into the database pod; they read the listener address and the
account from the API (kubectl get) and run the client on the runner. These fakes stand in for
the three things the body touches:

* ``kubectl``: answers the reads (pod node address, NodePort, Secret, pod, claim, volume, kubelet
  summary, old client pods) and refuses everything else, ``exec`` above all. Every call is logged.
* the system python (``DB_OPERATOR_PYTHON``): ``-c "import <driver>"`` succeeds; the client verbs go
  to responders the test supplies, the way the old fake ``kubectl exec ... sqlplus`` did. The
  Oracle responder reads the SQL*Plus script on stdin (unchanged from the old fakes), the MySQL
  responder reads ``SQL`` and ``DB`` from its environment. The account the call used is logged
  without its password.
* ``psql``: a responder that reads ``-c`` and ``PGOPTIONS``.

The client itself (script parsing, output) has its own tests with fake drivers
(test_db_host_executor_foundations.OperatorClientTests); real databases were measured on 104
(results in the C6b report).
"""
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

SECRET_VALUES = {
    "APP_USER": "banking",
    "APP_USER_PASSWORD": "pw-app-from-secret",
    "ORACLE_PASSWORD": "pw-dba-from-secret",
    "MON_USER": "lucida_mon",
    "MON_PASSWORD": "pw-mon-from-secret",
    "MYSQL_ROOT_PASSWORD": "pw-root-from-secret",
    "MYSQL_USER": "fooddelivery",
    "MYSQL_PASSWORD": "pw-food-from-secret",
    "POSTGRES_USER": "commerce",
    "POSTGRES_PASSWORD": "pw-pg-from-secret",
    "POSTGRES_DB": "commerce",
}
PASSWORDS = tuple(v for k, v in SECRET_VALUES.items() if "PASSWORD" in k)

NODE_FS = {"capacityBytes": 100 * 1024**3, "usedBytes": 20 * 1024**3, "availableBytes": 80 * 1024**3}


def _kubectl(tmp: Path, extra_cases: str) -> str:
    secret_cases = "\n".join(
        f"  *'jsonpath={{.data.{key}}}'*) printf {base64.b64encode(value.encode()).decode()} ;;"
        for key, value in SECRET_VALUES.items()
    )
    summary = '{"node":{"fs":' + str(NODE_FS).replace("'", '"') + "}}"
    pod_json = ('{"spec":{"nodeName":"tb-w9","volumes":[{"name":"data",'
                '"persistentVolumeClaim":{"claimName":"data-0"}}]}}')
    return f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >>{tmp}/kubectl.log
[[ -e {tmp}/kubectl-down ]] && {{ echo 'The connection to the server was refused' >&2; exit 1; }}
case "$*" in
  *" exec "*) echo "fake kubectl: exec refused" >&2; exit 97 ;;
{extra_cases}
  *hostIP*) printf 192.168.122.11 ;;
  *podIP*) printf 10.244.3.9 ;;
  *"get svc "*nodePort*) printf 30999 ;;
{secret_cases}
  *"get pods -l lucida.io/db-client=session"*) [[ -e {tmp}/legacy-pod ]] && cat {tmp}/legacy-pod ;;
  *"delete pod/"*) rm -f {tmp}/legacy-pod ;;
  *"stats/summary"*) printf '%s' '{summary}' ;;
  *"get pvc data-0"*) printf pv-data-0 ;;
  *"get pv pv-data-0 -o json"*) printf '%s' '{{"spec":{{"hostPath":{{"path":"/opt/local-path-provisioner/x"}}}}}}' ;;
  *" get pod "*"-o json"*) printf '%s' '{pod_json}' ;;
  *) echo "fake kubectl: unexpected $*" >&2; exit 9 ;;
esac
"""


def install(tmp: Path, *, oracle: str | None = None, mysql: str | None = None, psql: str | None = None,
            kubectl_cases: str = "") -> dict[str, str]:
    """Write the fakes under tmp/bin and return the environment that uses them.

    oracle, mysql, psql are Python sources of the responders (run with this interpreter).
    kubectl_cases are extra `case` arms checked before the built-in ones.
    """
    bindir = tmp / "bin"
    bindir.mkdir(exist_ok=True)
    kubectl = bindir / "kubectl"
    kubectl.write_text(_kubectl(tmp, kubectl_cases))
    kubectl.chmod(0o755)
    responders = {}
    for name, source in (("oracle", oracle), ("mysql", mysql), ("psql", psql)):
        if source is not None:
            path = tmp / f"fake_{name}.py"
            path.write_text(source)
            responders[name] = path
    python = bindir / "python-operator"
    lines = [
        "#!/usr/bin/env bash",
        # Driver checks (dbo_driver) succeed; the client verbs go to the responders.
        'if [[ "$1" == -c && "$2" == "import "* ]]; then exit 0; fi',
        'verb="$3"',
        f'printf "%s %s %s %s | argv:%s\\n" "$verb" "${{ODB_USER:-${{MDB_USER:-}}}}" "${{ODB_DSN:-${{MDB_HOST:-}}:${{MDB_PORT:-}}}}" "${{DB:-}}" "${{*:3}}" >>{tmp}/client.log',
        'case "$verb" in',
    ]
    if "oracle" in responders:
        lines.append(f'  oracle) exec {sys.executable} {responders["oracle"]} ;;')
    if "mysql" in responders:
        lines.append(f'  mysql|mysql-stream) exec {sys.executable} {responders["mysql"]} "$verb" ;;')
    lines += ['  *) echo "fake python: no responder for $verb" >&2; exit 9 ;;', "esac", ""]
    python.write_text("\n".join(lines))
    python.chmod(0o755)
    if "psql" in responders:
        fake_psql = bindir / "psql"
        fake_psql.write_text(f"#!/usr/bin/env bash\nexec {sys.executable} {responders['psql']} \"$@\"\n")
        fake_psql.chmod(0o755)
    return dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", DB_OPERATOR_PYTHON=str(python),
                SCENARIO_PROFILE_STATE_ROOT=str(tmp / "state"))


def kubectl_calls(tmp: Path) -> list[str]:
    path = tmp / "kubectl.log"
    return path.read_text().splitlines() if path.exists() else []


def assert_reads_only(test, tmp: Path) -> None:
    """Every kubectl call was a read (get), never exec, apply, delete or patch."""
    for line in kubectl_calls(tmp):
        words = line.split()
        verb = next((w for w in words if w in ("get", "exec", "apply", "delete", "patch", "create", "run")), None)
        test.assertEqual(verb, "get", line)


def assert_no_password(test, *texts: str | bytes) -> None:
    for text in texts:
        if isinstance(text, bytes):
            text = text.decode(errors="replace")
        for password in PASSWORDS:
            test.assertNotIn(password, text)


# A fake PostgreSQL behind psql for the lock session (pg_lock_remote.py). Sessions live as files under
# <tmp>/pg-sessions/<pid> ("<backend_start>|<state>"). `psql -c` answers the probes; psql without -c is
# the lock session reading statements on stdin, as the coprocess feeds it. A file <tmp>/pg-held-by-other
# makes the lock wait time out, as a row held by someone else would.
FAKE_PSQL_SERVER = r'''
import os, re, signal, sys, json, time
from pathlib import Path
TMP = Path(__TMP__)
SESS = TMP / "pg-sessions"
SESS.mkdir(exist_ok=True)
with (TMP / "psql.log").open("a") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "app": os.environ.get("PGAPPNAME", ""),
                          "options": os.environ.get("PGOPTIONS", ""), "host": os.environ.get("PGHOST", ""),
                          "user": os.environ.get("PGUSER", "")}) + "\n")
args = sys.argv[1:]
if "-c" in args:
    sql = args[args.index("-c") + 1]
    if sql.startswith("SHOW tcp_keepalives_idle"):
        print("60" if "tcp_keepalives_idle=60" in os.environ.get("PGOPTIONS", "") else "0")
    elif "to_regclass(" in sql:
        print("t")
    elif re.search(r"FROM \w+\.\w+ WHERE \w+='[^']*';$", sql):
        print("1")
    elif "SELECT backend_start FROM pg_stat_activity WHERE pid = " in sql:
        pid = re.search(r"pid = (\d+)", sql).group(1)
        f = SESS / pid
        if f.exists():
            start, state = f.read_text().split("|")
            if state == "idle in transaction":
                print(start)
    elif "FROM pg_stat_activity WHERE pid = " in sql and "backend_start = " in sql:
        pid = re.search(r"pid = (\d+)", sql).group(1)
        start = re.search(r"backend_start = '([^']*)'", sql).group(1)
        f = SESS / pid
        print(1 if f.exists() and f.read_text().split("|")[0] == start else 0)
    else:
        print(f"fake psql: unexpected {sql}", file=sys.stderr)
        sys.exit(3)
    sys.exit(0)
me = SESS / str(os.getpid())
def gone(*_):
    me.unlink(missing_ok=True)
    sys.exit(0)
signal.signal(signal.SIGTERM, gone)
for line in sys.stdin:
    line = line.strip()
    if line == "SELECT pg_backend_pid();":
        me.write_text(f"2026-11-10 01:00:00.{os.getpid()}+00|idle in transaction")
        print(os.getpid(), flush=True)
    elif line.startswith(("LOCK TABLE", "SELECT ")) and ("FOR UPDATE" in line or line.startswith("LOCK TABLE")):
        if (TMP / "pg-held-by-other").exists():
            print("ERROR:  canceling statement due to lock timeout", flush=True)
            gone()
        if "FOR UPDATE" in line:
            print("1", flush=True)
        (TMP / "pg-lock-statement").write_text(line)
    elif line == "\\echo locked":
        print("locked", flush=True)
    elif line == "ROLLBACK;":
        me.unlink(missing_ok=True)
    elif line == "\\q":
        break
gone()
'''


def pg_server(tmp: Path) -> str:
    return FAKE_PSQL_SERVER.replace("__TMP__", repr(str(tmp)))


def pg_sessions(tmp: Path) -> list[str]:
    d = tmp / "pg-sessions"
    return sorted(p.name for p in d.iterdir()) if d.exists() else []
