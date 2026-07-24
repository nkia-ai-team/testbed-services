from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "trusted_dispatcher", ROOT / "trusted_dispatcher.py"
)
assert SPEC and SPEC.loader
dispatcher_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dispatcher_module)

COMPILER_SPEC = importlib.util.spec_from_file_location(
    "dispatcher_test_compile_plan", ROOT / "compile-plan.py"
)
assert COMPILER_SPEC and COMPILER_SPEC.loader
compile_plan_module = importlib.util.module_from_spec(COMPILER_SPEC)
COMPILER_SPEC.loader.exec_module(compile_plan_module)


class FakeInvoker:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.failures: dict[tuple[str, str], int] = {}
        self.on_call = None

    def __call__(self, argv: Sequence[str]):
        call = list(argv)
        self.calls.append(call)
        if self.on_call:
            self.on_call(call)
        profile = Path(call[0]).name
        action = call[1]
        returncode = self.failures.get((profile, action), 0)
        return dispatcher_module.InvocationResult(
            returncode, stderr="injected failure" if returncode else ""
        )

    def actions(self) -> list[tuple[str, str]]:
        return [(Path(call[0]).name, call[1]) for call in self.calls]


class Harness:
    def __init__(self, temporary: str, profile_ids: list[str]) -> None:
        self.base = Path(temporary)
        self.root = self.base / "scenarios"
        self.root.mkdir()
        self.profile_ids = profile_ids
        self.instances = []
        for sequence, profile_id in enumerate(profile_ids):
            executor = self.root / f"{profile_id}.sh"
            executor.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            executor.chmod(0o755)
            self.instances.append(
                {
                    "sequence": sequence,
                    "profile_id": profile_id,
                    "executor": executor.name,
                    "executor_sha256": hashlib.sha256(executor.read_bytes()).hexdigest(),
                    "parameters": {"trusted": profile_id},
                }
            )
        self.plan = {
            "live_allowed": True,
            "plan_digest": "a" * 64,
            "scenario": {"id": "F99-H", "slug": "composite"},
            "profile_instances": self.instances,
        }
        self.invoker = FakeInvoker()
        self.dispatcher = dispatcher_module.Dispatcher(
            root=self.root,
            state_dir=self.base / "state",
            compile_plan=lambda slug: json.loads(json.dumps(self.plan)),
            invoker=self.invoker,
        )
        self.confirmation = "LIVE:F99-H:" + "a" * 64

    def dispatch(self, action: str = "run"):
        return self.dispatcher.dispatch(
            slug="composite",
            action=action,
            digest="a" * 64,
            confirmation=self.confirmation,
        )


