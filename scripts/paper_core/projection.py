"""Reader projection (implementation-handoff 7): one deterministic drawing dataset per snapshot.

The projection separates the canonical dataset from drawing data. Nodes are major items, connections
join ordered pairs of distinct major owners through recorded uses, and every displayed status comes
from ``assessment.reduce`` over the explicit obligation set. No independent correctness value is
authored here, and the renderer draws the result without a second color policy.
"""
from __future__ import annotations

import hashlib
from collections import OrderedDict, defaultdict

from . import PROJECTION_VERSION
from .assessment import derive_full, key_of, pinned_of, reduce, ref_of
from .canonical import compact_json
from .contract import INTERMEDIATE_KINDS, MAJOR_KINDS
from .storage import Database, Record

INDICATOR_RANK = {"not_required": 0, "complete": 1, "pending": 2, "compromised": 3, "disputed": 4}
PROOF_RECORD_COLLECTIONS = ("items", "parts", "arguments", "groups", "uses", "scopes", "coverage")
SECTION_ORDER = ("statement", "premises", "applications", "derivations", "composition", "coverage",
                 "findings", "sources", "review", "limitations")
SECTION_TITLES = {
    "statement": "Statement",
    "premises": "Premises and scope",
    "applications": "Applications",
    "derivations": "Intermediate derivations",
    "composition": "Final composition",
    "coverage": "Proof coverage",
    "findings": "Findings and repairs",
    "sources": "Source anchors",
    "review": "Independent review",
    "limitations": "Source limitations",
}
OUTSIDE_SCOPE = {"state": "gray", "label": "outside scope",
                 "explanation": "this result is outside the audit scope and appears as a neighbor of an audited result",
                 "check_refs": [], "finding_refs": [], "missing_obligation_ids": [],
                 "independent_review": "not_required"}


def connection_id(from_major: str, to_major: str) -> str:
    """``conn_`` plus the SHA-256 of the compact JSON pair ``[source_major_id,target_major_id]``."""
    payload = compact_json([from_major, to_major]).encode("utf-8")
    return "conn_" + hashlib.sha256(payload).hexdigest()


def _unpinned(ref) -> dict:
    return {"collection": ref["collection"], "id": ref["id"]}


def _pin_key(ref) -> tuple:
    return (ref["collection"], ref["id"], ref["version"])


def _ids(refs) -> list:
    out = []
    for ref in refs:
        if ref["id"] not in out:
            out.append(ref["id"])
    return out


def public_assessment(assessment: dict) -> dict:
    """Projection assessment: pinned check refs, finding IDs and explicit missing obligations."""
    return {"state": assessment["state"], "label": assessment["label"], "explanation": assessment["explanation"],
            "check_refs": [dict(r) for r in assessment["check_refs"]],
            "finding_refs": _ids(assessment["finding_refs"]),
            "missing_obligation_ids": list(assessment["missing_obligation_ids"]),
            "independent_review": assessment["independent_review"]}


def _dedupe_constituents(constituents: list) -> list:
    out, seen = [], set()
    for c in constituents:
        oid = c.get("obligation_id")
        if oid is None:
            out.append(c)
        elif oid not in seen:
            seen.add(oid)
            out.append(c)
    return out


class _Detail:
    """Sections of one reader detail; records added here are registered with the projector."""

    def __init__(self, projector, key: str):
        self.projector = projector
        self.key = key
        self.sections: dict = {}
        self._seen: dict = {}

    def add(self, kind: str, records=(), obligations=(), note=None):
        section = self.sections.get(kind)
        if section is None:
            section = {"key": kind, "title": SECTION_TITLES[kind], "kind": kind, "record_refs": [],
                       "obligation_ids": [], "note": note}
            self.sections[kind] = section
            self._seen[kind] = (set(), set())
        elif note and not section["note"]:
            section["note"] = note
        seen_records, seen_obligations = self._seen[kind]
        for record in records:
            if record is None:
                continue
            key = (record.collection, record.id, record.version)
            if key not in seen_records:
                seen_records.add(key)
                section["record_refs"].append(pinned_of(record))
                self.projector.register(record)
        for oid in obligations:
            if oid is not None and oid not in seen_obligations and oid in self.projector.obligation_index:
                seen_obligations.add(oid)
                section["obligation_ids"].append(oid)

    def build(self) -> dict:
        ordered = [self.sections[k] for k in SECTION_ORDER if k in self.sections]
        ordered = [s for s in ordered if s["record_refs"] or s["obligation_ids"] or s["note"]]
        refs, seen = [], set()
        for section in ordered:
            for ref in section["record_refs"]:
                key = _pin_key(ref)
                if key not in seen:
                    seen.add(key)
                    refs.append(dict(ref))
        return {"record_refs": refs, "sections": ordered}


