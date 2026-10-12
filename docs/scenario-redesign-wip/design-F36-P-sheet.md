---
title: F36-P 설계 시트 — food 메뉴 표 열 추가 마이그레이션이 판매 실적 내보내기 뒤에서 메타데이터 잠금 대기열을 만듦
status: Draft
owner: project
last_reviewed: 2026-10-12
tags:
  - scenario
  - design
summary: 메뉴를 주문 품목과 이어 읽는 느린 판매 실적 내보내기가 food MySQL menus 의 공유 메타데이터 잠금을 쥔 사이 INSTANT 열 추가 마이그레이션이 배타 잠금을 기다려, 그 뒤의 메뉴 읽기가 모두 줄을 서고 restaurant 풀이 차서 약 6분 동안 메뉴 보기와 음식 주문이 불가능해지는 후보 시나리오의 설계 근거.
---

# F36-P 설계 시트

## 1. 요약과 비유

운영자가 food MySQL 의 작은 메뉴 표 `fooddelivery.menus`(95행)에 알레르기 안내 열을 더하는 마이그레이션
`ALTER TABLE menus ADD COLUMN allergen_note varchar(255) NULL, ALGORITHM=INSTANT` 를 돌린다. 데이터 사전만 바꾸는
안전한 변경이지만 그 순간 표의 배타 메타데이터 잠금이 필요하다. 몇 초 앞서 분석 쪽의 메뉴별 판매 실적 내보내기가
주문 품목(약 258만 행)을 메뉴 표와 이어 읽기 시작해 약 6분 동안 menus 의 공유 메타데이터 잠금을 쥐고 있다.
마이그레이션은 기다리고, 기다리는 배타 요청이 새 공유 요청보다 앞서므로 그 뒤의 메뉴 읽기가 모두 줄을 선다.
메뉴는 손님이 메뉴를 볼 때와 주문할 때마다 읽히므로 restaurant 의 연결 풀이 몇 초 만에 차고, 약 6분 동안 메뉴를 열 수도
주문을 낼 수도 없다. 내보내기가 끝나면 마이그레이션이 지나가고 줄이 스스로 풀린다.

비유: 식당 메뉴판을 고치려고 "잠깐 메뉴판 좀 주세요" 하고 줄을 섰는데, 앞 사람이 메뉴판을 들고 6분째 매출 장부와
맞춰 보는 중이다. 고치는 사람이 줄 맨 앞에 서 있는 동안, 메뉴판을 보려는 손님 모두가 그 뒤에 선다.

## 2. 원본 사례

- **Honeycomb, 2024-08-06** "UI and API unavailable" 공식 상태 페이지 사후 보고: https://status.honeycomb.io/incidents/ycgvhvs201q1
  (자료 문서 `ref-real-world-incidents.md` M21 에 이미 있음).
