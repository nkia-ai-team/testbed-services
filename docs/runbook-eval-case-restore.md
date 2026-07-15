---
title: 평가 케이스 복원 runbook (소비자용)
status: Draft
owner: project
last_reviewed: 2026-07-14
tags:
  - evaluation
  - testbed
  - data
  - runbook
summary: /data/eval-cases/의 케이스 파일을 받아 격리된 일회용 DB 세트(VM/ClickHouse/PostgreSQL)에 복원하고, 평가 후 폐기하는 절차. 평가 담당자(소비자)용 가이드.
---

# 평가 케이스 복원 runbook (소비자용)

[평가용 데이터 캡처 설계](spec-eval-data-capture.md)가 산출한 케이스 파일을
받아 **격리된 일회용 DB에 복원**해 평가 입력을 만드는 절차다. 복원 이후
(어떤 모듈을 어떻게 돌려 채점할지)는 각 평가 담당자 몫이며, 이 문서는 모든
소비자가 공통으로 밟는 "파일 → 조회 가능한 DB"까지만 다룬다.

## 0. 전제

- 케이스 디렉터리: `/data/eval-cases/case-NN-slug/` — **읽기 전용으로만 접근**
  (케이스는 불변, 수정 금지).
- docker + docker compose 사용 가능.
- `lucida-next` 레포 체크아웃 (ClickHouse 스키마 DDL이 필요:
  `lucida-next/database/ddl/clickhouse/`).
- 케이스 파일 구성(계약은 [캡처 설계 §4](spec-eval-data-capture.md) 정본):

```
case-NN-slug/
  data/
    victoriametrics.export   # VM JSON lines export
    clickhouse/              # 테이블별 Parquet (파일명 = 테이블명)
      otel_traces_local.parquet
      lucida_logs_local.parquet
      lucida_events_local.parquet
      host_connections.parquet
      ...
    postgres.dump            # pg_dump custom format (-Fc, 스키마 포함)
  golden.rca.json
  golden.anomaly.json
  meta.json                  # 시간창 [t0,t1]·장애 시각·리드인
```

## 1. 격리 DB 세트 기동

⚠ **lucida-next의 docker-compose.yml을 재사용하지 말 것.** 전체 스택을 올리면
ingest·워커들이 같이 떠서 복원본에 새 데이터를 되써 평가 입력이 오염된다.
아래처럼 **DB 3개만** 있는 전용 compose를 쓴다. 이미지 버전은 운영과 동일하게
맞춘다(2026-07-14 기준 lucida-next compose 실값).

```yaml
# eval-dbs.compose.yml
services:
  victoriametrics:
    image: victoriametrics/victoria-metrics:v1.97.1
    command: ["-retentionPeriod=100y"]   # ⚠ 필수 — 아래 함정 참조
    ports: ["18428:8428"]
  clickhouse:
    image: clickhouse/clickhouse-server:24.3
    environment:
      CLICKHOUSE_DB: lucida
      CLICKHOUSE_USER: lucida
      CLICKHOUSE_PASSWORD: lucida123
    ports: ["18123:8123", "19000:9000"]
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: lucida
      POSTGRES_USER: lucida
      POSTGRES_PASSWORD: lucida123
    ports: ["15432:5432"]
```

```bash
CASE=/data/eval-cases/case-01-inventory-lock
docker compose -f eval-dbs.compose.yml -p eval-case01 up -d
```

- `-p eval-case01` 프로젝트명으로 케이스별 격리. 포트(18428/18123/19000/15432)는
  같은 호스트의 라이브 lucida와 충돌하지 않게 비표준을 쓴다.
- 케이스를 바꿀 때는 §5로 폐기 후 새로 올린다(볼륨 재사용 금지).

**⚠ VM retention 함정.** VictoriaMetrics 기본 보존기간(1개월)보다 오래된
타임스탬프는 import 시 **조용히 버려진다.** 캡처 시점이 과거인 케이스가
대부분이므로 `-retentionPeriod=100y`를 반드시 지정한다.

## 2. 스키마 준비

| 저장소 | 스키마 | 방법 |
|---|---|---|
| VM | 불필요 | 스키마 없는 시계열 저장소 |
| PostgreSQL | 불필요 | `postgres.dump`(-Fc)에 스키마 포함 → 복원이 곧 스키마 |
| ClickHouse | **선적용 필요** | Parquet에는 스키마가 없다 → lucida DDL 적용 |

ClickHouse DDL 적용 (파일명 순서대로):

```bash
for f in ../lucida-next/database/ddl/clickhouse/*.sql; do
  docker compose -p eval-case01 exec -T clickhouse \
    clickhouse-client -u lucida --password lucida123 --multiquery < "$f"
done
```

