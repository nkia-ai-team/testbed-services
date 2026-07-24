# 설계 시트 — F27-R / F27-P: 네트워크 Class B 2종

- **클래스**: 둘 다 **Class B (인프라)** — 헌장 §2-A. 정답이 앱 코드가 아니라 **자원/링크/네트워크 정책**.
- **근거 문서**: `fault-surface-infra-network-wpm.md` (§0 토폴로지 실측 = VM 이중 NIC / vnet 매핑, §1 주입점 3종, §1.2/§3.N3 kube-router NetworkPolicy 강제 실측, §1.4 **NMS 관측 불가 판정**).
- **재검증(이 세션 레포 실측)**:
  - NetworkPolicy 오브젝트 = 레포에 **0개** (`grep -rln "kind: NetworkPolicy"` 무매치) → F27-P가 도입하는 deny 정책은 기존과 충돌 없음, 최초 도입.
  - commerce→banking 앱경로 = **ClusterDNS** `http://testbed-transfer.rca-testbed-banking.svc.cluster.local:8082` (`commerce/payment-service/.../application.yml:48`, `BANKING_TRANSFER_URL`). NodePort 30282 직행 아님 → pod-to-pod, netpol 강제 대상. 문서 §0.2 앵커와 일치 재확인.
- **핵심 제약 (그대로 반영)**: 문서 §1.4 — SNMP 소스 0(모든 VM 161 닫힘), NetFlow/물리스위치 없음 → **NMS를 must_support ground-truth로 쓰는 골든 금지**. 감별은 APM(균일 지연)+KCM(throttle=0/pod ready)+SMS(CPU 정상)의 **소거법**으로만.
- **안전 요건 (self-blinding 함정)**: 문서 §1.1 — 관측 평면(br0 / vnet6~10)은 **절대 건드리지 않는다**. F27-R tc 주입은 앱 평면 vnet(1/2/3)에만, executor allowlist가 관측 NIC를 물리적으로 거부해야 함(§5).

---

## 1. 인과 사슬 (2개, 인프라 실측 앵커)

### F27-R — 워커 앱-평면 네트워크 지연 (F12-H의 정직한 역방향 쌍)
```
[GB10 host root: tc qdisc add dev vnet1 root netem delay 200ms 20ms]  ← 주입수단 신설(§5)
  vnet1 = tb-w1(commerce)의 앱-평면 NIC(virbr0 멤버)  ← 문서 §0.1 매핑
  → tb-w1로 들어가는 모든 트래픽이 균일 지연 (남북 NodePort 진입 + 동서 pod-to-pod 둘 다)
  → commerce 전 서비스(order/product/inventory/payment...)의 동기 hop 체인이
     netem 지연을 누적 → order→product→inventory→payment 각 구간 +200ms
  → 체인 길이만큼 latency 증폭, 하류 풀/스레드 압박, 일부 timeout
  → APM: commerce 전 서비스 p95 균일 상승 (단일 서비스 아님)
     KCM: container CPU throttle = 0 (자원 정상)
     SMS: host CPU 정상 (연산 부하 아님)
  → "네트워크다"를 NMS 양성확인 없이 소거법으로 추정
```
앵커: host `vnet1` egress qdisc (virbr0 멤버, 문서 §0.1 `virsh domiflist`/`brctl show` 실측). 주입=`tc qdisc add dev vnet1 root netem delay`, 복구=`tc qdisc del dev vnet1 root`. NET_ADMIN=GB10 호스트 root 실재(문서 §3.N1).
**정체성**: 균일성(uniformity). 진짜 네트워크 지연은 그 워커 위 **모든 pod**를 똑같이 늦춘다 — 특정 서비스 하나만 늦는 F12-H(CPU throttle)와 정반대. 문서 §3.N1의 "F12-H 정직한 쌍둥이 / F12-R(interface-down, blocked) realizable 대체".

