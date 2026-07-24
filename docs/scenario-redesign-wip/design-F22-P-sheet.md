# 설계 시트 — F22-P: banking transfer 커넥션풀(15) 고갈 전용 (Class A)

- **백로그 근거**: fault-surface-core-banking.md §FS-6 "Hikari pool 고갈 (transfer max=15) — FS-1/FS-4로 lock 대기·relay가 15 커넥션 점유 → 신규 이체가 connection-timeout 3s 초과 → transfer 내부 500(CannotGetJdbcConnection) → account 502. root=transfer pool". 헌장 §2-B P3(무제한 비관적 row-lock) + P2(기본 풀·timeout 부재)의 결합.
- **코드 근거**: core-banking transfer-service 실코드 file:line 실측(아래 앵커 전부 grep 확인).
- **정체성(F01-P와의 결정적 차이)**: F01-P는 특정 계좌쌍(commerce-settlement) row-lock 대기로 **그 계좌를 건드리는 이체만** 느려지는 것을 정답으로 삼는다. F22-P는 같은 Oracle row-lock 주입면을 쓰되, **락 대기 이체가 Hikari 15 커넥션을 전부 점유**해 **락과 무관한 계좌쌍(예 ACC-1005→ACC-1006) 이체까지 connection-timeout 3s 만에 500**이 나는 것 — 즉 **커넥션풀 고갈 자체**를 정답으로 삼는 전용 시나리오다. 무관 계좌 이체의 실패가 "lock 대기(F01-P)"와 "pool 고갈(F22-P)"을 가르는 유일하고 결정적인 증거다.

---

## 1. 인과 사슬 (코드 실측)

```
db.lock(oracle): BANKING.accounts 의 hot 계좌 row(예 ACC-1001)에
   client_identifier=rca-F22-P-oracle-pool-lock 로 SELECT ... FOR UPDATE 장기 점유
  → banking surge 가 transfer-heavy·hot계좌 집중 이체를 고동시성으로 주입
  → 각 transfer.execute()(@Transactional, timeout 미설정)가 findByIdForUpdate(hot)에서
     NOWAIT/timeout 없이 무한 대기(AccountRepository.java:18-20, TransferService.java:51,68-71)
  → 대기하는 동안 Hikari 커넥션 1개씩 점유, 반납 안 됨
  → 동시 대기 transfer 수 ≥ maximum-pool-size(15) 도달 → 풀 고갈(transfer application.yml:13)
  → 이 시점부터 신규 transfer.execute()는 findByIdForUpdate에 도달하기도 전에
     커넥션 획득 단계에서 connection-timeout 3000ms 초과(application.yml:15)
     → CannotGetJdbcConnection → GlobalExceptionHandler 미매핑 → Spring 기본 500
  → account.validateAndForward의 transferClient 가 5xx 수신
     → RestClientException → ServiceException(502 BAD_GATEWAY)(account TransferClient.java:45)
  → nginx /api/accounts·/api/transfers 경로로 502 관측
     ← 증상=account/api(502), 원인=transfer 풀 고갈
```

**핵심 구분점**: 락 대기(F01-P)에서는 hot 계좌를 안 건드리는 이체는 자기 커넥션을 얻어 즉시 완료된다. F22-P는 **대기 커넥션이 15를 다 먹은 뒤**라 hot 계좌와 무관한 이체조차 커넥션을 못 얻어 3s 만에 500 — row-lock 경합이 아니라 **커넥션 획득 실패**가 실패 원인이다.

앵커:
- `transfer-service/.../repository/AccountRepository.java:18-20` — `@Lock(PESSIMISTIC_WRITE)` findByIdForUpdate, **NOWAIT/timeout 없음 → 무한 대기**.
- `transfer-service/.../service/TransferService.java:51` — `execute()` `@Transactional`, **timeout 미설정**(무한). `:68-71` — firstId/secondId 순차 `findByIdForUpdate` (커넥션 보유한 채 lock 대기).
- `transfer-service/src/main/resources/application.yml:13` — `maximum-pool-size: ${DB_POOL_MAX:15}`, `:15` — `connection-timeout: 3000`.
- `account-service/.../client/TransferClient.java:45` — RestClientException(transfer 5xx/timeout) → `ServiceException(502 BAD_GATEWAY)`. `:53` — CB open/retry 소진 fallback도 502.
- 주입면: `db.lock` executor(`profiles/db_lock_executor.py`, oracle 경로 :42-43) — F01-P와 동일 실물, live-검증됨.

