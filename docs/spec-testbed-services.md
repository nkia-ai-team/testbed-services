---
title: 테스트베드 서비스 설계
status: Draft
owner: project
last_reviewed: 2026-07-09
tags:
  - testbed
  - services
  - rca
  - evaluation
summary: 테스트베드 3도메인(commerce/food-delivery/core-banking)의 서비스 구성, 호출 그래프, DB, 장애 주입 표면, 그리고 golden 바인딩을 위한 식별자·태깅 계약을 실제 testbed-services 레포 컨벤션에 맞춰 정의한다.
---

# 테스트베드 서비스 설계

이 문서는 [테스트베드 환경 설계](spec-testbed-design.md)가 정의한 무대 위에 올릴
**실제 애플리케이션**을 설계한다. 환경 문서가 인프라·관측 계약·물리 배치를
담당한다면, 이 문서는 서비스 구성, 호출 그래프, DB, 장애 표면, 그리고 관측이
golden에 바인딩되도록 하는 **식별자·태깅 계약**을 담당한다. 시나리오와 golden
승격 규칙은 [시나리오 설계](spec-scenario-design.md),
[시나리오 작성 규칙](spec-scenario-authoring.md)이 담당한다.

기준 레포 `github.com/nkia-ai-team/testbed-services`
(`/home/ydkim/project-2025/testbed-services`)는 **출발 골격(scaffold)** 으로만
쓴다. 설계·구현은 본 문서 기준으로 새로 하며, 기존 레포의 컨벤션(DB 엔진 매핑,
namespace, 라벨, Redis 유무, org id 방식 등)은 우리 설계에 맞게 바꾼다. 다만
**lucida-next 플랫폼 계약**(§3 — APM `service.name` 정확일치, collector의
`target_id` emit, KCM `namespace/name` 키 등)은 lucida-next의 동작 방식이므로
바꿀 수 없고, 앱·인프라가 이를 준수한다. 즉 §3은 준수 대상, 나머지 레포 관습은
승계 여부를 우리가 정한다.

## 1. 설계 원칙

- 세 도메인은 **업무 흐름과 DB 엔진만 다르고**, 서비스 골격·계측·태깅·배포 규약은
  동일하다. 한 도메인에서 확정한 패턴을 나머지에 복제한다.
- 각 도메인은 **자기완결 폴더**다: `<domain>/{pom.xml, shop-common, <svc>-service…,
  db/, k8s/}` + 진입점 `k8s/build-and-deploy.sh`. Ansible은 이 스크립트만 호출한다.
- **장애는 앱 코드에 내장하지 않는다.** 앱은 timeout·외부 의존 등 "실패 표면"만
  노출하고, 실제 주입은 외부(mockserver expectation API, `rca-scenario-runner`
  레포)가 한다.
- 모든 서비스는 golden 바인딩을 위해 §3의 식별자 계약을 만족해야 한다.

## 2. 공통 기술 규약 (레포 실측)

| 항목 | 규약 |
| --- | --- |
| 런타임 | Spring Boot **3.4.5**, Java **17**, Maven 멀티모듈, 부모 pom에 `spring-boot-starter-actuator` 공통 |
| 공통 모듈 | `shop-common` (data-jpa + jackson + spring-web): 공통 DTO·예외·BaseEntity |
| 동기 호출 | Spring **RestClient** + `SimpleClientHttpRequestFactory`. timeout은 `application.yml`의 `services.<name>.{connect-timeout,read-timeout}`(Duration) → `config/RestClientConfig.java` `@Value` 주입 |
| 비동기 | **Redis Streams** (`StringRedisTemplate.opsForStream`, consumer group). 기존엔 commerce에만 존재 → 신설계는 전 도메인에 도입 |
| 이미지 | 멀티스테이지 `eclipse-temurin:17-jdk`→`17-jre`, 빌드 컨텍스트=도메인 루트, 이미지 태그 `<domain>-<svc>:latest`. **javaagent는 이미지에 굽지 않음** |
| k8s 파일 | 번호 프리픽스 apply 순서: `00-namespace`→`01-secrets`→`02-configmaps`→`10-<db>`(+`11-redis`)→`20~2x-<svc>`→`30-<nginx/mock>` |
| 배포 | `k8s/build-and-deploy.sh` 4단계: docker build → cluster image import → `envsubst`(화이트리스트, fail-fast) 후 `kubectl apply` → `rollout status` |
| DB | StatefulSet + PVC(5Gi) + ClusterIP + `*-external` NodePort(DPM 접속용) |
| 앱 배포 | Deployment replicas 1, `imagePullPolicy: Never`, actuator health로 startup/readiness/liveness probe, requests 200m/512Mi · limits 500m/1Gi |

