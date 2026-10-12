---
title: F79-R 설계 시트 (food payment-service 를 PG 결제 주소를 구분자 없이 이어 만드는 릴리스로 롤아웃해 모든 결제와 주문 502)
status: Draft
owner: project
last_reviewed: 2026-10-12
tags:
  - scenario
  - design
  - release
summary: food payment-service 를 결함 있는 새 릴리스(food-delivery-payment:1.4.0, fault-images/f79-r 패치로 만든 별도 태그)로 롤아웃하면, 릴리스가 외부 PG 결제 주소를 '기본 주소 + 결제 경로' 문자열로 구분자 없이 잇는데 끝 '/' 가 로컬 기본값에만 있어, 운영 주소가 'http://testbed-external-pg-mock:9090pay' 가 되고 payment 가 요청을 보내기도 전에 'Bad authority' 로 모든 결제를 502 로 돌려준다. PG 는 멀쩡한데 모든 새 주문이 배차 뒤 결제 단계에서 502 로 롤백된다. 원본은 rust-lang crates.io 2023-07-20 다운로드 주소 '/' 누락.
---

# F79-R 설계 시트

## 1. 요약

food payment-service 의 새 릴리스 1.4.0 은 PG 공급자를 코드 변경 없이 늘리려고 결제 경로를 설정(`pg.api.pay-path`, 기본 `pay`)으로 빼고, 외부 PG 결제 주소를 `pg.api.base-url + pg.api.pay-path` 문자열로 만든다. 개발자는 기본 주소가 `/` 로 끝난다고 보고 릴리스의 `application.yml` 로컬 기본값을 `http://localhost:9090/` 로 바꿨다. 로컬과 시험은 이 값으로 통과한다. 운영은 food ConfigMap `service-config` 의 `PG_API_URL=http://testbed-external-pg-mock:9090`(끝 `/` 없음)을 쓰므로 결제 주소가 `http://testbed-external-pg-mock:9090pay` 가 된다.

기동과 `/actuator/health` 는 이 주소를 쓰지 않는다. 새 파드는 프로브를 통과해 Ready 가 되고 기본 롤링 전략(maxSurge 25%)으로 옛 파드를 대신한다. 그 순간부터 모든 `POST /api/payments` 가 payment 안에서 요청을 만드는 단계에 RestClient 의 `InvalidUrlException: Bad authority` 로 실패한다. 이 예외는 `RestClientException` 이 아니라 PgApiClient 의 'PG /pay failed' 로그를 건너뛰고, pg 재시도(2회, 300ms)와 pg 서킷브레이커 fallback 이 `ServiceException 502 'PG mock unavailable: Bad authority'`(서킷이 열리면 `'PG mock unavailable: CircuitBreaker 'pg' is OPEN ...'`)로 바꾼다. 결제 트랜잭션은 통째로 롤백되고, payment 는 로그 없이 약 0.3초 만에 502 를 돌려준다. 외부 PG mock 에는 요청이 하나도 가지 않는다. order 는 배차 뒤 마지막 동기 단계에서 이 502 를 받아 'Failed to process payment for order <id>: 502 ...' 를 남기고 모든 새 주문을 502 로 롤백한다.

비유: 계산대(payment)가 카드사(PG)에 승인 요청서를 보낼 때 쓰는 주소를 "본사 주소 + 부서 이름" 으로 적게 바꿨다. 연습용 본사 주소에는 끝에 칸이 있어 문제가 없었지만 실제 본사 주소에는 칸이 없어 "서울시...9090번지결제부" 처럼 붙어 버린다. 우체국이 주소가 틀렸다고 접수를 거부하니 요청서는 카드사에 닿지도 않는다. 손님이 보기엔 카드사가 고장 난 것 같지만 카드사는 아무 요청도 받지 못했다.

## 2. 원본 사례

