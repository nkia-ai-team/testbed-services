from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("compile_plan", ROOT / "compile-plan.py")
assert SPEC and SPEC.loader
compile_plan_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compile_plan_module)


class RegistryContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        (
            cls.catalog,
            cls.locations,
            cls.profiles,
            cls.queries,
            cls.controllers,
        ) = compile_plan_module.load_contracts()

    def test_f05g_rollout_uses_replica_catastrophic_gate_not_pod_ready(self) -> None:
        # F05-G의 의도된 invalid-image rollout은 새 replica를 지속 unready로 만들므로
        # pod_ready(전체 AND) abort는 셀프 트리거다. catastrophic 게이트는 서빙 전멸
        # (available_replicas==0)이어야 하고 success도 available_replicas를 봐야 한다.
        f05g = self.controllers["controllers"]["F05-G"]
        abort_pairs = {
            (item["observation"], item["op"], item["value"]) for item in f05g["abort"]["any"]
        }
        self.assertIn(("available_replicas", "eq", 0), abort_pairs)
        self.assertNotIn(("pod_ready", "eq", False), abort_pairs)
        success = {item["id"]: item for item in f05g["success"]["all"]}
        self.assertEqual(success["old-replica-ready"]["observation"], "available_replicas")
        self.assertEqual(success["old-replica-ready"]["op"], "gte")
        self.assertEqual(success["old-replica-ready"]["value"], 1)

    def test_abort_gates_are_user_impact_only(self) -> None:
        # catastrophic abort 게이트 재정의(2026-07-20): pod_ready는 "재난인가"와
        # "이 pod이 트래픽을 받나"의 의미 이중성 때문에 시나리오의 의도된
        # rollout·eviction·의존성 전파가 자기 abort를 트리거한 계보가 5건
        # (F08-H·F05-G·F09-P·F11-G·F11-R). abort는 사용자 영향 신호만 허용:
        # entry_status==0 필수, available_replicas==0 선택, pod_ready 금지.
        # pod_ready 제거로 8틱 우회(F09-P 등)도 불필요해져 2틱으로 정규화됐다.
        for scenario_id, controller in self.controllers["controllers"].items():
            abort_items = controller["abort"]["any"]
            observations = {item["observation"] for item in abort_items}
            self.assertNotIn("pod_ready", observations, scenario_id)
            self.assertIn(
                ("entry_status", "eq", 0),
                {(i["observation"], i["op"], i["value"]) for i in abort_items},
                scenario_id,
            )
            self.assertEqual(controller["abort"]["consecutive_ticks"], 2, scenario_id)

    def test_registry_closure_covers_64_scenarios_and_20_profiles(self) -> None:
        self.assertEqual(len(self.catalog["scenarios"]), 64)
        self.assertEqual(len(self.profiles["profiles"]), 20)
        known = set(self.profiles["profiles"])
        for scenario in self.catalog["scenarios"]:
            self.assertTrue(set(scenario["profiles"]) <= known)
            self.assertIn(scenario["injection_location"], self.locations["catalog_aliases"])

    def test_executor_paths_exist_and_hashes_are_bound_into_plans(self) -> None:
        for profile in self.profiles["profiles"].values():
            self.assertTrue((ROOT / profile["executor"]).is_file())
        plan = compile_plan_module.compile_plan("f07-h-north-south-surge")
        self.assertRegex(plan["profile_instances"][0]["executor_sha256"], r"^[0-9a-f]{64}$")

    def test_all_64_scenarios_compile_with_trusted_live_plans(self) -> None:
        live_ids = {
            "F01-R", "F01-H", "F01-G", "F03-G", "F05-G", "F06-R",
            "F07-H", "F08-H", "F09-P", "F11-R", "F11-G", "F02-R", "F04-R", "F12-H", "F06-G", "F05-R", "F05-H", "F07-P", "F08-P", "F09-R",
            "F01-P", "F08-G", "F15-G", "F06-H", "F03-P", "F09-H", "F05-P", "F15-T1", "F15-R",
        }
        for scenario in self.catalog["scenarios"]:
            plan = compile_plan_module.compile_plan(scenario["slug"])
            self.assertEqual(plan["scenario"]["id"], scenario["id"])
            self.assertFalse(plan["side_effects"])
            self.assertEqual(
                plan["live_allowed"],
                scenario["id"] in live_ids,
            )
            self.assertEqual(
                plan["cleanup_order"],
                [item["profile_id"] for item in reversed(plan["profile_instances"])],
            )

    def test_all_kubectl_locations_use_canonical_kubeconfig(self) -> None:
        for location in self.locations["locations"].values():
            if location["transport"] == "kubectl":
                self.assertEqual(location["kubeconfig"], "/root/tb-kubeconfig")

    def test_profile_location_allowlist_rejects_wrong_topology(self) -> None:
        with self.assertRaisesRegex(compile_plan_module.ContractError, "cannot use location"):
            compile_plan_module.validate_profile_location(
                "load.north_south", "commerce-namespace", self.profiles
            )

    def test_manifest_catalog_mismatch_is_rejected(self) -> None:
        scenario = self.catalog["scenarios"][0]
        manifest = json.loads(
            (ROOT / "manifests" / f"{scenario['slug']}.yaml").read_text(encoding="utf-8")
        )
        bad = copy.deepcopy(manifest)
        bad["injection"]["profile_refs"] = ["injector-profiles/unknown"]
        with self.assertRaisesRegex(compile_plan_module.ContractError, "injection mismatch"):
            compile_plan_module.validate_manifest(scenario, bad, self.controllers)

        bad = copy.deepcopy(manifest)
        bad["prerequisite_gate"]["live_allowed"] = not manifest["prerequisite_gate"]["live_allowed"]
        with self.assertRaisesRegex(compile_plan_module.ContractError, "prerequisite mismatch"):
            compile_plan_module.validate_manifest(scenario, bad, self.controllers)

    def test_raw_promql_is_rejected(self) -> None:
        bad = copy.deepcopy(self.queries)
        bad["queries"]["bad.raw"] = {
            "adapter": "prometheus", "promql": "up", "template_id": "bad"
        }
        with self.assertRaisesRegex(compile_plan_module.ContractError, "raw promql"):
            compile_plan_module.validate_contracts(
                self.catalog, self.locations, self.profiles, bad, self.controllers
            )
        with self.assertRaisesRegex(compile_plan_module.ContractError, "raw PromQL"):
            compile_plan_module.validate_observation_refs(
                [{"query_id": "prometheus.user_p95", "promql": "up"}], self.queries
            )

    def test_f07h_and_f03g_plans_are_deterministic_and_canonical(self) -> None:
        for slug in ("f07-h-north-south-surge", "f03-g-high-pool-usage-no-impact"):
            first = compile_plan_module.compile_plan(slug)
            second = compile_plan_module.compile_plan(slug)
            self.assertEqual(first, second)
            self.assertEqual(first["profile_instances"][0]["location_id"], "tb-runner")
            self.assertTrue(first["live_allowed"])
            self.assertRegex(first["plan_digest"], r"^[0-9a-f]{64}$")

    def test_live_manifests_have_complete_live_controllers(self) -> None:
        live_ids = set(self.controllers["live_scenario_ids"])
        self.assertEqual(
            live_ids,
            {
                "F01-R", "F01-H", "F01-G", "F03-G", "F05-G", "F06-R",
                "F07-H", "F08-H", "F09-P", "F11-R", "F11-G", "F02-R", "F04-R", "F12-H", "F06-G", "F05-R", "F05-H", "F07-P", "F08-P", "F09-R",
                "F01-P", "F08-G", "F15-G", "F06-H", "F03-P", "F09-H", "F05-P", "F15-T1", "F15-R",
            },
        )
        self.assertEqual(
            self.controllers["live_scenario_ids"],
            [
                "F01-R", "F01-H", "F03-G", "F06-R", "F07-H", "F08-H",
                "F09-P", "F11-G", "F01-G", "F05-G", "F11-R", "F02-R", "F04-R", "F12-H", "F06-G", "F05-R", "F05-H", "F07-P", "F08-P", "F09-R",
                "F01-P", "F08-G", "F15-G", "F06-H", "F03-P", "F09-H", "F05-P", "F15-T1", "F15-R",
            ],
        )
        for scenario in self.catalog["scenarios"]:
            manifest = json.loads(
                (ROOT / "manifests" / f"{scenario['slug']}.yaml").read_text(encoding="utf-8")
            )
            controller = manifest["execution"]["controller"]
            expected_live = scenario["id"] in live_ids
            self.assertEqual(controller["live_enabled"], expected_live)
            self.assertEqual(manifest["prerequisite_gate"]["live_allowed"], expected_live)
            self.assertEqual(compile_plan_module.compile_plan(scenario["slug"])["live_allowed"], expected_live)
            if expected_live:
                self.assertEqual(controller["binding"]["primary_ref"], scenario["profiles"][0])
                self.assertEqual(controller["binding"]["companion_refs"], scenario["profiles"][1:])
                self.assertEqual(controller["runtime"]["baseline"]["clean_window"], "30m")
                self.assertEqual(controller["runtime"]["capture"]["post_window"], "20m")
            else:
                self.assertNotIn("runtime", controller)
        f12h = compile_plan_module.compile_plan("f12-h-pod-cpu-network-lookalike")
        self.assertTrue(f12h["live_allowed"])
        self.assertEqual(f12h["profile_instances"][0]["location_id"], "commerce-namespace")

    def test_adaptive_ladders_and_fixed_profiles_are_registry_bound(self) -> None:
        controllers = self.controllers["controllers"]
        self.assertEqual(
            [level["parameters"]["target_rps"] for level in controllers["F07-H"]["profile"]["levels"]],
            [120, 140, 160],
        )
        self.assertEqual(
            [level["parameters"]["fault_cpu_limit"] for level in controllers["F09-P"]["profile"]["levels"]],
            ["250m", "100m", "50m"],
        )
        self.assertEqual(
            [level["parameters"]["fault_cpu_limit"] for level in controllers["F12-H"]["profile"]["levels"]],
            ["250m", "100m", "50m"],
        )
        for scenario_id, controller in controllers.items():
            if controller["mode"] == "evaluation":
                self.assertEqual(len(controller["profile"]["levels"]), 1)
                self.assertEqual(
                    controller["profile"]["approved_profile_id"],
                    controller["profile"]["primary_ref"],
                )

    def test_f12h_fixed_observations_prove_cpu_not_network_lookalike(self) -> None:
        controller = self.controllers["controllers"]["F12-H"]
        observations = {item["id"]: item for item in controller["observations"]}
        self.assertEqual(
            observations["product_p95"]["parameters"],
            {"service_name": "commerce-product"},
        )
        self.assertEqual(
            observations["order_p95"]["parameters"],
            {"service_name": "commerce-order"},
        )
        self.assertEqual(
            observations["cpu_throttled_time"]["query_id"],
            "prometheus.container_cpu_throttled_time",
        )
        self.assertEqual(
            observations["network_error_rate"]["query_id"],
            "prometheus.pod_network_error_rate",
        )
        self.assertEqual(
            observations["cpu_limit"]["query_id"],
            "kubernetes.deployment_container_cpu_limit",
        )
        recovery = {item["id"]: item for item in controller["recovery"]["all"]}
        self.assertEqual(recovery["cpu-limit-restored"]["value"], "500m")
        success = {item["id"]: item for item in controller["success"]["all"]}
        self.assertEqual(success["product-impact-visible"]["value"], 500)
        self.assertEqual(success["unrelated-service-stable"]["value"], 200)
        self.assertEqual(success["cpu-throttle-direct"]["op"], "gt")

    def test_f06g_transient_retry_contract_is_fixed_and_fail_closed(self) -> None:
        plan = compile_plan_module.compile_plan("f06-g-transient-5xx-absorbed")
        self.assertTrue(plan["live_allowed"])
        self.assertEqual(plan["profile_instances"][0]["location_id"], "commerce-mock")
        controller = self.controllers["controllers"]["F06-G"]
        self.assertEqual(controller["profile"]["levels"][0]["parameters"], {
            "path": "/v1/payments", "mode": "transient_status", "status_code": 500,
            "remaining_times": 1, "ttl_seconds": 10,
            "pulse_interval_seconds": 15, "max_pulses": 32,
        })
        observations = {item["id"]: item for item in controller["observations"]}
        self.assertEqual(
            observations["payment_duplicate_order_count_since_t1"]["query_id"],
            "database.payment_duplicate_order_count_since_t1",
        )
        self.assertEqual(
            observations["duplicate_expectation_count"]["query_id"],
            "mock.duplicate_expectation_count",
        )
        success = {item["observation"]: item for item in controller["success"]["all"]}
        self.assertEqual(success["achieved_rps"]["value"], 15)
        self.assertEqual(success["checkout_5xx_rate"]["value"], 0)
        self.assertEqual(success["business_ok"]["value"], True)
        self.assertEqual(success["transient_consumed_count"]["value"], 1)
        self.assertEqual(success["payment_duplicate_order_count_since_t1"]["value"], 0)
        recovery = {item["observation"]: item for item in controller["recovery"]["all"]}
        self.assertEqual(recovery["expectation_absent"]["value"], True)
        self.assertEqual(recovery["snapshot_restored"]["value"], True)

    def test_f05_payment_faults_are_exact_and_causally_distinct(self) -> None:
        f05r = self.controllers["controllers"]["F05-R"]
        self.assertEqual(
            [level["parameters"]["fault"]["limits"]["memory"] for level in f05r["profile"]["levels"]],
            ["768Mi", "640Mi", "576Mi"],
        )
        self.assertEqual(f05r["profile"]["levels"][0]["parameters"]["baseline"]["limits"]["memory"], "1Gi")
        r_success = {item["observation"]: item for item in f05r["success"]["all"]}
        self.assertEqual(r_success["termination_reason"]["value"], "OOMKilled")
        self.assertEqual(r_success["achieved_rps"]["value"], 30)
        self.assertEqual(f05r["capture"]["post_window"], "20m")
        self.assertFalse(f05r["capture"]["create_golden_anomaly"])

        f05h = self.controllers["controllers"]["F05-H"]
        parameters = f05h["profile"]["levels"][0]["parameters"]
        self.assertEqual(parameters["baseline"]["httpGet"], {
            "path": "/actuator/health", "port": 8083, "scheme": "HTTP",
        })
        self.assertEqual(parameters["fault"]["httpGet"]["path"], "/actuator/health/f05-h-fail")
        h_success = {item["observation"]: item for item in f05h["success"]["all"]}
        self.assertEqual(h_success["termination_reason"]["value"], "Error")
        self.assertEqual(h_success["restart_count"]["value"], 2)
        self.assertEqual(self.profiles["profiles"]["load.north_south"]["scenario_parameters"]["F05-H"]["target_rps"], 20)
        self.assertEqual(self.controllers["live_scenario_ids"][-2:], ["F15-T1", "F15-R"])

    def test_every_live_primary_plan_binds_controller_levels_for_runtime_apply(self) -> None:
        catalog_by_id = {item["id"]: item for item in self.catalog["scenarios"]}
        for scenario_id, controller in self.controllers["controllers"].items():
            plan = compile_plan_module.compile_plan(catalog_by_id[scenario_id]["slug"])
            primary = controller["profile"]["primary_ref"]
            instance = next(
                item for item in plan["profile_instances"] if item["profile_id"] == primary
            )
            expected = [
                {"level_id": level["id"], "parameters": level["parameters"]}
                for level in controller["profile"]["levels"]
            ]
            self.assertEqual(instance["approved_levels"], expected)
            self.assertIsNotNone(instance["selected_level_id"])

    def test_controller_registry_is_bound_into_registry_and_plan_digest(self) -> None:
        baseline = compile_plan_module.compile_plan("f07-h-north-south-surge")
        changed = copy.deepcopy(self.controllers)
        changed["controllers"]["F07-H"]["tick_interval"] = "10s"
        self.assertNotEqual(
            baseline["registry_digest"],
            compile_plan_module._digest({
                "locations": self.locations,
                "profiles": self.profiles,
                "queries": self.queries,
                "controllers": changed,
            }),
        )

    def test_each_profile_supports_all_plan_actions_and_refuses_live(self) -> None:
        required = self.profiles["required_actions"]
        for profile_id, profile in self.profiles["profiles"].items():
            executor = ROOT / profile["executor"]
            for action in required:
                completed = subprocess.run(
                    [str(executor), action], check=True, text=True, capture_output=True
                )
                body = json.loads(completed.stdout)
                self.assertFalse(body["side_effects"])
                self.assertEqual(body["profile_id"], profile_id)
                self.assertEqual(body["action"], action)
            refused = subprocess.run(
                [str(executor), "run", "--live"], text=True, capture_output=True
            )
            self.assertEqual(refused.returncode, 3)


if __name__ == "__main__":
    unittest.main()
