"""Real local dual-slot files used by crash-subprocess tests.

This is intentionally a tiny simulated ledger, not a network service.  Every
file touched by this module lives under a ledger directory created by the
caller.  The on-disk representation is explicit so tests can kill a child at
one of six durable phases and then recover from the surviving bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from .canonical import canonical_bytes, sha256_hex

PHASE_COUNT = 6
PHASE_NAMES = {
    1: "owner_token_durable",
    2: "partial_frame_durable",
    3: "first_node_frame_sealed",
    4: "second_quorum_node_frame_sealed",
    5: "commit_marker_durable",
    6: "read_confirmation_token_durable",
}


def fsync_directory(path: Path) -> None:
    """Best-effort directory fsync; Windows does not support every form."""

    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        fd = os.open(str(path), flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except (OSError, AttributeError):
        # File fsync calls above are the primary durability boundary on
        # Windows; directory entry fsync is best effort.
        pass


def write_json_durable(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    fsync_directory(path.parent)


def write_bytes_durable(path: Path, data: bytes, mode: str = "wb") -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode) as handle:
        written = handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return written


def _slot_paths(node_dir: Path, slot: int) -> tuple[Path, Path]:
    log = node_dir / f"slot-{slot}.log"
    seal = node_dir / f"slot-{slot}.seal.json"
    return log, seal


def build_phase_case(root: Path) -> dict[str, Any]:
    token_core = {
        "epoch": 7,
        "coordinator": "obs-a",
        "lease_id": "lease-abut-1",
        "not_before": 100,
        "expires_at": 110,
    }
    token = {**token_core, "digest": sha256_hex({"owner_token": token_core})}
    frame_core = {
        "version": 1,
        "proposal": {
            "id": "phase-policy-1",
            "kind": "revoke",
            "payload": {"target_auth_id": "auth-old", "policy_id": "simulated-policy"},
            "owner_token": {"token_id": "lease-abut-1", "epoch": 7, "digest": token["digest"]},
        },
        "participants": ["n1", "n2", "n3"],
        "prev_digest": sha256_hex({"crashfence": "genesis", "version": 0}),
    }
    frame = {**frame_core, "digest": sha256_hex(frame_core)}
    encoded = canonical_bytes(frame)
    split = max(1, min(len(encoded) - 1, len(encoded) // 3))
    return {
        "token": token,
        "frame": frame,
        "encoded_hex": encoded.hex(),
        "partial_bytes": split,
        "commit_marker": {
            "marker_id": "cm-phase-1",
            "version": 1,
            "proposal_id": "phase-policy-1",
            "frame_digest": frame["digest"],
            "physical_nodes": ["n1", "n2"],
        },
        "read_token": f"read-token-{frame['digest'][:16]}",
        "snapshot": {
            "snapshot_id": "snap-phase-1",
            "upstream": "upstream-reader-1",
            "version": 1,
            "frame_digest": frame["digest"],
            "confirmation_token": f"read-token-{frame['digest'][:16]}",
        },
    }


def _write_sealed_frame(node_dir: Path, slot: int, frame: dict[str, Any], encoded: bytes) -> None:
    log_path, seal_path = _slot_paths(node_dir, slot)
    write_bytes_durable(log_path, encoded, "wb")
    seal = {
        "version": frame["version"],
        "frame_digest": frame["digest"],
        "length": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "complete": True,
    }
    write_json_durable(seal_path, seal)


def run_phases(
    root: str | Path,
    crash_at: int | None = None,
    ready_file: str | Path | None = None,
    poll_seconds: float = 0.02,
) -> dict[str, Any]:
    """Run the six-phase durable protocol.

    When ``crash_at`` is one of 1..6, the function writes a ready descriptor
    for that phase and then waits to be killed.  It never exits normally at a
    crash phase.
    """

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    case = build_phase_case(root)
    frame = case["frame"]
    encoded = bytes.fromhex(case["encoded_hex"])

    def mark(phase: int) -> None:
        descriptor = {
            "phase": phase,
            "phase_name": PHASE_NAMES[phase],
            "root": str(root),
            "pid": os.getpid(),
            "ready": True,
        }
        write_json_durable(root / f"phase-{phase}.ready.json", descriptor)
        # The --ready-file is the crash barrier for the requested phase.  Do
        # not publish earlier heartbeats there, otherwise a parent can observe
        # phase 1 in the file intended to announce phase 2.
        if ready_file is not None and crash_at == phase:
            write_json_durable(Path(ready_file), descriptor)
        if crash_at == phase:
            # The parent kills this real subprocess after observing the ready
            # file.  The wait avoids a race where the child exits before kill.
            while True:
                time.sleep(poll_seconds)

    # Phase 1: complete owner token is durable.
    write_json_durable(root / "owner-token.json", case["token"])
    mark(1)

    # Phase 2: a explicitly partial frame prefix is fsynced.
    n1 = root / "nodes" / "n1"
    n2 = root / "nodes" / "n2"
    n1.mkdir(parents=True, exist_ok=True)
    n2.mkdir(parents=True, exist_ok=True)
    log0, _ = _slot_paths(n1, 0)
    write_bytes_durable(log0, encoded[: case["partial_bytes"]], "wb")
    mark(2)

    # Phase 3: finish n1 frame and make its seal durable.
    write_bytes_durable(log0, encoded[case["partial_bytes"] :], "ab")
    seal0 = {
        "version": frame["version"],
        "frame_digest": frame["digest"],
        "length": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "complete": True,
    }
    write_json_durable(n1 / "slot-0.seal.json", seal0)
    mark(3)

    # Phase 4: same frame is sealed on a second distinct physical node.
    _write_sealed_frame(n2, 1, frame, encoded)
    mark(4)

    # Phase 5: commit marker is durably published.
    write_json_durable(root / "commit-marker.json", case["commit_marker"])
    for node_id in ("n1", "n2"):
        marker_dir = root / "nodes" / node_id / "commit-markers"
        write_json_durable(marker_dir / "cm-phase-1.json", case["commit_marker"])
    mark(5)

    # Phase 6: reader possession confirmation token is durable.
    write_json_durable(root / "read-token.json", {"confirmation_token": case["read_token"]})
    write_json_durable(root / "snapshot.json", case["snapshot"])
    mark(6)
    return {"status": "completed", "phases": PHASE_COUNT, "root": str(root)}


def recover_node_dir(node_dir: str | Path) -> dict[str, Any]:
    node_dir = Path(node_dir)
    result: dict[str, Any] = {
        "node": node_dir.name,
        "exists": node_dir.exists(),
        "classification": "clean",
        "slots": {},
        "last_complete_owner_epoch": None,
    }
    if not node_dir.exists():
        return result

    classifications: list[str] = []
    for slot in (0, 1):
        log_path, seal_path = _slot_paths(node_dir, slot)
        slot_result: dict[str, Any] = {
            "slot": slot,
            "log_exists": log_path.exists(),
            "seal_exists": seal_path.exists(),
        }
        if not log_path.exists() or log_path.stat().st_size == 0:
            slot_result["classification"] = "empty"
            result["slots"][f"s{slot}"] = slot_result
            continue

        raw = log_path.read_bytes()
        slot_result["actual_bytes"] = len(raw)
        seal = None
        if seal_path.exists():
            try:
                seal = json.loads(seal_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                slot_result.update(classification="committed_log_corruption", error=str(exc))
                classifications.append("committed_log_corruption")
                result["slots"][f"s{slot}"] = slot_result
                continue

        if seal is None:
            slot_result["classification"] = "last_frame_incomplete"
            classifications.append("last_frame_incomplete")
        else:
            digest_ok = hashlib.sha256(raw).hexdigest() == seal.get("sha256")
            length_ok = len(raw) == int(seal.get("length", -1))
            if not seal.get("complete") or not digest_ok or not length_ok:
                slot_result["classification"] = "checksum_mismatch"
                classifications.append("checksum_mismatch")
            else:
                try:
                    frame = json.loads(raw.decode("utf-8"))
                    expected = sha256_hex(
                        {
                            "version": frame["version"],
                            "proposal": frame["proposal"],
                            "participants": frame["participants"],
                            "prev_digest": frame["prev_digest"],
                        }
                    )
                    if expected != frame.get("digest"):
                        raise ValueError("frame digest mismatch")
                    slot_result["classification"] = "complete"
                    slot_result["frame_digest"] = frame.get("digest")
                except (UnicodeDecodeError, json.JSONDecodeError, KeyError, ValueError) as exc:
                    slot_result.update(classification="corrupt_complete_frame", error=str(exc))
                    classifications.append("corrupt_complete_frame")
        result["slots"][f"s{slot}"] = slot_result

    marker_dir = node_dir / "commit-markers"
    markers = []
    if marker_dir.exists():
        for marker_path in sorted(marker_dir.glob("*.json")):
            try:
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
                markers.append(marker)
            except json.JSONDecodeError:
                classifications.append("committed_log_corruption")
    result["commit_markers"] = markers

    frame_digests = {
        s.get("frame_digest")
        for s in result["slots"].values()
        if s.get("classification") == "complete"
    }
    for marker in markers:
        if marker.get("frame_digest") not in frame_digests:
            classifications.insert(0, "committed_log_corruption")

    if "committed_log_corruption" in classifications:
        result["classification"] = "committed_log_corruption"
    elif "checksum_mismatch" in classifications:
        # A bad seal for a marker is committed corruption; otherwise it is an
        # uncommitted corrupt tail and safe to rebuild from quorum.
        if markers:
            result["classification"] = "committed_log_corruption"
        else:
            result["classification"] = "uncommitted_frame_corruption"
    elif "corrupt_complete_frame" in classifications:
        result["classification"] = (
            "committed_log_corruption" if markers else "uncommitted_frame_corruption"
        )
    elif "last_frame_incomplete" in classifications:
        result["classification"] = "last_frame_incomplete"
    else:
        result["classification"] = "clean"
    return result


def recover_root(root: str | Path) -> dict[str, Any]:
    root = Path(root)
    nodes = {}
    nodes_dir = root / "nodes"
    if nodes_dir.exists():
        for node_dir in sorted(p for p in nodes_dir.iterdir() if p.is_dir()):
            nodes[node_dir.name] = recover_node_dir(node_dir)

    owner_path = root / "owner-token.json"
    owner = json.loads(owner_path.read_text(encoding="utf-8")) if owner_path.exists() else None

    marker_path = root / "commit-marker.json"
    marker = None
    if marker_path.exists():
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            marker = {"corrupt": True}

    token_path = root / "read-token.json"
    read_token = None
    if token_path.exists():
        try:
            read_token = json.loads(token_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            read_token = {"corrupt": True}

    committed = bool(isinstance(marker, dict) and marker.get("marker_id") and not marker.get("corrupt"))
    corruption_nodes = [
        node_id
        for node_id, report in nodes.items()
        if report.get("classification") == "committed_log_corruption"
    ]
    incomplete_nodes = [
        node_id
        for node_id, report in nodes.items()
        if report.get("classification") == "last_frame_incomplete"
    ]
    complete_digest_nodes: dict[str, list[str]] = {}
    for node_id, report in nodes.items():
        for slot in report.get("slots", {}).values():
            digest = slot.get("frame_digest")
            if slot.get("classification") == "complete" and digest:
                complete_digest_nodes.setdefault(digest, [])
                if node_id not in complete_digest_nodes[digest]:
                    complete_digest_nodes[digest].append(node_id)
    quorum_digests = sorted(
        digest for digest, node_ids in complete_digest_nodes.items() if len(node_ids) >= 2
    )
    quorum_durable = bool(quorum_digests) and not committed

    visible_to_token_holder = bool(committed and read_token and not corruption_nodes)
    return {
        "schema": "crashfence.durable-recovery.v1",
        "root": str(root),
        "owner_token_durable": owner is not None,
        "commit_marker_durable": committed,
        "read_token_durable": read_token is not None,
        "visible_to_token_holder": visible_to_token_holder,
        "committed": committed,
        "corruption_nodes": corruption_nodes,
        "incomplete_nodes": incomplete_nodes,
        "complete_digest_nodes": complete_digest_nodes,
        "quorum_frame_digests": quorum_digests,
        "quorum_durable_without_marker": quorum_durable,
        "nodes": nodes,
        "classification": (
            "committed_log_corruption"
            if corruption_nodes
            else "committed_waiting_for_read_token"
            if committed and read_token is None
            else "readable"
            if visible_to_token_holder
            else "quorum_durable_waiting_for_commit_marker"
            if quorum_durable
            else "last_frame_incomplete"
            if incomplete_nodes
            else "single_node_durable_waiting_for_quorum"
            if complete_digest_nodes
            else "uncommitted_or_incomplete"
        ),
    }
