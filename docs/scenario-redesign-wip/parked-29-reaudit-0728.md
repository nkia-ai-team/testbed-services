# 미준비 29종 전수 재조사 (2026-07-28)

ready 31종을 뺀 나머지 29종(parked 26 / blocked 2 / draft 1)이 **실제로** 무엇에 막혀 있는지를,
`catalog.json`의 한 줄짜리 `prerequisite`나 07-24 설계 시트가 아니라 **레지스트리·실행기 실물과
라이브 인프라 실측**으로 다시 확정한 문서다.

## 0. 왜 재조사했나 — 기존 근거 두 종이 모두 신뢰 불가

| 근거 | 문제 | 실례 |
|---|---|---|
| `catalog.json`의 `prerequisite` 한 줄 | 실제 작업량을 축소. 여러 갭 중 하나만 적음 | F17-P는 "cleanup 역분개"만 적혀 있으나 실제 갭은 4항 |
| `docs/scenario-redesign-wip/design-*-sheet.md` | 07-24 시점 고정. 이후 07-27/28 관측 배선을 반영 못 함 | F17-P 시트는 관측 query 3종이 "없음"이라 하나 **셋 다 존재** |
| `golden4-audit-0727.md` | 07-27 시점. 이후 커밋(success 재분류·관측 배선)으로 낡음 + 일부 오진 | F10-H/F10-P 좌표 판정이 반대. F09-P "throttling 지표 부재"는 현재 존재 |

**교훈: 시나리오 상태의 정본은 문서가 아니라 레지스트리·실행기·라이브 인프라다.**

## 1. 이번에 실측한 사실 (신규)

### 1-1. 워커 3대 인프라 (2026-07-28 실측)

| 호스트 | 이름 | cores | mem | 루트FS | fio | stress-ng |
|---|---|---|---|---|---|---|
| 192.168.122.184 | tb-w1 | 4 | 11.9G (avail 4.5G) | 38G, 25G 사용, **66%** | **없음** | **없음** |
| 192.168.122.11 | tb-w2 | 4 | 11.9G (avail 7.7G) | 38G, 12G 사용, 31% | **없음** | **없음** |
| 192.168.122.14 | tb-w3 | 4 | 11.9G (avail 6.6G) | 38G, 19G 사용, 49% | **없음** | **없음** |

- `fio` 미설치 → **F02-H·F10-H·F10-P가 preflight에서 즉사**(`check()`가 `command -v fio` 요구)
- `stress-ng` 미설치 → **F15-P**(`mode: pressure`) 동일
- 둘 다 apt 설치 가능 확인(Ubuntu 24.04 arm64, `fio 3.36-1ubuntu0.1` / `stress-ng 0.17.06-1build1`, 저장소 도달)

### 1-2. DB PVC 실제 위치 — 도메인-워커 지도와 불일치

| PVC | 실제 호스트 |
|---|---|
| commerce PG (`pgdata-testbed-postgres-0`) | **tb-w1** |
| banking Oracle (`oracledata-testbed-oracle-0`) | **tb-w1** |
| food MySQL (`mysqldata-testbed-mysql-0`) | **tb-w3** |

DB PVC는 도메인별 워커에 붙어 있지 않다. `CLAUDE.md`의 "w1=commerce / w2=food / w3=core-banking"은
**애플리케이션 배치 의도이지 PVC 배치가 아니다.**

이로써 F10 계열 좌표 판정이 뒤집힌다:

| | manifest `location` | 실행기 계약 host | 실측 | 판정 |
|---|---|---|---|---|
| F10-R | worker-w1 | .184 (w1) | commerce PG on w1 | 일치 |
| F10-H | worker-w2 | .14 (**w3**) | food MySQL on **w3** | **manifest가 틀림** |
| F10-P | worker-w3 | .184 (**w1**) | banking Oracle on **w1** | **manifest가 틀림** |

감사는 "실행기 좌표가 도메인 맵과 정반대"라 했으나, **실행기가 맞고 매니페스트 라벨이 틀렸다.**
수정 범위는 매니페스트 `injection.location` 필드 하나씩.

### 1-3. 관측은 병목이 아니다

- `registry/queries.json` 53종이 러너 `observation_queries.json`에 **전부 구현돼 있음**(결손 0)
- 러너는 오히려 8종을 더 보유: `host.filesystem_used_percent`, `database.mysql_index_present`,
  `kubernetes.container_oom_killed`, `kubernetes.container_memory_limit_bytes` 등
- 보류 컨트롤러 8종이 요구하는 쿼리도 **전부 존재**(결손 0)
- → "관측 신설"을 이유로 든 prerequisite(F09-H·F09-P·F21-Q·F17-P)는 **대부분 낡았다**

### 1-4. `approved_profile_id: null`은 결손이 아니다

