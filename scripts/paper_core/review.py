"""Independent review intake, judgment mapping, reconciliation, qualification (record-contract 5.1).

The worker's response bytes are preserved verbatim as a blob before anything
else is derived from them. The tool generates response and check identifiers,
audit and protocol fields, role ``independent``, and bindings; the coordinator
never edits an independent judgment. Unparseable or out-of-scope material is
kept as a ``needs_revision`` response with diagnostics, and ``review map``
later resolves SourceTargets and mis-scoped references to canonical targets
without changing kind, outcome, reasoning, or evidence.
"""
from __future__ import annotations

import base64
import re

from .acceptance import COMMAND_MODES, accept, apply_batch
from .canonical import digest, load_json_bytes, sha256_bytes
from .contract import (BATCH, CHECK_TARGETS, EDIT_CREATE, MAPPING_REQUEST, SUBMISSION, WORKER_RESPONSE, Arr, Const,
                       Hash, Obj, Str, validate_body, validate_shape)
from .errors import InvalidRequest
from .ids import new_id
from .packets import load_packet
from .storage import Database

QUALIFICATION_REQUEST = Obj({"contract_version": Const(3), "request_id": Str(nonempty=True), "edits": Arr(EDIT_CREATE),
                             "blobs": Arr(Obj({"sha256": Hash(), "encoding": Const("base64"), "data": Str()}))})
JUDGMENT_KEY_RE = re.compile(r"^judgment:(\d+)$")


def _judgment_ref(index: int) -> str:
    return f"judgment:{index}"


def _in_scope(target: dict, scope_targets: list, db_or_packet_parts) -> bool:
    if target in scope_targets:
        return True
    if target["collection"] == "parts":
        item_id = db_or_packet_parts.get(target["id"])
        return item_id is not None and {"collection": "items", "id": item_id} in scope_targets
    return False


def classify_judgments(manifest: dict, response: dict, packet_records: dict) -> tuple[list, list]:
    """Split judgments into resolved (index, Ref) and pending (index, reason) per the packet read set."""
    read_set = {(r["collection"], r["id"]) for r in manifest["read_set"]}
    resolved, pending = [], []
    for index, judgment in enumerate(response["judgments"]):
        target = judgment["target"]
        problems = []
        if "source_anchor_id" in target:
            problems.append(f"SourceTarget on anchor {target['source_anchor_id']} awaits mapping: "
                            f"{target['description']}")
        else:
            if target["collection"] not in CHECK_TARGETS[judgment["kind"]]:
                problems.append(f"kind {judgment['kind']} cannot target {target['collection']}")
            if (target["collection"], target["id"]) not in read_set:
                problems.append(f"target {target['collection']}:{target['id']} is not in the packet read set")
        for anchor_id in judgment["evidence_refs"]:
            if ("anchors", anchor_id) not in read_set:
                problems.append(f"evidence anchor {anchor_id} is not in the packet read set")
        if problems:
            pending.append((index, "; ".join(problems)))
        else:
            resolved.append((index, target))
    return resolved, pending


def _check_body(audit_id: str, protocol_version: str, reviewer: str, judgment: dict, target: dict,
                response_id: str) -> dict:
    return {"audit_id": audit_id, "target": target, "kind": judgment["kind"], "role": "independent",
            "reviewer": reviewer, "protocol_version": protocol_version, "state": judgment["state"],
            "outcome": judgment["outcome"], "reasoning": judgment["reasoning"],
            "evidence_refs": list(judgment["evidence_refs"]), "conditions": list(judgment["conditions"]),
            "next_action": judgment["next_action"], "response_id": response_id, "supersedes": judgment["supersedes"]}


def _packet_parts(packet: dict) -> dict:
    return {r["ref"]["id"]: r["body"]["item_id"] for r in packet["records"] if r["ref"]["collection"] == "parts"}


