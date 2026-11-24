"""Deterministic in-memory model for Crashfence scenarios.

The model is deliberately explicit: cross-actor messages have to be sent and
delivered by a scenario step, disk writes have to be named, and crashes do not
silently repair anything.  The JSON-friendly state can be projected after each
step by :func:`safe_state`.
"""

from __future__ import annotations

import copy
from typing import Any

from .canonical import canonical_bytes, sha256_hex

PHYSICAL_NODES = ("n1", "n2", "n3")
SLOT_NAMES = ("s0", "s1")
QUORUM = 2
GENESIS_DIGEST = sha256_hex({"crashfence": "genesis", "version": 0})

TERMINAL_PROPOSAL = {"committed", "aborted", "superseded", "rejected"}


class ScenarioFormatError(ValueError):
    """Raised when a scenario/trace document does not match the declared format."""


def _new_id(prefix: str, supplied: Any, counter: int) -> str:
    return str(supplied) if supplied else f"{prefix}-{counter}"


def resolve_token(state: dict[str, Any], ref: Any) -> dict[str, Any] | None:
    if ref is None:
        return None
    if isinstance(ref, str):
        return state["tokens"].get(ref)
    if isinstance(ref, dict):
        if "digest" in ref and "epoch" in ref and "coordinator" in ref:
            return ref
        token_id = ref.get("token_id") or ref.get("id")
        if token_id:
            stored = state["tokens"].get(str(token_id))
            if stored is not None:
                return stored
        # A partial reference containing epoch can be resolved by epoch.
        epoch = ref.get("epoch")
        if epoch is not None:
            matches = [t for t in state["tokens"].values() if t.get("epoch") == epoch]
            if len(matches) == 1:
                return matches[0]
    return None


def token_digest(token: dict[str, Any]) -> str:
    core = {
        "epoch": token["epoch"],
        "coordinator": token["coordinator"],
        "lease_id": token.get("lease_id"),
        "not_before": token["not_before"],
        "expires_at": token["expires_at"],
    }
    return sha256_hex({"owner_token": core})


def lease_active(token: dict[str, Any], at: int) -> bool:
    """Half-open lease interval; ``expires_at == next.not_before`` is legal."""

    return int(token["not_before"]) <= int(at) < int(token["expires_at"])


def make_frame(
    *,
    version: int,
    proposal: dict[str, Any],
    participants: list[str],
    prev_digest: str,
) -> dict[str, Any]:
    core = {
        "version": int(version),
        "proposal": proposal,
        "participants": sorted(participants),
        "prev_digest": prev_digest,
    }
    digest = sha256_hex(core)
    return {**core, "digest": digest}


def initial_state(doc: dict[str, Any] | None = None) -> dict[str, Any]:
    doc = doc or {}
    if not isinstance(doc, dict):
        raise ScenarioFormatError("scenario must be a JSON object")

    raw_nodes = doc.get("nodes", list(PHYSICAL_NODES))
    nodes: dict[str, Any] = {}
    for item in raw_nodes:
        if isinstance(item, str):
            node_id, physical_id = item, item
        elif isinstance(item, dict):
            node_id = str(item["id"])
            physical_id = str(item.get("physical_id", node_id))
        else:
            raise ScenarioFormatError("nodes must be strings or objects")
        if node_id in nodes:
            raise ScenarioFormatError(f"duplicate node {node_id}")
        nodes[node_id] = {
            "id": node_id,
            "physical_id": physical_id,
            "up": True,
            "generation": 1,
            "slots": {
                name: {"name": name, "records": []} for name in SLOT_NAMES
            },
            "commit_markers": [],
            "last_complete_owner": None,
            "recovery": {"classification": "clean", "restart_count": 0},
            "events": [],
        }

    readers: dict[str, Any] = {}
    for item in doc.get("readers", []):
        if isinstance(item, str):
            reader_id = item
            upstream = f"upstream-{reader_id}"
            held: list[str] = []
        elif isinstance(item, dict):
            reader_id = str(item["id"])
            upstream = str(item.get("upstream", f"upstream-{reader_id}"))
            held = [str(x) for x in item.get("possession_tokens", item.get("tokens", []))]
        else:
            raise ScenarioFormatError("readers must be strings or objects")
        readers[reader_id] = {
            "id": reader_id,
            "upstream": upstream,
            "held_tokens": sorted(set(held)),
            "observations": [],
            "last_view": None,
        }

    policy = copy.deepcopy(doc.get("initial_policy", {}))
    policy.setdefault("policy_id", "simulated-policy")
    policy.setdefault("version", 0)
    policy.setdefault("authorizations", {})
    policy.setdefault("revoked", [])

    return {
        "schema": "crashfence.state.v1",
        "clock": int(doc.get("initial_clock", 0)),
        "counter": 0,
        "write_seq": 0,
        "nodes": nodes,
        "readers": readers,
        "observers": list(doc.get("observers", ["obs-a", "obs-b"])),
        "tokens": {},
        "owner": {
            "current_token_id": None,
            "last_complete": None,
            "history": [],
        },
        "proposals": {},
        "request_order": [],
        "committed_log": [],
        "commit_markers": {},
        "snapshots": {},
        "messages": {},
        "receipts": {},
        "client_responses": {},
        "policy": policy,
        "version": 0,
        "tip_digest": GENESIS_DIGEST,
        "events": [],
        "violations": [],
        "fatal": None,
    }


def _bump_counter(state: dict[str, Any]) -> int:
    state["counter"] += 1
    return state["counter"]


def _event(step_result: dict[str, Any], event: str, **fields: Any) -> None:
    item = {"event": event, **fields}
    step_result.setdefault("events", []).append(item)


def _reject(step_result: dict[str, Any], code: str, message: str, **fields: Any) -> dict[str, Any]:
    step_result["accepted"] = False
    step_result["outcome"] = "rejected"
    step_result["error"] = {"code": code, "message": message, **fields}
    _event(step_result, "rejected", code=code, message=message, **fields)
    return step_result


def _success(step_result: dict[str, Any], **fields: Any) -> dict[str, Any]:
    step_result["accepted"] = True
    step_result["outcome"] = fields.pop("outcome", "success")
    step_result.update(fields)
    return step_result


