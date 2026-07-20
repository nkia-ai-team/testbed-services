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


pricing_release = load("app_release_traffic_executor")
east_west = load("load_east_west_executor")
external = load("network_external_probe_executor")
queries = load("observation_query_candidates")
mock_business = load("mock_business_executor")
invariants = load("business_invariant_observer")


def plan(profile_id: str, scenario_id: str, params: dict, location: dict) -> dict:
    return {
        "scenario": {"id": scenario_id},
        "profile_instances": [{"profile_id": profile_id, "parameters": copy.deepcopy(params), "location": location}],
    }


class PriorityExecutorCandidateTests(unittest.TestCase):
    def test_f08r_and_f14h_use_unique_shadow_release_and_exact_selector_restore(self) -> None:
        for scenario_id, params in pricing_release.CONTRACTS.items():
            with self.subTest(scenario_id=scenario_id):
                pricing_release.validate(scenario_id, params, {})
                argv, stdin = pricing_release.build_invocation(plan(
                    "app.release", scenario_id, params,
                    {"transport": "kubectl", "namespace": "rca-testbed-commerce"},
                ), "cleanup")
                self.assertEqual(argv[0], "/usr/bin/bash")
                script = stdin.decode()
                self.assertIn('nodeName: $node', script)
                self.assertIn('imagePullPolicy: Never', script)
                self.assertIn('selector >"$state.tmp"', script)
                self.assertIn('restore_selector; original_ready', script)
                self.assertIn('resources_absent; rm -f "$state"', script)
                self.assertNotEqual(params["fault_selector"], params["baseline_selector"])

    def test_f07p_bulkhead_job_is_bounded_namespace_local_and_cleanup_scoped(self) -> None:
        self.assertEqual(set(east_west.F07P_LEVELS), {20, 35, 50})
        for rps, params in east_west.F07P_LEVELS.items():
            east_west.validate("F07-P", params, {})
            self.assertEqual(params["target_rps"], rps)
            self.assertEqual(params["node_name"], "tb-w1")
            self.assertEqual(params["max_vus"], 64)
        params = east_west.F07P_LEVELS[50]
        _, stdin = east_west.build_invocation(plan(
            "load.east_west", "F07-P", params,
            {"transport": "kubectl", "namespace": "rca-testbed-commerce"},
        ), "run")
        script = stdin.decode()
        self.assertIn("executor: 'constant-arrival-rate'", script)
        self.assertIn("Array.from({length:16}", script)
        self.assertIn('delete job "$job" --ignore-not-found --wait=true', script)
        self.assertIn('delete configmap "$configmap" --ignore-not-found --wait=true', script)

    def test_f03h_thread_pool_saturation_targets_the_db_free_render_surface(self) -> None:
        # 07-20 승격: order-service에 DB 비접촉 slow 렌더 엔드포인트가 신설되어
        # 기존 "no slow read-only order endpoint" 차단 사유가 해소되었다.
        self.assertEqual(set(east_west.F03H_LEVELS), {30, 45, 60})
        for rps, params in east_west.F03H_LEVELS.items():
            east_west.validate("F03-H", params, {})
            self.assertEqual(params["target_rps"], rps)
            self.assertIn("/api/orders/reports/render?delayMs=5000", params["target_url"])
            self.assertGreaterEqual(params["max_vus"], rps * 5)
        _, stdin = east_west.build_invocation(plan(
            "load.east_west", "F03-H", east_west.F03H_LEVELS[60],
            {"transport": "kubectl", "namespace": "rca-testbed-commerce"},
        ), "run")
        script = stdin.decode()
        self.assertIn("'$method' === 'GET' ? http.get('$target', params)", script)
        self.assertIn('rollout status deploy/"$target_deploy"', script)
        with self.assertRaisesRegex(east_west.ExecutorError, "not allowlisted"):
            east_west.validate("F14-H", {}, {})
        with self.assertRaisesRegex(east_west.ExecutorError, "exactly match"):
            east_west.validate("F03-H", east_west.F07P_LEVELS[20], {})

    def test_external_f12g_flow_is_bounded_and_password_is_not_embedded(self) -> None:
        external.validate("F12-G", external.CONTRACT, {})
        _, stdin = external.build_invocation(plan(
            "network.fault", "F12-G", external.CONTRACT,
            {"transport": "ssh", "host": "192.168.200.57"},
        ), "run")
        script = stdin.decode()
        self.assertIn("sshpass -e ssh", script)
        self.assertIn('[[ -n "${SSHPASS:-}"', script)
        self.assertIn("timeout ${duration}s nping", script)
        self.assertIn("/proc/$pid/cmdline", script)
        self.assertIn("ip route add '$route_prefix' via '$gateway' dev '$interface' proto static", script)
        self.assertIn('iptables -I FORWARD 1', script)
        self.assertIn('iptables -D FORWARD', script)
        self.assertIn('route_absent; rule_absent', script)
        self.assertIn('rm -f "$state/pid" "$state/rule-added" "$state/route-added"', script)
        self.assertNotIn("rm -rf", script)
        self.assertNotIn("tc qdisc", script)
        self.assertNotIn("iptables -P", script)
        self.assertNotIn("Cloud!!25", script)

    def test_mock_business_contracts_are_exact_and_f06p_is_capacity_gated(self) -> None:
        locations = {
            "F06-P": {"namespace": "rca-testbed-food", "resource": "deployment/testbed-external-pg-mock"},
            "F14-G": {"namespace": "rca-testbed-commerce", "resource": "deployment/testbed-external-pg-mock"},
        }
        for scenario_id, params in mock_business.CONTRACTS.items():
            with self.subTest(scenario_id=scenario_id):
                mock_business.validate(scenario_id, params, {})
                _, stdin = mock_business.build_invocation(plan(
                    "mock.expectation", scenario_id, params, locations[scenario_id],
                ), "preflight")
                script = stdin.decode()
                self.assertIn("food_capacity_ready", script)
                self.assertIn("service/testbed-dispatch", script)
                self.assertIn("snapshot_restored", script)
        self.assertEqual(mock_business.CONTRACTS["F06-P"]["remaining_times"], 5)
        self.assertEqual(mock_business.CONTRACTS["F06-P"]["ttl_seconds"], 4)
        self.assertEqual(mock_business.CONTRACTS["F06-P"]["pulse_interval_seconds"], 5)
        with self.assertRaisesRegex(mock_business.ExecutorError, "successful fallback"):
            mock_business.validate("F07-G", {}, {})

    def test_business_invariant_helpers_require_final_consistency(self) -> None:
        self.assertTrue(invariants.inventory_restored({1: 10}, {1: 10}))
        self.assertFalse(invariants.inventory_restored({1: 10}, {1: 9}))
        self.assertTrue(invariants.order_terminal_consistent(["COMPLETED", "FAILED"]))
        self.assertFalse(invariants.order_terminal_consistent(["COMPLETED", "PENDING"]))
        self.assertTrue(invariants.food_status_mix({"APPROVED": 2, "FAILED": 1}))
        self.assertFalse(invariants.food_status_mix({"FAILED": 1}))

    def test_candidate_queries_are_fixed_read_only_ids_without_raw_commands(self) -> None:
        required = {
            "F15-R": {"scenario.mock_flap_episode", "scenario.mock_flap_fault_active", "business.order_duplicate_count_since_t1"},
            "F07-P": {"kubernetes.job_active", "prometheus.apm_service_request_rate", "prometheus.apm_service_error_rate"},
            "F08-R": {"kubernetes.service_selector_matches", "business.pricing_quote_canonical_delta"},
            "F14-H": {"kubernetes.service_selector_matches", "business.pricing_quote_canonical_delta", "business.order_duplicate_count_since_t1"},
            "F06-P": {"business.food_payment_status_mix_since_t1"},
            "F14-G": {"business.inventory_balance_restored_since_t1", "business.order_terminal_consistency_since_t1"},
        }
        for scenario_id, expected in required.items():
            self.assertEqual(set(queries.SCENARIO_QUERY_IDS[scenario_id]), expected)
        for query in queries.QUERY_CANDIDATES.values():
            self.assertNotIn("command", query)
            self.assertNotIn("promql", query)
            self.assertIn(query["adapter"], {"kubernetes", "business_probe", "database", "prometheus"})


if __name__ == "__main__":
    unittest.main()
