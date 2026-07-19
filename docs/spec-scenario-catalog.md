---
title: AIOps 시나리오 카탈로그
status: Draft
owner: project
last_reviewed: 2026-07-15
tags:
  - scenario
  - testbed
  - aiops
  - incident
summary: 이상감지·이벤트 클러스터·인시던트 격상·RCA를 폭넓게 검증하기 위한 15개 사례군과 64개 시나리오 후보를 정의한다.
---

# AIOps 시나리오 카탈로그

이 문서는 [시나리오 설계](spec-scenario-design.md)의 규칙을 실제 후보군으로
전개한다. 적용 범위는 **이상감지 → 이벤트 클러스터 → 인시던트 격상 → RCA**이며
챗봇은 포함하지 않는다. 이 문서는 시나리오의 의미와 포트폴리오를 정의하고,
부하량·리소스 제한·지속시간·중단 임계치는 테스트베드 총용량 실측 후 각 YAML에
확정한다.

## 1. 카탈로그 구성 원칙

카탈로그는 원인 유형만 채우지 않는다. 다음 축을 함께 교차한다.

- **원인 계열:** APM, DPM, SMS, NMS, KCM, WPM과 Kafka/outbox·change·cache·외부 의존.
- **관측 현상:** 메트릭 변화, 로그 신규 패턴·급증, trace latency·error chain,
  lifecycle/change, 네트워크 flow/trap, 사용자 여정 phase.
- **사건 구조:** 단일 인과사슬, fan-out, cross-domain, merge, anti-merge,
  distractor, brownout, absorbed failure, change chain, flapping/reopen, multi-root.
- **영향 유형:** 지연, 실패, 부분 기능 불능, 비동기 SLA 위반, 데이터·업무 정합성
  오류, 실제 영향이 없는 guardrail.
- **유사 장애 관계:** 재발형, 유사 증상·다른 원인형, 부분 유사형.
- **업무 도메인:** commerce, food-delivery, core-banking, 도메인 횡단 경로.

각 사례군은 다음 네 역할을 기본으로 한다.

| 역할 | 의미 |
| --- | --- |
| `R` recurrence | 원인 메커니즘과 전파 구조는 같지만 대상·도메인·노이즈가 다른 재발형 |
| `H` same symptom, other cause | 사용자 증상은 유사하지만 실제 원인이 다른 사례 |
| `P` partial similarity | 전파의 일부만 같고 최초 원인 또는 영향 범위가 다른 사례 |
| `G` guardrail | 이상은 있으나 인시던트가 아니거나 단일 원인 확정이 부당한 사례 |

`R`도 서비스 이름만 바꾼 복제본이면 안 된다. DB 엔진, 진입 경로, 영향 범위,
노이즈 중 하나 이상을 달리한다. `H`와 `P`에는 서로를 구분할 결정적 관측 근거가
반드시 있어야 한다. 과거 장애 참고가 필요한 사례군은 `R`의 선행 사건을 먼저
실행·종결하고 후속 사건을 실행하는 시간 순서를 가진다.

## 2. 상태와 단계

| 상태 | 의미 |
| --- | --- |
| `ready` | 현재 서비스·주입 표면을 주로 재사용해 작성 가능 |
| `calibrate` | 표면은 있으나 영향 강도·대상·지속시간 실측 필요 |
| `prerequisite` | 앱·probe·관측·네트워크 표면 선행 구축 필요 |
| `defer` | 현재 구조에서는 정직한 인과관계가 성립하지 않아 보류 |

- **핵심 후보군:** F01~F08, 32개. 현재 표면을 주로 재사용할 수 있어 먼저
  상세화하기 좋은 후보다.
- **확장 후보군:** F09~F15, 32개. 호스트·스토리지·NMS·WPM·기능 정합성과 복합
  사건을 보강한다.
- 실제 **Phase 1 약 30개는 두 후보군 전체에서 선정**한다. APM/DPM/SMS/NMS/KCM/WPM
  각 계열과 주요 사건 구조를 빠짐없이 포함해야 하므로 F01~F08만으로 Phase 1을
  완료했다고 보지 않는다. NMS·WPM 선행조건이 해소되지 않으면 Phase 1의 해당
  계열 커버리지도 미완성으로 표시한다.

