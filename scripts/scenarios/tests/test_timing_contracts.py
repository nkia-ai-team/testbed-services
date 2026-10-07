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
QUERIES = Path(__file__).resolve().parent.parent / "registry" / "queries.json"

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
        cls.queries = json.loads(QUERIES.read_text(encoding="utf-8"))["queries"]

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
        # consecutive_ticks counts independent samples, not polls: a gate whose
        # slowest signal refreshes every 60s needs 60s between confirmations no
        # matter how often the controller ticks (adaptive.py _updated_streaks).
        # Budgeting `consecutive_ticks * tick_interval` understated the need by
        # 4x on every prometheus gate.
        for scenario_id, controller in self.controllers.items():
            success = controller.get("success")
            if not success:
                continue
            tick_sec = _seconds(controller.get("tick_interval", "15s"))
            ticks = success.get("consecutive_ticks", 1)
            step = max(tick_sec, self._gate_update_interval(controller, success))
            needed = (ticks - 1) * step + tick_sec
            for level in controller.get("profile", {}).get("levels", []):
                # Success is not evaluated until min_hold, so that — not settle —
                # is where the confirmation window can first start.
                budget = _seconds(level["timeout"]) - _seconds(level.get("min_hold", 0))
                self.assertGreaterEqual(
                    budget,
                    needed,
                    f"{scenario_id}/{level['id']}: {needed}s needed for {ticks} "
                    f"independent success samples ({step}s apart) but only "
                    f"{budget}s remain after min_hold",
                )

    def _gate_update_interval(self, controller: dict, gate: dict) -> int:
        """Source cadence that paces a gate, mirroring adaptive.py.

        An ``all`` gate waits on its slowest signal; an ``any`` gate is carried
        by whichever condition fires, so its quickest sets the pace.
        """
        query_by_observation = {
            item["id"]: item["query_id"] for item in controller.get("observations", [])
        }

        def intervals_for(conditions: list) -> list[int]:
            return [
                self.queries[query_by_observation[condition["observation"]]][
                    "update_interval_sec"
                ]
                for condition in conditions
                if condition.get("observation") in query_by_observation
            ]

        paces = [0]
        if gate.get("all"):
            paces.append(max(intervals_for(gate["all"]) or [0]))
        if gate.get("any"):
            paces.append(min(intervals_for(gate["any"]) or [0]))
        return max(paces)

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
