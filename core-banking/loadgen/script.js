// core-banking k6 상주 부하 스크립트. commerce/loadgen/script.js 의 constant-arrival-rate +
// mulberry32(per-VU 시드 분기) 패턴을 그대로 따른다. 여정 비중: 잔액조회/거래내역 위주(read 80%+)
// + 소액 이체(banking 은 낮은 빈도가 현실적이라 write 비중을 commerce 보다 낮게 잡음).
import http from 'k6/http';
import { sleep, check } from 'k6';
import exec from 'k6/execution';

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

// 이체 계좌 풀(2026-11-09, v4 E5 와 같은 733개). 동반 부하 다섯 스크립트와 같은 목록이다
// (test_load_north_south_executor 가 같음을 확인한다). 근거는 surge.js 의 같은 목록 주석.
const TRANSFER_ACCOUNTS = (
    '5002,5003,5004,5007,5008,5009,5010,5011,5012,5013,5015,5017,5018,5019,5020'
    + ',5021,5024,5026,5028,5029,5030,5031,5032,5034,5036,5037,5040,5041,5042,5043'
    + ',5044,5045,5046,5047,5048,5049,5051,5053,5054,5055,5056,5058,5061,5062,5063'
    + ',5066,5068,5069,5071,5073,5075,5076,5077,5078,5079,5080,5081,5083,5087,5088'
    + ',5089,5093,5094,5095,5097,5098,5099,5100,5102,5103,5104,5105,5106,5107,5108'
    + ',5110,5111,5112,5113,5114,5115,5116,5117,5118,5119,5120,5121,5122,5123,5124'
    + ',5126,5127,5129,5130,5131,5132,5133,5134,5135,5137,5138,5141,5143,5144,5145'
    + ',5147,5148,5149,5150,5151,5152,5153,5154,5157,5158,5159,5161,5162,5163,5167'
    + ',5168,5169,5170,5171,5172,5173,5174,5176,5177,5178,5180,5182,5183,5186,5187'
    + ',5188,5189,5193,5194,5195,5196,5198,5199,5201,5202,5203,5204,5205,5206,5207'
    + ',5208,5209,5210,5211,5212,5213,5214,5215,5217,5218,5219,5221,5223,5224,5226'
    + ',5227,5229,5231,5232,5233,5234,5235,5237,5238,5239,5240,5241,5242,5243,5244'
    + ',5245,5246,5248,5249,5250,5251,5252,5253,5254,5255,5256,5257,5258,5259,5260'
    + ',5261,5263,5264,5265,5266,5267,5268,5271,5272,5273,5275,5276,5279,5282,5283'
    + ',5285,5287,5288,5289,5290,5291,5292,5293,5294,5295,5296,5298,5299,5301,5305'
    + ',5308,5311,5312,5313,5314,5316,5317,5318,5320,5322,5323,5324,5325,5327,5328'
    + ',5329,5330,5332,5333,5334,5335,5336,5337,5338,5339,5340,5341,5342,5343,5344'
    + ',5345,5346,5347,5348,5349,5350,5352,5353,5355,5356,5357,5358,5359,5360,5361'
    + ',5362,5366,5368,5369,5370,5371,5372,5373,5374,5376,5378,5379,5380,5381,5382'
    + ',5383,5384,5385,5386,5387,5388,5389,5390,5391,5394,5395,5396,5397,5398,5399'
    + ',5400,5401,5403,5404,5406,5407,5408,5409,5412,5415,5416,5417,5418,5419,5420'
    + ',5423,5424,5425,5426,5427,5428,5429,5430,5433,5435,5437,5438,5439,5440,5441'
    + ',5443,5445,5446,5449,5451,5452,5453,5454,5455,5458,5460,5461,5462,5463,5464'
    + ',5465,5466,5467,5468,5469,5470,5471,5472,5473,5474,5476,5477,5478,5479,5483'
    + ',5486,5487,5488,5489,5490,5491,5492,5493,5494,5496,5497,5498,5499,5501,5502'
    + ',5503,5504,5505,5506,5507,5509,5510,5511,5512,5514,5515,5517,5518,5519,5521'
    + ',5522,5523,5524,5525,5527,5529,5530,5532,5533,5534,5535,5536,5537,5539,5540'
    + ',5541,5542,5543,5544,5545,5548,5549,5550,5551,5552,5554,5556,5557,5559,5561'
    + ',5562,5563,5565,5567,5568,5569,5570,5571,5572,5573,5574,5576,5577,5578,5579'
    + ',5580,5584,5586,5587,5588,5589,5591,5592,5593,5594,5596,5598,5599,5600,5601'
    + ',5602,5603,5605,5606,5608,5609,5610,5611,5613,5615,5616,5617,5618,5619,5620'
    + ',5622,5623,5624,5625,5627,5630,5631,5632,5633,5634,5635,5638,5639,5640,5642'
    + ',5643,5644,5645,5646,5647,5648,5649,5650,5652,5655,5656,5657,5658,5659,5660'
    + ',5662,5663,5664,5665,5666,5667,5668,5669,5670,5671,5672,5675,5676,5678,5679'
    + ',5680,5681,5682,5683,5684,5685,5686,5687,5688,5689,5690,5692,5693,5694,5695'
    + ',5696,5697,5698,5699,5700,5701,5704,5705,5706,5707,5708,5709,5710,5711,5713'
    + ',5714,5717,5718,5719,5721,5722,5723,5724,5725,5727,5728,5729,5730,5731,5733'
    + ',5734,5735,5736,5737,5738,5740,5741,5742,5745,5746,5748,5749,5750,5751,5752'
    + ',5753,5754,5758,5759,5760,5761,5762,5763,5764,5765,5767,5768,5770,5771,5775'
    + ',5776,5777,5778,5779,5780,5781,5783,5785,5788,5789,5790,5792,5795,5799,5800'
    + ',5801,5803,5807,5808,5809,5812,5816,5818,5819,5822,5823,5824,5825,5826,5827'
    + ',5828,5830,5832,5833,5834,5835,5837,5838,5840,5841,5842,5843,5844,5845,5847'
    + ',5848,5850,5851,5852,5854,5856,5857,5861,5862,5863,5865,5866,5868,5869,5871'
    + ',5872,5873,5874,5876,5877,5878,5879,5880,5881,5882,5884,5885,5886,5889,5890'
    + ',5891,5892,5893,5895,5896,5897,5898,5899,5900,5901,5902,5903,5905,5906,5907'
    + ',5908,5909,5910,5911,5913,5914,5915,5917,5918,5919,5920,5921,5922,5924,5925'
    + ',5926,5928,5929,5930,5931,5933,5934,5935,5936,5937,5938,5939,5940,5941,5942'
    + ',5944,5945,5946,5947,5950,5951,5953,5955,5957,5958,5959,5962,5963,5967,5969'
    + ',5971,5973,5975,5976,5977,5978,5979,5980,5981,5983,5984,5986,5987'
).split(',').map((n) => `ACC-${n}`);

