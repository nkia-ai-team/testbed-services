# 인프라 Fault Surface 지도 — 네트워크(NMS) · WPM 계층 (실측)

대상: RCA 테스트베드 인프라 하부(네트워크 토폴로지 + WPM/WebURL 관측 계층).
근거: 2026-07-24 GB10(192.168.200.109) 라이브 조회 + `testbed-services` 레포 실측.
클래스: **전부 Class B (인프라)** — 헌장 §2-A 기준. 코드 앵커가 아니라 자원/노드/NIC/probe가 정답.
스코프 규칙: 라이브는 **읽기 전용**으로만 수행함(tc/설정 변경 없음). 아래 주입 수단은 전부 "설계 가능성"이며, live validation 전까지 golden 확정 금지(spec-testbed-design.md §7).

---

## 0. 토폴로지 실측

### 0.1 이중 NIC 구조 — 앱 평면 vs 관측 평면 (핵심 발견)

GB10 호스트(192.168.200.109, ARM64, KVM/libvirt)에 5개 VM. **모든 VM이 NIC 2개**를 갖는다. 이 이중 구조가 네트워크 주입 설계의 근간이다.

```
                    GB10 호스트 (192.168.200.109)
   ┌─────────────────────────────────────────────────────────────┐
   │                                                               │
   │   virbr0 (192.168.122.1/24, libvirt NAT)   ← 앱/클러스터 평면  │
   │     ├ vnet0 → tb-cp     (122.77)  control-plane + NodePort 엣지 │
   │     ├ vnet1 → tb-w1     (122.184) commerce                     │
   │     ├ vnet2 → tb-w2     (122.11)  food-delivery                │
   │     ├ vnet3 → tb-w3     (122.14)  core-banking                 │
   │     └ vnet4 → tb-runner (122.206) loadgen (클러스터 외부)       │
   │                                                               │
   │   br0 (192.168.200.109/24, enP7s7 물리 브리지) ← 관측 평면      │
   │     ├ vnet6 → tb-cp    ┐                                       │
   │     ├ vnet7 → tb-w1    │ OTel/에이전트 아웃바운드 →            │
   │     ├ vnet8 → tb-w2    │   119(lucida-next) / .104(collector)  │
   │     ├ vnet9 → tb-w3    │                                       │
   │     └ vnet10 → tb-runner ┘                                     │
   └───────────────────────────────────────────────────────────────┘
```

| VM | 도메인/역할 | 122.x IP | 앱-평면 NIC(virbr0) | 관측-평면 NIC(br0) |
|---|---|---|---|---|
| tb-cp | control-plane + NodePort 엣지 | .77 | vnet0 | vnet6 |
| tb-w1 | **commerce** | .184 | **vnet1** | **vnet7** |
| tb-w2 | **food-delivery** | .11 | **vnet2** | **vnet8** |
| tb-w3 | **core-banking** | .14 | **vnet3** | **vnet9** |
| tb-runner | loadgen(외부 조종석) | .206 | vnet4 | vnet10 |

근거: `virsh domiflist`(각 VM vnetN=network/default + vnetM=bridge/br0), `brctl show`(virbr0=vnet0-4, br0=enP7s7+vnet6-10), `ip -4 addr`(virbr0=122.1/24, br0=200.109/24). 도메인↔워커 매핑은 project-2025/CLAUDE.md 서버맵.

### 0.2 CNI · 라우팅 · 방화벽

