---
title: F40-R 설계 시트 (pricing 프로모션 할인율 오입력으로 모든 checkout 견적이 0 원이 되어 은행이 정산 이체를 거절하고 checkout 이 502 로 실패)
status: Draft
owner: project
last_reviewed: 2026-10-09
tags:
  - scenario
  - design
  - config
  - commerce
summary: 운영자가 commerce pricing 에 '전 품목 10% 할인' 프로모션을 할인율 100.00 으로 잘못 넣고 반영하려고 pricing 을 재시작해, 모든 checkout 견적이 0 원이 되고 core-banking 이 금액 0 인 정산 이체를 400 'amount must be positive' 로 거절해 모든 checkout 이 502 로 실패하는 시나리오. pricing 과 banking 은 Ready 이고 빠르다. 원본은 Google Cloud 2025-06-12 정책 데이터 장애.
---

# F40-R 설계 시트

## 1. 요약

commerce pricing-service 는 활성 프로모션을 메모리에 들고 있다가(기동 때와 매시 정각에 `pricing_schema.promotions` 에서 읽음) checkout 견적마다 가장 큰 할인을 범위 검사 없이 빼고, 0 아래로 내려간 총액은 0 으로 자른다. 운영자가 가을 프로모션을 "전 품목 10% 할인" 으로 넣으려다 `discount_percent` 를 100.00 으로 넣고, 다음 정각을 기다리지 않고 반영하려고 pricing 을 재시작한다. 새 pricing 파드는 `Promotion cache refresh finished: activeCount=3`(평시 늘 2)을 남기고 그때부터 모든 견적을 200, 총액 0 으로 낸다.

order 는 그 총액으로 결제를 청한다. payment 는 결제 행을 저장하고 외부 PG 승인을 받은 뒤(mock 은 금액을 보지 않는다) core-banking 에 commerce-settlement 에서 commerce-merchant 로 그 금액의 정산 이체를 청한다. transfer-service 는 금액 0 을 `400 amount must be positive` 로 거절하고, payment 의 BankingTransferClient 는 그 거절을 502 로 바꾸며 결제 트랜잭션이 롤백된다. order 는 결제를 한 번 재시도하고, 예약 재고를 돌려준 뒤 502 로 답한다. 몇 초 안에 order 의 결제 서킷브레이커가 열려 이후 checkout 대부분은 바로 502 가 되고, 반열림 시도마다 전체 경로가 되풀이된다.

비유: 매장 계산대의 가격표 시스템에 "10% 할인" 대신 "100% 할인" 이 입력되고 시스템을 껐다 켜서 바로 반영했다. 계산대는 모든 물건을 0 원으로 찍고, 카드사(은행)가 "0 원 결제는 받을 수 없다" 며 승인 정산을 거절해 아무도 계산을 마치지 못한다. 가격표 시스템도, 카드사도 고장 나지 않았다.

## 2. 원본 사례