class _Projector:
    def __init__(self, db: Database, derivation, result: dict):
        self.db = db
        self.d = derivation
        self.snap = derivation.snap
        self.A = result
        self.overview = derivation.audit is None
        self.records: dict = OrderedDict()
        self.problems: list = []
        self.obligation_index = {o["id"]: o for o in result["obligations"]}
        self.outgoing = defaultdict(list)
        for use in sorted(self.snap.all("uses"), key=lambda r: r.id):
            self.outgoing[key_of(use.body["from"])].append(use)
        self.repairs_by_finding = defaultdict(list)
        for repair in sorted(self.snap.all("repairs"), key=lambda r: r.id):
            self.repairs_by_finding[repair.body["finding_id"]].append(repair)
        self.reuse_by_check = defaultdict(list)
        for decision in sorted(self.snap.all("reuse_decisions"), key=lambda r: r.id):
            self.reuse_by_check[decision.body["check_ref"]["id"]].append(decision)
        self.issues_by_anchor = defaultdict(list)
        self.issues_by_source = defaultdict(list)
        for issue in sorted(self.snap.all("source_issues"), key=lambda r: r.id):
            if issue.body["anchor_id"] is not None:
                self.issues_by_anchor[issue.body["anchor_id"]].append(issue)
            else:
                self.issues_by_source[issue.body["source_id"]].append(issue)

    # -- record registry -----------------------------------------------------
    def register(self, record: Record):
        key = (record.collection, record.id, record.version)
        if key not in self.records:
            self.records[key] = record

    def register_pinned(self, ref: dict):
        key = _pin_key(ref)
        if key in self.records:
            return
        record = self.snap.live(ref["collection"], ref["id"])
        if record is None or record.version != ref["version"]:
            record = self.db.version(ref["collection"], ref["id"], ref["version"])
        if record is None or record.body is None:
            self.problems.append(f"pinned reference {ref['collection']}:{ref['id']}:{ref['version']} has no stored body")
            return
        self.records[key] = record

    # -- lookups ---------------------------------------------------------------
    def obligations_for(self, ref, *, roles=None, kinds=None) -> list:
        out = []
        for oid in self.A["by_target"].get(key_of(ref), []):
            obligation = self.obligation_index[oid]
            if roles is not None and obligation["role"] not in roles:
                continue
            if kinds is not None and obligation["kind"] not in kinds:
                continue
            out.append(oid)
        return out

    def checks_for(self, ref, *, roles=None) -> list:
        checks = self.d.checks_by_target.get(key_of(ref), [])
        if roles is not None:
            checks = [c for c in checks if c.body["role"] in roles]
        return sorted(checks, key=lambda r: r.id)

    def findings_for(self, keys, use_ids=()) -> list:
        found = {}
        for key in keys:
            for finding in self.d.findings_by_target.get(key, []):
                found[finding.id] = finding
        for use_id in use_ids:
            for finding in self.d.findings_by_use.get(use_id, []):
                found[finding.id] = finding
        return [found[k] for k in sorted(found)]

    def anchors_of(self, records) -> list:
        ids = []
        for record in records:
            body = record.body
            for passage in body.get("passages", []) or []:
                ids.append(passage["anchor_id"])
            for anchor_id in body.get("evidence_refs", []) or []:
                ids.append(anchor_id)
            if record.collection == "coverage":
                ids.append(body["anchor_id"])
        anchors, seen = [], set()
        for anchor_id in ids:
            if anchor_id in seen:
                continue
            seen.add(anchor_id)
            anchor = self.snap.live("anchors", anchor_id)
            if anchor is not None:
                anchors.append(anchor)
        return sorted(anchors, key=lambda r: r.id)

    def scope_chain(self, scope_id) -> list:
        out, seen = [], set()
        while scope_id is not None and scope_id not in seen:
            seen.add(scope_id)
            scope = self.snap.live("scopes", scope_id)
            if scope is None:
                break
            out.append(scope)
            scope_id = scope.body["parent_id"]
        return out

    def constituents_for_use(self, use: Record) -> list:
        return self.d.use_constituents(use)

    def constituents_for_group(self, group: Record) -> list:
        return self.d.group_constituents(group, with_uses=False)

    # -- node set --------------------------------------------------------------
    def scoped_majors(self) -> dict:
        majors = OrderedDict()
        for ref in self.A["statements"]:
            major = self.snap.major_of(ref)
            if major is not None:
                majors.setdefault(major.id, major)
        return majors

    def node_set(self) -> tuple:
        if self.overview:
            items = [r for r in self.snap.all("items") if r.body["kind"] in MAJOR_KINDS]
            return OrderedDict((r.id, r) for r in sorted(items, key=lambda r: r.id)), OrderedDict()
        scoped = self.scoped_majors()
        nodes = OrderedDict(scoped)
        for use in self.snap.all("uses"):
            source, target = self.snap.major_of(use.body["from"]), self.snap.major_of(use.body["to"])
            if source is None or target is None:
                continue
            if source.id in scoped and target.id not in nodes:
                nodes[target.id] = target
            if target.id in scoped and source.id not in nodes:
                nodes[source.id] = source
        return OrderedDict(sorted(nodes.items())), scoped

    def boundary_uses(self, nodes: dict, scoped: dict) -> dict:
        pairs = defaultdict(list)
        for use in sorted(self.snap.all("uses"), key=lambda r: r.id):
            source, target = self.snap.major_of(use.body["from"]), self.snap.major_of(use.body["to"])
            if source is None or target is None:
                self.problems.append(f"use {use.id} has an endpoint without a major owner")
                continue
            if source.id == target.id or source.id not in nodes or target.id not in nodes:
                continue
            if not self.overview and source.id not in scoped and target.id not in scoped:
                continue
            pairs[(source.id, target.id)].append(use)
        return OrderedDict(sorted(pairs.items()))

    # -- boundary traces (handoff 7.2) -----------------------------------------
    def trace(self, use: Record, owner: Record) -> dict:
        """Represented targets and context reached from one boundary use toward the owning result."""
        snap = self.snap
        represented_uses, represented_groups, represented_arguments, conclusions = [use], [], [], []
        context: dict = OrderedDict()

        def add_context(record):
            if record is not None:
                context.setdefault((record.collection, record.id), record)

        add_context(snap.get(use.body["from"]))
        for anchor in self.anchors_of([use]):
            add_context(anchor)
        if use.body["group_id"] is None:
            return {"uses": represented_uses, "groups": represented_groups, "arguments": represented_arguments, "conclusions": conclusions,
                    "context": list(context.values())}
        visited_groups, visited_uses = set(), {use.id}
        frontier = [use.body["group_id"]]
        while frontier:
            group_id = frontier.pop(0)
            if group_id in visited_groups:
                continue
            visited_groups.add(group_id)
            group = snap.live("groups", group_id)
            if group is None:
                continue
            argument = snap.live("arguments", group.body["argument_id"])
            conclusion = snap.get(group.body["conclusion"])
            hidden = conclusion is not None and conclusion.collection == "items" \
                and conclusion.body["kind"] in INTERMEDIATE_KINDS
            if not hidden:
                # Only a major conclusion (including a statement part) ends the connection.
                # A hidden claim can have its own complete argument: its final group is still
                # an intermediate derivation represented by this connection, not the owner's
                # final composition.
                add_context(argument)
                add_context(group)
                for other in snap.member_records("uses_in_group", ref_of(group)):
                    if other.id not in visited_uses:
                        add_context(other)
                        add_context(snap.get(other.body["from"]))
                continue
            represented_groups.append(group)
            conclusions.append(conclusion)
            if argument is not None and argument.body["final_group_id"] == group.id \
                    and self.argument_section(argument) == "derivations":
                represented_arguments.append(argument)
            for anchor in self.anchors_of([group]):
                add_context(anchor)
            for other in snap.member_records("uses_in_group", ref_of(group)):
                if other.id in visited_uses:
                    continue
                # joint inputs entering the same hidden derivation are premises shown as context
                add_context(other)
                add_context(snap.get(other.body["from"]))
            for downstream in self.outgoing.get(key_of(ref_of(conclusion)), []):
                if downstream.id in visited_uses:
                    continue
                downstream_owner = snap.major_of(downstream.body["to"])
                if downstream_owner is None or downstream_owner.id != owner.id:
                    continue  # crossing into a different major result: that is another connection
                visited_uses.add(downstream.id)
                represented_uses.append(downstream)
                if downstream.body["group_id"] is not None:
                    frontier.append(downstream.body["group_id"])
        return {"uses": represented_uses, "groups": represented_groups, "arguments": represented_arguments, "conclusions": conclusions,
                "context": list(context.values())}

    # -- details -----------------------------------------------------------------
    def argument_section(self, argument: Record | None) -> str:
        target = self.snap.get(argument.body["target"]) if argument is not None else None
        return "derivations" if target is not None and target.collection == "items" \
            and target.body["kind"] in INTERMEDIATE_KINDS else "composition"

    def section_for_obligation(self, obligation: dict) -> str:
        role, kind = obligation["role"], obligation["kind"]
        if role in ("independent", "coordinator"):
            return "review"
        if kind == "source_fidelity":
            return "sources"
        if kind == "application":
            return "applications"
        if kind == "composition":
            return self.argument_section(self.snap.live("arguments", obligation["target"]["id"]))
        if kind == "external_source":
            return "statement"
        if kind in ("derivation", "case_coverage", "scope_discharge"):
            group = self.snap.live("groups", obligation["target"]["id"])
            if group is not None:
                argument = self.snap.live("arguments", group.body["argument_id"])
                if argument is not None and argument.body["final_group_id"] == group.id:
                    return self.argument_section(argument)
            return "derivations"
        return "review"

    def section_for_check(self, check: Record) -> str:
        if check.body["role"] != "primary":
            return "review"
        pseudo = {"role": "primary", "kind": check.body["kind"], "target": check.body["target"]}
        return self.section_for_obligation(pseudo)

    def place_obligations(self, detail: _Detail, ref):
        for oid in self.obligations_for(ref):
            detail.add(self.section_for_obligation(self.obligation_index[oid]), (), [oid])

    def place_checks(self, detail: _Detail, ref, *, independent_checks: list):
        for check in self.checks_for(ref):
            detail.add(self.section_for_check(check), [check])
            if check.body["role"] == "independent":
                independent_checks.append(check)

    def add_review_material(self, detail: _Detail, keys: set, independent_checks: list):
        snap = self.snap
        for rec in self.d.reconciliations:
            if key_of(rec.body["target"]) in keys:
                detail.add("review", [rec])
        for check in independent_checks:
            response = self.d.responses.get(check.body["response_id"]) if check.body["response_id"] else None
            detail.add("review", [response])
        seen_reviews = set()
        for record in list(detail.sections.get("applications", {}).get("record_refs", [])) \
                + list(detail.sections.get("derivations", {}).get("record_refs", [])) \
                + list(detail.sections.get("composition", {}).get("record_refs", [])) \
                + list(detail.sections.get("statement", {}).get("record_refs", [])) \
                + list(detail.sections.get("review", {}).get("record_refs", [])):
            if record["collection"] != "checks":
                continue
            for decision in self.reuse_by_check.get(record["id"], []):
                detail.add("review", [decision])
                review_id = decision.body["source_review_id"]
                if review_id not in seen_reviews:
                    seen_reviews.add(review_id)
                    detail.add("review", [snap.live("source_reviews", review_id)])

    def add_source_material(self, detail: _Detail, anchors: list, statements: list):
        snap = self.snap
        sources = OrderedDict()
        for anchor in anchors:
            detail.add("sources", [anchor])
            source = snap.live("sources", anchor.body["source_id"])
            if source is not None:
                sources.setdefault(source.id, source)
        for source in sources.values():
            detail.add("sources", [source])
        for statement in statements:
            for observation in sorted(self.d.observations_by_target.get(key_of(ref_of(statement)), []),
                                      key=lambda r: r.id):
                detail.add("sources", [observation])
        issues = OrderedDict()
        for anchor in anchors:
            for issue in self.issues_by_anchor.get(anchor.id, []):
                issues.setdefault(issue.id, issue)
        for source_id in sources:
            for issue in self.issues_by_source.get(source_id, []):
                issues.setdefault(issue.id, issue)
        open_count = sum(1 for issue in issues.values() if issue.body["lifecycle"] == "open")
        if issues:
            note = f"{open_count} open source issue(s) limit this material." if open_count else None
            detail.add("limitations", list(issues.values()), note=note)

    def add_findings(self, detail: _Detail, keys: set, use_ids: list):
        for finding in self.findings_for(keys, use_ids):
            detail.add("findings", [finding])
            for ref in finding.body["check_refs"]:
                self.register_pinned(ref)
            detail.add("findings", self.repairs_by_finding.get(finding.id, []))

    def item_detail(self, item: Record, in_scope: bool) -> _Detail:
        snap = self.snap
        detail = _Detail(self, f"item:{item.id}")
        parts = snap.member_records("parts_of_item", ref_of(item))
        statements = [item] + parts
        intermediates = snap.intermediates_of(item.id)
        note = None
        if not self.overview and not in_scope:
            note = "Outside the audit scope; shown as a neighbor of an audited result."
        detail.add("statement", statements, note=note)
        keys = {key_of(ref_of(r)) for r in statements + intermediates}
        use_ids: list = []
        independent_checks: list = []
        route_records: list = []
        for statement in statements:
            self.place_obligations(detail, ref_of(statement))
            self.place_checks(detail, ref_of(statement), independent_checks=independent_checks)
            for scope in self.scope_chain(statement.body["scope_id"]):
                detail.add("premises", [scope])
        arguments = OrderedDict()
        for member in statements + intermediates:
            for argument in snap.member_records("arguments_for_target", ref_of(member)):
                arguments.setdefault(argument.id, argument)
        for argument in arguments.values():
            keys.add(key_of(ref_of(argument)))
            route_records.append(argument)
            detail.add(self.argument_section(argument), [argument])
            self.place_obligations(detail, ref_of(argument))
            self.place_checks(detail, ref_of(argument), independent_checks=independent_checks)
            for scope in self.scope_chain(argument.body["scope_id"]):
                detail.add("premises", [scope])
            for scope in snap.member_records("scopes_in_argument", ref_of(argument)):
                detail.add("premises", [scope])
            final_id = argument.body["final_group_id"]
            for group in snap.member_records("groups_in_argument", ref_of(argument)):
                keys.add(key_of(ref_of(group)))
                route_records.append(group)
                section = self.argument_section(argument) if group.id == final_id else "derivations"
                detail.add(section, [group])
                self.place_obligations(detail, ref_of(group))
                self.place_checks(detail, ref_of(group), independent_checks=independent_checks)
                for scope_id in [group.body["scope_id"]] + list(group.body["case_scope_ids"]):
                    for scope in self.scope_chain(scope_id):
                        detail.add("premises", [scope])
                conclusion = snap.get(group.body["conclusion"])
                if conclusion is not None and conclusion.collection == "items" \
                        and conclusion.body["kind"] in INTERMEDIATE_KINDS:
                    detail.add("derivations", [conclusion])
            for coverage in snap.member_records("coverage_in_argument", ref_of(argument)):
                route_records.append(coverage)
                detail.add("coverage", [coverage])
        member_uses = OrderedDict()
        for member in statements + intermediates:
            for use in snap.member_records("incoming_uses", ref_of(member)):
                member_uses.setdefault(use.id, use)
        # Groups with no argument behind them (migrated overview groups stay provenance
        # annotations) never appear through groups_in_argument; collect them by conclusion
        # with their member uses so a cases group keeps its case qualification visible.
        provenance = OrderedDict()
        for member in statements + intermediates:
            for group in snap.groups_for_conclusion(ref_of(member)):
                if group is not None and group.body["argument_id"] is None:
                    provenance.setdefault(group.id, group)
        for group in provenance.values():
            keys.add(key_of(ref_of(group)))
            route_records.append(group)
            detail.add("derivations", [group])
            self.place_obligations(detail, ref_of(group))
            self.place_checks(detail, ref_of(group), independent_checks=independent_checks)
            for scope_id in [group.body["scope_id"]] + list(group.body["case_scope_ids"]):
                for scope in self.scope_chain(scope_id):
                    detail.add("premises", [scope])
            for use in snap.member_records("uses_in_group", ref_of(group)):
                member_uses.setdefault(use.id, use)
        for use in member_uses.values():
            keys.add(key_of(ref_of(use)))
            use_ids.append(use.id)
            route_records.append(use)
            detail.add("applications", [use])
            self.place_obligations(detail, ref_of(use))
            self.place_checks(detail, ref_of(use), independent_checks=independent_checks)
            supplier_owner = snap.major_of(use.body["from"])
            if supplier_owner is not None and supplier_owner.id != item.id:
                detail.add("premises", [snap.get(use.body["from"])])
        detail.add("derivations", intermediates)
        self.add_findings(detail, keys, use_ids)
        self.add_source_material(detail, self.anchors_of(statements + intermediates + route_records), statements)
        self.add_review_material(detail, keys, independent_checks)
        return detail

    def connection_detail(self, cid: str, traces: list, context: list) -> _Detail:
        detail = _Detail(self, f"conn:{cid}")
        keys, use_ids, independent_checks = set(), [], []
        boundary = [t["uses"][0] for t in traces]
        for use in boundary:
            keys.add(key_of(ref_of(use)))
            use_ids.append(use.id)
            detail.add("applications", [use])
            self.place_obligations(detail, ref_of(use))
            self.place_checks(detail, ref_of(use), independent_checks=independent_checks)
            detail.add("premises", [self.snap.get(use.body["from"])])
        for trace in traces:
            for argument in trace["arguments"]:
                keys.add(key_of(ref_of(argument)))
                detail.add("derivations", [argument])
                self.place_obligations(detail, ref_of(argument))
                self.place_checks(detail, ref_of(argument), independent_checks=independent_checks)
            for use in trace["uses"][1:]:
                keys.add(key_of(ref_of(use)))
                use_ids.append(use.id)
                detail.add("derivations", [use])
                self.place_obligations(detail, ref_of(use))
                self.place_checks(detail, ref_of(use), independent_checks=independent_checks)
            for group, conclusion in zip(trace["groups"], trace["conclusions"]):
                keys.add(key_of(ref_of(group)))
                detail.add("derivations", [group, conclusion])
                self.place_obligations(detail, ref_of(group))
                self.place_checks(detail, ref_of(group), independent_checks=independent_checks)
        composition_note = ("Final composition of the owning result; assessed on the result, "
                            "not on this connection.")
        for record in context:
            if record.collection in ("arguments", "groups"):
                detail.add("composition", [record], self.obligations_for(ref_of(record), roles=("primary",)),
                           note=composition_note)
            elif record.collection == "uses":
                detail.add("premises", [record])
            elif record.collection in ("items", "parts"):
                detail.add("premises", [record])
        self.add_findings(detail, keys, use_ids)
        anchors = []
        for trace in traces:
            anchors.extend(self.anchors_of(trace["uses"] + trace["groups"] + trace["arguments"]))
        unique = OrderedDict((a.id, a) for a in sorted(anchors, key=lambda r: r.id))
        self.add_source_material(detail, list(unique.values()), [])
        self.add_review_material(detail, keys, independent_checks)
        return detail

    # -- assembly ------------------------------------------------------------------
    def node_assessment(self, item: Record, scoped: dict) -> dict:
        key = key_of(ref_of(item))
        if key in self.A["assessments"]:
            return public_assessment(self.A["assessments"][key])
        if self.overview:
            return public_assessment(reduce([]))
        parts = [p for p in self.snap.member_records("parts_of_item", ref_of(item))
                 if key_of(ref_of(p)) in self.A["assessments"]]
        if parts:
            constituents, indicator = [], "not_required"
            for part in parts:
                constituents.extend(self.d.statement_constituents(part))
                candidate = self.A["independent"].get(key_of(ref_of(part)), "not_required")
                if INDICATOR_RANK[candidate] > INDICATOR_RANK[indicator]:
                    indicator = candidate
            return public_assessment(reduce(_dedupe_constituents(constituents), independent=indicator))
        return dict(OUTSIDE_SCOPE, check_refs=[], finding_refs=[], missing_obligation_ids=[])

    def build(self) -> tuple:
        snap, A = self.snap, self.A
        nodes, scoped = self.node_set()
        pairs = self.boundary_uses(nodes, scoped)
        connections, connection_details = [], OrderedDict()
        edges = []
        for (source_id, target_id), uses in pairs.items():
            owner = nodes[target_id]
            cid = connection_id(source_id, target_id)
            traces = [self.trace(use, owner) for use in uses]
            partition: dict = OrderedDict()
            for use, trace in zip(uses, traces):
                group = snap.live("groups", use.body["group_id"]) if use.body["group_id"] else None
                argument_id = None if group is None else group.body["argument_id"]
                partition.setdefault((argument_id, use.body["group_id"]), []).append((use, trace))
            groups_out, all_constituents, context = [], [], OrderedDict()
            for (argument_id, group_id), members in sorted(partition.items(), key=lambda kv: (kv[0][0] or "", kv[0][1] or "")):
                constituents = []
                for use, trace in members:
                    for rep in trace["uses"]:
                        constituents.extend(self.constituents_for_use(rep))
                    for group in trace["groups"]:
                        constituents.extend(self.constituents_for_group(group))
                    for argument in trace["arguments"]:
                        constituents.extend(dict(self.d.constituent_for(oid)) for oid in
                                            self.d.by_target.get(key_of(ref_of(argument)), []))
                    for record in trace["context"]:
                        context.setdefault((record.collection, record.id), record)
                constituents = _dedupe_constituents(constituents)
                obligation_ids = sorted({c["obligation_id"] for c in constituents if c.get("obligation_id")})
                groups_out.append({"argument_id": argument_id, "group_id": group_id,
                                   "use_ids": sorted(use.id for use, _ in members),
                                   "obligation_ids": obligation_ids,
                                   "assessment": public_assessment(reduce(constituents))})
                all_constituents.extend(constituents)
            all_constituents = _dedupe_constituents(all_constituents)
            obligation_ids = sorted({c["obligation_id"] for c in all_constituents if c.get("obligation_id")})
            support_refs, seen = [], set()
            for c in all_constituents:
                for ref in c.get("check_refs", ()):
                    key = (ref["collection"], ref["id"])
                    if key not in seen:
                        seen.add(key)
                        support_refs.append(_unpinned(ref))
                    self.register_pinned(ref)
            context_refs = [ref_of(r) for r in context.values()]
            connections.append({"id": cid, "from": source_id, "to": target_id,
                                "primary_use_ids": sorted(use.id for use in uses), "groups": groups_out,
                                "support_refs": support_refs, "obligation_ids": obligation_ids,
                                "context_refs": context_refs,
                                "assessment": public_assessment(reduce(all_constituents)),
                                "detail_key": f"conn:{cid}"})
            connection_details[cid] = (traces, list(context.values()))
            edges.append((source_id, target_id))
        node_list = []
        details = OrderedDict()
        for item_id, item in nodes.items():
            node_list.append({"id": item_id, "item_ref": pinned_of(item), "kind": item.body["kind"],
                              "label": item.body["label"], "caption": item.body["caption"],
                              "assessment": self.node_assessment(item, scoped), "detail_key": f"item:{item_id}"})
            details[f"item:{item_id}"] = self.item_detail(item, item_id in scoped).build()
        for connection in connections:
            traces, context = connection_details[connection["id"]]
            details[connection["detail_key"]] = self.connection_detail(connection["id"], traces, context).build()
        if self.d.audit is not None and any(o["required"] and o["target"] == ref_of(self.d.audit)
                                           for o in A["obligations"]):
            # Global examinations need a real reader destination, but never a
            # scientific graph node. Optional protocol rows need no new panel.
            detail = _Detail(self, f"audit:{self.d.audit.id}")
            detail.add("review", [self.d.audit])
            self.place_obligations(detail, ref_of(self.d.audit))
            self.place_checks(detail, ref_of(self.d.audit), independent_checks=[])
            details[detail.key] = detail.build()
        obligations = []
        for obligation in A["obligations"]:
            for ref in obligation["check_refs"]:
                self.register_pinned(ref)
            for ref in obligation["assessment"]["check_refs"]:
                self.register_pinned(ref)
            obligations.append({"id": obligation["id"], "target": _unpinned(obligation["target"]),
                                "kind": obligation["kind"], "role": obligation["role"],
                                "check_refs": [dict(r) for r in obligation["check_refs"]],
                                "assessment": public_assessment(obligation["assessment"])})
        for node in node_list:
            for ref in node["assessment"]["check_refs"]:
                self.register_pinned(ref)
        for group in A["findings"].values():
            if isinstance(group, list):
                for entry in group:
                    ref = entry["ref"] if "ref" in entry else entry
                    if "version" in ref:
                        self.register_pinned(ref)
        for ref in A["source_limits"]:
            self.register_pinned(ref)
        if not self.overview:
            self.register(self.d.audit)
        locations, located = [], set()
        for detail_key, detail in details.items():
            for section in detail["sections"]:
                for ref in section["record_refs"]:
                    key = _pin_key(ref)
                    if key not in located:
                        located.add(key)
                        locations.append({"ref": dict(ref), "detail_key": detail_key, "section_key": section["key"]})
        for key, record in self.records.items():
            if key in located:
                continue
            if record.collection in PROOF_RECORD_COLLECTIONS or record.collection == "findings":
                self.problems.append(f"{record.collection}:{record.id}:{record.version} has no reader location")
        layout = self.layout(list(nodes), edges)
        records = [{"ref": {"collection": c, "id": i, "version": v}, "body": self.records[(c, i, v)].body}
                   for (c, i, v) in sorted(self.records)]
        findings = A["findings"]
        summary = {
            "scope": {"mode": A["scope"]["mode"], "target_refs": [_unpinned(r) for r in A["scope"]["target_refs"]],
                      "exclusions": list(A["scope"]["exclusions"])},
            "progress": dict(A["progress"]),
            "findings": {"open": len(findings["open"]), "resolved": len(findings["resolved"]),
                         "superseded": len(findings["superseded"]),
                         "refs": _ids([entry["ref"] for entry in findings["refs"]])},
            "source_limits": _ids(A["source_limits"]),
            "limitations": list(A["problems"]),
            "published_revision": A["published_revision"],
        }
        # The imported overview's boundaries remain provenance even when an
        # audit selects a different set of targets. Do not merge the two scopes.
        papers = self.snap.all("papers")
        if len(papers) == 1 and any(key in papers[0].body for key in ("scope", "exclusions")):
            summary["overview_scope"] = {"text": papers[0].body.get("scope"),
                                         "exclusions": list(papers[0].body.get("exclusions", []))}
        projection = {
            "projection_version": PROJECTION_VERSION,
            "snapshot_revision": A["revision"],
            "audit_id": A["audit_id"],
            "nodes": node_list,
            "connections": connections,
            "records": records,
            "obligations": obligations,
            "details": dict(details),
            "record_locations": locations,
            "summary": summary,
            "layout": layout,
        }
        report = {"problems": list(A["problems"]) + self.problems,
                  "counts": {"nodes": len(node_list), "connections": len(connections), "records": len(records),
                             "obligations": len(obligations), "details": len(details), "locations": len(locations)}}
        return projection, report

    @staticmethod
    def layout(node_ids: list, edges: list) -> dict:
        indegree = {n: 0 for n in node_ids}
        outgoing = defaultdict(list)
        for source, target in edges:
            indegree[target] += 1
            outgoing[source].append(target)
        queue = [n for n in node_ids if indegree[n] == 0]
        removed = set()
        while queue:
            node = queue.pop(0)
            removed.add(node)
            for target in outgoing[node]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
        remaining = sorted(n for n in node_ids if n not in removed)
        if not remaining:
            return {"mode": "dag", "reasons": []}
        cycle = _find_cycle(remaining, outgoing)
        path = " to ".join(cycle) if cycle else ", ".join(remaining)
        return {"mode": "index",
                "reasons": [f"Connections form a cycle under major-owner projection ({path}); the diagram is "
                            "replaced by the complete major-item and connection index in the same shell.",
                            "A cycle introduced by ownership projection is a display limitation, "
                            "not automatically circular mathematics; every recorded use is kept."]}


