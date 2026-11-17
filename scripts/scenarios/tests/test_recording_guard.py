"""녹화 창과 덤프 대기 배포 보류 (scripts/scenarios/recording-guard.py, deploy-live-promotions.sh)."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
GUARD_PATH = ROOT / "recording-guard.py"
DEPLOY_PATH = ROOT / "deploy-live-promotions.sh"
_spec = importlib.util.spec_from_file_location("recording_guard", GUARD_PATH)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

# 러너 QueuePhase(rca-scenario-runner backend/app/live_queue.py) 전부.
RUNNER_PHASES = (
    "idle", "running", "waiting_capture", "waiting_clean_window", "paused", "completed",
    "cycle_reset", "cycle_normal", "cycle_buffer", "cycle_cooldown",
)
# 운영 스킬 testbed-operate recording-window.sh 의 녹화 창(창 안) 단계.
RECORDING_WINDOW = ("cycle_reset", "cycle_normal", "cycle_buffer", "running", "cycle_cooldown")
CAPTURE = ("waiting_capture", "waiting_clean_window")
SAFE = ("idle", "paused", "completed")

IDLE_ACTIVE = json.dumps({"is_active": False, "scenario_id": None, "run_id": None, "mode": None})


def _queue(phase: str, **extra: object) -> str:
    return json.dumps({"phase": phase, "current_scenario_id": "X", **extra})


def _active(mode: str | None, run_id: str = "r1") -> str:
    return json.dumps({"is_active": mode is not None, "run_id": run_id, "mode": mode})


class _FakeRunner:
    """러너 HTTP 흉내. live-queue 를 몇 번째로 읽느냐에 따라 phase 를 바꿀 수 있다."""

    def __init__(self, phase_at=lambda n: "idle", log: Path | None = None,
                 status: int = 200, body: str | None = None, delay: float = 0.0,
                 active: str = IDLE_ACTIVE, queue_extra: dict | None = None) -> None:
        self.phase_at = phase_at
        self.log = log
        self.queue_reads = 0
        runner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:  # 시험 출력 조용히
                pass

            def do_GET(self) -> None:  # noqa: N802
                if delay:
                    time.sleep(delay)
                if self.path == "/api/live-queue":
                    runner.queue_reads += 1
                    payload = _queue(runner.phase_at(runner.queue_reads), queue_id="q1",
                                     **(queue_extra or {}))
                    phase = json.loads(payload)["phase"]
                    runner._log(f"GET /api/live-queue {runner.queue_reads} {phase}")
                elif self.path == "/api/active":
                    payload = active
                    runner._log("GET /api/active")
                elif self.path == "/api/live-queue/readiness":
                    payload = json.dumps({"ready": True})
                    runner._log("GET /api/live-queue/readiness")
                else:
                    self.send_error(404)
                    return
                data = (body if body is not None else payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.api = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def _log(self, line: str) -> None:
        if self.log is not None:
            with self.log.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def __enter__(self) -> "_FakeRunner":
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


def _closed_port_api() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


class RecordingGuardDecisionTests(unittest.TestCase):
    def test_phase_sets_cover_every_runner_phase_once(self) -> None:
        sets = (guard.RECORDING_WINDOW_PHASES, guard.CAPTURE_PHASES, guard.SAFE_PHASES)
        self.assertEqual(sorted(p for s in sets for p in s), sorted(RUNNER_PHASES))
        self.assertEqual(guard.RECORDING_WINDOW_PHASES, frozenset(RECORDING_WINDOW))

    def test_recording_window_phases_block(self) -> None:
        # v4 Q26 답 2: 녹화 창(평시 시작 ~ 식힘 끝) 동안 66 쪽 ssh 금지. cycle_reset, cycle_normal 도 창이다.
        for phase in RECORDING_WINDOW:
            reason = guard.blocking_reason(_queue(phase), IDLE_ACTIVE)
            self.assertIsNotNone(reason, phase)
            self.assertIn("녹화 창", reason)
            self.assertIn(phase, reason)

    def test_blocks_from_injection_until_capture_finishes(self) -> None:
        # 2026-10-10: 덤프 대기 중 배포가 카탈로그 해시를 바꿔 덤프가 실패했다.
        for phase in ("cycle_buffer", "running", "cycle_cooldown", "waiting_capture", "waiting_clean_window"):
            self.assertIsNotNone(guard.blocking_reason(_queue(phase), IDLE_ACTIVE), phase)
        for phase in CAPTURE:
            self.assertIn("덤프 대기", guard.blocking_reason(_queue(phase), IDLE_ACTIVE))

    def test_allows_outside_recording(self) -> None:
        for phase in SAFE:
            self.assertIsNone(guard.blocking_reason(_queue(phase), IDLE_ACTIVE), phase)

    def test_unknown_phase_blocks(self) -> None:
        reason = guard.blocking_reason(_queue("cycle_something_new"), IDLE_ACTIVE)
        self.assertIsNotNone(reason)
        self.assertIn("모르는 phase", reason)

    def test_manual_run_outside_queue_blocks(self) -> None:
        # 큐 밖 웹 실행: 큐는 idle 이어도 러너가 실행 중이면 녹화 창이다(recording-window.sh 와 같음).
        for phase in SAFE:
            reason = guard.blocking_reason(_queue(phase), _active("run", "run-9"))
            self.assertIsNotNone(reason, phase)
            self.assertIn("run-9", reason)
        # 정리(cleanup)는 녹화가 아니다.
        self.assertIsNone(guard.blocking_reason(_queue("idle"), _active("cleanup")))

    def test_decoy_records_do_not_change_the_decision(self) -> None:
        """v4 미끼, 평시 배포 기록은 판정에 쓰지 않는다. phase 만으로 같은 답이 나온다."""
        for phase in RUNNER_PHASES:
            plain = guard.blocking_reason(_queue(phase), IDLE_ACTIVE)
            for status in ("planned", "rolling", "applied", "rolled_back", "skipped"):
                raw = _queue(phase, cycle_decoy_seed=7,
                             cycle_decoys=[{"id": "d1", "kind": "restart", "status": status}])
                self.assertEqual(guard.blocking_reason(raw, IDLE_ACTIVE) is None, plain is None,
                                 (phase, status))

    def test_recording_phases_still_block_with_decoys_recorded(self) -> None:
        for phase in ("cycle_normal", "cycle_buffer", "running", "cycle_cooldown", "waiting_capture"):
            raw = _queue(phase, cycle_decoys=[{"id": "d1", "kind": "restart", "status": "applied"}])
            self.assertIsNotNone(guard.blocking_reason(raw, IDLE_ACTIVE), phase)

    def test_paused_waiting_for_capture_blocks(self) -> None:
        # 리뷰 M2: 덤프 실패로 멈춘 큐는 재개하면 캡처만 다시 하고 카탈로그 해시를 다시 비교한다.
        for origin in CAPTURE:
            reason = guard.blocking_reason(_queue("paused", paused_from_phase=origin), IDLE_ACTIVE)
            self.assertIsNotNone(reason, origin)
            self.assertIn("덤프 대기(일시 정지", reason)
        # paused_from_phase 가 없던 옛 러너 기록: reason 머리로 가른다.
        for head in ("capture failed: run-1: boom", "capture retry failed: run-1: boom"):
            for raw in (_queue("paused", reason=head), _queue("paused", reason=head, paused_from_phase=None)):
                reason = guard.blocking_reason(raw, IDLE_ACTIVE)
                self.assertIsNotNone(reason, raw)
                self.assertIn("덤프 대기(일시 정지", reason)
        # 읽을 수 없거나 러너가 모르는 paused_from_phase 도 막는다.
        for origin in (["waiting_capture"], 3, "cycle_something_new"):
            self.assertIsNotNone(
                guard.blocking_reason(_queue("paused", paused_from_phase=origin), IDLE_ACTIVE), origin)

    def test_other_paused_passes(self) -> None:
        for origin in (None, *RECORDING_WINDOW, "idle", "completed"):
            raw = _queue("paused", paused_from_phase=origin, reason="DIRTY coordinator state blocks the live queue")
            self.assertIsNone(guard.blocking_reason(raw, IDLE_ACTIVE), origin)
        # paused_from_phase 가 있으면 reason 머리를 보지 않는다(러너 _capture_retry_check 와 같음).
        raw = _queue("paused", paused_from_phase="cycle_normal", reason="capture failed: run-1: boom")
        self.assertIsNone(guard.blocking_reason(raw, IDLE_ACTIVE))
        for reason in (None, "", "operator stop", "preflight failed: capture failed: x"):
            self.assertIsNone(guard.blocking_reason(_queue("paused", reason=reason), IDLE_ACTIVE), reason)

    def test_unreadable_response_blocks(self) -> None:
        for raw in ("", "not json", "{}", "[]", "null"):
            self.assertIsNotNone(guard.blocking_reason(raw, IDLE_ACTIVE), raw)
            self.assertIsNotNone(guard.blocking_reason(_queue("idle"), raw), raw)
        # 리뷰 L2: phase 가 목록이나 객체여도 예외(종료 코드 1)가 아니라 보류다.
        for raw in ('{"phase": ["idle"]}', '{"phase": {}}', '{"phase": null}', '{"phase": 1}'):
            self.assertIsNotNone(guard.blocking_reason(raw, IDLE_ACTIVE), raw)


class RecordingGuardHttpTests(unittest.TestCase):
    def test_reads_runner_http(self) -> None:
        for phase in RUNNER_PHASES:
            with _FakeRunner(phase_at=lambda n, p=phase: p) as runner:
                reason = guard.check(runner.api)
            self.assertEqual(reason is None, phase in SAFE, phase)

    def test_manual_run_over_http_blocks(self) -> None:
        with _FakeRunner(active=_active("run")) as runner:
            self.assertIsNotNone(guard.check(runner.api))

    def test_unreachable_runner_blocks(self) -> None:
        reason = guard.check(_closed_port_api(), timeout=2)
        self.assertIsNotNone(reason)
        self.assertIn("응답 없음", reason)

    def test_http_error_blocks(self) -> None:
        with _FakeRunner(status=500) as runner:
            self.assertIn("응답 없음", guard.check(runner.api, timeout=5))

    def test_garbage_body_blocks(self) -> None:
        with _FakeRunner(body="<html>proxy error</html>") as runner:
            self.assertIsNotNone(guard.check(runner.api, timeout=5))

    def test_slow_runner_blocks(self) -> None:
        with _FakeRunner(delay=2.0) as runner:
            self.assertIn("응답 없음", guard.check(runner.api, timeout=0.5))

    def test_cli_exit_codes_and_message(self) -> None:
        cases = (("idle", 0), ("completed", 0), ("cycle_reset", 3), ("cycle_normal", 3),
                 ("waiting_capture", 3))
        for phase, want in cases:
            with _FakeRunner(phase_at=lambda n, p=phase: p) as runner:
                done = subprocess.run(
                    [sys.executable, str(GUARD_PATH), "단계X"],
                    env={**os.environ, "TESTBED_RUNNER_API": runner.api},
                    capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
                )
            self.assertEqual(done.returncode, want, (phase, done.stderr))
            if want:
                self.assertIn("배포 보류(단계X 앞)", done.stderr)
                self.assertIn(phase, done.stderr)
            else:
                self.assertEqual(done.stderr, "")
        done = subprocess.run(
            [sys.executable, str(GUARD_PATH)],
            env={**os.environ, "TESTBED_RUNNER_API": _closed_port_api()},
            capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
        )
        self.assertEqual(done.returncode, 3)
        self.assertIn("응답 없음", done.stderr)

    def test_cli_holds_with_3_on_odd_bodies(self) -> None:
        for body in ('{"phase": ["idle"]}', '{"phase": {}}'):
            with _FakeRunner(body=body) as runner:
                done = subprocess.run(
                    [sys.executable, str(GUARD_PATH)],
                    env={**os.environ, "TESTBED_RUNNER_API": runner.api},
                    capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
                )
            self.assertEqual(done.returncode, 3, (body, done.stderr))
            self.assertNotIn("Traceback", done.stderr)
        # 덤프 대기에서 멈춘 paused 는 CLI 도 3.
        with _FakeRunner(phase_at=lambda n: "paused",
                         queue_extra={"paused_from_phase": "waiting_capture"}) as runner:
            done = subprocess.run(
                [sys.executable, str(GUARD_PATH), "단계Y"],
                env={**os.environ, "TESTBED_RUNNER_API": runner.api},
                capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
            )
        self.assertEqual(done.returncode, 3, done.stderr)
        self.assertIn("덤프 대기(일시 정지", done.stderr)

    def test_guard_makes_no_ssh(self) -> None:
        source = GUARD_PATH.read_text(encoding="utf-8")
        self.assertNotIn("subprocess", source)
        self.assertNotIn("ssh ", source.split('"""', 2)[2])


