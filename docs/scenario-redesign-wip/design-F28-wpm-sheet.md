# 설계 시트 — F28: WPM 관측 축 (성격 판단 우선 · Class B)

- **과제 근거**: team-lead F28 지시 — "W1(thread-state 감별자)이 전용 시나리오인지, 여러 시나리오의 감별을 강화하는 관측 능력인지 정직하게 판단". 근거 문서 = `fault-surface-infra-network-wpm.md §2`(WPM 복원 실측 3수리점 + W1 후보), `spec-scenario-design-charter.md §2`(골든 4조건), `spec-testbed-design.md:425,455`(WPM/WebURL 관측 계약).
- **실측 기준**: 2026-07-24 commerce/food-delivery/core-banking 3도메인 전 소스 grep + GB10 `/opt/polestar10/wpm` 보존자산 실측(fault-surface 문서 §2.2 인용).

---

## 0. 결론 요약 (먼저)

1. **성격 판단**: **F28은 전용 시나리오가 아니다. WPM javaagent는 "관측 능력 축"이며, 기존 시나리오 6개의 스레드-레벨 감별을 소거법→직접증거로 격상시킨다.** F28-R(전용) 설계는 **하지 않는다** — 실코드에 WPM 신호가 *정답 감별에 필수*인 앵커(스레드 데드락·스레드릭)가 **0개**이기 때문(§1 grep 실증).
2. **감별 강화 매핑**: 기존 카탈로그 **5개 강화**(F21-Q/P 스레드포화, F22-P 풀고갈, F01-R·F06-H·F01-P 락, F20 슬로쿼리) + **1개 정직한 음성(강화 안 됨)**(F09-H GC — STW라 스레드덤프 무의미, §2.6).
3. **WPM 복원 선행조건**: 3수리점(manager_ip→119 재지정 / 이중 javaagent 부착 / banking conf 신설) + 스모크 5단계 게이트. 최대 리스크 = OTel javaagent와의 **이중 바이트코드 계측 충돌**. live validation 통과 전 어떤 golden도 WPM을 must_support로 확정 금지.

---

## 1. 성격 판단 — 전용 시나리오가 없는 이유 (grep 실증)

전용 F28-R이 성립하려면 헌장 §2 조건 ③(감별 가능)에서 **WPM 신호가 없으면 판정 자체가 불가능**한 결함이 실코드에 앵커돼야 한다. 그런 결함의 전형은 (a) 락 순서 역전 데드락(5xx도 없고 풀 지표도 안 움직이고 오직 두 스레드 상호 BLOCKED 스택만이 증거), (b) 스레드릭(`ExecutorService` 미종료 → 스레드 수 단조증가). 3도메인 전 소스를 grep한 결과:

| 표면 | grep 결과 | 함의 |
|---|---|---|
| `synchronized` 블록 | **0건** | 앱코드 모니터 락 없음 → 모니터 데드락 앵커 불가 |
| `ReentrantLock`/`CountDownLatch`/`Semaphore`/`.wait()`/`.join()` | **0건** | 명시적 동기화 프리미티브 없음 → 락순서 데드락 앵커 불가 |
| `ExecutorService`/`Executors.`/`ThreadPoolExecutor`/`@Async`/`TaskExecutor` | **0건** | 앱 관리 스레드풀 없음 → 스레드릭(풀 미종료) 앵커 불가 |
| `CompletableFuture` | **0건** | 비동기 fan-out 없음(전부 동기 RestClient) |
| `new Thread`/`Thread.sleep` | **1건** = `commerce/order-service/.../OrderController.java:70`(합성 sleep) | 헌장이 이미 F03-H "합성 sleep 엔드포인트"로 재앵커 대상 지목 — 진짜 결함 아님 |
| `@Scheduled` | 다수(interest/reconciliation/outbox/cleanup 배치) | 전부 평범한 배치 잡, 스레드 병목 아님 |

**판정 근거**: 이 테스트베드의 유일한 실재 스레드 병목은 **Tomcat 컨테이너 워커 스레드(기본 200)의 자연 포화**(F21-Q/P)와 **Hikari 커넥션 대기**(F22-P)뿐이다. 둘 다 **앱코드 결함이 아니라 요청 점유 구조**에서 나오며, F21/F22 설계 시트가 이미 **별도 게이지(active_requests / hikari_pending / control-account 성공률)의 소거법으로 감별을 완결**했다. 즉 WPM 신호는 그 감별을 **더 직접적으로** 만들 뿐, WPM 없이는 판정 불가인 시나리오는 하나도 없다. → 헌장 조건 ①(코드 앵커)을 만족하는 WPM-전용 결함이 부재하므로 **F28-R 설계는 억지가 되며, 정직 판정은 "전용 시나리오 없음, 관측 능력으로만 가치"**다.

