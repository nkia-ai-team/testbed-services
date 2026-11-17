#!/bin/bash
# 렌더한 rsyslog 설정을 진짜 rsyslog(Ubuntu 24.04 패키지)로 확인하는 수동 도구(기본 시험에 넣지 않는다. docker 가 필요하다).
# 기본 단위 시험은 큰따옴표 안 맨 $ 같은 꼴만 정적으로 본다. 진짜 파서와 첫 규칙 정규식 동작은 이것으로 본다.
#   - 인벤토리(기본 t.json, s.json)의 모든 서버 묶음을 렌더한다
#   - 서버마다 일회 컨테이너의 /etc 를 패키지 기본으로 되돌리고 /etc/rsyslog* 로 놓이는 묶음 파일을 놓은 뒤
#     rsyslogd -N1 -d: 설정 오류 없음, 놓은 rsyslog.d 파일을 파서가 실제로 읽음, 읽는 파일과 포함 대상이 모두
#     /etc/rsyslog.conf, /etc/rsyslog.d/ 안(Ubuntu 의 rsyslogd AppArmor 프로필이 읽게 두는 범위를 경로로 흉내 낸다)
#   - 첫 규칙 동작: 원격 전달 파일은 빼고(검사 줄이 바깥으로 나가지 않게) rsyslogd 를 띄워 logger 로 줄을 보낸다.
#     실행기 줄(계정 이름이나 uid 가 줄 끝에 오는 줄, 실행기 uid 로 보낸 줄 포함)은 /var/log/opsjobs.log 에만,
#     아닌 줄은 opsjobs.log 에 없고 syslog 나 auth.log 에 있어야 한다
# 대상 VM 과 다른 점(여기서 못 보는 것과 보는 곳):
#   - AppArmor 가 커널에 없다. 프로필 범위는 경로로만 흉내 내고, /proc 읽기 거부 조각은 패키지 프로필에 포함해 파싱만 본다
#     (apparmor_parser -QTK, -p). 실제 거부는 설치 단계의 읽기 확인(rsyslog_reads), 재시작 뒤 /proc 거부 감사 0 확인과
#     verify 의 rsyslog-no-apparmor-denial, rsyslog-no-proc-audit 이 본다.
#   - 줄이 journald 를 거치지 않고 /dev/log 로 바로 들어온다. VM 의 길(journald 가 넘겨 주는 uid)은 syslog 단계의
#     실행기 uid 줄 확인과 runbook §7 이 본다.
#   - VM 의 rsyslog.d 에만 있는 파일(20-ufw.conf, 21-cloudinit.conf, WAS 의 tomcat10 파일)은 없다. 그 파일과 함께 읽는
#     검사는 설치 단계의 사본 검사가 VM 에서 한다.
# 끝에 컨테이너를 지운다. 104 같은 공용 서버에서는 무거운 잠금과 메모리 상한 아래에서 돌린다.
# 쓰는 법(저장소 뿌리에서): civil-petition/deploy/tools/rsyslog-check.sh [인벤토리 ...]
#   이미지는 RSYSLOG_CHECK_IMAGE(기본 ubuntu:24.04 의 다이제스트 고정). 컨테이너 안에서 rsyslog 를 apt 로 받으므로 바깥 길이
#   있어야 한다. RSYSLOG_CHECK_VERSION 을 주면 그 꼬리 판(apt-get install rsyslog=<판>)으로 고정한다.
#   CHECK <이름>|t 줄이 모두 t 이고 끝 줄이 failed=0 이면 통과(rc 0). 첫 줄의 rsyslog 꼬리 판을 대상 VM 의
#   dpkg-query -W rsyslog 와 견준다.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
deploy=$(cd "$here/.." && pwd)
# ubuntu:24.04, 2026-05-20 빌드(태그는 움직이므로 다이제스트로 고정한다).
image="${RSYSLOG_CHECK_IMAGE:-ubuntu@sha256:786a8b558f7be160c6c8c4a54f9a57274f3b4fb1491cf65146521ae77ff1dc54}"
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
apt-get update -qq >/dev/null && apt-get install -y -qq --no-install-recommends "rsyslog${RSYSLOG_CHECK_VERSION:+=$RSYSLOG_CHECK_VERSION}" apparmor >/dev/null \
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
    rm -rf /etc/rsyslog.d
    cp -a /root/pristine/rsyslog.d /etc/
    cp -a /root/pristine/rsyslog.conf /etc/rsyslog.conf
    placed=0
    conf_files=()
    while IFS=$'\t' read -r _step dest mode _owner _group src; do
        case "$dest" in
            /etc/rsyslog.conf|/etc/rsyslog.d/*)
                install -D -m "$mode" "$bundle/$src" "$dest"
                placed=$((placed + 1))
                [[ "$dest" != /etc/rsyslog.d/*.conf ]] || conf_files+=("$dest") ;;
        esac
    done < "$bundle/files.tsv"
    record "$label placed" $(( placed < 2 )) "rsyslog 파일 $placed 개"
    out=$(rsyslogd -N1 -d 2>&1)
    status=$?
    [[ $status -eq 0 ]] || grep '^rsyslogd:' <<<"$out"
    record "$label parse" "$status"
    unread=""
    for f in "${conf_files[@]}"; do
        grep -qF "pushed config fragment on top of stack: $f" <<<"$out" || unread+=" $f"
    done
    record "$label reads" $(( ${#unread} > 0 )) "읽지 않음:$unread"
    outside=$(grep -oE "(on top of stack: |include config file '|cnfDoInclude: file: ')[^' ]+" <<<"$out" \
        | sed -E "s/^(on top of stack: |include config file '|cnfDoInclude: file: ')//" \
        | grep -vE '^/etc/rsyslog\.(d/|conf$)' | sort -u | paste -sd' ' -)
    record "$label apparmor-scope" $(( ${#outside} > 0 )) "범위 밖: $outside"
    # AppArmor 조각: 패키지 rsyslogd 프로필에 포함돼 파싱되고(커널에 올리지는 않는다) 조각의 규칙이 모두 들어가는가.
    rm -f /etc/apparmor.d/rsyslog.d/annotate-proc
    fragment=$(awk -F'\t' '$2 == "/etc/apparmor.d/rsyslog.d/annotate-proc" {print $6}' "$bundle/files.tsv")
    aa_bad="조각 없음"
    if [[ -n "$fragment" ]]; then
        install -m 0644 "$bundle/$fragment" /etc/apparmor.d/rsyslog.d/annotate-proc
        aa_bad=""
        apparmor_parser -QTK /etc/apparmor.d/usr.sbin.rsyslogd >/dev/null 2>/tmp/aa.err || aa_bad="파싱 실패: $(grep -v 'Cache read' /tmp/aa.err | head -1)"
        expanded=$(apparmor_parser -p /etc/apparmor.d/usr.sbin.rsyslogd 2>/dev/null)
        while IFS= read -r rule; do
            [[ -z "$rule" || "$rule" == \#* ]] && continue
            grep -qxF -- "$rule" <<<"$expanded" || aa_bad+=" 프로필에 안 들어감: $rule"
        done < /etc/apparmor.d/rsyslog.d/annotate-proc
    fi
    record "$label apparmor-fragment" $(( ${#aa_bad} > 0 )) "$aa_bad"

    # 첫 규칙 동작. 원격 전달은 빼고 띄운다.
    rm -f /etc/rsyslog.d/remote/*.conf
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

docker run --name "$name" --rm --memory 512m -e "RSYSLOG_CHECK_VERSION=${RSYSLOG_CHECK_VERSION:-}" \
    -v "$work:/check:ro" "$image" bash /check/inner.sh
