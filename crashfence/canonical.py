"""Canonical JSON and digest helpers."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_bytes(value: Any) -> bytes:
    """Return deterministic UTF-8 bytes for *value*.

    Keys are sorted and insignificant whitespace is omitted.  ``ensure_ascii``
    is false so Chinese policy text remains readable in the UTF-8 ledger and
    CLI output.
    """

    text = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return text.encode("utf-8")


def canonical_json(value: Any) -> str:
    return canonical_bytes(value).decode("utf-8")


def sha256_hex(value: Any | bytes) -> str:
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    else:
        raw = canonical_bytes(value)
    return hashlib.sha256(raw).hexdigest()


def short_digest(digest: str | None, length: int = 12) -> str | None:
    if digest is None:
        return None
    return digest[:length]
