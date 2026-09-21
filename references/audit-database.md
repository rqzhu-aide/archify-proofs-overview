# Compatibility and proofcheck handoff

Read this reference for an existing detailed overview or a requested continuation into proofcheck. New overview authoring follows the focused [database workflow](database.md). A focused overview is a compatible subset of the native schema, not a new audit format. Its source matches are not proof checks, and its selected inventory is not exhaustive audit coverage.

## Existing detailed overviews

Existing native databases, captured exports, and unflagged `init` retain the full schema-3 reader. There is no automatic conversion or pruning. An export has no focused profile metadata: import with `init --focused` to enforce focused authoring, or without the flag for lossless rich-record compatibility. Incompatible rich content is rejected by focused initialization rather than trimmed. A later user-requested rescoping should use atomic edits in the authoritative database. Removing a current record affects a future audit import even when history survives.

Legacy `equation`, `claim`, and `derivation` records retain their required major-item `owner`. Uses with an intermediate endpoint remain detail annotations under the relevant owner and stay outside major graph layout and cycle detection. An intermediate self-use appears once. Legacy use `group` values retain their `joint` or `cases` annotations and consistent group identities; they do not establish logical sufficiency. New focused work creates none of these objects.

Legacy comparison digests and packets remain unchanged. An owner's context includes its owned rows, uses entering the owner and those rows, prerequisite statements, and evidence. Editing that context stales the owner's comparisons and can prevent reviewed reuse. Do not shorten a legacy packet by dropping this evidence. Source snapshots, old observations, and details remain available in native exports and conditional legacy rendering. The retained exhaustive `scaffold`/`reconcile` utilities apply only to non-focused stores; ordinary overview work uses bounded packets and optional candidates.

## Hand off to proofcheck

The overview store and audit store have distinct formats. The explicit handoff uses the bundled `paper_audit.py` wrapper and its local generated `paper_core/` package, without importing a sibling skill. Check the installed bundle before writing:

```text
python <skill>/scripts/paper_audit.py version
```

`bundle.ok` must be `true`. A false or absent value indicates a damaged installation; report it instead of modifying records with that bundle. This check concerns machine-readable compatibility, not the overview's mathematical content.

```text
python <skill>/scripts/paper_audit.py migrate-overview <overview-folder>/data/paper-records.sqlite --backup <overview-folder>/data/paper-records.pre-migration.sqlite
```

The command accepts the overview marker `archify-paper-database-1` with `schema_version: 3`, backs up the file, and rebuilds it in place as audit storage format 3. Other formats return exit 4 `INCOMPATIBLE`; this is a workflow handoff, not a universal format upgrade. A second migration returns `ALREADY_MIGRATED` without changes. Keep the verified backup.

The bridge preserves stable item/use/anchor identities, source bytes and bindings, scope, main-result selection, authored fields, aliases, issues, regimes, and locators. Legacy owner and group records retain their mappings. A group spanning several conclusions becomes one audit group per conclusion, disclosed in `limitations`. Only the newest applicable comparison for each current record becomes a live audit observation. Other observations remain in the archived overview export with a disclosed count. Comparison timestamps and provenance are retained; the handoff creates no mathematical reviews.

Anchors must reference captured source files. An excerpt-only overview without registered files cannot migrate. A page locator on a non-PDF file or an unknown item, owner, or endpoint is also refused rather than narrowed. Correct source bindings in the overview first. The audit store requires the machine-readable feature `overview-bridge/1`; an incompatible bundle refuses it instead of guessing.

After handoff the audit database is authoritative. Do not keep independently editable overview and audit masters. The overview's `paper_database.py` cannot read the audit store; render through `checkpoint`. The proofcheck workflow determines further inventory and checking needs. Importing a selective graph does not imply that omitted proof steps or results were assessed.

## Read an audit store

```text
python <skill>/scripts/paper_audit.py status <db>
python <skill>/scripts/paper_audit.py validate <db>
python <skill>/scripts/paper_audit.py checkpoint <db> --out <overview-folder>/overview.html
python <skill>/scripts/paper_audit.py export <db> --out <overview-folder>/exports/snapshot.json
python <skill>/scripts/paper_audit.py backup <db> --out copy.db
```

Commands emit JSON receipts. Exit codes distinguish acceptance (`0`), invalid data (`2`), conflict (`3`), incompatibility (`4`), unavailable source (`5`), and rendering/publication failure (`6`). Report the structured error code. An incomplete audit can be a successful status query with `process_complete: false`; it is not a failed command.

`status` distinguishes structural health, source limitations, review coverage, mathematical assessments, and published revision. `validate` checks structure and evidence bindings, not proof correctness. `checkpoint` preserves the previous HTML when rendering fails. `backup` provides full recovery; a mathematical export does not restore an operational controller session. Use proofcheck's guidance for audit maintenance, worklists, and new detailed checks. Do not edit either store directly with SQLite.

The audit reader keeps major-result graph conventions while exposing its richer recorded evidence separately. Any audit assessment derives from those audit records; the overview's source-comparison colors or counts must not be reinterpreted as proof judgments.
