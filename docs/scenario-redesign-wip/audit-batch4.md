# 배치4 감사: F10~F12 계열 (12개)

기준: manifests/*.yaml + catalog.json + registry/{profiles,scenario-metadata}.json + profiles/*executor* 실측 + 3도메인 fault-surface 지도.

핵심 실측 (executor 실체):
- `host.stress` → `host_stress_executor.py`. CONTRACTS = {F02-H, F10-R, F10-H, F10-P, F15-P} + ladder{F09-R, F05-P}. **F10-G 계약 없음** → 호출 시 "no verified bounded host-stress contract"로 실패.
- `network.fault`·`business.fault` → 둘 다 `profile-executor.sh` (generic). **`live_supported=false` → plan-only 스텁**. 실주입 코드 없음("live refused"). F12-R/P/G, F11-P 전부 inert.
- `k8s.patch` → `k8s_patch_executor.py`. **CPU limit(baseline/fault_cpu_limit)만 패치**. allowlist=[F09-P, F12-H]뿐. **F11-H는 allowlist에 없고 파라미터/레벨도 없음** → validate "not allowlisted"로 거부. 게다가 pool 결함을 CPU 패치로 낼 수 없음.
- `cache.control` → `cache_control_executor.py`. redis replicas 1→0, dependent=testbed-cart. F11-R/F11-G 실파라미터 존재, live_enabled=true. 실체 있음.
- answer-key(scenario-metadata.json)는 **ready 3종(F11-R, F11-G, F12-H)만 존재**. 나머지 9개(blocked/partial)는 answer-key 자체가 없음.

worker map: w1=.184=commerce / w2=.11=food / w3=.14=banking.

| id | 앵커 패턴 | answer-key 정합성 | 코드/실현 앵커 | 판정 | 한 줄 근거 |
|---|---|---|---|---|---|
| f10-r-postgres-volume-full | 인프라(디스크/볼륨 워터마크). root=PG PVC 포화 | metadata answer-key **없음** | executor watermark 모드 존재(host .184=w1 ✓, manifest worker-w1 ✓)이나 PVC 경로는 미검증 하드코딩, prereq "PG PVC path" unresolved, live 불가 | 🟡 | 능력·경로는 있으나 prereq 미검증+answer-key 부재로 재현 미확정 |
| f10-h-mysql-io-saturation | 인프라(fio IO 포화). root=MySQL 디스크 IO | answer-key **없음** | executor fio 계약 존재하나 **host=.14(banking w3) vs manifest worker-w2(.11) 불일치**, PVC 미검증, blocked | 🟡 | host↔worker 좌표 모순 + prereq 미해결 |
| f10-p-oracle-io-saturation | 인프라(fio IO 포화). root=Oracle 디스크 IO | answer-key **없음** | **host=.184(commerce w1) vs manifest worker-w3(.14) 불일치**, oracle PVC 경로 미검증, blocked | 🟡 | banking IO인데 host가 commerce 노드 — 좌표 모순 |
| f10-g-log-partition-watermark | 인프라 가드레일(별도 로그 마운트가 흡수→음성) | answer-key **없음** | **host.stress CONTRACTS에 F10-G 없음** → executor 계약 부재, no-load, "separate log mount proof" 미증명 | ❌ | 가드레일 개념만 있고 executor 계약·마운트 증거 전무 |
| f11-r-redis-down-fallback-overload | P4 인접(cart Redis CB→DB fallback) + 용량과부하 | cause="Redis outage amplified by overloaded PG fallback" — 코드정합(cart pool 20, CB fallback getCartFromDb) | cache.control 실executor+surge.js, live/calibration, 실파라미터 O. 단 "overload"는 자가치유 설계(C7 "DB부하로 안정")와 긴장—calibration으로 실증 필요 | ✅ | root=Redis+fallback 정확, 실executor·실부하. overload는 calibration 의존 |
| f11-h-cart-pg-pool-fault | P1/P2(풀 고갈) 의도 | answer-key **없음** | **k8s.patch allowlist 밖+파라미터 없음→executor 거부**. k8s.patch는 CPU만 패치, pool 결함 주입 불가. prereq "cart pool boundary" unresolved | ❌ | 선언된 주입수단(k8s.patch)으로 cart PG풀 결함을 낼 방법이 없음 |
| f11-p-stale-promotion-cache | P5 인접(데이터 드리프트) 의도 | answer-key **없음** | business.fault=inert 스텁(live_supported=false). pricing 프로모션은 **인메모리 캐시(DB 미조회, fault-surface pricing)** → staleness 주입 API 부재. "stale-cache API/fault build" 미구축 | ❌ | 주입기 스텁 + 대상 캐시가 인메모리라 주입 표면 없음 |
| f11-g-redis-down-absorbed | 인프라 가드레일(Redis 장애를 DB fallback이 완전 흡수=음성) | cause="No incident; absorbed by DB fallback" — **fault-surface C7과 정확 일치**(CB open→getCartFromDb, cart pool 20 흡수, Redis health 집계제외) | cache.control 실executor(replicas 1→0), live/evaluation, 성공기준=business_ok=true·pod_ready | ✅ | 코드 설계 그대로의 음성 시나리오, 실executor·live |
| f12-r-physical-interface-down | 인프라/네트워크. root=물리 NIC down | answer-key **없음** | network.fault=inert 스텁(live 불가), external-57 물리 인터페이스 down은 현 인프라에서 실현 불가(OOB 복구 미보장), prereq unresolved | ❌ | 주입기 스텁 + 물리 NIC 제어 수단 없음(알려진 갭) |
| f12-h-pod-cpu-network-lookalike | 인프라(CPU throttle) — 네트워크 장애 룩얼라이크 함정 | cause="Product pod CPU throttling", 구분증거=throttling有·network_error=0 — 코드/메트릭 정합 | k8s.patch F12-H 실파라미터(product 500m→250/100/50m), live/calibration, 성공기준이 network_error=0·order_p95<200(blast 격리)로 함정 잘 설계 | ✅ | 실executor·live, 룩얼라이크 판별기준 견고 |
| f12-p-cross-domain-packet-loss | 인프라/네트워크. root=크로스도메인 패킷로스 | answer-key **없음** | network.fault 스텁, **transport="unresolved"**, prereq "CNI/NET_ADMIN edge" 미해결 → 주입 위치조차 미정 | ❌ | 주입기 스텁 + transport 미정, 실현 불가 |
| f12-g-unrelated-flow-trap | 인프라/네트워크 가드레일(무관 플로우가 오탐 유발 안 함) | answer-key **없음** | network.fault 스텁, no-load, prereq "SNMP/NetFlow source" 미확보(현 인프라에 NMS/NetFlow 없음) | ❌ | 관측원(SNMP/NetFlow) 부재로 시나리오 성립 불가 |

## 집계
- ✅ 탄탄: **3** (F11-R, F11-G, F12-H)
- 🟡 의심: **3** (F10-R, F10-H, F10-P)
- ❌ 근거없음/실현불가: **6** (F10-G, F11-H, F11-P, F12-R, F12-P, F12-G)

## 가장 문제되는 Top 3
1. **F12 네트워크 3종(R/P/G) 전멸** — network.fault가 inert plan-only 스텁이고 물리 NIC down/크로스도메인 패킷로스/NetFlow 오탐은 현 인프라에서 주입·관측 수단이 아예 없음(F12-P는 transport조차 "unresolved"). 메모리의 "네트워크 F12=갭"을 실측 확인. answer-key도 전무.
2. **F11-H cart-pg-pool-fault** — 선언 주입기 k8s.patch가 CPU limit만 패치하고 F11-H는 allowlist 밖(파라미터/레벨 0). CPU 패치로는 cart의 PG 커넥션풀 결함을 원리적으로 낼 수 없음 → 이름과 수단이 불일치, executor가 거부. answer-key 없음.
3. **F11-P stale-promotion-cache** — business.fault가 inert 스텁인 데다, 대상인 pricing 프로모션이 인메모리 캐시(DB 미조회)라 staleness를 주입할 표면 자체가 없음. "stale-cache API/fault build" 미구축.

## 부수 발견
- **answer-key 커버리지 결함**: blocked/partial 9개는 scenario-metadata.json에 항목이 없어 정답(root cause)을 검증할 대상조차 없음. blocked라도 목표 root는 명시돼야 감사가능.
- **F10-H/F10-P host↔worker 좌표 모순**: executor CONTRACTS의 host IP(F10-H=.14, F10-P=.184)가 manifest injection.location(worker-w2, worker-w3) 및 도메인→worker 맵과 어긋남. F10 PVC 경로는 전부 미검증 하드코딩.
- **registry vs executor allowlist 불일치**: profiles.json의 host.stress `allowed_scenarios`=[F09-R,F05-P]인데 executor CONTRACTS엔 F10-R/H/P·F02-H·F15-P도 존재. build_invocation이 registry profile을 `{}`로 넘겨 검증 → registry 화이트리스트가 host.stress에 미강제.
- 양성 3종은 모두 commerce 전용(cart Redis / product CPU). food/banking 계열(F10-H/P, F12 등)은 실현 0 — 배치4의 도메인 커버리지는 commerce에 편중.
