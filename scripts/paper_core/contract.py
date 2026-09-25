"""Record contract 4: closed body schemas, local checks, and reference extraction.

Every stored body must contain exactly the listed fields; unknown fields are
rejected. The same schema drives reference-index extraction (record-contract 1.1).
"""
from __future__ import annotations

import re

from .ids import COLLECTIONS, valid_id

HEX64 = re.compile(r"^[0-9a-f]{64}$")
CHECK_KINDS = ("derivation", "application", "composition", "case_coverage",
               "scope_discharge", "external_source", "global_consistency",
               "adversarial", "method_interface")
CHECK_TARGETS = {
    "derivation": ("groups",), "application": ("uses",), "composition": ("arguments",),
    "case_coverage": ("groups",), "scope_discharge": ("groups",),
    "external_source": ("items", "parts"), "global_consistency": ("audits",),
    "adversarial": ("audits",), "method_interface": ("audits",),
}
GLOBAL_TASK_KINDS = ("global_consistency", "adversarial", "method_interface")
MAJOR_KINDS = ("assumption", "definition", "lemma", "proposition", "theorem",
               "corollary", "external_result")
# Intermediate rows live inside a major row's statement or proof and must name
# it as owner. ``intermediate_result`` is the audit core's own kind; the other
# three are the overview vocabulary (feature overview-bridge/1).
INTERMEDIATE_KINDS = ("intermediate_result", "equation", "claim", "derivation")
ITEM_KINDS = MAJOR_KINDS + INTERMEDIATE_KINDS
OUTCOMES = ("supported", "gap", "refuted", "inconclusive")


def pointer(*tokens) -> str:
    """Body-relative JSON Pointer for the given path tokens."""
    return "".join("/" + str(t).replace("~", "~0").replace("/", "~1") for t in tokens)


class T:
    nullable = False

    def check(self, value, path, errors):
        raise NotImplementedError

    def refs(self, value, path):
        return ()

    def validate(self, value, path, errors):
        if value is None:
            if not self.nullable:
                errors.append(f"{path or '/'}: null is not allowed")
            return
        self.check(value, path, errors)


class Str(T):
    def __init__(self, nonempty=False, nullable=False):
        self.nonempty, self.nullable = nonempty, nullable

    def check(self, value, path, errors):
        if not isinstance(value, str):
            errors.append(f"{path}: expected string")
        elif self.nonempty and not value.strip():
            errors.append(f"{path}: must be nonempty text")


class Int(T):
    def __init__(self, minimum=None, nullable=False):
        self.minimum, self.nullable = minimum, nullable

    def check(self, value, path, errors):
        if not isinstance(value, int) or isinstance(value, bool):
            errors.append(f"{path}: expected integer")
        elif self.minimum is not None and value < self.minimum:
            errors.append(f"{path}: must be >= {self.minimum}")


class Bool(T):
    def check(self, value, path, errors):
        if not isinstance(value, bool):
            errors.append(f"{path}: expected boolean")


class Const(T):
    def __init__(self, expected):
        self.expected = expected

    def check(self, value, path, errors):
        if value != self.expected or type(value) is not type(self.expected):
            errors.append(f"{path}: must equal {self.expected!r}")


class RequestVersion(T):
    """Old envelopes remain accepted; their bodies are normalized before storage."""
    def check(self, value, path, errors):
        if type(value) is not int or value not in (3, 4):
            errors.append(f"{path}: supported request contract versions are 3 and 4")


class Null(T):
    nullable = True

    def check(self, value, path, errors):
        errors.append(f"{path}: must be null")


class Any(T):
    nullable = True

    def check(self, value, path, errors):
        return None


class Enum(T):
    def __init__(self, values, nullable=False):
        self.values, self.nullable = tuple(values), nullable

    def check(self, value, path, errors):
        if not isinstance(value, str) or value not in self.values:
            errors.append(f"{path}: must be one of {list(self.values)}")


class Id(T):
    """A scalar ID. ``target`` names the referenced collection (None: opaque)."""

    def __init__(self, target=None, nullable=False, version_sibling=None):
        self.target, self.nullable, self.version_sibling = target, nullable, version_sibling

    def check(self, value, path, errors):
        if not valid_id(value):
            errors.append(f"{path}: invalid identifier")

    def refs(self, value, path):
        if self.target and valid_id(value):
            yield (path, self.target, value, None)


class Hash(T):
    def __init__(self, nullable=False):
        self.nullable = nullable

    def check(self, value, path, errors):
        if not isinstance(value, str) or not HEX64.match(value):
            errors.append(f"{path}: expected lowercase SHA-256 hex digest")


