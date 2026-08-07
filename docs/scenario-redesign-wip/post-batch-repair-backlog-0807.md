# 배치 후 수리 백로그 (2026-08-07 야간 배치 `live-41-58c5b3e5`)

목표: **다음 주 출근 시 41종이 전부 문제없이 돈다.**
배치가 도는 중에 확정된 것만 적는다. 배치 완주 전에는 클러스터를 바꾸지 않는다.

순서에 의존성이 있다. **B → A → 재실행** 순.

---

## B. `load.north_south` 무제한 목록 조회 — **선행 조건**

이걸 안 고치면 F18-P·F10-P는 무엇을 고쳐도 판정에 도달하지 못한다.

**기전 (확정)**
`load.north_south`가 `GET /api/transfers`를 page/size 없이 호출 →
`TransferController.java:60-63`이 **의도적으로** `Pageable.unpaged()` 사용 (주석에 근거 명시) →
`banking.transfers` 980,498행 전수 조회 → Hikari 풀(15) 고갈 →
동기 이체 POST가 502 → `transfer_2xx_rate` 0.44 → 감별자 `sync-path-also-broken`(`lt 0.95`) 발화 → 중단.

주입과 **인과관계 없음**. 시나리오가 자기 동반 부하로 자기 감별자를 켰다.

| | 실측 (F18-P 주입 창) |
|---|---|
| `POST /api/transfers` | 200: 733 / 400: 18 / **502: 915** → 2xx ≈ 0.44 |
| `GET /api/transfers` | 200: 675 / **502: 3459** |
| `GET /api/accounts*` | 거의 전량 200 (다른 서비스·다른 풀) |
| 주입 없는 평시 최근 5분 | **502 = 0건** |

영구 파손이 아니라 부하량 의존 조건부 파손이다.

**수리 방향**: 앱이 아니라 **부하 계약** 쪽. 무제한 조회는 앱의 의도된 동작이고 주석에 근거가 있다.
엔드포인트에 상한을 두면 F17-R 등 다른 시나리오의 전제를 건드린다.

### 수리 표면 — 41종이 아니라 **한 줄 × 3파일** (확정)

`load.north_south`를 쓰는 시나리오는 41종이지만(companion 38 + 주입 자체 3),
**무제한 목록 조회는 banking 스크립트에만, 각 한 줄씩 있다.** k6 스크립트 9종 전수 확인:

```
/opt/loadgen/core-banking/surge.js:82                 ← F10-P, F15-P, F18-P
/opt/loadgen/core-banking/transfer-heavy-surge.js:82  ← F21-P (parked)
/opt/loadgen/core-banking/script.js:64                ← 기저 부하 (항상 도는 것, TARGET_RPS=1)
```
```js
const res = http.get(`${GATEWAY_URL}/api/transfers?fromAccount=${id}`, {   // page/size 없음
```

**같은 파일의 나머지 호출은 전부 유계다** — `/api/accounts?status=ACTIVE&size=20`(:89), 단건 조회, POST.
commerce·food surge 계열에는 무제한 목록 조회가 **없다**(전부 `size=20`).
**banking 이체 목록만 관행에서 빠져 있었다. 설계 실수 한 줄이다.**

→ **`&page=0&size=20` 추가.** 새 값을 고르는 게 아니라 이 테스트베드가 이미 쓰는 값을 맞추는 것이다
(commerce products, food restaurants/deliveries, banking accounts 전부 20).
호스트: tb-runner `nkia@192.168.122.206`, `/opt/loadgen/` 아래.

### 실측 근거

```
GET /api/transfers?fromAccount=ACC-1001                → 10,518,310 B (10.0 MiB), 49,266행, 0.258s
GET /api/transfers?fromAccount=ACC-1001&page=0&size=20 →      4,089 B,            20행, 0.066s
GET /api/accounts?status=ACTIVE&size=20  (대조군)       →      1,618 B,                  0.004s
```