### F27-P — 크로스도메인 NetworkPolicy 파티션 (kube-router deny, 즉시 realizable)
```
[kubectl apply: rca-testbed-banking에 testbed-transfer ingress deny-from-commerce 정책]
  kube-router가 KUBE-ROUTER-FORWARD 체인으로 netpol 강제 (문서 §0.2, 4.2M pkts 실측)
  → commerce-payment(rca-testbed-commerce)의 SYN이 testbed-transfer(:8082)로 못 감(DROP)
     단, banking 내부 caller·kubelet probe는 통과(정책이 banking 네임스페이스는 allow)
  → payment.processPayment이 PG mock 결제 성공(200) 직후 BankingTransferClient 이체 호출
     → connect timeout (SYN silent drop = 응답 없음, refuse 아님)
  → BankingTransferClient는 CB 없음 → RestClientException → ServiceException(502)
  → @Transactional 전체 롤백 → checkout 502
  → APM: commerce payment→banking transfer cross-domain edge **소실**
     KCM: banking testbed-transfer pod 전부 **ready=true** (다운 아님)
     banking 자체 probe·banking 내부 caller는 **정상** (신호 없음)
```
앵커: kube-router netpol 강제(문서 §0.2 FORWARD 체인 실측). 주입=`kubectl apply` deny 정책, 복구=`kubectl delete`. tc로 불가능한 "특정 쌍만" 파티션의 **유일 실재 수단**(문서 §1.2). commerce→banking 경로=ClusterDNS(위 재검증).
**정체성**: root가 앱이 아니라 **네트워크 정책**. 증상은 C4(banking 앱 다운)·F17-R(transfer readiness 실패)과 동일 계열(payment 롤백·checkout 502)이나, **banking pod 전부 ready + banking 자체 무신호 + commerce에서만 connect timeout**이 감별 정체성. 문서 §3.N3의 "F12-P(cross-domain packet-loss, CNI/NET_ADMIN 미해결 blocked) realizable 대체".

---

## 2. 감별 설계 (골든 조건 ③)

| | must_support | must_rule_out (감별점) |
|---|---|---|
| **F27-R** | commerce **다중** 서비스 p95 균일 상승(order·product·inventory 동시); checkout latency↑; 지연이 서비스 경계 무관하게 워커 전역 | **KCM container throttle = 0** → throttle>0이면 F12-H(CPU) · 단일 서비스만 상승 아님(균일성) → 특정 svc면 앱 병목 · **SMS host CPU 정상** → 연산부하 아님 · entry_status ≠ 429 |
| **F27-P** | payment **502**↑ + checkout 502↑; commerce→banking cross-domain edge **소실/실패**; connect-단계 실패(read timeout 아님) | **banking testbed-transfer pod_ready = true** → false면 F17-R(transfer 다운)·C4 · banking 자체 error_rate 무증가 → banking 앱 정상 · Oracle blocking session 없음 → F01-P 아님 · entry_status ≠ 429 |

**F27-R ↔ F12-H 감별 골든 (상호 must_rule_out 쌍)**: 둘 다 "commerce 서비스가 느리다/timeout"으로 보이나 —
- **F12-H**: product **단일** 서비스만 지연 + product container **CPU throttle > 0** + interface/pod-network 정상. 원인=CPU 포화.
- **F27-R**: commerce **전** 서비스 균일 지연 + 모든 container throttle **= 0** + host CPU 정상. 원인=워커 링크 지연.
→ 결정 신호 = **(a) 지연의 균일성(전역 vs 단일)** + **(b) container throttle(=0 vs >0)**. 이 둘이 F12-H metadata의 "container throttling과 service trace 지연은 있으나 interface/pod network error 정상"을 정직하게 뒤집는다. 두 시나리오를 `related_scenarios.mutual_rule_out`으로 명시.

