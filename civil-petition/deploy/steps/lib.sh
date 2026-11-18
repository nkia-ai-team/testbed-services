# shellcheck shell=bash
# 설치 단계가 함께 쓰는 함수. install-host.sh 가 source 한다(BUNDLE, host.env 값이 이미 있다).

log() { printf '%s\n' "$*"; }
fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }

# NIC 를 찾는 곳. 시험은 다른 경로를 준다.
SYS_NET=${SYS_NET:-/sys/class/net}
# 설치 단계 network 가 적는 "인벤토리 NIC 이름 MAC" 줄. place_files 가 @@MAC:<이름>@@ 자리를 이것으로 채운다.
NIC_MAC_FILE=${NIC_MAC_FILE:-/var/lib/petition-deploy/nic-macs}

# rsyslog 설정이 있는 곳(/etc/rsyslog.conf, rsyslog.d 의 부모). 시험은 다른 경로를 준다.
RSYSLOG_ETC=${RSYSLOG_ETC:-/etc}

# files.tsv 에서 이 단계의 파일을 놓는다. @@SECRET:<이름>@@ 자리는 secrets/<이름> 내용으로,
# @@MAC:<NIC 이름>@@ 자리는 NIC_MAC_FILE 의 그 NIC 줄의 MAC 으로 채운다. 채울 값이 없으면 파일을 놓지 않고 멈춘다.
# 둘째 인자로 놓을 경로를 주면 그 파일 하나만 놓는다. 실패하면 0 아닌 값을 돌려준다(|| 뒤에서 불려도).
# PLACE_ROOT 를 주면 놓을 경로 앞에 붙여 그 아래에 놓는다(검사용 사본).
place_files() {
    local step="$1" only="${2:-}" row_step dest mode owner group src target
    while IFS=$'\t' read -r row_step dest mode owner group src; do
        [[ "$row_step" == "$step" ]] || continue
        [[ -z "$only" || "$dest" == "$only" ]] || continue
        target="${PLACE_ROOT:-}$dest"
        install -d -m 0755 "$(dirname "$target")" || return 1
        if ! python3 - "$BUNDLE/$src" "$target.new" "$BUNDLE/secrets" "$NIC_MAC_FILE" <<'PY'
import pathlib, re, sys
source, target, secrets = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
mac_file = pathlib.Path(sys.argv[4])
text = source.read_text(encoding="utf-8")
def fill(match):
    path = secrets / match.group(1)
    if not path.is_file():
        sys.exit(f"비밀값 파일 없음: {match.group(1)}")
    return path.read_text(encoding="utf-8").strip()
def fill_mac(match):
    macs = dict(line.split()[:2] for line in mac_file.read_text(encoding="utf-8").splitlines()
                if len(line.split()) >= 2) if mac_file.is_file() else {}
    mac = macs.get(match.group(1), "")
    if not re.fullmatch(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}", mac):
        sys.exit(f"NIC MAC 없음: {match.group(1)}")
    return mac
text = re.sub(r"@@SECRET:([a-z0-9_]+)@@", fill, text)
target.write_text(re.sub(r"@@MAC:([a-z0-9]+)@@", fill_mac, text), encoding="utf-8")
PY
        then
            rm -f "$target.new"
            return 1
        fi
        chown "$owner:$group" "$target.new" && chmod "$mode" "$target.new" && mv -f "$target.new" "$target" || return 1
        [[ -n "${PLACE_ROOT:-}" ]] || log "placed $dest"
    done < "$BUNDLE/files.tsv"
}

