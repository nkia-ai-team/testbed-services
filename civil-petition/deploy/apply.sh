#!/usr/bin/env bash
# 인벤토리의 서버에 묶음을 보내 설치한다. 104 나 tb-ops 에서 돈다(66 을 거쳐 관리망으로 VM 에 닿는다).
# 설계: docs/spec-testbed-vm-3tier.md §2-5, 적용 순서: docs/runbook-civil-petition-apply.md.
#
#   apply.sh --inventory inventory/t.json --host <이름> [--host ...] --war <petition.war> [--k6 <k6 바이너리>]
#            [--sms-agent <lucida-agent 바이너리> [--sms-agent-sha256 <기대 sha256>]] [--package-versions <파일>] [--steps "단계 ..."]
#            [--jump root@192.168.200.66] [--dry-run]
#   --sms-agent 는 서버(웹, WAS, DB)의 sms-agent 단계가 쓴다. 모든 단계를 돌거나 --steps 에 sms-agent 가 있으면 반드시
#   준다(runbook §11: 관제 서버 SMS 수집기 이미지 안의 lucida-agent-linux-amd64, T 와 S 가 같은 파일).
#   --sms-agent-sha256 을 주면 보내기 전에 그 값과 대조한다(T 와 S 가 같은 파일인지 사람의 눈 대조에만 기대지 않게).
#   apply.sh --inventory inventory/t.json --start-load      부하기 유닛을 처음 띄운다(마지막 순서)
#
# 비밀값은 인벤토리에 적힌 파일 경로에서 셸 변수 없이 파일 그대로 묶음의 secrets/ 로 복사하고 화면에 내지 않는다.
# 설치가 모든 단계를 돌면 VM 쪽 secrets/ 는 지워진다. 로컬 임시 묶음도 끝나면 지운다.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
DOMAIN_DIR=$(cd "$HERE/.." && pwd)
REPO=$(cd "$DOMAIN_DIR/.." && pwd)

inventory=""
hosts=()
war=""
k6=""
sms_agent=""
sms_agent_sha=""
versions=""
steps=""
jump="root@192.168.200.66"
dry_run=0
start_load=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --inventory) inventory="$2"; shift 2 ;;
        --host) hosts+=("$2"); shift 2 ;;
        --war) war="$2"; shift 2 ;;
        --k6) k6="$2"; shift 2 ;;
        --sms-agent) sms_agent="$2"; shift 2 ;;
        --sms-agent-sha256) sms_agent_sha="$2"; shift 2 ;;
        --package-versions) versions="$2"; shift 2 ;;
        --steps) steps="$2"; shift 2 ;;
        --jump) jump="$2"; shift 2 ;;
        --dry-run) dry_run=1; shift ;;
        --start-load) start_load=1; shift ;;
        *) echo "apply: 모르는 인자 $1" >&2; exit 2 ;;
    esac
done
[[ -r "$inventory" ]] || { echo "apply: --inventory 필요" >&2; exit 2; }
if [[ -n "$sms_agent_sha" ]]; then
    [[ -r "$sms_agent" ]] || { echo "apply: --sms-agent-sha256 에는 --sms-agent 가 필요" >&2; exit 2; }
    have=$(sha256sum "$sms_agent" | awk '{print $1}')
    [[ "$have" == "$sms_agent_sha" ]] || { echo "apply: --sms-agent sha256 $have 가 기대값 $sms_agent_sha 와 다름" >&2; exit 2; }
fi

field() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); v=d
for k in sys.argv[2].split("."): v=v[k]
print(v)' "$inventory" "$1"; }
host_field() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1]))
h=next(x for x in d["hosts"] if x["name"]==sys.argv[2])
m=[n for n,v in d["networks"].items() if v.get("management")][0]
print(h["role"] if sys.argv[3]=="role" else next(n["addr"] for n in h["nics"] if n["net"]==m))' "$inventory" "$1" "$2"; }
expand() { local p="$1"; echo "${p/#\~/$HOME}"; }

operator=$(field accounts.operator.name)
executor=$(field accounts.executor.name)
# 새 VM 첫 접속은 키를 받아 두고(accept-new), 이 테스트베드 전용 known_hosts 에 둔다. VM 을 다시 만들면
# runbook §5 대로 ssh-keygen -R 로 옛 키를 지운다.
known_hosts="$HOME/.ssh/known_hosts_$(field domain)"
ssh_opts=(-o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new -o "UserKnownHostsFile=$known_hosts" -J "$jump")

