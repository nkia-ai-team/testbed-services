from __future__ import annotations

import copy
import importlib.util
import json
import re
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

    # Counts stay out of the name: it already read "64 scenarios" while asserting
    # 60, which is how the stale pins of 2026-07-29 got past review.
    def test_registry_closure_covers_catalog_scenarios_and_profiles(self) -> None:
        self.assertEqual(len(self.catalog["scenarios"]), 60)
        self.assertEqual(len(self.profiles["profiles"]), 22)
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
            "F06-H", "F06-R", "F07-H", "F08-G", "F08-H", "F08-P", "F09-R",
            "F11-R", "F12-H", "F15-G", "F15-R", "F15-T1", "F16-H", "F17-R", "F18-P",
            "F19-P", "F19-S", "F20-Q", "F20-R", "F23-R", "F25-H",
            "F03-H", "F06-P",
            # 2026-07-28: 스토리지 포화 3종 + 복합 자원 고갈. 셋을 막고 있던 것은
            # fio 부재·약한 고정 계약·디스크 IO 관측 부재였고 모두 해소됐다.
            # 2026-08-06: F02-H는 인과 부재 실측 확정, F21-P·F21-Q는 레버가
            # slow-not-failed를 못 만들어 병합 재설계 — 셋 다 parked로 이동(0804 #24·27·28).
            "F10-H", "F10-P", "F15-P",
            "F09-H", "F09-P", "F17-P",
            # 2026-07-28: relay 정지의 commerce 정합판. 스위치는 이미 앱에 있었고
            # 막고 있던 것은 commerce PG용 미발행 outbox 관측의 부재였다.
            "F04-H",
            # 2026-07-29: 두 도메인 동시/순차 복합. food arm의 429는 외부 PG mock에서
            # 오는 것이라 F06-P 표면 그대로이고, commerce arm은 부하를 food에 내주므로
            # baseline만으로 측정되도록 테이블 락으로 규모를 맞췄다.
            "F15-H", "F15-T2",
            # 2026-07-29: 원장 테이블 READ ONLY. 필요하던 "catch-swallow injector"는
            # 앱에 심을 필요가 없었다 — 삼킴 + 자동 ack = 영구 유실이 이미 코드에 있고,
            # 없던 것은 그것을 발화시킬 쓰기 실패였다.
            "F14-P",
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

    def test_condition_ids_are_unique_within_every_condition_set(self) -> None:
        # The runner enforces id uniqueness per condition set; this registry only
        # checked (observation, op, value) signatures, so F16-H carried two
        # "reads-also-broken" vetoes at gte 0.3 and gte 0.1 — different signature,
        # same id. The manifest then failed to load at all, which took the whole
        # deploy path down with it (2026-07-29). Check what the runner checks.
        for scenario_id, controller in self.controllers["controllers"].items():
            for gate in ("success", "escalate", "abort", "must_rule_out", "recovery"):
                condition_set = controller.get(gate)
                if not isinstance(condition_set, dict):
                    continue
                for match in ("all", "any"):
                    items = condition_set.get(match)
                    if not isinstance(items, list):
                        continue
                    ids = [item["id"] for item in items if isinstance(item, dict) and "id" in item]
                    self.assertEqual(
                        len(ids), len(set(ids)), f"{scenario_id}.{gate}.{match}"
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

    def test_every_allowlisted_scenario_also_matches_its_tag_pattern(self) -> None:
        """한 계약 안의 두 목록이 갈라지면 주입도 정리도 거부된다.

        load.north_south 는 시나리오를 `allowed_scenarios` 로 한 번, 부하 태그를
        `tag_pattern` 으로 또 한 번 검사한다. 승격할 때 앞의 목록만 갱신되고
        정규식이 따라오지 않아, F06-P·F02-H·F10-H·F10-P·F15-H·F15-T2·F14-P 일곱이
        `scenario_tag is not allowlisted` 로 죽었다. 정리도 같은 검증을 거치므로
        런이 스스로 씻지 못하고 전역 DIRTY 가 된다(2026-08-03).
        """
        for profile_id, profile in self.profiles["profiles"].items():
            contract = profile.get("parameter_contract") or {}
            pattern = contract.get("tag_pattern")
            if not pattern:
                continue
            compiled = re.compile(pattern)
            unmatched = [
                scenario_id
                for scenario_id in contract.get("allowed_scenarios", [])
                if not compiled.fullmatch(f"scenario_id={scenario_id}")
            ]
            self.assertEqual(
                unmatched,
                [],
                f"{profile_id}: allowed_scenarios 에 있으나 tag_pattern 이 거부한다",
            )

    def test_every_pg_lock_executor_reads_the_pid_from_real_psql_output(self) -> None:
        """pid 추출은 실행기마다 따로 구현돼 있고, 같은 결함이 네 번 반복됐다.

        psql 은 결과보다 `BEGIN` 명령 태그를 먼저 찍으므로 첫 줄은 절대 pid 가 아니다.
        db_lock_executor(2026-07-31) → timeline_multi_injection(08-03) →
        timeline_lock_mock·timeline_dual_fault(08-03) 순으로 같은 자리를 네 번 고쳤다.
        파일마다 가드를 두는 대신 **pid 를 뽑는 모든 실행기**를 한 번에 검사한다.

        입력은 2026-07-31 에 파드에서 실측한 로그 그대로다.
        """
        observed_pod_log = "BEGIN\n35250\n1\n"
        checked = []
        for path in sorted((ROOT / "profiles").glob("*.py")):
            source = path.read_text(encoding="utf-8")
            if "did not report a backend pid" not in source:
                continue
            pipelines = [
                line.split('logs "$pod" 2>/dev/null |', 1)[1].rstrip(')"').strip()
                for line in source.splitlines()
                if 'logs "$pod" 2>/dev/null |' in line and 'pid="$(' in line
            ]
            self.assertTrue(pipelines, f"{path.name}: pid 추출 파이프라인을 찾지 못했다")
            for pipeline in pipelines:
                extracted = subprocess.run(
                    ["sh", "-c", f"cat | {pipeline}"],
                    input=observed_pod_log,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
                self.assertEqual(
                    extracted, "35250", f"{path.name}: 실제 psql 출력에서 pid 를 못 뽑는다"
                )
            checked.append(path.name)
        self.assertGreaterEqual(len(checked), 4, f"검사된 실행기가 너무 적다: {checked}")

    def test_executor_side_tables_cover_every_allowlisted_scenario(self) -> None:
        """실행기가 들고 있는 표도 레지스트리 목록과 같아야 한다.

        `k8s.env` 는 `profiles.json` 의 allowed_scenarios 로 한 번, 실행기 안의
        APPROVED_TARGETS / APPROVED_KEYS 로 또 한 번 검사한다. F04-H 는 2026-07-29
        승격 때 앞의 목록에만 들어가 `scenario environment target is not allowlisted`
        로 죽었고, 정리도 같은 검증을 지나므로 전역 DIRTY 가 됐다(2026-08-03).

        tag_pattern 짝(test_every_allowlisted_scenario_also_matches_its_tag_pattern)과
        같은 결함을 실행기 쪽에서 잡는다.
        """
        import ast

        spec = (ROOT / "profiles" / "k8s_env_executor.py").read_text(encoding="utf-8")
        tables: dict[str, object] = {}
        for node in ast.walk(ast.parse(spec)):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if getattr(target, "id", "") in {"APPROVED_TARGETS", "APPROVED_KEYS"}:
                        tables[target.id] = ast.literal_eval(node.value)
        allowed = (
            self.profiles["profiles"]["k8s.env"]
            .get("parameter_contract", {})
            .get("allowed_scenarios", [])
        )
        for name, table in sorted(tables.items()):
            self.assertEqual(
                [scenario_id for scenario_id in allowed if scenario_id not in table],
                [],
                f"k8s.env: allowed_scenarios 에 있으나 {name} 에 없다",
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
                "F06-H", "F06-R", "F07-H", "F08-G", "F08-H", "F08-P", "F09-R",
                "F11-R", "F12-H", "F15-G", "F15-R", "F15-T1", "F16-H", "F17-R", "F18-P",
                "F19-P", "F19-S", "F20-Q", "F20-R", "F23-R", "F25-H",
                "F03-H", "F06-P",
                "F10-H", "F10-P", "F15-P",
                "F09-H", "F09-P", "F17-P",
                "F04-H",
            # 2026-07-29: 두 도메인 동시/순차 복합. food arm의 429는 외부 PG mock에서
            # 오는 것이라 F06-P 표면 그대로이고, commerce arm은 부하를 food에 내주므로
            # baseline만으로 측정되도록 테이블 락으로 규모를 맞췄다.
            "F15-H", "F15-T2",
            # 2026-07-29: 원장 테이블 READ ONLY. 필요하던 "catch-swallow injector"는
            # 앱에 심을 필요가 없었다 — 삼킴 + 자동 ack = 영구 유실이 이미 코드에 있고,
            # 없던 것은 그것을 발화시킬 쓰기 실패였다.
            "F14-P",
            },
        )
        self.assertEqual(
            self.controllers["live_scenario_ids"],
            [
                "F01-R", "F01-H", "F06-R", "F07-H", "F08-H", "F11-R", "F04-R", "F12-H",
                "F05-R", "F05-H", "F08-P", "F09-R", "F01-P", "F08-G", "F15-G",
                "F06-H", "F03-P", "F05-P", "F15-T1", "F17-R", "F18-P", "F19-P", "F19-S",
                "F16-H", "F20-R", "F20-Q", "F25-H", "F23-R", "F15-R",
                # 2026-07-28 복귀: 앱의 Thread.sleep 자백을 실제 결함으로 교체했다.
                "F03-H",
                # 2026-07-28 신규: food 429 경로는 앱에 이미 완결돼 있었고,
                # 막고 있던 것은 429를 세는 관측(business_429_rate) 부재였다.
                "F06-P",
                # 2026-07-28 신규: 스토리지 포화 3종 — 2026-08-06 F02-H는 인과 부재
                # 실측 확정으로 parked(0804 #24).
                "F10-H", "F10-P",
                # 2026-07-28 신규: 복합 자원 고갈. 배치 고정으로 원 전제(공용 노드)가
                # 사라져 CPU+메모리 동시 압박으로 재정의했다.
                "F15-P",
                # 2026-07-28 신규였던 Tomcat 스레드풀 포화 짝(F21-Q·F21-P)은
                # 2026-08-06 parked — 무딘 레버가 slow-not-failed를 못 만든다(0804 #27~28).
                # 2026-07-28 복귀: 07-27에 parked된 두 건. success가 스스로 참인
                # 조건들로 채워져 있었고, 감별 신호는 없다고 판단됐지만 실은
                # 있었다 — GC는 재는 방법이, 스로틀은 대상이 없었을 뿐이다.
                "F09-H", "F09-P",
                # 2026-07-28: 배선 4항 중 둘은 이미 돼 있었고, 원장 역분개는
                # 주입 금액을 잔액의 0.3%로 낮춰 오염 자체를 없앴다.
                "F17-P",
                # 2026-07-28: relay 정지의 commerce 정합판(F18-P의 쌍). 앱은 손대지
                # 않았다 — OutboxRelay는 이미 @ConditionalOnProperty로 꺼지고,
                # 없던 것은 order_schema의 미발행 outbox를 세는 관측뿐이었다.
                "F04-H",
            # 2026-07-29: 두 도메인 동시/순차 복합. food arm의 429는 외부 PG mock에서
            # 오는 것이라 F06-P 표면 그대로이고, commerce arm은 부하를 food에 내주므로
            # baseline만으로 측정되도록 테이블 락으로 규모를 맞췄다.
            "F15-H", "F15-T2",
            # 2026-07-29: 원장 테이블 READ ONLY. 삼킴 + 자동 ack = 영구 유실은 이미
            # 코드에 있었고, 없던 것은 그것을 발화시킬 쓰기 실패였다.
            "F14-P",
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
        # F09-P returned to the live registry on 2026-07-28. The 07-27 audit
        # parked it for two reasons and both are now addressed: its success rules
        # were self-fulfilling (pod_ready and achieved_rps, since banned from
        # success outright) and the throttling signal was said not to exist. The
        # signal did exist — but the runner's PromQL template hardcoded
        # testbed-product, so it existed for F12-H and for nothing else. The
        # template is parameterized now and F09-P observes its own deployment.
        self.assertEqual(
            [level["parameters"]["fault_cpu_limit"]
             for level in controllers["F09-P"]["profile"]["levels"]],
            ["250m", "225m", "200m"],
        )
        # F12-H's floor was measured on the live pod (2026-08-04, 3 minutes per
        # rung under baseline load): 200m and 175m held Ready 12/12 with zero
        # restarts, 150m never became Ready and restarted twice. The old 100m/50m
        # rungs sat below pod survival, so the scenario's own discriminator
        # `product-pod-down` fired on its own injection.
        self.assertEqual(
            [level["parameters"]["fault_cpu_limit"] for level in controllers["F12-H"]["profile"]["levels"]],
            ["250m", "200m", "175m"],
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
            # 768Mi는 2026-08-07에 뺐다(run 95b07798) — 고정 힙 384m + 비힙 ~160MiB로
            # 바닥이 ~544MiB라 768Mi는 OOM이 구조적으로 불가능했고 승급만 하며 시간을 먹었다.
            ["640Mi", "576Mi"],
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
                         ["F04-H", "F15-H", "F15-T2", "F14-P"])

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

    def test_observation_parameters_stay_inside_what_the_query_allows(self) -> None:
        # 이 목록은 러너가 판정 시점에 집행한다. testbed 사본이 더 넓으면 여기서는
        # 통과하고 라이브에서 거부되고, 더 좁으면 관측이 조용히 파라미터를 잃는다.
        # 2026-08-04에 10개 loadgen 항목이 `domain`을 아예 선언하지 않은 채(관측 평면
        # 분리 때 러너에만 들어갔다) 갈라져 있었고, `_since_t1` 두 항목은 반대로
        # testbed 쪽이 러너에 없는 run_id·t1을 허용하고 있었다.
        queries = self.queries["queries"]
        for scenario_id, controller in self.controllers["controllers"].items():
            for observation in controller["observations"]:
                allowed = queries[observation["query_id"]].get("allowed_parameters")
                if allowed is None:
                    continue
                supplied = set(observation.get("parameters") or {})
                self.assertLessEqual(
                    supplied,
                    set(allowed),
                    f"{scenario_id}:{observation['id']} passes parameters "
                    f"{sorted(supplied - set(allowed))} that {observation['query_id']} does not allow",
                )

    def test_429_scenarios_stay_in_the_band_where_429_survives(self) -> None:
        # 이 앱들은 부하를 올리면 429를 5xx로 승격시킨다. 그래서 성공 조건(429 비율)과
        # 감별자(5xx 승격)가 부하 축에서 서로를 밀어낸다 — 부하를 올리는 행위 자체가
        # 관측하려던 신호를 지운다. 서로 다른 두 시나리오가 같은 형태로 실패했다(배치 #22).
        #
        # 실측 진행(2026-08-03):
        #   F06-P   3.6rps → 429 0.75 / 5xx 0.00      12.1rps → 0.18 / 0.71
        #   F15-H   3.8rps → 429 0.80 / 5xx 0.00      17.8rps → 0.10 / 0.84
        #
        # 정답은 저부하 구간에 있었고 설계된 40rps가 그 위를 지나쳐 버렸다. 성공은 3틱
        # 연속을 요구하는데 그 3틱이 쌓이기 전에 램프가 신호를 파괴한다.
        ceiling = 10
        for scenario_id in ("F06-P", "F15-H"):
            parameters = self.profiles["profiles"]["load.north_south"]["scenario_parameters"][scenario_id]
            self.assertLessEqual(
                parameters["target_rps"],
                ceiling,
                f"{scenario_id} load ramps past the band where 429 survives — "
                "the app promotes it to 5xx and its own discriminator rejects the run",
            )

    def test_f05r_pins_the_heap_so_the_limit_ladder_can_actually_oomkill(self) -> None:
        # limit을 내리면 힙 상한도 같이 내려간다(MaxRAMPercentage=25). 그래서 사다리
        # 세 단을 다 써도 OOMKill이 안 났다 — 25틱 내내 restart_count=0 (배치 #2).
        #
        # 109 실측 2026-08-04, payment-service cgroup:
        #   anon 413MiB / memory.current 417MiB (file 캐시는 0.8MiB뿐)
        #   heap committed 79MiB, non_heap 120MiB → 힙 외 anon ≈ 334MiB
        # 사다리 바닥 576Mi보다 163MiB 낮으니 커널이 죽일 이유가 없었다.
        #
        # -Xms + AlwaysPreTouch가 있어야 anon이 시작 즉시 384+334≈718MiB로 올라가
        # 640Mi·576Mi 단에서 확실히 한도를 넘는다. -Xmx만으로는 부족하다 — live set이
        # 73MiB뿐이라 SerialGC가 계속 회수해 힙이 그만큼 자라지 않는다.
        parameters = self.profiles["profiles"]["k8s.env"]["scenario_parameters"]["F05-R"]
        options = next(
            row["value"]
            for row in parameters["fault"]
            if row["name"] == "JAVA_TOOL_OPTIONS"
        )
        self.assertIn("-XX:+AlwaysPreTouch", options)
        self.assertRegex(options, r"-Xms(\d+)m")
        floor_mib = int(re.search(r"-Xms(\d+)m", options).group(1))
        non_heap_anon_mib = 334
        bottom_rung_mib = 576
        self.assertGreater(
            floor_mib + non_heap_anon_mib,
            bottom_rung_mib,
            "pretouched heap does not push anon past the bottom rung of the limit ladder",
        )

    def test_discriminator_floors_sit_at_rest_not_inside_the_ladder(self) -> None:
        # 감별자("증상은 있는데 기전이 없다")는 지표가 **평시 자리에 머물 때** 발동해야
        # 한다. 사다리가 밀어올리는 구간 안에 바닥을 두면 첫 단에서 곧바로 실격되고,
        # must_rule_out이 escalate보다 먼저 평가되므로(07-31) 사다리는 영영 못 오른다.
        #
        # F09-H가 그랬다. 감별자 바닥 0.5는 첫 단 heap-208의 예상값 0.385보다 위였고,
        # 매 틱 실격돼 heap-160·heap-128을 밟아보지도 못했다(배치 #28).
        #
        # 2026-08-06 후속: 비율 바닥은 은퇴했다. 파드별 실측으로 평시 범위가
        # 파드 나이에 따라 0.29(갓 뜬 파드)~0.4769(24h 숙성)로 넓어, 오발화 없는
        # 절대 바닥이 존재하지 않는다. "기전 부재"는 이제 잡음 없는 계단 함수
        # (살아 있는 파드 중 최소 Tenured limit)로 직접 묻는다 — run 4793c9f4의
        # '비율 얼어붙음'(주입 미반영)을 비율 임계로는 표현할 수 없다.
        controller = self.controllers["controllers"]["F09-H"]
        ratio_floors = [
            condition
            for condition in controller["must_rule_out"].get("any", [])
            if condition["observation"] == "order_old_gen_ratio" and condition["op"] == "lt"
        ]
        self.assertEqual(ratio_floors, [], "비율 바닥 감별자는 은퇴했다 — 위 주석 참조")
        steps = [
            condition
            for condition in controller["must_rule_out"].get("any", [])
            if condition["observation"] == "order_tenured_limit_mib"
        ]
        self.assertEqual(len(steps), 1, "F09-H must ask whether the injection landed")
        # 임계는 첫 단(heap-208 → Tenured 138.69MiB)과 평시(256Mi → 170.69MiB)
        # 사이에 있어야 한다: 평시를 읽으면 발화, 어떤 주입 단을 읽어도 침묵.
        self.assertEqual(steps[0]["op"], "gt")
        self.assertGreater(steps[0]["value"], 138.69)
        self.assertLess(steps[0]["value"], 170.69)

    def test_recovery_gates_read_metrics_that_actually_return(self) -> None:
        # 회복 게이트는 "부하가 끝나면 되돌아오는" 지표만 읽어야 한다.
        # prometheus.jvm_daemon_thread_count는 그렇지 않다 — JVM 스레드 풀은 한 번
        # 늘면 유지되므로 주입이 끝나도 내려오지 않는다. F21-P가 이걸 회복 조건으로
        # 읽어 정확히 10분 타임아웃을 태우고 전역 DIRTY로 끝났다(배치 #27). 임계
        # 추격도 이미 실패했다 — 07-31에 60→100으로 올렸는데 08-03엔 상주값이 109였다.
        # 상승만 하는 지표는 성공 조건(정체성)으로는 맞지만 회복 조건으로는 틀리다.
        one_way = {"prometheus.jvm_daemon_thread_count"}
        for scenario_id, controller in self.controllers["controllers"].items():
            query_by_observation = {
                item["id"]: item["query_id"] for item in controller["observations"]
            }
            block = controller.get("recovery") or {}
            for condition in block.get("all", []) + block.get("any", []):
                query_id = query_by_observation[condition["observation"]]
                self.assertNotIn(
                    query_id,
                    one_way,
                    f"{scenario_id}:recovery:{condition['id']} reads a metric "
                    "that does not come back after the load stops",
                )

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

    def test_rate_thresholds_are_stated_in_the_metric_unit(self) -> None:
        # prometheus.apm_service_error_rate reads apm.agent.otel.java.error_rate,
        # which the Polestar APM agent publishes as a PERCENTAGE (0..100) —
        # live-verified against VictoriaMetrics 119:18428 on 2026-07-29. The 48h
        # distinct non-zero values were 33.333 / 50 / 66.667 / 75 / 100, i.e.
        # 1/3, 1/2, 2/3, 3/4, 1 rendered as percent. A fraction would cap at 1.0.
        #
        # Every gate was authored as a 0..1 fraction (0.05 .. 0.3) beside genuine
        # 0..1 loadgen rates, so all 13 live scenarios were off by 100x — and in
        # both directions at once: success gates cleared at 0.1% error (a fake
        # success on one request in a thousand, charter G3) while must_rule_out
        # gates fired at 0.05% and vetoed sound runs.
        #
        # No gate here means "a tenth of a percent", so a non-zero threshold
        # below 1 is a unit error rather than a strict gate. F12-H's `gt 0`
        # asks only whether any error exists and is unit-free.
        floor = 1
        parked = json.loads(
            (ROOT / "registry" / "controllers-parked.json").read_text(encoding="utf-8")
        )["controllers"]
        every_controller = {**parked, **self.controllers["controllers"]}
        for scenario_id, controller in sorted(every_controller.items()):
            query_by_observation = {
                item["id"]: item["query_id"] for item in controller["observations"]
            }
            for section in ("success", "escalate", "must_rule_out", "recovery", "abort"):
                block = controller.get(section) or {}
                for condition in block.get("all", []) + block.get("any", []):
                    query_id = query_by_observation[condition["observation"]]
                    # 2026-07-30: the source moved to clickhouse.service_error_rate
                    # (trace table) because the APM rollup discarded 20-288x of the
                    # denominator. Both report percent, so the thresholds and this
                    # guard carry over unchanged.
                    if query_id != "clickhouse.service_error_rate":
                        continue
                    if condition["value"] == 0:
                        continue
                    self.assertGreaterEqual(
                        condition["value"],
                        floor,
                        f"{scenario_id}:{section}:{condition['id']} looks like a fraction",
                    )

    def test_any_gates_carry_no_threshold_subsumed_by_a_looser_sibling(self) -> None:
        # An `any` gate fires on its first satisfied condition, so two conditions
        # on the same observation and operator collapse into one: the looser
        # threshold always wins and the stricter can never decide anything.
        #
        # 2026-07-28 ff5b9ab moved discriminators out of `success` and inverted
        # them into `must_rule_out`, which is the right home for them. But three
        # landed beside a pre-existing veto on the same signal —
        # `payment-also-erroring >= 5` next to `payment-5xx-cascade >= 20` — and
        # silently killed it. The registry read as if F19-P/F20-Q/F20-R vetoed a
        # payment cascade; they had not been able to since that commit.
        #
        # A dead veto is worse than no veto: it reads as evidence of rigour.
        looser = {"gt": min, "gte": min, "lt": max, "lte": max}
        parked = json.loads(
            (ROOT / "registry" / "controllers-parked.json").read_text(encoding="utf-8")
        )["controllers"]
        every_controller = {**parked, **self.controllers["controllers"]}
        for scenario_id, controller in sorted(every_controller.items()):
            for section in ("success", "escalate", "must_rule_out", "recovery", "abort"):
                conditions = (controller.get(section) or {}).get("any") or []
                grouped: dict[tuple, list] = {}
                for condition in conditions:
                    key = (condition["observation"], condition["op"])
                    grouped.setdefault(key, []).append(condition)
                for (observation, operator), group in grouped.items():
                    if len(group) < 2 or operator not in looser:
                        continue
                    values = {item["id"]: item["value"] for item in group}
                    if any(
                        isinstance(value, bool) or not isinstance(value, (int, float))
                        for value in values.values()
                    ):
                        continue
                    dominant = looser[operator](values.values())
                    dead = sorted(cid for cid, value in values.items() if value != dominant)
                    self.assertEqual(
                        dead,
                        [],
                        f"{scenario_id}:{section}.any on {observation} {operator}: "
                        f"{dead} can never fire — {values} collapses to {dominant}",
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

    def test_north_south_load_outlives_every_judgment_window(self) -> None:
        # 배치 #16 (2026-08-03, F19-P·F19-S 실증): 부하(ramp_up+hold+ramp_down)가
        # 판정 창보다 먼저 끝나면 시나리오-출처 관측(live.json)이 사라져,
        # 진짜 실패 사유("성공 조건 미달")가 safety_observation_unavailable로
        # 덮이고 전이 사유로 분류돼 자동 재시도 2회를 태운다(실패 하나당 런 3개).
        # 동반 부하는 primary의 판정 창(최장 레벨 timeout)보다 60초 이상 오래
        # 살아야 한다. primary 부하는 반대 계약(timeout이 자기 부하를 덮는다,
        # test_level_timeout_covers_its_own_load_profile)과 함께 성립해야 하므로
        # 공백 0 — timeout == 부하 길이 — 만 허용된다.
        def seconds(value: str) -> int:
            match = re.fullmatch(r"(\d+)([sm])", value)
            assert match, value
            return int(match.group(1)) * (60 if match.group(2) == "m" else 1)

        margin = 60
        companion_params = self.profiles["profiles"]["load.north_south"][
            "scenario_parameters"
        ]
        problems = []
        for scenario_id, controller in self.controllers["controllers"].items():
            profile = controller["profile"]
            levels = profile["levels"]
            if profile.get("primary_ref") == "load.north_south":
                for level in levels:
                    parameters = level["parameters"]
                    if not {"ramp_up", "hold", "ramp_down"} <= set(parameters):
                        continue
                    duration = sum(
                        seconds(parameters[key])
                        for key in ("ramp_up", "hold", "ramp_down")
                    )
                    timeout = seconds(level["timeout"])
                    if duration != timeout:
                        problems.append(
                            f"{scenario_id}/{level['id']}: load {duration}s != timeout {timeout}s"
                        )
            elif "load.north_south" in (profile.get("companion_refs") or []):
                parameters = companion_params.get(scenario_id)
                if parameters is None:
                    problems.append(f"{scenario_id}: companion load has no parameters")
                    continue
                duration = sum(
                    seconds(parameters[key])
                    for key in ("ramp_up", "hold", "ramp_down")
                )
                timeout = max(seconds(level["timeout"]) for level in levels)
                if duration < timeout + margin:
                    problems.append(
                        f"{scenario_id}: companion load {duration}s < timeout {timeout}s + {margin}s"
                    )
        self.assertFalse(problems, "\n".join(problems))

    def test_loadgen_signals_require_a_profile_that_produces_them(self) -> None:
        """판정이 부하 산출물을 읽으면 부하를 만드는 프로파일이 있어야 한다.

        `loadgen_summary`/`http_probe` 어댑터는 `/tmp/rca-scenario-<ID>-live.json`을
        읽는다. 그 파일은 `load.*` 프로파일이 만든다. 둘이 어긋나면 신호가 **런 내내
        사용 불가**가 되고, 그 신호를 보는 게이트는 영원히 판정을 못 내린다.

        F15-T1이 그랬다(2026-08-04): companion이 하나도 없는데 success 조건이
        `checkout_5xx_rate`를 봤다. 두 주입(food OOMKilled, commerce 잠금 세션 20)이
        모두 성공했는데도 성공은 구조적으로 불가능했고, 재시작이 중단 예산에 닿아
        aborted로 끝났다. 같은 구멍이 F15-P·F14-P에도 있었다 — 그쪽은 success가 아니라
        **recovery**가 읽으므로, 실패가 그 시나리오에서 끝나지 않고 전역 DIRTY가 된다.

        primary도 부하를 만들 수 있다(F07-H·F11-R의 surge가 곧 주입이다).
        """
        loadgen_adapters = {"loadgen_summary", "http_probe"}
        loadgen_queries = {
            query_id
            for query_id, spec in self.queries["queries"].items()
            if spec.get("adapter") in loadgen_adapters
            # target_health는 http_probe지만 게이트웨이 URL을 직접 찌른다 —
            # live.json과 무관하므로 이 계약의 대상이 아니다(07-24 과매치).
            and query_id != "http.target_health"
        }
        self.assertTrue(loadgen_queries, "부하 산출물 질의를 하나도 못 찾았다")

        offenders = {}
        for scenario_id, controller in self.controllers["controllers"].items():
            profile = controller.get("profile") or {}
            refs = [profile.get("primary_ref") or profile.get("approved_profile_id") or ""]
            refs += profile.get("companion_refs") or []
            if any(str(ref).startswith("load.") for ref in refs):
                continue
            reading = {
                obs["id"]
                for obs in controller.get("observations") or []
                if obs.get("query_id") in loadgen_queries
                # domain 한정은 시나리오 자신의 live.json이 아니라 상주 baseline
                # 유닛의 문서를 읽는다(2026-08-06, 러너 2b01f43). 그 문서는 부하
                # companion과 무관하게 항상 존재하므로 이 계약의 위반이 아니다.
                and "domain" not in (obs.get("parameters") or {})
            }
            if not reading:
                continue
            judged = set()
            for gate in ("success", "escalate", "must_rule_out", "abort", "recovery"):
                block = controller.get(gate) or {}
                for cond in (block.get("all") or []) + (block.get("any") or []):
                    if cond.get("observation") in reading:
                        judged.add(f"{gate}:{cond['observation']}")
            if judged:
                offenders[scenario_id] = sorted(judged)

        # 2026-08-04 배치가 찾은 미수리 3종(F14-P·F15-P·F15-T1)은 2026-08-06에
        # 전부 해소됐다 — 판정이 읽는 관측을 domain 한정으로 옮겨 상주 baseline
        # 유닛이 생산자가 됐다. 목록은 이제 비어 있어야 하며, 새 항목이 생기면
        # 이 테스트가 그 자리에서 막는다. 라이브에서만 보이던 결함을 레지스트리
        # 단계로 끌어내리는 것이 이 검사의 목적이다.
        self.assertEqual(
            offenders,
            {},
            "판정이 읽는 부하 신호를 아무도 만들지 않는다: "
            + json.dumps(offenders, ensure_ascii=False, sort_keys=True),
        )

    def test_companionless_controllers_must_domain_qualify_their_loadgen_reads(self) -> None:
        # 게이트가 안 보는 관측이라도, companion 없는 시나리오가 자기 live.json을
        # 읽으면 그 신호는 런 내내 죽어 있다 — 표시만 안 될 뿐 같은 병이다.
        # F14-P는 transfer_2xx_rate에 @core-banking을 붙이면서 entry_health만
        # 빠뜨렸다(2026-08-06 발견). 관측 전체에 계약을 건다.
        loadgen_adapters = {"loadgen_summary", "http_probe"}
        loadgen_queries = {
            query_id
            for query_id, spec in self.queries["queries"].items()
            if spec.get("adapter") in loadgen_adapters
            and query_id != "http.target_health"
        }
        for scenario_id, controller in self.controllers["controllers"].items():
            profile = controller.get("profile") or {}
            refs = [profile.get("primary_ref") or profile.get("approved_profile_id") or ""]
            refs += profile.get("companion_refs") or []
            if any(str(ref).startswith("load.") for ref in refs):
                continue
            for obs in controller.get("observations") or []:
                if obs.get("query_id") in loadgen_queries:
                    self.assertIn(
                        "domain", obs.get("parameters") or {},
                        f"{scenario_id}:{obs['id']} — companion 없는 시나리오의 "
                        "부하 관측은 domain을 한정해야 한다",
                    )


if __name__ == "__main__":
    unittest.main()