## 3. 핵심 후보군 — 8개 사례군, 32개

### F01. Checkout·결제 실패

| ID | 역할 | 시나리오 | 사용자 영향과 전파 | 결정적 구분 근거 | 상태 |
| --- | --- | --- | --- | --- | --- |
| F01-R | R | commerce inventory의 checkout 대상 hot row를 PG에서 장기 점유 | 재고 예약 대기 → order timeout·보상 → gateway checkout 실패 | PG blocking session·점유 SQL, 외부 PG·호스트 CPU 정상 | ready |
| F01-H | H | external PG mock이 결제 요청에 429 반환 | payment 빠른 거절 → order 실패 → checkout 실패 | outbound span 429, 내부 DB lock·pool 정상 | ready |
| F01-P | P | core-banking Oracle 계좌 row lock이 commerce payment 경로까지 전파 | transfer 대기 → payment timeout → cross-domain checkout 실패 | Oracle blocking session과 cross-domain trace, commerce PG 정상 | ready |
| F01-G | G | 짧은 외부 PG 지연을 retry·보상 로직이 흡수 | 하위 지연 흔적은 있으나 최종 주문·결제 상태와 사용자 응답 정상 | 최종 상태 정상, SLA 내 회복, 원인 확정에 필요한 실영향 없음 | calibrate |

### F02. DB 조회·실행계획 지연

| ID | 역할 | 시나리오 | 사용자 영향과 전파 | 결정적 구분 근거 | 상태 |
| --- | --- | --- | --- | --- | --- |
| F02-R | R | commerce 상품 검색 핵심 인덱스 제거 | PG full scan → product 검색 지연 → gateway 검색 timeout | TopSQL·plan·rows scanned 변화, lock·host IO 정상 | calibrate |
| F02-H | H | 같은 검색 지연을 worker 디스크 IO 포화로 발생 | 여러 SQL이 함께 느려져 상품·checkout tail latency 증가 | host disk busy·IO wait, 실행계획 불변 | calibrate |
| F02-P | P | food restaurant/menu 필터 인덱스 제거 | MySQL scan → 메뉴 조회 지연 → 주문 시작 지연 | MySQL TopSQL/plan 변화, dispatch·payment 정상 | calibrate |
| F02-G | G | 야간 집계 SQL만 느리고 온라인 경로는 정상 | 배치 지연은 있으나 사용자 검색·주문에는 영향 없음 | 느린 SQL이 batch session에 한정, 온라인 trace 정상 | ready |

### F03. 커넥션·실행 슬롯 고갈

| ID | 역할 | 시나리오 | 사용자 영향과 전파 | 결정적 구분 근거 | 상태 |
| --- | --- | --- | --- | --- | --- |
| F03-R | R | food payment의 특정 실패 경로에서 connection 미반환 | active connection 단조 증가 → pool 고갈 → 결제·주문 실패 | Hikari active 미회복, DB session 증가, lock 없음 | prerequisite |
| F03-H | H | order servlet thread pool 고갈 | 느린 요청이 thread를 점유해 DB 오류처럼 보이는 전체 endpoint 대기 | JVM thread·동시 trace 대기, DB pool·session 정상 | calibrate |
| F03-P | P | 축소된 commerce payment Hikari pool에 checkout surge 중첩 | pool pending → payment timeout → checkout 실패 | 설정 변경 이후 Hikari pending/timeouts, blocking SQL 없음 | calibrate |
| F03-G | G | pool 사용률은 높지만 pending·timeout 없이 처리 | 자원 사용 증가는 있으나 지연·실패·기능 영향 없음 | error/latency 정상, 부하 종료 후 즉시 정상화 | ready |

### F04. Kafka·outbox 비동기 처리 지연

