# runner-patches — ⚠️ 은퇴(2026-07-24)

**이 디렉터리의 역할은 끝났다.** 2026-07-24에 정본화 PR #20이 main에 머지되고
109 `/root/rca-scenario-runner`가 **git checkout(main) 기반 배포로 전환**되면서,
여기 보존된 변경은 전부 GitHub `nkia-ai-team/rca-scenario-runner` main에 정식
커밋으로 존재한다(status-class=d70effc, db-selectors=7cc6b4e). 이후 러너 변경은
러너 레포에 커밋→109에서 `git pull`+`docker compose build`로 배포하면 되고, 새
patch를 여기 추가하지 않는다. 파일들은 이력 참고용으로만 남긴다.

---

(이하는 은퇴 전 기록)

`rca-scenario-runner`(109 배포본)의 백엔드는 GitHub 정본(`nkia-ai-team/rca-scenario-runner` main)보다 크게 앞서 있고 대부분의 컨트롤러/캡처/큐 코드가 정본에 없다(예: `runner.py`가 main 대비 984줄 차이, `live_queue.py`·`controller.py`·`preflight.py` 등은 main에 아예 부재). 109는 사실상 비-git 배포본이라 이 코드는 버전관리 밖에 있다.

이 디렉터리는 그 배포본에 적용한 변경을 **드리프트로 유실되지 않도록** patch로 보존한다. GitHub 정본 반영 방법은 사용자 결정 대기 중이므로 여기서는 손대지 않는다.

## 적용 방법

정본 부재로 최소 diff의 기준선이 없어(변경 대상 파일 전체가 main에 없음 + 편집 전 원본이 배포본 동기화로 덮여 재구성 불가), 이 patch는 대상 파일의 **현재 전체 내용을 담은 root 스냅샷 patch**다. 빈 트리 기준으로 파일을 재생성한다:

```bash
cd <rca-scenario-runner 체크아웃>
git am < scripts/runner-patches/<날짜>-newcontract-v2.patch   # 대상 파일이 없을 때
# 또는 대상 파일을 이 스냅샷으로 덮어쓰기:
git apply --3way scripts/runner-patches/<날짜>-newcontract-v2.patch
```

109 사본: `root@192.168.200.109:/root/rca-scenario-runner-nc-v2-<날짜>.patch`.

## 2026-07-20-newcontract-v2.patch — 담긴 변경 (신계약 v2 + F04-R)

대상 15파일(backend/app 7 + backend/tests 7 + docker-compose.yml). 이 중 신계약 관련 **실제 변경분**:

- `capture_orchestration.py` — PRE/POST_WINDOW 2h/45m→10m/20m, 검증 메시지
- `controller.py` — CapturePlan pre/post 7200/2700→600/1200, 정규화 기본값·clean_window
- `adaptive_runtime.py` — clean_window 최소 7200→1800(2h→30m)
- `live_queue.py` — CLEAN_WINDOW 2h→30m + **R6 preflight 게이트 배선**(주입 전 실행·5분 재검사·타임아웃 skip·verdict 파일드롭)
- `production_runtime.py` — `ProductionCaptureInvoker`가 `--preflight-json`·`--normal-segment` 전달(runs/<id>/ 파일드롭 수용)
- `preflight.py` — **신규**: R6 1층 결정적 게이트(verdict·검사 빌더) + 2층 AI 인터페이스
- `live_probes.py` — F04-R kafka 대상(`testbed-kafka-0`→`app=testbed-kafka`) 승인 목록 추가
- 대응 테스트 갱신·신규(preflight 8, 큐 preflight 4, invoker forwarding 1, kafka 1)

캡처 셸 스크립트(capture-eval-case.sh·dump-normal-segment.sh·assemble-eval-case.sh)는 이 레포(`scripts/`)에 이미 git으로 있으므로 이 patch에는 없다.

## 2026-07-23-live-probes-status-class.patch — 담긴 변경 (status-class loadgen 관측)

대상 1파일(`backend/app/live_probes.py`, root 스냅샷 — 위와 동일한 이유로 diff 기준선 없음). 109 컨테이너에서 `docker exec scenario-runner cat backend/app/live_probes.py`로 실측한 현재본에 `_loadgen_observation`(430행 부근) 3지점만 편집:

- allowlist set에 4 query_id 추가: `loadgen.write_step_status_rate`·`loadgen.read_step_status_rate`·`loadgen.food_create_status_rate`·`loadgen.transfer_2xx_rate`
- field map 추가: `write_step_status_rate→business_nonok_rate`, `read_step_status_rate→read_nonok_rate`, `food_create_status_rate→business_5xx_rate`, `transfer_2xx_rate→business_2xx_rate`
- range check를 `field == "checkout_5xx_rate"` 하드코딩에서 `field.endswith("_rate")` 일반화(모든 rate 필드 [0,1] 검증)

**아직 적용되지 않음(2026-07-23 기준) — repo 쪽 monitor 확장(`load_north_south_executor.py`) 및 `queries.json` 4항 선언과 짝을 이루지만, 이 patch를 컨테이너에 반영하기 전까지 새 query_id는 `LiveProbeError`로 거부된다.**

적용: `git apply --3way scripts/runner-patches/2026-07-23-live-probes-status-class.patch` (대상 파일이 이미 있으므로 `git am`이 아니라 `git apply --3way`를 쓴다) → 컨테이너 반영은 `docker cp`/재기동.
롤백: `git apply -R scripts/runner-patches/2026-07-23-live-probes-status-class.patch` 또는 직전 스냅샷(2026-07-20-newcontract-v2)의 `backend/app/live_probes.py`로 복원.

## 2026-07-24-live-probes-db-selectors.patch — 담긴 변경 (DB selector: outbox 적체)

대상 1파일(`backend/app/live_probes.py`, root 스냅샷). **2026-07-23 status-class 스냅샷을 포함·대체한다**(그 위에 1분기 추가) — 적용 시 07-23 patch는 따로 적용할 필요 없음:

- `_database_observation`에 `database.outbox_unpublished_count` 분기 추가: `kubectl exec testbed-oracle-0`(rca-testbed-banking) → sqlplus `SELECT COUNT(*) FROM banking.outbox_events WHERE published_at IS NULL`(FREEPDB1, init.sql:60-70 앵커). F18-P(outbox relay 정지) 결정 증거.
- 계약: `OUTBOX_UNPUBLISHED_CONTRACT = {"namespace": "rca-testbed-banking"}` 정확일치, 숫자 아닌 출력은 `LiveProbeError`.

**아직 적용되지 않음(2026-07-24 기준)** — repo `queries.json`의 `database.outbox_unpublished_count` 선언과 짝. 적용 전까지 해당 query는 러너에서 거부된다.

적용/롤백은 위와 동일(`git apply --3way` / `-R`).
