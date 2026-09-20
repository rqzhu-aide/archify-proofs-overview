# Reusable paper records

Use this workflow when building a native v3 overview, revising it, or keeping its records for later proof checking. SQLite is the authority after initialization. It retains immutable v3 snapshots and deduplicates captured source files. JSON exports are portable snapshots; JSON batches propose changes. Do not maintain independently editable JSON and database masters. V1/v2 records are not read or upgraded.

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

Initialize from a native schema-3 seed using [authoring.md](authoring.md), or from a captured schema-3 export. A seed has `source`, structured statements, and authored source locators; it may contain one result and an empty uses list. A captured export has `source_revision` instead, with its generated evidence and existing observations. Initialization never replaces an existing database or fabricates comparisons. Seed records start unreviewed; imported export observations remain subject to their recorded content digests.

Seed initialization captures source bytes and literal local TeX inputs, including local packages/classes. Use repeatable `--source appendix.tex` on `init` or `refresh` for additional relevant files. If using a PDF for numbering, formulas, or page evidence, register it at initialization with `--source paper.pdf`, before recording comparisons. Adding it later changes the captured source revision and can make those comparisons stale.

Seed source references resolve from the seed JSON's directory. `--source-root` specifies the actual manuscript directory for stored relative paths, extra `--source` files, and future refreshes. A seed under `work/` can therefore point to `../../main.tex` while the stored manuscript path is simply `main.tex`. Without the flag, the seed directory is the source root. Sources outside the chosen manuscript root retain absolute paths. Captured v3 exports retain their source bindings; specify the manuscript root where their relative paths resolve.

`get` returns the selected item, incoming uses, prerequisite statements, their passages, applicable comparisons, source freshness, and `expected_snapshot`. For an item with owned intermediate rows the packet also carries those rows (`owned_items`) and the uses entering them (`owned_step_uses`), because an intermediate step or a step use is reviewed together with its owner; use and prerequisite selection covers the owned steps as well. It excludes full source-file payloads and repeated comparison history; a history count is included, and exports retain the full history. A prerequisite packet helps bounded work, but is not an assertion that those prerequisites suffice. Read further source context whenever the argument needs it. Scope substantive reads and searches to the registered manuscript files; register a needed supplement before relying on it. Build metadata such as `.aux` can supply the numbering map described in [authoring.md](authoring.md), but is not evidence for a mathematical claim.

Packet keys are `item`, `owned_items`, `incoming_uses`, `owned_step_uses`, `prerequisite_items`, `anchors`, `source_revision`, `target_digests`, `observations`, `observation_history_count`, `target_fidelity`, `source_status`, `comparison_status`, and `expected_snapshot`. An item's `passages` entries contain `role` and `anchor_id`; a use's `evidence_refs` is a list of anchor IDs. Join either reference to the packet's top-level `anchors` list by `id` for the excerpt and locator. `target_fidelity` is the selected item's own derived review status (`matched`, `needs_attention`, `stale`, or `unreviewed`), while `comparison_status` is the database-wide aggregate. A packet can report its item matched while other records are stale; check `stale_targets` in `validate` for exactly which.

`validate` reports record integrity separately from source freshness, source comparison, and diagram availability. Resolve malformed records before rendering. Unresolved interpretation, unverified locators, and incomplete comparisons remain visible limitations. The receipt's `stale_targets` lists each stale record's collection and id, agreeing with the stale count; re-read the listed records' source context and re-record their comparisons after an edit cascade. Validation emits one aggregated warning when uses lack located evidence passages ("N uses have no located evidence passages"), and the receipt lists their IDs under `uses_without_evidence`; locate each passage or disclose the gap. Do not call an inventory complete just because validation succeeds.

## Core schema 3 records

| Record | Authored content | Generated or retained bookkeeping |
|---|---|---|
| Item | `kind`, `label`, `caption`, `statement`, `passages`; optional `aliases`, `issue`, `owner` | Stable `id`, independent of printed numbering |
| Use | `from`, `to`, `reason`; optional `type`, `regime`, `issue`, `group`, `evidence_refs` in edit batches | Stable `id`; `type` defaults to `dependency`, evidence defaults to an empty list |
| Anchor | Captured `file_id` and `locator` | `id`, source revision, exact excerpt/hash, locator-check method and limitation |
| Comparison | Targets, reviewer, actual result, concise note | Identity, target-content digest, timestamp |

