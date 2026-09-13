# Author a schema 1 seed or one-off JSON overview

This is the compact legacy format, still supported for initial authoring and one-off rendering. For reusable records, multiple proof passages, distinct parallel uses, and retained source comparisons, initialize the database and follow [database.md](database.md). After initialization the database is authoritative; this JSON is a seed import. The validator checks record integrity, not mathematical truth or completeness.

Build a small seed from the fields below and the actual paper. The optional [representer theorem example](../examples/representer-theorem/proof.md) and its [finished schema 2 records](../examples/representer-theorem/overview.json) illustrate source-backed authoring and delivery; the finished export is not a schema 1 seed to copy wholesale. Use meaningful stable internal identifiers such as `uniform-remainder`, independent of the printed labels shown to readers.

## Minimal records

| Record | Required content | Purpose |
|---|---|---|
| Dataset | `schema_version: 1`, `title`, `scope`, `source`, `items`, `uses`; optional `main_items` | Names the paper and makes the selected scope explicit |
| Paper `source` | `title`; optional `file` | Identifies the paper/source artifact or supplied excerpt |
| Item | `id`, `kind`, `label`, `caption`, `statement`, `source` | Represents one major mathematical item |
| Use | `from`, `to`, `reason`; optional `source`, `type`, `regime` | Explains how and when the target uses the prerequisite |

Supported item kinds are `assumption`, `definition`, `lemma`, `proposition`, `theorem`, `corollary`, and `external_result`. Do not create a node for every equation or proof paragraph. A crucial unnumbered result may use a descriptive label such as “Uniform remainder bound”; do not manufacture a manuscript number for it.

Represent one physical statement once, even if it has several TeX labels or its proof appears elsewhere. Use the reader's printed name for `label`, and identify any relevant aliases or proof passages in source locators and use reasons. A second label does not create a second mathematical result.

`label` is the compact reader-facing name, for example “Thm 2.3”. `caption` is a short explanation such as “Asymptotic normality”. `statement` contains the mathematical detail, including conditions essential to the result. For a lengthy statement, preserve its meaning in a clearly identified synopsis and point to the exact source for the full statement.

