// ============================================================
// 민원 포털 상주 부하(설계 docs/spec-testbed-vm-3tier.md §5, §2-3 여정 비중)
// ============================================================
// 진입은 웹 앞단 하나(ENTRY_URL = http://<웹>/<앱 경로>). 바깥 사용자처럼 라우터 상향으로 들어온다.
// 여정 비중(§2-3): 민원 신청 8 / 접수번호 조회 30 / 내 민원 목록 25 / 공개 민원 검색 15 /
// 담당자 처리 11 / 통계 화면 6 / 로그인 5. 쓰기 여정은 신청과 처리 19%. 담당자 처리 11 은 처리 능력을 수요
// (신청 8 x 민원당 처리 1.25 번 = 10)의 약 110% 로 둔 값이다(검토 세션 Q23 답 (가)). 그 1 은 통계 화면에서 옮겼다.
// 여정마다 step 태그를 단다. 모니터는 submit(사업 단계)과 lookup(조회 단계)을 따로 센다.
// 모든 요청에 check() 를 달아 기대 상태를 적는다(모니터의 check 필드가 단계 밖 비정상도 센다).
// http_5xx, http_no_response 카운터는 규모 축 판정(scale_verdict.py)이 --summary-export 에서 읽는다.
import http from 'k6/http';
import { check } from 'k6';
import { Counter } from 'k6/metrics';

const ENTRY_URL = __ENV.ENTRY_URL || 'http://127.0.0.1/petition';
const TARGET_RPS = Number(__ENV.TARGET_RPS || 5);
const DURATION = __ENV.DURATION || '1h';
const PRE_ALLOCATED_VUS = Number(__ENV.PRE_ALLOCATED_VUS || 40);
const MAX_VUS = Number(__ENV.MAX_VUS || 150);
const BASE_SEED = Number(__ENV.LOADGEN_SEED || 42);

export const options = {
    scenarios: {
        baseline: {
            executor: 'constant-arrival-rate',
            rate: TARGET_RPS,
            timeUnit: '1s',
            duration: DURATION,
            preAllocatedVUs: PRE_ALLOCATED_VUS,
            maxVUs: MAX_VUS,
        },
    },
    // 배경 부하라 개별 요청 실패로 실행을 실패시키지 않는다.
    thresholds: {},
    // 접근 로그에 부하 도구 이름(k6 기본 User-Agent)이 남지 않게 흔한 브라우저 값을 쓴다(리뷰 지적 8).
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
    summaryTrendStats: ['avg', 'p(50)', 'p(95)', 'p(99)', 'max'],
};

const serverErrors = new Counter('http_5xx');
const noResponse = new Counter('http_no_response');

// 시드 값과 맞춘 상수(db/seed.sql).
const SEED_PETITIONS = 2000000;
const CITIZENS = 49700;
const STAFF = 300;
const DEPARTMENTS = 300;
const SEED_PASSWORD = 'Passw0rd!';
const CATEGORIES = ['road', 'traffic', 'environment', 'building', 'welfare', 'tax', 'sanitation', 'etc'];
const KEYWORDS = ['보도블록', '가로등', '주정차', '소음', '쓰레기', '악취', '포트홀', '정류장', '공원', '하수구',
    '현수막', '누수', '횡단보도', '자전거', '체납', '방역', '노점', '먼지', '주차장', '가지치기'];
const TIMEOUT = '10s';

// 접수번호 조회 번호 고르기(설계 docs/spec-testbed-vm-3tier.md §5, 검토 세션 Q22 답 (가)).
// 원표는 가장 최근 번호부터 보존 건수 상한(200만)만큼을 담는다(보관 배치가 가장 오래된 것부터 옮긴다).
// 그래서 가장 최근 번호에서 뒤로 거리를 재어, 지금 원표에 있는 범위 안에서 최근 쪽에 무게를 두고 고른다.
// 가장 최근 번호는 실행(1시간)마다 setup() 이 공개 민원 검색 한 번으로 읽고, 가상 사용자가 자기 신청 응답의
// 번호로 올린다. 없는 번호 조회(오타, 오래된 번호)는 첫날부터 고정 1% 다(가정, 고정. 바꾸지 않는다).
const LOOKUP_NOT_FOUND_RATE = 0.01;
const LOOKUP_RECENT_MEAN = 200000;     // 가장 최근 번호에서 거리의 평균(기본 부하의 하루 신청 약 12만의 1.6일치)
const LOOKUP_LIVE_SPAN = 1500000;      // 거리 상한. 원표 200만보다 작게 두어 번호 틈과 한 시간 묵은 최근 번호를 덮는다
const LOOKUP_MISSING_BASE = 90000000;  // 이 테스트베드가 닿지 않는 번호대(하루 약 12만이면 약 2년 뒤)