케이스의 `data/clickhouse/`에 있는 테이블만 필요하면 해당 DDL만 골라 적용해도
된다(예: `001_init.sql` + `008_lucida_events.sql` + `008_host_connections.sql`).

## 3. 복원

### 3.1 VictoriaMetrics (메트릭)

```bash
curl -sS -X POST "http://localhost:18428/api/v1/import" \
  -T "$CASE/data/victoriametrics.export"
```

### 3.2 ClickHouse (트레이스·로그·이벤트·host_connections)

파일명 = 테이블명 규약이므로 그대로 돌린다:

```bash
for f in "$CASE"/data/clickhouse/*.parquet; do
  t=$(basename "$f" .parquet)
  docker compose -p eval-case01 exec -T clickhouse \
    clickhouse-client -u lucida --password lucida123 \
    --query "INSERT INTO lucida.${t} FORMAT Parquet" < "$f"
done
```

### 3.3 PostgreSQL (클러스터·인시던트·targets·정책·토폴로지)

```bash
docker compose -p eval-case01 exec -T postgres \
  pg_restore -U lucida -d lucida --no-owner --if-exists --clean < "$CASE/data/postgres.dump"
```

## 4. 복원 검증

`meta.json`의 시간창 `[t0,t1]`과 대조한다. 세 가지 모두 통과해야 복원 성공.

```bash
# VM — 시간창 안에 시계열이 있는가
curl -sS "http://localhost:18428/api/v1/query_range" \
  --data-urlencode 'query=count({__name__!=""})' \
  --data-urlencode "start=<t0>" --data-urlencode "end=<t1>" \
  --data-urlencode 'step=5m' | head -c 400

# CH — 테이블별 행수와 시간범위
docker compose -p eval-case01 exec clickhouse \
  clickhouse-client -u lucida --password lucida123 --query \
  "SELECT count(), min(timestamp), max(timestamp) FROM lucida.lucida_events_local"

# PG — 인시던트·targets 존재
docker compose -p eval-case01 exec postgres \
  psql -U lucida -d lucida -c \
  "SELECT (SELECT count(*) FROM incidents), (SELECT count(*) FROM targets);"
```

- 시간 데이터의 min/max가 시간창을 벗어나면 캡처 슬라이스 오류,
  행수 0이면 복원 실패(특히 VM은 retention 함정 §1 의심).
- `targets`는 시간창과 무관한 전체 스냅샷이므로 0이면 안 된다.

## 5. 평가 실행 시 주의 (모듈을 돌리는 소비자만)

복원된 DB를 조회만 하는 평가(RCA·챗봇 등 배치형)는 모듈이 바라보는 DB
접속 env만 격리 세트(위 포트)로 바꾸면 된다. **실워커를 돌리는 평가**는
[캡처 설계 §8 "재생 시 알려진 함정"](spec-eval-data-capture.md)을 반드시
읽어라. 요약:

- `ai-trainer`·**forecast 워커 미기동**, `STREAM_ANOMALY_TRAINER_RELOAD_SEC=0`,
  `STREAM_ANOMALY_MODEL_RELOAD_SEC=0`, 시드/백필 off.
- 과거 타임스탬프는 벽시계 가드(신선도·닫기·보류)에 걸린다 — 실워커 재생은
  타임시프트가 필요하다.
- 격리 DB에 워커가 되쓰기하는 것은 무방하다(일회용). 원본 케이스 파일만
  불변이면 된다.

## 6. 폐기

```bash
docker compose -f eval-dbs.compose.yml -p eval-case01 down -v   # -v 필수(볼륨 삭제)
```

같은 케이스를 다시 평가할 때도 **폐기 후 재복원**이 원칙이다 — 평가 중
워커가 되쓴 데이터가 남은 볼륨을 재사용하면 두 번째 실행부터 입력이 달라진다.

## 알려진 함정 요약

| 함정 | 증상 | 대응 |
|---|---|---|
| VM retentionPeriod | 과거 데이터가 import 후 조회 안 됨(무증상 유실) | `-retentionPeriod=100y` (§1) |
| lucida-next compose 재사용 | 워커가 복원본에 되쓰기 → 입력 오염 | DB 3개만 있는 전용 compose (§1) |
| CH 스키마 부재 | Parquet INSERT 실패 | DDL 선적용 (§2) |
| 볼륨 재사용 | 2회차 평가부터 결과 달라짐 | `down -v` 후 재복원 (§6) |
| 벽시계 가드 | 실워커가 과거 이벤트를 stale로 버림 | 캡처 설계 §8 참조 |