> 대비: fault-surface 문서 §2.4가 F13×3(WebURL phase)을 "계열 오배정 → 재설계 대기"로 판정한 것과 **다른 종류의 판정**이다. F13은 "WPM이 그 신호를 안 준다"(오배정)이고, F28-R은 "WPM이 주는 신호(thread-state)를 *정답의 유일 증거*로 쓸 실코드 결함이 없다"(앵커 부재)다. WPM thread-state 자체는 유효한 신호이며, 그것을 **기존 시나리오에 얹는다**.

---

## 2. WPM 신호 → 기존 시나리오 감별 강화 매핑 (주 산출물)

### 2.1 WPM javaagent가 주는 신호 (spec-testbed-design.md:455 · fault-surface §2.1)

| 신호 | 실체 | 이 매핑에서의 쓸모 |
|---|---|---|
| **thread state 분포** | JVM 전 스레드의 RUNNABLE/WAITING/BLOCKED/TIMED_WAITING 카운트 | "200개 busy"의 *상태별 분해* — 어느 상태에 몰렸나 |
| **thread stack (덤프)** | busy 스레드의 최상위 프레임(socketRead / getConnection / park) | *어디서* 막혔나 — 감별의 결정 증거 |
| **xlog / transaction profile** | 트랜잭션 내부 구간별 소요(SQL 구간·외부 hop 구간) | 슬로쿼리/외부지연 구간 분리 |
| **interaction (JDBC/HTTP intercept)** | JVM 내부에서 본 DB/원격호출 소요 | APM span과 교차검증(단, OTel와 중복 — §3 리스크) |

### 2.2 매핑표 (핵심)

| 시나리오 | 정답(root) | WPM 없을 때 감별 | WPM thread-state 신호 | WPM 있을 때 |
|---|---|---|---|---|
| **F21-Q** (food order Tomcat 200 포화, restaurant 상류지연) | order 스레드 고갈(Hikari 미보유) | **소거법**: `active_requests≈200` **그리고** `hikari_pending≈0`의 *결합*으로 "스레드지 풀 아님" 추정 | 다수 스레드가 **RUNNABLE, `SocketInputStream.read` on restaurant hop** 스택 · getConnection 대기 스레드 **0** | **직접 증거**: 스레드가 하류 read에 잡혀있고 커넥션 대기가 아님이 스택으로 자명 |
| **F21-P** (banking api Tomcat 200 포화, transfer 하류지연) | api 스레드 고갈(풀 자체 없음) | **소거법**: `active_requests≈200` + hikari query가 *null*(풀 부재)이라는 간접 | 다수 스레드 **RUNNABLE on account/transfer socketRead**, JDBC 프레임 전무(api엔 DataSource 없음) | **직접 증거**: 스택에 JDBC가 아예 없음 = "풀이 개념적으로 없는 서비스"가 스택으로 증명 |
| **F22-P** (banking transfer Hikari 15 고갈) | transfer 커넥션풀 고갈 | **소거법**: `hikari_active≈15 & pending>0` 게이지 + control 계좌 이체 동반실패 | 다수 스레드 **WAITING/TIMED_WAITING on `HikariPool.getConnection`(park)** | **직접 증거**: 스레드가 *커넥션 획득*에서 park됨 — F01-P(락대기)와 스택 프레임이 다름 |
| **F01-R** (PG row-lock checkout) | PG 행 락 대기 | **소거법**: DPM lock/wait 세션 + APM latency↑ | 스레드 **RUNNABLE on JDBC `SocketRead`(FOR UPDATE 응답 대기)**, 커넥션은 *보유 중* | **직접 증거**: 스레드가 커넥션을 쥔 채 DB 응답 대기 = 락 경합(풀 고갈 아님) |
| **F06-H** (payment DB row-lock) | payments 행 배타락 | **소거법**: DPM lock 세션 | 위와 동일 — JDBC socketRead 스택 + 커넥션 보유 | **직접 증거**: 락대기 스택 |
| **F01-P** (Oracle 락 cross-domain) | 특정 계좌쌍 락 대기 | **소거법**: DPM Oracle 세션 + hot 계좌만 지연 | 스택 = JDBC socketRead(FOR UPDATE), **단 pending은 낮음** | **직접 증거**: F22-P와의 결정 구분(락대기 vs 커넥션 park) |
| **F20** (slow query) | 느린 SQL | **소거법**: DPM TopSQL `sql_id`/`sql_hash` | **xlog SQL 구간 profile**(스레드가 SQL 실행 구간에 체류) | **직접 증거**: 스레드가 특정 SQL 실행에 오래 체류 |
| **F09-H** (order GC pressure) | GC STW 지연 | KCM/JVM heap·GC pause 메트릭 | **강화 안 됨** (§2.6) | — |

