"""Evidence bindings generated when assessment records are accepted (record-contract 6).

A binding pins the record versions and facets a judgment consumed, plus the
membership digests of the relations it depended on, all computed on the
prospective (post-batch) state. Outgoing consumers are never part of a
supplier's binding.
"""
from __future__ import annotations

from .refs import facet_digests, membership_digest, relation_members

BOUND_COLLECTIONS = ("checks", "observations", "reconciliations", "reuse_decisions", "source_reviews")


class _Builder:
    def __init__(self, state):
        self.state = state
        self.records = {}
        self.relations = {}
        self.statements_seen = set()
        self.scopes_seen = set()

    def add(self, collection, id, facet, *, version=None):
        if id is None:
            return None
        if version is None:
            record = self.state.live(collection, id)
        else:
            record = self.state.version(collection, id, version)
        if record is None or record.retired:
            return None
        facets = facet_digests(record.collection, record.body)
        digest_value = facets.get(facet) or facets["full"]
        key = (record.collection, record.id, record.version, facet if facet in facets else "full")
        self.records[key] = digest_value
        return record

    def add_ref(self, ref, facet):
        if ref is None:
            return None
        return self.add(ref["collection"], ref["id"], facet, version=ref.get("version"))

    def anchors(self, ids, facet="proof"):
        for anchor_id in ids:
            self.add("anchors", anchor_id, facet)

    def relation(self, relation, key):
        members = self.state.relation_members(relation, key)
        self.relations[(relation, key["collection"], key["id"])] = membership_digest(members)

    def scope_chain(self, scope_id):
        while scope_id is not None and scope_id not in self.scopes_seen:
            self.scopes_seen.add(scope_id)
            scope = self.add("scopes", scope_id, "scope")
            if scope is None:
                break
            for assumption in scope.body["assumptions"]:
                self.statement(assumption)
            self.anchors(scope.body["evidence_refs"], "statement")
            scope_id = scope.body["parent_id"]

    def statement(self, ref):
        record = self.add_ref(ref, "statement")
        if record is None:
            return None
        key = (record.collection, record.id, record.version)
        if key in self.statements_seen:
            return record
        # A scope may name an assumption declared in that same scope. Expand each
        # consumed statement once, while still binding every record in the cycle.
        self.statements_seen.add(key)
        self.anchors([p["anchor_id"] for p in record.body["passages"]
                      if p["role"] in ("statement", "definition")], "statement")
        self.scope_chain(record.body["scope_id"])
        if record.collection == "parts":
            self.statement({"collection": "items", "id": record.body["item_id"]})
        return record

    def borrowed_proof(self, ref):
        record = self.add_ref(ref, "proof")
        if record is None:
            return
        anchors = [p["anchor_id"] for p in record.body["passages"] if p["role"] in ("proof", "evidence")]
        self.anchors(anchors)
        if not anchors and record.collection == "parts":
            self.borrowed_proof({"collection": "items", "id": record.body["item_id"]})

    def use(self, use_id):
        use = self.add("uses", use_id, "application")
        if use is None:
            return None
        self.statement(use.body["from"])
        self.statement(use.body["to"])
        if use.body["type"] == "proof_argument":
            self.borrowed_proof(use.body["from"])
        self.anchors(use.body["evidence_refs"])
        if use.body["group_id"] is not None:
            group = self.add("groups", use.body["group_id"], "inference")
            if group is not None:
                self.scope_chain(group.body["scope_id"])
            self.relation("uses_in_group", {"collection": "groups", "id": use.body["group_id"]})
        return use

    def group(self, group_id, *, with_uses=True):
        group = self.add("groups", group_id, "inference")
        if group is None:
            return None
        self.statement(group.body["conclusion"])
        self.scope_chain(group.body["scope_id"])
        for scope_id in group.body["case_scope_ids"] + group.body["discharges"]:
            self.scope_chain(scope_id)
        self.anchors(group.body["evidence_refs"])
        key = {"collection": "groups", "id": group_id}
        self.relation("uses_in_group", key)
        if with_uses:
            for _, use_id, _ in self.state.relation_members("uses_in_group", key):
                self.use(use_id)
        return group

    def argument(self, argument_id):
        argument = self.add("arguments", argument_id, "proof")
        if argument is None:
            return None
        target = self.statement(argument.body["target"])
        if target is not None and target.collection == "items":
            item_key = {"collection": "items", "id": target.id}
            self.relation("parts_of_item", item_key)
            for _, part_id, _ in self.state.relation_members("parts_of_item", item_key):
                self.statement({"collection": "parts", "id": part_id})
        self.relation("incoming_uses", argument.body["target"])
        self.scope_chain(argument.body["scope_id"])
        self.anchors(argument.body["evidence_refs"])
        key = {"collection": "arguments", "id": argument_id}
        for relation in ("groups_in_argument", "coverage_in_argument", "scopes_in_argument"):
            self.relation(relation, key)
        for _, group_id, _ in self.state.relation_members("groups_in_argument", key):
            self.group(group_id)
        for _, scope_id, _ in self.state.relation_members("scopes_in_argument", key):
            self.scope_chain(scope_id)
        for _, coverage_id, _ in self.state.relation_members("coverage_in_argument", key):
            coverage = self.add("coverage", coverage_id, "coverage")
            if coverage is not None:
                self.add("anchors", coverage.body["anchor_id"], "proof")
        return argument

    def source_statement(self, ref):
        record = self.statement(ref)
        if record is None:
            return None
        self.add_ref(ref, "proof")
        for passage in record.body["passages"]:
            self.add("anchors", passage["anchor_id"], "source")
            self.add("anchors", passage["anchor_id"], "statement")
        if record.body["scope_id"] is not None:
            self.scope_chain(record.body["scope_id"])
        if record.collection == "items":
            self.relation("parts_of_item", {"collection": "items", "id": record.id})
        return record

    def result(self, packet):
        records = [{"ref": {"collection": c, "id": i, "version": v}, "facet": f, "digest": d}
                   for (c, i, v, f), d in sorted(self.records.items())]
        relations = [{"relation": r, "key": {"collection": c, "id": i}, "digest": d}
                     for (r, c, i), d in sorted(self.relations.items())]
        manifest = (packet or {}).get("manifest") or {}
        return {"records": records, "relations": relations,
                "source_context_digest": manifest.get("source_context_digest"),
                "packet_id": (packet or {}).get("packet_id")}


