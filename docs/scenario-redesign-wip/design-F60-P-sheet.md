---
title: F60-P 설계 시트 (취소된 인덱스 백필이 food 배차 이벤트 outbox 표를 치워 배차가 모두 커밋에서 되돌려지고 새 주문이 전량 503)
status: Draft
owner: project
last_reviewed: 2026-10-11
tags:
  - scenario
  - design
  - schema
  - mysql
  - operation
  - outbox
summary: 운영자가 food MySQL 의 배차 이벤트 outbox 표(dispatch_outbox_events)에 aggregate_id 인덱스를 더하는 온라인 백필을 시작했다가 취소했는데, 취소 정리가 그림자 표 대신 살아 있는 표를 보류 이름으로 치워 dispatch-service 의 outbox 기록이 MySQL 1146 으로 실패한다. dispatch 는 그 실패를 'non-critical' 로 잡지만 실패한 저장소 호출이 배차 트랜잭션을 rollback-only 로 표시해 모든 배차가 커밋에서 되돌려지고(500), order 가 모든 새 주문을 503 으로 거절한다. 용량 확인, 배달 조회, 둘러보기, 결제는 정상이다. 원본은 GitHub 2026-07-24 Vitess 백필 워크플로 취소가 기반 표를 지워 PR '생성'만 실패한 장애.
---

# F60-P 설계 시트

## 1. 요약

food 의 dispatch-service 는 배차(`dispatchCourier`)와 배달 완료(30초마다 도는 만료 배치)마다 상태 이력(`dispatch_events`)과 배차 이벤트(`dispatch_outbox_events`)를 배차와 같은 트랜잭션으로 쓴다(트랜잭션 outbox). `DispatchOutboxRelay` 가 2초마다 이 outbox 의 미발행 행을 Kafka(food.dispatch)로 보내고, 24시간이 지난 발행분은 지운다(약 11만 6천 행, 25MB). 운영자가 배차 id 로 이벤트 이력을 찾는 일이 잦아 `aggregate_id` 인덱스를 더하는 온라인 인덱스 백필을 시작한다. 도구는 새 인덱스를 가진 그림자 표(`_vt_vrp_<uuid>_<시각>_`)를 만들고 행을 옮긴다. 그런데 백필이 취소되고, 취소 정리가 그림자 표가 아니라 살아 있는 `dispatch_outbox_events` 를 테이블 수명주기의 보류 이름(`_vt_hld_<uuid>_<보류 기한>_`, Vitess 가 표를 지우는 방식)으로 치운다.

그 순간부터 dispatch 의 outbox INSERT 는 MySQL 1146 으로 실패한다. 코드는 그 실패를 잡아 WARN 'Failed to record dispatch outbox event (non-critical)' 를 남기고 'Dispatched courier=...' 까지 찍지만, 실패한 저장소 호출(Spring Data `save`, 진행 중인 트랜잭션에 참여)이 이미 배차 트랜잭션을 rollback-only 로 표시해 두었다. 커밋이 배차를 되돌리고 `UnexpectedRollbackException` 으로 500 이 나간다. order 는 그것을 503 'Dispatch service failed' 로 바꾸고 주문을 되돌린다. order 의 dispatch 서킷브레이커가 열리면 주문은 용량 확인에서부터 503 이다. 손님은 아무도 주문하지 못하고, 가게 둘러보기, 배달 추적, 주문 조회는 그대로다.

비유: 배차 담당이 기사를 정하고 배차 장부에 적은 뒤, 마지막으로 "배차 알림판"에 한 줄 붙여야 그날 배차가 확정되는 사무실이다. 알림판에 칸막이를 새로 달려고 옆에 임시판을 세우다 작업을 취소했는데, 정리하던 사람이 임시판 대신 원래 알림판을 창고로 치웠다. 담당은 "알림판은 부가 업무니 넘어가자"며 기사에게 출발하라고 말까지 하지만, 사무실 규칙상 한 단계라도 실패한 배차는 마감 때 통째로 취소된다. 주문 창구는 "배차가 안 된다"는 말만 듣고 손님을 돌려보낸다. 배차 가능 인원 문의와 배달 조회 창구는 멀쩡하다.

## 2. 원본 사례