- **rust-lang crates.io, 2023-07-20** (공식 블로그 사후 보고 "crates.io Postmortem: Broken Crate Downloads", Inside Rust 블로그 2023-07-21): https://blog.rust-lang.org/inside-rust/2023/07/21/crates-io-postmortem/
  - 2023-07-19 17:41 UTC PR #6834 병합: crates.io 코드의 AWS S3 접근을 object_store 크레이트로 옮기는 작업을 마무리하며, 크레이트와 readme 다운로드 끝점이 리다이렉트 주소를 만드는 방식도 리팩터했다.
  - 결함: 주소를 "CDN prefix" 와 "path" 로 이어 만들면서 사이의 `/` 가 빠져 `https://static.crates.iocrates/smallvec/smallvec-1.10.0.crate` 로 리다이렉트됐다. 운영 코드 경로를 시험이 다루지 않았고, 시험은 운영과 다른 값을 썼다.
  - 2023-07-20 12:17 UTC 스테이징을 운영으로 올린 뒤 Cargo 가 "Could not resolve host: static.crates.iocrates" 와 "failed to download from ..." 오류. 약 13분, 초당 약 4.71K 요청, 재시도 포함 약 370만 건 실패.
  - 탐지: 배포한 개발자가 Grafana 에서 늘어난 초당 요청 수를 봄. 배포 약 11분 뒤 커뮤니티 회원이 Zulip 에 보고.
  - 완화: 12:30 Heroku UI 로 이전 릴리스로 되돌려(약 1분) 즉시 복구. 14:10 더 현실적인 시험 값으로 고친 PR #6851 배포.
  - 후속: 다운로드를 스테이징 스모크 시험에 넣고 자동화, 모든 코드 경로를 시험하기 쉽게 구조화.
- `docs/ref-real-world-incidents.md` M2 에 이번에 추가했다(출처가 말한 사실만).

### 요소별 대응표

| 요소 | 원본 사례 | 테스트베드 재구성 |
|---|---|---|
| 촉발 계기 | 저장소 접근을 옮기며 주소 생성을 리팩터한 릴리스 배포 | payment-service 를 릴리스 food-delivery-payment:1.4.0 으로 롤아웃(KCM ScalingReplicaSet, 새 파드 Pulled 이벤트에 태그) |
| 원인이 된 결함 | 리다이렉트 주소를 'CDN 접두어' 와 '경로' 로 이으며 `/` 를 빠뜨림, 시험은 운영과 다른 값을 써 통과 | 결제 주소를 `pg.api.base-url + pg.api.pay-path` 로 구분자 없이 이음, 끝 `/` 는 릴리스의 로컬 기본값에만 있어 로컬과 시험은 통과 |
| 전파 경로 | 잘못된 주소 → 호스트 이름이 망가져 클라이언트가 내려받기 실패(서버에 닿지 않음) | 잘못된 주소 → 요청을 만들 때 'Bad authority'(PG 에 닿지 않음) → payment 502 → order 결제 단계 실패, 주문 롤백 |
| 사용자 증상 | 모든 크레이트 다운로드 실패, 클라이언트 재시도 | food 의 모든 새 주문이 502. 가게, 메뉴, 배달 조회는 정상 |
| 탐지된 경로 | 초당 요청 수 증가(재시도), 사용자 보고 | order 오류율과 ERROR 로그 급증으로 lucida 이상 탐지와 인시던트(원칙 7) |
| 완화와 복구 | 이전 릴리스로 되돌림 | cleanup 이 매니페스트 이미지로 되돌림 |

기전은 원본과 같다: "리팩터 릴리스가 바깥 의존의 주소를 접두어와 경로로 구분자 없이 이어 만든다 → 운영 값에서만 주소가 망가진다 → 요청이 대상에 닿기 전에 실패하고 그 기능 전체가 멈춘다 → 되돌리면 즉시 복구". 바꾼 것은 대상(크레이트 다운로드 리다이렉트 → 결제 대행사 호출)과 규모다. 원본에서는 접두어에 포트가 없어 망가진 주소가 이름 해석 실패로 드러났고, 여기서는 운영 주소에 포트(`:9090`)가 있어 같은 결함이 주소 해석 단계의 'Bad authority' 로 드러난다. 둘 다 "만든 주소가 틀려 요청이 대상에 닿지 않는다" 는 같은 지점에서 끊긴다.

## 3. 숫자 근거 (2026-10-12, `scenario-stats.py`, 정식 + 후보 115 → 116)

