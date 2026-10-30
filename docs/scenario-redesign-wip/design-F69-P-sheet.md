---
title: F69-P 설계 시트 (commerce inventory-service 릴리스의 재조정 배치가 재고 행을 공유 잠금으로 쥔 채 45일 원장 합계를 돌아 재고 예약과 checkout 이 주기적으로 막힘)
status: Draft
owner: project
last_reviewed: 2026-10-30
tags:
  - scenario
  - design
  - release
  - lock
summary: commerce inventory-service 를 릴리스 1.3.0(fault-images/f69-p 패치로 만든 별도 태그)으로 롤아웃한다. 재고 재조정 배치가 대사를 마지막 원장 행 비교에서 45일 기간 원장 대사로 바꾸며 기준 시점을 고정하려고 재고 행 16개를 FOR SHARE 로 읽어 커밋까지 쥔다. 상품마다 45일 이동 이력 합계가 약 2.8초라 한 번에 약 45초 잠금이 약 105초마다 되풀이되고, 그동안 재고 예약이 모두 기다려 checkout 이 5xx 로 실패한다. 원본은 KnowBe4 2025-10-13.
---

# F69-P 설계 시트

## 1. 요약

commerce 재고 서비스(inventory-service)를 새 버전 1.3.0 으로 배포한다. 새 버전은 1분마다 도는 재고 재조정 배치를 "마지막 원장 행 하나와 현재 재고 비교"에서 "45일 전 잔액 + 45일 동안의 이동 합계 = 현재 재고" 대사로 바꾼다. 대사하는 도중 예약이 재고를 바꾸면 거짓 불일치가 나므로, 배치는 처음에 재고 행 16개를 공유 잠금(FOR SHARE)으로 읽고 대사가 끝날 때까지 쥔다. 상품 하나의 45일 합계가 약 2.8초라 잠금은 한 번에 약 45초 이어지고, 그동안 재고 예약(FOR UPDATE)이 모두 기다린다. 예약이 재고 서비스의 DB 연결 10개를 채우면 재고 조회와 health 도 기다리고, 그 구간에 시작한 checkout 은 게이트웨이에서 5xx 로 끝난다. 배치가 끝나면 정상이 되었다가 60초 뒤 다시 잠근다.

비유: 창고 담당자가 재고 장부를 맞춰 보려고 매번 창고 문을 잠그고 한 달 반치 입출고 전표를 한 장씩 더한다. 그동안 출고 요청은 문 앞에 줄을 서고, 줄이 길어지면 창고 안내 데스크까지 막힌다.

관계: F69-R 과 같은 원본(새로 배포한 작업이 무거운 질의를 도는 동안 동시 갱신과 맞지 않는 잠금을 쥠)이지만 최초 원인(별도 Job 의 mysqldump 표 잠금 대 서비스 릴리스 안 배치의 행 공유 잠금), 엔진(MySQL 대 PostgreSQL), 정답(작업 대 inventory-service)이 다른 P. 증상만 보면 F01-R(외부 세션의 재고 행 잠금, 정답 표)과 같고 정답이 달라, 둘을 가르는 관측 근거(잠금 주인의 클라이언트 IP 와 문장, 주기성, 롤아웃)를 §8 에 둔다.

## 2. 원본 사례

KnowBe4, 2025-10-13. 공식 상태 페이지 사고 "KSAT & PhishER - Login Issues" 와 사후 보고(게시 2025-10-27), https://status.knowbe4.com/incidents/mm0hdhwtjzrz (자료 문서 M20 에 출처가 말한 사실이 이미 있다).

