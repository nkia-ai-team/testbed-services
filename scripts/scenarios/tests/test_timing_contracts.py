"""Static timing-budget contracts for controller registry entries.

07-19 F03-G post-mortem (docs/runbook-scenario-load-execution.md §10): a level
timeout shorter than its own load profile guarantees evaluation_level_timeout
even when every signal is healthy, and a freshness contract tighter than the
metric's real ingestion cadence resets success streaks on healthy runs.  These
tests keep both classes of defect out of the registry.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

REGISTRY = Path(__file__).resolve().parent.parent / "registry" / "controllers.json"

# APM percentile series land in per-minute batches (two 15s samples at :00/:15),
# so the newest-sample age routinely approaches 60s before the next batch.
P95_MIN_FRESHNESS_SEC = 120


def _seconds(value: str | int) -> int:
    if isinstance(value, int):
        return value
    match = re.fullmatch(r"(\d+)(s|m|h)", value)
    assert match, f"unparseable duration {value!r}"
    return int(match.group(1)) * {"s": 1, "m": 60, "h": 3600}[match.group(2)]


class TimingContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.controllers = json.loads(REGISTRY.read_text(encoding="utf-8"))["controllers"]

    def test_level_timeout_covers_its_own_load_profile(self) -> None:
        for scenario_id, controller in self.controllers.items():
            for level in controller.get("profile", {}).get("levels", []):
                parameters = level.get("parameters", {})
                if "ramp_up" not in parameters or "hold" not in parameters:
                    continue
                profile_sec = (
                    _seconds(parameters["ramp_up"])
                    + _seconds(parameters["hold"])
                    + _seconds(parameters.get("ramp_down", 0))
                )
                timeout_sec = _seconds(level["timeout"])
                self.assertGreaterEqual(
                    timeout_sec,
                    profile_sec,
                    f"{scenario_id}/{level['id']}: level timeout {timeout_sec}s is "
                    f"shorter than its load profile {profile_sec}s",
                )

    def test_max_injection_duration_covers_the_longest_level(self) -> None:
        for scenario_id, controller in self.controllers.items():
            levels = controller.get("profile", {}).get("levels", [])
            if not levels or "max_injection_duration" not in controller:
                continue
            longest = max(_seconds(level["timeout"]) for level in levels)
            self.assertGreaterEqual(
                _seconds(controller["max_injection_duration"]),
                longest,
                f"{scenario_id}: max_injection_duration is shorter than the "
                "longest level timeout",
            )

    def test_success_window_fits_inside_the_level(self) -> None:
        for scenario_id, controller in self.controllers.items():
            success = controller.get("success")
            if not success:
                continue
            tick_sec = _seconds(controller.get("tick_interval", "15s"))
            needed = success.get("consecutive_ticks", 1) * tick_sec
            for level in controller.get("profile", {}).get("levels", []):
                budget = _seconds(level["timeout"]) - _seconds(level.get("settle", 0))
                self.assertGreaterEqual(
                    budget,
                    needed,
                    f"{scenario_id}/{level['id']}: settle leaves no room for "
                    f"{success.get('consecutive_ticks')} consecutive success ticks",
                )

    def test_p95_freshness_matches_measured_ingestion_cadence(self) -> None:
        for scenario_id, controller in self.controllers.items():
            for observation in controller.get("observations", []):
                if observation.get("query_id") != "prometheus.apm_service_p95":
                    continue
                self.assertGreaterEqual(
                    _seconds(observation["freshness"]),
                    P95_MIN_FRESHNESS_SEC,
                    f"{scenario_id}/{observation['id']}: apm p95 freshness is "
                    "tighter than the per-minute batch ingestion cadence",
                )


if __name__ == "__main__":
    unittest.main()
