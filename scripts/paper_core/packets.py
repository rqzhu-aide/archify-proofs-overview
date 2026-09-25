"""Packets: bounded views of the database for one role (implementation-handoff 4.2 and 6).

A packet pins the record versions a worker read, declares the membership
relations its work depends on, and (for writers) the existing records it may
edit. Independent packets contain source material only.
"""
from __future__ import annotations

import copy
import json

from . import PACKET_VERSION, WORK_CONTEXT_EXTENSION_FEATURE
from .bindings import binding_changes, task_binding, task_binding_changes
from .canonical import canonical_bytes, digest
from .contract import CONTEXT_EXTENSION, MAJOR_KINDS, validate_body, validate_shape
from .errors import ConflictError, InvalidRequest, SourceUnavailable
from .ids import new_id
from .refs import RELATIONS, facet_digests, membership_digest, referrers, relation_members, setup_digest
from .storage import Database
from .semantics import application

MODES = ("author", "primary", "independent", "reconcile")
TARGET_COLLECTIONS = ("papers", "items", "parts", "audits")
STRUCTURAL = ("items", "parts", "scopes", "arguments", "groups", "uses", "coverage",
              "target_specs", "application_details", "connection_refinements", "overview_selections",
              "proof_boundaries")
SOURCE_META = ("anchors", "sources", "source_issues", "source_reviews")
ASSESSMENT = ("audits", "checks", "findings", "repairs", "observations", "reconciliations",
              "reuse_decisions", "qualifications")
READ_COLLECTIONS = {
    "author": ("papers",) + STRUCTURAL + SOURCE_META,
    "primary": ("papers",) + STRUCTURAL + SOURCE_META + ASSESSMENT,
    "reconcile": ("papers",) + STRUCTURAL + SOURCE_META + ASSESSMENT + ("responses", "identity_maps"),
    "independent": ("items", "parts", "anchors", "sources", "source_issues"),
}
WRITE_COLLECTIONS = {
    "author": ("papers",) + STRUCTURAL + SOURCE_META,
    "primary": STRUCTURAL + SOURCE_META + ("checks", "findings", "repairs", "audits"),
    "reconcile": SOURCE_META + ("checks", "findings", "responses"),
    "independent": (),
}
ROUTE_REVIEW_COLLECTIONS = ("items", "parts", "scopes", "arguments", "groups", "uses",
                            "target_specs", "application_details", "anchors", "sources", "source_issues")
# Collections and fields an independent packet must never carry (handoff 6).
BLINDED_COLLECTIONS = tuple(c for c in ("papers", "scopes", "arguments", "groups", "uses", "coverage", "checks",
                                        "findings", "repairs", "observations", "reconciliations", "responses",
                                        "reuse_decisions", "identity_maps", "qualifications", "audits",
                                        "source_reviews", "target_specs", "application_details",
                                        "connection_refinements", "overview_selections", "proof_boundaries"))


def source_context_digest(db: Database) -> str:
    # A context digest needs only identity/version/hash metadata, not file bodies.
    rows = [list(row) for row in db.conn.execute("""
        SELECT h.id,h.version,json_extract(v.body_json,'$.blob_sha256')
        FROM record_heads h JOIN record_versions v
          ON v.collection=h.collection AND v.id=h.id AND v.version=h.version
        WHERE h.collection='sources' AND v.retired=0 ORDER BY h.id""")]
    return digest(rows)


def _ref(record):
    return {"collection": record.collection, "id": record.id}


class _Closure:
    def __init__(self, db: Database, mode: str):
        self.db, self.mode = db, mode
        self.read_collections = ROUTE_REVIEW_COLLECTIONS if mode == "route_review" else READ_COLLECTIONS[mode]
        self.records = {}
        self.guards = set()
        self.omitted = []
        self._heads = {}
        self._done = set()
        # Private selection inputs. These are never serialized to a blind worker.
        self.neutral_inputs = {}
        self.neutral_relations = {}

    def once(self, *key) -> bool:
        """True the first time a closure step runs for a key; later calls return False."""
        if key in self._done:
            return False
        self._done.add(key)
        return True

    def heads(self, collection):
        if collection not in self._heads:
            self._heads[collection] = self.db.heads(collection)
        return self._heads[collection]

    def add(self, record):
        if record is None or record.retired or record.collection not in self.read_collections:
            return None
        self.records.setdefault(record.key, record)
        return record

    def record(self, collection, id):
        return self.db.head(collection, id)

    def add_id(self, collection, id):
        return None if id is None else self.add(self.record(collection, id))

    def add_ref(self, ref):
        return None if ref is None else self.add_id(ref["collection"], ref["id"])

    def guard(self, relation, collection, id):
        self.guards.add((relation, collection, id))

    def members(self, relation, collection, id):
        return relation_members(self.db.conn, relation, {"collection": collection, "id": id})

    def pull(self, owner_collections, *lookups):
        """Live heads in ``owner_collections`` that point into the packet through ``lookups``.

        Each lookup is ``(field_path, targets, prefix)``. This is the index-backed replacement for
        reading a whole collection and filtering it in Python: only records that actually refer to
        something already collected have their bodies read (implementation-handoff 10).
        """
        found = set()
        for field_path, targets, prefix in lookups:
            found.update(referrers(self.db.conn, owner_collections, field_path, targets, prefix=prefix))
        return self.db.heads_in({(collection, id) for collection, id, _ in found})

    def anchors(self, ids):
        for anchor_id in ids:
            self.add_id("anchors", anchor_id)

    def scope_chain(self, scope_id):
        while scope_id and self.once("scope", scope_id):
            scope = self.add_id("scopes", scope_id)
            if scope is None:
                break
            for assumption in scope.body["assumptions"]:
                self.supplier(assumption)
            self.anchors(scope.body["evidence_refs"])
            scope_id = scope.body["parent_id"]

    def supplier(self, ref):
        """A consumed statement: the record, its item, passages and scope; never its proofs."""
        record = self.add_ref(ref)
        if record is None or not self.once("supplier", record.collection, record.id):
            return record
        if record.collection == "parts":
            self.supplier({"collection": "items", "id": record.body["item_id"]})
        self.exact_target(record.ref)
        for passage in record.body["passages"]:
            self.add_id("anchors", passage["anchor_id"])
        if record.body.get("scope_id"):
            self.scope_chain(record.body["scope_id"])
        return record

    def exact_target(self, ref):
        if self.mode == "independent" or not self.once("exact_target", ref["collection"], ref["id"]):
            return
        self.guard("target_specs_for_target", ref["collection"], ref["id"])
        for spec in self.pull(("target_specs",), ("/target", [(ref["collection"], ref["id"])], False)):
            self.add(spec)
            self.anchors(spec.body["evidence_refs"])
            self.scope_chain(spec.body["scope_id"])
            if spec.body["statement_ref"]:
                self.add_ref(spec.body["statement_ref"])

    def proof_boundary(self, argument_id):
        for boundary in self.pull(("proof_boundaries",), ("/argument_ids", [("arguments", argument_id)], True)):
            self.add(boundary)
            self.add_ref(boundary.body["source_review_ref"])
            self.anchors(ref["id"] for ref in boundary.body["anchor_refs"])

    def assessments(self, collection, id):
        if self.mode not in ("primary", "reconcile"):
            return
        if self.mode == "reconcile":
            # reconciliation depends on the complete set of assessments for its target
            self.guard("checks_or_findings_for_target", collection, id)
        for owner_collection, owner_id, _ in self.members("checks_or_findings_for_target", collection, id):
            record = self.add_id(owner_collection, owner_id)
            if record is not None:
                self.anchors(record.body["evidence_refs"])

    def use(self, use_id):
        use = self.add_id("uses", use_id)
        if use is None or not self.once("use", use_id):
            return use
        self.supplier(use.body["from"])
        detail = self.add_id("application_details", use_id)
        if detail is not None:
            self.scope_chain(detail.body.get("scope_id"))
        for refinement in self.pull(("connection_refinements",), ("/summary_use_id", [use.key], False)):
            if self.add(refinement) is not None:
                self.add_id("arguments", refinement.body["argument_id"])
                for refined_id in refinement.body["use_ids"]:
                    self.use(refined_id)
        self.anchors(use.body["evidence_refs"])
        self.assessments("uses", use_id)
        return use

    def argument(self, argument_id):
        argument = self.add_id("arguments", argument_id)
        if argument is None or not self.once("argument", argument_id):
            return argument
        self.scope_chain(argument.body["scope_id"])
        self.anchors(argument.body["evidence_refs"])
        self.proof_boundary(argument_id)
        for relation in ("groups_in_argument", "coverage_in_argument", "scopes_in_argument"):
            self.guard(relation, "arguments", argument_id)
        for _, group_id, _ in self.members("groups_in_argument", "arguments", argument_id):
            group = self.add_id("groups", group_id)
            if group is None:
                continue
            self.guard("uses_in_group", "groups", group_id)
            self.scope_chain(group.body["scope_id"])
            for scope_id in group.body["case_scope_ids"] + group.body["discharges"]:
                self.scope_chain(scope_id)
            self.anchors(group.body["evidence_refs"])
            self.supplier(group.body["conclusion"])
            for _, use_id, _ in self.members("uses_in_group", "groups", group_id):
                self.use(use_id)
            self.assessments("groups", group_id)
        for _, scope_id, _ in self.members("scopes_in_argument", "arguments", argument_id):
            self.scope_chain(scope_id)
        for _, coverage_id, _ in self.members("coverage_in_argument", "arguments", argument_id):
            coverage = self.add_id("coverage", coverage_id)
            if coverage is not None:
                self.add_id("anchors", coverage.body["anchor_id"])
                for check_id in coverage.body["check_ids"]:
                    self.add_id("checks", check_id)
        self.assessments("arguments", argument_id)
        return argument

    def statement_closure(self, ref):
        """Everything a worker needs to author or assess proofs of one major result."""
        record = self.db.head(ref["collection"], ref["id"])
        if record is None or record.retired:
            raise InvalidRequest(f"target {ref['collection']}:{ref['id']} is not a live record")
        item = record if record.collection == "items" else self.db.head("items", record.body["item_id"])
        major = item
        if item.body["kind"] not in MAJOR_KINDS and item.body["owner_id"]:
            major = self.db.head("items", item.body["owner_id"]) or item
        statements = [major]
        statements += [p for p in (self.db.head("parts", pid)
                                   for _, pid, _ in self.members("parts_of_item", "items", major.id)) if p]
        statements += self.pull(("items",), ("/owner_id", [("items", major.id)], False))
        if record.key not in {s.key for s in statements}:
            statements.append(record)
        for statement in statements:
            self.add(statement)
            self.exact_target(statement.ref)
            if statement.body.get("scope_id"):
                self.scope_chain(statement.body["scope_id"])
            for passage in statement.body["passages"]:
                self.add_id("anchors", passage["anchor_id"])
            self.guard("incoming_uses", statement.collection, statement.id)
            self.guard("arguments_for_target", statement.collection, statement.id)
            if statement.collection == "items":
                self.guard("parts_of_item", "items", statement.id)
            if self.mode in ("primary", "reconcile"):
                self.guard("checks_or_findings_for_target", statement.collection, statement.id)
            for _, use_id, _ in self.members("incoming_uses", statement.collection, statement.id):
                self.use(use_id)
            for _, argument_id, _ in self.members("arguments_for_target", statement.collection, statement.id):
                self.argument(argument_id)
            self.assessments(statement.collection, statement.id)
        return major

    def audit_closure(self, audit_id):
        audit = self.add_id("audits", audit_id)
        if audit is None:
            raise InvalidRequest(f"audit {audit_id} is not a live record")
        if self.mode in ("primary", "reconcile"):
            self.guard("checks_or_findings_for_target", "audits", audit_id)
            self.assessments("audits", audit_id)
        for target in audit.body["targets"]:
            self.statement_closure(target)
        self.add_id("qualifications", audit.body["qualification_id"])

    def paper_closure(self):
        for collection in self.read_collections:
            for record in self.heads(collection):
                self.add(record)
        for collection in ("items", "parts"):
            for record in self.heads(collection):
                self.guard("incoming_uses", collection, record.id)
                self.guard("arguments_for_target", collection, record.id)
                if collection == "items":
                    self.guard("parts_of_item", "items", record.id)
                if self.mode in ("primary", "reconcile"):
                    self.guard("checks_or_findings_for_target", collection, record.id)
        for record in self.heads("arguments"):
            for relation in ("groups_in_argument", "coverage_in_argument", "scopes_in_argument"):
                self.guard(relation, "arguments", record.id)
        for record in self.heads("groups"):
            self.guard("uses_in_group", "groups", record.id)
        if self.mode in ("primary", "reconcile"):
            for record in self.heads("audits"):
                self.guard("checks_or_findings_for_target", "audits", record.id)

    def finish(self):
        """Dependent records: sources for anchors, open source issues, reviews, repairs, responses.

        Every conditional step here is a reverse lookup through ``record_refs`` rather than a read of
        the whole owning collection, so a bounded packet stays bounded however large the store grows
        (implementation-handoff 10). The unconditional sets below - audits, and responses and
        identity maps while reconciling - are complete by definition, not accidental scans.
        """
        for anchor in [r for r in list(self.records.values()) if r.collection == "anchors"]:
            source = self.add_id("sources", anchor.body["source_id"])
            if source is not None and source.version != anchor.body["source_version"]:
                self.omitted.append({"collection": "anchors", "id": anchor.id,
                                     "reason": f"anchor pins source version {anchor.body['source_version']}; "
                                               f"current version is {source.version}"})
        source_keys = [r.key for r in self.records.values() if r.collection == "sources"]
        anchor_keys = [r.key for r in self.records.values() if r.collection == "anchors"]
        for issue in self.pull(("source_issues",), ("/source_id", source_keys, False),
                               ("/anchor_id", anchor_keys, False)):
            if issue.body["lifecycle"] == "open":
                self.add(issue)
        if "source_reviews" in self.read_collections:
            for review in self.pull(("source_reviews",), ("/source_refs", source_keys, True),
                                    ("/anchor_refs", anchor_keys, True)):
                self.add(review)
        if self.mode in ("primary", "reconcile"):
            # one snapshot of what the closure collected: the loops below add to self.records
            keys = sorted(self.records)
            finding_keys = [k for k in keys if k[0] == "findings"]
            check_keys = [k for k in keys if k[0] == "checks"]
            for repair in self.pull(("repairs",), ("/finding_id", finding_keys, False)):
                self.add(repair)
                self.add_ref(repair.body["supported_form"])
                self.add_id("arguments", repair.body["argument_id"])
            for observation in self.pull(("observations",), ("/target", keys, False)):
                self.add(observation)
            for reconciliation in self.pull(("reconciliations",), ("/target", keys, False)):
                self.add(reconciliation)
                for ref in reconciliation.body["primary_checks"] + reconciliation.body["independent_checks"] \
                        + reconciliation.body["successor_checks"]:
                    self.add_id("checks", ref["id"])
            for decision in self.pull(("reuse_decisions",), ("/check_ref", check_keys, False)):
                self.add(decision)
                self.add_id("source_reviews", decision.body["source_review_id"])
            for audit in self.heads("audits"):
                self.add(audit)
                self.add_id("qualifications", audit.body["qualification_id"])
            if self.mode == "reconcile":
                for response in self.heads("responses"):
                    self.add(response)
                    self.add_id("qualifications", response.body["qualification_id"])
                for identity_map in self.heads("identity_maps"):
                    if identity_map.body["response_id"] is not None:
                        self.add(identity_map)
        for record in list(self.records.values()):
            if record.collection == "papers":
                continue
            if record.collection in ("items", "parts") and record.body.get("scope_id"):
                self.scope_chain(record.body["scope_id"])


