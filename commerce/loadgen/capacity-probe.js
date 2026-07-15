// ============================================================
// commerce 용량 탐색 프로브 (1회성 계측 도구) — docs/spec-scenario-load.md
// ============================================================
// 임계형 시나리오(L1 계열)의 강도 캘리브레이션을 시나리오마다 반복하지 않도록,
// 체크아웃 경로의 포화점(절대 RPS)을 계단식 정률 부하로 1회 실측한다.
// 여정 믹스·시드 계약은 surge.js와 동일해야 한다 — 같은 믹스로 재야
// 시나리오 강도 설계에 그대로 쓸 수 있다.
//
// 스텝별 결과는 per-scenario threshold 등록으로 k6 요약에 분리 표기된다.
// 5xx/timeout만 실패로 집계한다(4xx 재고·쿠폰 실패는 여정 내 정상 — surge.js와 동일).
import http from 'k6/http';
import { check } from 'k6';

const GATEWAY_URL = __ENV.GATEWAY_URL || 'http://192.168.122.77:30080';
const STEP_RPS = (__ENV.STEP_RPS || '30,40,50,60,70,80').split(',').map(Number);
const STEP_HOLD_SEC = Number(__ENV.STEP_HOLD_SEC || 180);
const STEP_GAP_SEC = Number(__ENV.STEP_GAP_SEC || 60);
const SURGE_SEED = Number(__ENV.SURGE_SEED || 4242);

http.setResponseCallback(http.expectedStatuses({ min: 200, max: 499 }));

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
        // 포화 구간에서 latency가 수 초로 늘어도 offered load가 유지되도록 넉넉히 —
        // 그래도 dropped_iterations가 나오면 그 자체가 포화 신호다.
        preAllocatedVUs: Math.min(rps * 2, 200),
        maxVUs: rps * 6,
        exec: 'mix',
    };
    thresholds[`http_req_duration{scenario:${name}}`] = [];
    thresholds[`http_req_failed{scenario:${name}}`] = [];
    thresholds[`dropped_iterations{scenario:${name}}`] = [];
    offsetSec += STEP_HOLD_SEC + STEP_GAP_SEC;
}

export const options = { scenarios, thresholds };

// ----- 이하 여정은 surge.js와 동일 계약 (mulberry32·데모 유저 20·flagship 16) -----
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

const DEMO_USER_COUNT = 20;
const SEED_PASSWORD = 'Passw0rd!';
const FLAGSHIP_PRODUCT_IDS = Array.from({ length: 16 }, (_, i) => i + 1);
const COUPON_CODES = [null, null, null, 'SAVE10', 'WELCOME5000'];
const userEmail = (id) => `user${String(id).padStart(2, '0')}@example.com`;
const tokenCache = {};

function login(userId) {
    const cached = tokenCache[userId];
    if (cached && cached.expiresAtMs > Date.now() + 60000) {
        return cached.token;
    }
    const res = http.post(
        `${GATEWAY_URL}/api/users/login`,
        JSON.stringify({ email: userEmail(userId), password: SEED_PASSWORD }),
        { headers: { 'Content-Type': 'application/json' }, tags: { journey: 'surge-checkout', step: 'login' } }
    );
    if (!check(res, { 'login 200': (r) => r.status === 200 })) {
        return null;
    }
    const body = res.json();
    tokenCache[userId] = { token: body.token, expiresAtMs: Date.parse(body.expiresAt) || Date.now() + 3600000 };
    return body.token;
}

const authHeaders = (token) => ({ 'Content-Type': 'application/json', Authorization: `Bearer ${token}` });

function browsing() {
    const page = randInt(0, 50);
    const res = http.get(`${GATEWAY_URL}/api/products?page=${page}&size=20`, {
        tags: { journey: 'surge-browsing', step: 'list' },
    });
    check(res, { 'browse 200': (r) => r.status === 200 });
    if (rand() < 0.5) {
        const id = pick(FLAGSHIP_PRODUCT_IDS);
        http.get(`${GATEWAY_URL}/api/products/${id}`, { tags: { journey: 'surge-browsing', step: 'detail' } });
    }
}

function cart() {
    const userId = randInt(1, DEMO_USER_COUNT);
    const res = http.get(`${GATEWAY_URL}/api/carts/${userId}`, { tags: { journey: 'surge-cart', step: 'get' } });
    check(res, { 'cart 200': (r) => r.status === 200 });
    http.post(
        `${GATEWAY_URL}/api/carts/${userId}/items`,
        JSON.stringify({ productId: pick(FLAGSHIP_PRODUCT_IDS), quantity: randInt(1, 2) }),
        { headers: { 'Content-Type': 'application/json' }, tags: { journey: 'surge-cart', step: 'add' } }
    );
}

function checkout() {
    const userId = randInt(1, DEMO_USER_COUNT);
    const token = login(userId);
    if (!token) {
        return;
    }
    http.del(`${GATEWAY_URL}/api/carts/${userId}`, null, {
        headers: authHeaders(token), tags: { journey: 'surge-checkout', step: 'clear-cart' },
    });
    http.post(
        `${GATEWAY_URL}/api/carts/${userId}/items`,
        JSON.stringify({ productId: pick(FLAGSHIP_PRODUCT_IDS), quantity: randInt(1, 2) }),
        { headers: authHeaders(token), tags: { journey: 'surge-checkout', step: 'add-to-cart' } }
    );
    const res = http.post(
        `${GATEWAY_URL}/api/orders/checkout`,
        JSON.stringify({ userId, couponCode: pick(COUPON_CODES) }),
        { headers: authHeaders(token), tags: { journey: 'surge-checkout', step: 'checkout' } }
    );
    check(res, { 'checkout not 5xx': (r) => r.status < 500 });
}

export function mix() {
    const r = rand();
    if (r < 0.35) {
        browsing();
    } else if (r < 0.5) {
        cart();
    } else {
        checkout();
    }
}
