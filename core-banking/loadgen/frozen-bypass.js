// ============================================================
// core-banking F17-P dual-arm 부하 (시나리오 전용) — FROZEN/CLOSED 계좌
// 무결성 우회 검증. docs/scenario-redesign-wip/design-F17-P-sheet.md 정본.
// ============================================================
// 매 iteration 마다 완전히 동일한 이체 payload를 두 경로로 흘린다:
//   direct arm  → testbed-transfer-external NodePort 30282 (account 검증 우회,
//                 TransferService.execute()가 계좌 status를 검사하지 않아 200
//                 COMPLETED로 성공 — 이게 시나리오가 증명하려는 위반)
//   control arm → 정상경로 30082 (nginx→api→account.validateAndForward,
//                 AccountService.java:66-71 ACTIVE 검사가 400으로 거절)
// 두 경로가 같은 payload로 다른 결과를 내는 것 자체가 결정적 증거(경로 비대칭).
//
// 관측 배선(merge-spec-F17-P.md 참조): direct arm은 tag step="frozen-direct"
// (domain_profile.business_step), control arm은 tag step="frozen-control"
// (domain_profile.read_step) — north-south executor의 기존 monitor.py가 이미
// business_step/read_step 두 스트림을 각각 2xx/4xx/5xx·nonok율로 집계하므로
// 신규 모니터 코드 없이 그대로 재사용 가능하다.
//
// baseline(script.js)·surge.js와 동일하게 FROZEN/CLOSED 계좌·소액 range는
// 창작 금지 원칙에 따라 시드 상수로 고정한다(executor 파라미터 계약은 9키
// 고정이라 이 값들은 env로 넘길 수 없다 — surge.js의 ACTIVE_ACCOUNTS와 동일 관례).
import http from 'k6/http';
import { check } from 'k6';

// direct arm URL은 executor 계약(entry_url→gateway_env)으로 바인딩된다.
const DIRECT_URL = __ENV.TRANSFER_DIRECT_URL || 'http://192.168.122.77:30282';
// control arm URL은 계약에 없는 스크립트 내부 상수 — surge.js GATEWAY_URL
// 기본값과 동일하게 고정값으로 둔다(30082는 이미 다른 시나리오의 domain_profile로
// 등록돼 있으나 이 스크립트에서는 대조군 호출일 뿐 executor에 노출하지 않는다).
const NORMAL_URL = __ENV.TRANSFER_NORMAL_URL || 'http://192.168.122.77:30082';

const TARGET_RPS = Number(__ENV.TARGET_RPS || 20);
const RAMP_UP = __ENV.RAMP_UP || '1m';
const HOLD = __ENV.HOLD || '8m';
const RAMP_DOWN = __ENV.RAMP_DOWN || '1m';
const SURGE_SEED = Number(__ENV.SURGE_SEED || 4242);
const PRE_ALLOCATED_VUS = Number(__ENV.PRE_ALLOCATED_VUS || 20);
const MAX_VUS = Number(__ENV.MAX_VUS || 100);

