---
title: 시나리오 실행·주입 위치 결정표
status: Draft
owner: project
last_reviewed: 2026-07-16
tags: [scenario, execution, injection, dry-run]
summary: 64개 AIOps 시나리오의 오케스트레이터, 실제 주입 위치, 전송 방식, 복구 위치와 실행 준비도를 고정한다.
---

# 시나리오 실행·주입 위치 결정표

## 1. 판정 규칙

모든 orchestration script는 `scenario-runner@109`가 소유한다. 실제 주입 위치는
원인과 관측 topology를 기준으로 별도 결정한다. 표의 약어는 다음과 같다.

- `R/ssh`: tb-runner `192.168.122.206`에서 실행한다.
- `K/kubectl`: runner가 Kubernetes API를 통해 대상 namespace의 resource에
  `exec`, `patch`, `scale` 또는 일회성 Job을 적용한다.
- `W/ssh`: 원인이 존재해야 하는 worker node에서 실행한다.
- `E57/ssh`: 물리 경로를 통과시키기 위해 외부 서버 `192.168.200.57`에서 실행한다.
- `API`: 실제 서비스가 호출하는 fault-control/mock API에서 실행한다. 비밀값은
  YAML이 아니라 환경변수 이름만 선언한다.

`실행 준비도`는 카탈로그의 의미 설계 상태와 별개다. `ready`는 현재 표면과
cleanup이 확인된 경우, `partial`은 스크립트·강도 또는 접근 경로가 일부 미확정인
경우, `blocked`는 선행 표면 없이는 실행하면 안 되는 경우다. `blocked` 행에는
라이브 스크립트를 만들지 않고 dry-run 계획만 유지한다.

## 2. 64개 위치 결정

