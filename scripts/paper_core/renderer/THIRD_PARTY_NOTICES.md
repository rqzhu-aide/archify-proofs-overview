# Third-party notices

The files under `assets/archify/` are vendored verbatim from the
`proof-graphify` package in this repository, which in turn vendors
them from Archify (MIT). The renderer uses the actual template, viewer runtime,
and embedded WOFF2 font data (JetBrains Mono, SIL Open Font License 1.1).
Every output is self-contained and makes no network request.

Upstream copyright: Archify contributors (tt-a1i 2026; Cocoon AI 2025), MIT
License; JetBrains Mono (c) 2020 The JetBrains Mono Project Authors, SIL OFL 1.1.

| Vendored file | Original path in Archify | License | SHA-256 |
| --- | --- | --- | --- |
| `assets/archify/template.html` | `assets/template.html` | MIT (fonts inside: SIL OFL 1.1) | `b3583470b9ec789418207963405252141c710b1b8a5c08a6cfd03156712f3f7d` |
| `assets/archify/utils.mjs` | `renderers/shared/utils.mjs` | MIT | `20124265300eb286db184a053561005cc32b6128dc1bc1b3f9520ffa8d43a241` |
| `assets/archify/i18n.mjs` | `renderers/shared/i18n.mjs` | MIT | `0d9e839c3e5346160b0028d07fcf78c86671189e0714ea069dc99913e859fcdb` |
| `assets/archify/LICENSE` | `LICENSE` | MIT (license text) | `b799ab081703e7821ae5096d2c1abdf14bbdc75e8ea1c4045998c36ed6db9706` |
| `assets/archify/JetBrainsMono-OFL.txt` | `assets/JetBrainsMono-OFL.txt` | SIL OFL 1.1 (license text) | `c1ab7c666206842a02b35b30770dac0d7a10156ed401c9defc3f02a754d89e90` |

What `render_projection.mjs` takes from these files:

- `utils.mjs`: the `esc`, `textUnits`, and `applyTemplate` helpers.
- `i18n.mjs`: localized viewer labels via `applyTemplate`.
- `template.html`: the actual viewer shell, camera, selection, dependency
  tracing, presentation, themes, and export runtime. `archify_adapter.mjs`
  applies the overview's reader placement adaptations and includes audit
  styles in the existing export pipeline. The vendored source stays unchanged.
  The embedded fonts are checked to contain only data URLs.
- Layout constants (`kinds` palette, `box` metrics), the longest-path layering,
  orthogonal edge routing, badge placement and `jsonForScript` in
  `render_projection.mjs` are adapted from
  `proof-graphify/scripts/render.mjs` (MIT, same authors).

`selftest.mjs` recomputes the hashes above and fails if any vendored file
changed without this table being updated.
