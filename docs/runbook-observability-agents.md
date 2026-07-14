---
title: 관측 에이전트 설치 runbook (SMS·APM·DPM·KCM)
status: Active
owner: project
last_reviewed: 2026-07-14
tags:
  - runbook
  - testbed
  - observability
  - lucida-next
summary: 109 테스트베드에 lucida-next(AP 118) 관측 4계층을 붙이는 실측 절차. 계층별 등록 방식, 실값, 함정, 검증 방법 포함.
---

# 관측 에이전트 설치 runbook (SMS·APM·DPM·KCM)

> 2026-07-13 실설치로 검증된 절차다. 테스트베드 배포 자체는
> `runbook-testbed-deploy.md`, 인프라 상세는 `spec-testbed-design.md` §5 참조.

## 0. 공통 실값

| 항목 | 값 |
|---|---|
| AP 서버 (lucida-next) | `192.168.230.118` — 웹 UI `:13000`, API `:18080`, OTLP gRPC `:14317`, KCM gRPC `:7575`, VictoriaMetrics `:18428` |
| lucida 로그인 | `manager / !nkia1234` (`POST /api/v1/login` → `lucida_session` 쿠키) |
| 워커 브리지 IP | w1=`200.136`(commerce) w2=`200.137`(food) w3=`200.138`(banking) |
| VM ssh | 109 경유 `ssh -i /root/.ssh/tb_key nkia@192.168.122.<NAT-ip>` |
| 메트릭 저장소 | VictoriaMetrics 단일(vmsingle). 검증은 `118:18428/api/v1/query` 직접 질의가 가장 확실 |

계층별 등록은 전부 **API 방식**이 재현성이 좋다(웹 UI는 SMS만 사용했음).
등록의 공통 골격: `POST /api/v1/targets`(type별) + 필요 시
`POST /api/v1/targets/{id}/collectors`(kind별).

## 1. SMS — 호스트 메트릭 (워커 3대)

- **방식**: 웹 UI 관제추가 > Polestar SMS > SSH 원격설치.
- **등록폼 실값**: IP=**200.x 브리지**(NAT 122.x는 AP에서 도달 불가) / OS Linux /
  포트 22 / 계정 `nkia` / 비번 `NKIA1234`.
- **함정**: Ubuntu cloud-image의 `60-cloudimg-settings.conf`가
  `PasswordAuthentication no`로 막는다 → 각 VM에
  `/etc/ssh/sshd_config.d/99-testbed-pwauth.conf`(yes) 드롭인 추가 후 sshd reload.
- 에이전트 실체: `~/.lucida-agent/bin/lucida-agent` 프로세스(systemd 아님).
- **검증**: 웹 UI 서버 목록에 호스트 메트릭 유입 확인.

## 2. APM — 앱 trace/JVM (Spring 19개)

- **핵심 사실**: 이 lucida 빌드에는 Polestar Java 전용 agent jar가 없다
  (SMS Go 바이너리만 존재). Polestar APM(Java)의 실체는 OTLP 재사용이므로
  **OTel javaagent가 `service.name`을 달아 AP `118:14317`로 직접 전송**하면
  polestar_apm 자산에 붙는다.
- **manifest hook** (각 앱 Deployment): apm-agent hostPath 볼륨(`/opt/polestar10/apm`)
  + volumeMount(`/opt/apm`) + env(`JAVA_TOOL_OPTIONS=-javaagent:...`,
  `OTEL_SERVICE_NAME`, `OTEL_EXPORTER_OTLP_ENDPOINT=http://192.168.230.118:14317`).
  커밋 `549a312`(gateway) + `8a1fa3f`(나머지 18개).
- **주의**: 노드 로컬 OTel 에이전트(hostIP:4317) 경유는 **불가** — 호스트 에이전트는
  hostmetrics 전용이라 OTLP receiver가 없다. AP로 직접 보낸다.
- **자산 등록 (API 일괄)**: `POST /targets`(type=application,
  meta.collector=polestar-java, service_name) +
  `POST /targets/{id}/collectors`(kind=polestar_apm, config.service_name).
  매칭키는 **service.name**(SMS는 target_id UUID).
- **검증**: 19/19 트레이스 유입(RPS·p50/p95/p99).

## 3. DPM — DB 세션/lock/slowSQL (PG·MySQL·Oracle)

에이전트리스 **POLLING**: AP의 collector-dpm이 DB NodePort로 직접 TCP 접속한다.

### 3-1. 선행 조건 3개

1. **비대칭 라우팅 해결 (필수)** — AP(230.118)의 SYN은 VM 브리지 NIC(enp6s0)로
   들어오는데 응답이 기본경로(NAT enp1s0→122.1)로 나가 핸드셰이크가 안 끝난다.
   워커 3대에 라우트 추가 + netplan 영속화:
   ```bash
   sudo ip route replace 192.168.230.0/24 via 192.168.200.1 dev enp6s0
   # /etc/netplan/99-testbed-230-route.yaml 로 영속화 (chmod 600)
   ```
   109에서의 접속 성공은 이 문제를 못 잡는다(200.x 온링크라 대칭). 반드시
   collector의 `last_collect_status`로 판정할 것.
