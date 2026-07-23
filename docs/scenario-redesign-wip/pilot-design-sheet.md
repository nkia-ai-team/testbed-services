# 파일럿 설계 시트 — F16-H: user-service fail-close → commerce 전 도메인 쓰기 401

- **제안 id**: `F16-H` (신규, 카탈로그 최초 P6)
- **slug**: `f16-h-user-service-fail-close`
- **Class / 패턴**: Class A (코드) / **P6 Fail-close 인증 확산** (헌장 §2-B). 카탈로그에 P6 0개였음 → 백로그 #1.
- **도메인**: commerce / 관측표면 APM(게이트웨이·user) + KCM(pod NotReady)
- **RCA 난이도**: 높음 (fault-surface C3, 국소→광역 위장)

---

## 1. 인과 사슬 (코드 실측 근거)

```
k8s.probe: testbed-user readinessProbe path → /actuator/health/f16-h-fail (404)
  → 3×10s 후 pod NotReady
  → Service testbed-user 엔드포인트 0개 (25-user-service.yaml:95-105)
  → gateway RestClient(userRestClient) verify-token 연결 실패
  → @Retry(user) 3회 소진 → @CircuitBreaker(user) open (yml:51-57: window10/min5/50%/open5s)
  → verifyTokenFallback → throw ServiceException(UNAUTHORIZED)   ← fail-close
  → 모든 쓰기 라우트 401 (GatewayProxyController.java:28-71, 8개 지점)
```

핵심 코드 앵커 (직접 검증 완료):
- `AuthGuard.java:31-48` `verifyIfWrite`: WRITE_METHODS={POST,PUT,DELETE,PATCH}, EXEMPT={/api/users/register,/api/users/login}. 토큰 검증 실패 시 401.
- `AuthGuard.java:50-58` `verifyToken`: `@CircuitBreaker(name="user", fallbackMethod="verifyTokenFallback")` + `@Retry(name="user")`.
- `AuthGuard.java:60-63` `verifyTokenFallback`: **무조건 `throw ServiceException(HttpStatus.UNAUTHORIZED, ...)`** — fail-close의 정확한 지점.
- `GatewayProxyController.java:28,34,40,46,52,58,65,71` — 8개 프록시 라우트가 진입 즉시 `authGuard.verifyIfWrite(request)` 호출 → 하류 프록시 이전 단계에서 차단.
- `application.yml:51-57`(user CB), `:102-106`(user retry 3회).
- `25-user-service.yaml:4,24,28,73-79` — Deployment testbed-user / container user-service / port 8085 / readinessProbe baseline.

---

## 2. 감별 설계 (골든 조건 ③)

| 구분 | 관측 | 논리 |
|---|---|---|
| **must_support** | write_401_rate ≥ 0.3 | 쓰기(add-to-cart/clear-cart/checkout)만 401 급증 |
| | available_replicas(testbed-user)=0, pod NotReady | 국소 원인 실재 |
| | gateway CB{user}=open + commerce-gateway error rate↑ | fail-close 트리거 확인 |
| **must_rule_out** | read_2xx_rate ≥ 0.9 (GET 정상) | **결정적 감별점** — fail-close는 쓰기만 막음. 읽기까지 깨지면 광역 아웃티지(별 시나리오) |
| | entry_status ≠ 429 | 외부 rate-limit 오인 차단 |
| | 401(4xx)이지 5xx 아님 | DB풀 고갈(C12)·결제지연(C1)·5xx 계열과 구분 |

**오답 유도 구조**: 증상이 order/cart/checkout 전반에 퍼져 naive RCA는 그 서비스들을 root로 지목. 그러나 401은 게이트웨이 AuthGuard에서 하류 프록시 *이전에* 던져지므로 order/cart/payment 하류에는 해당 요청 trace가 아예 없다(하류 정상). 정답=user-service.

---

## 3. 골든 4조건 자체점검표

