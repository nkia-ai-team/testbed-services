#!/bin/sh
# 서버 한 대의 평시 안정 사실을 JSON 한 줄로 낸다(설계 §10 T2 평시 24시간: 재시작 0, OOM 0, chrony 50ms).
# 24시간 창의 처음과 끝에 한 번씩 돌려 stability_check.py --facts-before, --facts-after 에 준다. 읽기만 한다.
# 쓰는 법: host-facts.sh <유닛> [유닛 ...]   (예: host-facts.sh tomcat10.service)
set -eu
host=$(hostname)
# NRestarts 는 systemd 의 자동 재시작(Restart=)만 센다. 사람이 다시 띄웠거나 죽은 뒤 다시 띄운 것은
# 시작마다 바뀌는 InvocationID 로 잡는다(리뷰 지적 4).
restarts=""
invocations=""
for unit in "$@"; do
    n=$(systemctl show -p NRestarts --value "$unit" 2>/dev/null || echo 0)
    inv=$(systemctl show -p InvocationID --value "$unit" 2>/dev/null || echo "")
    restarts="${restarts}${restarts:+,}\"${unit}\":${n:-0}"
    invocations="${invocations}${invocations:+,}\"${unit}\":\"${inv}\""
done
oom=$(journalctl -k -b --no-pager -q 2>/dev/null | grep -c -E 'Out of memory|oom-kill' || true)
offset=$(chronyc -c tracking 2>/dev/null | awk -F, '{printf "%.3f", $5 * 1000}')
boot=$(uptime -s)
printf '{"host":"%s","boot":"%s","nrestarts":{%s},"invocations":{%s},"oom_kills":%s,"chrony_offset_ms":%s}\n' \
    "$host" "$boot" "$restarts" "$invocations" "${oom:-0}" "${offset:-null}"
