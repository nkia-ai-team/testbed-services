// ============================================================
// food-delivery order-surge 부하 (시나리오 전용) — testbed-services docs/spec-scenario-load.md
// ============================================================
// surge.js(browsing/검색 중심 폭주)의 파생본. surge.js의 orderJourneyBounded는
// "여정 안의 낮은 고정 비중"으로만 주문을 섞어(§8.1-2, 헤더 주석 참고) courier
// pool을 포화시킬 만큼의 order-create 볼륨을 내지 않는다 — 이 스크립트는 그
// 반대로 order-create 자체를 주 목적(가중 상향, bounded 상한 제거)으로 삼아
// 배차 pool 포화 503(biz_reject_503)을 신뢰성 있게 유발한다. surge.js는
// browsing-surge 계열 시나리오의 원인 프로파일이므로 무변경 유지 — 이 스크립트가
// order 볼륨 문제를 분리해서 담당한다(Phase 2 관측 배관 스펙 블로커2).
//
// 여정·엔드포인트·시드 데이터 계약은 food baseline(food-delivery/loadgen/script.js)과
// 동일 — 창작하지 않는다. food-delivery엔 api-gateway가 없어(§7 범위 밖) 각
// 서비스를 개별 NodePort로 직접 호출한다(20/21/22-*.yaml *-external Service 주석 참조,
// tb-runner는 클러스터 밖이라 in-cluster DNS를 못 씀).
import http from 'k6/http';
import { check } from 'k6';

const RESTAURANT_URL = __ENV.RESTAURANT_URL || 'http://192.168.122.77:30181';
const ORDER_URL = __ENV.ORDER_URL || 'http://192.168.122.77:30180';
const DISPATCH_URL = __ENV.DISPATCH_URL || 'http://192.168.122.77:30182';
// 기본값 근거: surge.js와 달리 이 스크립트의 목적은 order-create 볼륨으로
// courier pool을 포화시키는 것 — browsing 무릎 실측(≥100rps)과 무관하게
// order-create 자체의 포화 임계를 찾아야 하므로 surge.js와 동일한 안전 기본값에서
// 시작하고 TARGET_RPS env로 튜닝한다(재현 검증은 실행 단계, 스펙 §2c).
const TARGET_RPS = Number(__ENV.TARGET_RPS || 50);
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
    // 폭주 중 5xx/timeout(courier pool 포화 503 포함)은 이 시나리오가 만들려는
    // 증상 그 자체 — 실패로 run을 중단하지 않고 check() 통계로만 관측한다.
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
const pick = (arr) => arr[randInt(0, arr.length - 1)];

// 시드 데이터 계약 — baseline과 동일 (7번 증분 db/init.sql 정합).
// 식당 id는 1~3(기존 데모)과 16~35(대량 시드)뿐 — 4~15 구간은 존재하지 않는다.
function randomRestaurantId() {
    return rand() < 3.0 / 23 ? randInt(1, 3) : randInt(16, 35);
}
const REGIONS = ['GANGNAM', 'HONGDAE', 'JAMSIL', 'YEOUIDO', 'MAPO', 'SONGPA', 'NOWON', 'GURO'];
const CUSTOMER_ID_MAX = 2000;
const DISPATCH_ID_MAX = 20000;
const customerId = () => `cust-${randInt(1, CUSTOMER_ID_MAX)}`;

function browsingJourney() {
    const restaurantId = randomRestaurantId();
    const detailRes = http.get(`${RESTAURANT_URL}/api/restaurants/${restaurantId}`,
        { tags: { journey: 'surge-browsing', step: 'detail' } });
    check(detailRes, { 'browse restaurant 200': (r) => r.status === 200 });

    const menuRes = http.get(`${RESTAURANT_URL}/api/restaurants/${restaurantId}/menu`,
        { tags: { journey: 'surge-browsing', step: 'menu' } });
    check(menuRes, { 'browse menu 200': (r) => r.status === 200 });

    if (rand() < 0.3) {
        const popularRes = http.get(`${RESTAURANT_URL}/api/restaurants/${restaurantId}/popular-menu`,
            { tags: { journey: 'surge-browsing', step: 'popular-menu' } });
        check(popularRes, { 'browse popular-menu 200': (r) => r.status === 200 });
    }
}

function searchJourney() {
    const region = pick(REGIONS);
    const page = randInt(0, 2);
    const res = http.get(`${RESTAURANT_URL}/api/restaurants?region=${region}&status=OPEN&page=${page}&size=20`,
        { tags: { journey: 'surge-search', step: 'query' } });
    check(res, { 'search 200': (r) => r.status === 200 });
}

// order-create가 이 스크립트의 주 목적이므로 surge.js의 "bounded" 제약(낮은
// 고정 비중)을 걷어냈다 — 여정 가중(아래 default export)에서 order를 대부분
// 차지하게 하고, TARGET_RPS 자체도 그대로 order 볼륨으로 전달된다.
function orderJourney() {
    const restaurantId = randomRestaurantId();
    const menuRes = http.get(`${RESTAURANT_URL}/api/restaurants/${restaurantId}/menu`,
        { tags: { journey: 'surge-order', step: 'menu-lookup' } });
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
        { headers: { 'Content-Type': 'application/json' }, tags: { journey: 'surge-order', step: 'create' } }
    );
    // 영업종료(400)·매진(400)·배차 포화(503)는 여정 안의 정상 실패 케이스 —
    // 503(courier pool 포화)이 바로 이 스크립트가 유발하려는 증상이다.
    check(orderRes, {
        'order 200/400/503': (r) => r.status === 200 || r.status === 400 || r.status === 503,
    });
}

function deliveryTrackingJourney() {
    if (rand() < 0.5) {
        const dispatchId = randInt(1, DISPATCH_ID_MAX);
        const res = http.get(`${DISPATCH_URL}/api/deliveries/${dispatchId}`,
            { tags: { journey: 'surge-delivery', step: 'detail' } });
        check(res, { 'delivery detail 200/404': (r) => r.status === 200 || r.status === 404 });
    } else {
        const res = http.get(`${DISPATCH_URL}/api/deliveries?status=DELIVERED&page=0&size=20`,
            { tags: { journey: 'surge-delivery', step: 'list' } });
        check(res, { 'delivery list 200': (r) => r.status === 200 });
    }
}

// 여정 가중(누적 확률) — 주문 75 / browsing 10 / 검색 5 / 추적 10.
// surge.js(45/30/10/15, order는 bounded)와 반대로 주문을 압도적 비중으로
// 올려 courier pool 포화 503을 신뢰성 있게 유발한다(스펙 블로커2 §2b).
export default function () {
    const r = rand();
    if (r < 0.75) {
        orderJourney();
    } else if (r < 0.85) {
        browsingJourney();
    } else if (r < 0.90) {
        searchJourney();
    } else {
        deliveryTrackingJourney();
    }
}