| ID | 역할 | 시나리오 | 사용자 영향과 전파 | 결정적 구분 근거 | 상태 |
| --- | --- | --- | --- | --- | --- |
| F04-R | R | commerce shipping consumer 지속 정지 | 주문·결제 성공 후 배송 생성 SLA 위반 | orders publish·outbox 정상, consumer lag 증가, shipment 미생성 | prerequisite |
| F04-H | H | order outbox relay만 정지 | 주문 성공 후 배송·알림 지연은 같지만 Kafka consumer lag은 낮음 | outbox row 증가, publish 부재, broker·consumer 정상 | prerequisite |
| F04-P | P | banking ledger consumer 처리율 제한 | 이체 성공 후 원장 반영·대사 지연 | transfers lag 증가, transfer DB commit 정상, ledger row 부재 | prerequisite |
| F04-G | G | 짧은 broker 장애를 outbox가 흡수하고 SLA 안에 catch-up | produce 오류·적체는 있으나 최종 사용자 기능과 비동기 SLA 정상 | backlog 완전 소진, 누락·중복 없음, 인시던트로 볼 실영향 없음 | calibrate |

### F05. Pod 재시작·가용성 저하

| ID | 역할 | 시나리오 | 사용자 영향과 전파 | 결정적 구분 근거 | 상태 |
| --- | --- | --- | --- | --- | --- |
| F05-R | R | commerce payment memory limit 축소 + 결제 부하로 OOMKill loop | 단일 replica 재시작 → checkout 간헐 실패 | OOMKilled·restart count, host memory 정상 | calibrate |
| F05-H | H | liveness probe timeout 오설정으로 같은 restart loop 발생 | probe 실패 → kubelet restart → checkout 실패 | probe 실패·change onset, OOM·node pressure 없음 | calibrate |
| F05-P | P | 특정 worker node memory pressure로 여러 pod eviction | 재스케줄 공백 동안 commerce·food 일부 API 동시 실패 | node condition·eviction·pod relocation, 앱 heap 정상 | calibrate |
| F05-G | G | 잘못된 이미지 rollout이 실패하지만 기존 pod가 계속 서비스 | ImagePullBackOff·change 이벤트는 있으나 사용자 경로 정상 | 기존 ready replica 유지, 요청 성공률·지연 정상 | ready |

### F06. 외부 의존 실패

| ID | 역할 | 시나리오 | 사용자 영향과 전파 | 결정적 구분 근거 | 상태 |
| --- | --- | --- | --- | --- | --- |
| F06-R | R | commerce external PG mock이 read-timeout보다 오래 hang | outbound timeout·retry → payment 실패 → checkout 실패 | 외부 span 장기 대기, commerce DB·banking 정상 | ready |
| F06-H | H | payment 내부 DB lock으로 외부 timeout과 같은 결제 지연 발생 | DB 대기 후 외부 호출 전 단계에서 timeout | 외부 span 도달 전 대기, DB blocking session 존재 | ready |
| F06-P | P | food external PG가 일부 요청에만 429 반환 | 결제 일부 실패와 정상 요청이 공존하는 brownout | 빠른 429와 성공 응답 혼재, timeout 패턴 없음 | prerequisite |
| F06-G | G | 일시 외부 5xx를 retry/CB가 흡수 | 내부 retry 로그는 있으나 최종 주문 정상 | 최종 성공·중복 결제 없음, 영향 범위가 SLA 안 | calibrate |

### F07. Retry storm·Circuit breaker·Bulkhead

| ID | 역할 | 시나리오 | 사용자 영향과 전파 | 결정적 구분 근거 | 상태 |
| --- | --- | --- | --- | --- | --- |
| F07-R | R | banking transfer를 timeout 경계로 지연하고 checkout 부하 중첩 | payment retry 증폭 → thread/connection 점유 → checkout 광역 지연 | 진입량보다 빠른 east-west 호출 증가, 반복 outbound span | calibrate |
| F07-H | H | north-south 사용자 폭주로 동일 호출량 증가 | 모든 계층 호출 증가와 latency 상승 | 진입 요청과 내부 호출 증가 비율이 유사, retry 반복 없음 | calibrate |
| F07-P | P | pricing quote bulkhead 슬롯 포화 | 가격 산정 요청만 거절되고 browse는 정상인 기능 brownout | bulkhead reject, pricing trace 집중, DB pool 정상 | calibrate |
| F07-G | G | CB가 장애 하류를 격리해 핵심 경로를 보호 | 하류 오류·CB open은 있으나 blast radius와 사용자 영향 제한 | downstream 호출 감소, 인접 서비스 정상, fallback 성공 | ready |