| 항목 | 실측값 | 함의 |
|---|---|---|
| CNI | **flannel(VXLAN)** — `flannel.1` + `cni0` 존재, `FLANNEL-FWD` 체인 | 노드 간 pod 트래픽은 VXLAN으로 **노드 IP(122.x=virbr0)** 위에 캡슐화 |
| NetworkPolicy | **kube-router 활성** — `KUBE-ROUTER-FORWARD` 체인(4.2M pkts) | **netpol 강제됨** → NetworkPolicy로 서비스쌍 파티션 주입 가능(주입 수단 실재!) |
| FORWARD 정책 | policy **DROP** + KUBE/LIBVIRT/FLANNEL ACCEPT 체인 | 과거 br_netfilter 이슈의 잔재 아님 — 정상 |
| br_netfilter | 로드됨, `bridge-nf-call-iptables=1` | 과거 이슈 **해결·영속 확인**(설계 위험 아님) |
| 크로스도메인 앱경로 | commerce→banking = **ClusterDNS** `testbed-transfer.rca-testbed-banking.svc:8082` (NodePort 30282 아님) | pod-to-pod VXLAN. tc로 "특정 쌍만" 격리 불가(전부 virbr0에 muxed) → 파티션은 netpol로만 |

### 0.3 NodePort 맵 (엣지 진입점)

| NodePort | 서비스 | 도메인 | 주입 시 영향 |
|---|---|---|---|
| 30080 | nginx→api-gateway | commerce | commerce 북남 엣지 |
| 30082 | nginx | core-banking | banking 엣지 |
| 30081 | nginx | social-feed(폐기예정) | — |
| 30180/30181/30182 | order/restaurant/dispatch | food | food 서비스별 |
| 30432/30236/30308 | PG/MySQL/Oracle | 각 DB | DB 직행 |
| 30282 | transfer-service | banking | 외부 직행 이체(앱은 미사용) |

근거: `grep nodePort */k8s/*.yaml`, `commerce/k8s/02-configmaps.yaml:41,46`.

---

## 1. 네트워크 표면 · 주입점 매핑

### 1.1 주입 지점 3종 (호스트 측 tc, 읽기전용 조회로 후보만 도출)

| 주입점 | 수단 | 영향 범위 | 관측 함정 |
|---|---|---|---|
| **워커 앱-NIC** (vnet1/2/3 host-egress) | `tc qdisc netem delay/loss` (호스트에서 VM으로 들어가는 트래픽) | 해당 워커 pod **전부** 균일 열화 | 앱 평면 — 안전 |
| **워커 관측-NIC** (vnet7/8/9, br0) | tc netem on 관측 egress | OTel/에이전트 아웃바운드만 열화, **앱 정상** | ★ **관측 채널 자체를 죽임** — self-blinding 함정 |
| **엣지 NIC** (vnet0, tb-cp) | tc netem | 북남 NodePort 진입 전 도메인 | 엣지 공용 — blast radius 큼 |

핵심 구분(헌장이 요구한 "관측 채널 죽이는 함정"): **vnet1(앱)** 주입 = 정직한 네트워크 장애(관측은 살아있음). **vnet7(관측)** 주입 = 관측 파이프 장애(앱은 멀쩡, 지표만 사라짐) — 완전히 다른 정답. 둘을 섞으면 안 됨.

### 1.2 부분 파티션 가능성

- **워커 단위 격리**: tc/링크다운으로 가능(워커 전체가 대상).
- **특정 서비스쌍만**(예: commerce→banking만): tc로는 **불가**(VXLAN에 muxed). 단 **kube-router NetworkPolicy가 강제되므로 deny 정책으로 주입 가능** — 이게 유일한 실재 부분-파티션 수단. Class B(k8s), 실주입.

### 1.3 기존 network.fault executor 상태 (실측)

| 시나리오 | executor | 상태 |
|---|---|---|
| F07-R, F12-P, F12-R, F12-G, F13-H | `network_fault_executor.py` | **fail-closed BLOCKED 껍데기** — tc/interface-down 미구현, 승격 방지용 거부만 |
| F12-G | `network_external_probe_executor.py` | 유일한 실동작 — 단 **tc가 아니라** 외부 .57에서 bounded nping TCP flow 생성(distractor 트래픽). 네트워크 열화 아님 |

→ **네트워크 열화(delay/loss/interface-down) 실주입 수단은 레포에 0개.** 설계하려면 host-side tc executor 신설 필요.

### 1.4 NMS 관측 정직 판정 ★