// 재현성: VU 마다 갈라진 고정 시드(commerce/loadgen/script.js 와 같은 mulberry32).
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
function randInt(min, max) { return Math.floor(rand() * (max - min + 1)) + min; }
function pick(items) { return items[randInt(0, items.length - 1)]; }
function pad(n, width) { return String(n).padStart(width, '0'); }
function day(offsetDays) {
    const d = new Date(Date.now() - offsetDays * 86400000);
    return d.toISOString().slice(0, 10);
}

// 이 가상 사용자가 본 가장 최근 접수번호(자기 신청 응답). setup() 값과 큰 쪽을 쓴다.
let ownNewest = 0;

function receiptNumber(text) {
    const match = /^CP([0-9]{8})$/.exec(String(text || ''));
    return match ? Number(match[1]) : 0;
}

// 위 상수의 규칙 하나. 거리는 평균 LOOKUP_RECENT_MEAN 의 지수 분포이고 상한을 넘으면 다시 뽑는다.
function pickReceiptNumber(newest) {
    if (rand() < LOOKUP_NOT_FOUND_RATE) return LOOKUP_MISSING_BASE + randInt(0, 9999999);
    let distance = LOOKUP_LIVE_SPAN;
    while (distance >= LOOKUP_LIVE_SPAN) distance = Math.floor(-Math.log(1 - rand()) * LOOKUP_RECENT_MEAN);
    return Math.max(1, newest - distance);
}

// 실행 시작에 한 번: 오늘과 어제의 공개 민원을 "민원"(부하기 신청 제목의 낱말)으로 찾아 가장 최근 번호를 읽는다.
// 결과가 없으면(부하기 신청 전, 시드만 있음) 시드의 가장 최근 번호다.
export function setup() {
    let newest = SEED_PETITIONS;
    const res = http.get(`${ENTRY_URL}/api/public/petitions?keyword=${encodeURIComponent('민원')}&from=${day(1)}&to=${day(0)}&page=0`,
        { tags: { step: 'setup' }, timeout: TIMEOUT });
    if (res.status === 200) {
        for (const row of res.json()) newest = Math.max(newest, receiptNumber(row.receipt_no));
    }
    return { newest };
}

// VU 마다 시민 하나, 담당자 하나의 세션을 따로 둔다(쿠키 단지 둘).
const citizenJar = new http.CookieJar();
const staffJar = new http.CookieJar();
// 로그인 여정(5%)마다 시민 또는 담당자 가운데 하나를 다른 사람으로 바꾼다(설계 §5). 가상 사용자 수(40~150)만큼의
// 사람만 쓰면 신청이 그 몇십 명에게 몰리고 그 몇십 부서만 처리되어, 나머지 부서의 열린 건이 끝없이 쌓인다.
let citizenId = randInt(1, CITIZENS);
let staffIndex = randInt(1, STAFF);
let citizenLoggedIn = false;
let staffLoggedIn = false;

function record(res, step, expected) {
    if (res.status >= 500) serverErrors.add(1, { step });
    if (res.status === 0) noResponse.add(1, { step });
    check(res, { [`${step} status`]: (r) => expected.includes(r.status) });
    return res;
}

function jsonParams(step, jar) {
    return { headers: { 'Content-Type': 'application/json' }, tags: { step }, jar, timeout: TIMEOUT };
}

function login(jar, loginId, step) {
    const res = http.post(`${ENTRY_URL}/api/login`, JSON.stringify({ loginId, password: SEED_PASSWORD }),
        jsonParams(step, jar));
    record(res, step, [200]);
    return res.status === 200;
}

function ensureCitizen() {
    if (!citizenLoggedIn) citizenLoggedIn = login(citizenJar, `citizen${pad(citizenId, 5)}`, 'login');
    return citizenLoggedIn;
}

function ensureStaff() {
    if (!staffLoggedIn) staffLoggedIn = login(staffJar, `staff${pad(staffIndex, 3)}`, 'login');
    return staffLoggedIn;
}

