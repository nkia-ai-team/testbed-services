# K8s / 노드 / 디스크 계층 Fault Surface 지도 (KCM·SMS 관측 도메인, Class B)

대상: `testbed-services`의 3개 도메인 k8s 매니페스트(`{commerce,food-delivery,core-banking}/k8s/*.yaml`) 전수 + 라이브 kubeadm 클러스터(tb-cp/w1/w2/w3) 읽기 전용 실측(2026-07-24, `KUBECONFIG=/root/tb-kubeconfig`).
목적: Class B(인프라 앵커) 시나리오 후보 도출. **앵커는 앱 코드가 아니라 노드·자원·probe·PV·hostPath 같은 인프라 지점**과 그 조작 수단이다(헌장 §2 Class B).
근거는 전부 `파일:라인` 또는 실행한 kubectl 명령 + 결과 요지. 추정은 "추정"으로 명시.

---

## 0. 전역 실측 상수 (모든 후보의 인프라 토대)

| 항목 | 값 | 근거 | 함의(fault surface) |
|---|---|---|---|
| 워커 노드 | tb-w1/w2/w3 각 **4 vCPU / 11.9Gi allocatable**, tb-cp 2 vCPU / 3.9Gi(control-plane) | `kubectl get nodes` capacity/allocatable | 앱은 워커 3대에만 배치. 노드 1대 = 전체 워커 용량의 1/3 |
| 모든 워크로드 replicas | **1** (예외 없음) | 모든 Deployment/StatefulSet `replicas: 1` (예: 20-order:16, 10-postgres:33, food 20-order:9, banking 20-api:9) | 파드 1개 소실 = 서비스 소실. 냉장고 없는 SPOF 22종+DB 3종 |
| nodeSelector / affinity | **전무** | 전 매니페스트 grep 무결과 | 스케줄러가 도메인 무관하게 흩뿌림 → 노드 blast radius가 도메인 경계를 넘음(§1) |
| 앱 서비스 자원(Java) | requests **200m/512Mi**, limits **500m/1Gi** (전 도메인 균일) | 20-order:108-113 등 22개 동일 | CPU 500m 상한이 부팅·부하 시 throttle 천장 |
| 노드 limits 총합 (overcommit) | tb-w1 **CPU 172% / MEM 116%**, w2 145%/94%, w3 137%/91% | `describe node` Allocated resources | limits 합이 allocatable 초과 → 실사용 급증 시 노드 메모리 고갈 여지 |
| DB 자원 | postgres 200-500m/256-**512Mi**(작음), mysql 200-500m/512Mi-1Gi, oracle 500-2000m/2-4Gi | 10-postgres:65-70, food 10-mysql:40-44, banking 10-oracle:37-41 | postgres 메모리 여유 극소 |
| hostPath (전 앱 파드) | `/opt/polestar10/apm` **type: Directory** → `/opt/apm` RO 마운트 | 20-order:25-29,46-49; 22개 전부 동일 | `type: Directory`는 kubelet이 디렉토리 실재를 강제 → 없으면 FailedMount |
| javaagent 의존 | `JAVA_TOOL_OPTIONS=-javaagent:/opt/apm/opentelemetry-javaagent.jar` | 20-order:64-65 (전 앱 동일) | jar 소실 시 JVM 부팅 실패(CrashLoop) |
| imagePullPolicy | 전 앱 이미지 **Never** | 20-order:33 등 | 이미지 없는 노드로 재스케줄 시 ErrImageNeverPull |
| ephemeral-storage req/limit | **미설정(0/0)** | `describe node` Allocated `ephemeral-storage 0(0%)` | 로그/tmp 무제한 → 노드 루트 fs 포화 → DiskPressure eviction 가능 |
| StorageClass | `local-path`(rancher, default, **WaitForFirstConsumer**, expansion 불가) | `kubectl get storageclass` | PV = 노드로컬 hostPath. 파드가 PV 있는 노드에 고정됨 |
| PV 보유 | DB 3종 + kafka 3개만. postgres 5Gi / mysql 5Gi / oracle 10Gi / kafka 각 5Gi. **앱 서비스는 PV 없음(overlay/emptyDir)** | `kubectl get pv`, PVC 6개 | 상태는 DB·kafka에만. 앱은 stateless지만 로그는 노드 루트로 감 |
| local-path provisioner 위치 | **tb-w2 단독** (`local-path-provisioner-...` on tb-w2) | `kubectl get pods -A -o wide` | 프로비저너 자체도 replicas=1 SPOF |
| DiskPressure/MemoryPressure | 현재 3워커 전부 **False**(정상) | `describe node` Conditions | 실측 시점 클러스터 건강. 후보는 유도 대상 |

