---
title: 시나리오 설계 헌장 (Draft)
status: Draft — 검토 대기
owner: project
last_reviewed: 2026-07-23
summary: RCA 평가용 시나리오를 "넓이(6도메인 다양성) + 깊이(코드/인프라 근거 4조건)"로 일관되게 설계하기 위한 기준서. 기존 spec-scenario-design.md의 다양성 규칙을 계승하되, (1) 양성/음성 균형을 폐지하고 진짜 장애만 담으며, (2) 깊이 4조건 루브릭을 신설한다.
---

# 시나리오 설계 헌장

## 0. 이 문서가 고치는 것

기존 64개 시나리오 전수 감사 결과: 코드 근거가 탄탄한 것은 30%(19)뿐, 55%가 미완성/얕음, 16%가 근거 단절이었다.
근본 원인은 **"만드는 기준(깊이)이 없었던 것"**. 다양성(넓이) 규칙은 이미 있어 6도메인을 다 채웠으나,
각 칸을 채우는 방식의 엄밀함을 강제하는 규칙이 없어 얕거나 깨진 채로 승격됐다.

이 헌장 = **넓이(기존 계승) + 깊이(신설)**.

## 1. 넓이 — 다양성 (기존 계승, 1건 수정)

시나리오는 6개 관측 도메인에 퍼져야 한다 (spec-scenario-design.md §2 계승):

| 도메인 | 잡는 것 | 본 헌장의 클래스 |
| --- | --- | --- |
| APM | 앱 코드·endpoint·호출 지연 | **Class A (코드)** |
| DPM | DB — 느린SQL·락·커넥션풀 | **Class A (코드)** |
| SMS | 호스트 — CPU·메모리·디스크 | **Class B (인프라)** |
| NMS | 네트워크 — 패킷손실·인터페이스 | **Class B (인프라)** |
| KCM | 컨테이너/pod — OOM·재시작·노드 | **Class B (인프라)** |
| WPM | 웹 프로브 — URL·브라우저 경로 | **Class B (인프라)** |

**[수정] 양성/음성 균형 규칙 폐지.** 음성(no-incident guardrail) 시나리오는 카탈로그에서 제외한다.
카탈로그는 **진짜 장애(real incident)만** 담는다. (2026-07-23 결정)
→ 개수 목표는 "도메인당 양성 N개"만 남고 "음성 M개"는 삭제.

## 2. 깊이 — 골든 4조건 (신설)

모든 시나리오는 다음 4조건을 **전부** 충족해야 golden으로 승격한다. 하나라도 빠지면 draft.

1. **코드/인프라 앵커** — 증상이 나는 메커니즘이 실제 근거에 박혀 있다.
   - Class A: 앱 코드 `file:line` (7 결함 패턴 P1~P7 중 하나에 매핑).
   - Class B: 인프라 지점 (자원·노드·NIC·probe)과 그 조작 수단.
2. **정답(answer-key) 작성** — `root_cause`(target_kind/id/mechanism)가 scenario-metadata에 실재하고, 실제 코드/인프라 거동과 일치한다.
3. **감별 가능** — 유사 시나리오와 구분되는 증거(`must_support`/`must_rule_out`)가 있다. 예: 외부 hang(F06-R) ↔ 내부 DB락(F06-H).
4. **주입 수단 실재** — executor가 껍데기(inert stub)가 아니라 실제로 결함을 만든다.

### 2-A. Class A / Class B 분리 규칙
- 두 클래스는 **섞지 않는다.** 각 시나리오는 생성 시점에 A 또는 B로 라벨링.
- Class A는 "이 코드/설정 때문에"가 정답. Class B는 "이 자원/노드/링크 때문에"가 정답.
- env로 코드 동작을 인위 왜곡(예: 풀 크기 강제 축소, timeout 임의값)하는 것은 **Class A로 위장 금지** — 자연 고갈이 아니면 Class B(설정/운영 결함)로 명시하거나 폐기.

### 2-B. 7 코드 결함 패턴 (Class A 앵커 사전)
- P1 트랜잭션 안 동기 원격호출 → 하류지연이 caller DB풀 고갈 (증상=caller, 원인=downstream)
- P2 `@Transactional(timeout)` 부재 + 기본 풀(Hikari 10~15 / Tomcat 200)
- P3 무제한 비관적 row-lock (NOWAIT/timeout 없음) → 상류 502
- P4 Resilience4j 불균일 — 정작 터지는 외부 hop(PG/banking client)에 CB 없음
- P5 Silent async (outbox→Kafka `send().get()`) → 동기 200인데 데이터 드리프트/lag, 5xx 없음
- P6 Fail-close 인증 확산 (user-service CB open → 전 도메인 쓰기 401)
- P7 크로스도메인 결합/무결성 (namespace 밖이 root)

