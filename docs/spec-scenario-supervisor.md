---
title: AI Scenario Supervisor 설계
status: Draft
owner: project
last_reviewed: 2026-07-19
tags:
  - scenario
  - supervisor
  - automation
  - monitoring
summary: 시나리오 큐가 pause·실패로 멈췄을 때 AI가 진단·수리·재개하고, 캡처 산출물의 시나리오 메타데이터 무결성을 상시 검증하는 감시 루프의 계약을 정의한다.
---

# AI Scenario Supervisor 설계

## 1. 목적과 비목표

시나리오 실행의 기계 부분(순차 큐, adaptive controller, cleanup, 캡처)은
`rca-scenario-runner`가 결정론적으로 수행한다. 그러나 controller는 의도적으로
fail-closed라서 의미 오류·관측 계약 결함·캡처 실패는 전부 queue pause로
끝나고, 사람이 개입할 때까지 큐가 며칠씩 멈춘다(F03-G: 6 run, 2일 소모).

supervisor의 목적은 이 **pause 해소 루프를 AI가 대신 도는 것**이다. 주기적으로
큐 상태를 읽고, 멈춤의 원인을 진단하고, 허용된 조치 범위 안에서 수리한 뒤
재개하며, 사람 판단이 필요한 경우에만 요약과 함께 에스컬레이션한다.

비목표:

- controller를 대체하거나 우회하지 않는다. 주입 강도·성공 판정·cleanup은
  계속 controller 소유다. supervisor는 controller의 입력(manifest·관측 계약·
  환경)을 고치고 재개할 뿐, 실행 중인 run의 판정에 개입하지 않는다.
- 성공을 만들어내지 않는다. "golden에 맞춰 결과를 만들지 않는다"는
  controller 원칙을 supervisor에도 그대로 적용한다. 실패한 run을 성공으로
  바꾸는 유일한 경로는 결함을 고친 뒤 같은 시나리오를 규칙대로 재실행하는
  것이다.

## 2. 선행 결함 수리 (P0)

07-19 F03-G 분석(런북 §10)에서 확인된 다음 결함은 supervisor 가동 전에
고쳐야 한다. tick 기록이 없으면 supervisor의 진단 자체가 성립하지 않는다.

| # | 결함 | 수리 |
| --- | --- | --- |
| 1 | tick별 관측·판정 미기록 | controller가 tick마다 신호값·usable 여부·streak·action을 `runs/<run-id>/ticks.jsonl`에 append |
| 2 | `logs/<run-id>.log` 0바이트 | run 로그 캡처 경로 수리 |
| 3 | level timeout < 부하 프로파일 길이 (F03-G 10m < 11m) | 전체 live manifest에 대해 `timeout ≥ ramp+hold+settle` 정적 검증 추가, 위반분 수정 |
| 4 | `user_p95` freshness 60s vs 분당 배치 적재 | 실측 케이던스 기반 freshness(120s)로 정정, 다른 지표도 케이던스 실측 후 고정 |
| 5 | `case-f01-r-791f695b` 메타데이터 공백 | registry 정본으로 backfill 후 SHA 기록, 불가하면 케이스를 평가 제외로 표시 |

## 3. 아키텍처

```text
.104 (개발 서버)
  systemd timer (10분)
    → claude -p "supervisor 프롬프트" (headless, 세션당 단발)
        ├─ GET 109:8091/api/live-queue, /readiness, /api/active
        ├─ 상태별 플레이북 실행 (아래 §4)
        ├─ 필요 시: 109 run 아티팩트 / VM(119:18428) / tb-runner 조회
        ├─ 조치: manifest·registry 수정 → 테스트 → runner 배포 → resume
        └─ state 기록: testbed-services/.omc/supervisor/state.json
                       + 조치 로그 append (supervisor-log.md)
```

- 실행 주체는 Claude Code headless(`claude -p`)를 기준 구현으로 한다. 동일
  프롬프트 계약을 지키면 Codex 등 다른 에이전트로 교체 가능하다.
- 한 wakeup은 **읽기 → 진단 → (허용 조치) → 기록 → 종료**의 단발 사이클이다.
  큐가 정상 진행 중이면 API 1~2회 조회 후 즉시 종료한다(no-op).
- 모든 조치는 git 커밋(수정 근거 포함)과 supervisor-log에 남긴다. 로그 없는
  조치는 금지다.

## 4. 상태별 플레이북

| 큐 상태 | supervisor 행동 |
| --- | --- |
| `running` | `updated_at` 나이 확인. `max_injection + cleanup + 30m`을 넘게 진전이 없으면 stalled로 판단하고 에스컬레이션. 그 외 no-op |
| `waiting_capture` | capture job 상태 확인. `failed`면 `capture-error.log` 진단 → 환경 원인(자격증명·디스크·연결)이면 수리 후 재시도. 완료면 §6 사후 검증 수행 |
| `paused` | §5 진단 분류 → 허용 조치면 수리·재개, 아니면 에스컬레이션 |
| `completed` | 다음 배치 후보 보고서 작성 후 종료 |

