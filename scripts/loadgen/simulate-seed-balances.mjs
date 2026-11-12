// Runs the real commerce and core-banking baseline k6 scripts the way k6 runs them and reports
// what their transfers do to the seven seed accounts (ACC-1001~1007) and to the transfer request
// shape. Used by scripts/test-loadgen-transfer-pairs.sh.
//
//   node simulate-seed-balances.mjs <commerce script.js> <core-banking script.js> '<json config>'
//
// config: hours, commerce {rate, vus}, banking {rate, vus}, fault {hour, from, to, kind}
//   (kind: refused = status 0 connection refused, or an HTTP status such as 503, 502),
//   gateway: true models the commerce gateway (see below), "expired" also refuses every token,
//   "login-down" answers every login 500, k6json: path for commerce k6 samples,
//   commerceEnv: extra __ENV for the commerce script only (e.g. GATEWAY_URL), commerceEntry: the
//   exported function an iteration of the commerce script runs (default "default").
//
// The driver is iteration level, like constant-arrival-rate: each simulated second gives `rate`
// iterations of the script's default() to the VUs in turn (k6 hands iterations to VUs through a
// FIFO). Each simulated hour loads fresh module instances, as the hourly k6 restart does, so a
// return a VU still holds at the end of an hour is lost. k6 modules are fakes: transfer POSTs
// move money in an in-memory bank (the commerce journey calls transfer directly, so a short
// balance answers 200 FAILED; the banking journey goes through api and account, so it answers
// 400), every other request answers 200 with an empty body, and Date.now and
// exec.scenario.startTime follow the simulated clock. Prints one JSON line.
//
// gateway: true (cart write auth, v4 E5c) adds the commerce gateway's AuthGuard: login answers a
// token, a write (POST, PUT, DELETE, PATCH) to the gateway other than login and register without an
// Authorization header answers 401 (with gateway: "expired", a write with a token too: an expired
// token; with "login-down", every login answers 500), and check() runs the script's predicates.
// A token names the user it was handed to; `tokenOwnerMismatch` counts cart writes whose token
// belongs to another user than the cart path's. The output then has
// `requests` (commerce counts by journey, step, method, status and whether a token was sent),
// `perMinute` (commerce counts by step and minute of the run), `cartUsers` (user ids of cart writes
// in order: the baseline's cart journey and the surge-cart journey of the scenario load scripts
// surge.js and capacity-probe.js, v4 E5d) and `checks` (pass and fail by check name), and with
// cartWrites: true also `cartWrites` (method, path and body of those writes in order). With k6json
// the commerce iterations, http_reqs and checks are also written as k6 --out json lines, for
// loadgen_monitor.py.
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const [commercePath, bankingPath, configArg] = process.argv.slice(2);
const config = JSON.parse(configArg || '{}');
const HOURS = config.hours ?? 24;
const LOAD = {
    commerce: { rate: 5, vus: 20, ...(config.commerce || {}) },
    banking: { rate: 3, vus: 10, ...(config.banking || {}) },
};
const FAULT = config.fault || null;
const GATEWAY = config.gateway === true || config.gateway === 'expired' || config.gateway === 'login-down';
const TOKENS_EXPIRED = config.gateway === 'expired';
const LOGIN_DOWN = config.gateway === 'login-down';
const COMMERCE_ENV = config.commerceEnv || {};
const COMMERCE_ENTRY = config.commerceEntry || 'default';
const SEED = {
    'ACC-1001': 5000000, 'ACC-1002': 3200000, 'ACC-1003': 1500000, 'ACC-1004': 800000,
    'ACC-1005': 12000000, 'ACC-1006': 450000, 'ACC-1007': 2750000,
};
const SEVEN = Object.keys(SEED);
const START = Date.UTC(2026, 10, 10, 0, 0, 0);