요약(자료 문서 M20): 10월 13일 19:30~21:58 UTC 캐나다 지역에서 콘솔 로그인 실패와 시간 초과. 그날 앞서 배포한 새 데이터 처리 작업이 공유 인증 표에 무거운 질의를 돌렸고, 그 질의가 동시 갱신과 맞지 않는 공유 잠금을 만들어 로그인과 등록 갱신이 끝나지 못함. 작업이 죽고 다시 뜨기를 되풀이하며 핵심 표의 잠금을 계속 다시 잡음. 처음엔 캠페인 갱신 적체와 DB 포화를 의심했고 잠금 감시가 없어 차단 요소를 찾는 데 늦었음. 그 작업을 치우자 잠금이 풀리고 로그인이 곧바로 회복.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 그날 배포한 새 데이터 처리 작업 | inventory-service 새 버전 1.3.0 배포(재조정 배치의 대사 방식 변경이 든 릴리스) |
| 원인이 된 결함 | 공유 인증 표에 무거운 질의를 돌리며 동시 갱신과 맞지 않는 공유 잠금을 쥠 | 배치가 재고 행 16개를 FOR SHARE 로 쥔 채 상품마다 45일 이동 이력 합계(약 23만 행, 약 2.8초)를 돌아 한 번에 약 45초 잠금. FOR SHARE 는 예약의 FOR UPDATE, 해제의 UPDATE 와 충돌 |
| 전파 경로 | 로그인, 등록 갱신이 잠금 뒤에서 끝나지 못하고 시간 초과 | 재고 예약이 잠금 뒤에서 기다리고(lock_timeout 0) inventory 연결 풀(10)이 차 재고 조회와 health 도 기다림 → product → inventory 10초, order → product 10초, 게이트웨이 → order 15초 시간 초과 |
| 사용자 증상 | 콘솔 로그인 실패, 시간 초과 | checkout 5xx(약 105초마다 반 분가량), 상품 상세 실패나 약 10초 지연. 상품 목록, 장바구니, 로그인은 정상 |
| 되풀이 | 작업이 죽고 다시 뜨며 잠금을 다시 잡음 | 배치가 끝나면 60초 뒤 다시 돌아 잠금을 다시 잡음(주기 약 105초) |
| 탐지 경로 | DB 층 시간 초과 감시, 잠금 감시 부재로 차단 요소 식별 지연 | APM 오류율, 지연, 로그 오류가 주기적으로 오름. 차단 요소는 DPM 세션의 blocked, blocking 짝과 Top SQL, 배치 로그로 찾음 |
| 완화와 복구 | 작업을 치우자 잠금이 풀리고 회복 | 이미지를 commerce-inventory:latest 로 되돌리면 배치가 잠금 없이 1초 안에 끝나 회복 |

기전 확인: 원본의 고리는 "새로 배포한 작업이 쥔 공유 잠금 + 그 잠금을 쥔 채 도는 무거운 질의 → 같은 표의 갱신이 끝나지 못함"이다. 재구성도 같은 고리이고, 우리 스택(Spring, JPA, PostgreSQL)에 맞게 작업을 서비스 안 예약 배치로, 표를 재고 표로 옮겼다.

## 3. 숫자 근거 (2026-10-30, `scenario-stats.py`, 정식 114 + 후보 3 = 117 → 118)

| 축 | 값 | 판단 |
|---|---|---|
| 서비스(정식 + 후보) | 쇼핑몰 39, 은행 39, 음식배달 39 | 동률이라 어느 것이든 된다(원칙 2). 부품 지도에서 쇼핑몰 inventory 가 서비스 정답 2 로 적고, 같은 증상(F01-R)과 다른 정답을 만들 수 있어 쇼핑몰을 골랐다 |
| 묶음 J(결함 있는 새 버전 배포) | 20(17%) → 21/118(17.8%) | 20% 아래. 현실 원인 1위(Google 37%) |
| 묶음 D(DB 잠금, 보조) | 17(14%), 주 묶음이 J 라 집계는 그대로 | |
| 정답 위치 재고 서비스 | 2 → 3(2.5%) | 20% 아래 |
| 결제 경로 합계 | 14(12%), 변화 없음 | |

