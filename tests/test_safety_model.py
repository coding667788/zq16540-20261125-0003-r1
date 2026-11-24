import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from crashfence.fixtures import all_fixtures
from crashfence.model import lease_active, simulate_scenario, token_digest


TOKEN_A = {
    "epoch": 1,
    "coordinator": "obs-a",
    "lease_id": "lease-a",
    "not_before": 0,
    "expires_at": 10,
}
TOKEN_B = {
    "epoch": 2,
    "coordinator": "obs-b",
    "lease_id": "lease-b",
    "not_before": 10,
    "expires_at": 20,
}


class SafetyModelTests(unittest.TestCase):
    def test_abutting_leases_share_boundary_without_overlap(self):
        a = {**TOKEN_A, "digest": token_digest(TOKEN_A)}
        b = {**TOKEN_B, "digest": token_digest(TOKEN_B)}
        self.assertTrue(lease_active(a, 9))
        self.assertFalse(lease_active(a, 10))
        self.assertTrue(lease_active(b, 10))
        self.assertFalse(lease_active(b, 20))

    def test_revocation_precedes_earlier_authorization_and_duplicate_is_idempotent(self):
        scenario = {
            "readers": [{"id": "r1", "upstream": "up1", "possession_tokens": ["proof"]}],
            "steps": [
                {"op": "grant_owner_token", "token_id": "t1", "token": {
                    "epoch": 1, "coordinator": "obs-a", "lease_id": "l",
                    "not_before": 0, "expires_at": 100,
                }},
                {"op": "request_authorization", "proposal_id": "a1", "auth_id": "auth1",
                 "conflict_key": "k"},
                {"op": "request_revocation", "proposal_id": "r1", "conflict_key": "k",
                 "target_auth_id": "auth1"},
                {"op": "write_frame", "proposal": "a1", "node": "n1", "token": "t1"},
                {"op": "write_frame", "proposal": "a1", "node": "n1", "token": "t1"},
                {"op": "write_frame", "proposal": "a1", "node": "n2", "token": "t1"},
                {"op": "write_frame", "proposal": "r1", "node": "n2", "token": "t1"},
                {"op": "write_frame", "proposal": "r1", "node": "n3", "token": "t1"},
                {"op": "publish_commit_marker", "proposal": "r1", "token": "t1"},
                {"op": "publish_commit_marker", "proposal": "a1", "token": "t1"},
            ],
        }
        report = simulate_scenario(scenario)
        self.assertEqual(report["status"], "safe", report["violations"])
        proposals = report["final_state"]["proposals"]
        self.assertEqual(proposals["a1"]["status"], "superseded")
        self.assertEqual(proposals["a1"]["duplicate_writes"], 1)
        self.assertGreaterEqual(len(proposals["r1"]["durable_nodes"]), 2)
        auth = report["authorization_projection"]["authorizations"]["auth1"]
        self.assertEqual(auth["decision"], "superseded")

    def test_reader_needs_matching_upstream_and_possession_token(self):
        scenario = {
            "readers": [
                {"id": "r1", "upstream": "up1"},
                {"id": "r2", "upstream": "up2", "possession_tokens": ["proof"]},
            ],
            "steps": [
                {"op": "grant_owner_token", "token_id": "t", "token": {
                    "epoch": 1, "coordinator": "obs-a", "lease_id": "l",
                    "not_before": 0, "expires_at": 100,
                }},
                {"op": "request_authorization", "proposal_id": "a1", "auth_id": "auth"},
                {"op": "write_frame", "proposal": "a1", "node": "n1", "token": "t"},
                {"op": "write_frame", "proposal": "a1", "node": "n2", "token": "t"},
                {"op": "publish_commit_marker", "proposal": "a1", "token": "t"},
                {"op": "publish_read_snapshot", "snapshot_id": "s", "upstream": "up1",
                 "confirmation_token": "proof"},
                {"op": "reader_read", "reader": "r1", "snapshot_id": "s"},
                {"op": "reader_read", "reader": "r2", "snapshot_id": "s"},
                {"op": "grant_read_token", "reader": "r1", "token": "proof"},
                {"op": "reader_read", "reader": "r1", "snapshot_id": "s"},
            ],
        }
        report = simulate_scenario(scenario)
        views = report["final_state"]["readers"]
        self.assertFalse(views["r1"]["observations"][0]["visible"])
        self.assertEqual(views["r1"]["observations"][0]["reason"], "missing_confirmation_token")
        self.assertFalse(views["r2"]["observations"][0]["visible"])
        self.assertEqual(views["r2"]["observations"][0]["reason"], "wrong_upstream")
        self.assertTrue(views["r1"]["last_view"]["visible"])

    def test_duplicate_receipt_consumes_once_and_uncertain_is_not_success(self):
        scenario = {
            "steps": [
                {"op": "redeem_receipt", "receipt_id": "rr1"},
                {"op": "redeem_receipt", "receipt_id": "rr1"},
                {"op": "record_response", "correlation_id": "c1", "outcome": "uncertain"},
                {"op": "record_response", "correlation_id": "bad", "outcome": "success"},
            ]
        }
        report = simulate_scenario(scenario)
        # The fake success is an impossible event, represented as a fatal violation.
        self.assertEqual(report["status"], "fatal_violation")
        receipt_step_2 = report["steps"][1]
        self.assertTrue(receipt_step_2["duplicate"])
        self.assertFalse(receipt_step_2["effect_applied"])
        self.assertTrue(report["steps"][2]["accepted"])
        self.assertEqual(report["steps"][2]["outcome"], "uncertain")
        self.assertEqual(report["steps"][3]["outcome"], "rejected")

    def test_every_scenario_fixture_reports_safe(self):
        for name, doc in all_fixtures().items():
            if not name.startswith("scenario_"):
                continue
            with self.subTest(name=name):
                report = simulate_scenario(doc)
                self.assertEqual(report["status"], "safe", report["violations"])

    def test_restart_recognizes_only_last_complete_owner_token(self):
        scenario = {
            "initial_clock": 10,
            "steps": [
                {"op": "grant_owner_token", "token_id": "ta", "token": TOKEN_A},
                {"op": "grant_owner_token", "token_id": "tb", "token": TOKEN_B},
                {"op": "persist_owner_token", "node": "n2", "token": "ta"},
                {"op": "persist_owner_token", "node": "n2", "token": "tb", "partial_bytes": 7},
                {"op": "crash", "node": "n2"},
                {"op": "restart", "node": "n2"},
            ],
        }
        report = simulate_scenario(scenario)
        node = report["final_state"]["nodes"]["n2"]
        self.assertEqual(node["recovery"]["classification"], "last_frame_incomplete")
        self.assertEqual(node["last_complete_owner_epoch"], 1)


if __name__ == "__main__":
    unittest.main()