| 조건 | 판정 | 근거 |
|---|---|---|
| ① 코드/인프라 앵커 | ✅ | AuthGuard.java:31-63 (fail-close throw 지점 실측) + GatewayProxyController.java:28-71 + application.yml:51-57. P6에 정확 매핑. |
| ② 정답(answer-key) 작성 | ✅ | pilot-metadata.json에 root_cause{target_kind=service, id=commerce-user, mechanism, code_anchor}. 코드 거동과 일치(verifyTokenFallback→401). |
| ③ 감별 가능 | 🟡 | 감별 *설계*는 완결(§2: write-401 vs read-200, 401 vs 5xx). 그러나 이를 관측할 **query_id 3종이 미존재**(§4) → 현재 관측 표면으로는 결정 불가. 설계 ✅ / 관측 배선 ❌. |
| ④ 주입 수단 실재 | 🟡 | executor는 실재·검증됨(k8s.probe, F05-H가 동일 메커니즘으로 KEEP). 그러나 **parameter_contract가 F05-H(payment)만 허용** → testbed-user/readinessProbe로 확장 필요. 코드 신설 아님, 계약 확장. |

**종합**: 2조건 완전 충족(✅✅), 2조건 조건부(🟡🟡) — 전부 "신규 executor/코드 없이 계약·관측 배선 확장"으로 승격 가능. 그래서 manifest `readiness=draft`, `prerequisite_gate.state=blocked`.

---

## 4. 능력 갭 (승격 전 배선 필요 — prerequisite_gate)

1. **주입 계약 확장 (조건 ④)**: `registry/profiles.json`
   - `k8s.probe.parameter_contract.allowed_scenarios`에 `F16-H` 추가 + `scenario_parameters.F16-H`(namespace=rca-testbed-commerce, deployment=testbed-user, container=user-service, probe=readinessProbe, baseline/fault). executor validate()가 `params == scenario_parameters[id]` 완전일치를 요구하므로 정확 등록 필요.
   - `k8s_probe_executor.py`가 `probe: readinessProbe`를 받는지 확인(현재 F05-H는 livenessProbe). 파라미터화돼 있으면 코드 무변경.
   - `load.north_south.parameter_contract`(allowed_scenarios/tag_pattern)에 `F16-H` 추가 + scenario_parameters(commerce surge.js, 35rps 권장).
2. **관측 query_id 3종 신설 (조건 ③ — 진짜 병목)**:
   - `loadgen.write_step_status_rate` / `loadgen.read_step_status_rate` — surge.js가 이미 step 태그(login/browse/cart/clear-cart/add-to-cart/checkout)를 달고 있으나(surge.js:75,92,102,119,124,129), **요약 지표가 status로 버킷팅 안 됨**. 결정적으로 `loadgen.checkout_5xx_rate`는 **401을 못 잡는다**(401<500, `checkout not 5xx` 체크를 통과). → surge.js 요약에 step-class×status 비율 emit + query_id 등록 필요.
   - `prometheus.gateway_circuitbreaker_open{cb=user}` — resilience4j CB 상태 메트릭 노출/쿼리 등록.
3. **readiness 선택 근거**: livenessProbe 실패(F05-H식)는 CrashLoop 주기(75s)로 *간헐* 불가용 → CB가 열렸다 닫혔다 반복. **readinessProbe 실패는 pod를 죽이지 않고 엔드포인트만 제거** → 지속적 verify-token 실패 → CB 지속 open → 깨끗하고 결정적인 fail-close. cleanup도 재시작 폭풍 없이 probe 경로 복원만으로 즉시 회복.

---

## 5. 헌장 부합성 평가

이 파일럿은 헌장이 요구한 "넓이+깊이"를 실제로 통과시켜 본 첫 사례로서 잘 나왔다. 깊이 조건 ①②는 코드 file:line까지 실측해 완전 충족했고(fail-close throw 지점 AuthGuard.java:61-62를 눈으로 확인), ③④는 "능력은 있으나 배선이 없다"는 상태를 정직하게 🟡로 분리해 `readiness=draft`+`prerequisite_gate=blocked`로 표기했다 — 헌장 §2가 "하나라도 빠지면 draft"라 한 규칙을 그대로 이행한 셈이다. 특히 가장 값진 파일럿 발견은 **주입은 되는데 관측이 안 되는 비대칭**이다: injector(k8s.probe)는 KEEP-검증된 실물이라 계약 한 줄로 확장되지만, 정작 이 시나리오의 정체성인 "write-401 vs read-200" 감별은 기존 `checkout_5xx_rate`가 401을 구조적으로 못 봐서 관측 배선(surge.js 요약 + query_id 3종)이 선행돼야 한다. 헌장의 4조건 루브릭이 없었다면 이 갭은 "그럴듯한 manifest"에 묻혀 얕은 채로 승격됐을 것이다. 결론: 헌장은 의도대로 굴러가며, 이 F16-H는 재설계의 본보기 기준선으로 삼을 만하다.
