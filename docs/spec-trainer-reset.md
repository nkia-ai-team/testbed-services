---
title: Trainer 초기화(golden 스냅샷 복원) 설계
status: Draft
owner: project
last_reviewed: 2026-07-21
tags:
  - evaluation
  - trainer
  - ai-layer
  - reset
  - snapshot
  - rca
summary: 매 시나리오 주입 전에 AI 학습 계층(trainer/observer)을 "깨끗하게 학습된 알려진 상태"로 되돌려, 케이스끼리 공정 비교가 되고 앞 시나리오의 장애가 뒤를 오염시키지 않게 하는 초기화 계약을 정의한다. 초기화는 "미리 떠둔 golden 스냅샷을 복원"으로 구현하며, time-shift·같은-시간대 제약이 뒤따른다. 메트릭 탐지기(60분 적응형)는 스코프에서 제외한다.
---

# Trainer 초기화(golden 스냅샷 복원) 설계

이 문서는 [평가용 데이터 캡처·재생 설계](spec-eval-data-capture.md)와
[AI Scenario Supervisor 설계](spec-scenario-supervisor.md)가 전제하는 **AI 계층의
재현 가능한 시작 상태**를 정의한다. 캡처 문서가 *입력 데이터*(VM/CH/PG)를 재현
가능하게 고정한다면, 이 문서는 그 입력을 소비하는 *AI의 두뇌 상태*(trainer가
학습한 모델·임계·템플릿)를 매 시나리오마다 동일한 출발점으로 고정한다.

## 1. 목적과 비목표

### 1.1 풀려는 문제

평가 대상인 AI 계층(`lucida-ai-trainer` + `lucida-ai-observer`)은 **살아있는 채로
계속 학습한다.** trainer autoloop이 15~30분마다 forecast·log-anomaly·trace-anomaly
모델을 새 버전으로 갱신한다(§4 실측). 이 때문에 두 가지가 깨진다.

1. **비교 불가(reproducibility).** 시나리오 A를 평가한 AI 두뇌와 시나리오 B를
   평가한 두뇌가 서로 다른 학습 상태다. "같은 입력 → 대상만 바꿔 점수 비교"라는
   평가 공정성 전제가 AI 계층에서 무너진다.
2. **오염(contamination).** 시나리오의 주입된 장애를 trainer가 "정상"의 일부로
   학습해버려, 다음 시나리오의 baseline을 오염시킨다.

### 1.2 목표

매 시나리오 주입 **직전에** AI 계층을 "장애를 본 적 없는, 정상만 충분히 학습한
**알려진 깨끗한 상태**"로 되돌린다(이하 *초기화*). 초기화 후 평가 구간 동안 trainer
학습을 **동결**해 두뇌가 변하지 않게 한다.

### 1.3 비목표

- 원시 입력 데이터(VM/CH/PG 사건 데이터)의 재현은 캡처 문서 소관이다.
- 초기화된 AI가 만든 이상감지·인시던트 판정의 *채점*은 평가 문서 소관이다.
- 메트릭 이상탐지(stream-anomaly)의 초기화는 **불필요**하다(§3.2).

## 2. 핵심 원리 — 왜 "초기화 = golden 스냅샷 복원"인가

"시나리오 전에 trainer를 초기화한다"는 방향은 옳다. 문제는 *초기화*의 구체적
의미이며, 셋 중 하나뿐이다.

| 초기화 해석 | 결과 | 판정 |
|---|---|---|
| **비우기**(빈 상태) | AI가 정상을 몰라 이상탐지 자체가 불가 | ❌ |
| **매번 재학습**(빈 상태→정상 부하로 재적재) | 주기/계절 모델은 며칠+같은 시간대 필요(§4) → 시나리오당 며칠 | ❌ |
| **복원**(미리 떠둔 깨끗한 학습 상태를 덮어쓰기) | 즉시, 매번 동일 상태 | ✅ |

따라서 **"trainer 초기화"와 "golden 스냅샷 복원"은 서로 다른 것이 아니라 같은
것이다.** 깨끗하지만 충분히 학습된 상태는 며칠치 학습의 산물이라 즉석에서 만들 수
없고, 유일한 즉시 조달 경로가 *미리 저장해둔 사본을 되돌리는 것*이다. golden을
과거 시점 T0에 한 번 뜨고 현재 T_now에 재사용하므로 **time-shift 세금**(§6)과
**같은-시간대 제약**(§7)이 뒤따른다 — 이는 설계 선택이 아니라 "며칠치 학습을 얼려
재사용"의 불가피한 귀결이다.

