# 설계 시트 — F26-R / F26-H / F26-P / F26-Q: 노드·K8s 인프라 결함 4종 (Class B)

- **백로그 근거**: 헌장 §1(KCM·SMS 도메인 = Class B 인프라 앵커), §5 "제어점 부재 — 파드 삭제·노드 cordon/drain·노드 정지 executor 전무".
- **근거 문서**: `scenario-redesign-wip/fault-surface-infra-k8s-node.md` N1(→F26-R)·N2(→F26-H)·N3(→F26-P)·N4(→F26-Q). 라이브 실측(2026-07-24, `KUBECONFIG=/root/tb-kubeconfig`) + 매니페스트 `file:line` 재검증(아래 앵커 전부 grep 확인).
- **공통 정체성**: Class B는 "이 자원/노드/링크 때문에"가 정답(헌장 §2-A). 4종 모두 **앱 코드 무결함** — 증상이 앱 5xx/재시작으로 뜨나 원인은 노드·PV·hostPath·루트fs라는 인프라 지점이다. naive RCA는 죽은 앱을 root로 오판한다.
- **재검증 앵커(공통 인프라 토대)**:
  - 모든 워크로드 `replicas: 1` — `commerce/k8s/20-order-service.yaml:16`, `food-delivery/k8s/10-mysql.yaml:10`, `commerce/k8s/10-postgres.yaml:33`, `core-banking/k8s/10-oracle.yaml:13`. nodeSelector/affinity 전무(전 매니페스트 grep 무결과).
  - hostPath `/opt/polestar10/apm` `type: Directory` — `commerce/k8s/20-order-service.yaml:26-29`, 마운트 `/opt/apm` RO `:47-49`, javaagent `:64-65`, `imagePullPolicy: Never` `:33`. 22개 앱 파드 동일.
  - local-path PV(WaitForFirstConsumer, 노드로컬 hostPath) — `food-delivery/k8s/10-mysql.yaml:49-56`(volumeClaimTemplates, storageClassName 미지정→default `local-path`). 기존 PV 실경로는 `host_stress_executor.py:13-15`가 이미 사용(F10 계열).
  - ephemeral-storage req/limit 미설정(0/0, `describe node` 실측) → 앱 로그/tmp가 노드 루트fs로.

---

## 1. 인과 사슬 (4개)

### F26-R (N1) — 워커 노드 상실 → 흩어진 배치가 만드는 다도메인 동시 장애
```
[워커 1대 소실: kubectl cordon+drain (비파괴) — 실제 정지/virsh destroy는 파괴적이라 배제]
  → 모든 워크로드 replicas=1 + affinity 전무 → 대체 파드 없음
  → drain된 노드의 파드 evict → 타 노드 재스케줄 시도하나
     N2(PV 노드고정)·N5(imagePullPolicy:Never 미적재)에 막혀 장기 미가용
  → §배치표대로 서로 무관한 3개 도메인의 다중 서비스가 같은 시각 Down
  → KCM pod_ready 다도메인 동시 급감 + node_ready=false
```
앵커: `20-order-service.yaml:16`(replicas 1), affinity 무(grep 무결과), 라이브 배치표(fault-surface §1). **정체성: "서로 무관한 도메인들이 동시에" 죽는 것 자체가 노드 공유가 root라는 증거** — 단일 앱 성능저하(F05/F09)와 완전히 다른 표면.

### F26-H (N2) — DB PV 노드고정 → 노드 상실 시 DB pod 영구 Pending
```
[DB pod가 얹힌 노드 cordon+drain]
  → StatefulSet가 DB pod 재생성 시도
  → local-path PV는 노드로컬 hostPath(/opt/local-path-provisioner/<pvc>)에 묶임
  → 스케줄러가 PV 없는 노드로 못 보냄 → DB pod Pending 영구
     (event: "N node(s) had volume node affinity conflict")
  → 해당 도메인 전 서비스가 DB 커넥션 실패로 5xx
  → "디스크는 멀쩡한데 DB에 닿지 못함" 함정
```
앵커: `10-mysql.yaml:49-56`(volumeClaimTemplates, local-path), `host_stress_executor.py:13-15`(PV 실경로가 노드로컬 hostPath임을 증명), StatefulSet replicas=1. **정체성: 디스크 지표는 정상인데 DB pod가 Pending** — F10(디스크 full/IO로 DB가 살아서 느림)과 mechanism 정반대(DB 완전 소실 + 재스케줄 원천 불가).