export const options = {
    scenarios: {
        frozen_bypass: {
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
    // direct arm의 200/control arm의 400은 각각 이 시나리오가 증명하려는
    // 정상 거동이지 실패가 아니다 — check()로만 관측, threshold로 run을 막지 않는다.
    thresholds: {},
};

// 재현성: loadgen(script.js)·surge.js와 동일한 mulberry32 + VU 분기 규약(R4).
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

// 시드 데이터 계약 — init.sql:91-92(FROZEN/CLOSED)·surge.js:69(ACTIVE_ACCOUNTS)와 동일 상수.
const FROZEN_FROM_ACCOUNT = 'ACC-9001'; // FROZEN, 잔액 100000
const CLOSED_TO_ACCOUNT = 'ACC-9002';   // CLOSED, 잔액 0
const ACTIVE_COUNTERPARTY = 'ACC-1001'; // ACTIVE, 상대방 계좌
const AMOUNT_MIN = 1000;
const AMOUNT_MAX = 5000; // FROZEN 잔액(100000)에 항상 충분 — balance 검사(TransferService.java:77)로
                          // FAILED가 섞여 direct arm 200 판정이 흐려지는 것을 방지(설계시트 §2 must_rule_out).

// direct arm(위반 유발) 발생 비율 — 매 iteration마다 흘리지 않는다. 두 가지 잔액
// 제약이 있다: (1) variant A는 ACC-9001(FROZEN, 잔액 100000)에서 매번 차감되므로
// TARGET_RPS 전체를 흘리면 수 초 안에 고갈된다. (2) variant B는 ACC-1001에서
// 차감되는데 이 계좌는 surge.js의 ACTIVE_ACCOUNTS 풀에 속한 **공유 baseline
// 계좌**다(core-banking/loadgen/surge.js:69) — 8분 hold 내내 흘리면 baseline
// 잔액을 고갈시켜 이 시나리오가 끝난 뒤에도 다른 시나리오/베이스라인의 이체가
// 잔액부족으로 조용히 실패하는 오염을 남긴다(project CLAUDE.md에 기록된
// commerce-settlement 계좌 고갈 실패 사례와 동형 위험). integrity_violation_count
// 성공조건은 절대값 count>0(단조증가, 1건만 있어도 영구 충족)이므로 낮은 비율로도
// 충분하다 — 전량을 direct에 흘릴 필요가 없다.
const DIRECT_ARM_RATIO = 0.02;

// A: FROZEN 계좌가 출금 당사자(from) — account-service ACTIVE 검사(:66) 우회 대상.
// B: CLOSED 계좌가 입금 당사자(to) — account-service ACTIVE 검사(:69) 우회 대상.
// 두 변형을 섞어 "FROZEN/CLOSED 계좌 모두 200 COMPLETED"라는 must_support 범위를 채운다.
function buildPayload() {
    const variant = rand() < 0.5 ? 'A' : 'B';
    const fromAccount = variant === 'A' ? FROZEN_FROM_ACCOUNT : ACTIVE_COUNTERPARTY;
    const toAccount = variant === 'A' ? ACTIVE_COUNTERPARTY : CLOSED_TO_ACCOUNT;
    return {
        variant,
        body: JSON.stringify({
            fromAccount,
            toAccount,
            amount: randInt(AMOUNT_MIN, AMOUNT_MAX),
            orderId: null,
        }),
    };
}

export default function () {
    // 두 arm은 반드시 동일 payload여야 경로 비대칭 증거가 성립한다(설계시트 §1 대조).
    const { body } = buildPayload();
    const headers = { headers: { 'Content-Type': 'application/json' } };

    // DIRECT_ARM_RATIO만 direct+control 쌍으로 흘리고(잔액 소모 억제), 나머지는
    // control만 반복해 normal_path_frozen_reject_rate(read_nonok_rate 재사용,
    // must_rule_out ≥0.9)에 필요한 물량을 확보한다. control만 있는 iteration도
    // FROZEN/CLOSED 당사자 payload이므로 대조군으로서 동일하게 유효하다.
    const fireDirect = rand() < DIRECT_ARM_RATIO;

    if (fireDirect) {
        const direct = http.post(`${DIRECT_URL}/api/transfers`, body, {
            ...headers,
            tags: { journey: 'frozen-direct', step: 'frozen-direct', path_class: 'direct' },
        });
        check(direct, {
            // account 우회 경로 — FROZEN/CLOSED 당사자인데도 200 COMPLETED로 성공하는 것이
            // 이 시나리오의 정답 증상이다(TransferService.java:86). balance 검사만 통과하면
            // 항상 COMPLETED이므로(AMOUNT_MAX가 FROZEN 잔액보다 훨씬 작음) FAILED는 기대하지 않는다.
            'direct arm bypasses status check (200)': (r) => r.status === 200,
        });
    }

    const control = http.post(`${NORMAL_URL}/api/transfers`, body, {
        ...headers,
        tags: { journey: 'frozen-control', step: 'frozen-control', path_class: 'normal' },
    });
    check(control, {
        // 정상경로는 account-service ACTIVE 검사(AccountService.java:66-71)에서 400 거절 — 감별 근거.
        'control arm rejects frozen/closed (400)': (r) => r.status === 400,
    });
}
