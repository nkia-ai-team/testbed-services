// Runs the real commerce and core-banking baseline k6 scripts against an in-memory bank and
// reports what their transfers do to the seven seed accounts (ACC-1001~1007) over simulated
// hours. Used by scripts/test-loadgen-transfer-pairs.sh.
//
//   node simulate-seed-balances.mjs <commerce script.js> <core-banking script.js> <hours> <fault>
//
// k6 modules are replaced by fakes: http.post moves money in the bank model (the commerce
// journey calls transfer directly, so short balance answers 200 FAILED; the banking journey
// goes through api and account, so short balance answers 400), exec.scenario.startTime and
// Date.now follow the simulated clock. Each simulated hour loads fresh module instances, as the
// hourly k6 restart does, so anything a VU still holds at the end of an hour is lost.
// fault=reversal-5xx makes every transfer answer 503 from minute 20 to 40 of hour 2, so returns
// owed by transfers made earlier in that hour hit the outage.
// The journeys are driven directly (a VU's transfer journey at random times), and in each hour's
// last three minutes a VU still holding a return runs the script's default function, which is
// where the flush lives. Prints one JSON line.
import { mkdtempSync, readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const [commercePath, bankingPath, hoursArg, fault = 'none'] = process.argv.slice(2);
const HOURS = Number(hoursArg || 24);
const SEED = {
    'ACC-1001': 5000000, 'ACC-1002': 3200000, 'ACC-1003': 1500000, 'ACC-1004': 800000,
    'ACC-1005': 12000000, 'ACC-1006': 450000, 'ACC-1007': 2750000,
};
const SEVEN = Object.keys(SEED);

// ---- bank model ----
const balance = new Map(Object.entries(SEED));
let simNow = Date.UTC(2026, 10, 10, 0, 0, 0);
const stats = { completed: 0, failed: 0, rejected400: 0, server5xx: 0 };
function account(id) {
    if (!balance.has(id)) {
        balance.set(id, 5000000); // pool accounts: all well above any transfer
    }
    return balance.get(id);
}
function transfer(body, viaApi) {
    const hourIndex = Math.floor((simNow - Date.UTC(2026, 10, 10)) / 3600000);
    const minute = Math.floor((simNow % 3600000) / 60000);
    if (fault === 'reversal-5xx' && hourIndex === 2 && minute >= 20 && minute < 40) {
        stats.server5xx += 1;
        return { status: 503, body: '{}' };
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
        json(selector) {
            const value = JSON.parse(r.body);
            return selector ? value[selector] : value;
        },
    };
}

// ---- fake k6 modules ----
globalThis.__k6sim = {
    post(url, body) {
        return response(transfer(body, !url.includes(':8082') && !url.includes('30282')));
    },
    get() { return response({ status: 200, body: '[]' }); },
    scenario: { startTime: simNow },
};
const dir = mkdtempSync(join(tmpdir(), 'k6sim-'));
mkdirSync(join(dir, 'k6'), { recursive: true });
writeFileSync(join(dir, 'http.mjs'), 'const other = () => globalThis.__k6sim.get();\nexport default { post: (u, b) => globalThis.__k6sim.post(u, b), get: other, del: other, put: other, patch: other, request: other };\n');
writeFileSync(join(dir, 'k6.mjs'), 'export function check() { return true; }\nexport function sleep() {}\n');
writeFileSync(join(dir, 'execution.mjs'), 'export default { get scenario() { return globalThis.__k6sim.scenario; } };\n');

function instrument(path, name, journey) {
    let source = readFileSync(path, 'utf8')
        .replace("from 'k6/http'", "from './http.mjs'")
        .replace("from 'k6/execution'", "from './execution.mjs'")
        .replace("from 'k6'", "from './k6.mjs'");
    source += `\nexport function __journey() { return ${journey}(); }\n`
        + 'export function __pending() { return typeof pendingReturn === "undefined" ? null : pendingReturn; }\n';
    const out = join(dir, `${name}.mjs`);
    writeFileSync(out, source);
    return out;
}
const commerceModule = instrument(commercePath, 'commerce', 'crossDomainJourney');
const bankingModule = instrument(bankingPath, 'banking', 'smallTransferJourney');

globalThis.__ENV = {};
const realNow = Date.now;
Date.now = () => simNow;

// Deterministic driver randomness (separate from the scripts' own PRNG).
let driverState = 12345;
function driverRand() {
    driverState = (driverState * 1103515245 + 12345) % 2147483648;
    return driverState / 2147483648;
}

const VUS = { commerce: 20, banking: 10 };
// Transfers per hour per VU, from 66 (11-10): commerce about 10,100 a day, banking about 32,000.
const PER_HOUR = { commerce: 21, banking: 133 };
const hourlyBalances = [];
let lostAtRestart = 0;
for (let hour = 0; hour < HOURS; hour += 1) {
    const hourStart = Date.UTC(2026, 10, 10) + hour * 3600000;
    globalThis.__k6sim.scenario = { startTime: hourStart };
    const vus = [];
    for (const kind of ['commerce', 'banking']) {
        for (let v = 1; v <= VUS[kind]; v += 1) {
            globalThis.__VU = v;
            const url = pathToFileURL(kind === 'commerce' ? commerceModule : bankingModule).href + `?h=${hour}&v=${v}`;
            vus.push({ kind, mod: await import(url) });
        }
    }
    // Journeys at random moments of the hour, in time order.
    const events = [];
    for (const vu of vus) {
        for (let i = 0; i < PER_HOUR[vu.kind]; i += 1) {
            events.push({ at: hourStart + Math.floor(driverRand() * 3600000), vu });
        }
    }
    events.sort((a, b) => a.at - b.at);
    for (const event of events) {
        simNow = event.at;
        if (simNow - hourStart >= 57 * 60000 && event.vu.mod.__pending() !== null) {
            event.vu.mod.default();
        } else if (simNow - hourStart < 57 * 60000) {
            event.vu.mod.__journey();
        }
    }
    // Last three minutes: every VU gets iterations; one holding a return flushes it.
    for (let tick = 0; tick < 3; tick += 1) {
        simNow = hourStart + (57 + tick) * 60000 + 30000;
        for (const vu of vus) {
            if (vu.mod.__pending() !== null) {
                vu.mod.default();
            }
        }
    }
    lostAtRestart += vus.filter((vu) => vu.mod.__pending() !== null).length;
    hourlyBalances.push(Object.fromEntries(SEVEN.map((id) => [id, balance.get(id)])));
}
Date.now = realNow;

const drift = Math.max(...SEVEN.map((id) => Math.abs(balance.get(id) - SEED[id])));
const minBalance = Math.min(...hourlyBalances.flatMap((b) => Object.values(b)));
console.log(JSON.stringify({
    hours: HOURS, fault, stats, lostAtRestart,
    maxDriftFromSeedAtEnd: drift,
    minSevenBalanceAtHourEnds: minBalance,
    endBalances: Object.fromEntries(SEVEN.map((id) => [id, balance.get(id)])),
}));