function pick(arr) {
  return arr[randInt(0, arr.length - 1)];
}

// step 태그는 관측 계약이다 — loadgen_monitor가 business_step/read_step 태그로
// 사업 지표를 분해한다(profiles.json domain_profiles: business_step=transfer,
// read_step=get). commerce·food-delivery baseline은 처음부터 태그를 달았는데
// core-banking만 빠져 있어 이 도메인의 baseline 사업 지표를 셀 수 없었다.
function balanceCheckJourney() {
  const id = pick(ACTIVE_ACCOUNTS);
  const res = http.get(`${GATEWAY_URL}/api/accounts/${id}`,
    { tags: { journey: 'balance', step: 'get' } });
  check(res, { 'balance check status is 200': (r) => r.status === 200 });
}

function transactionHistoryJourney() {
  const id = pick(ACTIVE_ACCOUNTS);
  const res = http.get(`${GATEWAY_URL}/api/transfers?fromAccount=${id}&page=0&size=20`,
    { tags: { journey: 'history', step: 'history' } });
  check(res, { 'transaction history status ok': (r) => r.status === 200 || r.status === 404 });
}

function accountListJourney() {
  const res = http.get(`${GATEWAY_URL}/api/accounts?status=ACTIVE&size=20`,
    { tags: { journey: 'browsing', step: 'list' } });
  check(res, { 'account list status ok': (r) => r.status === 200 || r.status === 404 });
}