def _target_statement(db: Database, target: dict):
    """Resolve an assessed local record to the statement whose proof contains it."""
    record = db.head(target["collection"], target["id"])
    visited = set()
    while record is not None and not record.retired and record.key not in visited:
        visited.add(record.key)
        if record.collection in ("items", "parts"):
            return record
        if record.collection == "arguments":
            ref = record.body["target"]
        elif record.collection == "groups":
            ref = {"collection": "arguments", "id": record.body["argument_id"]}
        elif record.collection == "uses":
            ref = ({"collection": "groups", "id": record.body["group_id"]}
                   if record.body["group_id"] else record.body["to"])
        else:
            return None
        record = db.head(ref["collection"], ref["id"])
    return None


def _target_in_scope(db: Database, target: dict, scope: list, parts: dict) -> bool:
    statement = _target_statement(db, target)
    visited = set()
    while statement is not None and statement.key not in visited:
        visited.add(statement.key)
        if _in_scope(statement.ref, scope, parts):
            return True
        if statement.collection != "items" or statement.body["kind"] != "intermediate_result":
            return False
        statement = db.head("items", statement.body["owner_id"]) if statement.body["owner_id"] else None
    return False


def _anchors_overlap(left: dict, right: dict) -> bool:
    if (left["source_id"], left["source_version"]) != (right["source_id"], right["source_version"]):
        return False
    a, b = left["locator"], right["locator"]
    if all(type(value) is int for value in (a["start_line"], a["end_line"], b["start_line"], b["end_line"])):
        return max(a["start_line"], b["start_line"]) <= min(a["end_line"], b["end_line"])
    return left["excerpt_sha256"] == right["excerpt_sha256"]


def _audit_for(db: Database, packet: dict):
    scope = packet.get("declared_scope") or {}
    audit = db.head("audits", scope["audit_id"]) if scope.get("audit_id") else None
    if audit is None or audit.retired:
        targets = packet["targets"]
        candidates = [a for a in db.heads("audits")
                      if any(t in a.body["targets"] for t in targets) or a.body["mode"] == "full"]
        if not candidates:
            raise InvalidRequest("no live audit covers the packet targets", code="AUDIT_UNKNOWN")
        audit = sorted(candidates, key=lambda a: a.id)[0]
    return audit, scope


