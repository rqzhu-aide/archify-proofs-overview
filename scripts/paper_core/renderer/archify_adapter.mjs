// Audit-specific content slots and focus bridge for the actual bundled Archify viewer.
// The viewer owns its camera, selection, traces, themes, presentation and exports.
import { readFileSync } from 'node:fs';
import { applyTemplate, esc } from './assets/archify/utils.mjs';

function replaceOnce(template, original, replacement) {
  if (template.split(original).length !== 2) throw new Error(`Archify adaptation point changed: ${original}`);
  return template.replace(original, () => replacement);
}

export function archifyPage({ templatePath, title, svg, beforeGraph, afterGraph, css, runtime, data, fontStyle, index }) {
  let template = readFileSync(templatePath, 'utf8');
  // The native selection panel belongs below a mathematical graph, outside its clipped camera.
  // These are the same layout adaptations used by archify-proofs-overview.
  template = replaceOnce(template, 'if (chip.hidden || activeIds.length !== 1) return;',
    'if (chip.hidden || !container.contains(chip) || activeIds.length !== 1) return;');
  template = replaceOnce(template, 'if (chip && !chip.hidden) {',
    'if (chip && !chip.hidden && container.contains(chip)) {');
  template = replaceOnce(template, 'if (!passport || passport.hidden || passportYielded) return passportYielded;',
    'if (!passport || passport.hidden || !container.contains(passport) || passportYielded) return passportYielded;');
  template = replaceOnce(template, 'var desiredWidth = availableSvgHeight * ratio + chrome.diagramX;',
    'var desiredWidth = maxWidth;');
  template = replaceOnce(template, 'settleOverflow(minWidth);',
    '// Audit evidence extends the document without resizing the graph camera.');
  // Keep the native SVG/raster export pipeline, including the audit's kind and verdict styles.
  template = replaceOnce(template, String.raw`\.c-|\.t-|\.a-|\.m-`,
    String.raw`\.c-|\.t-|\.a-|\.m-|\.proof-|\.k-`);
  template = replaceOnce(template, "probe.setAttribute('data-theme', themeAttr);",
    "probe.setAttribute('data-theme', themeAttr); probe.className = 'proof-svg';");
  template = replaceOnce(template, "var themeHost = document.querySelector('[data-theme=\"' + theme + '\"]') || document.documentElement;",
    "var themeHost = document.querySelector('svg.proof-svg') || document.documentElement;");
  template = template.replace(/<style id="archify-fonts">[\s\S]*?<\/style>/, () => fontStyle);
  let html = applyTemplate(template, { title, subtitle: '', svg, cards: afterGraph, locale: 'en' });
  html = html.replace('<div class="diagram-container"', () => `${beforeGraph}\n<div class="diagram-container"`);
  html = html.replace('</head>', () => `<style id="proof-style">${css}\n${ADAPTER_CSS}</style>\n</head>`);
  html = html.replace('<body>', () => `<body data-proof-layout="${index ? 'index' : 'dag'}"><script>document.documentElement.setAttribute('data-js','');</script>`);
  html = html.replace('</body>', () => `${data}<script id="proof-runtime">${runtime}</script></body>`);
  return html;
}

export function mainNavigation(nodes, connections, requested = []) {
  const ids = new Set(nodes.map((node) => node.id));
  const selected = requested.filter((id) => ids.has(id));
  const main = selected.length ? selected : nodes.filter((node) => !connections.some((edge) => edge.from === node.id)).map((node) => node.id);
  const chosen = main.length ? main : nodes.map((node) => node.id);
  return `<section class="proof-main-navigation" aria-labelledby="proof-main-title"><h2 id="proof-main-title">${selected.length ? 'Main results' : 'Results to explore'}</h2><div class="proof-main-list">${chosen.map((id) => {
    const node = nodes.find((candidate) => candidate.id === id);
    return `<button type="button" data-proof-main="${esc(id)}" class="k-${esc(node.kind)}"><strong>${esc(node.label)}</strong><span>${esc(node.caption)}</span></button>`;
  }).join('')}</div><div class="proof-view-controls"><button id="proof-full-structure" type="button">Fit complete structure (${nodes.length} items)</button><button id="proof-readable-view" type="button">Readable view</button><p id="proof-view-status" role="status">Select a result or connection to inspect its evidence below the graph.</p></div><p class="proof-trace-note">Tracing follows recorded connections, including alternative routes. It identifies supporting or potentially affected results; it does not establish a complete or verified proof route.</p></section>`;
}

const ADAPTER_CSS = `
.proof-reader{font:14px/1.6 system-ui,sans-serif;margin:14px 0;min-width:0}
.proof-reader .proof-panel{border-radius:8px;padding:14px}
.proof-main-navigation{margin:14px 0;font:13px/1.5 system-ui,sans-serif}
.proof-main-navigation h2{font-size:15px;margin:10px 0}
.proof-main-list{display:flex;flex-wrap:wrap;gap:9px}
.proof-main-list button{display:flex;flex-direction:column;gap:4px;min-width:175px;max-width:290px;text-align:left;background:var(--panel);color:var(--text);border:1px solid var(--panel-border);border-left:3px solid var(--tone);border-radius:7px;padding:10px 13px;font:inherit;cursor:pointer}
.proof-main-list button span{color:var(--text-muted)}
.proof-main-list button[aria-pressed="true"]{outline:2px solid var(--tone);outline-offset:2px}
.proof-view-controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:10px 0}
.proof-view-controls button{background:var(--panel);color:var(--text);border:1px solid var(--panel-border);border-radius:5px;padding:6px 10px;font:inherit;cursor:pointer}
.proof-view-controls p,.proof-trace-note{font-size:11px;color:var(--text-muted);margin:4px 0}
.diagram-container>svg.proof-svg{width:100%;height:auto;max-width:none;max-height:620px;display:block}
.diagram-container svg.proof-svg .proof-grid-fill{display:none}
.diagram-container{scroll-margin-top:80px}
#focus-chip{position:relative;inset:auto!important;width:100%;max-width:none;max-height:none;margin:12px 0;box-sizing:border-box}
#focus-chip[hidden]{display:none}
#focus-id,#focus-tag,#focus-brand,#focus-context,#focus-summary,#relationship-lens-list,#btn-focus-relations{display:none!important}
#focus-chip .relationship-lens-head{flex-shrink:0}
body[data-proof-layout="index"] .diagram-container,body[data-proof-layout="index"] .proof-view-controls{display:none}
svg[data-focus-active] .proof-edge-badge{opacity:.13}svg[data-reach-active] .proof-edge-badge{opacity:.09}
svg .proof-edge-badge[data-proof-emphasized]{opacity:1}
.proof-build-disclosure{margin:10px 0;color:var(--text-muted)}
.proof-build-disclosure>summary{cursor:pointer;font-size:12px}
@media print{.proof-main-navigation,#focus-chip{display:none!important}.proof-reader{font-size:11px}.diagram-container>svg.proof-svg{max-height:none}}
`;
