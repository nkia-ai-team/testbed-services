---
title: F57-P 설계 시트 (DB 를 새 인스턴스로 옮긴 이전이 commerce 재고 이동 이력 표의 id 시퀀스를 스냅숏 시점에 남겨 짧은 읽기 전용 전환 뒤 재고 예약이 기본 키 충돌로 실패)
status: Draft
owner: project
last_reviewed: 2026-10-11
tags:
  - scenario
  - design
  - schema
  - postgresql
  - migration
summary: 운영자가 commerce DB 를 새 PostgreSQL 인스턴스로 옮기는 이전(스냅숏 복사, 논리 복제 따라잡기)에서 행은 모두 옮겼지만 논리 복제가 옮기지 않는 inventory_schema.inventory_movements 의 id 시퀀스 상태는 옮기지 않았다. 전환(옛 쪽 읽기 전용과 앱 연결 끊기로 약 6초 commerce 쓰기 실패, 다시 끊어 새 쪽으로) 뒤 시퀀스가 스냅숏 시점 값(최대 키보다 10만 뒤)에서 다시 시작해 재고 예약의 이동 이력 INSERT 가 모두 기본 키 23505 로 거절되고, inventory 500, product 500, checkout 502 가 된다. 원본은 RevenueCat 2022-11-23 Aurora Postgres 이전이고, F57-R(banking Oracle identity 판)의 원본 엔진 판이다. 재평가 1회차 FAIL(원칙 4, 8) 반영판.
---

# F57-P 설계 시트

## 1. 요약

commerce 의 inventory-service 는 모든 재고 변화를 PostgreSQL `inventory_schema.inventory_movements`(2026-10-10 109 실측 9,685,372행, 최대 키 9,685,846, 인덱스 셋 포함 1,371MB)에 남긴다. 키 열 `id` 는 `BIGSERIAL` 이라 기본값 `nextval('inventory_schema.inventory_movements_id_seq')` 가 키를 내주고 기본 키 `inventory_movements_pkey` 가 지킨다. 이 표에 쓰는 것은 inventory-service 하나다(재고 예약 RESERVE, 해제 RELEASE, 조정 ADJUST, 1분 주기 재조정 배치의 ADJUST, RESTOCK).

운영자가 commerce DB 를 원본이 DB 를 옮긴 방식대로 새 PostgreSQL 인스턴스로 옮겼다: 스냅숏에서 새 사본을 만들고 그 뒤 쓰인 행은 논리 복제로 따라잡은 뒤 전환했다. 전환은 원본처럼 옛 쪽을 읽기 전용으로 두고 앱의 DB 연결을 끊어(풀이 쓰기를 거절하는 DB 에 다시 붙어 몇 초 동안 쓰기가 실패하고 읽기는 됨), 다시 연결을 끊어 새 쪽으로 넘기는 것이었다. PostgreSQL 논리 복제는 표 행은 옮기지만 시퀀스 상태는 옮기지 않는다. 그래서 새 쪽에서 재고 이동 이력 표의 시퀀스는 스냅숏 시점 값, 표의 최대 키보다 10만 뒤(재고 이동 약 39시간치)에 머문다. 원본처럼 큰 표 몇 개가 부딪힌 것을 이 표 하나로 줄였다.

전환의 약 6초 동안 commerce 의 모든 쓰기가 `25006 cannot execute ... in a read-only transaction` 으로 실패한다(풀은 끊긴 연결을 `57P01 terminating connection due to administrator command` 로 기록). 서비스들이 쓰기 가능으로 다시 붙은 순간부터 재고 예약이 모두 실패한다. checkout 은 장바구니와 가격 견적 뒤 품목마다 product 에 재고 예약(`POST /api/products/{id}/reserve-stock`)을 부르고, product 는 상품을 찾은 뒤 inventory(`POST /api/inventory/reserve`)를 부른다. inventory 는 재고 행을 `FOR UPDATE` 로 잡고 재고를 줄인 뒤 같은 트랜잭션에서 RESERVE 이동 이력을 넣는데, 시퀀스가 준 키가 이미 있어 PostgreSQL 이 `23505 duplicate key value violates unique constraint "inventory_movements_pkey"`, `Key (id)=(N) already exists` 로 거절한다. 트랜잭션이 되돌려져 재고는 줄지 않고 inventory 는 수 ms 안에 500 을 낸다. product 가 500 을 넘기고, order 의 productClient 가 200ms, 400ms 재시도 뒤 서킷을 열어 checkout 이 주문, 결제, banking 정산 전에 502 `Product service unavailable for product N` 으로 끝난다. 같은 표에 매분 RESTOCK, ADJUST 를 쓰는 재조정 배치도 같은 이유로 실패한다.

전환 뒤 표 읽기(배치의 최신 이동 조회, 이동 이력 조회), 재고 조회, 둘러보기, 장바구니, 가격, 로그인, 다른 commerce 표는 정상이다. 표의 행, 인덱스, 제약, 통계는 그대로이고 롤아웃도 재시작도 없다. PostgreSQL 은 떠 있고 inventory 는 Ready 이며 풀도 비지 않는다.

비유: 창고를 새 건물로 옮기며 출납부를 그대로 베껴 갔는데, 다음 쪽 번호를 찍는 넘버링 도장은 이틀 전 숫자에 둔 채였다. 이사하는 날 몇 초 동안 옛 창고 창구가 닫혀 출고를 못 받다가(읽기 전용), 새 건물 창구가 열리자 직원이 도장을 찍을 때마다 이미 있는 쪽 번호가 나와 장부 규칙(같은 쪽 번호 금지)에 걸려 출고 자체를 취소한다. 재고를 세는 일은 멀쩡하다.

## 2. 원본 사례

