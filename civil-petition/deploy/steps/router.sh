# SNMP 에이전트, ifOutDiscards 도우미, 링크 trap(설계 §4 rt-core01, §5 큐 버림 보고).
install -d -m 0755 /usr/local/lib/ifdiscards /etc/systemd/system/snmpd.service.d
place_files router
systemd-tmpfiles --create /etc/tmpfiles.d/ifdiscards.conf
python3 /usr/local/lib/ifdiscards/ifdiscards.py --config /etc/snmp/ifdiscards.json --dump >/dev/null \
    || fail "ifdiscards 도우미가 표를 만들지 못함"
restart_enabled snmpd
community=$(read_secret snmp_community)
for _ in $(seq 1 15); do
    snmpget -v2c -c "$community" -On 127.0.0.1 .1.3.6.1.2.1.1.5.0 >/dev/null 2>&1 && break
    sleep 1
done
snmpget -v2c -c "$community" -On 127.0.0.1 .1.3.6.1.2.1.1.5.0 >/dev/null 2>&1 || fail "snmpd 응답 없음"
for ifname in $ROUTER_AUGMENT; do
    index=$(cat "/sys/class/net/$ifname/ifindex")
    snmpget -v2c -c "$community" -On 127.0.0.1 ".1.3.6.1.2.1.2.2.1.19.$index" | grep -q Counter32 \
        || fail "$ifname ifOutDiscards 응답 없음"
    snmpget -v2c -c "$community" -On 127.0.0.1 ".1.3.6.1.2.1.31.1.1.1.15.$index" | grep -q 'Gauge32: 1000' \
        || fail "$ifname ifHighSpeed 가 1000 이 아님"
done
unset community
log "router snmp ok"