def plan_review_submission(db: Database, *, submission: dict, response_bytes: bytes) -> dict:
    """Derive the existing independent intake edits without owning a transaction."""
    errors = validate_shape(SUBMISSION, submission)
    if errors:
        raise InvalidRequest("invalid submission envelope", records=errors)
    if not isinstance(response_bytes, (bytes, bytearray)):
        raise InvalidRequest("the worker response must be supplied as bytes")
    response_bytes = bytes(response_bytes)
    packet_row = db.packet(submission["packet_id"])
    if packet_row is None:
        raise InvalidRequest(f"unknown packet {submission['packet_id']}", code="PACKET_UNKNOWN")
    manifest = packet_row["manifest"]
    if manifest["mode"] != "independent":
        raise InvalidRequest(f"review submit needs an independent packet; {submission['packet_id']} is a "
                             f"{manifest['mode']} packet", code="PACKET_MODE")
    qualification = db.head("qualifications", submission["qualification_id"])
    if qualification is None or qualification.retired:
        raise InvalidRequest(f"qualification {submission['qualification_id']} is not a live record")
    qualification_errors = validate_body("qualifications", qualification.body)
    if qualification_errors:
        raise InvalidRequest(f"qualification {qualification.id} does not satisfy the calibration policy",
                             records=qualification_errors)
    if qualification.body["reviewer"] != submission["reviewer"]:
        raise InvalidRequest(f"qualification {qualification.id} belongs to reviewer "
                             f"{qualification.body['reviewer']!r}, not {submission['reviewer']!r}")
    if not qualification.body["qualified"]:
        raise InvalidRequest(f"reviewer {submission['reviewer']!r} is not qualified under {qualification.id}")
    packet = load_packet(db, submission["packet_id"])
    audit, scope = _audit_for(db, packet)
    if qualification.body["protocol_version"] != audit.body["protocol_version"]:
        raise InvalidRequest(f"qualification protocol {qualification.body['protocol_version']} does not match audit "
                             f"protocol {audit.body['protocol_version']}", code="PROTOCOL_MISMATCH")
    blob_sha = sha256_bytes(response_bytes)
    request_digest = digest({"submission": submission, "response_sha256": blob_sha})
    diagnostics, worker, resolved, pending = [], None, [], []
    covered, coverage_note, worker_exposure = [], "", {"status": "possible_exposure", "note": "response unreadable"}
    try:
        parsed = load_json_bytes(response_bytes)
    except ValueError as exc:
        diagnostics.append(f"response is not valid JSON: {exc}")
    else:
        shape_errors = validate_shape(WORKER_RESPONSE, parsed)
        if shape_errors:
            diagnostics.extend(f"response shape: {e}" for e in shape_errors)
        else:
            worker = parsed
    if worker is not None:
        if worker["packet_id"] != submission["packet_id"]:
            diagnostics.append(f"worker response names packet {worker['packet_id']} but the submission names "
                               f"{submission['packet_id']}")
        parts = _packet_parts(packet)
        scope_targets = scope.get("targets") or packet["targets"]
        for target in worker["covered_targets"]:
            if not _in_scope(target, scope_targets, parts):
                diagnostics.append(f"covered target {target['collection']}:{target['id']} lies outside the declared "
                                   "scope")
        covered = [t for t in worker["covered_targets"] if _in_scope(t, scope_targets, parts)]
        coverage_note = worker["coverage_note"]
        worker_exposure = worker["exposure_report"]
        if not diagnostics:
            resolved, pending = classify_judgments(manifest, worker, packet)
            scoped = []
            for index, target in resolved:
                if (_target_in_scope(db, target, scope_targets, parts)
                        and _target_in_scope(db, target, covered, parts)):
                    scoped.append((index, target))
                else:
                    pending.append((index, "target is outside the independent review's covered scope; "
                                           "the worker must resubmit"))
            resolved = scoped
            pending.sort(key=lambda entry: entry[0])
    if submission["exposure"] == "source_only" and worker_exposure["status"] == "none_known":
        exposure, exposure_note = "source_only", submission["exposure_note"]
    else:
        exposure = "compromised"
        exposure_note = (f"coordinator: {submission['exposure']}"
                         + (f" ({submission['exposure_note']})" if submission["exposure_note"] else "")
                         + f"; worker: {worker_exposure['status']}"
                         + (f" ({worker_exposure['note']})" if worker_exposure.get("note") else ""))
    state = "accepted" if worker is not None and not diagnostics and not pending else "needs_revision"
    response_id = new_id("responses")
    response_body = {"audit_id": audit.id, "packet_id": submission["packet_id"], "reviewer": submission["reviewer"],
                     "qualification_id": qualification.id, "original_blob": blob_sha, "covered_targets": covered,
                     "coverage_note": coverage_note, "exposure": exposure, "exposure_note": exposure_note,
                     "state": state}
    edits = [{"op": "create", "collection": "responses", "id": response_id, "expected_version": None,
              "body": response_body}]
    annotations, checks = {}, []
    for index, target in resolved:
        judgment = worker["judgments"][index]
        check_id = new_id("checks")
        edits.append({"op": "create", "collection": "checks", "id": check_id, "expected_version": None,
                      "body": _check_body(audit.id, audit.body["protocol_version"], submission["reviewer"], judgment,
                                          target, response_id)})
        annotations[("checks", check_id)] = {"judgment_index": index}
        checks.append({"judgment_index": index, "check_id": check_id})
    warnings = list(diagnostics) + [f"judgment {i}: {reason}" for i, reason in pending]
    return {"request_digest": request_digest, "edits": edits, "blobs": [response_bytes],
            "annotations": annotations, "warnings": warnings, "response_id": response_id,
            "state": state, "exposure": exposure, "pending": pending, "diagnostics": diagnostics}