def _neutral_bind(closure, record, facet):
    """Retain private setup freshness without disclosing the coordinator's graph."""
    facets = facet_digests(record.collection, record.body)
    closure.neutral_inputs[(record.collection, record.id, facet)] = {
        "ref": record.pinned, "facet": facet, "digest": facets.get(facet, facets["full"])}
    projection = setup_digest(record.collection, record.body)
    if projection is not None:
        closure.neutral_inputs[(record.collection, record.id, facet)]["setup_digest"] = projection


def _neutral_members(closure, relation, ref):
    members = closure.members(relation, ref["collection"], ref["id"])
    closure.neutral_relations[(relation, ref["collection"], ref["id"])] = {
        "relation": relation, "key": dict(ref), "digest": membership_digest(members),
        "members": sorted([[c, i] for c, i, _ in members])}
    return members


def _neutral_scope(closure, scope_id):
    while scope_id and closure.once("neutral_scope", scope_id):
        scope = closure.record("scopes", scope_id)
        if scope is None or scope.retired:
            break
        _neutral_bind(closure, scope, "scope")
        closure.anchors(scope.body["evidence_refs"])
        for assumption in scope.body["assumptions"]:
            _neutral_statement(closure, assumption)
        if (scope.body["conditions"] or scope.body["binders"]) and not scope.body["evidence_refs"]:
            warning = {"reason": "Applicable setup has no captured source passage; request its exact source if needed."}
            if warning not in closure.omitted:
                closure.omitted.append(warning)
        scope_id = scope.body["parent_id"]


def _neutral_statement(closure, ref, *, borrowed=False):
    """Select source statement/setup; ordinary suppliers do not bring their proofs."""
    record = closure.record(ref["collection"], ref["id"])
    if record is None or record.retired or record.collection not in ("items", "parts") \
            or record.body["origin"] != "source" \
            or (record.collection == "items" and record.body["kind"] not in MAJOR_KINDS):
        return None
    closure.add(record)
    if closure.once("neutral_statement", record.collection, record.id):
        _neutral_bind(closure, record, "statement")
        closure.anchors(p["anchor_id"] for p in record.body["passages"]
                        if p["role"] in ("statement", "definition"))
        _neutral_scope(closure, record.body.get("scope_id"))
        for _, spec_id, _ in _neutral_members(closure, "target_specs_for_target", record.ref):
            spec = closure.record("target_specs", spec_id)
            if spec is not None and not spec.retired:
                _neutral_bind(closure, spec, "statement")
                closure.anchors(spec.body["evidence_refs"])
                _neutral_scope(closure, spec.body["scope_id"])
        if record.collection == "parts":
            _neutral_statement(closure, {"collection": "items", "id": record.body["item_id"]})
    if borrowed and closure.once("neutral_borrowed", record.collection, record.id):
        _neutral_bind(closure, record, "proof")
        anchors = [p["anchor_id"] for p in record.body["passages"] if p["role"] in ("proof", "evidence")]
        closure.anchors(anchors)
        for boundary in closure.pull(("proof_boundaries",), ("/target", [record.key], False)):
            closure.anchors(pin["id"] for pin in boundary.body["anchor_refs"])
        if not anchors and record.collection == "parts":
            _neutral_statement(closure, {"collection": "items", "id": record.body["item_id"]}, borrowed=True)
    return record


def _neutral_dependencies(closure, target):
    """Use the graph only to locate source context, never to supply a proof outline."""
    if not closure.once("neutral_dependencies", target["collection"], target["id"]):
        return
    uses = {identity for _, identity, _ in _neutral_members(closure, "incoming_uses", target)}
    for _, argument_id, _ in _neutral_members(closure, "arguments_for_target", target):
        argument = closure.record("arguments", argument_id)
        if argument is None or argument.retired:
            continue
        _neutral_bind(closure, argument, "proof")
        _neutral_scope(closure, argument.body["scope_id"])
        for _, group_id, _ in _neutral_members(closure, "groups_in_argument", argument.ref):
            group = closure.record("groups", group_id)
            if group is None or group.retired:
                continue
            _neutral_bind(closure, group, "inference")
            _neutral_scope(closure, group.body["scope_id"])
            for scope_id in group.body["case_scope_ids"] + group.body["discharges"]:
                _neutral_scope(closure, scope_id)
            uses.update(identity for _, identity, _ in _neutral_members(closure, "uses_in_group", group.ref))
    for use_id in sorted(uses):
        use = closure.record("uses", use_id)
        if use is None or use.retired:
            continue
        _neutral_bind(closure, use, "application")
        _neutral_statement(closure, use.body["from"], borrowed=use.body["type"] == "proof_argument")
        detail = closure.record("application_details", use_id)
        if detail is not None and not detail.retired:
            _neutral_bind(closure, detail, "application")
            _neutral_scope(closure, detail.body.get("scope_id"))


