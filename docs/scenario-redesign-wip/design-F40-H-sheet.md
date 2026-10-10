---
title: F40-H 설계 시트 (banking transfer-service 를 원 단위 정수 금액 검증을 더한 릴리스로 롤아웃)
status: Draft
owner: project
last_reviewed: 2026-10-10
tags:
  - scenario
  - design
  - release
summary: banking transfer-service 를 결함 있는 새 릴리스(core-banking-transfer:2.3.0, fault-images/f40-h 패치로 만든 별도 태그)로 롤아웃하면, 요청 검증 강화로 더한 '원화 금액은 원 단위 정수만' 확인이 값이 아니라 소수 표기 자릿수로 판정해, 금액을 27000.00 꼴로 보내는 commerce 정산 이체를 모두 400 으로 거절하고 commerce checkout 이 전부 502 가 되는 시나리오. banking 자체 이체(정수 금액)는 계속 완료된다. 원본은 Flagsmith 2024-01-18(릴리스의 요청 검증이 일부 클라이언트의 정당한 요청을 거절)과 Buttondown 2026-06-14(스키마 강화가 자사 화면이 보내던 필드를 거절). F40-R 과 같은 증상, 다른 원인(H).
---

# F40-H 설계 시트

## 1. 요약

banking transfer-service 의 새 릴리스 2.3.0 은 요청 검증을 강화했다. 원화에는 보조 단위가 없으니 이체 금액은 원 단위 정수여야 한다는 확인을 `TransferService.execute` 에 더했는데, 새 `KrwAmountPolicy.isWholeWon` 이 금액의 값이 아니라 BigDecimal 의 표기 자릿수(`amount.scale() <= 0`)로 판정한다. 릴리스의 시험은 정수 금액과 진짜 소수(1000.5)만 다룬다. banking 자기 호출자(api → account → transfer)는 금액을 정수로 보내 그대로 통과하지만, commerce payment-service 는 정산 이체 금액을 checkout 총액 그대로 소수 둘째 자리까지 적어(27000.00) 보내므로 정산이 하나도 빠짐없이 400 'amount must be in whole won: 27000.00' 으로 거절된다. commerce payment 가 거절을 502 로 바꾸고 결제를 되돌려 checkout 이 전부 502 가 된다. transfer 는 Ready 이고 빠르며, Oracle 도 평시다.

비유: 은행이 "원 단위로만 받습니다" 라는 새 창구 규칙을 붙였는데, 담당자가 금액의 값이 아니라 "소수점이 적혀 있는지" 를 본다. 늘 "27,000.00원" 이라고 적어 내던 거래처(commerce)의 서류만 모두 반려되고, "27,000원" 이라고 쓰는 일반 고객 서류는 통과한다. 고칠 곳은 거래처가 아니라 새 창구 규칙(릴리스)이다.

## 2. 원본 사례

- **Flagsmith, 2024-01-18** (공식 상태 페이지 사고와 사후 보고): https://status.flagsmith.com/incidents/0t2jlh80q0zt
  - 그날 앞선 릴리스가 숫자 identity 식별자 요청에만 해당하는 검증을 더했고, 그 검증 문제를 고치려고 약 13:45(사후 보고 시각, 시간대 표기 없음) 배포한 변경이 traits 키가 있어야 한다고 요구했다. 일부 클라이언트는 traits 가 비면 그 키를 빼고 보내는데(사후 보고는 빈 traits 목록을 빼는 Go 클라이언트를 듦), 그래서 traits 가 없는 identity 의 **유효한 요청이 잘못 거절**됐다.
  - 감시 경보와 영향받은 고객이 알렸다. 14:54 일부만 고친 수정, 15:06 영향 지역 **롤백**, 15:48 시험을 더한 영구 수정. 상태 갱신(UTC) 14:40 조사 시작, 15:50 해결.
  - 재발 방지: 더 세분된 오류율로 자동 롤백, SDK 마다 새 변경과의 호환을 확인하는 종단 시험.