function submit() {
    if (!ensureCitizen()) return;
    const attachments = rand() < 0.3 ? [{ fileName: `photo_${randInt(1, 999999)}.jpg`, sizeBytes: randInt(50000, 900000) }] : [];
    const keyword = pick(KEYWORDS);
    const res = http.post(`${ENTRY_URL}/api/petitions`, JSON.stringify({
        deptId: randInt(1, DEPARTMENTS),
        category: pick(CATEGORIES),
        title: `${keyword} 관련 민원`,
        body: `${keyword} 문제로 불편합니다. 현장 확인 후 조치하여 주시기 바랍니다.`,
        isPublic: rand() < 0.6,
        attachments,
    }), jsonParams('submit', citizenJar));
    if (res.status === 401) citizenLoggedIn = false;
    record(res, 'submit', [200]);
    if (res.status === 200) ownNewest = Math.max(ownNewest, receiptNumber(res.json('receipt_no')));
}

function lookup(data) {
    // 지금 원표에 있는 범위에서 최근 쪽으로, 1% 는 없는 번호(404 는 정상 결과다).
    const newest = Math.max((data && data.newest) || SEED_PETITIONS, ownNewest);
    const receipt = `CP${pad(pickReceiptNumber(newest), 8)}`;
    const res = http.get(`${ENTRY_URL}/api/petitions/${receipt}`, { tags: { step: 'lookup' }, timeout: TIMEOUT });
    record(res, 'lookup', [200, 404]);
}

function myList() {
    if (!ensureCitizen()) return;
    const span = randInt(30, 90);
    const res = http.get(`${ENTRY_URL}/api/my/petitions?from=${day(span)}&to=${day(0)}&page=0`,
        { tags: { step: 'my-list' }, jar: citizenJar, timeout: TIMEOUT });
    if (res.status === 401) citizenLoggedIn = false;
    record(res, 'my-list', [200]);
}

function publicSearch() {
    const span = randInt(7, 30);
    const res = http.get(
        `${ENTRY_URL}/api/public/petitions?keyword=${encodeURIComponent(pick(KEYWORDS))}&from=${day(span)}&to=${day(0)}&page=0`,
        { tags: { step: 'search' }, timeout: TIMEOUT });
    record(res, 'search', [200]);
}

function staffProcess() {
    if (!ensureStaff()) return;
    const inbox = http.get(`${ENTRY_URL}/api/staff/petitions?size=10`, { tags: { step: 'inbox' }, jar: staffJar, timeout: TIMEOUT });
    if (inbox.status === 401) staffLoggedIn = false;
    record(inbox, 'inbox', [200]);
    if (inbox.status !== 200) return;
    const items = inbox.json();
    if (!items.length) return;
    const target = pick(items);
    // 열린 민원 하나를 처리한다. 접수 상태면 25% 는 처리 중으로, 나머지는 바로 답변 완료. 민원당 처리가 평균 1.25 번
    // 들어 처리 수요는 여정의 10%, 처리 여정 11% 는 그 약 110% 다(Q23 답 (가)). 열린 건이 없으면 함 조회만 하고 끝난다.
    const action = target.status === 'RECEIVED' && rand() < 0.25 ? 'ACCEPT' : 'ANSWER';
    const res = http.post(`${ENTRY_URL}/api/staff/petitions/${target.petition_id}/process`,
        JSON.stringify({ action, note: '현장 확인 후 조치하였습니다.' }), jsonParams('process', staffJar));
    // 다른 담당자가 먼저 처리한 경우(409)는 정상 결과다.
    record(res, 'process', [200, 409]);
}

function stats() {
    const month = day(randInt(0, 60)).slice(0, 7);
    const res = http.get(`${ENTRY_URL}/api/stats/departments?month=${month}`, { tags: { step: 'stats' }, timeout: TIMEOUT });
    record(res, 'stats', [200]);
}

function relogin() {
    // 요청 수는 그대로 로그인 하나다. 반은 다른 시민으로, 반은 다른 담당자(다른 부서)로 다시 로그인한다.
    if (rand() < 0.5) {
        citizenId = randInt(1, CITIZENS);
        citizenLoggedIn = login(citizenJar, `citizen${pad(citizenId, 5)}`, 'login');
    } else {
        staffIndex = randInt(1, STAFF);
        staffLoggedIn = login(staffJar, `staff${pad(staffIndex, 3)}`, 'login');
    }
}

// 누적 비중표(합 100).
const JOURNEYS = [
    [8, submit],
    [38, lookup],
    [63, myList],
    [78, publicSearch],
    [89, staffProcess],
    [95, stats],
    [100, relogin],
];

export default function (data) {
    const roll = rand() * 100;
    for (const [limit, journey] of JOURNEYS) {
        if (roll < limit) {
            journey(data);
            return;
        }
    }
}
