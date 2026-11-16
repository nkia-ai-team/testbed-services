#!/bin/sh
# ============================================================
# 민원 포털 상주 부하(설계 docs/spec-testbed-vm-3tier.md §5)
# ============================================================
# 기존 도메인과 같은 하루 모양(코사인 근사, 피크 16시, 저점 4시)으로 매시 목표 요청률을 정해 k6 를
# constant-arrival-rate 1시간씩 되풀이한다. 같은 표와 같은 정수 산술을 쓴다(commerce/loadgen/entrypoint.sh).
# 이 부하기는 관제 서버로 아무것도 보내지 않는다. 요약은 로컬 파일(/tmp/k6-<도메인>-live.json)과
# 5분 창 기록(BASELINE_MONITOR_ARGS 의 --window-log)뿐이다.
set -eu

PEAK_RPS="${PEAK_RPS:-30}"
TROUGH_RPS="${TROUGH_RPS:-5}"
LOADGEN_SEED="${LOADGEN_SEED:-42}"
ENTRY_URL="${ENTRY_URL:?ENTRY_URL required}"
DOMAIN="${DOMAIN:?DOMAIN required}"
PRE_ALLOCATED_VUS="${PRE_ALLOCATED_VUS:-40}"
MAX_VUS="${MAX_VUS:-150}"
SCRIPT_PATH="${SCRIPT_PATH:-/opt/loadgen/${DOMAIN}/script.js}"

diurnal_pct() {
    case "$1" in
        0) echo 25 ;;  1) echo 15 ;;  2) echo 7 ;;   3) echo 2 ;;
        4) echo 0 ;;   5) echo 2 ;;   6) echo 7 ;;   7) echo 15 ;;
        8) echo 25 ;;  9) echo 37 ;; 10) echo 50 ;;  11) echo 63 ;;
        12) echo 75 ;; 13) echo 85 ;; 14) echo 93 ;; 15) echo 98 ;;
        16) echo 100 ;; 17) echo 98 ;; 18) echo 93 ;; 19) echo 85 ;;
        20) echo 75 ;; 21) echo 63 ;; 22) echo 50 ;; 23) echo 37 ;;
        *) echo 50 ;;
    esac
}

calc_rps() {
    pct=$(diurnal_pct "$1")
    echo $(( TROUGH_RPS + ( (PEAK_RPS - TROUGH_RPS) * pct + 50 ) / 100 ))
}

echo "[loadgen] starting diurnal loop: PEAK_RPS=${PEAK_RPS} TROUGH_RPS=${TROUGH_RPS} LOADGEN_SEED=${LOADGEN_SEED} ENTRY_URL=${ENTRY_URL}"

BASELINE_PUBLISH="${BASELINE_PUBLISH:-/opt/loadgen/baseline-publish.sh}"
BASELINE_K6_OUT=""
if [ -r "$BASELINE_PUBLISH" ]; then
    . "$BASELINE_PUBLISH"
    baseline_publisher_start "$DOMAIN" loadgen submit lookup
fi

while true; do
    HOUR=$(TZ=Asia/Seoul date +%H)
    HOUR=${HOUR#0}
    HOUR=${HOUR:-0}
    RPS=$(calc_rps "$HOUR")
    echo "[loadgen] $(date -Iseconds 2>/dev/null || date) KST_hour=${HOUR} target_rps=${RPS} starting 1h k6 run"
    # shellcheck disable=SC2086 # BASELINE_K6_OUT 은 비어 있으면 인자 자체가 사라져야 한다
    k6 run --quiet \
        ${BASELINE_K6_OUT:+--out "$BASELINE_K6_OUT"} \
        --env TARGET_RPS="${RPS}" \
        --env DURATION=1h \
        --env LOADGEN_SEED="${LOADGEN_SEED}" \
        --env ENTRY_URL="${ENTRY_URL}" \
        --env PRE_ALLOCATED_VUS="${PRE_ALLOCATED_VUS}" \
        --env MAX_VUS="${MAX_VUS}" \
        "${SCRIPT_PATH}" \
        || echo "[loadgen] k6 run exited non-zero (rps=${RPS}), continuing to next cycle"
done