2. **PG preload** — `pg_stat_statements`는 서버 시작 시 로드 필요.
   `commerce/k8s/10-postgres.yaml`의 args로 주입(커밋 `62d05d7`) + 재시작.
3. **모니터링 계정** — 3개 DB에 `lucida_mon / lucida_mon1234` 생성:
   - PG: `CREATE EXTENSION pg_stat_statements; GRANT pg_monitor TO lucida_mon;`
   - MySQL: `PROCESS, REPLICATION CLIENT ON *.*` + `SELECT ON performance_schema.*`
     + `SELECT, EXECUTE ON sys.*` + **`SELECT ON fooddelivery.*`**
     (마지막이 없으면 접속은 되고 수집만 Error 1044로 실패)
   - Oracle(FREEPDB1): `CREATE SESSION, SELECT_CATALOG_ROLE, SELECT ANY DICTIONARY`
   - ⚠ 계정은 init.sql에 미반영 — DB 볼륨 리셋 시 재생성 필요.

### 3-2. 등록 (API)

target + collector 2단 호출. 접속 실값:

| DB | engine | host:port | database |
|---|---|---|---|
| PostgreSQL-commerce | postgresql | 200.136:30432 | commerce |
| MySQL-fooddelivery | mysql | 200.137:30236 | fooddelivery |
| Oracle-corebanking | oracle | 200.138:30308 | FREEPDB1 (=서비스명) |

```bash
# ① POST /api/v1/targets
{"type":"database","name":"<이름>","address":"<host>","discovery":"manual",
 "meta":{"collector":"polestar-dpm","engine":"<engine>","db_system":"<engine>",
         "registration_method":"polestar-dpm-polling"},
 "labels":{"domain":"database","registration_method":"polestar-dpm"}}
# ② POST /api/v1/targets/{id}/collectors
{"kind":"polestar_dpm","enabled":true,
 "config":{"engine":"<engine>","host":"<host>","port":<port>,"database":"<db>",
           "ssl_mode":"prefer","collect_session_history":true,
           "collect_top_sql":true,"top_sql_count":20,
           "collection_policy_source":"common_policy"},
 "config_encrypted":{"username":"lucida_mon","password":"lucida_mon1234"}}
```

Oracle은 `database` 필드가 곧 서비스명이다(`service_name` 키 없음).
스키마 정본: lucida-next `collector-dpm/dbpoll/poll.go` + `DatabaseDpmPollingDrawer.tsx`.

### 3-3. 검증 (함정 있음)

- **1차**: collector의 `last_collect_status == "success"`
  (`GET /targets/{id}/collectors`) — collector-dpm이 폴마다 기록.
- **2차**: `GET /api/v1/databases/{id}/dpm/session/status` → `found:true` + 실세션수.
- ⚠ `GET /databases/{id}/sessions`(시계열)는 **레거시 메트릭명**을 조회해서
  DPM 수집이 정상이어도 0을 반환한다. collector-dpm의 발행 이름은
  `dpm.<engine>.session.*`.

## 4. KCM — k8s pod/node (kubeadm 클러스터)

에이전트 방식: 클러스터 안에 master Deployment 1 + node DaemonSet(전 노드,
toleration Exists). master가 AP `118:7575`(gRPC)로 push.

### 4-1. 핵심 사실: arm64 크로스 빌드 필요

기존 산출물(레지스트리 `200.78:5000`의 kcm-agent 전 태그, 오프라인 패키지
`/usr/nkia/kcm/KCM Agent 10.2.1 Offline.tar`)은 **전부 amd64**. 테스트베드 VM은
aarch64라 소스 빌드가 필요하다.

- 소스: `https://cims2.nkia.net:8443/gitlab/lucida-kcmagent.git` (Go 1.25)
- go-dcgm(GPU)이 빌드 태그 없이 항상 포함 → **CGO 필수**, CGO_ENABLED=0 불가:
  ```bash
  docker run --rm -v "$PWD":/app -w /app golang:1.25 bash -c "
    apt-get update -qq && apt-get install -y -qq gcc-aarch64-linux-gnu
    GOOS=linux GOARCH=arm64 CGO_ENABLED=1 CC=aarch64-linux-gnu-gcc \
      go build -o bin/kcm-agent-arm64 main.go"
  ```
- 이미지 조립: 빌드 호스트에 qemu binfmt가 없으면 RUN 불가 → **COPY-only**
  Dockerfile로 충분(kubectl·mkdir 생략 무방 — 마운트 포인트는 kubelet이 생성):
  ```dockerfile
  FROM debian:bookworm-slim
  COPY bin/kcm-agent-arm64 /bin/kcm-agent
  ENTRYPOINT ["/bin/kcm-agent"]
  ```
  `docker buildx build --platform linux/arm64 --load` → `docker save` →
  **4개 노드 전부** `sudo ctr -n k8s.io images import`(DaemonSet이 cp에도 뜬다).

### 4-2. 설치 (helm)

chart는 소스 레포 `deploy/kcm-agent`. tb-cp에 helm arm64 설치 후:

