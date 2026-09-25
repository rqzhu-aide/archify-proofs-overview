"""Founded support over recorded target/scope contexts.

Rules are conjunctive inside an inference and alternative between sufficient
routes. A finite worklist computes the least founded solution before any reader
asks for a colour; neither an ownership link nor a cycle supplies a premise.
"""
from collections import defaultdict, deque


def _ref(record):
    return {"collection": record.collection, "id": record.id}


class SupportClosure:
    def __init__(self, derivation):
        self.d = derivation
        self.snap = derivation.snap
        self.rules = {}
        self.values = {}
        self._scopes = {}

    def ancestry(self, scope_id):
        if scope_id not in self._scopes:
            chain, seen = [], set()
            current = scope_id
            while current is not None and current not in seen:
                seen.add(current)
                scope = self.snap.live("scopes", current)
                if scope is None:
                    # A broken scope must never collapse to the global context.
                    chain.append((current, None))
                    break
                chain.append((current, scope))
                current = scope.body["parent_id"]
            self._scopes[scope_id] = chain
        return self._scopes[scope_id]

    def admissible(self, source_scope, active_scope, discharges=()):
        return self.admissibility_issue(source_scope, active_scope, discharges) is None

    def admissibility_issue(self, source_scope, active_scope, discharges=()):
        """The first rejected scope, using exactly the support reducer's rule."""
        active = {sid for sid, _ in self.ancestry(active_scope)}
        discharged = set(discharges)
        for sid, scope in self.ancestry(source_scope):
            if sid in active or sid in discharged:
                continue
            if scope is None or any(scope.body.get(field) for field in
                                    ("argument_id", "assumptions", "binders", "conditions")):
                return sid
        return None

    def statement_key(self, ref, scope_id):
        return ("statement", ref["collection"], ref["id"], scope_id)

    def _local(self, target, kinds):
        states = [self.d._obligation_supported(target, kind) for kind in kinds]
        if "defect" in states:
            return "unavailable"
        return "available" if all(s == "supported" for s in states) else "conditional"

    def _statement_rules(self, key):
        _, collection, ident, scope_id = key
        ref = {"collection": collection, "id": ident}
        record = self.snap.get(ref)
        if record is None:
            return [("unavailable", ())]
        # Explicit hypothetical premises remain usable even when the assertion
        # is false outside that context. This does not establish the assertion.
        if f"{collection}:{ident}" in self.snap.scope_assumptions(scope_id):
            return [("available", ())]
        if not self.admissible(self.snap.exact_scope(ref), scope_id):
            return [("unavailable", ())]
        if self.d.statement_refuted(ref):
            return [("unavailable", ())]
        kind = self.snap.kind_of(ref)
        if kind in ("assumption", "definition"):
            return [("conditional", ())]
        if not self.d.exact_target_current(ref):
            return [("conditional", ())]
        if kind == "external_result":
            return [(self._local(ref, ["external_source"]), ())]
        rules = []
        arguments = self.snap.member_records("arguments_for_target", ref)
        for argument in arguments:
            if argument.body["lifecycle"] == "registered" and self.admissible(
                    argument.body["scope_id"], scope_id):
                rules.append(("available", (("argument", argument.id),)))
        # An internal equation has its own derivation. Its owner's final
        # composition is not an input, even if the owner has a false conclusion.
        from .contract import INTERMEDIATE_KINDS
        groups = self.snap.groups_for_conclusion(ref) if collection == "parts" or kind in INTERMEDIATE_KINDS else []
        for group in groups:
            argument = self.snap.live("arguments", group.body["argument_id"])
            if argument is None or argument.body["lifecycle"] != "registered":
                continue
            discharges = group.body["discharges"] if self._local(_ref(group), ["scope_discharge"]) == "available" else ()
            if self.admissible(group.body["scope_id"], scope_id, discharges):
                rules.append(("available", (("group", group.id),)))
        if collection == "parts" and not arguments and not groups:
            rules.append(("available", (self.statement_key(
                {"collection": "items", "id": record.body["item_id"]}, scope_id),)))
        return rules or [("conditional", ())]

    def _rules_for(self, key):
        kind = key[0]
        if kind == "statement":
            return self._statement_rules(key)
        record = self.snap.live({"argument": "arguments", "group": "groups", "use": "uses"}[kind], key[1])
        if record is None:
            return [("unavailable", ())]
        body = record.body
        if kind == "argument":
            group = self.snap.live("groups", body["final_group_id"])
            if body["lifecycle"] != "registered" or group is None or group.body["conclusion"] != body["target"]:
                return [("conditional", ())]
            discharges = group.body["discharges"] if self._local(_ref(group), ["scope_discharge"]) == "available" else ()
            if not self.admissible(group.body["scope_id"], body["scope_id"], discharges):
                return [("unavailable", ())]
            return [(self._local(_ref(record), ["composition"]), (("group", group.id),))]
        if kind == "group":
            required = ["derivation"]
            if body["kind"] == "cases":
                required.append("case_coverage")
                # Each branch must leave the inference through explicit,
                # examined discharge, never a union of branch assumptions.
                if not body["case_scope_ids"] or not set(body["case_scope_ids"]) <= set(body["discharges"]):
                    return [("conditional", ())]
            if body["discharges"]:
                required.append("scope_discharge")
            uses = self.snap.member_records("uses_in_group", _ref(record))
            if body["kind"] == "cases":
                used_scopes = {self.snap.application(use).get("scope_id") or body["scope_id"] for use in uses}
                if not set(body["case_scope_ids"]) <= used_scopes:
                    return [("conditional", ())]
            return [(self._local(_ref(record), required), tuple(("use", use.id) for use in uses))]
        app = self.snap.application(record)
        group = self.snap.live("groups", app.get("group_id"))
        if app.get("state") != "registered" or group is None:
            return [("conditional", ())]
        group_scope = group.body["scope_id"]
        active_scope = app.get("scope_id") or group_scope
        if active_scope != group_scope and not (
                group.body["kind"] == "cases" and active_scope in group.body["case_scope_ids"]):
            return [("unavailable", ())]
        return [(self._local(_ref(record), ["application"]),
                 (self.statement_key(body["from"], active_scope),))]

    def solve(self, roots):
        """Materialize only recorded contexts, then propagate founded support."""
        pending = deque(key for key in roots if key not in self.rules)
        new = set()
        while pending:
            key = pending.popleft()
            if key in self.rules:
                continue
            self.snap.visit_relations(1, str(key))
            rules = self._rules_for(key)
            self.rules[key] = rules
            new.add(key)
            pending.extend(dep for _, deps in rules for dep in deps if dep not in self.rules)
        if not new:
            return
        # Recompute the finite closure when adding a previously unseen context.
        # Most callers seed all visible records in one call, so this runs once.
        dependents = defaultdict(set)
        for key, rules in self.rules.items():
            for _, deps in rules:
                for dep in deps:
                    dependents[dep].add(key)
        available = set()
        queue = deque(self.rules)
        queued = set(queue)
        while queue:
            key = queue.popleft()
            queued.discard(key)
            if key in available:
                continue
            if any(local == "available" and all(dep in available for dep in deps)
                   for local, deps in self.rules[key]):
                available.add(key)
                for consumer in dependents[key]:
                    if consumer not in queued:
                        queue.append(consumer)
                        queued.add(consumer)
        # Hard failures propagate separately. An unfounded cycle remains open;
        # a known-false supplier is distinguished from an unanswered supplier.
        unavailable = set()
        queue = deque(key for key in self.rules if key not in available)
        queued = set(queue)
        while queue:
            key = queue.popleft()
            queued.discard(key)
            if key in available or key in unavailable:
                continue
            if all(local == "unavailable" or any(dep in unavailable for dep in deps)
                   for local, deps in self.rules[key]):
                unavailable.add(key)
                for consumer in dependents[key]:
                    if consumer not in queued:
                        queue.append(consumer)
                        queued.add(consumer)
        self.values = {key: "available" if key in available else "unavailable" if key in unavailable
                       else "conditional" for key in self.rules}

    def value(self, key):
        self.solve([key])
        return self.values[key]

    def explain(self, key, *, limit=8):
        """Trace one blocking requirement through the already derived support rules.

        This is a bounded explanation, never a second support evaluator. Alternative
        routes and the original availability remain authoritative in ``values``.
        """
        availability = self.value(key)
        if availability == "available":
            return None
        path, seen, current = [], set(), key
        for _ in range(limit):
            if current in seen:
                return {"availability": availability, "code": "unfounded_cycle", "path": path,
                        "message": "Recorded dependencies form a cycle without an established starting premise."}
            seen.add(current)
            if current[0] == "statement":
                ref = {"collection": current[1], "id": current[2]}
            else:
                ref = {"collection": {"use": "uses", "group": "groups", "argument": "arguments"}[current[0]],
                       "id": current[1]}
            path.append(ref)
            record = self.snap.get(ref)
            detail = {"availability": availability, "target": ref, "path": path}
            if record is None:
                return dict(detail, code="missing_record", message="A required source or proof record is unavailable.")
            if current[0] == "statement":
                source_scope, active_scope = self.snap.exact_scope(ref), current[3]
                blocked = self.admissibility_issue(source_scope, active_scope)
                if blocked is not None:
                    return dict(detail, code="scope_unavailable", source_scope_id=source_scope,
                                active_scope_id=active_scope, blocking_scope_id=blocked,
                                message="The supplier depends on a private scope that is not active or discharged at this use.")
                if self.d.statement_refuted(ref):
                    return dict(detail, code="statement_refuted", message="An open refutation applies to this exact supplier statement.")
                if not self.d.exact_target_current(ref):
                    return dict(detail, code="exact_target_pending", message="The supplier's exact target and source comparison are not current.")
            elif current[0] == "use":
                app = self.snap.application(record)
                group = self.snap.live("groups", app.get("group_id"))
                if app.get("state") != "registered" or group is None:
                    return dict(detail, code="application_unregistered", message="The exact application has not been registered in an inference group.")
                active_scope, group_scope = app.get("scope_id") or group.body["scope_id"], group.body["scope_id"]
                if active_scope != group_scope and not (group.body["kind"] == "cases" and
                                                       active_scope in group.body["case_scope_ids"]):
                    return dict(detail, code="application_scope_mismatch", active_scope_id=active_scope,
                                group_scope_id=group_scope,
                                message="The application's active scope does not match its inference group or a declared case.")
            rules = self.rules[current]
            # Prefer a hard blocking dependency when it explains unavailable support.
            dependencies = [dep for local, deps in rules for dep in deps
                            if self.values[dep] == self.values[current]]
            if dependencies:
                current = dependencies[0]
                continue
            if any(local != "available" for local, _ in rules):
                return dict(detail, code="local_examination_unresolved",
                            message="A required local examination is missing, stale, inconclusive, or records a defect; inspect its exact outcome.")
            dependencies = [dep for _, deps in rules for dep in deps if self.values[dep] != "available"]
            if dependencies:
                current = dependencies[0]
                continue
            return dict(detail, code="establishment_unresolved", message="No current recorded route establishes this statement in the active scope.")
        return {"availability": availability, "code": "explanation_truncated", "path": path,
                "message": "Further blocking dependencies remain; inspect the last displayed requirement."}
