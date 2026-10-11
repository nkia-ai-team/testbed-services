// ============================================================
// food-delivery delivery-status-surge 부하 (시나리오 전용) — testbed-services docs/spec-scenario-load.md
// ============================================================
// delivery-tracking-surge.js 의 파생본. 저녁 피크에 이미 주문한 손님들이 배달 상태(목록, 상세)를
// 되풀이해 확인하는 꼴만 남기고, 주문 생성과 가게 둘러보기는 넣지 않는다.
// 주문을 넣지 않는 이유: 배차 한도(dispatch.max-capacity 2000)는 동시에 배정 중인 배차 수로
// 세는데, 2026-10-11 실측으로 상주 기준선만으로도 배정 중 배차가 약 1,100~1,700 건이라
// (주문 초당 0.2~1.15건 × 배달 예정 시간 15~34분) 동반 부하가 주문을 더하면 피크 시간에는
// 몇 분 안에 'courier pool exhausted' 503 이 섞인다. 이 스크립트는 배달 조회 볼륨만 맡는다.
//
// 여정·엔드포인트·시드 데이터 계약은 food baseline(food-delivery/loadgen/script.js)의
// deliveryTrackingJourney 와 동일 — 창작하지 않는다. food-delivery엔 api-gateway가 없어
// dispatch NodePort(22-dispatch-deploy.yaml *-external Service)를 직접 호출한다.
import http from 'k6/http';
import { check } from 'k6';

const DISPATCH_URL = __ENV.DISPATCH_URL || 'http://192.168.122.77:30182';
const TARGET_RPS = Number(__ENV.TARGET_RPS || 10);
const RAMP_UP = __ENV.RAMP_UP || '2m';
const HOLD = __ENV.HOLD || '8m';
const RAMP_DOWN = __ENV.RAMP_DOWN || '1m';
const SURGE_SEED = Number(__ENV.SURGE_SEED || 4242);
const PRE_ALLOCATED_VUS = Number(__ENV.PRE_ALLOCATED_VUS || 50);
const MAX_VUS = Number(__ENV.MAX_VUS || 300);

export const options = {
    scenarios: {
        surge: {
            executor: 'ramping-arrival-rate',
            startRate: 1,
            timeUnit: '1s',
            preAllocatedVUs: PRE_ALLOCATED_VUS,
            maxVUs: MAX_VUS,
            stages: [
                { target: TARGET_RPS, duration: RAMP_UP },
                { target: TARGET_RPS, duration: HOLD },
                { target: 0, duration: RAMP_DOWN },
            ],
        },
    },
    // 폭주 중 5xx/timeout은 시나리오가 만들려는 증상 그 자체 — 실패로 run을
    // 중단하지 않고 check() 통계로만 관측한다.
    thresholds: {},
};

// 재현성: baseline과 동일한 mulberry32 + VU 분기 규약 (R4).
function mulberry32(seed) {
    let s = seed >>> 0;
    return function () {
        s = (s + 0x6d2b79f5) | 0;
        let t = Math.imul(s ^ (s >>> 15), 1 | s);
        t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
}
const rand = mulberry32(SURGE_SEED + (__VU || 0) * 7919);
const randInt = (min, max) => Math.floor(rand() * (max - min + 1)) + min;

// 시드 데이터 계약 — baseline과 동일.
const DISPATCH_ID_MAX = 20000;

function deliveryDetail() {
    const dispatchId = randInt(1, DISPATCH_ID_MAX);
    const res = http.get(`${DISPATCH_URL}/api/deliveries/${dispatchId}`,
        { tags: { journey: 'surge-delivery', step: 'detail' } });
    check(res, { 'delivery detail 200/404': (r) => r.status === 200 || r.status === 404 });
}

function deliveryList() {
    const res = http.get(`${DISPATCH_URL}/api/deliveries?status=DELIVERED&page=0&size=20`,
        { tags: { journey: 'surge-delivery', step: 'list' } });
    check(res, { 'delivery list 200': (r) => r.status === 200 });
}

// 여정 가중(누적 확률) — 추적 목록 75 / 추적 상세 25.
// 두 여정의 엔드포인트와 쿼리 문자열은 baseline deliveryTrackingJourney 와 같다.
export default function () {
    if (rand() < 0.75) {
        deliveryList();
    } else {
        deliveryDetail();
    }
}