- **Buttondown, 2026-06-14** (공식 블로그 사후 보고): https://buttondown.com/blog/incident-0029
  - 6월 13일(UTC) 기능은 그대로 두고 계약을 엄격하게 하려고 **선언되지 않은 필드가 든 API 요청을 거절하는 스키마 강화**를 내보냈다. 자사 화면의 수동 구독자 추가 양식이 클라이언트 전용 필드 active 를 함께 보내고 있어 그 요청이 422 'extra inputs are not permitted' 로 실패했다. 약 35시간 동안 454건 실패, 다른 경로는 무영향.
  - 시험의 mock 이 실제 스키마가 거절하는 필드를 받아들여 코드 리뷰와 CI 가 놓쳤다. 고객 지원 문의로 드러났고(422 로깅이 거칠어 적은 양의 경로를 못 잡음), 명시 필드 목록으로 요청을 만드는 수정으로 끝냈다.
- 두 사례 모두 자료 문서 M8 에 추가했다(출처가 말한 사실만).

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 서버 쪽 새 릴리스(요청 검증, 스키마 강화) 배포 | transfer-service 를 릴리스 core-banking-transfer:2.3.0 으로 롤아웃(KCM ScalingReplicaSet, 새 파드 Pulled 이벤트에 태그가 남음) |
| 원인이 된 결함 | 새 검증이 요청 형식의 기대를 좁혀, 예전부터 받아들이던 정당한 클라이언트의 한 변형(빈 traits 키 생략, 추가 필드)을 거절. 시험(mock, 테스트 케이스)이 그 변형을 다루지 않음 | 새 원 단위 금액 검증이 값이 아니라 표기 자릿수로 판정해, 예전부터 받던 commerce 정산의 소수 표기(27000.00)를 거절. 릴리스 시험은 정수와 1000.5 만 다룸 |
| 전파 경로 | 거절된 요청을 보낸 클라이언트 기능(identity 생성, 구독자 추가)이 실패. 다른 클라이언트와 경로는 정상 | transfer 400 → commerce payment BankingTransferClient 502, 결제 롤백 → order 'Checkout payment failed' 502 → gateway 502. banking 자체 이체(정수)는 정상 |
| 사용자 증상 | 해당 클라이언트 요청 오류(Go SDK, 수동 구독자 추가 422) | commerce checkout 전부 502(PG 승인 뒤), 매시 정산 실패. banking 손님은 무영향 |
| 원본의 탐지 경로 | 감시 경보(오류율)와 고객 신고 / 고객 지원 문의 | lucida-next 오류율(order, gateway, payment 5xx), 오류 로그 급증, KCM 롤아웃 이벤트, transfer 거절 로그 |
| 완화와 복구 | 영향 지역 롤백, 시험을 더한 수정 / 요청 필드 목록 수정 | cleanup 이 이미지를 매니페스트 이미지로 되돌림(롤백) |

기전 일치: 원인(서버의 새 버전이 요청 형식 기대를 좁힘)에서 증상(정당한 호출자 한 부류의 요청만 4xx 로 거절되어 그 기능이 실패)까지 고리가 같다. 거절 기준은 원본마다 다르므로(필수 키, 추가 필드) 우리 계약에서 실제로 호출자마다 다른 형식 변형, 곧 금액 표기 자릿수(119 7일 실측: commerce 정산 290,166건 전부 소수 표기, 그 밖 222,531건 전부 정수)를 고른다.

바꾼 것(기전 고리 밖):
- 거절 코드: 원본은 422 등이고 우리 transfer 의 검증 거절은 기존 관례대로 400 이다. commerce payment 가 모든 RestClientException 을 502 로 바꾸는 것은 원래 코드다(F40-R 과 같은 길).
- 롤아웃 공백: transfer 는 매니페스트 전략이 maxSurge 0 이라 새 파드가 Ready 가 될 때까지 약 1분 모든 이체가 실패한다. 원본에는 없는 짧은 구간이고 계기(롤아웃)와 같은 곳을 가리켜 정답을 흐리지 않는다. 판정은 min_hold 12m 뒤라 공백만으로 성공하지 않는다.

## 3. 숫자 근거 (2026-10-10, `scenario-stats.py`, 정식 + 후보 합계 61, 이 후보 전)