if [[ $start_load == 1 ]]; then
    load=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(next(h["name"] for h in d["hosts"] if h["role"]=="loadgen"))' "$inventory")
    addr=$(host_field "$load" mgmt)
    cmd="sudo systemctl start loadgen.service && sleep 30 && systemctl is-active loadgen.service && ls /tmp/k6-*-live.json"
    if [[ $dry_run == 1 ]]; then echo "ssh ${ssh_opts[*]} $operator@$addr '$cmd'"; exit 0; fi
    ssh "${ssh_opts[@]}" "$operator@$addr" "$cmd"
    exit 0
fi
[[ ${#hosts[@]} -gt 0 ]] || { echo "apply: --host 필요" >&2; exit 2; }

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
python3 "$HERE/render.py" --inventory "$inventory" --out "$work/bundles" $(printf -- '--host %s ' "${hosts[@]}") >/dev/null

for host in "${hosts[@]}"; do
    role=$(host_field "$host" role)
    addr=$(host_field "$host" mgmt)
    bundle="$work/bundles/$host"
    mkdir -p "$bundle/steps.d" "$bundle/artifacts" "$bundle/secrets"
    cp "$HERE/install-host.sh" "$bundle/"
    cp "$HERE"/steps/*.sh "$bundle/steps.d/"
    [[ -n "$versions" ]] && cp "$versions" "$bundle/artifacts/package-versions.txt"
    # SMS 에이전트(설계 §4): 서버 역할 묶음이 sms-agent 단계를 돌 때만 바이너리가 있어야 한다.
    if grep -qx sms-agent "$bundle/steps" && { [[ -z "$steps" ]] || [[ " $steps " == *" sms-agent "* ]]; }; then
        [[ -r "$sms_agent" ]] || { echo "apply: $host 에 --sms-agent 필요(runbook §11)" >&2; exit 2; }
        cp "$sms_agent" "$bundle/artifacts/lucida-agent"
    fi
    install -m 0600 "$(expand "$(field accounts.executor.pubkey_file)")" "$bundle/secrets/$executor.pub"
    install -m 0600 "$(expand "$(field accounts.operator.pubkey_file)")" "$bundle/secrets/$operator.pub"
    case "$role" in
        router)
            install -m 0600 "$(expand "$(field monitoring.snmp_community_file)")" "$bundle/secrets/snmp_community" ;;
        was)
            [[ -r "$war" ]] || { echo "apply: $host 에 --war 필요" >&2; exit 2; }
            cp "$war" "$bundle/artifacts/petition.war"
            install -m 0600 "$(expand "$(field app.db_password_file)")" "$bundle/secrets/db_password" ;;
        db)
            cp "$DOMAIN_DIR/db/schema.sql" "$DOMAIN_DIR/db/seed.sql" "$bundle/artifacts/"
            install -m 0600 "$(expand "$(field app.db_password_file)")" "$bundle/secrets/db_password" ;;
        loadgen)
            [[ -r "$k6" ]] || { echo "apply: loadgen 에 --k6 필요(기존 부하기와 같은 v0.57.0 바이너리)" >&2; exit 2; }
            cp "$k6" "$bundle/artifacts/k6"
            cp "$DOMAIN_DIR/loadgen/script.js" "$DOMAIN_DIR/loadgen/entrypoint.sh" "$bundle/artifacts/"
            cp "$REPO/scripts/scenarios/profiles/loadgen_monitor.py" "$REPO/scripts/loadgen/baseline-publish.sh" "$bundle/artifacts/" ;;
    esac
    (cd "$bundle/artifacts" && find . -maxdepth 1 -type f ! -name SHA256SUMS -printf '%f\n' | sort | xargs -r sha256sum > SHA256SUMS)
    # shellcheck disable=SC2086
    remote="sudo mkdir -p /root/petition-deploy && sudo rm -rf /root/petition-deploy/$host && sudo tar -xzf - -C /root/petition-deploy && sudo bash /root/petition-deploy/$host/install-host.sh /root/petition-deploy/$host $steps"
    if [[ $dry_run == 1 ]]; then
        echo "== $host ($role) -> $operator@$addr"
        (cd "$bundle" && find . -type f | sort | grep -v '^./secrets/' )
        echo "secrets: $(ls "$bundle/secrets" | tr '\n' ' ')"
        echo "ssh ${ssh_opts[*]} $operator@$addr '$remote'"
        continue
    fi
    tar -czf - -C "$work/bundles" "$host" | ssh "${ssh_opts[@]}" "$operator@$addr" "$remote"
done