이 후보를 고른 이유: 서비스 정답이 적은 commerce inventory 에서, 릴리스(현실 원인 1위)가 만든 잠금이라는 꼴은 아직 없다. 잠금 시나리오는 대부분 정답이 표(D)였는데 이 후보는 같은 잠금 증상의 정답이 서비스 새 버전이라, 관제 AI 가 "잠금이면 표"라는 지름길로 맞힐 수 없다.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 롤아웃: rca-testbed-commerce Deployment testbed-inventory 이미지 commerce-inventory:latest → commerce-inventory:1.3.0(commerce/k8s/22-inventory-service.yaml:27-28, imagePullPolicy Never, tb-w1 :19). 기본 전략이라 새 파드가 Ready 가 된 뒤 옛 파드가 내려간다.
2. 배치 시작: `ReconciliationBatch.run`(60초 fixedDelay, `@Transactional`, 기동 직후 첫 실행) 이 `InventoryRepository.findAllForReconciliation`(`@Lock(PESSIMISTIC_READ)`, `select ... from inventory_schema.inventory ... for share`)으로 재고 행 16개를 공유 잠금(fault-images/f69-p/inventory-service.patch:44-46, :87-93).
3. 상품마다 `findTopByProductIdAndCreatedAtBeforeOrderByCreatedAtDesc`(기간 시작 직전 잔액, 인덱스 한 번)와 `sumQuantitySince`(45일 합계)를 부름(patch:21-24, :104-110). 기간은 `inventory.reconciliation.ledger-window-days` 기본값 45(patch:71). 불일치면 WARN 'Inventory ledger drift detected' 와 보정 ADJUST 행(첫 실행에서 상품당 많아야 한 번).
4. 잠금 유지: 합계 16번(약 45초)과 보충이 끝나 커밋할 때까지 공유 잠금이 이어진다. 끝 로그 'Reconciliation batch finished: ... elapsedMs='(patch:133).
5. 예약 대기: `InventoryService.reserve` 의 `findByProductIdForUpdate`(`PESSIMISTIC_WRITE`, commerce/inventory-service/.../InventoryRepository.java:17-19, InventoryService.java:74-75)와 `release` 의 UPDATE 가 FOR SHARE 와 충돌. commerce PostgreSQL lock_timeout 0, statement_timeout 0(2026-10-30 SHOW 실측)이라 끝없이 기다린다.
6. 풀 고갈: 기다리는 예약마다 Hikari 연결(기본 10)과 Tomcat 스레드를 쥔다. 재고 조회(`GET /api/inventory/{productId}`)와 /actuator/health 의 DB 확인도 연결을 기다린다(Hikari connectionTimeout 30초). readinessProbe 3초 시간 제한 3회(:75-81)라 풀이 약 30초 넘게 차 있으면 잠깐 준비 안 됨.
7. 상류 시간 초과: product InventoryClient 읽기 10초(commerce/product-service/src/main/resources/application.yml:25-28, InventoryClient.java:23-49 는 시간 초과를 처리하지 않아 500), order ProductClient 10초에 재시도 3회, 게이트웨이 → order 15초(api-gateway application.yml:34-37) 와 재시도 2회. 잠금 구간에 시작한 checkout 은 15초 안에 끝나지 못해 5xx.
8. 회복과 되풀이: 배치가 커밋하면 쌓인 예약이 처리되고(호출한 쪽이 이미 포기한 것도 처리되어 재고가 빠짐), 60초 뒤 2번으로 돌아간다.

## 5. 원인 규정 (원칙 6)

- `root_cause.target_id`: `commerce-inventory`(새 버전 commerce-inventory:1.3.0). 결함은 잠금을 쥔 채 긴 집계를 도는 배치 로직에 있고, 고쳐야 재발이 막히는 곳도 이 버전이다.
- `root_cause.trigger_target_id`: 비움(계기인 배포도 같은 대상).
- 예약 요청(order → product → inventory)은 정당했다. 잠금을 기다린 예약, 시간 초과를 낸 product, order, 게이트웨이는 피해자다.
- `scoring.partial`: `commerce-postgres:inventory_schema.inventory`(잠긴 표까지는 짚었지만 잠금 주인이 새 버전 배치라는 데까지 못 간 답), `commerce-postgres`, `commerce-product`, `commerce-order`(증상 서비스).
- 원본 포스트모템이 지목한 원인(새로 배포한 작업)과 같은 층위다.

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇 |
|---|---|
| 감지 | commerce-order, commerce-product, commerce-gateway 오류율과 지연이 약 105초마다 오름(checkout 15초 시간 초과, 상품 상세 약 10초), commerce-product SEVERE 'Servlet.service() ... threw exception' 급증, inventory 서버 스팬 지연. 업무 규칙 없이 겉 증상으로 이벤트가 생긴다 |
| 피해 판정(러너) | 동반 부하 checkout 5xx 비율(loadgen.checkout_5xx_rate) ≥ 0.1, 3틱 |
| RCA | KCM 롤아웃과 이미지 태그, DPM 세션의 blocked, blocking 짝(같은 inventory 파드 IP), DPM Top SQL 의 새 합계 문장, inventory 배치 로그의 시작과 끝 간격 |

## 7. 관측 근거 (119 실조회, 평시. 표본 데이터만으로 증명하지 않는다)