def submit_review(db: Database, *, submission: dict, response_bytes: bytes) -> dict:
    """Preserve a worker response and accept the shared independent intake plan."""
    planned = plan_review_submission(db, submission=submission, response_bytes=response_bytes)
    response_id, state, exposure = (planned[k] for k in ("response_id", "state", "exposure"))
    pending, diagnostics = planned["pending"], planned["diagnostics"]
    receipt = accept(db, request_id=submission["request_id"], request_digest=planned["request_digest"],
                     packet_id=submission["packet_id"], edits=planned["edits"], command="review_submit",
                     blobs=planned["blobs"], annotations=planned["annotations"], warnings=planned["warnings"])
    # An idempotent replay returns the original receipt; report from it rather than from fresh identifiers.
    response_ids = [c["id"] for c in receipt["changed"] if c["collection"] == "responses"]
    response_id = response_ids[0] if response_ids else response_id
    stored = db.head("responses", response_id)
    check_list = [{"judgment_index": c["judgment_index"], "check_id": c["id"], "version": c["version"]}
                  for c in receipt["changed"] if c["collection"] == "checks"]
    return {"receipt": receipt, "response_id": response_id, "state": stored.body["state"] if stored else state,
            "exposure": stored.body["exposure"] if stored else exposure, "checks": check_list,
            "pending": [{"judgment_index": i, "reason": r} for i, r in pending],
            "diagnostics": diagnostics}


def _mapped_indexes(db: Database, response_id: str) -> set:
    mapped = set()
    for record in db.heads("identity_maps"):
        if record.body["reason"] != "response_mapping" or record.body["response_id"] != response_id:
            continue
        for entry in record.body["entries"]:
            match = JUDGMENT_KEY_RE.match(entry["old"])
            if match and entry["new_refs"]:
                mapped.add(int(match.group(1)))
    return mapped


