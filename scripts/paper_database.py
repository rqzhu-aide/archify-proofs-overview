"""A local paper database with immutable snapshots and small record edits.

After initialization this database is authoritative. JSON is an explicit export
or proposed edit batch. Storage integrity is not mathematical verification.
"""
from __future__ import annotations

import argparse
import copy
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

import paper_records as records


FORMAT = "archify-paper-database-1"
AUDIT_FORMAT = "archify-edge-audit-1"
COLLECTIONS = {"items", "uses", "anchors"}


class DatabaseError(ValueError):
    """An actionable database, patch, or snapshot problem."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DatabaseError(f"Duplicate JSON field {key!r}; retain one intended value.")
        result[key] = value
    return result


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DatabaseError(f"Cannot read JSON {path}: {exc}") from exc


def _normalize_dataset(dataset_path, extra_files, manuscript_root):
    try:
        return records.normalize(_read_json(dataset_path), dataset_path.parent,
                                 extra_files=extra_files, source_root=manuscript_root)
    except records.RecordError as exc:
        if str(exc).startswith("Cannot capture source "):
            raise DatabaseError(f"{exc} Seed file paths resolve from {dataset_path.parent}, "
                                "the JSON directory. --source-root controls stored paths and refresh, "
                                "not seed path resolution. Correct source.file relative to the JSON "
                                "or supply its absolute path.") from exc
        raise


def _connect(path, *, write=False):
    path = Path(path).resolve()
    if not path.is_file():
        raise DatabaseError(f"Paper database does not exist: {path}. Initialize it first.")
    connection = None
    try:
        connection = sqlite3.connect(path.as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True, timeout=10)
        connection.execute("PRAGMA foreign_keys=ON")
        row = connection.execute("SELECT value FROM metadata WHERE key='format'").fetchone()
        if not row or row[0] != FORMAT:
            raise DatabaseError(f"This file is not a supported paper database: {path}.")
        return connection
    except (sqlite3.Error, DatabaseError) as exc:
        if connection is not None:
            connection.close()
        if isinstance(exc, DatabaseError):
            raise
        raise DatabaseError(f"Cannot open paper database {path}: {exc}") from exc


def _head(connection):
    row = connection.execute("SELECT snapshot_id FROM current_snapshot WHERE singleton=1").fetchone()
    if row is None:
        raise DatabaseError("The database has no current snapshot.")
    return row[0]


def _require_head(connection, expected):
    if not isinstance(expected, str) or not expected:
        raise DatabaseError("Provide expected_snapshot from the packet or latest export.")
    actual = _head(connection)
    if expected != actual:
        raise DatabaseError(f"Stale edit: expected snapshot {expected}, current snapshot is {actual}. Retrieve the changed records and revise the batch.")


def _store_snapshot(connection, data):
    snapshot_id = records.snapshot_digest(data)
    payload = copy.deepcopy(data)
    payload.pop("snapshot_id", None)
    payload.pop("observations", None)
    for source in payload["source_revision"]["files"]:
        encoded = source.pop("content_base64")
        connection.execute("INSERT OR IGNORE INTO source_blobs(sha256, content_base64) VALUES (?, ?)", (source["sha256"], encoded))
        existing = connection.execute("SELECT content_base64 FROM source_blobs WHERE sha256=?", (source["sha256"],)).fetchone()[0]
        if existing != encoded:
            raise DatabaseError("A source digest is already associated with different bytes; the batch was not applied.")
    serialized = _json(payload)
    connection.execute("INSERT OR IGNORE INTO snapshots(id, payload, created_at) VALUES (?, ?, ?)", (snapshot_id, serialized, _now()))
    existing = connection.execute("SELECT payload FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()[0]
    if existing != serialized:
        # Capture clocks are provenance, not semantic identity. Keep the first
        # immutable payload when only excluded metadata differs, and verify it.
        _load_snapshot(connection, snapshot_id)
    return snapshot_id


def _store_observations(connection, snapshot_id, observations):
    for observation in observations:
        serialized = _json(observation)
        row = connection.execute("SELECT payload FROM observations WHERE id=?", (observation["id"],)).fetchone()
        if row and row[0] != serialized:
            raise DatabaseError(f"Observation {observation['id']!r} already has different content; retain the historical observation.")
        connection.execute("INSERT OR IGNORE INTO observations(id, snapshot_id, payload) VALUES (?, ?, ?)", (observation["id"], snapshot_id, serialized))


def init_database(db_path, dataset_path, extra_files=(), source_root=None, *, focused=False):
    """Capture a schema-3 seed or import a schema-3 export without creating reviews."""
    db_path, dataset_path = Path(db_path).resolve(), Path(dataset_path).resolve()
    manuscript_root = Path(source_root).resolve() if source_root is not None else dataset_path.parent
    data = _normalize_dataset(dataset_path, extra_files, manuscript_root)
    if focused:
        records.validate_focused_authoring(data)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise DatabaseError(f"Database already exists: {db_path}. Use apply for edits; initialization never replaces a database.") from exc
    os.close(descriptor)
    connection = None
    try:
        connection = sqlite3.connect(db_path)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript("""
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE source_blobs(sha256 TEXT PRIMARY KEY, content_base64 TEXT NOT NULL);
            CREATE TABLE snapshots(id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE current_snapshot(singleton INTEGER PRIMARY KEY CHECK(singleton=1), snapshot_id TEXT NOT NULL REFERENCES snapshots(id));
            CREATE TABLE observations(id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES snapshots(id), payload TEXT NOT NULL);
            CREATE TABLE builds(id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES snapshots(id), payload TEXT NOT NULL, created_at TEXT NOT NULL);
        """)
        with connection:
            connection.executemany("INSERT INTO metadata(key,value) VALUES (?,?)", [("format", FORMAT), ("source_root", str(manuscript_root))])
            if focused:
                connection.execute("INSERT INTO metadata(key,value) VALUES ('authoring_profile','focused')")
            snapshot_id = _store_snapshot(connection, data)
            connection.execute("INSERT INTO current_snapshot(singleton,snapshot_id) VALUES (1,?)", (snapshot_id,))
            _store_observations(connection, snapshot_id, data.get("observations", []))
    except Exception:
        if connection is not None:
            connection.close()
            connection = None
        db_path.unlink(missing_ok=True)
        raise
    finally:
        if connection is not None:
            connection.close()
    return {"database": str(db_path), "snapshot_id": snapshot_id, "authority": "sqlite", "items": len(data["items"]), "uses": len(data["uses"]),
            "authoring_profile": "focused" if focused else "compatibility"}


def _load_snapshot(connection, snapshot_id=None):
    snapshot_id = snapshot_id or _head(connection)
    row = connection.execute("SELECT payload FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
    if row is None:
        raise DatabaseError(f"Unknown snapshot {snapshot_id!r}; select a retained snapshot.")
    data = json.loads(row[0])
    records.require_schema3(data)
    for source in data["source_revision"]["files"]:
        blob = connection.execute("SELECT content_base64 FROM source_blobs WHERE sha256=?", (source["sha256"],)).fetchone()
        if blob is None:
            raise DatabaseError(f"Stored source bytes are missing for {source['path']!r}.")
        source["content_base64"] = blob[0]
    data["observations"] = [json.loads(row[0]) for row in connection.execute("SELECT payload FROM observations ORDER BY rowid")]
    if records.snapshot_digest(data) != snapshot_id:
        raise DatabaseError("Stored snapshot content does not match its digest; the database needs recovery.")
    data["snapshot_id"] = snapshot_id
    return data


def _read_snapshot(connection, snapshot_id=None):
    """Read a digest-verified native schema-3 snapshot."""
    storage_id = snapshot_id or _head(connection)
    data = records.validate_records(_load_snapshot(connection, storage_id))
    _check_profile(connection, data)
    return storage_id, data


def _authoring_profile(connection):
    row = connection.execute("SELECT value FROM metadata WHERE key='authoring_profile'").fetchone()
    if row is None:
        return "compatibility"
    if row[0] != "focused":
        raise DatabaseError(f"Unknown authoring profile {row[0]!r}; the database was not changed.")
    return row[0]


def _check_profile(connection, data):
    profile = _authoring_profile(connection)
    if profile == "focused":
        records.validate_focused_authoring(data)
    return profile


def _database_profile(db_path):
    connection = _connect(db_path)
    try:
        return _authoring_profile(connection)
    finally:
        connection.close()


def _writable_head(connection):
    row = connection.execute("SELECT value FROM metadata WHERE key='format'").fetchone()
    if row is None or row[0] != FORMAT:
        raise DatabaseError("The database format changed. Reopen it with the common-store overview adapter.")
    payload = json.loads(connection.execute("SELECT payload FROM snapshots WHERE id=?", (_head(connection),)).fetchone()[0])
    records.require_schema3(payload)


def _require_schema3(db_path):
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN")
        _writable_head(connection)
    finally:
        connection.close()


def _common_format(db_path):
    """Inspect format without making an unsupported database writable."""
    path = Path(db_path).resolve()
    if not path.is_file():
        return False
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        row = connection.execute("SELECT value FROM metadata WHERE key='storage_format'").fetchone()
        return row is not None


def _common_core():
    # Use the shipped bundle in development too, so installation location cannot
    # change the backend. Rebuild it from shared/paper_core when that source changes.
    scripts = Path(__file__).resolve().parent
    bundle = scripts / "paper_core"
    loaded = sys.modules.get("paper_core")
    if loaded is not None and Path(getattr(loaded, "__file__", "") or ".").resolve() != bundle / "__init__.py":
        raise DatabaseError("A different paper_core is already loaded. Run this skill's paper_database.py "
                            "in a fresh Python process; do not mix skill installations in one process.")
    missing = [name for name in ("__init__.py", "overview.py") if not (bundle / name).is_file()]
    if missing:
        raise DatabaseError(f"The bundled paper_core is incomplete: missing {', '.join(missing)}. "
                            "Reinstall the complete proof-graphify skill; no database was changed.")
    if not sys.path or sys.path[0] != str(scripts):
        if str(scripts) in sys.path:
            sys.path.remove(str(scripts))
        sys.path.insert(0, str(scripts))
    try:
        from paper_core import STORAGE_FORMAT, overview
        from paper_core.storage import Database, initialize, paper_record
    except ImportError as exc:
        raise DatabaseError("Cannot load the bundled paper_core. Reinstall the complete "
                            f"proof-graphify skill; no database was changed. Details: {exc}") from exc
    if STORAGE_FORMAT != 4 or not all(callable(getattr(overview, name, None)) for name in ("project", "update")):
        raise DatabaseError("The bundled paper_core is incompatible with this overview version. "
                            "Reinstall the complete proof-graphify skill; no database was changed.")
    return overview, Database, initialize, paper_record


def _materialize_common(db, destination, *, history=False):
    """Create disposable native snapshots for the existing overview algorithms."""
    overview, _, _, paper_record = _common_core()
    current, selection = overview.project(db)
    root = paper_record(db).body["source_root"]
    connection = sqlite3.connect(destination)
    try:
        connection.executescript("""
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE source_blobs(sha256 TEXT PRIMARY KEY, content_base64 TEXT NOT NULL);
            CREATE TABLE snapshots(id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE current_snapshot(singleton INTEGER PRIMARY KEY, snapshot_id TEXT NOT NULL);
            CREATE TABLE observations(id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE builds(id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
        """)
        connection.executemany("INSERT INTO metadata VALUES (?,?)", [("format", FORMAT), ("source_root", root)])
        profile = selection.body.get("authoring_profile") or "compatibility"
        if profile == "focused":
            connection.execute("INSERT INTO metadata VALUES ('authoring_profile','focused')")
        revisions = range(1, db.max_revision() + 1) if history else [db.max_revision()]
        for revision in revisions:
            if not db.records_at(revision, "overview_selections"):
                continue
            data, _ = overview.project(db, revision)
            snapshot_id = _store_snapshot(connection, data)
            _store_observations(connection, snapshot_id, data.get("observations", []))
        snapshot_id = _store_snapshot(connection, current)
        _store_observations(connection, snapshot_id, current.get("observations", []))
        connection.execute("INSERT INTO current_snapshot VALUES (1,?)", (snapshot_id,))
        connection.commit()
    finally:
        connection.close()
    return current, profile


def _common_dispatch(native, db_path, *args, **kwargs):
    """Run retained overview behavior, then atomically accept shared-field edits."""
    overview, Database, _, _ = _common_core()
    from paper_core.errors import CoreError
    name = native.__name__
    if name in {"render_database", "export_database"}:
        output = args[0] if args else kwargs["output_path"]
        if Path(output).resolve() == Path(db_path).resolve():
            raise DatabaseError("Choose an output path distinct from the authoritative database.")
    if name == "backup_database":
        with Database(db_path) as db:
            receipt = db.backup(args[0] if args else kwargs["output_path"])
            data, _ = overview.project(db)
            return {"backup": receipt.get("path", str(args[0] if args else kwargs["output_path"])),
                    "snapshot_id": records.snapshot_digest(data), "recovery": "complete common SQLite authority"}
    write = name in {"apply_edits", "compare_records", "refresh_database", "render_database"}
    history = name == "changes_database" or name == "compare_records" and bool((args[0] if args else kwargs["batch"]).get("reuse_from"))
    if name in {"export_snapshot", "get_packet", "list_records", "candidates_database", "validate_database", "render_database", "export_database"}:
        import inspect
        history = history or inspect.signature(native).bind(db_path, *args, **kwargs).arguments.get("snapshot_id") is not None
    try:
        with Database(db_path, write=write) as db:
            if write:
                db.begin_immediate()
            else:
                db.conn.execute("BEGIN")
            try:
                with tempfile.TemporaryDirectory(prefix="overview-projection-") as folder:
                    projected = Path(folder) / "projection.db"
                    before, profile = _materialize_common(db, projected, history=history)
                    if name == "refresh_database":
                        import inspect
                        supplied = inspect.signature(native).bind(projected, *args, **kwargs)
                        supplied.apply_defaults()
                        values = supplied.arguments
                        expected = values["expected_snapshot"]
                        if expected != records.snapshot_digest(before):
                            raise DatabaseError("Stale refresh: retrieve the current selected snapshot first.")
                        refreshed = records.refresh_sources(before, _source_root(projected, values["source_root"]),
                            extra_files=values["extra_files"], anchor_locations=values["anchor_locations"],
                            relocate_exact=values["relocate_exact"], file_map=values["file_map"], _common_projection=True)
                        result = _publish(projected, refreshed, expected, source_root=values["source_root"])
                    else:
                        try:
                            result = native(projected, *args, **kwargs)
                        except records.RecordError as exc:
                            if "excerpt differs from the captured line range" in str(exc):
                                raise DatabaseError("Selected source bytes advanced beyond the overview anchors. "
                                    "Refresh their bindings before continuing: refresh " + str(db_path) +
                                    " --expected-snapshot " + records.snapshot_digest(before) +
                                    ". Supply --anchors if the source locations changed.") from exc
                            raise
                    if name in {"apply_edits", "compare_records", "refresh_database"}:
                        after = _native_export_snapshot(projected)
                        source_root = _source_root(projected) if name == "refresh_database" else None
                        receipt = overview.update(db, before, after, profile=profile, source_root=source_root)
                        result["common_revision"] = db.max_revision()
                    elif name == "render_database":
                        payload = _json(result)
                        db.set_metadata("overview_build:" + hashlib.sha256(payload.encode()).hexdigest(), payload)
                    if write:
                        db.commit()
                    else:
                        db.rollback()
                    return result
            except BaseException:
                db.rollback()
                raise
    except CoreError as exc:
        raise DatabaseError(str(exc) + (": " + "; ".join(map(str, exc.records)) if exc.records else "")) from exc


def _adapt_common(native):
    def adapted(db_path, *args, **kwargs):
        if _common_format(db_path):
            return _common_dispatch(native, db_path, *args, **kwargs)
        return native(db_path, *args, **kwargs)
    adapted.__name__ = native.__name__
    adapted.__doc__ = native.__doc__
    return adapted


def _init_common(db_path, dataset_path, extra_files=(), source_root=None, *, focused=False):
    overview, Database, initialize, _ = _common_core()
    from paper_core import CORE_VERSION, STORAGE_FORMAT
    from paper_core.errors import CoreError
    db_path, dataset_path = Path(db_path).resolve(), Path(dataset_path).resolve()
    manuscript_root = Path(source_root).resolve() if source_root is not None else dataset_path.parent
    data = _normalize_dataset(dataset_path, extra_files, manuscript_root)
    if focused:
        records.validate_focused_authoring(data)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    initialized = False
    try:
        initialize(db_path, source_root=manuscript_root, title=data["title"])
        initialized = True
        with Database(db_path, write=True) as db:
            db.begin_immediate()
            try:
                overview.update(db, {"observations": []}, data, profile="focused" if focused else "compatibility")
                db.commit()
            except BaseException:
                db.rollback()
                raise
    except BaseException as exc:
        if initialized:
            db_path.unlink(missing_ok=True)
        if isinstance(exc, CoreError):
            raise DatabaseError(str(exc) + (": " + "; ".join(map(str, exc.records)) if exc.records else "")) from exc
        raise
    return {"database": str(db_path), "snapshot_id": records.snapshot_digest(data), "authority": "sqlite",
            "backend": "paper_core", "storage_format": STORAGE_FORMAT, "core_version": CORE_VERSION,
            "core_path": str(Path(overview.__file__).resolve().parent),
            "items": len(data["items"]), "uses": len(data["uses"]), "authoring_profile": "focused" if focused else "compatibility"}


def export_snapshot(db_path, snapshot_id=None):
    """Export immutable semantic content with append-only comparison history."""
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN")
        return _read_snapshot(connection, snapshot_id)[1]
    finally:
        connection.close()


def _source_root(db_path, override=None):
    if override is not None:
        return Path(override).resolve()
    connection = _connect(db_path)
    try:
        row = connection.execute("SELECT value FROM metadata WHERE key='source_root'").fetchone()
        return Path(row[0]) if row else Path(db_path).resolve().parent
    finally:
        connection.close()


def get_packet(db_path, item_id, snapshot_id=None):
    data = export_snapshot(db_path, snapshot_id)
    by_id = {item["id"]: item for item in data["items"]}
    if item_id not in by_id:
        use = next((row for row in data["uses"] if row["id"] == item_id), None)
        if use:
            raise DatabaseError(
                f"{item_id!r} is a connection, not an item. Its target is {use['to']!r}. "
                f"Retrieve that argument with paper_database.py get <database> {use['to']}" +
                (f" --snapshot {snapshot_id}" if snapshot_id else "") + ".")
        raise DatabaseError(f"Unknown item {item_id!r}. Use list to retrieve the stored item IDs.")
    # Owned intermediate rows and the uses entering them are part of the
    # item's fidelity context; the packet is incomplete for review without
    # them. Direct incoming uses stay distinguishable from owned-step uses.
    selected = records.review_context(data, item_id)
    owned = selected["owned"]
    incoming = [use for use in selected["uses"] if use["to"] == item_id]
    step_uses = [use for use in selected["uses"] if use["to"] != item_id]
    prerequisite_ids = {use["from"] for use in selected["uses"]}
    prerequisites = [item for item in data["items"] if item["id"] in prerequisite_ids]
    items = [by_id[item_id]] + owned + prerequisites
    anchor_ids = {passage["anchor_id"] for item in items for passage in item["passages"]}
    anchor_ids.update(anchor for use in selected["uses"] for anchor in use["evidence_refs"])
    anchors = [anchor for anchor in data["anchors"] if anchor["id"] in anchor_ids]
    targets = {("items", item["id"]) for item in items} | {("uses", use["id"]) for use in selected["uses"]}
    history = [observation for observation in data.get("observations", []) if (observation["target"]["collection"], observation["target"]["id"]) in targets]
    all_applicable, fidelity, digests = records._comparison_state(data, targets)
    applicable = [observation for observation in all_applicable
                  if (observation["target"]["collection"], observation["target"]["id"]) in targets]
    # A batch often shares a long comparison note across many targets. Keep
    # each complete note once in this retrieval, without changing stored
    # observations or losing their separate reviewer, result, and identity.
    comparison_notes, observations = {}, []
    for observation in applicable:
        note = observation["note"]
        note_ref = "note-" + hashlib.sha256(note.encode("utf-8")).hexdigest()
        comparison_notes[note_ref] = note
        observations.append({**{key: value for key, value in observation.items() if key != "note"},
                             "note_ref": note_ref})
    return {"expected_snapshot": data["snapshot_id"], "authoring_profile": _database_profile(db_path),
            "item": by_id[item_id], "owned_items": owned, "incoming_uses": incoming,
            "owned_step_uses": step_uses,
            "prerequisite_items": prerequisites, "anchors": anchors,
            "source_revision": {**data["source_revision"], "files": [{key: value for key, value in source.items() if key != "content_base64"} for source in data["source_revision"]["files"]]},
            "target_digests": [{"collection": collection, "id": identifier, "digest": digests[(collection, identifier)]} for collection, identifier in sorted(targets)],
            "observations": observations, "comparison_notes": comparison_notes, "observation_history_count": len(history),
            "locator_diagnostics": [entry for entry in records.locator_diagnostics(data)
                                    if set(entry['anchor_ids']) & anchor_ids],
            # The selected item's own derived fidelity, distinct from the
            # database-wide comparison aggregate below.
            "target_fidelity": fidelity[("items", item_id)],
            "source_status": records.source_status(data, _source_root(db_path)), "comparison_status": records.comparison_status(data, fidelity)}


def list_records(db_path, collection=None, snapshot_id=None):
    """List selected identities and comparison states without source blobs or prose."""
    if collection not in (None, "items", "uses", "anchors"):
        raise DatabaseError("Choose collection items, uses, or anchors.")
    data = export_snapshot(db_path, snapshot_id)
    labels = {item["id"]: item["label"] for item in data["items"]}
    fidelity = records.fidelity_by_row(data) if collection != "anchors" else {}
    result = {"expected_snapshot": data["snapshot_id"],
              "counts": {kind: len(data[kind]) for kind in ("items", "uses", "anchors")}}
    if collection in (None, "items"):
        main = set(data.get("main_items", []))
        result["items"] = [{"id": item["id"], "label": item["label"], "kind": item["kind"],
                            "main_result": item["id"] in main,
                            "comparison": fidelity[("items", item["id"])],
                            "anchor_ids": list(dict.fromkeys(p["anchor_id"] for p in item["passages"]))}
                           for item in data["items"]]
    if collection in (None, "uses"):
        result["uses"] = [{"id": use["id"], "from": use["from"], "to": use["to"],
                           "from_label": labels[use["from"]], "to_label": labels[use["to"]],
                           "type": use["type"], "comparison": fidelity[("uses", use["id"])],
                           "evidence_refs": use["evidence_refs"]} for use in data["uses"]]
    if collection == "anchors":
        files = {source["id"]: source["path"] for source in data["source_revision"]["files"]}
        result["anchors"] = [{"id": anchor["id"], "file_id": anchor.get("file_id"),
                              "file": files.get(anchor.get("file_id")), "locator": anchor["locator"]}
                             for anchor in data["anchors"]]
    return result


def _edit_data(data, patch):
    if not isinstance(patch, dict) or set(patch) - {"expected_snapshot", "edits", "set"}:
        raise DatabaseError("An edit batch supports expected_snapshot, edits, and optional set metadata.")
    edits, metadata = patch.get("edits", []), patch.get("set", {})
    if not isinstance(edits, list) or not isinstance(metadata, dict) or set(metadata) - {"title", "scope", "main_items", "inventory"}:
        raise DatabaseError("Provide an edits list and optional set object containing title, scope, main_items, or inventory exclusions.")
    if "inventory" in metadata:
        inventory = metadata["inventory"]
        if (not isinstance(inventory, dict) or set(inventory) != {"excluded"}
                or not isinstance(inventory["excluded"], list)
                or any(not isinstance(value, str) or not value.strip() for value in inventory["excluded"])):
            raise DatabaseError("Only inventory.excluded may be authored, as a list of scope explanations; declaration matches are generated.")
    if not edits and not metadata:
        raise DatabaseError("The edit batch is empty.")
    result, touched = copy.deepcopy(data), set()
    for index, edit in enumerate(edits, 1):
        if isinstance(edit, dict) and ('set' in edit or edit.get('op') == 'set'):
            raise DatabaseError(f'Edit {index}: metadata belongs in top-level "set" beside "edits", for example {{"edits": [], "set": {{"scope": "..."}}}}.')
        if not isinstance(edit, dict) or set(edit) - {"collection", "op", "id", "record"}:
            raise DatabaseError(f"Edit {index}: expected collection, op, id, and record for an upsert.")
        collection, operation, identifier = edit.get("collection"), edit.get("op"), edit.get("id")
        if not isinstance(collection, str) or collection not in COLLECTIONS or operation not in ("upsert", "remove") or not isinstance(identifier, str) or not identifier:
            raise DatabaseError(f"Edit {index}: use items, uses, or anchors; op upsert/remove; and a nonempty id.")
        if (collection, identifier) in touched:
            raise DatabaseError(f"Edit {index}: duplicate edit for {collection}/{identifier}; provide its final intended content once.")
        touched.add((collection, identifier))
        rows = result[collection]
        at = next((i for i, row in enumerate(rows) if row["id"] == identifier), None)
        if operation == "remove":
            if "record" in edit or at is None:
                raise DatabaseError(f"Edit {index}: remove needs an existing record and no replacement record.")
            rows.pop(at)
        else:
            record = edit.get("record")
            if not isinstance(record, dict):
                raise DatabaseError(f"Edit {index}: replacement record must be an object.")
            record = copy.deepcopy(record)
            if collection == "anchors" and "locator" in record and not set(record) - {"locator", "file_id"}:
                if record.get("file_id") is not None and not isinstance(record["file_id"], str):
                    raise DatabaseError(f"Edit {index}: file_id must be a source identifier.")
                record = records.make_anchor(result, record["locator"], file_id=record.get("file_id"), identity=identifier)
            record.setdefault("id", identifier)
            if record["id"] != identifier:
                raise DatabaseError(f"Edit {index}: replacement record must have matching id {identifier!r}.")
            if collection == "uses":
                record.setdefault("type", "dependency")
                record.setdefault("evidence_refs", [])
            if at is None:
                rows.append(record)
            else:
                rows[at] = record
    for key, value in metadata.items():
        if key == "main_items" and value is None:
            result.pop(key, None)
        elif key == "inventory":
            result.setdefault("inventory", {})["excluded"] = copy.deepcopy(value["excluded"])
        else:
            result[key] = copy.deepcopy(value)
    result.pop("snapshot_id", None)
    return records.update_inventory(result)


def _publish(db_path, data, expected_snapshot, source_root=None):
    connection = _connect(db_path, write=True)
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            _writable_head(connection)
            _require_head(connection, expected_snapshot)
            profile = _check_profile(connection, data)
            snapshot_id = _store_snapshot(connection, data)
            connection.execute("UPDATE current_snapshot SET snapshot_id=? WHERE singleton=1", (snapshot_id,))
            _store_observations(connection, snapshot_id, data.get("observations", []))
            if source_root is not None:
                connection.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES ('source_root',?)", (str(Path(source_root).resolve()),))
        return {"snapshot_id": snapshot_id, "previous_snapshot": expected_snapshot, "items": len(data["items"]), "uses": len(data["uses"]),
                "authoring_profile": profile}
    finally:
        connection.close()


def apply_edits(db_path, patch):
    if not isinstance(patch, dict):
        raise DatabaseError("An edit batch must be an object.")
    _require_schema3(db_path)
    data = export_snapshot(db_path)
    if patch.get("expected_snapshot") != data["snapshot_id"]:
        raise DatabaseError(f"Stale edit: current snapshot is {data['snapshot_id']}. Retrieve current records before applying the batch.")
    updated = _edit_data(data, patch)
    return _publish(db_path, updated, data["snapshot_id"])


def compare_records(db_path, batch):
    """Record an actual source comparison; the caller supplies its result."""
    if not isinstance(batch, dict) or set(batch) - {"expected_snapshot", "targets", "reviewer", "note", "result", "reuse_from", "changes_reviewed"}:
        raise DatabaseError("A comparison batch supports expected_snapshot, targets, reviewer, note, result, and optional reuse_from with changes_reviewed.")
    _require_schema3(db_path)
    reuse = 'reuse_from' in batch
    if reuse:
        if not isinstance(batch['reuse_from'], str) or not batch['reuse_from'].strip():
            raise DatabaseError('reuse_from must identify a retained baseline snapshot.')
        if batch.get('changes_reviewed') is not True or not isinstance(batch.get('note'), str) or not batch['note'].strip():
            raise DatabaseError('Reuse requires changes_reviewed: true and a note explaining the source/context changes actually reviewed. An unchanged excerpt alone is insufficient.')
        if batch.get('result', 'matched') != 'matched':
            raise DatabaseError('Reuse carries a prior matched comparison. Record needs_attention through a regular comparison batch.')
    elif 'changes_reviewed' in batch:
        raise DatabaseError('changes_reviewed accompanies reuse_from; omit it for a regular comparison.')
    targets = batch.get("targets")
    if not isinstance(targets, list) or not targets:
        raise DatabaseError("Comparison targets must be a nonempty list of item or use references.")
    seen = set()
    for target in targets:
        if (not isinstance(target, dict) or set(target) != {"collection", "id"}
                or not isinstance(target["collection"], str) or target["collection"] not in {"items", "uses"}
                or not isinstance(target["id"], str) or not target["id"]):
            raise DatabaseError("Each comparison target needs collection items/uses and a nonempty id.")
        key = (target["collection"], target["id"])
        if key in seen:
            raise DatabaseError("Comparison targets contain a duplicate; compare each target once per batch.")
        seen.add(key)
    if not isinstance(batch.get("reviewer"), str) or not batch["reviewer"].strip():
        raise DatabaseError("A comparison needs a reviewer name or identifier.")
    if not isinstance(batch.get("note", ""), str):
        raise DatabaseError("A comparison note must be text.")
    if not isinstance(batch.get("result", "matched"), str) or batch.get("result", "matched") not in {"matched", "needs_attention"}:
        raise DatabaseError("Comparison result must be matched or needs_attention.")
    data = export_snapshot(db_path)
    if batch.get("expected_snapshot") != data["snapshot_id"]:
        raise DatabaseError(f"Stale comparison: current snapshot is {data['snapshot_id']}. Compare the current input before recording a result.")
    if batch.get("result", "matched") == "matched":
        conflicts = {anchor_id for entry in records.locator_diagnostics(data)
                     if entry['code'] == 'statement_locator_conflict' for anchor_id in entry['anchor_ids']}
        if conflicts:
            items = {row['id']: row for row in data['items']}
            uses = {row['id']: row for row in data['uses']}
            for target in targets:
                row = (items if target['collection'] == 'items' else uses).get(target['id'])
                if row is None:
                    continue  # The observation builder supplies the missing-ID error.
                if target['collection'] == 'items':
                    bound = {passage['anchor_id'] for passage in row['passages']}
                else:
                    bound = set(row['evidence_refs'])
                    bound.update(p['anchor_id'] for endpoint in ('from', 'to') for p in items[row[endpoint]]['passages'])
                if bound & conflicts:
                    raise DatabaseError(f"Cannot mark {target['collection']}/{target['id']} matched: "
                                        f"statement locator conflicts at {', '.join(sorted(bound & conflicts))}. "
                                        "Correct the label or range, or record needs_attention while unresolved.")
    observations = records._make_observations_validated(data, batch.get("targets"), batch.get("reviewer"), note=batch.get("note", ""), result=batch.get("result", "matched"))
    connection = _connect(db_path, write=True)
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            _require_head(connection, data["snapshot_id"])
            # Observations can change without changing the record snapshot.
            # Check the proposed current comparison state under the write lock.
            if _authoring_profile(connection) == "focused":
                checked = _load_snapshot(connection, data['snapshot_id'])
                checked['observations'].extend(observations)
                records.validate_focused_authoring(checked)
            if reuse:
                # Recheck applicable observations under the integrating lock:
                # another reviewer may have recorded needs_attention without
                # changing the semantic snapshot since the packet was read.
                from paper_revision import build_changes
                current = records.validate_records(_load_snapshot(connection, data['snapshot_id']))
                baseline = records.validate_records(_load_snapshot(connection, batch['reuse_from']))
                report = build_changes(baseline, current)
                eligible = {(target['collection'], target['id']) for target in report['reuse_candidates']}
                prior = {(obs['target']['collection'], obs['target']['id']): obs for obs in records.applicable_observations(baseline)}
                for observation in observations:
                    key = observation['target']['collection'], observation['target']['id']
                    if key not in eligible:
                        raise DatabaseError(f"Cannot reuse {key[0]}/{key[1]}: no applicable prior match, changed/affected evidence, or a current concern. Inspect changes and perform a fresh comparison.")
                    observation['carried_from'] = prior[key]['id']
                checked = copy.deepcopy(current)
                checked['observations'].extend(observations)
                records.validate_records(checked)
            _store_observations(connection, data["snapshot_id"], observations)
    finally:
        connection.close()
    return {"snapshot_id": data["snapshot_id"], "recorded": len(observations), "reused": len(observations) if reuse else 0, "observations": observations}


def refresh_database(db_path, expected_snapshot, source_root=None, extra_files=(), anchor_locations=None, relocate_exact=False, file_map=None):
    _require_schema3(db_path)
    data = export_snapshot(db_path)
    if expected_snapshot != data["snapshot_id"]:
        raise DatabaseError(f"Stale refresh: current snapshot is {data['snapshot_id']}.")
    try:
        updated = records.refresh_sources(data, _source_root(db_path, source_root), extra_files=extra_files,
                                          anchor_locations=anchor_locations, relocate_exact=relocate_exact, file_map=file_map)
    except records.RecordError as exc:
        message = str(exc)
        if "Re-anchor" in message or "source path changed" in message or anchor_locations is not None:
            raise DatabaseError(f"{message} Supply corrected locations with refresh --anchors reanchors.json. The earlier database snapshot was preserved.") from exc
        raise
    return _publish(db_path, updated, expected_snapshot, source_root=source_root)


def changes_database(db_path, since_snapshot, snapshot_id=None):
    """Compare two retained inputs, never a mixture of changing live files."""
    from paper_revision import build_changes
    connection = _connect(db_path)
    try:
        connection.execute('BEGIN')
        after_id, after = _read_snapshot(connection, snapshot_id)
        before = _read_snapshot(connection, since_snapshot)[1]
        report = build_changes(before, after)
        report['from_snapshot'], report['to_snapshot'] = since_snapshot, after_id
    finally:
        connection.close()
    report['live_source_status'] = records.source_status(after, _source_root(db_path))
    report['citation_candidates'] = _candidate_summary(after)
    return report


def _candidate_summary(data):
    return records._citation_summary_validated(data)


def candidates_database(db_path, snapshot_id=None):
    """Citation candidates from one captured snapshot; proposes reviews, records nothing."""
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN")
        storage_id, data = _read_snapshot(connection, snapshot_id)
        profile = _authoring_profile(connection)
    finally:
        connection.close()
    report = records._citation_candidates_validated(data)
    report['authoring_profile'] = profile
    if profile == 'focused':
        report = records._selected_candidate_report(data, report)
    return report


def scaffold_audits(db_path, output_folder):
    """Write one empty edge-audit file per major row; never overwrites or records."""
    if _database_profile(db_path) == 'focused':
        raise DatabaseError('Exhaustive scaffold/reconcile is outside focused overview authoring. Use get and optional candidates to review selected connections.')
    data = export_snapshot(db_path)
    majors = [item["id"] for item in data["items"] if item["kind"] in records.MAJOR_KINDS]
    folder = Path(output_folder).resolve()
    if folder == Path(db_path).resolve():
        raise DatabaseError("Choose an audit folder distinct from the database file.")
    planned = {item_id: folder / f"{item_id}.json" for item_id in majors}
    existing = [path for path in planned.values() if path.exists()]
    if existing:
        raise DatabaseError(f"Audit file already exists: {existing[0]}. Scaffold never overwrites a possibly filled audit; remove it explicitly or choose a fresh folder.")
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for item_id in majors:
        audit = {"audit_format": AUDIT_FORMAT, "target": item_id, "expected_snapshot": data["snapshot_id"],
                 "candidates_considered": [row["id"] for row in data["items"] if row["id"] != item_id],
                 "depends_on": [], "dismissed": [],
                 "note": ("Dispose of every candidate exactly once: depends_on entries take id with optional type, "
                          "regime, reason, and evidence_refs (existing anchor ids), or a contributions list that "
                          "names recorded uses by use_id and proposes new ones; dismissed entries take id with an "
                          "optional note. This file proposes review work; reconcile diffs it against recorded uses "
                          "and never modifies the database."),
                 "packet": get_packet(db_path, item_id)}
        path = planned[item_id]
        descriptor, name = tempfile.mkstemp(prefix=".audit-", suffix=".json", dir=folder)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(audit, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
            Path(name).replace(path)
        finally:
            Path(name).unlink(missing_ok=True)
        written.append(str(path))
    return {"output": str(folder), "snapshot_id": data["snapshot_id"], "audits": written,
            "note": "Scaffold writes audit files only; it records nothing in the database. Fill each file, run reconcile, then record decisions with apply and compare."}


def _reconcile_one(audit, path, data, head, items, anchor_ids):
    context = f"Audit {Path(path).name}"
    if not isinstance(audit, dict) or audit.get("audit_format") != AUDIT_FORMAT:
        raise DatabaseError(f"{context}: expected audit_format {AUDIT_FORMAT!r}; scaffold a fresh audit file.")
    unknown = set(audit) - {"audit_format", "target", "expected_snapshot", "candidates_considered",
                            "depends_on", "dismissed", "note", "packet"}
    if unknown:
        raise DatabaseError(f"{context}: unknown fields {sorted(unknown)}; keep the scaffolded shape.")
    for key in ("target", "expected_snapshot", "candidates_considered", "depends_on", "dismissed"):
        if key not in audit:
            raise DatabaseError(f"{context}: missing {key!r}; keep the scaffolded shape.")
    target = audit["target"]
    if not isinstance(target, str) or target not in items or items[target]["kind"] not in records.MAJOR_KINDS:
        raise DatabaseError(f"{context}: target {target!r} is not a major row in this database.")
    if audit["expected_snapshot"] != head:
        raise DatabaseError(f"{context}: stale audit; expected snapshot {audit['expected_snapshot']}, current snapshot is {head}. Re-scaffold or re-check the audit against current records.")
    candidates = [row_id for row_id in items if row_id != target]
    offered = audit["candidates_considered"]
    if not isinstance(offered, list) or len(offered) != len(candidates) or set(offered) != set(candidates):
        raise DatabaseError(f"{context}: candidates_considered no longer matches the database rows; re-scaffold the audit.")
    uses_by_id = {use["id"]: use for use in data["uses"]}

    def check_use_description(fields, where):
        if "type" in fields and fields["type"] not in records.USE_TYPES:
            raise DatabaseError(f"{where}: unsupported use type {fields['type']!r}.")
        for key in ("regime", "reason"):
            if key in fields and not isinstance(fields[key], str):
                raise DatabaseError(f"{where}: {key} must be text.")
        refs = fields.get("evidence_refs", [])
        if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
            raise DatabaseError(f"{where}: evidence_refs must be a list of anchor ids.")
        missing = [ref for ref in refs if ref not in anchor_ids]
        if missing:
            raise DatabaseError(f"{where}: unknown anchor {missing[0]!r}; anchor the passage in the database first.")

    disposed = {}
    named_uses = {}
    for field in ("depends_on", "dismissed"):
        entries = audit[field]
        if not isinstance(entries, list):
            raise DatabaseError(f"{context}: {field} must be a list.")
        allowed = {"id", "type", "regime", "reason", "evidence_refs", "contributions"} if field == "depends_on" else {"id", "note"}
        for index, entry in enumerate(entries, 1):
            where = f"{context}: {field}/{index}"
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                raise DatabaseError(f"{where}: expected an object with a row id.")
            if set(entry) - allowed:
                raise DatabaseError(f"{where}: unknown fields {sorted(set(entry) - allowed)}.")
            row_id = entry["id"]
            if row_id not in items:
                raise DatabaseError(f"{where}: {row_id!r} is not a row in this database.")
            if row_id == target:
                raise DatabaseError(f"{where}: the target does not audit itself.")
            if row_id in disposed:
                raise DatabaseError(f"{where}: {row_id!r} is already listed under {disposed[row_id]}; dispose of each candidate exactly once.")
            disposed[row_id] = field
            if field == "depends_on":
                if "contributions" in entry:
                    mixed = [key for key in ("type", "regime", "reason", "evidence_refs") if key in entry]
                    if mixed:
                        raise DatabaseError(f"{where}: entry-level {', '.join(mixed)} mixed with contributions; put the description inside each contribution or drop contributions.")
                    contributions = entry["contributions"]
                    if not isinstance(contributions, list) or not contributions:
                        raise DatabaseError(f"{where}: contributions must name at least one contribution.")
                    for position, contribution in enumerate(contributions, 1):
                        cwhere = f"{where}/contributions/{position}"
                        if not isinstance(contribution, dict):
                            raise DatabaseError(f"{cwhere}: expected an object naming a use_id or describing a proposed use.")
                        if set(contribution) - {"use_id", "type", "regime", "reason", "evidence_refs"}:
                            raise DatabaseError(f"{cwhere}: unknown fields {sorted(set(contribution) - {'use_id', 'type', 'regime', 'reason', 'evidence_refs'})}.")
                        if "use_id" in contribution:
                            if set(contribution) != {"use_id"}:
                                raise DatabaseError(f"{cwhere}: a use_id stands alone; move type, regime, reason, or evidence_refs into a separate proposed contribution.")
                            if not isinstance(contribution["use_id"], str):
                                raise DatabaseError(f"{cwhere}: use_id must be text.")
                            use_id = contribution["use_id"]
                            if use_id not in uses_by_id:
                                raise DatabaseError(f"{cwhere}: {use_id!r} is not a recorded use in this database.")
                            if use_id in named_uses:
                                raise DatabaseError(f"{cwhere}: use {use_id!r} is already named under candidate {named_uses[use_id]!r}; name each recorded use once per audit.")
                            use = uses_by_id[use_id]
                            if use["from"] != row_id or use["to"] != target:
                                raise DatabaseError(f"{cwhere}: contribution use {use_id!r} runs {use['from']} → {use['to']}, not {row_id} → {target}; name a use of this pair or propose a new one.")
                            named_uses[use_id] = row_id
                        else:
                            if not set(contribution):
                                raise DatabaseError(f"{cwhere}: empty contribution; name a recorded use_id or describe a proposed use with type, regime, reason, or evidence_refs.")
                            check_use_description(contribution, cwhere)
                else:
                    check_use_description(entry, where)
            elif "note" in entry and not isinstance(entry["note"], str):
                raise DatabaseError(f"{where}: note must be text.")
    missing = [row_id for row_id in candidates if row_id not in disposed]
    if missing:
        raise DatabaseError(f"{context}: candidates not disposed: {', '.join(missing)}. Dispose of every candidate exactly once (|depends_on| + |dismissed| = {len(candidates)}).")
    by_from = {}
    for use in data["uses"]:
        if use["to"] == target:
            by_from.setdefault(use["from"], []).append(use)
    missing_edges, suspect, agreements, refinements, ambiguous = [], [], [], [], []
    for entry in audit["depends_on"]:
        source = entry["id"]
        recorded = by_from.get(source, [])
        if "contributions" in entry:
            named_ids = [contribution["use_id"] for contribution in entry["contributions"] if "use_id" in contribution]
            if named_ids:
                agreements.append({"candidate": source, "matched_uses": named_ids,
                                   "other_recorded_uses": [use["id"] for use in recorded if use["id"] not in named_ids]})
            for contribution in entry["contributions"]:
                if "use_id" not in contribution:
                    missing_edges.append({"candidate": source,
                                          "audit": {key: contribution[key] for key in ("type", "regime", "reason", "evidence_refs") if key in contribution}})
            continue
        if not recorded:
            missing_edges.append({"candidate": source,
                                  "audit": {key: entry[key] for key in ("type", "regime", "reason", "evidence_refs") if key in entry}})
            continue
        want_type, want_regime = entry.get("type", "dependency"), entry.get("regime")
        matched = [use for use in recorded if use["type"] == want_type and use.get("regime") == want_regime]
        if len(matched) == 1:
            agreements.append({"candidate": source, "matched_uses": [matched[0]["id"]],
                               "other_recorded_uses": [use["id"] for use in recorded if use["id"] != matched[0]["id"]]})
        elif matched:
            ambiguous.append({"candidate": source, "audit": {"type": want_type, "regime": want_regime},
                              "matched_uses": [use["id"] for use in matched],
                              "note": "The entry matches more than one recorded use; name each intended use under contributions with its use_id."})
        else:
            refinements.append({"candidate": source, "audit": {"type": want_type, "regime": want_regime},
                                "recorded": [{"id": use["id"], "type": use["type"], "regime": use.get("regime"),
                                              "reason": use["reason"]} for use in recorded]})
    for entry in audit["dismissed"]:
        for use in by_from.get(entry["id"], []):
            suspect.append({"use": use["id"], "from": entry["id"], "to": target,
                            "reason": use["reason"], "dismissal": entry.get("note")})
    # Coverage must not hide behind pair agreement or all-zero counts: every
    # recorded use into the target that is neither established nor suspect is
    # reported as unresolved.
    established = {use_id for agreement in agreements for use_id in agreement["matched_uses"]}
    suspect_ids = {row["use"] for row in suspect}
    unresolved = [{"use": use["id"], "from": use["from"], "to": target, "type": use["type"],
                   "regime": use.get("regime"), "reason": use["reason"]}
                  for use in data["uses"]
                  if use["to"] == target and use["id"] not in established and use["id"] not in suspect_ids]
    return {"file": str(path), "target": target, "missing_edge_candidates": missing_edges,
            "suspect_edges": suspect, "agreements": agreements, "refinements": refinements,
            "ambiguous_entries": ambiguous, "unresolved_uses": unresolved}


def reconcile_audits(db_path, audits):
    """Diff filled edge-audit files against recorded uses; prints rows, writes nothing."""
    if _database_profile(db_path) == 'focused':
        raise DatabaseError('Exhaustive scaffold/reconcile is outside focused overview authoring. Use get and optional candidates to review selected connections.')
    path = Path(audits).resolve()
    if path.is_dir():
        files = sorted(path.glob("*.json"))
        if not files:
            raise DatabaseError(f"No audit JSON files in {path}; scaffold audit files first.")
    elif path.is_file():
        files = [path]
    else:
        raise DatabaseError(f"Audit file or folder does not exist: {path}. Scaffold audit files first.")
    data = export_snapshot(db_path)
    head = data["snapshot_id"]
    items = {row["id"]: row for row in data["items"]}
    anchor_ids = {row["id"] for row in data["anchors"]}
    reports, seen_targets = [], set()
    for file in files:
        report = _reconcile_one(_read_json(file), file, data, head, items, anchor_ids)
        if report["target"] in seen_targets:
            raise DatabaseError(f"Audit {file.name}: duplicate audit for target {report['target']!r}; reconcile each target once.")
        seen_targets.add(report["target"])
        reports.append(report)
    agreed, seen_uses = [], set()
    for report in reports:
        for agreement in report["agreements"]:
            for use_id in agreement["matched_uses"]:
                if use_id not in seen_uses:
                    seen_uses.add(use_id)
                    agreed.append({"collection": "uses", "id": use_id})
    return {"snapshot_id": head, "audits": reports,
            "counts": {"audits": len(reports),
                       "missing_edge_candidates": sum(len(r["missing_edge_candidates"]) for r in reports),
                       "suspect_edges": sum(len(r["suspect_edges"]) for r in reports),
                       "agreements": sum(len(r["agreements"]) for r in reports),
                       "refinements": sum(len(r["refinements"]) for r in reports),
                       "ambiguous_entries": sum(len(r["ambiguous_entries"]) for r in reports),
                       "unresolved_uses": sum(len(r["unresolved_uses"]) for r in reports)},
            "compare_skeleton": {"expected_snapshot": head, "targets": agreed} if agreed else None,
            "unaudited_targets": [row["id"] for row in data["items"]
                                  if row["kind"] in records.MAJOR_KINDS and row["id"] not in seen_targets],
            "note": ("Reconcile never modifies the database; it diffs filled audits against recorded uses. Review the "
                     "cited passages, then record decisions with apply and record comparisons actually performed with "
                     "compare (the skeleton needs reviewer and note). Refinement fires when type or regime differ; "
                     "wording differences in reason stay with the reviewer. Recorded uses with no established or "
                     "suspect disposition stay listed as unresolved_uses per target.")}


def validate_database(db_path, snapshot_id=None):
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN")
        storage_id, data = _read_snapshot(connection, snapshot_id)
        profile = _authoring_profile(connection)
    finally:
        connection.close()
    fidelity = records.fidelity_by_row(data)
    prepared = records.record_report(data, _source_root(db_path), fidelity)
    stale_targets = [{"collection": collection, "id": identifier}
                     for (collection, identifier), status in sorted(fidelity.items()) if status == "stale"]
    return {"valid": True, "snapshot_id": storage_id, "authoring_profile": profile,
            "items": len(data["items"]), "uses": len(data["uses"]), "selected_inventory": prepared["inventory"],
            "warnings": prepared["warnings"], "graph_mode": prepared["graph_mode"], "graph_cycles": prepared["graph_cycles"],
            "locator_diagnostics": prepared['locator_diagnostics'],
            "uses_without_evidence": prepared["uses_without_evidence"],
            "citation_candidates": prepared["build_context"]["citation_candidates"],
            "stale_targets": stale_targets,
            "source_status": prepared["build_context"]["source_status"], "source_comparison": prepared["build_context"]["source_comparison"]}


def render_database(db_path, output_path, snapshot_id=None):
    from proof_overview import _render_validated_dataset
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN")
        storage_id, data = _read_snapshot(connection, snapshot_id)
        profile = _authoring_profile(connection)
    finally:
        connection.close()
    receipt = _render_validated_dataset(data, _source_root(db_path), Path(output_path), protected_paths=(Path(db_path),))
    receipt["snapshot_id"] = storage_id
    receipt["authoring_profile"] = profile
    connection = None
    try:
        connection = _connect(db_path, write=True)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            receipt["current_snapshot"] = _head(connection)
            if receipt["current_snapshot"] != storage_id:
                receipt["snapshot_note"] = "Report represents a retained snapshot; the database has a different current snapshot."
            receipt["build_recorded"] = True
            payload = _json(receipt)
            connection.execute("INSERT OR IGNORE INTO builds(id,snapshot_id,payload,created_at) VALUES (?,?,?,?)", (hashlib.sha256(payload.encode()).hexdigest(), storage_id, payload, _now()))
    except (DatabaseError, sqlite3.Error, OSError) as exc:
        # Artifact checks already passed and publication succeeded. Report a
        # bookkeeping limitation without mislabeling it a rendering failure.
        receipt["build_recorded"] = False
        receipt["build_record_error"] = str(exc)
    finally:
        if connection is not None:
            connection.close()
    return receipt


def _write_export(data, output_path, db_path):
    output_path = Path(output_path).resolve()
    if output_path == Path(db_path).resolve() or output_path.suffix.lower() != ".json":
        raise DatabaseError("Choose a .json export path distinct from the database.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".paper-export-", suffix=".json", dir=output_path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        Path(name).replace(output_path)
    finally:
        Path(name).unlink(missing_ok=True)
    return {"output": str(output_path), "snapshot_id": data.get("snapshot_id"), "authority": "export_snapshot"}


def export_database(db_path, output_path, snapshot_id=None):
    """Write a portable JSON export; the receipt counts exactly what was written.

    Comparison history moves without changing the semantic snapshot, so the
    snapshot id alone cannot show that a late comparison reached the export.
    """
    data = export_snapshot(db_path, snapshot_id)
    written = _write_export(data, output_path, db_path)
    return {**written, "observations": len(data.get("observations", [])),
            "source_comparison": records.comparison_status(data)}


def backup_database(db_path, output_path):
    output_path = Path(output_path).resolve()
    if output_path == Path(db_path).resolve() or output_path.exists():
        raise DatabaseError("Choose a new backup path distinct from the existing database and other files.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    source = _connect(db_path)
    destination = None
    created = False
    try:
        _read_snapshot(source)
        descriptor = os.open(output_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        created = True
        destination = sqlite3.connect(output_path)
        destination.execute("PRAGMA foreign_keys=ON")
        source.backup(destination)
        snapshot_id = _head(destination)
    except Exception:
        if destination is not None:
            destination.close()
            destination = None
        if created:
            output_path.unlink(missing_ok=True)
        raise
    finally:
        if destination is not None:
            destination.close()
        source.close()
    return {"backup": str(output_path), "snapshot_id": snapshot_id}


def main():
    # Redirected JSON must not inherit a Windows code page. Keep this at the
    # CLI boundary so importing the database API leaves caller streams alone.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    help_text = {"list": "List stored item/use IDs and comparison states without full source or history",
                 "apply": 'Apply full-record upserts/removals with expected_snapshot; metadata belongs in top-level set',
                 "compare": "Record one result and note for explicitly named item/use targets"}
    for name in ("init", "get", "list", "apply", "compare", "refresh", "changes", "export", "validate", "render", "backup", "candidates", "scaffold", "reconcile"):
        command = commands.add_parser(name, help=help_text.get(name), description=help_text.get(name))
        command.add_argument("database", type=Path)
        if name == "init":
            command.add_argument("dataset", type=Path, help="Seed/export JSON; source.file paths resolve from this JSON directory")
            command.add_argument("--focused", action="store_true", help="Author selected major statements and sourced connections with explicit main results")
        if name in ("init", "refresh"):
            command.add_argument("--source", type=Path, action="append", default=[], help="Additional appendix or macro source; repeat as needed")
            command.add_argument("--source-root", type=Path, help="Manuscript root for stored paths and refresh; seed paths still resolve from the JSON directory")
        if name == "get":
            command.add_argument("item", help="Stored item ID from list; returns the item and its incoming uses")
        if name == "list":
            command.add_argument("--collection", choices=("items", "uses", "anchors"), help="Default: items and uses; anchors lists locators without excerpts")
        if name == "reconcile":
            command.add_argument("audits", type=Path, help="Filled edge-audit JSON file or the folder scaffold wrote")
        if name in ("apply", "compare"):
            command.add_argument("batch", type=Path, help=('JSON: expected_snapshot, edits [{collection, op, id, record}], optional set {title, scope, main_items}. Item records use passages [{role, anchor_id}]. See references/database.md.' if name == "apply" else "JSON: expected_snapshot, targets, reviewer, result, note. Use list for exact target IDs."))
        if name in ("export", "render", "backup"):
            command.add_argument("output", type=Path)
        if name == "scaffold":
            command.add_argument("--output", type=Path, required=True, help="Folder receiving one edge-audit file per major row; existing files are never overwritten")
        if name in ("get", "list", "export", "validate", "render", "changes", "candidates"):
            command.add_argument("--snapshot")
        if name == "render":
            command.add_argument("--full-diagnostics", action="store_true", help="Print every math diagnostic; default groups large failure lists, with full details retained in the HTML")
        if name == 'changes':
            command.add_argument('--since', required=True, help='Retained baseline snapshot to compare')
            command.add_argument('--output', type=Path, help='Save the complete diff and return a compact receipt')
        if name == 'candidates':
            command.add_argument('--output', type=Path, help='Save the complete candidate report and return a compact receipt')
        if name == "refresh":
            command.add_argument("--expected-snapshot", required=True)
            command.add_argument("--anchors", type=Path, help="JSON map from stable anchor IDs to corrected locators in the new source")
            command.add_argument('--relocate-exact', action='store_true', help='Relocate shifted line anchors only when their exact excerpt is unique')
            command.add_argument('--file-map', type=Path, help='JSON map of registered file IDs to renamed paths, or null for removed files')
    args = parser.parse_args()
    try:
        if args.command == "init":
            result = init_database(args.database, args.dataset, extra_files=args.source, source_root=args.source_root, focused=args.focused)
        elif args.command == "get":
            result = get_packet(args.database, args.item, args.snapshot)
        elif args.command == "list":
            result = list_records(args.database, args.collection, args.snapshot)
        elif args.command == "apply":
            result = apply_edits(args.database, _read_json(args.batch))
        elif args.command == "compare":
            result = compare_records(args.database, _read_json(args.batch))
            # Full observations are retained and queryable; do not repeat all
            # generated hashes and the shared note in routine command output.
            result.pop('observations', None)
        elif args.command == "refresh":
            result = refresh_database(args.database, args.expected_snapshot, args.source_root, extra_files=args.source,
                                      anchor_locations=_read_json(args.anchors) if args.anchors else None,
                                      relocate_exact=args.relocate_exact, file_map=_read_json(args.file_map) if args.file_map else None)
        elif args.command == 'changes':
            result = changes_database(args.database, args.since, args.snapshot)
            if args.output:
                output = args.output.resolve()
                current = export_snapshot(args.database, result['to_snapshot'])
                protected = {(_source_root(args.database) / row['path']).resolve() for row in current['source_revision']['files']}
                if output in protected:
                    raise DatabaseError('Choose a changes report path distinct from captured manuscript files.')
                written = _write_export(result, output, args.database)
                result = {'output': written['output'], 'from_snapshot': result['from_snapshot'], 'to_snapshot': result['to_snapshot'],
                          'counts': result['counts'], 'live_source_status': result['live_source_status'],
                          'citation_candidates': result['citation_candidates'],
                          'context_review_required': True, 'note': 'Read the saved source diff before selecting any reuse candidates. No comparison was carried forward by this command.'}
        elif args.command == 'candidates':
            result = candidates_database(args.database, args.snapshot)
            if args.output:
                written = _write_export(result, args.output, args.database)
                result = {'output': written['output'], 'snapshot_id': result['snapshot_id'], 'counts': result['counts'],
                          'note': result['note']}
        elif args.command == 'scaffold':
            result = scaffold_audits(args.database, args.output)
        elif args.command == 'reconcile':
            result = reconcile_audits(args.database, args.audits)
        elif args.command == "export":
            result = export_database(args.database, args.output, args.snapshot)
        elif args.command == "validate":
            result = validate_database(args.database, args.snapshot)
        elif args.command == "render":
            result = render_database(args.database, args.output, args.snapshot)
            from proof_overview import compact_render_receipt
            result = compact_render_receipt(result, full=args.full_diagnostics)
        else:
            result = backup_database(args.database, args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except TypeError as exc:
        print(json.dumps({"error": f"Invalid record field type: {exc}. Check the dataset or edit batch."}, ensure_ascii=False), file=sys.stderr)
        return 2


_native_init_database = init_database
_native_export_snapshot = export_snapshot
for _operation in ("export_snapshot", "get_packet", "list_records", "apply_edits", "compare_records", "refresh_database",
                   "changes_database", "candidates_database", "scaffold_audits", "reconcile_audits",
                   "validate_database", "render_database", "export_database", "backup_database"):
    globals()[_operation] = _adapt_common(globals()[_operation])
init_database = _init_common


if __name__ == "__main__":
    raise SystemExit(main())