### 2.3 최강 시너지 — "동일 증상, thread-state로 3분할" (F21↔F22↔F01 삼각)

세 시나리오는 증상이 전부 오케스트레이터(order/api)로 수렴하고 심지어 "busy thread 200"까지 겹칠 수 있다. WPM 없이는 세 개의 *서로 다른 게이지*(active_requests, hikari_pending, control 성공률)를 **교차 소거**해야 정답이 갈린다. WPM thread-state 하나면 **같은 스냅샷 안에서 상태 분포로 즉시 3분할**된다:

- **RUNNABLE + 하류 socketRead** (커넥션 미보유/보유 무관, JDBC 프레임 위치로 구분) → 스레드 포화(F21) or 락대기(F01)
- **WAITING on getConnection(park)** → 커넥션풀 고갈(F22)
- **RUNNABLE on JDBC socketRead + 커넥션 보유** → 락대기(F01/F06)

이것이 WPM javaagent의 **유일하고 정직한 고유 가치**다: OTel `http.server.active_requests`는 *몇 개*가 in-flight인지만 세고 *어디서* 막혔는지는 못 본다. WPM thread-state 분포+스택이 그 "어디서"를 직접 준다.

### 2.4 OTel/APM이 이미 주는 것과의 경계 (과대평가 방지)

- APM span은 "order→payment hop이 8s"를 이미 보여준다 → **"무엇이 느린가"는 WPM 없이도 앎.**
- WPM의 증분 = **"그 느린 hop을 기다리는 동안 스레드가 어떤 상태로, 어느 자원에 묶여 있나"** — 즉 *병목 자원의 정체*(스레드 vs 커넥션 vs 락). 이게 F21/F22/F01의 감별 축과 정확히 일치한다.
- 따라서 WPM은 "새 시나리오"가 아니라 **기존 near-twin 삼각(F19-P/F21/F22/F01)의 감별을 소거법에서 직접증거로 올리는 보강재**다.

### 2.5 강화 요약

- **강화되는 기존 시나리오: 6개** — F21-Q, F21-P, F22-P(스레드/풀 삼각), F01-R, F06-H, F01-P(락 삼각), 그리고 F20(슬로쿼리 xlog). 그중 **가장 값진 4개** = F21-Q·F21-P·F22-P·F01-P(동일-증상 삼각의 직접 분해).
- 전부 **"이 신호 없으면 소거법(복수 게이지 교차), 있으면 직접 증거(단일 스레드 스냅샷)"**.

### 2.6 정직한 음성 — WPM이 강화하지 *못하는* 곳

- **F09-H (GC pressure)**: GC major pause는 **Stop-The-World** — 전 스레드가 safepoint에서 동시 정지한다. 이 순간 thread dump는 "모두 멈춤"만 보여줄 뿐 *특정 자원 대기*를 안 준다. GC의 정답 증거는 **KCM/JVM heap·GC pause·allocation rate 메트릭**이지 스레드-스테이트가 아니다. → team-lead 질문의 "F09-H GC=?"에 대한 답 = **WPM thread-state로 강화 불가(오히려 오도 위험)**. 정직하게 매핑에서 제외.
- **네트워크 계열(N1/N3, fault-surface §3)**: 균일 지연은 APM 소거법이 정답 관측이며 WPM thread-state는 증분 없음(모든 스레드가 균일하게 socketRead 대기 → F21과 구분 불가). 제외.

---

## 3. WPM 복원 선행조건 (prerequisite_gate 형식)

F28의 관측 능력은 wpmagent.jar 부착이 선행돼야 하며, 이는 **인프라 복원 작업**이다. golden 확정 전 아래 게이트 전부 통과 필수(spec-testbed-design.md §7 "live validation 전 must_support 확정 금지").

```
prerequisite_gate (F28 WPM 축 공통):
  state: blocked
  required: true
  live_allowed: false
  class: B (인프라 — 자원/에이전트 복원, 코드 앵커 아님)
```

### 3-A. 복원 3수리점 (fault-surface §2.2-2.3 실측)

