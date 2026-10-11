---
title: F60-H 설계 시트 (배차 표 열 추가 마이그레이션이 같은 표의 느린 내보내기 뒤에서 메타데이터 잠금을 기다려 배차 표 질의가 모두 줄을 서고, dispatch 풀 고갈로 모든 새 음식 주문 503)
status: Draft
owner: project
last_reviewed: 2026-10-11
tags:
  - scenario
  - design
  - mysql
  - migration
  - lock
summary: 배차 실적을 분석 저장소로 옮기는 느린 내보내기가 food MySQL fooddelivery.dispatches 의 공유 메타데이터 잠금을 쥔 사이, 운영자가 같은 표에 null 허용 열을 ALGORITHM=INSTANT 로 더하는 마이그레이션을 돌려 그 ALTER 가 배타 메타데이터 잠금을 기다리는 동안 그 뒤의 배차 표 질의(읽기 포함)가 모두 줄을 서고, dispatch 풀이 차서 약 6분 동안 모든 새 주문이 503 이 되는 시나리오. 원본은 Honeycomb 2024-08-06(MySQL teams 표 INSTANT 열 추가가 느린 SELECT 뒤에서 메타데이터 잠금을 기다리며 그 표의 모든 질의를 막아 API 요청 75% 거절). F60-R 과 같은 증상의 다른 원인(H).
---

# F60-H 설계 시트

## 1. 요약

food 의 dispatch-service 는 거의 모든 경로에서 `fooddelivery.dispatches` 를 부른다: order 가 주문을 저장하기 전에 묻는 배달원 용량(COUNT), 배달원 배정(INSERT), 배달 목록과 상세 읽기, 6초마다 보존 정리, 30초마다 만료 배치. 분석팀의 내보내기 작업이 배차 실적 전체(약 174만 행)를 한 행씩 받아 일정한 속도로 분석 저장소에 옮기고 있다(`SELECT id, order_id, courier_id, eta_minutes, status, assigned_at FROM dispatches`, 약 6분 동안 활성). 그 몇 초 뒤 운영자가 취소 사유를 담을 열을 더하는 마이그레이션을 돌린다:

```sql
ALTER TABLE dispatches ADD COLUMN cancel_reason varchar(64) NULL, ALGORITHM=INSTANT
```

MySQL 8.0 에서 INSTANT 열 추가는 데이터 사전만 바꾸는 안전한 변경이지만, 그 순간에 표의 배타 메타데이터 잠금이 필요하다. 내보내기가 공유 메타데이터 잠금을 쥐고 있어 ALTER 는 `Waiting for table metadata lock` 으로 기다리고(lock_wait_timeout 기본값 1년), 기다리는 배타 요청은 새 공유 요청보다 앞서므로 그 뒤의 배차 표 질의는 단순 읽기까지 모두 ALTER 뒤에 줄을 선다. dispatch 의 Hikari 10개가 몇 초 만에 기다리는 질의로 차고, order 의 용량 확인이 실패해 모든 새 주문이 503 이 되며 dispatch 는 health 실패로 빠지고 재시작된다. DB 에서는 아무 문장도 실패하지 않고, MySQL 은 한가하며, 다른 표는 평시대로 답한다. 내보내기가 끝나면 ALTER 가 잠금을 얻어 즉시 끝나고 줄이 풀린다.

비유: 도서관 배차 장부를 분석팀 직원이 한 장씩 천천히 베끼고 있다(장부를 펴 둔 상태). 사서장이 "장부에 '취소 사유' 칸을 하나 그어 넣겠다"며 장부 앞에 섰다. 칸 하나 긋는 데는 1초면 되지만 베끼던 사람이 덮을 때까지 기다려야 하고, 사서장이 줄 맨 앞에 서 있으니 장부를 보러 온 사서 열 명이 모두 그 뒤에 선다. 대출 창구는 "배달원 남았나요?" 를 물어볼 사서가 없어 손님을 돌려보낸다. 베끼기가 끝나면 칸은 1초 만에 그어지고 줄이 풀린다.

## 2. 원본 사례

