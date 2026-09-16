"""Semantic validation of edit batches against the prospective (post-batch) state.

Implements the commit-time validation list of record-contract 7 and invariants 1-10.
Shape validation of bodies lives in ``contract``; this module checks linked state.
"""
from __future__ import annotations

from dataclasses import dataclass

from .bindings import binding_changes
from .canonical import sha256_bytes
from .contract import MAJOR_KINDS, extract_refs, validate_body
from .ids import COLLECTIONS
from .refs import RELATIONS, body_members, relation_members
from .storage import Database, Record

STRUCTURAL = ("items", "parts", "scopes", "arguments", "groups", "uses", "coverage")
ASSESSMENT = ("checks", "findings", "repairs")
PAPER_LEVEL = ("audits", "identity_maps", "reuse_decisions", "qualifications", "sources", "anchors",
               "source_reviews", "source_issues", "responses", "papers")
IMMUTABLE_ONCE_CREATED = ("observations", "reconciliations", "qualifications")

# Per command: collections that may be created / replaced / retired.
COMMANDS = {
    "apply": {
        "create": STRUCTURAL + ASSESSMENT + ("audits", "identity_maps", "reuse_decisions"),
        "replace": ("papers",) + STRUCTURAL + ASSESSMENT + ("audits", "identity_maps", "reuse_decisions"),
        "retire": STRUCTURAL + ASSESSMENT + ("audits", "identity_maps", "reuse_decisions"),
    },
    "compare": {"create": ("observations",), "replace": (), "retire": ()},
    "work_primary": {"create": ("observations", "checks", "coverage", "findings"),
                     "replace": ("checks", "coverage"), "retire": ()},
    "source_capture": {"create": ("sources",), "replace": ("sources",), "retire": ()},
    "source_anchor": {"create": ("anchors",), "replace": ("anchors",), "retire": ("anchors",)},
    "source_review": {"create": ("source_reviews", "source_issues"),
                      "replace": ("source_reviews", "source_issues"), "retire": ()},
    "qualification": {"create": ("qualifications",), "replace": (), "retire": ()},
    "review_submit": {"create": ("responses", "checks"), "replace": (), "retire": ()},
    "review_map": {"create": ("identity_maps", "checks"), "replace": ("responses",), "retire": ()},
    "reconcile": {"create": ("reconciliations", "checks", "findings"), "replace": ("findings", "checks"),
                  "retire": ()},
    "import": {"create": tuple(c for c in COLLECTIONS if c != "papers"), "replace": ("papers",), "retire": ()},
}
INDEPENDENT_CHECK_COMMANDS = ("review_submit", "review_map", "import")
FINDING_TARGETS = ("items", "parts", "uses", "groups", "arguments", "audits")
OBSERVATION_TARGETS = ("items", "parts", "uses")  # record-contract 2: compared statement or use facets
USE_IDENTITY = ("from", "to", "type", "group_id", "needed_form", "substitutions", "regime")


@dataclass
class Planned:
    index: int
    op: str
    collection: str
    id: str
    expected_version: int | None
    body: dict | None
    reason: str | None = None
    prev: Record | None = None
    version: int = 0

    @property
    def key(self):
        return (self.collection, self.id)

    @property
    def record(self) -> Record:
        return Record(self.collection, self.id, self.version, None, self.body is None, self.body)


