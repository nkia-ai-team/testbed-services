# 망 설정을 cloud-init 에서 이 묶음의 netplan 으로 옮긴다(설계 §2-4). 주소는 cloud-init 이 넣은 것과 같다.
#
# NIC 이름: Proxmox cloud-init 은 NIC 를 MAC 으로 맞춰 eth0, eth1 ... 로 이름 짓는다. 이 묶음의 netplan 은 같은
# MAC 을 match 로 잡고 set-name 으로 인벤토리 이름(ens18 ...)을 준다. MAC 은 지금 인벤토리 주소를 가진 장치에서
# 읽어 NIC_MAC_FILE 에 적는다(lib.sh nic_resolve). 이름이 바뀌는 NIC 는 netplan apply 가 잠깐 내렸다 올린다
# (관리 NIC 포함). 다음 부팅부터는 netplan 이 만든 udev 이름 규칙이 같은 이름을 준다. cloud-init 은 os-base 가
# 꺼 두어 다음 부팅에 eth 이름과 자기 망 설정을 되살리지 않는다.
#
# 관리망이 끊기지 않게(끊겨도 돌아오게) 하는 순서:
#   1. 지금 /etc/netplan 의 사본을 둔다(처음 한 번은 cloud-init 설정을 따로 남긴다)
#   2. 되돌림 타이머를 건다: NET_ROLLBACK_SEC 초 뒤 사본으로 되돌린다(netplan-rollback)
#   3. 새 파일을 놓고 netplan generate 로 먼저 시험한다
#   4. netplan apply 를 세션과 떨어진 systemd 유닛으로 돌린다(세션이 끊겨도 반쯤 하다 멈추지 않는다)
#   5. 확인: NIC 마다 이름, MAC, 주소, 남은 물리 NIC 없음, 관리 NIC 로 66 관리 주소가 ARP 에 답함
#   6. 확인되면 타이머를 끈다. 확인이 안 되면 바로 되돌리고 실패한다.
#      세션이 끊겨 5, 6 에 닿지 못하면 타이머가 되돌린다(관리 접속은 cloud-init 설정으로 돌아온다).
state=${STATE_DIR:-/var/lib/petition-deploy}
netplan_dir=${NETPLAN_DIR:-/etc/netplan}
cloud_dir=${CLOUD_DIR:-/etc/cloud}
rollback_bin=${NETPLAN_ROLLBACK:-/usr/local/sbin/netplan-rollback}
backup="$state/netplan-before"
original="$state/netplan-cloud-init"
: "${NET_ROLLBACK_SEC:=300}"
: "${NET_SETTLE_SEC:=60}"
# netplan apply 가 걸리면(안의 udevadm settle 은 최대 120초) 이 시간에 끊어, 적용과 확인이 타이머 안에 끝나게 한다.
: "${NET_APPLY_MAX_SEC:=120}"
(( NET_ROLLBACK_SEC >= NET_APPLY_MAX_SEC + NET_SETTLE_SEC + 60 )) \
    || fail "NET_ROLLBACK_SEC($NET_ROLLBACK_SEC)는 적용 상한과 확인 시간의 합보다 60초 이상 길어야 함"

grep -qs 'config: disabled' "$cloud_dir/cloud.cfg.d/99-disable-network-config.cfg" \
    || fail "cloud-init 망 설정이 꺼져 있지 않음(os-base 를 먼저)"
[[ -e "$cloud_dir/cloud-init.disabled" ]] || fail "cloud-init 이 꺼져 있지 않음(os-base 를 먼저)"

# 앞 실행의 되돌림이 기다리거나 돌거나 실패한 채면 아무것도 바꾸지 않고 멈춘다. 다시 돌리면 사본이 지금 상태로
# 덮이고 기다리던 안전망이 꺼지기 때문이다.
case "$(rollback_state)" in
    pending) fail "앞 망 전환의 되돌림 타이머가 기다리는 중(NET_ROLLBACK_SEC 뒤 되돌림). 끝난 뒤 다시 돌린다" ;;
    running) fail "앞 망 전환의 되돌림이 도는 중. 끝난 뒤 다시 돌린다" ;;
    failed) fail "앞 망 전환의 되돌림이 실패한 채 남음. journalctl -u netplan-rollback 을 보고 systemctl reset-failed netplan-rollback.service 뒤 다시" ;;
esac

# NIC 를 인벤토리 주소로 찾아 MAC 을 적는다. 이미 적힌 MAC 과 다르면 멈춘다(NIC 가 바뀐 VM).
install -d -m 0755 "$state"
map=$(nic_resolve) || fail "인벤토리 주소를 가진 NIC 를 찾지 못함"
while read -r ifname dev mac; do
    old=$(awk -v n="$ifname" '$1 == n {print $2}' "$NIC_MAC_FILE" 2>/dev/null || true)
    [[ -z "$old" || "$old" == "$mac" ]] || fail "$ifname 의 MAC 이 $old 에서 $mac 로 바뀜"
