# Focused overview database

SQLite is authoritative after initialization. It retains immutable record versions, captured source bytes, append-only source comparisons, and build receipts. JSON seeds, edit batches, and exports are inputs or snapshots, not independently editable masters. New databases use common storage format 4, shared with proofcheck. The overview commands retain schema-3 seed, edit, and export shapes and their broad source-comparison semantics.

The common backend always uses the skill's bundled runtime, and `init` reports its backend and version; an incomplete or incompatible bundle requires reinstalling the complete skill, without a fallback to a different backend.

## Start and continue

Use the shared Python interpreter. Paths below are abbreviated; the normal database lives at `<overview-folder>/data/paper-records.sqlite`.

```text
python <skill>/scripts/paper_database.py init paper-records.sqlite work/seed.json --focused --source-root <manuscript-folder>
python <skill>/scripts/paper_database.py list paper-records.sqlite
python <skill>/scripts/paper_database.py get paper-records.sqlite representer
python <skill>/scripts/paper_database.py apply paper-records.sqlite work/edits.json
python <skill>/scripts/paper_database.py compare paper-records.sqlite work/comparisons.json
python <skill>/scripts/paper_database.py render paper-records.sqlite overview.html
```

Initialize from a [focused seed](authoring.md) or compatible captured export. A seed may contain one main result and no connections. `init` never replaces an existing database or fabricates comparisons. New seed records start unreviewed; imported comparisons remain bound to their recorded context.

`--focused` persists the focused authoring profile in the overview selection. Every later edit checks the complete proposed selected view, including metadata-only changes: major kinds, explicit nonempty main-result selection, major-to-major uses, and no non-null owner/group fields. Null optional fields behave as omission. An applicable matched comparison on a connection without located evidence is rejected during focused import, comparison, validation, and reviewed reuse. These are structural authoring checks, not mathematical verification.