- 요약: `teams` 표에 `INSTANT` 알고리즘으로 열 하나를 더하는 기술적으로 안전한 마이그레이션이, 몇 초 앞서 시작된 같은 표를 읽는
  느린 SELECT(일부 고객 조직에서 5분 넘게 걸림) 뒤에서 메타데이터 잠금을 기다렸고, 그 잠금 요청이 인증을 포함한 그 표의 다른
  질의를 막아 API 요청의 75% 를 약 20분 동안 거절했다. 장애가 배포 직후에 시작돼 진단이 어려웠고, 후속으로 느린 질의를 점검하고
  클라이언트 시간 제한과 맞는 DB 쪽 시간 제한을 두기로 했다.

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | `teams` 표에 INSTANT 열 추가 마이그레이션 배포 | 운영자가 `menus` 표에 `allergen_note` 를 INSTANT 로 더하는 마이그레이션 실행 |
| 원인이 된 결함 | 같은 표를 읽는 5분 넘는 느린 SELECT 가 공유 메타데이터 잠금을 쥔 상태에서 배타 잠금 요청이 대기, DB 쪽 시간 제한 없음 | 메뉴를 주문 품목과 이어 읽는 판매 실적 내보내기(약 360초 활성)가 공유 메타데이터 잠금을 쥔 상태에서 배타 잠금 요청이 대기, lock_wait_timeout 기본 1년 |
| 전파 경로 | 기다리는 배타 요청 뒤에 인증 경로의 `teams` 질의가 모두 줄을 섬 → API 요청 거절 | 기다리는 배타 요청 뒤에 restaurant 의 메뉴 읽기가 모두 줄을 섬 → restaurant Hikari 풀 고갈, NotReady 와 재시작 → 메뉴 보기 실패, 주문 흐름이 메뉴 단계에서 멈춤, order 의 메뉴 확인 실패(502) |
| 사용자 증상 | API 요청 75% 거절, UI 사용 불가 | 메뉴 보기 500/거절, 새 음식 주문 불가(약 6분) |
| 원본의 탐지 경로 | 13:00 경 저하 감지, 13:04 경보, DB 성능과 스키마 마이그레이션의 상관을 좁히는 데 몇 분 | DPM 세션 스냅숏의 'Waiting for table metadata lock' 과 긴 'Sending to client' 세션, restaurant 풀 고갈 로그, KCM Unhealthy/Killing |
| 완화와 복구 | 느린 질의가 끝나 잠금이 풀리고 회복(13:29), 느린 질의 점검과 DB 쪽 시간 제한 | 내보내기가 끝나면 열 추가가 즉시 끝나고 줄이 풀림, cleanup 이 열을 지움 |

기전 대응: 원본의 핵심 고리 "요청마다 읽히는 작은 표 + 그 표를 오래 읽는 느린 질의 + INSTANT 열 추가의 배타 메타데이터 잠금 대기
→ 그 표의 모든 질의가 막힘"을 그대로 둔다. 원본의 `teams` 는 인증마다 읽히는 작은 표였고 느린 SELECT 는 큰 고객 데이터를 함께
읽어 5분 넘게 걸렸다. 테스트베드의 menus 도 메뉴 보기와 주문마다 읽히는 작은 표이고, 느린 읽기는 큰 주문 품목 표를 메뉴 표와
이어 읽어 오래 걸린다. 같은 수단을 큰 표 자기 행에 쓴 F60-H(dispatches), F53-H(payments)보다 원본의 표 꼴에 더 가깝다.
이어 읽기는 억지 조인이 아니다: order_items 에는 메뉴 이름이 없어 판매 실적에 메뉴 이름을 붙이려면 menus 와 이어야 하고,
restaurant 의 인기 메뉴 집계(`MenuRepository.java:17-22`)가 이미 같은 이음(order_items JOIN menus)을 쓴다.

## 3. 숫자 근거 (2026-10-12, `scenario-stats.py`, 정식 + 후보)

- 서비스: commerce 34, core-banking 34, food-delivery 34(동률). 2026-10-12 균형 규칙(main 4a10ac0)으로 가장 적은 도메인 중
  어느 것이든 고르고 정답 위치는 20% 상한만 본다. 음식배달을 골랐다(가장 직접 실측할 수 있는 DB, 정답 위치 0인 표가 요청 경로에 있음).
  만든 뒤 음식배달 35.
- 묶음: D(DB 잠금) 15/102(14.7%) → 16/103(15.5%), 20% 아래.
- 정답 위치: DB 테이블(메뉴) 0 → 1(새 위치). 결제 경로 13/102 그대로(12.6%).
- 현실 근거: 원인 분포상 DB 잠금은 상위 원인이 아니지만, 계기는 계획된 스키마 마이그레이션(설정, 변경 배포 계열)이고 원본이
  공식 사후 보고로 있다. 정답 위치를 처음 쓰는 표(메뉴)로 옮겨 "애매하면 배차나 결제를 찍는" 지름길을 막는다.

### 후보 목록 (실제 기전 × 부품, 서로 다른 부품 7개)

