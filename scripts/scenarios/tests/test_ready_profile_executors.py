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

    def test_every_approved_ladder_rung_passes_its_own_executor(self) -> None:
        # 승인 레지스트리에 등재된 사다리 단이 정작 실행기 검증에서 거부되면, 그 단은
        # 라이브에서만 드러나는 사문이 된다. `bind_level_parameters`가 단을 정확히
        # 묶어 주는데도 검증이 `scenario_parameters` 한 벌만 보면 기본값과 다른 단은
        # 전부 막힌다 — F15-T1이 768Mi 단에서 그렇게 막혔고(배치 #12), F15-R의
        # `episode-fault-240`도 같은 상태였다. 실행기 세 개가 같은 형태였다.
        #
        # 한 실행기만 고치고 형제로 안 옮기는 것이 이 저장소의 반복 결함이므로
        # (#9·#29·#30) 파일 단위가 아니라 사다리를 가진 프로파일 전체를 훑는다.
        # `.py`만 훑던 탓에 정작 검증을 가진 실행기 둘을 통째로 빠뜨렸다 — 레지스트리가
        # 셸 래퍼(`profiles/load-east-west.sh`)를 가리키는데 validate는 그 옆
        # `load_east_west_executor.py`에 있기 때문이다. 그 사각에서 F03-H의 target_url이
        # 레지스트리(`?delayMs=5000`)와 실행기(`?days=120`) 사이로 갈라진 채 살아남았고,
        # 라이브에서 주입과 **정리가 함께** 거부돼 전역 DIRTY가 됐다(2026-08-05 배치).
        # host.stress도 같은 사각에 있었다(사다리 6종).
        checked = 0
        for profile_id, profile in self.profiles.items():
            levels = profile.get("scenario_levels") or {}
            if not levels:
                continue
            stem = Path(profile["executor"]).stem
            if profile["executor"].endswith(".sh"):
                stem = f"{stem.replace('-', '_')}_executor"
            module = load(stem)
            # load.north_south는 별도 validate 없이 build_invocation 안에서 검사한다.
            if not hasattr(module, "validate"):
                continue
            for scenario_id, rungs in levels.items():
                for rung in rungs:
                    with self.subTest(profile=profile_id, scenario=scenario_id, rung=rung["level_id"]):
                        module.validate(scenario_id, rung["parameters"], profile)
                        checked += 1
        self.assertGreater(checked, 20, "ladder sweep found almost nothing to check")

    def test_db_lock_injects_inside_the_cluster_and_reclaims_its_client(self) -> None:
        # 품질 기준서 G6 / 부록 A: 주입 세션은 실 앱 세션과 구별되지 않아야 하므로
        # (1) 클러스터 안에서 접속하고 (2) 신원을 사칭하며 (3) 정리는 파드 삭제로 한다.
        plan = compiler.compile_plan("f01-r-pg-lock-checkout")
        argv, stdin = db_lock.build_invocation(plan, "cleanup")
        self.assertEqual(argv[0], "/usr/bin/bash")
        self.assertIn(db_lock.APP_IDENTITY, argv)
        self.assertNotIn("nkia@192.168.122.206", argv)
        script = stdin.decode()
        self.assertIn("kind: Pod", script)
        self.assertIn("delete pod", script)
        self.assertIn("lucida.io/db-client=session", script)
        self.assertNotIn("pg_terminate_backend", script)
        self.assertNotIn("pg_sleep", script)
        self.assertNotIn("pkill", script)

    def test_oracle_lock_carries_its_values_into_the_pod(self) -> None:
        """F01-P 2026-08-04: 잠금이 한 번도 걸린 적이 없었다.

        원격 heredoc이 `$tag`·`$schema`를 이 스크립트의 변수로 알고 빈 문자열로
        폈다. 파드에는 `set_identifier('')`가 든 `/tmp/$tag.sql`과
        `SP2-0310 unable to open file` 로그만 남았는데, preflight·cleanup·recovery는
        전부 성공을 보고했다 — 그것들은 로컬에서 만든 `/tmp/${tag}.pid`를 보고
        주입은 `/tmp/$tag.pid`에 썼기 때문이다. 감별자 하나만이 이걸 잡았다.
        """
        plan = compiler.compile_plan("f01-p-oracle-lock-cross-domain")
        script = db_lock.build_invocation(plan, "run")[1].decode()
        run_case = script.split("run)")[1].split(";;")[0]
        # 값은 env로 건너간다. 로컬 이름을 원격에서 쓸 수는 없다.
        for pair in ('TAG="$tag"', 'SCHEMA="$schema"', 'TBL="$table"',
                     'KEYCOL="$keycol"', 'KEY="$key"', 'HOLD="$hold"'):
            self.assertIn(pair, run_case)
        # sh -lc 뒤가 파드에서 도는 텍스트다. 거기엔 로컬 소문자 이름이 없어야 한다.
        remote = run_case.split("sh -lc", 1)[1]
        for local_only in ("$tag", "$schema", "$table", "$keycol", "$key", "$hold"):
            self.assertNotIn(local_only, remote)
        self.assertIn("/tmp/$TAG.sql", remote)
        self.assertIn("/tmp/$TAG.pid", remote)
        # 식별자는 따옴표를 두르지 않는다 — 두르면 문자열을 select 하게 된다.
        self.assertIn("current_schema=$SCHEMA", remote)
        self.assertIn("select $KEYCOL from $TBL where $KEYCOL=", remote)
        # 값은 반대로 반드시 따옴표 안에 있어야 한다(셸 이스케이프를 거친 형태).
        quote = "'\"'\"'"
        self.assertIn(f"set_identifier({quote}$TAG{quote})", remote)
        self.assertIn(f"{quote}$KEY{quote}", remote)

    def test_mock_expectations_are_snapshotted_and_restored(self) -> None:
        script = self.assert_contract(mock, "f01-h-commerce-pg-429", "mock.expectation")
        self.assertIn("ACTIVE_EXPECTATIONS", script)
        self.assertIn('--data-binary "@$state"', script)
        self.assertIn("/root/tb-kubeconfig", script)
        plan = compiler.compile_plan("f06-r-commerce-external-hang")
        self.assertEqual(next(x for x in plan["profile_instances"] if x["profile_id"] == "mock.expectation")["parameters"]["delay_seconds"], 30)

    def test_kubernetes_patch_has_fixed_target_and_original_value_snapshot(self) -> None:
        script = self.assert_contract(k8s_patch, "f09-p-inventory-cpu-throttle", "k8s.patch")
        self.assertIn('current >"$state.tmp"', script)
        self.assertIn('--limits="cpu=$original"', script)
        self.assertIn("testbed-inventory", " ".join(k8s_patch.build_invocation(compiler.compile_plan("f09-p-inventory-cpu-throttle"), "run")[0]))

    def test_kubernetes_patch_lowers_the_request_with_the_limit(self) -> None:
        # 쿠버네티스는 자기 request보다 낮은 CPU limit을 표현할 수 없다 — 패치 전체가
        # 거부된다. 2026-08-04까지 이 실행기는 --limits만 설정했고, 두 시나리오의
        # 사다리는 request(200m) 아래인 100m·50m로 내려간다. 그래서 F12-H·F09-P는
        # 주입 자체가 한 번도 성립하지 않았다(배치 #1).
        #
        # 109 실측 2026-08-04 평시 CPU: product 16m / inventory 7m. request를 사다리
        # 값까지 내려도 파드는 그대로 스케줄된다.
        script = self.assert_contract(k8s_patch, "f12-h-pod-cpu-network-lookalike", "k8s.patch")
        self.assertIn('--requests="cpu=$request"', script)
        # 스냅샷은 limit·request 두 값을 함께 잡고, 정리는 둘 다 되돌린다. request를
        # 스냅샷하지 않으면 내려간 request가 영구히 남는다.
        self.assertIn("current() { echo \"$(field limits) $(field requests)\"; }", script)
        self.assertIn('--requests="cpu=$original_request"', script)
        # 사다리에 request 아래 단이 실제로 남아 있어야 이 수리가 의미가 있다.
        # F09-P는 여기서 빠진다 — 2026-08-04 라이브 실측이 그 사다리를 request 위로
        # 되돌려 놓았기 때문이다. 평시 CPU가 낮아 request를 내려도 스케줄은 되지만
        # (inventory 7m), 스케줄과 **서빙**은 다른 질문이었다: baseline 부하 아래에서
        # inventory는 200m에서 Ready 12/12·재시작 0인데 175m에서는 12틱 내내 Ready가
        # 되지 못하고 재시작한다. 같은 날 product는 175m을 버텼다 — 바닥은 서비스마다
        # 다르므로 사다리마다 재야 한다.
        rungs = [
            int(level["parameters"]["fault_cpu_limit"].removesuffix("m"))
            for level in self.profiles["k8s.patch"]["scenario_levels"]["F12-H"]
        ]
        self.assertTrue(
            [rung for rung in rungs if rung < 200],
            "F12-H ladder no longer reaches under the 200m request",
        )

    def test_kubernetes_patch_adaptive_ladder_accepts_only_exact_levels(self) -> None:
        profile = self.profiles["k8s.patch"]
        levels = profile["scenario_levels"]["F09-P"]
        self.assertEqual([level["level_id"] for level in levels], ["conservative-250m", "constrained-225m", "endpoint-200m"])
        self.assertEqual([level["parameters"]["fault_cpu_limit"] for level in levels], ["250m", "225m", "200m"])
        for level in levels:
            k8s_patch.validate("F09-P", level["parameters"], profile)
        instance = next(row for row in compiler.compile_plan("f09-p-inventory-cpu-throttle")["profile_instances"] if row["profile_id"] == "k8s.patch")
        self.assertEqual(instance["selected_level_id"], "endpoint-200m")
        self.assertEqual(instance["scenario_levels"], levels)
        tampered = dict(levels[1]["parameters"])
        tampered["fault_cpu_limit"] = "99m"
        with self.assertRaisesRegex(k8s_patch.ExecutorError, "predeclared"):
            k8s_patch.validate("F09-P", tampered, profile)

    def test_kubernetes_patch_reaches_banking_and_still_pins_each_scenario(self) -> None:
        # Until 2026-07-31 build_invocation hardcoded the commerce namespace, so
        # F21-P's "throttle transfer alone" lever could not be dispatched at all
        # even though allowed_locations already listed banking-namespace. Revert
        # the ALLOWLIST lookup to the old constant and this first assertion fails.
        profile = self.profiles["k8s.patch"]
        argv, _ = k8s_patch.build_invocation(
            compiler.compile_plan("f21-p-banking-api-tomcat-thread-saturation"), "run"
        )
        self.assertIn("rca-testbed-banking", argv)
        self.assertIn("testbed-transfer", argv)
        # Generalising the namespace must not turn it into a free parameter: a
        # scenario may only patch the deployment its own answer key names.
        with self.assertRaisesRegex(k8s_patch.ExecutorError, "not allowlisted"):
            k8s_patch.validate(
                "F21-P", profile["scenario_levels"]["F09-P"][0]["parameters"], profile
            )
        with self.assertRaisesRegex(k8s_patch.ExecutorError, "not allowlisted"):
            k8s_patch.validate(
                "F09-P", profile["scenario_levels"]["F21-P"][0]["parameters"], profile
            )

    def test_kubernetes_patch_ladder_stays_above_the_declared_cpu_request(self) -> None:
        # The API server rejects any limit below requests.cpu, and every testbed
        # deployment declares 200m. A rung below that is a rung that can never be
        # applied — measured 2026-07-31 on testbed-transfer with 100m and 50m.
        profile = self.profiles["k8s.patch"]
        levels = profile["scenario_levels"]["F21-P"]
        self.assertEqual(
            [level["parameters"]["fault_cpu_limit"] for level in levels],
            ["400m", "300m", "200m"],
        )
        for level in levels:
            millicores = int(level["parameters"]["fault_cpu_limit"].removesuffix("m"))
            self.assertGreaterEqual(millicores, 200, level["level_id"])

    def test_kubernetes_patch_level_override_changes_exact_builder_argv(self) -> None:
        plan = compiler.compile_plan("f09-p-inventory-cpu-throttle")
        instance = next(row for row in plan["profile_instances"] if row["profile_id"] == "k8s.patch")
        levels = instance["approved_levels"]
        canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"))
        low, low_id = sys.modules["executor_common"].bind_level_parameters(plan, "k8s.patch", 0, canonical(levels[0]["parameters"]))
        endpoint, endpoint_id = sys.modules["executor_common"].bind_level_parameters(plan, "k8s.patch", 2, canonical(levels[2]["parameters"]))
        low_argv, _ = k8s_patch.build_invocation(low, "run")
        endpoint_argv, _ = k8s_patch.build_invocation(endpoint, "run")
        self.assertEqual((low_id, endpoint_id), ("conservative-250m", "endpoint-200m"))
        self.assertIn("250m", low_argv)
        self.assertIn("200m", endpoint_argv)
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
            ["250m", "200m", "175m"],
        )
        self.assertEqual(instance["parameters"]["deployment"], "testbed-product")
        self.assertEqual(instance["parameters"]["container"], "product-service")
        k8s_patch.validate("F12-H", instance["parameters"], self.profiles["k8s.patch"])
        argv, script = k8s_patch.build_invocation(plan, "cleanup")
        self.assertIn("testbed-product", argv)
        self.assertIn("product-service", argv)
        self.assertIn('--limits="cpu=$original"', script.decode())

    def test_cache_cleanup_restores_snapshot_and_waits_for_dependents(self) -> None:
        script = self.assert_contract(cache, "f11-r-redis-down-fallback-overload", "cache.control")
        self.assertIn('--replicas="$original"', script)
        self.assertIn('rollout status deploy/"$dependent"', script)

    def test_composite_cleanup_is_reverse_order(self) -> None:
        script = self.assert_contract(timeline, "f08-h-rollout-plus-external-fault", "timeline.compose")
        cleanup = script[script.index("cleanup)"):script.index("recovery)")]
        self.assertLess(cleanup.index("reset_mock"), cleanup.index("restore_rollout"))
        self.assertIn("Reverse sub-injection order is mandatory", cleanup)

    def test_live_matrix_matches_the_governed_ready_set(self) -> None:
        expected = {
            "F01-H", "F01-P", "F01-R", "F03-P", "F04-R", "F05-H", "F05-P", "F05-R",
            "F06-H", "F06-R", "F07-H", "F08-G", "F08-H", "F08-P", "F09-R",
            "F11-R", "F12-H", "F15-G", "F15-R", "F15-T1", "F16-H", "F17-R", "F18-P",
            "F19-P", "F19-S", "F20-Q", "F20-R", "F23-R", "F25-H",
            "F03-H", "F06-P",
            # 2026-08-06: F02-H·F21-P·F21-Q parked (0804 #24·27·28)
            "F10-H", "F10-P", "F15-P",
            "F09-H", "F09-P", "F17-P",
            "F04-H",
            "F15-H", "F15-T2",
            # 2026-07-29: 원장 테이블 READ ONLY. 필요하던 "catch-swallow injector"는
            # 앱에 심을 필요가 없었다 — 삼킴 + 자동 ack = 영구 유실이 이미 코드에 있고,
            # 없던 것은 그것을 발화시킬 쓰기 실패였다.
            "F14-P",
        }
        catalog = json.loads((ROOT / "catalog.json").read_text())
        actual = {row["id"] for row in catalog["scenarios"] if compiler.compile_plan(row["slug"])["live_allowed"]}
        self.assertEqual(actual, expected)
        plan = compiler.compile_plan("f12-h-pod-cpu-network-lookalike")
        self.assertTrue(plan["live_allowed"])
        self.assertEqual(plan["profile_instances"][0]["location_id"], "commerce-namespace")

    def test_f04r_uses_exact_live_contracts(self) -> None:
        # F02-P dropped out of this test on 2026-07-27: it is parked (no food
        # query rides idx_menus_category, so the DDL drop injures nothing), and
        # a parked scenario must not compile to a live plan. Its executor
        # contract is still guarded — see the parked-archive assertion in
        # test_data_network_executor_foundations.py.
        cases = [
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