- 기업: RevenueCat (앱 구독 결제 인프라)
- 날짜: 2022-11-23 (UTC 9:34 옛 클러스터 읽기 전용, 트래픽 전환과 장애 시작, 13:07 대시보드와 SDK 끝점 100% 회복)
- 링크: [Postmortem for Aurora Postgres Migration November 23 2022 (공식 엔지니어링 블로그)](https://www.revenuecat.com/blog/engineering/postmortem-aurora-postgres-migration) (자료 문서 `ref-real-world-incidents.md` M21 에 F57-R 때 추가됨)
- 요약(출처가 말한 것만, 자료 문서 그대로): Aurora Postgres 10 에서 14 로 올리려고 스냅숏에서 새 클러스터를 만들고(2:51) 논리 복제로 따라잡은 뒤(8:51) 9:34 에 옛 클러스터를 읽기 전용으로 두고 트래픽을 넘겼다("requests with writes are expected to fail for less than 2 minutes"). 연쇄 장애 가운데 하나가 기본 키 중복이었다: "We did not realize that the state of sequences would not be copied to the databases." 논리 복제는 표 값은 옮기지만 시퀀스 상태는 옮기지 않아 "For a handful of large tables, we ended up with primary key sequences running into their values", 이미 표에 있는 행의 유일 제약을 위반했다. 미리 준비한 스크립트로 해당 시퀀스 값을 자동으로 올려 빠르게 완화했다. (같은 사고의 주 원인은 새 클러스터에 ANALYZE 가 돌지 않은 질의 계획 문제였고, 이 재구성은 그중 기본 키 중복 고리만 쓴다.)

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | DB 이전(스냅숏으로 새 클러스터, 논리 복제로 따라잡음). 9:34 옛 클러스터를 읽기 전용으로 두고(쓰기는 2분 안 되게 실패) 트래픽을 새 클러스터로 넘김 | commerce DB 이전(새 인스턴스에 스냅숏 사본, 그 뒤 행은 논리 복제로 따라잡음). 전환: 옛 쪽 읽기 전용(`ALTER DATABASE commerce SET default_transaction_read_only = on`)과 앱 연결 끊기(`pg_terminate_backend`)로 약 6초 쓰기 실패(25006, 읽기는 됨), 이어서 읽기 전용 해제와 다시 끊기로 새 쪽에 붙음 |
| 원인이 된 결함 | 표 값은 복제됐지만 시퀀스 상태는 복제되지 않아 기본 키 시퀀스가 기존 값 쪽에 머묾 | 행은 모두 옮겼지만 inventory_movements 의 id 시퀀스 상태는 옮기지 않아 시퀀스가 스냅숏 시점 값(최대 키 - 100000)에 머묾 |
| 전파 경로 | 새 행이 이미 있는 키를 받아 유일 제약 위반, 쓰기 실패 | RESERVE 이동 이력 INSERT 가 이미 있는 키를 받아 기본 키 inventory_movements_pkey 가 23505 로 거절 → 재고 예약 트랜잭션 되돌림, inventory 500 → product 500 → order productClient 재시도, 서킷 → checkout 502 |
| 사용자 증상 | 전환 동안 쓰기 실패, 이어서 큰 표 몇 개의 쓰기 실패(같은 사고의 다른 고리와 겹쳐 사용자 영향이 따로 적히지 않음) | 전환 약 6초 commerce 쓰기 전부 실패, 그 뒤 commerce 구매 100% 실패(결제 전 단계). 둘러보기, 재고 조회, 장바구니, 가격은 정상 |
| 탐지된 경로 | 출처에 탐지 경로가 따로 적히지 않음(유일 제약 위반) | commerce order, gateway 오류율, 전환 로그(57P01, 25006), inventory 새 오류 로그(23505 + 'Key (id)=(N) already exists', Servlet SEVERE, 재조정 배치 실패) → 119 이상 탐지 → 인시던트 |
| 완화와 복구 | 미리 준비한 스크립트로 해당 시퀀스 값을 올림 | cleanup 이 `setval` 로 시퀀스를 최대 키로 올림(즉시 회복). 읽기 전용이 남았으면 먼저 풀고 세션을 끊음 |

기전은 원본과 같다: "옛 쪽을 읽기 전용으로 두고 트래픽을 새 쪽으로 넘김(짧은 쓰기 실패) → 이전이 행은 옮기고 시퀀스 상태는 옮기지 않음 → 시퀀스가 이미 쓰인 키를 다시 내줌 → 기본 키가 새 행을 거절해 그 표에 쓰는 요청이 모두 실패 → 시퀀스를 최대 키 뒤로 올리면 회복". 엔진도 원본과 같은 PostgreSQL 시퀀스다(F57-R 은 Oracle identity 로 옮긴 판). 시퀀스가 부딪힌 표는 하나로 줄였다.

재구성의 경계: 새 인스턴스, 행 복사(약 969만 행, 1.37GB), 논리 복제는 다시 만들지 않는다. 같은 인스턴스에서 전환과 그 결과를 재현한다. ① 옛 쪽 읽기 전용: `ALTER DATABASE commerce SET default_transaction_read_only = on` 과 앱 계정 클라이언트 세션(실행기 자신 제외) `pg_terminate_backend`. 풀이 다시 붙은 세션은 읽기 전용이라 쓰기가 25006 으로 실패하고 읽기는 된다. 6초 유지(러너 lease 30초 안, 원본의 "2분 안 되게"). ② 새 쪽이 넘겨받음: 한 트랜잭션에서 `LOCK TABLE inventory_schema.inventory_movements IN EXCLUSIVE MODE`(대기 최대 2초), 잠금 아래 키 구간 다시 세기, `setval('inventory_schema.inventory_movements_id_seq', 최대 키 - 100000, true)`. ③ `ALTER DATABASE ... RESET default_transaction_read_only` 와 다시 세션 끊기로 풀이 쓰기 가능으로 다시 붙는다. 읽기 전용 뒤 어느 단계가 실패해도 ③을 먼저 하고 멈춘다. 행, 인덱스, 제약, 표 통계는 그대로다(복사와 복제로 채운 표는 이미 행을 센다). 100000 은 최근 키가 39시간치라(2026-10-09 08:55 ~ 10-10 23:47 UTC) 스냅숏에서 전환까지 걸린 시간으로 둔 값이고, 그 자리부터 최대 키 - 1000 까지 키가 모두 있음을 run 앞과 잠금 아래에서 두 번 센다(109 실측: 최근 10만 키 100,000건 모두 있음). 잠금 아래 다시 센 결과가 맞지 않으면 `RAISE EXCEPTION` 으로 트랜잭션 전체가 끝나 시퀀스를 건드리지 않는다(`setval` 은 되돌려지지 않으므로 확인 뒤에 둔다).

전환을 이렇게 만든 이유: 원본의 전환은 옛 클러스터 전체를 읽기 전용으로 두고 트래픽을 넘긴 것이다. PostgreSQL 에서 운영자가 DB 를 읽기 전용으로 두는 보통 방법이 `default_transaction_read_only` 기본값이고, 이미 열린 연결에는 안 미치므로 연결을 끊어 다시 붙게 하는 것도 전환 절차 그대로다. 표 하나의 쓰기 권한 회수는 앱 계정 `commerce` 가 슈퍼유저(109 `pg_roles.rolsuper = t`)라 듣지 않고, 트리거나 `pg_sleep` 같은 실제에 없는 문장은 쓰지 않는다(유지는 실행기 셸의 sleep 이고 DB 에는 대기 문장이 없다). 실행기 세션은 `default_transaction_read_only=off` 로 열어 자신은 막히지 않는다. 109 앱 세션은 commerce 계정 JDBC 56개(2026-10-11 조회)이고 DPM 수집 계정(lucida_mon)은 끊지 않는다.

관계와 id: 원본(RevenueCat 2022-11-23)과 원인 꼴(키 생성기 상태가 데이터 뒤에 남음)이 F57-R 과 같고 대상, 엔진, 도메인, 전파 경로가 달라 사례군 F57 의 P 다(같은 원본을 다른 표에 다시 쓴 F53-R/F53-P, F48-R/F48-P 와 같은 번호 관례, 장부 §6 에 적음). 서비스 이름만 바꾼 복제가 아니다: 엔진(PostgreSQL 시퀀스와 `setval`, 오류 25006, 23505 대 Oracle identity 와 ORA-12081, ORA-00001), 전환 범위(DB 전체 읽기 전용과 연결 끊기 대 표 하나 READ ONLY), 진입 경로(commerce gateway → order → product → inventory 대 banking nginx → api → account → transfer 와 commerce 정산), 실패 지점(결제 전 재고 예약 대 결제 뒤 정산), 함께 실패하는 내부 배치가 다르다. 같은 수단(db.ddl)이지만 새 모드(PostgreSQL 시퀀스 되감기)와 새 파라미터라 같은 주입 중복이 아니다. rejected 의 '표 이전이 commerce cart_items 또는 order_items 시퀀스 상태를 옮기지 않음' 은 cart_items 가 비어 있고 order_items 의 정답 위치가 0 이 아니어서 막혔다. 이 후보는 다른 표(inventory_movements, 969만 행, 키가 빈틈없이 참)이고 정답 위치가 0 이다. F38-R(food MySQL 전체 읽기 전용)과는 읽기 전용이 전환의 6초뿐이고 그 뒤 원인이 시퀀스라는 점이 다르다.

(1차 평가 FAIL 반영, 2026-10-11: 첫 판은 '새로 넘겨받은 표 객체는 누적 통계가 0 에서 시작한다' 를 근거로 run 에서 표와 인덱스의 통계 카운터를 0 으로 돌려 DPM live_rows 약 969만 하락을 계기의 결정 흔적으로 쓰고, cleanup 의 ANALYZE 를 원본의 해결이라 불렀다. 평가의 지적이 맞다: 스냅숏 복사와 논리 복제로 채운 표는 n_live_tup 이 이미 행 수만큼 차 있고(평가 로컬 실측), 원본의 ANALYZE 는 이 재구성이 쓰지 않는 질의 계획 고리를 고친 것이다. 그 하락은 이 주입 도구만 만드는 흔적이라 관제 AI 에 'DB 행 수 급락 = 표 이전' 이라는 현실에 없는 지름길을 가르친다(seq, idx 스캔 속도 리셋도 함께). 통계 리셋과 ANALYZE 를 빼고 계기 흔적을 실제 전환이 남기는 전수 로그 경계로 다시 세웠다(§7). 같은 평가의 권고대로 이야기를 '다른 인스턴스로 옮김' 으로 정리하고, Servlet 줄 수준을 SEVERE 로, '평시 0' 대상을 23505 문장으로 좁히고, R/P 접미사 관례를 장부에 적었다.)

(재평가 1회차 FAIL 반영, 2026-10-11: 1차 반영판은 전환을 1초 미만의 표 잠금과 setval 로만 두고, 계기 흔적으로 'checkout 완료 로그가 끊기는 분부터 23505' 를 내세웠다. 평가의 지적이 맞다: 그것은 증상이 시작되는 시각이지 시퀀스를 뒤로 보낸 사건의 흔적이 아니고, F57-R 1차 판이 같은 이유로 FAIL 이었다. 정답지도 '다른 변경 없음' 을 지지 근거로 썼다. 평가의 길 (나)를 골라 원본 그대로의 전환(옛 쪽 읽기 전용, 짧은 쓰기 실패, 연결을 새 쪽으로 넘김)을 PostgreSQL 안에서 재현하게 고쳤고, '다른 변경 없음' 은 must_rule_out 으로만 옮겼다. 테스트 주석의 옛 '표 통계 카운터 0' 표현도 고쳤다. 권고대로 Servlet 줄을 119 수집 수준인 SEVERE 로 적고, cleanup 의 `original` 호출 줄과 VACUUM 금지를 테스트로 고정했다.)

## 3. 숫자 근거 (2026-10-11, `scenario-stats.py`, 정식 43 + 후보 38 = 81, 이 후보 전)

| 축 | 값 | 판단 |
|---|---|---|
| 서비스 | 쇼핑몰 27, 은행 27, 음식배달 27 | 셋이 같다. 셋 모두 '가장 많은 서비스'라 정답 위치가 0 인 부품을 우선한다. 쇼핑몰은 후보가 4개뿐(은행 16, 음식배달 18)이고 쇼핑몰 부품 탐색이 이번 실행에서 가장 덜 됐다 → 쇼핑몰(27→28) |
| 묶음 | J 13(16%), P 10(12%), D 8, G 8, A 7, B 7, L 7, C 6, E 4, F 3, H 2, K 2, I 1, M 1, O 1, Q 1, N 0 | Q(데이터 이전 뒤 부속 상태 어긋남) 1→2(2.4%). J 는 상한(16.4)에 가까워 피했다 |
| 정답 위치 | DB 테이블(재고 이동 이력) 0 | 0→1. 재고 행 표 DB 테이블(재고)(3)와 다른 표이고, 재고 서비스(2)가 아니다. 최다 위치는 주문 서비스 8(9.8%) |
| 결제 경로 합계 | 12(14.8%) | 결제가 정답이 아니므로 그대로(12/82, 14.6%) |
| 어느 축이든 | 최대는 J 13/82(15.9%) | 20%(16.4) 미만 |

묶음 해석: 바뀐 것은 표의 키 시퀀스 상태 하나다. 설정값 배포가 아니라 G 가 아니고, 코드나 이미지 배포가 없어 J 가 아니다. 운영 작업의 대상은 맞았고(옮기려던 표) 빠뜨린 것이 작업 내용(시퀀스 상태)이라 대상 착오인 P 가 아니며, 표가 사라진 것도 아니다. 스키마와 코드는 그대로라 L 도 아니다. 잠금은 전환 순간(1초 미만)뿐이라 D 가 아니다. 장부 §2 의 Q 정의(데이터 이전, 복원, 복제 전환에서 표 데이터와 함께 가야 할 식별자 생성기, 시퀀스가 따라오지 않아 새 쓰기가 기존 데이터와 충돌)에 그대로 맞는다.

### 3단계 후보 목록 (실제 기전 × 부품, 숫자 순으로 줄 세움, 버린 이유 포함)

장부와 기존 목록은 피할 것 확인에만 썼다. 부품 여덟(commerce PostgreSQL inventory_movements, commerce pricing, commerce inventory, commerce PostgreSQL 앱 계정, commerce Kafka 와 notification, commerce gateway, banking nginx, commerce cart Redis)에 걸쳐 실제 기전으로 후보를 뽑았다. 이번 실행에서 쇼핑몰은 오래 최다 서비스라 가장 덜 탐색됐다.

| 순위 | 후보 | 원본 사례 | 부품(정답 위치 수), 묶음 | 결과 |
|---|---|---|---|---|
| 1 | 표 이전이 commerce inventory_movements 의 id 시퀀스 상태를 옮기지 않아 재고 예약이 기본 키 충돌 | RevenueCat 2022-11-23 공식 엔지니어링 블로그 | commerce PostgreSQL, DB 테이블(재고 이동 이력) 0, Q 1, 쇼핑몰 27 | **채택(F57-P)**. 로컬 PostgreSQL 16 과 저장소 inventory-service jar 로 실측(§4), 109 preflight 통과 |
| 2 | commerce pricing 새 릴리스가 견적 응답 구조를 바꿔 order 가 해석 실패 | GitHub 2021-10-08 공식 월간 보고(자료 문서 M8) | commerce pricing, 가격 서비스 1, L 7 | 뒤로 미룸(원칙 2): 정답 위치가 0 이 아니고, 같은 원본을 F49-R, F49-P 가 이미 두 번 썼다. 막힌 것은 아니다 |
| 3 | commerce inventory 새 릴리스가 헬스 끝점을 없애 liveness 재시작 반복 | Spreedly 2023-01-25 공식 상태 페이지(자료 문서 M11) | commerce inventory, 재고 서비스 2, J 13 | 뒤로 미룸(원칙 2): J 가 16% 로 상한에 가깝고 정답 위치가 0 이 아니다. F41-H 의 commerce 판이라 같은 원본, 같은 수단 모드 |
| 4 | commerce PostgreSQL 앱 계정 자격 증명 회전 실수로 계정 비활성 | Harness 2026-01-08 공식 상태 페이지(자료 문서 M7) | commerce PostgreSQL 계정, DB 계정 2, O 1 | 버림(카탈로그 §1): commerce 아홉 서비스가 계정 하나(commerce)를 함께 써 도메인 전체 DB 접속 실패가 되고, F35-R(banking 네 서비스가 한 계정, 계정 잠금)과 수단 엔진만 다른 같은 꼴이다 |
| 5 | commerce inventory 새 릴리스가 선언 안 된 요청 필드를 거절(422) | Buttondown 2026-06-14 공식 블로그(자료 문서 M8) | commerce inventory, 재고 서비스 2, L | 버림(F34-R 교훈): product 가 inventory 의 4xx 를 같은 상태로 넘기고 order 는 4xx 를 ClientErrorException 으로 그대로 전파해(ProductClient.java:38-39, 49-50) checkout 4xx 업무 거절 단일 신호가 된다 |
| 6 | commerce notification 소비자나 Kafka 토픽 설정 고장 | PostHog 2026-07-23(2차), PagerDuty 2023-08-28 | commerce Kafka, notification, 메시지 브로커 0, 알림 서비스 0 | 버림(원칙 7): notification 은 로그만 남기는 비동기 소비자이고 commerce Kafka 발행은 outbox 릴레이라 사용자 경로 증상이 없다(rejected 2026-10-09, 10-10 행과 같은 벽) |
| 7 | commerce gateway 새 릴리스의 메모리 누수 | Honeycomb 2019-11-06 공식 | commerce gateway, 게이트웨이 0, J | 버림(컨트롤러 필수 중단 조건): commerce 부하가 gateway 로 바로 들어가 gateway 가 멈추면 진입 상태 0 으로 abort(rejected 2026-10-10 행과 같은 벽) |
| 8 | banking nginx 레이트 리미터 설정 배포 | GitHub 2025-09-15 공식 월간 보고 | banking nginx, 게이트웨이 0, G | 버림(원칙 3): 119 에 nginx 로그와 스팬이 없다(rejected 2026-10-10 행과 같은 벽) |
| 9 | commerce cart Redis 캐시 비우기나 설정 변경으로 재적재 폭주 | Shepherd 2026-02-26, Slack 2022-02-22 공식 | commerce Redis, 캐시 1, B | 버림(원칙 9): cart DB 풀이 폴백 부하를 흡수한다(rejected 2026-10-09, 10-10 행과 같은 벽) |

rejected 기록, 장부 §4-1, §5, 이번 실행에서 막힌 목록에 같은 원본(RevenueCat)과 같은 주입(PostgreSQL 시퀀스 되감기)을 inventory_movements 에 건 것이 없다. 같은 원본의 commerce 판으로 막힌 것은 cart_items, order_items 뿐이다(§2 끝).

## 4. 인과 사슬 (코드와 인프라 위치)

1. 엔티티: `commerce/inventory-service/src/main/java/com/commerce/inventory/entity/InventoryMovement.java:13-17` `@Table(name = "inventory_movements", schema = "inventory_schema")`, `@GeneratedValue(strategy = GenerationType.IDENTITY)`. save 가 `insert into inventory_schema.inventory_movements (created_at,movement_type,product_id,quantity,resulting_stock) values (?,?,?,?,?)` 로 id 없이 나가 키를 DB 기본값(시퀀스)에 맡긴다.
2. 스키마: `commerce/db/init-schemas.sql:71-78` `id BIGSERIAL PRIMARY KEY`.
3. 예약: `InventoryService.java:73-96` `reserve`(@Transactional). `findByProductIdForUpdate`, 재고 줄임, `recordMovement`(:87, :129-136 `inventoryMovementRepository.save`)에서 23505, 트랜잭션 되돌림(재고 그대로, outbox 행도 없음). 전환의 읽기 전용 동안에는 첫 문장인 `FOR UPDATE`(Hibernate 가 `FOR NO KEY UPDATE` 로 냄)부터 25006.
4. 오류 처리: `inventory-service/.../config/GlobalExceptionHandler.java:10-21` 은 ServiceException 만 다뤄 DataIntegrityViolationException 과 JpaSystemException 은 Spring 기본 처리로 500 과 'Servlet.service() ... threw exception'(119 수집 수준 SEVERE).
5. 같은 표에 쓰는 배치: `ReconciliationBatch.java:69-118`(1분 주기, @Transactional) ADJUST(:90), RESTOCK(:109). 고장 중 재입고가 필요한 상품이 있으면 배치 전체가 23505 로 되돌려지고 'Unexpected error occurred in scheduled task' ERROR(평시 하루 1,440회 돌고 그중 약 70% 가 재입고 1건 이상).
6. 호출자: `product-service/.../ProductService.java:89-99`(상품 조회 뒤 `inventoryClient.reserve`), `product-service/.../client/InventoryClient.java:38-51`(inventory 5xx 를 같은 상태 ServiceException 으로), `order-service/.../client/ProductClient.java:25-54`(@Retry 3회 200ms 지수 백오프, @CircuitBreaker productClient, fallback 502 'Product service unavailable for product N'), `OrderService.java:157-161`(checkout 의 품목별 예약, 주문 저장과 결제보다 앞).
7. 풀: commerce 서비스들은 Hikari 풀로 DB 에 붙는다(예 `pricing-service/src/main/resources/application.yml:11-16` maximum-pool-size 15, max-lifetime 600000). 세션이 끊기면 풀이 'marked as broken because of SQLSTATE(57P01)' 와 'Failed to validate connection' 을 남기고 새 연결을 연다.
8. 프로브: `commerce/k8s/22-inventory-service.yaml:69-88` startup, readiness, liveness 모두 `/actuator/health`(DB 연결 확인). 시퀀스가 뒤에 있어도 inventory 는 Ready.
9. 인프라: rca-testbed-commerce StatefulSet testbed-postgres(파드 testbed-postgres-0, PostgreSQL 16.14, tb-w1, DB commerce). 109 읽기 전용 조회(2026-10-10~11): 9,685,372행, 최대 키 9,685,846, 시퀀스 last_value 9,685,846(is_called t, cache 1), 기본 키 inventory_movements_pkey 는 id 한 열, 최근 10만 키 모두 있음, 최근 1시간 RESERVE 1,564, RESTOCK 56행, 표 소유자이자 앱 계정 commerce(슈퍼유저), 앱 클라이언트 세션 commerce 계정 JDBC 56개.

### 로컬 실측 (postgres:16 컨테이너, 저장소 inventory-service jar, 2026-10-11)

- `init-schemas.sql` 로 스키마를 만들고 inventory_movements 30만 행(빈틈없는 키), 상품 16개 재고를 넣은 뒤 inventory-service jar(main 빌드)를 그 DB 에 붙였다. 실행기는 이 시나리오의 원격 스크립트를 그대로 쓰고 `kubectl exec` 만 `docker exec` 로 바꿔 돌렸다. 아래 로그 수준은 로컬 콘솔 수준이다(Servlet 줄은 119 에서 SEVERE 로 들어온다).
- 반영판: preflight 0(1.3초). 예약 POST 와 재고 조회 GET 을 0.1초 간격으로 200번 보내는 동안 run 0(8.5초). 예약은 run 앞 31건 200, 그 뒤 169건 모두 500. 재고 조회 GET 은 200번 모두 200.
- 초 단위 로그 순서: 00:36:58 WARN 'HikariPool-1 - Connection ... marked as broken because of SQLSTATE(57P01), ErrorCode(0)', 'SQL Error: 0, SQLState: 57P01', ERROR 'FATAL: terminating connection due to administrator command', WARN 'Failed to validate connection ... (This connection has been closed.)' 8건 → 00:36:58~00:37:04 초마다 약 7건 'SQL Error: 0, SQLState: 25006' + ERROR 'ERROR: cannot execute SELECT FOR NO KEY UPDATE in a read-only transaction' + 'Servlet.service() ... JpaSystemException' → 00:37:04 다시 57P01 과 'Failed to validate connection' 8건 → 00:37:04 부터 초마다 약 7건 'SQL Error: 0, SQLState: 23505' + ERROR 'ERROR: duplicate key value violates unique constraint "inventory_movements_pkey"' 다음 줄 'Detail: Key (id)=(200048) already exists.' + 'Servlet.service() ... DataIntegrityViolationException: could not execute statement [...] [insert into inventory_schema.inventory_movements (created_at,movement_type,product_id,quantity,resulting_stock) values (?,?,?,?,?)]; ... constraint [inventory_movements_pkey]'. 키 값은 200,048 에서 200,171 까지 시도마다 하나씩 커졌다.
- run 뒤 pg_db_role_setting 비어 있음(읽기 전용 해제), 시퀀스 200,171. recovery 1(시퀀스가 뒤라 원래 상태 아님), cleanup 0(1.6초), recovery 0, 두 번째 cleanup 0. 예약 200.
- 읽기 전용이 남은 상태(수동 ALTER DATABASE)에서 recovery 1, cleanup 0(해제와 세션 끊기), recovery 0, 예약 200.
- pg_stat_statements: 23505 로 실패한 INSERT 는 calls 에 들어가지 않는다. 실행기 세션은 track=none 이라 ALTER DATABASE, 세션 끊기, LOCK, setval 문장이 남지 않는다.
- 109 preflight(읽기 전용, 같은 스크립트를 ssh 로) 0(1.4초).

## 5. 원인 규정

| 칸 | 값 | 근거 |
|---|---|---|
| `root_cause.target_id` | `commerce-postgres:inventory_schema.inventory_movements` | 결함을 가진 곳은 이 표의 키 시퀀스 상태(데이터 뒤에 있음)다. 시퀀스는 표의 키 열 기본값으로 딸려 있어 표 단위로 지목한다. inventory, product, order, gateway 의 코드와 요청은 늘 하던 그대로라 정당하다(원칙 6). 표기는 F01-R, F23-P 와 같은 '인스턴스:스키마.테이블' |
| `trigger_target_id` | null | 근본과 계기가 같은 곳(이 DB 의 이전 작업과 그 전환이 남긴 시퀀스 상태). 전환은 DB 쪽 작업이라 앱 서비스를 계기로 지목하지 않는다 |
| `scoring.partial` | commerce-inventory, testbed-inventory:inventory-service, commerce-postgres, commerce-postgres:inventory_schema.inventory, commerce-product | 오류를 낸 서비스(inventory), 인스턴스까지만 짚은 답(전환이 인스턴스 수준이라 가장 가까운 부분 답), 같은 트랜잭션의 이웃 표(재고 행), 중계한 서비스(product) |

원본 사후 보고도 "시퀀스 상태가 복제되지 않아 기본 키 시퀀스가 기존 값과 부딪혔다" 를 원인으로 들었다. 같은 층위(그 표의 키 시퀀스)로 지목한다. 이전 작업의 절차나 누가 했는지는 요구하지 않는다(원칙 5). 오류 문장이 표의 기본 키와 이미 있는 키 값을 직접 말하고, INSERT 에 id 가 없어 키를 시퀀스가 냈음이 보이며, 그 직전 같은 초에 DB 쪽에서 모든 연결이 두 번 끊기고 그 사이 DB 가 쓰기를 거절했다는 것(전환)이 보이므로 관제 데이터로 낼 수 있는 결론이다.

## 6. 감지, 피해 판정, RCA 증거

| 층 | 무엇으로 | 기대 |
|---|---|---|
| 감지 | commerce-gateway, order 의 checkout 오류율(APM 표본), commerce 서비스들의 새 WARN/ERROR 로그(57P01, 25006), inventory 새 ERROR 로그(23505, Servlet SEVERE, 재조정 배치 실패) 급증 | 이상 탐지 이벤트와 인시던트. 같은 checkout 재고 예약 단계 502 를 내는 F01-R, F23-R 은 정식이다 |
| 피해 판정 | 러너: 동반 부하 checkout 5xx 비율 ≥ 0.5 와 checkout 2xx 비율 < 0.3, 3틱 | 평시 5xx 0 근처, 고장 중 약 1.0(재고 부족 409, 쿠폰 400 만 예외) |
| RCA | 계기: 같은 초 DB 쪽 연결 끊김(57P01) → 약 6초 쓰기만 25006(읽기는 됨) → 다시 끊김. 근본: 그 직후부터 이동 이력 INSERT 가 모두 23505(inventory_movements_pkey, 시퀀스가 낸 이미 있는 키, N 이 하나씩 커지고 성공 없음), 같은 표에 쓰는 배치도 실패 | §7, §8 |

## 7. 관측 근거 표 (119 실조회, 평시)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 핵심 증거는 전수 수집되는 로그다.

| 증거 | 표, 칸 | 조회 | 결과 |
|---|---|---|---|
| 계기: 전환(DB 쪽 연결 끊김, 짧은 읽기 전용) | CH `lucida_logs_local`(service_name, body) | `countIf(body LIKE '%57P01%')`, `'%administrator command%'`, `'%read-only transaction%'`, `'%marked as broken%'` by service_name, 60일 commerce-* | 9개 서비스 모두 0, 0, 0, 0(남아 있는 로그 2026-10-03 부터). '25006' 부분 일치는 notification 21, shipping 15, order 3, payment 3 이지만 모두 숫자 일부이고 'read-only transaction' 은 0 |
| 계기 문장이 로그로 남는 경로 | 같은 표, commerce-inventory 7일 | 'SQL Error: 0, SQLState: null' WARN 797건, 'HikariPool-1 - Connection is not available ...' ERROR(2026-10-06 풀 고갈) | Hikari 와 SqlExceptionHelper 의 WARN, ERROR 가 본문 그대로 수집된다. 로컬 실측 전환 문장도 같은 로거(§4) |
| 근본: 새 행이 이미 있는 키를 받음 | 같은 표 | `countIf(body LIKE '%23505%')`, `'%duplicate key%'`, `'%already exists%'`, `'%inventory_movements%'` by service_name, 60일 commerce-* | commerce-inventory 0, 0, 0, 0(38,539건 중). commerce-cart 는 23505 1, duplicate key 2(2026-10-06 장바구니 품목 유일 제약) |
| 근본 문장이 로그로 남는 경로 | 같은 표, commerce-cart 2026-10-06 10:46 | body | WARN 'SQL Error: 0, SQLState: 23505', ERROR 'ERROR: duplicate key value violates unique constraint "cart_items_cart_id_product_id_key"\n  Detail: Key (cart_id, product_id)=(2016, 11) already exists.', SEVERE 'Servlet.service() ... DataIntegrityViolationException ...'. 같은 세 줄 꼴(여러 줄 Detail 포함)이 본문 그대로 수집된다 |
| 근본 문장의 수준 | 같은 표, commerce-inventory 7일 'Servlet.service%' | severity_text | SEVERE 785건(2026-10-06 풀 고갈 때). Servlet 줄 자체는 평시에도 있으므로 '평시 0' 은 23505, duplicate key 문장에만 쓴다 |
| 전파: checkout 완료가 멈춤 | 같은 표, commerce-order INFO 'Cleared cart after checkout%' | 1일 건수, 분 단위 출현 | 41,518건(분당 약 29), 1,440분 중 1,419분. 증상(피해)의 시작을 보이는 것이지 계기의 흔적으로 쓰지 않는다 |
| 전파: 재조정 배치 | 같은 표, inventory INFO | 'Reconciliation batch finished ...' 하루 1,440회(재입고 1건 이상 약 70%) | 고장 중 재입고가 필요한 분마다 'Unexpected error occurred in scheduled task' 로 바뀐다(평시 7일 7건, 모두 풀 시간 초과 때) |
| 보조: 실패한 예약의 되돌림 | VM `dpm.postgresql.sql.rollback_count{db_name="commerce"}` | 7일 평균, p99, 최대 | 3.91, 6.93, 11.24. 오르지만 평시 폭이 있어 결정 증거로 쓰지 않는다 |
| 보조: 실패한 INSERT 는 Top SQL 에서 사라짐 | CH `dpm_topsql_local`(engine postgresql, log_attributes sql_id 9203184288234713255) | 7일 출현 수와 간격 | 3,052회, 간격 최대 4,767초(79분). 평시에도 비므로 결정 증거로 쓰지 않는다 |
| 주입 문장 자체 | CH `dpm_topsql_local` | F23-P 시트 실측: Top SQL 은 앞선 폴에 본 문장의 증분만 낸다 | 실행기 세션이 track=none 이라 ALTER DATABASE, pg_terminate_backend, LOCK, setval 은 남지 않는다 |
| 배제: 롤아웃, 재시작 | CH `kcm_events_local` | 7일 행 수 | 6,898건 수집 중. 주입 구간에 rca-testbed-commerce 변경이 없다는 것은 다른 원인을 배제하는 데만 쓴다 |
| 배제: 잠금(F01-R) | VM `dpm.postgresql.session.blocked_session` | 7일 최대, 간격 | 최대 1, 60초 간격. 전환의 표 잠금은 1초 미만이라 표본에 거의 걸리지 않고 그 뒤 차단이 없다 |

계기의 흔적: 계기는 DB 이전의 전환이고, 원본처럼 옛 쪽을 읽기 전용으로 두고 연결을 새 쪽으로 넘기는 것이다. 이것은 전수 로그에 세 단계로 남는다: 같은 초에 DB 를 쓰는 commerce 서비스들의 풀이 DB 쪽에서 끊긴 연결(57P01 'terminating connection due to administrator command')을 기록하고, 약 6초 동안 모든 쓰기가 25006 'cannot execute ... in a read-only transaction' 으로 실패하며 읽기는 성공하고(DB 가 읽기 전용), 다시 한 번 같은 끊김이 기록된다. 네 문장 모두 평시 0건이다. 이어서 근본의 흔적이 시작된다: 두 번째 끊김 직후부터 이동 이력 INSERT(id 없음)가 모두 'inventory_movements_pkey 에 이미 있는 행 (id)=(N)' 으로 실패하고 N 이 하나씩 커지며 성공이 없다. 곧 전환을 지나 다시 붙은 쪽의 시퀀스가 데이터 뒤에 있다. 직전까지 성공하던 키 값 자체는 119 에 남지 않는다(이동 이력 id 를 로그에 쓰지 않고 DPM 은 시퀀스를 수집하지 않음). 'checkout 완료 로그가 끊김' 과 '다른 변경 없음' 은 계기의 근거로 쓰지 않는다(앞의 것은 증상 시작, 뒤의 것은 배제용). 인공 지연은 없다(유지는 DB 가 쓰기를 거절하는 상태 자체다).

원칙 8 참고: 실행기는 F23-P, F33-P 처럼 파드 안 psql(앱 계정, pg_stat_statements.track=none)로 접속한다. 앱 세션을 끊는 것은 DB 의 세션 종료이고 로그에는 PostgreSQL 이 내는 표준 문장(57P01)만 남는다. 시나리오 id 와 실행 번호는 남지 않는다(테스트로 고정).

## 8. 감별

- must_support: 같은 초 commerce 서비스들의 Hikari 'marked as broken because of SQLSTATE(57P01)', 'FATAL: terminating connection due to administrator command', 이어서 약 6초 쓰기마다 25006 'cannot execute ... in a read-only transaction'(읽기 200), 다시 57P01. 그 직후부터 inventory WARN 'SQL Error: 0, SQLState: 23505' + ERROR 'duplicate key value violates unique constraint "inventory_movements_pkey"' + 'Detail: Key (id)=(N) already exists.' + Servlet SEVERE(insert into inventory_schema.inventory_movements, id 없음)가 예약마다, N 이 하나씩 커지고 성공 없음. 같은 표에 쓰는 재조정 배치 실패. checkout 502 'Product service unavailable for product N', 동반 부하 checkout 5xx ≥ 0.5, 'Cleared cart after checkout' 과 commerce 정산 이체 멈춤. 전환 뒤 둘러보기, 재고 조회, 장바구니, 가격 200.
- must_rule_out: F01-R(재고 행 잠금: 기다림 뒤 시간 초과, 풀 고갈, 차단 세션), F23-P(상품 표 지움: product 조회 42P01, 둘러보기 500), F23-R(재입고 배치 정지: 409 재고 부족), F09-P(inventory CPU 한도: 느려짐과 시간 초과), inventory 새 버전과 설정 배포, 배포나 재시작(KCM 변경 없음, 끊김은 DB 쪽 세션 종료), PostgreSQL 다운이나 과부하(F25-H, F33-P), F38-R 꼴 DB 전체 읽기 전용(25006 은 6초뿐, 그 뒤 쓰기 됨), F57-R(같은 원인 꼴의 banking Oracle 판).
- contrast_with: F57-R, F23-P, F01-R, F23-R, F09-P(정답지 `related_scenarios`).

가르는 관측 근거 한 줄: DB 쪽에서 연결이 두 번 끊기고 그 사이 약 6초 쓰기만 25006 으로 거절된 직후부터, inventory 의 이동 이력 INSERT(id 없음)가 모두 inventory_movements_pkey 23505 'Key (id)=(N) already exists'(N 이 하나씩 커지고 성공 없음)로 실패하고, 예약이 기다리지 않고 즉시 500 이며 같은 표의 읽기, 상품 조회, 다른 표, 모든 파드와 세션이 정상이다.

## 9. 러너 판정 조건과 강도, 부하

- 주입: db.ddl(PostgreSQL 시퀀스 되감기 모드) 1단 고정(`approved-fixed-f57-p`, rewind_rows 100000, read_only_seconds 6). run 은 러너 coordinator lock 을 쥔 채 돌고 lease 는 30초다. run 은 확인 질의와 전환 호출 약 15번(각 약 0.4초), 읽기 전용 유지 6초(셸 sleep 하나), 표 잠금 대기 최대 2초라 로컬 8.5초, 최악도 약 14초다. 잠금을 2초 안에 못 잡거나 키 구간이 맞지 않으면 시퀀스를 바꾸지 않고, 읽기 전용을 풀고 세션을 끊은 뒤 실패한다. 이 구조(sleep 하나, SQL 안 대기 없음, 전환 호출 하나, 실패 경로의 되돌림, 통계 리셋과 ANALYZE, VACUUM 없음)는 단위 테스트가 고정한다. 강도 축이 없다: 시퀀스가 꽉 찬 키 구간에 있는 동안 모든 INSERT 가 요청량과 무관하게 실패한다.
- 창 계산: 실패한 시도마다 키 하나를 쓴다. 동반 부하 checkout 약 1.5/s 가 product 재시도(최대 3회)를 거치되 서킷이 열리면(5초) 부르지 않고, 기준선 checkout 과 재조정 배치를 더해도 초당 5개 안팎, 25분이면 약 7천~1만 개로 확인한 9.9만 칸 안이다.
- 동반 부하: load.north_south `commerce surge.js` 3rps(여정 절반이 checkout), ramp 2m, hold 21m, entry 30080(F23-P 와 같은 스크립트와 세기). 부하 상한 180 의 2%.
- 피해 계산(원칙 9): 실패가 시간 초과가 아니라 즉시 나는 SQL 오류라 서킷브레이커는 실패를 빠르게 만들 뿐이다(order productClient 서킷 열림 → 502). 상주 commerce 부하의 checkout 도 모두 실패한다. 전환의 6초 쓰기 실패와 연결 끊김은 commerce 전체에 짧게 나고 진입점(gateway, nginx)은 DB 를 쓰지 않아 entry_status 가 0 이 되지 않는다.
- success: checkout_5xx_rate ≥ 0.5 와 checkout_2xx_rate < 0.3, 3틱. min_hold 15m, timeout 20m.
- must_rule_out: achieved_rps < 1, commerce PostgreSQL 파드 NotReady, inventory 파드 NotReady(창 안 재시작 포함).
- abort: entry_status == 0(nginx, gateway 나 노드가 죽음).
- recovery: target_health 200, inventory Ready, PostgreSQL Ready, 기준선 쓰기 실패율 < 0.1, 2틱, 10m.
- cleanup: 읽기 전용 기본값이 남았으면 `ALTER DATABASE ... RESET default_transaction_read_only` 와 세션 끊기, 시퀀스가 최대 키 뒤면 `SELECT setval('inventory_schema.inventory_movements_id_seq', (SELECT max(id) FROM inventory_schema.inventory_movements))`(원본의 해결), 그 뒤 원래 상태(시퀀스 앞, 읽기 전용 없음, 계약 기본 키) 확인, 부하 종료. 이미 원래 상태면 아무것도 하지 않는다. 상태 파일이 필요 없다. cleanup timeout 5m.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(RevenueCat 2022-11-23, 공식 엔지니어링 블로그)와 요소별 대응표, 기전과 엔진이 원본과 같다 (§2). 새 인스턴스와 행 복사, 복제는 만들지 않고 전환(옛 쪽 읽기 전용, 짧은 쓰기 실패, 연결 넘김)과 그 결과(시퀀스만 스냅숏 시점)를 만든다. 실제 전환에 없는 흔적(통계 리셋, 대기 문장, 트리거)은 만들지 않는다
- [x] 분류 장부를 갱신했고 묶음 Q(1→2, 2.4%), 정답 위치 DB 테이블(재고 이동 이력)(0→1), 결제 경로 12/82(14.6%), 서비스 쇼핑몰 27→28(세 도메인 동수, 정답 위치 0)과 고른 이유를 적었다. 어느 축도 20% 미만 (§3)
- [x] 근본 원인 위치가 인프라 지점(inventory_schema.inventory_movements, 시퀀스 inventory_movements_id_seq, 기본 키 inventory_movements_pkey, 109 사전 조회)과 `file:line`(InventoryMovement.java:13-17, init-schemas.sql:71-78, InventoryService.java:73-96, 129-136)으로 확인됐다 (§4)
- [x] 근본 원인의 흔적이 119 에 남는 경로를 조회했다: lucida_logs_local 의 SqlExceptionHelper WARN + 여러 줄 Detail ERROR + Servlet SEVERE 세 줄 꼴(같은 꼴의 cart 23505 수집), 23505, duplicate key 문장 평시 0건 (§7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: 로그 전수 (§7)
- [x] 계기 흔적: 전환이 전수 로그에 세 단계로 남는다(같은 초 DB 쪽 연결 끊김 57P01, 약 6초 쓰기만 25006, 다시 끊김. 네 문장 평시 0건). 증상 시작 시각과 '다른 변경 없음' 은 계기 근거로 쓰지 않고 배제에만 쓴다. 인공 지연 없음 (§7)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다: "commerce PostgreSQL inventory_movements 의 키 시퀀스가 전환 뒤 이미 쓰인 키를 내준다" 는 전환 문장과 오류 문장에서 바로 나온다 (§5)
- [x] 정답지 세 칸이 원칙 6대로 채워졌다 (§5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다 (§6)
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: 원격 스크립트와 argv 에 id 없음, 모든 SQL 이 track=none 세션, 로그에는 PostgreSQL 표준 문장만 (§7)
- [x] 부하 상한과 서킷브레이커를 고려해 피해가 실제로 날 계산이 있다: 즉시 실패라 요청량과 무관하게 100%, 창 1만 이하 대 9.9만 (§9)