Items support the seven major mathematical kinds plus `equation`, `claim`, and `derivation`, as described in [authoring.md](authoring.md). An intermediate row requires `owner` naming an existing major row and never becomes an overview node. A statement is `{ "form": "synopsis", "text": "..." }`; use `verbatim` or `transcription` only when that describes the content accurately. The viewer labels synopses and retains captured passages separately.

Each passage is `{ "role": "statement", "anchor_id": "anchor-training-span" }`. Other roles are `proof`, `definition`, and `evidence`. One item can have many passages and aliases. One anchor can serve several items. A label declared in a statement and an equation justified later can have separate anchors without creating extra overview nodes.

For a TeX anchor, use the actual TeX key in `locator.label` when available for mechanical matching; `item.label` contains the printed name. Attach a reviewed PDF page through a separate anchor with the PDF's `file_id` and `locator.page`; do not attach PDF page evidence to a TeX file anchor.

Each use has its own identity. Two uses of the same lemma in one theorem can differ in contribution, proof passage, or regime. Keep those records separate. The viewer currently preserves separate connections and exposes each contribution. Incoming/outgoing lists and geometry are derived, not manually maintained.

A use with at least one intermediate endpoint is a detail use: it refines the argument at an exact step, renders as an annotation inside its owner's detail panel — with its type, regime, reason, and evidence passages — and never enters the graph layout or cycle checks. Both endpoints may be the same intermediate row; the annotation then appears once under that row's owner.

The three use types are `dependency`, `definition`, and `proof_argument`. Applicability belongs in `regime` and the explanation. An optional `group` of `{ "id": ..., "kind": "joint" }` or `"cases"` marks prerequisites required together or alternative proof routes, with one consistent kind per group id. These remain overview annotations, not a formal encoding of joint premises or alternative proof completeness; no group claims that its premises suffice.

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

Author every batch as a file with a file-writing tool, never as an inline shell string: nested shell quoting corrupts backslash math (a `\binom` can degrade into a control character) and the batch is rejected as unparseable or, worse, recorded wrong. With the delivery-folder layout, write `work/edit-01-caption.json` beside `data/paper-records.sqlite`, review the file, then submit it with `apply`. The same rule covers `compare` batches.

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

## Candidate sweeps propose, never record

After the first-pass records exist, run `candidates` when applicable and review every reported missing or unsupported use. Record a use for each genuine missing contribution; locate its evidence or explain an inference after reading the passage. A missing citation does not prove a dependency is unsupported, and adding an "inferred" label merely to silence a warning is not a review. If the paper is PDF-only, use direct manuscript reading and `get`/`compare`, and disclose the absent citation scan.

These commands read one captured snapshot rather than live files and never modify records. Decisions enter through `apply` and `compare`. `scaffold` and `reconcile` remain an optional exhaustive review aid.

```text
python <skill>/scripts/paper_database.py candidates paper-records.sqlite [--output citation-candidates.json]
python <skill>/scripts/paper_database.py scaffold paper-records.sqlite --output work/edge-audit
python <skill>/scripts/paper_database.py reconcile paper-records.sqlite work/edge-audit
```

