# LLDP(설계 §4). 라우터는 SNMP AgentX 하위 에이전트로 붙는다(-x).
require_inventory_names
apt_install lldpd
place_files lldpd
restart_enabled lldpd
log "lldpd ok"
