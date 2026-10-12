// ============================================================
// food-delivery 배달원 정산 내역 조회 부하 (F70-P 전용) — testbed-services docs/scenario-redesign-wip/design-F70-P-sheet.md
// ============================================================
// baseline loadgen(food-delivery/loadgen/script.js, 불가침 — R1)과 별개로, 시나리오가
// 켰다 끄는 부하다. 두 갈래를 함께 돌린다.
//
//   1) journeys: delivery-status-surge.js 와 같은 배달 조회 여정(목록 75 / 상세 25, 엔드포인트와
//      질의 문자열은 baseline deliveryTrackingJourney 와 같고 목록은 늘 page=0&size=20)을
//      TARGET_RPS 만큼 흘린다. 주문 생성과 가게 둘러보기는 넣지 않는다: 배차 한도
//      (dispatch.max-capacity 2000)를 상주 기준선만으로도 약 1,100~1,700 건 쓰고 있어
//      주문을 더하면 피크에 'courier pool exhausted' 503 이 섞인다(delivery-status-surge.js 주석).
//   2) settlement_export: 배달원 정산 시스템(바깥 파트너) 하나가 배달 완료 내역을 받아 가는
//      조회를 흉내 낸다. 배달 목록 API 의 공개 계약(page/size 는 선택, 둘 다 빼면 전부 반환)대로
//      page 없이 GET /api/deliveries?status=DELIVERED 를 부르고, 응답을 받든 실패하든
//      EXPORT_PAUSE 뒤 다시 부른다. 한 번에 한 건(VU 1)이다.
//      step 태그는 'export' 라 러너 monitor 의 업무, 조회 집계에 들어가지 않는다.
//
// food-delivery 엔 api-gateway 가 없어 dispatch NodePort(22-dispatch-deploy.yaml *-external
// Service)를 직접 부른다. ramping-arrival-rate + mulberry32 시드 재현성(R4)은 surge.js 와 같다.
import http from 'k6/http';
import { check, sleep } from 'k6';

const DISPATCH_URL = __ENV.DISPATCH_URL || 'http://192.168.122.77:30182';
const TARGET_RPS = Number(__ENV.TARGET_RPS || 5);
const RAMP_UP = __ENV.RAMP_UP || '2m';
const HOLD = __ENV.HOLD || '13m';
const RAMP_DOWN = __ENV.RAMP_DOWN || '1m';
const SURGE_SEED = Number(__ENV.SURGE_SEED || 4242);
const PRE_ALLOCATED_VUS = Number(__ENV.PRE_ALLOCATED_VUS || 30);
const MAX_VUS = Number(__ENV.MAX_VUS || 250);
// 정산 시스템의 재조회 간격. 실패해도 같은 간격으로 다시 부른다.
const EXPORT_PAUSE_SEC = Number(__ENV.EXPORT_PAUSE_SEC || 60);

export const options = {
    scenarios: {
        journeys: {
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
        settlement_export: {
            executor: 'constant-vus',
            exec: 'settlementExport',
            vus: 1,
            startTime: RAMP_UP,
            duration: HOLD,
            gracefulStop: '0s',
        },
    },
    // 장애 중 5xx/timeout 은 이 시나리오가 만들려는 증상 그 자체 — check() 로만 관측한다.
    thresholds: {},
};

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

// 시드 데이터 계약 — baseline 과 동일.
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

// 여정 가중(누적 확률) — delivery-status-surge.js 와 같다: 추적 목록 75 / 추적 상세 25.
export default function () {
    if (rand() < 0.75) {
        deliveryList();
    } else {
        deliveryDetail();
    }
}

export function settlementExport() {
    const res = http.get(`${DISPATCH_URL}/api/deliveries?status=DELIVERED`, {
        timeout: '90s',
        tags: { journey: 'settlement-export', step: 'export' },
    });
    check(res, { 'settlement export 200': (r) => r.status === 200 });
    sleep(EXPORT_PAUSE_SEC);
}
