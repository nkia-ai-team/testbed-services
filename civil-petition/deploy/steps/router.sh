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
# snmpd 는 막 섰을 때 응답, interface 지시의 속도 덮어쓰기, pass_persist 도우미의 열이 늦게 설 수 있어
# 값마다 설 때까지 다시 읽는다(settle, lib.sh). 실패 문구에는 마지막으로 본 snmpget 출력이 붙는다.
#   snmp_is <OID> <꼴(grep -E)>   값을 내고 꼴에 맞으면 0
snmp_is() {
    local out
    out=$(snmpget -On 127.0.0.1 "$1" 2>&1) || { printf '%s\n' "$out"; return 1; }
    printf '%s\n' "$out"
    grep -Eq -- "$2" <<<"$out"
}
settle "$SETTLE_SEC" "$SETTLE_EVERY" "snmpd 응답 없음" snmpget -On 127.0.0.1 .1.3.6.1.2.1.1.5.0
for ifname in $ROUTER_AUGMENT; do
    index=$(cat "$SYS_NET/$ifname/ifindex")
    settle "$SETTLE_SEC" "$SETTLE_EVERY" "$ifname ifOutDiscards 응답 없음" \
        snmp_is ".1.3.6.1.2.1.2.2.1.19.$index" '= Counter32: [0-9]+$'
    settle "$SETTLE_SEC" "$SETTLE_EVERY" "$ifname ifHighSpeed 가 1000 이 아님" \
        snmp_is ".1.3.6.1.2.1.31.1.1.1.15.$index" '= Gauge32: 1000$'
done
log "router snmp ok"
