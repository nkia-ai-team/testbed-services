from __future__ import annotations

import copy
import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / "profiles"
sys.path.insert(0, str(PROFILES))


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, PROFILES / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dual = load("timeline_dual_fault_executor")


def plan(profile_id: str, scenario_id: str, params: dict, location: dict) -> dict:
    return {
        "scenario": {"id": scenario_id},
        "profile_instances": [{
            "profile_id": profile_id,
            "parameters": copy.deepcopy(params),
            "location": location,
        }],
    }


def canonical_params(memory: str = "576Mi") -> dict:
    return {
        "commerce_namespace": "rca-testbed-commerce",
        "pg_runner_host": "192.168.122.206",
        "pg_db_host": "192.168.122.77",
        "pg_db_port": 30432,
        "pg_db_name": "commerce",
        "pg_db_user": "commerce",
        "pg_application_name": "rca-F15-T1-inventory-lock",
        "pg_product_id": 1,
        "pg_hold_seconds": 600,
        "food_namespace": "rca-testbed-food",
        "food_deployment": "testbed-payment",
        "food_container": "payment-service",
        "food_baseline": {"limits": {"cpu": "500m", "memory": "1Gi"}, "requests": {"cpu": "200m", "memory": "512Mi"}},
        "food_fault": {"limits": {"cpu": "500m", "memory": memory}, "requests": {"cpu": "200m", "memory": "512Mi"}},
        "start_offset_seconds": 0,
    }


class TimelineDualFaultFoundationTests(unittest.TestCase):
    def test_f15t1_dual_fault_is_offset_zero_bounded_and_reverse_cleanup(self) -> None:
        params = canonical_params()
        profile = {"scenario_parameters": {"F15-T1": params}}
        dual.validate("F15-T1", params, profile)
        argv, stdin = dual.build_invocation(
            plan("timeline.compose", "F15-T1", params, {"transport": "local", "resolved": True}),
            "run",
        )
        self.assertEqual(argv[0], "/usr/bin/bash")
        script = stdin.decode()
        # exact-simultaneous: no wait branch may run when offset is 0
        self.assertIn('[[ "$offset" -eq 0 ]] || sleep "$offset"', script)
        # both roots injected together, PG first then food
        self.assertIn("pg run", script)
        self.assertIn('food_patch "$food_fault"', script)
        # reverse-order cleanup: food restored first, PG terminated second, fail-closed
        self.assertLess(script.index('food_patch "$original"'), script.index("pg cleanup"))
        self.assertIn("[[ $rc -eq 0 ]]", script)
        # no ambient shell injection
        self.assertNotIn("shell=True", script)

    def test_each_food_ladder_level_validates(self) -> None:
        for memory in ("768Mi", "640Mi", "576Mi"):
            params = canonical_params(memory)
            dual.validate("F15-T1", params, {"scenario_parameters": {"F15-T1": params}})

    def test_offset_must_be_zero(self) -> None:
        params = canonical_params()
        params["start_offset_seconds"] = 30
        with self.assertRaisesRegex(dual.ExecutorError, "exact-simultaneous"):
            dual.validate("F15-T1", params, {"scenario_parameters": {"F15-T1": params}})

    def test_food_fault_off_ladder_is_rejected(self) -> None:
        params = canonical_params("800Mi")
        with self.assertRaisesRegex(dual.ExecutorError, "measured F05-R ladder"):
            dual.validate("F15-T1", params, {"scenario_parameters": {"F15-T1": params}})

    def test_pg_must_run_through_tb_runner_nodeport(self) -> None:
        params = canonical_params()
        params["pg_db_host"] = "10.0.0.9"
        with self.assertRaisesRegex(dual.ExecutorError, "tb-runner against the NodePort"):
            dual.validate("F15-T1", params, {"scenario_parameters": {"F15-T1": params}})

    def test_session_tag_binds_scenario(self) -> None:
        params = canonical_params()
        params["pg_application_name"] = "rca-generic-lock"
        with self.assertRaisesRegex(dual.ExecutorError, "session tag"):
            dual.validate("F15-T1", params, {"scenario_parameters": {"F15-T1": params}})

    def test_other_composite_timelines_remain_blocked(self) -> None:
        self.assertEqual(
            set(dual.BLOCKED_TIMELINES),
            {"F08-G", "F14-R", "F15-H", "F15-G", "F15-T2", "F15-T3", "F15-T4"},
        )
        for scenario_id, reason in dual.BLOCKED_TIMELINES.items():
            with self.subTest(scenario_id=scenario_id):
                with self.assertRaises(dual.ExecutorError):
                    dual.validate(scenario_id, {}, {})
        # F15-R belongs to the flapping foundation, not this dual-fault one
        with self.assertRaisesRegex(dual.ExecutorError, "not allowlisted"):
            dual.validate("F15-R", {}, {})


if __name__ == "__main__":
    unittest.main()
