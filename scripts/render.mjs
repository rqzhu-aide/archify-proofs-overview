// Proof-specific adapter for the vendored Archify viewer. See THIRD_PARTY_NOTICES.md.
// Input is the validated, MathML-enriched dataset produced by proof_overview.py.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { applyTemplate, esc, textUnits } from '../assets/archify/utils.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const kinds = {
  assumption: ['Assumption', '#60a5fa', '#2563eb'],
  definition: ['Definition', '#94a3b8', '#475569'],
  lemma: ['Lemma', '#c4b5fd', '#7c3aed'],
  proposition: ['Proposition', '#a5b4fc', '#4f46e5'],
  theorem: ['Theorem', '#e879f9', '#a21caf'],
  corollary: ['Corollary', '#67e8f9', '#0e7490'],
  external_result: ['External result', '#cbd5e1', '#64748b'],
};
const box = { w: 170, h: 64, column: 275, row: 108, margin: 36 };
const useTypes = { dependency: 'Dependency', definition: 'Definition', proof_argument: 'Proof argument' };

function jsonForScript(value) {
  return JSON.stringify(value).replaceAll('<', '\\u003c').replaceAll('>', '\\u003e').replaceAll('&', '\\u0026');
}

function safeHref(value) {
  if (typeof value !== 'string' || /[\u0000-\u0020]/u.test(value)) return '';
  return /^(?:https?:\/\/|file:\/\/\/|#)/i.test(value) ? value : '';
}

function formulaHtml(markup) {
  return String(markup).replace(/<math\b[\s\S]*?<\/math>/g, (math) => `<span class="proof-formula${/\bdisplay="block"/.test(math) ? ' proof-formula-block' : ''}">${math}</span>`);
}

function replaceTemplateOnce(template, original, replacement) {
  if (template.split(original).length !== 2) throw new Error('Vendored viewer changed at a proof-specific adaptation point. Review the viewer adapter before rendering.');
  return template.replace(original, () => replacement);
}

function preparedDataset(input) {
  if (!input || !Array.isArray(input.items) || !Array.isArray(input.uses) || !input.items.length) {
    throw new Error('Prepared input needs nonempty items and a uses array.');
  }
  const ids = new Set();
  for (const item of input.items) {
    if (!item || !/^[a-zA-Z][a-zA-Z0-9_.:-]*$/.test(item.id || '') || ids.has(item.id)) {
      throw new Error('Each item needs a unique safe identifier.');
    }
    if (!Object.hasOwn(kinds, item.kind) || !item.label || typeof item.statement_html !== 'string') {
      throw new Error(`Item ${item.id} needs kind, label, and prepared statement_html.`);
    }
    ids.add(item.id);
  }
  const useIds = new Set();
  const uses = input.uses.map((use, index) => {
    if (!use || !ids.has(use.from) || !ids.has(use.to) || !use.reason || (use.from === use.to && input.graph_mode !== 'index')) {
      throw new Error(`Dependency ${index + 1} needs valid endpoints and a reason; self references require index mode.`);
    }
    const id = use.id || `use-${index + 1}`;
    if (!/^[a-zA-Z][a-zA-Z0-9_.:-]*$/.test(id) || useIds.has(id)) throw new Error('Dependency identifiers must be safe and unique.');
    useIds.add(id);
    const type = use.type || 'dependency';
    if (!Object.hasOwn(useTypes, type)) throw new Error(`Dependency ${id} has an unsupported type.`);
    if (use.regime !== undefined && (typeof use.regime !== 'string' || !use.regime.trim())) throw new Error(`Dependency ${id} needs a nonempty regime.`);
    return { ...use, id, type };
  });
  if (input.main_items !== undefined && (!Array.isArray(input.main_items) || !input.main_items.length || new Set(input.main_items).size !== input.main_items.length || input.main_items.some((id) => !ids.has(id)))) throw new Error('main_items must contain distinct existing item identifiers.');
  if (input.graph_mode !== undefined && !['dag', 'index'].includes(input.graph_mode)) throw new Error('graph_mode must be dag or index.');
  return { ...input, uses };
}

function indexGraph(data) {
  const nodes = new Map(data.items.map((item) => [item.id, item]));
  const incoming = new Map(data.items.map((item) => [item.id, []]));
  const outgoing = new Map(data.items.map((item) => [item.id, []]));
  data.uses.forEach((use) => { incoming.get(use.to).push(use); outgoing.get(use.from).push(use); });
  return { nodes, incoming, outgoing };
}

// Longest-path layers preserve prerequisite direction. Ordering uses stable
// barycentres, never changes the graph, and needs no authored coordinates.
function layoutGraph(data) {
  const nodes = new Map(data.items.map((item, order) => [item.id, { ...item, order, rank: 0 }]));
  const incoming = new Map(data.items.map((item) => [item.id, []]));
  const outgoing = new Map(data.items.map((item) => [item.id, []]));
  data.uses.forEach((use) => { incoming.get(use.to).push(use); outgoing.get(use.from).push(use); });
  const remaining = new Map([...incoming].map(([id, uses]) => [id, uses.length]));
  const queue = data.items.filter((item) => !remaining.get(item.id)).map((item) => item.id);
  let visited = 0;
  for (let index = 0; index < queue.length; index += 1) {
    const id = queue[index];
    visited += 1;
    for (const use of outgoing.get(id)) {
      nodes.get(use.to).rank = Math.max(nodes.get(use.to).rank, nodes.get(id).rank + 1);
      remaining.set(use.to, remaining.get(use.to) - 1);
      if (!remaining.get(use.to)) queue.push(use.to);
    }
  }
  if (visited !== nodes.size) throw new Error('Dependencies contain a cycle. Review the recorded uses before rendering.');
  // Independent connected arguments occupy separate bands. Within a band,
  // integral rows leave shared clear corridors for skipped-layer connections.
  const components = [], assigned = new Set();
  nodes.forEach((start) => {
    if (assigned.has(start.id)) return;
    const ids = [start.id]; assigned.add(start.id);
    for (let i = 0; i < ids.length; i += 1) {
      for (const use of [...incoming.get(ids[i]), ...outgoing.get(ids[i])]) {
        const other = use.from === ids[i] ? use.to : use.from;
        if (!assigned.has(other)) { assigned.add(other); ids.push(other); }
      }
    }
    const members = ids.map((id) => nodes.get(id)).sort((a, b) => a.order - b.order);
    const ranks = Array.from({ length: Math.max(...members.map((node) => node.rank)) + 1 }, () => []);
    members.forEach((node) => ranks[node.rank].push(node));
    ranks.forEach((rank) => rank.forEach((node, index) => { node.position = index; }));
    for (let round = 0; round < 4; round += 1) {
      for (let r = 1; r < ranks.length; r += 1) {
        const centre = (node) => {
          const parents = incoming.get(node.id).map((use) => nodes.get(use.from).position);
          return parents.length ? parents.reduce((sum, value) => sum + value, 0) / parents.length : node.position;
        };
        ranks[r].sort((a, b) => centre(a) - centre(b) || a.order - b.order);
        ranks[r].forEach((node, index) => { node.position = index; });
      }
    }
    components.push({ members, ranks, rowCount: Math.max(...ranks.map((rank) => rank.length)) });
  });
  const longUses = data.uses.filter((use) => nodes.get(use.to).rank > nodes.get(use.from).rank + 1);
  let top = box.margin;
  components.forEach((component, componentIndex) => {
    component.top = top; component.nodeTop = top + (components.length > 1 ? 32 : 0);
    component.ranks.forEach((rank, r) => rank.forEach((node, index) => {
      node.x = box.margin + r * box.column;
      node.y = component.nodeTop + index * box.row;
      node.width = box.w; node.height = box.h; node.component = componentIndex;
    }));
    component.bottom = component.nodeTop + (component.rowCount - 1) * box.row + box.h;
    top = component.bottom + 68;
  });
  const rankCount = Math.max(...components.map((component) => component.ranks.length));
  return {
    nodes, components, incoming, outgoing, longUses,
    width: box.margin * 2 + (rankCount - 1) * box.column + box.w,
    height: top - 68 + box.margin,
  };
}

function roundedPath(points, radius = 8) {
  const commands = [`M ${points[0][0]} ${points[0][1]}`];
  for (let i = 1; i < points.length - 1; i += 1) {
    const [px, py] = points[i - 1], [cx, cy] = points[i], [nx, ny] = points[i + 1];
    const before = Math.hypot(cx - px, cy - py), after = Math.hypot(nx - cx, ny - cy);
    const r = Math.min(radius, before / 2, after / 2);
    if (r < 1) { commands.push(`L ${cx} ${cy}`); continue; }
    commands.push(`L ${cx - (cx - px) / before * r} ${cy - (cy - py) / before * r}`);
    commands.push(`Q ${cx} ${cy} ${cx + (nx - cx) / after * r} ${cy + (ny - cy) / after * r}`);
  }
  commands.push(`L ${points.at(-1).join(' ')}`);
  return commands.join(' ');
}

function edgePoints(use, graph) {
  const a = graph.nodes.get(use.from), b = graph.nodes.get(use.to);
  const outs = graph.outgoing.get(a.id), ins = graph.incoming.get(b.id);
  const port = (node, records) => node.y + 13 + (records.indexOf(use) + 1) / (records.length + 1) * (box.h - 26);
  const start = [a.x + box.w, port(a, outs)], end = [b.x, port(b, ins)];
  const longIndex = graph.longUses.indexOf(use);
  if (longIndex >= 0) {
    const component = graph.components[a.component];
    const target = (start[1] + end[1]) / 2;
    const corridors = Array.from({ length: component.rowCount }, (_, row) => component.nodeTop + row * box.row + box.h + (box.row - box.h) / 2);
    const rail = corridors.reduce((best, value) => Math.abs(value - target) < Math.abs(best - target) ? value : best);
    const exit = start[0] + 18 + (outs.indexOf(use) + 1) / (outs.length + 1) * 24;
    const enter = end[0] - 18 - (ins.indexOf(use) + 1) / (ins.length + 1) * 24;
    return [start, [exit, start[1]], [exit, rail], [enter, rail], [enter, end[1]], end];
  }
  const sameGap = [...graph.outgoing.values()].flat().filter((entry) => {
    return graph.nodes.get(entry.from).component === a.component && graph.nodes.get(entry.from).rank === a.rank && graph.nodes.get(entry.to).rank === b.rank;
  });
  const channel = start[0] + 25 + (sameGap.indexOf(use) + 1) / (sameGap.length + 1) * (box.column - box.w - 50);
  return [start, [channel, start[1]], [channel, end[1]], end];
}

function captionLines(text, limit = 25) {
  const words = String(text || '').split(/\s+/u).filter(Boolean);
  const lines = [''];
  for (const word of words) {
    const at = lines.length - 1;
    if (lines[at] && textUnits(`${lines[at]} ${word}`) > limit) lines.push(word);
    else lines[at] += (lines[at] ? ' ' : '') + word;
  }
  if (lines.length > 2) return [lines[0], `${lines[1].slice(0, limit - 1)}…`];
  return lines;
}

function renderQualifiers(qualifiers, graph) {
  const occupied = [...graph.nodes.values()].map((node) => ({ x: node.x - 6, y: node.y - 6, w: box.w + 12, h: box.h + 12 }));
  const intersects = (a, b) => a.x < b.x + b.w + 3 && a.x + a.w + 3 > b.x && a.y < b.y + b.h + 3 && a.y + a.h + 3 > b.y;
  return qualifiers.map(({ use, description, labels, points }) => {
    const w = Math.max(...labels.map((label) => textUnits(label))) * 5.7 + 12, h = labels.length * 14 + 6;
    const candidates = [];
    for (let i = 1; i < points.length; i += 1) {
      const a = points[i - 1], b = points[i], length = Math.hypot(b[0] - a[0], b[1] - a[1]);
      if (length < 8) continue;
      for (const fraction of [0.5, 0.25, 0.75, 0.1, 0.9]) {
        const anchor = [a[0] + (b[0] - a[0]) * fraction, a[1] + (b[1] - a[1]) * fraction];
        for (const offset of [0, -h - 8, h + 8, -2 * h - 16, 2 * h + 16, -3 * h - 24, 3 * h + 24]) {
          const x = anchor[0], y = anchor[1] + offset;
          const rect = { x: x - w / 2, y: y - h / 2, w, h };
          if (rect.x < 8 || rect.x + w > graph.width - 8 || rect.y < 8 || rect.y + h > graph.height - 8) continue;
          candidates.push({ x, y, rect, anchor, cost: Math.abs(offset) + Math.abs(fraction - 0.5) * 18 + (b[1] === a[1] ? 0 : 3) });
        }
      }
    }
    candidates.sort((a, b) => a.cost - b.cost);
    let placed = candidates.find((candidate) => !occupied.some((entry) => intersects(candidate.rect, entry)));
    if (!placed) {
      // Dense annotations can use the free margin with a visible leader. No
      // relationship is silently omitted or replaced with a shortcut.
      const anchor = points[Math.floor(points.length / 2)];
      for (let y = 18 + h / 2; !placed && y < graph.height - h / 2; y += h + 7) {
        for (let x = 8 + w / 2; !placed && x < graph.width - w / 2; x += w + 8) {
          const rect = { x: x - w / 2, y: y - h / 2, w, h };
          if (!occupied.some((entry) => intersects(rect, entry))) placed = { x, y, rect, anchor };
        }
      }
    }
    if (!placed) {
      const anchor = points[Math.floor(points.length / 2)], x = Math.min(graph.width - w / 2 - 8, Math.max(w / 2 + 8, anchor[0])), y = graph.height + h / 2;
      placed = { x, y, anchor, rect: { x: x - w / 2, y: y - h / 2, w, h } };
      graph.height += h + 12;
    }
    occupied.push(placed.rect);
    const { x, y, anchor } = placed, leader = Math.hypot(x - anchor[0], y - anchor[1]) > 3;
    return `${leader ? `<g aria-hidden="true" class="proof-badge-decoration"><path class="proof-badge-leader" d="M ${anchor[0]} ${anchor[1]} L ${x} ${y}"/><circle class="proof-badge-anchor" cx="${anchor[0]}" cy="${anchor[1]}" r="2"/></g>` : ''}<g class="proof-edge-badge" data-proof-use="${esc(use.id)}" tabindex="0" role="button" aria-label="${esc(description)}"><title>${esc(description)}</title><rect x="${x - w / 2}" y="${y - h / 2}" width="${w}" height="${h}" rx="4"/>${labels.map((label, i) => `<text x="${x}" y="${y - (labels.length - 1) * 7 + i * 14 + 3}" text-anchor="middle">${esc(label)}</text>`).join('')}</g>`;
  }).join('');
}

function renderSvg(data, graph) {
  const qualifiers = [];
  const edges = data.uses.map((use, index) => {
    const points = edgePoints(use, graph);
    const uncertain = Boolean(use.uncertainty);
    const qualification = `${useTypes[use.type]}${use.regime ? `; only in regime: ${use.regime}` : ''}`;
    const description = `${graph.nodes.get(use.from).label} to ${graph.nodes.get(use.to).label}. ${qualification}: ${use.reason}${uncertain ? ` Uncertain: ${use.uncertainty}` : ''}`;
    if (use.type !== 'dependency' || use.regime) {
      const labels = [use.type !== 'dependency' ? useTypes[use.type] : '', use.regime ? `If: ${use.regime}` : ''].filter(Boolean).map((label) => textUnits(label) > 17 ? `${label.slice(0, 15)}…` : label);
      qualifiers.push({ use, description, labels, points });
    }
    return `<path data-edge-from="${esc(use.from)}" data-edge-to="${esc(use.to)}" data-edge-key="${index}" data-edge-id="${esc(use.id)}" data-use-type="${esc(use.type)}"${use.regime ? ` data-use-regime="${esc(use.regime)}"` : ''} data-edge-label="${esc(`${qualification}: ${use.reason}`)}" data-composition-points="${points.map((point) => point.join(',')).join(';')}" class="proof-edge a-default${uncertain ? ' proof-uncertain' : ''}${use.type === 'proof_argument' ? ' proof-argument-edge' : ''}" d="${roundedPath(points)}" stroke-width="1.6" marker-end="url(#proof-arrow)"><title>${esc(description)}</title></path>`;
  }).join('\n');
  const nodes = [...graph.nodes.values()].map((node) => {
    const caption = captionLines(node.caption), cx = node.x + box.w / 2;
    const labelSize = Math.max(12, Math.min(14, 150 / Math.max(1, textUnits(node.label)) / 0.61));
    const aria = `${node.label}. ${node.caption || kinds[node.kind][0]}. Select for full statement and dependencies.`;
    return `<g id="node-${esc(node.id)}" data-node-id="${esc(node.id)}" data-node-kind="${esc(node.kind)}" data-node-label="${esc(node.label)}" data-node-sublabel="${esc(node.caption || '')}" data-node-context="${esc(node.source_display || '')}" tabindex="0" role="button" aria-pressed="false" aria-label="${esc(aria)}">
      <title>${esc(`${node.label}: ${node.caption || ''}`)}</title>
      <rect x="${node.x}" y="${node.y}" width="${box.w}" height="${box.h}" rx="6" class="c-mask"/>
      <rect x="${node.x}" y="${node.y}" width="${box.w}" height="${box.h}" rx="6" class="proof-node c-${esc(node.kind)}" stroke-width="1.5"/>
      <text data-node-label="" x="${cx}" y="${node.y + 23}" class="t-primary" font-size="${labelSize}" font-weight="600" text-anchor="middle"${textUnits(node.label) * labelSize * 0.61 > 154 ? ' textLength="154" lengthAdjust="spacingAndGlyphs"' : ''}>${esc(node.label)}</text>
      ${caption.map((line, i) => `<text data-detail="context" x="${cx}" y="${node.y + 41 + 13 * i}" class="t-muted" font-size="11" text-anchor="middle">${esc(line)}</text>`).join('')}
    </g>`;
  }).join('\n');
  const qualifierSvg = renderQualifiers(qualifiers, graph);
  return `<svg viewBox="0 0 ${graph.width} ${graph.height}" role="img" aria-labelledby="archify-diagram-title archify-diagram-description" data-preset="classic" data-quality-profile="standard">
    <title id="archify-diagram-title">${esc(data.title)}</title><desc id="archify-diagram-description">Major mathematical items and their recorded prerequisite uses. This overview does not certify the proof.</desc>
    <defs><marker id="proof-arrow" markerWidth="8" markerHeight="6" refX="7.2" refY="3" orient="auto"><path d="M0 0 L8 3 L0 6 Z" class="proof-arrowhead"/></marker><pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse"><path d="M40 0 L0 0 0 40" class="c-grid" stroke-width="0.5"/></pattern></defs>
    <rect width="100%" height="100%" fill="url(#grid)"/>
    ${graph.components.length > 1 ? graph.components.map((component) => {
      const ends = component.members.filter((node) => !graph.outgoing.get(node.id).length).map((node) => node.label);
      return `<g class="proof-component" aria-hidden="true"><path d="M 22 ${component.top + 16} H ${graph.width - 22}"/><text x="${box.margin}" y="${component.top + 9}">Linked argument: ${esc(ends.join(', '))}</text></g>`;
    }).join('') : ''}${edges}${qualifierSvg}${nodes}
  </svg>`;
}

// These checks inspect the planned boxes and orthogonal route segments only.
// They do not claim text, badge, browser, or perceptual layout validation.
function geometryReceipt(data, graph) {
  const diagnostics = [], nodes = [...graph.nodes.values()];
  const messages = {
    'geometry/nonfinite-node': 'A displayed item has invalid coordinates or dimensions.',
    'geometry/node-clipping': 'A displayed item extends beyond the SVG viewBox.',
    'geometry/node-overlap': 'Two displayed item boxes overlap.',
    'geometry/nonfinite-route': 'A recorded use has a nonfinite route coordinate.',
    'geometry/route-through-node': 'A recorded use crosses an unrelated item box.',
  };
  const issue = (code, subject, evidence) => diagnostics.push({ code, severity: 'error', message: messages[code], subject, evidence, supportedFixes: [] });
  for (const node of nodes) {
    if (![node.x, node.y, node.width, node.height].every(Number.isFinite) || node.width <= 0 || node.height <= 0) {
      issue('geometry/nonfinite-node', { item: node.id }, {});
    } else if (node.x < 0 || node.y < 0 || node.x + node.width > graph.width || node.y + node.height > graph.height) {
      issue('geometry/node-clipping', { item: node.id }, { x: node.x, y: node.y, width: node.width, height: node.height, viewBox: [graph.width, graph.height] });
    }
  }
  for (let i = 0; i < nodes.length; i += 1) {
    for (let j = i + 1; j < nodes.length; j += 1) {
      const a = nodes[i], b = nodes[j];
      if (a.x < b.x + b.width && a.x + a.width > b.x && a.y < b.y + b.height && a.y + a.height > b.y) {
        issue('geometry/node-overlap', { items: [a.id, b.id] }, {});
      }
    }
  }
  for (const use of data.uses) {
    const points = edgePoints(use, graph);
    for (let i = 1; i < points.length; i += 1) {
      const [x1, y1] = points[i - 1], [x2, y2] = points[i];
      if (![x1, y1, x2, y2].every(Number.isFinite)) {
        issue('geometry/nonfinite-route', { use: use.id, segment: i - 1 }, {});
        continue;
      }
      for (const node of nodes) {
        if (node.id === use.from || node.id === use.to) continue;
        const horizontal = y1 === y2 && y1 > node.y && y1 < node.y + node.height && Math.max(x1, x2) > node.x && Math.min(x1, x2) < node.x + node.width;
        const vertical = x1 === x2 && x1 > node.x && x1 < node.x + node.width && Math.max(y1, y2) > node.y && Math.min(y1, y2) < node.y + node.height;
        if (horizontal || vertical) issue('geometry/route-through-node', { use: use.id, item: node.id, segment: i - 1 }, { from: points[i - 1], to: points[i] });
      }
    }
  }
  const checks = ['finite_node_geometry', 'node_overlaps', 'node_clipping', 'finite_route_geometry', 'routes_through_unrelated_nodes'];
  return { status: diagnostics.length ? 'fail' : 'pass', checks, diagnostics, limits: 'Computed node boxes and orthogonal route segments only; text, badge placement, rounded corners, browser behavior, and perceptual review are not checked.' };
}

function representationReceipt(data, html, graphMode) {
  const attributes = (tag) => Object.fromEntries([...tag.matchAll(/([\w:-]+)="([^"]*)"/g)].map((match) => [match[1], match[2]]));
  const nodes = [], uses = [];
  // Inspect the emitted drawing or index, not JSON, templates, or claimed counts.
  const representation = graphMode === 'dag' ? html.match(/<svg\b[\s\S]*?<\/svg>/)?.[0] || '' : html.match(/<details\b[^>]*id="proof-full-index"[\s\S]*?<\/main>/)?.[0] || '';
  for (const match of representation.matchAll(graphMode === 'dag' ? /<(?:g|path)\b[^>]*>/g : /<article\b[^>]*>/g)) {
    const attrs = attributes(match[0]);
    if (graphMode === 'dag') {
      if (Object.hasOwn(attrs, 'data-node-id')) nodes.push(attrs['data-node-id']);
      if (Object.hasOwn(attrs, 'data-edge-id')) uses.push({ id: attrs['data-edge-id'], from: attrs['data-edge-from'], to: attrs['data-edge-to'] });
    } else {
      if (Object.hasOwn(attrs, 'data-proof-index-item')) nodes.push(attrs['data-proof-index-item']);
      if (Object.hasOwn(attrs, 'data-proof-index-use')) uses.push({ id: attrs['data-proof-index-use'], from: attrs['data-proof-from'], to: attrs['data-proof-to'] });
    }
  }
  const expectedNodes = data.items.map(({ id }) => id).sort();
  const normalizedUses = (records) => records.map(({ id, from, to }) => ({ id, from, to })).sort((a, b) => a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
  const status = JSON.stringify(nodes.sort()) === JSON.stringify(expectedNodes) && JSON.stringify(normalizedUses(uses)) === JSON.stringify(normalizedUses(data.uses)) ? 'pass' : 'fail';
  return { status, expected_items: data.items.length, rendered_items: nodes.length, expected_uses: data.uses.length, rendered_uses: uses.length, representation: graphMode === 'dag' ? 'svg' : 'index', item_ids: nodes, uses: normalizedUses(uses) };
}

function sourceHtml(record) {
  const label = record.source_display || 'Source location not supplied';
  const href = safeHref(record.source_href);
  return `<p class="proof-source">${href ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>` : esc(label)}</p>`;
}

function passagesHtml(record) {
  const passages = Array.isArray(record.source_passages) && record.source_passages.length
    ? record.source_passages
    : record.source_excerpt ? [{ source_display: record.source_display, source_excerpt: record.source_excerpt }] : [];
  if (!passages.length) return '';
  return `<div class="proof-passages">${passages.map((passage) => {
    const verification = typeof passage.verification === 'string' ? { status: passage.verification } : passage.verification || {};
    const methods = { line_range: 'line range', tex_label: 'TeX label', pdf_page_bounds: 'physical PDF page bounds', entered_locator: 'entered locator' };
    const method = verification.method ? String(verification.method).split(',').map((value) => methods[value.trim()] || value.trim().replaceAll('_', ' ')).join(', ') : '';
    const status = verification.status === 'unverified' ? 'Locator not yet verified' : verification.status === 'checked' ? 'Locator checked' : verification.status ? `Locator check: ${String(verification.status).replaceAll('_', ' ')}` : '';
    const authoredRole = passage.role ? String(passage.role).replaceAll('_', ' ') : 'Source';
    const role = authoredRole[0].toUpperCase() + authoredRole.slice(1);
    return `<details><summary>${esc(role)} passage${passage.source_display ? `: ${esc(passage.source_display)}` : ''}</summary>${sourceHtml(passage)}${status ? `<p class="proof-hint">${esc(status)}${method ? ` (${esc(method)})` : ''}. This does not assess the mathematics.</p>` : ''}${verification.note ? `<p class="proof-hint">${esc(verification.note)}</p>` : ''}${passage.source_excerpt ? `<pre class="proof-excerpt">${esc(passage.source_excerpt)}</pre>` : '<p class="proof-hint">No source excerpt is available for this locator.</p>'}</details>`;
  }).join('')}</div>`;
}

function relationHtml(use, graph, incoming) {
  const other = graph.nodes.get(incoming ? use.from : use.to);
  return `<li><button type="button" data-proof-focus="${esc(other.id)}">${esc(other.label)}</button> ${qualificationHtml(use)}<div>${formulaHtml(use.reason_html || esc(use.reason))}</div>${use.uncertainty ? `<span class="proof-uncertainty"> Uncertain connection: ${esc(use.uncertainty)}</span>` : ''}${use.source_display ? `<small>${esc(use.source_display)}</small>` : ''}${passagesHtml(use)}</li>`;
}

function qualificationHtml(use) {
  return `<span class="proof-use-type">${esc(useTypes[use.type])}</span>${use.regime ? `<span class="proof-regime">Only in regime: ${esc(use.regime)}</span>` : ''}`;
}

function fullItemHtml(node, graph, { hover = false } = {}) {
  const incoming = graph.incoming.get(node.id), outgoing = graph.outgoing.get(node.id);
  if (hover) return `<strong>${esc(node.label)}</strong><p class="proof-hover-caption">${esc(node.caption || kinds[node.kind][0])}</p>${sourceHtml(node)}<p class="proof-hint">Click or press Enter for the full statement and dependencies.</p>`;
  return `
    ${node.statement_form === 'synopsis' ? '<p class="proof-hint proof-statement-form">Statement synopsis</p>' : ''}<div class="proof-statement">${formulaHtml(node.statement_html)}</div>${sourceHtml(node)}${node.uncertainty ? `<p class="proof-uncertainty">Extraction uncertainty: ${esc(node.uncertainty)}</p>` : ''}
      ${Array.isArray(node.aliases) && node.aliases.length ? `<p class="proof-hint">Also identified as: ${node.aliases.map((alias) => esc(alias)).join(', ')}</p>` : ''}${passagesHtml(node)}
      <div class="proof-dependencies"><h4>Prerequisites used (${incoming.length})</h4>${incoming.length ? `<ul>${incoming.map((use) => relationHtml(use, graph, true)).join('')}</ul><p class="proof-hint">These are recorded inputs to the argument. Their joint sufficiency has not been verified by this overview.</p>` : '<p>No prerequisite use is recorded in this overview.</p>'}
      <h4>Used by (${outgoing.length})</h4>${outgoing.length ? `<ul>${outgoing.map((use) => relationHtml(use, graph, false)).join('')}</ul>` : '<p>No downstream use is recorded in this overview.</p>'}</div>`;
}

function fullUseHtml(use, graph) {
  return `<h4>${esc(graph.nodes.get(use.from).label)} → ${esc(graph.nodes.get(use.to).label)}</h4>${qualificationHtml(use)}<div class="proof-statement">${formulaHtml(use.reason_html || esc(use.reason))}</div>${use.uncertainty ? `<p class="proof-uncertainty">Uncertain connection: ${esc(use.uncertainty)}</p>` : ''}${sourceHtml(use)}${passagesHtml(use)}<p class="proof-hint">This connection records a use in the argument. This overview does not verify that inference.</p>`;
}

function fullIndexHtml(data, graph, open = false) {
  return `<details class="proof-index" id="proof-full-index"${open ? ' open' : ''}><summary>Full statement index (${data.items.length} items, ${data.uses.length} recorded uses)</summary>${data.items.map((node) => `<article id="proof-index-item-${esc(node.id)}" data-proof-index-item="${esc(node.id)}"><h3>${esc(node.label)}${node.caption ? `: ${esc(node.caption)}` : ''}</h3>${fullItemHtml(node, graph)}</article>`).join('')}<h3>Recorded uses</h3>${data.uses.map((use) => `<article id="proof-index-use-${esc(use.id)}" data-proof-index-use="${esc(use.id)}" data-proof-from="${esc(use.from)}" data-proof-to="${esc(use.to)}">${fullUseHtml(use, graph)}</article>`).join('')}</details>`;
}

function recordsHtml(data, graphMode) {
  const records = { schema_version: data.schema_version || 1, graph_mode: graphMode, items: data.items, uses: data.uses, build_context: data.build_context || { mathematical_assessment: 'not_performed' } };
  return `<script id="proof-overview-records" type="application/json">${jsonForScript(records)}</script>`;
}

function buildContextHtml(data) {
  const context = data.build_context;
  if (!context) return '';
  const revision = typeof context.source_revision === 'string' ? context.source_revision : context.source_revision?.id;
  const sourceStatus = {
    current: 'The captured manuscript matches the registered files.',
    historical_changed: 'This overview uses an earlier captured manuscript; the registered files have changed.',
    historical_unavailable: 'This overview uses a captured manuscript whose original files are unavailable.',
    unregistered: 'No manuscript snapshot is registered.',
  }[context.source_status] || '';
  const comparisonStatus = typeof context.source_comparison === 'string' ? context.source_comparison : context.source_comparison?.status;
  const comparison = { complete: 'Source comparisons are complete.', incomplete: 'Some source comparisons remain incomplete.' }[comparisonStatus] || '';
  return `<p class="proof-hint">${revision ? `<span title="Captured revision ${esc(revision.slice(0, 12))}">Captured manuscript version.</span> ` : ''}${sourceStatus ? `${sourceStatus} ` : ''}${comparison ? `${comparison} ` : ''}Mathematical assessment has not been performed.</p>`;
}

function renderIndex(data) {
  const graph = indexGraph(data);
  const warnings = (data.warnings || []).map((warning) => `<li>${esc(warning)}</li>`).join('');
  const html = `<!doctype html><html lang="en" data-theme="light"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${esc(data.title)} | Proof overview index</title>${proofCss(data)}<style>
    :root{--text:#172033;--text-muted:#526075;--text-dim:#637086;--panel:#f8fafc;--panel-border:#d7dee8;--mask:#fff;--arrow:#64748b;color-scheme:light}html[data-theme="dark"]{--text:#e5eaf3;--text-muted:#adb8cb;--text-dim:#9caac0;--panel:#101827;--panel-border:#344155;--mask:#172132;color-scheme:dark}body{margin:0;background:var(--panel);color:var(--text);font:14px/1.6 system-ui,sans-serif}main{max-width:1000px;margin:auto;padding:28px 24px}h1{font-size:22px;line-height:1.3}.proof-index article{scroll-margin-top:18px}.proof-passages details{margin:8px 0}.proof-passages summary{cursor:pointer;font-size:12px;overflow-wrap:anywhere}.proof-index h4{font-size:13px}.proof-index-controls{display:flex;flex-wrap:wrap;gap:8px}.proof-index-controls a,.proof-index-controls button{font:inherit;color:var(--text);border:1px solid var(--panel-border);border-radius:5px;background:var(--mask);padding:5px 9px;text-decoration:none}@media print{main{max-width:none;padding:0}}
    </style></head><body><main><h1>${esc(data.title)}</h1><p class="proof-caption">${data.items.length} items · ${data.uses.length} recorded uses</p><p><strong>Index view.</strong> The recorded mapping is displayed as a complete index because the current diagram layout requires an acyclic graph. Cyclic mappings alone do not establish a circular proof.</p><p>${esc(data.scope || '')}</p>${buildContextHtml(data)}${warnings ? `<div class="proof-render-warnings"><ul>${warnings}</ul></div>` : ''}<nav class="proof-index-controls" aria-label="Index controls"><button type="button" id="proof-index-theme">Switch theme</button><a href="#proof-full-index">All statements and uses</a></nav>${fullIndexHtml(data, graph, true)}</main>${recordsHtml(data, 'index')}<script>(function(){document.getElementById('proof-index-theme').addEventListener('click',function(){document.documentElement.setAttribute('data-theme',document.documentElement.getAttribute('data-theme')==='dark'?'light':'dark');});document.addEventListener('click',function(event){var button=event.target.closest('[data-proof-focus]');if(!button)return;var article=document.getElementById('proof-index-item-'+button.getAttribute('data-proof-focus'));if(article){document.getElementById('proof-full-index').open=true;article.scrollIntoView({block:'start'});article.setAttribute('tabindex','-1');article.focus({preventScroll:true});}});})();</script></body></html>`;
  const graph_preservation = representationReceipt(data, html, 'index');
  if (graph_preservation.status !== 'pass') throw new Error('Rendered index does not preserve the supplied item and use identities.');
  return { html, items: data.items.length, uses: data.uses.length, viewBox: null, graph_mode: 'index', graph_preservation, geometry: { status: 'not_applicable', checks: [], diagnostics: [], reason: 'The complete records are shown as an index; no dependency geometry is drawn.' } };
}

function proofCss(data) {
  const badgeFocus = data.uses.filter((use) => use.type !== 'dependency' || use.regime).map((use) => `svg[data-focus-active]:not([data-reach-active]):has([data-edge-id="${use.id}"][data-focus-match]) [data-proof-use="${use.id}"],svg[data-reach-active]:has([data-edge-id="${use.id}"][data-reach-match]) [data-proof-use="${use.id}"]{opacity:1}`).join('');
  const palette = Object.entries(kinds).map(([kind, [, dark, light]]) => `
    [data-theme="dark"] .c-${kind}{--proof-tone:${dark}} [data-theme="light"] .c-${kind}{--proof-tone:${light}}
    .c-${kind}{stroke:var(--proof-tone,${dark});fill:color-mix(in srgb,var(--proof-tone,${dark}) 13%,transparent)}
    [data-theme="dark"] [data-kind="${kind}"]{--proof-kind:${dark}} [data-theme="light"] [data-kind="${kind}"]{--proof-kind:${light}}
    .overview-map-node[data-kind="${kind}"]{fill:var(--proof-kind,${dark});stroke:var(--proof-kind,${dark})}
    .semantic-lens-kind[data-kind="${kind}"]{--lens-color:var(--proof-kind,${dark})}
    .proof-legend [data-kind="${kind}"]::before{background:var(--proof-kind,${dark})}
  `).join('');
  return `<style id="proof-overview-style">
    ${palette}
    svg[data-focus-active] .proof-edge-badge{opacity:.13}svg[data-reach-active] .proof-edge-badge{opacity:.09}${badgeFocus}
    .proof-edge{fill:none;stroke:var(--arrow)} .proof-uncertain{stroke-dasharray:6 4}.proof-arrowhead{fill:var(--arrow)}
    .proof-argument-edge{stroke-width:2.2}.proof-edge-badge{cursor:pointer}.proof-edge-badge rect{fill:var(--mask);stroke:var(--panel-border)}.proof-edge-badge text{fill:var(--text-muted);font-size:10px}.proof-edge-badge:focus-visible rect{stroke:var(--text);stroke-width:2}.proof-badge-leader{fill:none;stroke:var(--arrow);stroke-width:1;stroke-dasharray:2 3;pointer-events:none}.proof-badge-anchor{fill:var(--arrow);pointer-events:none}.proof-component path{stroke:var(--panel-border);stroke-width:1}.proof-component text{fill:var(--text-muted);font-size:12px;paint-order:stroke;stroke:var(--panel);stroke-width:6px}
    .proof-use-type,.proof-regime{display:inline-block;font-size:10px;border:1px solid var(--panel-border);padding:2px 6px;border-radius:4px;margin:2px 3px;color:var(--text-muted)}
    .proof-navigation{margin:12px 0 14px}.proof-navigation h2{font-size:13px;color:var(--text);margin:0 0 8px}.proof-main-list{display:flex;gap:8px;overflow-x:auto;padding-bottom:4px;scrollbar-width:thin}.proof-main-list button{flex:0 0 auto;text-align:left;max-width:240px;padding:8px 12px;border-radius:6px;border:1px solid var(--panel-border);border-left:3px solid var(--proof-tone);background:var(--panel);color:var(--text);font:inherit;font-size:12px;cursor:pointer}.proof-main-list button strong{display:block;font-size:13px}.proof-main-list button span{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:11px;color:var(--text-muted);margin-top:4px}.proof-main-list button[aria-pressed="true"]{outline:1px solid var(--proof-tone)}
    .proof-view-controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 0}.proof-view-controls button{background:var(--panel);border:1px solid var(--panel-border);border-radius:5px;padding:6px 10px;color:var(--text);font:inherit;font-size:11px;cursor:pointer}.proof-view-controls button:focus-visible,.proof-main-list button:focus-visible{outline:2px solid var(--text);outline-offset:2px}.proof-view-controls p{font-size:11px;color:var(--text-muted);margin:0}
    .proof-scope{font-size:12px;line-height:1.6;color:var(--text-muted);margin:8px 0}.proof-scope summary{cursor:pointer}.proof-scope p{max-width:none}.proof-trace-note{font-size:11px;color:var(--text-muted);line-height:1.5;margin:8px 0}.diagram-container>svg{max-height:620px}.diagram-container{scroll-margin-top:80px}
    #focus-id,#focus-tag,#focus-brand,#focus-context,#focus-summary,#relationship-lens-list,#btn-focus-relations{display:none!important}
    .proof-caption{color:var(--text-muted);font-size:12px;line-height:1.6;margin:14px 0 0}.proof-caption p{margin:5px 0}
    .proof-legend{display:flex;gap:8px 17px;flex-wrap:wrap;padding:0;list-style:none;font-size:11px;color:var(--text-muted);margin:12px 0}
    .proof-legend li::before{content:'';display:inline-block;width:9px;height:9px;border-radius:3px;margin-right:6px}
    .proof-statement{font-family:Georgia,'Times New Roman',serif;font-size:16px;line-height:1.55;overflow-wrap:anywhere;color:var(--text);margin:10px 0}
    .proof-statement math[display="block"]{padding:4px 0}.proof-statement math{font-size:1.05em}
    .proof-formula{display:inline-block;max-width:100%;vertical-align:middle;overflow-x:auto;overflow-y:hidden;scrollbar-width:thin;scrollbar-color:var(--text-dim) var(--mask)}.proof-formula-block{display:block}
    .proof-source{font-size:11px;color:var(--text-muted);overflow-wrap:anywhere;margin:8px 0}.proof-source a{color:inherit;text-decoration:underline}
    #focus-chip{position:relative;inset:auto!important;display:flex;flex-direction:column;width:100%;max-width:none;max-height:none;box-sizing:border-box;margin:12px 0 0}#focus-chip[hidden]{display:none}
    #focus-chip .relationship-lens-head{flex-shrink:0}
    #proof-selection{flex:none;max-height:min(60vh,540px);overflow:auto;box-sizing:border-box;padding:0 14px 12px;margin-top:8px;min-width:0;scrollbar-width:thin;scrollbar-color:var(--text-dim) var(--mask)}
    .proof-hint,.proof-uncertainty{font-size:11px;color:var(--text-muted);line-height:1.55}.proof-dependencies{border-top:1px solid var(--panel-border);padding-top:10px;margin-top:10px}
    .proof-dependencies h4{margin:10px 0 6px;font-size:11px;color:var(--text)}.proof-dependencies p,.proof-dependencies li{font-size:11px;line-height:1.6;color:var(--text-muted)}
    .proof-dependencies ul{padding-left:18px;margin:6px 0}.proof-dependencies li{margin:5px 0}.proof-dependencies small{display:block;color:var(--text-dim)}
    [data-proof-focus]{border:0;background:transparent;color:var(--text);text-decoration:underline;font:inherit;cursor:pointer;padding:0}
    .proof-excerpt{font-size:11px;white-space:pre-wrap;overflow-wrap:anywhere;color:var(--text-muted);line-height:1.5}
    .proof-passages details{margin:8px 0}.proof-passages summary{cursor:pointer;font-size:11px;overflow-wrap:anywhere}.proof-statement-form{margin:8px 0 -4px;font-weight:600}
    #proof-tooltip{position:fixed;z-index:10000;width:320px;max-width:calc(100vw - 24px);box-sizing:border-box;padding:12px 15px;border:1px solid var(--panel-border);border-radius:8px;background:var(--mask);box-shadow:0 12px 35px #0003;pointer-events:none;color:var(--text);font-size:12px}#proof-tooltip[hidden]{display:none}.proof-hover-caption{margin:6px 0;line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}#proof-tooltip .proof-source{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}#proof-tooltip .proof-hint{margin:7px 0 0}
    .proof-index{margin:14px 0 0;color:var(--text-muted);font-size:12px}.proof-index>summary{cursor:pointer}.proof-index article{padding:16px 0;border-top:1px solid var(--panel-border);break-inside:avoid}.proof-index h3{font-size:14px;color:var(--text);margin:5px 0}
    .proof-attribution{font-size:10px;color:var(--text-dim);margin:15px 0 0}
    .proof-render-warnings{font-size:12px;line-height:1.6;color:var(--text);border:1px solid var(--panel-border);border-radius:8px;padding:10px 14px;margin:12px 0}.proof-render-warnings ul{padding-left:20px;margin:5px 0}.math-fallback{font-size:12px;white-space:pre-wrap;overflow-wrap:anywhere;border-bottom:1px dotted var(--text-muted)}
    @media(max-width:720px){.proof-main-list button{max-width:200px}.proof-view-controls p{width:100%}.diagram-container>svg{max-height:560px}}
    @media print{#proof-tooltip,#focus-chip,.proof-view-controls{display:none!important}.diagram-container>svg{max-height:none}.proof-index>summary{display:none}.proof-index article{display:block}.proof-index .proof-statement{color:#111}.proof-index details[open]>summary{display:none}}
  </style>`;
}

function proofRuntime(payload) {
  return `<div id="proof-tooltip" role="tooltip" hidden></div><script id="proof-overview-data" type="application/json">${jsonForScript(payload)}</script>
  <script>
  (function(){
    var data=JSON.parse(document.getElementById('proof-overview-data').textContent);
    var nodes=Object.create(null);data.items.forEach(function(item){nodes[item.id]=item;});
    var uses=Object.create(null);data.uses.forEach(function(use){uses[use.id]=use;});
    var svg=document.querySelector('.diagram-container svg'),tip=document.getElementById('proof-tooltip');
    // Keep explicit qualification controls above the upstream viewer's wide
    // edge hit targets so a badge always opens its own recorded connection.
    svg.querySelectorAll('.proof-edge-badge').forEach(function(badge){svg.appendChild(badge);});
    var panel=document.createElement('div');panel.id='proof-selection';panel.setAttribute('aria-label','Mathematical statement and recorded dependencies');
    var chip=document.getElementById('focus-chip'),head=chip.querySelector('.relationship-lens-head');
    // Mathematical details need their own reading space even when the graph
    // is only one or two rows high. Keep them outside the clipped canvas.
    svg.parentElement.insertAdjacentElement('afterend',chip);
    chip.removeAttribute('data-radar-yielded');chip.removeAttribute('aria-hidden');
    head.insertAdjacentElement('afterend',panel);
    chip.querySelector('.relationship-lens-eyebrow').textContent='Result details';
    chip.querySelector('.semantic-passport-reach-label').textContent='Trace dependencies';
    document.getElementById('btn-focus-clear').setAttribute('aria-label','Close result details');
    var current='',timer=null,focusFrame=null,keyboardNavigation=false,focusedNode=null;
    var status=document.getElementById('proof-view-status');
    function cancelPreview(){clearTimeout(timer);timer=null;if(focusFrame!==null)cancelAnimationFrame(focusFrame);focusFrame=null;tip.hidden=true;}
    function copyTemplate(id){var element=document.getElementById(id);return element?element.innerHTML:'';}
    function sync(){
      var active=Archify.focus.active(),relation=Archify.focus.relationship();
      var key=relation?'use:'+relation.id:(typeof active==='string'?'item:'+active:'');
      if(key){cancelPreview();focusedNode=null;}
      document.querySelectorAll('[data-proof-main]').forEach(function(button){button.setAttribute('aria-pressed',button.getAttribute('data-proof-main')===active?'true':'false');});
      if(key===current)return;current=key;
      if(relation&&uses[relation.id])panel.innerHTML=copyTemplate('proof-use-'+relation.id);
      else panel.innerHTML=typeof active==='string'?copyTemplate('proof-detail-'+active):'';
      panel.scrollTop=0;cancelPreview();
      if(key)chip.scrollIntoView({block:'nearest',behavior:'auto'});
    }
    new MutationObserver(sync).observe(svg,{subtree:true,attributes:true,attributeFilter:['data-focus-active','data-focus-selected','data-relationship-pinned']});
    function readableView(id){
      var first=svg.querySelector('g[data-node-id] text[data-node-label]');if(!first)return;
      var size=Math.min.apply(Math,Array.from(svg.querySelectorAll('g[data-node-id] text[data-node-label]')).map(function(label){return Number(label.getAttribute('font-size'))||14;})),viewBox=svg.viewBox.baseVal;
      var baseScale=Math.min(svg.clientWidth/viewBox.width,svg.clientHeight/viewBox.height),target=Math.max(1,Math.min(3,Math.ceil(12/(size*baseScale)*4)/4));
      var node=nodes[id]||{x:viewBox.width/2,y:viewBox.height/2};
      // Supply the scale in the same camera transaction. Repeated zoomIn calls
      // are sampled mid-transition by the viewer and can reset the next move.
      Archify.view.centerAt(node.x,node.y,{scale:target,minimumScale:target,instant:true});
      status.textContent='Readable view shows part of the complete graph. Drag the background or select a result to navigate.';
      cancelPreview();
    }
    function openResult(id){cancelPreview();Archify.focus.set(id,{toggle:false});sync();requestAnimationFrame(function(){readableView(id);chip.scrollIntoView({block:'nearest',behavior:'auto'});});}
    // The upstream viewer treats an otherwise unknown SVG target as a
    // background click. Own badge activation before that handler can clear it.
    document.addEventListener('click',function(event){var badge=event.target.closest('[data-proof-use]');if(!badge)return;event.preventDefault();event.stopPropagation();cancelPreview();Archify.focus.inspectRelationshipById(badge.getAttribute('data-proof-use'),{toggle:false});sync();},true);
    document.addEventListener('click',function(event){
      var button=event.target.closest('[data-proof-focus],[data-proof-main]'),use=event.target.closest('[data-proof-use]');
      if(button)openResult(button.getAttribute('data-proof-main')||button.getAttribute('data-proof-focus'));
      else if(use){cancelPreview();Archify.focus.inspectRelationshipById(use.getAttribute('data-proof-use'),{toggle:false});sync();}
      else setTimeout(sync,0);
    });
    document.addEventListener('keydown',function(event){keyboardNavigation=true;if(event.key==='Escape'){focusedNode=null;cancelPreview();}var badge=event.target.closest('[data-proof-use]');if(badge&&(event.key==='Enter'||event.key===' ')){event.preventDefault();badge.dispatchEvent(new MouseEvent('click',{bubbles:true}));}setTimeout(sync,0);});
    document.addEventListener('pointerdown',function(event){keyboardNavigation=false;focusedNode=null;cancelPreview();if(event.target.closest('[data-proof-use]'))event.stopPropagation();},true);
    function show(node){
      if(Archify.focus.active()||Archify.focus.relationship())return;
      tip.innerHTML=copyTemplate('proof-hover-'+node.getAttribute('data-node-id'));tip.hidden=false;
      var rect=node.getBoundingClientRect(),left=Math.max(12,Math.min(rect.left,window.innerWidth-tip.offsetWidth-12));
      var top=rect.bottom+10;if(top+tip.offsetHeight>window.innerHeight-12)top=Math.max(12,rect.top-tip.offsetHeight-10);
      tip.style.left=left+'px';tip.style.top=top+'px';
    }
    function showFocusedAfterScroll(){
      if(focusFrame!==null)cancelAnimationFrame(focusFrame);
      focusFrame=requestAnimationFrame(function(){focusFrame=null;if(focusedNode&&document.activeElement===focusedNode)show(focusedNode);});
    }
    svg.addEventListener('pointerover',function(event){var node=event.target.closest('[data-node-id]');if(!node||Archify.focus.active()||Archify.focus.relationship())return;cancelPreview();timer=setTimeout(function(){timer=null;show(node);},280);});
    svg.addEventListener('pointerout',function(event){var node=event.target.closest('[data-node-id]');if(node&&node.contains(event.relatedTarget))return;cancelPreview();});
    svg.addEventListener('focusin',function(event){var node=event.target.closest('[data-node-id]');focusedNode=keyboardNavigation?node:null;if(focusedNode)showFocusedAfterScroll();});
    svg.addEventListener('focusout',function(){focusedNode=null;cancelPreview();});
    function viewportChanged(){if(focusedNode&&document.activeElement===focusedNode)showFocusedAfterScroll();else cancelPreview();}
    window.addEventListener('resize',viewportChanged);window.addEventListener('scroll',viewportChanged,true);
    var index=document.getElementById('proof-full-index'),wasOpen=false;
    window.addEventListener('beforeprint',function(){wasOpen=index.open;index.open=true;});window.addEventListener('afterprint',function(){index.open=wasOpen;});
    document.getElementById('proof-full-structure').addEventListener('click',function(){
      cancelPreview();Archify.focus.clear();if(Archify.semanticLens)Archify.semanticLens.clear({preserveView:true});if(Archify.routeProbe)Archify.routeProbe.clear({restoreFocus:false});Archify.view.reset();sync();status.textContent='Complete structure fitted to the viewer. Choose Readable view or a result for larger text.';
    });
    document.getElementById('proof-readable-view').addEventListener('click',function(){var active=Archify.focus.active();readableView(typeof active==='string'?active:data.main_items[0]);});
    requestAnimationFrame(function(){if(!Archify.focus.active()&&!Archify.focus.relationship())readableView(data.main_items[0]);});
    sync();
  })();
  </script>`;
}

function render(input) {
  const data = preparedDataset(input);
  if (data.graph_mode === 'index') return renderIndex(data);
  const graph = layoutGraph(data);
  const mainItems = data.main_items || data.items.filter((item) => !graph.outgoing.get(item.id).length).map((item) => item.id);
  const hasRegimes = data.uses.some((use) => use.regime);
  const presentKinds = new Set(data.items.map((item) => item.kind));
  const legend = `<ul class="proof-legend" aria-label="Mathematical item types">${Object.entries(kinds).filter(([kind]) => presentKinds.has(kind)).map(([kind, [label]]) => `<li data-kind="${kind}">${label}</li>`).join('')}</ul>`;
  const cards = `<div class="proof-caption"><p>Arrows point from prerequisites to the results that use them. Badges distinguish definition uses, proof arguments, and regime-specific uses. Dashed arrows mark uncertain connections.</p></div>${legend}`;
  let template = fs.readFileSync(path.join(root, 'assets/archify/template.html'), 'utf8');
  template = replaceTemplateOnce(template,
    'meta.textContent = [viewerKindLabel(item.type), item.id, item.sublabel, item.tag]',
    'meta.textContent = [viewerKindLabel(item.type), item.context, item.sublabel, item.tag]');
  template = replaceTemplateOnce(template,
    'meta.title = [viewerKindLabel(item.type), item.id, item.context, item.sublabel, item.tag]',
    'meta.title = [viewerKindLabel(item.type), item.context, item.sublabel, item.tag]');
  // The proof inspector is docked below the canvas. Upstream overlay placement
  // and the overview map must not reserve canvas space for it or hide it.
  template = replaceTemplateOnce(template,
    'if (chip.hidden || activeIds.length !== 1) return;',
    'if (chip.hidden || !container.contains(chip) || activeIds.length !== 1) return;');
  template = replaceTemplateOnce(template,
    'if (chip && !chip.hidden) {',
    'if (chip && !chip.hidden && container.contains(chip)) {');
  template = replaceTemplateOnce(template,
    'if (!passport || passport.hidden || passportYielded) return passportYielded;',
    'if (!passport || passport.hidden || !container.contains(passport) || passportYielded) return passportYielded;');
  const svg = renderSvg(data, graph), geometry = geometryReceipt(data, graph);
  if (geometry.status !== 'pass') {
    const error = new Error('Proof diagram geometry checks failed. The previous artifact was preserved.');
    error.diagnostics = geometry.diagnostics; error.stage = 'geometry';
    throw error;
  }
  let html = applyTemplate(template, { title: data.title, subtitle: '', svg, cards, locale: 'en' });
  html = html.replace(/<title>[\s\S]*?<\/title>/, () => `<title>${esc(data.title)} | Proof overview</title>`);
  html = html.replace('</head>', () => `${proofCss(data)}</head>`);
  const warnings = Array.isArray(data.warnings) && data.warnings.length ? `<div class="proof-render-warnings" role="note"><strong>Source and rendering notes</strong><ul>${data.warnings.map((warning) => `<li>${esc(warning)}</li>`).join('')}</ul></div>` : '';
  const navigation = `<section class="proof-navigation" aria-labelledby="proof-main-title"><h2 id="proof-main-title">${data.main_items ? 'Main results' : 'Terminal results in the recorded graph'}</h2><div class="proof-main-list" id="proof-main-results">${mainItems.map((id) => {
    const node = graph.nodes.get(id);
    return `<button type="button" data-proof-main="${esc(id)}" class="c-${esc(node.kind)}" aria-pressed="false" title="${esc(`${node.label}: ${node.caption}. Open statement and supporting dependencies.`)}"><strong>${esc(node.label)}</strong><span>${esc(node.caption)}</span></button>`;
  }).join('')}</div><div class="proof-view-controls"><button type="button" id="proof-full-structure">Fit complete structure (${data.items.length} items)</button><button type="button" id="proof-readable-view">Readable view</button><p id="proof-view-status" aria-live="polite">Select a result to read its statement and explore its supporting items.</p></div>${hasRegimes ? '<p class="proof-trace-note" role="note">Some connections apply only in a named regime. Dependency tracing follows all recorded arrows, including alternative routes. Regime labels still apply; a trace is not one required or verified proof route.</p>' : ''}</section>`;
  html = html.replace('<div class="diagram-container"', () => `<div class="proof-caption"><p>${esc(data.source?.title || data.title)} · ${data.items.length} items · ${data.uses.length} recorded connections</p><p><strong>Dependency overview, not proof verification.</strong> Select a result for its statement, source, and supporting items.</p>${buildContextHtml(data)}</div><details class="proof-scope"><summary>Scope and reading limits</summary><p>${esc(data.scope)}</p></details>${warnings}${navigation}\n<div class="diagram-container"`);
  const fragments = data.items.map((node) => `<template id="proof-detail-${esc(node.id)}">${fullItemHtml(node, graph)}</template><template id="proof-hover-${esc(node.id)}">${fullItemHtml(node, graph, { hover: true })}</template>`).join('');
  const useFragments = data.uses.map((use) => `<template id="proof-use-${esc(use.id)}">${fullUseHtml(use, graph)}</template>`).join('');
  const index = `${fullIndexHtml(data, graph)}<p class="proof-attribution">Viewer adapted from Archify 2.17 by tt-a1i and Cocoon AI, MIT licensed. Proof-specific dataset and rendering by archify-proofs-overview.</p>`;
  // applyTemplate replaces the complete cards slot including its sentinels.
  html = html.replace(cards, () => `${cards}${index}`);
  html = html.replace('</body>', () => `${fragments}${useFragments}${recordsHtml(data, 'dag')}${proofRuntime({ items: [...graph.nodes.values()].map(({ id, x, y }) => ({ id, x: x + box.w / 2, y: y + box.h / 2 })), uses: data.uses.map(({ id }) => ({ id })), main_items: mainItems })}</body>`);
  const graph_preservation = representationReceipt(data, html, 'dag');
  if (graph_preservation.status !== 'pass') throw new Error('Rendered SVG does not preserve the supplied item and use identities.');
  return { html, items: data.items.length, uses: data.uses.length, viewBox: [graph.width, graph.height], graph_mode: 'dag', graph_preservation, geometry };
}

try {
  if (process.argv.length !== 4) throw new Error('Usage: node scripts/render.mjs prepared.json output.html');
  const inputPath = path.resolve(process.argv[2]), outputPath = path.resolve(process.argv[3]);
  if (inputPath === outputPath) throw new Error('Output must differ from the prepared dataset.');
  const inputBytes = fs.readFileSync(inputPath);
  const result = render(JSON.parse(inputBytes.toString('utf8').replace(/^\uFEFF/u, '')));
  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  const candidate = `${outputPath}.${process.pid}.tmp`;
  try { fs.writeFileSync(candidate, result.html, { encoding: 'utf8', flag: 'wx' }); fs.renameSync(candidate, outputPath); }
  finally { if (fs.existsSync(candidate)) fs.unlinkSync(candidate); }
  console.log(JSON.stringify({ ok: true, output: outputPath, items: result.items, uses: result.uses, viewBox: result.viewBox, graph_mode: result.graph_mode, graph_preservation: result.graph_preservation, geometry: result.geometry, bytes: Buffer.byteLength(result.html), input_sha256: crypto.createHash('sha256').update(inputBytes).digest('hex'), artifact_sha256: crypto.createHash('sha256').update(result.html).digest('hex'), browser_review: 'not_performed', visual_review: 'not_performed', mathematical_assessment: 'not_performed' }));
} catch (error) {
  console.error(JSON.stringify({ ok: false, error: error.message, ...(error.stage ? { stage: error.stage } : {}), ...(error.diagnostics ? { diagnostics: error.diagnostics } : {}) }));
  process.exitCode = 1;
}