class State:
    """Prospective state: database heads overlaid with a planned batch."""

    def __init__(self, db: Database, plan: list):
        self.db = db
        self.overlay = {p.key: p for p in plan}
        self._all = {}

    def head(self, collection, id) -> Record | None:
        p = self.overlay.get((collection, id))
        if p is not None:
            return p.record
        return self.db.head(collection, id)

    def live(self, collection, id) -> Record | None:
        record = self.head(collection, id)
        return record if record is not None and not record.retired else None

    def version(self, collection, id, version) -> Record | None:
        p = self.overlay.get((collection, id))
        if p is not None and p.version == version:
            return p.record
        return self.db.version(collection, id, version)

    def live_all(self, collection) -> list:
        if collection not in self._all:
            records = {r.key: r for r in self.db.heads(collection)}
            for key, p in self.overlay.items():
                if key[0] == collection:
                    if p.body is None:
                        records.pop(key, None)
                    else:
                        records[key] = p.record
            self._all[collection] = sorted(records.values(), key=lambda r: r.id)
        return self._all[collection]

    def referrers(self, collection, id) -> list:
        rows = [(r["owner_collection"], r["owner_id"], r["field_path"])
                for r in self.db.live_referrers(collection, id)
                if (r["owner_collection"], r["owner_id"]) not in self.overlay]
        for p in self.overlay.values():
            if p.body is None:
                continue
            for ref in extract_refs(p.collection, p.body):
                if ref["target_collection"] == collection and ref["target_id"] == id:
                    rows.append((p.collection, p.id, ref["field_path"]))
        return rows

    def relation_members(self, relation, key) -> list:
        members = [m for m in relation_members(self.db.conn, relation, key) if (m[0], m[1]) not in self.overlay]
        owners = RELATIONS[relation][0]
        for p in self.overlay.values():
            if p.body is not None and p.collection in owners and body_members(p.collection, p.body, relation, key):
                members.append((p.collection, p.id, p.version))
        return members

    def has_blob(self, sha) -> bool:
        return self.db.has_blob(sha)

    def major_of(self, ref) -> Record | None:
        """The major item owning an item or part reference, or None when unresolved."""
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


def _ref(record: Record) -> dict:
    return {"collection": record.collection, "id": record.id}


def owners_of(state: State, collection: str, body: dict, _seen=None) -> set:
    """Owner keys of a record: ``item:<major>``, ``audit:<id>``, or ``paper``."""
    _seen = _seen or set()

    def via_major(ref):
        major = state.major_of(ref)
        return {f"item:{major.id}"} if major is not None else set()

    def via(coll, id):
        if (coll, id) in _seen:
            return set()
        rec = state.live(coll, id)
        if rec is None:
            return set()
        return owners_of(state, coll, rec.body, _seen | {(coll, id)})

    def via_target(target):
        if target["collection"] in ("items", "parts"):
            return via_major(target)
        if target["collection"] == "audits":
            return {f"audit:{target['id']}"}
        if target["collection"] in ("uses", "groups", "arguments"):
            return via(target["collection"], target["id"])
        return set()

    if collection == "items":
        if body["kind"] in MAJOR_KINDS:
            return {"new_major"}
        return via_major({"collection": "items", "id": body["owner_id"]}) if body["owner_id"] else set()
    if collection == "parts":
        return via_major({"collection": "items", "id": body["item_id"]})
    if collection == "arguments":
        return via_major(body["target"])
    if collection in ("groups", "coverage"):
        return via("arguments", body["argument_id"])
    if collection == "uses":
        return via_major(body["to"])
    if collection == "scopes":
        if body["argument_id"] is not None:
            return via("arguments", body["argument_id"])
        if body["parent_id"] is not None:
            return via("scopes", body["parent_id"])
        return {"global_scope"}
    if collection in ("checks", "findings", "reconciliations", "observations"):
        owners = via_target(body["target"])
        if "audit_id" in body:
            owners.add(f"audit:{body['audit_id']}")
        return owners
    if collection == "repairs":
        return via("findings", body["finding_id"])
    return {"paper"}


def _shape_errors(plan: list) -> tuple[list, set]:
    errors, bad = [], set()
    for p in plan:
        if p.body is None:
            if p.op != "retire":  # the null body is the retire sentinel, never a writable body
                bad.add(p.key)
                errors.append(f"edits/{p.index} {p.collection}:{p.id} body must be an object")
            continue
        messages = validate_body(p.collection, p.body)
        if messages:
            bad.add(p.key)
            errors.extend(f"edits/{p.index} {p.collection}:{p.id} {m}" for m in messages)
    return errors, bad