def _independent(db: Database, targets: list, extra_anchor_ids=(), extra_paths=(), *, closure=None, audit_id=None):
    """Source-only packet material for an independent checker (handoff 6)."""
    closure = closure or _Closure(db, "independent")
    audits = [db.head("audits", audit_id)] if audit_id else closure.heads("audits")
    audits = [a for a in audits if a is not None and not a.retired]
    audit = None
    for target in targets:
        record = closure.record(target["collection"], target["id"])
        if record is None or record.retired or target["collection"] not in ("items", "parts"):
            raise InvalidRequest(f"independent targets must be live items or parts: {target}")
        if record.body["origin"] != "source":
            raise InvalidRequest(f"independent review requires a source-origin target; {target['collection']}:"
                                 f"{target['id']} has origin {record.body['origin']}")
        if record.collection == "items" and record.body["kind"] not in MAJOR_KINDS:
            raise InvalidRequest("independent packet failed blinding check",
                                 records=[f"items:{record.id} is an intermediate result"])
        _neutral_statement(closure, target)
        _neutral_dependencies(closure, target)
        for passage in record.body["passages"]:
            closure.add_id("anchors", passage["anchor_id"])
        # Supply neutral complete source segments without the coordinator's
        # boundary verdict, route structure, or exact specification.
        for boundary in closure.pull(("proof_boundaries",), ("/target", [record.key], False)):
            for pin in boundary.body["anchor_refs"]:
                closure.add_id("anchors", pin["id"])
        item = record if record.collection == "items" else closure.add_id("items", record.body["item_id"])
        if item is not None and record.collection == "items":
            closure.guard("parts_of_item", "items", item.id)
            for _, part_id, _ in closure.members("parts_of_item", "items", item.id):
                part = closure.record("parts", part_id)
                if part is None or part.retired:
                    continue
                if part.body["origin"] != "source":
                    closure.omitted.append({"collection": "parts", "id": part.id, "reason": "not source-origin"})
                    continue
                closure.add(part)
                _neutral_statement(closure, part.ref)
                _neutral_dependencies(closure, part.ref)
                for passage in part.body["passages"]:
                    closure.add_id("anchors", passage["anchor_id"])
        elif item is not None:
            # A part packet needs its parent's standing statement/setup, not every
            # sibling part or the parent's full proof. Explicit context extensions
            # can add any additional source actually needed.
            for passage in item.body["passages"]:
                if passage["role"] in ("statement", "definition", "evidence"):
                    closure.add_id("anchors", passage["anchor_id"])
        # Controller selection already derives the audit's effective consumed
        # closure, including prerequisite results and source-origin parts. A
        # generic get still uses its historical direct-target lookup.
        candidates = [a for a in audits if audit_id is not None
                      or any(t == target for t in a.body["targets"]) or a.body["mode"] == "full"]
        if audit is None and candidates:
            audit = sorted(candidates, key=lambda a: a.id)[0]
    if audit is None:
        raise InvalidRequest("independent packets need a registered audit covering the target")
    for anchor_id in extra_anchor_ids:
        if closure.add_id("anchors", anchor_id) is None:
            raise InvalidRequest(f"requested anchor {anchor_id} is not a live record")
    for path in extra_paths:
        matches = [s for s in closure.heads("sources") if s.body["path"] == path]
        if not matches:
            raise InvalidRequest(f"requested source path {path} is not captured")
        for source in matches:
            closure.add(source)
    closure.finish()
    declared_scope = {"audit_id": audit.id, "mode": audit.body["mode"], "targets": list(targets),
                      "exclusions": audit.body["exclusions"], "protocol_version": audit.body["protocol_version"]}
    return closure, declared_scope, audit


# Structural guards over source-derived membership (part lists) reveal no primary work; they let a
# whole-result submission conflict when a theorem part is added (implementation-handoff 4.4).
BLIND_SAFE_GUARDS = ("parts_of_item",)


def _packet_record(record, mode):
    body = record.body
    if mode == "independent" and record.collection == "items" and "proof_idea" in body:
        # This authored explanation is not captured source for a blind review.
        body = {key: value for key, value in body.items() if key != "proof_idea"}
    return {"ref": record.pinned, "body": body}


def blinding_violations(packet: dict) -> list:
    """Return every way a packet payload exposes primary work to an independent worker."""
    problems = []
    for field in ("work", "instructions", "assigned_task_ids", "conditional_on_task_ids", "tasks", "units",
                  "_manifest", "manifest", "historical_records", "supplied_derivations", "source_comparisons"):
        if field in packet:
            problems.append(f"{field} exposes coordinator work")
    for entry in packet.get("records", []):
        collection, body = entry["ref"]["collection"], entry["body"]
        if collection in BLINDED_COLLECTIONS:
            problems.append(f"{collection}:{entry['ref']['id']} is not source material")
        if collection in ("items", "parts") and body.get("origin") != "source":
            problems.append(f"{collection}:{entry['ref']['id']} has origin {body.get('origin')}")
        if collection == "items" and body.get("kind") not in MAJOR_KINDS:
            problems.append(f"items:{entry['ref']['id']} is an intermediate result")
        if collection == "items" and "proof_idea" in body:
            problems.append(f"items:{entry['ref']['id']} exposes an authored proof idea")
        if "report_path" in body:
            problems.append(f"{collection}:{entry['ref']['id']} carries a report path")
    if packet.get("write_scope"):
        problems.append("independent packets carry no write scope")
    for guard in packet.get("membership_guards", []):
        if guard["relation"] not in BLIND_SAFE_GUARDS:
            problems.append(f"membership guard {guard['relation']} exposes primary work")
    return problems


def _historical_statements(closure):
    """Read-only pinned statement content, never a duplicate live identity."""
    found = {}
    for spec in list(closure.records.values()):
        if spec.collection != "target_specs" or spec.body["statement_ref"] is None:
            continue
        pin = spec.body["statement_ref"]
        current = closure.record(pin["collection"], pin["id"])
        if current is not None and current.version == pin["version"]:
            continue
        reader = closure.state if hasattr(closure, "state") else closure.db
        original = reader.version(pin["collection"], pin["id"], pin["version"])
        if original is not None:
            found[(original.collection, original.id, original.version)] = {"ref": original.pinned,
                                                                          "body": original.body}
    return [found[key] for key in sorted(found)]


def _build(db: Database, *, targets: list, mode: str, extra_anchor_ids=(), extra_paths=(), extends=None) -> dict:
    if mode not in MODES:
        raise InvalidRequest(f"unknown packet mode {mode!r}; expected one of {list(MODES)}")
    if not targets:
        raise InvalidRequest("a packet needs at least one target")
    seen = set()
    for target in targets:
        if not isinstance(target, dict) or set(target) != {"collection", "id"} \
                or target["collection"] not in TARGET_COLLECTIONS:
            raise InvalidRequest(f"packet targets are {list(TARGET_COLLECTIONS)} references: {target}")
        if (target["collection"], target["id"]) in seen:
            raise InvalidRequest(f"duplicate packet target {target}")
        seen.add((target["collection"], target["id"]))
    # Optimistic snapshot: all reads below must finish at this same revision.
    # _persist checks under the write lock and rejects any interleaving commit.
    base_revision = db.max_revision()
    declared_scope = None
    if mode == "independent":
        closure, declared_scope, _ = _independent(db, targets, extra_anchor_ids, extra_paths)
    else:
        closure = _Closure(db, mode)
        for target in targets:
            if target["collection"] == "papers":
                paper = db.head("papers", target["id"])
                if paper is None:
                    raise InvalidRequest(f"unknown paper {target['id']}")
                closure.paper_closure()
            elif target["collection"] == "audits":
                if mode == "author":
                    raise InvalidRequest("author packets target papers, items or parts")
                closure.audit_closure(target["id"])
            else:
                closure.statement_closure(target)
        for anchor_id in extra_anchor_ids:
            if closure.add_id("anchors", anchor_id) is None:
                raise InvalidRequest(f"requested anchor {anchor_id} is not a live record")
        for path in extra_paths:
            for source in db.heads("sources"):
                if source.body["path"] == path:
                    closure.add(source)
        closure.finish()
    records = sorted(closure.records.values(), key=lambda r: (r.collection, r.id))
    read_set = [r.pinned for r in records]
    guards = []
    for relation, collection, id in sorted(closure.guards):
        key = {"collection": collection, "id": id}
        members = relation_members(db.conn, relation, key)
        guards.append({"relation": relation, "key": key, "digest": membership_digest(members)})
    write_scope = [r.ref for r in records if r.collection in WRITE_COLLECTIONS[mode]
                   and not (r.collection == "checks" and (r.body["role"] != "primary"
                                                          or r.body["state"] == "complete"))]
    manifest = {"packet_version": PACKET_VERSION, "packet_id": new_id("packet"), "base_revision": base_revision,
                "mode": mode, "targets": list(targets), "read_set": read_set, "membership_guards": guards,
                "source_context_digest": source_context_digest(db), "write_scope": write_scope}
    packet = dict(manifest)
    packet["records"] = [_packet_record(r, mode) for r in records]
    packet["declared_scope"] = declared_scope
    packet["omitted"] = closure.omitted
    packet["extends"] = extends
    packet["truncated"] = False
    historical = _historical_statements(closure)
    if historical:
        packet["historical_records"] = historical
    if mode == "independent":
        problems = blinding_violations(packet)
        if problems:
            raise InvalidRequest("independent packet failed blinding check", records=problems)
    return manifest, packet


def _persist(db: Database, manifest: dict, packet: dict) -> dict:
    payload = canonical_bytes(packet)
    db.begin_immediate()
    try:
        if db.max_revision() != manifest["base_revision"]:
            raise ConflictError("database changed while the packet was being assembled; request it again")
        sha = db.put_blob(payload)
        db.insert_packet(manifest["packet_id"], manifest["base_revision"], manifest["mode"], manifest, sha)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return packet


def get_packet(db: Database, *, targets=(), mode: str = "author", extend=None, request=None) -> dict:
    """Assemble and persist a packet; returns the full packet (manifest plus records)."""
    if not db.write:
        raise InvalidRequest("packets are recorded in the database; open it for writing")
    if extend is None:
        manifest, packet = _build(db, targets=list(targets), mode=mode)
        return _persist(db, manifest, packet)
    prior = db.packet(extend)
    if prior is None:
        raise InvalidRequest(f"unknown packet {extend}")
    if request is None:
        raise InvalidRequest("get --extend needs a context request (targets, source_anchor_ids, source_paths, reason)")
    errors = validate_shape(CONTEXT_EXTENSION, request)
    if errors:
        raise InvalidRequest("invalid context extension request", records=errors)
    prior_manifest = prior["manifest"]
    if "work" in prior_manifest:
        raise InvalidRequest("get --extend cannot extend a work assignment; use work extend --packet "
                             "with pinned source_refs and reason for neutral independent context, or work prepare "
                             "when consumed mathematics changed", code="WORK_PACKET_EXTENSION")
    if prior_manifest["source_context_digest"] != source_context_digest(db):
        raise ConflictError("captured sources changed since the packet was issued; request a fresh packet",
                            records=[{"kind": "source_context", "packet_id": extend}])
    merged = list(prior_manifest["targets"])
    for target in request["targets"]:
        if target not in merged:
            merged.append(target)
    # Retain the source context already delivered, including earlier extensions.
    # Reading the previous immutable payload avoids replaying the extension chain.
    prior_payload = json.loads(db.get_blob(prior["payload_sha256"]).decode("utf-8"))
    anchors = set(request["source_anchor_ids"])
    paths = set(request["source_paths"])
    for entry in prior_payload["records"]:
        if entry["ref"]["collection"] == "anchors":
            anchors.add(entry["ref"]["id"])
        elif entry["ref"]["collection"] == "sources":
            paths.add(entry["body"]["path"])
    manifest, packet = _build(db, targets=merged, mode=prior_manifest["mode"],
                              extra_anchor_ids=sorted(anchors), extra_paths=sorted(paths),
                              extends=extend)
    if manifest["source_context_digest"] != prior_manifest["source_context_digest"]:
        raise ConflictError("captured sources changed since the packet was issued; request a fresh packet",
                            records=[{"kind": "source_context", "packet_id": extend}])
    packet["extension_reason"] = request["reason"]
    return _persist(db, manifest, packet)