- 서비스: 쇼핑몰 26, 은행 18, 음식배달 17. 이 후보로 은행 19.
- 묶음: A 7, B 7, D 7, G 7, J 7(각 11%), C 6, **L 5**(8%), F 3, P 3, E 2, H 2, K 2, I 1, M 1, O 1, N 0. 이 후보는 L+J 로 L 6(9.7%), J 로 세도 8(12.9%). 은행에는 L 이 아직 없다(L 5개가 모두 음식배달).
- 정답 위치: 주문 서비스 7, 외부 결제 의존 6, 노드 5, **은행 이체 서비스 5**(8%) → 6(9.7%). 결제 경로 합계 10/62(16.1%) 그대로.
- 0 부품: commerce kafka, notification, gateway, nginx / banking kafka, nginx / food kafka, notify. 인프라 층 전부.
- 왜 이 후보인가: 현실 트리거 1위인 새 버전 배포(Google 37%)와 버그 원인 2위 데이터 형식 불일치(Liu 21%, 하위 호환성 문제 14.6%)가 겹치는 꼴이고, 원본이 공식 사후 보고 둘이다. 계기 흔적(이미지 태그), 근본 흔적(새 거절 로그, 400 스팬), 피해(commerce 오류 로그와 5xx)가 모두 전수 로그와 KCM 에 남는다(§7). 같은 증상의 F40-R(정답 pricing)과 짝이 되어, 관제 AI 가 "은행 400 이면 가격" 같은 외운 답 대신 거절 문장과 롤아웃 위치로 가려야 한다(카탈로그 관계 H).
- 음식배달(17)이 더 적지만 고르지 않은 이유는 §3 후보 목록에 적었다(이번 반복에서 food 후보가 원칙 1, 7, 9 와 같은 주입 중복으로 막힘).

### 후보 목록 (3단계, 기존 목록이 아니라 기전 × 부품에서 시작, 숫자 순)

