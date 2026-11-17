# 이 서버의 syslog 를 관제 서버로 보낸다(설계 §2-4, T 만. S 는 이 단계가 없다).
# 전달 설정은 /etc/rsyslog.d/remote/ 에 놓이고 rsyslog.conf 끝 포함 줄이 모든 로컬 규칙 뒤에 읽는다.
# 검사 사본에서 먼저 보고(rsyslog 가 그 파일을 실제로 읽는지까지) 통과할 때만 놓는다(syslog 단계와 같다).
rsyslog_place_checked syslog-forward
systemctl restart rsyslog
log "syslog forward on"
