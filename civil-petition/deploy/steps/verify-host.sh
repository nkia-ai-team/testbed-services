# 이 VM 이 묶음의 호스트가 맞는지 본다: 호스트 이름, 인벤토리 주소마다 그 주소를 가진 NIC(cloud-init 이 넣은 값).
# NIC 이름은 여기서 보지 않는다. 처음 설치 때는 cloud-init 이름(eth0 ...)이고 network 단계가 인벤토리 이름으로 바꾼다.
[[ "$(hostname)" == "$HOST_NAME" ]] || fail "호스트 이름 $(hostname) != $HOST_NAME"
map=$(nic_resolve) || fail "인벤토리 주소를 가진 NIC 를 찾지 못함"
while read -r ifname dev mac; do
    log "nic $ifname = $dev $mac"
done <<<"$map"
log "host $HOST_NAME nics ok"
