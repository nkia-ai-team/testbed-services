---
title: 44종 라이브 배치 결함 기록 (2026-08-03)
status: Draft
owner: project
last_reviewed: 2026-08-03
tags:
  - scenario
  - defect
  - live-batch
summary: 큐 live-44-8890d512 실행 중 발견된 결함을 층별로 기록한다. 정적 감사로는 하나도 잡히지 않았고, 전부 실물 관측으로 확증했다.
---

# 44종 라이브 배치 결함 기록 (2026-08-03)

큐 `live-44-8890d512`, 스모크 패스. **배치 자체가 결함 발견 수단**이라는 전제
(`docs/scenario-redesign-wip/` 이전 감사 문서들)가 다시 한번 그대로 입증됐다 —
아래 21건 중 **정적 감사로 잡을 수 있었던 것은 없다**. 매니페스트·계약·파라미터는
모두 정상으로 읽혔고, 실패는 파드 안 파일 이름, sqlplus 한 줄의 순서,
지표의 실제 분포처럼 **돌려봐야만 보이는 자리**에 있었다.

## 요약

| # | 시나리오 | 층 | 결함 | 조치 |
|---|---|---|---|---|
| 1 | F12-H (F09-P 예정) | 주입 한계 | CPU limit 사다리 바닥이 pod requests보다 낮아 k8s가 거부 | 스킵 |
| 2 | F05-R | 레버 부적합 | memory limit 하향으로는 JVM을 OOMKill 못 시킴 | 스킵 |
| 3 | F08-P | 주입 크기 | read-timeout 250ms가 대상 p95(27ms)의 9배 | 스킵 |
| 4 | F09-R (F15-P 예정) | 관측 | `kcm.node.cpu_utilization`이 실제 CPU를 못 읽음 | 스킵 |
| 5 | F01-P·F08-G·F15-G | 전송로 | Oracle 프로브 3종이 "Session altered."를 값에 섞음 | **수리** |
| 6 | F01-P·F08-G·F15-G | 레포 불일치 | 세션 태그가 매니페스트↔러너 허용목록 간 갈라짐 | **수리** |
| 7 | F01-P | 주입 | Oracle 락이 한 번도 걸린 적 없음 (원격 인용 오류) | 스킵, 수리 대기 |
| 8 | F08-G | 게이트 | `_safe_bool`이 프로브 예외를 조용히 False로 → 헛막힘 | 재시도로 통과 |
| 9 | F15-G | 주입 | 07-31에 고친 pid 파싱 결함이 형제 실행기에 안 옮겨져 있었다 | **수리** |
| 10 | **ready 9종** | 판정 구조 | 회복 게이트가 cleanup이 지우는 자기 부하기 파일을 읽는다 | **수리** |
| 11 | F05-P | 레포 불일치 | 매니페스트가 러너에 없는 검사 id를 부른다 | 스킵 |
| 12 | F15-T1 | 레포 불일치 | 같은 파라미터가 두 레지스트리에서 다르다 | 스킵 |
| 13 | F18-P (banking 전반) | 환경/시드 | 정산 계좌가 말라 이체가 2/3 실패 — 07-20 수정이 실행 DB에 닿은 적 없다 | **복구** |
| 14 | core-banking 전체 | 관측 | baseline 스크립트에 `step` 태그가 없어 관측 평면이 처음부터 죽어 있었다 | 미수리 |
| 15 | F18-P | 주입 부작용 | 릴레이를 끄면 health가 응답을 멈춰 liveness가 유일한 파드를 죽인다 | 스킵 |
| 16 | **17종 잠재** | 판정 진단 | companion 부하가 레벨 timeout보다 짧아, 오래 도는 런은 신호가 사라진 뒤 엉뚱한 사유로 죽는다 | 스킵·기록 |
| 17 | F05-H·F16-H·F17-R | 정리 검증 | 주입이 배포의 progress deadline을 넘기면 굳은 실패 조건이 cleanup 검증을 오염시켜 전역 DIRTY가 된다 | 스킵·기록 |
| 18 | F20-Q | 주입 크기 | 성공 임계(768Mi)가 서비스가 견디는 지점 위에 있다 — 624MB에서 먼저 죽는다 | 스킵 |
| 19 | F23-R (파괴적 시나리오 뒤 전부) | 간격 | 앞 시나리오가 DB를 재기동시키면 5분 창이 비어 다음 시나리오가 게이트에 막힌다 | 대기 후 재개 |
| 20 | F15-R·F03-H (+F15-T1) | 레포 불일치 | 승인 파라미터가 한 벌뿐인데 사다리는 여러 단 — 주입도 정리도 거부돼 전역 DIRTY | 스킵 |
| 21 | **live 7종** | 레포 불일치 | 같은 계약 안 두 목록이 갈라져 부하 태그가 거부된다 — 남은 배치의 절반 | **수리** |

---

## 1. 사다리 바닥을 requests가 잘라먹는다 (F12-H, F09-P)

`testbed-product`의 CPU limit을 100m로 내리려는데 그 파드 **requests가 200m**이라
쿠버네티스가 거부한다(`limits < requests` 불가). `k8s_patch_executor`는 `--limits`만
설정하고 requests는 건드리지 않는다. **한 번도 주입된 적 없는 시나리오였다.**

CPU limit 주입 3종의 사다리:

- **F12-H** 250m / **100m / 50m** ← 막힘
- **F09-P** 250m / **100m / 50m** ← 같은 벽 예정
- F21-P 400m / 300m / 200m ← 전부 ≥200m, 안전 (07-31 레버 교체 때 맞춰짐)

같은 구조가 메모리 쪽에도 있다 — F05-R의 memory limit 사다리 바닥 576Mi 아래로는
requests 512Mi 때문에 한 단(512Mi)밖에 못 간다.

**선택지:** (A) 사다리를 200m 위로 — 매니페스트만 고치면 되지만 500m→250m가 충분히
조이는지 불확실 (B) requests도 함께 내리도록 실행기 수정 — 원리적으로 맞고 재검증 필요
(C) 앱 requests 하향 — 파급 과다, 비권장.
**A/B 판단에 대상 파드 평시 CPU 실측이 선행돼야 한다**(109에 metrics-server 없음).

## 2. limit 하향은 JVM 상대로 OOMKill 레버가 아니다 (F05-R)

사다리 3단(768→640→576Mi)을 다 쓰고 `calibration_levels_exhausted`.
25틱 내내 `restart_count=0`, `termination_reason=None`. 배관은 정상이었다.

| 항목 | 값 |
|---|---|
| payment-service working set | **338Mi** (평시 부하) |
| 사다리 바닥 | 576Mi = working set의 1.7배 |
| JVM `MaxRAMPercentage` | **25 (기본값)** — `JAVA_TOOL_OPTIONS`엔 javaagent뿐 |
| 1Gi limit에서 `MaxHeapSize` | 256Mi |

컨테이너 limit을 내리면 **힙 상한이 같이 내려간다**(576Mi → 힙 144Mi). 컨테이너
OOMKill은 native+heap이 limit을 넘어야 나는데, 힙이 먼저 줄어 그 일이 안 일어난다.
힙을 굶겨 Java `OutOfMemoryError`를 내는 우회로도 막혀 있다 — 매니페스트
`must_rule_out`의 `non-oom-restart`(`termination_reason == "Error"`)가 정당하게 실격시킨다.

