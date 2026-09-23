"""Read-only status queries: validate a snapshot, report status, list changes.

Every function derives its answer from the record versions stored in the
database; nothing here writes. An incomplete mathematical assessment is a
successful status query with ``process_complete: false``, never an error
(implementation-handoff 5). This module never imports the legacy monolith.
"""
from __future__ import annotations

from collections import Counter

from . import CONTRACT_NAME, CONTRACT_VERSION, CORE_VERSION, PROJECTION_VERSION, STORAGE_FORMAT
from .assessment import Snapshot, derive_assessment
from .bindings import BOUND_COLLECTIONS, binding_changes
from .contract import extract_refs, validate_body
from .ids import COLLECTIONS
from .errors import InvalidRequest
from .packets import source_context_digest
from .projection import project, public_assessment
from .storage import Database


def _revision(db: Database, revision) -> int:
    top = db.max_revision()
    if revision is None:
        return top
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1 or revision > top:
        raise InvalidRequest(f"revision {revision!r} is not in 1..{top}", code="REVISION_RANGE")
    return revision


def _ref_text(record) -> str:
    return f"{record.collection}:{record.id}@{record.version}"


def validate_snapshot(db: Database, *, revision=None) -> dict:
    """Check one snapshot: body shapes, reference resolution, coverage, bindings, projection, integrity.

    Returns ``{revision, ok, records, errors, warnings, stale_bindings, projection_problems, integrity}``.
    ``errors`` are contract violations; ``warnings`` (stale bindings, projection problems) do not make
    the snapshot invalid because the assessment already reports them as stale or gray states.
    """
    revision = _revision(db, revision)
    snap = Snapshot(db, revision)
    live = db.records_at(revision)
    errors, warnings = [], []
    counts = Counter(r.collection for r in live)
    violated = set()
    for record in live:
        if record.collection not in COLLECTIONS:
            errors.append(f"{_ref_text(record)}: unknown collection")
            violated.add((record.collection, record.id, record.version))
            continue
        messages = validate_body(record.collection, record.body)
        if messages:
            violated.add((record.collection, record.id, record.version))
        for message in messages:
            errors.append(f"{_ref_text(record)}: {message}")
    for record in live:
        if record.collection not in COLLECTIONS or validate_body(record.collection, record.body):
            continue
        for ref in extract_refs(record.collection, record.body):
            tc, ti, tv = ref["target_collection"], ref["target_id"], ref["target_version"]
            if tv is None:
                if snap.live(tc, ti) is None:
                    historical = record.collection in ('checks','findings','observations','reconciliations','responses','identity_maps','source_reviews')
                    withdrawn = record.collection in ('application_details','connection_refinements') and record.body.get('state') == 'draft'
                    old = db.latest_at(tc, ti, revision)
                    if not (historical or withdrawn) or old is None:
                        errors.append(f"{_ref_text(record)} {ref['field_path']}: no live {tc} record {ti} at revision {revision}")
            elif db.version(tc, ti, tv) is None:
                errors.append(f"{_ref_text(record)} {ref['field_path']}: {tc} record {ti} has no version {tv}")
    papers = snap.all("papers")
    if len(papers) != 1:
        errors.append(f"expected exactly one live paper record, found {len(papers)}")
    # coverage offsets must lie inside the anchored excerpt they describe (record-contract 2)
    for cov in snap.all("coverage"):
        if validate_body("coverage", cov.body):
            continue
        anchor = snap.live("anchors", cov.body["anchor_id"])
        if anchor is not None and cov.body["end_offset"] > len(anchor.body["excerpt"]):
            errors.append(f"{_ref_text(cov)}: end_offset {cov.body['end_offset']} exceeds the excerpt length "
                          f"{len(anchor.body['excerpt'])} of anchor {anchor.id}")
    stale = []
    for collection in BOUND_COLLECTIONS:
        for record in snap.all(collection):
            if (record.collection, record.id, record.version) in violated:
                warnings.append(f"{_ref_text(record)}: binding not compared because the body "
                                "violates the contract")
                continue
            binding = snap.binding(record)
            if binding is None:
                if collection == "checks" and record.body.get("lifecycle") == "draft":
                    continue
                warnings.append(f"{_ref_text(record)}: no stored binding")
                continue
            try:
                drift = binding_changes(snap, binding["bindings"])
            except Exception as exc:  # noqa: BLE001 - a bound record that violates the contract
                warnings.append(f"{_ref_text(record)}: binding not compared "
                                f"({type(exc).__name__}: {exc}); a bound record violates the contract")
                continue
            if drift["records"] or drift["relations"]:
                stale.append({"ref": record.pinned, "records": drift["records"], "relations": drift["relations"]})
    problems = []
    if errors:
        warnings.append("projection skipped because the snapshot has contract violations")
    else:
        try:
            _, report = project(db, revision=revision)
            problems.extend({"audit_id": None, "problem": p} for p in report["problems"])
            for audit in snap.all("audits"):
                _, report = project(db, revision=revision, audit_id=audit.id)
                problems.extend({"audit_id": audit.id, "problem": p} for p in report["problems"])
        except InvalidRequest as exc:
            errors.append(f"projection failed: {exc.message}")
        except Exception as exc:  # noqa: BLE001 - a crash while projecting is itself a validation failure
            errors.append(f"projection crashed: {type(exc).__name__}: {exc}")
    integrity = db.integrity()
    if integrity["foreign_key_violations"]:
        errors.append(f"{len(integrity['foreign_key_violations'])} foreign key violations")
    if integrity["integrity"] != ["ok"]:
        errors.append("sqlite integrity_check did not return ok")
    if stale:
        warnings.append(f"{len(stale)} bound records have stale bindings (their judgments read as stale)")
    return {
        "revision": revision,
        "ok": not errors,
        "records": dict(sorted(counts.items())),
        "errors": errors,
        "warnings": warnings,
        "stale_bindings": stale,
        "projection_problems": problems,
        "integrity": integrity,
    }


