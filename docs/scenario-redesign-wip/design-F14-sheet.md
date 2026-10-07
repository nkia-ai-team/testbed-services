---
title: F14-P·F14-R 승격 설계 시트
status: Draft
owner: project
last_reviewed: 2026-07-29
tags:
  - scenario
  - promotion
  - business-integrity
summary: 회생 1순위 두 건 심사 결과. F14-P는 승격(앱 수정 0줄), F14-R은 타임아웃 지형 실측으로 반증되어 parked 유지. 두 건 모두 앱 결함 자체는 이미 코드에 박혀 있었다.
---

# F14-P·F14-R 승격 설계 시트

두 시나리오는 카탈로그가 **회생 1순위**로 지목한 건이다. 심사 결과는 갈렸다.

| | 결과 | 이유 |
| --- | --- | --- |
| **F14-P** | **승격** | 앵커·주입·피해·감별 전부 성립. 앱 수정 0줄 |
| **F14-R** | **parked 유지 (반증)** | 앵커는 진짜인데 타임아웃 지형상 발화가 불가능 |

두 건 모두 기존 선행조건 문구("injector 신설", "fault-proxy 구현")는 틀렸다. 결함은
**이미 코드에 있었다.** 부족한 것은 발화 조건이었고, F14-P는 그 조건을 만들 수 있었고
F14-R은 없었다.

F14-R의 반증은 F04-P·F07-P·F20-P·F24-Q(2026-07-28~29)와 같은 계열이지만 원인이 다르다.
그쪽은 "부하를 부으면 뻗는다"를 서비스시간 측정 없이 가정한 것이었고, 이쪽은 **중첩된
타임아웃의 대소 관계**를 확인하지 않은 것이다. 공통 교훈은 하나다 — 주입이 실제로
발화하는지를 **숫자로** 확인하기 전에는 설계가 끝난 게 아니다.

---

## 1. F14-P — 이체는 성공했는데 원장이 영구 소멸한다

### G1 실재 앵커 (Class A)

`core-banking/ledger-service/.../consumer/TransferEventConsumer.java:40-42`

```java
} catch (Exception ex) {
    log.error("Failed to process transfer event: {}", ex.getMessage(), ex);
}
```

리스너는 평범한 `@KafkaListener` **하나뿐**이다. `ErrorHandler`도, DLQ도, 재시도 설정도
없다(전 소스 검색 결과 `KafkaListener` 선언 1건 외 관련 심볼 0건). 따라서 예외를 삼키고
정상 리턴하면 **Spring Kafka 기본 ack가 오프셋을 커밋한다.**

앵커의 본질은 "예외를 삼킨다"가 아니라 **"삼킴 + 자동 ack = 영구 유실"**이다.
일시적 DB 오류 한 번이 복구 불가능한 데이터 손실로 증폭된다.

합성 지연이 아니고 env로 코드 동작을 왜곡한 것도 아니므로, F03-H가 CUT된 사유
(§G1 금지 1·2)에 걸리지 않는다.

### G2 주입 수단 — 앱이 아니라 DB를 건드린다

기존 선행조건은 "catch-swallow용 injector 신설"이었다. 즉 앱에 결함 제어표면을 새로
심으라는 뜻이다. **채택하지 않는다.** F04-H 선례(`OUTBOX_RELAY_ENABLED=false`)를 따라도
시나리오 전용 플래그가 앱에 남고, 그 이름 자체가 G6 누설 표면이 된다.

대신 **`ledger_entries` 테이블만 Oracle에서 READ ONLY로 전환**한다.

- `ALTER TABLE banking.ledger_entries READ ONLY` → INSERT가 즉시 ORA-12081로 실패
- catch가 삼킴 → 오프셋 전진 → **이벤트는 cleanup 후에도 돌아오지 않는다**
- `transfers` 테이블은 무영향 → 이체 API는 계속 200
- cleanup은 `READ WRITE` 한 줄, 역연산이 완전하다 (G9)
- 점검 후 테이블을 되돌리지 않은 상황 — 실제 운영 실패 모드다

주입 수단은 신설 profile `db.table_readonly`. Oracle 배관은 `db_lock_executor.py`의
검증된 경로(`kubectl exec testbed-oracle-0 -- sqlplus -s / as sysdba`,
`alter session set container=FREEPDB1`)를 그대로 재사용한다.

### G3 피해 — 계획에 적혀 있던 성공조건은 죽은 지표였다

**선행조건 문구 "imbalance 쿼리 등록"은 폐기한다.**

`LedgerService.sumImbalance()`는 `DEBIT합 − CREDIT합`이다. 그런데 이 결함은
`recordTransfer()` 하나의 `@Transactional` 안에서 DEBIT·CREDIT **두 행이 함께**
사라지므로 **imbalance는 영원히 0이다.** `ReconciliationBatch`(600초 주기)도 같은
이유로 끝까지 침묵한다.

