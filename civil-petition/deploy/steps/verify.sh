# 설치 뒤 점검(설계 §10 T2 의 VM 안에서 볼 수 있는 항목). 하나라도 FAIL 이면 단계가 실패한다.
failures=0
check() {
    local name="$1"; shift
    if "$@" >/dev/null 2>&1; then
        log "CHECK $name PASS"
    else
        log "CHECK $name FAIL"
        failures=$((failures + 1))
    fi
}
no_apt_timers() { ! systemctl list-timers --all --no-legend | grep -q 'apt-'; }
no_javaagent() { ! ps -eo args= | grep -v grep | grep -q -- '-javaagent'; }
no_otel_env() {
    local f
    for f in /proc/[0-9]*/environ; do
        tr '\0' '\n' < "$f" 2>/dev/null | grep -q '^OTEL_' && return 1
    done
    ! grep -rqs 'OTEL_\|javaagent' /etc/default/tomcat10 /etc/systemd/system /etc/environment
}
modules_loaded() { local m; for m in $MODULES; do lsmod | grep -q "^$m " || return 1; done; for m in $MODULES; do grep -qx "$m" /etc/modules-load.d/qdisc.conf || return 1; done; }
chrony_offset_ok() {
    local offset
    offset=$(chronyc -c tracking | awk -F, '{print $5}')
    awk -v o="$offset" 'BEGIN{exit !(o < 0.05 && o > -0.05)}'
}
# rsyslog 설정은 파일이 있는 것이 아니라 rsyslog 가 실제로 읽는지 본다(include mode="optional" 은 AppArmor 가 막아도 오류가 아니다).
first_rule_present() {
    [[ -s "$RSYSLOG_ETC/rsyslog.d/00-opsjobs.conf" ]] && grep -q "$EXECUTOR_NAME" "$RSYSLOG_ETC/rsyslog.d/00-opsjobs.conf" \
        && rsyslog_reads "$RSYSLOG_ETC/rsyslog.conf" "$RSYSLOG_ETC/rsyslog.d/00-opsjobs.conf"
}
forward_state_ok() {
    local forward="$RSYSLOG_ETC/rsyslog.d/remote/10-forward.conf"
    if [[ "$SYSLOG_FORWARD" == 1 ]]; then
        [[ -s "$forward" ]] && rsyslog_reads "$RSYSLOG_ETC/rsyslog.conf" "$forward"
    else
        ! compgen -G "$RSYSLOG_ETC/rsyslog.d/remote/*.conf" >/dev/null
    fi
}
# 돌고 있는 rsyslog 가 마지막으로 선 뒤 AppArmor 가 rsyslogd 의 무엇을 막은 줄이 없다(커널 로그와 감사 기록 둘 다 본다).
# /proc 읽기 거부는 따로도 센다(로컬 소켓 주석의 부작용을 감사 없이 거부하는 조각이 먹는지).
rsyslog_not_denied() { local out; out=$(rsyslog_denials) && [[ -z "$out" ]]; }
rsyslog_no_proc_denial() { local out; out=$(rsyslog_denials "$RSYSLOG_PROC_DENIAL") && [[ -z "$out" ]]; }
ip_forward_ok() {
    local want=0
    [[ "$HOST_ROLE" == router ]] && want=1
    [[ "$(sysctl -n net.ipv4.ip_forward)" == "$want" ]]
}
# NIC 가 인벤토리 이름, network 단계가 적은 MAC, 인벤토리 주소를 가지고 남은 물리 NIC 가 없다(lib.sh).
nic_names_ok() { [[ -z "$(nic_named_problem)" ]]; }
no_rollback_pending() { [[ "$(rollback_state)" == clear ]]; }
# 올라온 nft 표에 줄이 모두 있다(표가 있는 것만으로는 틀이 바뀌어 빠진 줄을 모른다).
#   nft_table_has <표> <줄 조각>...   조각은 nft list 출력의 한 줄 안에 그대로 있어야 한다
nft_table_has() {
    local table="$1" listed piece
    shift
    listed=$(nft list table inet "$table") || return 1
    for piece in "$@"; do grep -qF -- "$piece" <<<"$listed" || return 1; done
}
# 관리 보호 표: 관리 주소에서 오는 SSH 만 받고, 그 SSH 는 conntrack 에서 빠진다(표가 가득 차도 정리 SSH 가 붙게).
mgmt_guard_ok() {
    nft_table_has mgmtguard \
        "iifname \"$MGMT_IF\" ip saddr $MGMT_HOST tcp dport 22 accept" \
        "iifname \"$MGMT_IF\" ip saddr $MGMT_HOST ip daddr $MGMT_ADDR tcp dport 22 notrack" \
        "oifname \"$MGMT_IF\" ip saddr $MGMT_ADDR ip daddr $MGMT_HOST tcp sport 22 notrack"
}
# 라우터 자신의 관제 흐름(SNMP, trap, syslog, ICMP) notrack 과 관리 대역 거절 카운터.
monitor_notrack_ok() {
    nft_table_has monraw "udp dport 161 notrack" "udp sport 161 notrack" "icmp type echo-request notrack" \
        "icmp type echo-reply notrack" \
        && nft list table inet mgmtguard | grep -Eq "iifname != \"$MGMT_IF\" ip daddr [0-9./]+ counter packets [0-9]+ bytes [0-9]+ drop"
}
# DPM 흔적이 없다(설계 §2-5, §4: DPM 등록 안 함).
#   pg_hba: 주석 아닌 줄의 칸이 관제 주소 그대로이거나 그 주소의 /N 인 줄(종전 정규식은 점이 아무 글자, 앞뒤 숫자도 맞았다).
#   역할: 로그인할 수 있고 이름에 dpm, monitor 가 든 역할. PostgreSQL 내장 역할(pg_ 로 시작, pg_monitor 등)은 늘 있고
#   로그인하지 못하므로 뺀다. 질의가 실패하면 "없음" 이 아니라 실패로 본다.
PG_HBA=${PG_HBA:-/etc/postgresql/16/main/pg_hba.conf}
hba_mentions_monitor() {
    awk -v a="$MONITOR_ADDRESS" '!/^[[:space:]]*#/ { for (i = 1; i <= NF; i++) if ($i == a || index($i, a "/") == 1) found = 1 }
        END { exit !found }' "$PG_HBA"
}
monitor_login_roles() {
    runuser -u postgres -- psql -X -Atq -v ON_ERROR_STOP=1 -c \
        "select rolname from pg_roles where rolcanlogin and rolname !~ '^pg_' and (rolname ilike '%dpm%' or rolname ilike '%monitor%') order by 1"
}
no_dpm_traces() {
    local roles
    [[ -r "$PG_HBA" ]] && ! hba_mentions_monitor && roles=$(monitor_login_roles) && [[ -z "$roles" ]]
}
# 데이터 디렉터리가 놓인 마운트(DATA_MOUNT)가 serial 로 찾은 그 디스크다(db 단계와 같은 disk_by_serial, lib.sh).
# LABEL 이 같은 다른 파일 시스템이 붙거나 마운트가 빠져 OS 디스크에 쓰는 것을 잡는다.
data_mount_on_serial_disk() {
    local want have
    want=$(disk_by_serial "$DATA_DISK_SERIAL") || return 1
    have=$(findmnt -n -o SOURCE --mountpoint "$DATA_MOUNT") || return 1
    [[ -n "$have" && "$(readlink -f "$have")" == "$(readlink -f "$want")" ]]
}

