// ============================================================
// food-delivery slowquery 부하 (시나리오 전용, F20-Q) — testbed-services docs/scenario-redesign-wip/design-F20-slowquery-sheet.md
// ============================================================
// baseline loadgen(food-delivery/loadgen/script.js, 불가침 — R1)·surge.js/
// order-surge.js(폭주)와 별개로, F20-Q가 켰다 끄는 슬로우쿼리 부하다.
// food order-service의 무거운 조회 2종을 반복 호출한다:
//   - GET /api/orders (page/size 미지정) → OrderController.java:32-45의
//     Pageable.unpaged() 무조건 경로(무제한 반환) → search()의 OR-null 술어
//     (OrderRepository.java:22-33)가 인덱스 없이 풀스캔
//   - GET /api/orders/stats/daily?days=90 → aggregateDailyStats(:36-40) native
//     "GROUP BY DATE(created_at)" — created_at 컬럼 함수 래핑으로 인덱스 무력화
//     + filesort
// 무제한 결과셋이 order JVM 힙(k8s limit 1Gi, 20-order-deploy.yaml:91)에
// 적재되어 GC 압박/OOM 위험이 커지는 것이 F20-Q의 정체성(자체 힙 압박,
// sheet §1 F20-Q) — 단일 MySQL이라 F20-R 같은 교차 스키마 오염은 없다.
//
// ramping-arrival-rate + mulberry32 시드 재현성(R4)과 식당/고객 시드 계약은
// surge.js/order-surge.js와 동일 — 창작하지 않는다. food-delivery엔
// api-gateway가 없어(§7 범위 밖) RESTAURANT_URL(30181)·ORDER_URL(30180)을
// 개별 NodePort로 직접 호출한다. entry_url(30181)의 domain_profile은
// business_step="create"/read_step="menu"로 고정돼 있어(profiles.json), 이
// 스크립트도 그 두 step 이름을 그대로 태깅한다:
//   - 슬로우쿼리 조회(GET /api/orders, /api/orders/stats/daily)는 step:'menu'로
//     태깅 → loadgen.read_step_status_rate가 이 무거운 조회의 상태를 집계한다
//     (관측 계약, sheet §4-A).
//   - 대표 order-create 여정을 낮은 비중(20%)으로 섞어 step:'create' 샘플을
//     계속 흘려보낸다 — 이게 없으면 monitor의 entry_status가 채워지지 않아
//     http.entry_health/abort(entry-unreachable)가 무너진다(러너 monitor는
//     tags.step===business_step 요청만 entry_status로 집계, team-lead 지적
//     반영).
import http from 'k6/http';
import { check } from 'k6';

const RESTAURANT_URL = __ENV.RESTAURANT_URL || 'http://192.168.122.77:30181';
const ORDER_URL = __ENV.ORDER_URL || 'http://192.168.122.77:30180';
const TARGET_RPS = Number(__ENV.TARGET_RPS || 30);
const RAMP_UP = __ENV.RAMP_UP || '2m';
const HOLD = __ENV.HOLD || '8m';
const RAMP_DOWN = __ENV.RAMP_DOWN || '1m';
const SURGE_SEED = Number(__ENV.SURGE_SEED || 4242);
// 슬로우쿼리는 요청당 지연이 surge.js/order-surge.js의 일반 여정보다 훨씬
// 크다(무제한 반환·filesort). food surge.js(50rps/50/300) 대비 VU 상한을
// 넉넉히 잡는다.
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
    // 슬로우쿼리 중 지연/타임아웃은 이 시나리오가 만들려는 증상 그 자체 —
    // 실패로 run을 중단하지 않고 check() 통계로만 관측한다(배차 503도 여정
    // 안의 정상 실패로 취급).
    thresholds: {},
};

// 재현성: baseline(script.js)과 동일한 mulberry32 + VU 분기 규약 (R4).
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
const customerId = () => `cust-${randInt(1, CUSTOMER_ID_MAX)}`;

// 슬로우쿼리 ① — 무제한 목록 조회(page/size 미지정 → Pageable.unpaged() 무조건
// 경로). step은 domain_profile.read_step("menu")에 맞춰 태깅한다(러너 monitor
// 계약).
function unboundedListJourney() {
    const res = http.get(`${ORDER_URL}/api/orders`, {
        tags: { journey: 'slowquery-list', step: 'menu' },
    });
    check(res, { 'unbounded orders list not 5xx': (r) => r.status < 500 });
}

// 슬로우쿼리 ② — 일별 통계(native GROUP BY DATE(created_at), filesort).
function dailyStatsJourney() {
    const res = http.get(`${ORDER_URL}/api/orders/stats/daily?days=90`, {
        tags: { journey: 'slowquery-stats', step: 'menu' },
    });
    check(res, { 'order stats/daily not 5xx': (r) => r.status < 500 });
}

// 대표 order-create 여정 — order-surge.js의 orderJourney와 동일 계약(메뉴
// 조회 후 생성). 낮은 비중으로 섞어 step:'create' 샘플이 끊기지 않게 한다
// (entry_status 배선).
function createRepresentative() {
    const restaurantId = randomRestaurantId();
    const menuRes = http.get(`${RESTAURANT_URL}/api/restaurants/${restaurantId}/menu`,
        { tags: { journey: 'slowquery-create', step: 'menu-lookup' } });
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
        { headers: { 'Content-Type': 'application/json' }, tags: { journey: 'slowquery-create', step: 'create' } }
    );
    // 영업종료(400)·매진(400)·배차 포화(503)는 여정 안의 정상 실패 케이스.
    check(orderRes, {
        'order 200/400/503': (r) => r.status === 200 || r.status === 400 || r.status === 503,
    });
}

// 여정 가중 — 슬로우쿼리(무제한 목록 40 / 통계 40)가 주 목적, order-create는
// 20%로 entry_status 배선용 최소 비중만 유지.
export default function () {
    const r = rand();
    if (r < 0.4) {
        unboundedListJourney();
    } else if (r < 0.8) {
        dailyStatsJourney();
    } else {
        createRepresentative();
    }
}
