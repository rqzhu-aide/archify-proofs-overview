# Author a native v3 seed

Use a small schema-3 seed to initialize a paper database, then continue through the edits and comparisons in [database.md](database.md). The script captures source files and generates anchors, excerpts, hashes, and source revisions. After initialization the database is authoritative; the seed is not a second master. Validation checks record integrity, not mathematical truth or completeness.

Build the seed from the fields below and the actual paper. The [representer theorem example](../examples/representer-theorem/proof.md) and its [captured v3 export](../examples/representer-theorem/overview.json) illustrate source-backed records and delivery; the [minimal seed](../examples/representer-theorem/seed.json) is one complete, initializing seed against that same source. A seed has `source`; a captured export has `source_revision` and generated bookkeeping. Both declare `schema_version: 3`, and `init` accepts either, but never combine the two forms. Use meaningful stable identifiers such as `uniform-remainder`, independent of printed labels.

## Minimal records

| Record | Required content | Purpose |
|---|---|---|
| Dataset | `schema_version: 3`, `title`, `scope`, `source`, `items`, `uses`; optional `main_items` | Names the paper and makes the selected scope explicit |
| Paper `source` | `title`; optional `file` | Identifies the paper/source artifact or supplied excerpt |
| Item | `id`, `kind`, `label`, `caption`, `statement: {form, text}`; at least one `source` or `passages` entry | Represents a major item or an owned intermediate step |
| Use | `from`, `to`, `reason`; optional `id`, `source`, `type`, `regime`, `group`, `issue` | Explains one contribution of a prerequisite to a target |

An item may also carry `aliases`, `issue`, and, for an intermediate kind, its required `owner`. Its `source` is shorthand for a statement passage. To register several passages in the seed, use `passages: [{"role": "statement", "source": {"start_line": 10, "end_line": 14}}, {"role": "proof", "source": {"start_line": 20, "end_line": 30}}]` with the actual locators. Other passage roles are `definition` and `evidence`. The script turns these locators into generated anchors; seed authors do not supply `anchor_id`, excerpts, hashes, `evidence_refs`, or observations.

Use IDs may be supplied explicitly or generated at initialization; parallel uses remain separate. Retrieve the assigned IDs before later edits. New seed records start unreviewed. A captured v3 export retains its existing observation history when imported, without creating new comparisons.

Major kinds are `assumption`, `definition`, `lemma`, `proposition`, `theorem`, `corollary`, and `external_result`. Intermediate kinds are `equation`, `claim`, and `derivation`, with the owner rule below. Do not create a node for every equation or proof paragraph. A crucial unnumbered result may use a descriptive label such as “Uniform remainder bound”; do not manufacture a manuscript number for it.

A kind is a bibliographic fact about the manuscript — how the paper presents the content — never a judgment about its importance or its correctness. Importance belongs to `main_items`; this overview never assesses correctness. Two readers applying this procedure to the same paper should reach the same kinds. The declaration scan proposes major-row candidates, a citation sweep proposes uses, and a reviewer's need to address an exact passage proposes intermediate rows; the scans are checklists, not the only source of rows. An agent may also record a mathematical component found by reading — a global assumption or a model definition stated in ordinary prose — provided it has an explicit source locator and a faithful description. No source passage means no row. Then, in order:

1. Provenance. Content declared in its own theorem-like environment is a major row; a result proved elsewhere and only cited is an `external_result`; content living inside another statement or its proof is an intermediate row. Explicitly located prose content the argument depends on — a standing assumption, a model definition — is a major row with a faithful descriptive label; do not invent a declaration number for it or promote ordinary exposition to a theorem. Remarks, notation, and conventions stay outside automatic major-node enumeration by default; that is a scanning rule, not a ban — record a definition or condition inside such an environment when the argument actually depends on it, without creating a node for every such environment.
2. A major row takes its declaration type, normalized: shorthand environments map to their kind (`thm` becomes `theorem`, `cor` becomes `corollary`), and a condition-style declaration records as `assumption`. Confirm the kind against the printed wording. If the declaration type and the logical role disagree — a main result declared as a lemma — the declaration wins, so a second reader can re-check the assignment objectively.
3. An intermediate row takes its structural shape: a single displayed relation or bound that later work cites is an `equation`; one assertion with its own inline proof is a `claim`; a multi-step argument whose conclusion is never declared is a `derivation`. One assertion stays a `claim`; the passage-as-argument is a `derivation`.
4. Creation authority. Overview authoring creates major rows routinely and intermediate rows only on demand, when a review must address an exact step; a proof audit creates them routinely.
5. Stability. Renumbering edits `label`, never `kind`. A genuine promotion — a lemma becoming a theorem in a revised manuscript — is an ordinary content edit, which honestly stales the affected comparisons.

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

