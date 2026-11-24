"""Small independent serial oracle and schedule enumerators.

This module intentionally does not import the rich scenario state machine.  It
re-states only the safety-critical rules in a compact form so unittest can
enumerate many serial schedules and check that the richer simulator agrees on
authorization outcomes.
"""

from __future__ import annotations

import copy
import itertools
from typing import Any, Iterable

MAX_EVENTS = 12
GENESIS = "GENESIS"


class SerialReference:
    def __init__(self) -> None:
        self.epoch = 0
        self.tip = GENESIS
        self.version = 0
        self.tokens: dict[str, dict[str, Any]] = {}
        self.known_proposals: set[str] = set()
        self.local_epoch: dict[str, int] = {"n1": 0, "n2": 0, "n3": 0}
        self.durable: dict[str, dict[str, set[str]]] = {}
        self.frame_digest: dict[str, str] = {}
        self.committed: dict[str, dict[str, Any]] = {}
        self.auth_status: dict[str, str] = {}
        self.revoked_keys: set[str] = set()
        self.pending_revocation_keys: set[str] = set()
        self.receipts: dict[str, int] = {}
        self.receipt_redeliveries: dict[str, int] = {}
        self.held_tokens: set[str] = set()
        self.read_visible: dict[str, bool] = {}
        self.responses: list[dict[str, Any]] = []
        self.rejections: list[dict[str, Any]] = []
        self.violations: list[str] = []
        self.duplicate_writes = 0
        self.events_seen = 0

    def _reject(self, code: str) -> dict[str, Any]:
        self.rejections.append({"index": self.events_seen, "code": code})
        return {"accepted": False, "code": code}

    def apply(self, event: dict[str, Any]) -> dict[str, Any]:
        self.events_seen += 1
        op = event.get("op") or event.get("kind")
        at = int(event.get("at", 10))

        if op == "grant":
            token = event["token"]
            self.tokens[token["id"]] = token
            if int(token["epoch"]) > self.epoch:
                self.epoch = int(token["epoch"])
            return {"accepted": True}

        if op == "request_auth":
            self.known_proposals.add(event["id"])
            self.auth_status[event["id"]] = "pending"
            return {"accepted": True}

        if op == "request_revoke":
            self.known_proposals.add(event["id"])
            key = event.get("key", event["id"])
            self.pending_revocation_keys.add(key)
            self.auth_status.setdefault(event.get("target", ""), "pending")
            return {"accepted": True}

        if op == "write":
            pid = event["proposal"]
            if pid not in self.known_proposals:
                return self._reject("unknown_proposal")
            token = self.tokens.get(event.get("token"))
            if token is None:
                return self._reject("unknown_token")
            if not (int(token["not_before"]) <= at < int(token["expires_at"])):
                return self._reject("lease_inactive")
            node = event["node"]
            if int(token["epoch"]) < self.local_epoch.get(node, 0):
                return self._reject("fenced")
            digest = event["digest"]
            pid = event["proposal"]
            self.frame_digest.setdefault(pid, digest)
            if self.frame_digest[pid] != digest:
                return self._reject("digest_changed")
            nodes = self.durable.setdefault(pid, {})
            # Same token + same digest redelivery is an idempotent duplicate,
            # including when it is delivered to the same physical node.
            if digest in nodes.get(node, set()):
                self.duplicate_writes += 1
                return {"accepted": True, "duplicate": True}
            nodes.setdefault(node, set()).add(digest)
            self.local_epoch[node] = max(self.local_epoch[node], int(token["epoch"]))
            return {"accepted": True, "duplicate": False, "quorum": self._quorum(pid)}

        if op == "commit":
            pid = event["proposal"]
            if pid not in self.known_proposals:
                return self._reject("unknown_proposal")
            token = self.tokens.get(event.get("token"))
            if token is None or not (
                int(token["not_before"]) <= at < int(token["expires_at"])
            ):
                return self._reject("lease_inactive")
            if not self._quorum(pid):
                return self._reject("no_quorum")
            kind = event.get("proposal_kind", "auth")
            key = event.get("key", pid)
            if kind == "auth" and (key in self.pending_revocation_keys or key in self.revoked_keys):
                self.auth_status[pid] = "superseded"
                return self._reject("revocation_priority")
            if self.frame_digest.get(pid) != event.get("digest"):
                return self._reject("digest_mismatch")
            self.version += 1
            self.tip = event["digest"]
            self.committed[pid] = {"version": self.version, "key": key, "kind": kind}
            if kind == "auth":
                self.auth_status[pid] = "granted"
            else:
                self.revoked_keys.add(key)
                for auth_id, status in list(self.auth_status.items()):
                    if status == "pending" and event.get("target") in {auth_id, key}:
                        self.auth_status[auth_id] = "superseded"
                self.pending_revocation_keys.discard(key)
            return {"accepted": True, "version": self.version}

        if op == "response":
            if event.get("outcome") == "success" and event.get("proposal") not in self.committed:
                self.violations.append("fake_success")
                return self._reject("fake_success")
            self.responses.append(event)
            return {"accepted": True}

        if op == "receipt":
            rid = event["id"]
            if rid in self.receipts:
                # The consumed count stays at one; only a delivery counter moves.
                self.receipt_redeliveries[rid] = self.receipt_redeliveries.get(rid, 0) + 1
                return {"accepted": True, "duplicate": True, "effect_applied": False}
            self.receipts[rid] = 1
            return {"accepted": True, "duplicate": False, "effect_applied": True}

        if op == "hold_token":
            self.held_tokens.add(event["token"])
            return {"accepted": True}

        if op == "read":
            visible = event.get("snapshot_token") in self.held_tokens and event.get(
                "committed", False
            )
            self.read_visible[event.get("reader", "r1")] = visible
            if event.get("assert_visible") and not visible:
                self.violations.append("reader_without_token")
            return {"accepted": True, "visible": visible}

        return self._reject("unknown_event")

    def _quorum(self, proposal: str) -> bool:
        nodes = self.durable.get(proposal, {})
        physical = set()
        for node, digests in nodes.items():
            if digests:
                physical.add(node)  # Fixture node ids are already physical ids.
        return len(physical) >= 2

    def check_invariants(self) -> list[str]:
        problems = list(self.violations)
        for pid, meta in self.committed.items():
            if not self._quorum(pid):
                problems.append(f"committed without quorum: {pid}")
        for auth_id, status in self.auth_status.items():
            if status == "granted":
                pid = auth_id if auth_id in self.committed else None
                if pid is None:
                    problems.append(f"granted but not committed: {auth_id}")
        for reader, visible in self.read_visible.items():
            if visible and not self.held_tokens:
                problems.append(f"reader visible without token: {reader}")
        return problems

    def projection(self) -> dict[str, Any]:
        return {
            "epoch": self.epoch,
            "version": self.version,
            "auth_status": dict(sorted(self.auth_status.items())),
            "committed": sorted(self.committed),
            "receipts": dict(sorted(self.receipts.items())),
            "receipt_redeliveries": dict(sorted(self.receipt_redeliveries.items())),
            "read_visible": dict(sorted(self.read_visible.items())),
            "duplicate_writes": self.duplicate_writes,
            "rejections": copy.deepcopy(self.rejections),
            "violations": list(self.violations),
        }


