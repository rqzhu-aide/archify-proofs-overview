"""Derived assessment of a snapshot (record-contract 6-7, architecture 9.3, implementation-handoff 7.3).

Nothing here is stored. Obligations are derived from the registered structure
and the declared audit; freshness comes from comparing each judgment's stored
binding with the snapshot; dependency support follows the recorded uses; and
one reducer turns constituents into the four presentation states. The
projection reuses the same reducer, so the report and ``status`` cannot drift.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict, deque

from .bindings import binding_changes
from .canonical import compact_json, digest
from .contract import INTERMEDIATE_KINDS, MAJOR_KINDS, extract_refs
from .errors import ConflictError, InvalidRequest
from .refs import RELATIONS, facet_digests
from .storage import Database, Record
from .support_semantics import SupportClosure

PROOF_KINDS = ("lemma", "proposition", "theorem", "corollary")
PROOF_CHECK_KINDS = ("derivation", "application", "composition", "case_coverage", "scope_discharge",
                     "external_source")
DEFECT_OUTCOMES = ("gap", "refuted")
STATES = ("green", "red", "gray", "amber")
INDICATORS = ("not_required", "pending", "complete", "disputed", "compromised")
ROLES = ("primary", "independent", "coordinator")
OBLIGATION_KINDS = PROOF_CHECK_KINDS + ("global_consistency", "adversarial", "method_interface",
                                        "source_fidelity", "reconciliation")

COVERAGE_CAUSES = {
    "missing_coverage": ("No coverage row accounts for these proof spans.",
                         "Classify the spans and add coverage with the responsible primary work, or use an authoring packet if no checks remain."),
    "missing_anchor": ("The required proof anchor is unavailable.",
                       "Restore the source anchor and review the affected proof boundary."),
    "missing_claims": ("A substantive coverage row has no claimed statements.",
                       "Record the item or part statements examined in this passage."),
    "missing_checks": ("A substantive coverage row has no responsible primary checks.",
                       "Link the primary checks that examined its claimed statements."),
    "structural_links": ("A structural coverage row declares claims or checks.",
                         "Correct the classification or the inaccurately declared links."),
    "wrong_argument": ("A linked check examines another argument.",
                       "Correct an inaccurate link or obtain the responsible check in this argument."),
    "unusable_check": ("A linked check has no usable current, complete primary examination in this audit.",
                       "Renew affected reasoning with an explicit successor, or correct an inaccurate link; an unrelated newer check does not carry this link forward."),
    "unsatisfied_obligation": ("A linked check's corresponding obligation is not satisfied.",
                               "Resolve the obligation for the linked check before claiming coverage credit."),
    "unconsumed_claims": ("Linked checks did not consume every claimed statement.",
                          "Examine the missing item or part statements in responsible primary checks, or correct inaccurate claims; citing the passage alone is insufficient."),
}


def coverage_diagnostic(fact):
    """Render an eligibility fact without evaluating coverage a second time."""
    message, action = COVERAGE_CAUSES[fact["code"]]
    row = dict(fact, message=message, next_action=action)
    for field in ("argument_ids", "claim_refs", "spans"):
        if len(row.get(field, ())) > 10:
            row[field + "_count"] = len(row[field])
            row[field + "_truncated"] = True
            row[field] = row[field][:10]
    return row


class TraversalLimit(InvalidRequest):
    """The registered graph was not fully analyzed; never a mathematical verdict."""

    def __init__(self, bound, maximum, context):
        super().__init__(f"{bound} limit {maximum} exceeded while examining {context}",
                         code="WORK_TRAVERSAL_LIMIT")
        self.bound, self.maximum, self.context = bound, maximum, context


def key_of(ref) -> str:
    return f"{ref['collection']}:{ref['id']}"


def ref_of(record: Record) -> dict:
    return {"collection": record.collection, "id": record.id}


def pinned_of(record: Record) -> dict:
    return {"collection": record.collection, "id": record.id, "version": record.version}


def obligation_id(audit_id, target: dict, kind: str, role: str) -> str:
    payload = compact_json([audit_id, target["collection"], target["id"], kind, role]).encode("utf-8")
    return "obl_" + hashlib.sha256(payload).hexdigest()


class Snapshot:
    """Live records at one revision with relation, facet and ownership lookups."""

    def __init__(self, db: Database, revision: int, *, limits=None):
        top = db.max_revision()
        if not isinstance(revision, int) or revision < 1 or revision > top:
            raise InvalidRequest(f"revision {revision!r} is not in 1..{top}", code="REVISION_RANGE")
        self.db = db
        self.revision = revision
        self._records: dict = {}
        self._by_collection: dict = defaultdict(list)
        self._rel: dict = defaultdict(list)
        self._facets: dict = {}
        self._bindings: dict = {}
        self._versions: dict = {}
        self._children = defaultdict(list)
        self._boundaries = defaultdict(list)
        limits = limits or {}
        self.max_records = limits.get("max_records", 100000)
        self.max_relations = limits.get("max_relations", 500000)
        for name, value in (("max_records", self.max_records), ("max_relations", self.max_relations)):
            if type(value) is not int or value < 1:
                raise InvalidRequest(f"{name} must be a positive integer", code="WORK_LIMIT")
        self.relation_visits = 0
        # Bound materialization before reading large bodies. This counts the same
        # live snapshot as records_at, including historical snapshot selection.
        count = db.conn.execute("""SELECT COUNT(*) FROM record_versions v
            WHERE v.revision <= ? AND v.retired = 0 AND v.version =
            (SELECT MAX(w.version) FROM record_versions w WHERE w.collection = v.collection
             AND w.id = v.id AND w.revision <= ?)""", (revision, revision)).fetchone()[0]
        if count > self.max_records:
            raise TraversalLimit("records", self.max_records, f"revision {revision}")
        for record in db.records_at(revision):
            self._records[(record.collection, record.id)] = record
            self._by_collection[record.collection].append(record)
            if record.collection == "items" and record.body["kind"] in INTERMEDIATE_KINDS:
                self._children[record.body["owner_id"]].append(record)
            if record.collection == "proof_boundaries":
                for argument_id in record.body["argument_ids"]:
                    self._boundaries[argument_id].append(record)
            for row in extract_refs(record.collection, record.body):
                self.visit_relations(1, key_of(ref_of(record)))
                self._rel[(row["field_path"], row["target_collection"], row["target_id"])].append(
                    (record.collection, record.id, record.version))

    # -- record access -----------------------------------------------------
    def live(self, collection: str, id: str) -> Record | None:
        return self._records.get((collection, id))

    def get(self, ref) -> Record | None:
        if ref is None:
            return None
        return self.live(ref["collection"], ref["id"])

    def version(self, collection, id, version):
        current = self.live(collection, id)
        if current is not None and current.version == version:
            return current
        key = (collection, id, version)
        if key not in self._versions:
            self._versions[key] = self.db.version(collection, id, version)
        return self._versions[key]

    def all(self, collection: str) -> list:
        return list(self._by_collection.get(collection, []))

    def application(self, use):
        from .semantics import application
        return application(self, use)

    def exact_scope(self, ref):
        spec = self.target_spec(ref)
        record = self.get(ref)
        return spec.body["scope_id"] if spec else record.body.get("scope_id") if record else None

    def target_spec(self, ref):
        rows = self._rel.get(("/target", ref["collection"], ref["id"]), ())
        specs = [self.live(c, i) for c, i, _ in rows if c == "target_specs"]
        self.visit_relations(len(rows), key_of(ref))
        return specs[0] if len(specs) == 1 else None

    def proof_boundaries(self, argument_id):
        boundaries = self._boundaries.get(argument_id, ())
        self.visit_relations(len(boundaries), f"arguments:{argument_id}")
        return list(boundaries)

    def relation_members(self, relation: str, key: dict) -> list:
        owners, field_path, _ = RELATIONS[relation]
        if relation == "uses_in_group":
            owners = ("uses", "application_details")
        members = self._rel.get((field_path, key["collection"], key["id"]), [])
        self.visit_relations(len(members), key_of(key))
        if relation == "uses_in_group":
            translated = set()
            for c, i, version in members:
                if c == "application_details":
                    detail = self.live(c, i)
                    use = self.live("uses", detail.body["use_id"])
                    if use is not None:
                        translated.add(("uses", use.id, use.version))
                elif c == "uses" and self.live("application_details", i) is None:
                    translated.add((c, i, version))
            return sorted(translated)
        return [m for m in members if m[0] in owners]

    def visit_relations(self, count, context):
        self.relation_visits += count
        if self.relation_visits > self.max_relations:
            raise TraversalLimit("relation visits", self.max_relations, context)

    def scope_assumptions(self, scope_id):
        """Exact identities in scope ancestry, without interpreting their text."""
        found, seen = {}, set()
        while scope_id is not None and scope_id not in seen:
            seen.add(scope_id)
            scope = self.live("scopes", scope_id)
            if scope is None:
                break
            self.visit_relations(1 + len(scope.body["assumptions"]), f"scopes:{scope_id}")
            found.update((key_of(ref), ref) for ref in scope.body["assumptions"])
            scope_id = scope.body["parent_id"]
        return found

    def groups_for_conclusion(self, ref):
        rows = self._rel.get(("/conclusion", ref["collection"], ref["id"]), [])
        self.visit_relations(len(rows), key_of(ref))
        return [self.live(c, i) for c, i, _ in rows if c == "groups"]

    def member_records(self, relation: str, key: dict) -> list:
        records = [self.live(c, i) for c, i, _ in self.relation_members(relation, key)]
        if relation == "uses_in_group":
            records = [self.live("uses", r.body["use_id"]) if r is not None
                       and r.collection == "application_details" else r for r in records]
        return sorted((r for r in records if r is not None), key=lambda r: r.id)

    def facets(self, record: Record) -> dict:
        key = (record.collection, record.id, record.version)
        if key not in self._facets:
            self._facets[key] = facet_digests(record.collection, record.body)
        return self._facets[key]

    def binding(self, record: Record):
        key = (record.collection, record.id, record.version)
        if key not in self._bindings:
            self._bindings[key] = self.db.binding(*key)
        return self._bindings[key]

    def source_context_digest(self) -> str:
        rows = sorted([s.id, s.version, s.body["blob_sha256"]] for s in self.all("sources"))
        return digest(rows)

    # -- ownership ---------------------------------------------------------
    def major_of(self, ref) -> Record | None:
        """The major item owning an item or part reference (implementation-handoff 7.2)."""
        if ref is None:
            return None
        if ref["collection"] == "parts":
            part = self.live("parts", ref["id"])
            if part is None:
                return None
            ref = {"collection": "items", "id": part.body["item_id"]}
        if ref["collection"] != "items":
            return None
        item = self.live("items", ref["id"])
        if item is None:
            return None
        if item.body["kind"] in MAJOR_KINDS:
            return item
        if item.body["owner_id"] is None:
            return None
        owner = self.live("items", item.body["owner_id"])
        if owner is None or owner.body["kind"] not in MAJOR_KINDS:
            return None
        return owner

    def owner_of(self, ref) -> Record | None:
        """Major owner of any assessed record (items, parts, uses, groups, arguments)."""
        if ref is None:
            return None
        c = ref["collection"]
        if c in ("items", "parts"):
            return self.major_of(ref)
        record = self.get(ref)
        if record is None:
            return None
        if c == "uses":
            return self.major_of(record.body["to"])
        if c == "groups":
            return self.major_of(record.body["conclusion"])
        if c == "arguments":
            return self.major_of(record.body["target"])
        if c in ("target_specs", "proof_boundaries"):
            return self.major_of(record.body["target"])
        if c == "application_details":
            return self.owner_of({"collection": "uses", "id": record.body["use_id"]})
        if c == "connection_refinements":
            return self.owner_of({"collection": "arguments", "id": record.body["argument_id"]})
        return None

    def kind_of(self, ref) -> str | None:
        record = self.get(ref)
        if record is None:
            return None
        if record.collection == "parts":
            item = self.live("items", record.body["item_id"])
            return None if item is None else item.body["kind"]
        return record.body["kind"]

    def intermediates_of(self, item_id: str) -> list:
        children = self._children.get(item_id, ())
        self.visit_relations(len(children), f"items:{item_id}")
        return sorted(children, key=lambda r: r.id)

    def family(self, statement: Record) -> list:
        """The statement plus the intermediate claims owned by its major item."""
        owner_id = statement.body["item_id"] if statement.collection == "parts" else statement.id
        members = [statement]
        if statement.collection == "items" and statement.body["kind"] in INTERMEDIATE_KINDS:
            return members
        members.extend(self.intermediates_of(owner_id))
        return members


# -- freshness ---------------------------------------------------------------
def judgment_freshness(snap: Snapshot, record: Record, *, superseded: bool, reuse_index=None) -> dict:
    """Derived freshness of one bound judgment (record-contract 6)."""
    info = {"freshness": "current", "reused": False, "unbound": False, "changes": None,
            "context_changed": False}
    if superseded:
        info["freshness"] = "historical"
        return info
    if record.collection == "observations" and record.body.get("context_kind") == "overview" \
            and (record.body.get("context_data") or {}).get("applicable_on_import") is False:
        info["freshness"] = "needs_review"
        return info
    target = record.body.get("target") if record.collection != "reuse_decisions" else record.body["check_ref"]
    if isinstance(target, dict) and "collection" in target and snap.get(target) is None:
        info["freshness"] = "historical"
        return info
    binding = snap.binding(record)
    if binding is None:
        info["unbound"] = True
        return info
    bound = binding["bindings"] if "bindings" in binding else binding
    if record.collection == "checks" and record.body["role"] == "independent" \
            and record.body.get("response_id") and not bound.get("neutral_setup_validated"):
        # Older mapped checks may bind newer setup that their reviewer never
        # received. Recheck immutable response provenance without rewriting it.
        response = snap.live("responses", record.body["response_id"])
        original = snap.db.packet(response.body["packet_id"]) if response else None
        if original is not None:
            from .packets import independent_context_changes
            try:
                context_changes = independent_context_changes(snap, original["manifest"])
            except (ConflictError, InvalidRequest) as exc:
                context_changes = {"records": [{"ref": record.pinned, "facet": "independent_context",
                    "expected": "delivered source and applicable setup", "actual": None, "reason": str(exc)}],
                    "relations": []}
            if context_changes["records"] or context_changes["relations"]:
                info.update(freshness="needs_review", changes=context_changes, context_changed=True)
                return info
    info["context_changed"] = bound.get("source_context_digest") not in (None, snap.source_context_digest())
    changes = binding_changes(snap, bound)
    info["context_changed"] = info["context_changed"] or any(
        change["ref"]["collection"] == "sources" for change in changes["records"])
    if not changes["records"] and not changes["relations"]:
        return info
    info["changes"] = changes
    if isinstance(target, dict) and "collection" in target:
        for change in changes["records"]:
            if change["ref"]["collection"] == target["collection"] and change["ref"]["id"] == target["id"]:
                info["freshness"] = "historical"
                return info
    info["freshness"] = "needs_review"
    if reuse_index:
        for decision in reuse_index.get((record.collection, record.id, record.version), []):
            own = snap.binding(decision)
            if own is None:
                continue
            own_changes = binding_changes(snap, own["bindings"])
            if not own_changes["records"] and not own_changes["relations"]:
                info["freshness"] = "current"
                info["reused"] = True
                info["reuse_decision"] = pinned_of(decision)
                break
    return info


# -- reducer (architecture 9.3, shared with the projection) --------------------
def _explain(parts):
    return "; ".join(p for p in parts if p)


def reduce(constituents: list, *, independent: str = "not_required") -> dict:
    """Combine constituents into one assessment.

    A constituent is ``{obligation_id, required, kind, state, substantive, outcome, freshness, reused,
    support, check_refs, finding_refs, disputed, compromised}``. ``state`` is missing|draft|complete.
    """
    check_refs, finding_refs, missing = [], [], []
    seen_checks, seen_findings = set(), set()
    for c in constituents:
        for ref in c.get("check_refs", ()):
            k = (ref["collection"], ref["id"], ref.get("version"))
            if k not in seen_checks:
                seen_checks.add(k)
                check_refs.append(ref)
        for ref in c.get("finding_refs", ()):
            k = (ref["collection"], ref["id"], ref.get("version"))
            if k not in seen_findings:
                seen_findings.add(k)
                finding_refs.append(ref)
    if independent not in INDICATORS:
        raise ValueError(f"unknown independent indicator {independent!r}")
    base = {"check_refs": check_refs, "finding_refs": finding_refs, "missing_obligation_ids": missing,
            "independent_review": independent}

    def satisfied(c):
        return c["state"] == "complete" and c.get("freshness") == "current" and not c.get("disputed")

    for c in constituents:
        if c.get("required") and not satisfied(c) and c.get("obligation_id"):
            missing.append(c["obligation_id"])
    missing.sort()
    # Colors follow primary work; independent and coordinator work drive the indicator, the obligation
    # list and progress (architecture 9.3: green does not mean independently reviewed).
    color = [c for c in constituents if c.get("role", "primary") == "primary"]
    disputed = any(c.get("disputed") for c in constituents) or independent == "disputed"

    # 1. current confirmed direct defect beats everything else
    defects = [c for c in color if c["state"] == "complete" and c.get("freshness") == "current"
               and c.get("outcome") in DEFECT_OUTCOMES and c["kind"] in PROOF_CHECK_KINDS]
    if defects:
        kinds = sorted({c["kind"] for c in defects})
        note = "" if not missing else f"{len(missing)} required obligation(s) also incomplete"
        return {"state": "red", "label": "defect",
                "explanation": _explain([f"current completed {', '.join(kinds)} assessment identifies "
                                         f"{'/'.join(sorted({c['outcome'] for c in defects}))}", note]), **base}
    # 2. open independent disagreement stays amber until reconciled
    if disputed:
        return {"state": "amber", "label": "disputed",
                "explanation": "completed assessments disagree and have no explicit resolution",
                **base}
    # 3. nothing assessed
    substantive = [c for c in color if c["state"] == "complete"
                   or (c["state"] == "draft" and c.get("substantive"))]
    if not substantive:
        return {"state": "gray", "label": "unassessed",
                "explanation": "no completed or substantive draft assessment exists for the represented work"
                if constituents else "no work is represented", **base}
    # 4. every required constituent current, supported, with available dependency support
    reasons = []
    for c in color:
        if not c.get("required"):
            continue
        if c["state"] == "missing":
            reasons.append("partial")
        elif c["state"] == "draft":
            reasons.append("partial")
        elif c.get("freshness") == "needs_review":
            reasons.append("stale")
        elif c.get("freshness") == "historical":
            reasons.append("historical defect" if c.get("outcome") in DEFECT_OUTCOMES else "historical")
        elif c.get("outcome") == "inconclusive":
            reasons.append("inconclusive")
        elif c.get("outcome") in DEFECT_OUTCOMES:
            reasons.append("historical defect")
        elif c.get("outcome") == "needs_attention":
            reasons.append("source attention")
        elif c.get("outcome") not in ("supported", None):
            reasons.append(str(c.get("outcome")))
        if c.get("support") == "conditional":
            reasons.append("conditional")
        elif c.get("support") == "unavailable":
            reasons.append("premise unavailable")
        if c.get("compromised"):
            reasons.append("compromised independence")
    for c in color:
        if not c.get("required") and c["state"] == "complete" and c.get("freshness") == "historical" \
                and c.get("outcome") in DEFECT_OUTCOMES:
            reasons.append("historical defect against a changed input; recheck qualification pending")
    if not reasons and any(c.get("required") for c in color):
        reused = any(c.get("reused") for c in color)
        return {"state": "green", "label": "supported",
                "explanation": "every represented use and required supporting derivation is currently "
                               "supported with dependency support available under declared premises"
                               + (" (some work carried by accepted reuse decisions)" if reused else ""), **base}
    if not reasons:
        reasons.append("mixed: work exists but nothing is required here")
    ordered = []
    for r in reasons:
        if r not in ordered:
            ordered.append(r)
    label = ordered[0].split(";")[0].split(":")[0]
    return {"state": "amber", "label": label, "explanation": _explain(ordered), **base}


# -- derivation ----------------------------------------------------------------
class _Derivation:
    def __init__(self, snap: Snapshot, audit: Record | None):
        self.snap = snap
        self.audit = audit
        self.audit_id = None if audit is None else audit.id
        self.problems: list = []
        self.obligations: dict = {}          # id -> obligation dict
        self.by_target: dict = defaultdict(list)   # key -> [obligation ids]
        self.constituents: dict = {}         # obligation id -> constituent
        self.judgments: dict = {}            # "checks:ID" -> info
        self.judgment_changes: dict = {}    # internal freshness explanations, not a public record dump
        self.support_closure = SupportClosure(self)
        self.statements: list = []           # in-scope items/parts records
        self.routes: dict = {}               # statement key -> list of argument ids
        self.route_records: dict = {}        # statement key -> set of keys (arguments/groups/uses)
        self._index()

    # -- indexes ---------------------------------------------------------
    def _index(self):
        snap = self.snap
        self.superseded = set()
        self.checks_by_target = defaultdict(list)
        self.all_checks = []
        for check in snap.all("checks"):
            if self.audit_id is not None and check.body["audit_id"] != self.audit_id:
                continue
            self.all_checks.append(check)
            if check.body["supersedes"] is not None:
                self.superseded.add(check.body["supersedes"]["id"])
        for check in self.all_checks:
            self.checks_by_target[key_of(check.body["target"])].append(check)
        self.observations_by_target = defaultdict(list)
        for obs in snap.all("observations"):
            self.observations_by_target[key_of(obs.body["target"])].append(obs)
        self.reconciliations = []
        superseded_rec = set()
        for rec in snap.all("reconciliations"):
            if self.audit_id is not None and rec.body["audit_id"] != self.audit_id:
                continue
            self.reconciliations.append(rec)
            if rec.body["supersedes"] is not None:
                superseded_rec.add(rec.body["supersedes"]["id"])
        self.superseded_reconciliations = superseded_rec
        self.reuse_index = defaultdict(list)
        for decision in snap.all("reuse_decisions"):
            if decision.body["decision"] == "reusable":
                ref = decision.body["check_ref"]
                self.reuse_index[("checks", ref["id"], ref["version"])].append(decision)
        self.responses = {r.id: r for r in snap.all("responses")}
        self._route_review_basis = {}
        self.findings = []
        self.findings_by_target = defaultdict(list)
        self.findings_by_check = defaultdict(list)
        self.findings_by_use = defaultdict(list)
        for finding in snap.all("findings"):
            if self.audit_id is not None and finding.body["audit_id"] != self.audit_id:
                continue
            self.findings.append(finding)
            self.findings_by_target[key_of(finding.body["target"])].append(finding)
            for ref in finding.body["check_refs"]:
                self.findings_by_check[ref["id"]].append(finding)
            for use_id in finding.body["affected_uses"]:
                self.findings_by_use[use_id].append(finding)

    def judgment_info(self, check: Record) -> dict:
        key = key_of(pinned_of(check))
        if key in self.judgments:
            return self.judgments[key]
        info = judgment_freshness(self.snap, check, superseded=check.id in self.superseded,
                                  reuse_index=self.reuse_index)
        response = self.responses.get(check.body["response_id"]) if check.body["response_id"] else None
        info.update({"ref": pinned_of(check), "kind": check.body["kind"], "target": check.body["target"],
                     "role": check.body["role"], "state": check.body["state"], "outcome": check.body["outcome"],
                     "reviewer": check.body["reviewer"], "audit_id": check.body["audit_id"],
                     "substantive": bool(check.body["reasoning"].strip()),
                     "superseded": check.id in self.superseded,
                     "response_state": None if response is None else response.body["state"],
                     "exposure": None if response is None else response.body["exposure"],
                     "revision": check.revision})
        changes = info.pop("changes", None)
        if changes:
            self.judgment_changes[key] = changes
        self.judgments[key] = info
        return info

    def finding_refs(self, target_key: str, check_ids=(), use_id=None) -> list:
        found = list(self.findings_by_target.get(target_key, []))
        for cid in check_ids:
            found.extend(self.findings_by_check.get(cid, []))
        if use_id is not None:
            found.extend(self.findings_by_use.get(use_id, []))
        seen, refs = set(), []
        for f in sorted(found, key=lambda r: r.id):
            if f.id not in seen and f.body["lifecycle"] != "superseded":
                seen.add(f.id)
                refs.append(pinned_of(f))
        return refs

    # -- obligations -------------------------------------------------------
    def add_obligation(self, target: dict, kind: str, role: str, *, required: bool) -> str:
        oid = obligation_id(self.audit_id, target, kind, role)
        existing = self.obligations.get(oid)
        if existing is not None:
            existing["required"] = existing["required"] or required
            return oid
        self.obligations[oid] = {"id": oid, "target": {"collection": target["collection"], "id": target["id"]},
                                 "kind": kind, "role": role, "required": required}
        self.by_target[key_of(target)].append(oid)
        return oid

    def scope_statements(self) -> list:
        """Close accepted scope over consumed identities and exact establishing routes.

        The proof-required flag is context-sensitive: seeing a claim first as a
        local assumption cannot hide a later unconditional use of that claim.
        """
        snap, audit = self.snap, self.audit
        excluded = {key_of(e["target"]) for e in audit.body["exclusions"] if e.get("target")}
        self.required_establishment = set()
        self.scope_routes = defaultdict(set)
        self.scope_groups = defaultdict(set)
        self.scope_uses = defaultdict(set)
        self.scope_diagnostics = []
        pending = deque()
        for target in audit.body["targets"]:
            record = snap.get(target)
            if record is not None and record.collection == "items" and record.body["kind"] in INTERMEDIATE_KINDS:
                self.problems.append(f"audit target {key_of(target)} is an intermediate result")
            else:
                pending.append((target, True))
        if audit.body["mode"] == "full":
            pending.extend((ref_of(r), r.body["kind"] not in ("assumption", "definition", "external_result"))
                           for r in snap.all("items"))
            pending.extend((a.body["target"], True) for a in snap.all("arguments")
                           if a.body["lifecycle"] == "registered")
        ordered, seen, explored = [], set(), set()

        def excluded_ref(ref):
            record = snap.get(ref)
            return key_of(ref) in excluded or (record is not None and record.collection == "parts"
                and key_of({"collection": "items", "id": record.body["item_id"]}) in excluded)

        def consume(use, skey, argument=None):
            self.scope_uses[skey].add(use.id)
            application = snap.application(use)
            group = snap.live("groups", application.get("group_id"))
            scope_id = application.get("scope_id") or (group.body["scope_id"] if group else None)
            if scope_id is None and argument is not None:
                scope_id = argument.body["scope_id"]
            assumptions = snap.scope_assumptions(scope_id)
            pending.append((use.body["from"], key_of(use.body["from"]) not in assumptions))
            pending.extend((ref, False) for ref in assumptions.values())

        while pending:
            ref, proof_required = pending.popleft()
            skey = key_of(ref)
            if excluded_ref(ref) or (skey, proof_required) in explored:
                continue
            explored.add((skey, proof_required))
            snap.visit_relations(1, skey)
            statement = snap.get(ref)
            if statement is None:
                self.problems.append(f"required target {skey} is not live at revision {snap.revision}")
                continue
            if statement.collection not in ("items", "parts"):
                continue
            if skey not in seen:
                seen.add(skey)
                ordered.append(statement)
            kind = snap.kind_of(ref)
            if proof_required and (kind in PROOF_KINDS or kind in INTERMEDIATE_KINDS):
                self.required_establishment.add(skey)
            pending.extend((r, False) for r in snap.scope_assumptions(snap.exact_scope(ref)).values())
            if not proof_required:
                continue
            if statement.collection == "items" and kind not in INTERMEDIATE_KINDS:
                pending.extend((ref_of(p), True) for p in snap.member_records("parts_of_item", ref))
            arguments = {a.id: a for a in snap.member_records("arguments_for_target", ref)
                         if a.body["lifecycle"] != "retired"}
            # Written child routes remain accountable but cannot establish their parent.
            for member in snap.family(statement)[1:]:
                for arg in snap.member_records("arguments_for_target", ref_of(member)):
                    if arg.body["lifecycle"] != "retired":
                        arguments[arg.id] = arg
                        pending.append((ref_of(member), True))
            local_groups = []
            if statement.collection == "parts" or kind in INTERMEDIATE_KINDS:
                local_groups = [g for g in snap.groups_for_conclusion(ref)
                                if (a := snap.live("arguments", g.body["argument_id"])) is not None
                                and a.body["lifecycle"] == "registered"]
                for group in local_groups:
                    self.scope_groups[skey].add(group.id)
                    argument = snap.live("arguments", group.body["argument_id"])
                    pending.extend((r, False) for r in snap.scope_assumptions(group.body["scope_id"]).values())
                    for use in snap.member_records("uses_in_group", ref_of(group)):
                        consume(use, skey, argument)
                if statement.collection == "parts" and not local_groups and not arguments:
                    parent = {"collection": "items", "id": statement.body["item_id"]}
                    arguments.update((a.id, a) for a in snap.member_records("arguments_for_target", parent)
                                     if a.body["lifecycle"] == "registered")
            for argument in arguments.values():
                self.scope_routes[skey].add(argument.id)
                pending.extend((r, False) for r in snap.scope_assumptions(argument.body["scope_id"]).values())
                for group in snap.member_records("groups_in_argument", ref_of(argument)):
                    self.scope_groups[skey].add(group.id)
                    pending.append((group.body["conclusion"], False))
                    for scope_id in [group.body["scope_id"], *group.body["case_scope_ids"], *group.body["discharges"]]:
                        pending.extend((r, False) for r in snap.scope_assumptions(scope_id).values())
                    for use in snap.member_records("uses_in_group", ref_of(group)):
                        consume(use, skey, argument)
            for use in snap.member_records("incoming_uses", ref):
                # Already traversed grouped uses are not an additional route.
                if snap.live("application_details", use.id) is not None \
                        and snap.application(use).get("group_id") is None:
                    consume(use, skey)
            if skey in self.required_establishment:
                exact = [a for a in arguments.values() if a.body["target"] == ref
                         and a.body["lifecycle"] == "registered"]
                established = bool(exact or local_groups or (statement.collection == "parts" and arguments))
                if not established:
                    message = f"register establishment for {skey}: no exact registered proof route or local group"
                    self.problems.append(message)
                    self.scope_diagnostics.append({"code": "register_establishment", "target_refs": [ref],
                                                   "message": message, "required": True})
        return ordered

    def derive_obligations(self):
        snap, audit = self.snap, self.audit
        self.statements = self.scope_statements()
        independent_required = bool(audit.body["independent_required"])
        fidelity_required = audit.body["mode"] in ("full", "focused")
        for statement in self.statements:
            skey = key_of(ref_of(statement))
            kind = snap.kind_of(ref_of(statement))
            route_keys = set()
            argument_ids = []
            for argument_id in sorted(self.scope_routes.get(skey, ())):
                argument = snap.live("arguments", argument_id)
                if argument.body["lifecycle"] == "retired":
                    continue
                required = argument.body["lifecycle"] == "registered"
                argument_ids.append(argument.id)
                route_keys.add(key_of(ref_of(argument)))
                self.add_obligation(ref_of(argument), "composition", "primary", required=required)
                if independent_required and kind in PROOF_KINDS:
                    self.add_obligation(ref_of(argument), "composition", "independent", required=required)
            for group_id in sorted(self.scope_groups.get(skey, ())):
                group = snap.live("groups", group_id)
                argument = snap.live("arguments", group.body["argument_id"])
                required = argument is not None and argument.body["lifecycle"] == "registered"
                route_keys.add(key_of(ref_of(group)))
                self.add_obligation(ref_of(group), "derivation", "primary", required=required)
                if group.body["kind"] == "cases":
                    self.add_obligation(ref_of(group), "case_coverage", "primary", required=required)
                if group.body["discharges"]:
                    self.add_obligation(ref_of(group), "scope_discharge", "primary", required=required)
            for use_id in sorted(self.scope_uses.get(skey, ())):
                use = snap.live("uses", use_id)
                route_keys.add(key_of(ref_of(use)))
                argument = None
                application = snap.application(use)
                if application.get("group_id"):
                    group = snap.live("groups", application["group_id"])
                    argument = snap.live("arguments", group.body["argument_id"])
                self.add_obligation(ref_of(use), "application", "primary",
                                    required=argument is None or argument.body["lifecycle"] == "registered")
            exact_arguments = [a for a in snap.member_records("arguments_for_target", ref_of(statement))
                               if a.body["lifecycle"] == "registered"]
            if skey in self.required_establishment and statement.collection == "items" \
                    and kind in PROOF_KINDS and not exact_arguments:
                # a proof-required result with no written route: the composition obligation is missing
                self.add_obligation(ref_of(statement), "composition", "primary", required=True)
            if kind == "external_result":
                self.add_obligation(ref_of(statement), "external_source", "primary", required=True)
            if statement.body["origin"] == "source" and statement.body["passages"]:
                self.add_obligation(ref_of(statement), "source_fidelity", "primary", required=fidelity_required)
                spec = snap.target_spec(ref_of(statement))
                if spec is not None:
                    self.add_obligation(ref_of(spec), "source_fidelity", "primary", required=fidelity_required)
            if independent_required and kind in PROOF_KINDS and skey in self.required_establishment:
                self.add_obligation(ref_of(statement), "reconciliation", "coordinator", required=True)
            self.routes[skey] = argument_ids
            self.route_records[skey] = route_keys
        for task in audit.body["global_tasks"]:
            self.add_obligation(ref_of(audit), task["kind"], "primary", required=task["applicability"] == "required")

    # -- obligation status -------------------------------------------------
    @staticmethod
    def _observation_order(record) -> tuple:
        """Chronological order for source-fidelity candidates.

        Migration preserves native append order even when timestamps have only
        second precision. Later SQL revisions outrank imported observations.
        """
        created = record.body.get("created_at")
        order = (record.body.get("context_data") or {}).get("observation_order", 0)
        return (record.revision or 0, order if isinstance(order, int) else 0,
                created if isinstance(created, str) else "", record.id)

    def _candidates(self, obligation: dict) -> list:
        target_key = key_of(obligation["target"])
        kind, role = obligation["kind"], obligation["role"]
        if kind == "source_fidelity":
            observations = self.observations_by_target.get(target_key, [])
            target = self.snap.get(obligation["target"])
            if not observations and target is not None and target.collection == "target_specs":
                fidelity = target.body.get("fidelity_ref")
                observation = self.snap.get(fidelity)
                if observation is not None and observation.version == fidelity["version"]:
                    observations = [observation]
            return sorted(observations, key=self._observation_order)
        if kind == "reconciliation":
            return []
        out = []
        for check in self.checks_by_target.get(target_key, []):
            if check.body["kind"] != kind or check.body["role"] != role:
                continue
            out.append(check)
        return sorted(out, key=lambda r: (r.revision, r.id))

    def status(self, obligation: dict) -> dict:
        """Fill state/satisfied/freshness/outcome/check_refs and a constituent for one obligation."""
        kind = obligation["kind"]
        target_key = key_of(obligation["target"])
        constituent = {"obligation_id": obligation["id"], "required": obligation["required"], "kind": kind,
                       "role": obligation["role"], "target": obligation["target"], "state": "missing",
                       "substantive": False, "outcome": None, "freshness": None, "reused": False, "support": None,
                       "check_refs": [], "finding_refs": [], "disputed": False, "compromised": False}
        if kind == "reconciliation":
            self._reconciliation_status(obligation, constituent)
        elif kind == "source_fidelity":
            candidates = self._candidates(obligation)
            if candidates:
                active = candidates[-1]
                info = judgment_freshness(self.snap, active, superseded=False)
                if info.get("changes"):
                    self.judgment_changes[key_of(pinned_of(active))] = info["changes"]
                constituent.update({"state": "complete", "substantive": True,
                                    "outcome": "supported" if active.body["result"] == "matched" else "needs_attention",
                                    "freshness": info["freshness"], "check_refs": [pinned_of(active)]})
        else:
            candidates = [c for c in self._candidates(obligation) if c.id not in self.superseded]
            infos = [self.judgment_info(c) for c in candidates]
            if obligation["role"] == "independent":
                usable = [i for i in infos if self.independent_usable(i)]
                if infos and not usable:
                    constituent["compromised"] = True
                infos = usable
            complete = [i for i in infos if i["state"] == "complete"]
            current = [i for i in complete if i["freshness"] == "current"]
            pool = current or complete or infos
            if pool:
                active = max(pool, key=lambda i: (i["revision"], i["ref"]["version"], i["ref"]["id"]))
                constituent.update({"state": active["state"], "substantive": active["substantive"],
                                    "outcome": active["outcome"], "freshness": active["freshness"],
                                    "reused": active["reused"], "check_refs": [active["ref"]]})
            constituent["check_refs"] = [i["ref"] for i in infos] or constituent["check_refs"]
            # All current candidates consume the current obligation inputs. A later
            # row cannot silently resolve their disagreement; only supersession
            # removes a judgment from this pool. Keep both original outcomes in
            # judgments/check_refs and give the obligation a disputed aggregate.
            if len({i["outcome"] for i in current}) > 1:
                constituent.update({"state": "complete", "outcome": "inconclusive",
                                    "freshness": "current", "disputed": True})
        check_ids = [r["id"] for r in constituent["check_refs"]]
        constituent["finding_refs"] = self.finding_refs(
            target_key, check_ids, use_id=obligation["target"]["id"] if obligation["target"]["collection"] == "uses"
            else None)
        satisfied = constituent["state"] == "complete" and constituent["freshness"] == "current" \
            and not constituent["disputed"]
        obligation.update({"state": constituent["state"], "satisfied": satisfied,
                           "freshness": constituent["freshness"], "outcome": constituent["outcome"],
                           "check_refs": constituent["check_refs"],
                           "explanation": self._explain_obligation(obligation, constituent)})
        self.constituents[obligation["id"]] = constituent
        return constituent

    @staticmethod
    def _explain_obligation(obligation, c) -> str:
        if c["state"] == "missing":
            return f"no {obligation['role']} {obligation['kind']} work recorded"
        parts = [f"{obligation['role']} {obligation['kind']} {c['state']}"]
        if c["outcome"]:
            parts.append(f"outcome {c['outcome']}")
        if c["freshness"] and c["freshness"] != "current":
            parts.append(f"freshness {c['freshness']}")
        if c["reused"]:
            parts.append("carried by an accepted reuse decision")
        if c["compromised"]:
            parts.append("only compromised independent evidence exists")
        if c["disputed"]:
            parts.append("unresolved conflicting assessments")
        return "; ".join(parts)

    def _independent_checks_for(self, statement_key: str) -> list:
        keys = set(self.route_records.get(statement_key, ())) | {statement_key}
        out = []
        for key in keys:
            for check in self.checks_by_target.get(key, []):
                if check.body["role"] != "independent" or check.id in self.superseded:
                    continue
                info = self.judgment_info(check)
                if info["state"] == "complete":
                    out.append(info)
        return out

    def independent_usable(self, info):
        """Accept a supplied route only with its preserved initial blind review."""
        if info["response_state"] != "accepted":
            return False
        if info["exposure"] == "source_only":
            return True
        if info["exposure"] != "route_provided":
            return False
        check = self.snap.get(info["ref"])
        response = self.responses.get(check.body["response_id"]) if check is not None else None
        if response is None:
            return False
        if response.id not in self._route_review_basis:
            packet = self.snap.db.packet(response.body["packet_id"])
            self._route_review_basis[response.id] = {} if packet is None else packet["manifest"]
        manifest = self._route_review_basis[response.id]
        route_pin = manifest.get("route_ref")
        route = self.snap.get(route_pin)
        if manifest.get("review_basis") != "route_provided" or route is None \
                or route.version != route_pin["version"]:
            return False
        target = self.snap.get(info["target"])
        if target is None:
            return False
        argument_id = target.id if target.collection == "arguments" else target.body.get("argument_id")
        if target.collection == "uses":
            group = self.snap.live("groups", self.snap.application(target).get("group_id"))
            argument_id = group.body["argument_id"] if group else None
        if argument_id != route.id:
            return False
        owner = self.snap.major_of(route.body["target"])
        authorized_targets = {key_of(route.body["target"])}
        if owner is not None:
            authorized_targets.add(key_of(ref_of(owner)))
        for pin in manifest.get("initial_response_refs", ()):
            initial = self.responses.get(pin["id"])
            if initial is not None and initial.version == pin["version"] \
                    and initial.body["state"] == "accepted" and initial.body["exposure"] == "source_only" \
                    and initial.body["audit_id"] == response.body["audit_id"] \
                    and any(key_of(ref) in authorized_targets for ref in initial.body["covered_targets"]):
                return True
        return False

    def _primary_checks_for(self, statement_key: str) -> list:
        keys = set(self.route_records.get(statement_key, ())) | {statement_key}
        out = []
        for key in keys:
            for check in self.checks_by_target.get(key, []):
                if check.body["role"] == "primary" and check.id not in self.superseded:
                    info = self.judgment_info(check)
                    if info["state"] == "complete":
                        out.append(info)
        return out

    def _reconciliations_for(self, statement_key: str) -> list:
        keys = set(self.route_records.get(statement_key, ())) | {statement_key}
        return sorted((r for r in self.reconciliations
                       if key_of(r.body["target"]) in keys and r.id not in self.superseded_reconciliations),
                      key=lambda r: (r.revision, r.id))

    def independent_indicator(self, statement_key: str) -> str:
        if self.audit is None or not self.audit.body["independent_required"]:
            return "not_required"
        if self.snap.kind_of({"collection": statement_key.split(":")[0], "id": statement_key.split(":", 1)[1]}) \
                not in PROOF_KINDS:
            return "not_required"
        independent = self._independent_checks_for(statement_key)
        usable = [i for i in independent if self.independent_usable(i)]
        reconciliations = self._reconciliations_for(statement_key)
        for rec in reconciliations:
            if rec.body["decision"] == "unresolved":
                return "disputed"
        if independent and not usable and not reconciliations:
            return "compromised"
        if not usable:
            return "pending"
        current_ind = {(i["ref"]["id"], i["ref"]["version"]) for i in usable if i["freshness"] == "current"}
        reconciled = set()
        for rec in reconciliations:
            info = judgment_freshness(self.snap, rec, superseded=False)
            if info["freshness"] != "current":
                continue
            # Each row is exact-target evidence. A route can legitimately need
            # distinct application, group and composition reconciliations.
            listed = {(r["id"], r["version"]) for r in rec.body["independent_checks"]}
            if listed & current_ind:
                reconciled.update(listed)
        keys = set(self.route_records.get(statement_key, ())) | {statement_key}
        required = [obligation for target in keys for oid in self.by_target.get(target, ())
                    if (obligation := self.obligations[oid])["required"] and obligation["role"] == "independent"]
        for obligation in required:
            self.constituent_for(obligation["id"])
        required_complete = bool(required) and all(obligation["satisfied"] for obligation in required)
        # Empty current evidence is never coverage. Reconciled local checks are
        # insufficient while a required final composition/route is unexamined.
        if current_ind and current_ind <= reconciled and required_complete:
            return "complete"
        primary_outcomes = defaultdict(set)
        for info in self._primary_checks_for(statement_key):
            if info["freshness"] == "current":
                primary_outcomes[(key_of(info["target"]), info["kind"])].add(info["outcome"])
        for info in usable:
            if info["freshness"] == "current":
                outcomes = primary_outcomes.get((key_of(info["target"]), info["kind"]), set())
                if any(outcome != info["outcome"] for outcome in outcomes):
                    return "disputed"
        return "pending"

    def _reconciliation_status(self, obligation: dict, constituent: dict):
        statement_key = key_of(obligation["target"])
        indicator = self.independent_indicator(statement_key)
        reconciliations = self._reconciliations_for(statement_key)
        constituent["check_refs"] = [pinned_of(r) for r in reconciliations]
        constituent["substantive"] = bool(reconciliations)
        if indicator == "disputed":
            constituent.update({"state": "complete" if reconciliations else "missing", "outcome": "inconclusive",
                                "freshness": "current", "disputed": True})
        elif indicator == "complete":
            constituent.update({"state": "complete", "outcome": "supported", "freshness": "current"})
        elif indicator == "compromised":
            constituent.update({"state": "missing", "compromised": True})
        elif reconciliations:
            info = judgment_freshness(self.snap, reconciliations[-1], superseded=False)
            constituent.update({"state": "complete", "outcome": "inconclusive", "freshness": info["freshness"]})
            if info["freshness"] == "current":
                # a reconciliation exists but does not cover every current independent check
                constituent["freshness"] = "needs_review"

    def coverage_problems(self) -> list:
        """Account for declared written passages without interpreting their mathematics.

        Structural intervals need no verdict. Substantive intervals need claims
        and current, completed responsible checks in this audit. Missing coverage
        is unfinished work, not a malformed draft or a mathematical refutation.
        """
        # Keep compact facts for focused work preparation. Public assessment
        # expands only a bounded sample with the shared cause/action text.
        self.coverage_diagnostics = []
        if self.audit is None or self.audit.body["mode"] not in ("full", "focused"):
            return []
        snap = self.snap
        requirements = {}
        problems = []
        examined_boundaries = set()
        excluded = {anchor for e in self.audit.body["exclusions"] for anchor in e["source_anchor_ids"]}
        for statement in self.statements:
            statement_key = key_of(ref_of(statement))
            if statement_key not in self.required_establishment and not self.scope_routes.get(statement_key) \
                    and not self.scope_groups.get(statement_key):
                # A statement used as an explicit local premise supplies context,
                # not a request to inspect its separate proof text.
                continue
            for member in snap.family(statement):
                arguments = [a for a in snap.member_records("arguments_for_target", ref_of(member))
                             if a.body["lifecycle"] == "registered"]
                if member.collection == "parts" or self.snap.kind_of(ref_of(member)) in INTERMEDIATE_KINDS:
                    for group in snap.groups_for_conclusion(ref_of(member)):
                        argument = snap.live("arguments", group.body["argument_id"])
                        if argument is not None and argument.body["lifecycle"] == "registered" \
                                and argument.id not in {a.id for a in arguments}:
                            arguments.append(argument)
                written = [a for a in arguments if a.body["origin"] == "source"]
                for argument in written:
                    for anchor in argument.body["evidence_refs"]:
                        requirements[(argument.id, anchor)] = {argument.id}
                    if argument.id not in examined_boundaries:
                        examined_boundaries.add(argument.id)
                        boundary_anchors = self._reviewed_boundary(argument)
                        if boundary_anchors is None:
                            problems.append(f"proof boundary for {argument.id}: complete current source-boundary review required")
                        else:
                            for anchor in boundary_anchors:
                                requirements[(argument.id, anchor)] = {argument.id}
                if member.body["origin"] == "source":
                    for passage in member.body["passages"]:
                        if passage["role"] == "proof":
                            # A declared proof cannot disappear by omitting its
                            # evidence from the argument record.
                            requirements[(key_of(ref_of(member)), passage["anchor_id"])] = {
                                a.id for a in arguments}
        coverages = defaultdict(list)
        for cov in snap.all("coverage"):
            coverages[cov.body["anchor_id"]].append(cov)

        rejected = defaultdict(list)

        def reject(cov, code, **detail):
            rejected[cov.id].append({"code": code, "argument_ids": [cov.body["argument_id"]],
                "anchor_id": cov.body["anchor_id"], "coverage_ref": pinned_of(cov), **detail})
            return False

        def completed(cov):
            body = cov.body
            if body["classification"] == "structural":
                return (not body["claim_refs"] and not body["check_ids"]) or reject(cov, "structural_links")
            if not body["claim_refs"] or not body["check_ids"]:
                if not body["claim_refs"]:
                    reject(cov, "missing_claims")
                if not body["check_ids"]:
                    reject(cov, "missing_checks")
                return False
            consumed_claims = set()
            for check_id in body["check_ids"]:
                check = snap.live("checks", check_id)
                if check is None or check.body["audit_id"] != self.audit_id or check.body["role"] != "primary":
                    return reject(cov, "unusable_check", check_ref=ref_of(check) if check else
                                  {"collection": "checks", "id": check_id})
                target = snap.get(check.body["target"])
                if target is None:
                    return reject(cov, "unusable_check", check_ref=pinned_of(check))
                if target.collection == "arguments":
                    argument_id = target.id
                elif target.collection == "groups":
                    argument_id = target.body["argument_id"]
                elif target.collection == "uses" and snap.application(target).get("group_id") is not None:
                    group = snap.live("groups", snap.application(target)["group_id"])
                    argument_id = None if group is None else group.body["argument_id"]
                else:
                    return reject(cov, "wrong_argument", check_ref=pinned_of(check))
                if argument_id != body["argument_id"]:
                    return reject(cov, "wrong_argument", check_ref=pinned_of(check),
                                  check_argument_id=argument_id)
                obligation = self.obligation_for(check.body["target"], check.body["kind"])
                # A cited predecessor may be carried forward only through its
                # explicit successor chain, not through an unrelated newer row.
                pending, seen = [check], set()
                usable = False
                while pending:
                    candidate = pending.pop()
                    if candidate.id in seen:
                        continue
                    seen.add(candidate.id)
                    info = self.judgment_info(candidate)
                    if not info["superseded"] and info["state"] == "complete" and info["freshness"] == "current":
                        binding = snap.binding(candidate)
                        if binding is not None:
                            for entry in binding["bindings"]["records"]:
                                if entry["facet"] != "statement":
                                    continue
                                ref = entry["ref"]
                                if ref["collection"] == "target_specs":
                                    # Exact text may live only in its specification.
                                    # Resolve the consumed version, including in historical snapshots.
                                    spec = snap.version(ref["collection"], ref["id"], ref["version"])
                                    if spec is None or spec.retired:
                                        continue
                                    ref = spec.body["target"]
                                if ref["collection"] in ("items", "parts"):
                                    consumed_claims.add(key_of(ref))
                        usable = True
                        break
                    pending.extend(c for c in self.checks_by_target[key_of(check.body["target"])]
                                   if c.body["supersedes"] == pinned_of(candidate))
                if not usable:
                    info = self.judgment_info(check)
                    return reject(cov, "unusable_check", check_ref=pinned_of(check),
                                  state=info["state"], freshness=info["freshness"], superseded=info["superseded"])
                if obligation is None or not obligation.get("satisfied"):
                    return reject(cov, "unsatisfied_obligation", check_ref=pinned_of(check),
                                  obligation_id=None if obligation is None else obligation["id"])
            unconsumed = [ref for ref in body["claim_refs"] if key_of(ref) not in consumed_claims]
            return not unconsumed or reject(cov, "unconsumed_claims", claim_refs=unconsumed)

        usable = {cov.id: completed(cov) for rows in coverages.values() for cov in rows}
        reported = set()

        def uncovered(intervals, end):
            cursor, missing = 0, []
            for start, stop in sorted(intervals):
                if start < 0 or stop > end or stop < start:
                    continue
                if start > cursor:
                    missing.append([cursor, start])
                cursor = max(cursor, stop)
            if cursor < end:
                missing.append([cursor, end])
            return missing

        for (owner, anchor_id), argument_ids in sorted(requirements.items()):
            if anchor_id in excluded:
                continue
            anchor = snap.live("anchors", anchor_id)
            if anchor is None:
                problems.append(f"proof coverage for {owner}: anchor {anchor_id} is missing")
                self.coverage_diagnostics.append({"code": "missing_anchor", "argument_ids": sorted(argument_ids),
                                                  "owner": owner, "anchor_id": anchor_id})
                continue
            end = len(anchor.body["excerpt"])
            relevant = [c for c in coverages.get(anchor_id, []) if c.body["argument_id"] in argument_ids]
            missing = uncovered(((c.body["start_offset"], c.body["end_offset"])
                                 for c in relevant if usable[c.id]), end)
            if missing:
                spans = ", ".join(f"[{a}, {b})" for a, b in missing)
                problems.append(f"proof coverage for {owner}, anchor {anchor_id}: "
                                f"uncovered or unchecked spans {spans}")
                # Explain only rows that leave required spans unchecked. An
                # unused or redundant row must not create a new completion gate.
                for cov in relevant:
                    if cov.id in reported or not any(cov.body["start_offset"] < stop and
                            cov.body["end_offset"] > start for start, stop in missing):
                        continue
                    self.coverage_diagnostics.extend(rejected[cov.id])
                    reported.add(cov.id)
                absent = uncovered(((c.body["start_offset"], c.body["end_offset"]) for c in relevant), end)
                identity = (anchor_id, tuple(sorted(argument_ids)), tuple(map(tuple, absent)))
                if absent and identity not in reported:
                    self.coverage_diagnostics.append({"code": "missing_coverage", "argument_ids": sorted(argument_ids),
                        "owner": owner, "anchor_id": anchor_id, "spans": absent})
                    reported.add(identity)
        return problems

    def _reviewed_boundary(self, argument):
        """Return all reviewed source segments, or no coverage certificate."""
        snap = self.snap
        declared = set(argument.body["evidence_refs"])
        target = snap.get(argument.body["target"])
        if target is not None:
            declared.update(p["anchor_id"] for p in target.body.get("passages", ()) if p["role"] == "proof")
        for boundary in snap.proof_boundaries(argument.id):
            body = boundary.body
            if body["state"] != "complete" or body["target"] != argument.body["target"]:
                continue
            anchor_pins = body["anchor_refs"]
            if not anchor_pins or not declared <= {p["id"] for p in anchor_pins}:
                continue
            review_pin = body["source_review_ref"]
            review = snap.get(review_pin)
            if review is None or review.version != review_pin["version"] \
                    or review.body["decision"] != "accepted" or review.body["purpose"] != "proof_boundary":
                continue
            reviewed_anchors = {(r["id"], r["version"]) for r in review.body["anchor_refs"]}
            reviewed_sources = {(r["id"], r["version"]) for r in review.body["source_refs"]}
            current = True
            for pin in anchor_pins:
                anchor = snap.get(pin)
                if anchor is None or anchor.version != pin["version"] or (pin["id"], pin["version"]) not in reviewed_anchors:
                    current = False
                    break
                source = snap.live("sources", anchor.body["source_id"])
                if source is None or source.version != anchor.body["source_version"] \
                        or (source.id, source.version) not in reviewed_sources:
                    current = False
                    break
            if current:
                return [pin["id"] for pin in anchor_pins]
        return None

    # -- dependency support ----------------------------------------------
    def obligation_for(self, target: dict, kind: str, role: str = "primary"):
        return self.obligations.get(obligation_id(self.audit_id, target, kind, role))

    def _obligation_supported(self, target: dict, kind: str) -> str:
        """supported | defect | open for a primary obligation's current state."""
        if self.audit is not None and self.audit.body["mode"] == "triage":
            return "open"
        obligation = self.obligation_for(target, kind)
        if obligation is None:
            return "open"
        c = self.constituents.get(obligation["id"]) or self.status(obligation)
        if c["state"] == "complete" and c["freshness"] == "current" and not c["disputed"]:
            if c["outcome"] == "supported":
                record = self.snap.get(target)
                scope_id = None if record is None else record.body.get("scope_id")
                if record is not None and record.collection == "uses":
                    application = self.snap.application(record)
                    group = self.snap.live("groups", application.get("group_id"))
                    scope_id = application.get("scope_id") or (group.body["scope_id"] if group else None)
                elif record is not None and record.collection in ("items", "parts"):
                    scope_id = self.snap.exact_scope(target)
                conditions = {condition for _, scope in self.support_closure.ancestry(scope_id)
                              if scope is not None for condition in scope.body["conditions"]}
                for check_ref in c["check_refs"]:
                    check = self.snap.get(check_ref)
                    if check is not None and check.collection == "checks" \
                            and self.judgment_info(check)["freshness"] == "current" \
                            and check.body["outcome"] == "supported" \
                            and any(condition not in conditions for condition in check.body["conditions"]):
                        return "open"
                return "supported"
            if c["outcome"] in DEFECT_OUTCOMES:
                return "defect"
        return "open"

    def exact_target_current(self, ref):
        """A synopsis alone is not an exact mathematical checking target."""
        spec = self.snap.target_spec(ref)
        if spec is None or spec.body["state"] != "registered":
            return False
        pin = spec.body.get("statement_ref")
        if pin is not None:
            statement = self.snap.get(pin)
            if statement is None:
                return False
            if statement.version != pin["version"]:
                pinned = self.snap.version(pin["collection"], pin["id"], pin["version"])
                if pinned is None or self.snap.facets(statement)["statement"] != self.snap.facets(pinned)["statement"]:
                    return False
        fidelity = spec.body.get("fidelity_ref")
        direct = sorted(self.observations_by_target.get(key_of(ref_of(spec)), ()), key=self._observation_order)
        if direct:
            latest = direct[-1]
            return latest.body.get("result") == "matched" \
                and judgment_freshness(self.snap, latest, superseded=False)["freshness"] == "current"
        if fidelity is not None:
            observation = self.snap.get(fidelity)
            return observation is not None and observation.version == fidelity["version"] \
                and observation.body.get("result") == "matched" \
                and judgment_freshness(self.snap, observation, superseded=False)["freshness"] == "current"
        record = self.snap.get(ref)
        # Reconstructions have no source statement to compare. A source target
        # requires an explicit current comparison of its exact saved form.
        return record is not None and record.body.get("origin") != "source"

    def statement_refuted(self, ref):
        return any(f.body["category"] == "statement_refutation" and f.body["lifecycle"] == "open"
                   and judgment_freshness(self.snap, f, superseded=False)["freshness"] == "current"
                   for f in self.findings_by_target.get(key_of(ref), ()))

    def support(self, ref: dict, scope_id=None, *, default_scope=True) -> str:
        """Availability of an exact statement in the requested recorded context."""
        if self.audit is not None and self.audit.body["mode"] == "triage":
            return "conditional"
        if scope_id is None and default_scope:
            scope_id = self.snap.exact_scope(ref)
        return self.support_closure.value(self.support_closure.statement_key(ref, scope_id))

    def use_support(self, use: Record) -> str:
        """Supplier availability is separate from a locally valid implication."""
        if self.audit is not None and self.audit.body["mode"] == "triage":
            return "conditional"
        return self.support_closure.value(("use", use.id))

    # -- assessments per record ----------------------------------------------
    def constituent_for(self, oid: str) -> dict:
        c = self.constituents.get(oid)
        if c is None:
            c = self.status(self.obligations[oid])
        return c

    def use_constituents(self, use: Record) -> list:
        out = []
        for oid in self.by_target.get(key_of(ref_of(use)), []):
            c = dict(self.constituent_for(oid))
            c["support"] = self.use_support(use)
            out.append(c)
        if not out:
            out.append({"obligation_id": None, "required": False, "kind": "application", "role": "primary",
                        "target": ref_of(use), "state": "missing", "substantive": False, "outcome": None,
                        "freshness": None, "reused": False, "support": self.use_support(use), "check_refs": [],
                        "finding_refs": self.finding_refs(key_of(ref_of(use)), use_id=use.id), "disputed": False,
                        "compromised": False})
        return out

    def use_assessment(self, use: Record):
        constituents = self.use_constituents(use)
        local = reduce([dict(c, support=None) for c in constituents])
        assessment = reduce(constituents)
        assessment.update(availability=self.use_support(use), local_state=local["state"], local_label=local["label"])
        return _triage_assessment(assessment) if self.audit is not None and self.audit.body["mode"] == "triage" else assessment

    def group_constituents(self, group: Record, *, with_uses: bool = True) -> list:
        out = [dict(self.constituent_for(oid)) for oid in self.by_target.get(key_of(ref_of(group)), [])]
        if with_uses:
            for use in self.snap.member_records("uses_in_group", ref_of(group)):
                out.extend(self.use_constituents(use))
        return out

    def argument_constituents(self, argument: Record) -> list:
        out = [dict(self.constituent_for(oid)) for oid in self.by_target.get(key_of(ref_of(argument)), [])]
        for group in self.snap.member_records("groups_in_argument", ref_of(argument)):
            out.extend(self.group_constituents(group))
        return out

    def statement_constituents(self, statement: Record) -> list:
        skey = key_of(ref_of(statement))
        out = [dict(self.constituent_for(oid)) for oid in self.by_target.get(skey, [])]
        seen = {c["obligation_id"] for c in out}
        for key in sorted(self.route_records.get(skey, ())):
            for oid in self.by_target.get(key, []):
                if oid in seen:
                    continue
                seen.add(oid)
                c = dict(self.constituent_for(oid))
                if key.startswith("uses:"):
                    use = self.snap.live("uses", key.split(":", 1)[1])
                    if use is not None:
                        c["support"] = self.use_support(use)
                out.append(c)
        if statement.collection == "items":
            for part in self.snap.member_records("parts_of_item", ref_of(statement)):
                if key_of(ref_of(part)) in self.route_records:
                    for c in self.statement_constituents(part):
                        if c["obligation_id"] not in seen:
                            seen.add(c["obligation_id"])
                            out.append(c)
        return out

    def assess_all(self) -> dict:
        snap = self.snap
        assessments = {}
        for oid in list(self.obligations):
            self.constituent_for(oid)
        roots = [self.support_closure.statement_key(ref_of(s), snap.exact_scope(ref_of(s)))
                 for s in self.statements]
        roots.extend(("use", use_id) for use_ids in getattr(self, "scope_uses", {}).values() for use_id in use_ids)
        roots.extend(("argument", argument_id) for argument_ids in self.routes.values() for argument_id in argument_ids)
        roots.extend(("group", group_id) for group_ids in getattr(self, "scope_groups", {}).values() for group_id in group_ids)
        self.support_closure.solve(roots)
        for statement in self.statements:
            skey = key_of(ref_of(statement))
            indicator = self.independent_indicator(skey)
            constituents = self.statement_constituents(statement)
            assessment = reduce(constituents, independent=indicator)
            availability = self.support(ref_of(statement))
            assessment.update({"availability": availability, "local_state": assessment["state"],
                               "local_label": assessment["label"]})
            own = [c for c in constituents if c.get("target") == ref_of(statement)
                   and c["kind"] == "source_fidelity" and c.get("required")]
            source_ready = all(c["state"] == "complete" and c["freshness"] == "current"
                               and c["outcome"] == "supported" for c in own)
            if availability == "available" and source_ready and indicator != "disputed" \
                    and self.snap.kind_of(ref_of(statement)) not in ("assumption", "definition"):
                if assessment["state"] != "green":
                    assessment.update({"state": "green", "label": "established",
                                       "explanation": "a sufficient current route establishes the exact target in its declared scope; "
                                                      "other recorded route outcomes and unfinished audit work remain visible"})
            elif self.statement_refuted(ref_of(statement)):
                assessment.update({"state": "red", "label": "statement refuted",
                                   "explanation": "an open statement-refutation finding applies to this exact target; "
                                                  "locally valid conditional reasoning remains separately recorded"})
            elif availability != "available" and assessment["state"] == "green":
                assessment.update({"state": "amber", "label": "conditional",
                                   "explanation": "local examinations are supported, but no current founded route establishes "
                                                  "the exact target in this scope; exact-target, premise and condition "
                                                  "requirements remain applicable"})
            assessments[skey] = assessment
            for argument_id in self.routes.get(skey, []):
                argument = snap.live("arguments", argument_id)
                if argument is not None:
                    akey = key_of(ref_of(argument))
                    if akey not in assessments:
                        assessments[akey] = reduce(self.argument_constituents(argument), independent=indicator)
                    for group in snap.member_records("groups_in_argument", ref_of(argument)):
                        gkey = key_of(ref_of(group))
                        if gkey not in assessments:
                            assessments[gkey] = reduce(self.group_constituents(group))
                        for use in snap.member_records("uses_in_group", ref_of(group)):
                            ukey = key_of(ref_of(use))
                            if ukey not in assessments:
                                assessments[ukey] = self.use_assessment(use)
            for key in sorted(self.route_records.get(skey, ())):
                if key.startswith("groups:") and key not in assessments:
                    group = snap.live("groups", key.split(":", 1)[1])
                    if group is not None:
                        assessments[key] = reduce(self.group_constituents(group))
                if key.startswith("uses:") and key not in assessments:
                    use = snap.live("uses", key.split(":", 1)[1])
                    if use is not None:
                        assessments[key] = self.use_assessment(use)
        if self.audit is not None:
            akey = key_of(ref_of(self.audit))
            assessments[akey] = reduce([dict(self.constituent_for(oid)) for oid in self.by_target.get(akey, [])])
        if self.audit is not None and self.audit.body["mode"] == "triage":
            assessments = {key: _triage_assessment(value) for key, value in assessments.items()}
        return assessments