# SMS 에이전트(설계 §4, runbook §11): 도는 lucida-agent(root) 가 설치본 바이너리이고, 설치본은 sms-agent 단계가 적은 체크섬과
# 같으며, agent.env 의 이름과 보고 주소가 인벤토리 값이다.
sms_agent_binary_ok() {
    local bin="$SMS_AGENT_HOME/bin/lucida-agent" pid
    [[ -s "$SMS_AGENT_RECORD" ]] && [[ "$(sha256sum "$bin" | awk '{print $1}')" == "$(cat "$SMS_AGENT_RECORD")" ]] || return 1
    pid=$(pgrep -x -u 0 lucida-agent) && [[ $(wc -w <<<"$pid") == 1 ]] && [[ "$(readlink -f "/proc/$pid/exe")" == "$(readlink -f "$bin")" ]]
}
sms_agent_env_ok() {
    local env="$SMS_AGENT_HOME/agent.env"
    grep -qxF "LUCIDA_HOST=$SMS_AGENT_HOST" "$env" && grep -qxF "LUCIDA_SERVER=$SMS_SERVER_URL" "$env"
}

check nic-names nic_names_ok
check no-network-rollback-pending no_rollback_pending
check apt-timers-off no_apt_timers
check no-javaagent no_javaagent
check no-otel-env no_otel_env
check chrony-offset-under-50ms chrony_offset_ok
check rsyslog-first-rule first_rule_present
check syslog-forward-matches-inventory forward_state_ok
check rsyslog-no-apparmor-denial rsyslog_not_denied
check rsyslog-no-proc-audit rsyslog_no_proc_denial
check ip-forward ip_forward_ok
check mgmt-guard mgmt_guard_ok
[[ -n "$MODULES" ]] && check kernel-modules modules_loaded
case "$HOST_ROLE" in
    router)
        check snmpd-active systemctl is-active snmpd
        check fwacl nft list table inet fwacl
        check monitor-notrack monitor_notrack_ok ;;
    web) check apache2-active systemctl is-active apache2 ;;
    was)
        check tomcat10-active systemctl is-active tomcat10
        check war-checksum sh -c "cd /var/lib/tomcat10/webapps && sha256sum -c --quiet /var/lib/petition-deploy/war.sha256"
        if [[ "$BATCH" == 1 ]]; then check batch-timers systemctl is-enabled petition-stats.timer petition-archive.timer; fi ;;
    db)
        check postgresql-active systemctl is-active postgresql@16-main
        check data-on-data-disk sh -c "runuser -u postgres -- psql -Atqc 'show data_directory' | grep -q '^$DATA_MOUNT/'"
        check data-mount-on-serial-disk data_mount_on_serial_disk
        check no-dpm-traces no_dpm_traces
        check backup-timer systemctl is-enabled pg-backup.timer
        check table-stats-timer systemctl is-enabled pg-table-stats.timer ;;
    loadgen) check loadgen-enabled systemctl is-enabled loadgen.service ;;
esac
# SMS 에이전트는 관제 서버 등록 창(T3, S 는 ST3)에서 단다(runbook §11). 그 전(설치 기록도 자리도 없음)에는 세 항목을 보지 않고
# 그렇다고 한 줄 적는다. 기록이나 자리 중 하나라도 있으면(설치를 한 번이라도 했으면) 세 항목을 모두 본다.
if [[ -n "${SMS_AGENT_UNIT:-}" ]]; then
    if [[ -e "$SMS_AGENT_RECORD" || -e "$SMS_AGENT_HOME" ]]; then
        check sms-agent-active systemctl is-active "$SMS_AGENT_UNIT"
        check sms-agent-binary sms_agent_binary_ok
        check sms-agent-env sms_agent_env_ok
    else
        log "CHECK sms-agent NOT-INSTALLED(관제 서버 등록 창 전, runbook §11)"
    fi
fi
[[ "$failures" == 0 ]] || fail "점검 실패 $failures 건"
log "verify ok"