## 3. 기존 64개 처분 (새 규칙 적용)

| 처분 | 수 | 정의 |
| --- | ---: | --- |
| **KEEP** | 14 | 진짜 장애 + 4조건 충족(✅). 그대로 golden. |
| **FIX** | 30 | 진짜 장애지만 4조건 중 일부 미달(🟡). 보강 대상. |
| **CUT** | 20 | 음성 13 + 근거단절/수단없음 7. 폐기. |

### 3.1 KEEP (14) — 골든 기준선
F01-R, F01-H, F01-P, F04-R, F06-R, F06-H, F07-P, F08-H, F08-G, F11-R, F12-H, F15-G, F15-R, F15-T1
- 이 14개가 "잘 만든 시나리오는 이렇게 생겼다"의 본보기. 나머지 재설계의 기준선.

### 3.2 FIX (30) — 보강 후 승격
빠진 조건별로:
- **정답 미작성/blocked**: F02-H, F03-R, F04-H, F04-P, F14-H, F14-P, F14-R, F15-P, F15-T3, F15-T4 → answer-key 작성 + injector 확보
- **Class B로 재라벨(인프라, 정당)**: F05-R/H/P(KCM), F09-R/H/P(SMS), F10-R/H/P(SMS·DPM-IO), F13-H/P/R(NMS·WPM) → filler 아님, Class B로 정식화
- **env 인위 → 자연화 또는 재라벨**: F03-P(풀 강제축소), F08-P(timeout 방향 반대)
- **food-429 착각 수정**: F06-P, F15-H, F15-T2 → food는 429 없음(503). root 재정의
- **앵커 얕음**: F02-R(인덱스 미사용), F03-H(인위 sleep), F07-H(부하=원인, 코드앵커 0)

### 3.3 CUT (20) — 폐기
- **음성 13**: F01-G, F02-G, F03-G, F04-G, F05-G, F06-G, F07-G, F09-G, F10-G, F11-G, F12-G, F13-G, F14-G
- **근거단절/수단없음 7**: F02-P(인덱스 인과단절), F07-R·F08-R(정답·주입경로 없음), F11-H(수단 불일치), F11-P(주입표면 없음), F12-R·F12-P(네트워크 executor 껍데기)

## 4. 신규 설계 백로그 (코드 근거 확실, 지금 없음)

| 우선 | 시나리오 | 도메인 | 클래스/패턴 | 비고 |
| --- | --- | --- | --- | --- |
| 1 | user-service fail-close → 전 도메인 쓰기 401 | commerce | A / P6 | 카탈로그 전체에 P6 0개 |
| 2 | commerce→transfer 직행 무결성 우회(FROZEN 스킵) | cross | A / P7 | 5xx 없는 무결성 위반 |
| 3 | banking down → payment @Transactional 전체 롤백 | cross | A / P7+P1 | 성공 카드결제까지 소멸 |
| 4 | outbox relay 정지 → 원장/알림 silent lag (실주입) | banking·food | A / P5 | 외부 제어점 injector 필요 |
| 5 | food 실코드 결함 3종 (롱tx 풀고갈·배차정지·PG연쇄) | food | A / P1·P3·P4 | food 도메인 code-anchor 빈약 |

## 5. 능력 갭 (설계 아닌 인프라 — 별도 트랙)
- **P5 injector 부재**: outbox relay가 앱 내 @Scheduled라 외부 제어점 없음 → F04-H/P, F14-P/R가 blocked. 제어 훅 신설 필요.
- **NMS/WPM executor 껍데기**: F12(네트워크)·일부 F13은 inert stub. 물리 NIC/패킷로스 주입 수단 없으면 Class B로도 실현 불가.
- **answer-key 커버리지**: scenario-metadata.json 31/64. FIX 대상 다수가 정답 미작성.

## 6. 오픈 이슈 (검토 시 결정 필요)
- KEEP 14개 중 F08-G·F15-G는 -G 접미사지만 실장애(distractor·multiroot)라 유지. 접미사 규칙 재정의 필요.
- Class B 재라벨 대상(F05·F09·F10·F13, 총 12개)을 몇 개까지 유지할지 — SMS/NMS/KCM/WPM 도메인 최소 커버 수량 목표 재설정 필요(§1 개수 목표 공백).
- food/banking의 Class A 실현 = 능력 갭(injector) 해소에 종속.