| 축 | 이 후보 | 정식 + 후보 (전 → 후) | 상한 20% |
|---|---|---|---|
| 묶음 | J(결함 있는 새 버전 배포) | 19 → 20 (17.2%) | 아래 |
| 정답 위치 | 결제 서비스 | 4 → 5 (4.3%) | 아래 |
| 결제 경로 | 결제 서비스 | 13 → 14 (12.1%) | 아래 |
| 서비스 | 음식배달 | 38 → 39 (쇼핑몰 39, 은행 38) | 균형 |

- 은행과 음식배달이 38 로 가장 적어(쇼핑몰 39) 음식배달에서 골랐다. 음식배달 안 정답 위치는 상한만 본다.
- 결제 경로는 결제 서비스 4, 외부 결제 의존 6, DB 테이블(결제) 3 으로 합계 13(11%)이라 상한 아래다.
- 이 후보의 가치: 겉 증상(결제 단계 502, 'PG mock unavailable', pg 서킷 열림)이 정답 위치 '외부 결제 의존'(6, 가장 많은 위치 가운데 하나)인 F19-S, F06-P 와 같다. "결제가 깨지면 외부 PG 탓" 을 외운 관제 AI 는 여기서 틀린다. 가르는 근거(PG 클라이언트 스팬 소멸, 요청을 만들기 전의 주소 오류, payment 롤아웃)는 §7, §8.

### 후보 목록 (실제 기전 × 부품, 순서는 원칙 2 숫자 순)

| # | 후보 | 원본 사례 | 부품 | 묶음 / 정답 위치 | 판정 |
|---|---|---|---|---|---|
| 1 | 릴리스가 PG 결제 주소를 구분자 없이 이어 만듦 | rust-lang crates.io 2023-07-20(공식) | food payment → 외부 PG | J / 결제 서비스 4 | **채택** |
| 2 | 같은 결함을 order → dispatch 용량 확인 주소에 | 같은 원본 | food order | J / 주문 서비스 8 | 버림: 원칙 3. 'Bad authority' 는 RestClientException 이 아니라 DispatchClient 의 로그를 건너뛰고, 서킷 fallback 과 OrderService 의 용량 확인 처리, GlobalExceptionHandler 가 모두 로그 없이 503 을 돌려줘 원인 문장이 로그에 남지 않는다(rejected-candidates 292행 order → dispatch 서킷 판과 같은 벽) |
| 3 | 같은 결함을 banking api → account 이체 주소에 | 같은 원본 | banking api | J / 은행 API 5 | 버림: 카탈로그 §1. api 의 fallback 이 원인을 남기긴 하지만, F37-H(같은 api 릴리스가 이체 접수를 account 호출 전에 끊음)와 대상, 수단, 영향 범위(이체 접수만), 정답이 같고 오류 문장만 다르다 |
| 4 | 같은 결함을 banking account → transfer 주소에 | 같은 원본 | banking account | J / 은행 계좌 서비스 6 | 버림: 카탈로그 §1. F39-R(account 의 transfer 주소 설정 오배포)과 호출 쌍과 겉 증상이 같고, F39-H(account 릴리스)와 롤아웃 대상이 같다 |
| 5 | 프로덕션 확장 설정이 잘못된 배포(GUIDEcx 2026-08-12) | GUIDEcx 공식 상태 페이지 | food/banking Deployment | G / 각 서비스 | 버림: 원칙 1. 사후 보고가 '잘못된 확장 설정' 이라고만 하고 무엇이(replicas, 자동 확장, 자원) 틀렸는지 밝히지 않음. replicas 0 꼴이면 F51-R |
| 6 | 운영 PG_API_URL 을 옛 값으로 되돌리는 일상 롤아웃(E2B 2026-06-25) | E2B 공식 | food payment env | G / 결제 서비스 | 버림: 원칙 1, 카탈로그 §1. 원본은 DB 자격 증명 드리프트이고, 하류 주소 설정 오류는 F39-R 꼴(rejected 34 와 같은 벽) |
| 7 | 정리 배치 인덱스가 사라져 food orders 정리 UPDATE 가 전수 스캔으로 모든 행 잠금 | Chargebee 2018-03-02(공식) | food MySQL orders | D / DB 테이블(주문) 1 | 버림: 컨트롤러 필수 중단 조건. 주문 INSERT 가 잠금을 기다리면 order Hikari 15 가 마르고 order readiness(DB)가 빠져 entry_status 0(rejected 54 와 같은 벽) |
| 8 | 운영자가 food MySQL require_secure_transport 를 켬 | 공식 사후 보고 없음(Square 2025-02-26 은 인증서 검사) | food MySQL | O / DB 인스턴스 3 | 버림: 원칙 1, 필수 중단. 새 연결만 거절되어 풀이 수명대로 비면 order readiness 가 빠짐(F35-R 음식배달 판 벽) |
| 9 | 다른 서비스의 응답 루트 감싸기(Jackson WRAP_ROOT_VALUE) 설정 배포 | 공식 사후 보고 없음 | food dispatch/restaurant | L / 각 서비스 | 버림: 원칙 1, 7. 공용 DTO 가 빈 값으로 읽혀 4xx 나 조용한 통과가 됨(F34-R 교훈) |
| 10 | 운영 중 Oracle 통계 수집·내보내기 작업이 CPU 를 씀 | 공식 사후 보고 없음 | banking Oracle | A / DB 인스턴스 | 버림: 원칙 1, 9. 바깥 세션이 CPU 를 다 써도 짧은 이체 OLTP 가 굶지 않는다는 실측(rejected 115) |

