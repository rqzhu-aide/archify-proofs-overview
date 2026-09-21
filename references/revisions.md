# Revise an existing overview efficiently

Reuse the same database, statement identities, and output folder. Review source differences and affected selected statements and connections. This workflow updates the overview's source interpretation; it does not audit proof validity or automatically rescope existing detailed databases.

## Capture the revision and inspect what changed

First read the current database snapshot ID using `validate`. Record it as `OLD`. Then capture the revised manuscript and compare the two retained snapshots:

```text
python <skill>/scripts/paper_database.py validate <overview-folder>/data/paper-records.sqlite
python <skill>/scripts/paper_database.py refresh <overview-folder>/data/paper-records.sqlite --expected-snapshot OLD --relocate-exact
python <skill>/scripts/paper_database.py changes <overview-folder>/data/paper-records.sqlite --since OLD --output <overview-folder>/work/changes.json
```

Use the returned current snapshot ID in subsequent batches. `changes` compares captured versions, so the diff and its candidate list refer to one exact input. With `--output`, stdout is a compact receipt and the saved JSON contains the complete source diff. Without it, the report is printed. Neither command marks a source comparison as complete.

Inspect changed source context relevant to reuse, including changes outside registered statement/proof passages. The report distinguishes source changes, changed or moved anchors, changed records, possible downstream impact, and reuse candidates. Its limitations identify changed overview scope or presentation metadata. Consider new/removed declarations for relevance to the chosen results; they do not create an obligation to expand the overview. Graph reachability identifies possible impact, not a mathematical defect or failure of every proof route.

Use `get` to retrieve only the items needing further attention. A changed assumption, definition, conditioning statement, macro, or global notation may affect otherwise identical theorem text. In that case extend the source comparison to the affected argument. Do not classify changes outside anchors as irrelevant by default.

If the live manuscript changes again after capture, refresh and inspect the new diff before calling the output current. `validate` and the changes report distinguish captured versions from changed live sources. Old views and comparisons remain reproducible.

## Passage movement and renamed files

`--relocate-exact` retains the existing locator when it still matches the stored excerpt. Otherwise it moves a line range only if the exact nonempty excerpt has one whole-line match in the expected file. Repeated matches require an explicit locator. Changed text is not matched fuzzily or called unchanged; inspect it and correct its anchor when needed.

Use `refresh --anchors work/reanchors.json` for explicit replacements:

```json
{
  "anchor-main-proof": {"locator": {"start_line": 80, "end_line": 96}}
}
```

To rename source files, use `refresh --file-map work/files.json`. The map uses registered file IDs, preserving their identity:

```json
{
  "file-REGISTERED_ID": "appendix/revised-proof.tex"
}
```

Paths resolve from the source root. A `null` mapping removes a source from explicit registration; remove or rebind any remaining anchors that depend on it. Update the paper's actual input declarations as appropriate to its revision, not merely to satisfy the tool. The script does not guess file replacements. For a whole project move, `refresh --source-root <new-manuscript-folder>` resolves stored relative paths there. Older absolute paths or individually renamed files need explicit mappings.

These options can be combined in one refresh. A failed capture or ambiguous re-anchoring preserves the existing database snapshot. Existing source comparisons become stale until new comparisons or reviewed reuse apply.

## Reuse eligible comparisons after reviewing context

A reuse candidate requires an applicable prior `matched` observation, unchanged recorded content and evidence, and unchanged prerequisite context. Location movement alone may be allowed. Candidates exclude changed or possibly affected targets and a current exact-input `needs_attention` observation. Focused reuse cannot mark an unlocated connection matched. Changed PDF evidence cannot be called unchanged merely because extracted text looks the same. Printed labels and page numbers also require attention when numbering or pagination changes. [Rich-record compatibility](audit-database.md#existing-detailed-overviews) preserves the broader context of older owned records.

A page-number correction attached to an unchanged TeX line passage is reported as a location change. Check the corrected page, then use the reuse batch below for eligible comparisons; unchanged downstream arguments do not need to be read again. The exact comparison remains stale until that review is recorded. Changing a PDF anchor's page selects different evidence and is not treated as this kind of correction.

Adding a PDF with `refresh --source <paper.pdf>` changes the captured source revision, so existing comparisons initially become stale. Inspect the added document and the `changes` report; comparisons whose recorded evidence and context are unchanged can use the same reviewed-reuse batch. Adding the file does not check pages attached to TeX anchors. Use separate PDF anchors when recording PDF evidence, as described in [database.md](database.md).

The candidate list does not establish that changed external context is harmless. After inspecting the source diff, select only the candidates for which that conclusion is justified. Submit them through the existing `compare` command:

```json
{
  "expected_snapshot": "NEW",
  "reuse_from": "OLD",
  "changes_reviewed": true,
  "targets": [
    {"collection": "items", "id": "unchanged-result"},
    {"collection": "uses", "id": "unchanged-use"}
  ],
  "reviewer": "overview-author",
  "result": "matched",
  "note": "Reviewed the complete source diff: only the introductory wording changed; hypotheses, notation, numbering, proof passages, and recorded uses are unaffected."
}
```

This note is an example of a finding, not a default conclusion to copy. Use actual snapshot and target IDs from the report. The script rechecks eligibility and records new observations linked to the previous ones. It does not infer the meaning of the edits. If the note cannot be justified, keep the comparisons stale and inspect the necessary context.

For changed records, use ordinary `apply` edits and fresh `compare` batches without `reuse_from`. Do not repeatedly force a rejected reuse batch or replace its baseline merely to obtain a successful receipt. A regular fresh comparison is how a resolved concern is reassessed. Re-run `changes` after edits if using its candidate list for a later reuse batch.

## Finish the revision

Render to the same `<overview-folder>/overview.html`; rendering checks source freshness, comparisons, selected records, and integrity. Use standalone `validate` only when diagnosing a problem or not rendering. A failed candidate preserves the previous report. Keep stale or unreviewed selected records visible as unfinished work; reviewed unresolved interpretations remain explicit limitations. A successful overview has no proof-verification verdict.

For an editorial edit outside mathematical context, the work can be limited to inspecting a small diff, one reuse batch, and regeneration. A substantive assumption or proof change requires checking its actual consequences. No fixed token or cost reduction is promised. Keep full source snapshots and comparison history in the database; save only useful change/edit batches under `work/`, rather than dumping every retrieved packet.
