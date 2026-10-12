// ============================================================
// core-banking 가맹점 입금 확인 폴링 부하 (F74-R 전용) — testbed-services docs/scenario-redesign-wip/design-F74-R-sheet.md
// ============================================================
// baseline loadgen(core-banking/loadgen/script.js, 불가침 — R1)과 별개로, 시나리오가
// 켰다 끄는 부하다. 두 갈래를 함께 돌린다.
//
//   1) journeys: surge.js 와 같은 엔드포인트·시드 계좌 계약으로 이체 50 / 잔액 조회 40 /
//      계좌 목록 10 을 TARGET_RPS 만큼 흘린다. 거래 내역 단계는 넣지 않는다 — 그 건수
//      질의가 이 시나리오의 원인 경로라, 동반 부하가 같은 질의를 보태면 원인이 흐려진다
//      (F47-R 실행 3d446438 에서 transfer-heavy-surge.js 의 내역 15% 만으로 Oracle CPU 가
//      상한에 붙었다). step 태그(transfer, get)는 surge.js 와 같아 load.north_south 도메인
//      프로파일(business_step=transfer, read_step=get)의 관측 규약을 그대로 쓴다.
//   2) arrival_poll: 가맹점 입금 알림을 해 주는 제휴 연동 하나가 가맹점 정산 계좌마다
//      "오늘 완료된 입금이 한 건이라도 있는가"를 거래 내역 API 로 확인하는 폴링을 흉내 낸다.
//      맨 앞 한 건만 받으면 되므로 page=0&size=1 로 부른다. 확인 주기를 POLL_INTERVAL_SEC
//      로 줄인 새 버전이 나간 상태다(계좌 수 / 주기 = 초당 호출 수, 실패해도 같은 주기로
//      다시 부른다). step 태그는 'poll' 이라 러너 monitor 의 업무, 조회 집계에 들어가지 않는다.
//
// ramping-arrival-rate + mulberry32 시드 재현성(R4)은 surge.js 와 같다.
import http from 'k6/http';
import { check } from 'k6';
import exec from 'k6/execution';

const GATEWAY_URL = __ENV.GATEWAY_URL || 'http://192.168.122.77:30082';
const TARGET_RPS = Number(__ENV.TARGET_RPS || 5);
const RAMP_UP = __ENV.RAMP_UP || '2m';
const HOLD = __ENV.HOLD || '13m';
const RAMP_DOWN = __ENV.RAMP_DOWN || '1m';
const SURGE_SEED = Number(__ENV.SURGE_SEED || 4242);
const PRE_ALLOCATED_VUS = Number(__ENV.PRE_ALLOCATED_VUS || 30);
const MAX_VUS = Number(__ENV.MAX_VUS || 250);
// 제휴 연동의 계좌당 확인 주기(초). 예전 버전은 60초였다.
const POLL_INTERVAL_SEC = Number(__ENV.POLL_INTERVAL_SEC || 1);
const POLL_TIMEOUT = __ENV.POLL_TIMEOUT || '30s';

// 시드 데이터 계약 — baseline(script.js), surge.js 와 같다.
const ACTIVE_ACCOUNTS = ['ACC-1001', 'ACC-1002', 'ACC-1003', 'ACC-1004', 'ACC-1005', 'ACC-1006', 'ACC-1007'];
// 입금을 확인하는 가맹점 정산 계좌: 시드 계좌 7개와 commerce 정산이 들어오는 commerce-merchant.
const MERCHANT_ACCOUNTS = ACTIVE_ACCOUNTS.concat(['commerce-merchant']);
const POLL_RATE = Math.max(1, Math.round(MERCHANT_ACCOUNTS.length / POLL_INTERVAL_SEC));

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
        arrival_poll: {
            executor: 'constant-arrival-rate',
            exec: 'arrivalPoll',
            rate: POLL_RATE,
            timeUnit: '1s',
            preAllocatedVUs: 20,
            maxVUs: 200,
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

function accountListJourney() {
    const res = http.get(`${GATEWAY_URL}/api/accounts?status=ACTIVE&size=20`, {
        tags: { journey: 'surge-account-list', step: 'list' },
    });
    check(res, { 'account list not 5xx': (r) => r.status < 500 });
}

// 여정 가중(누적 확률): 이체 50 / 잔액 조회 40 / 계좌 목록 10. 거래 내역은 넣지 않는다(위 머리말).
export default function () {
    const r = rand();
    if (r < 0.50) {
        transferJourney();
    } else if (r < 0.90) {
        balanceCheckJourney();
    } else {
        accountListJourney();
    }
}

function todayStart() {
    const d = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}T00:00:00`;
}

// 계좌를 차례로 돌며 확인한다(시나리오 전체 반복 번호로 고른다).
export function arrivalPoll() {
    const account = MERCHANT_ACCOUNTS[exec.scenario.iterationInTest % MERCHANT_ACCOUNTS.length];
    const res = http.get(
        `${GATEWAY_URL}/api/transfers?toAccount=${account}&status=COMPLETED&from=${todayStart()}&page=0&size=1`,
        {
            timeout: POLL_TIMEOUT,
            tags: { journey: 'settlement-arrival-poll', step: 'poll' },
        },
    );
    check(res, { 'arrival poll 200': (r) => r.status === 200 });
}