**§2-A 위장 금지 준수**: 풀 고갈은 **DB_POOL_MAX(기본 15)를 자연 소진**해서 만든다. `DB_POOL_MAX`를 env로 낮춰 인위적으로 고갈시키는 것은 헌장 §2-A "env로 코드 동작 왜곡 = Class A 위장 금지"(F03-P가 걸린 함정) 위배이므로 **하지 않는다**. 15라는 실제 상수를 실제 동시 lock-대기로 넘겨야 한다.

---

## 2. 감별 설계 (골든 조건 ③) — F01-P와의 경계가 핵심

| | must_support | must_rule_out (감별점) |
|---|---|---|
| **F22-P** (pool 고갈) | account/api 502↑; transfer Hikari **active≈15 & pending>0**(커넥션 큐잉); **무관 계좌쌍(control) 이체도 3s 만에 실패**(control_transfer_2xx↓); tagged lock 존재 | **control(무관 계좌) 이체가 여전히 200이면 F22-P 아님 → F01-P(lock 대기)** · Oracle pod 정상 ready(전 서비스 광역 500 아님 → FS-12 Oracle-down 아님) · CB가 hot만 열린 게 아니라 커넥션 자체 부재 · 429 아님(banking 429 없음) |
| **F01-P** (lock 대기, KEEP) | hot(commerce-settlement) 계좌 경유 이체만 지연/502; cross-domain commerce checkout 5xx | 무관 계좌 이체는 정상 200(Hikari pending 없음/낮음) |

**결정 축**: F01-P와 F22-P는 **동일 주입면(Oracle row FOR UPDATE)**을 쓰므로 상태코드·lock 존재만으론 구분 불가. 유일한 결정적 증거는 **① transfer Hikari pending>0(active=15 포화, 커넥션 큐) + ② 락과 무관한 계좌쌍 이체까지 3s connection-timeout으로 실패**의 결합이다. F01-P에서는 무관 계좌 이체가 커넥션을 얻어 200으로 완료되고 pending도 0에 가깝다. 즉 F22-P의 정체성 = "**실패가 특정 row가 아니라 서비스 전체(커넥션 자원)에서 난다**".

**오답 유도**: 502가 항상 호출자(account/api)에서 나므로 naive RCA가 account를 범인으로 오판(fault-surface §7). 한 단계 더 — lock 존재를 보고 F01-P류 "특정 계좌 경합"으로 2차 오판. 정답은 lock이 트리거일 뿐 실제 5xx 원인은 **transfer 커넥션풀 고갈**이며, 증거는 무관 계좌 이체의 동반 실패다.

---

## 3. 골든 4조건 자체점검표

| 조건 | 판정 | 근거 |
|---|---|---|
| ① 코드/인프라 앵커 | ✅ | AccountRepository.java:18-20(무한 row-lock) + TransferService.java:51,68-71(timeout 없는 @Transactional 순차 lock) + application.yml:13,15(pool 15/timeout 3s) 실측. P3+P2 결합 매핑 |
| ② 정답(answer-key) | ✅ | metadata root_cause{service=core-banking-transfer, mechanism=Hikari 15 자연 고갈, code_anchor} 코드 거동 일치. lock은 트리거, root는 pool |
| ③ 감별 가능 | 🟡 | 감별 *설계*는 완결(무관 계좌 이체 실패 + Hikari pending이 F01-P와 결정적 구분). 그러나 관측 query 미존재: `prometheus.hikari_pending_connections`(transfer)·"무관 계좌(control) 이체 성공률" 미배선 |
| ④ 주입 수단 실재 | 🟡→❌ | db.lock oracle injector는 실재·live검증(F01-P). **그러나 단일 row-lock을 15 커넥션 고갈로 전환하려면 transfer-heavy·hot계좌 집중·고동시성 부하가 필요한데, 현 banking surge.js는 이체 10%·랜덤 계좌·건강상한 20rps라 구조적으로 ≥15 동시 대기를 못 만든다.** banking surge가 load.north_south에 아예 미배선(30082 사용 시나리오 0). = 진짜 능력갭 |
| **종합** | **draft / blocked** | ①②✅ ③🟡 ④🟡→❌ → `readiness=draft`, `prerequisite_gate.state=blocked`, `live_allowed=false` |

---

## 4. 능력 갭 (승격 전 선행 — prerequisite_gate)

