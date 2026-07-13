---
title: 테스트용 AP 서버 (lucida-next 192.168.230.118) 참고 정보
status: Active
owner: project
last_reviewed: 2026-07-13
tags:
  - ref
  - testbed
  - lucida-next
  - observability
summary: 테스트베드 관측을 수집하는 lucida-next AP 서버(118)의 접속 정보, 포트맵, 등록 자산 현황, 검증 엔드포인트, 알려진 제약.
---

# 테스트용 AP 서버 (lucida-next `192.168.230.118`)

> 테스트베드(109 kubeadm)의 관측 데이터를 수집하는 lucida-next 서버.
> 에이전트 설치 절차는 `runbook-observability-agents.md` 참조.
> ⚠ 구 `192.168.230.104`는 워크스테이션 자체 lucida 개발스택으로 **별개**다.

## 1. 접속 정보

| 항목 | 값 | 비고 |
|---|---|---|
| 웹 UI | `http://192.168.230.118:13000` | |
| 로그인 | `manager / !nkia1234` | 웹·API 공통 |
| API 로그인 | `POST http://…:18080/api/v1/login` `{"username","password"}` | `lucida_session` 쿠키 발급 |
| SSH | **미확보** — `root@118` publickey 거부 (2026-07-13 기준) | 서버 작업 필요 시 관리 주체에 문의 |

## 2. 포트맵 (실측)

| 포트 | 서비스 | 용도 |
|---|---|---|
| 13000 | 웹 UI | 프론트엔드 |
| 18080 | API (query 서비스) | `/api/v1/*` — 등록·조회 전부 |
| 8090 | collector-sms | SMS 에이전트 수신 |
| 14317 | OTLP gRPC | APM 트레이스 수신 (OTel javaagent 대상) |
| 14320 | OpAMP | SMS 에이전트 관리 |
| 7575 | collector-kcm gRPC | KCM 에이전트 수신 |
| 18428 | VictoriaMetrics (vmsingle, 컨테이너 8428) | 메트릭 저장소. **검증용 직접 질의 가능** |

배포 형태는 docker compose 단일 호스트(vmsingle + redpanda + ClickHouse 등).
메트릭=VictoriaMetrics(보존 7d), 로그/트레이스=ClickHouse.

## 3. 등록 자산 현황 (2026-07-13)

| type | 수 | 내용 |
|---|---|---|
| server | 3 | tb-w1/w2/w3 (SMS, 이름=200.x 브리지 IP) |
| application | 19 | Spring 앱 전부 (APM, 매칭키=service.name) |
| database | 3 | PostgreSQL-commerce · MySQL-fooddelivery · Oracle-corebanking (DPM) |
| kubernetes | 1 | `cluster-18c410c8-…` (display `rca-testbed`, KCM 자동등록 정본) |

⚠ `GET /api/v1/targets` 기본 목록에 kubernetes type이 안 나온다 —
`?type=kubernetes` 필터로 조회.

## 4. 검증 엔드포인트 치트시트

```bash
# 로그인
curl -c ck -X POST http://192.168.230.118:18080/api/v1/login \
  -H 'Content-Type: application/json' -d '{"username":"manager","password":"!nkia1234"}'

# 자산 목록 (type 필터)
curl -b ck "http://192.168.230.118:18080/api/v1/targets?type=database"

# DPM 수집 상태 (collector가 폴마다 기록)
curl -b ck http://192.168.230.118:18080/api/v1/targets/<id>/collectors
#   → last_collect_status / last_collect_error

# DPM 세션 실황 (legacy /databases/{id}/sessions 는 0 반환 — 쓰지 말 것)
curl -b ck http://192.168.230.118:18080/api/v1/databases/<id>/dpm/session/status

# VictoriaMetrics 직접 질의 (가장 확실한 유입 검증)
curl -G http://192.168.230.118:18428/api/v1/query \
  --data-urlencode 'query=count({target_id="<id>"})'
curl -G http://192.168.230.118:18428/api/v1/label/__name__/values \
  --data-urlencode 'match[]={target_id="<id>"}'
```

메트릭 네임스페이스: SMS=호스트, APM=OTLP 트레이스(ClickHouse),
DPM=`dpm.<engine>.*`, KCM=`kcm.<resource>.<measurement>`.

## 5. 알려진 제약 (2026-07-13 빌드 기준)

- **Polestar Java agent jar 미포함** — APM은 OTel javaagent → 14317 직접 전송으로 대체.
- **KCM OperatorService = TODO stub** — 에이전트 로그에 Unimplemented 에러가
  3ms 간격으로 도배되지만 수집 무관.
- **`KCM_TRUST_UNTOKENED=true`** — KCM 토큰 바인딩이 사실상 미작동, 에이전트는
  cluster_id 자동등록 경로로 target 생성(자동 managed/approved).
- 테스트베드 방향 통신(118→VM)은 워커의 `192.168.230.0/24` 라우트에 의존
  (netplan `99-testbed-230-route.yaml`, runbook §3-1).
