# shellcheck shell=bash
# 설치 단계가 함께 쓰는 함수. install-host.sh 가 source 한다(BUNDLE, host.env 값이 이미 있다).

log() { printf '%s\n' "$*"; }
fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }

# files.tsv 에서 이 단계의 파일을 놓는다. @@SECRET:<이름>@@ 자리는 secrets/<이름> 내용으로 채운다.
place_files() {
    local step="$1" row_step dest mode owner group src
    while IFS=$'\t' read -r row_step dest mode owner group src; do
        [[ "$row_step" == "$step" ]] || continue
        install -d -m 0755 "$(dirname "$dest")"
        python3 - "$BUNDLE/$src" "$dest.new" "$BUNDLE/secrets" <<'PY'
import pathlib, re, sys
source, target, secrets = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
text = source.read_text(encoding="utf-8")
def fill(match):
    path = secrets / match.group(1)
    if not path.is_file():
        sys.exit(f"비밀값 파일 없음: {match.group(1)}")
    return path.read_text(encoding="utf-8").strip()
target.write_text(re.sub(r"@@SECRET:([a-z0-9_]+)@@", fill, text), encoding="utf-8")
PY
        chown "$owner:$group" "$dest.new"
        chmod "$mode" "$dest.new"
        mv -f "$dest.new" "$dest"
        log "placed $dest"
    done < "$BUNDLE/files.tsv"
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