def _violation(state: dict[str, Any], code: str, message: str, **fields: Any) -> None:
    item = {"code": code, "message": message, **fields}
    if item not in state["violations"]:
        state["violations"].append(item)
    if state["fatal"] is None and code in {
        "FAKE_SUCCESS_WITHOUT_COMMIT",
        "COMMITTED_LOG_CORRUPTION",
        "READER_WITHOUT_POSSESSION_TOKEN",
    }:
        # Reader violation is recorded only when a trace tries to assert an
        # illegal visible view; ordinary denied reads remain safe.
        if code != "READER_WITHOUT_POSSESSION_TOKEN":
            state["fatal"] = item


def _physical_quorum_nodes(state: dict[str, Any], node_ids: list[str]) -> list[str]:
    seen: dict[str, str] = {}
    for node_id in node_ids:
        node = state["nodes"][node_id]
        seen.setdefault(node["physical_id"], node_id)
    return sorted(seen.values())


def _proposal_conflict_key(proposal: dict[str, Any]) -> str:
    if proposal.get("conflict_key"):
        return str(proposal["conflict_key"])
    target = proposal.get("target") or {}
    if isinstance(target, str):
        return target
    if proposal["kind"] == "authorize":
        auth = proposal.get("authorization", {})
        return str(
            auth.get("conflict_key")
            or auth.get("policy_id")
            or auth.get("resource")
            or auth.get("action")
            or proposal["id"]
        )
    rev = proposal.get("revocation", {})
    return str(
        rev.get("conflict_key")
        or rev.get("target_auth_id")
        or rev.get("policy_id")
        or rev.get("resource")
        or proposal["id"]
    )


def _matching_revocation(state: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any] | None:
    if proposal["kind"] != "authorize":
        return None
    key = _proposal_conflict_key(proposal)
    for other_id in reversed(state["request_order"]):
        other = state["proposals"][other_id]
        if other["kind"] != "revoke":
            continue
        other_key = _proposal_conflict_key(other)
        target_auth = other.get("revocation", {}).get("target_auth_id")
        if (
            other_key == key
            or target_auth == proposal["id"]
            or target_auth == proposal.get("authorization", {}).get("auth_id")
        ):
            if other["status"] not in {"aborted", "rejected"}:
                return other
    return None


def resolve_owner_for_write(
    state: dict[str, Any], node: dict[str, Any], step: dict[str, Any], result: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    token = resolve_token(state, step.get("token"))
    if token is None:
        return None, _reject(result, "MISSING_OWNER_TOKEN", "write requires a complete owner token")
    digest = token.get("digest") or token_digest(token)
    token = {**token, "digest": digest}

    local_owner = node.get("last_complete_owner")
    if local_owner and int(token["epoch"]) < int(local_owner["epoch"]):
        return None, _reject(
            result,
            "FENCED_STALE_TOKEN",
            "node has recovered a newer complete owner token",
            token_epoch=token["epoch"],
            local_epoch=local_owner["epoch"],
        )
    at = int(step.get("at", state["clock"]))
    if step.get("check_lease", True) and not lease_active(token, at):
        return None, _reject(
            result,
            "LEASE_NOT_ACTIVE",
            "owner token lease is not active at this step",
            at=at,
            not_before=token["not_before"],
            expires_at=token["expires_at"],
        )
    return token, None


# ---------------------------------------------------------------------------
# State projection and recovery
# ---------------------------------------------------------------------------


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in record.items() if k != "prefix_bytes"}
    if record.get("prefix_hex") is not None:
        out["prefix_hex"] = record["prefix_hex"]
    return out


def recover_node(state: dict[str, Any], node_id: str) -> dict[str, Any]:
    node = state["nodes"][node_id]
    # A restart derives the owner only from complete on-disk token frames; any
    # in-memory token pointer from before the crash must not be trusted.
    node["last_complete_owner"] = None
    invalid_committed: list[dict[str, Any]] = []
    incomplete_tail: dict[str, Any] | None = None
    invalid_uncommitted: list[dict[str, Any]] = []
    last_owner: dict[str, Any] | None = None
    highest_seq = -1
    highest_incomplete_seq = -1
    newest_seq = -1
    newest_record: dict[str, Any] | None = None

    marker_digests = {m["frame_digest"]: m for m in node.get("commit_markers", [])}

    for slot_name in SLOT_NAMES:
        for record in node["slots"][slot_name]["records"]:
            seq = int(record.get("write_seq", -1))
            if seq > newest_seq:
                newest_seq = seq
                newest_record = record
            frame = record.get("frame")
            digest = record.get("frame_digest")
            complete = bool(record.get("complete"))
            checksum_valid = bool(record.get("checksum_valid", complete))

            if not complete:
                if seq > highest_incomplete_seq:
                    highest_incomplete_seq = seq
                    incomplete_tail = {
                        "slot": slot_name,
                        "write_seq": seq,
                        "reason": record.get("reason", "incomplete_frame"),
                        "expected_bytes": record.get("expected_bytes"),
                        "actual_bytes": record.get("actual_bytes"),
                    }
                continue

            if not checksum_valid or frame is None:
                if digest in marker_digests:
                    invalid_committed.append(
                        {"slot": slot_name, "write_seq": seq, "frame_digest": digest}
                    )
                else:
                    invalid_uncommitted.append(
                        {"slot": slot_name, "write_seq": seq, "frame_digest": digest}
                    )
                continue

            expected_digest = make_frame(
                version=frame["version"],
                proposal=frame["proposal"],
                participants=frame["participants"],
                prev_digest=frame["prev_digest"],
            )["digest"]
            if expected_digest != frame["digest"] or frame["digest"] != digest:
                if digest in marker_digests or frame["digest"] in marker_digests:
                    invalid_committed.append(
                        {"slot": slot_name, "write_seq": seq, "frame_digest": digest}
                    )
                else:
                    invalid_uncommitted.append(
                        {"slot": slot_name, "write_seq": seq, "frame_digest": digest}
                    )
                continue

            proposal = frame.get("proposal", {})
            if proposal.get("kind") == "owner_token":
                token = proposal.get("token")
                if token and seq > highest_seq:
                    highest_seq = seq
                    last_owner = {**token, "digest": frame["digest"]}

    # A commit marker may survive while its frame is missing entirely.
    found_digests = set()
    for slot_name in SLOT_NAMES:
        for record in node["slots"][slot_name]["records"]:
            if record.get("complete") and record.get("checksum_valid", True):
                found_digests.add(record.get("frame_digest"))
    for digest, marker in marker_digests.items():
        if digest not in found_digests:
            invalid_committed.append(
                {"frame_digest": digest, "marker_id": marker.get("marker_id"), "missing": True}
            )

    newest_is_incomplete = bool(newest_record and not newest_record.get("complete"))
    if invalid_committed:
        classification = "committed_log_corruption"
    elif newest_is_incomplete:
        classification = "last_frame_incomplete"
    elif invalid_uncommitted:
        classification = "uncommitted_frame_corruption"
    else:
        classification = "clean"

    if last_owner is not None:
        node["last_complete_owner"] = last_owner
        if state["owner"]["last_complete"] is None or int(last_owner["epoch"]) >= int(
            state["owner"]["last_complete"]["epoch"]
        ):
            # Recovery is local.  The global projection reports the maximum
            # complete token any restart has observed; scenarios still decide
            # whether a quorum observed it.
            if state["owner"]["last_complete"] is None or int(last_owner["epoch"]) > int(
                state["owner"]["last_complete"]["epoch"]
            ):
                state["owner"]["last_complete"] = last_owner
                state["owner"]["current_token_id"] = last_owner.get("token_id")

    report = {
        "classification": classification,
        "invalid_committed": invalid_committed,
        "incomplete_tail": incomplete_tail if newest_is_incomplete else None,
        "obsolete_incomplete_records": [] if newest_is_incomplete or incomplete_tail is None else [incomplete_tail],
        "invalid_uncommitted": invalid_uncommitted,
        "last_complete_owner_epoch": (last_owner or node.get("last_complete_owner") or {}).get("epoch"),
    }
    node["recovery"] = report
    if classification == "committed_log_corruption":
        _violation(
            state,
            "COMMITTED_LOG_CORRUPTION",
            "a published commit marker refers to a corrupt or missing local frame",
            node=node_id,
            details=invalid_committed,
        )
    return report


