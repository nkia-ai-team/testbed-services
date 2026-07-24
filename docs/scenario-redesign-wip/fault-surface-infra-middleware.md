# 미들웨어 인프라 Fault Surface 지도 (Class B, DPM/KCM 관측 중심)

대상: PostgreSQL 16 / MySQL 8.0 / Oracle 23ai Free / Kafka 3.9(KRaft) / Redis 7 + 보조(mockserver·nginx).
스택 근거: 전부 k8s 매니페스트 실측(file:line) + 109 라이브 read-only 확인(2026-07-24, `KUBECONFIG=/root/tb-kubeconfig`).
**클래스**: 본 문서는 전부 **Class B — 미들웨어 인스턴스 자체의 인프라 결함**(자원 상한·SPOF·설정 미튜닝·영속성 부재)이다.
앱 코드가 결함인 Class A(F01 lock/F02 index/F04 outbox/F20 slowquery 등)와의 경계는 각 후보의 "경계" 항목에 명시한다.

주의: 이 테스트베드는 앱과 마찬가지로 **미들웨어에도 주입용 엔드포인트가 없다.** 주입은 rca-scenario-runner의 registry executor로만 한다.
따라서 각 후보는 "표면이 실재하는가"와 "그 표면을 때릴 injector가 실재하는가"를 분리 판정한다.

---

## 0. 미들웨어 인벤토리 + 실측 상수

| 미들웨어 | 도메인 | kind / replicas | 이미지 | CPU/Mem limit | PVC(영속) | 핵심 튜닝 파라미터(실측) | 라이브 노드 |
|---|---|---|---|---|---|---|---|
| **PostgreSQL 16** | commerce | StatefulSet / **1** | postgres:16-alpine | **500m / 512Mi** | pgdata **5Gi** | `max_connections=100`(기본), `shared_buffers=128MB`(기본), `superuser_reserved=3`, `pg_stat_statements` preload | tb-w1 |
| **MySQL 8.0** | food | StatefulSet / **1** | mysql:8.0 | 500m / **1Gi** | mysqldata 5Gi | `max_connections=151`(기본), `innodb_buffer_pool=128MB`(기본), charset+`log-error` 외 무튜닝 | tb-w3 |
| **Oracle 23ai Free** | banking | StatefulSet / **1** | gvenzl/oracle-free:23-slim | **2000m / 4Gi** | oracledata **10Gi** | `sessions=322`,`processes=200`,`sga_target=1.5GB`,`pga=512MB`, 단일 PDB **FREEPDB1** | tb-w1 |
| **Kafka 3.9 (KRaft)** | commerce/food/banking 각 1 | StatefulSet / **1** | apache/kafka:3.9.0 | 800m / 1536Mi | kafka-data 5Gi | broker+controller 겸임(SPOF), `num.partitions=3`, **모든 토픽 RF=1 / ISR=1**(라이브 확인), `min_isr=1`, auto-create=true | commerce:w2·food:w1·banking:w3 |
| **Redis 7** | commerce(cart 전용) | **Deployment / 1** | redis:7-alpine | 300m / 256Mi | **없음(영속성 0)** | 설정 파일 없음 → `maxmemory=0`(무제한),`noeviction`, **RDB/AOF 없음(재시작=전멸)** | tb-w2 |
| mockserver | commerce·food | Deployment / 1 | mockserver/mockserver:latest | 300m / 768Mi | 없음 | 외부 PG 모사(:1080). mock.expectation 주입 표면 | tb-w1 |
| nginx | commerce·banking | Deployment / 1 | nginx:alpine | 200m / 128Mi | 없음 | 엣지(:30080). WPM 프로브 표면 | commerce:w3·banking:w2 |

**앱 풀 합계 vs DB 상한 (인프라 과약정 계산, 핵심):**

