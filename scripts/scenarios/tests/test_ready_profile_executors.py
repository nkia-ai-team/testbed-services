from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / "profiles"
sys.path.insert(0, str(PROFILES))


def load(name: str):
    path = PROFILES / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


compiler_spec = importlib.util.spec_from_file_location("ready_compile", ROOT / "compile-plan.py")
assert compiler_spec and compiler_spec.loader
compiler = importlib.util.module_from_spec(compiler_spec)
compiler_spec.loader.exec_module(compiler)

db_lock = load("db_lock_executor")
mock = load("mock_expectation_executor")
k8s_patch = load("k8s_patch_executor")
k8s_lifecycle = load("k8s_lifecycle_executor")
cache = load("cache_control_executor")
timeline = load("timeline_compose_executor")
db_ddl = load("db_ddl_executor")
kafka_control = load("kafka_control_executor")
host_stress = load("host_stress_executor")


class ReadyProfileExecutorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.profiles = json.loads((ROOT / "registry" / "profiles.json").read_text())["profiles"]

    def assert_contract(self, module, slug: str, profile_id: str) -> str:
        plan = compiler.compile_plan(slug)
        instance = next(row for row in plan["profile_instances"] if row["profile_id"] == profile_id)
        module.validate(plan["scenario"]["id"], instance["parameters"], self.profiles[profile_id])
        argv, stdin = module.build_invocation(plan, "cleanup")
        self.assertEqual(argv[0], "/usr/bin/bash" if profile_id != "db.lock" else "/usr/bin/ssh")
        script = stdin.decode()
        self.assertNotIn("shell=True", script)
        return script

    def test_db_lock_uses_strict_ssh_and_tag_scoped_termination(self) -> None:
        plan = compiler.compile_plan("f01-r-pg-lock-checkout")
        argv, stdin = db_lock.build_invocation(plan, "cleanup")
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertIn("nkia@192.168.122.206", argv)
        script = stdin.decode()
        self.assertIn("application_name='$tag'", script)
        self.assertIn("pg_terminate_backend", script)
        self.assertNotIn("pkill", script)

    def test_mock_expectations_are_snapshotted_and_restored(self) -> None:
        script = self.assert_contract(mock, "f01-h-commerce-pg-429", "mock.expectation")
        self.assertIn("ACTIVE_EXPECTATIONS", script)
        self.assertIn('--data-binary "@$state"', script)
        self.assertIn("/root/tb-kubeconfig", script)
        plan = compiler.compile_plan("f06-r-commerce-external-hang")
        self.assertEqual(next(x for x in plan["profile_instances"] if x["profile_id"] == "mock.expectation")["parameters"]["delay_seconds"], 30)

    def test_transient_mock_pulses_are_bounded_consumption_proven_and_exactly_restored(self) -> None:
        plan = compiler.compile_plan("f06-g-transient-5xx-absorbed")
        instance = next(x for x in plan["profile_instances"] if x["profile_id"] == "mock.expectation")
        self.assertEqual(instance["location_id"], "commerce-mock")
        self.assertEqual(instance["parameters"], {
            "path": "/v1/payments", "mode": "transient_status", "status_code": 500,
            "remaining_times": 1, "ttl_seconds": 10,
            "pulse_interval_seconds": 15, "max_pulses": 32,
        })
        mock.validate("F06-G", instance["parameters"], self.profiles["mock.expectation"])
        _, script = mock.build_invocation(plan, "run")
        rendered = script.decode()
        self.assertIn('"times": {"remainingTimes": remaining, "unlimited": False}', rendered)
        self.assertIn('"timeToLive": {"timeUnit": "SECONDS", "timeToLive": ttl, "unlimited": False}', rendered)
        self.assertIn('recorded_before = request("/mockserver/retrieve?type=REQUESTS"', rendered)
        self.assertIn('if not matches:', rendered)
        self.assertIn('stop_worker', rendered)
        self.assertLess(rendered.index('stop_worker\n    start_pf; reset'), rendered.index('--data-binary "@$state"'))
        self.assertIn('[[ "$actual" == "$expected" ]]', rendered)

    def test_kubernetes_patch_has_fixed_target_and_original_value_snapshot(self) -> None:
        script = self.assert_contract(k8s_patch, "f09-p-inventory-cpu-throttle", "k8s.patch")
        self.assertIn('current >"$state.tmp"', script)
        self.assertIn('--limits="cpu=$original"', script)
        self.assertIn("testbed-inventory", " ".join(k8s_patch.build_invocation(compiler.compile_plan("f09-p-inventory-cpu-throttle"), "run")[0]))

    def test_kubernetes_patch_adaptive_ladder_accepts_only_exact_levels(self) -> None:
        profile = self.profiles["k8s.patch"]
        levels = profile["scenario_levels"]["F09-P"]
        self.assertEqual([level["level_id"] for level in levels], ["conservative-250m", "constrained-100m", "endpoint-50m"])
        self.assertEqual([level["parameters"]["fault_cpu_limit"] for level in levels], ["250m", "100m", "50m"])
        for level in levels:
            k8s_patch.validate("F09-P", level["parameters"], profile)
        instance = next(row for row in compiler.compile_plan("f09-p-inventory-cpu-throttle")["profile_instances"] if row["profile_id"] == "k8s.patch")
        self.assertEqual(instance["selected_level_id"], "endpoint-50m")
        self.assertEqual(instance["scenario_levels"], levels)
        tampered = dict(levels[1]["parameters"])
        tampered["fault_cpu_limit"] = "99m"
        with self.assertRaisesRegex(k8s_patch.ExecutorError, "predeclared"):
            k8s_patch.validate("F09-P", tampered, profile)

    def test_kubernetes_patch_level_override_changes_exact_builder_argv(self) -> None:
        plan = compiler.compile_plan("f09-p-inventory-cpu-throttle")
        instance = next(row for row in plan["profile_instances"] if row["profile_id"] == "k8s.patch")
        levels = instance["approved_levels"]
        canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"))
        low, low_id = sys.modules["executor_common"].bind_level_parameters(plan, "k8s.patch", 0, canonical(levels[0]["parameters"]))
        endpoint, endpoint_id = sys.modules["executor_common"].bind_level_parameters(plan, "k8s.patch", 2, canonical(levels[2]["parameters"]))
        low_argv, _ = k8s_patch.build_invocation(low, "run")
        endpoint_argv, _ = k8s_patch.build_invocation(endpoint, "run")
        self.assertEqual((low_id, endpoint_id), ("conservative-250m", "endpoint-50m"))
        self.assertIn("250m", low_argv)
        self.assertIn("50m", endpoint_argv)
        self.assertNotEqual(low_argv, endpoint_argv)
        tampered = dict(levels[0]["parameters"])
        tampered["fault_cpu_limit"] = "249m"
        with self.assertRaisesRegex(Exception, "predeclared"):
            sys.modules["executor_common"].bind_level_parameters(plan, "k8s.patch", 0, canonical(tampered))

    def test_f12h_patch_targets_product_and_restores_exact_cpu_limit(self) -> None:
        plan = compiler.compile_plan("f12-h-pod-cpu-network-lookalike")
        instance = next(row for row in plan["profile_instances"] if row["profile_id"] == "k8s.patch")
        self.assertEqual(
            [level["parameters"]["fault_cpu_limit"] for level in instance["approved_levels"]],
            ["250m", "100m", "50m"],
        )
        self.assertEqual(instance["parameters"]["deployment"], "testbed-product")
        self.assertEqual(instance["parameters"]["container"], "product-service")
        k8s_patch.validate("F12-H", instance["parameters"], self.profiles["k8s.patch"])
        argv, script = k8s_patch.build_invocation(plan, "cleanup")
        self.assertIn("testbed-product", argv)
        self.assertIn("product-service", argv)
        self.assertIn('--limits="cpu=$original"', script.decode())

    def test_lifecycle_has_fixed_target_snapshot_and_live_recovery(self) -> None:
        plan = compiler.compile_plan("f05-g-invalid-image-no-impact")
        self.assertTrue(plan["live_allowed"])
        self.assertEqual(plan["profile_instances"][0]["location_id"], "commerce-namespace")
        script = self.assert_contract(k8s_lifecycle, "f05-g-invalid-image-no-impact", "k8s.lifecycle")
        self.assertIn('current >"$state.tmp"', script)
        self.assertIn('"$container=$original"', script)

    def test_cache_cleanup_restores_snapshot_and_waits_for_dependents(self) -> None:
        script = self.assert_contract(cache, "f11-g-redis-down-absorbed", "cache.control")
        self.assertIn('--replicas="$original"', script)
        self.assertIn('rollout status deploy/"$dependent"', script)

    def test_composite_cleanup_is_reverse_order(self) -> None:
        script = self.assert_contract(timeline, "f08-h-rollout-plus-external-fault", "timeline.compose")
        cleanup = script[script.index("cleanup)"):script.index("recovery)")]
        self.assertLess(cleanup.index("reset_mock"), cleanup.index("restore_rollout"))
        self.assertIn("Reverse sub-injection order is mandatory", cleanup)

    def test_live_matrix_has_twenty_ready_plans_with_payment_faults(self) -> None:
        expected = {
            "F01-R", "F01-H", "F01-G", "F03-G", "F05-G", "F06-R",
            "F07-H", "F08-H", "F09-P", "F11-R", "F11-G", "F02-R", "F02-P", "F04-R", "F12-H", "F06-G", "F05-R", "F05-H", "F07-P", "F08-P", "F09-R",
            "F01-P", "F08-G", "F15-G", "F06-H", "F03-P", "F09-H", "F05-P", "F15-T1", "F17-R", "F15-R", "F03-H",
        }
        catalog = json.loads((ROOT / "catalog.json").read_text())
        actual = {row["id"] for row in catalog["scenarios"] if compiler.compile_plan(row["slug"])["live_allowed"]}
        self.assertEqual(actual, expected)
        plan = compiler.compile_plan("f12-h-pod-cpu-network-lookalike")
        self.assertTrue(plan["live_allowed"])
        self.assertEqual(plan["profile_instances"][0]["location_id"], "commerce-namespace")

    def test_f02r_and_f04r_use_exact_live_contracts(self) -> None:
        cases = [
            ("f02-r-pg-product-index-drop", "db.ddl", db_ddl, "tb-runner"),
            ("f04-r-commerce-shipping-consumer-stop", "kafka.control", kafka_control, "commerce-namespace"),
        ]
        for slug, profile_id, module, location_id in cases:
            plan = compiler.compile_plan(slug)
            self.assertTrue(plan["live_allowed"])
            instance = next(row for row in plan["profile_instances"] if row["profile_id"] == profile_id)
            self.assertEqual(instance["location_id"], location_id)
            self.assertEqual(instance["parameters"], module.CONTRACTS[plan["scenario"]["id"]])
            module.validate(plan["scenario"]["id"], instance["parameters"], self.profiles[profile_id])

    def test_new_adaptive_profiles_accept_only_predeclared_levels(self) -> None:
        cases = [
            ("f01-g-absorbed-pg-delay", "mock.expectation", mock),
            ("f11-r-redis-down-fallback-overload", "load.north_south", None),
            ("f09-r-worker-cpu-noisy-neighbor", "host.stress", host_stress),
        ]
        for slug, profile_id, module in cases:
            plan = compiler.compile_plan(slug)
            instance = next(
                row for row in plan["profile_instances"] if row["profile_id"] == profile_id
            )
            self.assertEqual(len(instance["approved_levels"]), 3)
            self.assertTrue(plan["live_allowed"])
            if module is not None:
                for level in instance["approved_levels"]:
                    module.validate(plan["scenario"]["id"], level["parameters"], self.profiles[profile_id])


if __name__ == "__main__":
    unittest.main()
