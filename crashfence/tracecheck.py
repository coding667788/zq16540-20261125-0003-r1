"""Offline trace completion and legal-history projection.

A TRACE document is UTF-8 JSON.  Its ``events`` array may contain normal
Crashfence steps, observation assertions, and missing entries encoded as
``null``, ``"?"``, or ``{"missing": true}``.  Missing entries are filled from
per-slot ``choices`` or a document-wide ``candidates`` alphabet.

The search is intentionally finite and explicit.  Fixtures and unittests keep
the event count at or below 12 so all schedules can be enumerated on an
ordinary CPU.
"""

from __future__ import annotations

import copy
import itertools
import json
import sys
from pathlib import Path
from typing import Any

from .canonical import canonical_json, sha256_hex
from .model import simulate_scenario

MAX_REFERENCE_EVENTS = 12

IMPOSSIBLE_STEP_CODES = {
    "UNKNOWN_OP",
    "FAKE_SUCCESS_WITHOUT_COMMIT",
}


def load_trace(path: str | Path) -> dict[str, Any]:
    if str(path) == "-":
        data = json.loads(sys.stdin.read())
    else:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        data = {"events": data}
    if not isinstance(data, dict):
        raise ValueError("TRACE must be a JSON object or array")
    return data


def _is_missing(entry: Any) -> bool:
    return entry is None or entry == "?" or (isinstance(entry, dict) and entry.get("missing") is True)


def normalize_trace(doc: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any] | None], list[list[dict[str, Any]] | None]]:
    initial = copy.deepcopy(doc.get("initial") or doc.get("scenario") or {})
    initial.setdefault("nodes", doc.get("nodes", ["n1", "n2", "n3"]))
    initial.setdefault("readers", doc.get("readers", []))

    slots: list[dict[str, Any] | None] = []
    if "events" in doc or "steps" in doc:
        raw_events = doc.get("events", doc.get("steps", []))
        if not isinstance(raw_events, list):
            raise ValueError("events must be an array")
        slots = [None if _is_missing(e) else copy.deepcopy(e) for e in raw_events]
    elif "entries" in doc:
        entries = doc["entries"]
        length = int(doc.get("length") or doc.get("last_seq", len(entries)))
        by_seq: dict[int, Any] = {}
        for entry in entries:
            seq = int(entry["seq"])
            by_seq[seq] = entry.get("event", entry.get("step", entry))
        max_seq = max(by_seq) if by_seq else -1
        length = max(length, max_seq + 1)
        slots = [None] * length
        for seq, value in by_seq.items():
            slots[seq] = None if _is_missing(value) else copy.deepcopy(value)
    else:
        raise ValueError("TRACE requires events/steps or sparse entries")

    if len(slots) > int(doc.get("max_events", MAX_REFERENCE_EVENTS)):
        raise ValueError(
            f"trace has {len(slots)} events; bounded reference maximum is "
            f"{doc.get('max_events', MAX_REFERENCE_EVENTS)}"
        )

    global_candidates = doc.get("candidates")
    if global_candidates is not None:
        global_candidates = [copy.deepcopy(x) for x in global_candidates]
    raw_choices = doc.get("choices", {})
    choices: list[list[dict[str, Any]] | None] = []
    for index, slot in enumerate(slots):
        if slot is not None:
            choices.append(None)
            continue
        local = None
        if isinstance(raw_choices, dict):
            local = raw_choices.get(str(index), raw_choices.get(index))
        elif isinstance(raw_choices, list) and index < len(raw_choices):
            local = raw_choices[index]
        if local is None:
            local = doc.get(f"choices_{index}") or global_candidates or default_candidates(initial)
        if not isinstance(local, list) or not local:
            raise ValueError(f"missing slot {index} has no candidate choices")
        choices.append([copy.deepcopy(x) for x in local])
    return initial, slots, choices


def default_candidates(initial: dict[str, Any]) -> list[dict[str, Any]]:
    """Small documented alphabet used when a trace omits explicit choices."""

    reader_id = "r1"
    if initial.get("readers"):
        first = initial["readers"][0]
        reader_id = first if isinstance(first, str) else first.get("id", "r1")
    return [
        {"op": "advance_clock", "to": 10},
        {
            "op": "grant_owner_token",
            "token_id": "t1",
            "token": {
                "epoch": 1,
                "coordinator": "obs-a",
                "lease_id": "l1",
                "not_before": 0,
                "expires_at": 20,
            },
        },
        {"op": "request_authorization", "proposal_id": "a1", "auth_id": "auth-1"},
        {"op": "request_revocation", "proposal_id": "rev1", "target_auth_id": "auth-1"},
        {"op": "write_frame", "proposal": "a1", "node": "n1", "token": "t1"},
        {"op": "write_frame", "proposal": "a1", "node": "n2", "token": "t1"},
        {"op": "publish_commit_marker", "proposal": "a1", "token": "t1"},
        {"op": "reader_read", "reader": reader_id},
        {"op": "redeem_receipt", "receipt_id": "receipt-1"},
    ]


