from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "profiles"))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


compiler = load_module("scenario_compile_plan_for_load", ROOT / "compile-plan.py")
executor = load_module(
    "load_north_south_executor", ROOT / "profiles" / "load_north_south_executor.py"
)
host_stress = load_module(
    "host_stress_executor", ROOT / "profiles" / "host_stress_executor.py"
)

SLUG_BY_SCENARIO = {
    json.loads(path.read_text())["id"]: json.loads(path.read_text())["slug"]
    for path in sorted((ROOT / "manifests").glob("*.yaml"))
}


class NorthSouthExecutorTests(unittest.TestCase):
    @staticmethod
    def canonical(value) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    def test_live_supported_profiles_are_explicit(self) -> None:
        profiles = json.loads((ROOT / "registry" / "profiles.json").read_text())["profiles"]
        live = {profile_id for profile_id, profile in profiles.items() if profile["live_supported"]}
        self.assertEqual(
            live,
            {
                "db.lock", "db.table_readonly", "mock.expectation", "load.north_south", "k8s.patch",
                "k8s.lifecycle", "cache.control", "timeline.compose", "db.ddl", "load.east_west",
                "kafka.control", "k8s.resource", "k8s.probe", "k8s.env", "host.stress", "timeline.multi",
                # 2026-08-06: F18-P 재설계 — 재기동 없는 DB 플래그 토글(0804 #13 집행).
                "app.control",
                # 2026-08-07: F02-H 캐시 무력화 재설계의 companion 주입 표면.
                # 읽기 전용 광역 스캔 한 모드만 라이브다(F03-P 커넥션 점유 지연은
                # 기전·자백 양쪽에서 성립하지 않아 별건 — 실행기 docstring 참조).
                "db.workload",
            },
        )

    def test_allowlisted_parameters_bind_capacity_profiles(self) -> None:
        profiles = json.loads((ROOT / "registry" / "profiles.json").read_text())["profiles"]
        profile = profiles["load.north_south"]
        expected = {"F07-H": 160, "F01-R": 35, "F01-H": 35, "F05-R": 35, "F05-H": 20}
        for scenario_id, target_rps in expected.items():
            params = profile["scenario_parameters"][scenario_id]
            executor.validate_parameters(scenario_id, params, profile)
            self.assertEqual(params["target_rps"], target_rps)
            self.assertEqual(params["entry_url"], "http://192.168.122.77:30080")
            self.assertEqual(params["script_path"], "/opt/loadgen/commerce/surge.js")

    def test_parameter_tampering_is_rejected(self) -> None:
        profiles = json.loads((ROOT / "registry" / "profiles.json").read_text())["profiles"]
        profile = profiles["load.north_south"]
        params = dict(profile["scenario_parameters"]["F07-H"])
        params["entry_url"] = "http://attacker.invalid"
        with self.assertRaisesRegex(executor.ExecutorError, "predeclared"):
            executor.validate_parameters("F07-H", params, profile)

    def test_f07h_capacity_ladder_is_exact_and_tamper_proof(self) -> None:
        profile = json.loads((ROOT / "registry" / "profiles.json").read_text())["profiles"]["load.north_south"]
        levels = profile["scenario_levels"]["F07-H"]
        self.assertEqual([level["level_id"] for level in levels], ["healthy-high-120", "knee-140", "overload-160"])
        self.assertEqual([level["parameters"]["target_rps"] for level in levels], [120, 140, 160])
        for level in levels:
            executor.validate_parameters("F07-H", level["parameters"], profile)
        plan = compiler.compile_plan("f07-h-north-south-surge")
        instance = executor.load_instance(plan)
        self.assertEqual(instance["selected_level_id"], "overload-160")
        self.assertEqual(instance["scenario_levels"], levels)
        tampered = dict(levels[1]["parameters"])
        tampered["target_rps"] = 69
        with self.assertRaisesRegex(executor.ExecutorError, "predeclared"):
            executor.validate_parameters("F07-H", tampered, profile)

    def test_level_override_changes_exact_builder_argv(self) -> None:
        plan = compiler.compile_plan("f07-h-north-south-surge")
        levels = executor.load_instance(plan)["approved_levels"]
        low, low_id = executor.bind_level_parameters(plan, "load.north_south", 0, self.canonical(levels[0]["parameters"]))
        knee, knee_id = executor.bind_level_parameters(plan, "load.north_south", 1, self.canonical(levels[1]["parameters"]))
        low_argv, _ = executor.build_invocation(low, "run")
        knee_argv, _ = executor.build_invocation(knee, "run")
        self.assertEqual((low_id, knee_id), ("healthy-high-120", "knee-140"))
        self.assertIn("120", low_argv)
        self.assertIn("140", knee_argv)
        self.assertNotEqual(low_argv, knee_argv)
        tampered = dict(levels[0]["parameters"])
        tampered["target_rps"] = 61
        with self.assertRaisesRegex(executor.ExecutorError, "predeclared"):
            executor.bind_level_parameters(plan, "load.north_south", 0, self.canonical(tampered))

    def test_fixed_companion_load_profile_rejects_level_overrides(self) -> None:
        # F01-R uses load.north_south as a fixed companion (db.lock is primary), so
        # the profile has no predeclared scenario_levels of its own for F01-R and
        # any attempt to bind an adaptive level index must be rejected outright.
        plan = compiler.compile_plan("f01-r-pg-lock-checkout")
        instance = executor.load_instance(plan)
        params = instance["parameters"]
        self.assertEqual(instance["approved_levels"], [])
        with self.assertRaisesRegex(executor.ExecutorError, "rejects adaptive level overrides"):
            executor.bind_level_parameters(plan, "load.north_south", 0, self.canonical(params))

    def test_command_builder_uses_strict_ssh_argv_and_scoped_cleanup(self) -> None:
        plan = compiler.compile_plan("f07-h-north-south-surge")
        argv, stdin = executor.build_invocation(plan, "cleanup")
        self.assertEqual(argv[0], "/usr/bin/ssh")
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertEqual(argv[argv.index("-i") + 1], "/root/.ssh/tb_key")
        self.assertIn("nkia@192.168.122.206", argv)
        self.assertNotIn("password", " ".join(argv).lower())
        script = stdin.decode()
        self.assertIn("/proc/[0-9]*", script)
        self.assertIn('prev" == "--tag"', script)
        self.assertNotIn("pkill", script)
        self.assertIn('systemctl is-active --quiet "$baseline_unit"', script)
        self.assertIn('--out "json=$samples"', script)
        self.assertIn('"entry_status": self.entry_status', script)
        self.assertIn('"checkout_5xx_rate": checkout_5xx_rate', script)
        self.assertIn("checkout_results", script)
        self.assertIn('"business_ok": self.entry_status in {200, 400, 409}', script)
        self.assertIn('rca-scenario-${safe_id}-live.json', script)
        # The monitor body is injected from the canonical loadgen_monitor.py
        # rather than restated here — a second hand-maintained copy is what
        # killed F06-P's sole success condition on 2026-07-29.
        self.assertIn("--mode tail --scenario-id", script)
        self.assertEqual(
            (Path(executor.__file__).resolve().parent / "loadgen_monitor.py").read_text()
            in script,
            True,
        )

    def test_live_requires_exact_digest_and_confirmation_before_dispatch(self) -> None:
        plan = compiler.compile_plan("f07-h-north-south-surge")
        confirmation = executor.confirmation_for(plan)
        with self.assertRaisesRegex(executor.ExecutorError, "plan-digest"):
            executor.authorize_live(plan, None, confirmation)
        with self.assertRaisesRegex(executor.ExecutorError, "confirm"):
            executor.authorize_live(plan, plan["plan_digest"], "wrong")
        executor.authorize_live(plan, plan["plan_digest"], confirmation)

    def test_composite_plan_selects_load_profile_by_id(self) -> None:
        plan = compiler.compile_plan("f01-h-commerce-pg-429")
        instance = executor.load_instance(plan)
        self.assertEqual(instance["profile_id"], "load.north_south")
        self.assertEqual(instance["parameters"]["scenario_tag"], "scenario_id=F01-H")