**고치려면** 명시적 `-Xmx`를 새 limit보다 **높게** 박아두고 limit을 내리거나,
할당을 늘리는 앱 결함 표면을 쓴다. 사다리를 한 단 더 내리는 것으로는 안 된다.

## 3. 주입 크기를 대상 실측 없이 정했다 (F08-P)

`SPRING_APPLICATION_JSON`으로 order-service에 `services.payment.read-timeout=250ms`가
정상 주입됐고 파드도 재기동됐다. 51틱 내내 `order_error_rate` 0.0, `checkout_5xx_rate` 0.0.

주입 창(23:34–23:46 UTC) `commerce-payment` SERVER 스팬 실측:

| p50 | p95 | p99 | max |
|---|---|---|---|
| 15.7ms | **27.1ms** | 145ms | 299ms |

**250ms는 p95의 9배**다. 물릴 수가 없다. 레버는 맞다 — `services.payment.read-timeout`은
order-service `RestClientConfig`에 실제로 바인딩돼 있다. **값만 크다.**
물게 하려면 ~20ms 이하로 내리거나 payment를 동시에 느리게 만들어야 한다.

**#2와 같은 뿌리다**: 임계·사다리를 정하기 전에 대상의 평시 분포를 안 쟀다.

### 재는 법

```bash
# 메모리/CPU (109엔 metrics-server 없음)
kubectl -n <ns> exec <pod> -c <container> -- sh -c 'cat /sys/fs/cgroup/memory.current'

# 지연 — 컬럼명은 service_name·span_kind·duration_ns (ServiceName/Duration 아님)
curl -s "$CLICKHOUSE_URL" -H "X-ClickHouse-User: lucida" -H "X-ClickHouse-Key: $CLICKHOUSE_PASSWORD" \
  --data-binary "SELECT quantile(0.95)(duration_ns/1e6) FROM lucida.otel_traces_local
                 WHERE service_name='commerce-payment' AND span_kind='SERVER'
                 AND timestamp >= '...' AND timestamp <= '...'"
```

접속·자격증명은 러너 `.env`의 `CLICKHOUSE_URL`(`192.168.230.119:18123`),
`CLICKHOUSE_USER`/`CLICKHOUSE_PASSWORD` — **`CH_PASSWORD`가 아니다.**

## 4. `kcm.node.cpu_utilization`이 노드 CPU를 못 읽는다 (F09-R, F15-P)

F09-R은 `must_rule_out`의 `symptom-without-node-load`(`node_cpu_util < 50`)가 3틱 연속
잡혀 abort했다. 주입은 정상이었다(주입 후 tb-w1 load average 5분 1.35).

**대조 실험** (큐 정지 중 tb-w1, `yes` 2개 × 75초, 4코어):

| | 값 |
|---|---|
| `/proc/stat` 차분 실측 | **53%** (설계대로 "noisy-50") |
| `kcm.node.cpu_utilization{node="tb-w1"}` 같은 구간 | 23:58 **7.4** → 23:59 **3.3** |
| 부하 직전 평시 | 15~28 |

**지연이 아니라 역주행이다.** 실제 F09-R 런에서도 같았다(4분 주입 내내 4.6~13%,
주입 전 15~28%).

### 지표를 고쳐도 남는 설계 결함

F09-R 사다리 0단은 "noisy-50" = 4코어 중 2워커 ≈ **50%**인데 감별자 바닥이 **`< 50`**이다.
완벽히 주입해도 경계에 걸리고, `must_rule_out`이 `escalate`보다 **먼저** 평가되므로
1·2단으로 올라갈 기회 자체가 없다 — **사다리가 구조적으로 도달 불가**다.

### 영향 범위 (`node_cpu_util` 사용 7종)

- **막힌다**: F09-R(스킵), **F15-P**(성공조건 `node_cpu_util ≥ 80` + `node_mem_util ≥ 75`)
- **감별자만 죽는다**: F10-H·F02-H·F10-P는 `node_cpu_util ≥ 85`를 confound 배제용으로만
  쓴다. 과소보고라 영영 안 터진다 → 막히진 않지만 **가짜 통과를 거를 수단이 없다**
- **미확인**: `kcm.node.mem_utilization`도 같은 병인지 안 쟀다. **F05-P**(성공
  `node_mem_util ≥ 92`)와 F15-P가 걸린다. 주입 중엔 오염되니 **큐 정지 중에만** 측정할 것

측정 방법: 109에서 `ssh -i /root/.ssh/tb_key nkia@<worker>` → `/proc/stat` 두 번 차분.
tb-w1은 4코어. 태운 뒤 `pgrep -c yes`로 잔재 0 확인 필수.

## 5. Oracle 프로브 3종이 값에 "Session altered."를 섞었다 — 수리함

F01-P가 주입도 못 하고 `cleanup_failed` → `dirty_run` → **큐 전체 정지**.

`db_lock_executor.py`의 `check_row()`가 `alter session`을 `set feedback off` **앞**에 둬서
sqlplus가 "Session altered."를 두 번 찍고, `tr -d '[:space:]' | grep -qx 1`이
`Sessionaltered.Sessionaltered.1`을 보게 됐다. **행은 멀쩡히 있었다(count=1).**

**왜 교착인가:** Oracle 경로는 `preflight) check_row; ! alive`이고 `recovery) ! alive; check_row`다.
같은 깨진 함수가 **주입 입구와 청소 출구를 동시에** 막아 런이 스스로 못 씻고,
DIRTY는 전역이라 나머지가 전부 선다. 정규 `POST /api/scenarios/F01-P/cleanup`은 exit 1.

같은 순서 문제가 형제 프로브 2개에도 있었다(실측 확인):

- `db_table_readonly_executor.py` `state()` → **`Sessionaltered.NO`** 반환,
  `expect()`가 `"NO"`와 비교 → 항상 거짓 (**F14-P**, index 43)
- `timeline_multi_injection_executor.py` `o_rowok`/`o_sessions`
  (**F08-G** index 13, **F15-G** 14 — `oracle_lock` 스텝)

셋 다 `set pages 0 feedback off heading off`를 alter 앞으로 옮겨 수리했다.
판정 조건·임계·사다리는 건드리지 않았다(전부 검증 전송로다).

### 교착을 푸는 절차

1. 소스를 고쳐 109 `/root/testbed-services/scripts/scenarios/profiles/`에 배포
2. `POST /api/scenarios/<id>/cleanup?repair_capsule=true` — 캡슐이 얼린 실행기를
   살아있는 trusted root에서 다시 잘라낸다. **정확히 이 상황을 위한 스위치**
   (선례: 07-30 F21-P의 따옴표 없는 `&`)

**함정:** scp 후 `chmod 640`을 하면 `trusted profile executor is unavailable`로 실패한다.
형제 실행기는 **`-rwxrwxr-x ydkim:ydkim`**이다. 배포 검증은 컨테이너 안에서
`docker exec ... md5sum`과 `ls -la` **둘 다**.

## 6. 세션 태그가 두 레포 사이에서 갈라져 있었다 — 수리함

| | 값 |
|---|---|
| 매니페스트 3종이 주입·관측하는 태그 | `dba-maintenance` |
| 러너 `APPROVED_ORACLE_TAGS` | `rca-F01-P-oracle-lock` / `rca-F08-G-…` / `rca-F15-G-…` |

매니페스트 쪽이 옳다 — 세션 태그에 시나리오 id가 박히면 **L2 정답 누설**이라
(`spec-scenario-quality-charter.md` G6) 실제 DBA 세션처럼 위장하도록 바꾼 것이다.
러너만 안 따라왔고 프로브는 첫 틱부터 `Oracle session tag is not allowlisted`를 냈다.

