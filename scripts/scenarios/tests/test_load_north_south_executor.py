from __future__ import annotations

import importlib.util
import json
import re
import shlex
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
                # 2026-10-08: F35-R — Oracle 애플리케이션 계정 잠금과 해제(역 문장 하나).
                "db.account",
                # 2026-10-08: F37-R — Deployment 파드 템플릿 dnsPolicy 한 칸의 롤아웃과 원복.
                "k8s.dns",
                # 2026-10-08: F38-R — MySQL 인스턴스 전체 read_only 켜기와 끄기(역 문장 하나).
                "db.instance_readonly",
                # 2026-10-09: F40-R — 업무 설정 행 하나 넣기와 지우기(+ 그 행을 읽는 서비스 재시작).
                "db.config_row",
                # 2026-10-09: F17-H — Deployment 컨테이너 이미지 한 칸의 롤아웃과 원복.
                "k8s.image",
                # 2026-10-09: F44-R — 워커 호스트 방화벽에 한 포트 허용 목록 체인 넣기와 지우기(ssh, 규칙마다 sudo iptables).
                "host.firewall",
                # 2026-10-09: F50-R — 네임스페이스 ResourceQuota 하나 만들기와 지우기(+ Deployment 일상 재배포).
                "k8s.quota",
                # 2026-10-09: F51-R — Deployment 하나의 replicas 를 줄이고 되돌리기(kubectl scale).
                "k8s.scale",
                # 2026-10-10: F59-R — 네임스페이스에 일회성 Job 하나 만들기와 지우기(내부 백필 작업).
                "k8s.job",
                # 2026-10-10: F52-R — 워커 노드 containerd 에서 이미지 지우기와 109 docker 에서 되돌려 넣기(ssh, sudo ctr) + Deployment 일상 재배포.
                "host.image",
                # 2026-10-16: F80-R — 워커 노드에서 일상 백업 작업을 systemd 일시 유닛으로 띄우고 멈추기(ssh, sudo systemd-run, systemctl stop) + 그 작업이 쓴 파일 지우기.
                "host.job",
                # 2026-10-10: F56-R — Oracle 표의 행 몇 개를 보관 표에 옮기고 지우기, 보관 행 되넣기(역 문장).
                "db.row_delete",
                # 2026-10-11: F51-P — MySQL 표의 한 기간 행을 백업 표에 복사하고 상태를 되돌리기, 백업 값 되쓰기(역 문장).
                "db.row_update",
                # 2026-11-09: F74-R 동반 — 부하 정리 뒤 건수 질의 하나의 공유 커서 비우기(DBMS_SHARED_POOL.PURGE)와 계획 복귀 확인.
                "db.cursor_reset",
            },
        )

    def test_allowlisted_parameters_bind_capacity_profiles(self) -> None:
        profiles = json.loads((ROOT / "registry" / "profiles.json").read_text())["profiles"]
        profile = profiles["load.north_south"]
        # 2026-08-20: F07-H 160 → 120. 사다리 pin(healthy-high-120)이 고정 evaluation
        # 계약으로 승격되며 scenario_parameters 가 pin 단 값이 됐다.
        expected = {"F07-H": 120, "F01-R": 35, "F01-H": 35, "F05-R": 35, "F05-H": 20}
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
        # 2026-08-20: F07-H 가 고정 계약이 되어 사다리 대조("predeclared")보다 먼저
        # entry_url 허용목록에서 거부된다. 어느 쪽이든 변조는 거부돼야 한다.
        with self.assertRaisesRegex(executor.ExecutorError, "not allowlisted|predeclared"):
            executor.validate_parameters("F07-H", params, profile)

    def test_f07h_fixed_contract_is_exact_and_tamper_proof(self) -> None:
        # 2026-08-20: F07-H 사다리(120→140→160)를 pin 단 healthy-high-120 으로 고정
        # 승격했다(approved-fixed-f07-h). load.north_south 에는 더 이상 사다리가 없으므로
        # 이 테스트는 고정 계약의 정확성과 변조 거부를 본다 — 고정 계약의 변조 방어선은
        # validate_parameters(범위 검사)가 아니라 bind_level_parameters 의 승인 단 대조다.
        profile = json.loads((ROOT / "registry" / "profiles.json").read_text())["profiles"]["load.north_south"]
        self.assertNotIn("F07-H", profile.get("scenario_levels", {}))
        params = profile["scenario_parameters"]["F07-H"]
        self.assertEqual(params["target_rps"], 120)
        executor.validate_parameters("F07-H", params, profile)
        plan = compiler.compile_plan("f07-h-north-south-surge")
        instance = executor.load_instance(plan)
        self.assertEqual(instance["selected_level_id"], "approved-fixed-f07-h")
        self.assertEqual(instance["scenario_levels"], [])
        self.assertEqual(
            instance["approved_levels"],
            [{"level_id": "approved-fixed-f07-h", "parameters": params}],
        )
        tampered = dict(params)
        tampered["target_rps"] = 69
        with self.assertRaisesRegex(executor.ExecutorError, "predeclared"):
            executor.bind_level_parameters(plan, "load.north_south", 0, self.canonical(tampered))

    def test_level_override_changes_exact_builder_argv(self) -> None:
        # 2026-08-20: load.north_south 의 마지막 사다리 셋(F07-H·F11-R·F20-R)이 고정
        # 승격돼 이 프로파일에는 단이 하나뿐이다. 단일 단에서도 override 경로는 같은
        # 규칙을 지켜야 한다 — 0번은 정확히 묶이고, 범위 밖 인덱스와 변조는 거부된다.
        plan = compiler.compile_plan("f07-h-north-south-surge")
        levels = executor.load_instance(plan)["approved_levels"]
        self.assertEqual(len(levels), 1)
        bound, bound_id = executor.bind_level_parameters(plan, "load.north_south", 0, self.canonical(levels[0]["parameters"]))
        bound_argv, _ = executor.build_invocation(bound, "run")
        self.assertEqual(bound_id, "approved-fixed-f07-h")
        self.assertIn("120", bound_argv)
        with self.assertRaisesRegex(executor.ExecutorError, "outside the predeclared adaptive ladder"):
            executor.bind_level_parameters(plan, "load.north_south", 1, self.canonical(levels[0]["parameters"]))
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
        # 발행되는 키만 확인한다. 종전에는 monitor 안의 지역 변수 이름까지 박아
        # 두어, 빈 창 가드를 넣느라 그 줄이 바뀌자 계약과 무관하게 깨졌다.
        self.assertIn('"checkout_5xx_rate"', script)
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

    def test_every_allowlisted_script_ships_from_the_repo(self) -> None:
        # deploy-live-promotions.sh 는 allowed_script_paths 에서 /opt/loadgen/<domain>/<file> 을
        # 유도해 <domain>/loadgen/<file> 을 올린다. 허용 목록에만 있고 저장소에 없으면 배포가
        # "load script missing from repo" 로 멈추므로 여기서 먼저 잡는다.
        contract = json.loads((ROOT / "registry" / "profiles.json").read_text())[
            "profiles"]["load.north_south"]["parameter_contract"]
        for remote_path in contract["allowed_script_paths"]:
            parts = Path(remote_path).parts
            self.assertEqual(parts[:3], ("/", "opt", "loadgen"), remote_path)
            self.assertTrue((REPO / parts[3] / "loadgen" / parts[4]).is_file(), remote_path)


