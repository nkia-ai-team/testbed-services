# 이 VM 이 묶음의 호스트가 맞는지 본다: 호스트 이름, NIC 이름과 주소(cloud-init 이 넣은 값).
[[ "$(hostname)" == "$HOST_NAME" ]] || fail "호스트 이름 $(hostname) != $HOST_NAME"
for pair in $NIC_EXPECT; do
    ifname=${pair%%=*}
    addr=${pair#*=}
    ip -4 -o addr show dev "$ifname" 2>/dev/null | grep -q " $addr/" || fail "$ifname 에 $addr 없음"
done
log "host $HOST_NAME nics ok"