### F26-P (N3) — hostPath 에이전트 디렉토리/jar 소실 → 노드 위 전 앱 기동 실패
```
[특정 워커의 /opt/polestar10/apm 디렉토리 또는 그 안 javaagent jar를 rename로 치움]
  (a) 디렉토리 소실 → type:Directory 검증 실패 → FailedMount → 재기동/재배포 시 Pending
  (b) jar만 소실 → -javaagent:/opt/apm/...jar 로드 실패 → JVM 즉시 종료 → CrashLoopBackOff
  → 해당 노드의 앱 파드가 재시작·재배포 시점에 일제히 못 뜸
  → KCM container_restart_count↑ / last_termination_reason / mount 실패 event
```
앵커: `20-order-service.yaml:26-29`(hostPath type:Directory), `:47-49`(mount /opt/apm RO), `:64-65`(javaagent), 22개 파드 공통. **정체성: 이미지·설정 정상인데 노드 로컬 경로 자산 부재** — F05-G(잘못된 이미지 레이어)와 관측 신호 다름(image pull failure vs mount/agent failure). **복구 안전이 핵심**: 소실 상태 방치 시 22개 앱 영구 불능 → trap 기반 원상복구 필수(§4-C).

### F26-Q (N4) — ephemeral 무제한 → 노드 루트fs 포화 → DiskPressure eviction
```
[host.stress watermark를 PV가 아닌 노드 루트fs(/) 대상으로 채움]
  → 전 파드 ephemeral-storage 상한 없음 → 앱 로그/tmp는 overlay(노드 루트 /)
  → 노드 루트fs가 kubelet 임계 초과 → DiskPressure=True
  → 노드 전역 eviction(초과사용·BestEffort 파드부터) — 가해 파드가 아닌 이웃까지 축출
  → 무고한 이웃 pod Evicted → 재스케줄 반복 (2차 피해)
```
앵커: `describe node` Allocated `ephemeral-storage 0(0%)`(전 파드 상한 부재), 앱 PV 없음(§0), watermark mode 실재(`host_stress_executor.py:104-106`). **정체성: root=노드 루트fs 포화인데 증상=무관한 이웃 파드 eviction** — F10-R(postgres PV watermark→DB 자신)과 대상·mechanism 다름(노드 루트 vs PV, 이웃 eviction vs DB 디스크 full).

---

## 2. 감별 설계 (골든 조건 ③)

| | must_support | must_rule_out (감별점) |
|---|---|---|
| **F26-R** | node_ready(대상 노드)=false; **서로 무관한 3개 도메인** pod_ready 동시=false(commerce order/payment + food + banking) | 단일 서비스 성능저하 아님(F05/F09: 노드 살아있음, node_ready=true) · 특정 DB만 문제 아님(다도메인 동시) · 앱 이미지/설정 정상 |
| **F26-H** | 대상 DB pod_ready=false + **pod Pending 사유=volume node affinity conflict**; 해당 도메인 서비스 5xx | 디스크 full/IO 아님(F10: DB 살아있음·디스크 지표 이상) → node/PV 디스크 정상 · CrashLoop/OOM 아님(스케줄 자체 불가, 컨테이너 미기동) |
| **F26-P** | container_restart_count↑ + last_termination_reason(agent/JVM) **또는** FailedMount event; 노드 로컬 경로 신호 | 이미지 부재 아님(image_pull_failure=false) → F05-G와 구분 · liveness/OOM 아님(F05-H/R: probe·mem limit 원인, hostPath 정상) · 코드 무결함 |
| **F26-Q** | 노드 **DiskPressure=true**; **가해 파드 아닌 이웃 파드 Evicted**; 노드 루트fs util 高 | MemoryPressure 아님(F05-P: memhog→mem eviction) → 디스크발 · PV 디스크 아님(F10: PV 대상, DB 자신) → 노드 루트fs · 이웃 eviction이지 가해 파드 자멸 아님(2차 피해) |

**4자 감별 축**: R=다도메인 동시사망+node down / H=DB Pending(volume affinity)+디스크정상 / P=재시작·mount·agent 실패(이미지정상) / Q=DiskPressure+이웃 eviction(mem 아님·PV 아님). R↔H는 near-twin(같은 node executor, drain 대상만 다름) — R은 광범위 다도메인, H는 DB pod의 노드를 특정해 "재스케줄 불가"를 지목. F05-P↔F26-Q도 near-twin(둘 다 eviction) — 노드 condition(Memory vs Disk)이 결정적 감별자.