**2,572배.** 30 rps × 10 MiB ≈ 300 MiB/s 직렬화, transfer 힙은 `-Xmx512m`. 풀 고갈 + GC 압박을 함께 만든다.

`banking.transfers` 980,970행의 계정별 분포 — loadgen이 쓰는 ACC-1001~1007이 **각 약 5만 행**:
```
commerce-settlement 618,607 (loadgen 미사용) / ACC-1005 50,345 / ACC-1002 49,687 / ACC-1007 49,329
ACC-1001 49,266 / ACC-1006 49,263 / ACC-1004 47,298 / ACC-1003 47,118
```
**이체가 계속 쌓이므로 매일 커진다** — 오늘 10MB면 다음 주엔 더 크다. 시간이 갈수록 나빠지는 형태다(→ 보존 정책).

### 건드리면 안 되는 것 (확정)

**F20-Q "food 주문 목록의 페이지네이션 부재 — 무제한 결과셋이 힙을 채운다"** — 페이지네이션 부재가 **주입 자체**다.
다행히 별개 스크립트(`food-delivery/slowquery.js:94` `/api/orders`)의 별개 엔드포인트라 banking 수리와 겹치지 않는다.
F20-P("trunc() 풀스캔")도 `/api/transfers/stats/daily?days=90` — 통계 엔드포인트라 무관.

### 임계 조정 — 불필요 (확정)

north_south 사용 시나리오의 부하 임계는 전부 `achieved_rps`(초당 요청 수)이고 **응답 크기와 무관**하다.
```
F10-P·F15-P  achieved_rps > 60 (target 20)   F17-P  > 40 (frozen-bypass, 무관)
F18-P        transfer_2xx_rate < 0.95 (target 30)
```
※ 가설: 응답이 작아지면 k6가 목표 rps를 더 쉽게 달성해 `achieved_rps`가 **오른다**.
임계 60은 target 20~30 대비 여유가 크지만 수리 후 첫 실행에서 실측 확인할 것.
반증법: 수리 후 F18-P를 동일 target_rps로 1회 실행해 대조.

### 다이제스트 — 해싱하지 않는다. 세 줄만 고치면 끝이다. (확정)

- `_digest()`는 **JSON 정규화 해시**이고 파일을 읽지 않는다 (`contracts/compile-plan.py:29,33,421,429`)
- compile-plan.py가 파일을 읽는 곳은 둘뿐: JSON 레지스트리(`:21`), 실행기 `.py`(`:385 executor_sha256`)
- `grep -n "script_path" compile-plan.py` → **0건**. 경로는 parameters 값으로 실려 다닐 뿐 열지 않는다
- 캡슐 `hashes.json` 425개 파일 중 **`.js` 0개** — k6는 계약 루트 밖(tb-runner `/opt/loadgen/`)
- 실행기도 내용 미검증: 경로 allowlist(`:49`)와 `-r` 확인(`:137`)뿐
- `execution.py:53 script_sha256`은 **레거시 시나리오 실행 스크립트**(`runner.py:171`)이고 k6와 무관

→ **레지스트리 변경·매니페스트 재생성 불필요. 기존 런 다이제스트와도 어긋나지 않는다.**

> ⚠ **실행기 `.py`는 건드리지 마라.** `load_north_south_executor.py`를 고치면 `executor_sha256`이 바뀌고
> 그건 `plan_digest`에 들어간다. `.js`만 만지는 한 무관하지만, "겸사겸사" 실행기까지 손대면 재생성이 필요해진다.

### 별도 확인 대상
F15-P는 `transfer_2xx_rate`를 관측하지 않는다(p95·노드 자원만). 무제한 조회는 F15-P에서
**`transfer_p95`·`account_p95`를 부풀리는** 형태로 오염된다 — 중단은 안 시키지만 지연 신호를 못 믿게 만든다.
반증법: 수리 전후 `transfer_p95` 대조. (미조사)