후보 1 은 새 사례군이다. 같은 원본을 다른 호출 쌍(2~4)에 쓰는 판은 위 사유로 버렸다. 이 시나리오는 F49-P(같은 payment 릴리스 수단)와 수단이 같지만 실패하는 곳이 반대다: F49-P 는 payment 가 결제를 끝내고 200 으로 답하며 order 가 응답을 못 읽고, F79-R 은 payment 가 PG 를 부르지 못하고 스스로 502 를 낸다. 겉 증상이 가장 닮은 것은 외부 PG 고장(F19-S, F06-P)이다.

## 4. 인과 사슬 (코드와 인프라 위치)

1. 롤아웃: `food-delivery/k8s/23-payment-deploy.yaml:9`(replicas 1), `:18-19`(nodeSelector tb-w3), `:27-28`(image food-delivery-payment:latest, imagePullPolicy Never), `:31-33`(envFrom service-config), `:67-86`(startup, readiness, liveness 모두 /actuator/health). 기본 전략 maxSurge 25% 라 새 파드 Ready 뒤 옛 파드가 내려간다.
2. 결함: `scripts/scenarios/fault-images/f79-r/payment-service.patch:17-26`(생성자가 `this.payUrl = pgBaseUrl + payPath;`), `:33-35`(`.uri("/pay")` → `.uri(payUrl)`), `:45-50`(`application.yml` 에 `pay-path: pay` 를 더하고 base-url 로컬 기본값만 `http://localhost:9090/` 로).
3. 운영 값: `food-delivery/k8s/02-configmaps.yaml:18`(`PG_API_URL: http://testbed-external-pg-mock:9090`, 끝 `/` 없음). 파드 env 는 이 값을 덮지 않는다(테스트로 고정).
4. 예외 처리: `food-delivery/payment-service/src/main/java/com/fooddelivery/payment/client/PgApiClient.java:32-66`(`catch (RestClientException ex)` 만 'PG /pay failed' 로그, 그 밖의 예외는 pg 재시도 뒤 fallback 이 `"PG mock unavailable: " + ex.getMessage()` 502), `PaymentService.java:50-89`(@Transactional processPayment 가 ServiceException 을 다시 던져 결제 행까지 롤백, 성공 때만 'Processed payment').
5. 호출자: `food-delivery/order-service/src/main/java/com/fooddelivery/order/client/PaymentClient.java:30-57`(5xx 응답을 'Failed to process payment for order {}: {}' ERROR 로 남기고 502, 재시도 2회 300ms, 서킷), `OrderService.java:128-160`(주문 행 저장, 배차 뒤 결제 호출, 실패를 'Order fan-out payment failed' 로 남기고 다시 던져 롤백).
6. 로컬 실측(2026-10-12): origin/main food-delivery 에 패치를 얹어 `./mvnw -o -pl payment-service -am package -DskipTests` 로 빌드한 jar 를 init.sql 의 MySQL 8.0(로컬 docker)과 PG 대역(파이썬, POST /pay 에 APPROVED)에 붙였다.
   - `PG_API_URL=http://127.0.0.1:19090`(운영과 같이 끝 `/` 없음): health UP, 결제 8건 모두 502(0.31~0.47초). 본문 첫 2건 `"message":"PG mock unavailable: Bad authority"`, 나머지 6건 `"PG mock unavailable: CircuitBreaker 'pg' is OPEN and does not permit further calls"`. PG 대역 수신 0건, payments 표에 남은 행 0(롤백). payment 로그에 결제 실패 문장 없음.
   - `PG_API_URL=http://127.0.0.1:19090/`(끝 `/` 있음, 릴리스의 로컬 기본값과 같은 꼴): 결제 8건 모두 200 APPROVED, PG 대역 POST /pay 8건.
   - Spring 6.2.6(앱과 같은 판) RestClient 에 같은 주소를 주면 `org.springframework.web.util.InvalidUrlException: Bad authority`(IllegalArgumentException 하위, RestClientException 아님)를 jshell 로 따로 확인했다. 상대 경로 `"pay"` 를 기본 주소에 붙일 때는 Spring 이 `/` 를 넣어 주므로, 결함은 문자열로 절대 주소를 만든 데서만 난다.

