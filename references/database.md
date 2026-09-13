# Reusable paper records

Use this workflow when building a reusable overview, continuing an existing map, or keeping records for later proof checking. SQLite is the authority after explicit initialization. It retains immutable content snapshots and deduplicates captured source files. JSON exports are portable snapshots; JSON batches propose changes. Do not maintain independently editable JSON and database masters.

The current release records source interpretation and comparison, not detailed proof-audit findings or verdicts. Detailed proof units can be added later under these stable major items.

For a revised manuscript, see [revisions.md](revisions.md) for change inspection, exact passage relocation, and reviewed reuse. Keep the existing database and output folder across revisions.

## Start and continue

Use the shared Python interpreter and `<skill>/scripts/paper_database.py` for these commands. No additional database package or server is needed. New overviews use `archify-proofs-overview-<short-paper-name>/overview.html` and `data/paper-records.sqlite` within that directory. The short database paths below are illustrative; pass the actual `data/` path when running them.

```text
python <skill>/scripts/paper_database.py init paper-records.sqlite paper-proof-overview.json --source-root <manuscript-folder>
python <skill>/scripts/paper_database.py get paper-records.sqlite representer
python <skill>/scripts/paper_database.py apply paper-records.sqlite edits.json
python <skill>/scripts/paper_database.py compare paper-records.sqlite comparisons.json
python <skill>/scripts/paper_database.py validate paper-records.sqlite
python <skill>/scripts/paper_database.py render paper-records.sqlite paper-proof-overview.html
```

Initialize from existing schema 1 JSON or a small seed using [authoring.md](authoring.md). The seed may contain one result and an empty uses list; add the remaining records incrementally. Initialization never replaces an existing database and never marks imported records as compared. It captures source bytes and literal local TeX inputs, including local packages/classes. Use repeatable `--source appendix.tex` on `init` or `refresh` for additional relevant files. If using a PDF for numbering, formulas, or page evidence, register it at initialization with `--source paper.pdf`, before recording comparisons. Adding it later changes the captured source revision and can make those comparisons stale.

Schema 1 source references resolve from the seed JSON's directory. `--source-root` specifies the actual manuscript directory for stored relative paths, extra `--source` files, and future refreshes. A seed under `work/` can therefore point to `../../main.tex` while the stored manuscript path is simply `main.tex`. Without the flag, the seed directory remains the source root for compatibility. Sources outside the chosen manuscript root retain absolute paths. Schema 2 imports retain their existing source bindings; specify the root where their relative paths currently resolve.

`get` returns the selected item, incoming uses, prerequisite statements, their passages, applicable comparisons, source freshness, and `expected_snapshot`. It excludes full source-file payloads and repeated comparison history; a history count is included, and exports retain the full history. A prerequisite packet helps bounded work, but is not an assertion that those prerequisites suffice. Read further source context whenever the argument needs it. Scope substantive reads and searches to the registered manuscript files; register a needed supplement before relying on it. Build metadata such as `.aux` can supply the numbering map described in [authoring.md](authoring.md), but is not evidence for a mathematical claim.

`validate` reports record integrity separately from source freshness, source comparison, and diagram availability. Resolve malformed records before rendering. Unresolved interpretation, unverified locators, and incomplete comparisons remain visible limitations. Do not call an inventory complete just because validation succeeds.

## Core schema 2 records

| Record | Authored content | Generated or retained bookkeeping |
|---|---|---|
| Item | `kind`, `label`, `caption`, `statement`, `passages`; optional `aliases`, `uncertainty` | Stable `id`, independent of printed numbering |
| Use | `from`, `to`, `reason`; optional `type`, `regime`, `uncertainty`, `evidence_refs` in edit batches | Stable `id`; `type` defaults to `dependency`, evidence defaults to an empty list |
| Anchor | Captured `file_id` and `locator` | `id`, source revision, exact excerpt/hash, locator-check method and limitation |
| Comparison | Targets, reviewer, actual result, concise note | Identity, target-content digest, timestamp |

Items support the same seven mathematical kinds as schema 1. A statement is `{ "form": "synopsis", "text": "..." }`; use `verbatim` or `transcription` only when that describes the content accurately. The viewer labels synopses and retains captured passages separately.

Each passage is `{ "role": "statement", "anchor_id": "anchor-training-span" }`. Other roles are `proof`, `definition`, and `evidence`. One item can have many passages and aliases. One anchor can serve several items. A label declared in a statement and an equation justified later can have separate anchors without creating extra overview nodes.

For a TeX anchor, use the actual TeX key in `locator.label` when available for mechanical matching; `item.label` contains the printed name. Attach a reviewed PDF page through a separate anchor with the PDF's `file_id` and `locator.page`; do not attach PDF page evidence to a TeX file anchor.

Each use has its own identity. Two uses of the same lemma in one theorem can differ in contribution, proof passage, or regime. Keep those records separate. The viewer currently preserves separate connections and exposes each contribution. Incoming/outgoing lists and geometry are derived, not manually maintained.

The three use types are `dependency`, `definition`, and `proof_argument`. Applicability belongs in `regime` and the explanation. This is an overview annotation, not a formal encoding of joint premises or alternative proof completeness.

## Apply a small edit batch

Use `expected_snapshot` from the packet you read. `upsert` provides the complete intended record, rather than silently merging unspecified mathematical fields. `id` need not be repeated inside `record`.

```json
{
  "expected_snapshot": "COPY_THE_PACKET_SNAPSHOT",
  "edits": [
    {
      "collection": "anchors",
      "op": "upsert",
      "id": "anchor-appendix-proof",
      "record": {
        "file_id": "COPY_REGISTERED_FILE_ID",
        "locator": {"start_line": 40, "end_line": 49}
      }
    },
    {
      "collection": "uses",
      "op": "upsert",
      "id": "use-projection-representer",
      "record": {
        "from": "projection",
        "to": "representer",
        "reason": "Projection preserves the training evaluations and removes the orthogonal contribution to the norm.",
        "evidence_refs": ["anchor-appendix-proof"]
      }
    }
  ]
}
```

