from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "profiles"))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


compiler = load_module("scenario_compile_plan_for_load", ROOT / "compile-plan.py")
executor = load_module(
    "load_north_south_executor", ROOT / "profiles" / "load_north_south_executor.py"
)


class NorthSouthExecutorTests(unittest.TestCase):
    @staticmethod
    def canonical(value) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    def test_live_supported_profiles_are_explicit(self) -> None:
        profiles = json.loads((ROOT / "registry" / "profiles.json").read_text())["profiles"]
        live = {profile_id for profile_id, profile in profiles.items() if profile["live_supported"]}
        self.assertEqual(
            live,
            {
                "db.lock", "mock.expectation", "load.north_south", "k8s.patch",
                "k8s.lifecycle", "cache.control", "timeline.compose", "db.ddl", "load.east_west",
                "kafka.control", "k8s.resource", "k8s.probe",
            },
        )

    def test_allowlisted_parameters_bind_capacity_profiles(self) -> None:
        profiles = json.loads((ROOT / "registry" / "profiles.json").read_text())["profiles"]
        profile = profiles["load.north_south"]
        expected = {"F07-H": 160, "F03-G": 35, "F06-G": 20, "F05-R": 35, "F05-H": 20}
        for scenario_id, target_rps in expected.items():
            params = profile["scenario_parameters"][scenario_id]
            executor.validate_parameters(scenario_id, params, profile)
            self.assertEqual(params["target_rps"], target_rps)
            self.assertEqual(params["entry_url"], "http://192.168.122.77:30080")
            self.assertEqual(params["script_path"], "/opt/loadgen/commerce/surge.js")

    def test_parameter_tampering_is_rejected(self) -> None:
        profiles = json.loads((ROOT / "registry" / "profiles.json").read_text())["profiles"]
        profile = profiles["load.north_south"]
        params = dict(profile["scenario_parameters"]["F07-H"])
        params["entry_url"] = "http://attacker.invalid"
        with self.assertRaisesRegex(executor.ExecutorError, "predeclared"):
            executor.validate_parameters("F07-H", params, profile)

    def test_f07h_capacity_ladder_is_exact_and_tamper_proof(self) -> None:
        profile = json.loads((ROOT / "registry" / "profiles.json").read_text())["profiles"]["load.north_south"]
        levels = profile["scenario_levels"]["F07-H"]
        self.assertEqual([level["level_id"] for level in levels], ["healthy-high-120", "knee-140", "overload-160"])
        self.assertEqual([level["parameters"]["target_rps"] for level in levels], [120, 140, 160])
        for level in levels:
            executor.validate_parameters("F07-H", level["parameters"], profile)
        plan = compiler.compile_plan("f07-h-north-south-surge")
        instance = executor.load_instance(plan)
        self.assertEqual(instance["selected_level_id"], "overload-160")
        self.assertEqual(instance["scenario_levels"], levels)
        tampered = dict(levels[1]["parameters"])
        tampered["target_rps"] = 69
        with self.assertRaisesRegex(executor.ExecutorError, "predeclared"):
            executor.validate_parameters("F07-H", tampered, profile)

    def test_level_override_changes_exact_builder_argv(self) -> None:
        plan = compiler.compile_plan("f07-h-north-south-surge")
        levels = executor.load_instance(plan)["approved_levels"]
        low, low_id = executor.bind_level_parameters(plan, "load.north_south", 0, self.canonical(levels[0]["parameters"]))
        knee, knee_id = executor.bind_level_parameters(plan, "load.north_south", 1, self.canonical(levels[1]["parameters"]))
        low_argv, _ = executor.build_invocation(low, "run")
        knee_argv, _ = executor.build_invocation(knee, "run")
        self.assertEqual((low_id, knee_id), ("healthy-high-120", "knee-140"))
        self.assertIn("120", low_argv)
        self.assertIn("140", knee_argv)
        self.assertNotEqual(low_argv, knee_argv)
        tampered = dict(levels[0]["parameters"])
        tampered["target_rps"] = 61
        with self.assertRaisesRegex(executor.ExecutorError, "predeclared"):
            executor.bind_level_parameters(plan, "load.north_south", 0, self.canonical(tampered))

    def test_fixed_load_profile_accepts_only_its_controller_bound_level(self) -> None:
        plan = compiler.compile_plan("f03-g-high-pool-usage-no-impact")
        instance = executor.load_instance(plan)
        params = instance["parameters"]
        bound, level_id = executor.bind_level_parameters(
            plan, "load.north_south", 0, self.canonical(params)
        )
        self.assertEqual(level_id, "approved-fixed-f03-g")
        self.assertEqual(executor.load_instance(bound)["parameters"], params)
        tampered = dict(params)
        tampered["target_rps"] += 1
        with self.assertRaisesRegex(executor.ExecutorError, "predeclared"):
            executor.bind_level_parameters(
                plan, "load.north_south", 0, self.canonical(tampered)
            )

    def test_command_builder_uses_strict_ssh_argv_and_scoped_cleanup(self) -> None:
        plan = compiler.compile_plan("f07-h-north-south-surge")
        argv, stdin = executor.build_invocation(plan, "cleanup")
        self.assertEqual(argv[0], "/usr/bin/ssh")
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertEqual(argv[argv.index("-i") + 1], "/root/.ssh/tb_key")
        self.assertIn("nkia@192.168.122.206", argv)
        self.assertNotIn("password", " ".join(argv).lower())
        script = stdin.decode()
        self.assertIn("/proc/[0-9]*", script)
        self.assertIn('prev" == "--tag"', script)
        self.assertNotIn("pkill", script)
        self.assertIn('systemctl is-active --quiet "$baseline_unit"', script)
        self.assertIn('--out "json=$samples"', script)
        self.assertIn('"entry_status": entry_status', script)
        self.assertIn('"checkout_5xx_rate": checkout_5xx_rate', script)
        self.assertIn("checkout_results", script)
        self.assertIn('"business_ok": entry_status in {200, 400, 409}', script)
        self.assertIn('rca-scenario-${safe_id}-live.json', script)

    def test_live_requires_exact_digest_and_confirmation_before_dispatch(self) -> None:
        plan = compiler.compile_plan("f07-h-north-south-surge")
        confirmation = executor.confirmation_for(plan)
        with self.assertRaisesRegex(executor.ExecutorError, "plan-digest"):
            executor.authorize_live(plan, None, confirmation)
        with self.assertRaisesRegex(executor.ExecutorError, "confirm"):
            executor.authorize_live(plan, plan["plan_digest"], "wrong")
        executor.authorize_live(plan, plan["plan_digest"], confirmation)

    def test_composite_plan_selects_load_profile_by_id(self) -> None:
        plan = compiler.compile_plan("f01-h-commerce-pg-429")
        instance = executor.load_instance(plan)
        self.assertEqual(instance["profile_id"], "load.north_south")
        self.assertEqual(instance["parameters"]["scenario_tag"], "scenario_id=F01-H")


if __name__ == "__main__":
    unittest.main()
