"""SQLite storage and explicit native format migration.

One database file is the single authority. Every connection enables foreign
keys, checks storage/feature compatibility, and uses explicit transactions.
"""
from __future__ import annotations

import datetime
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import (CONTRACT_VERSION, CORE_VERSION, LEGACY_OVERVIEW_FORMAT, PACKET_VERSION,
               PROJECTION_VERSION, PROTOCOL_VERSION, STORAGE_FORMAT, STORAGE_FORMATS_READABLE,
               STORAGE_FORMATS_WRITABLE, SUPPORTED_FEATURES)
from .canonical import canonical_bytes, digest, sha256_bytes
from .errors import ConflictError, IncompatibleError, InvalidRequest, SourceUnavailable
from .ids import new_id

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
INIT_METADATA = {
    "features": json.dumps(list(SUPPORTED_FEATURES)),
    "core_version": CORE_VERSION,
    "projection_version": str(PROJECTION_VERSION),
    "packet_version": str(PACKET_VERSION),
    "protocol_version": PROTOCOL_VERSION,
}


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def body_text(body) -> str:
    return canonical_bytes(body).decode("utf-8")


@dataclass(frozen=True)
class Record:
    collection: str
    id: str
    version: int
    revision: int | None
    retired: bool
    body: dict | None

    @property
    def ref(self):
        return {"collection": self.collection, "id": self.id}

    @property
    def pinned(self):
        return {"collection": self.collection, "id": self.id, "version": self.version}

    @property
    def key(self):
        return (self.collection, self.id)


# SQLite allows far more host parameters than this; a long id list is split so no single statement
# can approach the limit, and each batch is still one index-driven read.
_BATCH = 500


def _row_record(row) -> Record:
    body = None if row["body_json"] is None else json.loads(row["body_json"])
    return Record(row["collection"], row["id"], row["version"], row["revision"], bool(row["retired"]), body)


def _open(path: Path, *, write: bool) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + ("?mode=rw" if write else "?mode=ro")
    conn = sqlite3.connect(uri, uri=True, timeout=10, autocommit=True)
    conn.row_factory = sqlite3.Row
    conn.create_function('proofcheck_writer_format', 0, lambda: STORAGE_FORMAT)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _validate_metadata(meta: dict, path, *, write: bool):
    if meta.get("format") == LEGACY_OVERVIEW_FORMAT:
        raise IncompatibleError(
            f"{path} uses the overview database format {LEGACY_OVERVIEW_FORMAT}; "
            "run migrate-overview to upgrade it",
            records=[{"format": meta.get("format"), "schema_version": meta.get("schema_version")}])
    fmt = meta.get("storage_format")
    if fmt is None or not fmt.isdigit() or int(fmt) not in STORAGE_FORMATS_READABLE:
        raise IncompatibleError(
            f"unsupported storage_format {fmt!r}; this core reads {list(STORAGE_FORMATS_READABLE)}")
    if write and int(fmt) not in STORAGE_FORMATS_WRITABLE:
        raise IncompatibleError(
            f"storage_format {fmt} is read-only in this core; run migrate DB --backup BACKUP.db",
            code="MIGRATION_REQUIRED")
    expected_contract = str(CONTRACT_VERSION) if int(fmt) == STORAGE_FORMAT else '3'
    if meta.get("contract_version") != expected_contract:
        raise IncompatibleError(f"unsupported contract_version {meta.get('contract_version')!r}; "
                                f"this core requires {CONTRACT_VERSION}")
    try:
        features = json.loads(meta.get("features", "[]"))
    except ValueError:
        features = None
    if not isinstance(features, list) or not all(isinstance(value, str) for value in features):
        raise IncompatibleError("metadata features entry is not a JSON list of names")
    unsupported = sorted(set(features) - set(SUPPORTED_FEATURES))
    if unsupported:
        raise IncompatibleError(f"database requires unsupported features {unsupported}")
    return meta