`adaptive_ladder` 프로파일에서 `approved_profile_id`가 null인 것은 **정상**이다 —
현재 **라이브 31종 중 10종**(F03-P·F05-P·F05-R·F07-H·F07-P·F09-R·F11-R·F12-H·F15-R·F15-T1)이 동일하다.
사다리는 런타임에 캘리브레이션한다. 보류된 F03-H·F09-H·F09-P도 같은 구조이며, 이 필드는 차단 사유가 아니다.

### 1-5. 파드 배치 실측 — 도메인-워커 분리는 존재하지 않는다

2026-07-28 `kubectl get pods -A -o wide` 실측:

| 워커 | 파드 |
|---|---|
| tb-w1 | banking-api, banking-**oracle**, commerce-**postgres**, commerce-notification, commerce-external-pg-mock, food-kafka, food-order, food-restaurant, food-dispatch, food-external-pg-mock |
| tb-w2 | banking-ledger, banking-nginx, commerce-cart, commerce-gateway, commerce-inventory, commerce-kafka, commerce-redis, commerce-shipping, food-payment |
| tb-w3 | banking-account, banking-kafka, food-**mysql**, commerce-nginx, commerce-order, commerce-payment, commerce-pricing, commerce-product, commerce-user, food-notify |

**세 도메인이 세 워커에 완전히 뒤섞여 있다.** 이로부터 host.stress 계열 계약의 좌표 오류가 무더기로 드러난다:

| id | 계약 host | 대상 서비스 실제 위치 | 판정 |
|---|---|---|---|
| F21-P | .14 (tb-w3) | `banking-api`는 **tb-w1** | **대상 없는 노드를 때린다** |
| F21-Q | .11 (tb-w2) | `food-order`는 **tb-w1** | **대상 없는 노드를 때린다** |
| F09-R | .14 (tb-w3) | commerce order·pricing·product·payment·user가 tb-w3 | 현재는 일치 (우연) |
| F05-P | .11 (tb-w2) | cohort 6종 중 `testbed-payment` 접두사는 tb-w2의 **food**-payment에 매칭(commerce-payment는 tb-w3) | 게이트는 통과하나 의미가 어긋남 |

추가로 **공유 장치 문제**: tb-w1의 `/dev/vda1` 하나에 commerce PG와 banking Oracle이 **함께** 얹혀 있다.
따라서 F02-H(commerce 스토리지 IO)와 F10-P(Oracle IO)는 **구조적으로 서로를 오염시킨다** —
한쪽을 때리면 반드시 다른 도메인 DB도 함께 느려지므로, "어느 도메인의 스토리지 문제인가"를
묻는 답은 증거로 갈릴 수 없다(G5 감별 실패).

**그리고 이 배치는 고정이 아니다.** commerce·food·banking 어디에도 `nodeSelector`/affinity가 없어
재스케줄마다 바뀐다. 노드 대상 시나리오 중 런타임 재검증을 하는 것은 F05-P의 `required_cohort`
게이트뿐이고, 나머지는 전부 정적 IP를 믿는다 — **정답지가 조용히 썩는 경로다.**

### 1-6. fio 계약의 강도가 부족하다

tb-w3 `/dev/vda1` 실측(2026-07-28, fio randwrite 4k direct, 15s):
**약 18,600 IOPS**(min 13,516 / max 26,090), 72.9 MiB/s, `util 85%`.

계약의 `rate_iops`는 F10-H 4000, F10-P·F02-H 3000 — **장치 능력의 16~21%**.
`rate_iops`는 상한이므로 이 값으로는 DB를 굶길 수 없다(F10-R과 동형의 "안전한데 무피해").
정지 임계는 정적으로 알 수 없으므로 **`adaptive_ladder`로 전환**하는 것이 맞다
(라이브 31종 중 10종이 이미 사다리다).

## 2. 29종 차단 사유 분류 (실측 근거)

### A. 헌장 개정이 선행 — 4종 (기술적 결손 없음)

`F01-G` `F03-G` `F05-G` `F11-G`

컨트롤러·관측·allowlist 전부 갖춰져 있고 요구 쿼리 결손 0. 막은 것은 헌장 §1의
"no-incident(음성) 시나리오 폐지" 결정뿐이다. **코드 작업이 아니라 설계 결정 사항.**

### B. 라이브 실행만 하면 되는 것 — 1종

`F24-Q` (draft) — 러너 배선 완료. 매니페스트 runtime 블록 + controllers 등록 + **캘리브레이션 1회**.
문서로 닫을 수 없고 실행이 필요하다. 투자 대비 회수 최고.

### C. 패키지 설치 + 배선 — 4종