def _check_refs(state: State, p: Planned, errors: list):
    where = f"edits/{p.index} {p.collection}:{p.id}"
    for ref in extract_refs(p.collection, p.body):
        tc, ti, tv, path = ref["target_collection"], ref["target_id"], ref["target_version"], ref["field_path"]
        if tv is None:
            if state.live(tc, ti) is None:
                errors.append(f"{where} {path}: no live {tc} record {ti}")
        elif state.version(tc, ti, tv) is None:
            errors.append(f"{where} {path}: {tc} record {ti} has no version {tv}")


def _scope_chain(state: State, scope_id: str, limit=1000) -> list:
    chain, seen = [], set()
    current = scope_id
    while current is not None and current not in seen and len(chain) < limit:
        seen.add(current)
        scope = state.live("scopes", current)
        if scope is None:
            break
        chain.append(scope)
        current = scope.body["parent_id"]
    return chain


def _semantic(state: State, p: Planned, errors: list, command: str):
    """Collection-specific linked-state rules for one created or replaced record."""
    where = f"edits/{p.index} {p.collection}:{p.id}"
    body = p.body
    err = lambda message: errors.append(f"{where}: {message}")  # noqa: E731
    live = state.live
    c = p.collection

    if c == "papers":
        if p.prev is not None and p.prev.body["source_root"] != body["source_root"]:
            err("source_root is fixed at initialization")
        for item_id in body["main_items"]:
            item = live("items", item_id)
            if item is not None and item.body["kind"] not in MAJOR_KINDS:
                err(f"main item {item_id} is not a major result")
    elif c == "sources":
        papers = state.live_all("papers")
        if not any(paper.id == body["paper_id"] for paper in papers):
            err("paper_id must name this database's paper")
        if not state.has_blob(body["blob_sha256"]):
            err("source content blob is not stored")
    elif c == "anchors":
        if body["excerpt_sha256"] != sha256_bytes(body["excerpt"].encode("utf-8")):
            err("excerpt_sha256 does not match the excerpt")
        source = state.version("sources", body["source_id"], body["source_version"])
        if source is not None and body["locator"]["page"] is not None and source.body["media_type"] != "pdf":
            err("page locators require a PDF source")
    elif c == "items":
        if body["kind"] not in MAJOR_KINDS:
            owner = live("items", body["owner_id"]) if body["owner_id"] else None
            if owner is not None and owner.body["kind"] not in MAJOR_KINDS:
                err("an intermediate result's owner must be a major item")
            if owner is not None and owner.id == p.id:
                err("an item cannot own itself")
    elif c == "parts":
        item = live("items", body["item_id"])
        if item is not None and item.body["kind"] not in MAJOR_KINDS:
            err("parts belong to major items")
    elif c == "scopes":
        if body["parent_id"] == p.id:
            err("a scope cannot be its own parent")
        chain = _scope_chain(state, body["parent_id"]) if body["parent_id"] else []
        if any(s.id == p.id for s in chain):
            err("scope parent chain must be acyclic")
        parent = live("scopes", body["parent_id"]) if body["parent_id"] else None
        if parent is not None and parent.body["argument_id"] not in (None, body["argument_id"]):
            err("a scope's parent belongs to the same argument or is global")
    elif c == "arguments":
        scope = live("scopes", body["scope_id"])
        if scope is not None and scope.body["argument_id"] not in (None, p.id):
            err("an argument's scope is global or local to that argument")
        if body["final_group_id"] is not None:
            group = live("groups", body["final_group_id"])
            if group is not None:
                if group.body["argument_id"] != p.id:
                    err("the final group belongs to another argument")
                if group.body["conclusion"] != body["target"]:
                    err("the final group's conclusion must equal the argument target")
        if body["lifecycle"] == "retired":
            err("retire an argument with a retire edit, not lifecycle 'retired'")
    elif c == "groups":
        argument = live("arguments", body["argument_id"])
        if argument is not None:
            target = argument.body["target"]
            conclusion = body["conclusion"]
            legal = conclusion == target
            if not legal:
                major = state.major_of(target)
                if conclusion["collection"] == "items":
                    concl = live("items", conclusion["id"])
                    legal = (concl is not None and major is not None
                             and concl.body["kind"] not in MAJOR_KINDS and concl.body["owner_id"] == major.id)
                elif conclusion["collection"] == "parts" and target["collection"] == "items":
                    part = live("parts", conclusion["id"])
                    legal = part is not None and part.body["item_id"] == target["id"]
            if not legal:
                err("a group's conclusion is the argument target, one of its parts, or an intermediate "
                    "owned by the target's major result")
        scope = live("scopes", body["scope_id"])
        if scope is not None and scope.body["argument_id"] not in (None, body["argument_id"]):
            err("a group's scope is global or local to its argument")
        for sid in body["case_scope_ids"]:
            case = live("scopes", sid)
            if case is not None and case.body["argument_id"] != body["argument_id"]:
                err(f"case scope {sid} is not local to the argument")
        for sid in body["discharges"]:
            scope = live("scopes", sid)
            if scope is not None and scope.body["argument_id"] != body["argument_id"] \
                    and sid not in body["case_scope_ids"]:
                err(f"discharged scope {sid} is neither argument-local nor a case scope")
        for member in state.relation_members("uses_in_group", {"collection": "groups", "id": p.id}):
            use = live("uses", member[1])
            if use is not None and use.body["to"] != body["conclusion"]:
                err(f"member use {use.id} concludes {use.body['to']} but the group concludes {body['conclusion']}")
    elif c == "uses":
        if body["group_id"] is not None:
            group = live("groups", body["group_id"])
            if group is not None and group.body["conclusion"] != body["to"]:
                err("a grouped use must conclude the group's conclusion")
    elif c == "coverage":
        anchor = live("anchors", body["anchor_id"])
        if anchor is not None and body["end_offset"] > len(anchor.body["excerpt"]):
            err("coverage offsets exceed the anchor excerpt")
    elif c == "audits":
        for excl in body["exclusions"]:
            if excl["target"] is not None and live(excl["target"]["collection"], excl["target"]["id"]) is None:
                err("exclusion target is not a live record")
    elif c == "checks":
        if body["role"] == "independent" and command not in INDEPENDENT_CHECK_COMMANDS:
            err("independent checks are recorded only through review submit")
        target = live(body["target"]["collection"], body["target"]["id"])
        if body["kind"] in ("global_consistency", "adversarial", "method_interface") \
                and body["target"]["id"] != body["audit_id"]:
            err("global tasks target their own audit")
        if target is not None and body["kind"] == "case_coverage" and target.body["kind"] != "cases":
            err("case_coverage checks target case groups")
        if target is not None and body["kind"] == "scope_discharge" and not target.body["discharges"]:
            err("scope_discharge checks target groups that discharge a scope")
        if target is not None and body["kind"] == "application" and body["state"] == "complete" \
                and target.body["group_id"] is None:
            err("an application check completes only for a grouped use")
        if body["response_id"] is not None:
            response = live("responses", body["response_id"])
            if response is not None and response.body["audit_id"] != body["audit_id"]:
                err("check and response belong to different audits")
        if body["supersedes"] is not None:
            prior = state.version("checks", body["supersedes"]["id"], body["supersedes"]["version"])
            if body["supersedes"]["id"] == p.id:
                err("a check cannot supersede itself")
            elif prior is not None and not prior.retired:
                for field in ("audit_id", "role", "kind", "target"):
                    if prior.body[field] != body[field]:
                        err(f"a successor check keeps the predecessor's {field}")
    elif c == "findings":
        if body["target"]["collection"] not in FINDING_TARGETS:
            err(f"findings target {list(FINDING_TARGETS)}")
        for ref in body["check_refs"]:
            check = state.version("checks", ref["id"], ref["version"])
            if check is not None and not check.retired and check.body["audit_id"] != body["audit_id"]:
                err(f"check {ref['id']} belongs to another audit")
    elif c == "repairs":
        if body["kind"] == "restricted_statement" and body["supported_form"] is not None:
            form = live(body["supported_form"]["collection"], body["supported_form"]["id"])
            if form is not None and form.body["origin"] != "proposed_repair":
                err("a restricted statement is a separately identified proposed_repair statement")
        if body["kind"] == "supplemental_argument" and body["argument_id"] is not None:
            argument = live("arguments", body["argument_id"])
            if argument is not None and argument.body["origin"] != "proposed_repair":
                err("a supplemental argument has origin proposed_repair")
    elif c == "observations":
        if body["target"]["collection"] not in OBSERVATION_TARGETS:
            err(f"source fidelity observations target {list(OBSERVATION_TARGETS)}")
        elif body["target"]["collection"] in ("items", "parts"):
            target = live(body["target"]["collection"], body["target"]["id"])
            if target is not None and target.body["origin"] == "source" and not target.body["passages"] \
                    and body["result"] == "matched":
                err("a source-origin statement needs at least one passage before fidelity is confirmed")
    elif c == "responses":
        if not state.has_blob(body["original_blob"]):
            err("original response blob is not stored")
    elif c == "qualifications":
        if not state.has_blob(body["evidence_blob"]):
            err("qualification evidence blob is not stored")
        for case in body["valid_case_results"] + body["invalid_case_results"]:
            if not state.has_blob(case["response_blob"]):
                err(f"calibration response blob {case['case_id']} is not stored")
    elif c == "reconciliations":
        if body["target"]["collection"] not in ("items", "parts", "uses", "groups", "arguments", "audits"):
            err("reconciliations target assessed records")
        for field, role in (("primary_checks", "primary"), ("independent_checks", "independent"),
                            ("successor_checks", None)):
            for ref in body[field]:
                check = state.version("checks", ref["id"], ref["version"])
                if check is None or check.retired:
                    continue
                if check.body["audit_id"] != body["audit_id"] or check.body["target"] != body["target"]:
                    err(f"{field} entry {ref['id']} has another audit or target")
                if role is not None and check.body["role"] != role:
                    err(f"{field} entry {ref['id']} has role {check.body['role']}")
                if check.body["role"] == "independent":
                    response = state.live("responses", check.body["response_id"])
                    if response is None or response.body["state"] != "accepted":
                        err(f"{field} entry {ref['id']} belongs to an independent response that is not accepted")
        listed = {ref["id"] for ref in body["independent_checks"]}
        for response in state.live_all("responses"):
            if response.body["audit_id"] != body["audit_id"] or not _response_covers(state, response, body["target"]):
                continue
            if response.body["state"] != "accepted":
                # Preserved incomplete attempts carry no independent credit. They
                # remain visible, but cannot permanently veto a corrected review.
                continue
            for check in state.live_all("checks"):
                if check.body["response_id"] == response.id and check.body["target"] == body["target"] \
                        and check.id not in listed:
                    err(f"independent check {check.id} from response {response.id} is not listed")
        if body["supersedes"] is not None:
            if body["supersedes"]["id"] == p.id:
                err("a reconciliation cannot supersede itself")
            prior = state.version("reconciliations", body["supersedes"]["id"], body["supersedes"]["version"])
            if prior is not None and not prior.retired and (
                    prior.body["audit_id"] != body["audit_id"] or prior.body["target"] != body["target"]):
                err("a successor reconciliation keeps the audit and target")
    elif c == "reuse_decisions":
        check_ref = body["check_ref"]
        check = state.version("checks", check_ref["id"], check_ref["version"])
        head = live("checks", check_ref["id"])
        if check is None or check.retired or head is None:
            err("check_ref names an unknown or retired check")
            check = None
        else:
            if head.version != check_ref["version"]:
                err("check_ref must pin the live version of the check")
            if check.body["state"] != "complete":
                err("reuse decisions apply to completed checks")
        review = live("source_reviews", body["source_review_id"])
        if review is None:
            err("source_review_id names an unknown or retired source review")
        elif review.body["decision"] != "accepted":
            err("reuse requires an accepted source review")
        if body["decision"] == "reusable" and check is not None:
            # record-contract 6: reusable only for exact source relocation/context changes approved by the
            # review; any consumed mathematical facet or membership change needs a new check.
            stored = state.db.binding("checks", check.id, check.version)
            if stored is None:
                err("the check has no stored evidence binding; save a new check")
            else:
                changes = binding_changes(state, stored["bindings"])
                if changes["relations"]:
                    err("membership changed since the check was saved; not eligible for reuse, save a new check")
                reviewed = set()
                if review is not None:
                    for ref in review.body["source_refs"] + review.body["anchor_refs"]:
                        reviewed.add((ref["collection"], ref["id"], ref["version"]))
                for change in changes["records"]:
                    ref = change["ref"]
                    if change["facet"] != "source" or change["actual"] is None:
                        err(f"{ref['collection']}:{ref['id']} changed its {change['facet']} facet; "
                            "not eligible for reuse, save a new check")
                    elif (ref["collection"], ref["id"], change["live_version"]) not in reviewed:
                        err(f"{ref['collection']}:{ref['id']} version {change['live_version']} "
                            "is not covered by the source review")
    elif c == "identity_maps":
        if body["source_blob"] is not None and not state.has_blob(body["source_blob"]):
            err("identity map source blob is not stored")
        if command == "apply" and body["reason"] in ("import", "response_mapping"):
            err(f"identity maps with reason {body['reason']} come from dedicated commands")