**오답 유도**: 4종 모두 증상이 앱(5xx·재시작·Pending)으로 뜨므로 naive RCA가 앱을 root로 오판. 정답은 각각 노드 소실(R)/PV 노드고정(H)/hostPath 자산(P)/노드 루트fs(Q).

---

## 3. 골든 4조건 자체점검표

| 조건 | F26-R | F26-H | F26-P | F26-Q |
|---|---|---|---|---|
| ① 코드/인프라 앵커 | ✅ replicas=1·affinity無·배치표 실측 | ✅ local-path PV 노드고정 + PV 실경로 실측 | ✅ hostPath type:Directory + javaagent 실측 | ✅ ephemeral 0/0 + watermark mode 실재 |
| ② 정답(answer-key) | ✅ root_cause{target_kind=node, mechanism=SPOF blast radius} | ✅ root_cause{target_kind=node+PV, mechanism=volume node affinity conflict} | ✅ root_cause{target_kind=hostpath-asset(node-local)} | ✅ root_cause{target_kind=node-root-fs, mechanism=DiskPressure eviction} |
| ③ 감별 가능 | 🟡 node_ready·pod_ready 실재. 감별 설계 완결. 관측 배선 충분 | 🟡→❌ **pod Pending 사유(volume affinity) query 미존재** — 스칼라 감별자 부재 | 🟡 restart_count·last_termination 실재하나 **FailedMount event query 미존재**(dir-소실 분기) | 🟡→❌ **DiskPressure condition·eviction event query 둘 다 미존재** — 정체성 관측 불가 |
| ④ 주입 수단 실재 | ❌ **node drain executor 부재**(k8s.lifecycle=이미지 스왑만). `k8s.node` 신설 필요 | ❌ F26-R과 동일 node executor 갭(비파괴 drain) | 🟡→❌ host.stress에 **hostpath-disrupt mode(rename+trap 복구) 신설 필요** — 기존 mode에 없음(신규 executor 코드) | 🟡 host.stress **watermark mode 실재**(F10-R live검증). target_dir을 노드 루트fs로 + 안전 상한 신규 계약(배선) |
| **종합** | ①②✅ ③🟡 ④❌ → **draft/blocked**(능력갭: node executor) | ①②✅ ③❌ ④❌ → **draft/blocked**(능력갭×2: node executor + Pending 사유 query) | ①②✅ ③🟡 ④❌ → **draft/blocked**(능력갭: hostpath mode + 복구안전) | ①②✅ ③❌ ④🟡 → **draft/blocked**(배선: watermark 재타깃 + DiskPressure/eviction query 2종) |

4종 모두 `readiness=draft`, `prerequisite_gate.state=blocked`, `live_allowed=false`. **부류 분리**: R·H는 진짜 능력갭(신규 `k8s.node` executor) — 하나의 훅이 둘을 동시에 연다. P는 host.stress 신규 mode(코드)+복구안전. Q는 기존 watermark mode 재타깃(배선)이나 관측 query 2종이 없어 ③이 사실상 ❌.

---

## 4. 능력 갭 (승격 전 선행 — prerequisite_gate)

### 4-A. F26-R · F26-H 공통 (진짜 능력갭 — 신규 executor)
1. **노드 drain/cordon executor 부재**(근본, 헌장 §5): 현 executor 중 파드 삭제·노드 cordon/drain·노드 정지 수단 전무. `k8s.lifecycle`=이미지 `set image`만, `host.stress`=프로세스/파일만(fault-surface §2 executor 실측). **신설안 `k8s.node`**: `action=drain`(cordon+`kubectl drain --ignore-daemonsets --delete-emptydir-data`) / cleanup=`uncordon`. **비파괴·복구가능**이 핵심 — 실제 정지/`virsh destroy`는 local-path 데이터 소실 위험이라 배제(fault-surface N2 주의). 훅 하나가 R·H 동시 해제.
2. **대상 노드 동적 결정**: CLAUDE.md "tb-w1=commerce" 메모는 **실측 스테일**(fault-surface §1). 스케줄러 배치가 동적이므로 manifest에 노드명 하드코딩 금지 → preflight에서 **라이브 조회**로 결정: R=blast radius 최대 워커, H=대상 DB pod(postgres-0/mysql-0/oracle-0)가 실제 얹힌 워커.
3. **load.north_south companion**: 3도메인 baseline이 이미 상시 가동(tb-runner systemd) → 노드 소실의 5xx 신호는 baseline으로 관측. companion은 대상 도메인 create/checkout 부하 보강용.

