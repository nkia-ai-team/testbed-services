#!/bin/bash
# 라우터에서 큐 버림 보고(GT0 결정 4)를 진짜 snmpd 로 확인한다(설계 §10 T2 "큐 버림 보고" (1)~(4)).
# 정비 창에서만, 부하기를 켜기 전에 root 로 돈다. 하위 NIC 하나에 잠깐 qdisc 를 걸고 끝나면(실패해도) 지운다.
#   router-discards-selftest.sh <하위 NIC> <그 망 서버 주소>
# 결과는 PASS/FAIL 줄로만 낸다. syslog 로 보내지 않는다.
set -euo pipefail
dev="$1"
peer="$2"
oid_base=.1.3.6.1.2.1.2.2.1.19
index=$(cat "/sys/class/net/$dev/ifindex")
# 커뮤니티는 명령 인자가 아니라 0600 임시 snmp.conf 로 넘긴다(ps 에 보이지 않게).
snmpconf=$(mktemp -d)
( umask 077; awk '$1=="rocommunity" && $3=="127.0.0.1"{print "defVersion 2c"; print "defCommunity " $2; exit}' \
    /etc/snmp/snmpd.community.conf > "$snmpconf/snmp.conf" )
grep -q defCommunity "$snmpconf/snmp.conf" || { rm -rf "$snmpconf"; echo "FAIL 로컬 커뮤니티 줄 없음"; exit 1; }
export SNMPCONFPATH="$snmpconf"
cleanup() { tc qdisc del dev "$dev" root 2>/dev/null || true; rm -rf "$snmpconf"; }
trap cleanup EXIT
[[ "$(tc -j qdisc show dev "$dev" | python3 -c 'import json,sys; q=json.load(sys.stdin); print(any(x.get("root") and x["kind"] in ("tbf","prio","netem","htb") for x in q))')" == False ]] \
    || { echo "FAIL $dev root 에 이미 주입 qdisc 가 있음"; exit 1; }
get() { sleep 1.5; snmpget -Oqv 127.0.0.1 "$oid_base.$index"; }
drops() { tc -s -j qdisc show dev "$dev" | python3 -c "import json,sys; print({q['kind']: q['drops'] for q in json.load(sys.stdin)}.get('$1', 0))"; }
verdict=0
before=$(get)
# (1) 속도 제한 큐가 넘치면 넘친 수만큼 는다.
tc qdisc add dev "$dev" root tbf rate 200kbit burst 3000 limit 6000
ping -q -f -s 1400 -c 2000 -W 1 "$peer" >/dev/null 2>&1 || true
n=$(drops tbf)
after=$(get)
if [[ "$n" -gt 0 && $((after - before)) -eq "$n" ]]; then echo "PASS overflow drops=$n counter+=$((after - before))"; else echo "FAIL overflow drops=$n counter+=$((after - before))"; verdict=1; fi
# (3) qdisc 를 지워도 줄지 않는다.
tc qdisc del dev "$dev" root
kept=$(get)
if [[ "$kept" -ge "$after" ]]; then echo "PASS kept after delete $kept"; else echo "FAIL dropped after delete $after -> $kept"; verdict=1; fi
# (2) prio 아래 netem 무작위 손실은 더하지 않는다(TOS lowdelay 는 prio 0 번 띠로 간다).
tc qdisc add dev "$dev" root handle 1: prio
tc qdisc add dev "$dev" parent 1:1 handle 10: netem loss 100%
ping -q -Q 0x10 -i 0.01 -c 300 -W 1 "$peer" >/dev/null 2>&1 || true
lost=$(drops netem)
net=$(get)
if [[ "$lost" -gt 0 && "$net" -eq "$kept" ]]; then echo "PASS netem loss=$lost not counted"; else echo "FAIL netem loss=$lost counter $kept -> $net"; verdict=1; fi
tc qdisc del dev "$dev" root
# (4) 도우미를 죽여도 snmpd 는 응답한다(다음 요청 때 다시 띄운다).
pkill -f /usr/local/lib/ifdiscards/ifdiscards.py || true
if snmpget -t 3 -r 1 -Oqv 127.0.0.1 .1.3.6.1.2.1.1.3.0 >/dev/null && again=$(get) && [[ "$again" -ge "$net" ]]; then
    echo "PASS helper restart counter=$again"
else
    echo "FAIL snmpd stopped answering after helper kill"; verdict=1
fi
exit "$verdict"
