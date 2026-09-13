"""Describe captured paper revisions and conservative source-comparison reuse.

The report suggests targets for an explicit review of the source changes. It
does not carry observations forward, decide proof validity, or read live files.
"""
from __future__ import annotations

from collections import deque
import difflib
import json

import paper_records as records


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _index(data):
    return {"items": {row["id"]: row for row in data["items"]},
            "uses": {row["id"]: row for row in data["uses"]},
            "anchors": {row["id"]: row for row in data["anchors"]},
            "files": {row["id"]: row for row in data["source_revision"]["files"]}}


def _label(index, collection, identity):
    row = index[collection][identity]
    if collection == "items":
        return row["label"]
    source = index["items"][row["from"]]["label"]
    target = index["items"][row["to"]]["label"]
    return f"{source} → {target}" + (f" ({row['regime']})" if row.get("regime") else "")


def _reference(before, after, collection, identity):
    index = after if identity in after[collection] else before
    return {"collection": collection, "id": identity, "label": _label(index, collection, identity)}


def _is_pdf(source):
    return bool(source and source["media_type"] == "application/pdf")


def _source_changes(before, after):
    result = []
    for identity in sorted(before.keys() | after.keys()):
        old, new = before.get(identity), after.get(identity)
        if old and new and all(old[field] == new[field] for field in ("path", "sha256", "media_type")):
            continue
        status = "added" if old is None else "removed" if new is None else (
            "renamed" if old["path"] != new["path"] and old["sha256"] == new["sha256"] and old["media_type"] == new["media_type"] else "changed")
        entry = {"file_id": identity, "path": (new or old)["path"], "before_path": old["path"] if old else None,
                 "status": status, "before_sha256": old["sha256"] if old else None,
                 "after_sha256": new["sha256"] if new else None}
        binary = _is_pdf(old) or _is_pdf(new)
        try:
            old_text = records._source_bytes(old).decode("utf-8") if old else ""
            new_text = records._source_bytes(new).decode("utf-8") if new else ""
        except UnicodeError:
            binary = True
        if binary:
            entry.update(diff=None, diff_kind="binary", note="Captured byte hashes identify this change. Review the document directly; no complete text diff is claimed.")
        else:
            lines = difflib.unified_diff(old_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
                                        fromfile="a/" + old["path"] if old else "/dev/null",
                                        tofile="b/" + new["path"] if new else "/dev/null")
            # Keep every changed hunk and preserve line-ending differences.
            entry.update(diff="".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines),
                         diff_kind="unified_text")
        result.append(entry)
    return result


def _anchor_content(anchor, files):
    source = files.get(anchor.get("file_id"))
    result = {"id": anchor["id"], "file_id": anchor.get("file_id"), "excerpt": anchor["excerpt"],
              "verification": anchor["verification"], "media_type": source["media_type"] if source else None}
    if not anchor["excerpt"] or _is_pdf(source):
        # A label alone supplies no local content evidence. A changed PDF also
        # cannot be matched safely by its extracted text alone.
        result.update(locator=anchor["locator"], file_sha256=source["sha256"] if source else None)
    else:
        location_fields = {"start_line", "end_line"}
        if source and "start_line" in anchor["locator"]:
            # A page attached to a text passage is navigation metadata, not
            # the evidence selector. Reuse still requires reviewing its change.
            location_fields.add("page")
        result["locator"] = {key: value for key, value in anchor["locator"].items() if key not in location_fields}
    return result


def _anchor_changes(before, after):
    result = []
    for identity in sorted(before["anchors"].keys() | after["anchors"].keys()):
        old, new = before["anchors"].get(identity), after["anchors"].get(identity)
        source = (after if new else before)["files"].get((new or old).get("file_id"))
        entry = {"id": identity, "path": source["path"] if source else None,
                 "before_locator": old["locator"] if old else None, "after_locator": new["locator"] if new else None}
        if not old or not new:
            entry["status"] = "added" if new else "removed"
        elif _json(_anchor_content(old, before["files"])) != _json(_anchor_content(new, after["files"])):
            entry["status"] = "changed" if old["excerpt"] != new["excerpt"] else "context_changed"
        elif old["locator"] != new["locator"] or old.get("file_id") != new.get("file_id"):
            entry["status"] = "moved"
            if old["locator"].get("page") != new["locator"].get("page"):
                entry["note"] = "The text passage is unchanged; check the corrected PDF page before reusing comparisons."
        else:
            continue
        result.append(entry)
    return result


def _local_context(index, collection, identity):
    target = index[collection][identity]
    dependent = identity if collection == "items" else target["to"]
    uses = sorted((use for use in index["uses"].values() if use["to"] == dependent), key=lambda use: use["id"])
    item_ids = {dependent} | {use["from"] for use in uses}
    items = [index["items"][item_id] for item_id in sorted(item_ids)]
    anchor_ids = {passage["anchor_id"] for item in items for passage in item["passages"]}
    anchor_ids.update(anchor_id for use in uses for anchor_id in use["evidence_refs"])
    anchors = [index["anchors"][anchor_id] for anchor_id in sorted(anchor_ids)]
    content = {"target": target, "items": items, "incoming_uses": uses,
               "anchors": [_anchor_content(anchor, index["files"]) for anchor in anchors]}
    unsupported = any(not anchor["excerpt"] and not index["files"].get(anchor.get("file_id")) for anchor in anchors)
    return content, unsupported