Distinguish stated and inferred links in the reason text. For example, “Inferred from the substitution in the proof of Thm 2.3; no explicit lemma citation” makes the evidence level clear. Use an optional `issue` string on an item or use for a specific extraction or dependency question. The renderer discloses it without assigning a correctness judgment. Omit a speculative link when there is no source basis for it and describe the missing evidence in `scope`. Do not silently delete an apparent cycle; inspect the source and explain unresolved circularity or a representation limitation.

For a database-backed overview, close the dependency pass with the citation review in [database.md](database.md): after the first-pass build, run `candidates` for the citation evidence and disposition every row it reports — record a use for each real missing-edge candidate, and locate evidence or explain the inference in the reason for each unsupported-use flag. A missing citation does not prove a dependency is unsupported: do not add an "inferred" label merely to silence a candidate warning; the explanation must reflect the passage actually read. When a target's full incoming coverage should be reviewed exhaustively, the optional deep review is the `scaffold`/`reconcile` edge audit in [database.md](database.md): scaffold one audit file per major row, dispose of every candidate in each file (`depends_on` — pair-level or with a `contributions` list naming recorded uses by `use_id` or proposing new ones — or `dismissed`), and reconcile the filled files against the recorded uses. Work its rows — record genuine missing-edge candidates with `apply`, re-check suspect edges against the manuscript, submit the agreement `compare` skeleton only after performing the comparison, resolve refinements where type or regime diverged, resolve ambiguous entries by naming uses under `contributions`, and account for every `unresolved_uses` row before calling coverage complete. `candidates`, `scaffold`, and `reconcile` propose reviews and never modify records; only `apply` and `compare` change the database. The citation sweep needs captured TeX sources with real `\label` keys — a manuscript captured only as a PDF, or text without a manuscript marker (`\documentclass`, `\newtheorem`, `\begin{document}`), is reported as not applicable rather than as zero candidates; in that case review the recorded rows through `get` packets and `compare` batches and disclose the missing citation evidence as a coverage limit. Rows without a label anchor stay on the `not_mechanically_matchable` list and their links still require manual reading.

Use optional `regime` for a short applicability qualifier such as `"pilot-trained centers"` or `"Route B"`, and explain the condition fully in `reason`. Use the same name consistently for the same route. A known alternative regime is not an `issue`. Arrows without a regime are unqualified uses in the selected argument. The displayed qualifiers do not encode a formal logic of alternative proofs: if the grouping remains ambiguous, explain it in `scope` or select and name one route. Do not imply that all incoming arrows are jointly required across alternative proofs.

For example, a use may have `"type": "proof_argument"` and `"regime": "pilot-trained centers"` when that case reuses a bound from another proof. Omitted `type` defaults to `dependency`.

## Content and ownership fields

These fields apply to both seeds and captured records; [database.md](database.md) describes the generated anchors and edit-batch form.

- A statement is `{ "form": "synopsis", "text": "..." }`; use `verbatim` or `transcription` only when that describes the content accurately. A plain string is not a native v3 statement.
- An intermediate row (`equation`, `claim`, or `derivation`) requires `owner` naming the existing major row whose statement or proof contains it, and a major row never takes an `owner`. The row appears inside its owner's detail panel, never as an overview node. If no major row owns the content, disclose the step in `scope` instead of inventing a parent.
- A use may carry `group` as `{ "id": ..., "kind": "joint" }` or `"cases"`: `joint` marks prerequisites required together and `cases` marks alternative routes. One group id keeps one consistent kind across every use that names it; reuse the same id for the same route. The viewer discloses a group with a badge; a group never claims that its premises suffice.
- `issue` holds one specific open question on an item or use, disclosed without a correctness judgment. Omit it when there is no open question.

