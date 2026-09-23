"""Selected overview projection and shared-field updates for the common store.

The native overview algorithms consume a disposable projection. Its snapshots are
never an authority: accepted changes update versioned common records atomically.
"""
from __future__ import annotations

import base64
import copy
from pathlib import Path

from .acceptance import accept_in_transaction
from .canonical import digest, sha256_bytes
from .errors import InvalidRequest
from .ids import new_id
from .storage import paper_record

SELECTION_ID = "overview-default"


def comparison_context(state, target, selection_id):
    """Native broad comparison digest from a revision/acceptance-overlay state.

    Read only the selected graph, its recorded source manifest and anchors.
    Source bytes and audit extensions cannot enlarge this comparison scope.
    """
    selection = state.live("overview_selections", selection_id)
    if selection is None:
        return None
    body = selection.body
    selected = [selection]
    anchor_ids = list(body.get("native_context", {}).get("anchor_ids", []))
    for collection, key in (("items", "item_ids"), ("uses", "use_ids"), ("sources", "source_ids")):
        for identity in body[key]:
            record = state.live(collection, identity)
            if record is None:
                return None
            selected.append(record)
            if collection == "items":
                anchor_ids.extend(p["anchor_id"] for p in record.body["passages"])
            elif collection == "uses":
                anchor_ids.extend(record.body["evidence_refs"])
    for identity in dict.fromkeys(anchor_ids):
        anchor = state.live("anchors", identity)
        if anchor is None:
            return None
        selected.append(anchor)

    class SelectedState:
        def max_revision(self):
            return 0

        def records_at(self, revision):
            return selected

        def get_blob(self, sha):
            # target_digest uses manifest hashes, not embedded source bytes.
            return b""

    from .export_import import _overview_target_digest
    data, _ = project(SelectedState())
    if not any(row["id"] == target["id"] for row in data.get(target["collection"], [])):
        return None
    return _overview_target_digest(data, target["collection"], target["id"])


def selection_body(paper_id, data, *, profile="compatibility"):
    """Preserve native presentation metadata without copying mathematical rows."""
    pdf_sources = {f["id"] for f in data["source_revision"]["files"] if f["media_type"] == "application/pdf"}
    context = {
        "source_title": data["source_revision"]["title"],
        "source_created_at": data["source_revision"]["created_at"],
        "inventory": copy.deepcopy(data.get("inventory")),
        "main_items_present": "main_items" in data,
        "anchor_ids": [a["id"] for a in data["anchors"]],
        "optional_fields": {c: {r["id"]: sorted(r) for r in data[c]} for c in ("items", "uses")},
        "anchor_metadata": {a["id"]: {"verification": a["verification"], "locator_fields": sorted(a["locator"]),
                            **({"supplemental_page": a["locator"]["page"]}
                               if a["file_id"] not in pdf_sources and a["locator"].get("page") is not None else {})}
                            for a in data["anchors"]},
        "groups": {u["id"]: u["group"] for u in data["uses"] if u.get("group") is not None},
        "archived_observations": [copy.deepcopy(o) for o in data.get("observations", [])
                                  if not any(r["id"] == o["target"]["id"] for r in data.get(o["target"]["collection"], []))],
    }
    return {"paper_id": paper_id, "title": data["title"], "scope": data["scope"],
            "item_ids": [r["id"] for r in data["items"]], "use_ids": [r["id"] for r in data["uses"]],
            "main_item_ids": list(data.get("main_items", [])),
            "source_ids": [r["id"] for r in data["source_revision"]["files"]],
            "authoring_profile": profile, "roots": [],
            "unresolved": list(data.get("inventory", {}).get("unresolved", [])), "native_context": context}


