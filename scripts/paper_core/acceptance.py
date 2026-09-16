"""Transactional acceptance of edit batches (implementation-handoff 4.3).

Every write path (apply, compare, source commands, review commands, import)
funnels through :func:`accept`. The whole algorithm runs inside one
``BEGIN IMMEDIATE`` transaction: idempotency, packet checks, optimistic
concurrency, semantic validation, revision allocation, receipt, and the
generated indexes. Nothing is visible to readers until COMMIT, and the receipt
is returned only after COMMIT.
"""
from __future__ import annotations

import json
import sqlite3

from .bindings import BOUND_COLLECTIONS, compute_bindings
from .canonical import digest
from .contract import BATCH, extract_refs, validate_shape
from .errors import ConflictError, InvalidRequest
from .ids import valid_id
from .packets import source_context_digest
from .refs import facet_digests, membership_digest, relation_members
from .storage import Database
from .validation import Planned, State, validate_plan

PACKETLESS_COMMANDS = ("import", "source_capture", "qualification")
COMMAND_MODES = {
    "apply": ("author", "primary"),
    "compare": ("author", "primary"),
    "work_primary": ("primary",),
    "source_anchor": ("author", "primary", "reconcile"),
    "source_review": ("author", "primary", "reconcile"),
    "review_submit": ("independent",),
    "review_map": ("independent", "primary", "reconcile"),
    "reconcile": ("reconcile",),
}


def _retry_command(manifest: dict) -> dict:
    targets = " ".join(f"--target {t['collection']}:{t['id']}" for t in manifest["targets"])
    return {"command": f"get DB {targets} --mode {manifest['mode']} --out PACKET.json",
            "targets": manifest["targets"], "mode": manifest["mode"]}


def _plan(db: Database, edits: list, base_revision: int, conflicts: list) -> list:
    plan, seen, errors = [], set(), []
    for index, edit in enumerate(edits):
        collection, id = edit["collection"], edit["id"]
        if not valid_id(id):
            errors.append(f"edits/{index}: invalid identifier {id!r}")
            continue
        if (collection, id) in seen:
            errors.append(f"edits/{index}: {collection}:{id} appears twice in one batch")
            continue
        seen.add((collection, id))
        head = db.head(collection, id)
        op = edit["op"]
        if op == "create":
            if head is not None:
                if head.retired:
                    errors.append(f"edits/{index}: {collection}:{id} was retired; identifiers are never reused")
                elif head.revision > base_revision:
                    conflicts.append({"ref": {"collection": collection, "id": id}, "expected_version": None,
                                      "actual_version": head.version, "retired": False})
                else:
                    errors.append(f"edits/{index}: {collection}:{id} already exists at version {head.version}")
                continue
            plan.append(Planned(index, op, collection, id, None, edit["body"], version=1))
            continue
        if head is None:
            errors.append(f"edits/{index}: {collection}:{id} does not exist")
            continue
        if head.retired:
            errors.append(f"edits/{index}: {collection}:{id} is retired")
            continue
        if edit["expected_version"] != head.version:
            conflicts.append({"ref": {"collection": collection, "id": id}, "expected_version": edit["expected_version"],
                              "actual_version": head.version, "retired": False})
            continue
        plan.append(Planned(index, op, collection, id, head.version, edit.get("body"), reason=edit.get("reason"),
                            prev=head, version=head.version + 1))
    if errors:
        raise InvalidRequest("batch rejected", records=errors)
    return plan


def _read_set_conflicts(db: Database, manifest: dict, conflicts: list):
    known = {(c["ref"]["collection"], c["ref"]["id"]) for c in conflicts}
    for pinned in manifest["read_set"]:
        key = (pinned["collection"], pinned["id"])
        if key in known:
            continue
        head = db.head(*key)
        if head is None or head.version != pinned["version"] or head.retired:
            conflicts.append({"ref": {"collection": key[0], "id": key[1]}, "expected_version": pinned["version"],
                              "actual_version": None if head is None else head.version,
                              "retired": bool(head is not None and head.retired)})


def _guard_changes(db: Database, manifest: dict) -> list:
    changed = []
    for guard in manifest["membership_guards"]:
        members = relation_members(db.conn, guard["relation"], guard["key"])
        actual = membership_digest(members)
        if actual != guard["digest"]:
            changed.append({"relation": guard["relation"], "key": guard["key"], "expected_digest": guard["digest"],
                            "actual_digest": actual})
    return changed