## 5. pause 진단 분류와 조치 권한

진단 입력: `reason`, `runs/<run-id>/{state,result,decisions,ticks}.json*`,
run 로그, VM 시계열 재질의, tb-runner 잔존 아티팩트.

| 분류 | 예 | 조치 | 권한 |
| --- | --- | --- | --- |
| A. 일시 관측 요동 | SSH/probe 일시 실패로 재시도 소진 | 테스트베드 건강 확인 후 resume | **자율** |
| B. 관측 계약 결함 | 지표 이름·단위·freshness·allowlist 오류, timeout/케이던스 불일치 | manifest·observation 계약 수정 → 정적 테스트 통과 → runner 반영 → resume | **자율** (단, §7 제약) |
| C. 성공·중단 조건의 의미 변경 | 임계값 완화, 조건식 교체, 시나리오 의미 수정 | 근거 분석서 작성 후 사용자 승인 대기 | **승인 필요** |
| D. 테스트베드 서비스 결함 | 앱 코드·k8s 리소스·DB 결함 | 원인 분석서 + 수리안 작성 후 승인 대기 (기존 결함 1~6 수리와 동일 절차) | **승인 필요** |
| E. 캡처·저장 실패 | 자격증명 만료, 디스크 부족, dump 실패 | 환경 수리 후 캡처 재시도 | **자율** |
| F. DIRTY / cleanup 실패 | 복구 미완 | 상태 보고만. 자동 mutation 금지, 승인 후 수동 절차 | **승인 필요** |

권한 경계의 원칙: **관측을 실제에 맞게 고치는 것은 자율, 판정 기준을 바꾸는
것은 승인**. freshness를 실측 케이던스에 맞추는 것(B)과 p95 임계값을 500에서
800으로 올리는 것(C)은 다르다. 경계가 모호하면 C로 취급한다.

## 6. 시나리오 메타데이터 무결성 (상시 검증)

캡처 데이터에 시나리오 설명이 정확히 실리는지가 평가 전체의 전제이므로,
supervisor가 매 사이클 다음을 검증한다.

- **사전(pre-flight)**: 현재 큐의 모든 ID가
  `scripts/scenarios/registry/scenario-metadata.json`에 6필드(title,
  description, cause, injection_summary, user_impact,
  distinguishing_evidence) 모두 비어 있지 않게 존재하는지. 누락 발견 시
  해당 시나리오 도달 전에 에스컬레이션.
- **사후(post-capture)**: 새 케이스마다 런북 §7 read-back(6필드 jq 검사,
  `scenario_metadata_sha256` 형식, canonical JSON 재직렬화 SHA 대조,
  `golden_anomaly_file == false`, 모델 checksum)을 실행. 불일치 케이스는
  평가 사용 금지 표시 후 에스컬레이션.
- **정본-케이스 의미 대조(주기)**: 케이스의 `scenario_metadata`가 실제 주입
  내용(result.json의 profile·parameters)과 어긋나지 않는지 AI가 교차 읽기로
  점검한다. 예: injection_summary가 "PG row lock"인데 실주입이 north-south
  surge면 즉시 보고.

기존 위반: `case-f01-r-791f695b`는 `scenario_metadata`가 빈 객체이고 SHA가
없다(메타데이터 계약 도입 전 캡처). §2-5의 backfill 대상이다.

## 7. 안전 규칙

- supervisor는 시나리오를 **시작·주입하지 않는다**. start/resume API만 쓴다.
  주입은 항상 controller가 규칙(2시간 clean window 포함)대로 수행한다.
- 동일 시나리오에 대한 자율 수리·재개는 **연속 2회**까지. 3번째 실패는 무조건
  에스컬레이션한다(같은 원인을 반복 수리하는 루프 방지).
- 수정은 항상: 로컬 테스트(`test-scenarios.sh`, runner pytest) 통과 → git
  커밋 → runner 반영 → resume 순서. 테스트 없이 반영하지 않는다.
- 에스컬레이션은 supervisor-log 기록 + 사용자 알림(브리핑 파일과 알림 채널)
  으로 하고, 큐는 pause 상태 그대로 둔다.

## 8. 구현 단계

1. **P0**: §2 선행 결함 5건 수리 (runner + manifest + 케이스 backfill).
2. **M1 수동 모드**: supervisor 프롬프트·플레이북을 스킬로 작성하고, 사람이
   세션에서 호출해 1사이클씩 검증한다. F03-G 재실행으로 A·B 분류 실증.
3. **M2 자동 모드**: systemd timer + `claude -p` 배선, state·로그 영속화,
   에스컬레이션 알림 연결. 감시만 1~2일 돌려 오탐 확인 후 자율 조치 개방.
4. **M3 확장**: 큐 완주 후 다음 배치 자동 제안(카탈로그 `ready` 후보), 캡처
   케이스 품질 리포트 자동 생성.