**타이밍 함정 (확정)**: F18-P에서 2xx는 주입 **45초 뒤부터** 무너져 24틱 내내 임계 아래였다.
`min_hold` 8분이 그걸 가리다가 만료 직후 2틱으로 집행했다. **중단은 처음부터 예약돼 있었다.**
→ 로그에서 "min_hold 만료 직후 중단"을 보면 위배 시작 시각을 반드시 거슬러 확인할 것.

---

## A. banking 아웃박스 교차 배수 — ledger 릴레이 정지

**기전 (확정, 트레이스 전수)**
transfer·ledger **둘 다** `outbox.relay.enabled: true`이고 **둘 다 `default_schema` 미설정** →
같은 `BANKING.outbox_events`를 무스코프 `findTop100ByPublishedAtIsNullOrderByCreatedAtAsc()`로 경쟁 소비.

24시간 전수 (08-06 19:00 ~ 08-07 19:44):

| 발행 서비스 | `banking.transfers` | `banking.ledger` |
|---|---|---|
| core-banking-transfer | 13,072 | **13,154** |
| core-banking-ledger | **13,696** | 13,074 |

**양방향이다.** 행 하나가 어느 릴레이에 집히는지는 동전 던지기.

F18-P 주입 창 자연 실험:

| 구간 | transfer | ledger |
|---|---|---|
| 주입 전 | 10 | 4 |
| **주입 중** | **0** | **110** |
| 주입 후 | 19 | 12 |

→ **주입은 자기 대상에 완벽히 작동했다.** ledger가 빈자리를 흡수해 흔적을 지웠을 뿐.
스팬 속성 `thread.name=scheduling-1`(= `@Scheduled relay()` 전용)이 못을 박는다.
이 하나로 08-03부터의 F18-P 5회 실패 **전부**가 설명된다.

**세 도메인 대조 — banking만 격리 장치가 없다**

| 도메인 | relay 켠 서비스 | 격리 | 결과 |
|---|---|---|---|
| commerce | 5종 | 서비스별 PG 스키마 (`hibernate.default_schema`) | 안전 — F04-H 3/3 성공 (미발행 2290~3009 축적) |
| food-delivery | 3종 | 서비스별 테이블 (`*_outbox_events`, 제네릭 릴레이) | 안전 |
| **core-banking** | 2종 | **없음** (`default_schema` grep 0건) | **F18-P 5/5 실패** |

commerce는 서비스 5종·제어 필드 부재라는 **더 나쁜 조건**에서도 한 줄로 격리가 성립한다.

**이번 주기 수리: `ledger-service/application.yml`의 `outbox.relay.enabled: false`**

손실 0이 확정됐다:
- 전 도메인 `@KafkaListener` 8개 전수 — `banking.ledger` 구독 **0개**
- Kafka `--all-groups` — `banking.ledger` 컨슈머 그룹 **부재**(오프셋 커밋 이력조차 없음)
- 토픽에 약 **862,565건**이 쌓인 채 소비된 적 없음
- `LedgerEventPublisher.java:14-15` 주석이 스스로 인정 ("현재 consumer 없음, 향후 확장 지점")

끈 뒤 동작:
- ledger의 `OutboxRelay`·`OutboxRelayStore` 빈 소멸 (둘 다 `@ConditionalOnProperty`) → 교차 배수 소멸
- ledger의 LEDGER 행 INSERT는 **계속**됨 (`OutboxPublisher`는 무조건 `@Component`)
- 그 행의 발행은 **transfer 릴레이가 수행** — 이미 13,154건 하고 있다는 직접 증거
- F18-P 주입 시 = 유일한 배수구 정지 → 미발행 단조 증가 → success 조건 `> 20` 성립

**⚠ 이건 결함 제거가 아니라 발현 봉쇄다.**
무스코프 `findPending`은 그대로 남는다. 세 번째 relay-enabled banking 서비스가 생기거나
누가 이 줄을 되켜면 **조용히 재발**하고, 재발해도 오류가 안 난다 — 5회 놓친 그대로다.
- 그 줄에 이유를 주석으로 남길 것 (F18-P + 이 문서 참조)
- 테스트로 고정: banking에서 `outbox.relay.enabled: true`인 서비스가 1개를 넘지 않는다

