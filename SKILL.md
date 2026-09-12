---
name: archify-proofs-overview
description: Build an interactive HTML overview of a paper's main assumptions, definitions, lemmas, propositions, theorems, corollaries, and their dependencies. Use for understanding proof organization and navigating major results, not for full mathematical proof verification.
---

# Archify Proofs Overview

Create a compact, source-backed map of the paper's major mathematical results using the adapted Archify viewer. The deliverables are an editable JSON dataset and a standalone HTML overview. This map explains the structure of the written argument; it is not a certificate that the proofs are valid.

## Workflow

1. Read the supplied paper and its proof sections. Use TeX labels and source passages when available; inspect the PDF for printed numbering, pages, and formulas. Use existing shared PDF tools rather than building a new extraction pipeline.
2. Identify the major assumptions, definitions, lemmas, propositions, theorems, and corollaries within the requested scope. Include an external result when it materially explains a proof. Keep intermediate algebra and routine proof steps inside the item descriptions, not as additional overview nodes.
3. Author the dataset directly following [references/authoring.md](references/authoring.md). Record the paper's statements and the reason for each dependency. For a large map, identify its main results with optional `main_items`; qualify a use with `type` or `regime` when needed to preserve the written argument. Use the supplied example only to learn the format, never as evidence about the user's paper.
4. Validate the dataset, then render it with the shared script:

   ```text
   python <skill>/scripts/proof_overview.py validate paper-proof-overview.json
   python <skill>/scripts/proof_overview.py render paper-proof-overview.json paper-proof-overview.html
   ```

5. Compare the summaries and uses against the manuscript, including deterministic versus random quantities, conditioning, hypotheses, quantifiers, and the exact target or conclusion. Check labels, source locations, dependency direction, disconnected results, and unresolved extraction questions. Inspect the HTML when a browser is available, including the longest mathematical statement and a result with several prerequisites. Deliver the HTML under the user's main paper/output folder alongside its JSON dataset, unless the user specifies another location.

## Meaning and presentation

- Each arrow runs from a prerequisite to a result that uses it. Record the actual use, not merely the presence of a citation. Several inputs may be required together; an arrow alone does not mean that one prerequisite is sufficient.
- Distinguish a dependency on a stated result (`dependency`), use of a definition (`definition`), and reuse of an argument inside another proof (`proof_argument`). A `regime` names an applicability condition or alternative proof route; it does not mark uncertainty. Explain the contribution and qualification in `reason` as well.
- Preserve the paper's hypotheses, quantifiers, and conclusions in the detail text. Keep the visible node short: a manuscript label such as “Thm 1.2” and a brief caption. Hover or focus gives a short preview; selecting a node exposes its full statement, location, and connected uses.
- Let the introduction reuse reviewed main-result labels and captions. An optional, nonempty `main_items` selection changes the entry point to a large overview, not its inventory or mathematical content. Omit it to use the viewer's automatic terminal-result selection. Keep every item in the selected scope accessible; do not add a second set of summary claims to maintain.
- Node colors identify mathematical types, using the renderer's palette without red, green, or yellow. Neutral connections are the default. Do not assign correctness colors from extraction alone.
- State any selected scope, omitted sections, and unresolved source or dependency questions explicitly. Claim a complete inventory only after comparing the list against the paper's result declarations and relevant proof sections. Inventory coverage does not establish proof validity.
- Keep uncertain interpretations visible in the dataset and report. Never invent theorem numbers, PDF pages, source lines, dependencies, or proof judgments to fill a field. Explain a mapping limitation instead of changing manuscript content to accommodate the renderer.

Use shared Python with `latex2mathml` for offline MathML and shared Node.js for the bundled viewer. If the converter is missing, use the user's shared Python executable with `-m pip install --user latex2mathml`. If Node.js is unavailable, identify or request a shared installation. Do not create a project-local environment. Keep paper-specific reasoning in the JSON records; do not generate a custom Python bookkeeping or rendering program for each paper.