def safe_state(state: dict[str, Any]) -> dict[str, Any]:
    """Return the JSON-friendly state exposed after a step."""

    nodes_out: dict[str, Any] = {}
    for node_id, node in state["nodes"].items():
        nodes_out[node_id] = {
            "physical_id": node["physical_id"],
            "up": node["up"],
            "generation": node["generation"],
            "last_complete_owner_epoch": (node.get("last_complete_owner") or {}).get("epoch"),
            "recovery": node.get("recovery"),
            "commit_markers": copy.deepcopy(node.get("commit_markers", [])),
            "slots": {
                name: {
                    "records": [_public_record(r) for r in slot["records"]],
                }
                for name, slot in node["slots"].items()
            },
            "events": list(node.get("events", [])),
        }

    readers_out: dict[str, Any] = {}
    for reader_id, reader in state["readers"].items():
        readers_out[reader_id] = {
            "upstream": reader["upstream"],
            "held_tokens": list(reader["held_tokens"]),
            "last_view": copy.deepcopy(reader["last_view"]),
            "observations": list(reader["observations"]),
        }

    proposals_out = {
        pid: {
            "kind": p["kind"],
            "status": p["status"],
            "requested_index": p["requested_index"],
            "conflict_key": _proposal_conflict_key(p),
            "frame_version": p.get("frame_version"),
            "frame_digest": p.get("frame_digest"),
            "durable_nodes": list(p.get("durable_nodes", [])),
            "quorum_satisfied": len(_physical_quorum_nodes(state, p.get("durable_nodes", [])))
            >= QUORUM,
            "commit_marker_id": p.get("commit_marker_id"),
            "client_outcome": p.get("client_outcome"),
            "duplicate_writes": p.get("duplicate_writes", 0),
        }
        for pid, p in state["proposals"].items()
    }

    return {
        "clock": state["clock"],
        "committed_version": state["version"],
        "tip_digest": state["tip_digest"],
        "owner_epoch": (state["owner"].get("last_complete") or {}).get("epoch"),
        "fatal": state["fatal"],
        "violations": list(state["violations"]),
        "nodes": nodes_out,
        "readers": readers_out,
        "proposals": proposals_out,
        "policy": copy.deepcopy(state["policy"]),
        "authorization_projection": authorization_projection(state),
    }


def authorization_projection(state: dict[str, Any]) -> dict[str, Any]:
    """Stable projection used by trace ambiguity/minimal-evidence reports."""

    auths: dict[str, Any] = {}
    for auth_id, grant in state["policy"].get("authorizations", {}).items():
        auths[auth_id] = {
            "decision": grant.get("status", "granted"),
            "committed_version": grant.get("committed_version"),
        }
    for pid in state["request_order"]:
        p = state["proposals"][pid]
        if p["kind"] != "authorize":
            continue
        auth_id = p.get("authorization", {}).get("auth_id", pid)
        auths.setdefault(
            auth_id,
            {
                "decision": p["status"]
                if p["status"] in {"uncertain", "aborted", "superseded", "rejected"}
                else "pending",
                "proposal_status": p["status"],
            },
        )
    reader_views = {}
    for rid, reader in state["readers"].items():
        view = reader.get("last_view") or {}
        reader_views[rid] = {
            "visible": bool(view.get("visible")),
            "decision": (view.get("policy") or {}).get("authorizations", {})
            if view.get("visible")
            else None,
            "reason": view.get("reason"),
            "snapshot_id": view.get("snapshot_id"),
        }
    return {"authorizations": auths, "readers": reader_views}


# ---------------------------------------------------------------------------
# Step handlers
# ---------------------------------------------------------------------------


def _step_clock(state, step, result):
    if "at" in step:
        state["clock"] = int(step["at"])
    elif step.get("op") == "advance_clock":
        state["clock"] = int(step.get("to", state["clock"] + 1))
    _success(result, clock=state["clock"])


def _step_grant_token(state, step, result):
    token = step.get("token")
    if not isinstance(token, dict):
        return _reject(result, "BAD_TOKEN", "grant_owner_token requires token object")
    required = ("epoch", "coordinator", "not_before", "expires_at")
    missing = [k for k in required if k not in token]
    if missing:
        return _reject(result, "BAD_TOKEN", "token missing fields", missing=missing)
    token = copy.deepcopy(token)
    token_id = str(step.get("token_id") or token.get("token_id") or f"token-{_bump_counter(state)}")
    token["token_id"] = token_id
    token["digest"] = token_digest(token)
    state["tokens"][token_id] = token
    state["owner"]["current_token_id"] = token_id
    state["owner"]["history"].append({"token_id": token_id, "digest": token["digest"]})
    _event(result, "owner_token_created", token_id=token_id, digest=token["digest"])
    _success(result, token_id=token_id, digest=token["digest"])


