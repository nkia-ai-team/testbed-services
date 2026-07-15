// ============================================================
// core-banking 용량 탐색 프로브 (1회성 계측 도구) — docs/spec-scenario-load.md §8
// ============================================================
// commerce/loadgen/capacity-probe.js 와 동일한 계단식 정률 부하 패턴.
// 여정 믹스·시드 계약은 baseline(script.js)과 동일해야 한다 — 잔액조회 55% /
// 거래내역 25% / 계좌목록 10% / 소액이체 10%.
// 5xx/timeout만 실패로 집계한다(4xx — 잔액 부족 등 — 는 여정 내 정상 거절).
// 이체는 7개 활성 계좌 사이 순환이라 총액 보존 — 시작 잔액 충분성만 사전 확인.
import http from 'k6/http';
import { check } from 'k6';

const GATEWAY_URL = __ENV.GATEWAY_URL || 'http://192.168.122.77:30082';
const STEP_RPS = (__ENV.STEP_RPS || '20,40,60,80,100,120').split(',').map(Number);
const STEP_HOLD_SEC = Number(__ENV.STEP_HOLD_SEC || 180);
const STEP_GAP_SEC = Number(__ENV.STEP_GAP_SEC || 60);
const SURGE_SEED = Number(__ENV.SURGE_SEED || 4242);

http.setResponseCallback(http.expectedStatuses({ min: 200, max: 499 }));

const scenarios = {};
const thresholds = {};
let offsetSec = 0;
for (const rps of STEP_RPS) {
    const name = `s${rps}`;
    scenarios[name] = {
        executor: 'constant-arrival-rate',
        rate: rps,
        timeUnit: '1s',
        duration: `${STEP_HOLD_SEC}s`,
        startTime: `${offsetSec}s`,
        preAllocatedVUs: Math.min(rps * 2, 200),
        maxVUs: rps * 6,
        exec: 'mix',
    };
    thresholds[`http_req_duration{scenario:${name}}`] = [];
    thresholds[`http_req_failed{scenario:${name}}`] = [];
    thresholds[`dropped_iterations{scenario:${name}}`] = [];
    offsetSec += STEP_HOLD_SEC + STEP_GAP_SEC;
}

export const options = { scenarios, thresholds };

// ----- 이하 여정은 baseline script.js 와 동일 계약 (mulberry32·활성 계좌 7) -----
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

const ACTIVE_ACCOUNTS = ['ACC-1001', 'ACC-1002', 'ACC-1003', 'ACC-1004', 'ACC-1005', 'ACC-1006', 'ACC-1007'];

function balanceCheck() {
    const id = pick(ACTIVE_ACCOUNTS);
    const res = http.get(`${GATEWAY_URL}/api/accounts/${id}`, { tags: { journey: 'balance' } });
    check(res, { 'balance 200': (r) => r.status === 200 });
}

function transactionHistory() {
    const id = pick(ACTIVE_ACCOUNTS);
    const res = http.get(`${GATEWAY_URL}/api/transfers?fromAccount=${id}`, { tags: { journey: 'history' } });
    check(res, { 'history ok': (r) => r.status === 200 || r.status === 404 });
}

function accountList() {
    const res = http.get(`${GATEWAY_URL}/api/accounts?status=ACTIVE&size=20`, { tags: { journey: 'list' } });
    check(res, { 'list ok': (r) => r.status === 200 || r.status === 404 });
}

function smallTransfer() {
    const from = pick(ACTIVE_ACCOUNTS);
    let to = pick(ACTIVE_ACCOUNTS);
    while (to === from) {
        to = pick(ACTIVE_ACCOUNTS);
    }
    const res = http.post(
        `${GATEWAY_URL}/api/transfers`,
        JSON.stringify({ fromAccount: from, toAccount: to, amount: randInt(1000, 50000), orderId: null }),
        { headers: { 'Content-Type': 'application/json' }, tags: { journey: 'transfer' } }
    );
    check(res, { 'transfer not 5xx': (r) => r.status < 500 });
}

export function mix() {
    const r = rand();
    if (r < 0.55) {
        balanceCheck();
    } else if (r < 0.8) {
        transactionHistory();
    } else if (r < 0.9) {
        accountList();
    } else {
        smallTransfer();
    }
}