# rsyslogd -N1 -d 로 설정을 읽어 보고, 준 파일마다 파서가 실제로 열어 읽었는지(디버그 줄
# "pushed config fragment on top of stack: <파일>") 본다. 설정 오류이거나 못 읽은 파일이 있으면 rsyslogd 의 알림 줄과
# 못 읽은 파일을 stderr 에 내고 1. include(... mode="optional") 는 AppArmor 가 막아 못 읽어도 오류가 아니라서
# 종료 코드만으로는 못 읽은 것을 모른다.
rsyslog_reads() {
    local conf="$1" out status=0 f missing=()
    shift
    out=$(rsyslogd -N1 -d -f "$conf" 2>&1) || status=$?
    for f in "$@"; do
        awk -v s="on top of stack: $f" 'length($0) >= length(s) && substr($0, length($0) - length(s) + 1) == s { found = 1 }
            END { exit !found }' <<<"$out" || missing+=("$f")
    done
    if [[ $status -ne 0 || ${#missing[@]} -gt 0 ]]; then
        grep '^rsyslogd:' <<<"$out" >&2 || true
        [[ ${#missing[@]} -eq 0 ]] || printf 'rsyslog 가 읽지 않은 파일: %s\n' "${missing[*]}" >&2
        return 1
    fi
}

# 앞 내용을 남긴 파일들을 되돌린다(남긴 것이 없으면 지운다). 되돌리지 못한 경로를 한 줄씩 낸다.
#   rsyslog_put_back <남긴 디렉터리> <경로>...   (남긴 디렉터리/<순번> 이 그 순번 경로의 앞 내용)
rsyslog_put_back() {
    local keep="$1" dest i=0
    shift
    for dest in "$@"; do
        i=$((i + 1))
        rm -f "$dest.new"
        if [[ -e "$keep/$i" ]]; then
            cp -p "$keep/$i" "$dest" || echo "$dest"
        else
            rm -f "$dest" || echo "$dest"
        fi
    done
}

# AppArmor 가 켜졌는지 보는 곳과 프로필 디렉터리. 시험은 다른 경로를 준다.
APPARMOR_FS=${APPARMOR_FS:-/sys/kernel/security/apparmor}
APPARMOR_ETC=${APPARMOR_ETC:-/etc/apparmor.d}
# rsyslogd 의 /proc 읽기 거부(로컬 소켓 주석의 부작용) 감사 줄 꼴.
RSYSLOG_PROC_DENIAL='name="/proc/[0-9]+/|operation="ptrace"'

# 이 단계의 AppArmor 조각 하나(<경로>)를 놓고 그 조각을 포함하는 프로필(<프로필>)을 다시 읽는다(apparmor_parser -r).
# 다시 읽기가 실패하면 조각을 앞 상태로 되돌리고(없던 것은 지운다) 프로필을 한 번 더 읽어 앞 규칙으로 돌린 뒤 멈춘다.
# AppArmor 가 꺼졌거나 프로필이 없으면 조각만 놓고 다시 읽지 않는다.
apparmor_place_checked() {
    local step="$1" dest="$2" profile="$3" keep out again
    keep=$(mktemp) || fail "AppArmor 조각 앞 내용을 남길 곳을 못 만듦"
    if [[ -e "$dest" ]]; then cp -p "$dest" "$keep" || { rm -f "$keep"; fail "앞 내용을 못 남김: $dest"; }; else rm -f "$keep"; fi
    place_files "$step" "$dest" && [[ -f "$dest" ]] || { rm -f "$keep"; fail "AppArmor 조각을 못 놓음(묶음에 없거나 놓기 실패): $dest"; }
    if [[ ! -d "$APPARMOR_FS" || ! -f "$profile" ]]; then
        rm -f "$keep"
        log "apparmor off or no profile: $profile 다시 읽기 생략"
        return 0
    fi
    if out=$(apparmor_parser -r "$profile" 2>&1); then
        rm -f "$keep"
        log "apparmor reloaded $profile"
        return 0
    fi
    printf '%s\n' "$out" >&2
    if [[ -e "$keep" ]]; then cp -p "$keep" "$dest"; else rm -f "$dest"; fi
    rm -f "$keep"
    again=$(apparmor_parser -r "$profile" 2>&1) || again="앞 규칙으로 다시 읽기도 실패: $again"
    fail "AppArmor 프로필 다시 읽기 실패(단계 $step). 조각을 앞 상태로 되돌림. ${again}"
}

# rsyslog 가 마지막으로 선 뒤 AppArmor 가 rsyslogd 에 대해 남긴 거부 줄(커널 로그와 감사 기록)을 낸다.
# 인자로 꼴(grep -E)을 주면 그 꼴만. 시각을 못 읽으면 1(빈 출력을 "거부 없음" 으로 읽지 않게).
rsyslog_denials() {
    local started since lines
    started=$(systemctl show -p ActiveEnterTimestamp --value rsyslog) && [[ -n "$started" ]] || return 1
    since=$(date -d "$started" +%s) || return 1
    lines=$(journalctl -q --no-pager --since "@$since" _TRANSPORT=kernel + _TRANSPORT=audit) || return 1
    grep 'apparmor="DENIED"' <<<"$lines" | grep 'profile="rsyslogd"' | grep -E -- "${1:-.}" || true
}

# 이 단계의 파일(rsyslog 설정 포함)을 놓되, rsyslog 가 못 읽을 설정은 /etc 에 남기지 않는다.
#   1. 검사 사본: rsyslog.d 아래 숨은 하위 디렉터리 .check-<pid>(*.conf 포함에 들지 않고, AppArmor rsyslogd 프로필이
#      읽게 두는 /etc/rsyslog.d/** 안)에 지금 rsyslog.conf 와 rsyslog.d 의 *.conf, rsyslog.d/remote 의 *.conf 를 베끼고
#      (사본 디렉터리 자신은 베끼지 않는다) 이 단계 파일을 그 위에 놓은 뒤, 사본 rsyslog.conf 의 rsyslog.d 포함 경로를
#      사본으로 바꿔 읽어 본다. 이 단계의 rsyslog 파일을 모두 실제로 읽어야 통과. 실패하면 /etc 의 설정은 그대로 두고 멈춘다.
#   2. 통과하면 이 단계 파일의 지금 내용을 사본 디렉터리에 남기고 진짜로 놓은 뒤 진짜 설정을 같은 방법으로 본다.
#      놓기나 검사가 실패하면 놓은 파일을 앞 내용으로 되돌리고(없던 파일은 지운다) 멈춘다.
# 사본 디렉터리는 끝나면 지운다(EXIT trap 도 건다. 끊겨 남은 것은 다음 실행이 처음에 지운다).
# 실패하면 rsyslog 를 재시작하지 않으므로 돌던 rsyslog 는 앞 설정 그대로다(재시작하는 것은 부른 단계).
rsyslog_place_checked() {
    local step="$1" check_dir="$RSYSLOG_ETC/rsyslog.d" stage out row_step dest f i=0 kept=() reads=() staged=() undone
    rm -rf "$check_dir"/.check-*
    stage="$check_dir/.check-$$"
    mkdir -p "$stage$RSYSLOG_ETC/rsyslog.d/remote" "$stage/keep" || fail "rsyslog 검사 디렉터리를 못 만듦: $stage"
    trap "rm -rf '$stage'" EXIT
    [[ ! -e "$RSYSLOG_ETC/rsyslog.conf" ]] || cp -p "$RSYSLOG_ETC/rsyslog.conf" "$stage$RSYSLOG_ETC/rsyslog.conf"
    for f in "$check_dir"/*.conf; do [[ ! -f "$f" ]] || cp -p "$f" "$stage$RSYSLOG_ETC/rsyslog.d/"; done
    for f in "$check_dir"/remote/*.conf; do [[ ! -f "$f" ]] || cp -p "$f" "$stage$RSYSLOG_ETC/rsyslog.d/remote/"; done
    if ! PLACE_ROOT="$stage" place_files "$step"; then
        rm -rf "$stage"; trap - EXIT; fail "단계 $step 파일을 검사 사본에 못 놓음"
    fi
    if ! out=$(python3 - "$stage$RSYSLOG_ETC/rsyslog.conf" "$RSYSLOG_ETC/rsyslog.d/" "$stage$RSYSLOG_ETC/rsyslog.d/" <<'PY' 2>&1
import pathlib, sys
conf, real, staged = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
conf.write_text(conf.read_text(encoding="utf-8").replace(real, staged), encoding="utf-8")
PY
    ); then
        rm -rf "$stage"; trap - EXIT; fail "rsyslog 검사 사본을 못 만듦: $out"
    fi
    while IFS=$'\t' read -r row_step dest _; do
        [[ "$row_step" == "$step" ]] || continue
        # 이 단계가 놓는 rsyslog 설정 조각(rsyslog.d 아래든 그 밖의 rsyslog*.d 든)은 모두 실제로 읽혀야 한다.
        case "$dest" in "$RSYSLOG_ETC"/rsyslog*.d/*.conf) reads+=("$dest"); staged+=("$stage$dest") ;; esac
        i=$((i + 1))
        if [[ -e "$dest" ]] && ! cp -p "$dest" "$stage/keep/$i"; then
            rm -rf "$stage"; trap - EXIT; fail "앞 내용을 못 남김: $dest"
        fi
        kept+=("$dest")
    done < "$BUNDLE/files.tsv"
    if ! rsyslog_reads "$stage$RSYSLOG_ETC/rsyslog.conf" "${staged[@]}"; then
        rm -rf "$stage"; trap - EXIT
        fail "rsyslog 설정 검사(놓기 전, 단계 $step). $RSYSLOG_ETC 의 설정은 바꾸지 않음"
    fi
    if ! place_files "$step"; then
        undone=$(rsyslog_put_back "$stage/keep" "${kept[@]}")
        rm -rf "$stage"; trap - EXIT
        fail "단계 $step 파일을 놓지 못함. ${undone:+되돌리지 못한 파일: $(paste -sd' ' - <<<"$undone")}${undone:-놓은 파일을 앞 내용으로 되돌림}"
    fi
    if ! rsyslog_reads "$RSYSLOG_ETC/rsyslog.conf" "${reads[@]}"; then
        undone=$(rsyslog_put_back "$stage/keep" "${kept[@]}")
        rm -rf "$stage"; trap - EXIT
        fail "rsyslog 설정 검사(놓은 뒤, 단계 $step). ${undone:+되돌리지 못한 파일: $(paste -sd' ' - <<<"$undone")}${undone:-놓은 파일을 앞 내용으로 되돌림}"
    fi
    rm -rf "$stage"; trap - EXIT
}

# serial 이 <serial> 과 글자 그대로 같은 디스크(lsblk TYPE disk, 파티션 제외) 하나의 장치 경로를 낸다.
# 없거나 둘 이상이면 찾은 목록을 stderr 에 내고 1. /dev/disk/by-id 의 이름 꼴은 기대지 않는다: Proxmox 가 serial 을 준
# 디스크는 scsi-SQEMU_QEMU_HARDDISK_<serial> 이고 scsi-0QEMU_QEMU_HARDDISK_ 꼴은 drive-scsiN 에 붙는(66 db01, 11-18)
# 것처럼 QEMU 판과 serial 유무에 따라 다르다. serial 을 주지 않은 디스크는 lsblk SERIAL 이 drive-scsiN 이다.
disk_by_serial() {
    local want="$1" table found count
    table=$(lsblk -dnr -o PATH,SERIAL,TYPE) || { echo "lsblk 실패" >&2; return 1; }
    found=$(awk -v s="$want" '$3 == "disk" && $2 == s {print $1}' <<<"$table")
    count=$(grep -c . <<<"$found" || true)
    if [[ "$count" -ne 1 ]]; then
        printf 'serial %s 인 디스크가 %s개(하나여야 함). lsblk: %s\n' "$want" "$count" \
            "$(awk '$3 == "disk" {printf "%s=%s ", $1, ($2 == "" ? "(없음)" : $2)}' <<<"$table")" >&2
        return 1
    fi
    printf '%s\n' "$found"
}

# 서비스를 다시 띄운 직후의 확인은 값이 설 때까지 다시 읽는다. 서비스가 막 섰을 때는 첫 응답, 설정이 덮어쓰는 값,
# 도우미가 답하는 열이 아직 서지 않았을 수 있다(snmpd 가 선 지 1초 안에 ifHighSpeed 를 한 번만 읽어 0 을 보고 멈춘 일).
#   settle <상한 초> <간격 초> <실패 문구> <명령> [인자...]
# 명령이 0 을 돌려줄 때까지 간격마다 다시 돌린다. 상한은 횟수가 아니라 시계(date +%s)로 재고, 넘으면 실패 문구 뒤에
# 마지막으로 본 출력(stdout 과 stderr, 줄은 " | " 로 잇고 300자까지)을 붙여 멈춘다. 상한은 다시 읽기 전에 보므로
# 마지막 읽기 하나의 시간만큼 넘칠 수 있다. 명령의 출력은 통과하면 버린다.
SETTLE_SEC=${SETTLE_SEC:-30}
SETTLE_EVERY=${SETTLE_EVERY:-1}
settle() {
    local limit="$1" every="$2" what="$3" deadline out=""
    shift 3
    deadline=$(( $(date +%s) + limit ))
    while :; do
        if out=$("$@" 2>&1); then
            return 0
        fi
        (( $(date +%s) < deadline )) || break
        sleep "$every"
    done
    out=${out//$'\n'/ | }
    out=${out:0:300}
    fail "$what(${limit}초 동안 다시 읽음). 마지막 값: ${out:-(출력 없음)}"
}

# 서비스 이름을 받아 켜고 다시 띄운다.
restart_enabled() {
    systemctl daemon-reload
    systemctl enable "$@" >/dev/null
    systemctl restart "$@"
}

# 묶음의 artifacts/SHA256SUMS 와 파일 체크섬이 맞는지 본다.
verify_artifact() {
    local name="$1"
    (cd "$BUNDLE/artifacts" && grep -E "  ${name}\$" SHA256SUMS | sha256sum -c --quiet -) \
        || fail "artifact 체크섬 다름: $name"
}

# 셸 변수로만 비밀값을 읽는다(화면에 내지 않는다).
read_secret() {
    local name="$1"
    [[ -r "$BUNDLE/secrets/$name" ]] || fail "비밀값 파일 없음: $name"
    tr -d '\n' < "$BUNDLE/secrets/$name"
}

# 패키지 색인 시점(S 문서 §2-4 쌍 조건 2). APT_SNAPSHOT(apply.sh --apt-snapshot, 꼴 YYYYMMDDTHHMMSSZ, UTC)이 있으면
# Ubuntu snapshot 저장소의 그 시각 색인으로 받고 설치한다(T 짝 서버의 apt 설치 시각을 주면 의존 패키지까지 T 와 같은 판).
# apt 는 스냅숏 색인을 못 받아도(인증서, 길) 경고만 내고 끝 코드 0 으로 옛 색인을 쓰며, 미래 시각은 최신 색인을 준다.
# 그래서 경고, 미래 시각, 받은 색인 파일을 따로 보고 하나라도 어긋나면 멈춘다. 받기와 설치 둘 다에 같은 값을 준다
# (설치에 빠지면 스냅숏이 아닌 색인을 쓴다).
APT_LISTS=${APT_LISTS:-/var/lib/apt/lists}
POLICY_RC=${POLICY_RC:-/usr/sbin/policy-rc.d}
OS_RELEASE=${OS_RELEASE:-/etc/os-release}
apt_snapshot_opts=()
if [[ -n "${APT_SNAPSHOT:-}" ]]; then
    apt_snapshot_opts=(--snapshot "$APT_SNAPSHOT")
fi
# 이 서버를 앞서 깐 색인 시각(os-base 가 남김)과 이번 값이 다르면, 또는 앞서 스냅숏으로 깔았는데 이번에 값이 없으면 아무것도
# 하지 않고 멈춘다(다시 돌릴 때 다른 색인이나 지금 색인으로 받아 판이 갈라지지 않게). 기록이 없으면(처음, 또는 기록 전에 깐 T) 통과.
APT_SNAPSHOT_RECORD=${APT_SNAPSHOT_RECORD:-/var/lib/petition-deploy/apt-snapshot}
apt_snapshot_record_check() {
    local before=""
    [[ -r "$APT_SNAPSHOT_RECORD" ]] && before=$(tr -d '[:space:]' < "$APT_SNAPSHOT_RECORD")
    [[ -z "$before" || "$before" == "${APT_SNAPSHOT:-}" ]] \
        || fail "앞서 깐 색인 시각 $before 과 이번 값 '${APT_SNAPSHOT:-}' 이 다름($APT_SNAPSHOT_RECORD). 인벤토리 apt_snapshot 을 본다"
}
apt_update() {
    local out codename suite
    if [[ -n "${APT_SNAPSHOT:-}" ]]; then
        [[ "$APT_SNAPSHOT" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || fail "APT_SNAPSHOT 꼴이 다름(YYYYMMDDTHHMMSSZ): $APT_SNAPSHOT"
        [[ ! "$APT_SNAPSHOT" > "$(date -u +%Y%m%dT%H%M%SZ)" ]] || fail "APT_SNAPSHOT 이 미래 시각(최신 색인이 됨): $APT_SNAPSHOT"
    fi
    out=$(apt-get update -q "${apt_snapshot_opts[@]}" 2>&1) || { printf '%s\n' "$out" >&2; fail "apt-get update 실패"; }
    printf '%s\n' "$out"
    [[ -n "${APT_SNAPSHOT:-}" ]] || return 0
    ! grep -qE '^(W|E):' <<<"$out" || fail "스냅숏 색인을 받지 못함($APT_SNAPSHOT): $(grep -E '^(W|E):' <<<"$out" | head -1)"
    codename=$(. "$OS_RELEASE" && echo "${VERSION_CODENAME:-}")
    [[ -n "$codename" ]] || fail "배포판 이름을 못 읽음: $OS_RELEASE"
    for suite in "$codename" "$codename-updates" "$codename-security"; do
        compgen -G "$APT_LISTS/snapshot.ubuntu.com_ubuntu_${APT_SNAPSHOT}_dists_${suite}_InRelease" >/dev/null \
            || fail "스냅숏 색인 $suite 이 없음($APT_SNAPSHOT)"
    done
    log "apt snapshot $APT_SNAPSHOT"
}

# 패키지를 설치한다. 판 목록 파일(artifacts/package-versions.txt, "이름 판" 줄)이 있으면 그 판으로 고정한다
# (S 가 T 와 같은 판을 쓰는 쌍 조건 2, S 문서 §2-4). 설치 직후 서비스를 띄우지 않는다(설정을 놓은 뒤 단계가 띄운다).
apt_install() {
    local wanted=() name version
    for name in "$@"; do
        version=""
        if [[ -r "$BUNDLE/artifacts/package-versions.txt" ]]; then
            version=$(awk -v n="$name" '$1==n{print $2}' "$BUNDLE/artifacts/package-versions.txt")
        fi
        wanted+=("${name}${version:+=$version}")
    done
    printf '#!/bin/sh\nexit 101\n' > "$POLICY_RC"
    chmod 0755 "$POLICY_RC"
    if ! DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends "${apt_snapshot_opts[@]}" "${wanted[@]}"; then
        rm -f "$POLICY_RC"
        return 1
    fi
    rm -f "$POLICY_RC"
}

# 인벤토리 NIC(NIC_EXPECT 의 "이름=주소")마다 지금 그 주소를 가진 장치를 찾아 "이름 장치 MAC" 줄을 낸다.
# VM 생성(plan.py create)이 Proxmox netN 과 ipconfigN 을 같은 번호로 주므로, cloud-init 이 netN 의 MAC 에 붙인
# 인벤토리 주소가 그 NIC 를 가리킨다. 주소를 가진 장치가 없거나 둘 이상이거나, 한 장치가 두 NIC 의 주소를 가지면 실패.
nic_resolve() {
    local pair ifname addr devs dev mac seen=" "
    for pair in $NIC_EXPECT; do
        ifname=${pair%%=*}
        addr=${pair#*=}
        devs=$(ip -4 -o addr show | awk -v a="$addr" '{split($4, p, "/"); if (p[1] == a) print $2}' | sort -u)
        if [[ -z "$devs" ]]; then
            echo "nic: $addr 를 가진 장치 없음($ifname)" >&2; return 1
        fi
        if [[ $(wc -l <<<"$devs") -ne 1 ]]; then
            echo "nic: $addr 를 가진 장치가 여럿($(tr '\n' ' ' <<<"$devs"))" >&2; return 1
        fi
        dev=$devs
        if [[ "$seen" == *" $dev "* ]]; then
            echo "nic: $dev 가 인벤토리 NIC 둘의 주소를 가짐" >&2; return 1
        fi
        seen+="$dev "
        mac=$(tr 'A-F' 'a-f' < "$SYS_NET/$dev/address") || return 1
        printf '%s %s %s\n' "$ifname" "$dev" "$mac"
    done
}

# 인벤토리에 없는 물리 NIC(장치가 붙은 망 장치) 이름을 낸다. netplan 이 그 NIC 를 다루지 않게 되므로 있으면 안 된다.
nic_leftover() {
    local path name pair known=" "
    for pair in $NIC_EXPECT; do known+="${pair%%=*} "; done
    for path in "$SYS_NET"/*; do
        name=${path##*/}
        [[ -e "$path/device" ]] || continue
        [[ "$known" == *" $name "* ]] || printf '%s\n' "$name"
    done
}

# 인벤토리 이름대로 바뀌었는지 본다: NIC 마다 그 이름의 장치가 있고, MAC 이 NIC_MAC_FILE 의 값이고, 주소가 있다.
# 남은 물리 NIC 도 없어야 한다. 문제가 없으면 아무것도 내지 않는다(문제는 한 줄씩).
nic_named_problem() {
    local pair ifname addr want have extra
    for pair in $NIC_EXPECT; do
        ifname=${pair%%=*}
        addr=${pair#*=}
        want=$(awk -v n="$ifname" '$1 == n {print $2}' "$NIC_MAC_FILE" 2>/dev/null)
        if [[ ! -e "$SYS_NET/$ifname" ]]; then echo "$ifname 없음"; continue; fi
        have=$(tr 'A-F' 'a-f' < "$SYS_NET/$ifname/address")
        [[ -n "$want" && "$have" == "$want" ]] || echo "$ifname MAC $have != ${want:-기록 없음}"
        ip -4 -o addr show dev "$ifname" | grep -q " $addr/" || echo "$ifname 에 $addr 없음"
    done
    extra=$(nic_leftover)
    [[ -z "$extra" ]] || echo "인벤토리에 없는 물리 NIC $(tr '\n' ' ' <<<"$extra")"
}

# NIC 이름에 기대는 단계(firewall, lldpd, router)의 첫 줄. 이름이 인벤토리와 다르면 아무것도 바꾸지 않고 멈춘다.
# eth 이름 위에서 이 단계들이 돌면 라우터 보호 표가 66 → 관리 SSH 를 영구히 막고(부팅 때도 올라옴), 서버 보호 표는
# 관리 NIC 에 걸리지 않아 보호가 조용히 사라진다.
require_inventory_names() {
    local problem
    problem=$(nic_named_problem)
    [[ -z "$problem" ]] || fail "NIC 이름이 인벤토리와 다름(network 단계를 먼저): $(paste -sd, - <<<"$problem")"
}

# 망 전환 되돌림(netplan-rollback 타이머와 서비스)의 상태를 한 낱말로 낸다.
#   pending  타이머가 기다리는 중(아직 발화 안 함)
#   running  되돌림 서비스가 도는 중
#   failed   되돌림 서비스가 실패한 채 남음(사람이 journalctl 을 보고 reset-failed)
#   clear    그 밖(없음, 또는 발화해 끝남. 발화한 일시 타이머는 elapsed 로 남는다)
rollback_state() {
    local timer service
    timer=$(systemctl show -p SubState --value netplan-rollback.timer 2>/dev/null || true)
    service=$(systemctl show -p ActiveState --value netplan-rollback.service 2>/dev/null || true)
    case "$service" in
        active|activating|deactivating|reloading|refreshing) echo running; return ;;
        failed) echo failed; return ;;
    esac
    case "$timer" in
        waiting|running) echo pending ;;
        *) echo clear ;;
    esac
}