**F27-P ↔ F17-R 감별 (near-twin, 결정적 1축)**: 둘 다 commerce payment→banking 실패·payment 롤백·checkout 502. 갈림 =
- **F17-R**: transfer readinessProbe 실패 → Service 엔드포인트 0 → `banking_transfer_ready = FALSE`, connect가 **빠르게** 실패(엔드포인트 없음).
- **F27-P**: netpol이 패킷만 drop → pod·엔드포인트 **살아있음** → `banking_transfer_ready = TRUE`, SYN silent-drop이라 connect가 **timeout(느리게)** 실패.
→ 결정 신호 = **banking_transfer pod_ready(true=netpol / false=다운)** + **connect 실패 양태(timeout=drop / refuse·fast=엔드포인트 제거)**.

**오답 유도**: F27-R은 균일 지연을 보고 naive RCA가 "가장 느린 서비스"를 root로 지목(실은 워커 전역). F27-P는 payment 502를 보고 payment 앱 결함 또는 banking 다운으로 오판(실은 네트워크 정책, banking은 건강).

---

## 3. 골든 4조건 자체점검표

| 조건 | F27-R | F27-P |
|---|---|---|
| ① 코드/인프라 앵커 | ✅ host vnet1(virbr0, tb-w1 앱평면) qdisc — 문서 §0.1 `virsh domiflist`/`brctl show` 실측. tc netem delay 수단 | ✅ kube-router netpol 강제(FORWARD 체인 4.2M pkts 실측) + commerce→banking ClusterDNS 경로(payment yml:48 재검증). deny 정책 표면 |
| ② 정답(answer-key) | ✅ root_cause{target_kind=network-link, id=tb-w1/vnet1, mechanism=host netem delay}. infra_anchor=vnet1. **NMS 신호 없음 정직 표기** | ✅ root_cause{target_kind=network-policy, id=deny-commerce-to-transfer, mechanism=kube-router ingress drop}. code_anchor는 증상측(BankingTransferClient CB 부재)만 참고, root는 정책 |
| ③ 감별 가능 | 🟡 설계 완결(F12-H 상호 rule_out: 균일성+throttle=0). query 미배선: `prometheus.apm_service_p95`(서비스별), `kubernetes.container_cpu_throttle_rate`(KCM) 필요 | 🟡 설계 완결(F17-R rule_out: pod_ready true + connect timeout). query 미배선: cross-domain edge 소실 지표·connect-phase 실패 구분 |
| ④ 주입 수단 실재 | ❌ **host tc executor 미존재**(문서 §1.3 network.fault=BLOCKED 껍데기). 신설 필요 + 관측평면 allowlist 안전장치(§5) | 🟡 kube-router 강제=실재. deny 정책 manifest + **k8s.netpol 프로파일 신설**(신규 executor 아님, kubectl apply/delete 배선). 계약 등록 필요 |
| **종합** | ①②✅ ③🟡 ④❌ → **draft/blocked** (진짜 능력갭=host tc executor) | ①②✅ ③④🟡 → **draft/blocked** (배선=k8s.netpol 프로파일·정책 manifest) |

둘 다 `readiness=draft`, `prerequisite_gate.state=blocked`, `live_allowed=false`. **F27-P는 배선(k8s.netpol 프로파일+정책 manifest)만으로 승격 가능**, **F27-R은 진짜 능력갭(host-side tc executor 신설)**이 선행돼야 하는 다른 부류.

---

## 4. observation_domains 정직 기입 (NMS 불가 반영)

| | 자연 도메인 | 실제 신호 도메인 | NMS 표기 |
|---|---|---|---|
| F27-R | NMS(네트워크) | **APM**(균일 p95)+**KCM**(throttle=0)+**SMS**(CPU 정상) | ❌ 소거법 추정만, ground-truth 불가 |
| F27-P | NMS(네트워크) | **APM**(edge 소실)+**KCM**(banking pod ready) | ❌ 소거법 추정만, ground-truth 불가 |

