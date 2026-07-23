# RCA 테스트베드 커버리지 갭 분석 — P1~P7 × 3도메인

생성: 2026-07-23. 입력=3도메인 fault-surface 지도(실측) + 7 구조결함 패턴 + 현행 시나리오(레포 registry 64건, 러너 서빙 28건).

---

## 0. 판정 요약표 (행=패턴, 열=도메인)

| 패턴 | commerce | food-delivery | core-banking |
|---|---|---|---|
| **P1** tx 안 동기 원격호출 → caller 로컬 풀 고갈(오진) | 🟡 F06-R, F03-P | 🟡 F03-R(blocked)·food:05 | 🟡 F01-P 경유 FS-6 |
| **P2** @Transactional timeout 전무 + 기본풀 → thread/pool 포화 | ✅ F03-H (+F09-H) | ❌ | ❌ |
| **P3** 무제한 비관적 row-lock → 상류 502 | ✅ F01-R, F06-H, F15-G/T1 | ❌ | ✅ F01-P, F15-G |
| **P4** Resilience4j 불균일 — 터지는 외부 hop에 CB 없음 | ✅ F01-H, F06-R, F06-G, F07-G | 🟡 food:05 (단 food PG는 CB 존재) | 🟡 FS-2 계열, ready 없음 |
| **P5** silent async(outbox→Kafka) → 200인데 드리프트/lag | ✅ F04-R (F04-H blocked) | ❌ | 🟡 F04-P·F14-P 전부 blocked |
| **P6** fail-close 인증 확산 → 전 도메인 쓰기 401 | ❌ (코드=C3, 시나리오 0) | N/A (게이트웨이 auth 없음) | N/A |
| **P7** 크로스도메인 결합/무결성(root가 namespace 밖) | ✅ F01-P, F08-G, F15-G/T1 | 🟡 f15-t2·F15-T1 (co-victim만) | 🟡 lock=F01-P 커버 / 무결성우회 FS-5 = ❌ |

범례: ✅ 코드 메커니즘과 정확히 맞는 ready 시나리오 존재 / 🟡 비슷하나 근거 얕음·answer-key 어긋남·blocked / ❌ 확실한 fault surface인데 부재 / N/A 해당 도메인 코드에 그 패턴 없음.

---

## 1. 셀별 근거 (왜 이 판정인가)

### P1 (tx-wrapped 동기 원격호출 → 로컬 풀 고갈, 오진 함정 = C1/C12, food C1, FS-6)
- **commerce 🟡**: F06-R(external PG hang), F03-P(payment Hikari) 존재하나 — F03-P는 **자연적 풀고갈이 아니라 env로 pool을 인위 축소**해 증상을 만든다. 코드의 진짜 결함(기본풀 10 + tx가 원격호출을 감쌈 → 하류지연이 order/payment 풀을 자연 고갈, order DB가 범인처럼 보이는 오진)을 정확히 겨냥한 시나리오는 없다. F06-R의 answer-key도 "외부 PG"를 가리켜 풀-고갈 전파 사슬을 검증하지 않는다.
- **food 🟡**: food C1(order 롱-tx 커넥션풀 15 고갈)이 핵심인데, F03-R(food payment connection leak)은 **blocked**, 러너 food:05는 external PG timeout으로 풀-고갈 오진 축이 아님.
- **banking 🟡**: FS-6(transfer pool=15 고갈)은 F01-P(Oracle lock)이 lock 대기로 간접 유발하나, 풀-고갈 자체를 answer-key로 삼는 전용 시나리오는 없다.

### P2 (timeout 부재 + 기본풀 → thread/pool 포화)
- **commerce ✅**: F03-H = order 서블릿 스레드풀(기본 200) 순수 고갈(DB 비접촉 render 엔드포인트). F09-H(GC pause)도 인접. 코드 근거 강.
- **food/banking ❌**: 동일 구조(Tomcat 200 무튜닝, tx timeout 무한)인데 스레드-포화 시나리오 없음.

