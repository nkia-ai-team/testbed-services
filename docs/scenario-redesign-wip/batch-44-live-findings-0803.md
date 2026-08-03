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
아래 9건 중 **정적 감사로 잡을 수 있었던 것은 없다**. 매니페스트·계약·파라미터는
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

---

## 배치 후 수리 목록

| 대상 | 내용 | 레포 |
|---|---|---|
| F05-R | 레버 교체 — `-Xmx`를 새 limit보다 높게 고정 후 limit 하향 | testbed |
| F08-P | read-timeout을 ~20ms 이하로, 또는 payment 동시 지연 | testbed |
| F12-H·F09-P | 사다리를 requests 위로 올리거나 실행기가 requests도 낮추도록 | testbed / runner |
| F09-R·F15-P | 노드 CPU 관측 교체 + 사다리 0단과 감별자 바닥의 겹침 해소 | testbed / runner |
| F05-P·F15-P | `kcm.node.mem_utilization` 신뢰도 실측 (큐 정지 중에만) | — |
| F01-P | `ORACLE_REMOTE` apply를 인자 전달 방식으로 재작성 + pid 경로 통일 | testbed |
| 러너 | `_safe_bool` 예외 사유 보존, `check_failed:`를 1회 자동 재시도 대상으로 | runner |
| 러너 | **리스 만료 근본** — 주입 서브프로세스가 이벤트 루프를 막아 하트비트가 못 돈다. 만료되면 cleanup까지 거부돼 DIRTY가 된다(07-31 미해결, #9에서 재현) | runner |
| 러너 | 실행기 계약 테스트가 실제 출력 형태를 흉내내도록 (아래) | runner |

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
