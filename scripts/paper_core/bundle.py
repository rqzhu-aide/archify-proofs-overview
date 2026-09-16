"""Bundle manifest helpers shared by the release builder and the installed packages.

A generated ``scripts/paper_core/`` bundle carries ``bundle-manifest.json`` (implementation-handoff 8):
core version, storage formats readable/writable, contract versions, required feature names, projection
version, the source content identity, and the hash of every bundled file. ``verify_bundle`` lets an
installed package prove it is intact without any repository access. Caches and byte-compiled files are
never part of the identity.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import (CONTRACT_NAME, CONTRACT_VERSION, CORE_VERSION, LEGACY_OVERVIEW_FORMAT, PACKET_VERSION,
               PROJECTION_VERSION, PROTOCOL_VERSION, STORAGE_FORMATS_READABLE, STORAGE_FORMATS_WRITABLE,
               SUPPORTED_FEATURES)

MANIFEST_NAME = "bundle-manifest.json"
BUNDLE_NAME = "paper_core"
CACHE_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git"}
CACHE_SUFFIXES = {".pyc", ".pyo"}
PACKAGE_DIR = Path(__file__).resolve().parent


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bundle_files(root: Path) -> dict[str, str]:
    """``{posix relative path: sha256}`` for every shippable file under ``root`` (manifest excluded)."""
    root = Path(root)
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if CACHE_DIRS.intersection(relative.parts) or path.suffix in CACHE_SUFFIXES:
            continue
        if path.is_symlink():
            raise ValueError(f"bundle links are not supported: {path}")
        if path.is_file() and relative.as_posix() != MANIFEST_NAME:
            result[relative.as_posix()] = sha256_file(path)
    return result


def content_identity(files: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(files):
        digest.update(f"{relative}\0{files[relative]}\n".encode("utf-8"))
    return digest.hexdigest()


def build_manifest(files: dict[str, str]) -> dict:
    """Deterministic manifest (no timestamps) so two bundles of the same source are byte-identical."""
    return {
        "bundle": BUNDLE_NAME,
        "core_version": CORE_VERSION,
        "storage_formats_readable": list(STORAGE_FORMATS_READABLE),
        "storage_formats_writable": list(STORAGE_FORMATS_WRITABLE),
        "contract_version": CONTRACT_VERSION,
        "contract": CONTRACT_NAME,
        "packet_version": PACKET_VERSION,
        "projection_version": PROJECTION_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "supported_features": list(SUPPORTED_FEATURES),
        "legacy_overview_format": LEGACY_OVERVIEW_FORMAT,
        "source_identity": content_identity(files),
        "file_count": len(files),
        "files": dict(sorted(files.items())),
    }


def manifest_text(manifest: dict) -> str:
    return json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def load_manifest(root: Path | None = None) -> dict | None:
    path = Path(root or PACKAGE_DIR) / MANIFEST_NAME
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def verify_bundle(root: Path | None = None) -> dict:
    """Compare the files under ``root`` with its manifest.

    Returns ``{present, ok, source_identity, file_count, missing, unexpected, changed, constants_match}``.
    A bundle without a manifest (the repository source tree) reports ``present: False`` and ``ok: None``.
    """
    root = Path(root or PACKAGE_DIR)
    manifest = load_manifest(root)
    if manifest is None:
        return {"present": False, "ok": None, "source_identity": None, "file_count": None,
                "missing": [], "unexpected": [], "changed": [], "constants_match": None}
    actual = bundle_files(root)
    expected = manifest.get("files", {})
    missing = sorted(set(expected) - set(actual))
    unexpected = sorted(set(actual) - set(expected))
    changed = sorted(name for name in set(expected) & set(actual) if expected[name] != actual[name])
    fresh = build_manifest(actual)
    constants_match = all(manifest.get(key) == fresh[key] for key in fresh if key not in {"files", "file_count", "source_identity"})
    identity_ok = manifest.get("source_identity") == content_identity(expected) == fresh["source_identity"]
    return {
        "present": True,
        "ok": not missing and not unexpected and not changed and constants_match and identity_ok,
        "source_identity": manifest.get("source_identity"),
        "file_count": manifest.get("file_count"),
        "missing": missing,
        "unexpected": unexpected,
        "changed": changed,
        "constants_match": constants_match,
    }


__all__ = ["BUNDLE_NAME", "MANIFEST_NAME", "build_manifest", "bundle_files", "content_identity",
           "load_manifest", "manifest_text", "sha256_file", "verify_bundle"]