### 4-B. F26-H 전용 (관측 갭)
4. **NEW query: `kubernetes.pod_unschedulable_reason`** (조건 ③ 결정적 감별자): DB pod가 Pending일 때 스케줄 실패 사유(`volume node affinity conflict`)를 스칼라/문자열로 노출. 현재 `pod_ready`=false는 "불가용"만 알 뿐 **"디스크 정상인데 스케줄 불가"라는 F10 감별점을 못 짚음**. kubernetes adapter에 pod.status.conditions[PodScheduled].reason/message selector 등록 필요.

### 4-C. F26-P 전용 (신규 mode + 복구안전 — 헌장 §2-A 검토 포함)
5. **host.stress `hostpath-disrupt` mode 신설**(신규 executor 코드): ssh 프레임워크(`host_stress_executor.py`)는 재사용하나 fio/watermark/pressure/cpu/memhog 어디에도 파일 rename 기능 없음. 신규 mode = `mv /opt/polestar10/apm /opt/polestar10/apm.lucida-<scn>-aside`(dir 분기) 또는 jar만 rename(jar 분기). **§2-A 부합**: 이는 env로 코드 동작을 왜곡하는 게 아니라 **인프라 자산(노드 로컬 디렉토리)을 실제로 제거**하는 것 → 정당한 Class B.
6. **복구 안전 (필수, cleanup 설계)**: 소실 방치 = 그 노드 22개 앱 영구 불능(치명). 요건 — (a) run이 반드시 원본 경로를 aside로 **rename(삭제 아님)**해 원상복구 가능성 보존, (b) executor trap + cleanup이 aside→원경로 rename 되돌림, (c) **recovery_gate가 `[[ -d /opt/polestar10/apm && -e .../opentelemetry-javaagent.jar ]]` + 대상 노드 앱 pod_ready 회복까지 검증**해야 통과, (d) manifest `cleanup.required=true`·`recovery_gate=true`. FailedMount 분기는 파드를 재기동해야 회복되므로 recovery timeout 여유(≥15m).
7. **NEW query: `kubernetes.volume_mount_failure`** (dir-소실 분기 ③): FailedMount event를 boolean으로. jar-소실 분기는 기존 `container_restart_count`+`container_last_termination_reason`로 관측 가능(배선 불필요).

### 4-D. F26-Q 전용 (배선 — 기존 mode 재타깃 + 관측 query 2종)
8. **host.stress watermark 노드 루트fs 재타깃**(배선, 신규 코드 아님): `watermark` mode(`host_stress_executor.py:104-106`)는 target_dir을 %까지 채움 — 현재 PV 디렉토리 대상(F10-R). target_dir을 **노드 루트fs(`/`)의 임시 경로**로 두고 `watermark_percent`를 DiskPressure 임계 초과로 계약하면 재현. **안전 상한 필수**: `reserve_mib`(루트fs 여유 하한)·`maximum_fill_mib`로 노드 완전 마비 방지, artifact 파일 cleanup으로 즉시 회수.
9. **NEW query 2종** (조건 ③, 정체성): `kubernetes.node_disk_pressure`(node.condition.DiskPressure boolean — 현재 `node_ready`만 있고 세부 condition 없음) + `kubernetes.pod_evicted_count` 또는 eviction event(이웃 파드 축출 = 2차 피해 신호). 둘 다 없으면 "루트fs가 찼다"까지만 보이고 **"이웃이 쫓겨났다"는 정체성을 못 관측**.

### 4-E. 공통 등록 작업
10. `registry/profiles.json` allowed_scenarios/scenario_parameters(R·H=`k8s.node`, P·Q=`host.stress` 신규 mode), `catalog.json`·`scenario-metadata.json`·`controllers.json` 바인딩(primary + companion=load.north_south).

---

## 5. 운영 안전 요건 (F26-R·H 특별 — 노드 단위 교란)

노드 단위 교란은 실행 중 다른 시나리오·baseline·캡처와의 충돌이 극심하다(drain은 대상 노드의 **모든** 파드를 evict → 다도메인 baseline 동시 타격). manifest에 명세한 요건:

