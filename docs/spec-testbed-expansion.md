# spec-testbed-expansion — 테스트베드 대폭 확장 + 지속 부하 (commerce 플래그십)

> 상태: **설계 초안 (확정 대기)**. 2026-07-13.
> 목적: 테스트베드를 "의도된 장애 표면을 가진 소형 MSA" → **"평상시 부하가 계속 도는 production-like 서비스"**로 끌어올린다.
> 전략: **flagship-first** — commerce를 먼저 깊게 만들어 패턴 템플릿을 확정한 뒤 food-delivery·core-banking에 복제.

---

## 0. 왜 (해결하려는 갭)

현 테스트베드 현실성 평가(코드 실측)에서 나온 갭:

| 관점 | 현 판정 | 이 문서가 겨냥 |
|---|---|---|
| 기능적 현실성 | 현실적이나 엔드포인트 얇음(~8/도메인) | 읽기·집계·목록 API 대폭 보강 |
| 구조적 현실성 | 진짜 MSA지만 소규모(5-hop 미만, Kafka 없음) | 서비스 확장 + Kafka 백본 + 복원성 계층 |
| **지속 부하** | **없음 — 시나리오 밖 평상시 idle** | **상주 부하 생성기 신설(§8)** |
| 과도한 단순성 | 소형(도메인당 1~1.8k LOC, 3~7 테이블) | 스키마 15~25 테이블 + 대량 시드 |

가장 큰 값은 **지속 부하(§8)** — 이게 없으면 이상감지 정상분포·챗봇 추세/인벤토리·RCA 기준선이 성립하지 않는다.

---

## 1. 확장 원칙 (모든 도메인 공통 템플릿)

commerce에서 확정하고 나머지에 복제할 재사용 패턴:

1. **복원성 계층** (Resilience4j): 서비스 간 모든 동기 호출에 timeout + retry(지수백오프) + circuit breaker + bulkhead, DB는 HikariCP 풀 명시 튜닝. → 풀 고갈·재시도 폭풍·스탬피드·브레이커 open 같은 **실제 RCA 근본원인**을 재현 가능하게 함.
2. **Kafka 도메인 이벤트 백본**: 도메인 이벤트를 Kafka topic으로 발행/구독(현 Redis Streams는 캐시/경량 신호용으로만 잔존 or 제거). **outbox 패턴**으로 DB 트랜잭션-이벤트 정합. → consumer lag·파티션 스큐·컨슈머 리밸런싱 장애 표면.
3. **주기 배치/스케줄러**: 정산·집계·정리 배치를 상주. → 주기적 부하 패턴(이상감지 계절성) + 배치-온라인 자원 경합.
4. **읽기 표면 보강**: 목록/검색/필터/집계 read API를 서비스마다 추가. → baseline 트래픽 다양화 + 엔드포인트별 상이한 지연 프로파일.
5. **관례 유지**: 기존 Maven 멀티모듈 / `com.<domain>.<svc>.{client,config,controller,entity,event,repository,service}` 계층 / 번호순 k8s 매니페스트 / OTel javaagent 주입 그대로. 신규 서비스도 동일 틀.

---

## 2. commerce 플래그십 목표 토폴로지 (5 → ~10 서비스)

**현재(5)**: product · inventory · order · payment · notification
**목표(10)**: 아래

| 서비스 | 신규? | 책임 | DB |
|---|---|---|---|
| **api-gateway (BFF)** | 신규 | 엣지 라우팅·인증검증·집계 조회 | — |
| **user-service** | 신규 | 회원·주소·인증(토큰 발급) | PG |
| **catalog-service** (구 product) | 확장 | 상품·variant·카테고리·검색 | PG |
| **inventory-service** | 확장 | 재고·이동내역·예약/해제 | PG |
| **cart-service** | 신규 | 장바구니(세션성, Redis+PG) | PG+Redis |
| **pricing-service** | 신규 | 가격·프로모션·쿠폰 적용 | PG |
| **order-service** | 확장 | 주문 오케스트레이션(보상 포함) | PG |
| **payment-service** | 확장 | 결제(+ cross-domain banking 이체) | PG |
| **shipping-service** | 신규 | 배송·추적 이벤트 | PG |
| **notification-service** | 유지 | Kafka 소비 → 알림 | PG |

**동기 호출 그래프 (5-hop+ 심화)**
```
client → api-gateway → order → cart(조회) → pricing(가격확정) → inventory(예약) → payment → [cross-domain] banking.transfer
                          └→ order → shipping(배차)
```
**cross-domain 경로 2개(현 1개)**: ① commerce.payment → banking.transfer(기존), ② commerce.order 정산 → banking 원장(신규, 배치/이벤트).

---

## 3. 복원성 계층 배치 (Resilience4j)

| 지점 | 패턴 | 재현되는 RCA 원인 |
|---|---|---|
| gateway→order, order→* 동기 호출 | timeout + retry + circuit breaker | 하류 지연 전파, 재시도 증폭, 브레이커 open으로 인한 가짜 정상 |
| DB 커넥션(HikariCP, 서비스별 pool size 명시) | pool 튜닝 | **풀 고갈**(느린 쿼리 + 고부하 시) |
| pricing/catalog 조회 | bulkhead(동시성 제한) | 특정 하류 포화가 전체로 번지지 않게 격리(격리 실패 시나리오도 가능) |
| cart Redis | timeout + fallback | 캐시 장애 시 DB fallback → **캐시 스탬피드** |

---

## 4. Kafka 이벤트 백본