### F08. 변경 선행 장애

| ID | 역할 | 시나리오 | 사용자 영향과 전파 | 결정적 구분 근거 | 상태 |
| --- | --- | --- | --- | --- | --- |
| F08-R | R | commerce pricing 버그 릴리스가 잘못된 quote 반환 | 배포 후 잘못된 가격 노출·주문 금액 오류 | 새 pod·새 버전에만 오동작, 자원 지표 정상 | prerequisite |
| F08-H | H | 배포 직후 외부 PG 장애가 우연히 발생 | 시간상 배포 뒤 checkout 실패지만 원인은 외부 의존 | old/new pod 모두 동일, 외부 span에서 오류 시작 | ready |
| F08-P | P | ConfigMap read-timeout 과소 설정 | 임계값 근처 요청만 간헐 실패하는 tail brownout | 설정 변경 이후 특정 latency 구간만 timeout | calibrate |
| F08-G | G | notification 배포와 무관한 banking lock이 동시 발생 | 실제 이체 장애와 무관한 change distractor 공존 | 변경 대상이 장애 경로와 topology상 무관, Oracle lock 직접 근거 | ready |

## 4. 확장 후보군 — 7개 사례군, 28개

### F09. Host CPU·JVM pause·컨테이너 throttle

| ID | 역할 | 시나리오 | 사용자 영향과 전파 | 결정적 구분 근거 | 상태 |
| --- | --- | --- | --- | --- | --- |
| F09-R | R | 특정 worker의 CPU noisy neighbor | 같은 node의 여러 pod latency 동시 상승 | host CPU needle과 pod placement cohort, DB·외부 의존 정상 | calibrate |
| F09-H | H | order JVM GC 압박으로 유사한 latency spike 발생 | 주기적 stop-the-world → order 전 endpoint 지연 | heap after-GC·used/limit과 주기성, host CPU 포화 없음 | calibrate |
| F09-P | P | inventory pod CPU limit 과소 배포 | inventory 예약만 지연되어 checkout 일부 실패 | container throttle·limit, 같은 host 다른 pod 정상 | calibrate |
| F09-G | G | batch 프로세스 CPU 상승이 사용자 경로에 영향 없음 | CPU 이상은 있으나 온라인 trace·error 정상 | batch process만 상승, SLA·서비스 상태 정상 | ready |

### F10. 디스크·스토리지

| ID | 역할 | 시나리오 | 사용자 영향과 전파 | 결정적 구분 근거 | 상태 |
| --- | --- | --- | --- | --- | --- |
| F10-R | R | commerce PostgreSQL 데이터 볼륨 full | WAL/write 실패 → 주문·재고·결제 쓰기 실패 | 대상 filesystem full·PG ENOSPC, CPU·lock 정상 | calibrate |
| F10-H | H | food MySQL 데이터 디바이스 IO 포화 | 실패보다 전반적인 조회·주문 tail latency 증가 | disk busy·IO wait, 공간 여유·실행계획 정상 | calibrate |
| F10-P | P | banking Oracle 데이터 파일 IO 포화 | 이체·내역조회 동시 지연과 commerce 결제 전파 | Oracle wait와 host disk busy, row lock 없음 | calibrate |
| F10-G | G | 로그 파티션 사용률 경고가 높지만 서비스·DB 쓰기는 정상 | filesystem 이상은 있으나 업무 요청 정상 | DB data mount 여유, ENOSPC 없음, 사용자 영향 없음 | prerequisite |

### F11. Cache fallback·데이터 freshness