**치명적인 이유:** `database.oracle_tagged_session_count`가 **F01-P·F08-G·F15-G 셋 모두의
recovery 게이트**에 있다(`lock-cleared == 0`). 읽을 수 없는 관측은 영원히 통과 못 하므로
매번 `recovery_timeout` → `mark_dirty` → **전역 DIRTY**. 셋은 큐에서 12·13·14로 **연속**이라
연달아 세 번 선다. F01-P는 주입·유지 13분·cleanup까지 정상이었다 — 망가진 건
"끝났음을 확인하는" 쪽뿐이었다.

**수리:** `APPROVED_ORACLE_TAGS = {"dba-maintenance"}`. 옛 이름 3개는 아무 매니페스트도 안 쓴다.
러너 backend는 **이미지에 구워지므로 bind-mount가 아니다** — `docker compose build` + `up -d` 필요.

### 가드가 있었는데 왜 안 울렸나

`test_every_live_controller_observation_passes_the_probe_allowlists`가 바로 이걸 잡으라고
있는 테스트인데 **`prometheus`·`clickhouse` 어댑터만 검사하고 `database`는 빼놨다**
("서브프로세스가 필요해서"). 허용목록 검사는 서브프로세스보다 **먼저** 돌므로 뺄 이유가 없었다.
`guarded`에 `database`를 추가했고, 되돌려 확인하니 세 시나리오를 정확히 지목하며 실패한다.

## 7. F01-P의 Oracle 락은 한 번도 걸린 적이 없다

#5·#6을 고치고 나서야 보였다. 감별자 `injected-lock-absent`(`tagged_db_sessions == 0`)가
31틱 연속 잡아 **정당하게** abort시켰다 — 락이 실제로 없었다.

Oracle 파드 `/tmp`에 남은 물증 (파일 이름이 문자 그대로 `$tag`):

```
$tag.sql   alter session set current_schema='';
           begin dbms_session.set_identifier(''); end;
           select '' from '' where ''='''' for update;
           host sleep '';
$tag.log   SP2-0310: unable to open file "/tmp/$tag.sql"
$tag.pid   807706        <- nohup된 셸의 pid. 실행기는 이걸 보고 성공이라 했다
```

`ORACLE_REMOTE`의 apply가 `'"'"'$tag'"'"'` 꼴로 써서 원격에 **리터럴 `'$tag'`**를 보낸다.
원격 `sh -lc`에는 그 변수들이 없으므로 heredoc(`<<EOF`, 구분자 비인용)에서 전부 빈
문자열로 전개된다. 파일명도 리터럴 `$tag.sql`이 되어 sqlplus가 열지도 못했다.

**정상 대조군:** `timeline_multi_injection_executor.py`의 `oracle_lock`은 `'"$tag"'` 꼴로
**로컬 셸에서 치환**해 보낸다. F08-G·F15-G는 이 구현을 쓰므로 무관하다.

### 3중 은폐

1. `echo $! > pid`는 항상 성공하고, 실행기는 **pid 파일 존재만** 본다 — sqlplus 결과는 안 본다
2. `alive()`/`stop()`은 `/tmp/dba-maintenance.pid`를 보는데 run은 `/tmp/$tag.pid`에 쓴다.
   **경로가 어긋나 cleanup이 잔재를 영영 못 지우고**, preflight의 `! alive`도 항상 통과한다
3. 이걸 볼 수 있는 유일한 관측이 #6으로 죽어 있었다

07-31의 PostgreSQL `db.lock` pid 결함과 판박이다 — 그쪽도 "라이브 성공 이력 없음"이었다.

**고치려면** 값을 원격에 **인자로** 넘겨야 한다(PostgreSQL 경로처럼 `sh -lc '...' -- "$tag" …`
또는 env). 잔재 청소 경로도 pid 파일 경로 하나로 통일해야 한다.
`$tag.*` 잔재 3개는 수동 삭제했다.

## 8. 자격 게이트가 예외를 조용히 False로 바꾼다 (F08-G)

F08-G가 `blocked=['check_failed:baseline-business-success']`로 주입 전에 죽었다
(mutations 0건). 12분 전 F01-P는 같은 체크가 `true`였다.

게이트와 **똑같은 질의**를 3회 실측 — 5분간 PAID **49·50·50건**, 임계는 5.
데이터는 임계의 10배였다. 그냥 재시도하니 통과했다.

```python
def _safe_bool(action):
    try: return action() is True
    except Exception: return False
```

"비즈니스가 망가졌다"와 "프로브가 한 번 넘어졌다"가 **구별되지 않는다**. 같은 순간
kube-context·kube-node-set·target-health는 통과했으니 kubectl 전반의 문제는 아니었다.
사유가 안 남아 무엇이 던졌는지 알 수 없다 — **이것이 핵심 결함이다.**

게다가 `check_failed:*`는 `TRANSIENT_AUTO_RETRY_REASONS`에도 `..._PREFIXES`에도 없어
**큐가 자동 재시도하지 않고 그대로 선다.**

### 같은 병이 준비 점검 층에서도 나온다

2026-08-03 F25-H 차례에서 큐가 시나리오를 시작하지도 못하고 섰다:

```
operational readiness failed: preflight_signals
```

곧바로 `/api/live-queue/readiness`를 부르니 **17개 검사 전부 통과**였다. 일시적 실패였고,
resume 한 번으로 F25-H가 정상 시작했다.

`readiness()`도 같은 모양이다 — `preflight_probe.collect()`가 던지면 예외를 삼키고
`preflight_signals: False`로 바꾼다. 사유가 남지 않고, 자동 재시도도 없다.
**한 번의 프로브 실패가 배치를 세운다.**

대응은 아래와 같다 — 막히면 먼저 같은 것을 직접 재보고, 값이 멀쩡하면 resume한다.

**대응:** `check_failed:baseline-*`로 막히면 **먼저 게이트와 같은 질의를 직접 재라.**
값이 멀쩡하면 프로브 결함이니 resume해서 재시도시킨다 — 스킵할 일이 아니다.

```bash
kubectl -n rca-testbed-commerce exec testbed-postgres-0 -- sh -lc \
  "psql -U \$POSTGRES_USER -d \$POSTGRES_DB -At -c \
   \"SELECT count(*) FROM order_schema.orders WHERE status='PAID' AND created_at >= now() - interval '5 minutes'\""
```

## 9. pid 파싱 수정이 형제 실행기로 안 옮겨졌다 (F15-G) — 수리함

F15-G가 주입 즉시 죽고 **전역 DIRTY**. 사유가 07-31과 **한 글자도 다르지 않았다**:

```
profile_apply_failed: ... profile apply failed: lock session did not report a backend pid
```

`timeline_multi_injection_executor.py`의 `pg_lock` 스텝이 아직 `sed -n '1p'`였다.
psql이 `BEGIN` 명령 태그를 먼저 찍으므로 첫 줄은 pid가 아니다. `db_lock_executor.py`는
07-31(`1c33eea`)에 "첫 번째 숫자-only 줄"로 고쳤지만 **이 실행기에는 옮겨지지 않았다.**

### 실패가 두 겹으로 쌓였다

apply가 30초를 헛돌며 실패하는 동안 **러너 리스가 만료**됐고, 그래서 cleanup이
`runner lease is expired`로 거부돼 DIRTY가 됐다:

