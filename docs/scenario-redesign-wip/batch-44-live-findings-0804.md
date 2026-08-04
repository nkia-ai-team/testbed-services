---
title: 44종 라이브 배치 결함 기록 (2026-08-04 재실행)
status: Draft
owner: project
last_reviewed: 2026-08-04
tags:
  - scenario
  - defect
  - live-batch
summary: 큐 live-44-e7152d6f. 08-03 배치의 수리를 라이브에서 검증하는 재실행이다. 08-03 기록(batch-44-live-findings-0803.md)의 후속.
---

# 44종 라이브 배치 결함 기록 (2026-08-04 재실행)

큐 `live-44-e7152d6f`, 스모크 패스, 13:25 KST 착수.
[08-03 배치](batch-44-live-findings-0803.md)에서 나온 32건을 수리한 뒤의 재실행이다.

이번 배치에서 다시 확인된 것: **정지의 다수는 시나리오가 아니라 배치 기반의 결함이다.**
정지 4회 중 시나리오 결함은 1건뿐이었고, 2건은 배치를 세울 이유가 없는 것이었다.

## 요약

| # | 대상 | 층 | 결함 | 조치 |
|---|---|---|---|---|
| 1 | 러너 readiness | 진단 | 프로브 예외 1회에 배치 정지 + 사유 미기록 | **수리** `5144f04` |
| 2 | 러너 readiness | 패스 계약 | 스모크가 쓰지 않는 캡처 사슬이 배치를 정지 | **수리** `45e7f05` |
| 3 | k8s.resource | 진단 오도 | `deploys` 경고가 무관한 실패에 이름표를 붙임 | **수리** `d5feb95` |
| 4 | k8s.env ↔ k8s.resource | 경합 | companion의 롤아웃을 primary preflight이 못 기다림 | **수리** `96b8d46` |
| 5 | F05-R | **G3 피해 부재** | OOM은 실증됐으나 사용자 피해가 관측되지 않음 | **스킵** |

## 1. readiness 프로브 예외 1회가 배치를 세운다 (수리)

`preflight_signals`는 값이 나빠서가 아니라 **프로브가 예외를 던질 때만** false다.
`build_preflight_checks`는 신호가 하나라도 빠지면 fail-closed로 raise한다.
시나리오별 게이트는 같은 조건을 6회×5분 견디는데(`547f657`) 배치를 세우는 readiness
게이트에는 그 완화가 없었다 — 재시도 0회, 로그 0.

수리: 프로브 단독 블로커면 3분 유예. 삼키던 예외를 `OperationalReadiness.details`로
정지 사유에 싣는다.

**이 결함이 테스트를 통과한 이유**: `make_queue`가 `functional_readiness_enabled`를 꺼서,
운영이 **먼저** 걸리는 층을 테스트가 통째로 비워두고 아래층만 시험하고 있었다.

## 2. 스모크가 캡처 사슬로 정지한다 (수리)

스모크 패스는 캡처를 하지 않는데(`capture_enabled()`), readiness의 `capture_self_check`만
패스와 무관하게 돌아 **이번 패스에서 한 번도 쓰지 않을 119 의존성**으로 배치를 세웠다.
당시 119는 load 9.2 / ClickHouse가 메모리 한도의 80% / ai-observer 245% CPU였다.

수리: 스모크는 이 검사를 걷지 않는다. 데이터셋은 계속 검사하되 즉시 정지가 아니라 3분 유예.

## 3. `deploys` 경고가 진단을 오도한다 (수리)

`auth can-i patch "${kind}s"`가 `deploy`+`s`="deploys"를 만들었다. **검사는 통과한다**
(실측: exit 0, "yes"). 문제는 `profile-control.py:281`이 실패한 preflight의 stderr를
사유로 보고한다는 것 — 이 무해한 경고가 무관한 실패에 매번 엉뚱한 이름표를 달았고,
실제로 #4의 진단이 여기서 한참 샜다.

