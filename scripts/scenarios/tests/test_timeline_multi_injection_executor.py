from __future__ import annotations

import base64
import copy
import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / "profiles"
sys.path.insert(0, str(PROFILES))


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, PROFILES / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


multi = load("timeline_multi_injection_executor")


def plan(scenario: str, params: dict) -> dict:
    return {
        "scenario": {"id": scenario},
        "profile_instances": [
            {"profile_id": "timeline.multi", "parameters": copy.deepcopy(params), "location": {}}
        ],
    }


def decode_spec(argv_stdin: tuple[list, bytes]) -> dict:
    argv, _ = argv_stdin
    return json.loads(base64.b64decode(argv[-1]))


class MultiInjectionContractTests(unittest.TestCase):
    def test_f08g_distractor_precedes_cross_domain_oracle_lock(self) -> None:
        p = multi.CONTRACTS["F08-G"]
        multi.validate("F08-G", p, {})
        spec = decode_spec(multi.build_invocation(plan("F08-G", p), "run"))
        kinds = [s["kind"] for s in spec["steps"]]
        # distractor rollout first (topology-unrelated), real Oracle fault second
        self.assertEqual(kinds, ["rollout", "oracle_lock"])
        self.assertEqual(spec["steps"][0]["deployment"], "testbed-notification")
        self.assertEqual(spec["steps"][1]["key_value"], "commerce-settlement")
        self.assertEqual(spec["steps"][1]["client_identifier"], "rca-F08-G-oracle-lock")

    def test_f15g_two_independent_lock_roots_same_trace(self) -> None:
        p = multi.CONTRACTS["F15-G"]
        multi.validate("F15-G", p, {})
        spec = decode_spec(multi.build_invocation(plan("F15-G", p), "run"))
        kinds = sorted(s["kind"] for s in spec["steps"])
        self.assertEqual(kinds, ["oracle_lock", "pg_lock"])
        tags = {s.get("application_name") or s.get("client_identifier") for s in spec["steps"]}
        self.assertEqual(tags, {"rca-F15-G-inventory-lock", "rca-F15-G-oracle-lock"})
        # simultaneous (same checkout trace) => zero offsets
        self.assertTrue(all(s["offset_seconds"] == 0 for s in spec["steps"]))

    def test_orchestrator_is_tagged_reverse_and_death_confirmed(self) -> None:
        _, body = multi.build_invocation(plan("F15-G", multi.CONTRACTS["F15-G"]), "run")
        text = body.decode()
        # reverse-order cleanup
        self.assertIn("for ((i=step_count-1; i>=0; i--)); do dispatch release", text)
        # oracle tag via v$session client_identifier + server-side kill (death confirm)
        self.assertIn("dbms_session.set_identifier", text)
        self.assertIn("client_identifier=", text)
        self.assertIn("alter system kill session", text)
        # pg tag via application_name + pg_terminate_backend (death confirm)
        self.assertIn("PGAPPNAME=", text)
        self.assertIn("pg_terminate_backend", text)
        self.assertIn("FREEPDB1", text)
        self.assertIn("FOR UPDATE", text.upper())

    def test_validate_rejects_tampered_parameters(self) -> None:
        bad = copy.deepcopy(multi.CONTRACTS["F08-G"])
        bad["steps"][1]["key_value"] = "some-other-account"
        with self.assertRaises(multi.ExecutorError):
            multi.validate("F08-G", bad, {})

    def test_validate_requires_at_least_one_lock_root(self) -> None:
        allowlisted = {
            "parameter_contract": {"allowed_scenarios": ["F99-X"]},
            "scenario_parameters": {
                "F99-X": {
                    "steps": [
                        {"name": "a", "kind": "rollout", "offset_seconds": 0,
                         "namespace": "ns", "deployment": "d", "annotation_key": "k"},
                        {"name": "b", "kind": "rollout", "offset_seconds": 0,
                         "namespace": "ns", "deployment": "d2", "annotation_key": "k2"},
                    ]
                }
            },
        }
        params = allowlisted["scenario_parameters"]["F99-X"]
        with self.assertRaises(multi.ExecutorError):
            multi.validate("F99-X", params, allowlisted)


if __name__ == "__main__":
    unittest.main()
