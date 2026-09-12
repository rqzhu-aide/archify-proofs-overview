# Archify Proofs Overview

An interactive map of a paper's major mathematical results, adapted from [Archify](https://github.com/tt-a1i/archify). Explore assumptions, definitions, lemmas, propositions, theorems, and corollaries through compact nodes, manuscript labels, and annotated connections.

This skill explains the structure of the written argument. It does not verify that the proofs are correct.

## What it produces

- A standalone HTML overview with main-result navigation, zoom, search, dependency tracing, and light/dark themes.
- Short previews and selectable full mathematical statements, source locations, and available source excerpts.
- Visible distinctions between definitions, stated-result dependencies, reused proof arguments, and uses that apply only in a named regime.
- An editable JSON dataset that the shared renderer turns into HTML. No paper-specific program is needed.

Node colors identify mathematical types. Connections do not claim proof validity. Selecting main results changes navigation while preserving the complete recorded graph and statement index.

## Install and use

Ask Codex:

```text
Install the skill from https://github.com/rqzhu-aide/archify-proofs-overview
into ~/.codex/skills/archify-proofs-overview.
```

Then invoke it with a paper:

```text
Use $archify-proofs-overview to map this paper's main assumptions and results,
including the appendix. Put the HTML and editable JSON beside the paper.
```

The skill works independently of the Archify installation and other paper skills. It bundles the viewer assets it needs. Its shared runtime requirements are:

- Python 3.10 or newer.
- Node.js available on `PATH`.
- `latex2mathml` in the Python installation used to run the renderer, for offline typeset LaTeX.

Install the converter once in that shared interpreter, for example:

```text
python -m pip install --user latex2mathml
```

No npm packages or project-local environment are needed. Missing or unsupported math conversion is visibly reported, with the original LaTeX retained. Paper reading may also require a shared PDF reader or extraction tool.

## Try the bundled example

From this repository's root, using your shared Python:

```text
python scripts/proof_overview.py validate examples/mean-consistency.overview.json
python scripts/proof_overview.py render examples/mean-consistency.overview.json output/mean-consistency.html
```

Open `output/mean-consistency.html` in a browser. The example is synthetic teaching material. For another paper, author the JSON using the [dataset guide](references/authoring.md); keep mathematical reasoning in those records.

To share a finished overview, send the HTML file alone. It embeds the viewer, graph, mathematical content, and available source excerpts. Share the JSON and referenced manuscript files as well only when the recipient needs to edit or rebuild it. Relative source paths resolve from the JSON file's directory.

## How it works

The agent reads the statements and relevant proof passages, creates one record per major item, and records the actual use behind each dependency. Multiple labels for one statement remain one item. A citation alone does not create an arrow.

The validator checks record integrity, source anchors, and an acyclic graph. The renderer then builds the HTML from the same records. Before delivery, the agent compares statements and uses against the manuscript and inspects the report. These checks can catch missing or misrepresented conditions; structural validation alone cannot establish mathematical truth or completeness.

Tracing follows recorded connections and can combine alternative regimes. It does not compute a minimal or sufficient set of assumptions. Dense graphs remain fully available through navigation, zoom, and the complete-structure view.

## Development

```text
python -B -m unittest discover -s tests -v
```

The tests use synthetic fixtures and check source protection, schema compatibility, complete graph preservation, qualified uses, escaping, and deterministic output. Real-paper trial artifacts and local installation data are not part of this repository.

The root [SKILL.md](SKILL.md) is the skill entry point. `scripts/` contains validation, math conversion, and rendering; `assets/archify/` holds the original viewer files; `references/` documents dataset authoring.

## License and attribution

This project is MIT licensed; see [LICENSE](LICENSE). Archify's MIT notices and the embedded font's SIL Open Font License are preserved. Exact bundled-asset provenance is in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
