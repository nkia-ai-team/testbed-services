place_files time
restart_enabled chrony
# 막 다시 띄운 chrony 가 서버를 고를 때까지 다시 읽는다(settle, lib.sh). 상한은 종전 고리(2초 간격 30번)와 같은 60초.
# 실패하면 마지막 sources 출력이 문구에 붙는다.
chrony_selected() {
    local out
    out=$(chronyc -n sources 2>&1) || { printf '%s\n' "$out"; return 1; }
    printf '%s\n' "$out"
    grep -q "^\^\* $CHRONY_SERVER" <<<"$out"
}
settle 60 2 "chrony 가 $CHRONY_SERVER 에 붙지 않음" chrony_selected
log "chrony ok: $(chronyc tracking | awk -F': ' '/System time/{print $2}')"
