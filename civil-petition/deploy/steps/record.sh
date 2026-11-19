# 설치 기록: 이 묶음이 놓은 파일 가운데 비밀값을 채운 파일을 뺀 것의 sha256 을 절대 경로로 남긴다(설계 §6-4 "버전 표류",
# 러너 계약 (라), 지휘 세션 결정 11-19). 러너의 5분 평시 점검 읽기가 `cd / && sha256sum -c --quiet` 로 견주어 다르면 큐를
# 멈춘다(미끼 배포나 주입이 남긴 것). 묶음 전체를 돌린 끝에 install-host.sh 가 부르고, 이미 설치된 서버에는
# `install-host.sh <묶음> record` 로 따로 돌린다. WAR 는 was 단계의 war.sha256 이 따로 맡는다.
#
# 빼는 규칙은 렌더 산출물이 정한다: render.py 가 파일마다 meta.json 의 "secrets" 에 그 틀이 품은 비밀값 자리
# (@@SECRET:<이름>@@)를 적는다. 그 목록이 빈 파일만 기록한다. 비밀값 파일의 해시는 0644 파일에 남기지 않는다.
# meta.json 이 없거나, 어느 파일의 표시가 없거나, 표시는 비었는데 묶음 원본에 비밀값 자리가 있으면 기록하지 않고 멈춘다.
record="${PLACE_ROOT:-}${EXPECTED_RECORD:-/var/lib/petition-deploy/expected.sha256}"
install -d -m 0755 "$(dirname "$record")"
tmp="$record.new"
if ! python3 - "$BUNDLE" "${PLACE_ROOT:-}" "$tmp" <<'PY'
import hashlib, json, pathlib, sys
bundle, place, out = pathlib.Path(sys.argv[1]), sys.argv[2], pathlib.Path(sys.argv[3])
meta_path = bundle / "meta.json"
if not meta_path.is_file():
    sys.exit("record: meta.json 없음(렌더 산출물의 비밀값 표시를 읽을 수 없다)")
marks = {}
for entry in json.loads(meta_path.read_text(encoding="utf-8"))["files"]:
    if not isinstance(entry.get("secrets"), list):
        sys.exit(f"record: {entry.get('dest')} 의 비밀값 표시 없음")
    marks.setdefault(entry["dest"], set()).update(entry["secrets"])
lines, seen = [], set()
for row in (bundle / "files.tsv").read_text(encoding="utf-8").splitlines():
    if not row.strip():
        continue
    _, dest, _, _, _, src = row.split("\t")
    if dest in seen:
        continue
    seen.add(dest)
    if dest not in marks:
        sys.exit(f"record: {dest} 가 meta.json 에 없음")
    if "@@SECRET:" in (bundle / src).read_text(encoding="utf-8") and not marks[dest]:
        sys.exit(f"record: {dest} 의 원본에 비밀값 자리가 있는데 표시가 비었다")
    if marks[dest]:
        continue
    path = pathlib.Path(place + dest)
    if path.is_file():
        lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {dest}")
out.write_text("".join(line + "\n" for line in sorted(lines, key=lambda l: l.split("  ", 1)[1])), encoding="utf-8")
PY
then
    rm -f "$tmp"
    fail "설치 기록을 남기지 못함"
fi
chmod 0644 "$tmp"
mv -f "$tmp" "$record"
log "record ok $(wc -l <"$record") files"
