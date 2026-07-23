# 설계 시트 — F17-R: banking transfer 다운 → commerce payment @Transactional 전체 롤백

- **제안 id**: `F17-R` (신규) · **slug**: `f17-r-banking-down-payment-rollback`
- **Class / 패턴**: Class A (코드) / **P7 크로스도메인 결합 + P1 트랜잭션 안 동기 원격호출** (헌장 §2-B, 백로그 #3)
- **도메인**: cross (commerce payment ↔ core-banking transfer) / 관측표면 APM(commerce-payment 5xx·cross-domain trace) + KCM(rca-testbed-banking pod NotReady)
- **RCA 난이도**: 높음 (fault-surface C4, namespace 경계 넘는 원인)
- **readiness**: `draft` / `prerequisite_gate.state=blocked` (배선 미완, §4)

---

## 1. 인과 사슬 (코드 실측 근거)

```
k8s.probe: testbed-transfer(rca-testbed-banking) readinessProbe path → /actuator/health/f17-r-fail (404)
  → 3×10s 후 pod NotReady (22-transfer-service.yaml:71-77 readiness periodSeconds10/failureThreshold3)
  → Service testbed-transfer 엔드포인트 0개 (22-transfer-service.yaml:93-103)
  → commerce payment.processPayment 진행: PG mock 결제 성공(200, transaction_id 발급·payments 행 save)
  → BankingTransferClient.transfer → bankingTransferRestClient POST /api/transfers → 연결 실패
  → catch(RestClientException) → throw ServiceException(BAD_GATEWAY 502)  ← CB/Retry 없음
  → processPayment @Transactional 롤백 → 방금 저장한 payments 행 + payment_logs 전부 소멸
  → order.checkout이 502로 감싸 전파 → 게이트웨이 502, checkout 실패
```

핵심 코드 앵커 (직접 검증 완료):
- `PaymentService.java:53-89` `processPayment`: `@Transactional`. line 59·71 payments save → line 63 `pgApiClient.requestPayment`(성공) → line 77 `bankingTransferClient.transfer` 호출. 이체 실패 시 메서드 전체 롤백(저장된 payment 행 포함) — fault-surface C4·구조결함 #2와 정확히 일치.
- `BankingTransferClient.java:37-59` `transfer`: `bankingTransferRestClient.post().uri("/api/transfers")...`, **CB/Retry 없음**. line 55-58 `catch (RestClientException) → throw ServiceException(HttpStatus.BAD_GATEWAY, ...)` — 롤백을 트리거하는 정확한 지점.
- `payment application.yml:46-50` — banking 클라 read 10s, **CB/Retry 미설정**(fault-surface §0 최약점).
- `22-transfer-service.yaml:4,24,28,71-77` — Deployment testbed-transfer / container transfer-service / port 8082 / readinessProbe(path /actuator/health, period10, timeout3, failureThreshold3) baseline.
- `22-transfer-service.yaml:93-103` — Service testbed-transfer(셀렉터 app=testbed-transfer) — NotReady 시 엔드포인트 제거로 payment 연결 실패 유발.

**readinessProbe 선택 근거**: livenessProbe 실패(F05-H식)는 pod를 죽여 CrashLoop → 재시작 폭풍·간헐 회복. readinessProbe 실패는 pod를 살려둔 채 **엔드포인트만 제거** → 이체 호출이 지속적·결정적으로 연결 실패 → 깨끗한 롤백 신호. cleanup도 probe 경로 복원만으로 재시작 없이 즉시 회복.

---

## 2. 감별 설계 (골든 조건 ③)

| 구분 | 관측 | 논리 |
|---|---|---|
| **must_support** | banking_transfer_ready(testbed-transfer)=false | 원인=타 namespace banking transfer 불가용 실재 |
| | payment_error_rate(commerce-payment) ≥ 0.1 + checkout_5xx_rate ≥ 0.05 | payment 롤백/5xx가 checkout까지 전파(502는 5xx라 기존 checkout_5xx_rate가 그대로 잡음) |
| | (trace/prose) PG 200 성공 span 직후 /api/transfers 연결실패로 끊김 | "PG는 성공찍혔다 소멸" — 정합성 파괴 흔적 |
| **must_rule_out** | payment_pod_ready(testbed-payment)=true | **commerce 내부 정상** — payment 자체 결함 아님(원인 크로스도메인 지목) |
| | entry_status ≠ 429 | 외부 rate-limit 오인 차단 |
| | banking_transfer_restart_count < 2 (증가 없음) | readiness-only 다운임을 확정, CrashLoop/OOM 대안 배제 |

**F01-P(oracle lock 크로스도메인)와의 결정적 감별점**:
- F01-P: transfer **살아있음**(pod ready) + Oracle blocking session(tagged) 존재 + payment가 **timeout으로 느리게** 실패.
- F17-R: transfer **다운**(pod NotReady) + Oracle lock **없음** + payment가 **연결실패로 빠르게** 502.
- 스칼라 감별자 = `banking_transfer_ready`(F17-R=false / F01-P=true) — 기존 `kubernetes.pod_ready` 쿼리 재사용, 신규 배선 불필요.

**오답 유도 구조**: 증상(payment 5xx·checkout 실패)이 commerce payment/order에 뜨므로 naive RCA는 commerce-payment를 root로 지목. 그러나 payment pod·DB·호스트는 정상이고, 실패 span의 마지막 hop이 core-banking /api/transfers 연결실패다. 정답=core-banking-transfer(타 도메인).

---

## 3. 골든 4조건 자체점검표

| 조건 | 판정 | 근거 |
|---|---|---|
| ① 코드/인프라 앵커 | ✅ | PaymentService.java:53-89(@Transactional PG-then-banking 롤백) + BankingTransferClient.java:55-58(RestClientException→502, CB 없음) + payment yml:46-50 + 22-transfer-service.yaml:71-77(readiness). P7+P1에 정확 매핑, fault-surface C4·구조결함 #2 직결. |
| ② 정답(answer-key) 작성 | ✅ | metadata에 F01-P와 동일 6필드 + root_cause{target_kind=service, id=core-banking-transfer, namespace, mechanism, code_anchor}. 실제 코드 거동(이체 실패→전체 롤백)과 일치. |
| ③ 감별 가능 | ✅ | must_support/must_rule_out 전부 **기존 query_id로 관측 가능**: banking_transfer_ready·payment_pod_ready(kubernetes.pod_ready), checkout_5xx_rate(502는 5xx라 그대로 포착 — F16-H의 401 사각과 달리 문제 없음), payment_error_rate. F01-P 감별도 pod_ready 스칼라로 결정적. **유일한 잔여**: "PG 성공분 소멸"의 정합성 아티팩트는 롤백으로 payment_logs까지 사라져 스칼라 쿼리가 없고 APM trace/prose 수준(F01-P의 distinguishing_evidence와 동급 처리). |
| ④ 주입 수단 실재 | 🟡 | k8s.probe executor 실재·KEEP검증(F05-H 동일 메커니즘). readinessProbe도 코드 지원(k8s_probe_executor.py:50). 파라미터(namespace/deployment/container/probe/baseline/fault)는 전량 passthrough. 그러나 `APPROVED_TARGETS`가 **F05-H만 하드코딩**(:11-13)이고 validate()가 미등록 시나리오를 거부(:45) → F17-R 타깃 등록 필요. **신규 executor 로직 아님, 계약·allowlist 확장.** |

**종합**: ①②③ 완전 충족(✅✅✅), ④만 조건부(🟡). F16-H 파일럿보다 한 단계 낫다 — F16-H는 401이 checkout_5xx_rate에 안 잡혀 관측 배선(surge.js 요약+query 3종)이 선행돼야 했으나, F17-R은 502가 기존 5xx 지표에 그대로 잡히고 감별자도 전부 기존 query라 ③이 ✅. 남은 건 순수 allowlist/계약 등록 4곳. 그래서 `readiness=draft`+`prerequisite_gate=blocked`(헌장 §2 "하나라도 빠지면 draft").

---

## 4. 능력 갭 (승격 전 배선 필요 — prerequisite_gate, 전부 등록 작업·신규 코드 0)

1. **k8s.probe allowlist 확장 (조건 ④)**: `profiles/k8s_probe_executor.py`
   - `APPROVED_TARGETS`에 `"F17-R": ("rca-testbed-banking","testbed-transfer","transfer-service","readinessProbe")` 추가.
   - (일관성) F05-H식 `F17_R_PARAMETERS` 상수 + validate() 정확일치 가드 추가 권장. readinessProbe는 :50에서 이미 허용.
   - `registry/profiles.json`의 `k8s.probe.parameter_contract.allowed_scenarios`에 `F17-R` 추가 + `scenario_parameters.F17-R`(manifest profile.levels[0].parameters와 동일 스냅샷).
2. **load.north_south 계약 확장 (companion)**: `registry/profiles.json`
   - `load.north_south.parameter_contract.allowed_scenarios` + `tag_pattern`(정규식)에 `F17-R` 추가 + `scenario_parameters.F17-R`(commerce surge.js, 35rps 권장, entry 30080).
3. **registry/catalog·scenario-metadata·controllers 등록**: catalog.json 엔트리, scenario-metadata.json에 본 metadata 반영, controllers.json 바인딩(primary=k8s.probe, companion=load.north_south).

### 4-A. 런타임 리스크 (별도 추적)
- 메모리 `live30-followups-0722` 기록: **load.north_south companion이 pod-교란 시나리오(F05-H·F08-P)와 결합 시 live.json 조기삭제→abort** 미해결 버그. F17-R도 banking pod를 교란(readiness)하며 commerce surge를 병행하므로 동일 lifecycle 버그에 노출될 수 있다. 다만 readinessProbe는 pod를 **죽이지 않아**(restart 없음) livenessProbe/eviction 계열보다 러너 lifecycle 자극이 약할 가능성 — live 활성화 전 이 조합의 실제 재현 여부 확인 필요.

---

## 5. 헌장 부합성 평가 (한 문단)

F17-R은 헌장이 요구한 "넓이+깊이"를 cross-domain 축에서 통과시킨 사례로, 백로그 #3(P7+P1)을 코드 file:line까지 박아 실현했다. 깊이 ①②는 PaymentService.java:53-89의 `@Transactional` 안 PG-then-banking 순차호출과 BankingTransferClient.java:55-58의 CB 없는 502 throw를 눈으로 확인해 완전 충족했고, ③은 F16-H 파일럿의 뼈아픈 교훈(401이 checkout_5xx_rate 사각)을 피해간다 — 롤백 원인이 502(=5xx)라 기존 checkout_5xx_rate·payment_error_rate가 그대로 잡고, F01-P와의 감별도 `banking_transfer_ready` 스칼라 하나로 결정적이라 관측 배선 신설이 필요 없다. 유일한 갭 ④는 "능력은 실재하는데 allowlist가 F05-H 전용"이라는, 파일럿과 동일한 계약-확장 문제이며 신규 executor 코드는 0이다. 가장 값진 발견은 **감별 대칭성**이다: 같은 cross-domain·같은 증상표면(payment 5xx→checkout 실패)을 공유하는 F01-P(lock=느린 실패, transfer 살아있음)와 F17-R(down=빠른 실패, transfer NotReady)이 `banking_transfer_ready`와 실패 지연 특성으로 깔끔히 갈라져, 카탈로그에 "downstream 느림 vs downstream 죽음"의 결정적 감별짝을 만든다. 결론: F17-R은 draft로 정직하게 게이트하되, 배선만 채우면 즉시 golden으로 승격 가능한 고품질 cross-domain 본보기다.
