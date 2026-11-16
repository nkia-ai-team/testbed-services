# 실행기 계정(러너가 쓰는 중립 이름, rsyslog 첫 규칙이 거르는 계정)과 운영자 계정의 키와 sudo.
# 운영자 계정은 cloud-init 기본 사용자로 이미 있다.
id "$OPERATOR_NAME" >/dev/null 2>&1 || fail "운영자 계정 $OPERATOR_NAME 없음"
if ! id "$EXECUTOR_NAME" >/dev/null 2>&1; then
    useradd --uid "$EXECUTOR_UID" --create-home --shell /bin/bash "$EXECUTOR_NAME"
fi
[[ "$(id -u "$EXECUTOR_NAME")" == "$EXECUTOR_UID" ]] || fail "실행기 uid 가 $EXECUTOR_UID 이 아님"
for account in "$EXECUTOR_NAME" "$OPERATOR_NAME"; do
    key="$BUNDLE/secrets/${account}.pub"
    [[ -r "$key" ]] || fail "공개 키 없음: ${account}.pub"
    home=$(getent passwd "$account" | cut -d: -f6)
    install -d -m 0700 -o "$account" -g "$account" "$home/.ssh"
    install -m 0600 -o "$account" -g "$account" "$key" "$home/.ssh/authorized_keys"
done
place_files accounts
visudo -cf /etc/sudoers.d/60-ops >/dev/null || fail "sudoers 문법"
log "accounts ok"
