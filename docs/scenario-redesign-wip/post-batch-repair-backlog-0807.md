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
→ `load.north_south`가 page/size를 명시하게 한다.

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

## 예측 (측정 전에 박아둔다)

**F10-P는 주입과 무관하게 `transfer_2xx_rate < 0.95`로 must_rule_out 중단된다.**
남은 큐 banking 4종 중 F10-P만 `north_south` + `transfer_2xx_rate` 조합을 갖는다(= F18-P와 동일 서명).
F15-P·F17-P는 north_south만, F14-P는 transfer_2xx_rate만.

대조 시 볼 것:
- **언제 무너졌나** — 부하 가설이면 settle 직후 첫 평가 틱부터 (F18-P는 +52s). 주입 탓이면 주입 고유 지연을 두고.
- **GET vs POST 502 비율** — 부하 가설이면 GET이 훨씬 많아야 한다 (F18-P: GET 3459 vs POST 915). nginx 로그를 method로 가를 것.
- **빗나가면**: F10-P 주입이 transfer 자체나 DB 풀을 건드리면 두 원인이 겹쳐 구분 불가.
  그때는 `/api/accounts` 성공률(대조 팔)을 볼 것 — 풀 고갈이면 accounts는 멀쩡하다.
- min_hold 길이가 다르면 중단 **시각**만 달라지지 결론은 안 달라진다.