| DB | 상한(usable) | 앱 Hikari `maximum-pool-size` 합 | 헤드룸 | 라이브 현재 접속 |
|---|---|---|---|---|
| commerce PG | **97** (100−3 reserved) | order10+product10+inventory10+payment10+cart20+pricing15+user10+shipping10 = **95** | **+2 (사실상 0)** | **62** (commerce 58 + bg 4), 대부분 minimum-idle |
| food MySQL | 151 | order15+restaurant10+payment15+dispatch10+notify≈10 = **≈60** | +91 (여유 큼) | — |
| banking Oracle | 322 sessions | account10+transfer15+ledger10+api≈10 = **≈45** | 여유 큼 (진짜 상한은 세션 아님) | — |

→ **연결 고갈은 commerce PG 고유 표면.** food/banking은 연결 상한 여유가 커 "자연 고갈" 불성립(§2-A 위장 금지 대상).
※ commerce의 order/product/inventory/payment는 application.yml에 hikari 블록이 없어 **Hikari 기본값 10**을 적용(fault-surface-commerce.md §0 실측과 일치).

---

## 1. 미들웨어별 인프라 표면

### 1.1 PostgreSQL 16 (commerce, 단일 인스턴스 8스키마) — blast radius 최대
- **연결 슬롯 고갈**: `max_connections=100`, reserved 3 → usable 97. 앱 풀 합 95 + DPM 외부 폴링 + 배치. 라이브 baseline 이미 **62 접속**. 부하 상승 시 cart(5→20)·pricing(5→15)·user(3→10)·shipping(3→10)가 max로 램프하면 +39 ≈ **97에 정확히 도달**. 초과 시 `FATAL: sorry, too many clients already` — **8개 스키마 전부** 신규 커넥션 거부(단일 인스턴스라 격리 없음).
- **메모리 상한 OOM**: cgroup limit **512Mi**, shared_buffers 128MB. 슬로우쿼리 대량 정렬/해시(work_mem×동시성) + 캐시가 512Mi를 밀면 **postgres-0 OOMKill → commerce DB 전면 정지**(재기동은 PVC로 데이터 보존되나 수십초 outage).
- **CPU 상한**: limit 500m(0.5코어). 공유 인스턴스라 한 스키마의 슬로우쿼리가 CPU를 먹으면 타 스키마 동반 지연(fault-surface-commerce.md C8과 같은 표면이나, 여기선 "인프라 CPU 상한"이 앵커).
- **디스크**: pgdata 5Gi. outbox/movements 누적으로 상승하나 느려 후순위.

### 1.2 MySQL 8.0 (food, flat 단일 DB 15테이블)
- 연결 상한 151 vs 앱 풀 ≈60 → **연결 고갈 표면 없음**(자연화 불가).
- 메모리 limit **1Gi**(PG의 2배) + buffer pool 128MB 기본. OOM 여지 PG보다 낮음.
- 진짜 표면은 인프라보다 **인덱스 부재(F02-P, Class A)** 쪽 — 미들웨어 인스턴스 결함으로는 빈약. **후보 없음** 판정(억지 생성 금지).

### 1.3 Oracle 23ai Free (banking, 단일 PDB 4서비스 공유)
- **Free 에디션 하드캡이 진짜 상한**: SGA 1.5GB·PGA 512MB(라이브)로 고정, cpu limit 2코어. 세션 322은 여유지만 **단일 PDB FREEPDB1을 api/account/transfer/ledger 4서비스가 공유** → 한 서비스의 무거운 쿼리가 SGA/CPU를 잠식하면 **banking 전 서비스 동반 저하**(blast radius = 도메인 전체).
- 기동이 무거워(healthcheck.sh, startup 60×15s) **재시작 비용이 큼** — pod 교란 시 수분 outage.
- 이것이 헌장 §0에서 언급된 **FS-12 후보**와 겹치는 지점(단일 PDB 공유). Oracle 자체를 조르는 직접 injector는 없음(§2 판정).

