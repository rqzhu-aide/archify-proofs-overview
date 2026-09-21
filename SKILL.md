---
name: archify-proofs-overview
description: Build a selective, source-backed interactive map of a paper's main results, important prerequisites, and their connections. Use to understand argument structure and navigate results, not to reconstruct detailed proof steps or verify proofs.
metadata:
  version: "3.0.3"
---

# Archify Proofs Overview

Deliver a standalone HTML graph explaining the structure of the paper's written argument. Select its main results, the important assumptions, definitions, and supporting results that explain them, and sourced connections between those statements. Preserve essential qualifications in concise summaries. Detailed derivations and mathematical validity checks belong to proofcheck.

## Select and build

1. **Orient and select.** Read the introduction, main results, and relevant source context. Start from the central contributions or the results the user names; follow important prerequisite chains as far as needed to explain them. State the selected scope and material omissions. Ask about scope only when an unresolved choice would materially change the requested overview. A declaration scan aids discovery, not an obligation to represent every declaration.
2. **Record statements.** Read [authoring.md](references/authoring.md) and [database.md](references/database.md). Initialize with `init --focused`, explicit `main_items`, and the supplied source files. Preserve essential conditions from the statement and applicable surrounding setup. Use one identity per selected statement and its manuscript declaration type. All selected statements remain visible; main-result selection highlights roles without filtering the graph. Do not author intermediate proof-step records, owners, or formal use groups.
3. **Record connections.** Explain what the prerequisite supplies and where the target uses it, with any essential restriction. Distinguish a stated condition or conclusion, a definition, and a reused proof argument. A citation alone is not a dependency. Do not collapse omitted derivations into an unsupported direct arrow. Explain joint requirements, alternative routes, and relevant cycles briefly in the connection reasons or scope.
4. **Compare and refine.** Compare the current saved summaries and connection reasons with their sources and applicable setup in bounded groups, reusing recently read unchanged context. Check for omitted qualifications and unsupported claims in that wording; a correct review note cannot repair an inaccurate record. Correct affected connected explanations together. Record only the comparisons actually performed. Use `candidates` when a connection needs investigation; zero scanner warnings and exhaustive manuscript coverage are not completion requirements.
5. **Render and deliver.** Render a representative early draft when useful to catch math or label problems, then render the final database after edits and comparisons. Keep specific unresolved interpretations visible. A cycle can remain with an explanation; do not invent intermediate nodes or change genuine directions to remove it.

## Durable output and continuation

For a new overview, default to this folder beside the manuscript, unless the user chooses another destination:

```text
archify-proofs-overview-<short-paper-name>/
  overview.html
  data/paper-records.sqlite
  work/                       optional seed and edit batches
  exports/                    optional portable snapshots
```

Register the actual manuscript directory with `--source-root`. Scripts and viewer assets stay in the shared skill installation. After initialization the database is authoritative; seeds and exports are not parallel masters. Reuse the database, stable identities, and output folder for revisions, following [revisions.md](references/revisions.md).

```text
python <skill>/scripts/paper_database.py render <overview-folder>/data/paper-records.sqlite <overview-folder>/overview.html
```

Rendering checks integrity, source status, comparisons, record preservation, and geometry. An unchanged successful render needs no extra `validate`. Regenerate a retained JSON export after final comparisons, because observations can change without changing the mathematical snapshot ID.

Completion means the selected statements and connections are represented, source comparisons are current or explicitly unresolved, and the HTML faithfully displays them. Unreviewed or stale selected records still need attention; a reviewed `needs_attention` record can be delivered as unresolved. Source comparison does not establish proof correctness. Foreground the overview, selected scope, actual inspection, and material limitations in the delivery message; routine counts remain available in the artifact and receipts.

When a browser is available, inspect a long statement, several prerequisites, search/selection, and any cycle explanation. After one failed attempt and one reasonable fallback, disclose untested behavior and stop unless browser repair was requested. Static checks do not establish interaction correctness.

Existing detailed databases remain readable and retain their records and history. Do not auto-convert or prune them. Read [audit-database.md](references/audit-database.md) only for rich-record compatibility or a requested proofcheck handoff. A selective overview is a starting inventory for an audit, not an exhaustive proof inventory.

Use shared Python, `latex2mathml`, Node.js, and `pypdf` when PDF input needs it. Identify missing shared dependencies rather than creating a project-local environment. Keep paper reasoning in records and JSON batches; do not generate a custom bookkeeping program. The bundled representer-theorem example illustrates the format, never evidence about the user's paper.