Profile metadata is outside the mathematical payload and target digests. Portable exports retain the native schema without a profile field; use `init --focused` when importing one for focused work. Incompatible rich content is rejected without trimming records or observations. Existing rich databases and lossless unflagged import remain supported through the [compatibility path](audit-database.md#existing-detailed-overviews), not the new-work tutorial.

Seed source references resolve from the JSON directory. `--source-root` identifies the actual manuscript directory for stored paths, extra sources, and refreshes. Literal local TeX inputs and packages are captured. Register additional relevant files using repeatable `--source appendix.tex`; register a PDF used for numbering, formulas, or evidence before comparisons. Adding a source later changes the source revision and initially stales earlier comparisons. Captured exports retain source bindings, so provide the root where their relative paths resolve.

## Read one argument

`list <db>` gives compact IDs, labels, roles, connection endpoints, anchor references, comparison states, and `expected_snapshot`, without statement bodies or source excerpts. Use `--collection items`, `uses`, or `anchors` to narrow it. Copy generated IDs from this listing or a packet; do not infer their hash suffixes or inspect raw SQL merely to find them.

`get <db> <item-id>` returns the selected statement, incoming connections, directly relevant prerequisites, required source passages, current comparisons, freshness, and `expected_snapshot`. Join item `passages[].anchor_id` and use `evidence_refs` to the packet's `anchors`. Comparison `note_ref` values resolve through `comparison_notes`, so repeated notes appear once. Full source blobs and accumulated review history are omitted; exports retain them. Empty compatibility fields can remain.

Retrieve the argument being edited, not the whole export. Reuse recently read context at the same source revision and read additional source only when needed. A packet is not a claim that its prerequisites suffice. See [revisions.md](revisions.md) when the manuscript changes.

For standing setup, seed passages may repeat the same source locator. In canonical edits, reuse a suitable existing `anchor_id` in related items' `passages` with role `definition` or `evidence`, as appropriate. Read shared context once where possible; no separate anchor-consolidation pass or graph node is needed merely for bookkeeping.

`validate` reports the authoring profile, integrity, source freshness, source-comparison states, stale target IDs, and uses lacking evidence separately. Resolve malformed records before rendering. An unverified locator or unresolved interpretation remains a visible limitation; a passing validation is not proof correctness or exhaustive coverage.

`get`, `validate`, and `render` also report `locator_diagnostics` for ambiguous TeX keys or statement ranges inside a different declaration. These current checks preserve saved evidence and its original verification metadata. Correct a conflicting statement locator before recording a new `matched` comparison; `needs_attention` can retain an unresolved case. A quoted statement subrange need not repeat its declaration label. A duplicate key alone is a warning, not a demand to rewrite the manuscript.

## Apply a bounded batch

Use the current `expected_snapshot` from `list` or a packet. **An `upsert` replaces the entire record; it does not merge omitted fields.** Re-supply every intended field. A seed's inline `source` becomes an anchor at initialization: canonical item edits use `passages[].anchor_id`, and use edits use `evidence_refs`. Do not paste a seed-style `source` into a canonical item edit. An anchor upsert needs an existing file ID and a verified locator; the script extracts and hashes its passage.

```json
{
  "expected_snapshot": "COPY_THE_PACKET_SNAPSHOT",
  "set": {
    "scope": "Selected main result and its important prerequisites.",
    "main_items": ["representer"]
  },
  "edits": [
    {
      "collection": "anchors",
      "op": "upsert",
      "id": "anchor-projection-use",
      "record": {
        "file_id": "COPY_REGISTERED_FILE_ID",
        "locator": {"start_line": 75, "end_line": 81}
      }
    },
    {
      "collection": "items",
      "op": "upsert",
      "id": "projection-lemma",
      "record": {
        "kind": "lemma",
        "label": "Lemma 4",
        "caption": "Projection preserves training evaluations",
        "statement": {
          "form": "synopsis",
          "text": "Under Assumption 1 and Definition 2, every $f\\in\\mathcal H$ has a unique decomposition $f=s+h$ with $s=P_Sf\\in S$ and $h\\in S^\\perp$; $f(x_i)=s(x_i)$ for $i=1,\\ldots,n$ and $\\|f\\|_{\\mathcal H}^2=\\|s\\|_{\\mathcal H}^2+\\|h\\|_{\\mathcal H}^2$."
        },
        "passages": [
          {"role": "statement", "anchor_id": "COPY_LEMMA_STATEMENT_ANCHOR_ID"},
          {"role": "proof", "anchor_id": "COPY_LEMMA_PROOF_ANCHOR_ID"}
        ]
      }
    },
    {
      "collection": "uses",
      "op": "upsert",
      "id": "COPY_EXISTING_PROJECTION_USE_ID",
      "record": {
        "from": "projection-lemma",
        "to": "representer",
        "type": "dependency",
        "reason": "Lemma 4 preserves the minimizer's training evaluations; its norm identity and the strictly increasing penalty force the orthogonal component to vanish.",
        "evidence_refs": ["anchor-projection-use"]
      }
    }
  ]
}
```

This example follows the minimal seed's `projection-lemma` and `representer` identities. Copy the lemma's existing passage anchor IDs and the existing use ID from `get` or `list`; use the registered `proof.md` file ID for the new anchor. The sample lines are specific to that example. For another paper, use its actual locations and wording. `set` is a top-level sibling of `edits`, never an edit operation. It changes `title`, `scope`, or `main_items`; a focused database cannot clear its main-result selection. A metadata-only batch can use an empty `edits` list. Do not write per-declaration exclusions merely because statements are outside the selected scope.

For item upserts supply `kind`, `label`, `caption`, structured `statement`, and `passages`, plus any intended optional fields. Renumbering changes `label`, not identity. On a common store, upserts preserve proofcheck extensions and unselected records. `remove` deselects an existing item or use without deleting its common identity; revise selected references atomically. The whole batch is checked before committing. An endpoint change that contradicts a registered proof application is refused until proofcheck updates or withdraws that structure. A stale snapshot rejects the batch: retrieve and review intervening changes before submitting a revised expectation.

Write JSON batches as files with a file-writing tool, not inline shell strings that may corrupt backslash mathematics. Do not hand-write generated hashes, timestamps, comparison IDs, or duplicated excerpts.

## Record source comparison

```json
{
  "expected_snapshot": "COPY_THE_CURRENT_SNAPSHOT",
  "targets": [
    {"collection": "items", "id": "projection-lemma"},
    {"collection": "items", "id": "representer"},
    {"collection": "uses", "id": "COPY_EXISTING_PROJECTION_USE_ID"}
  ],
  "reviewer": "overview-author",
  "result": "matched",
  "note": "Compared the stated restrictions and conclusion, and the projection contribution, with their located passages."
}
```

Compare the actual current stored statements and reasons, including essential qualifications, with source evidence as explained in [authoring.md](authoring.md#compare-and-display). Use the current packet or exact records already in context; fetch again only when needed to obtain the current version. After edits, compare the changed context before recording a match.

One top-level `result` and `note` applies to the named targets. Group only comparisons for which that note is accurate; use separate batches for different findings. `matched` means the saved content was compared and agrees with its source, not that its proof is valid. Use `needs_attention` with a specific unresolved interpretation. An item comparison does not automatically review its incoming connections; name every reviewed target.

The four displayed states are `unreviewed`, `matched`, `needs_attention`, and `stale`. The aggregate `complete` means every recorded target has a current match; `incomplete` can also mean everything was reviewed but some questions remain. Its summary and counts distinguish those cases. A reviewed unresolved record can be delivered honestly, while unreviewed and stale selected records still need attention.

Comparisons bind to the selected source manifest and relevant statement/connection context. A substantive change can stale connected comparisons even when the displayed target is unchanged. Unselected audit additions and audit-only sources do not silently expand that context. Appending observations does not change the mathematical snapshot ID. A newer `needs_attention` for identical context supersedes an earlier match. The [revision workflow](revisions.md) covers explicit reviewed reuse; unchanged text alone does not perform that review.

If proofcheck captures revised bytes for a selected source before the overview anchors are refreshed, an overview read can report that the stored excerpts need rebinding. Run the `refresh` command and current `--expected-snapshot` shown in that message; supply revised anchors if their locations changed. Refresh checks the resulting source bindings and leaves earlier comparisons stale for review.

A page supplied beside a text line or label locator is retained as overview navigation metadata. The shared source evidence remains tied to the captured text location; a page alone requires a captured PDF. Correcting a supplemental page still requires review of the overview comparison context.

## Optional discovery and delivery

```text
python <skill>/scripts/paper_database.py candidates paper-records.sqlite --output work/candidates.json
python <skill>/scripts/paper_database.py export paper-records.sqlite exports/paper-records.json
python <skill>/scripts/paper_database.py backup paper-records.sqlite paper-records-backup.sqlite
```

Use citation candidates when connections are uncertain. They concern captured source, not live files, and propose reading without writing records. Inspect selected endpoints and actionable evidence gaps. A missing citation is not a false arrow; unselected declarations are listed separately under `outside_selected_scope`, not treated as missing overview content. Shared scan limitations need one explanation. PDF-only input has no TeX citation scan and requires direct reading. Exhaustive `scaffold`/`reconcile` utilities are retained for compatibility and refuse focused stores; they are not focused authoring or completion steps.

Final rendering checks exact record preservation, input/output hashes, actual graph identities and endpoints, source/comparison status, and selected geometry before replacing the HTML. Failure preserves the prior report. Cycles and parallel connections remain visible; layout order is not proof order. Mechanical checks do not establish readability or interaction correctness.

Large math diagnostic sets are grouped by cause and record field, with totals and a short sample in the render receipt. Use `render <db> <html> --full-diagnostics` or the HTML's Math display notes when individual occurrences are needed. This avoids reading the same macro warning repeatedly; it does not suppress unresolved display limitations in the report.

Regenerate a retained export after final comparisons and check its observation receipt as well as its snapshot. Use `backup` for a consistent database copy. The database includes captured source files; share it only when those sources should be shared. The HTML alone is standalone and contains selected excerpts. Historical snapshots remain renderable with their source limitations disclosed.