def map_response(db: Database, *, mapping: dict) -> dict:
    """Map pending judgments of a needs_revision response to canonical targets (record-contract 5.1)."""
    errors = validate_shape(MAPPING_REQUEST, mapping)
    if errors:
        raise InvalidRequest("invalid mapping request", records=errors)
    if not mapping["entries"]:
        raise InvalidRequest("mapping request lists no entries")
    prior = db.commit_by_request(mapping["request_id"])
    if prior is not None:
        if prior["request_digest"] != digest(mapping):
            raise InvalidRequest("mapping request ID was already used for different input", code="REQUEST_ID_REUSED")
        receipt = load_json_bytes(prior["receipt_json"].encode("utf-8"))
        return {"receipt": receipt, "response_id": mapping["response_id"],
                "state": db.head("responses", mapping["response_id"]).body["state"]}
    response = db.head("responses", mapping["response_id"])
    if response is None or response.retired:
        raise InvalidRequest(f"response {mapping['response_id']} is not a live record")
    if response.body["state"] == "accepted":
        raise InvalidRequest(f"response {response.id} is already accepted; mapping is closed",
                             code="RESPONSE_ACCEPTED")
    packet_row = db.packet(mapping["packet_id"])
    if packet_row is None:
        raise InvalidRequest(f"unknown packet {mapping['packet_id']}", code="PACKET_UNKNOWN")
    if packet_row["manifest"]["mode"] not in COMMAND_MODES["review_map"]:
        raise InvalidRequest(f"review map needs a packet in mode {list(COMMAND_MODES['review_map'])}",
                             code="PACKET_MODE")
    raw = db.get_blob(response.body["original_blob"])
    if raw is None:
        raise InvalidRequest(f"response {response.id} original bytes are missing")
    try:
        worker = load_json_bytes(raw)
    except ValueError as exc:
        raise InvalidRequest(f"response {response.id} bytes are not valid JSON; the worker must resubmit: {exc}",
                             code="RESPONSE_UNREADABLE") from exc
    if validate_shape(WORKER_RESPONSE, worker):
        raise InvalidRequest(f"response {response.id} does not follow the worker response shape; the worker must "
                             "resubmit", code="RESPONSE_UNREADABLE")
    original_packet = load_packet(db, response.body["packet_id"])
    original_records = {(r["ref"]["collection"], r["ref"]["id"]): r["body"]
                        for r in original_packet["records"]}
    original_scope = (original_packet.get("declared_scope") or {}).get("targets") or original_packet["targets"]
    original_parts = _packet_parts(original_packet)
    if worker["packet_id"] != response.body["packet_id"] or any(
            not _in_scope(t, original_scope, original_parts) for t in worker["covered_targets"]):
        raise InvalidRequest("the original response's packet or covered scope is invalid; the worker must resubmit",
                             code="RESPONSE_SCOPE")
    mapping_read_set = {(r["collection"], r["id"]) for r in packet_row["manifest"]["read_set"]}
    _, pending = classify_judgments(original_packet["_manifest"], worker, original_packet)
    pending_indexes = {i for i, _ in pending}
    already = _mapped_indexes(db, response.id)
    audit = db.head("audits", response.body["audit_id"])
    if audit is None:
        raise InvalidRequest(f"audit {response.body['audit_id']} is missing")
    qualification = db.head("qualifications", response.body["qualification_id"])
    if (qualification is None or qualification.retired or not qualification.body["qualified"]
            or validate_body("qualifications", qualification.body)
            or qualification.body["reviewer"] != response.body["reviewer"]
            or qualification.body["protocol_version"] != audit.body["protocol_version"]):
        raise InvalidRequest("the original response lacks a valid qualification for this audit; "
                             "obtain a qualified review before mapping", code="QUALIFICATION_INVALID")
    edits, entries, check_list, annotations = [], [], [], {}
    seen = set()
    for index, entry in enumerate(mapping["entries"]):
        j = entry["judgment_index"]
        where = f"entries/{index}"
        if j >= len(worker["judgments"]):
            errors.append(f"{where}: judgment {j} does not exist (response has {len(worker['judgments'])})")
            continue
        if j not in pending_indexes:
            errors.append(f"{where}: judgment {j} was already resolved at submission")
            continue
        if j in already or j in seen:
            errors.append(f"{where}: judgment {j} is already mapped")
            continue
        seen.add(j)
        judgment = worker["judgments"][j]
        target = entry["target"]
        if target["collection"] not in CHECK_TARGETS[judgment["kind"]]:
            errors.append(f"{where}: kind {judgment['kind']} cannot target {target['collection']}")
            continue
        head = db.head(target["collection"], target["id"])
        if head is None or head.retired:
            errors.append(f"{where}: target {target['collection']}:{target['id']} is not a live record")
            continue
        if (target["collection"], target["id"]) not in mapping_read_set:
            errors.append(f"{where}: target {target['collection']}:{target['id']} is not in the mapping packet read set")
            continue
        if not _target_in_scope(db, target, original_scope, original_parts) or not _target_in_scope(
                db, target, worker["covered_targets"], original_parts):
            errors.append(f"{where}: target is outside the original independent review's covered scope")
            continue
        evidence = list(judgment["evidence_refs"])
        if "source_anchor_id" in judgment["target"]:
            evidence.append(judgment["target"]["source_anchor_id"])
        if any(("anchors", anchor_id) not in original_records for anchor_id in evidence):
            errors.append(f"{where}: judgment refers to evidence absent from the original independent packet; "
                          "the worker must review a source extension and resubmit")
            continue
        target_anchor_ids = (head.body.get("evidence_refs") or
                             [p["anchor_id"] for p in head.body.get("passages", [])])
        target_anchors = [db.head("anchors", aid) for aid in target_anchor_ids]
        if not any(anchor is not None and _anchors_overlap(original_records[("anchors", aid)], anchor.body)
                   for aid in evidence for anchor in target_anchors):
            errors.append(f"{where}: mapped target has no source passage overlapping the worker's reviewed evidence")
            continue
        check_id = new_id("checks")
        edits.append({"op": "create", "collection": "checks", "id": check_id, "expected_version": None,
                      "body": _check_body(audit.id, audit.body["protocol_version"], response.body["reviewer"], judgment,
                                          target, response.id)})
        annotations[("checks", check_id)] = {"judgment_index": j}
        entries.append({"old": _judgment_ref(j), "new_refs": [target], "rationale": entry["rationale"]})
        check_list.append({"judgment_index": j, "check_id": check_id})
    if errors:
        raise InvalidRequest("mapping rejected", records=errors)
    map_id = new_id("identity_maps")
    edits.append({"op": "create", "collection": "identity_maps", "id": map_id, "expected_version": None,
                  "body": {"reason": "response_mapping", "source_blob": response.body["original_blob"],
                           "response_id": response.id, "entries": entries, "reviewer": mapping["reviewer"],
                           "note": ""}})
    remaining = pending_indexes - already - seen
    completes = not remaining
    if completes:
        body = dict(response.body)
        body["state"] = "accepted"
        edits.append({"op": "replace", "collection": "responses", "id": response.id,
                      "expected_version": response.version, "body": body})
    receipt = accept(db, request_id=mapping["request_id"], request_digest=digest(mapping),
                     packet_id=mapping["packet_id"], edits=edits, command="review_map", annotations=annotations,
                     scope_exempt={("responses", response.id)}, required_packet_ids=(response.body["packet_id"],))
    stored = db.head("responses", response.id)
    return {"receipt": receipt, "response_id": response.id, "state": stored.body["state"],
            "identity_map_id": map_id,
            "checks": [{"judgment_index": c["judgment_index"], "check_id": c["id"], "version": c["version"]}
                       for c in receipt["changed"] if c["collection"] == "checks"],
            "remaining": sorted(remaining)}


