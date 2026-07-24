// ============================================================
// food-delivery create-heavy 부하 (F21-Q 전용) — testbed-services docs/scenario-redesign-wip/design-F21-threads-sheet.md
// ============================================================
// order-surge.js(order-create 주목적, 75% 가중)의 파생본. F21-Q는 order Tomcat
// 200 포화를 위해 order-create 동시성만 극대화하면 되고, courier pool 포화
// (order-surge.js의 목적)와 달리 배차 503 여부는 관심사가 아니다 — 오히려
// 그 잡음을 줄이기 위해 order-create 비중을 더 끌어올리고 무관 GET
// /api/orders(order-service 자신의 read 엔드포인트, restaurant-service가 아님)를
// 신규 'orders-read' 스텝으로 섞어 스레드 굶주림 전염 신호를 만든다
// (order-surge.js/surge.js 둘 다 order-service 자체의 GET을 치지 않는다 — sheet
// §4-A #3 지적: 현 read_step 계약(food domain_profiles.read_step="menu")은
// restaurant-service 메뉴조회라 order Tomcat 풀과 무관하다. 이 스크립트가 도입한
// 'orders-read' 태그를 read_step으로 재계약해야 계측이 성립 — merge-spec-F21.md
// §3 참조. 재계약 전에는 'orders-read' 태그가 나가되 미수집 상태로 무해하다).
//
// Little's law 산술(design-F21-threads-sheet.md §4-B): order Tomcat 200 포화
// = 동시 in-flight 200. restaurant 지연 d≈4s → 200 = λ·4 → λ≈50 create-req/s.
// 아래 가중(90% create)과 TARGET_RPS 기본값(56)의 곱 ≈50.4 req/s로 목표에 맞춘다.
// 여정·엔드포인트·시드 데이터 계약은 food baseline(food-delivery/loadgen/script.js)
// 및 order-surge.js와 동일 — 창작하지 않는다.
import http from 'k6/http';
import { check } from 'k6';

const RESTAURANT_URL = __ENV.RESTAURANT_URL || 'http://192.168.122.77:30181';
const ORDER_URL = __ENV.ORDER_URL || 'http://192.168.122.77:30180';
// TARGET_RPS 기본값 56: create 가중 90% 곱하면 λ≈50.4 create-req/s(설계 목표
// λ≈50과 정합). 지연 d가 5s(read-timeout) 쪽으로 붙으면 λ 요구는 40으로
// 낮아지나 retry/502 위험이 커지므로(sheet §4-B) 상한 쪽 여유를 기본값으로 둔다.
const TARGET_RPS = Number(__ENV.TARGET_RPS || 56);
const RAMP_UP = __ENV.RAMP_UP || '2m';
const HOLD = __ENV.HOLD || '8m';
const RAMP_DOWN = __ENV.RAMP_DOWN || '1m';
const SURGE_SEED = Number(__ENV.SURGE_SEED || 4242);
const PRE_ALLOCATED_VUS = Number(__ENV.PRE_ALLOCATED_VUS || 60);
// order Tomcat 200 포화가 목적이므로 동시 in-flight 요청이 200을 넘나들 수
// 있다 — k6 VU가 그 이상으로 눌리지 않도록 MAX_VUS를 넉넉히(order-surge.js
// 300보다 낮게, F21-Q는 courier pool 관심사가 없어 300까지 필요 없음) 잡는다.
const MAX_VUS = Number(__ENV.MAX_VUS || 260);

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
    // 스레드 포화 중 지연/timeout(무관 GET 전염 포함)은 이 시나리오가 만들려는
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

// 시드 데이터 계약 — baseline과 동일 (order-surge.js와 동일 규약).
function randomRestaurantId() {
    return rand() < 3.0 / 23 ? randInt(1, 3) : randInt(16, 35);
}
const CUSTOMER_ID_MAX = 2000;
const customerId = () => `cust-${randInt(1, CUSTOMER_ID_MAX)}`;

// order-create 여정 — order-surge.js의 orderJourney()와 동일 규약(메뉴 조회 →
// 가용 아이템 필터 → 주문 생성). 이 스크립트에선 create 자체가 주 목적이라
// 별도 변경 없이 그대로 재사용한다.
function orderCreateJourney() {
    const restaurantId = randomRestaurantId();
    const menuRes = http.get(`${RESTAURANT_URL}/api/restaurants/${restaurantId}/menu`,
        { tags: { journey: 'f21q-create', step: 'menu-lookup' } });
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
        { headers: { 'Content-Type': 'application/json' }, tags: { journey: 'f21q-create', step: 'create' } }
    );
    // restaurant 상류지연 중 createOrder는 지연/timeout으로 나타나되 502/503
    // 폭증은 아니어야 정체성이 성립(sheet §2) — 여기선 통계로만 관측한다.
    check(orderRes, {
        'order 200/400/503/timeout': (r) => r.status === 0 || r.status === 200 || r.status === 400 || r.status === 503,
    });
}

// order-service 자신의 무관 읽기 GET /api/orders — 하류(restaurant/dispatch/
// payment)를 안 타는 엔드포인트(OrderService.java:173/182)가 스레드 굶주림으로
// 동반 지연되는지가 F21-Q의 결정적 지문(sheet §2 "무관 GET 전염"). 'orders-read'
// 태그는 merge-spec-F21.md §3의 재계약(food domain_profiles.read_step)을
// 전제로 수집된다 — 재계약 전에는 무해하게 미수집.
function ordersReadJourney() {
    const res = http.get(`${ORDER_URL}/api/orders?page=0&size=10`,
        { tags: { journey: 'f21q-read', step: 'orders-read' } });
    check(res, { 'orders read 200/timeout': (r) => r.status === 0 || r.status === 200 });
}

// 여정 가중 — order-create 90 / orders-read(무관 GET 전염 계측) 10.
// order-surge.js(75/10/5/10, courier pool 포화 목적)와 달리 F21-Q는 스레드
// 포화 자체가 목적이라 browsing/search/delivery 여정은 걷어냈다.
export default function () {
    const r = rand();
    if (r < 0.9) {
        orderCreateJourney();
    } else {
        ordersReadJourney();
    }
}