핵심 인프라 결함 3가지(후보 대부분의 뿌리):
1. **replicas=1 + affinity 전무 + 흩어진 배치** → 노드 1대 소실이 3개 도메인 다중 서비스를 동시에 죽인다(§1).
2. **local-path PV 노드고정** → DB 파드는 PV 있는 노드를 떠날 수 없다. 노드 상실 = 재스케줄 불가 = 영구 불가용.
3. **hostPath type:Directory + javaagent + imagePullPolicy:Never** → 노드 로컬 자산(apm 디렉토리·jar·이미지)이 파드 기동의 숨은 전제. 소실/부재 시 그 노드의 파드가 못 뜬다.

---

## 1. 노드 / 워크로드 배치 실측 (라이브)

`kubectl get pods -A -o wide` 실측 결과, 배치는 **도메인별로 묶이지 않고 흩어져 있다**(스케줄러 임의 배치, affinity 없음). CLAUDE.md의 "tb-w1=commerce" 메모는 **실측과 불일치(스테일)** — 아래가 현재 실상.

| 노드 | commerce | food | banking | 인프라/DB |
|---|---|---|---|---|
| **tb-w1** | notification, external-pg-mock, **postgres-0** | dispatch, **kafka-0**, order, restaurant | api, **oracle-0** | — |
| **tb-w2** | cart, gateway, **kafka-0**, redis, shipping | payment | ledger, nginx, transfer | **local-path-provisioner** |
| **tb-w3** | nginx, **order, payment, pricing, product, user** | **mysql-0**, notify | account, **kafka-0** | — |

관측되는 blast radius(노드별 소실 시):
- **tb-w3 소실** = commerce 핵심 5종(order·payment·pricing·product·user) + food DB(mysql-0) + food notify + banking account 동시 상실 → **가장 치명적**(commerce 결제/주문 전멸 + food DB 소실).
- **tb-w1 소실** = commerce postgres-0(commerce 전 서비스 DB) + banking oracle-0(banking 전 서비스 DB) + food kafka+order+restaurant → **2개 도메인 DB 동시 상실**.
- **tb-w2 소실** = commerce gateway(commerce 단일 진입점) + kafka + redis + shipping + banking transfer + local-path-provisioner → commerce 진입 차단 + 향후 PV 프로비저닝 불가.

→ 어느 노드가 죽어도 **최소 2개 도메인**이 함께 영향. 이것이 §2의 N1/N2 근거.

---

## 2. 시나리오 후보

각 후보: 트리거 / 전파 / 증상 / root vs 증상 / 인프라 앵커 / 주입수단 판정 / 기존 F05·F09·F10·F13·F02-H 중복 여부.

기존 Class B가 이미 커버한 표면(중복 판정 기준):

| 기존 | 표면 | injector |
|---|---|---|
| F05-R | 컨테이너 **메모리 limit 축소 → OOMKill loop** | k8s.resource |
| F05-H | **liveness probe 오설정 → 재시작 loop** | k8s.probe |
| F05-P | **워커 노드 메모리 압박 → eviction**(host.stress memhog, tb-w2) | host.stress |
| F09-R | **워커 노드 CPU noisy neighbor**(tb-w3) | host.stress cpu |
| F09-H | **JVM heap 축소 → GC 압박** | k8s.env(-Xmx) |
| F09-P | **컨테이너 CPU limit throttle**(inventory 50m) | k8s.patch |
| F10-R | **DB PV 디스크 용량 full**(postgres, watermark) | host.stress watermark |
| F10-H/P, F02-H | **DB PV 디스크 IO 포화**(mysql/oracle/postgres, fio) | host.stress fio |
| F05-G | **잘못된 이미지 → rollout 실패** | k8s.lifecycle(set image) |
| F13-* | 네트워크/edge(현재 inert) | network.fault/wpm.probe(live=false) |