**결론: NMS 계열로는 네트워크 장애를 관측할 수 없다(주입은 tc로 가능, 관측은 불가).**

라이브 실측:
- 호스트 snmpd = **inactive**, udp/161 미개방.
- tb-cp/w1/w2/w3/runner **전부 161 닫힘** — SNMP 소스 0.
- NetFlow exporter·물리 스위치·라우터 = 테스트베드에 **없음**(전부 KVM 가상 브리지).

119의 collector-nms/snmp/nms-trap이 살아 있어도 **폴링/수신할 소스가 없음**. spec-testbed-design.md:425의 NMS dimension(`if_index`, packet loss/drop, RTT)은 이 테스트베드에서 **채워지지 않는다**.

정직한 감별 방식: 네트워크 장애 주입 시 관측은 **APM(전 서비스 균일 지연 상승) + KCM(container throttle=0) + SMS(host CPU 정상)의 배제 추론**으로만 가능. 즉 "네트워크다"를 NMS로 **양성 확인 불가**, 앱/자원 신호의 소거법으로 **추정**만 가능. → NMS를 must_support ground-truth로 쓰는 골든은 **금지**. (헌장 §1 NMS 도메인 커버는 "주입 가능/관측 소거법"으로만 정직 표기.)

---

## 2. WPM 복원 표면 · 관측 기여

### 2.1 두 개의 서로 다른 신호 계열 (혼동 주의) ★

| 계열 | 실체 | 신호 | 이 테스트베드 현황 |
|---|---|---|---|
| **WPM javaagent** (`wpmagent.jar`) | JVM 내부 계측(APM류) | xlog / transaction profile / **thread 상태·스택** | 바이너리 보존, **미부착**. 복원 대상 |
| **WebURL synthetic probe** (collector-weburl) | 외부 HTTP 프로버 | phase별(DNS/connect/**TTFB**/download) latency | probe job **미구성**. 별도 트랙 |

**이 구분이 F13 판정의 핵심**(§2.4).

### 2.2 WPM javaagent 복원 실측 (GB10 `/opt/polestar10/wpm`)

보존 자산:
- commerce 5종: order/payment/product/inventory/notification `wpmagent.jar` + `wpmagent.conf`.
- food 4종: food-order/food-payment/restaurant/dispatch.
- banking: **없음**(신규 conf 합성 필요).

conf 실측(예 `order/wpmagent.conf`):
```
msa_group_id=plopvape-shop-order      # ← 레거시 social-feed 네이밍(commerce인데 plopvape-shop)
obj_name=plopvape-shop-order
manager_ip=10.43.234.31               # ← ★ STALE: k8s ClusterIP(10.43/16), 지금 없는 인-클러스터 콜렉터
is_msa_env=true
net_collector_udp_port=31002          # 119 lucida-collector-wpm 정포트와 일치 ✓
net_collector_tcp_port=31005          # 일치 ✓
```

**복원 3대 수리점**:
1. **manager_ip 재지정 (필수 차단요인)** — 현재 `10.43.234.31`(commerce)/`10.43.232.72`(food)는 폐기된 인-클러스터 ClusterIP. 실 콜렉터는 119의 lucida-collector-wpm. pod→119 도달은 관측 평면(br0→192.168.230.x) 경유 → `manager_ip=192.168.230.119`로 재지정 필요(pod에서 119 도달성 live 검증 선행).
2. **msa_group_id 정리** — `plopvape-shop-*`(레거시) → `commerce-*`. 관측 식별자 명료화(차단은 아님).
3. **banking conf 신설** — 4서비스 conf 합성.

### 2.3 매니페스트 주입 패턴 · 이중 부착 충돌

현행 규약(spec-testbed-services.md:105-108, 실측): **JAVA_TOOL_OPTIONS 이중 javaagent**
```
-javaagent:/opt/apm/opentelemetry-javaagent.jar \
-javaagent:/opt/wpm/<svc>/wpmagent.jar -Dwpm.config=/opt/wpm/<svc>/wpmagent.conf
```
- hostPath: `/opt/polestar10/apm`→`/opt/apm`, `/opt/polestar10/wpm`→`/opt/wpm`(readOnly).