| id | 도구 | 그 외 잔여 |
|---|---|---|
| F02-H | fio | observations/success 신설, live_enabled 승격 |
| F10-H | fio | metadata·controller 신설, **manifest location w2→w3 수정** |
| F10-P | fio | metadata·controller 신설, **manifest location w3→w1 수정** |
| F15-P | stress-ng | metadata·allowlist 신설, co-residency 보장 코드 |

`apt-get install fio stress-ng` 1회로 도구 장벽은 사라진다. 남는 것은 순수 레지스트리 작업.

### D. 주입 설계 결함 — 3종

| id | 결함 | 필요 |
|---|---|---|
| F21-P | `profiles.json` 파라미터(`cpu/host .14/workers 3/480s`)가 **F09-R `noisy-75`와 완전 동일** — 한 주입에 두 정답(CI 금지 대상) | `db.workload` 경로로 이전. 단 `db.workload`는 `live_supported: false` |
| F21-Q | host.stress **executor CONTRACTS 미등록** → 실행 즉시 `ExecutorError`. (`prometheus.jvm_nondaemon_thread_count`는 이미 존재하므로 "busy-thread 신호 부재"는 낡음) | CONTRACTS 등록 + controller |
| F04-H | injector가 `kafka.control`인데 의미가 정반대(생산자 정지 vs 소비자 정지) | `k8s.env`로 교체 + outbox selector |

### E. 구조적 불가 — 1종

`F10-R` — **안전 구간에 피해가 없고, 피해가 나는 구간은 시나리오 정체성이 붕괴한다.**

PVC가 tb-w1 루트FS(`/dev/vda1`) 위에 있어 전용 볼륨이 없다. 계약대로 계산하면:

```
blocks=39535100 used=25764120 avail=13754596 (KiB)
desired = blocks×0.85 − used     = 7,840,715
avail − reserve(10GiB)           = 3,268,836   ← 더 작아서 채택
→ 실제 채움 ≈ 3.1GiB, 사용률 66% → 74%
```

PostgreSQL 무피해 = G3 실패. 예비를 없애 14G를 채우면 kubelet·containerd가 함께 죽어
**노드 전체 DiskPressure 사건**이 되며, 이는 "postgres 볼륨 고갈"이라는 정답과 다른 사건이다.
회생하려면 PGDATA 전용 볼륨(별도 디스크/LVM/quota)이 선행되어야 한다 —
**인프라 재구성 없이는 불가.**

### F. 앱 코드 변경 선행 — 7종

| id | 앱 갭 | 성격 |
|---|---|---|
| F03-H | `OrderController.java:70`의 `Thread.sleep`이 주석으로 "F03-H 주입 표면"이라 **자백** | G6 누설. 컨트롤러·실행기·allowlist는 완비 — **앱만 고치면 됨** |
| F02-P | `idx_menus_category`를 타는 쿼리가 food 전체에 **부재** → 인덱스를 드롭해도 피해 0 | 조회 경로 신설 + success 교체(현재 `entry_status ne 0` 하나뿐 = G3 미충족) |
| F06-P | food에 429 응답 코드 **0건** | 429 경로 구현 + metadata·controller·mock allowlist |
| F13-R | commerce에 **Ingress 리소스 자체가 없음**(nginx NodePort뿐) | Ingress 도입 + k8s.patch allowlist |
| F14-R | `OrderService.java:59-60` 멱등키 0건 (앵커 최상급) | fault-proxy 구현 + allowlist |
| F14-P | `TransferEventConsumer.java:40-42` catch-swallow (앵커 최상급) | `business.fault`가 BLOCKED stub — "ledger가 selective event-drop 제어를 노출 안 함" |
| F13-P | banking에 bounded large-history 응답 없음 | `business.fault` BLOCKED + wpm.probe 계약 |

### G. 프로파일이 fail-closed — 2종 + 위 중복

`live_supported: false`인 프로파일: `app.release` `business.fault` `db.workload` `network.fault` `wpm.probe`

| id | 차단 |
|---|---|
| F13-H | `network_fault_executor.py` BLOCKED: "WPM probe source host and interface are unresolved" + `wpm.probe` live 미지원 |
| F13-P | `business_fault_executor.py` BLOCKED (위 F항 중복) + `wpm.probe` |

`network.fault`의 거부 사유는 **"신뢰할 수 있는 주입 전송과 검증된 out-of-band 복구 경로가 없다"** —
안전 설계상 의도된 거부이며, 해제하려면 복구 경로부터 확보해야 한다.

### H. timeline.compose 계열 — 5종

`F14-R` `F15-H` `F15-T2` `F15-T3` `F15-T4`

실행기(`timeline_dual_fault` / `timeline_flap`)에는 계약이 있으나
`profiles.json`의 `timeline.compose.allowed_scenarios`는 `F08-H, F15-R, F15-T1` 셋뿐 →
**dispatcher fail-closed**. 추가로 metadata 전무.