```
cleanup reason: timeline.multi:...runner lease is expired; load.north_south:...runner lease is expired
```

리스 만료는 07-31에 "미해결"로 남겨둔 자해 구조다(주입 서브프로세스가 이벤트 루프를 막아
하트비트가 못 돈다). pid 결함이 고쳐지면 "apply가 30초를 태우며 실패"하는 상황 자체가
사라지므로 당장은 재발하지 않지만, **근본은 그대로 남아 있다.**

환경 자체는 깨끗했다 — db-client 파드 0, `idle in transaction` 0, Oracle 태그 세션 0.
apply 실패 경로가 클라이언트 파드를 지우고 나갔다.

### 가드를 함께 붙였다

`db_lock` 쪽에는 실측 로그(`"BEGIN\n35250\n1\n"`)로 파이프라인을 직접 돌려보는 가드가
있었는데(`test_f06h_db_table_lock.py`), timeline 쪽에는 없었다. 같은 가드를
`test_timeline_multi_injection_executor.py`에 추가했고, **수정을 되돌려 실패를 확인**했다.

**주의:** `ORCHESTRATOR`는 `br"""..."""` 바이트 리터럴이라 주석도 **ASCII만** 가능하다
(한글을 넣으면 `SyntaxError: bytes can only contain ASCII literal characters`).

영향 범위는 F15-G 하나다 — `timeline.multi`를 쓰는 나머지 F08-G는 `pg_lock` 스텝이 없다.

## 10. 회복 게이트가 cleanup이 지우는 신호를 읽는다 (ready 9종)

F03-P가 사다리 3단을 모두 적용·정리하고 **cleanup까지 성공한 뒤** `recovery_timeout`으로
DIRTY가 됐다. 주입은 제대로 되돌아가 있었다 — `SPRING_APPLICATION_JSON` 제거됨, 파드 Running.
망가진 건 "되돌아갔음을 확인하는" 쪽이다.

회복 조건은 셋인데 그중 하나가 이렇다:

```json
{"id": "pool-restored", "observation": "checkout_5xx_rate", "op": "lt", "value": 0.02}
```

`checkout_5xx_rate`의 출처는 **시나리오 자신의 부하기**가 쓰는
`/tmp/rca-scenario-F03-P-live.json`이다. 그런데 cleanup이 companion 부하(`load.north_south`)를
걷으면서 그 파일을 지운다. 회복은 **cleanup 다음에** 평가되므로
(`actions.cleanup.recovery_gate: true`), 그 신호는 회복을 판정할 시점에
**구조적으로 존재할 수 없다.**

```
cleanup load.north_south  complete_at 01:48:37   <- live.json 삭제
recovery_timeout          01:58:43               <- 정확히 10분 뒤
```

마지막 8틱 내내 `achieved_rps`·`checkout_5xx_rate`·`entry_status`가
`No such file or directory`로 unusable이었다. 읽을 수 없는 조건은 영원히 통과 못 한다.

### 영향 범위 — 회복 게이트가 loadgen_summary를 읽는 ready 9종

| 시나리오 | 큐 index | 게이트 관측 |
|---|---|---|
| F03-P | 16 (스킵됨) | `checkout_5xx_rate` |
| F19-P | 21 | `order_create_5xx_rate` |
| F19-S | 22 | `order_create_5xx_rate` |
| F16-H | 23 | `write_401_rate` |
| F25-H | 26 | `checkout_5xx_rate` |
| F03-H | 29 | `checkout_5xx_rate` |
| F06-P | 30 | `order_create_429_rate` |
| F15-H | 41 | `food_create_429_rate` |
| F15-T2 | 42 | `food_create_429_rate` |

**아홉 번 모두 전역 DIRTY로 큐를 세운다.** 지금까지 나온 것 중 가장 넓은 결함이다.

### 고칠 수단은 이미 있다 (07-29에 만들어졌고 아무도 안 썼다)

`_loadgen_observation`은 `domain` 파라미터를 받으면 시나리오 부하기 대신
**상주 baseline 부하기의 문서**를 읽는다. 그 docstring이 정확히 이 문제를 말한다:

> Separates the observation plane from the injection plane: this document exists
> whether or not the running scenario pours load into that domain.

바로 아래 주석은 이렇다 — **"The 43 live controllers pass no parameters and are untouched."**
배관만 깔리고 매니페스트가 채택하지 않았다.

실물 확인(2026-08-03 11:03 KST) — 셋 다 살아 있고 신선하며 필요한 필드를 갖고 있다:

```
commerce      achieved_rps 6.04  checkout_5xx_rate 0.0  observed_at 02:03:37Z
core-banking  achieved_rps 3.07  checkout_5xx_rate 0.0  observed_at 02:03:37Z
food-delivery achieved_rps 4.00  checkout_5xx_rate 0.0  observed_at 02:03:38Z
```

도메인 이름은 `commerce` / **`core-banking`** / **`food-delivery`**다(네임스페이스 이름과 다르다).

### 수리 (2026-08-03 적용, 임계는 그대로)

성공 조건은 시나리오 부하 위에서 판정해야 하므로 기존 관측을 그대로 두고,
**baseline 출처의 관측을 하나 더 추가해 회복 조건만 그쪽으로** 돌린다.

```json
{"id": "checkout_5xx_rate_baseline", "adapter": "loadgen_summary",
 "query_id": "loadgen.checkout_5xx_rate",
 "parameters": {"domain": "commerce"}, "freshness": "30s"}
```

`0.02`라는 임계는 건드리지 않았다. 바뀌는 것은 **어느 트래픽 위에서 재는가**뿐이고,
시나리오 부하가 이미 멈춘 시점에는 baseline이 유일하게 남아 있는 트래픽이므로
의미상으로도 이쪽이 맞다.

**정본은 `registry/controllers.json`이고 매니페스트는 `generate-manifests.py`의 생성물**이다.
레지스트리만 고치고 재생성하면 9개 매니페스트가 정확히 따라온다.

러너 쪽 배관은 손댈 필요가 없었다 — 라이브 경로가 쓰는
`backend/app/observation_queries.json`에는 **모든 loadgen 질의에 이미 `allowed_parameters: ["domain"]`**이
들어 있다(testbed `registry/queries.json` 사본에는 없지만 그쪽은 라이브 바인딩에 쓰이지 않는다).

컨테이너 안에서 실제로 값이 읽히는 것까지 확인했다:

```
loadgen.checkout_5xx_rate       commerce      -> k6:baseline:commerce:checkout_5xx_rate
loadgen.write_step_status_rate  commerce      -> k6:baseline:commerce:business_nonok_rate
loadgen.food_create_status_rate food-delivery -> k6:baseline:food-delivery:business_5xx_rate
loadgen.food_create_429_rate    food-delivery -> k6:baseline:food-delivery:business_429_rate
```

측정 당시 commerce가 `checkout_5xx_rate = 1.0`이었는데, 이는 F17-R이 그 순간 commerce에
장애를 주입 중이었기 때문이다(읽기 경로는 `read_2xx_rate = 1.0`으로 정상). **baseline 평면이
주입된 손상을 그대로 반영한다**는 뜻이므로 회복 게이트로 쓰기에 적합하다 — 값이 늘 0이라
공허하게 통과하는 종류가 아니다.

### 가드도 함께 넓혔다