- 기업, 날짜: Google Cloud, 2025-06-12 (약 10:45 PDT 정책 변경, 10:51 PDT 상태 페이지 사고 시작)
- 링크: [공식 사후 보고](https://status.cloud.google.com/incidents/ow5i3PPK96RduMcb1SsW) (`ref-real-world-incidents.md` M1 에 이미 있음, 2026-10-09 WebFetch 로 원문을 확인해 세부를 보강)
- 요약(출처가 말한 사실): 정책 변경이 Service Control 이 정책을 읽는 지역 Spanner 테이블에 들어갔다. 정책 데이터에 의도치 않은 빈 필드가 있었고, 쿼터 메타데이터라 몇 초 안에 전역 복제됐다. 지역마다 쿼터 검사가 그 값을 읽어 널 포인터 경로를 타 바이너리가 크래시 루프에 빠졌고 외부 API 요청이 503 으로 거절됐다. 그 코드 경로는 5월 29일 기능 추가로 생겼는데 "did not have appropriate error handling nor was it feature flag protected". 2분 안에 분류, 10분 안에 원인 식별, 약 40분 안에 그 서빙 경로를 끄는 red-button 배포를 마쳤다. 보고서의 근본 원인 요약은 "an invalid automated quota update to our API management system which was distributed globally". 재발 방지로 fail open, 전역 복제 데이터의 점진 전파와 검증, 기능 플래그 보호를 약속했다.

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 의도치 않은 빈 필드가 든 정책 변경이 Service Control 이 읽는 정책 테이블(Spanner)에 들어감 | 할인율을 10.00 대신 100.00 으로 잘못 넣은 프로모션 행이 pricing 이 읽는 `pricing_schema.promotions` 에 들어감 |
| 반영 | 쿼터 메타데이터라 몇 초 안에 전역 복제, 다음 요청부터 모든 지역이 읽음 | 프로모션은 기동 때와 매시 정각에 메모리로 읽는다. 운영자가 바로 반영하려고 pricing 을 재시작, 새 파드가 기동하며 읽음(`activeCount=3`) |
| 원인이 된 결함 | 정책 값을 읽는 코드 경로에 오류 처리와 기능 플래그가 없음(빈 필드를 그대로 씀) | 견적 코드가 프로모션 할인율을 범위 검사 없이 그대로 씀(100% 를 빼고 0 아래는 0 으로 자름) |
| 전파 경로 | 모든 쿼터 검사가 그 정책을 읽음 → 널 포인터 → 크래시 루프 → API 요청 503 | 모든 checkout 견적이 그 할인을 씀 → 총액 0 → 결제 → 은행이 금액 0 을 400 으로 거절 → payment 502, order 502 |
| 사용자 증상 | 여러 Google Cloud, Workspace 제품의 외부 API 요청이 503 | 모든 checkout 이 502. 상품 보기, 장바구니, 로그인은 정상 |
| 원본의 탐지 경로 | 2분 안에 SRE 분류, 10분 안에 원인(정책 변경) 식별 | order, gateway 5xx 와 order ERROR 급증. 원인은 같은 분의 pricing 재시작과 활성 프로모션 수 변화, 은행의 금액 거절 문장으로 닿는다 |
| 근본 위치의 신호 | 정책을 읽은 Service Control 자체가 크래시 루프(근본 서비스에 오류가 보임) | 근본 서비스 pricing 은 아무 오류 신호도 내지 않는다(견적 200, 지연 평시). 신호는 재시작과 활성 프로모션 수 변화뿐이고 오류는 하류에서 난다. 원본보다 어렵다 |
| 완화와 복구 | 그 서빙 경로를 끄는 red-button(약 40분), 큰 지역은 재시작 몰림으로 더 걸림 | 그 프로모션 행을 지우고 pricing 을 다시 재시작(cleanup). 커밋된 주문, 결제, 이체가 없어 복구할 데이터가 없다 |

기전 비교: 고리는 "검증 없이 들어간 정책 데이터 → 서빙 경로가 그 값을 그대로 씀 → 모든 요청 실패" 로 같다. 다른 점은 실패가 드러나는 곳이다. 원본은 정책을 읽은 바이너리 자체가 널 포인터로 죽었고, 테스트베드는 정책을 읽은 pricing 은 멀쩡히 200 을 내고 그 결과(총액 0)를 하류 은행이 검증에서 거절한다. 원본처럼 소비 서비스가 죽게 만들려면 pricing 코드에 결함을 심어야 하는데(앱 코드 변경, 사람 검토 대상), 프로모션 표는 NOT NULL 제약이라 빈 값도 넣을 수 없다. 대상과 규모를 우리 스택에 맞춘 것이고, 근본 원인 층위(잘못된 정책 데이터와 그것을 받아들인 서비스)는 원본과 같다.

## 3. 숫자 근거 (2026-10-09, `scenario-stats.py`, 정식 + 후보 합계 43, 이 후보 전)

| 축 | 값 | 판단 |
|---|---|---|
| 서비스 | 쇼핑몰 23, 은행 11, 음식배달 9 | 음식배달, 은행이 적지만 0 인 정답 위치가 남지 않았다(아래). 쇼핑몰은 정답 위치 0 인 부품이면 낸다(원칙 2, 2026-10-08 완화) |
| 묶음 0개 | J, K, N | 아래 이유로 이번에도 막힘 |
| 묶음 G(설정 오배포) | 5(12%) → 6(14%) | 현실 트리거 31%(Google SRE) 대비 적다 |
| 상한 근처 | A 7, B 7, D 7(각 16%) | 피함 |
| 정답 위치 가격 서비스 | 0 → 1 | §2-1 에 새로 더함(commerce pricing, 부품 지도에서 0) |
| 결제 경로 합계 | 10(23%) | 정답이 결제가 아니므로 변동 없음. 결제 서비스는 부분 점수(증상)다 |

0인 묶음과 음식배달, 은행의 0 인 부품을 고르지 못한 이유(2026-10-09 실측):

- J(결함 있는 새 버전 배포): tb-w3 의 food 이미지가 서비스마다 `latest` 하나뿐이다(crictl, 2026-10-09). 결함 이미지는 앱 코드 변경이 필요하다.
- K(네트워크): 워커 conntrack 한도 축소는 sudo 명령과 커널 'table full' 로그가 119 syslog 에 남지만(2026-10-09 확인: tb-w* 의 kernel, sudo COMMAND 수집) 공식 포스트모템을 찾지 못했다(웹 검색 2회, 블로그와 벤더 KB 뿐).
- N(재시도 증폭): food, banking 은 저율(최대 수 rps)이고 모든 동기 호출에 서킷브레이커가 있어 재시도로 하류가 포화되지 않는다.
- food 의 0 인 부품(kafka, notify, order 쪽 정답 위치는 이미 6): 세 도메인 모두 Kafka 발행이 outbox 릴레이(비동기)라 브로커, 토픽, notify 고장이 사용자 증상으로 이어지지 않는다(원칙 7). restaurant 는 loadgen 진입점(30181)이라 통째로 멈추면 주문 여정이 나가지 않는다.
- banking 의 0 인 부품(kafka, nginx): Kafka 는 위와 같고, nginx 는 OTel 이 없어 거절이 119 에 남지 않는다(rejected 기록).

왜 이 후보인가: 정답 위치(가격 서비스)가 0 이고, G 는 현실에서 가장 흔한 트리거인데 아직 적다. 지금까지 G 의 주입은 모두 배포 env, DB 전역 변수, 프로브 같은 "기술 설정" 이었고 업무 설정 데이터(프로모션, 쿠폰, 요금 같은 운영자가 넣는 값)가 서빙 경로를 깨는 장애는 처음이다. 증상(정산 이체 실패로 checkout 502)은 F17-R, F01-P 와 겹치지만 그 둘은 은행 쪽이 고장이고 F40-R 은 은행이 멀쩡하게 거절한다. 같은 증상 아래 다른 원인을 가리는 감별이 이 시나리오의 가치다.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 운영자가 `pricing_schema.promotions` 에 행 4 를 넣는다: '가을 정기 세일', 설명 '전 품목 10% 할인', `discount_percent` 100.00, 어제부터 30일, 활성. 시드(`commerce/pricing-service/src/main/resources/data.sql:9-13`)는 id 1~3 을 `ON CONFLICT DO NOTHING` 으로 넣으므로 재시작해도 행 4 는 그대로다. 2026-10-09 조회: 행 1~3, 활성 2개(10.00, 5.00).
2. `kubectl rollout restart deployment testbed-pricing`. replicas 1, 기본 롤링 업데이트라 새 파드가 Ready 가 될 때까지 옛 파드가 견적을 낸다.
3. 새 파드 기동 때 `PricingService.java:48-59 (initPromotionCache → refreshPromotionCache)` 가 활성 프로모션을 읽어 `Promotion cache refresh finished: activeCount=3` INFO 를 남긴다. 같은 메서드는 `@Scheduled(cron = "0 0 * * * *")` 로 매시 정각에도 돈다.
4. `PricingService.java:78-121 (calculateQuote)` 가 `findBestActivePromotion`(125-128, 할인율 최댓값)으로 100% 를 골라 소계 전부를 빼고, 쿠폰(SAVE10, WELCOME5000) 할인 뒤 음수가 된 총액을 0 으로 자른다. 응답은 200.
5. `commerce/order-service/.../OrderService.java:155 (checkout)` 가 견적을 받고 `189-199` 에서 `paymentClient.requestPayment(order.getId(), quote.total(), "CARD")` 를 부른다.
6. `commerce/payment-service/.../PaymentService.java:53-79 (processPayment)` 가 결제 행을 저장하고 PG mock 승인(금액을 보지 않는 기대값, `commerce/k8s/31-external-pg-mock.yaml`)을 받은 뒤 `BankingTransferClient.java:37-58 (transfer)` 로 정산 이체를 청한다.
7. `core-banking/transfer-service/.../TransferService.java:53-56 (execute)` 가 금액 0 이하를 `400 amount must be positive` 로 거절한다(잠금, DB 접근 전). transfer 의 GlobalExceptionHandler 는 로그를 남기지 않는다.
8. BankingTransferClient 는 4xx 를 포함한 `RestClientException` 을 `core-banking transfer call failed: ...` 502 로 바꾸고, `processPayment` 의 @Transactional 이 롤백된다. payment 는 502 로 답한다.
9. order `PaymentClient` 는 5xx 를 실패로 세고 한 번 재시도한 뒤(`application.yml` paymentClient max-attempts 2) fallback 이 `Payment service unavailable: Payment service error: 502 ...` 를 던진다. order 는 `Checkout payment failed, releasing reserved stock` ERROR 를 남기고 재고를 돌려준 뒤 502. 서킷브레이커(창 10, 최소 5, 실패율 50%, 열림 5초)가 열려 이후는 'CircuitBreaker paymentClient is OPEN' 으로 바로 실패한다.
10. gateway 는 하류 응답을 그대로 넘긴다(`ProxyService.doForward`). 사용자 checkout 502.

## 5. 원인 규정 (원칙 5, 6)

- 근본(`root_cause.target_id`): `commerce-pricing`, target_kind `config`. 결함은 pricing 이 읽는 업무 설정(프로모션 행 하나의 할인율)이다. 원본이 근본 원인으로 지목한 층위(서빙 시스템에 들어간 잘못된 정책 데이터)와 같다. 고쳐야 재발이 막히는 곳도 pricing(행 삭제, 범위 검증)이다.
- 계기(`trigger_target_id`): 비움. 반영 절차(재시작)도 같은 pricing 에서 일어났다.
- 부분 점수(`scoring.partial`): `commerce-payment`, `commerce-order`. 실패를 낸 곳(증상)이고 관제 데이터만으로 "결제 단계에서 금액이 거절됐다" 까지 간 답이다.
- 오답: `core-banking-transfer`. 은행은 정당한 검증으로 거절했고 자체 이체는 계속 성공한다. 은행을 찍는 답은 "거절한 곳 = 고장 난 곳" 으로 본 것이다.
- 원칙 5: 정답은 "pricing 에 새로 활성화된 설정(프로모션)이 checkout 금액을 0 으로 만들었다" 다. 관제 데이터로 닿는 경로는 ① 실패 시작과 같은 분의 pricing 재시작(KCM)과 활성 프로모션 수 2 → 3(로그) ② 실패 문장에 실린 은행의 금액 거절('amount must be positive') ③ 금액이 pricing 견적에서 온다는 호출 관계(order → pricing → payment 순서, 스팬) ④ 경로의 다른 모든 곳이 빠르고 Ready 이며 은행 자체 이체가 성공한다는 사실이다. 할인율 값(100.00) 자체와 견적 본문은 수집되지 않으므로 요구하지 않는다. 코드 설계 결함(범위 검증 없음)은 정답이 아니라 user_impact 와 mechanism 설명에 둔다.

## 6. 감지, 피해 판정, RCA 증거 구분 (원칙 7)

| 층 | 무엇으로 | 이 시나리오 |
|---|---|---|
| 감지 | 겉 증상 | order, gateway POST /api/orders/checkout 5xx(스팬, APM 오류율), order ERROR 로그 급증('Checkout payment failed', 평시 F 시나리오 실행 때만), payment 5xx. 별도 업무 규칙 없이 오류율과 오류 로그로 이상 탐지가 걸린다 |
| 피해 판정(러너) | k6 live 문서 | 동반 부하의 checkout 5xx 비율 ≥ 0.5, commerce-order 서버 스팬 오류율 ≥ 10% |
| RCA | 녹화된 데이터 | §7 의 KCM 롤아웃, pricing 시작 로그, order ERROR 본문, banking 로그와 스팬, pricing 정상 |

인시던트 근거: F08-P(정식, 같은 checkout 502 를 order 설정 한 칸으로 만든다)와 F17-R(정식, 같은 정산 이체 실패)이 녹화에서 인시던트를 만들었다. F40-R 의 겉 증상(order, gateway checkout 5xx, order ERROR 급증)은 둘과 같은 꼴이고 강도는 checkout 거의 전부라 더 크다. promote 는 F38-R 시트(§6)에 적힌 대로 대개 고장 2~4분째 첫 판정에서 난다고 보고 min_hold 를 12분으로 둔다.

## 7. 관측 근거 표 (119 실조회, 2026-10-09 04:00~04:30 UTC)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 핵심 증거는 전수 로그와 KCM 이다.

| 증거 | 119 표, 칸 | 실제 조회 | 평시 결과 |
|---|---|---|---|
| 계기: pricing 재시작 | `kcm_events_local` (namespace, object_name, reason) | `SELECT object_name, reason, count(), max(timestamp) FROM lucida.kcm_events_local WHERE namespace='rca-testbed-commerce' AND object_name LIKE 'testbed-pricing%' GROUP BY 1,2` | 마지막 기동 2026-08-20(Created, Started), 2026-08-25 NodeNotReady 뒤로 롤아웃 없음. 주입 때 ScalingReplicaSet 이 이 보존 기간 안에서 처음이다. 같은 꼴의 이벤트가 다른 Deployment 에 남는 것 확인(food testbed-dispatch, 2026-10-09 00:26 'Scaled up replica set ...') |
| 계기와 근본: 활성 프로모션 수 | `lucida_logs_local` (service_name='commerce-pricing', body) | `SELECT body, count() FROM lucida.lucida_logs_local WHERE timestamp > now() - INTERVAL 30 DAY AND service_name='commerce-pricing' AND body LIKE 'Promotion cache refresh finished%' GROUP BY body` 와 `toMinute(timestamp) != 0` 조건 | 보존된 7일(2026-10-02 05:00 ~ 10-09 04:00) 168건(시간당 1건, started 와 finished 를 합친 로그는 336건) 모두 'activeCount=2', 정각이 아닌 기록 0건. 기동 때 로그도 남는 것은 다른 서비스로 확인(food dispatch 재시작 때 'HikariPool-1 - Start completed', 'Started DispatchServiceApplication' 이 119 에 남음) |
| 전파: 은행의 금액 거절 | `lucida_logs_local` (body) | `SELECT count() FROM lucida.lucida_logs_local WHERE timestamp > now() - INTERVAL 30 DAY AND body LIKE '%amount must be positive%'` | 0건. order 의 'Checkout payment failed, releasing reserved stock: ...' 는 F01-P 등 실행 때만 있고 그때 본문은 서킷 열림 문장이었다 |
| 전파: 정산 이체 끊김 | `lucida_logs_local` (service_name='core-banking-transfer', body LIKE 'Transfer COMPLETED%') | `SELECT countIf(body LIKE '%from=commerce-settlement%'), countIf(body NOT LIKE '%from=commerce-settlement%') ... INTERVAL 7 DAY` | 7일 정산 295,026건, 은행 자체 219,840건. 고장 중 앞쪽만 끊기고 뒤쪽은 이어져야 한다 |
| 증상과 감별: 스팬 | `otel_traces_local` (service_name, span_name, http.response.status_code, duration_ns) | 7일 SERVER 스팬 집계 | pricing POST /api/pricing/quote 14,330건 5xx 0, p50 1.7ms, p95 2.8ms. payment POST /api/payments 13,394건 5xx 0. transfer POST /api/transfers 23,661건 5xx 1, 4xx 0. order POST /api/orders/checkout 14,393건 5xx 926(다른 시나리오 실행 구간) |
| 보조: DB 쪽 | `dpm_topsql_local` (engine='postgresql', body.sqlText, totalRows) | `... WHERE engine='postgresql' AND body ILIKE '%promotion%'` | 프로모션 조회가 일부 정각에만 상위 문장에 들어 totalRows 2 로 남음(분당 상위 17~19 문장만 수집). 주입의 INSERT 와 기동 때 조회(행 3)는 그 안에 들 때만 보인다. 핵심 증거로 쓰지 않는다 |

## 8. 감별

- must_support: KCM pricing 롤아웃과 새 파드의 activeCount=3, 같은 분부터 order ERROR 와 'amount must be positive', order, gateway 5xx 와 checkout 5xx 비율, 정산 이체 로그만 끊김, pricing 200 과 평시 지연, 다른 재시작 없음.
- must_rule_out: banking 고장(F17-R, F01-P, F35-R), 외부 결제나 payment 자원(F01-H, F06-R, F05-R), pricing 다운이나 지연, order 설정 배포(F08-P), 재고 고갈(F23-R), 부하. 각 근거는 정답지에 적었다.
- contrast_with: F17-R(같은 정산 실패, transfer NotReady 로 연결 거절), F01-P(정산 계좌 잠금으로 기다리다 실패), F08-P(checkout 502 를 order 배포 env 가 만든다).
- 러너 must_rule_out: achieved_rps < 3, banking transfer 파드 NotReady, commerce payment 파드 NotReady, commerce-pricing 서버 스팬 오류율 ≥ 10%. pricing 파드 Ready 는 넣지 않는다: 재시작 직후 약 1분 동안 새 파드가 NotReady 인 것은 주입의 일부라 오발화한다(recovery 에만 쓴다).

## 9. 러너 판정 조건과 강도, 부하 계산 (원칙 9)

진행 대본: 설계 강도 하나로 고정한 평가 모드(`approved-fixed-f40-r`). 강도라는 축이 없다: 프로모션이 활성이면 모든 checkout 견적이 0 이다.

| 항목 | 값 | 근거 |
|---|---|---|
| primary | db.config_row, 계약 `{postgresql, rca-testbed-commerce, testbed-postgres-0, pricing_schema.promotions, row_id 4, '가을 정기 세일', '전 품목 10% 할인', 100.00, testbed-pricing}` | 실행기 CONTRACTS 와 profiles.json 양쪽 같은 값(G2) |
| companion | load.north_south, commerce surge.js, target_rps 6, ramp 2m, hold 21m, 진입점 30080, 기준선 loadgen-commerce | 여정의 50% 가 checkout(초당 약 3건) |
| min_hold / settle / timeout | 12m / 30s / 20m, max_injection 25m | 재시작 약 1분 뒤부터 실패가 12분 넘게 이어진다 |
| success(3틱) | checkout 5xx ≥ 0.5, commerce-order 서버 스팬 오류율 ≥ 10% | 성공 조건은 피해만 묻는다(파드 상태 금지, 검사 test_success_conditions_ask_only_whether_damage_occurred). payment 스팬은 서킷이 열리면 60초 창에 표본이 비기 쉬워 쓰지 않는다 |
| must_rule_out(2틱) | §8 | |
| abort | entry_status == 0 (필수) | entry_status 는 동반 부하의 checkout 응답 코드다. 은행 거절은 빠른 502 라 0 이 나오지 않는다 |
| recovery | target_health 200, pricing 파드 Ready, commerce-order 오류율 < 5 (2틱, 10m) | cleanup 이 행을 지우고 재시작한 뒤 가용을 기다린다(상한 180초) |

부하와 재고 계산(계산값, 실측 아님):

- checkout 초당 약 3건(6rps × 50%), 한 건에 상품 1~2개(평균 1.5) → 고장 전 램프 2분과 재시작 약 1분 동안 재고 소비 초당 약 4.5개(분당 약 270) + 기준선(재입고 배치 주석의 평시 소비 분당 약 110). 재입고 배치는 1분마다 20 미만인 상품 16종을 60 으로 채우므로 상품당 분당 약 24개 소비는 따라간다. 10rps 이상이면 상품당 분당 35개를 넘어 정각 재입고 사이에 0 에 닿아 409 가 섞인다.
- 고장 중에는 checkout 이 예약 재고를 돌려주므로 재고가 줄지 않는다.
- 결제는 checkout 한 건에 두 번(재시도) PG 까지 가지만 서킷이 열리면 반열림 시도(5초마다 3건)만 간다. PG mock 과 은행 모두 즉시 응답하므로 VU 가 쌓이지 않는다. 동반 부하 6rps 는 load.north_south 계약(1~180) 안.
- 표본: order checkout 서버 스팬 초당 약 3건 × 10% = 분당 약 18건, 거의 모두 502.

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(Google Cloud 2025-06-12, 공식 사후 보고)와 요소별 대응표가 있고, 고리(검증 없는 정책 데이터 → 서빙 경로가 그대로 씀 → 모든 요청 실패)가 같다. 실패가 드러나는 곳의 차이와 이유를 적었다 (§2)
- [x] 분류 장부 §2-1(가격 서비스 추가), §4-1, §6 갱신. 묶음 G 5→6(14%), 정답 위치 가격 서비스 0→1, 결제 경로 합계 10 그대로, 쇼핑몰 23→24(정답 위치 0 이라 허용). 어느 축도 20% 미만 (§3)
- [x] 근본 원인 위치: `PricingService.java:48-59, 78-121, 125-128`, `data.sql:9-13`, 인프라 `pricing_schema.promotions`, Deployment testbed-pricing (§4)
- [x] 근본 원인 흔적: CH `lucida_logs_local` pricing 'activeCount' 로그(전수, 평시 2), `kcm_events_local` 롤아웃 (§7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: 로그 전수(pricing, order, banking), KCM. 스팬은 감별 보조 (§7)
- [x] 계기 흔적: pricing 롤아웃 이벤트와 새 파드 시작 로그. 인공 지연 없음 (§7)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다. 할인율 값 자체는 요구하지 않는다 (§5)
- [x] 정답지 세 칸이 원칙 6대로 (§5)
- [x] 감지, 피해 판정, RCA 증거 구분 (§6)
- [x] 주입 도구가 시나리오 id 를 남기지 않는다: psql 을 postgres 파드 안 DB 소유자로 열어 행 하나와 재시작 하나, 인자와 스크립트에 id 없음(테스트로 고정). 확인용 count 조회가 DPM Top SQL 에 잡히는지는 첫 녹화 누설 검사에서 본다(§11)
- [x] 부하 상한과 서킷브레이커, 재고 재입고를 고려한 피해 계산 (§9)

## 11. 첫 실행(곧 녹화 실행)에서 확인할 것

- 새 pricing 파드의 시작 로그 'Promotion cache refresh finished: activeCount=3' 가 119 에 남는지(@PostConstruct 시점 로그 수집). 남지 않으면 근본 흔적은 KCM 롤아웃과 실패 시작 시각뿐이 되므로 검증 보고서에 적는다.
- order ERROR 'Checkout payment failed, releasing reserved stock' 의 전체 경로 시도 본문에 'amount must be positive' 가 실리는지(Spring 6.2 RestClient 오류 메시지가 응답 본문을 자르지 않는다고 보고 설계했다, 실측 아님). 실리지 않으면 은행 거절의 흔적은 transfer 400 스팬(표본)과 정산 이체 로그 끊김뿐이다.
- transfer POST /api/transfers 400 이 나고 banking 자체 이체 'Transfer COMPLETED' 가 이어지는지. commerce-settlement 잔액은 바뀌지 않는다.
- 누설 검사: 실행기가 run, cleanup 전후로 `SELECT count(*) FROM pricing_schema.promotions WHERE id = $1` 만 몇 번 실행한다(할인율, 활성 여부를 묻지 않게 줄였다). 녹화본의 `dpm_topsql_local` 에 이 문장이 잡혔는지 본다. 운영자의 INSERT, DELETE 는 현실의 계기 흔적이라 남아도 된다.
- 고장 전 구간에 checkout 409(재고 부족)가 섞이는지. 섞이면 다음 보강에서 target_rps 를 낮춘다.
- PG mock 이 금액 0 을 승인하는지(기대값이 금액을 보지 않음, `31-external-pg-mock.yaml`).
- promote 가 났다면 `incidents.promoted_at`, 묶음 멤버, 판정 문장. pricing 이 묶음에 들어가는지(오류가 없는 서비스라 안 들어갈 수 있다. 그래도 정답은 바뀌지 않는다).
- cleanup 뒤 행 4 가 없고 pricing 이 'activeCount=2' 로 다시 시작했는지. 아니면 다음 무인 실행 전에 수동으로 `DELETE FROM pricing_schema.promotions WHERE id=4` 와 pricing 재시작.
- 정각(매시 0분)이 고장 창에 끼면 정각 갱신도 'activeCount=3' 을 남긴다(행이 그대로라 고장이 이어진다). 정상 동작이다.
