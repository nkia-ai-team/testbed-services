"""관측 평면 분리 회귀 테스트.

정본 설계 = docs/spec-scenario-observation-plane.md

핵심은 두 가지다.
1. monitor 정본 하나가 시나리오·baseline 두 신원을 모두 낼 수 있어야 한다.
2. baseline entrypoint가 발행하는 step 이름이 러너의 관측 계약(profiles.json
   domain_profiles)과 **글자 그대로** 같아야 한다. 이 계열의 결함은 거의 항상
   "주입은 멀쩡한데 판정하는 쪽이 다른 이름을 본다"였다.
"""
from __future__ import annotations

import contextlib
import datetime
import importlib.util
import io
import json
import re
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROFILES_DIR = HERE.parent / "profiles"
REPO = HERE.parent.parent.parent

SPEC = importlib.util.spec_from_file_location("loadgen_monitor", PROFILES_DIR / "loadgen_monitor.py")
assert SPEC and SPEC.loader
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)

# 도메인 -> (entrypoint 경로, baseline script 경로, systemd 유닛)
DOMAINS = {
    "commerce": ("commerce/loadgen/entrypoint.sh", "commerce/loadgen/script.js", "loadgen-commerce"),
    "food-delivery": (
        "food-delivery/loadgen/entrypoint.sh",
        "food-delivery/loadgen/script.js",
        "loadgen-food",
    ),
    "core-banking": (
        "core-banking/loadgen/entrypoint.sh",
        "core-banking/loadgen/script.js",
        "loadgen-banking",
    ),
}

START_CALL = re.compile(
    r"baseline_publisher_start\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)"
)


def _point(metric: str, stamp: datetime.datetime, **tags: str) -> str:
    return json.dumps(
        {
            "type": "Point",
            "metric": metric,
            "data": {"time": stamp.isoformat().replace("+00:00", "Z"), "tags": tags},
        }
    )


