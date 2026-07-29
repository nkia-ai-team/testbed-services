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
        "pg_access": "in-cluster-pod",
        "pg_db_pod": "testbed-postgres-0",
        "pg_service": "testbed-postgres",
        "pg_secret": "postgres-secret",
        "pg_image": "postgres:16-alpine",
        "pg_schema": "inventory_schema",
        "pg_table": "inventory",
        "pg_key_column": "product_id",
        "pg_key_value": "1",
        "pg_client_identity": "PostgreSQL JDBC Driver",
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
        # G6/L2: app identity, idle-in-transaction hold, in-cluster origin
        self.assertIn("PostgreSQL JDBC Driver", argv)
        self.assertNotIn("pg_sleep", script)
        self.assertNotIn("pg_terminate_backend", script)
        self.assertIn("idle in transaction", script)
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

    def test_pg_lock_must_originate_inside_the_cluster(self) -> None:
        # Injecting from tb-runner leaves that host's NAT address in the capture,
        # which points at the answer (charter G6/L2, appendix A-1).
        params = canonical_params()
        params["pg_access"] = "tb-runner-nodeport"
        with self.assertRaisesRegex(dual.ExecutorError, "inside the cluster"):
            dual.validate("F15-T1", params, {"scenario_parameters": {"F15-T1": params}})

    def test_session_identity_must_impersonate_the_application(self) -> None:
        # The opposite of the old rule: encoding the scenario in the session
        # identity is exactly the L1 leak the charter now forbids.
        params = canonical_params()
        params["pg_client_identity"] = "rca-F15-T1-inventory-lock"
        with self.assertRaisesRegex(dual.ExecutorError, "impersonate the real application"):
            dual.validate("F15-T1", params, {"scenario_parameters": {"F15-T1": params}})

    def test_other_composite_timelines_remain_blocked(self) -> None:
        # F15-H/F15-T2 left this set on 2026-07-29: they route to
        # timeline_lock_mock_executor now. Their old block reasons ("food dispatch
        # baseline/recovery") were leftovers of the discarded dispatch-503 reading
        # of their food arm — food's 429 comes from the external PG mock, not from
        # food's own capacity path, and that surface was already proven by F06-P.
        self.assertEqual(
            set(dual.BLOCKED_TIMELINES),
            {"F08-G", "F14-R", "F15-G", "F15-T3", "F15-T4"},
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