def _bind_check(b: _Builder, body: dict):
    kind, target = body["kind"], body["target"]
    if kind == "application":
        b.use(target["id"])
    elif kind in ("derivation", "case_coverage", "scope_discharge"):
        b.group(target["id"])
    elif kind == "composition":
        b.argument(target["id"])
    elif kind == "external_source":
        b.source_statement(target)
    else:
        audit = b.add("audits", target["id"], "full")
        if audit is not None:
            for audit_target in audit.body["targets"]:
                b.statement(audit_target)
                b.relation("arguments_for_target", audit_target)
                b.relation("incoming_uses", audit_target)
    b.anchors(body["evidence_refs"])
    if body["supersedes"] is not None:
        b.add_ref(body["supersedes"], "full")


MATHEMATICAL_FACETS = ("statement", "proof", "application", "inference", "scope", "coverage")
RELATION_FACETS = {"uses_in_group": "application", "incoming_uses": "application",
                   "groups_in_argument": "inference", "scopes_in_argument": "scope",
                   "coverage_in_argument": "coverage", "parts_of_item": "statement",
                   "arguments_for_target": "proof", "checks_or_findings_for_target": "full"}


def _semantic_members(b):
    """Freeze membership identities plus member facets for controller bindings only."""
    identities = []
    for relation, collection, id in list(b.relations):
        key = {"collection": collection, "id": id}
        members = b.state.relation_members(relation, key)
        for member_collection, member_id, _ in members:
            b.add(member_collection, member_id, RELATION_FACETS[relation])
        identities.append({"relation": relation, "key": key,
                           "members": sorted([[c, i] for c, i, _ in members])})
    return identities


