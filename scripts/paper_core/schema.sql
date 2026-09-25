-- Proofcheck storage format 4, record contract 4.
-- Normative DDL scaffold. JSON body and cross-record semantic validation belong
-- to the core described in record-contract.md. This is not a runtime migration.
PRAGMA foreign_keys = ON;

CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO metadata VALUES ('storage_format', '4');
INSERT INTO metadata VALUES ('contract_version', '4');

CREATE TABLE blobs (
    sha256 TEXT PRIMARY KEY CHECK (length(sha256) = 64),
    content BLOB NOT NULL
);

CREATE TABLE commits (
    revision INTEGER PRIMARY KEY CHECK (revision >= 1),
    parent_revision INTEGER REFERENCES commits(revision),
    base_revision INTEGER REFERENCES commits(revision),
    request_id TEXT NOT NULL UNIQUE,
    request_digest TEXT NOT NULL CHECK (length(request_digest) = 64),
    receipt_json TEXT NOT NULL CHECK (json_valid(receipt_json)),
    created_at TEXT NOT NULL
);

CREATE TABLE record_versions (
    collection TEXT NOT NULL CHECK (collection IN (
        'papers','sources','anchors','items','parts','scopes','arguments',
        'groups','uses','coverage','checks','findings','source_issues','repairs',
        'observations','responses','reconciliations','audits','qualifications',
        'source_reviews','reuse_decisions','identity_maps','overview_selections',
        'target_specs','application_details','connection_refinements','proof_boundaries'
    )),
    id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    revision INTEGER NOT NULL REFERENCES commits(revision),
    retired INTEGER NOT NULL CHECK (retired IN (0, 1)),
    body_json TEXT,
    body_digest TEXT NOT NULL CHECK (length(body_digest) = 64),
    PRIMARY KEY (collection, id, version),
    UNIQUE (collection, id, revision),
    CHECK ((retired = 1 AND body_json IS NULL) OR
           (retired = 0 AND body_json IS NOT NULL AND json_valid(body_json)))
);
CREATE INDEX versions_at_revision ON record_versions(revision, collection, id);

