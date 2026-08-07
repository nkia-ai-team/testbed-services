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
        cls.profiles = _load("registry/profiles.json")
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

    def test_injection_summary_names_a_profile_the_scenario_actually_binds(self) -> None:
        # 정답지 산문이 실제로 주입하지 않는 실행기를 설명하고 있었다(2026-08-07 실측 3건).
        # 구조화된 injected_fault.levels 는 컨트롤러와 대조되지만 산문은 아무도 안 봐서
        # 조용히 낡는다 — 그런데 데이터셋을 채점하는 사람이 읽는 것은 산문 쪽이다.
        #   F12-H: "k8s.resource" 라고 적었으나 실제 primary 는 k8s.patch
        #   F15-R: "timeline.flap" — 레지스트리에 아예 없는 프로파일 이름
        #   F18-P: "k8s.env … rollout" — e1f17b6 이 버린 옛 설계 그대로. 그 설계는
        #          maxSurge=0 에서 자기 감별자를 켜서 폐기된 것이라 특히 오도적이다.
        # 산문이 primary 대신 companion 을 먼저 소개하는 것은 정상이다(F11-R) —
        # 그래서 검사는 "바인딩된 프로파일 중 하나인가"이지 "primary 인가"가 아니다.
        known_profiles = set(self.profiles["profiles"])
        for scenario_id in sorted(self.active):
            summary = self.metadata[scenario_id].get("injection_summary")
            if not summary:
                continue
            match = re.match(r"\s*([a-z0-9][a-z0-9._]*)\s+executor", summary)
            if match is None:
                continue
            named = match.group(1)
            self.assertIn(
                named,
                known_profiles,
                f"{scenario_id}: injection_summary names profile {named!r}, "
                "which is not in the profile registry",
            )
            controller = self.controllers.get(scenario_id)
            if controller is None:
                continue  # 컨트롤러가 없는 활성 항목은 바인딩을 대조할 대상이 없다
            profile = controller["profile"]
            bound = {profile["primary_ref"], *(profile.get("companion_refs") or [])}
            self.assertIn(
                named,
                bound,
                f"{scenario_id}: injection_summary names {named!r} but the scenario "
                f"binds {sorted(bound)}",
            )

    def test_prose_ladders_match_the_contract_ladder(self) -> None:
        """산문이 사다리를 나열하면 그 값은 계약에 실재해야 한다.

        구조화된 injected_fault.levels 는 컨트롤러와 대조되지만 산문은 아무도
        대조하지 않아, 사다리를 고칠 때마다 조용히 낡는다. 2026-08-07 감사에서
        5건이 나왔다 — F05-R(뺀 768Mi 단이 남음)·F09-H(사다리도 GC 플래그도
        다름)·F12-H(cause 만 옛 값)·F25-H(사다리 전환 전 고정값). 채점자가 읽는
        것은 산문 쪽이므로 이 드리프트는 데이터셋 오염이다.

        범위는 의도적으로 좁다. "A→B→C" 처럼 **나열로 쓴 것**만 본다 —
        서술 속의 단일 값("한도 1Gi")이나 측정 서술("anon 413MiB")은 계약 밖의
        사실을 정당하게 인용할 수 있어 대조 대상이 아니다. 나열은 사다리를
        옮겨 적은 것이므로 계약과 1:1이어야 한다.
        """
        sequence = re.compile(
            r"(\d+)\s*(Mi|Gi|m|rps)?\s*(?:(?:→|->|/)\s*(\d+)\s*(Mi|Gi|m|rps)?\s*){1,5}"
        )
        problems: list[str] = []
        for scenario_id in sorted(self.active):
            controller = self.controllers.get(scenario_id)
            if controller is None:
                continue
            memory, cpu, plain = self._contract_magnitudes(scenario_id, controller)
            for field, text in self._prose_fields(scenario_id):
                for match in sequence.finditer(text):
                    span = match.group(0)
                    values = [
                        (int(number), unit)
                        for number, unit in re.findall(r"(\d+)\s*(Mi|Gi|m|rps)?", span)
                        if number
                    ]
                    if len(values) < 2:
                        continue
                    trailing = re.match(r"\s*(MiB|Mi|Gi|rps|m)\b", text[match.end():])
                    unit = next((u for _, u in reversed(values) if u), None) or (
                        trailing.group(1) if trailing else None
                    )
                    unit = "Mi" if unit == "MiB" else unit
                    if unit not in ("Mi", "Gi", "m"):
                        continue  # rps·무단위 나열은 사다리가 아닌 서술일 수 있다
                    for number, own in values:
                        own = own or unit
                        if own in ("Mi", "Gi"):
                            mib = number * 1024 if own == "Gi" else number
                            ok = mib in memory or number in plain
                        else:
                            ok = number in cpu or number in plain
                        if not ok:
                            problems.append(
                                f"{scenario_id}.{field}: '{span.strip()}' cites "
                                f"{number}{own}, which the contract does not contain"
                            )
        self.assertFalse(problems, "\n".join(problems))

    def _contract_magnitudes(self, scenario_id: str, controller: dict):
        """이 시나리오의 계약에 실재하는 크기들 — 메모리(MiB)·CPU(밀리코어)·생값."""
        raw: set = set()

        def walk(node) -> None:
            if isinstance(node, dict):
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
            else:
                raw.add(node)

        walk(controller)
        for profile in self.profiles["profiles"].values():
            walk(profile.get("scenario_parameters", {}).get(scenario_id))
            walk(profile.get("scenario_levels", {}).get(scenario_id))

        memory: set[int] = set()
        cpu: set[int] = set()
        plain: set[int] = set()
        for value in raw:
            if isinstance(value, str):
                match = re.fullmatch(r"(\d+)(Mi|Gi|m)", value)
                if match:
                    number, unit = int(match.group(1)), match.group(2)
                    if unit == "Gi":
                        memory.add(number * 1024)
                    elif unit == "Mi":
                        memory.add(number)
                    else:
                        cpu.add(number)
                # JVM 힙 플래그도 계약이다(F05-R·F09-H가 이 경로로 힙을 고정한다)
                for heap in re.findall(r"-Xm[sx](\d+)m", value):
                    memory.add(int(heap))
            elif isinstance(value, int) and not isinstance(value, bool):
                plain.add(value)
                if value >= 1048576 and value % 1048576 == 0:
                    memory.add(value // 1048576)  # 바이트로 적힌 임계
        return memory, cpu, plain

    def _prose_fields(self, scenario_id: str):
        """정답지의 사람이 읽는 필드 전부(구조화된 injected_fault 는 제외)."""
        out: list[tuple[str, str]] = []

        def walk(prefix: str, node) -> None:
            if isinstance(node, str):
                out.append((prefix, node))
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(f"{prefix}[{index}]", value)
            elif isinstance(node, dict):
                for key, value in node.items():
                    if key == "injected_fault":
                        continue
                    walk(f"{prefix}.{key}" if prefix else key, value)

        walk("", self.metadata.get(scenario_id) or {})
        return out


if __name__ == "__main__":
    unittest.main()