def binding_changes(state, binding: dict) -> dict:
    """Compare a stored binding with ``state`` (prospective State or Snapshot).

    Returns ``{"records": [{ref, facet, expected, actual}], "relations": [{relation, key, expected, actual}]}``
    where ``actual`` is None when the bound record is no longer live.
    """
    records, relations = [], []
    for entry in binding["records"]:
        ref = entry["ref"]
        live = state.live(ref["collection"], ref["id"])
        actual = None
        if live is not None:
            facets = facet_digests(live.collection, live.body)
            actual = facets.get(entry["facet"]) or facets["full"]
        if actual != entry["digest"]:
            records.append({"ref": ref, "facet": entry["facet"], "expected": entry["digest"], "actual": actual,
                            "live_version": None if live is None else live.version})
    for entry in binding["relations"]:
        actual = membership_digest(state.relation_members(entry["relation"], entry["key"]))
        if actual != entry["digest"]:
            semantic = next((r for r in binding.get("semantic_memberships", [])
                             if r["relation"] == entry["relation"] and r["key"] == entry["key"]), None)
            if semantic is not None and sorted([[c, i] for c, i, _ in state.relation_members(
                    entry["relation"], entry["key"])]) == semantic["members"]:
                continue
            relations.append({"relation": entry["relation"], "key": entry["key"], "expected": entry["digest"],
                              "actual": actual})
    return {"records": records, "relations": relations}


def task_binding(state, task: dict) -> dict:
    """Freeze one assigned obligation's mathematical inputs, independent of packet siblings.

    ``state`` is a lazy current-state adapter or the coordinator's existing snapshot.
    A work packet transports the union of these records, but that union is never a
    judgment's freshness boundary.
    """
    b = _Builder(state)
    target = task["target"]
    if task["action"] == "compare_source":
        if target["collection"] == "uses":
            b.use(target["id"])
        else:
            b.source_statement(target)
    elif task["action"] == "reconcile":
        if target["collection"] in ("items", "parts"):
            b.statement(target)
        elif target["collection"] == "groups":
            b.group(target["id"])
        elif target["collection"] == "arguments":
            b.argument(target["id"])
        elif target["collection"] == "uses":
            b.use(target["id"])
        else:
            b.add_ref(target, "full")
        b.relation("checks_or_findings_for_target", target)
        for ref in task.get("judgment_refs", []) + task.get("prerequisite_judgment_refs", []):
            check = b.add_ref(ref, "full")
            if check is not None and check.collection == "checks":
                _bind_check(b, check.body)
                b.relation("checks_or_findings_for_target", check.body["target"])
    else:
        _bind_check(b, {"kind": task["kind"], "target": target,
                        "evidence_refs": [], "supersedes": None})
    if task["kind"] == "composition":
        for ref in task.get("prerequisite_judgment_refs", []):
            b.add_ref(ref, "full")
    # Work guards protect mathematical membership, not presentation-only member
    # versions. Store each member's relevant facet so semantic comparison below
    # can distinguish changed membership from a relabelled existing member.
    _semantic_members(b)
    result = b.result(None)
    return {"consumed_inputs": result["records"], "membership_guards": result["relations"]}


def task_binding_changes(state, task: dict, *, evidence_refs=(), packet=None) -> dict:
    """Compare original assigned inputs and result-specific extra evidence with current state.

    Extra evidence must have been delivered in the original packet. Its pinned
    version is read explicitly; a new current excerpt is never silently consumed.
    Source-context freshness remains acceptance's separate mandatory check.
    """
    binding = {"records": list(task["consumed_inputs"]),
               "relations": list(task["membership_guards"])}
    manifest = (packet or {}).get("manifest", packet or {})
    pins = {(r["collection"], r["id"]): r for r in manifest.get("read_set", [])}
    missing = []
    facet = "source" if task["action"] == "compare_source" else "proof"
    seen = {(r["ref"]["collection"], r["ref"]["id"], r["facet"]) for r in binding["records"]}
    for anchor_id in dict.fromkeys(evidence_refs):
        pin = pins.get(("anchors", anchor_id))
        if pin is None:
            missing.append({"ref": {"collection": "anchors", "id": anchor_id},
                            "facet": facet, "expected": "original packet evidence", "actual": None})
            continue
        if ("anchors", anchor_id, facet) in seen:
            continue
        original = state.version("anchors", anchor_id, pin["version"])
        if original is None or original.retired:
            missing.append({"ref": pin, "facet": facet, "expected": "original packet evidence", "actual": None})
            continue
        binding["records"].append({"ref": pin, "facet": facet,
                                   "digest": facet_digests("anchors", original.body)[facet]})
    result = binding_changes(state, binding)
    revision = manifest.get("base_revision")
    if revision is not None and hasattr(state, "db"):
        changed_relations = []
        for changed in result["relations"]:
            original_members = relation_members(state.db.conn, changed["relation"], changed["key"], revision=revision)
            current_members = state.relation_members(changed["relation"], changed["key"])
            if {(c, i) for c, i, _ in original_members} != {(c, i) for c, i, _ in current_members}:
                changed_relations.append(changed)
        result["relations"] = changed_relations
    result["records"].extend(missing)
    return result