| # | 수리점 | 현황(실측) | 조치 | 차단성 |
|---|---|---|---|---|
| R1 | **manager_ip 재지정** | conf가 `10.43.234.31`(commerce)/`10.43.232.72`(food) = 폐기된 인-클러스터 ClusterIP | `manager_ip=192.168.230.119`(119 lucida-collector-wpm). pod→119 도달은 관측평면(br0→192.168.230.x) 경유 | **필수 차단요인** (이거 없으면 콜렉터 미도달) |
| R2 | **이중 javaagent 부착** | 현 commerce 매니페스트는 APM javaagent만 부착, WPM 볼륨/2nd javaagent 부재. social-feed에 hostPath 패턴만 보존 | (a) `/opt/polestar10/wpm`→`/opt/wpm` hostPath 볼륨+마운트(readOnly) 추가, (b) `JAVA_TOOL_OPTIONS`에 `-javaagent:/opt/wpm/<svc>/wpmagent.jar -Dwpm.config=...` append | 부착 안 되면 신호 0 |
| R3 | **banking conf 신설** | banking 4서비스 wpmagent.conf **없음**(commerce 5·food 4만 보존) | account/transfer/api/ledger conf 합성(net_collector_udp 31002·tcp 31005는 119와 일치 확인됨) | banking 대상(F21-P·F22-P·F01-P) 관측의 선행 |
| R4 | **msa_group_id 정리**(비차단) | `plopvape-shop-*`(레거시 social-feed 네이밍) | `commerce-*`로 식별자 명료화 | 차단 아님(관측 식별만) |

### 3-B. 스모크 절차 (게이트 5단계 — 순서 강제)

```
G1  TCP 소통      : pod에서 119:31005 TCP 도달 확인(관측평면 라우팅 살아있나)
G2  conf 수리     : manager_ip=192.168.230.119 재지정 후 1서비스(commerce order) conf 반영
G3  파일럿 부착   : order-service 1개에만 2nd javaagent 부착 → pod Ready·기동 안정 확인
G4  수신 확인     : 119 collector-wpm가 order xlog/thread 신호 수신하는지(WPM hash→text dict 해석 포함)
G5  이중계측 검증 : ★최대 리스크 — OTel javaagent와 WPM javaagent가 동일 JVM 바이트코드 이중 계측.
                   기동 오버헤드·클래스변환 경합·HTTP/JDBC 인터셉트 중복으로 트레이스 무결 훼손 여부.
                   G5 통과(기동 안정 + OTel 트레이스 정상 + WPM 신호 수신) 전 golden 확정 금지.
```

**게이트 실패 시 처분**: G1~G4 실패 = 복원 배선 문제(수리 재시도). **G5 실패(이중계측 충돌) = WPM 축 전체 보류** — OTel(정본 관측)을 훼손하면서까지 thread-state를 얻을 수는 없다. 이 경우 F28은 "복원 불가, F21/F22/F01은 기존 소거법 감별 유지"로 정직 강등.

### 3-C. golden 확정 제약 (spec-testbed-design.md:455 WPM=확장 후보)

WPM/WebURL은 spec 표에서 **"확장 후보"**(우선 검증 아님)다. 따라서 §2 매핑의 WPM 신호는 전부 **must_support 후보이되, G5 통과 + live validation(WPM hash 해석·시간창 내 안정 수집)까지 draft**. 강화 대상 6개 시나리오의 *현재* golden은 WPM 없이(기존 게이지 소거법으로) 성립해야 하며, WPM은 통과 후 **감별 강화 레이어로 additive**하게 얹는다(기존 must_support를 대체하지 않음).

---

## 4. 헌장 부합성 평가 (한 문단)

F28은 헌장 §1의 WPM 도메인 커버(6도메인 넓이) 요구에 답하되, **정직 판정으로 "전용 시나리오 없음"에 도달한 것 자체가 헌장 §2 4조건 루브릭의 성과**다: 조건 ①(코드/인프라 앵커)을 WPM-전용 결함으로 채우려면 데드락·스레드릭이 실코드에 있어야 하는데 3도메인 전 소스 grep이 `synchronized`·`ExecutorService`·명시적 락 프리미티브 **전부 0**을 확인했고(유일한 스레드 조작은 헌장이 이미 F03-H로 폐기 지목한 합성 sleep 1건), 앵커 없는 F28-R을 억지로 만드는 것은 §2-A "위장 금지"의 정신에 반한다. 대신 WPM javaagent의 thread-state 신호는 **기존 near-twin 삼각(F21 스레드포화 / F22 풀고갈 / F01·F06 락대기)이 소거법으로 감별하던 것을 단일 스레드 스냅샷의 직접 증거로 격상**시키는데, 이는 헌장 조건 ③(감별 가능)을 *새 시나리오 없이 강화*하는 순수 관측 이득이다. 정직한 한계도 두 개 명시했다: (a) F09-H GC는 STW라 thread-dump가 무의미해 오히려 오도 위험이므로 매핑에서 제외했고, (b) OTel javaagent와의 이중 바이트코드 계측 충돌(G5)이 최대 리스크라 이 게이트를 통과하기 전에는 WPM을 어떤 golden의 must_support로도 확정하지 않는다. 결론: **F28 = Class B 관측 능력 축**이며, 산출물은 전용 manifest/metadata가 아니라 (1) 6개 기존 시나리오 감별 강화 매핑 + (2) wpmagent.jar 복원 3수리점·5단계 게이트 prerequisite다.