## 3. 스코프 — 무엇을 golden에 담고, 무엇을 제외하나

### 3.1 golden에 담는 것 (초기화 대상)

- **PG trainer/observer 상태 테이블**(§4에 실측 목록·행수). trainer_* 학습
  산출물 + `ai_model_state_snapshots`·`detector_seen_signatures`·
  `log_drain_templates`·`ai_coverages`/`coverage_*` + 모델 레지스트리/바인딩.
- **볼륨 4종**: `lucida_ai-model-store`·`lucida_clickhouse-data`·
  `lucida_vm-data`·`lucida_redpanda-data`.
- **Kafka(redpanda) consumer offset** — 재생 시 소비 위치 정합.

### 3.2 제외 — 메트릭 이상탐지(stream-anomaly)

`stream-anomaly`의 운영 모델은 **사람이 심은 60분 적응형 기본값**이다(§4 실측:
model.json 481B = 설정, `"no trainer, human profile"`, `WindowMinutes:60`,
`AdaptiveBaseline:true`, 콜드스타트 ~5분). trainer는 이 탐지기를 `skipped
— active 보존`으로 두어 덮어쓰지 않는다. 따라서 **깨끗한 정상 리드인 ~1시간이면
자가 회복**하며, 별도 golden 복원이 불필요하다(정상 세그먼트/리드인 계약이 이미
제공). 초기화 machinery의 부담을 이만큼 줄인다.

## 4. 실측 근거 (2026-07-21, 119 라이브)

- **스택**: 119 단일 공유 스택 — `lucida-ai-observer`·`lucida-ai-trainer`·
  `lucida-clickhouse`(24.3)·`lucida-victoriametrics`·`lucida-redpanda` + 수집기
  15종 + vmalert. lucida-next develop-ai 테스트도 이 스택을 공유.
- **볼륨 4종**: `lucida_ai-model-store`·`lucida_clickhouse-data`·
  `lucida_vm-data`·`lucida_redpanda-data`.
- **PG**: `lucida-postgres`(:15432, user=lucida, db=lucida, user table 340개).
- **trainer autoloop 활동**(로그 실측): forecast(conformal/model-route/season)·
  log-anomaly(masking/thresholds/periodic/profiles)·trace-anomaly(trace_threshold)
  를 15~30분마다 새 버전으로 학습. stream-anomaly는 `skipped — active 보존`.
- **학습 상태 규모**(pg_stat_user_tables, n_live_tup):

  | 테이블 | 행수 | 성격 |
  |---|---|---|
  | `trainer_stream_snapshot_history` | 131,592 | 스트림 스냅샷 이력 |
  | `trainer_stream_threshold` | 68,029 | 스트림 임계 |
  | `trainer_trace_threshold` | 12,348 | trace 임계 |
  | `trainer_template_profiles` | 10,533 | 로그 템플릿 프로파일 |
  | `trainer_periodic_templates` | 7,668 | 주기(시간대) 템플릿 |
  | `trainer_masking_rules` | 7,315 | 로그 마스킹 |
  | `trainer_stream_distribution_profile` | 893 | 분포 프로파일 |
  | `trainer_forecast_conformal` | 474 | conformal 예측 |
  | `trainer_forecast_model_route` | 291 | 모델 라우팅 |
  | `trainer_forecast_season` | 291 | 계절성 예측 |
  | `trainer_thresholds` | 1,956 | 임계 |
  | `ai_coverages` | 4,656 | 커버리지 등록 |
  | `coverage_events` / `coverage_signal_state` | 11,936 / 6,367 | 커버리지 상태 |
  | `ai_model_state_snapshots` | 35 | observer 상태 스냅샷 |
  | `detector_seen_signatures` | 175 | 로그 신규성 기억 |
  | `log_drain_templates` | 0 | (현재 빈 채 — 확인 필요) |
  | `ai_model_registry` / `ai_model_bindings` | 9 / 1 | 모델 레지스트리 |

