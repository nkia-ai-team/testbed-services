// Runs the real commerce and core-banking baseline k6 scripts the way k6 runs them and reports
// what their transfers do to the seven seed accounts (ACC-1001~1007) and to the transfer request
// shape. Used by scripts/test-loadgen-transfer-pairs.sh.
//
//   node simulate-seed-balances.mjs <commerce script.js> <core-banking script.js> '<json config>'
//
// config: hours, commerce {rate, vus}, banking {rate, vus}, fault {hour, from, to, kind}
//   (kind: refused = status 0 connection refused, or an HTTP status such as 503, 502).
//
// The driver is iteration level, like constant-arrival-rate: each simulated second gives `rate`
// iterations of the script's default() to the VUs in turn (k6 hands iterations to VUs through a
// FIFO). Each simulated hour loads fresh module instances, as the hourly k6 restart does, so a
// return a VU still holds at the end of an hour is lost. k6 modules are fakes: transfer POSTs
// move money in an in-memory bank (the commerce journey calls transfer directly, so a short
// balance answers 200 FAILED; the banking journey goes through api and account, so it answers
// 400), every other request answers 200 with an empty body, and Date.now and
// exec.scenario.startTime follow the simulated clock. Prints one JSON line.
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

// ---- fake k6 modules ----
globalThis.__k6sim = {
    post(url, body) {
        if (url.endsWith('/api/transfers')) {
            return response(transfer(body, !url.includes(':8082') && !url.includes('30282')));
        }
        return response({ status: 200, body: '{}' });
    },
    other() { return response({ status: 200, body: '[]' }); },
    scenario: { startTime: START },
};
const dir = mkdtempSync(join(tmpdir(), 'k6sim-'));
writeFileSync(join(dir, 'http.mjs'), 'const other = () => globalThis.__k6sim.other();\n'
    + 'export default { post: (u, b) => globalThis.__k6sim.post(u, b), get: other, del: other, put: other, patch: other, request: other, batch: (r) => r.map(other) };\n');
writeFileSync(join(dir, 'k6.mjs'), 'export function check() { return true; }\nexport function sleep() {}\nexport function group(n, f) { return f(); }\n');
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
        globalThis.__ENV = { LOADGEN_SEED: String(42 + hour) };
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
                try {
                    vu.default();
                } catch (e) {
                    stats.scriptErrors += 1;
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
console.log(JSON.stringify({
    hours: HOURS, load: LOAD, fault: FAULT, stats, lostAtRestart, shape,
    maxDriftFromSeedAtEnd: drift,
    minSevenBalanceAtHourEnds: minBalance,
    endBalances: Object.fromEntries(SEVEN.map((id) => [id, balance.get(id)])),
}));