현행 commerce 매니페스트 실측(`22-inventory-service.yaml:49-50` 등): **APM javaagent만** 부착, WPM 볼륨/2nd javaagent **부재**. social-feed 매니페스트(`20-post-service.yaml:23-39`)에는 wpm-agent hostPath 마운트 패턴은 보존돼 있으나 **javaagent 부착 자체가 빠져** OTel도 미적용(spec 3.4 주석과 일치).

**복원 = 3단계**: (a) wpm-agent hostPath 볼륨+마운트 추가(social-feed 패턴 이식), (b) JAVA_TOOL_OPTIONS에 2nd javaagent+`-Dwpm.config` append, (c) manager_ip 수리.

**이중 부착 충돌 위험(최대 리스크)**: OTel javaagent와 WPM javaagent가 **동일 JVM을 둘 다 바이트코드 계측** → 클래스 변환 경합, 기동 오버헤드, HTTP/JDBC 인터셉트 중복 가능. spec-testbed-services.md:107-108에 과거 이중 부착 형태 기록. **live validation(JVM 기동 안정성·트레이스 무결) 통과 전 golden 금지.**

### 2.4 F13×3 처리 방침 판정 ★

기존 F13 실측(catalog + metadata):

| ID | slug | 실제 대상 계열 | metadata 정답 | executor |
|---|---|---|---|---|
| F13-R | checkout-edge-**ttfb**-delay | **WebURL** TTFB phase | 없음 | wpm.probe(`live_supported:false`) |
| F13-H | **connect**-phase-network-delay | WebURL connect + tc | 없음 | network.fault(BLOCKED 껍데기) |
| F13-P | large-history-**download** | WebURL download + banking 대용량 | 없음 | wpm.probe(inert) |
| F13-G | single-probe-failure | 음성(멀티로케이션 필요) | 없음 | — (헌장 CUT 대상) |

`wpm.probe` 프로파일 실측: `live_supported:false`, default_query는 generic `http.entry_health`뿐 — **WPM/WebURL phase 신호 배선 0**. 전부 blocked.

**판정: WPM javaagent 복원해도 F13은 그대로 승격 불가 — 재설계 필요.**
- F13 시리즈는 전부 **WebURL synthetic-probe phase 신호**(TTFB/connect/download)를 정답 증거로 요구. 이건 `wpmagent.jar`(JVM 내부 계측)가 **주는 신호가 아님**. 계열 오배정.
- F13 실현 조건 = 119 collector-weburl에 **테스트베드 NodePort 대상 probe job 구성** + phase metric query 계약(`weburl.dns.duration` 등, spec-testbed-design.md:455) 신설. `wpmagent.jar` 복원과 **무관한 별도 능력**.
- 방침: **F13-R/H/P는 "WebURL 재설계 대기"로 분류(승격 아님)**. F13-G는 헌장대로 CUT. wpmagent.jar 복원은 F13이 아니라 **더 잘 앵커된 WPM-native 신규 후보(W1)**를 여는 데 쓴다.

### 2.5 WPM javaagent가 실제로 강화하는 감별

WPM xlog/thread 신호(spec-testbed-design.md:455의 xlog/transaction/interaction)의 고유 기여 = **"왜 느린가"의 스레드 레벨 증거**. APM은 latency↑를 보여주나, WPM thread profile은 스레드가 어디서 BLOCKED인지(getConnection 대기 / row-lock 대기 / 외부 hop read)를 보여줌.

- **F22(풀 고갈)**: WPM thread state = 다수 스레드 `getConnection` 대기 → Hikari 고갈 직접 증거.
- **F01/F06-H(row-lock)**: WPM thread state = lock wait 스택 → 락 경합 직접 증거.
- **F20(slow query)**: WPM xlog = 느린 SQL 구간 profile.
- **F21(thread)**: **최강 시너지** — thread dump/state가 정답 그 자체.

