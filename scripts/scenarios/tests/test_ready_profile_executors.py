from __future__ import annotations

import importlib.util
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / "profiles"
sys.path.insert(0, str(PROFILES))

from executor_common import ExecutorError  # noqa: E402  (sys.path 조작 뒤에야 import 가능)


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
app_control = load("app_control_executor")
db_workload = load("db_workload_executor")


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

    def test_app_control_flips_the_flag_without_touching_the_deployment(self) -> None:
        # F18-P 재설계(0804 #13 집행): 재기동 없는 DB 플래그 토글. 롤아웃을 유발하는
        # 어떤 kubectl 동사도 스크립트에 있어선 안 된다 — patch/rollout이 다시 들어오면
        # maxSurge=0 아래서 자기 감별자(transfer-pod-failure)를 켜는 원래 병이 재발한다.
        script = self.assert_contract(app_control, "f18-p-banking-outbox-relay-halt", "app.control")
        self.assertIn("sqlplus", script)
        self.assertIn("update $table set $col=$1", script)
        for rollout_verb in ("kubectl patch", "rollout", "set env"):
            self.assertNotIn(rollout_verb, script)
        # 정리는 상태 파일 없이 멱등 UPDATE 하나여야 한다.
        self.assertNotIn("state_root", script)

    def test_app_control_delay_scenarios_carry_a_bounded_value(self) -> None:
        # F21-P·F21-Q 병합 재설계(0804 #27·#28): 무딘 레버가 대상을 죽여버렸으므로
        # 제어 행은 켜고 끄는 플래그가 아니라 "얼마나 느리게"라는 값을 나른다. 값형은
        # 사다리가 단마다 다른 값을 물고 오므로 동일성이 아니라 범위로 검증해야 한다 —
        # 실행기가 한 벌만 보고 다른 단을 전부 막던 병(배치 #12)을 물려받지 않는다.
        for scenario_id, contract in app_control.CONTRACTS.items():
            if "delay_ms" not in contract:
                continue
            with self.subTest(scenario=scenario_id):
                profile = self.profiles["app.control"]
                app_control.validate(scenario_id, dict(contract, delay_ms=2500), profile)
                column, injected, clean = app_control.control_values(contract)
                self.assertEqual(column, "delay_ms")
                self.assertEqual(injected, str(contract["delay_ms"]))
                # 평시 값이 0 이어야 preflight·cleanup 이 "지연 없음"을 기대치로 삼는다.
                self.assertEqual(clean, "0")
                # 0 은 평시값과 같다 — 아무 행도 바꾸지 않는 주입이 성공을 보고한다.
                for bad in (0, -1, app_control.MAX_DELAY_MS + 1, "1000", 1000.0, True, None):
                    with self.assertRaises(ExecutorError):
                        app_control.validate(scenario_id, dict(contract, delay_ms=bad), profile)
                with self.assertRaises(ExecutorError):
                    app_control.validate(scenario_id, dict(contract, service_id="somebody-else"), profile)

    def test_app_control_speaks_both_engines_without_leaking_a_local_name(self) -> None:
        # F21-Q 는 food/MySQL 이다. 오라클 한 벌만 있는 스크립트에 mysql 계약을 물리면
        # 주입은 조용히 아무 행도 건드리지 않는다. 그리고 원격 텍스트 안의 이름은 원격
        # 것이어야 한다 — 로컬 이름이 원격 heredoc 에서 빈 문자열로 퍼진 것이 F01-P 를
        # 일주일 죽였다(4398722). 비밀번호는 파드 자기 env 에서만 온다.
        script = app_control.SCRIPT.decode()
        self.assertIn("engine=", script)
        for engine in ("oracle)", "mysql)"):
            self.assertIn(engine, script)
        remote = script[script.index("mysql)"):script.index("esac")]
        self.assertIn("MYSQL_PWD=\"$MYSQL_ROOT_PASSWORD\"", remote)
        # 원격 sh -c 는 홑따옴표라 로컬이 먼저 펴지 않는다.
        self.assertIn("sh -c 'MYSQL_PWD=", remote)
        # 문장은 stdin 으로 간다. `-e "$SQL"` 이면 KCM 이 수집하는 원격 프로세스
        # 명령줄에 SQL 전문이 찍혀 주입이 자기 테이블 이름을 자백한다.
        self.assertIn("printf '%s\\n' \"$1\" |", remote)
        self.assertNotIn("-e \"$SQL\"", remote)
        self.assertNotIn("rootpassword", script)
        # 식별자를 따옴표로 감싸면 테이블이 아니라 문자열을 읽는다.
        self.assertIn("select $col from $table", script)
        for engine, expected in (("oracle", "systimestamp"), ("mysql", "now()")):
            with self.subTest(engine=engine):
                self.assertIn(f'now="{expected}"', script)
        for scenario_id, contract in app_control.CONTRACTS.items():
            with self.subTest(scenario=scenario_id):
                self.assertIn(f'{contract["engine"]})', script,
                              f"{scenario_id}: contract names an engine the script cannot speak")

    def test_the_delay_ceiling_is_one_number_in_every_place_that_enforces_it(self) -> None:
        # 상한은 세 곳이 각자 조인다: 앱(Thread.sleep 직전 클램프), DDL 의 CHECK,
        # 실행기의 값 검증. 하나만 올리면 주입은 성공을 보고하는데 앱은 다른 값을 쓰고,
        # 판정은 있지도 않은 지연을 전제로 결론을 적는다. 한 값임을 고정한다.
        repo = ROOT.parents[1]
        ceilings: dict[str, int] = {}
        for relative in ("core-banking/shop-common/src/main/java/com/corebanking/common/delay"
                         "/ResponseDelayFilter.java",
                         "food-delivery/restaurant-service/src/main/java/com/fooddelivery/restaurant/delay"
                         "/ResponseDelayFilter.java"):
            text = (repo / relative).read_text(encoding="utf-8")
            found = re.findall(r"MAX_DELAY_MS\s*=\s*([\d_]+)L", text)
            self.assertEqual(len(found), 1, f"{relative}: expected exactly one ceiling")
            ceilings[relative] = int(found[0].replace("_", ""))
        # DDL 은 init.sql 과 배포 스크립트(기존 PVC 대응) 두 벌로 존재한다.
        sql_sources = ("core-banking/db/init.sql", "food-delivery/db/init.sql",
                       "core-banking/k8s/build-and-deploy.sh", "food-delivery/k8s/build-and-deploy.sh")
        for relative in sql_sources:
            text = (repo / relative).read_text(encoding="utf-8")
            found = re.findall(r"delay_ms BETWEEN 0 AND (\d+)", text)
            self.assertEqual(len(found), 1, f"{relative}: expected exactly one delay_ms CHECK")
            ceilings[relative] = int(found[0])
        ceilings["app_control_executor.MAX_DELAY_MS"] = app_control.MAX_DELAY_MS
        self.assertEqual(len(set(ceilings.values())), 1, f"delay ceilings disagree: {ceilings}")

    def test_app_control_flag_scenarios_keep_their_one_or_zero_contract(self) -> None:
        # 값형을 들이면서 플래그형(F18-P)의 의미가 뒤집히면 릴레이가 정리 후에도
        # 멈춘 채로 남는다. enabled 는 주입 0 / 평시 1 이다.
        self.assertEqual(app_control.control_values(app_control.CONTRACTS["F18-P"]), ("enabled", "0", "1"))

    def test_app_control_contract_matches_the_approved_profile(self) -> None:
        approved = self.profiles["app.control"]["scenario_parameters"]
        for scenario_id, contract in app_control.CONTRACTS.items():
            self.assertEqual(
                contract, approved.get(scenario_id),
                f"{scenario_id}: executor CONTRACTS drifted from profiles.json",
            )

    def test_db_lock_contracts_do_not_drift_from_the_approved_profile(self) -> None:
        # F03-H(0804 #21)는 수리가 실행기에만 들어가 레지스트리와 어긋난 채 일주일을
        # 죽어 있었다. db.lock은 반대 방향도 가능하다 — CONTRACTS가 실행기 안에
        # 하드코딩돼 있어 registry만 고치면 validate()가 주입을 거부한다(F01-P
        # hold 600->900에서 실제로 걸릴 뻔했다). 두 벌이 같은 값을 주장하는지 고정.
        approved = self.profiles["db.lock"]["scenario_parameters"]
        for scenario_id, contract in db_lock.CONTRACTS.items():
            if scenario_id in approved:
                self.assertEqual(
                    contract, approved[scenario_id],
                    f"{scenario_id}: executor CONTRACTS drifted from profiles.json",
                )

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


    def _db_workload_plan(self) -> dict:
        """F02-H는 아직 db.workload에 묶여 있지 않다(판정 계약은 실측 후 별건).

        그래서 `compile_plan`으로는 이 프로파일 인스턴스를 얻을 수 없다. 실행기가
        받는 것과 같은 모양의 계획을 계약 표에서 직접 만든다 — 검증 대상은 계획
        컴파일러가 아니라 실행기 자신이다.
        """
        scenario_id = "F02-H"
        return {
            "scenario": {"id": scenario_id},
            "profile_instances": [
                {
                    "profile_id": "db.workload",
                    "parameters": dict(db_workload.CONTRACTS[scenario_id]),
                }
            ],
        }

    def test_db_workload_contract_matches_the_approved_profile(self) -> None:
        # 실행기 표와 레지스트리가 갈라지면 주입은 승인되지 않은 값으로 나간다.
        profile = self.profiles["db.workload"]
        self.assertTrue(profile["live_supported"])
        self.assertEqual(
            sorted(profile["parameter_contract"]["allowed_scenarios"]),
            sorted(db_workload.CONTRACTS),
        )
        for scenario_id, contract in db_workload.CONTRACTS.items():
            self.assertEqual(profile["scenario_parameters"][scenario_id], contract)

    def test_db_workload_scan_never_confesses_the_injection(self) -> None:
        # G6/L2. 세 축이 전부 자연 분포 안에 있어야 한다(2026-07-27 캡처 실측).
        plan = self._db_workload_plan()
        params = plan["profile_instances"][0]["parameters"]
        argv, stdin = db_workload.build_invocation(plan, "run")
        script = stdin.decode()

        # ① 세션 신원이 실제 앱과 같아야 한다 — 시나리오 이름이 새면 그 자체가 정답이다.
        self.assertEqual(params["client_identity"], "PostgreSQL JDBC Driver")
        for fragment in db_workload.FORBIDDEN_IDENTITY_FRAGMENTS:
            with self.assertRaises(ExecutorError):
                db_workload.validate(
                    "F02-H", dict(params, client_identity=f"batch-{fragment}-job"), {}
                )

        # ② 주기 대기는 클라이언트측이어야 한다. 서버측 pg_sleep은 실운영에 없는
        #    PgSleep 대기를 남겨 주입을 혼자 식별시킨다.
        self.assertNotIn("pg_sleep", script.lower())
        self.assertIn('sleep "\\$PERIOD"', script)

        # ③ 질의문이 파드 argv에 실리면 노드 프로세스 수집기가 그대로 캡처한다.
        #    SQL은 env(SCAN_SQL)로만 들어가고, 러너 argv에도 완성된 SELECT는 없다.
        self.assertIn("SCAN_SQL", script)
        self.assertNotIn("SELECT count(*)", " ".join(argv))
        self.assertNotIn("payment_logs;", " ".join(argv))

    def test_db_workload_stays_read_only_and_inside_the_cluster(self) -> None:
        plan = self._db_workload_plan()
        params = plan["profile_instances"][0]["parameters"]
        script = db_workload.build_invocation(plan, "run")[1].decode()

        # 읽기 전용은 선언이 아니라 서버측 강제여야 한다. 업무 테이블 쓰기·잠금은
        # F02-H 설계의 명시 금지이고, 잠금은 db.lock의 표면이라 겹치면 원인이 갈리지 않는다.
        self.assertIn("default_transaction_read_only = on", script)
        self.assertIn("statement_timeout", script)
        for banned in ("LOCK TABLE", "FOR UPDATE", "INSERT ", "UPDATE ", "DELETE "):
            self.assertNotIn(banned, script)

        # 정리는 파드 삭제여야 한다. 신원이 앱과 같아졌으므로 이름으로 쏘는
        # pg_terminate_backend는 실 서비스 세션을 죽인다(db.lock이 먼저 배운 것).
        self.assertNotIn("pg_terminate_backend", script)
        self.assertIn("delete pod", script)

        # 클러스터 밖에서 붙으면 client_addr이 tb-runner의 NAT 주소로 드러난다.
        with self.assertRaises(ExecutorError):
            db_workload.validate("F02-H", dict(params, access="tb-runner"), {})

    def test_db_workload_period_is_a_range_not_an_equality(self) -> None:
        # 사다리가 단마다 주기를 바꾼다. 동일성으로만 검증하면 기본값 아닌 단이
        # 통째로 거부된다(배치 #12의 재발 형태).
        params = dict(db_workload.CONTRACTS["F02-H"])
        for good in (db_workload.MIN_PERIOD_SECONDS, 2, 5, db_workload.MAX_PERIOD_SECONDS):
            db_workload.validate("F02-H", dict(params, period_seconds=good), {})
        for bad in (0, -1, db_workload.MAX_PERIOD_SECONDS + 1, "10", 10.0, True, None):
            with self.assertRaises(ExecutorError):
                db_workload.validate("F02-H", dict(params, period_seconds=bad), {})
        # 나머지 키는 여전히 동일성이다 — 대상이 조용히 바뀌면 안 된다.
        for drift in ("table", "schema", "namespace", "db_pod"):
            with self.assertRaises(ExecutorError):
                db_workload.validate("F02-H", dict(params, **{drift: "somewhere-else"}), {})

    def test_db_workload_refuses_unlisted_scenarios_and_modes(self) -> None:
        params = dict(db_workload.CONTRACTS["F02-H"])
        with self.assertRaises(ExecutorError):
            db_workload.validate("F03-P", params, {})
        with self.assertRaises(ExecutorError):
            db_workload.validate("F02-H", dict(params, mode="occupy"), {})
        with self.assertRaises(ExecutorError):
            db_workload.validate("F02-H", dict(params, engine="mysql"), {})
        # 레지스트리가 막으면 실행기도 막아야 한다.
        with self.assertRaises(ExecutorError):
            db_workload.validate(
                "F02-H", params, {"parameter_contract": {"allowed_scenarios": ["F09-Z"]}}
            )


if __name__ == "__main__":
    unittest.main()