FAKE_SSH = textwrap.dedent("""\
    #!/usr/bin/env bash
    # 원격 명령을 기록한다. 진짜 ssh 처럼 -n 이 없으면 표준 입력을 끝까지 읽는다(원격으로 넘기는 몫,
    # man ssh: -n prevents reading from stdin). while read 고리 안에서 -n 없이 부르면 고리의 남은 줄을 먹는다.
    printf 'REMOTE ssh %s\\n' "$*" >> "$FAKE_LOG"
    read_stdin=1
    for a in "$@"; do case "$a" in -n) read_stdin=0 ;; -*) ;; *) break ;; esac; done
    if (( read_stdin )); then cat > /dev/null; fi
    case "$*" in
      *"docker image inspect"*)
        if [[ -n "${FAKE_MISSING_IMAGE:-}" && "$*" == *"$FAKE_MISSING_IMAGE"* ]]; then exit 1; fi
        exit 0 ;;
      *md5sum*)
        all="$*"; p=${all##*md5sum }; p=${p%%\\'*}
        case "$p" in
          /opt/loadgen/loadgen_monitor.py) local_file=$FAKE_REPO/scripts/scenarios/profiles/loadgen_monitor.py ;;
          /opt/loadgen/baseline-publish.sh) local_file=$FAKE_REPO/scripts/loadgen/baseline-publish.sh ;;
          *) rest=${p#/opt/loadgen/}; local_file=$FAKE_REPO/${rest%%/*}/loadgen/${rest#*/} ;;
        esac
        if [[ "$p" == "${FAKE_MD5_WRONG:-}" ]]; then echo 00000000000000000000000000000000; exit 0; fi
        md5sum "$local_file" | cut -d' ' -f1 ;;
    esac
    exit 0
    """)
