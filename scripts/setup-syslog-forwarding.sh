#!/usr/bin/env bash
# 테스트베드 노드의 OS 레벨 syslog 를 lucida-next collector-syslog(119:514/udp)로 전달한다.
#
# 왜 필요한가: 캡처 계약 v3 의 12 테이블 중 하나가 `syslog_local` 인데
# 2026-07-30 확인 시점까지 **행이 0 개**였다. 수신기는 멀쩡했다 — 119 에서
# UDP 514 를 열고 대기 중이었고, 등록되지 않은 송신 IP 도 버리지 않고
# `registered=0` 으로 적재한다(collector-syslog/syslog/receiver.go:342 주석
# "미해소 = 미등록 표시(드롭 아님)"). 없던 것은 **보내는 쪽**이다. 그래서
# 지금까지 만든 모든 평가 케이스에서 이 상자가 비어 있었다.
#
# 왜 스크립트인가: 같은 날 Oracle 의 lucida_mon 계정이 노드 이사 한 번에
# 사라져 DPM 이 36 시간 멈췄다. 런북에 경고가 있었지만 문장이라 실행되지
# 않았다. 노드를 다시 만들면 /etc/rsyslog.d 도 함께 사라지므로, 배선을
# 문장이 아니라 실행 가능한 형태로 남긴다.
#
# 멱등하다. 설정이 이미 같으면 rsyslog 를 건드리지 않는다.
#
# 사용:
#   bash scripts/setup-syslog-forwarding.sh          # 적용
#   bash scripts/setup-syslog-forwarding.sh --check  # 현재 상태만 확인(변경 없음)
set -Eeuo pipefail

collector_host=${SYSLOG_COLLECTOR_HOST:-192.168.230.119}
collector_port=${SYSLOG_COLLECTOR_PORT:-514}
gb10=${GB10_SSH:-root@192.168.200.109}
# 109 위에서 해석되는 경로다. 여기서 ~ 를 쓰면 .104 의 홈으로 먼저 확장돼 버린다.
tb_key=${TB_SSH_KEY:-/root/.ssh/tb_key}
conf_path=/etc/rsyslog.d/60-lucida-forward.conf

mode=apply
[[ "${1:-}" == "--check" ]] && mode=check

# 대상 = RCA 가 노드 레벨 사건을 물어야 하는 곳 전부.
#   109        KVM 호스트. 여기서 난 일은 모든 VM 에 번진다.
#   tb-cp      control-plane
#   tb-w1/2/3  앱과 DB 가 실제로 도는 워커. OOMKill·디스크·커널 메시지의 출처라
#              F26 계열(노드 상실·PV·eviction) 판정의 직접 증거가 된다.
#   tb-runner  부하 조종석. 유닛 재기동이 baseline 공백을 설명한다.
# 119 는 넣지 않는다 — 수집기 자신이라 자기 로그를 자기에게 보내는 되먹임이 되고,
# lucida 는 selfsystem 수집기를 따로 갖고 있다.
tb_nodes=(
  "192.168.122.77|tb-cp"
  "192.168.122.184|tb-w1"
  "192.168.122.11|tb-w2"
  "192.168.122.14|tb-w3"
  "192.168.122.206|tb-runner"
)

# omfwd + linkedlist 큐: 수집기가 잠깐 죽어도 노드의 로깅이 막히지 않는다.
# 장애 주입 중에 노드가 로깅 때문에 느려지면 그 자체가 측정 오염이다.
read -r -d '' conf_body <<EOF || true
# lucida-next collector-syslog 전달 (scripts/setup-syslog-forwarding.sh 가 생성)
# 정본 = testbed-services 저장소. 손으로 고치지 말 것 — 노드 재생성 시 유실된다.
*.* action(type="omfwd"
           target="${collector_host}" port="${collector_port}" protocol="udp"
           queue.type="linkedlist" queue.size="10000"
           action.resumeRetryCount="-1")
EOF

# 원격에서 실행할 본문. 설정이 이미 동일하면 재기동하지 않는다(멱등).
remote_script() {
  cat <<REMOTE
set -Eeuo pipefail
want=\$(cat <<'CONF'
${conf_body}
CONF
)
if [ "${mode}" = check ]; then
  if [ -f "${conf_path}" ] && [ "\$(cat ${conf_path})" = "\$want" ]; then
    echo "OK 설정 일치 / rsyslog=\$(systemctl is-active rsyslog)"
  elif [ -f "${conf_path}" ]; then
    echo "DRIFT 설정 다름 / rsyslog=\$(systemctl is-active rsyslog)"
  else
    echo "MISSING 설정 없음 / rsyslog=\$(systemctl is-active rsyslog)"
  fi
  exit 0
fi
if [ -f "${conf_path}" ] && [ "\$(cat ${conf_path})" = "\$want" ]; then
  echo "변경 없음(이미 동일)"
  exit 0
fi
printf '%s\n' "\$want" | sudo tee ${conf_path} >/dev/null
sudo chmod 644 ${conf_path}
sudo rsyslogd -N1 >/dev/null 2>&1 || { echo "rsyslog 설정 검증 실패 — 적용 취소" >&2; sudo rm -f ${conf_path}; exit 1; }
sudo systemctl restart rsyslog
sudo logger -t lucida-syslog-setup "syslog forwarding to ${collector_host}:${collector_port} enabled"
echo "적용 완료 / rsyslog=\$(systemctl is-active rsyslog)"
REMOTE
}

printf '[syslog] 수집기=%s:%s  모드=%s\n\n' "$collector_host" "$collector_port" "$mode"

printf '%-22s ' "gb10(109)"
remote_script | ssh -o BatchMode=yes "$gb10" 'bash -s'

for entry in "${tb_nodes[@]}"; do
  ip=${entry%%|*}
  name=${entry##*|}
  printf '%-22s ' "$name($ip)"
  remote_script | ssh -o BatchMode=yes "$gb10" \
    "ssh -i ${tb_key} -o BatchMode=yes -o ConnectTimeout=10 nkia@${ip} 'bash -s'"
done

printf '\n[syslog] 도착 확인은 119 에서:\n'
printf "  docker exec lucida-clickhouse clickhouse-client -q \\\\\n"
printf "    \"select source_ip, count() from lucida.syslog_local group by source_ip\"\n"