| # | 후보 | 층, 부품 | 원본 사례 | 결과 |
|---|---|---|---|---|
| 1 | commerce orders 표에 열을 더하고 CONCURRENTLY 없이 인덱스를 만드는 마이그레이션이 표 전체를 막음 | DB, commerce 주문 표(정답 위치 0) | Railway 2025-10-28 공식 https://blog.railway.com/p/incident-report-oct-28th-2025 | 버림: 원칙 9. 로컬 PostgreSQL 16(0.5 CPU, 512Mi, orders 꼴 464만 행 623MB) 실측으로 같은 트랜잭션의 ADD COLUMN + CREATE INDEX 가 6.6초, 기존 열 인덱스도 15.7~20.9초뿐이다. 원본의 30분 차단은 약 10억 행에서 나왔다 |
| 2 | commerce gateway 의 인가 경로 성능 변경이 메모리를 새어 반복 크래시 | 앱, commerce gateway(정답 위치 0) | GitHub 2023-11-03 공식 월간 보고 | 버림: 원칙 1, 9. 원본에서 샌 것은 별도 인가 서비스(우리 user-service, 쇼핑몰 최다 서비스인데 정답 위치 1)이고, gateway 로 옮기면 누수 위치가 바뀐다. 쓰기 검증 토큰이 데모 사용자 20명분이라 요청 수에 비례해 새려면 요청당 1KB 넘게 붙잡아야 해 계산이 서지 않음 |
| 3 | food payment → 외부 결제 mock 송신을 노드 방화벽 변경이 막음 | 노드 tb-w3 | Ex Libris Alma 2019-07-09 RCA 공식 | 버림: 원칙 1, 같은 주입. mock 이 같은 노드 tb-w3 의 파드라 '밖으로 나가는 트래픽 차단' 이 아니라 F44-R 과 같은 tb-w3 FORWARD 차단의 포트 교체가 된다 |
| 4 | food dispatch Service 셀렉터에 걸리는 시험용 Deployment 가 요청 일부를 떨굼 | K8s Service, food dispatch | GitHub 2025-01-13 공식 월간 보고 | 버림: 원칙 1, 9. 원본이 '트래픽 라우팅과 시험 관련 설정 변경으로 내부 LB 가 요청을 떨굼' 까지만 밝혀 대응표를 채울 수 없고, keep-alive 재사용과 order 재시도 3회로 실패율이 정해지지 않으며 새 실행기가 필요 |
| 5 | food 메뉴 일괄 갱신이 available 을 빈 값으로 넣어 널 경로로 메뉴 조회 실패 | 데이터, food restaurant | Google Cloud 2025-06-12 공식(F40-R 원본) | 버림: 원칙 1. `RestaurantService.toMenuResponse` 가 `Boolean.TRUE.equals(...)` 라 널이 예외가 아니라 매진(false)이 되고 주문은 400 으로 끝난다(앞선 Clerk 반려와 같은 벽) |
| 6 | 새 릴리스가 메시지마다 Kafka 프로듀서를 새로 만들어 브로커 메모리 고갈 | food Kafka(정답 위치 0) | PagerDuty 2023-08-28 공식 블로그 | 버림: 원칙 7. 세 도메인의 Kafka 발행은 outbox 릴레이 비동기뿐이라 사용자 경로 증상이 없다(원본 피해도 알림 지연) |
| 7 | 조회 경로가 공유 행에 '마지막 조회 시각' 을 써 부하 때 잠금 경합 | food | Nango 2025-10-06 공식 상태 페이지 | 버림: 원칙 1. 원본 계기는 배포가 아니라 부하 급증이고, 테스트베드에는 조회 경로가 공유 행을 쓰는 코드가 없어 패치로 넣으면 기전이 바뀐다 |
| 8 | 자격 증명 회전이 새 값을 다른 환경에 배포하고 옛 값을 지움 | DB 계정 | Cloudflare R2 2025-03-21(웹 확인 생략, 막힌 이유가 원본과 무관) | 버림: 같은 주입, 컨트롤러 필수 중단 조건, 원칙 2. banking 은 F35-R(db.account)과 같은 주입, food 는 네 서비스가 한 계정이라 order readiness 가 빠져 entry 0, commerce 는 최다 서비스인데 DB 계정 정답 위치 1 |
| 9 | **banking transfer 릴리스의 요청 검증 강화가 commerce 정산의 소수 표기 금액을 거절** | 앱, banking transfer(L+J) | Flagsmith 2024-01-18, Buttondown 2026-06-14 공식 | **채택** |

## 4. 인과 사슬 (코드와 인프라 위치)

1. 계기와 결함: `scripts/scenarios/fault-images/f40-h/transfer-service.patch:19-21` (`KrwAmountPolicy.isWholeWon` — `amount.scale() <= 0`), `:31-36` (`TransferService.execute` — 양수 확인 뒤 isWholeWon 이 거짓이면 INFO 'Transfer rejected (amount not in whole won): from=... to=... amount=...' 를 남기고 400 'amount must be in whole won: <amount>').
2. 매니페스트 버전: `core-banking/transfer-service/src/main/java/com/corebanking/transfer/service/TransferService.java:52-60` (금액은 양수인지만 확인).
3. 호출자별 형식: commerce `commerce/payment-service/src/main/java/com/commerce/payment/client/BankingTransferClient.java:37-58` 가 checkout 총액 BigDecimal(소수 둘째 자리)을 그대로 보내고 4xx 를 포함한 RestClientException 을 502 'core-banking transfer call failed' 로 바꾼다. banking 쪽은 loadgen 정수 금액을 api, account 가 그대로 넘긴다. 로컬 Jackson 실측(Spring Jackson2ObjectMapperBuilder): JSON `27000.00` → BigDecimal scale 2, `23807` → scale 0, BigDecimal 직렬화 `27000.00`.
4. commerce 전파: `commerce/payment-service/src/main/java/com/commerce/payment/service/PaymentService.java:53-80` (PG 승인 뒤 정산 이체, 실패 시 @Transactional 롤백) → `commerce/order-service/src/main/java/com/commerce/order/service/OrderService.java:189-196` ('Checkout payment failed, releasing reserved stock', 재고 반환, 502). order 의 paymentClient 서킷브레이커(`commerce/order-service/src/main/resources/application.yml:91-`)와 재시도 2회.
5. 매시 정산: `commerce/payment-service/src/main/java/com/commerce/payment/service/SettlementBatch.java:42-80` ('Settlement banking transfer failed, unsettled payments carry over to next cycle').
6. 인프라: `core-banking/k8s/22-transfer-service.yaml` replicas 1 :9, maxUnavailable 1 / maxSurge 0 :17-18, nodeSelector tb-w2 :27-28, image core-banking-transfer:latest imagePullPolicy Never :36-37. 결함 이미지는 109 docker 에만 두고 실행기가 주입 직전에 tb-w2 containerd 에 올린다(`fault-images/f40-h/image.json`).

