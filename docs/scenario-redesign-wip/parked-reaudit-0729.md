# parked 12종 재심사 (2026-07-29)

07-28 재심사(`parked-29-reaudit-0728.md`) 이후 **본류가 움직였다** — 배치가 nodeSelector로
고정됐고, 순차 오프셋 타임라인(F15-T2)이 라이브로 올라갔고, 관측 평면이 분리됐다.
그 사이 parked 항목의 선행조건 문구가 실물과 어긋났는지만 본다. 라이브 검증은
네트워크 미복구로 불가하므로 **이 재심사는 승격을 하지 않는다** — 문구를 실물에 맞추고
남은 일을 정확히 적는 것까지가 범위다.

대상 = parked 11 + draft 1 = 12종.

## 1. 결론 요약

| 판정 | 수 | 대상 |
|---|--:|---|
| **선행조건이 틀렸다 — 실제 잔여가 훨씬 작다** | 2 | F15-T3, F15-T4 |
| 실측 반증 — 재설계 없이는 안 열린다 | 6 | F04-P, F07-P, F14-R, F20-P, F24-Q, F10-R |
| 능력갭 유지 — 07-28 판정 그대로 | 4 | F02-P, F13-R, F13-H, F13-P |

움직인 것은 F15-T3·T4 둘뿐이다. 나머지 10종은 실물이 바뀌지 않았으므로 판정도 그대로다.

## 2. F15-T3 — 선행조건 세 줄 중 세 줄이 다 틀렸다

카탈로그 선행조건: *"merge 결속 구현 + metadata 신설 + dispatcher 등록"*
dispatcher 거부 사유(`timeline_compose_dispatch.py:26`): *"worker placement and consumer stall SLA are unresolved"*

### 2-A. "worker placement 미해결" — 07-28에 해결됐다. 그리고 정답지 좌표가 틀렸다

`fix-F15-T3-metadata.json`은 대상 노드를 `cluster-node(tb-w3)`로 적고 있다.
실물은 다르다:

```
core-banking/k8s/23-ledger-service.yaml:18-19
      nodeSelector:
        kubernetes.io/hostname: tb-w2
```

07-28 배치 고정에서 banking은 **tb-w2**로 못 박혔다(commerce=w1, banking=w2, food=w3).
같은 날 `ba625bd`가 노드 대상 시나리오 좌표를 22+7건 갱신했지만 **parked 항목은 그 빗질에
들어가지 않았다**. F15-T3는 07-28에 발견한 "때리지 않는 노드를 관측한다"(`8411e0d`)와
정확히 같은 결함을 그대로 들고 있다 — 다만 parked라 아직 가짜 성공을 낼 기회가 없었을 뿐이다.

→ **이 문서와 함께 tb-w3 → tb-w2로 정정한다.**

### 2-B. "merge 결속 구현" — 결속할 것이 애초에 하나뿐이다

정답지 자신이 `has_single_cause: true`라고 적는다. 주입은 **host CPU 포화 하나**이고
consumer stall은 그 하류 전파다. 하나짜리 주입에 compose는 필요 없다.

카탈로그가 `profiles: ["timeline.compose"]`를 배정한 것이 그래서 오배정이다. 같은 모양의
`F15-P`(공용 노드 압박)는 `profiles: ["host.stress"]` 단독으로 이미 ready다. T3도 같아야 한다.

→ 잔여는 "merge 결속 구현"이 아니라 **프로파일 재배정**이다.

### 2-C. "consumer stall SLA 미해결" — 관측 두 축이 이미 등록돼 있다

```
kubernetes.kafka_consumer_lag   allowed_parameters: [namespace, pod, bootstrap_server, consumer_group]
prometheus.node_cpu_utilization allowed_parameters: [node]
```

정답지가 요구한 `kafka.consumer_lag{group=ledger-service}` + 노드 host CPU 동시 노출이
파라미터까지 그대로 있다. SLA(몇 분 만에 lag이 판정 가능한 크기로 벌어지는가)는
**캘리브레이션으로 정하는 값**이지 미해결 계약이 아니다.

### 2-D. 코드 앵커 재검증 — 유효

```
TransferEventConsumer.java:28  @KafkaListener(groupId = "ledger-service")
ledger-service/application.yml:25-29  kafka.consumer 에 concurrency 없음 = 단일스레드
```
둘 다 실소스에 그대로 있다.

### 2-E. 신규 감별 부채 — F14-P와 같은 컨슈머를 쓴다

오늘 승격한 **F14-P가 바로 이 `TransferEventConsumer`를 쓴다**(catch-swallow + 자동 ack).
T3의 `must_rule_out`은 F04-R·F18-P만 배제하고 F14-P를 모른다 — 정답지 작성 시점에
F14-P가 ready가 아니었기 때문이다.

감별축은 명확하다: **F14-P는 유실(lag 없이 원장 행이 영영 안 옴), F15-T3는 적체(lag이
자라고 CPU를 걷으면 따라잡는다)**. 배제 조건 1줄 추가가 필요하다.