def _step_persist_owner_token(state, step, result):
    node_id = str(step["node"])
    if node_id not in state["nodes"]:
        return _reject(result, "UNKNOWN_NODE", f"unknown node {node_id}")
    node = state["nodes"][node_id]
    if not node["up"]:
        return _reject(result, "NODE_DOWN", f"node {node_id} is crashed")
    token = resolve_token(state, step.get("token"))
    if token is None:
        return _reject(result, "MISSING_OWNER_TOKEN", "unknown token reference")
    token = {**token, "digest": token.get("digest") or token_digest(token)}
    at = int(step.get("at", state["clock"]))
    if step.get("check_lease", False) and not lease_active(token, at):
        return _reject(result, "LEASE_NOT_ACTIVE", "token lease is not active")

    participants = [str(x) for x in step.get("participants", sorted(state["nodes"]))]
    proposal = {
        "kind": "owner_token",
        "token": {k: v for k, v in token.items() if k != "digest"},
    }
    version = int(step.get("version", 0))
    frame = make_frame(
        version=version,
        proposal=proposal,
        participants=participants,
        prev_digest=step.get("prev_digest", GENESIS_DIGEST),
    )
    # Ensure token digest and frame digest are both observable; frame digest is
    # what recovery uses as the complete owner record boundary.
    _append_record(state, node_id, step, frame, proposal_id=f"owner:{token['token_id']}")
    if step.get("complete", True) and not step.get("partial_bytes"):
        node["last_complete_owner"] = token
    _success(result, token_id=token["token_id"], frame_digest=frame["digest"])


def _append_record(
    state: dict[str, Any],
    node_id: str,
    step: dict[str, Any],
    frame: dict[str, Any],
    proposal_id: str,
) -> dict[str, Any]:
    node = state["nodes"][node_id]
    slot_name = str(step.get("slot") or f"s{int(frame['version']) % 2}")
    if slot_name not in SLOT_NAMES:
        slot_name = SLOT_NAMES[0]
    slot = node["slots"][slot_name]

    encoded = canonical_bytes(frame)
    partial_bytes = step.get("partial_bytes")
    corrupt = bool(step.get("corrupt", False))
    state["write_seq"] += 1
    write_seq = state["write_seq"]

    # Idempotency: an already complete identical frame is never appended twice.
    for record in slot["records"]:
        if record.get("frame_digest") == frame["digest"] and record.get("complete"):
            return {
                "duplicate": True,
                "record": record,
                "slot": slot_name,
                "write_seq": record["write_seq"],
            }

    if partial_bytes is not None:
        actual = max(0, min(int(partial_bytes), len(encoded)))
        record = {
            "write_seq": write_seq,
            "proposal_id": proposal_id,
            "slot": slot_name,
            "frame_digest": frame["digest"],
            "complete": False,
            "reason": "partial_frame_write",
            "expected_bytes": len(encoded),
            "actual_bytes": actual,
            "prefix_hex": encoded[:actual].hex(),
            "checksum_valid": False,
        }
        slot["records"].append(record)
        node["events"].append(f"partial_write:{write_seq}")
        return {"duplicate": False, "record": record, "slot": slot_name, "write_seq": write_seq}

    # A full retry of the same digest repairs the earlier torn tail instead of
    # leaving an obsolete incomplete record ahead of the replacement.
    repaired_index = None
    for index, prior in enumerate(slot["records"]):
        if prior.get("frame_digest") == frame["digest"] and not prior.get("complete"):
            repaired_index = index
            break

    record = {
        "write_seq": write_seq,
        "proposal_id": proposal_id,
        "slot": slot_name,
        "frame_digest": frame["digest"],
        "complete": True,
        "repaired_partial_write_seq": (
            slot["records"][repaired_index].get("write_seq")
            if repaired_index is not None
            else None
        ),
        "expected_bytes": len(encoded),
        "actual_bytes": len(encoded),
        "frame": copy.deepcopy(frame),
        "checksum_valid": not corrupt,
        "corruption": "digest_mismatch" if corrupt else None,
    }
    if repaired_index is not None:
        slot["records"][repaired_index] = record
        node["events"].append(f"repaired_write:{write_seq}")
        return {
            "duplicate": False,
            "repaired": True,
            "record": record,
            "slot": slot_name,
            "write_seq": write_seq,
        }

    slot["records"].append(record)
    node["events"].append(f"complete_write:{write_seq}")
    return {"duplicate": False, "record": record, "slot": slot_name, "write_seq": write_seq}


def _step_request_authorization(state, step, result):
    idx = _bump_counter(state)
    proposal_id = _new_id("auth", step.get("proposal_id") or step.get("id"), idx)
    if proposal_id in state["proposals"]:
        return _reject(result, "DUPLICATE_PROPOSAL_ID", "proposal id already exists")
    auth = copy.deepcopy(step.get("authorization", {}))
    if step.get("auth_id"):
        auth["auth_id"] = str(step["auth_id"])
    auth.setdefault("auth_id", proposal_id)
    auth.setdefault("principal", step.get("principal", "simulated-principal"))
    auth.setdefault("action", step.get("action", "invoke"))
    auth.setdefault("resource", step.get("resource", "simulated-resource"))
    if "policy_id" in step:
        auth["policy_id"] = step["policy_id"]
    proposal = {
        "id": proposal_id,
        "kind": "authorize",
        "status": "requested",
        "requested_index": len(state["request_order"]) + 1,
        "authorization": auth,
        "conflict_key": step.get("conflict_key"),
        "owner_epoch": (resolve_token(state, step.get("token")) or {}).get("epoch"),
        "writes": {},
        "durable_nodes": [],
        "duplicate_writes": 0,
        "client_outcome": "pending",
    }
    state["proposals"][proposal_id] = proposal
    state["request_order"].append(proposal_id)
    _success(result, proposal_id=proposal_id, status="requested")