### 1.4 Kafka 3.9 KRaft (도메인별 단일 브로커) — SPOF
- **replicas=1 + broker/controller 겸임 + 전 토픽 RF=1/ISR=1**(라이브: commerce.orders/payments/inventory/shipping 모두 RF=1). → **브로커 1개가 죽으면 failover 없이 전 토픽 unavailable.** PVC 5Gi로 데이터는 보존되나 재기동 동안 발행/소비 전면 중단.
- KRaft combined라 controller까지 같이 죽어 메타데이터 쿼럼도 상실.
- 디스크 5Gi 소진 시 브로커 정지(retention 기본, 느림 → 후순위).

### 1.5 Redis 7 (commerce cart 전용) — 영속성 0
- **Deployment(PVC 없음) + persistence 설정 없음** → pod 재시작/스케일0 시 **캐시 전멸**. cart는 CB→DB fallback(cart-service 코드) 설계라 자가치유되지만, fallback 트래픽이 cart DB풀(20)로 몰림.
- maxmemory=0(무제한) + limit 256Mi → 대량 cart 키 적재 시 cgroup OOM 여지(단, cart 데이터 규모상 후순위).

### 1.6 보조(mockserver / nginx)
- mockserver: 외부 PG 모사. 이미 **mock.expectation** injector 표면(Class A성 — 외부 의존 거동 모사). 미들웨어 인프라 결함으로 새로 볼 것 없음.
- nginx: 엣지. WPM 프로브 대상이나 injector(wpm.probe)가 `live_supported:false` 껍데기.

---

## 2. 시나리오 후보 (트리거 / 전파 / 증상 / root vs 증상 / 인프라 앵커 / 주입 판정 / Class A 경계)

### B1. ★ commerce PG 연결 슬롯 고갈 → 8스키마 전면 신규요청 거부
- **트리거**: 지속 부하로 cart·pricing·user·shipping 풀이 동시에 max 램프 → 총 접속 97 초과. (또는 슬롯 소비 injector로 결정화)
- **전파**: PG usable 97 소진 → 신규 `getConnection` 전부 `FATAL: too many clients` → **모든 스키마**(order/payment/inventory/…)의 커넥션 획득 실패 → 전 서비스 5xx.
- **증상**: 특정 서비스가 아니라 commerce **전 서비스가 동시에** DB 연결 오류. DPM: `active_connections≈100 flat`, `connection_errors` 급증.
- **root vs 증상**: root=**PG max_connections 인프라 상한(미튜닝)**. 증상=전 서비스(오진 유발 — 앱마다 "DB 안 붙음"만 보임).
- **인프라 앵커**: 10-postgres.yaml(무튜닝 → 기본 100) + 앱 풀 합 95 실측. 라이브 baseline 62.
- **주입 수단 판정**: 🟡 **부분 갭**. 자연 도달은 경계선(95 vs 97)이라 비결정적. 결정화하려면 **"유휴 커넥션 N개를 잡는 슬롯 소비 injector"**가 필요(현 db.lock은 row-lock만, bare 커넥션 홀드 아님) → **신규 injector 갭**. 또는 max_connections를 낮춰 config-fault로 만들면 pod 재시작 필요.
- **Class A 경계**: F03/F12(order·payment Hikari 앱풀 고갈, Class A)는 **앱 풀이 원인**. B1은 **PG 서버 상한**이 원인 — 앱 풀을 아무리 키워도 PG가 못 받음. 증상 범위도 다름(A=한 서비스, B1=전 스키마 동시).

