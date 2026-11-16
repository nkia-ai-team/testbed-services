// ============================================================
// 민원 포털 상주 부하(설계 docs/spec-testbed-vm-3tier.md §5, §2-3 여정 비중)
// ============================================================
// 진입은 웹 앞단 하나(ENTRY_URL = http://<웹>/<앱 경로>). 바깥 사용자처럼 라우터 상향으로 들어온다.
// 여정 비중(§2-3): 민원 신청 8 / 접수번호 조회 30 / 내 민원 목록 25 / 공개 민원 검색 15 /
// 담당자 처리 10 / 통계 화면 7 / 로그인 5. 쓰기는 신청과 처리 18%.
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

// VU 마다 시민 하나, 담당자 하나의 세션을 따로 둔다(쿠키 단지 둘).
const citizenJar = new http.CookieJar();
const staffJar = new http.CookieJar();
const citizenId = randInt(1, CITIZENS);
const staffIndex = randInt(1, STAFF);
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
}

function lookup() {
    // 보관 배치가 옮기지 않은 최근 쪽(시드의 뒤 3/4)에서 고른다. 옮겨진 번호의 404 는 정상 결과다.
    const receipt = `CP${pad(randInt(SEED_PETITIONS / 4, SEED_PETITIONS), 8)}`;
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
    // 열린 민원 하나를 처리한다. 접수 상태면 25% 는 처리 중으로, 나머지는 바로 답변 완료(신청 8% 와 처리 10% 가 균형).
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
    citizenLoggedIn = login(citizenJar, `citizen${pad(citizenId, 5)}`, 'login');
}

// 누적 비중표(합 100).
const JOURNEYS = [
    [8, submit],
    [38, lookup],
    [63, myList],
    [78, publicSearch],
    [88, staffProcess],
    [95, stats],
    [100, relogin],
];

export default function () {
    const roll = rand() * 100;
    for (const [limit, journey] of JOURNEYS) {
        if (roll < limit) {
            journey();
            return;
        }
    }
}
