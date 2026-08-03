from __future__ import annotations

import base64
import copy
import importlib.util
import json
import subprocess
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
        self.assertEqual(spec["steps"][1]["client_identifier"], "dba-maintenance")

    def test_f15g_two_independent_lock_roots_same_trace(self) -> None:
        p = multi.CONTRACTS["F15-G"]
        multi.validate("F15-G", p, {})
        spec = decode_spec(multi.build_invocation(plan("F15-G", p), "run"))
        kinds = sorted(s["kind"] for s in spec["steps"])
        self.assertEqual(kinds, ["oracle_lock", "pg_lock"])
        # G6/L1: 신원에 시나리오를 인코딩하지 않는다. PG는 실 앱 신원을 사칭하고
        # Oracle은 전 케이스 균일한 상수를 쓴다.
        identities = {s.get("client_identity") or s.get("client_identifier") for s in spec["steps"]}
        self.assertEqual(identities, {"PostgreSQL JDBC Driver", "dba-maintenance"})
        self.assertFalse(any("F15-G" in json.dumps(s) for s in spec["steps"]))
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
        # pg lock: 클러스터 내 클라이언트 파드 + 앱 신원 사칭 + idle-in-transaction 유지.
        # 이름으로 세션을 죽이지 않는다(실 서비스를 죽일 수 있다) — 파드 삭제로 정리한다.
        self.assertIn("PGAPPNAME", text)
        self.assertNotIn("pg_terminate_backend", text)
        self.assertNotIn("pg_sleep", text)
        self.assertIn("idle in transaction", text)
        self.assertIn("lucida.io/db-client: session", text)
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

    def test_backend_pid_is_read_from_real_psql_output(self) -> None:
        """pg_lock 스텝이 실제 psql 로그 모양에서 pid를 뽑아낼 수 있어야 한다.

        psql은 결과보다 `BEGIN` 명령 태그를 먼저 찍는다. 첫 줄만 보던 구현은
        pid를 영영 못 찾고 "did not report a backend pid"로 죽는다. db.lock
        실행기는 2026-07-31에 고쳤지만 이 실행기에는 옮겨지지 않아, F15-G가
        같은 자리에서 죽고 전역 DIRTY로 큐를 세웠다(2026-08-03).
        아래 입력은 그날 파드에서 실측한 로그 그대로다.
        """
        script = multi.ORCHESTRATOR.decode()
        pipeline = next(
            line.split('logs "$pod" 2>/dev/null |', 1)[1].rstrip(')"').strip()
            for line in script.splitlines()
            if 'pid="$(' in line and "logs" in line
        )
        extracted = subprocess.run(
            ["sh", "-c", f"cat | {pipeline}"],
            input="BEGIN\n35250\n1\n",
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(extracted, "35250")


if __name__ == "__main__":
    unittest.main()
