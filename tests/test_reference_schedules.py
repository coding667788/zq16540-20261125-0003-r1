import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from crashfence.model import simulate_scenario
from crashfence.reference import (
    boundary_permutation_plan,
    twelve_event_streams,
    enumerate_permutations,
    enumerate_stream_schedules,
    run_serial,
    SerialReference,
)


def reference_schedule_to_model_steps(schedule):
    steps = []
    for event in schedule:
        op = event["op"]
        if op == "grant":
            token = dict(event["token"])
            token_id = token.pop("id")
            steps.append({"op": "grant_owner_token", "token_id": token_id, "token": token})
        elif op == "request_auth":
            steps.append({
                "op": "request_authorization",
                "proposal_id": event["id"],
                "auth_id": event["id"],
            })
        elif op == "write":
            steps.append({
                "op": "write_frame",
                "proposal": event["proposal"],
                "node": event["node"],
                "token": event["token"],
                "at": event.get("at", 10),
            })
        elif op == "commit":
            steps.append({
                "op": "publish_commit_marker",
                "proposal": event["proposal"],
                "token": event["token"],
                "at": event.get("at", 10),
            })
        else:
            raise AssertionError(f"unmapped reference op {op}")
    return steps


class SerialReferenceScheduleTests(unittest.TestCase):
    def test_all_seven_event_permutations_are_safe_and_match_model_grants(self):
        reports = enumerate_permutations(boundary_permutation_plan())
        # Two write events are intentionally identical, so unique schedules are
        # 7! / 2 = 2520 rather than 5040 noisy duplicates.
        self.assertEqual(len(reports), 2520)
        for report in reports:
            self.assertEqual(report["invariant_problems"], [], report["schedule"])
            model = simulate_scenario({"steps": reference_schedule_to_model_steps(report["schedule"])})
            self.assertEqual(model["violations"], [])
            model_granted = (
                model["authorization_projection"]["authorizations"]
                .get("auth-1", {})
                .get("decision")
                == "granted"
            )
            ref_granted = report["projection"]["auth_status"].get("auth-1") == "granted"
            self.assertEqual(
                model_granted,
                ref_granted,
                msg={
                    "schedule": report["schedule"],
                    "ref": report["projection"],
                    "model_status": model["status"],
                },
            )

    def test_twelve_event_stream_interleavings(self):
        streams = twelve_event_streams()
        self.assertEqual(sum(len(s) for s in streams), 12)
        reports = enumerate_stream_schedules(streams)
        # 12! / (2! * 4! * 6!) preserving order inside each stream.
        self.assertEqual(len(reports), 13860)
        unsafe = [r for r in reports if not r["safe"]]
        self.assertEqual(unsafe, [])

    def test_canonical_twelve_event_schedule_covers_transition_duplicate_and_revocation(self):
        streams = twelve_event_streams()
        schedule = streams[0] + streams[1] + streams[2]
        ref, _ = run_serial(schedule)
        self.assertEqual(ref.auth_status["auth-1"], "superseded")
        self.assertIn("rev-1", ref.committed)
        self.assertEqual(ref.receipts, {"receipt-1": 1})
        self.assertEqual(ref.duplicate_writes, 1)
        self.assertEqual(ref.check_invariants(), [])

        model = simulate_scenario({"steps": reference_schedule_to_model_steps(
            # The stream-only mapping does not include receipt events; map the
            # protocol prefix through the same helper and assert no violation.
            [e for e in schedule if e["op"] in {"grant", "request_auth", "write", "commit"}]
        )})
        self.assertEqual(model["status"], "safe")

    def test_abutting_lease_boundary_in_independent_reference(self):
        ref = SerialReference()
        ref.apply({"op": "grant", "token": {
            "id": "a", "epoch": 1, "not_before": 0, "expires_at": 10,
        }})
        ref.apply({"op": "grant", "token": {
            "id": "b", "epoch": 2, "not_before": 10, "expires_at": 20,
        }})
        ref.known_proposals.add("p")
        at_nine = ref.apply({"op": "write", "proposal": "p", "node": "n1",
                             "token": "a", "digest": "d", "at": 9})
        old_at_boundary = ref.apply({"op": "write", "proposal": "p", "node": "n2",
                                     "token": "a", "digest": "d", "at": 10})
        new_at_boundary = ref.apply({"op": "write", "proposal": "p", "node": "n2",
                                     "token": "b", "digest": "d", "at": 10})
        self.assertTrue(at_nine["accepted"])
        self.assertFalse(old_at_boundary["accepted"])
        self.assertEqual(old_at_boundary["code"], "lease_inactive")
        self.assertTrue(new_at_boundary["accepted"])


if __name__ == "__main__":
    unittest.main()