def load_packet(db: Database, packet_id: str) -> dict:
    row = db.packet(packet_id)
    if row is None:
        raise InvalidRequest(f"unknown packet {packet_id}")
    payload = db.get_blob(row["payload_sha256"])
    packet = json.loads(payload.decode("utf-8"))
    packet["_manifest"] = row["manifest"]
    return packet


DEFAULT_WORK_BYTES = 131072
MAX_WORK_BYTES = 1048576
MAX_WORK_RECORDS = 2048


class _ContextLimit(Exception):
    def __init__(self, *, records=0, bytes=0, contributors=()):
        self.records, self.bytes, self.contributors = records, bytes, list(contributors)


class _WorkState:
    """Lazy, bounded record view shared by local closure and task binding builders."""

    def __init__(self, db, max_bytes, max_records=MAX_WORK_RECORDS):
        self.db, self.max_bytes, self.max_records = db, max_bytes, max_records
        self.cache, self.relations, self.sizes = {}, {}, {}
        self.body_bytes = 0

    def live(self, collection, id):
        key = collection, id
        if key not in self.cache:
            row = self.db.conn.execute("""
                SELECT v.retired,length(CAST(v.body_json AS BLOB)) FROM record_heads h
                JOIN record_versions v ON v.collection=h.collection AND v.id=h.id AND v.version=h.version
                WHERE h.collection=? AND h.id=?""", key).fetchone()
            if row is None or row[0]:
                self.cache[key] = None
            else:
                size = row[1] or 0
                self.sizes[key] = size
                if len(self.sizes) > self.max_records or self.body_bytes + size > self.max_bytes:
                    raise _ContextLimit(records=len(self.sizes), bytes=self.body_bytes + size,
                                        contributors=self.contributors())
                self.body_bytes += size
                self.cache[key] = self.db.head(collection, id)
        return self.cache[key]

    def version(self, collection, id, version):
        current = self.live(collection, id)
        if current is not None and current.version == version:
            return current
        # Explicit historical judgment references are small, separately bounded bodies.
        row = self.db.conn.execute("""SELECT length(CAST(body_json AS BLOB)) FROM record_versions
            WHERE collection=? AND id=? AND version=?""", (collection, id, version)).fetchone()
        if row is not None and (row[0] or 0) > self.max_bytes:
            raise _ContextLimit(records=len(self.sizes), bytes=row[0], contributors=self.contributors())
        return self.db.version(collection, id, version)

    def contributors(self):
        return [{"record_ref": {"collection": c, "id": i}, "bytes": n}
                for (c, i), n in sorted(self.sizes.items(), key=lambda pair: (-pair[1], pair[0]))[:5]]

    def referrers(self, owners, field, targets, *, prefix=False, audit_id=None):
        found = set()
        owner_marks = ",".join("?" for _ in owners)
        field_sql = "r.field_path>=? AND r.field_path<?" if prefix else "r.field_path=?"
        field_args = (field + "/", field + "0") if prefix else (field,)
        audit_sql = " AND json_extract(v.body_json,'$.audit_id')=?" if audit_id else ""
        audit_args = (audit_id,) if audit_id else ()
        for collection, id in sorted(set(targets)):
            rows = self.db.conn.execute(f"""
                SELECT DISTINCT r.owner_collection,r.owner_id,r.owner_version
                FROM record_refs r INDEXED BY refs_from_field
                JOIN record_heads h ON h.collection=r.owner_collection AND h.id=r.owner_id
                    AND h.version=r.owner_version
                JOIN record_versions v ON v.collection=h.collection AND v.id=h.id AND v.version=h.version
                WHERE r.owner_collection IN ({owner_marks}) AND {field_sql}
                    AND r.target_collection=? AND r.target_id=? AND v.retired=0 {audit_sql}
                LIMIT ?""", (*owners, *field_args, collection, id, *audit_args, self.max_records + 1))
            for row in rows:
                found.add(tuple(row))
                if len(found) > self.max_records:
                    raise _ContextLimit(records=len(found), bytes=self.body_bytes,
                                        contributors=self.contributors())
        return sorted(found)

    def relation_members(self, relation, key):
        cache_key = relation, key["collection"], key["id"]
        if cache_key not in self.relations:
            owners, field, legal = RELATIONS[relation]
            if legal is not None and key["collection"] not in legal:
                raise InvalidRequest(f"{relation} cannot be keyed by {key['collection']}")
            members = self.referrers(owners, field, [(key["collection"], key["id"])])
            if relation == "uses_in_group":
                for _, identity, _ in self.referrers(("application_details",), "/group_id",
                                                      [(key["collection"], key["id"])]):
                    use = self.live("uses", identity)
                    if use is not None:
                        members.append(("uses", use.id, use.version))
            self.relations[cache_key] = sorted(set(members))
        return self.relations[cache_key]


class _LocalClosure(_Closure):
    """Expand registered local inference material, never a containing theorem by default."""

    def __init__(self, db, mode, audit_id, max_bytes):
        super().__init__(db, mode)
        self.audit_id = audit_id
        self.state = _WorkState(db, max_bytes, MAX_WORK_RECORDS)

    def record(self, collection, id):
        return self.state.live(collection, id)

    def members(self, relation, collection, id):
        return self.state.relation_members(relation, {"collection": collection, "id": id})

    def heads(self, collection):
        # Only explicit all-source path lookup and audit selection use this path.
        # Neutral source suppliers are located through indexed relationships.
        where, params = "h.collection=? AND v.retired=0", [collection]
        if collection == "audits":
            where += " AND h.id=?"
            params.append(self.audit_id)
        rows = list(self.db.conn.execute(f"""SELECT h.id FROM record_heads h JOIN record_versions v
            ON v.collection=h.collection AND v.id=h.id AND v.version=h.version
            WHERE {where} ORDER BY h.id LIMIT ?""", (*params, MAX_WORK_RECORDS + 1)))
        if len(rows) > MAX_WORK_RECORDS:
            raise _ContextLimit(records=len(rows), bytes=self.state.body_bytes)
        return [self.record(collection, row[0]) for row in rows]

    def pull(self, owner_collections, *lookups):
        found = set()
        scoped = set(owner_collections) <= {"checks", "findings", "reconciliations", "responses"}
        for field, targets, prefix in lookups:
            found.update(self.state.referrers(owner_collections, field, targets, prefix=prefix,
                                              audit_id=self.audit_id if scoped else None))
        return [self.record(c, i) for c, i, _ in sorted(found)]

    def supplier(self, ref):
        record = self.add_ref(ref)
        if record is None or not self.once("supplier", record.collection, record.id):
            return record
        if record.collection not in ("items", "parts"):
            return record
        self.exact_target(record.ref)
        self.anchors(p["anchor_id"] for p in record.body["passages"] if p["role"] in ("statement", "definition"))
        self.scope_chain(record.body.get("scope_id"))
        if record.collection == "parts":
            self.supplier({"collection": "items", "id": record.body["item_id"]})
        return record

    def borrowed_proof(self, ref):
        record = self.add_ref(ref)
        if record is None or not self.once("borrowed", record.collection, record.id):
            return
        anchors = [p["anchor_id"] for p in record.body["passages"] if p["role"] in ("proof", "evidence")]
        self.anchors(anchors)
        if not anchors and record.collection == "parts":
            self.borrowed_proof({"collection": "items", "id": record.body["item_id"]})

    def assessments(self, collection, id):
        if self.mode not in ("primary", "reconcile"):
            return
        for record in self.pull(("checks", "findings"), ("/target", [(collection, id)], False)):
            self.add(record)
            self.anchors(record.body["evidence_refs"])
        if self.mode == "reconcile":
            self.guard("checks_or_findings_for_target", collection, id)

    def use(self, use_id):
        use = super().use(use_id)
        if use is not None:
            self.supplier(use.body["to"])
            if use.body["type"] == "proof_argument":
                self.borrowed_proof(use.body["from"])
        return use

    def group(self, group_id):
        group = self.add_id("groups", group_id)
        if group is None or not self.once("local_group", group_id):
            return group
        argument = self.add_id("arguments", group.body["argument_id"])
        if argument:
            self.supplier(argument.body["target"])
            self.scope_chain(argument.body["scope_id"])
        self.supplier(group.body["conclusion"])
        self.scope_chain(group.body["scope_id"])
        for scope_id in group.body["case_scope_ids"] + group.body["discharges"]:
            self.scope_chain(scope_id)
        self.anchors(group.body["evidence_refs"])
        self.guard("uses_in_group", "groups", group_id)
        for _, use_id, _ in self.members("uses_in_group", "groups", group_id):
            self.use(use_id)
        self.assessments("groups", group_id)
        return group

    def argument(self, argument_id):
        argument = self.add_id("arguments", argument_id)
        if argument is None or not self.once("argument", argument_id):
            return argument
        target = self.supplier(argument.body["target"])
        if target is not None and target.collection == "items":
            self.guard("parts_of_item", "items", target.id)
            for _, part_id, _ in self.members("parts_of_item", "items", target.id):
                self.supplier({"collection": "parts", "id": part_id})
        self.guard("incoming_uses", argument.body["target"]["collection"], argument.body["target"]["id"])
        self.scope_chain(argument.body["scope_id"])
        self.anchors(argument.body["evidence_refs"])
        self.proof_boundary(argument_id)
        for relation in ("groups_in_argument", "scopes_in_argument", "coverage_in_argument"):
            self.guard(relation, "arguments", argument_id)
        for _, group_id, _ in self.members("groups_in_argument", "arguments", argument_id):
            self.group(group_id)
        for _, scope_id, _ in self.members("scopes_in_argument", "arguments", argument_id):
            self.scope_chain(scope_id)
        for _, coverage_id, _ in self.members("coverage_in_argument", "arguments", argument_id):
            coverage = self.add_id("coverage", coverage_id)
            if coverage is None:
                continue
            self.add_id("anchors", coverage.body["anchor_id"])
            for check_id in coverage.body["check_ids"]:
                self.add_id("checks", check_id)
        self.assessments("arguments", argument_id)
        return argument

    def task(self, task):
        target, action = task["target"], task["action"]
        if action == "reconcile":
            # Reconciliation is a result-level obligation, but canonical comparison
            # records target the exact argument/group/use judged by both reviewers.
            # Expand only these supplied prerequisite judgments, not every proof
            # owned by the major result.
            for pin in task.get("prerequisite_judgment_refs", []):
                prior = self.state.version(pin["collection"], pin["id"], pin["version"])
                self.add(prior)
                if prior is not None and prior.collection == "checks":
                    self.anchors(prior.body["evidence_refs"])
                    self.task({"target": prior.body["target"], "action": "check", "kind": prior.body["kind"]})
        if action == "compare_source":
            if target["collection"] == "uses":
                use = self.add_id("uses", target["id"])
                detail = application(self.state, use)
                if use and detail["group_id"]:
                    self.group(detail["group_id"])
                else:
                    self.use(target["id"])
            elif target["collection"] == "target_specs":
                spec = self.add_id("target_specs", target["id"])
                if spec:
                    self.supplier(spec.body["target"])
                    self.anchors(spec.body["evidence_refs"])
                    self.scope_chain(spec.body["scope_id"])
            else:
                statement = self.supplier(target)
                if statement:
                    self.anchors(p["anchor_id"] for p in statement.body["passages"])
                    if statement.collection == "items":
                        self.guard("parts_of_item", "items", statement.id)
        elif target["collection"] == "uses":
            use = self.add_id("uses", target["id"])
            detail = application(self.state, use)
            if use and detail["group_id"]:
                self.group(detail["group_id"])
            else:
                self.use(target["id"])
        elif target["collection"] == "groups":
            self.group(target["id"])
        elif target["collection"] == "arguments":
            self.argument(target["id"])
        elif target["collection"] in ("items", "parts"):
            self.supplier(target)
            if task["kind"] == "external_source":
                self.borrowed_proof(target)
        elif target["collection"] == "audits":
            audit = self.add_id("audits", target["id"])
            for ref in audit.body["targets"]:
                self.supplier(ref)
        self.assessments(target["collection"], target["id"])
        for pin in task.get("draft_refs", []) + task.get("judgment_refs", []):
            self.add(self.state.version(pin["collection"], pin["id"], pin["version"]))

    def finish(self):
        if self.mode in ("independent", "route_review"):
            # The independent helper already selected source targets/assumptions.
            # Its source-only branch in the original finish is safe and bounded by pull.
            super().finish()
        else:
            audit = self.add_id("audits", self.audit_id)
            if audit is None:
                raise InvalidRequest(f"audit {self.audit_id} is not live")
            self.add_id("qualifications", audit.body["qualification_id"])
            keys = list(self.records)
            finding_keys = [key for key in keys if key[0] == "findings"]
            check_keys = [key for key in keys if key[0] == "checks"]
            for repair in self.pull(("repairs",), ("/finding_id", finding_keys, False)):
                self.add(repair)
                self.add_ref(repair.body["supported_form"])
                self.add_id("arguments", repair.body["argument_id"])
            for decision in self.pull(("reuse_decisions",), ("/check_ref", check_keys, False)):
                self.add(decision)
                self.add_id("source_reviews", decision.body["source_review_id"])
            if self.mode == "reconcile":
                for check in [r for r in list(self.records.values()) if r.collection == "checks"]:
                    response = self.add_id("responses", check.body["response_id"])
                    if response is not None:
                        self.add_id("qualifications", response.body["qualification_id"])
                        for mapping in self.pull(("identity_maps",), ("/response_id", [response.key], False)):
                            self.add(mapping)
            for observation in self.pull(("observations",), ("/target", keys, False)):
                self.add(observation)
                self.anchors(observation.body["evidence_refs"])
            for reconciliation in self.pull(("reconciliations",), ("/target", keys, False)):
                self.add(reconciliation)
                self.anchors(reconciliation.body["evidence_refs"])
                for pin in reconciliation.body["primary_checks"] + reconciliation.body["independent_checks"] \
                        + reconciliation.body["successor_checks"]:
                    self.add(self.state.version(pin["collection"], pin["id"], pin["version"]))
            for anchor in [r for r in list(self.records.values()) if r.collection == "anchors"]:
                self.add_id("sources", anchor.body["source_id"])
            source_keys = [key for key in self.records if key[0] == "sources"]
            anchor_keys = [key for key in self.records if key[0] == "anchors"]
            for issue in self.pull(("source_issues",), ("/source_id", source_keys, False),
                                   ("/anchor_id", anchor_keys, False)):
                if issue.body["lifecycle"] == "open":
                    self.add(issue)
            for review in self.pull(("source_reviews",), ("/source_refs", source_keys, True),
                                    ("/anchor_refs", anchor_keys, True)):
                self.add(review)
        for anchor in [r for r in self.records.values() if r.collection == "anchors"]:
            source = self.record("sources", anchor.body["source_id"])
            if source is None or source.version != anchor.body["source_version"]:
                raise SourceUnavailable("selected work contains a missing or stale source anchor",
                                        records=[{"anchor_id": anchor.id, "source_id": anchor.body["source_id"]}])