REPO = ROOT.parents[1]
BANKING_HEAVY = "/opt/loadgen/core-banking/transfer-heavy-surge.js"
BANKING_NO_HISTORY = "/opt/loadgen/core-banking/transfer-balance-surge.js"
# 여정 함수 → api 를 거치는가. 잔액 조회와 계좌 목록은 nginx 가 account 로 바로 보낸다.
BANKING_JOURNEY_VIA_API = {
    "transferJourney": True,
    "transactionHistoryJourney": True,
    "balanceCheckJourney": False,
    "accountListJourney": False,
}


def journey_weights(script: str) -> dict[str, float]:
    """k6 default 함수의 누적 확률 분기(`if (r < x) { journey(); }`)를 여정별 비중으로 푼다."""
    body = script[script.index("export default function"):]
    weights: dict[str, float] = {}
    previous = 0.0
    for threshold, journey in re.findall(r"if \(r < ([0-9.]+)\) \{\s*(\w+)\(\);", body):
        weights[journey] = round(float(threshold) - previous, 6)
        previous = float(threshold)
    tail = re.search(r"\} else \{\s*(\w+)\(\);", body)
    assert tail is not None
    weights[tail.group(1)] = round(1.0 - previous, 6)
    return weights


class BankingCompanionMixTests(unittest.TestCase):
    """2026-10-20: F47-R, F63-R 동반 부하에서 거래 내역 단계를 뺐다.

    transfer-heavy-surge.js 의 거래 내역(15%)이 transfer 건수 질의 byb2y2hv7p4ag 로 Oracle CPU 를
    상한까지 채워(F47-R-run-3d446438, F63-R-run-81153e98) 러너가 설계 기전이 아니라 DB 갈래 피해로
    판정했다. 거래 내역 몫을 이체로 옮긴 transfer-balance-surge.js 를 두 시나리오만 쓰고, 나머지
    banking 시나리오의 동반 부하는 그대로여야 한다.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.profile = json.loads((ROOT / "registry" / "profiles.json").read_text())[
            "profiles"]["load.north_south"]
        cls.heavy = (REPO / "core-banking" / "loadgen" / "transfer-heavy-surge.js").read_text()
        cls.no_history = (REPO / "core-banking" / "loadgen" / "transfer-balance-surge.js").read_text()

    def test_default_transfer_heavy_mix_is_unchanged(self) -> None:
        self.assertEqual(
            journey_weights(self.heavy),
            {"transferJourney": 0.40, "balanceCheckJourney": 0.35,
             "transactionHistoryJourney": 0.15, "accountListJourney": 0.10},
        )

    def test_no_history_variant_drops_history_and_keeps_the_api_request_rate(self) -> None:
        weights = journey_weights(self.no_history)
        self.assertEqual(
            weights,
            {"transferJourney": 0.55, "balanceCheckJourney": 0.35, "accountListJourney": 0.10},
        )
        self.assertNotIn("/api/transfers?", self.no_history)
        heavy = journey_weights(self.heavy)

        def via_api(mix: dict[str, float]) -> float:
            return round(sum(w for j, w in mix.items() if BANKING_JOURNEY_VIA_API[j]), 6)

        def direct(mix: dict[str, float]) -> float:
            return round(sum(w for j, w in mix.items() if not BANKING_JOURNEY_VIA_API[j]), 6)

        # 같은 target_rps 면 api 요청률(F63-R 누수 속도)과 account 직행 몫이 그대로다.
        self.assertEqual(via_api(weights), via_api(heavy))
        self.assertEqual(direct(weights), direct(heavy))

    def test_no_history_variant_tags_the_banking_observation_steps(self) -> None:
        banking = self.profile["parameter_contract"]["domain_profiles"]["http://192.168.122.77:30082"]
        for step in (banking["business_step"], banking["read_step"]):
            self.assertIn(f"step: '{step}'", self.no_history)
        # 여정, 엔드포인트, 시드 규약은 원본과 같아야 관측 매칭과 재현성이 그대로다.
        for shared in (
            "http.post(`${GATEWAY_URL}/api/transfers`",
            "http.get(`${GATEWAY_URL}/api/accounts/${id}`",
            "http.get(`${GATEWAY_URL}/api/accounts?status=ACTIVE&size=20`",
            "mulberry32(SURGE_SEED + (__VU || 0) * 7919)",
            "const ACTIVE_ACCOUNTS = ['ACC-1001', 'ACC-1002', 'ACC-1003', 'ACC-1004', 'ACC-1005', 'ACC-1006', 'ACC-1007'];",
            "const MAX_VUS = Number(__ENV.MAX_VUS || 400);",
        ):
            self.assertIn(shared, self.heavy)
            self.assertIn(shared, self.no_history)

    def test_only_listed_scenarios_use_the_no_history_variant(self) -> None:
        params = self.profile["scenario_parameters"]
        self.assertIn(BANKING_NO_HISTORY, self.profile["parameter_contract"]["allowed_script_paths"])
        self.assertIn(BANKING_HEAVY, self.profile["parameter_contract"]["allowed_script_paths"])
        users = {sid for sid, p in params.items() if p["script_path"] == BANKING_NO_HISTORY}
        # 2026-10-21: F37-H 추가(운영 보강 1회차). F37-H-run-cd6543d1 이 같은 거래 내역 질의의 Oracle 포화로
        # must_rule_out ④(transfer·Oracle 고장 아님)에 걸렸다. 이체는 api 안에서 500 이라 하류 부하가 없다.
        # 2026-10-22: F68-R 추가(운영 보강 1회차). F68-R-run-8b3f61c5 가 같은 거래 내역 질의로 Oracle CPU 를
        # 1.92/2 까지 올려 must_rule_out ①(F42-R 아님: Oracle 평시)에 걸렸다.
        # 2026-10-26: F72-R 추가(운영 보강 1회차). F72-R-run-92d33300 이 surge.js 거래 내역(25%)의 같은 질의로
        # Oracle CPU 를 1.79/2 까지 올려 must_rule_out 3(Oracle 활성 세션과 CPU 평시)에 걸렸다.
        self.assertEqual(users, {"F37-H", "F47-R", "F63-R", "F68-R", "F72-R"})
        self.assertEqual(params["F21-P"]["script_path"], BANKING_HEAVY)
        # F63-R 은 api 요청률(약 11건/초)이 그대로여야 해 20rps 를 유지한다. F47-R 은 보고서
        # 8592be57 §5 구체안 2 의 12rps(50% 기준 이체 6건/초)를 55% 변형에 맞춰 11rps 로 환산했다.
        self.assertEqual(params["F63-R"]["target_rps"], 20)
        self.assertEqual(params["F47-R"]["target_rps"], 11)
        # F37-H 는 스크립트만 바꾸고 20rps 를 유지한다(이체 약 11건/초가 모두 api 의 설정 조회 예외로 500).
        self.assertEqual(params["F37-H"]["target_rps"], 20)
        # F68-R 은 10rps(이체 40%, 초당 약 4건)를 55% 변형에 맞춰 7rps(이체 초당 약 3.9건)로 환산했다.
        self.assertEqual(params["F68-R"]["target_rps"], 7)
        # F72-R 은 스크립트만 바꾸고 10rps 를 유지한다(방화벽이 DB 연결을 끊어 피해가 부하량과 무관하다).
        self.assertEqual(params["F72-R"]["target_rps"], 10)
        for scenario_id in sorted(users):
            executor.validate_parameters(scenario_id, params[scenario_id], self.profile)
            argv, _ = executor.build_invocation(
                compiler.compile_plan(SLUG_BY_SCENARIO[scenario_id]), "preflight")
            self.assertIn(shlex.quote(BANKING_NO_HISTORY), argv)
            self.assertNotIn(shlex.quote(BANKING_HEAVY), argv)


BANKING_TRANSFER_SCRIPTS = ("surge.js", "transfer-heavy-surge.js", "transfer-balance-surge.js",
                            "settlement-arrival-poll.js", "statement-export.js")
# 66 읽기 조회(2026-11-09): ACC-5001~ACC-5987 가운데 FROZEN 인 계좌. 이체 풀에 들어가면 account 가 400 으로 거절한다.
BANKING_FROZEN_BULK = {
    "5067", "5128", "5146", "5190", "5277", "5300", "5321", "5367", "5411", "5414", "5500", "5508",
    "5614", "5636", "5739", "5743", "5814", "5849", "5855", "5860", "5916", "5970", "5985",
}


def transfer_accounts(script: str) -> list[str]:
    block = script[script.index("const TRANSFER_ACCOUNTS = ("):]
    block = block[:block.index(").split(',')")]
    return ["ACC-" + n for n in "".join(re.findall(r"'([0-9,]+)'", block)).split(",")]


class BankingCompanionAccountPoolTests(unittest.TestCase):
    """2026-11-09 (v4 E5): 은행 동반 부하의 이체가 활성 계좌 7개끼리만 오가서 잔액이 금세 바닥나고
    'Insufficient balance' 400 이 잡음으로 늘었다(F47-R, F72-R 녹화). 이체 출금, 입금 계좌를 66 에서
    ACTIVE 이고 잔액 2,000,000 이상인 대량 계좌 733개로 넓힌다. 잔액 조회와 거래 내역은 그대로라
    조회 모양과 거래 내역 건수 질의(byb2y2hv7p4ag) 수가 바뀌지 않는다."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.scripts = {name: (REPO / "core-banking" / "loadgen" / name).read_text() for name in BANKING_TRANSFER_SCRIPTS}

    def test_every_banking_companion_script_shares_one_wide_transfer_pool(self) -> None:
        pools = {name: transfer_accounts(script) for name, script in self.scripts.items()}
        reference = pools["surge.js"]
        self.assertEqual(len(reference), 733)
        self.assertEqual(len(set(reference)), 733)
        self.assertTrue(all(re.fullmatch(r"ACC-5[0-9]{3}", a) and "ACC-5001" <= a <= "ACC-5987" for a in reference))
        self.assertFalse({a[4:] for a in reference} & BANKING_FROZEN_BULK)
        for name, pool in pools.items():
            self.assertEqual(pool, reference, name)

    def test_only_transfers_move_to_the_wide_pool(self) -> None:
        for name, script in self.scripts.items():
            transfer_fn = re.search(r"function (?:small)?[tT]ransferJourney\(\) \{(.*?)\n\}", script, re.S).group(1)
            self.assertIn("const from = pick(TRANSFER_ACCOUNTS);", transfer_fn, name)
            self.assertIn("let to = pick(TRANSFER_ACCOUNTS);", transfer_fn, name)
            self.assertNotIn("ACTIVE_ACCOUNTS", transfer_fn, name)
            self.assertEqual(script.count("pick(TRANSFER_ACCOUNTS)"), 3, name)
            # 잔액 조회와 거래 내역은 계속 활성 계좌 7개다.
            self.assertIn("const id = pick(ACTIVE_ACCOUNTS);", script, name)
        self.assertIn("const MERCHANT_ACCOUNTS = ACTIVE_ACCOUNTS.concat(['commerce-merchant']);",
                      self.scripts["settlement-arrival-poll.js"])

    def test_pool_file_watched_by_the_balance_check_matches_the_scripts(self) -> None:
        # scripts/oracle-maintenance.sh check 가 이 파일의 계좌 잔액 하한을 본다(확인 필요 12).
        listed = [line for line in (REPO / "core-banking" / "loadgen" / "transfer-accounts.txt").read_text().splitlines()
                  if line and not line.startswith("#")]
        self.assertEqual(listed, transfer_accounts(self.scripts["surge.js"]))

    def test_baseline_pairs_pool_to_seed_transfers_with_a_return(self) -> None:
        # 2026-11-09(확인 필요 11): 상주 기준선은 풀 계좌 → 활성 계좌 7개로 보내고, 같은 VU 의 다음 이체가
        # 같은 금액을 되돌린다. 7개 계좌는 입금(F74-R 폴링 전제)과 출금(거래 내역 첫 쪽)이 계속 쌓이고,
        # 받은 만큼만 돌려주므로 잔액이 시작 아래로 내려가지 않는다.
        baseline = (REPO / "core-banking" / "loadgen" / "script.js").read_text()
        self.assertEqual(transfer_accounts(baseline), transfer_accounts(self.scripts["surge.js"]))
        body = baseline[baseline.index("function smallTransferJourney()"):]
        body = body[:body.index("\n}\n")]
        self.assertIn("from = pick(TRANSFER_ACCOUNTS);", body)
        self.assertIn("to = pick(ACTIVE_ACCOUNTS);", body)
        self.assertIn("({ from, to, amount } = pendingReturn);", body)
        self.assertIn("pendingReturn = { from: to, to: from, amount: amount };", body)
        # 돈이 실제로 옮겨진 이체(COMPLETED)만 되돌린다.
        self.assertIn("if (status === 'COMPLETED') {", body)
        self.assertIn("if (!returning && res.status === 200) {", body)
        # 여정 비중, 단계 이름, 조회 대상은 그대로다.
        self.assertEqual(journey_weights(baseline), {
            "balanceCheckJourney": 0.55, "transactionHistoryJourney": 0.25,
            "accountListJourney": 0.10, "smallTransferJourney": 0.10})
        self.assertIn("step: 'transfer'", body)
        self.assertEqual(baseline.count("const id = pick(ACTIVE_ACCOUNTS);"), 2)