### F15-T3 실제 잔여
1. 좌표 정정 tb-w3 → tb-w2 *(이 커밋에서 처리)*
2. `profiles`를 `timeline.compose` → `host.stress`로 재배정
3. metadata를 `registry/scenario-metadata.json`으로 반입 (+ F14-P 배제 1줄)
4. controller 작성 (fixed)
5. 캘리브레이션 — **라이브 필요, 네트워크 복구 대기**

## 3. F15-T4 — 주입기 둘 다 실재, 잔여는 "인계" 한 가지

카탈로그 선행조건: *"split 인계 구간 구현 + metadata 신설 + dispatcher 등록"*
dispatcher 거부 사유: *"handoff close interval and consumer drain SLA are unresolved"*

주입기는 07-28 재심사 시점부터 둘 다 있었고 지금도 있다 — `db.lock`(inventory, F02/C2)과
`kafka.control`(shipping scale=0, F04-R). **07-29에 달라진 것은 순차 스케줄링이
라이브에 올라갔다는 점이다**:

```
timeline_lock_mock_executor.py:62-66  start_offset_seconds (F15-T2는 60~600초)
timeline_lock_mock_executor.py:226    [[ "$offset" -eq 0 ]] || sleep "$offset"
```

다만 이것으로 T4가 그대로 되지는 않는다. T2는 **1구간을 붙잡은 채** 2구간을 얹는다
(offset 뒤에도 PG 락이 살아 있어 두 창이 겹친다). T4가 요구하는 것은 **비중첩 인계** —
락을 풀고 나서 shipping을 세워야 "동시 시작이면 multi-root"라는 배제 조건이 성립한다.

→ 잔여는 "split 인계 구간 구현"이 맞다. 다만 신규 골격이 아니라 **T2 실행기에
`run` 안에서 1구간을 먼저 해제하는 단계를 넣는 변형**이다. 블로킹 `sleep`을 러너가
이미 최대 600초까지 견딘다는 것이 T2로 실증됐으므로 전송 계약도 새로 필요 없다.

"consumer drain SLA"도 T3와 같은 성격 — 캘리브레이션 값이지 미해결 계약이 아니다.

### F15-T4 실제 잔여
1. `timeline_lock_mock_executor` 변형 = 락 해제 → 간격 → shipping scale=0 (신규 executor 아님)
2. metadata 반입 + dispatcher ROUTES 등록
3. controller 작성
4. 구간별 시간창을 캡처 메타에 분리 기록 (split 판정 근거)
5. 캘리브레이션 — **라이브 필요**

## 4. 나머지 10종 — 판정 유지

### 실측 반증 6종 (재설계 전에는 안 열린다)
| id | 반증 요지 | 열리려면 |
|---|---|---|
| F04-P | 건당 서비스시간 1~3ms(능력 300~500/s) vs 도달 유입 ≤72/s | 소비 경로에 실제 지연 신설 = 앱 변경 |
| F07-P | pricing bulkhead 20 vs 응답 2.5~12ms → 동시성 0.1 | 부하 형상 재설계 |
| F14-R | 안쪽 타임아웃 10s < 바깥 15s → "커밋됐는데 응답 유실"이 구조적으로 불가 | fault-proxy 신설 |
| F20-P | 진입점 30082에 `/api/transfers/stats/daily` 패스스루 부재 → 404 | 라우팅 신설 |
| F24-Q | restaurant `/menu` 67~95ms vs read-timeout 5s → 포화 미발생 | 지연 주입 수단 |
| F10-R | PVC가 루트FS 위 → 계약대로면 66→74%, PostgreSQL 무피해 | PGDATA 전용 볼륨 |

여섯 다 **"부하를 부으면 뻗는다"를 서비스시간 측정 없이 가정**했던 것이 무너진 사례다.
반증 근거는 실측이므로 문구 수정 없이 유지한다.

### 능력갭 4종 (07-28 판정 그대로, 실물 변화 없음)
- **F02-P** — `idx_menus_category`를 타는 쿼리가 food 전체에 부재. 조회 경로 신설(앱 변경) + success 교체
- **F13-R** — commerce에 Ingress 리소스 자체가 없다(nginx NodePort뿐)
- **F13-H** — `network_fault_executor.py:21` BLOCKED. 거부는 안전 설계상 의도된 것 — 검증된 out-of-band 복구 경로가 선행
- **F13-P** — `business_fault_executor.py:13` BLOCKED + WPM download-phase 계약. WPM 복원 트랙 미착수

## 5. 다음

네트워크가 복구되면 **F15-T3 → F15-T4 순**으로 집는다. T3가 먼저인 이유는 주입기·관측·
좌표가 다 서고 캘리브레이션만 남기 때문이고, T4는 실행기 변형이 선행하기 때문이다.
둘 다 열리면 ready 44 → 46.