def _owner_major(state: State, ref: dict):
    """Major item whose result an assessed record belongs to (items, parts, uses, groups, arguments)."""
    c = ref["collection"]
    if c in ("items", "parts"):
        return state.major_of(ref)
    record = state.live(c, ref["id"])
    if record is None:
        return None
    if c == "uses":
        return state.major_of(record.body["to"])
    if c == "groups":
        return state.major_of(record.body["conclusion"])
    if c == "arguments":
        return state.major_of(record.body["target"])
    return None


def _response_covers(state: State, response: Record, target: dict) -> bool:
    if target["collection"] == "audits":
        return True
    major = _owner_major(state, target)
    for covered in response.body["covered_targets"]:
        if covered == target:
            return True
        covered_major = state.major_of(covered)
        if major is not None and covered_major is not None and covered_major.id == major.id:
            return True
    return False


def _immutability(state: State, p: Planned, errors: list, command: str):
    where = f"edits/{p.index} {p.collection}:{p.id}"
    prev = p.prev
    if prev is None or prev.retired:
        return
    if p.collection in IMMUTABLE_ONCE_CREATED:
        errors.append(f"{where}: {p.collection} records are immutable once recorded")
    elif p.collection == "checks" and prev.body["state"] == "complete":
        errors.append(f"{where}: completed checks are immutable; record a successor check with supersedes")
    elif p.collection == "responses" and prev.body["state"] == "accepted":
        errors.append(f"{where}: accepted responses are immutable")
    elif p.collection == "source_reviews" and prev.body["decision"] == "accepted":
        errors.append(f"{where}: accepted source reviews are immutable")
    if p.op == "replace" and p.collection == "responses":
        changed = [k for k in prev.body if prev.body[k] != p.body.get(k)]
        if command != "review_map" or changed not in ([], ["state"]):
            errors.append(f"{where}: a response only advances its state through review map (changed {changed})")