- F15-H·F15-T2: food 429 전제(F06-P와 동일한 앱 갭)에 의존
- F15-T3: merge 결속을 만들 코드 0
- F15-T4: split 인계 구간 미구현
- F14-R: fault-proxy(F항 중복)

### I. 배선만 남은 것 — 2종

| id | 잔여 |
|---|---|
| F17-P | 관측 3종(`loadgen.frozen_bypass_completed_rate`·`loadgen.normal_path_reject_rate`·`database.integrity_violation_count`) **이미 존재**. 남은 것 = 30282 `allowed_entry_urls` 등록 + `frozen-bypass.js` dual-arm 스크립트 + controller + **cleanup 원장 역분개**(공유 계정 ACC-1001 차감이 다음 케이스 오염) |
| F04-P | "rate limit" 코드 부재(실체는 consumer concurrency 미설정) → 정답 재작성 + injector·metadata·controller 전무 |

## 3. 종합

| 분류 | 수 | 선행 조건 |
|---|---|---|
| A. 헌장 개정 | 4 | 설계 결정 (코드 아님) |
| B. 라이브 실행 1회 | 1 | 스모크에 포함 가능 |
| C. 패키지 설치 + 배선 | 4 | `apt install fio stress-ng` + 레지스트리 |
| D. 주입 설계 수정 | 3 | 충돌 해소·injector 교체 |
| E. 구조적 불가 | 1 | 인프라 재구성 (PGDATA 전용 볼륨) |
| F. 앱 코드 변경 | 7 | testbed 앱 수정 + 재빌드·재배포 |
| G. 프로파일 fail-closed | 2 | 복구 경로/제어 표면 확보 |
| H. timeline dispatcher | 5 | allowlist 등록 + metadata (일부는 F에 의존) |
| I. 배선 | 2 | 스크립트·컨트롤러·cleanup |

중복 제외 실질 합계 = 29.

**앱을 안 건드리고 도달 가능한 상한**: 31 + B(1) + C(4) + D(3) + I(2) = **41종**.
여기에 A(4, 헌장 개정 시) = 45종. 나머지 15종은 앱 수정·인프라 재구성·복구경로 확보가 선행된다.

## 4. 즉시 정정이 필요한 잘못된 기록

1. `catalog.json` F10-R `prerequisite` — "F10 계열 재설계 1순위: 좌표는 정합하므로 정답·컨트롤러·allowlist만 신설"
   → **틀림.** 좌표는 맞지만 구조적으로 사건을 만들 수 없다(E항).
2. `manifests/f10-h-*.yaml` `injection.location: worker-w2` → **worker-w3**
3. `manifests/f10-p-*.yaml` `injection.location: worker-w3` → **worker-w1**
4. F09-H·F09-P·F21-Q·F17-P의 "관측 신설" prerequisite — 해당 쿼리 전부 존재. 문구 갱신 필요
5. `CLAUDE.md`의 도메인-워커 지도가 PVC·파드 배치로 오독될 여지 — 실제로는 세 도메인이 세 워커에 뒤섞여 있다
6. **F21-P·F21-Q의 host.stress 대상 노드** — 대상 서비스가 없는 노드를 때린다(§1-5)
7. **F02-H·F10-P의 상호 오염** — tb-w1 단일 장치에 commerce PG와 banking Oracle 공존(§1-5)
8. **fio `rate_iops` 3000~4000** — 장치 능력의 16~21%로 피해 불가. 사다리 전환 필요(§1-6)

## 5. 노드 대상 시나리오의 구조적 위험 (신규 과제)

파드 배치에 `nodeSelector`가 없어 재스케줄마다 바뀌는데, 노드를 IP로 지목하는 시나리오는
그 사실을 모른 채 굳어 있다. 선택지는 셋이다.

1. **배치 고정**: 세 도메인에 `nodeSelector`/affinity를 도입해 도메인-워커 분리를 실제로 만든다.
   가장 근본적이나 앱 매니페스트 전면 수정이고, 기존 라이브 시나리오의 전제도 바뀐다.
2. **런타임 재검증**: F05-P의 `required_cohort` 게이트를 노드 대상 전 시나리오로 확대한다.
   배치가 바뀌면 fail-closed로 멈춘다 — 틀린 정답지로 캡처하는 것보다 낫다.
3. **좌표 동적 해석**: 주입 직전에 대상 서비스가 실제로 앉은 노드를 조회해 host를 결정한다.
   정답지가 안 썩지만 실행기 계약(고정 파라미터 일치 검사)과 충돌한다.

**권고는 2번(즉시) + 1번(중기)**이다. 2번은 기존 코드 패턴이 있고 fail-closed라 안전하며,
1번 없이는 "노드 X를 때리면 도메인 Y가 아프다"는 서술 자체가 계속 우연에 의존한다.
