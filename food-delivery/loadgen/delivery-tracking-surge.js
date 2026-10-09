// ============================================================
// food-delivery delivery-tracking-surge 부하 (시나리오 전용) — testbed-services docs/spec-scenario-load.md
// ============================================================
// surge.js 의 파생본. 저녁 피크에 주문한 손님들이 배달 상태를 되풀이해 확인하는 꼴로,
// 배달 추적(목록, 상세)을 주 여정으로 두고 주문 생성은 낮은 고정 비중으로만 섞는다.
// surge.js(45/30/10/15)는 browsing/검색 폭주 시나리오의 원인 프로파일이라 무변경 유지,
// order-surge.js 는 주문 생성 볼륨(배차 pool 포화)을 담당하므로 이 스크립트는 추적 볼륨만 맡는다.
// 주문 비중을 낮게 두는 이유: 배차 한도(dispatch.max-capacity 2000)를 동반 부하가 채워
// 'courier pool exhausted' 503 이 섞이지 않게 하려는 것이다(초당 주문 약 1건 × 20분 = 약 1,200건,
// 평시 배정 중 배차 약 300건을 더해도 한도 아래).
//
// 여정·엔드포인트·시드 데이터 계약은 food baseline(food-delivery/loadgen/script.js)과
// 동일 — 창작하지 않는다. food-delivery엔 api-gateway가 없어 각 서비스를 개별 NodePort로
// 직접 호출한다(20/21/22-*.yaml *-external Service 주석 참조).
import http from 'k6/http';
import { check } from 'k6';

const RESTAURANT_URL = __ENV.RESTAURANT_URL || 'http://192.168.122.77:30181';
const ORDER_URL = __ENV.ORDER_URL || 'http://192.168.122.77:30180';
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
const pick = (arr) => arr[randInt(0, arr.length - 1)];

// 시드 데이터 계약 — baseline과 동일 (7번 증분 db/init.sql 정합).
// 식당 id는 1~3(기존 데모)과 16~35(대량 시드)뿐 — 4~15 구간은 존재하지 않는다.
function randomRestaurantId() {
    return rand() < 3.0 / 23 ? randInt(1, 3) : randInt(16, 35);
}
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
}

// bounded: 추적 여정 믹스에서 낮은 고정 비중만 차지한다(위 헤더 주석).
function orderJourneyBounded() {
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
    // 영업종료(400)·매진(400)·배차 포화(503)는 여정 안의 정상 실패 케이스.
    check(orderRes, {
        'order 200/400/503': (r) => r.status === 200 || r.status === 400 || r.status === 503,
    });
}

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

// 여정 가중(누적 확률) — 추적 목록 60 / 추적 상세 15 / 주문(bounded) 10 / browsing 15.
// 추적 두 여정의 엔드포인트와 쿼리 문자열은 baseline deliveryTrackingJourney 와 같다.
export default function () {
    const r = rand();
    if (r < 0.60) {
        deliveryList();
    } else if (r < 0.75) {
        deliveryDetail();
    } else if (r < 0.85) {
        orderJourneyBounded();
    } else {
        browsingJourney();
    }
}
