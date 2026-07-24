// ============================================================
// core-banking slowquery 부하 (시나리오 전용, F20-P) — testbed-services docs/scenario-redesign-wip/design-F20-slowquery-sheet.md
// ============================================================
// baseline loadgen(core-banking/loadgen/script.js, 불가침 — R1)·surge.js(폭주)와
// 별개로, F20-P가 켰다 끄는 슬로우쿼리 부하다. transfer-service의 무거운 통계
// 조회를 반복 호출한다:
//   - GET /api/transfers/stats/daily?days=90 → TransferRepository.dailyStatsSince
//     (:33-36) native "trunc(created_at) ... group by trunc(created_at)" — 인덱스
//     idx_transfers_created(init.sql:46)는 존재하나 trunc() 함수 래핑으로
//     무력화되어 풀스캔이 된다.
// 이 풀스캔이 transfer Hikari(max=15) 커넥션을 스캔 시간만큼 점유해 동시 이체
// execute()의 FOR UPDATE와 경합하는 것이 F20-P의 정체성(자체 커넥션풀 점유,
// sheet §1 F20-P) — F20-R(공유 PG CPU 오염)과 달리 banking 체인에 국한된다.
//
// ramping-arrival-rate + mulberry32 시드 재현성(R4)과 계좌 시드 계약은 surge.js와
// 동일 — 창작하지 않는다. entry_url(30082)의 domain_profile은
// business_step="transfer"/read_step="get"으로 고정돼 있어(profiles.json), 이
// 스크립트도 그 두 step 이름을 그대로 태깅한다:
//   - 슬로우쿼리 조회는 step:'get'으로 태깅 → loadgen.read_step_status_rate가
//     이 무거운 조회의 상태를 집계한다(관측 계약, sheet §4-A).
//   - 대표 이체 여정을 낮은 비중(20%)으로 섞어 step:'transfer' 샘플을 계속
//     흘려보낸다 — 이게 없으면 monitor의 entry_status가 채워지지 않아
//     http.entry_health/abort(entry-unreachable)가 무너진다(러너 monitor는
//     tags.step===business_step 요청만 entry_status로 집계, team-lead 지적
//     반영). 이 대표 이체 트래픽은 F20-P가 노리는 "커넥션 경합"도 실제로
//     재현한다(sheet의 이체 경합 악화 경로).
import http from 'k6/http';
import { check } from 'k6';

const GATEWAY_URL = __ENV.GATEWAY_URL || 'http://192.168.122.77:30082';
const TARGET_RPS = Number(__ENV.TARGET_RPS || 20);
const RAMP_UP = __ENV.RAMP_UP || '2m';
const HOLD = __ENV.HOLD || '8m';
const RAMP_DOWN = __ENV.RAMP_DOWN || '1m';
const SURGE_SEED = Number(__ENV.SURGE_SEED || 4242);
// 슬로우쿼리는 요청당 지연이 surge.js의 일반 여정보다 훨씬 크다(trunc() 풀스캔).
// banking surge.js(20rps/30/250) 대비 VU 상한을 넉넉히 잡는다.
const PRE_ALLOCATED_VUS = Number(__ENV.PRE_ALLOCATED_VUS || 50);
const MAX_VUS = Number(__ENV.MAX_VUS || 350);

export const options = {
    scenarios: {
        slowquery: {
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
    // 슬로우쿼리 중 지연/타임아웃·502는 이 시나리오가 만들려는 증상 그 자체 —
    // 실패로 run을 중단하지 않고 check() 통계로만 관측한다.
    thresholds: {},
};

// 재현성: surge.js(script.js)와 동일한 mulberry32 + VU 분기 규약 (R4).
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

// 시드 데이터 계약 — surge.js와 동일. 활성(ACTIVE) 개인 계좌만 사용
// (ACC-9001 FROZEN / ACC-9002 CLOSED 제외). §8.1-1 전처리(잔액 1억 상향)가
// 적용된 환경 전제.
const ACTIVE_ACCOUNTS = ['ACC-1001', 'ACC-1002', 'ACC-1003', 'ACC-1004', 'ACC-1005', 'ACC-1006', 'ACC-1007'];

// 슬로우쿼리 — 일별 통계 (native trunc(created_at) group by, 인덱스 무력화).
// step은 domain_profile.read_step("get")에 맞춰 태깅한다(러너 monitor 계약).
function dailyStatsJourney() {
    const res = http.get(`${GATEWAY_URL}/api/transfers/stats/daily?days=90`, {
        tags: { journey: 'slowquery-stats', step: 'get' },
    });
    check(res, { 'transfer stats/daily not 5xx': (r) => r.status < 500 });
}

// 대표 이체 여정 — surge.js의 smallTransferJourney와 동일 계약. 낮은 비중으로
// 섞어 step:'transfer' 샘플이 끊기지 않게 하고, 슬로우쿼리와 커넥션을 실제로
// 경합시킨다(F20-P 정체성).
function transferRepresentative() {
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
        tags: { journey: 'slowquery-transfer', step: 'transfer' },
    });
    check(res, { 'transfer not 5xx': (r) => r.status < 500 });
}

// 여정 가중 — 슬로우쿼리(통계)가 80%로 주 목적, 대표 이체는 20%로
// entry_status 배선 + 커넥션 경합 재현용 최소 비중만 유지.
export default function () {
    const r = rand();
    if (r < 0.8) {
        dailyStatsJourney();
    } else {
        transferRepresentative();
    }
}