### P3 (무제한 PESSIMISTIC_WRITE row-lock → 502)
- **commerce ✅**: F01-R(inventory hot row FOR UPDATE), F06-H(payments 테이블 잠금), F15-G/T1(inventory lock). 강.
- **food ❌**: food createOrder는 롱-tx이나 commerce/banking식 hot-row FOR UPDATE 주입 표면이 없고 시나리오도 없음.
- **banking ✅**: F01-P(Oracle FOR UPDATE, NOWAIT 없음, cross-domain), F15-G. FS-1과 정확 일치.

### P4 (외부 hop CB 결여)
- **commerce ✅**: PgApiClient/BankingTransferClient는 CB 없음 — F01-H(429), F06-R(hang), F06-G·F01-G(흡수 음성), F07-G(CB 격리). 잘 커버.
- **food 🟡**: food payment PG client는 **코드상 CB `pg`가 존재**(retry max=2). 즉 food의 외부 hop은 P4의 "CB 없음"에 안 맞음. food:05/06 시나리오는 있으나 패턴 근거가 코드와 어긋남.
- **banking 🟡**: 은행 내부 api/account client는 CB 보유(P4 반례). FS-2(CB open 전면거절)는 CB **동작** 관측이지 "CB 결여"가 아님. ready 시나리오도 없음.

### P5 (silent outbox→Kafka lag, 5xx 없음)
- **commerce ✅**: F04-R(shipping consumer 중단 → lag·shipment 미생성, outbox publish 정상). F04-H(order outbox relay 정지)는 blocked.
- **food ❌**: food C5(outbox relay 정지) 표면 있으나 시나리오 0(F04 계열이 commerce/banking에만).
- **banking 🟡**: FS-3(ledger 단일스레드 consumer lag)·FS-4(outbox 적체)·FS-10(이벤트 유실) 표면 강하나 F04-P(ledger rate-limit)·F14-P(selective ledger loss) **전부 blocked** — ready 0.

### P6 (fail-close 인증 → 쓰기 전면 401) — commerce 전용
- **commerce ❌**: C3 = 게이트웨이 AuthGuard가 매 쓰기요청 verify-token → user CB open 시 401 fail-close(광역 blast radius, RCA 난이도 최상). **user-service 장애를 주입하는 시나리오가 하나도 없음.** 코드 근거 확실한 최대 공백.
- food/banking: 게이트웨이 토큰검증 fail-close 구조 부재 → N/A.

### P7 (크로스도메인 결합/무결성)
- **commerce ✅**: F01-P(banking Oracle lock → commerce checkout), F08-G(distractor), F15-G/T1/f15-t2(multi-root). 강.
- **food 🟡**: f15-t2·F15-T1에서 food는 동시-피해자(co-victim)로만 등장, food발 크로스도메인 결합/무결성 없음.
- **banking 🟡**: lock 전파(F01-P)는 커버되나 **FS-5(검증 비대칭 무결성 우회)** = commerce가 NodePort 30282로 transfer 직행 시 account의 ACTIVE/FROZEN 검사 스킵 → FROZEN 계좌 이체 200 성공(5xx 없는 무결성 위반). 이 강력한 표면에 시나리오 **부재**. C4(banking down → payment @Transactional 전체 롤백, PG성공분 소멸)도 미커버.

---

## 2. 레포 registry vs 러너 서빙 카탈로그 — 드리프트 (심각)