## 3. 식별자·태깅 계약 (핵심)

lucida-next가 관측을 target에 귀속시키는 규칙이다. 어기면 golden 차원이
바인딩되지 않는다. (근거: lucida-next `application.go`, `labels.go`,
`collector-*`; 실측: testbed-services manifest.)

### 3.1 채널별 바인딩 방식이 다르다

| 채널 | 바인딩 키 | 누가 emit |
| --- | --- | --- |
| APM trace(golden signal) | **`service.name` 문자열 정확일치** = 등록된 application target의 `meta.service_name` | **앱** (OTel resource attr) |
| DPM / SMS / KCM 등 리소스 계열 | **`lucida.target_id`(등록 시 lucida 발급 UUID)** | **collector** (db_poll/sms agent/kcm agent) — 앱 아님 |
| 앱 JVM/런타임 메트릭 | `lucida.target_id`(application target UUID) | 앱 (신설계에서 도입) |

즉 **DB·호스트·pod의 target_id는 collector가 자기 등록정보로 emit하므로 앱이 넣지
않는다.** 앱이 반드시 맞춰야 하는 건 **APM의 `service.name`** 이다.

### 3.2 APM `service.name` — `OTEL_SERVICE_NAME`으로 세팅 (실측 규약)

- `spring.application.name`은 순수 모듈명(`order-service`)으로 두고, OTel용
  이름은 **k8s deployment env `OTEL_SERVICE_NAME`** 으로 도메인 프리픽스를 붙여
  재정의한다(기존 실측: `food-delivery-order`, `commerce-order`).
- 신설계 `service.name` 규약: **`<domain>-<svc>`**. 이 값은 lucida에 등록한
  application target `meta.service_name`과 글자까지 동일해야 한다.

| 도메인 | OTEL_SERVICE_NAME 값 |
| --- | --- |
| commerce | `commerce-order`, `commerce-product`, `commerce-inventory`, `commerce-payment`, `commerce-notification` |
| food-delivery | `food-delivery-order`, `food-delivery-restaurant`, `food-delivery-dispatch`, `food-delivery-payment`, `food-delivery-notify` |
| core-banking | `core-banking-account`, `core-banking-transfer`, `core-banking-ledger`, `core-banking-api` |

### 3.3 lucida 태깅 — `OTEL_RESOURCE_ATTRIBUTES` (실측 규약)

- 기존 실측값: `lucida.organizationId=${POLESTAR_ORG_ID},lucida.groupId=<domain>`
  (food는 `${POLESTAR_ORG_ID}` placeholder + `build-and-deploy.sh` envsubst; commerce는
  org id 하드코딩 — 신설계는 **placeholder 방식으로 통일**).
- `lucida.groupId` = 도메인명(APM에서 도메인 내 서비스 그룹핑).
- **`lucida.target_id`는 기존 레포에 없으나 신설계에서 도입한다.**
  `OTEL_RESOURCE_ATTRIBUTES`에 `lucida.target_id=<application target UUID>`를 추가해
  앱 JVM/런타임 메트릭을 application target에 바인딩한다(§7 등록 루프). 단 APM
  trace의 golden signal 바인딩은 여전히 `service.name` 정확일치이고(플랫폼 계약),
  리소스 계열(DPM/SMS/KCM) target_id는 collector가 담당한다 — target_id 도입은
  앱 메트릭 채널에 대한 추가일 뿐 이 둘을 대체하지 않는다.
- OTel 공통 env: `OTEL_EXPORTER_OTLP_ENDPOINT=${OTLP_ENDPOINT}`,
  `OTEL_EXPORTER_OTLP_PROTOCOL=grpc`, `OTEL_{TRACES,METRICS,LOGS}_EXPORTER=otlp`,
  `OTEL_METRIC_EXPORT_INTERVAL=10000`.

### 3.4 OTel agent 주입 (실측 규약)

- **k8s deployment env `JAVA_TOOL_OPTIONS`** 로 주입(Dockerfile 아님). agent jar는
  hostPath 마운트: `/opt/polestar10/apm`→`/opt/apm`, `/opt/polestar10/wpm`→`/opt/wpm`(readOnly).