로컬 검증: origin/main core-banking 에 패치를 얹어 `./mvnw -pl transfer-service -am test` 통과(릴리스 자신의 KrwAmountPolicyTest 2건, 기존 TransferRetentionTest 5건). 같은 사본에 임시로 넣은 형식 확인 시험(패치에는 없음)으로 commerce 형식 거절, banking 형식 통과를 확인했다.

## 5. 원인 규정 (원칙 5, 6)

- 근본(`root_cause.target_id`): core-banking-transfer(새 릴리스 2.3.0). 요청 형식 기대를 좁힌 검증을 들여온 곳이고, 되돌려야 재발이 멈춘다. 원본 둘도 서버 쪽 릴리스를 원인으로 짚고 롤백, 수정으로 끝냈다(같은 층위).
- 계기(`trigger_target_id`): 비움. 계기도 같은 transfer 롤아웃이다.
- 부분 점수: commerce-payment(400 을 502 로 바꾼 증상 위치), commerce-order(checkout 502 를 낸 증상 위치).
- 원칙 6: commerce 의 정산 요청은 평시와 같은 정당한 요청이고 매니페스트 transfer 는 같은 요청을 COMPLETED 로 처리해 왔다. 잘못된 것은 B(transfer)의 새 로직이다.
- 원칙 5: 정답은 "transfer 의 새 버전이 commerce 정산 요청을 거절하기 시작했다" 로, 롤아웃 이벤트, 거절 로그, 400 스팬, 은행 자체 이체의 정상 완료로 관제 데이터에서 바로 나온다. 패치 속 줄(scale 비교)을 맞히라고 요구하지 않는다.

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 | 내용 |
|---|---|---|
| 감지 | lucida-next 이상 탐지 | commerce order, gateway, payment 서버 5xx 오류율(checkout 전부), order ERROR 'Checkout payment failed' 급증. F40-R 녹화 대기 중이라 같은 길의 정식 녹화는 아직 없지만 F01-P, F17-R, F35-R(같은 정산 실패 → checkout 502 꼴, 정식)이 인시던트를 만들었다 |
| 피해 판정 | 러너 | 동반 부하 checkout 5xx 비율 ≥ 0.5, commerce-order 오류 스팬 ≥ 10(3틱) |
| RCA | 녹화 데이터 | KCM 롤아웃(2.3.0), transfer 거절 로그와 400 스팬, 정산 COMPLETED 끊김과 banking 자체 COMPLETED 지속, commerce 오류 본문의 은행 문장 |

## 7. 관측 근거 표 (119 실조회, 2026-10-10 03:30~04:10 UTC)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 핵심 증거는 전수 로그와 KCM 이다.

