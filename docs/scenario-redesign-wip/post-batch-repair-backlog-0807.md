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

### 미확인 — 수리 전 반드시 확인
**`plan_digest`/`manifest_digest`가 k6 스크립트 내용을 해싱하는가.**
레지스트리 `allowed_script_paths`는 경로만 고정하고 내용은 안 보므로 레지스트리 변경은 불필요하다(확정).
다이제스트가 내용을 해싱하면 얘기가 다르다.

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

### F19-P·F19-S — food 진입 경로 502
둘 다 `aborted must_rule_out`, success 스트릭 14·16이 쌓인 상태에서 거부됐다.

| | `order_create_5xx_rate` | `..._baseline` | `entry_status` |
|---|---|---|---|
| F19-P | `None!error/fresh` | 0.5 | `None!error/fresh` |
| F19-S | 0.918 | **1.0** | **502** |

**베이스라인이 주입 팔보다 더 망가져 있다.** → 감별자(`7c90e08`)는 **옳게 작동**하는 것이고,
결함은 환경일 가능성이 높다. F18-P와 같은 계열(진입 경로 502).
※ 초기 가설이었던 "감별자가 작은 분모의 잡음에 발화" 는 F19-S 증거로 뒤집혔다. 분모 확정 대기.

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

## 별도 항목 (이번 주기에 넣지 않음)

1. **공유 아웃박스 테이블에 대한 무스코프 `findPending`** — 항목 제목을 "Oracle 스키마 분리"로 잡지 말 것.
   해법 후보 넷: 서비스별 스키마(commerce 방식) / 서비스별 테이블(food 방식) /
   `aggregate_type` 스코프(공유 테이블 유지, DDL·이관 없음) / 릴레이 통합.
   **`aggregate_type` 스코프가 가장 쌀 수 있다 — 미검증 추측이므로 실측 후 판단.**
2. **보존 정책** — `banking.transfers` 980,498행 / `food.dispatches` 1,369,619행.
   오늘 **테이블 성장이 시나리오 판정을 뒤집은 사례가 둘**이다. 선택 항목이 아니다.
   F20 계열 재보정(2.32× 드리프트)도 여기에 묶인다.
3. **F21-P 제어 표면 비대칭** — `response_delay_control`도 `service_id` 키 단일 행이고 `transfer`만 값이 있다.
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
