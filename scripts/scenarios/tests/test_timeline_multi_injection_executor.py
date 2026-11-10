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
own_body = load("db_operator_remote").without_library


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
        # The shared operator-session and pg lock functions are checked where they live; this looks at the
        # orchestrator's own text and the oracle functions it embeds.
        text = own_body(body.decode())
        # reverse-order cleanup
        self.assertIn("for ((i=step_count-1; i>=0; i--)); do dispatch release", text)
        # oracle: runner-side session tagged through the driver; death confirmed by the
        # holder process ending and the row becoming lockable server-side (v4 C6).
        # The old path exec'd into the Oracle pod as SYS and killed sessions by tag.
        self.assertIn('conn.client_identifier = _env("LOCK_TAG")', text)
        release = text[text.index("oracle() {"):text.index("pg() {")].split("release)")[1].split(";;")[0]
        # local stop first and unconditionally, then the server-side check (review C6 #2)
        self.assertLess(release.index('ora_hold_stop "$run_dir" || rc=1'), release.index('ora_prepare'))
        self.assertIn("ora_released || rc=1", release)
        self.assertIn('return "$rc"', release)
        check = text[text.index("oracle() {"):text.index("pg() {")].split("check)")[1].split(";;")[0]
        self.assertIn('ora_dcd_ok "$ns" "$pod"', check)
        self.assertNotIn("alter system kill session", text)
        self.assertNotIn("sqlplus", text)
        # pg lock: 클러스터 내 클라이언트 파드(psql 하나, attach 로 구동) + 앱 신원 사칭 + idle-in-transaction 유지.
        # 이름으로 세션을 죽이지 않는다(실 서비스를 죽일 수 있다) — 파드 삭제로 정리한다(pg_lock_remote).
        pg = text[text.index("pg() {"):text.index("rollout() {")]
        self.assertIn('pgl_apply "$run_dir" "$schema" "$table" row "" "$keycol" "$keyval" "$hold" "$identity"', pg)
        self.assertIn('pgl_release "$run_dir"', pg)
        self.assertIn('PGL_IMAGE=$(field "$i" image)', pg)
        self.assertNotIn("pg_terminate_backend", text)
        self.assertNotIn("pg_sleep", text)
        full = body.decode()
        self.assertIn("lucida.io/db-client: session", full)
        self.assertIn('command: ["psql", ${args}]', full)
        self.assertIn("pg_lock_remote.bash_functions()", (PROFILES / "timeline_multi_injection_executor.py").read_text())
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

    def test_backend_pid_is_read_from_the_shared_lock_session(self) -> None:
        """pg_lock 스텝은 db.lock 과 같은 잠금 세션(pg_lock_remote)을 쓴다. pid 추출이 실행기마다 따로 있다가
        같은 결함을 네 번 고친 일(2026-07-31, 08-03)이 다시 생기지 않게, 이 실행기에는 제 추출이 없어야 한다.
        세션이 실제 psql 출력에서 pid 를 뽑는 것은 test_registry_contracts 와 test_f06h_db_table_lock 이 본다."""
        script = own_body(multi.ORCHESTRATOR.decode())
        self.assertNotIn('logs "$pod"', script)
        self.assertNotIn("backend pid", script)
        self.assertIn("pgl_apply", script)


if __name__ == "__main__":
    unittest.main()