---

## 3. 시나리오 후보

각 후보: 트리거 / 전파 / 증상 / 인프라 앵커 / 주입수단 판정 / F12·F13 관계.

### N1. ★ 워커 네트워크 지연 — F12-H의 "정직한 쌍둥이" (최우선)
- **트리거**: commerce 워커(tb-w1) 앱-NIC(host-side vnet1)에 `tc netem delay`(예 200ms).
- **전파**: commerce **전 서비스**가 균일하게 느려짐(단일 서비스 아님). 동기 hop 체인(order→product→inventory→payment)이 netem 누적으로 타임아웃 → 하류 풀/스레드 압박.
- **증상**: APM 전 commerce 서비스 latency↑ **균일**, KCM container throttle=**0**, SMS host CPU **정상**. → "네트워크다"를 소거법으로 추정.
- **인프라 앵커**: host `vnet1` egress qdisc(virbr0 멤버). 주입=tc netem, 복구=`tc qdisc del`.
- **주입수단 판정**: **신설 필요**(현재 host-side tc executor 0개). NET_ADMIN은 GB10 호스트 root — 실재.
- **F12 관계**: **F12-H(CPU throttle이 네트워크처럼 보임)의 역방향 진짜 네트워크**. 둘을 must_rule_out 쌍으로 묶으면 감별 골든 완성(F12-H: throttle↑ / N1: throttle=0). F12-R(interface-down, blocked)의 realizable 대체.

### N2. 관측-평면 격리 — self-blinding 함정 (주의)
- **트리거**: commerce 관측-NIC(vnet7, br0)에 netem loss → OTel/에이전트 아웃바운드만 열화.
- **전파**: **앱 트래픽 정상**, 지표/트레이스만 119 도달 실패 → 콜렉터 수신 gap.
- **증상**: APM/메트릭 시계열 구멍, 그러나 loadgen 북남 신호는 정상(앱 건강). 정답="관측 파이프 장애, 앱 아님".
- **인프라 앵커**: host `vnet7`(br0 멤버).
- **주입수단 판정**: 신설 필요(tc). **위험**: 자기 관측을 죽여 캡처 파이프까지 오염 가능 → live-30 큐에 넣기 전 격리 검증 필수.
- **F12 관계**: 헌장이 경고한 "관측 채널 죽이는 함정"의 명시적 시나리오화. 유지 여부는 오픈이슈(distractor 가치 vs 캡처 오염 리스크).

### N3. ★ 크로스도메인 파티션 — NetworkPolicy deny (실주입 가능)
- **트리거**: `NetworkPolicy`로 commerce namespace → banking namespace egress deny(kube-router 강제).
- **전파**: payment.processPayment의 banking 이체 호출만 네트워크 레벨 차단 → C4와 동일 증상(payment 롤백, checkout 실패)이나 **원인이 앱이 아니라 네트워크 정책**.
- **증상**: APM cross-domain edge(commerce payment→banking transfer) **소실**, banking은 자체 probe·타 caller에 **건강**. commerce만 banking 도달 실패.
- **인프라 앵커**: kube-router netpol(FORWARD 체인 실측 확인). 주입=`kubectl apply` deny policy, 복구=`kubectl delete`.
- **주입수단 판정**: **실재**(kube-router 강제 확인). Class B(k8s), 실주입. tc로 불가능한 "특정 쌍만" 파티션의 유일 수단.
- **F12 관계**: **F12-P(cross-domain packet-loss, CNI/NET_ADMIN 미해결로 blocked)를 realizable 메커니즘으로 대체**. C4(banking 앱 다운)와 must_rule_out 쌍(N3: banking 건강 / C4: banking 5xx).

### N4. NodePort 엣지 지연 (낮은 우선, N1과 중복)
- 트리거: tb-cp vnet0에 netem → 특정 도메인 북남 진입 지연. 앵커=엣지 NIC. N1과 증상 유사, blast radius만 큼. 보류.