def _retire(state: State, p: Planned, errors: list):
    where = f"edits/{p.index} {p.collection}:{p.id}"
    if p.prev is None or p.prev.retired:
        return
    if p.collection == "checks" and p.prev.body["state"] == "complete":
        errors.append(f"{where}: completed checks cannot be retired")
    if p.collection in IMMUTABLE_ONCE_CREATED:
        errors.append(f"{where}: {p.collection} records cannot be retired")
    referrers = state.referrers(p.collection, p.id)
    if referrers:
        listing = ", ".join(f"{oc}:{oi}{path}" for oc, oi, path in referrers[:8])
        errors.append(f"{where}: still referenced by live records ({listing})")


def _unique_applications(state: State, plan_uses: list, errors: list):
    if not plan_uses:
        return
    seen = {}
    for use in state.live_all("uses"):
        identity = tuple(repr(use.body[field]) for field in USE_IDENTITY)
        seen.setdefault(identity, []).append(use.id)
    for p in plan_uses:
        identity = tuple(repr(p.body[field]) for field in USE_IDENTITY)
        others = [i for i in seen.get(identity, []) if i != p.id]
        if others:
            errors.append(f"edits/{p.index} uses:{p.id}: duplicates the application identity of {others}")


def _scope(state: State, plan: list, errors: list, targets: list):
    """New records must fall within the packet's declared target scope (handoff 4.2)."""
    if any(t["collection"] == "papers" for t in targets):
        return
    allowed = set()
    for t in targets:
        if t["collection"] in ("items", "parts"):
            major = state.major_of(t)
            if major is not None:
                allowed.add(f"item:{major.id}")
        elif t["collection"] == "audits":
            allowed.add(f"audit:{t['id']}")
    for p in plan:
        if p.op != "create" or p.collection in PAPER_LEVEL:
            continue
        owners = owners_of(state, p.collection, p.body)
        if "new_major" in owners:
            errors.append(f"edits/{p.index} {p.collection}:{p.id}: new major items need a paper-scoped packet")
            continue
        if "global_scope" in owners:
            referrer_owners = set()
            for oc, oi, _ in state.referrers(p.collection, p.id):
                rec = state.live(oc, oi)
                if rec is not None:
                    referrer_owners |= owners_of(state, oc, rec.body)
            owners = referrer_owners or {"global_scope"}
        if not owners or not owners & allowed:
            errors.append(f"edits/{p.index} {p.collection}:{p.id}: outside the packet's target scope "
                          f"(owners {sorted(owners)}, allowed {sorted(allowed)})")