| 순서 | 후보 | 원본 사례 | 부품 / 정답 위치(현재 수) | 결과 |
|---|---|---|---|---|
| 1 | food menus INSTANT 열 추가가 메뉴 판매 실적 내보내기 뒤에서 메타데이터 잠금 대기 | Honeycomb 2024-08-06 | food MySQL menus / DB 테이블(메뉴) 0 | **채택(F36-P)** |
| 2 | banking OUTBOX_EVENTS identity 생성기 상태 미이전으로 이체 INSERT ORA-00001 | RevenueCat 2022-11-23 | banking Oracle OUTBOX_EVENTS / DB 테이블(이체 이벤트 outbox) 0 | 버림: 카탈로그 §1(F57-R 과 엔진, 진입 경로, 영향이 같고 같은 이체 트랜잭션의 표만 다름) |
| 3 | food payment_outbox_events 열 추가 대기열 | Honeycomb 2024-08-06 | food MySQL payment outbox / 0 | 버림: 원칙 1(이미 Kafka 로 나간 outbox 를 느리게 내보낼 자연스러운 읽는 쪽이 없음), 같은 표의 F60-P 복제 지적(rejected 318행) |
| 4 | commerce Kafka 브로커 설정 롤아웃 | GitHub 2026-03-05 | commerce Kafka / 메시지 브로커 0 | 버림: 이번 실행에서 막힘(원칙 7·9, outbox 비동기라 사용자 증상 없음) |
| 5 | banking nginx 라우트 또는 레이트 리밋 설정 | PostHog 2025-10-24, GitHub 2026-03-05 | banking nginx / 게이트웨이 0 | 버림: 이번 실행에서 막힘(원칙 3, nginx 119 미관측) |
| 6 | 클러스터 DNS 전달자 오설정 | Let's Encrypt 2025-07-21, Intercom 2024-11-05 | CoreDNS / DNS 0 | 버림: 이번 실행에서 막힘(원칙 3·4, kube-system 미수집) |
| 7 | commerce 쿠폰 표 일괄 비활성 또는 빈 값 | Atlassian 2022-04-05, Google Cloud 2025-06-12 | commerce PG coupons / DB 테이블(쿠폰) 0 | 버림: 원칙 7(쿠폰 무효는 400 업무 거절 단일 신호, F34-R 교훈), 열이 NOT NULL DEFAULT 0 이라 빈 값 경로 없음 |
| 8 | DB 자격 증명 Secret 회전 실수 | Harness 2026-01-08 | banking Secret / 비밀값 0 | 버림: rejected 213행과 같은 원본·주입(원칙 1) |
| 9 | food customers, riders 표 고장 | (사례 미선정) | food MySQL / 0 | 버림: 앱 코드가 두 표를 읽지 않음(init.sql 장식 표), 사용자 증상 없음 |
| 10 | commerce 배송, banking 원장 표 고장 | Chargebee 2018-03-02 등 | shipments, LEDGER_ENTRIES / 0 | 버림: 이미 막힘(원칙 7, 소비자 비동기) |

## 4. 인과 사슬 (코드와 인프라 위치)

1. 내보내기 `SELECT m.id, m.name, m.price, i.order_id, i.qty, i.unit_price FROM menus m JOIN order_items i ON i.menu_id = m.id`
   가 앱 계정으로 `mysql --quick` 한 행씩 받기, 주문 품목 행 추정/360 행마다 1초 쉬는 읽기 쪽으로 약 360초 활성
   (`'Sending to client'`). menus 와 order_items 의 SHARED_READ 메타데이터 잠금을 쥔다(로컬 실측의 performance_schema.metadata_locks).
2. 마이그레이션 `ALTER TABLE menus ADD COLUMN allergen_note varchar(255) NULL, ALGORITHM=INSTANT`(root)가 restaurants,
   menu_categories, order_items 의 SHARED_UPGRADABLE 은 얻고 menus 의 EXCLUSIVE 를 PENDING 으로 기다린다
   (`lock_wait_timeout` 31536000).