- 형태: `-javaagent:/opt/apm/opentelemetry-javaagent.jar -javaagent:/opt/wpm/<svc>/wpmagent.jar -Dwpm.config=/opt/wpm/<svc>/wpmagent.conf`.
- (참고: 기존 social-feed는 이 주입이 빠져 OTel 미적용 상태 — 신설계는 전 도메인 적용.)

### 3.5 k8s 라벨·네이밍 (실측 규약)

- **라벨은 단일 키 `app: testbed-<svc>`** (기존은 domain/tier 라벨 없음). Deployment/
  Service/Pod selector 모두 이 키. 필요 시 `domain=<domain>` 보조 라벨을 추가할 수
  있으나 필수 아님.
- namespace = **`rca-testbed-<domain>`** (commerce/food/banking).
- 컨테이너명 `<svc>-service`, 이미지 `<domain>-<svc>:latest`.
- NodePort는 도메인 간 충돌 없게 30xxx로 지정(기존 혼재: PG 30432/30232, MySQL 30306,
  nginx 30080/30081). 신규 MariaDB NodePort는 30307 등 미사용 값.

## 4. 도메인별 서비스 설계

> 2026-07-13 확장(spec-testbed-expansion.md) 반영 현행판. 각 도메인은 **동기 체인 +
> Kafka 이벤트 백본(outbox) + 관계형 DB + 상주 배치 + 대량 히스토리 시드 + k6 loadgen +
> 외부 mock**을 갖는다. Redis Streams 비동기는 전 도메인에서 Kafka로 대체됐다
> (Redis 자체는 commerce cart 캐시 용도로만 잔존).

### 4.1 commerce (PostgreSQL) — 플래그십, 10 서비스 + gateway

| service.name | 모듈 | 역할 | 주요 호출 |
| --- | --- | --- | --- |
| commerce-gateway | api-gateway | 엣지 라우팅·토큰검증·BFF 집계 (DB 없음) | → 전 서비스(RestClient+CB) |
| commerce-user | user-service | 회원·주소·opaque 토큰 | → PG, → Kafka `commerce.user`(outbox) |
| commerce-product | product-service | 상품·variants·카테고리·검색 | → PG |
| commerce-inventory | inventory-service | 재고·이동내역·예약/해제, 재조정 배치 | → PG, → Kafka `commerce.inventory`(outbox) |
| commerce-cart | cart-service | 장바구니(Redis 캐시 + PG fallback), 만료정리 배치 | → Redis/PG |
| commerce-pricing | pricing-service | 가격·프로모션·쿠폰 견적(bulkhead) | → PG |
| commerce-order | order-service | checkout 오케스트레이션(보상 포함) | → cart→pricing→inventory→payment, → Kafka `commerce.orders`(outbox) |
| commerce-payment | payment-service | 결제·정산 배치 | → PG mock, → **banking transfer(cross-domain ①·②)**, → Kafka `commerce.payments`(outbox) |
| commerce-shipping | shipping-service | 배송 생성(orders 구독)·상태 자동전이 | ← Kafka `commerce.orders`, → Kafka `commerce.shipping`(outbox) |
| commerce-notification | notification-service | 알림 (DB 없는 순수 consumer) | ← Kafka `commerce.orders`/`commerce.payments` |

- DB: PostgreSQL 16, 스키마 8개·물리 테이블 25. 시드: 유저 3k·상품 2k(+variants 6k)·
  주문 50k·배송이벤트 137k, 90일 diurnal+주말 가중, `setseed` 재현.
- 복원성: 전 동기 호출 Resilience4j(timeout+retry+CB, 결제만 max-attempts=2), HikariCP 명시.
- 장애 표면: 기존(row-lock·slow query·mock timeout·pool 고갈) + 재시도 폭풍·CB open·
  bulkhead 포화·캐시 스탬피드·consumer lag·outbox 적체·배치-온라인 경합.

### 4.2 food-delivery (MySQL) — 템플릿 복제 완료

| service.name | 모듈 | 역할 | 주요 호출 |
| --- | --- | --- | --- |
| food-delivery-order | order-service | 주문 허브, stale 정리 배치 | → restaurant/dispatch/payment(CB), → Kafka `food.orders`(outbox) |
| food-delivery-restaurant | restaurant-service | 매장·메뉴, 인기메뉴 집계 배치 | → MySQL |
| food-delivery-dispatch | dispatch-service | 배차·만료전이 배치 | → MySQL, → Kafka `food.dispatch`(outbox) |
| food-delivery-payment | payment-service | 결제·정산 배치 | → PG mock, → Kafka `food.payments`(outbox) |
| food-delivery-notify | notify-service | 3토픽 소비(worker) | ← Kafka `food.*` |

