# Class B 재라벨 감사 시트 (F05·F09·F10·F13 = 12개)

작성 2026-07-23. 근거: 헌장 §1/§2, 12 manifest, registry/{profiles,controllers,scenario-metadata,catalog}.json,
profiles/*_executor.py (실 executor), profiles/profile-executor.sh (inert plan-only boundary).

## 핵심 사실 (executor 실재 판정 근거)
- 실 injection은 `profiles/<name>_executor.py`가 수행. `<name>.sh`는 profile-executor.sh로 가는 **inert plan-only**(side_effects:false) 래퍼.
- `host_stress_executor.py` CONTRACTS: **F10-R=watermark, F10-H=fio, F10-P=fio 실재**(구체 PVC target_dir 박힘) + F05-P memhog·F09-R cpu 실재.
- `network_fault_executor.py` = **fail-closed**: F13-H "WPM probe source host/interface unresolved"로 BLOCKED.
- `business_fault_executor.py` = **fail-closed**: F13-P "banking exposes no bounded large-history/seed contract"로 BLOCKED.
- `wpm.probe` = wpm-probe.sh(inert). 실 WPM/TTFB probe executor 없음(network_external_probe_executor.py는 F12-G 전용).
- scenario-metadata: F05·F09 descriptive(cause/distinguishing_evidence)만, **구조적 root_cause(target_kind/id/mechanism) 없음**. F10·F13은 **metadata 자체 부재**.
- catalog/metadata에 class 필드 없음 → "Class B 라벨"은 신규 필드 추가 결정.

## 4조건 (Class B): ①인프라앵커(지점+조작수단) ②answer-key(자원/노드 root) ③감별 ④injector 실재

| id | 도메인 | ①앵커 | ②answer-key | ③감별 | ④injector | 실현성 |
|---|---|---|---|---|---|---|
| F05-R | KCM | ✅ payment mem limit patch(768→576Mi) | 🟡 desc有/구조無 = pod testbed-payment, OOMKilled | ✅ non-oom/liveness/overload rule-out | ✅ k8s_resource_executor, live-proven | **주입가능** |
| F05-H | KCM | ✅ liveness probe path→fail endpoint | 🟡 = testbed-payment liveness-fail→Error/CrashLoop | ✅ OOMKilled+resource-change rule-out (vs F05-R) | ✅ k8s_probe_executor, live | **주입가능** |
| F05-P | KCM | ✅ host memhog ladder tb-w2 kubelet eviction, cohort guard | 🟡 = node tb-w2 mem pressure→pod eviction | ✅ impact-without-node-pressure rule-out | ✅ host_stress memhog, live | **주입가능** |
| F09-R | SMS | ✅ host cpu busy-loop ladder tb-w3(.14) | 🟡 = node tb-w3 CPU sat→co-located svc p95 | ✅ symptom-without-node-load rule-out | ✅ host_stress cpu, live | **주입가능** |
| F09-H | SMS | ✅ k8s.env JAVA_TOOL_OPTIONS -Xmx256/208/176m (§2-A: env=자원제약, Class A 아님) | 🟡 = order-service heap under-provision→GC pause | 🟡 crossed-into-oom rule-out만(약) | ✅ k8s_env_executor, live | **주입가능** |
| F09-P | SMS | ✅ k8s.patch cpu limit 500→50m CFS throttle | 🟡 = inventory-service CPU-limit throttle→gateway p95 | 🟡 overload(rps>65) rule-out | ✅ k8s_patch_executor, live | **주입가능** |
| F10-R | SMS·DPM-IO | ✅ watermark fill pgdata PVC tb-w1(.184) 85%/reserve10Gi | ❌ metadata 부재 = postgres PVC/tb-w1 disk-full→write fail | ❌ blocked stub(관측/success/rule-out 미정의) | ✅ host_stress watermark 실재 | **주입가능·manifest 미승격** |
| F10-H | SMS·DPM-IO | ✅ fio food-mysql PVC | ❌ 부재 = mysql PVC disk-io sat | ❌ blocked stub | ✅ fio 실재 | **주입가능·좌표충돌·미승격** |
| F10-P | SMS·DPM-IO | ✅ fio banking-oracle PVC | ❌ 부재 = oracle PVC disk-io sat | ❌ blocked stub | ✅ fio 실재 | **주입가능·좌표충돌·미승격** |
| F13-H | NMS·WPM | ❌ 지점 unresolved | ❌ 부재 | ❌ blocked | ❌ network.fault fail-closed + wpm inert | **능력갭(별도 인프라 트랙)** |
| F13-P | NMS·WPM | ❌ seed/endpoint 없음 | ❌ 부재 | ❌ blocked | ❌ business.fault fail-closed + wpm inert | **능력갭(데이터시딩+엔드포인트+WPM)** |
| F13-R | NMS·WPM | 🟡 k8s.patch ingress(주입면 가능) | ❌ 부재 | ❌ blocked | 🟡 k8s.patch 실재하나 WPM TTFB 관측 inert | **능력갭(WPM probe 부재)** |

## 좌표 오류 (실 executor host vs manifest location vs 도메인맵 w1/.184=commerce·w2/.11=food·w3/.14=banking)
- **F10-H**: host_stress CONTRACTS host=`192.168.122.14`(tb-w3) + target_dir=food_mysqldata / manifest location=`worker-w2` / 도메인 food=w2(.11). → **executor .14 vs manifest w2 vs map .11 삼중 불일치**. local-path PVC는 node-pinned이므로 실제 mysql-0 배치 노드로 3자 정합 필요.
- **F10-P**: host_stress host=`192.168.122.184`(tb-w1) + target_dir=banking_oracledata / manifest location=`worker-w3` / 도메인 banking=w3(.14). → **executor .184 vs manifest w3 vs map .14 삼중 불일치**. 실제 oracle-0 배치 노드로 정합 필요.
- F10-R은 정합(.184=w1=commerce=postgres, manifest worker-w1 ✓).

## 부수 registry 결함
- profiles.json `host.stress.parameter_contract.allowed_scenarios` = `[F09-R,F05-P]`만 → 실 host_stress_executor.py가 지원하는 **F02-H,F10-R,F10-H,F10-P,F15-P 누락**(stale). F10 승격 시 allowlist 확장 필요.
- F09-P/F13-R가 `k8s.patch.allowed_scenarios`(현 [F09-P,F12-H])에 — F09-P는 있음, F13-R 없음.

## 라벨 확정 (전 12개 Class B)
F05-R/H/P=B(KCM), F09-R/H/P=B(SMS), F10-R/H/P=B(SMS·DPM-IO), F13-H/P/R=B(NMS·WPM).
- F09-H는 §2-A 준수: env -Xmx는 "인프라 자원제약(heap under-provision)"으로 B, Class A 위장 아님.