if __name__ == "__main__":
    unittest.main()


class RemoteArgumentQuotingTests(unittest.TestCase):
    """ssh hands the trailing argv to a *shell*, not to exec.

    Everything after the destination is joined with spaces into one command line
    that the remote login shell parses. Any metacharacter in a value therefore
    changes the command's structure instead of travelling as data. The banking
    health_path `/api/accounts?status=ACTIVE&size=1` did exactly that: the `&`
    backgrounded the first half and the remainder ran as a second command, which
    surfaced as `bash: line 1: GATEWAY_URL: command not found`. F10-P, F14-P,
    F18-P, F20-P and F21-P all failed within ten seconds of dispatch.

    These tests do not assert on the argv string — that is what let the defect
    ship. They replay the join through a real shell and compare what the remote
    script would actually receive in "$@".
    """

    PROBE = b'printf "%s\\n" "$#" "$@"\n'

    def _round_trip(self, argv: list[str]) -> list[str]:
        """Feed argv through a shell the way sshd does and read back "$@"."""
        marker = argv.index("--", argv.index("-s"))
        remote_command = " ".join(argv[marker - 2:])  # bash -s -- <joined args>
        completed = subprocess.run(
            ["bash", "-c", remote_command],
            input=self.PROBE, capture_output=True, timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        lines = completed.stdout.decode().splitlines()
        self.assertEqual(int(lines[0]), len(lines) - 1, "argument count disagrees with $#")
        return lines[1:]

    def test_banking_health_path_ampersand_survives_the_remote_shell(self) -> None:
        plan = compiler.compile_plan("f21-p-banking-api-tomcat-thread-saturation")
        argv, _ = executor.build_invocation(plan, "cleanup")
        received = self._round_trip(argv)
        self.assertEqual(len(received), 15, f"remote script needs 15 positional args, got {received}")
        self.assertEqual(received[11], "/api/accounts?status=ACTIVE&size=1")
        self.assertEqual(received[12], "GATEWAY_URL")
        self.assertEqual(received[13], "transfer")

    def test_every_domain_profile_survives_the_remote_shell(self) -> None:
        profiles = json.loads((ROOT / "registry" / "profiles.json").read_text())["profiles"]
        contract = profiles["load.north_south"]["parameter_contract"]
        seen_entry_urls = set()
        for scenario_id, parameters in profiles["load.north_south"]["scenario_parameters"].items():
            entry_url = parameters.get("entry_url")
            if entry_url in seen_entry_urls or entry_url not in contract["domain_profiles"]:
                continue
            seen_entry_urls.add(entry_url)
            slug = SLUG_BY_SCENARIO[scenario_id]
            argv, _ = executor.build_invocation(compiler.compile_plan(slug), "cleanup")
            received = self._round_trip(argv)
            domain_profile = contract["domain_profiles"][entry_url]
            self.assertEqual(
                received[11], domain_profile["health_path"],
                f"{scenario_id}: health_path did not survive the remote shell",
            )
            self.assertEqual(received[12], domain_profile["gateway_env"])
        self.assertEqual(len(seen_entry_urls), len(contract["domain_profiles"]))

    def test_host_stress_arguments_survive_the_remote_shell(self) -> None:
        # F21-P held this guard until 2026-07-31, when its injection moved from
        # node-wide CPU pressure to a transfer-only CPU limit. F21-Q is the same
        # cpu-mode host.stress contract on the food worker, so the quoting
        # property stays covered by a scenario that still dispatches it.
        plan = compiler.compile_plan("f21-q-food-order-tomcat-thread-saturation")
        argv, _ = host_stress.build_invocation(plan, "cleanup")
        received = self._round_trip(argv)
        self.assertEqual(received[:4], ["cleanup", "F21-Q", "cpu", "192.168.122.14"])
