# Audit database (v2.0)

The `paper_database.py` workflow in [database.md](database.md) remains the default for
building and revising overviews. This document covers the shared audit core that ships beside it, for
two cases: upgrading an overview so a proof audit can continue from the same records, and reading an
audit database that the proofcheck skill produced.

Entry point: `python <skill>/scripts/paper_audit.py <command>`. It runs the generated
`scripts/paper_core/` bundle beside it, the same bundle the proofcheck skill ships, so both skills
read and write one storage format. It never reads this skill's `paper_database.py` and never looks up
a sibling skill. Confirm the installation first:

```text
python <skill>/scripts/paper_audit.py version
```

`bundle.ok` must be `true`. A `false` or absent value means the installed files no longer match their
manifest; report a damaged installation rather than writing records with it.

## Command contract

Every command writes one JSON object to stdout, with diagnostics on stderr. Exit codes are `0`
accepted or read, `2` invalid request or data, `3` conflict, `4` incompatible schema or package, `5`
source unavailable, `6` render or publication failure. A failing command returns
`{"ok": false, "error": {"code", "message", "records", ...}}`; report the code rather than a
paraphrase. An incomplete audit is a successful `status` query with `process_complete: false` and
exit 0, never a command failure.

## Upgrading an overview database

An overview database written by `paper_database.py` is format `archify-paper-database-1` at snapshot
schema version 2. The audit core reports it as exit 4 `INCOMPATIBLE` and refuses to write to it until
it is upgraded explicitly:

```text
python <skill>/scripts/paper_audit.py migrate-overview <overview-folder>/data/paper-records.sqlite --backup <overview-folder>/data/paper-records.pre-migration.sqlite
```

The upgrade backs the database up first, then rebuilds it in place as storage format 3. It preserves
item, use, and anchor identity, keeps use-targeted observations, imports the source blobs unchanged,
and records an identity map plus the original snapshot IDs as provenance. It leaves uses ungrouped
and records no audit outcomes: migration moves records, it does not create mathematical reviews.
Running it a second time returns `ALREADY_MIGRATED` and changes nothing.

Migrate only when the records are going into a proof audit. An overview that will stay an overview
needs no upgrade, and `paper_database.py` cannot read a migrated database. Keep the backup.

A native audit database with `storage_format: 2` is a different older format. It remains readable,
but writing requires `paper_audit.py migrate DB --backup BACKUP.db`. This preserves its mathematical
revisions and historical packet-1 bytes. New native databases use storage format 3, record contract 3,
packet version 2, and projection version 2. No migration changes proof judgments or grants reuse.

## Reading and reporting on an audit database

After migration, or on a database the proofcheck skill created, these commands are the ones this
skill needs:

```text
python <skill>/scripts/paper_audit.py status <db>
python <skill>/scripts/paper_audit.py work list <db> --audit <audit-id>
python <skill>/scripts/paper_audit.py validate <db>
python <skill>/scripts/paper_audit.py checkpoint <db> --out <overview-folder>/overview.html
python <skill>/scripts/paper_audit.py export <db> --out <overview-folder>/exports/snapshot.json
python <skill>/scripts/paper_audit.py attach <db> --report <overview-folder>/proofcheck-report.html
```

`status` separates structural health, source limits, review coverage, mathematical assessments, and
the published revision. `validate` checks record shapes, references, bindings, coverage intervals,
and projection inputs; it certifies nothing mathematical, so a passing validation is not a complete
inventory and not a correct proof. `checkpoint` renders the projection to a working HTML report and
retains the previous file byte for byte if the render fails. `attach` registers a proofcheck report
path on the paper record so there is still exactly one master.

The worklist shows actionable recorded obligations and coordinator diagnostics. The proofcheck
coordinator can focus on a result and prepare a coherent sequence of work, rather than ask a model
to schedule each edge. The controller does not infer missing mathematical dependencies: inventory
and graph refinement still require reading the manuscript. Incomplete task analysis must remain
visible as incomplete; it is not evidence that the remaining proof is empty.

The rendered report uses the same viewer conventions as an overview: node colors identify
mathematical types, arrows run from a prerequisite to the result that uses it, and the renderer falls
back to a complete index when its layout cannot draw the mapping. Connection states in an audit
report are derived from recorded check evidence and carry their qualifications; they are not
extraction colors and must not be described as a correctness verdict.

Keep the overview diagram at major-result level. Hidden intermediate claims, applications and joint
derivations belong in the connection detail and lower reader; worklist links lead to that detailed
evidence. Distinguish local outcome, freshness, dependency support, and process completion. A valid
application of a lemma with an unresolved proof can have conditional support without making that
application itself a new proof defect.

Use `backup <db> --out copy.db` for complete recovery. Mathematical `export` contains record-linked
evidence, not the operational submission index, all packets or commit history; it cannot restore a
controller session. Rendering and reading worklists require no mathematical model call.

## Boundaries

- Do not maintain an overview JSON, an overview database, and an audit database as three masters.
  After migration the audit database is the authority.
- Do not open either database with another SQLite tool to change records. Record versions are
  immutable and every edit goes through a packet.
- Do not run new-format data through `paper_database.py validate`, and do not point
  `paper_audit.py` at an unmigrated overview expecting it to adapt.
- Inventory coverage still does not establish proof validity. State selected scope, omitted sections,
  and unresolved source questions explicitly.