---

## C. F18-P 레지스트리 자기모순

`scenario-metadata.json`의 F18-P에서 `cause`·`root_cause.mechanism`·`root_cause.infra_anchor`는
아직 **env 토글**(`OUTBOX_RELAY_ENABLED=false` + `@ConditionalOnProperty`, "k8s.env executor")로 서술돼 있는데,
같은 문서의 `injection_summary`·`injected_fault`는 **app.control DB 행**이고 "env 토글은 쓰지 않는다"고 명시한다.
`infra_anchor`가 `injection_summary`를 정면으로 반박한다. → 4면 동기화 대상.

---

## D. 러너 — companion cleanup이 recovery 판정보다 먼저 프로브 파일을 지운다

중단 후 5틱에서 `entry_status`·`transfer_2xx_rate`가 전부 unusable:
`exit 1: cat: /tmp/rca-scenario-F18-P-live.json: No such file or directory`.
이번엔 recovery 조건이 그 둘을 안 봐서 통과했을 뿐이다.
첫 aborted 틱의 `pod_ready=False`도 이 구간 것이라 "회복 실패"로 오독될 수 있다.

---

## 조사 중 (2026-08-07 19:45 시점)

### F19-P·F19-S — **해결. 대조군이 대조군이 아니다.** (확정)

둘 다 `aborted must_rule_out`, success 스트릭 14·16이 쌓인 상태에서 거부됐다.

**주입 대상**(`scenario-metadata.json`):
```
F19-P  mock.expectation  /pay  delay 8s   "외부 PG 지연(타임아웃 미만) — 502 없이 order 커넥션 풀만 마른다"
F19-S  mock.expectation  /pay  delay 30s  "외부 PG 지연(타임아웃 초과) — 502와 서킷 개방이 주문으로 연쇄한다"
```

**모든 주문이 `/pay`를 지난다 — 베이스라인 암도 포함해서.**
따라서 `order_create_5xx_rate_baseline = 1.0`은 주입이 **작동한** 증거이지 교란 요인의 증거가 아니다.
**베이스라인 암은 이 시나리오의 대조군이 될 수 없다. 주입된 구성요소를 공유하기 때문이다.**

`7c90e08`이 넣은 베이스라인 감별자는 **정확히 주입이 성공할 때 거부한다.** 구조적으로 무효다.

#### 트레이스 실측 — 환경은 멀쩡하다 (`POST /api/orders`, food-delivery-order SERVER)

| 구간 | 5xx | 전체 | 비율 |
|---|---|---|---|
| 공백 (F17-R 후, 18:59–19:04) | 0 | 6 | 0 |
| F18-P 주입 (banking, 19:04–19:14) | 0 | 11 | 0 |
| 공백 | 0 | 4 | 0 |
| **F19-P 주입 (19:18–19:27)** | **182** | **223** | **0.816** |
| 공백 | 0 | 4 | 0 |
| **F19-S 주입 (19:32–19:42)** | **229** | **242** | **0.946** |
| 공백 | 0 | 4 | 0 |
| F16-H 주입 (commerce, 19:46–19:54) | 0 | 14 | 0 |
| F20-R 이후 | 0 | 16 | 0 |

**5xx는 F19 주입 창에만 있다. 나머지 전 구간 59건 중 5xx 0건.**
분모도 223·242로 크다 — 표본 부족이 아니다.

#### 그래서 무슨 일이 있었나 — 오늘 수리 둘의 상호작용
1. **이전**: `dispatches` status 인덱스 부재 → 주문 생성마다 풀스캔 → 상시 5xx →
   F19가 **환경 5xx로 거짓 통과**했다. (이게 `7c90e08`을 넣은 원래 이유다)
2. **오늘 수리 ①**: `(status, assigned_at)` 인덱스 투입 → **환경이 깨끗해졌다**(위 표가 증거).
3. **오늘 수리 ②**: 베이스라인 감별자 추가 — 2차 방어선으로 넣었다.
4. **그런데 ②는 구조적으로 무효다.** 베이스라인이 주입된 목을 공유하므로,
   ①이 원인을 제거한 지금 ②는 **유효한 런만 거부한다.**

