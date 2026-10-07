# shellcheck shell=sh
# ============================================================
# baseline 라이브 문서 발행 헬퍼 (POSIX sh)
# ============================================================
# 상주 loadgen 유닛이 k6 샘플을 FIFO로 흘리고, loadgen_monitor.py가 이를 파싱해
# 도메인 문서 /tmp/rca-baseline-<domain>-live.json 을 계속 갱신하게 한다.
# 설계 정본 = docs/spec-scenario-observation-plane.md
#
# 세 도메인 entrypoint가 이 파일을 source 한다 — 같은 20줄을 세 번 복사하지 않는다.
#
# **부하는 관측보다 우선한다.** 발행을 시작하지 못해도 baseline 트래픽은 계속 흘러야 하므로
# 이 헬퍼는 실패 시 요란하게 로그만 남기고 성공을 가장하지 않는다. 문서가 생기지 않으면
# 러너 쪽 관측은 신선도 계약에 걸려 fail-closed로 거절된다 — 조용한 오판보다 낫다.

BASELINE_MONITOR="${BASELINE_MONITOR:-/opt/loadgen/loadgen_monitor.py}"
BASELINE_STATE_DIR="${BASELINE_STATE_DIR:-/tmp}"

# 발행을 시작하고, k6에 붙일 --out 인자를 BASELINE_K6_OUT 에 설정한다.
# 실패하면 BASELINE_K6_OUT 은 빈 문자열로 남아 k6가 FIFO 없이 정상 기동한다.
#   사용: baseline_publisher_start <domain> <unit> <business_step> [read_step]
baseline_publisher_start() {
    baseline_domain=$1
    baseline_unit=$2
    baseline_business_step=$3
    baseline_read_step=${4:-}
    BASELINE_K6_OUT=""

    baseline_fifo="${BASELINE_STATE_DIR}/rca-baseline-${baseline_domain}-samples.fifo"
    baseline_live="${BASELINE_STATE_DIR}/rca-baseline-${baseline_domain}-live.json"

    if ! command -v python3 >/dev/null 2>&1; then
        echo "[loadgen] WARN python3 없음 — baseline 관측 문서를 발행하지 않는다" >&2
        return 0
    fi
    if [ ! -r "$BASELINE_MONITOR" ]; then
        echo "[loadgen] WARN monitor 없음 ($BASELINE_MONITOR) — baseline 관측 문서를 발행하지 않는다" >&2
        return 0
    fi

    rm -f "$baseline_fifo"
    if ! mkfifo -m 600 "$baseline_fifo" 2>/dev/null; then
        echo "[loadgen] WARN FIFO 생성 실패 ($baseline_fifo) — 관측 문서를 발행하지 않는다" >&2
        return 0
    fi

    python3 "$BASELINE_MONITOR" \
        --source "$baseline_fifo" \
        --output "$baseline_live" \
        --business-step "$baseline_business_step" \
        --read-step "$baseline_read_step" \
        --mode stream \
        --domain "$baseline_domain" \
        --unit "$baseline_unit" &
    BASELINE_PUBLISHER_PID=$!

    # 유닛이 멈추면 문서를 남기지 않는다 — 남으면 정지된 baseline을 살아 있는 것으로
    # 오독할 수 있다. 신선도 계약이 걸러주긴 하지만 흔적 자체를 지우는 편이 안전하다.
    trap 'baseline_publisher_stop' EXIT INT TERM

    BASELINE_K6_OUT="json=${baseline_fifo}"
    echo "[loadgen] baseline 관측 발행 시작: domain=${baseline_domain} live=${baseline_live} pid=${BASELINE_PUBLISHER_PID}"
}

baseline_publisher_stop() {
    if [ -n "${BASELINE_PUBLISHER_PID:-}" ]; then
        kill "$BASELINE_PUBLISHER_PID" 2>/dev/null || true
    fi
    [ -n "${baseline_fifo:-}" ] && rm -f "$baseline_fifo"
    [ -n "${baseline_live:-}" ] && rm -f "$baseline_live"
    return 0
}
