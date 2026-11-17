#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
scenario_root="$repo_root/scripts/scenarios"
# 러너 호스트 = 66 tb-ops(192.168.122.20). 104 ~/.ssh/config 에 Host tb-ops (ProxyJump root@192.168.200.66) 별칭이 있어야 한다.
remote=${SCENARIO_REMOTE:-root@tb-ops}
remote_root=${SCENARIO_REMOTE_ROOT:-/root/testbed-services/scripts/scenarios}
# 러너 HTTP. 104 에서 ssh 없이 바로 닿는 66 호스트의 포워딩(8091 -> tb-ops). 다른 러너 호스트로
# 배포하면(SCENARIO_REMOTE) 이것도 함께 바꾼다. 운영 스킬 recording-window.sh 와 같은 변수다.
runner_api=${TESTBED_RUNNER_API:-http://192.168.200.66:8091}
runner_repo=${RCA_SCENARIO_RUNNER_REPO:-"$repo_root/../rca-scenario-runner"}

cd "$repo_root"
python3 -m unittest discover -s scripts/scenarios/tests -q
bash scripts/scenarios/test-scenarios.sh
python3 scripts/scenarios/generate-manifests.py --check

# Validate the exact external manifests against the production runner schema
# before publishing a registry entry that could block the active queue.
(
  cd "$runner_repo/backend"
  RCA_TRACE_DIR=/tmp/rca-traces uv run pytest tests -q
  # Derived from the catalog, not pinned: the pinned 64 outlived the catalog's
  # move to 60 and this assertion had been failing ever since, blocking the
  # deploy path it was meant to guard.
  uv run python -c \
    'import json;from pathlib import Path;from app.manifests import load_manifests;root=Path("../../testbed-services/scripts/scenarios");expected=len(json.loads((root/"catalog.json").read_text())["scenarios"]);manifests=load_manifests(root/"manifests");assert len({item.id for item in manifests.values()}) == expected, f"manifests {len(manifests)} != catalog {expected}"'
)

# ===== 원격 단계: 여기서부터 66 쪽(66 호스트, tb-ops, tb-runner)에 ssh 와 rsync 를 한다 =====
# 녹화 창(평시 시작 ~ 식힘 끝)과 덤프 대기 동안에는 66 쪽에 ssh 하지 않는다. 접속 줄이 녹화 syslog 에
# 담기고(v4 Q26 답 2), 덤프는 실행 끝에 적은 카탈로그 해시를 라이브 카탈로그와 다시 비교하므로
# 그 사이 배포는 녹화 하나를 잃게 한다(2026-10-10 F40-R). 판정은 러너 HTTP 로만 읽는다(ssh 없음).
# 위 검사가 몇 분 걸리고 아래 단계도 몇 분 걸리므로(결함 이미지 빌드 등), 한 번 보고 끝내지 않고
# 원격 명령마다 바로 앞에서 다시 본다. 보류면 종료 코드 3 으로 그 자리에서 멈춘다. 앞 단계만 나간
# 배포는 다음 실행이 처음부터 다시 덮는다(이미지는 있으면 건너뛰고, 나머지는 같은 내용을 다시 쓴다).
recording_guard() {
  TESTBED_RUNNER_API="$runner_api" python3 "$scenario_root/recording-guard.py" "$1"
}

# 결함 버전 이미지가 러너 호스트 docker 에 없으면 만든다. 배포가 녹화 때문에 미뤄지면 그 반복의
# build.sh 도 함께 빠져, 나중 배포에서 시나리오만 나가고 preflight 가 이미지 없음으로
# 막힌다(2026-10-11 F63-R). 이미 있는 태그는 다시 만들지 않는다.
for image_json in "$scenario_root"/fault-images/*/image.json; do
  [[ -f "$image_json" ]] || continue
  fault_tag=$(jq -r .fault "$image_json")
  recording_guard "결함 이미지 확인 $fault_tag"
  if ! ssh "$remote" docker image inspect "$fault_tag" > /dev/null 2>&1; then
    # 빌드는 몇 분 걸린다. 빌드 안의 ssh 는 이 확인 하나에 기댄다(build.sh 는 단독으로도 쓴다).
    recording_guard "결함 이미지 빌드 $fault_tag"
    BUILD_HOST="$remote" bash "$scenario_root/fault-images/build.sh" "$(basename "$(dirname "$image_json")")"
  fi
done

# Publish the load scripts the live profiles reference. 2026-07-28: this step did
# not exist, and tb-runner still held only what was hand-copied on 07-14/07-15 —
# commerce/surge.js and the three baseline script.js. Every scenario authored
# since (banking surge, frozen-bypass, transfer-heavy-surge, the slowquery trio,
# the food surge family) named a path that was not on the machine that runs it.
# Seven already-ready scenarios would have failed the batch smoke on "no such
# file". The registry is the source of truth for which paths must exist, so the
# list is derived from it rather than restated here.
loadgen_paths=$(python3 - "$scenario_root/registry/profiles.json" <<'PY'
import json, sys
profiles = json.load(open(sys.argv[1]))["profiles"]
paths = set()
for profile in profiles.values():
    contract = profile.get("parameter_contract", {})
    paths.update(contract.get("allowed_script_paths", []))
print("\n".join(sorted(paths)))
PY
)
# /opt/loadgen/<domain>/<file> mirrors <domain>/loadgen/<file> in this repo.
loadgen_repo_files=""
while read -r remote_path; do
  [[ -n "$remote_path" ]] || continue
  domain=$(basename "$(dirname "$remote_path")")
  file=$(basename "$remote_path")
  source_file="$repo_root/$domain/loadgen/$file"
  [[ -f "$source_file" ]] || { echo "load script missing from repo: $source_file" >&2; exit 1; }
  loadgen_repo_files+="$domain/loadgen/$file"$'\n'
done <<<"$loadgen_paths"

# Observation plane separation (docs/spec-scenario-observation-plane.md): the
# resident baseline units publish a per-domain live document, so tb-runner needs
# the shared monitor, the sh helper the three entrypoints source, and the
# entrypoints themselves. Without these the units keep running the old script and
# no baseline document ever appears — observations then fail closed, silently.
baseline_shared_files=(
  "scripts/scenarios/profiles/loadgen_monitor.py"
  "scripts/loadgen/baseline-publish.sh"
)
baseline_domains=(commerce core-banking food-delivery)
for domain in "${baseline_domains[@]}"; do
  loadgen_repo_files+="$domain/loadgen/entrypoint.sh"$'\n'
done
for shared in "${baseline_shared_files[@]}"; do
  [[ -f "$repo_root/$shared" ]] || { echo "baseline file missing from repo: $shared" >&2; exit 1; }
  loadgen_repo_files+="$shared"$'\n'
done

# The baseline script.js files are deliberately not shipped here — they belong to
# the resident loadgen-* units and replacing one would need a unit restart.
recording_guard "부하 스크립트 올리기"
tar czf - -C "$repo_root" $(echo "$loadgen_repo_files" | tr '\n' ' ') \
  | ssh "$remote" "cat > /tmp/loadgen-publish.tgz && \
      scp -q -i ~/.ssh/tb_key /tmp/loadgen-publish.tgz nkia@192.168.122.206:/tmp/ && \
      ssh -i ~/.ssh/tb_key nkia@192.168.122.206 '
        set -e
        rm -rf /tmp/loadgen-stage && mkdir -p /tmp/loadgen-stage
        tar xzf /tmp/loadgen-publish.tgz -C /tmp/loadgen-stage
        for src in /tmp/loadgen-stage/*/loadgen/*.js /tmp/loadgen-stage/*/loadgen/entrypoint.sh; do
          [ -f \$src ] || continue
          domain=\$(basename \$(dirname \$(dirname \$src)))
          sudo install -m 644 -o root -g root \$src /opt/loadgen/\$domain/\$(basename \$src)
        done
        sudo install -m 644 -o root -g root \
          /tmp/loadgen-stage/scripts/scenarios/profiles/loadgen_monitor.py /opt/loadgen/loadgen_monitor.py
        sudo install -m 644 -o root -g root \
          /tmp/loadgen-stage/scripts/loadgen/baseline-publish.sh /opt/loadgen/baseline-publish.sh
        rm -rf /tmp/loadgen-stage /tmp/loadgen-publish.tgz'
      rm -f /tmp/loadgen-publish.tgz"

# Verify by content, not by exit status: a silently truncated copy still exits 0.
# Every published file is checked, not just the k6 scripts — the baseline monitor
# and the entrypoints are just as load-bearing now.
verify_pairs=""
while read -r remote_path; do
  [[ -n "$remote_path" ]] || continue
  domain=$(basename "$(dirname "$remote_path")")
  file=$(basename "$remote_path")
  verify_pairs+="$remote_path|$repo_root/$domain/loadgen/$file"$'\n'
done <<<"$loadgen_paths"
for domain in "${baseline_domains[@]}"; do
  verify_pairs+="/opt/loadgen/$domain/entrypoint.sh|$repo_root/$domain/loadgen/entrypoint.sh"$'\n'
done
verify_pairs+="/opt/loadgen/loadgen_monitor.py|$repo_root/scripts/scenarios/profiles/loadgen_monitor.py"$'\n'
verify_pairs+="/opt/loadgen/baseline-publish.sh|$repo_root/scripts/loadgen/baseline-publish.sh"$'\n'

while IFS='|' read -r remote_path local_path; do
  [[ -n "$remote_path" ]] || continue
  local_sum=$(md5sum "$local_path" | cut -d' ' -f1)
  recording_guard "부하 스크립트 대조 $remote_path"
  # -n 필수: 이 고리는 표준 입력(verify_pairs)에서 줄을 읽는다. -n 없는 ssh 는 표준 입력을 끝까지 읽어
  # 원격으로 넘기므로 남은 줄을 먹고, 고리가 첫 파일 하나만 대조하고 끝난다(v4 RGUARD 2회차 재현).
  remote_sum=$(ssh -n "$remote" "ssh -i ~/.ssh/tb_key nkia@192.168.122.206 'md5sum $remote_path'" | cut -d' ' -f1)
  [[ "$local_sum" == "$remote_sum" ]] \
    || { echo "load script mismatch on tb-runner: $remote_path" >&2; exit 1; }
done <<<"$verify_pairs"

# An entrypoint on disk is not an entrypoint in effect: the resident unit keeps
# running whatever it started with. Publishing without restarting would leave a
# green md5 next to a baseline that publishes nothing. Report it rather than
# restarting here — dropping baseline traffic mid-deploy is the caller's call.
# The unit each domain's entrypoint belongs to. test_loadgen_monitor.py locks
# this same pairing against profiles.json, so it cannot drift silently.
declare -A baseline_units=(
  [commerce]=loadgen-commerce
  [core-banking]=loadgen-banking
  [food-delivery]=loadgen-food
)
for domain in "${baseline_domains[@]}"; do
  unit=${baseline_units[$domain]}
  echo "[deploy] $unit: entrypoint published — restart for it to take effect:"
  echo "[deploy]   ssh nkia@192.168.122.206 'sudo systemctl restart $unit'"
done

# 2026-07-30: service error rate moved off the APM rollup onto the trace table,
# which needs a credential the runner did not previously hold. Without it every
# clickhouse observation returns quality=error, and an unusable signal in
# must_rule_out blocks success for the 13 scenarios that read it — a whole batch
# would fail for a reason no tick record explains. Fail here instead, loudly.
recording_guard "러너 ClickHouse 자격 확인"
ssh "$remote" bash -s << 'PREFLIGHT'
set -Eeuo pipefail
container=$(docker ps --format '{{.Names}}' | grep -E 'rca-scenario-runner|scenario-runner' | head -1)
[[ -n "$container" ]] || { echo "runner container not found" >&2; exit 1; }
docker exec "$container" printenv CLICKHOUSE_PASSWORD > /dev/null 2>&1 || {
  echo "runner is missing CLICKHOUSE_PASSWORD — clickhouse.service_error_rate cannot authenticate" >&2
  echo "set CLICKHOUSE_USER/CLICKHOUSE_PASSWORD on the runner and recreate it" >&2
  exit 1
}
# Reachability and grant are separate failures; check the query the probe runs.
# -i is load-bearing: without it docker exec never attaches stdin, `python3 -`
# reads EOF, runs nothing and exits 0 — a check that always passes. Verified on
# 109 (2026-07-30): `docker exec c python3 - <<< 'print(1)'` prints nothing.
docker exec -i "$container" python3 - << 'PROBE'
import json, os, urllib.request
url = os.environ.get("CLICKHOUSE_URL", "http://192.168.230.119:18123/")
sql = (
    "SELECT count() AS n FROM lucida.otel_traces_local "
    "WHERE timestamp > now() - INTERVAL 60 SECOND FORMAT JSON"
)
request = urllib.request.Request(
    url,
    data=sql.encode(),
    headers={
        "X-ClickHouse-User": os.environ.get("CLICKHOUSE_USER", "lucida"),
        "X-ClickHouse-Key": os.environ["CLICKHOUSE_PASSWORD"],
    },
    method="POST",
)
with urllib.request.urlopen(request, timeout=10) as response:
    json.loads(response.read())["data"][0]["n"]
print("[deploy] clickhouse trace query reachable")
PROBE
PREFLIGHT

# Publish implementations and manifests before the registry exposes new live IDs.
# SOURCE_COMMIT 은 작업 트리에서 '$Format:%H$' 그대로라 보내지 않고 바로 아래에서 이 배포의 커밋을 적는다.
recording_guard "시나리오 rsync"
rsync -az --exclude registry/controllers.json --exclude SOURCE_COMMIT \
  "$scenario_root/" "$remote:$remote_root/"
recording_guard "controllers.json rsync"
rsync -az "$scenario_root/registry/controllers.json" \
  "$remote:$remote_root/registry/controllers.json"

# 녹화 meta.source_commits.testbed_services 의 근거(2026-11-18 v4 CAPMETA). tb-ops 사본은 git 이 아니라서
# 러너가 녹화에 그 커밋을 적으려면 배포가 표지를 남겨야 한다. git archive 로 올리면 export-subst 가 같은 파일에
# 커밋을 적는다(.gitattributes). scripts/scenarios 에 커밋 안 된 변경이 있으면 -dirty 를 붙인다.
source_commit=$(git -C "$repo_root" rev-parse HEAD)
if [[ -n "$(git -C "$repo_root" status --porcelain -- scripts/scenarios)" ]]; then
  source_commit+="-dirty"
fi
recording_guard "SOURCE_COMMIT 표지"
printf '%s\n' "$source_commit" |
  ssh "$remote" "cat > '$remote_root/SOURCE_COMMIT.tmp' && mv -f '$remote_root/SOURCE_COMMIT.tmp' '$remote_root/SOURCE_COMMIT'"
echo "[deploy] SOURCE_COMMIT $source_commit"

# 확인은 러너 HTTP 로 바로 읽는다(ssh 없음).
curl -fsS --max-time 15 "$runner_api/api/live-queue/readiness" && echo && curl -fsS --max-time 15 "$runner_api/api/live-queue"
