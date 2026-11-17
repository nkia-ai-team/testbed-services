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
first_rule_present() { [[ -s /etc/rsyslog.d/00-opsjobs.conf ]] && grep -q "$EXECUTOR_NAME" /etc/rsyslog.d/00-opsjobs.conf; }
forward_state_ok() {
    if [[ -s /etc/rsyslog.remote.d/10-forward.conf ]]; then [[ "$SYSLOG_FORWARD" == 1 ]]; else [[ "$SYSLOG_FORWARD" == 0 ]]; fi
}
ip_forward_ok() {
    local want=0
    [[ "$HOST_ROLE" == router ]] && want=1
    [[ "$(sysctl -n net.ipv4.ip_forward)" == "$want" ]]
}
# NIC 가 인벤토리 이름, network 단계가 적은 MAC, 인벤토리 주소를 가지고 남은 물리 NIC 가 없다(lib.sh).
nic_names_ok() { [[ -z "$(nic_named_problem)" ]]; }
no_rollback_pending() { [[ "$(rollback_state)" == clear ]]; }
no_dpm_traces() {
    ! grep -Eq "^[^#].*$MONITOR_ADDRESS" /etc/postgresql/16/main/pg_hba.conf \
        && ! runuser -u postgres -- psql -Atqc "select 1 from pg_roles where rolname ilike '%dpm%' or rolname ilike '%monitor%'" | grep -q 1
}

check nic-names nic_names_ok
check no-network-rollback-pending no_rollback_pending
check apt-timers-off no_apt_timers
check no-javaagent no_javaagent
check no-otel-env no_otel_env
check chrony-offset-under-50ms chrony_offset_ok
check rsyslog-first-rule first_rule_present
check syslog-forward-matches-inventory forward_state_ok
check ip-forward ip_forward_ok
check mgmt-guard nft list table inet mgmtguard
[[ -n "$MODULES" ]] && check kernel-modules modules_loaded
case "$HOST_ROLE" in
    router)
        check snmpd-active systemctl is-active snmpd
        check fwacl nft list table inet fwacl
        check monitor-notrack nft list table inet monraw ;;
    web) check apache2-active systemctl is-active apache2 ;;
    was)
        check tomcat10-active systemctl is-active tomcat10
        check war-checksum sh -c "cd /var/lib/tomcat10/webapps && sha256sum -c --quiet /var/lib/petition-deploy/war.sha256"
        if [[ "$BATCH" == 1 ]]; then check batch-timers systemctl is-enabled petition-stats.timer petition-archive.timer; fi ;;
    db)
        check postgresql-active systemctl is-active postgresql@16-main
        check data-on-data-disk sh -c "runuser -u postgres -- psql -Atqc 'show data_directory' | grep -q '^$DATA_MOUNT/'"
        check no-dpm-traces no_dpm_traces
        check backup-timer systemctl is-enabled pg-backup.timer
        check table-stats-timer systemctl is-enabled pg-table-stats.timer ;;
    loadgen) check loadgen-enabled systemctl is-enabled loadgen.service ;;
esac
[[ "$failures" == 0 ]] || fail "점검 실패 $failures 건"
log "verify ok"
