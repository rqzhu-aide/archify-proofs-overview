"""Canonical JSON and digests shared by idempotency, bindings, and guards."""
from __future__ import annotations

import hashlib
import json


def canonical_bytes(value) -> bytes:
    """UTF-8, sorted keys, compact separators, no NaN/Infinity, array order kept."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(value) -> str:
    return sha256_bytes(canonical_bytes(value))


def compact_json(value) -> str:
    """Compact JSON without key sorting; used for generated identity hashes."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def load_json_bytes(data: bytes):
    """Parse JSON rejecting duplicate keys and NaN literals."""
    def pairs(items):
        seen = set()
        for key, _ in items:
            if key in seen:
                raise ValueError(f"duplicate object key {key!r}")
            seen.add(key)
        return dict(items)

    def constant(name):
        raise ValueError(f"non-finite number {name} is not allowed")
    return json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
