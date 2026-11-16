place_files time
restart_enabled chrony
for _ in $(seq 1 30); do
    chronyc -n sources 2>/dev/null | grep -q "^\^\* $CHRONY_SERVER" && break
    sleep 2
done
chronyc -n sources | grep -q "^\^\* $CHRONY_SERVER" || fail "chrony 가 $CHRONY_SERVER 에 붙지 않음"
log "chrony ok: $(chronyc tracking | awk -F': ' '/System time/{print $2}')"
