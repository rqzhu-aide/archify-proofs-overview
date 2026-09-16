"""Bounded, derived bookkeeping for coordinator-directed proof checking.

No task is persisted and no function here calls a model or changes the graph.
Scheduling follows accepted references, not an interpretation of mathematics.
"""
from __future__ import annotations

import base64
import json
from collections import defaultdict, deque

from .assessment import (PROOF_KINDS, TraversalLimit, derive_full, key_of,
                         obligation_id, pinned_of, ref_of)
from .canonical import compact_json, digest
from .errors import InvalidRequest


def _identity(prefix, *values):
    return prefix + digest(list(values))


def _focus(value):
    if value is None:
        return None
    if isinstance(value, str) and ":" in value:
        collection, identifier = value.split(":", 1)
        value = {"collection": collection, "id": identifier}
    if not isinstance(value, dict) or set(value) != {"collection", "id"} \
            or not all(isinstance(v, str) and v for v in value.values()):
        raise InvalidRequest("focus must be COLLECTION:ID or a record reference", code="WORK_FOCUS")
    return value


def _components(edges):
    """Iterative Kosaraju; includes cycles internal to a group before contraction."""
    seen, finish = set(), []
    for start in sorted(edges):
        if start in seen:
            continue
        stack = [(start, False)]
        while stack:
            node, exiting = stack.pop()
            if exiting:
                finish.append(node)
                continue
            if node in seen:
                continue
            seen.add(node)
            stack.append((node, True))
            stack.extend((other, False) for other in sorted(edges[node], reverse=True) if other not in seen)
    reverse = defaultdict(set)
    for node, others in edges.items():
        for other in others:
            reverse[other].add(node)
    seen, components = set(), []
    for start in reversed(finish):
        if start in seen:
            continue
        members, pending = [], [start]
        while pending:
            node = pending.pop()
            if node in seen:
                continue
            seen.add(node)
            members.append(node)
            pending.extend(reverse[node] - seen)
        if len(members) > 1 or start in edges[start]:
            components.append(sorted(members))
    return components


def _task_argument(snap, target):
    record = snap.get(target)
    if record is None:
        return None
    if record.collection == "arguments":
        return ref_of(record)
    if record.collection == "groups":
        return {"collection": "arguments", "id": record.body["argument_id"]}
    if record.collection == "uses" and record.body["group_id"] is not None:
        group = snap.live("groups", record.body["group_id"])
        if group:
            return {"collection": "arguments", "id": group.body["argument_id"]}
    return None


def _source_order(snap, target):
    record = snap.get(target)
    if record is None:
        return ("", 0, key_of(target))
    anchors = list(record.body.get("evidence_refs", ()))
    anchors.extend(p["anchor_id"] for p in record.body.get("passages", ()))
    positions = []
    for aid in anchors:
        anchor = snap.live("anchors", aid)
        if anchor:
            source = snap.live("sources", anchor.body["source_id"])
            loc = anchor.body["locator"]
            positions.append(("" if source is None else source.body["path"],
                              loc.get("start_line") or loc.get("page") or 0, key_of(target)))
    return min(positions, default=("", 0, key_of(target)))