- **전용 클린윈도우 (배타 리스)**: `global-dirty-lease` + `baseline-clean-window`(30m)를 3도메인 전체로 확장. R·H 실행 중에는 어떤 시나리오도 동시 실행 금지(러너 409 단일락으로도 강제되나, 노드 교란은 baseline까지 흔드므로 명시). 캡처는 이 이벤트가 **다-namespace 동시**임을 알아야 함(capture pre/post window에 3도메인 포함).
- **preflight 대상 위치 확인**: H는 drain 전 대상 DB pod가 실제 얹힌 노드를 라이브 조회(`kubectl get pod -o wide`)로 확정 — 배치가 동적이라 하드코딩 금지(§4-A #2). R은 blast radius 최대 워커를 동적 선정.
- **비파괴 원칙**: drain만 사용(cordon+drain), 노드 파일·PV 데이터 보존. 실제 정지/`virsh destroy` 금지(local-path 데이터 소실 위험).
- **cleanup·recovery_gate**: cleanup=`uncordon` + recovery_gate가 **대상 노드 node_ready=true + 영향받은 전 namespace pod_ready 회복**까지 검증해야 통과. H는 uncordon 후 DB pod가 자기 PV 노드로 재스케줄되어 Ready 되는 것까지(recovery timeout ≥15m). 미회복 시 DIRTY 유지.
- **F26-P·Q 파일 교란 안전**: P는 §4-C(rename·trap·경로+pod 검증), Q는 §4-D(reserve/max 상한·artifact 회수). 둘 다 `cleanup.required=true`·`recovery_gate=true`.
- **live 게이트**: 4종 전부 `live_enabled=false`·`prerequisite_gate=blocked`. 능력갭(R·H·P) 또는 관측 query(H·Q) 해소 + 위 안전요건 실증 전까지 live 승격 금지.

---

## 6. 헌장 부합성 평가 (한 문단)

F26 4종은 헌장 §1의 KCM·SMS 인프라 도메인을 Class B로 채우되, 전부 **앱 코드 무결함 + 인프라 지점이 정답**이라는 §2-A 분리 규칙을 정직하게 지켰다(env로 코드 왜곡해 Class A로 위장하는 유혹이 없는, 순수 인프라 표면). 깊이 ①②는 replicas=1·hostPath type:Directory·local-path PV·ephemeral 0/0을 매니페스트 `file:line`과 라이브로 동시 확인해 완전 충족했고, 감별 ③은 4자 매트릭스(다도메인 동시사망 / DB Pending / mount·agent 실패 / DiskPressure 이웃 eviction)로 F05·F09·F10과의 경계를 결정적으로 그었다 — 특히 F05-P(memhog→MemoryPressure eviction)와 F26-Q(root-fs→DiskPressure eviction)의 near-twin을 노드 condition 하나로 가르고, F10(PV 디스크로 DB가 살아서 느림)과 F26-H(PV 노드고정으로 DB가 죽어서 스케줄 불가)를 mechanism 정반대로 대비시킨 것이 카탈로그에 값진 감별짝을 만든다. 그러나 ③④가 걸린다: R·H는 헌장 §5가 지목한 "노드 cordon/drain executor 전무"라는 진짜 능력갭이고(하나의 `k8s.node` 훅이 둘을 동시에 연다), P는 host.stress에 없는 hostpath-disrupt mode 신설 + 복구안전(22개 앱을 영구 불능으로 만들 수 있어 trap 복구가 설계의 중심)이며, H·Q는 정체성 관측 query(Pending 사유 / DiskPressure·eviction)가 아예 없어 감별이 "설계는 완결이나 관측 배선 부재"로 막힌다. 4조건 루브릭이 없었다면 F26-H는 "노드 죽이면 DB 안 뜸"이라는 그럴듯하나 F10과 감별 불가능한 얕은 manifest로, F26-Q는 "디스크 채우면 eviction"이라는 이웃 피해(정체성)를 못 보는 껍데기로 승격됐을 것이다. 결론: 4종 모두 draft로 정직하게 게이트하되, `k8s.node` executor(R·H·P 파일 mode 포함)와 3종 신규 query(Pending 사유·DiskPressure·eviction)를 인프라 트랙(헌장 §5)으로 올리면 노드/K8s 도메인의 고품질 Class B 4연작으로 승격 가능하다.
</content>
</invoke>
