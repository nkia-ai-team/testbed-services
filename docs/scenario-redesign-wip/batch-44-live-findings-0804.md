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