def build_work(derivation, assessment, *, focus=None):
    """Reuse an already assessed snapshot, including for projection and status."""
    snap, audit = derivation.snap, derivation.audit
    if audit is None:
        raise InvalidRequest("work requires an audit", code="AUDIT_REQUIRED")
    focus = _focus(focus)
    if focus is not None and snap.get(focus) is None:
        raise InvalidRequest(f"focus {key_of(focus)} is not live", code="WORK_FOCUS")
    tasks, diagnostics, prerequisites = {}, {}, defaultdict(set)
    task_contexts, task_order = defaultdict(set), {}
    draft_index, tasks_by_target, tasks_by_anchor = defaultdict(list), defaultdict(list), defaultdict(list)
    for judgment in assessment["judgments"].values():
        if judgment["state"] == "draft" and not judgment["superseded"]:
            draft_index[(key_of(judgment["target"]), judgment["kind"], judgment["role"])].append(judgment)
    for drafts in draft_index.values():
        drafts.sort(key=lambda j: (j["revision"], j["ref"]["id"]))

    def diagnostic(code, refs, message, related=(), required=True):
        refs = sorted({key_of(r): {"collection": r["collection"], "id": r["id"]}
                       for r in refs}.values(), key=key_of)
        identifier = _identity("act_", audit.id, code, refs)
        if identifier not in diagnostics:
            diagnostics[identifier] = {"id": identifier, "code": code, "target_refs": refs,
                "related_task_ids": [], "message": message, "required": required}
        row = diagnostics[identifier]
        row["related_task_ids"] = sorted(set(row["related_task_ids"]) | set(related))
        return identifier

    for obligation in assessment["obligations"]:
        oid, target = obligation["id"], obligation["target"]
        owner = snap.owner_of(target)
        argument = _task_argument(snap, target)
        drafts = draft_index.get((key_of(target), obligation["kind"], obligation["role"]), ())
        next_action = None
        if drafts:
            draft = snap.get(drafts[-1]["ref"])
            next_action = draft.body["next_action"]
        support = assessment["support"].get(key_of(target))
        if target["collection"] == "uses":
            support = derivation.use_support(snap.get(target))
        elif owner:
            support = assessment["support"].get(key_of(ref_of(owner)))
        tasks[oid] = {"id": oid, "target": target, "owner": ref_of(owner) if owner else None,
            "argument": argument, "kind": obligation["kind"], "role": obligation["role"],
            "action": "compare_source" if obligation["kind"] == "source_fidelity" else
                      "reconcile" if obligation["kind"] == "reconciliation" else "check",
            "required": obligation["required"], "state": "satisfied" if obligation["satisfied"] else "ready",
            "prerequisite_ids": [], "waiting_on": [], "blocker_ids": [],
            "judgment_refs": obligation["check_refs"], "draft_refs": [d["ref"] for d in drafts],
            "next_action": next_action, "outcome": obligation["outcome"], "freshness": obligation["freshness"],
            "dependency_support": support}
        if argument:
            task_contexts[oid].add(key_of(argument))
        tasks_by_target[key_of(target)].append(oid)
        task_order[oid] = _source_order(snap, target)

    def obligation(target, kind, role="primary"):
        oid = obligation_id(audit.id, target, kind, role)
        return oid if oid in tasks and tasks[oid]["required"] else None

    def add_predecessor(oid, predecessor):
        if predecessor is not None:
            snap.visit_relations(1, oid)
            prerequisites[oid].add(predecessor)

    def fidelity(oid, target):
        pred = obligation(target, "source_fidelity")
        add_predecessor(oid, pred)
        if pred and tasks[oid]["argument"]:
            task_contexts[pred].add(key_of(tasks[oid]["argument"]))

    def scope_fidelity(oid, scope_id):
        for assumed in snap.scope_assumptions(scope_id).values():
            fidelity(oid, assumed)

    def establish(oid, statement, argument, assumed):
        if key_of(statement) in assumed:
            return
        kind = snap.kind_of(statement)
        if kind in ("assumption", "definition"):
            return
        if kind == "external_result":
            add_predecessor(oid, obligation(statement, "external_source"))
            return
        local = []
        if statement["collection"] == "parts" or kind == "intermediate_result":
            local = [g for g in snap.groups_for_conclusion(statement)
                     if argument is not None and g.body["argument_id"] == argument["id"]]
        if local:
            for group in local:
                for group_kind in ("derivation", "case_coverage", "scope_discharge"):
                    add_predecessor(oid, obligation(ref_of(group), group_kind))
            return
        arguments = [a for a in snap.member_records("arguments_for_target", statement)
                     if a.body["lifecycle"] == "registered"]
        if not arguments and (statement["collection"] == "parts" or kind == "intermediate_result"):
            groups = [g for g in snap.groups_for_conclusion(statement)
                      if (a := snap.live("arguments", g.body["argument_id"])) is not None
                      and a.body["lifecycle"] == "registered"]
            for group in groups:
                add_predecessor(oid, obligation(ref_of(group), "derivation"))
            if groups:
                return
        if not arguments and statement["collection"] == "parts":
            part = snap.get(statement)
            arguments = [a for a in snap.member_records("arguments_for_target",
                         {"collection": "items", "id": part.body["item_id"]})
                         if a.body["lifecycle"] == "registered"
                         and (argument is None or a.id != argument["id"])]
        if arguments:
            for route in arguments:
                add_predecessor(oid, obligation(ref_of(route), "composition"))
        elif kind in PROOF_KINDS or kind == "intermediate_result":
            action = diagnostic("register_establishment", [statement],
                f"Register the exact establishing route or group for {key_of(statement)}.", [oid])
            tasks[oid]["blocker_ids"].append(action)

    for oid, task in tasks.items():
        target, kind = task["target"], task["kind"]
        record = snap.get(target)
        if record is None:
            task["blocker_ids"].append(diagnostic("missing_record", [target],
                "A required record is unavailable.", [oid]))
            continue
        anchors = list(record.body.get("evidence_refs", ()))
        anchors.extend(p["anchor_id"] for p in record.body.get("passages", ()))
        for aid in anchors:
            tasks_by_anchor[aid].append(oid)
            anchor = snap.live("anchors", aid)
            if anchor is None or snap.live("sources", anchor.body["source_id"]) is None:
                task["blocker_ids"].append(diagnostic("missing_source", [target],
                    f"Required source anchor {aid} or its source is unavailable.", [oid]))
        if task["role"] == "independent":
            qualification = snap.live("qualifications", audit.body["qualification_id"]) \
                if audit.body["qualification_id"] else None
            if qualification is None or not qualification.body["qualified"] \
                    or qualification.body["protocol_version"] != audit.body["protocol_version"]:
                task["blocker_ids"].append(diagnostic("qualification_required", [ref_of(audit)],
                    "Independent review requires a qualification for this protocol.", [oid]))
            continue
        if kind == "application":
            group = snap.live("groups", record.body["group_id"]) if record.body["group_id"] else None
            assumed = snap.scope_assumptions(group.body["scope_id"]) if group else {}
            fidelity(oid, record.body["from"])
            fidelity(oid, record.body["to"])
            for ref in assumed.values():
                fidelity(oid, ref)
            establish(oid, record.body["from"], task["argument"], assumed)
        elif target["collection"] == "groups":
            fidelity(oid, record.body["conclusion"])
            scope_fidelity(oid, record.body["scope_id"])
            for use in snap.member_records("uses_in_group", target):
                add_predecessor(oid, obligation(ref_of(use), "application"))
            for scope_id in record.body["case_scope_ids"] + record.body["discharges"]:
                scope_fidelity(oid, scope_id)
        elif kind == "composition" and target["collection"] == "arguments":
            fidelity(oid, record.body["target"])
            if record.body["target"]["collection"] == "items":
                for part in snap.member_records("parts_of_item", record.body["target"]):
                    fidelity(oid, ref_of(part))
            scope_fidelity(oid, record.body["scope_id"])
            for group in snap.member_records("groups_in_argument", target):
                for group_kind in ("derivation", "case_coverage", "scope_discharge"):
                    add_predecessor(oid, obligation(ref_of(group), group_kind))
        elif kind == "reconciliation":
            keys = set(assessment["route_records"].get(key_of(target), ())) | {key_of(target)}
            for target_key in keys:
                for other_id in tasks_by_target[target_key]:
                    if tasks[other_id]["role"] in ("primary", "independent"):
                        add_predecessor(oid, other_id)
        elif kind == "external_source":
            fidelity(oid, target)
        elif kind == "composition" and target["collection"] in ("items", "parts"):
            task["blocker_ids"].append(diagnostic("register_establishment", [target],
                f"Register a proof route for {key_of(target)} before checking composition.", [oid]))

    for item in getattr(derivation, "scope_diagnostics", ()):
        related = [oid for oid, task in tasks.items() if task["target"] in item["target_refs"]]
        diagnostic(item["code"], item["target_refs"], item["message"], related, item["required"])
    for source_ref in assessment["source_limits"]:
        issue = snap.get(source_ref)
        related = tasks_by_anchor.get(issue.body["anchor_id"], [])
        if issue.body["anchor_id"] is None:
            related = [oid for anchor_id, oids in tasks_by_anchor.items()
                       if (anchor := snap.live("anchors", anchor_id)) is not None
                       and anchor.body["source_id"] == issue.body["source_id"] for oid in oids]
        identifier = diagnostic("source_attention", [source_ref],
            "Resolve the recorded source limitation before assigning affected work.", related)
        for oid in related:
            tasks[oid]["blocker_ids"].append(identifier)

    # A provisional mathematical boundary cannot bypass absent source or
    # structure further down its prerequisite chain.
    successors = defaultdict(set)
    for oid, predecessors in prerequisites.items():
        for predecessor in predecessors:
            successors[predecessor].add(oid)
    blocked = deque(oid for oid, task in tasks.items() if task["blocker_ids"] and task["state"] != "satisfied")
    while blocked:
        predecessor = blocked.popleft()
        for oid in successors[predecessor]:
            snap.visit_relations(1, oid)
            if tasks[oid]["state"] == "satisfied":
                continue
            before = set(tasks[oid]["blocker_ids"])
            after = before | set(tasks[predecessor]["blocker_ids"])
            if after != before:
                tasks[oid]["blocker_ids"] = sorted(after)
                blocked.append(oid)

    active_edges = {oid: {p for p in prerequisites[oid] if tasks[p]["state"] != "satisfied"}
                    for oid in tasks if tasks[oid]["state"] != "satisfied"}
    cycles = _components(active_edges)
    cycle_tasks = set()
    for members in cycles:
        cycle_tasks.update(members)
        diagnostic("ordering_cycle", [tasks[oid]["target"] for oid in members],
            "Recorded examinations form an ordering cycle; refine the graph or explicitly prepare provisional work.",
            members)
    for oid, task in tasks.items():
        task["prerequisite_ids"] = sorted(prerequisites[oid])
        task["waiting_on"] = sorted(p for p in prerequisites[oid] if tasks[p]["state"] != "satisfied")
        if task["state"] != "satisfied":
            task["state"] = "needs_coordinator" if task["blocker_ids"] else \
                            "waiting" if task["waiting_on"] else "ready"

    units, unit_of = {}, {}
    for oid, task in tasks.items():
        record = snap.get(task["target"])
        group_id = record.id if record.collection == "groups" else \
            record.body["group_id"] if record.collection == "uses" else None
        uid = _identity("unit_", audit.id, task["role"], "groups", group_id) if group_id else \
            _identity("unit_", oid)
        unit_of[oid] = uid
        if uid not in units:
            units[uid] = {"id": uid, "kind": "group" if group_id else task["kind"],
                "role": task["role"], "argument": task["argument"], "owner": task["owner"],
                "obligation_ids": [], "pending_obligation_ids": [], "predecessor_unit_ids": [],
                "blocker_ids": [], "state": "ready"}
        units[uid]["obligation_ids"].append(oid)
    for uid, unit in units.items():
        members = unit["obligation_ids"]
        # A stable local topological order, retaining precise cycle diagnostics.
        ordered, remaining = [], set(members)
        while remaining:
            ready = sorted((oid for oid in remaining if not (prerequisites[oid] & remaining)),
                           key=lambda oid: (task_order[oid], oid))
            if not ready:
                ready = sorted(remaining, key=lambda oid: (task_order[oid], oid))
            ordered.extend(ready)
            remaining.difference_update(ready)
        unit["obligation_ids"] = ordered
        unit["pending_obligation_ids"] = [oid for oid in ordered if tasks[oid]["state"] != "satisfied"
                                          and tasks[oid]["required"]]
        unit["predecessor_unit_ids"] = sorted({unit_of[p] for oid in members for p in prerequisites[oid]
                                                if unit_of[p] != uid})
        unit["blocker_ids"] = sorted({b for oid in unit["pending_obligation_ids"] for b in tasks[oid]["blocker_ids"]})
        unit["cyclic"] = bool(set(unit["pending_obligation_ids"]) & cycle_tasks)
        pending_external = any(tasks[p]["state"] != "satisfied" for oid in unit["pending_obligation_ids"]
                               for p in prerequisites[oid] if unit_of[p] != uid)
        unit["state"] = "satisfied" if not unit["pending_obligation_ids"] else \
            "needs_coordinator" if unit["blocker_ids"] else "waiting" if pending_external or unit["cyclic"] else "ready"

    if focus is not None:
        seeds = {oid for oid, task in tasks.items() if task["target"] == focus or task["owner"] == focus
                 or task["argument"] == focus}
        # Source-focused requests can still identify the argument consuming the source task.
        wanted, pending = set(), list(seeds)
        while pending:
            oid = pending.pop()
            if oid in wanted:
                continue
            wanted.add(oid)
            pending.extend(prerequisites[oid])
            pending.extend(units[unit_of[oid]]["obligation_ids"])
        tasks = {oid: task for oid, task in tasks.items() if oid in wanted}
        units = {uid: unit for uid, unit in units.items() if any(oid in wanted for oid in unit["obligation_ids"])}
    actions = sorted(diagnostics.values(), key=lambda row: (row["code"], row["id"]))
    return {"revision": assessment["revision"], "audit_id": audit.id, "analysis_complete": True,
            "focus": focus, "progress": assessment["progress"],
            "tasks": sorted(tasks.values(), key=lambda task: (task_order[task["id"]], task["id"])),
            "units": sorted(units.values(), key=lambda unit: (min(task_order[t] for t in unit["obligation_ids"]), unit["id"])),
            "coordinator_actions": actions[:100], "diagnostic_count": len(actions),
            "diagnostics_truncated": len(actions) > 100,
            "task_contexts": {oid: sorted(contexts) for oid, contexts in task_contexts.items() if oid in tasks}}