def run_serial(events: Iterable[dict[str, Any]]) -> tuple[SerialReference, list[dict[str, Any]]]:
    ref = SerialReference()
    results = [ref.apply(copy.deepcopy(e)) for e in events]
    return ref, results


def enumerate_permutations(plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(plan) > MAX_EVENTS:
        raise ValueError(f"reference schedule is bounded at {MAX_EVENTS} events")
    reports = []
    seen = set()
    for permutation in itertools.permutations(plan):
        key = repr(permutation)
        if key in seen:
            continue
        seen.add(key)
        ref, _ = run_serial(list(permutation))
        problems = ref.check_invariants()
        reports.append(
            {
                "schedule": list(permutation),
                "projection": ref.projection(),
                "invariant_problems": problems,
                "safe": not problems,
            }
        )
    return reports


def interleave_streams(streams: list[list[dict[str, Any]]]) -> Iterable[list[dict[str, Any]]]:
    """Yield every interleaving while preserving order inside each stream."""

    if not streams:
        yield []
        return
    total = sum(len(s) for s in streams)
    if total > MAX_EVENTS:
        raise ValueError(f"reference schedule is bounded at {MAX_EVENTS} events")

    def rec(indices: list[int], acc: list[dict[str, Any]]):
        if len(acc) == total:
            yield list(acc)
            return
        for stream_index, stream in enumerate(streams):
            i = indices[stream_index]
            if i < len(stream):
                indices[stream_index] += 1
                acc.append(stream[i])
                yield from rec(indices, acc)
                acc.pop()
                indices[stream_index] -= 1

    yield from rec([0] * len(streams), [])


def enumerate_stream_schedules(streams: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    reports = []
    for schedule in interleave_streams(streams):
        ref, _ = run_serial(schedule)
        problems = ref.check_invariants()
        reports.append(
            {
                "schedule": schedule,
                "projection": ref.projection(),
                "invariant_problems": problems,
                "safe": not problems,
            }
        )
    return reports


def boundary_permutation_plan() -> list[dict[str, Any]]:
    """Seven-event plan with an exact same-token/same-digest redelivery."""

    token_a = {
        "id": "t-a",
        "epoch": 1,
        "coordinator": "obs-a",
        "not_before": 0,
        "expires_at": 10,
    }
    token_b = {
        "id": "t-b",
        "epoch": 2,
        "coordinator": "obs-b",
        "not_before": 10,
        "expires_at": 20,
    }
    digest = "digest-auth-boundary"
    return [
        {"op": "grant", "token": token_a},
        {"op": "grant", "token": token_b},
        {"op": "request_auth", "id": "auth-1"},
        {"op": "write", "proposal": "auth-1", "node": "n1", "token": "t-b", "digest": digest, "at": 10},
        # Exact boundary: same owner token and same frame digest twice.
        {"op": "write", "proposal": "auth-1", "node": "n1", "token": "t-b", "digest": digest, "at": 10},
        {"op": "write", "proposal": "auth-1", "node": "n2", "token": "t-b", "digest": digest, "at": 10},
        {"op": "commit", "proposal": "auth-1", "token": "t-b", "digest": digest, "at": 10},
    ]


def twelve_event_streams() -> list[list[dict[str, Any]]]:
    """Three ordered streams totaling exactly 12 events."""

    token_a = {"id": "t-a", "epoch": 1, "coordinator": "obs-a", "not_before": 0, "expires_at": 10}
    token_b = {"id": "t-b", "epoch": 2, "coordinator": "obs-b", "not_before": 10, "expires_at": 20}
    auth_digest = "digest-auth-12"
    rev_digest = "digest-rev-12"
    return [
        [
            {"op": "grant", "token": token_a},
            {"op": "grant", "token": token_b},
        ],
        [
            {"op": "request_auth", "id": "auth-1", "key": "policy-x"},
            {"op": "request_revoke", "id": "rev-1", "key": "policy-x", "target": "auth-1"},
            {"op": "receipt", "id": "receipt-1"},
            {"op": "receipt", "id": "receipt-1"},
        ],
        [
            {"op": "write", "proposal": "auth-1", "node": "n1", "token": "t-b", "digest": auth_digest, "at": 10},
            {"op": "write", "proposal": "auth-1", "node": "n1", "token": "t-b", "digest": auth_digest, "at": 10},
            {"op": "write", "proposal": "auth-1", "node": "n2", "token": "t-b", "digest": auth_digest, "at": 10},
            {"op": "write", "proposal": "rev-1", "node": "n2", "token": "t-b", "digest": rev_digest, "at": 10, "proposal_kind": "revoke", "key": "policy-x"},
            {"op": "write", "proposal": "rev-1", "node": "n3", "token": "t-b", "digest": rev_digest, "at": 10, "proposal_kind": "revoke", "key": "policy-x"},
            {"op": "commit", "proposal": "rev-1", "token": "t-b", "digest": rev_digest, "at": 10, "proposal_kind": "revoke", "key": "policy-x", "target": "auth-1"},
        ],
    ]