3. 그 뒤의 menus 질의가 모두 대기: restaurant `RestaurantService.java:61-68`(getMenu → `MenuRepository.java:12` findByRestaurantId),
   `PopularMenuBatch.java:37-44`(1시간마다). order_items 는 `fk_order_items_menu`(`db/init.sql:87-96`)로 menus 를 가리켜
   주문 품목 INSERT 도 부모 표 잠금을 기다린다. restaurants 읽기, orders INSERT, order_items 읽기와 DELETE 는 막히지 않는다.
4. restaurant Hikari `maximum-pool-size 10, connection-timeout 3000`(`restaurant-service/.../application.yml:12-15`)이 기다리는
   메뉴 읽기로 차고, 뒤의 요청(가게 상세, 검색 포함)은 3초 뒤 'Connection is not available' 500.
5. `/actuator/health`(DB 확인)가 연결을 못 얻어 readiness(10초 x 3) 실패로 Service 에서 빠지고, liveness(15초 x 5, 약 75초)가
   재시작(`k8s/21-restaurant-deploy.yaml:67-86`). 죽은 파드의 서버 쪽 세션은 그대로 기다리고 새 파드도 첫 메뉴 읽기에서 막힌다.
6. 손님 앱(기준선 `loadgen/script.js:116-125` orderJourney)은 메뉴를 먼저 조회하고 200 이 아니면 주문을 내지 않는다. order 에
   닿은 주문은 `OrderService.java:71-91` 메뉴 확인이 `RestaurantClient.java:58-83`(read-timeout 5초, 재시도 3회, 서킷 창 10)에서
   실패해 502 'Restaurant service unavailable'.
7. 내보내기가 끝나면 ALTER 가 즉시 끝나고 줄이 풀린다. cleanup 이 `DROP COLUMN ... ALGORITHM=INSTANT`.

## 5. 원인 규정 (원칙 5, 6)

- `root_cause.target_id`: `food-mysql:fooddelivery.menus` (그 표의 메타데이터 잠금 대기열. F60-H, F53-H 와 같은 꼴)
- `trigger_target_id`: 비움. 계기(마이그레이션)와 결함(그 표를 오래 읽는 질의와의 겹침)이 같은 표에서 일어난다.
- `scoring.partial`: `food-delivery-restaurant`, `food-restaurant`(증상을 낸 서비스), `food-mysql`(인스턴스까지만 짚은 답).
- 원본 사후 보고가 "같은 표의 느린 SELECT 와 INSTANT 열 추가의 메타데이터 잠금 대기"를 원인으로 지목한 층위와 같다. restaurant,
  order 의 코드와 설정은 정당한 요청을 처리했을 뿐이다. 관제 데이터(DPM 세션 상태, 풀 로그)로 낼 수 있는 결론이고 코드 설계 결함
  추론이 필요 없다.

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 |
|---|---|
| 감지 | restaurant 오류율과 지연(APM), restaurant 오류 로그 급증('Connection is not available', 'DataSource health check failed'), KCM Unhealthy/Killing |
| 피해 판정(러너) | 기준선 loadgen-food 의 메뉴 보기(read step `menu`) 실패율 ≥ 0.5, 3틱 |
| RCA | DPM 세션 스냅숏의 'Waiting for table metadata lock'(root ALTER, restaurant 파드 세션)과 앱 계정 'Sending to client' 긴 세션, active_session 상승과 평시 CPU, 롤아웃 없는 재시작 |

## 7. 관측 근거 (119 실조회, 2026-10-12, 평시)

