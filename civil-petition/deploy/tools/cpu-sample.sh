#!/bin/sh
# /proc/stat 을 주기마다 읽어 CPU 사용률, softirq, steal 비율을 JSON 줄로 낸다(규모 축 시험 §10 T2).
# 쓰는 법: cpu-sample.sh <이름> <초 간격> <횟수>   (예: cpu-sample.sh router 10 180 > router.jsonl)
# 읽기만 한다. 출력은 표준 출력 하나이고 syslog 로 보내지 않는다.
set -eu
name="$1"
interval="${2:-10}"
count="${3:-180}"
read_cpu() {
    awk '/^cpu /{print $2, $3, $4, $5, $6, $7, $8, $9}' /proc/stat
}
set -- $(read_cpu)
p_user=$1 p_nice=$2 p_sys=$3 p_idle=$4 p_iowait=$5 p_irq=$6 p_softirq=$7 p_steal=$8
i=0
while [ "$i" -lt "$count" ]; do
    sleep "$interval"
    set -- $(read_cpu)
    d_user=$(( $1 - p_user )); d_nice=$(( $2 - p_nice )); d_sys=$(( $3 - p_sys )); d_idle=$(( $4 - p_idle ))
    d_iowait=$(( $5 - p_iowait )); d_irq=$(( $6 - p_irq )); d_softirq=$(( $7 - p_softirq )); d_steal=$(( $8 - p_steal ))
    total=$(( d_user + d_nice + d_sys + d_idle + d_iowait + d_irq + d_softirq + d_steal ))
    [ "$total" -gt 0 ] || total=1
    busy=$(( total - d_idle - d_iowait - d_steal ))
    printf '{"host":"%s","t":"%s","busy_pct":%s,"softirq_pct":%s,"steal_pct":%s}\n' \
        "$name" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        "$(awk -v a="$busy" -v b="$total" 'BEGIN{printf "%.2f", a*100/b}')" \
        "$(awk -v a="$d_softirq" -v b="$total" 'BEGIN{printf "%.2f", a*100/b}')" \
        "$(awk -v a="$d_steal" -v b="$total" 'BEGIN{printf "%.2f", a*100/b}')"
    p_user=$1 p_nice=$2 p_sys=$3 p_idle=$4 p_iowait=$5 p_irq=$6 p_softirq=$7 p_steal=$8
    i=$(( i + 1 ))
done