### B2. ★ commerce PG 메모리 상한 OOMKill → commerce DB 전면 outage
- **트리거**: PG 컨테이너 memory limit(512Mi)를 조여(예 320Mi) 슬로우쿼리 부하와 겹치면 cgroup OOM.
- **전파**: postgres-0 OOMKill → 재기동(수십초) 동안 8스키마 전부 접속 불가 → commerce 전면 정지 → 복구 후 자동 회복(PVC 보존).
- **증상**: KCM `container_last_termination_reason=OOMKilled`, `container_restart_count↑`; 전 서비스 순간 5xx 후 회복. DPM 접속 끊김.
- **root vs 증상**: root=**PG 메모리 인프라 상한**. 증상=전 서비스 순단.
- **인프라 앵커**: 라이브 limit 512Mi 확인. shared_buffers 128MB.
- **주입 수단 판정**: 🟢 **실재(확장 필요)**. `k8s.resource` executor가 memory limit 패치+OOM 유도로 실재(현재 F05-R=payment 대상, allowlist `commerce-namespace`만). **타깃을 testbed-postgres로 확장**(allowlist/파라미터)하면 그대로 사용. 신규 코드 아님.
- **Class A 경계**: F05-R(payment 앱 pod OOM, Class B이나 앱 컨테이너)와 표면 동일 계열이지만 **대상이 공유 DB**라 blast radius가 도메인 전체 — 격이 다름.

### B3. ★ Kafka 브로커 다운(SPOF, RF=1) → 이벤트 백본 전면 중단
- **트리거**: kafka statefulset scale 0 또는 pod 삭제(도메인별 단일 브로커).
- **전파**: RF=1/ISR=1이라 failover 없음 → 발행(OutboxRelay `send().get()`) 블로킹/실패, 소비 중단. 동기 결제 경로는 정상, **비동기만 전멸**(배송 미생성·알림 정지·상태 사후보정 정지). 재기동 시 PVC로 복구·리밸런싱.
- **증상**: KCM `kafka_consumer_lag↑`(재기동까지), `deployment_available_replicas(kafka)=0`, outbox 미발행 누적. 5xx 없음(조용한 고장).
- **root vs 증상**: root=**Kafka 인스턴스(SPOF)**. 증상=shipping/notification 무행동 + outbox 백로그.
- **인프라 앵커**: 12-kafka.yaml replicas:1, RF=1(라이브 전 토픽 확인).
- **주입 수단 판정**: 🟢 **실재**. `kafka.control` executor(F04-R)가 kafka pod / consumer group 제어로 실재. 브로커 스케일0/드레인 지원.
- **Class A 경계**: **F04(outbox relay, Class A/P5)** 는 "relay 코드가 조용히 async 실패"가 원인 — **브로커는 살아있음**. B3는 **브로커 인스턴스 자체가 죽음**(인프라). 감별점: B3는 `available_replicas(kafka)=0`+lag가 재기동 후 catch-up, F04는 브로커 정상인데 relay만 정지(lag는 오히려 0 수렴, 헌장 §5 정정과 일치).

### B4. ★ banking Oracle 단일 PDB 공유 포화 → banking 전 서비스 저하
- **트리거**: Oracle cpu limit(2코어) 또는 Free SGA(1.5GB) 압박 하에서 4서비스가 FREEPDB1 동시 부하.
- **전파**: 단일 PDB가 SGA/CPU 상한에 걸려 쿼리 큐잉 → api/account/transfer/ledger **전부** 지연(공유 자원 오염). 재시작은 무거워(startup 수분) 회복 지연.
- **증상**: banking 전 서비스 DPM 쿼리 지연 동반 상승, Oracle CPU/메모리 KCM 상한 근접. 특정 서비스 원인 안 보임.
- **root vs 증상**: root=**Oracle Free 인프라 상한 + 단일 PDB 공유**. 증상=banking 도메인 전반.
- **인프라 앵커**: 라이브 sga_target=1.5GB, cpu limit 2, 단일 PDB FREEPDB1(4서비스 공유).
- **주입 수단 판정**: 🟡 **부분 갭**. Oracle 내부를 직접 조르는 injector 없음. `k8s.patch`(cpu limit 하향)로 Oracle cpu를 조이면 간접 유도 가능하나 allowlist는 commerce 전용 → **allowlist 확장 필요**. host.stress(노드)로도 간접 가능하나 노드 교란은 별 트랙(infra-k8s-node).
- **Class A 경계**: F01-P(Oracle **row-lock**, Class A/db.lock)는 특정 행 락이 원인 — Oracle은 건강. B4는 **Oracle 인스턴스 자원 상한**이 원인, 락 아님. FS-12(헌장 언급)와 동일 표면이므로 **중복 주의 — B4로 통합 권고**.