metadata `observation_domains`에 NMS를 **넣지 않고** 실제 신호 도메인만 기입. 시나리오가 "네트워크"임에도 NMS가 빠지는 이 비대칭이 문서 §1.4 판정의 정직한 귀결이다.

---

## 5. 능력 갭 (승격 전 선행 — prerequisite_gate)

### 5-A. F27-R 전용 (진짜 능력갭 — host tc executor 신설)
현재 `network.fault`(`profiles/network-fault.sh`)는 fail-closed BLOCKED 껍데기(문서 §1.3) — tc 미구현. **host-side tc executor 신설 요구사항 명세**:

1. **실행 위치·권한**: GB10(192.168.200.109) 호스트 root에서 `tc qdisc add/del`. NET_ADMIN 실재.
2. **대상 vnet 지정 계약**: `target_nic` 파라미터(예 `vnet1`) + `worker`/`domain` 라벨로 워커 매핑 검증(vnet1=commerce/tb-w1, vnet2=food/tb-w2, vnet3=banking/tb-w3 — 문서 §0.1 표).
3. **관측 평면 접근 금지 allowlist (self-blinding 방어, 필수)**: executor는 `target_nic ∈ {vnet1, vnet2, vnet3}`(앱 평면, virbr0 멤버)만 허용하고 **`{br0, vnet0, vnet6, vnet7, vnet8, vnet9, vnet10}`(관측 평면/엣지)은 물리적으로 거부**. 이 거부가 문서 §1.1의 "관측 채널 죽이는 함정"(N2)을 코드로 차단.
4. **cleanup 필수·idempotent**: `tc qdisc del dev <nic> root`가 성공/부재 양쪽에서 clean exit(멱등). trap으로 abort/timeout 시에도 원복. 잔존 qdisc 검출 preflight.
5. **파라미터**: `mode=delay`(+`delay_ms`, `jitter_ms`), 확장 시 `mode=loss`(+`loss_pct`). F27-R 골든은 `delay 200ms±20ms`.
6. **프로파일 등록**: `network.host_tc`(또는 `host.tc.netem`) 신설, `allowed_locations=["gb10-host"]`, `live_supported`는 격리 검증 후, `parameter_contract.allowed_scenarios=["F27-R"]`, `scenario_parameters` = target_nic/mode/delay_ms + `forbidden_nics` allowlist.
7. **NEW query_id**: `prometheus.apm_service_p95`(서비스별 p95 — 균일성 판정의 핵심, 현재 서비스별 p95 비교 query 없음), `kubernetes.container_cpu_throttle_rate`(KCM throttle=0 확인, F12-H 대비축). SMS host CPU는 기존 host 지표 재사용 가능.

### 5-B. F27-P 전용 (배선 — k8s.netpol 프로파일 신설, 신규 executor 로직 아님)
1. **k8s.netpol 프로파일 신설**: `k8s.probe`/`k8s.env` 스타일 계약. executor는 봉인된 deny 정책 manifest를 `kubectl apply`(run) / `kubectl delete`(cleanup)만 수행 — 신규 파괴 로직 없음. `location_strategy=scenario`, `allowed_locations=["banking-namespace"]`, `live_supported=true`(kube-router 강제 실측), `parameter_contract.allowed_scenarios=["F27-P"]`.
2. **deny 정책 manifest (봉인)**: `rca-testbed-banking`에 `podSelector: app=testbed-transfer`, `policyTypes:[Ingress]`, `ingress.from`에 **`namespaceSelector: rca-testbed-banking`만** 허용(동일 네임스페이스 caller·probe 유지) → cross-namespace commerce는 자동 DROP. allowlist 방식이라 "commerce만 deny"를 banking-internal allow로 표현.
3. **kubelet probe 도달성 (live 검증 필수)**: readinessProbe는 노드(kubelet) 소스라 netpol에 안 걸리는 게 통상이나 kube-router 구현에 따라 host 트래픽 예외가 필요할 수 있음 → `banking_transfer_ready=true` 유지가 이 시나리오의 감별 근간이므로 **live 격리 검증 전 golden 금지**. (probe까지 막히면 F17-R로 붕괴.)
4. **NEW query_id**: commerce→banking cross-domain edge 실패/소실 지표(APM edge), banking self error_rate(무증가 확인). banking pod_ready·payment 502는 기존 `kubernetes.pod_ready`·`prometheus.apm_service_error_rate` 재사용.

