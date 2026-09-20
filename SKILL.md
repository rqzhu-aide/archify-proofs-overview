---
name: archify-proofs-overview
description: Build an interactive HTML overview of a paper's main assumptions, definitions, lemmas, propositions, theorems, corollaries, and their dependencies. Use for understanding proof organization and navigating major results, not for full mathematical proof verification.
metadata:
  version: "3.0.0"
---

# Archify Proofs Overview

Create a compact, source-backed map of the paper's major mathematical results using the adapted Archify viewer. Record the paper in a native schema-3 database and deliver a standalone HTML overview. This release accepts native v3 seeds and captured exports only. The map explains the written argument; it is not a certificate that the proofs are valid.

When revising an existing v3 overview, reuse its database and follow [references/revisions.md](references/revisions.md). Inspect the source changes and affected records instead of restarting the paper inventory. Do not move a v3 project or create another database solely to adopt the naming convention below.

## Delivery folder

For a new overview, default to `archify-proofs-overview-<short-paper-name>/` under the paper's project/output directory, unless the user specifies another destination. Choose a short, stable name and reuse it when the paper title or version changes.

```text
archify-proofs-overview-papername/
  overview.html
  data/paper-records.sqlite
  work/                       optional seed and edit batches
  exports/                    optional portable JSON exports
```

Keep `overview.html` at that folder's root. Create optional folders only when needed. Scripts and viewer assets remain in the shared skill installation; do not copy them into each paper's output. Register the actual manuscript directory with `init --source-root`, independently of where the seed JSON or database sits. This keeps source paths usable when the whole paper project moves.

## Workflow

1. Read the supplied paper and its proof sections. Use TeX labels and source passages when available; inspect the PDF for printed numbering, pages, and formulas. Use existing shared PDF tools rather than building a new extraction pipeline.
2. Identify the major assumptions, definitions, lemmas, propositions, theorems, and corollaries within the requested scope. The declaration scan proposes candidates; also record an explicitly located mathematical component found by reading — a global assumption or model definition written in ordinary prose — with a faithful descriptive label and source passage, never an invented theorem number. Include an external result when it materially explains a proof. Keep intermediate algebra and routine proof steps inside the item descriptions, not as additional overview nodes. Create an intermediate row (`equation`, `claim`, or `derivation`) only on demand, when a review must address an exact step; it requires an `owner` naming the major row whose statement or proof contains it, and it never becomes an overview node. If no major row owns the content, disclose the step in `scope` instead of inventing a parent.
3. Follow [references/database.md](references/database.md) for source registration, bounded retrieval, record edits, and comparison batches. Initialize from a small native v3 seed following [references/authoring.md](references/authoring.md) — the bundled [seed.json](examples/representer-theorem/seed.json) is one complete minimal example — or import a captured v3 export. Scripts capture source files and generate anchors, excerpts, and hashes. The database becomes authoritative after initialization; the seed is not a second master. Register separate appendices or local macro files when literal TeX input discovery cannot find them.
4. Store one item with stable identity and multiple statement/proof passages. Record actual uses with independent IDs, even when their endpoints match. Read relevant statements and proof passages, then submit small JSON edit batches. Scripts generate source hashes, excerpts, revisions, and declaration candidates. They do not infer proof dependencies from citations. Retrieve the relevant item packet rather than repeatedly dumping the database or all captured source files.
5. Validate and render the draft once the inventory and a few dependencies exist. Inspect the receipt's `math_diagnostics`, which identify the record, field, excerpt, and rejection reason, and fix supported notation before recording a large batch of comparisons. Faithful unsupported expressions may remain as labeled LaTeX with the display limitation disclosed; unresolved mathematical interpretation requires `needs_attention`. The draft HTML keeps its unreviewed status. Then compare summaries and each actual use against the manuscript through bounded `get` packets and `compare` batches. Check explicit premises separately from citations, preserving deterministic versus random quantities, conditioning, hypotheses, quantifiers, and the exact conclusion. Run `candidates` after the first pass when applicable and resolve its review lists as described in [references/database.md](references/database.md#candidate-sweeps-propose-never-record). For PDF-only papers, read the passages directly and disclose the scan limitation. Exhaustive `scaffold`/`reconcile` review is optional. Check inventory coverage, source freshness, labels, disconnected results, and unresolved questions; locate missing use evidence or disclose the gap. Record comparisons only after reading the relevant context.
6. Validate and render a consistent database snapshot:

   ```text
   python <skill>/scripts/paper_database.py validate <overview-folder>/data/paper-records.sqlite
   python <skill>/scripts/paper_database.py render <overview-folder>/data/paper-records.sqlite <overview-folder>/overview.html
   ```