**기존 테스트가 이 버그를 주장하고 있었다** — `assertIn('can-i patch "${kind}s"')`.
`2df5774`(F25-H 승격)에서 하드코딩 `deployments`를 `${kind}s`로 바꾸자 테스트가
바뀐 결과를 그대로 받아 적었다.

## 4. companion의 롤아웃을 primary가 기다리지 못한다 (수리)

```
06:20:34.9  companion k8s.env       apply -> 06:20:35.8   (testbed-payment env 패치)
06:20:36.7  primary   k8s.resource  apply -> 실패
06:20:37.2  cleanup 실패 -> adaptive_controller_dirty
```

109 실측 — payment에 롤아웃을 유발하고 preflight을 반복:

| 시점 | preflight |
|---|---|
| 정상 상태 | EXIT=0 |
| 롤아웃 직후 +1s / +2s / +3s | EXIT=1 |
| 롤아웃 완료 후(약 58초) | EXIT=0 |

**첫 수리는 역효과였다.** companion의 apply에 대기를 넣었더니 apply가 58초가 됐고,
`profile-control`이 **apply 동안 코디네이터 lock을 쥐고 있어** 하트비트가 30초 리스를
갱신하지 못해 만료됐다("runner lease is expired"). 긴 apply는 구조적으로 불가능하다.

**대기는 preflight에 있어야 한다** — 같은 파일이 이미 적어 두고 있었다:
"Do not hold the coordinator lock while a remote preflight or cleanup runs."
최종 수리: `k8s.env`의 apply는 안 기다린다(주석으로 이유 고정), `k8s.resource`의
**preflight만** `settle=90s`. apply 경로의 `check`는 1초 유지.

> 오래 걸리는 일을 어디에 두느냐는 취향이 아니라 **락 구조가 정한다.**

## 5. F05-R — OOM은 진짜인데 피해가 없다 (스킵, 설계 결정 필요)

배관이 완전히 열린 뒤의 첫 완주. 사다리 3단을 다 밟았고 cleanup·recovery 모두 성공,
dirty 아님. 그런데 `aborted`로 끝났다.

```
768Mi  06:43~06:45   restart=0                 -> escalate (OOM 미도달)
640Mi  06:48~06:49   restart=0                 -> escalate
576Mi  06:53:07      restart=1  OOMKilled      <- 주입 성공
       06:53:55      restart=2  OOMKilled
       06:54:59      restart=3  OOMKilled      -> abort (restart-budget >= 3)

checkout_5xx_rate = 0.0   (전 구간, 단 한 번도 안 움직임)
achieved_rps      = 35    (부하는 정상)
```

성공 조건은 `termination_reason == OOMKilled` AND `restart_count >= 1` AND
**`checkout_5xx_rate >= 0.05`** 셋 모두를 2연속으로 요구한다. 앞의 둘은 충족됐고
세 번째가 끝까지 0이었다.

**G3(서비스 피해 실재) 미충족이다.** payment이 실제로 OOMKill 루프에 들어가는데
체크아웃 5xx가 전혀 반응하지 않는다. 가능성은 둘이고 아직 가리지 못했다:

1. **성공 조건이 잘못된 피해 신호를 지목한다** — 체크아웃 경로가 payment을 동기로
   타지 않거나(비동기 outbox), 재시도·서킷브레이커가 가린다. 그렇다면 payment 자신의
   가용성·지연을 재는 신호로 바꿔야 한다.
2. **정말로 사용자 피해가 없다** — 그렇다면 이 시나리오는 G3를 통과할 수 없고
   Class 재검토 대상이다.

부수 관찰: 성공 창이 구조적으로 좁다. 성공은 `restart_count >= 1`, 중단은 `>= 3`으로
**같은 단조 증가 카운터**를 두고 경쟁하며, 카운터는 런 안에서 내려오지 않는다.
1번으로 판명되면 이 예산도 함께 봐야 한다.

**다음 행동**: 체크아웃 요청이 payment을 동기 호출하는지 코드로 확인 → 1/2 판별 →
성공 조건 재설계 또는 Class 재검토.