**→ 수리: `7c90e08`의 베이스라인 감별자를 F19-P·F19-S에서 제거한다.**
원래 막으려던 환경 5xx는 인덱스 수리로 이미 사라졌고, 그 사실은 위 표로 검증됐다.
재발 감시가 필요하면 베이스라인 암이 아니라 **주입 목의 상태**를 보는 조건이어야 한다.

> **교훈**: 대조군을 설계할 때 "주입된 구성요소를 지나지 않는가"를 먼저 확인해야 한다.
> 공유 진입점을 쓰는 부하는 대조군이 아니다. — 오늘 F18-P의 "자기 부하가 자기 감별자를 켠다"와 짝을 이루는 결함이다.

미결: `payment_error_rate=100.0` — 다른 신호는 0~1인데 이것만 100. 단위 계약 불일치 의심.

### E. 비율 신호가 분모 없이 기록된다 — **확정, 감사 불가능성의 원인**

`ticks.jsonl`의 신호 봉투는 비율만 담는다. 표본 수가 없다:

```json
{"source": "k6:baseline:food-delivery:business_5xx_rate",
 "value": 0.0, "quality": "good", "usable": true,
 "freshness": "fresh", "observed_at": "...", "update_interval_sec": 30, "error": null}
```

→ **"1.0이 2/2인지 40/40인지"를 런 산출물로 답할 수 없다.** F19 진단이 여기서 막혔다.
비율 기반 게이트 **전부**가 사후 감사 불가능하다는 뜻이다.

오늘 `loadgen_monitor.py`에서 분모(`checkout_count`·`read_count`)를 항상 발행하도록 고쳤지만,
**러너가 그것을 판정 기록으로 옮기지 않는다.** 관측 시점에는 존재하는 값이 기록에서 사라진다.

베이스라인 문서(`/app/state/loadgen/baseline-*-live.json`)는 런 중에만 존재하고 cleanup에서 사라지므로
사후 조회로도 복구 불가능하다(§D와 같은 계열 — 판정 근거가 판정보다 먼저 삭제된다).

**수리**: 비율 신호의 봉투에 표본 수를 함께 실어라(최소한 `ticks.jsonl`에).
분모 없는 비율은 판정 근거로 쓸 수 없다 — 이건 오늘 다섯 번째 "측정이 거짓말하는" 사례다.
우회로는 트레이스에서 직접 세는 것뿐이고, food-502 에이전트가 그 방법으로 진행 중이다.

---

## F. `achieved_rps` 감별자 29건/26종 — 한 지표가 두 실패 양상을 동시에 만든다

`controllers.json` + `controllers-parked.json` 전수. **두 계열이 같은 신호를 반대 방향으로 읽는다.**

### 계열 A — `gt` (부하 과다 배제형) 15건: **건당 비용 급증에 눈이 먼다** → 거짓 음성 → 거짓 성공

F10-P·F15-P·F21-P(각 `gt 60`/`gt 45`, target 20·30) / F17-P `gt 40` / F02-H·F05-P·F09-H·F09-R `gt 60` /
F09-P `gt 65`(target 60) / F10-H `gt 60` / F12-H `gt 40`(target 35) / F21-Q `gt 100` /
**F03-P `overload-not-pool gte 100`** / F05-R `general-overload gt 45` / F11-R `gt 70`(target 65)

막으려던 것: "고장이 아니라 부하가 많아서 느린 것"의 배제.
맹점: **rps는 그대로인데 요청 하나의 원가만 오르는 형태를 통과시킨다.** F10-P가 그 실례다.

특히 **F03-P의 `overload-not-pool`은 이름이 "과부하지 풀 문제가 아니다"를 가른다는 뜻인데,
정작 풀을 마르게 하는 것은 rps가 아니라 커넥션 점유 시간이다.**
감별자가 자기가 가르겠다고 선언한 축을 안 보고 있다.

