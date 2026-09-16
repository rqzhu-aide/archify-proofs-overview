"""Bounded coordinator tools. No model calls, persistent task ledger or automatic retries."""
from __future__ import annotations

import base64
import json
from pathlib import Path

from .acceptance import accept_in_transaction
from .bindings import task_binding_changes
from .canonical import canonical_bytes, digest, load_json_bytes, sha256_bytes
from .contract import (BATCH, CHECK_TARGETS, WORK_PRIMARY_RESPONSE, WORK_SUBMISSION, WORKER_RESPONSE,
                       extract_refs, validate_body, validate_shape)
from .errors import CoreError, ConflictError, InvalidRequest
from .ids import new_id, valid_id
from .packets import load_packet, prepare_assignment, source_context_digest
from .review import _check_body, plan_review_submission
from .refs import facet_digests
from .storage import Database
from .validation import State
from .work import derive_work, list_work, select_assignment

ENVELOPE_LIMIT = 65536
RESPONSE_LIMIT = 2097152
DIAGNOSTIC_LIMIT = 100


def read_bounded(path, limit):
    try:
        with Path(path).open("rb") as stream:
            data = stream.read(limit + 1)
    except OSError as exc:
        raise InvalidRequest(f"cannot read input {path}: {exc}", code="FILE_UNREADABLE") from exc
    if len(data) > limit:
        raise InvalidRequest(f"input exceeds {limit} bytes", code="INPUT_TOO_LARGE")
    return data


def _shape(schema, value, what):
    errors = validate_shape(schema, value)
    if errors:
        raise InvalidRequest(f"invalid {what}", records=errors[:DIAGNOSTIC_LIMIT])