- DB: MySQL 8.0 단일 flat DB, 15테이블. outbox는 `@MappedSuperclass`+제네릭 베이스로
  서비스별 테이블 분리(스키마 분리 불가 대안). 시드: 고객 2k·주문 20k·배차이벤트 40k,
  점심/저녁 피크+주말 가중.
- 장애 표면: 4.1과 동일 계열 + MySQL lock/wait.

### 4.3 core-banking (Oracle) — 신규 + Oracle 전환(2026-07-12 확정)

| service.name | 모듈 | 역할 | 주요 호출 |
| --- | --- | --- | --- |
| core-banking-api | api-service | 이체 요청 진입 | → account(CB) |
| core-banking-account | account-service | 계좌 조회·검증 | → transfer(CB), → Oracle |
| core-banking-transfer | transfer-service | 이체 트랜잭션(`FOR UPDATE` lock 집중), 이자·정리 배치 | → Oracle, → Kafka `banking.transfers`(outbox). **← commerce-payment(cross-domain)** |
| core-banking-ledger | ledger-service | transfers 소비 → 원장 기표, 일말 대사 배치 | ← Kafka, → Oracle, → Kafka `banking.ledger`(outbox) |

- DB: **Oracle 23ai Free**(`gvenzl/oracle-free:23-slim`, arm64) — MariaDB 폐기.
  DPM Oracle 전용 collector·최다 핸들러 커버가 전환 근거. 시드: 계좌 1k·이체 20k·
  원장 39k, 90일 diurnal (`CONNECT BY LEVEL`).
- 장애 표면: 이체 lock, 원장 정합 대기, pool 고갈, slow query + Kafka 계열.

## 5. cross-domain 경로 (2개)

- **① commerce-payment → core-banking transfer** (`POST /api/transfers`, 결제 시점 동기 호출):
  namespace 분리(`rca-testbed-*`)라 FQDN(`testbed-transfer.rca-testbed-banking.svc.cluster.local`)
  으로 호출. 시드 계약: banking 시드에 `commerce-settlement` 계좌 실존.
- **② commerce-payment 정산 배치 → banking 이체** (매시, 미정산 집계분 이체): 배치발
  주기 cross-domain 트래픽 — 야간·정시 스파이크가 경계를 넘는 패턴을 만든다.
- **W3C traceparent 전파 필수**: 같은 trace로 이어져야 lucida-next가 경계를 넘는
  `apm_call` edge를 실측 trace로 생성한다(환경 문서 §4, §7.2). RestClient 사용 시
  OTel javaagent가 자동 전파하므로, 계측만 켜져 있으면 성립한다.

## 6. 장애 주입 표면

- 앱에 injection 엔드포인트를 넣지 않는다. 실제 주입 경로:
  - **external mock**: `mockserver`(food/commerce의 external-pg-mock, social/banking의
    push mock). 시나리오 실행 시 `PUT /mockserver/expectation`으로 지연/504/hang 주입.
  - **DB lock/slow**: `kubectl exec`로 DB pod에 `SELECT … FOR UPDATE; SLEEP` 등 주입
    (기존 시나리오 스크립트 방식).
  - **pod CPU throttle / traffic flood**: K8s CPU limit 조정, 부하 주입.
- 실제 실행은 **`rca-scenario-runner` 레포**가 담당(host에서 K8s API 호출). 본 레포는
  주입 대상 표면(timeout 설정, 외부 의존, lock 가능 테이블)만 제공.
- 재활용: 기존 시나리오 셸 스크립트 4종(row-lock/timeout/cpu-throttle/flood)을 이식,
  MySQL/MariaDB용 변형과 cross-domain 시나리오를 추가.

## 7. 등록·배포 루프

식별자 계약(§3) 때문에 배포는 다음 순서를 따른다.