class FoodLoadOrderLookupTests(unittest.TestCase):
    """2026-11-09 (v4 E4): food 부하에 주문 읽기가 없어 order 관측이 POST 하나뿐이었다. 상주 기준선과
    surge.js 에 주문 조회 5% 를 브라우징 몫에서 떼어 더한다. 관측 단계(create, menu)와 이름이 겹치면
    러너 판정 값이 바뀌므로 새 단계 이름은 따로 둔다. 배차 목록 조회는 page=0 Slice 그대로여야
    dispatch 풀 계산(surge.js 주석)이 맞는다."""

    @classmethod
    def setUpClass(cls) -> None:
        loadgen = REPO / "food-delivery" / "loadgen"
        cls.baseline = (loadgen / "script.js").read_text()
        cls.surge = (loadgen / "surge.js").read_text()
        cls.food = json.loads((ROOT / "registry" / "profiles.json").read_text())["profiles"][
            "load.north_south"]["parameter_contract"]["domain_profiles"]["http://192.168.122.77:30181"]

    def test_mixes_take_the_order_lookup_share_from_browsing_only(self) -> None:
        self.assertEqual(journey_weights(self.baseline), {
            "browsingJourney": 0.50, "searchJourney": 0.15, "orderJourney": 0.20,
            "orderLookupJourney": 0.05, "deliveryTrackingJourney": 0.10})
        self.assertEqual(journey_weights(self.surge), {
            "browsingJourney": 0.40, "searchJourney": 0.30, "orderJourneyBounded": 0.10,
            "orderLookupJourney": 0.05, "deliveryTrackingJourney": 0.15})

    def test_order_lookup_reads_real_orders_without_touching_observed_steps(self) -> None:
        for script in (self.baseline, self.surge):
            body = script[script.index("function orderLookupJourney()"):]
            body = body[:body.index("\n}\n")]
            self.assertIn("`${ORDER_URL}/api/orders/${lastOrderId}`", body)
            self.assertIn("`${ORDER_URL}/api/orders?customerId=${customerId()}&page=0&size=10`", body)
            steps = set(re.findall(r"step: '([\w-]+)'", body))
            self.assertEqual(steps, {"order-detail", "order-history"})
            self.assertFalse(steps & {self.food["business_step"], self.food["read_step"]})
            # 상세는 생성 응답의 id 만 본다(시드 id 는 보존 기간으로 지워져 404).
            self.assertEqual(script.count("rememberOrder(orderRes);"), 1)
            self.assertIn("res.json('id')", script)

    def test_delivery_detail_reads_ids_from_the_active_list_not_a_fixed_range(self) -> None:
        # 2026-11-09: id 1~20000 은 30일 보존으로 지워져(66 배차 id 5,114,395~6,850,136) 상세가 전부 404 였다.
        for script in (self.baseline, self.surge):
            self.assertNotIn("DISPATCH_ID_MAX", script.replace("1~DISPATCH_ID_MAX", ""))
            body = script[script.index("function deliveryTrackingJourney()"):]
            body = body[:body.index("\n}\n")]
            self.assertIn("const dispatchId = pick(trackedDeliveryIds);", body)
            self.assertIn("if (trackedDeliveryIds.length === 0 || rand() < 0.1) {", body)
            self.assertIn("'delivery detail 200': (r) => r.status === 200", body)
            self.assertNotIn("404", body)
            refresh = script[script.index("function refreshTrackedDeliveries()"):]
            refresh = refresh[:refresh.index("\n}\n")]
            self.assertIn("step: 'active-list'", refresh)
            self.assertIn(".map((d) => d.id)", refresh)
            # 새 단계 이름도 러너 관측 단계와 겹치지 않는다.
            self.assertNotIn("active-list", {self.food["business_step"], self.food["read_step"]})

    def test_delivery_list_stays_a_first_page_slice_call(self) -> None:
        for script in (self.baseline, self.surge):
            calls = re.findall(r"/api/deliveries\?[^`]*", script)
            self.assertEqual(sorted(calls), ["/api/deliveries?status=ASSIGNED&page=0&size=20",
                                             "/api/deliveries?status=DELIVERED&page=0&size=20"])
        dispatch = (REPO / "food-delivery" / "dispatch-service" / "src" / "main" / "java" / "com" / "fooddelivery"
                    / "dispatch" / "repository" / "DispatchRepository.java").read_text()
        self.assertIn("Slice<Dispatch> findByStatus(String status, Pageable pageable);", dispatch)
        self.assertNotIn("Page<Dispatch>", dispatch)


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
        #
        # 2026-10-16: host.stress no longer puts arguments on the command line (sudo
        # logs it to the worker's auth log, which reaches the recording). They ride on
        # stdin as a `set --` line, which bash parses with the same quoting rules, so
        # the round trip now goes through that line. The scenario id is not among them.
        plan = compiler.compile_plan("f21-q-food-order-tomcat-thread-saturation")
        argv, stdin = host_stress.build_invocation(plan, "cleanup")
        self.assertEqual(argv[-3:], ["sudo", "bash", "-s"])
        preamble = stdin.split(b"\n", 1)[0] + b"\n"
        completed = subprocess.run(
            ["bash", "-s"], input=preamble + self.PROBE, capture_output=True, timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        lines = completed.stdout.decode().splitlines()
        self.assertEqual(int(lines[0]), len(lines) - 1, "argument count disagrees with $#")
        self.assertEqual(lines[1:], ["cleanup", "cpu", "192.168.122.14", "3", "480"])