def _step_request_revocation(state, step, result):
    idx = _bump_counter(state)
    proposal_id = _new_id("rev", step.get("proposal_id") or step.get("id"), idx)
    if proposal_id in state["proposals"]:
        return _reject(result, "DUPLICATE_PROPOSAL_ID", "proposal id already exists")
    revocation = copy.deepcopy(step.get("revocation", {}))
    for key in ("target_auth_id", "policy_id", "resource", "action"):
        if key in step:
            revocation[key] = step[key]
    proposal = {
        "id": proposal_id,
        "kind": "revoke",
        "status": "requested",
        "requested_index": len(state["request_order"]) + 1,
        "revocation": revocation,
        "conflict_key": step.get("conflict_key"),
        "priority": "revocation_before_uncommitted_authorization",
        "writes": {},
        "durable_nodes": [],
        "duplicate_writes": 0,
        "client_outcome": "pending",
    }
    state["proposals"][proposal_id] = proposal
    state["request_order"].append(proposal_id)
    _success(result, proposal_id=proposal_id, status="requested")


def _step_write_frame(state, step, result):
    proposal_id = str(step.get("proposal") or step.get("proposal_id"))
    if proposal_id not in state["proposals"]:
        return _reject(result, "UNKNOWN_PROPOSAL", f"unknown proposal {proposal_id}")
    proposal = state["proposals"][proposal_id]
    node_id = str(step.get("node"))
    if node_id not in state["nodes"]:
        return _reject(result, "UNKNOWN_NODE", f"unknown node {node_id}")
    node = state["nodes"][node_id]
    if not node["up"]:
        return _reject(result, "NODE_DOWN", f"node {node_id} is crashed")

    token, err = resolve_owner_for_write(state, node, step, result)
    if err is not None:
        return err

    version = int(step.get("version", proposal.get("frame_version") or state["version"] + 1))
    prev_digest = str(
        step.get("prev_digest")
        or proposal.get("prev_digest")
        or state["tip_digest"]
    )
    participants = [str(x) for x in step.get("participants", sorted(state["nodes"]))]
    frame_proposal = {
        "id": proposal_id,
        "kind": proposal["kind"],
        "payload": proposal.get("authorization") or proposal.get("revocation"),
        "owner_token": {
            "token_id": token["token_id"],
            "epoch": token["epoch"],
            "digest": token["digest"],
        },
    }
    frame = make_frame(
        version=version,
        proposal=frame_proposal,
        participants=participants,
        prev_digest=prev_digest,
    )
    if proposal.get("frame_digest") is None:
        proposal["frame_version"] = version
        proposal["prev_digest"] = prev_digest
        proposal["frame_digest"] = frame["digest"]
    elif proposal["frame_digest"] != frame["digest"]:
        # A rebased proposal is a new attempt and must use a new proposal id in
        # scenario terms.  Refuse to mutate the digest underneath quorum votes.
        return _reject(
            result,
            "FRAME_MISMATCH",
            "frame fields differ from the existing proposal attempt; rebase as a new proposal",
            expected_digest=proposal["frame_digest"],
            actual_digest=frame["digest"],
        )

    append_info = _append_record(state, node_id, step, frame, proposal_id)
    duplicate = bool(append_info.get("duplicate"))
    if duplicate:
        proposal["duplicate_writes"] += 1
        _event(result, "duplicate_frame_ignored", node=node_id, frame_digest=frame["digest"])
    else:
        proposal["writes"][node_id] = {
            "slot": append_info["slot"],
            "write_seq": append_info["write_seq"],
            "complete": bool(append_info["record"].get("complete")),
            "frame_digest": frame["digest"],
            "checksum_valid": bool(append_info["record"].get("checksum_valid")),
        }
        if append_info["record"].get("complete") and not step.get("corrupt", False):
            if node_id not in proposal["durable_nodes"]:
                proposal["durable_nodes"].append(node_id)
        proposal["status"] = "durable" if proposal["status"] == "requested" else proposal["status"]
        _event(
            result,
            "frame_written",
            node=node_id,
            slot=append_info["slot"],
            repaired=bool(append_info.get("repaired")),
            complete=append_info["record"].get("complete"),
            write_seq=append_info["write_seq"],
        )
        if append_info.get("repaired"):
            recover_node(state, node_id)

    quorum_nodes = _physical_quorum_nodes(state, proposal["durable_nodes"])
    _success(
        result,
        frame_digest=frame["digest"],
        duplicate=duplicate,
        slot=append_info["slot"],
        durable_nodes=list(proposal["durable_nodes"]),
        physical_quorum_nodes=quorum_nodes,
        quorum_satisfied=len(quorum_nodes) >= QUORUM,
    )


