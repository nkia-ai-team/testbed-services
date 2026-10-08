---
title: 시나리오 수명주기와 녹화 대기 큐
status: Active
owner: project
last_reviewed: 2026-10-08
tags:
  - scenario
  - harness
  - recording
summary: 시나리오가 설계(draft)에서 후보(candidate)를 거쳐 정식(official)이 되거나 폐기(parked)되는 상태와, 누가 언제 상태를 바꾸는지, 운영 하네스가 읽는 녹화 대기 큐의 형식.
---

# 시나리오 수명주기와 녹화 대기 큐

## 1. 상태

| 상태 | catalog 표시 | 뜻 | 러너 | 분류 장부 |
|---|---|---|---|---|
| 설계 | `readiness: draft` | 설계 중이거나 아직 실행할 수 없음 | 실행 목록에 없음 | §4-1 |
| **후보** | `readiness: ready`, `stage: candidate` | 실행은 되지만 정상 녹화가 아직 없음 | 실행 가능, 웹에서는 "검증 대기"로 따로 보임 | §4-1 |
| **정식** | `readiness: ready`, `stage: official` | **eval-cases 에 정상 녹화(`evaluation_eligible: true`)가 1개 이상 있음** | 웹의 정식 목록 | §4, 집계 대상 |
| 폐기 | `readiness: parked` (`stage` 없음) | 검증을 통과하지 못해 뺀 것. 정의와 설계 시트는 참고 자료로 남김 | 실행 목록에서 빠짐 | §5 |

- 정식인지는 녹화본이 정한다. 녹화본은 104 `/data/eval-cases/case-*/meta.json`이다. `stage`는 그 사실을 저장소에 옮겨 적은 표시이고, 운영 하네스가 녹화에 성공했을 때만 `official`로 바꾼다.
- 설계 원칙 문서와 분류 장부의 모든 수(묶음 비율, 서비스 균형)는 정식만 센다.
- 2026-10-08 전환 시점: ready 40종 중 정상 녹화가 있는 35종을 정식, 나머지 5종(F10-P, F15-R, F15-T2, F20-Q, F30-R)을 후보로 표시했다. 같은 날 그중 정상 녹화가 하나도 없는 기존 4종(F10-P, F15-R, F15-T2, F20-Q)을 사용자 결정으로 폐기해 후보는 F30-R 하나다.

## 2. 누가 상태를 바꾸는가

| 전이 | 하는 쪽 | 함께 바꾸는 것 |
|---|---|---|
| (없음) → 후보 | 시나리오 생성 하네스(`testbed-scenario-create`) | catalog 행, controller 등록(강도 찾기 `calibration` 모드), 정답지, 장부 §4-1, **큐에 `reason: new`로 추가** |
| 후보 → 정식 | 운영 하네스(tmux) | 녹화 성공 뒤 `stage: official`, 찾은 강도를 고정(`evaluation` 모드), 장부 §4-1 → §4, 큐에서 제거 |
| 후보 → 후보(재시도) | 운영 하네스 | 검증 결과가 "시나리오 보강"이면 강도나 부하 조정, 큐 항목 `attempts` +1 |
| 후보 → 폐기 | 운영 하네스 | `attempts`가 3을 넘거나 검증이 "폐기 제안"이면 `readiness: parked`, `stage` 삭제, controller 를 `registry/controllers-parked.json`으로 옮기고 `live_scenario_ids`에서 뺌, 장부 §5에 사유와 검증 보고서 위치, 큐에서 제거 |
| 큐 보류 | 운영 하네스 | 환경 문제(수집기 정지 등)면 `status: hold`, `hold_reason`. 재시도 횟수에 세지 않는다 |
| 정식 → 큐(재녹화) | 사람, 또는 사람이 지시한 세션 | 큐에 `reason: recapture`(새 제품 버전 등) 또는 `user`로 추가. 정식 상태는 그대로 |
| 보류 해제, 폐기 되살리기 | 사람 | 큐 `status: waiting`으로 되돌리거나, §5의 시나리오를 다시 후보로 |

시나리오 하나의 검증(실행 데이터가 쓸 만한가)은 검증 하네스(`testbed-scenario-verify`)가 판정하고, 위 전이는 그 판정을 보고 운영 하네스가 한다.

## 3. 녹화 대기 큐

파일: [`scripts/scenarios/recording-queue.json`](../scripts/scenarios/recording-queue.json). main 이 정본이다. 운영 하네스는 실행 하나가 끝날 때마다 origin/main 에서 다시 읽으므로, 다른 세션이 추가한 항목도 지시 없이 다음 차례에 실행된다.

```json
{
  "schema_version": 1,
  "entries": [
    {
      "scenario_id": "F30-R",
      "reason": "new",
      "note": "새 시나리오 첫 검증",
      "requested_by": "testbed-scenario-create",
      "requested_at": "2026-10-08",
      "attempts": 0,
      "status": "waiting"
    }
  ]
}
```

| 칸 | 값 |
|---|---|
| `scenario_id` | catalog 의 ready 시나리오. 한 시나리오는 큐에 한 번만 |
| `reason` | `new`(새 후보), `no-recording`(후보인데 녹화가 없음), `recapture`(정식의 재녹화), `user`(사람 요청) |
| `note` | 한 줄 설명. `recapture`면 대상 제품 버전 |
| `requested_by`, `requested_at` | 넣은 쪽과 날짜 |
| `attempts` | 운영 하네스가 실제로 돌린 횟수 중 "시나리오 보강"으로 끝난 횟수. 겹침, 환경 문제로 다시 돈 것은 세지 않는다 |
| `status` | `waiting`(차례 대기) 또는 `hold`(사람 확인 필요) |
| `hold_reason` | `hold`일 때만, 무엇을 확인해야 하는지 |

- 순서는 배열 순서다. 운영 하네스는 앞에서부터 `waiting`인 첫 항목을 고른다.
- 모든 후보는 큐에 있어야 한다(검사 `tests/test_recording_queue.py`). 큐에서 빠지는 길은 정식 승격과 폐기 둘뿐이다.
- 실행 중인 항목의 진행 상태(실행 id, 시각)는 큐에 쓰지 않는다. 운영 하네스의 로컬 기록에 둔다.