def project(db, revision=None):
    """Read selected shared content; audit additions do not expand the view."""
    revision = db.max_revision() if revision is None else revision
    if hasattr(db, "latest_at"):
        # Ordinary overview reads do not reconstruct the full audit export.
        selections = db.records_at(revision, "overview_selections")
        records = {(r.collection, r.id): r for r in selections if not r.retired}
    else:
        records = {(r.collection, r.id): r for r in db.records_at(revision) if not r.retired}
        selections = [r for (c, _), r in records.items() if c == "overview_selections"]
    selection = next((r for r in selections if r.id == SELECTION_ID), selections[0] if selections else None)
    if selection is None:
        raise InvalidRequest("This common database has no overview selection. Register overview_selections before opening its selected view.")
    body = selection.body
    context = body.get("native_context", {})

    def get(collection, identifier):
        record = records.get((collection, identifier))
        if record is None and hasattr(db, "latest_at"):
            record = db.latest_at(collection, identifier, revision)
            if record and not record.retired:
                records[collection, identifier] = record
        if record is None or record.retired:
            raise InvalidRequest(f"Selected {collection}:{identifier} is unavailable at revision {revision}")
        return record

    files = []
    for identifier in body["source_ids"]:
        source = get("sources", identifier).body
        raw = db.get_blob(source["blob_sha256"])
        if raw is None:
            raise InvalidRequest(f"Captured source bytes are missing for {identifier}")
        files.append({"id": identifier, "path": source["path"], "media_type": "application/pdf" if source["media_type"] == "pdf" else "text/plain",
                      "sha256": source["blob_sha256"], "content_base64": base64.b64encode(raw).decode("ascii")})
    source_identity = digest(sorted(({k: f[k] for k in ("id", "path", "media_type", "sha256")} for f in files), key=lambda r: r["id"]))
    data = {"schema_version": 3, "title": body["title"], "scope": body.get("scope") or "Selected overview",
            "source_revision": {"id": source_identity, "title": context.get("source_title", body["title"]),
                                "created_at": context.get("source_created_at", "1970-01-01T00:00:00+00:00"), "files": files},
            "items": [], "uses": [], "anchors": [],
            "observations": copy.deepcopy(context.get("archived_observations", []))}
    optional = context.get("optional_fields", {})
    for identifier in body["item_ids"]:
        item = get("items", identifier).body
        row = {"id": identifier, **{k: copy.deepcopy(item[k]) for k in ("kind", "label", "caption", "statement", "passages")}}
        for old, new in (("owner", "owner_id"), ("issue", "uncertainty"), ("aliases", "aliases")):
            if old in optional.get("items", {}).get(identifier, []) or item.get(new):
                row[old] = copy.deepcopy(item.get(new))
        data["items"].append(row)
    for identifier in body["use_ids"]:
        use = get("uses", identifier).body
        if any(use[key]["collection"] != "items" for key in ("from", "to")):
            raise InvalidRequest(f"Selected use {identifier} needs item endpoints for overview authoring")
        row = {"id": identifier, "from": use["from"]["id"], "to": use["to"]["id"],
               **{k: copy.deepcopy(use[k]) for k in ("type", "reason", "evidence_refs")}}
        for old, new in (("issue", "uncertainty"), ("regime", "regime")):
            if old in optional.get("uses", {}).get(identifier, []) or use.get(new):
                row[old] = copy.deepcopy(use.get(new))
        if identifier in context.get("groups", {}):
            row["group"] = copy.deepcopy(context["groups"][identifier])
        data["uses"].append(row)
    anchor_ids = list(context.get("anchor_ids", []))
    needed = [p["anchor_id"] for i in data["items"] for p in i["passages"]]
    needed += [a for u in data["uses"] for a in u["evidence_refs"]]
    anchor_ids += [a for a in needed if a not in anchor_ids]
    for identifier in dict.fromkeys(anchor_ids):
        anchor = get("anchors", identifier).body
        metadata = context.get("anchor_metadata", {}).get(identifier, {})
        locator = {k: v for k, v in anchor["locator"].items() if v is not None or k in metadata.get("locator_fields", [])}
        if "supplemental_page" in metadata:
            locator["page"] = metadata["supplemental_page"]
        verification = copy.deepcopy(metadata.get("verification", {"status": "unverified" if anchor.get("limitation") else "checked", "method": anchor["method"]}))
        data["anchors"].append({"id": identifier, "source_revision": source_identity, "file_id": anchor["source_id"],
                                "locator": locator, "excerpt": anchor["excerpt"], "excerpt_hash": anchor["excerpt_sha256"],
                                "verification": verification})
    observation_rows = db.records_at(revision, "observations") if hasattr(db, "latest_at") else [r for (c, _), r in records.items() if c == "observations"]
    observations = sorted((r for r in observation_rows if not r.retired), key=lambda r: (r.revision, (r.body.get("context_data") or {}).get("observation_order", 0), r.body.get("created_at", ""), r.id))
    for observation in observations:
        details = observation.body.get("context_data") or {}
        if observation.body.get("context_kind") == "overview" and details.get("selection_id") == selection.id:
            original = details.get("native_observation")
            if original and not any(o["id"] == original["id"] for o in data["observations"]):
                data["observations"].append(copy.deepcopy(original))
    if context.get("main_items_present", bool(body["main_item_ids"])):
        data["main_items"] = list(body["main_item_ids"])
    if context.get("inventory") is not None:
        data["inventory"] = copy.deepcopy(context["inventory"])
    return data, selection