class Arr(T):
    def __init__(self, inner, nonempty=False):
        self.inner, self.nonempty = inner, nonempty

    def check(self, value, path, errors):
        if not isinstance(value, list):
            errors.append(f"{path}: expected array")
            return
        if self.nonempty and not value:
            errors.append(f"{path}: must be nonempty")
        for index, entry in enumerate(value):
            self.inner.validate(entry, f"{path}/{index}", errors)

    def refs(self, value, path):
        if isinstance(value, list):
            for index, entry in enumerate(value):
                yield from self.inner.refs(entry, f"{path}/{index}")


class Obj(T):
    def __init__(self, fields, nullable=False, local=None, optional=()):
        self.fields, self.nullable, self.local = dict(fields), nullable, local
        # Optional fields may be absent; when present they validate like any other field.
        self.optional = frozenset(optional)
        if not self.optional <= set(self.fields):
            raise ValueError("optional names must be declared fields")

    def check(self, value, path, errors):
        if not isinstance(value, dict):
            errors.append(f"{path or '/'}: expected object with fields {list(self.fields)}; "
                          f"got {type(value).__name__}; use the generated shape")
            return
        unknown = sorted(set(value) - set(self.fields))
        missing = [name for name in self.fields if name not in value and name not in self.optional]
        for name in unknown:
            errors.append(f"{path}{pointer(name)}: unknown field")
        for name in missing:
            errors.append(f"{path}{pointer(name)}: missing required field")
        before = len(errors)
        for name, spec in self.fields.items():
            if name in value:
                spec.validate(value[name], f"{path}{pointer(name)}", errors)
        if self.local and not unknown and not missing and len(errors) == before:
            for message in self.local(value):
                errors.append(f"{path or '/'}: {message}")

    def refs(self, value, path):
        if not isinstance(value, dict):
            return
        for name, spec in self.fields.items():
            if name not in value or value[name] is None:
                continue
            child = f"{path}{pointer(name)}"
            if isinstance(spec, Id) and spec.version_sibling:
                if spec.target and valid_id(value[name]):
                    yield (child, spec.target, value[name], value.get(spec.version_sibling))
                continue
            yield from spec.refs(value[name], child)


class RefT(T):
    """Structured Ref/PinnedRef indexed at the object itself."""

    def __init__(self, collections=COLLECTIONS, pinned=False, nullable=False):
        self.collections, self.pinned, self.nullable = tuple(collections), pinned, nullable

    def check(self, value, path, errors):
        expected = {"collection", "id"} | ({"version"} if self.pinned else set())
        shape = "PinnedRef" if self.pinned else "Ref"
        if not isinstance(value, dict) or set(value) != expected:
            actual = f"object keys {sorted(value)}" if isinstance(value, dict) else type(value).__name__
            errors.append(f"{path}: expected {shape} with keys {sorted(expected)}; got {actual}")
            return
        if value["collection"] not in self.collections:
            errors.append(f"{path}/collection: must be one of {list(self.collections)}")
        if not valid_id(value["id"]):
            errors.append(f"{path}/id: invalid identifier")
        if self.pinned and (not isinstance(value["version"], int) or isinstance(value["version"], bool)
                            or value["version"] < 1):
            errors.append(f"{path}/version: expected positive integer")

    def refs(self, value, path):
        if isinstance(value, dict) and value.get("collection") in self.collections and valid_id(value.get("id")):
            version = value.get("version") if self.pinned else None
            yield (path, value["collection"], value["id"], version)


class OneOf(T):
    """Alternatives distinguished by their exact key sets."""

    def __init__(self, options, nullable=False):
        self.options, self.nullable = list(options), nullable

    def _pick(self, value):
        if not isinstance(value, dict):
            return None
        candidates = [option for option in self.options if set(option.fields) == set(value)]
        for option in candidates:
            if all(value.get(name) == field.expected for name, field in option.fields.items()
                   if isinstance(field, Const)):
                return option
        return candidates[0] if candidates else None

    def check(self, value, path, errors):
        option = self._pick(value)
        if option is None:
            shapes = [sorted(option.fields) for option in self.options]
            actual = f"object keys {sorted(value)}" if isinstance(value, dict) else type(value).__name__
            errors.append(f"{path}: object does not match any allowed shape; expected one of {shapes}; "
                          f"got {actual}; keep required nullable fields present")
            return
        option.validate(value, path, errors)

    def refs(self, value, path):
        option = self._pick(value)
        if option is not None:
            yield from option.refs(value, path)