// ---- bank model ----
const balance = new Map(Object.entries(SEED));
let simNow = START;
let current = 'commerce';
const stats = { completed: 0, failed: 0, rejected400: 0, faulted: 0, scriptErrors: 0 };
// transfers[kind][hour][minute]
const transfers = { commerce: [], banking: [] };
function account(id) {
    if (!balance.has(id)) {
        balance.set(id, 5000000); // pool accounts: all well above any transfer
    }
    return balance.get(id);
}
function inFault() {
    if (!FAULT) {
        return false;
    }
    const hour = Math.floor((simNow - START) / 3600000);
    const minute = Math.floor((simNow % 3600000) / 60000);
    return hour === FAULT.hour && minute >= FAULT.from && minute < FAULT.to;
}
function transfer(body, viaApi) {
    const hour = Math.floor((simNow - START) / 3600000);
    const minute = Math.floor((simNow % 3600000) / 60000);
    transfers[current][hour][minute] += 1;
    if (inFault()) {
        stats.faulted += 1;
        if (FAULT.kind === 'refused') {
            return { status: 0, error: 'dial tcp 192.168.122.77:30282: connect: connection refused', body: '' };
        }
        return { status: Number(FAULT.kind), body: '{}' };
    }
    const { fromAccount, toAccount, amount } = JSON.parse(body);
    if (account(fromAccount) < amount) {
        if (viaApi) {
            stats.rejected400 += 1;
            return { status: 400, body: '{"message":"Insufficient balance"}' };
        }
        stats.failed += 1;
        return { status: 200, body: '{"status":"FAILED"}' };
    }
    balance.set(fromAccount, account(fromAccount) - amount);
    balance.set(toAccount, account(toAccount) + amount);
    stats.completed += 1;
    return { status: 200, body: '{"status":"COMPLETED"}' };
}
function response(r) {
    return {
        status: r.status,
        error: r.error || '',
        body: r.body,
        json(selector) {
            const value = JSON.parse(r.body);
            return selector ? value[selector] : value;
        },
    };
}

// ---- commerce gateway model (gateway: true) ----
const COMMERCE_GATEWAY = 'http://testbed-gateway:8089';
const requests = {};
const perMinute = {};
const cartUsers = [];
const cartWrites = [];
const checks = {};
const k6lines = [];
const issued = new Set();
let tokenOwnerMismatch = 0;
function stampIso() {
    return new Date(simNow).toISOString();
}
function k6point(metric, value, tags) {
    if (config.k6json && current === 'commerce') {
        k6lines.push(JSON.stringify({ type: 'Point', metric, data: { time: stampIso(), value, tags } }));
    }
}
function gateway(method, url, body, params) {
    if (!GATEWAY || !url.startsWith(COMMERCE_GATEWAY)) {
        return null;
    }
    const path = url.slice(COMMERCE_GATEWAY.length).split('?')[0];
    const headers = (params && params.headers) || {};
    const tags = (params && params.tags) || {};
    // Only a token this gateway's login handed out counts ("Bearer undefined" does not).
    const token = typeof headers.Authorization === 'string' && headers.Authorization.startsWith('Bearer ')
        && issued.has(headers.Authorization.slice('Bearer '.length));
    let r;
    if (method !== 'GET' && path !== '/api/users/login' && path !== '/api/users/register' && !token) {
        r = { status: 401, body: '{"message":"Missing Authorization token"}' };
    } else if (method !== 'GET' && path !== '/api/users/login' && path !== '/api/users/register' && TOKENS_EXPIRED) {
        r = { status: 401, body: '{"message":"Invalid or expired token"}' };
    } else if (path === '/api/users/login' && LOGIN_DOWN) {
        r = { status: 500, body: '{"message":"Internal Server Error"}' };
    } else if (path === '/api/users/login') {
        const { email } = JSON.parse(body);
        issued.add(`tok-${email}`);
        r = { status: 200, body: JSON.stringify({ token: `tok-${email}`, expiresAt: new Date(simNow + 24 * 3600000).toISOString() }) };
    } else {
        r = { status: 200, body: method === 'GET' ? '[]' : '{}' };
    }
    const key = `${tags.journey || '-'} ${tags.step || '-'} ${method} ${r.status} ${token ? 'token' : 'none'}`;
    requests[key] = (requests[key] || 0) + 1;
    const minute = Math.floor((simNow - START) / 60000);
    const step = `${tags.journey || '-'}/${tags.step || '-'}`;
    perMinute[step] = perMinute[step] || {};
    perMinute[step][minute] = (perMinute[step][minute] || 0) + 1;
    if (/(^|-)cart$/.test(tags.journey || '') && (tags.step === 'add' || tags.step === 'remove')) {
        cartUsers.push(Number(path.split('/')[3]));
        cartWrites.push(`${method} ${path} ${body || ''}`);
    }
    if (token && path.startsWith('/api/carts/')) {
        const owner = `tok-user${String(Number(path.split('/')[3])).padStart(2, '0')}@example.com`;
        if (headers.Authorization.slice('Bearer '.length) !== owner) {
            tokenOwnerMismatch += 1;
        }
    }
    k6point('http_reqs', 1, { journey: tags.journey || '', step: tags.step || '', method, status: String(r.status) });
    return response(r);
}

