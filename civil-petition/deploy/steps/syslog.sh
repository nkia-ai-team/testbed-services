# rsyslog 첫 규칙(실행기 배관 줄을 로컬 파일로)과 uid 속성(설계 §6-2). 전달은 다음 단계(syslog-forward)가 켠다.
# 새 설정은 검사 사본에서 먼저 보고 통과할 때만 놓는다(못 읽을 설정이 남아 재시작, 재부팅 때 rsyslog 가 서지 못하는 일을 막는다).
install -d -m 0755 "$RSYSLOG_ETC/rsyslog.remote.d"
rsyslog_place_checked syslog
touch /var/log/opsjobs.log && chown syslog:adm /var/log/opsjobs.log && chmod 0640 /var/log/opsjobs.log
systemctl restart rsyslog
# ParseTrusted 를 켜도 본문에 _UID= 같은 꼬리가 붙지 않는지 본다(붙으면 모든 줄의 모양이 바뀐다).
# 검사 줄은 원격 전달이 켜지기 전(첫 설치)에만 남긴다. 전달이 이미 켜진 뒤 다시 돌릴 때는 관제 서버로 가므로 건너뛴다.
if compgen -G "$RSYSLOG_ETC/rsyslog.remote.d/*.conf" >/dev/null; then
    log "rsyslog ok (전달이 켜져 있어 검사 줄 생략)"
else
    logger -t rsyslog-check "annotation format check"
    sleep 1
    line=$(grep "rsyslog-check" /var/log/syslog | tail -1)
    [[ -n "$line" ]] || fail "rsyslog 검사 줄이 /var/log/syslog 에 없음"
    [[ "$line" != *"_UID="* && "$line" != *"@["* ]] || fail "본문에 자격 증명 꼬리가 붙음: $line"
    log "rsyslog ok"
fi