---

## 6. F12 계열 재편 정리 (헌장 §3.3 CUT 대상의 realizable 승계)

| 기존 | 헌장 처분 | F27 승계 | 관계 |
|---|---|---|---|
| **F12-H** (pod CPU가 네트워크처럼 보임) | **KEEP**(golden 유지) | — | **F27-R의 감별 쌍** — 상호 must_rule_out(throttle>0/=0, 단일/균일) |
| **F12-R** (physical interface-down) | CUT(executor 껍데기) | **F27-R** | interface-down 대신 tc netem delay로 realizable 대체 |
| **F12-P** (cross-domain packet-loss) | CUT(CNI/NET_ADMIN 미해결 blocked) | **F27-P** | packet-loss 대신 kube-router netpol deny로 realizable 대체 |
| **F12-G** (unrelated-flow trap, 음성) | CUT(음성 13) | — | 폐기(헌장 §1 양성/음성 균형 폐지) |

→ F27 2종이 F12 시리즈의 "네트워크 executor 껍데기"(F12-R/P) 문제를 정직하게 해소: **F27-R=host tc(신설), F27-P=netpol(실재)**. F12-H는 golden으로 남아 F27-R의 감별 파트너가 된다.

---

## 7. 헌장 부합성 평가 (한 문단)

F27 2종은 헌장 §3.3이 "네트워크 executor 껍데기"로 CUT한 F12-R/F12-P를 폐기가 아니라 **realizable 메커니즘으로 승계**했고(F12-R→tc netem delay, F12-P→kube-router netpol), 동시에 KEEP된 F12-H를 F27-R의 감별 파트너로 재활용해 헌장 §2 조건③(감별 가능)을 상호 must_rule_out 쌍으로 완성했다. 가장 정직해야 했던 지점은 조건②·④의 관측이다: 이 테스트베드는 SNMP/NetFlow 소스가 0이라(문서 §1.4 실측) "네트워크 장애"임에도 **NMS를 ground-truth로 쓸 수 없고**, APM(균일 지연)·KCM(throttle=0/pod ready)·SMS(CPU 정상)의 소거법으로만 추정된다 — observation_domains에서 NMS를 뺀 비대칭이 그 정직한 표시다. 두 시나리오는 능력의 결이 다르다: F27-P는 kube-router가 실제로 정책을 강제함이 실측돼(FORWARD 체인 4.2M pkts) k8s.netpol 프로파일 + 봉인된 deny manifest라는 순수 **배선**으로 올라가지만, F27-R은 host-side tc executor가 레포에 전무해 **진짜 능력갭**이며, 그 신설에는 조건④를 넘어 헌장이 경고한 self-blinding 함정(관측 평면 vnet7~9을 죽이면 앱은 멀쩡한데 지표만 사라지는 완전히 다른 정답)을 코드로 차단하는 allowlist가 반드시 함께 와야 한다 — target_nic을 앱 평면(vnet1/2/3)으로만 제한하고 관측 평면/엣지를 물리적으로 거부하는 이 안전장치가 없으면 F27-R은 감별 시나리오가 아니라 캡처 파이프 오염 사고가 된다. 결론: F27-P는 배선 백로그로 즉시 올릴 값어치가 있고, F27-R은 tc executor + 관측평면 allowlist라는 인프라 갭을 별도 트랙(헌장 §5)으로 선행해야 하는, 정직하게 blocked인 설계다.
