// ============================================================
// food-delivery 지속 부하 생성기 (§8 baseline load driver, commerce/loadgen 이식)
// ============================================================
// 시나리오(장애 주입)와 별개인 상주 컴포넌트 — 평상시 저율·현실적 사용자 여정을 끊김 없이
// 발생시킨다. food-delivery 에는 api-gateway 가 없어(§7 범위 밖) 각 서비스를 k8s Service
// DNS로 직접 호출한다.
//
// 여정 가중(§8): 식당 브라우징 50% / 지역 검색 15% / 주문 생성 20% / 주문 조회 5% / 배달 상태 조회 10%
// (2026-11-09 v4 E4 에서 주문 조회를 브라우징 몫에서 떼어 더함).
import http from 'k6/http';
import { check, sleep } from 'k6';

// ------------------------------------------------------------
// 설정 (entrypoint.sh가 --env로 주입)
// ------------------------------------------------------------
const RESTAURANT_URL = __ENV.RESTAURANT_URL || 'http://testbed-restaurant:8081';
const ORDER_URL = __ENV.ORDER_URL || 'http://testbed-order:8080';
const DISPATCH_URL = __ENV.DISPATCH_URL || 'http://testbed-dispatch:8082';
const TARGET_RPS = Number(__ENV.TARGET_RPS || 3);
const PRE_ALLOCATED_VUS = Number(__ENV.PRE_ALLOCATED_VUS || 10);
const MAX_VUS = Number(__ENV.MAX_VUS || 30);
const BASE_SEED = Number(__ENV.LOADGEN_SEED || 42);

export const options = {
    scenarios: {
        baseline: {
            executor: 'constant-arrival-rate',
            rate: TARGET_RPS,
            timeUnit: '1s',
            duration: '1h',
            preAllocatedVUs: PRE_ALLOCATED_VUS,
            maxVUs: MAX_VUS,
        },
    },
    // 배경(정상) 트래픽이라 개별 요청 실패로 전체 run을 실패 처리하지 않는다 — 매진/영업종료
    // 거부(400/503)도 여정 안의 정상 케이스, check() 통계로만 관측한다.
    thresholds: {},
};

// ------------------------------------------------------------
// 재현성: mulberry32 PRNG. entrypoint가 매시 새 k6 프로세스를 띄우므로, 이 모듈 스코프 코드는
// VU 초기화마다(최초 1회) 실행된다. 시드를 __VU로 분기해 VU마다 서로 다른(그러나 재현 가능한)
// 난수열을 갖게 한다.
function mulberry32(seed) {
    let s = seed >>> 0;
    return function () {
        s = (s + 0x6d2b79f5) | 0;
        let t = Math.imul(s ^ (s >>> 15), 1 | s);
        t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
}

const rand = mulberry32(BASE_SEED + (__VU || 0) * 7919);

function randInt(min, max) {
    return Math.floor(rand() * (max - min + 1)) + min;
}

function pick(arr) {
    return arr[randInt(0, arr.length - 1)];
}

// ------------------------------------------------------------
// 시드 데이터 재사용 (7번 증분 db/init.sql 과 정합)
// ------------------------------------------------------------
// 식당 id 는 1~3(기존 데모) 과 16~35(대량 시드) 뿐 — 4~15 구간은 존재하지 않는다.
function randomRestaurantId() {
    return rand() < 3.0 / 23 ? randInt(1, 3) : randInt(16, 35);
}
const REGIONS = ['GANGNAM', 'HONGDAE', 'JAMSIL', 'YEOUIDO', 'MAPO', 'SONGPA', 'NOWON', 'GURO'];
const CUSTOMER_ID_MAX = 2000;
// 배달 상태 조회 여정에서 찍을 dispatch id 범위 — 대량 시드가 20,000건이라 이 범위 안에서
// 뽑으면 대부분 200, 배포 환경별로 시드 유무가 다를 수 있어 404 도 정상 케이스로 둔다.

function customerId() {
    return `cust-${randInt(1, CUSTOMER_ID_MAX)}`;
}

// ------------------------------------------------------------
// 여정 1: 식당/메뉴 브라우징 — 55%
// ------------------------------------------------------------
function browsingJourney() {
    const restaurantId = randomRestaurantId();

    const detailRes = http.get(`${RESTAURANT_URL}/api/restaurants/${restaurantId}`,
        { tags: { journey: 'browsing', step: 'detail' } });
    check(detailRes, { 'browse restaurant 200': (r) => r.status === 200 });

    const menuRes = http.get(`${RESTAURANT_URL}/api/restaurants/${restaurantId}/menu`,
        { tags: { journey: 'browsing', step: 'menu' } });
    check(menuRes, { 'browse menu 200': (r) => r.status === 200 });

    // §7 신규 인기메뉴 조회를 가끔 곁들인다.
    if (rand() < 0.3) {
        const popularRes = http.get(`${RESTAURANT_URL}/api/restaurants/${restaurantId}/popular-menu`,
            { tags: { journey: 'browsing', step: 'popular-menu' } });
        check(popularRes, { 'browse popular-menu 200': (r) => r.status === 200 });
    }
}

// ------------------------------------------------------------
// 여정 2: 지역 검색 — 15%
// ------------------------------------------------------------
function searchJourney() {
    const region = pick(REGIONS);
    const page = randInt(0, 2);
    const res = http.get(`${RESTAURANT_URL}/api/restaurants?region=${region}&status=OPEN&page=${page}&size=20`,
        { tags: { journey: 'search', step: 'query' } });
    check(res, { 'search 200': (r) => r.status === 200 });
}

// ------------------------------------------------------------
// 여정 3: 주문 생성 — 20%
// ------------------------------------------------------------
// 식당의 실제 메뉴를 먼저 조회해 그 안에서 1~2개를 골라 주문한다(존재하지 않는 menuId로
// 인한 스푸리어스 400 방지 — 도메인 검증 자체를 시험하는 게 목적이 아니라 정상 주문
// 트래픽을 만드는 게 목적).
function orderJourney() {
    const restaurantId = randomRestaurantId();
    const menuRes = http.get(`${RESTAURANT_URL}/api/restaurants/${restaurantId}/menu`,
        { tags: { journey: 'order', step: 'menu-lookup' } });
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
        { headers: { 'Content-Type': 'application/json' }, tags: { journey: 'order', step: 'create' } }
    );
    // 영업종료(400)·매진(400)·배차 포화(503) 는 여정 안의 정상 실패 케이스.
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
            { tags: { journey: 'order-lookup', step: 'order-detail' } });
        check(res, { 'order detail 200': (r) => r.status === 200 });
    } else {
        const res = http.get(`${ORDER_URL}/api/orders?customerId=${customerId()}&page=0&size=10`,
            { tags: { journey: 'order-lookup', step: 'order-history' } });
        check(res, { 'order history 200': (r) => r.status === 200 });
    }
}