def derive_work(db, *, audit_id, revision=None, focus=None, limits=None):
    try:
        derivation, result = derive_full(db, audit_id=audit_id, revision=revision, limits=limits)
        return build_work(derivation, result, focus=focus)
    except TraversalLimit as exc:
        action = {"id": _identity("act_", audit_id, exc.code, exc.bound), "code": exc.code,
                  "target_refs": [], "related_task_ids": [], "message": exc.message, "required": True}
        return {"revision": revision if revision is not None else db.max_revision(), "audit_id": audit_id,
                "analysis_complete": False, "focus": _focus(focus), "progress": {"process_complete": False},
                "tasks": [], "units": [], "coordinator_actions": [action], "diagnostic_count": 1,
                "diagnostics_truncated": False, "task_contexts": {}}


def list_work(db, *, audit_id, focus=None, revision=None, limit=20, cursor=None, limits=None):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise InvalidRequest("worklist limit must be 1..100", code="WORK_LIMIT")
    focus = _focus(focus)
    offset = 0
    if cursor is not None:
        try:
            if not isinstance(cursor, str) or len(cursor) > 4096:
                raise ValueError()
            page = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
            if set(page) != {"revision", "audit_id", "focus", "offset"} or page["audit_id"] != audit_id \
                    or page["focus"] != focus or (revision is not None and revision != page["revision"]) \
                    or type(page["offset"]) is not int or page["offset"] < 0:
                raise ValueError()
            revision, offset = page["revision"], page["offset"]
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise InvalidRequest("work cursor does not match the audit, focus or snapshot", code="WORK_CURSOR") from None
    result = derive_work(db, audit_id=audit_id, revision=revision, focus=focus, limits=limits)
    rows = result["tasks"]
    result["tasks"] = rows[offset:offset + limit]
    page_ids = {r["id"] for r in result["tasks"]}
    result["units"] = [unit for unit in result["units"] if page_ids.intersection(unit["obligation_ids"])]
    result.pop("task_contexts", None)
    result["next_cursor"] = None
    if offset + limit < len(rows):
        result["next_cursor"] = base64.urlsafe_b64encode(compact_json({"revision": result["revision"],
            "audit_id": audit_id, "focus": focus, "offset": offset + limit}).encode()).decode()
    return result


