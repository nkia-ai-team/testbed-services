# shellcheck shell=bash
# 설치 단계가 함께 쓰는 함수. install-host.sh 가 source 한다(BUNDLE, host.env 값이 이미 있다).

log() { printf '%s\n' "$*"; }
fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }

# NIC 를 찾는 곳. 시험은 다른 경로를 준다.
SYS_NET=${SYS_NET:-/sys/class/net}
# 설치 단계 network 가 적는 "인벤토리 NIC 이름 MAC" 줄. place_files 가 @@MAC:<이름>@@ 자리를 이것으로 채운다.
NIC_MAC_FILE=${NIC_MAC_FILE:-/var/lib/petition-deploy/nic-macs}

# rsyslog 설정이 있는 곳(/etc/rsyslog.conf, rsyslog.d, rsyslog.remote.d 의 부모). 시험은 다른 경로를 준다.
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

# 이 단계의 파일(rsyslog 설정 포함)을 놓되, rsyslog 가 못 읽을 설정은 /etc 에 남기지 않는다.
#   1. 지금 rsyslog 설정(rsyslog.conf, rsyslog.d, rsyslog.remote.d)을 임시 디렉터리에 베끼고 이 단계의 파일을 그 위에 놓은 뒤,
#      사본 rsyslog.conf 의 포함 경로를 사본으로 바꿔 rsyslogd -N1 로 본다. 실패하면 /etc 는 건드리지 않고 멈춘다.
#   2. 통과하면 이 단계 파일의 지금 내용을 남겨 두고 진짜로 놓은 뒤 진짜 설정을 rsyslogd -N1 로 다시 본다.
#      실패하면 이 단계가 놓은 파일을 앞 내용으로 되돌리고(없던 파일은 지운다) 멈춘다.
# 어느 쪽이든 실패하면 rsyslog 를 재시작하지 않으므로 돌던 rsyslog 는 앞 설정 그대로다(재시작하는 것은 부른 단계).
rsyslog_place_checked() {
    local step="$1" stage out row_step dest dir i=0 kept=()
    stage=$(mktemp -d) || fail "rsyslog 검사 디렉터리를 못 만듦"
    mkdir -p "$stage$RSYSLOG_ETC/rsyslog.d" "$stage$RSYSLOG_ETC/rsyslog.remote.d" "$stage/keep"
    [[ ! -e "$RSYSLOG_ETC/rsyslog.conf" ]] || cp -p "$RSYSLOG_ETC/rsyslog.conf" "$stage$RSYSLOG_ETC/rsyslog.conf"
    for dir in rsyslog.d rsyslog.remote.d; do
        [[ ! -d "$RSYSLOG_ETC/$dir" ]] || cp -a "$RSYSLOG_ETC/$dir/." "$stage$RSYSLOG_ETC/$dir/"
    done
    if ! PLACE_ROOT="$stage" place_files "$step"; then
        rm -rf "$stage"; fail "단계 $step 파일을 검사 사본에 못 놓음"
    fi
    out=$(python3 - "$stage$RSYSLOG_ETC/rsyslog.conf" "$RSYSLOG_ETC" "$stage$RSYSLOG_ETC" <<'PY' 2>&1
import pathlib, sys
conf, real, staged = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
text = conf.read_text(encoding="utf-8")
for name in ("rsyslog.d/", "rsyslog.remote.d/"):
    text = text.replace(f"{real}/{name}", f"{staged}/{name}")
conf.write_text(text, encoding="utf-8")
PY
) || { rm -rf "$stage"; fail "rsyslog 검사 사본을 못 만듦: $out"; }
    if ! out=$(rsyslogd -N1 -f "$stage$RSYSLOG_ETC/rsyslog.conf" 2>&1); then
        printf '%s\n' "$out" >&2
        rm -rf "$stage"
        fail "rsyslog 설정 검사(놓기 전, 단계 $step). $RSYSLOG_ETC 는 바꾸지 않음"
    fi
    while IFS=$'\t' read -r row_step dest _; do
        [[ "$row_step" == "$step" ]] || continue
        i=$((i + 1))
        if [[ -e "$dest" ]]; then cp -p "$dest" "$stage/keep/$i" || { rm -rf "$stage"; fail "앞 내용을 못 남김: $dest"; }; fi
        kept+=("$dest")
    done < "$BUNDLE/files.tsv"
    if place_files "$step" && out=$(rsyslogd -N1 -f "$RSYSLOG_ETC/rsyslog.conf" 2>&1); then
        rm -rf "$stage"
        return 0
    fi
    printf '%s\n' "$out" >&2
    i=0
    for dest in "${kept[@]}"; do
        i=$((i + 1))
        if [[ -e "$stage/keep/$i" ]]; then cp -p "$stage/keep/$i" "$dest"; else rm -f "$dest"; fi
        rm -f "$dest.new"
    done
    rm -rf "$stage"
    fail "rsyslog 설정 검사(놓은 뒤, 단계 $step). 놓은 파일을 앞 내용으로 되돌림"
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
    printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
    chmod 0755 /usr/sbin/policy-rc.d
    if ! DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends "${wanted[@]}"; then
        rm -f /usr/sbin/policy-rc.d
        return 1
    fi
    rm -f /usr/sbin/policy-rc.d
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
