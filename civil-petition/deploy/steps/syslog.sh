# rsyslog 첫 규칙(실행기 배관 줄을 로컬 파일로)과 uid 속성(설계 §6-2). 전달은 다음 단계(syslog-forward)가 켠다.
install -d -m 0755 /etc/rsyslog.remote.d
place_files syslog
rsyslogd -N1 >/dev/null 2>&1 || { rsyslogd -N1; fail "rsyslog 설정 검사"; }
touch /var/log/opsjobs.log && chown syslog:adm /var/log/opsjobs.log && chmod 0640 /var/log/opsjobs.log
systemctl restart rsyslog
# ParseTrusted 를 켜도 본문에 _UID= 같은 꼬리가 붙지 않는지 본다(붙으면 모든 줄의 모양이 바뀐다).
logger -t rsyslog-check "annotation format check"
sleep 1
line=$(grep "rsyslog-check" /var/log/syslog | tail -1)
[[ -n "$line" ]] || fail "rsyslog 검사 줄이 /var/log/syslog 에 없음"
[[ "$line" != *"_UID="* && "$line" != *"@["* ]] || fail "본문에 자격 증명 꼬리가 붙음: $line"
log "rsyslog ok"