이는 2026-07-28에 정리한 "지표 결함" 계열과 정확히 같은 함정이며, 품질기준서
G3 파생규칙 2 **구조적 항상-참 금지**에 정면으로 걸린다. 그대로 구현했으면 성공 판정이
영원히 나지 않는 시나리오가 됐을 것이다.

성공조건은 **transfer↔ledger 대사**로 간다 — 창구 내 `COMPLETED` 이체 중 매칭
원장행이 없는 **비율**(단일 샘플 금지 규칙 준수).

피해 유형은 **비즈니스 무결성 훼손**이며 5xx는 0이다. 기준서가 "반드시 포함시킨다"고
못 박은 silent failure 유형에 해당한다.

### G4 관측 폐포

RCA 시스템이 캡처 데이터만으로 도달하는 경로:

| 증거 | 캡처 위치 |
| --- | --- |
| `Failed to process transfer event` + ORA-12081 ERROR 로그 | 로그 테이블 (ledger-service) |
| ledger-service DB 오류율 상승, transfer-service 정상 | APM |
| consumer lag ≈ 0 (오프셋은 정상 전진) | Kafka 지표 |

부하생성기 클라이언트 측정치는 증거로 쓰지 않는다(G4 규칙).

### G5 감별 — `contrast_with: F18-P`

F18-P(banking outbox relay halt)와 증상이 같다: "이체 성공인데 원장 없음".
데이터에서 갈린다.

| | F18-P | F14-P |
| --- | --- | --- |
| Kafka 메시지 | **아예 없음** | 발행·소비됨 |
| consumer lag | — | ≈ 0 |
| ledger ERROR 로그 | 없음 | **있음** |
| outbox 적체 | 증가 | 없음 |

### G6 누설

시나리오 이름이 박힌 env·태그·플래그가 없다. 로그 문자열은 앱 고유 문구이고,
ORA-12081은 원인의 지문이지 정답 문자열이 아니다.

---

## 2. F14-R — 반증. 앵커는 진짜인데 발화시킬 수 없다

### G1 실재 앵커 (Class A, 결함 쌍)

두 지점이 맞물려야 성립한다.

1. `commerce/payment-service/.../PaymentService.java` `processPayment()` —
   **멱등성이 전혀 없다.** orderId 조회 없이 무조건 `new Payment()` 저장 → PG 호출 →
   **core-banking 이체까지** 재실행한다.
2. `commerce/order-service/.../client/PaymentClient.java:28` `@Retry(name="paymentClient")`
   — `max-attempts: 2`, `wait-duration: 300ms`.

결정적 확인: `PaymentClient.java:37`의 catch는 `RestClientResponseException`만 잡는다.
read-timeout은 `ResourceAccessException`이라 **그대로 빠져나가 resilience4j 재시도에
걸린다.** `ignore-exceptions`는 `ClientErrorException`뿐이므로 제외되지 않는다.

`order-service/application.yml`의 payment `read-timeout: 15s`.

그리고 같은 파일에 이런 주석이 이미 있다:

```yaml
paymentClient:
  # 결제는 재시도 증폭(중복 결제) 위험이 커서 시도 횟수를 낮게 유지.
  max-attempts: 2
```

**코드가 스스로 이 결함을 알고 있었다.** 완화만 했을 뿐 멱등키는 끝내 넣지 않았다.

### G2 주입 수단 — **반증됨 (2026-07-29). 승격 불가, parked 유지**

앵커는 진짜인데 **발화시킬 방법이 없다.** 처음 설계는 pg-mock
(`testbed-external-pg-mock`)의 `/v1/payments`를 일시적으로 15s 넘게 지연시켜
"1차 응답 유실 → 재시도 → 중복"을 만드는 것이었다. 타임아웃 지형을 실측하니 성립하지
않는다.

**중복이 생기려면 두 조건이 동시에 참이어야 한다.**

1. 1차 시도가 **커밋**해야 한다 → payment-service의 각 하류 호출이 자기 타임아웃 안에
   끝나야 한다. `processPayment()`는 통째로 `@Transactional`이므로 어느 한 구간이라도
   터지면 결제 행이 남지 않는다.
2. order-service가 **재시도**해야 한다 → 1차 시도가 order 쪽 15s를 넘겨야 한다.

**실측한 타임아웃 (`RestClientConfig.java`가 `SimpleClientHttpRequestFactory`에 실제로
적용하는 값):**

| 구간 | 값 | 출처 |
| --- | --- | --- |
| order → payment | **15s** | `order-service/application.yml` `services.payment.read-timeout` |
| payment → PG mock | **10s** | `payment-service/application.yml` `services.pg.read-timeout` |
| payment → banking transfer | **10s** | 같은 파일 `services.banking-transfer.read-timeout` |

pg-mock만 늦추면 1차 시도의 총 소요는 사실상 PG 지연 하나이고, 커밋을 유지하려면
**10s 미만이어야 한다.** 그러면 order 쪽 15s에 **영원히 도달하지 못한다** — 재시도가
아예 발생하지 않는다. 지연을 15s 이상으로 올리면 PG 구간이 먼저 터져 롤백되므로
결제 행이 없다. **어느 쪽으로도 중복이 나오지 않는다.**