def _source_target(closure, task):
    target = task["target"]
    if target["collection"] in ("items", "parts"):
        return target
    record = closure.record(target["collection"], target["id"])
    if record is not None and target["collection"] == "arguments":
        return record.body["target"]
    if task.get("owner"):
        return task["owner"]
    raise InvalidRequest("independent work needs a source-origin item or part target")


def _source_comparisons(closure, tasks):
    """Exact saved text and changed prior wording for selected primary comparisons."""
    rows = []

    def statement(record):
        if record is None or record.retired:
            return None
        if record.collection == "target_specs" and record.body["statement"] is None:
            pin = record.body["statement_ref"]
            saved = closure.state.version(pin["collection"], pin["id"], pin["version"]) if pin else None
            return saved.body["statement"] if saved is not None and not saved.retired else None
        return record.body["statement"]

    for task in tasks:
        target = task["target"]
        if task["action"] != "compare_source" or target["collection"] not in ("items", "parts", "target_specs"):
            continue
        record = closure.record(target["collection"], target["id"])
        if record is None:
            continue
        current = statement(record)
        prior = closure.state.version(record.collection, record.id, record.version - 1) if record.version > 1 else None
        before = statement(prior)
        anchors = record.body["evidence_refs"] if record.collection == "target_specs" else [
            p["anchor_id"] for p in record.body["passages"] if p["role"] in ("statement", "definition")]
        rows.append({"task_id": task["id"], "target": record.pinned, "saved_statement": current,
            "previous_statement": {"ref": prior.pinned, "statement": before} if before is not None and before != current else None,
            "source_anchor_refs": [closure.records[("anchors", aid)].pinned for aid in dict.fromkeys(anchors)
                                   if ("anchors", aid) in closure.records],
            "scope_ref": {"collection": "scopes", "id": record.body["scope_id"]} if record.body.get("scope_id") else None})
    return rows


def _assignment_packet(closure, *, audit_id, mode, selection, units, tasks, specs, packet_id, revision, max_bytes,
                       declared_scope=None):
    records = sorted(closure.records.values(), key=lambda record: record.key)
    guards = [{"relation": relation, "key": {"collection": c, "id": i},
               "digest": membership_digest(closure.members(relation, c, i))}
              for relation, c, i in sorted(closure.guards)]
    targets = []
    for task in tasks:
        target = _source_target(closure, task) if mode == "independent" else task["target"]
        if target not in targets:
            targets.append(target)
    write_scope = [r.ref for r in records if r.collection == "coverage" or
                   (r.collection == "checks" and r.body["role"] == "primary" and r.body["state"] == "draft")]
    if mode == "reconcile":
        write_scope += [r.ref for r in records if r.collection in WRITE_COLLECTIONS[mode]
                        and r.ref not in write_scope and r.collection != "checks"]
    if mode == "independent":
        write_scope = []
    manifest = {"packet_version": PACKET_VERSION, "packet_id": packet_id, "base_revision": revision,
                "mode": mode, "targets": targets, "read_set": [r.pinned for r in records],
                "membership_guards": guards, "source_context_digest": source_context_digest(closure.db),
                "write_scope": write_scope}
    packet = dict(manifest, records=[_packet_record(r, mode) for r in records],
                  declared_scope=declared_scope, omitted=closure.omitted, extends=None, truncated=False)
    historical = _historical_statements(closure)
    if historical:
        packet["historical_records"] = historical
    task_ids = {task["id"] for task in tasks}
    unit_rows = [{"id": unit["id"], "task_ids": [id for id in unit["task_ids"] if id in task_ids],
                  "prerequisite_unit_ids": list(unit.get("prerequisite_unit_ids", []))} for unit in units]
    conditional = list(selection.get("conditional_on_task_ids", []))
    if mode != "independent":
        packet["instructions"] = {
            "units": unit_rows,
            "tasks": [{key: task[key] for key in ("id", "target", "kind", "role", "action", "prerequisite_ids")}
                      for task in tasks],
            "conditional_on_task_ids": conditional,
                        "boundary": "Check each local inference under its stated premises. Report missing dependencies "
                        "to the coordinator. A saved local judgment does not certify upstream support."}
    if mode == "primary":
        comparisons = _source_comparisons(closure, tasks)
        if comparisons:
            packet["source_comparisons"] = comparisons
    size = {"unique_records": len(records),
            "source_excerpt_bytes": sum(len(r.body["excerpt"].encode("utf-8")) for r in records
                                        if r.collection == "anchors"),
            "worker_bytes": len(canonical_bytes(packet))}
    manifest["work"] = {"audit_id": audit_id, "audit_ref": closure.db.head("audits", audit_id).pinned, "mode": mode,
                        "context": selection.get("context", {"owner": None, "argument": None}),
                        "units": unit_rows, "tasks": specs, "conditional_on_task_ids": conditional,
                        "limits": {"max_units": selection.get("max_units", 5), "max_bytes": max_bytes,
                                   "max_records": MAX_WORK_RECORDS}, "size": size}
    if mode == "independent":
        manifest["work"]["source_context_inputs"] = [closure.neutral_inputs[key]
                                                       for key in sorted(closure.neutral_inputs)]
        manifest["work"]["source_context_relations"] = [closure.neutral_relations[key]
                                                          for key in sorted(closure.neutral_relations)]
        manifest["work"]["neutral_setup_selection"] = 1
    return manifest, packet


