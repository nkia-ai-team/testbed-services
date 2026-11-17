# SNMP 에이전트, ifOutDiscards 도우미, 링크 trap(설계 §4 rt-core01, §5 큐 버림 보고).
require_inventory_names
install -d -m 0755 /usr/local/lib/ifdiscards /etc/systemd/system/snmpd.service.d
place_files router
systemd-tmpfiles --create /etc/tmpfiles.d/ifdiscards.conf
python3 /usr/local/lib/ifdiscards/ifdiscards.py --config /etc/snmp/ifdiscards.json --dump >/dev/null \
    || fail "ifdiscards 도우미가 표를 만들지 못함"
restart_enabled snmpd
# 커뮤니티는 명령 인자(ps 에 보임)가 아니라 0600 임시 snmp.conf 로 넘긴다(리뷰 지적 11).
snmpconf=$(mktemp -d)
trap 'rm -rf "$snmpconf"' EXIT
( umask 077; printf 'defVersion 2c\ndefCommunity %s\n' "$(read_secret snmp_community)" > "$snmpconf/snmp.conf" )
export SNMPCONFPATH="$snmpconf"
for _ in $(seq 1 15); do
    snmpget -On 127.0.0.1 .1.3.6.1.2.1.1.5.0 >/dev/null 2>&1 && break
    sleep 1
done
snmpget -On 127.0.0.1 .1.3.6.1.2.1.1.5.0 >/dev/null 2>&1 || fail "snmpd 응답 없음"
for ifname in $ROUTER_AUGMENT; do
    index=$(cat "/sys/class/net/$ifname/ifindex")
    snmpget -On 127.0.0.1 ".1.3.6.1.2.1.2.2.1.19.$index" | grep -q Counter32 \
        || fail "$ifname ifOutDiscards 응답 없음"
    snmpget -On 127.0.0.1 ".1.3.6.1.2.1.31.1.1.1.15.$index" | grep -q 'Gauge32: 1000' \
        || fail "$ifname ifHighSpeed 가 1000 이 아님"
done
log "router snmp ok"