def _step_publish_commit_marker(state, step, result):
    proposal_id = str(step.get("proposal") or step.get("proposal_id"))
    if proposal_id not in state["proposals"]:
        return _reject(result, "UNKNOWN_PROPOSAL", f"unknown proposal {proposal_id}")
    proposal = state["proposals"][proposal_id]

    if proposal["status"] == "committed":
        return _success(
            result,
            duplicate=True,
            marker_id=proposal.get("commit_marker_id"),
            frame_digest=proposal.get("frame_digest"),
        )
    if proposal["status"] in {"aborted", "superseded", "rejected"}:
        return _reject(
            result,
            "PROPOSAL_NOT_COMMITTABLE",
            f"proposal is {proposal['status']}",
            status=proposal["status"],
        )

    token = resolve_token(state, step.get("token"))
    if step.get("require_active_lease", True):
        if token is None:
            return _reject(result, "MISSING_OWNER_TOKEN", "commit marker requires owner token")
        at = int(step.get("at", state["clock"]))
        if not lease_active(token, at):
            return _reject(result, "LEASE_NOT_ACTIVE", "commit lease is not active")
        local_epoch = (state["owner"].get("last_complete") or {}).get("epoch")
        if local_epoch is not None and int(token["epoch"]) < int(local_epoch):
            return _reject(result, "FENCED_STALE_TOKEN", "commit token is stale")

    quorum_nodes = _physical_quorum_nodes(state, proposal.get("durable_nodes", []))
    if len(quorum_nodes) < QUORUM:
        return _reject(
            result,
            "NO_QUORUM",
            "change is not durable on two distinct physical nodes",
            durable_nodes=proposal.get("durable_nodes", []),
            physical_quorum_nodes=quorum_nodes,
        )

    if proposal.get("frame_version") != state["version"] + 1:
        return _reject(
            result,
            "STALE_FRAME_VERSION",
            "frame is not built on the current log tip",
            frame_version=proposal.get("frame_version"),
            expected_version=state["version"] + 1,
        )
    if proposal.get("prev_digest") != state["tip_digest"]:
        return _reject(
            result,
            "STALE_PREV_DIGEST",
            "frame does not extend the current committed tip; rebase required",
            prev_digest=proposal.get("prev_digest"),
            tip_digest=state["tip_digest"],
        )

    blocking = _matching_revocation(state, proposal)
    if blocking is not None:
        proposal["status"] = "aborted"
        proposal["abort_reason"] = "revocation_priority"
        blocking.setdefault("blocks_authorization", []).append(proposal_id)
        _event(result, "authorization_aborted_for_revocation", blocking_revocation=blocking["id"])
        return _reject(
            result,
            "REVOCATION_PRIORITY",
            "a revocation for this conflict key takes precedence over uncommitted authorization",
            revocation=blocking["id"],
            authorization_requested_index=proposal["requested_index"],
            revocation_requested_index=blocking["requested_index"],
        )

    marker_id = _new_id("cm", step.get("marker_id") or step.get("id"), _bump_counter(state))
    version = state["version"] + 1
    frame_digest = proposal["frame_digest"]
    marker = {
        "marker_id": marker_id,
        "version": version,
        "proposal_id": proposal_id,
        "frame_digest": frame_digest,
        "physical_nodes": quorum_nodes,
        "published_at": state["clock"],
        "token_epoch": (token or {}).get("epoch"),
    }
    state["commit_markers"][marker_id] = marker
    state["committed_log"].append(
        {
            "version": version,
            "frame_digest": frame_digest,
            "proposal_id": proposal_id,
            "marker_id": marker_id,
            "participants": quorum_nodes,
        }
    )
    state["version"] = version
    state["tip_digest"] = frame_digest
    proposal["status"] = "committed"
    proposal["commit_marker_id"] = marker_id
    proposal["committed_version"] = version
    proposal["client_outcome"] = "success"

    # Publish marker locally to the quorum nodes that hold the frame.
    for node_id in quorum_nodes:
        state["nodes"][node_id]["commit_markers"].append(copy.deepcopy(marker))

    payload = proposal.get("authorization") or proposal.get("revocation") or {}
    if proposal["kind"] == "authorize":
        auth_id = payload.get("auth_id", proposal_id)
        state["policy"]["authorizations"][auth_id] = {
            **copy.deepcopy(payload),
            "status": "granted",
            "committed_version": version,
            "marker_id": marker_id,
        }
    else:
        rev = payload
        target_auth = rev.get("target_auth_id")
        if target_auth and target_auth in state["policy"]["authorizations"]:
            state["policy"]["authorizations"][target_auth]["status"] = "revoked"
            state["policy"]["authorizations"][target_auth]["revoked_version"] = version
        if target_auth:
            state["policy"]["revoked"].append(target_auth)
        if rev.get("policy_id"):
            state["policy"]["version"] = version
        # Revocation wins over every conflicting not-yet-committed grant, even
        # when that authorization request has a smaller request index.
        for other in state["proposals"].values():
            if other["kind"] != "authorize" or other["status"] == "committed":
                continue
            if _proposal_conflict_key(other) == _proposal_conflict_key(proposal) or (
                target_auth
                and (
                    other["id"] == target_auth
                    or other.get("authorization", {}).get("auth_id") == target_auth
                )
            ):
                other["status"] = "superseded"
                other["superseded_by"] = proposal_id

    _success(result, marker_id=marker_id, version=version, frame_digest=frame_digest)


def _step_crash_node(state, step, result):
    node_id = str(step["node"])
    if node_id not in state["nodes"]:
        return _reject(result, "UNKNOWN_NODE", f"unknown node {node_id}")
    node = state["nodes"][node_id]
    node["up"] = False
    node["crashed_generation"] = node["generation"]
    # Volatile state would be lost here.  This model only keeps explicitly
    # persisted slot records, so there is no hidden durable-looking state.
    _success(result, node=node_id, generation=node["generation"])


def _step_restart_node(state, step, result):
    node_id = str(step["node"])
    if node_id not in state["nodes"]:
        return _reject(result, "UNKNOWN_NODE", f"unknown node {node_id}")
    node = state["nodes"][node_id]
    was_up = node["up"]
    node["up"] = True
    if not was_up:
        node["generation"] += 1
    node["recovery"]["restart_count"] = int(node["recovery"].get("restart_count", 0)) + 1
    report = recover_node(state, node_id)
    _success(result, node=node_id, generation=node["generation"], recovery=report)


def _step_inject_corruption(state, step, result):
    node_id = str(step["node"])
    slot_name = str(step.get("slot", SLOT_NAMES[0]))
    index = int(step.get("record_index", -1))
    slot = state["nodes"][node_id]["slots"][slot_name]
    if not slot["records"]:
        return _reject(result, "NO_RECORD", "slot has no record to corrupt")
    record = slot["records"][index]
    record["complete"] = True
    record["checksum_valid"] = False
    record["corruption"] = step.get("kind", "bit_flip")
    record["frame"] = record.get("frame") or {"corrupt": True}
    report = recover_node(state, node_id)
    _success(result, node=node_id, recovery=report)


def _step_send_message(state, step, result):
    message = step.get("message") or {}
    message_id = str(step.get("id") or message.get("id") or f"m-{_bump_counter(state)}")
    if message_id in state["messages"]:
        return _reject(result, "DUPLICATE_MESSAGE_ID", "message id already exists")
    message = copy.deepcopy(message)
    message["id"] = message_id
    message.setdefault("status", "queued")
    message.setdefault("sent_at", state["clock"])
    state["messages"][message_id] = message
    _success(result, message_id=message_id, status="queued")


def _step_deliver_message(state, step, result):
    message_id = str(step.get("id") or step.get("message_id"))
    message = state["messages"].get(message_id)
    if message is None:
        # Allow an inline message for compact trace fixtures.
        if step.get("message"):
            send_result = {"events": []}
            _step_send_message(state, {"message": step["message"], "id": message_id}, send_result)
            message = state["messages"].get(message_id)
        else:
            return _reject(result, "UNKNOWN_MESSAGE", f"unknown message {message_id}")
    if message.get("status") == "delivered":
        return _success(result, duplicate=True, message_id=message_id)
    message["status"] = "delivered"
    message["delivered_at"] = state["clock"]
    inner = message.get("step") or message.get("payload", {}).get("step")
    if inner:
        inner_result: dict[str, Any] = {"op": inner.get("op"), "events": []}
        apply_step(state, inner, inner_result)
        result["inner_result"] = {
            "accepted": inner_result.get("accepted"),
            "outcome": inner_result.get("outcome"),
            "error": inner_result.get("error"),
        }
    _success(result, message_id=message_id, delivered=True)