def _locator_local(value):
    start, end = value["start_line"], value["end_line"]
    if (start is None) != (end is None):
        yield "start_line and end_line must appear together"
    if start is not None and end is not None and start > end:
        yield "start_line must not exceed end_line"
    if start is None and value["page"] is None and value["label"] is None:
        yield "at least one location method is required"


REF = RefT()
TARGET = RefT(("items", "parts"))
PINNED = RefT(pinned=True)
STATEMENT_FIELDS = {"form": Enum(("verbatim", "transcription", "synopsis")), "text": Str(nonempty=True)}
STATEMENT = Obj(STATEMENT_FIELDS)
PASSAGE = Obj({"role": Enum(("statement", "proof", "definition", "evidence")), "anchor_id": Id("anchors")})
ORIGIN = Enum(("source", "reconstruction", "proposed_repair"))
LOCATOR = Obj({"start_line": Int(1, nullable=True), "end_line": Int(1, nullable=True),
               "page": Int(1, nullable=True), "label": Str(nonempty=True, nullable=True)}, local=_locator_local)
BINDER = Obj({"symbol": Str(nonempty=True), "domain": Str(nonempty=True),
              "quantifier": Enum(("fixed", "forall", "exists"))})
SUBSTITUTION = Obj({"symbol": Str(nonempty=True), "value": Str(nonempty=True)})
EXCLUSION = Obj({"target": RefT(nullable=True), "source_anchor_ids": Arr(Id("anchors")),
                 "reason": Str(nonempty=True), "consequence": Str(nonempty=True)},
                local=lambda v: (["exclusion needs a target or a source location"]
                                 if v["target"] is None and not v["source_anchor_ids"] else []))
GLOBAL_TASK = Obj({"kind": Enum(GLOBAL_TASK_KINDS), "applicability": Enum(("required", "not_applicable")),
                   "reason": Str()},
                  local=lambda v: (["a not_applicable task needs a nonempty reason"]
                                   if v["applicability"] == "not_applicable" and not v["reason"].strip() else []))
PROFILE = Obj({"provider": Str(nonempty=True), "model": Str(nonempty=True), "effort": Str(nullable=True),
               "tools": Arr(Str(nonempty=True)), "context_isolation": Str(nonempty=True)})
CALIBRATION = Obj({"case_id": Str(nonempty=True), "response_blob": Hash(),
                   "outcome": Enum(("pass", "fail", "inconclusive"))})
MAPPING = Obj({"old": Str(nonempty=True), "new_refs": Arr(REF), "rationale": Str(nonempty=True)})
EXPOSURE_REPORT = Obj({"status": Enum(("none_known", "possible_exposure")), "note": Str()})
SOURCE_TARGET = Obj({"source_anchor_id": Id("anchors"), "description": Str(nonempty=True)})
REF_OBJECT = Obj({"collection": Enum(COLLECTIONS), "id": Id()})


def _items_local(v):
    if v["kind"] in INTERMEDIATE_KINDS and v["owner_id"] is None:
        yield "an intermediate result needs exactly one owner_id"
    if v["kind"] not in INTERMEDIATE_KINDS and v["owner_id"] is not None:
        yield "a major item has owner_id null"


def _arguments_local(v):
    if v["lifecycle"] == "registered" and v["final_group_id"] is None:
        yield "a registered argument needs a final group"


def _groups_local(v):
    if (v["argument_id"] is None) != (v["scope_id"] is None):
        yield "a provenance group has argument_id and scope_id both null; an argument group keeps both"
    if v["kind"] == "joint" and v["case_scope_ids"]:
        yield "a joint group has empty case_scope_ids"
    if v["kind"] == "cases" and v["scope_id"] is not None and not v["case_scope_ids"]:
        yield "a cases group needs nonempty case_scope_ids"
    if len(set(v["case_scope_ids"])) != len(v["case_scope_ids"]):
        yield "case_scope_ids must be distinct"
    if len(set(v["discharges"])) != len(v["discharges"]):
        yield "discharges must be distinct"


def _uses_local(v):
    if v["from"] == v["to"]:
        yield "a use cannot connect a statement to itself"


def _target_spec_local(v):
    if (v['statement_ref'] is None) == (v['statement'] is None):
        yield "specify either statement_ref or exact statement, never both"
    if v['statement_ref'] is not None and any(v['statement_ref'][k] != v['target'][k] for k in ('collection', 'id')):
        yield "statement_ref must pin this target's exact statement"