// ---- fake k6 modules ----
globalThis.__k6sim = {
    post(url, body, params) {
        if (url.endsWith('/api/transfers')) {
            return response(transfer(body, !url.includes(':8082') && !url.includes('30282')));
        }
        return gateway('POST', url, body, params) || response({ status: 200, body: '{}' });
    },
    other(method, url, body, params) {
        return gateway(method, url, body, params) || response({ status: 200, body: '[]' });
    },
    check(res, predicates) {
        if (!GATEWAY) {
            return true;
        }
        let all = true;
        for (const [name, predicate] of Object.entries(predicates)) {
            const ok = Boolean(predicate(res));
            if (current === 'commerce') {
                checks[name] = checks[name] || { pass: 0, fail: 0 };
                checks[name][ok ? 'pass' : 'fail'] += 1;
                k6point('checks', ok ? 1 : 0, { check: name, group: '', scenario: 'baseline' });
            }
            all = all && ok;
        }
        return all;
    },
    scenario: { startTime: START },
};
const dir = mkdtempSync(join(tmpdir(), 'k6sim-'));
writeFileSync(join(dir, 'http.mjs'), 'const sim = () => globalThis.__k6sim;\n'
    + 'export default { post: (u, b, p) => sim().post(u, b, p), get: (u, p) => sim().other(\'GET\', u, null, p),'
    + ' del: (u, b, p) => sim().other(\'DELETE\', u, b, p), put: (u, b, p) => sim().other(\'PUT\', u, b, p),'
    + ' patch: (u, b, p) => sim().other(\'PATCH\', u, b, p), request: () => sim().other(\'GET\', \'\'),'
    + ' batch: (r) => r.map(() => sim().other(\'GET\', \'\')),'
    + ' setResponseCallback: () => {}, expectedStatuses: () => null };\n');
writeFileSync(join(dir, 'k6.mjs'), 'export function check(r, p) { return globalThis.__k6sim.check(r, p); }\nexport function sleep() {}\nexport function group(n, f) { return f(); }\n');
writeFileSync(join(dir, 'execution.mjs'), 'export default { get scenario() { return globalThis.__k6sim.scenario; }, vu: {} };\n');

function instrument(path, name) {
    let source = readFileSync(path, 'utf8')
        .replace("from 'k6/http'", "from './http.mjs'")
        .replace("from 'k6/execution'", "from './execution.mjs'")
        .replace("from 'k6'", "from './k6.mjs'");
    source += '\nexport function __pending() { return typeof pendingReturn === "undefined" ? null : pendingReturn; }\n';
    const out = join(dir, `${name}.mjs`);
    writeFileSync(out, source);
    return out;
}
const modules = { commerce: instrument(commercePath, 'commerce'), banking: instrument(bankingPath, 'banking') };

globalThis.__ENV = {};
const realNow = Date.now;
Date.now = () => simNow;

const lostAtRestart = { commerce: 0, banking: 0 };
const hourlyBalances = [];
for (let hour = 0; hour < HOURS; hour += 1) {
    const hourStart = START + hour * 3600000;
    globalThis.__k6sim.scenario = { startTime: hourStart };
    const vus = {};
    for (const kind of ['commerce', 'banking']) {
        transfers[kind].push(new Array(60).fill(0));
        vus[kind] = [];
        // A different seed every hour so hours are independent samples (k6 itself reuses one seed,
        // but its hourly rate changes with the diurnal curve).
        globalThis.__ENV = { LOADGEN_SEED: String(42 + hour), ...(kind === 'commerce' ? COMMERCE_ENV : {}) };
        for (let v = 1; v <= LOAD[kind].vus; v += 1) {
            globalThis.__VU = v;
            vus[kind].push(await import(pathToFileURL(modules[kind]).href + `?h=${hour}&v=${v}`));
        }
    }
    const next = { commerce: 0, banking: 0 };
    for (let second = 0; second < 3600; second += 1) {
        for (const kind of ['commerce', 'banking']) {
            const rate = LOAD[kind].rate;
            for (let i = 0; i < rate; i += 1) {
                simNow = hourStart + second * 1000 + Math.floor((i * 1000) / rate);
                current = kind;
                const vu = vus[kind][next[kind]];
                next[kind] = (next[kind] + 1) % vus[kind].length;
                k6point('iterations', 1, { scenario: 'baseline' });
                try {
                    vu[kind === 'commerce' ? COMMERCE_ENTRY : 'default']();
                } catch (e) {
                    stats.scriptErrors += 1;
                    if (config.debugErrors) {
                        console.error(e);
                    }
                }
            }
        }
    }
    for (const kind of ['commerce', 'banking']) {
        lostAtRestart[kind] += vus[kind].filter((m) => m.__pending() !== null).length;
    }
    hourlyBalances.push(Object.fromEntries(SEVEN.map((id) => [id, balance.get(id)])));
}
Date.now = realNow;