def _step_grant_read_token(state, step, result):
    reader_id = str(step["reader"])
    if reader_id not in state["readers"]:
        return _reject(result, "UNKNOWN_READER", f"unknown reader {reader_id}")
    token = str(step.get("token") or step.get("confirmation_token"))
    reader = state["readers"][reader_id]
    if token not in reader["held_tokens"]:
        reader["held_tokens"].append(token)
        reader["held_tokens"].sort()
    _success(result, reader=reader_id, token=token)


def _step_publish_read_snapshot(state, step, result):
    snapshot_id = str(step.get("snapshot_id") or f"snap-{_bump_counter(state)}")
    version = int(step.get("version", state["version"]))
    if version < 1 or version > state["version"]:
        return _reject(result, "SNAPSHOT_NOT_COMMITTED", "snapshot version is not committed")
    log_entry = next((x for x in state["committed_log"] if x["version"] == version), None)
    if log_entry is None:
        return _reject(result, "SNAPSHOT_NOT_COMMITTED", "missing committed log entry")
    upstream = str(step.get("upstream", f"upstream-{step.get('reader', 'default')}"))
    token = str(
        step.get("confirmation_token")
        or step.get("token")
        or f"read-token-{log_entry['frame_digest'][:16]}"
    )
    snapshot = {
        "snapshot_id": snapshot_id,
        "upstream": upstream,
        "version": version,
        "frame_digest": log_entry["frame_digest"],
        "confirmation_token": token,
        "published_at": state["clock"],
        "policy": copy.deepcopy(state["policy"]),
    }
    # Do not leak internal proposal bookkeeping into reader policy projection.
    snapshot["policy"] = {
        "policy_id": state["policy"].get("policy_id"),
        "version": state["policy"].get("version"),
        "authorizations": copy.deepcopy(state["policy"].get("authorizations", {})),
        "revoked": list(state["policy"].get("revoked", [])),
    }
    state["snapshots"][snapshot_id] = snapshot
    _success(result, snapshot_id=snapshot_id, confirmation_token=token, version=version)


def _step_reader_read(state, step, result):
    reader_id = str(step["reader"])
    if reader_id not in state["readers"]:
        return _reject(result, "UNKNOWN_READER", f"unknown reader {reader_id}")
    reader = state["readers"][reader_id]
    snapshot_id = step.get("snapshot_id")
    if snapshot_id:
        snapshot = state["snapshots"].get(str(snapshot_id))
        if snapshot is None:
            view = {"visible": False, "reason": "unknown_snapshot", "snapshot_id": snapshot_id}
        elif snapshot["upstream"] != reader["upstream"]:
            view = {
                "visible": False,
                "reason": "wrong_upstream",
                "snapshot_id": snapshot_id,
                "reader_upstream": reader["upstream"],
                "snapshot_upstream": snapshot["upstream"],
            }
        elif snapshot["confirmation_token"] not in reader["held_tokens"]:
            view = {
                "visible": False,
                "reason": "missing_confirmation_token",
                "snapshot_id": snapshot_id,
            }
        else:
            view = {
                "visible": True,
                "reason": None,
                "snapshot_id": snapshot_id,
                "version": snapshot["version"],
                "frame_digest": snapshot["frame_digest"],
                "confirmation_token": snapshot["confirmation_token"],
                "policy": copy.deepcopy(snapshot["policy"]),
            }
    else:
        candidates = [
            s
            for s in state["snapshots"].values()
            if s["upstream"] == reader["upstream"]
            and s["confirmation_token"] in reader["held_tokens"]
        ]
        if not candidates:
            # Distinguish wrong upstream from missing token when helpful.
            upstream_snapshots = [
                s for s in state["snapshots"].values() if s["upstream"] == reader["upstream"]
            ]
            reason = "missing_confirmation_token" if upstream_snapshots else "wrong_upstream"
            view = {"visible": False, "reason": reason}
        else:
            snapshot = max(candidates, key=lambda s: int(s["version"]))
            view = {
                "visible": True,
                "reason": None,
                "snapshot_id": snapshot["snapshot_id"],
                "version": snapshot["version"],
                "frame_digest": snapshot["frame_digest"],
                "confirmation_token": snapshot["confirmation_token"],
                "policy": copy.deepcopy(snapshot["policy"]),
            }
    reader["last_view"] = copy.deepcopy(view)
    reader["observations"].append(copy.deepcopy(view))
    _success(result, view=view)


def _step_redeem_receipt(state, step, result):
    receipt_id = str(step.get("receipt_id") or step.get("id"))
    if not receipt_id:
        return _reject(result, "MISSING_RECEIPT_ID", "receipt_id required")
    payload = {
        "authorization_id": step.get("authorization_id"),
        "action": step.get("action", "consume"),
        "nonce": step.get("nonce"),
    }
    existing = state["receipts"].get(receipt_id)
    if existing is not None:
        if existing["payload"] != payload:
            return _reject(result, "RECEIPT_CONFLICT", "receipt id reused with different payload")
        existing["duplicate_deliveries"] += 1
        return _success(
            result,
            receipt_id=receipt_id,
            duplicate=True,
            effect_applied=False,
            consumed_count=existing["consumed_count"],
        )
    state["receipts"][receipt_id] = {
        "payload": payload,
        "consumed_count": 1,
        "duplicate_deliveries": 0,
        "at": state["clock"],
    }
    _success(result, receipt_id=receipt_id, duplicate=False, effect_applied=True, consumed_count=1)


def _step_record_response(state, step, result):
    correlation_id = str(step.get("correlation_id") or step.get("id"))
    outcome = str(step.get("outcome", "uncertain"))
    proposal_id = step.get("proposal") or step.get("proposal_id")
    proposal = state["proposals"].get(str(proposal_id)) if proposal_id else None
    if outcome == "success":
        if proposal is None or proposal.get("status") != "committed":
            _violation(
                state,
                "FAKE_SUCCESS_WITHOUT_COMMIT",
                "a success response was recorded before a published commit marker",
                correlation_id=correlation_id,
                proposal=proposal_id,
            )
            return _reject(
                result,
                "FAKE_SUCCESS_WITHOUT_COMMIT",
                "uncertain or uncommitted operation must not be reported as success",
            )
    if outcome not in {"success", "uncertain", "failure", "retry"}:
        return _reject(result, "BAD_OUTCOME", "outcome must be success/uncertain/failure/retry")
    state["client_responses"][correlation_id] = {
        "outcome": outcome,
        "proposal": proposal_id,
        "at": state["clock"],
    }
    if proposal is not None and outcome == "uncertain":
        proposal["client_outcome"] = "uncertain"
    _success(result, correlation_id=correlation_id, outcome=outcome)