def _work_composition_checks(b, body, packet):
    """Bind a controller composition to the actual prospective local judgments.

    Earlier results in the same response now have real generated IDs in the
    prospective state. Applications/derivations do not consume these siblings.
    """
    manifest = (packet or {}).get("manifest", packet or {})
    if not manifest.get("work") or body["kind"] != "composition" or body["role"] != "primary":
        return
    if body["target"]["collection"] != "arguments":
        return
    targets = []
    for _, group_id, _ in b.state.relation_members("groups_in_argument", body["target"]):
        group_ref = {"collection": "groups", "id": group_id}
        targets.append(group_ref)
        targets.extend({"collection": c, "id": i} for c, i, _ in
                       b.state.relation_members("uses_in_group", group_ref))
    for target in targets:
        for collection, id, _ in b.state.relation_members("checks_or_findings_for_target", target):
            if collection != "checks":
                continue
            check = b.state.live(collection, id)
            if check is not None and check.body["audit_id"] == body["audit_id"] \
                    and check.body["role"] == body["role"] and check.body["state"] == "complete":
                b.add("checks", id, "full")


def _semantic_membership_origin(state, collection, body, manifest):
    """Preserve a controller review's binding policy through generic identity mapping.

    The mapping command still checks both packets using its ordinary strict
    transaction rules. This selects only the saved check's future comparison
    policy, using immutable response-to-original-packet provenance.
    """
    if manifest.get("packet_version") == 2 and isinstance(manifest.get("work"), dict):
        return True
    if collection != "checks" or body["role"] != "independent" or body["response_id"] is None:
        return False
    response = state.live("responses", body["response_id"])
    if response is None:
        return False
    original = state.db.packet(response.body["packet_id"])
    if original is None or original["packet_version"] != 2 or original["mode"] != "independent":
        return False
    work = original["manifest"].get("work")
    return isinstance(work, dict) and work.get("mode") == "independent" \
        and work.get("audit_id") == body["audit_id"]


def compute_bindings(state, collection: str, body: dict, *, packet=None) -> dict | None:
    """Bindings for one prospective record, or None when the collection carries none."""
    if collection not in BOUND_COLLECTIONS:
        return None
    b = _Builder(state)
    if collection == "checks":
        _bind_check(b, body)
        _work_composition_checks(b, body, packet)
    elif collection == "observations":
        if body["target"]["collection"] == "uses":
            b.use(body["target"]["id"])
        else:
            b.source_statement(body["target"])
        b.anchors(body["evidence_refs"], "source")
    elif collection == "reconciliations":
        for ref in body["primary_checks"] + body["independent_checks"] + body["successor_checks"]:
            b.add_ref(ref, "full")
        if body["target"]["collection"] in ("items", "parts"):
            b.statement(body["target"])
        else:
            b.add_ref(body["target"], "full")
        b.anchors(body["evidence_refs"])
    elif collection == "reuse_decisions":
        check = b.add_ref(body["check_ref"], "full")
        b.add("source_reviews", body["source_review_id"], "full")
        b.anchors(body["evidence_refs"], "source")
        if check is not None:
            # pin the check's consumed records in the new context so the decision itself stays current
            # exactly as long as that context does (record-contract 6)
            _bind_check(b, check.body)
    elif collection == "source_reviews":
        for ref in body["source_refs"]:
            b.add_ref(ref, "source")
        for ref in body["anchor_refs"]:
            b.add_ref(ref, "source")
    manifest = (packet or {}).get("manifest", packet or {})
    identities = _semantic_members(b) if _semantic_membership_origin(state, collection, body, manifest) else None
    result = b.result(packet)
    if identities is not None:
        result["semantic_memberships"] = identities
    return result


__all__ = ["BOUND_COLLECTIONS", "MATHEMATICAL_FACETS", "binding_changes", "compute_bindings",
           "task_binding", "task_binding_changes"]