An item `source` identifies where that item is stated. A use `source`, when available, identifies the proof passage establishing that particular use. Source fields are `file` (inherits the paper's file if omitted), `start_line` and `end_line` as a pair, `page` for a reviewed physical PDF page, and `label` for a manuscript label or section reference. When a TeX key exists, use it in `source.label` for mechanical matching; keep the printed name in the item's `label`. Descriptive locators for unlabeled material remain supported but require source comparison. Provide a meaningful locator rather than an empty object. A source label can locate material when a verified page or line range is unavailable. Paper file paths should identify the actual supplied artifacts; the root file may be omitted for a supplied excerpt identified by its title. Do not estimate PDF pages from TeX line numbers.

Relative source file paths resolve from the JSON dataset's directory, not the current terminal directory or skill directory.

For a larger overview, optional `main_items` is a nonempty list of unique existing item IDs identifying the main results after reading the paper, for example `"main_items": ["normality", "band-coverage"]`. The viewer reuses their labels and captions as the entry point; do not write another set of summary statements. Keep the complete selected inventory in `items` and its actual connections in `uses`. Choosing main results does not establish that their prerequisites have been proved. Omit `main_items` to use the viewer's automatic terminal-result selection; an empty list is invalid. This fallback identifies endpoints in the mapped argument and does not infer scientific importance.

## Dependencies

Store each use once. Outgoing and incoming lists, graph layout, and neighbor highlights are computed by the renderer.

For a theorem requiring an assumption and two lemmas, make three use records whose `to` is that theorem. Explain the contribution of each prerequisite in `reason`. These are dependencies of the written argument, not three independent proofs of the theorem. Check the target's explicit premises separately from its proof citations: an explicitly imposed assumption deserves a direct use even if it is also reachable through a lemma. Do not add other transitive arrows merely because a dependency can be reached through another item.

A useful reason is “Projection preserves the training evaluations while removing the orthogonal contribution to the norm.” “Used in the theorem” is too vague. A general assumption applies to every result only when the paper says so or the proof actually uses it; avoid connecting every assumption to every theorem by default.

Use optional `type` only to distinguish the contribution of an arrow:

| Use type | Meaning |
|---|---|
| `dependency` (default) | The target relies on the source's stated condition or conclusion |
| `definition` | The target uses the source's definition or construction |
| `proof_argument` | The target reuses an argument inside the source's proof, rather than applying its stated conclusion |

For `proof_argument`, identify the particular bound or argument in `reason` and locate its proof passage when available. Check which conditions that argument needs at the target and state them in the target's synopsis and use reason; this edge does not import the source theorem's hypotheses wholesale. A theorem's ratio conclusion and an absolute-error estimate inside its proof are different contributions. A citation alone does not establish either kind of use.

Distinguish stated and inferred links in the reason text. For example, “Inferred from the substitution in the proof of Thm 2.3; no explicit lemma citation” makes the evidence level clear. Use an optional `uncertainty` string on an item or use to identify a specific extraction or dependency question. The renderer discloses uncertainty and distinguishes uncertain links; it does not assign a correctness judgment. Omit a speculative link when there is no source basis for it and describe the missing evidence in `scope`. Do not silently delete an apparent cycle; inspect the source and explain unresolved circularity or a representation limitation.

Use optional `regime` for a short applicability qualifier such as `"pilot-trained centers"` or `"Route B"`, and explain the condition fully in `reason`. Use the same name consistently for the same route. A known alternative regime is not an `uncertainty`. Arrows without a regime are unqualified uses in the selected argument. The displayed qualifiers do not encode a formal logic of alternative proofs: if the grouping remains ambiguous, explain it in `scope` or select and name one route. Do not imply that all incoming arrows are jointly required across alternative proofs.

For example, a use may have `"type": "proof_argument"` and `"regime": "pilot-trained centers"` when that case reuses a bound from another proof. These optional fields remain part of schema version 1; older datasets continue to work and their uses default to `dependency`.

## Source fidelity and scope

Read each included result's statement and enough of its proof to identify its major uses. Check declarations in appendices and supplements before saying that the overview covers the whole supplied paper. Otherwise describe the selection, for example “Main results of Sections 2 to 4; auxiliary appendix lemmas omitted.” Identify unreadable formulas, missing supplements, and unresolved numbering in the same scope description.

Compare every statement or synopsis against its source before delivery. In particular, retain whether quantities are deterministic or random, what is conditioned on, all essential hypotheses and quantifiers, and the precise target, normalization, and mode of convergence. Compare each use against the cited proof passage: check what is actually borrowed and whether it applies only under one regime. Correct the same JSON records when this comparison exposes an omission; no separate review ledger is required. A validator cannot detect an omitted mathematical condition merely because the remaining record is well formed.

A graph of roughly 8 to 12 major items is often easy to read, but this is a presentation target, not a limit. Retain all selected results. Use an explicitly narrower scope when appropriate; never hide results solely to reach a node count.

Keep exact manuscript labels distinct from descriptive captions. When available, use the current compiled `.aux` to map TeX keys to printed theorem and equation numbers, checking the mapping against the corresponding PDF. Do not infer numbering by counting theorem environments or displayed formulas; leave unresolved labels explicit. An external result should identify the cited theorem or named result and its use location. Do not present an external theorem's full hypotheses as checked unless they were actually reviewed.

Use LaTeX math delimiters in `statement`, such as `$...$` or `\[...\]`. JSON requires each backslash to be escaped: write `"$X_n\\xrightarrow{p}X$"` in the file. Fix escaping in the data instead of adding paper-specific rendering code. Unsupported math should be surfaced and corrected or explicitly reported, never silently replaced with an inaccurate formula.

Validate after assembling the inventory and a few representative dependencies, then again after completing the dataset. This catches duplicate IDs, broken links, and malformed records before report polish. Structural validation and a rendered diagram do not replace the final comparison with the manuscript.
