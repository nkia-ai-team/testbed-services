// ============================================================
// commerce surge 부하 (시나리오 전용) — testbed-services docs/spec-scenario-load.md
// ============================================================
// baseline loadgen(tb-runner 상주, 불가침 — R1)과 별개로, 시나리오가 켰다 끄는
// 폭주 부하다. 강도는 절대 RPS가 아니라 "baseline 대비 배수"로 정의하고(R4·§3),
// 셸(시나리오 스크립트)이 diurnal 테이블로 현재 baseline RPS를 계산해
// TARGET_RPS(=baseline×배수)로 넘겨준다.
//
// 여정은 loadgen 여정 카탈로그(commerce/loadgen/script.js)와 같은 계약(같은
// 엔드포인트·데모 유저 20·flagship 상품 16)을 쓰되, Black Friday 성격에 맞게
// 체크아웃 가중을 폭주형(50%)으로 바꾼다 — 주문 폭주가 곧 이 시나리오의 원인이다.
import http from 'k6/http';
import { check } from 'k6';

const GATEWAY_URL = __ENV.GATEWAY_URL || 'http://192.168.122.77:30080';
const TARGET_RPS = Number(__ENV.TARGET_RPS || 40);
const RAMP_UP = __ENV.RAMP_UP || '2m';
const HOLD = __ENV.HOLD || '8m';
const RAMP_DOWN = __ENV.RAMP_DOWN || '1m';
const SURGE_SEED = Number(__ENV.SURGE_SEED || 4242);
const PRE_ALLOCATED_VUS = Number(__ENV.PRE_ALLOCATED_VUS || 50);
const MAX_VUS = Number(__ENV.MAX_VUS || 150);

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
    // 폭주 중 5xx/timeout은 이 시나리오가 만들려는 증상 그 자체 — 실패로 run을
    // 중단하지 않고 check() 통계로만 관측한다.
    thresholds: {},
};

// 재현성: loadgen과 동일한 mulberry32 + VU 분기 규약 (R4).
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

// 시드 데이터 계약 — loadgen과 동일 (데모 유저 20, flagship 상품 16, 쿠폰).
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
    // baseline loadgen과 동일한 이유(자립성 §8)로 체크아웃 물량을 bounded로 유지 —
    // 공유 데모 유저의 cart 팽창분을 비우고 이번 여정 물량만 담는다.
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
    // 폭주 중 5xx가 곧 증상. 4xx(재고·쿠폰)는 여정 내 정상 실패.
    check(res, { 'checkout not 5xx': (r) => r.status < 500 });
}

export default function () {
    const r = rand();
    if (r < 0.35) {
        browsing();
    } else if (r < 0.5) {
        cart();
    } else {
        checkout();
    }
}
