#!/bin/bash
# 렌더한 rsyslog 설정을 대상 VM 과 같은 rsyslog(ubuntu:24.04 패키지)로 확인하는 수동 도구(기본 시험에 넣지 않는다. docker 가 필요하다).
# 기본 단위 시험은 큰따옴표 안 맨 $ 같은 꼴만 정적으로 본다. 진짜 파서와 진짜 규칙 동작은 이것으로 본다.
#   - 인벤토리(기본 t.json, s.json)의 모든 서버 묶음을 렌더한다
#   - 서버마다 일회 컨테이너의 /etc 를 패키지 기본으로 되돌리고 /etc/rsyslog* 로 놓이는 묶음 파일을 놓은 뒤 rsyslogd -N1
#   - 첫 규칙 동작: 원격 전달 파일은 빼고(검사 줄이 바깥으로 나가지 않게) rsyslogd 를 띄워 logger 로 줄을 보낸다.
#     실행기 줄(계정 이름이나 uid 가 줄 끝에 오는 줄, 실행기 uid 로 보낸 줄 포함)은 /var/log/opsjobs.log 에만,
#     아닌 줄은 opsjobs.log 에 없고 syslog 나 auth.log 에 있어야 한다
# 끝에 컨테이너를 지운다. 104 같은 공용 서버에서는 무거운 잠금과 메모리 상한 아래에서 돌린다.
# 쓰는 법(저장소 뿌리에서): civil-petition/deploy/tools/rsyslog-check.sh [인벤토리 ...]
#   이미지는 RSYSLOG_CHECK_IMAGE(기본 ubuntu:24.04). 컨테이너 안에서 rsyslog 를 apt 로 받으므로 바깥 길이 있어야 한다.
#   CHECK <이름>|t 줄이 모두 t 이고 끝 줄이 failed=0 이면 통과(rc 0). rsyslog 줄의 판을 대상 VM 의 rsyslogd -v 와 견준다.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
deploy=$(cd "$here/.." && pwd)
image="${RSYSLOG_CHECK_IMAGE:-ubuntu:24.04}"
if [[ $# -gt 0 ]]; then
    inventories=("$@")
else
    inventories=("$deploy/inventory/t.json" "$deploy/inventory/s.json")
fi
name="rsyslog-check-$$"
work=$(mktemp -d)
cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; rm -rf "$work"; }
trap cleanup EXIT

for inv in "${inventories[@]}"; do
    python3 "$deploy/render.py" --inventory "$inv" --out "$work/bundles/$(basename "$inv" .json)" >/dev/null
done
chmod -R a+rX "$work"

cat > "$work/inner.sh" <<'INNER'
set -uo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null && apt-get install -y -qq --no-install-recommends rsyslog >/dev/null \
    || { echo "rsyslog 설치 실패"; exit 2; }
echo "rsyslog $(dpkg-query -W -f '${Version}' rsyslog) ($(rsyslogd -v | head -1))"
mkdir -p /root/pristine
cp -a /etc/rsyslog.conf /etc/rsyslog.d /root/pristine/
chown root:syslog /var/log && chmod 0775 /var/log

checks=0
failed=0
record() {  # 이름 결과(0 이면 통과) [설명]
    checks=$((checks + 1))
    if [[ $2 -eq 0 ]]; then
        echo "CHECK $1|t"
    else
        echo "CHECK $1|f ${3:-}"
        failed=$((failed + 1))
    fi
}
# 줄이 ": <본문>" 으로 끝나는 줄이 파일에 있는가(본문이 다른 본문의 일부여도 섞이지 않게 끝을 맞춘다).
has_line() { [[ -f "$2" ]] && awk -v s=": $1" 'length($0) >= length(s) && substr($0, length($0) - length(s) + 1) == s { found = 1 } END { exit !found }' "$2"; }

for bundle in /check/bundles/*/*/; do
    bundle=${bundle%/}
    label="$(basename "$(dirname "$bundle")")/$(basename "$bundle")"
    rm -rf /etc/rsyslog.d /etc/rsyslog.remote.d
    cp -a /root/pristine/rsyslog.d /etc/
    cp -a /root/pristine/rsyslog.conf /etc/rsyslog.conf
    mkdir -p /etc/rsyslog.remote.d
    placed=0
    while IFS=$'\t' read -r _step dest mode _owner _group src; do
        case "$dest" in
            /etc/rsyslog.conf|/etc/rsyslog.d/*|/etc/rsyslog.remote.d/*)
                install -D -m "$mode" "$bundle/$src" "$dest"
                placed=$((placed + 1)) ;;
        esac
    done < "$bundle/files.tsv"
    record "$label placed" $(( placed < 2 )) "rsyslog 파일 $placed 개"
    out=$(rsyslogd -N1 2>&1)
    status=$?
    [[ $status -eq 0 ]] || printf '%s\n' "$out"
    record "$label parse" "$status"

    # 첫 규칙 동작. 원격 전달은 빼고 띄운다.
    rm -f /etc/rsyslog.remote.d/*.conf
    # shellcheck disable=SC1091
    . "$bundle/host.env"
    n=$EXECUTOR_NAME
    u=$EXECUTOR_UID
    rm -f /var/log/syslog /var/log/auth.log /var/log/opsjobs.log /run/rsyslogd.pid /dev/log
    for f in syslog auth.log opsjobs.log; do touch "/var/log/$f"; chown syslog:adm "/var/log/$f"; chmod 0640 "/var/log/$f"; done
    rsyslogd -i /run/rsyslogd.pid >/dev/null 2>&1
    for _ in $(seq 50); do [[ -S /dev/log ]] && break; sleep 0.1; done
    # 종류|우선순위|태그|실행기 uid 로 보냄(1)|본문
    cases=(
        "hit|auth.info|sshd|0|Accepted publickey for $n from 192.0.2.1 port 50000 ssh2"
        "hit|authpriv.info|sudo|0|pam_unix(sudo:session): session opened for user root(uid=0) by $n(uid=$u)"
        "hit|authpriv.info|sshd|0|pam_unix(sshd:session): session closed for user $n"
        "hit|auth.info|systemd-logind|0|New session 4 of user $n."
        "hit|daemon.info|systemd|0|Started user@$u.service - User Manager for UID $u."
        "hit|daemon.info|systemd|0|Removed slice user-$u.slice."
        "hit|daemon.info|systemd|0|Stopped User Manager for UID $u"
        "hit|daemon.info|systemd|0|Finished session cleanup for $n"
        "hit|daemon.info|systemd|0|run-r0123456789abcdef0123456789abcdef.service: Deactivated successfully."
        "hit|user.notice|cron|1|executor uid line"
        "miss|auth.info|sshd|0|Accepted publickey for $OPERATOR_NAME from 192.0.2.1 port 1 ssh2"
        "miss|daemon.info|systemd|0|Started session-1450.scope."
        "miss|auth.info|sshd|0|${n}s logged"
        "miss|auth.info|sshd|0|/var/log/${n}s.log rotated"
        "miss|auth.info|sshd|0|session closed for user x.${n}"
        "miss|daemon.info|systemd|0|Started session-$u.scope - Session $u of User $OPERATOR_NAME."
        "miss|daemon.info|systemd|0|Stopped User Manager for UID ${u}0"
        "miss|cron.info|CRON|0|($n) CMD (true)"
        "miss|user.notice|cron|0|root uid line"
    )
    for row in "${cases[@]}"; do
        IFS='|' read -r _kind pri tag as_exec text <<<"$row"
        if [[ $as_exec == 1 ]]; then
            setpriv --reuid="$u" --regid="$u" --clear-groups logger -p "$pri" -t "$tag" -- "$text"
        else
            logger -p "$pri" -t "$tag" -- "$text"
        fi
    done
    sleep 1
    kill "$(cat /run/rsyslogd.pid 2>/dev/null)" 2>/dev/null
    for _ in $(seq 50); do pgrep -x rsyslogd >/dev/null || break; sleep 0.1; done
    for row in "${cases[@]}"; do
        IFS='|' read -r kind pri tag _as_exec text <<<"$row"
        if [[ $kind == hit ]]; then
            has_line "$text" /var/log/opsjobs.log && ! has_line "$text" /var/log/syslog && ! has_line "$text" /var/log/auth.log
            record "$label hit $tag: $text" $?
        else
            ! has_line "$text" /var/log/opsjobs.log && { has_line "$text" /var/log/syslog || has_line "$text" /var/log/auth.log; }
            record "$label miss $tag: $text" $?
        fi
    done
done
echo "checks=$checks failed=$failed"
[[ $failed -eq 0 ]]
INNER

docker run --name "$name" --rm --memory 512m -v "$work:/check:ro" "$image" bash /check/inner.sh
