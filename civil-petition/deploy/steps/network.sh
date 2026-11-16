# 망 설정을 cloud-init 에서 이 묶음의 netplan 으로 옮긴다. 주소는 cloud-init 이 넣은 것과 같아 연결이 끊기지 않는다.
place_files network
rm -f /etc/netplan/50-cloud-init.yaml
netplan generate
netplan apply
sysctl --system >/dev/null
for pair in $NIC_EXPECT; do
    ifname=${pair%%=*}
    addr=${pair#*=}
    ip -4 -o addr show dev "$ifname" | grep -q " $addr/" || fail "netplan 뒤 $ifname 에 $addr 없음"
done
# 관리 NIC 에는 기본 경로가 없어야 한다(설계 §3-5 규칙 4).
if ip -4 route show default | grep -q "dev $MGMT_IF"; then fail "관리 NIC 에 기본 경로가 있음"; fi
log "network ok"
