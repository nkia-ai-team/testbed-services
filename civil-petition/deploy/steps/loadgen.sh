# k6(기존 부하기와 같은 v0.57.0 바이너리) 와 상주 부하 유닛(설계 §5). 관제 서버로 아무것도 보내지 않는다.
verify_artifact k6
install -m 0755 "$BUNDLE/artifacts/k6" /usr/local/bin/k6
k6 version | grep -q 'v0.57.0' || fail "k6 판이 v0.57.0 이 아님"
id k6 >/dev/null 2>&1 || useradd --system --home-dir /var/lib/k6 --shell /usr/sbin/nologin k6
install -d -m 0755 "/opt/loadgen/$DOMAIN"
for name in script.js entrypoint.sh; do
    verify_artifact "$name"
    install -m 0644 "$BUNDLE/artifacts/$name" "/opt/loadgen/$DOMAIN/$name"
done
for name in loadgen_monitor.py baseline-publish.sh; do
    verify_artifact "$name"
    install -m 0644 "$BUNDLE/artifacts/$name" "/opt/loadgen/$name"
done
place_files loadgen
systemctl daemon-reload
systemctl enable loadgen.service >/dev/null
# 처음 띄우기는 적용 순서의 마지막(웹, WAS, DB 가 다 선 뒤)에 apply.sh --start-load 가 한다.
curl -fsS -m 5 -o /dev/null "$ENTRY_URL/api/health" || log "WARN 진입 주소 아직 응답 없음: $ENTRY_URL"
log "loadgen installed (not started)"