```bash
helm install kcm-agent ~/kcm/kcm-agent -n kcm-monitoring --create-namespace \
  --set clusterName=rca-testbed \
  --set kcm.addr=192.168.230.118:7575 \
  --set kcm.orgId=<org-id> --set kcm.token=<token> \
  --set kcm.insecure=true \
  --set image=polestar.io/kcm-agent:10.2.4-arm64-testbed \
  --set pullPolicy=IfNotPresent
```

org_id/token은 사전 등록으로 발급받는다:
`POST /targets`(type=kubernetes, name=클러스터명, meta.collector=polestar-agent)
→ 응답에 `kcm:{org_id, token}` 1회 노출.

### 4-3. 함정 (서버 빌드에 따라 다름 — 2026-07-14 갱신)

1. **토큰은 필수이며 target 삭제 시 즉시 수집이 끊긴다.**
   2026-07-13 이전 빌드는 무토큰 자동등록(`KCM_TRUST_UNTOKENED=true`)을
   허용했으나, 07-11 커밋으로 **폐지**(fail-closed)됐고 118은 07-13 저녁
   업데이트로 이를 반영했다. 에이전트는 토큰을 `Authorization` 헤더로 보내며
   새 빌드는 이를 `kcm-token`의 폴백으로 수용한다.
   - 토큰 분실/무효 시: `POST /targets/{id}/kcm-token`
     (body `{"current_password":"<로그인 비번>"}`)로 재발급 → helm upgrade
     `--set kcm.orgId=<target-id> --set kcm.token=<새 토큰>` + rollout restart.
   - 증상: master 로그에 `Unauthenticated: invalid kcm agent credentials`,
     VM에서 kcm.* 샘플 중단.
   - (2026-07-13 실제 사고: 토큰이 묶인 사전등록 target을 중복 정리로 삭제
     → 서버 업데이트 시점부터 수집 중단. 자동등록 target
     `cluster-18c410c8-…`(display `rca-testbed`)에 토큰 재발급으로 복구.)
2. **`OperatorService Unimplemented` 에러 도배**(구 빌드) — AP측 명령 스트림
   stub. 수집과 무관. 새 빌드에서는 구현됨.
3. **`GET /targets` 기본 목록에 kubernetes type이 안 보인다** —
   `?type=kubernetes` 필터로 조회할 것.

### 4-4. 검증

```bash
# pod: master 1 + node agent 4 전부 Running
kubectl -n kcm-monitoring get pods -o wide
# 메트릭: VM 직접 질의 (target_id는 ?type=kubernetes 로 확인)
curl -G 'http://192.168.230.118:18428/api/v1/query' \
  --data-urlencode 'query={__name__="kcm.node.cpu_usage",target_id="<id>"}'
```

2026-07-13 실측: 노드 4 · 파드 53, `kcm.*` 7,200+ 시리즈 유입.

## 5. 수집 상태 점검 (헬스체크 — 작업 시작 전 권장)

118은 수시로 업데이트되는 개발 스택이라 관측이 조용히 끊길 수 있다
(2026-07-14 KCM 사례: 서버측 인증 정책 변경으로 하룻밤 새 중단).
알람 튜닝·시나리오 실행 등 관측에 의존하는 작업 전에 4계층을 훑는다.

```bash
BASE=http://192.168.230.118:18080/api/v1
VM=http://192.168.230.118:18428
curl -sc ck -X POST $BASE/login -H 'Content-Type: application/json' \
  -d '{"username":"manager","password":"!nkia1234"}' -o /dev/null

# ① DPM — collector가 폴마다 기록하는 상태값이 가장 정확
for tid in $(curl -sb ck "$BASE/targets?type=database" | jq -r '.targets[].id'); do
  curl -sb ck "$BASE/targets/$tid/collectors" | \
    jq -r '.collectors[] | "\(.last_collect_status)\t\(.last_collected_at)\t\(.last_collect_error // "")"'
done

# ② SMS — server target들의 최신 샘플 시각 (5분 이상 벌어지면 이상)
curl -sG "$VM/api/v1/query" --data-urlencode \
  'query=max(timestamp({target_id=~".+"})) by (target_id)'

# ③ KCM — kcm.* 최신 샘플 (빈 결과 = 끊김. 24h lookback으로 끊긴 시각 추정)
curl -sG "$VM/api/v1/query" --data-urlencode \
  'query=max(tlast_over_time({__name__="kcm.node.cpu_usage"}[24h]))'

# ④ APM — 웹 UI 애플리케이션 목록에서 RPS 유입 확인 (트레이스는 ClickHouse라 VM 질의 불가)
```

판독 요령:
- **DPM `error`**: `last_collect_error`에 원인이 그대로 나온다
  (dial timeout=라우팅/§3-1, Access denied=grant/§3-1-3).
- **KCM 빈 결과 + pod는 Running**: 에이전트 인증 문제 가능성 높음 —
  master 로그에서 `Unauthenticated` 확인 → §4-3-1 토큰 재발급.
- **전 계층 동시 중단**: 118 서버 자체(재시작/업데이트) 또는 118→VM 라우트를 의심.