- 기업: GitHub (풀 리퀘스트 생성)
- 날짜: 2026-07-24 19:17~20:02 UTC(57분)
- 링크: [GitHub Availability Report: July 2026 (공식)](https://github.blog/news-insights/company-news/github-availability-report-july-2026/) (자료 문서 `ref-real-world-incidents.md` M21 에 이미 있음)
- 요약(출처가 말한 것만): 사용자가 웹, CLI, API 로 풀 리퀘스트를 만들지 못했다. 시도 113,930회, 사용자 50,904명, 평균 오류율 1.75%, 최대 2.25%. 기존 풀 리퀘스트와 다른 기능은 영향이 없었고 PR 을 만드는 워크플로도 실패했다. 근본 원인은 풀 리퀘스트 데이터를 담은 Vitess keyspace 로의 백필 워크플로였고, "The cancellation executed a misunderstood Vitess codepath that dropped the backing table to the target keyspace" 라 낡은 vschema 참조가 남았다. 데이터베이스 변경을 되돌리자 PR 생성이 바로 재개됐고, 낡은 참조를 지워 마무리했다. 재발 방지로 인덱스 백필 운영 지침을 더하고 예상 밖 취소 동작을 문서화했다. 탐지 경로는 적혀 있지 않다.
- Vitess 쪽 사실(제품 문서, 사고 보고 아님): 표를 지울 때 수명주기(hold → purge → evac → drop)를 밟고 상태를 표 이름에 담는다([Vitess table lifecycle](https://vitess.io/docs/user-guides/schema-changes/table-lifecycle/)). F48-R, F48-P 와 같이 "지움" 을 보류 이름으로 바꾸기로 재구성한다: 원본의 복구(변경 되돌림 → 즉시 재개)와 같은 꼴이고 행을 잃지 않는다.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | Vitess keyspace 로의 백필 워크플로를 취소 | 운영자가 dispatch_outbox_events 에 aggregate_id 인덱스를 더하는 온라인 인덱스 백필(그림자 표 + 새 인덱스 + 행 복사)을 시작했다가 취소 |
| 원인이 된 결함 | 취소가 잘못 이해된 코드 경로를 타 대상 keyspace 의 기반 표를 지움 | 취소 정리가 그림자 표 대신 살아 있는 표를 보류 이름(_vt_hld_)으로 치움(Vitess 의 표 지우기 방식, 행 보존) |
| 전파 경로 | 표가 사라졌는데 vschema 와 앱은 여전히 그 표를 참조 → 그 표에 쓰는 PR 생성 실패 | dispatch 가 배차 트랜잭션 안에서 그 표에 쓰는 outbox INSERT 가 1146 → 'non-critical' 로 잡혔지만 트랜잭션이 rollback-only → 커밋에서 배차 되돌림(500) → order 503, 주문 되돌림 |
| 사용자 증상 | PR 을 만들지 못함(만들기만 실패), 기존 PR 과 다른 기능은 정상 | 새 주문을 하지 못함(503), 가게 둘러보기, 배달 추적, 주문 조회는 정상 |
| 탐지된 경로 | 적혀 있지 않음 | order, dispatch 오류율과 새 오류 로그(1146, rollback-only, 스케줄러 ERROR) 급증(119 이상 탐지 → 인시던트) |
| 완화와 복구 | 데이터베이스 변경을 되돌리자 즉시 재개, 낡은 참조 정리 | cleanup 이 보류 표를 원래 이름으로 되돌림(즉시 재개, 행 그대로), 남은 그림자 표를 지움 |

기전은 원본과 같다: "백필 취소가 살아 있는 표를 지움 → 앱이 여전히 그 표를 참조 → 그 표에 쓰는 '만들기' 기능만 실패, 나머지는 정상 → 변경을 되돌리면 즉시 회복". 원본에서 실패한 것은 PR **생성**과 그 워크플로였다. 여기서도 실패하는 것은 배차 **생성**(과 같은 표에 쓰는 배달 완료 처리)이고 읽기는 정상이다. 우리 스택에 맞춘 것은 대상(MySQL 8.0 의 outbox 표 하나)과 규모다. 테스트베드 고유의 차이 하나: dispatch 가 outbox 기록 실패를 'non-critical' 로 잡지만 Spring 트랜잭션 전파 탓에 결국 배차 전체가 되돌려진다. 이것은 코드 구조가 키운 피해라 user_impact 와 감별 근거에 적고 정답에 넣지 않는다(원칙 5).

## 3. 숫자 근거 (2026-10-11, `scenario-stats.py`, 정식 + 후보 합계 97, 이 후보 전)

| 축 | 값 | 판단 |
|---|---|---|
| 서비스 | 쇼핑몰 33, 은행 32, 음식배달 32 | 음식배달과 은행이 가장 적다 → 음식배달(32→33) |
| 묶음 | J 16(16.5%), D 14, G 12, P 11, L 8, A 7, B 7, C 6, E 4, F 3, H 2, K 2, Q 2, I 1, M 1, O 1, N 0 | P(운영 작업의 대상 착오) 11→12(12.2%). F48-R, F48-P 처럼 결함이 앱이 기대하는 표가 없는 꼴로 드러나 P+L. L 로 세도 8→9(9.2%) |
| 정답 위치 | DB 테이블(배차 이벤트 outbox) 0 | §2-1 의 'DB 테이블(<무엇>)' 꼴, 0→1(1.0%). DB 테이블(배차)(4, dispatches), DB 테이블(배차 이력)(1, dispatch_events), 배달 서비스(6)와 다른 대상이다 |
| 결제 경로 합계 | 13(13.4%) | 결제가 정답이 아니므로 그대로(13/98, 13.3%) |
| 어느 축이든 | 최대는 J 16/98(16.3%), 주문 서비스 8/98(8.2%) | 20% 미만 |

묶음 해석: 바뀐 것은 표 하나의 존재다. 설정값이 아니라 G 가 아니고, 코드나 이미지 배포가 없어 J 가 아니며, 계획된 스키마 변경이 코드보다 먼저 적용된 F36-R(L)과도 다르다: 백필이 의도한 변경(인덱스 추가)은 맞았고 그것을 적용하던 도구의 정리 단계가 엉뚱한 대상(살아 있는 표)을 치웠다. DB 프로세스, 자원, 잠금, 질의 비용은 그대로라 A, B, D, F 가 아니다.

관계와 id: 사례군 F60 의 P(전파의 일부만 같고 최초 원인이 다름). F60-R 은 외래 키 추가의 표 복사 잠금으로 dispatch 배차 트랜잭션의 상태 기록 단계(dispatch_events)가 몇 분씩 기다려 dispatch 풀이 차는 고장이다. F60-P 도 같은 배차 트랜잭션의 기록 단계(recordEvent)에서 고장이 나지만, 원인이 표가 사라진 것이고 기다림 없이 즉시 실패해 배차가 되돌려진다. 원인 기전(취소된 백필이 살아 있는 표를 치움)은 F48-R, F48-P 와 같고 수단도 같다(db.ddl MySQL 보류 표 모드). 표와 인덱스가 달라 같은 주입 중복(같은 수단과 같은 파라미터)이 아니다(앞머리: "같은 수단의 다른 표는 다른 파라미터"). F48-P 와 비교하면 카탈로그 §1 의 R 조건(DB 엔진, 진입 경로, 영향 범위, 노이즈 중 하나 이상이 다름)을 만족한다: 실패하는 서비스가 하류 dispatch 이고(order 자신 대 dispatch → order), 상태 코드가 다르며(order 500 대 dispatch 500 → order 503), 영향 범위가 다르다(F48-P 는 배차와 결제가 남고, F60-P 는 배차가 되돌려지고 결제 전에 끝나며 배달 완료 처리도 멈춘다). F53-P(F53-R 과 같은 수단, 다른 표가 한 홉 떨어진 서비스에서 실패)가 같은 꼴의 선례다.

겉 증상(order 503)은 F32-R(정식), F32-P, F60-H, F33-R(정식) 와 같고 정답이 다르다(카탈로그 관계 H). 관제 AI 가 "food 주문 503 이면 배차 표나 배차 한도" 를 외워 찍지 못하게 하는 짝이다. 가르는 관측 근거는 §8 에 둔다.

### 후보 목록 (실제 기전 × 부품, 숫자 순으로 줄 세움, 버린 이유 포함)

부품 지도의 0인 부품(commerce kafka, notification, nginx / banking kafka, nginx / food kafka)은 이번 실행에서 원칙 7(비동기 outbox 경로라 사용자 증상 없음), 원칙 3(nginx OTel 없음)으로 거듭 막혔다(rejected-candidates.md 6, 16, 27, 59, 64, 89, 97, 98, 146, 238, 258행 등). 출발은 `ref-real-world-incidents.md` 의 기전(M2, M15, M19, M21, M22)과 웹에서 새로 찾은 사례, 그리고 테스트베드의 실제 쓰기 경로다. 서로 다른 부품: food 배차 outbox 표, food 메뉴 표, commerce 장바구니 품목 표, banking transfer, food MySQL 연결 풀, commerce Redis, 노드 방화벽, PostgreSQL 인덱스.

| # | 후보 | 원본 사례 | 부품, 층 | 결과 |
|---|---|---|---|---|
| 1 | food 배차 이벤트 outbox 표를 취소된 인덱스 백필이 치움 | GitHub 2026-07-24 공식 | food MySQL 표(배차 쪽), 앱 층 | **채택**: 음식배달 32(최소 동률), 정답 위치 0, 묶음 P 11. 로컬 실측과 119 조회로 관문 통과(§4, §7) |
| 2 | food menus 표를 같은 백필(또는 로컬 마이그레이션)이 치움 | GitHub 2026-07-24 / Resend 2024-02-21 공식 | food MySQL 표(메뉴) | 버림(원칙 7, rejected 206행과 같은 벽): loadgen 주문 여정이 메뉴 조회 200 뒤에만 주문을 보내 주문 표본이 사라지고 restaurant 메뉴 500 만 남음 |
| 3 | commerce cart_items 를 운영을 가리킨 로컬 마이그레이션이 치움 | Resend 2024-02-21 공식 | commerce PostgreSQL 표(장바구니) | 남김(순위 아래): 쇼핑몰 33(최다)이라 정답 위치 0 이어도 1번보다 뒤. cart_items 는 평시 0행(109 실측)이고 checkout 의 장바구니 조회가 Redis 캐시 우선이라 피해가 대부분 400 'Cart is empty'(4xx)로 가려져 원칙 7 판단이 흐림. GitHub 판(백필 취소)은 PG 에 같은 도구 경로가 없어 원칙 1(rejected 반복 47) |
| 4 | food dispatch_outbox_events 열 추가가 느린 내보내기 뒤 메타데이터 잠금 대기 | Honeycomb 2024-08-06 공식 | food MySQL 표(배차 outbox) | 버림(원칙 1, 9): 11만 6천 행(25MB)이라 6분 내보내기가 억지(rejected 299, 301행 벽, payment_outbox 5.8만 행과 같은 이유) |
| 5 | banking transfer 릴리스가 요청 원격 주소별 한도를 더해 account 파드 하나에서 오는 모든 은행 이체를 429 | PostHog 2025-10-24 공식 | banking transfer(은행 이체 서비스 7) | 남김(순위 아래): 정답 위치 7 로 0 이 아님. account 가 transfer 4xx 를 그대로 넘겨 은행 이체 증상이 429 단일 신호(원칙 7, F34-R 교훈) |
| 6 | banking transfer 릴리스의 DB 연결 누수 | Octopus Deploy 2025-11-25 공식 | banking transfer | 남김(순위 아래): 정답 위치 7, 같은 원본이 이미 셋(F43-R, F43-P, F51-H) |
| 7 | 배포가 들여온 중첩 연결 획득으로 풀 교착(hold-and-wait) | Foxglove 2023-10-16(공식 Notion 사후 보고) | food dispatch 또는 banking account 연결 풀 | 버림(원칙 1, 9): 사후 보고의 근본 원인 절이 잘려 결함 꼴을 확인하지 못했고(WebFetch 본문 없음), 180rps·ms 지연에서 동시성이 풀 크기 미만(rejected 261행 WorkOS 와 같은 벽) |
| 8 | 배포 버그로 모든 캐시 항목이 만료·재계산되어 DB 과부하 | GitHub 2026-03 월간 가용성 보고 공식 | commerce Redis(캐시 1) | 버림(원칙 2): banking, food 에 캐시가 없고(grep), commerce 는 최다 서비스인데 캐시 정답 위치가 0 이 아님 |
| 9 | DEBUG 로그 설정이 남아 로그가 임시 저장소를 채움 | GitLab Runner 2024-04(이슈), Knak 상태 페이지 | 노드, 디스크 | 버림(원칙 1, 9): Knak 은 로그 전송 실패 뒤 로컬 기록이라 고리가 다르고, 앱은 stdout 로그를 kubelet 이 10Mi 로 돌려 디스크가 차지 않으며 119 로그 수집만 범람 |
| 10 | tb-w3 MySQL 포트 허용 목록이 한 구성 요소를 빠뜨려 연결 수명 뒤 재접속 실패 | Harness 2024-09-01 공식 | 노드 방화벽 | 버림(같은 원본·같은 수단): F44-R 이 같은 원본으로 tb-w3 host.firewall 을 이미 씀, 정답 위치 노드 6 |
| 11 | 인덱스 동시 생성 뒤 삼중 문자 인덱스 잠금 경합 | GitLab 2018-03-12 공식 production#300 | commerce PostgreSQL 인덱스 | 버림(원칙 1): 원인이 PostgreSQL 점 릴리스 버그이고 사후 보고도 기전이 불분명하다고 적음 |

1번을 먼저 고른 이유: 음식배달이 가장 적은 동률(32)이고, 정답 위치가 0 이며, 원본의 피해 모양('만들기' 하나만 실패, 나머지 정상, 되돌리면 즉시 회복)과 같다. 0인 묶음(N)과 0인 부품(kafka, nginx, notify)에서 낸 후보는 모두 앞선 반복의 벽에 걸렸고 이번에 새로 찾은 사례(7, 8, 9, 11)도 원칙 1, 2, 9 를 넘지 못했다.

## 4. 인과 사슬 (코드와 인프라 위치)

```
db.ddl(mysql, 보류 표 모드): testbed-mysql-0 안 MySQL 클라이언트, lock_wait_timeout 10초
  CREATE TABLE _vt_vrp_<uuid>_<시각>_ LIKE dispatch_outbox_events;
  ALTER TABLE _vt_vrp_... ADD INDEX idx_dispatch_outbox_aggregate (aggregate_id);
  INSERT INTO _vt_vrp_... SELECT * FROM dispatch_outbox_events      (약 11만 6천 행 25MB, 2~3초)
  RENAME TABLE dispatch_outbox_events TO _vt_hld_<uuid>_<보류 기한>_  (취소 정리의 착오)
  → DPM: fooddelivery table_count 16→17, index_count 31→34 (그 분, 그림자 표의 PRIMARY, idx_dispatch_outbox_unpublished, 새 인덱스)
  → order POST /api/orders: OrderService.createOrder (@Transactional)
       restaurantClient.getRestaurant, getMenu → 200
       dispatchClient.checkCapacity → dispatch GET /api/deliveries/capacity → COUNT(ASSIGNED) → 200
       orderRepository.save, orderItemRepository.save, log.info "Created order id=..."
       dispatchClient.dispatchCourier → dispatch POST /api/deliveries/dispatch
         DispatchService.dispatchCourier (@Transactional)
           countByStatus("ASSIGNED") 용량 확인, dispatchRepository.save (배차 INSERT)
           recordEvent: dispatchEventLogRepository.save (dispatch_events INSERT)
             outboxPublisher.publish → outboxEventRepository.save (IDENTITY, 즉시 INSERT, 진행 중 트랜잭션에 참여)
               'insert into dispatch_outbox_events (...)' → MySQL 1146
               → Hibernate SqlExceptionHelper WARN 'SQL Error: 1146, SQLState: 42S02', ERROR "Table ... doesn't exist"
               → 저장소 프록시의 트랜잭션 인터셉터가 참여 중인 트랜잭션을 rollback-only 로 표시
               → ServiceException(500, "Failed to record outbox event: ...")
             catch → WARN 'Failed to record dispatch outbox event (non-critical): ...'
           log.info "Dispatched courier=... for order=..."
         커밋 → UnexpectedRollbackException 'Transaction silently rolled back because it has been marked as rollback-only'
         → GlobalExceptionHandler 가 ServiceException 만 다뤄 서블릿 기본 처리: ERROR 'Servlet.service() ... threw exception', 500
       → DispatchClient: ServiceException 503 'Dispatch service failed: 500 ...'(재시도 3회, 서킷 집계)
       → log.info "Order fan-out dispatch failed: ...", 주문 트랜잭션 되돌림, 503
     (dispatch 서킷이 열린 동안: checkCapacity fallback 503 'Dispatch service unavailable: CircuitBreaker ...' → 주문 저장 전 503)
  → DispatchOutboxRelay.poll(2초마다) → OutboxRelay.relay (@Transactional) findTop100... → 1146
       → 스케줄러 ERROR 'Unexpected error occurred in scheduled task'(exception.message 에 그 SELECT 와 표 이름)
  → DispatchOutboxRelay.purge(30초마다) → WARN 'Outbox purge failed, will retry next cycle: ...'
  → DispatchService.deliverExpiredDispatches(30초마다, @Transactional) → 만료 배차마다 recordEvent → 같은 1146 과 'non-critical' WARN
       → 'Dispatch expiry batch finished: delivered=N' 뒤 커밋 → UnexpectedRollbackException → 스케줄러 ERROR, 배정 중 수 그대로
  ↔ dispatch GET /api/deliveries/capacity, /api/deliveries, /api/deliveries/{id}: dispatches, dispatch_events 만 읽음 → 200
  ↔ restaurant, order 조회, payment: 이 표를 쓰지 않음 → 정상(주문이 결제 전에 되돌려져 payment 는 호출되지 않음)
  ↔ dispatch, order /actuator/health: DataSource isValid() → UP, Ready 유지
```

앵커(정답지 `code_anchor` 와 같음):

- `food-delivery/dispatch-service/src/main/java/com/fooddelivery/dispatch/entity/DispatchOutboxEvent.java:7-9` (`@Table(name = "dispatch_outbox_events")`)
- `food-delivery/dispatch-service/src/main/java/com/fooddelivery/dispatch/service/DispatchService.java:51-77` (dispatchCourier, `:72` recordEvent, `:74` 'Dispatched courier'), `:120-130` (deliverExpiredDispatches), `:132-145` (recordEvent, `:139` publish, `:143` 'non-critical' WARN)
- `food-delivery/shop-common/src/main/java/com/fooddelivery/common/outbox/OutboxPublisher.java:25-37` (save 실패 → ServiceException 500)
- `food-delivery/shop-common/src/main/java/com/fooddelivery/common/outbox/OutboxEventRepository.java:17` (JpaRepository, save 가 트랜잭션에 참여)
- `food-delivery/shop-common/src/main/java/com/fooddelivery/common/outbox/OutboxEvent.java:14-15` (IDENTITY)
- `food-delivery/dispatch-service/src/main/java/com/fooddelivery/dispatch/config/GlobalExceptionHandler.java:9-21` (ServiceException 만 다룸)
- `food-delivery/dispatch-service/src/main/java/com/fooddelivery/dispatch/event/DispatchOutboxRelay.java:19-27`
- `food-delivery/shop-common/src/main/java/com/fooddelivery/common/outbox/OutboxRelay.java:46-48`
- `food-delivery/order-service/src/main/java/com/fooddelivery/order/client/DispatchClient.java:55-74` (503 'Dispatch service failed', fallback 'Dispatch service unavailable')
- `food-delivery/order-service/src/main/java/com/fooddelivery/order/service/OrderService.java:93-151` (용량 확인 `:96`, 'Created order' `:141`, 배차 fan-out `:145`, 'Order fan-out dispatch failed' `:147`)
- `food-delivery/dispatch-service/src/main/resources/application.yml:20` (ddl-auto none)
- `food-delivery/db/init.sql:142-152` (dispatch_outbox_events, 기본 키와 idx_dispatch_outbox_unpublished, 외래 키 없음)
- 인프라: rca-testbed-food `testbed-mysql-0`(MySQL 8.0.46, tb-w3). 109 실측(2026-10-11 19:43 UTC, 읽기 전용 세션): 표 116,615행(미발행 0, 데이터 25MB, 인덱스 3MB), `SHOW INDEX` 는 PRIMARY 와 idx_dispatch_outbox_unpublished, 외래 키로 참조되지 않음(REFERENTIAL_CONSTRAINTS 에 없음), fooddelivery 표 16개, `_vt` 표 0개, lock_wait_timeout 기본 31536000(스크립트가 세션에서 10 으로 낮춤), REPEATABLE READ. 배정 중(ASSIGNED) 294. dispatch 파드 `testbed-dispatch-6cbcbcbcf5-sgkgx`(재시작 0, 2026-10-11 06:52 UTC 롤아웃)

로컬 실측(2026-10-11, mysql:8.0 = 8.0.46 컨테이너, init.sql 의 dispatches, dispatch_events, dispatch_outbox_events 정의 그대로, outbox 3,000행 + 109 와 같은 main 의 dispatch-service jar, 실행기 스크립트 `MYSQL_HOLD_REMOTE` 를 가짜 kubectl 로):

- 주입 전 배차 3건 200, 용량 `{"currentAssigned":3,...}` 200.
- preflight rc 0, run rc 0(1초 안). 표 목록이 `_vt_hld_..._20261013193952_`, `_vt_vrp_..._20261011193951_`, dispatch_events, dispatches.
- 곧바로 배차 3건 모두 500. 로그는 배차마다 WARN 'SQL Error: 1146, SQLState: 42S02', ERROR "Table 'fooddelivery.dispatch_outbox_events' doesn't exist", WARN 'Failed to record dispatch outbox event (non-critical): Failed to record outbox event: could not execute statement [Table ... doesn't exist] [insert into dispatch_outbox_events ...]', INFO 'Dispatched courier=courier-e148f513 for order=11', ERROR 'Servlet.service() ... UnexpectedRollbackException: Transaction silently rolled back because it has been marked as rollback-only'. dispatches 행 수 3, dispatch_events 3 그대로(되돌림).
- 같은 때 용량 200, GET /api/deliveries/1 200, 목록 200, /events 200, health UP.
- 릴레이 확인(미발행 0 으로 다시 띄움): 주입 뒤 스케줄러 스레드에서 2초마다 WARN 1146 과 ERROR 'Table ... doesn't exist', ERROR 'Unexpected error occurred in scheduled task'(약 45초에 19, 15회), WARN 'Outbox purge failed, will retry next cycle: ... dispatch_outbox_events ...'. 만료 배치는 'Dispatch expiry batch started: candidates=4', 'non-critical' WARN 4건, 'finished: delivered=4' 를 찍었지만 ASSIGNED 4 그대로(되돌림).
- 두 번째 run 은 check 에서 거절(rc 1, MySQL 오류 첫 줄이 stderr), cleanup 두 번(두 번째는 할 일 없음) rc 0, recovery rc 0, 곧바로 배차 200, `_vt` 표 0, outbox 3,003행과 인덱스 그대로.

## 5. 원인 규정 (원칙 5, 6)

| 칸 | 값 | 근거 |
|---|---|---|
| `root_cause.target_id` | `food-mysql:fooddelivery.dispatch_outbox_events` | 결함은 표 쪽이다(운영 도구가 살아 있는 표를 치움). 원본 사후 보고의 원인과 복구도 데이터베이스 변경(표를 지움)과 그 되돌림을 가리킨다. 표기는 F48-P, F60-R, F60-H 와 같은 '인스턴스:스키마.테이블' 꼴 |
| `trigger_target_id` | null | 근본과 계기가 같은 곳(이 표에 대한 운영 작업) |
| `scoring.partial` | `food-delivery-dispatch`, `food-dispatch`, `food-mysql` | 오류가 찍히는 서비스와 DB 인스턴스는 원인 근처다(F60-H, F32-P 와 같은 부분 점수 구성) |

원칙 5: 정답은 로그의 오류 문장("없는 표 dispatch_outbox_events"), 그 실패 바로 뒤에 같은 트레이스로 오는 'rollback-only' 와 배차 500, 같은 DB 의 다른 질의 성공, 같은 분 DPM 표 수 증가, 그 시각에 배포나 재시작이 없다는 사실로 낼 수 있는 결론이다. "non-critical 기록이 왜 배차를 되돌리는가" 를 Spring 트랜잭션 전파로 설명할 필요는 없다: 로그가 순서대로 그 사실(1146 → non-critical → rollback-only → 500)을 보여 준다. "dispatch 가 배차에서 500 을 낸다" 고 답하면 dispatch(부분), "dispatch_outbox_events 표가 사라졌다(DDL, 운영 작업)" 고 답하면 정답이다. 'non-critical' 로 잡은 실패가 트랜잭션 전체를 되돌리는 코드 구조는 피해 설명이고 정답이 아니다.

원칙 6: dispatch 와 릴레이, 만료 배치, order 는 늘 하던 정당한 요청을 보냈고 결함 있는 곳(사라진 표)에서 실패했다. 표를 치운 운영 작업이 원인이다.

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 | 기대 |
|---|---|---|
| 감지 | order 서버 스팬 오류(POST /api/orders 503), dispatch 서버 스팬 오류(POST /api/deliveries/dispatch 500), 새 오류 로그 템플릿(1146 WARN, ERROR, 'non-critical' WARN, 'rollback-only' 서블릿 ERROR, 스케줄러 ERROR)과 로그 급증 | 주문 생성 100% 503. 로그는 부하와 무관하게 릴레이에서만 분당 약 90줄(2초마다 WARN, ERROR, 스택 붙은 ERROR), 만료 배치가 30초마다 만료 후보마다 세 줄, 여기에 배차 시도마다 다섯 줄 |
| 피해 판정 | 러너: 기준선 주문 생성 5xx 비율 ≥ 0.8 과 2xx 비율 < 0.2(3틱) | 평시 주문 생성 5xx 는 0 근처(배차 한도 503 이 드물게) |
| 원인 설명 | 로그 전수(1146 오류 문장, 'rollback-only', 'Dispatched courier' 계속, 만료 배치 후보 반복), DPM 표 수와 인덱스 수, order, payment 로그와 스팬, KCM 무변화 | §7 |

인시던트 근거와 위험:

- 같은 order 503 꼴의 F32-R, F33-R 이 정식이다. 이 후보는 order 에 더해 dispatch 에도 오류 로그와 5xx 스팬이 나고, dispatch 의 새 템플릿이 넷(1146 WARN, ERROR, 'non-critical' WARN, 'rollback-only' ERROR)에 스케줄러 ERROR 가 부하와 무관하게 2초마다 이어져 로그 급증 이벤트가 날 것으로 본다.
- 동반 부하가 없어 가장 한가한 시간(KST 03~06시)에는 주문 생성이 분당 약 12건이다. 그래도 order 는 주문마다 배차를 세 번 시도(재시도)해 dispatch 에 분당 약 36건의 500 이 나고, 릴레이 오류가 2초마다 이어진다. 인시던트가 안 생기면 첫 실행 검증에서 묶음의 멤버 템플릿, 판정 사유, promoted_at 과 실행 시각대를 본다(§11).

## 7. 관측 근거 표 (119 실조회, 2026-10-11 19:30~19:50 UTC)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 결정 증거는 전수 로그와 DPM, KCM 이다.

| 증거 | 119 표와 칸 | 조회 | 평시 결과 |
|---|---|---|---|
| 근본: 없는 표 오류 | CH `lucida_logs_local` (`service_name`, `body`) | `SELECT service_name, countIf(position(body,'SQL Error: 1146')>0), countIf(position(body,'doesn''t exist')>0), countIf(position(body,'rollback-only')>0) FROM lucida.lucida_logs_local WHERE service_name LIKE 'food-delivery-%' GROUP BY 1` | 다섯 food 서비스 모두 1146 0건, "doesn't exist" 0건, 'rollback-only' 0건(남아 있는 로그 가장 이른 2026-10-04 16:00 UTC) |
| 근본 경로의 평시 기록(같은 로거, 같은 서비스) | 같은 표 | `SELECT min(timestamp), countIf(position(body,'Failed to record dispatch outbox event')>0), countIf(position(body,'Unexpected error occurred in scheduled task')>0), countIf(position(body,'Dispatched courier=')>0), countIf(position(body,'Dispatch expiry batch finished')>0), count() FROM lucida.lucida_logs_local WHERE service_name='food-delivery-dispatch'` | 'non-critical' WARN 0건, 스케줄러 ERROR 99건(과거 시나리오 실행 창의 Hikari 연결 대기 시간 초과와 2026-10-09 F38-R read-only 30건, 1146 없음), 'Dispatched courier' 409,450건, 'expiry batch finished' 20,509건, 전체 455,213건. 'dispatch_outbox_events' 를 담은 줄 483건은 모두 2026-10-09 F38-R 창의 read-only 실패(UPDATE, purge)다(본문 분류 조회) |
| 오류 원문이 CH 에 남는가 | 같은 표, `log_attributes` | dispatch 스케줄러 ERROR 한 건과 food 서블릿 ERROR 한 건의 `log_attributes` | 스케줄러 ERROR 의 `exception.message` 에 실패한 문장 전체('could not execute statement [The MySQL server is running with the --read-only option ...] [insert into dispatch_events (...)]')와 `exception.stacktrace` 가 남는다. order 의 'Servlet.service() ... threw exception [Request processing failed: ...]' 도 `exception.message` 와 스택을 남긴다(남은 로그 전체 2026-10-04~ food 서비스 서블릿 ERROR 6,267건, 그중 dispatch 671건). 1146 과 UnexpectedRollbackException 도 같은 경로로 남는다 |
| 다른 질의는 계속 | CH `otel_traces_local` (`span_kind='SERVER'`, `span_name`, `http.response.status_code`) | 7일 dispatch 서버 스팬 | GET /api/deliveries/capacity 19,495(5xx 12), POST /api/deliveries/dispatch 19,190(5xx 3), GET /api/deliveries 5,202, /{id} 5,111. 고장 중 앞의 하나와 목록, 상세는 200 으로 이어지고 POST 만 500 이 된다(로컬 실측) |
| 계기: 백필(그림자 표와 새 인덱스) | VM `dpm.mysql.database.table_count{db_name="fooddelivery"}`, `dpm.mysql.database.index_count` (target c8c558e5-…, 60초 간격) | 현재값, `max_over_time`, `min_over_time` [7d] | table_count 현재 16, 7일 최대 16, 최소 16. index_count 현재 31, 7일 최대 31, 최소 30. F48-R, F48-P 시트 §7 이 같은 지표로 표 수 16 고정을 확인한 선례다 |
| 계기: DDL 문장 자체 | CH `dpm_topsql_local` | DDL 검색 | F48-R 시트와 같이 DDL 은 Top SQL 에 남지 않는다 |
| outbox 표의 평시 질의 | `dpm_topsql_local` `body` | `dispatch_outbox_events` 를 포함한 문장 | 릴레이, 정리 SELECT 16,250행, 정리 DELETE 3,844행(2026-08-13~10-11). 이 표가 늘 쓰이는 표임을 보여 준다 |
| 다른 변경 없음 | CH `kcm_events_local` | `namespace='rca-testbed-food' AND position(object_name,'testbed-dispatch')=1` 사유별 | Unhealthy 1,276, Killing 52, ScalingReplicaSet 32(마지막 2026-10-11 06:53 UTC, 롤아웃은 이 표에 남는다). 109 kubectl: dispatch 파드 재시작 0 |
| 기준선 주문량 | `lucida_logs_local` | dispatch 'Dispatched courier=' 를 KST 시각별 7일 평균 | 분당 11.8(06시)~68.6(17시)건. 만료 배치 완료 로그 하루 2,878건(30초마다) |
| 배제: DB 정상 | VM `dpm.mysql.session.active_session`, `dpm.mysql.instance.avg_query_response_time` | `quantile_over_time(0.5, ...[7d])` | 7일 중앙 1 세션, 6.5ms |
| 배제: order 쪽 평시 실패 | `lucida_logs_local` order | 'Failed to dispatch courier', 'Order fan-out dispatch failed', 'Dispatch service unavailable' | 242, 334, 334건(남은 로그 전체, 과거 시나리오 실행 창). 'Created order' 409,634건 |

## 8. 감별

- must_support: 1146 오류 로그(평시 0)가 배차 시도마다, 2초마다, 30초마다 나옴, 'non-critical' WARN 뒤 같은 트레이스의 'rollback-only' ERROR 와 배차 500('Dispatched courier' 계속), 같은 분 DPM 표 수 16→17과 인덱스 수 31→34, order 503 'Dispatch service failed'/'Dispatch service unavailable' 와 용량 확인·배달 조회·둘러보기 200, 롤아웃과 재시작 없음과 dispatch Hikari 정상.
- must_rule_out(정답지에 문장으로): dispatch 나 order 새 버전, 배차 표 지움(F32-P)이나 잠금(F60-H), 배달원 한도(F32-R), MySQL 읽기 전용(F38-R)이나 다운, 부하나 풀 부족.
- contrast_with: F60-R(같은 배차 트랜잭션의 기록 단계, 표 복사 잠금으로 기다림), F48-P(같은 원본과 도구, order 자신의 outbox), F32-P(배차 표 자체가 사라짐), F32-R(배달원 한도).
- 겉 증상이 같은 정식·후보와 가르는 관측 근거(H 관계):
  - F32-R(배달원 한도 설정): order 503 의 문장이 'No courier available (capacity full)' 이고 dispatch 는 500 을 내지 않는다. F60-P 는 'Dispatch service failed: 500' 과 dispatch 의 1146, 'rollback-only' 다. 배정 중 수가 한도에 닿지 않는다.
  - F32-P(dispatches 지움): 용량 확인(COUNT)부터 1146 으로 실패하고 배달 조회도 실패한다. F60-P 는 용량 확인과 배달 조회가 200 이고 실패 문장의 표가 dispatch_outbox_events 다.
  - F60-H(dispatches 메타데이터 잠금 대기열): dispatch 가 기다리다 풀이 차 'Connection is not available' 과 NotReady, 재시작이 난다. F60-P 는 기다림이 없고(실패한 배차가 수십 ms), 풀과 Ready 가 정상이며 DPM 에 잠금 대기 세션이 없다.
  - F33-R(인덱스 제거, 전수 스캔): MySQL CPU 포화와 느린 COUNT. F60-P 는 MySQL 응답 시간과 세션이 평시다.
  - 계기: F60-P 만 DPM 표 수와 인덱스 수가 바뀐다(F32-P 는 표 수가 줄고, 나머지는 그대로).
- 판별력: 서비스 입도 채점에서는 F32-R, F43-P, F55-R, F63-P 와 같은 'dispatch' 근처 답이 부분 점수다. 정답 입도(database-relation)에서는 표 이름이 정답이고, 인스턴스(food-mysql)는 부분 점수다.

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

진행 대본: 설계 강도 하나로 고정한 평가 모드(`approved-fixed-f60-p`). 강도라는 축이 없다: 표가 없으면 그 표에 쓰는 문장은 요청량과 무관하게 100% 실패한다.

| 항목 | 값 | 근거 |
|---|---|---|
| primary | db.ddl(보류 표 모드), 계약 `{engine mysql, rca-testbed-food, testbed-mysql-0, fooddelivery, dispatch_outbox_events, backfill_index idx_dispatch_outbox_aggregate, backfill_column aggregate_id, minimum_rows 1000}` | 실행기 CONTRACTS 와 profiles.json 같은 값(G2). 스크립트는 F48-R, F48-P 와 같고 계약만 다르다(테스트로 고정) |
| companion | 없음 | 기준선만으로 모든 주문 생성이 실패한다(아래). F48-P, F14-P 가 동반 부하 없는 평가 모드 선례 |
| entry_status | 기준선 문서(domain food-delivery)의 주문 생성 단계 | 고장 중 503(0 아님). 0 은 노드나 order 가 죽었을 때만 |
| min_hold / settle / timeout | 15m / 30s / 20m, max_injection 25m | F48-P 와 같음 |
| success(3틱) | 기준선 주문 생성 5xx 비율 ≥ 0.8, 2xx 비율 < 0.2 | loadgen 집계(표본 아님), 30초 창에 6~34건 |
| must_rule_out(2틱) | MySQL NotReady, order NotReady | DB 다운이나 order 다운이면 다른 시나리오다. dispatch 파드 준비 상태는 러너 kubernetes 선택자 허용 목록에 없어 관측하지 않는다(F60-H, F63-P 와 같음, 러너 변경 없음) |
| abort | entry_status == 0 (필수) | |
| recovery(2틱, 10m) | target_health 200, MySQL Ready, order Ready, 기준선 주문 2xx ≥ 0.7 | 되돌리면 다음 배차부터 즉시 200(로컬 실측), order 의 dispatch 서킷은 5초 뒤 반열림에서 닫힘 |

부하 계산:

- 기준선 주문 생성: 분당 약 12(KST 03~06시)~69건(15~18시), 30초 창 6~34건. 고장 중 모두 503 → 5xx 비율 1.0, 2xx 비율 0. 문턱(0.8, 0.2)을 어느 시간대에도 넘는다.
- 서킷브레이커: order 의 dispatch 서킷(창 10, 최소 5회, 실패율 50%, 열림 5초, 반열림 3회)이 몇 초 안에 열리고, 열린 5초 동안 용량 확인이 fallback 503 이 된다. 반열림의 세 호출이 다시 500 이라 곧 다시 열린다. 어느 쪽이든 주문은 503 이고 실패 지점이 두 곳(용량 확인, 배차)으로 갈리는 것은 같은 원인의 두 모양이다. order 의 restaurant, payment 서킷은 열리지 않는다(restaurant 200, payment 호출 없음).
- 배달원 한도: 실패한 배차는 되돌려지고 만료 배치도 되돌려져 배정 중 수가 주입 때 값(한가할 때 약 300, 바쁠 때 약 1,700, 한도 2000)에 머문다. 한도로 실패 지점이 옮겨 갈 일이 없다. 고장이 끝나면 다음 만료 배치가 밀린 만료분을 한 번에 DELIVERED 로 바꾼다(가장 바쁜 시간 15분이면 약 1,000건, 한 트랜잭션에 수 초).
- 로그 양: 만료 배치는 30초마다 밀린 만료 후보를 모두 다시 처리하고 되돌린다. 후보는 고장 동안 분당 약 12~69건씩 쌓여 15분 끝에 최대 약 1,000건, 회차마다 후보당 세 줄(1146 WARN, ERROR, 'non-critical' WARN)이다. 가장 바쁜 시간에도 15분 합계 약 4~5만 줄로, 119 수집이 감당하는 범위다(F38-R 녹화 창 order 한 서비스 3,874줄, 1분 간격 DPM).
- 강도와 피해: 주입은 표 이름 하나를 바꾸는 메타데이터 작업이고 그림자 복사는 약 11만 6천 행(25MB) 2~3초다. 복사 동안 REPEATABLE READ 의 공유 잠금이 원본 표 끝에 걸려 그 사이 들어온 outbox INSERT 와 릴레이 UPDATE 가 2~3초 기다릴 수 있지만 그 뒤 곧바로 RENAME 이 이어진다. 실패한 배차는 즉시 SQL 오류와 되돌림으로 끝나 dispatch 풀을 오래 쥐지 않는다(로컬 실측 수십 ms).

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(GitHub 2026-07-24, 공식 월간 가용성 보고)와 요소별 대응표가 있고 기전이 같다 (§2)
- [x] 분류 장부 §4-1, §6 갱신. 묶음 P 11→12(12.2%, L 로 세도 9.2%), 정답 위치 DB 테이블(배차 이벤트 outbox) 0→1(1.0%), 결제 경로 13/98(13.3%), 음식배달 32→33(가장 적은 동률). 어느 축도 20% 미만 (§3)
- [x] 근본 원인 위치: `DispatchOutboxEvent.java:7-9`, `DispatchService.java:132-145`, `OutboxPublisher.java:25-37`, 인프라 testbed-mysql-0 dispatch_outbox_events (§4)
- [x] 근본 원인 흔적: CH `lucida_logs_local` 1146 오류 문장(전수, 같은 서비스의 스케줄러 ERROR 와 서블릿 ERROR 가 exception.message 에 문장과 표 이름을 남기는 것 확인) (§7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: 로그 전수, DPM 표 수와 인덱스 수, KCM. 스팬은 보조 (§7)
- [x] 계기 흔적: DPM table_count 16→17, index_count 31→34(1분 간격, 7일 동안 16 고정), 오류 문장의 표 이름, 그 시각에 롤아웃과 재시작 없음. 인공 지연 없음 (§7)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다. 'non-critical' 실패가 배차를 되돌리는 코드 구조는 로그 순서로 보이는 피해 설명에만 둔다 (§5)
- [x] 정답지 세 칸이 원칙 6대로 (§5)
- [x] 감지, 피해 판정, RCA 증거 구분과 인시던트 위험 (§6)
- [x] 주입 도구가 시나리오 id 를 남기지 않는다: 표 이름은 Vitess 꼴 접두사, 무작위 uuid, 시각뿐이고 인자와 스크립트에 id 없음(테스트로 고정). 동반 부하가 없어 k6 태그도 없다
- [x] 부하 상한과 서킷브레이커를 고려한 피해 계산, 가장 한가한 시간대와 가장 바쁜 시간대 둘 다 (§9)

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- dispatch 로그에 'SQL Error: 1146, SQLState: 42S02' 와 "Table 'fooddelivery.dispatch_outbox_events' doesn't exist", 'Failed to record dispatch outbox event (non-critical)', 'UnexpectedRollbackException ... rollback-only' 가 배차 시도마다, 스케줄러 ERROR 'Unexpected error occurred in scheduled task' 가 2초마다 남는지. POST /api/deliveries/dispatch 스팬이 500 이고 order POST /api/orders 가 503 인지.
- 'Dispatched courier=' 는 이어지는데 배정 중 수가 늘지 않는지, 'Dispatch expiry batch started: candidates=N' 의 N 이 회차마다 커지는지(되돌림의 흔적), 용량 확인과 배달 조회가 200 인지.
- VM 의 `dpm.mysql.database.table_count{db_name="fooddelivery"}` 가 주입 분에 17, `index_count` 가 34 가 되고 cleanup 뒤 16, 31 로 돌아오는지.
- 인시던트: 묶음 멤버 템플릿과 판정 사유, promoted_at, 실행 시각대. 한가한 시간에 신호가 약해 인시던트가 안 생겼다면 그 사실을 검증 보고서에 남긴다. 보강 후보는 주문 동반 부하(order-surge.js, 배정 중 수가 멈춰 한도 위험 없음)다.
- 주입이 메타데이터 잠금 대기로 실패(lock_wait_timeout 10초)하면 아무것도 바뀌지 않은 채 run 이 실패하고 MySQL 오류 첫 줄이 러너 로그에 남는다. 그림자 표만 만들어진 채 RENAME 이 실패했다면 cleanup 이 그림자를 지운다.
- cleanup 뒤 `dispatch_outbox_events` 가 행 그대로이고 `_vt_` 로 시작하는 표가 없는지(recovery 가 확인), 밀린 만료분이 다음 배치에서 한꺼번에 DELIVERED 가 되고 그 이벤트가 발행되는지. 고장 중 쌓이지 못한 배차 이벤트는 없다(배차가 되돌려졌으므로).
- 녹화 창에 food MySQL OOM 재시작이 끼면 녹화로 쓰지 않는다.
