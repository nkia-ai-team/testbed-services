from __future__ import annotations

import hashlib
import fcntl
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("profile_control", ROOT / "profile-control.py")
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]):
        self.calls.append(list(argv))
        return module.Result(0)


class ProfileControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "contracts"
        self.root.mkdir()
        self.executor = self.root / "load.sh"
        self.executor.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.executor.chmod(0o755)
        self.now = datetime(2026, 7, 16, 12, tzinfo=timezone.utc)
        self.level_parameters = [
            {"target_rps": 60, "hold": "8m"},
            {"target_rps": 70, "hold": "8m"},
            {"target_rps": 80, "hold": "8m"},
        ]
        self.levels = [
            {"level_id": level_id, "parameters": parameters}
            for level_id, parameters in zip(
                ("healthy-ceiling-60", "knee-70", "measured-upper-80"),
                self.level_parameters,
            )
        ]
        self.plan = {
            "live_allowed": True,
            "plan_digest": "a" * 64,
            "scenario": {"id": "F07-H", "slug": "adaptive"},
            "profile_instances": [{
                "profile_id": "load.north_south",
                "executor": "load.sh",
                "executor_sha256": hashlib.sha256(self.executor.read_bytes()).hexdigest(),
                "parameters": self.level_parameters[-1],
                "approved_levels": self.levels,
            }],
        }
        self.coordinator = self.base / "coordinator.json"
        self.coordinator.write_text(json.dumps({
            "active_lease": {
                "run_id": "run-1", "scenario_id": "F07-H", "fencing_token": 7,
                "expires_at": (self.now + timedelta(minutes=5)).isoformat(),
            },
            "dirty_run": None,
        }), encoding="utf-8")
        self.runner = FakeRunner()
        self.controller = module.ProfileController(
            root=self.root,
            coordinator_state=self.coordinator,
            control_state=self.base / "profile-state.json",
            compile_plan=lambda slug: json.loads(json.dumps(self.plan)),
            runner=self.runner,
            clock=lambda: self.now,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def call(self, action: str, key: str, **kwargs):
        return self.controller.execute(
            slug="adaptive", profile_id="load.north_south", action=action,
            run_id="run-1", fencing_token=7, idempotency_key=key,
            digest="a" * 64, confirmation="LIVE:F07-H:" + "a" * 64,
            **kwargs,
        )

    def test_apply_remains_active_until_explicit_cleanup(self) -> None:
        parameters = module.ProfileController._canonical(self.level_parameters[0])
        applied = self.call(
            "apply", "apply:0", level_index=0, level_id="healthy-ceiling-60",
            parameters_json=parameters,
        )
        self.assertEqual(applied, {"applied_at": "2026-07-16T12:00:00Z"})
        self.assertEqual(len(self.runner.calls), 2)
        self.assertEqual([call[1] for call in self.runner.calls], ["preflight", "run"])
        self.assertEqual(json.loads(self.coordinator.read_text())["active_lease"]["run_id"], "run-1")

        cleaned = self.call("cleanup", "cleanup:run-1")
        self.assertTrue(cleaned["succeeded"])
        self.assertEqual(cleaned["effect_ended_at"], "2026-07-16T12:00:00Z")
        self.assertEqual(
            [call[1] for call in self.runner.calls],
            ["preflight", "run", "preflight", "cleanup", "recovery"],
        )

    def test_apply_timestamp_is_conservative_effect_start_not_process_completion(self) -> None:
        started = self.now

        def delayed(argv: Sequence[str]):
            if argv[1] == "run":
                self.now += timedelta(seconds=12)
            return module.Result(0)

        self.controller.runner = delayed
        applied = self.call(
            "apply", "apply:started", level_index=0,
            level_id="healthy-ceiling-60",
            parameters_json=module.ProfileController._canonical(self.level_parameters[0]),
        )
        self.assertEqual(applied["applied_at"], module.ProfileController._timestamp(started))

    def test_cleanup_claim_is_terminal_fence_for_new_apply(self) -> None:
        state = json.loads(self.coordinator.read_text())
        state["cleanup_claim"] = {
            "run_id": "run-1", "fencing_token": 7, "claimant": "watchdog"
        }
        self.coordinator.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(module.ControlError, "terminal fence"):
            self.call(
                "apply", "apply-after-claim", level_index=0,
                level_id="healthy-ceiling-60",
                parameters_json=module.ProfileController._canonical(self.level_parameters[0]),
            )
        self.assertEqual(self.runner.calls, [])

    def test_apply_is_idempotent_without_a_second_profile_invocation(self) -> None:
        raw = module.ProfileController._canonical(self.level_parameters[1])
        first = self.call(
            "apply", "apply:1", level_index=1, level_id="knee-70", parameters_json=raw
        )
        second = self.call(
            "apply", "apply:1", level_index=1, level_id="knee-70", parameters_json=raw
        )
        self.assertEqual(first, second)
        self.assertEqual(len(self.runner.calls), 2)
        with self.assertRaisesRegex(module.ControlError, "another request"):
            self.call(
                "apply", "apply:1", level_index=2, level_id="measured-upper-80",
                parameters_json=module.ProfileController._canonical(self.level_parameters[2]),
            )

    def test_apply_mutation_holds_terminal_fence_but_preflight_does_not(self) -> None:
        calls: list[list[str]] = []

        def lock_probe(argv: Sequence[str]):
            calls.append(list(argv))
            lock_path = self.coordinator.with_suffix(self.coordinator.suffix + ".lock")
            with lock_path.open("a+") as handle:
                if argv[1] == "preflight":
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                else:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return module.Result(0)

        self.controller.runner = lock_probe
        raw = module.ProfileController._canonical(self.level_parameters[0])
        self.call(
            "apply", "apply:heartbeat", level_index=0,
            level_id="healthy-ceiling-60", parameters_json=raw,
        )
        self.assertEqual([call[1] for call in calls], ["preflight", "run"])

    def test_arbitrary_level_and_noncanonical_parameters_are_rejected(self) -> None:
        with self.assertRaisesRegex(module.ControlError, "predeclared level"):
            self.call(
                "apply", "bad:level", level_index=1,
                level_id="knee-70",
                parameters_json=module.ProfileController._canonical({"target_rps": 71, "hold": "8m"}),
            )
        with self.assertRaisesRegex(module.ControlError, "canonical JSON"):
            self.call(
                "apply", "bad:json", level_index=1,
                level_id="knee-70",
                parameters_json='{"target_rps": 70, "hold": "8m"}',
            )
        self.assertEqual(self.runner.calls, [])

    def test_wrong_fence_or_expired_lease_is_rejected(self) -> None:
        raw = module.ProfileController._canonical(self.level_parameters[0])
        with self.assertRaisesRegex(module.ControlError, "fencing proof"):
            self.controller.execute(
                slug="adaptive", profile_id="load.north_south", action="apply",
                run_id="run-1", fencing_token=8, idempotency_key="wrong:fence",
                digest="a" * 64, confirmation="LIVE:F07-H:" + "a" * 64,
                level_index=0, parameters_json=raw,
            )
        state = json.loads(self.coordinator.read_text())
        state["active_lease"]["expires_at"] = (self.now - timedelta(seconds=1)).isoformat()
        self.coordinator.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(module.ControlError, "expired"):
            self.call(
                "apply", "expired", level_index=0, level_id="healthy-ceiling-60",
                parameters_json=raw,
            )

    def test_expired_lease_cleanup_requires_matching_watchdog_claim(self) -> None:
        state = json.loads(self.coordinator.read_text())
        state["active_lease"]["expires_at"] = (self.now - timedelta(seconds=1)).isoformat()
        self.coordinator.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(module.ControlError, "expired"):
            self.call("cleanup", "cleanup:unclaimed-expired")

        state["cleanup_claim"] = {
            "run_id": "run-1",
            "fencing_token": 7,
            "claimant": "watchdog-a",
            "claimed_at": self.now.isoformat(),
        }
        self.coordinator.write_text(json.dumps(state), encoding="utf-8")

        cleaned = self.call("cleanup", "cleanup:claimed-expired")
        self.assertTrue(cleaned["succeeded"])

    def test_cleanup_failure_is_persistently_idempotent_and_not_claimed_success(self) -> None:
        calls: list[list[str]] = []

        def failing_runner(argv: Sequence[str]):
            calls.append(list(argv))
            return module.Result(9, stderr="cleanup failed")

        self.controller.runner = failing_runner
        result = self.call("cleanup", "cleanup:failed")
        self.assertEqual(result, {
            "succeeded": False, "effect_ended_at": None, "reason": "cleanup failed"
        })
        again = self.call("cleanup", "cleanup:failed")
        self.assertEqual(again, result)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