기존 injector 능력 요지(주입수단 판정 근거):
- `k8s.lifecycle` = **이미지 스왑만** 함(잘못된 이미지로 `set image`). 파드 삭제·노드 drain 기능 **없음**(executor 실측).
- `host.stress` = ssh로 워커에 fio/watermark/pressure/cpu/memhog. 프로세스·파일 스트레스만. 파드/노드 오케스트레이션 **없음**.
- `k8s.resource/probe/patch/env` = Deployment 스펙 왜곡(자연 고갈 아닌 **설정 결함** → 헌장 §2-A상 Class B로 정당).
- **파드 delete / 노드 cordon·drain / 노드 정지 / hostPath 조작 executor는 부재**(가장 큰 공통 갭).

---

### N1. ★ 워커 노드 상실 → 흩어진 배치가 만드는 다도메인 동시 장애 (SPOF blast radius)
- **트리거**: 워커 1대 소실(전원/네트워크/커널패닉). 재현은 `kubectl drain` 또는 GB10 libvirt `virsh destroy <node-vm>`.
- **전파**: 모든 워크로드 replicas=1 + affinity 전무 → 대체 파드 없음. Deployment가 재스케줄을 시도하나 N2(PV 고정)·N5(이미지 미적재)에 막혀 장기 outage.
- **증상**: §1 배치대로 **여러 네임스페이스가 같은 시각에 함께** Down. KCM `pod_ready` 다도메인 동시 급감, 동시 다발 alarm.
- **root vs 증상**: root=**노드(인프라)** 1점. 증상=commerce+food+banking 앱 다수. 감별 신호 = "서로 무관한 도메인들이 동시에" 죽는 것 자체가 노드 공유가 root라는 증거.
- **인프라 앵커**: replicas=1(전 매니페스트), nodeSelector/affinity 무(grep 무결과), 실측 배치표(§1).
- **주입수단 판정**: **능력갭(신규 executor 필요)**. `k8s.lifecycle`는 이미지 스왑만, `host.stress`는 프로세스만 → 노드 drain/파드 evict 수단 없음. 신설안: `k8s.node`(cordon+drain, 복구 시 uncordon) 또는 GB10 libvirt 훅. drain 방식이면 비파괴적·복구가능해 권장.
- **중복 여부**: **신규**. F05/F09/F10은 단일 서비스 성능저하(노드는 살아있음). N1은 노드 자체 소실 → 다중 서비스 완전 소실. 표면 겹치지 않음.

### N2. ★ DB StatefulSet local-path PV 노드고정 → 노드 상실 시 DB 영구 불가용
- **트리거**: DB 파드가 얹힌 노드 소실. postgres-0·oracle-0=tb-w1, mysql-0=tb-w3.
- **전파**: local-path PV는 노드로컬 hostPath(`/opt/local-path-provisioner/<pvc>`). PV가 노드에 묶여 있어 파드가 타 노드로 재스케줄 불가 → **Pending 영구**. 게다가 프로비저너가 tb-w2 단독이라 tb-w2 소실 시 신규 PV도 못 만듦.
- **증상**: DB `pod_ready=0` + 해당 도메인 전 서비스가 DB 커넥션 실패로 5xx. "디스크는 멀쩡한데 DB에 닿지 못함".
- **root vs 증상**: root=**노드 소실 + PV 노드고정**. 증상=도메인 전 서비스 5xx. 감별 = 디스크 지표는 정상인데 DB 파드가 Pending(스케줄 불가 event: `node(s) had volume node affinity conflict`).
- **인프라 앵커**: `kubectl get pv`(local-path, WaitForFirstConsumer), PVC 6개, provisioner=tb-w2 단독(실측), StatefulSet replicas=1.
- **주입수단 판정**: N1과 동일 노드/파드 executor 갭. 주의: **실제 노드 정지는 파괴적**(local-path 데이터 소실 위험) → drain으로 "재스케줄 불가" 상태만 재현하고 노드 파일은 보존하는 방식 필요.
- **중복 여부**: **신규**. F10(디스크 full/IO)은 DB가 살아있는 성능저하. N2는 DB 완전 소실 + 재스케줄 원천 불가(가용성/내구성 SPOF). mechanism 다름.

