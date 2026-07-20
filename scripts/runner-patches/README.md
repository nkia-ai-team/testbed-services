# runner-patches

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
