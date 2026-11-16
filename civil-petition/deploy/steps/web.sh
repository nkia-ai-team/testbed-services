# Apache httpd 2.4 event MPM, mod_proxy_balancer(설계 §2-3 웹).
place_files web
a2enmod -q proxy proxy_http proxy_balancer lbmethod_byrequests slotmem_shm headers >/dev/null
a2dissite -q 000-default >/dev/null || true
a2ensite -q petition >/dev/null
apache2ctl configtest
restart_enabled apache2
curl -fsS -m 5 "http://127.0.0.1:${WEB_PORT}/" >/dev/null || fail "정적 첫 화면 응답 없음"
log "web ok"