### 계열 B — `lt` (부하 전달 확인형) 14건: **반대로 과민하다** → 거짓 양성 → 거짓 중단

F03-H·F03-P·F05-H·F06-H·F06-P·F07-P·F08-P·F15-H·F15-R·F15-T2·F17-R `lt 15` /
F05-R·F11-R `lt 30` / **F07-H `surge-not-achieved lt 100`(target 160)**

막으려던 것: "부하가 실제로 전달됐음"의 확인 — 로드젠이 죽은 창을 무효화한다.
맹점 아님. 다만 요청이 비싸지면 k6가 목표 rps를 못 채워 `achieved_rps`가 **떨어지고** 이게 발화한다.

**→ 건당 비용이 오르면 A는 침묵하고 B는 비명을 지른다.**

### 이미 알고 있던 것과의 수렴
**F06-P·F15-H는 target 5인데 임계가 `lt 15`** — 정상 동작 중에도 조건이 성립한다.
이건 배포 전부터 대기 중이던 항목("설계 부하 5rps vs `lt 15` — 항상 발화")과 **독립적으로 같은 결론**에 도달했다.
독립 수렴이므로 신뢰도가 높다. 우선 처리 대상.

### B 수리가 이들에 미치는 영향 — 임계 조정 불필요
수리는 banking 스크립트만 건드리므로 `achieved_rps`가 오르는 것은 F10-P·F15-P·F21-P(+F18-P는 이 감별자 없음)뿐이고,
각각 `gt 60`(target 20)·`gt 45`(target 30)로 여유가 크다.
※ 실제 상승폭은 수리 후 1회 실행으로 확인할 것(가설).

---

## G. 통과 16건 인과 감사 — **거짓 통과 0건** (2026-08-07 20:03 UTC 스냅샷)

범위 `t1 >= 2026-08-07T13:42:44Z`, 25런(성공 16 = evaluation 13 + calibration 3 · 중단 7 · 실패 2).

### 결론: 16건 전부 판정이 주입 때문이다. 거짓 0 · 의심 0.

전 건 `BEFORE≈0 → DURING 급등 → AFTER 0.000` 패턴이 트레이스로 확인됐다(DURING 표본 n=277~1835).

**B 수리와 독립이다** — 16건 전부 `commerce/surge.js`를 썼고 `core-banking/surge.js` 사용은 **0건**이다(런별 `plan.json` 직접 확인).
무제한 조회에 노출된 넷 중 F18-P는 중단, F21-P는 parked, **F10-P·F15-P는 이번 배치에서 아직 미실행**이다.
core-banking 통과 4건(F01-P·F08-G·F15-G·F17-R)은 commerce 진입점을 쓰는 크로스도메인이라 banking 스크립트를 안 쓴다.

### F07-H — 배치 안에 이미 있던 음성 대조군 (확정)

**`load.north_south` 주 프로파일, rps 160(전 시나리오 최대), checkout 실패율 0.000, n=1,835.**

→ **commerce 부하는 그 자체로 checkout 5xx를 만들지 않는다.**
F18-P를 망친 "부하가 감별 신호를 만든다" 함정이 commerce 경로에는 없다는 직접 증거다.
따라서 다른 런에서 주입 창에만 5xx가 오르는 것은 주입 때문이라고 읽어도 된다.

### 감사 도구 `causality_absent` 6건은 전부 거짓 음성 (확정)

| 함정 | 대상 | 시나리오 |
|---|---|---|
| **A. 대상이 DB라 스팬이 없다** | `target_kind=database` | F06-H(PG payments), F08-G·F15-G·F01-P(Oracle accounts) |
| **B. 대상이 죽어서 스팬을 못 남긴다** | 파드 재시작/OOM | F05-H, F05-R |

F05-H는 `restart_count` 1·2·3이 오를 때마다 checkout 5xx가 **톱니파로 따라 오른다**(주입이 먼저, 증상이 나중).
파드가 죽어 있는 동안 실패한 요청이 그 파드의 스팬을 남길 리 없다.
**도구 출력을 그대로 믿었으면 6건을 거짓 통과로 오판했을 것이다.**