Edit batches use two operations only: `upsert` supplies the complete intended record rather than merging unspecified fields, and `remove` deletes an existing identity; see [database.md](database.md).

## Source fidelity and scope

Read each included result's statement and enough of its proof to identify its major uses. Check declarations in appendices and supplements before saying that the overview covers the whole supplied paper. Otherwise describe the selection, for example “Main results of Sections 2 to 4; auxiliary appendix lemmas omitted.” Identify unreadable formulas, missing supplements, and unresolved numbering in the same scope description.

Compare every statement or synopsis against its source before delivery. In particular, retain whether quantities are deterministic or random, what is conditioned on, all essential hypotheses and quantifiers, and the precise target, normalization, and mode of convergence. Compare each use against the cited proof passage: check what is actually borrowed and whether it applies only under one regime. Correct the same JSON records when this comparison exposes an omission; no separate review ledger is required. A validator cannot detect an omitted mathematical condition merely because the remaining record is well formed.

Check the synopsis and stated premises before marking a record matched. Check scope when completing or changing the inventory:

1. Does the synopsis preserve the conclusion, normalization constants, conditions, domains, quantifiers, and relevant notation? A dropped factor of one-half, or a subscript lost onto a squared symbol, is a content error, not a presentation choice.
2. Does the incoming dependency list represent every explicitly stated premise, separately from proof citations? A premise stated as a range such as "(A1) through (A3)" still needs each intended connection recorded.
3. Does the declared scope describe the records and source passages actually included, including what was omitted or unreadable?

Read each format the way it can mislead. For PDF input, use extracted text to navigate and draft, but visually compare every important recorded formula with the rendered page before marking it matched: subscripts, powers, hats, signs, inverse operators, and domains are exactly what extraction loses. Establish shared notation once carefully before reusing it. For TeX input, read the source definitions of private macros and preserve numerical factors when replacing formulas with prose; use source labels and descriptive titles when printed numbering is unavailable, and never infer printed numbers from environment order. For unavailable proofs or external results, compare only what the supplied source supports, and disclose whether a connection is stated, inferred from discussion, or unreviewable with the available material.

A graph of roughly 8 to 12 major items is often easy to read, but this is a presentation target, not a limit. Retain all selected results. Use an explicitly narrower scope when appropriate; never hide results solely to reach a node count.

Keep exact manuscript labels distinct from descriptive captions. When available, use the current compiled `.aux` to map TeX keys to printed theorem and equation numbers, checking the mapping against the corresponding PDF. Do not infer numbering by counting theorem environments or displayed formulas; leave unresolved labels explicit. An external result should identify the cited theorem or named result and its use location. Do not present an external theorem's full hypotheses as checked unless they were actually reviewed.

Use LaTeX math delimiters in `statement.text`, such as `$...$` or `\[...\]`. JSON requires each backslash to be escaped: write `"$X_n\\xrightarrow{p}X$"` in the file. Fix escaping in the data instead of adding paper-specific rendering code. Unsupported math should be surfaced and corrected or explicitly reported, never silently replaced with an inaccurate formula.

Try the display while the records are still drafts: render the database after assembling the inventory and a few representative dependencies, then inspect `math_diagnostics` before recording a large batch of comparisons. Each entry names the record id, field, bounded excerpt, and converter rejection reason. Fix escaping and supported notation early to avoid later comparison churn. Sized named bar delimiters such as `\Bigl\lVert` and unbraced binomial arguments followed by a script, as in `\binom nk^{-1}`, are adapted on the converter-input copy with the original TeX retained as the annotation. Expand paper-specific macros from their actual source definitions; the renderer does not interpret TeX macro definitions.

A faithful expression that the converter cannot typeset may remain as visible labeled LaTeX with the display limitation disclosed. That limitation alone does not prevent a source comparison from being matched. Use `needs_attention` when the mathematical interpretation itself remains unresolved; do not change the meaning merely to clear a display diagnostic.

Validate after assembling the inventory and a few representative dependencies, then again after completing the dataset. This catches duplicate IDs, broken links, and malformed records before report polish. Structural validation and a rendered diagram do not replace the final comparison with the manuscript.
