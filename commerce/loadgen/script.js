// ============================================================
// commerce 지속 부하 생성기 (§8 baseline load driver)
// ============================================================
// 시나리오(장애 주입)와 별개인 상주 컴포넌트 — 평상시 저율·현실적 사용자 여정을 끊김 없이
// 발생시킨다. rca-scenario-runner(장애 주입 전용)와 역할이 분리되어 있다.
//
// 진입점은 api-gateway(§2 그래프의 client 진입점) 하나뿐이다. 단, cross-domain 이체 여정만
// 예외적으로 core-banking transfer API를 직접 호출한다(게이트웨이가 /api/transfers를
// 프록시하지 않음 — GatewayProxyController 라우트 목록에 없음, payment-service가 결제
// 처리 중에만 내부적으로 호출하는 경로라 게이트웨이 대상이 아니다).
//
// 여정 가중(§8): 브라우징 65% / 검색 15% / 장바구니 10% / 체크아웃 8% / cross-domain 이체 2%.
import http from 'k6/http';
import { check, sleep } from 'k6';

// ------------------------------------------------------------
// 설정 (entrypoint.sh가 --env로 주입)
// ------------------------------------------------------------
const GATEWAY_URL = __ENV.GATEWAY_URL || 'http://testbed-gateway:8089';
const BANKING_TRANSFER_URL = __ENV.BANKING_TRANSFER_URL
    || 'http://testbed-transfer.rca-testbed-banking.svc.cluster.local:8082';
const TARGET_RPS = Number(__ENV.TARGET_RPS || 5);
const PRE_ALLOCATED_VUS = Number(__ENV.PRE_ALLOCATED_VUS || 20);
const MAX_VUS = Number(__ENV.MAX_VUS || 50);
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
    // 시나리오 배경(정상) 트래픽이라 개별 요청 실패로 전체 run을 실패 처리하지 않는다 —
    // 체크아웃 실패(재고 소진 등)도 여정 안의 정상 케이스, check() 통계로만 관측한다.
    thresholds: {},
};

// ------------------------------------------------------------
// 재현성: mulberry32 PRNG. entrypoint가 매시 새 k6 프로세스를 띄우므로, 이 모듈 스코프 코드는
// VU 초기화마다(최초 1회) 실행된다. 시드를 __VU(k6 내장 VU 번호)로 분기해 VU마다 서로 다른
// (그러나 재현 가능한) 난수열을 갖게 한다 — 그렇지 않으면 같은 시드를 공유하는 모든 VU가
// 완전히 같은 유저/상품/여정을 동일 순서로 고르는 lockstep이 벌어진다.
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
    // [min, max] 포함 범위 정수.
    return Math.floor(rand() * (max - min + 1)) + min;
}

function pick(arr) {
    return arr[randInt(0, arr.length - 1)];
}

// ------------------------------------------------------------
// 시드 데이터 재사용 (4번 증분 시드와 정합)
// ------------------------------------------------------------
// 항상 존재가 보장되는 데모 유저 20명(user-service 자체 data.sql, 대량 시드 여부와 무관하게
// 기동만 하면 존재)을 로그인/체크아웃 대상으로 쓴다 — 대량 시드(3a bulk 1001~4000)는 환경에
// 따라 미배포일 수 있어(선택적 phase) 로그인 필요 여정의 안정성을 위해 제외했다. 카트/장바구니
// 조회처럼 실패해도 무해한 여정에는 굳이 배제할 이유가 없어 유저 풀을 그대로 재사용한다.
const DEMO_USER_COUNT = 20;
const SEED_PASSWORD = 'Passw0rd!';
// order/pricing/inventory 3개 서비스에 걸쳐 완전히 정합된 flagship 상품만 장바구니/체크아웃에
// 사용한다(4번 증분 보고 참조 — 대량 생성 상품 2,000개는 가격·재고 데이터가 없음).
const FLAGSHIP_PRODUCT_IDS = Array.from({ length: 16 }, (_, i) => i + 1);
// 브라우징은 카탈로그 전체(대량 시드 포함 최대 3000)를 대상으로 한다 — 없는 id를 찍어도
// 404는 여정 안의 정상 케이스(존재 보장 없는 카탈로그 열람 실패)로 처리한다.
const BROWSE_PRODUCT_ID_MAX = 3000;
const SEARCH_KEYWORDS = ['PlopVape', '액상', '코일', '케이스', 'Air', 'Nova', '배터리', '멘솔'];
const COUPON_CODES = [null, null, null, 'SAVE10', 'WELCOME5000'];
// 개인 계좌만 사용(ACC-9001/9002는 동결/해지 계좌라 실패가 보장되므로 제외).
const BANK_ACCOUNTS = ['ACC-1001', 'ACC-1002', 'ACC-1003', 'ACC-1004', 'ACC-1005', 'ACC-1006', 'ACC-1007'];