| ID | 주입 유형 | 실제 주입 위치·전송 | 영향 대상·진입 경로 | cleanup 위치 | 실행 준비도·판정 근거 |
| --- | --- | --- | --- | --- | --- |
| F01-R | PG row lock + checkout | R/ssh → PG NodePort 30432 | inventory hot row; 사용자는 commerce NodePort | R의 tagged DB session | ready — 실제 checkout row와 동일 DB |
| F01-H | external 429 | K/kubectl port-forward → commerce MockServer API | payment outbound `/v1/payments` | 같은 MockServer에서 기본 200 복원 | ready — mock path 확인됨 |
| F01-P | Oracle row lock + checkout | R/ssh → Oracle NodePort 30308 | banking accounts row; commerce→banking trace | R의 tagged Oracle session | partial — Oracle 전용 lock script 필요 |
| F01-G | 짧은 external delay | K/kubectl → commerce MockServer API | retry가 흡수할 지연 범위 | 같은 MockServer | ready — 1/3/5초 adaptive ladder와 5xx·업무 guardrail 고정 |
| F02-R | PG index fault | R/ssh → PG NodePort | product search SQL/index | R에서 exact inverse DDL | ready — idx_products_name·rows≥2000 registry 고정(07-18), 라이브 실존 검증 07-20 |
| F02-H | storage IO stress | commerce worker/ssh | PG PVC backing device; commerce search NodePort | 같은 worker의 scenario process | blocked — backing device 미확정 |
| F02-P | MySQL index fault | R/ssh → MySQL NodePort | food menu filter SQL/index | R에서 index 복원 | blocked — 실측(07-20): menus 95행뿐이라 인덱스 제거로 증상 불성립. 시드 보강(수만 행) 선행 필요 |
| F02-G | batch-only heavy SQL | R/ssh → 해당 DB NodePort | batch-tagged session, 온라인 경로는 관측만 | R의 tagged session | blocked — 실제 batch 기동 표면 미확정 |
| F03-R | connection leak | K/kubectl → food-payment fault build | 실제 payment 실패 경로 | K에서 image/config rollback | blocked — leak fault surface 없음 |
| F03-H | servlet thread saturation | K/kubectl one-shot Job | service DNS를 통한 east-west slow calls | K에서 Job 삭제 | partial — executor 관측·강도 필요 |
| F03-P | Hikari 축소 + surge | K/kubectl config patch + R/ssh load | commerce payment + checkout NodePort | K config rollback, R k6 종료 | partial — pool/강도 실측 필요 |
| F03-G | 낮은 surge | R/ssh | 용량 무릎 아래 commerce NodePort | R k6 tagged process | ready — 사용자 경로 동일 |
| F04-R | consumer stop | K/kubectl scale shipping=0 | commerce orders→Kafka→shipping | K replica 복원·lag drain | ready — SLA probe 대신 kafka-consumer-groups lag 직접 판정(replicas=0 ∧ lag>0)으로 재설계(07-18) |
| F04-H | outbox relay stop | K/kubectl fault config/build | order DB outbox→Kafka | K relay 복원·backlog drain | blocked — relay 독립 제어 없음 |
| F04-P | ledger rate limit | K/kubectl fault config/build | banking transfers→ledger | K 처리율 복원·lag drain | blocked — rate-control 표면 없음 |
| F04-G | short broker outage | K/kubectl Kafka lifecycle | outbox→Kafka→consumer | K broker 복구·완전 drain | partial — SLA 내 중단시간 실측 필요 |
| F05-R | memory limit + load | K/kubectl payment limit patch + R/ssh | checkout→payment pod | K 원 spec rollback, R load 종료 | ready — adaptive ladder가 안전 강도 탐색, restart 예산 3회 stop-loss(07-18) |
| F05-H | liveness misconfig | K/kubectl probe patch | payment restart→checkout | K 원 probe rollback | ready — fault probe(/actuator/health/f05-h-fail) 고정, restart 예산 4회(07-18) |
| F05-P | node memory pressure | 대상 worker/ssh | 해당 worker의 commerce·food pods | 같은 worker process 종료 | partial — 배치 지도 확보(07-20, F09-R 참조). eviction 안전 강도 실측만 잔여 |
| F05-G | invalid image rollout | K/kubectl → `rca-testbed-commerce/testbed-payment` | 새 replica만 실패, 기존 replica 유지 | K에서 snapshot한 원 image 복원 | ready — target/image 정본과 ImagePullBackOff 관측 고정 |
| F06-R | external hang | K/kubectl → commerce MockServer API | payment outbound timeout | 같은 MockServer 기본 200 | ready — path·restore 계약 확인됨 |
| F06-H | payment DB lock | R/ssh → PG NodePort | 외부 호출 전 payment DB row | R tagged DB session | partial — 실측(07-20): payment 경로는 결제마다 신규 INSERT라 row lock 대상 없음. 표면 = payments 테이블 잠금(tagged session LOCK TABLE) 변형으로 상세화 |
| F06-P | partial 429 | K/kubectl → food MockServer API | food `/pay`, 성공/429 혼재 | 같은 MockServer 기본 200 | blocked — 배차 풀 복구 전 정상 주문·결제 영향 평가 불가 |
| F06-G | transient 5xx | K/kubectl → 대상 MockServer API | retry/CB 실제 호출 경로 | 같은 MockServer | ready — fixed evaluation 계약 완비, live 등재(07-18) |
| F07-R | downstream delay + surge | 미확정 edge fault + R/ssh | commerce payment→banking transfer | fault 제거 + R load 종료 | blocked — 안전한 edge delay 위치 없음 |
| F07-H | north-south surge | R/ssh | commerce NodePort | R tagged k6 | ready — Commerce는 80 RPS 실측 |
| F07-P | pricing bulkhead saturation | K/kubectl one-shot Job | pricing service DNS | K Job 삭제 | ready-후보 — 실측(07-20): resilience4j pricingQuote max-concurrent 20·wait 0(즉시 503) 확인. 동시 slow call >20 Job으로 상세화 가능 |
| F07-G | CB containment | K/kubectl → 선택 mock/downstream | 실제 fallback 경로 | fault reset·CB closed 확인 | partial — 하류와 fallback을 YAML에서 고정 필요 |
| F08-R | faulty pricing release | K/kubectl image rollout | pricing→checkout correctness | K previous image + 업무 데이터 정리 | blocked — 결함 image 없음 |
| F08-H | rollout + external fault | K/kubectl rollout + MockServer API | checkout NodePort, 독립 sub-injection | K에서 두 fault 역순 복구 | ready — composite script 필요 |
| F08-P | timeout ConfigMap | K/kubectl config+rollout | payment/order tail latency | K config rollback+rollout | ready-후보 — 실측(07-20): timeout은 yml 리터럴(env 없음) → SPRING_APPLICATION_JSON env 오버라이드로 재빌드 없이 주입 가능 |
| F08-G | distractor rollout + Oracle lock | K/kubectl + R/ssh | notification change와 banking accounts | Oracle rollback 후 rollout 정리 | partial — Oracle script 필요 |
| F09-R | host CPU noisy neighbor | 선택 worker/ssh | 해당 node pod cohort | 같은 worker tagged process | ready-후보 — 실측(07-20): 배치 지도 확보(w2=cart·gateway·inventory·redis·kafka·food-payment / w3=order·payment·pricing·product·user·mysql) |
| F09-H | JVM GC pressure | K/kubectl JVM config + R/ssh | order endpoints | K config rollback, R load 종료 | partial — OOM 전 pause 범위 필요 |
| F09-P | pod CPU throttle | K/kubectl inventory limit + R/ssh | checkout→inventory | K 원 limit rollback, R load 종료 | ready — 기존 resource patch 패턴 사용 |
| F09-G | batch CPU only | K/kubectl exec 또는 batch owner | batch process, 온라인 trace 정상 | 같은 process/Job | blocked — 독립 batch trigger 없음 |
| F10-R | PG volume fill | commerce worker/ssh 또는 privileged K Job | 실제 PG data volume | filler만 제거 | blocked — PVC path 오판 위험 |
| F10-H | MySQL IO saturation | food worker/ssh | 실제 MySQL device | 같은 worker stress 종료 | blocked — backing device 미확정 |
| F10-P | Oracle IO saturation | banking worker/ssh | 실제 Oracle device | 같은 worker stress 종료 | blocked — 데이터 손상 위험·device 미확정 |
| F10-G | log partition watermark | 대상 worker/ssh | DB data와 분리된 log mount | filler 제거 | blocked — 별도 partition 확인 안 됨 |
| F11-R | Redis down + surge | K/kubectl Redis + R/ssh | cart fallback→PG | R load 종료 후 Redis replica 복원 | ready — 35/50/65 RPS adaptive ladder와 checkout 5xx 비율 관측 고정 |
| F11-H | PG pool fault | K/kubectl cart config + R/ssh | cart/checkout, Redis 정상 | K rollback, R load 종료 | partial — pool 경계 필요 |
| F11-P | stale price cache | 미확정 API/K fault surface | pricing correctness | cache/version 복원 | blocked — 조작 API·fault build 없음 |
| F11-G | Redis down under low load | K/kubectl Redis + R/ssh | fallback이 흡수할 cart/checkout | Redis 복구, R load 종료 | ready — 낮은 강도 사용 |
| F12-R | actual interface down | E57/ssh 또는 network device API | .57→실제 NIC/bridge→tb-cp | 같은 interface up·route 확인 | blocked — 실제 interface·OOB 복구 미확정 |
| F12-H | product pod CPU throttle | K/kubectl `rca-testbed-commerce/testbed-product` + R/ssh 35 RPS | `commerce-product` APM p95/error, pod throttle; checkout/network 정상 | CPU limit 원값 `500m` rollback, pod Ready 확인, R load 종료 | ready — `product-service` CPU `500m→250m→100m→50m` adaptive ladder, product-only 영향·network error 0 고정 관측 |
| F12-P | cross-domain packet loss | 미확정 network namespace | commerce payment↔banking edge | netem/policy 완전 제거 | blocked — CNI/NET_ADMIN 지점 없음 |
| F12-G | unrelated flow/trap | E57/ssh의 비서비스 interface | 서비스 topology와 무관한 NMS source | E57 generator 종료·상태 복원 | blocked — NMS source setup 필요 |
| F13-R | edge route delay | K/kubectl commerce ingress config | WPM→checkout NodePort | K ingress config 복원 | blocked — WPM phase 계약 없음 |
| F13-H | connect phase delay | WPM source 또는 E57/ssh | probe→tb-cp NodePort | tc/route 복원 | blocked — probe 위치 미확정 |
| F13-P | oversized history response | R/ssh DB seed 또는 K fault build | WPM→banking history | seed 제거/image rollback | blocked — response-size 표면·WPM 없음 |
| F13-G | single probe failure | 특정 WPM probe host | 해당 probe만 URL 접근 | probe config/network 복원 | blocked — 복수 probe topology 없음 |
| F14-R | response loss + non-idempotent retry | K ingress/fault image | checkout business key | proxy 제거·image rollback·중복 row 정리 | blocked — fault surfaces 없음 |
| F14-H | pricing correctness bug | K/kubectl fault image/cache | pricing→checkout | K rollback·오염 데이터 정리 | blocked — fault image 없음 |
| F14-P | selective ledger loss | K/kubectl fault consumer | transfer→Kafka→ledger | consumer 복원·event replay | blocked — selective drop 없음 |
| F14-G | compensation path | MockServer API + R/ssh | checkout failure→inventory release | mock 복원·업무 invariant 확인 | blocked — invariant probe 필요 |
| F15-R | 429 flapping | K/kubectl → commerce MockServer API | checkout episodes | 기본 200 복원 | partial — judge episode 간격 실측 필요 |
| F15-H | simultaneous PG lock + food 429 | K/kubectl 두 injection point | commerce DB와 food mock, offset 0 | orchestrator가 역순 cleanup | blocked — food 주문 경로 상시 배차 503 |
| F15-P | common node pressure | 공통 worker/ssh | 같은 node의 commerce·food pods | worker stress 종료 | blocked — 현재 공통 placement 불확실 |
| F15-G | PG lock + Oracle lock | R/ssh 두 DB NodePort | 동일 checkout trace의 독립 roots | 두 tagged transaction rollback | partial — row·순서 확정 필요 |
| F15-T1 | PG lock + food OOM exact time | K/kubectl 두 target | commerce DB와 food pod, offset 0 | orchestrator 역순 cleanup | partial — food OOM 강도 필요 |
| F15-T2 | PG lock then food 429 | K/kubectl 두 target | commerce DB offset 0, food mock +3m | mock 200 후 DB rollback | blocked — 정적 파일럿만 허용, food 배차 복구 선행 |
| F15-T3 | host CPU + nested consumer stall | worker/ssh + K/kubectl | node cohort + Kafka consumer | consumer drain 후 worker stress 종료 | partial — SLA·placement 필요 |
| F17-R | banking transfer down → payment @Tx 전체 롤백 | K/kubectl probe patch (rca-testbed-banking) | commerce checkout 502 (cross-domain, PG 성공분 소멸) | K 원 readinessProbe rollback | ready — injector·관측 allowlist 완비(07-24, 재설계 신규 1호 승격, runner 94bb225) |
| F18-P | outbox relay 정지 → 원장/알림 silent lag | K/kubectl env-patch OUTBOX_RELAY_ENABLED=false + rollout (rca-testbed-banking) | 동기 이체는 정상(2xx 유지), outbox 미발행 적체만 증가 — F04-R(lag 증가)과 감별 | K 원 env rollback + rollout | ready — env allowlist·outbox count·banking kafka lag 배선 완비(07-24, runner 674c092) |
| F15-T4 | PG lock then Kafka lag | R/ssh + K/kubectl | lock 회복 +2m 후 consumer | consumer drain, DB session 확인 | partial — judge close 간격 필요 |