## 5. 원인 규정 (원칙 6)

- `root_cause.target_id`: `food-delivery-payment`(container). 결함을 가진 곳이자 고쳐야(되돌리거나 주소 조립을 고쳐야) 재발이 막히는 곳이다.
- `trigger_target_id`: 비움(근본과 같은 곳의 롤아웃).
- `scoring.accept`: food-delivery-payment, food-payment, testbed-payment. `partial`: testbed-external-pg-mock, food-external-pg(겉 증상만 본 답), food-delivery-order, food-order, testbed-order(주문 502 를 낸 곳).
- 원본 사후 보고도 "리팩터 릴리스의 주소 생성 결함" 을 원인으로 지목했다. 같은 층위다. 관제 데이터로 "payment 의 새 릴리스 뒤 payment 가 PG 를 부르지 못하고 주소 오류로 실패한다" 까지 바로 간다: 롤아웃 시각, 오류 문장('Bad authority'), PG 클라이언트 스팬 소멸. 패치의 코드 줄을 맞히라고 요구하지 않는다(원칙 5).

## 6. 감지, 피해 판정, RCA 증거

- 감지(lucida): order 서버 오류율과 order ERROR 로그('Failed to process payment for order') 급증, payment 서버 스팬 5xx.
- 피해 판정(러너): 기준선(loadgen-food) 주문 생성 5xx 비율 ≥ 0.8 과 2xx 비율 < 0.2, 3틱(F49-P 와 같은 관측).
- RCA: KCM testbed-payment 롤아웃과 Pulled 이벤트의 새 태그, order 로그의 'PG mock unavailable: Bad authority', payment 'Processed payment' 와 'PG /pay failed' 의 부재, payment → PG 클라이언트 스팬 소멸, MySQL 과 order 무변화.

## 7. 관측 근거 (119 실조회, 평시, 2026-10-12 22:50~23:20 UTC)

표본 데이터(트레이스 10%, APM 지표)만으로 증명하지 않는다. 결정 증거는 전수 로그와 KCM 이벤트이고, 트레이스는 보조다.