def select_assignment(work, request, limits=None):
    """Choose a ready seed, then grow through compatible local successors.

    Packet construction measures actual deduplicated bytes and may defer a
    suffix. This selector never estimates away an indivisible joint context.
    """
    limits = limits or {}
    mode = request.get("mode", "primary")
    role = {"primary": "primary", "independent": "independent", "reconcile": "coordinator"}.get(mode)
    if role is None:
        raise InvalidRequest("work mode must be primary, independent or reconcile", code="WORK_MODE")
    cap = request.get("max_units", limits.get("max_units", 5))
    if type(cap) is not int or not 1 <= cap <= 10:
        raise InvalidRequest("max_units must be 1..10", code="WORK_LIMIT")
    provisional = request.get("allow_provisional", False)
    if type(provisional) is not bool:
        raise InvalidRequest("allow_provisional must be boolean", code="WORK_REQUEST")
    tasks = {task["id"]: task for task in work["tasks"]}
    units = {unit["id"]: unit for unit in work["units"]}
    requested, excluded = set(request.get("task_ids", ())), set(request.get("exclude_task_ids", ()))
    unknown = (requested | excluded) - tasks.keys()
    if unknown:
        raise InvalidRequest("unknown or out-of-focus task IDs", code="WORK_TASK", records=sorted(unknown))
    if any(tasks[oid]["role"] != role for oid in requested):
        raise InvalidRequest("requested tasks belong to another role", code="WORK_ROLE")
    unit_of = {oid: unit["id"] for unit in units.values() for oid in unit["obligation_ids"]}
    deferred, candidates = [], []
    for unit in work["units"]:
        if unit["role"] != role or not unit["pending_obligation_ids"]:
            continue
        if set(unit["pending_obligation_ids"]) & excluded:
            deferred.append({"unit_id": unit["id"], "reason": "already_assigned"})
        elif unit["blocker_ids"]:
            deferred.append({"unit_id": unit["id"], "reason": "needs_coordinator"})
        else:
            candidates.append(unit)
    base = {"prepared": False, "revision": work["revision"], "audit_id": work["audit_id"], "mode": mode,
            "selected_unit_ids": [], "assigned_task_ids": [], "conditional_on_task_ids": [],
            "deferred": deferred, "coordinator_actions": work["coordinator_actions"],
            "units": [], "tasks": [], "context": {"owner": None, "argument": None}}
    if not work["analysis_complete"]:
        return base
    if requested:
        allowed = {unit_of[oid] for oid in requested}
        pending = list(allowed)
        # Explicitly requested work may recommend prerequisites, never unrelated filler.
        while pending:
            uid = pending.pop()
            for predecessor in units[uid]["predecessor_unit_ids"]:
                if predecessor in units and predecessor not in allowed:
                    allowed.add(predecessor)
                    pending.append(predecessor)
        candidates = [unit for unit in candidates if unit["id"] in allowed]
        if provisional:
            # Explicit provisional work means the requested local implication,
            # with named unfinished predecessors, rather than another argument.
            requested_units = {unit_of[oid] for oid in requested}
            candidates.sort(key=lambda unit: unit["id"] not in requested_units)
    focus = _focus(request.get("focus", work.get("focus")))
    if focus is not None:
        focused = {unit["id"] for unit in candidates if unit["owner"] == focus or unit["argument"] == focus
                   or any(tasks[oid]["target"] == focus for oid in unit["obligation_ids"])}
        pending = list(focused)
        while pending:
            uid = pending.pop()
            for predecessor in units[uid]["predecessor_unit_ids"]:
                if predecessor in units and predecessor not in focused:
                    focused.add(predecessor)
                    pending.append(predecessor)
        candidates = [unit for unit in candidates if unit["id"] in focused]
    selected, selected_ids, conditional = [], set(), set()
    context = None

    def pending_predecessors(unit):
        return {p for oid in unit["pending_obligation_ids"] for p in tasks[oid]["prerequisite_ids"]
                if p in tasks and tasks[p]["state"] != "satisfied" and unit_of[p] != unit["id"]
                and unit_of[p] not in selected_ids}

    def possible_contexts(unit):
        if unit["argument"]:
            return {key_of(unit["argument"])}
        contexts = set()
        for oid in unit["obligation_ids"]:
            contexts.update(work.get("task_contexts", {}).get(oid, ()))
        if contexts:
            return contexts
        return {key_of(unit["owner"])} if unit["owner"] else {unit["id"]}

    while len(selected) < cap:
        choice = None
        for unit in candidates:
            if unit["id"] in selected_ids:
                continue
            if context is not None and context not in possible_contexts(unit):
                continue
            waiting = pending_predecessors(unit)
            if (waiting or unit.get("cyclic")) and not provisional:
                continue
            # Missing required context cannot be waived by calling it provisional.
            if any(tasks[p]["blocker_ids"] for p in waiting):
                continue
            choice = unit
            break
        if choice is None:
            break
        if context is None:
            options = possible_contexts(choice)
            preferred = key_of(focus) if focus else None
            context = preferred if preferred in options else min(options)
        conditional.update(pending_predecessors(choice))
        if choice.get("cyclic") and provisional:
            conditional.update(p for oid in choice["pending_obligation_ids"] for p in tasks[oid]["waiting_on"]
                               if unit_of[p] == choice["id"])
        selected.append(choice)
        selected_ids.add(choice["id"])
    assigned = [oid for unit in selected for oid in unit["pending_obligation_ids"]]
    for unit in candidates:
        if unit["id"] not in selected_ids:
            reason = "unit_limit" if len(selected) >= cap else "different_context" \
                if context is not None and context not in possible_contexts(unit) else "waiting_on_prerequisites"
            deferred.append({"unit_id": unit["id"], "reason": reason})
    if selected:
        context_ref = dict(zip(("collection", "id"), context.split(":", 1))) if ":" in context else None
        argument = context_ref if context_ref and context_ref["collection"] == "arguments" else None
        owner = next((unit["owner"] for unit in selected if unit["argument"] == argument and unit["owner"]),
                     selected[0]["owner"])
        base.update({"prepared": True, "selected_unit_ids": [unit["id"] for unit in selected],
                     "assigned_task_ids": assigned, "conditional_on_task_ids": sorted(conditional),
                     "context": {"owner": owner, "argument": argument}})
    base["units"] = [dict(unit, task_ids=unit["pending_obligation_ids"],
                          prerequisite_unit_ids=unit["predecessor_unit_ids"]) for unit in selected]
    for oid in assigned:
        task = dict(tasks[oid])
        task["prerequisite_judgment_refs"] = [ref for pid in task["prerequisite_ids"]
            for ref in tasks[pid]["judgment_refs"] if tasks[pid]["state"] == "satisfied"]
        base["tasks"].append(task)
    return base


__all__ = ["build_work", "derive_work", "list_work", "select_assignment"]