def reconcile(db: Database, *, batch: dict) -> dict:
    """Record reconciliations with their successor checks and finding updates (reconcile packet)."""
    errors = validate_shape(BATCH, batch)
    if errors:
        raise InvalidRequest("invalid edit envelope", records=errors)
    return accept(db, request_id=batch["request_id"], request_digest=digest(batch), packet_id=batch["packet_id"],
                  edits=batch["edits"], command="reconcile")


def compare(db: Database, *, batch: dict) -> dict:
    """Record source-versus-record observations for a packet (compare command)."""
    return apply_batch(db, batch, command="compare")


def record_qualification(db: Database, *, receipt: dict) -> dict:
    """Store a qualification receipt: qualification records plus their evidence blobs (packetless)."""
    errors = validate_shape(QUALIFICATION_REQUEST, receipt)
    if errors:
        raise InvalidRequest("invalid qualification receipt", records=errors)
    blobs = []
    for index, blob in enumerate(receipt["blobs"]):
        try:
            data = base64.b64decode(blob["data"], validate=True)
        except (ValueError, TypeError) as exc:
            raise InvalidRequest(f"blobs/{index}: data is not valid base64") from exc
        if sha256_bytes(data) != blob["sha256"]:
            raise InvalidRequest(f"blobs/{index}: sha256 does not match the decoded data")
        blobs.append(data)
    for index, edit in enumerate(receipt["edits"]):
        if edit["collection"] != "qualifications":
            errors.append(f"edits/{index}: qualification receipts create qualifications only")
    if errors:
        raise InvalidRequest("qualification receipt rejected", records=errors)
    return accept(db, request_id=receipt["request_id"], request_digest=digest(receipt), packet_id=None,
                  edits=receipt["edits"], command="qualification", blobs=blobs)


__all__ = ["QUALIFICATION_REQUEST", "classify_judgments", "compare", "map_response", "reconcile",
           "record_qualification", "submit_review"]