| ID | 역할 | 시나리오 | 사용자 영향과 전파 | 결정적 구분 근거 | 상태 |
| --- | --- | --- | --- | --- | --- |
| F11-R | R | Redis 중단 + cart/checkout surge로 PG fallback 용량 초과 | cart 조회·checkout 지연과 실패 | Redis 오류 뒤 DB session/pool 급증, fallback trace | calibrate |
| F11-H | H | Redis는 정상이나 PG pool 고갈 | 동일 cart timeout이 발생하지만 cache failure 없음 | Redis 정상, Hikari pending·timeout 직접 근거 | calibrate |
| F11-P | P | stale promotion cache가 오래된 가격 반환 | 응답은 빠르지만 잘못된 quote·주문 금액 발생 | latency 정상, cache version과 DB 정본 불일치 | prerequisite |
| F11-G | G | Redis 장애를 PG fallback이 완전히 흡수 | cache 오류는 있으나 cart·checkout 결과와 SLA 정상 | DB headroom 충분, 사용자 영향 없음 | ready |

### F12. 실제 네트워크 경로

| ID | 역할 | 시나리오 | 사용자 영향과 전파 | 결정적 구분 근거 | 상태 |
| --- | --- | --- | --- | --- | --- |
| F12-R | R | .57→tb-cp 서비스 경로의 실제 인터페이스 down | 해당 경로 NodePort 접속 불가·다도메인 요청 실패 | 동일 인터페이스 ifDown/trap과 연결 실패, pod·DB 정상 | prerequisite |
| F12-H | H | 서비스 pod CPU 포화로 네트워크 장애 같은 timeout 발생 | 특정 서비스 요청만 지연·실패 | interface 정상, container throttle·service trace 이상 | calibrate |
| F12-P | P | commerce→banking cross-domain edge에만 packet loss | checkout 결제는 간헐 실패하지만 banking 직접 요청은 정상 | 특정 edge retransmit·timeout, 다른 진입 경로 정상 | prerequisite |
| F12-G | G | 서비스 경로와 무관한 인터페이스 trap·flow 급증 | NMS 이벤트는 있으나 서비스 지연·실패 없음 | topology상 무관한 interface, 사용자 경로 정상 | prerequisite |

합성 trap과 별도 차단을 결합해 가짜 상관을 만들면 안 된다. F12의 양성 후보는
서비스 트래픽이 실제로 지난 동일 인터페이스에서 주입과 관측이 함께 일어나야 한다.

### F13. WPM 사용자 여정·phase

| ID | 역할 | 시나리오 | 사용자 영향과 전파 | 결정적 구분 근거 | 상태 |
| --- | --- | --- | --- | --- | --- |
| F13-R | R | commerce checkout edge route에 지연을 주입 | page/API 응답 대기 후 checkout timeout | WPM TTFB phase 증가, 내부 order 처리시간·connect/TLS 정상 | prerequisite |
| F13-H | H | 실제 네트워크 connect phase 지연 | 사용자는 같은 “페이지가 느림”을 경험 | connect phase 증가, backend 처리시간 정상 | prerequisite |
| F13-P | P | banking transfer history의 과대 응답으로 download phase 지연 | 조회 화면만 느리고 이체 쓰기는 정상 | response size·download phase 증가, DB 실행시간 정상 | prerequisite |
| F13-G | G | 단일 probe 위치만 실패하고 실제 서비스와 다른 probe는 정상 | synthetic failure는 있으나 공통 사용자 영향 없음 | probe location 편향, APM·다른 probe 정상 | prerequisite |

### F14. 업무 정합성·기능 오동작

| ID | 역할 | 시나리오 | 사용자 영향과 전파 | 결정적 구분 근거 | 상태 |
| --- | --- | --- | --- | --- | --- |
| F14-R | R | 응답 유실 후 재시도에서 idempotency 부재로 중복 주문·결제 | 사용자는 한 번 요청했지만 주문·결제가 중복 생성 | 동일 business key·trace 계보의 중복 row, 자원 지표 정상 | prerequisite |
| F14-H | H | pricing 버그로 잘못된 금액이지만 주문 중복은 없음 | checkout은 성공하나 청구 금액 오류 | quote/정본 가격 불일치, 중복 business key 없음 | prerequisite |
| F14-P | P | 이체 성공 후 ledger consumer 누락으로 원장 미기표 | 즉시 이체 성공 뒤 대사·원장 정합성 위반 | transfer commit 존재, ledger row·consumer 처리 부재 | prerequisite |
| F14-G | G | 보상 트랜잭션 중 중간 상태가 흔들리지만 최종 정합 | 일시 상태 변화는 있으나 최종 주문·결제·재고가 일치 | 최종 business invariant 만족, 중복·누락 없음 | prerequisite |