def _application_local(v):
    if v['state'] == 'registered' and (v['group_id'] is None or v['needed_form'] is None):
        yield "a registered application needs its inference group and exact required form"


def _coverage_local(v):
    if v["end_offset"] < v["start_offset"]:
        yield "end_offset must be >= start_offset"
    if v["classification"] == "structural" and (v["claim_refs"] or v["check_ids"]):
        yield "structural coverage has empty claim_refs and check_ids"


def _audits_local(v):
    kinds = [task["kind"] for task in v["global_tasks"]]
    if len(set(kinds)) != len(kinds):
        yield "global task kinds must be distinct"
    if v["mode"] in ("full", "focused") and sorted(kinds) != sorted(GLOBAL_TASK_KINDS):
        yield "full/focused audits list exactly the three global tasks"
    if len({(t["collection"], t["id"]) for t in v["targets"]}) != len(v["targets"]):
        yield "audit targets must be distinct"


def _checks_local(v):
    if v["target"]["collection"] not in CHECK_TARGETS[v["kind"]]:
        yield f"check kind {v['kind']} targets {list(CHECK_TARGETS[v['kind']])}, not {v['target']['collection']}"
    if v["state"] == "complete":
        if v["outcome"] is None:
            yield "a complete check needs a non-null outcome"
        if not v["reasoning"].strip():
            yield "a complete check needs nonempty reasoning"
    if v["role"] == "independent" and v["response_id"] is None:
        yield "an independent check carries its response_id"
    if v["role"] == "primary" and v["response_id"] is not None:
        yield "a primary check has response_id null"


def _findings_local(v):
    if v["lifecycle"] == "resolved" and not (v["resolution"] or "").strip():
        yield "a resolved finding needs a nonempty resolution"


def _repairs_local(v):
    if v["kind"] == "restricted_statement" and v["supported_form"] is None:
        yield "a restricted statement repair names its supported form"
    if v["kind"] == "supplemental_argument" and v["argument_id"] is None:
        yield "a supplemental argument repair names its argument"


def _source_issues_local(v):
    if v["lifecycle"] == "resolved" and not (v["resolution"] or "").strip():
        yield "a resolved source issue needs a nonempty resolution"


def _qualifications_local(v):
    if v["qualified"] and (not v["valid_case_results"] or not v["invalid_case_results"]):
        yield "qualified requires calibration evidence from both valid and invalid cases"
    if v["qualified"]:
        cases = v["valid_case_results"] + v["invalid_case_results"]
        if any(case["outcome"] != "pass" for case in cases):
            yield "qualified requires every recorded calibration case to pass"
        if len({case["case_id"] for case in cases}) != len(cases):
            yield "qualified requires distinct calibration case IDs across valid and invalid cases"


def _responses_local(v):
    if v["exposure"] == "compromised" and not v["exposure_note"].strip():
        yield "a compromised response needs a nonempty exposure note"


def _reconciliations_local(v):
    if v["decision"] in ("primary_revised", "independent_revised") and not v["successor_checks"]:
        yield "a revised decision needs successor checks"


def _identity_maps_local(v):
    if v["reason"] == "response_mapping" and v["response_id"] is None:
        yield "a response mapping names its response"
    if v["reason"] != "response_mapping" and v["response_id"] is not None:
        yield "only response mappings carry a response_id"