| 증거 | 119 표와 칸 | 조회와 결과 |
|---|---|---|
| 배치 로그(전수) | CH `lucida_logs_local`, service_name commerce-inventory, body | `SELECT timestamp, substring(body,1,120) FROM lucida.lucida_logs_local WHERE service_name='commerce-inventory' AND timestamp > now() - INTERVAL 4 MINUTE ORDER BY timestamp` → 2026-10-30 02:47:05.638 'Reconciliation batch started: productsChecked=16', 02:47:05.654 'Reconciliation batch finished: productsChecked=16, mismatches=0, restocked=3', 다음 시작 02:48:05.658. 평시 시작과 끝 간격 1초 안. inventory 로그 7일 INFO 35,272, WARN 118, ERROR 16 |
| 행 잠금 대기와 주인(전수 스냅숏) | CH `dpm_session_local`, engine postgresql, body 의 ip, waitType, waitEvent, sessionType, blockingPid, relNames, tranTime, sqlHash | `SELECT timestamp, JSONExtractString(body,'ip'), JSONExtractString(body,'waitEvent'), JSONExtractString(body,'sessionType'), JSONExtractString(body,'relNames'), JSONExtractFloat(body,'tranTime') FROM lucida.dpm_session_local WHERE engine='postgresql' AND toDate(timestamp)='2026-10-20' AND JSONExtractString(body,'waitEvent')='transactionid'` → 10:40:05 ip 10.244.3.66(testbed-inventory 파드 IP, 2026-10-30 kubectl), transactionid, blocked, relNames 'inventory, inventory_pkey, inventory_product_id_key', tranTime 29~90초. 같은 경로의 blocking 쪽 기록도 있다(2026-10-06 transactionid blocking 16건). 평시 PostgreSQL 활성 세션 기록은 시간당 약 60건뿐이고 Lock 대기는 장애 실행 날짜에만 있다 |
| 새 합계 문장 | CH `dpm_topsql_local`, body 의 sqlText, totalExecTime, calls | `SELECT substring(body,1,500) FROM lucida.dpm_topsql_local WHERE engine='postgresql' AND body ILIKE '%inventory_movements%'` → 평시에는 'insert into inventory_schema.inventory_movements ...' 같은 짧은 문장만(avgExecTime 0.2~2.8ms). 릴리스 뒤 sum(quantity) 합계 문장이 호출당 초 단위로 새로 나타나야 한다(시험 실행에서 확인) |
| 롤아웃과 이미지 | CH `kcm_events_local`, body, object_name | `SELECT timestamp, substring(body,1,250) FROM lucida.kcm_events_local WHERE body LIKE '%commerce-product:1.4.0%'` → 2026-10-24 12:28:29 'Container image "commerce-product:1.4.0" already present on machine'. 같은 경로로 commerce-inventory:1.3.0 이 남는다 |
| 상류 실패(전수) | CH `lucida_logs_local`, commerce-product | 7일 'SEVERE Servlet.service() for servlet [dispatcherServlet] ... threw exception' 2,137건(다른 시나리오 실행), 'HikariPool-1 - Connection is not available' 75건. product 는 시간 초과를 처리하지 않아 같은 SEVERE 로 남는다 |
| 합계 비용 | commerce PostgreSQL 읽기 전용 실측(tb-ops kubectl exec psql, default_transaction_read_only) | 상품 5: 45일 238,889행 2,861ms, 상품 6: 230,067행 2,701ms. 30일 120,329행 1,738ms, 90일 582,482행 8,981~9,412ms, 전체 기간 약 9~11초 |

표본 데이터(트레이스 10%, APM 지표)는 보조로만 쓴다. 핵심 증거(배치 로그, DPM 세션과 Top SQL, KCM 이벤트, product 로그)는 전수이거나 장애 동안 계속 찍히는 스냅숏이다.

로컬 확인(2026-10-30, 104): 같은 패치로 만든 jar 와 postgres:16(CPU 0.5, 이동 이력 1,040만 행)에서 Hibernate 가 'select ... from inventory_schema.inventory i1_0 order by i1_0.product_id for share' 를 내고, 배치 도중(11.3초) 보낸 예약 POST /api/inventory/reserve 가 배치가 끝날 때까지 1.9~8.2초 기다린 뒤 200 이었다(배치 밖 예약은 0.0초).

## 8. 감별

