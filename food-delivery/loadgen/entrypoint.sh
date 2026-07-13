#!/bin/sh
# ============================================================
# food-delivery 지속 부하 생성기 diurnal 루프 (§8, commerce/loadgen/entrypoint.sh 이식)
# ============================================================
# 매시 현재 KST 시각 기준 사인파(코사인) 프로파일로 목표 RPS를 계산해, 그 RPS로 k6를
# constant-arrival-rate 1시간씩 반복 실행한다. python3/awk/bc에 의존하지 않는다 —
# grafana/k6 공식 이미지는 최소 구성(alpine 기반 busybox ash)이라 이런 도구가 없을 수 있어,
# 정수 산술 + 24시간 lookup 테이블(case문)만으로 구현했다.
set -eu

PEAK_RPS="${PEAK_RPS:-6}"
TROUGH_RPS="${TROUGH_RPS:-1}"
LOADGEN_SEED="${LOADGEN_SEED:-42}"
RESTAURANT_URL="${RESTAURANT_URL:-http://testbed-restaurant:8081}"
ORDER_URL="${ORDER_URL:-http://testbed-order:8080}"
DISPATCH_URL="${DISPATCH_URL:-http://testbed-dispatch:8082}"
PRE_ALLOCATED_VUS="${PRE_ALLOCATED_VUS:-10}"
MAX_VUS="${MAX_VUS:-30}"
SCRIPT_PATH="${SCRIPT_PATH:-/scripts/script.js}"

# 시(0~23) -> 코사인 곡선 위 위치를 0~100 백분율로 근사한 lookup.
# peak_hour=16(오후 낮~저녁 피크), trough_hour=4(새벽 최저) 기준 cos((h-16)*15˚)를
# 사람이 미리 계산해 박아넣었다 — 셸에 삼각함수가 없어 매 tick 계산 대신 정적 테이블 사용.
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
    hour="$1"
    pct=$(diurnal_pct "$hour")
    # 정수 산술만 사용, +50 은 반올림 보정.
    echo $(( TROUGH_RPS + ( (PEAK_RPS - TROUGH_RPS) * pct + 50 ) / 100 ))
}

echo "[loadgen] starting diurnal loop: PEAK_RPS=${PEAK_RPS} TROUGH_RPS=${TROUGH_RPS} LOADGEN_SEED=${LOADGEN_SEED} RESTAURANT_URL=${RESTAURANT_URL}"

while true; do
    HOUR=$(TZ=Asia/Seoul date +%H)
    HOUR=${HOUR#0}
    HOUR=${HOUR:-0}
    RPS=$(calc_rps "$HOUR")

    echo "[loadgen] $(date -Iseconds 2>/dev/null || date) KST_hour=${HOUR} target_rps=${RPS} — starting 1h k6 run"

    k6 run \
        --env TARGET_RPS="${RPS}" \
        --env LOADGEN_SEED="${LOADGEN_SEED}" \
        --env RESTAURANT_URL="${RESTAURANT_URL}" \
        --env ORDER_URL="${ORDER_URL}" \
        --env DISPATCH_URL="${DISPATCH_URL}" \
        --env PRE_ALLOCATED_VUS="${PRE_ALLOCATED_VUS}" \
        --env MAX_VUS="${MAX_VUS}" \
        "${SCRIPT_PATH}" \
        || echo "[loadgen] k6 run exited non-zero (rps=${RPS}), continuing to next cycle"
done