1. **DB target 등록** — 3개 DB를 `type=database` + collector `db_poll`로 등록
   (host/port/database/**engine**=postgresql/mysql/mariadb). collector가 `lucida.target_id`·
   `db.system`을 자동 emit.
2. **K8s 클러스터 target 등록** + collector-kcm 배포 → pod/container 자동 승격
   (`namespace/name` 키, Running 상태만).
3. **SMS agent enroll** — worker VM마다 안정적 hostname으로 enroll.
4. **application target 등록** — 12개 서비스를 `type=application`,
   `meta.service_name`=`<domain>-<svc>`로 등록 → UUID 수령.
5. **배포** — deployment env에 `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`
   (`lucida.groupId`=도메인, org id placeholder, `lucida.target_id`=app UUID)를 세팅하고
   `build-and-deploy.sh`로 배포(`${OTLP_ENDPOINT}` envsubst).
6. **바인딩 검증** — §9 체크리스트.

DB/host/pod의 target_id는 collector 등록에서 나오므로 앱 배포와 분리된다. UUID를
manifest에 넣어야 하는 경우(선택적 app target)는 스크립트로 주입해 재현성을 지킨다.

## 8. 재활용 자산 매핑

| 기존 자산 | 신설계에서 |
| --- | --- |
| 전자담배 쇼핑몰 예제(PG + Redis Streams, 5 svc + nginx) | **commerce** 베이스(거의 그대로, 리네이밍) |
| food-delivery (PG, 4 svc, RestClient timeout, external-pg-mock) | **food-delivery** 베이스 — PG→MySQL 전환 + Redis 추가 |
| social-feed (MySQL 8.0) | 폐기하되 **MySQL 계측·init.sql 패턴을 food/banking에 참고** |
| 시나리오 셸 스크립트 4종 | 이식 + MySQL/MariaDB·cross-domain 변형 추가 |
| OTel 계측 패턴(JAVA_TOOL_OPTIONS, hostPath, OTEL_SERVICE_NAME, groupId) | 표준 승계 |
| k8s 번호 규약·build-and-deploy.sh·StatefulSet 규약 | 승계 |

## 9. 서비스 검증 체크리스트

- [ ] 서비스별 `OTEL_SERVICE_NAME`(`<domain>-<svc>`)이 등록 target `meta.service_name`과 정확일치하고 trace golden signal이 바인딩된다.
- [ ] 동기 체인(RestClient)과 Redis 비동기 경로가 각 도메인에서 관측된다.
- [ ] 3개 DB(PG/MySQL/MariaDB)가 각 engine으로 DPM 등록되고 session/lock/slow query 근거가 나온다(`sql_hash`/`sql_id`).
- [ ] KCM에서 pod/container가 `namespace/name` 키로 승격되고 pod→node가 관측된다.
- [ ] worker VM host가 안정 hostname으로 SMS enroll되어 host/process 근거가 나온다.
- [ ] cross-domain(commerce-payment → core-banking-transfer)이 같은 trace로 이어져 경계를 넘는 `apm_call` edge가 실측된다.
- [ ] 장애 주입 스크립트가 cleanup으로 baseline 회복된다.

## 10. 확정된 설계 결정과 구현 과제

레포는 scaffold일 뿐이므로 아래는 "우리 설계대로 새로 만든다"로 확정한다.

확정된 설계 결정:

- **commerce = 기존 전자담배 쇼핑몰 예제를 commerce로 리네이밍**(PG + Redis, 베이스 그대로).
- **food-delivery = PostgreSQL → MySQL 전환** + Redis 비동기 경로 추가.
- **core-banking = 신규 폴더**(account/transfer/ledger + api, MariaDB).
- **`lucida.target_id` 도입**: 앱 `OTEL_RESOURCE_ATTRIBUTES`에 추가(§3.3).
- **DB 접속 env 키 통일**(예: `DB_*` 계열로).
- **org id = placeholder(`${POLESTAR_ORG_ID}`)+envsubst로 통일**(하드코딩 제거).
- **cross-domain FQDN을 `service-config` configmap에 추가**.
- **social-feed 폐기**(core-banking으로 대체, MySQL 패턴만 참고).

구현 단계에서 확정할 과제:

- 각 서비스의 구체 엔드포인트·DB 스키마 컬럼(코드 생성 시).
- 등록·배포 루프(§7) 자동화 스크립트(등록 API → manifest 템플릿 주입).
- NodePort 등 충돌 없는 포트 지정(신규 MariaDB 포함).
- 기존 README-코드 불일치는 신규 작성 기준으로 정본화(namespace `rca-testbed-<domain>`,
  wpm.config 경로 관습 하나로).