### W1. ★ WPM thread-state 감별자 — 풀/락 고갈 (WPM-native 유일 가치)
- **트리거**: 기존 F22(order Hikari 10 고갈) 또는 F01/F06-H(row-lock) 재사용.
- **전파**: (기존 코드 앵커 그대로) — 신규 주입 아님.
- **증상 강화**: APM은 latency↑만, **WPM thread profile이 스레드 스택을 노출** → `getConnection` 대기(풀 고갈) vs `lock wait`(락) vs 외부 read(hop 지연)를 **직접 감별**. must_support = WPM thread state.
- **인프라 앵커**: wpmagent.jar 부착(§2.2-2.3 복원 후). 정답은 코드(F22/F01)지만 **관측 계열이 WPM** → Class B 관측 보강 후보.
- **주입수단 판정**: 기존 injector 재사용 + WPM 복원. **복원(manager_ip·이중 javaagent) live validation에 종속.**
- **F13 관계**: F13이 요구한 WPM 계열 커버를 **thread 신호로 정직하게** 실현(F13의 WebURL phase와 무관, 더 잘 앵커됨).

### W2. WebURL phase 분해 — F13 재설계 (별도 트랙)
- F13-R/P를 WebURL synthetic-probe phase 신호(TTFB/download)로 재설계. **조건**: 119 collector-weburl에 테스트베드 NodePort probe job 구성 + phase metric 계약 신설. wpmagent.jar 복원과 독립. 현 시점 능력 갭 → 백로그.

---

## 4. 요약표

| # | 후보 | 계열 | 주입 수단 | 실현성 | F12/F13 관계 |
|---|---|---|---|---|---|
| **N1** | 워커 네트워크 지연 | 네트워크(관측=APM 소거법) | host tc netem on vnet1 (**신설**) | 앵커 실재, executor 신설 필요 | F12-H 정직한 쌍 / F12-R 대체 |
| **N3** | 크로스도메인 netpol 파티션 | 네트워크(k8s) | `kubectl` deny policy (**실재**) | **즉시 realizable** | F12-P realizable 대체 |
| **W1** | WPM thread-state 감별 | WPM javaagent | wpmagent.jar 복원 + 기존 injector | 복원 live 검증 종속 | F13 WPM커버를 thread로 정직 실현 |
| N2 | 관측-평면 격리 | 네트워크(관측 self-blind) | host tc on vnet7 (신설) | 위험(캡처 오염) | "관측 죽이는 함정" 명시화 |
| N4 | NodePort 엣지 지연 | 네트워크 | host tc on vnet0 | N1 중복, 보류 | — |
| W2 | WebURL phase 분해 | WebURL probe | collector-weburl probe job (미구성) | 능력 갭, 백로그 | F13-R/P 재설계 |

**핵심 판정 요약**:
- **네트워크 주입점**: 워커별 앱-NIC(vnet1/2/3)=열화 주입, 관측-NIC(vnet7/8/9)=관측 파이프 함정, 특정-쌍 파티션=kube-router NetworkPolicy(유일). host-side tc executor는 레포에 **없음**(신설 필요), NetworkPolicy는 **즉시 가능**.
- **NMS 관측**: SNMP/NetFlow 소스 **전무**(모든 VM 161 닫힘) → NMS ground-truth **불가**, APM/KCM/SMS 소거법으로만 추정. NMS를 must_support로 쓰는 골든 금지.
- **WPM 복원 시 F13**: **그대로 승격 불가**. F13은 WebURL phase 신호 대상인데 wpmagent.jar는 그걸 안 줌(계열 오배정). F13-R/H/P=WebURL 재설계 대기, F13-G=CUT. 복원은 대신 W1(thread-state 감별, F21/F22 시너지)을 연다. 최대 리스크=OTel와의 이중 javaagent 충돌(live 검증 필수).
- **신규 후보**: 6개(N1-N4, W1-W2). **top 3 = N1(F12-H 정직한 쌍), N3(netpol 크로스도메인 파티션, 즉시 realizable), W1(WPM thread 감별)**.