def prepare_assignment(db: Database, *, audit_id: str, mode: str, selection: dict,
                       max_bytes: int = DEFAULT_WORK_BYTES) -> dict:
    """Persist one bounded complete-unit prefix of a derived coordinator selection.

    No model is dispatched. On overflow the uncut next unit remains pending; if
    even the first complete unit cannot fit, no packet is stored.
    """
    if not db.write:
        raise InvalidRequest("work preparation records a packet; open the database for writing")
    if mode not in ("primary", "independent", "reconcile"):
        raise InvalidRequest("work mode must be primary, independent or reconcile")
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_WORK_BYTES:
        raise InvalidRequest(f"max_bytes must be an integer in 1..{MAX_WORK_BYTES}")
    units, tasks = selection.get("units", []), selection.get("tasks", [])
    deferral_reasons = [dict(row) for row in selection.get("deferred", [])]
    remaining = {"deferred": [row["unit_id"] for row in deferral_reasons],
                 "deferred_reasons": deferral_reasons,
                 "coordinator_actions": selection.get("coordinator_actions", [])}
    if len(units) > 10:
        raise InvalidRequest("an assignment cannot contain more than 10 work units")
    if selection.get("analysis_complete") is False:
        return {"prepared": False, "diagnostics": [{"code": "ANALYSIS_INCOMPLETE"}], **remaining}
    if not units or not tasks:
        return {"prepared": False, "diagnostics": [], **remaining}
    task_map = {task["id"]: task for task in tasks}
    if len(task_map) != len(tasks):
        raise InvalidRequest("selection repeats a task ID")
    revision = db.max_revision()
    if selection.get("revision", revision) != revision:
        raise ConflictError("work selection belongs to a changed snapshot; list and prepare current work")
    audit = db.head("audits", audit_id)
    if audit is None or audit.retired:
        raise InvalidRequest(f"audit {audit_id} is not live")
    if mode == "independent":
        qualification = db.head("qualifications", audit.body["qualification_id"])
        if qualification is None or qualification.retired or not qualification.body["qualified"] \
                or qualification.body["protocol_version"] != audit.body["protocol_version"] \
                or validate_body("qualifications", qualification.body):
            raise InvalidRequest("independent preparation needs the audit's valid qualification",
                                 code="QUALIFICATION_INVALID")
    closure = _LocalClosure(db, mode, audit_id, max_bytes)
    selected, assigned, specs, best, diagnostics = [], [], [], None, []
    packet_id, declared_scope = new_id("packet"), None
    for index, unit in enumerate(units):
        try:
            unit_tasks = []
            for task_id in unit["task_ids"]:
                if task_id not in task_map:
                    raise InvalidRequest(f"selected unit names unknown task {task_id}")
                if task_id not in {t["id"] for t in assigned}:
                    unit_tasks.append(task_map[task_id])
            if mode == "independent":
                source_targets = []
                for task in assigned + unit_tasks:
                    target = _source_target(closure, task)
                    if target not in source_targets:
                        source_targets.append(target)
                closure, declared_scope, _ = _independent(db, source_targets, closure=closure, audit_id=audit_id)
            else:
                for task in unit_tasks:
                    closure.task(task)
                closure.finish()
            unit_specs = []
            for task in unit_tasks:
                binding_task = task
                if mode == "independent":
                    binding_task = dict(task, target=_source_target(closure, task), action="compare_source",
                                        source_only=True)
                bound = task_binding(closure.state, binding_task)
                spec = {key: task[key] for key in ("id", "target", "kind", "role", "action", "prerequisite_ids")}
                spec.update(bound, draft_refs=task.get("draft_refs", []))
                unit_specs.append(spec)
                # Bindings can identify selected scope assumptions or saved predecessor
                # checks which are not reached through the initial context traversal.
                for entry in bound["consumed_inputs"]:
                    pin = entry["ref"]
                    record = closure.state.version(pin["collection"], pin["id"], pin["version"])
                    closure.add(record)
            closure.finish()
            manifest, packet = _assignment_packet(closure, audit_id=audit_id, mode=mode, selection=selection,
                units=selected + [unit], tasks=assigned + unit_tasks, specs=specs + unit_specs,
                packet_id=packet_id, revision=revision, max_bytes=max_bytes, declared_scope=declared_scope)
            size = manifest["work"]["size"]
            if size["unique_records"] > MAX_WORK_RECORDS or size["worker_bytes"] > max_bytes:
                raise _ContextLimit(records=size["unique_records"], bytes=size["worker_bytes"],
                                    contributors=closure.state.contributors())
            selected.append(unit)
            assigned.extend(unit_tasks)
            specs.extend(unit_specs)
            best = manifest, packet
        except _ContextLimit as exc:
            diagnostics.append({"code": "OVERSIZED_CONTEXT", "unit_id": unit["id"],
                "target_ref": unit.get("target", (unit_tasks or tasks)[0]["target"]),
                "reason": "oversized_context", "measured_or_lower_bound_bytes": exc.bytes,
                "unique_record_count": exc.records, "largest_contributors": exc.contributors})
            break
    for unit in units:
        if unit not in selected:
            remaining["deferred"].append(unit["id"])
            deferral_reasons.append({"unit_id": unit["id"], "reason": "packet_size_limit"})
    if best is None:
        return {"prepared": False, "diagnostics": diagnostics, **remaining}
    manifest, packet = best
    if mode == "independent":
        problems = blinding_violations(packet)
        if problems:
            raise InvalidRequest("independent work packet failed blinding check", records=problems)
    _persist(db, manifest, packet)
    return {"prepared": True, "packet_id": packet_id, "revision": revision, "audit_id": audit_id, "mode": mode,
            "manifest": manifest, "packet": packet, "selected_unit_ids": [u["id"] for u in selected],
            "assigned_task_ids": [t["id"] for t in assigned],
            "conditional_on_task_ids": manifest["work"]["conditional_on_task_ids"],
            **remaining, "size": manifest["work"]["size"], "diagnostics": diagnostics}


def _legacy_extension_setup(db, manifest, state, *, verify=True):
    """Compare old v2 packets' untransported scope using bounded historical reads."""
    # Earlier v2 packets did not persist neutral selection inputs. Reconstruct
    # only their assigned mathematical bindings, never a full historical audit.
    class HistoricalState:
        def __init__(self):
            self.db, self.revision = db, manifest["base_revision"]
            self.cache, self.bytes = {}, 0

        def live(self, collection, identity):
            key = collection, identity
            if key not in self.cache:
                record = db.latest_at(collection, identity, manifest["base_revision"])
                self.cache[key] = None if record is None or record.retired else record
                if record is not None and not record.retired:
                    self.bytes += len(canonical_bytes(record.body))
                if len(self.cache) > MAX_WORK_RECORDS or self.bytes > getattr(state, "max_bytes", manifest["work"]["limits"]["max_bytes"]):
                    raise _ContextLimit(records=len(self.cache), bytes=self.bytes)
            return self.cache[key]

        def version(self, collection, identity, version):
            return db.version(collection, identity, version)

        def relation_members(self, relation, key):
            rows = relation_members(db.conn, relation, key, revision=manifest["base_revision"])
            if len(rows) > MAX_WORK_RECORDS:
                raise _ContextLimit(records=len(rows), bytes=self.bytes)
            return rows

    historical = HistoricalState()
    inputs, relations = {}, {}
    for task in manifest["work"]["tasks"]:
        binding = task_binding(historical, task)
        if verify:
            changes = task_binding_changes(state, dict(task, **binding), packet=manifest)
            if changes["records"] or changes["relations"]:
                raise ConflictError("consumed target or setup changed since the historical assignment; prepare renewed work",
                                    records=[changes])
        for row in binding["consumed_inputs"]:
            inputs[(row["ref"]["collection"], row["ref"]["id"], row["facet"])] = row
        for row in binding["membership_guards"]:
            key = row["key"]
            relations[(row["relation"], key["collection"], key["id"])] = dict(row,
                members=sorted([[c, i] for c, i, _ in historical.relation_members(row["relation"], key)]))
    return list(inputs.values()), list(relations.values())


def neutral_relation_covered(state, relation, records, *, checked_targets=None):
    """Whether changed graph membership still selects only the delivered setup.

    Routine coordinator IDs may be added without new source work. Inspect their
    bounded neutral selection instead of treating every new graph ID as evidence.
    """
    from .semantics import related

    class Selection(_Closure):
        def __init__(self):
            super().__init__(state.db, "independent")
            self.seen, self.bytes = set(), 0

        def record(self, collection, identity):
            record = state.live(collection, identity)
            if record is not None and record.key not in self.seen:
                self.seen.add(record.key)
                self.bytes += len(canonical_bytes(record.body))
                if len(self.seen) > MAX_WORK_RECORDS or self.bytes > MAX_WORK_BYTES:
                    raise _ContextLimit(records=len(self.seen), bytes=self.bytes)
            return record

        def members(self, name, collection, identity):
            members = state.relation_members(name, {"collection": collection, "id": identity})
            if len(members) > MAX_WORK_RECORDS:
                raise _ContextLimit(records=len(members), bytes=self.bytes)
            return members

        def pull(self, owners, *lookups):
            found = {}
            for field, targets, prefix in lookups:
                for collection, identity in targets:
                    for owner in owners:
                        for record in related(state, owner, field, {"collection": collection, "id": identity}, prefix=prefix):
                            found[record.key] = self.record(record.collection, record.id)
            return list(found.values())

    closure = Selection()
    try:
        key = relation["key"]
        target = key
        if key["collection"] == "groups":
            group = closure.record("groups", key["id"])
            target = {"collection": "arguments", "id": group.body["argument_id"]} if group else None
        if target and target["collection"] == "arguments":
            argument = closure.record("arguments", target["id"])
            target = argument.body["target"] if argument else None
        if target is None or target["collection"] not in ("items", "parts"):
            return False
        selection_key = (target["collection"], target["id"], relation["relation"] == "target_specs_for_target")
        if checked_targets is not None and selection_key in checked_targets:
            return True
        _neutral_statement(closure, target)
        if relation["relation"] != "target_specs_for_target":
            _neutral_dependencies(closure, target)
        expected = {(row["ref"]["collection"], row["ref"]["id"], row["facet"]): row["digest"] for row in records}
        for row in closure.neutral_inputs.values():
            pin = row["ref"]
            if pin["collection"] in ("items", "parts", "scopes") \
                    and expected.get((pin["collection"], pin["id"], row["facet"])) != row["digest"]:
                return False
        for record in closure.records.values():
            if record.collection == "anchors":
                facets = facet_digests(record.collection, record.body)
                if any(expected.get(("anchors", record.id, facet)) != facets[facet] for facet in ("statement", "source")):
                    return False
        if checked_targets is not None:
            checked_targets.add(selection_key)
        return True
    except _ContextLimit:
        return False


