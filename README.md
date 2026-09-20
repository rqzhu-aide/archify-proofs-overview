# Archify Proofs Overview

Version 3.0.0.

This release uses native schema-3 seeds, databases, and exports. Intermediate
rows (`equation`, `claim`, `derivation`) belong to a major row and appear in its
detail panel. Use groups mark `joint` prerequisites or alternative `cases`, and
`issue` records an open question. Source capture and evidence bookkeeping are
automatic. V1/v2 files are not accepted or upgraded.

Keep the database to revise the manuscript within v3, or use
[the audit handoff](references/audit-database.md) to continue into proofcheck.
The previous release is retained on the [v2 branch](https://github.com/rqzhu-aide/archify-proofs-overview/tree/v2).

Turn a paper's assumptions, definitions, lemmas, propositions, theorems, and corollaries into an interactive map, adapted from [Archify](https://github.com/tt-a1i/archify). Follow the written argument through compact nodes, manuscript labels, source passages, and annotated connections.

**This is a proof overview, not mathematical proof verification.** Arrows record how results are used; they do not certify correctness.

[![Example](examples/overview-browser.png)](examples/overview-browser.png)

Explore the [Example](examples/representer-theorem/overview.html) and its [source proof](examples/representer-theorem/proof.md). Download the HTML and open it locally to select statements, inspect source excerpts, and trace dependencies.

The viewer includes search, zoom, main-result navigation, and light/dark themes. Node colors distinguish mathematical types. Connections distinguish definition uses, theorem dependencies, reused proof arguments, and applicability conditions. Selecting a result preserves access to the complete recorded structure.

## Use

Install this folder as `archify-proofs-overview` in your agent's skill directory,
or select the repository copy explicitly:

```text
Use the skill at /absolute/path/to/archify-proofs-overview/SKILL.md
and its scripts to map this paper's main assumptions and results,
including its appendix. Keep reusable paper records beside the manuscript.
```

Requirements: shared Python 3.10+, Node.js on `PATH`, and `latex2mathml` in that Python installation for offline typeset mathematics:

```text
python -m pip install --user latex2mathml
```

No npm packages, separate Archify installation, or project-local environment are needed. PDF page capture uses a shared `pypdf` installation.

## Output and reuse

```text
archify-proofs-overview-<paper-name>/
  overview.html
  data/paper-records.sqlite
```

**Send the HTML alone to share the overview.** Keep the database for later revisions; shared scripts supply the tooling. Optional `work/` and `exports/` folders hold edit batches and portable records.

See [native record authoring](references/authoring.md), the [database workflow](references/database.md), and [manuscript revisions](references/revisions.md). Reusing previous v3 comparisons requires review of the changed source context.

Current limitations: PDF-heavy commands can be slow because source processing is repeated;
math inside issue notes is still displayed as LaTeX text. Automated checks preserve records
and test selected geometry, but browser inspection and accurate source reading remain necessary.

## Development and attribution

Run tests with your shared runtimes:

```text
python -B -m unittest discover -s tests -v
```

The skill entry point is [SKILL.md](SKILL.md). [MIT licensed](LICENSE); bundled Archify and font notices are preserved in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