def _step_assert(state, step, result):
    """Trace observation/assertion.  Mismatches make a history inconsistent."""

    errors: list[dict[str, Any]] = []
    if "authorization" in step or "authorization_id" in step:
        auth_id = str(step.get("authorization_id") or step.get("authorization"))
        expected = step.get("expected_decision") or step.get("decision")
        projection = authorization_projection(state)["authorizations"].get(auth_id)
        actual = (projection or {}).get("decision", "unknown")
        if expected is not None and actual != expected:
            errors.append(
                {
                    "assertion": "authorization_decision",
                    "authorization_id": auth_id,
                    "expected": expected,
                    "actual": actual,
                }
            )
    if "reader" in step and ("expected_visible" in step or "visible" in step):
        reader_id = str(step["reader"])
        expected_visible = bool(step.get("expected_visible", step.get("visible")))
        view = (state["readers"].get(reader_id, {}).get("last_view") or {})
        actual_visible = bool(view.get("visible"))
        if expected_visible != actual_visible:
            errors.append(
                {
                    "assertion": "reader_visibility",
                    "reader": reader_id,
                    "expected_visible": expected_visible,
                    "actual_visible": actual_visible,
                    "reason": view.get("reason"),
                }
            )
        if expected_visible is True and actual_visible is False:
            _violation(
                state,
                "READER_WITHOUT_POSSESSION_TOKEN",
                "trace asserts a visible read without a valid possession token",
                reader=reader_id,
            )
    if "recovery_classification" in step:
        node_id = str(step.get("node"))
        actual = (state["nodes"].get(node_id, {}).get("recovery") or {}).get("classification")
        if actual != step["recovery_classification"]:
            errors.append(
                {
                    "assertion": "recovery_classification",
                    "node": node_id,
                    "expected": step["recovery_classification"],
                    "actual": actual,
                }
            )
    if "committed_version" in step:
        expected = int(step["committed_version"])
        if state["version"] != expected:
            errors.append(
                {
                    "assertion": "committed_version",
                    "expected": expected,
                    "actual": state["version"],
                }
            )
    if errors:
        result["accepted"] = False
        result["outcome"] = "assertion_failed"
        result["assertion_errors"] = errors
        _event(result, "assertion_failed", errors=errors)
        return result
    _success(result, assertion_passed=True)


HANDLERS = {
    "advance_clock": _step_clock,
    "set_clock": _step_clock,
    "grant_owner_token": _step_grant_token,
    "create_owner_token": _step_grant_token,
    "persist_owner_token": _step_persist_owner_token,
    "request_authorization": _step_request_authorization,
    "request_auth": _step_request_authorization,
    "request_revocation": _step_request_revocation,
    "request_revoke": _step_request_revocation,
    "write_frame": _step_write_frame,
    "persist_frame": _step_write_frame,
    "frame": _step_write_frame,
    "publish_commit_marker": _step_publish_commit_marker,
    "commit": _step_publish_commit_marker,
    "crash_node": _step_crash_node,
    "crash": _step_crash_node,
    "restart_node": _step_restart_node,
    "restart": _step_restart_node,
    "recover_node": _step_restart_node,
    "inject_corruption": _step_inject_corruption,
    "corrupt": _step_inject_corruption,
    "send_message": _step_send_message,
    "send": _step_send_message,
    "deliver_message": _step_deliver_message,
    "deliver": _step_deliver_message,
    "grant_read_token": _step_grant_read_token,
    "deliver_read_token": _step_grant_read_token,
    "publish_read_snapshot": _step_publish_read_snapshot,
    "reader_read": _step_reader_read,
    "read": _step_reader_read,
    "redeem_receipt": _step_redeem_receipt,
    "receipt": _step_redeem_receipt,
    "record_response": _step_record_response,
    "client_response": _step_record_response,
    "assert": _step_assert,
    "observe": _step_assert,
}


def apply_step(state: dict[str, Any], step: dict[str, Any], result: dict[str, Any] | None = None):
    if not isinstance(step, dict):
        raise ScenarioFormatError("each step must be a JSON object")
    op = str(step.get("op") or step.get("type") or "")
    result = result if result is not None else {"op": op, "events": []}
    result.setdefault("op", op)
    result.setdefault("index", step.get("index"))
    if "at" in step:
        state["clock"] = int(step["at"])

    handler = HANDLERS.get(op)
    if handler is None:
        _reject(result, "UNKNOWN_OP", f"unknown operation {op!r}")
        result["safe_state"] = safe_state(state)
        state["events"].append(
            {
                "op": op,
                "accepted": False,
                "outcome": "rejected",
                "error": result.get("error"),
            }
        )
        return result
    try:
        handler(state, step, result)
    except (KeyError, TypeError, ValueError) as exc:
        _reject(result, "STEP_ERROR", str(exc), step=step)
    result["safe_state"] = safe_state(state)
    state["events"].append(
        {
            "op": op,
            "accepted": result.get("accepted"),
            "outcome": result.get("outcome"),
            "error": result.get("error"),
        }
    )
    return result


def simulate_scenario(doc: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(doc, dict):
        raise ScenarioFormatError("scenario document must be an object")
    state = initial_state(doc)
    results = []
    for index, step in enumerate(doc.get("steps", []), start=1):
        step = copy.deepcopy(step)
        result = apply_step(state, step)
        result["index"] = index
        # Refresh index inside safe state? no need.
        results.append(result)

    status = "safe"
    if state["fatal"] is not None:
        status = "fatal_violation"
    elif any(r.get("outcome") == "assertion_failed" for r in results):
        status = "assertion_failed"
    elif state["violations"]:
        status = "safety_violation"

    return {
        "schema": "crashfence.simulation.v1",
        "status": status,
        "scenario_name": doc.get("name", "unnamed"),
        "deterministic": True,
        "step_count": len(results),
        "steps": results,
        "final_state": safe_state(state),
        "authorization_projection": authorization_projection(state),
        "violations": state["violations"],
        "fatal": state["fatal"],
    }