def completion_iter(slots, choices):
    option_lists = []
    for slot, local_choices in zip(slots, choices):
        option_lists.append([slot] if slot is not None else local_choices)
    for values in itertools.product(*option_lists):
        yield list(values)


def evaluate_completion(initial: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    scenario = copy.deepcopy(initial)
    scenario["steps"] = events
    sim = simulate_scenario(scenario)

    impossible_errors = []
    assertion_errors = []
    for step in sim["steps"]:
        error = step.get("error") or {}
        code = error.get("code")
        if step.get("outcome") == "assertion_failed":
            assertion_errors.extend(step.get("assertion_errors", []))
        if code in IMPOSSIBLE_STEP_CODES:
            impossible_errors.append({"step_index": step.get("index"), **error})
        if code == "UNKNOWN_OP":
            impossible_errors.append({"step_index": step.get("index"), **error})

    consistent = not impossible_errors and not assertion_errors
    node_recovery = {
        node_id: node.get("recovery", {}).get("classification")
        for node_id, node in sim["final_state"]["nodes"].items()
    }
    return {
        "consistent": consistent,
        "impossible_errors": impossible_errors,
        "assertion_errors": assertion_errors,
        "simulation_status": sim["status"],
        "committed_version": sim["final_state"]["committed_version"],
        "authorization_projection": sim["authorization_projection"],
        "node_recovery": node_recovery,
        "fatal": sim.get("fatal"),
        "violations": sim.get("violations", []),
        "simulation": sim,
    }


def history_summary(index: int, events: list[dict[str, Any]], evaluation: dict[str, Any]) -> dict[str, Any]:
    projection = evaluation["authorization_projection"]
    return {
        "history_index": index,
        "event_digest": sha256_hex(canonical_json(events)),
        "events": events,
        "committed_version": evaluation["committed_version"],
        "authorization_projection": projection,
        "projection_digest": sha256_hex(canonical_json(projection)),
        "node_recovery": evaluation["node_recovery"],
        "simulation_status": evaluation["simulation_status"],
        "fatal": evaluation["fatal"],
    }


def enumerate_histories(doc: dict[str, Any], *, include_simulations: bool = False) -> dict[str, Any]:
    initial, slots, choices = normalize_trace(doc)
    limit = int(doc.get("history_limit", 10000))
    legal = []
    inconsistent_examples = []
    examined = 0
    for events in completion_iter(slots, choices):
        examined += 1
        evaluation = evaluate_completion(initial, events)
        if evaluation["consistent"]:
            summary = history_summary(len(legal), events, evaluation)
            if include_simulations or doc.get("include_simulations"):
                summary["simulation"] = evaluation["simulation"]
            legal.append(summary)
            if len(legal) >= limit:
                break
        elif len(inconsistent_examples) < 20:
            inconsistent_examples.append(
                {
                    "events": events,
                    "assertion_errors": evaluation["assertion_errors"],
                    "impossible_errors": evaluation["impossible_errors"],
                }
            )
    total_possible = 1
    for slot, local_choices in zip(slots, choices):
        total_possible *= 1 if slot is not None else len(local_choices or [])
    return {
        "initial": initial,
        "slots": slots,
        "choices": choices,
        "total_possible_completions": total_possible,
        "examined": examined,
        "enumeration_complete": examined >= total_possible,
        "legal_histories": legal,
        "inconsistent_examples": inconsistent_examples,
    }


def _prefix_doc(base_doc: dict[str, Any], length: int) -> dict[str, Any]:
    prefix = copy.deepcopy(base_doc)
    for key in ("events", "steps"):
        if key in prefix:
            prefix[key] = prefix[key][:length]
    if "entries" in prefix:
        prefix["entries"] = [e for e in prefix["entries"] if int(e["seq"]) < length]
        prefix["length"] = length
    prefix.pop("history_limit", None)
    return prefix


def shortest_contradictory_prefix(doc: dict[str, Any]) -> dict[str, Any] | None:
    _, slots, _ = normalize_trace(doc)
    for length in range(1, len(slots) + 1):
        prefix_doc = _prefix_doc(doc, length)
        result = enumerate_histories(prefix_doc)
        if not result["legal_histories"]:
            reason: dict[str, Any] = {}
            if result["inconsistent_examples"]:
                example = result["inconsistent_examples"][0]
                reason = {
                    "assertion_errors": example.get("assertion_errors"),
                    "impossible_errors": example.get("impossible_errors"),
                }
            fixed_entries = [e for e in slots[:length] if e is not None]
            return {
                "prefix_length": length,
                "observed_entry_count": len(fixed_entries),
                "prefix": slots[:length],
                "fixed_entries": fixed_entries,
                "reason": reason,
            }
    return None


def _prefix_authorization_variants(doc: dict[str, Any], length: int) -> list[dict[str, Any]]:
    result = enumerate_histories(_prefix_doc(doc, length))
    variants = {}
    for history in result["legal_histories"]:
        key = history["projection_digest"]
        variants.setdefault(key, history)
    return list(variants.values())


def minimal_authorization_evidence(doc: dict[str, Any], histories: list[dict[str, Any]]) -> dict[str, Any] | None:
    distinct = {h["projection_digest"]: h for h in histories}
    if len(distinct) <= 1:
        return None

    _, slots, _ = normalize_trace(doc)
    for length in range(1, len(slots) + 1):
        variants = _prefix_authorization_variants(doc, length)
        if len(variants) >= 2:
            witnesses = variants[:2]
            a_events = witnesses[0]["events"]
            b_events = witnesses[1]["events"]
            diff_slot = next(
                (i for i in range(min(len(a_events), len(b_events))) if a_events[i] != b_events[i]),
                None,
            )
            return {
                "prefix_length": length,
                "observed_prefix": [e for e in slots[:length] if e is not None],
                "distinguishing_slot": diff_slot,
                "candidate_a": a_events[diff_slot] if diff_slot is not None else None,
                "candidate_b": b_events[diff_slot] if diff_slot is not None else None,
                "witness_event_sequences": [a_events, b_events],
                "authorization_projections": [
                    witnesses[0]["authorization_projection"],
                    witnesses[1]["authorization_projection"],
                ],
            }

    # Defensive fallback: the full trace itself distinguishes them.
    values = list(distinct.values())[:2]
    return {
        "prefix_length": len(slots),
        "distinguishing_slot": None,
        "witness_event_sequences": [h["events"] for h in values],
        "authorization_projections": [h["authorization_projection"] for h in values],
    }


def classify_trace_faults(histories: list[dict[str, Any]]) -> dict[str, Any]:
    classes: dict[str, int] = {}
    committed_corruption_histories = []
    incomplete_histories = []
    for history in histories:
        recovery = history.get("node_recovery", {})
        labels = set(recovery.values())
        if history.get("fatal") or "committed_log_corruption" in labels:
            classes["committed_log_corruption"] = classes.get("committed_log_corruption", 0) + 1
            committed_corruption_histories.append(history["history_index"])
        elif "last_frame_incomplete" in labels:
            classes["last_frame_incomplete"] = classes.get("last_frame_incomplete", 0) + 1
            incomplete_histories.append(history["history_index"])
        else:
            classes["clean_or_safely_rejected"] = classes.get("clean_or_safely_rejected", 0) + 1
    return {
        "counts": classes,
        "committed_log_corruption_histories": committed_corruption_histories,
        "last_frame_incomplete_histories": incomplete_histories,
        "distinction": (
            "last_frame_incomplete means the newest unsealed prefix may be discarded/retried; "
            "committed_log_corruption means a published marker references bad/missing committed bytes "
            "and requires restoration, never silent truncation."
        ),
    }


def check_trace(doc: dict[str, Any] | str | Path) -> dict[str, Any]:
    if isinstance(doc, (str, Path)):
        doc = load_trace(doc)
    elif isinstance(doc, list):
        doc = {"events": doc}
    if not isinstance(doc, dict):
        raise ValueError("TRACE must be a JSON object or array")

    enumerated = enumerate_histories(doc)
    histories = enumerated["legal_histories"]
    contradiction = shortest_contradictory_prefix(doc) if not histories else None

    auth_variants = {h["projection_digest"] for h in histories}
    minimal_evidence = (
        minimal_authorization_evidence(doc, histories) if len(auth_variants) > 1 else None
    )
    faults = classify_trace_faults(histories)

    if not histories:
        status = "no_legal_history"
    elif len(auth_variants) > 1:
        status = "ambiguous_authorization"
    elif len(histories) > 1:
        status = "unique_authorization_projection"
    else:
        status = "unique_history"

    return {
        "schema": "crashfence.trace-report.v1",
        "status": status,
        "trace_name": doc.get("name", "unnamed-trace"),
        "event_count": len(enumerated["slots"]),
        "max_reference_events": int(doc.get("max_events", MAX_REFERENCE_EVENTS)),
        "total_possible_completions": enumerated["total_possible_completions"],
        "examined": enumerated["examined"],
        "enumeration_complete": enumerated["enumeration_complete"],
        "legal_history_count": len(histories),
        "authorization_projection_count": len(auth_variants),
        "histories": histories,
        "projections": [h["authorization_projection"] for h in histories],
        "shortest_contradictory_prefix": contradiction,
        "contradictory_prefix": contradiction,
        "minimal_authorization_evidence": minimal_evidence,
        "minimal_evidence": minimal_evidence,
        "fault_classification": faults,
    }