기능 오동작 시나리오는 CPU·메모리처럼 자연히 이벤트가 발생하지 않을 수 있다.
업무 invariant·상태 전이·SLA를 관측할 수 있는 신호 계약이 없으면 양성으로
승격하지 않는다.

### F15. 사건 경계·수명주기·복합 원인

| ID | 역할 | 시나리오 | 사용자 영향과 전파 | 결정적 구분 근거 | 상태 |
| --- | --- | --- | --- | --- | --- |
| F15-R | R | external PG success/429를 반복 전환해 장애→회복→재발 | checkout 실패가 회복 후 같은 원인으로 다시 발생 | 두 episode 사이 정상 구간과 동일 외부 원인 | calibrate |
| F15-H | H | commerce inventory lock과 food external PG 429 동시 발생 | 두 도메인에서 비슷한 주문 실패가 동시에 발생 | topology와 root signal이 분리된 두 사건, anti-merge 필요 | prerequisite |
| F15-P | P | 공통 worker node pressure가 commerce·food pod에 동시 영향 | 호출 관계 없는 서비스가 같은 인프라 원인으로 실패 | 동일 node pressure·eviction이라는 공통 root, merge 필요 | calibrate |
| F15-G | G | inventory lock과 banking lock이 같은 checkout trace에서 동시 발생 | 하나의 사용자 사건이지만 독립 root가 둘이라 단일 원인 확정 부당 | 두 blocking root가 각각 충분하고 선후 인과가 없음 | calibrate |

시간 중첩 이종 장애 변형을 추가한다. 아래 네 후보는 단순히 장애 두 개를 동시에
켜는 것이 아니라 **시간적 근접성이 사건 경계 판단을 흐리는지** 검증한다.

| ID | 중첩 형태 | 시나리오 | 기대 사건 경계 | 상태 |
| --- | --- | --- | --- | --- |
| F15-T1 | 정확 동시 | commerce inventory PG lock과 food pod OOMKill을 같은 시각에 시작 | 원인·topology가 다른 인시던트 2개로 분리(anti-merge) | ready |
| F15-T2 | 시차 중첩 | commerce inventory PG lock 시작 3분 뒤 food external PG 429 시작, 두 장애 구간은 겹침 | 비슷한 주문 실패여도 인시던트 2개로 분리 | prerequisite |
| F15-T3 | nested | 장시간 worker CPU pressure 도중 짧은 Kafka consumer stall 삽입 | 공통 원인 증거가 없으면 장기·단기 사건을 분리하고 각각 RCA | calibrate |
| F15-T4 | hand-off | inventory lock 회복 2분 뒤 무관한 Kafka lag 시작 | 앞 사건 reopen이 아니라 새 인시던트로 생성 | calibrate |

F15는 일반적인 “시나리오 사이 최소 1시간 정상 구간”의 예외다. 의도적인 동시·근접
발생은 하나의 YAML과 하나의 orchestration/cleanup 경계 안에서만 허용하며, 독립
스크립트를 우연히 겹쳐 실행해서 만들지 않는다. 각 하위 장애의 시작·종료 시각과
cleanup 결과는 따로 기록한다. 캡처의 `t1`은 가장 이른 주입 시작, `t2`는 가장 늦은
주입 종료로 계산한다.

`F15-T2`의 YAML과 orchestration script 초안은
`rca-scenario-runner/scenarios/services/cross-domain/`에 구현했다(2026-07-15).
정적 구문·전체 카탈로그/API 회귀 테스트까지만 통과했으며 **라이브 주입은 아직
실행하지 않았다**. 용량 조사와 실행 승인 뒤 별도 캘리브레이션한다.

## 5. 커버리지 요약

### 5.1 원인·관측 표면

