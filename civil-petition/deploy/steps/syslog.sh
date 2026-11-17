# rsyslog 첫 규칙(실행기 배관 줄을 로컬 파일로)과 uid 속성(설계 §6-2). 전달은 다음 단계(syslog-forward)가 켠다.
# 새 설정은 검사 사본에서 먼저 보고 통과할 때만 놓는다(못 읽을 설정이 남아 재시작, 재부팅 때 rsyslog 가 서지 못하는 일을 막는다).
log_dir=${RSYSLOG_LOG_DIR:-/var/log}
remote_dir="$RSYSLOG_ETC/rsyslog.d/remote"
install -d -m 0755 "$remote_dir"
# 옛 전달 자리(AppArmor rsyslogd 프로필 밖이라 rsyslog 가 읽지 못하던 곳)는 비어 있으면 지운다.
rmdir "$RSYSLOG_ETC/rsyslog.remote.d" 2>/dev/null || true
rsyslog_place_checked syslog
touch "$log_dir/opsjobs.log" && chown syslog:adm "$log_dir/opsjobs.log" && chmod 0640 "$log_dir/opsjobs.log"
systemctl restart rsyslog
# 검사 줄은 원격 전달이 켜지기 전(첫 설치)에만 남긴다. 전달이 이미 켜진 뒤 다시 돌릴 때는 관제 서버로 갈 수 있으므로 건너뛴다.
if compgen -G "$remote_dir/*.conf" >/dev/null; then
    log "rsyslog ok (전달이 켜져 있어 검사 줄 생략)"
else
    # ParseTrusted 를 켜도 본문에 _UID= 같은 꼬리가 붙지 않는지 본다(붙으면 모든 줄의 모양이 바뀐다).
    logger -t rsyslog-check "annotation format check"
    sleep 1
    line=$(grep "rsyslog-check" "$log_dir/syslog" | tail -1)
    [[ -n "$line" ]] || fail "rsyslog 검사 줄이 $log_dir/syslog 에 없음"
    [[ "$line" != *"_UID="* && "$line" != *"@["* ]] || fail "본문에 자격 증명 꼬리가 붙음: $line"
    # 실행기 계정으로 보낸 줄이 진짜 길(journald 가 넘겨 주는 자격 증명의 uid 속성)로 첫 규칙에 걸리는지 본다.
    # 그 줄은 opsjobs.log 에만 있어야 하고 syslog, auth.log 에는 없어야 한다(실행기 계정은 앞 단계 accounts 가 만든다).
    mark="uid route check $(date +%s%N)"
    setpriv --reuid="$EXECUTOR_NAME" --regid="$EXECUTOR_NAME" --init-groups logger -t rsyslog-check -- "$mark"
    for _ in $(seq 20); do grep -qF -- "$mark" "$log_dir/opsjobs.log" && break; sleep 0.5; done
    grep -qF -- "$mark" "$log_dir/opsjobs.log" || fail "실행기 uid 줄이 $log_dir/opsjobs.log 에 없음(첫 규칙의 uid 갈래가 안 걸림)"
    for f in syslog auth.log; do
        [[ ! -f "$log_dir/$f" ]] || ! grep -qF -- "$mark" "$log_dir/$f" || fail "실행기 uid 줄이 $log_dir/$f 에도 있음"
    done
    log "rsyslog ok"
fi
