# Archify Proofs Overview

[![Version v3.0.0](https://img.shields.io/badge/version-v3.0.0-6366f1)](SKILL.md)

**Build a reusable database of a paper's proof structure, then visualize it in the style of [Archify](https://github.com/tt-a1i/archify).**

The skill reads your paper and records its assumptions, definitions, lemmas, theorems, and dependencies in a local SQLite database, together with source passages and recorded source comparisons. Scripts generate an interactive HTML overview directly from those records. Major results form the graph; intermediate steps remain available in the details.

This database makes the work reusable: [revise the overview](references/revisions.md) as the manuscript changes, or [hand the records to proofcheck](references/audit-database.md) for a deeper audit. Source comparisons document what was reviewed; they do not certify mathematical correctness.

[![Example](examples/overview-browser.png)](examples/overview-browser.png)

Open the [example HTML](examples/representer-theorem/overview.html) locally to explore statements and trace dependencies. Read its [source proof](examples/representer-theorem/proof.md).

## Use

Install this repository as `archify-proofs-overview` in your agent's skill directory, then ask:

```text
Use archify-proofs-overview to map this paper, including its appendix.
```

Requires shared Python 3.10+, Node.js, and `latex2mathml`; PDF input also uses `pypdf`. No separate Archify installation is needed. See [SKILL.md](SKILL.md) for the workflow.

## Output

```text
archify-proofs-overview-<paper-name>/
  overview.html
  data/paper-records.sqlite
```

**Share the HTML alone. Keep the database for future revisions.** See the [database guide](references/database.md) for editing and exporting records. V3 accepts native v3 records only; the previous release is preserved on the [v2 branch](https://github.com/rqzhu-aide/archify-proofs-overview/tree/v2).

[MIT license](LICENSE) · [Third-party notices](THIRD_PARTY_NOTICES.md)
