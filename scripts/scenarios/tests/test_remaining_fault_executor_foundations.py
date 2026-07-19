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


app_release = load("app_release_executor")
business = load("business_fault_executor")
network = load("network_fault_executor")
flap = load("timeline_flap_executor")
mock = load("mock_expectation_executor")


def plan(profile_id: str, scenario_id: str, params: dict, location: dict) -> dict:
    return {
        "scenario": {"id": scenario_id},
        "profile_instances": [{
            "profile_id": profile_id,
            "parameters": copy.deepcopy(params),
            "location": location,
        }],
    }


class RemainingFaultExecutorFoundationTests(unittest.TestCase):
    def test_app_release_foundation_is_digest_pinned_and_exactly_reversible(self) -> None:
        params = {
            "namespace": "rca-testbed-commerce",
            "deployment": "testbed-pricing",
            "container": "pricing-service",
            "baseline_image": "commerce-pricing:latest",
            "fault_image": "commerce-pricing@sha256:" + "a" * 64,
        }
        profile = {"scenario_parameters": {"F08-R": params}}
        app_release.validate("F08-R", params, profile)
        argv, stdin = app_release.build_invocation(plan(
            "app.release", "F08-R", params,
            {"transport": "kubectl", "namespace": "rca-testbed-commerce"},
        ), "cleanup")
        self.assertEqual(argv[0], "/usr/bin/bash")
        script = stdin.decode()
        self.assertIn('current >"$state.tmp"', script)
        self.assertIn('"$container=$fault"', script)
        self.assertIn('"$container=$original"', script)
        self.assertIn('[[ "$(current)" == "$original" ]]', script)
        mutable = dict(params, fault_image="commerce-pricing:broken")
        with self.assertRaisesRegex(app_release.ExecutorError, "immutable digest"):
            app_release.validate("F08-R", mutable, {"scenario_parameters": {"F08-R": mutable}})

    def test_app_release_scenarios_remain_blocked_without_verified_fault_artifacts(self) -> None:
        for scenario_id, reason in app_release.BLOCKED.items():
            with self.subTest(scenario_id=scenario_id):
                with self.assertRaisesRegex(app_release.ExecutorError, reason.split()[0]):
                    app_release.validate(scenario_id, {}, {})

    def test_business_semantic_faults_fail_closed_without_control_and_inverse_contracts(self) -> None:
        self.assertEqual(set(business.BLOCKED), {"F11-P", "F13-P", "F14-P"})
        for scenario_id in business.BLOCKED:
            with self.assertRaises(business.ExecutorError):
                business.validate(scenario_id, {}, {})
            with self.assertRaises(business.ExecutorError):
                business.build_invocation({"scenario": {"id": scenario_id}}, "run")

    def test_network_faults_remain_blocked_without_oob_recovery(self) -> None:
        self.assertEqual(set(network.BLOCKED), {"F07-R", "F12-P", "F12-R", "F12-G", "F13-H"})
        for scenario_id in network.BLOCKED:
            with self.assertRaises(network.ExecutorError):
                network.validate(scenario_id, {}, {})

    def test_f15r_flap_foundation_is_bounded_async_and_exactly_restored(self) -> None:
        params = {
            "namespace": "rca-testbed-commerce",
            "mock_resource": "deployment/testbed-external-pg-mock",
            "mock_path": "/v1/payments",
            "fault_status": 429,
            "episode_count": 2,
            "fault_hold_seconds": 60,
            "recovery_gap_seconds": 120,
        }
        profile = {"scenario_parameters": {"F15-R": params}}
        flap.validate("F15-R", params, profile)
        argv, stdin = flap.build_invocation(plan(
            "timeline.compose", "F15-R", params,
            {"transport": "local", "resolved": True},
        ), "run")
        self.assertEqual(argv[0], "/usr/bin/bash")
        script = stdin.decode()
        self.assertIn('(( episode == episodes )) || sleep "$recovery_gap"', script)
        self.assertIn('nohup bash "$worker"', script)
        self.assertIn('stop_worker; start_pf; restore', script)
        self.assertIn('[[ "$actual" == "$expected" ]]', script)
        too_fast = dict(params, recovery_gap_seconds=30)
        with self.assertRaisesRegex(flap.ExecutorError, "outside bounded"):
            flap.validate("F15-R", too_fast, {"scenario_parameters": {"F15-R": too_fast}})

    def test_unresolved_timeline_and_mock_business_paths_are_explicitly_not_promotable(self) -> None:
        self.assertEqual(
            set(flap.BLOCKED_TIMELINES),
            {"F08-G", "F14-R", "F15-H", "F15-G", "F15-T1", "F15-T2", "F15-T3", "F15-T4"},
        )
        profile = {
            "parameter_contract": {"allowed_scenarios": ["F01-H"]},
            "scenario_parameters": {"F01-H": {"path": "/v1/payments", "mode": "status", "status_code": 429, "delay_seconds": 0}},
        }
        for scenario_id in ("F06-P", "F07-G", "F14-G"):
            with self.assertRaisesRegex(mock.ExecutorError, "not allowlisted"):
                mock.validate(scenario_id, {}, profile)


if __name__ == "__main__":
    unittest.main()
