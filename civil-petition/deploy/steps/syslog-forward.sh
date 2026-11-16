# 이 서버의 syslog 를 관제 서버로 보낸다(설계 §2-4, T 만. S 는 이 단계가 없다).
place_files syslog-forward
rsyslogd -N1 >/dev/null 2>&1 || { rsyslogd -N1; fail "rsyslog 설정 검사"; }
systemctl restart rsyslog
log "syslog forward on"