BODY_SCHEMAS = {
    # Overview bridging (feature overview-bridge/1): migrate-overview carries the legacy
    # scope text and the explicit inventory exclusions onto the paper record; databases
    # authored without a legacy overview omit both optional fields.
    "papers": Obj({"title": Str(nonempty=True), "source_root": Str(nonempty=True),
                   "main_items": Arr(Id("items")), "report_paths": Arr(Str(nonempty=True)),
                   "scope": Str(nullable=True), "exclusions": Arr(Str(nonempty=True))},
                  optional=("scope", "exclusions")),
    "sources": Obj({"paper_id": Id("papers"), "path": Str(nonempty=True),
                    "media_type": Enum(("tex", "pdf", "bib", "text", "other")), "blob_sha256": Hash(),
                    "capture_method": Str(nonempty=True), "limitation": Str(nullable=True)}),
    "anchors": Obj({"source_id": Id("sources", version_sibling="source_version"), "source_version": Int(1),
                    "locator": LOCATOR, "excerpt": Str(), "excerpt_sha256": Hash(),
                    "method": Enum(("exact_lines", "label_match", "exact_relocation", "reviewed_page",
                                    "reviewed_span")),
                    "limitation": Str(nullable=True)}),
    # Overview bridging (feature overview-bridge/1): migrate-overview keeps the legacy
    # comparison timestamp so chronological precedence stays machine-readable; comparisons
    # recorded after the migration omit the optional field and order by revision and id.
    "observations": Obj({"target": REF, "result": Enum(("matched", "needs_attention")),
                         "reviewer": Str(nonempty=True), "note": Str(), "evidence_refs": Arr(Id("anchors")),
                         "created_at": Str(nonempty=True),
                         "context_kind": Enum(("overview", "exact_target")), "context_data": Any()},
                        optional=("created_at", "context_kind", "context_data")),
    "source_issues": Obj({"source_id": Id("sources"), "anchor_id": Id("anchors", nullable=True),
                          "category": Enum(("unresolved_branch", "ambiguous_label", "missing_source",
                                            "missing_citation", "locator_limit", "other_resolution")),
                          "description": Str(nonempty=True), "lifecycle": Enum(("open", "resolved", "superseded")),
                          "resolution": Str(nullable=True), "reviewer": Str(nonempty=True)},
                         local=_source_issues_local),
    "source_reviews": Obj({"source_refs": Arr(RefT(("sources",), pinned=True)),
                           "anchor_refs": Arr(RefT(("anchors",), pinned=True)),
                           "purpose": Enum(("branch_selection", "locator_confirmation", "context_change", "proof_boundary")),
                           "decision": Enum(("accepted", "unresolved")), "rationale": Str(nonempty=True),
                           "reviewer": Str(nonempty=True)}),
    "items": Obj({"kind": Enum(ITEM_KINDS), "label": Str(nonempty=True), "caption": Str(), "statement": STATEMENT,
                  "passages": Arr(PASSAGE), "aliases": Arr(Str(nonempty=True)), "uncertainty": Str(nullable=True),
                  "origin": ORIGIN, "owner_id": Id("items", nullable=True), "scope_id": Id("scopes", nullable=True),
                  "proof_idea": Str(nonempty=True)}, optional=("proof_idea",), local=_items_local),
    "parts": Obj({"item_id": Id("items"), "label": Str(nonempty=True), "statement": STATEMENT,
                  "passages": Arr(PASSAGE), "scope_id": Id("scopes", nullable=True), "origin": ORIGIN}),
    "scopes": Obj({"argument_id": Id("arguments", nullable=True), "parent_id": Id("scopes", nullable=True),
                   "assumptions": Arr(TARGET), "binders": Arr(BINDER), "conditions": Arr(Str(nonempty=True)),
                   "evidence_refs": Arr(Id("anchors"))}),
    "arguments": Obj({"target": TARGET, "label": Str(nonempty=True), "origin": ORIGIN, "scope_id": Id("scopes"),
                      "final_group_id": Id("groups", nullable=True), "evidence_refs": Arr(Id("anchors")),
                      "lifecycle": Enum(("draft", "registered", "retired"))}, local=_arguments_local),
    "groups": Obj({"argument_id": Id("arguments", nullable=True), "conclusion": TARGET, "kind": Enum(("joint", "cases")),
                   "scope_id": Id("scopes", nullable=True), "case_scope_ids": Arr(Id("scopes")), "discharges": Arr(Id("scopes")),
                   "rationale": Str(nonempty=True), "evidence_refs": Arr(Id("anchors"))}, local=_groups_local),
    "uses": Obj({"from": TARGET, "to": TARGET, "type": Enum(("dependency", "definition", "proof_argument")),
                 "reason": Str(nonempty=True),
                 "evidence_refs": Arr(Id("anchors")), "regime": Str(nullable=True), "uncertainty": Str(nullable=True)},
                local=_uses_local),
    "application_details": Obj({"use_id": Id("uses"), "group_id": Id("groups", nullable=True),
                                "needed_form": Obj(STATEMENT_FIELDS, nullable=True), "substitutions": Arr(SUBSTITUTION),
                                "scope_id": Id("scopes", nullable=True), "state": Enum(("draft", "registered"))},
                               local=_application_local, optional=("scope_id",)),
    "target_specs": Obj({"target": TARGET, "statement_ref": RefT(("items", "parts"), pinned=True, nullable=True),
                          "statement": Obj(STATEMENT_FIELDS, nullable=True), "scope_id": Id("scopes", nullable=True),
                          "evidence_refs": Arr(Id("anchors")), "state": Enum(("draft", "registered")),
                          "fidelity_ref": RefT(("observations",), pinned=True, nullable=True)}, local=_target_spec_local),
    "overview_selections": Obj({"paper_id": Id("papers"), "title": Str(), "scope": Str(nullable=True),
                                "item_ids": Arr(Id("items")), "use_ids": Arr(Id("uses")),
                                "main_item_ids": Arr(Id("items")), "source_ids": Arr(Id("sources")),
                                "authoring_profile": Str(nullable=True), "roots": Arr(Str()), "unresolved": Arr(Str()),
                                "native_context": Any()}, optional=("native_context",)),
    "connection_refinements": Obj({"summary_use_id": Id("uses"), "argument_id": Id("arguments"),
                                    "use_ids": Arr(Id("uses"), nonempty=True), "state": Enum(("draft", "registered")),
                                    "note": Str()}),
    "proof_boundaries": Obj({"target": TARGET, "argument_ids": Arr(Id("arguments"), nonempty=True),
                              "anchor_refs": Arr(RefT(("anchors",), pinned=True), nonempty=True),
                              "source_review_ref": RefT(("source_reviews",), pinned=True),
                              "state": Enum(("complete", "unresolved"))}),
    "coverage": Obj({"argument_id": Id("arguments"), "anchor_id": Id("anchors"), "start_offset": Int(0),
                     "end_offset": Int(0), "classification": Enum(("substantive", "structural")),
                     "claim_refs": Arr(TARGET), "check_ids": Arr(Id("checks")), "note": Str()},
                    local=_coverage_local),
    "audits": Obj({"paper_id": Id("papers"), "mode": Enum(("triage", "focused", "full")), "targets": Arr(TARGET),
                   "exclusions": Arr(EXCLUSION), "protocol_version": Str(nonempty=True),
                   "independent_required": Bool(), "qualification_id": Id("qualifications", nullable=True),
                   "global_tasks": Arr(GLOBAL_TASK), "report_path": Str()}, local=_audits_local),
    "checks": Obj({"audit_id": Id("audits"), "target": REF, "kind": Enum(CHECK_KINDS),
                   "role": Enum(("primary", "independent")), "reviewer": Str(nonempty=True),
                   "protocol_version": Str(nonempty=True), "state": Enum(("draft", "complete")),
                   "outcome": Enum(OUTCOMES, nullable=True),
                   "reasoning": Str(), "evidence_refs": Arr(Id("anchors")), "conditions": Arr(Str(nonempty=True)),
                   "next_action": Str(nullable=True), "response_id": Id("responses", nullable=True),
                   "supersedes": RefT(("checks",), pinned=True, nullable=True)}, local=_checks_local),
    "findings": Obj({"audit_id": Id("audits"), "target": REF,
                     "category": Enum(("presentation", "proof_gap", "dependency_mismatch",
                                       "statement_refutation", "inconclusive")),
                     "lifecycle": Enum(("open", "resolved", "superseded")), "description": Str(nonempty=True),
                     "evidence_refs": Arr(Id("anchors")), "check_refs": Arr(RefT(("checks",), pinned=True)),
                     "affected_uses": Arr(Id("uses")), "impact_reason": Str(nonempty=True),
                     "resolution": Str(nullable=True)}, local=_findings_local),
    "repairs": Obj({"finding_id": Id("findings"),
                    "kind": Enum(("supplemental_argument", "restricted_statement", "source_revision_proposal")),
                    "description": Str(nonempty=True), "supported_form": RefT(("items", "parts"), nullable=True),
                    "argument_id": Id("arguments", nullable=True), "added_conditions": Arr(Str(nonempty=True)),
                    "evidence_refs": Arr(Id("anchors"))}, local=_repairs_local),
    "qualifications": Obj({"reviewer": Str(nonempty=True), "profile": PROFILE, "protocol_version": Str(nonempty=True),
                           "valid_case_results": Arr(CALIBRATION), "invalid_case_results": Arr(CALIBRATION),
                           "evidence_blob": Hash(), "qualified": Bool(), "limitations": Arr(Str(nonempty=True))},
                          local=_qualifications_local),
    "responses": Obj({"audit_id": Id("audits"), "packet_id": Str(nonempty=True), "reviewer": Str(nonempty=True),
                      "qualification_id": Id("qualifications"), "original_blob": Hash(),
                      "covered_targets": Arr(TARGET), "coverage_note": Str(),
                      "exposure": Enum(("source_only", "route_provided", "compromised")), "exposure_note": Str(),
                      "state": Enum(("accepted", "needs_revision"))}, local=_responses_local),
    "reconciliations": Obj({"audit_id": Id("audits"), "target": REF,
                            "primary_checks": Arr(RefT(("checks",), pinned=True)),
                            "independent_checks": Arr(RefT(("checks",), pinned=True)),
                            "decision": Enum(("agree", "primary_revised", "independent_revised", "unresolved")),
                            "rationale": Str(nonempty=True), "evidence_refs": Arr(Id("anchors")),
                            "successor_checks": Arr(RefT(("checks",), pinned=True)),
                            "supersedes": RefT(("reconciliations",), pinned=True, nullable=True),
                            "adjudicator": Str(nonempty=True)}, local=_reconciliations_local),
    "reuse_decisions": Obj({"check_ref": RefT(("checks",), pinned=True), "source_review_id": Id("source_reviews"),
                            "decision": Enum(("reusable", "recheck")), "rationale": Str(nonempty=True),
                            "evidence_refs": Arr(Id("anchors"))}),
    "identity_maps": Obj({"reason": Enum(("import", "split", "merge", "successor", "response_mapping")),
                          "source_blob": Hash(nullable=True), "response_id": Id("responses", nullable=True),
                          "entries": Arr(MAPPING), "reviewer": Str(nonempty=True), "note": Str()},
                         local=_identity_maps_local),
}
assert set(BODY_SCHEMAS) == set(COLLECTIONS)

