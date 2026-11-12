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

import copy
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

    def test_stated_companions_agree_with_controller_and_catalog(self) -> None:
        """정답지가 말하는 companion 은 실제로 주입되는 것이어야 한다.

        `levels` 는 test_stated_injection_matches_the_live_controller 가 컨트롤러와
        대조하는데 **companions 는 아무도 대조하지 않았다.** 2026-08-07 감사에서
        둘이 나왔다:

          F05-R: 정답지가 `[load.north_south]` 만 적었으나 실제는
                 `[k8s.env, load.north_south]` 다. 같은 레코드 안의 injection_summary
                 는 "k8s.env companion 이 JAVA_TOOL_OPTIONS 에…"라고 정확히 쓴다 —
                 산문이 맞고 구조화 필드가 틀렸다. 그 k8s.env 가 힙을 고정하는
                 두 번째 레버라 이걸 빠뜨리면 정답지가 주입의 절반을 감춘다.
          F14-P: 주입하지도 않는 companion 을 주장했다(실제 companion_refs 는 비었고
                 산문도 "부하는 baseline loadgen 이 공급한다"고 쓴다).

        디스패치는 이 필드를 읽지 않지만(러너는 plan 의 companion_profile_ids 를
        쓴다) 캡처 케이스에 실려 sha256 으로 봉인되므로 채점자가 읽는 면이다.

        카탈로그까지 세 면을 함께 본다 — `catalog.profiles` 는 primary 를 맨 앞에
        둔 전체 목록이므로 `[primary] + companions` 와 정확히 같아야 한다.
        """
        problems: list[str] = []
        for scenario_id in sorted(self.active):
            controller = self.controllers.get(scenario_id)
            if controller is None:
                continue
            profile = controller["profile"]
            companions = list(profile.get("companion_refs") or [])
            stated = (self.metadata[scenario_id].get("injected_fault") or {}).get(
                "companions"
            )
            if stated is None:
                problems.append(f"{scenario_id}: injected_fault.companions 가 없다")
            elif list(stated) != companions:
                problems.append(
                    f"{scenario_id}: 정답지 companions={list(stated)} 인데 "
                    f"컨트롤러 companion_refs={companions}"
                )
            declared = list(self.catalog[scenario_id]["profiles"])
            expected = [profile["primary_ref"], *companions]
            if declared != expected:
                problems.append(
                    f"{scenario_id}: catalog.profiles={declared} 인데 "
                    f"[primary]+companions={expected}"
                )
        self.assertFalse(problems, "\n".join(problems))

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

    def test_scored_app_names_carry_the_observed_service_name(self) -> None:
        # 채점 이름 칸(accept, partial)에 앱 서비스를 앱 별칭이나 Deployment, 컨테이너 이름으로만
        # 두면, 관측 서버 서비스 이름(미끼 정책 targets 의 service_name, otel service_name)으로
        # 짚은 답이 문자열 비교에서 빠진다. 같은 목록에 그 서비스 이름이 함께 있어야 한다. 컨테이너 입도의 accept 는 컨테이너
        # 이름이 정답이라 서비스 이름을 더하면 범위가 넓어지므로 보지 않는다. parked 와 draft 도
        # 되살릴 수 있어 함께 본다.
        policy = _load("registry/decoy-deployments.json")
        domain_namespaces = policy["domain_namespaces"]
        missing = []
        for scenario_id, entry in sorted(self.metadata.items()):
            scoring = entry.get("scoring")
            if not isinstance(scoring, dict):
                continue
            namespace = domain_namespaces.get(entry.get("domain"))
            for kind in ("accept", "partial"):
                if scoring["granularity"] == "container" and kind == "accept":
                    continue
                names = scoring.get(kind, [])
                for target in policy["targets"]:
                    aliases = {*target["aliases"], target["target_id"]}
                    local = {
                        target["deployment"],
                        target["container"],
                        f"{target['deployment']}:{target['container']}",
                        f"deployment/{target['deployment']}",
                        f"{target['namespace']}/deployment/{target['deployment']}",
                    }
                    named = any(n in aliases or (n in local and target["namespace"] == namespace) for n in names)
                    if named and target["service_name"] not in names:
                        missing.append(f"{scenario_id} {kind}: {target['service_name']}")
        self.assertEqual(missing, [], "observed service name missing from a scored name list")

    def test_beyond_data_notes_stay_out_of_the_scored_answer(self) -> None:
        # 참고 메모 칸(beyond_data, beyond_data_ko): 수집 데이터로는 가를 수 없어 정답 칸에
        # 넣지 못한 상세 원인을 연구원이 볼 수 있게 남긴다. 선택 칸이고, capture 가 meta.json
        # scenario_notes 로 따로 싣는다. 채점과 정답 판정이 읽는 칸에 같은 글이 섞이면
        # 관제 데이터로 낼 수 없는 답을 요구하게 되므로(원칙 5) 섞이지 않게 막는다.
        note_keys = ["confirmed_by", "detail", "missing_data"]
        scored = ("title", "description", "cause", "injection_summary", "user_impact",
                  "distinguishing_evidence")
        for scenario_id, entry in sorted(self.metadata.items()):
            present = [key for key in ("beyond_data", "beyond_data_ko") if key in entry]
            if not present:
                continue
            self.assertEqual(present, ["beyond_data", "beyond_data_ko"],
                             f"{scenario_id}: beyond_data needs its _ko pair")
            scored_text = [str(entry.get(field, "")) for field in scored]
            scored_text += [str(entry.get(f"{field}_ko", "")) for field in scored]
            scored_text.append(str((entry.get("root_cause") or {}).get("mechanism", "")))
            for key in present:
                note = entry[key]
                self.assertIsInstance(note, dict, f"{scenario_id}.{key}")
                self.assertEqual(sorted(note), note_keys, f"{scenario_id}.{key}")
                for field, text in note.items():
                    self.assertIsInstance(text, str, f"{scenario_id}.{key}.{field}")
                    self.assertTrue(text.strip(), f"{scenario_id}.{key}.{field}")
                self.assertFalse(
                    any(note["detail"] in text for text in scored_text),
                    f"{scenario_id}.{key}: the note detail must not be copied into a scored field",
                )

    def test_scored_evidence_does_not_cite_runner_only_values(self) -> None:
        # 원칙 5: 채점 칸(must_support, must_rule_out)의 근거는 관제(119)가 수집한 데이터여야
        # 한다. 부하 생성기가 계산한 값(단계별 비율, 달성 요청률, 업무 불변식), 러너 진입 탐침,
        # mock 상태, outbox 미발행 행 같은 앱 DB 업무 행은 관제가 보지 못한다. 그런 근거는
        # 관제 데이터로 같은 사실을 보이는 증거로 바꾸거나 beyond_data 로 옮긴다(2026-11-09 D1).
        # 여기서는 러너 질의 키와 부하기 단계 비율처럼 기계로 가릴 수 있는 표기만 막는다.
        runner_only = re.compile(
            r"loadgen|achieved_rps|business_ok|checkout_invariant|entry_status|published_at"
            r"|step (?:get|transfer)\b|mock_flap|order_duplicate_count|order_create_\w+_rate"
            r"|payment_502_rate|(?:동반 부하|기준선)[^.:]{0,30}(?:비율|실패율)"
            # 러너 판정 키(…_matches_baseline)와, 요청량을 러너 부하 설정(기준선, 동반 부하)에
            # 견주는 문장. 동반 부하는 진입량을 평시의 몇 배로 올리므로 관제 데이터로 가를 수 없다.
            r"|matches_baseline|(?:요청량|요청률|요청 수)[^.:]{0,20}(?:기준선|동반 부하)"
            r"|동반 부하[^.:]{0,12}(?:그대로|만큼)|기준선[^.:]{0,6}그대로"
        )
        # 외부 PG 429 는 payment 의 클라이언트 스팬에만 남는다(PG mock 은 스팬을 내지 않는다).
        # 서버 스팬에 429 가 없다는 문장은 외부 429 시나리오에서도 참이라 감별 근거가 못 된다.
        server_429 = re.compile(r"서버 스팬[^.]{0,20}429")
        for scenario_id, entry in sorted(self.metadata.items()):
            for field in ("must_support", "must_rule_out"):
                for index, text in enumerate(entry.get(field) or []):
                    match = runner_only.search(text)
                    self.assertIsNone(
                        match,
                        f"{scenario_id}.{field}[{index}] cites runner-only evidence: "
                        f"{match.group(0) if match else ''}",
                    )
                    if field == "must_rule_out":
                        self.assertIsNone(
                            server_429.search(text),
                            f"{scenario_id}.{field}[{index}] rules out an external 429 by server spans",
                        )

    def test_rollout_timing_names_the_rolled_out_service(self) -> None:
        # 대상 이름 규약(docs/spec-scenario-authoring.md §1.3, 2026-11-10 D4): 실패 시각을 롤아웃에
        # 견주는 문장은 어느 서비스의 롤아웃인지 관측 서버 서비스 이름으로 적고, 공통 시작 절을
        # 쓴다. v4 녹화에는 경로 밖 미끼 배포(재시작 롤아웃)가 같은 창에 들어가므로 이름 없는
        # "롤아웃 시각" 은 어느 롤아웃인지 가리지 못한다. 이름 없이 쓰면 미끼 계산도 그 문장을
        # 네임스페이스 전체가 바뀌지 않았다는 말로 읽는다. 해시 대상 6칸과 그 _ko 짝, root_cause
        # 는 이 규약 밖이다(바꾸면 녹화 대조가 갈리거나 정답이 바뀐다. 남은 곳은 장부 §6 의
        # 2026-11-10 알려진 위반 목록에 있다).
        policy = _load("registry/decoy-deployments.json")
        services = {target["service_name"] for target in policy["targets"]}
        locked = {"title", "description", "cause", "injection_summary", "user_impact",
                  "distinguishing_evidence", "root_cause", "scoring", "injected_fault"}
        locked |= {f"{field}_ko" for field in list(locked)}
        anchor = re.compile(r"(?:롤아웃|재배포)(?: 시각|에서 옛 파드)")
        # 시작 절은 공통이고, 끝 절은 주입이 실제로 만든 회복 모양대로 쓴다(되돌림, 할당량
        # 삭제, 노드 이미지 재반입 등). 끝 절이 없는 문장만 막는다.
        clause = re.compile(r"롤아웃에서 옛 파드가 내려간 뒤 시작해 [^,]*끝난다")
        loose = re.compile(r"(?:롤아웃|재배포) 시각에 (?:맞춰|시작)")

        def texts(value, path):
            if isinstance(value, str):
                yield path, value
            elif isinstance(value, dict):
                for key, item in value.items():
                    yield from texts(item, f"{path}.{key}")
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    yield from texts(item, f"{path}[{index}]")

        problems: list[str] = []
        for scenario_id, entry in sorted(self.metadata.items()):
            for key, value in entry.items():
                if key in locked:
                    continue
                for path, text in texts(value, f"{scenario_id}.{key}"):
                    for match in anchor.finditer(text):
                        before = text[: match.start()].rstrip().split()
                        if not before or before[-1] not in services:
                            problems.append(f"{path}: 롤아웃 앞에 서비스 이름이 없다: {text[:80]}")
                        if match.group(0).endswith("옛 파드") and not clause.search(text):
                            problems.append(f"{path}: 공통 시작 절이나 끝 절이 없다: {text[:80]}")
                    if loose.search(text):
                        problems.append(f"{path}: 공통 문구 대신 옛 표현: {text[:80]}")
        self.assertFalse(problems, "\n".join(problems))

    def test_story_ko_is_a_display_only_story(self) -> None:
        # 화면 이야기 칸(story_ko): 러너 시나리오 카드가 description 대신 보이는 사람용 글이다.
        # 채점 칸이 아니며 capture 도 싣지 않는다. 작성 규칙은 docs/spec-scenario-story.md.
        # 2026-10-30 전 시나리오를 채운 뒤 필수 칸이 됐다: 새 시나리오가 빠뜨리면 카드가 옛
        # description 으로 돌아가므로 여기서 막는다. 카드가 다섯 제목(개요, 주입 장애,
        # 전파 과정, 근본 원인, 설계 근거)으로 그리므로 모양을 고정하고, 다른 시나리오 ID 는
        # 대조가 필요한 설계 근거에만 둔다. 문서 규칙대로 가운뎃점과 줄표를 쓰지 않는다.
        keys = ["design_notes", "injected_fault", "overview", "propagation", "root_cause"]
        scenario_id_pattern = re.compile(r"\bF\d{2}-[A-Z]\w*")
        for scenario_id, entry in sorted(self.metadata.items()):
            story = entry.get("story_ko")
            self.assertIsNotNone(story, f"{scenario_id}: story_ko is required (docs/spec-scenario-story.md)")
            self.assertIsInstance(story, dict, scenario_id)
            self.assertEqual(sorted(story), keys, scenario_id)
            for field in ("overview", "injected_fault", "root_cause"):
                self.assertIsInstance(story[field], str, f"{scenario_id}.{field}")
                self.assertTrue(story[field].strip(), f"{scenario_id}.{field}")
            stages = story["propagation"]
            self.assertIsInstance(stages, list, scenario_id)
            self.assertTrue(1 <= len(stages) <= 5, f"{scenario_id}: propagation has {len(stages)} stages")
            for stage in stages:
                self.assertEqual(sorted(stage), ["stage", "text"], scenario_id)
                self.assertTrue(stage["stage"].strip() and stage["text"].strip(), scenario_id)
            notes = story["design_notes"]
            self.assertIsInstance(notes, list, scenario_id)
            self.assertTrue(all(isinstance(note, str) and note.strip() for note in notes), scenario_id)
            story_text = [story["overview"], story["injected_fault"], story["root_cause"]]
            story_text += [stage["stage"] + stage["text"] for stage in stages]
            for text in story_text + notes:
                self.assertNotIn("\u00b7", text, f"{scenario_id}: no middle dot in story_ko")
                self.assertNotIn("\u2014", text, f"{scenario_id}: no em dash in story_ko")
            for text in story_text:
                self.assertIsNone(
                    scenario_id_pattern.search(text),
                    f"{scenario_id}: scenario ids belong in design_notes only: {text[:60]}",
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

    def test_answer_keys_do_not_claim_the_unreachable_token_fallback_401(self) -> None:
        # 게이트웨이 AuthGuard.verifyIfWrite 는 verifyToken 을 같은 객체 안에서 직접 부른다(자기 호출).
        # 그래서 verifyToken 에 붙은 user 서킷, 재시도, 401 폴백이 적용되지 않고, user 를 쓸 수 없으면
        # 토큰을 단 쓰기는 401 이 아니라 500 이다. 정답지가 "user 를 못 쓰면 쓰기가 401(fail-close)"
        # 이라고 적으면 녹화에 없는 증거를 정답으로 삼는다(원칙 3). v3 녹화 두 건의 주입 중 401 은 토큰
        # 없이 보낸 쓰기 몫이었고 토큰을 단 쓰기는 500 이었다(2026-11-12 Q18). 코드가 바뀌어 자기 호출이
        # 사라지면 앞 단언이 실패해 정답지를 다시 보게 한다.
        guard = (REPO / "commerce/api-gateway/src/main/java/com/commerce/gateway/service/AuthGuard.java").read_text(
            encoding="utf-8"
        )
        body = re.search(r"public void verifyIfWrite\(.*?\n    }\n", guard, re.S)
        self.assertIsNotNone(body, "AuthGuard.verifyIfWrite not found")
        self.assertRegex(body.group(0), r"=\s*verifyToken\(token\)", "verifyIfWrite no longer self-invokes verifyToken")

        context = re.compile(
            r"user-service|user 서킷|user circuit|verify-token|verifyToken|토큰 확인|token check|"
            r"fail-close|fail closed|AuthGuard|검증기|verifier|쓰기[^.]{0,30}401|\bwrites?\b[^.]{0,30}401",
            re.I,
        )
        # 폴백이 돌지 않는다는 설명이나 401 과 대조하는 문장은 주장이 아니다. "토큰 없는 여정" 처럼
        # 다른 것을 꾸미는 부정(없는)은 주장을 덮지 않게 뺀다.
        negation = re.compile(r"아니|않|없이|없어|\bnot\b|\bno\b|n't|\bnever\b|bypass|instead of 401|rather than 401", re.I)
        problems = []
        for scenario_id in sorted(self.metadata):
            for path, text in self._prose_fields(scenario_id):
                for sentence in re.split(r"(?<=[.。])\s+|\n|; ", text):
                    if "401" in sentence and context.search(sentence) and not negation.search(sentence):
                        problems.append(f"{scenario_id}.{path}: {sentence[:120]}")
        self.assertEqual(problems, [], "answer key claims a 401 the self-invoked token check never returns")

    # ------------------------------------------------------------------ 계기 칸(원칙 6)
    # 설계 원칙 6: root_cause.trigger_target_id 는 결함을 드러나게 한 사건이 난 곳이고 근본과 같으면
    # 비운다. 피해가 드러나는 증상 서비스는 이 칸이 아니라 scoring.partial 에 둔다. 2026-11-12 까지
    # 34건 중 31건이 증상 서비스나 두 번째 근본을 이 칸에 적고 있었다(v4 TRIG). 아래 셋은 기계로
    # 가를 수 있는 몫만 본다. 사건이 정말 계기인지(정당한 작업이 숨은 결함을 드러냈는지)는 사람이 본다.

    def _place_names(self, name: str, namespaces: set[str]) -> set[tuple[str, str]]:
        """한 이름이 가리키는 곳들. 앱은 미끼 정책 targets 의 네임스페이스/Deployment 로 모은다.
        Deployment, 컨테이너 이름은 두 도메인이 함께 쓰므로 주어진 네임스페이스 안에서만 앱으로 읽는다.
        제품 대상 이름 꼴 `<네임스페이스>/<Deployment>` 는 그 네임스페이스의 그 대상 하나다."""
        policy = _load("registry/decoy-deployments.json")
        places: set[tuple[str, str]] = {("name", name.lower())}
        base, _, rest = name.partition(":")
        scoped_ns, slash, scoped = base.partition("/")
        if slash and scoped_ns in policy["domain_namespaces"].values():
            namespaces = {scoped_ns}
            base = scoped.removeprefix("deployment/")
        for target in policy["targets"]:
            key = f"{target['namespace']}/{target['deployment']}"
            if name in {target["service_name"], target["target_id"], *target["aliases"]}:
                places.add(("app", key))
            local = {
                target["deployment"],
                target["container"],
                f"{target['deployment']}:{target['container']}",
                f"deployment/{target['deployment']}",
            }
            if target["namespace"] in namespaces and (name in local or base in local):
                places.add(("app", key))
        if rest and "." in rest:
            places.add(("table", rest.rsplit(".", 1)[1].lower()))
        if "/job/" in name:
            places.add(("job", name.rsplit("/", 1)[1]))
        return places

    def _injection_places(self, scenario_id: str, entry: dict) -> set[tuple[str, str]]:
        """injected_fault 매개변수가 손댄 곳들(Deployment, 노드, 표, Job).

        Deployment, 컨테이너 이름은 같은 딕셔너리 안의 짝 네임스페이스로만 앱으로 읽는다:
        `<접두>_deployment` 는 `<접두>_namespace`(접두를 뒤에서부터 줄여 찾음), 없으면 그 딕셔너리의
        `namespace`, 그것도 없으면 바깥 딕셔너리의 것, 끝으로 시나리오 도메인 네임스페이스. 두
        도메인을 겹친 시나리오(F15 계열)에서 한쪽 Deployment 이름이 다른 도메인의 같은 이름 앱을
        "주입이 손댄 곳" 으로 만들지 않게 하려는 것이다(2026-11-12 rv-trig 중 1)."""
        locations = _load("registry/locations.json")["locations"]
        host_node = {row["host"]: row["node"] for row in locations.values() if row.get("node") and row.get("host")}
        home = _load("registry/decoy-deployments.json")["domain_namespaces"].get(entry.get("domain"), "")
        places: set[tuple[str, str]] = set()

        def namespace_for(key: str, node: dict, inherited: str) -> str:
            parts = key.split("_")
            for size in range(len(parts) - 1, 0, -1):
                paired = node.get("_".join(parts[:size]) + "_namespace")
                if isinstance(paired, str):
                    return paired
            own = node.get("namespace")
            return own if isinstance(own, str) else inherited

        def visit(key: str, value: str, namespace: str) -> None:
            if value in host_node:
                places.add(("name", host_node[value].lower()))
            if key.endswith("table"):
                places.add(("table", value.lower()))
            if key.endswith("job"):
                places.add(("job", value))
            if key.endswith(("deployment", "container", "resource", "node", "host")):
                places.update(self._place_names(value, {namespace}))

        def walk(node, inherited: str) -> None:
            if isinstance(node, dict):
                own = node.get("namespace")
                here = own if isinstance(own, str) else inherited
                for key, child in node.items():
                    if isinstance(child, str):
                        visit(key, child, namespace_for(key, node, here))
                    elif isinstance(child, list) and all(isinstance(item, str) for item in child):
                        for item in child:
                            visit(key, item, namespace_for(key, node, here))
                    else:
                        walk(child, here)
            elif isinstance(node, list):
                for child in node:
                    walk(child, inherited)

        for level in (entry.get("injected_fault") or {}).get("levels") or []:
            walk(level.get("parameters"), home)
        return places

    def _home(self, entry: dict) -> set[str]:
        return {_load("registry/decoy-deployments.json")["domain_namespaces"].get(entry.get("domain"), "")}

    def _trigger_problems(self, scenario_id: str, entry: dict) -> list[str]:
        """계기 칸 규칙 셋(근본과 다름, 주입이 손댄 곳, 부분 점수)의 위반."""
        root_cause = entry.get("root_cause")
        if not isinstance(root_cause, dict) or root_cause.get("trigger_target_id") is None:
            return []
        trigger = root_cause["trigger_target_id"]
        if not isinstance(trigger, str) or not trigger.strip() or " " in trigger:
            return [f"{scenario_id}: trigger must be null or one identifier, got {trigger!r}"]
        problems = []
        home = self._home(entry)
        names = self._place_names(trigger, home)
        same = names & self._place_names(root_cause["target_id"], home)
        if same:
            problems.append(f"{scenario_id}: trigger {trigger} is the root ({sorted(same)})")
        if not names & self._injection_places(scenario_id, entry):
            problems.append(f"{scenario_id}: trigger {trigger} is not a place the injection acted on")
        scoring = entry.get("scoring") or {}
        if trigger not in (scoring.get("partial") or []):
            problems.append(f"{scenario_id}: trigger {trigger} missing from partial")
        if trigger in (scoring.get("accept") or []):
            problems.append(f"{scenario_id}: trigger {trigger} is accepted as a root")
        return problems

    def _accept_problems(self, scenario_id: str, entry: dict) -> list[str]:
        """accept 의 앱이 근본도 아니고 주입이 손댄 곳도 아니면 위반."""
        root_cause = entry.get("root_cause")
        scoring = entry.get("scoring")
        if not isinstance(root_cause, dict) or not isinstance(scoring, dict):
            return []
        home = self._home(entry)
        root = self._place_names(root_cause["target_id"], home)
        injected = self._injection_places(scenario_id, entry)
        problems = []
        for name in scoring.get("accept") or []:
            places = self._place_names(name, home)
            if not any(kind == "app" for kind, _ in places) or places & root:
                continue
            if not places & injected:
                problems.append(f"{scenario_id}: accepted {name} is neither the root nor injected")
        return problems

    def test_trigger_is_empty_or_a_place_other_than_the_root(self) -> None:
        # 근본과 같은 곳을 계기에 적지 않고 비운다. 빈 문자열은 null 과 같은 뜻을 두 꼴로 적는 것이라
        # 받지 않는다. parked, draft 도 되살릴 수 있어 함께 본다.
        problems = [
            problem
            for scenario_id, entry in sorted(self.metadata.items())
            for problem in self._trigger_problems(scenario_id, entry)
            if "is the root" in problem or "must be null" in problem
        ]
        self.assertEqual(problems, [], "trigger_target_id names the root cause; leave it null")

    def test_trigger_is_where_the_injection_acted(self) -> None:
        # 계기는 주입이 실제로 손댄 곳(Deployment, 노드, 표, Job) 가운데 하나여야 한다. 증상 서비스는
        # 주입이 손대지 않은 곳이라 여기서 걸린다(2026-11-12 까지의 commerce-order, commerce-payment 꼴).
        # 정답지에 주입이 없으면 계기를 확인할 수 없어 비워 둔다.
        problems = [
            problem
            for scenario_id, entry in sorted(self.metadata.items())
            for problem in self._trigger_problems(scenario_id, entry)
            if "not a place" in problem
        ]
        self.assertEqual(problems, [], "trigger_target_id must name where the injection acted")

    def test_a_trigger_only_answer_earns_partial_not_full_credit(self) -> None:
        # 원칙 6 의 partial 정의(증상 서비스, 계기만 본 답). 계기를 accept 에 두면 그것은 계기가 아니라
        # 또 하나의 근본이다(두 장애를 겹친 시나리오의 두 번째 근본은 accept 에 두고 계기는 비운다).
        problems = [
            problem
            for scenario_id, entry in sorted(self.metadata.items())
            for problem in self._trigger_problems(scenario_id, entry)
            if "partial" in problem or "accepted as a root" in problem
        ]
        self.assertEqual(problems, [], "a trigger-only answer is partial credit")

    def test_an_accepted_app_is_the_root_or_where_the_injection_acted(self) -> None:
        # 원칙 6: 증상 서비스는 partial 이다. accept 에 둔 앱 서비스가 근본도 아니고 주입이 손댄 곳도
        # 아니면 증상이나 통로로 본다(2026-11-12 까지 F15-H, F15-T2 의 food payment: 외부 PG 의 429 를
        # 그대로 전파한 통로였다, v4 TRIG). 두 장애를 겹친 시나리오의 두 번째 근본은 주입이 손댄 곳이라
        # 통과한다(F15-T1 food payment 메모리 한도). 앱이 아닌 대상(DB 표, 노드, mock)은 보지 않는다.
        problems = [
            problem
            for scenario_id, entry in sorted(self.metadata.items())
            for problem in self._accept_problems(scenario_id, entry)
        ]
        self.assertEqual(problems, [], "an accepted app that the fault only passed through belongs in partial")

    def test_two_domain_scenarios_do_not_lend_a_deployment_name_across_domains(self) -> None:
        # rv-trig 중 1 의 변이 V1, V3: F15-T1 의 주입은 food 의 testbed-payment 를 손댄다. commerce 의
        # 같은 이름 앱(commerce-payment)은 손대지 않았으므로 accept 나 계기에 두면 걸려야 한다.
        entry = copy.deepcopy(self.metadata["F15-T1"])
        self.assertEqual(self._accept_problems("F15-T1", entry), [])
        self.assertIn(("app", "rca-testbed-food/testbed-payment"), self._injection_places("F15-T1", entry))
        self.assertNotIn(("app", "rca-testbed-commerce/testbed-payment"), self._injection_places("F15-T1", entry))

        v1 = copy.deepcopy(entry)
        v1["scoring"]["accept"].append("commerce-payment")
        self.assertEqual(
            self._accept_problems("F15-T1", v1),
            ["F15-T1: accepted commerce-payment is neither the root nor injected"],
        )

        v3 = copy.deepcopy(entry)
        v3["root_cause"]["trigger_target_id"] = "commerce-payment"
        v3["scoring"]["partial"].append("commerce-payment")
        self.assertIn(
            "F15-T1: trigger commerce-payment is not a place the injection acted on",
            self._trigger_problems("F15-T1", v3),
        )
        # 같은 자리에 food 쪽 이름을 두면 주입이 손댄 곳이라 그 규칙은 지나간다(근본과 accept 규칙만 남음).
        food = copy.deepcopy(entry)
        food["root_cause"]["trigger_target_id"] = "rca-testbed-food/testbed-payment"
        food["scoring"]["partial"].append("rca-testbed-food/testbed-payment")
        self.assertFalse(
            [p for p in self._trigger_problems("F15-T1", food) if "not a place" in p],
        )

    def test_external_pg_mocks_are_scored_by_their_product_target_names(self) -> None:
        # 채점 이름은 제품 대상 이름(119 targets.name)이 정본이다. 두 외부 PG mock 은 APM 피호출 주소
        # (server.address)와 Deployment 이름이 모두 testbed-external-pg-mock 이라 그 이름으로는 가를 수
        # 없고, 대상 이름이 rca-testbed-commerce/testbed-external-pg-mock 과
        # rca-testbed-food/testbed-external-pg-mock 으로 가른다(2026-11-12 v4 TRIG 실측). 두 mock 모두
        # 대상 이름으로만 적고, 공용 짧은 이름과 관제 데이터에 없는 옛 이름은 어느 도메인에서도 쓰지 않는다.
        retired = {"testbed-external-pg-mock", "food-external-pg-mock", "food-external-pg", "external-pg"}
        problems = []
        for scenario_id, entry in sorted(self.metadata.items()):
            root_cause = entry.get("root_cause") or {}
            scoring = entry.get("scoring") or {}
            names = [root_cause.get("target_id"), root_cause.get("trigger_target_id")]
            names += list(scoring.get("accept") or []) + list(scoring.get("partial") or [])
            for name in names:
                if name in retired:
                    problems.append(f"{scenario_id}: {name} is not a product target name")
                if isinstance(name, str) and name.endswith("external-pg-mock") and name not in {
                    "rca-testbed-commerce/testbed-external-pg-mock",
                    "rca-testbed-food/testbed-external-pg-mock",
                }:
                    problems.append(f"{scenario_id}: {name} is not one of the two mock target names")
        self.assertEqual(problems, [], "external PG mocks must carry their product target names")

if __name__ == "__main__":
    unittest.main()
