# Author selected statements and connections

Start with the paper's main results, then include prerequisites that materially explain them. There is no fixed node count or one-hop limit. A supporting statement unrelated to the selected results calls for a relevance check; a main result with no located prerequisites is valid without invented arrows. Describe the selection and omitted material once in `scope`.

Use the [minimal seed](../examples/representer-theorem/seed.json) and [database workflow](database.md). A seed declares `schema_version: 3` and has `source`; a captured export instead has `source_revision` and generated bookkeeping. Do not combine the two forms. New work uses `init --focused`.

## Statements and main-result roles

| Record | Authored fields |
|---|---|
| Dataset | `schema_version`, `title`, `scope`, `source`, `items`, `uses`, nonempty `main_items` |
| Source | `title`; `file` when the root manuscript is a file |
| Item | `id`, `kind`, `label`, `caption`, `statement: {form, text}`, and `source` or nonempty `passages`; optional `proof_idea` |
| Use | `from`, `to`, `reason`; optional `id`, `type`, `source`, `regime`, `issue` |

Choose stable IDs independent of printed numbering. `main_items` contains unique IDs from the selected major statements; it identifies main-result roles, not a separate record type or a filter. A main theorem may support another main theorem while retaining one identity.

Major kinds are `assumption`, `definition`, `lemma`, `proposition`, `theorem`, `corollary`, and `external_result`. Follow the manuscript's declaration type: a declared lemma remains a lemma even if the paper attributes its proof elsewhere. Use `external_result` for a result only cited from other work. Located prose assumptions and definitions may have descriptive labels. Do not promote exposition or individual proof steps to major results. Intermediate kinds, non-null `owner`, and non-null use `group` are outside focused authoring; compatibility is documented separately.

Represent one physical statement once. Several equations defining one coherent setup may share a definition node with multiple passages; separately numbered declarations, independently invoked results, or logically distinct assumptions remain distinct. Additional TeX keys may be recorded in optional `aliases` or source passages, without duplicating the result. `label` is its verified manuscript name or a short, honest descriptive label such as `Thm · Fixed point` when no printed number is available. Put the longer description in `caption` or `statement`, rather than a sentence-length node label. `caption` is a short prose gloss such as “Uniform error bound”; put formulas in `statement` instead. `statement` normally uses `{"form": "synopsis", "text": "..."}`; choose `verbatim` or `transcription` only when accurate.

For a main result, use `proof_idea` to explain the written argument's central mechanism and how its important inputs work together, usually in two to four sentences. For example: projection preserves training evaluations, while a nonzero orthogonal component increases the norm, so a strictly increasing penalty excludes it at a finite minimizer. Name the essential restriction or turning point instead of repeating the conclusion or listing lemma numbers. An informative caption such as “Projection preserves predictions” can expose the same mechanism briefly.

Store this explanation as a nonempty string on the item, separate from its precise statement. Ground it in the item's proof/evidence passages and the located contributions already being read; add a passage only when needed to support the explanation. Compare it in the same item review. Supporting lemmas may also benefit, but assumptions and definitions need no routine proof idea. If the written argument is missing or unclear, omit the explanation and disclose the limitation in the existing `issue` or scope. Do not invent a strategy or add proof-step nodes to fill the section. Existing records without the field remain usable.

Use ordinary Unicode prose with explicitly delimited LaTeX in statements, proof ideas, reasons, issues, regimes, and scope. JSON encodes a single LaTeX backslash as `\\`, for example `"\\(X_n\\xrightarrow{p}X\\)"`; after JSON parsing the text has single backslashes. Read private macro definitions and write their meaning with standard LaTeX commands in authored summaries. Preserve captured passages literally. Check representative notation in the current draft before bulk comparisons when needed, using `math_diagnostics` to locate repairs; do not replace the database to repair display text.

Invalid JSON escapes fail immediately; valid escapes such as `\r`, `\n`, and `\t` can silently consume the beginning of a LaTeX command. For math-heavy seeds or batches, an optional Python serializer avoids manual escaping. Given an existing `seed` object:

```python
import json
seed["items"][0]["statement"]["text"] = r"Under Assumption 1, \(\lVert f\rVert \leq M\)."
with open("work/seed.json", "w", encoding="utf-8") as output:
    json.dump(seed, output, ensure_ascii=False, indent=2)
```

This only writes ordinary JSON; direct file-tool authoring remains valid. Diagnostics catch specific corruption patterns, not every escaping mistake. Correct reported errors from the source, without guessing the missing command.

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

Seed paths resolve relative to the JSON file. `--source-root` identifies the manuscript root for stored paths and future refreshes, not a different rule for seed paths. An absolute manuscript path avoids dependence on the seed's `work/` location. Register any PDF or supplement relied on before comparisons. PDF anchors name the PDF file separately from TeX anchors.

### PDF-only input

Use page-only locations for a PDF. This seed illustrates the shape; replace the sample path, page, label, and synopsis with the actual paper evidence:

```json
{
  "schema_version": 3,
  "title": "Selected paper results",
  "scope": "Main consistency result and its prerequisites.",
  "source": {"title": "Paper", "file": "C:/papers/paper.pdf"},
  "items": [{
    "id": "consistency", "kind": "theorem", "label": "Theorem 2",
    "caption": "Consistency under the stated conditions",
    "statement": {"form": "synopsis", "text": "Replace with the conditions and conclusion read from the paper."},
    "source": {"page": 7}
  }],
  "uses": [], "main_items": ["consistency"]
}
```