def build_changes(before, after):
    """Compare two frozen schema-2 snapshots without altering either one.

    Reuse candidates are a necessary mechanical precondition, never permission
    to skip reviewing changed source context. Dependency impact is conservative.
    """
    before, after = records.validate_records(before), records.validate_records(after)
    old, new = _index(before), _index(after)
    sources = _source_changes(old["files"], new["files"])
    anchors = _anchor_changes(old, new)
    changed_anchors = {row["id"] for row in anchors if row["status"] != "moved"}
    changes = {}

    def changed(collection, identity, reason, change="changed", fields=()):
        key = (collection, identity)
        if key not in changes:
            changes[key] = {**_reference(old, new, collection, identity), "change": change, "reasons": [], "fields": []}
        changes[key]["reasons"].append(reason)
        changes[key]["fields"] = sorted(set(changes[key]["fields"]) | set(fields))

    for collection in ("items", "uses"):
        for identity in sorted(old[collection].keys() | new[collection].keys()):
            left, right = old[collection].get(identity), new[collection].get(identity)
            if left is None or right is None:
                changed(collection, identity, "Record added." if right else "Record removed.", "added" if right else "removed")
            elif _json(left) != _json(right):
                fields = [key for key in left.keys() | right.keys() if left.get(key) != right.get(key)]
                changed(collection, identity, "Recorded content changed.", fields=fields)
            for row in (left, right):
                if row is None:
                    continue
                references = ({passage["anchor_id"] for passage in row["passages"]} if collection == "items" else set(row["evidence_refs"]))
                if references & changed_anchors:
                    changed(collection, identity, "A referenced source passage or its evidence context changed.")
                    break

    outgoing = {}
    all_uses = list(old["uses"].values()) + list(new["uses"].values())
    for use in all_uses:
        outgoing.setdefault(use["from"], set()).add(use["to"])
    seeds = {identity for collection, identity in changes if collection == "items"}
    for collection, identity in changes:
        if collection == "uses":
            seeds.update(index["uses"][identity]["to"] for index in (old, new) if identity in index["uses"])
    reached, queue = set(seeds), deque(sorted(seeds))
    while queue:
        for dependent in sorted(outgoing.get(queue.popleft(), ())):
            if dependent not in reached:
                reached.add(dependent)
                queue.append(dependent)
    affected = {("items", identity) for identity in reached}
    affected.update(("uses", use["id"]) for use in all_uses if use["from"] in reached or use["to"] in reached)
    affected.difference_update(changes)

    prior = {(row["target"]["collection"], row["target"]["id"]): row for row in records.applicable_observations(before)}
    current = {(row["target"]["collection"], row["target"]["id"]): row for row in records.applicable_observations(after)}
    candidates = []
    for collection in ("items", "uses"):
        for identity in sorted(old[collection].keys() & new[collection].keys()):
            key = (collection, identity)
            observation = prior.get(key)
            if key in changes or key in affected or not observation or observation["result"] != "matched":
                continue
            if observation["input_snapshot"] != records.target_digest(before, collection, identity):
                continue
            latest = current.get(key)
            if latest and latest["result"] == "needs_attention" and latest["input_snapshot"] == records.target_digest(after, collection, identity):
                continue
            old_context, old_unsupported = _local_context(old, collection, identity)
            new_context, new_unsupported = _local_context(new, collection, identity)
            if old_unsupported or new_unsupported or _json(old_context) != _json(new_context):
                continue
            candidates.append(_reference(old, new, collection, identity))

    limitations = ["Reuse candidates require an explicit review of the revision context. This report does not carry comparisons forward or verify proofs.",
                   "Potential impact follows recorded connections across all regimes. It does not establish that a result is false or that every alternative argument fails."]
    metadata_changes = [key for key in ('title', 'scope', 'main_items') if before.get(key) != after.get(key)]
    if before.get('inventory', {}).get('excluded', []) != after.get('inventory', {}).get('excluded', []):
        metadata_changes.append('inventory.excluded')
    if metadata_changes:
        limitations.append('Overview metadata changed: ' + ', '.join(metadata_changes) + '. Review the revised scope and presentation; these are not counted as changed item/use records.')
    if sources:
        limitations.append("Review every changed source file, including changes outside recorded passages. Unchanged excerpts do not establish unchanged macros, assumptions, conditioning, or interpretation.")
    if any(row["status"] in {"added", "removed"} for row in sources):
        limitations.append("Added or removed source files require a review of scope and context; this helper does not infer file replacements.")
    if any(row["diff_kind"] == "binary" for row in sources):
        limitations.append("Changed PDFs and non-UTF-8 files require direct document review. Targets relying on a changed PDF are not mechanical reuse candidates.")
    if any(row.get("note") for row in anchors):
        limitations.append("For location-only changes, check the corrected pages before reviewed reuse; unchanged downstream arguments need not be read again. Other source or record changes still require context review.")
    changed_list = [changes[key] for key in sorted(changes)]
    affected_list = [_reference(old, new, collection, identity) for collection, identity in sorted(affected)]
    return {"schema_version": 1, "from_snapshot": before["snapshot_id"], "to_snapshot": after["snapshot_id"],
            "source_changes": sources, "anchor_changes": anchors, "changed_targets": changed_list,
            "potentially_affected": affected_list, "reuse_candidates": candidates,
            "counts": {"source_changes": len(sources), "anchor_changes": len(anchors),
                       "moved_anchors": sum(row["status"] == "moved" for row in anchors),
                       "changed_targets": len(changed_list), "potentially_affected": len(affected_list),
                       "reuse_candidates": len(candidates)}, "limitations": limitations}