def _work_scope(state: State, plan: list, errors: list, work, context_refs, *, mode="primary"):
    """Exact assignment authority, including auxiliary records and reconciliation.

    Context is the immutable active packet's original read set, not the narrowed
    freshness read set. A supplied lower statement permits a finding about that
    input, but it does not authorize an extra check slot against that statement.
    """
    if not isinstance(work, dict) or work.get("mode") != mode:
        errors.append(f"work submission requires an exact {mode} assignment")
        return
    context = {(r["collection"], r["id"]) for r in context_refs or []}
    pins = {(r["collection"], r["id"], r["version"]) for r in context_refs or []}
    generated = {(p.collection, p.id) for p in plan if p.op == "create" and p.body is not None}
    generated_pins = {(p.collection, p.id, p.version) for p in plan if p.body is not None}
    slots = {(t["target"]["collection"], t["target"]["id"], t["kind"], t["role"], t["action"])
             for t in work["tasks"]}
    targets = {(t["target"]["collection"], t["target"]["id"]) for t in work["tasks"]}
    if mode == "reconcile":
        for task in work["tasks"]:
            for entry in task.get("consumed_inputs", []):
                ref = entry["ref"]
                if ref["collection"] == "checks":
                    check = state.version("checks", ref["id"], ref["version"])
                    if check is not None and not check.retired and check.body["audit_id"] == work["audit_id"]:
                        targets.add((check.body["target"]["collection"], check.body["target"]["id"]))
    arguments = {id for collection, id in targets if collection == "arguments"}
    argument = work.get("context", {}).get("argument")
    if argument:
        arguments.add(argument["id"])
    draft_pins = {tuple(sorted(ref.items())) for t in work["tasks"] for ref in t.get("draft_refs", [])}

    def supplied(p, ref, field, *, pinned=False, allow_generated=False):
        key = ((ref["collection"], ref["id"], ref["version"]) if pinned
               else (ref["collection"], ref["id"]))
        permitted = pins if pinned else context
        created = generated_pins if pinned else generated
        if key not in permitted and not (allow_generated and key in created):
            errors.append(f"edits/{p.index}/{field}: reference is outside the supplied assignment context")

    for p in plan:
        if p.body is None:
            continue
        body = p.body
        if mode == "reconcile":
            if p.collection not in ("checks", "findings", "reconciliations") \
                    or (body["target"]["collection"], body["target"]["id"]) not in targets \
                    or body["audit_id"] != work["audit_id"]:
                errors.append(f"edits/{p.index}: record is outside the reconciliation assignment")
            if p.collection == "checks" and body["role"] != "primary":
                errors.append(f"edits/{p.index}: reconciliation cannot author independent judgments")
        for anchor_id in body.get("evidence_refs", []):
            supplied(p, {"collection": "anchors", "id": anchor_id}, "evidence_refs")
        if body.get("supersedes"):
            supplied(p, body["supersedes"], "supersedes", pinned=True)
        if p.collection == "checks":
            slot = (*((body["target"]["collection"], body["target"]["id"])),
                    body["kind"], body["role"], "check")
            if mode == "primary" and (slot not in slots or body["audit_id"] != work["audit_id"]):
                errors.append(f"edits/{p.index}: check is outside the assigned task slots")
            if p.prev is not None:
                if ((mode == "primary" and tuple(sorted(p.prev.pinned.items())) not in draft_pins)
                        or p.prev.body["reviewer"] != body["reviewer"]
                        or p.prev.body["state"] != "draft"):
                    errors.append(f"edits/{p.index}: replacement is not an assigned draft of this reviewer")
                if p.prev.body["supersedes"] != body["supersedes"]:
                    errors.append(f"edits/{p.index}: resuming a draft preserves its supersedes reference")
        elif p.collection == "observations":
            slot = (body["target"]["collection"], body["target"]["id"],
                    "source_fidelity", "primary", "compare_source")
            if slot not in slots:
                errors.append(f"edits/{p.index}: comparison is outside the assigned task slots")
        elif p.collection == "coverage":
            if body["argument_id"] not in arguments:
                errors.append(f"edits/{p.index}: coverage argument is outside the assignment")
            supplied(p, {"collection": "anchors", "id": body["anchor_id"]}, "anchor_id")
            for ref in body["claim_refs"]:
                supplied(p, ref, "claim_refs")
            for check_id in body["check_ids"]:
                supplied(p, {"collection": "checks", "id": check_id}, "check_ids", allow_generated=True)
            if p.prev is not None:
                supplied(p, p.prev.pinned, "expected_version", pinned=True)
                if p.prev.body["argument_id"] != body["argument_id"]:
                    errors.append(f"edits/{p.index}: replacement coverage keeps its argument")
        elif p.collection == "findings":
            supplied(p, body["target"], "target")
            if body["audit_id"] != work["audit_id"]:
                errors.append(f"edits/{p.index}: finding belongs to another audit")
            if body["target"]["collection"] == "audits" \
                    and ("audits", body["target"]["id"]) not in targets:
                errors.append(f"edits/{p.index}: global finding needs an assigned global task")
            for ref in body["check_refs"]:
                supplied(p, ref, "check_refs", pinned=True, allow_generated=True)
            for use_id in body["affected_uses"]:
                supplied(p, {"collection": "uses", "id": use_id}, "affected_uses")
        elif p.collection == "reconciliations":
            for field in ("primary_checks", "independent_checks", "successor_checks"):
                for ref in body[field]:
                    supplied(p, ref, field, pinned=True, allow_generated=True)


