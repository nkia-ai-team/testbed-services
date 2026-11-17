# Apache httpd 2.4 event MPM, mod_proxy_balancer(설계 §2-3 웹).
place_files web
a2enmod -q proxy proxy_http proxy_balancer lbmethod_byrequests slotmem_shm headers >/dev/null
a2dissite -q 000-default >/dev/null || true
a2ensite -q petition >/dev/null
apache2ctl configtest
restart_enabled apache2
# 막 다시 띄운 apache2 가 답할 때까지 다시 읽는다(settle, lib.sh).
settle "$SETTLE_SEC" "$SETTLE_EVERY" "정적 첫 화면 응답 없음" curl -fsS -m 5 -o /dev/null "http://127.0.0.1:${WEB_PORT}/"
log "web ok"