`candidates` scans the captured TeX sources for `\ref`-family occurrences (`\ref`, `\eqref`, `\autoref`, `\cref`/`\Cref`) whose target label belongs to a recorded row. A captured file counts as a TeX source when its extension is `.tex`/`.ltx`/`.sty`/`.cls` or its decoded content carries a conservative manuscript marker (`\documentclass`, `\newtheorem`, or `\begin{document}`); a `.md` file holding a full manuscript is therefore scanned, and the sniffing is disclosed once per file in the inventory's unresolved notes ("treated as TeX by content: …"). Files without a marker — including prose notes that merely contain an isolated `\begin{equation}` fragment — are never scanned, and PDFs never are: extracted PDF text has no `\ref` commands to find. The label map is built from the dataset's own statement-passage anchors carrying TeX labels; rows without one are listed under `not_mechanically_matchable`, an honest limitation in the same idiom as the inventory's `unresolved`. Each occurrence is classified by its span: inside the statement of a row, inside the proof of a row, or narrative. A proof span is attributed to its row by three rules in priority order: a sectioning command (`\section`/`\subsection`/`\subsubsection`, starred or not) whose title contains "proof of" followed by `\ref{key}` attributes the span through the next same-or-higher heading to `key`'s row — the appendix style where each proof is its own subsection; failing that, a `\label{proof:<suffix>}` (or `proof-<suffix>`) on the heading line attributes the section to the row whose label reduces to `<suffix>` after stripping its own prefix (`proof:fixed_point` → `thm:fixed_point`), with ambiguous suffixes left unattributed; failing both, the whitespace-adjacency fallback attributes a `proof` environment to the declaration it directly follows. A heading attribution outranks adjacency when both could claim the same proof, and the heading's own `\ref` key is a self-citation that never forms a pair. When these rules leave an occurrence unattributed, including paragraph headings and plain prose, the rows' own recorded proof-role passage ranges can attribute it when exactly one row's proof passage contains the line and no declaration or statement context claims it. Recovered occurrences carry `attribution: "proof_passage"`; overlapping, contradictory, or ambiguous ownership stays on `unattributed_occurrences`, visible rather than guessed. Occurrences outside every attributed span — mostly main-text narrative or unmarked proof sections — stay on `unattributed_occurrences` and are never used to form pairs; the report's `attribution` object counts `attributed` versus `unattributed` matched occurrences (their sum is `counts.occurrences`), and the note calls out the unattributed share when there is one. The report lists every citing/cited pair with line numbers and context, and derives two review lists: `missing_uses` (pairs with occurrences but no recorded use from cited to citing) and `unsupported_uses` (recorded uses with no citing passage whose reason or issue does not say "inferred" — uses not corroborated by this scan, not false dependencies). A citation is evidence to review, not a recorded use; contribution, type, and regime require reading.

Every report carries a `coverage` object: `files_scanned` (captured files treated as TeX), `text_files_total`, `tex_by_content` (files sniffed rather than suffixed), and `applicable`. When `files_scanned` is zero — a PDF-only paper, or text sources without a manuscript marker — the report sets `applicable: false`, nulls `pairs`, `missing_uses`, `unsupported_uses`, `counts`, and `attribution` (zero-valued lists would look like a clean bill), and its note discloses the coverage limit: review the recorded rows through `get` packets and `compare` batches. The `validate` and `changes` receipts mirror this: an applicable scan shows compact counts under `citation_candidates`, including `pairs`, `not_mechanically_matchable`, and the `attributed` and `unattributed` shares, and an inapplicable one shows `"not applicable (no TeX sources captured)"` instead of counts. The report is snapshot-bound, and `refresh` makes a stale report visible through those receipts.

The `scaffold`/`reconcile` pair below is an **optional deep review**, not a required close-out sweep. The default dependency workflow is the per-argument one: statement-premise checks, `candidates`, `get` packets, and `compare` batches. Use the edge audit when a target's full incoming coverage should be disposed explicitly — it forces every other row to be depended on or dismissed, and it reports recorded uses that no disposition covers.

`scaffold` writes one audit file per major row into a fresh folder, pre-filled with the row's packet and the complete `candidates_considered` list of every other row, major or intermediate. It never overwrites an existing file, because a filled audit is agent work. Fill each file by disposing of every candidate exactly once: `depends_on` entries take the candidate `id` with either the optional pair-level `type`, `regime`, `reason`, and `evidence_refs` (existing anchor ids), or a `contributions` list — never both. Each contribution is either `{"use_id": ...}` naming one recorded use from that candidate to the target (establishing exactly that use), or a proposed new use carrying at least one of `type`, `regime`, `reason`, `evidence_refs`. Use `contributions` whenever two recorded uses share a pair, when a use should be matched by identity rather than by type/regime, or when one candidate contributes in several distinguishable ways; contributions with the same endpoints never merge. `dismissed` entries take the `id` with an optional `note`.