| 증거 | 119 표/칸 | 조회 | 결과 |
|---|---|---|---|
| DB 세션 상태 | CH `lucida.dpm_session_local` body JSON `state`, `user`, `hostname`, `queryTime` | `SELECT JSONExtractString(body,'state') st, count() FROM lucida.dpm_session_local WHERE engine='mysql' AND timestamp > now() - INTERVAL 60 DAY GROUP BY 1` | 'Waiting for table metadata lock' 8행(전부 2026-10-09 03:43 한 스냅숏, 10.244.2.82 와 root), 'Sending to client' 0행. 장애 때 둘 다 새로 나타난다 |
| 세션 수 | VM `dpm.mysql.session.active_session` | `max_over_time(...[7d])`, `quantile_over_time(0.5, ...[7d])` | 최대 12, 중앙 1. current_session 최대 75, 중앙 56 |
| restaurant 풀 고갈 | CH `lucida_logs_local` service_name `food-delivery-restaurant`, body | `... body ILIKE 'HikariPool-1 - Connection is not available%' AND body NOT ILIKE '%total=0%'` 7일 | 0건(전수 로그). total=0 꼴 139건은 같은 주 'Communications link failure' 141건과 함께 MySQL 연결이 끊긴 때. 'DataSource health check failed' 5건 |
| 재시작 | CH `kcm_events_local` object_name `testbed-restaurant%` | 30일 reason 별 | Unhealthy 60, Killing 0. 장애 때 Killing 과 롤아웃 없는 재시작이 새로 남는다 |
| 질의 텍스트 | CH `dpm_topsql_local` body `sqlText` | `body ILIKE '%menus%'` 1일 | 무거운 질의만 남음(인기 메뉴 집계의 EXPLAIN). 내보내기와 ALTER 는 끝난 뒤 Top SQL 에 긴 실행 시간으로 남을 수 있으나 핵심 증거로 쓰지 않는다 |
| restaurant APM | VM `apm.agent.otel.java.*` service_name `food-delivery-restaurant` | 지표 목록, `avg_over_time(rps[1d])` | rps, error_rate, db_responsetime 있음(표본 기반이라 추세 보조로만) |

