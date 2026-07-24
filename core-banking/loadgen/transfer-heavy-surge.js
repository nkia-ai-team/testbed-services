// ============================================================
// core-banking transfer-heavy surge 부하 (F21-P 전용) — testbed-services docs/spec-scenario-load.md
// ============================================================
// surge.js(조회55/내역25/목록10/이체10, §8.1-1 실측 무릎 20~40rps)의 파생본. F21-P는
// api-service Tomcat 200 스레드 포화를 만들어야 하는데, 이체 점유시간 d≈8s에서
// Little's law(L=λ·W)로 L=200 스레드엔 λ≈25 transfer-req/s가 지속 필요하다.
// surge.js 그대로 쓰면 이체 비중이 10%뿐이라 λ=25rps엔 총 250rps가 필요해
// load.north_south target_rps 상한(180, profiles.json)을 넘는다. 이 스크립트는
// 이체 비중을 40%로 올려 총 rps를 180 한도 안에서 낮게 유지한 채(=목표 90~110rps)
// λ≈25 transfer-rps를 달성한다(90*0.4=36rps ≥25, 캘리브레이션 여유 확보).
// 여정·엔드포인트·시드 데이터 계약은 surge.js/banking baseline(script.js)과 동일 —
// 창작하지 않는다. business_step 태그('transfer')도 surge.js와 동일하게 유지해
// load.north_south 도메인 프로파일(banking entry, business_step=transfer)의
// 이체-비율/2xx 관측 매칭을 그대로 재사용한다.
import http from 'k6/http';
import { check } from 'k6';

const GATEWAY_URL = __ENV.GATEWAY_URL || 'http://192.168.122.77:30082';
// 기본값 근거: λ≈25 transfer-rps를 이체비중 40%로 얻으려면 총 rps ≈ 25/0.4 = 62.5.
// 지연 주입 중 응답이 느려져(목표 ~8s) VU 소모가 커지므로 캘리브레이션 여유를 두고
// 90으로 시작한다(90*0.4=36 transfer-rps, 목표 25 대비 여유 확보). load.north_south
// target_rps.maximum(180)에 크게 못 미쳐 상한 위반 없음.
const TARGET_RPS = Number(__ENV.TARGET_RPS || 90);
const RAMP_UP = __ENV.RAMP_UP || '2m';
const HOLD = __ENV.HOLD || '8m';
const RAMP_DOWN = __ENV.RAMP_DOWN || '1m';
const SURGE_SEED = Number(__ENV.SURGE_SEED || 4242);
// 이체 요청이 지연 주입 중 최대 ~8-10s 걸릴 수 있어(F21-P 목표) surge.js보다
// VU 여유를 더 크게 잡는다(응답시간 상승 시 in-flight VU가 arrival-rate×latency로 는다).
const PRE_ALLOCATED_VUS = Number(__ENV.PRE_ALLOCATED_VUS || 60);
const MAX_VUS = Number(__ENV.MAX_VUS || 400);

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
    // 폭주 중 지연/timeout(F21-P가 만들려는 증상 그 자체)은 실패로 run을 중단하지
    // 않고 check() 통계로만 관측한다.
    thresholds: {},
};

// 재현성: loadgen(script.js)/surge.js와 동일한 mulberry32 + VU 분기 규약 (R4).
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

// 시드 데이터 계약 — baseline(script.js)/surge.js와 동일. 활성(ACTIVE) 개인 계좌만
// 사용(ACC-9001 FROZEN / ACC-9002 CLOSED 제외). §8.1-1 실측 전처리(활성 계좌 잔액
// 1억 상향) 전제.
const ACTIVE_ACCOUNTS = ['ACC-1001', 'ACC-1002', 'ACC-1003', 'ACC-1004', 'ACC-1005', 'ACC-1006', 'ACC-1007'];

function balanceCheckJourney() {
    const id = pick(ACTIVE_ACCOUNTS);
    const res = http.get(`${GATEWAY_URL}/api/accounts/${id}`, {
        tags: { journey: 'surge-balance', step: 'get' },
    });
    check(res, { 'balance check not 5xx': (r) => r.status < 500 });
}

function transactionHistoryJourney() {
    const id = pick(ACTIVE_ACCOUNTS);
    const res = http.get(`${GATEWAY_URL}/api/transfers?fromAccount=${id}`, {
        tags: { journey: 'surge-history', step: 'list' },
    });
    check(res, { 'transaction history not 5xx': (r) => r.status < 500 });
}

function accountListJourney() {
    const res = http.get(`${GATEWAY_URL}/api/accounts?status=ACTIVE&size=20`, {
        tags: { journey: 'surge-account-list', step: 'list' },
    });
    check(res, { 'account list not 5xx': (r) => r.status < 500 });
}

// F21-P 핵심 여정 — surge.js의 smallTransferJourney와 엔드포인트/태그(step:'transfer')
// 동일, 비중만 40%로 상향(surge.js 대비 4배). tags.step은 surge.js와 동일하게
// 'transfer'를 유지해 load.north_south 도메인 프로파일(business_step=transfer)의
// 기존 이체-비율/2xx 관측 매칭 규약을 그대로 재사용한다.
function transferJourney() {
    const from = pick(ACTIVE_ACCOUNTS);
    let to = pick(ACTIVE_ACCOUNTS);
    while (to === from) {
        to = pick(ACTIVE_ACCOUNTS);
    }
    const payload = JSON.stringify({
        fromAccount: from,
        toAccount: to,
        amount: randInt(1000, 50000),
        orderId: null,
    });
    const res = http.post(`${GATEWAY_URL}/api/transfers`, payload, {
        headers: { 'Content-Type': 'application/json' },
        tags: { journey: 'surge-transfer', step: 'transfer' },
    });
    // F21-P는 지연이 정답 신호이지 5xx가 아니다(느린 200) — not-5xx로만 관측.
    check(res, { 'transfer not 5xx': (r) => r.status < 500 });
}

// 여정 가중(누적 확률) — 이체 40 / 조회 35 / 내역 15 / 목록 10.
// surge.js(조회55/내역25/목록10/이체10)와 반대로 이체를 압도적 비중으로 올려
// api Tomcat 200 포화에 필요한 λ≈25 transfer-rps를 낮은 총 rps로 달성한다.
export default function () {
    const r = rand();
    if (r < 0.40) {
        transferJourney();
    } else if (r < 0.75) {
        balanceCheckJourney();
    } else if (r < 0.90) {
        transactionHistoryJourney();
    } else {
        accountListJourney();
    }
}