| 증거 | 119 표와 칸 | 조회 | 평시 결과 |
|---|---|---|---|
| 근본: 주소 오류 문장 | CH `lucida_logs_local`(body) | 전 서비스 `countIf(position(body,'Bad authority')>0)`, `'PG mock unavailable'`, `'InvalidUrlException'` | 0, 0, 0(보존 창 시작 2026-10-05 21:33 UTC) |
| 근본 경로가 order 로그에 남는가 | 같은 표, `service_name='food-delivery-order'`, `'Failed to process payment'` | 상태별 | 750건(400 748, 500 2, 502 0). 본문이 payment 응답 JSON 전체(`"message":"..."` 포함)를 담는다: 예 'Failed to process payment for order 5025641: 400 : "{"status":400,"error":"Bad Request","message":"orderId required",...}"'. 'Order fan-out payment failed: orderId=... status=400 BAD_REQUEST msg=Payment service failed: 400 : ...' 도 같은 꼴 |
| 계기: 롤아웃과 이미지 태그 | CH `kcm_events_local`(namespace, object_name, reason, body) | `reason IN ('Pulled','ScalingReplicaSet')`, rca-testbed-food | 'Container image "food-delivery-restaurant:1.4.0" already present on machine'(2026-10-12 22:36:59, F49-R 실행)처럼 food 릴리스 태그가 남고 ScalingReplicaSet 이 함께 남는다. testbed-payment 파드 이벤트(Startup/Readiness probe, Killing, ScalingReplicaSet)도 수집된다(2026-10-08) |
| 전파: payment 결제 처리 | `lucida_logs_local` payment 'Processed payment', order 'Created order' | 시간별 6시간 | 시간당 720~930건으로 둘이 같다. 고장 중 'Processed payment' 가 0 이 되고 'Created order' 는 이어진다 |
| 전파: payment → PG 호출 | CH `otel_traces_local`(span_kind CLIENT, span_name 'POST', span_attributes['url.full']) | food-delivery-payment 1일, 시간별 | `http://testbed-external-pg-mock:9090/pay` 5,783건(1일 표본), 시간당 47~96건. 고장 중 0(요청을 만들기 전에 실패) |
| 전파: payment 서버 스팬 | `otel_traces_local` SERVER 'POST /api/payments' | 7일 | 17,641건, 오류 0. 고장 중 502 |
| 배제: 다른 변경 | `kcm_events_local` rca-testbed-food | 주입 구간 | testbed-payment 외 롤아웃, 재시작이 없어야 한다 |

계기의 흔적: 롤아웃(ScalingReplicaSet)과 새 파드 Pulled 이벤트가 새 태그를 담고, 같은 시각부터 오류 문장이 시작된다. 인공 지연은 없다(원칙 4).

원칙 8: 패치가 더한 문자열에 시나리오 id, fault, bug, chaos, scenario 가 없다(테스트). 실행기 argv 에 id 가 없다(테스트).

## 8. 감별

- must_support: 정답지 `must_support` 4항.
- must_rule_out: 외부 PG 고장(F06-P, F06-R, F19-P, F19-S), payment 응답 계약 변경(F49-P), 결제 표(F53-P), 결제 요청 계약(F30-R), 가게·배달 쪽(F49-R, F36-R, F44-R, F32-R, F32-H, F33-H, F49-H, F75-R), order 자체.
- contrast_with: F19-S, F06-P/F06-R/F19-P, F49-P, F53-P, F30-R.
- 가르는 근거:
  - F19-S 와(가장 닮음): 둘 다 payment 의 pg 서킷이 열려 'PG mock unavailable: CircuitBreaker 'pg' is OPEN' 과 주문 502 가 된다. F19-S 는 외부 PG 지연이라 payment 의 PG 클라이언트 스팬이 남고 길며, 서킷이 열리기 전에 'PG /pay failed for order=...: I/O error ... Read timed out' 이 남는다. F79-R 은 PG 클라이언트 스팬이 사라지고 'PG /pay failed' 가 없으며, 서킷이 반열림일 때마다 'Bad authority' 가 섞이고, 직전에 testbed-payment 롤아웃이 있다.
  - F49-P 와: 같은 payment 릴리스 수단이지만 F49-P 는 payment 가 'Processed payment ... APPROVED' 를 남기고 200, order 의 'Error while extracting response' 다. F79-R 은 payment 가 502 를 내고 결제 처리 로그가 없다.
  - F53-P 와: payment 의 MySQL 1146 오류와 500 이 없다.

## 9. 러너 판정 조건과 강도, 부하 계산

