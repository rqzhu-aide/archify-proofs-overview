# Author selected statements and connections

Start with the paper's main results, then include prerequisites that materially explain them. There is no fixed node count or one-hop limit. A supporting statement unrelated to the selected results calls for a relevance check; a main result with no located prerequisites is valid without invented arrows. Describe the selection and omitted material once in `scope`.

Use the [minimal seed](../examples/representer-theorem/seed.json) and [database workflow](database.md). A seed declares `schema_version: 3` and has `source`; a captured export instead has `source_revision` and generated bookkeeping. Do not combine the two forms. New work uses `init --focused`.

## Statements and main-result roles

| Record | Authored fields |
|---|---|
| Dataset | `schema_version`, `title`, `scope`, `source`, `items`, `uses`, nonempty `main_items` |
| Source | `title`; `file` when the root manuscript is a file |
| Item | `id`, `kind`, `label`, `caption`, `statement: {form, text}`, and `source` or nonempty `passages` |
| Use | `from`, `to`, `reason`; optional `id`, `type`, `source`, `regime`, `issue` |

Choose stable IDs independent of printed numbering. `main_items` contains unique IDs from the selected major statements; it identifies main-result roles, not a separate record type or a filter. A main theorem may support another main theorem while retaining one identity.

Major kinds are `assumption`, `definition`, `lemma`, `proposition`, `theorem`, `corollary`, and `external_result`. Follow the manuscript's declaration type: a declared lemma remains a lemma even if the paper attributes its proof elsewhere. Use `external_result` for a result only cited from other work. Located prose assumptions and definitions may have descriptive labels. Do not promote exposition or individual proof steps to major results. Intermediate kinds, non-null `owner`, and non-null use `group` are outside focused authoring; compatibility is documented separately.

Represent one physical statement once. Additional TeX keys may be recorded in optional `aliases` or source passages, without duplicating the result. `label` is its verified manuscript name or a short, honest descriptive label such as `Thm · Fixed point` when no printed number is available. Put the longer description in `caption` or `statement`, rather than a sentence-length node label. `caption` is a short plain-language gloss. `statement` normally uses `{"form": "synopsis", "text": "..."}`; choose `verbatim` or `transcription` only when accurate.

Use ordinary Unicode prose with explicitly delimited LaTeX in statements, reasons, issues, regimes, and scope. JSON encodes a single LaTeX backslash as `\\`, for example `"\\(X_n\\xrightarrow{p}X\\)"`; after JSON parsing the text has single backslashes. Read private macro definitions and write their meaning with standard LaTeX commands in authored summaries. Preserve captured passages literally. Check representative notation in the current draft before bulk comparisons when needed, using `math_diagnostics` to locate repairs; do not replace the database to repair display text.

Read the statement with its applicable section setup, preceding definitions, and referenced assumptions. For conditional results, use “Under [essential setup and conditions], [conclusion]” as a writing aid. Include relations that define the formula's objects, such as what a remainder is the difference of. Follow relevant references without widening every passage by a fixed number of lines. Preserve domains, quantifiers, conditioning, quantitative caps, conjunctions, normalization, and convergence modes whose omission changes the claim. For example, \(\sup_t\|f_t\|\leq M\) must not become merely “uniformly bounded” when the specified \(M\) matters. Shorten exposition around these restrictions, not the restrictions themselves.