| 축 | 대표 사례군 |
| --- | --- |
| APM·trace·외부 호출 | F01, F03, F06, F07, F11 |
| DPM·SQL·session·lock | F01, F02, F03, F10 |
| SMS·host·filesystem·disk | F09, F10, F15 |
| NMS·interface·packet·flow | F12, F13 |
| KCM·pod·node lifecycle | F05, F08, F09, F15 |
| WPM·URL·phase | F13 |
| Kafka·outbox·consumer | F04, F14 |
| change·configuration | F05, F08, F09 |
| 기능·업무 정합성 | F08, F11, F14 |

### 5.2 사건 구조

| 구조 | 대표 후보 |
| --- | --- |
| 동일 사건 이벤트 merge | F01-R/P, F05-R/H, F07-R |
| cross-domain 전파 | F01-P, F07-R, F10-P, F12-P |
| brownout·부분 영향 | F06-P, F07-P, F08-P, F13-P |
| absorbed failure·미격상 | 각 사례군의 `G`, 특히 F04-G·F05-G·F07-G·F11-G |
| change 선행 인과사슬 | F05-H, F08-R/P, F09-P |
| distractor·anti-merge | F08-G, F15-H |
| flapping·reopen | F15-R |
| 공통 인프라 root의 다서비스 merge | F05-P, F09-R, F15-P |
| 단일 원인 확정이 부당한 multi-root | F15-G |
| 동시·근접 이종 장애의 시간 경계 | F15-H, F15-T1~T4 |
| 자원 이상 없는 기능 incident | F08-R, F11-P, F14-R/H/P |

### 5.3 도메인과 엔진

- commerce를 플래그십으로 쓰되 food-delivery와 core-banking 재발형을 반드시 둔다.
- PostgreSQL, MySQL, Oracle 각각에서 lock 또는 query 사례를 하나 이상 확보한다.
- 동기 checkout뿐 아니라 Kafka 후속 처리와 정시 배치 경로를 포함한다.
- commerce→core-banking 경로를 cross-domain 대표로 사용한다.
- 같은 기술 주입을 세 도메인에 기계적으로 복제하지 않고 업무 영향과 전파 경로를
  다르게 설계한다.

## 6. 후보 채택·제외 규칙

후보 하나를 실제 YAML 작성 대상으로 올리기 전에 다음을 확인한다.

- 사용자·서비스 영향이 구체적이며 “메트릭이 움직였다”로 끝나지 않는다.
- 원인 주입과 사용자 영향 사이의 인과 경로가 실제 코드·토폴로지에 존재한다.
- 함께 묶여야 할 이벤트와 제외해야 할 distractor를 설명할 수 있다.
- 인시던트의 영향 범위가 전체 서비스인지 특정 기능·SKU·지역·경로인지 명확하다.
- 원인을 지지하는 신호와 가장 그럴듯한 대안을 배제하는 정상 신호가 존재한다.
- 유사 사례군에서는 공유되는 부분과 달라야 하는 부분이 모두 명시돼 있다.
- `guardrail`은 수집 실패가 아니라 의도적으로 영향이 흡수되거나 원인 확정이
  부당한 상황이다.
- `prerequisite` 후보는 필요한 표면·probe·관측 계약이 생기기 전 양성으로
  승격하지 않는다.
- cleanup 후 baseline과 업무 데이터 정합성이 복원된다.

## 7. 다음 작성 순서

1. F01의 네 후보를 첫 사례군으로 상세화해 family 작성 형식을 고정한다.
2. F05·F06으로 KCM과 외부 의존의 양성/guardrail 경계를 고정한다.
3. F04의 비동기 SLA probe 필요 여부를 서비스 계약과 함께 확정한다.
4. F02·F03·F07·F08을 추가하고 F12·F13 선행조건을 점검해 Phase 1 후보를
   전체 60개에서 균형 있게 선정한다.
5. 테스트베드 총용량 결과를 받아 각 후보의 강도·지속시간·중단 임계치를 붙인다.
6. Phase 2는 NMS/WPM·기능 정합성의 선행 계약을 먼저 만든 뒤 상세화한다.