def independent_context_binding(state, manifest):
    """Original source/setup and its private attachments for a blind work review.

    This binding is also retained on mapped checks. Otherwise a coordinator could
    map an old response after changing an unseen standing assumption and bind the
    resulting judgment only to that newer setup.
    """
    work = manifest.get("work")
    if manifest.get("mode") != "independent" or not isinstance(work, dict) \
            or manifest.get("review_basis") == "route_provided":
        return None
    inputs = work.get("source_context_inputs")
    relations = work.get("source_context_relations", [])
    if inputs is None:
        inputs, relations = _legacy_extension_setup(state.db, manifest, state, verify=False)
    records = {}
    for row in inputs:
        pin = row["ref"]
        if pin["collection"] not in ("items", "parts", "scopes", "arguments", "groups", "uses",
                                    "application_details", "target_specs"):
            continue
        original = state.version(pin["collection"], pin["id"], pin["version"])
        if original is None or original.retired:
            raise InvalidRequest("original independent context is unavailable", code="SOURCE_CONTEXT_UNAVAILABLE", records=[pin])
        if pin["collection"] in ("items", "parts") and original.body["origin"] != "source":
            continue
        consumed = copy.deepcopy(row)
        projection = setup_digest(original.collection, original.body)
        if projection is not None:
            consumed["setup_digest"] = projection
        records[(pin["collection"], pin["id"], row["facet"])] = consumed
    for pin in manifest["read_set"]:
        facets = {"anchors": ("statement", "source"), "sources": ("source",),
                  "source_issues": ("full",)}.get(pin["collection"], ())
        if not facets:
            continue
        original = state.version(pin["collection"], pin["id"], pin["version"])
        if original is None or original.retired:
            raise InvalidRequest("original independent source is unavailable", code="SOURCE_CONTEXT_UNAVAILABLE", records=[pin])
        values = facet_digests(original.collection, original.body)
        for facet in facets:
            records[(pin["collection"], pin["id"], facet)] = {
                "ref": dict(pin), "facet": facet, "digest": values[facet]}
    if not work.get("neutral_setup_selection"):
        # Earlier packets could omit selected setup, including exact-specification
        # and discharged scopes. Never recover credit for undelivered context.
        class HistoricalSelection:
            db, revision = state.db, manifest["base_revision"]

            def live(self, collection, identity):
                record = self.db.latest_at(collection, identity, self.revision)
                return record if record is not None and not record.retired else None

            def relation_members(self, relation, key):
                return relation_members(self.db.conn, relation, key, revision=self.revision)

        historical = HistoricalSelection()
        relations = copy.deepcopy(relations)
        checked_targets = set()
        for guard in relations:
            if not neutral_relation_covered(historical, guard, list(records.values()), checked_targets=checked_targets):
                raise ConflictError("original independent assignment omitted applicable source or setup; obtain a renewed review")
        for row in list(records.values()):
            pin = row["ref"]
            if pin["collection"] not in ("items", "parts") or row["facet"] != "statement":
                continue
            key = {k: pin[k] for k in ("collection", "id")}
            guard = {"relation": "target_specs_for_target", "key": key}
            if not neutral_relation_covered(historical, guard, list(records.values())):
                raise ConflictError("original independent assignment omitted applicable source or setup; obtain a renewed review")
            members = historical.relation_members(guard["relation"], key)
            guard.update(digest=membership_digest(members), members=sorted([[c, i] for c, i, _ in members]))
            relations = [entry for entry in relations if (entry["relation"], entry["key"]) != (guard["relation"], key)]
            relations.append(guard)
            for collection, identity, _ in members:
                spec = historical.live(collection, identity)
                records[(collection, identity, "statement")] = {"ref": spec.pinned, "facet": "statement",
                    "digest": facet_digests(collection, spec.body)["statement"],
                    "setup_digest": setup_digest(collection, spec.body)}
    return {"records": [records[key] for key in sorted(records)],
            "relations": [{k: row[k] for k in ("relation", "key", "digest")} for row in relations],
            "semantic_memberships": [dict(copy.deepcopy(row), neutral_context=True) for row in relations]}


def independent_context_changes(state, manifest):
    binding = independent_context_binding(state, manifest)
    return binding_changes(state, binding) if binding is not None else {"records": [], "relations": []}


def extend_work_assignment(db: Database, *, packet_id: str, request: dict) -> dict:
    """Add neutral captured source to the same independent obligation, atomically.

    The old packet and response are immutable. The new packet authorizes a new
    response, not a rebased claim that the old response examined the new context.
    """
    if not db.write:
        raise InvalidRequest("work extension records a packet; open the database for writing")
    if not isinstance(request, dict) or set(request) != {"source_refs", "reason"} \
            or not isinstance(request["reason"], str) or not request["reason"].strip() \
            or not isinstance(request["source_refs"], list) or not request["source_refs"]:
        raise InvalidRequest("context request must be {source_refs: [pinned item/part/anchor], reason: nonempty string}",
                             code="WORK_CONTEXT_REQUEST")
    if len(request["source_refs"]) > MAX_WORK_RECORDS or len(canonical_bytes(request)) > MAX_WORK_BYTES:
        raise InvalidRequest("context request exceeds the work record/byte limit", code="WORK_CONTEXT_REQUEST")
    seen = set()
    for index, pin in enumerate(request["source_refs"]):
        if not isinstance(pin, dict) or set(pin) != {"collection", "id", "version"} \
                or pin["collection"] not in ("items", "parts", "anchors") \
                or not isinstance(pin["id"], str) or not pin["id"] \
                or type(pin["version"]) is not int or pin["version"] < 1:
            raise InvalidRequest(f"source_refs[{index}] must be {{collection: items|parts|anchors, id: string, version: positive integer}}",
                                 code="WORK_CONTEXT_REQUEST")
        key = pin["collection"], pin["id"]
        if key in seen:
            raise InvalidRequest(f"source_refs[{index}] duplicates {key[0]}:{key[1]}", code="WORK_CONTEXT_REQUEST")
        seen.add(key)
    db.begin_immediate()
    try:
        stored = db.packet(packet_id)
        if stored is None:
            raise InvalidRequest(f"unknown packet {packet_id}", code="PACKET_UNKNOWN")
        original = stored["manifest"]
        if original.get("packet_version") != 2 or not isinstance(original.get("work"), dict) \
                or original.get("mode") != "independent" or original.get("review_basis") == "route_provided":
            raise InvalidRequest("work extend requires a source-only independent v2 work assignment; "
                                 "use work prepare for route-provided or other work", code="WORK_CONTEXT_MODE")
        pending = db.conn.execute("SELECT request_id FROM work_submissions WHERE packet_id=? AND state='received' LIMIT 1",
                                  (packet_id,)).fetchone()
        if pending:
            raise InvalidRequest(f"inspect and idempotently replay received request {pending[0]} before extending its assignment",
                                 code="WORK_CONTEXT_PENDING")
        prior = json.loads(db.get_blob(stored["payload_sha256"]).decode("utf-8"))
        if blinding_violations(prior):
            raise InvalidRequest("the original assignment is not source-only", code="WORK_CONTEXT_MODE")
        work = original["work"]
        audit = db.head("audits", work["audit_id"])
        old_audit_pin = work["audit_ref"]
        old_audit = db.version("audits", old_audit_pin["id"], old_audit_pin["version"])
        scope_fields = ("paper_id", "mode", "targets", "exclusions", "protocol_version", "qualification_id", "independent_required")
        if audit is None or audit.retired or old_audit is None \
                or any(audit.body[k] != old_audit.body[k] for k in scope_fields):
            raise ConflictError("audit scope or review protocol changed; prepare a renewed assignment")
        qualification = db.head("qualifications", audit.body["qualification_id"])
        if qualification is None or qualification.retired or not qualification.body["qualified"] \
                or qualification.body["protocol_version"] != audit.body["protocol_version"]:
            raise InvalidRequest("independent extension needs a valid current qualification", code="QUALIFICATION_INVALID")
        max_bytes = work["limits"]["max_bytes"]
        closure = _LocalClosure(db, "independent", audit.id, max_bytes)
        for entry in prior["records"]:
            pin = entry["ref"]
            current = closure.record(pin["collection"], pin["id"])
            if current is None or current.retired or current.version != pin["version"]:
                raise ConflictError("previously delivered source context changed; prepare a renewed assignment", records=[pin])
            closure.add(current)
        for task in work["tasks"]:
            changes = task_binding_changes(closure.state, task, packet=original)
            if changes["records"] or changes["relations"]:
                raise ConflictError("consumed mathematics changed; prepare a renewed assignment", records=[changes])
        neutral_inputs, neutral_relations = work.get("source_context_inputs", []), work.get("source_context_relations", [])
        if "source_context_inputs" not in work:
            neutral_inputs, neutral_relations = _legacy_extension_setup(db, original, closure.state)
        neutral_binding = {"records": neutral_inputs,
                           "relations": [{k: row[k] for k in ("relation", "key", "digest")} for row in neutral_relations],
                           "semantic_memberships": neutral_relations}
        changes = binding_changes(closure.state, neutral_binding)
        if changes["records"] or changes["relations"]:
            raise ConflictError("applicable source setup or registered inference changed; prepare a renewed assignment", records=[changes])
        for guard in original.get("membership_guards", []):
            if membership_digest(closure.members(guard["relation"], guard["key"]["collection"], guard["key"]["id"])) != guard["digest"]:
                raise ConflictError("reviewed source membership changed; prepare a renewed assignment", records=[guard])
        for pin in request["source_refs"]:
            record = closure.record(pin["collection"], pin["id"])
            if record is None or record.retired or record.version != pin["version"]:
                raise ConflictError("requested source_ref is not a pinned live version", records=[pin])
            if record.collection == "anchors":
                closure.add(record)
            elif _neutral_statement(closure, pin) is None:
                raise InvalidRequest("context items/parts must be source-origin statements, not proposed intermediate claims or repairs",
                                     code="WORK_CONTEXT_SOURCE", records=[pin])
        closure.finish()
        for record in closure.records.values():
            if record.collection == "sources" and record.body["paper_id"] != audit.body["paper_id"]:
                raise InvalidRequest("context source belongs to another paper", code="WORK_CONTEXT_SOURCE", records=[record.pinned])
            if record.collection == "anchors":
                source = closure.record("sources", record.body["source_id"])
                if source is None or source.version != record.body["source_version"]:
                    raise ConflictError("requested anchor does not pin the current captured source", records=[record.pinned])
        old_keys = {(entry["ref"]["collection"], entry["ref"]["id"]) for entry in prior["records"]}
        if not (set(closure.records) - old_keys):
            raise InvalidRequest("context request adds no new source material", code="WORK_CONTEXT_EMPTY")
        revision, identity = db.max_revision(), new_id("packet")
        records = sorted(closure.records.values(), key=lambda r: r.key)
        manifest, packet = copy.deepcopy(original), copy.deepcopy(prior)
        common = {"packet_id": identity, "base_revision": revision, "extends": packet_id,
                  "read_set": [r.pinned for r in records], "source_context_digest": source_context_digest(db)}
        manifest.update(common)
        packet.update(common)
        packet["records"] = [_packet_record(r, "independent") for r in records]
        packet["omitted"] = prior.get("omitted", []) + [row for row in closure.omitted if row not in prior.get("omitted", [])]
        size = {"unique_records": len(records),
                "source_excerpt_bytes": sum(len(r.body["excerpt"].encode("utf-8")) for r in records if r.collection == "anchors"),
                "worker_bytes": len(canonical_bytes(packet))}
        if size["unique_records"] > MAX_WORK_RECORDS or size["worker_bytes"] > max_bytes:
            raise _ContextLimit(records=size["unique_records"], bytes=size["worker_bytes"], contributors=closure.state.contributors())
        manifest["work"]["size"] = size
        manifest["work"]["audit_ref"] = audit.pinned
        manifest["work"]["context_extension"] = {"parent_packet_id": packet_id, "request": copy.deepcopy(request)}
        inputs = {(row["ref"]["collection"], row["ref"]["id"], row["facet"]): row for row in neutral_binding["records"]}
        inputs.update(closure.neutral_inputs)
        manifest["work"]["source_context_inputs"] = [inputs[key] for key in sorted(inputs)]
        relations = {(row["relation"], row["key"]["collection"], row["key"]["id"]): row
                     for row in neutral_relations}
        relations.update(closure.neutral_relations)
        manifest["work"]["source_context_relations"] = [relations[key] for key in sorted(relations)]
        problems = blinding_violations(packet)
        if problems:
            raise InvalidRequest("extended assignment failed blinding check", records=problems)
        sha = db.put_blob(canonical_bytes(packet))
        db.insert_packet(identity, revision, "independent", manifest, sha)
        features = set(json.loads(db.conn.execute("SELECT value FROM metadata WHERE key='features'").fetchone()[0]))
        features.add(WORK_CONTEXT_EXTENSION_FEATURE)
        db.conn.execute("UPDATE metadata SET value=? WHERE key='features'", (json.dumps(sorted(features)),))
        db.commit()
        db.metadata = db.check_compatibility()
        return {"prepared": True, "packet_id": identity, "revision": revision, "audit_id": audit.id,
                "mode": "independent", "manifest": manifest, "packet": packet,
                "selected_unit_ids": [u["id"] for u in work["units"]],
                "assigned_task_ids": [t["id"] for t in work["tasks"]],
                "conditional_on_task_ids": work["conditional_on_task_ids"], "size": size,
                "deferred": [], "diagnostics": []}
    except _ContextLimit as exc:
        db.rollback()
        return {"prepared": False, "packet_id": packet_id, "mode": "independent", "deferred": [],
                "diagnostics": [{"code": "OVERSIZED_CONTEXT", "measured_or_lower_bound_bytes": exc.bytes,
                                 "unique_record_count": exc.records, "largest_contributors": exc.contributors,
                                 "next_action": "request a narrower source passage or plan a separately bounded review"}]}
    except BaseException:
        db.rollback()
        raise