- must_support: KCM 롤아웃과 1.3.0 이미지, 배치 로그 시작과 끝 약 45초 간격(평시 1초 안), DPM blocked(inventory 파드 IP, Lock transactionid 또는 tuple, relNames inventory) ↔ blocking(같은 IP, tranTime 수십 초)의 주기적 반복, Top SQL 의 새 합계 문장, 실패가 배치 구간에 맞물림.
- must_rule_out:
  - F01-R, F15-H(외부 세션의 재고 행 잠금): 잠금 주인 IP 가 inventory 파드 자신이고 문장이 원장 합계, 잠금이 주기적으로 걸렸다 풀린다.
  - F23-R(보충 정지): 409 가 아니라 5xx, 보충 로그가 이어진다.
  - F20-R(공유 PG 질의 폭주): 재고를 거치지 않는 경로는 정상, 대기가 CPU 가 아니라 Lock.
  - F25-H, F71-R, F71-H(DB 다운, 계정, 접근 규칙): PostgreSQL Ready, 인증 오류 없음.
  - F09-P, F05-H(자원, 프로브): 바뀐 것은 이미지뿐.
  - F76-R, F43-R(다른 서비스 릴리스): product, order 이미지 그대로.
  - F66-R, F73-R(열 추가 마이그레이션 대기열): ALTER 와 relation 대기가 없고 행 대기다.
- contrast_with: F01-R, F69-R, F23-R, F20-R, F42-R(정답지 related_scenarios).

## 9. 러너 판정 조건과 강도, 부하 계산

- 주입: k8s.image release 모드, 계약 `approved-fixed-f69-p`(testbed-inventory, inventory-service, commerce-inventory:latest → commerce-inventory:1.3.0). 결정적 결함이라 강도 사다리가 없다. 강도는 패치 안 기간 기본값(45일)이 정한다.
- 잠금 시간: 상품당 2.70~2.86초 × 16 ≈ 45초(+ 시작 직전 잔액 조회 16번, ms 단위). 주기 = 잠금 + 60초 ≈ 105초. 데이터는 하루 상품당 약 4~5천 행씩 쌓여 45일 창 안의 행 수가 거의 일정하다.
- 재시작 여유: 풀이 차는 시점(잠금 시작 뒤 몇 초) 부터 풀릴 때까지 약 40초 < liveness 실패 5회 × 15초 = 75초라 재시작은 계획에 없다. 잠금이 75초를 넘을 만큼 PostgreSQL 이 느려지면 inventory 가 재시작되고 기동 직후 배치가 다시 잠그는데, 원인과 정답은 같다(원본의 '작업이 죽고 다시 뜨며 잠금을 다시 잡음'과 같은 꼴).
- 부하: 기준선 예약 약 0.84건/초(119 inventory 서버 스팬 POST /api/inventory/reserve 1시간 304건, 표본 10%), checkout 약 0.56건/초. 동반 surge.js 3rps(여정 50% checkout, 주문당 예약 약 1.5건)로 예약 약 3건/초 더해 잠금 구간 몇 초 안에 풀(10)이 찬다. target_rps 3 은 상한 180 의 2% 이고 F76-R 과 같은 값이다.
- 피해 계산: checkout 은 '잠금 시작 ~ 잠금 끝 15초 전'에 시작하면 게이트웨이 15초 안에 끝나지 못해 5xx 다(약 30초/105초 ≈ 29%). 풀이 찬 동안 상품 상세도 10초 시간 초과. 60초 창 checkout 5xx 비율은 0~0.5 사이를 오가고, 실패 구간과 6초 넘게 겹치는 창(105초 중 약 78초)에서 0.1 을 넘는다.
- 판정: success checkout_5xx_rate ≥ 0.1(3틱), must_rule_out achieved_rps < 1, PostgreSQL NotReady, checkout 409 ≥ 0.3, abort 진입 0. min_hold 8m, timeout 14m, max_injection_duration 20m.
- 정리: 이미지를 되돌리고 available 확인, tb-w1 containerd 의 1.3.0 참조 제거. 1.3.0 이 첫 대사에서 남긴 보정 ADJUST 행은 resulting_stock 이 그때 stock 이라 옛 배치 대사와 어긋나지 않는다.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(KnowBe4 2025-10-13, 공식 상태 페이지 사후 보고, 링크)와 요소별 대응표, 기전이 같다(§2).
- [x] 장부를 갱신했고(§4-1 행, §6 기록) 묶음 J+D(J 21/118 = 17.8%), 정답 위치 재고 서비스(3), 결제 경로 14(12%), 서비스 동률 39, 고른 이유를 적었다. 어느 축도 20% 에 닿지 않는다(§3).
- [x] 근본 원인 위치를 file:line 으로 확인했다(§4, 패치 줄, 매니페스트 줄).
- [x] 근본 원인의 흔적이 119 실데이터에서 조회됐다: 배치 로그, DPM 세션의 inventory 파드 IP 행 잠금 대기, Top SQL, KCM 이미지 이벤트(§7).
- [x] 핵심 증거가 표본 데이터에만 있지 않다(로그 전수, DPM 스냅숏, KCM 이벤트).
- [x] 계기의 흔적이 조회됐다(KCM 롤아웃과 이미지 태그). 인공 지연 없음: 지연은 실제 집계 질의와 실제 잠금에서 난다.
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다(잠금 주인 세션의 IP 와 문장, 배치 로그, 롤아웃). 코드 설계 결함 추론이 필요하지 않다.
- [x] 정답지 세 칸이 원칙 6대로 채워졌다(§5).
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다(§6).
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다(패치가 더한 문자열에 F69, fault, bug, chaos, scenario 없음, 시험이 확인).
- [x] 부하 상한과 시간 제한 사슬을 고려한 피해 계산이 있다(§9).