`test_every_live_controller_observation_passes_the_probe_allowlists`에 `loadgen_summary`를
추가했다(#6에서 `database`를 추가한 것과 같은 이유로 빠져 있었다). 단 파라미터가 없는
loadgen 관측은 **실행 중인 시나리오의 k6 출력**을 읽으므로 테스트가 줄 수 없는 런타임
맥락이 필요하다 — 그래서 파라미터가 있는 형태만 검사한다. 도메인 값을 오타로 바꿔
실패하는 것을 확인했다.

---

## 11. 매니페스트가 러너에 없는 검사 id를 부른다 (F05-P)

주입 전에 죽었다(mutations 0건, DIRTY 아님):

```
[ERROR] Adaptive controller failed closed: unknown approved check ids: ['worker-cohort-placement']
```

러너의 승인 검사 목록에 그런 이름이 없다. 러너는 모르는 이름을 만나면 통과시키지 않고
그 자리에서 닫는다(fail closed) — 옳은 동작이다. #6과 같은 레포 간 불일치 계열이다.

## 12. 같은 파라미터가 두 레지스트리에서 다르다 (F15-T1)

```
profile control refused: parameters must exactly match an approved F15-T1 timeline
```

실행기는 매니페스트 파라미터를 **`registry/profiles.json`의 `scenario_parameters`**와
정확 비교한다. `food_fault`의 메모리가 어긋나 있었다:

| 출처 | 값 |
|---|---|
| `profiles.json` (승인) | `576Mi` |
| `controllers.json` → 매니페스트 | **`768Mi`** |

실행기 자신의 기본값도 576Mi다. 어느 쪽이 옳은지는 설계 판단이 필요하고(#2에 따르면
JVM 상대로는 576Mi도 OOM을 못 낸다), 자기 시나리오만 막으므로 스킵했다.

## 13. 정산 계좌가 말랐다 — 07-20 수정이 실행 DB에 닿은 적 없다 (F18-P)

F18-P가 `must_rule_out`의 `sync-path-also-broken`(`transfer_2xx_rate < 0.95`)으로 abort했다.
실측값이 20틱 내내 0.0~0.36이었다. cleanup·recovery는 성공했고 DIRTY도 아니었다.

transfer 앱은 **정상**이었다(Ready, 재시작 0, 주입 env 복원됨). 로그가 이유를 그대로 말한다:

```
Transfer FAILED (insufficient balance): from=commerce-settlement balance=1094.04 amount=108400.00
```

`BANKING.accounts`의 `commerce-settlement` 잔액이 **1094.04**, `updated_at`이 **00:00 UTC**
— 2시간 40분째 그 계좌에서 나가는 이체가 전부 실패하고 있었다.

### 왜 말랐나 — 고쳤다고 생각한 수정이 적용된 적이 없다

`core-banking/db/init.sql`의 주석은 이 문제를 이미 알고 있다:

> 정산 계좌는 두 상시 소비자(checkout당 이체 + 매시 정산 배치)가 출금만 하는 저수지다.
> 5천만이면 반나절에 고갈돼 이체가 전부 FAILED로 침묵 실패한다(2026-07-20 실증).

그래서 시드를 **1조**로 올렸다. 그런데 시딩이 MERGE이고 매칭 절이 이렇다:

```sql
WHEN MATCHED THEN UPDATE SET a.holder = src.holder
```

**`balance`는 갱신하지 않는다.** 계좌가 이미 존재하므로 올린 시드 값은 실행 중인 DB에
영영 반영되지 않았고, 5천만 시절 잔액이 그대로 다시 말랐다.

**복구:** 설계 시드 값으로 되돌렸다(`update ... set balance = 1000000000000.00
where id = 'commerce-settlement' and balance < 1000000000000.00`).
근본 수리는 재시딩 경로가 이 저수지 계좌의 잔액도 복원하게 만드는 것이다.

**F18-P의 감별자는 정당했다** — 동기 경로가 실제로 깨져 있었으니 그 시나리오는 자기 주장을
증명할 수 없는 상태였다. 계좌 복구 후 재시도했다.

## 14. core-banking 관측 평면은 처음부터 죽어 있었다

`/tmp/rca-baseline-core-banking-live.json`이 `business_2xx_rate 0.0`,
`entry_status null`, 모든 rate 0을 계속 발행한다. commerce·food-delivery는 정상(1.0/200)이다.

원인은 baseline 부하 스크립트다. 모니터는 `step` 태그로 표본을 분류한다:

```
loadgen_monitor.py --business-step transfer --read-step get
...
elif metric == "http_reqs" and tags.get("step") == self.business_step:
```

그런데 `/opt/loadgen/core-banking/script.js`에는 **`tags`가 한 줄도 없다**(`grep -c tags` = 0).
commerce 스크립트에는 있다. 요청은 나가지만 태그가 없어 영영 분류되지 않는다.
`achieved_rps`만 `iterations` 메트릭에서 나와 정상으로 보인다 — **살아 있는 것처럼 보이는
죽은 관측**이다.

`#10`의 회복 게이트 재지정은 commerce·food-delivery만 쓰므로 영향받지 않는다.
다만 **`domain: core-banking`을 쓰는 관측을 새로 만들면 0을 읽는다** — 스크립트에 태그를
넣기 전까지는 쓸 수 없다.

### 곁다리: tb-runner에 남은 좀비 모니터

`pgrep -af monitor`에 몇 시간 전 시나리오의 모니터가 살아 있다
(`rca-scenario-F08-P-monitor.py`, `F01-P`, `F08-G`). cleanup이 이들을 거두지 않는다.
당장 해롭진 않지만 누적된다.

## 15. 주입이 의도와 다른 장애를 만든다 (F18-P)

계좌를 복구(#13)한 뒤 재시도했는데도 같은 감별자로 abort했다. 이번엔 원인이 다르다.

이벤트가 그대로 말한다:

```
Container transfer-service failed liveness probe, will be restarted
Liveness probe failed: .../actuator/health: context deadline exceeded
Readiness probe failed: .../actuator/health: context deadline exceeded
```

주입은 `OUTBOX_RELAY_ENABLED=false`(k8s.env)다. 그런데 **릴레이를 끈 파드가
`/actuator/health`에 응답하지 못한다.** liveness가 죽이고, 배포가
**`replicas=1` · `maxSurge=0` · `maxUnavailable=1`**이라 그때마다 동기 경로가 통째로 끊긴다.

틱에 그대로 보인다 — `pod_ready`가 1분 주기로 false↔true, 그때마다
`entry_status` 502, `transfer_2xx_rate` 0.0. 계좌 복구 덕에 파드가 살아 있는 구간에서는
0.52까지 올라왔지만(복구 전엔 최대 0.36) 곧 다시 0으로 떨어진다.

성공 조건 `outbox_unpublished > 20`도 서지 못한다 — 0 → 13 → 0 → 4 → 0으로,
파드가 갈릴 때마다 릴레이가 다시 돌아 백로그를 비운다.

**시나리오가 의도한 장애는 "릴레이만 멈추고 동기 경로는 멀쩡"인데, 실제로 만들어지는 것은
"서비스가 통째로 불안정"이다.** 감별자 `sync-path-also-broken`은 정확히 그 차이를
잡으라고 있는 것이므로 **정당하게 거부한 것**이다.

두 갈래로 읽을 수 있고 어느 쪽이든 배치 중에 정할 일이 아니다:

- **앱 결함** — 릴레이를 끈다고 health가 타임아웃되는 것은 그 자체로 이상하다
- **시나리오 전제 오류** — 이 주입이 동기 경로에 무해하다는 가정이 틀렸다

`maxSurge=0`이라 어떤 env 주입이든 이 서비스에서는 완전 정지를 동반한다는 점도 함께 남긴다.

## 16. companion 부하가 판정 창보다 먼저 끝난다 (F19-P, 17종 잠재)

**#10의 수리는 여기서 확인됐다.** F19-P의 회복 게이트가 값을 읽었고
(`order_create_5xx_rate_baseline = 0.0`, 출처 `k6:baseline:food-delivery:business_5xx_rate`),
**recovery 성공 · DIRTY 없음**으로 끝났다. 고치기 전이라면 `recovery_timeout` → 전역 DIRTY였다.

그런데 F19-P는 다른 사유로 abort했다 — `safety_observation_unavailable`.

```
03:43:30  부하·주입 apply
03:55:21  entry_status / order_create_5xx_rate 가 unusable  <- live.json 사라짐
03:55:37~ safety_observation_pending  (4틱 대기)
03:56:45  cleanup 시작                                       <- 삭제보다 84초 늦다
03:56:46  abort: safety_observation_unavailable
```

원인은 **지속시간 불일치**다:

| | 값 |
|---|---|
| companion 부하 `load.north_south` | ramp_up 2m + hold 8m + ramp_down 1m = **11분** |
| 레벨 | settle 45s + min_hold 8m, **timeout 13분** |

부하가 11분에 끝나면서 `/tmp/rca-scenario-F19-P-live.json`이 사라지는데, 컨트롤러는
13분까지 판정을 계속할 수 있다. F19-P의 `must_rule_out`은 `entry_status`(시나리오 부하 출처)를
쓰므로 그 2분 공백에서 **안전 관측 상실**로 abort한다. 큐는 이 사유를 전이 사유로 보고
**두 번 자동 재시도한 뒤** 세 번째에 섰다 — 한 시나리오에 런 3개(약 40분)를 태웠다.

### 범위: timeout > 부하 길이 이면서 감별자가 시나리오 신호를 쓰는 17종

`F07-H F11-R F14-P F15-H F15-R F15-T2 F16-H F17-P F17-R F18-P F19-P F19-S
F20-Q F20-R F21-Q F23-R F25-H` (공백 +1분 ~ **+14분**, F15-R이 최악)

**다만 이것만으로 죽지는 않는다.** 성공하는 런은 min_hold(8분) 안에 끝나 부하가 살아 있는
동안 정리에 들어간다 — 실제로 F07-H(+1)·F11-R(+1)·F17-R(+2)는 이 배치에서 통과했다.
공백이 무는 것은 **성공하지 못해 timeout까지 가는 런**이고, 그때 하는 일이 나쁘다:

- 진짜 사유("성공 조건 미달")를 **엉뚱한 사유**("안전 관측 상실")로 덮는다
- 전이 사유로 분류돼 **자동 재시도 2회**를 태운다 → 실패 하나당 런 3개

즉 **실패를 만들지는 않지만 실패의 진단을 망가뜨리고 시간을 세 배로 쓴다.**

**F19-S에서 그대로 재현됐다**(2026-08-03): 첫 틱 04:38:28 → 신호 소실 04:49:45(11분 17초),
`safety_observation_unavailable`, 자동 재시도 2회 소진 후 정지. 회복 게이트는 이번에도
정상이었다(`order_create_5xx_rate_baseline` usable, cleanup·recovery 성공, DIRTY 없음) —
**#10의 수리가 두 번째로 확인됐다.**

부하 길이는 주입 파라미터라 스모크 패스 규칙상 배치 중에 건드리지 않는다.
수리 방향은 **부하가 판정 창보다 오래 살게 하는 것**(ramp_down 연장 또는 timeout 축소)이다.

## 17. 주입이 배포의 progress deadline을 넘기면 cleanup 검증이 오염된다 (F16-H)

F16-H가 `cleanup_failed after safety_observation_unavailable`로 **전역 DIRTY**가 됐다.
1차 사유는 #16(부하 공백)이고, DIRTY를 만든 것은 그 다음이다:

```
cleanup reason: k8s.probe:error: deployment "testbed-user" exceeded its progress deadline
```

**그런데 복원은 성공해 있었다.** 확인한 실물:

- 프로브가 이미 원값(`/actuator/health`)으로 되돌아와 있었다
- 파드는 부팅 중이었고 **68초 뒤 1/1 Running**이 됐다
- 그 뒤 정규 cleanup을 다시 부르니 **그대로 성공**했다

### 기제

실행기의 정리는 `rollout status --timeout=180s`로 기다린다(넉넉하다). 문제는 그 앞이다 —
주입한 프로브가 파드를 **10분 넘게 unready로 붙잡아** 배포의
`progressDeadlineSeconds: 600`이 만료됐고, Deployment에 `ProgressDeadlineExceeded`
조건이 **굳어버린다**. 복원 패치 뒤 `rollout status`는 기다리지 않고 그 굳은 조건을
즉시 되읽어 실패로 보고한다.

즉 **정리는 됐는데 정리를 확인하는 쪽이 과거 상태를 본다.** #10·#6과 같은 계열
("되돌아갔음을 확인하는 쪽이 망가진다")이며, 이번엔 대가가 전역 DIRTY다.

### 범위

`k8s.probe`를 쓰는 live 3종의 레벨 timeout이 전부 **13분 > 10분**이다:
**F05-H · F16-H · F17-R**.

**#16과 마찬가지로 성공하는 런은 걸리지 않는다** — min_hold(8분) 안에 끝나면 파드가
deadline 전에 복구된다. 실제로 F05-H와 F17-R은 이 배치에서 통과했다. 걸리는 것은
**timeout까지 가는 런**이고, 그때 clean한 abort가 **전역 DIRTY로 승격**돼 사람이 붙어야 한다.

수리 방향은 셋 중 하나다 — 레벨 timeout을 `progressDeadlineSeconds` 아래로,
배포의 deadline을 늘리기, 또는 정리 검증이 굳은 조건 대신 **파드 준비 상태를 직접** 보게 하기.
마지막이 가장 정확하다.

## 18. 성공 임계가 서비스가 견디는 지점 위에 있다 (F20-Q)

F20-Q는 별도 fault 프로파일이 없다 — **부하 자체가 주입**이다
(`slowquery.js`, 30 rps, unpaged slow query로 힙 압박).

성공 조건은 둘 다 만족해야 한다:

```
order_p95            >= 2000 ms
order_memory_current >= 805,306,368 (768Mi)
```

실측 진행:

```
+0s   entry_status 400  p95 641ms  memory 624MB  pod_ready true
+15s  entry_status   0  p95   0ms  memory 625MB  pod_ready false   <- 서비스가 죽었다
+34s  abort: abort_condition (entry-unreachable, 2틱 연속)
      이후 memory 295MB (새 JVM)
```

**메모리가 624MB일 때 서비스가 먼저 죽는다.** 성공에 필요한 768Mi에 닿기 전이다.
`abort`의 `entry-unreachable`(`entry_status == 0`)은 "특정 열화가 아니라 경로가 통째로
끊겼다"를 잡는 안전 정지이고, 여기서는 **정확히 제 역할을 했다**.

#2(F05-R 메모리)·#3(F08-P 타임아웃)과 같은 뿌리다 — **대상이 어디까지 버티는지 재지 않고
임계를 정했다.** 이번엔 방향이 반대일 뿐이다: 주입이 약해서 못 넘긴 게 아니라,
넘기기 전에 대상이 죽는다.

수리하려면 임계를 서비스가 살아 있는 구간으로 내리거나(예: p95만으로 판정),
힙이 아니라 지연만 밀어올리는 부하로 바꿔야 한다.

## 19. 파괴적 시나리오 뒤에는 게이트 창이 비어 있다 (F23-R)

F23-R이 주입 전에 `check_failed:baseline-business-success`로 막혔다. **#8과 달리 이번엔
게이트가 옳았다** — 절차대로 같은 질의를 직접 재보니 5분간 PAID가 **0 · 0 · 1건**이었다(임계 5).

원인은 바로 앞 시나리오다. F25-H는 commerce PostgreSQL을 OOMKill시키는 시나리오이고,
그 직후 상태가 이랬다:

```
testbed-postgres-0   1/1 Running   4m42s   <- 방금 재기동됐다
15분 기준 PAID       2093건                <- 경로 자체는 살아 있다
commerce baseline    business_2xx_rate 0.76, entry_status 200
```

DB가 재기동된 구간에는 주문이 기록되지 않으므로, 게이트가 보는 **직전 5분 창이 통째로 비었다.**
경로는 멀쩡한데 창이 비어 있는 것이다.

스모크 패스의 시나리오 간격은 5분이라 **창을 다시 채우기에 빠듯하다** — 파괴적 시나리오
뒤에는 거의 확정적으로 막힌다. 데이터셋 등급(30분)에서는 안 생긴다.

**대응은 스킵도 재시도도 아니고 기다림이다.** PAID/5분이 임계를 넘을 때까지 폴링한 뒤
resume했다(30건까지 회복 후 재개, F23-R 정상 시작).

`#8`과 이 건을 가르는 절차는 그대로다 — **막히면 먼저 같은 질의를 직접 재라.**
값이 멀쩡하면 프로브 결함(#8, resume), 값이 실제로 낮으면 환경 회복 대기(#19).

## 20. 승인 파라미터가 한 벌인데 사다리는 여러 단이다 (F03-H·F15-R)

#12(F15-T1)와 같은 계열이 두 건 더 나왔고, 이번엔 **전역 DIRTY**까지 갔다.

### F03-H — 사다리 3단, 승인은 1벌

```
profile control refused: parameters must exactly match a measured F03-H level
cleanup reason: load.east_west:refused: parameters must exactly match a measured F03-H level
```

`profiles.json`의 `load.east_west/F03-H`에는 파라미터가 **한 벌**(`target_rps: 30`)뿐인데
컨트롤러 사다리는 세 단이다:

| level | target_rps | 승인과 일치 |
|---|---|---|
| below-saturation-30rps | 30 | ✅ (완전 일치) |
| at-saturation-45rps | 45 | ❌ |
| past-saturation-60rps | 60 | ❌ |

**1·2단은 영영 적용될 수 없다.** 더 나쁜 것은 정리도 같은 검증을 통과해야 한다는 점이다 —
그래서 주입 입구와 청소 출구가 동시에 막히고(#5와 같은 교착 모양) 전역 DIRTY가 된다.

### F15-R — 주입 실패 뒤 회복이 주입 산출물을 기다린다

같은 거부(`must exactly match an approved F15-R timeline`)로 주입이 무산됐는데,
회복 조건 셋이 **주입이 만들어내는 상태**를 본다:

```
mock_flap_fault_active  eq False    <- business_probe (scenario_id=F15-R)
mock_flap_episode       gte 2       <- 주입이 두 번 flap해야 만족한다
order_duplicate_count   eq 0        <- database (scenario_id=F15-R)
```

주입이 없었으니 그 상태 파일도 없고, 세 신호 모두 `error/fresh`로 읽히지 않는다.
**주입이 실패하면 회복은 반드시 10분을 태우고 DIRTY로 끝난다.**
`mock_flap_episode >= 2`는 애초에 "정상으로 돌아왔는가"가 아니라 성공 조건에 가깝다.

회복 게이트가 `scenario_id` 파라미터(주입 산출물)에 의존하는 live 시나리오는 넷이다 —
**F02-H · F10-H · F10-P**(`disk_io_util < 20`)와 **F15-R**. 앞의 셋은 주입만 성공하면
정상 동작하지만, 주입이 거부되면 같은 길을 간다.

### 환경은 깨끗했다 — 그리고 정규 경로로는 못 씻는다

F03-H는 실물 확인 결과 잔재가 전혀 없었다(k6 job/configmap 없음, 시나리오 k6 프로세스 없음,
`/tmp/rca-scenario-*-live.json` 없음, commerce 파드 전부 정상). 그런데 정리가 **구조적으로**
거부되므로 `cleanup`도 `repair_capsule`도 통하지 않는다(캡슐을 다시 잘라도 같은 레지스트리를 본다).

그래서 **직접 확인한 근거로 `coordinator.json`의 dirty_run을 수동 해제**했다
(백업 `coordinator.json.bak-f03h-dirty-*`). 기계 검증이 원리적으로 도달할 수 없는 경우의
유일한 탈출구다 — 다만 이건 마지막 수단이고, 먼저 잔재를 전수 확인해야 한다.

## 21. 같은 계약 안의 두 목록이 갈라졌다 (live 7종) — 수리함

F06-P가 주입 전에 죽고 전역 DIRTY가 됐다:

```
profile control refused: scenario_tag is not allowlisted
cleanup reason: load.north_south:refused: scenario_tag is not allowlisted
```

`load.north_south`의 `parameter_contract`는 시나리오를 **두 번** 검사한다:

| 검사 | 내용 |
|---|---|
| `allowed_scenarios` | 목록에 있는가 |
| `tag_pattern` | `^scenario_id=F(07-H\|01-R\|…)$` 정규식에 맞는가 |

승격할 때 **앞의 목록만 갱신되고 정규식이 따라오지 않았다.** 갈라진 결과:

**F06-P · F02-H · F10-H · F10-P · F15-H · F15-T2 · F14-P** — 일곱 모두
`allowed_scenarios`에는 있으나 `tag_pattern`이 거부한다. 그리고 **일곱 전부가 큐에 남아 있었다**
(남은 14종의 절반). 정리도 같은 검증을 거치므로 매번 전역 DIRTY가 된다.

**수리:** 누락된 일곱을 `tag_pattern`에 추가했다(`registry/profiles.json`).
재시도한 F06-P는 **주입이 정상 적용**됐다(`load.north_south`·`mock.expectation` 둘 다 완료).

### 가드

`test_every_allowlisted_scenario_also_matches_its_tag_pattern` 신설 —
`tag_pattern`을 가진 모든 프로파일에 대해 `allowed_scenarios` 전원이 그 패턴을 통과하는지 본다.
F06-P를 패턴에서 빼 실패하는 것을 확인했다.

### `repair_capsule`이 조용히 아무 일도 안 했다

레지스트리를 고친 뒤 `cleanup?repair_capsule=true`를 불렀는데 **여전히 거부**됐고,
`capsule-repair.json`이 **생성되지 않았으며** 오류 로그도 남지 않았다(HTTP 200).
캡슐 안 `registry/profiles.json`은 옛 패턴 그대로였다.

#5에서는 같은 스위치가 동작했으므로 조건부로 실패하는 것이다 — 아마 재컴파일된 plan의
`registry_digest`가 "움직여도 되는 필드"(`executor_sha256`·`plan_digest`) 밖이라
수리가 거부되고, 그 예외가 삼켜지는 것으로 보인다. **탈출구가 조용히 없어지는 것은
그 자체로 결함이다.** 결국 잔재를 전수 확인한 뒤 `coordinator.json`을 수동 해제했다.

**교훈: 레지스트리를 고쳐도 이미 실패한 런은 못 살린다 — 새 런만 새 캡슐을 받는다.**
그래서 F06-P는 스킵이 아니라 **재시도**가 정답이었다.

## 배치 후 수리 목록

| 대상 | 내용 | 레포 |
|---|---|---|
| F05-R | 레버 교체 — `-Xmx`를 새 limit보다 높게 고정 후 limit 하향 | testbed |
| F08-P | read-timeout을 ~20ms 이하로, 또는 payment 동시 지연 | testbed |
| F12-H·F09-P | 사다리를 requests 위로 올리거나 실행기가 requests도 낮추도록 | testbed / runner |
| F09-R·F15-P | 노드 CPU 관측 교체 + 사다리 0단과 감별자 바닥의 겹침 해소 | testbed / runner |
| F05-P·F15-P | `kcm.node.mem_utilization` 신뢰도 실측 (큐 정지 중에만) | — |
| F01-P | `ORACLE_REMOTE` apply를 인자 전달 방식으로 재작성 + pid 경로 통일 | testbed |
| F05-P | `worker-cohort-placement` 검사를 러너에 구현하거나 매니페스트에서 제거 | runner / testbed |
| F15-T1 | `food_fault` 메모리를 두 레지스트리 중 어느 쪽으로 통일할지 결정 | testbed |
| testbed | `registry/queries.json`의 loadgen 항목에도 `allowed_parameters: ["domain"]` 반영(라이브엔 무해하나 두 사본이 갈라져 있다) | testbed |
| 러너 | `_safe_bool` 예외 사유 보존, `check_failed:`를 1회 자동 재시도 대상으로 | runner |
| 러너 | **리스 만료 근본** — 주입 서브프로세스가 이벤트 루프를 막아 하트비트가 못 돈다. 만료되면 cleanup까지 거부돼 DIRTY가 된다(07-31 미해결, #9에서 재현) | runner |
| 러너 | 실행기 계약 테스트가 실제 출력 형태를 흉내내도록 (아래) | runner |
| **core-banking** | baseline `script.js`에 `step: transfer` / `step: get` 태그 추가 — 없으면 이 도메인 관측은 전부 0 | testbed |
| **시딩** | `init.sql` MERGE가 정산 계좌 잔액도 복원하도록 (지금은 holder만 갱신해 상향된 시드가 영영 반영 안 됨) | testbed |
| tb-runner | 시나리오 cleanup이 모니터 프로세스를 거두도록 | runner |
| F18-P | 릴레이 비활성 시 health가 멈추는 원인 규명(앱 결함인지) + `maxSurge=0`이라 env 주입이 항상 완전 정지를 동반하는 점 재설계 | testbed |
| **17종** | companion 부하 길이 ≥ 레벨 timeout 이 되도록 정렬 — 안 하면 실패 진단이 `safety_observation_unavailable`로 덮이고 재시도 2회를 태운다 | testbed |
| F05-H·F16-H·F17-R | `k8s.probe` 정리 검증이 `rollout status`(굳은 조건) 대신 파드 준비 상태를 직접 보도록 — 또는 timeout을 progressDeadline 아래로 | testbed |
| F20-Q | 성공 임계를 서비스 생존 구간 안으로 (624MB에서 죽는데 768Mi를 요구한다) | testbed |
| 러너/큐 | 파괴적 시나리오 뒤 간격을 게이트 창(5분)보다 길게, 또는 게이트가 회복을 기다리게 | runner |
| F03-H | `profiles.json`에 사다리 각 단(30/45/60)의 승인 파라미터를 모두 등록 | testbed |
| F15-R | 회복 조건에서 주입 산출물 의존을 걷어내기 (`mock_flap_episode >= 2`는 회복 조건이 아니라 성공 조건이다) | testbed |
| 러너 | 주입이 적용되지 않은 런은 회복 게이트를 건너뛰도록 — 지금은 실패한 주입이 반드시 10분 뒤 DIRTY가 된다 | runner |
| 러너 | `repair_capsule`이 실패할 때 사유를 남기도록 — 지금은 HTTP 200에 아무 흔적 없이 무동작(#21) | runner |

## 반복되는 메타 패턴

**테스트가 전부 초록인데 결함이 라이브에서 터진다.** 이번 배치에서 세 번 반복됐다:

- #5 — testbed 141개 테스트 통과. sqlplus 출력 형태를 흉내내는 가짜가 없다
- #6 — 가드가 존재했으나 `database` 어댑터를 커버리지에서 뺐다
- #7 — 원격 셸 인용을 검증하는 테스트가 없다. pid 파일 존재만으로 성공 판정
- #9 — 가드가 형제 실행기에 복제되지 않았다. 결함도 수정도 한쪽에만 있었다

공통 처방은 하나다: **가짜를 실물 출력 형태로 흉내내면 기존 테스트가 그대로 가드가 된다.**
그리고 가드는 **수정을 되돌려 실패를 봐야** 가드다(#6에서 실제로 되돌려 확인했다).

## 실행 상태 (2026-08-03 10:00 KST 기준)

- 완주 11: F01-R · F01-H · F06-R · F07-H · F08-H · F11-R · F04-R · F05-H · F08-G · F15-G · F06-H
- 스킵 5: F12-H · F05-R · F08-P · F09-R · F01-P (+ F15-P · F09-P 예약)
- 남은 28종
- 스킵은 상태 파일의 `next_index`를 넘기고 `skipped_scenario_ids`에 기록하는 방식이며
  (사전검사 3회 실패 시 큐가 하는 것과 동일), 매번 `state/live-queue.json.bak-before-skip-*` 백업

### 실패가 확정된 시나리오는 미리 넘긴다

**F15-P**(#4의 눈먼 노드 CPU가 성공조건)와 **F09-P**(#1의 requests 벽)는 결과가 이미 정해져
있어 도는 것 자체가 낭비다. 그런데 큐는 `scenario_ids[next_index]`로만 다음 일을 고르고
`skipped_scenario_ids`는 **선택에 쓰이지 않는 기록일 뿐**이며, `resume()`은 얼린 목록의 변경을
거부한다 — 즉 **미리 빼둘 수단이 없고, 도달하는 순간 넘기는 수밖에 없다.**

그래서 `preskip.py`(러너 홈, `logs/preskip.log`)를 띄워 둔다. 워커는 5초마다 돌며 clean
window가 지나는 즉시 다음 시나리오를 시작하므로, 이 스크립트는 **`clean_window_not_before`가
90초 이상 남았을 때(워커가 시작할 수 없는 구간) 또는 paused일 때만** 상태를 쓰고, 쓴 뒤
반드시 되읽어 확인한다. 이 안전 조건은 되돌려 실패를 확인한 테스트로 고정돼 있다.

### 미커밋 변경

- testbed: `db_lock_executor.py` · `db_table_readonly_executor.py` ·
  `timeline_multi_injection_executor.py` · `tests/test_timeline_multi_injection_executor.py`
- runner: `live_probes.py` · `tests/test_external_live_manifests.py` ·
  `tests/test_db_host_live_probes.py`