### N3. ★ hostPath `/opt/polestar10/apm`(type:Directory) 소실 → 노드 위 전 앱 파드 기동 실패
- **트리거**: 특정 워커의 `/opt/polestar10/apm` 디렉토리 또는 그 안 javaagent jar 소실(오퍼레이터 실수/정리 스크립트/마운트 해제).
- **전파**: (a) **디렉토리 소실** → `type: Directory` 검증 실패 → FailedMount → 파드 재기동·재배포 시 Pending. (b) **jar만 소실** → `-javaagent:/opt/apm/...jar` 로드 실패 → JVM 즉시 종료 → CrashLoopBackOff.
- **증상**: 해당 노드의 22종 앱 중 그 노드분이 재시작·재배포 시점에 일제히 못 뜸. 관측=KCM `container_restart_count`↑, mount 실패 event, 또는 CrashLoop.
- **root vs 증상**: root=**노드 로컬 hostPath 자산**. 증상=앱 파드 기동 실패(코드 무결함). 감별 = 이미지·설정 정상인데 노드 로컬 경로 event.
- **인프라 앵커**: 20-order:25-29(hostPath type:Directory), :46-49(mountPath /opt/apm RO), :64-65(javaagent). 22개 파드 공통.
- **주입수단 판정**: **배선갭(소규모)**. `host.stress`가 이미 ssh로 워커에서 파일 조작(fio/watermark) → 같은 프레임워크에 "dir/jar rename + trap 복구" mode 추가면 됨. 파괴적이라 복구 보장 필수.
- **중복 여부**: **신규**(각도 유사, 표면 별개). F05-G는 컨테이너 이미지 레이어 부재, N3은 노드 hostPath 마운트 자산 부재. 관측 신호도 다름(image pull failure vs mount/agent 실패).

### N4. ★ ephemeral-storage 무제한 → 앱 로그/tmp가 노드 루트 fs 포화 → DiskPressure eviction
- **트리거**: 로그 폭증(장애 상황 로그 스톰) 또는 임시파일 누적으로 노드 루트 fs 채워짐.
- **전파**: 전 파드 ephemeral-storage req/limit 미설정 → 앱 로그는 overlay/emptyDir(노드 루트 `/`). 한 파드가 루트를 채우면 kubelet `DiskPressure=True` → **노드 전역** eviction(초과사용·BestEffort 파드부터).
- **증상**: 특정 노드의 파드들 `Evicted` → 재스케줄 반복. 가해 파드가 아닌 이웃까지 쫓겨나는 오진 유발.
- **root vs 증상**: root=**노드 루트 디스크 포화(ephemeral 상한 부재)**. 증상=무관한 이웃 파드 eviction. 감별 = 노드 `DiskPressure` condition + eviction event(특정 PV 아님).
- **인프라 앵커**: `describe node` Allocated `ephemeral-storage 0(0%)`(전 파드 상한 없음), 앱에 PV 없음(§0).
- **주입수단 판정**: **배선갭만**. `host.stress` watermark mode를 **PV 디렉토리가 아닌 노드 루트 fs** target_dir로 신규 계약(상한·복구 포함)하면 기존 executor로 재현 가능.
- **중복 여부**: **신규**. F10-R(postgres PV watermark)은 PV 디렉토리를 채워 DB 디스크 full → DB 자신 영향. N4는 노드 루트를 채워 DiskPressure → **앱 파드 eviction**. 대상·mechanism 다름.

