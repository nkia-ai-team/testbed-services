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
| 9 | F01-P | **판정 계약** | 주입·피해 모두 실증됐으나 성공 신호가 표본 부족으로 요동 | **스킵** |
| 10 | F03-P | **G3 피해 부재** | 커넥션 풀을 2로 줄여도 지연이 오히려 내려감 | **스킵** |
| 11 | F05-P | **임계가 분포 밖** | 회복 조건이 노드 평시(50%)보다 낮은 40%를 요구 → 거짓 전역 DIRTY | **스킵** |
| 12 | F15-T1·F15-P·F14-P | **죽은 신호** | 부하를 만드는 프로파일이 없는데 판정이 부하 산출물을 읽음 | **스킵 + 가드** |
| 13 | F18-P | **자기 주입이 자기 감별자를 켠다** | maxSurge=0이라 env 변경이 곧 파드 unready | **스킵** |
| 14 | F19-P | **레버가 못 만드는 증상을 요구** | 5xx는 94%인데 성공이 커넥션 풀 대기를 함께 요구 | **스킵** |
| 15 | F19-S | **레버가 못 만드는 증상을 요구** | 5xx는 95%인데 성공이 payment 자신의 오류율을 함께 요구 | **스킵** |
| 16 | F16-H | **여정에 없는 필드를 안전 관측이 읽음** | 부하는 도는데 checkout 필드가 없어 52틱 전부 사용 불가 → 안전 중단 | **스킵** |
| 17 | F20-R | **엣지에서 빨리 실패해 하류가 안 느려짐** | entry 502인데 order·payment p95는 0.0(무트래픽) | **스킵** |
| 18 | F20-Q | **느려지는 게 아니라 죽는다** | 메모리 임계는 넘겼으나(어제 수리 검증) 파드가 죽어 지연이 0 | **스킵** |
| 19 | F25-H | **레버가 실사용의 2.6배** | PG는 124MiB 쓰는데 fault limit이 320Mi → OOM 불가 | **스킵** |
| 20 | F15-R | **부하 live.json 조기 소멸(재발)** | 23분 런의 막판에 파일이 사라져 안전 관측 상실 → 중단 | **스킵** |

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