| 증거 | 119 표, 칸 | 실제 조회 | 평시 결과 |
|---|---|---|---|
| 계기: transfer 롤아웃 | `kcm_events_local` (namespace, object_name, reason, body) | `SELECT reason, substring(body,1,160), max(timestamp) FROM lucida.kcm_events_local WHERE namespace='rca-testbed-banking' AND object_name LIKE 'testbed-transfer%' AND reason IN ('Pulled','ScalingReplicaSet') GROUP BY 1,2` | 마지막 롤아웃 2026-10-01 17:31, 'Pulled: Container image "core-banking-transfer:latest" already present on machine', 'Scaled up replica set testbed-transfer-74b498b67b to 1'. 30일 기록 모두 :latest. 주입 때 2.3.0 이 처음 나타난다 |
| 근본: 호출자별 금액 표기 | `lucida_logs_local` (service_name='core-banking-transfer', body) | `SELECT countIf(body LIKE '%from=commerce-settlement%' AND match(body,'amount=[0-9]+\\.[0-9]+')), countIf(body LIKE '%from=commerce-settlement%' AND NOT match(...)), countIf(body NOT LIKE '%from=commerce-settlement%' AND match(...)), countIf(body NOT LIKE ... AND NOT match(...)) ... INTERVAL 7 DAY AND body LIKE 'Transfer %: ref=%'` | 정산 소수 표기 290,166 / 정산 정수 0 / 그 밖 소수 0 / 그 밖 정수 222,531 (2026-10-03 03:59 ~ 10-10 03:59). 새 검증이 정산만 전부 거절하고 나머지는 전부 통과시킨다 |
| 근본: 새 거절 문장 | `lucida_logs_local` (body) | `SELECT countIf(body LIKE '%whole won%') FROM lucida.lucida_logs_local WHERE timestamp > now() - INTERVAL 30 DAY` | 0건. account 의 'Transfer rejected: insufficient balance ...'(30일 4,834건)는 다른 서비스, 다른 문장이다 |
| 전파: 은행 400 | `otel_traces_local` (service_name, span_name, span_kind, status_code, span_attributes['http.response.status_code']) | 7일 SERVER `POST /api/transfers` 집계 | 23,476건, 응답 코드 200, 500 만(500 6건), 400 0건. 주입 뒤 정산 요청 표본이 400 |
| 전파: commerce 오류 본문 | `lucida_logs_local` (service_name='commerce-order', body) | `SELECT substring(body,1,600), count() ... WHERE body LIKE '%core-banking transfer call failed%' INTERVAL 30 DAY` | 다른 시나리오 실행 때만 있고 본문 전체가 실린다: 'Checkout payment failed, releasing reserved stock: userId=7, Payment service unavailable: Payment service error: 502 : "{...core-banking transfer call failed: I/O error ... Connection refused...}"'. 주입 때는 같은 자리에 '400 ... amount must be in whole won: ...' 이 실린다 |
| 전파: 매시 정산 | `lucida_logs_local` (service_name='commerce-payment') | `countIf(service_name='commerce-payment' AND body LIKE 'Settlement batch finished%')` 30일 | 168건(시간당 1건). 주입 구간의 정각에 'Settlement banking transfer failed' |
| 증상 | `otel_traces_local` | 7일 commerce-order SERVER `POST /api/orders/checkout` | 13,288건, 502 는 다른 시나리오 실행 구간뿐 |

## 8. 감별

- must_support: 정답지 5항목(롤아웃 2.3.0, 정산 거절 로그와 400, banking 자체 정수 이체 지속, commerce 오류 본문과 502, 경로 전체 Ready·평시).
- must_rule_out: F40-R(거절 문장 'amount must be positive', pricing 재시작, transfer 롤아웃 없음), transfer 도달 불가(F17-R, F17-H, F51-R 꼴, F44-P: 연결 거부나 시간 초과), banking DB 장애(F01-P, F35-R, F42-R: 대기, ORA, Oracle CPU), 외부 결제와 payment 자원(F01-H, F06-R, F05-R), commerce 쪽 변경(F08-P).
- contrast_with: F40-R(같은 증상, 다른 원인), F49-R, F49-H(응답 형식을 깬 릴리스), F42-R, F17-H(같은 transfer 의 다른 릴리스), F17-R.
- F40-R 과 가르는 관측 근거: 거절 문장(whole won 대 positive), 금액(양수 NNNNN.00 대 0), 롤아웃 위치(banking testbed-transfer 대 commerce testbed-pricing), pricing 'Promotion cache refresh' 활성 수 변화 유무.

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

