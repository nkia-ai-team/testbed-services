# SMS 에이전트(설계 §4, runbook §11). 관제 서버 SMS 수집기 이미지 안의 lucida-agent 바이너리 하나를 systemd 유닛으로
# 돌린다(기존 작업 노드와 같은 방식: 같은 유닛 꼴, root, 프로세스 이름 lucida-agent). 이름(LUCIDA_HOST)은 업무 주소,
# 보고 주소는 관제 서버 터널 주소다. 무엇을 감시할지(로그 감시 파일과 패턴)는 서버 쪽 등록(runbook §12)이 정하고
# 에이전트는 서버가 내려 준 목록만 읽는다.
# 다시 돌리면 바이너리, agent.env, 유닛이 모두 같을 때 에이전트를 다시 띄우지 않는다(멱등, 평시에 없는 재시작을 남기지 않게).
verify_artifact lucida-agent
agent_bin="$SMS_AGENT_HOME/bin/lucida-agent"
agent_log="$SMS_AGENT_HOME/logs/agent.log"

# 놓을 것(바이너리와 이 단계 파일)의 지금 체크섬을 한 줄씩 낸다. 없는 것은 "-".
sms_agent_state() {
    local row_step dest
    printf '%s %s\n' "$(sha256sum "$agent_bin" 2>/dev/null | awk '{print $1}')" "$agent_bin"
    while IFS=$'\t' read -r row_step dest _; do
        [[ "$row_step" == sms-agent ]] || continue
        printf '%s %s\n' "$(sha256sum "$dest" 2>/dev/null | awk '{print $1}')" "$dest"
    done < "$BUNDLE/files.tsv"
}
# 에이전트가 섰는지: 유닛이 돌고, root 의 lucida-agent 프로세스가 있고, 시작 줄이 기대보다 많아졌으며 마지막 시작 줄이
# 이 서버 이름과 보고 주소를 담는다.
sms_agent_started() {
    local want="$1" count last
    systemctl is-active --quiet "$SMS_AGENT_UNIT" || { echo "유닛 $SMS_AGENT_UNIT 이 돌지 않음"; return 1; }
    pgrep -x -u 0 lucida-agent >/dev/null || { echo "root 의 lucida-agent 프로세스 없음"; return 1; }
    count=$(grep -c 'agent started (' "$agent_log" 2>/dev/null || true)
    (( ${count:-0} >= want )) || { echo "시작 줄 ${count:-0}개(기대 $want 이상)"; return 1; }
    last=$(grep 'agent started (' "$agent_log" | tail -n 1)
    [[ "$last" == *"host=$SMS_AGENT_HOST,"* && "$last" == *"server=$SMS_SERVER_URL,"* ]] \
        || { echo "마지막 시작 줄의 이름이나 보고 주소가 다름: $last"; return 1; }
}

before=$(sms_agent_state)
install -d -m 0755 "$SMS_AGENT_HOME" "$SMS_AGENT_HOME/bin" "$SMS_AGENT_HOME/logs" "$SMS_AGENT_HOME/tmp"
if ! cmp -s "$BUNDLE/artifacts/lucida-agent" "$agent_bin"; then
    install -m 0755 "$BUNDLE/artifacts/lucida-agent" "$agent_bin.new"
    mv -f "$agent_bin.new" "$agent_bin"
fi
place_files sms-agent || fail "단계 sms-agent 파일을 놓지 못함"
after=$(sms_agent_state)
systemctl daemon-reload
systemctl enable "$SMS_AGENT_UNIT" >/dev/null
started=$(grep -c 'agent started (' "$agent_log" 2>/dev/null || true)
started=${started:-0}
if [[ "$before" != "$after" ]] || ! systemctl is-active --quiet "$SMS_AGENT_UNIT"; then
    systemctl restart "$SMS_AGENT_UNIT"
    want=$((started + 1))
    log "lucida-agent (re)started"
else
    want=$((started > 0 ? started : 1))
    log "lucida-agent unchanged, not restarted"
fi
settle "${SMS_AGENT_SETTLE_SEC:-60}" "$SETTLE_EVERY" "에이전트가 서지 않음" sms_agent_started "$want"
install -d -m 0755 "$(dirname "$SMS_AGENT_RECORD")"
sha256sum "$agent_bin" | awk '{print $1}' > "$SMS_AGENT_RECORD"
log "sms agent ok: $(cat "$SMS_AGENT_RECORD") host=$SMS_AGENT_HOST server=$SMS_SERVER_URL"
