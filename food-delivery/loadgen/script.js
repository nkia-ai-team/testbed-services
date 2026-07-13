// ============================================================
// food-delivery 지속 부하 생성기 (§8 baseline load driver, commerce/loadgen 이식)
// ============================================================
// 시나리오(장애 주입)와 별개인 상주 컴포넌트 — 평상시 저율·현실적 사용자 여정을 끊김 없이
// 발생시킨다. food-delivery 에는 api-gateway 가 없어(§7 범위 밖) 각 서비스를 k8s Service
// DNS로 직접 호출한다.
//
// 여정 가중(§8): 식당 브라우징 55% / 지역 검색 15% / 주문 생성 20% / 배달 상태 조회 10%.
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
const DISPATCH_ID_MAX = 20000;

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
}

// ------------------------------------------------------------
// 여정 4: 배달 상태 조회 — 10%
// ------------------------------------------------------------
function deliveryTrackingJourney() {
    if (rand() < 0.5) {
        const dispatchId = randInt(1, DISPATCH_ID_MAX);
        const res = http.get(`${DISPATCH_URL}/api/deliveries/${dispatchId}`,
            { tags: { journey: 'delivery', step: 'detail' } });
        check(res, { 'delivery detail 200/404': (r) => r.status === 200 || r.status === 404 });
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
    if (r < 0.55) {
        browsingJourney();
    } else if (r < 0.70) {
        searchJourney();
    } else if (r < 0.90) {
        orderJourney();
    } else {
        deliveryTrackingJourney();
    }
    sleep(randInt(0, 1));
}
