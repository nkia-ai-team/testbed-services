// ============================================================
// core-banking 정산 대사 조회 부하 (F70-R 전용) — testbed-services docs/scenario-redesign-wip/design-F70-R-sheet.md
// ============================================================
// baseline loadgen(core-banking/loadgen/script.js, 불가침 — R1)과 별개로, 시나리오가
// 켰다 끄는 부하다. 두 갈래를 함께 돌린다.
//
//   1) journeys: surge.js 와 같은 여정·엔드포인트·시드 계좌 계약을 transfer-heavy-surge.js
//      와 같은 비중(이체 40 / 조회 35 / 내역 15 / 목록 10)으로 TARGET_RPS 만큼 흘린다.
//      step 태그(transfer, get)는 surge.js 와 같아 load.north_south 도메인 프로파일
//      (business_step=transfer, read_step=get)의 관측 규약을 그대로 쓴다.
//   2) statement_export: 가맹 정산 대사 시스템 하나가 한 달치 완료 이체를 받아 가는
//      조회를 흉내 낸다. 거래 내역 API 의 공개 계약(page/size 는 선택, 빼면 전부 반환)대로
//      page 없이 GET /api/transfers?status=COMPLETED&from=<30일 전 0시> 를 부르고,
//      응답을 받든 실패하든 EXPORT_PAUSE 뒤 다시 부른다. 한 번에 한 건(VU 1)이다.
//      step 태그는 'export' 라 러너 monitor 의 업무, 조회 집계에 들어가지 않는다.
//
// ramping-arrival-rate + mulberry32 시드 재현성(R4)은 surge.js 와 같다.
import http from 'k6/http';
import { check, sleep } from 'k6';

const GATEWAY_URL = __ENV.GATEWAY_URL || 'http://192.168.122.77:30082';
const TARGET_RPS = Number(__ENV.TARGET_RPS || 5);
const RAMP_UP = __ENV.RAMP_UP || '2m';
const HOLD = __ENV.HOLD || '13m';
const RAMP_DOWN = __ENV.RAMP_DOWN || '1m';
const SURGE_SEED = Number(__ENV.SURGE_SEED || 4242);
const PRE_ALLOCATED_VUS = Number(__ENV.PRE_ALLOCATED_VUS || 30);
const MAX_VUS = Number(__ENV.MAX_VUS || 250);
// 대사 시스템의 재조회 간격과 기간(일). 실패해도 같은 간격으로 다시 부른다.
const EXPORT_PAUSE_SEC = Number(__ENV.EXPORT_PAUSE_SEC || 60);
const EXPORT_DAYS = Number(__ENV.EXPORT_DAYS || 30);

export const options = {
    scenarios: {
        journeys: {
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
        statement_export: {
            executor: 'constant-vus',
            exec: 'statementExport',
            vus: 1,
            startTime: RAMP_UP,
            duration: HOLD,
            gracefulStop: '0s',
        },
    },
    // 장애 중 5xx/timeout 은 이 시나리오가 만들려는 증상 그 자체 — check() 로만 관측한다.
    thresholds: {},
};

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

// 시드 데이터 계약 — baseline(script.js), surge.js 와 같다.
const ACTIVE_ACCOUNTS = ['ACC-1001', 'ACC-1002', 'ACC-1003', 'ACC-1004', 'ACC-1005', 'ACC-1006', 'ACC-1007'];

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

function balanceCheckJourney() {
    const id = pick(ACTIVE_ACCOUNTS);
    const res = http.get(`${GATEWAY_URL}/api/accounts/${id}`, {
        tags: { journey: 'surge-balance', step: 'get' },
    });
    check(res, { 'balance check not 5xx': (r) => r.status < 500 });
}

function transactionHistoryJourney() {
    const id = pick(ACTIVE_ACCOUNTS);
    const res = http.get(`${GATEWAY_URL}/api/transfers?fromAccount=${id}&page=0&size=20`, {
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

// 여정 가중(누적 확률) — transfer-heavy-surge.js 와 같다: 이체 40 / 조회 35 / 내역 15 / 목록 10.
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

function exportSince() {
    const d = new Date(Date.now() - EXPORT_DAYS * 86400 * 1000);
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}T00:00:00`;
}

export function statementExport() {
    const res = http.get(`${GATEWAY_URL}/api/transfers?status=COMPLETED&from=${exportSince()}`, {
        timeout: '90s',
        tags: { journey: 'statement-export', step: 'export' },
    });
    check(res, { 'statement export 200': (r) => r.status === 200 });
    sleep(EXPORT_PAUSE_SEC);
}