Each PDF anchor captures the whole physical page's extracted text. Line ranges require a captured text source; they cannot select lines within a PDF page. Use an available page-viewing tool or a shared renderer such as PyMuPDF for page images; `pypdf` extracts text but does not render images. No second extractor is required. Establish image capability on the first page inspection already needed: correct an ordinary path error, but stop retrying a route that explicitly cannot supply usable images. Capability depends on the available tool, not the agent name; a missing browser does not imply missing image input.

Prefer direct PDF anchors. Extraction replacements such as `�` disclose lost characters, not recovered mathematics. If a derived transcript is genuinely needed, register the original PDF as a source before comparisons and disclose the derivation; transcript lines are text evidence and supplemental PDF pages are navigation metadata. Never guess missing glyphs. Apply the formula comparison guidance below to the affected records.

## Connections

Arrows run from a prerequisite to the result using it. Record the actual contribution once. Several inputs may be needed together; a single arrow never claims sufficiency. Check explicitly stated premises separately from proof citations. A selected assumption expressly imposed by a target deserves a direct connection even if also reachable through a lemma. Do not add other transitive arrows automatically.

| Type | Meaning |
|---|---|
| `dependency` (default) | The target relies on the source's stated condition or conclusion |
| `definition` | The target uses the source's definition or construction |
| `proof_argument` | The target borrows an argument inside the source's proof |

Write the existing `reason` as “The target uses [this particular condition, conclusion, definition, or proof argument] to [do this], under [any essential restriction].” Compare that contribution with both passages. “Uses Lemma A's bound to control the remainder” identifies a contribution; “shares Lemma A's assumptions” or “motivates the construction” alone does not justify an arrow.

A citation that merely identifies a shared premise does not make the cited theorem a prerequisite: locate the actual assumption or explain the relationship without inventing a theorem dependency. Stronger source hypotheses cannot silently justify a target under weaker hypotheses. Compare the actual consumer and contribution, for example:

- If Lemma L verifies a model's conditions and then invokes universal Theorem T, record T → L with that model restriction. L is not a premise of the universal theorem; do not add L → T merely to represent condition verification.
- An overlap assumption used to estimate a population quantity supports the estimation construction, not automatically the earlier identity defining that quantity.
- If a selected lemma explicitly reuses an argument in a selected theorem's proof, retain the qualified `proof_argument` arrow, even if it creates a cycle. This need not invoke the whole theorem or carry all its hypotheses.

If a target both assumes a theorem's hypotheses and borrows its proof argument, preserve the distinct contributions when relevant. Parallel arrows are allowed for materially different uses, not every citation occurrence.

Optional `regime` names an applicability condition or route, explained in `reason`. Joint requirements and alternative routes belong in concise reasons or scope; do not build formal groups. Preserve genuine arrows and explain cycles without reconstructing detailed proof steps.

Normally each connection has located evidence. A provisional connection must have a source basis and a specific `issue` explaining what remains unclear. An unlocated connection cannot receive a current `matched` comparison. If no source basis exists, omit the speculative arrow and disclose the gap in scope. An inferred use should say what source passage supports the inference; a citation alone, or adding the word “inferred,” does not establish it.

## Compare and display

Compare the current saved `statement.text`, any `proof_idea`, and `reason` with their supporting passages and applicable setup, using the bounded packet or exact current records already in context. Check what the wording drops (a condition, domain, bound, or conjunction) and what it adds (a stronger conclusion, equivalence, or unsupported contribution). Check complete fractions and normalization, not just the symbols. During this same comparison, check the target's explicitly imposed assumptions against its incoming arrows: each already selected assumption needs a direct connection, even if also reachable through a lemma. Keep the synopsis, proof idea, and connection explanations consistent.

Read necessary proof passages without auditing individual deductions. Correct affected records through normal edits and compare the changed context, reusing unchanged source reading without reopening unrelated branches. An accurate comparison note cannot excuse inaccurate saved wording. Keep notes concise and specific to the targets actually compared; a shared note is sufficient when accurate for each target. This is the existing source comparison, not another review pass or certificate.

When a comparison relies on PDF-extracted mathematics, visually check the formulas actually transcribed or relied on by the saved statement or connection before marking it matched. Reuse a page inspection across supported records; unrelated formulas on that page need no check. A rate whose denominator, exponent, sign, or normalization cannot be confirmed is formula-sensitive. A prose citation relationship may be checked independently when it asserts none of that uncertain content; absence of LaTeX alone does not make a reason formula-independent. If usable images are unavailable, retain specific `needs_attention` notes for affected records and continue. Do not propagate uncertainty to every neighbor. Agreeing extractors or mathematical plausibility cannot settle damaged formulas, and no extra extraction round is required.

For external attribution, a numbered lemma restated from another work remains a lemma. “Apply the external fixed-point theorem to obtain existence” can justify an external-result node; “see this survey for background” cannot. Preserve the supplied citation when details are unavailable; do not invent bibliographic expansions or claim an external theorem was checked without reading it. Disclose unavailable material and interpretation limits. Selection need not produce identical node counts across authors.

The renderer derives MathML without changing the recorded mathematics. Resolve authoring and escaping errors through normal edits. Faithful unsupported notation can remain visibly labeled with a display limitation; unresolved meaning requires `needs_attention`.

Render representative draft content before final comparisons when that would reveal display problems. Finish with selected statements and connections compared or explicitly unresolved, not an exhaustive declaration inventory or a zero-warning citation scan. Structural validation cannot detect an omitted mathematical qualification and never verifies a proof.