// ---- shape ----
function meanStd(values) {
    const mean = values.reduce((a, b) => a + b, 0) / values.length;
    const sd = Math.sqrt(values.reduce((a, b) => a + (b - mean) ** 2, 0) / values.length);
    return { mean, sd };
}
const shape = {};
for (const kind of ['commerce', 'banking']) {
    const hours = transfers[kind].filter((_, h) => !FAULT || h !== FAULT.hour);
    // Minute-of-hour profile averaged over hours, and each hour on its own.
    const profile = Array.from({ length: 60 }, (_, m) => hours.reduce((a, row) => a + row[m], 0) / hours.length);
    const head = meanStd(profile.slice(0, 40));
    const tailMax = Math.max(...profile.slice(40));
    let hoursOver = 0;
    for (const row of hours) {
        const s = meanStd(row.slice(0, 40));
        if (Math.max(...row.slice(40)) > s.mean + 3 * Math.max(s.sd, 1)) {
            hoursOver += 1;
        }
    }
    const perHour = hours.map((row) => row.reduce((a, b) => a + b, 0));
    const pct = (values, q) => {
        const sorted = [...values].sort((a, b) => a - b);
        return sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))];
    };
    const headMinutes = hours.flatMap((row) => row.slice(0, 40));
    const tailMinutes = hours.flatMap((row) => row.slice(40));
    const expectedShare = kind === 'commerce' ? 0.02 : 0.10;
    shape[kind] = {
        transfersPerHour: meanStd(perHour).mean,
        expectedPerHour: LOAD[kind].rate * 3600 * expectedShare,
        profileHeadMean: head.mean,
        profileHeadSd: head.sd,
        profileTailMax: tailMax,
        profileTail: profile.slice(40).map((x) => Math.round(x * 100) / 100),
        hoursWithTailMinuteOver3Sd: hoursOver,
        // Per-minute counts of single hours: the last 20 minutes against the first 40.
        headMinuteP99: pct(headMinutes, 0.99),
        tailMinuteP99: pct(tailMinutes, 0.99),
        headMinuteMax: Math.max(...headMinutes),
        tailMinuteMax: Math.max(...tailMinutes),
        hours: hours.length,
    };
    if (FAULT) {
        const row = transfers[kind][FAULT.hour];
        shape[kind].faultHourPerMinute = row.slice(FAULT.from, FAULT.to);
        shape[kind].faultHourMaxPerMinute = Math.max(...row.slice(FAULT.from, FAULT.to));
    }
}

const drift = Math.max(...SEVEN.map((id) => Math.abs(balance.get(id) - SEED[id])));
const minBalance = Math.min(...hourlyBalances.flatMap((b) => Object.values(b)));
if (config.k6json) {
    writeFileSync(config.k6json, k6lines.length ? `${k6lines.join('\n')}\n` : '');
}
console.log(JSON.stringify({
    hours: HOURS, load: LOAD, fault: FAULT, stats, lostAtRestart, shape,
    ...(GATEWAY ? { requests, perMinute, cartUsers, checks, tokenOwnerMismatch, ...(config.cartWrites ? { cartWrites } : {}) } : {}),
    maxDriftFromSeedAtEnd: drift,
    minSevenBalanceAtHourEnds: minBalance,
    endBalances: Object.fromEntries(SEVEN.map((id) => [id, balance.get(id)])),
}));
