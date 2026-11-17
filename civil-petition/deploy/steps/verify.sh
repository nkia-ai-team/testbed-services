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
check rsyslog-no-apparmor-denial rsyslog_not_denied
check rsyslog-no-proc-audit rsyslog_no_proc_denial
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
