#!/usr/bin/env bash
# 서버 하나를 묶음 하나로 설치한다. VM 안에서 root 로 돈다(적용 스크립트 apply.sh 가 부른다).
#
#   install-host.sh <묶음 디렉터리> [단계 ...]
#
# 묶음은 render.py 가 만든 host.env, steps, files.tsv, files/ 와 적용 스크립트가 넣은 steps/(설치 단계 코드),
# artifacts/(WAR, k6, DB 스키마와 시드, 부하 스크립트), secrets/(비밀값 파일) 로 이루어진다.
# 단계를 주면 그 단계만, 안 주면 묶음의 steps 순서대로 모두 돈다. 단계는 다시 돌려도 같은 상태가 된다.
# 끝나면(성공, 실패, 단계 일부만 돈 경우 모두) secrets/ 를 지운다(비밀값이 VM 의 묶음 디렉터리에 남지 않게).
set -euo pipefail

BUNDLE=$(cd "$1" && pwd)
shift
STEPS_DIR="$BUNDLE/steps.d"
[[ $EUID -eq 0 ]] || { echo "install-host: root 로 돌아야 함" >&2; exit 2; }
[[ -r "$BUNDLE/host.env" && -r "$BUNDLE/steps" && -r "$BUNDLE/files.tsv" ]] || { echo "install-host: 묶음이 아님: $BUNDLE" >&2; exit 2; }

# 비밀값은 어떤 경로로 끝나든(모든 단계, 일부 단계, 실패) 지운다. 다시 돌릴 때는 apply.sh 가 묶음째 새로 보낸다.
wipe_secrets() {
    if [[ -d "$BUNDLE/secrets" ]]; then
        find "$BUNDLE/secrets" -type f -exec shred -u {} + 2>/dev/null || true
        rm -rf "$BUNDLE/secrets"
    fi
}
trap wipe_secrets EXIT

# shellcheck source=/dev/null
. "$BUNDLE/host.env"
# shellcheck source=steps/lib.sh
. "$STEPS_DIR/lib.sh"

if [[ $# -gt 0 ]]; then
    steps=("$@")
else
    mapfile -t steps < "$BUNDLE/steps"
    # 묶음 전체를 돌리면 끝에 설치 기록(놓은 파일의 sha256)을 남긴다(steps/record.sh, 러너 버전 표류 기대값).
    steps+=(record)
fi

LOG=/var/log/petition-deploy.log
for step in "${steps[@]}"; do
    [[ -n "$step" ]] || continue
    script="$STEPS_DIR/$step.sh"
    [[ -r "$script" ]] || { echo "install-host: 단계 코드 없음: $step" >&2; exit 2; }
    echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ) $HOST_NAME $step" | tee -a "$LOG"
    set +e
    # shellcheck source=/dev/null
    ( set -euo pipefail; . "$script" ) 2>&1 | tee -a "$LOG"
    status=${PIPESTATUS[0]}
    set -e
    if [[ $status -ne 0 ]]; then
        echo "install-host: 단계 $step 실패($status)" | tee -a "$LOG" >&2
        exit "$status"
    fi
done

echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ) $HOST_NAME 끝" | tee -a "$LOG"
