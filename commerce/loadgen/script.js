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
import exec from 'k6/execution';

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

// journey 는 로그인 요청의 journey 태그다(이 토큰이 필요한 여정). 캐시는 여정과 무관하게 VU 안에서 함께 쓴다.
function login(userId, journey = 'checkout') {
    const cached = tokenCache[userId];
    if (cached && cached.expiresAtMs > Date.now() + 60000) {
        return cached.token;
    }
    const res = http.post(
        `${GATEWAY_URL}/api/users/login`,
        JSON.stringify({ email: userEmail(userId), password: SEED_PASSWORD }),
        { headers: { 'Content-Type': 'application/json' }, tags: { journey, step: 'login' } }
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
    // 카테고리 목록(§7 GET /api/categories) — 가끔 곁들인다.
    if (rand() < 0.1) {
        const catRes = http.get(`${GATEWAY_URL}/api/categories`, { tags: { journey: 'browsing', step: 'categories' } });
        check(catRes, { 'browse categories 200': (r) => r.status === 200 });
    }

    // 상품 목록 — §7의 category=/page=/size= 파라미터로 2,000개 대량 상품 전체를 페이지 단위로
    // 브라우징한다(카탈로그 범위 대비 넉넉한 page 상한을 잡아 빈 페이지도 정상 케이스로 둔다).
    const page = randInt(0, 50);
    const listRes = rand() < 0.5
        ? http.get(`${GATEWAY_URL}/api/products?category=${randInt(1, 4)}&page=${page}&size=20`,
            { tags: { journey: 'browsing', step: 'list' } })
        : http.get(`${GATEWAY_URL}/api/products?page=${page}&size=20`,
            { tags: { journey: 'browsing', step: 'list' } });
    check(listRes, { 'browse list 200': (r) => r.status === 200 });

    // 상세 조회 1~2건(대량 시드 상품 포함 전체 카탈로그 범위) + variants 일부(§7).
    const detailCount = randInt(1, 2);
    for (let i = 0; i < detailCount; i++) {
        const productId = randInt(1, BROWSE_PRODUCT_ID_MAX);
        const res = http.get(`${GATEWAY_URL}/api/products/${productId}`, { tags: { journey: 'browsing', step: 'detail' } });
        check(res, { 'browse detail 200/404': (r) => r.status === 200 || r.status === 404 });

        if (rand() < 0.3) {
            const variantsRes = http.get(`${GATEWAY_URL}/api/products/${productId}/variants`,
                { tags: { journey: 'browsing', step: 'variants' } });
            check(variantsRes, { 'browse variants 200/404': (r) => r.status === 200 || r.status === 404 });
        }
    }
}

// ------------------------------------------------------------
// 여정 2: 검색 — 15%
// ------------------------------------------------------------
function searchJourney() {
    const keyword = pick(SEARCH_KEYWORDS);
    const page = randInt(0, 5);
    const res = http.get(`${GATEWAY_URL}/api/products?q=${encodeURIComponent(keyword)}&page=${page}&size=20`, {
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

    // 장바구니 쓰기(담기, 빼기)는 게이트웨이 AuthGuard 가 토큰을 요구한다(2026-07-13 부터, 없으면 401
    // "Missing Authorization token"). 체크아웃 여정과 같은 VU 토큰 캐시를 쓰므로, 이 VU 가 이 사용자로
    // 이미 로그인했으면 로그인 요청이 늘지 않는다. 고르는 순서(담기냐 빼기냐, 상품, 수량)는 종전과 같아
    // 난수열과 쓰기 요청 수, 사용자 분포가 바뀌지 않는다. 로그인이 실패하면 체크아웃 여정처럼 쓰기를
    // 보내지 않는다(로그인 실패는 check 에 이미 남는다).
    const adding = rand() < 0.6;
    const productId = pick(FLAGSHIP_PRODUCT_IDS);
    const quantity = adding ? randInt(1, 2) : 0;
    const token = login(userId, 'cart');
    if (!token) {
        return;
    }
    if (adding) {
        const addRes = http.post(
            `${GATEWAY_URL}/api/carts/${userId}/items`,
            JSON.stringify({ productId, quantity }),
            { headers: authHeaders(token), tags: { journey: 'cart', step: 'add' } }
        );
        check(addRes, { 'cart add 200': (r) => r.status === 200 });
    } else {
        const delRes = http.del(`${GATEWAY_URL}/api/carts/${userId}/items/${productId}`, null, {
            headers: authHeaders(token), tags: { journey: 'cart', step: 'remove' },
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

    // 체크아웃 물량을 bounded로 유지: 다른 여정(cartJourney)이 쌓아둔 장바구니를 비우고
    // 이번 여정 물량(1~2개)만 담는다. 데모 유저 20명을 전 여정이 공유하므로, 비우지 않으면
    // cart가 무한 팽창해 재고를 한 번에 소진 → checkout이 영구 실패하는 자립성 버그(§8)가
    // 재발한다(2026-07-14 실측: user 4 카트 982개, flagship 재고 14/16종 소진).
    // 두 쓰기에도 check 를 단다(2026-11-11 v4 E5c): 상주 감시가 checkout, list 밖 단계는 check 로만 센다.
    const clearRes = http.del(`${GATEWAY_URL}/api/carts/${userId}`, null, {
        headers: authHeaders(token), tags: { journey: 'checkout', step: 'clear-cart' },
    });
    check(clearRes, { 'checkout clear-cart 200': (r) => r.status === 200 });

    // 체크아웃 전 장바구니에 최소 1개는 있어야 하므로(비어있으면 400) 아이템을 추가해둔다.
    const itemCount = randInt(1, 2);
    for (let i = 0; i < itemCount; i++) {
        const productId = pick(FLAGSHIP_PRODUCT_IDS);
        const addRes = http.post(
            `${GATEWAY_URL}/api/carts/${userId}/items`,
            JSON.stringify({ productId, quantity: randInt(1, 2) }),
            { headers: authHeaders(token), tags: { journey: 'checkout', step: 'add-to-cart' } }
        );
        check(addRes, { 'checkout add-to-cart 200': (r) => r.status === 200 });
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
// 짝 이체(2026-11-10, v4 E5b). 활성 계좌 7개끼리 아무렇게나 이체하면(66 하루 약 1만 건, 평균 약
// 2.56만) 계좌마다 잔액이 하루 표준편차 약 157만씩 무작위로 흩어지고, 바닥에 닿은 계좌는 출금이
// 막혀(잔액 부족) 돈이 몇 계좌에 몰린다(11-10 66: ACC-1003 2,190만, ACC-1007 11만, 잔액 부족 FAILED
// 하루 314건). 이제 두 번이 한 짝이다: 보내기는 7개 중 둘 사이(금액 1,000~50,000), 이 VU 의 다음
// 이체가 같은 금액을 되돌린다. 계좌, 금액 분포, 요청 수는 그대로이고 계좌마다 잔액은 시작값에서
// 진행 중인 짝 몫만큼만 움직인다.
let pendingReturn = null;

function transferCompleted(res) {
    if (res.status !== 200) {
        return false;
    }
    try {
        return res.json('status') === 'COMPLETED';
    } catch (e) {
        return false;
    }
}

// 되돌리기 결과(2026-11-10 E5b 수정 1회차). 돈이 옮겨진 이체(COMPLETED)만 되돌릴 짝을 남긴다. 되돌리기는
// 요청이 처리되지 않은 것이 확실한 실패(503, 연결 거부)일 때만 짝을 남겨 다음 이체 차례에 다시 한다.
// 500, 502, 504, 시간 초과는 버린다: api 경로는 하류가 늦으면 502 를 준 뒤 transfer 가 커밋할 수
// 있어 다시 하면 두 번 되돌려질 수 있다. 버린 짝은 7개 계좌 잔액을 그만큼 남기고, Oracle 점검
// (oracle-maintenance.sh check) 의 7개 계좌 하한이 쌓인 어긋남을 잡는다.
function retryLater(res) {
    return res.status === 503
        || (res.status === 0 && String(res.error || '').indexOf('connection refused') >= 0);
}

function crossDomainJourney() {
    let from;
    let to;
    let amount;
    const returning = pendingReturn !== null;
    if (returning) {
        ({ from, to, amount } = pendingReturn);
    } else {
        from = pick(BANK_ACCOUNTS);
        to = pick(BANK_ACCOUNTS);
        while (to === from) {
            to = pick(BANK_ACCOUNTS);
        }
        amount = randInt(1000, 50000);
    }
    const orderId = `loadgen-${Date.now()}-${randInt(1000, 9999)}`;

    const res = http.post(
        `${BANKING_TRANSFER_URL}/api/transfers`,
        JSON.stringify({ fromAccount: from, toAccount: to, amount, orderId }),
        { headers: { 'Content-Type': 'application/json' }, tags: { journey: 'cross-domain', step: 'transfer' } }
    );
    check(res, { 'transfer 200': (r) => r.status === 200 });
    if (returning) {
        if (!retryLater(res)) {
            pendingReturn = null;
        }
    } else if (transferCompleted(res)) {
        pendingReturn = { from: to, to: from, amount };
        returnForced = false;
        sentWhileClosing = closing();
        // 마감 뒤 보낸 짝은 끝 30초 전까지 남은 시간 안에서 최대 1분 뒤 아무 때나 되돌린다. 바로 다음
        // 반복에 되돌리면 두 건이 같은 분에 몰려 분당 이체 수가 평소보다 크게 흔들린다.
        returnDueAt = sentWhileClosing
            ? Date.now() + Math.floor(closeRand() * Math.max(0, Math.min(60000, runEnd() - Date.now() - 30000)))
            : 0;
    } else if (closing()) {
        // 마감 뒤 보내기가 돈을 옮기지 못했다(은행 장애 등): 짝이 생기지 않았으니 번갈기를 되돌려 다음
        // 이체 차례도 보내기로 둔다. 안 그러면 장애 동안 차례당 이체가 0.5건으로 줄어 주입 구간 요청이 준다.
        skipThisTurn = false;
    }
}

// 재시작 전 짝 마치기(2026-11-10 E5b 수정 1회차). k6 는 한 시간마다 새로 뜬다(entrypoint.sh).
// 쥔 짝은 재시작 때 버려지므로 VU 마다 정한 마감 시각(끝 3~20분 전, VU 시드로 고르게 흩음)부터는
// 짝을 끝 전에 마친다. 이체 수와 시간 모양은 그대로 둔다:
//   1) 마감 때 이미 쥔 짝은 다음 반복에서 한 번 앞당겨 되돌리고, 그 몫으로 다음 이체 차례 하나를
//      다른 여정으로 돌린다(owedSkips).
//   2) 마감 뒤 이체 차례는 번갈아, 짝을 보내고 1분 안에 되돌리거나(이체 2건) 다른 여정으로
//      돌린다(0건). 두 차례에 이체 2건이라 종전(차례마다 1건)과 같다. 번갈기 시작은 VU 마다 고르게 정한다.
// 앞당기기는 짝마다 한 번뿐이다. 그 시도가 실패해 짝이 남으면 보통 이체 차례에만 다시 하므로, 은행
// 장애 중에도 이체 요청이 늘지 않는다.
const RUN_DURATION_MS = 60 * 60 * 1000;
const closeRand = mulberry32(BASE_SEED + (__VU || 0) * 7919 + 104729);
const CLOSE_BEFORE_END_MS = Math.floor((3 + closeRand() * 17) * 60 * 1000);
let returnForced = false;
let sentWhileClosing = false;
let returnDueAt = 0;
let owedSkips = 0;
// 마감 뒤 이체 차례를 번갈아 거르는 순서. 시작을 VU 마다 고르게 정해 한 시간 합이 한쪽으로 치우치지 않게 한다.
let skipThisTurn = closeRand() < 0.5;
// 끝 10초 안에는 새 짝을 보내지 않는다: 되돌릴 반복이 남지 않아 짝이 버려지기 쉽다(VU 반복 간격은
// 가장 느린 밤에도 10초 안쪽).
const LAST_SEND_MS = 10 * 1000;

function runEnd() {
    return exec.scenario.startTime + RUN_DURATION_MS;
}

function closing() {
    return Date.now() >= runEnd() - CLOSE_BEFORE_END_MS;
}

// ------------------------------------------------------------
// 여정 선택 (누적 확률 — §8 여정 가중 그대로)
// ------------------------------------------------------------
export default function () {
    if (pendingReturn !== null && !returnForced && closing() && Date.now() >= returnDueAt) {
        returnForced = true;
        if (!sentWhileClosing) {
            owedSkips += 1;
        }
        crossDomainJourney();
        sleep(randInt(0, 1));
        return;
    }
    let r = rand();
    if (r >= 0.98 && pendingReturn !== null && sentWhileClosing && skipThisTurn && closing()) {
        // 마감 뒤 보낸 짝을 앞당기기 전에 이체 차례가 왔다: 이 차례가 되돌리기이자 거를 차례다.
        skipThisTurn = false;
    } else if (r >= 0.98 && pendingReturn === null && closing()) {
        // 마감 뒤 이체 차례: 빚진 차례를 갚거나, 번갈아 짝 보내기와 다른 여정(비중은 이체를 뺀 나머지 그대로)을 한다.
        if (owedSkips > 0) {
            owedSkips -= 1;
            r = rand() * 0.98;
        } else {
            if (skipThisTurn || runEnd() - Date.now() < LAST_SEND_MS) {
                r = rand() * 0.98;
            }
            if (runEnd() - Date.now() >= LAST_SEND_MS) {
                skipThisTurn = !skipThisTurn;
            }
        }
    }
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