- **model.json**(stream-anomaly): 481B, 사람 프로파일. 07-13 이후 무변경.

> 함정: 원격 psql에 식별자용 큰따옴표(`"pg_catalog"`)를 문자열 리터럴 자리에 쓰면
> 타입 오류로 **빈 결과**가 나온다(리터럴은 작은따옴표). `pg_stat_user_tables`
> 테이블명 컬럼은 `tablename`이 아니라 `relname`.

## 5. 시나리오 1개당 초기화 사이클 (개요)

```
1. 격리 스택을 golden으로 복원   (PG trainer/observer 상태 + 볼륨 4종 + offset)
2. time-shift                    (CH/VM 데이터 시각을 Δ만큼 이동 — §6)
3. trainer autoloop 동결          (평가 구간 학습 정지)
4. 시나리오 주입 → 캡처           (기존 파이프라인)
5. 스택/상태 폐기 → 다음 시나리오는 1부터
```

메트릭 탐지기는 3~4 사이에서 정상 리드인으로 자가 회복하므로 별도 단계가 없다.

## 6. 쟁점 ① — 스택 위치와 에이전트 재배선 (확정: 119 in-place)

**결정(2026-07-21, 사용자): 119 관측 스택을 제자리(in-place)로 snapshot/restore.**
119는 공유 dev 서버가 아니라 **테스트베드 전용 AP(관측 평면)** 이므로, 별도 격리
스택을 신설하지 않고 기존 119 스택을 그 자리에서 초기화한다.

근거·이점:

1. **에이전트 재배선 0** — APM javaagent·KCM·SMS·수집기가 이미 119를 향한다.
2. **CH 머티리얼라이즈드 뷰 문제 자동 우회** — 볼륨(clickhouse-data)을 통째로
   복원하면 base 테이블 + MV 상태가 함께 오므로, per-table `INSERT SELECT`가
   유발하는 MV 재집계 문제(§7.1)가 발생하지 않는다. **쟁점 ②의 벌크 shift 전제와
   일치.**
3. **리소스 적정** — 2차 스택(119 여유 RAM 13GB엔 빠듯) 대신 기존 스택 재사용.
4. **무손실 타이밍** — 복원은 시나리오 사이(큐 정지 구간)에 수행하므로, 그 순간
   스택이 내려가도 잃을 실시간 텔레메트리가 없다.

검토 기록(대안): (B) 전용 최소 스택 신설 — 104(x86·RAM 221GB avail·2.7TB·텔레
메트리 이미 .104:4317 도달)나 신규 VM. 격리는 깨끗하나 에이전트 재배선·팀 조율
비용. 109는 **ARM64 + 여유 RAM 5GB로 제외**(lucida 이미지 전부 amd64).

**남은 확인**: 119의 lucida-next develop-ai 배포가 관측 스택의 누적 데이터(CH/VM/
모델 볼륨)에 의존하지 않는지 — 의존 없으면 in-place 볼륨 롤백이 안전(앱 컨테이너는
별개). **미결 구현**: in-place 복원 중 스택 정지·볼륨 스왑·재기동의 원자적 절차와
소요 시간(§9).

## 7. 미결 쟁점 ② — CH/VM time-shift 구현

golden을 T0에 떴으므로 재생 시 데이터 시각을 Δ=T_now−T0만큼 밀어야 AI baseline이
"지금"과 정합한다. **Δ를 정수 일(며칠) + 같은 요일/시간대로 잡으면** 학습된 계절성/
주기 모델의 위상이 그대로 맞아, 무거운 "모델 내부 시각 이동" 없이 **원시 데이터
버퍼(CH/VM)만 이동**하면 된다.

- **VM**: `/api/v1/export`(JSON-lines, ms 타임스탬프)→timestamps 배열에 +Δ→
  `/api/v1/import`. 삭제는 `/api/v1/admin/tsdb/delete_series`.
- **CH**: `INSERT ... SELECT`로 DateTime64 컬럼 +`INTERVAL Δ`, 신규 테이블 적재.
- **PG TTL**: trainer 상태의 시각/보존 윈도 shift.

**권고: 정수-일 shift(시간대 위상 보존).**

### 7.1 PoC 결과 (2026-07-21, 119 라이브, 무오염 검증)