// 소액 이체(2026-11-09, v4 확인 필요 11). 예전에는 활성 계좌 7개끼리 이체해(66 하루 약 32,000건,
// 8억) 잔액이 무작위 걸음으로 흩어지고 잔액 부족 400 이 평시에도 났다(F72-R 녹화 평시 59건/2시간).
// 이제 이체는 두 번이 한 짝이다:
//   보내기: 대량 계좌 풀(TRANSFER_ACCOUNTS 733개) 하나 → 활성 계좌 7개 중 하나, 금액 1,000~50,000
//   되돌리기: 이 VU 의 다음 이체가 같은 금액을 받은 계좌에서 보낸 계좌로 돌려준다
// 받은 만큼만 돌려주므로 이 부하로는 활성 계좌 7개가 시작 잔액 아래로 내려가지 않고(잔액 부족 0), 풀 계좌는
// 한 번에 많아야 금액 하나(50,000)만 비어 있다. 7개 계좌는 이체 절반의 받는 쪽, 절반의 보내는
// 쪽이라 입금(F74-R 폴링 전제: 오늘 완료 입금 1건 이상)과 출금 내역(거래 내역 첫 쪽 20건이
// 차서 건수 질의가 도는 조건)이 계속 쌓인다. 짝은 VU 안에서만 이어지므로 k6 가 매시 새로 뜨기
// 전에 남은 되돌리기를 먼저 한다(아래 nearRunEnd).
let pendingReturn = null;

// 되돌리기 결과(2026-11-10, v4 E5b). 돈이 옮겨진 이체(COMPLETED)만 되돌릴 짝을 남기고, 되돌리기가
// 서버 쪽 실패(5xx, 연결 실패)면 짝을 남겨 이 VU 의 다음 이체가 다시 시도한다. 4xx 나 FAILED 는 짝을
// 버린다(받은 돈이 다른 흐름으로 빠졌다는 뜻이라 다시 해도 같다).
function transferCompleted(res) {
  if (res.status !== 200) {
    return false;
  }
  try {
    return res.json('status') === 'COMPLETED';
  } catch (e) {
    return false;
  }
}

function retryLater(res) {
  return res.status === 0 || res.status >= 500;
}

function smallTransferJourney() {
  let from;
  let to;
  let amount;
  const returning = pendingReturn !== null;
  if (returning) {
    ({ from, to, amount } = pendingReturn);
  } else {
    from = pick(TRANSFER_ACCOUNTS);
    to = pick(ACTIVE_ACCOUNTS);
    amount = randInt(1000, 50000);
  }
  const payload = JSON.stringify({
    fromAccount: from,
    toAccount: to,
    amount: amount,
    orderId: null,
  });
  const res = http.post(`${GATEWAY_URL}/api/transfers`, payload, {
    headers: { 'Content-Type': 'application/json' },
    tags: { journey: 'transfer', step: 'transfer' },
  });
  check(res, { 'transfer status ok': (r) => r.status === 200 });
  if (returning) {
    if (!retryLater(res)) {
      pendingReturn = null;
    }
  } else if (transferCompleted(res)) {
    // 돈이 실제로 옮겨진 이체만 되돌린다(잔액 부족이면 200 이어도 status 가 FAILED).
    pendingReturn = { from: to, to: from, amount: amount };
  }
}

// k6 는 한 시간마다 새로 뜬다(entrypoint.sh). 끝나기 3분 전부터는 되돌릴 짝이 남은 VU 가 여정
// 대신 되돌리기를 먼저 해서, 재시작 때 짝이 버려져 잔액이 한쪽으로 새지 않게 한다.
const RUN_DURATION_MS = 60 * 60 * 1000;
const RETURN_FLUSH_MS = 3 * 60 * 1000;

function nearRunEnd() {
  return exec.scenario.startTime + RUN_DURATION_MS - Date.now() < RETURN_FLUSH_MS;
}

export default function () {
  if (pendingReturn !== null && nearRunEnd()) {
    smallTransferJourney();
    sleep(randInt(0, 1));
    return;
  }
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
