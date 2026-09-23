"""Stable identifiers: prefixed UUIDv4 hex, never printed theorem numbers."""
from __future__ import annotations

import re
import uuid

ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
ID_MAX = 128
PREFIXES = {
    "papers": "pap", "sources": "src", "anchors": "anc", "items": "itm", "parts": "prt",
    "scopes": "scp", "arguments": "arg", "groups": "grp", "uses": "use", "coverage": "cov",
    "checks": "chk", "findings": "fnd", "source_issues": "sis", "repairs": "rep",
    "observations": "obs", "responses": "rsp", "reconciliations": "rec", "audits": "aud",
    "qualifications": "qua", "source_reviews": "srv", "reuse_decisions": "reu",
    "identity_maps": "map", "request": "req", "packet": "pkt", "publication": "pub",
    "run": "run", "event": "evt",
    "overview_selections": "sel", "target_specs": "tgt", "application_details": "app",
    "connection_refinements": "ref", "proof_boundaries": "bnd",
}
COLLECTIONS = tuple(name for name in PREFIXES if name not in {
    "request", "packet", "publication", "run", "event"})


def valid_id(value) -> bool:
    return isinstance(value, str) and len(value) <= ID_MAX and bool(ID_RE.match(value))


def new_id(kind: str) -> str:
    if kind not in PREFIXES:
        raise ValueError(f"unknown id kind {kind!r}")
    return f"{PREFIXES[kind]}_{uuid.uuid4().hex}"