FAKE_RSYNC = textwrap.dedent("""\
    #!/usr/bin/env bash
    printf 'REMOTE rsync %s\\n' "$*" >> "$FAKE_LOG"
    """)


class DeployScriptReGuardTests(unittest.TestCase):
    """deploy-live-promotions.sh 원격 단계를 가짜 ssh, rsync, 러너 HTTP 로 돌린다.

    앞의 로컬 검사(unittest, 러너 pytest)는 이 시험 자신을 다시 부르므로 빼고, 머리(변수)와
    "원격 단계" 표시 뒤만 돌린다.
    """

    MARKER = "# ===== 원격 단계"

    @classmethod
    def setUpClass(cls) -> None:
        source = DEPLOY_PATH.read_text(encoding="utf-8")
        head = source[: source.index('cd "$repo_root"')]
        repo_line = 'repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)'
        assert repo_line in head
        head = head.replace(repo_line, f"repo_root={REPO}")
        cls.remote_part = head + source[source.index(cls.MARKER):]
        cls.tmp = tempfile.TemporaryDirectory()
        cls.bin = Path(cls.tmp.name) / "bin"
        cls.bin.mkdir()
        for name, body in (("ssh", FAKE_SSH), ("rsync", FAKE_RSYNC)):
            path = cls.bin / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)
        cls.script = Path(cls.tmp.name) / "remote-part.sh"
        cls.script.write_text(cls.remote_part, encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def _run(self, api: str, log: Path, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.script)],
            env={**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}",
                 "TESTBED_RUNNER_API": api, "FAKE_LOG": str(log), "FAKE_REPO": str(REPO),
                 "SCENARIO_REMOTE": "root@fake-runner", **env},
            capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL, cwd=REPO,
        )

    def _log(self, name: str) -> Path:
        path = Path(self.tmp.name) / name
        path.write_text("", encoding="utf-8")
        return path

    def test_every_remote_command_is_right_after_a_fresh_guard(self) -> None:
        log = self._log("all-idle.log")
        with _FakeRunner(log=log) as runner:
            done = self._run(runner.api, log)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        lines = log.read_text(encoding="utf-8").splitlines()
        remote_at = [i for i, line in enumerate(lines) if line.startswith("REMOTE ")]
        self.assertGreater(len(remote_at), 10)
        guard_reads = [l for l in lines if l.startswith("GET /api/live-queue ")]
        self.assertEqual(len(guard_reads) - 1, len(remote_at))  # 끝의 러너 HTTP 확인 한 번을 뺀다
        # 이미지 확인, 부하 스크립트 올리기, 대조, ClickHouse 확인, rsync 두 번이 모두 나간다.
        joined = "\n".join(lines)
        for needle in ("docker image inspect", "cat > /tmp/loadgen-publish.tgz", "md5sum",
                       "bash -s", "REMOTE rsync -az --exclude registry/controllers.json",
                       "registry/controllers.json root@fake-runner:"):
            self.assertIn(needle, joined)
        for i in remote_at:
            self.assertGreaterEqual(i, 2, lines[i])
            self.assertTrue(lines[i - 2].startswith("GET /api/live-queue "), (lines[i - 2], lines[i]))
            self.assertEqual(lines[i - 1], "GET /api/active", lines[i])
        # 끝 확인은 ssh 가 아니라 러너 HTTP 로 읽는다.
        self.assertEqual(lines[-2], "GET /api/live-queue/readiness")
        self.assertFalse(any("curl" in lines[i] for i in remote_at))
        self.assertIn('"ready": true', done.stdout)

    # 원격 명령 종류와 그 앞 판정의 단계 이름(배포 스크립트 recording_guard 인자).
    REMOTE_KINDS = (
        ("docker image inspect", "결함 이미지 확인"),
        ("cat > /tmp/loadgen-publish.tgz", "부하 스크립트 올리기"),
        ("md5sum", "부하 스크립트 대조"),
        ("bash -s", "러너 ClickHouse 자격 확인"),
        ("--exclude registry/controllers.json", "시나리오 rsync"),
        ("registry/controllers.json root@", "controllers.json rsync"),
        ("SOURCE_COMMIT.tmp", "SOURCE_COMMIT 표지"),
    )

    @classmethod
    def _kind(cls, line: str) -> str:
        for needle, stage in cls.REMOTE_KINDS:
            if needle in line:
                return stage
        raise AssertionError(f"모르는 원격 명령: {line}")

    def test_window_opening_mid_deploy_stops_before_the_next_remote_command(self) -> None:
        # 판정 순번 k 를 몇 곳으로 고르지 않고, 첫 실행 기록에서 원격 명령 종류마다 첫 판정과 마지막
        # 판정을 모두 고른다. 판정 결과를 버리는 꼴(|| true 등)이 어느 구간에 있어도 잡힌다(리뷰 M1).
        log = self._log("count.log")
        with _FakeRunner(log=log) as runner:
            self.assertEqual(self._run(runner.api, log).returncode, 0)
            guards = runner.queue_reads - 1  # 끝의 러너 HTTP 확인 한 번을 뺀다
        remote = [l for l in log.read_text(encoding="utf-8").splitlines() if l.startswith("REMOTE ")]
        self.assertEqual(len(remote), guards)  # 판정 하나에 원격 명령 하나
        kinds = [self._kind(l) for l in remote]
        self.assertEqual(list(dict.fromkeys(kinds)), [stage for _, stage in self.REMOTE_KINDS])
        picks: dict[int, str] = {}
        for k, kind in enumerate(kinds, start=1):
            if k == 1 or kinds[k - 2] != kind:
                picks[k] = kind
            if k == len(kinds) or kinds[k] != kind:
                picks[k] = kind
        for _, stage in self.REMOTE_KINDS:
            want = min(kinds.count(stage), 2)  # 여러 번이면 첫과 마지막, 한 번이면 그 하나
            self.assertEqual(list(picks.values()).count(stage), want, stage)
        self.assertGreater(kinds.count("결함 이미지 확인"), 1)
        self.assertGreater(kinds.count("부하 스크립트 대조"), 1)
        phases = ("cycle_reset", "cycle_normal")
        for n, (k, kind) in enumerate(sorted(picks.items())):
            phase = phases[n % 2]
            with self.subTest(k=k, kind=kind, phase=phase):
                log = self._log(f"switch-{k}.log")
                with _FakeRunner(phase_at=lambda i, k=k, p=phase: "idle" if i < k else p,
                                 log=log) as runner:
                    done = self._run(runner.api, log)
                self.assertEqual(done.returncode, 3, done.stderr[-2000:])
                self.assertIn(f"배포 보류({kind}", done.stderr)
                self.assertIn(phase, done.stderr)
                lines = log.read_text(encoding="utf-8").splitlines()
                opened = next(i for i, line in enumerate(lines)
                              if line.startswith(f"GET /api/live-queue {k} "))
                self.assertEqual([l for l in lines[opened:] if l.startswith("REMOTE ")], [])
                self.assertEqual(sum(l.startswith("REMOTE ") for l in lines[:opened]), k - 1)

    def test_window_opening_before_a_fault_image_build_stops_the_build(self) -> None:
        image = json.loads(sorted((ROOT / "fault-images").glob("*/image.json"))[0].read_text())
        log = self._log("build.log")
        with _FakeRunner(phase_at=lambda n: "idle" if n < 2 else "cycle_reset", log=log) as runner:
            done = self._run(runner.api, log, FAKE_MISSING_IMAGE=image["fault"])
        self.assertEqual(done.returncode, 3, done.stderr[-2000:])
        self.assertIn("결함 이미지 빌드", done.stderr)
        remote = [l for l in log.read_text(encoding="utf-8").splitlines() if l.startswith("REMOTE ")]
        self.assertEqual(len(remote), 1)
        self.assertIn("docker image inspect", remote[0])

    @staticmethod
    def _verify_paths() -> list[str]:
        """배포 스크립트가 대조하는 원격 경로, 스크립트와 같은 순서."""
        profiles = json.loads((ROOT / "registry" / "profiles.json").read_text())["profiles"]
        paths = sorted({p for profile in profiles.values()
                        for p in profile.get("parameter_contract", {}).get("allowed_script_paths", [])})
        paths += [f"/opt/loadgen/{d}/entrypoint.sh" for d in ("commerce", "core-banking", "food-delivery")]
        return paths + ["/opt/loadgen/loadgen_monitor.py", "/opt/loadgen/baseline-publish.sh"]

    def test_load_script_verify_checks_every_file(self) -> None:
        # ssh 가 -n 없이 while read 고리의 표준 입력을 먹으면 첫 파일만 대조하고 고리가 끝난다.
        paths = self._verify_paths()
        self.assertGreater(len(paths), 3)
        log = self._log("verify-all.log")
        with _FakeRunner(log=log) as runner:
            done = self._run(runner.api, log)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        checked = [l.rsplit("md5sum ", 1)[1].rstrip("'") for l in log.read_text(encoding="utf-8").splitlines()
                   if l.startswith("REMOTE ssh") and "md5sum" in l]
        self.assertEqual(checked, paths)

    def test_load_script_verify_catches_a_later_mismatch(self) -> None:
        paths = self._verify_paths()
        for wrong in (paths[1], paths[-1]):
            with self.subTest(wrong=wrong):
                log = self._log("verify-wrong.log")
                with _FakeRunner(log=log) as runner:
                    done = self._run(runner.api, log, FAKE_MD5_WRONG=wrong)
                self.assertEqual(done.returncode, 1, done.stderr[-2000:])
                self.assertIn(f"load script mismatch on tb-runner: {wrong}", done.stderr)
                self.assertNotIn("REMOTE rsync", log.read_text(encoding="utf-8"))

    def test_unreachable_runner_makes_no_remote_command(self) -> None:
        log = self._log("dead.log")
        done = self._run(_closed_port_api(), log)
        self.assertEqual(done.returncode, 3, done.stderr[-2000:])
        self.assertIn("응답 없음", done.stderr)
        self.assertNotIn("REMOTE ", log.read_text(encoding="utf-8"))


