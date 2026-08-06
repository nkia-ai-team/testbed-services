---
title: 44종 라이브 배치 결함 기록 (2026-08-04 재실행)
status: Draft
owner: project
last_reviewed: 2026-08-06
tags:
  - scenario
  - defect
  - live-batch
summary: 큐 live-44-e7152d6f. 08-03 배치의 수리를 라이브에서 검증하는 재실행이다. 08-03 기록(batch-44-live-findings-0803.md)의 후속.
---

# 44종 라이브 배치 결함 기록 (2026-08-04 재실행)

큐 `live-44-e7152d6f`, 스모크 패스, 13:25 KST 착수.
[08-03 배치](batch-44-live-findings-0803.md)에서 나온 32건을 수리한 뒤의 재실행이다.

이번 배치에서 다시 확인된 것 둘.

**하나, 정지의 절반은 시나리오가 아니라 배치 기반의 결함이다.** 러너·실행기 결함이
5건이고 그중 3건(#1·#2·#8)은 애초에 배치를 세울 이유가 없는 것이었다.

**둘, 어제까지 "주입이 안 된다"였던 것들이 이제 한 층 아래에서 걸린다.** 스킵 4건은
전부 주입이 실증된 뒤의 문제다 — 피해가 없거나(#5·#6·#10), 피해는 있는데 판정이
못 받아준다(#9). 배관을 고치자 설계 질문이 드러난 것이고, 이것이 배치를 결함 발견
수단으로 돌리는 이유다.

## 요약

| # | 대상 | 층 | 결함 | 조치 |
|---|---|---|---|---|
| 1 | 러너 readiness | 진단 | 프로브 예외 1회에 배치 정지 + 사유 미기록 | **수리** `5144f04` |
| 2 | 러너 readiness | 패스 계약 | 스모크가 쓰지 않는 캡처 사슬이 배치를 정지 | **수리** `45e7f05` |
| 3 | k8s.resource | 진단 오도 | `deploys` 경고가 무관한 실패에 이름표를 붙임 | **수리** `d5feb95` |
| 4 | k8s.env ↔ k8s.resource | 경합 | companion의 롤아웃을 primary preflight이 못 기다림 | **수리** `96b8d46` |
| 5 | F05-R | **G3 피해 부재** | OOM은 실증됐으나 사용자 피해가 관측되지 않음 | **스킵** |
| 6 | F09-R | **G3 피해 부재** | 노드를 100% 태워도 동거 서비스가 멀쩡함 | **스킵** |
| 7 | db.lock (Oracle) | 주입 부재 | 값이 파드까지 안 가 잠금이 한 번도 안 걸림 | **수리** `4398722` |
| 8 | 러너 preflight | 진단 | #1의 형제 — 시나리오 경로도 예외 1회에 정지 | **수리** `2e611fe` |
| 9 | F01-P | **판정 계약** | 주입·피해 모두 실증됐으나 성공 신호가 표본 부족으로 요동 | **수리** `c887e20` (hold 600→900) |
| 10 | F03-P | **G3 피해 부재** | 커넥션 풀을 2로 줄여도 지연이 오히려 내려감 | **스킵** |
| 11 | F05-P | **임계가 분포 밖** | 회복 조건이 노드 평시(50%)보다 낮은 40%를 요구 → 거짓 전역 DIRTY | **수리** `c887e20` (회복 <60, 감별자 <40) |
| 12 | F15-T1·F15-P·F14-P | **죽은 신호** | 부하를 만드는 프로파일이 없는데 판정이 부하 산출물을 읽음 | **스킵 + 가드** |
| 13 | F18-P | **자기 주입이 자기 감별자를 켠다** | maxSurge=0이라 env 변경이 곧 파드 unready | **재설계** `e1f17b6` (app.control) |
| 14 | F19-P | **레버가 못 만드는 증상을 요구** | 5xx는 94%인데 성공이 커넥션 풀 대기를 함께 요구 | **수리** `c887e20` (AND 강등) |
| 15 | F19-S | **레버가 못 만드는 증상을 요구** | 5xx는 95%인데 성공이 payment 자신의 오류율을 함께 요구 | **수리** `c887e20` (AND 강등) |
| 16 | F16-H | **여정에 없는 필드를 안전 관측이 읽음** | 부하는 도는데 checkout 필드가 없어 52틱 전부 사용 불가 → 안전 중단 | **스킵** |
| 17 | F20-R | **엣지에서 빨리 실패해 하류가 안 느려짐** | entry 502인데 order·payment p95는 0.0(무트래픽) | **스킵** |
| 18 | F20-Q | **느려지는 게 아니라 죽는다** | 메모리 임계는 넘겼으나(어제 수리 검증) 파드가 죽어 지연이 0 | **재설계** `c1a52be` (5xx 피해·min_hold 30s) |
| 19 | F25-H | **레버가 실사용의 2.6배** | PG는 124MiB 쓰는데 fault limit이 320Mi → OOM 불가 | **재설계** `9e23a9a` (256→192→128Mi 사다리) |
| 20 | F15-R | **부하 live.json 조기 소멸(재발)** | 23분 런의 막판에 파일이 사라져 안전 관측 상실 → 중단 | **스킵** |
| 21 | F03-H | **레지스트리↔실행기 드리프트** | 07-28 수리가 실행기에만 들어가 주입·정리 모두 거부 → 전역 DIRTY | **수리** `b4b000e` |
| 22 | F03-H | **레버가 포화에 못 미침** | 수리 후 주입은 성공, 60rps에서도 p95 77.5ms | **스킵** |
| 23 | F06-P | **성공↔감별자 상호배타** | 429가 5xx로 승격되고 처리량이 무너져 감별자 2개 동시 발화 | **스킵** |
| 24 | F02-H | **인과 부재 실측 확정** | 디스크 81% 포화인데 order p95 41.9ms · payment 24.2ms | **파킹** `c887e20` |
| 25 | F10-H | **같은 레버가 노드마다 다르다** | io-20000이 tb-w1에선 81%, tb-w3에선 64%(임계 70 미달) | **스킵** |
| 26 | F10-P | **느려지는 대신 죽고, 세 신호가 동시에 서지 않는다** | 주입·피해 실증, joint streak 최고 2/3 — p95가 무트래픽 0.0으로 리셋 | **재설계** `c1a52be` (2xx 붕괴 성공) |
| 27 | F21-Q | **과주입 — 입구가 먼저 죽는다** | cpu 3워커가 평시 load 3 노드를 99.97%로 태워 entry_status=0 abort | **파킹** `c887e20` |
| 28 | F21-P | **성공↔감별자 상호배타 재확인 + 러너 streak 구멍** | 롤아웃 과도기 증거만으로 min_hold 직후 1틱에 감별자 발화 | **수리** 러너 `0eec7ed`(미배포) + **파킹** `c887e20` |
| 29 | F09-H | **레버가 지표를 반대로 움직인다** | 힙을 208→160으로 줄이자 old_gen_ratio가 0.477→0.324로 내려가 감별자 바닥(0.35) 아래로 | **스킵** |
| 30 | F09-P | 환경 — 주입 전 blocked | 16:53 baseline-business-success 체크 실패, babysitter 재시도 대기 | **관찰** |

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

## 6. F09-R — 노드를 다 태워도 동거 서비스가 멀쩡하다 (스킵, 설계 결정 필요)

#5와 같은 계열이다. 사다리를 끝까지 밟고 `calibration_levels_exhausted`로 실패.
cleanup·recovery 성공, dirty 아님.

```
사다리  noisy-50(2워커) -> noisy-75(3) -> noisy-100(4, tb-w1 전 코어)

node_cpu_util = 99.6 ~ 99.99   (성공 임계 85. 정리 후 5.8로 복귀)
order_p95     = 16 ~ 111 ms    (성공 임계 300)
pricing_p95   = 1.6 ~ 6.9 ms   (성공 임계 300)
achieved_rps  = 20
```

**어제 교체한 노드 CPU 관측이 정확히 동작하는 것은 이 런이 증명한다** —
주입 중 99.9, 정리 후 5.8. `kcm.node.cpu_utilization`이었다면 못 봤을 값이다.

문제는 주입이 아니라 인과다. 노드가 100% 포화인데 같은 노드의 order p95가 오히려
93ms -> 16ms로 **내려간다**. CPU requests가 cgroup 몫을 보장하고 부하가 20rps로 낮아
서비스가 굶지 않는다. 레버를 더 올릴 곳도 없다(코어를 이미 다 씀).

선택지:

1. **동거 서비스가 실제로 굶는 조건을 만든다** — 부하를 올려 여유를 없애거나,
   requests를 낮춰 보장 몫을 줄인다. 후자는 배치 고정 계약을 건드리므로 신중해야 한다.
2. **성공 조건을 노드 계층의 피해로 바꾼다** — p95 대신 스케줄링 지연·throttling 같은
   신호. 다만 그것이 "사용자 피해"인지는 G3 기준으로 다시 물어야 한다.
3. **Class 재검토** — 이 배치 고정(도메인당 워커 1개, requests 보장) 아래서는
   noisy neighbor가 성립하지 않는다고 판정한다.

#5와 함께 **"주입은 진짜인데 피해가 없다"** 계열로 묶어 한 번에 판단하는 것이 좋겠다.

## 7. Oracle 잠금은 한 번도 걸린 적이 없었다 (수리)

F01-P의 감별자 `injected-lock-absent`(tagged_db_sessions == 0)가 첫 틱부터 485초
내내 걸렸다. 파드 안이 증거였다:

```
/tmp/$tag.sql     <- 파일 이름에 변수가 안 풀렸다
/tmp/$tag.log     SP2-0310: unable to open file "/tmp/$tag.sql"
sqlplus 프로세스   없음

$tag.sql:  set current_schema='';  set_identifier('');  select '' from '' where ''=''
```

원격 heredoc이 로컬 변수 이름을 폈고, 파드 셸에 그 이름이 없어 전부 빈 문자열이 됐다.

**더 나쁜 것은 배관 전체가 초록불이었다는 점이다.** `alive`/`stop`은 로컬이 만든
`/tmp/${tag}.pid`를 보는데 주입은 `/tmp/$tag.pid`에 썼다 — preflight·cleanup·recovery가
모두 통과했다. 이걸 잡은 유일한 장치가 감별자다. **감별자가 없었다면 통과로
기록됐을 것이다.**

수리: 값을 `env`로 파드에 넘기고, 식별자에서 따옴표를 제거했다(원래 SQL은
`select 'id' from 'accounts'` 꼴이라 값이 채워졌어도 틀렸다).

## 8. #1의 형제 — 시나리오 preflight도 예외 1회에 정지했다 (수리)

`5144f04`가 readiness 게이트를 고쳤는데 같은 모양이 시나리오 경로에 두 군데 더 있었다.
17시경 그대로 터졌다: `preflight probe failed for F01-P: [Errno 104] Connection reset
by peer`. 정지 직후 재보니 119는 멀쩡했다(CH=200, VM=200) — 이날만 세 번째 119 딸꾹질.

비대칭이 핵심이다. **더러운 판정**은 이미 6회 × 5분을 기다리는데 **예외**는 첫 번째에
즉시 정지였다. 둘 다 "지금은 판단할 수 없다"인데 한쪽만 참았다.

수리: `_probe_error_wait`로 같은 예산을 쓰게 했다. 예산을 소진하고도 깨져 있으면
그때는 정지한다(더러운 판정이 소진 시 **스킵**하는 것과 다르다 — 죽은 프로브는
그 시나리오만의 문제가 아니라 환경 장애다). cycle 경로에도 같은 자리가 남아 있다.

**여기서도 기존 테스트가 옛 동작을 주장하고 있었다** — 이날 세 번째다(#3, #4, #8).

## 9. F01-P — 주입도 피해도 진짜인데 성공 신호가 요동한다 (스킵)

#7 수리 후 재시도. **주입이 걸렸다**(`tagged_db_sessions=1`, 10분 유지) 그리고
**피해도 압도적이다**:

```
entry_status      = 502          (10분 내내)
checkout_5xx_rate = 0.89 ~ 1.00  (성공 임계 0.05)
payment_error_rate= 33 ~ 100     (성공 임계 10)
```

그런데 `must_rule_out_detected`로 abort했다. 원인은 두 겹이다.

**(a) 성공 신호 하나가 표본 부족으로 0까지 떨어진다.** 08:23:03 틱에서
`payment_error_rate = 0.0`(usable·fresh, 진짜 값). 값들이 33.3 / 50 / 42.8 / 66.7 /
80 / 100 / 75 / 0 처럼 움직이는데, 게이트웨이가 502로 막아 payment까지 닿는 트레이스가
몇 건뿐이라 분모가 무너진 비율이다. 같은 시각 `checkout_5xx_rate`는 0.99로 흔들리지
않는다. success는 두 조건을 **3연속** 요구하므로 이 한 번의 0이 streak을 리셋한다.

**(b) min_hold 이후 남는 창이 2분뿐이다.** `min_hold 8m` / 주입 `hold_seconds 600`.
streak은 375초에 이미 3에 도달했지만 그때는 min_hold 중이라 선언되지 않았고,
min_hold가 끝난 485초부터 잠금이 스스로 풀리는 610초까지 125초 안에 요동치는 신호로
3연속을 다시 만들어야 했다. 실패하자 잠금이 만료돼 `tagged_db_sessions=0`이 되고
감별자가 정당하게 발화했다.

**권고**: (b)를 먼저 고친다 — `hold_seconds` 600 -> 900 (max_injection_duration 15m
안). 그러면 min_hold 이후 창이 7분이 되어 3연속을 잡을 기회가 충분해진다. (a)는
F12-H에서 이미 같은 이유로 `product_error_rate`를 success에서 뺀 전례가 있으나,
F01-P에서 payment_error_rate는 **도메인 교차 인과의 유일한 증거**이므로 단순 제거는
주장을 약화시킨다. 최소 분모 가드를 얹거나 창 평균으로 바꾸는 편이 낫다.

## 10. F03-P — 커넥션 풀을 2로 줄여도 아무 일이 없다 (스킵, 설계 결정 필요)

#5·#6과 같은 계열, 세 번째다. 사다리를 끝까지(pool 5 -> 3 -> 2) 밟고
`calibration_levels_exhausted`. cleanup·recovery 성공, dirty 아님.

```
사다리 바닥 saturation-pool-2, 부하 60rps

payment_p95       = 31.6ms -> 5.5ms   (성공 임계 500ms. 주입 중에 오히려 내려갔다)
checkout_5xx_rate = 0.0               (성공 임계 0.05, escalate 임계 0.02 미만 8연속)
achieved_rps      = 60                (부하 정상)  pod_ready = True
```

**산수가 맞지 않는다.** payment의 DB 작업이 ~5ms이므로 커넥션 2개의 이론 처리량은
2 / 0.005s = **약 400rps**다. 부하는 60rps다. 풀은 포화될 수 없고, 큐 대기도 생기지
않는다. 레버가 부하에 비해 한 자릿수 약하다.

**선택지**: (1) 이 시나리오 전용으로 부하를 400rps 이상으로 올린다 —
`load.north_south`의 surge를 붙여야 하고 다른 서비스에 번질 수 있다.
(2) 풀을 1로 내리고 동시에 쿼리를 느리게 만든다(느린 쿼리 주입과 결합).
(3) Class 재검토. **부하 대비 레버 크기를 실측으로 정하는 것이 먼저다** —
#5·#6과 함께 묶어 판단할 것.

## 11. F05-P — 회복 임계가 노드 평시보다 낮다 (스킵, 임계 재설정 필요)

주입·정리는 정상이었다(`host.stress` memhog 5500MiB, cleanup succeeded). 그런데
**회복 검증이 10분을 다 쓰고 timeout** 되어 `adaptive_controller_dirty`가 됐다.
환경은 깨끗한데 **전역 DIRTY가 남아 배치를 세운다** — 시나리오 하나의 실패보다 나쁘다.

회복 5조건 중 넷은 충족이고 하나가 영원히 거짓이다:

```
target_health=200  pod_ready=True  node_ready=True  gateway_replicas=1   <- 충족
node_mem_util = 47.3 ~ 47.4  (10분 내내 평평)      요구: < 40           <- 불가능
```

**tb-w1의 평시 메모리가 이미 47~50%다**(kubelet stats/summary 실측 50.0%,
러너 프로브 47.4%). 임계가 대상의 실제 분포 밖에 있다.

같은 시나리오의 감별자도 같은 병이다 — `impact-without-node-pressure`가
`node_mem_util < 50`이라 **평시 47.4%에서 이미 참**이다. 실제로 이 런은
must_rule_out streak 3까지 올라갔다.

형제 확인: `node_mem_util`을 쓰는 다른 시나리오는 F15-P뿐이고, 그쪽 회복은 `< 60`이라
평시 아래로 내려가지 않는다(안전).

**권고**: 두 임계를 실측 위로 올린다 — 회복 `< 40` -> `< 60`(F15-P와 동일),
감별자 `< 50` -> `< 60` 이하로는 두지 말 것. 성공 `>= 92`는 유지 가능해 보이나
5500MiB 주입 시 실제 도달값을 함께 재서 정할 것. `1cfd2f4`("성공 조건이 대상의 실제
분포 밖에 있던 셋")와 같은 계열이며, 그 스윕이 F05-P를 빠뜨렸다.

**교훈**: 회복 게이트가 도달 불가능하면 실패가 그 시나리오에서 끝나지 않고
**전역 DIRTY로 번져 배치를 멈춘다.** 임계 감사는 성공 조건뿐 아니라 회복 조건에도
같은 무게로 적용해야 한다.

## 12. 부하 생성기가 없는데 판정이 부하 산출물을 읽는다 (스킵 3종 + 레지스트리 가드)

F15-T1은 **두 주입이 모두 성공했는데** aborted로 끝났다:

```
food_restart_count = 1 -> 2 -> 3   food_termination_reason = OOMKilled   <- 주입 성공
commerce_blocked_db_sessions = 10 -> 13 -> 20                            <- 주입 성공

entry_status      = 사용 불가 (전 구간)
checkout_5xx_rate = 사용 불가 (전 구간)
   exit 1: cat: /tmp/rca-scenario-F15-T1-live.json: No such file or directory
```

**F15-T1은 `companion_refs: []`다.** 부하를 만드는 프로파일이 primary에도 companion에도
없는데, 관측 둘이 부하 생성기 산출물(`/tmp/rca-scenario-<ID>-live.json`)을 읽는다.
아무도 만들지 않는 파일이니 그 신호는 런 내내 죽어 있고, `success`가 그중 하나를
보므로 **성공이 구조적으로 불가능**했다. 그 사이 재시작이 중단 예산 3에 닿아 abort했다.

레지스트리 전수 조사 결과 같은 구멍이 **3종**이다(primary가 부하인 F07-H·F11-R 등은 정상):

| 칸 | 시나리오 | primary | 판정에서의 사용 |
|---|---|---|---|
| 19 | F15-T1 | `timeline.compose` | **success**:checkout_5xx_rate · abort · recovery |
| 35 | F15-P | `host.stress` | abort · **recovery** |
| 44 | F14-P | `db.table_readonly` | must_rule_out 2건 · abort · **recovery** |

**F15-P·F14-P가 더 위험하다.** success가 아니라 **recovery**가 죽은 신호를 읽으므로,
#11(F05-P)에서 본 것처럼 실패가 그 시나리오에서 끝나지 않고 **전역 DIRTY로 번져
배치를 멈춘다.**

### 가드 (`test_loadgen_signals_require_a_profile_that_produces_them`)

"판정이 부하 산출물을 읽으면 부하를 만드는 프로파일이 있어야 한다"를 레지스트리
계약으로 고정했다. **라이브에서만 보이던 결함을 레지스트리 단계로 끌어내린 것**이
요지다. 미수리 3종은 명시적 허용 목록에 두었고 **줄어들기만 해야 한다** — 새 항목이
생기면 그 자리에서 막힌다.

**다음 행동**: 세 종에 부하 companion을 붙이거나(형제 시나리오의 파라미터가 참고가
되지만 부하 종류·강도가 시나리오 주장과 맞는지 검증이 필요하다) 게이트에서 죽은
신호를 뺀다. 어느 쪽이든 허용 목록에서 해당 항목을 지우는 것으로 완료를 증명한다.

## 13. F18-P — 자기 주입이 자기 감별자를 켠다 (스킵, 설계 결정 확정용 증거)

`must_rule_out_detected`로 494초 내내(22연속) 실격. cleanup·recovery 성공, dirty 아님.

```
pod_ready         = False    <- 감별자 transfer-pod-failure 발화
entry_status      = 502
transfer_2xx_rate = 0.0
outbox_unpublished= 0        <- 성공 임계 > 20
ledger_consumer_lag = 0
```

주입은 `k8s.env`로 `OUTBOX_RELAY_ENABLED=false`를 넣는 것이고, 그러면 배포가 롤아웃된다.
그런데 대상 배포의 전략이 이렇다(109 실측):

```
testbed-transfer: RollingUpdate maxSurge=0 maxUnavailable=1 replicas=1
```

**단일 파드를 먼저 내리고 새로 띄운다.** 즉 env를 바꾸는 순간 `pod_ready`가 반드시
false가 되고, 그것을 F18-P 자신의 감별자 `transfer-pod-failure`가 "이건 릴레이 정지가
아니라 파드 장애"로 읽어 실격시킨다. **성공 조건과 감별자가 상호배타다** —
F21-P에서 본 것과 같은 모양이다(2026-07-31).

덧붙여 성공 신호도 성립하지 않았다. 파드가 서비스를 못 하니 outbox에 쌓일 트랜잭션
자체가 생기지 않아 `outbox_unpublished`가 0에 머문다.

**이 배포 설정은 바꿀 수 없다.** `maxSurge=0`은 F17-R을 위해 의도적으로 넣은 것이다
(커밋 `c30924a`). 그래서 선택지는 배포가 아니라 시나리오 쪽에 있다:

1. **재기동 없이 릴레이를 멈춘다** — 런타임 토글(관리 엔드포인트·설정 리로드)로
   바꿔 롤아웃 자체를 없앤다. 앱에 제어 표면이 필요하다.
2. **정체성을 재정의한다** — "릴레이 정지"가 아니라 "전송 서비스 재기동 중 아웃박스
   적체"로 주장을 바꾸고 감별자에서 `pod_ready`를 뺀다. 다만 그러면 F04-R·다른
   파드 장애 시나리오와의 변별력(G10)을 다시 봐야 한다.

**이 런은 그 결정에 필요한 증거를 제공한다** — 배관은 정상이고, 막는 것은 순수하게
설계다. 어제 남겨둔 "설계 결정 3건" 중 F18-P 항목을 이제 실측으로 닫을 수 있다.

## 14. F19-P — 피해는 94%인데 성공이 다른 증상을 함께 요구한다 (스킵)

`evaluation_level_timeout`. streak이 하나도 오르지 않았다(success·escalate·must_rule_out 전부 0).
cleanup·recovery 성공, dirty 아님.

```
order_create_5xx_rate = 0.944   (성공 임계 >= 0.1)   <- 충족
order_hikari_pending  = 0.0     (성공 임계 > 0)      <- 끝까지 0
entry_status = 503   pod_ready = True
```

성공은 두 조건의 **AND**다. 주입은 `mock.expectation`으로 5xx를 내는 것이라 피해는
압도적으로 나오는데, 두 번째 조건은 **커넥션 풀 대기**를 요구한다. mock이 돌려주는
오류는 풀을 붙잡지 않으므로 이 신호는 구조적으로 움직이지 않는다.
**레버가 만들지 않는 증상을 성공 조건이 요구한다.**

선택지: (1) `order_hikari_pending`을 성공에서 빼고 보조 증거로 내린다 —
5xx 94%만으로 G3는 충족된다. (2) 풀을 실제로 마르게 하는 레버로 바꾼다(F03-P의
교훈대로 부하 대비 크기를 실측으로 정해야 한다). (1)이 주장을 덜 바꾼다.

### 곁가지: 환경 오진을 한 번 했다

`order_create_5xx_rate_baseline = 1.0`을 보고 food 도메인 평시가 100% 실패인 줄 알았다.
직접 호출해 보니 **500이 3연속** 나와 더 의심했는데, 부하 생성기 스크립트를 읽어 보니
내 요청 본문이 틀렸다(`{customerId, restaurantId, items}`가 맞다). 올바른 형태로는
**5/5 모두 200**이고 baseline live.json도 `business_2xx_rate: 1.0`이다.

`_baseline` 관측은 주입 중에도 같은 공유 경로를 보므로 1.0이 정상이다.
**관측이 이상하면 먼저 내 질의부터 의심할 것.**

## 15. F19-S — mock 뒤에 있는 서비스의 오류율을 성공이 요구한다 (스킵)

#14와 같은 병, 같은 primary(`mock.expectation`). `evaluation_level_timeout`,
streak 전부 0, cleanup·recovery 성공.

```
order_create_5xx_rate = 0.951   (성공 임계 >= 0.1)   <- 충족
payment_error_rate    = 0.0     (성공 임계 >= 30)    <- 끝까지 0
entry_status = 503   pod_ready = True
```

주입은 mock이 payment 호출을 가로채 5xx를 돌려주는 것이다. 그래서 **호출자(order)는
95% 실패하지만 payment 서비스 자신은 오류를 내지 않는다** — 실제로 실패하는 주체가
mock이기 때문이다. 성공이 AND라 이 조건은 도달 불가다.

### 두 건이 같은 모양이므로 계열로 본다

`mock.expectation`을 쓰는 시나리오의 성공 조건에 **mock 뒤편(=실제 서비스)에서 재는
신호**가 AND로 들어가면 구조적으로 못 넘는다. mock은 그 서비스를 대신하지, 그
서비스를 아프게 하지 않는다. 같은 이유로 F19-P는 커넥션 풀 대기를 요구했다.

**권고**: 두 시나리오 모두 두 번째 AND 조건을 성공에서 내리고 보조 증거로 둔다.
G3(피해 실재)는 이미 호출자 5xx 94~95%로 충분히 증명된다. 감별력이 걱정되면
`must_rule_out`에 "mock이 실제로 걸려 있는가"를 넣는 편이 정확하다.

**가드 후보**: `mock.expectation`이 primary인 시나리오의 success에서, mock이 대신하는
서비스 자신을 재는 관측을 금지하거나 최소한 AND에서 배제한다. 레지스트리 단계에서
기계적으로 검사 가능하다(#12 가드와 같은 성격).

## 16. F16-H — 부하는 도는데 안전 관측이 그 여정에 없는 필드를 읽는다 (스킵)

`safety_observation_unavailable`로 중단. 790초, streak 전부 0.
cleanup·recovery 성공, dirty 아님.

```
entry_status = 사용 불가 52/52 틱
   48x "checkout entry status is unavailable"
   4x  live.json 없음
   source: live-probes:http.entry_health:LiveProbeError

write_401_rate = 0.0 (usable)   <- 같은 파일의 다른 필드는 읽힌다
user_available_replicas = 0     pod_ready = False
```

**#12와 다르다.** F16-H는 `load.north_south` companion을 **갖고 있고**, live.json도
존재한다(`write_401_rate`가 정상적으로 읽힌다). 문제는 한 겹 더 안쪽이다 —
부하가 도는 여정은 **write 스텝**을 만들지 checkout 엔트리를 만들지 않는데,
안전 관측 `entry_status`가 `http.entry_health`(=checkout 엔트리)를 읽는다.
**파일은 있고 필드가 없다.**

그리고 `entry_status`는 abort 게이트가 쓰는 **안전 관측**이다. 러너는 안전 관측을
읽을 수 없으면 판정을 계속하지 않고 중단한다(옳은 설계다). 그래서 이 시나리오는
자기 주장과 무관하게 **완주 자체가 불가능**했다.

### #12 가드는 필요조건이지 충분조건이 아니었다

가드는 "부하를 만드는 프로파일이 있는가"만 본다. F16-H는 그 검사를 통과한다.
필요한 계약은 한 단계 더 좁다:

> **관측이 읽는 필드를, 그 부하가 실제로 도는 여정이 생산해야 한다.**

`loadgen.*` 질의별로 어느 여정이 그 필드를 쓰는지 표를 만들면 레지스트리 단계에서
기계 검사가 가능하다. #12 가드의 확장으로 남긴다.

### 곁가지 (미확정)

성공 조건 `write_401_rate >= 0.3`도 관측값이 0.0이었다. 다만 이 런은 안전 중단으로
일찍 끝났고, `user_available_replicas = 0`이라 파드가 아예 서비스를 못 하는 상태였다.
그렇다면 write는 401이 아니라 연결 실패로 떨어졌을 가능성이 높다 —
"fail-close가 401을 돌려준다"는 주장 자체를 다시 봐야 할 수 있다.
**안전 관측을 고친 뒤 다시 돌려야 판단할 수 있다.**

## 17. F20-R — 엣지가 빨리 실패해서 하류가 느려질 기회가 없다 (스킵)

`evaluation_level_timeout`, streak 전부 0. cleanup·recovery 성공.

```
entry_status = 502
order_p95    = 0.0   (성공 임계 >= 1500)
payment_p95  = 0.0   (성공 임계 >= 500)
payment_error_rate = 0.0    pod_ready = True
```

성공은 "주문 지연 1.5초 이상 **그리고** 결제 지연 0.5초 이상"을 요구한다. 주입은
north-south 서지다. 그런데 게이트웨이가 502로 **엣지에서 거절**해 버리니 order·payment에
요청이 닿지 않고, 닿지 않으니 느려질 수도 없다.

### 0.0은 "빠르다"가 아니라 "트래픽이 없다"였다

지표 자체는 멀쩡하다. 같은 질의를 지금(무주입) 재면:

```
max without(grade) (apm.agent.otel.java.percentile95{service_name="..."})
  commerce-order 54.2 ms · commerce-payment 15.25 ms · food-delivery-order 663.99 ms
```

**APM 에이전트는 유휴 서비스에 0을 발행한다.** 그래서 이 관측의 0.0은 `usable=True`로
들어오고, "매우 빠름"과 "트래픽 없음"이 구분되지 않는다. F20-R처럼 `>=` 성공 조건이면
그냥 실패하지만, **`<` 임계를 쓰는 감별자라면 장애 중에 조용히 참이 된다.**
지연 지표에 하한 조건을 걸 때는 트래픽 유무를 함께 봐야 한다(예: 호출량 > 0).

**권고**: 이 시나리오의 주장을 정한다 —
(1) "서지가 하류 지연을 밀어올린다"를 유지하려면 엣지가 거절하지 않는 수준으로 부하를
낮춰 실측으로 무릎을 찾는다(F07-H가 그렇게 통과했다).
(2) 실제 관측된 사건("엣지 포화로 502")으로 정체성을 바꾼다. 그러면 F07-H와의 변별력
(G10)을 다시 봐야 한다.

## 18. F20-Q — 메모리는 목표를 넘겼는데 서비스가 느려지지 않고 죽는다 (스킵)

62초 만에 `abort_condition`으로 중단(abort 2연속). cleanup·recovery 성공.

```
order_memory_current = 868,941,824  (성공 임계 805,306,368 = 768Mi)  <- 넘겼다
order_p95            = 0.0          (성공 임계 >= 2000)              <- 무트래픽
pod_ready            = False
entry_status / order_create_5xx_rate = 사용 불가
```

**어제 수리가 검증됐다.** `-Xmx512m` 명시(커밋 `8208e18`)가 없었다면 힙이 컨테이너
limit의 25%인 256Mi에 묶여 `memory.current`가 768Mi에 닿기 전에 GC 스래싱으로 죽었다.
이제 **829MB까지 올라가 성공 임계를 실제로 넘긴다.**

막는 것은 두 번째 AND 조건이다. 메모리가 차오르자 서비스가 **느려지는 게 아니라
파드가 unready가 됐고**(그 뒤 재기동해 스스로 복구했다 — 실측 restart 1회),
트래픽이 끊기니 `order_p95`는 0.0이다. #17과 같은 모양이다: **죽은 것과 느린 것은
다른 사건인데 성공 조건은 느린 쪽을 요구한다.**

**권고**: 성공의 두 번째 조건을 실제로 일어나는 피해로 바꾼다 — 파드 unready·재기동
또는 그 구간의 요청 실패율. 지연을 유지하려면 메모리 상승 속도를 낮춰 죽기 전 구간을
길게 만들어야 하는데, 그건 레버 재설계다.

## 19. F25-H — PostgreSQL fault limit이 실사용의 2.6배다 (스킵, 숫자는 나왔다)

`evaluation_level_timeout`, streak 전부 0. cleanup·recovery 성공.

```
pg_termination_reason = None   (성공: == OOMKilled)
pg_restart_count      = 0      (성공: >= 1)
checkout_5xx_rate     = 0.0    (성공: >= 0.1)
```

주입은 postgres StatefulSet의 memory limit을 512Mi -> **320Mi**로 내리는 것이다.
그런데 109 실측(무주입):

```
testbed-postgres-0  workingSet 124MiB · rss 86MiB · usage 139MiB
현재 limits.memory 512Mi / requests.memory 256Mi
```

**fault limit 320Mi는 실사용 124MiB의 2.6배다.** 여유가 그만큼 남으니 OOMKill은
일어날 수 없다. F03-P(커넥션 풀 2가 400rps를 감당)·F09-R(노드를 태워도 requests가
서비스를 지킴)과 같은 계열 — **레버 크기를 실측 없이 정한 것**이다.

**권고(숫자 포함)**: fault limit을 실사용 근처인 **128Mi** 수준으로 내린다. 단
`requests.memory`가 256Mi이므로 limit을 그 아래로 두면 쿠버네티스가 패치를 거부한다 —
`k8s.patch`에서 이미 겪은 문제이고(커밋 `765e99f`) `k8s.resource`도 같은 처리가 필요하다.
requests를 함께 내려야 한다.

덧붙여 PostgreSQL 메모리는 탄력적이라(shared_buffers·page cache) limit을 낮추면
OOM 대신 디스크 I/O가 늘어날 수도 있다. **한 단이 아니라 사다리로 내려가며 실측해
바닥을 찾는 것**이 안전하다(F12-H·F09-P에서 배운 방식 그대로).

## 20. F15-R — 부하 산출물이 런 도중에 사라진다 (스킵, 알려진 미해결 버그의 재발)

`safety_observation_unavailable`로 중단. 80틱 · 1394초(23.2분).
cleanup·recovery 성공, dirty 아님.

신호별 사용 가능 집계:

```
achieved_rps        78/80    2x  cat: /tmp/rca-scenario-F15-R-live.json: No such file
checkout_5xx_rate   78/80    2x  (동일)
entry_status        78/80    2x  (동일)   <- 안전 관측
mock_flap_episode   71/80    9x  freshness

마지막 4틱
  03:19:17 wait        safety_observation_pending      불가: mock_flap_*
  03:19:34 wait        safety_observation_pending      불가: mock_flap_*
  03:20:07 mark_clean  safety_observation_unavailable  불가: achieved_rps·checkout_5xx_rate·entry_status
  03:20:25 mark_clean  safety_observation_unavailable  불가: (동일)
```

**78틱 동안 멀쩡하던 파일이 막판 2틱에 사라졌다.** 그 파일이 안전 관측
`entry_status`를 먹여 살리므로 런이 중단됐다.

이것은 [08-03 배치 기록](batch-44-live-findings-0803.md) 시절부터 미해결로 남아 있던
**"north_south companion의 live.json 조기 삭제"**의 재발이다(그때는 F05-H·F08-P).

### 시간 부족은 아니다 (실측)

```
부하 companion  ramp_up 2m + hold 23m + ramp_down 1m = 26분
레벨 episode-fault-240  min_hold 21m · timeout 23m
실제 런 23.2분
```

26분짜리 부하가 23.2분 런을 덮어야 한다. **단순한 지속시간 미달로는 설명되지 않으므로
원인은 아직 미상이다.** 후보는 (a) ramp_down 진입 시 산출물을 먼저 지운다,
(b) k6 프로세스가 조기 종료한다, (c) 정리 순서가 판정보다 먼저 돈다.

**다음 행동**: tb-runner에서 이 시나리오의 부하를 단독으로 26분 돌리며
`/tmp/rca-scenario-<ID>-live.json`의 존재를 1초 간격으로 찍어 **언제 사라지는지**
먼저 실측한다. 원인을 고르기 전에 사라지는 시각을 아는 것이 순서다.
가장 긴 런에서 재현됐다는 점이 단서다 — 이번 배치에서 23분은 최장이었다.

## 21~22. F03-H — 07-28부터 죽어 있었고, 살리고 보니 레버가 약했다

### 21. 드리프트 (수리 `b4b000e`)

주입이 실행기 자신의 검증에 거부됐고 **정리도 같은 검증을 지나 함께 거부**되어 전역
DIRTY가 됐다. 원인은 `target_url` 한 줄:

```
레지스트리  .../reports/render?delayMs=5000     (4개 파일 12군데)
실행기      .../reports/render?days=120
앱          renderReport(@RequestParam(defaultValue="30") int days)   <- delayMs 없음
```

실행기 주석이 이력을 남겨 놨다(2026-07-28): `delayMs`는 앱이 호출자가 준 시간만큼
`Thread.sleep` 하는 구조라 **(a) 결함이 아니라 파라미터였고 (b) 접근 로그의 delayMs가
정답을 자백했다(G6)**. 그래서 진짜 결함(`OrderReportRenderer`의 synchronized O(n^2)
렌더)으로 갈아탔는데, **그 수리가 실행기에만 들어갔다.** F03-H는 그날 이후 한 번도
주입되지 못했다.

**가드가 9종을 안 보고 있었다.** 사다리 가드가 `.py` 실행기만 훑는데 레지스트리는
셸 래퍼를 가리킨다(`load.east_west`, `host.stress` 사다리 6종). `.sh`를 형제
`_executor.py`로 풀어 훑도록 넓혔다 — 서브테스트 90 -> 117.

**DIRTY 해제에 세 단계가 필요했다**: `repair_capsule` 실패 -> 레지스트리 수정·배포 후에도
실패 -> 러너 재기동 후에도 실패(캡슐이 파라미터를 얼려 복구 경로까지 막는다,
2026-07-31 기록과 동일) -> **잔재 전수 확인 후 수동 해제**. 확인 결과 `scenario-f03-h`
Job·ConfigMap 부재(=주입 자체가 없었음)·파드 15개 정상·tb-runner baseline 3종만.

### 22. 살아난 뒤: 60rps로도 포화가 안 된다 (스킵)

새 런은 새 캡슐을 받으므로 재시도는 정상 동작했다 — **사다리 3단(30/45/60rps)을
다 밟고 114틱**, `maximum_injection_duration`(30m)으로 종료.

```
order_p95         = 77.53 ms   (성공 임계 >= 2000)
checkout_5xx_rate = 0.0        (성공 임계 >= 0.05)
entry_status = 200   pod_ready = True
```

설계 주석의 예측(x86 상한 ~131rps, aarch64는 더 낮으므로 30/45/60이 포화를 걸친다)이
**실측과 어긋난다.** 60rps에서도 p95가 77ms다.

**권고**: 렌더 비용을 워커(aarch64)에서 직접 재고 사다리를 다시 그린다. `days`를
키우는 쪽이 rps를 키우는 쪽보다 비용이 제곱으로 늘어 효율적이다(실측 x86:
days=120 7.6ms vs 365 62.34ms). #19(F25-H)와 같은 처방 — **레버는 대상에서 재고 정한다.**

## 23. F06-P — 앱이 429를 5xx로 바꿔 자기 감별자에 걸린다 (스킵)

`must_rule_out_detected`. 33틱 · 489초. cleanup·recovery 성공, dirty 아님.
**성공 조건도 한때 충족됐다**(`streak_marks.success = 489`) — 그런데 감별자가 함께 참이었다.

```
성공:  order_create_429_rate >= 0.3       관측 0.222 (한때 충족)
감별:  achieved_rps        < 15           관측 5.28    <- 발화
       order_create_5xx_rate >= 0.1       관측 0.778   <- 발화
       read_step_rate      >= 0.1         관측 0.0     ok
       pod_ready           == false       관측 True    ok
```

주입은 mock이 주문 생성에 429를 돌려주는 것이다. 실제로 일어난 일은 두 가지다.

1. **앱이 상류 429를 호출자에게 5xx로 승격한다** — `order_create_5xx_rate` 0.778.
   감별자 `promoted-to-5xx`는 바로 그것을 "깨끗한 rate-limit 거절이 아니다"라고 실격시킨다.
2. **처리량이 무너진다** — `achieved_rps` 5.28(평시 30~60). 감별자 `load-not-delivered`가
   "부하가 전달되지 않았다"로 읽는다. 실제로는 부하가 전달됐는데 앱이 못 받아낸 것이다.

즉 **성공 조건이 그리는 그림(깨끗한 429 거절)과 앱의 실제 거동(429 -> 5xx 승격 +
처리량 붕괴)이 다르다.** F18-P(#13)·F21-P(2026-07-31)와 같은 계열 —
**성공과 감별자가 상호배타**다.

F06-P는 2026-07-28에 "앱 수정 0줄"로 신설된 관측 시나리오다. 감별자가 **실측이 아니라
기대**로 쓰였고, 이번이 첫 라이브 검증이다.

**권고**: 앱이 429를 어디서 5xx로 바꾸는지 코드로 확인한 뒤 둘 중 하나 —
(1) 앱이 상류 429를 그대로 전파하도록 고친다(정체성 유지, 앱 수정 필요) 또는
(2) 정체성을 "상류 rate-limit이 5xx로 증폭된다"로 바꾸고 감별자에서 `promoted-to-5xx`를
뺀다. 후자라면 `load-not-delivered` 임계도 함께 봐야 한다 — 처리량 붕괴가 증상이라면
그것을 실격 사유로 둘 수 없다.

## 24. F02-H — 디스크를 81%까지 태워도 commerce는 안 느려진다 (스킵, 설계 질문 확정)

사다리를 끝까지 밟고(io-6000 -> io-12000 -> io-20000) `calibration_levels_exhausted`.
80틱 · cleanup·recovery 성공, dirty 아님.

```
성공 조건 (AND 셋)
  disk_io_util >= 70    관측 81       <- 충족. 주입은 확실히 걸렸다
  order_p95    >= 1000  관측 41.85ms  <- 미달
  payment_p95  >= 400   관측 24.17ms  <- 미달

node_cpu_util 39.5   achieved_rps 20.1   pod_ready True
```

**주입은 성공했다** — 장치가 81% 포화다(감별자 `symptom-without-device-load`도, 
`node-cpu-saturated`도 걸리지 않았으니 원인은 디스크가 맞다). 그런데 그 위에서 도는
commerce 서비스의 지연이 꿈쩍하지 않는다.

이것으로 **2026-08-04에 "인과가 성립하지 않는 셋"으로 남겨둔 항목이 실측으로
확정됐다** — 기록에는 "commerce/PG에 인과 부재, food/MySQL은 같은 레버로 5.6초를
냈다"고 되어 있었고, 이번 런이 commerce 쪽 인과 부재를 라이브에서 재확인했다.
(같은 묶음의 F18-P는 #13에서, F21-Q는 뒤 칸에서 각각 확정된다.)

**왜 안 아픈가**: PG의 워킹셋이 작아(#19 실측: postgres workingSet 124MiB) 읽기가
대부분 page cache에서 끝난다. 장치를 아무리 태워도 commerce 질의는 디스크에 내려가지
않는다. food/MySQL이 같은 레버에 5.6초로 반응한 것은 워킹셋·캐시 사정이 다르기 때문이다.

**권고**: 셋 중 하나.
(1) **대상을 옮긴다** — 같은 시나리오를 food/MySQL로 재정의한다(이미 반응이 실측됐다).
(2) **캐시를 무력화한다** — PG shared_buffers를 줄이거나 캐시 밖 데이터를 읽는 질의로
    바꿔 디스크를 실제로 타게 만든다.
(3) **Class 재검토** — 이 배치·이 데이터 크기에서 commerce 디스크 포화는 사용자 피해로
    이어지지 않는다고 판정한다.

#5·#6·#10·#19와 함께 **"주입은 진짜인데 피해가 없다"** 계열의 다섯 번째다.

## 25. F10-H — 같은 사다리 꼭대기가 노드마다 다른 값을 낸다 (스킵)

사다리를 끝까지 밟고(io-6000 -> 12000 -> 20000) `calibration_levels_exhausted`.
62틱 · escalate 유지 486초. cleanup·recovery 성공, dirty 아님.

```
성공:  disk_io_util >= 70    관측 64      <- 미달 (escalate device-not-yet-saturated)
       order_p95    >= 2500  관측 0.0     <- 무트래픽 (#17 교훈: 0.0은 "빠름"이 아니다)

order_create_5xx_rate 0.625   achieved_rps 5.09   node_cpu_util 80.6   entry_status 503
```

### 노드별 실측 대비 (이번 배치)

```
io-20000  ->  tb-w1 (192.168.122.184, commerce) : disk_io_util 81   [#24 F02-H]
io-20000  ->  tb-w3 (192.168.122.14,  food)     : disk_io_util 64   [이 런]
```

**같은 레버, 다른 노드, 17포인트 차이.** 성공 임계 70이 한쪽에선 넘고 한쪽에선 못 넘는다.
CPU 사다리에서 배운 것과 같은 교훈이다 — **바닥도 꼭대기도 대상마다 재야 한다**
(2026-08-04 `d209ca6`: "바닥은 서비스마다 다르다").

### 이 시나리오는 예전에 성립했었다

성공 조건의 주석이 알려준다 — 2026-08-03 배치에서 **disk 77 · p95 5632ms**로 손상을
크게 재현했고, 그때는 풀 대기가 진짜 0이라 실패해서 그 조건을 성공 게이트에서 뺐다.
즉 tb-w3에서 70을 넘긴 전례가 있다. 오늘은 64에서 멈췄다.

차이의 후보: 노드 CPU가 80.6%까지 올라 **fio 자신이 CPU를 못 얻었을** 가능성
(io-20000은 CPU도 쓴다), 또는 페이지 캐시·디스크 상태 차이. 감별자
`node-cpu-saturated`(>= 85)는 아슬하게 안 걸렸다.

**권고**: 사다리에 단을 더 얹기 전에 **tb-w3에서 fio 단독으로 io_util-대-부하 곡선을
재라.** 70을 안정적으로 넘기는 지점이 어디인지, 그리고 그 시점의 node CPU가 감별자
임계 85에 닿지 않는지 함께 봐야 한다 — 둘이 붙어 있으면 F06-P처럼 성공과 감별자가
서로를 막는다.

## 26. F10-P — 주입도 피해도 진짜인데 세 신호가 같은 틱에 서지 않는다 (스킵)

run `61db5e49` (08-05). 사다리를 끝까지(io-6000 -> 12000 -> 20000) 밟고 99틱 ·
`calibration_levels_exhausted`. cleanup·recovery 성공, dirty 아님. streak 전부 0.

### 먼저, 의심했던 것부터 기각한다

`/tmp/rca-scenario-F10-P-live.json` 부재로 신호 3종(achieved_rps·entry_status·
transfer_2xx_rate)이 unusable이 된 틱은 정확히 28개인데, **전부 종료 후
`failed/mark_clean` 구간이다** — 판정이 살아 있던 71틱 동안 파일은 멀쩡했다.
정리 순서상 companion 부하가 먼저 걷힌 뒤 회복 폴링이 계속 그 신호를 읽은 것뿐이고,
F10-P의 recovery 게이트는 loadgen 신호를 쓰지 않으므로(#12 가드 통과) 무해하다.
**live.json 부재는 이 런의 실패 원인이 아니다.** (`/tmp/ticks.py` 기본 키가 다른
시나리오 것이라 처음에 오독했다 — ticks.jsonl의 `signals` 키로 재판독해 확정.)

### 진짜 원인은 두 겹이다

주입은 실증됐고(disk_io_util 최대 96) 피해도 실증됐다(transfer_p95 최대 28.5초,
hikari_pending 최대 31, pod_ready False 73/99틱, entry 502 59/99틱). 성공은
`disk_io_util >= 70` AND `transfer_p95 >= 4000` AND `hikari_pending > 0`의
**3연속**을 요구하는데, 세 조건이 같은 틱에 선 것은 **최대 2연속**이었다.

**(a) 피해가 '느려짐'이 아니라 '죽음'으로 발현한다.** Oracle 디스크가 포화되면
transfer 파드의 readiness가 깨지고(73/99틱 unready) 게이트웨이가 502로 거절한다.
트래픽이 끊기면 APM은 유휴 서비스에 0을 발행하므로(#17 교훈) `transfer_p95`가
0.0으로 떨어져 latency 조건이 리셋된다. **피해가 클수록 latency 신호가 죽는
자기 모순** — #17(F20-R)·#18(F20-Q)과 같은 계열의 네 번째다.

**(b) disk_io_util이 구간 평균으로도 40↔96을 오간다.** `cd874a7`(틱 구간 평균)은
배포·동작 확인됐다 — tb-w2의 `F10-P.iosample` 상태 파일이 런 중 갱신됐고 프로브
간격도 16초로 일정했다. 그런데도 io-20000에서 값이 약 60~80초 주기로 45↔94를
오간다. 게스트 direct=1이어도 호스트(qemu) 쪽 캐시·writeback 주기가 장치 바쁨을
버스트로 만드는 것으로 보인다. 임계 70에 3연속을 걸면 이 요동에선 동전 던지기다.

**권고**: (a)가 본질이므로 성공 조건 재설계가 먼저다 — latency 대신 실제로 일어나는
피해(pod unready·entry 502·transfer_2xx_rate 붕괴)를 지목하거나, 사다리를 내려
죽지 않고 느려지는 구간을 찾는다(단 io-12000에서도 이미 pod가 죽었다). (b)는
그 다음이다 — 재설계 후에도 disk 조건을 유지한다면 임계·연속성을 실측 분포로
다시 정할 것. #24(F02-H)·#25(F10-H)와 함께 **디스크 3부작 설계 결정**으로 묶는다.

## 27. F21-Q — 노드를 태우자 상류가 아니라 입구가 죽었다 (스킵, #26/08-03과 병합)

run `8d09341e` (08-06). evaluation 모드(approved-fixed, cpu 3워커·480초).
250초 만에 `abort_condition`(entry-unreachable 2연속). cleanup·recovery 성공.

```
node_cpu_util        55.7 -> 95.97 -> 99.97   (tb-w3, 4코어 — 평시 load avg ~3)
order_busy_threads   38 -> 230     (성공 임계 180 충족)
order_p95            3600대 ↔ 0.0  (성공 임계 2500, 무트래픽 리셋 반복)
restaurant_p95       63 ~ 90 ms    (성공 임계 300 — 끝까지 미달)
order_hikari_pending 34 -> 155     (감별자 pool-is-the-bottleneck > 2, 내내 참)
entry_status         503/500 반복 -> 235초 0  -> abort
```

08-03 배치 #26("만들려던 원인은 안 생기고 다른 원인이 생겼다")과 같은 병의 재발이다.
그때는 부하 서지로 풀이 고갈됐고, 이번엔 레버를 host.stress cpu로 바꿨는데 —
**평시 load ~3인 4코어 노드에 워커 3을 얹으니** 노드가 99.97%로 포화되어 상류
(restaurant)는 여전히 빠른데(CPU requests가 보장) order 자신과 DB 풀, 마지막에는
**게이트웨이 입구까지 죽었다**(entry_status=0 catastrophic abort). F21-P와 같은
**"입구가 먼저 죽는" 계열**이다.

주장(상류 지연 -> 스레드 적체)의 인과 사슬에서 **상류 지연이 두 번의 레버 교체에도
발생하지 않았고**, 감별자 `pool-is-the-bottleneck`은 두 번 모두 정당하게 발화했다.
레버를 더 키울 곳도 없다 — 이미 입구가 죽는다. **F21-P와 병합해 정체성 재설계
대상으로 묶는다**(아래 설계 결정 검토).

## 28. F21-P — 감별자는 정당했고, 러너는 부당했다 (러너 수리 + 재설계 대상)

run `5df51067` (08-06). 07-31 레버 교체(k8s.patch CPU limit 사다리 400/300/200m)
후 첫 라이브. 첫 단 conservative-400m에서 189초 만에 `must_rule_out_detected`
(api-failing-not-queueing). cleanup·recovery 성공, dirty 아님.

```
api_error_rate    40 -> 95 -> 100 %   (감별자 임계 5, 첫 틱부터)
transfer_throttled 842 -> 59481       (스로틀은 진짜 걸렸다)
transfer_2xx_rate  0.0 (79초의 0.51 순간 제외)
entry_status       502 (거의 내내)     api_busy_threads 87 (성공 임계 180)
transfer_p95       0.0 (무트래픽)      pod_ready True (관측 대상이 testbed-api라서)
```

### 왜 첫 틱부터 100%인가 — maxSurge=0이 여기서도 문제다

`k8s.patch`로 CPU limit을 바꾸면 파드 템플릿이 바뀌어 롤아웃이 돈다. 그런데
testbed-transfer는 **maxSurge=0 · replicas=1**(F17-R을 위한 의도적 설정, #13에서
확정)이라 **유일한 파드를 먼저 내린다.** 즉 주입 순간 transfer가 통째로 사라지고,
이어서 400m 제한 아래 Spring Boot 기동이 심하게 스로틀되어(throttled 59481)
가용 상태로 못 돌아온다. api는 큐잉이 아니라 즉시 실패했고(40->100%),
entry는 502가 됐다. **F18-P(#13)를 막은 것과 같은 배포 설정이 F21-P의 레버도 막는다.**

### 러너 층의 구멍 — 판정만 미루고 증거는 settle에서 쌓였다

`7eb9391`(07-24)은 must_rule_out **판정**을 min_hold 뒤로 미뤘지만 **streak은
settle 구간에서 계속 쌓였다.** 이 런의 settle=min_hold=3m: streak 3이 142초
(settle 안)에 완성됐고, min_hold를 지난 **첫 틱(189초)에 그대로 발화했다** —
정착 상태의 증거는 단 1틱. 롤아웃 과도기를 흡수하라고 있는 settle이 실격 증거를
모으는 구간이 된 것이다.

**수리**(`0eec7ed`, rca-scenario-runner, **미배포** — 배치 완주 후 배포): settle·
min_hold 대기 틱에서 must_rule_out streak을 0으로 되돌려 confirmation이 min_hold
이후 관측 consecutive_ticks개로만 성립하게 했다. 진짜 대체 원인은 최대
consecutive_ticks-1틱 늦게 여전히 abort된다. 273 테스트 통과.

### 수리는 필요조건이지 충분조건이 아니다

수리 후 이 런을 다시 돌리면 abort가 ~45초 늦어질 뿐, api_error_rate는 min_hold
이후에도 100%였다 — transfer가 400m에서 서비스 불능이므로. **성공 조건("느려지되
실패하지 않는다": api_p95>=6000 AND transfer_p95>=2000)과 실제 거동(즉시 실패 +
무트래픽 p95=0)의 상호배타가 재확인됐다.** 2026-07-31 판정 그대로다. 감별자는
정당하다 — 이 레버로는 이 정체성을 만들 수 없다. F21-Q와 병합해 재설계한다.

## 29. F09-H — 힙을 줄였더니 GC 압력 지표가 내려갔다 (스킵)

run `4cc52147` (08-06). 08-03 배치 #28의 권고("0.85를 보려면 힙을 줄이거나…")가
구현된 뒤의 첫 라이브다: `k8s.env`로 `-Xmx/-Xms` 사다리 208→160→128→112Mi
(+AlwaysPreTouch), 성공 임계는 0.85→0.8로 조정돼 있었다. heap-160에서
`must_rule_out_detected`(symptom-without-gc-pressure). cleanup·recovery 성공.

```
heap-208  old_gen_ratio 0.477 (전 틱 고정)  p95 40~312ms  -> escalate
heap-160  old_gen_ratio 0.324 (전 틱 고정)  p95 60~1592ms
          -> 감별자 < 0.35 참 -> min_hold 종료 직후 abort
```

**레버가 지표를 반대로 움직였다.** 힙을 줄이면 GC가 더 자주·더 철저히 돌아
**GC 직후 잔존 점유(used_after_last_gc/limit)는 오히려 내려간다** — 208Mi에서
0.477이던 것이 160Mi에서 0.324가 됐다. 성공(>= 0.8)에 다가가기는커녕 감별자
바닥(< 0.35) 아래로 떨어졌고, 아래 단(128/112)은 밟아보지도 못했다.
08-03 #28의 "감별자 바닥이 지표 천장보다 높다"가 힙 축소로도 풀리지 않은 것이다.

구조적으로: 이 워크로드의 live set은 ~50MiB 수준이라, `old_gen_ratio >= 0.8`이
성립하려면 힙이 live set의 1.25배(~64Mi) 안쪽이어야 하는데 그 근처에선 OOM이
먼저 온다 — 감별자 `crossed-into-oom`(restart > 0)과 정면 충돌한다. F05-R(#5)의
성공↔중단 예산 경쟁과 같은 모양이 성공↔감별자 사이에 있다.

곁가지 둘. (1) heap-160 시작 틱들의 `order_restart_count = 2`는 이전 파드의
카운터다 — env 롤아웃으로 새 파드가 뜨며 0으로 리셋됐다. settle 구간에서
`crossed-into-oom`이 잠깐 참이었다는 뜻이고, `0eec7ed`가 막는 오염의 또 다른 사례다
(이 런의 abort 자체는 수리 후에도 났을 것이다 — 0.324는 지속값이므로).
(2) `old_gen_ratio`가 레벨 내내 한 값에 고정되는 것은 major GC 후에만 갱신되는
지표 특성으로, 평시엔 표본이 드물다는 뜻이다.

**권고**: `old_gen_ratio`는 힙 크기 레버와 단조 관계가 아니므로 성공·감별자
지표로 부적합하다. GC **압력을 직접 재는** 지표(GC pause 시간 비중, GC CPU
오버헤드, allocation stall)로 게이트를 재설계하거나, 정말 회수 불가능한 객체를
쌓는 앱 표면(누수형)이 필요하다. 어느 쪽이든 감별자 바닥은 힙 크기별 실측
분포로 재산정할 것.

## 30. F09-P — 시나리오가 아니라 환경이 막았다 (관찰)

run `55f2f25b` (08-06 16:53)는 주입 전 `blocked` —
`check_failed:baseline-business-success`. ticks 0, 변이 0. 큐는
`waiting_clean_window`로 대기 중이며 babysitter가 재시도를 관리한다.
시나리오 결함 증거가 아직 없으므로 재시도 결과가 나온 뒤 판단한다.
(참고: 08-03 01:29에 F09-P·F15-P에 preskip이 걸린 이력이 있다.)

## 설계 결정 검토 — 미결 3건 (2026-08-06)

> **집행 기록 2 (같은 날 밤)**: F18-P 런타임 토글을 실제로 만들었다(`e1f17b6`
> — outbox_relay_control DB 플래그 + app.control 실행기, 무재기동·무자백).
> F20-Q·F10-P 성공 조건을 실측 트레이스 기반 죽음-피해로 재설계(`c1a52be`),
> F25-H를 256→192→128Mi 실측 사다리로 전환(`9e23a9a`), F01-P 실행기
> CONTRACTS 드리프트를 동기화하고 가드를 얹었다(`64170de`). 전부 미배포.
>
> **집행 기록 (같은 날 저녁, `c887e20`)**: 아래 검토 중 F02-H 파킹과 F21-P/Q
> 병합 파킹은 레지스트리에 반영했다(catalog readiness=parked + prerequisite,
> 컨트롤러는 controllers-parked.json 보관). F18-P의 런타임 토글은 앱 표면
> 신설이 필요해 별도 작업으로 남긴다. #9·#11·#14·#15의 게이트 수정도 같은
> 커밋에 있다. 전부 **미배포** — 배치 완주 후 109 반영.

### F18-P: maxSurge=0은 못 바꾼다 → 재기동 없는 제어 표면이 정답이다

#13의 선택지 둘 중 **1번(런타임 토글)을 권고한다.** 근거가 하나 늘었다 — #28에서
같은 maxSurge=0 롤아웃 과도기가 F21-P의 레버까지 막았다. 즉 "env 변경 = 파드 재기동"
경로는 이 배포 설정 아래서 **구조적으로 감별자와 충돌**하며, 정체성을 "재기동 중
적체"로 바꾸는 2번은 F04-R 계열과의 변별력(G10)을 희생한다. testbed-services는
우리 앱이므로 relay on/off를 관리 엔드포인트(재기동 없는 설정 리로드)로 노출하면
정체성·감별자·G10을 모두 보존한다. 주의: F03-H의 교훈대로 **호출자가 값을 넘기는
파라미터형 자백 표면이 되면 안 된다** — 토글은 서버측 상태로만.

### F02-H: 인과 부재가 실측으로 확정됐다 → 파킹을 권고한다

#24 실측(disk 81% / order p95 41.9ms)으로 commerce/PG 인과 부재는 확정이다.
선택지 셋 중: (1) food/MySQL 이관은 F10-H와 정체성이 겹치고(G10), (2) 캐시 무력화는
shared_buffers 축소 + 캐시 밖 질의라는 새 표면이 필요해 사실상 신규 설계다.
**(3) Class 재검토(파킹)를 권고한다** — 이 배치·데이터 크기에서 "commerce 디스크
포화 -> 사용자 피해" 주장은 성립하지 않는다. 디스크 포화의 대표성은 이미 반응이
실측된 F10-H(food/MySQL)가 가져가고, F02-H는 "장치 포화가 곧 피해는 아니다"의
음성 사례로 기록을 남긴다. 되살리려면 (2)를 사다리 실측과 함께 신규 설계로.

### F21-Q ↔ F21-P: 병합 확정 — 같은 주장, 같은 병, 레버만 다르다

두 시나리오의 주장은 동형이다: "하류/상류가 **느려져** Tomcat 스레드가 쌓인다."
그리고 두 번의 라이브가 보여준 병도 동형이다 — 무딘 레버(노드 CPU 스트레스,
CPU limit 스로틀)는 **선택적으로 느리게 만들지 못하고** 대상 전체 혹은 입구를
죽인다(#27 entry=0, #28 api 100% 실패). 성공 조건이 요구하는 "slow-not-failed"를
이 레버들은 만들 수 없다.

**병합 재설계 방향**: 필요한 것은 "특정 의존성만, 정해진 만큼, 죽지 않게" 지연시키는
레버다. 후보는 (a) `mock.expectation`의 지연 응답 — F19 계열에서 mock이 이미
경로에 있다면 rollout 없이 지연을 주입할 수 있다(단 #15의 교훈: mock 뒤편 서비스를
재는 신호를 success에 두지 말 것), (b) 앱 서버측 고정 지연 설정(재기동 없는 리로드,
F18-P와 같은 제어 표면 — 호출자 파라미터형 자백 금지). tracker의 기존 메모
"정밀 delay injector 부재 = 캘리브레이션 게이트"가 바로 이것이다. 제어 표면이
생기기 전까지 두 시나리오 모두 라이브 큐에서 빼는 것이 맞다.