# Request envelopes (record-contract 5.1, implementation-handoff 4.2).
JUDGMENT = Obj({"target": OneOf([REF_OBJECT, SOURCE_TARGET]), "kind": Enum(CHECK_KINDS),
                "state": Enum(("draft", "complete")), "outcome": Enum(OUTCOMES, nullable=True),
                "reasoning": Str(), "evidence_refs": Arr(Id("anchors")), "conditions": Arr(Str(nonempty=True)),
                "next_action": Str(nullable=True), "supersedes": RefT(("checks",), pinned=True, nullable=True)})
WORKER_RESPONSE = Obj({"packet_id": Str(nonempty=True), "covered_targets": Arr(TARGET), "coverage_note": Str(),
                       "exposure_report": EXPOSURE_REPORT, "judgments": Arr(JUDGMENT)})
SUBMISSION = Obj({"contract_version": RequestVersion(), "request_id": Str(nonempty=True), "packet_id": Str(nonempty=True),
                  "reviewer": Str(nonempty=True), "qualification_id": Id("qualifications"),
                  "exposure": Enum(("source_only", "route_provided", "compromised")), "exposure_note": Str()})
MAPPING_REQUEST = Obj({"contract_version": RequestVersion(), "request_id": Str(nonempty=True), "packet_id": Str(nonempty=True),
                       "response_id": Id("responses"),
                       "entries": Arr(Obj({"judgment_index": Int(0), "target": REF, "rationale": Str(nonempty=True)})),
                       "reviewer": Str(nonempty=True)})
