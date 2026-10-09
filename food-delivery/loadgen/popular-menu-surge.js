// ============================================================
// food-delivery 인기 메뉴 둘러보기 부하 (시나리오 전용, F48-R) — docs/scenario-redesign-wip/design-F48-R-sheet.md
// ============================================================
// baseline loadgen(food-delivery/loadgen/script.js, tb-runner 상주 — 불가침)과 별개로,
// 시나리오가 켰다 끄는 둘러보기 부하다. 가게 화면을 여는 손님 여정(가게 상세, 메뉴,
// 인기 메뉴)만 되풀이하고 주문은 넣지 않는다. 기준선(script.js)에서는 인기 메뉴가
// 둘러보기의 30%(전체 여정의 약 16%)에만 붙어 가장 한가한 시간에 초당 약 0.2건뿐이라,
// 인기 메뉴 경로의 실패를 러너 판정과 119 표본 스팬이 시간대와 무관하게 볼 수 있도록
// 이 경로의 요청량을 채운다. 주문을 넣지 않는 이유: 주문은 배차 한도(평시 ASSIGNED 가
// 한도 근처까지 오르는 시간대가 있다)를 함께 소모해 order 503 이 섞이기 때문이다.
//
// 단계 태그: food 진입점(30181)의 domain_profile 은 business_step="create",
// read_step="menu" 로 고정돼 있다(profiles.json). 이 스크립트는 인기 메뉴 조회를
// step:'menu' 로 태깅해 loadgen.read_step_status_rate 가 인기 메뉴의 상태를 집계하게
// 한다(slowquery.js 가 무거운 조회를 같은 이름으로 태깅한 것과 같은 관측 계약).
// 메뉴 조회는 step:'menu-view' 로 따로 두어 섞이지 않게 한다. 주문 단계가 없어 이
// 문서의 entry_status 는 비어 있으므로, 진행 대본은 entry_status 를 기준선 문서
// (domain food-delivery)에서 읽는다.
//
// 엔드포인트·시드 데이터 계약은 food baseline 과 같다 — 창작하지 않는다.
import http from 'k6/http';
import { check } from 'k6';

const RESTAURANT_URL = __ENV.RESTAURANT_URL || 'http://192.168.122.77:30181';
const TARGET_RPS = Number(__ENV.TARGET_RPS || 5);
const RAMP_UP = __ENV.RAMP_UP || '2m';
const HOLD = __ENV.HOLD || '8m';
const RAMP_DOWN = __ENV.RAMP_DOWN || '15s';
const SURGE_SEED = Number(__ENV.SURGE_SEED || 4848);
const PRE_ALLOCATED_VUS = Number(__ENV.PRE_ALLOCATED_VUS || 20);
const MAX_VUS = Number(__ENV.MAX_VUS || 100);

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
    // 실패는 check() 통계로만 관측하고 run 을 중단하지 않는다.
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

// 시드 데이터 계약 — baseline과 동일 (7번 증분 db/init.sql 정합).
// 식당 id는 1~3(기존 데모)과 16~35(대량 시드)뿐 — 4~15 구간은 존재하지 않는다.
function randomRestaurantId() {
    return rand() < 3.0 / 23 ? randInt(1, 3) : randInt(16, 35);
}

// 가게 화면 한 번: 상세, 메뉴, 인기 메뉴.
export default function () {
    const restaurantId = randomRestaurantId();
    const detailRes = http.get(`${RESTAURANT_URL}/api/restaurants/${restaurantId}`,
        { tags: { journey: 'popular-browsing', step: 'detail' } });
    check(detailRes, { 'browse restaurant 200': (r) => r.status === 200 });

    const menuRes = http.get(`${RESTAURANT_URL}/api/restaurants/${restaurantId}/menu`,
        { tags: { journey: 'popular-browsing', step: 'menu-view' } });
    check(menuRes, { 'browse menu 200': (r) => r.status === 200 });

    const popularRes = http.get(`${RESTAURANT_URL}/api/restaurants/${restaurantId}/popular-menu`,
        { tags: { journey: 'popular-browsing', step: 'menu' } });
    check(popularRes, { 'browse popular-menu 200': (r) => r.status === 200 });
}