**결론: time-shift 기법은 VM·CH 양쪽에서 실현 가능(GREEN).** scratch DB·throwaway
시리즈로만 검증하고 즉시 삭제(라이브 데이터 무접촉).

- **VM ✅**: 과거(3일 전) golden 시리즈 import→export→timestamps +3일→import→
  현재 시점 조회에서 값 보존 확인(round-trip 전부 204, delete_series로 정리).
  **발견한 제약: VM은 먼 미래 타임스탬프 샘플을 드롭한다.** 따라서 shift 방향은
  **반드시 과거→현재**(Δ=now−T0, now를 넘겨 미래로 밀지 않음)여야 한다. 실제
  워크플로(golden은 항상 과거 시점)가 이 제약을 자연히 만족한다.
- **CH ✅**: `col + INTERVAL N DAY`가 DateTime64(3/6/9, UTC)에서 동작(scratch
  테이블 3→6행 +3일, 실테이블 `host_connections` 2.28M행 read-only shift 확인).
  서브초 정밀도 보존.
### 7.2 shift의 목적 = 연속성 (2026-07-21 사용자 확정)

**time-shift는 전체 데이터 볼륨(수백만 행)의 재적재가 아니다.** observer는 탐지 시
최근 윈도(stream 60분·log 1분·trace 5분)와 **동결된 PG 모델**만 쓰고(14일/96시간
lookback은 trainer=학습 몫이며 평가 중 동결), 원시 과거 데이터를 재조회하지 않는다.

shift의 실제 목적은 **정상 세그먼트와 부하 주입 시각의 시간 연속성**이다: golden(정상
상태)은 과거 시각에 찍혔고 주입은 지금 일어나므로, 그대로 두면 AI가 보는 타임라인이
"정상(과거) … 공백 … 장애(지금)"로 끊긴다. 따라서 **golden의 정상 구간을 주입 시각
바로 앞에 이어붙여** "정상 → 장애"를 끊김 없는 연속 시간선으로 만든다.

- 범위 = **바운디드 정상 윈도**(수백만 행 전체 볼륨 ✗). §7.1 PoC가 검증한 규모.
- 이미 있는 `assembled/`(정상 prefix를 앞으로 shift해 시나리오 창에 이어붙임,
  [평가용 데이터 캡처 §2.1](spec-eval-data-capture.md))와 **같은 종류의 연산**.
- 이점: 즉시 연속 정상을 제공하므로 **매 시나리오 1시간 라이브 리드인 대기 불필요**
  → 하루종일 연속 실행(§8)에 유리.
- MV 문제(§7.1)는 바운디드 윈도 shift에서는 규모가 작아 관리 가능(전체 볼륨 벌크
  재적재를 안 하므로).

**남은 미결**: golden 정상 윈도의 길이·구성(PG AI상태 + 정상 데이터 범위), 기존
normal-segment/assembled 재사용 여부, PG TTL 정합.

## 8. 쟁점 ③ — 같은-시간대 제약 vs 처리량 (확정: 시간대별 golden 다장, 하루종일 실행)

**결정(2026-07-21, 사용자): 시나리오는 하루종일 연속 실행하고, 시간대별 golden을
여러 장 확보한다.** "야간 창에만 실행"(밤당 2~3개 → 30개 ~2주)은 처리량이 비현실적
이므로 기각한다.

메커니즘:

1. **golden 세트 캡처** — 깨끗한 baseline이 도는 날, 하루의 시간대를 대표하도록
   **여러 시각에 golden을 캡처**한다(예: 2~3시간 간격). 각 golden = 그 시각의
   trainer/observer 상태 + 볼륨 4종 스냅샷.
2. **시나리오 실행 시(임의 T_now)** — T_now의 **시간대(time-of-day)에 가장 가까운
   golden**을 선택해 복원하고, **정수-일 shift**(Δ=whole days)로 그 golden의 캡처
   시점을 오늘 T_now에 정렬한다. 정수-일 shift라 시간대 위상은 보존되고, 최근접
   golden 선택으로 위상 오차를 golden 간격의 절반 이내로 억제한다.
3. 이로써 **낮·밤 구분 없이 큐를 연속 실행**한다(30개 ~1일 규모).

