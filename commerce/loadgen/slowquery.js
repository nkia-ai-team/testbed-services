// ============================================================
// commerce slowquery 부하 (시나리오 전용, F20-R) — testbed-services docs/scenario-redesign-wip/design-F20-slowquery-sheet.md
// ============================================================
// baseline loadgen(tb-runner 상주, 불가침 — R1)·surge.js(체크아웃 폭주)와 별개로,
// F20-R이 켰다 끄는 슬로우쿼리 부하다. commerce order-service의 무거운 조회
// 2종을 반복 호출한다:
//   - GET /api/orders/stats/daily?days=90 — FUNCTION('DATE', createdAt) GROUP BY
//     (OrderRepository.java:32-35, created_at 인덱스 없음 → 풀스캔)
//   - GET /api/orders?from=<과거>&... (page/size 미지정, status/from 중 하나라도
//     넘겨야 OrderController.java:45-61의 :59 Pageable.unpaged() 경로로 들어간다 —
//     from만 넘기면 status/to는 null인 채로 search()의 OR-null 술어가 그대로
//     풀스캔된다)
// 이 풀스캔이 단일 PostgreSQL 인스턴스 CPU를 잠식해 payment_schema/inventory_schema
// 조회까지 동반 지연시키는 것이 F20-R의 정체성(교차 스키마 오염, sheet §1 F20-R).
//
// ramping-arrival-rate + mulberry32 시드 재현성(R4)과 데모 유저/상품 시드 계약은
// surge.js와 동일 — 창작하지 않는다. entry_url(30080)의 domain_profile은
// business_step="checkout"/read_step="list"로 고정돼 있어(profiles.json), 이
// 스크립트도 그 두 step 이름을 그대로 태깅한다:
//   - 슬로우쿼리 조회는 step:'list'로 태깅 → loadgen.read_step_status_rate가
//     이 무거운 조회의 상태를 집계한다(관측 계약, sheet §4-A).
//   - 대표 checkout 여정을 낮은 비중(20%)으로 섞어 step:'checkout' 샘플을
//     계속 흘려보낸다 — 이게 없으면 monitor의 entry_status가 채워지지 않아
//     http.entry_health/abort(entry-unreachable)가 무너진다(러너 monitor는
//     tags.step===business_step 요청만 entry_status로 집계, 08절 team-lead
//     지적 반영).
import http from 'k6/http';
import { check } from 'k6';

const GATEWAY_URL = __ENV.GATEWAY_URL || 'http://192.168.122.77:30080';
const TARGET_RPS = Number(__ENV.TARGET_RPS || 30);
const RAMP_UP = __ENV.RAMP_UP || '2m';
const HOLD = __ENV.HOLD || '8m';
const RAMP_DOWN = __ENV.RAMP_DOWN || '1m';
const SURGE_SEED = Number(__ENV.SURGE_SEED || 4242);
// 슬로우쿼리는 요청당 지연이 surge.js의 일반 여정보다 훨씬 크다(풀스캔) — 같은
// RPS라도 훨씬 많은 VU가 동시에 스캔을 기다리게 된다. surge.js(80rps/100/900)
// 대비 낮은 RPS(30)이지만 VU 상한은 넉넉히 잡는다.
const PRE_ALLOCATED_VUS = Number(__ENV.PRE_ALLOCATED_VUS || 60);
const MAX_VUS = Number(__ENV.MAX_VUS || 400);

export const options = {
    scenarios: {
        slowquery: {
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
    // 슬로우쿼리 중 지연/타임아웃은 이 시나리오가 만들려는 증상 그 자체 — 실패로
    // run을 중단하지 않고 check() 통계로만 관측한다.
    thresholds: {},
};

// 재현성: surge.js/loadgen(script.js)과 동일한 mulberry32 + VU 분기 규약 (R4).
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

// 시드 데이터 계약 — surge.js와 동일 (데모 유저 20, flagship 상품 16, 쿠폰).
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
        { headers: { 'Content-Type': 'application/json' }, tags: { journey: 'slowquery-login', step: 'login' } }
    );
    if (!check(res, { 'login 200': (r) => r.status === 200 })) {
        return null;
    }
    const body = res.json();
    tokenCache[userId] = { token: body.token, expiresAtMs: Date.parse(body.expiresAt) || Date.now() + 3600000 };
    return body.token;
}

const authHeaders = (token) => ({ 'Content-Type': 'application/json', Authorization: `Bearer ${token}` });

// 슬로우쿼리 ① — 일별 통계 (FUNCTION('DATE', createdAt) GROUP BY, 인덱스 없음).
// step은 domain_profile.read_step("list")에 맞춰 태깅한다(러너 monitor 계약).
function dailyStatsJourney() {
    const res = http.get(`${GATEWAY_URL}/api/orders/stats/daily?days=90`, {
        tags: { journey: 'slowquery-stats', step: 'list' },
    });
    check(res, { 'stats/daily not 5xx': (r) => r.status < 500 });
}

// 슬로우쿼리 ② — 무제한 검색. from만 채워 status/to는 null로 두면
// OrderController.java:59의 Pageable.unpaged() 경로로 들어가고, search()의
// OR-null 술어(OrderRepository.java:19-28)가 인덱스 없이 풀스캔된다. from을
// 충분히 과거로 잡아 사실상 전 행을 대상으로 한다.
function unboundedSearchJourney() {
    const res = http.get(`${GATEWAY_URL}/api/orders?from=2000-01-01T00:00:00`, {
        tags: { journey: 'slowquery-search', step: 'list' },
    });
    check(res, { 'unbounded search not 5xx': (r) => r.status < 500 });
}

// 대표 checkout 여정 — surge.js와 동일 계약(로그인→장바구니→체크아웃). 낮은
// 비중으로 섞어 step:'checkout' 샘플이 끊기지 않게 한다(entry_status 배선).
function checkoutRepresentative() {
    const userId = randInt(1, DEMO_USER_COUNT);
    const token = login(userId);
    if (!token) {
        return;
    }
    http.del(`${GATEWAY_URL}/api/carts/${userId}`, null, {
        headers: authHeaders(token), tags: { journey: 'slowquery-checkout', step: 'clear-cart' },
    });
    http.post(
        `${GATEWAY_URL}/api/carts/${userId}/items`,
        JSON.stringify({ productId: pick(FLAGSHIP_PRODUCT_IDS), quantity: randInt(1, 2) }),
        { headers: authHeaders(token), tags: { journey: 'slowquery-checkout', step: 'add-to-cart' } }
    );
    const res = http.post(
        `${GATEWAY_URL}/api/orders/checkout`,
        JSON.stringify({ userId, couponCode: pick(COUPON_CODES) }),
        { headers: authHeaders(token), tags: { journey: 'slowquery-checkout', step: 'checkout' } }
    );
    check(res, { 'checkout not 5xx': (r) => r.status < 500 });
}

// 여정 가중 — 슬로우쿼리(통계 40 / 무제한 검색 40)가 주 목적, checkout은 20%로
// entry_status 배선용 최소 비중만 유지.
export default function () {
    const r = rand();
    if (r < 0.4) {
        dailyStatsJourney();
    } else if (r < 0.8) {
        unboundedSearchJourney();
    } else {
        checkoutRepresentative();
    }
}