function userEmail(userId) {
    return `user${String(userId).padStart(2, '0')}@example.com`;
}

// VU 내에서 재사용하는 토큰 캐시("토큰 캐시 재사용") — userId -> {token, expiresAtMs}.
const tokenCache = {};

function login(userId) {
    const cached = tokenCache[userId];
    if (cached && cached.expiresAtMs > Date.now() + 60000) {
        return cached.token;
    }
    const res = http.post(
        `${GATEWAY_URL}/api/users/login`,
        JSON.stringify({ email: userEmail(userId), password: SEED_PASSWORD }),
        { headers: { 'Content-Type': 'application/json' }, tags: { journey: 'checkout', step: 'login' } }
    );
    const ok = check(res, { 'login 200': (r) => r.status === 200 });
    if (!ok) {
        return null;
    }
    const body = res.json();
    tokenCache[userId] = { token: body.token, expiresAtMs: Date.parse(body.expiresAt) || Date.now() + 3600000 };
    return body.token;
}

function authHeaders(token) {
    return { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` };
}

// ------------------------------------------------------------
// 여정 1: 브라우징(read) — 65%
// ------------------------------------------------------------
function browsingJourney() {
    // 상품 목록(카테고리 필터 랜덤 or 전체) — 현재 API에 offset/limit 페이지네이션이 없어
    // 목록은 전체 반환이다(§7 read API 보강 전까지의 현재 표면 그대로).
    const listRes = rand() < 0.5
        ? http.get(`${GATEWAY_URL}/api/products?categoryId=${randInt(1, 4)}`, { tags: { journey: 'browsing', step: 'list' } })
        : http.get(`${GATEWAY_URL}/api/products`, { tags: { journey: 'browsing', step: 'list' } });
    check(listRes, { 'browse list 200': (r) => r.status === 200 });

    // 상세 조회 1~2건 — 대량 시드 상품까지 포함한 전체 카탈로그 범위에서.
    const detailCount = randInt(1, 2);
    for (let i = 0; i < detailCount; i++) {
        const productId = randInt(1, BROWSE_PRODUCT_ID_MAX);
        const res = http.get(`${GATEWAY_URL}/api/products/${productId}`, { tags: { journey: 'browsing', step: 'detail' } });
        check(res, { 'browse detail 200/404': (r) => r.status === 200 || r.status === 404 });
    }
}

// ------------------------------------------------------------
// 여정 2: 검색 — 15%
// ------------------------------------------------------------
function searchJourney() {
    const keyword = pick(SEARCH_KEYWORDS);
    const res = http.get(`${GATEWAY_URL}/api/products?name=${encodeURIComponent(keyword)}`, {
        tags: { journey: 'search', step: 'query' },
    });
    check(res, { 'search 200': (r) => r.status === 200 });
}

// ------------------------------------------------------------
// 여정 3: 장바구니 — 10%
// ------------------------------------------------------------
function cartJourney() {
    const userId = randInt(1, DEMO_USER_COUNT);
    const getRes = http.get(`${GATEWAY_URL}/api/carts/${userId}`, { tags: { journey: 'cart', step: 'get' } });
    check(getRes, { 'cart get 200': (r) => r.status === 200 });

    if (rand() < 0.6) {
        const productId = pick(FLAGSHIP_PRODUCT_IDS);
        const addRes = http.post(
            `${GATEWAY_URL}/api/carts/${userId}/items`,
            JSON.stringify({ productId, quantity: randInt(1, 2) }),
            { headers: { 'Content-Type': 'application/json' }, tags: { journey: 'cart', step: 'add' } }
        );
        check(addRes, { 'cart add 200': (r) => r.status === 200 });
    } else {
        const productId = pick(FLAGSHIP_PRODUCT_IDS);
        const delRes = http.del(`${GATEWAY_URL}/api/carts/${userId}/items/${productId}`, null, {
            tags: { journey: 'cart', step: 'remove' },
        });
        check(delRes, { 'cart remove 200': (r) => r.status === 200 });
    }
}

// ------------------------------------------------------------
// 여정 4: 체크아웃(order→payment, cross-domain ① 경유) — 8%
// ------------------------------------------------------------
function checkoutJourney() {
    const userId = randInt(1, DEMO_USER_COUNT);
    const token = login(userId);
    if (!token) {
        return; // 로그인 실패는 정상 케이스로 카운트(check()에 이미 기록됨), 여정 중단만.
    }

    // 체크아웃 전 장바구니에 최소 1개는 있어야 하므로(비어있으면 400) 아이템을 추가해둔다.
    const itemCount = randInt(1, 2);
    for (let i = 0; i < itemCount; i++) {
        const productId = pick(FLAGSHIP_PRODUCT_IDS);
        http.post(
            `${GATEWAY_URL}/api/carts/${userId}/items`,
            JSON.stringify({ productId, quantity: randInt(1, 2) }),
            { headers: authHeaders(token), tags: { journey: 'checkout', step: 'add-to-cart' } }
        );
    }

    const checkoutRes = http.post(
        `${GATEWAY_URL}/api/orders/checkout`,
        JSON.stringify({ userId, couponCode: pick(COUPON_CODES) }),
        { headers: authHeaders(token), tags: { journey: 'checkout', step: 'checkout' } }
    );
    // 재고 소진(409)·쿠폰 오류(400) 등은 여정 안의 정상 실패 케이스 — check()로만 관측하고
    // 여정 자체를 실패 처리하지 않는다.
    check(checkoutRes, {
        'checkout 200/400/409': (r) => r.status === 200 || r.status === 400 || r.status === 409,
    });
}

// ------------------------------------------------------------
// 여정 5: cross-domain 이체 — 2% (게이트웨이를 거치지 않고 core-banking을 직접 호출)
// ------------------------------------------------------------
function crossDomainJourney() {
    const from = pick(BANK_ACCOUNTS);
    let to = pick(BANK_ACCOUNTS);
    while (to === from) {
        to = pick(BANK_ACCOUNTS);
    }
    const amount = randInt(1000, 50000);
    const orderId = `loadgen-${Date.now()}-${randInt(1000, 9999)}`;

    const res = http.post(
        `${BANKING_TRANSFER_URL}/api/transfers`,
        JSON.stringify({ fromAccount: from, toAccount: to, amount, orderId }),
        { headers: { 'Content-Type': 'application/json' }, tags: { journey: 'cross-domain', step: 'transfer' } }
    );
    check(res, { 'transfer 200': (r) => r.status === 200 });
}

// ------------------------------------------------------------
// 여정 선택 (누적 확률 — §8 여정 가중 그대로)
// ------------------------------------------------------------
export default function () {
    const r = rand();
    if (r < 0.65) {
        browsingJourney();
    } else if (r < 0.80) {
        searchJourney();
    } else if (r < 0.90) {
        cartJourney();
    } else if (r < 0.98) {
        checkoutJourney();
    } else {
        crossDomainJourney();
    }
    sleep(randInt(0, 1));
}