- 강도: 고정 1단(approved-fixed-f40-h, release 모드, fault_image core-banking-transfer:2.3.0). 강도에 따라 달라지는 값이 없다: 새 버전이 뜬 뒤 정산 이체는 한 건도 통과하지 못한다(7일 정산 전부 소수 표기).
- 부하: north-south 6rps commerce surge.js(F40-R 과 같은 값, 여정의 약 50% 가 checkout → 초당 약 3건). 180rps 상한에 한참 못 미친다. 6rps 는 실패 전 구간(램프 2분, 롤아웃 약 1분)의 재고 소비를 재입고 배치(1분마다 20 미만 상품을 60 으로)가 따라가게 고른 F40-R 의 값이다. 고장 중 실패한 checkout 은 예약 재고를 돌려준다.
- 서킷브레이커: order 의 paymentClient 서킷(10건 중 50%, 5초 열림, 반열림 3회)이 열리면 checkout 이 'CircuitBreaker paymentClient is OPEN' 으로 빨리 502 가 된다. 반열림 시도마다 전체 경로를 다시 타며 다시 열린다. 어느 쪽이든 5xx 라 checkout 5xx 비율은 1 에 가깝다.
- 타임아웃 사슬: 거절이 즉시 400 이라 order, payment 의 풀이나 스레드가 묶이지 않는다(order readiness, 진입점 영향 없음, entry_status 0 이 나올 경로 없음).
- 성공: checkout_5xx_rate ≥ 0.5 그리고 commerce-order 오류 스팬 ≥ 10, 3틱 연속, min_hold 12m 뒤(롤아웃 공백만으로 판정하지 않음), timeout 20m.
- 배제: achieved_rps < 3(부하 미전달), commerce payment NotReady(payment 장애), transfer 재시작 ≥ 2(기동 실패나 크래시 루프 같은 다른 결함). transfer 준비 상태는 롤아웃 공백 동안 false 라 배제에 쓰지 않는다(F42-R 과 같음).
- 회복: target_health 200, transfer Ready, available 1, commerce-order 오류 스팬 < 5, 2틱, 10분.
- 중단: entry_status == 0(commerce 진입점 연결 불가), 2틱.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Flagsmith 2024-01-18, Buttondown 2026-06-14, 공식)와 요소별 대응표가 있고, 기전(서버 새 버전의 요청 검증 강화가 정당한 호출자의 한 형식 변형을 거절)이 원본과 같다 (원칙 1)
- [x] 분류 장부를 갱신했다(§4-1, §6). 묶음 L 5→6(9.7%), 정답 위치 은행 이체 서비스 5→6(9.7%), 결제 경로 10/62(16.1%), 은행 18→19. 어느 축도 20% 에 닿지 않는다 (원칙 2)
- [x] 근본 원인 위치가 `file:line`(패치 19-21, 31-36)과 인프라 지점(testbed-transfer 이미지)으로 확인됐다 (G1)
- [x] 근본 원인의 흔적이 119 실데이터에서 조회됐다: 호출자별 금액 표기(로그 7일 전수), KCM 롤아웃 이벤트 (원칙 3)
- [x] 핵심 증거가 표본에만 있지 않다: 거절 로그, commerce 오류 본문, KCM 이 전수다 (원칙 3)
- [x] 계기의 흔적이 조회됐다(KCM ScalingReplicaSet, Pulled 이미지 태그). 인공 지연을 쓰지 않는다 (원칙 4)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다 (원칙 5)
- [x] 정답지 세 칸이 원칙 6대로 채워졌다
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다 (원칙 7)
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: 태그 2.3.0, 패치가 더한 문자열에 id, fault, bug, chaos, scenario 없음(실행기 시험이 확인) (원칙 8)
- [x] 부하 상한과 서킷브레이커를 고려해 피해가 실제로 날 계산이 있다: 정산 전부 소수 표기라 거절이 결정적이고, 6rps 로 checkout 초당 약 3건 (원칙 9)

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- 배포 전제: 109 에서 `bash scripts/scenarios/fault-images/build.sh f40-h` 로 core-banking-transfer:2.3.0 이 빌드되어 있어야 preflight 가 통과한다.
- 새 파드 기동 뒤 첫 정산부터 'Transfer rejected (amount not in whole won)' 가 남는지, banking 자체 이체의 'Transfer COMPLETED' 가 이어지는지.
- order 오류 본문에 은행 문장('amount must be in whole won')이 실리는지(반열림 시도).
- lucida-next 가 commerce 쪽 오류로 인시던트를 만드는지(원칙 7).