### 남은 불확실성 (정직하게)
BEFORE 표본이 n=3~33으로 작다. 런 간격 5~6분 + 기저 부하 1~2 rps의 구조적 한계다.
강한 근거는 BEFORE가 아니라 **DURING의 표본 크기와 AFTER의 0.000 복귀**, 그리고 F07-H 대조군이다.
F01-R은 배치 첫 런이라 BEFORE 대조가 아예 없다 — 고유 신호(`blocked_db_sessions`)로만 판단했다.

---

## H. 성공 조건이 시나리오를 식별하지 못한다 — 거짓 통과는 아니나 조용히 오염될 자리

**문자 그대로 동일한 성공 조건:**
```
[F01-H, F06-R, F08-G, F15-G]   checkout_5xx_rate >= 0.05                            ← 단독 조건
[F01-P, F17-R]                 checkout_5xx_rate >= 0.05  AND  payment_error_rate >= 10
```

F01-H(외부 PG 429) / F06-R(외부 PG 30초 hang) / F08-G(Oracle 계좌 잠금 + 배포 위장) / F15-G(재고+계좌 동시 잠금)
— **원인이 전혀 다른 넷이 같은 한 줄로 성공한다.** 성공 조건은 "체크아웃이 5% 이상 실패했다"밖에 말하지 않는다.
F01-P/F17-R 쌍은 성공 조건 **두 개가 완전히 일치**한다. 정답지가 다른 두 시나리오가 관측만으로는 같아 보인다.

식별을 `must_rule_out`에만 의존하고 성공 신호에는 판별력이 없다.
**F18-P와 같은 계열이다** — 신호가 주입에 고유하지 않으면 판정이 원인을 증명하지 못한다.
지금 거짓 통과가 안 난 것은 commerce 부하가 5xx를 안 만들어서일 뿐이다(F07-H).
**부하 프로파일이 바뀌거나 배경 5xx가 생기면 이 넷은 동시에 거짓 통과로 돌아선다.**(가설 — commerce에서 재현한 적 없음)

우선순위: **B·A보다 낮고 "무스코프 findPending"보다 높다.** 지금 오염 중은 아니지만, 오염되면 조용히 오염된다.

---

## 측정 창을 자를 때 (오늘 두 번 당했다)

**임의로 잡은 BEFORE 창은 거의 항상 직전 런의 주입을 문다.** 배치 런 간격이 5~6분이기 때문이다.
- 내가 KST/UTC를 혼동해 F19-P 주입 전체를 "조용한 구간"에 넣고 셌다 → 결론이 반대로 나왔다
- 감사 에이전트가 F08-G BEFORE를 17:05부터 잡아 0.96을 얻었다 → 직전 F01-P 주입이 17:07:55까지 돌고 있었다. 잘라내니 **0.000**

**규칙: BEFORE는 반드시 직전 런의 `t2` 이후로 자른다. 시각대(UTC/KST)를 명시한다.**

---

## 별도 항목 (이번 주기에 넣지 않음)

1. **공유 아웃박스 테이블에 대한 무스코프 `findPending`** — 항목 제목을 "Oracle 스키마 분리"로 잡지 말 것.
   해법 후보 넷: 서비스별 스키마(commerce 방식) / 서비스별 테이블(food 방식) /
   `aggregate_type` 스코프(공유 테이블 유지, DDL·이관 없음) / 릴레이 통합.
   **`aggregate_type` 스코프가 가장 쌀 수 있다 — 미검증 추측이므로 실측 후 판단.**
2. **보존 정책** — `banking.transfers` 980,498행 / `food.dispatches` 1,369,619행.
   오늘 **테이블 성장이 시나리오 판정을 뒤집은 사례가 둘**이다. 선택 항목이 아니다.
   F20 계열 재보정(2.32× 드리프트)도 여기에 묶인다.