ANCHOR_REQUEST = Obj({"contract_version": RequestVersion(), "request_id": Str(nonempty=True), "packet_id": Str(nonempty=True),
                      "anchors": Arr(Obj({"id": Id(), "expected_version": Int(1, nullable=True),
                                          "source_id": Id("sources"), "locator": LOCATOR}))})
CONTEXT_EXTENSION = Obj({"targets": Arr(REF), "source_anchor_ids": Arr(Id("anchors")),
                         "source_paths": Arr(Str(nonempty=True)), "reason": Str(nonempty=True)})
EDIT_CREATE = Obj({"op": Const("create"), "collection": Enum(COLLECTIONS), "id": Id(),
                   "expected_version": Null(), "body": Any()})
EDIT_REPLACE = Obj({"op": Const("replace"), "collection": Enum(COLLECTIONS), "id": Id(),
                    "expected_version": Int(1), "body": Any()})
EDIT_RETIRE = Obj({"op": Const("retire"), "collection": Enum(COLLECTIONS), "id": Id(),
                   "expected_version": Int(1), "reason": Str(nonempty=True)})
BATCH = Obj({"contract_version": RequestVersion(), "request_id": Str(nonempty=True), "packet_id": Str(nonempty=True),
             "edits": Arr(OneOf([EDIT_CREATE, EDIT_REPLACE, EDIT_RETIRE]))})