golden 간격(몇 개를 몇 시간 간격으로)은 위상 민감도로 정한다: §3.2·§4에서 확인
했듯 **지배적 메트릭 탐지기는 60분 적응형이라 위상 둔감**하고, 위상 민감한 것은
forecast/log/trace 보조 탐지기다. 따라서 촘촘한 golden(예: 매시)까지는 불필요할
수 있어 **간격은 실측으로 조정**한다(넓게 시작→민감하면 촘촘히).

비용: golden N장 = N × (trainer PG 상태 + 볼륨 4종). 1회 캡처 세트이므로 저장 비용은
감수. **미결**: golden 개수·간격 초기값, golden 세트 저장·버전 관리, 최근접 선택
로직(runner). 검토 기록(대안): (b) 야간 창 = 처리량 비현실적으로 기각.

## 9. 구현 진행

- [x] **golden 캡처 스크립트** `scripts/capture-golden-state.sh` — PG AI-상태 23테이블
      + 정상 윈도(VM/CH) + meta(tod_phase). 109 라이브 end-to-end 검증. (커밋 faff9f7)
- [x] **복원 스크립트** `scripts/restore-golden-state.sh` — 최근접-tod golden 선택 →
      trainer 동결(docker stop) → PG AI-상태 `pg_restore --clean` → observer 재기동 →
      health 대기. `--thaw`로 평가 후 해동. **로직 검증 완료**(선택·circular·sha 가드·
      dry-run). **라이브 변경 경로(119 freeze/restore/restart)는 미검증** — 실증에 119
      관측 스택 순간 정지 수반, 안전 창 필요.
- [x] **time-shift PoC** — VM export/import·CH INTERVAL 검증(§7.1). 단, 경량 방식
      확정으로 **라이브 복원 경로에는 볼륨 shift 불요**(라이브 baseline이 연속성 제공,
      §7.2). shift는 캡처 케이스 연속성(assembled/)에 국한.
- [x] **복원 스크립트 라이브 검증** — 119 end-to-end 통과(freeze→data-only 복원→
      observer 재기동→health→thaw). 첫 `--clean` 시도가 제약 유실 인시던트를 냈으나
      104 레퍼런스로 완전 복구, data-only(FK-safe)로 전환(커밋 d085984).
- [x] **runner 통합** — `live_queue.py` `_tick_running`(주입 직전 `_restore_golden`,
      실패 시 pause)·`_tick_capture`/`_pause`/`_complete`(`_thaw_trainer` fail-open).
      **기본 OFF 플래그 `GOLDEN_RESET_ENABLED`** 뒤(활성화 전 동작 불변). 신규 테스트
      4종, 전체 183 passed. rca-scenario-runner PR #20 브랜치 커밋 bd0500e.
- [ ] **활성화(배포)**: (1)`restore-golden-state.sh`를 119 `/opt/lucida/`에 배치,
      (2)golden 세트 캡처(시간대별 N장 → `/data/eval-cases/goldens`), (3)109 runner env
      `GOLDEN_STORE_ROOT`·`RESTORE_SCRIPT`·(준비 시)`GOLDEN_RESET_ENABLED` 설정 +
      runner 이미지 재빌드, (4)큐 기동 시 실전 end-to-end 검증(시나리오 주입 수반).
- [ ] **preflight 편입**: "golden 복원 정합"(observer health·trainer 동결 확인).
- [ ] golden 윈도 길이·필터 튜닝(VM 138MB/5m → 120m ~3GB) + restore 회귀 test-*.sh.

## 10. 미해결 질문

- `log_drain_templates`가 0행인 이유(비활성? 다른 저장?) — 로그 신규성 탐지 경로 확인.
- golden 시점 선정 기준(어느 정상 구간을 "가장 깨끗한 학습 상태"로 볼지).
- ~~trainer 동결 방법~~ → **해소: `docker stop lucida-ai-trainer`**(restart-policy=no라
  멈추면 유지, `docker start`로 해동). autoloop 제어 API·pause env 없음(AUTOLOOP_AGENTS
  env는 있으나 변경 시 컨테이너 recreate 필요). observer는 restart-policy=always·health
  endpoint로 readiness 확인.
- golden 개수·간격 초기값(위상 민감도 실측 후 결정).
- 격리 스택에서 수집기 15종 중 실제 필요한 최소 집합.
