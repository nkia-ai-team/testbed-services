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
        # parked과 cut은 실행 집합 밖이라 정답지를 요구하지 않는다. 둘을 나눈 이유는
        # 회생 가능성이지 정답지 의무가 아니므로, 여기서는 같은 취급이다.
        cls.active = {
            scenario_id
            for scenario_id, row in cls.catalog.items()
            if row["readiness"] not in ("parked", "cut")
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

    def test_written_keys_are_machine_matchable(self) -> None:
        # Charter §3-1 fix 1 + fix 4. The three pre-existing keys put the
        # trigger and the fault-bearing target into one prose paragraph, which
        # no scorer can match, and none of them defined how to score at all.
        for scenario_id in sorted(self.active):
            entry = self.metadata[scenario_id]
            root_cause = entry.get("root_cause")
            if not isinstance(root_cause, dict):
                continue
            target_id = root_cause["target_id"]
            self.assertNotIn(" ", target_id, f"{scenario_id}: target_id must be an identifier")
            self.assertLessEqual(len(target_id), 64, scenario_id)

            scoring = entry.get("scoring")
            self.assertIsInstance(scoring, dict, f"{scenario_id}: written key needs scoring")
            self.assertIn(scoring["granularity"], {"service", "database-relation", "node", "container"})
            self.assertTrue(scoring["accept"], scenario_id)
            self.assertNotIn(
                target_id,
                scoring.get("partial", []),
                f"{scenario_id}: the root cause cannot also be partial credit",
            )

    def test_code_anchors_still_name_a_symbol_that_lives_there(self) -> None:
        # Charter §3-1 fix 3 / G7 CI requirement. Line numbers rot as code moves;
        # the audit found three anchors that had drifted off the symbol they
        # name. Anchors are written "path:start-end (Symbol — note)", so the
        # check is that at least one identifier from the parenthetical actually
        # appears within the cited span (±1 line of slack for signatures that
        # wrap).
        anchor_pattern = re.compile(r"^([\w./-]+):(\d+)(?:-(\d+))?\s*\((.+)$")
        checked = 0
        for scenario_id in sorted(self.active):
            root_cause = self.metadata[scenario_id].get("root_cause")
            if not isinstance(root_cause, dict):
                continue
            for anchor in root_cause.get("code_anchor", []):
                match = anchor_pattern.match(anchor)
                self.assertIsNotNone(
                    match, f"{scenario_id}: anchor is not 'path:line (Symbol …)': {anchor}"
                )
                relative = match.group(1)
                start = int(match.group(2))
                end = int(match.group(3) or match.group(2))
                symbol_text = match.group(4)

                path = REPO / relative
                self.assertTrue(path.is_file(), f"{scenario_id}: no such file {relative}")
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                self.assertLessEqual(
                    end, len(lines), f"{scenario_id}: {relative}:{end} is past EOF"
                )

                span = "\n".join(lines[max(0, start - 2):min(len(lines), end + 1)])
                identifiers = re.findall(r"[A-Za-z_][A-Za-z0-9_-]{2,}", symbol_text)
                self.assertTrue(
                    any(identifier in span for identifier in identifiers),
                    f"{scenario_id}: {relative}:{start}-{end} names "
                    f"{identifiers[:4]} but none of them is there",
                )
                checked += 1
        self.assertGreater(checked, 0, "no code anchors were verifiable")


if __name__ == "__main__":
    unittest.main()
