"""녹화본 stream-anomaly 정지 판정 (scripts/scenarios/stream-anomaly-stall-check.py)."""
from __future__ import annotations

import datetime as dt
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("stream_anomaly_stall_check", ROOT / "stream-anomaly-stall-check.py")
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

CS = dt.datetime(2026, 10, 1, 0, 0, tzinfo=dt.timezone.utc)
CE = CS + dt.timedelta(minutes=170)
T1 = CS + dt.timedelta(minutes=130)
T2 = CS + dt.timedelta(minutes=145)
META = {
    "capture_start": "2026-10-01T00:00:00Z",
    "capture_end": "2026-10-01T02:50:00Z",
    "t1": "2026-10-01T02:10:00Z",
    "t2": "2026-10-01T02:25:00Z",
}
FULL_RANGE = (CS, CE)


def _every(metric, start_min, end_min, step_min=1):
    return [(CS + dt.timedelta(minutes=m), metric) for m in range(start_min, end_min + 1, step_min)]


def _healthy():
    return (
        _every("sms.server.cpu.usage", 0, 170)
        + _every("nms.network.if.in_octets", 0, 170, 3)
        + _every("dpm.mysql.sql.select_count", 0, 170, 10)
    )


class StreamAnomalyStallCheckTest(unittest.TestCase):
    def test_healthy_recording_is_normal(self):
        row = check.classify(META, _healthy(), FULL_RANGE)
        self.assertEqual(row["verdict"], check.NORMAL)
        self.assertLessEqual(row["host_quiet_minutes"], 3)
        self.assertEqual(row["families"]["sms"]["in_fault_window"], 16)

    def test_host_paths_silent_through_fault_window_is_stopped(self):
        events = (
            _every("sms.server.cpu.usage", 0, 60)
            + _every("nms.network.if.in_octets", 0, 58, 3)
            + _every("dpm.mysql.sql.select_count", 0, 170, 10)
        )
        row = check.classify(META, events, FULL_RANGE)
        self.assertEqual(row["verdict"], check.STOPPED)
        self.assertEqual(row["host_quiet_minutes"], 110.0)
        self.assertEqual(row["host_quiet_from"], CS + dt.timedelta(minutes=60))
        self.assertEqual(row["host_quiet_to"], CE)
        self.assertTrue(row["quiet_covers_fault_window"])
        self.assertEqual(row["families"]["sms"]["in_fault_window"], 0)
        self.assertEqual(row["families"]["dpm"]["in_fault_window"], 2)

    def test_stall_in_normal_phase_that_recovers_is_stopped_but_not_covering(self):
        events = [e for e in _healthy() if not (20 <= (e[0] - CS).total_seconds() / 60 <= 80 and e[1][:3] in ("sms", "nms"))]
        row = check.classify(META, events, FULL_RANGE)
        self.assertEqual(row["verdict"], check.STOPPED)
        self.assertFalse(row["quiet_covers_fault_window"])

    def test_whole_recording_without_host_events_is_stopped(self):
        events = _every("dpm.mysql.sql.select_count", 0, 170, 10)
        row = check.classify(META, events, FULL_RANGE)
        self.assertEqual(row["verdict"], check.STOPPED)
        self.assertEqual(row["host_quiet_minutes"], 170.0)

    def test_short_trailing_gap_is_ambiguous(self):
        events = _every("sms.server.cpu.usage", 0, 150) + _every("nms.network.if.in_octets", 0, 150, 3)
        row = check.classify(META, events, FULL_RANGE)
        self.assertEqual(row["verdict"], check.AMBIGUOUS)
        self.assertEqual(row["host_quiet_minutes"], 20.0)

    def test_sms_only_silence_is_ambiguous(self):
        events = _every("sms.server.cpu.usage", 0, 60) + _every("nms.network.if.in_octets", 0, 170, 3)
        row = check.classify(META, events, FULL_RANGE)
        self.assertEqual(row["verdict"], check.AMBIGUOUS)
        self.assertIn("sms 만", row["reason"])

    def test_event_table_not_covering_capture_is_ambiguous(self):
        events = _every("dpm.mysql.sql.select_count", 0, 100, 10)
        row = check.classify(META, events, (CS, CS + dt.timedelta(minutes=100)))
        self.assertEqual(row["verdict"], check.AMBIGUOUS)
        self.assertIn("덮지 못함", row["reason"])
        self.assertEqual(check.classify(META, [], None)["verdict"], check.AMBIGUOUS)

    def test_sparse_events_with_live_sms_bands_are_not_stopped(self):
        # nms 경로는 녹화 내내 없고 sms 는 드물게 내다가 장애 구간에 몰아 낸 녹화: sms 밴드가 이어지면 정지가 아니다.
        events = (
            _every("sms.server.cpu.usage", 0, 40, 5)
            + _every("sms.server.cpu.usage", 131, 144)
            + _every("dpm.mysql.sql.select_count", 0, 170, 10)
        )
        live_sms = check.classify(META, events, FULL_RANGE, {"sms": 2, "nms": 170})
        self.assertEqual(live_sms["verdict"], check.AMBIGUOUS)
        self.assertIn("sms 밴드가 이어져", live_sms["reason"])
        both_cut = check.classify(META, events, FULL_RANGE, {"sms": 95, "nms": 170})
        self.assertEqual(both_cut["verdict"], check.STOPPED)

    def test_events_outside_capture_are_ignored(self):
        outside = [(CS - dt.timedelta(minutes=5), "sms.server.cpu.usage"), (CE + dt.timedelta(minutes=5), "nms.x")]
        row = check.classify(META, outside + _every("dpm.mysql.x", 0, 170, 10), FULL_RANGE)
        self.assertEqual(row["families"]["sms"]["count"], 0)
        self.assertEqual(row["verdict"], check.STOPPED)


if __name__ == "__main__":
    unittest.main()