def _find_cycle(nodes: list, outgoing: dict) -> list:
    remaining = set(nodes)
    for start in nodes:
        stack = [(start, [start])]
        seen = set()
        while stack:
            node, path = stack.pop()
            for target in sorted(outgoing.get(node, [])):
                if target not in remaining:
                    continue
                if target == start:
                    return path + [start]
                if target not in seen:
                    seen.add(target)
                    stack.append((target, path + [target]))
    return []


def project(db: Database, *, revision: int | None = None, audit_id: str | None = None) -> tuple:
    """Build the projection and a report ``{problems, counts}`` for one snapshot."""
    derivation, result = derive_full(db, revision=revision, audit_id=audit_id)
    projection, report = _Projector(db, derivation, result).build()
    if derivation.audit is not None:
        from .work import build_work
        work = build_work(derivation, result)
        locations = {(entry["ref"]["collection"], entry["ref"]["id"]): entry
                     for entry in projection["record_locations"]}
        obligation_locations = defaultdict(list)
        for detail_key, detail in projection["details"].items():
            for section in detail["sections"]:
                for oid in section["obligation_ids"]:
                    obligation_locations[oid].append({"detail_key": detail_key,
                        "section_key": section["key"], "obligation_id": oid})
        for task in work["tasks"]:
            record = derivation.snap.get(task["target"])
            owner = derivation.snap.get(task["owner"]) if task["owner"] else record
            label = (owner.body.get("label") or owner.body.get("title") or owner.id) if owner else "Audit"
            kind_label = {"source_fidelity": "source comparison", "composition": "final composition"}.get(
                task["kind"], task["kind"].replace("_", " "))
            subject = None
            if record and record.collection == "groups":
                subject = derivation.snap.get(record.body["conclusion"])
            elif record and record.collection == "uses":
                subject = derivation.snap.get(record.body["from"])
            elif record and record.collection in ("items", "parts") and record != owner:
                subject = record
            if subject and subject.body.get("label") and subject != owner:
                kind_label += " (" + subject.body["label"] + ")"
            task["label"] = label + ": " + kind_label
            choices = obligation_locations.get(task["id"], [])
            preferred = "item:" + owner.id if owner and owner.collection == "items" else None
            chosen = next((choice for choice in choices if choice["detail_key"] == preferred),
                          choices[0] if choices else None)
            task["location"] = dict(chosen, ref=pinned_of(record)) if chosen and record else \
                locations.get((task["target"]["collection"], task["target"]["id"]))
        projection["worklist"] = {k: work[k] for k in ("revision", "analysis_complete", "tasks", "coordinator_actions",
                                                     "diagnostic_count", "diagnostics_truncated")}
    else:
        projection["worklist"] = None
    return projection, report


def build_projection(db: Database, *, revision: int | None = None, audit_id: str | None = None) -> dict:
    """The reader projection for one snapshot (implementation-handoff 7.1 to 7.3)."""
    return project(db, revision=revision, audit_id=audit_id)[0]


__all__ = ["PROOF_RECORD_COLLECTIONS", "SECTION_ORDER", "SECTION_TITLES", "build_projection", "connection_id",
           "project", "public_assessment"]