- 기업: Honeycomb
- 날짜: 2024-08-06 12:59:39~13:20:05 PDT(사후 보고 2024-08-07 15:42 PDT 게시)
- 링크: [공식 상태 페이지 사고 "UI and API unavailable"과 사후 보고](https://status.honeycomb.io/incidents/ycgvhvs201q1)
- 요약(`docs/ref-real-world-incidents.md` M21 에 이미 있는 행, 2026-10-11 추가분): 스키마 마이그레이션은 기술적으로 안전한 변경이었다("a column addition to the `teams` table using an `INSTANT` algorithm"). 그런데 마이그레이션 몇 초 앞서 같은 표를 읽는 질의가 시작됐고, 그 느린 SELECT 는 일부 고객 조직에서 5분 넘게 걸릴 수 있었다. 마이그레이션이 메타데이터 잠금을 얻으려다 그 질의 뒤에서 기다리는 동안("the `ALTER` statement itself waits for already running queries and transactions using this table to terminate") 그 잠금 요청이 인증을 포함한 그 표의 다른 질의를 막았다. 약 17분 동안 텔레메트리의 약 4분의 1 을 버리고 API 가 요청의 75% 를 거절(대부분 /1/auth), UI 는 적어도 19분 쓸 수 없었다. 후속: 그 느린 질의를 점검하고 클라이언트 쪽 시간 제한과 맞는 DB 쪽 시간 제한을 두기로 했다.
- 자료 문서에 이미 있어 새로 더하지 않는다. 같은 원본을 PostgreSQL 로 옮긴 F66-R, F66-P 가 있고, 이 후보는 원본 엔진(MySQL, `ALGORITHM=INSTANT`, 메타데이터 잠금)이다.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | teams 표에 INSTANT 알고리즘으로 열을 더하는 마이그레이션 | dispatches 에 `ADD COLUMN cancel_reason varchar(64) NULL, ALGORITHM=INSTANT` 마이그레이션 |
| 원인이 된 결함 | 몇 초 앞서 시작된 5분 넘는 느린 SELECT 가 같은 표를 쓰는 동안 마이그레이션이 메타데이터 잠금을 기다리고, 그 대기가 뒤의 모든 질의를 막음 | 앱 계정의 내보내기 SELECT(dispatches 전체, 한 행씩 일정 속도로 받아 약 6분 활성)가 공유 메타데이터 잠금을 쥔 사이 ALTER 가 `Waiting for table metadata lock`, 뒤의 dispatches 질의 모두 대기 |
| 전파 경로 | 그 표를 읽는 인증 경로가 막혀 API 요청 대부분이 거절됨 | 배차 표를 읽고 쓰는 dispatch 의 모든 경로가 막혀 Hikari 10개 고갈 → order 용량 확인 실패, 서킷 열림 → 모든 새 주문 503, dispatch NotReady 와 재시작 |
| 사용자 증상 | API 요청 75% 거절, UI 19분 사용 불가 | 약 6분 동안 음식 주문 전량 503 'Dispatch service unavailable', 배달 조회 실패. 둘러보기, 주문 조회는 정상 |
| 탐지된 경로 | 13:00 경 저하를 알아채고 13:04 경보, 배포 직후라 DB 성능과 마이그레이션의 상관을 좁히는 데 몇 분 | lucida 이상 탐지(order 오류 로그와 503, dispatch 풀 고갈 로그, KCM Unhealthy, DPM 세션 수 상승), 인시던트 |
| 완화와 복구 | 느린 질의가 끝나 마이그레이션이 지나가고 회복 | 내보내기가 스스로 끝나 ALTER 가 즉시 끝나고 줄이 풀림. cleanup 은 그 뒤 더한 열을 즉시 지움 |

기전 확인: 원인(안전한 INSTANT 열 추가가 같은 표의 긴 읽기 뒤에서 메타데이터 잠금을 기다림)에서 증상(그 표를 쓰는 경로 전체가 막혀 서비스 요청 대부분 실패)까지 고리가 원본과 같고, 엔진(MySQL), 알고리즘(INSTANT), 잠금 종류(메타데이터 잠금), DB 쪽 시간 제한 없음까지 같다. 원본의 느린 질의는 제품 질의였고 이 재구성은 분석용 내보내기다(F66-R 과 같은 선택). 둘 다 "같은 표를 오래 읽는 정당한 읽기"라는 점이 고리의 요소다.

## 3. 숫자 근거 (2026-10-11, `scenario-stats.py`, 정식 + 후보 합계 88, 이 후보 전)

| 축 | 값 | 판단 |
|---|---|---|
| 서비스 | commerce 31, core-banking 30, food-delivery 27 | 음식배달이 가장 적어 우선 |
| 묶음 | A 7, B 7, C 6, D 10, E 4, F 3, G 11, H 2, I 1, J 14(15.9%), K 2, L 7, M 1, O 1, P 10, Q 2, N 0 | 주 묶음 D 10 → 11(89 중 12.4%). 20% 아래 |
| 정답 위치 | 최다 주문 서비스 8(9%). DB 테이블(배차) 2(F33-R, F32-P) | 2 → 3(3.4%) |
| 결제 경로 합계 | 12(13.6%) | 이 후보는 결제 경로가 아니라 12/89(13.5%) |
| 부품 지도 | food: mysql 9, dispatch 8, order 4, restaurant 4, payment 3, notify 1, kafka 0 | 증상 부품(dispatch)은 많지만 정답은 DB 표의 잠금 대기열 |

왜 이 후보인가: 서비스 최소(음식배달), 묶음과 정답 위치가 상한에서 멀다. 겉 증상(dispatch 풀 고갈과 재시작, 주문 503)이 F60-R, F33-R, F32-P, F59-R 과 같아 관제 AI 가 증상만 보고 찍을 수 없고, DB 세션 상태로만 갈리는 H 후보다(지시서: 증상이 같고 정답이 다른 후보는 오히려 가치가 크다). 2026-10-08 의 같은 기전 반려(rejected 22행)는 dispatches 판을 "정답 위치와 증상이 F33-R 과 같다"로 막았다. 2026-10-09 사용자 정정으로 증상 겹침은 버릴 사유가 아니다. 그 반려의 나머지 벽(입구 중단 조건, 대기 세션 누적)은 §9 에서 이 표에 대해 따로 풀었다.

### 3단계 후보 목록 (실제 기전 × 부품, 숫자 순으로 줄 세움)

| # | 후보 | 원본 | 부품, 정답 위치(현재 수) | 판단 |
|---|---|---|---|---|
| 1 | INSTANT 열 추가가 느린 내보내기 뒤에서 메타데이터 잠금을 기다려 배차 표 질의가 모두 줄을 섬 | Honeycomb 2024-08-06 공식 | food MySQL dispatches, DB 테이블(배차) 2 | **채택(F60-H)**. 음식배달 최소, D 12.4% |
| 2 | 같은 기전을 food 배차 이벤트 outbox(dispatch_outbox_events) 에 | Honeycomb 2024-08-06 공식 | food MySQL, DB 테이블(배차 outbox) 0 | 보류(줄 세움 2위): 'outbox + 추적 열 + 이벤트 내보내기' 가 F66-P 와 같은 꼴이라 카탈로그 §1 복제 위험이 1번보다 크다. 1번이 막히면 다음 |
| 3 | 같은 기전을 menus, restaurants 에 | Honeycomb 2024-08-06 공식 | restaurant, DB 테이블(메뉴) 0 | 버림(컨트롤러 필수 중단 조건, 원칙 7): loadgen 주문 여정이 메뉴 조회 200 을 전제로 해(`food-delivery/loadgen/script.js:118-125`) 주문이 아예 나가지 않아 판정 표본이 사라지고, restaurants 는 orders 의 외래 키 부모라 주문 INSERT 가 줄에 서 order 풀이 묶인다(로컬 실측: 부모 대기열 뒤에서 자식 INSERT 대기). rejected 22행과 같은 벽 |
| 4 | 한 서비스의 풀 최대·최소 유휴를 크게 올린 설정 배포가 DB 연결 상한을 차지 | Resend 2026-02-15 공식 | banking ledger, 은행 원장 서비스 1 | 버림(원칙 9): rejected 61행과 같은 주입(굶는 쪽이 정해지지 않고 증상이 F35-R 과 같음) |
| 5 | 배포 스크립트의 캐시 전체 비우기로 몰림, 또는 캐시 설정 변경으로 동시 재기록 | Shepherd 2026-02-26 공식 상태 페이지, GitHub 2026-02 월간 보고 | commerce Redis, 캐시 1 | 버림(원칙 2): 쇼핑몰이 최다(31)라 정답 위치 0 부품만 낼 수 있는데 캐시는 1 |
| 6 | 자격 증명 회전이 외부 결제 키를 잘못 바꿈 | Cloudflare R2 2025-03-21 공식(새 자격 증명을 다른 환경에 적용, 옛 것 삭제) | food payment → PG mock, 결제 서비스 4 | 버림(원칙 1, 4): PG mock 에 인증이 없어 인증 경로를 새로 깔아야 하고, 결제는 배차 뒤 단계라 실패마다 배차 용량이 샘(rejected 44행 벽) |
| 7 | 표 권한 회수(보안 정비)로 그 표 질의만 1142 | (공식 사후 보고 못 찾음, 웹 검색 2회) | food menus, DB 계정 2 | 버림(원칙 1) |
| 8 | 운영 DEBUG 로그 켜기로 서비스 CPU, I/O 포화 | RavenDB 블로그(공식 사후 보고 아님) | food restaurant, 가게 서비스 3 | 버림(원칙 1): 웹 검색 1회에 공식 사후 보고 없음 |
| 9 | 테이블스페이스 할당량 회수로 이체 INSERT ORA-01536 | (공식 사후 보고 없음) | banking Oracle TRANSFERS, DB 계정 2 | 버림(원칙 1) |
| 10 | 엄격한 요청 검증 릴리스가 선언 안 된 필드를 거절 | Buttondown 2026-06-14 공식 | food dispatch, 배달 서비스 5 | 버림(원칙 1): order 가 공유 DTO(orderId, region)만 보내 선언 밖 필드가 없어, 재현하려면 필드 제거라는 두 번째 변경이 필요 |
| 11 | 이체 참조 유일 제약(인덱스 역할) 제거 | Chargebee 2018-03-02 공식 | banking TRANSFERS.transfer_ref, DB 테이블(이체) 2 | 버림(원칙 9): GET /api/transfers/{ref} 를 부르는 곳이 없어 느려질 질의가 없음 |
| 12 | 원장 대사 배치 주기 오배포로 무거운 합계가 상시 돎 | Slack 2025-11-01 공식 | banking ledger, 은행 원장 서비스 1 | 버림(원칙 9): @Scheduled(fixedDelay) 단일 스레드라 한 줄기 부하뿐(rejected 의 Catapult 행과 같은 벽) |

서로 다른 부품: dispatches 표, dispatch outbox 표, menus/restaurants, ledger 풀, Redis, PG mock 경로, restaurant 로그, Oracle 테이블스페이스, dispatch 요청 검증, TRANSFERS 인덱스, ledger 배치(4개 이상).

## 4. 인과 사슬과 코드, 인프라 위치

1. 내보내기: 앱 계정(`fooddelivery@localhost`) 세션이 `SELECT id, order_id, courier_id, eta_minutes, status, assigned_at FROM dispatches` 를 `mysql --quick`(한 행씩 받기)로 실행하고 받는 쪽이 N 행마다 1초 쉰다. 서버 세션 상태 `Sending to client`, dispatches 의 SHARED_READ 메타데이터 잠금 GRANTED(로컬 performance_schema.metadata_locks 실측).
2. 마이그레이션: root 세션의 `ALTER TABLE dispatches ADD COLUMN cancel_reason varchar(64) NULL, ALGORITHM=INSTANT` 가 EXCLUSIVE PENDING, 상태 `Waiting for table metadata lock`.
3. 뒤의 질의: dispatches 의 SELECT COUNT, INSERT 가 모두 `Waiting for table metadata lock`(로컬 실측: lock_wait_timeout 3초로 둔 탐침이 1205 로 끝남, 같은 순간 dispatch_events SELECT 는 0.12초).
   - `food-delivery/dispatch-service/src/main/java/com/fooddelivery/dispatch/service/DispatchService.java:51-71`(dispatchCourier: countByStatus 60, save 71)
   - `DispatchService.java:105-107`(getCapacity), `:79-93`(getDispatch, searchDispatches), `:119-136`(deliverExpiredDispatches, 30초), `DispatchRetentionBatch.java:41-43`(purge, 6초)
4. dispatch 풀: `food-delivery/dispatch-service/src/main/resources/application.yml:12-15`(maximum-pool-size 10, connection-timeout 3000). 기다리는 질의가 연결을 쥐어 뒤의 요청은 `HikariPool-1 - Connection is not available, request timed out after 3000ms` 500.
5. order: `food-delivery/order-service/src/main/java/com/fooddelivery/order/service/OrderService.java:96-111`(주문 저장 전 용량 확인, 실패면 503), `order/client/DispatchClient.java:32-49`(서킷, 재시도, fallback 503 'Dispatch service unavailable'). order 자신의 표(orders, order_items, order_outbox_events)는 dispatches 와 외래 키가 없어(`food-delivery/db/init.sql:110-131` 주석과 정의) 줄에 서지 않는다.
6. dispatch 재시작: `food-delivery/k8s/22-dispatch-deploy.yaml:67-86`(모든 프로브 /actuator/health, readiness 10초 × 3, liveness 15초 × 5). 죽은 파드의 서버 쪽 대기 세션은 잠금이 풀릴 때까지 남는다(rejected 22행의 로컬 실측과 같음).
7. 109 실측(2026-10-11, 읽기 전용): dispatches 행 추정 1,737,338(222MB), innodb_tables total_row_versions 0, lock_wait_timeout 31536000, max_execution_time 0, net_write_timeout 60, 메타데이터 잠금 계측 YES, 열린 세션 21, mysql 8.0.46 aarch64(이미지 mysql:8.0), 파드 안 awk, mktemp, nohup 있음, 앱 계정이 파드 안에서 `fooddelivery@%` 로 붙음.

## 5. 원인 규정

- 근본(`root_cause.target_id`): `food-mysql:fooddelivery.dispatches` — 이 표에 대한 스키마 변경이 같은 표의 긴 읽기와 겹쳐 생긴 메타데이터 잠금 대기열. 원본 Honeycomb 의 사후 보고도 "teams 표의 마이그레이션이 느린 SELECT 뒤에서 잠금을 기다리며 그 표의 질의를 막음" 을 원인으로 적는다(같은 층위).
- 계기(`trigger_target_id`): 비움. 근본과 계기가 같은 곳(이 표의 스키마 변경과 긴 읽기)이다. F66-R, F60-R 과 같은 규정.
- 부분 점수(`scoring.partial`): `food-delivery-dispatch`, `food-dispatch`(증상 서비스), `food-mysql`(인스턴스까지만 지목).
- 원칙 5: "DB 안에서 한 표에 DDL 이 긴 읽기 뒤에 서서 그 표의 질의가 모두 기다림" 은 DPM 세션 상태만으로 내릴 수 있는 결론이다. 무슨 열을 더했는지, 내보내기가 무엇이었는지는 요구하지 않는다. 코드 결함 추론이 필요 없다.
- 원칙 6: dispatch, order 의 코드, 설정, 요청량은 그대로라 정당하다. 내보내기 자체도 정당한 읽기이고, 잘못은 그 표를 쓰는 긴 질의가 도는 중에 배타 잠금이 필요한 DDL 을 시간 제한 없이 낸 것이다(원본 재발 방지: DB 쪽 시간 제한).

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 |
|---|---|
| 감지(lucida) | order ERROR 로그 급증('Failed to check dispatch capacity', 처음엔 Read timed out, 그 뒤엔 500), order POST /api/orders 503, dispatch 'Connection is not available' 급증, KCM testbed-dispatch Unhealthy, Killing, DPM active_session 상승 |
| 피해 판정(러너) | 기준선 loadgen-food 의 주문 생성 5xx 비율 ≥ 0.5, 2xx 비율 < 0.2 (3틱, 판정 관측 갱신 30초) |
| RCA 증거 | DPM 세션 스냅숏의 fooddelivery@localhost 'Sending to client'(queryTime 이 분마다 늘어 약 360초), root@localhost 와 dispatch 파드 IP 의 'Waiting for table metadata lock', 롤아웃 없음, 'copy to tmp table' 없음, 표·인덱스 수 불변, 1146 없음 |

## 7. 관측 근거 (119 실조회, 2026-10-11)

| 증거 | 표, 칸 | 조회와 결과(평시) | 표본 여부 |
|---|---|---|---|
| 계기: 긴 내보내기 세션 | CH `lucida.dpm_session_local`(body JSON `user`, `hostname`, `state`, `queryTime`, `dbName`) | `SELECT JSONExtractString(body,'state') st, count() c, max(JSONExtractFloat(body,'queryTime')) FROM lucida.dpm_session_local WHERE engine='mysql' AND timestamp > now() - INTERVAL 60 DAY GROUP BY 1` → 상태 23종 가운데 'Sending to client' **0건**. 스냅숏은 1분마다(`count(DISTINCT timestamp)` 1시간 59). 수집기는 Sleep 이 아닌 세션만 남긴다(1시간 179행, 스냅숏당 약 3행: 데몬 2 + 감시 계정 1). 내보내기는 Query 명령으로 활성이라 분마다 남는다 | 전수(DPM 1분 폴링) |
| 계기, 근본: 메타데이터 잠금 대기 | 위 표 state | 'Waiting for table metadata lock' 60일 8건, 모두 2026-10-09 03:43 한 스냅숏(fooddelivery@10.244.2.82 7, root@localhost 1). localhost 세션도 기록된다(60일 1건). 장애 시 root@localhost(ALTER)와 dispatch 파드 IP(user fooddelivery)의 세션이 분마다 쌓인다 | 전수 |
| 전파: 세션 수 | VM `dpm.mysql.session.active_session`, `current_session`{target_id="c8c558e5-…"} | 7일 active 중앙 1, 최대 12. current 중앙 57, 최대 75, `max_session` 151. 장애 시 active 가 대기 세션 수만큼 오른다 | 전수 |
| 감별: 복사·스캔 없음 | VM `dpm.mysql.instance.data_file_write_bytes`, Top SQL | F60-R 시트 실측: 7일 중앙 0.41MB/s, 최대 2.55MB/s. 이 후보는 표를 다시 쓰지 않아 오르지 않는다(F60-R 은 오른다). 내보내기 읽기는 222MB 를 6분에 나눠 약 0.6MB/s | 전수 |
| 감별: 표, 인덱스 수 불변 | VM `dpm.mysql.database.table_count`, `index_count` | 표 16, 인덱스 31(F32-P, F33-R 시트 7일 고정 확인). INSTANT 열 추가는 둘 다 바꾸지 않는다 | 전수 |
| 전파: order 용량 확인 실패 | CH `lucida.lucida_logs_local` `service_name='food-delivery-order' AND body LIKE 'Failed to check dispatch capacity%'` | 7일 593건(다른 시나리오 실행 창). 장애 시 오른다 | 전수 |
| 전파: dispatch 풀 고갈 | 위 표 `service_name='food-delivery-dispatch' AND body LIKE '%Connection is not available%'` | 7일 771건(다른 시나리오 실행 창) | 전수 |
| 전파: dispatch 재시작 | CH `lucida.kcm_events_local` `namespace='rca-testbed-food' AND object_name LIKE 'testbed-dispatch%'` | 7일 Unhealthy 138, Killing 14, ScalingReplicaSet 28(모두 시나리오 실행 창). 장애 시 Unhealthy, Killing 이 ScalingReplicaSet 없이 나온다 | 전수 |
| 피해: 주문, 배차 로그 | 위 표 'Created order id=%'(order), 'Dispatched courier=%'(dispatch) | 최근 1시간 각 4,129건(약 1.15건/초). 장애 시 멈춘다 | 전수 |
| 보조: 주문 503, 배차 스팬 | CH `lucida.otel_traces_local` | 장애 시 order POST /api/orders 503, dispatch GET /api/deliveries/capacity 긴 지속 시간 | **10% 표본, 보조로만** |

핵심 증거(내보내기와 DDL 세션 상태, 메타데이터 잠금 대기 세션, 세션 수, order 와 dispatch 로그, KCM 이벤트)는 모두 전수 수집이다. 트레이스와 APM 지표는 보조로만 쓴다. 일회성 DDL 문장은 DPM Top SQL 에 남지 않는다(F60-R, F23-P 실행기 설명과 같음). 장애 시 형태는 첫 실행에서 다시 확인한다.

## 8. 감별

- must_support: 'Sending to client' 의 긴 앱 계정 세션과 메타데이터 잠금 대기 세션, active_session 상승과 데이터 파일 쓰기·표·인덱스 수 불변, order 'Failed to check dispatch capacity' 뒤 503, dispatch 풀 고갈과 KCM Unhealthy, Killing(롤아웃 없음), 둘러보기 정상, 다른 표 쓰기 정상, 내보내기가 사라지는 분에 회복.
- must_rule_out: F60-R(copy to tmp table, 쓰기만 막힘, 데이터 파일 쓰기 급증), F33-R/F59-R(긴 스캔, MySQL 포화), F32-P(1146, 표 수 감소), dispatch 릴리스나 설정 배포(롤아웃), MySQL 다운·읽기 전용(다른 표 쓰기 성공).
- contrast_with: F60-R, F66-R, F33-R, F32-P, F59-R(정답지 related_scenarios 에 차이를 적음).
- 관계: F60-R 과 겉 증상(dispatch 풀 고갈과 재시작, 모든 새 주문 503 'Dispatch service unavailable')이 같고 원인이 다르다(H). 가르는 근거: 사슬 맨 앞 세션의 상태('Sending to client' 앱 계정 읽기 대 'copy to tmp table' root DDL), 읽기도 막힘(배달 목록 실패) 대 쓰기만 막힘, 데이터 파일 쓰기 불변 대 급증. 정답 표도 다르다(dispatches 대 dispatch_events, F60-R 은 dispatches 도 받음).

## 9. 러너 판정과 강도, 부하 계산

- 강도 축이 없다: 잠금 대기열 길이는 내보내기 길이(export_seconds 360)로 정해진다. 설계 강도 하나로 고정한다.
- 내보내기 속도: run 이 행 추정(1,737,338)을 360 으로 나눠 약 4,826 행마다 1초 쉰다. 행 추정 ≥ 100 × 360 이 아니면 시작하지 않는다. 서버는 받는 쪽이 쉬는 1초 동안 쓰기가 막혀 기다릴 뿐 net_write_timeout(60초)에 걸리지 않는다(로컬 실측: 2만 행을 60초로 나눈 내보내기가 약 57초 활성, 끊기지 않음).
- 시간표(기준선 주문 r ≈ 0.2~1.15건/초): 0초 ALTER 대기 시작 → dispatch 의 용량 COUNT, 배차 INSERT, 배달 읽기, 보존 정리(6초), 만료 배치(30초)가 줄에 섬 → 주문 하나가 용량 확인 시간 초과 3번(5초 + 0.2초 + 5초 + 0.4초 + 5초 ≈ 16초)마다 dispatch 연결을 셋 남기고, 배달 조회(0.1~0.6건/초)와 배치가 더해 약 5~20초에 풀 10개가 빈다. 그 뒤 용량 확인은 연결 대기 3초 끝 500, 서킷 열림으로 곧바로 503. dispatch NotReady(약 30~40초) 뒤엔 연결 거부로 곧바로 503. 판정 관측 둘은 30초마다 갱신되므로 min_hold 90s + 3틱이면 판정은 약 3분에 끝나 잠금 대기열(약 6분) 안에 든다. timeout 6m.
- order 풀(15) 압박: 처음 십수 초 동안 주문 트랜잭션이 용량 확인 응답을 기다리며 연결을 쥔다(r × 16초 ≈ 3~18). 서킷이 열리면 곧 풀린다. F60-R 과 같은 동학이며 order readiness 는 10초 × 3번 연속 실패가 필요해 빠지지 않는다고 본다. 그래서 동반 부하를 두지 않는다. must_rule_out 의 order NotReady 가 이를 지킨다.
- 입구 중단 조건(abort entry_status==0): 주문 여정은 restaurant 메뉴 조회 뒤 order 로 가고 dispatch 를 직접 거치지 않으며, order 표는 dispatches 와 외래 키가 없어 줄에 서지 않는다(로컬 실측: orders INSERT 는 즉시). entry_status 는 order 파드가 빠질 때만 0 이다.
- MySQL 세션 상한: dispatch 파드 한 생애에 서버 쪽 대기 세션이 최대 약 10~13개 남는다. liveness 재시작(약 75초) + 기동(약 40~60초)이라 6분에 두세 번 재시작, 약 30~40개. preflight 가 열린 세션 90 이하(지금 21)를 요구하므로 최악 약 130 으로 max_connections 151 아래.
- InnoDB INSTANT 행 버전: 표당 64, 실행 한 번(열 추가 + cleanup 열 지우기)에 2. preflight 가 60 이하를 요구한다(지금 0). 30번 넘게 돌리면 preflight 가 막고 사람이 표를 다시 만들어야 한다(실행기 문서에 적음).
- cleanup: 내보내기가 스스로 끝나기를 최대 300초 기다리고(원본처럼 끝나게 둠, 녹화에 대기열 전체가 담김), 넘으면 KILL QUERY 로 내보내기와 남은 마이그레이션을 끝낸다. 두 세션이 없어진 것을 본 뒤 열을 INSTANT 로 지운다(lock_wait_timeout 5초, 5번까지). cleanup 은 성공, abort, must_rule_out 어느 경로든 같은 동작이라, 안전 중단 뒤에도 내보내기가 남아 있으면 장애가 최대 300초 더 이어진다(F60-R, F66-R 과 같은 결정: 데이터를 바꾸지 않고 스스로 끝난다).
- run 은 러너 coordinator lock 을 쥔 채 돈다: 두 문장을 DB 파드 안에서 백그라운드(nohup, 표준 입출력 리디렉션)로 띄우고 각각 자리를 잡은 것을 보면 돌아온다(로컬 2.6초).

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Honeycomb, 2024-08-06, 공식 상태 페이지 사후 보고)와 요소별 대응표, 기전 동일(엔진, INSTANT, 메타데이터 잠금까지 같음)(§2).
- [x] 분류 장부를 갱신했고 묶음 D(10→11, 12.4%), 정답 위치 DB 테이블(배차)(2→3), 결제 경로 12/89(13.5%), 서비스(음식배달 27→28), 이 후보를 고른 이유를 적었다. 어느 축도 20% 미만(§3).
- [x] 근본 원인 위치: food MySQL fooddelivery.dispatches(`food-delivery/db/init.sql:111-131`), 막히는 질의 `DispatchService.java:51-136`, 109 실측 값(§4).
- [x] 근본 원인 흔적이 119 에 조회됐다: dpm_session_local 의 state(평시 'Sending to client' 0건, 메타데이터 잠금 대기 한 스냅숏), localhost 세션 기록 가능(§7).
- [x] 핵심 증거가 표본 데이터에만 있지 않다: DPM 세션, 로그, KCM 이 전수다(§7).
- [x] 계기 흔적: 내보내기 세션과 DDL 세션이 분마다 각 상태로 남는다. 인공 지연(SLEEP)이 아니라 받는 쪽이 천천히 받는 실제 읽기와 현실의 스키마 변경 문장이다(원칙 4).
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다: "배차 표에 대한 DDL 이 긴 읽기 뒤에 서서 그 표의 질의가 모두 기다림"(§5).
- [x] 정답지 세 칸이 원칙 6대로다(§5).
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다(§6).
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: 문장, 열 이름, 파드 안 파일(/tmp/table-export.sh, /tmp/table-export.log, /tmp/schema-migration.log, cleanup 이 지움)에 시나리오 id 가 없고 테스트로 고정했다. 내보내기는 앱 계정, 마이그레이션은 다른 db.ddl MySQL 모드와 같은 root@localhost 다.
- [x] 부하 상한과 서킷브레이커를 고려한 피해 계산(§9). 강도는 한 단, 동반 부하 없음.

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- dpm_session_local 에 'Sending to client'(fooddelivery@localhost)와 'Waiting for table metadata lock' 이 실제로 분마다 담기는지.
- order 가 Ready 를 지키는지(must_rule_out), MySQL 현재 세션 최대값(151 아래).
- 내보내기가 약 360초 이어지는지(행 추정과 실제 행 수 차이).
- cleanup 뒤 cancel_reason 열 없음, total_row_versions 2 증가, /tmp 파일 없음, dispatch 회복.