class LoadgenMonitorTests(unittest.TestCase):
    def _feed(self, builder: monitor.LiveDocumentBuilder) -> dict:
        base = datetime.datetime(2026, 7, 29, 3, 0, tzinfo=datetime.timezone.utc)
        for index in range(20):
            stamp = base + datetime.timedelta(seconds=index * 0.5)
            builder.consume(_point("iterations", stamp))
            status = "500" if index % 5 == 0 else "200"
            builder.consume(_point("http_reqs", stamp, step="checkout", status=status))
            builder.consume(_point("http_reqs", stamp, step="list", status="200"))
        document = builder.build()
        assert document is not None
        return document

    def test_baseline_and_scenario_identities_come_from_one_parser(self) -> None:
        scenario = self._feed(
            monitor.LiveDocumentBuilder(
                {"scenario_id": "F07-H", "scenario_tag": "scenario_id=F07-H"}, "checkout", "list"
            )
        )
        baseline = self._feed(
            monitor.LiveDocumentBuilder(
                {"domain": "commerce", "unit": "loadgen-commerce"}, "checkout", "list"
            )
        )

        # 신원만 다르고 지표는 완전히 같아야 한다 — 이 변경은 *누가 언제 쓰는가*만 바꾼다.
        identity_keys = {"scenario_id", "scenario_tag", "domain", "unit"}
        self.assertEqual(
            {k: v for k, v in scenario.items() if k not in identity_keys},
            {k: v for k, v in baseline.items() if k not in identity_keys},
        )
        self.assertEqual(baseline["domain"], "commerce")
        self.assertEqual(baseline["unit"], "loadgen-commerce")
        self.assertNotIn("scenario_id", baseline)
        self.assertNotIn("domain", scenario)
        self.assertAlmostEqual(scenario["business_5xx_rate"], 0.2)
        self.assertEqual(scenario["read_nonok_rate"], 0.0)

    def test_a_request_free_window_publishes_no_rate_at_all(self) -> None:
        """분모가 0인 창은 비율을 발행하지 않는다.

        종전에는 `if checkout_count else 0.0`으로 0을 실었다. 이 비율들의 소비자는
        전부 감별자라(F03-P·F19-P·F20-Q·F06-P·F10-H·F14-P·F16-H의 must_rule_out)
        요청이 한 건도 못 나간 창이 "오류율 0% = 정상"으로 답하면 배제되지 않은
        교란 요인이 배제된 것처럼 통과한다. 필드를 생략하면 러너의
        _loadgen_observation이 None을 받아 LiveProbeError를 던져 unusable이 된다.
        """
        builder = monitor.LiveDocumentBuilder({"domain": "commerce", "unit": "u"}, "checkout", "list")
        base = datetime.datetime(2026, 8, 7, 3, 0, tzinfo=datetime.timezone.utc)
        for index in range(6):
            builder.consume(_point("iterations", base + datetime.timedelta(seconds=index)))
        document = builder.build()
        assert document is not None

        for field in (
            "checkout_5xx_rate",
            "business_2xx_rate",
            "business_4xx_rate",
            "business_5xx_rate",
            "business_409_rate",
            "business_429_rate",
            "business_nonok_rate",
            "read_2xx_rate",
            "read_nonok_rate",
        ):
            self.assertNotIn(field, document)
        # 분모는 실린다 — 0건이라는 사실 자체가 읽는 쪽에 필요한 정보다.
        self.assertEqual(document["checkout_count"], 0)
        self.assertEqual(document["read_count"], 0)
        # achieved_rps는 접기가 아니다: 분모가 시간이고 0은 "부하가 전달되지
        # 않았다"는 진짜 측정값이다. 이걸 부재로 만들면 achieved_rps < 15를 읽는
        # load-not-delivered 감별자가 발화해야 할 때 침묵한다.
        self.assertIn("achieved_rps", document)

    def test_rates_and_their_denominator_are_published_together(self) -> None:
        document = self._feed(
            monitor.LiveDocumentBuilder({"domain": "commerce", "unit": "u"}, "checkout", "list")
        )
        self.assertEqual(document["checkout_count"], 20)
        self.assertEqual(document["read_count"], 20)
        self.assertAlmostEqual(document["business_5xx_rate"], 0.2)
        # 분모 없이는 0.2가 1/5인지 20/100인지 구분되지 않는다. 2026-08-07 실측에서
        # 대조 팔의 창당 요청이 2~15건이라 p≈0.3의 표준오차가 ~0.20이었고, 그게
        # 차분 게이트가 성립하지 않은 이유였다.
        self.assertEqual(
            round(document["business_5xx_rate"] * document["checkout_count"]), 4
        )

    def test_identity_is_mutually_exclusive_and_domain_requires_a_unit(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                monitor.main(["--source", "s", "--output", "o", "--business-step", "checkout"])
            with self.assertRaises(SystemExit):
                monitor.main([
                    "--source", "s", "--output", "o", "--business-step", "checkout",
                    "--domain", "commerce",  # --unit 없음
                ])


class BaselinePublisherWiringTests(unittest.TestCase):
    """entrypoint가 넘기는 step 이름이 러너의 관측 계약과 일치하는지 잠근다."""

    @classmethod
    def setUpClass(cls) -> None:
        profiles = json.loads((PROFILES_DIR.parent / "registry" / "profiles.json").read_text())
        cls.contract = profiles["profiles"]["load.north_south"]["parameter_contract"]

    def _start_call(self, domain: str) -> tuple[str, str, str, str]:
        entrypoint = (REPO / DOMAINS[domain][0]).read_text()
        match = START_CALL.search(entrypoint)
        self.assertIsNotNone(match, f"{domain} entrypoint이 baseline 발행을 시작하지 않는다")
        return match.groups()  # type: ignore[return-value]

    def test_every_baseline_entrypoint_publishes_a_domain_document(self) -> None:
        for domain in DOMAINS:
            with self.subTest(domain=domain):
                published_domain, unit, _business, _read = self._start_call(domain)
                self.assertEqual(published_domain, domain)
                self.assertEqual(unit, DOMAINS[domain][2])

    def test_published_steps_match_the_runner_observation_contract(self) -> None:
        """step 이름이 어긋나면 문서는 나오지만 사업 지표가 전부 0이 된다."""
        for domain, (_entry, _script, unit) in DOMAINS.items():
            with self.subTest(domain=domain):
                _d, _u, business_step, read_step = self._start_call(domain)
                declared = {
                    (profile["business_step"], profile.get("read_step", ""))
                    for profile in self.contract["domain_profiles"].values()
                    if profile["baseline_unit"] == unit
                }
                self.assertIn((business_step, read_step), declared)

    def test_baseline_scripts_tag_the_steps_they_promise_to_publish(self) -> None:
        """core-banking baseline은 태그가 아예 없어 사업 지표를 셀 수 없었다(2026-07-29)."""
        for domain, (_entry, script_path, _unit) in DOMAINS.items():
            with self.subTest(domain=domain):
                _d, _u, business_step, read_step = self._start_call(domain)
                script = (REPO / script_path).read_text()
                for step in (business_step, read_step):
                    if not step:
                        continue
                    self.assertIn(
                        f"step: '{step}'",
                        script,
                        f"{domain} baseline script가 step '{step}'을 태그하지 않는다",
                    )


if __name__ == "__main__":
    unittest.main()