CREATE TABLE record_heads (
    collection TEXT NOT NULL,
    id TEXT NOT NULL,
    version INTEGER NOT NULL,
    PRIMARY KEY (collection, id),
    FOREIGN KEY (collection, id, version)
      REFERENCES record_versions(collection, id, version)
      DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE record_refs (
    owner_collection TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    owner_version INTEGER NOT NULL,
    field_path TEXT NOT NULL, -- body-relative JSON Pointer; record-contract 1.1
    target_collection TEXT NOT NULL,
    target_id TEXT NOT NULL,
    target_version INTEGER,
    PRIMARY KEY (owner_collection, owner_id, owner_version, field_path),
    FOREIGN KEY (owner_collection, owner_id, owner_version)
      REFERENCES record_versions(collection, id, version)
      DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (target_collection, target_id)
      REFERENCES record_heads(collection, id)
      DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (target_collection, target_id, target_version)
      REFERENCES record_versions(collection, id, version)
      DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX refs_to_target ON record_refs(target_collection, target_id, owner_collection);
CREATE INDEX refs_from_field ON record_refs(owner_collection, field_path, target_collection, target_id);

CREATE TABLE record_facets (
    collection TEXT NOT NULL,
    id TEXT NOT NULL,
    version INTEGER NOT NULL,
    facet TEXT NOT NULL CHECK (facet IN (
      'statement','proof','application','inference','scope','coverage','source','full'
    )),
    digest TEXT NOT NULL CHECK (length(digest) = 64),
    PRIMARY KEY (collection, id, version, facet),
    FOREIGN KEY (collection, id, version)
      REFERENCES record_versions(collection, id, version)
      DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE packets (
    packet_id TEXT PRIMARY KEY,
    packet_version INTEGER NOT NULL CHECK (packet_version IN (1, 2)),
    base_revision INTEGER NOT NULL REFERENCES commits(revision),
    mode TEXT NOT NULL CHECK (mode IN ('author','primary','independent','reconcile')),
    manifest_json TEXT NOT NULL CHECK (json_valid(manifest_json)),
    payload_sha256 TEXT NOT NULL REFERENCES blobs(sha256),
    created_at TEXT NOT NULL
);

CREATE TABLE evidence_bindings (
    owner_collection TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    owner_version INTEGER NOT NULL,
    packet_id TEXT REFERENCES packets(packet_id),
    bindings_json TEXT NOT NULL CHECK (json_valid(bindings_json)),
    PRIMARY KEY (owner_collection, owner_id, owner_version),
    FOREIGN KEY (owner_collection, owner_id, owner_version)
      REFERENCES record_versions(collection, id, version)
      DEFERRABLE INITIALLY DEFERRED
);

-- The operational intake index does not define mathematical progress.
CREATE TABLE work_submissions (
    request_id TEXT PRIMARY KEY,
    request_digest TEXT NOT NULL CHECK (length(request_digest) = 64),
    packet_id TEXT NOT NULL REFERENCES packets(packet_id),
    audit_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('primary', 'independent', 'reconcile')),
    envelope_sha256 TEXT NOT NULL REFERENCES blobs(sha256),
    response_sha256 TEXT NOT NULL REFERENCES blobs(sha256),
    received_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('received', 'accepted', 'needs_revision', 'conflict')),
    committed_revision INTEGER REFERENCES commits(revision),
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    CHECK (
      (state = 'received' AND committed_revision IS NULL AND result_json IS NULL)
      OR (state = 'accepted' AND committed_revision IS NOT NULL AND result_json IS NOT NULL)
      OR (state = 'needs_revision' AND result_json IS NOT NULL)
      OR (state = 'conflict' AND committed_revision IS NULL AND result_json IS NOT NULL)
    )
);
CREATE INDEX work_submissions_by_audit ON work_submissions(audit_id, received_at, request_id);
CREATE INDEX work_submissions_by_packet ON work_submissions(packet_id, received_at, request_id);
CREATE INDEX packets_work_audit
    ON packets(json_extract(manifest_json, '$.work.audit_id'), created_at, packet_id);
CREATE TRIGGER work_submissions_no_delete BEFORE DELETE ON work_submissions
BEGIN SELECT RAISE(ABORT, 'submission history is immutable'); END;
CREATE TRIGGER work_submissions_finalize_only BEFORE UPDATE ON work_submissions
WHEN OLD.state <> 'received' OR NEW.state = 'received'
  OR NEW.request_id <> OLD.request_id OR NEW.request_digest <> OLD.request_digest
  OR NEW.packet_id <> OLD.packet_id OR NEW.audit_id <> OLD.audit_id OR NEW.role <> OLD.role
  OR NEW.envelope_sha256 <> OLD.envelope_sha256 OR NEW.response_sha256 <> OLD.response_sha256
  OR NEW.received_at <> OLD.received_at
BEGIN SELECT RAISE(ABORT, 'only received submissions may be finalized'); END;

CREATE TABLE publications (
    id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL REFERENCES commits(revision),
    kind TEXT NOT NULL CHECK (kind IN ('working','release')),
    state TEXT NOT NULL CHECK (state IN ('built','published','failed')),
    output_path TEXT NOT NULL,
    artifact_sha256 TEXT REFERENCES blobs(sha256),
    receipt_json TEXT NOT NULL CHECK (json_valid(receipt_json)),
    created_at TEXT NOT NULL
);
CREATE INDEX publication_revision ON publications(revision, created_at);

CREATE TABLE run_events (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    stage TEXT NOT NULL CHECK (stage IN (
      'reading_reasoning','authoring','retry','renewal','command','waiting','report','combined'
    )),
    started_at TEXT NOT NULL,
    elapsed_ms INTEGER CHECK (elapsed_ms IS NULL OR elapsed_ms >= 0),
    details_json TEXT NOT NULL CHECK (json_valid(details_json))
);

-- Append-only history is enforced at SQL and command boundaries. Drafts append
-- versions too. record_heads alone advances. Compaction is a future migration,
-- not a normal worker operation.
CREATE TRIGGER immutable_versions_update BEFORE UPDATE ON record_versions
BEGIN SELECT RAISE(ABORT, 'record versions are immutable'); END;
CREATE TRIGGER immutable_versions_delete BEFORE DELETE ON record_versions
BEGIN SELECT RAISE(ABORT, 'record versions are immutable'); END;
CREATE TRIGGER immutable_commits_update BEFORE UPDATE ON commits
BEGIN SELECT RAISE(ABORT, 'commits are immutable'); END;
CREATE TRIGGER immutable_commits_delete BEFORE DELETE ON commits
BEGIN SELECT RAISE(ABORT, 'commits are immutable'); END;

-- Use one BEGIN IMMEDIATE transaction. Declared deferred reference constraints
-- are checked at commit; the record_versions.revision FK is immediate.
-- A retired head remains for historical reference resolution; the application
-- rejects live references to it unless they are explicitly historical/pinned.
-- Follow implementation-handoff 4.3: next = COALESCE(MAX(revision), 0) + 1 while
-- holding the writer lock; compute the final receipt and INSERT commits first,
-- then blobs, versions, heads, refs/facets/bindings. Never update the receipt.

-- SQL superset views: all derived from canonical immutable versions.
CREATE TRIGGER writer_format_barrier BEFORE INSERT ON commits
BEGIN
  SELECT CASE WHEN proofcheck_writer_format() != 4
    THEN RAISE(ABORT, 'incompatible writer; reopen with SQL superset core') END;
END;

CREATE VIEW current_records AS
SELECT v.* FROM record_heads h JOIN record_versions v USING(collection,id,version)
WHERE v.retired=0;

CREATE VIEW overview_items AS
SELECT s.id AS selection_id, i.id, i.version, i.body_json,
       EXISTS(SELECT 1 FROM json_each(s.body_json,'$.main_item_ids') m WHERE m.value=i.id) AS is_main
FROM current_records s JOIN json_each(s.body_json,'$.item_ids') m
JOIN current_records i ON i.collection='items' AND i.id=m.value
WHERE s.collection='overview_selections';

CREATE VIEW overview_connections AS
SELECT s.id AS selection_id,u.id,u.version,u.body_json,
       json_extract(u.body_json,'$.from.id') AS supplier_id,
       json_extract(u.body_json,'$.to.id') AS consumer_id
FROM current_records s JOIN json_each(s.body_json,'$.use_ids') m
JOIN current_records u ON u.collection='uses' AND u.id=m.value
WHERE s.collection='overview_selections';

CREATE VIEW proof_targets AS
SELECT s.id,s.version,json_extract(s.body_json,'$.target.collection') AS target_collection,
       json_extract(s.body_json,'$.target.id') AS target_id,
       json_extract(s.body_json,'$.scope_id') AS scope_id,
       json_extract(s.body_json,'$.state') AS state,s.body_json
FROM current_records s WHERE s.collection='target_specs';

CREATE VIEW proof_applications AS
SELECT u.id,u.version AS use_version,a.version AS application_version,
       json_extract(u.body_json,'$.from.collection') AS supplier_collection,
       json_extract(u.body_json,'$.from.id') AS supplier_id,
       json_extract(u.body_json,'$.to.collection') AS conclusion_collection,
       json_extract(u.body_json,'$.to.id') AS conclusion_id,
       json_extract(a.body_json,'$.group_id') AS group_id,
       json_extract(a.body_json,'$.scope_id') AS scope_id,
       json_extract(a.body_json,'$.state') AS state,
       u.body_json AS connection_json,a.body_json AS application_json
FROM current_records u JOIN current_records a ON a.collection='application_details' AND a.id=u.id
WHERE u.collection='uses';

CREATE VIEW proof_inferences AS
SELECT g.id,g.version,json_extract(g.body_json,'$.argument_id') AS argument_id,
       json_extract(g.body_json,'$.conclusion.id') AS conclusion_id,
       json_extract(g.body_json,'$.scope_id') AS scope_id,g.body_json
FROM current_records g WHERE g.collection='groups';

CREATE VIEW proof_checks AS
SELECT c.id,c.version,c.revision,c.retired,
       json_extract(c.body_json,'$.target.collection') AS target_collection,
       json_extract(c.body_json,'$.target.id') AS target_id,
       json_extract(c.body_json,'$.kind') AS kind,
       json_extract(c.body_json,'$.outcome') AS outcome,c.body_json,
       EXISTS(SELECT 1 FROM record_heads h WHERE h.collection=c.collection AND h.id=c.id AND h.version=c.version) AS is_head
FROM record_versions c WHERE c.collection='checks';

CREATE VIEW proof_consumers AS
SELECT r.target_collection,r.target_id,r.owner_collection,r.owner_id,r.field_path
FROM record_refs r JOIN current_records v ON v.collection=r.owner_collection AND v.id=r.owner_id AND v.version=r.owner_version;