def update(db, before, after, *, profile, source_root=None):
    """Accept shared-field edits while retaining every unselected or audit field.

    Caller holds BEGIN IMMEDIATE and validates the native projection first.
    Removing an overview row changes selection only, not its common identity.
    """
    edits, blobs = [], []
    paper = paper_record(db)

    def upsert(collection, identifier, fields):
        head = db.head(collection, identifier)
        body = {**(head.body if head and not head.retired else {}), **fields}
        if head is not None and head.retired:
            raise InvalidRequest(f"Cannot reuse retired {collection}:{identifier}")
        if head is None or body != head.body:
            edits.append({"op": "replace" if head else "create", "collection": collection, "id": identifier,
                          "expected_version": head.version if head else None, "body": body})
        return (head.version if head else 0) + int(head is None or body != head.body)

    source_versions = {}
    pdf_sources = {f["id"] for f in after["source_revision"]["files"] if f["media_type"] == "application/pdf"}
    for source in after["source_revision"]["files"]:
        from .sources import media_type
        raw = base64.b64decode(source["content_base64"])
        blobs.append(raw)
        old = db.head("sources", source["id"])
        source_versions[source["id"]] = upsert("sources", source["id"], {
            "paper_id": paper.id, "path": source["path"], "media_type": media_type(source["path"]),
            "blob_sha256": source["sha256"], "capture_method": old.body["capture_method"] if old else "overview_capture",
            "limitation": old.body.get("limitation") if old else None})
    for anchor in after["anchors"]:
        loc = anchor["locator"]
        if anchor.get("file_id") not in source_versions:
            raise InvalidRequest(f"Overview anchor {anchor['id']} needs a registered captured source file before common-store initialization")
        shared_locator = {k: loc.get(k) for k in ("start_line", "end_line", "page", "label")}
        # A page beside a text line/label locator is navigation metadata, not
        # evidence that the captured text file is a reviewed PDF.
        if anchor["file_id"] not in pdf_sources:
            shared_locator["page"] = None
        if not any(shared_locator.get(k) is not None for k in ("start_line", "page", "label")):
            raise InvalidRequest(f"Overview anchor {anchor['id']} needs a line range, page or label in its captured source")
        upsert("anchors", anchor["id"], {"source_id": anchor["file_id"], "source_version": source_versions[anchor["file_id"]],
            "locator": shared_locator,
            "excerpt": anchor["excerpt"], "excerpt_sha256": anchor["excerpt_hash"],
            "method": "exact_lines" if "start_line" in loc else "label_match" if "label" in loc else "reviewed_page",
            "limitation": None if anchor["verification"]["status"] == "checked" else anchor["verification"].get("note", "Unverified overview locator")})
    for item in after["items"]:
        existing = db.head("items", item["id"])
        defaults = {} if existing else {"origin": "source", "owner_id": item.get("owner"), "scope_id": None}
        fields = {k: item[k] for k in ("kind", "label", "caption", "statement", "passages")}
        fields.update(aliases=item.get("aliases", []), uncertainty=item.get("issue"), **defaults)
        # An explicit retained-rich owner edit is shared navigation, not scope.
        if "owner" in item:
            fields["owner_id"] = item["owner"]
        upsert("items", item["id"], fields)
    for use in after["uses"]:
        upsert("uses", use["id"], {"from": {"collection": "items", "id": use["from"]}, "to": {"collection": "items", "id": use["to"]},
            "type": use["type"], "reason": use["reason"], "regime": use.get("regime"), "uncertainty": use.get("issue"),
            "evidence_refs": use["evidence_refs"]})
    selection = db.head("overview_selections", SELECTION_ID)
    upsert("overview_selections", SELECTION_ID, selection_body(paper.id, after, profile=profile))
    old_observations = {o["id"] for o in before.get("observations", [])}
    from .export_import import _applicable_observations
    applicable = _applicable_observations({"payload": after, "observations": after.get("observations", [])})
    for observation_order, observation in enumerate(after.get("observations", [])):
        if observation["id"] in old_observations:
            continue
        target = observation["target"]
        row = next((r for r in after[target["collection"]] if r["id"] == target["id"]), None)
        if row is None:
            continue  # Retained as historical metadata, without a dangling SQL reference.
        evidence = row["evidence_refs"] if target["collection"] == "uses" else [p["anchor_id"] for p in row["passages"]]
        upsert("observations", observation["id"], {"target": target, "result": observation["result"], "reviewer": observation["reviewer"],
            "note": observation["note"], "created_at": observation["created_at"], "evidence_refs": evidence,
            "context_kind": "overview", "context_data": {"selection_id": SELECTION_ID, "input_snapshot": observation["input_snapshot"],
                "native_observation": observation, "observation_order": observation_order,
                "applicable_on_import": observation["id"] in applicable}})
    paper_fields = {"main_items": list(after.get("main_items", [])), "title": after["title"], "scope": after["scope"],
                    "exclusions": after.get("inventory", {}).get("excluded", [])}
    if source_root is not None:
        paper_fields["source_root"] = Path(source_root).resolve().as_posix()
    upsert("papers", paper.id, paper_fields)
    if not edits:
        return None
    return accept_in_transaction(db, request_id=new_id("request"), request_digest=digest(edits), packet_id=None,
                                 edits=edits, command="overview", blobs=blobs)
