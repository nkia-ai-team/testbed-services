# nftables: 관리 NIC 보호(모든 서버), 라우터는 보호 표와 관제 흐름 notrack, 계층 사이 ACL(설계 §3-3, §3-5).
require_inventory_names
install -d -m 0755 /etc/nftables.d
place_files firewall
nft -c -f /etc/nftables.conf || fail "nftables 문법"
restart_enabled nftables
nft list table inet mgmtguard >/dev/null || fail "보호 표 없음"
log "firewall ok: $(nft list tables | tr '\n' ' ')"
