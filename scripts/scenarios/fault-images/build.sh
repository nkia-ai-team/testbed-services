#!/usr/bin/env bash
# 결함 버전 이미지 빌드: fault-images/<id 소문자>/ 의 패치를 origin/main 위에 얹어 109 docker 에
# 릴리스 태그로만 빌드한다. 노드에는 올리지 않는다(주입 직전에 k8s.image 실행기가 올리고 cleanup 이 지운다).
#
# 사용: bash scripts/scenarios/fault-images/build.sh f41-r      (testbed-services 작업 트리 어디서나)
#       BUILD_HOST=root@192.168.200.109 (기본값), REF=origin/main (기본값)
#
# 규칙(docs/spec-scenario-design-principles.md §3 예외, 시나리오 생성 하네스 "결함 버전 이미지"):
# - 앱 소스와 매니페스트, 기본 이미지는 그대로 둔다. 원본은 REF 의 도메인 폴더를 git archive 로 그대로 보낸다.
# - 결함 태그로만 붙인다. 기본 태그를 다시 붙이거나 덮어쓰지 않는다. 빌드 전후 기본 이미지 ID 가 같아야 한다.
set -euo pipefail

id="${1:?usage: build.sh <fault image dir, e.g. f41-r>}"
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(git -C "$here" rev-parse --show-toplevel)"
dir="$here/$id"
host="${BUILD_HOST:-root@192.168.200.109}"
ref="${REF:-origin/main}"

[[ -f "$dir/image.json" ]] || { echo "no $dir/image.json" >&2; exit 2; }
base=$(jq -r .base "$dir/image.json"); fault=$(jq -r .fault "$dir/image.json")
domain=$(jq -r .domain "$dir/image.json"); module=$(jq -r .module "$dir/image.json")
patch="$dir/$module.patch"
[[ -f "$patch" ]] || { echo "no $patch" >&2; exit 2; }
[[ "$base" != "$fault" && "${base%%:*}" == "${fault%%:*}" ]] || { echo "fault must be another tag of $base" >&2; exit 2; }
# 패치는 한 모듈의 src/ 안만 고친다.
if grep -E '^(\+\+\+|---) ' "$patch" | grep -vE "^(\+\+\+|---) (/dev/null|[ab]/$domain/$module/src/)" >/dev/null; then
  echo "patch touches files outside $domain/$module/src/" >&2; exit 2
fi

ssh_() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$host" "$@"; }
before=$(ssh_ docker image inspect --format '{{.Id}}' "$base")
tmp=$(ssh_ mktemp -d /tmp/fault-image-build.XXXXXX)
trap 'ssh_ find "$tmp" -delete >/dev/null 2>&1 || true' EXIT

git -C "$repo" fetch -q origin
git -C "$repo" archive --format=tar "$ref" "$domain" | ssh_ tar -x -C "$tmp"
ssh_ "cat > $tmp/release.patch" <"$patch"
ssh_ "cd $tmp && git apply -p1 release.patch && rm release.patch"
ssh_ docker build --network=host -q -f "$tmp/$domain/$module/Dockerfile" -t "$fault" "$tmp/$domain"

after=$(ssh_ docker image inspect --format '{{.Id}}' "$base")
built=$(ssh_ docker image inspect --format '{{.Id}}' "$fault")
[[ "$before" == "$after" ]] || { echo "base image $base changed: $before -> $after" >&2; exit 1; }
[[ "$built" != "$before" ]] || { echo "fault image $fault is the base image" >&2; exit 1; }
echo "built $fault ($built) on $host from $ref + $(basename "$patch"); base $base unchanged ($before)"
