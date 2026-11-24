import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from crashfence.fixtures import all_fixtures
from crashfence.tracecheck import check_trace


class TraceCheckTests(unittest.TestCase):
    def test_all_synthetic_trace_fixtures_have_expected_statuses(self):
        fixes = all_fixtures()
        expected = {
            "trace_ambiguous.json": "ambiguous_authorization",
            "trace_no_history.json": "no_legal_history",
            "trace_fake_success.json": "no_legal_history",
            "trace_torn_frame.json": "unique_history",
            "trace_committed_corruption.json": "unique_history",
            "trace_wrong_corruption_expectation.json": "no_legal_history",
        }
        for name, status in expected.items():
            with self.subTest(name=name):
                report = check_trace(fixes[name])
                self.assertEqual(report["status"], status)

    def test_no_history_returns_shortest_contradictory_prefix(self):
        report = check_trace(all_fixtures()["trace_no_history.json"])
        prefix = report["shortest_contradictory_prefix"]
        self.assertEqual(prefix["prefix_length"], 4)
        self.assertEqual(prefix["reason"]["assertion_errors"][0]["actual"], "pending")

    def test_ambiguous_trace_returns_minimal_authorization_evidence(self):
        report = check_trace(all_fixtures()["trace_ambiguous.json"])
        self.assertEqual(report["legal_history_count"], 2)
        evidence = report["minimal_authorization_evidence"]
        self.assertEqual(evidence["prefix_length"], 5)
        self.assertEqual(evidence["distinguishing_slot"], 3)
        decisions = [
            p["authorizations"]["auth-amb"]["decision"]
            for p in evidence["authorization_projections"]
        ]
        self.assertIn("granted", decisions)
        self.assertTrue(any(d != "granted" for d in decisions))

    def test_torn_tail_and_committed_corruption_are_distinguished(self):
        torn = check_trace(all_fixtures()["trace_torn_frame.json"])
        corrupt = check_trace(all_fixtures()["trace_committed_corruption.json"])
        self.assertIn("last_frame_incomplete", torn["fault_classification"]["counts"])
        self.assertIn("committed_log_corruption", corrupt["fault_classification"]["counts"])

        mismatch = check_trace(all_fixtures()["trace_wrong_corruption_expectation.json"])
        error = mismatch["shortest_contradictory_prefix"]["reason"]["assertion_errors"][0]
        self.assertEqual(error["expected"], "last_frame_incomplete")
        self.assertEqual(error["actual"], "committed_log_corruption")

    def test_sparse_entries_are_normalized_as_missing_sequence_positions(self):
        doc = {
            "entries": [
                {"seq": 0, "event": {"op": "advance_clock", "to": 1}},
                {"seq": 2, "event": {"op": "assert", "committed_version": 0}},
            ],
            "length": 3,
            "choices": {"1": [{"op": "advance_clock", "to": 2}]},
        }
        report = check_trace(doc)
        self.assertEqual(report["status"], "unique_history")
        self.assertEqual(report["event_count"], 3)
        self.assertEqual(report["legal_history_count"], 1)


if __name__ == "__main__":
    unittest.main()
