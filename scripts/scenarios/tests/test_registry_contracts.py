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
        self.assertEqual(len(self.catalog["scenarios"]), 60)
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

    def test_all_scenarios_compile_with_trusted_live_plans(self) -> None:
        # 32 live of 60. 2026-07-27 골든 감사로 8종이 빠졌고(F01-G, F02-P, F03-G,
        # F03-H, F05-G, F09-H, F09-P, F11-G) 컨트롤러는 controllers-parked.json으로
        # 옮겼다. 2026-07-28 F03-H가 복귀했다 — 주입 표면을 자백하던 Thread.sleep을
        # 실제 결함(직렬화된 O(n^2) 리포트 렌더러)으로 교체해 G6 누설을 없앴다.
        live_ids = {
            "F01-H", "F01-P", "F01-R", "F03-P", "F04-R", "F05-H", "F05-P", "F05-R",
            "F06-H", "F06-R", "F07-H", "F07-P", "F08-G", "F08-H", "F08-P", "F09-R",
            "F11-R", "F12-H", "F15-G", "F15-R", "F15-T1", "F16-H", "F17-R", "F18-P",
            "F19-P", "F19-S", "F20-P", "F20-Q", "F20-R", "F23-R", "F25-H",
            "F03-H", "F06-P",
            # 2026-07-28: 스토리지 포화 3종 + 복합 자원 고갈. 셋을 막고 있던 것은
            # fio 부재·약한 고정 계약·디스크 IO 관측 부재였고 모두 해소됐다.
            "F02-H", "F10-H", "F10-P", "F15-P",
            "F21-P", "F21-Q",
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

    def test_success_conditions_ask_only_whether_damage_occurred(self) -> None:
        # Quality charter G3 / audit §3-2·§3-3, enforced structurally on
        # 2026-07-28. success must certify damage; three signal shapes cannot:
        #
        #   http.entry_health    — the last single checkout sample, not a rate.
        #                          An "entry_status >= 500" gate only fires when
        #                          the blast radius is 100%. Kept for abort's
        #                          "== 0" (unreachable) and for veto conditions.
        #   loadgen.achieved_rps — the load we ourselves drove. Asserting it is
        #                          reading back the injection, so it belongs in
        #                          must_rule_out as a guard ("load never flowed").
        #   kubernetes.pod_ready — either structurally true (the injection does
        #                          not touch the pod) or a read-back (it does).
        #                          As a discriminator it inverts into a veto.
        banned = {
            "http.entry_health",
            "loadgen.achieved_rps",
            "kubernetes.pod_ready",
        }
        for scenario_id, controller in self.controllers["controllers"].items():
            query_by_observation = {
                item["id"]: item["query_id"] for item in controller["observations"]
            }
            for condition in controller["success"]["all"]:
                query_id = query_by_observation[condition["observation"]]
                self.assertNotIn(
                    query_id, banned, f"{scenario_id}:{condition['id']}"
                )
            self.assertTrue(controller["success"]["all"], scenario_id)

    def test_no_veto_condition_is_stated_twice(self) -> None:
        # Demoting a success condition into must_rule_out can collide with a veto
        # that already says the same thing; a duplicate would double-count toward
        # the streak that aborts the run.
        for scenario_id, controller in self.controllers["controllers"].items():
            signatures = [
                (item["observation"], item["op"], json.dumps(item["value"]))
                for item in controller["must_rule_out"]["any"]
            ]
            self.assertEqual(len(signatures), len(set(signatures)), scenario_id)

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

    def test_f07h_and_f01r_plans_are_deterministic_and_canonical(self) -> None:
        for slug in ("f07-h-north-south-surge", "f01-r-pg-lock-checkout"):
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
                "F01-H", "F01-P", "F01-R", "F03-P", "F04-R", "F05-H", "F05-P", "F05-R",
                "F06-H", "F06-R", "F07-H", "F07-P", "F08-G", "F08-H", "F08-P", "F09-R",
                "F11-R", "F12-H", "F15-G", "F15-R", "F15-T1", "F16-H", "F17-R", "F18-P",
                "F19-P", "F19-S", "F20-P", "F20-Q", "F20-R", "F23-R", "F25-H",
                "F03-H", "F06-P",
                "F02-H", "F10-H", "F10-P", "F15-P",
                "F21-P", "F21-Q",
            },
        )
        self.assertEqual(
            self.controllers["live_scenario_ids"],
            [
                "F01-R", "F01-H", "F06-R", "F07-H", "F08-H", "F11-R", "F04-R", "F12-H",
                "F05-R", "F05-H", "F07-P", "F08-P", "F09-R", "F01-P", "F08-G", "F15-G",
                "F06-H", "F03-P", "F05-P", "F15-T1", "F17-R", "F18-P", "F19-P", "F19-S",
                "F16-H", "F20-R", "F20-P", "F20-Q", "F25-H", "F23-R", "F15-R",
                # 2026-07-28 복귀: 앱의 Thread.sleep 자백을 실제 결함으로 교체했다.
                "F03-H",
                # 2026-07-28 신규: food 429 경로는 앱에 이미 완결돼 있었고,
                # 막고 있던 것은 429를 세는 관측(business_429_rate) 부재였다.
                "F06-P",
                # 2026-07-28 신규: 스토리지 포화 3종. 마지막 차단은 도구도 계약도
                # 아니라 "장치가 바쁘다"를 셀 수 없다는 것이었다(host.disk_io_utilization).
                "F02-H", "F10-H", "F10-P",
                # 2026-07-28 신규: 복합 자원 고갈. 배치 고정으로 원 전제(공용 노드)가
                # 사라져 CPU+메모리 동시 압박으로 재정의했다.
                "F15-P",
                # 2026-07-28 신규: Tomcat 스레드풀 포화 짝. busy-thread 신호를
                # non-daemon에서 daemon으로 바로잡고서야 볼 수 있게 됐다.
                "F21-Q", "F21-P",
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
        # F09-P's ladder assertion moved to the parked archive on 2026-07-27
        # (parked: its three success rules are all self-fulfilling and the
        # throttling signal it needs does not exist). F12-H below keeps the same
        # ladder under live coverage.
        parked = json.loads((ROOT / "registry" / "controllers-parked.json").read_text())
        self.assertNotIn("F09-P", controllers)
        self.assertEqual(
            [level["parameters"]["fault_cpu_limit"]
             for level in parked["controllers"]["F09-P"]["profile"]["levels"]],
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
        self.assertEqual(success["cpu-throttle-direct"]["op"], "gt")
        # "the unrelated service stays fast" and "no network errors" are what
        # separate a CPU fault from a network lookalike, but neither is damage,
        # so both are veto conditions rather than success conditions (2026-07-28).
        #
        # The unrelated service must actually be unrelated. This veto was wired
        # to commerce-order, which sits directly upstream of product and *must*
        # slow down when product is throttled — the scenario would have vetoed
        # itself. commerce-pricing calls nobody, so its p95 rising really does
        # mean the cause is broader than one container (2026-07-28).
        rule_out = {item["id"]: item for item in controller["must_rule_out"]["any"]}
        self.assertEqual(rule_out["cross-service-overload"]["observation"], "pricing_p95")
        self.assertEqual(rule_out["cross-service-overload"]["value"], 300)
        self.assertEqual(rule_out["network-fault-alternative"]["op"], "gt")

    def test_f05_payment_faults_are_exact_and_causally_distinct(self) -> None:
        f05r = self.controllers["controllers"]["F05-R"]
        self.assertEqual(
            [level["parameters"]["fault"]["limits"]["memory"] for level in f05r["profile"]["levels"]],
            ["768Mi", "640Mi", "576Mi"],
        )
        self.assertEqual(f05r["profile"]["levels"][0]["parameters"]["baseline"]["limits"]["memory"], "1Gi")
        r_success = {item["observation"]: item for item in f05r["success"]["all"]}
        self.assertEqual(r_success["termination_reason"]["value"], "OOMKilled")
        # achieved_rps is a guard ("load actually flowed"), not damage — it vetoes
        # the run from must_rule_out instead of certifying it from success.
        r_rule_out = {item["observation"]: item for item in f05r["must_rule_out"]["any"]}
        self.assertEqual((r_rule_out["achieved_rps"]["op"], r_rule_out["achieved_rps"]["value"]), ("lt", 30))
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
        self.assertEqual(self.controllers["live_scenario_ids"][-4:],
                         ["F10-P", "F15-P", "F21-Q", "F21-P"])

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

    def test_latency_thresholds_are_stated_in_the_metric_unit(self) -> None:
        # prometheus.apm_service_p95 reads apm.agent.otel.java.percentile95, which
        # the Polestar APM agent publishes in MILLISECONDS — live-verified against
        # VictoriaMetrics 119:18428 on 2026-07-28 (24h: commerce-order p50 92,
        # core-banking-transfer p50 707, max 49,600; seconds would be 13 hours).
        #
        # F20-P/Q/R were authored in seconds (1.5 / 3.0 / 0.8). Against a
        # millisecond metric those gates are 1000x too low, and they fail in both
        # directions at once: success clears at idle baseline, so the scenario
        # certifies damage that never happened (charter G3), while recovery
        # demands a latency the service never reaches even when healthy, so the
        # cleanup gate can only time out.
        #
        # No live service idles under 50ms at p95, so a threshold below that is a
        # unit error rather than a strict gate.
        floor = 50
        for scenario_id, controller in self.controllers["controllers"].items():
            query_by_observation = {
                item["id"]: item["query_id"] for item in controller["observations"]
            }
            for section in ("success", "escalate", "must_rule_out", "recovery", "abort"):
                block = controller.get(section) or {}
                for condition in block.get("all", []) + block.get("any", []):
                    query_id = query_by_observation[condition["observation"]]
                    if query_id != "prometheus.apm_service_p95":
                        continue
                    self.assertGreaterEqual(
                        condition["value"],
                        floor,
                        f"{scenario_id}:{section}:{condition['id']} looks like seconds",
                    )

    def test_host_stress_observes_the_node_it_actually_injects(self) -> None:
        # 2026-07-28: 배치 고정에 맞춰 좌표를 갱신할 때 profiles.json의 주입 host는
        # 고쳤으나 컨트롤러 observation의 node= 파라미터는 그대로였다. 그 결과 F09-R은
        # tb-w1을 때리면서 tb-w3의 CPU를 재고 있었다 — success(node_cpu_util>=85)는
        # 유휴 노드라 영원히 서지 않고, 동시에 must_rule_out(node_cpu_util<50)이 즉시
        # 발화하는 자기-거부 구조다. F05-P도 동형이었다.
        #
        # 주입 좌표와 관측 좌표가 따로 노는 한 이 사고는 배치가 바뀔 때마다 재발한다.
        # locations.json이 alias→(host, node)를 함께 들고 있으므로 여기서 묶어 강제한다.
        host_to_node = {
            location["host"]: location["node"]
            for location in self.locations["locations"].values()
            if "node" in location and "host" in location
        }
        self.assertTrue(host_to_node, "worker locations must bind a k8s node name")

        scenario_parameters = self.profiles["profiles"]["host.stress"]["scenario_parameters"]
        parked = json.loads(
            (ROOT / "registry" / "controllers-parked.json").read_text(encoding="utf-8")
        )["controllers"]
        every_controller = {**parked, **self.controllers["controllers"]}

        for scenario_id, parameters in sorted(scenario_parameters.items()):
            controller = every_controller.get(scenario_id)
            if controller is None:
                continue
            expected_node = host_to_node.get(parameters["host"])
            self.assertIsNotNone(
                expected_node, f"{scenario_id} injects an unmapped host {parameters['host']}"
            )
            for observation in controller["observations"]:
                node = (observation.get("parameters") or {}).get("node")
                if node is None:
                    continue
                self.assertEqual(
                    node,
                    expected_node,
                    f"{scenario_id}.{observation['id']} observes {node} "
                    f"but the injection lands on {expected_node}",
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
