// ============================================================
// core-banking transfer-balance surge 부하 (F47-R, F63-R 동반 부하), testbed-services docs/spec-scenario-load.md
// ============================================================
// transfer-heavy-surge.js(이체 40 / 잔액 35 / 내역 15 / 목록 10)에서 거래 내역 단계만 뺀 파생본.
// 거래 내역(GET /api/transfers)은 transfer 의 건수 질의(Oracle sql_id byb2y2hv7p4ag, 실행당
// 논리 읽기 약 11만)를 돌린다. 20rps 의 15%(약 3건/초)만으로 Oracle CPU 를 상한(2)까지 채워
// account, transfer Hikari 풀이 고갈됐다(F47-R-run-3d446438, F63-R-run-81153e98). 그러면 러너가
// 설계 기전이 아니라 DB 갈래 피해로 성공을 판정한다. 그래서 내역 몫(15%)을 이체로 옮긴다
// (이체 55 / 잔액 35 / 목록 10). api 를 거치는 요청(이체 + 내역) 비율은 55% 로 같아,
// 같은 target_rps 면 api 요청률이 그대로다(20rps 면 약 11건/초).
// 여정, 엔드포인트, 태그(step 'transfer', 'get', 'list'), 시드 데이터, 난수 규약은
// transfer-heavy-surge.js 와 같다. load.north_south 도메인 프로파일(banking entry,
// business_step=transfer, read_step=get)의 이체 비율과 잔액 조회 관측 매칭을 그대로 쓴다.
import http from 'k6/http';
import { check } from 'k6';

const GATEWAY_URL = __ENV.GATEWAY_URL || 'http://192.168.122.77:30082';
// 실행기가 늘 TARGET_RPS 를 넘긴다. 기본값은 F63-R 설계 강도(20rps)다.
const TARGET_RPS = Number(__ENV.TARGET_RPS || 20);
const RAMP_UP = __ENV.RAMP_UP || '2m';
const HOLD = __ENV.HOLD || '8m';
const RAMP_DOWN = __ENV.RAMP_DOWN || '1m';
const SURGE_SEED = Number(__ENV.SURGE_SEED || 4242);
// VU 여유는 transfer-heavy-surge.js 와 같게 둔다(응답이 늦어지면 in-flight VU 가
// arrival-rate × latency 로 는다).
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
    // 지연과 시간 초과(시나리오가 만들려는 증상)는 실패로 run 을 멈추지 않고
    // check() 통계로만 관측한다.
    thresholds: {},
};

// 재현성: transfer-heavy-surge.js 와 같은 mulberry32 + VU 분기 규약 (R4).
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

// 시드 데이터 계약: transfer-heavy-surge.js 와 같다. 활성(ACTIVE) 개인 계좌만
// 쓴다(ACC-9001 FROZEN, ACC-9002 CLOSED 제외).
const ACTIVE_ACCOUNTS = ['ACC-1001', 'ACC-1002', 'ACC-1003', 'ACC-1004', 'ACC-1005', 'ACC-1006', 'ACC-1007'];

function balanceCheckJourney() {
    const id = pick(ACTIVE_ACCOUNTS);
    const res = http.get(`${GATEWAY_URL}/api/accounts/${id}`, {
        tags: { journey: 'surge-balance', step: 'get' },
    });
    check(res, { 'balance check not 5xx': (r) => r.status < 500 });
}

function accountListJourney() {
    const res = http.get(`${GATEWAY_URL}/api/accounts?status=ACTIVE&size=20`, {
        tags: { journey: 'surge-account-list', step: 'list' },
    });
    check(res, { 'account list not 5xx': (r) => r.status < 500 });
}

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
    check(res, { 'transfer not 5xx': (r) => r.status < 500 });
}

// 여정 가중(누적 확률): 이체 55 / 잔액 35 / 목록 10. 거래 내역은 없다.
export default function () {
    const r = rand();
    if (r < 0.55) {
        transferJourney();
    } else if (r < 0.90) {
        balanceCheckJourney();
    } else {
        accountListJourney();
    }
}
