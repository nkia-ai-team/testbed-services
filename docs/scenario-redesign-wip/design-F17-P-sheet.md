# 설계 시트 — F17-P: commerce→transfer 직행 무결성 우회 (FROZEN 계좌 검사 스킵)

- **제안 id**: `F17-P` (신규, 카탈로그 최초 P7 무결성 위반)
- **slug**: `f17-p-transfer-direct-integrity-bypass`
- **Class / 패턴**: Class A (코드) / **P7 크로스도메인 결합·무결성** (헌장 §2-B). 백로그 #2.
- **도메인**: cross (commerce + core-banking) / 관측표면 APM(transfer·account 트레이스) + DPM(Oracle 데이터 정합)
- **RCA 난이도**: 높음 — 5xx·지연 0, "성공했는데 틀린" 상태. 이상감지가 아예 안 뜰 수 있음.
- **근거**: fault-surface-core-banking.md FS-5.

---

## 1. 인과 사슬 (코드 실측)

```
commerce-payment ──HTTP POST──▶ testbed-transfer-external NodePort 30282 (k8s/22:105-120)
   │  (api·account 우회 — 검증 단계 통째로 스킵)
   ▼
TransferService.execute()  @Transactional  (TransferService.java:51)
   ├─ amount>0 ?           (:53-55)   ✅ 통과
   ├─ from≠to ?            (:56-58)   ✅ 통과
   ├─ 계좌 FOR UPDATE lock (:68-71)   ✅ ACC-9001 존재
   ├─ ❌ status(FROZEN/CLOSED) 검사  ── 없음 ──   ← 결함 지점
   ├─ from.balance ≥ amount ? (:77)  ✅ ACC-9001 잔액 100000 ≥ 소액
   ├─ 잔액 차감/증가 save    (:82-85)
   ├─ status = "COMPLETED"  (:86)     ← 200 성공 확정
   └─ eventPublisher.publish(:100-104) → 원장 이벤트까지 발행 (부정합 전파)

대조(정상경로):  30082 nginx → api → account.validateAndForward (AccountService.java:57-81)
   └─ from/to ACTIVE 검사 (:66-71) → FROZEN → throw 400 "From account is FROZEN"  ← 여기서만 막힘
```

핵심 앵커 (직접 검증 완료):
- `TransferService.java:52-89` — execute 검증부. amount/from≠to/존재/balance만. **status 검사 부재**가 눈으로 확인됨(:77 balance 다음 바로 :82 차감·:86 COMPLETED).
- `AccountService.java:66-71` — `if (!"ACTIVE".equalsIgnoreCase(from.getStatus())) throw 400`. ACTIVE 검증이 **오직 여기(정상경로)에만** 존재.
- `k8s/22-transfer-service.yaml:105-120` — testbed-transfer-external, type NodePort 30282, selector app=testbed-transfer. 주석: "banking nginx를 타지 않고 직접" = account 우회 진입점.
- `db/init.sql:91-92` — `ACC-9001 FROZEN 잔액 100000` / `ACC-9002 CLOSED 잔액 0` 시드 상수. **이미 존재** = 시딩 injector 불필요.
- 대조 데이터: `core-banking/loadgen/surge.js:66` — baseline은 ACC-9001/9002를 **일부러 제외**(ACTIVE_ACCOUNTS만) → 평시엔 이 위반이 안 생기므로 clean baseline 보장.

---

## 2. 감별 설계 (골든 조건 ③)

| 구분 | 관측 | 논리 |
|---|---|---|
| **must_support** | frozen_bypass_completed_rate > 0 | 직행(30282) FROZEN 이체가 200 COMPLETED |
| | integrity_violation_count > 0 | FROZEN 계좌 당사자 COMPLETED transfers 행 실재(데이터 ground-truth) |
| | target_5xx_rate 정상 | 위반이 **에러 없이** 성공 |
| **must_rule_out** | normal_path_frozen_reject_rate ≥ 0.9 | **결정적 감별점** — 동일 payload가 정상경로(30082)에선 400 거절. 경로 비대칭 = 정답 서명 |
| | target_5xx_rate < 0.05 | 가용성 장애(FS-1 lock·502 계열) 아님 — 상태 위반이지 5xx 아님 |
| | ACC-9001 잔액 100000 충분 | 잔액부족(4xx)·미존재 거절 아님 |