- 주입: k8s.image release 모드 1단 고정(`approved-fixed-f79-r`). 강도 축이 없다: 주소가 틀리면 결제마다 실패한다.
- 동반 부하: 없다. 실패한 주문은 이미 용량 확인과 배차를 지난 뒤라 주문 부하를 더하면 ASSIGNED 배차가 ETA 15~35분 동안 쌓여 배달원 한도 2000 이 몇 분 만에 찬다(F49-P, F53-P, F48-P 와 같은 이유). 기준선만 쓰면 실패한 주문의 배차는 평소 성공한 주문이 남기던 만큼과 같아 ASSIGNED 수준이 그대로다.
- 피해 계산(원칙 9): 실패가 시간 초과가 아니라 요청을 만들기 전의 즉시 예외라, 결제 단계에 닿는 주문의 100% 가 502 다. payment 응답 0.3~0.5초, order 재시도 1회(300ms)를 더해도 주문당 약 1초라 기준선 주문(시간당 720~930건, 초당 0.2~0.26건)에서 order 풀(15) 사용은 1 안팎이다. 30초 창의 주문 6~34건(F49-P 관측과 같음)이 모두 5xx 라 판정 문턱(0.8)을 넉넉히 넘는다.
- success: order_create_5xx_rate_baseline ≥ 0.8 과 order_create_2xx_rate_baseline < 0.2, 3틱 연속. min_hold 15m, settle 30s, timeout 20m, max_injection_duration 25m.
- must_rule_out: food MySQL 파드 NotReady, payment 파드 NotReady, order 파드 NotReady(2틱). F49-P 의 'payment 서버 스팬 5xx ≥ 10%' 배제는 두지 않는다(여기서는 payment 가 502 를 내는 것이 정상 경로다). 외부 PG mock 파드는 러너 관측 허용 목록(`live_probes.APPROVED_K8S_TARGETS`)에 없어 녹화로 감별한다.
- abort: entry_status == 0(노드나 order 파드가 죽음). order 는 Ready 로 502 를 답하므로 0 이 나지 않는다.
- recovery: target_health 200, MySQL Ready, payment Ready, 기준선 주문 생성 2xx ≥ 0.7, 2틱, 10m.
- cleanup: 이미지를 food-delivery-payment:latest 로 되돌리고 available 확인, 노드에서 1.4.0 참조 제거. 고장 동안 결제 행이 남지 않는다(payment 가 롤백).

## 10. 설계 원칙 §5 점검표

- [x] 원본 실제 사례(rust-lang crates.io 2023-07-20, 공식)와 요소별 대응표, 기전이 원본과 같다 (§2)
- [x] 분류 장부를 갱신했고 묶음 J(19→20, 17.2%), 정답 위치 결제 서비스(4→5), 결제 경로 13→14(12.1%), 서비스 음식배달 38→39 와 고른 이유를 적었다. 어느 축도 20% 미만 (§3)
- [x] 근본 원인 위치가 `file:line`(payment-service.patch:17-26, :33-35, :45-50, PgApiClient.java:32-66)과 인프라 지점(02-configmaps.yaml:18, 23-payment-deploy.yaml)으로 확인됐다 (§4)
- [x] 근본 원인의 흔적이 119 에 남는 경로를 조회했다: order 로그가 payment 응답 JSON 의 message 를 그대로 담고(평시 750건), 'Bad authority'·'PG mock unavailable' 평시 0, kcm_events_local Pulled 이벤트의 태그 (§7)
- [x] 핵심 증거가 표본 데이터에만 있지 않다: 로그 전수, KCM 이벤트. 트레이스(PG 클라이언트 스팬 소멸)는 보조 (§7)
- [x] 계기 흔적: 롤아웃과 Pulled 이벤트의 새 태그, 같은 시각부터 오류 문장. 인공 지연 없음 (§7)
- [x] 정답이 관제 데이터로 낼 수 있는 결론이다: 롤아웃 직후 payment 가 PG 를 부르지 않고 주소 오류로 실패 (§5)
- [x] 정답지 세 칸이 원칙 6대로 채워졌다 (§5)
- [x] 감지, 피해 판정, RCA 증거가 각각 적혀 있다 (§6)
- [x] 주입 도구가 데이터에 시나리오 id 를 남기지 않는다: 패치 문자열과 argv 에 id 없음(테스트) (§7)
- [x] 부하 상한과 서킷브레이커를 고려해 피해가 실제로 날 계산이 있다: 즉시 예외라 결제 단계 주문 100% 실패, 동반 부하 없이 기준선으로 판정 (§9)
