// core-banking k6 상주 부하 스크립트. commerce/loadgen/script.js 의 constant-arrival-rate +
// mulberry32(per-VU 시드 분기) 패턴을 그대로 따른다. 여정 비중: 잔액조회/거래내역 위주(read 80%+)
// + 소액 이체(banking 은 낮은 빈도가 현실적이라 write 비중을 commerce 보다 낮게 잡음).
import http from 'k6/http';
import { sleep, check } from 'k6';

const TARGET_RPS = Number(__ENV.TARGET_RPS || 2);
const BASE_SEED = Number(__ENV.LOADGEN_SEED || 42);
const GATEWAY_URL = __ENV.GATEWAY_URL || 'http://testbed-nginx';
const PRE_ALLOCATED_VUS = Number(__ENV.PRE_ALLOCATED_VUS || 10);
const MAX_VUS = Number(__ENV.MAX_VUS || 30);

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
  thresholds: {},
};

// mulberry32 — commerce loadgen 과 동일 구현. VU 별로 시드를 분기(SEED + VU*7919)해
// 재현성과 VU 간 비상관성을 함께 확보.
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
  return Math.floor(rand() * (max - min + 1)) + min;
}

// 활성(ACTIVE) 개인 계좌만 사용 — ACC-9001(FROZEN)/ACC-9002(CLOSED) 제외.
const ACTIVE_ACCOUNTS = ['ACC-1001', 'ACC-1002', 'ACC-1003', 'ACC-1004', 'ACC-1005', 'ACC-1006', 'ACC-1007'];

function pick(arr) {
  return arr[randInt(0, arr.length - 1)];
}

function balanceCheckJourney() {
  const id = pick(ACTIVE_ACCOUNTS);
  const res = http.get(`${GATEWAY_URL}/api/accounts/${id}`);
  check(res, { 'balance check status is 200': (r) => r.status === 200 });
}

function transactionHistoryJourney() {
  const id = pick(ACTIVE_ACCOUNTS);
  const res = http.get(`${GATEWAY_URL}/api/transfers?fromAccount=${id}`);
  check(res, { 'transaction history status ok': (r) => r.status === 200 || r.status === 404 });
}

function accountListJourney() {
  const res = http.get(`${GATEWAY_URL}/api/accounts?status=ACTIVE&size=20`);
  check(res, { 'account list status ok': (r) => r.status === 200 || r.status === 404 });
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
  });
  check(res, { 'transfer status ok': (r) => r.status === 200 });
}

export default function () {
  const r = rand();
  // read 80%+ : 잔액조회 55% / 거래내역 조회 25% / 계좌목록 10%, write 10%: 소액 이체.
  if (r < 0.55) {
    balanceCheckJourney();
  } else if (r < 0.8) {
    transactionHistoryJourney();
  } else if (r < 0.9) {
    accountListJourney();
  } else {
    smallTransferJourney();
  }
  sleep(randInt(0, 1));
}