**오답 유도 구조**: 5xx·지연이 0이라 에러율/latency 기반 이상감지가 **아예 안 뜬다**. naive RCA는 "정상 운영"으로 오판. 진짜 증거는 (a) 데이터 정합(FROZEN 계좌의 COMPLETED transfer 존재)과 (b) 경로 비대칭(direct 200 vs normal 400)뿐 — 둘 다 기존 관측표면에 없어서 능동 배선이 선행돼야 잡힌다.

---

## 3. 골든 4조건 자체점검표

| 조건 | 판정 | 근거 |
|---|---|---|
| ① 코드/인프라 앵커 | ✅ | TransferService.java:52-89(status 미검사, 실측) vs AccountService.java:66-71(ACTIVE 검사) + k8s/22:105-120(30282 직행) + init.sql:91-92(FROZEN 시드). P7 정확 매핑. |
| ② 정답(answer-key) 작성 | ✅ | design-F17-P-metadata.json에 root_cause{target_kind=service, target_id=banking-transfer, mechanism=검증 비대칭, code_anchor 4종}. 코드 거동과 일치(FROZEN from→balance 통과→COMPLETED). |
| ③ 감별 가능 | 🟡 | 감별 *설계*는 완결(§2: direct-200 vs normal-400, integrity count). 그러나 관측 **query_id 3종 미존재** + 5xx가 없어 기존 이상감지 표면으로는 결정 불가. 설계 ✅ / 관측 배선 ❌. F16-H보다 심함(5xx 부재로 anomaly 자체가 안 뜸). |
| ④ 주입 수단 실재 | 🟡 | FROZEN/CLOSED 계좌는 seed(ACC-9001/9002)에 **이미 존재** → 시딩 injector 불필요 ✅. 그러나 **30282 직행 + FROZEN-타깃 이체를 흘리는 injector 부재** ❌: load.north_south의 allowed_entry_urls={30080,30181,30082}에 30282 없음, 기존 surge.js는 FROZEN을 일부러 제외. 새 executor는 아니고 계약 확장 + dual-arm 스크립트 신설. |

**종합**: 2조건 완전 충족(✅✅), 2조건 조건부(🟡🟡). readiness=draft, prerequisite_gate.state=blocked, live_allowed=false. 헌장 §2 "하나라도 빠지면 draft" 이행.

---

## 4. 능력 갭 (승격 전 배선 필요 — prerequisite_gate)

1. **주입 배관 (조건 ④)**: `registry/profiles.json` load.north_south
   - `parameter_contract.allowed_entry_urls`에 `http://192.168.122.77:30282`(직행) 추가. 현재 3개(30080/30181/30082)만 허용, executor가 `entry_url not in allowed_entry_urls`면 `ExecutorError`(load_north_south_executor.py:46). **30282는 domain_profiles에도 없음** → 신규 domain_profile 항목 필요(baseline_unit=loadgen-banking, health_path).
   - `allowed_script_paths`에 `/opt/loadgen/core-banking/frozen-bypass.js` 추가 + `allowed_scenarios`/`tag_pattern`에 F17-P 등록 + `scenario_parameters.F17-P`.
   - **신규 스크립트 `frozen-bypass.js`(dual-arm)**: (a) direct arm — 30282로 fromAccount=ACC-9001(FROZEN)·toAccount=ACC-1001 POST, 각 요청 `path_class=direct` 태그 + body의 `status==COMPLETED` assert; (b) control arm — 동일 payload를 30082로 POST, `path_class=normal`, 400 기대. 기존 surge.js는 FROZEN 제외라 재사용 불가 → 신설.
   - *대안*: load.north_south 확장 대신 전용 one-shot HTTP injector 프로파일 신설도 가능(경로 2개·상태 assert가 north_south 계약과 안 맞으면). 설계 판단 필요.
