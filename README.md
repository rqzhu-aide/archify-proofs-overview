# Archify Proofs Overview

Version [v1.0.0](https://github.com/rqzhu-aide/archify-proofs-overview/tree/v1.0.0).

Turn a paper's assumptions, definitions, lemmas, propositions, theorems, and corollaries into an interactive map, adapted from [Archify](https://github.com/tt-a1i/archify). Follow the written argument through compact nodes, manuscript labels, source passages, and annotated connections.

**This is a proof overview, not mathematical proof verification.** Arrows record how results are used; they do not certify correctness.

[![Example](examples/overview-browser.png)](examples/overview-browser.png)

Explore the [Example](examples/representer-theorem/overview.html) and its [source proof](examples/representer-theorem/proof.md). Download the HTML and open it locally to select statements, inspect source excerpts, and trace dependencies.

The viewer includes search, zoom, main-result navigation, and light/dark themes. Node colors distinguish mathematical types. Connections distinguish definition uses, theorem dependencies, reused proof arguments, and applicability conditions. Selecting a result preserves access to the complete recorded structure.

## Install and use

Ask Codex:

```text
Install https://github.com/rqzhu-aide/archify-proofs-overview
into ~/.codex/skills/archify-proofs-overview.
```

Then provide your paper:

```text
Use $archify-proofs-overview to map this paper's main assumptions
and results, including its appendix. Keep reusable paper records
in a dedicated overview folder beside the manuscript.
```

Requirements: shared Python 3.10+, Node.js on `PATH`, and `latex2mathml` in that Python installation for offline typeset mathematics:

```text
python -m pip install --user latex2mathml
```

No npm packages, separate Archify installation, or project-local environment are needed. PDF reading may require a shared extraction tool.

## Output and reuse

```text
archify-proofs-overview-<paper-name>/
  overview.html
  data/paper-records.sqlite
```

**Send the HTML alone to share the overview.** Keep the database for later revisions; shared scripts supply the tooling. Optional `work/` and `exports/` folders hold edit batches and portable records.

See [record authoring](references/authoring.md), the [database workflow](references/database.md), and [manuscript revisions](references/revisions.md). Existing JSON-only overviews remain supported. Reusing previous comparisons requires review of the changed source context.

## Development and attribution

Run tests with your shared runtimes:

```text
python -B -m unittest discover -s tests -v
```

The skill entry point is [SKILL.md](SKILL.md). [MIT licensed](LICENSE); bundled Archify and font notices are preserved in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
