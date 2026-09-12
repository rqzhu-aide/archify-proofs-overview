"""A local paper database with immutable snapshots and small record edits.

After initialization this database is authoritative. JSON is an explicit export
or proposed edit batch. Storage integrity is not mathematical verification.
"""
from __future__ import annotations

import argparse
import copy
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


def init_database(db_path, dataset_path, extra_files=(), source_root=None):
    """Import schema 1 or 2, retaining supplied observations without certifying them."""
    db_path, dataset_path = Path(db_path).resolve(), Path(dataset_path).resolve()
    manuscript_root = Path(source_root).resolve() if source_root is not None else dataset_path.parent
    data = records.normalize(_read_json(dataset_path), dataset_path.parent, extra_files=extra_files, source_root=manuscript_root)
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
    return {"database": str(db_path), "snapshot_id": snapshot_id, "authority": "sqlite", "items": len(data["items"]), "uses": len(data["uses"])}


def _load_snapshot(connection, snapshot_id=None):
    snapshot_id = snapshot_id or _head(connection)
    row = connection.execute("SELECT payload FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
    if row is None:
        raise DatabaseError(f"Unknown snapshot {snapshot_id!r}; select a retained snapshot.")
    data = json.loads(row[0])
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


def export_snapshot(db_path, snapshot_id=None):
    """Export immutable semantic content with append-only comparison history."""
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN")
        return records.validate_records(_load_snapshot(connection, snapshot_id))
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
        raise DatabaseError(f"Unknown item {item_id!r}.")
    incoming = [use for use in data["uses"] if use["to"] == item_id]
    prerequisite_ids = {use["from"] for use in incoming}
    prerequisites = [item for item in data["items"] if item["id"] in prerequisite_ids]
    items = [by_id[item_id]] + prerequisites
    anchor_ids = {passage["anchor_id"] for item in items for passage in item["passages"]}
    anchor_ids.update(anchor for use in incoming for anchor in use["evidence_refs"])
    anchors = [anchor for anchor in data["anchors"] if anchor["id"] in anchor_ids]
    targets = {("items", item["id"]) for item in items} | {("uses", use["id"]) for use in incoming}
    history = [observation for observation in data.get("observations", []) if (observation["target"]["collection"], observation["target"]["id"]) in targets]
    applicable = [observation for observation in records.applicable_observations(data)
                  if (observation["target"]["collection"], observation["target"]["id"]) in targets]
    return {"expected_snapshot": data["snapshot_id"], "item": by_id[item_id], "incoming_uses": incoming,
            "prerequisite_items": prerequisites, "anchors": anchors,
            "source_revision": {**data["source_revision"], "files": [{key: value for key, value in source.items() if key != "content_base64"} for source in data["source_revision"]["files"]]},
            "target_digests": [{"collection": collection, "id": identifier, "digest": records.target_digest(data, collection, identifier)} for collection, identifier in sorted(targets)],
            "observations": applicable, "observation_history_count": len(history),
            "source_status": records.source_status(data, _source_root(db_path)), "comparison_status": records.comparison_status(data)}


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
            _require_head(connection, expected_snapshot)
            snapshot_id = _store_snapshot(connection, data)
            connection.execute("UPDATE current_snapshot SET snapshot_id=? WHERE singleton=1", (snapshot_id,))
            _store_observations(connection, snapshot_id, data.get("observations", []))
            if source_root is not None:
                connection.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES ('source_root',?)", (str(Path(source_root).resolve()),))
        return {"snapshot_id": snapshot_id, "previous_snapshot": expected_snapshot, "items": len(data["items"]), "uses": len(data["uses"])}
    finally:
        connection.close()


def apply_edits(db_path, patch):
    if not isinstance(patch, dict):
        raise DatabaseError("An edit batch must be an object.")
    data = export_snapshot(db_path)
    if patch.get("expected_snapshot") != data["snapshot_id"]:
        raise DatabaseError(f"Stale edit: current snapshot is {data['snapshot_id']}. Retrieve current records before applying the batch.")
    updated = _edit_data(data, patch)
    return _publish(db_path, updated, data["snapshot_id"])


def compare_records(db_path, batch):
    """Record an actual source comparison; the caller supplies its result."""
    if not isinstance(batch, dict) or set(batch) - {"expected_snapshot", "targets", "reviewer", "note", "result", "reuse_from", "changes_reviewed"}:
        raise DatabaseError("A comparison batch supports expected_snapshot, targets, reviewer, note, result, and optional reuse_from with changes_reviewed.")
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
    observations = records.make_observations(data, batch.get("targets"), batch.get("reviewer"), note=batch.get("note", ""), result=batch.get("result", "matched"))
    connection = _connect(db_path, write=True)
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            _require_head(connection, data["snapshot_id"])
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
        before = records.validate_records(_load_snapshot(connection, since_snapshot))
        after = records.validate_records(_load_snapshot(connection, snapshot_id))
        report = build_changes(before, after)
    finally:
        connection.close()
    report['live_source_status'] = records.source_status(after, _source_root(db_path))
    return report


def validate_database(db_path, snapshot_id=None):
    data = export_snapshot(db_path, snapshot_id)
    prepared = records.prepare_records(data, _source_root(db_path))
    return {"valid": True, "snapshot_id": data["snapshot_id"], "items": len(data["items"]), "uses": len(data["uses"]),
            "warnings": prepared["warnings"], "graph_mode": prepared["graph_mode"],
            "source_status": prepared["build_context"]["source_status"], "source_comparison": prepared["build_context"]["source_comparison"]}


def render_database(db_path, output_path, snapshot_id=None):
    from proof_overview import render_dataset
    data = export_snapshot(db_path, snapshot_id)
    receipt = render_dataset(data, _source_root(db_path), Path(output_path), protected_paths=(Path(db_path),))
    receipt["snapshot_id"] = data["snapshot_id"]
    connection = None
    try:
        connection = _connect(db_path, write=True)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            receipt["current_snapshot"] = _head(connection)
            if receipt["current_snapshot"] != data["snapshot_id"]:
                receipt["snapshot_note"] = "Report represents a retained snapshot; the database has a different current snapshot."
            receipt["build_recorded"] = True
            payload = _json(receipt)
            connection.execute("INSERT OR IGNORE INTO builds(id,snapshot_id,payload,created_at) VALUES (?,?,?,?)", (hashlib.sha256(payload.encode()).hexdigest(), data["snapshot_id"], payload, _now()))
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


def backup_database(db_path, output_path):
    output_path = Path(output_path).resolve()
    if output_path == Path(db_path).resolve() or output_path.exists():
        raise DatabaseError("Choose a new backup path distinct from the existing database and other files.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    source = _connect(db_path)
    destination = None
    created = False
    try:
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
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "get", "apply", "compare", "refresh", "changes", "export", "validate", "render", "backup"):
        command = commands.add_parser(name)
        command.add_argument("database", type=Path)
        if name == "init":
            command.add_argument("dataset", type=Path)
        if name in ("init", "refresh"):
            command.add_argument("--source", type=Path, action="append", default=[], help="Additional appendix or macro source; repeat as needed")
            command.add_argument("--source-root", type=Path, help="Manuscript root, separate from the seed and output folders")
        if name == "get":
            command.add_argument("item")
        if name in ("apply", "compare"):
            command.add_argument("batch", type=Path)
        if name in ("export", "render", "backup"):
            command.add_argument("output", type=Path)
        if name in ("get", "export", "validate", "render", "changes"):
            command.add_argument("--snapshot")
        if name == 'changes':
            command.add_argument('--since', required=True, help='Retained baseline snapshot to compare')
            command.add_argument('--output', type=Path, help='Save the complete diff and return a compact receipt')
        if name == "refresh":
            command.add_argument("--expected-snapshot", required=True)
            command.add_argument("--anchors", type=Path, help="JSON map from stable anchor IDs to corrected locators in the new source")
            command.add_argument('--relocate-exact', action='store_true', help='Relocate shifted line anchors only when their exact excerpt is unique')
            command.add_argument('--file-map', type=Path, help='JSON map of registered file IDs to renamed paths, or null for removed files')
    args = parser.parse_args()
    try:
        if args.command == "init":
            result = init_database(args.database, args.dataset, extra_files=args.source, source_root=args.source_root)
        elif args.command == "get":
            result = get_packet(args.database, args.item, args.snapshot)
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
                          'context_review_required': True, 'note': 'Read the saved source diff before selecting any reuse candidates. No comparison was carried forward by this command.'}
        elif args.command == "export":
            result = _write_export(export_snapshot(args.database, args.snapshot), args.output, args.database)
        elif args.command == "validate":
            result = validate_database(args.database, args.snapshot)
        elif args.command == "render":
            result = render_database(args.database, args.output, args.snapshot)
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


if __name__ == "__main__":
    raise SystemExit(main())
