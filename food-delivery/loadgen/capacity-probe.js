// ============================================================
// food-delivery 용량 탐색 프로브 (1회성 계측 도구) — docs/spec-scenario-load.md §8
// ============================================================
// commerce/loadgen/capacity-probe.js 와 동일한 계단식 정률 부하 패턴.
// 여정 믹스·시드 계약은 baseline(script.js)과 동일 — 브라우징 55% / 지역 검색 15% /
// 주문 생성 20% / 배달 상태 조회 10%.
//
// food 특유의 함정: 배차 포화 거절이 503(5xx 코드지만 설계된 업무 거절 — commerce의
// 재고 409에 해당)이다. 503을 실패로 세면 배차 풀 크기를 인프라 무릎으로 오독하므로,
// 503은 expected로 두고 별도 카운터(biz_reject_503)로 스텝별 분리 집계한다.
// http_req_failed = 503 제외 5xx/timeout = 진짜 가용성 장애.
import http from 'k6/http';
import { check } from 'k6';
import { Counter } from 'k6/metrics';

const RESTAURANT_URL = __ENV.RESTAURANT_URL || 'http://192.168.122.77:30181';
const ORDER_URL = __ENV.ORDER_URL || 'http://192.168.122.77:30180';
const DISPATCH_URL = __ENV.DISPATCH_URL || 'http://192.168.122.77:30182';
const STEP_RPS = (__ENV.STEP_RPS || '15,30,45,60,80,100').split(',').map(Number);
const STEP_HOLD_SEC = Number(__ENV.STEP_HOLD_SEC || 180);
const STEP_GAP_SEC = Number(__ENV.STEP_GAP_SEC || 60);
const SURGE_SEED = Number(__ENV.SURGE_SEED || 4242);

http.setResponseCallback(http.expectedStatuses({ min: 200, max: 499 }, 503));
const bizReject503 = new Counter('biz_reject_503');

const scenarios = {};
const thresholds = {};
let offsetSec = 0;
for (const rps of STEP_RPS) {
    const name = `s${rps}`;
    scenarios[name] = {
        executor: 'constant-arrival-rate',
        rate: rps,
        timeUnit: '1s',
        duration: `${STEP_HOLD_SEC}s`,
        startTime: `${offsetSec}s`,
        preAllocatedVUs: Math.min(rps * 2, 200),
        maxVUs: rps * 6,
        exec: 'mix',
    };
    thresholds[`http_req_duration{scenario:${name}}`] = [];
    thresholds[`http_req_failed{scenario:${name}}`] = [];
    thresholds[`dropped_iterations{scenario:${name}}`] = [];
    thresholds[`biz_reject_503{scenario:${name}}`] = [];
    offsetSec += STEP_HOLD_SEC + STEP_GAP_SEC;
}

export const options = { scenarios, thresholds };

// ----- 이하 여정은 baseline script.js 와 동일 계약 -----
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
const pick = (arr) => arr[randInt(0, arr.length - 1)];

function randomRestaurantId() {
    return rand() < 3.0 / 23 ? randInt(1, 3) : randInt(16, 35);
}
const REGIONS = ['GANGNAM', 'HONGDAE', 'JAMSIL', 'YEOUIDO', 'MAPO', 'SONGPA', 'NOWON', 'GURO'];
const CUSTOMER_ID_MAX = 2000;
const DISPATCH_ID_MAX = 20000;

const customerId = () => `cust-${randInt(1, CUSTOMER_ID_MAX)}`;

function browsing() {
    const restaurantId = randomRestaurantId();
    const detailRes = http.get(`${RESTAURANT_URL}/api/restaurants/${restaurantId}`,
        { tags: { journey: 'browsing' } });
    check(detailRes, { 'browse 200': (r) => r.status === 200 });
    http.get(`${RESTAURANT_URL}/api/restaurants/${restaurantId}/menu`, { tags: { journey: 'browsing' } });
    if (rand() < 0.3) {
        http.get(`${RESTAURANT_URL}/api/restaurants/${restaurantId}/popular-menu`, { tags: { journey: 'browsing' } });
    }
}

function search() {
    const region = pick(REGIONS);
    const res = http.get(
        `${RESTAURANT_URL}/api/restaurants?region=${region}&status=OPEN&page=${randInt(0, 2)}&size=20`,
        { tags: { journey: 'search' } });
    check(res, { 'search 200': (r) => r.status === 200 });
}

function order() {
    const restaurantId = randomRestaurantId();
    const menuRes = http.get(`${RESTAURANT_URL}/api/restaurants/${restaurantId}/menu`,
        { tags: { journey: 'order' } });
    if (menuRes.status !== 200) {
        return;
    }
    let menu;
    try {
        menu = menuRes.json();
    } catch (e) {
        return;
    }
    const available = (menu || []).filter((m) => m.available);
    if (available.length === 0) {
        return;
    }
    const itemCount = Math.min(available.length, randInt(1, 2));
    const items = [];
    for (let i = 0; i < itemCount; i++) {
        const m = pick(available);
        items.push({ menuId: m.id, qty: randInt(1, 2), unitPrice: m.price });
    }
    const orderRes = http.post(
        `${ORDER_URL}/api/orders`,
        JSON.stringify({ customerId: customerId(), restaurantId, items }),
        { headers: { 'Content-Type': 'application/json' }, tags: { journey: 'order' } }
    );
    if (orderRes.status === 503) {
        bizReject503.add(1);
    }
    check(orderRes, { 'order not hard-5xx': (r) => r.status < 500 || r.status === 503 });
}

function tracking() {
    if (rand() < 0.5) {
        const res = http.get(`${DISPATCH_URL}/api/deliveries/${randInt(1, DISPATCH_ID_MAX)}`,
            { tags: { journey: 'tracking' } });
        check(res, { 'tracking ok': (r) => r.status === 200 || r.status === 404 });
    } else {
        http.get(`${DISPATCH_URL}/api/deliveries?status=DELIVERED&page=0&size=20`, { tags: { journey: 'tracking' } });
    }
}

export function mix() {
    const r = rand();
    if (r < 0.55) {
        browsing();
    } else if (r < 0.7) {
        search();
    } else if (r < 0.9) {
        order();
    } else {
        tracking();
    }
}
