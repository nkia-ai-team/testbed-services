"""Answer-key contracts (quality charter G7).

The 2026-07-27 golden audit found that 44 metadata entries contained exactly
three charter-shaped answer keys, and that F21-P and F09-R carried *different*
answer keys for an identical injection. Nothing in the repo could have caught
that, because the metadata never recorded what was actually injected.

These tests close that hole: the answer key must state the injection, that
statement must match the live controller, and no two scenarios may claim the
same injection.
"""

from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]


def _load(name: str) -> dict:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


class AnswerKeyContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = {row["id"]: row for row in _load("catalog.json")["scenarios"]}
        cls.controllers = _load("registry/controllers.json")["controllers"]
        cls.metadata = _load("registry/scenario-metadata.json")["scenarios"]
        cls.active = {
            scenario_id
            for scenario_id, row in cls.catalog.items()
            if row["readiness"] != "parked"
        }

    def test_every_active_scenario_states_what_it_injected(self) -> None:
        for scenario_id in sorted(self.active):
            self.assertIn(scenario_id, self.metadata, scenario_id)
            self.assertIn("injected_fault", self.metadata[scenario_id], scenario_id)

    def test_stated_injection_matches_the_live_controller(self) -> None:
        # An answer key that drifts from the controller describes a fault that
        # was never injected — the dataset would be scored against fiction.
        for scenario_id in sorted(self.active):
            stated = self.metadata[scenario_id]["injected_fault"]
            controller = self.controllers.get(scenario_id)
            if controller is None:
                self.assertIsNone(stated, scenario_id)
                continue
            profile = controller["profile"]
            self.assertEqual(stated["profile"], profile["approved_profile_id"], scenario_id)
            self.assertEqual(
                [(level["id"], level["parameters"]) for level in stated["levels"]],
                [(level["id"], level["parameters"]) for level in profile["levels"]],
                scenario_id,
            )

    def test_no_two_scenarios_claim_the_same_injection(self) -> None:
        # The F21-P / F09-R defect: one injection, two contradictory answers.
        by_fingerprint: dict[str, list[str]] = {}
        for scenario_id in sorted(self.active):
            stated = self.metadata[scenario_id]["injected_fault"]
            if not stated:
                continue
            digest = hashlib.sha256(
                json.dumps(stated["levels"], sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            by_fingerprint.setdefault(digest, []).append(scenario_id)
        collisions = {k: v for k, v in by_fingerprint.items() if len(v) > 1}
        self.assertEqual(collisions, {}, f"identical injections: {collisions}")

    def test_code_anchors_point_at_a_line_that_exists(self) -> None:
        # Charter §3-1 fix 3. The audit measured three anchors whose line
        # numbers had drifted off the symbol they name.
        anchor_pattern = re.compile(r"^([\w./-]+\.(?:java|yml|yaml|sql|py|js)):(\d+)")
        checked = 0
        for scenario_id in sorted(self.active):
            root_cause = self.metadata[scenario_id].get("root_cause")
            if not isinstance(root_cause, dict):
                continue
            for anchor in root_cause.get("code_anchor", []):
                match = anchor_pattern.match(anchor)
                if not match:
                    continue
                relative, line_no = match.group(1), int(match.group(2))
                path = REPO / relative
                if not path.is_file():
                    matches = list(REPO.glob(f"**/{Path(relative).name}"))
                    self.assertTrue(matches, f"{scenario_id}: no such file {relative}")
                    path = matches[0]
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                self.assertLessEqual(
                    line_no, len(lines), f"{scenario_id}: {relative}:{line_no} past EOF"
                )
                checked += 1
        self.assertGreater(checked, 0, "no code anchors were verifiable")


if __name__ == "__main__":
    unittest.main()
