#!/usr/bin/env python3
"""Lock a single application database account and unlock it again.

F35-R reconstructs a credential rotation that disabled the old database user
while the services still logged in with it (Harness 2026-01-08).  `ACCOUNT LOCK`
refuses every *new* login (ORA-28000) but leaves the sessions already open alone,
so the services keep working until their Hikari pools retire those connections
(max-lifetime 10 min) and cannot replace them.  That delay is the scenario: the
change happens once, the damage arrives as the pools drain.

The blast radius is one account.  The database stays up, the DPM monitoring
account (LUCIDA_MON) and SYS keep logging in, and no table, row or password is
touched.  The inverse is a single statement (`ACCOUNT UNLOCK`), which is why this
is a profile of its own rather than a mode bolted onto `db.table_readonly` (one
table) or `app.control` (a control row the services read).

F54-R reconstructs Google 2020-12-14 (status.cloud.google.com/incident/zall/20013):
automated quota management cut one service's quota below what it really used, and
the service's legitimate requests were refused.  Here the limit is an Oracle
resource profile on the same application account: a new profile whose
LOGICAL_READS_PER_CALL sits below the heaviest call the services make (the
transfer history page count, about 50k to 200k logical reads per execute) is
assigned to BANKING.  Oracle reads a session's limits at login, so -- exactly
like the lock -- the sessions already open keep working and the damage arrives
as the Hikari pools replace them; from then on every call over the cap is cut
with ORA-02395 while the session itself stays usable and every light call
succeeds.  Undoing it is two statements (back to the DEFAULT profile, drop the
new one); sessions opened under the limit keep it until the pools retire them.

F54-P is the same Google 2020-12-14 quota cut on the food MySQL application
account, and closer to the original, whose quota was on the account database's
writes: `ALTER USER 'fooddelivery'@'%' WITH MAX_UPDATES_PER_HOUR 500`, far below
the tens of thousands of writes an hour the food services make.  MySQL attaches
the per-hour counters to a connection at login, so -- like the Oracle profile --
connections already open are not counted and the damage arrives as the Hikari
pools replace them; the new connections share one counter, and once it passes
the cap every INSERT, UPDATE and DELETE on them is refused with 1226 ("has
exceeded the 'max_updates' resource") while SELECT, SET, COMMIT and ping keep
working.  Setting the limit back to 0 lifts it at once for every connection
(verified on a local mysql:8.0.46).

F71-R reconstructs GitHub 2025-03 (availability report, March 2025): a credential an
internal service used expired because the alerting for upcoming expirations was
broken, and the service failed until the credential was renewed.  Here the
credential is the password of the commerce PostgreSQL application role
`commerce`, which every commerce service logs in with over the network
(pg_hba `host all all all scram-sha-256`).  The expiry date is reached:
`ALTER ROLE commerce VALID UNTIL <now>`.  PostgreSQL checks the date only during
password authentication, so -- like the lock and the limits above -- the sessions
already open keep working, and from then on every new network login is refused
with "password authentication failed for user" (the server log alone adds "has
an expired password").  The damage arrives as each Hikari pool retires its
connections (max-lifetime 10 min for user, cart, pricing, shipping, the 30 min
default for product, order, payment, inventory) and cannot replace them.  Local
socket logins (trust) are not password checks, so this executor and the
database's own tooling keep working; the DPM account lucida_mon is a different
role.  Renewing is a single statement (`VALID UNTIL 'infinity'`, no expiry), and
the pools refill on their own (verified on a local postgres:16-alpine).

F71-H reconstructs Harness 2024-09-01 (status.harness.io/incidents/bs6qp18g8l21) at the
same role's login path: an access rule "that was previously too permissive" was
tightened and "did not account for" the address one client really comes from, and that
client kept working on its persistent connections until it had to reconnect.  Here the
rule is the commerce PostgreSQL host-based access file: the catch-all network rule
(`host all all all scram-sha-256`, any address) is replaced by the node address that
NodePort clients are SNATed to (10.244.3.1: the DB monitor, the runner) and the host and
management networks, and the configuration is reloaded.  The commerce services connect
from their own pod addresses on the same node (10.244.3.0/24) and match no rule any more.
PostgreSQL reads the file only when a connection starts, so -- like the expiry above --
open sessions keep working and every new network login is refused with
`no pg_hba.conf entry for host "10.244.3.x", user "commerce", database "commerce", no
encryption`, on the client and in the server log alike; the pools drain on the same
10 / 30 minute lifetimes.  The role, its password and the other rules stay as they are.
Restoring is the inverse edit and a reload (verified on a local postgres:16-alpine:
an open session kept answering after the reload, new logins from the omitted network
were refused, the original line brought them back, and the file kept its owner).
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, profile_instance

PROFILE_ID = "db.account"

# Only the banking (Oracle), food (MySQL) and commerce (PostgreSQL) application
# accounts.  Widening this dict is a design change: LUCIDA_MON / lucida_mon is the
# observer, and limiting it would blind DPM instead of breaking the services.
CONTRACTS: dict[str, dict[str, Any]] = {
    "F35-R": {
        "engine": "oracle",
        "namespace": "rca-testbed-banking",
        "pod": "testbed-oracle-0",
        "pdb": "FREEPDB1",
        "account": "BANKING",
    },
    # The cap is 30000 logical reads per call: the outbox relay poll (about 10k,
    # 7-day max 10.5k) and every OLTP statement stay under it, the history page
    # count (7-day minimum about 46k) and the ledger reconciliation sums (about
    # 146k) do not.  The profile name carries no scenario id.
    "F54-R": {
        "engine": "oracle",
        "namespace": "rca-testbed-banking",
        "pod": "testbed-oracle-0",
        "pdb": "FREEPDB1",
        "account": "BANKING",
        "profile": "APP_CALL_LIMITS",
        "resource": "LOGICAL_READS_PER_CALL",
        "limit": 30000,
    },
    # 500 updates an hour: the food services make about 59k at the 10:15 UTC
    # sample (performance_schema per-user insert 597, update 360, delete 26 a
    # minute) and DPM's 7-day 1st percentile is still about 130 a minute, so the
    # cap is spent within minutes of the first capped connections at any hour.
    # Only MAX_UPDATES_PER_HOUR: MAX_QUERIES_PER_HOUR would also refuse the
    # driver's connection set-up and the health checks and empty the pools.
    "F54-P": {
        "engine": "mysql",
        "namespace": "rca-testbed-food",
        "pod": "testbed-mysql-0",
        "account": "fooddelivery",
        "host": "%",
        "resource": "MAX_UPDATES_PER_HOUR",
        "limit": 500,
    },
    # The password of the role every commerce service logs in with reaches its
    # expiry date.  Only the date changes: the password, the role's other
    # attributes and the open sessions stay as they are.
    "F71-R": {
        "engine": "postgresql",
        "namespace": "rca-testbed-commerce",
        "pod": "testbed-postgres-0",
        "account": "commerce",
        "change": "password-valid-until-now",
    },
    # 2026-10-12. The catch-all network rule the role logs in through is narrowed to the
    # node address and the host and management networks. 2026-10-12 109 measurement: the
    # file's only network rule for non-local addresses is line 128 `host all all all
    # scram-sha-256`; the role's sessions come from the eight service pods 10.244.3.81, .82,
    # .83, .84, .88, .92, .95, .210 (56 sessions), and tb-w1 conntrack shows the NodePort
    # 30432 clients (119 DPM 192.168.230.128, 109) reaching the pod as 10.244.3.1.
    "F71-H": {
        "engine": "postgresql",
        "namespace": "rca-testbed-commerce",
        "pod": "testbed-postgres-0",
        "account": "commerce",
        "change": "host-rule-narrowed",
        "allowed_sources": ["10.244.3.1/32", "192.168.122.0/24", "192.168.200.0/24", "192.168.230.0/24"],
        "omitted_cidr": "10.244.3.0/24",
    },
}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    del profile
    expected = CONTRACTS.get(scenario_id)
    if expected is None:
        raise ExecutorError("scenario has no verified database account contract")
    if params != expected:
        raise ExecutorError("parameters do not exactly match the verified account contract")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    scenario_id = plan["scenario"]["id"]
    validate(scenario_id, p, {})
    # The scenario id stays on this side of kubectl: nothing that reaches the
    # database carries it.
    if p["engine"] == "mysql":
        argv = [
            "/usr/bin/bash", "-s", "--", action,
            p["namespace"], p["pod"], p["account"], p["host"], p["resource"], str(p["limit"]),
        ]
        return argv, MYSQL_LIMIT_REMOTE
    if p["engine"] == "postgresql":
        argv = [
            "/usr/bin/bash", "-s", "--", action,
            p["namespace"], p["pod"], p["account"],
        ]
        if p["change"] == "host-rule-narrowed":
            return argv + [",".join(p["allowed_sources"]), p["omitted_cidr"]], POSTGRES_HBA_REMOTE
        return argv, POSTGRES_EXPIRY_REMOTE
    argv = [
        "/usr/bin/bash", "-s", "--", action,
        p["namespace"], p["pod"], p["pdb"], p["account"],
    ]
    if "profile" in p:
        argv += [p["profile"], p["resource"], str(p["limit"])]
        return argv, ORACLE_PROFILE_REMOTE
    return argv, ORACLE_REMOTE


ORACLE_REMOTE = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; ns="$2"; pod="$3"; pdb="$4"; account="$5"
k=(kubectl --kubeconfig /root/tb-kubeconfig -n "$ns")

# DBA_USERS.ACCOUNT_STATUS is the authority. Never trust the ALTER's exit status
# alone: locking an already locked account also succeeds.
state() {
  # feedback off first, or "Session altered." folds into the value (see
  # db_table_readonly_executor).
  printf 'set pages 0 feedback off heading off\nalter session set container=%s;\nselect account_status from dba_users where username='"'"'%s'"'"';\nexit;\n' \
    "$pdb" "$account" | "${k[@]}" exec -i "$pod" -- sqlplus -s / as sysdba | tr -d '[:space:]'
}
flip() {
  printf 'alter session set container=%s;\nalter user %s account %s;\nexit;\n' \
    "$pdb" "$account" "$1" | "${k[@]}" exec -i "$pod" -- sqlplus -s / as sysdba >/dev/null
}
expect() { [[ "$(state)" == "$1" ]]; }

case "$action" in
  preflight)
    # The account must exist and be plainly OPEN, or the run would "succeed"
    # against an account an earlier, uncleaned run left locked.
    expect OPEN
    ;;
  run)
    expect OPEN
    flip lock
    expect LOCKED
    ;;
  cleanup)
    flip unlock
    expect OPEN
    ;;
  recovery)
    # Logins work again; the pools refill on their own and readiness follows.
    expect OPEN
    ;;
  *) exit 2 ;;
esac
'''


ORACLE_PROFILE_REMOTE = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; ns="$2"; pod="$3"; pdb="$4"; account="$5"
profile="$6"; resource="$7"; limit="$8"
k=(kubectl --kubeconfig /root/tb-kubeconfig -n "$ns")
q="'"

# One line, three facts: the account's profile, the limit the named profile sets
# (NONE when the profile does not exist), and whether Oracle enforces kernel
# limits at all (RESOURCE_LIMIT). The dictionary is the authority, not the exit
# status of the DDL: sqlplus -s exits 0 on ORA errors.
state() {
  printf '%s\n' 'set pages 0 feedback off heading off' \
    "alter session set container=$pdb;" \
    "select u.profile||'|'||nvl((select p.limit from dba_profiles p where p.profile=$q$profile$q and p.resource_name=$q$resource$q),'NONE')||'|'||(select upper(value) from v\$parameter where name='resource_limit') from dba_users u where u.username=$q$account$q;" \
    'exit;' | "${k[@]}" exec -i "$pod" -- sqlplus -s / as sysdba | tr -d '[:space:]'
}
sql() {
  printf '%s\n' "alter session set container=$pdb;" "$@" 'exit;' \
    | "${k[@]}" exec -i "$pod" -- sqlplus -s / as sysdba >/dev/null
}
expect() { [[ "$(state)" == "$1" ]]; }

case "$action" in
  preflight)
    # Plain DEFAULT, no leftover profile from an uncleaned run, limits enforced.
    expect "DEFAULT|NONE|TRUE"
    ;;
  run)
    expect "DEFAULT|NONE|TRUE"
    sql "create profile $profile limit $resource $limit;" "alter user $account profile $profile;"
    expect "$profile|$limit|TRUE"
    ;;
  cleanup)
    # Idempotent: back to DEFAULT first (a profile still assigned cannot be
    # dropped without CASCADE), then drop the profile only if it is there.
    sql "alter user $account profile default;"
    if [[ "$(state)" != "DEFAULT|NONE|TRUE" ]]; then
      sql "drop profile $profile;"
    fi
    expect "DEFAULT|NONE|TRUE"
    ;;
  recovery)
    # The dictionary is clean; sessions opened under the cap keep it until the
    # pools retire them (max-lifetime 10 min), which the controller waits for.
    expect "DEFAULT|NONE|TRUE"
    ;;
  *) exit 2 ;;
esac
'''


MYSQL_LIMIT_REMOTE = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; ns="$2"; pod="$3"; account="$4"; host="$5"; resource="$6"; limit="$7"
k=(kubectl --kubeconfig /root/tb-kubeconfig -n "$ns")
case "$resource" in
  MAX_UPDATES_PER_HOUR) ;;
  *) exit 2 ;;
esac
# The first MySQL error line goes to stderr so a failed change is readable in the runner log.
mysql() { "${k[@]}" exec "$pod" -- env SQL="$1" sh -lc 'e=$(mktemp); mysql -N -uroot -p"$MYSQL_ROOT_PASSWORD" -e "$SQL" 2>"$e"; rc=$?; grep -v "Using a password" "$e" | head -n 1 >&2; rm -f "$e"; exit $rc'; }

# mysql.user is the authority: the account's four resource limits in one line
# (updates|questions|connections per hour|user connections). Empty when the
# account does not exist, so every expect fails. Never trust the ALTER's exit
# status alone: setting the same limit again also succeeds.
state() {
  mysql "SELECT CONCAT_WS('|', max_updates, max_questions, max_connections, max_user_connections) FROM mysql.user WHERE user='$account' AND host='$host';" | tr -d '[:space:]'
}
expect() { [[ "$(state)" == "$1" ]]; }
cap() { mysql "ALTER USER '$account'@'$host' WITH $resource $1;" >/dev/null; }

case "$action" in
  preflight)
    # No limit of any kind on the account, or the run would "succeed" against
    # an account an earlier, uncleaned run left capped.
    expect "0|0|0|0"
    ;;
  run)
    expect "0|0|0|0"
    cap "$limit"
    expect "$limit|0|0|0"
    ;;
  cleanup)
    # 0 means no limit; it applies at once to the connections already capped.
    cap 0
    expect "0|0|0|0"
    ;;
  recovery)
    expect "0|0|0|0"
    ;;
  *) exit 2 ;;
esac
'''


POSTGRES_EXPIRY_REMOTE = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; ns="$2"; pod="$3"; account="$4"
k=(kubectl --kubeconfig /root/tb-kubeconfig -n "$ns")

# SQL travels in the environment over the pod's local socket (trust, not a password
# login, so the expiry never locks this executor out). pg_stat_statements does not
# record these sessions, so nothing here reaches Top SQL. The first psql error line
# goes to stderr for the runner log.
sql() { "${k[@]}" exec "$pod" -- env SQL="$1" PGOPTIONS="-c pg_stat_statements.track=none" sh -lc 'e=$(mktemp); psql -X -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$SQL" 2>"$e"; rc=$?; head -n 1 "$e" >&2; rm -f "$e"; exit $rc'; }

# pg_roles and pg_hba_file_rules are the authority, never the exit status of the
# ALTER (setting the same date again also succeeds): whether the role's password
# has an expiry and whether it has passed, whether the role may log in, and the
# method of the network rule the services match. Empty when the role does not exist.
state() {
  sql "SELECT CASE WHEN r.rolvaliduntil IS NULL OR r.rolvaliduntil = 'infinity' THEN 'NONE' WHEN r.rolvaliduntil <= now() THEN 'EXPIRED' ELSE 'FUTURE' END || '|' || r.rolcanlogin::text || '|' || coalesce((SELECT h.auth_method FROM pg_hba_file_rules h WHERE h.type = 'host' AND h.address = 'all' ORDER BY h.line_number LIMIT 1), 'none') FROM pg_roles r WHERE r.rolname = '$account'" | tr -d '[:space:]'
}
expect() { [[ "$(state)" == "$1" ]]; }

# A network login with the role's own password, the way the services log in: ok
# or refused. The password stays inside the pod.
netlogin() {
  "${k[@]}" exec "$pod" -- sh -lc 'ip=$(hostname -i | awk "{print \$1}"); case "$ip" in ""|127.*|::1) echo noip; exit 0 ;; esac; if PGPASSWORD="$POSTGRES_PASSWORD" PGCONNECT_TIMEOUT=5 psql -X -At -h "$ip" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select 1" >/dev/null 2>&1; then echo ok; else echo refused; fi' | tr -d '[:space:]'
}

case "$action" in
  preflight)
    # No expiry, login allowed, the network rule checks the password, and the
    # services' kind of login works -- or the run would "succeed" against a role
    # an earlier, uncleaned run left expired.
    expect "NONE|true|scram-sha-256"
    [[ "$(netlogin)" == ok ]]
    ;;
  run)
    expect "NONE|true|scram-sha-256"
    now=$(sql "SELECT to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.US') || '+00'")
    sql "ALTER ROLE \"$account\" VALID UNTIL '$now'" >/dev/null
    expect "EXPIRED|true|scram-sha-256"
    [[ "$(netlogin)" == refused ]]
    ;;
  cleanup)
    # 'infinity' is PostgreSQL's "no expiry"; the pools refill on their own.
    sql "ALTER ROLE \"$account\" VALID UNTIL 'infinity'" >/dev/null
    expect "NONE|true|scram-sha-256"
    [[ "$(netlogin)" == ok ]]
    ;;
  recovery)
    expect "NONE|true|scram-sha-256"
    ;;
  *) exit 2 ;;
esac
'''


POSTGRES_HBA_REMOTE = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; ns="$2"; pod="$3"; account="$4"; allowed="$5"; omitted="$6"
k=(kubectl --kubeconfig /root/tb-kubeconfig -n "$ns")

# SQL travels in the environment over the pod's local socket (trust: the local rules
# are never edited, so the narrowed file never locks this executor out). pg_stat_statements
# does not record these sessions. The first psql error line goes to stderr for the runner log.
sql() { "${k[@]}" exec "$pod" -- env SQL="$1" PGOPTIONS="-c pg_stat_statements.track=none" sh -lc 'e=$(mktemp); psql -X -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$SQL" 2>"$e"; rc=$?; head -n 1 "$e" >&2; rm -f "$e"; exit $rc'; }

# The narrowed rules, one per allowed source, and the same sources as the
# (address, netmask) pairs pg_hba_file_rules reports.
mask() { local b=$1 m=0 i; for ((i = 0; i < 32; i++)); do (( i < b )) && m=$(( m | (1 << (31 - i)) )); done
  echo "$(( (m >> 24) & 255 )).$(( (m >> 16) & 255 )).$(( (m >> 8) & 255 )).$(( m & 255 ))"; }
rules=""; pairs=""; nets=""
for src in ${allowed//,/ }; do
  rules+="host all all $src scram-sha-256"$'\n'
  pairs+="${pairs:+,}('${src%/*}','$(mask "${src#*/}")')"
  nets+="${nets:+,}'$src'"
done
n=$(grep -c . <<<"$rules")

# pg_hba_file_rules (the file as PostgreSQL parses it) is the authority, never the exit
# status of the edit: how many catch-all network rules (any address, any role), how many
# of the narrowed rules, and how many lines PostgreSQL cannot parse.
state() {
  sql "SELECT count(*) FILTER (WHERE type = 'host' AND database = ARRAY['all'] AND user_name = ARRAY['all'] AND address = 'all' AND auth_method = 'scram-sha-256') || '|' || count(*) FILTER (WHERE type = 'host' AND database = ARRAY['all'] AND user_name = ARRAY['all'] AND auth_method = 'scram-sha-256' AND (address, netmask) IN (VALUES $pairs)) || '|' || count(*) FILTER (WHERE error IS NOT NULL) FROM pg_hba_file_rules" | tr -d '[:space:]'
}
expect() { [[ "$(state)" == "$1" ]]; }

# The change only bites when the role's sessions really come from the omitted network
# and no allowed source covers them: count those sessions (>0) and any session from
# elsewhere that the narrowed file would also refuse (0).
bites() {
  [[ "$(sql "SELECT count(*) FILTER (WHERE client_addr <<= '$omitted'::inet AND NOT client_addr <<= ANY (ARRAY[$nets]::inet[])) > 0 AND count(*) FILTER (WHERE NOT client_addr <<= '$omitted'::inet AND NOT client_addr <<= ANY (ARRAY[$nets]::inet[])) = 0 FROM pg_stat_activity WHERE usename = '$account' AND client_addr IS NOT NULL" | tr -d '[:space:]')" == t ]]
}

# A network login with the role's own password from the pod's address, which sits in the
# omitted network like the services: ok or refused. The password stays inside the pod.
netlogin() {
  "${k[@]}" exec "$pod" -- sh -lc 'ip=$(hostname -i | awk "{print \$1}"); case "$ip" in ""|127.*|::1) echo noip; exit 0 ;; esac; if PGPASSWORD="$POSTGRES_PASSWORD" PGCONNECT_TIMEOUT=5 psql -X -At -h "$ip" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select 1" >/dev/null 2>&1; then echo ok; else echo refused; fi' | tr -d '[:space:]'
}
# The pod's own address must sit in the omitted network, or netlogin would not stand
# for the services.
ip_int() { local a b c d; IFS=. read -r a b c d <<<"$1"; echo $(( (a << 24) | (b << 16) | (c << 8) | d )); }
in_cidr() {
  local ip=$1 net=${2%/*} bits=${2#*/} m
  m=$(( bits == 0 ? 0 : (0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF ))
  (( ($(ip_int "$ip") & m) == ($(ip_int "$net") & m) ))
}
inside() { local ip; ip=$("${k[@]}" exec "$pod" -- sh -c 'hostname -i' | awk '{print $1}'); [[ "$ip" =~ ^[0-9.]+$ ]] && in_cidr "$ip" "$omitted"; }

# Rewrite the file in place (cat into it keeps its owner and mode): narrow puts the rules
# where the catch-all line was; restore puts the catch-all back where the first narrowed
# rule is and drops the rest. Nothing else in the file moves.
edit() {
  local hba; hba=$(sql "SHOW hba_file")
  "${k[@]}" exec "$pod" -- env HBA="$hba" MODE="$1" RULES="$rules" sh -c '
    t=$(mktemp)
    if [ "$MODE" = narrow ]; then
      awk '"'"'/^[[:space:]]*host[[:space:]]+all[[:space:]]+all[[:space:]]+all[[:space:]]+scram-sha-256[[:space:]]*$/ { printf "%s", ENVIRON["RULES"]; next } { print }'"'"' "$HBA" >"$t"
    else
      awk '"'"'BEGIN { c = split(ENVIRON["RULES"], r, "\n"); for (i = 1; i <= c; i++) if (r[i] != "") ours[r[i]] = 1 }
           ($0 in ours) { if (!done) { print "host all all all scram-sha-256"; done = 1 } next } { print }'"'"' "$HBA" >"$t"
    fi
    rc=$?; [ "$rc" -eq 0 ] && { cat "$t" >"$HBA"; rc=$?; }; rm -f "$t"; exit "$rc"'
}
reload() { [[ "$(sql "SELECT pg_reload_conf()" | tr -d '[:space:]')" == t ]]; }
# The reload is a signal: wait until a new login sees the new rules.
until_login() { local i; for i in 1 2 3 4 5 6 7 8 9 10; do [[ "$(netlogin)" == "$1" ]] && return 0; sleep 1; done; return 1; }

case "$action" in
  preflight)
    # One catch-all rule, none of the narrowed rules, a clean file, the services' kind of
    # login works, and the role's sessions sit in the omitted network -- or the run would
    # "succeed" against a file an earlier, uncleaned run left narrowed.
    expect "1|0|0"
    inside
    bites
    [[ "$(netlogin)" == ok ]]
    ;;
  run)
    expect "1|0|0"
    inside
    bites
    edit narrow
    expect "0|$n|0"
    reload
    until_login refused
    ;;
  cleanup)
    # Idempotent: nothing to do when the catch-all is back and no narrowed rule is left.
    if ! expect "1|0|0"; then
      expect "0|$n|0"
      edit restore
    fi
    expect "1|0|0"
    reload
    until_login ok
    ;;
  recovery)
    expect "1|0|0"
    [[ "$(netlogin)" == ok ]]
    ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