> **`transient_status`로도 안 된다.** 500을 주면 같은 이유로 `@Transactional`이
> 롤백되어 결제 행이 남지 않는다. 중복이 아니라 단순 실패다.

**성립 조건**: 두 구간을 **동시에** 늦춰야 한다 — 예: PG 9s + 뱅킹 이체 8s
(각각 10s 미만이라 커밋 유지, 합 17s > 15s라 재시도 발생). 그런데 **뱅킹 이체 구간에는
제어 가능한 지연 수단이 없다.** `db.lock`은 해제까지 대기(F01-P는 `hold_seconds: 600`)라
10s를 넘겨 롤백시킨다. 즉 "몇 초만 늦추기"를 표현할 수단 자체가 없다.

**여기서 막고 있는 것은 능력 부재가 아니라 올바른 설계다.** 안쪽 타임아웃(10s)이
바깥쪽(15s)보다 짧게 잡혀 있어 "커밋됐는데 응답만 유실"이라는 상태가 구조적으로
생기지 않는다. 이건 앱의 결함이 아니라 미덕이다.

**갱신된 선행조건**: 뱅킹 이체 구간에 유한·제어 가능한 지연 주입 수단 신설
(PG 9s + 뱅킹 8s 동시 주입). 마진이 ~2s로 얇아 캘리브레이션 난도가 높다는 점도 함께
기록한다.

### 부수 발견 — 고아 probe

`database.payment_duplicate_order_count_since_t1`이 저장소·러너 **양쪽 레지스트리에
등록돼 있고 `live_probes.py:1214`에 구현까지 돼 있는데, 이를 쓰는 매니페스트가 하나도
없다.**

```sql
SELECT count(*) FROM (
  SELECT order_id FROM payment_schema.payments
  WHERE created_at >= (t1)
  GROUP BY order_id HAVING count(*) > 1) AS duplicates
```

정확히 F14-R의 피해를 재는 쿼리다. 누군가 중복결제 probe를 먼저 만들어 뒀지만 위
타임아웃 지형 때문에 **어떤 주입으로도 0 외의 값을 반환할 수 없었다.** F14-R이 풀리기
전까지 이 probe는 계속 고아로 남는다 — 지우지 말고 이 문서를 가리켜 둔다.

### 되살아났을 때를 위해 남겨두는 것

주입 수단만 생기면 나머지는 이미 성립한다. 재심사 때 다시 파지 않도록 적어 둔다.

- **G3 피해**: 주문 1건 / 결제 2건 / 코어뱅킹 이체 2건 / 5xx 0건. 위 고아 probe가
  그대로 성공조건이 된다.
- **G4 관측 폐포**: 한 trace 계보 안에 payment span이 두 개 뜨고, 자원 지표는 전부 정상.
  APM 캡처 범위 안이다.
- **G5 감별 — `contrast_with: F06-R`**: 같은 주입 표면을 쓰는 정반대 서명이다.

  | | F06-R | F14-R |
  | --- | --- | --- |
  | checkout 5xx | ≥ 0.05 (사용자가 실패를 봄) | **0** |
  | 중복 결제 | 없음 | **있음** |
  | 정답 | 외부 PG 행 | 멱등키 부재 × 재시도 증폭 |

  `must_rule_out`에 `checkout_5xx_rate > 0.05`를 둔다 — 5xx가 뜨면 F06-R이다.

---

## 3. 구현 범위 (F14-P만)

**testbed-services**

- `profiles/db_table_readonly_executor.py` 신설 (Oracle READ ONLY / READ WRITE)
- `registry/profiles.json` — `db.table_readonly` 등록
- `registry/queries.json` — ledger 대사 쿼리 신설
- `manifests/f14-p-selective-ledger-loss.yaml` — parked → ready
- `catalog.json` — F14-P readiness 갱신, F14-P·F14-R 선행조건 문구 교체
- `test-scenarios.sh` — ready 43 → 44

**rca-scenario-runner**

- `observation_queries.json` — ledger 대사 쿼리 등록
- `live_probes.py` — ledger 대사 probe 구현 (`integrity_violation_count` 패턴 재사용)
- adapter/profile allowlist에 `db.table_readonly` 등록 (양방향 정합성 — G2)

`mock_expectation_executor.py`는 **건드리지 않는다.** 설계 도중 `transient_delay` 모드를
추가했다가 F14-R 반증과 함께 원복했다 — 소비자 없는 모드는 G2 양방향 정합성 위반이다.

## 4. 미결

- **F14-P의 라이브 실증은 네트워크 복구 이후.** 109 ↔ 192.168.230.0/24가 07-29
  01:20 UTC부터 90% 패킷 로스이고, APM 보고 서비스가 19 → **1**로 붕괴한 상태다
  (07-29 재측정). 주입·판정은 클러스터 내부에서 끝나지만 캡처는 관측 평면에 의존한다.
- **F14-R은 뱅킹 이체 구간 지연 수단이 선행**이며, 마진 ~2s의 캘리브레이션이 뒤따른다.