def accept(db: Database, *, request_id: str, request_digest: str, packet_id, edits: list, command: str,
           blobs=(), annotations=None, warnings=(), check_guards: bool = True, scope_exempt=frozenset(),
           required_packet_ids=(), freshness_validator=None, receipt_context=None) -> dict:
    """Run the acceptance algorithm for one batch and return its immutable receipt."""
    if not db.write:
        raise InvalidRequest("the database is open read-only")
    db.begin_immediate()
    try:
        receipt = accept_in_transaction(
            db, request_id=request_id, request_digest=request_digest, packet_id=packet_id, edits=edits,
            command=command, blobs=blobs, annotations=annotations, warnings=warnings,
            check_guards=check_guards, scope_exempt=scope_exempt, required_packet_ids=required_packet_ids,
            freshness_validator=freshness_validator, receipt_context=receipt_context)
        try:
            db.commit()
        except sqlite3.IntegrityError as exc:
            raise InvalidRequest(f"batch violates referential integrity: {exc}", code="INTEGRITY") from exc
    except BaseException:
        try:
            db.rollback()
        except sqlite3.Error:
            pass
        raise
    return receipt


def accept_in_transaction(db: Database, *, request_id: str, request_digest: str, packet_id,
                          edits: list, command: str, blobs=(), annotations=None, warnings=(),
                          check_guards: bool = True, scope_exempt=frozenset(), required_packet_ids=(),
                          freshness_validator=None, receipt_context=None) -> dict:
    """Accept within the caller's writer transaction, without committing or rolling back.

    The optional trusted task validator returns a manifest with a validated task-specific
    read set and guards. It may not change identity, permissions or source context. Ordinary
    packets and callers retain their exact version checks.
    """
    if not db.write or not db.conn.in_transaction:
        raise InvalidRequest("accept_in_transaction requires a caller-owned write transaction")
    annotations = annotations or {}
    receipt_context = receipt_context or {}
    if set(receipt_context) - {"packet_id", "acceptance_packet_id"}:
        raise InvalidRequest("unsupported receipt context fields")
    # (1) idempotency
    row = db.commit_by_request(request_id)
    if row is not None:
        if row["request_digest"] == request_digest:
            receipt = json.loads(row["receipt_json"])
            return receipt
        raise InvalidRequest(f"request id {request_id} was already used for a different batch",
                             code="REQUEST_ID_REUSED", records=[{"request_id": request_id,
                                                                 "revision": row["revision"]}])
    # (2) packet, write scope, read versions, guards, source context
    manifest = None
    if freshness_validator is not None and packet_id is None:
        raise InvalidRequest("task freshness validation requires a version-2 work submission")
    if packet_id is None:
        if command not in PACKETLESS_COMMANDS:
            raise InvalidRequest(f"command {command} requires a packet", code="PACKET_REQUIRED")
        base_revision = db.max_revision()
        targets = [r.ref for r in db.heads("papers")]
    else:
        packet = db.packet(packet_id)
        if packet is None:
            raise InvalidRequest(f"unknown packet {packet_id}", code="PACKET_UNKNOWN")
        manifest = packet["manifest"]
        if command == "work_primary" and (packet["packet_version"] != 2 or not manifest.get("work")):
            raise InvalidRequest("work_primary requires a version-2 work assignment", code="WORK_PACKET_REQUIRED")
        if manifest.get("work") is not None and command in ("apply", "compare"):
            raise InvalidRequest("prepared work is submitted through work submit", code="WORK_SUBMIT_REQUIRED")
        modes = COMMAND_MODES.get(command)
        if modes and manifest["mode"] not in modes:
            raise InvalidRequest(f"command {command} needs a packet in mode {list(modes)}; "
                                 f"{packet_id} is a {manifest['mode']} packet", code="PACKET_MODE")
        base_revision = manifest["base_revision"]
        targets = manifest["targets"]
    conflicts = []
    plan = _plan(db, edits, base_revision, conflicts)
    if manifest is not None:
        if freshness_validator is not None:
            if (packet["packet_version"] != 2 or not manifest.get("work")
                    or command not in ("work_primary", "review_submit", "reconcile")):
                raise InvalidRequest("task freshness validation requires a version-2 work submission")
            validated = freshness_validator(db, json.loads(json.dumps(manifest)), plan)
            if not isinstance(validated, dict) or {k: v for k, v in validated.items()
                                                  if k not in ("read_set", "membership_guards")} != {
                    k: v for k, v in manifest.items() if k not in ("read_set", "membership_guards")}:
                raise InvalidRequest("task freshness validator changed packet identity or permissions")
            if not isinstance(validated.get("read_set"), list) or not isinstance(validated.get("membership_guards"), list):
                raise InvalidRequest("task freshness validator omitted read set or membership guards")
            manifest = validated
        scope = {(r["collection"], r["id"]) for r in manifest["write_scope"]}
        outside = [f"edits/{p.index}: {p.collection}:{p.id} is not in the packet's write scope"
                   for p in plan if p.op != "create" and p.key not in scope and p.key not in scope_exempt]
        if outside:
            raise InvalidRequest("writes outside the packet's write scope", code="WRITE_SCOPE", records=outside)
        _read_set_conflicts(db, manifest, conflicts)
        # Work submissions never gain a raw guard bypass; the validator must explicitly
        # validate task semantics and supply the remaining current guard expectations.
        changed_relations = _guard_changes(db, manifest) if check_guards or manifest.get("work") else []
        source_changed = manifest["source_context_digest"] != source_context_digest(db)
    else:
        changed_relations, source_changed = [], False
    if conflicts or changed_relations or source_changed:
        raise ConflictError("the packet's inputs changed; request a replacement packet and rebase the batch",
                            records=[{"changed": conflicts, "changed_relations": changed_relations,
                                      "source_context_changed": source_changed}],
                            retry=_retry_command(manifest) if manifest else None)
    # Mapping adds canonical identities to an earlier independent response. A fresh
    # coordinator packet must not replace the source inputs that worker actually read.
    # Validate those earlier packets under this same transaction, before saving checks.
    for required_id in dict.fromkeys(required_packet_ids):
        required = db.packet(required_id)
        if required is None:
            raise InvalidRequest(f"unknown required packet {required_id}", code="PACKET_UNKNOWN")
        original = required["manifest"]
        original_conflicts = []
        _read_set_conflicts(db, original, original_conflicts)
        original_relations = _guard_changes(db, original)
        original_source_changed = original["source_context_digest"] != source_context_digest(db)
        if original_conflicts or original_relations or original_source_changed:
            raise ConflictError("the independent worker's inputs changed; obtain a fresh review before mapping",
                                records=[{"packet_id": required_id, "changed": original_conflicts,
                                          "changed_relations": original_relations,
                                          "source_context_changed": original_source_changed}],
                                retry=_retry_command(original))
    # (4) blobs (content addressed, rolled back with the transaction), shapes, prospective linked state
    for data in blobs:
        db.put_blob(data)
    errors = validate_plan(db, plan, command=command, targets=targets,
                           work=manifest.get("work") if manifest else None,
                           context_refs=packet["manifest"]["read_set"] if manifest else None)
    if errors:
        raise InvalidRequest("batch failed validation", code="INVALID_BATCH", records=errors)
    parent = db.max_revision()
    revision = parent + 1
    rebased_from = base_revision if parent > base_revision else None
    # (5) receipt and commit row first
    changed = []
    for p in plan:
        entry = {"collection": p.collection, "id": p.id, "version": p.version, "op": p.op}
        if p.op == "retire":
            entry["reason"] = p.reason
        entry.update(annotations.get(p.key, {}))
        changed.append(entry)
    receipt = {"request_id": request_id, "revision": revision, "rebased_from": rebased_from,
               "changed": changed, "warnings": list(warnings)}
    receipt.update(receipt_context)
    db.insert_commit(revision=revision, parent_revision=parent, base_revision=base_revision,
                     request_id=request_id, request_digest=request_digest, receipt=receipt)
    # (6) versions, heads, refs, facets, bindings
    for p in plan:
        db.insert_version(p.collection, p.id, p.version, revision, p.body)
        db.set_head(p.collection, p.id, p.version)
    state = State(db, plan)
    for p in plan:
        if p.body is None:
            continue
        db.insert_refs(p.collection, p.id, p.version, extract_refs(p.collection, p.body))
        db.insert_facets(p.collection, p.id, p.version, facet_digests(p.collection, p.body))
        if p.collection in BOUND_COLLECTIONS:
            bindings = compute_bindings(state, p.collection, p.body, packet=packet if manifest else None)
            db.insert_binding(p.collection, p.id, p.version, packet_id, bindings)
    return receipt


def apply_batch(db: Database, batch: dict, *, command: str = "apply") -> dict:
    """Validate a generic edit envelope and accept it (implementation-handoff 4.2)."""
    errors = validate_shape(BATCH, batch)
    if errors:
        raise InvalidRequest("invalid edit envelope", records=errors)
    if command == "apply":
        for index, edit in enumerate(batch["edits"]):
            body = edit.get("body")
            if edit["collection"] == "checks" and isinstance(body, dict):
                if body.get("role") == "independent":
                    errors.append(f"edits/{index}: independent checks enter through review submit, not apply")
                if body.get("response_id") is not None:
                    errors.append(f"edits/{index}: checks with a response_id enter through review submit")
        if errors:
            raise InvalidRequest("batch rejected", records=errors)
    return accept(db, request_id=batch["request_id"], request_digest=digest(batch), packet_id=batch["packet_id"],
                  edits=batch["edits"], command=command)


__all__ = ["PACKETLESS_COMMANDS", "accept", "accept_in_transaction", "apply_batch"]