- **개수**: 레포 catalog.json = **64** (F01-R…F15-T4). 러너 `/api/scenarios` = **28**.
- **id 스킴 완전 불일치**: 레포는 F-series(F0x-R/H/P/G, commerce/food/banking+cross-domain 합성). 러너는 `commerce:01`(1) / `food-delivery:01–07`(7) / `plopvape-shop:01–14`(12) / `social-feed:01–06`(6) / `cross-domain:f15-t2`(1). **id 교집합 0.**
- **러너는 재설계 이전(legacy) 카탈로그를 서빙 중**: 28건 중 **plopvape-shop 12 + social-feed 6 = 18건(64%)이 폐기/구명칭 도메인**. `plopvape-shop`은 commerce의 구명칭, `social-feed`는 CLAUDE.md상 "폐기 예정" 레거시. 현행 3도메인 실측 코드(commerce/food/banking)와 매핑 불가.
- **개념적 잔존 overlap**은 `cross-domain:f15-t2`↔레포 F15-T2, `food-delivery:05`↔F06-P, `commerce:01`↔F07-H 정도로 극소.
- **결론 한 줄**: 러너 서빙 엔드포인트는 F-series 재설계본을 반영하지 못했고 pre-redesign 레거시(plopvape/social-feed 위주)를 서빙 중 — 개수(64 vs 28)·id·도메인 전 축에서 near-total 드리프트.

---

## 3. 의심 시나리오 (있으나 코드 근거 약함) — top 5

1. **러너 plopvape-shop:01–14 + social-feed:01–06 (18건)** — 폐기/구명칭 도메인. 현행 3도메인 코드에 대응 없음. 서빙 카탈로그의 최대 오염원.
2. **F03-P (payment Hikari 축소+surge)** — 자연 풀고갈이 아니라 env로 pool을 인위 축소. answer-key가 구조결함(P1)이 아닌 "설정변경"을 가리킬 위험.
3. **F08-P (order→payment read-timeout 250ms 오설정)** — 임의 합성 config 오배포. 실제 구조결함(무한 timeout)의 정반대 값을 주입 — 결함 클래스가 다름.
4. **F02-P (food menus 카테고리 인덱스 드롭)** — order 핫패스의 getMenu는 restaurant_id 조회이지 category 조회가 아님 → 드롭 인덱스가 주문경로 지연에 직결되는지 근거 얕음.
5. **food-delivery:01 (Restaurant Closed Storm)** — status=CLOSED → 4xx 비즈니스 거절. 코드상 biz-reject(장애 아님)를 장애 시나리오로 포장 — 장애 신호 낮음.

---

## 4. 신규 설계 1순위 (코드 근거 확실한데 부재) — top 5

1. **[P6 commerce] user-service fail-close → 전 도메인 쓰기 401** (C3). AuthGuard verify-token CB open → register/login 제외 모든 POST/PUT/DELETE 401. 광역 blast radius·최상 난이도인데 시나리오 0. **최우선.**
2. **[P7 banking] 검증 비대칭 무결성 우회** (FS-5). commerce가 NodePort 30282로 transfer 직행 → account ACTIVE/FROZEN 검사 스킵 → FROZEN 계좌 이체 200 성공. 5xx 없는 무결성 위반(trace만으론 정상과 구분 불가) — 데이터정합 판정축.
3. **[P7/C4 commerce] banking down → payment @Transactional 전체 롤백** — PG 성공 후 banking 이체 실패 시 payment 행까지 소멸("PG는 결제됐는데 payment 없음"). F01-P는 lock만 커버. 드리프트/보상 판정.
4. **[P5 banking·food] silent async lag ready화** — FS-3(ledger 단일스레드 consumer lag)·FS-4(outbox 적체)·FS-10(이벤트 유실) 및 food 아웃박스 정지. 현재 F04-P·F14-P·food outbox 전부 blocked/부재 → ready 시나리오 0. 5xx 없는 원장 지연/드리프트.
5. **[P1 commerce·food] 하류지연 → 기본풀(10) 자연 고갈 오진** (C1/C12, food C1). "order/payment DB가 범인처럼 보이나 진짜 root는 하류 지연"의 대표 오진 함정. F06-R은 external PG를 answer-key로 삼아 풀-고갈 전파 사슬을 검증 못함 → 전용 시나리오 필요.

부수 갭: P2 food/banking thread-saturation, P3 food row-lock 부재(도메인 코드상 hot-row FOR UPDATE 표면 약함은 감안).
