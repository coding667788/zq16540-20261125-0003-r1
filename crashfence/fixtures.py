"""Synthetic offline JSON fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def revocation_priority_scenario() -> dict[str, Any]:
    return {
        "schema": "crashfence.scenario.v1",
        "name": "revocation wins over earlier uncommitted authorization",
        "readers": [{"id": "r1", "upstream": "up-r1"}],
        "steps": [
            {
                "op": "grant_owner_token",
                "token_id": "t1",
                "token": {
                    "epoch": 1,
                    "coordinator": "obs-a",
                    "lease_id": "lease-1",
                    "not_before": 0,
                    "expires_at": 100,
                },
            },
            {
                "op": "request_authorization",
                "proposal_id": "auth-1",
                "auth_id": "auth-1",
                "conflict_key": "policy-x",
                "principal": "service-example",
                "action": "invoke",
                "resource": "policy-x",
            },
            {
                "op": "request_revocation",
                "proposal_id": "rev-1",
                "target_auth_id": "auth-1",
                "conflict_key": "policy-x",
            },
            {"op": "write_frame", "proposal": "auth-1", "node": "n1", "token": "t1"},
            {
                "op": "write_frame",
                "proposal": "auth-1",
                "node": "n1",
                "token": "t1",
                "comment": "same token, same proposal/frame digest: duplicate",
            },
            {"op": "write_frame", "proposal": "auth-1", "node": "n2", "token": "t1"},
            {"op": "write_frame", "proposal": "rev-1", "node": "n2", "token": "t1"},
            {"op": "write_frame", "proposal": "rev-1", "node": "n3", "token": "t1"},
            {"op": "publish_commit_marker", "proposal": "rev-1", "token": "t1"},
            {"op": "publish_commit_marker", "proposal": "auth-1", "token": "t1"},
            {
                "op": "publish_read_snapshot",
                "snapshot_id": "snap-1",
                "version": 1,
                "upstream": "up-r1",
                "confirmation_token": "read-proof-1",
            },
            {"op": "reader_read", "reader": "r1", "snapshot_id": "snap-1"},
            {"op": "grant_read_token", "reader": "r1", "token": "read-proof-1"},
            {"op": "reader_read", "reader": "r1", "snapshot_id": "snap-1"},
            {
                "op": "assert",
                "authorization_id": "auth-1",
                "expected_decision": "superseded",
            },
        ],
    }


def coordinator_transition_scenario() -> dict[str, Any]:
    return {
        "schema": "crashfence.scenario.v1",
        "name": "abutting coordinator leases and last complete owner token",
        "initial_clock": 10,
        "steps": [
            {
                "op": "grant_owner_token",
                "token_id": "ta",
                "token": {
                    "epoch": 1,
                    "coordinator": "obs-a",
                    "lease_id": "lease-a",
                    "not_before": 0,
                    "expires_at": 10,
                },
            },
            {
                "op": "grant_owner_token",
                "token_id": "tb",
                "token": {
                    "epoch": 2,
                    "coordinator": "obs-b",
                    "lease_id": "lease-b",
                    "not_before": 10,
                    "expires_at": 20,
                },
            },
            {"op": "persist_owner_token", "node": "n1", "token": "ta", "complete": True},
            {
                "op": "persist_owner_token",
                "node": "n1",
                "token": "tb",
                "partial_bytes": 12,
            },
            {"op": "crash", "node": "n1"},
            {"op": "restart", "node": "n1"},
            {
                "op": "assert",
                "node": "n1",
                "recovery_classification": "last_frame_incomplete",
            },
            {
                "op": "request_authorization",
                "proposal_id": "auth-transition",
                "auth_id": "auth-transition",
            },
            {
                "op": "write_frame",
                "proposal": "auth-transition",
                "node": "n1",
                "token": "ta",
                "at": 10,
                "comment": "old lease expires exactly at boundary 10",
            },
            {
                "op": "write_frame",
                "proposal": "auth-transition",
                "node": "n1",
                "token": "tb",
                "at": 10,
            },
            {"op": "persist_owner_token", "node": "n1", "token": "tb", "complete": True},
            {"op": "restart", "node": "n1"},
        ],
    }


def reader_and_receipt_scenario() -> dict[str, Any]:
    return {
        "schema": "crashfence.scenario.v1",
        "name": "upstream-bound possession tokens and idempotent receipts",
        "readers": [
            {"id": "r-a", "upstream": "up-a"},
            {"id": "r-b", "upstream": "up-b", "possession_tokens": ["read-good"]},
        ],
        "steps": [
            {
                "op": "grant_owner_token",
                "token_id": "t1",
                "token": {
                    "epoch": 1,
                    "coordinator": "obs-a",
                    "lease_id": "l1",
                    "not_before": 0,
                    "expires_at": 100,
                },
            },
            {"op": "request_authorization", "proposal_id": "a1", "auth_id": "auth-read"},
            {"op": "write_frame", "proposal": "a1", "node": "n1", "token": "t1"},
            {"op": "write_frame", "proposal": "a1", "node": "n2", "token": "t1"},
            {"op": "publish_commit_marker", "proposal": "a1", "token": "t1"},
            {
                "op": "publish_read_snapshot",
                "snapshot_id": "snap-a",
                "upstream": "up-a",
                "confirmation_token": "read-good",
            },
            {"op": "reader_read", "reader": "r-a", "snapshot_id": "snap-a"},
            {"op": "reader_read", "reader": "r-b", "snapshot_id": "snap-a"},
            {"op": "grant_read_token", "reader": "r-a", "token": "read-good"},
            {"op": "reader_read", "reader": "r-a", "snapshot_id": "snap-a"},
            {"op": "redeem_receipt", "receipt_id": "receipt-7", "authorization_id": "auth-read"},
            {"op": "redeem_receipt", "receipt_id": "receipt-7", "authorization_id": "auth-read"},
            {
                "op": "record_response",
                "correlation_id": "resp-uncertain",
                "proposal": "a1",
                "outcome": "uncertain",
            },
        ],
    }


def crash_recovery_scenario() -> dict[str, Any]:
    return {
        "schema": "crashfence.scenario.v1",
        "name": "partial frame, restart, retry, quorum commit",
        "steps": [
            {
                "op": "grant_owner_token",
                "token_id": "t1",
                "token": {
                    "epoch": 1,
                    "coordinator": "obs-b",
                    "lease_id": "l-crash",
                    "not_before": 0,
                    "expires_at": 100,
                },
            },
            {"op": "request_authorization", "proposal_id": "a1", "auth_id": "auth-crash"},
            {"op": "write_frame", "proposal": "a1", "node": "n1", "token": "t1", "partial_bytes": 20},
            {"op": "crash", "node": "n1"},
            {"op": "restart", "node": "n1"},
            {"op": "assert", "node": "n1", "recovery_classification": "last_frame_incomplete"},
            {"op": "write_frame", "proposal": "a1", "node": "n1", "token": "t1"},
            {"op": "write_frame", "proposal": "a1", "node": "n2", "token": "t1"},
            {"op": "publish_commit_marker", "proposal": "a1", "token": "t1"},
            {"op": "restart", "node": "n1"},
            {"op": "assert", "node": "n1", "recovery_classification": "clean"},
            {"op": "assert", "committed_version": 1},
        ],
    }


def message_passing_scenario() -> dict[str, Any]:
    return {
        "schema": "crashfence.scenario.v1",
        "name": "explicit deterministic message send and duplicate delivery",
        "steps": [
            {
                "op": "grant_owner_token",
                "token_id": "t1",
                "token": {
                    "epoch": 1,
                    "coordinator": "obs-a",
                    "lease_id": "l-msg",
                    "not_before": 0,
                    "expires_at": 100,
                },
            },
            {"op": "request_authorization", "proposal_id": "a1", "auth_id": "auth-msg"},
            {
                "op": "send_message",
                "id": "m1",
                "from": "obs-a",
                "to": "n1",
                "message": {
                    "type": "append",
                    "step": {"op": "write_frame", "proposal": "a1", "node": "n1", "token": "t1"},
                },
            },
            {"op": "deliver_message", "id": "m1"},
            {"op": "deliver_message", "id": "m1"},
            {
                "op": "send_message",
                "id": "m2",
                "from": "obs-a",
                "to": "n2",
                "message": {
                    "type": "append",
                    "step": {"op": "write_frame", "proposal": "a1", "node": "n2", "token": "t1"},
                },
            },
            {"op": "deliver_message", "id": "m2"},
            {"op": "publish_commit_marker", "proposal": "a1", "token": "t1"},
            {"op": "assert", "committed_version": 1},
        ],
    }


def ambiguous_trace() -> dict[str, Any]:
    return {
        "schema": "crashfence.trace.v1",
        "name": "missing quorum event creates authorization ambiguity",
        "max_events": 12,
        "readers": [],
        "events": [
            {
                "op": "grant_owner_token",
                "token_id": "t1",
                "token": {
                    "epoch": 1,
                    "coordinator": "obs-a",
                    "lease_id": "l1",
                    "not_before": 0,
                    "expires_at": 100,
                },
            },
            {"op": "request_authorization", "proposal_id": "a1", "auth_id": "auth-amb"},
            {"op": "write_frame", "proposal": "a1", "node": "n1", "token": "t1"},
            None,
            {"op": "publish_commit_marker", "proposal": "a1", "token": "t1"},
        ],
        "choices": {
            "3": [
                {"op": "write_frame", "proposal": "a1", "node": "n2", "token": "t1"},
                {"op": "advance_clock", "to": 5},
            ]
        },
    }


def no_history_trace() -> dict[str, Any]:
    return {
        "schema": "crashfence.trace.v1",
        "name": "success is asserted without a commit marker",
        "events": [
            {
                "op": "grant_owner_token",
                "token_id": "t1",
                "token": {
                    "epoch": 1,
                    "coordinator": "obs-a",
                    "lease_id": "l1",
                    "not_before": 0,
                    "expires_at": 100,
                },
            },
            {"op": "request_authorization", "proposal_id": "a1", "auth_id": "auth-no"},
            {"op": "write_frame", "proposal": "a1", "node": "n1", "token": "t1"},
            {"op": "assert", "authorization_id": "auth-no", "expected_decision": "granted"},
        ],
    }


def fake_success_trace() -> dict[str, Any]:
    return {
        "schema": "crashfence.trace.v1",
        "name": "uncertain response cannot be recorded as success",
        "events": [
            {
                "op": "record_response",
                "correlation_id": "c1",
                "proposal": "missing-proposal",
                "outcome": "success",
            }
        ],
    }


def torn_frame_trace() -> dict[str, Any]:
    return {
        "schema": "crashfence.trace.v1",
        "name": "last frame prefix is incomplete, not committed corruption",
        "events": [
            {
                "op": "grant_owner_token",
                "token_id": "t1",
                "token": {
                    "epoch": 1,
                    "coordinator": "obs-a",
                    "lease_id": "l1",
                    "not_before": 0,
                    "expires_at": 100,
                },
            },
            {"op": "request_authorization", "proposal_id": "a1", "auth_id": "auth-torn"},
            {
                "op": "write_frame",
                "proposal": "a1",
                "node": "n1",
                "token": "t1",
                "partial_bytes": 18,
            },
            {"op": "crash", "node": "n1"},
            {"op": "restart", "node": "n1"},
            {
                "op": "assert",
                "node": "n1",
                "recovery_classification": "last_frame_incomplete",
            },
        ],
    }


def committed_corruption_trace() -> dict[str, Any]:
    return {
        "schema": "crashfence.trace.v1",
        "name": "published marker plus bad frame is committed log corruption",
        "events": [
            {
                "op": "grant_owner_token",
                "token_id": "t1",
                "token": {
                    "epoch": 1,
                    "coordinator": "obs-a",
                    "lease_id": "l1",
                    "not_before": 0,
                    "expires_at": 100,
                },
            },
            {"op": "request_authorization", "proposal_id": "a1", "auth_id": "auth-bad"},
            {"op": "write_frame", "proposal": "a1", "node": "n1", "token": "t1"},
            {"op": "write_frame", "proposal": "a1", "node": "n2", "token": "t1"},
            {"op": "publish_commit_marker", "proposal": "a1", "token": "t1"},
            {"op": "inject_corruption", "node": "n1", "slot": "s1", "record_index": -1},
            {"op": "restart", "node": "n1"},
            {
                "op": "assert",
                "node": "n1",
                "recovery_classification": "committed_log_corruption",
            },
        ],
    }


def wrong_corruption_expectation_trace() -> dict[str, Any]:
    doc = committed_corruption_trace()
    doc["name"] = "trace wrongly calls committed corruption a torn tail"
    doc["events"][-1] = {
        "op": "assert",
        "node": "n1",
        "recovery_classification": "last_frame_incomplete",
    }
    return doc


def all_fixtures() -> dict[str, dict[str, Any]]:
    return {
        "scenario_revocation_priority.json": revocation_priority_scenario(),
        "scenario_coordinator_transition.json": coordinator_transition_scenario(),
        "scenario_reader_receipt.json": reader_and_receipt_scenario(),
        "scenario_crash_recovery.json": crash_recovery_scenario(),
        "scenario_message_passing.json": message_passing_scenario(),
        "trace_ambiguous.json": ambiguous_trace(),
        "trace_no_history.json": no_history_trace(),
        "trace_fake_success.json": fake_success_trace(),
        "trace_torn_frame.json": torn_frame_trace(),
        "trace_committed_corruption.json": committed_corruption_trace(),
        "trace_wrong_corruption_expectation.json": wrong_corruption_expectation_trace(),
    }


def write_fixtures(directory: str | Path) -> list[str]:
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for name, doc in all_fixtures().items():
        path = out / name
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        written.append(str(path))
    return written