### B5. Redis 다운 → cart 캐시 전멸 → DB fallback 폭주 (자가치유)
- **트리거**: redis deployment scale 0(영속성 0이라 재기동해도 캐시 비어있음).
- **전파**: cart-service CB open → getCartFromDb fallback → cart DB풀(20)로 트래픽 이동. Redis health 집계 제외라 pod 재시작 안 됨.
- **증상**: cart 초기 지연 스파이크 후 CB open되며 안정. cart DB 부하 상승.
- **root vs 증상**: root=**Redis 인스턴스(영속성 없는 캐시)**. 증상=cart 일시 지연.
- **인프라 앵커**: 11-redis.yaml Deployment(PVC 없음)+무설정.
- **주입 수단 판정**: 🟢 **실재**. `cache.control` executor(F11-R/G)가 redis scale 0으로 실재.
- **Class A 경계 / 기존 중복**: **이미 F11-R로 존재(헌장 §3.1 KEEP 14 포함).** 신규 아님 — 인벤토리 정합성 확인용으로만 기재. C7(cart CB fallback 코드, Class A)과의 경계는 "Redis 인스턴스 부재(B5) vs cart 코드의 CB 거동(A)".

### B6. Kafka/DB 디스크(PVC 5Gi) 소진 → 인스턴스 정지 (후순위)
- **트리거**: 장기 누적(kafka retention·outbox·movements)으로 5Gi PVC 소진.
- **판정**: 표면 실재하나 **주입 injector 없음** + 도달이 느려 시나리오 부적합. **후보 제외**(기록만).

---

## 3. 요약표

| # | 미들웨어 | 인프라 앵커 | root cause | 1차 증상 | 주입 수단 | 판정 | Class A 경계 |
|---|---|---|---|---|---|---|---|
| **B1** | commerce PG | max_conn=100 vs 풀합 95 | PG 연결 상한 | 8스키마 전면 5xx | 슬롯소비 injector(신규) | 🟡 injector 갭 | F03/F12=앱풀 |
| **B2** | commerce PG | mem limit 512Mi | PG OOMKill | commerce 전면 순단 | k8s.resource(확장) | 🟢 확장이면 실재 | F05-R=앱pod |
| **B3** | Kafka(3도메인) | replicas1·RF1·ISR1 | 브로커 SPOF | 비동기 백본 정지 | kafka.control(F04-R) | 🟢 실재 | F04=relay코드 |
| **B4** | banking Oracle | Free SGA1.5G·단일PDB | Oracle 자원 상한 | banking 전 서비스 저하 | k8s.patch(확장)/host.stress | 🟡 부분 갭 | F01-P=row-lock |
| **B5** | commerce Redis | 영속성0·Deployment | 캐시 인스턴스 부재 | cart 일시 지연 | cache.control(F11) | 🟢 **기존 F11-R** | C7=CB코드 |
| B6 | Kafka/DB 디스크 | PVC 5Gi | 디스크 소진 | 인스턴스 정지 | 없음 | ❌ 제외 | — |

**신규 채택 권고 3건**: B1(PG 연결 고갈), B2(PG OOM), B3(Kafka SPOF), B4(Oracle 공유 포화). B5는 기존 F11-R와 동일 → 중복.
**공통 능력 갭 2건**: (1) **bare 커넥션 슬롯 소비 injector**(B1), (2) **k8s.resource/k8s.patch allowlist를 DB pod(postgres/oracle)로 확장**(B2·B4 — 신규 코드 아닌 계약 확장).