def prepare_route_assignment(db: Database, *, audit_id, route_id, max_bytes=DEFAULT_WORK_BYTES):
    """Prepare a supplied derivation for independent review without primary verdicts."""
    from .assessment import obligation_id

    revision = db.max_revision()
    audit, route = db.head("audits", audit_id), db.head("arguments", route_id)
    if audit is None or audit.retired or route is None or route.retired:
        raise InvalidRequest("route review needs a live audit and argument", code="ROUTE_REVIEW_SCOPE")
    if route.body["lifecycle"] != "registered":
        raise InvalidRequest("register the proposed route before independent review", code="ROUTE_REVIEW_SCOPE")
    qualification = db.head("qualifications", audit.body["qualification_id"]) if audit.body["qualification_id"] else None
    if (qualification is None or qualification.retired or not qualification.body["qualified"]
            or qualification.body["protocol_version"] != audit.body["protocol_version"]
            or validate_body("qualifications", qualification.body)):
        raise InvalidRequest("route review needs the audit's valid qualification", code="QUALIFICATION_INVALID")
    target = route.body["target"]
    statement = db.head(target["collection"], target["id"])
    owner = ({"collection": "items", "id": statement.body["item_id"]} if statement.collection == "parts"
             else {"collection": "items", "id": statement.body["owner_id"]} if statement.body.get("owner_id")
             else target)
    candidates = [target] if owner == target else [target, owner]
    # A restricted repair may be a new major target. Its finding identifies the
    # original source result whose blind written-proof review must be preserved.
    for _, repair_id, _ in referrers(db.conn, ("repairs",), "/argument_id", [route.key]):
        repair = db.head("repairs", repair_id)
        finding = db.head("findings", repair.body["finding_id"])
        if finding is not None and finding.body["target"]["collection"] in ("items", "parts"):
            candidates.append(finding.body["target"])
    initial = []
    for _, response_id, _ in referrers(db.conn, ("responses",), "/covered_targets",
                                       [(r["collection"], r["id"]) for r in candidates], prefix=True):
        response = db.head("responses", response_id)
        if (response.body["audit_id"] == audit_id and response.body["exposure"] == "source_only"
                and response.body["state"] == "accepted"):
            for _, check_id, _ in referrers(db.conn, ("checks",), "/response_id", [response.key]):
                check = db.head("checks", check_id)
                written = (db.head("arguments", check.body["target"]["id"])
                           if check.body["kind"] == "composition" else None)
                if (check.body["state"] == "complete" and written is not None
                        and written.body["target"] in candidates):
                    initial.append(response.pinned)
                    break
    if not initial:
        raise InvalidRequest("preserve the initial source-only review of this result before supplying a route",
                             code="SOURCE_ONLY_REVIEW_REQUIRED")
    if audit.body["mode"] != "full" and not any(ref in audit.body["targets"] for ref in candidates):
        # Focused prerequisites are admitted by a previously accepted review in
        # this same audit; no unrelated source-only response can qualify.
        if not any(target in db.head("responses", pin["id"]).body["covered_targets"] for pin in initial):
            raise InvalidRequest("the supplied route is outside the audit's reviewed scope", code="ROUTE_REVIEW_SCOPE")

    closure = _LocalClosure(db, "route_review", audit_id, max_bytes)
    try:
        closure.argument(route_id)
        closure.finish()
        pairs = [(route.ref, "composition")]
        for _, group_id, _ in closure.members("groups_in_argument", "arguments", route_id):
            group = closure.record("groups", group_id)
            pairs.append((group.ref, "derivation"))
            if group.body["kind"] == "cases":
                pairs.append((group.ref, "case_coverage"))
            if group.body["discharges"]:
                pairs.append((group.ref, "scope_discharge"))
            pairs.extend(({"collection": "uses", "id": use_id}, "application")
                         for _, use_id, _ in closure.members("uses_in_group", "groups", group_id))
        tasks, specs, supplied, supplied_refs = [], [], [], []
        for exact_target, kind in pairs:
            task = {"id": obligation_id(audit_id, exact_target, kind, "independent"), "target": exact_target,
                    "kind": kind, "role": "independent", "action": "check", "prerequisite_ids": [], "owner": target}
            bound = task_binding(closure.state, task)
            checks = [r for r in closure.pull(("checks",), ("/target", [(exact_target["collection"], exact_target["id"])], False))
                      if r.body["role"] == "primary" and r.body["kind"] == kind and r.body["state"] == "complete"]
            superseded = {r.body["supersedes"]["id"] for r in checks if r.body["supersedes"]}
            checks = [r for r in checks if r.id not in superseded]
            for check in checks:
                # Preserve substantive proposed mathematics, never its outcome,
                # role, grading hints, findings, or prior independent opinions.
                supplied.append({key: check.body[key] for key in
                                 ("target", "kind", "reasoning", "evidence_refs", "conditions")})
                supplied_refs.append(check.pinned)
                closure.anchors(check.body["evidence_refs"])
                bound["consumed_inputs"].append({"ref": check.pinned, "facet": "full",
                                                 "digest": facet_digests("checks", check.body)["full"]})
            specs.append({**{k: task[k] for k in ("id", "target", "kind", "role", "action", "prerequisite_ids")},
                          **bound, "draft_refs": []})
            tasks.append(task)
        closure.finish()
        unit = {"id": "unit_" + digest(["route_review", audit_id, route_id]), "task_ids": [t["id"] for t in tasks]}
        scope = {"audit_id": audit_id, "mode": audit.body["mode"], "targets": [target],
                 "exclusions": audit.body["exclusions"], "protocol_version": audit.body["protocol_version"]}
        selection = {"context": {"owner": owner, "argument": route.ref}, "max_units": 1}
        manifest, packet = _assignment_packet(closure, audit_id=audit_id, mode="independent", selection=selection,
            units=[unit], tasks=tasks, specs=specs, packet_id=new_id("packet"), revision=revision,
            max_bytes=max_bytes, declared_scope=scope)
        manifest.update(review_basis="route_provided", route_ref=route.pinned,
                        initial_response_refs=initial, supplied_derivation_refs=supplied_refs)
        packet.update(review_basis="route_provided", route_ref=route.pinned, supplied_derivations=supplied)
        # The worker sees the reviewed proposal but none of the author judgments.
        # Source review records and coverage are coordinator provenance only.
        if any(r["ref"]["collection"] not in ROUTE_REVIEW_COLLECTIONS for r in packet["records"]):
            raise InvalidRequest("supplied-route packet contains assessment records")
        size = manifest["work"]["size"]
        size["worker_bytes"] = len(canonical_bytes(packet))
        if size["worker_bytes"] > max_bytes:
            raise _ContextLimit(records=size["unique_records"], bytes=size["worker_bytes"],
                                contributors=closure.state.contributors())
    except _ContextLimit as exc:
        return {"prepared": False, "diagnostics": [{"code": "OVERSIZED_CONTEXT", "reason": "oversized_context",
                "measured_or_lower_bound_bytes": exc.bytes, "unique_record_count": exc.records,
                "largest_contributors": exc.contributors}], "deferred": [route_id]}
    _persist(db, manifest, packet)
    return {"prepared": True, "packet_id": manifest["packet_id"], "revision": revision,
            "audit_id": audit_id, "mode": "independent", "review_basis": "route_provided",
            "manifest": manifest, "packet": packet, "selected_unit_ids": [unit["id"]],
            "assigned_task_ids": unit["task_ids"], "conditional_on_task_ids": [],
            "deferred": [], "size": size, "diagnostics": []}


__all__ = ["MODES", "READ_COLLECTIONS", "WRITE_COLLECTIONS", "blinding_violations", "get_packet", "load_packet",
           "source_context_digest", "prepare_assignment", "extend_work_assignment", "independent_context_binding",
           "independent_context_changes", "DEFAULT_WORK_BYTES", "MAX_WORK_BYTES", "MAX_WORK_RECORDS"]