done <<<"$map"
awk '{print $1, $3}' <<<"$map" > "$NIC_MAC_FILE.new"
mv -f "$NIC_MAC_FILE.new" "$NIC_MAC_FILE"
log "nic map: $(awk '{printf "%s=%s(%s) ", $1, $2, $3}' <<<"$map")"

# 1. 사본
shopt -s nullglob
current=("$netplan_dir"/*.yaml)
shopt -u nullglob
[[ ${#current[@]} -gt 0 ]] || fail "$netplan_dir 에 netplan 파일이 없음(되돌릴 사본을 둘 수 없음)"
rm -rf "$backup"
install -d -m 0700 "$backup"
cp -p "${current[@]}" "$backup"/
if [[ ! -d "$original" && -e "$netplan_dir/50-cloud-init.yaml" ]]; then
    install -d -m 0700 "$original"
    cp -p "$netplan_dir/50-cloud-init.yaml" "$original"/
fi

# 2. 되돌림 타이머. 앞에서 발화해 끝난 일시 타이머(elapsed)만 남아 있을 수 있어 그것을 치운다(위에서 기다림,
#    돎, 실패가 아님을 확인했다).
systemctl stop netplan-rollback.timer >/dev/null 2>&1 || true
# 되돌림 스크립트는 타이머보다 먼저 있어야 한다. 묶음의 network 파일 중 그것만 먼저 놓는다.
place_files network "$rollback_bin" || fail "되돌림 스크립트를 놓지 못함(아무것도 바꾸지 않음)"
[[ -x "$rollback_bin" ]] || fail "묶음에 되돌림 스크립트 $rollback_bin 이 없음"
systemd-run --quiet --unit=netplan-rollback --on-active="${NET_ROLLBACK_SEC}s" --timer-property=AccuracySec=1s \
    "$rollback_bin" || fail "되돌림 타이머를 걸지 못함(아무것도 바꾸지 않음)"
armed_at=$SECONDS

abort() {
    systemctl stop netplan-rollback.timer >/dev/null 2>&1 || true
    systemctl reset-failed netplan-rollback-now.service >/dev/null 2>&1 || true
    if systemd-run --quiet --wait --collect --unit=netplan-rollback-now -p Type=oneshot "$rollback_bin"; then
        fail "$1. 사본으로 되돌림"
    fi
    fail "$1. 되돌리기도 실패, Proxmox 콘솔에서 $rollback_bin 을 돌린다"
}

# 3. 새 파일, 생성 시험
place_files network || abort "파일을 놓지 못함"
rm -f "$netplan_dir/50-cloud-init.yaml"
netplan generate || abort "netplan generate 실패"

# 4. 적용
systemctl reset-failed netplan-switch.service >/dev/null 2>&1 || true
systemd-run --quiet --wait --collect --unit=netplan-switch -p Type=oneshot -p RuntimeMaxSec="$NET_APPLY_MAX_SEC" netplan apply \
    || abort "netplan apply 실패"

# 5. 확인
mgmt_problem() {
    if ip -4 route show default | grep -q "dev $MGMT_IF\( \|$\)"; then echo "관리 NIC 에 기본 경로가 있음"; return; fi
    ping -c 1 -W 1 -I "$MGMT_IF" "$MGMT_HOST" >/dev/null 2>&1 || true
    ip neigh show "$MGMT_HOST" dev "$MGMT_IF" | grep -q 'lladdr .*\(REACHABLE\|STALE\|DELAY\|PROBE\)' \
        || echo "관리 NIC $MGMT_IF 로 $MGMT_HOST 가 ARP 에 답하지 않음"
}
deadline=$((SECONDS + NET_SETTLE_SEC))
while :; do
    problem=$(nic_named_problem; mgmt_problem)
    [[ -n "$problem" ]] || break
    (( SECONDS < deadline )) || abort "netplan 적용 뒤 확인 실패($NET_SETTLE_SEC초): $(paste -sd, - <<<"$problem")"
    sleep 2
done

# 6. 확정. 타이머가 아직 기다리는 중이고 발화 시각에서 20초 넘게 떨어져 있을 때만 끈다. 이미 발화했으면
#    되돌림이 돌았거나 도는 중이라 "network ok" 를 내지 않는다.
state=$(rollback_state)
[[ "$state" == pending ]] || fail "확정 전에 되돌림이 발화함($state). 되돌림이 끝난 뒤 다시 돌린다"
(( SECONDS - armed_at < NET_ROLLBACK_SEC - 20 )) \
    || fail "확정이 되돌림 발화 시각에 너무 가까움. 되돌림이 끝난 뒤 다시 돌린다"
systemctl stop netplan-rollback.timer >/dev/null 2>&1 || true
state=$(rollback_state)
[[ "$state" == clear ]] || fail "되돌림 타이머를 끄지 못함($state). systemctl stop netplan-rollback.timer 를 손으로"
sysctl --system >/dev/null
log "network ok: $(awk '$1 != $2 {printf "%s->%s ", $2, $1}' <<<"$map")$(ip -br link | awk '$1 != "lo" {sub(/@.*/, "", $1); printf "%s ", $1}')"
