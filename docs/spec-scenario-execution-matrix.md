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
| F02-H | storage IO stress | commerce worker/ssh | PG PVC backing device; commerce search NodePort | 같은 worker의 scenario process | blocked → 주입가능(fix-answerkey-A-sheet 07-23): host_stress_executor.py CONTRACTS['F02-H'] fio 계약(tb-w1 pgdata PVC) 이미 실재, Class B/SMS-disk. 잔여 gap=profiles.json host.stress+load.north_south allowlist 미등록, 호스트 disk IO query_id 미배선 |
| F02-P | MySQL index fault | R/ssh → MySQL NodePort | food menu filter SQL/index | R에서 index 복원 | ready — 시드 보강 후 07-24 live 승격(46d3371). 07-23 CUT 판정은 번복(wave1-worklist §A) |
| F03-H | servlet thread saturation | K/kubectl one-shot Job | service DNS를 통한 east-west slow calls | K에서 Job 삭제 | partial — executor 관측·강도 필요 |
| F03-P | Hikari 축소 + surge | K/kubectl config patch + R/ssh load | commerce payment + checkout NodePort | K config rollback, R k6 종료 | partial — pool/강도 실측 필요. Class B/config 재라벨 확정(fix-food429-reanchor-sheet 07-23): root=undersized pool 설정 오배포, surge=증폭기(R5). executor·metadata 이미 정합, 라벨만 정정 |
| F04-R | consumer stop | K/kubectl scale shipping=0 | commerce orders→Kafka→shipping | K replica 복원·lag drain | ready — SLA probe 대신 kafka-consumer-groups lag 직접 판정(replicas=0 ∧ lag>0)으로 재설계(07-18) |
| F04-H | outbox relay stop | K/kubectl fault config/build | order DB outbox→Kafka | K relay 복원·backlog drain | blocked — relay 독립 제어 없음 |
| F04-P | ledger rate limit | K/kubectl fault config/build | banking transfers→ledger | K 처리율 복원·lag drain | blocked — rate-control 표면 없음 |
| F05-R | memory limit + load | K/kubectl payment limit patch + R/ssh | checkout→payment pod | K 원 spec rollback, R load 종료 | ready — adaptive ladder가 안전 강도 탐색, restart 예산 3회 stop-loss(07-18) |
| F05-H | liveness misconfig | K/kubectl probe patch | payment restart→checkout | K 원 probe rollback | ready — fault probe(/actuator/health/f05-h-fail) 고정, restart 예산 4회(07-18) |
| F05-P | node memory pressure | 대상 worker/ssh | 해당 worker의 commerce·food pods | 같은 worker process 종료 | partial — 배치 지도 확보(07-20, F09-R 참조). eviction 안전 강도 실측만 잔여 |
| F06-R | external hang | K/kubectl → commerce MockServer API | payment outbound timeout | 같은 MockServer 기본 200 | ready — path·restore 계약 확인됨 |
| F06-H | payment DB lock | R/ssh → PG NodePort | 외부 호출 전 payment DB row | R tagged DB session | partial — 실측(07-20): payment 경로는 결제마다 신규 INSERT라 row lock 대상 없음. 표면 = payments 테이블 잠금(tagged session LOCK TABLE) 변형으로 상세화 |
| F06-P | partial 429 | K/kubectl → food MockServer API | food `/pay`, 성공/429 혼재 | 같은 MockServer 기본 200 | blocked — 배차 풀 복구 전 정상 주문·결제 영향 평가 불가 |
| F07-H | north-south surge | R/ssh | commerce NodePort | R tagged k6 | ready — Commerce는 80 RPS 실측. 재앵커(fix-food429-reanchor-sheet 07-23): root_cause를 order Hikari pool(10)·Tomcat(200) 용량 무릎 초과로 구체화(코드 한계 앵커), F03-P/F08-P와 설정변경 이력 부재로 감별 |
| F07-P | pricing bulkhead saturation | K/kubectl one-shot Job | pricing service DNS | K Job 삭제 | ready-후보 — 실측(07-20): resilience4j pricingQuote max-concurrent 20·wait 0(즉시 503) 확인. 동시 slow call >20 Job으로 상세화 가능 |
| F08-H | rollout + external fault | K/kubectl rollout + MockServer API | checkout NodePort, 독립 sub-injection | K에서 두 fault 역순 복구 | ready — composite script 필요 |
| F08-P | timeout ConfigMap | K/kubectl config+rollout | payment/order tail latency | K config rollback+rollout | ready-후보 — 실측(07-20): timeout은 yml 리터럴(env 없음) → SPRING_APPLICATION_JSON env 오버라이드로 재빌드 없이 주입 가능 |
| F08-G | distractor rollout + Oracle lock | K/kubectl + R/ssh | notification change와 banking accounts | Oracle rollback 후 rollout 정리 | partial — Oracle script 필요 |
| F09-R | host CPU noisy neighbor | 선택 worker/ssh | 해당 node pod cohort | 같은 worker tagged process | ready-후보 — 실측(07-20): 배치 지도 확보(w2=cart·gateway·inventory·redis·kafka·food-payment / w3=order·payment·pricing·product·user·mysql) |
| F09-H | JVM GC pressure | K/kubectl JVM config + R/ssh | order endpoints | K config rollback, R load 종료 | partial — OOM 전 pause 범위 필요 |
| F09-P | pod CPU throttle | K/kubectl inventory limit + R/ssh | checkout→inventory | K 원 limit rollback, R load 종료 | ready — 기존 resource patch 패턴 사용 |
| F10-R | PG volume fill | commerce worker/ssh 또는 privileged K Job | 실제 PG data volume | filler만 제거 | blocked — PVC path 오판 위험 |
| F10-H | MySQL IO saturation | food worker/ssh | 실제 MySQL device | 같은 worker stress 종료 | blocked — backing device 미확정 |
| F10-P | Oracle IO saturation | banking worker/ssh | 실제 Oracle device | 같은 worker stress 종료 | blocked — 데이터 손상 위험·device 미확정 |
| F11-R | Redis down + surge | K/kubectl Redis + R/ssh | cart fallback→PG | R load 종료 후 Redis replica 복원 | ready — 35/50/65 RPS adaptive ladder와 checkout 5xx 비율 관측 고정 |
| F12-H | product pod CPU throttle | K/kubectl `rca-testbed-commerce/testbed-product` + R/ssh 35 RPS | `commerce-product` APM p95/error, pod throttle; checkout/network 정상 | CPU limit 원값 `500m` rollback, pod Ready 확인, R load 종료 | ready — `product-service` CPU `500m→250m→100m→50m` adaptive ladder, product-only 영향·network error 0 고정 관측 |
| F13-R | edge route delay | K/kubectl commerce ingress config | WPM→checkout NodePort | K ingress config 복원 | blocked — WPM phase 계약 없음 |
| F13-H | connect phase delay | WPM source 또는 E57/ssh | probe→tb-cp NodePort | tc/route 복원 | blocked — probe 위치 미확정 |
| F13-P | oversized history response | R/ssh DB seed 또는 K fault build | WPM→banking history | seed 제거/image rollback | blocked — response-size 표면·WPM 없음 |
| F14-R | response loss + non-idempotent retry | K ingress/fault image | checkout business key | proxy 제거·image rollback·중복 row 정리 | blocked — fault surfaces 없음 |
| F14-P | selective ledger loss | K/kubectl fault consumer | transfer→Kafka→ledger | consumer 복원·event replay | blocked — selective drop 없음 |
| F15-R | 429 flapping | K/kubectl → commerce MockServer API | checkout episodes | 기본 200 복원 | partial — judge episode 간격 실측 필요 |
| F15-H | simultaneous PG lock + food 429 | K/kubectl 두 injection point | commerce DB와 food mock, offset 0 | orchestrator가 역순 cleanup | blocked — food 주문 경로 상시 배차 503 |
| F15-P | common node pressure | 공통 worker/ssh | 같은 node의 commerce·food pods | worker stress 종료 | blocked — 현재 공통 placement 불확실 |
| F15-G | PG lock + Oracle lock | R/ssh 두 DB NodePort | 동일 checkout trace의 독립 roots | 두 tagged transaction rollback | partial — row·순서 확정 필요 |
| F15-T1 | PG lock + food OOM exact time | K/kubectl 두 target | commerce DB와 food pod, offset 0 | orchestrator 역순 cleanup | partial — food OOM 강도 필요 |
| F15-T2 | PG lock then food 429 | K/kubectl 두 target | commerce DB offset 0, food mock +3m | mock 200 후 DB rollback | blocked — 정적 파일럿만 허용, food 배차 복구 선행 |
| F15-T3 | host CPU + nested consumer stall | worker/ssh + K/kubectl | node cohort + Kafka consumer | consumer drain 후 worker stress 종료 | partial — SLA·placement 필요 |
| F16-H | user-service fail-close → 전 도메인 쓰기 401 | K/kubectl probe patch (rca-testbed-commerce testbed-user readinessProbe) | 게이트웨이 AuthGuard가 쓰기 라우트 전부 401 fail-close, 읽기는 정상(결정 감별) | K 원 readinessProbe rollback | ready — monitor read_step 증분+write/read status 배선 완비(07-24, 재설계 파일럿 승격, runner fb06466) |
| F17-R | banking transfer down → payment @Tx 전체 롤백 | K/kubectl probe patch (rca-testbed-banking) | commerce checkout 502 (cross-domain, PG 성공분 소멸) | K 원 readinessProbe rollback | ready — injector·관측 allowlist 완비(07-24, 재설계 신규 1호 승격, runner 94bb225) |
| F18-P | outbox relay 정지 → 원장/알림 silent lag | K/kubectl env-patch OUTBOX_RELAY_ENABLED=false + rollout (rca-testbed-banking) | 동기 이체는 정상(2xx 유지), outbox 미발행 적체만 증가 — F04-R(lag 증가)과 감별 | K 원 env rollback + rollout | ready — env allowlist·outbox count·banking kafka lag 배선 완비(07-24, runner 674c092) |
| F19-P | food 롱tx 풀고갈 (createOrder @Tx내 동기 fanout) | K/kubectl → food MockServer /pay delay 8s | order Hikari(10) 고갈 → create 5xx, payment는 정상 유지 | mock 스냅샷 복원 | ready — mock food 일반화·hikari_pending·food_create_status 배선(07-24, runner e1a34b4) |
| F19-S | food PG mock 지연 → payment 502 연쇄 | K/kubectl → food MockServer /pay delay 30s(>read-timeout 10s) | payment error≥30% → order create 502 전파 | mock 스냅샷 복원 | ready — CB-open 게이트는 상태코드 rate 대체 관측(부품2 폐기 판정, 07-24) |
| F20-R | commerce order 통계/검색 슬로우쿼리 → 공유 PG CPU 오염 → 타 스키마 교차 지연 | R/ssh load.north_south → commerce slowquery.js(stats/daily+무제한 search 80%, checkout 대표 20%) | order p95↑ + payment p95도 동반↑(교차 스키마)·payment error_rate<0.05 | R load 종료 | ready — 신규 slowquery.js 배선 + DB-side pg_slow_active_query_count 신설(07-24, 재설계 패밀리 P8 1호, runner live_probes.py 로컬 패치) |
| F20-P | banking transfer stats/daily trunc() 풀스캔 → transfer 커넥션 점유 → 이체 경합 악화 | R/ssh load.north_south → core-banking slowquery.js(stats/daily 80%, 대표 이체 20%) | transfer p95↑ + transfer Hikari pending>0 + account/api error_rate↑ | R load 종료 | ready — 신규 slowquery.js 배선 + APPROVED_HIKARI_SERVICES/APPROVED_APM_SERVICES에 core-banking 4종 추가(07-24, runner live_probes.py 로컬 패치) |
| F20-Q | food order 무제한 조회+stats filesort → order JVM 힙 압박 | R/ssh load.north_south → food-delivery slowquery.js(무제한목록+stats 80%, 대표 create 20%) | order p95↑ + order 컨테이너 메모리 1Gi limit로 상승, payment error_rate<0.05 | R load 종료 | ready — 신규 slowquery.js 배선 + F20_FOOD_ORDER_TARGET 신설로 container_memory/restart_count를 food testbed-order로 확장(07-24, runner live_probes.py 로컬 패치) |
| F15-T4 | PG lock then Kafka lag | R/ssh + K/kubectl | lock 회복 +2m 후 consumer | consumer drain, DB session 확인 | partial — judge close 간격 필요 |
| F25-H | commerce PostgreSQL(testbed-postgres StatefulSet) 메모리 상한 축소 → OOMKill → commerce 8스키마 전면 순단 | K/kubectl postgres StatefulSet mem limit patch(512Mi→320Mi) + R/ssh load.north_south checkout companion | postgres 컨테이너 termination_reason=OOMKilled + restart_count↑ + commerce checkout 5xx 스파이크 후 회복 | K 원 mem limit(512Mi) rollback, pod ready 확인 | ready — k8s.resource executor StatefulSet kind 일반화 + postgres 관측 allowlist 배선 완료(07-24, Class B 1호 승격) |
| F23-R | commerce inventory 재입고 배치 정지(env stopgap) → 재고 소진 → checkout 409 조용한 기능마비 | K/kubectl testbed-inventory env(SPRING_APPLICATION_JSON interval-ms) stopgap + R/ssh load.north_south checkout companion | checkout_409_rate↑ + inventory_stock_level(재고=0 상품 수)↑ 정체 + restock_movement_rate==0 | K 원 env 원복 후 rollout 회복 확인 | ready — k8s.env stopgap + database.inventory_stock_level/restock_movement_rate/loadgen.checkout_409_rate 관측 배선 완료(07-24, evaluation-모드 stopgap 경로) |
| F17-P | commerce→core-banking transfer 직행(30282) 무결성 우회 — FROZEN 계좌 이체가 200 COMPLETED | R/ssh load.north_south dual-arm(core-banking/loadgen/frozen-bypass.js, direct 30282 + control 30082) | direct arm 200 COMPLETED(frozen_bypass_completed_rate>0) + control arm 400 거절(normal_path_reject_rate≥0.9) + database.integrity_violation_count>0 | R load 종료; 원장 정정(역이체·REVERSED 마킹)은 미구현 | draft — 관측·주입 배선 완료하나 cleanup(원장 정정) 미구현으로 live 보류 |
| F21-Q | food order-service Tomcat 200 스레드풀 포화(restaurant 상류지연, save 前 hop이라 Hikari 미보유) | W/ssh host.stress(worker-w2, cpu) + R/ssh load.north_south companion(order-surge.js, target_rps=70) | order_busy_threads(jvm_nondaemon_thread_count)↑ + order_hikari_pending==0 + order_p95_latency↑ | W stress 해제 | draft — 배선 완료, calibration 미확정(포화 시 busy_threads/p95 실측 없음) |
| F21-P | core-banking api-service Tomcat 200 스레드풀 포화(transfer 하류지연, DataSource 부재로 풀 자체 없음) | W/ssh host.stress(worker-w3, cpu) + R/ssh load.north_south companion(core-banking/loadgen/transfer-heavy-surge.js, target_rps=90) | api_busy_threads(jvm_nondaemon_thread_count)↑ + api_502_rate<0.05(CB closed, 느린 200) | W stress 해제 | draft — 배선 완료, calibration 미확정(포화 시 busy_threads 실측 없음, read-timeout 전이 경계 미검증) |
| F24-Q | food restaurant NodePort(30181) 조회 부하 폭주 → restaurant Hikari(10)/Tomcat 자연 포화 → order retry 증폭·CB open → order 전량 502 | R/ssh load.north_south → food-delivery surge.js(F02-P companion 경로 primary 재사용, target_rps=70) | restaurant_p95·restaurant_error_rate↑ + order_create_5xx_rate≥0.1, payment_502_rate<0.05 | R load 종료(restaurant는 부하 제거만으로 회복) | draft — 배선 완료(신규 executor·스크립트 없음), calibration 미확정(target_rps=70이 5xx 임계까지 미는지 미검증) |

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
