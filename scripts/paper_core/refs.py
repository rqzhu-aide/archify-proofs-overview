"""Reference index rows, facet digests, and membership relations.

Implements record-contract 1.1 and the guard relations of implementation-handoff 4.3.
"""
from __future__ import annotations

from .canonical import digest
from .contract import extract_refs

FACETS = ("statement", "proof", "application", "inference", "scope", "coverage", "source", "full")

# relation name -> (owner collections, body-relative field path, legal key collections or None)
RELATIONS = {
    "incoming_uses": (("uses",), "/to", ("items", "parts")),
    "uses_in_group": (("uses",), "/group_id", ("groups",)),
    "groups_in_argument": (("groups",), "/argument_id", ("arguments",)),
    "arguments_for_target": (("arguments",), "/target", ("items", "parts")),
    "parts_of_item": (("parts",), "/item_id", ("items",)),
    "coverage_in_argument": (("coverage",), "/argument_id", ("arguments",)),
    "scopes_in_argument": (("scopes",), "/argument_id", ("arguments",)),
    "checks_or_findings_for_target": (("checks", "findings"), "/target", None),
}


def _pick(body, *names):
    return {name: body[name] for name in names}


def facet_digests(collection: str, body: dict) -> dict:
    """Facet digests for one record version. Each facet is a function of that version alone."""
    facets = {"full": digest(body)}
    if collection == "items":
        statement = dict(body["statement"], origin=body["origin"], scope_id=body["scope_id"],
                         passages=[p for p in body["passages"] if p["role"] in ("statement", "definition")])
        facets["statement"] = digest(statement)
        facets["proof"] = digest(_pick(body, "passages", "origin"))
    elif collection == "parts":
        statement = dict(body["statement"], origin=body["origin"], scope_id=body["scope_id"], item_id=body["item_id"],
                         passages=[p for p in body["passages"] if p["role"] in ("statement", "definition")])
        facets["statement"] = digest(statement)
        facets["proof"] = digest(_pick(body, "passages", "origin"))
    elif collection == "anchors":
        facets["statement"] = digest({"excerpt": body["excerpt"]})
        facets["proof"] = digest({"excerpt": body["excerpt"]})
        facets["source"] = digest(_pick(body, "source_id", "source_version", "locator", "method", "excerpt_sha256"))
    elif collection == "uses":
        facets["application"] = digest(_pick(body, "from", "to", "type", "needed_form", "substitutions",
                                             "regime", "reason", "evidence_refs", "group_id"))
    elif collection == "groups":
        facets["inference"] = digest(_pick(body, "conclusion", "kind", "scope_id", "case_scope_ids",
                                           "discharges", "rationale", "evidence_refs"))
    elif collection == "scopes":
        facets["scope"] = digest(_pick(body, "argument_id", "parent_id", "assumptions", "binders",
                                       "conditions", "evidence_refs"))
    elif collection == "coverage":
        facets["coverage"] = digest(_pick(body, "anchor_id", "start_offset", "end_offset",
                                          "classification", "claim_refs"))
    elif collection == "arguments":
        facets["proof"] = digest(_pick(body, "target", "origin", "scope_id", "final_group_id", "evidence_refs"))
    elif collection == "sources":
        facets["source"] = digest(_pick(body, "path", "media_type", "blob_sha256"))
    elif collection == "source_reviews":
        facets["source"] = digest(body)
    return facets


def membership_digest(members) -> str:
    """Digest of the sorted (collection, id, version) triples of live member records."""
    triples = sorted((str(c), str(i), int(v)) for c, i, v in members)
    return digest([list(t) for t in triples])


# As in storage: a long target list is split so one statement never approaches SQLite's parameter
# limit. Each chunk is still one index seek per matching row.
_CHUNK = 500