2. **관측 query_id 3종 신설 (조건 ③ — 진짜 병목)**:
   - `loadgen.frozen_bypass_completed_rate` — direct arm의 200∧COMPLETED 비율. 기존 `loadgen.checkout_5xx_rate`·`achieved_rps`는 성공을 못 세고 5xx만 봄 → 무결성 위반에 **구조적으로 눈멀음**.
   - `loadgen.normal_path_reject_rate` — control arm 400 비율. 경로 비대칭 증거.
   - `database.integrity_violation_count` — Oracle에 `SELECT count(*) FROM transfers JOIN accounts ... WHERE status IN('FROZEN','CLOSED') AND transfer.status='COMPLETED'`. 5xx가 없으니 **유일한 ground-truth 증인**. database 어댑터에 신규 등록(기존 database.tagged_session_count/index_present 패턴 따라).
3. **readiness 선택 근거**: `live_enabled=false`/`live_allowed=false`로 시작 — mutation(부정합 기입)이 실제 원장 데이터를 오염시키므로 cleanup(정정 or 마킹) 설계가 확정될 때까지 live 금지. capture는 dry-run/plan 경로로 계약·관측 검증부터.

---

## 5. 배관 gap 요약 (한눈)

| 항목 | 상태 | 조치 |
|---|---|---|
| FROZEN/CLOSED 계좌 seed | ✅ 있음(ACC-9001/9002, init.sql:91-92) | 없음 |
| 30282 직행 진입점(인프라) | ✅ 있음(k8s/22 NodePort) | 없음 |
| 30282 entry_url 허용 | ❌ 계약 미등록 | allowed_entry_urls + domain_profiles 추가 |
| FROZEN-타깃 dual-arm 스크립트 | ❌ 없음(기존 surge는 FROZEN 제외) | frozen-bypass.js 신설 |
| direct-200-COMPLETED 관측 | ❌ query 없음 | loadgen.frozen_bypass_completed_rate |
| normal-400 대조 관측 | ❌ query 없음 | loadgen.normal_path_reject_rate |
| 데이터 정합 위반 witness | ❌ query 없음 | database.integrity_violation_count |
| live mutation 안전(정정) | ❌ 미설계 | cleanup 정정 설계 → 이후 live 승격 |

---

## 6. 헌장 부합성 평가

F17-P는 헌장이 노린 "깊이 4조건"을 P7·무결성 축에서 통과시킨 두 번째 본보기다. ①②는 코드 file:line까지 실측해 완전 충족했고(status 미검사 지점 TransferService.java:77→86, ACTIVE 검사 AccountService.java:66-71을 눈으로 대조), ③④는 F16-H와 동형의 "주입은 되는데 관측이 안 되는 비대칭"을 정직하게 🟡로 분리했다. 다만 이 시나리오는 F16-H보다 한 겹 더 어렵다: F16-H는 401(4xx)이라도 상태코드가 바뀌지만, F17-P는 **5xx·지연·상태코드 변화가 전혀 없는 200 성공** — 에러율 기반 이상감지가 원리적으로 안 뜨므로, 정답 증거가 오직 "데이터 정합 + 경로 비대칭" 두 능동 신호에만 존재한다. 가장 값진 발견은 seed에 이미 FROZEN/CLOSED 계좌가 있고(주입 데이터 확보) 30282 인프라도 실재하지만, 정작 그 둘을 잇는 "FROZEN을 직행으로 흘리는 부하 + 성공을 세는 관측"이 통째로 비어 있다는 점이다 — 4조건 루브릭이 없었다면 "그럴듯한 200 정상"에 묻혀 장애로 인지조차 안 됐을 것이다. 결론: readiness=draft가 정직한 판정이며, 배관 7항(§5) 해소 시 카탈로그 유일의 "에러 없는 무결성 위반" 골든이 된다.
