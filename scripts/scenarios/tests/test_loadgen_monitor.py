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