**Topics**: `commerce.orders` · `commerce.payments` · `commerce.inventory` · `commerce.shipping` · `commerce.user`
**Producer→Consumer(예)**:
- order → `commerce.orders` → {notification, shipping, settlement-batch, analytics}
- payment → `commerce.payments` → {order(상태전이), settlement-batch}
- inventory → `commerce.inventory` → {catalog(재고표시), analytics}

**정합**: 각 서비스 **outbox 테이블 + 릴레이**로 DB커밋과 발행 원자화. → outbox 적체·릴레이 지연도 관측 대상.

---

## 5. 배치/스케줄러 (상주)

| 배치 | 주기 | 목적/부하패턴 |
|---|---|---|
| 정산(settlement) | 시간별/야간 | 결제·주문 집계 → 원장. 야간 스파이크 |
| 재고 재조정(reconciliation) | 10분 | inventory_movements 대사. 주기적 DB 부하 |
| 장바구니 만료 정리 | 5분 | 유휴 cart 정리 |
| 프로모션 갱신 | 시간별 | pricing 캐시 refresh |

이 주기성은 **이상감지 계절성 학습**과 **평상시 baseline 패턴**의 핵심 재료.

---

## 6. 스키마 확장 (7 → ~20 테이블)

users, addresses, categories, products, product_variants, inventory, inventory_movements, carts, cart_items, prices, promotions, coupons, orders, order_items, payments, shipments, shipment_events, notifications, outbox_events, (+ settlement_summary). 시드: 상품 수천, 유저 수천, 과거 주문 수만 행(히스토리 → 챗봇 추세/집계 질의 재료).

---

## 7. 엔드포인트 표면 (읽기 위주 대폭 보강)

각 서비스에 목록/검색/필터/집계 read API 추가. 예(catalog): `GET /products?category=&q=&page=`, `GET /products/{id}`, `GET /products/{id}/variants`, `GET /categories`. read:write 비중을 실서비스처럼 읽기 우위(≈8:2)로 → 부하 생성기가 현실적 혼합 트래픽 생성 가능.

---

## 8. 지속 부하 생성기 (baseline load driver) — 신규 핵심

**정체**: 시나리오(장애 주입)와 **별개인 상주 컴포넌트**. 평상시 저율·현실적 사용자 여정을 **끊김 없이** 발생시켜 "평소 데이터"가 계속 쌓이게 한다. (rca-scenario-runner는 장애 주입 전용으로 유지 — 역할 분리)

**도구**: **k6**(constant-arrival-rate, 시나리오 스크립트, Prometheus/OTLP 출력). 대안 Locust.
**배치**: 테스트베드 내부 **상시 k8s Deployment**(`loadgen`) 또는 runner 호스트 상주. 항상 on.
**여정 가중(예)**: 브라우징(read) 65% / 검색 15% / 장바구니 10% / 체크아웃(order→payment) 8% / cross-domain 이체 2%.
**시간 패턴(diurnal)**: 낮 높고 새벽 낮은 사인파 RPS 프로파일 → 이상감지 계절성·챗봇 추세 성립.
**자립성**: 시드 유저/상품 재사용, 주문은 폐기 가능 데이터, 장기 실행에도 상태 팽창 없게(정리 배치와 연동).
**eval 연계**: eval-data-capture의 "긴 운영 캡처"가 바로 이 상주 부하 위에서 시간창을 뜬다. 평가 재현성을 위해 **부하 프로파일·시드·RNG 고정** 필요.

**미결(부하)**: RPS 절대치(테스트베드 용량 대비), diurnal 진폭/주기, 부하 트래픽을 golden에서 어떻게 배경(정상)으로 표시할지.

---

## 9. RCA 장애 표면 매핑 (각 확장이 여는 근본원인)

| 확장 | 새로 가능한 장애/근본원인 |
|---|---|
| 복원성 계층 | 풀 고갈, 재시도 폭풍, 브레이커 오작동, bulkhead 격리 실패 |
| Kafka 백본 | consumer lag, 파티션 스큐, 리밸런싱 폭풍, outbox 적체 |
| 배치 | 배치-온라인 자원 경합, 야간 스파이크 오탐 |
| 깊은 호출그래프 | 다중 홉 지연 전파, 원인-증상 도메인 분리(cross-domain) |
| 지속 부하 | 위 모든 것이 **평상시 기준선 위에서** 발현 → 이상감지·챗봇 평가 성립 |

---

## 10. 부수 정리 (확장과 함께)

- **리네이밍**: `plopvape-shop` artifact;/패키지 `com.plopvape.*` → `commerce` 정본화 (Codex 지적 #3 관련).
- **배포 정합**: build-and-deploy.sh의 `k3s ctr images import` → **kubeadm(containerd/`ctr -n k8s.io`)** 정정, rollout 실패 `|| true` 제거(Codex 지적).
- **cross-domain 시드 정합**: commerce가 부르는 banking 계좌 ID를 banking 시드에 존재하게(Codex 지적).
- **DB**: core-banking는 별도 확정대로 **Oracle**. commerce는 PostgreSQL 유지.

---

## 11. 다음 단계

1. 본 설계 확정(서비스 목록·부하 도구·RPS 방향).
2. commerce 플래그십 구현(신규 5서비스 + 복원성 + Kafka + 배치 + 스키마 + read API).
3. 부하 생성기(`loadgen`) 구현 + 상주 배포.
4. 플래그십 검증(평상시 데이터가 lucida-next로 흐르는지 + 장애 시 신호).
5. 템플릿 확정 후 food-delivery·core-banking 복제.