def _triage_assessment(assessment):
    result = dict(assessment, state="gray", label="triage",
                  explanation="triage records evidence and follow-up work; it does not certify proof correctness or audit completion")
    if "availability" in result:
        result.update(availability="conditional", local_state="gray", local_label="recorded evidence only")
    return result


def _source_limits(snap: Snapshot, statements: list, route_records: dict, mode: str) -> list:
    anchors, sources = set(), set()
    for statement in statements:
        for passage in statement.body["passages"]:
            anchors.add(passage["anchor_id"])
        for key in route_records.get(key_of(ref_of(statement)), ()):
            collection, id = key.split(":", 1)
            record = snap.live(collection, id)
            if record is not None:
                anchors.update(record.body.get("evidence_refs", []))
    for anchor_id in list(anchors):
        anchor = snap.live("anchors", anchor_id)
        if anchor is not None:
            sources.add(anchor.body["source_id"])
    limits = []
    for issue in snap.all("source_issues"):
        if issue.body["lifecycle"] != "open":
            continue
        relevant = mode in ("full", "overview") or issue.body["anchor_id"] in anchors \
            or (issue.body["anchor_id"] is None and issue.body["source_id"] in sources)
        if relevant:
            limits.append(pinned_of(issue))
    return limits


def derive_full(db: Database, *, revision: int | None = None, audit_id: str | None = None, limits=None) -> tuple:
    """Derive one snapshot; returns ``(derivation, result)`` so the projection can reuse the live indexes."""
    if revision is None:
        revision = db.max_revision()
    snap = Snapshot(db, revision, limits=limits)
    audit = None
    if audit_id is not None:
        audit = snap.live("audits", audit_id)
        if audit is None:
            raise InvalidRequest(f"audit {audit_id} is not live at revision {revision}", code="AUDIT_UNKNOWN")
    derivation = _Derivation(snap, audit)
    if audit is not None:
        derivation.derive_obligations()
    assessments = derivation.assess_all()
    derivation.problems.extend(derivation.coverage_problems())
    obligations = [derivation.obligations[oid] for oid in sorted(derivation.obligations)]
    for obligation in obligations:
        obligation["assessment"] = reduce([derivation.constituents[obligation["id"]]],
                                          independent="not_required" if obligation["role"] != "coordinator"
                                          else derivation.independent_indicator(key_of(obligation["target"])))
        if audit is not None and audit.body["mode"] == "triage":
            obligation["assessment"] = _triage_assessment(obligation["assessment"])
    judgments = derivation.judgments
    for check in derivation.all_checks:
        derivation.judgment_info(check)
    support = {}
    for statement in derivation.statements:
        support[key_of(ref_of(statement))] = derivation.support(ref_of(statement))
    indicators = {key_of(ref_of(s)): derivation.independent_indicator(key_of(ref_of(s)))
                  for s in derivation.statements}
    findings = {"open": [], "resolved": [], "superseded": [], "refs": []}
    for finding in sorted(derivation.findings, key=lambda r: r.id):
        findings[finding.body["lifecycle"]].append(pinned_of(finding))
        findings["refs"].append({"ref": pinned_of(finding), "category": finding.body["category"],
                                 "target": finding.body["target"], "lifecycle": finding.body["lifecycle"]})
    mode = "overview" if audit is None else audit.body["mode"]
    source_limits = _source_limits(snap, derivation.statements, derivation.route_records, mode)
    required = [o for o in obligations if o["required"]]
    satisfied = [o for o in required if o["satisfied"]]
    draft_checks = sum(1 for j in judgments.values() if j["state"] == "draft" and not j["superseded"])
    unbound = 0
    for statement in derivation.statements:
        if statement.body["origin"] == "source":
            anchors = [snap.live("anchors", p["anchor_id"]) for p in statement.body["passages"]]
            if not anchors or any(a is None for a in anchors):
                unbound += 1
    disputed = any(v == "disputed" for v in indicators.values())
    process_complete = audit is not None and bool(required) and len(satisfied) == len(required) \
        and not source_limits and not disputed and not derivation.problems
    if mode == "triage":
        process_complete = False
    if audit is not None and mode in ("full", "focused") and not derivation.statements:
        process_complete = False
    published = None
    for pub in db.publications():
        if pub["state"] == "published" and pub["revision"] <= revision:
            published = max(published or 0, pub["revision"])
    context_changed = sum(1 for j in judgments.values() if j.get("context_changed"))
    return derivation, {
        "revision": revision,
        "analysis_complete": True,
        "audit_id": None if audit is None else audit.id,
        "mode": mode,
        "scope": {"mode": mode,
                  "target_refs": [] if audit is None else list(audit.body["targets"]),
                  "exclusions": [] if audit is None else list(audit.body["exclusions"])},
        "statements": [ref_of(s) for s in derivation.statements],
        "routes": derivation.routes,
        "route_records": {k: sorted(v) for k, v in derivation.route_records.items()},
        "obligations": obligations,
        "by_target": {k: sorted(v) for k, v in derivation.by_target.items()},
        "constituents": derivation.constituents,
        "judgments": judgments,
        "assessments": assessments,
        "support": support,
        "independent": indicators,
        "findings": findings,
        "source_limits": source_limits,
        "progress": {"process_complete": process_complete, "required_obligations": len(required),
                     "completed_current_obligations": len(satisfied), "draft_checks": draft_checks,
                     "major_results": sum(1 for s in derivation.statements
                                          if s.collection == "items" and s.body["kind"] in MAJOR_KINDS),
                     "source_unbound_items": unbound},
        "published_revision": published,
        "context": {"source_context_digest": snap.source_context_digest(),
                    "judgments_in_older_context": context_changed},
        "audits": [a.id for a in snap.all("audits")],
        "problems": derivation.problems,
        "coverage_diagnostics": [coverage_diagnostic(row) for row in derivation.coverage_diagnostics[:100]],
        "coverage_diagnostic_count": len(derivation.coverage_diagnostics),
        "coverage_diagnostics_truncated": len(derivation.coverage_diagnostics) > 100,
    }


def derive_assessment(db: Database, *, revision: int | None = None, audit_id: str | None = None, limits=None) -> dict:
    """Derive obligations, freshness, support and presentation states for one snapshot."""
    try:
        return derive_full(db, revision=revision, audit_id=audit_id, limits=limits)[1]
    except TraversalLimit as exc:
        return {"revision": revision if revision is not None else db.max_revision(), "audit_id": audit_id,
                "analysis_complete": False, "progress": {"process_complete": False},
                "problems": [exc.message], "limit": {"bound": exc.bound, "maximum": exc.maximum,
                                                     "context": exc.context}}


__all__ = ["DEFECT_OUTCOMES", "INDICATORS", "OBLIGATION_KINDS", "PROOF_CHECK_KINDS", "PROOF_KINDS", "ROLES",
           "STATES", "Snapshot", "derive_assessment", "derive_full", "judgment_freshness", "key_of", "obligation_id", "pinned_of",
           "reduce", "ref_of"]
