#!/bin/sh
# core-banking 상주 부하 생성기 entrypoint. commerce/loadgen/entrypoint.sh 의 diurnal 프로파일
# 패턴을 그대로 복제하되, banking 은 낮은 빈도가 현실적이라 피크/저점 RPS 를 낮게 잡는다
# (스펙 §8 결정값: 피크 4 req/s, 저점 1 req/s).
set -eu

PEAK_RPS="${PEAK_RPS:-4}"
TROUGH_RPS="${TROUGH_RPS:-1}"
LOADGEN_SEED="${LOADGEN_SEED:-42}"
GATEWAY_URL="${GATEWAY_URL:-http://testbed-nginx}"
PRE_ALLOCATED_VUS="${PRE_ALLOCATED_VUS:-10}"
MAX_VUS="${MAX_VUS:-30}"
SCRIPT_PATH="${SCRIPT_PATH:-/scripts/script.js}"

# 24시간 diurnal 프로파일(0~100%). 피크 16시, 저점 4시 — commerce loadgen 과 동일한
# "미리 계산한 코사인 근사" 방식(POSIX sh 에 삼각함수가 없어 정적 룩업으로 대체).
diurnal_pct() {
  case "$1" in
    0) echo 20 ;; 1) echo 12 ;; 2) echo 6 ;; 3) echo 2 ;; 4) echo 0 ;;
    5) echo 2 ;; 6) echo 8 ;; 7) echo 18 ;; 8) echo 32 ;; 9) echo 48 ;;
    10) echo 64 ;; 11) echo 78 ;; 12) echo 88 ;; 13) echo 94 ;; 14) echo 98 ;;
    15) echo 100 ;; 16) echo 100 ;; 17) echo 96 ;; 18) echo 88 ;; 19) echo 76 ;;
    20) echo 62 ;; 21) echo 48 ;; 22) echo 36 ;; 23) echo 28 ;;
    *) echo 50 ;;
  esac
}

calc_rps() {
  pct=$(diurnal_pct "$1")
  echo $(( TROUGH_RPS + ( (PEAK_RPS - TROUGH_RPS) * pct + 50 ) / 100 ))
}

echo "core-banking loadgen starting: PEAK_RPS=${PEAK_RPS} TROUGH_RPS=${TROUGH_RPS} SEED=${LOADGEN_SEED} GATEWAY=${GATEWAY_URL}"

while true; do
  HOUR=$(TZ=Asia/Seoul date +%H)
  HOUR=${HOUR#0}
  [ -z "$HOUR" ] && HOUR=0
  TARGET_RPS=$(calc_rps "$HOUR")
  echo "[$(date -Iseconds)] hour=${HOUR} target_rps=${TARGET_RPS}"
  k6 run \
    --env TARGET_RPS="${TARGET_RPS}" \
    --env LOADGEN_SEED="${LOADGEN_SEED}" \
    --env GATEWAY_URL="${GATEWAY_URL}" \
    --env PRE_ALLOCATED_VUS="${PRE_ALLOCATED_VUS}" \
    --env MAX_VUS="${MAX_VUS}" \
    "${SCRIPT_PATH}" || echo "[$(date -Iseconds)] k6 run failed, continuing loop"
done