def referrers(conn, owner_collections, field_path: str, targets, *, prefix=False, revision=None):
    """Live head records in ``owner_collections`` whose ``field_path`` points at one of ``targets``.

    ``targets`` is an iterable of ``(collection, id)`` pairs. The reverse lookup runs over
    ``record_refs``, which ``refs_from_field`` indexes, so the work is proportional to the number of
    records that actually refer into ``targets`` rather than to the size of the owning collections
    (implementation-handoff 10). No body is read here; the caller fetches only what matched.

    ``prefix`` matches every element of an array field, so ``/source_refs`` matches ``/source_refs/0``
    and its siblings. It is a range over ``field_path`` rather than a ``LIKE`` so the index still
    serves it: ``0`` is the byte after ``/``, and the column collates as bytes.

    Returns sorted ``(owner_collection, owner_id, owner_version)`` triples. The order is imposed here
    rather than left to the query plan, because two databases holding the same records have to build
    the same packet bytes.
    """
    by_collection = {}
    for collection, id in targets:
        by_collection.setdefault(collection, []).append(id)
    owners = tuple(owner_collections)
    if not by_collection or not owners:
        return []
    owner_marks = ",".join("?" for _ in owners)
    if prefix:
        field_sql = "r.field_path >= ? AND r.field_path < ?"
        field_params = (field_path + "/", field_path + "0")
    else:
        field_sql = "r.field_path = ?"
        field_params = (field_path,)
    found = set()
    for collection in sorted(by_collection):
        ids = sorted(set(by_collection[collection]))
        for start in range(0, len(ids), _CHUNK):
            chunk = ids[start:start + _CHUNK]
            marks = ",".join("?" for _ in chunk)
            if revision is None:
                sql = f"""
                    SELECT DISTINCT r.owner_collection, r.owner_id, r.owner_version
                    FROM record_refs r INDEXED BY refs_from_field
                    JOIN record_heads h ON h.collection = r.owner_collection AND h.id = r.owner_id
                                        AND h.version = r.owner_version
                    JOIN record_versions v ON v.collection = h.collection AND v.id = h.id
                                           AND v.version = h.version
                    WHERE r.owner_collection IN ({owner_marks}) AND {field_sql}
                      AND r.target_collection = ? AND r.target_id IN ({marks}) AND v.retired = 0"""
                params = (*owners, *field_params, collection, *chunk)
            else:
                sql = f"""
                    SELECT DISTINCT r.owner_collection, r.owner_id, r.owner_version
                    FROM record_refs r INDEXED BY refs_from_field
                    JOIN record_versions v ON v.collection = r.owner_collection AND v.id = r.owner_id
                                           AND v.version = r.owner_version
                    WHERE r.owner_collection IN ({owner_marks}) AND {field_sql}
                      AND r.target_collection = ? AND r.target_id IN ({marks})
                      AND v.retired = 0 AND v.revision <= ?
                      AND v.version = (SELECT MAX(w.version) FROM record_versions w
                                       WHERE w.collection = v.collection AND w.id = v.id
                                         AND w.revision <= ?)"""
                params = (*owners, *field_params, collection, *chunk, revision, revision)
            found.update((row[0], row[1], row[2]) for row in conn.execute(sql, params))
    return sorted(found)


def relation_members(conn, relation: str, key: dict, *, revision=None):
    """Live head members of ``relation`` for ``key`` (or the state at ``revision``)."""
    owners, field_path, legal = RELATIONS[relation]
    if legal is not None and key["collection"] not in legal:
        raise ValueError(f"relation {relation} does not accept keys in {key['collection']}")
    return referrers(conn, owners, field_path, [(key["collection"], key["id"])], revision=revision)


def body_members(collection: str, body: dict, relation: str, key: dict) -> bool:
    """Whether a (prospective) body participates in ``relation`` under ``key``."""
    owners, field_path, _ = RELATIONS[relation]
    if collection not in owners:
        return False
    for row in extract_refs(collection, body):
        if row["field_path"] == field_path and row["target_collection"] == key["collection"] \
                and row["target_id"] == key["id"]:
            return True
    return False


__all__ = ["FACETS", "RELATIONS", "extract_refs", "facet_digests", "membership_digest",
           "referrers", "relation_members", "body_members"]
