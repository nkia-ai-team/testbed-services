# 설치 기록: 이 묶음이 놓은 파일의 sha256 을 절대 경로로 남긴다(설계 §6-4 "버전 표류", 러너 계약 (라), v4 T4S).
# 러너의 5분 평시 점검 읽기가 `cd / && sha256sum -c --quiet` 로 견주어 다르면 큐를 멈춘다(미끼 배포나 주입이 남긴 것).
# 묶음 전체를 돌린 끝에 install-host.sh 가 부르고, 이미 설치된 서버에는 `install-host.sh <묶음> record` 로 따로 돌린다.
# WAR 는 was 단계의 war.sha256 이 따로 맡는다.
record="${PLACE_ROOT:-}${EXPECTED_RECORD:-/var/lib/petition-deploy/expected.sha256}"
install -d -m 0755 "$(dirname "$record")"
tmp="$record.new"
: >"$tmp"
while IFS=$'\t' read -r _ dest _; do
    [[ -n "$dest" && -f "${PLACE_ROOT:-}$dest" ]] || continue
    printf '%s  %s\n' "$(sha256sum "${PLACE_ROOT:-}$dest" | cut -d' ' -f1)" "$dest" >>"$tmp"
done < <(sort -u -t$'\t' -k2,2 "$BUNDLE/files.tsv")
chmod 0644 "$tmp"
mv -f "$tmp" "$record"
log "record ok $(wc -l <"$record") files"