### N5. imagePullPolicy: Never + 노드별 사전적재 → 재스케줄 시 ErrImageNeverPull
- **트리거**: 파드가 이미지 미적재 노드로 재스케줄(노드 상실 후 rescheduling, 또는 수동 이동).
- **전파**: 전 앱 이미지 `imagePullPolicy: Never`(20-order:33). build-and-deploy.sh는 `ssh ctr import`로 **노드별** 주입(`IMPORT_SSH_NODES` 대상에만; commerce build-and-deploy.sh:69-71). 대상 노드에 이미지 없으면 `ErrImageNeverPull`.
- **증상**: 재스케줄된 파드가 ImagePullBackOff 유사(Never라 즉시 실패)로 기동 불가. N1을 증폭(노드 죽고 재스케줄해도 안 뜸).
- **root vs 증상**: root=**노드 로컬 이미지 캐시 부재 + Never 정책**. 증상=파드 기동 실패.
- **인프라 앵커**: 20-order:33, build-and-deploy.sh:69-75. **추정**: 이미지가 전 워커에 적재됐는지는 런타임 `IMPORT_SSH_NODES` 설정 의존이라 노드 접근 없이 확증 불가(node-level ctr 접근 금지) — "추정"으로 명시.
- **주입수단 판정**: N1 노드/재스케줄 executor 갭 공유. 단독 재현은 어렵고 N1의 후속 증상으로 관측하는 편이 자연스러움.
- **중복 여부**: **중복 위험 中**. F05-G(잘못된 이미지 스왑)와 관측(image pull failure)이 겹침. 각도(재스케줄 노출 vs rollout)만 다름 → 독립 시나리오화보다 **N1의 2차 증상**으로 편입 권고.

### N6. (관측 상수 — 시나리오화 보류) 노드 overcommit + 콜드스타트 폭풍
- **overcommit**(N4와 별개 각도): limits 합 116~172%(§0). 정상 워크로드가 limits까지 차오르면 노드 메모리 고갈로 커널 OOM이 무고한 이웃을 죽일 수 있음 → 그러나 §2-A상 인위 부하 없이 자연 유도 어렵고 F05-P(memhog eviction)와 표면 중복 → **글로벌 상수로만 기록, 별도 후보 보류**.
- **콜드스타트 폭풍**: 노드 재기동 시 다수 JVM 동시 부팅이 CPU 500m 상한에서 throttle → startupProbe 150s 초과 → 재시작 루프. F09-P(CPU throttle) 표면과 겹침 → N1의 복구 지연 요인으로만 기술 권고.

---

## 3. 요약표

| # | root cause(인프라 앵커) | 1차 증상 표면 | 주입수단 판정 | 기존 중복 | 강도 |
|---|---|---|---|---|---|
| **N1** | 워커 노드 소실(replicas=1·affinity無·흩어진 배치) | 다도메인 다중 서비스 동시 Down | **능력갭**: node drain/파드 evict executor 신설 | 신규(F05/F09/F10과 무관) | ★강 |
| **N2** | DB PV 노드고정(local-path) + 노드 소실 | DB Pending + 도메인 전 서비스 5xx | **능력갭**: N1과 동일(비파괴 drain) | 신규(F10과 mechanism 다름) | ★강 |
| **N3** | hostPath `/opt/polestar10/apm`(type:Directory)·jar 소실 | 노드 위 앱 파드 FailedMount/CrashLoop | **배선갭(소)**: host.stress에 dir/jar rename+복구 mode | 신규(F05-G와 표면 별개) | ★강 |
| **N4** | ephemeral 상한 부재 → 노드 루트 fs 포화 | 노드 DiskPressure → 이웃 파드 eviction | **배선갭**: host.stress watermark를 노드 루트 대상 신규 계약 | 신규(F10은 PV 대상) | ★강 |
| **N5** | imagePullPolicy:Never + 노드별 미적재 | 재스케줄 파드 ErrImageNeverPull | 능력갭(N1 공유) | 중복 中(F05-G) → N1 편입 권고 | 중 |
| N6 | overcommit / 콜드스타트 throttle | 노드 OOM·부팅 재시작 루프 | (자연유도 난) | F05-P·F09-P 중복 | 보류 |

**공통 능력갭(별도 인프라 트랙)**: 현재 executor 중 **파드 삭제·노드 cordon/drain·노드 정지** 수단이 전무. N1/N2/N5가 여기에 종속. 헌장 §5의 "제어점 부재" 계열로, `k8s.node`(비파괴 drain+uncordon 복구) 또는 GB10 libvirt 훅 신설이 이 3개를 동시에 연다. N3/N4는 기존 host.stress 프레임워크 확장(배선)만으로 실현 가능해 우선순위가 높다.
