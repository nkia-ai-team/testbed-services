# rca-scenario-runner — 카탈로그 이중구조 판정 (2026-07-23)

컨테이너 `scenario-runner` @ GB10(192.168.200.109), 읽기 전용 조사. 모든 근거는 컨테이너 내부
`/app/backend/app/*.py` (repo = rca-scenario-runner) + 실행 시점 env/state.

## 핵심 사실: 두 카탈로그는 id 공간이 완전히 분리(disjoint)됨

| 축 | 레거시 service-spec 카탈로그 | F-시리즈 manifest 카탈로그 |
|---|---|---|
| 소스 파일 | `/app/scenarios/*/service-spec.yaml` (마운트=런너 repo `scenarios/services`) | `/app/scenario-manifests/*.yaml` (마운트=testbed-services `scripts/scenarios/manifests`) |
| 로더 | `scenarios.py` `get_scenario/list_scenarios` (env `SCENARIOS_ROOT=/app/scenarios`) | `manifests.py` `get_manifest/load_manifests` (env `SCENARIO_MANIFEST_ROOT=/app/scenario-manifests`) |
| 계약/실행 자산 | repo bash script (`_resolve_script_path`) | `/opt/lucida/scenario-contracts` = `run-scenario.sh`(dispatcher) + `catalog.json`(TRUSTED_CATALOG) + `registry/` + `profile-control.py` |
| 개수 | 28개 (plopvape/commerce/food-delivery/cross-domain/social-feed) | 64 manifest, 그중 `readiness=ready & live_enabled=true` = 30 |
| id 예 | `commerce:01`, `food-delivery:01`, `cross-domain:f15-t2` | `F01-G` / slug `f01-g-absorbed-pg-delay` |

충돌 실측: `get_scenario("F02-P")` = `None`, `get_scenario("f02-p-...")` = `None`;
`get_manifest`에는 `F02-P`/`f02-p-...`/`f02-p` 세 키 모두 존재. → 두 공간은 절대 겹치지 않는다.

## 질문별 답

**Q1. 웹 UI `/api/scenarios` 소스** — `main.py:45` → `list_scenarios()` (`scenarios.py`).
`_DEFAULT_SCENARIOS_ROOT = <repo>/scenarios/services`, env `SCENARIOS_ROOT=/app/scenarios`로 오버라이드.
즉 **런너 repo의 service-spec 28개**(레거시)만 나온다. F-시리즈는 이 목록에 **안 나온다**.
단, F-시리즈는 별도 엔드포인트 `main.py:50 /api/scenario-manifests` 로 64개가 그대로 노출됨.

**Q2. 실제 실행 경로 authoritative 카탈로그** — F-시리즈 실행은 **`/opt/lucida/scenario-contracts`(TRUSTED_CATALOG/dispatcher)** 로 돈다.
근거 체인: `runner.start()`(runner.py:247) → `get_scenario()`가 None → `get_manifest()` fallback(:250) →
`external_manifest.runtime_scenario()` → `script_path = self.dispatcher_path`(:270, `TRUSTED_DISPATCHER=/opt/lucida/scenario-contracts/run-scenario.sh`) →
`prepare_capsule(contract_root=self.dispatcher_path.parent)`(:295) 로 F-시리즈 계약 루트에서 capsule 컴파일 →
`_execute_adaptive`가 `production_runtime(...)` 로 실행하며 evidence에 `catalog_sha256=file_sha256(TRUSTED_CATALOG)` 기록(runner.py:864-873).
service-spec 스크립트 경로(`_resolve_script_path`)는 오직 레거시 id일 때만.

**Q3. 라우트 dispatch/검증** — `POST /api/scenarios/{id}/run`(main.py:128) → `runner.start(id)`.
`start()`는 **먼저 `get_scenario`(service-spec), 실패 시 `get_manifest`(F-시리즈) 순**으로 id를 해석.
id 공간이 disjoint이므로: 레거시 id → 레거시 스크립트 실행 / F-시리즈 id(`F08-P` 등) → TRUSTED dispatcher 실행. 오분기 없음.
둘 다 못 찾으면 `ValueError→404`. 즉 이 단일 run 라우트가 **양쪽 카탈로그를 모두 실행 가능**하다.

**Q4. F-시리즈를 실제로 트리거·실행·캡처 가능한가?** — **YES.** 가능할 뿐 아니라 지금 이 런너의 배치 드라이버가
F-시리즈 **전용**으로 돌고 있다. `live_queue.py`가 실 배치:
`_queue_contract()`(:923)가 `registry/controllers.json`의 `live_scenario_ids`(30개, `F01-R…F03-H`) + manifest_root를 교차검증(각 `live_enabled=true` 필수)해 큐를 동결 →
`self.runner.start(scenario_id=<F-id>, mode="run")`(:472)로 순차 실행.
실측 상태: `/app/state/live-queue.json` = `queue_id=live-30-9d16a1f8`, F-시리즈 30개, `next_index=18`,
`current=F08-P` 에서 `phase=paused`. (이 정지는 카탈로그 배선이 아니라 메모리에 기록된 F08-P live.json 조기삭제 버그 때문 — 별개 이슈.)

**Q5. 배선 수정 필요?** — **불필요.** 실행 런타임은 이미 F-시리즈(TRUSTED_CATALOG)로 정확히 배선됨.
드리프트는 순수 **UI 표시 문제**: `/api/scenarios`(리스트)는 레거시 28개만 보여주고 F-시리즈는 `/api/scenario-manifests`에 있다.
표시를 통일하고 싶을 때의 최소안(택1, 지금 적용하지 말 것):
- 프론트엔드가 메인 목록을 이미 존재하는 `/api/scenario-manifests`(또는 그중 ready 30개)로 렌더링하도록 전환 — **백엔드/마운트/env 변경 0**.
- 또는 아무것도 안 함(순수 표시 이슈이므로).

## 부수 리스크(경고, F-시리즈와 무관)
`POST /api/scenarios/{id}/run`이 레거시 id도 그대로 받아 **옛 service-spec bash script를 실 실행**한다.
UI에서 레거시(plopvape/commerce:01 등)를 클릭하면 F-시리즈가 아닌 구 스크립트가 돈다.
"폐기 예정" 레거시가 아직 UI·실행 양쪽에 살아있는 것이므로, 정리하려면 service-spec 마운트/디렉토리에서
레거시 도메인을 제거하는 게 근원 차단(선택 사항).

## Verdict (한 문단)
이중구조는 **실행 배선 결함이 아니라 UI 표시 드리프트**다. F-시리즈 시나리오는 이 런너에서 실제로 트리거·실행·캡처
가능하며(그 근거로 live-30 큐가 F-시리즈 전용으로 동결·구동 중, 현재 F08-P에서 별개 버그로 paused),
실행 authority는 `runner.start`→manifest fallback→`/opt/lucida/scenario-contracts`(TRUSTED_CATALOG/dispatcher)로
확정된다. 웹 `/api/scenarios` 목록만 레거시 28 service-spec을 보여줄 뿐이고 F-시리즈는 `/api/scenario-manifests`로
노출된다. id 공간이 disjoint라 오분기·충돌은 없다. 따라서 배선 수정은 불필요하며, 원하면 프론트 목록을
`/api/scenario-manifests`로 돌리는 프론트 단독 변경이 최소안이다.