def validate_plan(db: Database, plan: list, *, command: str, targets: list, work=None, context_refs=None) -> list:
    """Return all semantic errors for a planned batch, or an empty list."""
    errors, bad = _shape_errors(plan)
    state = State(db, plan)
    allowed = COMMANDS[command]
    for p in plan:
        if p.collection not in allowed[p.op]:
            errors.append(f"edits/{p.index}: command {command} cannot {p.op} {p.collection} records")
    for p in plan:
        if p.op == "retire":
            _retire(state, p, errors)
            continue
        if p.key in bad:
            continue
        _check_refs(state, p, errors)
        _semantic(state, p, errors, command)
        if p.op == "replace":
            _immutability(state, p, errors, command)
    _unique_applications(state, [p for p in plan if p.collection == "uses" and p.body is not None
                                 and p.key not in bad], errors)
    if not errors and command in ("apply", "compare", "reconcile") and not (command == "reconcile" and work):
        _scope(state, plan, errors, targets)
    if not errors and command == "work_primary":
        _work_scope(state, plan, errors, work, context_refs)
    if not errors and command == "reconcile" and work:
        _work_scope(state, plan, errors, work, context_refs, mode="reconcile")
    return errors


__all__ = ["COMMANDS", "Planned", "State", "owners_of", "validate_plan"]
