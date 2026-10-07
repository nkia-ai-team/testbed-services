# 관측 평면 분리 — baseline이 도메인별 라이브 문서를 발행한다

2026-07-29 작성. 정본. 관련 = `spec-scenario-load.md`(부하 규칙), `spec-scenario-controller.md`(관측 계약).

## 1. 무엇이 문제였나

`loadgen.*` 관측은 **시나리오가 자기 손으로 돌린 k6**에서만 나온다.

- `load_north_south_executor.py`의 monitor가 k6 샘플을 파싱해
  `/tmp/rca-scenario-<ID>-live.json` 하나를 쓴다.
- 러너의 `_loadgen_live_document()`(`backend/app/live_probes.py:1461`)는 그 파일만 읽고,
  문서의 `scenario_id`·`scenario_tag`가 **지금 도는 시나리오와 정확히 일치**할 것을 요구한다.
- 그 파일은 시나리오 k6가 도는 동안에만 존재한다(`cleanup`이 `rm -f`).

따라서 관측 가능 범위가 주입 대상과 한 몸이 된다:

> 어떤 도메인의 사업 결과(주문 5xx, 이체 2xx …)를 보려면
> **그 도메인에 시나리오가 직접 k6를 부어야만 한다.**

부작용 세 가지가 실제로 관측됐다.

1. **F15-G·F15-T3·F15-T4가 막혔다.** 부하를 붓지 않는 도메인의 사업 결과를 봐야 하는데 볼 방법이 없다.
2. **surge 스크립트가 증폭기가 아니라 관측 수단이 됐다.** 헌장은 surge를 "평상시 위에 얹는 증폭"으로
   정의하는데, 관측을 얻으려고 부하를 붓는 일이 생긴다.
3. **preflight `baseline-business-success`가 DB를 직접 질의한다.** 상시 도는 baseline이
   아무 문서도 남기지 않아 우회로가 필요했다(2026-07-29 신설).

24시간 상주하는 baseline 부하(tb-runner `loadgen-commerce`/`-food`/`-banking`)는
`k6 run`만 하고 샘플을 버린다 — 컨트롤러 눈에 존재하지 않는다.

## 2. 원칙

**관측 평면은 주입 평면과 분리한다.** 어느 도메인에 부하를 부었는지와 무관하게,
세 도메인의 사업 결과를 항상 읽을 수 있어야 한다.

## 3. 설계

### 3.1 baseline이 도메인별 라이브 문서를 발행한다

각 baseline 유닛이 k6 샘플을 FIFO로 흘리고, 상주 monitor가 이를 파싱해 도메인 문서를 쓴다.

```
/tmp/rca-baseline-commerce-live.json
/tmp/rca-baseline-food-delivery-live.json
/tmp/rca-baseline-core-banking-live.json
```

문서 스키마는 시나리오 문서와 **동일**하다. 신원 필드만 다르다.

| 모드 | 신원 필드 |
|---|---|
| 시나리오 | `scenario_id`, `scenario_tag` |
| baseline | `domain`, `unit` |

**FIFO를 쓰는 이유**: 상주 유닛은 1시간짜리 k6 run을 무한 반복한다. `--out json=<파일>`이면
샘플 파일이 시간당 수 GB로 자라 tb-runner 디스크를 채운다. FIFO는 소비되는 즉시 사라진다.

**monitor는 EOF에서 재개방한다.** k6가 매시 재시작해도 monitor는 살아 있다. k6 재기동 몇 초
동안 문서가 갱신되지 않지만, 30초 신선도 계약이 이를 흡수한다. k6가 30초 넘게 죽어 있으면
문서는 stale이 되고 관측은 **fail-closed**로 거절된다 — 의도된 동작이다.

### 3.2 monitor는 정본 하나다

지금 monitor는 `load_north_south_executor.py` 안에 heredoc으로 박혀 있다. baseline용으로
복사하면 **손으로 관리하는 사본이 둘**이 된다. 2026-07-29에 F06-P의 유일한 성공 조건을
매 tick 실패시킨 원인이 정확히 그 패턴이었다(필드맵과 가드 allowlist가 갈라짐).

→ `scripts/scenarios/profiles/loadgen_monitor.py` 하나로 분리하고,

- 시나리오 경로는 executor가 그 파일 내용을 **읽어서 원격 스크립트에 주입**한다
  (ssh stdin 한 번으로 전송되는 기존 구조 유지, 별도 배포 의존 없음).
- baseline 경로는 tb-runner에 배포된 같은 파일을 직접 실행한다.

두 경로가 같은 소스를 쓴다.

### 3.3 지표 계산은 바꾸지 않는다

30초 창, `business_step`/`read_step` 태그 기준 상태코드 분해, `achieved_rps` 산출 모두
기존 로직 그대로다. 이 변경은 **누가 언제 문서를 쓰는가**만 바꾼다.

### 3.4 core-banking baseline에 step 태그를 추가한다

commerce·food-delivery baseline은 이미 `tags: { journey, step }`을 단다.
**core-banking baseline만 태그가 없어** 사업 지표를 계산할 수 없다.
`domain_profiles` 계약(`business_step: transfer`, `read_step: get`)에 맞춰 태그를 단다.

### 3.5 러너는 `domain` 파라미터로 도메인 문서를 읽는다

새 관측 ID를 만들지 않는다. 만들면 10지표 × 3도메인 = 30개 ID가 생기고 필드맵이 또 갈라진다.

```
loadgen.checkout_5xx_rate                          → 시나리오 자기 문서 (기존 동작)
loadgen.checkout_5xx_rate {domain: "commerce"}     → baseline 도메인 문서 (신규)
```

- `LOADGEN_FIELDS`가 계속 유일한 필드 정본이다.
- `allowed_parameters`에 `domain`을 추가한다. 값은 세 도메인으로 allowlist.
- 파라미터가 없으면 기존과 완전히 동일 — **live 43종은 손대지 않는다.**

## 4. 이 설계가 푸는 것

- F15-G·F15-T3·F15-T4의 차단 사유가 사라진다.
- surge가 헌장대로 증폭기로 돌아간다.
- `baseline-business-success`가 DB 직접 질의를 그만둘 수 있다(후속 작업).

## 5. 배포 시 유의

- baseline `script.js`·`entrypoint.sh`는 상주 유닛 소유물이라 `deploy-live-promotions.sh`가
  기존에는 배포하지 않았다. 이제 monitor와 entrypoint를 배포 대상에 포함하고,
  **적용에는 유닛 재시작이 필요하다**(`systemctl restart loadgen-*`).
- 유닛은 tb-runner에서 k6를 컨테이너 없이 직접 실행한다
  (`ExecStart=/bin/sh /opt/loadgen/<domain>/entrypoint.sh`, `User=nkia`).
  따라서 `/tmp/rca-baseline-*-live.json`은 러너의 기존 `ssh nkia@tb-runner cat` 경로로 그대로 읽힌다.
