"""audit-run-causality.py 가드.

이 감사기가 틀리면 "통과가 진짜인가"라는 질문 자체가 틀린 답을 받는다. 그래서
오늘 실제로 잡아낸 오작동 두 개를 회귀로 고정한다:

  · 진입 서비스를 시나리오 domain 으로 고르면 교차 도메인에서 조용히 0건이 된다
    (F17-R: domain=core-banking 인데 판정 신호는 commerce 의 checkout_5xx_rate).
  · 정답지의 짧은 이름(`food-order`)을 APM 이름(`food-delivery-order`)으로 못 바꾸면
    대상 집합이 통째로 비어 모든 run 이 "판정 불가"가 된다.

라이브 ClickHouse 없이 돈다 — 질의는 주입한다.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("audit_run_causality", ROOT / "audit-run-causality.py")
assert _spec and _spec.loader
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)


KNOWN = {"commerce-order", "commerce-payment", "food-delivery-order",
         "food-delivery-payment", "core-banking-transfer", "core-banking-ledger"}


def tick(elapsed: int, at: str, streaks: dict[str, int], signals: dict | None = None) -> dict:
    return {"elapsed_sec": elapsed, "at": at, "phase": "evaluating",
            "streaks": streaks, "signals": signals or {}}


class SignalClassificationTests(unittest.TestCase):
    def test_failure_rate_family_is_recognised(self) -> None:
        for signal in ("checkout_5xx_rate", "order_create_5xx_rate", "payment_error_rate",
                       "transfer_2xx_rate", "order_error_rate"):
            self.assertTrue(audit.is_failure_rate(signal), signal)

    def test_latency_and_resource_signals_are_not_failure_rate(self) -> None:
        # 지연·자원 계열은 상류 고장을 그대로 흡수하지 않으므로 이 검사 대상이 아니다.
        for signal in ("order_p95", "node_mem_util", "disk_io_util", "achieved_rps",
                       "order_hikari_pending"):
            self.assertFalse(audit.is_failure_rate(signal), signal)


class NameNormalisationTests(unittest.TestCase):
    def test_answer_key_short_names_map_to_apm_names(self) -> None:
        self.assertEqual(audit.to_apm_service("food-payment", "food-delivery", KNOWN),
                         "food-delivery-payment")
        self.assertEqual(audit.to_apm_service("food-order", "food-delivery", KNOWN),
                         "food-delivery-order")

    def test_already_apm_names_pass_through_and_suffixes_are_stripped(self) -> None:
        self.assertEqual(audit.to_apm_service("commerce-payment", "commerce", KNOWN),
                         "commerce-payment")
        # commerce-postgres:schema.table 는 DB 라 APM 스팬이 없다 → None
        self.assertIsNone(audit.to_apm_service("commerce-postgres:inventory_schema.inventory",
                                               "commerce", KNOWN))

    def test_non_apm_targets_are_dropped_rather_than_guessed(self) -> None:
        # mock 은 otel 에이전트가 없다. 억지로 매핑하면 없는 근거를 만들어낸다.
        self.assertIsNone(audit.to_apm_service("testbed-external-pg-mock", "food-delivery", KNOWN))


class EntryServiceTests(unittest.TestCase):
    def test_signal_wins_over_domain_for_cross_domain_scenarios(self) -> None:
        # F17-R 회귀: 주입은 banking 인데 판정 신호는 commerce 체크아웃이다.
        self.assertEqual(
            audit.entry_service_for(["checkout_5xx_rate"], "core-banking"), "commerce-order")

    def test_domain_is_the_fallback(self) -> None:
        self.assertEqual(audit.entry_service_for(["something_else_rate"], "food-delivery"),
                         "food-delivery-order")

    def test_entry_service_is_excluded_from_targets(self) -> None:
        # 진입 서비스를 대상에 남기면 모든 실패 트레이스가 대상을 포함해 비율이 1.0 이
        # 되고 검사가 공허해진다.
        metadata = {"F19-P": {"domain": "food-delivery",
                              "root_cause": {"target_id": "testbed-external-pg-mock",
                                             "trigger_target_id": "food-order"},
                              "scoring": {"accept": ["testbed-external-pg-mock"],
                                          "partial": ["food-order", "food-payment"]}}}
        targets, domain = audit.injection_targets("F19-P", metadata, KNOWN, "food-delivery-order")
        self.assertEqual(targets, {"food-delivery-payment"})
        self.assertEqual(domain, "food-delivery")


class VerdictWindowTests(unittest.TestCase):
    def test_window_is_the_streak_that_confirmed_the_verdict(self) -> None:
        ticks = [
            tick(0, "2026-08-07T00:00:00Z", {"success": 0}),
            tick(15, "2026-08-07T00:00:15Z", {"success": 0}),
            tick(30, "2026-08-07T00:00:30Z", {"success": 1}),
            tick(45, "2026-08-07T00:00:45Z", {"success": 2}),
            tick(60, "2026-08-07T00:01:00Z", {"success": 3}),
            tick(75, "2026-08-07T00:01:15Z", {"success": 4}),
        ]
        run = {"ticks": ticks}
        start, end, span = audit.verdict_window(run, "success", 3)
        self.assertEqual((start, end, span), ("2026-08-07T00:00:30Z", "2026-08-07T00:01:00Z", 3))

    def test_missing_confirmation_is_an_error_not_a_guess(self) -> None:
        run = {"ticks": [tick(0, "2026-08-07T00:00:00Z", {"success": 1})]}
        with self.assertRaises(audit.AuditError):
            audit.verdict_window(run, "success", 3)


class CausalCheckTests(unittest.TestCase):
    def _check(self, failed: int, touching: int):
        return audit.causal_check(
            start_at="2026-08-07T00:00:00Z", end_at="2026-08-07T00:01:00Z",
            entry_service="commerce-order", targets={"commerce-payment"},
            query=lambda sql: [{"failed_traces": failed, "traces_touching_target": touching}])

    def test_absent_when_no_failure_trace_touches_the_target(self) -> None:
        # F19 가 정확히 이 모양이었다: 482건 중 0건.
        result = self._check(482, 0)
        self.assertEqual(result["status"], "causality_absent")
        self.assertEqual(result["ratio"], 0.0)
        self.assertEqual(result["confidence"], "high")

    def test_present_when_most_failures_reach_the_target(self) -> None:
        result = self._check(100, 90)
        self.assertEqual(result["status"], "causality_present")
        self.assertEqual(result["confidence"], "high")

    def test_partial_sits_between(self) -> None:
        self.assertEqual(self._check(169, 18)["status"], "causality_partial")

    def test_small_sample_is_undetermined_not_a_verdict(self) -> None:
        result = self._check(5, 0)
        self.assertEqual(result["status"], "undetermined")
        self.assertIn("표본 부족", result["reason"])

    def test_zero_failures_is_undetermined_not_absent(self) -> None:
        # 실패 자체가 없으면 "인과 없음"이 아니라 "물어볼 게 없음"이다.
        self.assertEqual(self._check(0, 0)["status"], "undetermined")

    def test_low_confidence_is_flagged_below_rule_of_three(self) -> None:
        result = self._check(23, 0)
        self.assertEqual(result["status"], "causality_absent")
        self.assertEqual(result["confidence"], "low")
        self.assertIn("rule of three", result["confidence_note"])

    def test_no_apm_target_is_undetermined(self) -> None:
        result = audit.causal_check(
            start_at="2026-08-07T00:00:00Z", end_at="2026-08-07T00:01:00Z",
            entry_service="commerce-order", targets=set(), query=lambda sql: [])
        self.assertEqual(result["status"], "undetermined")

    def test_service_names_are_quoted_into_sql(self) -> None:
        captured = {}

        def query(sql):
            captured["sql"] = sql
            return [{"failed_traces": 100, "traces_touching_target": 0}]

        audit.causal_check(start_at="2026-08-07T00:00:00Z", end_at="2026-08-07T00:01:00Z",
                           entry_service="commerce-order", targets={"o'brien"}, query=query)
        self.assertIn("'o''brien'", captured["sql"])


class ControlArmTests(unittest.TestCase):
    def test_baseline_without_denominator_is_uninterpretable(self) -> None:
        run = {"ticks": [tick(0, "t", {}, {"checkout_5xx_rate_baseline": {"value": 0.0}})]}
        arm = audit.control_arm(run, 0)
        self.assertEqual(arm["status"], "uninterpretable")
        self.assertIn("요청 없음", arm["reason"])

    def test_denominator_makes_it_interpretable_without_code_change(self) -> None:
        # 다음 배포에서 분모가 실리면 코드를 안 고쳐도 해석 가능해져야 한다 —
        # 조건이 데이터에 있지 코드에 박혀 있지 않기 때문이다.
        run = {"ticks": [tick(0, "t", {}, {"checkout_5xx_rate_baseline": {"value": 0.0},
                                           "checkout_count": {"value": 120}})]}
        arm = audit.control_arm(run, 0)
        self.assertEqual(arm["status"], "interpretable")
        self.assertEqual(arm["denominators"], {"checkout_count": 120})

    def test_absent_control_arm_is_reported_as_absent(self) -> None:
        run = {"ticks": [tick(0, "t", {}, {"order_p95": {"value": 1.0}})]}
        self.assertEqual(audit.control_arm(run, 0)["status"], "absent")


class DecidingGateTests(unittest.TestCase):
    def test_gate_is_derived_from_outcome_and_reason(self) -> None:
        def run(outcome, reason):
            return {"result": {"outcome": outcome},
                    "decisions": {"controller_state": {"reason": reason}}}

        self.assertEqual(audit.deciding_gate(run("succeeded", "success")), "success")
        self.assertEqual(audit.deciding_gate(run("aborted", "must_rule_out_detected")), "must_rule_out")
        self.assertEqual(audit.deciding_gate(run("aborted", "abort_condition")), "abort")
        # 타임아웃·사다리 소진은 게이트가 만든 판정이 아니다 → 인과를 물을 대상이 아니다.
        self.assertIsNone(audit.deciding_gate(run("failed", "calibration_levels_exhausted")))
        self.assertIsNone(audit.deciding_gate(run("failed", "evaluation_level_timeout")))


class RegistryDerivationTests(unittest.TestCase):
    def test_apm_service_set_comes_from_the_registry(self) -> None:
        # 러너 상수를 베끼지 않는다 — 베끼면 한쪽이 바뀔 때 조용히 어긋난다.
        services = audit.apm_services()
        self.assertIn("commerce-order", services)
        self.assertIn("food-delivery-payment", services)

    def test_every_signal_entry_mapping_points_at_a_real_service(self) -> None:
        services = audit.apm_services()
        for signal, service in audit.SIGNAL_ENTRY_SERVICE.items():
            self.assertIn(service, services, f"{signal} → {service} 가 레지스트리에 없다")


if __name__ == "__main__":
    unittest.main()
