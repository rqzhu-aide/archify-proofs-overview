"""Snapshot export, legacy overview migration and legacy audit import.

Implements record-contract section 7 (export shape and adapter rules) and the
``export``, ``migrate-overview`` and ``import-legacy`` commands of
implementation-handoff section 5. Both importers write through the ordinary
acceptance engine with ``command="import"``. Native migration then activates
the converted tables in one exclusive transaction. Neither importer imports
the legacy monolith or the legacy overview scripts.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import sqlite3
import uuid
from pathlib import Path

from . import (CONTRACT_VERSION, CORE_VERSION, LEGACY_OVERVIEW_FORMAT, LEGACY_OVERVIEW_SCHEMA_VERSIONS,
               PROJECTION_VERSION, PROTOCOL_VERSION)
from .acceptance import accept
from .bindings import BOUND_COLLECTIONS
from .canonical import canonical_bytes, digest, sha256_bytes
from .contract import ITEM_KINDS, MAJOR_KINDS
from .errors import IncompatibleError, InvalidRequest, SourceUnavailable
from .ids import new_id, valid_id
from .sources import _decode, _extract_lines, capture_sources, media_type, resolve_anchor
from .storage import Database, Record, initialize, paper_record

LEGACY_AUDIT_PROTOCOL = "stat-paper-proofcheck/1.5-ledger"


# -- export -------------------------------------------------------------------

def envelope(record: Record) -> dict:
    return {"collection": record.collection, "id": record.id, "version": record.version,
            "revision": record.revision, "retired": record.retired,
            "body": None if record.retired else record.body}


def _blob_refs(record: Record) -> list:
    """Blob hashes a live record body refers to."""
    body = record.body
    if body is None:
        return []
    if record.collection == "sources":
        return [body["blob_sha256"]]
    if record.collection == "responses":
        return [body["original_blob"]]
    if record.collection == "qualifications":
        return [body["evidence_blob"]] + [c["response_blob"]
                                          for c in body["valid_case_results"] + body["invalid_case_results"]]
    if record.collection == "identity_maps" and body["source_blob"] is not None:
        return [body["source_blob"]]
    return []


def _check_revision(db: Database, revision) -> int:
    top = db.max_revision()
    if revision is None:
        return top
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1 or revision > top:
        raise InvalidRequest(f"revision {revision!r} is not in 1..{top}", code="REVISION_RANGE")
    return revision


def export_snapshot(db: Database, *, revision=None, history: bool = False) -> dict:
    """Portable JSON of one revision (record-contract 7). ``history`` adds every version up to it."""
    revision = _check_revision(db, revision)
    records = db.records_at(revision, include_retired=True)
    papers = [r for r in records if r.collection == "papers" and not r.retired]
    if len(papers) != 1:
        raise IncompatibleError(f"revision {revision} holds {len(papers)} live paper records; expected one")
    bindings, hashes, missing = [], set(), []
    for record in records:
        hashes.update(_blob_refs(record))
        if record.retired or record.collection not in BOUND_COLLECTIONS:
            continue
        stored = db.binding(record.collection, record.id, record.version)
        if stored is not None:
            bindings.append({"ref": record.pinned, "packet_id": stored["packet_id"], "bindings": stored["bindings"]})
    blobs = []
    for sha in sorted(hashes):
        data = db.get_blob(sha)
        if data is None:
            missing.append(sha)
            continue
        blobs.append({"sha256": sha, "encoding": "base64", "data": base64.b64encode(data).decode("ascii")})
    sources = sorted([s.id, s.version, s.body["blob_sha256"]]
                     for s in records if s.collection == "sources" and not s.retired)
    result = {
        "contract_version": int(db.metadata["contract_version"]),
        "storage_format": int(db.metadata["storage_format"]),
        "revision": revision,
        "paper_id": papers[0].id,
        "records": [envelope(r) for r in records],
        "bindings": bindings,
        "blobs": blobs,
        "provenance": {"core_version": CORE_VERSION, "projection_version": PROJECTION_VERSION,
                       "source_identity": digest(sources)},
    }
    if missing:
        result["provenance"]["missing_blobs"] = missing
    if history:
        result["history"] = [envelope(v) for v in db.all_versions() if v.revision <= revision]
    return result


def write_export(db: Database, *, output, revision=None, history: bool = False) -> dict:
    output = Path(output)
    data = export_snapshot(db, revision=revision, history=history)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False).encode("utf-8")
    staged = output.with_name(output.name + f".tmp-{uuid.uuid4().hex}")
    staged.write_bytes(payload)
    os.replace(staged, output)
    return {"output": str(output), "revision": data["revision"], "paper_id": data["paper_id"],
            "records": len(data["records"]), "blobs": len(data["blobs"]), "history": len(data.get("history", [])),
            "sha256": sha256_bytes(payload), "missing_blobs": data["provenance"].get("missing_blobs", [])}


# -- shared import helpers ------------------------------------------------------

def _exclusive_create(path: Path, what: str) -> None:
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise InvalidRequest(f"refusing to overwrite existing {what} {path}") from None
    os.close(fd)


def _slug(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-")
    if not slug or not slug[0].isalpha():
        slug = "x" + slug
    return slug[:100]


class _IdMapper:
    """Preserve valid legacy IDs; remap the rest and keep every decision for the identity map."""

    def __init__(self):
        self.entries: list = []
        self.taken: set = set()
        self.remapped: list = []

    def assign(self, collection: str, old: str, *, prefer=None, rationale: str) -> str:
        candidate = prefer if prefer is not None else old
        if not valid_id(candidate) or (collection, candidate) in self.taken:
            candidate = new_id(collection)
            self.remapped.append({"collection": collection, "old": old, "new": candidate})
        self.taken.add((collection, candidate))
        self.entries.append({"old": f"{collection}:{old}", "new_refs": [{"collection": collection, "id": candidate}],
                             "rationale": rationale})
        return candidate

    def note(self, old: str, refs: list, rationale: str) -> None:
        self.entries.append({"old": old, "new_refs": refs, "rationale": rationale})


def _create(collection: str, id: str, body: dict) -> dict:
    return {"op": "create", "collection": collection, "id": id, "expected_version": None, "body": body}


def _media_from_legacy(path: str, legacy_media: str) -> str:
    kind = media_type(path)
    if kind != "other":
        return kind
    if legacy_media == "application/pdf":
        return "pdf"
    if legacy_media.startswith("text/"):
        return "text"
    return "other"


def _overview_target_digest(payload: dict, collection: str, identity: str) -> str:
    """Mirror native v3 target_digest without importing the overview runtime.

    Comparison identity includes the row's owned intermediates and their incoming uses.
    An observation is current only for the context that the overview actually compared.
    """
    def digest_row(row: dict) -> dict:
        return {key: value for key, value in row.items()
                if not (value is None and key in ("owner", "group", "issue"))}

    records = {row["id"]: row for row in payload[collection]}
    target = records[identity]
    items = {row["id"]: row for row in payload["items"]}
    if collection == "items":
        owned = [row for row in payload["items"] if row.get("owner") == identity]
        scope = {identity} | {row["id"] for row in owned}
        uses = [u for u in payload["uses"] if u["to"] in scope]
        relevant_items = [target] + owned + [items[u["from"]] for u in uses]
    else:
        uses, relevant_items = [target], [items[target["from"]], items[target["to"]]]
    anchors = {p["anchor_id"] for i in relevant_items for p in i["passages"]}
    anchors.update(a for u in uses for a in u["evidence_refs"])
    context = {"target": digest_row(target), "source_revision": payload["source_revision"]["id"],
               "uses": [digest_row(u) for u in uses], "items": [digest_row(i) for i in relevant_items],
               "anchors": [a for a in payload["anchors"] if a["id"] in anchors]}
    return digest(context)


def _applicable_observations(legacy: dict) -> set:
    """Ids of the legacy observations the overview itself would treat as current.

    The overview's rule (paper_records.applicable_observations with fidelity_by_row): per item or
    use row, the newest observation whose input_snapshot equals the row's current target digest.
    A row whose observations all review older content is stale, and on an unchanged input a newer
    needs_attention supersedes an older matched. Only applicable observations migrate as live
    records; the rest remain available in the archived legacy export only.
    """
    payload = legacy["payload"]
    history = {}
    for observation in legacy["observations"]:
        target = observation["target"]
        history.setdefault((target.get("collection"), target.get("id")), []).append(observation)
    applicable = set()
    for collection in ("items", "uses"):
        for row in payload[collection]:
            candidates = history.get((collection, row["id"]), [])
            if not candidates:
                continue
            identity = _overview_target_digest(payload, collection, row["id"])
            newest = next((o for o in reversed(candidates) if o.get("input_snapshot") == identity), None)
            if newest is not None:
                applicable.add(newest["id"])
    return applicable


def _unresolved_issue_source(entry: str, payload: dict, file_sources: dict) -> str:
    """The imported source an inventory note belongs to: its ``path: message`` prefix, else the first file."""
    prefix = entry.split(":", 1)[0]
    files = payload["source_revision"]["files"]
    for f in files:
        if f["path"] == prefix:
            return file_sources[f["id"]]
    # The note still names its own path in the description text; attach it to the first captured
    # file so the limitation stays queryable instead of vanishing into the archived payload.
    return file_sources[files[0]["id"]]


def _finish_import(db: Database, *, request_id: str, request_digest: str, edits: list, blobs: list,
                   warnings: list) -> dict:
    return accept(db, request_id=request_id, request_digest=request_digest, packet_id=None, edits=edits,
                  command="import", blobs=blobs, warnings=warnings)


# -- legacy overview migration ---------------------------------------------------

def _read_legacy_overview(path: Path) -> dict:
    """Read the current snapshot of an archify-paper-database-1 file without importing its scripts."""
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    except sqlite3.Error as exc:
        raise IncompatibleError(f"cannot open {path}: {exc}") from exc
    try:
        conn.row_factory = sqlite3.Row
        try:
            meta = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM metadata")}
        except sqlite3.DatabaseError as exc:
            raise IncompatibleError(f"{path} is not a paper database (no metadata table): {exc}") from exc
        if meta.get("storage_format") is not None:
            raise InvalidRequest(f"{path} is already storage format {meta['storage_format']}; nothing to migrate",
                                 code="ALREADY_MIGRATED")
        if meta.get("format") != LEGACY_OVERVIEW_FORMAT:
            raise IncompatibleError(f"{path} has format {meta.get('format')!r}; migrate-overview accepts "
                                    f"{LEGACY_OVERVIEW_FORMAT} only", records=[{"format": meta.get("format")}])
        current = conn.execute("SELECT snapshot_id FROM current_snapshot WHERE singleton = 1").fetchone()
        if current is None:
            raise IncompatibleError(f"{path} has no current snapshot to migrate")
        snapshot_id = current["snapshot_id"]
        row = conn.execute("SELECT payload, created_at FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone()
        if row is None:
            raise IncompatibleError(f"{path}: current snapshot {snapshot_id} is missing from snapshots")
        payload = json.loads(row["payload"])
        if type(payload.get("schema_version")) is not int or payload["schema_version"] not in LEGACY_OVERVIEW_SCHEMA_VERSIONS:
            raise IncompatibleError(f"{path}: migrate-overview accepts native schema-3 overview records only; "
                                    "start a new v3 overview from the manuscript for older versions")
        blobs = {r["sha256"]: base64.b64decode(r["content_base64"])
                 for r in conn.execute("SELECT sha256, content_base64 FROM source_blobs")}
        # Comparison history is append-only across snapshots; the birth snapshot
        # of an observation is provenance, not a filter (paper_database.py reads
        # the full table the same way).
        observations = [json.loads(r["payload"]) for r in conn.execute(
            "SELECT payload FROM observations ORDER BY rowid")]
        snapshot_ids = [r["id"] for r in conn.execute("SELECT id FROM snapshots ORDER BY created_at, rowid")]
        builds = conn.execute("SELECT COUNT(*) FROM builds").fetchone()[0]
        return {"metadata": meta, "snapshot_id": snapshot_id, "created_at": row["created_at"], "payload": payload,
                "blobs": blobs, "observations": observations, "snapshot_ids": snapshot_ids, "builds": builds}
    finally:
        conn.close()


def _backup_legacy(path: Path, backup: Path, expected_snapshot: str) -> dict:
    _exclusive_create(backup, "backup file")
    try:
        source = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        try:
            target = sqlite3.connect(str(backup))
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        check = sqlite3.connect(backup.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            row = check.execute("SELECT snapshot_id FROM current_snapshot WHERE singleton = 1").fetchone()
        finally:
            check.close()
        if row is None or row[0] != expected_snapshot:
            raise IncompatibleError("backup verification failed: current snapshot differs from the original")
    except Exception:
        backup.unlink(missing_ok=True)
        raise
    return {"path": str(backup), "sha256": sha256_bytes(backup.read_bytes())}


def _overview_edits(paper: Record, legacy: dict, mapper: _IdMapper, limitations: list) -> tuple:
    payload = legacy["payload"]
    edits, blobs = [], []
    anchor_verify = {}
    file_sources = {}
    file_media = {}
    for f in payload["source_revision"]["files"]:
        data = legacy["blobs"].get(f["sha256"])
        if data is None:
            raise IncompatibleError(f"legacy source blob {f['sha256']} for {f['path']} is missing")
        if sha256_bytes(data) != f["sha256"]:
            raise IncompatibleError(f"legacy source blob for {f['path']} does not match its recorded sha256")
        source_id = mapper.assign("sources", f["id"], prefer=None, rationale="legacy overview source file")
        blobs.append(data)
        media = _media_from_legacy(f["path"], f.get("media_type", ""))
        edits.append(_create("sources", source_id, {
            "paper_id": paper.id, "path": f["path"], "media_type": media,
            "blob_sha256": f["sha256"], "capture_method": "legacy_overview_import", "limitation": None}))
        file_sources[f["id"]] = source_id
        file_media[f["id"]] = media
        anchor_verify[f["id"]] = _decode(data)
    anchor_ids = {}
    for a in payload["anchors"]:
        file_id = a.get("file_id")
        source_id = file_sources.get(file_id)
        if source_id is None:
            raise IncompatibleError(f"legacy anchor {a['id']} has no registered source file ({file_id!r}); "
                                    "a contract-3 anchor requires one")
        loc = a["locator"]
        start, end = loc.get("start_line"), loc.get("end_line")
        page, label = loc.get("page"), loc.get("label")
        if (start is None) != (end is None):
            raise IncompatibleError(f"legacy anchor {a['id']} carries only one of start_line/end_line")
        if start is None and page is None and label is None:
            raise IncompatibleError(f"legacy anchor {a['id']} records no line range, page or label")
        if page is not None and file_media[file_id] != "pdf":
            if start is None and label is None:
                raise IncompatibleError(f"legacy anchor {a['id']} reviews page {page} of {file_id}, which is not a "
                                        "captured PDF; re-anchor it in the overview before migrating")
            # Supplemental compiled-page navigation survives in the overview
            # selection; canonical evidence remains bound to captured text.
            page = None
        method = "exact_lines" if start is not None else "label_match" if label is not None else "reviewed_page"
        excerpt = a["excerpt"]
        limitation = None
        text = anchor_verify.get(file_id)
        if a.get("verification", {}).get("status") != "checked":
            limitation = a.get("verification", {}).get("note") or f"legacy verification status {a.get('verification', {}).get('status')!r}"
        elif start is not None and text is not None and _extract_lines(text, start, end) != excerpt:
            limitation = "legacy excerpt differs from the captured source lines; re-anchor before reuse"
        if a.get("excerpt_hash") != sha256_bytes(excerpt.encode("utf-8")):
            limitations.append(f"anchor {a['id']}: legacy excerpt_hash did not match the excerpt; recomputed")
        if limitation:
            limitations.append(f"anchor {a['id']}: {limitation}")
        new_id_ = mapper.assign("anchors", a["id"], rationale="legacy overview anchor")
        anchor_ids[a["id"]] = new_id_
        edits.append(_create("anchors", new_id_, {
            "source_id": source_id, "source_version": 1,
            "locator": {"start_line": start, "end_line": end, "page": page, "label": label},
            "excerpt": excerpt, "excerpt_sha256": sha256_bytes(excerpt.encode("utf-8")), "method": method,
            "limitation": limitation}))
    item_ids = {}
    passages_of = {it["id"]: it.get("passages", []) for it in payload["items"]}
    for it in payload["items"]:
        item_ids[it["id"]] = mapper.assign("items", it["id"], rationale="legacy overview item")
    for it in payload["items"]:
        kind = it["kind"]
        uncertainty = it.get("issue")
        if kind not in ITEM_KINDS:
            raise IncompatibleError(f"overview item {it['id']} has unsupported kind {kind!r}; "
                                    "correct the item before handoff")
        owner = it.get("owner")
        if owner is not None and owner not in item_ids:
            raise IncompatibleError(f"legacy item {it['id']} names unknown owner {owner!r}")
        passages = []
        for p in it.get("passages", []):
            if p["anchor_id"] not in anchor_ids:
                raise IncompatibleError(f"legacy item {it['id']} names unknown anchor {p['anchor_id']}")
            passages.append({"role": p["role"], "anchor_id": anchor_ids[p["anchor_id"]]})
        edits.append(_create("items", item_ids[it["id"]], {
            "kind": kind, "label": it["label"], "caption": it.get("caption") or "", "statement": it["statement"],
            "passages": passages, "aliases": list(it.get("aliases") or []), "uncertainty": uncertainty or None,
            "origin": "source", "owner_id": item_ids.get(owner), "scope_id": None,
            **({"proof_idea": it["proof_idea"]} if "proof_idea" in it else {})}))
    use_ids = {}
    for u in payload["uses"]:
        for end in ("from", "to"):
            if u[end] not in item_ids:
                raise IncompatibleError(f"legacy use {u['id']} names unknown item {u[end]}")
        use_ids[u["id"]] = mapper.assign("uses", u["id"], rationale="legacy overview use")
    # An overview group has no argument or scope behind it; it migrates as a
    # provenance group (both null) whose conclusion is the member uses' shared
    # target. A group id spanning several conclusions yields one record each.
    group_ids = {}
    for u in payload["uses"]:
        group = u.get("group")
        if group is None:
            continue
        key = (group["id"], u["to"])
        if key in group_ids:
            continue
        if any(old == group["id"] for old, _ in group_ids):
            limitations.append(f"use {u['id']}: overview group {group['id']!r} spans several conclusions; "
                               "it migrates as one groups record per conclusion")
        group_id = mapper.assign("groups", group["id"], rationale="legacy overview use group")
        group_ids[key] = group_id
        edits.append(_create("groups", group_id, {
            "argument_id": None, "conclusion": {"collection": "items", "id": item_ids[u["to"]]},
            "kind": group["kind"], "scope_id": None, "case_scope_ids": [], "discharges": [],
            "rationale": f"Overview {group['kind']} group {group['id']!r} imported from {LEGACY_OVERVIEW_FORMAT}; "
                         "an audit has not assigned it to an argument.",
            "evidence_refs": []}))
    for u in payload["uses"]:
        evidence_refs = []
        for ref in u.get("evidence_refs", []):
            if ref in anchor_ids:
                evidence_refs.append(anchor_ids[ref])
            else:
                limitations.append(f"use {u['id']}: evidence anchor {ref} is not an imported anchor; dropped")
        group = u.get("group")
        edits.append(_create("uses", use_ids[u["id"]], {
            "from": {"collection": "items", "id": item_ids[u["from"]]},
            "to": {"collection": "items", "id": item_ids[u["to"]]}, "type": u["type"],
            "reason": u.get("reason") or "Legacy overview use; no reason was recorded.", "evidence_refs": evidence_refs,
            "regime": u.get("regime") or None, "uncertainty": u.get("issue") or None}))
        if group:
            edits.append(_create("application_details", use_ids[u["id"]], {
                "use_id": use_ids[u["id"]], "group_id": group_ids[group["id"], u["to"]],
                "needed_form": None, "substitutions": [], "scope_id": None, "state": "draft"}))
    inventory = payload.get("inventory") or {}
    for entry in inventory.get("unresolved", []):
        # Each legacy unresolved-inventory entry becomes an open source issue, so the coverage
        # restriction stays queryable after migration. Entries that report an unavailable input
        # or source file are missing sources; the rest record another resolution limit.
        category = "missing_source" if ("input" in entry or "source" in entry) else "other_resolution"
        edits.append(_create("source_issues", new_id("source_issues"), {
            "source_id": _unresolved_issue_source(entry, payload, file_sources), "anchor_id": None,
            "category": category, "description": entry, "lifecycle": "open", "resolution": None,
            "reviewer": "migrate-overview"}))
    from .overview import SELECTION_ID, selection_body
    selected = selection_body(paper.id, dict(payload, observations=legacy["observations"]),
                              profile=legacy["metadata"].get("authoring_profile", "compatibility"))
    # Native identifiers normally already satisfy the common contract. Preserve
    # an explicit map where a historical identifier had to change.
    selected["item_ids"] = [item_ids[i] for i in selected["item_ids"]]
    selected["use_ids"] = [use_ids[i] for i in selected["use_ids"]]
    selected["main_item_ids"] = [item_ids[i] for i in selected["main_item_ids"]]
    selected["source_ids"] = [file_sources[i] for i in selected["source_ids"]]
    selected["native_context"]["anchor_ids"] = [anchor_ids[i] for i in selected["native_context"]["anchor_ids"]]
    for name, mapping in (("items", item_ids), ("uses", use_ids)):
        fields = selected["native_context"]["optional_fields"][name]
        selected["native_context"]["optional_fields"][name] = {mapping[k]: v for k, v in fields.items()}
    selected["native_context"]["anchor_metadata"] = {anchor_ids[k]: v for k, v in selected["native_context"]["anchor_metadata"].items()}
    selected["native_context"]["groups"] = {use_ids[k]: v for k, v in selected["native_context"]["groups"].items()}
    edits.append(_create("overview_selections", SELECTION_ID, selected))
    noted_observation_fields = False
    applicable = _applicable_observations(legacy)
    archived_observations = 0
    for observation_order, o in enumerate(legacy["observations"]):
        target = o["target"]
        if target.get("collection") == "items" and target.get("id") in item_ids:
            new_target = {"collection": "items", "id": item_ids[target["id"]]}
        elif target.get("collection") == "uses" and target.get("id") in use_ids:
            new_target = {"collection": "uses", "id": use_ids[target["id"]]}
        else:
            limitations.append(f"observation {o['id']}: target {target} is not an imported item or use; skipped")
            continue
        if o["id"] not in applicable:
            archived_observations += 1
        result = o["result"] if o["result"] in ("matched", "needs_attention") else "needs_attention"
        note = o.get("note") or ""
        if result != o["result"]:
            note = f"[legacy result {o['result']!r}] {note}".strip()
        if result == "matched" and target["collection"] == "items" and not passages_of.get(target["id"]):
            result = "needs_attention"
            note = f"[legacy result 'matched' on an item without passages] {note}".strip()
            limitations.append(f"observation {o['id']}: matched result on a passage-less item recorded as needs_attention")
        provenance = " ".join(f"[legacy {field} {o[field]}]" for field in ("created_at", "input_snapshot", "carried_from")
                              if o.get(field) is not None)
        if provenance:
            note = f"{provenance} {note}".strip()
            noted_observation_fields = True
        evidence_refs = []
        for ref in o.get("evidence_refs", []):
            if ref in anchor_ids:
                evidence_refs.append(anchor_ids[ref])
            else:
                limitations.append(f"observation {o['id']}: evidence anchor {ref} is not an imported anchor; dropped")
        obs_id = mapper.assign("observations", o["id"], rationale="legacy overview observation")
        native_observation = dict(o, target=new_target, id=obs_id)
        obs_body = {"target": new_target, "result": result,
                    "reviewer": o.get("reviewer") or "legacy-overview", "note": note,
                    "evidence_refs": evidence_refs, "context_kind": "overview",
                    "context_data": {"selection_id": SELECTION_ID, "input_snapshot": o["input_snapshot"],
                                     "native_observation": native_observation,
                                     "observation_order": observation_order,
                                     "applicable_on_import": o["id"] in applicable}}
        if isinstance(o.get("created_at"), str) and o["created_at"]:
            obs_body["created_at"] = o["created_at"]
        edits.append(_create("observations", obs_id, obs_body))
    if archived_observations:
        limitations.append(f"{archived_observations} historical observations preserved with their original "
                           "comparison inputs; they do not acquire current comparison credit")
    main_items = []
    kinds = {it["id"]: it["kind"] for it in payload["items"]}
    for old in payload.get("main_items", []):
        if old in item_ids and kinds.get(old) in MAJOR_KINDS:
            main_items.append(item_ids[old])
        else:
            limitations.append(f"main item {old} is not an imported major result; dropped from main_items")
    body = dict(paper.body)
    body["main_items"] = main_items
    body["scope"] = payload.get("scope")
    body["exclusions"] = list(inventory.get("excluded", []))
    edits.append({"op": "replace", "collection": "papers", "id": paper.id, "expected_version": paper.version,
                  "body": body})
    return edits, blobs


def migrate_overview(db_path, *, backup) -> dict:
    """Offline conversion with SQLite locking and transactional in-place cutover.

    A reserved writer lock precedes the backup and freezes observation appends,
    including appends that do not change the native snapshot id. Schema replacement
    upgrades that lock to exclusive. No file is replaced under an open connection.
    """
    db_path, backup = Path(db_path), Path(backup)
    if not db_path.is_file():
        raise InvalidRequest(f"database not found: {db_path}")
    temp = db_path.with_name(f"{db_path.name}.migrating-{uuid.uuid4().hex}")
    receipt = None
    lock = sqlite3.connect(str(db_path), timeout=0, isolation_level=None)
    try:
        # WAL readers retain older schemas; require their closure before the
        # offline conversion. Changing journal mode is a SQLite-coordinated
        # operation, not manual manipulation of -wal/-shm sidecars.
        if lock.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower() != "delete":
            raise IncompatibleError("Offline conversion requires all other clients to close the database", code="DATABASE_BUSY")
        lock.execute("BEGIN IMMEDIATE")
        metadata = dict(lock.execute("SELECT key,value FROM metadata"))
        if metadata.get("storage_format") is not None:
            raise InvalidRequest(f"{db_path} is already storage format {metadata['storage_format']}; nothing to migrate", code="ALREADY_MIGRATED")
        if metadata.get("format") != LEGACY_OVERVIEW_FORMAT:
            raise IncompatibleError(f"Unsupported overview format {metadata.get('format')!r}")
        current = lock.execute("SELECT snapshot_id FROM current_snapshot WHERE singleton=1").fetchone()
        if current is None:
            raise IncompatibleError("Native overview has no current snapshot")
        native_payload = lock.execute("SELECT payload FROM snapshots WHERE id=?", (current[0],)).fetchone()
        schema_version = json.loads(native_payload[0]).get("schema_version") if native_payload else None
        if type(schema_version) is not int or schema_version not in LEGACY_OVERVIEW_SCHEMA_VERSIONS:
            raise IncompatibleError("migrate-overview accepts native schema-3 overview records only; start a new v3 overview from the manuscript for older versions")
        backup_info = _backup_legacy(db_path, backup, current[0])
        legacy = _read_legacy_overview(backup)
        payload = legacy["payload"]
        limitations: list = []
        source_root = legacy["metadata"].get("source_root")
        if not source_root:
            raise IncompatibleError("Native overview has no source-root provenance; supply an explicit verified root before conversion")
        root = Path(source_root)
        if not root.is_dir():
            limitations.append(f"Registered source root {source_root!r} is unavailable; captured bytes and the original root remain authoritative")
        title = payload.get("title") or db_path.stem
        init = initialize(temp, source_root=root, title=title, allow_missing_source_root=True)
        with Database(temp, write=True) as db:
            paper = paper_record(db)
            mapper = _IdMapper()
            edits, blobs = _overview_edits(paper, legacy, mapper, limitations)
            next(e for e in edits if e["collection"] == "papers")["body"]["source_root"] = root.as_posix()
            legacy_export = canonical_bytes({"format": LEGACY_OVERVIEW_FORMAT, "snapshot_id": legacy["snapshot_id"],
                                             "snapshot": payload, "observations": legacy["observations"]})
            blobs.append(legacy_export)
            map_id = new_id("identity_maps")
            edits.append(_create("identity_maps", map_id, {
                "reason": "import", "source_blob": sha256_bytes(legacy_export), "response_id": None,
                "entries": mapper.entries, "reviewer": "migrate-overview",
                "note": f"Imported legacy overview snapshot {legacy['snapshot_id']} from {db_path.name}; "
                        f"{legacy['builds']} legacy build rows and {len(legacy['snapshot_ids'])} snapshots remain "
                        f"in the backup only."}))
            receipt = _finish_import(
                db, request_id=new_id("request"),
                request_digest=digest({"command": "migrate-overview", "snapshot_id": legacy["snapshot_id"],
                                       "source": str(db_path)}),
                edits=edits, blobs=blobs, warnings=limitations)
            db.set_metadata("legacy_overview_format", LEGACY_OVERVIEW_FORMAT)
            db.set_metadata("legacy_overview_current_snapshot", legacy["snapshot_id"])
            db.set_metadata("legacy_overview_snapshot_ids", json.dumps(legacy["snapshot_ids"]))
            db.set_metadata("legacy_overview_backup", json.dumps(backup_info))
            counts = {c: sum(1 for e in edits if e["collection"] == c and e["op"] == "create")
                      for c in ("sources", "anchors", "items", "uses", "groups", "observations")}
            opened = sum(1 for e in edits if e["collection"] == "source_issues" and e["op"] == "create")
            if opened:
                counts["source_issues"] = opened
            remapped, paper_id = mapper.remapped, paper.id
        # Keep historical snapshots/builds in the backup. Remove every old
        # writable native table so even a preopened old writer cannot append
        # after activation. All DDL and data copying share this transaction.
        lock.execute("ATTACH DATABASE ? AS converted", (str(temp),))
        schema = lock.execute("SELECT type,name,sql FROM converted.sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 WHEN 'view' THEN 2 ELSE 3 END").fetchall()
        for name in ("observations", "builds", "current_snapshot", "snapshots", "source_blobs", "metadata"):
            lock.execute(f'DROP TABLE "{name}"')
        for kind, name, sql in schema:
            if kind == "table":
                lock.execute(sql)
                quoted = '"' + name.replace('"', '""') + '"'
                lock.execute(f"INSERT INTO main.{quoted} SELECT * FROM converted.{quoted}")
        for kind, name, sql in schema:
            if kind != "table":
                lock.execute(sql)
        if lock.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise IncompatibleError("Converted database failed foreign-key validation; original authority preserved")
        lock.execute("COMMIT")
        lock.execute("DETACH DATABASE converted")
    except sqlite3.DatabaseError as exc:
        if lock.in_transaction:
            lock.execute("ROLLBACK")
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise IncompatibleError("Database is busy. Close all overview clients and retry the explicit offline conversion; original authority preserved.", code="DATABASE_BUSY") from exc
        raise IncompatibleError(f"Cannot convert overview database {db_path}: {exc}") from exc
    except Exception:
        if lock.in_transaction:
            lock.execute("ROLLBACK")
        raise
    finally:
        lock.close()
        temp.unlink(missing_ok=True)
    return {"database": str(db_path), "backup": backup_info, "paper_id": paper_id, "revision": receipt["revision"],
            "legacy": {"format": LEGACY_OVERVIEW_FORMAT, "snapshot_id": legacy["snapshot_id"],
                       "snapshots": len(legacy["snapshot_ids"]), "builds": legacy["builds"]},
            "counts": counts, "identity_map": map_id, "remapped": remapped, "limitations": limitations,
            "init": init, "receipt": receipt}


# -- legacy audit import ----------------------------------------------------------

def _load_json(path: Path, *, required: bool):
    if not path.is_file():
        if required:
            raise InvalidRequest(f"legacy audit file missing: {path}")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        raise InvalidRequest(f"legacy audit file is not valid JSON: {path}: {exc}") from exc


def _legacy_outcome(review: dict) -> tuple[str, str]:
    status = str(review.get("unit_status") or review.get("statement_status") or "").lower()
    argument = str(review.get("argument_status") or "").lower()
    if status in ("verified", "supported") and argument in ("", "valid", "verified"):
        return "supported", status
    if status in ("incorrect", "refuted", "invalid") or argument in ("invalid", "incorrect"):
        return "refuted", status or argument
    if status in ("gap", "conditional", "incomplete", "blocked"):
        return "gap", status
    return "inconclusive", status or "unknown"


def _legacy_verdict(verdict) -> str:
    v = str(verdict or "").lower()
    if v in ("verified", "supported", "correct", "agreed_verified"):
        return "supported"
    if v in ("incorrect", "refuted", "invalid"):
        return "refuted"
    if v in ("gap", "conditional", "incomplete"):
        return "gap"
    return "inconclusive"


def _finding_category(issue: dict) -> str:
    if issue.get("invalidation_kind") == "statement_refuted":
        return "statement_refutation"
    status = issue.get("finding_status")
    if status == "defect":
        return "dependency_mismatch" if issue.get("scope") == "dependency" else "proof_gap"
    if status == "inconclusive":
        return "inconclusive"
    return "presentation"


def import_legacy(audit_folder, *, db_path, mapping_path) -> dict:
    """Create a new storage-format-2 database from a stat-paper-proofcheck v1.5 audit folder."""
    folder, db_path, mapping_path = Path(audit_folder), Path(db_path), Path(mapping_path)
    if not folder.is_dir():
        raise InvalidRequest(f"legacy audit folder not found: {folder}")
    if mapping_path.exists():
        raise InvalidRequest(f"refusing to overwrite existing mapping file {mapping_path}")
    manifest = _load_json(folder / "AUDIT_MANIFEST.json", required=False) or {}
    inventory = _load_json(folder / "audit/01_index/theorem_inventory.json", required=True)
    registry = _load_json(folder / "audit/03_dependencies/DEPENDENCY_REGISTRY.json", required=False) or {}
    issue_log = _load_json(folder / "audit/06_reports/ISSUE_LOG.json", required=False) or {}
    ledger_dir = folder / "audit/04_local_checks"
    if not ledger_dir.is_dir():
        ledger_dir = folder / "audit/02_ledgers"
    ledgers = {}
    for path in sorted(ledger_dir.glob("*.ledger.json")) if ledger_dir.is_dir() else []:
        data = _load_json(path, required=True)
        if isinstance(data, dict) and data.get("unit_id"):
            ledgers[data["unit_id"]] = (path, data)
    limitations: list = []
    project = folder / "audit/00_sources/project"
    if project.is_dir():
        source_root = project
    else:
        source_root = folder
        limitations.append("audit/00_sources/project is missing; the audit folder is the source root and "
                           "statements are imported without source anchors")
    root_file = inventory.get("root_file") or manifest.get("paper_file") or "paper"
    title = f"Legacy audit import: {Path(root_file).name} ({folder.name})"
    units = inventory.get("units") or []
    if not units:
        raise InvalidRequest("theorem_inventory.json lists no units; nothing to import")

    artifact_files = {}
    for rel in ("AUDIT_MANIFEST.json", "PROGRESS.json", "FINALIZATION.json", "audit/01_index/theorem_inventory.json",
                "audit/01_index/cross_reference_audit.json", "audit/03_dependencies/DEPENDENCY_REGISTRY.json",
                "audit/06_reports/ISSUE_LOG.json"):
        p = folder / rel
        if p.is_file():
            artifact_files[rel] = sha256_bytes(p.read_bytes())
    for path, _ in ledgers.values():
        artifact_files[path.relative_to(folder).as_posix()] = sha256_bytes(path.read_bytes())

    init = initialize(db_path, source_root=source_root, title=title)
    try:
        with Database(db_path, write=True) as db:
            paper = paper_record(db)
            mapper = _IdMapper()
            edits, blobs = [], []
            # sources
            wanted = sorted({u["statement"]["file"] for u in units if u.get("statement", {}).get("file")}
                            | {u["proof"]["file"] for u in units if u.get("proof") and u["proof"].get("file")})
            present = [f for f in wanted if (source_root / f).is_file()]
            for f in wanted:
                if f not in present:
                    limitations.append(f"source file {f} is not present under {source_root}; anchors omitted")
            source_by_path = {}
            capture = None
            if present:
                capture = capture_sources(db, files=present)
                limitations.extend(capture["limitations"])
                for entry in capture["sources"]:
                    source_by_path[entry["path"]] = entry["id"]
                    mapper.note(f"files:{entry['path']}", [{"collection": "sources", "id": entry["id"]}],
                                "legacy audit source captured from audit/00_sources/project")
            # anchors and items
            anchors_by_unit: dict = {}
            proof_anchor: dict = {}
            item_ids: dict = {}

            def anchor_for(unit_id: str, role: str, span: dict):
                source_id = source_by_path.get(span.get("file"))
                if source_id is None or not span.get("start_line"):
                    return None
                source = db.head("sources", source_id)
                locator = {"start_line": span["start_line"], "end_line": span.get("end_line") or span["start_line"],
                           "page": None, "label": None}
                try:
                    body = resolve_anchor(db, source, locator)
                except InvalidRequest as exc:
                    limitations.append(f"{unit_id} {role}: {exc}")
                    return None
                anchor_id = new_id("anchors")
                edits.append(_create("anchors", anchor_id, body))
                mapper.note(f"units:{unit_id}/{role}", [{"collection": "anchors", "id": anchor_id}],
                            f"legacy {role} span {span.get('file')}:{locator['start_line']}-{locator['end_line']}")
                return anchor_id

            for unit in units:
                uid = unit["id"]
                item_ids[uid] = mapper.assign("items", uid, prefer=_slug(uid), rationale="legacy inventory unit")
            for unit in units:
                uid = unit["id"]
                kind = unit.get("semantic_kind") or unit.get("environment") or "theorem"
                uncertainty = None
                if kind not in MAJOR_KINDS:
                    uncertainty = f"legacy semantic kind {kind!r} recorded as proposition"
                    limitations.append(f"unit {uid}: {uncertainty}")
                    kind = "proposition"
                passages = []
                a_stmt = anchor_for(uid, "statement", unit.get("statement") or {})
                if a_stmt:
                    passages.append({"role": "statement", "anchor_id": a_stmt})
                a_proof = anchor_for(uid, "proof", unit.get("proof") or {}) if unit.get("proof") else None
                if a_proof:
                    passages.append({"role": "proof", "anchor_id": a_proof})
                anchors_by_unit[uid] = [a for a in (a_stmt, a_proof) if a]
                proof_anchor[uid] = [a_proof] if a_proof else []
                text = unit.get("statement_excerpt") or ""
                form = "verbatim"
                if not text.strip():
                    text = f"Statement text of {uid} is not recorded in the legacy inventory."
                    form = "synopsis"
                    uncertainty = (uncertainty + "; " if uncertainty else "") + "statement text missing in legacy inventory"
                edits.append(_create("items", item_ids[uid], {
                    "kind": kind, "label": unit.get("label") or uid, "caption": unit.get("statement_title") or "",
                    "statement": {"form": form, "text": text}, "passages": passages, "aliases": [uid],
                    "uncertainty": uncertainty, "origin": "source", "owner_id": None, "scope_id": None}))
            # external results
            for ext in registry.get("external_results") or []:
                eid = ext.get("id") or ext.get("external_id") or f"external-{len(item_ids)}"
                item_ids[eid] = mapper.assign("items", eid, prefer=_slug(eid), rationale="legacy external result")
                text = ext.get("statement") or ext.get("needed_form") or ext.get("citation") or eid
                edits.append(_create("items", item_ids[eid], {
                    "kind": "external_result", "label": ext.get("label") or ext.get("citation") or eid,
                    "caption": ext.get("citation") or "", "statement": {"form": "transcription", "text": text},
                    "passages": [], "aliases": [eid], "uncertainty": None, "origin": "source",
                    "owner_id": None, "scope_id": None}))
            # uses
            use_ids: dict = {}
            for use in registry.get("internal_uses") or []:
                src, dst = use.get("dependency_id"), use.get("dependent_unit")
                if src not in item_ids or dst not in item_ids or src == dst:
                    limitations.append(f"dependency use {use.get('use_id')} ({src} -> {dst}) skipped: unknown endpoints")
                    continue
                old = f"{dst}/{use.get('use_id') or 'use'}"
                new = mapper.assign("uses", old, prefer=_slug(f"{dst}-{use.get('use_id') or 'use'}"),
                                    rationale="legacy dependency registry use")
                use_ids[(dst, use.get("use_id"))] = new
                needed = use.get("needed_form")
                edits.append(_create("uses", new, {
                    "from": {"collection": "items", "id": item_ids[src]}, "to": {"collection": "items", "id": item_ids[dst]},
                    "type": "dependency", "group_id": None,
                    "reason": use.get("compatibility_check") or f"Legacy dependency use {use.get('use_id')} of {src} in {dst}.",
                    "needed_form": {"form": "transcription", "text": needed} if isinstance(needed, str) and needed.strip() else None,
                    "substitutions": [], "evidence_refs": proof_anchor.get(dst, []), "regime": None,
                    "uncertainty": "grouping of legacy step ids " + ", ".join(use.get("step_ids") or []) + " remains unmapped"
                    if use.get("step_ids") else None}))
            # audit
            audit_id = new_id("audits")
            majors = [item_ids[u["id"]] for u in units]
            edits.append(_create("audits", audit_id, {
                "paper_id": paper.id, "mode": "triage", "targets": [{"collection": "items", "id": i} for i in majors],
                "exclusions": [], "protocol_version": LEGACY_AUDIT_PROTOCOL, "independent_required": False,
                "qualification_id": None, "global_tasks": [], "report_path": ""}))
            mapper.note("audit:legacy", [{"collection": "audits", "id": audit_id}],
                        f"legacy audit {manifest.get('protocol', {}).get('skill_name', 'stat-paper-proofcheck')} "
                        f"{manifest.get('protocol', {}).get('skill_version', '')}".strip())
            scope_id = new_id("scopes")
            edits.append(_create("scopes", scope_id, {"argument_id": None, "parent_id": None, "assumptions": [],
                                                      "binders": [], "conditions": [], "evidence_refs": []}))
            # ledgers -> arguments, primary composition checks, legacy independent responses
            checks_by_unit: dict = {}
            qualification_id = None
            independent_units = [uid for uid, (_, led) in ledgers.items()
                                 if isinstance(led.get("independent_check"), dict) and led["independent_check"].get("status")]
            if independent_units:
                qual_blob = canonical_bytes({"kind": "legacy-independent-check-summary",
                                             "units": {uid: ledgers[uid][1]["independent_check"] for uid in independent_units}})
                blobs.append(qual_blob)
                qualification_id = new_id("qualifications")
                edits.append(_create("qualifications", qualification_id, {
                    "reviewer": "legacy-challenger", "profile": {
                        "provider": "legacy", "model": "stat-paper-proofcheck v1.5 challenger", "effort": None,
                        "tools": [], "context_isolation": ledgers[independent_units[0]][1]["independent_check"].get(
                            "independence_level") or "unknown"},
                    "protocol_version": LEGACY_AUDIT_PROTOCOL, "valid_case_results": [], "invalid_case_results": [],
                    "evidence_blob": sha256_bytes(qual_blob), "qualified": False,
                    "limitations": ["legacy challenger; no item-audit/1 calibration cases exist"]}))
            for uid, (path, led) in ledgers.items():
                if uid not in item_ids:
                    limitations.append(f"ledger {path.name} names unknown unit {uid}; skipped")
                    continue
                ledger_bytes = path.read_bytes()
                blobs.append(ledger_bytes)
                review = led.get("review") or {}
                outcome, status = _legacy_outcome(review)
                argument_id = new_id("arguments")
                edits.append(_create("arguments", argument_id, {
                    "target": {"collection": "items", "id": item_ids[uid]}, "label": f"Legacy ledger proof of {uid}",
                    "origin": "source", "scope_id": scope_id, "final_group_id": None,
                    "evidence_refs": proof_anchor.get(uid, []), "lifecycle": "draft"}))
                mapper.note(f"ledgers:{uid}", [{"collection": "arguments", "id": argument_id}],
                            f"legacy ledger {path.name} (sha256 {sha256_bytes(ledger_bytes)}); step grouping unmapped")
                steps = led.get("steps") or []
                reasoning = (f"Imported from legacy ledger {path.name}: unit_status={review.get('unit_status')!r}, "
                             f"argument_status={review.get('argument_status')!r}, "
                             f"statement_status={review.get('statement_status')!r}, "
                             f"dependency_closure={review.get('dependency_closure')!r}; "
                             f"{len(steps)} ledger steps, statuses "
                             + ", ".join(f"{s.get('id')}={s.get('status')}" for s in steps) + ".")
                check_id = new_id("checks")
                edits.append(_create("checks", check_id, {
                    "audit_id": audit_id, "target": {"collection": "arguments", "id": argument_id}, "kind": "composition",
                    "role": "primary", "reviewer": "legacy-ledger", "protocol_version": LEGACY_AUDIT_PROTOCOL,
                    "state": "complete", "outcome": outcome, "reasoning": reasoning,
                    "evidence_refs": anchors_by_unit.get(uid, []), "conditions": [c for c in review.get("explicit_assumptions") or [] if isinstance(c, str) and c.strip()],
                    "next_action": None, "response_id": None, "supersedes": None}))
                checks_by_unit[uid] = [check_id]
                indep = led.get("independent_check") if isinstance(led.get("independent_check"), dict) else None
                if indep and indep.get("status") and qualification_id:
                    artifact_rel = indep.get("artifact")
                    artifact_path = folder / artifact_rel if artifact_rel else None
                    if artifact_path is not None and artifact_path.is_file():
                        response_bytes = artifact_path.read_bytes()
                        exposure_note = f"legacy challenge artifact {artifact_rel}"
                    else:
                        response_bytes = canonical_bytes(indep)
                        exposure_note = "legacy challenge artifact missing; independent_check record preserved instead"
                        limitations.append(f"{uid}: {exposure_note}")
                    blobs.append(response_bytes)
                    response_id = new_id("responses")
                    edits.append(_create("responses", response_id, {
                        "audit_id": audit_id, "packet_id": f"legacy:{uid}", "reviewer": "legacy-challenger",
                        "qualification_id": qualification_id, "original_blob": sha256_bytes(response_bytes),
                        "covered_targets": [{"collection": "items", "id": item_ids[uid]}],
                        "coverage_note": f"legacy independence level {indep.get('independence_level')!r}",
                        "exposure": "source_only", "exposure_note": exposure_note, "state": "accepted"}))
                    ind_check = new_id("checks")
                    edits.append(_create("checks", ind_check, {
                        "audit_id": audit_id, "target": {"collection": "arguments", "id": argument_id},
                        "kind": "composition", "role": "independent", "reviewer": "legacy-challenger",
                        "protocol_version": LEGACY_AUDIT_PROTOCOL, "state": "complete",
                        "outcome": _legacy_verdict(indep.get("reconciled_verdict") or indep.get("challenger_verdict")),
                        "reasoning": f"Legacy independent check status {indep.get('status')!r}: challenger verdict "
                                     f"{indep.get('challenger_verdict')!r}, reconciled verdict "
                                     f"{indep.get('reconciled_verdict')!r}.",
                        "evidence_refs": anchors_by_unit.get(uid, []), "conditions": [], "next_action": None,
                        "response_id": response_id, "supersedes": None}))
                    checks_by_unit[uid].append(ind_check)
                    mapper.note(f"independent_checks:{uid}", [{"collection": "responses", "id": response_id},
                                                               {"collection": "checks", "id": ind_check}],
                                "legacy challenger response and verdict")
            for uid in item_ids:
                if uid not in ledgers and any(u["id"] == uid for u in units):
                    limitations.append(f"unit {uid} has no legacy ledger; no composition judgment imported")
            # issues -> findings
            finding_ids = []
            for issue in issue_log.get("issues") or []:
                unit = issue.get("affected_result") or (issue.get("origin_ref") or {}).get("unit_id")
                if unit not in item_ids:
                    limitations.append(f"issue {issue.get('id')} names unknown unit {unit!r}; skipped")
                    continue
                fid = mapper.assign("findings", str(issue.get("id") or f"issue-{len(finding_ids)}"),
                                    prefer=_slug(str(issue.get("id") or "")), rationale="legacy issue log entry")
                finding_ids.append(fid)
                affected = []
                for other in issue.get("affected_results") or []:
                    for (dst, use_id), new in use_ids.items():
                        if dst == other and dst != unit:
                            affected.append(new)
                for ref in issue.get("contract_refs") or []:
                    if ref.get("kind") == "dependency_use" and (ref.get("unit_id"), ref.get("use_id")) in use_ids:
                        affected.append(use_ids[(ref["unit_id"], ref["use_id"])])
                lifecycle = "resolved" if str(issue.get("status", "")).lower() in ("closed", "resolved") else "open"
                impact = (f"legacy severity {issue.get('severity')}, load_bearing={issue.get('load_bearing')}, "
                          f"affected results {issue.get('affected_results') or [unit]}")
                edits.append(_create("findings", fid, {
                    "audit_id": audit_id, "target": {"collection": "items", "id": item_ids[unit]},
                    "category": _finding_category(issue), "lifecycle": lifecycle,
                    "description": issue.get("summary") or f"legacy issue {issue.get('id')}",
                    "evidence_refs": anchors_by_unit.get(unit, []),
                    "check_refs": [{"collection": "checks", "id": c, "version": 1} for c in checks_by_unit.get(unit, [])],
                    "affected_uses": sorted(set(affected)), "impact_reason": impact,
                    "resolution": issue.get("resolution") if lifecycle == "resolved" else None}))
            # paper, identity map
            body = dict(paper.body)
            body["main_items"] = [item_ids[u["id"]] for u in units]
            edits.append({"op": "replace", "collection": "papers", "id": paper.id, "expected_version": paper.version,
                          "body": body})
            artifact = {"kind": "stat-paper-proofcheck-legacy-import", "folder": folder.name,
                        "protocol": manifest.get("protocol") or {}, "files": artifact_files,
                        "source_root": source_root.resolve().as_posix()}
            artifact_bytes = canonical_bytes(artifact)
            blobs.append(artifact_bytes)
            map_body = {"reason": "import", "source_blob": sha256_bytes(artifact_bytes), "response_id": None,
                        "entries": mapper.entries, "reviewer": "import-legacy",
                        "note": f"Imported legacy audit folder {folder.name}; ledger step grouping, scopes and "
                                f"inference groups remain unmapped work."}
            map_id = new_id("identity_maps")
            edits.append(_create("identity_maps", map_id, map_body))
            receipt = _finish_import(
                db, request_id=new_id("request"),
                request_digest=digest({"command": "import-legacy", "artifact": sha256_bytes(artifact_bytes)}),
                edits=edits, blobs=blobs, warnings=limitations)
            counts = {}
            for e in edits:
                if e["op"] == "create":
                    counts[e["collection"]] = counts.get(e["collection"], 0) + 1
            paper_id = paper.id
    except Exception:
        db_path.unlink(missing_ok=True)
        raise
    artifact_path = mapping_path.with_name(mapping_path.stem + ".import-artifact.json")
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(artifact_bytes)
    mapping = dict(map_body)
    mapping["import_artifact"] = {"path": str(artifact_path), "sha256": sha256_bytes(artifact_bytes)}
    mapping["identity_map_id"] = map_id
    mapping_path.write_text(json.dumps(mapping, indent=1, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    return {"database": str(db_path), "mapping": str(mapping_path), "import_artifact": mapping["import_artifact"],
            "paper_id": paper_id, "audit_id": audit_id, "revision": receipt["revision"], "counts": counts,
            "remapped": mapper.remapped, "limitations": list(dict.fromkeys(limitations)), "init": init,
            "receipt": receipt}


__all__ = ["LEGACY_AUDIT_PROTOCOL", "envelope", "export_snapshot", "import_legacy", "migrate_overview",
           "write_export"]