7. Inspect the HTML when a browser is available, including the longest mathematical statement, selection/search, and a result with several prerequisites. The script checks actual item/use preservation and selected geometry; its receipt does not claim browser or mathematical review. State actual inspection and remaining limitations in the delivery message. The delivery message also discloses the fidelity status counts from the validate or render receipt (`source_comparison`: matched, needs_attention, stale, unreviewed), so the reader sees how much of the map was actually compared against the source. Render after the final comparison; when an export is kept, regenerate it afterwards and check its receipt's observation and comparison counts, which move even when the snapshot ID does not. Deliver the HTML at the overview folder's root and keep the authoritative database under `data/`. The HTML is shareable by itself; JSON exports are optional.

When a v3 overview is going on into a proof audit, use the handoff described in [references/audit-database.md](references/audit-database.md). `paper_audit.py migrate-overview` converts the v3 overview database to the audit store with a verified backup. It carries authored records and currently applicable comparisons; other comparison history remains in the archived overview export. Use the audit store as the authority afterwards and render it with `checkpoint`. The same reference covers reading a database produced by proofcheck.

Use the bundled representer-theorem example only to learn the format, never as evidence about the user's paper.

The native record fields are detailed in [references/authoring.md](references/authoring.md): `owner` ties an intermediate row to the major row whose statement or proof contains it; a use `group` of `{"id", "kind"}` marks `joint` prerequisites or alternative `cases`, disclosed with a badge and never as a sufficiency claim; and `issue` records one specific open question.

## Meaning and presentation

- Each arrow runs from a prerequisite to a result that uses it. Record the actual use, not merely the presence of a citation. Several inputs may be required together; an arrow alone does not mean that one prerequisite is sufficient.
- A combined cycle may reflect alternative arguments or a mapping problem. Keep the records and explain the limitation. The renderer provides a complete index when its DAG layout cannot draw the mapping. Do not remove an actual dependency or change the manuscript to satisfy layout.
- Distinguish a dependency on a stated result (`dependency`), use of a definition (`definition`), and reuse of an argument inside another proof (`proof_argument`). A `regime` names an applicability condition or alternative proof route; it does not mark uncertainty. Explain the contribution and qualification in `reason` as well.
- Preserve the paper's hypotheses, quantifiers, and conclusions in the detail text. Keep the visible node short: a manuscript label such as “Thm 1.2” and a brief caption. Hover or focus gives a short preview; selecting a node exposes its full statement, location, and connected uses.
- Let the introduction reuse reviewed main-result labels and captions. An optional, nonempty `main_items` selection changes the entry point to a large overview, not its inventory or mathematical content. Omit it to use the viewer's automatic terminal-result selection. Keep every item in the selected scope accessible; do not add a second set of summary claims to maintain.
- Node colors identify mathematical types, using the renderer's palette without red, green, or yellow. Neutral connections are the default. Do not assign correctness colors from extraction alone.
- State any selected scope, omitted sections, and unresolved source or dependency questions explicitly. Claim a complete inventory only after comparing the list against the paper's result declarations and relevant proof sections. Inventory coverage does not establish proof validity.
- Keep uncertain interpretations visible in the dataset and report. Never invent theorem numbers, PDF pages, source lines, dependencies, or proof judgments to fill a field. Explain a mapping limitation instead of changing manuscript content to accommodate the renderer.

Use shared Python with its standard `sqlite3` module, `latex2mathml` for offline MathML, and shared Node.js for the bundled viewer. If the converter is missing, use the user's shared Python executable with `-m pip install --user latex2mathml`. If Node.js is unavailable, identify or request a shared installation. Do not create a project-local environment. Keep paper-specific reasoning in records and JSON edit batches; do not generate a custom Python bookkeeping or rendering program for each paper.