3. **k6 스크립트가 다이제스트 보호 밖에 있다** — 실행기 `.py`는 `executor_sha256`으로 고정되는데
   실제 부하를 결정하는 `.js`는 안 된다. **보호 경계가 실제 인과 경계와 어긋나 있다.**
   부하 정의가 조용히 바뀌어도 런 산출물로는 감지할 수 없다 — 오늘 겪은 것과 같은 계열(주입은 걸렸는데 관측이 못 봄).
   안: `allowed_script_paths`에 경로만이 아니라 기대 sha256을 함께 싣고 실행기가 대조.
   ※ 이번 수리가 바로 이 공백 덕에 싸게 끝난다는 점은 아이러니다. 수리 후에 닫을 것.

4. **F21-P 제어 표면 비대칭** — `response_delay_control`도 `service_id` 키 단일 행이고 `transfer`만 값이 있다.
   지연은 필터 기반이라 아웃박스 공유와 무관하지만 같은 비대칭 위에 서 있다.
   F21-P 승격 전 이 표면이 대상 서비스에서만 유효한지 확인 필요. (미조사)

---

## 예측 — **정정됨**

### 틀린 예측 (기록으로 남긴다)
> "F10-P는 주입과 무관하게 `transfer_2xx_rate < 0.95`로 must_rule_out 중단된다."

**틀렸다. F10-P에는 `sync-path-also-broken` must_rule_out이 없다.** `controllers.json` 실측:

```
F10-P  success        [{"id":"transfer-2xx-collapse", "observation":"transfer_2xx_rate", "op":"lt", "value":0.5}]
       must_rule_out  [{"id":"user-load-overshot",    "observation":"achieved_rps",      "op":"gt", "value":60}]
```

`transfer_2xx_rate < 0.5`는 F10-P의 **성공 조건**이다. F18-P에서 부하만으로 실측된 2xx는 **0.30~0.49** — 그 임계 아래다.

### 정정된 예측
**F10-P는 중단되지 않고 `succeeded`로 판정될 가능성이 높다. 주입(fio)이 걸렸든 안 걸렸든.**

**거짓 중단보다 나쁘다.** 중단은 눈에 보이지만 거짓 성공은 유효 표본으로 조용히 데이터셋에 들어간다.

### 자체 감별자가 못 막는 이유 — 일반화할 만한 맹점
F10-P의 must_rule_out은 `achieved_rps > 60`이고 target_rps는 20이다. 감별자는 통과한다.

**감별자가 부하의 *양*을 보고 *건당 비용*을 안 보기 때문이다.**
무제한 조회는 rps를 그대로 둔 채 요청 하나의 원가만 **2,572배**로 올린다(10.0 MiB vs 4,089 B 실측).
`achieved_rps` 계열 감별자 전부가 이 형태에 눈이 멀어 있다. — 오늘 확인된 40개 감별자 조건 재검토 시 이 각도를 넣을 것.

### 그래서 볼 것 — "중단되는가"가 아니라 "성공이 주입 때문인가"

F10-P는 판별에 필요한 신호를 이미 관측한다. **`disk_io_util`과 `transfer_hikari_pending`의 선후:**

| 원인 | `disk_io_util` | `hikari_pending` | 순서 |
|---|---|---|---|
| 주입(fio)이 진짜 원인 | **급등** | 상승 | disk가 **먼저**, hikari가 따라온다 |
| 부하(무제한 조회)가 원인 | **평온** | 상승 | disk는 안 움직이고 hikari만 오른다 |

`disk_io_util` 평온 + 2xx가 0.5 아래 → **거짓 양성 확정.**

**F10-P 자신의 must_support 1번("PVC 백킹 디바이스 busy 비율 급등")이 곧 판별식인데 success 조건에 안 들어 있다.**
이게 이 시나리오의 별도 결함이다. → 수리 대상.

**중요**: F10-P가 안 멈춘다고 부하 가설이 반증되는 게 아니다. `disk_io_util` 평온 + 2xx 붕괴면 **확증**이다.
