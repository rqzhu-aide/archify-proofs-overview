# Third-party notices

The proof overview reuses the actual standalone viewer and template helpers from [tt-a1i/archify](https://github.com/tt-a1i/archify). The five files in `assets/archify/` are copied without modification from the locally installed Archify skill on 2026-09-12. The installed skill declares version **2.17**; its template generator string is **2.17.0-dev.1**. This records the exact installed source, not a claim that it equals a particular upstream Git commit.

Archify is MIT licensed, with copyright notices for tt-a1i (2026) and Cocoon AI (2025). The full notice is preserved in [assets/archify/LICENSE](assets/archify/LICENSE). Its embedded JetBrains Mono font uses the SIL Open Font License, preserved in [assets/archify/JetBrainsMono-OFL.txt](assets/archify/JetBrainsMono-OFL.txt) and the template itself.

| Vendored file | Original Archify path | SHA-256 |
| --- | --- | --- |
| `template.html` | `assets/template.html` | `b3583470b9ec789418207963405252141c710b1b8a5c08a6cfd03156712f3f7d` |
| `utils.mjs` | `renderers/shared/utils.mjs` | `20124265300eb286db184a053561005cc32b6128dc1bc1b3f9520ffa8d43a241` |
| `i18n.mjs` | `renderers/shared/i18n.mjs` | `0d9e839c3e5346160b0028d07fcf78c86671189e0714ea069dc99913e859fcdb` |
| `LICENSE` | `LICENSE` | `b799ab081703e7821ae5096d2c1abdf14bbdc75e8ea1c4045998c36ed6db9706` |
| `JetBrainsMono-OFL.txt` | `assets/JetBrainsMono-OFL.txt` | `c1ab7c666206842a02b35b30770dac0d7a10156ed401c9defc3f02a754d89e90` |

The proof-specific SVG, mathematical item palette, dataset layout, statement panels, and hover integration are implemented in `scripts/render.mjs`. Its rounded path construction is adapted from Archify's `renderers/shared/geometry.mjs`, under the same MIT notice. The Archify schema, brand registry, CLI, update checker, and industrial component categories are not part of this adapter.

The output uses Archify's existing viewer interactions. The proof adapter does not claim Archify showcase validation, mathematical verification, or browser acceptance merely because rendering succeeds.