These identifiers and line numbers are examples, not source evidence. Reuse actual record/file IDs and verified locators. Anchor shorthand makes the script extract and hash the passage. Do not hand-write source hashes, timestamps, generated comparison IDs, or duplicated excerpts. Register a missing file before referring to it.

For an item, supply `kind`, `label`, `caption`, structured `statement`, and `passages`. Renumbering changes `label`, not the identity. Splitting or merging real statements requires an explicit mapping of their items and uses. `remove` needs an existing identity and no `record`; update affected links in the same batch.

Optional `set` changes `title`, `scope`, or `main_items`. Set `main_items` to `null` to restore automatic terminal-result navigation. Use `set.inventory.excluded` for concise, explicit scope exclusions; declaration matches are regenerated from captured sources and item anchors. An exclusion note does not mechanically prove that every unmatched declaration is accounted for.

The whole batch is validated and applied atomically. A stale `expected_snapshot` is rejected with the current snapshot. Retrieve the updated records and revise the batch; do not replace the expectation without reviewing the intervening changes. Keep batches bounded to the argument being handled.

## Record the comparison you actually performed

```json
{
  "expected_snapshot": "COPY_THE_CURRENT_SNAPSHOT",
  "targets": [
    {"collection": "items", "id": "representer"},
    {"collection": "uses", "id": "use-projection-representer"}
  ],
  "reviewer": "overview-author",
  "result": "matched",
  "note": "Compared the target, projection argument, and stated hypotheses with the cited passages."
}
```

Use `needs_attention` when source fidelity is unresolved. Comparisons can cover a bounded group in one operation. They reference existing items/uses, not another authored prerequisite list. A command cannot decide that a comparison happened: the agent must read the evidence first. `matched` means the recorded synopsis/use agrees with the reviewed source, not that the theorem is proved.

An item comparison includes its incoming uses and prerequisite statements. Changing those records invalidates its earlier comparison. Source changes conservatively invalidate comparisons through the source revision, including macro or hypothesis changes outside the anchored excerpt. A reviewed reuse batch can carry an eligible prior comparison to the new version, with a generated `carried_from` link; see [revisions.md](revisions.md). Neither refresh nor a changes report performs that review automatically. Appending comparisons or build history does not change the mathematical record snapshot. Historical views use the newest comparison matching their exact content, rather than being made stale by a later comparison of a different version. A newer `needs_attention` observation for that same input takes precedence over an earlier `matched` one. Routine comparison command output is a concise receipt; full observations remain in the database.

## Paper changes, source locations, and history

```text
python <skill>/scripts/paper_database.py refresh paper-records.sqlite --expected-snapshot SNAPSHOT
python <skill>/scripts/paper_database.py export paper-records.sqlite paper-records.json
python <skill>/scripts/paper_database.py render paper-records.sqlite earlier-overview.html --snapshot SNAPSHOT
python <skill>/scripts/paper_database.py backup paper-records.sqlite paper-records-backup.sqlite
```

Refresh captures a new source version and rebinds locators at their entered positions. It does not claim that unchanged line numbers still contain the intended theorem. Inspect changed excerpts and correct anchors and interpretations through an edit batch. If a range no longer exists, refresh stops with the affected anchor and preserves the current database. Supply corrected bindings with `refresh --anchors reanchors.json`; capture and relocation then occur in the same atomic update. Never edit the manuscript merely to make a locator fit.

The relocation file maps existing anchor IDs to their new locators, for example `{ "anchor-appendix-proof": { "locator": {"start_line": 31, "end_line": 39} } }`. Add `file_id` to that object when explicitly relocating to another registered file. Identities and earlier bindings are retained; the script extracts the new passage and makes outdated comparisons stale.

If moving the manuscript, use `refresh --source-root <new-paper-folder>`; relative source paths are resolved there. Historical snapshots retain exact source bytes and earlier bindings. A historical export can be rendered even when live files are missing or changed, with that limitation visibly stated. Labels and physical PDF pages are distinguished from mechanically checked line ranges. PDF page bounds are checked when shared `pypdf` is available; content and printed numbering still need comparison with the PDF.

The inventory helper reports literal theorem-like declarations and unresolved input forms. It is a candidate list. Custom theorem macros, conditional TeX, unnumbered results, and proof dependencies still require reading. No completeness or mathematical judgment follows from the scanner.

Use `backup` for a consistent copy of an active database. The database holds captured manuscript content; share it only when that source material should also be shared. To share only the finished overview, send the HTML alone. It includes selected source excerpts and the viewer, and needs neither JSON nor SQLite beside it.

## Delivery checks and limits

The renderer receives one fixed projection. Before replacing an existing HTML file, the wrapper checks the exact input/output hashes, embedded record content, actual SVG node/use identities and endpoints, the complete statement/use index, and the renderer's selected geometry checks. A failure preserves the earlier report. Its preservation does not mean a new report was produced.

Cyclic or self-referential mappings remain in the dataset. Until the graph layout supports them, delivery uses a complete index with an explanation. A cycle can arise from alternate regimes or an interpretation error and is not itself a verdict of circular proof.

Generated receipts distinguish these mechanical checks from browser inspection and mathematical assessment. Inspect browser behavior and readability when possible and report what was actually checked. Geometry checks do not establish that every label or dense connection is perceptually clear. Source hashes and graph traversal do not establish proof validity, repair difficulty, or the sufficiency of a set of premises.