class Database:
    """A supported database opened read-only or read-write, never silently migrated."""

    def __init__(self, path, *, write: bool = False):
        self.path = Path(path)
        if not self.path.is_file():
            raise InvalidRequest(f"database not found: {self.path}")
        self.write = write
        try:
            self.conn = _open(self.path, write=write)
        except sqlite3.Error as exc:
            raise IncompatibleError(f"cannot open database {self.path}: {exc}") from exc
        try:
            self.metadata = self.check_compatibility()
        except Exception:
            self.conn.close()
            raise

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- compatibility ----------------------------------------------------
    def check_compatibility(self) -> dict:
        try:
            rows = self.conn.execute("SELECT key, value FROM metadata").fetchall()
        except sqlite3.DatabaseError as exc:
            raise IncompatibleError(f"{self.path} is not a proofcheck database: {exc}") from exc
        meta = {row["key"]: row["value"] for row in rows}
        return _validate_metadata(meta, self.path, write=self.write)

    # -- transactions -----------------------------------------------------
    def begin_immediate(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            current = self.check_compatibility()
            if current.get('generation') != self.metadata.get('generation'):
                raise IncompatibleError('database generation changed; close and reopen this client')
        except BaseException:
            self.rollback()
            raise

    def commit(self):
        self.conn.execute("COMMIT")

    def rollback(self):
        if self.conn.in_transaction:
            self.conn.execute("ROLLBACK")

    # -- reads ------------------------------------------------------------
    def max_revision(self) -> int:
        return self.conn.execute("SELECT COALESCE(MAX(revision), 0) FROM commits").fetchone()[0]

    def commit_row(self, revision: int):
        row = self.conn.execute("SELECT * FROM commits WHERE revision = ?", (revision,)).fetchone()
        return None if row is None else dict(row)

    def commit_by_request(self, request_id: str):
        row = self.conn.execute("SELECT * FROM commits WHERE request_id = ?", (request_id,)).fetchone()
        return None if row is None else dict(row)

    def head(self, collection: str, id: str) -> Record | None:
        row = self.conn.execute(
            """SELECT v.* FROM record_heads h JOIN record_versions v
               ON v.collection = h.collection AND v.id = h.id AND v.version = h.version
               WHERE h.collection = ? AND h.id = ?""", (collection, id)).fetchone()
        return None if row is None else _row_record(row)

    def version(self, collection: str, id: str, version: int) -> Record | None:
        row = self.conn.execute(
            "SELECT * FROM record_versions WHERE collection = ? AND id = ? AND version = ?",
            (collection, id, version)).fetchone()
        return None if row is None else _row_record(row)

    def latest_at(self, collection: str, id: str, revision: int) -> Record | None:
        row = self.conn.execute(
            """SELECT * FROM record_versions WHERE collection = ? AND id = ? AND revision <= ?
               ORDER BY version DESC LIMIT 1""", (collection, id, revision)).fetchone()
        return None if row is None else _row_record(row)

    def heads(self, collection: str | None = None, *, include_retired: bool = False) -> list:
        sql = """SELECT v.* FROM record_heads h JOIN record_versions v
                 ON v.collection = h.collection AND v.id = h.id AND v.version = h.version"""
        clauses, params = [], []
        if collection is not None:
            clauses.append("v.collection = ?")
            params.append(collection)
        if not include_retired:
            clauses.append("v.retired = 0")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY v.collection, v.id"
        return [_row_record(row) for row in self.conn.execute(sql, params)]

    def heads_in(self, keys, *, include_retired: bool = False) -> list:
        """Live heads for the given ``(collection, id)`` pairs, batched rather than one query each.

        The companion of a reverse lookup through ``record_refs``: the index says which records
        matched, this reads only those bodies. Ordered by collection then id, the same order
        :meth:`heads` returns, so a caller that switches from a scan to a lookup keeps its ordering.
        """
        wanted = {}
        for collection, id in keys:
            wanted.setdefault(collection, set()).add(id)
        out = []
        for collection in sorted(wanted):
            ids = sorted(wanted[collection])
            for start in range(0, len(ids), _BATCH):
                chunk = ids[start:start + _BATCH]
                marks = ",".join("?" for _ in chunk)
                sql = f"""SELECT v.* FROM record_heads h JOIN record_versions v
                          ON v.collection = h.collection AND v.id = h.id AND v.version = h.version
                          WHERE h.collection = ? AND h.id IN ({marks})"""
                if not include_retired:
                    sql += " AND v.retired = 0"
                sql += " ORDER BY v.id"
                out.extend(_row_record(row) for row in self.conn.execute(sql, (collection, *chunk)))
        return out

    def records_at(self, revision: int, collection: str | None = None, *,
                   include_retired: bool = False) -> list:
        sql = """SELECT v.* FROM record_versions v
                 WHERE v.revision <= ? AND v.version = (SELECT MAX(w.version) FROM record_versions w
                       WHERE w.collection = v.collection AND w.id = v.id AND w.revision <= ?)"""
        params = [revision, revision]
        if collection is not None:
            sql += " AND v.collection = ?"
            params.append(collection)
        if not include_retired:
            sql += " AND v.retired = 0"
        sql += " ORDER BY v.collection, v.id"
        return [_row_record(row) for row in self.conn.execute(sql, params)]

    def versions_since(self, revision: int, *, limit: int, offset: int) -> list:
        rows = self.conn.execute(
            """SELECT * FROM record_versions WHERE revision > ?
               ORDER BY revision, collection, id LIMIT ? OFFSET ?""", (revision, limit, offset))
        return [_row_record(row) for row in rows]

    def count_versions_since(self, revision: int) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM record_versions WHERE revision > ?",
                                 (revision,)).fetchone()[0]

    def versions_of(self, collection: str, id: str) -> list:
        rows = self.conn.execute(
            "SELECT * FROM record_versions WHERE collection = ? AND id = ? ORDER BY version",
            (collection, id))
        return [_row_record(row) for row in rows]

    def all_versions(self) -> list:
        rows = self.conn.execute("SELECT * FROM record_versions ORDER BY revision, collection, id, version")
        return [_row_record(row) for row in rows]

    def refs_from(self, collection: str, id: str, version: int) -> list:
        rows = self.conn.execute(
            """SELECT field_path, target_collection, target_id, target_version FROM record_refs
               WHERE owner_collection = ? AND owner_id = ? AND owner_version = ? ORDER BY field_path""",
            (collection, id, version))
        return [dict(row) for row in rows]

    def live_referrers(self, collection: str, id: str) -> list:
        """Live head records that reference (collection, id) at any field."""
        rows = self.conn.execute(
            """SELECT DISTINCT r.owner_collection, r.owner_id, r.owner_version, r.field_path, r.target_version
               FROM record_refs r
               JOIN record_heads h ON h.collection = r.owner_collection AND h.id = r.owner_id
                                   AND h.version = r.owner_version
               JOIN record_versions v ON v.collection = h.collection AND v.id = h.id AND v.version = h.version
               WHERE r.target_collection = ? AND r.target_id = ? AND v.retired = 0
               ORDER BY r.owner_collection, r.owner_id, r.field_path""", (collection, id))
        return [dict(row) for row in rows]

    def facets(self, collection: str, id: str, version: int) -> dict:
        rows = self.conn.execute(
            "SELECT facet, digest FROM record_facets WHERE collection = ? AND id = ? AND version = ?",
            (collection, id, version))
        return {row["facet"]: row["digest"] for row in rows}

    def binding(self, collection: str, id: str, version: int):
        row = self.conn.execute(
            """SELECT packet_id, bindings_json FROM evidence_bindings
               WHERE owner_collection = ? AND owner_id = ? AND owner_version = ?""",
            (collection, id, version)).fetchone()
        if row is None:
            return None
        return {"packet_id": row["packet_id"], "bindings": json.loads(row["bindings_json"])}

    def packet(self, packet_id: str):
        row = self.conn.execute("SELECT * FROM packets WHERE packet_id = ?", (packet_id,)).fetchone()
        if row is None:
            return None
        packet = dict(row)
        packet["manifest"] = json.loads(packet.pop("manifest_json"))
        return packet

    def work_submission(self, request_id: str):
        """Immutable intake and its original outcome; format 2 has no operational index."""
        if int(self.metadata["storage_format"]) < 3:
            return None
        row = self.conn.execute("SELECT * FROM work_submissions WHERE request_id = ?",
                                (request_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        raw = result.pop("result_json")
        result["result"] = None if raw is None else json.loads(raw)
        return result

    def work_submissions(self, *, audit_id=None, packet_id=None, limit=20, after=None) -> list:
        """Bounded metadata-only intake history, ordered by receive time and request ID."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise InvalidRequest("submission history limit must be in 1..100")
        if int(self.metadata["storage_format"]) < 3:
            return []
        clauses, params = [], []
        for field, value in (("audit_id", audit_id), ("packet_id", packet_id)):
            if value is not None:
                clauses.append(f"{field} = ?")
                params.append(value)
        if after is not None:
            if not isinstance(after, (list, tuple)) or len(after) != 2 or not all(isinstance(v, str) for v in after):
                raise InvalidRequest("submission history cursor must be [received_at, request_id]")
            clauses.append("(received_at, request_id) > (?, ?)")
            params.extend(after)
        sql = """SELECT request_id, request_digest, packet_id, audit_id, role, received_at,
                        state, committed_revision FROM work_submissions"""
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY received_at, request_id LIMIT ?"
        return [dict(row) for row in self.conn.execute(sql, (*params, limit))]

    def publications(self, *, revision: int | None = None) -> list:
        if revision is None:
            rows = self.conn.execute("SELECT * FROM publications ORDER BY created_at, id")
        else:
            rows = self.conn.execute("SELECT * FROM publications WHERE revision = ? ORDER BY created_at, id",
                                     (revision,))
        return [dict(row) for row in rows]

    def get_blob(self, sha256: str) -> bytes | None:
        row = self.conn.execute("SELECT content FROM blobs WHERE sha256 = ?", (sha256,)).fetchone()
        return None if row is None else bytes(row["content"])

    def has_blob(self, sha256: str) -> bool:
        return self.conn.execute("SELECT 1 FROM blobs WHERE sha256 = ?", (sha256,)).fetchone() is not None

    def blob_hashes(self) -> list:
        return [row[0] for row in self.conn.execute("SELECT sha256 FROM blobs ORDER BY sha256")]

    # -- writes (call inside a transaction) -------------------------------
    def put_blob(self, data: bytes) -> str:
        sha = sha256_bytes(data)
        self.conn.execute("INSERT OR IGNORE INTO blobs (sha256, content) VALUES (?, ?)", (sha, data))
        return sha

    def insert_commit(self, *, revision: int, parent_revision, base_revision, request_id: str,
                      request_digest: str, receipt: dict):
        self.conn.execute(
            """INSERT INTO commits (revision, parent_revision, base_revision, request_id, request_digest,
                                    receipt_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (revision, parent_revision, base_revision, request_id, request_digest, body_text(receipt), now_iso()))

    def insert_version(self, collection: str, id: str, version: int, revision: int, body):
        retired = body is None
        self.conn.execute(
            """INSERT INTO record_versions (collection, id, version, revision, retired, body_json, body_digest)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (collection, id, version, revision, 1 if retired else 0, None if retired else body_text(body),
             digest(body)))

    def set_head(self, collection: str, id: str, version: int):
        self.conn.execute(
            """INSERT INTO record_heads (collection, id, version) VALUES (?, ?, ?)
               ON CONFLICT(collection, id) DO UPDATE SET version = excluded.version""",
            (collection, id, version))

    def insert_refs(self, collection: str, id: str, version: int, rows: list):
        self.conn.executemany(
            """INSERT INTO record_refs (owner_collection, owner_id, owner_version, field_path,
                                        target_collection, target_id, target_version)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [(collection, id, version, row["field_path"], row["target_collection"], row["target_id"],
              row["target_version"]) for row in rows])

    def insert_facets(self, collection: str, id: str, version: int, facets: dict):
        self.conn.executemany(
            "INSERT INTO record_facets (collection, id, version, facet, digest) VALUES (?, ?, ?, ?, ?)",
            [(collection, id, version, facet, value) for facet, value in sorted(facets.items())])

    def insert_binding(self, collection: str, id: str, version: int, packet_id, bindings: dict):
        self.conn.execute(
            """INSERT INTO evidence_bindings (owner_collection, owner_id, owner_version, packet_id, bindings_json)
               VALUES (?, ?, ?, ?, ?)""", (collection, id, version, packet_id, body_text(bindings)))

    def insert_packet(self, packet_id: str, base_revision: int, mode: str, manifest: dict, payload_sha256: str):
        version = manifest.get("packet_version")
        if isinstance(version, bool) or version not in (1, 2):
            raise InvalidRequest(f"unsupported packet_version {version!r}")
        self.conn.execute(
            """INSERT INTO packets (packet_id, packet_version, base_revision, mode, manifest_json,
                                    payload_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (packet_id, version, base_revision, mode, body_text(manifest), payload_sha256, now_iso()))

    def _require_work_transaction(self):
        if not self.write or not self.conn.in_transaction:
            raise InvalidRequest("submission writes require a caller-owned write transaction")
        if int(self.metadata["storage_format"]) != STORAGE_FORMAT:
            raise IncompatibleError("work submissions require the current storage format", code="MIGRATION_REQUIRED")

    def insert_work_submission(self, *, request_id: str, request_digest: str, packet_id: str,
                               audit_id: str, role: str, envelope_bytes: bytes, response_bytes: bytes):
        """Retain original input without allocating a mathematical revision.

        The coordinator validates packet/provenance before this primitive. Replays preserve
        the first raw envelope bytes even when the canonical envelope has been reformatted.
        """
        self._require_work_transaction()
        old = self.work_submission(request_id)
        if old is not None:
            if old["request_digest"] != request_digest:
                raise InvalidRequest(f"request id {request_id} was already used for different input",
                                     code="REQUEST_ID_REUSED")
            return old
        if self.commit_by_request(request_id) is not None:
            raise InvalidRequest(f"request id {request_id} belongs to an existing mathematical commit",
                                 code="REQUEST_ID_REUSED")
        envelope_sha = self.put_blob(envelope_bytes)
        response_sha = self.put_blob(response_bytes)
        self.conn.execute(
            """INSERT INTO work_submissions
               (request_id, request_digest, packet_id, audit_id, role, envelope_sha256,
                response_sha256, received_at, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'received')""",
            (request_id, request_digest, packet_id, audit_id, role, envelope_sha, response_sha, now_iso()))
        return self.work_submission(request_id)

    def finalize_work_submission(self, request_id: str, *, state: str, result: dict,
                                 committed_revision=None):
        """Finalize once, in the same transaction as any associated mathematical commit."""
        self._require_work_transaction()
        old = self.work_submission(request_id)
        if old is None:
            raise InvalidRequest(f"unknown submission {request_id}")
        if old["state"] != "received":
            return old
        if state not in ("accepted", "needs_revision", "conflict"):
            raise InvalidRequest(f"invalid terminal submission state {state!r}")
        if (state == "accepted" and committed_revision is None) or (state == "conflict" and committed_revision is not None):
            raise InvalidRequest("submission state and mathematical revision disagree")
        commit = self.commit_by_request(request_id)
        if committed_revision is None:
            if commit is not None or result.get("receipt") is not None:
                raise InvalidRequest("a submission without a revision cannot have a mathematical receipt",
                                     code="SUBMISSION_INTEGRITY")
        elif (commit is None or commit["revision"] != committed_revision
              or commit["request_digest"] != old["request_digest"]
              or result.get("receipt") != json.loads(commit["receipt_json"])):
            raise InvalidRequest("submission receipt does not match its mathematical commit",
                                 code="SUBMISSION_INTEGRITY")
        if (result.get("request_id") != request_id or result.get("state") != state
                or result.get("stored") is not True or result.get("committed_revision") != committed_revision):
            raise InvalidRequest("submission result identity or state disagrees with its index",
                                 code="SUBMISSION_INTEGRITY")
        self.conn.execute(
            "UPDATE work_submissions SET state = ?, committed_revision = ?, result_json = ? WHERE request_id = ?",
            (state, committed_revision, body_text(result), request_id))
        return self.work_submission(request_id)

    def insert_publication(self, id: str, revision: int, kind: str, state: str, output_path: str,
                           artifact_sha256, receipt: dict):
        self.conn.execute(
            """INSERT INTO publications (id, revision, kind, state, output_path, artifact_sha256, receipt_json,
                                         created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (id, revision, kind, state, output_path, artifact_sha256, body_text(receipt), now_iso()))

    def insert_run_event(self, id: str, run_id: str, stage: str, started_at: str, elapsed_ms, details: dict):
        self.conn.execute(
            """INSERT INTO run_events (id, run_id, stage, started_at, elapsed_ms, details_json)
               VALUES (?, ?, ?, ?, ?, ?)""", (id, run_id, stage, started_at, elapsed_ms, body_text(details)))

    def set_metadata(self, key: str, value: str):
        self.conn.execute(
            "INSERT INTO metadata (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value))

    # -- maintenance ------------------------------------------------------
    def integrity(self) -> dict:
        violations = [dict(row) for row in self.conn.execute("PRAGMA foreign_key_check")]
        integrity = [row[0] for row in self.conn.execute("PRAGMA integrity_check")]
        return {"foreign_key_violations": violations, "integrity": integrity}

    def backup(self, destination) -> dict:
        dest = Path(destination)
        try:
            fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise InvalidRequest(f"refusing to overwrite existing file {dest}") from None
        os.close(fd)
        try:
            target = sqlite3.connect(str(dest))
            try:
                self.conn.backup(target)
            finally:
                target.close()
        except Exception:
            dest.unlink(missing_ok=True)
            raise
        return {"backup": str(dest), "sha256": sha256_bytes(dest.read_bytes()), "revision": self.max_revision()}


def paper_record(db: Database) -> Record:
    papers = db.heads("papers")
    if len(papers) != 1:
        raise IncompatibleError(f"expected exactly one paper record, found {len(papers)}")
    return papers[0]


def _schema_statements(text: str):
    """Yield complete SQL statements, including trigger bodies, without executescript commits."""
    pending = ""
    for line in text.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending
            pending = ""
    if pending.strip():
        raise InvalidRequest("migration contains incomplete SQL")


def migrate_database(path, *, backup) -> dict:
    """Explicitly upgrade native storage 2 to 3 after an exclusive-destination SQLite backup.

    Mathematical history and packet bytes are unchanged. A same-connection data-version
    check covers operational as well as mathematical concurrent writes around the backup.
    """
    path, destination = Path(path), Path(backup)
    if not path.is_file():
        raise InvalidRequest(f"database not found: {path}")
    conn = _open(path, write=True)
    try:
        try:
            meta = dict(conn.execute("SELECT key, value FROM metadata"))
        except sqlite3.DatabaseError as exc:
            raise IncompatibleError(f"{path} is not a proofcheck database: {exc}") from exc
        _validate_metadata(meta, path, write=False)
        if int(meta["storage_format"]) == STORAGE_FORMAT:
            return {"database": str(path), "storage_format": STORAGE_FORMAT, "migrated": False,
                    "already_current": True, "backup": None}
        previous_format = int(meta['storage_format'])
        if previous_format not in (2, 3):
            raise IncompatibleError("native migration requires storage format 2 or 3")
        before = conn.execute("PRAGMA data_version").fetchone()[0]
        try:
            fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise InvalidRequest(f"refusing to overwrite existing file {destination}") from None
        os.close(fd)
        try:
            target = sqlite3.connect(str(destination))
            try:
                conn.backup(target)
            finally:
                target.close()
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        after = conn.execute("PRAGMA data_version").fetchone()[0]
        if after != before:
            raise ConflictError("database changed during migration backup; retry with a new backup destination")
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN EXCLUSIVE")
        if conn.execute("PRAGMA data_version").fetchone()[0] != after:
            raise ConflictError("database changed after migration backup; retry with a new backup destination")
        if dict(conn.execute("SELECT key, value FROM metadata")) != meta:
            raise ConflictError("database metadata changed before migration backup; retry with a new backup destination")
        revision = conn.execute("SELECT COALESCE(MAX(revision), 0) FROM commits").fetchone()[0]
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        if previous_format == 2:
            packet_ddl = schema[schema.index("CREATE TABLE packets ("):schema.index("CREATE TABLE evidence_bindings (")]
            packet_ddl = packet_ddl.replace("CREATE TABLE packets (", "CREATE TABLE packets_v3 (", 1)
            for statement in _schema_statements(packet_ddl):
                conn.execute(statement)
            conn.execute("INSERT INTO packets_v3 SELECT * FROM packets")
            conn.execute("DROP TABLE packets")
            conn.execute("ALTER TABLE packets_v3 RENAME TO packets")
            work_ddl = schema[schema.index("CREATE TABLE work_submissions ("):schema.index("CREATE TABLE publications (")]
            for statement in _schema_statements(work_ddl):
                conn.execute(statement)
        record_ddl = schema[schema.index('CREATE TABLE record_versions ('):schema.index('CREATE INDEX versions_at_revision')]
        conn.execute(record_ddl.replace('CREATE TABLE record_versions (', 'CREATE TABLE record_versions_v4 (', 1))
        conn.execute('INSERT INTO record_versions_v4 SELECT * FROM record_versions')
        conn.execute('DROP TABLE record_versions')
        conn.execute('ALTER TABLE record_versions_v4 RENAME TO record_versions')
        conn.execute('CREATE INDEX versions_at_revision ON record_versions(revision,collection,id)')
        triggers = schema[schema.index('CREATE TRIGGER immutable_versions_update'):schema.index('CREATE TRIGGER immutable_commits_update')]
        for statement in _schema_statements(triggers):
            conn.execute(statement)
        for statement in _schema_statements(schema[schema.index('-- SQL superset views'):]):
            conn.execute(statement)
        updates = dict(INIT_METADATA, storage_format=str(STORAGE_FORMAT), contract_version=str(CONTRACT_VERSION),
                       generation=new_id('request'))
        # Preserve every supported requirement already declared by the older database.
        updates["features"] = json.dumps(sorted(set(json.loads(meta.get("features", "[]"))) | set(SUPPORTED_FEATURES)))
        conn.executemany("INSERT INTO metadata(key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", updates.items())
        # Preserve old bodies and judgments verbatim. Only new heads use the
        # separated representation; old evidence is never silently re-certified.
        from .semantics import normalize_edits
        from .refs import extract_refs, facet_digests
        bridge = Database.__new__(Database)
        bridge.path, bridge.conn, bridge.write, bridge.metadata = path, conn, True, dict(meta, **updates)
        edits = [{'op': 'replace', 'collection': 'uses', 'id': r.id, 'expected_version': r.version, 'body': r.body}
                 for r in bridge.heads('uses') if any(k in r.body for k in ('group_id', 'needed_form', 'substitutions'))]
        if edits:
            normalized = normalize_edits(bridge, edits)
            revision += 1
            request_id = new_id('request')
            receipt = {'request_id': request_id, 'revision': revision, 'rebased_from': None,
                       'changed': [], 'warnings': ['Historical mathematical examinations retained; exact targets and boundaries require review.']}
            for edit in normalized:
                version = (edit['expected_version'] or 0) + 1
                receipt['changed'].append({'collection': edit['collection'], 'id': edit['id'], 'version': version, 'op': edit['op']})
            bridge.insert_commit(revision=revision,parent_revision=revision-1,base_revision=revision-1,
                                 request_id=request_id,request_digest=digest({'migration':4,'edits':normalized}),receipt=receipt)
            for edit in normalized:
                c, identity, body = edit['collection'], edit['id'], edit['body']
                version = (edit['expected_version'] or 0) + 1
                bridge.insert_version(c,identity,version,revision,body)
                bridge.set_head(c,identity,version)
                bridge.insert_refs(c,identity,version,extract_refs(c,body))
                bridge.insert_facets(c,identity,version,facet_digests(c,body))
        violations = [dict(row) for row in conn.execute("PRAGMA foreign_key_check")]
        integrity = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        if violations or integrity != ["ok"]:
            raise IncompatibleError("migration failed integrity checks",
                                    records=[{"foreign_key_violations": violations, "integrity": integrity}])
        conn.execute("COMMIT")
        conn.execute("PRAGMA foreign_keys = ON")
        with Database(path, write=True) as checked:
            checked.check_compatibility()
        return {"database": str(path), "storage_format": STORAGE_FORMAT, "previous_storage_format": previous_format,
                "migrated": True, "revision": revision, "backup": str(destination),
                "backup_sha256": sha256_bytes(destination.read_bytes())}
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.close()


def initialize(path, *, source_root, title: str, allow_missing_source_root=False) -> dict:
    """Create a new database with the initialization commit (revision 1) holding the paper record."""
    path = Path(path)
    root = Path(source_root)
    if not isinstance(title, str) or not title.strip():
        raise InvalidRequest("title must be nonempty text")
    if not root.is_dir() and not allow_missing_source_root:
        raise SourceUnavailable(f"source root is not a directory: {root}")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise InvalidRequest(f"refusing to overwrite existing file {path}") from None
    os.close(fd)
    conn = None
    try:
        conn = _open(path, write=True)
        conn.execute("BEGIN IMMEDIATE")
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        for key, value in INIT_METADATA.items():
            conn.execute("INSERT INTO metadata (key, value) VALUES (?, ?)", (key, value))
        conn.execute('INSERT INTO metadata(key,value) VALUES (?,?)', ('generation',new_id('request')))
        conn.execute("INSERT INTO metadata (key, value) VALUES (?, ?)", ("created_at", now_iso()))
        paper_id = new_id("papers")
        body = {"title": title, "source_root": root.resolve().as_posix(), "main_items": [], "report_paths": []}
        request_id = new_id("request")
        request = {"command": "init", "title": title, "source_root": body["source_root"]}
        receipt = {"request_id": request_id, "revision": 1, "rebased_from": None,
                   "changed": [{"collection": "papers", "id": paper_id, "version": 1, "op": "create"}],
                   "warnings": []}
        conn.execute(
            """INSERT INTO commits (revision, parent_revision, base_revision, request_id, request_digest,
                                    receipt_json, created_at) VALUES (1, NULL, NULL, ?, ?, ?, ?)""",
            (request_id, digest(request), body_text(receipt), now_iso()))
        conn.execute(
            """INSERT INTO record_versions (collection, id, version, revision, retired, body_json, body_digest)
               VALUES ('papers', ?, 1, 1, 0, ?, ?)""", (paper_id, body_text(body), digest(body)))
        conn.execute("INSERT INTO record_heads (collection, id, version) VALUES ('papers', ?, 1)", (paper_id,))
        conn.execute("INSERT INTO record_facets (collection, id, version, facet, digest) VALUES ('papers', ?, 1, 'full', ?)",
                     (paper_id, digest(body)))
        conn.execute("COMMIT")
    except Exception:
        if conn is not None:
            try:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
            finally:
                conn.close()
        path.unlink(missing_ok=True)
        raise
    conn.close()
    return {"database": str(path), "paper_id": paper_id, "revision": 1, "receipt": receipt}