`reconcile` validates filled audits (every candidate disposed exactly once, real row/anchor/use ids, the current `expected_snapshot`) and compares them with recorded uses. It reports a **missing-edge candidate** for a proposed use with no counterpart, a **suspect edge** for a recorded pair that was dismissed, an **agreement** for an unambiguous pair-level entry or a contribution's named use, a **refinement** when type or regime differ, and an **ambiguous entry** when several uses match a pair-level entry. Resolve ambiguity by naming use IDs under `contributions`; ambiguous matches stay out of the comparison skeleton. Every incoming recorded use that is neither established nor suspect remains under **`unresolved_uses`**, including uses cited by a refinement or ambiguity. Wording differences in `reason` stay with the reviewer. For agreements, the output includes a `compare` skeleton with the current snapshot; add the reviewer and a note describing the actual source comparison. Reconcile writes nothing to the database.

## Paper changes, source locations, and history

```text
python <skill>/scripts/paper_database.py refresh paper-records.sqlite --expected-snapshot SNAPSHOT
python <skill>/scripts/paper_database.py export paper-records.sqlite paper-records.json
python <skill>/scripts/paper_database.py render paper-records.sqlite earlier-overview.html --snapshot SNAPSHOT
python <skill>/scripts/paper_database.py backup paper-records.sqlite paper-records-backup.sqlite
```

Refresh captures a new source version and rebinds locators at their entered positions. It does not claim that unchanged line numbers still contain the intended theorem. Inspect changed excerpts and correct anchors and interpretations through an edit batch. If a range no longer exists, refresh stops with the affected anchor and preserves the current database. Supply corrected bindings with `refresh --anchors reanchors.json`; capture and relocation then occur in the same atomic update. Never edit the manuscript merely to make a locator fit.

The relocation file maps existing anchor IDs to their new locators, for example `{ "anchor-appendix-proof": { "locator": {"start_line": 31, "end_line": 39} } }`. Add `file_id` to that object when explicitly relocating to another registered file. Identities and earlier bindings are retained; the script extracts the new passage and makes outdated comparisons stale.

If moving the manuscript, use `refresh --source-root <new-paper-folder>`; relative source paths are resolved there. Historical snapshots retain exact source bytes and earlier bindings. The `export` receipt prints the exported snapshot ID, and the file carries the same ID. The receipt also reports the observation count and comparison counts derived from the exact exported data: a comparison appended after the last export moves those counts even when the snapshot ID is unchanged, so regenerate a retained export after the final comparison. A historical export can be rendered even when live files are missing or changed, with that limitation visibly stated. Labels and physical PDF pages are distinguished from mechanically checked line ranges. PDF page bounds are checked when shared `pypdf` is available; content and printed numbering still need comparison with the PDF.

The export's `inventory` block reports literal theorem-like declarations matched against item anchors, any authored exclusions, and unresolved input forms. It is a candidate list. Custom theorem macros, conditional TeX, unnumbered results, and proof dependencies still require reading. No completeness or mathematical judgment follows from the scanner.

Use `backup` for a consistent copy of an active database. The database holds captured manuscript content; share it only when that source material should also be shared. To share only the finished overview, send the HTML alone. It includes selected source excerpts and the viewer, and needs neither JSON nor SQLite beside it.

## Delivery checks and limits

The renderer receives one fixed projection. Before replacing an existing HTML file, the wrapper checks the exact input/output hashes, embedded record content, actual SVG node/use identities and endpoints, the complete statement/use index, and the renderer's selected geometry checks. A failure preserves the earlier report. Its preservation does not mean a new report was produced.

Cyclic or self-referential mappings remain in the dataset. Until the graph layout supports them, delivery uses a complete index with an explanation. A cycle can arise from alternate regimes or an interpretation error and is not itself a verdict of circular proof.

Generated receipts distinguish these mechanical checks from browser inspection and mathematical assessment. Inspect browser behavior and readability when possible and report what was actually checked. Geometry checks do not establish that every label or dense connection is perceptually clear. Source hashes and graph traversal do not establish proof validity, repair difficulty, or the sufficiency of a set of premises.