// ------------------------------------------------------------
// 여정 4: 배달 상태 조회 — 10%
// ------------------------------------------------------------
// 배달 상세가 읽을 배차 id(2026-11-09, v4 E4 추가). 예전에는 1~DISPATCH_ID_MAX(20000) 에서 골랐는데
// 배차 표에 30일 보존이 생긴 뒤 66 의 id 는 5,114,395~6,850,136 이라 상세 조회가 전부 404 였다(로그는 안
// 남아 눈에 띄지 않음). 이제 진행 중 배차 목록(status=ASSIGNED, 66 약 1,700행, 배정 30분 안쪽)에서 받은
// id 를 VU 마다 기억해 그중 하나를 본다. 배차 행은 끝나도 30일 남으므로 기억한 id 는 계속 200 이다.
// 목록은 VU 의 첫 상세 때와 그 뒤 상세 열 번에 한 번꼴로 다시 받는다(66 EXPLAIN: status 인덱스 ref,
// LIMIT 21 행만 읽음, Slice 라 COUNT 없음).
let trackedDeliveryIds = [];

function refreshTrackedDeliveries() {
    const res = http.get(`${DISPATCH_URL}/api/deliveries?status=ASSIGNED&page=0&size=20`,
        { tags: { journey: 'delivery', step: 'active-list' } });
    check(res, { 'active delivery list 200': (r) => r.status === 200 });
    if (res.status !== 200) {
        return;
    }
    try {
        const ids = (res.json() || []).map((d) => d.id).filter((id) => id);
        if (ids.length > 0) {
            trackedDeliveryIds = ids;
        }
    } catch (e) {
        // 응답이 JSON 이 아니면 기억한 id 를 그대로 쓴다.
    }
}

function deliveryTrackingJourney() {
    if (rand() < 0.5) {
        if (trackedDeliveryIds.length === 0 || rand() < 0.1) {
            refreshTrackedDeliveries();
        }
        if (trackedDeliveryIds.length === 0) {
            return;
        }
        const dispatchId = pick(trackedDeliveryIds);
        const res = http.get(`${DISPATCH_URL}/api/deliveries/${dispatchId}`,
            { tags: { journey: 'delivery', step: 'detail' } });
        check(res, { 'delivery detail 200': (r) => r.status === 200 });
    } else {
        const res = http.get(`${DISPATCH_URL}/api/deliveries?status=DELIVERED&page=0&size=20`,
            { tags: { journey: 'delivery', step: 'list' } });
        check(res, { 'delivery list 200': (r) => r.status === 200 });
    }
}

// ------------------------------------------------------------
// 여정 선택 (누적 확률 — §8 여정 가중 그대로)
// ------------------------------------------------------------
export default function () {
    const r = rand();
    // 2026-11-09(v4 E4): 주문 조회 5% 를 식당 브라우징에서 떼어 왔다(55 → 50). 다른 여정 몫은 그대로다.
    if (r < 0.50) {
        browsingJourney();
    } else if (r < 0.65) {
        searchJourney();
    } else if (r < 0.85) {
        orderJourney();
    } else if (r < 0.90) {
        orderLookupJourney();
    } else {
        deliveryTrackingJourney();
    }
    sleep(randInt(0, 1));
}