## 3. 공통 preflight와 중단 조건

현재 판정 합계는 `ready 12`, `partial 23`, `blocked 29`이다. 이 숫자는 실제
환경 read-only preflight와 calibration 결과에 따라 갱신하며, 카탈로그 의미
설계 상태를 자동으로 승격시키지 않는다.

실행 계약 기준으로는 이 중 14개가 normalized `live_allowed=true`다. 새로 열린
F01-G와 F11-R은 미실측 경계를 임의 고정하지 않고 각 실행 안에서 adaptive
calibration으로 결정한다. F05-G는 저장소의 commerce Deployment 정본과 원 image
snapshot, ImagePullBackOff 관측을 결합한다. F12-H는 commerce product Deployment와
container, CPU 원값 `500m`, 35 RPS companion load가 정본화되어 adaptive calibration만 허용한다.
성공 조건은 product APM p95/error와 CPU throttle이 함께 상승하되 control service와
pod network error는 정상인 경우로 제한하고, cleanup 뒤 CPU limit `500m` 복원을 recovery gate로 확인한다.

라이브 실행 전에는 runner와 실제 target을 변경하지 않는 read-only preflight를
통과해야 한다. kubeconfig current-context와 namespace/resource 존재, RBAC,
tb-runner의 baseline unit·k6·전용 script, NodePort health, stale PID/session,
cleanup 대상 원 spec을 확인한다. 정본 kubeconfig는 109의
`/root/tb-kubeconfig`이며 kubeadm 4-node testbed(`tb-cp`, `tb-w1`, `tb-w2`,
`tb-w3`)를 가리킨다. `/home/nkia/.kube/config`는 이전 K3s 경로이므로 runner,
compose mount, 신규 scenario script에서 사용하면 안 된다. dry-run은 정본 경로와
예상 context를 계획에 표시하고, live preflight는 current-context와 node 집합이
일치하지 않으면 subprocess 전에 거부한다.

비밀값·Secret 전체·환경 전체를 로그로 출력하지 않는다. DB session은 scenario tag로,
load process는 전용 script tag로, Kubernetes 변경은 실행 직전 원 spec snapshot으로
복구 범위를 좁힌다. `blocked` 시나리오는 선행조건이 해소될 때까지 dry-run만 허용한다.