### 후보 목록 (이번 반복, "실제 기전 × 부품", 숫자 순)

| # | 후보 | 부품 | 원본 | 결과 |
|---|---|---|---|---|
| 1 | inventory 릴리스의 재조정 배치가 재고 행 공유 잠금을 쥔 채 기간 원장 합계 | commerce inventory | KnowBe4 2025-10-13 | 채택(F69-P) |
| 2 | cart Redis 캐시 비우기, TTL 0 배포로 DB 몰림 | commerce Redis, cart | Slack 2022-02-22, Shepherd 2026-02-26 | 버림: cart 가 캐시 실패를 DB 폴백으로 흡수하고 풀 20, ms 질의라 피해 계산이 서지 않음(rejected 37, 197행 벽) |
| 3 | 게이트웨이 토큰 검증이 user 의 틀린 주소를 보게 하는 설정 롤아웃 | commerce 게이트웨이, user | GitHub 2026-06-10 | 버림: 증상이 쓰기 401(4xx) 하나라 원칙 7(F34-R 교훈), 설정 롤아웃의 하류 주소 오설정은 F39-R 꼴 |
| 4 | product 목록이 상품마다 재고를 부르는 릴리스(호출 폭증) | commerce product, inventory | Cloudflare 2025-09-12 | 버림: 목록 약 4건/초 × 20 × 2.5ms ≈ 동시 0.2 로 풀 10 에 못 미침(원칙 9), 같은 원본을 F47-R, F16-P 가 이미 씀 |
| 5 | 한 서비스 풀 상한 과다로 commerce PostgreSQL 연결 한도 고갈 | commerce PostgreSQL | Resend 2026-02-15 | 버림: 어느 풀이 굶을지 수명 교체 순서에 달려 무작위(rejected 334행 벽) |
| 6 | banking 원장 대사 배치를 무겁게 하는 릴리스 | banking ledger, Oracle | Slack 2025-11-01 | 버림: fixedDelay 단일 스레드라 Oracle CPU 2 중 하나 이상을 못 쓰고 원장은 비동기 경로(rejected 302행 벽) |
| 7 | 노드 오버레이(flannel.1) 대역 제한 설정의 범위 착오 | 노드 네트워크 | Google Cloud 2019-06-02 | 버림: 원본은 네트워크 제어면 작업 해제로 용량이 준 것이라 tc 제한은 기전이 바뀌고, netem 꼴은 원칙 4 로 반려됨(rejected 112, 231행) |
| 8 | food notify 소비자 독 메시지 | food notify, Kafka | Cloudflare 2023-01-24, PostHog 2026-07-23 | 버림: 소비가 비동기이고 Spring Kafka 기본 오류 처리기가 재시도 뒤 건너뛰어 사용자 증상 없음(원칙 7, rejected 146행 벽) |
| 9 | inventory 재고 응답 모양을 바꾼 릴리스 | commerce inventory, product | Onfido 2024-10-18 | 버림: F49-R, F39-H 와 같은 꼴의 세 번째 서비스 이름 바꾸기(카탈로그 §1) |
| 10 | user-service(maxSurge 0) 롤아웃의 기동 확인 실패로 쓰기 401 | commerce user | PostHog 2025-10-28 | 버림: F17-H, F50-R 과 같은 '새 파드가 못 뜨고 옛 파드가 먼저 내려감' 꼴의 서비스 이름 바꾸기, rejected 341행(기동 확인 문턱 축소) |
