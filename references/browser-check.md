# Inspect the delivered overview

Use an available browser tool to check a representative long statement, its prerequisites, and navigation. This is a small display check, not another source review. Reuse the shared browser capability; do not create a browser driver, install a framework, or make paper-specific automation a delivery requirement. After one failed attempt and one reasonable fallback, report the interaction checks that remain untested.

Use controls visible in the current viewport. Selection can pan the canvas and scroll to the details below it, so locate controls again after navigation. Open the finder before addressing its hidden input or results. A narrow browser pane may collapse optional controls; checking a wider viewport is useful when available, not a reason to change the paper records.

For existing DOM-capable tools, these are the current graph viewer's observable hooks. Choose real item and use IDs from `paper_database.py list`; the bracketed placeholders below are not literal IDs.

| Check | Action and observable result |
|---|---|
| Viewer is initialized | Wait for `#proof-selection` and callable `window.Archify.focus.active`, after the document loads. There is no `archify:ready` event to wait for. This establishes initialization, not successful interaction. |
| Open a statement | Activate `.diagram-container svg g[data-node-id="<item-id>"]`, using click or keyboard. `#focus-chip` becomes visible and `#proof-selection .proof-statement` shows that item's synopsis, formulas, source, and prerequisite list. The selected ID is available through `Archify.focus.active()`. |
| Read a prerequisite | Activate a visible `#proof-selection [data-proof-focus="<prerequisite-id>"]`. The selected statement and its displayed text must change to that prerequisite. Expand a source passage and check that its text is visible. |
| Search and navigate | Open `#btn-node-finder`, type a known label or caption into `#node-finder-input`, and activate the matching visible button in `#node-finder-results`. Confirm the intended statement opens, then use `#proof-full-structure` to restore the fitted graph. |
| Inspect a qualified connection or cycle, when present | Activate the visible `[data-proof-use="<use-id>"]` badge or cycle-list control. The details show the correct endpoint labels, contribution, type, and any restriction or issue. Ordinary connections can also be read in the selected statement's prerequisite list. |

Inspect visible mathematical notation and long text in the opened details. Counting MathML elements or testing for overflow is useful supporting evidence, but does not show that a formula means the right thing. Report observed outcomes. Screenshots establish appearance; a screenshot alone does not establish that controls worked.

If the render receipt specifies index mode, inspect `#proof-full-index` and its `article[data-proof-index-item]` entries instead. That mode provides the complete selected content without the graph viewer, so the graph-only selectors and interactions do not apply.
