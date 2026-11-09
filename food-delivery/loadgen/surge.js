// ============================================================
// food-delivery surge 부하 (시나리오 전용) — testbed-services docs/spec-scenario-load.md
// ============================================================
// baseline loadgen(food-delivery/loadgen/script.js, tb-runner 상주 — 불가침)과 별개로,
// 시나리오가 켰다 끄는 폭주 부하다. §8.1-2 실측: food는 browsing/검색/추적 경로가
// s15~s100 전 구간 5xx 0%·p95 평탄으로 무릎이 없다(세 도메인 중 최경량 인프라) —
// 즉 이 도메인엔 "브라우징 폭주로 무너뜨릴 무릎"이 아직 없다. 반면 주문 경로는
// 배차 pool 사이징 특성상 courier pool 포화 시 503(biz_reject_503)을 낼 수 있는
// 구조라 원인 후보지만, 상시 대량 주문 시도로 그 자체를 만드는 건 이 스크립트의
// 목적이 아니다(§8.1-2, docs 결함 4 수리 이력 참고) — 주문은 bounded로만 섞는다.
// 따라서 여정 가중은 browsing/검색/추적 중심으로 두고 TARGET_RPS도 browsing 상한
// 실측(≥100rps 무릎 없음)보다 한참 낮은 안전 기본값을 쓴다.
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
// 기본값 근거: §8.1-2 실측(browsing/검색/추적 s15~s100 전 구간 무릎 없음, 건강
// 상한 ≥100rps) 대비 한참 아래인 40~60 구간을 기본으로 잡는다 — 이 도메인은
// "무너질 상한"이 아직 실측되지 않았으므로 무릎을 찾으러 가는 값이 아니라
// 배차 pool 등 하류 결함을 노출시키는 안전한 지속 강도로 둔다. env로 오버라이드.
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
    // 폭주 중 5xx/timeout은 이 시나리오가 만들려는 증상 그 자체 — 실패로 run을
    // 중단하지 않고 check() 통계로만 관측한다(배차 503도 baseline과 동일하게
    // 여정 안의 정상 실패로 취급).
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

// bounded: 폭주 여정 믹스에서 낮은 고정 비중만 차지한다 — 이 시나리오의 원인은
// browsing/검색 트래픽이지 주문 폭주가 아니다(§8.1-2 결함 4 수리 이력 참고).
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
    rememberOrder(orderRes);
}

// ------------------------------------------------------------
// 주문 조회(2026-11-09, v4 E4) — 손님이 방금 넣은 주문의 상태를 보거나 내 주문 목록을 본다.
// ------------------------------------------------------------
// 이 부하에는 주문 읽기가 없어 order 의 관측이 POST 하나뿐이었다. 그러면 주문 쓰기가 깨진 장애에서
// "order 자체는 읽기에 답한다"는 감별 근거가 데이터에 없다. 주문 생성과 별개 여정이라 생성이 실패하는
// 동안에도 이어진다. 주문 상세는 이 VU 가 마지막으로 만든 주문(생성 응답의 id)을 본다: 시드 id 는
// 30일 보존으로 지워져(66, 2026-11-09 orders 최소 id 5,120,480) 임의 id 는 404 다. 만든 주문이 아직
// 없으면 목록을 본다. 목록은 customer_id 인덱스로 한 손님 주문(66 실측 약 262건) 앞 10건만 읽는다
// (EXPLAIN ref idx_orders_customer, Slice 라 COUNT 없음).
let lastOrderId = null;

function rememberOrder(res) {
    if (res.status !== 200) {
        return;
    }
    try {
        const id = res.json('id');
        if (id) {
            lastOrderId = id;
        }
    } catch (e) {
        // 응답이 JSON 이 아니면 이전 값을 쓴다.
    }
}

function orderLookupJourney() {
    if (lastOrderId !== null && rand() < 0.5) {
        const res = http.get(`${ORDER_URL}/api/orders/${lastOrderId}`,
            { tags: { journey: 'surge-order-lookup', step: 'order-detail' } });
        check(res, { 'order detail 200': (r) => r.status === 200 });
    } else {
        const res = http.get(`${ORDER_URL}/api/orders?customerId=${customerId()}&page=0&size=10`,
            { tags: { journey: 'surge-order-lookup', step: 'order-history' } });
        check(res, { 'order history 200': (r) => r.status === 200 });
    }
}

// 배차 목록 조회와 dispatch 연결 풀(최대 10, 대기 3초) 계산(2026-11-09, v4 E4).
// F19-P, F19-S, F10-H 녹화(2026-08-22~23)에서 이 목록 조회가 dispatch 풀을 채운 것은 목록이 Page 라
// 쪽마다 COUNT(약 174만 행의 끝난 배차를 다 셈)가 붙었기 때문이다: 50rps x 추적 15% x 목록 50%
// = 초당 3.75건 x COUNT 약 2.4초 = 동시 약 9개로 풀 10개에 거의 찼다. 27a3d0a(10-08)가 목록을 Slice 로
// 바꿔 COUNT 를 없앴고, 66 MySQL 다이제스트(2026-11-09)에서 목록 문장은 status 인덱스로 21행만 읽는
// LIMIT 조회 하나(334,351회, 평균 5.05ms, p99 6.3ms)이며 COUNT 목록 문장은 없다. 지금은 50rps 에서
// 초당 3.75건 x 0.005초 = 동시 약 0.02개다. 10배 느려져도 0.2개라 부하 쪽에서 줄일 것이 없다.
// 목록 조회를 다시 Page 로 바꾸거나 page 를 깊게 넘기면 이 계산이 깨진다(F59-R 이 일부러 그렇게 한다).
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

// 여정 가중(누적 확률) — browsing 40 / 검색 30 / 주문(bounded) 10 / 주문 조회 5 / 추적 15.
// baseline(50/15/20/5/10)과 달리 browsing·검색을 폭주 중심으로 올리고 주문
// 비중은 낮춰 원인을 browsing/검색 쪽에 둔다(§8.1-2 근거, 위 헤더 주석).
// 2026-11-09(v4 E4): 주문 조회 5% 를 browsing 에서 떼어 왔다(45 → 40), baseline 과 같은 여정이다.
export default function () {
    const r = rand();
    if (r < 0.40) {
        browsingJourney();
    } else if (r < 0.70) {
        searchJourney();
    } else if (r < 0.80) {
        orderJourneyBounded();
    } else if (r < 0.85) {
        orderLookupJourney();
    } else {
        deliveryTrackingJourney();
    }
}