class DeployScriptLayoutTests(unittest.TestCase):
    def test_deploy_script_runs_guard_before_first_remote_write(self) -> None:
        script = DEPLOY_PATH.read_text(encoding="utf-8")
        guard_at = script.index("recording-guard.py")
        self.assertLess(guard_at, script.index("tar czf"))
        self.assertLess(guard_at, script.index("rsync -az"))

    def test_deploy_script_builds_missing_fault_images_after_guard(self) -> None:
        # 2026-10-11: 녹화로 미룬 배포에서 build.sh 가 빠져 F63-R 이미지가 없었다.
        script = DEPLOY_PATH.read_text(encoding="utf-8")
        build_at = script.index("fault-images/build.sh")
        self.assertLess(script.index("recording-guard.py"), build_at)
        self.assertLess(build_at, script.index("rsync -az"))

    def test_deploy_script_has_no_ssh_before_the_remote_marker(self) -> None:
        script = DEPLOY_PATH.read_text(encoding="utf-8")
        local = script[: script.index(DeployScriptReGuardTests.MARKER)]
        code = "\n".join(l for l in local.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn("ssh ", code)
        self.assertNotIn("rsync ", code)

    def test_final_runner_check_has_a_time_limit(self) -> None:
        # 리뷰 L3: 러너가 연결만 받고 답하지 않아도 배포가 끝나야 한다.
        script = DEPLOY_PATH.read_text(encoding="utf-8")
        final = [l for l in script.splitlines() if l.startswith("curl ")]
        self.assertEqual(len(final), 1)
        self.assertEqual(final[0].count("curl -fsS --max-time "), 2, final[0])

    def test_deploy_script_reads_guard_without_ssh(self) -> None:
        script = DEPLOY_PATH.read_text(encoding="utf-8")
        self.assertNotIn("curl -sS '$runner_url/api/live-queue'", script)
        self.assertNotIn("| python3 \"$scenario_root/recording-guard.py\"", script)


class SourceCommitMarkerTests(unittest.TestCase):
    """tb-ops 사본은 git 이 아니라서 배포가 커밋 표지를 남긴다(2026-11-18 v4 CAPMETA). 러너가 그 표지를 읽어
    녹화 meta.source_commits.testbed_services 에 싣는다."""

    def test_deploy_script_does_not_ship_the_unstamped_marker_and_writes_the_commit_after_rsync(self) -> None:
        script = (ROOT / "deploy-live-promotions.sh").read_text(encoding="utf-8")
        first_rsync = script.index("rsync -az")
        self.assertIn("--exclude SOURCE_COMMIT", script[first_rsync:script.index("\n", script.index("\n", first_rsync) + 1)])
        write_at = script.index("SOURCE_COMMIT.tmp")
        self.assertLess(script.index("recording-guard.py"), write_at)
        self.assertLess(script.rindex("rsync -az"), write_at)
        self.assertIn("git -C \"$repo_root\" rev-parse HEAD", script)
        self.assertIn('source_commit+="-dirty"', script)

    def test_git_archive_writes_the_archived_commit_into_the_marker(self) -> None:
        repo_root = ROOT.parents[1]
        marker = ROOT / "SOURCE_COMMIT"
        self.assertEqual(marker.read_text(encoding="utf-8"), "$Format:%H$\n")
        with tempfile.TemporaryDirectory() as raw:
            work = Path(raw)
            (work / "scripts" / "scenarios").mkdir(parents=True)
            shutil.copyfile(repo_root / ".gitattributes", work / ".gitattributes")
            shutil.copyfile(marker, work / "scripts" / "scenarios" / "SOURCE_COMMIT")

            def git(*args: str) -> str:
                return subprocess.run(
                    ["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                    check=True, capture_output=True, text=True,
                ).stdout

            git("init", "-q")
            git("add", "-A")
            git("commit", "-q", "-m", "marker")
            head = git("rev-parse", "HEAD").strip()
            archived = subprocess.run(
                ["git", "-C", str(work), "archive", "--format=tar", "HEAD", "scripts/scenarios/SOURCE_COMMIT"],
                check=True, capture_output=True,
            ).stdout
            with tarfile.open(fileobj=io.BytesIO(archived)) as tar:
                content = tar.extractfile("scripts/scenarios/SOURCE_COMMIT").read().decode()
            self.assertEqual(content, head + "\n")
            # 작업 트리 파일은 치환되지 않는다(그래서 rsync 배포는 표지를 따로 적는다).
            self.assertEqual((work / "scripts" / "scenarios" / "SOURCE_COMMIT").read_text(), "$Format:%H$\n")


if __name__ == "__main__":
    unittest.main()