def _parse(raw, what):
    try:
        return load_json_bytes(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise InvalidRequest(f"{what} is not valid JSON: {exc}", code="JSON_INVALID") from exc


def _key(ref):
    return ref["collection"], ref["id"]


def _stored_result(row):
    value = row.get("result") if isinstance(row, dict) else None
    if value is not None:
        return value
    return json.loads(row["result_json"])


def _packet(db, packet_id):
    row = db.packet(packet_id)
    if row is None:
        raise InvalidRequest(f"unknown packet {packet_id}", code="PACKET_UNKNOWN")
    manifest = row["manifest"]
    if manifest.get("packet_version") != 2 or not isinstance(manifest.get("work"), dict):
        raise InvalidRequest("work submit needs a version-2 work assignment", code="WORK_PACKET_REQUIRED")
    return row


def _provenance(db, envelope, manifest):
    work = manifest["work"]
    audit = db.head("audits", work["audit_id"])
    if audit is None or audit.retired:
        raise InvalidRequest("the assignment audit is not live", code="AUDIT_UNKNOWN")
    mode = work["mode"]
    if mode != manifest["mode"] or mode not in ("primary", "independent", "reconcile"):
        raise InvalidRequest("assignment mode is inconsistent", code="PACKET_MODE")
    if mode == "independent":
        qid = envelope["qualification_id"]
        qualification = db.head("qualifications", qid) if qid else None
        if qualification is None or qualification.retired:
            raise InvalidRequest("independent work needs a live qualification", code="QUALIFICATION_INVALID")
        body = qualification.body
        if (validate_body("qualifications", body) or not body["qualified"]
                or body["reviewer"] != envelope["reviewer"]
                or body["protocol_version"] != audit.body["protocol_version"]):
            raise InvalidRequest("reviewer qualification does not match this assignment",
                                 code="QUALIFICATION_INVALID")
        if envelope["exposure"] not in ("source_only", "compromised"):
            raise InvalidRequest("independent work needs coordinator exposure provenance")
        if envelope["exposure"] == "compromised" and not envelope["exposure_note"].strip():
            raise InvalidRequest("compromised exposure needs a note")
    elif envelope["qualification_id"] is not None or envelope["exposure"] is not None:
        raise InvalidRequest("primary/reconcile work has null qualification and exposure fields")
    return audit


def _task_map(manifest):
    return {task["id"]: task for task in manifest["work"]["tasks"]}


def _pinned_in(ref, manifest):
    return ref in manifest["read_set"]


def _reference(ref, manifest, *, pinned=False, where="reference"):
    allowed = manifest["read_set"]
    if not any(_key(r) == _key(ref) and (not pinned or r["version"] == ref["version"]) for r in allowed):
        raise InvalidRequest(f"{where} is outside the supplied context", code="WRITE_SCOPE", records=[ref])


def _edit(collection, body, replaces=None):
    return {"op": "replace" if replaces else "create", "collection": collection,
            "id": replaces["id"] if replaces else new_id(collection),
            "expected_version": replaces["version"] if replaces else None, "body": body}


def _pin_edit(edit):
    return {"collection": edit["collection"], "id": edit["id"],
            "version": (edit["expected_version"] or 0) + 1}


def _primary_plan(db, envelope, manifest, active, worker):
    _shape(WORK_PRIMARY_RESPONSE, worker, "primary response")
    if not any(worker[field] for field in ("results", "coverage", "findings")):
        raise InvalidRequest("response proposes no work", code="NO_WORK")
    original_tasks, active_tasks = _task_map(manifest), _task_map(active)
    audit = db.head("audits", manifest["work"]["audit_id"])
    edits, record_map, selected, evidence, auxiliary = [], {}, [], {}, []
    arguments = set()
    for task in active_tasks.values():
        arg = task.get("argument")
        if arg:
            arguments.add(arg["id"])
        if task["target"]["collection"] == "arguments":
            arguments.add(task["target"]["id"])
    context_arg = active["work"].get("context", {}).get("argument")
    if context_arg:
        arguments.add(context_arg["id"])

    for index, result in enumerate(worker["results"]):
        tid = result["task_id"]
        if tid in record_map or tid not in original_tasks or tid not in active_tasks:
            raise InvalidRequest(f"results/{index}/task_id is duplicate or unassigned", code="TASK_SCOPE")
        task, current = original_tasks[tid], active_tasks[tid]
        if any(task[k] != current[k] for k in ("target", "kind", "role", "action")):
            raise ConflictError("task identity changed", records=[{"task_id": tid}])
        if task["role"] != "primary":
            raise InvalidRequest("primary response cannot submit another role", code="TASK_SCOPE")
        for anchor in result["evidence_refs"]:
            _reference({"collection": "anchors", "id": anchor}, manifest, where=f"results/{index}/evidence_refs")
        selected.append(tid)
        evidence[tid] = result["evidence_refs"]
        if result["type"] == "source_fidelity":
            if task["action"] != "compare_source":
                raise InvalidRequest(f"results/{index}: source comparison does not match the task")
            body = {"target": task["target"], "reviewer": envelope["reviewer"],
                    **{k: result[k] for k in ("result", "note", "evidence_refs")}}
            edit = _edit("observations", body)
        else:
            if task["action"] != "check":
                raise InvalidRequest(f"results/{index}: check does not match the task")
            replacement = result["replaces"]
            if replacement:
                if replacement not in current.get("draft_refs", []):
                    raise InvalidRequest(f"results/{index}/replaces is not an authorized draft", code="WRITE_SCOPE")
                prior = db.head("checks", replacement["id"])
                if (prior is None or prior.retired or prior.version != replacement["version"]):
                    raise ConflictError("draft changed", records=[replacement])
                if (prior.body["reviewer"] != envelope["reviewer"] or prior.body["state"] != "draft"
                        or prior.body["target"] != task["target"] or prior.body["kind"] != task["kind"]
                        or prior.body["audit_id"] != audit.id or prior.body["role"] != "primary"):
                    raise InvalidRequest("only this reviewer's same-task draft may be replaced", code="WRITE_SCOPE")
                if prior.body["supersedes"] != result["supersedes"]:
                    raise InvalidRequest("resuming a draft preserves its supersedes reference")
            if result["supersedes"]:
                _reference(result["supersedes"], manifest, pinned=True, where="supersedes")
            body = {k: result[k] for k in ("state", "outcome", "reasoning", "evidence_refs",
                                          "conditions", "next_action", "supersedes")}
            body.update(audit_id=audit.id, target=task["target"], kind=task["kind"], role="primary",
                        reviewer=envelope["reviewer"], protocol_version=audit.body["protocol_version"],
                        response_id=None)
            edit = _edit("checks", body, replacement)
        errors = validate_body(edit["collection"], body)
        if errors:
            raise InvalidRequest("invalid included result", records=[f"results/{index}: {e}" for e in errors])
        edits.append(edit)
        record_map[tid] = _pin_edit(edit)

    def linked_checks(task_ids, refs, where):
        linked = []
        for tid in task_ids:
            pin = record_map.get(tid)
            if pin is None or pin["collection"] != "checks":
                raise InvalidRequest(f"{where}: task {tid} has no check in this response")
            linked.append(pin)
        for ref in refs:
            _reference(ref, manifest, pinned=True, where=where)
            auxiliary.append(ref)
            if ref not in linked:
                linked.append(ref)
        return linked

    for index, row in enumerate(worker["coverage"]):
        if row["argument_id"] not in arguments:
            raise InvalidRequest(f"coverage/{index}: argument is not assigned", code="WRITE_SCOPE")
        _reference({"collection": "anchors", "id": row["anchor_id"]}, manifest, where="coverage anchor")
        auxiliary.append({"collection": "anchors", "id": row["anchor_id"]})
        for ref in row["claim_refs"]:
            _reference(ref, manifest, where="coverage claim")
            auxiliary.append(ref)
        replacement = row["replaces"]
        if replacement:
            _reference(replacement, active, pinned=True, where="coverage replaces")
            prior = db.head("coverage", replacement["id"])
            if prior is None or prior.retired or prior.version != replacement["version"]:
                raise ConflictError("coverage changed", records=[replacement])
            if prior.body["argument_id"] != row["argument_id"]:
                raise InvalidRequest("coverage replacement keeps its argument")
        body = {k: row[k] for k in ("argument_id", "anchor_id", "start_offset", "end_offset",
                                   "classification", "claim_refs", "note")}
        body["check_ids"] = [r["id"] for r in linked_checks(row["check_task_ids"], row["existing_check_refs"],
                                                         f"coverage/{index}")]
        edits.append(_edit("coverage", body, replacement))
    for index, row in enumerate(worker["findings"]):
        _reference(row["target"], manifest, where=f"findings/{index}/target")
        auxiliary.append(row["target"])
        for aid in row["evidence_refs"]:
            _reference({"collection": "anchors", "id": aid}, manifest, where="finding evidence")
            auxiliary.append({"collection": "anchors", "id": aid})
        for uid in row["affected_uses"]:
            _reference({"collection": "uses", "id": uid}, manifest, where="affected use")
            auxiliary.append({"collection": "uses", "id": uid})
        body = {k: row[k] for k in ("target", "category", "description", "evidence_refs",
                                   "affected_uses", "impact_reason")}
        body.update(audit_id=audit.id, lifecycle="open", resolution=None,
                    check_refs=linked_checks(row["related_task_ids"], row["existing_check_refs"],
                                             f"findings/{index}"))
        edits.append(_edit("findings", body))
    # Coverage/findings without new results still consume the assigned local context.
    consumed = selected or list(original_tasks.keys() & active_tasks.keys())
    return {"edits": edits, "record_map": record_map, "task_ids": consumed, "evidence": evidence,
            "auxiliary": auxiliary, "command": "work_primary", "state": "accepted",
            "annotations": {}, "warnings": [], "blobs": []}


def _review_plan(db, envelope, manifest, worker, raw):
    _shape(WORKER_RESPONSE, worker, "independent response")
    auxiliary = []
    supplied = {_key(ref) for ref in manifest["read_set"]}
    for index, judgment in enumerate(worker["judgments"]):
        target = judgment["target"]
        if "source_anchor_id" in target:
            source_ref = {"collection": "anchors", "id": target["source_anchor_id"]}
            _reference(source_ref, manifest, where=f"judgments/{index}/target")
            auxiliary.append(source_ref)
            target = {"collection": CHECK_TARGETS[judgment["kind"]][0], "id": "pending"}
        elif _key(target) in supplied:
            auxiliary.append(target)
        # Unknown targets/evidence remain pending in the shared review mapper.
        # Every explicitly consumed supplied input must retain its original meaning.
        auxiliary.extend({"collection": "anchors", "id": aid} for aid in judgment["evidence_refs"]
                         if ("anchors", aid) in supplied)
        body = _check_body(manifest["work"]["audit_id"], "unused", envelope["reviewer"], judgment,
                           target, "pending")
        errors = validate_body("checks", body)
        if errors:
            raise InvalidRequest("malformed independent judgment",
                                 records=[f"judgments/{index}: {e}" for e in errors])
    request = {k: envelope[k] for k in ("contract_version", "request_id", "packet_id", "reviewer",
                                      "qualification_id", "exposure", "exposure_note")}
    plan = plan_review_submission(db, submission=request, response_bytes=raw)
    plan.update(command="review_submit", task_ids=list(_task_map(manifest)), evidence={},
                auxiliary=auxiliary, record_map={})
    return plan


def _reconcile_plan(db, envelope, manifest, worker):
    _shape(BATCH, worker, "reconciliation batch")
    if worker["request_id"] != envelope["request_id"]:
        raise InvalidRequest("reconciliation request identity disagrees with envelope")
    tasks = _task_map(manifest)
    targets = {_key(t["target"]) for t in tasks.values()}
    for task in tasks.values():
        for entry in task["consumed_inputs"]:
            ref = entry["ref"]
            if ref["collection"] == "checks":
                check = db.version("checks", ref["id"], ref["version"])
                if check is not None and not check.retired:
                    targets.add(_key(check.body["target"]))
    if not worker["edits"]:
        raise InvalidRequest("response proposes no work", code="NO_WORK")
    auxiliary = []
    produced = {(edit["collection"], edit["id"]): (edit["expected_version"] or 0) + 1
                for edit in worker["edits"] if edit["op"] != "retire"}
    for i, edit in enumerate(worker["edits"]):
        body = edit.get("body") or {}
        if (edit["collection"] not in ("checks", "findings", "reconciliations")
                or _key(body.get("target", {"collection": "", "id": ""})) not in targets
                or body.get("audit_id") != manifest["work"]["audit_id"]):
            raise InvalidRequest(f"edits/{i}: outside the reconciliation assignment", code="TASK_SCOPE")
        errors = validate_body(edit["collection"], body)
        if errors:
            raise InvalidRequest("malformed reconciliation edit", records=errors)
        for row in extract_refs(edit["collection"], body):
            ref = {"collection": row["target_collection"], "id": row["target_id"]}
            if row["target_version"] is not None:
                ref["version"] = row["target_version"]
            if ref["collection"] == "audits":
                continue  # Audit semantics are compared separately, allowing report-path edits.
            if _key(ref) in produced and ("version" not in ref or ref["version"] == produced[_key(ref)]):
                continue  # Same-response successors are validated by the acceptance transaction.
            auxiliary.append(ref)
    return {"edits": worker["edits"], "record_map": {}, "task_ids": list(tasks), "evidence": {},
            "auxiliary": auxiliary, "command": "reconcile", "state": "accepted",
            "annotations": {}, "warnings": [], "blobs": []}


def _freshness(original, active, plan):
    def check(db, manifest, _planned):
        if original["source_context_digest"] != source_context_digest(db):
            raise ConflictError("source context changed; review sources before reusing this response")
        old_tasks, fresh_tasks = _task_map(original), _task_map(active)
        state = State(db, [])
        audit_ref = original["work"].get("audit_ref") or next(
            (r for r in original["read_set"] if r["collection"] == "audits"
             and r["id"] == original["work"]["audit_id"]), None)
        if audit_ref is None:
            raise InvalidRequest("assignment lacks pinned audit provenance", code="WORK_PACKET_REQUIRED")
        old_audit = db.version("audits", audit_ref["id"], audit_ref["version"])
        live_audit = db.head("audits", audit_ref["id"])
        if (old_audit is None or live_audit is None or live_audit.retired
                or {k: v for k, v in old_audit.body.items() if k != "report_path"}
                != {k: v for k, v in live_audit.body.items() if k != "report_path"}):
            raise ConflictError("audit scope or checking protocol changed")
        for tid in plan["task_ids"]:
            if tid not in old_tasks or tid not in fresh_tasks:
                raise InvalidRequest("the acceptance packet does not authorize this task", code="TASK_SCOPE")
            task, fresh = old_tasks[tid], fresh_tasks[tid]
            if any(task[k] != fresh[k] for k in ("target", "kind", "role", "action")):
                raise ConflictError("assignment task changed", records=[{"task_id": tid}])
            changes = task_binding_changes(state, task, evidence_refs=plan["evidence"].get(tid, ()),
                                           packet=original)
            if changes["records"] or changes["relations"]:
                raise ConflictError("consumed mathematical inputs changed", records=[{"task_id": tid, **changes}])
            current = task_binding_changes(state, fresh, packet=active)
            if current["records"] or current["relations"]:
                raise ConflictError("acceptance context changed", records=[{"task_id": tid, **current}])
        original_pins = {_key(r): r for r in original["read_set"]}
        for ref in plan.get("auxiliary", []):
            _reference(ref, active, where="auxiliary evidence acceptance context")
            pin = original_pins.get(_key(ref))
            before = db.version(ref["collection"], ref["id"], pin["version"]) if pin else None
            now = db.head(ref["collection"], ref["id"])
            facet = {"items": "statement", "parts": "statement", "uses": "application",
                     "groups": "inference", "arguments": "proof", "scopes": "scope"}.get(ref["collection"], "full")
            if (before is None or now is None or now.retired
                    or facet_digests(ref["collection"], before.body)[facet]
                    != facet_digests(ref["collection"], now.body)[facet]
                    or ("version" in ref and now.version != ref["version"])):
                raise ConflictError("auxiliary evidence changed", records=[ref])
        # The explicit task comparison above replaces whole-assignment version guards.
        # The acceptance kernel still checks write versions and the original source digest.
        return {**manifest, "read_set": [], "membership_guards": []}
    return check


def _failure(exc, *, request_id=None, stored=False):
    return {"request_id": request_id, "stored": stored,
            "state": "conflict" if isinstance(exc, ConflictError) else "needs_revision",
            "committed_revision": None, "receipt": None, "record_map": {}, "remaining_task_ids": [],
            "diagnostics": exc.records[:DIAGNOSTIC_LIMIT] or [exc.message],
            "next_actions": ["inspect retained input and prepare a corrected/new request" if stored
                             else "correct setup or bounded input"],
            "exit_code": exc.exit_code, **exc.to_json()}


def submit_work(db, *, envelope_bytes: bytes, response_bytes: bytes) -> dict:
    """Retain input, then atomically register this explicit subset. Never dispatch or retry."""
    envelope = None
    try:
        if not db.write:
            raise InvalidRequest("work submission needs a writable database")
        for name, raw, limit in (("envelope", envelope_bytes, ENVELOPE_LIMIT),
                                 ("response", response_bytes, RESPONSE_LIMIT)):
            if not isinstance(raw, bytes) or len(raw) > limit:
                raise InvalidRequest(f"{name} must be bytes within {limit} bytes", code="INPUT_TOO_LARGE")
        envelope = _parse(envelope_bytes, "envelope")
        _shape(WORK_SUBMISSION, envelope, "coordinator envelope")
        if not valid_id(envelope["request_id"]):
            raise InvalidRequest("request_id must be a valid identifier")
        request_id = envelope["request_id"]
        request_digest = digest({"command": "work submit", "envelope": envelope,
                                 "response_sha256": sha256_bytes(response_bytes)})
        db.begin_immediate()
        try:
            previous = db.work_submission(request_id)
            if previous:
                if previous["request_digest"] != request_digest:
                    raise InvalidRequest("request ID was already used for different input", code="REQUEST_ID_REUSED")
                if previous["state"] != "received":
                    result = _stored_result(previous)
                    db.rollback()
                    return result
            else:
                if db.commit_by_request(request_id) is not None:
                    raise InvalidRequest("request ID belongs to another command", code="REQUEST_ID_REUSED")
                original = _packet(db, envelope["packet_id"])["manifest"]
                _provenance(db, envelope, original)
                if envelope["rebase_packet_id"]:
                    active = _packet(db, envelope["rebase_packet_id"])["manifest"]
                    if (active["work"]["audit_id"], active["mode"]) != (original["work"]["audit_id"], original["mode"]):
                        raise InvalidRequest("rebase packet has another audit or mode", code="PACKET_MODE")
                db.insert_work_submission(request_id=request_id, request_digest=request_digest,
                                          packet_id=envelope["packet_id"], audit_id=original["work"]["audit_id"],
                                          role=original["mode"], envelope_bytes=envelope_bytes,
                                          response_bytes=response_bytes)
            db.commit()
        except BaseException:
            db.rollback()
            raise
    except CoreError as exc:
        return _failure(exc, request_id=envelope.get("request_id") if isinstance(envelope, dict) else None)

    try:
        worker = _parse(response_bytes, "worker response")
        db.begin_immediate()
        try:
            previous = db.work_submission(request_id)
            if previous["state"] != "received":
                result = _stored_result(previous)
                db.rollback()
                return result
            original = _packet(db, envelope["packet_id"])["manifest"]
            active = _packet(db, envelope["rebase_packet_id"] or envelope["packet_id"])["manifest"]
            _provenance(db, envelope, original)
            if not isinstance(worker, dict) or worker.get("packet_id") != envelope["packet_id"]:
                raise InvalidRequest("worker response must name its original packet", code="PACKET_MISMATCH")
            mode = original["mode"]
            if mode == "primary":
                plan = _primary_plan(db, envelope, original, active, worker)
            elif mode == "independent":
                plan = _review_plan(db, envelope, original, worker, response_bytes)
            else:
                plan = _reconcile_plan(db, envelope, original, worker)
            receipt = accept_in_transaction(
                db, request_id=request_id, request_digest=request_digest, packet_id=active["packet_id"],
                edits=plan["edits"], command=plan["command"], blobs=plan["blobs"],
                annotations=plan["annotations"], warnings=plan["warnings"],
                freshness_validator=_freshness(original, active, plan),
                receipt_context={"packet_id": original["packet_id"], "acceptance_packet_id": active["packet_id"]})
            current_work = derive_work(db, audit_id=original["work"]["audit_id"])
            task_states = {t["id"]: t["state"] for t in current_work["tasks"]}
            remaining = [tid for tid in _task_map(original) if task_states.get(tid) != "satisfied"]
            result = {"request_id": request_id, "packet_id": original["packet_id"],
                      "acceptance_packet_id": active["packet_id"], "stored": True, "state": plan["state"],
                      "committed_revision": receipt["revision"], "receipt": receipt,
                      "record_map": plan["record_map"], "remaining_task_ids": sorted(set(remaining)),
                      "diagnostics": plan.get("diagnostics", [])[:DIAGNOSTIC_LIMIT],
                      "next_actions": ["prepare current remaining work"] if remaining else ["inspect next work"],
                      "exit_code": 0}
            if mode == "independent":
                result.update(response_id=plan["response_id"],
                              pending=[{"judgment_index": i, "reason": r} for i, r in plan["pending"]])
            db.finalize_work_submission(request_id, state=result["state"], result=result,
                                        committed_revision=receipt["revision"])
            db.commit()
            return result
        except BaseException:
            db.rollback()
            raise
    except CoreError as exc:
        result = _failure(exc, request_id=request_id, stored=True)
        db.begin_immediate()
        try:
            previous = db.work_submission(request_id)
            if previous["state"] != "received":
                result = _stored_result(previous)
            else:
                db.finalize_work_submission(request_id, state=result["state"], result=result)
            db.commit()
        except BaseException:
            db.rollback()
            raise
        return result
    except Exception as exc:
        # The already committed intake is the recovery authority. Transient errors
        # leave it received; do not invent a terminal mathematical rejection.
        return {"request_id": request_id, "stored": True, "state": "received", "committed_revision": None,
                "receipt": None, "exit_code": 2,
                "error": {"code": "PROCESSING_INTERRUPTED", "message": f"{type(exc).__name__}: {exc}",
                          "records": []},
                "next_actions": ["inspect the request, then explicitly replay the unchanged input"]}


def response_scaffold(manifest):
    mode = manifest["mode"]
    if mode == "independent":
        return {"packet_id": manifest["packet_id"], "covered_targets": manifest["targets"],
                "coverage_note": "", "exposure_report": {"status": "none_known", "note": ""}, "judgments": []}
    if mode == "reconcile":
        return {"contract_version": 3, "request_id": new_id("request"), "packet_id": manifest["packet_id"],
                "edits": []}
    results = []
    for task in manifest["work"]["tasks"]:
        if task["action"] == "compare_source":
            results.append({"type": "source_fidelity", "task_id": task["id"], "result": "needs_attention",
                            "note": "", "evidence_refs": []})
        else:
            results.append({"type": "check", "task_id": task["id"], "state": "draft", "outcome": None,
                            "reasoning": "", "evidence_refs": [], "conditions": [], "next_action": None,
                            "replaces": None, "supersedes": None})
    return {"packet_id": manifest["packet_id"], "results": results, "coverage": [], "findings": []}


def prepare_work(db, *, audit_id, mode, focus=None, task_ids=(), exclude_task_ids=(),
                 max_units=5, max_bytes=131072, allow_provisional=False):
    if mode not in ("primary", "independent", "reconcile"):
        raise InvalidRequest("unknown assignment mode")
    if type(max_bytes) is not int or not 1 <= max_bytes <= 1048576:
        raise InvalidRequest("max_bytes must be between 1 and 1048576")
    view = derive_work(db, audit_id=audit_id, focus=focus)
    selection = select_assignment(view, {"mode": mode, "focus": focus, "task_ids": list(task_ids),
                                        "exclude_task_ids": list(exclude_task_ids), "max_units": max_units,
                                        "allow_provisional": allow_provisional})
    if not selection.get("prepared"):
        return {"revision": view["revision"], "audit_id": audit_id, "mode": mode, **selection}
    prepared = prepare_assignment(db, audit_id=audit_id, mode=mode, selection=selection, max_bytes=max_bytes)
    if prepared.get("prepared"):
        prepared["scaffold"] = response_scaffold(prepared["manifest"])
    return prepared


def _current(db, row):
    current = {"revision": db.max_revision()}
    result = _stored_result(row) if row["state"] != "received" else {}
    if result.get("response_id"):
        record = db.head("responses", result["response_id"])
        current["response"] = None if record is None else {"ref": record.pinned, "state": record.body["state"]}
    current["written_records"] = [
        {"ref": r, "live_version": (head.version if (head := db.head(r["collection"], r["id"])) else None)}
        for r in result.get("receipt", {}).get("changed", [])
    ] if result.get("receipt") else []
    return current


def inspect_work(db, *, request_id=None, packet_id=None, audit_id=None, limit=20, cursor=None):
    if sum(v is not None for v in (request_id, packet_id, audit_id)) != 1:
        raise InvalidRequest("inspect needs exactly one request, packet or audit")
    if request_id:
        row = db.work_submission(request_id)
        if row is None:
            raise InvalidRequest("unknown work submission", code="REQUEST_UNKNOWN")
        return {"request_id": request_id, "state": row["state"],
                "result": _stored_result(row) if row["state"] != "received" else None,
                "current": _current(db, row), "envelope_sha256": row["envelope_sha256"],
                "response_sha256": row["response_sha256"], "packet_id": row["packet_id"]}
    if packet_id:
        row = _packet(db, packet_id)
        return {"packet_id": packet_id, "revision": row["base_revision"], "manifest": row["manifest"],
                "packet": _parse(db.get_blob(row["payload_sha256"]), "stored packet"),
                "scaffold": response_scaffold(row["manifest"]),
                "submissions": db.work_submissions(packet_id=packet_id, limit=20)}
    if type(limit) is not int or not 1 <= limit <= 100:
        raise InvalidRequest("limit must be between 1 and 100")
    if db.head("audits", audit_id) is None:
        raise InvalidRequest("unknown audit", code="AUDIT_UNKNOWN")
    if int(db.metadata["storage_format"]) < 3:
        return {"audit_id": audit_id, "entries": [], "next_cursor": None}
    after = ["", ""]
    if cursor:
        try:
            value = _parse(base64.urlsafe_b64decode(cursor), "history cursor")
        except (ValueError, TypeError) as exc:
            raise InvalidRequest("invalid history cursor") from exc
        if not isinstance(value, dict) or value.get("audit_id") != audit_id:
            raise InvalidRequest("history cursor belongs to another audit")
        after = value.get("after")
        if not isinstance(after, list) or len(after) != 2 or not all(isinstance(x, str) for x in after):
            raise InvalidRequest("invalid history cursor")
    # Indexed packet metadata and intake rows only; never load historical worker blobs.
    rows = db.conn.execute("""
        SELECT 'packet:' || packet_id AS key, packet_id AS id, 'preparation' AS kind,
               base_revision AS revision, mode, created_at AS at, NULL AS state
          FROM packets WHERE json_extract(manifest_json, '$.work.audit_id') = ?
            AND (created_at, 'packet:' || packet_id) > (?, ?)
        UNION ALL
        SELECT 'request:' || request_id, request_id, 'submission', committed_revision, role,
               received_at, state FROM work_submissions WHERE audit_id = ?
            AND (received_at, 'request:' || request_id) > (?, ?)
        ORDER BY at, key LIMIT ?
    """, (audit_id, *after, audit_id, *after, limit + 1))
    values = [([row["at"], row["key"]], dict(row)) for row in rows]
    page = values[:limit]
    next_cursor = (base64.urlsafe_b64encode(canonical_bytes({"audit_id": audit_id, "after": page[-1][0]}))
                   .decode("ascii")) if len(values) > limit else None
    return {"audit_id": audit_id, "entries": [v for _, v in page], "next_cursor": next_cursor}


def write_artifacts(db, result, directory):
    """Write explicit inspection/preparation files without replacing differing files or sources."""
    destination = Path(directory).resolve()
    protected = {db.path.resolve()}
    paper = db.heads("papers")[0]
    source_root = Path(paper.body["source_root"]).resolve()
    protected.update((source_root / s.body["path"]).resolve() for s in db.heads("sources"))
    files = {}
    if "packet" in result:
        files = {"worker-packet.json": canonical_bytes(result["packet"]),
                 "coordinator-manifest.json": canonical_bytes(result["manifest"]),
                 "response-scaffold.json": canonical_bytes(result["scaffold"])}
    elif result.get("request_id"):
        files = {"submission-envelope.json": db.get_blob(result["envelope_sha256"]),
                 "worker-response.json": db.get_blob(result["response_sha256"])}
    for name, data in files.items():
        path = destination / name
        if path.resolve() in protected:
            raise InvalidRequest("output would overwrite a source or database", code="OUTPUT_CONFLICT")
        if path.exists() and (not path.is_file() or path.stat().st_size != len(data) or path.read_bytes() != data):
            raise InvalidRequest(f"output already exists with different content: {path}", code="OUTPUT_CONFLICT")
    destination.mkdir(parents=True, exist_ok=True)
    output = {}
    for name, data in files.items():
        path = destination / name
        if not path.exists():
            with path.open("xb") as stream:
                stream.write(data)
        output[name] = {"path": str(path), "bytes": len(data)}
    return output
