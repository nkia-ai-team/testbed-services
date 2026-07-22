// ============================================================
// core-banking surge 부하 (시나리오 전용) — testbed-services docs/spec-scenario-load.md
// ============================================================
// baseline loadgen(tb-runner 상주, core-banking/loadgen/script.js, 불가침 — R1)과
// 별개로, 시나리오가 켰다 끄는 폭주 부하다. commerce/loadgen/surge.js 와 동일한
// ramping-arrival-rate + mulberry32 시드 재현성 계약(R4)을 따르되, 여정 믹스·
// 엔드포인트·시드 데이터는 core-banking baseline(script.js)의 계약을 그대로 쓴다
// (창작 금지 — 조회 55% / 거래내역 25% / 계좌목록 10% / 이체 10%).
//
// §8.1-1 실측(2026-07-15, 조회55/내역25/목록10/이체10 믹스): 건강 상한 surge
// 20rps(총 ~24), 무릎은 20~40 사이로만 확인 — commerce보다 훨씬 낮다. 20~40
// 구간이 정밀화되기 전에는 banking 부하 시나리오의 golden 양성 강도를 확정하지
// 않고 calibrate로 유지하라는 스펙 권고에 따라, TARGET_RPS 기본값은 실측 건강
// 상한(20rps)을 넘지 않는 보수값으로 둔다. 무릎 이상을 시험하려면 env로 명시
// 오버라이드할 것.
import http from 'k6/http';
import { check } from 'k6';

const GATEWAY_URL = __ENV.GATEWAY_URL || 'http://192.168.122.77:30082';
const TARGET_RPS = Number(__ENV.TARGET_RPS || 20);
const RAMP_UP = __ENV.RAMP_UP || '2m';
const HOLD = __ENV.HOLD || '8m';
const RAMP_DOWN = __ENV.RAMP_DOWN || '1m';
const SURGE_SEED = Number(__ENV.SURGE_SEED || 4242);
// s40(surge 40rps)에서 p95 4.7s 실측(무릎 지남) — 저 RPS라도 지연이 늘면 VU가
// 크게 소모된다. TARGET_RPS를 무릎 쪽으로 올려 쓰는 calibration 실행을 감안해
// baseline(10/30)보다 넉넉히 잡는다.
const PRE_ALLOCATED_VUS = Number(__ENV.PRE_ALLOCATED_VUS || 30);
const MAX_VUS = Number(__ENV.MAX_VUS || 250);

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
    // 폭주 중 5xx/timeout은 이 시나리오가 만들려는 증상 그 자체 — 실패로 run을
    // 중단하지 않고 check() 통계로만 관측한다.
    thresholds: {},
};

// 재현성: loadgen(script.js)과 동일한 mulberry32 + VU 분기 규약 (R4).
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

// 시드 데이터 계약 — baseline(script.js)과 동일. 활성(ACTIVE) 개인 계좌만 사용
// (ACC-9001 FROZEN / ACC-9002 CLOSED 제외). §8.1-1 실측 전처리(활성 계좌 잔액
// 1억 상향)가 적용된 환경을 전제로 한다 — 없으면 이체가 잔액부족으로 fast-fail
// 한다(notes 참조).
const ACTIVE_ACCOUNTS = ['ACC-1001', 'ACC-1002', 'ACC-1003', 'ACC-1004', 'ACC-1005', 'ACC-1006', 'ACC-1007'];

function balanceCheckJourney() {
    const id = pick(ACTIVE_ACCOUNTS);
    const res = http.get(`${GATEWAY_URL}/api/accounts/${id}`, {
        tags: { journey: 'surge-balance', step: 'get' },
    });
    // 폭주 중 5xx가 곧 증상 — 200 단정 대신 not-5xx로 관측만 한다.
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

function smallTransferJourney() {
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
    // baseline은 200 단정(잔액 충분 전제 — 잔액부족은 §8.1-1 전처리로 회피).
    // surge 중에는 폭주 자체가 만드는 5xx를 실패로 취급하지 않되, 잔액부족(4xx)과는
    // 구분해 not-5xx로만 관측한다.
    check(res, { 'transfer not 5xx': (r) => r.status < 500 });
}

export default function () {
    const r = rand();
    // baseline(script.js)과 동일 믹스 — 조회 55% / 거래내역 25% / 계좌목록 10% / 이체 10%.
    if (r < 0.55) {
        balanceCheckJourney();
    } else if (r < 0.8) {
        transactionHistoryJourney();
    } else if (r < 0.9) {
        accountListJourney();
    } else {
        smallTransferJourney();
    }
}