def _pick_audit(snap: Snapshot, audit_id):
    if audit_id is not None:
        audit = snap.live("audits", audit_id)
        if audit is None:
            raise InvalidRequest(f"audit {audit_id} is not live at revision {snap.revision}", code="AUDIT_UNKNOWN")
        return audit
    audits = snap.all("audits")
    if not audits:
        return None
    # the audit registered most recently (highest creating revision, then id) is the default
    return sorted(audits, key=lambda a: (a.revision or 0, a.id))[-1]


def status(db: Database, *, audit_id=None, revision=None) -> dict:
    """Progress and presentation state of one snapshot; never fails for an incomplete assessment."""
    revision = _revision(db, revision)
    snap = Snapshot(db, revision)
    audit = _pick_audit(snap, audit_id)
    result = derive_assessment(db, revision=revision, audit_id=None if audit is None else audit.id)
    papers = snap.all("papers")
    paper = papers[0] if len(papers) == 1 else None
    counts = Counter(r.collection for r in db.records_at(revision))
    sources = snap.all("sources")
    limited = [{"id": s.id, "path": s.body["path"], "limitation": s.body["limitation"]}
               for s in sources if s.body.get("limitation")]
    assessments = {key: public_assessment(value) for key, value in sorted(result["assessments"].items())}
    publications = [{"publication_id": p["id"], "revision": p["revision"], "kind": p["kind"], "state": p["state"],
                     "output_path": p["output_path"], "created_at": p["created_at"]}
                    for p in db.publications() if p["revision"] <= revision]
    return {
        "revision": revision,
        "head_revision": db.max_revision(),
        "storage": {"storage_format": int(db.metadata["storage_format"]), "contract_version": CONTRACT_VERSION,
                    "contract": CONTRACT_NAME, "core_version": CORE_VERSION,
                    "projection_version": PROJECTION_VERSION, "metadata": dict(db.metadata)},
        "paper": None if paper is None else {"id": paper.id, "version": paper.version, **paper.body},
        "counts": dict(sorted(counts.items())),
        "sources": {"count": len(sources), "limited": limited,
                    "context_digest": result["context"]["source_context_digest"]},
        "audit": None if audit is None else {"id": audit.id, "version": audit.version, "mode": audit.body["mode"],
                                             "protocol_version": audit.body["protocol_version"],
                                             "targets": audit.body["targets"],
                                             "independent_required": audit.body["independent_required"]},
        "audits": result["audits"],
        "mode": result["mode"],
        "process_complete": result["progress"]["process_complete"],
        "progress": result["progress"],
        "obligations": {"required": [o["id"] for o in result["obligations"] if o["required"]],
                        "unsatisfied": [o["id"] for o in result["obligations"] if o["required"] and not o["satisfied"]]},
        "assessments": assessments,
        "independent": result["independent"],
        "findings": result["findings"],
        "source_limits": result["source_limits"],
        "published_revision": result["published_revision"],
        "publications": publications,
        "context": result["context"],
        "problems": result["problems"],
    }


def changes(db: Database, *, since: int, limit: int = 200, offset: int = 0) -> dict:
    """Record versions committed after ``since`` (paginated) plus source-context and check freshness."""
    top = db.max_revision()
    if isinstance(since, bool) or not isinstance(since, int) or since < 0 or since > top:
        raise InvalidRequest(f"--since must be a revision in 0..{top}", code="REVISION_RANGE")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 5000:
        raise InvalidRequest("limit must be an integer in 1..5000")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise InvalidRequest("offset must be a non-negative integer")
    total = db.count_versions_since(since)
    rows = db.versions_since(since, limit=limit, offset=offset)
    records = []
    for record in rows:
        op = "retire" if record.retired else ("create" if record.version == 1 else "replace")
        records.append({"collection": record.collection, "id": record.id, "version": record.version,
                        "revision": record.revision, "retired": record.retired, "op": op})
    current_digest = source_context_digest(db)
    since_digest = Snapshot(db, since).source_context_digest() if since >= 1 else None
    affected = []
    if top > since:
        head = Snapshot(db, top)
        changed_keys = {(r.collection, r.id) for r in db.versions_since(since, limit=total or 1, offset=0)}
        for check in head.all("checks"):
            binding = head.binding(check)
            if binding is None:
                continue
            drift = binding_changes(head, binding["bindings"])
            reasons = []
            for entry in drift["records"]:
                key = (entry["ref"]["collection"], entry["ref"]["id"])
                reasons.append({"kind": "record", "ref": entry["ref"], "facet": entry["facet"],
                                "changed_since": key in changed_keys})
            for entry in drift["relations"]:
                reasons.append({"kind": "relation", "relation": entry["relation"], "key": entry["key"]})
            if reasons:
                affected.append({"ref": check.pinned, "freshness": "stale", "reasons": reasons})
    return {
        "since": since,
        "revision": top,
        "total": total,
        "limit": limit,
        "offset": offset,
        "returned": len(records),
        "next_offset": offset + len(records) if offset + len(records) < total else None,
        "records": records,
        "source_context": {"changed": since_digest is not None and since_digest != current_digest,
                           "since_digest": since_digest, "current_digest": current_digest},
        "affected_checks": affected,
    }


__all__ = ["changes", "status", "validate_snapshot"]