### 4-A. 진짜 능력갭 (배선 아님 — F22-P를 blocked로 만드는 근본)
1. **banking 부하가 pool 고갈을 못 만든다 (근본)**: 현 `core-banking/loadgen/surge.js`는 조회55/내역25/목록10/**이체10%** 믹스에 랜덤 ACTIVE 계좌, 건강 상한 TARGET_RPS=20(실측 §8.1-1)이다. F22-P는 **hot 계좌(락 대상)로 이체를 집중**시켜 ≥15개가 동시에 FOR UPDATE 대기에 걸려야 풀이 고갈되는데, 랜덤·저비율·저rps 믹스로는 한 row에 15 동시 대기를 못 쌓는다. → **transfer-heavy·hot계좌 집중 surge 변형**(예 env `TRANSFER_RATIO`, `HOT_ACCOUNT` 편향 + 높은 동시성)이 신설돼야 한다. 헌장 §2-A상 `DB_POOL_MAX` 하향으로 우회하는 것은 금지(자연 고갈 요건). = 헌장 §4·§5 계열 "banking surge 계약" 갭.
2. **banking surge가 load.north_south에 미배선**: `profiles.json` load.north_south scenario_parameters에 30082(banking nginx) 진입을 쓰는 시나리오가 **하나도 없다**(F01-P는 cross-domain이라 commerce surge/30080 사용). F22-P용 계약 신설 필요: `entry_url=http://192.168.122.77:30082`, `script_path=/opt/loadgen/core-banking/surge.js`(또는 위 변형), `baseline_unit=loadgen-banking`, allowed_scenarios·tag_pattern에 F22-P 추가. tb-runner에 banking surge 배치도 확인 필요.

### 4-B. 관측 배선 (③ — 신규 query, 신규 코드 아님)
3. **NEW query_id: `prometheus.hikari_pending_connections`** (transfer): core-banking-transfer의 `hikaricp_connections_pending`/`hikaricp_connections_active` 게이지. **이 시나리오의 결정적 신호**(active=15 & pending>0 = 풀 큐잉). F19-P가 food-order용으로 이미 NEW 제안한 것과 **동일 query·다른 service_name** — 공유. OTel javaagent가 hikaricp 메트릭 노출하는지 확인 후 등록.
4. **NEW query: "무관 계좌(control) 이체 성공률"** (`loadgen.transfer_2xx_rate` 계열): F22-P의 **정체성 증거**. 락과 무관한 고정 control 계좌쌍(예 ACC-1006→ACC-1007, hot 아님)에 이체를 흘려 그 2xx 비율을 emit하거나, surge가 이체를 hot/control 클래스로 태깅해 status-class 버킷 emit. F18-P가 제안한 `transfer_2xx_rate`와 동일 계열. 현 banking surge는 이체를 not-5xx check로만 접어 status/계좌클래스별 비율을 안 낸다.

### 4-C. 배선 (재사용)
5. **db.lock parameter_contract**: `profiles.json` db.lock allowed_scenarios에 F22-P 추가 + scenario_parameters(engine=oracle, namespace=rca-testbed-banking, pod=testbed-oracle-0, schema=BANKING, table=accounts, key_column=id, key_value=**hot 계좌 id**, client_identifier=`rca-F22-P-oracle-pool-lock`, hold_seconds=600). db_lock_executor.py는 CONTRACTS 하드코딩(F01-P/F06-H)이 아닌 allowlist 경로로 검증하므로 코드 변경 없이 계약 등록만으로 수용(:26-30, :42-43 oracle 분기 그대로). client_identifier가 `rca-F22-P-...-lock` 규약(:35) 충족.

---

## 5. 헌장 부합성 평가 (한 문단)

F22-P는 fault-surface §FS-6의 순수 pool 고갈을 transfer 실코드 file:line으로 앵커링해(①② ✅) 헌장이 요구한 P3+P2 결합 깊이를 채웠고, KEEP 골든 F01-P와 **주입면은 같되 정답축(특정 row 대기 vs 커넥션 자원 고갈)을 분리**해 헌장 조건 ③(감별)을 설계로 완결했다. 그러나 가장 정직하게 드러난 것은 F22-P가 **F19-Q와 동종의 진짜 능력갭**이라는 점이다: injector(db.lock)는 F01-P에서 live-검증된 실물이지만, 단일 row-lock을 15 커넥션 자연 고갈로 전환하려면 **hot 계좌 집중·transfer-heavy·고동시성 banking 부하**가 필요한데 현 banking surge.js는 이체 10%·랜덤 계좌·건강상한 20rps라 구조적으로 그 강도를 못 낸다. 손쉬운 대안인 `DB_POOL_MAX` 하향은 헌장 §2-A "Class A 위장 금지"에 정면으로 걸리고(F03-P 함정과 동일) F22-P의 자연 고갈 정체성 자체를 무너뜨린다. 관측 면에서도 결정 증거 두 축(`hikari_pending_connections` transfer 게이지 + 무관 계좌 이체 성공률)이 모두 미배선이라 ③이 🟡에 걸린다. 결론: F22-P는 설계로는 F01-P와 명확히 구분되는 값진 전용 시나리오지만, **banking 부하 계약(transfer-heavy hot-account surge)** 이라는 인프라 갭이 선행돼야 승격 가능한, 정직하게 blocked인 설계다.