- 핵심 증거(DPM 세션 스냅숏, 전수 로그, KCM 이벤트)는 표본 데이터가 아니다. 트레이스와 APM 지표는 보조로만 쓴다(원칙 3).
- 계기의 흔적(원칙 4): 마이그레이션 세션(root@localhost, 'Waiting for table metadata lock')과 내보내기 세션(앱 계정, 'Sending to
  client', queryTime 증가)이 DPM 세션 스냅숏에 1분마다 남는다. 인위 지연(SLEEP)은 없다: 받는 쪽이 천천히 받아 문장이 활성으로 남는다.
- DB 실측(109 읽기 전용, 2026-10-12): menus 95행, order_items 행 추정 2,583,629(실제 2,588,317), menus total_row_versions 0,
  lock_wait_timeout 31536000, max_execution_time 0, net_write_timeout 60, mdl 계측 YES, 열린 세션 20(restaurant 10.244.2.218 이 5개).
- 로컬 실측(mysql 8.0.46, 같은 표 정의, menus 95행 + order_items 40만 행): 대기 중 menus 읽기·order_items INSERT·restaurants UPDATE 가
  lock_wait_timeout(4초)까지 대기, restaurants 읽기·orders INSERT·order_items 읽기와 DELETE·menu_categories 읽기는 0.1초대.
  이 계약 그대로 db.ddl 실행기를 가짜 kubectl 로 돌려 preflight 통과, run 약 4초에 큐 형성, 내보내기 약 359초 유지, cleanup 뒤 열 없음,
  recovery 와 다시 preflight 통과를 확인했다.

## 8. 감별

- must_support: 정답지 `must_support` 7개(DPM 세션 상태 2종, active_session 상승과 평시 CPU, restaurant 풀 고갈 로그, 롤아웃 없는
  restaurant 재시작, 메뉴 보기 실패, MySQL/order Ready 와 다른 표 정상, 내보내기 종료 시 함께 회복).
- must_rule_out: F36-R(1054 없음, 질의가 기다림), restaurant 릴리스(F42-P, F49-R, F36-H: 이미지와 ReplicaSet 그대로, MySQL CPU 평시),
  다른 표 대기열(F60-H, F53-H: 대기 세션 출처가 restaurant, 배달 조회와 결제 정상), F48-R(1146 없음), F44-R(파드 안 풀 고갈과 DB
  잠금 대기), MySQL 다운·읽기 전용(F25-H, F38-R).
- contrast_with: F36-R, F60-H, F53-H, F42-P, F48-R. 증상이 같은 F42-P(restaurant 와 메뉴 쪽 실패)와는 MySQL CPU 와 이미지로,
  원인 수단이 같은 F60-H 와는 대기 세션의 출처와 막힌 경로로 갈린다.

## 9. 러너 판정과 강도, 부하

- 설계 강도 하나(평가 모드): `approved-fixed-f36-p`, export_seconds 360, drain_seconds 300, minimum_rows 50(menus), maximum_sessions 90.
- 성공: 기준선 메뉴 보기 실패율 ≥ 0.5, 3틱(min_hold 90s, timeout 6m). 주문 생성은 기준선 주문 여정이 메뉴 단계에서 멈춰 표본이
  끊기므로 판정에 쓰지 않는다.
- 부하 계산: 기준선 1~6 iter/s(최대 VU 30)의 55% 가 메뉴 보기라 30초 창에 약 16~100건. restaurant 메뉴 요청은 둘러보기 55% + 주문
  여정 20% 로 초당 약 0.75~4.5건이라 Hikari 10개가 약 2~13초에 찬다. 그 뒤 요청은 3초 뒤 500, NotReady 동안 연결 거절이라 창 실패율은
  1 에 가깝다(피해가 계산상 확실, 원칙 9). 동반 부하는 없다.
- 세션 여유: 평시 열린 세션 20, restaurant 가 6분 동안 약 3번 재시작하면 대기 세션 약 30개가 더해져 max_connections 151 아래다.
  preflight 상한 90.
- abort(entry_status == 0, 2틱): order 는 Ready 로 남고 자기 표는 줄에 서지 않는다. 드문 경우(마이그레이션이 대기에 든 순간 메뉴
  확인을 마친 주문 하나가 주문 품목 INSERT 에서 줄을 서면) k6 시간 제한 60초 뒤 마지막 주문 생성 상태가 0 으로 남아 abort 될 수 있다.
  주문 생성 초당 0.2~1.2건 x 그 구간 수십 ms 로 실행당 약 1~5% 로 본다. 그 경우는 환경 사유의 재실행 대상이다.
- recovery: target_health 200, MySQL·order Ready, 메뉴 보기 실패율 < 0.2(2틱, timeout 10m).

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Honeycomb 2024-08-06, 공식 상태 페이지 사후 보고)와 요소별 대응표, 기전 동일(§2)
- [x] 장부 갱신(§4-1 행, §6 기록), 묶음 D 16/103(15.5%), 정답 위치 DB 테이블(메뉴) 1, 결제 경로 13/103, 서비스 음식배달 35, 고른 이유(§3). 어느 축도 20% 미만
- [x] 근본 원인 위치: food MySQL fooddelivery.menus 와 `RestaurantService.java:61-68`, `MenuRepository.java:12`, `db/init.sql:87-96`(G1)
- [x] 근본 원인 흔적의 119 실조회: `dpm_session_local` state, `dpm.mysql.session.active_session`(§7)
- [x] 핵심 증거가 표본 데이터에만 있지 않음: DPM 세션, 전수 로그, KCM 이벤트
- [x] 계기 흔적 조회: 마이그레이션, 내보내기 세션이 DPM 에 남음. 인위 지연 없음
- [x] 정답이 관제 데이터로 낼 수 있는 결론(표의 메타데이터 잠금 대기열)
- [x] 정답지 세 칸이 원칙 6대로(§5)
- [x] 감지, 피해 판정, RCA 증거 구분(§6)
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않음(문장, 파드 안 파일 이름 /tmp/table-export.sh 에 id 없음, 테스트로 확인)
- [x] 부하 상한과 서킷을 고려한 피해 계산(§9)
