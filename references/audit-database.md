# Hand off a v3 overview to proofcheck

The `paper_database.py` workflow in [database.md](database.md) builds and revises native v3
overviews. This document covers the shared audit core for two cases: handing a v3 overview to
proofcheck so an audit can continue from the same records, and reading an audit database that
proofcheck produced.

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

## Hand off a native v3 overview database

The v3 overview store and audit store have different formats. The overview store's internal marker
is `archify-paper-database-1`; its record payload must be `schema_version: 3`. The audit core
refuses to write to that store until the explicit handoff:

```text
python <skill>/scripts/paper_audit.py migrate-overview <overview-folder>/data/paper-records.sqlite --backup <overview-folder>/data/paper-records.pre-migration.sqlite
```

The command accepts native schema-3 overview records only. Other overview versions receive exit 4
`INCOMPATIBLE`; this is a workflow handoff, not an old-version upgrade path.

One boundary: an overview built from a supplied excerpt, whose anchors have no captured source file,
cannot migrate — contract-3 anchors require a registered source. The refusal is exit 4 `INCOMPATIBLE`
with the anchor named, never a silent loss. Register the manuscript sources before migrating, or keep
such an overview as an overview.

The handoff backs the database up first, then rebuilds it in place as audit storage format 3. It preserves
item, use, and anchor identity and carries every authored field: item kinds (including owned
`equation`/`claim`/`derivation` intermediate rows), aliases, `issue` notes, use
`regime`s, evidence anchors, and all four locator shapes — line spans, TeX labels, PDF pages, and
combinations — with line excerpts re-verified against the captured source. Use groups migrate as
provenance groups whose conclusion is the member uses' shared target; an overview group id spanning
several conclusions becomes one group record per conclusion and says so in the receipt's
`limitations`. Only the newest applicable comparison for each current item or use is imported as a
live observation, retaining its original `created_at`. Stale and superseded observations remain in
the archived overview export; the receipt reports their count. Imported observations also preserve
overview `created_at`, `input_snapshot`, and `carried_from` in their notes. The source blobs are
imported unchanged, and an identity map plus the original snapshot IDs are recorded as provenance.
The backup is taken before the rebuild and is kept when the rebuild refuses.

The handoff moves records and creates no mathematical reviews. Any uncarried field appears in
`limitations`. Three invalid source or reference states are refused rather than narrowed:
an anchor naming no captured source file, a page locator on a non-PDF source, and an item, owner, or
use endpoint naming an unknown record. Re-anchor or correct those in the overview first.

The rebuilt database stamps the `overview-bridge/1` feature. An audit core older than this release —
including a stale `scripts/paper_core/` bundle — refuses it with exit 4 `INCOMPATIBLE` naming that
feature; keep both skills on the same release. After migration, render with `paper_audit.py
checkpoint`; this skill's own `paper_database.py render` reads only the native overview store and
cannot read the migrated file. Running the migration a second time returns `ALREADY_MIGRATED` and
changes nothing.

Use the handoff only when the records are going into a proof audit. An overview that will stay an
overview needs no conversion, and `paper_database.py` cannot read the audit store. Keep the backup.

Audit storage, record, packet, and projection versions are separate from the overview schema.
Use proofcheck's own guidance for audit-store maintenance; retiring old overview formats does not
change that workflow or grant new proof judgments.

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