class TrustedDispatcherTests(unittest.TestCase):
    def test_fixed_load_one_shot_is_accepted_without_external_commands(self) -> None:
        slug = "f01-r-pg-lock-checkout"
        with tempfile.TemporaryDirectory() as temporary:
            invoker = FakeInvoker()
            dispatcher = dispatcher_module.Dispatcher(
                state_dir=Path(temporary), invoker=invoker
            )
            plan = compile_plan_module.compile_plan(slug)
            result = dispatcher.dispatch(
                slug=slug, action="run", digest=plan["plan_digest"],
                confirmation=dispatcher.confirmation(plan),
            )
            self.assertEqual(result["cleanup"], "verified")
            self.assertEqual(
                invoker.actions(),
                [
                    ("db_lock_executor.py", "preflight"),
                    ("load_north_south_executor.py", "preflight"),
                    ("db_lock_executor.py", "run"),
                    ("load_north_south_executor.py", "run"),
                    ("load_north_south_executor.py", "cleanup"),
                    ("db_lock_executor.py", "cleanup"),
                    ("load_north_south_executor.py", "recovery"),
                    ("db_lock_executor.py", "recovery"),
                ],
            )

    def test_adaptive_load_refuses_one_shot_dispatch(self) -> None:
        slug = "f07-h-north-south-surge"
        with tempfile.TemporaryDirectory() as temporary:
            invoker = FakeInvoker()
            dispatcher = dispatcher_module.Dispatcher(
                state_dir=Path(temporary), invoker=invoker
            )
            plan = compile_plan_module.compile_plan(slug)
            with self.assertRaisesRegex(dispatcher_module.DispatchError, "profile-control"):
                dispatcher.dispatch(
                    slug=slug, action="run", digest=plan["plan_digest"],
                    confirmation=dispatcher.confirmation(plan),
                )
            self.assertEqual(invoker.calls, [])

    def test_every_current_live_plan_uses_the_same_generic_dispatch_contract(self) -> None:
        catalog, _, _, _, _ = compile_plan_module.load_contracts()
        live_slugs = [
            row["slug"]
            for row in catalog["scenarios"]
            if compile_plan_module.compile_plan(row["slug"])["live_allowed"]
            and row["load_mode"] != "adaptive"
        ]
        self.assertGreaterEqual(len(live_slugs), 2)
        for slug in live_slugs:
            with self.subTest(slug=slug), tempfile.TemporaryDirectory() as temporary:
                invoker = FakeInvoker()
                dispatcher = dispatcher_module.Dispatcher(
                    state_dir=Path(temporary), invoker=invoker
                )
                plan = compile_plan_module.compile_plan(slug)
                result = dispatcher.dispatch(
                    slug=slug,
                    action="run",
                    digest=plan["plan_digest"],
                    confirmation=dispatcher.confirmation(plan),
                )
                profile_count = len(plan["profile_instances"])
                actions = [action for _, action in invoker.actions()]
                self.assertEqual(actions[:profile_count], ["preflight"] * profile_count)
                self.assertEqual(actions[profile_count:2 * profile_count], ["run"] * profile_count)
                self.assertEqual(actions[2 * profile_count:3 * profile_count], ["cleanup"] * profile_count)
                self.assertEqual(actions[3 * profile_count:], ["recovery"] * profile_count)
                self.assertEqual(result["profiles"], [
                    row["profile_id"] for row in plan["profile_instances"]
                ])

    def test_digest_and_confirmation_tampering_are_rejected_before_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = Harness(temporary, ["alpha"])
            with self.assertRaisesRegex(dispatcher_module.DispatchError, "plan-digest"):
                harness.dispatcher.dispatch(
                    slug="composite",
                    action="run",
                    digest="b" * 64,
                    confirmation=harness.confirmation,
                )
            with self.assertRaisesRegex(dispatcher_module.DispatchError, "confirm"):
                harness.dispatcher.dispatch(
                    slug="composite",
                    action="run",
                    digest="a" * 64,
                    confirmation="LIVE:F99-H:forged",
                )
            self.assertEqual(harness.invoker.calls, [])

    def test_generic_composite_runs_in_order_and_cleans_in_reverse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = Harness(temporary, ["alpha", "beta", "gamma"])
            result = harness.dispatch()
            self.assertEqual(result["profiles"], ["alpha", "beta", "gamma"])
            self.assertEqual(
                harness.invoker.actions(),
                [
                    ("alpha.sh", "preflight"),
                    ("beta.sh", "preflight"),
                    ("gamma.sh", "preflight"),
                    ("alpha.sh", "run"),
                    ("beta.sh", "run"),
                    ("gamma.sh", "run"),
                    ("gamma.sh", "cleanup"),
                    ("beta.sh", "cleanup"),
                    ("alpha.sh", "cleanup"),
                    ("gamma.sh", "recovery"),
                    ("beta.sh", "recovery"),
                    ("alpha.sh", "recovery"),
                ],
            )
            state = json.loads(harness.dispatcher.state_path.read_text())
            self.assertEqual(state["status"], "clean")
            self.assertEqual(state["fence"], 1)

    def test_partial_run_failure_rolls_back_only_applied_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = Harness(temporary, ["alpha", "beta", "gamma"])
            harness.invoker.failures[("beta.sh", "run")] = 9
            with self.assertRaisesRegex(dispatcher_module.DispatchError, "beta run failed"):
                harness.dispatch()
            self.assertEqual(
                harness.invoker.actions()[-2:],
                [("alpha.sh", "cleanup"), ("alpha.sh", "recovery")],
            )
            self.assertEqual(
                json.loads(harness.dispatcher.state_path.read_text())["status"], "clean"
            )

    def test_cleanup_failure_persists_dirty_and_explicit_cleanup_recovers_same_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = Harness(temporary, ["alpha", "beta"])
            harness.invoker.failures[("alpha.sh", "cleanup")] = 7
            with self.assertRaisesRegex(dispatcher_module.DispatchError, "cleanup/recovery"):
                harness.dispatch()
            state = json.loads(harness.dispatcher.state_path.read_text())
            self.assertEqual(state["status"], "dirty")
            self.assertEqual(state["applied"], ["alpha", "beta"])
            with self.assertRaisesRegex(dispatcher_module.DispatchError, "dirty"):
                harness.dispatch()

            harness.invoker.failures.clear()
            harness.invoker.failures[("alpha.sh", "preflight")] = 6
            result = harness.dispatch("cleanup")
            self.assertEqual(result["status"], "recovered")
            self.assertEqual(len(result["preflight_warnings"]), 1)
            final = json.loads(harness.dispatcher.state_path.read_text())
            self.assertEqual(final["status"], "clean")
            self.assertEqual(final["fence"], 2)

    def test_active_or_locked_dispatcher_blocks_concurrent_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = Harness(temporary, ["alpha"])
            harness.dispatcher.state_dir.mkdir(parents=True)
            lock = harness.dispatcher.lock_path.open("a+")
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with self.assertRaisesRegex(dispatcher_module.DispatchError, "global lease"):
                    harness.dispatch()
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                lock.close()

            harness.dispatcher._write_state(
                {
                    "schema_version": "1.0",
                    "status": "active",
                    "fence": 4,
                    "scenario": "composite",
                }
            )
            with self.assertRaisesRegex(dispatcher_module.DispatchError, "active"):
                harness.dispatch()

    def test_executor_hash_drift_between_preflight_and_run_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = Harness(temporary, ["alpha"])

            def drift(call: list[str]) -> None:
                if call[1] == "preflight":
                    Path(call[0]).write_text("#!/usr/bin/env bash\nexit 8\n")

            harness.invoker.on_call = drift
            with self.assertRaisesRegex(dispatcher_module.DispatchError, "hash drift"):
                harness.dispatch()
            self.assertEqual(
                json.loads(harness.dispatcher.state_path.read_text())["status"], "clean"
            )


if __name__ == "__main__":
    unittest.main()