# The controller supplies administrative fields from its immutable assignment.
# Workers author judgments and coverage, never arbitrary graph edits.
WORK_SUBMISSION = Obj({
    "contract_version": RequestVersion(), "request_id": Str(nonempty=True), "packet_id": Str(nonempty=True),
    "rebase_packet_id": Str(nonempty=True, nullable=True), "reviewer": Str(nonempty=True),
    "qualification_id": Id("qualifications", nullable=True),
    "exposure": Enum(("source_only", "route_provided", "compromised"), nullable=True), "exposure_note": Str(),
})
WORK_CHECK = Obj({
    "type": Const("check"), "task_id": Str(nonempty=True), "state": Enum(("draft", "complete")),
    "outcome": Enum(OUTCOMES, nullable=True), "reasoning": Str(), "evidence_refs": Arr(Id("anchors")),
    "conditions": Arr(Str(nonempty=True)), "next_action": Str(nullable=True),
    "replaces": RefT(("checks",), pinned=True, nullable=True),
    "supersedes": RefT(("checks",), pinned=True, nullable=True),
})
WORK_COMPARISON = Obj({"type": Const("source_fidelity"), "task_id": Str(nonempty=True),
                       "result": Enum(("matched", "needs_attention")), "note": Str(),
                       "evidence_refs": Arr(Id("anchors"))})


class _WorkResult(OneOf):
    def _pick(self, value):
        # Select the declared variant before reporting missing/extra fields, so
        # repair diagnostics identify the exact field instead of a union error.
        if isinstance(value, dict):
            for option in self.options:
                if value.get("type") == option.fields["type"].expected:
                    return option
        return None


WORK_COVERAGE = Obj({
    "argument_id": Id("arguments"), "anchor_id": Id("anchors"), "start_offset": Int(0), "end_offset": Int(0),
    "classification": Enum(("substantive", "structural")), "claim_refs": Arr(TARGET),
    "check_task_ids": Arr(Str(nonempty=True)), "existing_check_refs": Arr(RefT(("checks",), pinned=True)),
    "replaces": RefT(("coverage",), pinned=True, nullable=True), "note": Str(),
})
WORK_FINDING = Obj({
    "target": REF, "category": Enum(("presentation", "proof_gap", "dependency_mismatch",
                                    "statement_refutation", "inconclusive")),
    "description": Str(nonempty=True), "evidence_refs": Arr(Id("anchors")),
    "related_task_ids": Arr(Str(nonempty=True)), "existing_check_refs": Arr(RefT(("checks",), pinned=True)),
    "affected_uses": Arr(Id("uses")), "impact_reason": Str(nonempty=True),
})
WORK_PRIMARY_RESPONSE = Obj({"packet_id": Str(nonempty=True),
                             "results": Arr(_WorkResult([WORK_CHECK, WORK_COMPARISON])),
                             "coverage": Arr(WORK_COVERAGE), "findings": Arr(WORK_FINDING)})


def validate_shape(schema, value, context="") -> list:
    errors = []
    schema.validate(value, context, errors)
    return errors


def validate_body(collection: str, body) -> list:
    """Return error strings for a body against its closed schema and local rules."""
    if collection not in BODY_SCHEMAS:
        return [f"unknown collection {collection!r}"]
    if not isinstance(body, dict):
        return ["body must be an object"]
    if collection == 'uses' and any(k in body for k in ('group_id', 'needed_form', 'substitutions')):
        # Historical contract-3 bodies remain readable. Acceptance normalizes
        # incoming bodies, so new versions never store these duplicate fields.
        common = {k: v for k, v in body.items() if k not in ('group_id', 'needed_form', 'substitutions')}
        historical = Obj({'group_id': Id('groups',nullable=True), 'needed_form': Obj(STATEMENT_FIELDS,nullable=True),
                          'substitutions': Arr(SUBSTITUTION)})
        return validate_shape(BODY_SCHEMAS[collection], common) + validate_shape(historical, {k:body[k] for k in
                      ('group_id','needed_form','substitutions') if k in body})
    return validate_shape(BODY_SCHEMAS[collection], body)


def extract_refs(collection: str, body) -> list:
    """Reference occurrences as rows (field_path, target_collection, target_id, target_version)."""
    rows = []
    for path, target, target_id, version in BODY_SCHEMAS[collection].refs(body, ""):
        rows.append({"field_path": path, "target_collection": target, "target_id": target_id,
                     "target_version": version})
    if collection == 'uses' and body.get('group_id'):
        rows.insert(2, {'field_path':'/group_id','target_collection':'groups','target_id':body['group_id'],'target_version':None})
    return rows