Make essential conditions visible in the synopsis or an explicit, clearly named prerequisite. A source excerpt alone cannot repair an overstated synopsis. Attach needed standing setup through existing passages, reusing its reading and suitable anchors as described in [database.md](database.md#read-one-argument). A shared condition needs its own node only when it materially explains the selected argument. Do not reconstruct omitted proofs or infer missing hypotheses. An optional `issue` states one specific unresolved interpretation.

## Source locations

An item's `source` locates its statement. For several passages use, for example:

```json
"passages": [
  {"role": "statement", "source": {"start_line": 10, "end_line": 14}},
  {"role": "proof", "source": {"start_line": 20, "end_line": 30}}
]
```

The other passage roles are `definition` and `evidence`. A use's `source` locates the passage supporting that contribution. These example lines are placeholders, not evidence. Scripts generate anchors, excerpts, hashes, and observations; seed authors do not invent those fields.

Locators support `file`, `label`, `page`, and paired `start_line`/`end_line`. Lines are inclusive and one-based; PDF pages are physical, one-based pages. Use actual TeX keys in source labels and verified printed names in item labels. A descriptive locator is allowed when exact numbering is unavailable. Never infer printed numbers by counting environments, or PDF pages from TeX lines. A current `.aux` may help map numbering when checked against its PDF; it is not mathematical evidence.

Seed paths resolve relative to the JSON file. `--source-root` identifies the manuscript root for stored paths and future refreshes, not a different rule for seed paths. A seed in `work/` might therefore refer to `../../main.tex`. Register any PDF or supplement relied on before comparisons. PDF anchors name the PDF file separately from TeX anchors.

## Connections

Arrows run from a prerequisite to the result using it. Record the actual contribution once. Several inputs may be needed together; a single arrow never claims sufficiency. Check explicitly stated premises separately from proof citations. A selected assumption expressly imposed by a target deserves a direct connection even if also reachable through a lemma. Do not add other transitive arrows automatically.

| Type | Meaning |
|---|---|
| `dependency` (default) | The target relies on the source's stated condition or conclusion |
| `definition` | The target uses the source's definition or construction |
| `proof_argument` | The target borrows an argument inside the source's proof |

Write the existing `reason` as “The target uses [this particular condition, conclusion, definition, or proof argument] to [do this], under [any essential restriction].” Compare that contribution with both passages. “Uses Lemma A's bound to control the remainder” identifies a contribution; “shares Lemma A's assumptions” or “motivates the construction” alone does not justify an arrow.

A citation that merely identifies a shared premise does not make the cited theorem a prerequisite: locate the actual assumption or explain the relationship without inventing a theorem dependency. Stronger source hypotheses cannot silently justify a target under weaker hypotheses. If the target reuses a separable proof argument, identify that narrower contribution and its conditions without claiming the whole theorem applies. Connect to the statement that actually consumes it; a later estimator construction does not add a prerequisite to an earlier proposition. If a target both assumes a theorem's hypotheses and borrows its proof argument, preserve the distinct contributions when relevant. Parallel arrows are allowed for materially different uses, not every citation occurrence.

Optional `regime` names an applicability condition or route, explained in `reason`. Joint requirements and alternative routes belong in concise reasons or scope; do not build formal groups. An argument reused from a theorem's proof may create a cycle with that theorem. Preserve the major statements and genuine arrows, and explain the reuse. Resolving every apparent cycle into detailed steps is outside the overview's task.

Normally each connection has located evidence. A provisional connection must have a source basis and a specific `issue` explaining what remains unclear. An unlocated connection cannot receive a current `matched` comparison. If no source basis exists, omit the speculative arrow and disclose the gap in scope. An inferred use should say what source passage supports the inference; a citation alone, or adding the word “inferred,” does not establish it.

## Compare and display

Compare the current saved `statement.text` and `reason` with their supporting passages and applicable setup, using the bounded packet or exact current records already in context. Check what the wording drops (a condition, domain, bound, or conjunction) and what it adds (a stronger conclusion, equivalence, or unsupported contribution). Check complete fractions and normalization, not just the symbols. During this same comparison, check the target's explicitly imposed assumptions against its incoming arrows: each already selected assumption needs a direct connection, even if also reachable through a lemma. Keep the synopsis and connection explanations consistent.

Read necessary proof passages without auditing individual deductions. Correct affected records through normal edits and compare the changed context, reusing unchanged source reading without reopening unrelated branches. An accurate comparison note cannot excuse inaccurate saved wording. Keep notes concise and specific to the targets actually compared; a shared note is sufficient when accurate for each target. This is the existing source comparison, not another review pass or certificate.

When a formula comparison relies on PDF-extracted text, visually compare the important recorded formula with the page before marking it matched. If page images cannot be inspected, retain `needs_attention` for those formula-sensitive records and explain the missing evidence. Agreeing text extractors alone do not settle fractions, underlines, or superscripts. Continue with the usable overview and disclose these unresolved comparisons; no extra extraction round is required.

For an external result, preserve the citation as supplied when author names or details are unavailable; do not invent bibliographic expansions or claim the external theorem was checked without reading it. Disclose unavailable material and interpretation limits.

The renderer derives MathML without changing the recorded mathematics. Resolve authoring and escaping errors through normal edits. Faithful unsupported notation can remain visibly labeled with a display limitation; unresolved meaning requires `needs_attention`.

Render representative draft content before final comparisons when that would reveal display problems. Finish with selected statements and connections compared or explicitly unresolved, not an exhaustive declaration inventory or a zero-warning citation scan. Structural validation cannot detect an omitted mathematical qualification and never verifies a proof.
